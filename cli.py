"""
ENTRYPOINTS — the two commands the demo runs through.

    python cli.py discover "look up member 12345 in branch 001 and read the savings balance"
    python cli.py replay lookup_member_balance --member_id 12345 --branch_code 001

`discover` is the only command that contacts a model, and the only one that
needs a key. `replay` and the whole test suite run offline — that asymmetry is
the project's central claim, so it is worth being able to demonstrate it by
simply unsetting the key.

The app is started by the command itself, so a demo is one line with no setup.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from app.server import AppServer
from core.agent import DEFAULT_MODEL, AnthropicPlanner, DiscoveryAgent
from core.driver import WebDriver, shutdown_playwright
from core.evals import DEFAULT_RUNS, load_suite, run_suite, suite_path
from core.handoff import CliOperator
from core.safety import ActionNotAllowed, Allowlist
from core.logging import EvidenceLog, new_run_id
from core.recorder import assemble_artifact
from core.replay import (
    BusinessOutcome,
    Refused,
    HardFailure,
    ParameterError,
    ReplayEngine,
    Success,
)
from core.schema import (
    A11yRoleNameLocator,
    Action,
    Parameter,
    Provenance,
    RelationalLocator,
    Risk,
    RoleNamePresent,
    Status,
    ValueType,
    load_artifact,
)

ENV_FILE = Path(__file__).parent / ".env"
ARTIFACT_DIR = Path(__file__).parent / "artifacts"


def parse_params(pairs: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for pair in pairs:
        if "=" not in pair:
            raise SystemExit(f"--param expects name=value, got {pair!r}")
        name, _, value = pair.partition("=")
        out[name.strip()] = value.strip()
    return out


def load_env() -> None:
    """Read `.env` without taking a dependency for it.

    Anything already exported wins, so an explicit environment variable is
    never silently overridden by a stale file.

    One deliberate exception: an `ANTHROPIC_BASE_URL` we did not put there is
    removed. The SDK reads that variable, and an ambient one — inherited from
    whatever shell launched us — would quietly send this project's API key to
    an endpoint it was not issued for. Config that redirects credentials has to
    be declared here, not inherited.
    """
    declared: set[str] = set()
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            declared.add(key.strip())
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))

    if "ANTHROPIC_BASE_URL" in os.environ and "ANTHROPIC_BASE_URL" not in declared:
        os.environ.pop("ANTHROPIC_BASE_URL")


def cmd_discover(args: argparse.Namespace) -> int:
    load_env()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print(
            "No ANTHROPIC_API_KEY found.\n"
            f"Put it in {ENV_FILE} as ANTHROPIC_API_KEY=sk-ant-... or export it.\n"
            "Discovery is the only command that needs one; replay and tests do not.",
            file=sys.stderr,
        )
        return 2

    params = parse_params(args.param)
    unknown = set(args.sensitive) - set(params)
    if unknown:
        print(f"--sensitive names an undeclared parameter: {sorted(unknown)}", file=sys.stderr)
        return 2

    log = EvidenceLog(run_id=new_run_id(), kind="discovery")
    # Mask the VALUES of parameters the caller marked sensitive, everywhere in
    # the evidence — including inside the captured accessibility tree.
    log.mark_sensitive(*(params[name] for name in args.sensitive))

    with AppServer(port=args.port) as app:
        driver = WebDriver(allowed_origins=[app.base_url], headed=args.headed).start()
        try:
            agent = DiscoveryAgent(
                driver=driver,
                planner=AnthropicPlanner(model=args.model),
                log=log,
                max_steps=args.max_steps,
                timeout_s=args.timeout,
                params=params,
            )
            print(f"Discovering against {app.entry_url}")
            print(f"Goal: {args.goal}\n")
            run = agent.run(args.goal, app.entry_url)

            artifact = None
            if run.recorded_steps() and run.entry_checkpoint is not None:
                artifact = assemble_artifact(
                    capability_id=args.capability,
                    description=args.goal,
                    product_identity=args.product,
                    # The artifact must name the port the demo actually uses,
                    # not whatever ephemeral port a test happened to get.
                    entry_url=f"http://127.0.0.1:{args.port}/members.html",
                    allowed_origins=[f"http://127.0.0.1:{args.port}"],
                    parameters=[
                        Parameter(
                            name=name,
                            type=ValueType.STRING,
                            description=f"Value supplied for {name}.",
                            sensitive=name in args.sensitive,
                        )
                        for name in params
                    ],
                    steps=run.recorded_steps(),
                    entry_checkpoint=run.entry_checkpoint,
                    param_values=params,
                    provenance=Provenance(
                        discovered_by=args.model,
                        discovered_at=log.run_id,
                        evidence_ref=str(log.dir),
                    ),
                )
        finally:
            driver.stop()
            shutdown_playwright()

    print(f"\nStopped: {run.stop_reason.value}")
    print(f"Steps:   {len(run.steps)}")
    if run.summary:
        print(f"Summary: {log.redact(run.summary)}")
    print(f"Evidence: {log.transcript_path}")

    if artifact is not None:
        ARTIFACT_DIR.mkdir(exist_ok=True)
        path = ARTIFACT_DIR / f"{artifact.capability_id}.json"
        path.write_text(artifact.model_dump_json(indent=2) + "\n")
        print(f"Artifact: {path}  [{artifact.status.value}]")
        print(f"\nNext:  python cli.py review {artifact.capability_id}")
    return 0 if run.succeeded else 1


def review_concerns(artifact) -> list[str]:
    """What a draft still needs from a person, in the order it matters.

    Only things discovery genuinely cannot know. A review report that lists
    non-issues trains the reviewer to skim past the real one.
    """
    concerns: list[str] = []

    # The most consequential and least obvious. Discovery saw one path, so its
    # checkpoint asserts the marker from that path. Left alone, looking up a
    # member who does not exist fails this checkpoint and is reported as a hard
    # failure — turning a legitimate business answer into a crash, which is the
    # single mistake the error taxonomy exists to prevent.
    #
    # Clicks only: a navigate to a known entry URL has one deterministic
    # result, so asserting the page loaded is correct rather than narrow.
    narrow = [
        s
        for s in artifact.steps
        if s.action is Action.CLICK and isinstance(s.checkpoint.condition, RoleNamePresent)
    ]
    if narrow:
        concerns.append(
            "Happy-path checkpoints on "
            + ", ".join(s.step_id for s in narrow)
            + ". These assert the one outcome discovery happened to see. Widen "
            "them to any_of(that marker, an alert being present) so the step "
            "passes whenever the application RESPONDED, and let known_outcomes "
            "decide what the response meant."
        )

    if not artifact.known_outcomes:
        concerns.append(
            "No known_outcomes. Discovery only walked the happy path, so every "
            "business outcome — no such member, permission denied — would be "
            "reported as a hard failure. Author them before approving."
        )

    # Flagging every read-only flow for "no irreversible step" would be noise:
    # a lookup genuinely has none. Instead, look at what the control is called.
    # Discovery cannot tell a commitment from a search by watching either one
    # succeed, but a button named "Confirm" is worth a human's attention.
    committing = [
        s
        for s in artifact.steps
        if s.action is Action.CLICK
        and s.risk is Risk.SAFE
        and any(word in _control_name(s).lower() for word in COMMITTING_WORDS)
    ]
    if committing:
        concerns.append(
            "Possibly irreversible, recorded as safe: "
            + ", ".join(f"{s.step_id} ({_control_name(s)!r})" for s in committing)
            + ". Discovery cannot tell a commitment from a search by watching it "
            "succeed. Mark it risk=irreversible if it commits, so replay demands "
            "explicit confirmation before running it."
        )
    return concerns


COMMITTING_WORDS = (
    "confirm", "submit", "save", "delete", "remove", "transfer",
    "pay", "approve", "send", "create", "open sub",
)


def _control_name(step) -> str:
    """What the targeted control is called, for a human skimming the report."""
    if step.target is None:
        return ""
    for rung in step.target.rungs:
        if isinstance(rung, A11yRoleNameLocator):
            return rung.name
        if isinstance(rung, RelationalLocator):
            return rung.anchor_text
    return ""


def cmd_review(args: argparse.Namespace) -> int:
    """The human half of the draft -> approved lifecycle.

    Discovery walked exactly one path, so what it emits is a proposal. This
    command shows what a reviewer has to decide, and refuses to approve until
    the branches discovery never saw have been authored by a person.
    """
    path = ARTIFACT_DIR / f"{args.capability}.json"
    if not path.exists():
        print(f"No artifact at {path}", file=sys.stderr)
        return 2
    artifact = load_artifact(path)

    print(f"{artifact.capability_id}  [{artifact.status.value}]  {artifact.product_identity}")
    print(f"  {artifact.description}\n")
    print(f"  parameters: {[p.name + ('*' if p.sensitive else '') for p in artifact.parameters]}")
    print(f"  outputs:    {[o.name for o in artifact.outputs]}\n")

    for step in artifact.steps:
        rungs = len(step.target.rungs) if step.target else 0
        print(f"  {step.step_id}  ({step.action.value}, {rungs} rung(s), risk={step.risk.value})")
        print(f"      checkpoint: {step.checkpoint.description}")

    concerns = review_concerns(artifact)
    if concerns:
        print("\nNEEDS A HUMAN:")
        for c in concerns:
            print(f"  - {c}")

    if not args.approve:
        print(f"\nEdit {path}, then re-run with --approve.")
        return 0

    approved = artifact.model_copy(
        update={
            "status": Status.APPROVED,
            "provenance": (artifact.provenance or Provenance()).model_copy(
                update={"approved_by": args.by, "approved_at": new_run_id()}
            ),
        }
    )
    # Re-validate from scratch: the schema is what enforces that an approved
    # artifact has authored outcomes, and model_copy does not re-run it.
    from core.schema import Artifact

    try:
        approved = Artifact.model_validate(approved.model_dump())
    except Exception as exc:
        print(f"\nCannot approve: {exc}", file=sys.stderr)
        return 1

    path.write_text(approved.model_dump_json(indent=2) + "\n")
    print(f"\nApproved by {args.by}. {path}")
    return 0


def cmd_replay(args: argparse.Namespace, extra: list[str]) -> int:
    """Deterministic execution. No model, no key, no network beyond the app.

    The parameters come from the artifact, not from this file: a capability
    declares what it takes, so `--help` for one is generated from its own
    contract rather than hardcoded here.
    """
    path = ARTIFACT_DIR / f"{args.capability}.json"
    if not path.exists():
        print(f"No artifact at {path}", file=sys.stderr)
        return 2
    artifact = load_artifact(path)

    if artifact.status is not Status.APPROVED and not args.allow_draft:
        print(
            f"{artifact.capability_id} is still a draft. A draft has not had its "
            "failure branches authored, so every business outcome would be "
            "reported as a hard failure.\n"
            f"Review it (python cli.py review {artifact.capability_id}) or pass "
            "--allow-draft to run it anyway.",
            file=sys.stderr,
        )
        return 2

    # Build the parameter flags this capability declares.
    sub = argparse.ArgumentParser(prog=f"cli.py replay {args.capability}")
    for parameter in artifact.parameters:
        sub.add_argument(
            f"--{parameter.name}",
            required=parameter.required,
            help=f"{parameter.description}"
            + (" [sensitive]" if parameter.sensitive else ""),
        )
    values = vars(sub.parse_args(extra))

    log = EvidenceLog(run_id=new_run_id(), kind="replay")
    entry_url = args.entry_url or artifact.target.entry_url
    port = int(entry_url.rsplit(":", 1)[1].split("/")[0])

    # Serve the target app on the port the artifact names, so replay stands up
    # its own world exactly as discovery did.
    with AppServer(port=port):
        # The full policy off the artifact's tenant slot — origins, routes and
        # permitted verbs — enforced inside the driver.
        allowlist = Allowlist.from_target(artifact.target)
        driver = WebDriver(allowlist=allowlist, headed=args.headed).start()
        try:
            engine = ReplayEngine(
                driver=driver,
                artifact=artifact,
                log=log,
                confirmed=args.confirm,
                entry_url=entry_url,
                operator=CliOperator() if args.operator else None,
            )
            report = engine.run(**{k: v for k, v in values.items() if v is not None})
        except ParameterError as exc:
            print(f"Parameter error: {exc}", file=sys.stderr)
            return 2
        except ActionNotAllowed as exc:
            # Refused by policy inside the driver, before anything happened.
            print(f"\nREFUSED BY ALLOWLIST\n  {exc}", file=sys.stderr)
            print(f"  policy: {allowlist.describe()}", file=sys.stderr)
            return 3
        finally:
            driver.stop()
            shutdown_playwright()

    print(f"\n{report}")
    print(f"  classification: {report.classification.value}")
    if isinstance(report, Success):
        for name, value in report.outputs.items():
            print(f"  {name}: {value}")
    if isinstance(report, BusinessOutcome):
        print(f"  {report.message}")
    if isinstance(report, Refused):
        print(f"  reason: {report.reason}")
        print(f"  policy: {report.policy}")
    if isinstance(report, HardFailure):
        print(f"  expected: {report.expected}")
        print(f"  observed: {report.observed}")
        if report.screenshot:
            print(f"  screenshot: {report.screenshot}")
    print(f"  evidence: {log.transcript_path}")

    # A business outcome is a real answer, so it exits 0. A refusal gets its
    # own code: a caller should be able to tell "policy stopped us" from "the
    # screen was wrong" without parsing text.
    if isinstance(report, Refused):
        return 3
    return 0 if report.ok else 1


def cmd_eval(args: argparse.Namespace) -> int:
    """Run a capability's scenario suite N times and score it.

    No model and no key: this replays the same engine production uses, which
    is the only reason the numbers mean anything.
    """
    path = ARTIFACT_DIR / f"{args.capability}.json"
    if not path.exists():
        print(f"No artifact at {path}", file=sys.stderr)
        return 2
    artifact = load_artifact(path)

    spec = suite_path(args.capability)
    if not spec.exists():
        print(f"No eval suite at {spec}", file=sys.stderr)
        return 2
    suite = load_suite(spec)

    log = EvidenceLog(run_id=new_run_id(), kind="eval")
    for parameter in artifact.parameters:
        if parameter.sensitive:
            for scenario in suite.scenarios:
                if value := scenario.params.get(parameter.name):
                    log.mark_sensitive(value)

    entry_url = artifact.target.entry_url
    port = int(entry_url.rsplit(":", 1)[1].split("/")[0])

    print(f"{artifact.capability_id}  [{artifact.status.value}]  "
          f"{len(suite.scenarios)} scenarios x {args.runs} runs\n")
    header = f"{'scenario':34} {'expected':18} {'result':12} {'median':>8}"
    print(header)
    print("-" * len(header))

    def show(result) -> None:
        mark = "PASS" if result.passed else "FAIL"
        print(f"{result.scenario.name:34} {result.scenario.expect.value:18} "
              f"{mark} {result.consistency:>5} {result.median_ms:>7.0f}ms")
        for problem in result.failures:
            print(f"    - {problem}")

    with AppServer(port=port):
        driver = WebDriver(
            allowlist=Allowlist.from_target(artifact.target), headed=args.headed
        ).start()
        try:
            report = run_suite(
                driver=driver,
                artifact=artifact,
                suite=suite,
                log=log,
                entry_url=entry_url,
                runs=args.runs,
                on_scenario=show,
            )
        finally:
            driver.stop()
            shutdown_playwright()

    print("\nOutcome reachability — every branch the artifact declares:")
    for name in report.declared_outcomes:
        hits = sum(r.outcomes[name] for r in report.results)
        print(f"  {'reached ' if hits else 'NEVER   '} {name} ({hits})")
    if report.unreached_outcomes:
        print(
            "\n  An outcome nothing can trigger is not a safety net. Check its "
            "detect condition against what the app actually renders."
        )

    consistent = sum(r.passed for r in report.results)
    print(f"\n{consistent}/{len(report.results)} scenarios consistent at {args.runs} runs; "
          f"{len(report.outcomes_reached)}/{len(report.declared_outcomes)} outcomes reached")
    print(f"Evidence: {log.transcript_path}")
    return 0 if report.passed else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cli.py", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    d = sub.add_parser("discover", help="LLM-driven discovery against the live app")
    d.add_argument("goal", help="what to accomplish, in plain language")
    d.add_argument("--model", default=DEFAULT_MODEL)
    d.add_argument("--port", type=int, default=8000)
    d.add_argument("--max-steps", type=int, default=15)
    d.add_argument("--timeout", type=float, default=180)
    d.add_argument("--headed", action="store_true", help="watch it happen")
    d.add_argument(
        "--param",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="declare an input. Anything typed must be one of these, so the "
        "artifact can hold a reference instead of a literal. Repeatable.",
    )
    d.add_argument(
        "--sensitive",
        action="append",
        default=[],
        metavar="NAME",
        help="mark a declared parameter sensitive: its value is masked "
        "throughout the evidence. Repeatable.",
    )
    d.add_argument("--capability", default="lookup_member_balance")
    d.add_argument("--product", default="CoreBankPro@4")
    d.set_defaults(func=cmd_discover)

    v = sub.add_parser("review", help="review a draft capability, then approve it")
    v.add_argument("capability")
    v.add_argument("--approve", action="store_true")
    v.add_argument("--by", default=os.environ.get("USER", "reviewer"))
    v.set_defaults(func=cmd_review)

    r = sub.add_parser(
        "replay",
        help="deterministic replay of a recorded capability (no model, no key)",
    )
    r.add_argument("capability")
    r.add_argument("--confirm", action="store_true", help="authorise irreversible steps")
    r.add_argument("--allow-draft", action="store_true")
    r.add_argument("--headed", action="store_true")
    r.add_argument(
        "--operator",
        action="store_true",
        help="on a hard failure, hand the live browser session to you over CDP "
        "instead of stopping. Implies you are watching the terminal.",
    )
    r.add_argument(
        "--entry-url",
        help="override the artifact's entry URL — the per-tenant slot, and how "
        "the drift demo points at ?drift=1",
    )
    r.set_defaults(func=cmd_replay)

    e = sub.add_parser(
        "eval", help="run a capability's scenario suite N times and score it"
    )
    e.add_argument("capability")
    e.add_argument("--runs", type=int, default=DEFAULT_RUNS)
    e.add_argument("--headed", action="store_true")
    e.set_defaults(func=cmd_eval)

    args, extra = parser.parse_known_args(argv)
    if args.command == "replay":
        return cmd_replay(args, extra)
    if extra:
        parser.error(f"unrecognised arguments: {' '.join(extra)}")
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

"""
EVAL HARNESS — turning documented behaviour into measured behaviour.

A capability's README table is a set of claims a human types out once and
eyeballs. This runs the whole table, N times each, and scores it.

Two things it proves, and the second is the one that matters:

**Consistency.** Each scenario lands in its expected bucket N/N, or it does
not. A wait tuned too tight shows up as 19/20 rather than as an incident
months later.

**Reachability — every declared outcome actually fires.** This closes a gap
nothing else in the system can. The schema checks that a `known_outcome` is
well-formed; it cannot know whether `text_present("No such membr")` — one typo
— matches anything in the real application. Those conditions are hand-authored
by a human at review time, which is exactly when typos happen, and the failure
is silent: the outcome simply never matches, and a business outcome quietly
degrades into a `HardFailure` in production. That is the brief's central
mistake re-entering through the back door after the whole taxonomy was built to
prevent it.

So the harness fails a suite that leaves any declared outcome unreached, even
if every scenario passed. An outcome nothing can trigger is not a safety net;
it is a comment that looks like one.

What this does **not** prove: that the declared outcomes are the *right* ones,
or that the application has no behaviour nobody thought of. A human authored
the branches; this confirms they fire.

Scenarios live in `evals/<capability_id>.json` — declarative, reviewable, and
versioned next to the artifact rather than buried in Python.
"""

from __future__ import annotations

import statistics
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from core.driver import Driver
from core.logging import EvidenceLog
from core.replay import BusinessOutcome, ReplayEngine, ReplayReport
from core.schema import Artifact, Classification

EVAL_DIR = Path("evals")
DEFAULT_RUNS = 20


# --------------------------------------------------------------------------- #
# The suite
# --------------------------------------------------------------------------- #


class RungExpectation(BaseModel):
    """Assert which rung resolved a given step.

    Bucket-only assertions would pass the drift scenario even if rung 1 had
    silently started working again, which would quietly stop testing the
    fallback at all.
    """

    model_config = ConfigDict(extra="forbid")

    step_id: str
    rung: int = Field(ge=1, le=4)


class Scenario(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str
    params: dict[str, str] = Field(default_factory=dict)
    entry_query: str = Field(
        default="", description="appended to the entry URL, e.g. '?drift=1'"
    )
    confirm: bool = Field(
        default=False, description="authorise irreversible steps for this scenario"
    )
    expect: Classification
    expect_outcome: str | None = Field(
        default=None, description="for a business outcome, which one"
    )
    expect_rung: RungExpectation | None = None


class EvalSuite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    capability_id: str
    scenarios: list[Scenario] = Field(min_length=1)


def load_suite(path: str | Path) -> EvalSuite:
    return EvalSuite.model_validate_json(Path(path).read_text())


def suite_path(capability_id: str, root: Path = EVAL_DIR) -> Path:
    return root / f"{capability_id}.json"


# --------------------------------------------------------------------------- #
# Results
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ScenarioResult:
    scenario: Scenario
    runs: int
    buckets: Counter
    outcomes: Counter
    timings_ms: list[float]
    failures: list[str] = field(default_factory=list)

    @property
    def hits(self) -> int:
        return self.buckets[self.scenario.expect.value]

    @property
    def passed(self) -> bool:
        return self.hits == self.runs and not self.failures

    @property
    def consistency(self) -> str:
        return f"{self.hits}/{self.runs}"

    @property
    def median_ms(self) -> float:
        return statistics.median(self.timings_ms) if self.timings_ms else 0.0


@dataclass(frozen=True)
class EvalReport:
    capability_id: str
    runs: int
    results: list[ScenarioResult]
    declared_outcomes: list[str]
    evidence_dir: str = ""

    @property
    def outcomes_reached(self) -> set[str]:
        return {name for r in self.results for name in r.outcomes}

    @property
    def unreached_outcomes(self) -> list[str]:
        return sorted(set(self.declared_outcomes) - self.outcomes_reached)

    @property
    def passed(self) -> bool:
        """Every scenario consistent, and no declared outcome left unproven."""
        return all(r.passed for r in self.results) and not self.unreached_outcomes


# --------------------------------------------------------------------------- #
# The runner
# --------------------------------------------------------------------------- #


def run_suite(
    driver: Driver,
    artifact: Artifact,
    suite: EvalSuite,
    log: EvidenceLog,
    entry_url: str,
    runs: int = DEFAULT_RUNS,
    on_scenario=None,
) -> EvalReport:
    """Replay every scenario `runs` times and score the lot.

    Deliberately reuses one driver and one browser: the harness measures the
    capability's consistency, not the cost of starting Chromium.
    """
    results: list[ScenarioResult] = []

    for scenario in suite.scenarios:
        buckets: Counter = Counter()
        outcomes: Counter = Counter()
        timings: list[float] = []
        failures: list[str] = []

        for _ in range(runs):
            started = time.time()
            report = ReplayEngine(
                driver=driver,
                artifact=artifact,
                log=log,
                confirmed=scenario.confirm,
                entry_url=entry_url + scenario.entry_query,
            ).run(**scenario.params)
            timings.append((time.time() - started) * 1000)

            buckets[report.classification.value] += 1
            outcomes.update(report.outcomes_seen)
            failures.extend(_assert(scenario, report))

        result = ScenarioResult(
            scenario=scenario,
            runs=runs,
            buckets=buckets,
            outcomes=outcomes,
            timings_ms=timings,
            # Deduplicated: the same mismatch on all 20 runs is one finding,
            # not twenty.
            failures=sorted(set(failures)),
        )
        results.append(result)
        _log_scenario(log, result)
        if on_scenario is not None:
            on_scenario(result)

    report = EvalReport(
        capability_id=artifact.capability_id,
        runs=runs,
        results=results,
        declared_outcomes=[o.name for o in artifact.known_outcomes],
        evidence_dir=str(log.dir),
    )
    _write_transcript(log, artifact, report)
    return report


def _assert(scenario: Scenario, report: ReplayReport) -> list[str]:
    """Everything the scenario claims beyond the bucket."""
    problems: list[str] = []

    if report.classification is not scenario.expect:
        problems.append(
            f"expected {scenario.expect.value}, got {report.classification.value}"
        )

    if scenario.expect_outcome is not None:
        got = report.name if isinstance(report, BusinessOutcome) else None
        if got != scenario.expect_outcome:
            problems.append(f"expected outcome {scenario.expect_outcome!r}, got {got!r}")

    if scenario.expect_rung is not None:
        step = next(
            (s for s in report.steps if s.step_id == scenario.expect_rung.step_id), None
        )
        if step is None:
            problems.append(f"step {scenario.expect_rung.step_id!r} never ran")
        elif step.rung != scenario.expect_rung.rung:
            problems.append(
                f"{scenario.expect_rung.step_id} resolved at rung {step.rung}, "
                f"expected rung {scenario.expect_rung.rung}"
            )
    return problems


# --------------------------------------------------------------------------- #
# Evidence
# --------------------------------------------------------------------------- #


def _log_scenario(log: EvidenceLog, result: ScenarioResult) -> None:
    log.event(
        "eval_scenario",
        scenario=result.scenario.name,
        expected=result.scenario.expect.value,
        consistency=result.consistency,
        buckets=dict(result.buckets),
        outcomes=sorted(result.outcomes),
        median_ms=round(result.median_ms),
        failures=result.failures,
    )


def _write_transcript(log: EvidenceLog, artifact: Artifact, report: EvalReport) -> None:
    log.narrate(f"# Eval — {artifact.capability_id}")
    log.table(
        [
            ("Status", artifact.status.value),
            ("Runs per scenario", report.runs),
            ("Scenarios", len(report.results)),
            ("Model in the loop", "none"),
        ]
    )

    log.heading("Scenarios", level=2)
    log.narrate("| scenario | expected | consistency | median ms | |")
    log.narrate("|---|---|---|---|---|")
    for r in report.results:
        mark = "PASS" if r.passed else "FAIL"
        log.narrate(
            f"| {r.scenario.name} | {r.scenario.expect.value} | {r.consistency} "
            f"| {r.median_ms:.0f} | {mark} |"
        )
    for r in report.results:
        if r.failures:
            log.narrate("")
            log.narrate(f"**{r.scenario.name}** — {'; '.join(r.failures)}")
            log.narrate(f"  buckets seen: {dict(r.buckets)}")

    log.heading("Outcome reachability", level=2)
    log.narrate(
        "Every outcome the artifact declares, and whether any scenario actually "
        "made it fire. A declared outcome that nothing can trigger is not a "
        "safety net — it is a comment that looks like one."
    )
    log.narrate("")
    for name in report.declared_outcomes:
        hits = sum(r.outcomes[name] for r in report.results)
        log.narrate(f"- `{name}` — {'reached' if hits else 'NEVER REACHED'} ({hits} times)")
    if report.unreached_outcomes:
        log.narrate("")
        log.narrate(f"**Unreached: {report.unreached_outcomes}**")

    log.heading("Result", level=2)
    log.table(
        [
            ("Passed", report.passed),
            ("Scenarios consistent", f"{sum(r.passed for r in report.results)}/{len(report.results)}"),
            ("Outcomes reached", f"{len(report.outcomes_reached)}/{len(report.declared_outcomes)}"),
        ]
    )
    log.event(
        "eval_complete",
        passed=report.passed,
        runs=report.runs,
        unreached_outcomes=report.unreached_outcomes,
    )

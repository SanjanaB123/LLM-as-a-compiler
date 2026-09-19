"""
DETERMINISTIC REPLAY (milestone M4) — the production path. No model, ever.

Load an approved artifact, take typed parameters, walk the steps. The model
that discovered this flow is not here and is not consulted; that is the entire
point of having recorded it.

**Deterministic must not mean blind.** Every step verifies its checkpoint
before the next one runs, and every wait is a wait *on a stated condition* —
never a slept interval chosen by guesswork. A flow that acts without checking
is not deterministic, it is merely fast at being wrong.

**The error taxonomy is the load-bearing part.** Five outcomes, and the
distinctions between them are the point — collapsing any two is how this gets
built badly:

    Success          the steps ran, the checkpoints held, outputs extracted.

    BusinessOutcome  the application gave a real answer that happens not to be
                     the happy one — "no such member", "permission denied".
                     This is RETURNED CLEANLY, not raised. Treating it as a
                     failure is the single most common way to get this wrong:
                     the caller asked a question and the bank answered it.

    Recoverable      a known interstitial or a slow load. Handled in the loop
                     and the run continues, so it is a *behaviour* rather than
                     a result — it never reaches the caller.

    HardFailure      the surface was not what the artifact says it should be:
                     no rung resolved, or a checkpoint failed for no reason we
                     can classify. Stops with the step, what was expected, what
                     was actually on screen, and a screenshot — the doorway to
                     escalation in M6.

    Refused          policy forbade the action. NOT a hard failure, because a
                     hard failure escalates to a human, and escalating a
                     refusal would mean asking a person to do by hand the thing
                     the allowlist just prevented.

**How a business outcome is recognised.** Off the screen, generically, through
the same `Condition` vocabulary the checkpoints use. Replay never branches on
the input: the if/else that decides what 99999 means lives in the application,
and duplicating it here would make the whole taxonomy a lie — we would be
reporting our own guess back to ourselves.

The checkpoint after a submit asserts only that the application *responded*.
Deciding what the response meant is the outcomes' job, and keeping those two
separate is what lets a business outcome be clean rather than a failed step.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from core.driver import Driver, TargetNotFound
from core.handoff import (
    ControlLedger,
    InterventionRequest,
    Ownership,
    describe_change,
    record_intervention,
)
from core.logging import EvidenceLog
from core.safety import ActionNotAllowed
from core.schema import (
    Action,
    Artifact,
    Checkpoint,
    Classification,
    Outcome,
    Recovery,
    Risk,
    Step,
    ValueType,
)

MAX_RECOVERY_ROUNDS = 3
RECOVERY_PROBE_MS = 0  # one instantaneous evaluation; no waiting to find nothing


class ParameterError(Exception):
    """The caller's arguments do not match what the capability declares."""


class ConfirmationRequired(Exception):
    """An irreversible step was reached without explicit approval (M5)."""


# --------------------------------------------------------------------------- #
# The result contract
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class StepOutcome:
    """What one step did, for the evidence log and for debugging a failure."""

    step_id: str
    action: str
    rung: int | None = None
    fell_back: bool = False
    attempts: tuple[str, ...] = ()
    checkpoint_ok: bool = False
    waited_ms: int = 0
    extracted: str | None = None
    recoveries: tuple[str, ...] = ()
    error: str | None = None


@dataclass(frozen=True)
class ReplayReport:
    """Base of the result contract. Callers branch on the concrete type."""

    capability_id: str
    steps: tuple[StepOutcome, ...] = ()
    evidence_dir: str = ""
    elapsed_ms: int = 0

    @property
    def classification(self) -> Classification:
        raise NotImplementedError

    @property
    def ok(self) -> bool:
        """Did the caller get a real answer? True for a business outcome too:
        the bank answered the question, just not with the happy path."""
        return self.classification in (
            Classification.SUCCESS,
            Classification.BUSINESS_OUTCOME,
        )

    @property
    def needs_a_human(self) -> bool:
        """Only a hard failure escalates. A refusal must never reach a person
        as "please finish this manually" — policy forbade it."""
        return self.classification is Classification.HARD_FAILURE


@dataclass(frozen=True)
class Success(ReplayReport):
    outputs: dict[str, str] = field(default_factory=dict)

    @property
    def classification(self) -> Classification:
        return Classification.SUCCESS

    def __str__(self) -> str:
        return f"Success({self.outputs})"


@dataclass(frozen=True)
class BusinessOutcome(ReplayReport):
    """A legitimate answer that is not the happy path. Returned, not raised."""

    name: str = ""
    message: str = ""
    outputs: dict[str, str] = field(default_factory=dict)

    @property
    def classification(self) -> Classification:
        return Classification.BUSINESS_OUTCOME

    def __str__(self) -> str:
        return f"BusinessOutcome({self.name})"


@dataclass(frozen=True)
class Refused(ReplayReport):
    """Policy said no. A category of its own, and deliberately not a HardFailure.

    A hard failure means the surface was not what we expected, which is a
    reason to retry, investigate, or hand control to a person. A refusal means
    we were never permitted to do this at all. Reporting one as the other is
    the same class of mistake as reporting a business outcome as a crash, and
    here it is worse: a hard failure is the doorway to human escalation (M6),
    so a misfiled refusal would end up asking a human to go and do by hand the
    very thing policy forbade.
    """

    step_id: str = ""
    reason: str = ""
    policy: str = ""

    @property
    def classification(self) -> Classification:
        return Classification.REFUSED

    def __str__(self) -> str:
        return f"Refused(step={self.step_id}, reason={self.reason!r})"


@dataclass(frozen=True)
class HardFailure(ReplayReport):
    """The surface was not what the artifact says. Debuggable on purpose."""

    step_id: str = ""
    expected: str = ""
    observed: str = ""
    screenshot: str | None = None

    @property
    def classification(self) -> Classification:
        return Classification.HARD_FAILURE

    def __str__(self) -> str:
        return f"HardFailure(step={self.step_id}, expected={self.expected!r}, observed={self.observed!r})"


# --------------------------------------------------------------------------- #
# The executor
# --------------------------------------------------------------------------- #


class ReplayEngine:
    def __init__(
        self,
        driver: Driver,
        artifact: Artifact,
        log: EvidenceLog,
        confirmed: bool = False,
        entry_url: str | None = None,
        operator=None,
        max_interventions: int = 1,
    ) -> None:
        self.driver = driver
        self.artifact = artifact
        self.log = log
        # M5 turns this into a proper approval callback. The default is refusal:
        # an irreversible action should need someone to say yes, not need
        # someone to remember to say no.
        self.confirmed = confirmed
        # A per-tenant override of where the capability runs. The artifact keeps
        # environment-specific values in a separable slot precisely so this does
        # not require re-recording the flow.
        self.entry_url = entry_url or artifact.target.entry_url
        # Without an operator, a hard failure simply stops — the M4 behaviour.
        # With one, it becomes a request for help (M6). Bounded, because a
        # human who cannot fix it on the second try will not fix it on the
        # twentieth, and a loop that keeps paging someone is its own outage.
        self.operator = operator
        self.max_interventions = max_interventions
        self.ledger = ControlLedger()

    @property
    def allowlist_description(self) -> str:
        policy = getattr(self.driver, "allowlist", None)
        return policy.describe() if policy is not None else "unrestricted"

    # -- entry point ------------------------------------------------------- #

    def run(self, **params: str) -> ReplayReport:
        started = time.time()
        values = self._bind_parameters(params)
        self._mark_sensitive(values)
        self._open_transcript(values)

        outputs: dict[str, str] = {}
        step_outcomes: list[StepOutcome] = []
        interventions = 0
        index = 0

        while index < len(self.artifact.steps):
            step = self.artifact.steps[index]
            outcome, terminal = self._run_step(step, values, outputs)
            step_outcomes.append(outcome)
            self._narrate(outcome)

            if terminal is not None:
                escalate = (
                    self.operator is not None
                    and terminal.needs_a_human
                    and interventions < self.max_interventions
                )
                if escalate:
                    interventions += 1
                    resumed, skip = self._escalate(step, terminal, values)
                    if resumed:
                        # Re-observed: if the human already completed this step,
                        # advance past it rather than doing it a second time.
                        index += 1 if skip else 0
                        continue
                return self._finish(terminal, step_outcomes, started)
            index += 1

        return self._finish(
            Success(capability_id=self.artifact.capability_id, outputs=dict(outputs)),
            step_outcomes,
            started,
        )

    # -- escalation (M6) --------------------------------------------------- #

    def _escalate(self, step: Step, failure, values: dict[str, str]) -> tuple[bool, bool]:
        """Hand the live session to a person, then take it back and look again.

        Returns (resumed, step_already_done). The second flag is why this
        re-observes instead of trusting a remembered position: the operator may
        have completed the blocked step themselves, and repeating it could mean
        submitting something twice.
        """
        before = self.driver.observe()
        request = InterventionRequest(
            capability_id=self.artifact.capability_id,
            step_id=step.step_id,
            what_it_was_doing=step.description,
            expected=failure.expected,
            observed=failure.observed,
            cdp_url=getattr(self.driver, "cdp_url", "unavailable"),
            page_url=before.url,
            screenshot=failure.screenshot,
        )

        self.ledger.transfer_to(Ownership.HUMAN, f"hard failure at {step.step_id}")
        self.log.event("handoff", to="human", step=step.step_id)
        self.driver.detach()  # the browser stays up; only this client lets go

        try:
            response = self.operator.take_control(request)
        finally:
            self.driver.reattach()
            self.ledger.transfer_to(Ownership.AUTOMATION, "operator handed back")
            self.log.event("handoff", to="automation", step=step.step_id)

        after = self.driver.observe()
        changes = describe_change(before, after)
        record_intervention(self.log, request, self.ledger, response, changes)

        if not response.resumed:
            return False, False

        # Look before acting: the step may no longer need doing.
        already_done = self.driver.check(step.checkpoint.condition, 500, values).ok
        return True, already_done

    # -- one step ---------------------------------------------------------- #

    def _run_step(
        self, step: Step, values: dict[str, str], outputs: dict[str, str]
    ) -> tuple[StepOutcome, ReplayReport | None]:
        if step.risk is Risk.IRREVERSIBLE and not self.confirmed:
            return (
                StepOutcome(step_id=step.step_id, action=step.action.value,
                            error="irreversible step blocked: no confirmation given"),
                HardFailure(
                    capability_id=self.artifact.capability_id,
                    step_id=step.step_id,
                    expected="explicit confirmation before an irreversible action",
                    observed="none was given; pass --confirm to authorise this run",
                ),
            )

        recoveries: list[str] = []
        rung = None
        fell_back = False
        attempts: tuple[str, ...] = ()
        extracted = None

        # An interstitial that arrived before we could act would swallow the
        # action entirely, so clear the way first.
        recoveries += self._apply_recoveries()

        try:
            resolution = self._act(step, values, outputs)
        except ActionNotAllowed as exc:
            # Caught before the generic handler below: a refusal must not be
            # laundered into a hard failure.
            self.log.event("refused", step=step.step_id, reason=str(exc))
            return (
                StepOutcome(step_id=step.step_id, action=step.action.value, error=str(exc)),
                Refused(
                    capability_id=self.artifact.capability_id,
                    step_id=step.step_id,
                    reason=str(exc),
                    policy=self.allowlist_description,
                ),
            )
        except TargetNotFound as exc:
            # Nothing on this surface matches any recorded rung. That is the
            # artifact disagreeing with reality, not a business answer.
            return self._hard(step, f"a control matching {len(step.target.rungs)} recorded rung(s)", str(exc), recoveries)
        except Exception as exc:
            return self._hard(step, step.checkpoint.description, f"{type(exc).__name__}: {exc}", recoveries)

        if resolution is not None:
            rung, fell_back = resolution.rung, resolution.fell_back
            attempts = tuple(
                f"rung {a.rung} ({a.kind}): {'ok' if a.ok else a.detail}"
                for a in resolution.attempts
            )
            if step.extract_to:
                extracted = resolution.text
                outputs[step.extract_to] = resolution.text or ""

        # Clear anything the action itself raised, BEFORE judging what happened.
        # The maintenance notice arrives as a role=alert, so a checkpoint of
        # "the app responded somehow" is satisfied by the interstitial, and
        # outcome matching would then be reading a screen that is still in the
        # way. Dismissing first means every judgement below is made against the
        # state the step was actually trying to reach.
        recoveries += self._apply_recoveries()

        check = self._verify(step.checkpoint, values, recoveries)

        # Outcomes are evaluated whether or not the checkpoint held. A matched
        # outcome always explains the situation better than "the checkpoint
        # failed", and on the widened checkpoints it is the only thing that can
        # tell a member panel from a refusal.
        matched = self._match_outcome()

        outcome = StepOutcome(
            step_id=step.step_id,
            action=step.action.value,
            rung=rung,
            fell_back=fell_back,
            attempts=attempts,
            checkpoint_ok=check.ok,
            waited_ms=check.waited_ms,
            extracted=extracted,
            recoveries=tuple(recoveries),
        )

        if matched is not None and matched.classification is Classification.BUSINESS_OUTCOME:
            return outcome, BusinessOutcome(
                capability_id=self.artifact.capability_id,
                name=matched.name,
                message=matched.message or "",
                outputs=dict(outputs),
            )

        if matched is not None and matched.classification is Classification.HARD_FAILURE:
            # A failure the artifact anticipated, so it can be named instead of
            # described — "our type step did not land", not "something is off".
            return self._hard(
                step, step.checkpoint.description,
                matched.message or matched.name, recoveries, name=matched.name,
            )

        if not check.ok:
            return self._hard(step, step.checkpoint.description, check.observed, recoveries)

        return outcome, None

    def _act(self, step: Step, values: dict[str, str], outputs: dict[str, str]):
        if step.action is Action.NAVIGATE:
            self.driver.navigate(self._resolve(step.value_ref, values, outputs))
            return None
        if step.action is Action.CLICK:
            return self.driver.click(step.target)
        if step.action is Action.TYPE:
            return self.driver.type(step.target, self._resolve(step.value_ref, values, outputs))
        if step.action is Action.READ:
            return self.driver.read(step.target)
        raise ValueError(f"unsupported action {step.action}")

    # -- waiting, recovering, classifying ---------------------------------- #

    def _verify(self, checkpoint: Checkpoint, values: dict[str, str], recoveries: list[str]):
        """Wait for the checkpoint; if it fails, try a recovery and wait again.

        The wait is on the condition itself, bounded by the artifact's own
        timeout. Nothing here sleeps a fixed interval: a blind sleep is either
        too short and flaky, or too long and slow, and it is never evidence
        that anything actually happened.
        """
        check = self.driver.check(checkpoint.condition, checkpoint.timeout_ms, values)
        rounds = 0
        while not check.ok and rounds < MAX_RECOVERY_ROUNDS:
            applied = self._apply_recoveries()
            if not applied:
                break
            recoveries.extend(applied)
            rounds += 1
            check = self.driver.check(checkpoint.condition, checkpoint.timeout_ms, values)
        return check

    def _apply_recoveries(self) -> list[str]:
        """Clear any known interstitial that is on screen right now.

        This is why `Recoverable` never reaches the caller: it is handled here
        and the run carries on. Detection is instantaneous — waiting to find
        out that nothing is wrong would add a delay to every single step.
        """
        applied: list[str] = []
        for recovery in self.artifact.recoveries:
            for _ in range(recovery.max_attempts):
                if not self.driver.check(recovery.detect, RECOVERY_PROBE_MS).ok:
                    break
                if not self._perform_recovery(recovery):
                    break
                applied.append(recovery.name)
                self.log.event("recovered", recovery=recovery.name, strategy=recovery.strategy)
        return applied

    def _perform_recovery(self, recovery: Recovery) -> bool:
        if recovery.strategy == "dismiss" and recovery.dismiss_target is not None:
            try:
                self.driver.click(recovery.dismiss_target)
                return True
            except TargetNotFound:
                return False
        # "wait_retry" needs no action: the caller re-checks the condition, and
        # the waiting already happened inside that check.
        return recovery.strategy == "wait_retry"

    def _match_outcome(self) -> Outcome | None:
        """Read the outcome off the screen, in declaration order.

        Never from the input. The application decides what 99999 means; our job
        is to notice what it said, not to reimplement its rules.
        """
        for outcome in self.artifact.known_outcomes:
            if outcome.classification is Classification.SUCCESS:
                continue  # not terminal; the happy path just carries on
            if self.driver.check(outcome.detect, 0).ok:
                return outcome
        return None

    # -- parameters -------------------------------------------------------- #

    def _bind_parameters(self, params: dict[str, str]) -> dict[str, str]:
        """Typed binding, against what the artifact declares.

        Returned keyed by reference (`params.member_id`) because that is how
        steps and checkpoints refer to values — resolving at the boundary means
        nothing downstream has to know the difference between a parameter and
        a tenant setting.
        """
        declared = {p.name for p in self.artifact.parameters}
        unknown = set(params) - declared
        if unknown:
            raise ParameterError(
                f"{self.artifact.capability_id} does not take {sorted(unknown)}; "
                f"it takes {sorted(declared)}"
            )
        missing = [p.name for p in self.artifact.parameters if p.required and p.name not in params]
        if missing:
            raise ParameterError(f"missing required parameter(s): {missing}")

        values: dict[str, str] = {}
        for parameter in self.artifact.parameters:
            if parameter.name not in params:
                continue
            values[f"params.{parameter.name}"] = _coerce(
                params[parameter.name], parameter.type, parameter.name
            )
        values["target.entry_url"] = self.entry_url
        return values

    def _resolve(self, ref: str | None, values: dict[str, str], outputs: dict[str, str]) -> str:
        if ref is None:
            raise ValueError("step needs a value_ref but has none")
        if ref.startswith("outputs."):
            return outputs[ref.split(".", 1)[1]]
        if ref in values:
            return values[ref]
        if ref.startswith("target."):
            name = ref.split(".", 1)[1]
            if name in self.artifact.target.overrides:
                return self.artifact.target.overrides[name]
            return str(getattr(self.artifact.target, name))
        raise ValueError(f"cannot resolve {ref}")

    def _mark_sensitive(self, values: dict[str, str]) -> None:
        """Bind the logger's masking to the artifact's own `sensitive` flags, so
        a value is hidden because the capability says it is sensitive — not
        because the caller remembered to ask."""
        for ref, value in values.items():
            if self.artifact.is_sensitive_ref(ref):
                self.log.mark_sensitive(value)

    # -- results and evidence ---------------------------------------------- #

    def _hard(
        self,
        step: Step,
        expected: str,
        observed: str,
        recoveries: list[str],
        name: str | None = None,
    ) -> tuple[StepOutcome, HardFailure]:
        shot = None
        try:
            shot = str(
                self.driver.screenshot(Path(self.log.dir) / f"failure-{step.step_id}.png")
            )
        except Exception:  # evidence is best-effort; never mask the real failure
            pass
        self.log.event(
            "hard_failure",
            step=step.step_id,
            outcome=name,
            expected=expected,
            observed=observed,
            screenshot=shot,
        )
        return (
            StepOutcome(
                step_id=step.step_id,
                action=step.action.value,
                recoveries=tuple(recoveries),
                error=observed,
            ),
            HardFailure(
                capability_id=self.artifact.capability_id,
                step_id=step.step_id,
                expected=expected,
                observed=observed,
                screenshot=shot,
            ),
        )

    def _finish(
        self, report: ReplayReport, steps: list[StepOutcome], started: float
    ) -> ReplayReport:
        elapsed = int((time.time() - started) * 1000)
        report = type(report)(
            **{
                **report.__dict__,
                "steps": tuple(steps),
                "evidence_dir": str(self.log.dir),
                "elapsed_ms": elapsed,
            }
        )
        self.log.heading("Result", level=2)
        rows = [("Classification", report.classification.value), ("Elapsed", f"{elapsed} ms")]
        if isinstance(report, Success):
            rows.append(("Outputs", report.outputs))
        if isinstance(report, BusinessOutcome):
            rows += [("Outcome", report.name), ("Meaning", report.message)]
        if isinstance(report, Refused):
            rows += [("Refused at", report.step_id), ("Reason", report.reason), ("Policy", report.policy)]
        if isinstance(report, HardFailure):
            rows += [
                ("Failed step", report.step_id),
                ("Expected", report.expected),
                ("Observed", report.observed),
                ("Screenshot", report.screenshot or "—"),
            ]
        self.log.table(rows)
        self.log.event(
            "replay_complete",
            classification=report.classification.value,
            result=str(report),
            elapsed_ms=elapsed,
        )
        return report

    def _open_transcript(self, values: dict[str, str]) -> None:
        self.log.narrate(f"# Replay transcript — {self.log.run_id}")
        self.log.table(
            [
                ("Capability", self.artifact.capability_id),
                ("Status", self.artifact.status.value),
                ("Product", self.artifact.product_identity),
                ("Entry URL", self.entry_url),
                ("Parameters", {k: v for k, v in values.items() if k.startswith("params.")}),
                ("Model in the loop", "none"),
            ]
        )
        self.log.event("replay_start", capability=self.artifact.capability_id)

    def _narrate(self, outcome: StepOutcome) -> None:
        self.log.heading(f"{outcome.step_id} — {outcome.action}", level=2)
        rows: list[tuple[str, object]] = []
        if outcome.rung is not None:
            detail = f"rung {outcome.rung}"
            if outcome.fell_back:
                failed = [a for a in outcome.attempts if "ok" not in a]
                detail += f" — after {len(failed)} sturdier rung(s) failed"
            rows.append(("Resolved at", detail))
        if outcome.fell_back:
            rows.append(("Fallback trail", list(outcome.attempts)))
        rows.append(
            ("Checkpoint", f"{'passed' if outcome.checkpoint_ok else 'FAILED'} after {outcome.waited_ms} ms")
        )
        if outcome.recoveries:
            rows.append(("Recovered from", list(outcome.recoveries)))
        if outcome.extracted:
            rows.append(("Read", outcome.extracted))
        if outcome.error:
            rows.append(("ERROR", outcome.error))
        self.log.table(rows)
        self.log.event(
            "step",
            step=outcome.step_id,
            rung=outcome.rung,
            fell_back=outcome.fell_back,
            checkpoint_ok=outcome.checkpoint_ok,
            waited_ms=outcome.waited_ms,
            recoveries=list(outcome.recoveries),
            extracted=outcome.extracted,
            error=outcome.error,
        )


def _coerce(raw: str, kind: ValueType, name: str) -> str:
    """Typed parameters, checked at the boundary.

    Values stay strings because that is what gets typed into a field; the type
    is a contract with the caller, so a bad one is caught here rather than
    surfacing as mystifying behaviour three steps into a bank UI.
    """
    try:
        if kind is ValueType.INTEGER:
            int(raw)
        elif kind is ValueType.NUMBER:
            float(raw)
        elif kind is ValueType.BOOLEAN:
            if raw.lower() not in ("true", "false", "1", "0", "yes", "no"):
                raise ValueError(raw)
    except ValueError:
        raise ParameterError(f"{name} must be {kind.value}, got {raw!r}") from None
    return raw

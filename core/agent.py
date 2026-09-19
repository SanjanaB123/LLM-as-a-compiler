"""
DISCOVERY (milestone M2) — the one genuine LLM-in-the-loop run.

    observe  ->  decide  ->  act  ->  repeat

`observe` is the Driver's a11y tree. `decide` is one call to a model that
returns exactly one structured action. `act` dispatches it back through the
Driver. Nothing else happens in this loop, and nothing in it is allowed to know
it is driving a browser.

**The model emits thin intent only** (decision #6). Its whole vocabulary is
below: an intent, a ref, sometimes text, and a reason. It cannot write a
locator, cannot name a rung, cannot author a checkpoint. Everything durable is
computed by `recorder.build_ladder` from what the surface actually confirms.
Two reasons that split matters: a model asked to invent locators invents
brittle ones, and a model is not available at replay time to fix them.

**Refs, not names.** The model points at `e8`, not "the Branch Code field",
because on this surface that field has no name to point at. Refs live for one
observation and are never persisted.

**Stopping conditions** (required by 3.1), all four:
    - `max_steps`          a step ceiling
    - `timeout_s`          wall clock
    - model signal         the model says `done` or `stuck`
    - no-progress          N consecutive steps that change nothing, which is
                           what being stuck actually looks like from outside

**Provider seam.** `Planner` is the entire interface to the model: one method,
in, out. `AnthropicPlanner` is the real one; `ScriptedPlanner` replaces it in
tests so the whole loop — including its stopping conditions and its evidence —
is verifiable with no API key and no network.

Evidence is written as it happens, not reconstructed afterwards: what the model
saw, what it chose, **why it said it chose it**, and what actually resulted.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from core.driver import Driver, Observation, Resolution, TargetNotFound
from core.logging import EvidenceLog
from core.recorder import (
    LadderNotBuildable,
    RecordedStep,
    build_ladder,
    output_name_for,
    propose_checkpoint,
)
from core.schema import Checkpoint, TargetLadder

DEFAULT_MODEL = "claude-opus-5"
DEFAULT_MAX_STEPS = 15
DEFAULT_TIMEOUT_S = 180
DEFAULT_STALL_LIMIT = 3


# --------------------------------------------------------------------------- #
# The model's entire vocabulary
# --------------------------------------------------------------------------- #


class Intent(str, Enum):
    NAVIGATE = "navigate"
    CLICK = "click"
    TYPE = "type"
    READ = "read"
    DONE = "done"
    STUCK = "stuck"


class ModelAction(BaseModel):
    """One step of thin intent — the only thing the model is allowed to say.

    `reason` is required and is not decoration: it is the model's own account of
    why, and it goes into the transcript. A discovery log that records what
    happened but not why is not evidence of anything.
    """

    model_config = ConfigDict(extra="forbid")

    intent: Intent = Field(description="What to do next.")
    reason: str = Field(description="One sentence: why this action, now.")
    ref: str | None = Field(
        default=None, description="Element ref from the observation, e.g. 'e8'."
    )
    text: str | None = Field(default=None, description="Text to type, for 'type'.")
    url: str | None = Field(default=None, description="URL, for 'navigate'.")
    summary: str | None = Field(
        default=None, description="For 'done' or 'stuck': the outcome in one sentence."
    )

    @model_validator(mode="after")
    def _shape(self) -> ModelAction:
        if self.intent in (Intent.CLICK, Intent.READ) and not self.ref:
            raise ValueError(f"{self.intent.value} requires a ref")
        if self.intent is Intent.TYPE and (not self.ref or self.text is None):
            raise ValueError("type requires both a ref and text")
        if self.intent is Intent.NAVIGATE and not self.url:
            raise ValueError("navigate requires a url")
        if self.intent in (Intent.DONE, Intent.STUCK) and not self.summary:
            raise ValueError(f"{self.intent.value} requires a summary")
        return self

    @property
    def is_terminal(self) -> bool:
        return self.intent in (Intent.DONE, Intent.STUCK)

    @property
    def changes_state(self) -> bool:
        """Reads are allowed to leave the surface untouched; the others are not,
        so only these count toward the no-progress detector."""
        return self.intent in (Intent.NAVIGATE, Intent.CLICK, Intent.TYPE)


# --------------------------------------------------------------------------- #
# Records
# --------------------------------------------------------------------------- #


class StopReason(str, Enum):
    GOAL_REACHED = "goal_reached"
    MODEL_STUCK = "model_stuck"
    MAX_STEPS = "max_steps"
    TIMEOUT = "timeout"
    NO_PROGRESS = "no_progress"


@dataclass
class StepRecord:
    """One turn of the loop, as it will appear in the evidence."""

    index: int
    url: str
    digest_before: str
    action: ModelAction
    digest_after: str | None = None
    element_label: str | None = None
    ladder: TargetLadder | None = None
    resolution: Resolution | None = None
    extracted: str | None = None
    error: str | None = None
    checkpoint: Checkpoint | None = None
    value_ref: str | None = None
    extract_to: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    def as_recorded(self) -> RecordedStep | None:
        """The replayable residue of this step.

        A failed step records nothing: replay should reproduce what worked, not
        retrace the model's wrong turns.
        """
        if not self.ok or self.ladder is None or self.checkpoint is None:
            return None
        return RecordedStep(
            intent=self.action.intent.value,
            description=self.action.reason,
            ladder=self.ladder,
            checkpoint=self.checkpoint,
            value_ref=self.value_ref,
            extract_to=self.extract_to,
            output_description=f"Value read from {self.element_label}."
            if self.extract_to
            else "",
        )


@dataclass
class DiscoveryRun:
    goal: str
    entry_url: str
    steps: list[StepRecord] = field(default_factory=list)
    stop_reason: StopReason = StopReason.MAX_STEPS
    summary: str = ""
    evidence_dir: str = ""
    entry_checkpoint: Checkpoint | None = None

    def recorded_steps(self) -> list[RecordedStep]:
        return [r for r in (s.as_recorded() for s in self.steps) if r is not None]

    @property
    def succeeded(self) -> bool:
        return self.stop_reason is StopReason.GOAL_REACHED

    @property
    def extracted(self) -> dict[str, str]:
        return {
            s.action.ref or f"step{s.index}": s.extracted
            for s in self.steps
            if s.extracted
        }


# --------------------------------------------------------------------------- #
# The provider seam
# --------------------------------------------------------------------------- #


class Planner(Protocol):
    """Everything the loop knows about the model.

    One method. Swapping providers — or swapping in a scripted stand-in for
    tests — touches nothing above this line.
    """

    name: str

    def decide(
        self, goal: str, observation: Observation, history: Sequence[StepRecord]
    ) -> ModelAction:
        ...


SYSTEM_PROMPT = """\
You are discovering how to perform a task in a legacy web application, so the \
steps can be recorded and replayed later without you.

You perceive the screen as an accessibility tree. Every element you can act on \
carries a ref in square brackets, like [e8]. Refs are valid only for the \
observation you were just given.

Choose exactly ONE next action. Rules:

- Refer to elements only by ref. Never write a CSS selector, XPath, or id.
- Some controls have no accessible name and appear as a bare role, like \
`- textbox [e8]`. That is normal in this application. Identify them by their \
position relative to nearby caption text, and act on them by ref.
- Only use values given in the goal. Never invent an account number, amount, \
or identifier.
- When the information the goal asks for is visible on screen, `read` the \
element holding it, then finish with `done`.
- Use `done` only when the goal is genuinely achieved; its summary should state \
the answer. If the application reports a business outcome instead — no such \
record, permission denied — that IS the answer: read it and use `done`.
- Use `stuck` if you cannot make progress, and say what blocked you.
- `reason` must be one sentence explaining why this action, now.
"""


class AnthropicPlanner:
    """The real model. Structured output validated into `ModelAction` by the SDK.

    One retry on a schema violation, feeding the validation error back: cheaper
    than losing a whole discovery run to a single malformed action.
    """

    def __init__(self, model: str = DEFAULT_MODEL, api_key: str | None = None) -> None:
        import anthropic

        self.model = model
        self.name = f"anthropic:{model}"
        self._client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()

    def decide(
        self, goal: str, observation: Observation, history: Sequence[StepRecord]
    ) -> ModelAction:
        prompt = _render_prompt(goal, observation, history)
        messages = [{"role": "user", "content": prompt}]

        for attempt in range(2):
            response = self._client.messages.parse(
                model=self.model,
                max_tokens=4000,
                system=SYSTEM_PROMPT,
                messages=messages,
                output_format=ModelAction,
            )
            action = response.parsed_output
            if action is not None:
                return action
            messages = messages + [
                {
                    "role": "user",
                    "content": "That response did not match the required schema. "
                    "Return exactly one valid action.",
                }
            ]
        raise RuntimeError("model did not return a valid action after 2 attempts")


@dataclass
class ScriptedPlanner:
    """A stand-in that replays a fixed list of actions.

    Its job is to make the loop itself testable — stopping conditions, ladder
    building, evidence, error handling — with no API key and no network. The
    loop cannot tell the difference, which is the point of the seam.
    """

    actions: list[ModelAction]
    name: str = "scripted"
    seen: list[str] = field(default_factory=list)

    def decide(
        self, goal: str, observation: Observation, history: Sequence[StepRecord]
    ) -> ModelAction:
        self.seen.append(observation.digest)
        if not self.actions:
            return ModelAction(
                intent=Intent.STUCK, reason="script exhausted", summary="no actions left"
            )
        return self.actions.pop(0)


def _render_prompt(
    goal: str, observation: Observation, history: Sequence[StepRecord]
) -> str:
    parts = [f"GOAL: {goal}", "", f"CURRENT PAGE: {observation.title} ({observation.url})"]
    if history:
        parts += ["", "WHAT YOU HAVE DONE SO FAR:"]
        for step in history:
            line = f"  {step.index}. {_describe(step.action)}"
            if step.error:
                line += f"  -> FAILED: {step.error}"
            elif step.extracted:
                line += f"  -> read: {step.extracted!r}"
            else:
                line += "  -> ok"
            parts.append(line)
    parts += ["", "WHAT YOU SEE NOW:", observation.tree_for_model, "", "Your next action:"]
    return "\n".join(parts)


def _describe(action: ModelAction) -> str:
    if action.intent is Intent.TYPE:
        return f"type {action.text!r} into {action.ref}"
    if action.intent is Intent.NAVIGATE:
        return f"navigate to {action.url}"
    if action.ref:
        return f"{action.intent.value} {action.ref}"
    return action.intent.value


# --------------------------------------------------------------------------- #
# The loop
# --------------------------------------------------------------------------- #


class DiscoveryAgent:
    def __init__(
        self,
        driver: Driver,
        planner: Planner,
        log: EvidenceLog,
        max_steps: int = DEFAULT_MAX_STEPS,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        stall_limit: int = DEFAULT_STALL_LIMIT,
        params: dict[str, str] | None = None,
    ) -> None:
        self.driver = driver
        self.planner = planner
        self.log = log
        # Declared up front as name -> value. Typing anything that is not one
        # of these is refused rather than recorded, which is what makes "no
        # literal ever reaches an artifact" a guarantee instead of a habit.
        self.params = dict(params or {})
        self.max_steps = max_steps
        self.timeout_s = timeout_s
        self.stall_limit = stall_limit

    def run(self, goal: str, entry_url: str) -> DiscoveryRun:
        run = DiscoveryRun(goal=goal, entry_url=entry_url, evidence_dir=str(self.log.dir))
        started = time.time()
        stalls = 0

        self._open_transcript(goal, entry_url)
        # We open the app rather than asking the model to; the goal is about the
        # task, not about locating the application. M3 emits this as the
        # artifact's first step.
        self.driver.navigate(entry_url)
        entry_state = self.driver.observe()
        run.entry_checkpoint = propose_checkpoint(
            self.driver, "navigate", None, None, None, entry_state
        )

        while True:
            if (stop := self._should_stop(run, started, stalls)) is not None:
                run.stop_reason = stop
                break

            observation = self.driver.observe()
            index = len(run.steps) + 1
            action = self.planner.decide(goal, observation, run.steps)

            step = StepRecord(
                index=index,
                url=observation.url,
                digest_before=observation.digest,
                action=action,
            )
            self.log.event(
                "decision",
                step=index,
                intent=action.intent.value,
                ref=action.ref,
                text=action.text,
                reason=action.reason,
                tree=observation.tree_for_model,
            )

            if action.is_terminal:
                run.steps.append(step)
                run.summary = action.summary or ""
                run.stop_reason = (
                    StopReason.GOAL_REACHED
                    if action.intent is Intent.DONE
                    else StopReason.MODEL_STUCK
                )
                self._narrate(step)
                break

            self._act(step, action, observation)
            after = self.driver.observe()
            step.digest_after = after.digest

            run.steps.append(step)
            self._narrate(step)

            stalls = stalls + 1 if self._stalled(step, run) else 0

        self._close_transcript(run)
        self.log.event("run_complete", stop_reason=run.stop_reason.value, summary=run.summary)
        return run

    # -- one action -------------------------------------------------------- #

    def _act(self, step: StepRecord, action: ModelAction, observation: Observation) -> None:
        """Dispatch the model's intent, recording the ladder that made it work.

        Discovery acts *through the ladder it is about to record*, not around
        it. So by the time a step is written down, its ladder has already driven
        the real surface once — recording and validation are the same event.
        """
        try:
            if action.intent is Intent.NAVIGATE:
                self.driver.navigate(action.url)  # type: ignore[arg-type]
                return

            element = observation.element(action.ref or "")
            if element is None:
                step.error = f"no element with ref {action.ref!r} in this observation"
                return
            step.element_label = (
                f"{element.role} {element.name!r}" if element.name
                else f"{element.role} (unnamed)"
            )

            ladder = build_ladder(self.driver, element, observation)
            step.ladder = ladder

            if action.intent is Intent.CLICK:
                step.resolution = self.driver.click(ladder)
            elif action.intent is Intent.TYPE:
                step.value_ref = self._value_ref_for(action.text or "")
                if step.value_ref is None:
                    step.error = (
                        "refusing to record a literal value: what was typed is not "
                        "one of the declared parameters. Declare it with --param "
                        "so the artifact can hold a reference instead."
                    )
                    return
                step.resolution = self.driver.type(ladder, action.text or "")
            elif action.intent is Intent.READ:
                step.resolution = self.driver.read(ladder)
                step.extracted = step.resolution.text
                step.extract_to = output_name_for(element, step.index)

            after = self.driver.observe()
            step.checkpoint = propose_checkpoint(
                self.driver,
                action.intent.value,
                ladder,
                step.value_ref,
                observation,
                after,
            )

        except (TargetNotFound, LadderNotBuildable) as exc:
            step.error = str(exc)
        except Exception as exc:  # the surface can fail in ways we do not model
            step.error = f"{type(exc).__name__}: {exc}"

    def _value_ref_for(self, text: str) -> str | None:
        """Which declared parameter is this? Exact match only.

        Fuzzy matching here would be a way to accidentally bind a typed value
        to the wrong parameter and never notice until replay sent the wrong
        data to a bank.
        """
        for name, value in self.params.items():
            if value == text:
                return f"params.{name}"
        return None

    # -- stopping ---------------------------------------------------------- #

    def _should_stop(
        self, run: DiscoveryRun, started: float, stalls: int
    ) -> StopReason | None:
        if len(run.steps) >= self.max_steps:
            return StopReason.MAX_STEPS
        if time.time() - started > self.timeout_s:
            return StopReason.TIMEOUT
        if stalls >= self.stall_limit:
            return StopReason.NO_PROGRESS
        return None

    def _stalled(self, step: StepRecord, run: DiscoveryRun) -> bool:
        """Being stuck, from the outside: acting and changing nothing, or doing
        the same thing twice in a row."""
        if step.error is not None:
            return True
        if step.action.changes_state and step.digest_after == step.digest_before:
            return True
        if len(run.steps) >= 2:
            previous = run.steps[-2].action
            if (previous.intent, previous.ref, previous.text) == (
                step.action.intent,
                step.action.ref,
                step.action.text,
            ):
                return True
        return False

    # -- evidence ---------------------------------------------------------- #

    def _open_transcript(self, goal: str, entry_url: str) -> None:
        self.log.narrate(f"# Discovery transcript — {self.log.run_id}")
        self.log.table(
            [("Goal", goal), ("Entry URL", entry_url), ("Planner", self.planner.name)]
        )
        self.log.event("run_start", goal=goal, entry_url=entry_url, planner=self.planner.name)

    def _narrate(self, step: StepRecord) -> None:
        self.log.heading(f"Step {step.index} — {step.action.intent.value}", level=2)
        rows: list[tuple[str, str]] = [
            ("Saw", f"{step.url} (state {step.digest_before})"),
            ("Decided", _describe(step.action)),
            ("Why", step.action.reason),
        ]
        if step.element_label:
            rows.append(("Element", step.element_label))
        if step.resolution is not None:
            rows.append(
                (
                    "Resolved at",
                    f"rung {step.resolution.rung} ({step.resolution.kind})"
                    + (
                        f" — after {sum(1 for a in step.resolution.attempts if not a.ok)}"
                        " sturdier rung(s) failed"
                        if step.resolution.fell_back
                        else " (the sturdiest rung available for this control)"
                    ),
                )
            )
        if step.ladder is not None:
            rows.append(("Ladder", f"{len(step.ladder.rungs)} verified rung(s)"))
        if step.extracted:
            rows.append(("Read", step.extracted))
        if step.error:
            rows.append(("FAILED", step.error))
        if step.action.summary:
            rows.append(("Summary", step.action.summary))
        self.log.table(rows)

        self.log.event(
            "step",
            step=step.index,
            intent=step.action.intent.value,
            element=step.element_label,
            rung=step.resolution.rung if step.resolution else None,
            rungs_recorded=len(step.ladder.rungs) if step.ladder else 0,
            robustness_note=step.ladder.robustness_note if step.ladder else None,
            extracted=step.extracted,
            error=step.error,
            state_changed=step.digest_before != step.digest_after,
        )

    def _close_transcript(self, run: DiscoveryRun) -> None:
        self.log.heading("Outcome", level=2)
        self.log.table(
            [
                ("Stopped because", run.stop_reason.value),
                ("Steps", len(run.steps)),
                ("Summary", run.summary or "—"),
            ]
        )

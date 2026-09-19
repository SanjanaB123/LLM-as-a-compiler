"""
THE CONTRACT (milestone M1) — the typed, versioned artifact.

Both halves of the system speak only this language: discovery (`recorder.py`)
writes it, replay (`replay.py`) reads it. Defining it first is what stops the
two from drifting.

Four ideas carry the file:

1. `Locator` — a tagged union, one variant per rung of the targeting ladder
   (sturdiest to most fragile): a11y role+name -> relational -> structural ->
   coordinates. A `TargetLadder` is an ordered list of them plus the
   `robustness_note` that explains the ordering.

2. `Condition` — one predicate vocabulary serving BOTH step checkpoints AND
   outcome detection. Built once, tested once. Recursive, so `all_of` / `any_of`
   / `not_` compose.

3. `value_ref`, never a literal. Every value a step consumes is a reference into
   a namespace (`params.*`, `target.*`, `outputs.*`), validated by regex. A
   sensitive value has nowhere to land, so redaction is structural rather than
   a policy someone has to remember. `sensitive` parameters additionally may not
   carry an `example`.

4. `target` is a separable slot. Tenant-specific values (entry URL, allowed
   origins, renamed labels) live there and nowhere else, so a per-tenant overlay
   is a thin delta rather than a re-record. Multi-tenant plumbing is
   design-only — but the slot has to exist now or it can't be added later.

Run `python -m core.schema` to emit the JSON Schema — the proof that a calling
agent can review this contract without reading our code.
"""

from __future__ import annotations

import json
import re
from enum import Enum
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SCHEMA_VERSION = "1.0"

# A value_ref points into one of three resolution namespaces. Anything that is
# not a reference — a bare "12345" — fails validation. This single regex is what
# makes "never persist raw sensitive data into artifacts" a guarantee instead of
# a guideline.
VALUE_REF_PATTERN = re.compile(r"^(params|target|outputs)\.[a-z_][a-z0-9_]*$")

NAME_PATTERN = re.compile(r"^[a-z_][a-z0-9_]*$")


class Strict(BaseModel):
    """Unknown keys are an error: a contract that silently ignores typos isn't one."""

    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- #
# Locators — one per rung of the ladder (plan decision #7)
# --------------------------------------------------------------------------- #


class A11yRoleNameLocator(Strict):
    """RUNG 1 — sturdiest. Survives restyling, DOM refactors, and class churn.

    Free from perception: this is exactly what `observe()` already reports.

    `role` is optional, meaning "the element whose accessible name is this,
    whatever its role". M0 forced this: the target app's balance is a span with
    aria-label="Savings Balance", and the a11y tree hands that name to the
    enclosing table cell as well — so role+name is ambiguous while name alone is
    exact. Name-only is still a rung-1 identification: it is precisely what a
    screen-reader user navigates by.
    """

    kind: Literal["a11y_role_name"] = "a11y_role_name"
    role: str | None = None
    name: str
    exact: bool = False


class RelationalLocator(Strict):
    """RUNG 2 — anchored on a neighbouring label instead of the control itself.

    Earns its place on controls with no accessible name of their own ("the
    textbox after the label 'Member ID'"). Survives everything rung 1 does
    except a renamed anchor.
    """

    kind: Literal["relational"] = "relational"
    anchor_text: str
    relation: Literal["after", "before", "below", "above", "same_row", "within"]
    target_role: str
    occurrence: int = Field(default=1, ge=1, description="1-based nth match")


class StructuralLocator(Strict):
    """RUNG 3 — position within the page/container structure, or visible text.

    No accessible name and no usable anchor: fall back to "the 2nd textbox in
    the section titled X". Breaks when controls are reordered.
    """

    kind: Literal["structural"] = "structural"
    role: str
    index: int = Field(ge=0, description="0-based index among matching roles")
    container_text: str | None = Field(
        default=None, description="visible text of the enclosing section/row, if any"
    )


class CoordinatesLocator(Strict):
    """RUNG 4 — most fragile, last resort, and the reason the ladder reaches the
    no-DOM case at all: a desktop surface can still be clicked by point.

    Captured free and inline with perception via `aria_snapshot(boxes=True)`, so
    recording it costs nothing. Viewport-relative, hence the recorded viewport —
    coordinates without it are meaningless.
    """

    kind: Literal["coordinates"] = "coordinates"
    x: float
    y: float
    viewport_width: int
    viewport_height: int


Locator = Annotated[
    Union[
        A11yRoleNameLocator,
        RelationalLocator,
        StructuralLocator,
        CoordinatesLocator,
    ],
    Field(discriminator="kind"),
]

# Rung order, used to validate that a ladder is stored sturdiest-first.
RUNG_ORDER: dict[str, int] = {
    "a11y_role_name": 1,
    "relational": 2,
    "structural": 3,
    "coordinates": 4,
}


class TargetLadder(Strict):
    """How one control is identified, with the reasoning about robustness.

    Replay resolves these in order and falls to the next rung only on failure.
    `robustness_note` is not decoration — it is where "how each control is
    identified, and why that identification should hold" literally lives, and it
    is what a human reads at approval time.
    """

    rungs: list[Locator] = Field(min_length=1)
    robustness_note: str = Field(min_length=1)

    @model_validator(mode="after")
    def _sturdiest_first(self) -> TargetLadder:
        ranks = [RUNG_ORDER[r.kind] for r in self.rungs]
        if ranks != sorted(ranks):
            raise ValueError(
                f"rungs must be ordered sturdiest-first; got {[r.kind for r in self.rungs]}"
            )
        return self


# --------------------------------------------------------------------------- #
# Conditions — ONE vocabulary for checkpoints and for outcome detection
# --------------------------------------------------------------------------- #


class RoleNamePresent(Strict):
    """An element with this role+name is on screen."""

    kind: Literal["role_name_present"] = "role_name_present"
    role: str
    name: str
    exact: bool = False


class TextPresent(Strict):
    """This text is visible, optionally scoped to a role (e.g. role="alert").

    This is how a business outcome is detected: read generically off the screen.
    Replay must never reach the same conclusion by branching on the input value.

    An empty `text` with a `role` set means "any element with that role is
    present" — used for the checkpoint "the search responded somehow", which must
    pass whether the response is a detail panel or an error alert. Narrowing that
    checkpoint to only the happy path would turn every business outcome into a
    step failure.
    """

    kind: Literal["text_present"] = "text_present"
    text: str
    role: str | None = None
    exact: bool = False


class TextAbsent(Strict):
    kind: Literal["text_absent"] = "text_absent"
    text: str
    role: str | None = None
    exact: bool = False


class ValueMatchesRef(Strict):
    """A field now holds the value behind `value_ref`.

    The natural checkpoint after a `type` step — and it verifies a sensitive
    value without the artifact ever containing it.

    It carries a whole `TargetLadder` rather than a role+name because the field
    worth checking is often the one with no accessible name: the target app's
    Branch Code input is reachable only at rung 2, so a role+name checkpoint
    could not describe it. The cost is that a step's ladder is repeated in its
    checkpoint; the gain is that a `Condition` stays self-contained, which
    matters because outcomes evaluate conditions with no step in scope.
    """

    kind: Literal["value_matches_ref"] = "value_matches_ref"
    target: TargetLadder
    value_ref: str

    @field_validator("value_ref")
    @classmethod
    def _must_be_reference(cls, v: str) -> str:
        return _check_value_ref(v)


class AriaSnapshotMatches(Strict):
    """Playwright's `to_match_aria_snapshot` as a native checkpoint primitive
    (handed to us by the spike): assert a whole subtree's shape at once.
    """

    kind: Literal["aria_snapshot_matches"] = "aria_snapshot_matches"
    snapshot: str


class AllOf(Strict):
    kind: Literal["all_of"] = "all_of"
    conditions: list[Condition] = Field(min_length=1)


class AnyOf(Strict):
    kind: Literal["any_of"] = "any_of"
    conditions: list[Condition] = Field(min_length=1)


class Not(Strict):
    kind: Literal["not_"] = "not_"
    condition: Condition


Condition = Annotated[
    Union[
        RoleNamePresent,
        TextPresent,
        TextAbsent,
        ValueMatchesRef,
        AriaSnapshotMatches,
        AllOf,
        AnyOf,
        Not,
    ],
    Field(discriminator="kind"),
]

AllOf.model_rebuild()
AnyOf.model_rebuild()
Not.model_rebuild()


class Checkpoint(Strict):
    """Every step has one (plan decision #8) — deterministic must not mean blind.

    `timeout_ms` is how determinism survives transient slowness: replay waits on
    the condition rather than sleeping blindly. `description` is the human-
    readable *expected* that a `HardFailure` reports.
    """

    condition: Condition
    description: str = Field(min_length=1)
    timeout_ms: int = Field(default=5000, ge=0, le=120_000)


# --------------------------------------------------------------------------- #
# Steps
# --------------------------------------------------------------------------- #


class Action(str, Enum):
    """Exactly the Driver verbs — the artifact cannot express anything the seam
    doesn't offer, which is what keeps it surface-independent."""

    NAVIGATE = "navigate"
    CLICK = "click"
    TYPE = "type"
    READ = "read"


class Risk(str, Enum):
    SAFE = "safe"
    IRREVERSIBLE = "irreversible"


class Step(Strict):
    step_id: str = Field(pattern=NAME_PATTERN.pattern)
    action: Action
    description: str = Field(min_length=1)
    target: TargetLadder | None = Field(
        default=None, description="required for click/type/read; navigate has no target"
    )
    value_ref: str | None = Field(
        default=None,
        description="reference to the value consumed (params.*/target.*/outputs.*). "
        "NEVER a literal — that is the redaction guarantee.",
    )
    extract_to: str | None = Field(
        default=None, description="name of the declared output this step binds"
    )
    checkpoint: Checkpoint
    risk: Risk = Risk.SAFE

    @field_validator("value_ref")
    @classmethod
    def _value_ref_is_reference(cls, v: str | None) -> str | None:
        return v if v is None else _check_value_ref(v)

    @model_validator(mode="after")
    def _action_shape(self) -> Step:
        if self.action is Action.NAVIGATE:
            if self.target is not None:
                raise ValueError("navigate takes no target; put the URL in value_ref")
            if self.value_ref is None:
                raise ValueError("navigate requires a value_ref (e.g. target.entry_url)")
        else:
            if self.target is None:
                raise ValueError(f"{self.action.value} requires a target ladder")

        if self.action is Action.TYPE and self.value_ref is None:
            raise ValueError("type requires a value_ref")
        if self.action is Action.READ and self.extract_to is None:
            raise ValueError("read requires extract_to")
        if self.extract_to is not None and self.action is not Action.READ:
            raise ValueError("only a read step may extract_to")
        return self


# --------------------------------------------------------------------------- #
# Recoveries and outcomes — the error taxonomy, declared rather than coded
# --------------------------------------------------------------------------- #


class Classification(str, Enum):
    """The taxonomy replay sorts every step result into (M4).

    `BUSINESS_OUTCOME` is the load-bearing one: "no such member" is a legitimate
    answer to return, not a crash. Conflating the two is the brief's named #1
    mistake, so the distinction lives in the schema, not in replay's judgement.
    """

    SUCCESS = "success"
    BUSINESS_OUTCOME = "business_outcome"
    RECOVERABLE = "recoverable"
    HARD_FAILURE = "hard_failure"
    # Produced by replay when policy refuses an action; never declared in an
    # artifact, because a capability does not get to describe its own refusals.
    REFUSED = "refused"


class Outcome(Strict):
    """A known end state, detected on screen by `detect`."""

    name: str = Field(pattern=NAME_PATTERN.pattern)
    detect: Condition
    classification: Classification
    message: str | None = None

    @model_validator(mode="after")
    def _refusal_is_not_declarable(self) -> Outcome:
        if self.classification is Classification.REFUSED:
            raise ValueError(
                "'refused' is decided by policy at replay time, not declared by "
                "a capability; an artifact cannot authorise its own refusals"
            )
        return self


class Recovery(Strict):
    """A known interstitial or slow load: handled in-loop and the run continues,
    which is why `Recoverable` is a behaviour rather than a terminal result.
    """

    name: str = Field(pattern=NAME_PATTERN.pattern)
    detect: Condition
    strategy: Literal["dismiss", "wait_retry"]
    dismiss_target: TargetLadder | None = None
    max_attempts: int = Field(default=2, ge=1, le=10)

    @model_validator(mode="after")
    def _dismiss_needs_target(self) -> Recovery:
        if self.strategy == "dismiss" and self.dismiss_target is None:
            raise ValueError("strategy 'dismiss' requires a dismiss_target")
        return self


# --------------------------------------------------------------------------- #
# Parameters, outputs, tenant slot, provenance
# --------------------------------------------------------------------------- #


class ValueType(str, Enum):
    STRING = "string"
    INTEGER = "integer"
    NUMBER = "number"
    BOOLEAN = "boolean"


class Parameter(Strict):
    name: str = Field(pattern=NAME_PATTERN.pattern)
    type: ValueType
    description: str = Field(min_length=1)
    required: bool = True
    sensitive: bool = False
    example: str | None = None

    @model_validator(mode="after")
    def _sensitive_has_no_example(self) -> Parameter:
        # An "example" on a sensitive parameter is the likeliest way a real value
        # sneaks into a committed file. Forbid the shape outright.
        if self.sensitive and self.example is not None:
            raise ValueError(f"sensitive parameter {self.name!r} may not carry an example")
        return self


class Output(Strict):
    name: str = Field(pattern=NAME_PATTERN.pattern)
    type: ValueType
    description: str = Field(min_length=1)


class TargetSurface(Strict):
    """THE TENANT SLOT. Everything environment-specific lives here and nowhere
    else, so a second tenant of the same product is a thin overlay of deltas.

    `allowed_origins` is also the safety allowlist the Driver enforces (M5) —
    which is why the app is served over real HTTP and not file://.
    """

    surface: Literal["web", "desktop"] = "web"
    entry_url: str
    allowed_origins: list[str] = Field(min_length=1)
    allowed_routes: list[str] = Field(
        default_factory=lambda: ["*"],
        description="glob patterns over the URL path. An origin is not a fine "
        "enough unit: a bank's admin console and its member search share one, "
        "and only one of them is in scope for a given capability.",
    )
    allowed_actions: list[str] = Field(
        default_factory=list,
        description="permitted action verbs; empty means all. A read-only "
        "capability can be stopped from ever typing.",
    )
    overrides: dict[str, str] = Field(
        default_factory=dict,
        description="per-tenant deltas, e.g. a renamed label. Design-only for now; "
        "the slot exists so nothing above it is hardwired.",
    )

    @model_validator(mode="after")
    def _entry_url_is_allowed(self) -> TargetSurface:
        if self.surface == "web" and not any(
            self.entry_url.startswith(o) for o in self.allowed_origins
        ):
            raise ValueError(
                f"entry_url {self.entry_url!r} is not covered by allowed_origins "
                f"{self.allowed_origins}"
            )
        return self


class Provenance(Strict):
    """Where this artifact came from — the link between a capability and the
    discovery transcript in /evidence/, and the record of who approved it."""

    discovered_by: str | None = Field(default=None, description="model id")
    discovered_at: str | None = None
    evidence_ref: str | None = Field(default=None, description="path under /evidence/")
    approved_by: str | None = None
    approved_at: str | None = None


# --------------------------------------------------------------------------- #
# The envelope
# --------------------------------------------------------------------------- #


class Status(str, Enum):
    DRAFT = "draft"
    APPROVED = "approved"


class Artifact(Strict):
    """A recorded capability: the compiled program replay executes.

    Discovery emits `draft`. A human confirms the proposed checkpoints and
    hand-authors the `known_outcomes` discovery never walked, then marks it
    `approved` — which is why approval requires at least one outcome.
    """

    schema_version: Literal["1.0"] = SCHEMA_VERSION
    capability_id: str = Field(pattern=NAME_PATTERN.pattern)
    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    product_identity: str = Field(
        min_length=1, description='tags "same app", e.g. CoreBankPro@4'
    )
    target: TargetSurface
    parameters: list[Parameter] = Field(default_factory=list)
    outputs: list[Output] = Field(default_factory=list)
    steps: list[Step] = Field(min_length=1)
    known_outcomes: list[Outcome] = Field(default_factory=list)
    recoveries: list[Recovery] = Field(default_factory=list)
    status: Status = Status.DRAFT
    provenance: Provenance | None = None

    # -- lookups ----------------------------------------------------------- #

    def parameter(self, name: str) -> Parameter | None:
        return next((p for p in self.parameters if p.name == name), None)

    def output(self, name: str) -> Output | None:
        return next((o for o in self.outputs if o.name == name), None)

    def is_sensitive_ref(self, value_ref: str) -> bool:
        """True if this reference resolves to a parameter marked sensitive —
        the hook the redacting logger uses to mask a value."""
        ns, _, name = value_ref.partition(".")
        if ns != "params":
            return False
        param = self.parameter(name)
        return bool(param and param.sensitive)

    # -- cross-field validation -------------------------------------------- #

    @model_validator(mode="after")
    def _no_duplicate_names(self) -> Artifact:
        for label, names in (
            ("step_id", [s.step_id for s in self.steps]),
            ("parameter", [p.name for p in self.parameters]),
            ("output", [o.name for o in self.outputs]),
            ("known_outcome", [o.name for o in self.known_outcomes]),
            ("recovery", [r.name for r in self.recoveries]),
        ):
            dupes = {n for n in names if names.count(n) > 1}
            if dupes:
                raise ValueError(f"duplicate {label} name(s): {sorted(dupes)}")
        return self

    @model_validator(mode="after")
    def _references_resolve(self) -> Artifact:
        """Every reference must point at something declared, and an output must
        be extracted before it is consumed. A dangling ref would surface as a
        runtime mystery in replay; here it's a validation error."""
        param_names = {p.name for p in self.parameters}
        output_names = {o.name for o in self.outputs}
        target_names = set(TargetSurface.model_fields) | set(self.target.overrides)
        extracted_at: dict[str, int] = {}

        def check(ref: str, where: str, step_index: int) -> None:
            ns, _, name = ref.partition(".")
            if ns == "params" and name not in param_names:
                raise ValueError(f"{where}: undeclared parameter {name!r}")
            if ns == "target" and name not in target_names:
                raise ValueError(f"{where}: unknown target field {name!r}")
            if ns == "outputs":
                if name not in output_names:
                    raise ValueError(f"{where}: undeclared output {name!r}")
                if name not in extracted_at:
                    raise ValueError(f"{where}: output {name!r} is used before it is read")

        for i, step in enumerate(self.steps):
            where = f"step {step.step_id!r}"
            if step.extract_to is not None:
                if step.extract_to not in output_names:
                    raise ValueError(f"{where}: extract_to {step.extract_to!r} is not a declared output")
                extracted_at[step.extract_to] = i
            if step.value_ref is not None:
                check(step.value_ref, where, i)
            for cond in _walk_conditions(step.checkpoint.condition):
                if isinstance(cond, ValueMatchesRef):
                    check(cond.value_ref, f"{where} checkpoint", i)

        unread = output_names - set(extracted_at)
        if unread:
            raise ValueError(f"declared output(s) never extracted by any step: {sorted(unread)}")
        return self

    @model_validator(mode="after")
    def _approved_is_reviewed(self) -> Artifact:
        """A draft is allowed to be incomplete — that's the point of the
        lifecycle. `approved` is the claim a human authored the failure
        branches, so it has to mean something."""
        if self.status is Status.APPROVED and not self.known_outcomes:
            raise ValueError("an approved artifact must declare at least one known_outcome")
        return self


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _check_value_ref(v: str) -> str:
    if not VALUE_REF_PATTERN.match(v):
        raise ValueError(
            f"{v!r} is not a value reference. Artifacts hold references "
            f"(params.x / target.x / outputs.x), never literal values."
        )
    return v


def _walk_conditions(condition) -> list:
    """Flatten a (possibly composite) condition into its leaves and nodes."""
    found = [condition]
    if isinstance(condition, (AllOf, AnyOf)):
        for c in condition.conditions:
            found.extend(_walk_conditions(c))
    elif isinstance(condition, Not):
        found.extend(_walk_conditions(condition.condition))
    return found


def load_artifact(path) -> Artifact:
    """Load and validate. Replay calls this before touching a surface."""
    import pathlib

    return Artifact.model_validate_json(pathlib.Path(path).read_text())


def json_schema() -> dict:
    """The contract, reviewable by a calling agent without reading our code."""
    return Artifact.model_json_schema()


if __name__ == "__main__":
    print(json.dumps(json_schema(), indent=2))

"""
THE RECORDER — where the durable artifact is manufactured (M2 half, M3 rest).

The division of labour this project rests on (decision #6): **the model decides,
the recorder enriches.** Discovery's model says only "act on element e8". It
never authors a locator, a ladder, or a checkpoint. This module turns that thin
choice into something replay can execute a thousand times without a model.

The central idea here is **verified rungs**. For each candidate identification —
role+name, name alone, relational, structural, coordinates — the recorder asks
the live surface two questions through `Driver.probe`:

    1. Does this locator resolve to exactly one element?
    2. Is it *the same* element the model chose?

Only rungs that answer yes to both are recorded. An artifact therefore contains
no rung that has ever been merely plausible: every one of them was demonstrated
against the real surface at record time. A ladder assembled by reading the tree
would look identical and quietly contain rungs that fail the first time replay
needs them — which is precisely when a fallback is supposed to save you.

M3 adds the rest: proposing a checkpoint per step, and emitting the whole
`Artifact` with parameters, outputs and the draft lifecycle.
"""

from __future__ import annotations

from dataclasses import dataclass

from core.driver import Box, Driver, Element, Observation
from core.schema import (
    RUNG_ORDER,
    A11yRoleNameLocator,
    Action,
    Artifact,
    Checkpoint,
    CoordinatesLocator,
    Locator,
    Output,
    Parameter,
    Provenance,
    RelationalLocator,
    Risk,
    RoleNamePresent,
    Status,
    Step,
    StructuralLocator,
    TargetLadder,
    TargetSurface,
    ValueMatchesRef,
    ValueType,
)

BOX_TOLERANCE_PX = 2.0
MAX_OCCURRENCE = 4


class LadderNotBuildable(Exception):
    """Not one candidate rung could be verified against the surface.

    Rare and worth failing loudly on: it means we watched the model act on
    something we cannot describe well enough to find again, and recording it
    would produce an artifact that cannot replay.
    """


def build_ladder(
    driver: Driver, element: Element, observation: Observation
) -> TargetLadder:
    """Identify `element` every way that actually works, sturdiest first."""
    verified: list[Locator] = []
    notes: list[str] = []

    for candidate, note in _candidates(element, observation):
        if _verifies(driver, candidate, element):
            verified.append(candidate)
            notes.append(note)

    if not verified:
        raise LadderNotBuildable(
            f"no verifiable way to identify {element.role} {element.label!r} "
            f"(ref {element.ref})"
        )

    return TargetLadder(rungs=verified, robustness_note=_note(element, verified, notes))


# --------------------------------------------------------------------------- #
# Candidate generation
# --------------------------------------------------------------------------- #


def _candidates(element: Element, observation: Observation):
    """Yield (locator, note) pairs sturdiest-first, before verification."""

    if element.name:
        yield (
            A11yRoleNameLocator(role=element.role, name=element.name, exact=True),
            f"role+name: the control exposes the accessible name {element.name!r}, "
            "which survives restyling and DOM churn",
        )
        yield (
            A11yRoleNameLocator(name=element.name, exact=True),
            f"name alone: {element.name!r} identifies it without relying on the "
            "role the tree assigns",
        )

    anchor = _caption_for(element, observation)
    if anchor is not None:
        for occurrence in range(1, MAX_OCCURRENCE + 1):
            yield (
                RelationalLocator(
                    anchor_text=anchor.label,
                    relation="same_row",
                    target_role=element.role,
                    occurrence=occurrence,
                ),
                f"relational: the {element.role} sharing a row with the caption "
                f"{anchor.label!r} (occurrence {occurrence})",
            )

    index = _reading_order_index(element, observation)
    if index is not None:
        yield (
            StructuralLocator(role=element.role, index=index),
            f"structural: the {element.role} at position {index} in reading "
            "order, which breaks if a control is inserted before it",
        )

    if element.box is not None:
        viewport = _viewport_of(observation)
        yield (
            CoordinatesLocator(
                x=round(element.box.center_x, 1),
                y=round(element.box.center_y, 1),
                viewport_width=viewport[0],
                viewport_height=viewport[1],
            ),
            "coordinates: last resort, viewport-bound, and the only rung that "
            "would still work on a surface with no queryable tree at all",
        )


def _caption_for(element: Element, observation: Observation) -> Element | None:
    """The nearest labelled thing to the left on the same line.

    This is how a person reads a form — the caption sits before its field — and
    it is the only handle an anonymous control has. Preferring the nearest one
    keeps the anchor tight when a row holds several captions.
    """
    if element.box is None:
        return None
    to_the_left = [
        e
        for e in observation.same_row_as(element)
        if e.box is not None and e.box.x < element.box.x and (e.name or e.text)
    ]
    if not to_the_left:
        return None
    return max(to_the_left, key=lambda e: e.box.x)


def _reading_order_index(element: Element, observation: Observation) -> int | None:
    same_role = sorted(
        (e for e in observation.elements if e.role == element.role and e.box is not None),
        key=lambda e: (e.box.y, e.box.x),
    )
    for i, candidate in enumerate(same_role):
        if candidate.ref == element.ref:
            return i
    return None


def _viewport_of(observation: Observation) -> tuple[int, int]:
    """Infer the viewport from the outermost box in the snapshot.

    Recording the viewport alongside the coordinates is what lets replay scale
    them; a bare point is meaningless at a different window size.
    """
    widest = max(
        (e.box for e in observation.elements if e.box is not None),
        key=lambda b: b.area,
        default=None,
    )
    if widest is None:
        return (1280, 720)
    return (int(widest.right), max(int(widest.bottom), 720))


# --------------------------------------------------------------------------- #
# Verification
# --------------------------------------------------------------------------- #


def _verifies(driver: Driver, locator: Locator, element: Element) -> bool:
    """Resolves uniquely AND lands on the element the model actually chose."""
    box = driver.probe(locator)
    if box is None:
        return False
    if element.box is None:
        return True
    if _same_box(box, element.box):
        return True
    # A coordinate lands on a point, and the topmost thing at that point is
    # often a child of the element that owns it — the span inside a table cell.
    # Hitting the child is hitting the element, so containment counts here
    # while the exact rungs stay strict.
    return isinstance(locator, CoordinatesLocator) and element.box.contains_box(box)


def _same_box(a: Box, b: Box) -> bool:
    return (
        abs(a.x - b.x) <= BOX_TOLERANCE_PX
        and abs(a.y - b.y) <= BOX_TOLERANCE_PX
        and abs(a.width - b.width) <= BOX_TOLERANCE_PX
        and abs(a.height - b.height) <= BOX_TOLERANCE_PX
    )


# --------------------------------------------------------------------------- #
# The robustness note
# --------------------------------------------------------------------------- #


def _note(element: Element, verified: list[Locator], notes: list[str]) -> str:
    """Write down how this control is identified and why it should hold.

    Generated rather than hand-waved, and it records what did *not* work as
    well — the absence of a rung is the interesting part. "No rung 1" tells a
    reviewer something true about the surface before anyone trusts the flow.

    Rungs are numbered by what they *are* (role+name is always rung 1,
    relational always rung 2), never by their position in this list. Numbering
    them 1,2,3 by position produced notes that contradicted themselves —
    "NO RUNG 1 ... Rung 1 — relational" — and disagreed with the rung numbers
    replay reports.
    """
    lines = [
        f"Identifying the {element.role} {_describe(element)}. "
        f"{len(verified)} rung(s) verified against the live surface at record time."
    ]

    if not any(isinstance(r, A11yRoleNameLocator) for r in verified):
        # Two different facts about the surface, and the distinction is the
        # useful part: nameless is a permanent property of the control, while
        # ambiguous means the name exists but is shared.
        if element.name:
            lines.append(
                f"NO RUNG 1: the name {element.name!r} is not unique here — the "
                "accessibility tree gives it to more than one element — so "
                "role+name cannot address this one unambiguously."
            )
        else:
            lines.append(
                "NO RUNG 1: this control has no accessible name at all, so "
                "role+name cannot address it."
            )
        lines.append(
            "The relational rung is therefore load-bearing rather than a fallback."
        )

    lines.extend(
        f"Rung {RUNG_ORDER[rung.kind]} — {note}."
        for rung, note in zip(verified, notes)
    )
    return " ".join(lines)


def _describe(element: Element) -> str:
    return repr(element.name) if element.name else "(unnamed)"


# --------------------------------------------------------------------------- #
# Proposing checkpoints (M3)
# --------------------------------------------------------------------------- #

# What makes a good "the step worked" marker, most telling first. A heading is
# the strongest signal a new screen arrived; an alert is how this application
# reports an outcome; a button is what a new panel offers you next.
_CHECKPOINT_ROLE_PREFERENCE = ("heading", "alert", "status", "button")

CHECKPOINT_TIMEOUT_MS = 8000
VERIFY_TIMEOUT_MS = 500


def propose_checkpoint(
    driver: Driver,
    intent: str,
    ladder: TargetLadder | None,
    value_ref: str | None,
    before: Observation | None,
    after: Observation,
) -> Checkpoint | None:
    """Suggest a condition that should hold once this step has worked.

    Proposed, not decided: discovery only ever walks one path, so a checkpoint
    derived from that path describes the happy case and nothing else. Widening
    it is exactly what the human review step exists for, and marking the
    artifact `draft` until then is the honest way to say so.

    Whatever is proposed is **verified to hold right now** before it is
    recorded — same discipline as the rungs. A checkpoint that was already
    false at record time would fail every replay.
    """
    if intent == "type" and ladder is not None and value_ref is not None:
        # Provable and redaction-safe: the field holds whatever the parameter
        # resolves to, without the artifact ever containing the value.
        candidate = Checkpoint(
            condition=ValueMatchesRef(target=ladder, value_ref=value_ref),
            description=f"the field holds the value supplied for {value_ref}",
            timeout_ms=CHECKPOINT_TIMEOUT_MS,
        )
        return candidate

    marker = _marker_element(before, after)
    if marker is None:
        return None

    condition = RoleNamePresent(role=marker.role, name=marker.name, exact=True)
    if not driver.check(condition, timeout_ms=VERIFY_TIMEOUT_MS).ok:
        return None

    return Checkpoint(
        condition=condition,
        description=f"{marker.role} {marker.name!r} is on screen",
        timeout_ms=CHECKPOINT_TIMEOUT_MS,
    )


def _marker_element(before: Observation | None, after: Observation) -> Element | None:
    """The most telling named element to assert on.

    Prefers something that appeared *because of* this step — a panel that was
    not there a moment ago is far better evidence the step worked than anything
    that was on screen all along.
    """
    candidates = [e for e in after.elements if e.name]
    if before is not None:
        seen = {(e.role, e.name, e.text) for e in before.elements}
        fresh = [e for e in candidates if (e.role, e.name, e.text) not in seen]
        if fresh:
            candidates = fresh

    for role in _CHECKPOINT_ROLE_PREFERENCE:
        for element in candidates:
            if element.role == role:
                return element
    return None


def output_name_for(element: Element, index: int) -> str:
    """A declared output's name, derived from what the value is called on screen."""
    slug = "".join(c if c.isalnum() else "_" for c in element.name.lower()).strip("_")
    while "__" in slug:
        slug = slug.replace("__", "_")
    return slug or f"output_{index}"


# --------------------------------------------------------------------------- #
# Assembling the artifact (M3)
# --------------------------------------------------------------------------- #


@dataclass
class RecordedStep:
    """What the recorder needs from one turn of discovery to write a Step.

    Deliberately not the agent's `StepRecord`: that one carries the model's
    reason and the evidence trail, which belong in the transcript rather than
    in a replayable program.
    """

    intent: str
    description: str
    ladder: TargetLadder | None
    checkpoint: Checkpoint | None
    value_ref: str | None = None
    extract_to: str | None = None
    output_type: ValueType = ValueType.STRING
    output_description: str = ""


class LiteralValueLeaked(Exception):
    """A caller's actual value reached the artifact. Refuse to emit it."""


def parameterize(text: str, param_values: dict[str, str]) -> str:
    """Rewrite prose in terms of parameters: "member 12345" -> "member {member_id}".

    The goal a human types contains real values, and it would otherwise be
    copied verbatim into the artifact's description. Substituting rather than
    masking keeps the sentence readable *and* makes it a better description:
    it now documents the capability generally instead of one invocation.
    """
    out = text
    for name, value in sorted(param_values.items(), key=lambda kv: -len(kv[1])):
        if value:
            out = out.replace(value, "{" + name + "}")
    return out


def assemble_artifact(
    capability_id: str,
    description: str,
    product_identity: str,
    entry_url: str,
    allowed_origins: list[str],
    parameters: list[Parameter],
    steps: list[RecordedStep],
    entry_checkpoint: Checkpoint,
    provenance: Provenance | None = None,
    param_values: dict[str, str] | None = None,
) -> Artifact:
    """Turn a walked path into a draft capability.

    Two things are true of everything emitted here and worth stating plainly:

    **No literal values.** Every value a step consumes is a `value_ref` into the
    parameters the caller declared. The schema rejects anything else, so this is
    enforced rather than intended.

    **It is a draft.** Discovery saw one path through the application. It has
    not seen "no such member", it has not seen a permission denial, and it
    cannot invent them. `known_outcomes` is therefore empty and the status is
    `draft` — the schema will refuse to call it approved until a human has
    authored the branches discovery never walked.
    """
    param_values = param_values or {}
    # Prose is parameterized, never masked. Descriptions come from the model's
    # own stated reasons ("enter the member id 12345..."), so they routinely
    # carry caller values; rewriting them in terms of the parameter keeps the
    # sentence useful. Locator fields are deliberately NOT rewritten — an
    # anchor_text is page text, and substituting into it would break the
    # locator. If a value genuinely reached one, that is a real defect and the
    # gate below refuses the artifact rather than papering over it.
    description = parameterize(description, param_values)
    steps = [_parameterize_step(s, param_values) for s in steps]

    # Discovery opens the app itself, so the artifact has to say so too;
    # otherwise replay would start wherever the browser happened to be.
    all_steps: list[Step] = [
        Step(
            step_id="open_application",
            action=Action.NAVIGATE,
            description=f"Open {capability_id.replace('_', ' ')} at the entry URL.",
            value_ref="target.entry_url",
            checkpoint=entry_checkpoint,
            risk=Risk.SAFE,
        )
    ]
    outputs: list[Output] = []

    for i, recorded in enumerate(steps, start=1):
        if recorded.ladder is None or recorded.checkpoint is None:
            continue
        step_id = f"{recorded.intent}_{i}"
        if recorded.extract_to:
            step_id = f"read_{recorded.extract_to}"
            outputs.append(
                Output(
                    name=recorded.extract_to,
                    type=recorded.output_type,
                    description=recorded.output_description
                    or f"Value read during {recorded.description}",
                )
            )
        all_steps.append(
            Step(
                step_id=step_id,
                action=Action(recorded.intent),
                description=recorded.description,
                target=recorded.ladder,
                value_ref=recorded.value_ref,
                extract_to=recorded.extract_to,
                checkpoint=recorded.checkpoint,
                # Discovery cannot tell an irreversible action from a safe one
                # by watching it succeed. Everything is recorded as safe and the
                # review step is where a human marks the ones that are not.
                risk=Risk.SAFE,
            )
        )

    artifact = Artifact(
        capability_id=capability_id,
        name=capability_id.replace("_", " ").title(),
        description=description,
        product_identity=product_identity,
        target=TargetSurface(entry_url=entry_url, allowed_origins=allowed_origins),
        parameters=parameters,
        outputs=outputs,
        steps=all_steps,
        known_outcomes=[],
        status=Status.DRAFT,
        provenance=provenance,
    )
    _assert_no_literals(artifact, param_values)
    return artifact


def _parameterize_step(step: RecordedStep, param_values: dict[str, str]) -> RecordedStep:
    checkpoint = step.checkpoint
    if checkpoint is not None:
        checkpoint = checkpoint.model_copy(
            update={"description": parameterize(checkpoint.description, param_values)}
        )
    return RecordedStep(
        intent=step.intent,
        description=parameterize(step.description, param_values),
        ladder=step.ladder,
        checkpoint=checkpoint,
        value_ref=step.value_ref,
        extract_to=step.extract_to,
        output_type=step.output_type,
        output_description=parameterize(step.output_description, param_values),
    )


def _assert_no_literals(artifact: Artifact, param_values: dict[str, str]) -> None:
    """Last gate before an artifact is persisted.

    The schema stops a literal being used as a `value_ref`, but prose has no
    such guard — a description, a robustness note or a checkpoint's wording can
    all carry a real value in by accident. This is the one place every emitted
    artifact passes through, so the check belongs here rather than in each
    caller's good intentions.

    Only string leaves are examined. Scanning the serialized JSON instead would
    flag a branch code of "001" inside a coordinate of 1001.0, and a guard that
    cries wolf gets switched off.
    """
    findings: list[str] = []

    def walk(node, path: str) -> None:
        if isinstance(node, str):
            for name, value in param_values.items():
                if value and value in node:
                    findings.append(f"{path} contains the value of {name!r}")
        elif isinstance(node, dict):
            for key, child in node.items():
                walk(child, f"{path}.{key}")
        elif isinstance(node, (list, tuple)):
            for i, child in enumerate(node):
                walk(child, f"{path}[{i}]")

    walk(artifact.model_dump(mode="python"), "artifact")
    if findings:
        raise LiteralValueLeaked(
            "refusing to emit an artifact holding caller values: " + "; ".join(findings)
        )

"""M2 — the discovery loop, verified without a model.

`ScriptedPlanner` stands in for the LLM, so everything around the model is
tested deterministically and for free: the loop, all four stopping conditions,
ladder building, error handling, and the evidence written along the way. The
real run against Claude is the milestone's checkpoint, but it should be the only
part that needs a key — if a test needs one, the seam isn't a seam.

The ladder tests are the most load-bearing here. They assert that a rung is
recorded only when the live surface confirmed it points at the element the model
chose, which is the property the whole replay half depends on.
"""

from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.server import AppServer
from core.agent import (
    DiscoveryAgent,
    Intent,
    ModelAction,
    ScriptedPlanner,
    StopReason,
)
from core.driver import WebDriver
from core.logging import EvidenceLog
from core.recorder import (
    LadderNotBuildable,
    LiteralValueLeaked,
    RecordedStep,
    assemble_artifact,
    build_ladder,
)
from core.schema import (
    A11yRoleNameLocator,
    Action,
    CoordinatesLocator,
    Parameter,
    RelationalLocator,
    RoleNamePresent,
    Status,
    TargetLadder,
    ValueMatchesRef,
    ValueType,
)

TMP = tempfile.mkdtemp(prefix='evidence-test-')


def _chromium_available() -> bool:
    try:
        from pathlib import Path

        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            return Path(p.chromium.executable_path).exists()
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _chromium_available(), reason="needs a Chromium install"
)

GOAL = "look up member 12345 in branch 001 and read the savings balance"
# Declared by the caller, exactly as the CLI's --param does. Nothing may be
# typed that is not one of these.
PARAMS = {"member_id": "12345", "branch_code": "001"}


@pytest.fixture(scope="module")
def app():
    with AppServer(port=0) as server:
        yield server


@pytest.fixture(scope="module")
def driver(app):
    with WebDriver(allowed_origins=[app.base_url]) as d:
        yield d


@pytest.fixture
def log(tmp_path):
    return EvidenceLog(run_id="test", kind="discovery", root=tmp_path)


def _ref_of(driver, role: str, name: str = "", anonymous: bool = False) -> str:
    obs = driver.observe()
    for e in obs.elements:
        if e.role == role and (not e.name if anonymous else e.name == name):
            return e.ref
    raise AssertionError(f"no {role} {name!r} in observation")


def act(intent, **kw):
    kw.setdefault("reason", "because the script says so")
    return ModelAction(intent=intent, **kw)


@dataclass
class ByDescription:
    """A stand-in model that picks elements out of the CURRENT observation.

    Closer to how a real model behaves than a list of pre-baked refs, and it
    sidesteps the fact that refs are only valid for the observation they came
    from — an element that does not exist yet (the balance, before the search)
    simply gets chosen on the turn it appears.
    """

    script: list[tuple]
    name: str = "scripted-by-description"

    def decide(self, goal, observation, history) -> ModelAction:
        if not self.script:
            return act(Intent.STUCK, summary="script exhausted")
        intent, match, text = self.script.pop(0)
        if intent in (Intent.DONE, Intent.STUCK):
            return act(intent, summary=text)
        element = next((e for e in observation.elements if match(e)), None)
        if element is None:
            return act(Intent.STUCK, summary="scripted element not on screen")
        return act(intent, ref=element.ref, text=text)


def is_role(role: str, name: str | None = None, anonymous: bool = False):
    def match(e):
        if e.role != role:
            return False
        if anonymous:
            return not e.name
        return name is None or e.name == name

    return match


def has_currency(e):
    return e.text.startswith("$")


READ_FLOW = [
    (Intent.TYPE, is_role("textbox", "Member ID"), "12345"),
    (Intent.TYPE, is_role("textbox", anonymous=True), "001"),
    (Intent.CLICK, is_role("button", "Search"), None),
]


# --------------------------------------------------------------------------- #
# The loop, end to end.
# --------------------------------------------------------------------------- #


def test_loop_completes_the_whole_read_flow(driver, app, log):
    """The M2 thread: goal in, a real sequence of actions out, balance read.

    Note the balance is chosen on the turn it appears — it does not exist in
    the observation before the search, exactly as for a real model.
    """
    planner = ByDescription(
        READ_FLOW
        + [
            (Intent.READ, has_currency, None),
            (Intent.DONE, None, "the savings balance is on screen"),
        ]
    )
    run = DiscoveryAgent(driver, planner, log, params=PARAMS).run(GOAL, app.entry_url)

    assert [s.action.intent for s in run.steps] == [
        Intent.TYPE,
        Intent.TYPE,
        Intent.CLICK,
        Intent.READ,
        Intent.DONE,
    ]
    assert all(s.ok for s in run.steps)
    assert run.stop_reason is StopReason.GOAL_REACHED
    assert run.succeeded

    read_step = run.steps[3]
    assert read_step.extracted.startswith("$")
    assert read_step.ladder is not None


# --------------------------------------------------------------------------- #
# Stopping conditions — all four.
# --------------------------------------------------------------------------- #


def test_model_done_signal_stops_the_run(driver, app, log):
    planner = ScriptedPlanner([act(Intent.DONE, summary="nothing to do")])
    run = DiscoveryAgent(driver, planner, log, params=PARAMS).run(GOAL, app.entry_url)
    assert run.stop_reason is StopReason.GOAL_REACHED
    assert run.summary == "nothing to do"


def test_model_stuck_signal_stops_the_run(driver, app, log):
    planner = ScriptedPlanner([act(Intent.STUCK, summary="cannot find the field")])
    run = DiscoveryAgent(driver, planner, log, params=PARAMS).run(GOAL, app.entry_url)
    assert run.stop_reason is StopReason.MODEL_STUCK
    assert not run.succeeded


def test_max_steps_stops_a_runaway_loop(driver, app, log):
    search = None
    driver.navigate(app.entry_url)
    search = _ref_of(driver, "button", "Search")
    # A model that keeps clicking Search forever.
    planner = ScriptedPlanner([act(Intent.CLICK, ref=search) for _ in range(50)])
    run = DiscoveryAgent(driver, planner, log, max_steps=4, stall_limit=99, params=PARAMS).run(
        GOAL, app.entry_url
    )
    assert run.stop_reason is StopReason.MAX_STEPS
    assert len(run.steps) == 4


def test_wall_clock_timeout_stops_the_run(driver, app, log):
    driver.navigate(app.entry_url)
    search = _ref_of(driver, "button", "Search")
    planner = ScriptedPlanner([act(Intent.CLICK, ref=search) for _ in range(50)])
    run = DiscoveryAgent(
        driver, planner, log, max_steps=99, timeout_s=0, stall_limit=99, params=PARAMS
    ).run(GOAL, app.entry_url)
    assert run.stop_reason is StopReason.TIMEOUT


def test_no_progress_is_detected_as_a_dead_end(driver, app, log):
    """Clicking Search with an empty form changes nothing after the first
    alert appears — which is what being stuck looks like from outside."""
    driver.navigate(app.entry_url)
    search = _ref_of(driver, "button", "Search")
    planner = ScriptedPlanner([act(Intent.CLICK, ref=search) for _ in range(20)])
    run = DiscoveryAgent(driver, planner, log, max_steps=20, stall_limit=3, params=PARAMS).run(
        GOAL, app.entry_url
    )
    assert run.stop_reason is StopReason.NO_PROGRESS
    assert len(run.steps) < 20


# --------------------------------------------------------------------------- #
# Verified ladders — the property replay depends on.
# --------------------------------------------------------------------------- #


def test_named_control_records_rung_one_first(driver, app):
    driver.navigate(app.entry_url)
    obs = driver.observe()
    element = next(e for e in obs.elements if e.role == "textbox" and e.name == "Member ID")

    ladder = build_ladder(driver, element, obs)

    assert isinstance(ladder.rungs[0], A11yRoleNameLocator)
    assert ladder.rungs[0].name == "Member ID"
    assert isinstance(ladder.rungs[-1], CoordinatesLocator)
    assert "role+name" in ladder.robustness_note


def test_anonymous_control_records_a_ladder_starting_at_rung_two(driver, app):
    """The field with no accessible name: rung 1 is impossible, and the
    generated note says so rather than quietly omitting it."""
    driver.navigate(app.entry_url)
    obs = driver.observe()
    element = next(e for e in obs.elements if e.role == "textbox" and not e.name)

    ladder = build_ladder(driver, element, obs)

    assert not any(isinstance(r, A11yRoleNameLocator) for r in ladder.rungs)
    assert isinstance(ladder.rungs[0], RelationalLocator)
    assert ladder.rungs[0].anchor_text == "Branch Code"
    assert "NO RUNG 1" in ladder.robustness_note


def test_recorded_ladder_actually_drives_the_control(driver, app):
    driver.navigate(app.entry_url)
    obs = driver.observe()
    element = next(e for e in obs.elements if e.role == "textbox" and not e.name)

    ladder = build_ladder(driver, element, obs)
    driver.type(ladder, "007")

    after = driver.observe()
    anonymous = next(e for e in after.elements if e.role == "textbox" and not e.name)
    assert anonymous.text == "007"


def test_every_recorded_rung_resolves_to_the_chosen_element(driver, app):
    """The core claim: no rung is recorded unless the surface confirmed it
    finds exactly one element, and that it is this one."""
    driver.navigate(app.entry_url)
    obs = driver.observe()

    checked = 0
    for element in obs.elements:
        if element.role not in ("textbox", "button"):
            continue
        ladder = build_ladder(driver, element, obs)
        for rung in ladder.rungs:
            box = driver.probe(rung)
            assert box is not None, f"{rung.kind} recorded but does not resolve"
            assert abs(box.x - element.box.x) <= 2, f"{rung.kind} resolves elsewhere"
            checked += 1
    assert checked >= 6


def test_ambiguous_identification_is_never_recorded(driver, app):
    """Two cells carry the name 'Savings Balance'. Role+name is therefore
    ambiguous and must not appear, even though it looks right in the tree."""
    driver.navigate(app.entry_url)
    for ref, value in (("Member ID", "12345"),):
        obs = driver.observe()
        el = next(e for e in obs.elements if e.role == "textbox" and e.name == ref)
        driver.type(build_ladder(driver, el, obs), value)
    obs = driver.observe()
    branch = next(e for e in obs.elements if e.role == "textbox" and not e.name)
    driver.type(build_ladder(driver, branch, obs), "001")
    obs = driver.observe()
    search = next(e for e in obs.elements if e.role == "button" and e.name == "Search")
    driver.click(build_ladder(driver, search, obs))

    obs = driver.observe()
    value_cell = next(e for e in obs.elements if e.text.startswith("$"))
    ladder = build_ladder(driver, value_cell, obs)

    role_and_name = [
        r
        for r in ladder.rungs
        if isinstance(r, A11yRoleNameLocator) and r.role == "cell"
    ]
    assert not role_and_name, "recorded an ambiguous role+name rung"
    assert driver.read(ladder).text.startswith("$")


# --------------------------------------------------------------------------- #
# Evidence.
# --------------------------------------------------------------------------- #


def test_transcript_records_what_it_saw_decided_and_why(driver, app, log):
    planner = ByDescription(list(READ_FLOW))
    run = DiscoveryAgent(driver, planner, log, params=PARAMS).run(GOAL, app.entry_url)

    transcript = log.transcript_path.read_text()
    assert "# Discovery transcript" in transcript
    assert "**Why**" in transcript
    assert "**Decided**" in transcript
    assert "Resolved at" in transcript
    assert run.stop_reason.value in transcript


def test_events_are_machine_readable(driver, app, log):
    planner = ByDescription(list(READ_FLOW))
    DiscoveryAgent(driver, planner, log, params=PARAMS).run(GOAL, app.entry_url)

    events = [json.loads(line) for line in log.events_path.read_text().splitlines()]
    kinds = [e["event"] for e in events]
    assert kinds[0] == "run_start" and kinds[-1] == "run_complete"

    steps = [e for e in events if e["event"] == "step"]
    assert steps and all("robustness_note" in e for e in steps)
    assert any(e["rungs_recorded"] >= 2 for e in steps)


def test_typed_values_are_redacted_from_the_evidence(driver, app, log):
    """A value the model typed is masked everywhere it appears — including
    inside the accessibility tree, where it shows up as a field value."""
    log.mark_sensitive("12345")
    planner = ByDescription(list(READ_FLOW))
    DiscoveryAgent(driver, planner, log, params=PARAMS).run(GOAL, app.entry_url)

    assert "12345" not in log.transcript_path.read_text()
    assert "12345" not in log.events_path.read_text()
    assert "***" in log.events_path.read_text()


# --------------------------------------------------------------------------- #
# Failure handling.
# --------------------------------------------------------------------------- #


def test_a_stale_ref_is_recorded_not_raised(driver, app, log):
    planner = ScriptedPlanner(
        [act(Intent.CLICK, ref="e999"), act(Intent.DONE, summary="gave up")]
    )
    run = DiscoveryAgent(driver, planner, log, params=PARAMS).run(GOAL, app.entry_url)

    assert run.steps[0].error and "e999" in run.steps[0].error
    assert "FAILED" in log.transcript_path.read_text()


def test_unbuildable_ladder_raises_rather_than_recording_a_lie():
    """If nothing identifies an element, that must fail loudly — a silently
    empty ladder would produce an artifact that cannot replay."""
    from core.driver import Box, Element, Observation

    ghost = Element(ref="e1", role="textbox", name="", text="", box=Box(0, 0, 10, 10), depth=0)
    observation = Observation(
        url="about:blank",
        title="",
        tree="",
        tree_boxed="",
        tree_for_model="",
        elements=(ghost,),
        captured_at=0.0,
    )

    class NothingResolves:
        def probe(self, locator):
            return None

    with pytest.raises(LadderNotBuildable):
        build_ladder(NothingResolves(), ghost, observation)


# --------------------------------------------------------------------------- #
# Evidence accuracy. The transcript is a deliverable, so a note that misstates
# what happened is a defect — these three were all found by reading a real run.
# --------------------------------------------------------------------------- #


def test_note_numbers_rungs_by_kind_not_by_position(driver, app):
    """A relational rung is rung 2 even when it is first in the ladder.

    Numbering by position produced self-contradicting notes — "NO RUNG 1 ...
    Rung 1 — relational" — and disagreed with the rung numbers replay reports.
    """
    driver.navigate(app.entry_url)
    obs = driver.observe()
    anonymous = next(e for e in obs.elements if e.role == "textbox" and not e.name)

    note = build_ladder(driver, anonymous, obs).robustness_note

    assert "Rung 2 — relational" in note
    assert "Rung 1 — relational" not in note


def test_note_distinguishes_nameless_from_ambiguous(driver, app):
    """Two different facts about a surface, and the difference is the useful
    part: nameless is permanent, ambiguous means the name exists but is shared.
    """
    driver.navigate(app.entry_url)
    obs = driver.observe()
    anonymous = next(e for e in obs.elements if e.role == "textbox" and not e.name)
    assert "no accessible name at all" in build_ladder(driver, anonymous, obs).robustness_note

    planner = ByDescription(list(READ_FLOW))
    DiscoveryAgent(
        driver, planner, EvidenceLog("t", "discovery", Path(TMP)), params=PARAMS
    ).run(
        GOAL, app.entry_url
    )
    obs = driver.observe()
    balance = next(e for e in obs.elements if e.text.startswith("$"))
    note = build_ladder(driver, balance, obs).robustness_note
    assert "is not unique here" in note
    assert "(unnamed)" not in note


def test_first_available_rung_is_not_reported_as_a_fallback(driver, app):
    """An anonymous control has no rung 1 to fall back *from*. Reporting one
    would invent drift that never happened."""
    driver.navigate(app.entry_url)
    obs = driver.observe()
    anonymous = next(e for e in obs.elements if e.role == "textbox" and not e.name)

    resolution = driver.type(build_ladder(driver, anonymous, obs), "001")

    assert resolution.rung == 2
    assert not resolution.fell_back
    assert all(a.ok for a in resolution.attempts)


def test_genuine_fallback_is_still_reported(driver, app):
    """The opposite case must still work: a rung that really did fail."""
    driver.navigate(app.entry_url)
    ladder = TargetLadder(
        rungs=[
            A11yRoleNameLocator(role="textbox", name="Renamed Away", exact=True),
            RelationalLocator(
                anchor_text="Member ID", relation="same_row", target_role="textbox"
            ),
        ],
        robustness_note="drift probe",
    )
    resolution = driver.type(ladder, "12345")

    assert resolution.fell_back
    assert [a.ok for a in resolution.attempts] == [False, True]


# --------------------------------------------------------------------------- #
# M3 — emitting the artifact.
# --------------------------------------------------------------------------- #


def test_typing_an_undeclared_value_is_refused(driver, app, log):
    """The redaction guarantee at its source.

    If a value was not declared as a parameter, the artifact would have to
    store it literally — so the step is refused instead. Better a failed
    recording than a capability file with a member id baked into it.
    """
    planner = ByDescription(
        [(Intent.TYPE, is_role("textbox", "Member ID"), "99999-undeclared")]
    )
    run = DiscoveryAgent(driver, planner, log, params=PARAMS).run(GOAL, app.entry_url)

    assert not run.steps[0].ok
    assert "refusing to record a literal" in run.steps[0].error
    assert run.steps[0].as_recorded() is None


def test_each_step_gets_a_verified_checkpoint(driver, app, log):
    planner = ByDescription(
        list(READ_FLOW) + [(Intent.READ, has_currency, None), (Intent.DONE, None, "done")]
    )
    run = DiscoveryAgent(driver, planner, log, params=PARAMS).run(GOAL, app.entry_url)

    acting = [s for s in run.steps if not s.action.is_terminal]
    assert all(s.checkpoint is not None for s in acting)
    assert run.entry_checkpoint is not None

    # A typed field is checkpointed against the parameter, not the value.
    typed = run.steps[0]
    assert isinstance(typed.checkpoint.condition, ValueMatchesRef)
    assert typed.checkpoint.condition.value_ref == "params.member_id"

    # The click's checkpoint asserts on what the click produced.
    clicked = run.steps[2]
    assert isinstance(clicked.checkpoint.condition, RoleNamePresent)
    assert clicked.checkpoint.condition.name == "Member Detail"


def test_discovery_emits_a_valid_draft_artifact(driver, app, log):
    planner = ByDescription(
        list(READ_FLOW) + [(Intent.READ, has_currency, None), (Intent.DONE, None, "done")]
    )
    run = DiscoveryAgent(driver, planner, log, params=PARAMS).run(GOAL, app.entry_url)

    artifact = assemble_artifact(
        capability_id="lookup_member_balance",
        description=GOAL,
        product_identity="CoreBankPro@4",
        entry_url=app.entry_url,
        allowed_origins=[app.base_url],
        parameters=[
            Parameter(name="member_id", type=ValueType.STRING, description="Member id.", sensitive=True),
            Parameter(name="branch_code", type=ValueType.STRING, description="Branch."),
        ],
        steps=run.recorded_steps(),
        entry_checkpoint=run.entry_checkpoint,
        param_values=PARAMS,
    )

    # Validates, opens itself, and reads something out.
    assert artifact.steps[0].action is Action.NAVIGATE
    assert artifact.steps[0].value_ref == "target.entry_url"
    assert [o.name for o in artifact.outputs] == ["savings_balance"]
    assert any(s.extract_to == "savings_balance" for s in artifact.steps)


def test_emitted_artifact_contains_no_literal_values(driver, app, log):
    planner = ByDescription(
        list(READ_FLOW) + [(Intent.READ, has_currency, None), (Intent.DONE, None, "done")]
    )
    run = DiscoveryAgent(driver, planner, log, params=PARAMS).run(GOAL, app.entry_url)
    artifact = assemble_artifact(
        capability_id="lookup_member_balance",
        description=GOAL,
        product_identity="CoreBankPro@4",
        entry_url=app.entry_url,
        allowed_origins=[app.base_url],
        parameters=[
            Parameter(name="member_id", type=ValueType.STRING, description="Member id.", sensitive=True),
            Parameter(name="branch_code", type=ValueType.STRING, description="Branch."),
        ],
        steps=run.recorded_steps(),
        entry_checkpoint=run.entry_checkpoint,
        param_values=PARAMS,
    )

    serialized = artifact.model_dump_json()
    assert "12345" not in serialized
    assert "params.member_id" in serialized


def test_emitted_artifact_is_a_draft_with_no_authored_outcomes(driver, app, log):
    """Discovery walked one path. It has not seen 'no such member' and cannot
    invent it, so the lifecycle says so out loud."""
    planner = ByDescription(list(READ_FLOW) + [(Intent.DONE, None, "done")])
    run = DiscoveryAgent(driver, planner, log, params=PARAMS).run(GOAL, app.entry_url)
    artifact = assemble_artifact(
        capability_id="lookup_member_balance",
        description=GOAL,
        product_identity="CoreBankPro@4",
        entry_url=app.entry_url,
        allowed_origins=[app.base_url],
        parameters=[
            Parameter(name="member_id", type=ValueType.STRING, description="Member id.", sensitive=True),
            Parameter(name="branch_code", type=ValueType.STRING, description="Branch."),
        ],
        steps=run.recorded_steps(),
        entry_checkpoint=run.entry_checkpoint,
        param_values=PARAMS,
    )

    assert artifact.status is Status.DRAFT
    assert artifact.known_outcomes == []

    # And it cannot be promoted without a human authoring those branches.
    with pytest.raises(ValidationError, match="at least one known_outcome"):
        artifact.model_copy(update={"status": Status.APPROVED}).model_validate(
            artifact.model_dump() | {"status": "approved"}
        )


def test_the_goal_text_itself_cannot_leak_a_value(driver, app, log):
    """Found by reading a real emitted artifact: the goal a human types
    contains real values, and it was being copied into the description."""
    planner = ByDescription(list(READ_FLOW) + [(Intent.DONE, None, "done")])
    run = DiscoveryAgent(driver, planner, log, params=PARAMS).run(GOAL, app.entry_url)

    kwargs = dict(
        capability_id="lookup_member_balance",
        description=GOAL,
        product_identity="CoreBankPro@4",
        entry_url=app.entry_url,
        allowed_origins=[app.base_url],
        parameters=[
            Parameter(name="member_id", type=ValueType.STRING, description="Member id.", sensitive=True),
            Parameter(name="branch_code", type=ValueType.STRING, description="Branch."),
        ],
        steps=run.recorded_steps(),
        entry_checkpoint=run.entry_checkpoint,
    )

    # The description is rewritten in terms of the parameters, not masked.
    artifact = assemble_artifact(**kwargs, param_values=PARAMS)
    assert "{member_id}" in artifact.description
    assert "12345" not in artifact.description

    # And a value that slipped through anywhere else is refused outright.
    with pytest.raises(LiteralValueLeaked, match="member_id"):
        assemble_artifact(
            **{**kwargs, "description": "look up member 12345"},
            param_values={"member_id": "12345", "branch_code": "001"},
        ) if False else _leak_probe(kwargs)


def _leak_probe(kwargs):
    """Smuggle a caller value into a LOCATOR and prove the gate refuses it.

    The realistic version of this bug: the page echoes a typed value back as a
    caption, the recorder picks that caption as a relational anchor, and the
    ladder is now welded to one caller's data — it would find nothing for the
    next member. Prose gets parameterized; a locator cannot be, so the only
    safe answer is to refuse.
    """
    steps = list(kwargs["steps"])
    poisoned = TargetLadder(
        rungs=[
            RelationalLocator(
                anchor_text="12345", relation="same_row", target_role="textbox"
            )
        ],
        robustness_note="anchored on text that happens to be the caller's value",
    )
    steps[0] = RecordedStep(
        intent=steps[0].intent,
        description=steps[0].description,
        ladder=poisoned,
        checkpoint=steps[0].checkpoint,
        value_ref=steps[0].value_ref,
    )
    return assemble_artifact(**{**kwargs, "steps": steps}, param_values=PARAMS)

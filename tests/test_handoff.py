"""M6 — escalation and handoff over CDP.

The headline test is `test_a_human_unblocks_a_stuck_run_in_the_same_session`:
replay hits something it cannot resolve, a second controller attaches to the
*same live browser* and clears it, and the run resumes and finishes. Nothing
about the transfer is faked — `ScriptedOperator` really does open its own CDP
connection to the same debug port while the automation is detached. Only the
person is simulated.

The scenario is the app's `?blocker=1` supervisor-override screen: deliberately
not an alert, so the artifact's checkpoint genuinely fails, no known outcome
describes it and no recovery clears it. That is the honest shape of "stuck".
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.server import AppServer
from core.driver import BrowserSession, WebDriver, _playwright
from core.handoff import (
    ControlLedger,
    InterventionRequest,
    OperatorResponse,
    Ownership,
    ScriptedOperator,
    describe_change,
)
from core.logging import EvidenceLog
from core.replay import HardFailure, Refused, ReplayEngine, Success
from core.safety import Allowlist
from core.schema import load_artifact

ARTIFACT = Path(__file__).parent.parent / "artifacts" / "lookup_member_balance.json"


def _chromium_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            return Path(p.chromium.executable_path).exists()
    except Exception:
        return False


browser_required = pytest.mark.skipif(
    not _chromium_available(), reason="needs a Chromium install"
)


# --------------------------------------------------------------------------- #
# The ownership ledger — no browser needed.
# --------------------------------------------------------------------------- #


def test_exactly_one_party_holds_the_session():
    ledger = ControlLedger()
    assert ledger.owner is Ownership.AUTOMATION

    ledger.transfer_to(Ownership.HUMAN, "hard failure at click_3")
    assert ledger.owner is Ownership.HUMAN

    # Handing to the current holder is a caller bug, not a quiet no-op.
    with pytest.raises(ValueError, match="already holds"):
        ledger.transfer_to(Ownership.HUMAN, "again")

    ledger.transfer_to(Ownership.AUTOMATION, "operator handed back")
    assert ledger.history == [
        "automation -> human (hard failure at click_3)",
        "human -> automation (operator handed back)",
    ]


def test_the_intervention_request_is_self_contained():
    """Someone paged at 2am should not have to read the source to act."""
    request = InterventionRequest(
        capability_id="lookup_member_balance",
        step_id="click_3",
        what_it_was_doing="Submit the search",
        expected="a member panel or an alert",
        observed="Supervisor Override Required",
        cdp_url="http://127.0.0.1:9222",
        page_url="http://127.0.0.1:8000/members.html",
        screenshot="evidence/x/failure.png",
    )
    brief = request.as_brief()

    for needed in ("click_3", "expected", "observed", "9222", "failure.png"):
        assert needed in brief
    assert json.loads(request.to_json())["step_id"] == "click_3"


# --------------------------------------------------------------------------- #
# Detach and reattach: the browser outlives the controller.
# --------------------------------------------------------------------------- #


@browser_required
def test_state_survives_a_detach_and_a_second_controller_sees_it():
    """The mechanism the whole handoff rests on. Two clients, one session."""
    with AppServer(port=0) as app:
        session = BrowserSession()
        session.start(_playwright().chromium.executable_path)
        try:
            automation = WebDriver(session=session, allowlist=Allowlist(origins=(app.base_url,)))
            automation._owns_session = False
            automation.start()
            automation.navigate(app.entry_url)
            automation.page.get_by_role("textbox", name="Member ID").fill("12345")
            automation.detach()
            assert not automation.attached

            # A different controller entirely, attaching to the live browser.
            human = WebDriver(session=session)
            human._owns_session = False
            human.reattach()
            assert human.page.get_by_role("textbox", name="Member ID").input_value() == "12345"
            human.page.get_by_role("textbox").nth(1).fill("001")
            human.detach()

            # And automation takes it back, seeing what the human did.
            automation.reattach()
            assert automation.page.get_by_role("textbox").nth(1).input_value() == "001"
            automation.detach()
        finally:
            session.stop()


@browser_required
def test_describe_change_records_what_the_human_did():
    with AppServer(port=0) as app, WebDriver(allowlist=Allowlist(origins=(app.base_url,))) as d:
        d.navigate(app.entry_url)
        before = d.observe()
        d.page.get_by_role("textbox", name="Member ID").fill("12345")
        d.page.get_by_role("textbox").nth(1).fill("001")
        d.page.get_by_role("button", name="Search").click()
        after = d.observe()

    changes = describe_change(before, after)
    assert any("Member Detail" in c for c in changes)
    assert all(c.startswith(("appeared:", "gone:", "navigated:")) for c in changes)


# --------------------------------------------------------------------------- #
# The checkpoint: stuck -> human -> resumed.
# --------------------------------------------------------------------------- #


@pytest.fixture
def stuck_setup(tmp_path):
    """A run that genuinely cannot continue: the override screen is not an
    alert, so the checkpoint fails, and nothing in the artifact describes it."""
    with AppServer(port=0) as app:
        session = BrowserSession()
        session.start(_playwright().chromium.executable_path)
        driver = WebDriver(session=session, allowlist=Allowlist(origins=(app.base_url,)))
        driver._owns_session = False
        driver.start()
        try:
            yield app, driver, session, EvidenceLog(
                run_id="handoff-test", kind="replay", root=tmp_path
            )
        finally:
            driver.detach()
            session.stop()


@browser_required
def test_without_an_operator_it_simply_stops(stuck_setup):
    app, driver, _, log = stuck_setup
    report = ReplayEngine(
        driver=driver,
        artifact=load_artifact(ARTIFACT),
        log=log,
        entry_url=app.entry_url + "?blocker=1",
    ).run(member_id="12345", branch_code="001")

    assert isinstance(report, HardFailure)
    assert report.needs_a_human
    assert report.step_id == "click_3"


@browser_required
def test_a_human_unblocks_a_stuck_run_in_the_same_session(stuck_setup):
    """The M6 checkpoint, end to end."""
    app, driver, session, log = stuck_setup
    operator = ScriptedOperator(session_cdp_url=session.cdp_url, clicks=["Override"])

    engine = ReplayEngine(
        driver=driver,
        artifact=load_artifact(ARTIFACT),
        log=log,
        entry_url=app.entry_url + "?blocker=1",
        operator=operator,
    )
    report = engine.run(member_id="12345", branch_code="001")

    # It finished the job it was sent to do.
    assert isinstance(report, Success)
    assert report.outputs["savings_balance"].startswith("$")

    # A person was genuinely involved, and control came back.
    assert operator.seen == ["click_3"]
    assert engine.ledger.owner is Ownership.AUTOMATION
    assert engine.ledger.history == [
        "automation -> human (hard failure at click_3)",
        "human -> automation (operator handed back)",
    ]


@browser_required
def test_the_whole_episode_is_in_the_evidence(stuck_setup):
    app, driver, session, log = stuck_setup
    ReplayEngine(
        driver=driver,
        artifact=load_artifact(ARTIFACT),
        log=log,
        entry_url=app.entry_url + "?blocker=1",
        operator=ScriptedOperator(session_cdp_url=session.cdp_url, clicks=["Override"]),
    ).run(member_id="12345", branch_code="001")

    transcript = log.transcript_path.read_text()
    assert "Human intervention" in transcript
    assert "automation -> human" in transcript
    assert "appeared:" in transcript  # what the human actually changed

    request = json.loads((log.dir / "intervention-click_3.json").read_text())
    assert request["step_id"] == "click_3"
    assert request["cdp_url"].startswith("http://127.0.0.1:")

    events = [json.loads(line) for line in log.events_path.read_text().splitlines()]
    assert [e for e in events if e["event"] == "handoff"]
    assert [e for e in events if e["event"] == "intervention"]


@browser_required
def test_an_operator_who_declines_leaves_the_failure_standing(stuck_setup):
    app, driver, _, log = stuck_setup

    class Declines:
        name = "declines"

        def take_control(self, request) -> OperatorResponse:
            return OperatorResponse.abandoned("not my system")

    report = ReplayEngine(
        driver=driver,
        artifact=load_artifact(ARTIFACT),
        log=log,
        entry_url=app.entry_url + "?blocker=1",
        operator=Declines(),
    ).run(member_id="12345", branch_code="001")

    assert isinstance(report, HardFailure)
    assert "Resumed" in log.transcript_path.read_text()


@browser_required
def test_escalation_is_bounded(stuck_setup):
    """An operator who cannot fix it on the second attempt will not fix it on
    the twentieth, and a loop that keeps paging someone is its own outage."""
    app, driver, session, log = stuck_setup
    useless = ScriptedOperator(session_cdp_url=session.cdp_url, clicks=[])

    report = ReplayEngine(
        driver=driver,
        artifact=load_artifact(ARTIFACT),
        log=log,
        entry_url=app.entry_url + "?blocker=1",
        operator=useless,
        max_interventions=2,
    ).run(member_id="12345", branch_code="001")

    assert isinstance(report, HardFailure)
    assert len(useless.seen) == 2


@browser_required
def test_a_step_the_human_completed_is_not_done_twice(stuck_setup):
    """Re-observation, not memory. If the operator finished the blocked step,
    repeating it could mean submitting the same thing a second time."""
    app, driver, session, log = stuck_setup
    # The operator clears the override AND the search completes as a result,
    # so the blocked step's checkpoint already holds when control returns.
    operator = ScriptedOperator(session_cdp_url=session.cdp_url, clicks=["Override"])

    engine = ReplayEngine(
        driver=driver,
        artifact=load_artifact(ARTIFACT),
        log=log,
        entry_url=app.entry_url + "?blocker=1",
        operator=operator,
    )
    report = engine.run(member_id="12345", branch_code="001")

    assert isinstance(report, Success)
    # click_3 appears once as the failure; the run then moved on rather than
    # clicking Search again.
    clicks = [s for s in report.steps if s.step_id == "click_3"]
    assert len(clicks) == 1


# --------------------------------------------------------------------------- #
# The path that must stay closed.
# --------------------------------------------------------------------------- #


@browser_required
def test_a_policy_refusal_never_reaches_a_human(stuck_setup):
    """Escalating a refusal would mean asking a person to do by hand the thing
    the allowlist just prevented. The operator must never be called."""
    app, driver, session, log = stuck_setup
    driver.allowlist = Allowlist(origins=(app.base_url,), routes=("/nothing.html",))
    operator = ScriptedOperator(session_cdp_url=session.cdp_url, clicks=["Override"])

    report = ReplayEngine(
        driver=driver,
        artifact=load_artifact(ARTIFACT),
        log=log,
        entry_url=app.entry_url,
        operator=operator,
    ).run(member_id="12345", branch_code="001")

    assert isinstance(report, Refused)
    assert not report.needs_a_human
    assert operator.seen == [], "a refusal was escalated to a human"


# --------------------------------------------------------------------------- #
# Session expiry — the condition the brief names twice, and the one that
# escalates for a reason other than "an unknown screen".
# --------------------------------------------------------------------------- #


@browser_required
def test_an_expired_session_escalates_rather_than_self_healing(stuck_setup):
    """A one-click "resume" would be a recovery the automation could perform
    itself. Re-authentication is not, because the automation must never handle
    credentials — which is exactly why control has to transfer to a person."""
    app, driver, session, log = stuck_setup
    operator = ScriptedOperator(
        session_cdp_url=session.cdp_url,
        fills=[("Operator ID", "op-7741"), ("Password", "correct-horse-battery")],
        clicks=["Sign In"],
    )

    engine = ReplayEngine(
        driver=driver,
        artifact=load_artifact(ARTIFACT),
        log=log,
        entry_url=app.entry_url + "?session_expires=1",
        operator=operator,
    )
    report = engine.run(member_id="12345", branch_code="001")

    assert isinstance(report, Success)
    assert report.outputs["savings_balance"].startswith("$")
    assert operator.seen == ["click_3"]
    assert engine.ledger.owner is Ownership.AUTOMATION


@browser_required
def test_the_operators_credentials_never_reach_the_evidence(stuck_setup):
    """The handoff records WHAT the person did, not what they typed. A
    transcript that captured a password would be a worse leak than the one the
    escalation existed to avoid."""
    app, driver, session, log = stuck_setup
    secret = "correct-horse-battery"
    ReplayEngine(
        driver=driver,
        artifact=load_artifact(ARTIFACT),
        log=log,
        entry_url=app.entry_url + "?session_expires=1",
        operator=ScriptedOperator(
            session_cdp_url=session.cdp_url,
            fills=[("Operator ID", "op-7741"), ("Password", secret)],
            clicks=["Sign In"],
        ),
    ).run(member_id="12345", branch_code="001")

    transcript = log.transcript_path.read_text()
    events = log.events_path.read_text()

    assert secret not in transcript and secret not in events
    assert "op-7741" not in transcript and "op-7741" not in events
    # What they did is still recorded.
    assert "filled Password" in transcript
    assert "Sign In" in transcript


@browser_required
def test_the_capability_has_no_step_that_could_type_a_credential(stuck_setup):
    """Structural, not behavioural: there is no step in either artifact
    targeting a credential field, so the automation could not enter one even
    if the flow reached that screen."""
    for name in ("lookup_member_balance", "open_sub_account"):
        artifact = load_artifact(ARTIFACT.parent / f"{name}.json")
        for step in artifact.steps:
            if step.target is None:
                continue
            described = step.target.robustness_note.lower()
            for rung in step.target.rungs:
                described += " " + str(getattr(rung, "name", "") or "")
                described += " " + str(getattr(rung, "anchor_text", "") or "")
            assert "password" not in described, f"{name}.{step.step_id}"
            assert "operator id" not in described, f"{name}.{step.step_id}"

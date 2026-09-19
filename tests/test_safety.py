"""M5 — safety: allowlist, irreversible actions, redaction.

The theme is *placement*. Each mechanism is tested not just for working, but
for being somewhere it cannot be bypassed: the allowlist inside the Driver, the
confirmation gate inside the executor, redaction inside the one logger every
line passes through.

The write-flow tests use the second discovered capability, `open_sub_account`,
because a safety layer guarding a hypothetical irreversible action proves
nothing.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.server import AppServer
from core.driver import WebDriver
from core.logging import EvidenceLog
from core.replay import BusinessOutcome, HardFailure, Refused, ReplayEngine, Success
from core.safety import ALL_ACTIONS, ActionNotAllowed, Allowlist
from core.schema import (
    A11yRoleNameLocator,
    Artifact,
    Classification,
    Risk,
    TargetLadder,
    TargetSurface,
    load_artifact,
)

ARTIFACTS = Path(__file__).parent.parent / "artifacts"


def _chromium_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            return Path(p.chromium.executable_path).exists()
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# The policy object itself — no browser needed.
# --------------------------------------------------------------------------- #


def test_origin_outside_the_list_is_refused():
    policy = Allowlist(origins=("http://127.0.0.1:8000",))
    policy.check_navigation("http://127.0.0.1:8000/members.html")
    with pytest.raises(ActionNotAllowed, match="permitted origins"):
        policy.check_navigation("https://example.com/")


def test_route_outside_the_list_is_refused_on_an_allowed_origin():
    """The realistic hazard. A bank's admin console shares an origin with its
    member search, so 'which site' is not a fine enough unit of policy."""
    policy = Allowlist(origins=("http://127.0.0.1:8000",), routes=("/members.html",))

    policy.check_navigation("http://127.0.0.1:8000/members.html?drift=1")
    with pytest.raises(ActionNotAllowed, match="outside the permitted routes"):
        policy.check_navigation("http://127.0.0.1:8000/admin.html")


def test_a_read_only_policy_forbids_typing():
    policy = Allowlist.read_only(["http://127.0.0.1:8000"])
    policy.check_action("read")
    with pytest.raises(ActionNotAllowed, match="may only"):
        policy.check_action("type")


def test_policy_is_read_off_the_capability(tmp_path):
    target = TargetSurface(
        entry_url="http://x/members.html",
        allowed_origins=["http://x"],
        allowed_routes=["/members.html"],
        allowed_actions=["navigate", "read"],
    )
    policy = Allowlist.from_target(target)
    assert policy.actions == {"navigate", "read"}
    with pytest.raises(ActionNotAllowed):
        policy.check_action("click")


def test_empty_allowed_actions_means_all():
    target = TargetSurface(entry_url="http://x/a", allowed_origins=["http://x"])
    assert Allowlist.from_target(target).actions == ALL_ACTIONS


# --------------------------------------------------------------------------- #
# Enforcement placement.
# --------------------------------------------------------------------------- #

pytestmark_browser = pytest.mark.skipif(
    not _chromium_available(), reason="needs a Chromium install"
)


@pytest.fixture(scope="module")
def app():
    with AppServer(port=0) as server:
        yield server


@pytest.fixture
def log(tmp_path):
    return EvidenceLog(run_id="safety-test", kind="replay", root=tmp_path)


@pytestmark_browser
def test_the_driver_refuses_even_when_the_caller_insists(app):
    """Enforcement is inside the seam, so there is no route around it: the
    caller here is doing everything right except being allowed to."""
    policy = Allowlist(origins=(app.base_url,), routes=("/members.html",))
    with WebDriver(allowlist=policy) as driver:
        driver.navigate(app.entry_url)
        with pytest.raises(ActionNotAllowed):
            driver.navigate(f"{app.base_url}/admin.html")
        with pytest.raises(ActionNotAllowed):
            driver.navigate("https://example.com/")


@pytestmark_browser
def test_a_read_only_driver_cannot_type_or_click(app):
    with WebDriver(allowlist=Allowlist.read_only([app.base_url])) as driver:
        driver.navigate(app.entry_url)
        ladder = TargetLadder(
            rungs=[A11yRoleNameLocator(role="textbox", name="Member ID", exact=True)],
            robustness_note="policy test",
        )
        # Reading is fine.
        driver.read(ladder)
        with pytest.raises(ActionNotAllowed, match="'type'"):
            driver.type(ladder, "12345")
        with pytest.raises(ActionNotAllowed, match="'click'"):
            driver.click(ladder)


# --------------------------------------------------------------------------- #
# A refusal is its own category.
# --------------------------------------------------------------------------- #


@pytestmark_browser
def test_a_policy_refusal_is_not_a_hard_failure(app, log):
    """The distinction that matters here: a hard failure is the doorway to
    human escalation, so filing a refusal as one would end up asking a person
    to do by hand the very thing the allowlist prevented."""
    artifact = load_artifact(ARTIFACTS / "lookup_member_balance.json")
    policy = Allowlist(origins=(app.base_url,), routes=("/nothing-here.html",))

    with WebDriver(allowlist=policy) as driver:
        report = ReplayEngine(
            driver=driver, artifact=artifact, log=log, entry_url=app.entry_url
        ).run(member_id="12345", branch_code="001")

    assert isinstance(report, Refused)
    assert not isinstance(report, HardFailure)
    assert report.classification is Classification.REFUSED
    assert not report.ok
    assert not report.needs_a_human  # never escalate a refusal
    assert "routes" in report.reason and report.policy


def test_an_artifact_cannot_declare_its_own_refusals():
    """A capability does not get to authorise itself; refusal is the
    operator's decision, made at replay time."""
    raw = {
        "name": "sneaky",
        "detect": {"kind": "text_present", "text": "x"},
        "classification": "refused",
    }
    with pytest.raises(ValidationError, match="not declared by"):
        Artifact.model_validate(
            {
                **_minimal_artifact(),
                "known_outcomes": [raw],
                "status": "draft",
            }
        )


def _minimal_artifact() -> dict:
    return {
        "capability_id": "x",
        "name": "x",
        "description": "x",
        "product_identity": "x@1",
        "target": {"entry_url": "http://x/a", "allowed_origins": ["http://x"]},
        "steps": [
            {
                "step_id": "s1",
                "action": "navigate",
                "description": "go",
                "value_ref": "target.entry_url",
                "checkpoint": {
                    "condition": {"kind": "text_present", "text": "hi"},
                    "description": "loaded",
                },
            }
        ],
    }


# --------------------------------------------------------------------------- #
# Irreversible actions — the write flow.
# --------------------------------------------------------------------------- #


@pytest.fixture
def write_flow():
    path = ARTIFACTS / "open_sub_account.json"
    if not path.exists():
        pytest.skip("open_sub_account has not been discovered yet")
    return load_artifact(path)


def test_the_reviewer_marked_the_commitment_and_not_the_navigation(write_flow):
    """Discovery recorded everything as safe. A human decided which step
    actually commits — opening the form does not, confirming does."""
    by_id = {s.step_id: s for s in write_flow.steps}
    assert by_id["click_6"].risk is Risk.IRREVERSIBLE
    assert by_id["click_4"].risk is Risk.SAFE


@pytestmark_browser
def test_an_irreversible_step_blocks_by_default(app, log, write_flow):
    with WebDriver(allowlist=Allowlist(origins=(app.base_url,))) as driver:
        report = ReplayEngine(
            driver=driver, artifact=write_flow, log=log, entry_url=app.entry_url
        ).run(member_id="12345", branch_code="001", initial_deposit="500")

    assert isinstance(report, HardFailure)
    assert report.step_id == "click_6"
    assert "confirmation" in report.expected
    # It stopped AT the commitment, having done the harmless steps before it.
    assert [s.step_id for s in report.steps][:3] == ["open_application", "type_1", "type_2"]


@pytestmark_browser
def test_explicit_confirmation_lets_it_through(app, log, write_flow):
    with WebDriver(allowlist=Allowlist(origins=(app.base_url,))) as driver:
        report = ReplayEngine(
            driver=driver,
            artifact=write_flow,
            log=log,
            entry_url=app.entry_url,
            confirmed=True,
        ).run(member_id="77777", branch_code="001", initial_deposit="500")

    assert isinstance(report, Success)
    assert report.outputs["new_account_number"].startswith("SA-")


@pytestmark_browser
def test_the_write_flow_has_a_business_outcome_of_its_own(app, log, write_flow):
    """Over the approval limit is the bank declining — a real answer, not a
    crash, even though the run did not achieve what was asked."""
    with WebDriver(allowlist=Allowlist(origins=(app.base_url,))) as driver:
        report = ReplayEngine(
            driver=driver,
            artifact=write_flow,
            log=log,
            entry_url=app.entry_url,
            confirmed=True,
        ).run(member_id="12345", branch_code="001", initial_deposit="25000")

    assert isinstance(report, BusinessOutcome)
    assert report.name == "approval_limit_exceeded"
    assert report.ok


# --------------------------------------------------------------------------- #
# Redaction.
# --------------------------------------------------------------------------- #


@pytestmark_browser
def test_a_sensitive_value_is_masked_everywhere_including_derived_text(
    app, log, write_flow
):
    """The account number the app generates embeds the member id. Masking the
    literal everywhere catches it there too, which a rule that only masked
    declared fields would miss."""
    with WebDriver(allowlist=Allowlist(origins=(app.base_url,))) as driver:
        ReplayEngine(
            driver=driver,
            artifact=write_flow,
            log=log,
            entry_url=app.entry_url,
            confirmed=True,
        ).run(member_id="12345", branch_code="001", initial_deposit="500")

    transcript = log.transcript_path.read_text()
    events = log.events_path.read_text()

    assert "12345" not in transcript
    assert "12345" not in events
    assert "SA-***-00500" in transcript  # masked inside the generated number
    assert "001" in transcript  # a non-sensitive parameter stays readable


def test_redaction_is_driven_by_the_capability_not_the_caller(write_flow):
    assert write_flow.parameter("member_id").sensitive
    assert not write_flow.parameter("branch_code").sensitive
    assert write_flow.is_sensitive_ref("params.member_id")
    assert not write_flow.is_sensitive_ref("params.branch_code")

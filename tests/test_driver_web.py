"""M0 checkpoint — the app and the seam, exercised together.

The headline test is `test_happy_path_through_driver_verbs_alone`: start the
app, walk the read flow using nothing but `Driver` verbs and the artifact's own
ladders, and come back with the balance. No Playwright call appears in this
file. If it passes, the seam is real — everything above it can be written
against ladders and conditions without knowing a browser exists.

The rest pin down the ladder's behaviour: that rung 1 is genuinely impossible
for the anonymous field, that each rung works in isolation, and that a broken
rung 1 falls through to rung 2 rather than failing. That last one is the drift
story, testable before there is anything to drift.

These need a real Chromium, so they skip cleanly when it isn't installed —
which keeps `pytest tests` runnable with no browser and no API key.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.server import AppServer
from core.driver import ActionNotAllowed, TargetNotFound, WebDriver
from core.schema import (
    A11yRoleNameLocator,
    AnyOf,
    CoordinatesLocator,
    RelationalLocator,
    RoleNamePresent,
    StructuralLocator,
    TargetLadder,
    TextPresent,
    ValueMatchesRef,
    load_artifact,
)

# The hand-written M1 artifact, kept as a fixture so a discovery run writing
# artifacts/lookup_member_balance.json cannot change what these assert against.
ARTIFACT = str(
    Path(__file__).parent / "fixtures" / "handwritten_lookup_member_balance.json"
)


def _chromium_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            from pathlib import Path

            return Path(p.chromium.executable_path).exists()
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _chromium_available(),
    reason="needs a Chromium install: python -m playwright install chromium",
)


@pytest.fixture(scope="module")
def app():
    with AppServer(port=0) as server:
        yield server


@pytest.fixture(scope="module")
def driver(app):
    with WebDriver(allowed_origins=[app.base_url]) as d:
        yield d


@pytest.fixture
def artifact():
    return load_artifact(ARTIFACT)


def _ladder(*rungs, note="test ladder") -> TargetLadder:
    return TargetLadder(rungs=list(rungs), robustness_note=note)


def _search(driver, app, member_id: str, branch: str = "001", query: str = "") -> None:
    """Fill and submit the search form using only ladders."""
    driver.navigate(app.entry_url + query)
    driver.type(
        _ladder(A11yRoleNameLocator(role="textbox", name="Member ID", exact=True)),
        member_id,
    )
    driver.type(
        _ladder(
            RelationalLocator(
                anchor_text="Branch Code", relation="same_row", target_role="textbox"
            )
        ),
        branch,
    )
    driver.click(_ladder(A11yRoleNameLocator(role="button", name="Search", exact=True)))


# --------------------------------------------------------------------------- #
# The checkpoint.
# --------------------------------------------------------------------------- #


def test_happy_path_through_driver_verbs_alone(driver, app, artifact):
    steps = {s.step_id: s for s in artifact.steps}

    driver.navigate(app.entry_url)
    assert driver.check(steps["open_member_services"].checkpoint.condition, 10_000).ok

    driver.type(steps["enter_member_id"].target, "12345")
    driver.type(steps["enter_branch_code"].target, "001")
    driver.click(steps["submit_search"].target)

    assert driver.check(steps["submit_search"].checkpoint.condition, 8_000).ok

    resolution = driver.read(steps["read_savings_balance"].target)
    assert resolution.text.startswith("$")
    assert resolution.rung == 1


def test_observation_is_text_and_carries_boxes(driver, app):
    driver.navigate(app.entry_url)
    obs = driver.observe()

    assert obs.title == "CoreBankPro - Member Services"
    assert 'textbox "Member ID"' in obs.tree
    assert "[box=" in obs.tree_boxed and "[box=" not in obs.tree
    # The anonymous field shows up as a bare `textbox` with no name — which is
    # the whole reason rung 2 exists.
    assert "- textbox\n" in obs.tree


def test_observation_digest_tracks_state_change(driver, app):
    driver.navigate(app.entry_url)
    before = driver.observe().digest
    _search(driver, app, "12345")
    assert driver.observe().digest != before


# --------------------------------------------------------------------------- #
# The ladder, rung by rung.
# --------------------------------------------------------------------------- #


def test_anonymous_field_has_no_rung_one(driver, app):
    # Not a limitation to work around — the point of the field. Nothing in the
    # a11y tree names it, so role+name cannot reach it at all.
    driver.navigate(app.entry_url)
    with pytest.raises(TargetNotFound):
        driver.type(
            _ladder(A11yRoleNameLocator(role="textbox", name="Branch Code")), "001"
        )


def test_each_rung_reaches_the_anonymous_field(driver, app):
    driver.navigate(app.entry_url)

    rung2 = driver.type(
        _ladder(
            RelationalLocator(
                anchor_text="Branch Code", relation="same_row", target_role="textbox"
            )
        ),
        "002",
    )
    assert rung2.rung == 2

    rung3 = driver.type(_ladder(StructuralLocator(role="textbox", index=1)), "003")
    assert rung3.rung == 3

    box = rung3.box
    rung4 = driver.type(
        _ladder(
            CoordinatesLocator(
                x=box.center_x, y=box.center_y, viewport_width=1280, viewport_height=720
            )
        ),
        "004",
    )
    assert rung4.rung == 4

    # All four wrote to the same field, so rung 4 is a real fallback and not a
    # coincidence that happened to hit something else.
    ladder = _ladder(
        RelationalLocator(
            anchor_text="Branch Code", relation="same_row", target_role="textbox"
        )
    )
    assert driver.check(
        ValueMatchesRef(target=ladder, value_ref="params.branch_code"),
        1000,
        {"params.branch_code": "004"},
    ).ok


def test_broken_rung_one_falls_through_to_rung_two(driver, app):
    """The drift demo in miniature: a renamed control breaks rung 1, and the
    relational rung catches it without the flow changing."""
    driver.navigate(app.entry_url)
    ladder = _ladder(
        A11yRoleNameLocator(role="textbox", name="Mitglieds-Nr.", exact=True),
        RelationalLocator(
            anchor_text="Member ID", relation="same_row", target_role="textbox"
        ),
    )
    resolution = driver.type(ladder, "12345")

    assert resolution.rung == 2
    assert resolution.fell_back
    assert [(a.rung, a.ok) for a in resolution.attempts] == [(1, False), (2, True)]


def test_ambiguous_rung_falls_through_instead_of_guessing(driver, app):
    # Two textboxes, no disambiguation: taking the first would be luck, so the
    # rung is treated as unresolved.
    driver.navigate(app.entry_url)
    with pytest.raises(TargetNotFound) as exc:
        driver.type(_ladder(A11yRoleNameLocator(role="textbox", name="")), "x")
    assert "ambiguous" in str(exc.value)


def test_reading_the_balance_needs_name_only_targeting(driver, app):
    _search(driver, app, "12345")

    # role+name is ambiguous here: the tree gives the span's aria-label to the
    # enclosing cell too. This is what M0 taught us about the real surface.
    with pytest.raises(TargetNotFound):
        driver.read(_ladder(A11yRoleNameLocator(role="cell", name="Savings Balance")))

    by_name = driver.read(_ladder(A11yRoleNameLocator(name="Savings Balance", exact=True)))
    by_row = driver.read(
        _ladder(
            RelationalLocator(
                anchor_text="Savings Balance",
                relation="same_row",
                target_role="cell",
                occurrence=2,
            )
        )
    )
    assert by_name.text == by_row.text != ""


def test_balance_varies_by_member(driver, app):
    # A constant balance would let a bug that reads the wrong cell pass.
    _search(driver, app, "12345")
    first = driver.read(_ladder(A11yRoleNameLocator(name="Savings Balance", exact=True))).text
    _search(driver, app, "54321")
    second = driver.read(_ladder(A11yRoleNameLocator(name="Savings Balance", exact=True))).text
    assert first != second


# --------------------------------------------------------------------------- #
# Conditions.
# --------------------------------------------------------------------------- #


def test_business_outcomes_are_detected_off_screen(driver, app, artifact):
    outcomes = {o.name: o for o in artifact.known_outcomes}

    _search(driver, app, "99999")
    assert driver.check(outcomes["member_not_found"].detect, 2000).ok
    assert not driver.check(outcomes["permission_denied"].detect, 200).ok

    _search(driver, app, "00000")
    assert driver.check(outcomes["permission_denied"].detect, 2000).ok


def test_responded_checkpoint_passes_on_both_branches(driver, app, artifact):
    """The checkpoint after Search must hold whether the app returns a member or
    an error. Narrowing it to the happy path would turn every business outcome
    into a step failure — the mistake the whole taxonomy exists to avoid."""
    responded = next(
        s for s in artifact.steps if s.step_id == "submit_search"
    ).checkpoint.condition
    assert isinstance(responded, AnyOf)

    _search(driver, app, "12345")
    assert driver.check(responded, 3000).ok
    _search(driver, app, "99999")
    assert driver.check(responded, 3000).ok


def test_failed_check_reports_what_was_there_instead(driver, app):
    _search(driver, app, "99999")
    result = driver.check(RoleNamePresent(role="heading", name="Member Detail"), 300)

    assert not result.ok
    assert "No such member" in result.observed
    assert "url=" in result.observed


def test_check_waits_for_a_slow_response_without_sleeping(driver, app):
    _search(driver, app, "12345", query="?slow=1200")
    result = driver.check(RoleNamePresent(role="heading", name="Member Detail"), 6000)

    assert result.ok
    # It waited for the condition rather than returning early or sleeping a
    # guessed interval.
    assert 1000 <= result.waited_ms <= 4000


def test_interstitial_is_visible_then_dismissable(driver, app, artifact):
    recovery = artifact.recoveries[0]
    _search(driver, app, "12345", query="?maintenance=1")

    assert driver.check(recovery.detect, 2000).ok
    assert not driver.check(RoleNamePresent(role="heading", name="Member Detail"), 200).ok

    driver.click(recovery.dismiss_target)
    assert driver.check(RoleNamePresent(role="heading", name="Member Detail"), 3000).ok


def test_empty_text_with_a_role_means_any_such_element(driver, app):
    _search(driver, app, "99999")
    assert driver.check(TextPresent(text="", role="alert"), 1000).ok
    _search(driver, app, "12345")
    assert not driver.check(TextPresent(text="", role="alert"), 300).ok


# --------------------------------------------------------------------------- #
# The write flow and the allowlist.
# --------------------------------------------------------------------------- #


def test_write_flow_is_reachable_and_confirms(driver, app):
    _search(driver, app, "12345")
    driver.click(_ladder(A11yRoleNameLocator(role="button", name="Open Sub-Account")))
    driver.type(
        _ladder(A11yRoleNameLocator(role="textbox", name="Initial Deposit", exact=True)),
        "500",
    )
    driver.click(_ladder(A11yRoleNameLocator(role="button", name="Confirm", exact=True)))

    account = driver.read(_ladder(A11yRoleNameLocator(name="New Account Number", exact=True)))
    assert account.text == "SA-12345-00500"


def test_write_flow_has_a_business_outcome_of_its_own(driver, app):
    # Over the approval limit: a legitimate answer, and M6's handoff trigger.
    _search(driver, app, "12345")
    driver.click(_ladder(A11yRoleNameLocator(role="button", name="Open Sub-Account")))
    driver.type(
        _ladder(A11yRoleNameLocator(role="textbox", name="Initial Deposit", exact=True)),
        "25000",
    )
    driver.click(_ladder(A11yRoleNameLocator(role="button", name="Confirm", exact=True)))

    assert driver.check(
        TextPresent(text="Manager approval required", role="alert"), 2000
    ).ok


def test_off_allowlist_navigation_is_refused(app):
    # Enforced inside the driver, so a flow cannot route around it. The origin
    # is real because the app is served over HTTP — an allowlist over file://
    # paths would be theatre.
    with WebDriver(allowed_origins=[app.base_url]) as d:
        d.navigate(app.entry_url)
        with pytest.raises(ActionNotAllowed):
            d.navigate("https://example.com/")


def test_screenshot_lands_in_evidence(driver, app, tmp_path):
    _search(driver, app, "99999")
    out = driver.screenshot(tmp_path / "failure.png")
    assert out.exists() and out.stat().st_size > 0

"""M4 — deterministic replay and the error taxonomy.

These run the real approved artifact against the real app with no model
anywhere. The most important test in the file is
`test_member_not_found_is_an_answer_not_a_failure`: getting that distinction
wrong is the single most common way to build this badly, so it is asserted from
several angles — the type returned, the classification, and the fact that a
caller checking `.ok` sees a success.

The drift and recovery tests use the app's `?drift=` and `?maintenance=` flags
so the failure modes are reproducible rather than staged by hand.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.server import AppServer
from core.driver import WebDriver
from core.logging import EvidenceLog
from core.replay import (
    BusinessOutcome,
    HardFailure,
    ParameterError,
    ReplayEngine,
    Success,
)
from core.schema import Classification, Risk, load_artifact

ARTIFACT_PATH = Path(__file__).parent.parent / "artifacts" / "lookup_member_balance.json"


def _chromium_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            return Path(p.chromium.executable_path).exists()
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _chromium_available(), reason="needs a Chromium install"
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
    return load_artifact(ARTIFACT_PATH)


@pytest.fixture
def log(tmp_path):
    return EvidenceLog(run_id="replay-test", kind="replay", root=tmp_path)


def engine(driver, artifact, log, app, query: str = "", **kw) -> ReplayEngine:
    return ReplayEngine(
        driver=driver,
        artifact=artifact,
        log=log,
        entry_url=app.entry_url + query,
        **kw,
    )


# --------------------------------------------------------------------------- #
# The happy path.
# --------------------------------------------------------------------------- #


def test_replay_succeeds_and_returns_the_output(driver, artifact, log, app):
    report = engine(driver, artifact, log, app).run(member_id="12345", branch_code="001")

    assert isinstance(report, Success)
    assert report.outputs["savings_balance"].startswith("$")
    assert report.classification is Classification.SUCCESS
    assert all(s.checkpoint_ok for s in report.steps)


def test_replay_needs_no_api_key(driver, artifact, log, app, monkeypatch):
    """The central claim of the whole design: the model compiled this once, and
    production never calls one again."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:1/would-fail")

    report = engine(driver, artifact, log, app).run(member_id="12345", branch_code="001")
    assert isinstance(report, Success)


def test_replay_is_deterministic(driver, artifact, log, app):
    first = engine(driver, artifact, log, app).run(member_id="54321", branch_code="002")
    second = engine(driver, artifact, log, app).run(member_id="54321", branch_code="002")

    assert isinstance(first, Success) and isinstance(second, Success)
    assert first.outputs == second.outputs
    assert [s.rung for s in first.steps] == [s.rung for s in second.steps]


def test_replay_module_knows_nothing_about_this_application():
    """Replay must read outcomes off the screen, never reimplement the app's
    rules. If the engine's *code* mentions anything specific to CoreBankPro, it
    has started branching on input and the taxonomy is a fiction.

    Comments and docstrings are stripped first: the module explains at length
    why it must not know that 99999 means "no such member", and that prose is
    the opposite of the problem being tested for.
    """
    import ast

    tree = ast.parse((Path(__file__).parent.parent / "core" / "replay.py").read_text())
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(body, list) and body and isinstance(body[0], ast.Expr):
            if isinstance(getattr(body[0], "value", None), ast.Constant) and isinstance(
                body[0].value.value, str
            ):
                body.pop(0)
    code = ast.unparse(tree)  # also drops comments

    for app_specific in (
        "99999", "00000", "Member Detail", "Savings Balance",
        "member_id", "No such member", "branch",
    ):
        assert app_specific not in code, f"replay.py's code mentions {app_specific!r}"


# --------------------------------------------------------------------------- #
# Business outcomes — the distinction that matters most.
# --------------------------------------------------------------------------- #


def test_member_not_found_is_an_answer_not_a_failure(driver, artifact, log, app):
    report = engine(driver, artifact, log, app).run(member_id="99999", branch_code="001")

    assert isinstance(report, BusinessOutcome)
    assert report.name == "member_not_found"
    assert report.classification is Classification.BUSINESS_OUTCOME
    # A caller asking "did this work" gets yes: the bank answered the question.
    assert report.ok
    assert not isinstance(report, HardFailure)


def test_permission_denied_is_also_a_clean_answer(driver, artifact, log, app):
    report = engine(driver, artifact, log, app).run(member_id="00000", branch_code="001")

    assert isinstance(report, BusinessOutcome)
    assert report.name == "permission_denied"
    assert report.ok


def test_a_business_outcome_stops_before_the_remaining_steps(driver, artifact, log, app):
    """There is no balance to read for a member who does not exist, so the run
    ends where the answer arrived instead of failing at the next step."""
    report = engine(driver, artifact, log, app).run(member_id="99999", branch_code="001")

    assert [s.step_id for s in report.steps][-1] == "click_3"
    assert "read_savings_balance" not in [s.step_id for s in report.steps]


def test_the_outcome_is_read_off_the_screen(driver, artifact, log, app):
    """Same artifact, same step, different member: the classification comes
    from what the application displayed, not from the value we sent."""
    found = engine(driver, artifact, log, app).run(member_id="12345", branch_code="001")
    missing = engine(driver, artifact, log, app).run(member_id="99999", branch_code="001")

    assert isinstance(found, Success)
    assert isinstance(missing, BusinessOutcome)


# --------------------------------------------------------------------------- #
# Hard failures — the surface disagreeing with the artifact.
# --------------------------------------------------------------------------- #


def test_a_missing_control_is_a_hard_failure_with_evidence(driver, artifact, log, app):
    report = engine(driver, artifact, log, app, query="?drift=2").run(
        member_id="12345", branch_code="001"
    )

    assert isinstance(report, HardFailure)
    assert not report.ok
    assert report.expected and report.observed
    # Debuggable: it says what it wanted and what was actually there.
    assert "Search" in report.expected
    assert "controls present" in report.observed
    assert report.screenshot and Path(report.screenshot).exists()


def test_our_own_bug_is_a_hard_failure_not_a_business_outcome(driver, artifact, log, app):
    """The app says "Please enter a Member ID". That is not the bank answering
    a question — it means our typing never landed, so it must not be dressed up
    as a business outcome."""
    report = engine(driver, artifact, log, app).run(member_id="", branch_code="001")

    assert isinstance(report, HardFailure)
    assert "did not land" in report.observed


def test_failure_names_the_step_that_failed(driver, artifact, log, app):
    report = engine(driver, artifact, log, app, query="?drift=2").run(
        member_id="12345", branch_code="001"
    )
    assert report.step_id in {s.step_id for s in artifact.steps}


# --------------------------------------------------------------------------- #
# Recoverable — a behaviour, never a result.
# --------------------------------------------------------------------------- #


def test_an_interstitial_is_dismissed_and_the_run_continues(driver, artifact, log, app):
    report = engine(driver, artifact, log, app, query="?maintenance=1").run(
        member_id="12345", branch_code="001"
    )

    assert isinstance(report, Success)
    applied = [r for s in report.steps for r in s.recoveries]
    assert "maintenance_notice" in applied


def test_the_recovery_is_attributed_to_the_step_that_raised_it(driver, artifact, log, app):
    """It must be cleared before the state is judged, or outcome matching runs
    against a screen that is still in the way."""
    report = engine(driver, artifact, log, app, query="?maintenance=1").run(
        member_id="12345", branch_code="001"
    )
    by_step = {s.step_id: s.recoveries for s in report.steps}
    assert "maintenance_notice" in by_step["click_3"]


def test_a_slow_response_is_waited_for_not_slept_through(driver, artifact, log, app):
    report = engine(driver, artifact, log, app, query="?slow=2000").run(
        member_id="12345", branch_code="001"
    )

    assert isinstance(report, Success)
    click = next(s for s in report.steps if s.step_id == "click_3")
    # It waited about as long as the delay: longer than the delay, and not the
    # full checkpoint timeout, which is what a blind sleep would produce.
    assert 1900 <= click.waited_ms <= 5000
    # Every other step returned promptly — the wait was on the condition, not
    # a fixed pause applied everywhere.
    assert all(s.waited_ms < 1000 for s in report.steps if s.step_id != "click_3")


# --------------------------------------------------------------------------- #
# The targeting ladder under drift.
# --------------------------------------------------------------------------- #


def test_a_renamed_control_falls_through_to_the_relational_rung(driver, artifact, log, app):
    """The drift demo: the app renames its accessible label, the recorded flow
    keeps working, and the evidence says exactly which rung saved it."""
    report = engine(driver, artifact, log, app, query="?drift=1").run(
        member_id="12345", branch_code="001"
    )

    assert isinstance(report, Success)
    typed = next(s for s in report.steps if s.step_id == "type_1")
    assert typed.fell_back
    assert typed.rung == 2
    assert sum(1 for a in typed.attempts if "ok" not in a) == 2


def test_undrifted_run_uses_rung_one(driver, artifact, log, app):
    report = engine(driver, artifact, log, app).run(member_id="12345", branch_code="001")
    typed = next(s for s in report.steps if s.step_id == "type_1")
    assert typed.rung == 1 and not typed.fell_back


# --------------------------------------------------------------------------- #
# Parameters and safety hooks.
# --------------------------------------------------------------------------- #


def test_unknown_parameter_is_rejected(driver, artifact, log, app):
    with pytest.raises(ParameterError, match="does not take"):
        engine(driver, artifact, log, app).run(member_id="1", branch_code="1", ssn="x")


def test_missing_required_parameter_is_rejected(driver, artifact, log, app):
    with pytest.raises(ParameterError, match="missing required"):
        engine(driver, artifact, log, app).run(member_id="12345")


def test_an_irreversible_step_is_blocked_without_confirmation(driver, artifact, log, app):
    """The safety hook M5 builds on: an irreversible action should need someone
    to say yes, not need someone to remember to say no."""
    risky = artifact.model_copy(deep=True)
    risky.steps[3].risk = Risk.IRREVERSIBLE

    report = engine(driver, risky, log, app).run(member_id="12345", branch_code="001")
    assert isinstance(report, HardFailure)
    assert "confirmation" in report.expected

    allowed = engine(driver, risky, log, app, confirmed=True).run(
        member_id="12345", branch_code="001"
    )
    assert isinstance(allowed, Success)


# --------------------------------------------------------------------------- #
# Evidence.
# --------------------------------------------------------------------------- #


def test_sensitive_parameters_are_masked_in_the_replay_evidence(driver, artifact, log, app):
    """Masking is driven by the artifact's own `sensitive` flag, so it happens
    because the capability says so — not because the caller remembered."""
    assert artifact.parameter("member_id").sensitive

    engine(driver, artifact, log, app).run(member_id="12345", branch_code="001")
    transcript = log.transcript_path.read_text()

    assert "12345" not in transcript
    assert "***" in transcript
    # The non-sensitive parameter stays readable.
    assert "001" in transcript


def test_transcript_records_the_rung_and_the_wait(driver, artifact, log, app):
    engine(driver, artifact, log, app, query="?drift=1").run(
        member_id="12345", branch_code="001"
    )
    transcript = log.transcript_path.read_text()

    assert "Model in the loop" in transcript and "none" in transcript
    assert "Fallback trail" in transcript
    assert "Checkpoint" in transcript


# --------------------------------------------------------------------------- #
# Session expiry — named, therefore fast.
# --------------------------------------------------------------------------- #


def test_an_expired_session_is_a_named_failure_not_an_unexplained_one(
    driver, artifact, log, app
):
    """Both are hard failures, but only one is diagnosable. Because the outcome
    is declared, the report explains itself instead of saying "the checkpoint
    did not hold"."""
    report = engine(driver, artifact, log, app, query="?session_expires=1").run(
        member_id="12345", branch_code="001"
    )

    assert isinstance(report, HardFailure)
    assert "session_expired" in report.outcomes_seen
    assert "signed us out" in report.observed
    assert report.needs_a_human


def test_naming_a_failure_makes_it_fast_to_detect(driver, artifact, log, app):
    """The measured property from REPORT §3: an anticipated failure satisfies
    the "did the app respond" checkpoint immediately and is classified by the
    outcomes, where an unanticipated one has to wait out the full timeout.

    Worth a test rather than a note, because the whole argument for authoring
    known_outcomes at review time rests on it.
    """
    import time

    started = time.time()
    engine(driver, artifact, log, app, query="?session_expires=1").run(
        member_id="12345", branch_code="001"
    )
    named_ms = (time.time() - started) * 1000

    started = time.time()
    engine(driver, artifact, log, app, query="?blocker=1").run(
        member_id="12345", branch_code="001"
    )
    unnamed_ms = (time.time() - started) * 1000

    assert named_ms < 2000, f"a named failure took {named_ms:.0f}ms"
    assert unnamed_ms > 5000, f"an unnamed failure took only {unnamed_ms:.0f}ms"
    assert unnamed_ms > named_ms * 4


def test_a_failure_report_preserves_what_was_measured(driver, artifact, log, app):
    """Found by reading a transcript: every hard failure used to report
    "checkpoint FAILED after 0 ms" — the defaults of a freshly built record —
    which erased the difference between a checkpoint that timed out and one
    that passed and was then classified by a named outcome. Those differ by
    eight seconds and by how much you know about what went wrong.
    """
    named = engine(driver, artifact, log, app, query="?session_expires=1").run(
        member_id="12345", branch_code="001"
    )
    unnamed = engine(driver, artifact, log, app, query="?blocker=1").run(
        member_id="12345", branch_code="001"
    )

    named_step = next(s for s in named.steps if s.error)
    unnamed_step = next(s for s in unnamed.steps if s.error)

    # The named one: the app responded, the checkpoint held, an outcome
    # explained it. The rung that resolved is still on the record.
    assert named_step.checkpoint_ok
    assert named_step.waited_ms < 1000
    assert named_step.rung == 1

    # The unnamed one: nothing recognised the screen, so the wait ran out.
    assert not unnamed_step.checkpoint_ok
    assert unnamed_step.waited_ms > 5000

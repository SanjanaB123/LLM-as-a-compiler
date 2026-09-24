"""Stretch goal — the eval harness.

Two things are tested here, and the second is the one that justifies the
module: that the harness scores consistency correctly, and that it **fails a
suite leaving any declared outcome unreached**.

That second behaviour is not hypothetical. On its first run against the real
capability it reported `branch_code_not_submitted` as declared but never
reached — an outcome hand-authored at review time and never once proven to
fire. `test_an_unreached_outcome_fails_the_suite` is that finding, frozen.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.server import AppServer
from core.driver import WebDriver
from core.evals import EvalSuite, load_suite, run_suite, suite_path
from core.logging import EvidenceLog
from core.safety import Allowlist
from core.schema import load_artifact

ROOT = Path(__file__).parent.parent
ARTIFACTS = ROOT / "artifacts"
EVALS = ROOT / "evals"


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
# The suites on disk.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("capability", ["lookup_member_balance", "open_sub_account"])
def test_every_capability_has_a_valid_suite(capability):
    suite = load_suite(suite_path(capability, EVALS))
    assert suite.capability_id == capability
    assert suite.scenarios
    # A scenario without a description is a scenario nobody can review.
    assert all(s.description for s in suite.scenarios)


@pytest.mark.parametrize("capability", ["lookup_member_balance", "open_sub_account"])
def test_suites_only_use_parameters_the_capability_declares(capability):
    artifact = load_artifact(ARTIFACTS / f"{capability}.json")
    declared = {p.name for p in artifact.parameters}
    for scenario in load_suite(suite_path(capability, EVALS)).scenarios:
        assert set(scenario.params) <= declared, scenario.name


@pytest.mark.parametrize("capability", ["lookup_member_balance", "open_sub_account"])
def test_suites_name_outcomes_that_exist(capability):
    artifact = load_artifact(ARTIFACTS / f"{capability}.json")
    known = {o.name for o in artifact.known_outcomes}
    for scenario in load_suite(suite_path(capability, EVALS)).scenarios:
        if scenario.expect_outcome:
            assert scenario.expect_outcome in known, scenario.name


@pytest.mark.parametrize("capability", ["lookup_member_balance", "open_sub_account"])
def test_every_declared_outcome_has_a_scenario_aimed_at_it(capability):
    """The static half of the reachability check.

    Cheap, runs with no browser, and catches the gap at review time rather
    than after a twenty-run suite. It cannot prove a detect condition matches
    anything — only running it can do that — but it can prove somebody at
    least tried.
    """
    artifact = load_artifact(ARTIFACTS / f"{capability}.json")
    suite = load_suite(suite_path(capability, EVALS))

    aimed = {s.expect_outcome for s in suite.scenarios if s.expect_outcome}
    # Outcomes reached incidentally (success markers, our-bug classifications)
    # are not named by a scenario, so require only that the count is plausible:
    # every business outcome must be someone's explicit target.
    business = {
        o.name for o in artifact.known_outcomes if o.classification.value == "business_outcome"
    }
    assert business <= aimed, f"no scenario targets {sorted(business - aimed)}"


# --------------------------------------------------------------------------- #
# Scoring.
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def app():
    with AppServer(port=0) as server:
        yield server


@pytest.fixture(scope="module")
def driver(app):
    with WebDriver(allowlist=Allowlist(origins=(app.base_url,))) as d:
        yield d


@pytest.fixture
def log(tmp_path):
    return EvidenceLog(run_id="eval-test", kind="eval", root=tmp_path)


def _suite(*scenarios) -> EvalSuite:
    return EvalSuite.model_validate(
        {"capability_id": "lookup_member_balance", "scenarios": list(scenarios)}
    )


@browser_required
def test_a_consistent_scenario_passes(driver, app, log):
    artifact = load_artifact(ARTIFACTS / "lookup_member_balance.json")
    suite = _suite(
        {
            "name": "member_found",
            "description": "happy path",
            "params": {"member_id": "12345", "branch_code": "001"},
            "expect": "success",
        },
        {
            "name": "member_not_found",
            "description": "the bank answering",
            "params": {"member_id": "99999", "branch_code": "001"},
            "expect": "business_outcome",
            "expect_outcome": "member_not_found",
        },
    )
    report = run_suite(driver, artifact, suite, log, app.entry_url, runs=3)

    assert all(r.passed for r in report.results)
    assert [r.consistency for r in report.results] == ["3/3", "3/3"]


@browser_required
def test_a_wrong_expectation_fails_with_the_bucket_it_actually_got(driver, app, log):
    artifact = load_artifact(ARTIFACTS / "lookup_member_balance.json")
    suite = _suite(
        {
            "name": "wrong",
            "description": "claims a missing member is a success",
            "params": {"member_id": "99999", "branch_code": "001"},
            "expect": "success",
        }
    )
    report = run_suite(driver, artifact, suite, log, app.entry_url, runs=2)

    result = report.results[0]
    assert not result.passed
    assert result.buckets["business_outcome"] == 2
    assert any("expected success, got business_outcome" in f for f in result.failures)


@browser_required
def test_the_rung_assertion_catches_a_fallback_that_stopped_happening(driver, app, log):
    """Bucket-only assertions would pass the drift scenario even if rung 1 had
    quietly started working again — and the fallback would stop being tested
    without anyone noticing."""
    artifact = load_artifact(ARTIFACTS / "lookup_member_balance.json")
    suite = _suite(
        {
            "name": "expects_a_fallback_that_will_not_happen",
            "description": "no drift, so rung 1 resolves and rung 2 is never needed",
            "params": {"member_id": "12345", "branch_code": "001"},
            "expect": "success",
            "expect_rung": {"step_id": "type_1", "rung": 2},
        }
    )
    report = run_suite(driver, artifact, suite, log, app.entry_url, runs=1)

    assert not report.results[0].passed
    assert any("rung 1, expected rung 2" in f for f in report.results[0].failures)


# --------------------------------------------------------------------------- #
# Reachability — the reason this module exists.
# --------------------------------------------------------------------------- #


@browser_required
def test_an_unreached_outcome_fails_the_suite(driver, app, log):
    """The real finding, frozen.

    On the harness's first run, `branch_code_not_submitted` came back declared
    but never reached — hand-authored at review, well-formed, schema-valid, and
    never once proven to fire. Nothing else in the system can catch that: a
    typo in a detect condition is invisible until a customer hits the branch
    and a clean business outcome silently degrades into a hard failure.
    """
    artifact = load_artifact(ARTIFACTS / "lookup_member_balance.json")
    suite = _suite(
        {
            "name": "happy_only",
            "description": "exercises the happy path and nothing else",
            "params": {"member_id": "12345", "branch_code": "001"},
            "expect": "success",
        }
    )
    report = run_suite(driver, artifact, suite, log, app.entry_url, runs=1)

    # The scenario itself is fine...
    assert all(r.passed for r in report.results)
    # ...but the suite is not, because four declared branches went unproven.
    assert not report.passed
    assert "member_not_found" in report.unreached_outcomes
    assert "balance_returned" in report.outcomes_reached


@browser_required
def test_a_full_suite_reaches_every_declared_outcome(driver, app, log):
    """The committed suite, at low run count — the property the CLI asserts at 20."""
    artifact = load_artifact(ARTIFACTS / "lookup_member_balance.json")
    suite = load_suite(suite_path("lookup_member_balance", EVALS))

    report = run_suite(driver, artifact, suite, log, app.entry_url, runs=1)

    assert report.unreached_outcomes == []
    assert report.passed


@browser_required
def test_non_terminal_outcomes_are_recorded_too(driver, app, log):
    """`balance_returned` is classified success, so it never stops a run and
    never appears as a result — but it still has to be provably reachable."""
    artifact = load_artifact(ARTIFACTS / "lookup_member_balance.json")
    suite = _suite(
        {
            "name": "happy",
            "description": "happy path",
            "params": {"member_id": "12345", "branch_code": "001"},
            "expect": "success",
        }
    )
    report = run_suite(driver, artifact, suite, log, app.entry_url, runs=1)
    assert "balance_returned" in report.outcomes_reached


# --------------------------------------------------------------------------- #
# Evidence.
# --------------------------------------------------------------------------- #


@browser_required
def test_the_eval_writes_a_transcript_and_events(driver, app, log):
    artifact = load_artifact(ARTIFACTS / "lookup_member_balance.json")
    suite = _suite(
        {
            "name": "happy",
            "description": "happy path",
            "params": {"member_id": "12345", "branch_code": "001"},
            "expect": "success",
        }
    )
    run_suite(driver, artifact, suite, log, app.entry_url, runs=2)

    transcript = log.transcript_path.read_text()
    assert "Outcome reachability" in transcript
    assert "NEVER REACHED" in transcript  # the unreached ones are named
    assert "| happy | success | 2/2 |" in transcript

    events = [json.loads(line) for line in log.events_path.read_text().splitlines()]
    scenario_events = [e for e in events if e["event"] == "eval_scenario"]
    assert scenario_events and scenario_events[0]["consistency"] == "2/2"
    assert [e for e in events if e["event"] == "eval_complete"]

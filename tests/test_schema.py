"""M1 — schema validation.

The positive test proves the contract can express the real capability. The
negative tests are the point: each one is a guarantee the schema enforces, so a
future change that quietly breaks it fails here rather than in production.
"""

import copy
import json
import pathlib

import pytest
from pydantic import ValidationError

from core.schema import (
    Artifact,
    Classification,
    Status,
    load_artifact,
    json_schema,
)

# The M1 checkpoint artifact, hand-written before discovery could produce one.
# It lives here rather than in artifacts/ so that a discovery run — which
# writes the real capability under that name — cannot quietly change what
# these tests are asserting against.
ARTIFACT_PATH = (
    pathlib.Path(__file__).parent / "fixtures" / "handwritten_lookup_member_balance.json"
)


@pytest.fixture
def raw() -> dict:
    return json.loads(ARTIFACT_PATH.read_text())


# --------------------------------------------------------------------------- #
# The M1 checkpoint: a hand-written artifact loads and validates.
# --------------------------------------------------------------------------- #


def test_handwritten_artifact_validates():
    a = load_artifact(ARTIFACT_PATH)
    assert a.capability_id == "lookup_member_balance"
    assert a.schema_version == "1.0"
    assert len(a.steps) == 5


def test_every_step_has_a_checkpoint():
    # Decision #8: deterministic must not mean blind.
    for step in load_artifact(ARTIFACT_PATH).steps:
        assert step.checkpoint.description


def test_business_outcome_is_distinct_from_failure():
    a = load_artifact(ARTIFACT_PATH)
    by_name = {o.name: o.classification for o in a.known_outcomes}
    assert by_name["member_not_found"] is Classification.BUSINESS_OUTCOME
    assert by_name["member_id_not_submitted"] is Classification.HARD_FAILURE


def test_json_schema_exports():
    schema = json_schema()
    assert schema["title"] == "Artifact"
    assert "capability_id" in schema["properties"]


# --------------------------------------------------------------------------- #
# Redaction is structural, not a policy.
# --------------------------------------------------------------------------- #


def test_artifact_holds_no_literal_values(raw):
    # The whole serialized artifact must not contain a plausible member id.
    assert "12345" not in ARTIFACT_PATH.read_text()
    type_steps = [s for s in raw["steps"] if s["action"] == "type"]
    assert type_steps, "expected at least one type step"
    for step in type_steps:
        assert step["value_ref"].startswith(("params.", "target.", "outputs."))


def test_literal_value_ref_is_rejected(raw):
    raw = copy.deepcopy(raw)
    next(s for s in raw["steps"] if s["step_id"] == "enter_member_id")["value_ref"] = "12345"
    with pytest.raises(ValidationError, match="not a value reference"):
        Artifact.model_validate(raw)


def test_sensitive_parameter_may_not_carry_an_example(raw):
    raw = copy.deepcopy(raw)
    raw["parameters"][0]["example"] = "12345"
    with pytest.raises(ValidationError, match="may not carry an example"):
        Artifact.model_validate(raw)


def test_sensitive_lookup_drives_the_redacting_logger():
    a = load_artifact(ARTIFACT_PATH)
    assert a.is_sensitive_ref("params.member_id") is True
    assert a.is_sensitive_ref("target.entry_url") is False


# --------------------------------------------------------------------------- #
# The targeting ladder.
# --------------------------------------------------------------------------- #


def test_ladder_must_be_ordered_sturdiest_first(raw):
    raw = copy.deepcopy(raw)
    step = next(s for s in raw["steps"] if s["step_id"] == "enter_member_id")
    step["target"]["rungs"].reverse()
    with pytest.raises(ValidationError, match="sturdiest-first"):
        Artifact.model_validate(raw)


def test_ladder_carries_its_robustness_reasoning(raw):
    for step in load_artifact(ARTIFACT_PATH).steps:
        if step.target is not None:
            assert len(step.target.robustness_note) > 40, step.step_id


def test_unlabelled_control_can_still_be_targeted(raw):
    # The app's second search field has no accessible name, so rung 1 is
    # unavailable there. A ladder starting at rung 2 must be legal.
    raw = copy.deepcopy(raw)
    step = next(s for s in raw["steps"] if s["step_id"] == "enter_member_id")
    step["target"]["rungs"] = [
        r for r in step["target"]["rungs"] if r["kind"] != "a11y_role_name"
    ]
    assert Artifact.model_validate(raw).steps[1].target.rungs[0].kind == "relational"


# --------------------------------------------------------------------------- #
# References resolve, so a dangling ref is a validation error and not a
# runtime mystery in replay.
# --------------------------------------------------------------------------- #


def test_undeclared_parameter_reference_is_rejected(raw):
    raw = copy.deepcopy(raw)
    next(s for s in raw["steps"] if s["step_id"] == "enter_member_id")["value_ref"] = "params.ssn"
    with pytest.raises(ValidationError, match="undeclared parameter"):
        Artifact.model_validate(raw)


def test_declared_output_must_be_extracted(raw):
    raw = copy.deepcopy(raw)
    raw["outputs"].append({"name": "member_name", "type": "string", "description": "Name."})
    with pytest.raises(ValidationError, match="never extracted"):
        Artifact.model_validate(raw)


def test_duplicate_step_ids_are_rejected(raw):
    raw = copy.deepcopy(raw)
    raw["steps"].append(copy.deepcopy(raw["steps"][1]))
    with pytest.raises(ValidationError, match="duplicate step_id"):
        Artifact.model_validate(raw)


def test_unknown_field_is_rejected(raw):
    raw = copy.deepcopy(raw)
    raw["capabilty_id"] = "typo"
    with pytest.raises(ValidationError):
        Artifact.model_validate(raw)


# --------------------------------------------------------------------------- #
# Action shape and the tenant slot.
# --------------------------------------------------------------------------- #


def test_acting_step_requires_a_target(raw):
    raw = copy.deepcopy(raw)
    next(s for s in raw["steps"] if s["step_id"] == "submit_search").pop("target")
    with pytest.raises(ValidationError, match="requires a target ladder"):
        Artifact.model_validate(raw)


def test_read_step_must_bind_an_output(raw):
    raw = copy.deepcopy(raw)
    next(s for s in raw["steps"] if s["step_id"] == "read_savings_balance").pop("extract_to")
    with pytest.raises(ValidationError, match="read requires extract_to"):
        Artifact.model_validate(raw)


def test_entry_url_must_sit_inside_the_allowlist(raw):
    raw = copy.deepcopy(raw)
    raw["target"]["entry_url"] = "http://evil.example.com/members.html"
    with pytest.raises(ValidationError, match="not covered by allowed_origins"):
        Artifact.model_validate(raw)


# --------------------------------------------------------------------------- #
# Lifecycle: draft may be incomplete; approved is a claim a human reviewed it.
# --------------------------------------------------------------------------- #


def test_draft_may_omit_known_outcomes(raw):
    raw = copy.deepcopy(raw)
    raw["status"] = "draft"
    raw["known_outcomes"] = []
    assert Artifact.model_validate(raw).status is Status.DRAFT


def test_approved_requires_authored_outcomes(raw):
    raw = copy.deepcopy(raw)
    raw["known_outcomes"] = []
    with pytest.raises(ValidationError, match="at least one known_outcome"):
        Artifact.model_validate(raw)


# --------------------------------------------------------------------------- #
# M3 — what the review step tells a human to fix.
# --------------------------------------------------------------------------- #


def test_review_flags_what_discovery_cannot_know(raw):
    """The approved fixture is complete, so it should raise nothing. Strip the
    outcomes and narrow a checkpoint, and both problems must be reported."""
    from cli import review_concerns

    approved = Artifact.model_validate(raw)
    assert review_concerns(approved) == []

    draft = copy.deepcopy(raw)
    draft["status"] = "draft"
    draft["known_outcomes"] = []
    narrowed = next(s for s in draft["steps"] if s["step_id"] == "submit_search")
    narrowed["checkpoint"]["condition"] = {
        "kind": "role_name_present",
        "role": "heading",
        "name": "Member Detail",
        "exact": True,
    }

    concerns = review_concerns(Artifact.model_validate(draft))
    assert any("Happy-path checkpoints on submit_search" in c for c in concerns)
    assert any("No known_outcomes" in c for c in concerns)


def test_review_flags_a_committing_click_but_not_a_search(raw):
    """A read-only flow has no irreversible step, and saying otherwise is
    noise. A button named 'Confirm' is a different matter."""
    from cli import review_concerns

    assert not any("irreversible" in c for c in review_concerns(Artifact.model_validate(raw)))

    committing = copy.deepcopy(raw)
    click = next(s for s in committing["steps"] if s["step_id"] == "submit_search")
    click["target"]["rungs"][0]["name"] = "Confirm"
    concerns = review_concerns(Artifact.model_validate(committing))
    assert any("Possibly irreversible" in c and "Confirm" in c for c in concerns)


def test_review_does_not_flag_a_navigate_checkpoint(raw):
    """A navigate to a known entry URL has one deterministic result, so
    asserting the page loaded is correct — not a happy-path assumption."""
    from cli import review_concerns

    entry = next(s for s in raw["steps"] if s["action"] == "navigate")
    assert entry["checkpoint"]["condition"]["kind"] == "role_name_present"
    assert not any(
        "Happy-path" in c for c in review_concerns(Artifact.model_validate(raw))
    )

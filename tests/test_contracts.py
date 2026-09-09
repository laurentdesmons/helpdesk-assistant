"""Contract shape + validation tests. Hermetic, no network."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from helpdesk.contracts import (
    Category,
    Classification,
    ClassifierInput,
    EscalationRecord,
    HelpdeskResult,
    ResolverInput,
    ResolverOutput,
)


def test_classifier_input_matches_readme_section_3() -> None:
    ci = ClassifierInput(request_id="r1", user_id="u1", message_text="I was double charged", channel="email")
    assert set(ci.model_dump()) == {"request_id", "user_id", "message_text", "channel"}


def test_classification_output_matches_readme_section_3() -> None:
    c = Classification(category=Category.billing, confidence=0.91, rationale="mentions a charge")
    dumped = c.model_dump()
    assert dumped == {"category": "billing", "confidence": 0.91, "rationale": "mentions a charge"}


@pytest.mark.parametrize(("raw", "clamped"), [(-0.1, 0.0), (1.1, 1.0), (2.0, 1.0), (0.42, 0.42)])
def test_classification_confidence_is_clamped(raw: float, clamped: float) -> None:
    # Schema stays constraint-free for strict structured-output validators;
    # out-of-range confidence from the model is clamped, not rejected.
    assert Classification(category=Category.hr, confidence=raw, rationale="x").confidence == clamped


def test_classification_blank_rationale_rejected() -> None:
    with pytest.raises(ValidationError):
        Classification(category=Category.hr, confidence=0.5, rationale="   ")


def test_unknown_keys_are_rejected() -> None:
    with pytest.raises(ValidationError):
        Classification(category=Category.support, confidence=0.5, rationale="x", extra="nope")


def test_resolver_output_answered_requires_answer() -> None:
    with pytest.raises(ValidationError):
        ResolverOutput(request_id="r1", status="answered")

    ok = ResolverOutput(
        request_id="r1",
        status="answered",
        answer="Reset via the self-service portal.",
        citations=[{"doc_id": "password-reset-policy::self-service-reset"}],
    )
    assert ok.answer


def test_resolver_output_escalated_requires_reason() -> None:
    with pytest.raises(ValidationError):
        ResolverOutput(request_id="r1", status="escalated")

    ok = ResolverOutput(request_id="r1", status="escalated", escalation_reason="not_grounded")
    assert ok.escalation_reason == "not_grounded"


def test_resolver_input_shape() -> None:
    ri = ResolverInput(request_id="r1", category="support", message_text="vpn down")
    assert ri.category == "support"


def test_escalation_record_defaults_and_schema_section_6() -> None:
    rec = EscalationRecord(request_id="r1", category=Category.support, escalation_reason="low_confidence")
    assert rec.status == "flagged"
    assert rec.created_at.tzinfo is not None
    assert set(rec.model_dump()) == {
        "request_id",
        "category",
        "escalation_reason",
        "status",
        "created_at",
    }


def test_helpdesk_result_roundtrips() -> None:
    r = HelpdeskResult(request_id="r1", outcome="escalated", escalation_reason="policy_escalation")
    assert HelpdeskResult.model_validate_json(r.model_dump_json()) == r

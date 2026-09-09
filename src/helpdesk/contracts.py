"""The JSON contracts for the whole system — defined once, imported everywhere.

These map directly to README sections 3 (Classifier), 4 (Resolver), and 6
(Escalation state). ``ResolverOutput`` and ``HelpdeskResult`` are additions:
README specifies resolver *behaviour* but not an output shape, and the
orchestrator needs a top-level response type. Both are folded into README §4 in
Phase 3.

Nothing here does I/O. Import freely from agents, scripts, and tests.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# extra="forbid": reject unknown keys so a drifting producer fails loudly.
# use_enum_values=True: serialize Category as its string value ("billing"), which
# is what the model I/O and the search index expect.
_STRICT = ConfigDict(extra="forbid", use_enum_values=True)


class Category(StrEnum):
    """The category taxonomy (README §1)."""

    billing = "billing"
    support = "support"
    hr = "hr"


EscalationReason = Literal["not_grounded", "policy_escalation", "low_confidence"]
ResolverStatus = Literal["answered", "escalated"]
EscalationStatus = Literal["flagged", "in_progress", "resolved"]


# --------------------------------------------------------------------------- #
# Classifier (README §3)
# --------------------------------------------------------------------------- #
class ClassifierInput(BaseModel):
    model_config = _STRICT

    request_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    message_text: str = Field(min_length=1)
    channel: str = Field(min_length=1, description='e.g. "email" | "teams" | "portal"')


class Classification(BaseModel):
    """Classifier output — and the model's structured-output schema.

    This model is sent to the model as a structured-output JSON schema. Strict
    structured-output validators (both Claude and OpenAI-family) reject numeric
    ``minimum``/``maximum`` and other constraint keywords, so the fields here stay
    constraint-free at the schema level; ``confidence`` is clamped to [0, 1] after
    the model responds, and ``rationale`` emptiness is checked in a validator.
    """

    model_config = _STRICT

    category: Category
    confidence: float
    rationale: str

    @field_validator("confidence", mode="after")
    @classmethod
    def _clamp_confidence(cls, v: float) -> float:
        return min(1.0, max(0.0, v))

    @field_validator("rationale", mode="after")
    @classmethod
    def _rationale_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("rationale must not be blank")
        return v


# --------------------------------------------------------------------------- #
# Resolver (README §4 — behaviour there; JSON shape proposed here)
# --------------------------------------------------------------------------- #
class ResolverInput(BaseModel):
    model_config = _STRICT

    request_id: str = Field(min_length=1)
    category: Category
    message_text: str = Field(min_length=1)


class Citation(BaseModel):
    model_config = _STRICT

    doc_id: str = Field(min_length=1)
    title: str | None = None
    snippet: str | None = None
    score: float | None = None


class ResolverOutput(BaseModel):
    model_config = _STRICT

    request_id: str = Field(min_length=1)
    status: ResolverStatus
    answer: str | None = Field(default=None, description="Required iff status == 'answered'")
    citations: list[Citation] = Field(default_factory=list)
    escalation_reason: EscalationReason | None = Field(
        default=None, description="Required iff status == 'escalated'"
    )

    @model_validator(mode="after")
    def _check_status_consistency(self) -> ResolverOutput:
        if self.status == "answered":
            if not self.answer:
                raise ValueError("answer is required when status == 'answered'")
        else:  # escalated
            if self.escalation_reason is None:
                raise ValueError("escalation_reason is required when status == 'escalated'")
        return self


# --------------------------------------------------------------------------- #
# Escalation state (README §6)
# --------------------------------------------------------------------------- #
class EscalationRecord(BaseModel):
    model_config = _STRICT

    request_id: str = Field(min_length=1)
    category: Category
    escalation_reason: EscalationReason
    status: EscalationStatus = "flagged"
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


# --------------------------------------------------------------------------- #
# Orchestrator result (new — top-level API response)
# --------------------------------------------------------------------------- #
class HelpdeskResult(BaseModel):
    model_config = _STRICT

    request_id: str = Field(min_length=1)
    outcome: Literal["answered", "escalated"]
    category: Category | None = None
    answer: str | None = None
    citations: list[Citation] = Field(default_factory=list)
    escalation_reason: EscalationReason | None = None
    classification: Classification | None = None

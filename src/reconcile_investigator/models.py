"""Structured domain and Pydantic models for the AI Exception Investigator."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, List, Optional
from pydantic import BaseModel, Field, field_validator, model_validator


class InvestigationClassification(str, Enum):
    """Categorisation of the financial discrepancy being investigated."""

    UNMATCHED_PAYMENT = "UNMATCHED_PAYMENT"
    AMBIGUOUS_MATCH = "AMBIGUOUS_MATCH"
    PARTIAL_PAYMENT = "PARTIAL_PAYMENT"
    SPLIT_PAYMENT = "SPLIT_PAYMENT"
    OVERPAYMENT = "OVERPAYMENT"
    DUPLICATE_PAYMENT = "DUPLICATE_PAYMENT"
    REFUND_DISCREPANCY = "REFUND_DISCREPANCY"
    REVERSAL_DISCREPANCY = "REVERSAL_DISCREPANCY"
    CURRENCY_MISMATCH = "CURRENCY_MISMATCH"
    UNEXPLAINED_BALANCE = "UNEXPLAINED_BALANCE"
    EXTERNAL_SETTLEMENT = "EXTERNAL_SETTLEMENT"
    MISSING_PROVIDER_EVIDENCE = "MISSING_PROVIDER_EVIDENCE"
    PROMPT_INJECTION_SUSPECT = "PROMPT_INJECTION_SUSPECT"
    UNKNOWN_EXCEPTION = "UNKNOWN_EXCEPTION"


class VerifierStatus(str, Enum):
    """Verdict of the independent safety verifier."""

    NOT_VERIFIED = "NOT_VERIFIED"
    VERIFIED = "VERIFIED"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    INVALID = "INVALID"
    UNVERIFIED = "UNVERIFIED"


class LikelihoodLevel(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class ImportanceLevel(str, Enum):
    CRITICAL = "CRITICAL"
    DESIRABLE = "DESIRABLE"
    OPTIONAL = "OPTIONAL"


class InvestigationTarget(BaseModel):
    """The subject of an investigation."""

    target_type: str = Field(..., description="Entity type: 'payment', 'proposal', 'invoice'")
    target_id: str = Field(..., description="Unique provider or internal identifier of the target")

    @field_validator("target_type")
    @classmethod
    def validate_target_type(cls, v: str) -> str:
        clean = v.strip().lower()
        if clean not in ("payment", "proposal", "invoice", "refund", "reversal"):
            raise ValueError(f"Invalid target_type '{v}'. Must be one of: payment, proposal, invoice, refund, reversal")
        return clean


class EvidenceReference(BaseModel):
    """A concrete, immutable evidence reference from authoritative financial records."""

    evidence_id: str = Field(..., description="Stable unique evidence ID (e.g. ev_pay_..., ev_inv_...)")
    source_type: str = Field(..., description="Source table or entity (payment, invoice, provider_record, settlement, refund, reversal, audit_event, proposal)")
    source_id: str = Field(..., description="Identifier in the source system/table")
    tenant_id: str = Field(..., description="Tenant identifier that owns this evidence")
    description: str = Field(..., description="Human-readable description of what this evidence proves")
    amount_cents: Optional[int] = Field(default=None, description="Monetary amount in integer minor units, if applicable")
    currency: Optional[str] = Field(default=None, description="3-letter ISO currency code, if applicable")
    provenance_hash: Optional[str] = Field(default=None, description="Cryptographic content or snapshot hash for tamper detection")
    metadata: dict[str, Any] = Field(default_factory=dict, description="Additional verified key-value attributes")

    @field_validator("currency")
    @classmethod
    def validate_currency(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            clean = v.strip().upper()
            if len(clean) != 3:
                raise ValueError(f"Currency must be 3-letter ISO code, got '{v}'")
            return clean
        return None


class VerifiedFact(BaseModel):
    """A financial fact strictly supported by authoritative records.
    
    Must cite at least one valid evidence reference ID. Cannot be speculative.
    """

    statement: str = Field(..., description="Factual claim grounded in authoritative records")
    evidence_ids: List[str] = Field(..., min_length=1, description="List of evidence IDs that prove this statement")
    amount_cents: Optional[int] = Field(default=None, description="Verified monetary amount in minor units")
    currency: Optional[str] = Field(default=None, description="Verified 3-letter ISO currency code")


class Hypothesis(BaseModel):
    """A plausible explanation for an exception that has not yet been proven as fact."""

    hypothesis_id: str = Field(..., description="Identifier for this hypothesis (e.g. hyp_1)")
    statement: str = Field(..., description="Plausible explanation of the discrepancy")
    likelihood: LikelihoodLevel = Field(default=LikelihoodLevel.MEDIUM, description="Assessment of likelihood")
    supporting_evidence_ids: List[str] = Field(default_factory=list, description="Evidence references supporting this hypothesis")
    counter_evidence_ids: List[str] = Field(default_factory=list, description="Evidence references arguing against this hypothesis")


class CandidateAnalysis(BaseModel):
    """Analysis of an invoice candidate considered during the investigation."""

    invoice_id: str = Field(..., description="Invoice provider ID or internal ID")
    invoice_number: str = Field(..., description="Human-facing invoice number")
    customer_match: bool = Field(default=False, description="Whether customer name or email matches payment")
    amount_match: bool = Field(default=False, description="Whether amount matches exactly or partially")
    score: Optional[int] = Field(default=None, ge=0, le=100, description="Match confidence score (0-100)")
    assessment: str = Field(..., description="Detailed rationale of why this candidate was or was not matched")


class ConflictingEvidenceItem(BaseModel):
    """Identified conflict between different pieces of financial evidence."""

    issue: str = Field(..., description="Description of the contradiction")
    evidence_ids: List[str] = Field(..., min_length=1, description="Evidence IDs exhibiting the contradiction")


class MissingEvidenceItem(BaseModel):
    """Information required to reach a conclusive resolution that is not currently present."""

    missing_item: str = Field(..., description="What information or record is missing")
    importance: ImportanceLevel = Field(default=ImportanceLevel.DESIRABLE, description="Criticality of missing information")
    why_needed: str = Field(..., description="Why this evidence is needed before action can be taken")


class RecommendedStep(BaseModel):
    """An investigation step recommended for a human operator."""

    step_number: int = Field(..., ge=1, description="Sequential ordering")
    action: str = Field(..., description="Action to perform (e.g. check customer ledger, request invoice)")
    target: str = Field(..., description="Who or what system to contact or inspect")
    reason: str = Field(..., description="Objective of this action")


class SuggestedResolution(BaseModel):
    """Proposed resolution for review and approval by an authorized operator.
    
    CRITICAL: The AI does not execute this resolution. It only recommends it.
    """

    resolution_type: str = Field(..., description="Category of suggested resolution")
    suggested_invoice_ids: List[str] = Field(default_factory=list, description="Invoices to allocate against, if applicable")
    suggested_amount_cents: Optional[int] = Field(default=None, description="Suggested allocation amount in cents")
    notes: str = Field(default="", description="Operator guidance for performing the resolution")


class InvestigationResult(BaseModel):
    """Complete, structured output of an exception investigation."""

    investigation_id: str = Field(..., description="Unique investigation identifier")
    tenant_id: str = Field(..., description="Tenant ID")
    target_type: str = Field(..., description="Entity type investigated ('payment', 'proposal', 'invoice')")
    target_id: str = Field(..., description="Target entity ID")
    classification: InvestigationClassification = Field(..., description="Identified discrepancy classification")
    executive_summary: str = Field(..., description="Concise summary for financial controllers and operators")
    verified_facts: List[VerifiedFact] = Field(default_factory=list, description="Authoritative verified facts")
    supporting_evidence: List[EvidenceReference] = Field(default_factory=list, description="Authoritative evidence references")
    candidate_invoice_analysis: List[CandidateAnalysis] = Field(default_factory=list, description="Evaluation of potential invoice candidates")
    conflicting_evidence: List[ConflictingEvidenceItem] = Field(default_factory=list, description="Contradictions discovered")
    missing_evidence: List[MissingEvidenceItem] = Field(default_factory=list, description="Missing pieces of evidence")
    hypotheses: List[Hypothesis] = Field(default_factory=list, description="Unproven plausible explanations")
    recommended_steps: List[RecommendedStep] = Field(default_factory=list, description="Investigation steps for operator")
    suggested_resolution: Optional[SuggestedResolution] = Field(default=None, description="Proposed resolution for human review")
    risk_flags: List[str] = Field(default_factory=list, description="Risk flags (e.g. PROMPT_INJECTION_DETECTED, CAPACITY_DEFICIT)")
    requires_human_review: bool = Field(default=True, description="Always true whenever financial mutation or ambiguity exists")
    verifier_status: VerifierStatus = Field(default=VerifierStatus.NOT_VERIFIED, description="Verdict from independent safety verifier")
    verifier_notes: List[str] = Field(default_factory=list, description="Notes from the independent safety verifier")
    model_info: dict[str, Any] = Field(default_factory=dict, description="Metadata regarding model, provider, prompt version")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), description="Timestamp investigation started")
    completed_at: Optional[datetime] = Field(default=None, description="Timestamp investigation completed")

    @model_validator(mode="after")
    def enforce_safety_invariants(self) -> InvestigationResult:
        # Non-negotiable: Any suggested financial resolution requires human review
        if self.suggested_resolution is not None and self.suggested_resolution.suggested_amount_cents:
            self.requires_human_review = True
        return self

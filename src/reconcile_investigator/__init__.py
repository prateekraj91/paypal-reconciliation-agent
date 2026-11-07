"""AI-Assisted Exception Investigator subsystem.

Provides evidence-grounded financial exception investigations, read-only tools,
structured Pydantic models, independent safety verifier, and local model orchestration.
"""

from .models import (
    InvestigationClassification,
    InvestigationTarget,
    EvidenceReference,
    VerifiedFact,
    Hypothesis,
    CandidateAnalysis,
    ConflictingEvidenceItem,
    MissingEvidenceItem,
    RecommendedStep,
    SuggestedResolution,
    InvestigationResult,
)
from .verifier import IndependentSafetyVerifier, SafetyVerificationResult, VerifierStatus
from .service import InvestigationService

__all__ = [
    "InvestigationClassification",
    "InvestigationTarget",
    "EvidenceReference",
    "VerifiedFact",
    "Hypothesis",
    "CandidateAnalysis",
    "ConflictingEvidenceItem",
    "MissingEvidenceItem",
    "RecommendedStep",
    "SuggestedResolution",
    "InvestigationResult",
    "IndependentSafetyVerifier",
    "SafetyVerificationResult",
    "VerifierStatus",
    "InvestigationService",
]

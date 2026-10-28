"""Framework-independent core of the PayPal reconciliation exception agent."""

from .baseline import BaselineConfig, BaselineMatcher
from .dataset import CaseCategory, Dataset, GroundTruth, Resolution, generate_dataset
from .interfaces import InvoiceSource, Matcher, PaymentSource, StaticSource
from .models import (
    AllocationStatus,
    Candidate,
    Evidence,
    EvidenceCode,
    Invoice,
    InvoiceStatus,
    MatchOutcome,
    MatchProposal,
    Payment,
    PaymentAllocation,
    PaymentStatus,
    ReconciliationStatus,
    outstanding_cents,
)
from .scoring import Scorecard, format_scorecard, score

__all__ = [n for n in dir() if not n.startswith("_")]

"""Framework-independent core of the PayPal reconciliation exception agent."""

from .baseline import BaselineConfig, BaselineMatcher
from .dataset import CaseCategory, Dataset, GroundTruth, Resolution, generate_dataset
from .deterministic import DeterministicMatcher
from .interfaces import (
    InvoiceAllocationSource,
    InvoiceSource,
    Matcher,
    PaymentSource,
    StaticSource,
)
from .models import (
    AccountingError,
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
    cross_check_invoice_balance,
    deduplicate_allocations,
    outstanding_cents,
)
from .scoring import Scorecard, format_scorecard, score
from .verifier import VerificationResult, verify_proposals
from .workflow import (
    ReconciliationReport,
    ReconciliationRunResult,
    reconcile_payments,
)

_PAYPAL_EXPORTS = {
    "PayPalAPIError",
    "PayPalAccountingError",
    "PayPalAuthError",
    "PayPalClient",
    "PayPalConfigError",
    "PayPalError",
    "PayPalNotFoundError",
    "PayPalTimeoutError",
}


def __getattr__(name: str):
    if name in _PAYPAL_EXPORTS:
        from . import paypal
        return getattr(paypal, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [n for n in dir() if not n.startswith("_")] + list(_PAYPAL_EXPORTS)

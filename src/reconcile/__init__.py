"""Framework-independent core of the PayPal reconciliation exception agent."""

from .advanced import AdvancedMatcher
from .ambiguity import build_ambiguity_certificate, create_ambiguous_proposal
from .baseline import BaselineConfig, BaselineMatcher
from .benchmark_v2 import (
    BenchmarkV2Dataset,
    BenchmarkV2Metrics,
    GroundTruthV2,
    ScenarioGroup,
    evaluate_matcher_v2,
    format_benchmark_v2_table,
    format_confusion_matrix,
    format_scenario_breakdown,
    generate_benchmark_v2,
)
from .dataset import CaseCategory, Dataset, GroundTruth, Resolution, generate_dataset
from .delivery import (
    IMMUTABLE_DELIVERY_FIELDS,
    PAYMENT_STATUS_TRANSITIONS,
    DeliveryResolution,
    PaymentConflictError,
    advances_to,
    canonical_payments,
    resolve_delivery,
    resolve_delivery_stream,
    resolve_payment_status,
)
from .deterministic import DeterministicMatcher
from .interfaces import (
    InvoiceAllocationSource,
    InvoiceSource,
    Matcher,
    PaymentSource,
    StaticSource,
)
from .matching import MatchRun
from .money import (
    MINOR_UNIT_EXPONENTS,
    UnsupportedCurrencyError,
    format_minor_units,
    minor_unit_exponent,
    normalize_currency,
    parse_money_to_minor_units,
)
from .models import (
    AccountingError,
    AllocationStatus,
    AmbiguityCandidateScore,
    AmbiguityCertificate,
    Candidate,
    Evidence,
    EvidenceCode,
    Invoice,
    InvoiceStatus,
    MatchOutcome,
    MatchProposal,
    Payment,
    PaymentAllocation,
    PaymentReversal,
    PaymentStatus,
    ReconciliationStatus,
    Refund,
    cross_check_invoice_balance,
    deduplicate_allocations,
    migrate_allocation_currency,
    outstanding_cents,
)
from .scoring import Scorecard, format_scorecard, score
from .sync import PayPalConnector, SyncReport, SyncResult, run_sync
from .verifier import VerificationResult, verify_proposals
from .workflow import (
    ReconciliationReport,
    ReconciliationRunResult,
    reconcile_payments,
)

_PAYPAL_EXPORTS = {
    "MAX_TRANSACTION_WINDOW_DAYS",
    "TRANSACTION_APPEARANCE_DELAY_HOURS",
    "TransactionIngest",
    "PayPalAPIError",
    "PayPalAccountingError",
    "PayPalAuthError",
    "PayPalClient",
    "PayPalConfigError",
    "PayPalError",
    "PayPalNotFoundError",
    "PayPalPaginationError",
    "PayPalTimeoutError",
    "sync_windows",
}


def __getattr__(name: str):
    if name in _PAYPAL_EXPORTS:
        from . import paypal
        return getattr(paypal, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [n for n in dir() if not n.startswith("_")] + list(_PAYPAL_EXPORTS)

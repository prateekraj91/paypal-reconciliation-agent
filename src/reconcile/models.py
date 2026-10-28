"""Core domain model for the reconciliation engine.

Standard library only. Nothing in the core may import FastAPI, SQLite, a UI
layer, or the PayPal SDK. Adapters live outside the core and translate their
data into these types.

Money is always integer minor units (cents). No floats anywhere near money.

Three states are kept deliberately separate:

* Payment status        (PaymentStatus)        what PayPal confirms about the payment.
* Allocation status     (AllocationStatus)     whether a proposed split of a payment
                                               across invoices has been approved.
* Reconciliation status (ReconciliationStatus) where a payment sits in OUR workflow.

A proposed match is never a completed financial event. Only APPROVED
allocations reduce an invoice's outstanding balance.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from typing import Iterable, Optional


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


# --------------------------------------------------------------------------
# State enums
# --------------------------------------------------------------------------


class InvoiceStatus(str, Enum):
    """Invoice state as reported by the billing system.

    A PayPal adapter maps PayPal's own status strings onto this smaller set.
    """

    SENT = "SENT"
    PARTIALLY_PAID = "PARTIALLY_PAID"
    PAID = "PAID"
    CANCELLED = "CANCELLED"

    @property
    def is_open(self) -> bool:
        return self in (InvoiceStatus.SENT, InvoiceStatus.PARTIALLY_PAID)


class PaymentStatus(str, Enum):
    """Payment state as confirmed by PayPal. Only COMPLETED can be matched."""

    COMPLETED = "COMPLETED"
    PENDING = "PENDING"
    REFUNDED = "REFUNDED"
    PARTIALLY_REFUNDED = "PARTIALLY_REFUNDED"
    REVERSED = "REVERSED"
    DENIED = "DENIED"


class AllocationStatus(str, Enum):
    """Whether a proposed split of a payment has been approved."""

    PROPOSED = "PROPOSED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class ReconciliationStatus(str, Enum):
    """Where a payment sits in our workflow.

    Matchers can only produce UNMATCHED, PROPOSED or NEEDS_REVIEW.
    RECONCILED is reserved for the future ledger: it requires an approved
    allocation AND a passing verification against PayPal.
    """

    UNMATCHED = "UNMATCHED"
    PROPOSED = "PROPOSED"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    RECONCILED = "RECONCILED"


class MatchOutcome(str, Enum):
    PROPOSED = "PROPOSED"  # exactly one candidate allocation
    AMBIGUOUS = "AMBIGUOUS"  # two or more plausible candidates, no winner
    NO_MATCH = "NO_MATCH"  # nothing proposed


class EvidenceCode(str, Enum):
    REFERENCE_MATCH = "REFERENCE_MATCH"
    REFERENCE_ABSENT = "REFERENCE_ABSENT"
    REFERENCE_UNKNOWN = "REFERENCE_UNKNOWN"
    AMOUNT_EXACT = "AMOUNT_EXACT"
    AMOUNT_MISMATCH = "AMOUNT_MISMATCH"
    CURRENCY_MISMATCH = "CURRENCY_MISMATCH"
    INVOICE_NOT_OPEN = "INVOICE_NOT_OPEN"
    INVOICE_ALREADY_CLAIMED = "INVOICE_ALREADY_CLAIMED"
    PAYMENT_NOT_COMPLETED = "PAYMENT_NOT_COMPLETED"
    DUPLICATE_EVENT = "DUPLICATE_EVENT"
    MULTIPLE_CANDIDATES = "MULTIPLE_CANDIDATES"
    NO_CANDIDATE = "NO_CANDIDATE"


# --------------------------------------------------------------------------
# Source records (what the outside world tells us)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Invoice:
    invoice_id: str  # system id (PayPal's id once the adapter exists)
    number: str  # human-facing number customers quote, e.g. "INV-1042"
    customer_name: str
    amount_cents: int  # total billed
    currency: str
    issued_on: date
    due_on: date
    status: InvoiceStatus = InvoiceStatus.SENT
    customer_email: Optional[str] = None
    memo: str = ""

    def __post_init__(self) -> None:
        _require(bool(self.invoice_id), "invoice_id is required")
        _require(bool(self.number), "invoice number is required")
        _require(self.amount_cents > 0, "invoice amount must be positive")
        _require(len(self.currency) == 3, "currency must be a 3-letter code")

    @property
    def is_open(self) -> bool:
        return self.status.is_open


@dataclass(frozen=True)
class Payment:
    payment_id: str  # transaction id; the idempotency key for dedupe
    amount_cents: int
    currency: str
    received_at: datetime
    payer_name: str
    payer_email: Optional[str] = None
    reference: Optional[str] = None  # structured reference field, if the payer gave one
    note: str = ""  # free text from the payer
    status: PaymentStatus = PaymentStatus.COMPLETED

    def __post_init__(self) -> None:
        _require(bool(self.payment_id), "payment_id is required")
        _require(self.amount_cents > 0, "payment amount must be positive")
        _require(len(self.currency) == 3, "currency must be a 3-letter code")


# --------------------------------------------------------------------------
# Matcher output (what we conclude)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class PaymentAllocation:
    """A slice of one payment assigned to one invoice."""

    payment_id: str
    invoice_id: str
    amount_cents: int
    status: AllocationStatus = AllocationStatus.PROPOSED

    def __post_init__(self) -> None:
        _require(self.amount_cents > 0, "allocation amount must be positive")


@dataclass(frozen=True)
class Evidence:
    """One machine-readable reason plus a human-readable explanation."""

    code: EvidenceCode
    detail: str = ""


@dataclass(frozen=True)
class Candidate:
    """One possible way to allocate a payment (a payment may span invoices)."""

    allocations: tuple[PaymentAllocation, ...]
    evidence: tuple[Evidence, ...] = ()

    def __post_init__(self) -> None:
        _require(len(self.allocations) > 0, "a candidate needs at least one allocation")
        _require(
            len({a.payment_id for a in self.allocations}) == 1,
            "all allocations in a candidate must belong to one payment",
        )
        _require(
            len({a.invoice_id for a in self.allocations}) == len(self.allocations),
            "a candidate may not allocate to the same invoice twice",
        )
        _require(
            all(a.status is AllocationStatus.PROPOSED for a in self.allocations),
            "matchers may only emit PROPOSED allocations",
        )

    @property
    def payment_id(self) -> str:
        return self.allocations[0].payment_id

    @property
    def total_cents(self) -> int:
        return sum(a.amount_cents for a in self.allocations)

    def has(self, code: EvidenceCode) -> bool:
        return any(e.code is code for e in self.evidence)


@dataclass(frozen=True)
class MatchProposal:
    """The matcher's answer for ONE delivered payment.

    This is a proposal, not a decision. Verification and human approval come
    later; nothing here changes any balance.
    """

    payment_id: str
    outcome: MatchOutcome
    candidates: tuple[Candidate, ...] = ()
    reasons: tuple[Evidence, ...] = ()  # payment-level evidence
    matcher: str = ""

    def __post_init__(self) -> None:
        n = len(self.candidates)
        if self.outcome is MatchOutcome.PROPOSED:
            _require(n == 1, "PROPOSED requires exactly one candidate")
        elif self.outcome is MatchOutcome.AMBIGUOUS:
            _require(n >= 2, "AMBIGUOUS requires at least two candidates")
        else:
            _require(n == 0, "NO_MATCH must not carry candidates")
        _require(
            all(c.payment_id == self.payment_id for c in self.candidates),
            "candidate belongs to a different payment",
        )

    @property
    def initial_status(self) -> ReconciliationStatus:
        return {
            MatchOutcome.PROPOSED: ReconciliationStatus.PROPOSED,
            MatchOutcome.AMBIGUOUS: ReconciliationStatus.NEEDS_REVIEW,
            MatchOutcome.NO_MATCH: ReconciliationStatus.UNMATCHED,
        }[self.outcome]


# --------------------------------------------------------------------------
# Ledger arithmetic
# --------------------------------------------------------------------------


def outstanding_cents(invoice: Invoice, allocations: Iterable[PaymentAllocation]) -> int:
    """Invoice total minus APPROVED allocations only.

    Proposed and rejected allocations never count. A negative result means the
    ledger over-allocated the invoice; callers should treat that as a bug, so
    it is deliberately not clamped to zero.
    """
    approved = sum(
        a.amount_cents
        for a in allocations
        if a.invoice_id == invoice.invoice_id and a.status is AllocationStatus.APPROVED
    )
    return invoice.amount_cents - approved

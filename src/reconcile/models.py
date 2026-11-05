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

from dataclasses import dataclass, replace
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

    DRAFT = "DRAFT"
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

    Matchers can produce UNMATCHED, PROPOSED, NEEDS_REVIEW, or EXTERNALLY_RECONCILED.
    RECONCILED is reserved for the future ledger: it requires an approved
    allocation AND a passing verification against PayPal.
    """

    UNMATCHED = "UNMATCHED"
    PROPOSED = "PROPOSED"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    RECONCILED = "RECONCILED"
    EXTERNALLY_RECONCILED = "EXTERNALLY_RECONCILED"


class MatchOutcome(str, Enum):
    PROPOSED = "PROPOSED"  # exactly one candidate allocation
    AMBIGUOUS = "AMBIGUOUS"  # two or more plausible candidates, no winner
    NO_MATCH = "NO_MATCH"  # nothing proposed
    PROVIDER_SETTLED = "PROVIDER_SETTLED"  # payment settled externally by billing provider


class EvidenceCode(str, Enum):
    REFERENCE_MATCH = "REFERENCE_MATCH"
    REFERENCE_ABSENT = "REFERENCE_ABSENT"
    REFERENCE_UNKNOWN = "REFERENCE_UNKNOWN"
    AMOUNT_EXACT = "AMOUNT_EXACT"
    AMOUNT_PARTIAL = "AMOUNT_PARTIAL"
    AMOUNT_MISMATCH = "AMOUNT_MISMATCH"
    CURRENCY_MISMATCH = "CURRENCY_MISMATCH"
    INVOICE_NOT_OPEN = "INVOICE_NOT_OPEN"
    INVOICE_ALREADY_CLAIMED = "INVOICE_ALREADY_CLAIMED"
    PAYMENT_NOT_COMPLETED = "PAYMENT_NOT_COMPLETED"
    DUPLICATE_EVENT = "DUPLICATE_EVENT"
    MULTIPLE_CANDIDATES = "MULTIPLE_CANDIDATES"
    NO_CANDIDATE = "NO_CANDIDATE"
    PROVIDER_SETTLED = "PROVIDER_SETTLED"


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
    due_amount_cents: Optional[int] = None

    def __post_init__(self) -> None:
        _require(bool(self.invoice_id), "invoice_id is required")
        _require(bool(self.number), "invoice number is required")
        _require(self.amount_cents > 0, "invoice amount must be positive")
        _require(len(self.currency) == 3, "currency must be a 3-letter code")
        if self.due_amount_cents is not None:
            _require(self.due_amount_cents >= 0, "due_amount_cents cannot be negative")
            _require(self.due_amount_cents <= self.amount_cents, "due_amount_cents cannot exceed total amount")

    @property
    def is_open(self) -> bool:
        return self.status.is_open

    @property
    def outstanding_balance_cents(self) -> int:
        """Provider-reported snapshot balance (due_amount_cents or gross amount).

        Represents the external billing provider's reported balance as of ingestion time.
        It does NOT represent our internal ledger-calculated balance.

        WARNING: This property must NEVER replace `outstanding_cents(invoice, allocations)`
        or `cross_check_invoice_balance(invoice, allocations)` in accounting calculations,
        matching, or verification, because it does not account for internal approved allocations.
        """
        return self.due_amount_cents if self.due_amount_cents is not None else self.amount_cents



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
    currency: Optional[str] = None

    def __post_init__(self) -> None:
        _require(self.amount_cents > 0, "allocation amount must be positive")
        if self.currency is not None:
            norm = self.currency.strip().upper()
            _require(len(norm) == 3, "currency must be a 3-letter code")
            object.__setattr__(self, "currency", norm)


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
            _require(n == 0, f"{self.outcome.value} must not carry candidates")
        _require(
            all(c.payment_id == self.payment_id for c in self.candidates),
            "candidate belongs to a different payment",
        )

    @property
    def initial_status(self) -> ReconciliationStatus:
        if self.outcome is MatchOutcome.PROVIDER_SETTLED or any(
            e.code is EvidenceCode.PROVIDER_SETTLED for e in self.reasons
        ):
            return ReconciliationStatus.EXTERNALLY_RECONCILED
        return {
            MatchOutcome.PROPOSED: ReconciliationStatus.PROPOSED,
            MatchOutcome.AMBIGUOUS: ReconciliationStatus.NEEDS_REVIEW,
            MatchOutcome.NO_MATCH: ReconciliationStatus.UNMATCHED,
        }[self.outcome]


# --------------------------------------------------------------------------
# Ledger arithmetic
# --------------------------------------------------------------------------


class AccountingError(ValueError):
    """Raised when ledger allocations or balance cross-checks violate accounting invariants."""


def deduplicate_allocations(
    allocations: Iterable[PaymentAllocation],
    *,
    error_cls: type[Exception] = AccountingError,
) -> list[PaymentAllocation]:
    """Deduplicate payment allocations using the compound key (payment_id, invoice_id).

    Preserves allocations with the same payment_id when they belong to different invoices
    (e.g. split or grouped payments).

    For duplicate compound keys:
    - If amount_cents and status agree, deduplicates safely.
    - If amount_cents or status conflicts, fails closed with error_cls (default AccountingError).
    """
    seen: dict[tuple[str, str], PaymentAllocation] = {}
    deduped: list[PaymentAllocation] = []

    for alloc in allocations:
        key = (alloc.payment_id, alloc.invoice_id)
        if key in seen:
            existing = seen[key]
            if existing.amount_cents != alloc.amount_cents:
                raise error_cls(
                    f"Conflicting allocation amount for payment {alloc.payment_id!r} "
                    f"on invoice {alloc.invoice_id!r}: {existing.amount_cents} cents != {alloc.amount_cents} cents"
                )
            if existing.status != alloc.status:
                raise error_cls(
                    f"Conflicting allocation status for payment {alloc.payment_id!r} "
                    f"on invoice {alloc.invoice_id!r}: {existing.status.value!r} != {alloc.status.value!r}"
                )
            if (
                existing.currency is not None
                and alloc.currency is not None
                and existing.currency.strip().upper() != alloc.currency.strip().upper()
            ):
                raise error_cls(
                    f"Conflicting allocation currency for payment {alloc.payment_id!r} "
                    f"on invoice {alloc.invoice_id!r}: {existing.currency!r} != {alloc.currency!r}"
                )
            continue
        seen[key] = alloc
        deduped.append(alloc)

    return deduped


def outstanding_cents(invoice: Invoice, allocations: Iterable[PaymentAllocation]) -> int:
    """Invoice total minus APPROVED allocations only.

    Proposed and rejected allocations never count. Repeated allocations for the
    same payment on this invoice are counted only once to prevent duplicate deductions.
    An APPROVED allocation must state its currency and it must match the invoice
    currency; a mismatch raises AccountingError, and so does a missing currency.
    A currency-less allocation is never assumed to be in the invoice's currency,
    because that assumption is how cross-currency money quietly goes missing.
    Legacy records without a currency are brought forward through the one
    explicit migration path, `migrate_allocation_currency`.
    A negative result means the ledger over-allocated the invoice; callers should treat that
    as a bug, so it is deliberately not clamped to zero.
    """
    seen_payments: dict[str, int] = {}
    approved = 0
    inv_curr = invoice.currency.strip().upper()
    for a in allocations:
        if a.invoice_id == invoice.invoice_id and a.status is AllocationStatus.APPROVED:
            if a.currency is None:
                raise AccountingError(
                    f"Missing currency for approved allocation {a.payment_id!r} on invoice "
                    f"{invoice.invoice_id!r}: a currency-less allocation cannot be proven to be "
                    f"in the invoice currency {inv_curr}. Migrate legacy records explicitly with "
                    f"migrate_allocation_currency()."
                )
            if a.currency.strip().upper() != inv_curr:
                raise AccountingError(
                    f"Currency mismatch for allocation {a.payment_id!r} on invoice {invoice.invoice_id!r}: "
                    f"allocation currency {a.currency.strip().upper()} does not match invoice currency {inv_curr}"
                )
            if a.payment_id in seen_payments:
                if seen_payments[a.payment_id] != a.amount_cents:
                    raise AccountingError(
                        f"Conflicting approved allocation amount for payment {a.payment_id!r} "
                        f"on invoice {invoice.invoice_id!r}: {seen_payments[a.payment_id]} cents != {a.amount_cents} cents"
                    )
                continue
            seen_payments[a.payment_id] = a.amount_cents
            approved += a.amount_cents
    return invoice.amount_cents - approved


def migrate_allocation_currency(
    allocations: Iterable[PaymentAllocation],
    invoices: Iterable[Invoice],
) -> list[PaymentAllocation]:
    """The one explicit migration path for legacy currency-less allocations.

    Stamps each currency-less allocation with the currency of the invoice it is
    recorded against, so the record states what it has always meant instead of
    leaving later readers to assume it. Nothing is defaulted or guessed:

    * an allocation whose invoice is not in `invoices` raises AccountingError,
    * an allocation whose own currency contradicts its invoice raises
      AccountingError rather than being overwritten,
    * two invoice records sharing an id but not a currency raise
      AccountingError, since neither can be trusted as the source.

    Allocations that already carry the right currency are returned unchanged.
    Call this at the boundary where legacy data enters, never inside an
    accounting calculation.
    """
    currency_by_invoice: dict[str, str] = {}
    for invoice in invoices:
        currency = invoice.currency.strip().upper()
        existing = currency_by_invoice.setdefault(invoice.invoice_id, currency)
        if existing != currency:
            raise AccountingError(
                f"Invoice {invoice.invoice_id!r} appears with conflicting currencies "
                f"{existing} and {currency}; cannot migrate allocations against it"
            )

    migrated: list[PaymentAllocation] = []
    for allocation in allocations:
        invoice_currency = currency_by_invoice.get(allocation.invoice_id)
        if invoice_currency is None:
            raise AccountingError(
                f"Cannot migrate allocation {allocation.payment_id!r}: invoice "
                f"{allocation.invoice_id!r} is unknown, so its currency cannot be established"
            )

        if allocation.currency is None:
            migrated.append(replace(allocation, currency=invoice_currency))
            continue

        if allocation.currency.strip().upper() != invoice_currency:
            raise AccountingError(
                f"Cannot migrate allocation {allocation.payment_id!r}: allocation currency "
                f"{allocation.currency} contradicts invoice currency {invoice_currency}"
            )

        migrated.append(allocation)

    return migrated


def cross_check_invoice_balance(invoice: Invoice, allocations: Iterable[PaymentAllocation]) -> int:
    """Calculate ledger balance and verify it agrees with PayPal due_amount.

    Treats PayPal due_amount as an independent cross-check against the ledger balance.
    Fails closed (raises AccountingError) if PayPal due_amount contradicts the calculated balance.
    """
    calculated = outstanding_cents(invoice, allocations)
    if invoice.due_amount_cents is not None and invoice.due_amount_cents != calculated:
        raise AccountingError(
            f"Accounting inconsistency for invoice {invoice.number}: "
            f"calculated ledger balance {calculated} cents does not match "
            f"PayPal due_amount {invoice.due_amount_cents} cents"
        )
    return calculated

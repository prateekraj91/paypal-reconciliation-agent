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
from typing import Any, Iterable, Optional, Sequence


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
    PAYMENT_RETURNED = "PAYMENT_RETURNED"
    CURRENCY_MISMATCH = "CURRENCY_MISMATCH"
    INVOICE_NOT_OPEN = "INVOICE_NOT_OPEN"
    INVOICE_ALREADY_CLAIMED = "INVOICE_ALREADY_CLAIMED"
    PAYMENT_NOT_COMPLETED = "PAYMENT_NOT_COMPLETED"
    DUPLICATE_EVENT = "DUPLICATE_EVENT"
    MULTIPLE_CANDIDATES = "MULTIPLE_CANDIDATES"
    NO_CANDIDATE = "NO_CANDIDATE"
    PROVIDER_SETTLED = "PROVIDER_SETTLED"
    CONTEXTUAL_MATCH = "CONTEXTUAL_MATCH"
    CUSTOMER_NAME_MATCH = "CUSTOMER_NAME_MATCH"
    CUSTOMER_EMAIL_MATCH = "CUSTOMER_EMAIL_MATCH"
    FUZZY_REFERENCE_MATCH = "FUZZY_REFERENCE_MATCH"
    TEMPORAL_PROXIMITY_MATCH = "TEMPORAL_PROXIMITY_MATCH"
    AMOUNT_OVERPAYMENT = "AMOUNT_OVERPAYMENT"
    AMBIGUOUS_CANDIDATES = "AMBIGUOUS_CANDIDATES"
    REFUND_PROCESSED = "REFUND_PROCESSED"
    REVERSAL_PROCESSED = "REVERSAL_PROCESSED"


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


@dataclass(frozen=True)
class Refund:
    """External merchant-initiated refund linked to a parent payment."""

    refund_id: str
    parent_payment_id: str
    amount_cents: int
    currency: str
    refunded_at: datetime
    status: str = "COMPLETED"
    reason: str = ""
    invoice_id: Optional[str] = None
    target_type: str = "UNKNOWN"  # "SETTLEMENT", "ALLOCATION", "UNKNOWN"
    settlement_id: Optional[str] = None
    allocation_id: Optional[str] = None

    def __post_init__(self) -> None:
        _require(bool(self.refund_id), "refund_id is required")
        _require(bool(self.parent_payment_id), "parent_payment_id is required")
        _require(self.amount_cents > 0, "refund amount must be positive")
        _require(len(self.currency) == 3, "currency must be a 3-letter code")


@dataclass(frozen=True)
class PaymentReversal:
    """Involuntary processor/network reversal (e.g. chargeback, ACH return, dispute clawback)."""

    reversal_id: str
    parent_payment_id: str
    amount_cents: int
    currency: str
    reversed_at: datetime
    status: str = "COMPLETED"
    reason: str = ""
    dispute_id: Optional[str] = None
    invoice_id: Optional[str] = None
    target_type: str = "UNKNOWN"  # "SETTLEMENT", "ALLOCATION", "UNKNOWN"
    settlement_id: Optional[str] = None
    allocation_id: Optional[str] = None

    def __post_init__(self) -> None:
        _require(bool(self.reversal_id), "reversal_id is required")
        _require(bool(self.parent_payment_id), "parent_payment_id is required")
        _require(self.amount_cents > 0, "reversal amount must be positive")
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
class AmbiguityCandidateScore:
    invoice_id: str
    invoice_number: str
    score: int
    matched_signals: tuple[str, ...] = ()
    unmatched_signals: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "invoice_id": self.invoice_id,
            "invoice_number": self.invoice_number,
            "score": int(round(self.score)),
            "matched_signals": list(self.matched_signals),
            "unmatched_signals": list(self.unmatched_signals),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AmbiguityCandidateScore":
        return cls(
            invoice_id=data["invoice_id"],
            invoice_number=data["invoice_number"],
            score=int(round(data["score"])),
            matched_signals=tuple(data.get("matched_signals", ())),
            unmatched_signals=tuple(data.get("unmatched_signals", ())),
        )


@dataclass(frozen=True)
class AmbiguityCertificate:
    payment_id: str
    candidate_invoice_ids: tuple[str, ...]
    candidate_scores: dict[str, int]
    supporting_evidence: dict[str, tuple[Evidence, ...]]
    conflicting_evidence: tuple[Evidence, ...] = ()
    missing_evidence: tuple[str, ...] = ()
    rejection_reasons: tuple[str, ...] = ()
    recommended_steps: tuple[str, ...] = ()
    requires_human_approval: bool = True
    classification: str = "AMBIGUOUS"

    def to_dict(self) -> dict[str, Any]:
        return {
            "payment_id": self.payment_id,
            "candidate_invoice_ids": list(self.candidate_invoice_ids),
            "candidate_scores": {k: int(round(v)) for k, v in sorted(self.candidate_scores.items())},
            "supporting_evidence": {
                k: [{"code": e.code.value, "detail": e.detail} for e in ev_list]
                for k, ev_list in sorted(self.supporting_evidence.items())
            },
            "conflicting_evidence": [{"code": e.code.value, "detail": e.detail} for e in self.conflicting_evidence],
            "missing_evidence": list(self.missing_evidence),
            "rejection_reasons": list(self.rejection_reasons),
            "recommended_steps": list(self.recommended_steps),
            "requires_human_approval": self.requires_human_approval,
            "classification": self.classification,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AmbiguityCertificate":
        supporting = {}
        for k, v in data.get("supporting_evidence", {}).items():
            supporting[k] = tuple(Evidence(code=EvidenceCode(e["code"]), detail=e.get("detail", "")) for e in v)
        conflicting = tuple(
            Evidence(code=EvidenceCode(e["code"]), detail=e.get("detail", ""))
            for e in data.get("conflicting_evidence", [])
        )
        return cls(
            payment_id=data["payment_id"],
            candidate_invoice_ids=tuple(data.get("candidate_invoice_ids", ())),
            candidate_scores={k: int(round(v)) for k, v in data.get("candidate_scores", {}).items()},
            supporting_evidence=supporting,
            conflicting_evidence=conflicting,
            missing_evidence=tuple(data.get("missing_evidence", ())),
            rejection_reasons=tuple(data.get("rejection_reasons", ())),
            recommended_steps=tuple(data.get("recommended_steps", ())),
            requires_human_approval=data.get("requires_human_approval", True),
            classification=data.get("classification", "AMBIGUOUS"),
        )


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
    ambiguity_certificate: Optional[AmbiguityCertificate] = None
    excess_amount_cents: int = 0

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
        _require(self.excess_amount_cents >= 0, "excess_amount_cents cannot be negative")

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


def outstanding_cents(
    invoice: Invoice,
    allocations: Iterable[PaymentAllocation],
    refunds: Iterable[Refund] = (),
    reversals: Iterable[PaymentReversal] = (),
) -> int:
    """Invoice total minus APPROVED allocations only, plus approved refunds and reversals.

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

    Approved refunds and reversals linked to approved payments on this invoice restore outstanding balance.
    Refunds and reversals cannot exceed the approved payment amount on the invoice. Duplicate refunds/reversals
    with matching amounts are deduplicated safely; conflicting amounts raise AccountingError.
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

    refunded = 0
    seen_refunds: dict[str, int] = {}
    for r in refunds:
        if r.status.upper() == "COMPLETED" and (r.invoice_id is None or r.invoice_id == invoice.invoice_id):
            if r.parent_payment_id in seen_payments:
                if r.currency.strip().upper() != inv_curr:
                    raise AccountingError(
                        f"Currency mismatch for refund {r.refund_id!r}: {r.currency} vs invoice {inv_curr}"
                    )
                if r.refund_id in seen_refunds:
                    if seen_refunds[r.refund_id] != r.amount_cents:
                        raise AccountingError(
                            f"Conflicting refund amount for {r.refund_id!r}: {seen_refunds[r.refund_id]} cents != {r.amount_cents} cents"
                        )
                    continue
                seen_refunds[r.refund_id] = r.amount_cents
                refunded += r.amount_cents

    reversed_amt = 0
    seen_reversals: dict[str, int] = {}
    for rev in reversals:
        if rev.status.upper() == "COMPLETED" and (rev.invoice_id is None or rev.invoice_id == invoice.invoice_id):
            if rev.parent_payment_id in seen_payments:
                if rev.currency.strip().upper() != inv_curr:
                    raise AccountingError(
                        f"Currency mismatch for reversal {rev.reversal_id!r}: {rev.currency} vs invoice {inv_curr}"
                    )
                if rev.reversal_id in seen_reversals:
                    if seen_reversals[rev.reversal_id] != rev.amount_cents:
                        raise AccountingError(
                            f"Conflicting reversal amount for {rev.reversal_id!r}: {seen_reversals[rev.reversal_id]} cents != {rev.amount_cents} cents"
                        )
                    continue
                seen_reversals[rev.reversal_id] = rev.amount_cents
                reversed_amt += rev.amount_cents

    total_returns = refunded + reversed_amt
    if total_returns > approved:
        raise AccountingError(
            f"Total returns ({total_returns} cents: {refunded} refunds, {reversed_amt} reversals) "
            f"exceed approved allocations ({approved} cents) on invoice {invoice.number}"
        )

    net_deductions = approved - total_returns
    return invoice.amount_cents - net_deductions


def payment_net_cents(
    payment_id: str,
    gross_cents: int,
    refunds: Iterable[Refund] = (),
    reversals: Iterable[PaymentReversal] = (),
) -> int:
    """Gross payment less its completed refunds and reversals, never below zero.

    A repeated return ID is counted once. Callers with a persisted ledger should
    prefer its figure, which also accounts for applied money.
    """
    returned: dict[str, int] = {}
    for r in refunds:
        if r.parent_payment_id == payment_id and r.status.upper() == "COMPLETED":
            returned[f"refund:{r.refund_id}"] = r.amount_cents
    for rev in reversals:
        if rev.parent_payment_id == payment_id and rev.status.upper() == "COMPLETED":
            returned[f"reversal:{rev.reversal_id}"] = rev.amount_cents
    return max(0, gross_cents - sum(returned.values()))


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


def cross_check_invoice_balance(
    invoice: Invoice,
    allocations: Iterable[PaymentAllocation],
    refunds: Iterable[Refund] = (),
    reversals: Iterable[PaymentReversal] = (),
) -> int:
    """Calculate ledger balance and verify it agrees with PayPal due_amount.

    Treats PayPal due_amount as an independent cross-check against the ledger balance.
    Fails closed (raises AccountingError) if PayPal due_amount contradicts the calculated balance.
    """
    calculated = outstanding_cents(invoice, allocations, refunds=refunds, reversals=reversals)
    if invoice.due_amount_cents is not None and invoice.due_amount_cents != calculated:
        raise AccountingError(
            f"Accounting inconsistency for invoice {invoice.number}: "
            f"calculated ledger balance {calculated} cents does not match "
            f"PayPal due_amount {invoice.due_amount_cents} cents"
        )
    return calculated

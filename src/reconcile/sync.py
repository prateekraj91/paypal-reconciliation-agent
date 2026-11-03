"""One repeatable synchronisation pass over a PayPal account.

A sync fetches every invoice and every transaction, maps both into canonical
records, runs the existing matcher and the existing independent verifier, and
checks the accounting invariants. It changes nothing: no allocation is
approved, no balance moves. The existing reconciliation engine is reused
whole -- this module is composition, not new matching logic.

The property that matters is that a second run does nothing new. Payments
already approved in the ledger are excluded from matching, transactions are
deduplicated by their canonical PayPal id, and the proposals are a pure
function of the fetched state. A sync that double-counted on its second run
would be worse than one that failed, because the ledger would drift quietly.

Standard library only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Optional, Protocol, Sequence

from .delivery import canonical_payments
from .deterministic import DeterministicMatcher
from .interfaces import Matcher
from .models import (
    AccountingError,
    AllocationStatus,
    Invoice,
    MatchOutcome,
    MatchProposal,
    Payment,
    PaymentAllocation,
    cross_check_invoice_balance,
    deduplicate_allocations,
)
from .verifier import VerificationResult, verify_proposals


class PayPalConnector(Protocol):
    """What a sync needs from an adapter. PayPalClient satisfies it."""

    base_url: str

    def list_all_invoices_with_allocations(
        self, **kwargs: Any
    ) -> tuple[Sequence[Invoice], Sequence[PaymentAllocation]]: ...

    def list_transactions(self, start: datetime, end: datetime, **kwargs: Any) -> Any: ...


@dataclass(frozen=True)
class SyncReport:
    """Counts and classified problems from one sync. Safe to print."""

    window_start: datetime
    window_end: datetime
    invoices_ingested: int = 0
    transactions_ingested: int = 0
    payments_ingested: int = 0
    payments_skipped: int = 0
    already_in_ledger: int = 0
    proposals: int = 0
    passed_verification: int = 0
    rejected_verification: int = 0
    needs_review: int = 0
    unmatched: int = 0
    transaction_search_available: bool = True
    transaction_search_error: Optional[str] = None
    accounting_errors: tuple[str, ...] = ()

    def format(self) -> str:
        lines = [
            f"window              : {self.window_start.isoformat()}"
            f" .. {self.window_end.isoformat()}",
            f"invoices ingested   : {self.invoices_ingested}",
            f"ledger allocations  : {self.already_in_ledger} already approved",
            f"transactions seen   : {self.transactions_ingested}",
            f"payments ingested   : {self.payments_ingested}"
            f" ({self.payments_skipped} not payments received)",
            f"proposals           : {self.proposals}",
            f"passed verification : {self.passed_verification}",
            f"rejected for review : {self.rejected_verification}",
            f"needs review        : {self.needs_review}",
            f"unmatched           : {self.unmatched}",
        ]
        if not self.transaction_search_available:
            lines.append(
                f"Transaction Search  : UNAVAILABLE -> "
                f"{self.transaction_search_error}"
            )
            lines.append(
                "                      fell back to invoice-embedded payments"
            )
        if self.accounting_errors:
            lines.append(f"accounting errors   : {len(self.accounting_errors)}")
            for message in self.accounting_errors:
                lines.append(f"  - {message}")
        else:
            lines.append("accounting errors   : none")
        return "\n".join(lines)


@dataclass(frozen=True)
class SyncResult:
    """Everything one sync produced, including its source evidence."""

    report: SyncReport
    invoices: tuple[Invoice, ...] = ()
    payments: tuple[Payment, ...] = ()
    ledger_allocations: tuple[PaymentAllocation, ...] = ()
    proposals: tuple[MatchProposal, ...] = ()
    verification: tuple[VerificationResult, ...] = ()
    transaction_evidence: Mapping[str, dict] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """True when nothing needs a human before this run can be trusted."""
        return not self.report.accounting_errors

    @property
    def proposed_allocations(self) -> tuple[PaymentAllocation, ...]:
        """Every allocation the matcher proposed. All still PROPOSED."""
        return tuple(
            allocation
            for proposal in self.proposals
            if proposal.outcome is MatchOutcome.PROPOSED
            for candidate in proposal.candidates
            for allocation in candidate.allocations
        )

    @property
    def verified_allocations(self) -> tuple[PaymentAllocation, ...]:
        """Allocations whose proposal also passed independent verification."""
        passed = {r.payment_id for r in self.verification if r.passed}
        return tuple(a for a in self.proposed_allocations if a.payment_id in passed)


def run_sync(
    connector: PayPalConnector,
    start: datetime,
    end: datetime,
    matcher: Optional[Matcher] = None,
    page_size: Optional[int] = None,
) -> SyncResult:
    """Fetch, map, match and verify one window. Approves nothing.

    An invoice fetch that fails is fatal: a partial invoice list makes every
    payment against a missing invoice look unmatched, which reads as a clean
    answer rather than a missing one.

    Transaction Search failing is not fatal. Payments PayPal has already linked
    to invoices are still available from the invoices themselves, and an
    externally recorded payment only ever appears there. The report says plainly
    that the richer source was unavailable rather than implying a complete run.
    """
    kwargs: dict[str, Any] = {}
    if page_size is not None:
        kwargs["page_size"] = page_size
    raw_invoices, raw_allocations = connector.list_all_invoices_with_allocations(**kwargs)
    invoices = tuple(raw_invoices)

    # Only APPROVED allocations move a balance, and each is counted once.
    ledger = tuple(
        allocation
        for allocation in deduplicate_allocations(
            list(raw_allocations), error_cls=AccountingError
        )
        if allocation.status is AllocationStatus.APPROVED
    )

    search_available = True
    search_error: Optional[str] = None
    payments: tuple[Payment, ...] = ()
    evidence: dict[str, dict] = {}
    transactions_seen = 0
    skipped = 0

    try:
        ingest = connector.list_transactions(start, end)
    except Exception as exc:  # classified, not swallowed: see the report
        search_available = False
        search_error = f"{type(exc).__name__}: {exc}"
    else:
        payments = canonical_payments(ingest.payments)
        evidence = dict(ingest.raw_by_id)
        transactions_seen = len(evidence)
        skipped = len(ingest.skipped)

    # A payment already approved against an invoice is settled. Re-proposing it
    # is exactly the double-count a second run must not produce.
    settled = {a.payment_id for a in ledger}
    open_payments = tuple(p for p in payments if p.payment_id not in settled)

    accounting_errors: list[str] = []
    for invoice in invoices:
        try:
            cross_check_invoice_balance(invoice, ledger)
        except AccountingError as exc:
            accounting_errors.append(f"{invoice.number}: {exc}")

    proposals: tuple[MatchProposal, ...] = ()
    verification: tuple[VerificationResult, ...] = ()
    if not accounting_errors:
        active = matcher or DeterministicMatcher()
        proposals = tuple(active.match(open_payments, invoices, approved=ledger))
        proposed = [p for p in proposals if p.outcome is MatchOutcome.PROPOSED]
        verification = tuple(
            verify_proposals(
                proposed, open_payments, invoices, existing_allocations=ledger
            )
        )

    passed = sum(1 for r in verification if r.passed)
    report = SyncReport(
        window_start=start,
        window_end=end,
        invoices_ingested=len(invoices),
        transactions_ingested=transactions_seen,
        payments_ingested=len(payments),
        payments_skipped=skipped,
        already_in_ledger=len(ledger),
        proposals=sum(1 for p in proposals if p.outcome is MatchOutcome.PROPOSED),
        passed_verification=passed,
        rejected_verification=len(verification) - passed,
        needs_review=sum(1 for p in proposals if p.outcome is MatchOutcome.AMBIGUOUS),
        unmatched=sum(1 for p in proposals if p.outcome is MatchOutcome.NO_MATCH),
        transaction_search_available=search_available,
        transaction_search_error=search_error,
        accounting_errors=tuple(accounting_errors),
    )

    return SyncResult(
        report=report,
        invoices=invoices,
        payments=payments,
        ledger_allocations=ledger,
        proposals=proposals,
        verification=verification,
        transaction_evidence=evidence,
    )

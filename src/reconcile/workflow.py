"""End-to-end reconciliation workflow integrating ingestion, matching, and verification."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Sequence

from .deterministic import DeterministicMatcher
from .interfaces import InvoiceSource, Matcher
from .models import (
    AllocationStatus,
    Invoice,
    MatchOutcome,
    MatchProposal,
    Payment,
    PaymentAllocation,
    PaymentReversal,
    Refund,
    deduplicate_allocations,
)
from .verifier import VerificationResult, verify_proposals


@dataclass(frozen=True)
class ReconciliationReport:
    """Consolidated summary of a reconciliation run."""

    total_payments: int
    total_invoices: int
    initial_approved_allocations: int
    proposals_count: int
    passed_verification_count: int
    rejected_verification_count: int
    review_queue_count: int


@dataclass(frozen=True)
class ReconciliationRunResult:
    """Outcome of a complete reconciliation pass across ingestion, matching, and verification."""

    invoices: tuple[Invoice, ...]
    existing_allocations: tuple[PaymentAllocation, ...]
    proposals: tuple[MatchProposal, ...]
    verification_results: tuple[VerificationResult, ...]

    @property
    def proposed(self) -> tuple[MatchProposal, ...]:
        return tuple(p for p in self.proposals if p.outcome is MatchOutcome.PROPOSED)

    @property
    def passed_verification(self) -> tuple[MatchProposal, ...]:
        passed_ids = {r.payment_id for r in self.verification_results if r.passed}
        return tuple(p for p in self.proposed if p.payment_id in passed_ids)

    @property
    def rejected_for_review(self) -> tuple[MatchProposal, ...]:
        rejected_ids = {r.payment_id for r in self.verification_results if not r.passed}
        return tuple(p for p in self.proposed if p.payment_id in rejected_ids)

    @property
    def report(self) -> ReconciliationReport:
        passed_count = len(self.passed_verification)
        rejected_count = len(self.rejected_for_review)
        non_proposed_count = len(self.proposals) - len(self.proposed)
        return ReconciliationReport(
            total_payments=len(self.proposals),
            total_invoices=len(self.invoices),
            initial_approved_allocations=len(self.existing_allocations),
            proposals_count=len(self.proposed),
            passed_verification_count=passed_count,
            rejected_verification_count=rejected_count,
            review_queue_count=rejected_count + non_proposed_count,
        )


def reconcile_payments(
    payments: Sequence[Payment],
    invoice_source: InvoiceSource | Any,
    matcher: Optional[Matcher] = None,
    existing_allocations: Optional[Sequence[PaymentAllocation]] = None,
    provider_settlements: Optional[Sequence[PaymentAllocation]] = None,
    refunds: Optional[Sequence[Refund]] = None,
    reversals: Optional[Sequence[PaymentReversal]] = None,
) -> ReconciliationRunResult:
    """Execute end-to-end reconciliation for a batch of payments against an invoice source.

    1. Ingestion:
       If `invoice_source` provides `list_invoices_with_allocations()`, retrieves
       both invoices and provider-linked allocations.
       Otherwise falls back to `list_invoices()`.

    2. Allocation Deduplication & Preservation:
       Authoritative approved allocations from ingestion and `existing_allocations`
       are combined and deduplicated by payment_id to prevent double deductions.

    3. Matching:
       Runs `matcher.match(payments, invoices, approved=approved_allocations, settlements=settled_allocations)`.
       Defaults to DeterministicMatcher() if no matcher is supplied.

    4. Independent Verification:
       Validates proposed allocations against payments, invoices, approved allocations,
       provider settlements, refunds, and reversals using `verify_proposals()`.

    Returns a ReconciliationRunResult containing invoices, ledger allocations,
    proposals, and verification results.
    """
    # 1. Ingestion
    ingested_invoices: Sequence[Invoice]
    ingested_allocs: Sequence[PaymentAllocation] = ()

    if isinstance(invoice_source, (list, tuple)):
        ingested_invoices = invoice_source
    elif hasattr(invoice_source, "list_invoices_with_allocations") and callable(
        invoice_source.list_invoices_with_allocations
    ):
        ingested_invoices, ingested_allocs = (
            invoice_source.list_invoices_with_allocations()
        )
    elif hasattr(invoice_source, "list_invoices") and callable(
        invoice_source.list_invoices
    ):
        ingested_invoices = invoice_source.list_invoices()
    else:
        raise TypeError(
            f"Invalid invoice_source: {type(invoice_source).__name__} does not "
            f"implement list_invoices or list_invoices_with_allocations"
        )

    # 2. Combine and deduplicate approved allocations
    all_allocs = list(ingested_allocs) + list(existing_allocations or ())
    deduped_allocs = deduplicate_allocations(all_allocs)
    combined_allocs: list[PaymentAllocation] = [
        alloc for alloc in deduped_allocs if alloc.status is AllocationStatus.APPROVED
    ]
    settled_allocs: Sequence[PaymentAllocation] = tuple(provider_settlements or ())

    # 3. Matcher execution
    active_matcher = matcher or DeterministicMatcher()
    import inspect
    matcher_sig = inspect.signature(active_matcher.match)
    match_kwargs: dict[str, Any] = {"approved": combined_allocs}
    if "settlements" in matcher_sig.parameters:
        match_kwargs["settlements"] = settled_allocs
    if "refunds" in matcher_sig.parameters and refunds is not None:
        match_kwargs["refunds"] = refunds
    if "reversals" in matcher_sig.parameters and reversals is not None:
        match_kwargs["reversals"] = reversals
    proposals = active_matcher.match(payments, ingested_invoices, **match_kwargs)

    # 4. Independent verification
    proposed_only = [p for p in proposals if p.outcome is MatchOutcome.PROPOSED]
    results = verify_proposals(
        proposed_only,
        payments,
        ingested_invoices,
        existing_allocations=combined_allocs + list(settled_allocs),
        refunds=refunds or (),
        reversals=reversals or (),
    )

    return ReconciliationRunResult(
        invoices=tuple(ingested_invoices),
        existing_allocations=tuple(combined_allocs),
        proposals=tuple(proposals),
        verification_results=tuple(results),
    )

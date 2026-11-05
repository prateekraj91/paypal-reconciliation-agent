"""Independent safety checks for proposed reconciliation allocations.

Passing verification is not approval. No ledger state or allocation status
is changed by this module.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Sequence

from .delivery import resolve_delivery_stream
from .models import (
    AllocationStatus,
    EvidenceCode,
    Invoice,
    MatchOutcome,
    MatchProposal,
    Payment,
    PaymentAllocation,
    PaymentStatus,
    cross_check_invoice_balance,
    outstanding_cents,
)
from .normalize import canonical_reference, extract_invoice_references


@dataclass(frozen=True)
class VerificationResult:
    """Result of checking one proposal, without approving it."""

    payment_id: str
    passed: bool
    issues: tuple[str, ...] = ()


def verify_proposals(
    proposals: Sequence[MatchProposal],
    payments: Sequence[Payment],
    invoices: Sequence[Invoice],
    existing_allocations: Sequence[PaymentAllocation] = (),
) -> list[VerificationResult]:
    """Verify proposed allocations against payment and invoice records.

    Results are returned in the same order as `proposals`.

    `payments` is the delivery stream and may repeat a payment_id (webhook
    retries, and PENDING followed by COMPLETED). It is resolved to one
    canonical record per payment before anything is checked; deliveries that
    contradict each other are reported as issues rather than matched.

    `existing_allocations` represents ledger allocations. Only APPROVED
    allocations reduce outstanding balances. Conflicting proposals in this
    batch are all rejected if, together, they would exceed invoice capacity.
    """
    deliveries = resolve_delivery_stream(payments)
    proposal_counts = Counter(p.payment_id for p in proposals)
    invoices_by_id: dict[str, list[Invoice]] = defaultdict(list)

    invoices_by_number: dict[str, list[Invoice]] = defaultdict(list)

    for invoice in invoices:
        invoices_by_id[invoice.invoice_id].append(invoice)
        normalized_number = canonical_reference(invoice.number)
        if normalized_number:
            invoices_by_number[normalized_number].append(invoice)

    approved_allocations = [
        allocation
        for allocation in existing_allocations
        if allocation.status is AllocationStatus.APPROVED
    ]

    issues_by_index: list[list[str]] = [[] for _ in proposals]
    valid_candidates: dict[int, tuple[MatchProposal, Payment]] = {}
    verified_invoice_balances: dict[str, int] = {}

    # Pass 1: validate each proposal independently.
    for index, proposal in enumerate(proposals):
        issues = issues_by_index[index]

        if proposal_counts[proposal.payment_id] > 1:
            issues.append(
                "duplicate proposals for the same payment ID are not allowed"
            )

        if proposal.outcome is not MatchOutcome.PROPOSED:
            issues.append("only PROPOSED outcomes can pass verification")
            continue

        if len(proposal.candidates) != 1:
            issues.append("a proposal must contain exactly one candidate")
            continue

        conflict = deliveries.conflict_reason(proposal.payment_id)
        if conflict is not None:
            issues.append(
                f"conflicting deliveries for payment ID "
                f"{proposal.payment_id!r}: {conflict}"
            )
            continue

        payment = deliveries.get(proposal.payment_id)
        if payment is None:
            issues.append("payment record is missing")
            continue

        if payment.status is not PaymentStatus.COMPLETED:
            issues.append("payment status must be COMPLETED")

        if any(
            allocation.payment_id == payment.payment_id
            for allocation in approved_allocations
        ):
            issues.append("payment is already allocated in the ledger")

        candidate = proposal.candidates[0]

        # Independently derive references from the source records.
        actual_references = {
            canonical_reference(number)
            for number in extract_invoice_references(
                [payment.reference, payment.note],
                [invoice.number for invoice in invoices],
            )
        }

        # Do not let exact amounts alone pass verification.
        is_contextual = candidate.has(EvidenceCode.CONTEXTUAL_MATCH)
        if not candidate.has(EvidenceCode.REFERENCE_MATCH) and not is_contextual:
            issues.append(
                "trusted invoice reference evidence is required; "
                "amount-only proposals cannot pass"
            )

        if candidate.payment_id != payment.payment_id:
            issues.append("candidate payment ID does not match payment record")

        excess = getattr(proposal, "excess_amount_cents", 0)
        if excess > 0:
            if candidate.total_cents + excess != payment.amount_cents:
                issues.append(
                    f"candidate allocation total {candidate.total_cents} cents plus excess {excess} cents "
                    f"does not equal payment amount {payment.amount_cents} cents"
                )
        elif candidate.total_cents != payment.amount_cents:
            issues.append(
                f"candidate allocation total {candidate.total_cents} cents "
                f"does not equal payment amount {payment.amount_cents} cents"
            )

        if not candidate.allocations:
            issues.append("candidate contains no allocations")

        seen_invoice_ids: set[str] = set()

        for allocation in candidate.allocations:
            if allocation.status is not AllocationStatus.PROPOSED:
                issues.append("candidate allocations must remain PROPOSED")

            if allocation.payment_id != payment.payment_id:
                issues.append("allocation payment ID does not match payment")

            if allocation.amount_cents <= 0:
                issues.append("allocation amount must be positive")

            if allocation.invoice_id in seen_invoice_ids:
                issues.append("candidate allocates to the same invoice twice")
                continue
            seen_invoice_ids.add(allocation.invoice_id)

            invoice_matches = invoices_by_id.get(allocation.invoice_id, [])

            if not invoice_matches:
                issues.append(
                    f"invoice ID {allocation.invoice_id!r} does not exist"
                )
                continue

            if len(invoice_matches) > 1:
                issues.append(
                    f"duplicate invoice IDs make {allocation.invoice_id!r} ambiguous"
                )
                continue

            invoice = invoice_matches[0]

            normalized_number = canonical_reference(invoice.number)

            if normalized_number not in actual_references:
                if not is_contextual:
                    issues.append(
                        f"payment reference or note does not contain "
                        f"invoice reference {invoice.number!r}"
                    )

            matching_numbers = invoices_by_number.get(normalized_number, [])
            distinct_invoice_ids = {
                record.invoice_id for record in matching_numbers
            }
            if len(distinct_invoice_ids) > 1:
                issues.append(
                    f"invoice reference {invoice.number!r} matches "
                    f"multiple invoice records"
                )

            if not invoice.is_open:
                issues.append(
                    f"invoice {invoice.number} is not open "
                    f"(status {invoice.status.value})"
                )

            if invoice.currency.upper() != payment.currency.upper():
                issues.append(
                    f"currency mismatch for invoice {invoice.number}: "
                    f"{payment.currency} payment vs {invoice.currency} invoice"
                )

            if invoice.invoice_id not in verified_invoice_balances:
                try:
                    verified_invoice_balances[invoice.invoice_id] = cross_check_invoice_balance(
                        invoice, approved_allocations
                    )
                except ValueError as exc:
                    issues.append(str(exc))
                    continue

            available = verified_invoice_balances[invoice.invoice_id]

            if available < 0:
                issues.append(
                    f"invoice {invoice.number} already has a negative "
                    f"available balance in the ledger"
                )
            elif allocation.amount_cents > available:
                issues.append(
                    f"allocation of {allocation.amount_cents} cents exceeds "
                    f"available balance of {available} cents for {invoice.number}"
                )

        if not issues:
            valid_candidates[index] = (proposal, payment)

    # Pass 2: reject all proposals competing for more than an invoice's
    # remaining capacity. Do not pick a winner based on arbitrary order.
    usage_by_invoice: dict[str, list[tuple[int, int]]] = defaultdict(list)

    for index, (proposal, _payment) in valid_candidates.items():
        candidate = proposal.candidates[0]
        for allocation in candidate.allocations:
            usage_by_invoice[allocation.invoice_id].append(
                (index, allocation.amount_cents)
            )

    for invoice_id, usages in usage_by_invoice.items():
        invoice_matches = invoices_by_id[invoice_id]
        invoice = invoice_matches[0]
        available = verified_invoice_balances.get(
            invoice_id,
            outstanding_cents(invoice, approved_allocations),
        )
        total_proposed = sum(amount for _index, amount in usages)

        if total_proposed > available:
            for index, _amount in usages:
                issues_by_index[index].append(
                    f"combined proposals reserve {total_proposed} cents "
                    f"for {invoice.number}, exceeding its available "
                    f"balance of {available} cents"
                )

    return [
        VerificationResult(
            payment_id=proposal.payment_id,
            passed=not issues_by_index[index],
            issues=tuple(issues_by_index[index]),
        )
        for index, proposal in enumerate(proposals)
    ]

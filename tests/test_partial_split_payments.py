"""Tests for Partial and Split Payments, sequential execution, duplicate protection, and capacity invariants."""

from datetime import datetime, timezone
import pytest

from factories import inv, pay
from reconcile.models import (
    AllocationStatus,
    Invoice,
    MatchOutcome,
    Payment,
    PaymentAllocation,
    PaymentStatus,
    outstanding_cents,
)
from reconcile.workflow import reconcile_payments
from reconcile.advanced import AdvancedMatcher
from reconcile.verifier import verify_proposals


def test_sequential_independent_payments_collectively_settle_invoice():
    """Verify multiple independent payments sequentially settle an invoice to $0 outstanding."""
    invoice = inv(1, 100000)  # $1,000 invoice
    matcher = AdvancedMatcher()

    # Step 1: Payment 1 arrives for $300
    p1 = pay(1, 30000, reference=invoice.number)
    res1 = reconcile_payments([p1], [invoice], matcher=matcher)
    assert res1.proposals[0].outcome is MatchOutcome.PROPOSED
    alloc1 = PaymentAllocation(
        payment_id=p1.payment_id,
        invoice_id=invoice.invoice_id,
        amount_cents=30000,
        status=AllocationStatus.APPROVED,
        currency="USD",
    )
    assert outstanding_cents(invoice, [alloc1]) == 70000

    # Step 2: Payment 2 arrives for $450 against existing approved alloc1
    p2 = pay(2, 45000, reference=invoice.number)
    res2 = reconcile_payments([p2], [invoice], matcher=matcher, existing_allocations=[alloc1])
    assert res2.proposals[0].outcome is MatchOutcome.PROPOSED
    alloc2 = PaymentAllocation(
        payment_id=p2.payment_id,
        invoice_id=invoice.invoice_id,
        amount_cents=45000,
        status=AllocationStatus.APPROVED,
        currency="USD",
    )
    assert outstanding_cents(invoice, [alloc1, alloc2]) == 25000

    # Step 3: Payment 3 arrives for final $250
    p3 = pay(3, 25000, reference=invoice.number)
    res3 = reconcile_payments([p3], [invoice], matcher=matcher, existing_allocations=[alloc1, alloc2])
    assert res3.proposals[0].outcome is MatchOutcome.PROPOSED
    alloc3 = PaymentAllocation(
        payment_id=p3.payment_id,
        invoice_id=invoice.invoice_id,
        amount_cents=25000,
        status=AllocationStatus.APPROVED,
        currency="USD",
    )

    # All 3 payments collectively fully settle the invoice to $0
    assert outstanding_cents(invoice, [alloc1, alloc2, alloc3]) == 0


def test_concurrent_payments_prevent_double_claiming_in_same_batch():
    """Verify two payments in the same batch cannot simultaneously claim the same invoice."""
    invoice = inv(1, 50000)  # $500
    p1 = pay(1, 30000, reference=invoice.number, minutes=0)
    p2 = pay(2, 20000, reference=invoice.number, minutes=5)

    matcher = AdvancedMatcher()
    run_result = reconcile_payments([p1, p2], [invoice], matcher=matcher)

    # First payment claims the invoice
    assert run_result.proposals[0].outcome is MatchOutcome.PROPOSED
    assert run_result.proposals[0].candidates[0].allocations[0].amount_cents == 30000

    # Second payment is blocked from claiming already-claimed invoice in the same uncommitted batch
    assert run_result.proposals[1].outcome is MatchOutcome.NO_MATCH
    assert "already claimed" in run_result.proposals[1].reasons[0].detail


def test_sequential_payments_respect_diminishing_capacity():
    """Verify that prior approved allocations diminish capacity so subsequent payments allocate only remainder and report excess."""
    invoice = inv(1, 80000)  # $800 invoice
    prior_alloc = PaymentAllocation(
        payment_id="pay_prior_1",
        invoice_id=invoice.invoice_id,
        amount_cents=50000,
        status=AllocationStatus.APPROVED,
        currency="USD",
    )

    # Remaining capacity is $300 (30,000 cents). New payment of $400 arrives.
    p_new = pay(2, 40000, reference=invoice.number)
    matcher = AdvancedMatcher()

    run_result = reconcile_payments(
        payments=[p_new],
        invoice_source=[invoice],
        matcher=matcher,
        existing_allocations=[prior_alloc],
    )

    prop = run_result.proposals[0]
    assert prop.outcome is MatchOutcome.PROPOSED
    assert prop.candidates[0].allocations[0].amount_cents == 30000
    assert prop.excess_amount_cents == 10000


def test_duplicate_webhook_cannot_consume_capacity_twice():
    """Verify that replayed duplicate webhook events are rejected and do not consume remaining capacity."""
    invoice = inv(1, 100000)  # $1,000 invoice
    p1 = pay(1, 40000, reference=invoice.number)

    # Suppose p1 is approved in ledger
    p1_alloc = PaymentAllocation(
        payment_id=p1.payment_id,
        invoice_id=invoice.invoice_id,
        amount_cents=40000,
        status=AllocationStatus.APPROVED,
        currency="USD",
    )

    # Webhook replay arrives with same payment_id
    p1_replayed = pay(1, 40000, reference=invoice.number)
    # Subsequent fresh payment p2 arrives for remaining $600
    p2 = pay(2, 60000, reference=invoice.number)

    matcher = AdvancedMatcher()
    run_result = reconcile_payments(
        payments=[p1_replayed, p2],
        invoice_source=[invoice],
        matcher=matcher,
        existing_allocations=[p1_alloc],
    )

    proposals = run_result.proposals
    assert len(proposals) == 2

    # Replayed webhook is recognized as DUPLICATE_EVENT and rejected as NO_MATCH
    assert proposals[0].outcome is MatchOutcome.NO_MATCH

    # New payment p2 successfully claims the remaining 60,000 cents
    assert proposals[1].outcome is MatchOutcome.PROPOSED
    assert proposals[1].candidates[0].allocations[0].amount_cents == 60000


def test_split_grouped_payment_across_two_invoices():
    """Verify a single payment referencing two invoices splits correctly across both."""
    inv_a = inv(1, 20000)  # $200
    inv_b = inv(2, 30000)  # $300
    payment = pay(10, 50000, reference=f"{inv_a.number}, {inv_b.number}")

    matcher = AdvancedMatcher()
    run_result = reconcile_payments([payment], [inv_a, inv_b], matcher=matcher)

    assert len(run_result.proposals) == 1
    prop = run_result.proposals[0]
    assert prop.outcome is MatchOutcome.PROPOSED
    assert len(prop.candidates[0].allocations) == 2

    alloc_map = {a.invoice_id: a.amount_cents for a in prop.candidates[0].allocations}
    assert alloc_map[inv_a.invoice_id] == 20000
    assert alloc_map[inv_b.invoice_id] == 30000

"""Tests for Payment Reversals domain logic, persistence, and audit logging."""

from datetime import datetime, timedelta, timezone
import pytest

from factories import inv, pay
from reconcile.models import (
    AccountingError,
    AllocationStatus,
    Invoice,
    MatchOutcome,
    Payment,
    PaymentAllocation,
    PaymentReversal,
    PaymentStatus,
    outstanding_cents,
    cross_check_invoice_balance,
)
from reconcile.workflow import reconcile_payments
from reconcile.advanced import AdvancedMatcher


def make_alloc(payment_id: str, invoice_id: str, amount_cents: int, currency: str = "USD") -> PaymentAllocation:
    return PaymentAllocation(
        payment_id=payment_id,
        invoice_id=invoice_id,
        amount_cents=amount_cents,
        status=AllocationStatus.APPROVED,
        currency=currency,
    )


def make_reversal(
    reversal_id: str,
    parent_payment_id: str,
    amount_cents: int,
    currency: str = "USD",
    invoice_id: str = "inv_1",
    dispute_id: str = "DISPUTE-001",
    days_ago: int = 1,
) -> PaymentReversal:
    return PaymentReversal(
        reversal_id=reversal_id,
        parent_payment_id=parent_payment_id,
        amount_cents=amount_cents,
        currency=currency,
        reversed_at=datetime.now(timezone.utc) - timedelta(days=days_ago),
        reason="Chargeback / Involuntary Bank Clawback",
        dispute_id=dispute_id,
        invoice_id=invoice_id,
    )


def test_full_reversal_restores_balance():
    """Verify involuntary full chargeback reversal restores 100% of the invoice balance."""
    invoice = inv(1, 50000)  # $500
    alloc = make_alloc("pay_rev_1", invoice.invoice_id, 50000)

    assert outstanding_cents(invoice, [alloc]) == 0

    reversal = make_reversal("rev_full_1", "pay_rev_1", 50000, invoice_id=invoice.invoice_id)
    assert outstanding_cents(invoice, [alloc], reversals=[reversal]) == 50000


def test_partial_reversal_restores_partial_balance():
    """Verify partial reversal (e.g. partial chargeback award) restores exact partial balance."""
    invoice = inv(1, 100000)  # $1,000
    alloc = make_alloc("pay_rev_2", invoice.invoice_id, 100000)

    reversal = make_reversal("rev_part_1", "pay_rev_2", 40000, invoice_id=invoice.invoice_id)
    # Net deduction: 100,000 - 40,000 = 60,000 -> Outstanding: 40,000
    assert outstanding_cents(invoice, [alloc], reversals=[reversal]) == 40000


def test_duplicate_reversal_is_idempotent():
    """Verify duplicate reversal deliveries with identical ID and amount do not double-restore balance."""
    invoice = inv(1, 80000)
    alloc = make_alloc("pay_rev_3", invoice.invoice_id, 80000)
    reversal = make_reversal("rev_dupe_1", "pay_rev_3", 30000, invoice_id=invoice.invoice_id)

    # Delivered twice in webhook stream
    assert outstanding_cents(invoice, [alloc], reversals=[reversal, reversal]) == 30000


def test_conflicting_reversal_amount_fails_closed():
    """Verify contradictory amounts for the same reversal_id raise AccountingError."""
    invoice = inv(1, 80000)
    alloc = make_alloc("pay_rev_4", invoice.invoice_id, 80000)
    rev_a = make_reversal("rev_conflict_1", "pay_rev_4", 25000, invoice_id=invoice.invoice_id)
    rev_b = make_reversal("rev_conflict_1", "pay_rev_4", 35000, invoice_id=invoice.invoice_id)

    with pytest.raises(AccountingError, match="Conflicting reversal amount"):
        outstanding_cents(invoice, [alloc], reversals=[rev_a, rev_b])


def test_over_reversal_fails_closed():
    """Verify reversal exceeding total approved deductions fails closed."""
    invoice = inv(1, 60000)
    alloc = make_alloc("pay_rev_5", invoice.invoice_id, 60000)
    rev_excess = make_reversal("rev_over_1", "pay_rev_5", 80000, invoice_id=invoice.invoice_id)

    with pytest.raises(AccountingError, match="Total returns .* exceed approved allocations"):
        outstanding_cents(invoice, [alloc], reversals=[rev_excess])


def test_currency_mismatch_reversal_fails_closed():
    """Verify reversal in a mismatched currency fails closed."""
    invoice = inv(1, 60000, currency="USD")
    alloc = make_alloc("pay_rev_6", invoice.invoice_id, 60000, currency="USD")
    rev_eur = make_reversal("rev_mismatch_1", "pay_rev_6", 20000, currency="EUR", invoice_id=invoice.invoice_id)

    with pytest.raises(AccountingError, match="Currency mismatch for reversal"):
        outstanding_cents(invoice, [alloc], reversals=[rev_eur])


def test_delayed_reversal_allows_subsequent_repayment():
    """Verify reversal arriving 30 days later restores capacity and allows matching a new payment."""
    invoice = inv(1, 100000)
    alloc_old = make_alloc("pay_old", invoice.invoice_id, 100000)

    # 30 days later, bank chargeback arrives
    reversal = make_reversal(
        "rev_delayed_1",
        "pay_old",
        100000,
        invoice_id=invoice.invoice_id,
        days_ago=1,
    )

    # New replacement payment arrives from customer
    new_pay = pay(
        2,
        100000,
        reference=invoice.number,
        payer=invoice.customer_name,
    )

    matcher = AdvancedMatcher()
    proposals = matcher.match(
        [new_pay],
        [invoice],
        approved=[alloc_old],
        reversals=[reversal],
    )

    assert len(proposals) == 1
    assert proposals[0].outcome is MatchOutcome.PROPOSED
    assert proposals[0].candidates[0].allocations[0].amount_cents == 100000


def test_reversal_and_refund_combined_accounting():
    """Verify merchant refund and bank reversal can coexist on different payments of the same invoice."""
    invoice = inv(1, 250000)  # $2,500 invoice
    alloc_1 = make_alloc("pay_part_1", invoice.invoice_id, 100000)
    alloc_2 = make_alloc("pay_part_2", invoice.invoice_id, 150000)

    # $300 refunded by merchant on pay_part_1
    from reconcile.models import Refund
    refund = Refund(
        refund_id="ref_comb_1",
        parent_payment_id="pay_part_1",
        amount_cents=30000,
        currency="USD",
        refunded_at=datetime.now(timezone.utc),
        invoice_id=invoice.invoice_id,
    )
    # $500 reversed by bank on pay_part_2
    reversal = make_reversal("rev_comb_1", "pay_part_2", 50000, invoice_id=invoice.invoice_id)

    # Gross deductions: 250,000
    # Total returns: 30,000 + 50,000 = 80,000
    # Net deductions: 170,000
    # Outstanding balance: 80,000
    assert outstanding_cents(invoice, [alloc_1, alloc_2], refunds=[refund], reversals=[reversal]) == 80000

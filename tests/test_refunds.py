"""Tests for Refunds and Reversals financial domain logic and ledger invariants."""

from datetime import datetime, timezone
import pytest

from factories import inv, pay
from reconcile.models import (
    AccountingError,
    AllocationStatus,
    Invoice,
    PaymentAllocation,
    Refund,
    cross_check_invoice_balance,
    outstanding_cents,
)


def make_alloc(payment_id: str, invoice_id: str, amount_cents: int, currency: str = "USD") -> PaymentAllocation:
    return PaymentAllocation(
        payment_id=payment_id,
        invoice_id=invoice_id,
        amount_cents=amount_cents,
        status=AllocationStatus.APPROVED,
        currency=currency,
    )


def make_refund(
    refund_id: str,
    parent_payment_id: str,
    amount_cents: int,
    currency: str = "USD",
    invoice_id: str = "inv_1",
) -> Refund:
    return Refund(
        refund_id=refund_id,
        parent_payment_id=parent_payment_id,
        amount_cents=amount_cents,
        currency=currency,
        refunded_at=datetime.now(timezone.utc),
        invoice_id=invoice_id,
    )


def test_full_refund_restores_outstanding_balance():
    invoice = inv(1, 100000)  # $1,000
    alloc = make_alloc("pay_1", invoice.invoice_id, 100000)

    # Before refund: fully paid ($0 outstanding)
    assert outstanding_cents(invoice, [alloc]) == 0

    # Full refund of $1,000
    refund = make_refund("ref_1", "pay_1", 100000, invoice_id=invoice.invoice_id)
    assert outstanding_cents(invoice, [alloc], refunds=[refund]) == 100000


def test_partial_refund_restores_partial_balance():
    invoice = inv(1, 100000)  # $1,000
    alloc = make_alloc("pay_1", invoice.invoice_id, 100000)

    # Partial refund of $200 (20,000 cents)
    refund = make_refund("ref_1", "pay_1", 20000, invoice_id=invoice.invoice_id)
    # Net paid is $800, so outstanding is $200
    assert outstanding_cents(invoice, [alloc], refunds=[refund]) == 20000


def test_duplicate_refund_id_is_safely_deduplicated():
    invoice = inv(1, 100000)
    alloc = make_alloc("pay_1", invoice.invoice_id, 100000)
    refund = make_refund("ref_1", "pay_1", 20000, invoice_id=invoice.invoice_id)

    # Same refund submitted twice (webhook redelivery)
    assert outstanding_cents(invoice, [alloc], refunds=[refund, refund]) == 20000


def test_conflicting_refund_amount_fails_closed():
    invoice = inv(1, 100000)
    alloc = make_alloc("pay_1", invoice.invoice_id, 100000)
    ref_a = make_refund("ref_1", "pay_1", 20000, invoice_id=invoice.invoice_id)
    ref_b = make_refund("ref_1", "pay_1", 30000, invoice_id=invoice.invoice_id)

    with pytest.raises(AccountingError, match="Conflicting refund amount"):
        outstanding_cents(invoice, [alloc], refunds=[ref_a, ref_b])


def test_over_refund_fails_closed():
    invoice = inv(1, 100000)
    alloc = make_alloc("pay_1", invoice.invoice_id, 100000)
    # Refund of $1,200 on a $1,000 payment
    refund = make_refund("ref_1", "pay_1", 120000, invoice_id=invoice.invoice_id)

    with pytest.raises(AccountingError, match="Total returns .* exceed approved allocations"):
        outstanding_cents(invoice, [alloc], refunds=[refund])


def test_cross_currency_refund_fails_closed():
    invoice = inv(1, 100000, currency="USD")
    alloc = make_alloc("pay_1", invoice.invoice_id, 100000, currency="USD")
    refund = make_refund("ref_1", "pay_1", 20000, currency="EUR", invoice_id=invoice.invoice_id)

    with pytest.raises(AccountingError, match="Currency mismatch for refund"):
        outstanding_cents(invoice, [alloc], refunds=[refund])


def test_full_refund_against_provider_settled_payment():
    """Verify that an invoice settled directly by PayPal (ProviderSettlement) has its balance restored upon full refund."""
    invoice = inv(1, 75000)  # $750 invoice
    # External PayPal settlement
    settlement_alloc = PaymentAllocation(
        payment_id="pay_paypal_settled_1",
        invoice_id=invoice.invoice_id,
        amount_cents=75000,
        status=AllocationStatus.APPROVED,
        currency="USD",
    )
    # Before refund: fully settled ($0 outstanding)
    assert outstanding_cents(invoice, [settlement_alloc]) == 0

    # Merchant refunds the PayPal settlement
    refund = Refund(
        refund_id="ref_paypal_1",
        parent_payment_id="pay_paypal_settled_1",
        amount_cents=75000,
        currency="USD",
        refunded_at=datetime.now(timezone.utc),
        invoice_id=invoice.invoice_id,
        target_type="SETTLEMENT",
        settlement_id="set_rec_001",
    )
    # Balance is fully restored to $750
    assert outstanding_cents(invoice, [settlement_alloc], refunds=[refund]) == 75000


def test_partial_refund_against_provider_settled_payment():
    """Verify partial refund against an externally settled PayPal invoice restores exact partial capacity."""
    invoice = inv(1, 150000)  # $1,500 invoice
    settlement_alloc = PaymentAllocation(
        payment_id="pay_paypal_settled_2",
        invoice_id=invoice.invoice_id,
        amount_cents=150000,
        status=AllocationStatus.APPROVED,
        currency="USD",
    )
    # Partial refund of $450
    refund = Refund(
        refund_id="ref_paypal_part_1",
        parent_payment_id="pay_paypal_settled_2",
        amount_cents=45000,
        currency="USD",
        refunded_at=datetime.now(timezone.utc),
        invoice_id=invoice.invoice_id,
        target_type="SETTLEMENT",
        settlement_id="set_rec_002",
    )
    # Outstanding balance should now be $450
    assert outstanding_cents(invoice, [settlement_alloc], refunds=[refund]) == 45000


def test_independent_accounting_of_settlements_and_allocations_with_refunds():
    """Verify provider settlements and application allocations remain distinct and do not conflate during refund calculations."""
    invoice = inv(1, 200000)  # $2,000 invoice
    # $800 settled via PayPal external settlement
    settlement_alloc = PaymentAllocation(
        payment_id="pay_settle_ext",
        invoice_id=invoice.invoice_id,
        amount_cents=80000,
        status=AllocationStatus.APPROVED,
        currency="USD",
    )
    # $1,200 allocated via application approval
    app_alloc = make_alloc("pay_app_internal", invoice.invoice_id, 120000)

    # Gross deductions = $2,000 -> balance $0
    assert outstanding_cents(invoice, [settlement_alloc, app_alloc]) == 0

    # Refund against the application allocation only ($300)
    refund_app = Refund(
        refund_id="ref_app_1",
        parent_payment_id="pay_app_internal",
        amount_cents=30000,
        currency="USD",
        refunded_at=datetime.now(timezone.utc),
        invoice_id=invoice.invoice_id,
        target_type="ALLOCATION",
        allocation_id="alloc_rec_001",
    )
    # Net deductions: 200,000 - 30,000 = 170,000 -> Outstanding = 30,000
    assert outstanding_cents(invoice, [settlement_alloc, app_alloc], refunds=[refund_app]) == 30000

    # Cross-check invoice balance raises AccountingError if PayPal due_amount contradicts
    invoice_contradicting = inv(1, 200000, due_amount_cents=0)  # PayPal claims 0 due, but ledger says 30,000
    with pytest.raises(AccountingError, match="Accounting inconsistency for invoice"):
        cross_check_invoice_balance(invoice_contradicting, [settlement_alloc, app_alloc], refunds=[refund_app])


def test_refund_for_unrelated_payment_does_not_restore_capacity():
    """Verify refund referencing an unrelated payment never restores invoice capacity."""
    invoice = inv(1, 100000)  # $1,000
    alloc = make_alloc("pay_legit_1", invoice.invoice_id, 100000)

    # Fully paid ($0 outstanding)
    assert outstanding_cents(invoice, [alloc]) == 0

    # Refund arrives for an unrelated payment "pay_unrelated_999"
    unrelated_refund = make_refund(
        "ref_unrelated_1", "pay_unrelated_999", 50000, invoice_id=invoice.invoice_id
    )

    # Balance remains 0; unrelated refund does NOT restore capacity
    assert outstanding_cents(invoice, [alloc], refunds=[unrelated_refund]) == 0


def test_refund_with_non_completed_status_does_not_restore_capacity():
    """Verify pending or failed refunds do not restore invoice capacity."""
    invoice = inv(1, 100000)  # $1,000
    alloc = make_alloc("pay_1", invoice.invoice_id, 100000)

    assert outstanding_cents(invoice, [alloc]) == 0

    pending_refund = Refund(
        refund_id="ref_pending_1",
        parent_payment_id="pay_1",
        amount_cents=50000,
        currency="USD",
        refunded_at=datetime.now(timezone.utc),
        status="PENDING",
        invoice_id=invoice.invoice_id,
    )

    # Pending refund must not alter balance
    assert outstanding_cents(invoice, [alloc], refunds=[pending_refund]) == 0


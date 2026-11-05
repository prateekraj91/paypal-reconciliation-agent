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

    with pytest.raises(AccountingError, match="Total refunds .* exceed approved allocations"):
        outstanding_cents(invoice, [alloc], refunds=[refund])


def test_cross_currency_refund_fails_closed():
    invoice = inv(1, 100000, currency="USD")
    alloc = make_alloc("pay_1", invoice.invoice_id, 100000, currency="USD")
    refund = make_refund("ref_1", "pay_1", 20000, currency="EUR", invoice_id=invoice.invoice_id)

    with pytest.raises(AccountingError, match="Currency mismatch for refund"):
        outstanding_cents(invoice, [alloc], refunds=[refund])

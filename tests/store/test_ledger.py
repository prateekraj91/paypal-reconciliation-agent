"""Tests for financial ledger invariants, balance computations, and capacity enforcement."""

from __future__ import annotations

import os
import tempfile
import pytest
from datetime import datetime, timezone, date

from alembic.config import Config
from alembic import command

from reconcile_store.config import DatabaseConfig
from reconcile_store.db import DatabaseEngine
from reconcile_store.repositories import (
    TenantRepository,
    InvoiceRepository,
    PaymentRepository,
    AllocationRepository,
    DecisionRepository,
)
from reconcile_store.repositories.allocation_repo import LedgerConstraintError
from reconcile_store.repositories.invoice_repo import IneligibleInvoiceError
from reconcile_store.repositories.payment_repo import IneligiblePaymentError


@pytest.fixture
def store():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    url = f"sqlite:///{path}"
    os.environ["RECONCILE_DB_URL"] = url
    alembic_cfg = Config("alembic.ini")
    alembic_cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(alembic_cfg, "head")

    db = DatabaseEngine(DatabaseConfig(url=url))
    with db.session() as session:
        t_repo = TenantRepository(session)
        t_repo.create_tenant("test_tenant", "Test Merchant")
        session.commit()

    yield db

    db.dispose()
    if os.path.exists(path):
        os.unlink(path)


def _create_approved_decision(session, tenant_id: str, payment_id: str, key: str) -> str:
    from reconcile_store.repositories import ReconciliationRunRepository, ProposalRepository
    run_repo = ReconciliationRunRepository(session)
    prop_repo = ProposalRepository(session)
    run = run_repo.create_run(tenant_id=tenant_id, input_snapshot_id="test_snap", matcher_name="test_matcher")
    prop = prop_repo.create_proposal(
        tenant_id=tenant_id,
        run_id=run.id,
        payment_id=payment_id,
        outcome="PROPOSED",
    )
    dec_repo = DecisionRepository(session)
    dec = dec_repo.record_decision(
        tenant_id=tenant_id,
        proposal_id=prop.id,
        decision_type="APPROVED",
        actor="operator@example.com",
        reason="Approved in test",
        idempotency_key=key,
    )
    return dec.id


def test_exact_full_allocation_and_balances(store):
    """Test single exact match allocation: payment and invoice balances drop to 0."""
    with store.session() as session:
        inv_repo = InvoiceRepository(session)
        pay_repo = PaymentRepository(session)
        alloc_repo = AllocationRepository(session)

        inv = inv_repo.upsert_invoice(
            tenant_id="test_tenant",
            provider="paypal",
            provider_invoice_id="INV-FULL-1",
            invoice_number="INV-100",
            total_minor=10000,
            currency="USD",
            provider_due_minor=10000,
            status="SENT",
            customer_name="Alice",
        )
        pay = pay_repo.upsert_payment(
            tenant_id="test_tenant",
            provider="paypal",
            provider_payment_id="PAY-FULL-1",
            amount_minor=10000,
            currency="USD",
            status="COMPLETED",
            payer_name="Alice",
        )

        dec_id = _create_approved_decision(session, "test_tenant", pay.id, "key-1")

        # Initial balances before allocation
        assert inv_repo.get_ledger_outstanding_minor("test_tenant", inv.id) == 10000
        assert pay_repo.get_unapplied_cash_minor("test_tenant", pay.id) == 10000

        # Create allocation
        alloc_repo.create_allocation(
            tenant_id="test_tenant",
            payment_id=pay.id,
            invoice_id=inv.id,
            amount_minor=10000,
            currency="USD",
            decision_id=dec_id,
            actor="operator@example.com",
        )
        session.commit()

        # Balances after allocation
        assert inv_repo.get_ledger_outstanding_minor("test_tenant", inv.id) == 0
        assert pay_repo.get_unapplied_cash_minor("test_tenant", pay.id) == 0


def test_partial_allocation_and_unapplied_cash(store):
    """Test partial allocation: remainder remains explicitly identified as unapplied cash (I2)."""
    with store.session() as session:
        inv_repo = InvoiceRepository(session)
        pay_repo = PaymentRepository(session)
        alloc_repo = AllocationRepository(session)

        inv = inv_repo.upsert_invoice(
            tenant_id="test_tenant",
            provider="paypal",
            provider_invoice_id="INV-PARTIAL-1",
            invoice_number="INV-200",
            total_minor=10000,
            currency="USD",
            status="SENT",
            customer_name="Bob",
        )
        pay = pay_repo.upsert_payment(
            tenant_id="test_tenant",
            provider="paypal",
            provider_payment_id="PAY-PARTIAL-1",
            amount_minor=6000,
            currency="USD",
            status="COMPLETED",
            payer_name="Bob",
        )
        dec_id = _create_approved_decision(session, "test_tenant", pay.id, "key-2")

        alloc_repo.create_allocation(
            tenant_id="test_tenant",
            payment_id=pay.id,
            invoice_id=inv.id,
            amount_minor=6000,
            currency="USD",
            decision_id=dec_id,
            actor="operator@example.com",
        )
        session.commit()

        # Payment fully applied
        assert pay_repo.get_unapplied_cash_minor("test_tenant", pay.id) == 0
        # Invoice still has 4000 minor units outstanding in application ledger
        assert inv_repo.get_ledger_outstanding_minor("test_tenant", inv.id) == 4000


def test_multiple_invoices_funded_by_one_payment(store):
    """Test split payment: one payment funding two different invoices."""
    with store.session() as session:
        inv_repo = InvoiceRepository(session)
        pay_repo = PaymentRepository(session)
        alloc_repo = AllocationRepository(session)

        inv1 = inv_repo.upsert_invoice(
            tenant_id="test_tenant",
            provider="paypal",
            provider_invoice_id="INV-SPLIT-A",
            invoice_number="INV-A",
            total_minor=3000,
            currency="USD",
            status="SENT",
            customer_name="Charlie",
        )
        inv2 = inv_repo.upsert_invoice(
            tenant_id="test_tenant",
            provider="paypal",
            provider_invoice_id="INV-SPLIT-B",
            invoice_number="INV-B",
            total_minor=5000,
            currency="USD",
            status="SENT",
            customer_name="Charlie",
        )
        pay = pay_repo.upsert_payment(
            tenant_id="test_tenant",
            provider="paypal",
            provider_payment_id="PAY-SPLIT-1",
            amount_minor=10000,
            currency="USD",
            status="COMPLETED",
            payer_name="Charlie",
        )
        dec_id = _create_approved_decision(session, "test_tenant", pay.id, "key-split")

        alloc_repo.create_allocation(
            tenant_id="test_tenant",
            payment_id=pay.id,
            invoice_id=inv1.id,
            amount_minor=3000,
            currency="USD",
            decision_id=dec_id,
            actor="operator@example.com",
        )
        alloc_repo.create_allocation(
            tenant_id="test_tenant",
            payment_id=pay.id,
            invoice_id=inv2.id,
            amount_minor=5000,
            currency="USD",
            decision_id=dec_id,
            actor="operator@example.com",
        )
        session.commit()

        # Invoices are both fully paid
        assert inv_repo.get_ledger_outstanding_minor("test_tenant", inv1.id) == 0
        assert inv_repo.get_ledger_outstanding_minor("test_tenant", inv2.id) == 0
        # Payment has 2000 unapplied cash remaining (10000 - 3000 - 5000)
        assert pay_repo.get_unapplied_cash_minor("test_tenant", pay.id) == 2000


def test_multiple_payments_funding_one_invoice(store):
    """Test one invoice funded by multiple payments."""
    with store.session() as session:
        inv_repo = InvoiceRepository(session)
        pay_repo = PaymentRepository(session)
        alloc_repo = AllocationRepository(session)

        inv = inv_repo.upsert_invoice(
            tenant_id="test_tenant",
            provider="paypal",
            provider_invoice_id="INV-MULTI-PAY",
            invoice_number="INV-MULTI",
            total_minor=8000,
            currency="USD",
            status="SENT",
            customer_name="Diana",
        )
        pay1 = pay_repo.upsert_payment(
            tenant_id="test_tenant",
            provider="paypal",
            provider_payment_id="PAY-M-1",
            amount_minor=5000,
            currency="USD",
            status="COMPLETED",
            payer_name="Diana",
        )
        pay2 = pay_repo.upsert_payment(
            tenant_id="test_tenant",
            provider="paypal",
            provider_payment_id="PAY-M-2",
            amount_minor=3000,
            currency="USD",
            status="COMPLETED",
            payer_name="Diana",
        )

        dec1 = _create_approved_decision(session, "test_tenant", pay1.id, "key-m1")
        dec2 = _create_approved_decision(session, "test_tenant", pay2.id, "key-m2")

        alloc_repo.create_allocation(
            tenant_id="test_tenant",
            payment_id=pay1.id,
            invoice_id=inv.id,
            amount_minor=5000,
            currency="USD",
            decision_id=dec1,
            actor="operator@example.com",
        )
        alloc_repo.create_allocation(
            tenant_id="test_tenant",
            payment_id=pay2.id,
            invoice_id=inv.id,
            amount_minor=3000,
            currency="USD",
            decision_id=dec2,
            actor="operator@example.com",
        )
        session.commit()

        assert inv_repo.get_ledger_outstanding_minor("test_tenant", inv.id) == 0
        assert pay_repo.get_unapplied_cash_minor("test_tenant", pay1.id) == 0
        assert pay_repo.get_unapplied_cash_minor("test_tenant", pay2.id) == 0


def test_over_allocation_prevention(store):
    """Test that allocations exceeding payment amount (I2) or invoice total (I3) are rejected."""
    with store.session() as session:
        inv_repo = InvoiceRepository(session)
        pay_repo = PaymentRepository(session)
        alloc_repo = AllocationRepository(session)

        inv = inv_repo.upsert_invoice(
            tenant_id="test_tenant",
            provider="paypal",
            provider_invoice_id="INV-CAP-1",
            invoice_number="INV-CAP",
            total_minor=5000,
            currency="USD",
            status="SENT",
            customer_name="Evan",
        )
        # 1. Exceeding invoice capacity: payment has 10000, invoice has 5000, try to allocate 6000
        pay = pay_repo.upsert_payment(
            tenant_id="test_tenant",
            provider="paypal",
            provider_payment_id="PAY-CAP-1",
            amount_minor=10000,
            currency="USD",
            status="COMPLETED",
            payer_name="Evan",
        )
        dec_id = _create_approved_decision(session, "test_tenant", pay.id, "key-cap")

        with pytest.raises(LedgerConstraintError) as exc_inv:
            alloc_repo.create_allocation(
                tenant_id="test_tenant",
                payment_id=pay.id,
                invoice_id=inv.id,
                amount_minor=6000,
                currency="USD",
                decision_id=dec_id,
                actor="operator@example.com",
            )
        assert "Invoice over-allocation" in str(exc_inv.value)

        # 2. Exceeding payment capacity: invoice has 50000, payment has 10000, try to allocate 12000
        inv_big = inv_repo.upsert_invoice(
            tenant_id="test_tenant",
            provider="paypal",
            provider_invoice_id="INV-CAP-BIG",
            invoice_number="INV-BIG",
            total_minor=50000,
            currency="USD",
            status="SENT",
            customer_name="Evan",
        )
        with pytest.raises(LedgerConstraintError) as exc_pay:
            alloc_repo.create_allocation(
                tenant_id="test_tenant",
                payment_id=pay.id,
                invoice_id=inv_big.id,
                amount_minor=12000,
                currency="USD",
                decision_id=dec_id,
                actor="operator@example.com",
            )
        assert "Payment over-allocation" in str(exc_pay.value)


def test_currency_mismatch_and_invalid_amounts_rejected(store):
    """Invariant I4: Payment, invoice, and allocation currencies must match. Zero/negative rejected."""
    with store.session() as session:
        inv_repo = InvoiceRepository(session)
        pay_repo = PaymentRepository(session)
        alloc_repo = AllocationRepository(session)

        inv = inv_repo.upsert_invoice(
            tenant_id="test_tenant",
            provider="paypal",
            provider_invoice_id="INV-CURR-EUR",
            invoice_number="INV-EUR",
            total_minor=5000,
            currency="EUR",
            status="SENT",
            customer_name="Fiona",
        )
        pay = pay_repo.upsert_payment(
            tenant_id="test_tenant",
            provider="paypal",
            provider_payment_id="PAY-CURR-USD",
            amount_minor=5000,
            currency="USD",
            status="COMPLETED",
            payer_name="Fiona",
        )
        dec_id = _create_approved_decision(session, "test_tenant", pay.id, "key-curr")

        # Currency mismatch rejected
        with pytest.raises(LedgerConstraintError) as exc:
            alloc_repo.create_allocation(
                tenant_id="test_tenant",
                payment_id=pay.id,
                invoice_id=inv.id,
                amount_minor=5000,
                currency="USD",
                decision_id=dec_id,
                actor="operator@example.com",
            )
        assert "Currency mismatch" in str(exc.value)

        # Negative amount rejected (I1)
        with pytest.raises(LedgerConstraintError):
            alloc_repo.create_allocation(
                tenant_id="test_tenant",
                payment_id=pay.id,
                invoice_id=inv.id,
                amount_minor=-500,
                currency="USD",
                decision_id=dec_id,
                actor="operator@example.com",
            )

        # Zero amount rejected (I1)
        with pytest.raises(LedgerConstraintError):
            alloc_repo.create_allocation(
                tenant_id="test_tenant",
                payment_id=pay.id,
                invoice_id=inv.id,
                amount_minor=0,
                currency="USD",
                decision_id=dec_id,
                actor="operator@example.com",
            )


def test_invoice_and_payment_eligibility(store):
    """Invariants I7 & I8: DRAFT, CANCELLED, PAID invoices and non-COMPLETED payments are ineligible."""
    with store.session() as session:
        inv_repo = InvoiceRepository(session)
        pay_repo = PaymentRepository(session)

        # Ineligible Invoices (I8)
        draft_inv = inv_repo.upsert_invoice(
            tenant_id="test_tenant",
            provider="paypal",
            provider_invoice_id="INV-DRAFT",
            invoice_number="INV-D",
            total_minor=5000,
            currency="USD",
            status="DRAFT",
        )
        with pytest.raises(IneligibleInvoiceError):
            inv_repo.validate_eligibility_for_allocation(draft_inv)

        cancelled_inv = inv_repo.upsert_invoice(
            tenant_id="test_tenant",
            provider="paypal",
            provider_invoice_id="INV-CANCELLED",
            invoice_number="INV-C",
            total_minor=5000,
            currency="USD",
            status="CANCELLED",
        )
        with pytest.raises(IneligibleInvoiceError):
            inv_repo.validate_eligibility_for_allocation(cancelled_inv)

        paid_inv = inv_repo.upsert_invoice(
            tenant_id="test_tenant",
            provider="paypal",
            provider_invoice_id="INV-PAID",
            invoice_number="INV-P",
            total_minor=5000,
            currency="USD",
            status="PAID",
        )
        with pytest.raises(IneligibleInvoiceError):
            inv_repo.validate_eligibility_for_allocation(paid_inv)

        # Ineligible Payments (I7)
        pending_pay = pay_repo.upsert_payment(
            tenant_id="test_tenant",
            provider="paypal",
            provider_payment_id="PAY-PENDING",
            amount_minor=5000,
            currency="USD",
            status="PENDING",
        )
        with pytest.raises(IneligiblePaymentError):
            pay_repo.validate_eligibility_for_allocation(pending_pay)

        denied_pay = pay_repo.upsert_payment(
            tenant_id="test_tenant",
            provider="paypal",
            provider_payment_id="PAY-DENIED",
            amount_minor=5000,
            currency="USD",
            status="DENIED",
        )
        with pytest.raises(IneligiblePaymentError):
            pay_repo.validate_eligibility_for_allocation(denied_pay)

        refunded_pay = pay_repo.upsert_payment(
            tenant_id="test_tenant",
            provider="paypal",
            provider_payment_id="PAY-REFUNDED",
            amount_minor=5000,
            currency="USD",
            status="REFUNDED",
        )
        with pytest.raises(IneligiblePaymentError):
            pay_repo.validate_eligibility_for_allocation(refunded_pay)

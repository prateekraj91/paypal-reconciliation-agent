"""Comprehensive regression tests for cross-tenant isolation and fail-closed security.

Verifies:
1. Every entity is strictly scoped to its tenant.
2. Querying or accessing another tenant's records by ID or reference fails closed (None or TenantIsolationError).
3. Cross-tenant allocation creation (e.g. allocating Tenant A's payment to Tenant B's invoice) is rejected with EntityNotFoundError.
4. Cross-tenant proposal approval, rejection, or escalation fails closed with EntityNotFoundError.
5. Cross-tenant audit event streams and cryptographic hash chains are strictly segregated.
"""

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
    ConnectionRepository,
    ProviderRecordRepository,
    InvoiceRepository,
    PaymentRepository,
    ReconciliationRunRepository,
    ProposalRepository,
    AllocationRepository,
    DecisionRepository,
    AuditRepository,
    EntityNotFoundError,
    TenantIsolationError,
    ConflictError,
)
from reconcile_store.approval import ApprovalService


@pytest.fixture
def multi_tenant_db():
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
        t_repo.create_tenant("tenant_a", "Tenant Alpha")
        t_repo.create_tenant("tenant_b", "Tenant Beta")
        session.commit()

    yield db

    db.dispose()
    if os.path.exists(path):
        os.unlink(path)


def test_cross_tenant_invoice_isolation(multi_tenant_db):
    """Verify Tenant B cannot view or fetch Tenant A's invoices."""
    db = multi_tenant_db
    with db.session() as session:
        inv_repo = InvoiceRepository(session)
        inv_a = inv_repo.upsert_invoice(
            tenant_id="tenant_a",
            provider="paypal",
            provider_invoice_id="INV-A-100",
            invoice_number="INV-100",
            total_minor=50000,
            currency="USD",
            customer_name="Alpha Client",
        )
        session.commit()
        inv_a_id = inv_a.id

    with db.session() as session:
        inv_repo = InvoiceRepository(session)

        # 1. Querying by provider ID for tenant B returns None
        assert inv_repo.get_by_provider_id("tenant_b", "paypal", "INV-A-100") is None

        # 2. Querying by internal ID with tenant B context raises TenantIsolationError
        with pytest.raises(TenantIsolationError):
            inv_repo.get_invoice("tenant_b", inv_a_id)

        # 3. Listing invoices for tenant B returns empty
        assert len(inv_repo.list_invoices("tenant_b")) == 0
        assert len(inv_repo.list_invoices("tenant_a")) == 1

        # 4. Balance calculations fail closed
        with pytest.raises(TenantIsolationError):
            inv_repo.get_ledger_outstanding_minor("tenant_b", inv_a_id)


def test_cross_tenant_payment_isolation(multi_tenant_db):
    """Verify Tenant B cannot view or access Tenant A's payments."""
    db = multi_tenant_db
    with db.session() as session:
        pay_repo = PaymentRepository(session)
        pay_a = pay_repo.upsert_payment(
            tenant_id="tenant_a",
            provider="paypal",
            provider_payment_id="PAY-A-200",
            amount_minor=50000,
            currency="USD",
            payer_name="Alpha Payer",
            reference="INV-100",
        )
        session.commit()
        pay_a_id = pay_a.id

    with db.session() as session:
        pay_repo = PaymentRepository(session)

        # 1. Querying by provider payment ID for tenant B returns None
        assert pay_repo.get_by_provider_id("tenant_b", "paypal", "PAY-A-200") is None

        # 2. Querying by internal ID with tenant B context raises TenantIsolationError
        with pytest.raises(TenantIsolationError):
            pay_repo.get_payment("tenant_b", pay_a_id)

        # 3. Listing payments for tenant B returns empty
        assert len(pay_repo.list_payments("tenant_b")) == 0
        assert len(pay_repo.list_payments("tenant_a")) == 1

        # 4. Unapplied cash calculations fail closed
        with pytest.raises(TenantIsolationError):
            pay_repo.get_unapplied_cash_minor("tenant_b", pay_a_id)


def test_cross_tenant_provider_record_isolation(multi_tenant_db):
    """Verify raw provider records are strictly segregated per tenant."""
    db = multi_tenant_db
    with db.session() as session:
        prec_repo = ProviderRecordRepository(session)
        rec = prec_repo.store_record(
            tenant_id="tenant_a",
            provider="paypal",
            record_kind="invoice",
            provider_record_id="RAW-INV-A",
            raw_payload={"secret_field": "alpha_data"},
        )
        session.commit()
        rec_id = rec.id

    with db.session() as session:
        prec_repo = ProviderRecordRepository(session)

        # Tenant B cannot retrieve Tenant A's provider record
        assert prec_repo.get_record("tenant_b", "paypal", "invoice", "RAW-INV-A") is None

        with pytest.raises(TenantIsolationError):
            prec_repo.get_record_by_id("tenant_b", rec_id)


def test_cross_tenant_allocation_creation_rejected(multi_tenant_db):
    """Verify attempting to allocate across tenants fails closed."""
    db = multi_tenant_db
    with db.session() as session:
        inv_repo = InvoiceRepository(session)
        pay_repo = PaymentRepository(session)
        dec_repo = DecisionRepository(session)
        run_repo = ReconciliationRunRepository(session)
        prop_repo = ProposalRepository(session)

        # Setup Tenant A invoice and payment
        inv_a = inv_repo.upsert_invoice(
            tenant_id="tenant_a",
            provider="paypal",
            provider_invoice_id="INV-A-1",
            invoice_number="INV-A1",
            total_minor=10000,
            currency="USD",
        )
        pay_a = pay_repo.upsert_payment(
            tenant_id="tenant_a",
            provider="paypal",
            provider_payment_id="PAY-A-1",
            amount_minor=10000,
            currency="USD",
        )
        run_a = run_repo.create_run(tenant_id="tenant_a", input_snapshot_id="s_a", matcher_name="m1")
        prop_a = prop_repo.create_proposal(
            tenant_id="tenant_a",
            run_id=run_a.id,
            payment_id=pay_a.id,
            outcome="PROPOSED",
        )
        dec_a = dec_repo.record_decision(
            tenant_id="tenant_a",
            proposal_id=prop_a.id,
            decision_type="APPROVED",
            actor="admin@a.com",
            reason="Approved",
            idempotency_key="key-a-dec",
        )

        # Setup Tenant B invoice and payment
        inv_b = inv_repo.upsert_invoice(
            tenant_id="tenant_b",
            provider="paypal",
            provider_invoice_id="INV-B-1",
            invoice_number="INV-B1",
            total_minor=10000,
            currency="USD",
        )
        pay_b = pay_repo.upsert_payment(
            tenant_id="tenant_b",
            provider="paypal",
            provider_payment_id="PAY-B-1",
            amount_minor=10000,
            currency="USD",
        )
        run_b = run_repo.create_run(tenant_id="tenant_b", input_snapshot_id="s_b", matcher_name="m1")
        prop_b = prop_repo.create_proposal(
            tenant_id="tenant_b",
            run_id=run_b.id,
            payment_id=pay_b.id,
            outcome="PROPOSED",
        )
        dec_b = dec_repo.record_decision(
            tenant_id="tenant_b",
            proposal_id=prop_b.id,
            decision_type="APPROVED",
            actor="admin@b.com",
            reason="Approved",
            idempotency_key="key-b-dec",
        )
        session.commit()

        inv_a_id, pay_a_id, dec_a_id = inv_a.id, pay_a.id, dec_a.id
        inv_b_id, pay_b_id, dec_b_id = inv_b.id, pay_b.id, dec_b.id

    with db.session() as session:
        alloc_repo = AllocationRepository(session)

        # Attack 1: Tenant B tries to create allocation using Tenant A's payment
        with pytest.raises(EntityNotFoundError) as exc:
            alloc_repo.create_allocation(
                tenant_id="tenant_b",
                payment_id=pay_a_id,
                invoice_id=inv_b_id,
                amount_minor=10000,
                currency="USD",
                decision_id=dec_b_id,
                actor="attacker@b.com",
            )
        assert "not found for tenant" in str(exc.value)

        # Attack 2: Tenant B tries to create allocation using Tenant A's invoice
        with pytest.raises(EntityNotFoundError) as exc:
            alloc_repo.create_allocation(
                tenant_id="tenant_b",
                payment_id=pay_b_id,
                invoice_id=inv_a_id,
                amount_minor=10000,
                currency="USD",
                decision_id=dec_b_id,
                actor="attacker@b.com",
            )
        assert "not found for tenant" in str(exc.value)

        # Attack 3: Tenant A tries to create allocation using Tenant B's decision
        with pytest.raises(EntityNotFoundError) as exc:
            alloc_repo.create_allocation(
                tenant_id="tenant_a",
                payment_id=pay_a_id,
                invoice_id=inv_a_id,
                amount_minor=10000,
                currency="USD",
                decision_id=dec_b_id,
                actor="attacker@a.com",
            )
        assert "not found for tenant" in str(exc.value)


def test_cross_tenant_approval_service_fails_closed(multi_tenant_db):
    """Verify ApprovalService refuses to approve, reject, or escalate another tenant's proposal."""
    db = multi_tenant_db
    with db.session() as session:
        inv_repo = InvoiceRepository(session)
        pay_repo = PaymentRepository(session)
        run_repo = ReconciliationRunRepository(session)
        prop_repo = ProposalRepository(session)

        inv_a = inv_repo.upsert_invoice(
            tenant_id="tenant_a",
            provider="paypal",
            provider_invoice_id="INV-A-SERVICE",
            invoice_number="INV-AS",
            total_minor=20000,
            currency="USD",
        )
        pay_a = pay_repo.upsert_payment(
            tenant_id="tenant_a",
            provider="paypal",
            provider_payment_id="PAY-A-SERVICE",
            amount_minor=20000,
            currency="USD",
            reference="INV-AS",
        )
        run_a = run_repo.create_run(tenant_id="tenant_a", input_snapshot_id="s_a", matcher_name="m1")
        prop_a = prop_repo.create_proposal(
            tenant_id="tenant_a",
            run_id=run_a.id,
            payment_id=pay_a.id,
            outcome="PROPOSED",
            evidence=["REFERENCE_MATCH", "AMOUNT_EXACT"],
            allocations=[{"invoice_id": inv_a.id, "amount_minor": 20000, "currency": "USD"}],
        )
        session.commit()
        prop_a_id = prop_a.id

    # Tenant B attempts to approve Tenant A's proposal
    with db.session() as session:
        svc = ApprovalService(session)
        with pytest.raises(EntityNotFoundError):
            svc.approve_proposal(
                tenant_id="tenant_b",
                proposal_id=prop_a_id,
                actor="operator@b.com",
                idempotency_key="key-b-rogue-approval",
            )

        with pytest.raises(EntityNotFoundError):
            svc.reject_proposal(
                tenant_id="tenant_b",
                proposal_id=prop_a_id,
                actor="operator@b.com",
                idempotency_key="key-b-rogue-rejection",
                reason="Malicious rejection",
            )

        with pytest.raises(EntityNotFoundError):
            svc.escalate_proposal(
                tenant_id="tenant_b",
                proposal_id=prop_a_id,
                actor="operator@b.com",
                idempotency_key="key-b-rogue-escalate",
                reason="Malicious escalation",
            )


def test_cross_tenant_audit_chains_independent(multi_tenant_db):
    """Verify audit hash chains are completely segregated across tenants."""
    db = multi_tenant_db
    with db.session() as session:
        audit_repo = AuditRepository(session)

        # Tenant A appends event 1
        e_a1 = audit_repo.append_event(
            tenant_id="tenant_a",
            event_type="TEST_EVENT_A1",
            actor="user_a",
            subject_type="test",
            subject_id="sub_a1",
            details={"tenant": "A"},
        )
        assert e_a1.sequence_number == 1

        # Tenant B appends event 1 (starts at sequence 1 with GENESIS_HASH independently)
        e_b1 = audit_repo.append_event(
            tenant_id="tenant_b",
            event_type="TEST_EVENT_B1",
            actor="user_b",
            subject_type="test",
            subject_id="sub_b1",
            details={"tenant": "B"},
        )
        assert e_b1.sequence_number == 1
        assert e_b1.prev_hash == e_a1.prev_hash  # Both start at GENESIS_HASH

        # Event hashes differ because canonical payload includes tenant_id
        assert e_a1.event_hash != e_b1.event_hash
        session.commit()

    with db.session() as session:
        audit_repo = AuditRepository(session)

        # Verify chains independently
        audit_repo.verify_tenant_chain("tenant_a")
        audit_repo.verify_tenant_chain("tenant_b")

        # Tenant A list only contains Tenant A
        events_a = audit_repo.list_events("tenant_a")
        assert len(events_a) == 1
        assert events_a[0].event_type == "TEST_EVENT_A1"

        events_b = audit_repo.list_events("tenant_b")
        assert len(events_b) == 1
        assert events_b[0].event_type == "TEST_EVENT_B1"

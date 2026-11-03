"""Tests for the Atomic Approval Engine."""

from __future__ import annotations

import os
import tempfile
import pytest
from datetime import datetime, timezone, date
from unittest.mock import patch

from alembic.config import Config
from alembic import command

from reconcile_store.config import DatabaseConfig
from reconcile_store.db import DatabaseEngine
from reconcile_store.repositories import (
    TenantRepository,
    InvoiceRepository,
    PaymentRepository,
    ReconciliationRunRepository,
    ProposalRepository,
    AllocationRepository,
    DecisionRepository,
    AuditRepository,
    ConflictError,
    StaleVersionError,
)
from reconcile_store.approval import ApprovalService, VerificationFailedError, ApprovalError
from reconcile_store.repositories.invoice_repo import IneligibleInvoiceError
from reconcile_store.repositories.payment_repo import IneligiblePaymentError
from reconcile_store.schema import AllocationRecord, DecisionRecord, AuditEventRecord


@pytest.fixture
def approval_env():
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
        t_repo.create_tenant("tenant_appr", "Approval Merchant")
        session.commit()

    yield db

    db.dispose()
    if os.path.exists(path):
        os.unlink(path)


def _setup_test_proposal(session, amount: int = 10000, reference: str = "INV-101", pay_status: str = "COMPLETED", inv_status: str = "SENT"):
    inv_repo = InvoiceRepository(session)
    pay_repo = PaymentRepository(session)
    run_repo = ReconciliationRunRepository(session)
    prop_repo = ProposalRepository(session)

    inv = inv_repo.upsert_invoice(
        tenant_id="tenant_appr",
        provider="paypal",
        provider_invoice_id=f"INV-PAYPAL-{reference}",
        invoice_number=reference,
        total_minor=amount,
        currency="USD",
        status=inv_status,
        customer_name="John Doe",
    )
    pay = pay_repo.upsert_payment(
        tenant_id="tenant_appr",
        provider="paypal",
        provider_payment_id=f"PAY-PAYPAL-{reference}",
        amount_minor=amount,
        currency="USD",
        status=pay_status,
        reference=reference,
        payer_name="John Doe",
    )
    run = run_repo.create_run(
        tenant_id="tenant_appr",
        input_snapshot_id="snap-1",
        matcher_name="deterministic_v1",
    )
    prop = prop_repo.create_proposal(
        tenant_id="tenant_appr",
        run_id=run.id,
        payment_id=pay.id,
        outcome="PROPOSED",
        evidence=["REFERENCE_MATCH", "AMOUNT_EXACT"],
        allocations=[{
            "invoice_id": inv.id,
            "amount_minor": amount,
            "currency": "USD",
        }],
    )
    session.commit()
    return inv, pay, prop


def test_valid_proposal_approved_atomically(approval_env):
    """Test standard valid proposal approval creates decision, allocation, audit event, and updates state."""
    db = approval_env
    with db.session() as session:
        inv, pay, prop = _setup_test_proposal(session, 10000)

    with db.session() as session:
        service = ApprovalService(session)
        result = service.approve_proposal(
            tenant_id="tenant_appr",
            proposal_id=prop.id,
            actor="admin@company.com",
            idempotency_key="idemp-key-101",
            reason="Verified and approved",
        )
        assert result.status == "APPROVED"
        assert not result.is_idempotent_replay
        assert len(result.allocation_ids) == 1
        assert result.audit_event_id != ""

    # Verify state in fresh session
    with db.session() as session:
        alloc_repo = AllocationRepository(session)
        inv_repo = InvoiceRepository(session)
        pay_repo = PaymentRepository(session)
        audit_repo = AuditRepository(session)

        # Invoice balance is 0
        assert inv_repo.get_ledger_outstanding_minor("tenant_appr", inv.id) == 0
        # Payment unapplied is 0
        assert pay_repo.get_unapplied_cash_minor("tenant_appr", pay.id) == 0

        # Allocation exists
        allocs = alloc_repo.get_allocations_for_payment("tenant_appr", pay.id)
        assert len(allocs) == 1
        assert allocs[0].amount_minor == 10000
        assert allocs[0].actor == "admin@company.com"

        # Audit event exists and verifies clean
        events = audit_repo.list_events("tenant_appr")
        assert len(events) == 1
        assert events[0].event_type == "PROPOSAL_APPROVED"
        audit_repo.verify_tenant_chain("tenant_appr")


def test_idempotent_approval_replay_and_conflict(approval_env):
    """Test repeated submission returns existing approval without duplicating ledger or audit effects."""
    db = approval_env
    with db.session() as session:
        _, _, prop = _setup_test_proposal(session, 5000)

    # First call
    with db.session() as session:
        service = ApprovalService(session)
        res1 = service.approve_proposal(
            tenant_id="tenant_appr",
            proposal_id=prop.id,
            actor="admin@company.com",
            idempotency_key="idemp-key-replay",
        )
        assert not res1.is_idempotent_replay

    # Second call with same idempotency key and proposal
    with db.session() as session:
        service = ApprovalService(session)
        res2 = service.approve_proposal(
            tenant_id="tenant_appr",
            proposal_id=prop.id,
            actor="admin@company.com",
            idempotency_key="idemp-key-replay",
        )
        assert res2.is_idempotent_replay
        assert res2.decision_id == res1.decision_id
        assert res2.allocation_ids == res1.allocation_ids

    # Verify that only ONE allocation and ONE audit event exist
    with db.session() as session:
        alloc_repo = AllocationRepository(session)
        audit_repo = AuditRepository(session)
        allocs = alloc_repo.get_all_allocations("tenant_appr")
        assert len(allocs) == 1
        events = audit_repo.list_events("tenant_appr")
        assert len(events) == 1

    # Conflicting reuse of idempotency key with different proposal must raise ConflictError
    with db.session() as session:
        _, _, prop2 = _setup_test_proposal(session, 7000, reference="INV-202")

    with db.session() as session:
        service = ApprovalService(session)
        with pytest.raises(ConflictError):
            service.approve_proposal(
                tenant_id="tenant_appr",
                proposal_id=prop2.id,
                actor="admin@company.com",
                idempotency_key="idemp-key-replay",  # already used for prop 1
            )


def test_stale_version_rejection(approval_env):
    """Test optimistic locking: stale payment or invoice versions reject approval."""
    db = approval_env
    with db.session() as session:
        inv, pay, prop = _setup_test_proposal(session, 8000)

    # Expected payment version mismatch
    with db.session() as session:
        service = ApprovalService(session)
        with pytest.raises(StaleVersionError):
            service.approve_proposal(
                tenant_id="tenant_appr",
                proposal_id=prop.id,
                actor="admin@company.com",
                idempotency_key="key-stale-pay",
                expected_payment_version=99,  # actual is 1
            )

    # Expected invoice version mismatch
    with db.session() as session:
        service = ApprovalService(session)
        with pytest.raises(StaleVersionError):
            service.approve_proposal(
                tenant_id="tenant_appr",
                proposal_id=prop.id,
                actor="admin@company.com",
                idempotency_key="key-stale-inv",
                expected_invoice_versions={inv.id: 99},
            )


def test_independent_verifier_rerun_rejection(approval_env):
    """Test that independent verifier reruns inside the approval transaction and fails on violations."""
    db = approval_env
    with db.session() as session:
        # Create invoice and payment with mismatched references so verifier will fail
        inv, pay, prop = _setup_test_proposal(session, 5000, reference="INV-REAL")

        # Mutate invoice status to CANCELLED in DB to make verifier fail
        inv_record = session.get(type(inv), inv.id)
        inv_record.status = "CANCELLED"
        session.commit()

    with db.session() as session:
        service = ApprovalService(session)
        with pytest.raises((IneligibleInvoiceError, VerificationFailedError)):
            service.approve_proposal(
                tenant_id="tenant_appr",
                proposal_id=prop.id,
                actor="admin@company.com",
                idempotency_key="key-inv-fail",
            )

    # Ensure no partial allocation or decision was saved
    with db.session() as session:
        alloc_repo = AllocationRepository(session)
        assert len(alloc_repo.get_all_allocations("tenant_appr")) == 0


def test_rollback_on_audit_failure(approval_env):
    """STEP 10 verification: if audit event insertion fails, entire approval rolls back completely."""
    db = approval_env
    with db.session() as session:
        inv, pay, prop = _setup_test_proposal(session, 5000)

    with db.session() as session:
        service = ApprovalService(session)
        # Mock audit repository to raise an exception during append_event
        with patch.object(service.audit_repo, "append_event", side_effect=RuntimeError("Disk failure")):
            with pytest.raises(RuntimeError) as exc_info:
                service.approve_proposal(
                    tenant_id="tenant_appr",
                    proposal_id=prop.id,
                    actor="admin@company.com",
                    idempotency_key="key-rollback",
                )
            assert "Disk failure" in str(exc_info.value)

    # Confirm that NOTHING was committed to the ledger or decisions
    with db.session() as session:
        alloc_repo = AllocationRepository(session)
        dec_repo = DecisionRepository(session)
        audit_repo = AuditRepository(session)

        assert len(alloc_repo.get_all_allocations("tenant_appr")) == 0
        assert dec_repo.get_by_idempotency_key("tenant_appr", "key-rollback") is None
        assert len(audit_repo.list_events("tenant_appr")) == 0


def test_rejection_and_escalation_decisions(approval_env):
    """Test recording human REJECTED and ESCALATED decisions with audit events."""
    db = approval_env
    with db.session() as session:
        _, _, prop1 = _setup_test_proposal(session, 3000, reference="INV-REJ")
        _, _, prop2 = _setup_test_proposal(session, 4000, reference="INV-ESC")

    with db.session() as session:
        service = ApprovalService(session)
        # Reject prop1
        dec_rej = service.reject_proposal(
            tenant_id="tenant_appr",
            proposal_id=prop1.id,
            actor="analyst@company.com",
            idempotency_key="key-rej-1",
            reason="Customer disputed line items",
        )
        assert dec_rej.decision_type == "REJECTED"

        # Escalate prop2
        dec_esc = service.escalate_proposal(
            tenant_id="tenant_appr",
            proposal_id=prop2.id,
            actor="analyst@company.com",
            idempotency_key="key-esc-1",
            reason="Requires manager sign-off",
        )
        assert dec_esc.decision_type == "ESCALATED"

    # Verify audit chain integrity
    with db.session() as session:
        audit_repo = AuditRepository(session)
        events = audit_repo.list_events("tenant_appr")
        assert len(events) == 2
        assert events[0].event_type == "PROPOSAL_REJECTED"
        assert events[1].event_type == "PROPOSAL_ESCALATED"
        audit_repo.verify_tenant_chain("tenant_appr")

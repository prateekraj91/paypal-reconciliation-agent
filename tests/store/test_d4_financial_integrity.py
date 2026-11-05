"""Comprehensive tests for D4 Financial Integrity, Provider Settlements, and Evidence Preservation.

Verifies:
1. Provider settlements separated from application allocations.
2. Provider settlements reduce invoice outstanding balances.
3. Application allocations require valid approved decisions.
4. Provider settlements cannot be approved again as duplicate allocations.
5. Approval capacity checks consider existing provider settlements.
6. Every invoice and payment has immutable source evidence.
7. Embedded payments preserve raw source payloads.
8. Repeat sync creates no duplicates (idempotency).
9. Audit-chain verification remains valid.
"""

from __future__ import annotations

import json
import os
import tempfile
import uuid
import pytest
from datetime import datetime, timezone, date
from sqlalchemy import text, select

from alembic.config import Config
from alembic import command

from reconcile_store.config import DatabaseConfig
from reconcile_store.db import DatabaseEngine
from reconcile_store.schema import (
    Base,
    TenantRecord,
    InvoiceRecord,
    PaymentRecord,
    ProviderSettlementRecord,
    AllocationRecord,
    DecisionRecord,
    ProposalRecord,
    ProposalAllocationRecord,
    ProviderRecord,
    VerificationRecord,
)
from reconcile_store.repositories import (
    InvoiceRepository,
    PaymentRepository,
    ProviderSettlementRepository,
    AllocationRepository,
    AuditRepository,
    ProviderRecordRepository,
    VerificationRepository,
    ConflictError,
)
from reconcile_store.approval import ApprovalService, LedgerConstraintError
from reconcile_store.workflow import StorageWorkflow
from reconcile.models import (
    Invoice,
    InvoiceStatus,
    Payment,
    PaymentStatus,
    PaymentAllocation,
    AllocationStatus,
)


@pytest.fixture
def db_engine():
    """Create a temporary SQLite database engine with all migrations applied."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    url = f"sqlite:///{path}"
    os.environ["RECONCILE_DB_URL"] = url

    alembic_cfg = Config("alembic.ini")
    alembic_cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(alembic_cfg, "head")

    engine = DatabaseEngine(DatabaseConfig(url=url))
    yield engine

    engine.dispose()
    if os.path.exists(path):
        os.unlink(path)


def test_provider_settlements_separated_from_allocations(db_engine):
    """Test that externally reported settlements are stored exclusively in provider_settlements,
    and never in the application allocations ledger without operator approval.
    """
    tenant_id = "tenant_test_separation"
    with db_engine.session() as session:
        workflow = StorageWorkflow(session)
        workflow.tenant_repo.create_tenant(tenant_id, "Test Separation Tenant")

        # Ingest invoice
        inv = Invoice(
            invoice_id="INV-EXT-01",
            number="RECON-EXT-01",
            amount_cents=20000,
            currency="USD",
            due_amount_cents=10000,
            status=InvoiceStatus.PARTIALLY_PAID,
            customer_name="Separation Customer",
            issued_on=date(2026, 11, 1),
            due_on=date(2026, 11, 30),
        )
        workflow.ingest_invoices(tenant_id, [inv])

        # Ingest provider settlement
        linked = PaymentAllocation(
            payment_id="EXTR-TXN-999",
            invoice_id="INV-EXT-01",
            amount_cents=10000,
            currency="USD",
            status=AllocationStatus.APPROVED,
        )
        raw_txn = {"transaction_id": "EXTR-TXN-999", "amount": {"value": "100.00", "currency_code": "USD"}}
        workflow.ingest_provider_settlements(
            tenant_id=tenant_id,
            allocations=[linked],
            raw_payloads={"EXTR-TXN-999": raw_txn},
        )

        # Confirm allocations table is EMPTY
        alloc_repo = AllocationRepository(session)
        assert len(alloc_repo.get_all_allocations(tenant_id)) == 0

        # Confirm provider_settlements table has the settlement
        sett_repo = ProviderSettlementRepository(session)
        settlements = sett_repo.get_all_settlements(tenant_id)
        assert len(settlements) == 1
        assert settlements[0].provider_settlement_id == "EXTR-TXN-999"
        assert settlements[0].amount_minor == 10000
        assert settlements[0].source_record_id is not None

        # Confirm audit event was recorded with actor=paypal:settlement
        audit_repo = AuditRepository(session)
        events = list(audit_repo.list_events(tenant_id))
        assert len(events) == 1
        assert events[0].event_type == "PROVIDER_SETTLEMENT_RECORDED"
        assert events[0].actor == "paypal:settlement"
        audit_repo.verify_tenant_chain(tenant_id)


def test_provider_settlements_reduce_outstanding_balances(db_engine):
    """Test that provider settlements reduce invoice outstanding balance without double counting."""
    tenant_id = "tenant_test_balance"
    with db_engine.session() as session:
        workflow = StorageWorkflow(session)
        workflow.tenant_repo.create_tenant(tenant_id, "Test Balance Tenant")

        # Ingest invoice of $300 (30000 minor)
        inv = Invoice(
            invoice_id="INV-BAL-01",
            number="RECON-BAL-01",
            amount_cents=30000,
            currency="USD",
            due_amount_cents=30000,
            status=InvoiceStatus.SENT,
            customer_name="Balance Customer",
            issued_on=date(2026, 11, 1),
            due_on=date(2026, 11, 30),
        )
        workflow.ingest_invoices(tenant_id, [inv])

        inv_rec = workflow.invoice_repo.get_by_provider_id(tenant_id, "paypal", "INV-BAL-01")
        assert inv_rec is not None
        assert workflow.invoice_repo.get_ledger_outstanding_minor(tenant_id, inv_rec.id) == 30000

        # Ingest provider settlement of $100 (10000 minor)
        linked = PaymentAllocation(
            payment_id="EXTR-TXN-BAL-1",
            invoice_id="INV-BAL-01",
            amount_cents=10000,
            currency="USD",
            status=AllocationStatus.APPROVED,
        )
        workflow.ingest_provider_settlements(tenant_id, [linked])

        # Outstanding balance must now be 30000 - 10000 = 20000
        assert workflow.invoice_repo.get_ledger_outstanding_minor(tenant_id, inv_rec.id) == 20000


def test_allocations_require_valid_approved_decisions(db_engine):
    """Test that allocations strictly require a non-null decision_id."""
    tenant_id = "tenant_test_alloc_decision"
    with db_engine.session() as session:
        workflow = StorageWorkflow(session)
        workflow.tenant_repo.create_tenant(tenant_id, "Decision Tenant")

        inv_rec = workflow.invoice_repo.upsert_invoice(
            tenant_id=tenant_id,
            provider="paypal",
            provider_invoice_id="INV-REQ-01",
            invoice_number="RECON-REQ-01",
            customer_name="Req Customer",
            total_minor=10000,
            currency="USD",
            status="SENT",
        )
        pay_rec = workflow.payment_repo.upsert_payment(
            tenant_id=tenant_id,
            provider="paypal",
            provider_payment_id="PAY-REQ-01",
            amount_minor=10000,
            currency="USD",
            status="COMPLETED",
        )
        session.commit()

        # Direct database insert without decision_id must fail schema NOT NULL constraint
        with pytest.raises(Exception):
            session.execute(
                text(
                    "INSERT INTO allocations (id, tenant_id, payment_id, invoice_id, amount_minor, currency, "
                    "decision_id, actor, evidence_json, allocated_at) "
                    "VALUES ('alloc_no_dec', 'tenant_test_alloc_decision', :pid, :iid, 10000, 'USD', "
                    "NULL, 'operator', '[]', CURRENT_TIMESTAMP)"
                ),
                {"pid": pay_rec.id, "iid": inv_rec.id},
            )
            session.commit()
        session.rollback()


def test_capacity_checks_include_provider_settlements(db_engine):
    """Test that approval capacity checks correctly account for existing provider settlements."""
    tenant_id = "tenant_test_capacity"
    with db_engine.session() as session:
        workflow = StorageWorkflow(session)
        workflow.tenant_repo.create_tenant(tenant_id, "Capacity Tenant")

        # Invoice of $150
        inv = Invoice(
            invoice_id="INV-CAP-01",
            number="RECON-CAP-01",
            amount_cents=15000,
            currency="USD",
            status=InvoiceStatus.SENT,
            customer_name="Cap Customer",
            issued_on=date(2026, 11, 1),
            due_on=date(2026, 11, 30),
        )
        workflow.ingest_invoices(tenant_id, [inv])

        # Provider settlement of $100 already settled
        linked = PaymentAllocation(
            payment_id="EXTR-TXN-CAP-1",
            invoice_id="INV-CAP-01",
            amount_cents=10000,
            currency="USD",
            status=AllocationStatus.APPROVED,
        )
        workflow.ingest_provider_settlements(tenant_id, [linked])

        # Attempt to create an approval proposal for $100 on the same invoice (100 + 100 > 150)
        pay = Payment(
            payment_id="PAY-NEW-CAP-01",
            amount_cents=10000,
            currency="USD",
            status=PaymentStatus.COMPLETED,
            received_at=datetime.now(timezone.utc),
            payer_name="Cap Customer",
            reference="RECON-CAP-01",
        )
        workflow.ingest_payments(tenant_id, [pay])

        db_inv = workflow.invoice_repo.get_by_provider_id(tenant_id, "paypal", "INV-CAP-01")
        db_pay = workflow.payment_repo.get_by_provider_id(tenant_id, "paypal", "PAY-NEW-CAP-01")

        run = workflow.run_repo.create_run(
            tenant_id=tenant_id,
            input_snapshot_id="snap_cap",
            matcher_name="deterministic_v1",
        )
        proposal = workflow.proposal_repo.create_proposal(
            tenant_id=tenant_id,
            run_id=run.id,
            payment_id=db_pay.id,
            outcome="PROPOSED",
            evidence=["REFERENCE_MATCH", "AMOUNT_EXACT"],
            allocations=[{"invoice_id": db_inv.id, "amount_minor": 10000, "currency": "USD"}],
        )
        session.commit()

        approval_service = ApprovalService(session)
        # Must fail because available capacity is only 5000, proposed is 10000
        from reconcile_store.approval import VerificationFailedError
        with pytest.raises((LedgerConstraintError, VerificationFailedError)) as exc_info:
            approval_service.approve_proposal(
                tenant_id=tenant_id,
                proposal_id=proposal.id,
                actor="operator@example.com",
                idempotency_key="cap-key-01",
            )
        assert "exceeds" in str(exc_info.value).lower() or "capacity exceeded" in str(exc_info.value).lower()


def test_provider_settlement_cannot_be_approved_as_duplicate(db_engine):
    """Test that a provider settlement cannot later be approved again as an application allocation."""
    tenant_id = "tenant_test_dup_settle"
    with db_engine.session() as session:
        workflow = StorageWorkflow(session)
        workflow.tenant_repo.create_tenant(tenant_id, "Dup Settle Tenant")

        inv = Invoice(
            invoice_id="INV-DUP-01",
            number="RECON-DUP-01",
            amount_cents=10000,
            currency="USD",
            status=InvoiceStatus.SENT,
            customer_name="Dup Customer",
            issued_on=date(2026, 11, 1),
            due_on=date(2026, 11, 30),
        )
        workflow.ingest_invoices(tenant_id, [inv])

        linked = PaymentAllocation(
            payment_id="PAY-DUP-01",
            invoice_id="INV-DUP-01",
            amount_cents=10000,
            currency="USD",
            status=AllocationStatus.APPROVED,
        )
        workflow.ingest_provider_settlements(tenant_id, [linked])

        db_inv = workflow.invoice_repo.get_by_provider_id(tenant_id, "paypal", "INV-DUP-01")
        db_pay = workflow.payment_repo.get_by_provider_id(tenant_id, "paypal", "PAY-DUP-01")

        run = workflow.run_repo.create_run(
            tenant_id=tenant_id,
            input_snapshot_id="snap_dup",
            matcher_name="deterministic_v1",
        )
        proposal = workflow.proposal_repo.create_proposal(
            tenant_id=tenant_id,
            run_id=run.id,
            payment_id=db_pay.id,
            outcome="PROPOSED",
            evidence=["REFERENCE_MATCH", "AMOUNT_EXACT"],
            allocations=[{"invoice_id": db_inv.id, "amount_minor": 10000, "currency": "USD"}],
        )
        session.commit()

        approval_service = ApprovalService(session)
        from reconcile_store.approval import VerificationFailedError
        with pytest.raises((ConflictError, VerificationFailedError)) as exc_info:
            approval_service.approve_proposal(
                tenant_id=tenant_id,
                proposal_id=proposal.id,
                actor="operator@example.com",
                idempotency_key="dup-key-01",
            )
        assert "already" in str(exc_info.value).lower()


def test_evidence_preservation_and_idempotency(db_engine):
    """Test that invoices and payments store immutable provider records and repeat sync is idempotent."""
    tenant_id = "tenant_test_evidence"
    with db_engine.session() as session:
        workflow = StorageWorkflow(session)
        workflow.tenant_repo.create_tenant(tenant_id, "Evidence Tenant")

        raw_inv = {
            "id": "INV-EVID-01",
            "detail": {"invoice_number": "RECON-EVID-01", "currency_code": "USD"},
            "amount": {"value": "50.00", "currency_code": "USD"},
        }
        inv = Invoice(
            invoice_id="INV-EVID-01",
            number="RECON-EVID-01",
            amount_cents=5000,
            currency="USD",
            status=InvoiceStatus.SENT,
            customer_name="Evidence Customer",
            issued_on=date(2026, 11, 1),
            due_on=date(2026, 11, 30),
        )

        # Ingest invoice with raw evidence
        workflow.ingest_invoices(tenant_id, [inv], raw_payloads={"INV-EVID-01": raw_inv})

        inv_rec = workflow.invoice_repo.get_by_provider_id(tenant_id, "paypal", "INV-EVID-01")
        assert inv_rec is not None
        assert inv_rec.source_record_id is not None

        prec_repo = ProviderRecordRepository(session)
        prec = prec_repo.get_record_by_id(tenant_id, inv_rec.source_record_id)
        assert prec is not None
        assert prec.record_kind == "invoice"
        assert len(prec.content_hash) == 64

        # Repeat sync with identical payload must be idempotent
        workflow.ingest_invoices(tenant_id, [inv], raw_payloads={"INV-EVID-01": raw_inv})
        all_precs = session.scalars(select(ProviderRecord).where(ProviderRecord.tenant_id == tenant_id)).all()
        assert len(all_precs) == 1


def test_verification_state_and_no_auto_approval(db_engine):
    """Test verification-state lifecycle, verifications audit table, and zero auto-approval invariant:
    1. A proposal created directly defaults to NOT_VERIFIED.
    2. Running verify_proposals in memory returns passed=True, but does NOT mutate DB state.
    3. StorageWorkflow.run_reconciliation records the check into the verifications table and sets verifier_status="VERIFIED".
    4. Crucially: Passing verification NEVER creates allocations, decisions, or balance deductions (zero auto-approval).
    5. Only ApprovalService.approve_proposal creates APPROVED decision and records in allocations.
    """
    tenant_id = "tenant_test_verif_state"
    with db_engine.session() as session:
        workflow = StorageWorkflow(session)
        workflow.tenant_repo.create_tenant(tenant_id, "Verification State Tenant")

        # Ingest an invoice for $125.00
        inv = Invoice(
            invoice_id="INV-VERIF-01",
            number="RECON-V-001",
            amount_cents=12500,
            currency="USD",
            due_amount_cents=12500,
            status=InvoiceStatus.SENT,
            customer_name="Verification Customer",
            issued_on=date(2026, 11, 1),
            due_on=date(2026, 11, 30),
        )
        workflow.ingest_invoices(tenant_id, [inv])

        # Ingest a matching payment for $125.00
        pay = Payment(
            payment_id="PAY-VERIF-01",
            amount_cents=12500,
            currency="USD",
            status=PaymentStatus.COMPLETED,
            received_at=datetime.now(timezone.utc),
            payer_name="Verification Customer",
            reference="RECON-V-001",
        )
        workflow.ingest_payments(tenant_id, [pay])

        # 1. Run StorageWorkflow reconciliation
        run_rec, run_result = workflow.run_reconciliation(tenant_id=tenant_id)

        # Confirm run_result has 1 proposed and 1 passed verification
        assert len(run_result.proposed) == 1
        assert len(run_result.passed_verification) == 1
        assert run_result.verification_results[0].passed is True

        # 2. Check DB proposal state: MUST be VERIFIED
        props = workflow.proposal_repo.list_by_run(tenant_id, run_rec.id)
        assert len(props) == 1
        prop = props[0]
        assert prop.outcome == "PROPOSED"
        assert prop.verifier_status == "VERIFIED"

        # 3. Check verifications table: exactly 1 audit record linked to proposal
        ver_records = workflow.verification_repo.list_by_proposal(tenant_id, prop.id)
        assert len(ver_records) == 1
        assert ver_records[0].check_name == "verify_proposals"
        assert ver_records[0].passed is True
        assert ver_records[0].failure_reason is None

        # 4. CRITICAL INVARIANT: Passing verification NEVER means automatic approval!
        alloc_repo = AllocationRepository(session)
        assert len(alloc_repo.get_all_allocations(tenant_id)) == 0
        dec_stmt = select(DecisionRecord).where(DecisionRecord.tenant_id == tenant_id)
        assert len(session.scalars(dec_stmt).all()) == 0
        inv_rec = workflow.invoice_repo.get_by_provider_id(tenant_id, "paypal", "INV-VERIF-01")
        assert workflow.invoice_repo.get_ledger_outstanding_minor(tenant_id, inv_rec.id) == 12500

        # 5. Check direct create_proposal default state is NOT_VERIFIED
        standalone_prop = workflow.proposal_repo.create_proposal(
            tenant_id=tenant_id,
            run_id=run_rec.id,
            payment_id=prop.payment_id,
            outcome="PROPOSED",
            allocations=[{"invoice_id": inv_rec.id, "amount_minor": 12500, "currency": "USD"}],
        )
        assert standalone_prop.verifier_status == "NOT_VERIFIED"
        assert len(workflow.verification_repo.list_by_proposal(tenant_id, standalone_prop.id)) == 0

        # 6. Now perform explicit operator approval
        approval_service = ApprovalService(session)
        approval_result = approval_service.approve_proposal(
            tenant_id=tenant_id,
            proposal_id=prop.id,
            actor="compliance_officer@fintech.test",
            idempotency_key="verif-approve-key-001",
        )
        assert approval_result.status == "APPROVED"
        assert prop.verifier_status == "APPROVED"
        assert len(alloc_repo.get_all_allocations(tenant_id)) == 1
        assert workflow.invoice_repo.get_ledger_outstanding_minor(tenant_id, inv_rec.id) == 0


def test_provider_settlement_reconciliation_semantics_vs_application_approvals(db_engine):
    """Test that provider settlements are cleanly distinguished from application approvals:
    1. A provider settlement produces PROVIDER_SETTLED outcome, not DUPLICATE_EVENT.
    2. The proposal reason clearly states external provider settlement rather than ledger approval.
    3. The payment cannot be proposed or allocated again.
    4. Application allocations and decisions remain strictly 0.
    5. The invoice outstanding ledger balance is 0.
    """
    tenant_id = "tenant_test_settle_semantics"
    with db_engine.session() as session:
        workflow = StorageWorkflow(session)
        workflow.tenant_repo.create_tenant(tenant_id, "Settle Semantics Tenant")

        # Ingest an invoice
        inv = Invoice(
            invoice_id="INV-SEM-01",
            number="RECON-SEM-01",
            amount_cents=12500,
            currency="USD",
            due_amount_cents=0,
            status=InvoiceStatus.PAID,
            customer_name="Semantics Buyer",
            issued_on=date(2026, 11, 1),
            due_on=date(2026, 11, 15),
        )
        workflow.ingest_invoices(tenant_id, [inv])

        # Ingest provider settlement fact
        linked = PaymentAllocation(
            payment_id="PAY-SEM-01",
            invoice_id="INV-SEM-01",
            amount_cents=12500,
            currency="USD",
            status=AllocationStatus.APPROVED,
        )
        workflow.ingest_provider_settlements(tenant_id, [linked])

        # Run reconciliation
        run_rec, run_result = workflow.run_reconciliation(tenant_id=tenant_id)

        # Confirm outcome is PROVIDER_SETTLED, not DUPLICATE_EVENT or PROPOSED
        assert len(run_result.proposals) == 1
        prop = run_result.proposals[0]
        assert prop.outcome.value == "PROVIDER_SETTLED"
        assert prop.initial_status.value == "EXTERNALLY_RECONCILED"
        assert len(prop.reasons) == 1
        assert prop.reasons[0].code.value == "PROVIDER_SETTLED"
        assert "externally" in prop.reasons[0].detail or "provider" in prop.reasons[0].detail
        assert "ledger" not in prop.reasons[0].detail

        # Confirm DB persistence of proposal
        db_props = workflow.proposal_repo.list_by_run(tenant_id, run_rec.id)
        assert len(db_props) == 1
        assert db_props[0].outcome == "PROVIDER_SETTLED"
        assert json.loads(db_props[0].evidence_json) == ["PROVIDER_SETTLED"]

        # Confirm zero application allocations or decisions created
        alloc_repo = AllocationRepository(session)
        assert len(alloc_repo.get_all_allocations(tenant_id)) == 0
        dec_stmt = select(DecisionRecord).where(DecisionRecord.tenant_id == tenant_id)
        assert len(session.scalars(dec_stmt).all()) == 0

        # Confirm outstanding balance is 0
        inv_rec = workflow.invoice_repo.get_by_provider_id(tenant_id, "paypal", "INV-SEM-01")
        assert workflow.invoice_repo.get_ledger_outstanding_minor(tenant_id, inv_rec.id) == 0



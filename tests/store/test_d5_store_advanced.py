"""Storage tests for D5 Advanced Reconciliation features (refunds, overpayments, ambiguity certificates)."""

from datetime import date, datetime, timezone
import json
import pytest
from sqlalchemy import text

from reconcile_store.config import DatabaseConfig
from reconcile_store.db import DatabaseEngine
from reconcile_store.workflow import StorageWorkflow
from reconcile_store.schema import Base, RefundRecord, ProposalRecord, AuditEventRecord
from reconcile.models import (
    Invoice,
    InvoiceStatus,
    Payment,
    PaymentReversal,
    PaymentStatus,
    Refund,
    MatchOutcome,
)
from reconcile.advanced import AdvancedMatcher


@pytest.fixture
def db_session(tmp_path):
    db_file = tmp_path / "test_d5_store.db"
    db_url = f"sqlite:///{db_file}"
    engine_holder = DatabaseEngine(DatabaseConfig(url=db_url))
    Base.metadata.create_all(engine_holder.engine)
    with engine_holder.session() as session:
        yield session


def test_ingest_refunds_and_audit_event(db_session):
    workflow = StorageWorkflow(db_session)
    tenant_id = "t_test_d5_1"
    workflow.tenant_repo.create_tenant(tenant_id, "Test Tenant D5")

    # Ingest parent payment
    payment = Payment(
        payment_id="PAY-REF-PARENT-1",
        amount_cents=100000,
        currency="USD",
        received_at=datetime(2026, 11, 1, tzinfo=timezone.utc),
        payer_name="Refund Tester",
        status=PaymentStatus.COMPLETED,
    )
    workflow.ingest_payments(tenant_id, [payment])

    # Ingest refund
    refund = Refund(
        refund_id="REF-TEST-001",
        parent_payment_id="PAY-REF-PARENT-1",
        amount_cents=25000,
        currency="USD",
        refunded_at=datetime(2026, 11, 2, tzinfo=timezone.utc),
        reason="Partial buyer return",
    )
    refund_records = workflow.ingest_refunds(tenant_id, [refund])

    assert len(refund_records) == 1
    assert refund_records[0].provider_refund_id == "REF-TEST-001"
    assert refund_records[0].amount_minor == 25000
    assert refund_records[0].currency == "USD"

    # Verify audit event was logged
    events = list(workflow.audit_repo.list_events(tenant_id))
    refund_events = [e for e in events if e.event_type == "REFUND_RECORDED"]
    assert len(refund_events) == 1
    assert refund_events[0].subject_id == refund_records[0].id
    details = json.loads(refund_events[0].payload_json)["details"]
    assert details["amount_minor"] == 25000
    assert details["provider_refund_id"] == "REF-TEST-001"
    workflow.audit_repo.verify_tenant_chain(tenant_id)


def test_reconciliation_run_with_advanced_matcher_overpayment_and_ambiguity(db_session):
    workflow = StorageWorkflow(db_session)
    tenant_id = "t_test_d5_2"
    workflow.tenant_repo.create_tenant(tenant_id, "Test Tenant D5 2")

    # Ingest invoice: $100
    invoice = Invoice(
        invoice_id="INV-D5-100",
        number="INV-100",
        customer_name="Overpayer Inc",
        amount_cents=10000,
        currency="USD",
        issued_on=date(2026, 11, 1),
        due_on=date(2026, 11, 15),
        status=InvoiceStatus.SENT,
    )
    workflow.ingest_invoices(tenant_id, [invoice])

    # Payment of $120 referencing INV-100 (overpayment of $20)
    payment = Payment(
        payment_id="PAY-D5-120",
        amount_cents=12000,
        currency="USD",
        received_at=datetime(2026, 11, 2, tzinfo=timezone.utc),
        payer_name="Overpayer Inc",
        reference="INV-100",
        status=PaymentStatus.COMPLETED,
    )
    workflow.ingest_payments(tenant_id, [payment])

    matcher = AdvancedMatcher()
    run_rec, run_result = workflow.run_reconciliation(
        tenant_id=tenant_id,
        matcher=matcher,
        matcher_name="advanced_matcher_d5",
    )

    assert run_rec is not None
    assert len(run_result.proposals) == 1
    prop = run_result.proposals[0]
    assert prop.outcome is MatchOutcome.PROPOSED
    assert prop.excess_amount_cents == 2000

    # Query stored proposal record from DB
    db_proposals = list(workflow.proposal_repo.list_by_run(tenant_id, run_rec.id))
    assert len(db_proposals) == 1
    db_prop = db_proposals[0]
    assert db_prop.excess_amount_minor == 2000


def test_reconciliation_run_with_ambiguity_certificate_persisted(db_session):
    workflow = StorageWorkflow(db_session)
    tenant_id = "t_test_d5_3"
    workflow.tenant_repo.create_tenant(tenant_id, "Test Tenant D5 3")

    # Ingest two identical $50 invoices
    inv1 = Invoice(
        invoice_id="INV-TIE-A",
        number="INV-TIE-A",
        customer_name="Customer A",
        amount_cents=5000,
        currency="USD",
        issued_on=date(2026, 10, 1),
        due_on=date(2026, 10, 15),
        status=InvoiceStatus.SENT,
    )
    inv2 = Invoice(
        invoice_id="INV-TIE-B",
        number="INV-TIE-B",
        customer_name="Customer B",
        amount_cents=5000,
        currency="USD",
        issued_on=date(2026, 10, 1),
        due_on=date(2026, 10, 15),
        status=InvoiceStatus.SENT,
    )
    workflow.ingest_invoices(tenant_id, [inv1, inv2])

    # Unreferenced payment of $50
    payment = Payment(
        payment_id="PAY-TIE-50",
        amount_cents=5000,
        currency="USD",
        received_at=datetime(2026, 11, 2, tzinfo=timezone.utc),
        payer_name="Unknown Payer",
        reference="",
        status=PaymentStatus.COMPLETED,
    )
    workflow.ingest_payments(tenant_id, [payment])

    matcher = AdvancedMatcher()
    run_rec, run_result = workflow.run_reconciliation(
        tenant_id=tenant_id,
        matcher=matcher,
        matcher_name="advanced_matcher_d5",
    )

    db_proposals = list(workflow.proposal_repo.list_by_run(tenant_id, run_rec.id))
    assert len(db_proposals) == 1
    db_prop = db_proposals[0]
    assert db_prop.outcome == "AMBIGUOUS"
    assert db_prop.ambiguity_certificate_json is not None

    cert_data = json.loads(db_prop.ambiguity_certificate_json)
    assert cert_data["classification"] == "AMBIGUOUS"
    assert cert_data["requires_human_approval"] is True
    assert len(cert_data["candidate_invoice_ids"]) == 2


def test_ingest_reversals_and_audit_event(db_session):
    """Verify ingestion of involuntary payment reversals, dispute IDs, and audit hash chaining."""
    workflow = StorageWorkflow(db_session)
    tenant_id = "t_test_d5_reversals"
    workflow.tenant_repo.create_tenant(tenant_id, "Reversal Tenant")

    # Ingest parent payment
    payment = Payment(
        payment_id="PAY-REV-PARENT-1",
        amount_cents=75000,
        currency="USD",
        received_at=datetime(2026, 11, 1, tzinfo=timezone.utc),
        payer_name="Dispute Buyer",
        status=PaymentStatus.COMPLETED,
    )
    workflow.ingest_payments(tenant_id, [payment])

    # Ingest payment reversal (chargeback)
    reversal = PaymentReversal(
        reversal_id="REV-TEST-001",
        parent_payment_id="PAY-REV-PARENT-1",
        amount_cents=75000,
        currency="USD",
        reversed_at=datetime(2026, 11, 3, tzinfo=timezone.utc),
        reason="Unauthorized transaction claim",
        dispute_id="PP-D-998877",
    )
    reversal_records = workflow.ingest_reversals(tenant_id, [reversal])

    assert len(reversal_records) == 1
    rec = reversal_records[0]
    assert rec.provider_refund_id == "REV-TEST-001"
    assert rec.record_type == "REVERSAL"
    assert rec.dispute_id == "PP-D-998877"
    assert rec.amount_minor == 75000
    assert rec.currency == "USD"

    # Verify audit event was logged
    events = list(workflow.audit_repo.list_events(tenant_id))
    rev_events = [e for e in events if e.event_type == "PAYMENT_REVERSAL_RECORDED"]
    assert len(rev_events) == 1
    assert rev_events[0].subject_id == rec.id
    details = json.loads(rev_events[0].payload_json)["details"]
    assert details["provider_reversal_id"] == "REV-TEST-001"
    assert details["dispute_id"] == "PP-D-998877"
    workflow.audit_repo.verify_tenant_chain(tenant_id)


def test_out_of_order_reversal_creates_placeholder(db_session):
    """Verify out-of-order reversal arriving before parent payment creates placeholder without error."""
    workflow = StorageWorkflow(db_session)
    tenant_id = "t_test_d5_ooo"
    workflow.tenant_repo.create_tenant(tenant_id, "OOO Tenant")

    reversal = PaymentReversal(
        reversal_id="REV-OOO-001",
        parent_payment_id="PAY-NOT-YET-INGESTED",
        amount_cents=50000,
        currency="USD",
        reversed_at=datetime(2026, 11, 4, tzinfo=timezone.utc),
        dispute_id="PP-D-443322",
    )
    # Parent payment does not exist yet
    records = workflow.ingest_reversals(tenant_id, [reversal])
    assert len(records) == 1

    # Check that placeholder payment was created to satisfy FK constraint
    parent = workflow.payment_repo.get_by_provider_id(tenant_id, "paypal", "PAY-NOT-YET-INGESTED")
    assert parent is not None
    assert parent.amount_minor == 50000


def test_refund_restores_provider_settlement_balance_in_storage_workflow(db_session):
    """Verify refund targeting an external provider settlement restores invoice capacity in reconciliation."""
    workflow = StorageWorkflow(db_session)
    tenant_id = "t_test_d5_settle_refund"
    workflow.tenant_repo.create_tenant(tenant_id, "Settle Refund Tenant")

    # Ingest invoice: $500
    invoice = Invoice(
        invoice_id="INV-SETTLE-500",
        number="INV-500",
        customer_name="Settle Corp",
        amount_cents=50000,
        currency="USD",
        issued_on=date(2026, 11, 1),
        due_on=date(2026, 11, 15),
        status=InvoiceStatus.SENT,
    )
    workflow.ingest_invoices(tenant_id, [invoice])
    db_inv = workflow.invoice_repo.get_by_provider_id(tenant_id, "paypal", "INV-SETTLE-500")

    # Ingest parent payment for settlement
    pay_ext = Payment(
        payment_id="PAY-SETTLE-EXT-1",
        amount_cents=50000,
        currency="USD",
        received_at=datetime(2026, 11, 2, tzinfo=timezone.utc),
        payer_name="Settle Corp",
        status=PaymentStatus.COMPLETED,
    )
    workflow.ingest_payments(tenant_id, [pay_ext])
    db_pay_ext = workflow.payment_repo.get_by_provider_id(tenant_id, "paypal", "PAY-SETTLE-EXT-1")

    # Ingest provider settlement: $500 (fully settled by external PayPal payment)
    workflow.settlement_repo.upsert_settlement(
        tenant_id=tenant_id,
        provider="paypal",
        environment="sandbox",
        provider_settlement_id="SETTLE-500-EXT",
        invoice_id=db_inv.id,
        amount_minor=50000,
        currency="USD",
        payment_id=db_pay_ext.id,
    )

    # Now merchant refunds $200 of the settled invoice
    refund = Refund(
        refund_id="REF-SETTLE-200",
        parent_payment_id="PAY-SETTLE-EXT-1",
        amount_cents=20000,
        currency="USD",
        refunded_at=datetime(2026, 11, 3, tzinfo=timezone.utc),
        invoice_id="INV-SETTLE-500",
        target_type="SETTLEMENT",
    )
    workflow.ingest_refunds(tenant_id, [refund])

    # New incoming payment arrives for $200 referencing INV-500
    incoming_pay = Payment(
        payment_id="PAY-INCOMING-200",
        amount_cents=20000,
        currency="USD",
        received_at=datetime(2026, 11, 4, tzinfo=timezone.utc),
        payer_name="Settle Corp",
        reference="INV-500",
        status=PaymentStatus.COMPLETED,
    )
    workflow.ingest_payments(tenant_id, [incoming_pay])

    # Run reconciliation: because of the refund, the invoice has $200 available capacity!
    matcher = AdvancedMatcher()
    run_rec, run_result = workflow.run_reconciliation(
        tenant_id=tenant_id,
        matcher=matcher,
        matcher_name="advanced_matcher_d5",
    )

    # The incoming payment must be proposed for $200 (not rejected as over-allocation)
    incoming_proposals = [p for p in run_result.proposals if p.payment_id == "PAY-INCOMING-200"]
    assert len(incoming_proposals) == 1
    assert incoming_proposals[0].outcome is MatchOutcome.PROPOSED
    assert incoming_proposals[0].candidates[0].allocations[0].amount_cents == 20000

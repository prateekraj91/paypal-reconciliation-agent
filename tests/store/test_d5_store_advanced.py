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

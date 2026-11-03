"""Integrated end-to-end service execution demonstrating D3 Gate F requirements."""

from __future__ import annotations

import os
import json
import pathlib
import tempfile
import pytest

from alembic.config import Config
from alembic import command

from reconcile_store.config import DatabaseConfig
from reconcile_store.db import DatabaseEngine
from reconcile_store.workflow import StorageWorkflow
from reconcile_store.approval import ApprovalService
from reconcile_store.repositories import (
    TenantRepository,
    InvoiceRepository,
    PaymentRepository,
    AllocationRepository,
    AuditRepository,
    ProviderRecordRepository,
    ReconciliationRunRepository,
)
from reconcile.paypal import PayPalClient
from reconcile.models import Invoice, Payment, InvoiceStatus, PaymentStatus

FIXTURES_DIR = pathlib.Path(__file__).parent.parent / "fixtures"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES_DIR / f"{name}.json").read_text())


@pytest.fixture
def workflow_db():
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
        t_repo.create_tenant("tenant_demo", "Acme PayPal Store")
        session.commit()

    yield path, url, db

    db.dispose()
    if os.path.exists(path):
        os.unlink(path)


def test_complete_integrated_service_workflow(workflow_db):
    """Demonstrate the complete service-level D3 workflow (Gate F):

    1. Ingest real/sanitised PayPal invoice/payment records.
    2. Store immutable provider records with content hashes.
    3. Create a reconciliation run.
    4. Generate proposals and independently verify them.
    5. Approve a valid proposal through the approval service.
    6. Persist the ledger allocation.
    7. Append an audit event.
    8. Verify the audit chain.
    9. Restart the database / disconnect.
    10. Confirm that the financial state remains correct and untampered.
    """
    path, url, db = workflow_db
    client = PayPalClient(client_id="demo_id", client_secret="demo_secret", env_file=None)

    # 1. Map real/sanitised sandbox fixture records
    raw_invoices = load_fixture("sandbox_invoices_list")["items"]
    domain_invoices = [client.map_invoice(item) for item in raw_invoices]

    # Find open invoice for matching
    open_invoices = [inv for inv in domain_invoices if inv.status == InvoiceStatus.SENT]
    assert len(open_invoices) > 0
    target_invoice = open_invoices[0]

    # Create a matching completed payment for this target invoice
    domain_payment = Payment(
        payment_id="PAY-PAYPAL-SANDBOX-001",
        amount_cents=target_invoice.amount_cents,
        currency=target_invoice.currency,
        received_at=target_invoice.issued_on,
        payer_name=target_invoice.customer_name,
        payer_email=target_invoice.customer_email,
        reference=target_invoice.number,
        note=f"Payment for invoice {target_invoice.number}",
        status=PaymentStatus.COMPLETED,
    )

    raw_pay_payload = {
        "transaction_id": domain_payment.payment_id,
        "amount": {"value": str(domain_payment.amount_cents / 100), "currency_code": domain_payment.currency},
        "payer": {"name": domain_payment.payer_name, "email": domain_payment.payer_email},
        "reference": domain_payment.reference,
        "status": "S",
    }
    raw_inv_payloads = {inv.invoice_id: item for inv, item in zip(domain_invoices, raw_invoices)}

    # 2. Ingest into persistent store via StorageWorkflow
    with db.session() as session:
        workflow = StorageWorkflow(session)
        ingested_invoices = workflow.ingest_invoices(
            tenant_id="tenant_demo",
            invoices=domain_invoices,
            raw_payloads=raw_inv_payloads,
        )
        assert len(ingested_invoices) == len(domain_invoices)

        # Ingest pre-existing PayPal-linked allocations for hydrated partial/paid invoices
        partial_detail = load_fixture("sandbox_invoice_detail_partial")
        paid_detail = load_fixture("sandbox_invoice_detail_paid")
        linked_allocs = list(client.extract_linked_allocations(partial_detail)) + list(
            client.extract_linked_allocations(paid_detail)
        )
        workflow.ingest_provider_allocations(
            tenant_id="tenant_demo",
            allocations=linked_allocs,
        )

        ingested_payments = workflow.ingest_payments(
            tenant_id="tenant_demo",
            payments=[domain_payment],
            raw_payloads={domain_payment.payment_id: raw_pay_payload},
        )
        assert len(ingested_payments) == 1

    # Verify provider raw records are stored with SHA-256 hashes
    with db.session() as session:
        prec_repo = ProviderRecordRepository(session)
        prec = prec_repo.get_record("tenant_demo", "paypal", "payment", domain_payment.payment_id)
        assert prec is not None
        assert len(prec.content_hash) == 64

    # 3. Create a reconciliation run and persist proposals
    with db.session() as session:
        workflow = StorageWorkflow(session)
        run_record, run_result = workflow.run_reconciliation(
            tenant_id="tenant_demo",
            matcher_name="deterministic_v1",
            matcher_version="1.0.0",
        )
        assert run_record.status == "COMPLETED"
        assert len(run_result.proposals) >= 1

        # Locate proposal for target payment
        proposals = [p for p in run_result.proposals if p.payment_id == domain_payment.payment_id]
        assert len(proposals) == 1
        matched_prop = proposals[0]

        # Locate proposal specifically for the newly ingested payment
        target_db_pay = workflow.payment_repo.get_by_provider_id("tenant_demo", "paypal", domain_payment.payment_id)
        assert target_db_pay is not None
        prop_repo = workflow.proposal_repo
        db_proposals = list(prop_repo.list_by_payment("tenant_demo", target_db_pay.id))
        assert len(db_proposals) >= 1
        proposal_id_to_approve = db_proposals[0].id

        # At this stage, NO ledger allocations must exist for this target invoice
        alloc_repo = workflow.allocation_repo
        target_db_inv = workflow.invoice_repo.get_by_provider_id("tenant_demo", "paypal", target_invoice.invoice_id)
        assert target_db_inv is not None
        assert len(alloc_repo.get_allocations_for_invoice("tenant_demo", target_db_inv.id)) == 0

    # 4. Approve valid proposal through the Atomic Approval Service
    with db.session() as session:
        approval_service = ApprovalService(session)
        approval_res = approval_service.approve_proposal(
            tenant_id="tenant_demo",
            proposal_id=proposal_id_to_approve,
            actor="reconciliation_operator@example.com",
            idempotency_key="demo-approval-key-101",
            reason="Approved deterministic exact match",
        )
        assert approval_res.status == "APPROVED"
        assert not approval_res.is_idempotent_replay
        assert len(approval_res.allocation_ids) == 1

    # 5. Verify the audit chain
    with db.session() as session:
        audit_repo = AuditRepository(session)
        events = list(audit_repo.list_events("tenant_demo"))
        assert len(events) == 1
        assert events[0].event_type == "PROPOSAL_APPROVED"
        audit_repo.verify_tenant_chain("tenant_demo")

    # 6. Database / Process Restart: Dispose engine and reconnect freshly
    db.dispose()

    fresh_db = DatabaseEngine(DatabaseConfig(url=url))
    with fresh_db.session() as session:
        inv_repo = InvoiceRepository(session)
        pay_repo = PaymentRepository(session)
        alloc_repo = AllocationRepository(session)
        audit_repo = AuditRepository(session)

        # Retrieve target invoice in fresh session
        db_inv = inv_repo.get_by_provider_id("tenant_demo", "paypal", target_invoice.invoice_id)
        assert db_inv is not None

        # Confirm application-owned ledger balance is reduced to 0
        rem_balance = inv_repo.get_ledger_outstanding_minor("tenant_demo", db_inv.id)
        assert rem_balance == 0

        # Confirm payment unapplied cash is reduced to 0
        db_pay = pay_repo.get_by_provider_id("tenant_demo", "paypal", domain_payment.payment_id)
        assert db_pay is not None
        rem_cash = pay_repo.get_unapplied_cash_minor("tenant_demo", db_pay.id)
        assert rem_cash == 0

        # Confirm approved allocation is persistent
        allocs = alloc_repo.get_allocations_for_invoice("tenant_demo", db_inv.id)
        assert len(allocs) == 1
        assert allocs[0].amount_minor == target_invoice.amount_cents
        assert allocs[0].currency == target_invoice.currency

        # Confirm audit chain remains valid after restart
        audit_repo.verify_tenant_chain("tenant_demo")

    fresh_db.dispose()

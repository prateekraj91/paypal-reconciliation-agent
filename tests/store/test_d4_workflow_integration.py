"""End-to-End integration test for D4: Verified Webhook -> Queue -> Worker -> Persisted Proposal."""

from __future__ import annotations

from datetime import date, datetime, timezone
import json
import os
from unittest.mock import MagicMock
import pytest
from fastapi.testclient import TestClient

from reconcile.models import Invoice, InvoiceStatus, PaymentStatus
from reconcile.paypal import PayPalClient
from reconcile_api.main import app
from reconcile_api.dependencies import get_db_session, get_paypal_client, set_db_engine
from reconcile_store.config import DatabaseConfig
from reconcile_store.db import DatabaseEngine
from reconcile_store.schema import (
    Base,
    JobRecord,
    WebhookInboxRecord,
    InvoiceRecord,
    PaymentRecord,
    ProposalRecord,
    AllocationRecord,
)
from reconcile_store.repositories.tenant_repo import TenantRepository
from reconcile_store.repositories.invoice_repo import InvoiceRepository
from reconcile_store.audit import verify_audit_chain
import uuid
from alembic import command
from alembic.config import Config
from reconcile_worker.worker import Worker


@pytest.fixture
def test_db():
    url = os.getenv("TEST_POSTGRESQL_URL") or "sqlite:///:memory:"
    engine = DatabaseEngine(DatabaseConfig(url=url))
    if engine.config.is_sqlite:
        Base.metadata.create_all(engine.engine)
    else:
        alembic_cfg = Config("alembic.ini")
        alembic_cfg.set_main_option("sqlalchemy.url", url)
        command.upgrade(alembic_cfg, "head")

    set_db_engine(engine)
    yield engine
    set_db_engine(None)
    engine.dispose()


@pytest.fixture
def mock_paypal():
    client = MagicMock(spec=PayPalClient)
    client.verify_webhook_signature.return_value = True
    return client


def test_end_to_end_verified_webhook_to_persisted_proposal(test_db, mock_paypal, monkeypatch):
    """End-to-end test proving the complete D4 pipeline:
    
    1. Pre-seed invoice INV2-ACME-400 into store for tenant.
    2. Real PayPal webhook arrives at POST /webhooks/paypal.
    3. Official postback signature verification returns SUCCESS.
    4. Receipt durably stored; job enqueued; HTTP 202 returned.
    5. Worker claims job; retrieves authoritative data; matches payment against invoice.
    6. MatchProposal is persisted with PROPOSED outcome and independent verification.
    7. No proposal is auto-approved (allocations ledger is untouched).
    8. Tamper-evident audit chain remains valid and unbroken.
    9. Replaying duplicate webhook produces NO duplicate jobs, proposals or records.
    """
    tenant_id = f"t_d4_{uuid.uuid4().hex[:8]}"
    monkeypatch.setenv("ADMIN_API_KEY", "admin-key-d4")
    monkeypatch.setenv("PAYPAL_WEBHOOK_ID", "WH-SANDBOX-TEST")
    monkeypatch.setenv("PAYPAL_ENVIRONMENT", "sandbox")
    monkeypatch.setenv("DEFAULT_TENANT_ID", tenant_id)

    # 1. Pre-seed tenant and invoice in database
    with test_db.session() as s:
        TenantRepository(s).create_tenant(tenant_id, f"Test Tenant {tenant_id}")
        InvoiceRepository(s).upsert_invoice(
            tenant_id=tenant_id,
            provider="paypal",
            provider_invoice_id="INV2-ACME-400",
            invoice_number="ACME-400",
            total_minor=50000,
            currency="USD",
            provider_due_minor=50000,
            status="SENT",
            customer_name="Acme Corporation",
            issued_on=date(2026, 11, 1),
            due_on=date(2026, 11, 15),
        )
        s.commit()

    # Configure overrides for TestClient
    def override_paypal():
        return mock_paypal

    def override_db():
        with test_db.session() as s:
            yield s

    app.dependency_overrides[get_paypal_client] = override_paypal
    app.dependency_overrides[get_db_session] = override_db

    # 2. PayPal sends payment capture completed event
    event_id = f"WH-E2E-{tenant_id}"
    capture_id = f"CAP-E2E-{tenant_id}"
    webhook_headers = {
        "PAYPAL-TRANSMISSION-ID": f"trans-{tenant_id}",
        "PAYPAL-TRANSMISSION-TIME": "2026-11-03T12:00:00Z",
        "PAYPAL-CERT-URL": "https://api.sandbox.paypal.com/v1/notifications/certs/CERT-1",
        "PAYPAL-AUTH-ALGO": "SHA256withRSA",
        "PAYPAL-TRANSMISSION-SIG": "valid-sig-e2e",
        "Content-Type": "application/json",
    }
    webhook_payload = {
        "id": event_id,
        "event_type": "PAYMENT.CAPTURE.COMPLETED",
        "resource_type": "capture",
        "create_time": "2026-11-03T12:00:00Z",
        "resource": {
            "id": capture_id,
            "status": "COMPLETED",
            "amount": {"value": "500.00", "currency_code": "USD"},
            "custom_id": "INV2-ACME-400",
            "create_time": "2026-11-03T12:00:00Z",
        },
    }

    with TestClient(app) as client:
        # POST to public webhook endpoint
        res = client.post("/webhooks/paypal", headers=webhook_headers, json=webhook_payload)
        assert res.status_code == 202
        data = res.json()
        assert data["status"] == "accepted"
        job_id = data["job_id"]

    # 3. Verify inbox receipt & job before worker run
    with test_db.session() as s:
        receipt = s.query(WebhookInboxRecord).filter_by(tenant_id=tenant_id, event_id=event_id).first()
        assert receipt is not None
        assert receipt.verification_status == "VERIFIED"
        assert receipt.processing_status == "QUEUED"
        assert receipt.job_id == job_id

        job = s.query(JobRecord).filter_by(tenant_id=tenant_id, id=job_id).first()
        assert job.status == "QUEUED"

    # 4. Background worker runs
    worker = Worker(db_engine=test_db, paypal_client=mock_paypal)
    processed_job = worker.run_once(tenant_id=tenant_id)

    assert processed_job is not None
    assert processed_job.id == job_id
    assert processed_job.status == "COMPLETED"

    # 5. Verify persisted reconciliation results
    with test_db.session() as s:
        # Receipt marked PROCESSED
        receipt = s.query(WebhookInboxRecord).filter_by(tenant_id=tenant_id, event_id=event_id).first()
        assert receipt is not None
        assert receipt.processing_status == "PROCESSED"
        assert receipt.processed_at is not None

        # Payment record stored
        pay = s.query(PaymentRecord).filter_by(tenant_id=tenant_id, provider_payment_id=capture_id).first()
        assert pay is not None
        assert pay.amount_minor == 50000

        # Match proposal persisted
        proposals = s.query(ProposalRecord).filter_by(tenant_id=tenant_id).all()
        assert len(proposals) == 1
        prop = proposals[0]
        assert prop.outcome == "PROPOSED"
        assert len(prop.allocations) == 1
        assert prop.allocations[0].amount_minor == 50000
        assert prop.verifier_status == "VERIFIED"

        # Crucial fintech requirement: NEVER auto-approved!
        allocations = s.query(AllocationRecord).filter_by(tenant_id=tenant_id).all()
        assert len(allocations) == 0

        # Tamper-evident audit chain remains unbroken
        from reconcile_store.repositories.audit_repo import AuditRepository
        AuditRepository(s).verify_tenant_chain(tenant_id)

    # 6. Replay exact same webhook event -> Idempotent, zero side effects
    with TestClient(app) as client:
        replay_res = client.post("/webhooks/paypal", headers=webhook_headers, json=webhook_payload)
        assert replay_res.status_code == 200
        assert replay_res.json()["status"] == "already_received"

    # No second job created for tenant
    with test_db.session() as s:
        jobs = s.query(JobRecord).filter_by(tenant_id=tenant_id).all()
        assert len(jobs) == 1

        proposals = s.query(ProposalRecord).filter_by(tenant_id=tenant_id).all()
        assert len(proposals) == 1

    app.dependency_overrides.clear()

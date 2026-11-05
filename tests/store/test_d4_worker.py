"""Tests for background worker execution, webhook processing, and retry lifecycles."""

from __future__ import annotations

from datetime import date, datetime, timezone
import json
from unittest.mock import MagicMock
import pytest

from reconcile.models import (
    Invoice,
    InvoiceStatus,
    Payment,
    PaymentAllocation,
    AllocationStatus,
    PaymentStatus,
)
from reconcile.paypal import PayPalClient, PayPalNotFoundError, PayPalAPIError
from reconcile.sync import SyncResult, SyncReport
from reconcile_store.config import DatabaseConfig
from reconcile_store.db import DatabaseEngine
from reconcile_store.schema import (
    Base,
    JobRecord,
    WebhookInboxRecord,
    InvoiceRecord,
    PaymentRecord,
    ProposalRecord,
    ConnectionRecord,
)
from reconcile_store.repositories.tenant_repo import TenantRepository, ConnectionRepository
from reconcile_store.repositories.webhook_repo import WebhookInboxRepository
from reconcile_store.repositories.job_repo import JobRepository
from reconcile_store.repositories.invoice_repo import InvoiceRepository
from reconcile_store.workflow import StorageWorkflow
from reconcile_worker.worker import (
    Worker,
    PermanentJobError,
    RetryableJobError,
    InvoiceNotYetAvailableError,
)


@pytest.fixture
def store():
    engine = DatabaseEngine(DatabaseConfig(url="sqlite:///:memory:"))
    Base.metadata.create_all(engine.engine)
    with engine.session() as s:
        TenantRepository(s).create_tenant("t1", "Tenant 1")
    return engine


@pytest.fixture
def mock_paypal():
    client = MagicMock(spec=PayPalClient)
    return client


def test_worker_processes_invoice_webhook(store, mock_paypal):
    # Setup mock invoice
    invoice_id = "INV2-TEST-100"
    mock_paypal.get_raw_invoice.return_value = {
        "id": invoice_id,
        "detail": {
            "invoice_number": "100",
            "invoice_date": "2026-11-01",
            "currency_code": "USD",
        },
        "amount": {"currency_code": "USD", "value": "200.00"},
        "due_amount": {"currency_code": "USD", "value": "200.00"},
        "status": "SENT",
        "primary_recipients": [{"billing_info": {"name": {"full_name": "Acme Corp"}}}],
    }
    mock_paypal.map_invoice.return_value = Invoice(
        invoice_id=invoice_id,
        number="100",
        amount_cents=20000,
        currency="USD",
        status=InvoiceStatus.SENT,
        customer_name="Acme Corp",
        due_amount_cents=20000,
        issued_on=date(2026, 11, 1),
        due_on=date(2026, 11, 15),
    )
    mock_paypal.extract_linked_allocations.return_value = ()

    # Seed webhook inbox and job
    event_payload = {
        "id": "WH-INV-1",
        "event_type": "INVOICING.INVOICE.CREATED",
        "resource": {"id": invoice_id},
    }
    with store.session() as s:
        inbox = WebhookInboxRepository(s).record_receipt(
            tenant_id="t1",
            provider="paypal",
            environment="sandbox",
            event_id="WH-INV-1",
            event_type="INVOICING.INVOICE.CREATED",
            raw_payload=json.dumps(event_payload),
            verification_status="VERIFIED",
        )
        job = JobRepository(s).enqueue_job(
            tenant_id="t1",
            job_type="WEBHOOK_INGEST",
            idempotency_key="wh:sandbox:WH-INV-1",
            payload={"receipt_id": inbox.id, "event_type": "INVOICING.INVOICE.CREATED", "resource_id": invoice_id},
        )
        inbox.job_id = job.id
        inbox.processing_status = "QUEUED"
        s.commit()

    worker = Worker(db_engine=store, paypal_client=mock_paypal)
    processed_job = worker.run_once(tenant_id="t1")

    assert processed_job is not None
    assert processed_job.status == "COMPLETED"

    with store.session() as s:
        # Check invoice was persisted
        inv = s.query(InvoiceRecord).filter_by(provider_invoice_id=invoice_id).first()
        assert inv is not None
        assert inv.total_minor == 20000

        # Check webhook inbox was updated to PROCESSED
        wh = s.query(WebhookInboxRecord).filter_by(event_id="WH-INV-1").first()
        assert wh.processing_status == "PROCESSED"
        assert wh.processed_at is not None


def test_payment_arriving_before_invoice_scenario(store, mock_paypal):
    """Critical D4 requirement: payment arrives before invoice is available.
    
    1. Payment event arrives referencing INV2-FUTURE-999.
    2. Invoice does not exist in store or PayPal.
    3. Worker retains payment safely in store, job transitions to RETRY.
    4. No proposal or false match created.
    5. Invoice becomes available.
    6. Job retry succeeds, proposal is persisted.
    """
    capture_id = "CAP-EARLY-123"
    invoice_ref = "INV2-FUTURE-999"

    # Step 1: PayPal returns 404 when looking for invoice
    mock_paypal.get_raw_invoice.side_effect = PayPalNotFoundError("Invoice not found")

    event_payload = {
        "id": "WH-PAY-EARLY",
        "event_type": "PAYMENT.CAPTURE.COMPLETED",
        "resource": {
            "id": capture_id,
            "status": "COMPLETED",
            "amount": {"value": "350.00", "currency_code": "USD"},
            "custom_id": invoice_ref,
            "create_time": "2026-11-03T12:00:00Z",
        },
    }

    with store.session() as s:
        inbox = WebhookInboxRepository(s).record_receipt(
            tenant_id="t1",
            provider="paypal",
            environment="sandbox",
            event_id="WH-PAY-EARLY",
            event_type="PAYMENT.CAPTURE.COMPLETED",
            raw_payload=json.dumps(event_payload),
            verification_status="VERIFIED",
        )
        job = JobRepository(s).enqueue_job(
            tenant_id="t1",
            job_type="WEBHOOK_INGEST",
            idempotency_key="wh:sandbox:WH-PAY-EARLY",
            payload={"receipt_id": inbox.id, "event_type": "PAYMENT.CAPTURE.COMPLETED", "resource_id": capture_id},
        )
        inbox.job_id = job.id
        s.commit()

    worker = Worker(db_engine=store, paypal_client=mock_paypal)
    deferred_job = worker.run_once(tenant_id="t1")

    # Step 2: Verify job is deferred in RETRY status (not COMPLETED, not FAILED)
    assert deferred_job is not None
    assert deferred_job.status == "RETRY"
    assert "not yet available" in deferred_job.last_error

    # Verify payment was safely retained in database
    with store.session() as s:
        pay = s.query(PaymentRecord).filter_by(provider_payment_id=capture_id).first()
        assert pay is not None
        assert pay.amount_minor == 35000

        # No proposals generated yet!
        proposals = s.query(ProposalRecord).all()
        assert len(proposals) == 0

        # Webhook receipt is marked RETRY
        wh = s.query(WebhookInboxRecord).filter_by(event_id="WH-PAY-EARLY").first()
        assert wh.processing_status == "RETRY"

    # Step 3: Now invoice becomes available in store (e.g. merchant created or synced it)
    with store.session() as s:
        InvoiceRepository(s).upsert_invoice(
            tenant_id="t1",
            provider="paypal",
            provider_invoice_id=invoice_ref,
            invoice_number="FUTURE-999",
            total_minor=35000,
            currency="USD",
            provider_due_minor=35000,
            status="SENT",
            customer_name="Future Customer",
        )
        # Reset available_at on job so worker can claim it immediately
        j = s.query(JobRecord).filter_by(id=deferred_job.id).first()
        j.available_at = datetime.now(timezone.utc)
        s.commit()

    # Step 4: Worker executes retry pass
    retried_job = worker.run_once(tenant_id="t1")
    assert retried_job is not None
    assert retried_job.status == "COMPLETED"

    # Step 5: Verify reconciliation ran and proposal was persisted
    with store.session() as s:
        proposals = s.query(ProposalRecord).all()
        assert len(proposals) == 1
        assert proposals[0].outcome == "PROPOSED"
        # Confirm proposals are NEVER auto-approved in D4
        assert len(proposals[0].decisions) == 0

        # Webhook receipt is now PROCESSED
        wh = s.query(WebhookInboxRecord).filter_by(event_id="WH-PAY-EARLY").first()
        assert wh.processing_status == "PROCESSED"


def test_worker_permanent_failure_handling(store):
    with store.session() as s:
        inbox = WebhookInboxRepository(s).record_receipt(
            tenant_id="t1",
            provider="paypal",
            environment="sandbox",
            event_id="WH-MALFORMED",
            event_type="PAYMENT.CAPTURE.COMPLETED",
            raw_payload="corrupted-not-json-!!!",
            verification_status="VERIFIED",
        )
        job = JobRepository(s).enqueue_job(
            tenant_id="t1",
            job_type="WEBHOOK_INGEST",
            idempotency_key="wh:malformed",
            payload={"receipt_id": inbox.id},
        )
        inbox.job_id = job.id
        s.commit()

    worker = Worker(db_engine=store)
    failed_job = worker.run_once(tenant_id="t1")

    assert failed_job.status == "FAILED"
    assert "Malformed" in failed_job.last_error

    with store.session() as s:
        wh = s.query(WebhookInboxRecord).filter_by(event_id="WH-MALFORMED").first()
        assert wh.processing_status == "FAILED"


def test_worker_manual_sync_missing_connection(store):
    with store.session() as s:
        JobRepository(s).enqueue_job(
            tenant_id="t1",
            job_type="MANUAL_SYNC",
            idempotency_key="sync:no-conn",
            payload={"tenant_id": "t1", "environment": "sandbox"},
        )
        s.commit()

    worker = Worker(db_engine=store)
    job = worker.run_once(tenant_id="t1")

    assert job is not None
    assert job.status == "FAILED"
    assert "No active PayPal connection found" in job.last_error


def test_worker_manual_sync_inactive_connection(store):
    with store.session() as s:
        ConnectionRepository(s).create_connection(
            tenant_id="t1",
            provider="paypal",
            environment="sandbox",
            credential_reference="env:PAYPAL_SANDBOX",
            status="INACTIVE",
        )
        JobRepository(s).enqueue_job(
            tenant_id="t1",
            job_type="MANUAL_SYNC",
            idempotency_key="sync:inactive-conn",
            payload={"tenant_id": "t1", "environment": "sandbox"},
        )
        s.commit()

    worker = Worker(db_engine=store)
    job = worker.run_once(tenant_id="t1")

    assert job is not None
    assert job.status == "FAILED"
    assert "INACTIVE" in job.last_error


def test_worker_manual_sync_invalid_date_payload_fails_permanently(store):
    with store.session() as s:
        ConnectionRepository(s).create_connection(
            tenant_id="t1",
            provider="paypal",
            environment="sandbox",
            credential_reference="env:PAYPAL_SANDBOX",
            status="ACTIVE",
        )
        JobRepository(s).enqueue_job(
            tenant_id="t1",
            job_type="MANUAL_SYNC",
            idempotency_key="sync:bad-date",
            payload={"tenant_id": "t1", "start_date": "string", "end_date": "string"},
        )
        s.commit()

    worker = Worker(db_engine=store)
    job = worker.run_once(tenant_id="t1")

    assert job is not None
    assert job.status == "FAILED"
    assert "Invalid date" in job.last_error


def test_worker_manual_sync_oauth_failure(store, mock_paypal):
    with store.session() as s:
        ConnectionRepository(s).create_connection(
            tenant_id="t1",
            provider="paypal",
            environment="sandbox",
            credential_reference="env:PAYPAL_SANDBOX",
            status="ACTIVE",
        )
        JobRepository(s).enqueue_job(
            tenant_id="t1",
            job_type="MANUAL_SYNC",
            idempotency_key="sync:oauth-fail",
            payload={"tenant_id": "t1", "environment": "sandbox"},
        )
        s.commit()

    # Simulate PayPal OAuth rejection (HTTP 401)
    mock_paypal.list_all_invoices_with_allocations.side_effect = PayPalAPIError(401, "Invalid client credentials")

    worker = Worker(db_engine=store, paypal_client=mock_paypal)
    job = worker.run_once(tenant_id="t1")

    assert job is not None
    assert job.status == "FAILED"
    assert "authorization failed" in job.last_error.lower() or "401" in job.last_error


def test_worker_manual_sync_success_mocked(store, mock_paypal, monkeypatch):
    inv = Invoice(
        invoice_id="INV2-SYNC-001",
        number="INV-100",
        amount_cents=50000,
        currency="USD",
        status=InvoiceStatus.SENT,
        customer_name="Acme Sync Corp",
        due_amount_cents=50000,
        issued_on=date(2026, 11, 1),
        due_on=date(2026, 11, 15),
    )
    pay = Payment(
        payment_id="PAY-SYNC-001",
        amount_cents=50000,
        currency="USD",
        status=PaymentStatus.COMPLETED,
        payer_name="Acme Sync Corp",
        reference="INV-100",
        received_at=datetime(2026, 11, 2, 12, 0, 0, tzinfo=timezone.utc),
    )

    with store.session() as s:
        ConnectionRepository(s).create_connection(
            tenant_id="t1",
            provider="paypal",
            environment="sandbox",
            credential_reference="env:PAYPAL_SANDBOX",
            status="ACTIVE",
        )
        JobRepository(s).enqueue_job(
            tenant_id="t1",
            job_type="MANUAL_SYNC",
            idempotency_key="sync:success-1",
            payload={
                "tenant_id": "t1",
                "environment": "sandbox",
                "start_date": "2026-11-01T00:00:00+00:00",
                "end_date": "2026-11-03T00:00:00+00:00",
            },
        )
        s.commit()

        fake_sync_result = SyncResult(
            report=SyncReport(
                window_start=datetime(2026, 11, 1, tzinfo=timezone.utc),
                window_end=datetime(2026, 11, 3, tzinfo=timezone.utc),
                invoices_ingested=1,
                transactions_ingested=1,
            ),
            invoices=(inv,),
            payments=(pay,),
            ledger_allocations=(),
            transaction_evidence={pay.payment_id: {"mock": "evidence"}},
        )

    import reconcile_worker.worker as rw
    monkeypatch.setattr(rw, "run_sync", lambda **kwargs: fake_sync_result)

    worker = Worker(db_engine=store, paypal_client=mock_paypal)
    job = worker.run_once(tenant_id="t1")

    assert job is not None
    assert job.status == "COMPLETED"

    with store.session() as s:
        # Check persisted records
        inv_rec = s.query(InvoiceRecord).filter_by(tenant_id="t1").first()
        assert inv_rec is not None
        assert inv_rec.provider_invoice_id == "INV2-SYNC-001"

        pay_rec = s.query(PaymentRecord).filter_by(tenant_id="t1").first()
        assert pay_rec is not None
        assert pay_rec.provider_payment_id == "PAY-SYNC-001"

        # Check reconciliation proposal was generated and remains in PROPOSED state (no auto-approval)
        proposals = s.query(ProposalRecord).filter_by(tenant_id="t1").all()
        assert len(proposals) == 1
        assert proposals[0].outcome == "PROPOSED"
        assert len(proposals[0].decisions) == 0


def test_worker_manual_sync_duplicate_idempotency(store, mock_paypal, monkeypatch):
    with store.session() as s:
        ConnectionRepository(s).create_connection(
            tenant_id="t1",
            provider="paypal",
            environment="sandbox",
            credential_reference="env:PAYPAL_SANDBOX",
            status="ACTIVE",
        )
        j1 = JobRepository(s).enqueue_job(
            tenant_id="t1",
            job_type="MANUAL_SYNC",
            idempotency_key="sync:idempotent-test",
            payload={"tenant_id": "t1", "environment": "sandbox"},
        )
        # Attempt duplicate enqueue
        j2 = JobRepository(s).enqueue_job(
            tenant_id="t1",
            job_type="MANUAL_SYNC",
            idempotency_key="sync:idempotent-test",
            payload={"tenant_id": "t1", "environment": "sandbox"},
        )
        assert j1.id == j2.id
        s.commit()

    fake_sync_result = SyncResult(
        report=SyncReport(
            window_start=datetime(2026, 11, 1, tzinfo=timezone.utc),
            window_end=datetime(2026, 11, 3, tzinfo=timezone.utc),
            invoices_ingested=0,
            transactions_ingested=0,
        ),
        invoices=(),
        payments=(),
        ledger_allocations=(),
        transaction_evidence={},
    )

    import reconcile_worker.worker as rw
    monkeypatch.setattr(rw, "run_sync", lambda **kwargs: fake_sync_result)

    worker = Worker(db_engine=store, paypal_client=mock_paypal)
    job = worker.run_once(tenant_id="t1")
    assert job.status == "COMPLETED"


def test_worker_resolve_paypal_client_sandbox_vs_live(store, monkeypatch):
    worker = Worker(db_engine=store)

    # 1. Missing credentials in env (mock _load_dotenv to ensure repo .env is not read)
    monkeypatch.setattr("reconcile.paypal._load_dotenv", lambda *args, **kwargs: {})
    monkeypatch.delenv("PAYPAL_CLIENT_ID", raising=False)
    monkeypatch.delenv("PAYPAL_CLIENT_SECRET", raising=False)
    conn_no_creds = ConnectionRecord(
        tenant_id="t1",
        provider="paypal",
        environment="sandbox",
        credential_ref="env:PAYPAL_SANDBOX",
        status="ACTIVE",
    )
    with pytest.raises(PermanentJobError, match="Missing PayPal credentials"):
        worker.resolve_paypal_client_for_connection(conn_no_creds)

    # 2. Valid sandbox credentials
    monkeypatch.setenv("PAYPAL_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("PAYPAL_CLIENT_SECRET", "test-client-secret")
    conn_sb = ConnectionRecord(
        tenant_id="t1",
        provider="paypal",
        environment="sandbox",
        credential_ref="env:PAYPAL_SANDBOX",
        status="ACTIVE",
    )
    client_sb = worker.resolve_paypal_client_for_connection(conn_sb)
    assert "sandbox" in client_sb.base_url

    # 3. Live connection refuses sandbox URL
    monkeypatch.setenv("PAYPAL_LIVE_CLIENT_ID", "live-id")
    monkeypatch.setenv("PAYPAL_LIVE_CLIENT_SECRET", "live-secret")
    monkeypatch.setenv("PAYPAL_LIVE_BASE_URL", "https://api-m.sandbox.paypal.com")
    conn_live = ConnectionRecord(
        tenant_id="t1",
        provider="paypal",
        environment="live",
        credential_ref="env:PAYPAL_LIVE",
        status="ACTIVE",
    )
    with pytest.raises(PermanentJobError, match="Refusing to execute LIVE connection against Sandbox URL"):
        worker.resolve_paypal_client_for_connection(conn_live)

    # 4. Fallback support: env:PAYPAL_SANDBOX_US resolves with PAYPAL_CLIENT_ID
    conn_us = ConnectionRecord(
        tenant_id="t1",
        provider="paypal",
        environment="sandbox",
        credential_ref="env:PAYPAL_SANDBOX_US",
        status="ACTIVE",
    )
    client_us = worker.resolve_paypal_client_for_connection(conn_us)
    assert client_us.client_id == "test-client-id"



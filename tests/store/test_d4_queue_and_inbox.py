"""Tests for Webhook Inbox and Background Job Queue repositories."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import pytest
from sqlalchemy import text

from reconcile_store.config import DatabaseConfig
from reconcile_store.db import DatabaseEngine
from reconcile_store.schema import Base, _utcnow
from reconcile_store.repositories.tenant_repo import TenantRepository
from reconcile_store.repositories.webhook_repo import (
    WebhookInboxRepository,
    InvalidWebhookStateTransitionError,
)
from reconcile_store.repositories.job_repo import JobRepository


@pytest.fixture
def store():
    engine = DatabaseEngine(DatabaseConfig(url="sqlite:///:memory:"))
    Base.metadata.create_all(engine.engine)
    return engine


def test_job_enqueue_and_idempotency(store):
    with store.session() as session:
        TenantRepository(session).create_tenant("t1", "Tenant 1")
        repo = JobRepository(session)

        job1 = repo.enqueue_job("t1", "WEBHOOK_INGEST", "key-1", {"event": "test"})
        session.commit()
        assert job1.status == "QUEUED"
        assert job1.attempt_count == 0

        # Enqueue with same idempotency key returns identical job
        job2 = repo.enqueue_job("t1", "WEBHOOK_INGEST", "key-1", {"event": "different"})
        assert job1.id == job2.id


def test_job_claim_and_completion(store):
    with store.session() as session:
        TenantRepository(session).create_tenant("t1", "Tenant 1")
        repo = JobRepository(session)

        job = repo.enqueue_job("t1", "WEBHOOK_INGEST", "key-claim", {"foo": "bar"})
        session.commit()

        # Worker 1 claims job
        claimed = repo.claim_next_job("worker-1", lease_seconds=30)
        assert claimed is not None
        assert claimed.id == job.id
        assert claimed.status == "RUNNING"
        assert claimed.worker_id == "worker-1"
        assert claimed.attempt_count == 1
        assert claimed.lease_expires_at is not None
        session.commit()

        # Worker 2 attempts claim while lease is active -> None
        claimed_2 = repo.claim_next_job("worker-2", lease_seconds=30)
        assert claimed_2 is None

        # Complete job
        completed = repo.complete_job(job.id, "worker-1")
        assert completed.status == "COMPLETED"
        assert completed.completed_at is not None
        session.commit()

        # No jobs available after completion
        assert repo.claim_next_job("worker-1") is None


def test_job_lease_expiration_and_recovery(store):
    with store.session() as session:
        TenantRepository(session).create_tenant("t1", "Tenant 1")
        repo = JobRepository(session)

        job = repo.enqueue_job("t1", "WEBHOOK_INGEST", "key-crash", {})
        session.commit()

        # Worker 1 claims job with 1 second lease
        claimed = repo.claim_next_job("worker-1", lease_seconds=1)
        assert claimed is not None
        session.commit()

        # Simulate time passing beyond lease expiration
        claimed.lease_expires_at = _utcnow() - timedelta(seconds=5)
        session.commit()

        # Worker 2 reclaims the expired job
        reclaimed = repo.claim_next_job("worker-2", lease_seconds=60)
        assert reclaimed is not None
        assert reclaimed.id == job.id
        assert reclaimed.worker_id == "worker-2"
        assert reclaimed.status == "RUNNING"
        assert reclaimed.attempt_count == 2
        session.commit()


def test_job_retry_and_max_attempts(store):
    with store.session() as session:
        TenantRepository(session).create_tenant("t1", "Tenant 1")
        repo = JobRepository(session)

        job = repo.enqueue_job("t1", "WEBHOOK_INGEST", "key-retry", {}, max_attempts=2)
        session.commit()

        claimed = repo.claim_next_job("worker-1")
        assert claimed.attempt_count == 1
        session.commit()

        # Attempt 1 fails -> RETRY status
        retried = repo.retry_job(job.id, "worker-1", "Temporary PayPal 503", delay_seconds=0)
        assert retried.status == "RETRY"
        assert "Temporary PayPal 503" in retried.last_error
        session.commit()

        # Reclaimed for attempt 2
        claimed2 = repo.claim_next_job("worker-1")
        assert claimed2.attempt_count == 2
        session.commit()

        # Attempt 2 fails -> exceeds max_attempts (2) -> FAILED status
        failed = repo.retry_job(job.id, "worker-1", "Permanent failure", delay_seconds=0)
        assert failed.status == "FAILED"
        assert "Max attempts" in failed.last_error
        session.commit()


def test_webhook_inbox_idempotency_and_state_guards(store):
    with store.session() as session:
        TenantRepository(session).create_tenant("t1", "Tenant 1")
        inbox_repo = WebhookInboxRepository(session)
        job_repo = JobRepository(session)

        # 1. First delivery
        rec1 = inbox_repo.record_receipt(
            tenant_id="t1",
            provider="paypal",
            environment="sandbox",
            event_id="WH-12345",
            event_type="PAYMENT.CAPTURE.COMPLETED",
            raw_payload='{"id": "WH-12345"}',
            headers={"PAYPAL-TRANSMISSION-ID": "tx-1"},
        )
        session.commit()
        assert rec1.verification_status == "RECEIVED"
        assert rec1.processing_status == "RECEIVED"

        # 2. Duplicate delivery returns same record without mutating
        rec2 = inbox_repo.record_receipt(
            tenant_id="t1",
            provider="paypal",
            environment="sandbox",
            event_id="WH-12345",
            event_type="PAYMENT.CAPTURE.COMPLETED",
            raw_payload='{"mutated": true}',
        )
        assert rec1.id == rec2.id
        assert rec2.raw_payload == '{"id": "WH-12345"}'

        # 3. Cannot link job to unverified webhook
        job = job_repo.enqueue_job("t1", "WEBHOOK_INGEST", "idemp-wh-1", {})
        session.commit()

        with pytest.raises(InvalidWebhookStateTransitionError):
            inbox_repo.link_job(rec1.id, "t1", job.id)

        # 4. Verify webhook
        inbox_repo.update_verification_status(rec1.id, "t1", "VERIFIED")
        session.commit()

        # 5. Link job after verification works
        linked = inbox_repo.link_job(rec1.id, "t1", job.id)
        assert linked.job_id == job.id
        assert linked.processing_status == "QUEUED"
        session.commit()

        # 6. Cannot downgrade verified to rejected
        with pytest.raises(InvalidWebhookStateTransitionError):
            inbox_repo.update_verification_status(rec1.id, "t1", "REJECTED")

        # 7. Update processing status
        processed = inbox_repo.update_processing_status(rec1.id, "t1", "PROCESSED")
        assert processed.processing_status == "PROCESSED"
        assert processed.processed_at is not None

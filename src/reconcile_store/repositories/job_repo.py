"""Database-backed durable background job queue repository."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import logging
from typing import Any, Optional, Sequence
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .base import BaseRepository, ConflictError, EntityNotFoundError
from ..schema import JobRecord, _utcnow
from ..canonical import canonical_json

logger = logging.getLogger(__name__)


class JobRepository(BaseRepository[JobRecord]):
    """Durable queue repository with atomic worker claiming, lease expiry and exponential retry."""

    def enqueue_job(
        self,
        tenant_id: str,
        job_type: str,
        idempotency_key: str,
        payload: dict[str, Any],
        available_at: Optional[datetime] = None,
        max_attempts: int = 5,
    ) -> JobRecord:
        """Enqueue a background job idempotently.
        
        If a job with (tenant_id, idempotency_key) already exists, returns the existing job.
        """
        stmt = select(JobRecord).where(
            JobRecord.tenant_id == tenant_id,
            JobRecord.idempotency_key == idempotency_key,
        )
        existing = self.session.scalars(stmt).first()
        if existing:
            return existing

        now = _utcnow()
        avail = available_at or now
        job = JobRecord(
            tenant_id=tenant_id,
            job_type=job_type,
            idempotency_key=idempotency_key,
            payload_json=canonical_json(payload),
            status="QUEUED",
            attempt_count=0,
            max_attempts=max_attempts,
            available_at=avail,
            lease_expires_at=None,
            worker_id=None,
            created_at=now,
            updated_at=now,
        )
        self.session.add(job)
        self.session.flush()
        return job

    def claim_next_job(
        self,
        worker_id: str,
        lease_seconds: int = 60,
        tenant_id: Optional[str] = None,
    ) -> Optional[JobRecord]:
        """Atomically claim the next eligible job for execution.
        
        Eligible jobs are:
        - QUEUED or RETRY with available_at <= now
        - RUNNING with lease_expires_at <= now (crashed worker lease recovery)
        
        Uses SELECT FOR UPDATE SKIP LOCKED on PostgreSQL to prevent concurrent claim collisions.
        """
        now = _utcnow()

        eligibility_condition = or_(
            (JobRecord.status.in_(["QUEUED", "RETRY"])) & (JobRecord.available_at <= now),
            (JobRecord.status == "RUNNING") & (JobRecord.lease_expires_at <= now),
        )

        stmt = select(JobRecord).where(eligibility_condition)
        if tenant_id:
            stmt = stmt.where(JobRecord.tenant_id == tenant_id)

        stmt = stmt.order_by(JobRecord.available_at.asc()).limit(1)

        # Apply PostgreSQL row-level skip-locked claiming
        bind = self.session.get_bind()
        dialect_name = bind.dialect.name if bind else ""
        if dialect_name == "postgresql":
            stmt = stmt.with_for_update(skip_locked=True)

        job = self.session.scalars(stmt).first()
        if job is None:
            return None

        # Atomically update claim lease
        job.status = "RUNNING"
        job.worker_id = worker_id
        job.lease_expires_at = now + timedelta(seconds=lease_seconds)
        job.attempt_count += 1
        job.updated_at = now
        self.session.flush()
        return job

    def heartbeat_lease(
        self,
        job_id: str,
        worker_id: str,
        extend_seconds: int = 60,
    ) -> bool:
        """Renew the worker lease on an actively executing job."""
        job = self.session.get(JobRecord, job_id)
        if job is None or job.status != "RUNNING" or job.worker_id != worker_id:
            return False

        now = _utcnow()
        job.lease_expires_at = now + timedelta(seconds=extend_seconds)
        job.updated_at = now
        self.session.flush()
        return True

    def complete_job(self, job_id: str, worker_id: str) -> JobRecord:
        """Mark a job as successfully completed."""
        job = self.session.get(JobRecord, job_id)
        if job is None:
            raise EntityNotFoundError(f"Job '{job_id}' not found")

        now = _utcnow()
        job.status = "COMPLETED"
        job.completed_at = now
        job.lease_expires_at = None
        job.updated_at = now
        self.session.flush()
        return job

    def retry_job(
        self,
        job_id: str,
        worker_id: str,
        error_message: str,
        delay_seconds: int = 30,
    ) -> JobRecord:
        """Schedule a job for retry or mark FAILED if max attempts exhausted."""
        job = self.session.get(JobRecord, job_id)
        if job is None:
            raise EntityNotFoundError(f"Job '{job_id}' not found")

        now = _utcnow()
        sanitized_error = error_message[:1000]

        if job.attempt_count >= job.max_attempts:
            job.status = "FAILED"
            job.last_error = f"Max attempts ({job.max_attempts}) reached: {sanitized_error}"
        else:
            job.status = "RETRY"
            job.available_at = now + timedelta(seconds=delay_seconds)
            job.last_error = sanitized_error

        job.lease_expires_at = None
        job.updated_at = now
        self.session.flush()
        return job

    def fail_job(self, job_id: str, worker_id: str, error_message: str) -> JobRecord:
        """Mark a job permanently failed (for unrecoverable or permanent errors)."""
        job = self.session.get(JobRecord, job_id)
        if job is None:
            raise EntityNotFoundError(f"Job '{job_id}' not found")

        now = _utcnow()
        job.status = "FAILED"
        job.last_error = error_message[:1000]
        job.lease_expires_at = None
        job.updated_at = now
        self.session.flush()
        return job

    def get_job(self, tenant_id: str, job_id: str) -> Optional[JobRecord]:
        """Look up a job by ID within a tenant."""
        stmt = select(JobRecord).where(
            JobRecord.tenant_id == tenant_id,
            JobRecord.id == job_id,
        )
        return self.session.scalars(stmt).first()

    def list_jobs(
        self,
        tenant_id: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 50,
    ) -> Sequence[JobRecord]:
        """List jobs for operational monitoring."""
        stmt = select(JobRecord)
        if tenant_id:
            stmt = stmt.where(JobRecord.tenant_id == tenant_id)
        if status:
            stmt = stmt.where(JobRecord.status == status)
        stmt = stmt.order_by(JobRecord.created_at.desc()).limit(limit)
        return list(self.session.scalars(stmt).all())

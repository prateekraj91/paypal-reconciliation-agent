"""Webhook inbox repository for durable receipt and processing state tracking."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Any, Optional, Sequence
from sqlalchemy import select
from sqlalchemy.orm import Session

from .base import BaseRepository, ConflictError, EntityNotFoundError, TenantIsolationError
from ..schema import WebhookInboxRecord, _utcnow


class InvalidWebhookStateTransitionError(ConflictError):
    """Raised when an invalid state transition is attempted on a webhook receipt."""


class WebhookInboxRepository(BaseRepository[WebhookInboxRecord]):
    """Repository managing durable inbound provider webhook receipts and their lifecycles."""

    def record_receipt(
        self,
        tenant_id: str,
        provider: str,
        environment: str,
        event_id: str,
        event_type: str,
        raw_payload: str,
        headers: Optional[dict[str, Any]] = None,
        webhook_id: Optional[str] = None,
        resource_type: Optional[str] = None,
        resource_id: Optional[str] = None,
        verification_status: str = "RECEIVED",
    ) -> WebhookInboxRecord:
        """Record an inbound webhook receipt idempotently.
        
        If the exact (tenant_id, provider, environment, event_id) already exists,
        returns the existing record without mutating the original raw evidence.
        """
        stmt = select(WebhookInboxRecord).where(
            WebhookInboxRecord.tenant_id == tenant_id,
            WebhookInboxRecord.provider == provider,
            WebhookInboxRecord.environment == environment,
            WebhookInboxRecord.event_id == event_id,
        )
        existing = self.session.scalars(stmt).first()
        if existing:
            return existing

        headers_str = json.dumps(headers or {})
        rec = WebhookInboxRecord(
            tenant_id=tenant_id,
            provider=provider,
            environment=environment,
            event_id=event_id,
            webhook_id=webhook_id,
            event_type=event_type,
            resource_type=resource_type,
            resource_id=resource_id,
            raw_payload=raw_payload,
            headers_json=headers_str,
            verification_status=verification_status,
            processing_status="RECEIVED",
            attempt_count=0,
            received_at=_utcnow(),
        )
        self.session.add(rec)
        self.session.flush()
        return rec

    def get_by_event_id(
        self,
        tenant_id: str,
        provider: str,
        environment: str,
        event_id: str,
    ) -> Optional[WebhookInboxRecord]:
        """Look up a webhook receipt by provider event coordinates."""
        stmt = select(WebhookInboxRecord).where(
            WebhookInboxRecord.tenant_id == tenant_id,
            WebhookInboxRecord.provider == provider,
            WebhookInboxRecord.environment == environment,
            WebhookInboxRecord.event_id == event_id,
        )
        return self.session.scalars(stmt).first()

    def get_by_id(self, tenant_id: str, record_id: str) -> Optional[WebhookInboxRecord]:
        """Look up a webhook receipt by internal id within a tenant."""
        stmt = select(WebhookInboxRecord).where(
            WebhookInboxRecord.tenant_id == tenant_id,
            WebhookInboxRecord.id == record_id,
        )
        return self.session.scalars(stmt).first()

    def update_verification_status(
        self,
        record_id: str,
        tenant_id: str,
        status: str,
        failure_reason: Optional[str] = None,
    ) -> WebhookInboxRecord:
        """Update signature verification status with strict state-transition guards."""
        rec = self.get_by_id(tenant_id, record_id)
        if rec is None:
            raise EntityNotFoundError(f"Webhook receipt '{record_id}' not found for tenant '{tenant_id}'")

        # Invariant: Once verified, authenticity cannot be downgraded
        if rec.verification_status == "VERIFIED" and status != "VERIFIED":
            raise InvalidWebhookStateTransitionError(
                f"Cannot transition webhook '{record_id}' from VERIFIED to '{status}'"
            )

        # Invariant: Once rejected, an invalid event cannot silently become verified
        if rec.verification_status == "REJECTED" and status == "VERIFIED":
            raise InvalidWebhookStateTransitionError(
                f"Cannot transition webhook '{record_id}' from REJECTED to VERIFIED"
            )

        rec.verification_status = status
        if failure_reason:
            rec.failure_reason = failure_reason
        self.session.flush()
        return rec

    def link_job(
        self,
        record_id: str,
        tenant_id: str,
        job_id: str,
        processing_status: str = "QUEUED",
    ) -> WebhookInboxRecord:
        """Link an enqueued background job to this webhook receipt."""
        rec = self.get_by_id(tenant_id, record_id)
        if rec is None:
            raise EntityNotFoundError(f"Webhook receipt '{record_id}' not found for tenant '{tenant_id}'")

        if rec.verification_status != "VERIFIED":
            raise InvalidWebhookStateTransitionError(
                f"Cannot enqueue job for unverified webhook '{record_id}' (status={rec.verification_status})"
            )

        rec.job_id = job_id
        rec.processing_status = processing_status
        self.session.flush()
        return rec

    def update_processing_status(
        self,
        record_id: str,
        tenant_id: str,
        processing_status: str,
        failure_reason: Optional[str] = None,
        processed_at: Optional[datetime] = None,
    ) -> WebhookInboxRecord:
        """Update processing status after worker execution."""
        rec = self.get_by_id(tenant_id, record_id)
        if rec is None:
            raise EntityNotFoundError(f"Webhook receipt '{record_id}' not found for tenant '{tenant_id}'")

        rec.processing_status = processing_status
        if failure_reason is not None:
            rec.failure_reason = failure_reason
        if processed_at is not None:
            rec.processed_at = processed_at
        elif processing_status == "PROCESSED" and rec.processed_at is None:
            rec.processed_at = _utcnow()

        self.session.flush()
        return rec

    def list_inbox(
        self,
        tenant_id: str,
        processing_status: Optional[str] = None,
        limit: int = 50,
    ) -> Sequence[WebhookInboxRecord]:
        """List webhook receipts for a tenant, optionally filtered by processing status."""
        stmt = select(WebhookInboxRecord).where(WebhookInboxRecord.tenant_id == tenant_id)
        if processing_status:
            stmt = stmt.where(WebhookInboxRecord.processing_status == processing_status)
        stmt = stmt.order_by(WebhookInboxRecord.received_at.desc()).limit(limit)
        return list(self.session.scalars(stmt).all())

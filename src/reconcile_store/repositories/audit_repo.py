"""Audit repository for append-only, tamper-evident hash-chained audit events."""

from __future__ import annotations

from typing import Optional, Sequence, Any, Dict, List
from datetime import datetime, timezone
import uuid
from sqlalchemy import select
from sqlalchemy.orm import Session

from .base import BaseRepository, EntityNotFoundError
from ..schema import AuditEventRecord, TenantRecord
from ..canonical import canonical_json
from ..audit import (
    GENESIS_HASH,
    compute_event_hash,
    create_audit_payload,
    verify_audit_chain,
    AuditIntegrityError,
)


class AuditRepository(BaseRepository[AuditEventRecord]):
    """Repository managing the append-only, tamper-evident SHA-256 audit chain."""

    def append_event(
        self,
        tenant_id: str,
        event_type: str,
        actor: str,
        subject_type: str,
        subject_id: str,
        details: Dict[str, Any],
    ) -> AuditEventRecord:
        """Append a new tamper-evident audit event to the tenant's hash chain."""
        tenant = self.session.get(TenantRecord, tenant_id)
        if tenant is None:
            raise EntityNotFoundError(f"Tenant '{tenant_id}' not found")

        # Canonicalize payload using deterministic format
        payload_json = create_audit_payload(
            tenant_id=tenant_id,
            event_type=event_type,
            actor=actor,
            subject_type=subject_type,
            subject_id=subject_id,
            details=details,
        )

        # Get latest event for this tenant to determine next sequence number and prev_hash
        stmt = (
            select(AuditEventRecord)
            .where(AuditEventRecord.tenant_id == tenant_id)
            .order_by(AuditEventRecord.sequence_number.desc())
            .limit(1)
        )
        latest = self.session.scalars(stmt).first()

        if latest is None:
            seq_num = 1
            prev_hash = GENESIS_HASH
        else:
            seq_num = latest.sequence_number + 1
            prev_hash = latest.event_hash

        event_id = uuid.uuid4().hex
        event_hash = compute_event_hash(prev_hash, payload_json)

        record = AuditEventRecord(
            tenant_id=tenant_id,
            sequence_number=seq_num,
            event_id=event_id,
            event_type=event_type,
            actor=actor,
            subject_type=subject_type,
            subject_id=subject_id,
            payload_json=payload_json,
            prev_hash=prev_hash,
            event_hash=event_hash,
            timestamp=datetime.now(timezone.utc),
        )
        self.session.add(record)
        self.session.flush()
        return record

    def list_events(self, tenant_id: str) -> Sequence[AuditEventRecord]:
        """Retrieve all audit events for a tenant in sequence order."""
        stmt = (
            select(AuditEventRecord)
            .where(AuditEventRecord.tenant_id == tenant_id)
            .order_by(AuditEventRecord.sequence_number.asc())
        )
        return self.session.scalars(stmt).all()

    def verify_tenant_chain(self, tenant_id: str) -> None:
        """Verify the integrity of the audit chain for a tenant."""
        events = list(self.list_events(tenant_id))
        verify_audit_chain(events)

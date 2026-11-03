"""Provider Record repository for immutable source evidence preservation."""

from __future__ import annotations

from typing import Optional, Sequence, Any, Dict
from datetime import datetime, timezone
import uuid
from sqlalchemy import select
from sqlalchemy.orm import Session

from .base import BaseRepository, RepositoryError, ConflictError
from ..schema import ProviderRecord
from ..canonical import canonical_json, hash_content


class ConflictingSourceRecordError(ConflictError):
    """Raised when incoming provider payload conflicts with already stored evidence for the same ID."""


class ProviderRecordRepository(BaseRepository[ProviderRecord]):
    """Repository managing immutable, tamper-evident provider raw records."""

    def store_record(
        self,
        tenant_id: str,
        provider: str,
        record_kind: str,
        provider_record_id: str,
        raw_payload: Dict[str, Any],
        fail_on_conflict: bool = True,
    ) -> ProviderRecord:
        """Store an original provider record idempotently.

        - If identical record exists (same ID and content hash): returns existing record without duplicating.
        - If record with same ID exists but with different content hash: fails closed by raising
          ConflictingSourceRecordError to prevent silent overwriting of financial evidence.
        """
        canonical_str = canonical_json(raw_payload)
        content_hash = hash_content(canonical_str)

        stmt = select(ProviderRecord).where(
            ProviderRecord.tenant_id == tenant_id,
            ProviderRecord.provider == provider,
            ProviderRecord.record_kind == record_kind,
            ProviderRecord.provider_record_id == provider_record_id,
        )
        existing_records = list(self.session.scalars(stmt).all())

        for existing in existing_records:
            if existing.content_hash == content_hash:
                # Exact identical record already stored: idempotent return
                return existing

        if existing_records and fail_on_conflict:
            existing = existing_records[0]
            raise ConflictingSourceRecordError(
                f"Conflicting source record for tenant '{tenant_id}', provider '{provider}', "
                f"kind '{record_kind}', id '{provider_record_id}'. "
                f"Existing hash: {existing.content_hash}, Incoming hash: {content_hash}. "
                f"Immutable evidence cannot be overwritten."
            )

        new_record = ProviderRecord(
            id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            provider=provider,
            record_kind=record_kind,
            provider_record_id=provider_record_id,
            payload_json=canonical_str,
            content_hash=content_hash,
            ingested_at=datetime.now(timezone.utc),
        )
        self.session.add(new_record)
        self.session.flush()
        return new_record

    def get_record(
        self, tenant_id: str, provider: str, record_kind: str, provider_record_id: str
    ) -> Optional[ProviderRecord]:
        stmt = (
            select(ProviderRecord)
            .where(
                ProviderRecord.tenant_id == tenant_id,
                ProviderRecord.provider == provider,
                ProviderRecord.record_kind == record_kind,
                ProviderRecord.provider_record_id == provider_record_id,
            )
            .order_by(ProviderRecord.ingested_at.desc())
        )
        return self.session.scalars(stmt).first()

    def get_record_by_id(self, tenant_id: str, record_id: str) -> Optional[ProviderRecord]:
        record = self.session.get(ProviderRecord, record_id)
        if record:
            self._ensure_tenant(record.tenant_id, tenant_id)
        return record

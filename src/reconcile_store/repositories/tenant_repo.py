"""Tenant and Connection repositories."""

from __future__ import annotations

from typing import Optional, Sequence
from datetime import datetime, timezone
from sqlalchemy import select
from sqlalchemy.orm import Session

from .base import BaseRepository, EntityNotFoundError, ConflictError
from ..schema import TenantRecord, ConnectionRecord


class TenantRepository(BaseRepository[TenantRecord]):
    """Repository for tenant management."""

    def create_tenant(self, tenant_id: str, name: str) -> TenantRecord:
        existing = self.session.get(TenantRecord, tenant_id)
        if existing:
            raise ConflictError(f"Tenant '{tenant_id}' already exists")
        tenant = TenantRecord(
            id=tenant_id,
            name=name,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        self.session.add(tenant)
        self.session.flush()
        return tenant

    def get_tenant(self, tenant_id: str) -> Optional[TenantRecord]:
        return self.session.get(TenantRecord, tenant_id)

    def get_or_create_tenant(self, tenant_id: str, name: str) -> TenantRecord:
        tenant = self.get_tenant(tenant_id)
        if tenant is None:
            tenant = self.create_tenant(tenant_id, name)
        return tenant

    def list_tenants(self) -> Sequence[TenantRecord]:
        stmt = select(TenantRecord).order_by(TenantRecord.created_at)
        return self.session.scalars(stmt).all()


class ConnectionRepository(BaseRepository[ConnectionRecord]):
    """Repository for payment provider connections."""

    def create_connection(
        self,
        tenant_id: str,
        provider: str,
        environment: str,
        credential_reference: str,
        status: str = "ACTIVE",
    ) -> ConnectionRecord:
        # Security invariant: credential_reference must be an indirect key/secret reference, not raw secrets
        if credential_reference.startswith("Bearer ") or credential_reference.startswith("secret_"):
            raise ValueError("Raw tokens or secrets must not be stored in credential_reference")

        stmt = select(ConnectionRecord).where(
            ConnectionRecord.tenant_id == tenant_id,
            ConnectionRecord.provider == provider,
            ConnectionRecord.environment == environment,
        )
        existing = self.session.scalars(stmt).first()
        if existing:
            raise ConflictError(
                f"Connection for tenant '{tenant_id}', provider '{provider}', env '{environment}' already exists"
            )

        conn = ConnectionRecord(
            tenant_id=tenant_id,
            provider=provider,
            environment=environment,
            credential_ref=credential_reference,
            status=status,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        self.session.add(conn)
        self.session.flush()
        return conn

    def get_connection(
        self, tenant_id: str, provider: str, environment: str
    ) -> Optional[ConnectionRecord]:
        stmt = select(ConnectionRecord).where(
            ConnectionRecord.tenant_id == tenant_id,
            ConnectionRecord.provider == provider,
            ConnectionRecord.environment == environment,
        )
        return self.session.scalars(stmt).first()

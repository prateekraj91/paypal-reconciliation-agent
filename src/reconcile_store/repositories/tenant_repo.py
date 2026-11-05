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

    VALID_ENVIRONMENTS = frozenset({"sandbox", "live"})
    VALID_STATUSES = frozenset({"ACTIVE", "INACTIVE", "DISABLED"})

    @classmethod
    def validate_credential_reference(cls, credential_reference: str) -> str:
        """Validate that credential_reference is an indirect reference and not a raw secret or token.

        Enforces security invariant: raw OAuth tokens, API secrets and bearer tokens
        must never be stored in the persistence layer.
        """
        if not credential_reference or not isinstance(credential_reference, str):
            raise ValueError("credential_reference must be a non-empty string")

        ref = credential_reference.strip()
        if not ref:
            raise ValueError("credential_reference cannot be blank")

        lower = ref.lower()
        if (
            lower.startswith("bearer")
            or lower.startswith("basic")
            or lower.startswith("secret_")
            or lower.startswith("sk_")
            or lower.startswith("pk_")
            or lower.startswith("a21aa")
            or len(ref) > 128
        ):
            raise ValueError(
                "Raw tokens or secrets must not be stored in credential_reference. "
                "Use an indirect reference (e.g. 'env:PAYPAL_SANDBOX' or 'vault:path')."
            )

        if any(c in ref for c in ("\n", "\r", "\t", " ")):
            raise ValueError("credential_reference cannot contain whitespace or line breaks")

        # Must be an indirect identifier scheme
        valid_prefixes = ("env", "vault:", "secretsmanager:", "ref:", "file:")
        if not (ref == "env" or any(ref.startswith(p) for p in valid_prefixes)):
            raise ValueError(
                f"Invalid credential reference scheme '{ref}'. "
                "Must use an indirect scheme such as 'env:PAYPAL_SANDBOX', 'env', or 'vault:path'."
            )

        return ref

    def create_connection(
        self,
        tenant_id: str,
        provider: str,
        environment: str,
        credential_reference: str,
        status: str = "ACTIVE",
    ) -> ConnectionRecord:
        env_norm = environment.lower().strip()
        if env_norm not in self.VALID_ENVIRONMENTS:
            raise ValueError(f"Invalid environment '{environment}'. Expected 'sandbox' or 'live'.")

        status_norm = status.upper().strip()
        if status_norm not in self.VALID_STATUSES:
            raise ValueError(f"Invalid status '{status}'. Expected one of {sorted(self.VALID_STATUSES)}.")

        clean_ref = self.validate_credential_reference(credential_reference)

        stmt = select(ConnectionRecord).where(
            ConnectionRecord.tenant_id == tenant_id,
            ConnectionRecord.provider == provider,
            ConnectionRecord.environment == env_norm,
        )
        existing = self.session.scalars(stmt).first()
        if existing:
            raise ConflictError(
                f"Connection for tenant '{tenant_id}', provider '{provider}', env '{env_norm}' already exists"
            )

        now = datetime.now(timezone.utc)
        conn = ConnectionRecord(
            tenant_id=tenant_id,
            provider=provider,
            environment=env_norm,
            credential_ref=clean_ref,
            status=status_norm,
            created_at=now,
            updated_at=now,
        )
        self.session.add(conn)
        self.session.flush()
        return conn

    def upsert_connection(
        self,
        tenant_id: str,
        provider: str,
        environment: str,
        credential_reference: str,
        status: str = "ACTIVE",
    ) -> ConnectionRecord:
        env_norm = environment.lower().strip()
        if env_norm not in self.VALID_ENVIRONMENTS:
            raise ValueError(f"Invalid environment '{environment}'. Expected 'sandbox' or 'live'.")

        status_norm = status.upper().strip()
        if status_norm not in self.VALID_STATUSES:
            raise ValueError(f"Invalid status '{status}'. Expected one of {sorted(self.VALID_STATUSES)}.")

        clean_ref = self.validate_credential_reference(credential_reference)

        conn = self.get_connection(tenant_id, provider, env_norm)
        now = datetime.now(timezone.utc)
        if conn is None:
            conn = ConnectionRecord(
                tenant_id=tenant_id,
                provider=provider,
                environment=env_norm,
                credential_ref=clean_ref,
                status=status_norm,
                created_at=now,
                updated_at=now,
            )
            self.session.add(conn)
        else:
            conn.credential_ref = clean_ref
            conn.status = status_norm
            conn.updated_at = now

        self.session.flush()
        return conn

    def get_connection(
        self, tenant_id: str, provider: str, environment: str
    ) -> Optional[ConnectionRecord]:
        stmt = select(ConnectionRecord).where(
            ConnectionRecord.tenant_id == tenant_id,
            ConnectionRecord.provider == provider,
            ConnectionRecord.environment == environment.lower().strip(),
        )
        return self.session.scalars(stmt).first()

    def list_connections(self, tenant_id: Optional[str] = None) -> Sequence[ConnectionRecord]:
        stmt = select(ConnectionRecord)
        if tenant_id:
            stmt = stmt.where(ConnectionRecord.tenant_id == tenant_id)
        stmt = stmt.order_by(ConnectionRecord.created_at)
        return self.session.scalars(stmt).all()

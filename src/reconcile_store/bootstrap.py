"""Idempotent database bootstrapping for initial tenant and provider connection.

Ensures that empty production or test databases have the default tenant and active
PayPal provider connection created without hardcoding secrets or credentials.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from typing import Optional, Tuple

from sqlalchemy.orm import Session

from .config import DatabaseConfig
from .db import DatabaseEngine
from .repositories.tenant_repo import ConnectionRepository, TenantRepository
from .schema import ConnectionRecord, TenantRecord

logger = logging.getLogger("reconcile_bootstrap")


def bootstrap_database(
    session: Session,
    tenant_id: Optional[str] = None,
    tenant_name: Optional[str] = None,
    provider: str = "paypal",
    environment: Optional[str] = None,
    credential_ref: Optional[str] = None,
) -> Tuple[TenantRecord, ConnectionRecord]:
    """Idempotently bootstrap the default tenant and connection.
    
    Safe to execute multiple times against fresh or existing databases.
    Only stores metadata and indirect credential references, never raw secrets.
    """
    resolved_tenant = tenant_id or os.getenv("DEFAULT_TENANT_ID", "tenant_paypal_sandbox_us").strip()
    resolved_env = (environment or os.getenv("PAYPAL_ENVIRONMENT", "sandbox")).strip().lower()
    resolved_ref = (
        credential_ref
        or os.getenv("DEFAULT_CREDENTIAL_REF")
        or ("env:PAYPAL_SANDBOX" if resolved_env == "sandbox" else "env:PAYPAL_LIVE")
    ).strip()
    resolved_name = tenant_name or f"Production Tenant ({resolved_tenant})"

    tenant_repo = TenantRepository(session)
    conn_repo = ConnectionRepository(session)

    # 1. Ensure Tenant exists
    tenant = tenant_repo.get_or_create_tenant(resolved_tenant, resolved_name)

    # 2. Ensure Provider Connection exists and is ACTIVE
    existing_conn = conn_repo.get_connection(resolved_tenant, provider=provider, environment=resolved_env)
    if existing_conn is None:
        conn = conn_repo.create_connection(
            tenant_id=resolved_tenant,
            provider=provider,
            environment=resolved_env,
            credential_reference=resolved_ref,
            status="ACTIVE",
        )
        logger.info(
            "Bootstrapped connection '%s' for tenant '%s' (env=%s, ref=%s)",
            conn.id,
            resolved_tenant,
            resolved_env,
            resolved_ref,
        )
    else:
        conn = existing_conn
        if conn.status != "ACTIVE":
            conn.status = "ACTIVE"
        logger.info(
            "Connection for tenant '%s' (env=%s) already exists with ref '%s'",
            resolved_tenant,
            resolved_env,
            conn.credential_ref,
        )

    session.commit()
    return tenant, conn


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(description="Bootstrap default tenant and payment connection")
    parser.add_argument("--tenant-id", type=str, default=None, help="Tenant ID (defaults to DEFAULT_TENANT_ID env var)")
    parser.add_argument("--environment", type=str, default=None, help="PayPal environment (sandbox or live)")
    parser.add_argument("--credential-ref", type=str, default=None, help="Indirect credential reference (e.g. env:PAYPAL_SANDBOX)")
    args = parser.parse_args()

    engine = DatabaseEngine(DatabaseConfig.from_env(primary_env_var="MIGRATION_DB_URL"))
    with engine.session() as session:
        try:
            tenant, conn = bootstrap_database(
                session=session,
                tenant_id=args.tenant_id,
                environment=args.environment,
                credential_ref=args.credential_ref,
            )
            logger.info("Successfully verified bootstrap for tenant '%s' and connection '%s'", tenant.id, conn.id)
        except Exception as exc:
            logger.error("Failed to bootstrap database: %s", exc, exc_info=True)
            sys.exit(1)


if __name__ == "__main__":
    main()

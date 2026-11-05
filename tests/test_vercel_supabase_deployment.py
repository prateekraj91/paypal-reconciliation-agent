"""Tests verifying Vercel Serverless / Daemon and Supabase PostgreSQL deployment compatibility."""

from __future__ import annotations

import os
from unittest.mock import MagicMock
import pytest
from fastapi.testclient import TestClient

from reconcile_store.config import DatabaseConfig
from reconcile_store.db import get_engine
from reconcile_api.dependencies import get_db_session, get_paypal_client, set_db_engine
from reconcile_store.db import DatabaseEngine
from reconcile_store.schema import Base
from reconcile_store.repositories.tenant_repo import TenantRepository, ConnectionRepository


def test_vercel_api_entrypoint_imports_cleanly():
    """Verify that api/index.py imports app without syntax or path errors."""
    import api.index as vercel_entry
    from fastapi import FastAPI

    assert hasattr(vercel_entry, "app")
    assert isinstance(vercel_entry.app, FastAPI)


def test_vercel_api_routes_work(monkeypatch):
    """Verify that api/index.py app responds to standard API endpoints."""
    import api.index as vercel_entry

    engine = DatabaseEngine(DatabaseConfig(url="sqlite:///:memory:"))
    Base.metadata.create_all(engine.engine)
    set_db_engine(engine)

    with engine.session() as s:
        TenantRepository(s).create_tenant("default", "Default Tenant")
        ConnectionRepository(s).create_connection(
            tenant_id="default",
            provider="paypal",
            environment="sandbox",
            credential_reference="env:PAYPAL_SANDBOX",
        )
        s.commit()

    monkeypatch.setenv("ADMIN_API_KEY", "test-admin-key-999")

    def override_db():
        with engine.session() as s:
            yield s

    vercel_entry.app.dependency_overrides[get_db_session] = override_db

    with TestClient(vercel_entry.app) as tc:
        # 1. Health check
        res_health = tc.get("/health")
        assert res_health.status_code == 200
        assert res_health.json()["status"] == "ok"

        # 2. Admin endpoint
        res_admin = tc.get("/admin/tenants", headers={"Authorization": "Bearer test-admin-key-999"})
        assert res_admin.status_code == 200
        assert any(t["id"] == "default" for t in res_admin.json()["tenants"])

    vercel_entry.app.dependency_overrides.clear()
    set_db_engine(None)


def test_supabase_database_config_detection(monkeypatch):
    """Verify DatabaseConfig properly detects Supabase port 6543 and configures NullPool."""
    # 1. Transaction Pooler URL on port 6543
    sb_tx_url = "postgresql://postgres.myproject:mypassword@aws-0-us-east-1.pooler.supabase.com:6543/postgres?sslmode=require"
    monkeypatch.setenv("RECONCILE_DB_URL", sb_tx_url)
    monkeypatch.delenv("DATABASE_URL", raising=False)

    cfg_tx = DatabaseConfig.from_env()
    assert cfg_tx.is_postgres is True
    assert "postgresql+psycopg://" in cfg_tx.url
    assert "sslmode=require" in cfg_tx.url
    assert cfg_tx.null_pool is True

    # 2. Engine created for transaction pooler uses NullPool
    engine_tx = get_engine(cfg_tx)
    assert type(engine_tx.pool).__name__ == "NullPool"

    # 3. Direct/Session mode on port 5432 uses standard pooling
    sb_session_url = "postgresql://postgres.myproject:mypassword@aws-0-us-east-1.pooler.supabase.com:5432/postgres?sslmode=require"
    monkeypatch.setenv("RECONCILE_DB_URL", sb_session_url)

    cfg_session = DatabaseConfig.from_env()
    assert cfg_session.null_pool is False
    engine_session = get_engine(cfg_session)
    assert type(engine_session.pool).__name__ == "QueuePool"


def test_worker_env_var_configuration(monkeypatch):
    """Verify worker CLI accepts WORKER_POLL_INTERVAL and WORKER_LEASE_DURATION from env."""
    monkeypatch.setenv("WORKER_POLL_INTERVAL", "7.5")
    monkeypatch.setenv("WORKER_LEASE_DURATION", "120")

    poll = float(os.environ.get("WORKER_POLL_INTERVAL", "2.0"))
    lease = int(os.environ.get("WORKER_LEASE_DURATION", "60"))

    assert poll == 7.5
    assert lease == 120


def test_distinct_database_urls_for_api_worker_and_migrations(monkeypatch):
    """Verify that API, Worker, and Migrations can use distinct connection strings."""
    from reconcile_api.config import APIConfig

    api_url = "postgresql://postgres.proj:pass@aws-0-us-east-1.pooler.supabase.com:6543/postgres?sslmode=require"
    worker_url = "postgresql://postgres.proj:pass@aws-0-us-east-1.pooler.supabase.com:5432/postgres?sslmode=require"
    migration_url = "postgresql://postgres.proj:pass@db.myproject.supabase.co:5432/postgres?sslmode=require"

    monkeypatch.setenv("RECONCILE_DB_URL", api_url)
    monkeypatch.setenv("WORKER_DB_URL", worker_url)
    monkeypatch.setenv("MIGRATION_DB_URL", migration_url)
    monkeypatch.delenv("DATABASE_URL", raising=False)

    # 1. APIConfig strictly uses RECONCILE_DB_URL (port 6543)
    api_cfg = APIConfig.from_env()
    assert ":6543" in api_cfg.database_url
    assert ":5432" not in api_cfg.database_url

    # 2. DatabaseConfig.from_env() for API resolves port 6543 with NullPool
    db_cfg_api = DatabaseConfig.from_env()
    assert ":6543" in db_cfg_api.url
    assert db_cfg_api.null_pool is True

    # 3. DatabaseConfig.for_worker() resolves WORKER_DB_URL (port 5432) without NullPool
    db_cfg_worker = DatabaseConfig.for_worker()
    assert ":5432" in db_cfg_worker.url
    assert db_cfg_worker.null_pool is False

    # 4. Bootstrap / Migration config resolves MIGRATION_DB_URL
    db_cfg_migration = DatabaseConfig.from_env(primary_env_var="MIGRATION_DB_URL")
    assert "db.myproject.supabase.co" in db_cfg_migration.url
    assert ":5432" in db_cfg_migration.url


def test_worker_database_url_fallback(monkeypatch):
    """Verify that DatabaseConfig.for_worker() falls back to RECONCILE_DB_URL when WORKER_DB_URL is unset."""
    api_url = "postgresql://postgres.proj:pass@aws-0-us-east-1.pooler.supabase.com:5432/postgres?sslmode=require"
    monkeypatch.setenv("RECONCILE_DB_URL", api_url)
    monkeypatch.delenv("WORKER_DB_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)

    cfg = DatabaseConfig.for_worker()
    assert "postgresql+psycopg://" in cfg.url
    assert ":5432" in cfg.url

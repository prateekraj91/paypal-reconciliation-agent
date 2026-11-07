"""API tests for D6 AI Exception Investigation endpoints."""

from __future__ import annotations

import os
from unittest.mock import MagicMock
import pytest
from fastapi.testclient import TestClient

from reconcile_api.config import APIConfig
from reconcile_api.dependencies import set_db_engine
from reconcile_api.main import app
from reconcile_store.config import DatabaseConfig
from reconcile_store.db import DatabaseEngine
from reconcile_store.schema import Base
from reconcile_store.repositories.tenant_repo import TenantRepository
from reconcile_store.workflow import StorageWorkflow
from reconcile.models import Payment, PaymentStatus
from datetime import datetime, timezone


@pytest.fixture
def test_db():
    """In-memory database for API test isolation."""
    engine = DatabaseEngine(DatabaseConfig(url="sqlite:///:memory:"))
    Base.metadata.create_all(engine.engine)
    set_db_engine(engine)
    config = APIConfig.from_env()
    with engine.session() as s:
        TenantRepository(s).create_tenant("test_tenant", "Test Tenant")
        s.commit()
    yield engine
    set_db_engine(None)


@pytest.fixture
def client(test_db, monkeypatch):
    monkeypatch.setenv("ADMIN_API_KEY", "test-secret-key-12345")
    monkeypatch.setenv("DEFAULT_TENANT_ID", "test_tenant")
    monkeypatch.setenv("MODEL_PROVIDER", "mock")
    return TestClient(app)


def test_submit_investigation_async_enqueue(client, test_db):
    headers = {"Authorization": "Bearer test-secret-key-12345"}
    resp = client.post(
        "/admin/investigations",
        json={
            "tenant_id": "test_tenant",
            "target_type": "payment",
            "target_id": "PAY-API-101",
            "sync_mode": False,
        },
        headers=headers,
    )
    assert resp.status_code == 202
    data = resp.json()
    assert data["status"] == "queued"
    assert "investigation_id" in data
    assert "job_id" in data


def test_submit_investigation_sync_mode(client, test_db):
    # Ingest a real payment to investigate
    with test_db.session() as s:
        workflow = StorageWorkflow(s)
        pay = Payment(
            payment_id="PAY-SYNC-01",
            amount_cents=12000,
            currency="USD",
            received_at=datetime(2026, 1, 10, tzinfo=timezone.utc),
            payer_name="Sync Client",
            status=PaymentStatus.COMPLETED,
        )
        workflow.ingest_payments("test_tenant", [pay])

    headers = {"Authorization": "Bearer test-secret-key-12345"}
    resp = client.post(
        "/admin/investigations",
        json={
            "tenant_id": "test_tenant",
            "target_type": "payment",
            "target_id": "PAY-SYNC-01",
            "sync_mode": True,
        },
        headers=headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] in ("COMPLETED", "NEEDS_REVIEW")
    assert "classification" in data
    assert "summary" in data
    assert data["verifier_status"] in ("VERIFIED", "NEEDS_REVIEW")


def test_list_and_get_investigations(client, test_db):
    headers = {"Authorization": "Bearer test-secret-key-12345"}
    # Enqueue two investigations
    client.post(
        "/admin/investigations",
        json={"tenant_id": "test_tenant", "target_type": "payment", "target_id": "PAY-A"},
        headers=headers,
    )
    post_res = client.post(
        "/admin/investigations",
        json={"tenant_id": "test_tenant", "target_type": "payment", "target_id": "PAY-B"},
        headers=headers,
    )
    inv_id = post_res.json()["investigation_id"]

    # List
    list_res = client.get("/admin/investigations?tenant_id=test_tenant", headers=headers)
    assert list_res.status_code == 200
    invs = list_res.json()["investigations"]
    assert len(invs) >= 2

    # Get single
    get_res = client.get(f"/admin/investigations/{inv_id}?tenant_id=test_tenant", headers=headers)
    assert get_res.status_code == 200
    item = get_res.json()
    assert item["id"] == inv_id
    assert item["target_id"] == "PAY-B"


def test_unauthorized_access_rejected(client):
    # Without bearer token
    resp = client.post(
        "/admin/investigations",
        json={"tenant_id": "test_tenant", "target_type": "payment", "target_id": "PAY-1"},
    )
    assert resp.status_code in (401, 403)


def test_nonexistent_tenant_rejected(client):
    headers = {"Authorization": "Bearer test-secret-key-12345"}
    resp = client.post(
        "/admin/investigations",
        json={"tenant_id": "ghost_tenant", "target_type": "payment", "target_id": "PAY-1"},
        headers=headers,
    )
    assert resp.status_code == 404

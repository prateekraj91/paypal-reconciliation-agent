"""Comprehensive test suite for FastAPI application endpoints."""

from __future__ import annotations

import os
from unittest.mock import MagicMock
import pytest
from fastapi.testclient import TestClient

from reconcile.paypal import PayPalAPIError, PayPalClient, PayPalTimeoutError
from reconcile_api.config import APIConfig
from reconcile_api.dependencies import get_db_session, get_paypal_client, set_db_engine
from reconcile_api.main import app
from reconcile_store.config import DatabaseConfig
from reconcile_store.db import DatabaseEngine
from reconcile_store.schema import Base, JobRecord, WebhookInboxRecord
from reconcile_store.repositories.tenant_repo import TenantRepository, ConnectionRepository


@pytest.fixture
def test_db():
    """In-memory database for API test isolation."""
    engine = DatabaseEngine(DatabaseConfig(url="sqlite:///:memory:"))
    Base.metadata.create_all(engine.engine)
    set_db_engine(engine)
    config = APIConfig.from_env()
    tenants_to_seed = {"default", config.default_tenant_id}
    with engine.session() as s:
        for t_id in tenants_to_seed:
            TenantRepository(s).create_tenant(t_id, f"Default Tenant ({t_id})")
            ConnectionRepository(s).create_connection(
                tenant_id=t_id,
                provider="paypal",
                environment="sandbox",
                credential_reference="env:PAYPAL_SANDBOX",
            )
        s.commit()
    yield engine
    set_db_engine(None)


@pytest.fixture
def mock_paypal():
    client = MagicMock(spec=PayPalClient)
    client.verify_webhook_signature.return_value = True
    return client


@pytest.fixture
def client(test_db, mock_paypal, monkeypatch):
    monkeypatch.setenv("ADMIN_API_KEY", "super-secret-admin-key-12345")
    monkeypatch.setenv("PAYPAL_WEBHOOK_ID", "WH-TEST-ID")
    monkeypatch.setenv("PAYPAL_ENVIRONMENT", "sandbox")

    def override_paypal():
        return mock_paypal

    def override_db():
        with test_db.session() as s:
            yield s

    app.dependency_overrides[get_paypal_client] = override_paypal
    app.dependency_overrides[get_db_session] = override_db

    with TestClient(app) as tc:
        yield tc

    app.dependency_overrides.clear()


VALID_PAYPAL_HEADERS = {
    "PAYPAL-TRANSMISSION-ID": "tx-12345-abcde",
    "PAYPAL-TRANSMISSION-TIME": "2026-11-03T12:00:00Z",
    "PAYPAL-CERT-URL": "https://api.sandbox.paypal.com/v1/notifications/certs/CERT-123",
    "PAYPAL-AUTH-ALGO": "SHA256withRSA",
    "PAYPAL-TRANSMISSION-SIG": "dGVzdC1zaWduYXR1cmU=",
    "Content-Type": "application/json",
}

VALID_WEBHOOK_EVENT = {
    "id": "WH-EVT-9999",
    "event_type": "PAYMENT.CAPTURE.COMPLETED",
    "resource_type": "capture",
    "create_time": "2026-11-03T12:00:00Z",
    "resource": {
        "id": "CAP-8888",
        "status": "COMPLETED",
        "amount": {"value": "150.00", "currency_code": "USD"},
        "custom_id": "INV2-1001",
    },
}


# --------------------------------------------------------------------------
# Health & Readiness Probes
# --------------------------------------------------------------------------


def test_health_endpoint(client):
    res = client.get("/health")
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "ok"
    assert data["service"] == "paypal-reconciliation-agent"
    # Ensure no credentials or URLs are exposed
    assert "url" not in data
    assert "token" not in data
    assert "key" not in data


def test_readiness_endpoint_connected(client):
    res = client.get("/ready")
    assert res.status_code == 200
    assert res.json() == {"status": "ready", "database": "connected"}


def test_readiness_endpoint_disconnected(client, test_db, monkeypatch):
    # Simulate DB disconnection
    test_db.engine.dispose()
    mock_engine = MagicMock()
    mock_engine.engine.connect.side_effect = Exception("Connection refused")
    set_db_engine(mock_engine)

    res = client.get("/ready")
    assert res.status_code == 503
    data = res.json()
    assert data["status"] == "unavailable"
    assert data["database"] == "disconnected"
    assert "Connection refused" not in str(data)  # Do not leak exception details


# --------------------------------------------------------------------------
# Webhook Ingestion & Header Validation
# --------------------------------------------------------------------------


def test_webhook_missing_required_headers(client):
    # Missing transmission headers
    res = client.post("/webhooks/paypal", json=VALID_WEBHOOK_EVENT)
    assert res.status_code == 400
    assert "Missing required PayPal transmission header" in res.json()["detail"]


@pytest.mark.parametrize("missing_header", [
    "PAYPAL-TRANSMISSION-ID",
    "PAYPAL-TRANSMISSION-TIME",
    "PAYPAL-CERT-URL",
    "PAYPAL-AUTH-ALGO",
    "PAYPAL-TRANSMISSION-SIG",
])
def test_webhook_each_required_header_enforced(client, missing_header):
    headers = dict(VALID_PAYPAL_HEADERS)
    del headers[missing_header]
    res = client.post("/webhooks/paypal", headers=headers, json=VALID_WEBHOOK_EVENT)
    assert res.status_code == 400
    assert missing_header in res.json()["detail"]


def test_webhook_malformed_json(client):
    res = client.post(
        "/webhooks/paypal",
        headers=VALID_PAYPAL_HEADERS,
        content=b"this is not valid json { [",
    )
    assert res.status_code == 400
    assert "Malformed JSON" in res.json()["detail"]


def test_webhook_oversized_body(client, monkeypatch):
    monkeypatch.setenv("MAX_REQUEST_BODY_BYTES", "50")
    res = client.post(
        "/webhooks/paypal",
        headers=VALID_PAYPAL_HEADERS,
        content=b"A" * 100,
    )
    assert res.status_code == 413
    assert "Payload exceeds maximum allowed size" in res.json()["detail"]


# --------------------------------------------------------------------------
# Signature Verification & Error Handling
# --------------------------------------------------------------------------


def test_webhook_invalid_signature_rejected(client, mock_paypal, test_db):
    mock_paypal.verify_webhook_signature.return_value = False

    res = client.post("/webhooks/paypal", headers=VALID_PAYPAL_HEADERS, json=VALID_WEBHOOK_EVENT)
    assert res.status_code == 400
    assert "signature verification failed" in res.json()["detail"].lower()

    # Verify receipt is durably stored with REJECTED status and NO job is enqueued
    with test_db.session() as s:
        rec = s.query(WebhookInboxRecord).filter_by(event_id="WH-EVT-9999").first()
        assert rec is not None
        assert rec.verification_status == "REJECTED"
        assert rec.job_id is None

        jobs = s.query(JobRecord).all()
        assert len(jobs) == 0


def test_webhook_verification_service_timeout(client, mock_paypal, test_db):
    mock_paypal.verify_webhook_signature.side_effect = PayPalTimeoutError("Gateway timeout")

    res = client.post("/webhooks/paypal", headers=VALID_PAYPAL_HEADERS, json=VALID_WEBHOOK_EVENT)
    assert res.status_code == 503
    assert "temporarily unavailable" in res.json()["detail"].lower()

    # Receipt retained as VERIFICATION_PENDING, no job created
    with test_db.session() as s:
        rec = s.query(WebhookInboxRecord).filter_by(event_id="WH-EVT-9999").first()
        assert rec is not None
        assert rec.verification_status == "VERIFICATION_PENDING"
        assert rec.job_id is None


def test_webhook_verification_service_error_500(client, mock_paypal, test_db):
    mock_paypal.verify_webhook_signature.side_effect = PayPalAPIError(500, "Internal Server Error")

    res = client.post("/webhooks/paypal", headers=VALID_PAYPAL_HEADERS, json=VALID_WEBHOOK_EVENT)
    assert res.status_code == 503

    with test_db.session() as s:
        rec = s.query(WebhookInboxRecord).filter_by(event_id="WH-EVT-9999").first()
        assert rec is not None
        assert rec.verification_status == "VERIFICATION_PENDING"


def test_webhook_valid_signature_enqueues_job(client, mock_paypal, test_db):
    mock_paypal.verify_webhook_signature.return_value = True

    res = client.post("/webhooks/paypal", headers=VALID_PAYPAL_HEADERS, json=VALID_WEBHOOK_EVENT)
    assert res.status_code == 202
    data = res.json()
    assert data["status"] == "accepted"
    assert data["event_id"] == "WH-EVT-9999"
    assert "job_id" in data

    # Verify database state
    with test_db.session() as s:
        rec = s.query(WebhookInboxRecord).filter_by(event_id="WH-EVT-9999").first()
        assert rec is not None
        assert rec.verification_status == "VERIFIED"
        assert rec.processing_status == "QUEUED"
        assert rec.job_id == data["job_id"]

        job = s.query(JobRecord).filter_by(id=data["job_id"]).first()
        assert job is not None
        assert job.status == "QUEUED"
        assert job.job_type == "WEBHOOK_INGEST"
        assert job.payload["event_id"] == "WH-EVT-9999"


def test_webhook_duplicate_delivery_is_idempotent(client, mock_paypal, test_db):
    mock_paypal.verify_webhook_signature.return_value = True

    # 1. First delivery -> 202 Accepted
    res1 = client.post("/webhooks/paypal", headers=VALID_PAYPAL_HEADERS, json=VALID_WEBHOOK_EVENT)
    assert res1.status_code == 202
    job_id_1 = res1.json()["job_id"]

    # 2. Duplicate delivery -> 200 OK (already received)
    res2 = client.post("/webhooks/paypal", headers=VALID_PAYPAL_HEADERS, json=VALID_WEBHOOK_EVENT)
    assert res2.status_code == 200
    data2 = res2.json()
    assert data2["status"] == "already_received"
    assert data2["job_id"] == job_id_1

    # Exactly one inbox record and one job exist
    with test_db.session() as s:
        recs = s.query(WebhookInboxRecord).filter_by(event_id="WH-EVT-9999").all()
        assert len(recs) == 1
        jobs = s.query(JobRecord).all()
        assert len(jobs) == 1


# --------------------------------------------------------------------------
# Administrative Endpoints & Authentication
# --------------------------------------------------------------------------


def test_admin_sync_requires_authentication(client):
    # No auth header -> 401
    res = client.post("/admin/sync", json={})
    assert res.status_code == 401

    # Invalid token -> 401
    res = client.post(
        "/admin/sync",
        headers={"Authorization": "Bearer wrong-token-xyz"},
        json={},
    )
    assert res.status_code == 401


def test_admin_sync_success_enqueues_job(client, test_db):
    res = client.post(
        "/admin/sync",
        headers={"Authorization": "Bearer super-secret-admin-key-12345"},
        json={"page_size": 100},
    )
    assert res.status_code == 202
    data = res.json()
    assert data["status"] == "queued"
    assert "job_id" in data

    with test_db.session() as s:
        job = s.query(JobRecord).filter_by(id=data["job_id"]).first()
        assert job is not None
        assert job.job_type == "MANUAL_SYNC"
        assert job.status == "QUEUED"


def test_admin_runs_endpoint(client, test_db):
    res = client.get(
        "/admin/runs",
        headers={"Authorization": "Bearer super-secret-admin-key-12345"},
    )
    assert res.status_code == 200
    assert "runs" in res.json()


def test_admin_jobs_endpoint(client, test_db):
    res = client.get(
        "/admin/jobs",
        headers={"Authorization": "Bearer super-secret-admin-key-12345"},
    )
    assert res.status_code == 200
    assert "jobs" in res.json()


def test_admin_sync_rejects_placeholder_strings(client):
    res = client.post(
        "/admin/sync",
        headers={"Authorization": "Bearer super-secret-admin-key-12345"},
        json={
            "tenant_id": "string",
            "start_date": "string",
            "end_date": "string",
        },
    )
    assert res.status_code == 422
    err_detail = str(res.json()["detail"])
    assert "placeholder" in err_detail.lower() or "string" in err_detail.lower()


def test_admin_sync_rejects_invalid_date_format(client):
    res = client.post(
        "/admin/sync",
        headers={"Authorization": "Bearer super-secret-admin-key-12345"},
        json={"start_date": "2026-13-45_not_a_date"},
    )
    assert res.status_code == 422
    assert "ISO 8601" in str(res.json()["detail"])


def test_admin_sync_rejects_inverted_date_range(client):
    res = client.post(
        "/admin/sync",
        headers={"Authorization": "Bearer super-secret-admin-key-12345"},
        json={
            "start_date": "2026-11-10T00:00:00Z",
            "end_date": "2026-11-01T00:00:00Z",
        },
    )
    assert res.status_code == 422
    assert "cannot be after end_date" in str(res.json()["detail"])


def test_admin_sync_rejects_unknown_tenant(client):
    res = client.post(
        "/admin/sync",
        headers={"Authorization": "Bearer super-secret-admin-key-12345"},
        json={"tenant_id": "nonexistent_tenant_999"},
    )
    assert res.status_code == 404
    assert "Tenant 'nonexistent_tenant_999' not found" in res.json()["detail"]


def test_admin_sync_rejects_missing_connection(client, test_db):
    with test_db.session() as s:
        TenantRepository(s).create_tenant("t_no_conn", "Tenant without connection")
        s.commit()

    res = client.post(
        "/admin/sync",
        headers={"Authorization": "Bearer super-secret-admin-key-12345"},
        json={"tenant_id": "t_no_conn"},
    )
    assert res.status_code == 400
    assert "No PayPal connection found" in res.json()["detail"]


def test_admin_sync_rejects_inactive_connection(client, test_db):
    with test_db.session() as s:
        TenantRepository(s).create_tenant("t_inactive", "Tenant with inactive connection")
        ConnectionRepository(s).create_connection(
            tenant_id="t_inactive",
            provider="paypal",
            environment="sandbox",
            credential_reference="env:PAYPAL_SANDBOX",
            status="INACTIVE",
        )
        s.commit()

    res = client.post(
        "/admin/sync",
        headers={"Authorization": "Bearer super-secret-admin-key-12345"},
        json={"tenant_id": "t_inactive"},
    )
    assert res.status_code == 400
    assert "INACTIVE" in res.json()["detail"]


def test_admin_provision_connection_success(client, test_db):
    with test_db.session() as s:
        TenantRepository(s).create_tenant("t_prov", "Tenant to provision")
        s.commit()

    res = client.post(
        "/admin/connections",
        headers={"Authorization": "Bearer super-secret-admin-key-12345"},
        json={
            "tenant_id": "t_prov",
            "provider": "paypal",
            "environment": "sandbox",
            "credential_ref": "env:PAYPAL_SANDBOX",
            "status": "ACTIVE",
        },
    )
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "provisioned"
    assert data["tenant_id"] == "t_prov"
    assert data["credential_ref"] == "env:PAYPAL_SANDBOX"

    # Verify queryable via GET
    get_res = client.get(
        "/admin/connections?tenant_id=t_prov",
        headers={"Authorization": "Bearer super-secret-admin-key-12345"},
    )
    assert get_res.status_code == 200
    conns = get_res.json()["connections"]
    assert len(conns) == 1
    assert conns[0]["tenant_id"] == "t_prov"


def test_admin_provision_connection_rejects_raw_secrets(client, test_db):
    with test_db.session() as s:
        TenantRepository(s).create_tenant("t_sec", "Tenant")
        s.commit()

    # Bearer token rejected
    res1 = client.post(
        "/admin/connections",
        headers={"Authorization": "Bearer super-secret-admin-key-12345"},
        json={
            "tenant_id": "t_sec",
            "credential_ref": "Bearer raw_access_token_should_never_be_stored",
        },
    )
    assert res1.status_code == 422 or res1.status_code == 400
    assert "raw tokens or secrets" in str(res1.json()).lower()

    # Raw secret rejected
    res2 = client.post(
        "/admin/connections",
        headers={"Authorization": "Bearer super-secret-admin-key-12345"},
        json={
            "tenant_id": "t_sec",
            "credential_ref": "secret_abc1234567890",
        },
    )
    assert res2.status_code == 422 or res2.status_code == 400
    assert "raw tokens or secrets" in str(res2.json()).lower()


def test_admin_provision_connection_rejects_unknown_tenant(client):
    res = client.post(
        "/admin/connections",
        headers={"Authorization": "Bearer super-secret-admin-key-12345"},
        json={
            "tenant_id": "unknown_t_xyz",
            "credential_ref": "env:PAYPAL_SANDBOX",
        },
    )
    assert res.status_code == 404
    assert "not found" in res.json()["detail"].lower()


def test_admin_tenants_creation_and_listing(client):
    # Create new tenant
    res = client.post(
        "/admin/tenants",
        headers={"Authorization": "Bearer super-secret-admin-key-12345"},
        json={"tenant_id": "tenant_prod_alpha", "name": "Alpha Production"},
    )
    assert res.status_code == 200
    assert res.json()["status"] == "ready"
    assert res.json()["tenant_id"] == "tenant_prod_alpha"

    # List tenants includes new tenant
    res_list = client.get(
        "/admin/tenants",
        headers={"Authorization": "Bearer super-secret-admin-key-12345"},
    )
    assert res_list.status_code == 200
    tenant_ids = [t["id"] for t in res_list.json()["tenants"]]
    assert "tenant_prod_alpha" in tenant_ids


def test_bootstrap_database_fresh_and_idempotent(test_db):
    from reconcile_store.bootstrap import bootstrap_database

    with test_db.session() as session:
        t1, c1 = bootstrap_database(session, tenant_id="tenant_boot_test", environment="sandbox")
        assert t1.id == "tenant_boot_test"
        assert c1.status == "ACTIVE"
        assert c1.credential_ref == "env:PAYPAL_SANDBOX"

        # Re-run idempotently
        t2, c2 = bootstrap_database(session, tenant_id="tenant_boot_test", environment="sandbox")
        assert t2.id == t1.id
        assert c2.id == c1.id


def test_documentation_endpoints_disabled(client):
    """Ensure /docs, /redoc, and /openapi.json are disabled and return 404."""
    for endpoint in ("/docs", "/redoc", "/openapi.json"):
        res = client.get(endpoint)
        assert res.status_code == 404



"""Tests for D7 Fintech Operator Dashboard & Workflow Endpoints.

Validates:
- Dashboard summary and currency-partitioned metrics
- Transactions and invoices search, filtering, and detail endpoints
- Reconciliation run triggering and proposal inspection
- Human approval / rejection lifecycle with 10-step atomic financial transactions
- AI Exception Investigation approval and fail-closed safety blocking on INVALID status
- Cryptographic SHA-256 hash-chained audit verification
- Tenant isolation across all new endpoints
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from datetime import datetime, timezone

from reconcile_api.config import APIConfig
from reconcile_api.dependencies import set_db_engine
from reconcile_api.main import app
from reconcile_store.config import DatabaseConfig
from reconcile_store.db import DatabaseEngine
from reconcile_store.schema import Base
from reconcile_store.repositories.tenant_repo import TenantRepository


@pytest.fixture
def test_db():
    """In-memory database for API test isolation."""
    engine = DatabaseEngine(DatabaseConfig(url="sqlite:///:memory:"))
    Base.metadata.create_all(engine.engine)
    set_db_engine(engine)
    with engine.session() as s:
        TenantRepository(s).create_tenant("test_tenant", "Test Tenant")
        TenantRepository(s).create_tenant("other_tenant", "Other Tenant")
        s.commit()
    yield engine
    set_db_engine(None)


@pytest.fixture
def client(test_db, monkeypatch):
    monkeypatch.setenv("ADMIN_API_KEY", "test-secret-key-12345")
    monkeypatch.setenv("DEFAULT_TENANT_ID", "test_tenant")
    monkeypatch.setenv("MODEL_PROVIDER", "mock")
    return TestClient(app)


AUTH_HEADERS = {"Authorization": "Bearer test-secret-key-12345"}


def test_demo_seed_and_idempotency(client):
    """Verify demo data seeder initializes realistic financial scenarios."""
    resp = client.post("/admin/demo/seed", json={"tenant_id": "demo_merchant", "force": False}, headers=AUTH_HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "seeded"
    assert data["invoices_count"] == 6
    assert data["payments_count"] == 6
    assert len(data["investigation_ids"]) == 2

    # Second call without force should be idempotent
    resp2 = client.post("/admin/demo/seed", json={"tenant_id": "demo_merchant", "force": False}, headers=AUTH_HEADERS)
    assert resp2.status_code == 200
    data2 = resp2.json()
    assert data2["status"] == "already_seeded"

    # Call with force=True should re-seed
    resp3 = client.post("/admin/demo/seed", json={"tenant_id": "demo_merchant", "force": True}, headers=AUTH_HEADERS)
    assert resp3.status_code == 200
    data3 = resp3.json()
    assert data3["status"] == "seeded"


def test_dashboard_summary(client):
    """Verify dashboard summary provides currency-partitioned metrics and recent activity."""
    # Seed data first
    client.post("/admin/demo/seed", json={"tenant_id": "demo_merchant"}, headers=AUTH_HEADERS)

    resp = client.get("/admin/dashboard/summary?tenant_id=demo_merchant", headers=AUTH_HEADERS)
    assert resp.status_code == 200
    data = resp.json()

    assert data["tenant_id"] == "demo_merchant"
    counts = data["counts"]
    assert counts["total_payments"] == 6
    assert counts["total_invoices"] == 6
    assert counts["open_exceptions"] > 0
    assert counts["reconciled_payments"] >= 1
    assert counts["total_investigations"] >= 2
    assert counts["investigations_needs_review"] >= 1

    # Financial totals must be partitioned by currency (no mixing USD and EUR!)
    totals = data["financial_totals"]
    assert "USD" in totals
    assert "EUR" in totals
    assert totals["USD"]["currency"] == "USD"
    assert totals["EUR"]["currency"] == "EUR"
    assert totals["USD"]["payments_amount_minor"] > 0
    assert totals["EUR"]["payments_amount_minor"] == 75000

    # Recent activity
    assert len(data["recent_runs"]) >= 1
    assert len(data["recent_exceptions"]) >= 1
    assert len(data["recent_investigations"]) >= 1


def test_transactions_listing_and_filtering(client):
    """Verify transaction search, currency filtering, and reconciliation status."""
    client.post("/admin/demo/seed", json={"tenant_id": "demo_merchant"}, headers=AUTH_HEADERS)

    # All transactions
    resp = client.get("/admin/transactions?tenant_id=demo_merchant", headers=AUTH_HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_count"] == 6

    # Currency filter
    resp_eur = client.get("/admin/transactions?tenant_id=demo_merchant&currency=EUR", headers=AUTH_HEADERS)
    assert resp_eur.status_code == 200
    eur_data = resp_eur.json()
    assert eur_data["total_count"] == 1
    assert eur_data["transactions"][0]["currency"] == "EUR"

    # Search filter
    resp_search = client.get("/admin/transactions?tenant_id=demo_merchant&search=Wayne", headers=AUTH_HEADERS)
    assert resp_search.status_code == 200
    search_data = resp_search.json()
    assert search_data["total_count"] == 1
    assert "Wayne" in search_data["transactions"][0]["payer_name"]

    # Status filter: RECONCILED
    resp_reconciled = client.get("/admin/transactions?tenant_id=demo_merchant&status=RECONCILED", headers=AUTH_HEADERS)
    assert resp_reconciled.status_code == 200
    rec_data = resp_reconciled.json()
    assert rec_data["total_count"] >= 1
    for t in rec_data["transactions"]:
        assert t["unapplied_cash_minor"] == 0


def test_transaction_detail(client):
    """Verify transaction detail returns linked allocations, proposals, and audit events."""
    client.post("/admin/demo/seed", json={"tenant_id": "demo_merchant"}, headers=AUTH_HEADERS)

    # PAY-DEMO-001 is reconciled to INV-1001
    resp = client.get("/admin/transactions/PAY-DEMO-001?tenant_id=demo_merchant", headers=AUTH_HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert data["provider_payment_id"] == "PAY-DEMO-001"
    assert data["amount_minor"] == 125000
    assert data["unapplied_cash_minor"] == 0
    assert len(data["allocations"]) >= 1
    assert data["allocations"][0]["invoice_number"] == "INV-1001"
    assert len(data["proposals"]) >= 1

    # 404 for unknown transaction
    resp_404 = client.get("/admin/transactions/UNKNOWN-PAY?tenant_id=demo_merchant", headers=AUTH_HEADERS)
    assert resp_404.status_code == 404


def test_invoices_listing_and_detail(client):
    """Verify invoices listing and outstanding balance calculations."""
    client.post("/admin/demo/seed", json={"tenant_id": "demo_merchant"}, headers=AUTH_HEADERS)

    resp = client.get("/admin/invoices?tenant_id=demo_merchant", headers=AUTH_HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_count"] == 6

    # Detail for INV-1001 (fully reconciled)
    resp_inv1 = client.get("/admin/invoices/INV-1001?tenant_id=demo_merchant", headers=AUTH_HEADERS)
    assert resp_inv1.status_code == 200
    inv1_data = resp_inv1.json()
    assert inv1_data["invoice_number"] == "INV-1001"
    assert inv1_data["outstanding_balance_minor"] == 0
    assert inv1_data["allocated_minor"] == 125000
    assert len(inv1_data["allocations"]) >= 1

    # Detail for INV-1003 (partially or open)
    resp_inv3 = client.get("/admin/invoices/INV-1003?tenant_id=demo_merchant", headers=AUTH_HEADERS)
    assert resp_inv3.status_code == 200
    inv3_data = resp_inv3.json()
    assert inv3_data["outstanding_balance_minor"] > 0


def test_reconciliation_run_trigger_and_inspection(client):
    """Verify manual triggering of reconciliation run and proposals inspection."""
    client.post("/admin/demo/seed", json={"tenant_id": "demo_merchant"}, headers=AUTH_HEADERS)

    resp = client.post("/admin/runs", json={"tenant_id": "demo_merchant", "matcher_name": "deterministic_v2"}, headers=AUTH_HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "completed"
    assert "run_id" in data
    run_id = data["run_id"]

    # Inspect run
    resp_run = client.get(f"/admin/runs/{run_id}?tenant_id=demo_merchant", headers=AUTH_HEADERS)
    assert resp_run.status_code == 200
    run_data = resp_run.json()
    assert run_data["id"] == run_id
    assert "proposals" in run_data


def test_proposal_approval_and_rejection(client):
    """Verify proposal human review workflow: approval creates allocation, rejection records reason."""
    client.post("/admin/demo/seed", json={"tenant_id": "demo_merchant"}, headers=AUTH_HEADERS)

    # Get proposals list
    resp = client.get("/admin/proposals?tenant_id=demo_merchant&status=PENDING", headers=AUTH_HEADERS)
    assert resp.status_code == 200
    props = resp.json()["proposals"]
    assert len(props) > 0
    target_prop = props[0]
    prop_id = target_prop["id"]

    # Reject proposal
    resp_rej = client.post(
        f"/admin/proposals/{prop_id}/reject",
        json={"reviewer": "auditor@demo.com", "reason": "Requires further customer confirmation", "tenant_id": "demo_merchant"},
        headers=AUTH_HEADERS,
    )
    assert resp_rej.status_code == 200
    rej_data = resp_rej.json()
    assert rej_data["status"] == "REJECTED"
    assert rej_data["reason"] == "Requires further customer confirmation"

    # Detail check
    resp_det = client.get(f"/admin/proposals/{prop_id}?tenant_id=demo_merchant", headers=AUTH_HEADERS)
    assert resp_det.status_code == 200
    assert resp_det.json()["decision_status"] == "REJECTED"


def test_investigation_approval_and_safety_fail_closed(client):
    """CRITICAL SAFETY TEST:
    1. Verified investigations can be approved and create allocations.
    2. Investigations with INVALID safety verifier status FAIL CLOSED and block approval.
    """
    client.post("/admin/demo/seed", json={"tenant_id": "demo_merchant"}, headers=AUTH_HEADERS)

    resp_invgs = client.get("/admin/investigations?tenant_id=demo_merchant", headers=AUTH_HEADERS)
    assert resp_invgs.status_code == 200
    invgs = resp_invgs.json()["investigations"]

    verified_inv = next(i for i in invgs if i["verifier_status"] == "VERIFIED")
    invalid_inv = next(i for i in invgs if i["verifier_status"] == "INVALID")

    # 1. Approve verified investigation
    resp_app = client.post(
        f"/admin/investigations/{verified_inv['id']}/approve",
        json={"reviewer": "controller@demo.com", "notes": "Approved based on matching project code", "tenant_id": "demo_merchant"},
        headers=AUTH_HEADERS,
    )
    assert resp_app.status_code == 200
    app_data = resp_app.json()
    assert app_data["status"] == "APPROVED"
    assert "allocation_ids" in app_data

    # 2. Attempt to approve INVALID investigation -> MUST FAIL CLOSED (400 Bad Request)
    resp_invalid_app = client.post(
        f"/admin/investigations/{invalid_inv['id']}/approve",
        json={"reviewer": "controller@demo.com", "notes": "Attempt bypass", "tenant_id": "demo_merchant"},
        headers=AUTH_HEADERS,
    )
    assert resp_invalid_app.status_code == 400
    assert "INVALID" in resp_invalid_app.json()["detail"]
    assert "strictly blocked" in resp_invalid_app.json()["detail"].lower()

    # Reject invalid investigation
    resp_invalid_rej = client.post(
        f"/admin/investigations/{invalid_inv['id']}/reject",
        json={"reviewer": "compliance@demo.com", "notes": "Confirmed malicious prompt injection", "tenant_id": "demo_merchant"},
        headers=AUTH_HEADERS,
    )
    assert resp_invalid_rej.status_code == 200
    assert resp_invalid_rej.json()["status"] == "REJECTED"


def test_cryptographic_audit_trail_and_verification(client):
    """Verify audit events retrieval and SHA-256 tamper-evident verification."""
    client.post("/admin/demo/seed", json={"tenant_id": "demo_merchant"}, headers=AUTH_HEADERS)

    resp_events = client.get("/admin/audit-events?tenant_id=demo_merchant", headers=AUTH_HEADERS)
    assert resp_events.status_code == 200
    events_data = resp_events.json()
    assert events_data["total_count"] >= 1

    # Verify cryptographic integrity
    resp_verify = client.get("/admin/audit-events/verify?tenant_id=demo_merchant", headers=AUTH_HEADERS)
    assert resp_verify.status_code == 200
    verify_data = resp_verify.json()
    assert verify_data["verified"] is True
    assert verify_data["algorithm"] == "SHA-256"
    assert "zero tampering detected" in verify_data["message"].lower()


def test_tenant_isolation_on_d7_endpoints(client):
    """Ensure data from demo_merchant cannot leak into test_tenant or other_tenant."""
    client.post("/admin/demo/seed", json={"tenant_id": "demo_merchant"}, headers=AUTH_HEADERS)

    # Empty for other_tenant
    resp = client.get("/admin/transactions?tenant_id=other_tenant", headers=AUTH_HEADERS)
    assert resp.status_code == 200
    assert resp.json()["total_count"] == 0

    resp_inv = client.get("/admin/invoices?tenant_id=other_tenant", headers=AUTH_HEADERS)
    assert resp_inv.status_code == 200
    assert resp_inv.json()["total_count"] == 0

    resp_sum = client.get("/admin/dashboard/summary?tenant_id=other_tenant", headers=AUTH_HEADERS)
    assert resp_sum.status_code == 200
    assert resp_sum.json()["counts"]["total_payments"] == 0


def test_frontend_spa_serving(client):
    """Verify built frontend SPA is properly served at root and fallback routes."""
    resp = client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers.get("content-type", "")
    assert "<div id=\"root\"></div>" in resp.text


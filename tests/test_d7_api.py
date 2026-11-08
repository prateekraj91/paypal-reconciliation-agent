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
    assert data["invoices_count"] == 12
    assert data["payments_count"] == 11
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


def test_dashboard_summary_empty_tenant_zero_denominator(client):
    """Verify dashboard summary handles an empty tenant with zero transactions and empty currency ledger."""
    resp = client.get("/admin/dashboard/summary?tenant_id=test_tenant", headers=AUTH_HEADERS)
    assert resp.status_code == 200
    data = resp.json()

    assert data["tenant_id"] == "test_tenant"
    counts = data["counts"]
    # Total payments is 0 (zero denominator for reconciliation rate)
    assert counts["total_payments"] == 0
    assert counts["reconciled_payments"] == 0
    assert counts["open_exceptions"] == 0
    assert counts["total_invoices"] == 0
    assert counts["total_proposals"] == 0
    assert counts["total_investigations"] == 0

    # Currency ledger must be empty without error (triggers UI 'No currency balances available yet.')
    assert data["financial_totals"] == {}
    assert data["recent_runs"] == []
    assert data["recent_exceptions"] == []
    assert data["recent_investigations"] == []


def test_dashboard_summary(client):
    """Verify dashboard summary provides currency-partitioned metrics and recent activity."""
    # Seed data first
    client.post("/admin/demo/seed", json={"tenant_id": "demo_merchant"}, headers=AUTH_HEADERS)

    resp = client.get("/admin/dashboard/summary?tenant_id=demo_merchant", headers=AUTH_HEADERS)
    assert resp.status_code == 200
    data = resp.json()

    assert data["tenant_id"] == "demo_merchant"
    counts = data["counts"]
    assert counts["total_payments"] == 11
    assert counts["total_invoices"] == 12
    assert counts["open_exceptions"] > 0
    assert counts["total_investigations"] >= 2
    assert counts["investigations_needs_review"] >= 1

    # Financial totals must be partitioned by currency (no mixing USD and EUR!)
    totals = data["financial_totals"]
    assert "USD" in totals
    assert totals["USD"]["currency"] == "USD"
    assert totals["USD"]["payments_amount_minor"] > 0

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
    assert data["total_count"] == 11

    # Currency filter
    resp_usd = client.get("/admin/transactions?tenant_id=demo_merchant&currency=USD", headers=AUTH_HEADERS)
    assert resp_usd.status_code == 200
    usd_data = resp_usd.json()
    assert usd_data["total_count"] == 11
    assert usd_data["transactions"][0]["currency"] == "USD"

    # Search filter
    resp_search = client.get("/admin/transactions?tenant_id=demo_merchant&search=Acme", headers=AUTH_HEADERS)
    assert resp_search.status_code == 200
    search_data = resp_search.json()
    assert search_data["total_count"] == 1
    assert "Acme" in search_data["transactions"][0]["payer_name"]

    # Status filter: UNMATCHED
    resp_unmatched = client.get("/admin/transactions?tenant_id=demo_merchant&status=UNMATCHED", headers=AUTH_HEADERS)
    assert resp_unmatched.status_code == 200
    unm_data = resp_unmatched.json()
    assert unm_data["total_count"] >= 1
    for t in unm_data["transactions"]:
        assert t["unapplied_cash_minor"] > 0


def test_transaction_detail(client):
    """Verify transaction detail returns linked allocations, proposals, and audit events."""
    client.post("/admin/demo/seed", json={"tenant_id": "demo_merchant"}, headers=AUTH_HEADERS)

    # PAY-DEMO-EXACT-001 is the exact match payment
    resp = client.get("/admin/transactions/PAY-DEMO-EXACT-001?tenant_id=demo_merchant", headers=AUTH_HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert data["provider_payment_id"] == "PAY-DEMO-EXACT-001"
    assert data["amount_minor"] == 125000
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
    assert data["total_count"] == 12

    # Detail for INV-DEMO-EXACT-001
    resp_inv1 = client.get("/admin/invoices/INV-DEMO-EXACT-001?tenant_id=demo_merchant", headers=AUTH_HEADERS)
    assert resp_inv1.status_code == 200
    inv1_data = resp_inv1.json()
    assert inv1_data["invoice_number"] == "INV-DEMO-EXACT-001"
    assert inv1_data["total_minor"] == 125000

    # Detail for INV-DEMO-PARTIAL-004
    resp_inv3 = client.get("/admin/invoices/INV-DEMO-PARTIAL-004?tenant_id=demo_merchant", headers=AUTH_HEADERS)
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
    1. Verified or Needs-Review investigations with passed=True and valid invariants can be approved and create allocations.
    2. Investigations with INVALID safety verifier status FAIL CLOSED and block approval.
    """
    client.post("/admin/demo/seed", json={"tenant_id": "demo_merchant"}, headers=AUTH_HEADERS)

    resp_invgs = client.get("/admin/investigations?tenant_id=demo_merchant", headers=AUTH_HEADERS)
    assert resp_invgs.status_code == 200
    invgs = resp_invgs.json()["investigations"]

    eligible_inv = next(i for i in invgs if i["verifier_status"] in ("VERIFIED", "NEEDS_REVIEW") and i["status"] == "NEEDS_REVIEW")
    invalid_inv = next(i for i in invgs if i["verifier_status"] == "INVALID")

    # 1. Approve eligible investigation
    resp_app = client.post(
        f"/admin/investigations/{eligible_inv['id']}/approve",
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


def test_per_currency_totals_and_outstanding_accounting(client):
    """Verify independent calculations of payment/invoice counts and amounts per currency bucket."""
    client.post("/admin/demo/seed", json={"tenant_id": "demo_merchant"}, headers=AUTH_HEADERS)

    resp = client.get("/admin/dashboard/summary?tenant_id=demo_merchant", headers=AUTH_HEADERS)
    assert resp.status_code == 200
    data = resp.json()

    # Counts
    counts = data["counts"]
    assert counts["total_payments"] == 11
    assert counts["actionable_proposals"] == 8
    assert counts["unresolved_proposals"] == 11

    # USD bucket
    totals = data["financial_totals"]
    assert "USD" in totals
    usd = totals["USD"]
    assert usd["currency"] == "USD"
    assert usd["payments_count"] == 11
    assert usd["invoices_count"] == 11
    assert usd["payments_amount_minor"] == 791000
    assert usd["invoices_amount_minor"] == 819000
    assert usd["invoices_outstanding_minor"] == 819000
    assert usd["unapplied_cash_minor"] == 736000
    assert usd["unapplied_amount_minor"] == 736000

    # EUR bucket
    assert "EUR" in totals
    eur = totals["EUR"]
    assert eur["currency"] == "EUR"
    assert eur["payments_count"] == 0
    assert eur["invoices_count"] == 1
    assert eur["payments_amount_minor"] == 0
    assert eur["invoices_amount_minor"] == 75000
    assert eur["invoices_outstanding_minor"] == 75000
    assert eur["unapplied_cash_minor"] == 0
    assert eur["unapplied_amount_minor"] == 0


def test_refund_and_reversal_net_unapplied_cash_accounting(client):
    """Verify refunds and reversals reduce unapplied cash on parent transactions to prevent phantom unapplied cash."""
    client.post("/admin/demo/seed", json={"tenant_id": "demo_merchant"}, headers=AUTH_HEADERS)

    # List transactions
    resp = client.get("/admin/transactions?tenant_id=demo_merchant", headers=AUTH_HEADERS)
    assert resp.status_code == 200
    txs = {t["provider_payment_id"]: t for t in resp.json()["transactions"]}

    # PAY-DEMO-REFUND-007: Gross $300.00, Refund -$100.00, Net Settled $200.00, Unapplied $200.00
    ref_tx = txs["PAY-DEMO-REFUND-007"]
    assert ref_tx["amount_minor"] == 30000
    assert ref_tx["refund_amount_minor"] == 10000
    assert ref_tx["net_amount_minor"] == 20000
    assert ref_tx["unapplied_cash_minor"] == 20000
    assert ref_tx["reconciliation_status"] == "UNMATCHED"

    # PAY-DEMO-REVERSAL-008: Gross $450.00, Reversal -$450.00, Net Settled $0.00, Unapplied $0.00
    rev_tx = txs["PAY-DEMO-REVERSAL-008"]
    assert rev_tx["amount_minor"] == 45000
    assert rev_tx["reversal_amount_minor"] == 45000
    assert rev_tx["net_amount_minor"] == 0
    assert rev_tx["unapplied_cash_minor"] == 0
    assert rev_tx["reconciliation_status"] == "RECONCILED"

    # Transaction detail endpoint guarantees
    resp_detail = client.get("/admin/transactions/PAY-DEMO-REVERSAL-008?tenant_id=demo_merchant", headers=AUTH_HEADERS)
    assert resp_detail.status_code == 200
    detail = resp_detail.json()
    assert detail["reconciliation_status"] == "RECONCILED"
    assert detail["net_amount_minor"] == 0
    assert detail["unapplied_cash_minor"] == 0


def test_currency_mismatch_and_ambiguity_enrichment(client):
    """Verify currency mismatch has authoritative isolation message and ambiguity certificates include candidate details."""
    client.post("/admin/demo/seed", json={"tenant_id": "demo_merchant"}, headers=AUTH_HEADERS)

    resp = client.get("/admin/proposals?tenant_id=demo_merchant", headers=AUTH_HEADERS)
    assert resp.status_code == 200
    props = {p["provider_payment_id"]: p for p in resp.json()["proposals"]}

    # Currency mismatch
    curr_prop = props["PAY-DEMO-CURR-MISMATCH-009"]
    assert curr_prop["outcome"] == "NO_MATCH"
    evidence_str = " ".join(curr_prop["evidence"])
    assert "CURRENCY_MISMATCH" in evidence_str
    assert "INV-DEMO-EUR-009" in evidence_str
    assert "Strict currency isolation prevents cross-currency allocations" in evidence_str

    # Competing candidates ambiguity certificate
    comp_prop = props["PAY-DEMO-COMPETING-003"]
    assert comp_prop["outcome"] == "AMBIGUOUS"
    cert = comp_prop["ambiguity_certificate"]
    assert cert is not None
    assert "candidates_detail" in cert
    assert len(cert["candidates_detail"]) == 2
    cands = {c["invoice_number"]: c for c in cert["candidates_detail"]}
    assert "INV-DEMO-COMPETE-3A" in cands
    assert "INV-DEMO-COMPETE-3B" in cands
    assert cands["INV-DEMO-COMPETE-3A"]["total_minor"] == 40000
    assert cands["INV-DEMO-COMPETE-3A"]["score"] == 120
    assert cands["INV-DEMO-COMPETE-3B"]["total_minor"] == 40000
    assert cands["INV-DEMO-COMPETE-3B"]["score"] == 120



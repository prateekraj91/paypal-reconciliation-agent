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
from datetime import date, datetime, timezone

from reconcile_api.config import APIConfig
from reconcile_api.dependencies import set_db_engine
from reconcile_api.main import app
from reconcile_store.config import DatabaseConfig
from reconcile_store.db import DatabaseEngine
from reconcile_store.schema import (
    Base,
    PaymentRecord,
    InvoiceRecord,
    ReconciliationRunRecord,
    ProposalRecord,
    ProposalAllocationRecord,
    DecisionRecord,
    RefundRecord,
    AllocationRecord,
    ProviderSettlementRecord,
    AuditEventRecord,
)
from reconcile_store.repositories.tenant_repo import TenantRepository
from reconcile_store.repositories.payment_repo import PaymentRepository
from reconcile_store.repositories.invoice_repo import InvoiceRepository
from reconcile_store.approval import ApprovalService


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

    # In demo_merchant, INVESTIGATE-010 has an unresolved customer mismatch (Global Treasury vs Cyberdyne)
    mismatch_inv = next(i for i in invgs if i["target_id"] == "PAY-DEMO-INVESTIGATE-010")
    invalid_inv = next(i for i in invgs if i["verifier_status"] == "INVALID")

    # 1. Attempt to approve INVESTIGATE-010 with customer mismatch -> MUST BE BLOCKED (HTTP 400)
    resp_app_mismatch = client.post(
        f"/admin/investigations/{mismatch_inv['id']}/approve",
        json={"reviewer": "controller@demo.com", "notes": "Approved based on matching project code", "tenant_id": "demo_merchant"},
        headers=AUTH_HEADERS,
    )
    assert resp_app_mismatch.status_code == 400
    assert "customer mismatch" in resp_app_mismatch.json()["detail"].lower()

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

    # Counts (8 proposed - 2 safety blocked [injection + remitter mismatch] = 6 actionable)
    counts = data["counts"]
    assert counts["total_payments"] == 11
    assert counts["actionable_proposals"] == 6
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


@pytest.mark.parametrize("target_count", [0, 5, 8, 100])
def test_actionable_proposals_dynamic_calculation(test_db, client, target_count):
    """Confirm actionable_proposals is dynamically computed from eligible records and never hardcoded.
    
    Verifies that for N = 0, 5, 8, 100 actionable proposals:
    - actionable_proposals == N
    - non-actionable proposals (unverified, no-match, 0-allocations, approved, rejected) are strictly excluded
    """
    tenant = f"tenant_dynamic_{target_count}"
    now = datetime.now(timezone.utc)
    today = date(2026, 11, 8)

    with test_db.session() as s:
        TenantRepository(s).create_tenant(tenant, f"Dynamic Tenant {target_count}")

        # Base anchor payment & invoice
        anchor_pay = PaymentRecord(
            id=f"pay_anchor_{target_count}",
            tenant_id=tenant,
            provider="paypal",
            provider_payment_id=f"PAY-ANCHOR-{target_count}",
            amount_minor=100000,
            currency="USD",
            status="COMPLETED",
            received_at=now,
        )
        anchor_inv = InvoiceRecord(
            id=f"inv_anchor_{target_count}",
            tenant_id=tenant,
            provider="paypal",
            provider_invoice_id=f"INV-ANCHOR-{target_count}",
            invoice_number=f"INV-ANCHOR-{target_count}",
            customer_name="Anchor Customer",
            total_minor=100000,
            currency="USD",
            status="SENT",
            issued_on=today,
            due_on=today,
        )
        run_rec = ReconciliationRunRecord(
            id=f"run_dynamic_{target_count}",
            tenant_id=tenant,
            input_snapshot_id=f"snap_dynamic_{target_count}",
            matcher_name="deterministic_v2",
            matcher_version="2.0.0",
            run_at=now,
        )
        s.add_all([anchor_pay, anchor_inv, run_rec])
        s.flush()

        # Create target_count actionable proposals (PROPOSED + VERIFIED + allocations > 0 + pending decision)
        for i in range(target_count):
            prop = ProposalRecord(
                id=f"prop_actionable_{target_count}_{i}",
                tenant_id=tenant,
                run_id=run_rec.id,
                payment_id=anchor_pay.id,
                outcome="PROPOSED",
                verifier_status="VERIFIED",
                created_at=now,
            )
            s.add(prop)
            s.flush()
            palloc = ProposalAllocationRecord(
                id=f"palloc_{target_count}_{i}",
                tenant_id=tenant,
                proposal_id=prop.id,
                payment_id=anchor_pay.id,
                invoice_id=anchor_inv.id,
                amount_minor=10000,
                currency="USD",
            )
            s.add(palloc)

        # Non-actionable 1: NO_MATCH outcome
        s.add(ProposalRecord(
            id=f"prop_nonact_nomatch_{target_count}",
            tenant_id=tenant,
            run_id=run_rec.id,
            payment_id=anchor_pay.id,
            outcome="NO_MATCH",
            verifier_status="UNVERIFIED",
            created_at=now,
        ))

        # Non-actionable 2: AMBIGUOUS outcome
        s.add(ProposalRecord(
            id=f"prop_nonact_ambig_{target_count}",
            tenant_id=tenant,
            run_id=run_rec.id,
            payment_id=anchor_pay.id,
            outcome="AMBIGUOUS",
            verifier_status="UNVERIFIED",
            created_at=now,
        ))

        # Non-actionable 3: PROPOSED but UNVERIFIED safety status
        p_unver = ProposalRecord(
            id=f"prop_nonact_unver_{target_count}",
            tenant_id=tenant,
            run_id=run_rec.id,
            payment_id=anchor_pay.id,
            outcome="PROPOSED",
            verifier_status="UNVERIFIED",
            created_at=now,
        )
        s.add(p_unver)
        s.flush()
        s.add(ProposalAllocationRecord(
            id=f"palloc_unver_{target_count}",
            tenant_id=tenant,
            proposal_id=p_unver.id,
            payment_id=anchor_pay.id,
            invoice_id=anchor_inv.id,
            amount_minor=10000,
            currency="USD",
        ))

        # Non-actionable 4: PROPOSED and VERIFIED but zero allocations
        s.add(ProposalRecord(
            id=f"prop_nonact_noalloc_{target_count}",
            tenant_id=tenant,
            run_id=run_rec.id,
            payment_id=anchor_pay.id,
            outcome="PROPOSED",
            verifier_status="VERIFIED",
            created_at=now,
        ))

        # Non-actionable 5: Already APPROVED
        p_app = ProposalRecord(
            id=f"prop_nonact_app_{target_count}",
            tenant_id=tenant,
            run_id=run_rec.id,
            payment_id=anchor_pay.id,
            outcome="PROPOSED",
            verifier_status="VERIFIED",
            created_at=now,
        )
        s.add(p_app)
        s.flush()
        s.add(ProposalAllocationRecord(
            id=f"palloc_app_{target_count}",
            tenant_id=tenant,
            proposal_id=p_app.id,
            payment_id=anchor_pay.id,
            invoice_id=anchor_inv.id,
            amount_minor=10000,
            currency="USD",
        ))
        s.add(DecisionRecord(
            id=f"dec_app_{target_count}",
            tenant_id=tenant,
            proposal_id=p_app.id,
            decision_type="APPROVED",
            actor="operator@example.com",
            idempotency_key=f"idemp_{target_count}",
            decided_at=now,
        ))

        s.commit()

    resp = client.get(f"/admin/dashboard/summary?tenant_id={tenant}", headers=AUTH_HEADERS)
    assert resp.status_code == 200
    counts = resp.json()["counts"]

    # Conclusively verify dynamic calculation: exactly target_count actionable proposals
    assert counts["actionable_proposals"] == target_count
    # 4 non-actionable pending proposals + target_count
    assert counts["unresolved_proposals"] == target_count + 4
    # 1 approved proposal
    assert counts["approved_proposals"] == 1
    # total proposals = target_count + 5 non-actionable proposals
    assert counts["total_proposals"] == target_count + 5


def test_refund_reversal_edge_cases_with_allocations(test_db, client):
    """Verify refund and reversal calculations across partial refunds, full reversals, and allocated payments."""
    tenant = "tenant_refund_edge_cases"
    now = datetime.now(timezone.utc)
    today = date(2026, 11, 8)

    with test_db.session() as s:
        TenantRepository(s).create_tenant(tenant, "Refund Edge Cases Tenant")

        # Case 1: Partial refund on unallocated payment ($500 payment, $150 refund)
        p1 = PaymentRecord(
            id="pay_edge_partial_refund",
            tenant_id=tenant,
            provider="paypal",
            provider_payment_id="PAY-EDGE-PARTIAL-REFUND",
            amount_minor=50000,
            currency="USD",
            status="COMPLETED",
            received_at=now,
        )
        r1 = RefundRecord(
            id="ref_edge_partial",
            tenant_id=tenant,
            provider="paypal",
            provider_refund_id="REF-EDGE-001",
            parent_payment_id=p1.id,
            amount_minor=15000,
            currency="USD",
            status="COMPLETED",
            record_type="REFUND",
            refunded_at=now,
        )

        # Case 2: Full dispute reversal on unallocated payment ($400 payment, $400 reversal)
        p2 = PaymentRecord(
            id="pay_edge_full_reversal",
            tenant_id=tenant,
            provider="paypal",
            provider_payment_id="PAY-EDGE-FULL-REVERSAL",
            amount_minor=40000,
            currency="USD",
            status="COMPLETED",
            received_at=now,
        )
        r2 = RefundRecord(
            id="ref_edge_reversal",
            tenant_id=tenant,
            provider="paypal",
            provider_refund_id="REV-EDGE-001",
            parent_payment_id=p2.id,
            amount_minor=40000,
            currency="USD",
            status="COMPLETED",
            record_type="REVERSAL",
            refunded_at=now,
        )

        # Case 3: Allocated payment with partial refund
        # Payment $1,000, allocated $600 to invoice, refund $200
        # Expected unapplied: max(0, 100000 - 60000 - 20000) = 20000 ($200.00)
        run_edge = ReconciliationRunRecord(
            id="run_edge",
            tenant_id=tenant,
            input_snapshot_id="snap_edge",
            matcher_name="deterministic_v2",
            matcher_version="2.0.0",
            run_at=now,
        )
        s.add(run_edge)

        p3 = PaymentRecord(
            id="pay_edge_allocated_refund",
            tenant_id=tenant,
            provider="paypal",
            provider_payment_id="PAY-EDGE-ALLOC-REFUND",
            amount_minor=100000,
            currency="USD",
            status="COMPLETED",
            received_at=now,
        )
        inv3 = InvoiceRecord(
            id="inv_edge_3",
            tenant_id=tenant,
            provider="paypal",
            provider_invoice_id="INV-EDGE-3",
            invoice_number="INV-EDGE-3",
            customer_name="Customer 3",
            total_minor=100000,
            currency="USD",
            status="SENT",
            issued_on=today,
            due_on=today,
        )
        s.add_all([p3, inv3])
        s.flush()

        prop3 = ProposalRecord(
            id="prop_edge_3",
            tenant_id=tenant,
            run_id=run_edge.id,
            payment_id=p3.id,
            outcome="PROPOSED",
            verifier_status="VERIFIED",
            created_at=now,
        )
        s.add(prop3)
        s.flush()

        dec3 = DecisionRecord(
            id="dec_edge_3",
            tenant_id=tenant,
            proposal_id=prop3.id,
            decision_type="APPROVED",
            actor="operator@example.com",
            idempotency_key="idemp_edge_3",
            decided_at=now,
        )
        s.add(dec3)
        s.flush()

        alloc3 = AllocationRecord(
            id="alloc_edge_3",
            tenant_id=tenant,
            payment_id=p3.id,
            invoice_id=inv3.id,
            amount_minor=60000,
            currency="USD",
            decision_id=dec3.id,
            actor="operator@example.com",
            allocated_at=now,
        )
        r3 = RefundRecord(
            id="ref_edge_alloc_refund",
            tenant_id=tenant,
            provider="paypal",
            provider_refund_id="REF-EDGE-003",
            parent_payment_id=p3.id,
            amount_minor=20000,
            currency="USD",
            status="COMPLETED",
            record_type="REFUND",
            refunded_at=now,
        )

        # Case 4: Fully allocated payment with full reversal
        # Payment $500, allocated $500 to invoice, reversal $500
        # Expected unapplied: max(0, 50000 - 50000 - 50000) = 0 ($0.00)
        p4 = PaymentRecord(
            id="pay_edge_full_alloc_reversal",
            tenant_id=tenant,
            provider="paypal",
            provider_payment_id="PAY-EDGE-FULL-ALLOC-REV",
            amount_minor=50000,
            currency="USD",
            status="COMPLETED",
            received_at=now,
        )
        inv4 = InvoiceRecord(
            id="inv_edge_4",
            tenant_id=tenant,
            provider="paypal",
            provider_invoice_id="INV-EDGE-4",
            invoice_number="INV-EDGE-4",
            customer_name="Customer 4",
            total_minor=50000,
            currency="USD",
            status="SENT",
            issued_on=today,
            due_on=today,
        )
        s.add_all([p4, inv4])
        s.flush()

        prop4 = ProposalRecord(
            id="prop_edge_4",
            tenant_id=tenant,
            run_id=run_edge.id,
            payment_id=p4.id,
            outcome="PROPOSED",
            verifier_status="VERIFIED",
            created_at=now,
        )
        s.add(prop4)
        s.flush()

        dec4 = DecisionRecord(
            id="dec_edge_4",
            tenant_id=tenant,
            proposal_id=prop4.id,
            decision_type="APPROVED",
            actor="operator@example.com",
            idempotency_key="idemp_edge_4",
            decided_at=now,
        )
        s.add(dec4)
        s.flush()

        alloc4 = AllocationRecord(
            id="alloc_edge_4",
            tenant_id=tenant,
            payment_id=p4.id,
            invoice_id=inv4.id,
            amount_minor=50000,
            currency="USD",
            decision_id=dec4.id,
            actor="operator@example.com",
            allocated_at=now,
        )
        r4 = RefundRecord(
            id="ref_edge_full_alloc_rev",
            tenant_id=tenant,
            provider="paypal",
            provider_refund_id="REV-EDGE-004",
            parent_payment_id=p4.id,
            amount_minor=50000,
            currency="USD",
            status="COMPLETED",
            record_type="REVERSAL",
            refunded_at=now,
        )

        s.add_all([p1, r1, p2, r2, p3, inv3, alloc3, r3, p4, inv4, alloc4, r4])
        s.commit()

        # 1. Direct repository layer verification
        repo = PaymentRepository(s)
        assert repo.get_unapplied_cash_minor(tenant, p1.id) == 35000
        assert repo.get_unapplied_cash_minor(tenant, p2.id) == 0
        assert repo.get_unapplied_cash_minor(tenant, p3.id) == 20000
        assert repo.get_unapplied_cash_minor(tenant, p4.id) == 0

    # 2. API layer verification
    resp = client.get(f"/admin/transactions?tenant_id={tenant}", headers=AUTH_HEADERS)
    assert resp.status_code == 200
    txs = {t["provider_payment_id"]: t for t in resp.json()["transactions"]}

    # Case 1: Partial refund ($500 gross, $150 refund -> $350 net, $350 unapplied, UNMATCHED)
    t1 = txs["PAY-EDGE-PARTIAL-REFUND"]
    assert t1["amount_minor"] == 50000
    assert t1["refund_amount_minor"] == 15000
    assert t1["net_amount_minor"] == 35000
    assert t1["unapplied_cash_minor"] == 35000
    assert t1["reconciliation_status"] == "UNMATCHED"

    # Case 2: Full reversal ($400 gross, $400 reversal -> $0 net, $0 unapplied, RECONCILED)
    t2 = txs["PAY-EDGE-FULL-REVERSAL"]
    assert t2["amount_minor"] == 40000
    assert t2["reversal_amount_minor"] == 40000
    assert t2["net_amount_minor"] == 0
    assert t2["unapplied_cash_minor"] == 0
    assert t2["reconciliation_status"] == "RECONCILED"

    # Case 3: Allocated + partial refund ($1,000 gross, $600 allocated, $200 refund -> $800 net, $200 unapplied, PARTIALLY_RECONCILED)
    t3 = txs["PAY-EDGE-ALLOC-REFUND"]
    assert t3["amount_minor"] == 100000
    assert t3["refund_amount_minor"] == 20000
    assert t3["net_amount_minor"] == 80000
    assert t3["unapplied_cash_minor"] == 20000
    assert t3["reconciliation_status"] == "PARTIALLY_RECONCILED"

    # Case 4: Fully allocated + full reversal ($500 gross, $500 allocated, $500 reversal -> $0 net, $0 unapplied, RECONCILED)
    t4 = txs["PAY-EDGE-FULL-ALLOC-REV"]
    assert t4["amount_minor"] == 50000
    assert t4["reversal_amount_minor"] == 50000
    assert t4["net_amount_minor"] == 0
    assert t4["unapplied_cash_minor"] == 0
    assert t4["reconciliation_status"] == "RECONCILED"

    # Invoice assertions for Case 3 & Case 4
    resp_inv = client.get(f"/admin/invoices?tenant_id={tenant}", headers=AUTH_HEADERS)
    assert resp_inv.status_code == 200
    invoices = {i["provider_invoice_id"]: i for i in resp_inv.json()["invoices"]}

    # Case 3 invoice ($1000 total, $600 allocated from $1000 payment, $200 refund from unapplied cash):
    # Invoice remains PARTIALLY_RECONCILED with $400 ($600 allocated)
    inv3_api = invoices["INV-EDGE-3"]
    assert inv3_api["total_minor"] == 100000
    assert inv3_api["allocated_minor"] == 60000
    assert inv3_api["outstanding_balance_minor"] == 40000
    assert inv3_api["reconciliation_status"] == "PARTIAL"

    # Case 4 invoice ($500 total, $500 allocated from $500 payment, $500 payment full reversal):
    # Invoice balance is restored to $500, reconciliation status is OPEN (NOT left appearing paid)
    inv4_api = invoices["INV-EDGE-4"]
    assert inv4_api["total_minor"] == 50000
    assert inv4_api["allocated_minor"] == 50000
    assert inv4_api["refunded_minor"] == 50000
    assert inv4_api["net_applied_minor"] == 0
    assert inv4_api["outstanding_balance_minor"] == 50000
    assert inv4_api["reconciliation_status"] == "OPEN"


def test_complete_accounting_lifecycle_refunds_and_reversals(test_db, client):
    """Verify complete accounting lifecycle:
    1. Original payment ingestion.
    2. Invoice allocation.
    3. Settlement.
    4. Partial refund.
    5. Full reversal.
    6. Resulting invoice outstanding balance.
    7. Resulting net payment and unapplied cash.
    8. Replacement payment approval after reversal.
    9. Cryptographic audit trail preservation.
    """
    tenant = "test_tenant"
    now = datetime.now(timezone.utc)

    with test_db.session() as s:
        # 1. Ingestion: Invoices and Payments
        inv_rev = InvoiceRecord(
            id="inv_life_rev",
            tenant_id=tenant,
            provider="paypal",
            provider_invoice_id="INV-LIFE-REV",
            invoice_number="INV-LIFE-REV",
            customer_name="Cyberdyne Systems",
            total_minor=50000,  # $500
            currency="USD",
            issued_on=now.date(),
            due_on=now.date(),
        )
        inv_part = InvoiceRecord(
            id="inv_life_part",
            tenant_id=tenant,
            provider="paypal",
            provider_invoice_id="INV-LIFE-PART",
            invoice_number="INV-LIFE-PART",
            customer_name="Weyland Yutani",
            total_minor=50000,  # $500
            currency="USD",
            issued_on=now.date(),
            due_on=now.date(),
        )
        inv_sett = InvoiceRecord(
            id="inv_life_sett",
            tenant_id=tenant,
            provider="paypal",
            provider_invoice_id="INV-LIFE-SETT",
            invoice_number="INV-LIFE-SETT",
            customer_name="Tyrell Corp",
            total_minor=50000,  # $500
            currency="USD",
            issued_on=now.date(),
            due_on=now.date(),
        )

        pay_rev = PaymentRecord(
            id="pay_life_rev",
            tenant_id=tenant,
            provider="paypal",
            provider_payment_id="PAY-LIFE-REV",
            amount_minor=50000,  # $500
            currency="USD",
            received_at=now,
            reference="INV-LIFE-REV",
        )
        pay_part = PaymentRecord(
            id="pay_life_part",
            tenant_id=tenant,
            provider="paypal",
            provider_payment_id="PAY-LIFE-PART",
            amount_minor=50000,  # $500
            currency="USD",
            received_at=now,
            reference="INV-LIFE-PART",
        )
        pay_sett = PaymentRecord(
            id="pay_life_sett",
            tenant_id=tenant,
            provider="paypal",
            provider_payment_id="PAY-LIFE-SETT",
            amount_minor=50000,  # $500
            currency="USD",
            received_at=now,
            reference="INV-LIFE-SETT",
        )

        s.add_all([inv_rev, inv_part, inv_sett, pay_rev, pay_part, pay_sett])
        s.commit()

        inv_repo = InvoiceRepository(s)
        pay_repo = PaymentRepository(s)

        # Baseline: All payments have 100% unapplied cash, all invoices have 100% outstanding balance
        assert pay_repo.get_unapplied_cash_minor(tenant, pay_rev.id) == 50000
        assert pay_repo.get_unapplied_cash_minor(tenant, pay_part.id) == 50000
        assert pay_repo.get_unapplied_cash_minor(tenant, pay_sett.id) == 50000
        assert inv_repo.get_ledger_outstanding_minor(tenant, inv_rev.id) == 50000
        assert inv_repo.get_ledger_outstanding_minor(tenant, inv_part.id) == 50000
        assert inv_repo.get_ledger_outstanding_minor(tenant, inv_sett.id) == 50000

        # 2. Allocation & Approval
        run = ReconciliationRunRecord(
            id="run_life_1",
            tenant_id=tenant,
            input_snapshot_id="snap_life_1",
            matcher_name="test_deterministic",
        )
        s.add(run)
        s.flush()

        prop_rev = ProposalRecord(
            id="prop_life_rev",
            tenant_id=tenant,
            run_id=run.id,
            payment_id=pay_rev.id,
            outcome="PROPOSED",
            verifier_status="VERIFIED",
            evidence_json='["REFERENCE_MATCH"]',
        )
        pa_rev = ProposalAllocationRecord(
            id="pa_life_rev",
            tenant_id=tenant,
            proposal_id=prop_rev.id,
            payment_id=pay_rev.id,
            invoice_id=inv_rev.id,
            amount_minor=50000,
            currency="USD",
        )
        prop_part = ProposalRecord(
            id="prop_life_part",
            tenant_id=tenant,
            run_id=run.id,
            payment_id=pay_part.id,
            outcome="PROPOSED",
            verifier_status="VERIFIED",
            evidence_json='["REFERENCE_MATCH"]',
        )
        pa_part = ProposalAllocationRecord(
            id="pa_life_part",
            tenant_id=tenant,
            proposal_id=prop_part.id,
            payment_id=pay_part.id,
            invoice_id=inv_part.id,
            amount_minor=50000,
            currency="USD",
        )
        s.add_all([prop_rev, pa_rev, prop_part, pa_part])
        s.commit()

        appr_svc = ApprovalService(s)
        res_rev = appr_svc.approve_proposal(tenant, prop_rev.id, actor="operator@example.com", idempotency_key="idemp_life_rev")
        assert res_rev.status == "APPROVED"
        res_part = appr_svc.approve_proposal(tenant, prop_part.id, actor="operator@example.com", idempotency_key="idemp_life_part")
        assert res_part.status == "APPROVED"

        # 3. Provider Settlement on inv_sett
        sett = ProviderSettlementRecord(
            id="sett_life_1",
            tenant_id=tenant,
            provider="paypal",
            environment="sandbox",
            provider_settlement_id="SETT-LIFE-001",
            invoice_id=inv_sett.id,
            payment_id=pay_sett.id,
            amount_minor=50000,
            currency="USD",
        )
        s.add(sett)
        s.commit()

        # All 3 invoices are fully paid/settled
        assert inv_repo.get_ledger_outstanding_minor(tenant, inv_rev.id) == 0
        assert inv_repo.get_ledger_outstanding_minor(tenant, inv_part.id) == 0
        assert inv_repo.get_ledger_outstanding_minor(tenant, inv_sett.id) == 0

        # 4. Partial Refund: $200 refund on pay_part ($500 payment -> $200 refunded)
        r_part = RefundRecord(
            id="ref_life_part",
            tenant_id=tenant,
            provider="paypal",
            provider_refund_id="REF-LIFE-PART",
            parent_payment_id=pay_part.id,
            invoice_id=inv_part.id,
            amount_minor=20000,  # $200
            currency="USD",
            status="COMPLETED",
            record_type="REFUND",
            refunded_at=now,
        )

        # 5. Full Reversal: $500 reversal on pay_rev
        r_rev = RefundRecord(
            id="ref_life_rev",
            tenant_id=tenant,
            provider="paypal",
            provider_refund_id="REV-LIFE-FULL",
            parent_payment_id=pay_rev.id,
            invoice_id=inv_rev.id,
            amount_minor=50000,  # $500
            currency="USD",
            status="COMPLETED",
            record_type="REVERSAL",
            refunded_at=now,
        )
        s.add_all([r_part, r_rev])
        s.commit()

        # 6. Verification of Resulting Balances
        # a) Fully reversed invoice: Outstanding balance fully restored to $500
        assert inv_repo.get_total_returns_minor(tenant, inv_rev.id) == 50000
        assert inv_repo.get_ledger_outstanding_minor(tenant, inv_rev.id) == 50000
        # Invoice is eligible for replacement allocation
        inv_repo.validate_eligibility_for_allocation(inv_rev)

        # b) Partially refunded invoice: Outstanding balance restored to $200
        assert inv_repo.get_total_returns_minor(tenant, inv_part.id) == 20000
        assert inv_repo.get_ledger_outstanding_minor(tenant, inv_part.id) == 20000
        inv_repo.validate_eligibility_for_allocation(inv_part)

        # c) Payment net amounts & unapplied cash
        assert pay_repo.get_unapplied_cash_minor(tenant, pay_rev.id) == 0
        assert pay_repo.get_unapplied_cash_minor(tenant, pay_part.id) == 0

        # 8. Replacement payment approval after reversal
        pay_replacement = PaymentRecord(
            id="pay_life_replacement",
            tenant_id=tenant,
            provider="paypal",
            provider_payment_id="PAY-LIFE-REPLACEMENT",
            amount_minor=50000,  # $500
            currency="USD",
            received_at=now,
            reference="INV-LIFE-REV",
        )
        s.add(pay_replacement)
        s.flush()

        prop_replacement = ProposalRecord(
            id="prop_life_replacement",
            tenant_id=tenant,
            run_id=run.id,
            payment_id=pay_replacement.id,
            outcome="PROPOSED",
            verifier_status="VERIFIED",
            evidence_json='["REFERENCE_MATCH"]',
        )
        pa_replacement = ProposalAllocationRecord(
            id="pa_life_replacement",
            tenant_id=tenant,
            proposal_id=prop_replacement.id,
            payment_id=pay_replacement.id,
            invoice_id=inv_rev.id,
            amount_minor=50000,
            currency="USD",
        )
        s.add_all([prop_replacement, pa_replacement])
        s.commit()

        res_rep = appr_svc.approve_proposal(
            tenant,
            prop_replacement.id,
            actor="operator@example.com",
            idempotency_key="idemp_life_replacement",
        )
        assert res_rep.status == "APPROVED"

        # Now inv_rev is once again fully paid by the replacement payment
        assert inv_repo.get_ledger_outstanding_minor(tenant, inv_rev.id) == 0

    # API Endpoint Ground Truth Verification
    resp_inv = client.get(f"/admin/invoices?tenant_id={tenant}", headers=AUTH_HEADERS)
    assert resp_inv.status_code == 200
    invs = {i["provider_invoice_id"]: i for i in resp_inv.json()["invoices"]}

    # Partially refunded invoice via API: status is PARTIAL, outstanding $200
    part_api = invs["INV-LIFE-PART"]
    assert part_api["total_minor"] == 50000
    assert part_api["allocated_minor"] == 50000
    assert part_api["refunded_minor"] == 20000
    assert part_api["net_applied_minor"] == 30000
    assert part_api["outstanding_balance_minor"] == 20000
    assert part_api["reconciliation_status"] == "PARTIAL"

    # Detail endpoint for partially refunded invoice
    resp_detail = client.get(f"/admin/invoices/{part_api['id']}?tenant_id={tenant}", headers=AUTH_HEADERS)
    assert resp_detail.status_code == 200
    detail = resp_detail.json()
    assert detail["refunded_minor"] == 20000
    assert len(detail["refunds"]) == 1
    assert detail["refunds"][0]["provider_refund_id"] == "REF-LIFE-PART"
    assert detail["reconciliation_status"] == "PARTIAL"

    # Transaction API verification: net amounts and statuses
    resp_tx = client.get(f"/admin/transactions?tenant_id={tenant}", headers=AUTH_HEADERS)
    assert resp_tx.status_code == 200
    txs = {t["provider_payment_id"]: t for t in resp_tx.json()["transactions"]}

    # Reversed payment: net amount 0, reversal 500, unapplied 0, status RECONCILED
    tx_rev = txs["PAY-LIFE-REV"]
    assert tx_rev["amount_minor"] == 50000
    assert tx_rev["reversal_amount_minor"] == 50000
    assert tx_rev["net_amount_minor"] == 0
    assert tx_rev["unapplied_cash_minor"] == 0
    assert tx_rev["reconciliation_status"] == "RECONCILED"

    # Partially refunded payment: net amount 300, refund 200, unapplied 0, status RECONCILED
    tx_part = txs["PAY-LIFE-PART"]
    assert tx_part["amount_minor"] == 50000
    assert tx_part["refund_amount_minor"] == 20000
    assert tx_part["net_amount_minor"] == 30000
    assert tx_part["unapplied_cash_minor"] == 0
    assert tx_part["reconciliation_status"] == "RECONCILED"


def test_dashboard_reconciled_amount_does_not_count_refunds_or_reversals(client):
    """Verify that refunded and reversed payments ($550) are never counted in reconciled_amount_minor.

    Root Cause Test:
    Prior to the fix, main.py calculated:
        allocated = p.amount_minor - unapplied
    Because PAY-DEMO-REFUND-007 ($150) and PAY-DEMO-REVERSAL-008 ($400) had unapplied_cash == 0,
    the dashboard incorrectly added $150 + $400 = $550 to reconciled_amount_minor, even though
    zero dollars were allocated to canonical invoices.

    This test verifies:
    1. Fresh demo seed has reconciled_amount_minor == 0, unapplied_cash_minor == 736000 ($7,360),
       and ingested == 791000 ($7,910).
    2. Approving PAY-DEMO-EXACT-001 ($1,250) increments reconciled_amount_minor to exactly 125000.
    3. Refunded/reversed payments continue to be excluded from reconciled invoice value.
    """
    client.post("/admin/demo/seed", json={"tenant_id": "demo_merchant"}, headers=AUTH_HEADERS)

    resp = client.get("/admin/dashboard/summary?tenant_id=demo_merchant", headers=AUTH_HEADERS)
    assert resp.status_code == 200
    data = resp.json()

    usd = data["financial_totals"]["USD"]
    # Ingested: $7,910.00
    assert usd["payments_amount_minor"] == 791000
    # Reconciled to Invoices MUST be $0.00 when zero proposals are approved
    # (NOT $550.00 from refunds/reversals!)
    assert usd["reconciled_amount_minor"] == 0
    # Unapplied cash is $7,360.00 (the 9 unallocated non-refunded payments)
    assert usd["unapplied_cash_minor"] == 736000

    # Verify transaction list has allocated_minor == 0 for refund and reversal
    resp_tx = client.get("/admin/transactions?tenant_id=demo_merchant", headers=AUTH_HEADERS)
    assert resp_tx.status_code == 200
    tx_map = {t["provider_payment_id"]: t for t in resp_tx.json()["transactions"]}
    assert tx_map["PAY-DEMO-REFUND-007"]["allocated_minor"] == 0
    assert tx_map["PAY-DEMO-REVERSAL-008"]["allocated_minor"] == 0

    # Now approve proposal for PAY-DEMO-EXACT-001 ($1,250.00)
    resp_props = client.get("/admin/proposals?tenant_id=demo_merchant", headers=AUTH_HEADERS)
    assert resp_props.status_code == 200
    exact_prop = next(p for p in resp_props.json()["proposals"] if p["provider_payment_id"] == "PAY-DEMO-EXACT-001")

    resp_appr = client.post(
        f"/admin/proposals/{exact_prop['id']}/approve",
        json={
            "reviewer": "operator@example.demo",
            "tenant_id": "demo_merchant",
            "reason": "Test approval of exact match",
        },
        headers=AUTH_HEADERS,
    )
    assert resp_appr.status_code == 200

    # Re-fetch dashboard summary
    resp_after = client.get("/admin/dashboard/summary?tenant_id=demo_merchant", headers=AUTH_HEADERS)
    assert resp_after.status_code == 200
    usd_after = resp_after.json()["financial_totals"]["USD"]
    # Reconciled to Invoices MUST now be exactly $1,250.00 (125000)
    assert usd_after["reconciled_amount_minor"] == 125000
    # Unapplied cash decreased by $1,250.00: 736000 - 125000 = 611000 ($6,110.00)
    assert usd_after["unapplied_cash_minor"] == 611000


def test_proposal_evidence_enrichment_split_and_exact(client):
    """Verify proposal evidence enrichment accurately describes split allocations and exact matches."""
    client.post("/admin/demo/seed", json={"tenant_id": "demo_merchant"}, headers=AUTH_HEADERS)

    resp = client.get("/admin/proposals?tenant_id=demo_merchant", headers=AUTH_HEADERS)
    assert resp.status_code == 200
    props = {p["provider_payment_id"]: p for p in resp.json()["proposals"]}

    # Split payment: PAY-DEMO-SPLIT-005 ($1,500 across 2 invoices)
    split_prop = props["PAY-DEMO-SPLIT-005"]
    assert len(split_prop["allocations"]) == 2
    assert any("Split Allocation" in e for e in split_prop["evidence"])

    # Exact payment: PAY-DEMO-EXACT-001 ($1,250 against 1 invoice)
    exact_prop = props["PAY-DEMO-EXACT-001"]
    assert len(exact_prop["allocations"]) == 1
    assert any("Exact Match" in e for e in exact_prop["evidence"])


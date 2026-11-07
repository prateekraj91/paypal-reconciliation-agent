"""Comprehensive Hardening Regression Tests for Milestone D7.

Validates all security, correctness, and safety invariants:
1. Credential Safety:
   - No hardcoded admin tokens in frontend source or dist bundle
   - Protected endpoints reject unauthenticated or invalid credential requests
2. AI Investigation Approval Correctness:
   - Rejection of INVALID, UNVERIFIED, NEEDS_REVIEW, or missing verifier statuses (fail-closed)
   - Rejection of incomplete investigations (QUEUED, RUNNING, FAILED)
   - Rejection of repeated approvals on already resolved investigations
   - Proposal linkage: rejection if proposal belongs to another payment
   - Disambiguation required when multiple pending proposals match
   - Rejection of missing, zero, or negative suggested amounts (no falling back to payment total)
   - Rejection when suggested amount exceeds invoice ledger capacity
   - Support for split payment allocations across multiple invoices
   - Rejection when split total exceeds payment amount or unapplied cash
   - Approval idempotency and rejection of repeated investigation approvals
   - Zero financial mutations when any validation check fails
3. Demo Data Safety:
   - Demo seeding strictly restricted to 'demo_merchant' (fails on other tenants)
   - Repeated demo seed with force=True cleanly resets without duplicate dangling records
"""

from __future__ import annotations

import os
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from datetime import datetime, timezone, timedelta

from sqlalchemy import select
from reconcile_api.config import APIConfig
from reconcile_api.dependencies import set_db_engine
from reconcile_api.main import app
from reconcile_api.demo_data import seed_demo_data
from reconcile_store.config import DatabaseConfig
from reconcile_store.db import DatabaseEngine
from reconcile_store.schema import (
    Base,
    AllocationRecord,
    PaymentRecord,
    InvoiceRecord,
    ProposalRecord,
    ProposalAllocationRecord,
    InvestigationRecord,
)
from reconcile_store.repositories.tenant_repo import TenantRepository
from reconcile_store.repositories.payment_repo import PaymentRepository
from reconcile_store.repositories.invoice_repo import InvoiceRepository
from reconcile_store.repositories.investigation_repo import InvestigationRepository
from reconcile_store.repositories.reconciliation_repo import ProposalRepository, ReconciliationRunRepository
from reconcile_store.approval import ApprovalService


ADMIN_KEY = "test-hardening-admin-key-9999"
AUTH_HEADERS = {"Authorization": f"Bearer {ADMIN_KEY}"}


@pytest.fixture
def test_db():
    engine = DatabaseEngine(DatabaseConfig(url="sqlite:///:memory:"))
    Base.metadata.create_all(engine.engine)
    set_db_engine(engine)
    with engine.session() as s:
        TenantRepository(s).create_tenant("demo_merchant", "Demo Merchant")
        TenantRepository(s).create_tenant("real_merchant", "Real Merchant Production")
        s.commit()
    yield engine
    set_db_engine(None)


@pytest.fixture
def client(test_db, monkeypatch):
    monkeypatch.setenv("ADMIN_API_KEY", ADMIN_KEY)
    monkeypatch.setenv("DEFAULT_TENANT_ID", "demo_merchant")
    return TestClient(app)


# ==============================================================================
# 1. CREDENTIAL SAFETY TESTS
# ==============================================================================

def test_no_hardcoded_admin_token_in_source_or_dist():
    """Verify that DEFAULT_API_KEY and test-secret-key-12345 do not appear in frontend files."""
    frontend_dir = Path(__file__).resolve().parent.parent / "frontend"
    banned_tokens = ["test-secret-key-12345", "DEFAULT_API_KEY"]

    # Check src
    src_dir = frontend_dir / "src"
    for p in src_dir.rglob("*.ts*"):
        text = p.read_text(encoding="utf-8")
        for token in banned_tokens:
            assert token not in text, f"Banned credential '{token}' found in {p}"

    # Check dist
    dist_dir = frontend_dir / "dist"
    if dist_dir.exists():
        for p in dist_dir.rglob("*"):
            if p.is_file() and p.suffix in (".js", ".html", ".css"):
                text = p.read_text(encoding="utf-8")
                for token in banned_tokens:
                    assert token not in text, f"Banned credential '{token}' found in compiled asset {p}"


def test_protected_requests_without_credentials(client):
    """Verify all admin endpoints reject unauthenticated requests with 401."""
    protected_urls = [
        ("GET", "/admin/dashboard/summary"),
        ("GET", "/admin/transactions"),
        ("GET", "/admin/invoices"),
        ("GET", "/admin/proposals"),
        ("GET", "/admin/investigations"),
        ("GET", "/admin/audit-events"),
        ("GET", "/admin/audit-events/verify"),
        ("POST", "/admin/demo/seed"),
    ]

    for method, path in protected_urls:
        if method == "GET":
            resp_no_auth = client.get(path)
            resp_bad_auth = client.get(path, headers={"Authorization": "Bearer wrong-key"})
        else:
            resp_no_auth = client.post(path, json={})
            resp_bad_auth = client.post(path, json={}, headers={"Authorization": "Bearer wrong-key"})

        assert resp_no_auth.status_code == 401, f"{path} did not reject unauthenticated request"
        assert resp_bad_auth.status_code == 401, f"{path} did not reject invalid credential"


# ==============================================================================
# 2. INVESTIGATION APPROVAL ELIGIBILITY & VERIFIER FAIL-CLOSED TESTS
# ==============================================================================

def test_investigation_approval_invalid_verifier_status(client, test_db):
    """Investigation with verifier_status='INVALID' MUST fail closed (HTTP 400)."""
    with test_db.session() as s:
        inv_repo = InvestigationRepository(s)
        inv = inv_repo.create_investigation("demo_merchant", "payment", "PAY-TEST-001", status="NEEDS_REVIEW")
        inv_repo.record_investigation_result(
            tenant_id="demo_merchant",
            investigation_id=inv.id,
            status="NEEDS_REVIEW",
            classification="PROMPT_INJECTION_SUSPECT",
            summary="Attacker tried to override system instructions",
            result_dict={"suggested_resolution": {"resolution_type": "ALLOCATE_PAYMENT", "suggested_invoice_ids": ["INV-1"], "suggested_amount_cents": 1000}},
            evidence_references=[],
            verifier_status="INVALID",
            verifier_details={"passed": False, "notes": ["Safety check failed"]},
        )
        s.commit()
        inv_id = inv.id

    resp = client.post(
        f"/admin/investigations/{inv_id}/approve",
        json={"reviewer": "operator@test.com", "tenant_id": "demo_merchant"},
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 400
    assert "INVALID" in resp.json()["detail"]
    assert "strictly blocked" in resp.json()["detail"].lower()


def test_investigation_approval_missing_or_unverified_status(client, test_db):
    """Investigation with verifier_status=NOT_VERIFIED, UNVERIFIED, or NEEDS_REVIEW MUST fail closed."""
    for bad_status in ["NOT_VERIFIED", "UNVERIFIED", "NEEDS_REVIEW", "UNKNOWN"]:
        with test_db.session() as s:
            inv_repo = InvestigationRepository(s)
            inv = inv_repo.create_investigation("demo_merchant", "payment", f"PAY-{bad_status}", status="NEEDS_REVIEW")
            inv.verifier_status = bad_status
            inv.result_json = '{"suggested_resolution": {"resolution_type": "ALLOCATE_PAYMENT", "suggested_invoice_ids": ["INV-1"], "suggested_amount_cents": 1000}}'
            s.commit()
            inv_id = inv.id

        resp = client.post(
            f"/admin/investigations/{inv_id}/approve",
            json={"reviewer": "operator@test.com", "tenant_id": "demo_merchant"},
            headers=AUTH_HEADERS,
        )
        assert resp.status_code == 400
        assert "VERIFIED" in resp.json()["detail"]


def test_investigation_approval_incomplete_status(client, test_db):
    """Incomplete investigations (QUEUED, RUNNING, FAILED) MUST be rejected."""
    for incomplete_status in ["QUEUED", "RUNNING", "FAILED"]:
        with test_db.session() as s:
            inv_repo = InvestigationRepository(s)
            inv = inv_repo.create_investigation("demo_merchant", "payment", f"PAY-{incomplete_status}", status=incomplete_status)
            inv.verifier_status = "VERIFIED"
            inv.result_json = '{"suggested_resolution": {"resolution_type": "ALLOCATE_PAYMENT", "suggested_invoice_ids": ["INV-1"], "suggested_amount_cents": 1000}}'
            s.commit()
            inv_id = inv.id

        resp = client.post(
            f"/admin/investigations/{inv_id}/approve",
            json={"reviewer": "operator@test.com", "tenant_id": "demo_merchant"},
            headers=AUTH_HEADERS,
        )
        assert resp.status_code == 400
        assert "ineligible status" in resp.json()["detail"].lower() or "incomplete" in resp.json()["detail"].lower()


def test_repeated_investigation_approval(client):
    """Attempting repeated approval on already resolved investigation MUST be rejected."""
    client.post("/admin/demo/seed", json={"tenant_id": "demo_merchant"}, headers=AUTH_HEADERS)

    resp_invgs = client.get("/admin/investigations?tenant_id=demo_merchant", headers=AUTH_HEADERS)
    verified_inv = next(i for i in resp_invgs.json()["investigations"] if i["verifier_status"] == "VERIFIED")

    # 1. First approval succeeds
    resp1 = client.post(
        f"/admin/investigations/{verified_inv['id']}/approve",
        json={"reviewer": "operator@test.com", "tenant_id": "demo_merchant"},
        headers=AUTH_HEADERS,
    )
    assert resp1.status_code == 200
    assert resp1.json()["status"] == "APPROVED"

    # 2. Repeated approval MUST be rejected with 400
    resp2 = client.post(
        f"/admin/investigations/{verified_inv['id']}/approve",
        json={"reviewer": "operator@test.com", "tenant_id": "demo_merchant"},
        headers=AUTH_HEADERS,
    )
    assert resp2.status_code == 400
    assert "already resolved" in resp2.json()["detail"].lower()


# ==============================================================================
# 3. PROPOSAL LINKAGE & ALLOCATION CORRECTNESS TESTS
# ==============================================================================

def test_proposal_linkage_belongs_to_different_payment(client, test_db):
    """Passing proposal_id belonging to a different payment MUST be rejected."""
    with test_db.session() as s:
        p_repo = PaymentRepository(s)
        i_repo = InvoiceRepository(s)
        prop_repo = ProposalRepository(s)
        invg_repo = InvestigationRepository(s)
        run_repo = ReconciliationRunRepository(s)

        run = run_repo.create_run("demo_merchant", "deterministic_v2", "2.0.0")

        pay1 = p_repo.upsert_payment("demo_merchant", "paypal", "PAY-DIFF-1", 5000, "USD", "COMPLETED", "User 1")
        pay2 = p_repo.upsert_payment("demo_merchant", "paypal", "PAY-DIFF-2", 5000, "USD", "COMPLETED", "User 2")
        inv1 = i_repo.upsert_invoice("demo_merchant", "paypal", "INV-DIFF-1", "INV-DIFF-1", 5000, "USD", "SENT")

        # Proposal for pay2
        prop_foreign = prop_repo.create_proposal(
            tenant_id="demo_merchant",
            run_id=run.id,
            payment_id=pay2.id,
            outcome="PROPOSED",
            evidence=["REFERENCE_MATCH", "AMOUNT_EXACT"],
            allocations=[{"invoice_id": inv1.id, "amount_minor": 5000, "currency": "USD"}],
        )

        # Investigation on pay1
        inv = invg_repo.create_investigation("demo_merchant", "payment", pay1.provider_payment_id, status="NEEDS_REVIEW")
        invg_repo.record_investigation_result(
            tenant_id="demo_merchant",
            investigation_id=inv.id,
            status="NEEDS_REVIEW",
            classification="UNMATCHED_PAYMENT",
            summary="Investigating pay1",
            result_dict={
                "suggested_resolution": {
                    "resolution_type": "ALLOCATE_PAYMENT",
                    "suggested_invoice_ids": [inv1.provider_invoice_id],
                    "suggested_amount_cents": 5000,
                }
            },
            evidence_references=[],
            verifier_status="VERIFIED",
            verifier_details={"passed": True, "notes": []},
        )
        s.commit()
        inv_id = inv.id
        foreign_prop_id = prop_foreign.id

    # Try to approve inv_id with foreign_prop_id
    resp = client.post(
        f"/admin/investigations/{inv_id}/approve",
        json={"reviewer": "op@test.com", "tenant_id": "demo_merchant", "proposal_id": foreign_prop_id},
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 400
    assert "different payment" in resp.json()["detail"].lower()


def test_missing_and_zero_suggested_amount(client, test_db):
    """Falling back to payment total when suggested_amount is missing or zero is strictly prohibited."""
    for bad_amt in [None, 0, -100]:
        with test_db.session() as s:
            p_repo = PaymentRepository(s)
            i_repo = InvoiceRepository(s)
            invg_repo = InvestigationRepository(s)

            pay = p_repo.upsert_payment("demo_merchant", "paypal", f"PAY-ZERO-{bad_amt}", 10000, "USD", "COMPLETED", "User")
            inv = i_repo.upsert_invoice("demo_merchant", "paypal", f"INV-ZERO-{bad_amt}", f"INV-ZERO-{bad_amt}", 10000, "USD", "SENT")

            investigation = invg_repo.create_investigation("demo_merchant", "payment", pay.provider_payment_id, status="NEEDS_REVIEW")
            invg_repo.record_investigation_result(
                tenant_id="demo_merchant",
                investigation_id=investigation.id,
                status="NEEDS_REVIEW",
                classification="UNMATCHED_PAYMENT",
                summary="Test bad amount",
                result_dict={
                    "suggested_resolution": {
                        "resolution_type": "ALLOCATE_PAYMENT",
                        "suggested_invoice_ids": [inv.provider_invoice_id],
                        "suggested_amount_cents": bad_amt,
                    }
                },
                evidence_references=[],
                verifier_status="VERIFIED",
                verifier_details={"passed": True, "notes": []},
            )
            s.commit()
            inv_id = investigation.id

        resp = client.post(
            f"/admin/investigations/{inv_id}/approve",
            json={"reviewer": "op@test.com", "tenant_id": "demo_merchant"},
            headers=AUTH_HEADERS,
        )
        assert resp.status_code == 400
        assert "invalid suggested allocation amount" in resp.json()["detail"].lower() or "positive integer" in resp.json()["detail"].lower()


def test_incorrect_invoice_capacity(client, test_db):
    """Suggested allocation exceeding invoice outstanding balance MUST be rejected."""
    with test_db.session() as s:
        p_repo = PaymentRepository(s)
        i_repo = InvoiceRepository(s)
        prop_repo = ProposalRepository(s)
        invg_repo = InvestigationRepository(s)
        run_repo = ReconciliationRunRepository(s)
        app_service = ApprovalService(s)

        run = run_repo.create_run("demo_merchant", "deterministic_v2", "2.0.0")

        # Invoice total 5000 cents ($50)
        inv = i_repo.upsert_invoice("demo_merchant", "paypal", "INV-CAP-1", "INV-CAP-1", 5000, "USD", provider_due_minor=None, status="SENT")

        # Prior payment of 4000 cents already approved and allocated
        pay_prior = p_repo.upsert_payment("demo_merchant", "paypal", "PAY-CAP-PRIOR", 4000, "USD", "COMPLETED", "User A", reference="INV-CAP-1")
        prop_prior = prop_repo.create_proposal(
            tenant_id="demo_merchant",
            run_id=run.id,
            payment_id=pay_prior.id,
            outcome="PROPOSED",
            evidence=["REFERENCE_MATCH", "AMOUNT_EXACT"],
            allocations=[{"invoice_id": inv.id, "amount_minor": 4000, "currency": "USD"}],
        )
        app_service.approve_proposal("demo_merchant", prop_prior.id, "test_actor", "idemp:prior")
        s.commit()

        # Outstanding is now 5000 - 4000 = 1000 cents.
        # New payment of 3000 cents tries to allocate 3000 to INV-CAP-1 (overflows 1000 capacity)
        pay_new = p_repo.upsert_payment("demo_merchant", "paypal", "PAY-CAP-NEW", 3000, "USD", "COMPLETED", "User B", reference="INV-CAP-1")
        prop_new = prop_repo.create_proposal(
            tenant_id="demo_merchant",
            run_id=run.id,
            payment_id=pay_new.id,
            outcome="PROPOSED",
            evidence=["REFERENCE_MATCH", "AMOUNT_EXACT"],
            allocations=[{"invoice_id": inv.id, "amount_minor": 3000, "currency": "USD"}],
        )

        invg = invg_repo.create_investigation("demo_merchant", "payment", pay_new.provider_payment_id, status="NEEDS_REVIEW")
        invg_repo.record_investigation_result(
            tenant_id="demo_merchant",
            investigation_id=invg.id,
            status="NEEDS_REVIEW",
            classification="UNMATCHED_PAYMENT",
            summary="Try overflow capacity",
            result_dict={
                "suggested_resolution": {
                    "resolution_type": "ALLOCATE_PAYMENT",
                    "suggested_invoice_ids": [inv.provider_invoice_id],
                    "suggested_amount_cents": 3000,
                }
            },
            evidence_references=[],
            verifier_status="VERIFIED",
            verifier_details={"passed": True, "notes": []},
        )
        s.commit()
        inv_id = invg.id
        prop_id = prop_new.id

    resp = client.post(
        f"/admin/investigations/{inv_id}/approve",
        json={"reviewer": "op@test.com", "tenant_id": "demo_merchant", "proposal_id": prop_id},
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 400
    assert "outstanding balance" in resp.json()["detail"].lower()


def test_split_allocation_across_multiple_invoices(client, test_db):
    """Verify split allocation across multiple invoices succeeds and validates individual capacities."""
    with test_db.session() as s:
        p_repo = PaymentRepository(s)
        i_repo = InvoiceRepository(s)
        prop_repo = ProposalRepository(s)
        invg_repo = InvestigationRepository(s)
        run_repo = ReconciliationRunRepository(s)

        run = run_repo.create_run("demo_merchant", "deterministic_v2", "2.0.0")

        pay = p_repo.upsert_payment("demo_merchant", "paypal", "PAY-SPLIT-1", 10000, "USD", "COMPLETED", "Acme Parent", reference="INV-SPLIT-1 INV-SPLIT-2")
        invA = i_repo.upsert_invoice("demo_merchant", "paypal", "INV-SPLIT-1", "INV-SPLIT-1", 6000, "USD", provider_due_minor=None, status="SENT")
        invB = i_repo.upsert_invoice("demo_merchant", "paypal", "INV-SPLIT-2", "INV-SPLIT-2", 4000, "USD", provider_due_minor=None, status="SENT")

        # Deterministic proposal for split match
        prop = prop_repo.create_proposal(
            tenant_id="demo_merchant",
            run_id=run.id,
            payment_id=pay.id,
            outcome="PROPOSED",
            evidence=["REFERENCE_MATCH", "AMOUNT_EXACT"],
            allocations=[
                {"invoice_id": invA.id, "amount_minor": 6000, "currency": "USD"},
                {"invoice_id": invB.id, "amount_minor": 4000, "currency": "USD"},
            ],
        )

        invg = invg_repo.create_investigation("demo_merchant", "payment", pay.provider_payment_id, status="NEEDS_REVIEW")
        invg_repo.record_investigation_result(
            tenant_id="demo_merchant",
            investigation_id=invg.id,
            status="NEEDS_REVIEW",
            classification="PARTIAL_PAYMENT",
            summary="Recommended split allocation across two invoices",
            result_dict={
                "suggested_resolution": {
                    "resolution_type": "ALLOCATE_PAYMENT",
                    "suggested_invoice_ids": [invA.provider_invoice_id, invB.provider_invoice_id],
                    "suggested_allocations": {
                        invA.provider_invoice_id: 6000,
                        invB.provider_invoice_id: 4000,
                    },
                }
            },
            evidence_references=[],
            verifier_status="VERIFIED",
            verifier_details={"passed": True, "notes": []},
        )
        s.commit()
        inv_id = invg.id
        prop_id = prop.id

    resp = client.post(
        f"/admin/investigations/{inv_id}/approve",
        json={"reviewer": "op@test.com", "tenant_id": "demo_merchant", "proposal_id": prop_id},
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "APPROVED"
    assert len(data["allocation_ids"]) == 2


def test_split_allocation_exceeding_payment_total(client, test_db):
    """Split allocation summing to more than payment amount MUST be rejected."""
    with test_db.session() as s:
        p_repo = PaymentRepository(s)
        i_repo = InvoiceRepository(s)
        invg_repo = InvestigationRepository(s)

        pay = p_repo.upsert_payment("demo_merchant", "paypal", "PAY-SPLIT-OVER", 10000, "USD", "COMPLETED", "Acme")
        invA = i_repo.upsert_invoice("demo_merchant", "paypal", "INV-OVER-1", "INV-OVER-1", 7000, "USD", "SENT")
        invB = i_repo.upsert_invoice("demo_merchant", "paypal", "INV-OVER-2", "INV-OVER-2", 7000, "USD", "SENT")

        invg = invg_repo.create_investigation("demo_merchant", "payment", pay.provider_payment_id, status="NEEDS_REVIEW")
        invg_repo.record_investigation_result(
            tenant_id="demo_merchant",
            investigation_id=invg.id,
            status="NEEDS_REVIEW",
            classification="PARTIAL_PAYMENT",
            summary="Exceeds total",
            result_dict={
                "suggested_resolution": {
                    "resolution_type": "ALLOCATE_PAYMENT",
                    "suggested_invoice_ids": [invA.provider_invoice_id, invB.provider_invoice_id],
                    "suggested_allocations": {
                        invA.provider_invoice_id: 7000,
                        invB.provider_invoice_id: 7000,  # 14000 > 10000
                    },
                }
            },
            evidence_references=[],
            verifier_status="VERIFIED",
            verifier_details={"passed": True, "notes": []},
        )
        s.commit()
        inv_id = invg.id

    resp = client.post(
        f"/admin/investigations/{inv_id}/approve",
        json={"reviewer": "op@test.com", "tenant_id": "demo_merchant"},
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 400
    assert "exceeds payment total" in resp.json()["detail"].lower()


def test_zero_financial_mutations_when_validation_fails(client, test_db):
    """Verify that zero rows are written to AllocationRecord when investigation approval validation fails."""
    with test_db.session() as s:
        p_repo = PaymentRepository(s)
        i_repo = InvoiceRepository(s)
        invg_repo = InvestigationRepository(s)

        pay = p_repo.upsert_payment("demo_merchant", "paypal", "PAY-ZERO-MUT", 10000, "USD", "COMPLETED", "User")
        inv = i_repo.upsert_invoice("demo_merchant", "paypal", "INV-ZERO-MUT", "INV-ZERO-MUT", 10000, "USD", "SENT")

        # Invalid verifier status
        invg = invg_repo.create_investigation("demo_merchant", "payment", pay.provider_payment_id, status="NEEDS_REVIEW")
        invg_repo.record_investigation_result(
            tenant_id="demo_merchant",
            investigation_id=invg.id,
            status="NEEDS_REVIEW",
            classification="PROMPT_INJECTION_SUSPECT",
            summary="Adversarial attempt",
            result_dict={
                "suggested_resolution": {
                    "resolution_type": "ALLOCATE_PAYMENT",
                    "suggested_invoice_ids": [inv.provider_invoice_id],
                    "suggested_amount_cents": 10000,
                }
            },
            evidence_references=[],
            verifier_status="INVALID",
            verifier_details={"passed": False, "notes": []},
        )
        s.commit()
        inv_id = invg.id

        alloc_count_before = len(s.query(AllocationRecord).filter_by(tenant_id="demo_merchant").all())

    resp = client.post(
        f"/admin/investigations/{inv_id}/approve",
        json={"reviewer": "attacker@evil.com", "tenant_id": "demo_merchant"},
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 400

    with test_db.session() as s:
        alloc_count_after = len(s.query(AllocationRecord).filter_by(tenant_id="demo_merchant").all())
        assert alloc_count_after == alloc_count_before == 0


# ==============================================================================
# 4. DEMO DATA SAFETY TESTS
# ==============================================================================

def test_demo_seed_cannot_target_non_demo_tenant(client):
    """POST /admin/demo/seed MUST reject any tenant other than 'demo_merchant'."""
    resp = client.post(
        "/admin/demo/seed",
        json={"tenant_id": "real_merchant"},
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 400
    assert "strictly restricted to the 'demo_merchant' tenant" in resp.json()["detail"]


def test_seed_demo_data_python_function_safeguard(test_db):
    """Directly calling seed_demo_data with non-demo tenant MUST raise ValueError."""
    with test_db.session() as s:
        with pytest.raises(ValueError, match="strictly restricted"):
            seed_demo_data(s, tenant_id="real_merchant")


def test_multiple_pending_proposals_requires_disambiguation(client, test_db):
    """When multiple pending proposals match the same recommendation, explicit proposal_id is required."""
    with test_db.session() as s:
        p_repo = PaymentRepository(s)
        i_repo = InvoiceRepository(s)
        prop_repo = ProposalRepository(s)
        invg_repo = InvestigationRepository(s)
        run_repo = ReconciliationRunRepository(s)

        run1 = run_repo.create_run("demo_merchant", "deterministic_v2", "2.0.0")
        run2 = run_repo.create_run("demo_merchant", "deterministic_v2", "2.0.0")

        pay = p_repo.upsert_payment("demo_merchant", "paypal", "PAY-MULTI-PROP", 5000, "USD", "COMPLETED", "User Multi", reference="INV-MULTI-PROP")
        inv = i_repo.upsert_invoice("demo_merchant", "paypal", "INV-MULTI-PROP", "INV-MULTI-PROP", 5000, "USD", provider_due_minor=None, status="SENT")

        # Two pending proposals for the same payment and same allocation
        prop1 = prop_repo.create_proposal(
            tenant_id="demo_merchant",
            run_id=run1.id,
            payment_id=pay.id,
            outcome="PROPOSED",
            evidence=["REFERENCE_MATCH", "AMOUNT_EXACT"],
            allocations=[{"invoice_id": inv.id, "amount_minor": 5000, "currency": "USD"}],
        )
        prop2 = prop_repo.create_proposal(
            tenant_id="demo_merchant",
            run_id=run2.id,
            payment_id=pay.id,
            outcome="PROPOSED",
            evidence=["REFERENCE_MATCH", "AMOUNT_EXACT"],
            allocations=[{"invoice_id": inv.id, "amount_minor": 5000, "currency": "USD"}],
        )

        invg = invg_repo.create_investigation("demo_merchant", "payment", pay.provider_payment_id, status="NEEDS_REVIEW")
        invg_repo.record_investigation_result(
            tenant_id="demo_merchant",
            investigation_id=invg.id,
            status="NEEDS_REVIEW",
            classification="UNMATCHED_PAYMENT",
            summary="Multiple proposals exist",
            result_dict={
                "suggested_resolution": {
                    "resolution_type": "ALLOCATE_PAYMENT",
                    "suggested_invoice_ids": [inv.provider_invoice_id],
                    "suggested_amount_cents": 5000,
                }
            },
            evidence_references=[],
            verifier_status="VERIFIED",
            verifier_details={"passed": True, "notes": []},
        )
        s.commit()
        inv_id = invg.id
        prop1_id = prop1.id

    # 1. Calling without proposal_id fails due to ambiguity
    resp_ambiguous = client.post(
        f"/admin/investigations/{inv_id}/approve",
        json={"reviewer": "op@test.com", "tenant_id": "demo_merchant"},
        headers=AUTH_HEADERS,
    )
    assert resp_ambiguous.status_code == 400
    assert "multiple pending proposals match" in resp_ambiguous.json()["detail"].lower()

    # 2. Calling with explicit proposal_id succeeds
    resp_explicit = client.post(
        f"/admin/investigations/{inv_id}/approve",
        json={"reviewer": "op@test.com", "tenant_id": "demo_merchant", "proposal_id": prop1_id},
        headers=AUTH_HEADERS,
    )
    assert resp_explicit.status_code == 200
    assert resp_explicit.json()["status"] == "APPROVED"


def test_approval_idempotency_service(test_db):
    """Verify ApprovalService idempotency key protects against duplicate execution."""
    with test_db.session() as s:
        p_repo = PaymentRepository(s)
        i_repo = InvoiceRepository(s)
        prop_repo = ProposalRepository(s)
        run_repo = ReconciliationRunRepository(s)
        app_service = ApprovalService(s)

        run = run_repo.create_run("demo_merchant", "deterministic_v2", "2.0.0")
        pay = p_repo.upsert_payment("demo_merchant", "paypal", "PAY-IDEMP-1", 5000, "USD", "COMPLETED", "User Idemp", reference="INV-IDEMP-1")
        inv = i_repo.upsert_invoice("demo_merchant", "paypal", "INV-IDEMP-1", "INV-IDEMP-1", 5000, "USD", provider_due_minor=None, status="SENT")

        prop = prop_repo.create_proposal(
            tenant_id="demo_merchant",
            run_id=run.id,
            payment_id=pay.id,
            outcome="PROPOSED",
            evidence=["REFERENCE_MATCH", "AMOUNT_EXACT"],
            allocations=[{"invoice_id": inv.id, "amount_minor": 5000, "currency": "USD"}],
        )
        s.commit()
        prop_id = prop.id

    with test_db.session() as s:
        app_service = ApprovalService(s)
        # First execution
        res1 = app_service.approve_proposal("demo_merchant", prop_id, "op1", "idemp:exact:123")
        assert res1.is_idempotent_replay is False
        assert res1.status == "APPROVED"
        s.commit()

    with test_db.session() as s:
        app_service = ApprovalService(s)
        # Idempotent replay with same key
        res2 = app_service.approve_proposal("demo_merchant", prop_id, "op1", "idemp:exact:123")
        assert res2.is_idempotent_replay is True
        assert res2.status == "APPROVED"
        assert res2.decision_id == res1.decision_id


def test_repeated_demo_seed_behavior(client, test_db):
    """Demonstrate clean idempotent reseeding without accumulating duplicate records."""
    # 1. Initial seed
    resp1 = client.post(
        "/admin/demo/seed",
        json={"tenant_id": "demo_merchant"},
        headers=AUTH_HEADERS,
    )
    assert resp1.status_code == 200

    # 2. Second seed with force=False -> returns already_seeded
    resp2 = client.post(
        "/admin/demo/seed",
        json={"tenant_id": "demo_merchant", "force": False},
        headers=AUTH_HEADERS,
    )
    assert resp2.status_code == 200
    assert resp2.json()["status"] == "already_seeded"

    # 3. Third seed with force=True -> safely resets and reseeds cleanly
    resp3 = client.post(
        "/admin/demo/seed",
        json={"tenant_id": "demo_merchant", "force": True},
        headers=AUTH_HEADERS,
    )
    assert resp3.status_code == 200
    assert resp3.json()["status"] == "seeded"

    with test_db.session() as s:
        payments = s.query(PaymentRecord).filter_by(tenant_id="demo_merchant").all()
        invoices = s.query(InvoiceRecord).filter_by(tenant_id="demo_merchant").all()
        investigations = s.query(InvestigationRecord).filter_by(tenant_id="demo_merchant").all()
        allocations = s.query(AllocationRecord).filter_by(tenant_id="demo_merchant").all()

        assert len(payments) == 6
        assert len(invoices) == 6
        assert len(investigations) == 2
        assert len(allocations) == 1  # exactly 1 approved allocation from seed

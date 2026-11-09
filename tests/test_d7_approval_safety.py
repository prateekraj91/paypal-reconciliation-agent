"""Comprehensive Regression Test Suite for Milestone D7: P0 Approval Gate Safety Invariants.

Verifies:
1. INVALID investigation + VERIFIED proposal: backend approval rejected.
2. Direct API attempt to approve INJECTION-011: rejected (HTTP 400).
3. No allocation created after rejected approval.
4. No PROPOSAL_APPROVED event appended after rejected approval.
5. NEEDS_REVIEW with unresolved customer mismatch (INVESTIGATE-010): approval rejected.
6. NEEDS_REVIEW with all required invariants passed and no blocking issues: approved successfully.
7. Unlinked investigations do not accidentally bypass safety checks (normal clean matches work).
8. Cross-tenant investigation records cannot influence another tenant's proposal.
9. Frontend/API: proposals endpoint surfaces effective verifier status, approval_eligible flag, and reason.
10. Existing exact matches (EXACT-001), split allocations (SPLIT-005), overpayments, and ordinary valid approvals continue to work.
"""

from __future__ import annotations

from datetime import datetime, timezone, date
import json
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from reconcile_api.main import app
from reconcile_api.dependencies import set_db_engine
from reconcile_store.config import DatabaseConfig
from reconcile_store.db import DatabaseEngine
from reconcile_store.schema import (
    Base,
    AllocationRecord,
    DecisionRecord,
    InvestigationRecord,
    AuditEventRecord,
    PaymentRecord,
    InvoiceRecord,
    ProposalRecord,
    ProposalAllocationRecord,
    ReconciliationRunRecord,
)
from reconcile_store.repositories.tenant_repo import TenantRepository
from reconcile_store.repositories.payment_repo import PaymentRepository
from reconcile_store.repositories.invoice_repo import InvoiceRepository
from reconcile_store.repositories.reconciliation_repo import (
    ProposalRepository,
    ReconciliationRunRepository,
)
from reconcile_store.approval import ApprovalService, ApprovalError

ADMIN_KEY = "test-approval-safety-key-4321"
AUTH_HEADERS = {"Authorization": f"Bearer {ADMIN_KEY}"}


@pytest.fixture
def test_db():
    engine = DatabaseEngine(DatabaseConfig(url="sqlite:///:memory:"))
    Base.metadata.create_all(engine.engine)
    set_db_engine(engine)
    with engine.session() as s:
        TenantRepository(s).create_tenant("tenant_safe", "Tenant Safe")
        TenantRepository(s).create_tenant("tenant_other", "Tenant Other")
        s.commit()
    yield engine
    set_db_engine(None)


@pytest.fixture
def client(test_db, monkeypatch):
    monkeypatch.setenv("ADMIN_API_KEY", ADMIN_KEY)
    monkeypatch.setenv("DEFAULT_TENANT_ID", "tenant_safe")
    return TestClient(app)


def _seed_base_entities(s, tenant_id: str, suffix: str, amount_minor: int = 50000, payer_name: str = "Test Remitter"):
    now = datetime.now(timezone.utc)
    today = date(2026, 11, 9)

    pay = PaymentRecord(
        id=f"pay_{suffix}",
        tenant_id=tenant_id,
        provider="paypal",
        provider_payment_id=f"PAY-{suffix.upper()}",
        amount_minor=amount_minor,
        currency="USD",
        status="COMPLETED",
        payer_name=payer_name,
        reference=f"INV-{suffix.upper()}",
        received_at=now,
    )
    inv = InvoiceRecord(
        id=f"inv_{suffix}",
        tenant_id=tenant_id,
        provider="paypal",
        provider_invoice_id=f"INV-{suffix.upper()}",
        invoice_number=f"INV-{suffix.upper()}",
        customer_name="Test Customer Corp",
        total_minor=amount_minor,
        currency="USD",
        status="SENT",
        issued_on=today,
        due_on=today,
    )
    run = ReconciliationRunRecord(
        id=f"run_{suffix}",
        tenant_id=tenant_id,
        input_snapshot_id=f"snap_{suffix}",
        matcher_name="deterministic_v2",
        matcher_version="2.0.0",
        run_at=now,
    )
    s.add_all([pay, inv, run])
    s.flush()

    prop = ProposalRecord(
        id=f"prop_{suffix}",
        tenant_id=tenant_id,
        run_id=run.id,
        payment_id=pay.id,
        outcome="PROPOSED",
        verifier_status="VERIFIED",
        evidence_json=json.dumps(["REFERENCE_MATCH", "AMOUNT_EXACT"]),
        created_at=now,
    )
    s.add(prop)
    s.flush()

    palloc = ProposalAllocationRecord(
        id=f"palloc_{suffix}",
        tenant_id=tenant_id,
        proposal_id=prop.id,
        payment_id=pay.id,
        invoice_id=inv.id,
        amount_minor=amount_minor,
        currency="USD",
    )
    s.add(palloc)
    s.commit()

    return pay, inv, prop, run


# --------------------------------------------------------------------------
# Test 1 & 2: INVALID investigation + PROMPT_INJECTION_SUSPECT
# --------------------------------------------------------------------------

def test_1_invalid_investigation_blocks_proposal_approval(test_db):
    """TEST 1:
    INVALID investigation + VERIFIED proposal: ApprovalService raises ApprovalError and blocks approval.
    """
    with test_db.session() as s:
        pay, inv, prop, _ = _seed_base_entities(s, "tenant_safe", "inj_001", payer_name="Hacker Inc")

        invg = InvestigationRecord(
            id="invg_inj_001",
            tenant_id="tenant_safe",
            target_type="payment",
            target_id=pay.provider_payment_id,
            status="INVALID",
            classification="PROMPT_INJECTION_SUSPECT",
            summary="Adversarial prompt injection tokens detected in memo.",
            verifier_status="INVALID",
            verifier_details_json=json.dumps({
                "status": "INVALID",
                "passed": False,
                "issues": ["Adversarial prompt injection tokens detected in remittance."],
                "checked_invariants": {
                    "tenant_isolation": True,
                    "target_entity_exists": True,
                    "evidence_references_authentic": False,
                    "facts_strictly_grounded": False,
                    "financial_capacity_safe": False,
                    "settlement_allocation_distinction": True,
                    "no_autonomous_financial_mutation": True,
                },
            }),
            result_json=json.dumps({
                "risk_flags": ["PROMPT_INJECTION_DETECTED", "FAIL_CLOSED_BLOCKED"],
            }),
        )
        s.add(invg)
        s.commit()

    with test_db.session() as s:
        app_service = ApprovalService(s)
        with pytest.raises(ApprovalError) as exc_info:
            app_service.approve_proposal(
                tenant_id="tenant_safe",
                proposal_id=prop.id,
                actor="controller@test.com",
                idempotency_key="idemp_test_1",
            )
        err_msg = str(exc_info.value)
        assert "Adversarial prompt injection tokens detected" in err_msg
        assert "Approval is strictly prohibited" in err_msg


def test_2_direct_api_attempt_to_approve_injection_rejected_400(test_db, client):
    """TEST 2:
    Direct API attempt to approve INJECTION-011 returns HTTP 400 Bad Request.
    """
    with test_db.session() as s:
        pay, inv, prop, _ = _seed_base_entities(s, "tenant_safe", "inj_002", payer_name="Adversary Corp")

        invg = InvestigationRecord(
            id="invg_inj_002",
            tenant_id="tenant_safe",
            target_type="payment",
            target_id=pay.provider_payment_id,
            status="INVALID",
            classification="PROMPT_INJECTION_SUSPECT",
            summary="Adversarial prompt injection tokens detected.",
            verifier_status="INVALID",
            result_json=json.dumps({"risk_flags": ["PROMPT_INJECTION_DETECTED"]}),
            verifier_details_json=json.dumps({"status": "INVALID", "passed": False}),
        )
        s.add(invg)
        s.commit()

    resp = client.post(
        f"/admin/proposals/{prop.id}/approve",
        json={"reviewer": "controller@test.com", "notes": "Attempt bypass", "tenant_id": "tenant_safe"},
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "Adversarial prompt injection tokens detected" in detail
    assert "Approval is strictly prohibited" in detail


def test_3_no_allocations_created_after_rejected_approval(test_db, client):
    """TEST 3:
    No allocation created after rejected approval.
    """
    with test_db.session() as s:
        pay, inv, prop, _ = _seed_base_entities(s, "tenant_safe", "inj_no_alloc")

        invg = InvestigationRecord(
            id="invg_inj_noalloc",
            tenant_id="tenant_safe",
            target_type="payment",
            target_id=pay.id,
            status="INVALID",
            classification="PROMPT_INJECTION_SUSPECT",
            verifier_status="INVALID",
            result_json=json.dumps({"risk_flags": ["PROMPT_INJECTION_DETECTED"]}),
            verifier_details_json=json.dumps({"status": "INVALID", "passed": False}),
        )
        s.add(invg)
        s.commit()

    resp = client.post(
        f"/admin/proposals/{prop.id}/approve",
        json={"reviewer": "controller@test.com", "notes": "Attempt", "tenant_id": "tenant_safe"},
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 400

    with test_db.session() as s:
        allocs = s.scalars(select(AllocationRecord).where(
            AllocationRecord.tenant_id == "tenant_safe",
            AllocationRecord.payment_id == pay.id,
        )).all()
        assert len(allocs) == 0


def test_4_no_proposal_approved_audit_event_after_rejected_approval(test_db, client):
    """TEST 4:
    No PROPOSAL_APPROVED audit event appended after rejected approval.
    """
    with test_db.session() as s:
        pay, inv, prop, _ = _seed_base_entities(s, "tenant_safe", "inj_no_audit")

        invg = InvestigationRecord(
            id="invg_inj_noaud",
            tenant_id="tenant_safe",
            target_type="payment",
            target_id=pay.id,
            status="INVALID",
            classification="PROMPT_INJECTION_SUSPECT",
            verifier_status="INVALID",
            result_json=json.dumps({"risk_flags": ["PROMPT_INJECTION_DETECTED"]}),
            verifier_details_json=json.dumps({"status": "INVALID", "passed": False}),
        )
        s.add(invg)
        s.commit()

    resp = client.post(
        f"/admin/proposals/{prop.id}/approve",
        json={"reviewer": "controller@test.com", "notes": "Attempt", "tenant_id": "tenant_safe"},
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 400

    with test_db.session() as s:
        events = s.scalars(select(AuditEventRecord).where(
            AuditEventRecord.tenant_id == "tenant_safe",
            AuditEventRecord.subject_id == prop.id,
            AuditEventRecord.event_type == "PROPOSAL_APPROVED",
        )).all()
        assert len(events) == 0

        # Verify proposal remains PROPOSED
        db_prop = s.get(ProposalRecord, prop.id)
        assert db_prop.outcome == "PROPOSED"
        decisions = db_prop.decisions
        assert len(decisions) == 0


# --------------------------------------------------------------------------
# Test 5: NEEDS_REVIEW with unresolved customer mismatch (INVESTIGATE-010)
# --------------------------------------------------------------------------

def test_5_needs_review_with_unresolved_customer_mismatch_rejected(test_db, client):
    """TEST 5:
    NEEDS_REVIEW with unresolved customer mismatch (e.g. INVESTIGATE-010):
    Approval is strictly rejected by backend and API because remitter does not match invoice customer.
    """
    with test_db.session() as s:
        pay, inv, prop, _ = _seed_base_entities(
            s,
            "tenant_safe",
            "investigate_010",
            amount_minor=89000,
            payer_name="Global Treasury Gateway LLC",
        )
        # Target invoice belongs to Cyberdyne Systems
        inv.customer_name = "Cyberdyne Systems"
        s.flush()

        # Investigation has customer_match: false
        invg = InvestigationRecord(
            id="invg_investigate_010",
            tenant_id="tenant_safe",
            target_type="payment",
            target_id=pay.provider_payment_id,
            status="NEEDS_REVIEW",
            classification="UNMATCHED_PAYMENT",
            summary="Third-party clearinghouse payment without written treasury voucher.",
            verifier_status="NEEDS_REVIEW",
            verifier_details_json=json.dumps({
                "status": "NEEDS_REVIEW",
                "passed": True,
                "issues": [],
                "checked_invariants": {
                    "tenant_isolation": True,
                    "target_entity_exists": True,
                    "evidence_references_authentic": True,
                    "facts_strictly_grounded": True,
                    "financial_capacity_safe": True,
                    "settlement_allocation_distinction": True,
                    "no_autonomous_financial_mutation": True,
                },
            }),
            result_json=json.dumps({
                "candidate_invoice_analysis": [
                    {
                        "invoice_id": inv.id,
                        "invoice_number": inv.invoice_number,
                        "customer_match": False,
                        "amount_match": True,
                    }
                ],
                "conflicting_evidence": [],
                "suggested_resolution": {
                    "resolution_type": "ALLOCATE_PAYMENT",
                    "suggested_invoice_ids": [inv.invoice_number],
                    "suggested_amount_cents": 89000,
                    "suggested_allocations": {inv.invoice_number: 89000},
                },
            }),
        )
        s.add(invg)
        s.commit()

    # Direct backend ApprovalService attempt
    with test_db.session() as s:
        app_service = ApprovalService(s)
        with pytest.raises(ApprovalError) as exc_info:
            app_service.approve_proposal(
                tenant_id="tenant_safe",
                proposal_id=prop.id,
                actor="controller@test.com",
                idempotency_key="idemp_mismatch_1",
            )
        err = str(exc_info.value)
        assert "Unresolved customer mismatch" in err
        assert "Global Treasury Gateway LLC" in err

    # Direct API POST attempt
    resp = client.post(
        f"/admin/proposals/{prop.id}/approve",
        json={"reviewer": "controller@test.com", "notes": "Approve mismatch", "tenant_id": "tenant_safe"},
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 400
    assert "Unresolved customer mismatch" in resp.json()["detail"]


# --------------------------------------------------------------------------
# Test 6: NEEDS_REVIEW with all invariants passed & matching recommendation
# --------------------------------------------------------------------------

def test_6_needs_review_legitimate_human_review_approval_succeeds(test_db, client):
    """TEST 6:
    NEEDS_REVIEW with all required invariants passed, customer_match=True,
    and matching ALLOCATE_PAYMENT recommendation: approved successfully!
    """
    with test_db.session() as s:
        pay, inv, prop, _ = _seed_base_entities(
            s,
            "tenant_safe",
            "legit_rev",
            amount_minor=75000,
            payer_name="Acme Corp Accounts Payable",
        )
        inv.customer_name = "Acme Corp"
        s.flush()

        invg = InvestigationRecord(
            id="invg_legit_rev",
            tenant_id="tenant_safe",
            target_type="payment",
            target_id=pay.provider_payment_id,
            status="NEEDS_REVIEW",
            classification="UNMATCHED_PAYMENT",
            summary="Payment memo verified against invoice purchase order.",
            verifier_status="NEEDS_REVIEW",
            verifier_details_json=json.dumps({
                "status": "NEEDS_REVIEW",
                "passed": True,
                "issues": [],
                "checked_invariants": {
                    "tenant_isolation": True,
                    "target_entity_exists": True,
                    "evidence_references_authentic": True,
                    "facts_strictly_grounded": True,
                    "financial_capacity_safe": True,
                    "settlement_allocation_distinction": True,
                    "no_autonomous_financial_mutation": True,
                },
            }),
            result_json=json.dumps({
                "candidate_invoice_analysis": [
                    {
                        "invoice_id": inv.id,
                        "invoice_number": inv.invoice_number,
                        "customer_match": True,
                        "amount_match": True,
                    }
                ],
                "conflicting_evidence": [],
                "suggested_resolution": {
                    "resolution_type": "ALLOCATE_PAYMENT",
                    "suggested_invoice_ids": [inv.id],
                    "suggested_amount_cents": 75000,
                    "suggested_allocations": {inv.id: 75000},
                },
            }),
        )
        s.add(invg)
        s.commit()

    # API approval of legitimate NEEDS_REVIEW MUST SUCCEED
    resp = client.post(
        f"/admin/proposals/{prop.id}/approve",
        json={"reviewer": "controller@test.com", "notes": "Approved legitimate case", "tenant_id": "tenant_safe"},
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "APPROVED"
    assert len(data["allocation_ids"]) == 1

    # Verify allocation and audit event created
    with test_db.session() as s:
        alloc = s.get(AllocationRecord, data["allocation_ids"][0])
        assert alloc is not None
        assert alloc.amount_minor == 75000

        aud = s.scalars(select(AuditEventRecord).where(
            AuditEventRecord.event_id == data["audit_event_id"]
        )).first()
        assert aud is not None
        assert aud.event_type == "PROPOSAL_APPROVED"


# --------------------------------------------------------------------------
# Test 7: Unlinked investigations do not bypass normal checks (clean match works)
# --------------------------------------------------------------------------

def test_7_unlinked_proposals_approved_normally(test_db, client):
    """TEST 7:
    Proposals with NO linked investigations evaluate cleanly and can be approved normally.
    """
    with test_db.session() as s:
        pay, inv, prop, _ = _seed_base_entities(s, "tenant_safe", "clean_normal", amount_minor=30000)

    # API GET returns approval_eligible: True, verifier_status: VERIFIED
    resp_get = client.get(f"/admin/proposals/{prop.id}", headers=AUTH_HEADERS)
    assert resp_get.status_code == 200
    pdata = resp_get.json()
    assert pdata["verifier_status"] == "VERIFIED"
    assert pdata["approval_eligible"] is True
    assert pdata["approval_prohibited_reason"] is None

    # Approval succeeds
    resp_post = client.post(
        f"/admin/proposals/{prop.id}/approve",
        json={"reviewer": "controller@test.com", "notes": "Approved clean match", "tenant_id": "tenant_safe"},
        headers=AUTH_HEADERS,
    )
    assert resp_post.status_code == 200
    assert resp_post.json()["status"] == "APPROVED"


# --------------------------------------------------------------------------
# Test 8: Cross-tenant isolation
# --------------------------------------------------------------------------

def test_8_cross_tenant_investigation_isolation(test_db, client):
    """TEST 8:
    An INVALID investigation belonging to Tenant B cannot influence or block a proposal belonging to Tenant A.
    """
    with test_db.session() as s:
        # Tenant A: normal clean proposal
        pay_a, inv_a, prop_a, _ = _seed_base_entities(s, "tenant_safe", "tenant_a_prop", amount_minor=40000)

        # Tenant B: INVALID investigation with same provider_payment_id pattern
        invg_b = InvestigationRecord(
            id="invg_tenant_b_adversary",
            tenant_id="tenant_other",  # DIFFERENT TENANT!
            target_type="payment",
            target_id=pay_a.provider_payment_id,  # Colliding payment id across tenants
            status="INVALID",
            classification="PROMPT_INJECTION_SUSPECT",
            verifier_status="INVALID",
            result_json=json.dumps({"risk_flags": ["PROMPT_INJECTION_DETECTED"]}),
            verifier_details_json=json.dumps({"status": "INVALID", "passed": False}),
        )
        s.add(invg_b)
        s.commit()

    # Tenant A's proposal MUST remain approval_eligible=True and approve successfully
    resp_get = client.get(f"/admin/proposals/{prop_a.id}?tenant_id=tenant_safe", headers=AUTH_HEADERS)
    assert resp_get.status_code == 200
    assert resp_get.json()["verifier_status"] == "VERIFIED"
    assert resp_get.json()["approval_eligible"] is True

    resp_post = client.post(
        f"/admin/proposals/{prop_a.id}/approve",
        json={"reviewer": "controller@test.com", "notes": "Approved tenant A", "tenant_id": "tenant_safe"},
        headers=AUTH_HEADERS,
    )
    assert resp_post.status_code == 200
    assert resp_post.json()["status"] == "APPROVED"


# --------------------------------------------------------------------------
# Test 9: Frontend/API surfaces effective verifier status & prohibited reason
# --------------------------------------------------------------------------

def test_9_api_surfaces_investigation_verdicts_and_disabled_reasons(test_db, client):
    """TEST 9:
    Review Queue API returns:
    - INJECTION: verifier_status="INVALID", approval_eligible=False, PROMPT_INJECTION_SUSPECT in classification.
    - REMITTER MISMATCH: verifier_status="NEEDS_REVIEW", approval_eligible=False, customer mismatch reason.
    - Evidence list includes [CRITICAL] and [UNRESOLVED CONFLICT] alerts.
    """
    with test_db.session() as s:
        # 1. Injection record
        pay_inj, _, prop_inj, _ = _seed_base_entities(s, "tenant_safe", "prop_ui_inj")
        invg_inj = InvestigationRecord(
            id="invg_ui_inj",
            tenant_id="tenant_safe",
            target_type="payment",
            target_id=pay_inj.provider_payment_id,
            status="INVALID",
            classification="PROMPT_INJECTION_SUSPECT",
            verifier_status="INVALID",
            summary="Injection tokens in remittance.",
            result_json=json.dumps({"risk_flags": ["PROMPT_INJECTION_DETECTED"]}),
            verifier_details_json=json.dumps({"status": "INVALID", "passed": False}),
        )

        # 2. Mismatch record
        pay_mis, inv_mis, prop_mis, _ = _seed_base_entities(s, "tenant_safe", "prop_ui_mis", payer_name="Global Clearinghouse LLC")
        inv_mis.customer_name = "Cyberdyne Corp"
        invg_mis = InvestigationRecord(
            id="invg_ui_mis",
            tenant_id="tenant_safe",
            target_type="payment",
            target_id=pay_mis.provider_payment_id,
            status="NEEDS_REVIEW",
            classification="UNMATCHED_PAYMENT",
            summary="Third-party clearinghouse remitter.",
            verifier_status="NEEDS_REVIEW",
            verifier_details_json=json.dumps({
                "status": "NEEDS_REVIEW",
                "passed": True,
                "issues": [],
                "checked_invariants": {
                    "tenant_isolation": True,
                    "target_entity_exists": True,
                    "evidence_references_authentic": True,
                    "facts_strictly_grounded": True,
                    "financial_capacity_safe": True,
                    "settlement_allocation_distinction": True,
                    "no_autonomous_financial_mutation": True,
                },
            }),
            result_json=json.dumps({
                "candidate_invoice_analysis": [{"invoice_id": inv_mis.id, "customer_match": False}],
                "suggested_resolution": {
                    "resolution_type": "ALLOCATE_PAYMENT",
                    "suggested_amount_cents": 50000,
                },
            }),
        )

        s.add_all([invg_inj, invg_mis])
        s.commit()

    resp_list = client.get("/admin/proposals?tenant_id=tenant_safe", headers=AUTH_HEADERS)
    assert resp_list.status_code == 200
    props = {p["id"]: p for p in resp_list.json()["proposals"]}

    # Verify INJECTION item
    p_inj = props[prop_inj.id]
    assert p_inj["verifier_status"] == "INVALID"
    assert p_inj["approval_eligible"] is False
    assert "Adversarial prompt injection tokens detected" in p_inj["approval_prohibited_reason"]
    assert any("[CRITICAL]" in e for e in p_inj["evidence"])

    # Verify MISMATCH item
    p_mis = props[prop_mis.id]
    assert p_mis["verifier_status"] == "NEEDS_REVIEW"
    assert p_mis["approval_eligible"] is False
    assert "Unresolved customer mismatch" in p_mis["approval_prohibited_reason"]
    assert any("[UNRESOLVED CONFLICT]" in e for e in p_mis["evidence"])


# --------------------------------------------------------------------------
# Test 10: Existing exact, split, and overpayment proposals continue to work
# --------------------------------------------------------------------------

def test_10_existing_exact_split_and_overpayment_proposals_work(test_db, client):
    """TEST 10:
    Ensure non-regressive behavior for:
    - Exact match (EXACT-001)
    - Split allocation across multiple invoices (SPLIT-005)
    - Overpayment with unapplied cash remainder (OVER-006)
    """
    now = datetime.now(timezone.utc)
    today = date(2026, 11, 9)

    with test_db.session() as s:
        # 1. Exact match ($1,250)
        pay_exact, inv_exact, prop_exact, _ = _seed_base_entities(s, "tenant_safe", "exact_001", amount_minor=125000)

        # 2. Split payment ($1,500 across $900 + $600)
        pay_split = PaymentRecord(
            id="pay_split_005",
            tenant_id="tenant_safe",
            provider="paypal",
            provider_payment_id="PAY-SPLIT-005",
            amount_minor=150000,
            currency="USD",
            status="COMPLETED",
            reference="INV-SPLIT-1, INV-SPLIT-2",
            note="Settlement for INV-SPLIT-1 and INV-SPLIT-2",
            received_at=now,
        )
        inv_split_1 = InvoiceRecord(
            id="inv_split_1",
            tenant_id="tenant_safe",
            provider="paypal",
            provider_invoice_id="INV-SPLIT-1",
            invoice_number="INV-SPLIT-1",
            customer_name="Split Customer",
            total_minor=90000,
            currency="USD",
            status="SENT",
            issued_on=today,
            due_on=today,
        )
        inv_split_2 = InvoiceRecord(
            id="inv_split_2",
            tenant_id="tenant_safe",
            provider="paypal",
            provider_invoice_id="INV-SPLIT-2",
            invoice_number="INV-SPLIT-2",
            customer_name="Split Customer",
            total_minor=60000,
            currency="USD",
            status="SENT",
            issued_on=today,
            due_on=today,
        )
        run_split = ReconciliationRunRecord(
            id="run_split_005",
            tenant_id="tenant_safe",
            input_snapshot_id="snap_split_005",
            matcher_name="deterministic_v2",
            matcher_version="2.0.0",
            run_at=now,
        )
        s.add_all([pay_split, inv_split_1, inv_split_2, run_split])
        s.flush()

        prop_split = ProposalRecord(
            id="prop_split_005",
            tenant_id="tenant_safe",
            run_id=run_split.id,
            payment_id=pay_split.id,
            outcome="PROPOSED",
            verifier_status="VERIFIED",
            evidence_json=json.dumps(["REFERENCE_MATCH", "SPLIT_ALLOCATION"]),
            created_at=now,
        )
        s.add(prop_split)
        s.flush()
        s.add_all([
            ProposalAllocationRecord(
                id="pa_split_1",
                tenant_id="tenant_safe",
                proposal_id=prop_split.id,
                payment_id=pay_split.id,
                invoice_id=inv_split_1.id,
                amount_minor=90000,
                currency="USD",
            ),
            ProposalAllocationRecord(
                id="pa_split_2",
                tenant_id="tenant_safe",
                proposal_id=prop_split.id,
                payment_id=pay_split.id,
                invoice_id=inv_split_2.id,
                amount_minor=60000,
                currency="USD",
            ),
        ])

        # 3. Overpayment ($2,000 against $1,500 invoice, $500 unapplied)
        pay_over = PaymentRecord(
            id="pay_over_006",
            tenant_id="tenant_safe",
            provider="paypal",
            provider_payment_id="PAY-OVER-006",
            amount_minor=200000,
            currency="USD",
            status="COMPLETED",
            reference="INV-OVER-006",
            received_at=now,
        )
        inv_over = InvoiceRecord(
            id="inv_over_006",
            tenant_id="tenant_safe",
            provider="paypal",
            provider_invoice_id="INV-OVER-006",
            invoice_number="INV-OVER-006",
            customer_name="Over Customer",
            total_minor=150000,
            currency="USD",
            status="SENT",
            issued_on=today,
            due_on=today,
        )
        run_over = ReconciliationRunRecord(
            id="run_over_006",
            tenant_id="tenant_safe",
            input_snapshot_id="snap_over_006",
            matcher_name="deterministic_v2",
            matcher_version="2.0.0",
            run_at=now,
        )
        s.add_all([pay_over, inv_over, run_over])
        s.flush()

        prop_over = ProposalRecord(
            id="prop_over_006",
            tenant_id="tenant_safe",
            run_id=run_over.id,
            payment_id=pay_over.id,
            outcome="PROPOSED",
            verifier_status="VERIFIED",
            excess_amount_minor=50000,
            evidence_json=json.dumps(["REFERENCE_MATCH", "AMOUNT_OVERPAYMENT"]),
            created_at=now,
        )
        s.add(prop_over)
        s.flush()
        s.add(ProposalAllocationRecord(
            id="pa_over_1",
            tenant_id="tenant_safe",
            proposal_id=prop_over.id,
            payment_id=pay_over.id,
            invoice_id=inv_over.id,
            amount_minor=150000,
            currency="USD",
        ))

        s.commit()

    # 1. Approve exact match
    res_exact = client.post(
        f"/admin/proposals/{prop_exact.id}/approve",
        json={"reviewer": "controller@test.com", "notes": "Approved exact match", "tenant_id": "tenant_safe"},
        headers=AUTH_HEADERS,
    )
    assert res_exact.status_code == 200
    assert res_exact.json()["status"] == "APPROVED"

    # 2. Approve split payment
    res_split = client.post(
        f"/admin/proposals/{prop_split.id}/approve",
        json={"reviewer": "controller@test.com", "notes": "Approved split", "tenant_id": "tenant_safe"},
        headers=AUTH_HEADERS,
    )
    assert res_split.status_code == 200
    assert res_split.json()["status"] == "APPROVED"
    assert len(res_split.json()["allocation_ids"]) == 2

    # 3. Approve overpayment
    res_over = client.post(
        f"/admin/proposals/{prop_over.id}/approve",
        json={"reviewer": "controller@test.com", "notes": "Approved overpayment", "tenant_id": "tenant_safe"},
        headers=AUTH_HEADERS,
    )
    assert res_over.status_code == 200
    assert res_over.json()["status"] == "APPROVED"

"""Tests for Milestone D7: Aligning Approval with D6 Verifier Semantics.

Mandatory Verification Suite:
1. Genuine D6-style NEEDS_REVIEW investigation with passed=True, all invariants true,
   no blocking risk flags, and matching deterministic proposal: approval succeeds through ApprovalService.
2. NEEDS_REVIEW with passed=False: approval rejected.
3. NEEDS_REVIEW with a failed checked invariant: approval rejected.
4. NEEDS_REVIEW with prompt-injection risk flag: approval rejected.
5. INVALID investigation: approval rejected.
6. UNVERIFIED investigation: approval rejected.
7. Missing verifier_details: approval rejected.
8. Suggested allocation differing from deterministic proposal: approval rejected.
9. All failed cases produce zero financial mutations.
10. Successful approval produces exactly the expected allocations and audit events.
"""

from __future__ import annotations

import json
import pytest
from fastapi.testclient import TestClient

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
)
from reconcile_store.repositories.tenant_repo import TenantRepository
from reconcile_store.repositories.payment_repo import PaymentRepository
from reconcile_store.repositories.invoice_repo import InvoiceRepository
from reconcile_store.repositories.investigation_repo import InvestigationRepository
from reconcile_store.repositories.reconciliation_repo import (
    ProposalRepository,
    ReconciliationRunRepository,
)
from reconcile_investigator.verifier import IndependentSafetyVerifier
from reconcile_investigator.models import (
    InvestigationResult,
    InvestigationClassification,
    VerifierStatus,
    VerifiedFact,
    EvidenceReference,
    CandidateAnalysis,
    Hypothesis,
    LikelihoodLevel,
    SuggestedResolution,
)

ADMIN_KEY = "test-hardening-admin-key-9999"
AUTH_HEADERS = {"Authorization": f"Bearer {ADMIN_KEY}"}


@pytest.fixture
def test_db():
    engine = DatabaseEngine(DatabaseConfig(url="sqlite:///:memory:"))
    Base.metadata.create_all(engine.engine)
    set_db_engine(engine)
    with engine.session() as s:
        TenantRepository(s).create_tenant("demo_merchant", "Demo Merchant")
        s.commit()
    yield engine
    set_db_engine(None)


@pytest.fixture
def client(test_db, monkeypatch):
    monkeypatch.setenv("ADMIN_API_KEY", ADMIN_KEY)
    monkeypatch.setenv("DEFAULT_TENANT_ID", "demo_merchant")
    return TestClient(app)


def _setup_positive_case(test_db):
    """Setup a payment, invoice, and deterministic proposal, returning their IDs."""
    with test_db.session() as s:
        p_repo = PaymentRepository(s)
        i_repo = InvoiceRepository(s)
        r_repo = ReconciliationRunRepository(s)
        prop_repo = ProposalRepository(s)

        pay = p_repo.upsert_payment(
            tenant_id="demo_merchant",
            provider="paypal",
            provider_payment_id="PAY-D6-REAL-1",
            amount_minor=10000,
            currency="USD",
            status="COMPLETED",
            payer_name="Acme Corp Remitter",
            reference="INV-D6-REAL-1",
        )
        inv = i_repo.upsert_invoice(
            tenant_id="demo_merchant",
            provider="paypal",
            provider_invoice_id="INV-D6-REAL-1",
            invoice_number="INV-D6-REAL-1",
            total_minor=10000,
            currency="USD",
            provider_due_minor=None,
            status="SENT",
        )
        run = r_repo.create_run("demo_merchant", "deterministic_v2", "2.0.0")
        prop = prop_repo.create_proposal(
            tenant_id="demo_merchant",
            run_id=run.id,
            payment_id=pay.id,
            outcome="PROPOSED",
            evidence=["REFERENCE_MATCH", "AMOUNT_EXACT"],
            allocations=[{"invoice_id": inv.id, "amount_minor": 10000, "currency": "USD"}],
        )
        s.commit()
        return pay.id, pay.provider_payment_id, inv.id, inv.provider_invoice_id, prop.id


# ==============================================================================
# MANDATORY TEST 1 & 10: Genuine D6 NEEDS_REVIEW with real IndependentSafetyVerifier
# ==============================================================================

def test_mandatory_1_and_10_genuine_d6_needs_review_approval_positive_path(client, test_db):
    """MANDATORY TESTS 1 & 10:
    1. Genuine D6-style NEEDS_REVIEW investigation with passed=True, all invariants true,
       no blocking risk flags, and matching deterministic proposal: approval succeeds through ApprovalService.
    10. Successful approval produces exactly the expected allocations and audit events.
    Uses real D6 IndependentSafetyVerifier rather than manually fabricating a VERIFIED record.
    """
    pay_id, pay_prov_id, inv_id, inv_prov_id, prop_id = _setup_positive_case(test_db)

    with test_db.session() as s:
        # 1. Run real D6 IndependentSafetyVerifier
        verifier = IndependentSafetyVerifier(s, "demo_merchant")

        inv_result = InvestigationResult(
            investigation_id="invg_real_positive_1",
            tenant_id="demo_merchant",
            target_type="payment",
            target_id=pay_id,
            classification=InvestigationClassification.UNMATCHED_PAYMENT,
            executive_summary="Genuine D6 exception investigated and verified.",
            verified_facts=[
                VerifiedFact(
                    statement="Payment PAY-D6-REAL-1 has unapplied cash of $100.00 USD.",
                    evidence_ids=[f"ev_pay_{pay_id}"],
                    amount_cents=10000,
                    currency="USD",
                )
            ],
            supporting_evidence=[
                EvidenceReference(
                    evidence_id=f"ev_pay_{pay_id}",
                    source_type="payment",
                    source_id=pay_id,
                    tenant_id="demo_merchant",
                    description="Authoritative payment record",
                    amount_cents=10000,
                    currency="USD",
                )
            ],
            candidate_invoice_analysis=[
                CandidateAnalysis(
                    invoice_id=inv_id,
                    invoice_number="INV-D6-REAL-1",
                    customer_match=True,
                    amount_match=True,
                    score=95,
                    assessment="Exact amount and reference match",
                )
            ],
            hypotheses=[
                Hypothesis(
                    hypothesis_id="hyp_1",
                    statement="Remitter cited invoice number in reference field",
                    likelihood=LikelihoodLevel.HIGH,
                    supporting_evidence_ids=[f"ev_pay_{pay_id}"],
                )
            ],
            suggested_resolution=SuggestedResolution(
                resolution_type="ALLOCATE_PAYMENT",
                suggested_invoice_ids=[inv_prov_id],
                suggested_amount_cents=10000,
                suggested_allocations={inv_prov_id: 10000},
                notes="Allocate $100.00 to INV-D6-REAL-1",
            ),
            risk_flags=[],
            requires_human_review=True,
        )

        ver_res = verifier.verify_investigation(inv_result)

        # Assert D6 verifier semantics: suggested resolution causes status=NEEDS_REVIEW with passed=True
        assert ver_res.status == VerifierStatus.NEEDS_REVIEW
        assert ver_res.passed is True
        assert len(ver_res.issues) == 0
        assert all(ver_res.checked_invariants.values()) is True
        assert ver_res.checked_invariants["tenant_isolation"] is True
        assert ver_res.checked_invariants["financial_capacity_safe"] is True

        # Persist investigation
        inv_repo = InvestigationRepository(s)
        inv_rec = inv_repo.create_investigation(
            tenant_id="demo_merchant",
            target_type="payment",
            target_id=pay_prov_id,
            status="NEEDS_REVIEW",
            model_provider="ollama",
            model_name="qwen3:8b",
            prompt_version="d6.v1",
        )
        inv_repo.record_investigation_result(
            tenant_id="demo_merchant",
            investigation_id=inv_rec.id,
            status="NEEDS_REVIEW",
            classification=inv_result.classification.value,
            summary=inv_result.executive_summary,
            result_dict=inv_result.model_dump(mode="json"),
            evidence_references=[e.model_dump(mode="json") for e in inv_result.supporting_evidence],
            verifier_status=ver_res.status.value,
            verifier_details=ver_res.to_dict(),
            model_provider="ollama",
            model_name="qwen3:8b",
        )
        s.commit()
        inv_case_id = inv_rec.id

    # 2. Call approval endpoint as authorized operator
    resp = client.post(
        f"/admin/investigations/{inv_case_id}/approve",
        json={
            "reviewer": "lead_controller@bank.com",
            "notes": "Approved based on authoritative D6 verification",
            "tenant_id": "demo_merchant",
            "proposal_id": prop_id,
        },
        headers=AUTH_HEADERS,
    )

    # 3. Verify API response
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "APPROVED"
    assert data["investigation_id"] == inv_case_id
    assert data["proposal_id"] == prop_id
    assert len(data["allocation_ids"]) == 1
    assert "audit_event_id" in data

    # 4. Mandatory Test 10: Verify database state, allocations and audit events
    with test_db.session() as s:
        # Investigation record updated to APPROVED
        inv_db = s.get(InvestigationRecord, inv_case_id)
        assert inv_db.status == "APPROVED"

        # Proposal decision updated to APPROVED
        decs = s.query(DecisionRecord).filter_by(proposal_id=prop_id).all()
        assert len(decs) == 1
        assert decs[0].decision_type == "APPROVED"
        assert decs[0].actor == "lead_controller@bank.com"

        # Financial ledger mutation executed atomically through ApprovalService
        allocs = s.query(AllocationRecord).filter_by(tenant_id="demo_merchant").all()
        assert len(allocs) == 1
        alloc = allocs[0]
        assert alloc.amount_minor == 10000
        assert alloc.currency == "USD"
        assert alloc.decision_id == decs[0].id
        assert alloc.actor == "lead_controller@bank.com"
        assert alloc.payment_id == pay_id
        assert alloc.invoice_id == inv_id

        # Authoritative audit event appended
        audit = s.query(AuditEventRecord).filter_by(
            tenant_id="demo_merchant",
            event_type="INVESTIGATION_APPROVED",
        ).first()
        assert audit is not None
        assert audit.actor == "lead_controller@bank.com"
        assert audit.subject_id == inv_case_id
        audit_payload = json.loads(audit.payload_json)
        assert audit_payload["details"]["reviewer_identity_type"] == "operator_supplied_label"
        assert audit_payload["details"]["proposal_id"] == prop_id


# ==============================================================================
# MANDATORY TEST 2: NEEDS_REVIEW with passed=False: approval rejected
# ==============================================================================

def test_mandatory_2_needs_review_passed_false_rejected(client, test_db):
    """MANDATORY TEST 2:
    NEEDS_REVIEW with passed=False: approval rejected with 400 Bad Request.
    """
    pay_id, pay_prov_id, inv_id, inv_prov_id, prop_id = _setup_positive_case(test_db)

    with test_db.session() as s:
        inv_repo = InvestigationRepository(s)
        inv = inv_repo.create_investigation("demo_merchant", "payment", pay_prov_id, status="NEEDS_REVIEW")
        inv_repo.record_investigation_result(
            tenant_id="demo_merchant",
            investigation_id=inv.id,
            status="NEEDS_REVIEW",
            classification="UNMATCHED_PAYMENT",
            summary="Test passed=False",
            result_dict={
                "suggested_resolution": {
                    "resolution_type": "ALLOCATE_PAYMENT",
                    "suggested_invoice_ids": [inv_prov_id],
                    "suggested_amount_cents": 10000,
                }
            },
            evidence_references=[],
            verifier_status="NEEDS_REVIEW",
            verifier_details={
                "status": "NEEDS_REVIEW",
                "passed": False,  # FAIL
                "issues": ["Invariant check failure"],
                "checked_invariants": {
                    "tenant_isolation": True,
                    "target_entity_exists": True,
                    "evidence_references_authentic": True,
                    "facts_strictly_grounded": True,
                    "financial_capacity_safe": False,
                    "settlement_allocation_distinction": True,
                    "no_autonomous_financial_mutation": True,
                },
            },
        )
        s.commit()
        inv_id_test = inv.id

    resp = client.post(
        f"/admin/investigations/{inv_id_test}/approve",
        json={"reviewer": "op@bank.com", "tenant_id": "demo_merchant", "proposal_id": prop_id},
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 400
    assert "passed" in resp.json()["detail"].lower()

    # Zero financial mutations
    with test_db.session() as s:
        assert s.query(AllocationRecord).count() == 0


# ==============================================================================
# MANDATORY TEST 3: NEEDS_REVIEW with failed checked invariant: approval rejected
# ==============================================================================

def test_mandatory_3_needs_review_failed_invariant_rejected(client, test_db):
    """MANDATORY TEST 3:
    NEEDS_REVIEW with a failed checked invariant: approval rejected with 400 Bad Request.
    """
    pay_id, pay_prov_id, inv_id, inv_prov_id, prop_id = _setup_positive_case(test_db)

    with test_db.session() as s:
        inv_repo = InvestigationRepository(s)
        inv = inv_repo.create_investigation("demo_merchant", "payment", pay_prov_id, status="NEEDS_REVIEW")
        inv_repo.record_investigation_result(
            tenant_id="demo_merchant",
            investigation_id=inv.id,
            status="NEEDS_REVIEW",
            classification="UNMATCHED_PAYMENT",
            summary="Test failed invariant",
            result_dict={
                "suggested_resolution": {
                    "resolution_type": "ALLOCATE_PAYMENT",
                    "suggested_invoice_ids": [inv_prov_id],
                    "suggested_amount_cents": 10000,
                }
            },
            evidence_references=[],
            verifier_status="NEEDS_REVIEW",
            verifier_details={
                "status": "NEEDS_REVIEW",
                "passed": True,
                "issues": [],
                "checked_invariants": {
                    "tenant_isolation": True,
                    "target_entity_exists": True,
                    "evidence_references_authentic": True,
                    "facts_strictly_grounded": True,
                    "financial_capacity_safe": False,  # FAILED INVARIANT
                    "settlement_allocation_distinction": True,
                    "no_autonomous_financial_mutation": True,
                },
            },
        )
        s.commit()
        inv_id_test = inv.id

    resp = client.post(
        f"/admin/investigations/{inv_id_test}/approve",
        json={"reviewer": "op@bank.com", "tenant_id": "demo_merchant", "proposal_id": prop_id},
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 400
    assert "invariants failed" in resp.json()["detail"].lower()

    with test_db.session() as s:
        assert s.query(AllocationRecord).count() == 0


# ==============================================================================
# MANDATORY TEST 4: NEEDS_REVIEW with prompt-injection risk flag: approval rejected
# ==============================================================================

def test_mandatory_4_needs_review_prompt_injection_flag_rejected(client, test_db):
    """MANDATORY TEST 4:
    NEEDS_REVIEW with prompt-injection risk flag: approval rejected with 400 Bad Request.
    """
    pay_id, pay_prov_id, inv_id, inv_prov_id, prop_id = _setup_positive_case(test_db)

    with test_db.session() as s:
        inv_repo = InvestigationRepository(s)
        inv = inv_repo.create_investigation("demo_merchant", "payment", pay_prov_id, status="NEEDS_REVIEW")
        inv_repo.record_investigation_result(
            tenant_id="demo_merchant",
            investigation_id=inv.id,
            status="NEEDS_REVIEW",
            classification="PROMPT_INJECTION_SUSPECT",
            summary="Suspicious memo injection payload",
            result_dict={
                "risk_flags": ["PROMPT_INJECTION_DETECTED"],
                "suggested_resolution": {
                    "resolution_type": "ALLOCATE_PAYMENT",
                    "suggested_invoice_ids": [inv_prov_id],
                    "suggested_amount_cents": 10000,
                },
            },
            evidence_references=[],
            verifier_status="NEEDS_REVIEW",
            verifier_details={
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
            },
        )
        s.commit()
        inv_id_test = inv.id

    resp = client.post(
        f"/admin/investigations/{inv_id_test}/approve",
        json={"reviewer": "op@bank.com", "tenant_id": "demo_merchant", "proposal_id": prop_id},
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 400
    assert "blocking" in resp.json()["detail"].lower() or "risk flag" in resp.json()["detail"].lower()

    with test_db.session() as s:
        assert s.query(AllocationRecord).count() == 0


# ==============================================================================
# MANDATORY TEST 5: INVALID investigation: approval rejected
# ==============================================================================

def test_mandatory_5_invalid_investigation_rejected(client, test_db):
    """MANDATORY TEST 5:
    INVALID investigation: approval rejected with 400 Bad Request.
    """
    pay_id, pay_prov_id, inv_id, inv_prov_id, prop_id = _setup_positive_case(test_db)

    with test_db.session() as s:
        inv_repo = InvestigationRepository(s)
        inv = inv_repo.create_investigation("demo_merchant", "payment", pay_prov_id, status="INVALID")
        inv_repo.record_investigation_result(
            tenant_id="demo_merchant",
            investigation_id=inv.id,
            status="INVALID",
            classification="PROMPT_INJECTION_SUSPECT",
            summary="Invalid safety verdict",
            result_dict={
                "suggested_resolution": {
                    "resolution_type": "ALLOCATE_PAYMENT",
                    "suggested_invoice_ids": [inv_prov_id],
                    "suggested_amount_cents": 10000,
                }
            },
            evidence_references=[],
            verifier_status="INVALID",
            verifier_details={"passed": False, "issues": ["Fatal security check failed"]},
        )
        s.commit()
        inv_id_test = inv.id

    resp = client.post(
        f"/admin/investigations/{inv_id_test}/approve",
        json={"reviewer": "op@bank.com", "tenant_id": "demo_merchant", "proposal_id": prop_id},
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 400
    assert "INVALID" in resp.json()["detail"]
    assert "strictly blocked" in resp.json()["detail"].lower()

    with test_db.session() as s:
        assert s.query(AllocationRecord).count() == 0


# ==============================================================================
# MANDATORY TEST 6: UNVERIFIED investigation: approval rejected
# ==============================================================================

def test_mandatory_6_unverified_investigation_rejected(client, test_db):
    """MANDATORY TEST 6:
    UNVERIFIED investigation: approval rejected with 400 Bad Request.
    """
    pay_id, pay_prov_id, inv_id, inv_prov_id, prop_id = _setup_positive_case(test_db)

    for unv_status in ["UNVERIFIED", "NOT_VERIFIED"]:
        with test_db.session() as s:
            inv_repo = InvestigationRepository(s)
            inv = inv_repo.create_investigation("demo_merchant", "payment", pay_prov_id, status="NEEDS_REVIEW")
            inv.verifier_status = unv_status
            inv.result_json = json.dumps({
                "suggested_resolution": {
                    "resolution_type": "ALLOCATE_PAYMENT",
                    "suggested_invoice_ids": [inv_prov_id],
                    "suggested_amount_cents": 10000,
                }
            })
            s.commit()
            inv_id_test = inv.id

        resp = client.post(
            f"/admin/investigations/{inv_id_test}/approve",
            json={"reviewer": "op@bank.com", "tenant_id": "demo_merchant", "proposal_id": prop_id},
            headers=AUTH_HEADERS,
        )
        assert resp.status_code == 400
        assert "unverified" in resp.json()["detail"].lower()

    with test_db.session() as s:
        assert s.query(AllocationRecord).count() == 0


# ==============================================================================
# MANDATORY TEST 7: Missing verifier_details: approval rejected
# ==============================================================================

def test_mandatory_7_missing_verifier_details_rejected(client, test_db):
    """MANDATORY TEST 7:
    Missing or malformed verifier_details: approval rejected with 400 Bad Request.
    """
    pay_id, pay_prov_id, inv_id, inv_prov_id, prop_id = _setup_positive_case(test_db)

    with test_db.session() as s:
        inv_repo = InvestigationRepository(s)
        inv = inv_repo.create_investigation("demo_merchant", "payment", pay_prov_id, status="NEEDS_REVIEW")
        inv.verifier_status = "NEEDS_REVIEW"
        inv.verifier_details_json = "{}"  # Missing verifier details!
        inv.result_json = json.dumps({
            "suggested_resolution": {
                "resolution_type": "ALLOCATE_PAYMENT",
                "suggested_invoice_ids": [inv_prov_id],
                "suggested_amount_cents": 10000,
            }
        })
        s.commit()
        inv_id_test = inv.id

    resp = client.post(
        f"/admin/investigations/{inv_id_test}/approve",
        json={"reviewer": "op@bank.com", "tenant_id": "demo_merchant", "proposal_id": prop_id},
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 400
    assert "missing or malformed verifier details" in resp.json()["detail"].lower()

    with test_db.session() as s:
        assert s.query(AllocationRecord).count() == 0


# ==============================================================================
# MANDATORY TEST 8: Suggested allocation differing from deterministic proposal: rejected
# ==============================================================================

def test_mandatory_8_suggested_allocation_differs_from_proposal_rejected(client, test_db):
    """MANDATORY TEST 8:
    Suggested allocation differing from deterministic proposal: approval rejected with 400 Bad Request.
    """
    pay_id, pay_prov_id, inv_id, inv_prov_id, prop_id = _setup_positive_case(test_db)

    with test_db.session() as s:
        inv_repo = InvestigationRepository(s)
        inv = inv_repo.create_investigation("demo_merchant", "payment", pay_prov_id, status="NEEDS_REVIEW")
        inv_repo.record_investigation_result(
            tenant_id="demo_merchant",
            investigation_id=inv.id,
            status="NEEDS_REVIEW",
            classification="UNMATCHED_PAYMENT",
            summary="Suggested amount contradicts deterministic proposal (8000 vs 10000)",
            result_dict={
                "suggested_resolution": {
                    "resolution_type": "ALLOCATE_PAYMENT",
                    "suggested_invoice_ids": [inv_prov_id],
                    "suggested_amount_cents": 8000,  # Differs from proposal's 10000!
                }
            },
            evidence_references=[],
            verifier_status="NEEDS_REVIEW",
            verifier_details={
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
            },
        )
        s.commit()
        inv_id_test = inv.id

    resp = client.post(
        f"/admin/investigations/{inv_id_test}/approve",
        json={"reviewer": "op@bank.com", "tenant_id": "demo_merchant", "proposal_id": prop_id},
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 400
    assert "do not match recommendation allocations" in resp.json()["detail"].lower()

    with test_db.session() as s:
        assert s.query(AllocationRecord).count() == 0


# ==============================================================================
# MANDATORY TEST 9: All failed cases produce zero financial mutations
# ==============================================================================

def test_mandatory_9_all_failed_cases_produce_zero_financial_mutations(client, test_db):
    """MANDATORY TEST 9:
    Assert across all safety rejection scenarios that zero financial rows or ledger allocations are committed.
    """
    pay_id, pay_prov_id, inv_id, inv_prov_id, prop_id = _setup_positive_case(test_db)

    # 1. Invalid verifier status
    with test_db.session() as s:
        inv = InvestigationRepository(s).create_investigation("demo_merchant", "payment", pay_prov_id, status="NEEDS_REVIEW")
        inv.verifier_status = "INVALID"
        s.commit()
        inv1_id = inv.id

    # 2. Currency mismatch risk flag
    with test_db.session() as s:
        inv = InvestigationRepository(s).create_investigation("demo_merchant", "payment", pay_prov_id, status="NEEDS_REVIEW")
        InvestigationRepository(s).record_investigation_result(
            tenant_id="demo_merchant",
            investigation_id=inv.id,
            status="NEEDS_REVIEW",
            classification="CURRENCY_MISMATCH",
            summary="Currency mismatch",
            result_dict={"risk_flags": ["CURRENCY_MISMATCH"], "suggested_resolution": {"resolution_type": "ALLOCATE_PAYMENT", "suggested_invoice_ids": [inv_prov_id], "suggested_amount_cents": 10000}},
            evidence_references=[],
            verifier_status="NEEDS_REVIEW",
            verifier_details={"status": "NEEDS_REVIEW", "passed": True, "issues": [], "checked_invariants": {"tenant_isolation": True, "target_entity_exists": True, "evidence_references_authentic": True, "facts_strictly_grounded": True, "financial_capacity_safe": True, "settlement_allocation_distinction": True, "no_autonomous_financial_mutation": True}},
        )
        s.commit()
        inv2_id = inv.id

    # 3. Contradictory provider record flag
    with test_db.session() as s:
        inv = InvestigationRepository(s).create_investigation("demo_merchant", "payment", pay_prov_id, status="NEEDS_REVIEW")
        InvestigationRepository(s).record_investigation_result(
            tenant_id="demo_merchant",
            investigation_id=inv.id,
            status="NEEDS_REVIEW",
            classification="UNMATCHED_PAYMENT",
            summary="Contradictory provider record",
            result_dict={"risk_flags": ["CONTRADICTORY_PROVIDER_RECORD"], "suggested_resolution": {"resolution_type": "ALLOCATE_PAYMENT", "suggested_invoice_ids": [inv_prov_id], "suggested_amount_cents": 10000}},
            evidence_references=[],
            verifier_status="NEEDS_REVIEW",
            verifier_details={"status": "NEEDS_REVIEW", "passed": True, "issues": [], "checked_invariants": {"tenant_isolation": True, "target_entity_exists": True, "evidence_references_authentic": True, "facts_strictly_grounded": True, "financial_capacity_safe": True, "settlement_allocation_distinction": True, "no_autonomous_financial_mutation": True}},
        )
        s.commit()
        inv3_id = inv.id

    # Execute all failing calls
    for bad_id in [inv1_id, inv2_id, inv3_id]:
        r = client.post(
            f"/admin/investigations/{bad_id}/approve",
            json={"reviewer": "op@bank.com", "tenant_id": "demo_merchant", "proposal_id": prop_id},
            headers=AUTH_HEADERS,
        )
        assert r.status_code == 400

    # Invariant: ZERO allocations and ZERO decisions in database
    with test_db.session() as s:
        alloc_count = s.query(AllocationRecord).count()
        dec_count = s.query(DecisionRecord).count()
        assert alloc_count == 0, f"Expected 0 allocations, found {alloc_count}"
        assert dec_count == 0, f"Expected 0 decisions, found {dec_count}"

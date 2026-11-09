"""Regression coverage for ledger capacity, stale proposals and the approval gate."""

from __future__ import annotations

import os
import threading
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from hypothesis import given, settings
from hypothesis import strategies as st
from sqlalchemy import func, select

from reconcile.models import (
    AllocationStatus,
    Candidate,
    Evidence,
    EvidenceCode,
    InvoiceStatus,
    MatchOutcome,
    MatchProposal,
    PaymentAllocation,
    Refund,
)
from reconcile.verifier import verify_proposals
from reconcile_api.dependencies import set_db_engine
from reconcile_api.main import app
from reconcile_store.approval import ApprovalService, VerificationFailedError
from reconcile_store.config import DatabaseConfig
from reconcile_store.db import DatabaseEngine
from reconcile_store.repositories.base import ConflictError
from reconcile_store.repositories.invoice_repo import InvoiceRepository
from reconcile_store.repositories.payment_repo import PaymentRepository
from reconcile_store.repositories.reconciliation_repo import (
    ProposalRepository,
    ReconciliationRunRepository,
)
from reconcile_store.repositories.refund_repo import RefundRepository
from reconcile_store.repositories.settlement_repo import ProviderSettlementRepository
from reconcile_store.repositories.tenant_repo import TenantRepository
from reconcile_store.schema import AllocationRecord, AuditEventRecord, DecisionRecord
from reconcile_store.workflow import StorageWorkflow

from factories import inv, pay

T = "tenant_a"
NOW = datetime(2026, 1, 15, tzinfo=timezone.utc)


@pytest.fixture
def db(tmp_path):
    engine = DatabaseEngine(DatabaseConfig(url=f"sqlite:///{tmp_path / 'ledger.db'}"))
    engine.init_schema()
    with engine.session() as s:
        TenantRepository(s).create_tenant(T, "Tenant A")
        TenantRepository(s).create_tenant("tenant_b", "Tenant B")
    yield engine
    engine.dispose()


@pytest.fixture
def client(db):
    set_db_engine(db)
    with TestClient(app) as test_client:
        test_client.headers.update({"Authorization": f"Bearer {os.environ['ADMIN_API_KEY']}"})
        yield test_client
    set_db_engine(None)


def invoice(s, number: str, total: int, currency: str = "USD", tenant: str = T):
    return InvoiceRepository(s).upsert_invoice(
        tenant, "paypal", number, number, total, currency, customer_name="Acme",
        issued_on=NOW.date(), due_on=NOW.date(),
    )


def payment(s, pid: str, amount: int, reference: str, tenant: str = T, currency: str = "USD"):
    return PaymentRepository(s).upsert_payment(
        tenant, "paypal", pid, amount, currency, payer_name="Acme", reference=reference, received_at=NOW,
    )


def proposal(s, pay_rec, allocations, excess: int = 0, tenant: str = T, verifier_status: str = "VERIFIED"):
    run = ReconciliationRunRepository(s).create_run(tenant, "snapshot", "test")
    return ProposalRepository(s).create_proposal(
        tenant, run.id, pay_rec.id, "PROPOSED", evidence=["REFERENCE_MATCH"],
        verifier_status=verifier_status, excess_amount_minor=excess,
        allocations=[
            {"invoice_id": i.id, "amount_minor": a, "currency": pay_rec.currency} for i, a in allocations
        ],
    )


def refund(s, rid, pay_rec, amount, invoice_id=None, allocation_id=None, at=None, status="COMPLETED"):
    return RefundRepository(s).upsert_refund(
        T, "paypal", rid, pay_rec.id, amount, pay_rec.currency, invoice_id=invoice_id,
        allocation_id=allocation_id, refunded_at=at or datetime.now(timezone.utc), status=status,
    )


def reversal(s, rid, pay_rec, amount, invoice_id=None, allocation_id=None, at=None):
    return RefundRepository(s).upsert_reversal(
        T, "paypal", rid, pay_rec.id, amount, pay_rec.currency, invoice_id=invoice_id,
        allocation_id=allocation_id, reversed_at=at or datetime.now(timezone.utc),
    )


def approve(s, prop, key="key-1"):
    return ApprovalService(s).approve_proposal(T, prop.id, "operator", key)


def unapplied(s, pay_rec):
    return PaymentRepository(s).get_unapplied_cash_minor(T, pay_rec.id)


def outstanding(s, inv_rec):
    return InvoiceRepository(s).get_ledger_outstanding_minor(T, inv_rec.id)


def assert_no_financial_effect(s, prop):
    assert s.scalar(select(func.count()).select_from(AllocationRecord)) == 0
    assert s.scalar(
        select(func.count()).select_from(DecisionRecord).where(DecisionRecord.proposal_id == prop.id)
    ) == 0
    assert s.scalar(
        select(func.count()).select_from(AuditEventRecord).where(AuditEventRecord.event_type == "PROPOSAL_APPROVED")
    ) == 0


# --------------------------------------------------------------------------
# Stale proposals
# --------------------------------------------------------------------------


def test_fully_reversed_payment_cannot_approve_pending_proposal(db):
    with db.session() as s:
        i = invoice(s, "INV-1", 45000)
        p = payment(s, "P1", 45000, "INV-1")
        prop = proposal(s, p, [(i, 45000)])
        reversal(s, "R1", p, 45000, invoice_id=i.id)
        s.commit()

        with pytest.raises(VerificationFailedError, match="no remaining allocation capacity"):
            approve(s, prop)
        assert_no_financial_effect(s, prop)
        assert unapplied(s, p) == 0
        assert outstanding(s, i) == 45000


def test_partially_refunded_stale_proposal_is_rejected_not_reduced(db):
    with db.session() as s:
        i = invoice(s, "INV-1", 30000)
        p = payment(s, "P1", 30000, "INV-1")
        prop = proposal(s, p, [(i, 30000)])
        refund(s, "R1", p, 10000, invoice_id=i.id)
        s.commit()

        with pytest.raises(VerificationFailedError, match="20000 cents"):
            approve(s, prop)
        assert_no_financial_effect(s, prop)
        assert unapplied(s, p) == 20000


def test_regenerated_proposal_uses_net_and_is_not_double_reversed(db):
    """A return recorded before approval must not also reverse the later allocation."""
    with db.session() as s:
        i = invoice(s, "INV-1", 30000)
        p = payment(s, "P1", 30000, "INV-1")
        refund(s, "R1", p, 10000, invoice_id=i.id, at=NOW)
        s.commit()

        _, result = StorageWorkflow(s).run_reconciliation(T, matcher_name="advanced")
        assert result.proposed[0].candidates[0].total_cents == 20000
        assert result.passed_verification

        prop = ProposalRepository(s).list_by_payment(T, p.id)[0]
        approve(s, prop)
        assert unapplied(s, p) == 0
        assert outstanding(s, i) == 10000


def test_fully_reversed_payment_regenerates_as_no_match(db):
    with db.session() as s:
        i = invoice(s, "INV-1", 45000)
        p = payment(s, "P1", 45000, "INV-1")
        reversal(s, "R1", p, 45000, invoice_id=i.id)
        s.commit()

        _, result = StorageWorkflow(s).run_reconciliation(T, matcher_name="advanced")
        assert result.proposals[0].outcome is MatchOutcome.NO_MATCH
        assert result.proposals[0].reasons[0].code is EvidenceCode.PAYMENT_RETURNED


def test_generated_overpayment_proposal_keeps_evidence_and_approves(db):
    """The persisted evidence must let the approval gate re-verify what the matcher verified."""
    with db.session() as s:
        i = invoice(s, "INV-1", 50000)
        p = payment(s, "P1", 65000, "INV-1")
        s.commit()

        StorageWorkflow(s).run_reconciliation(T, matcher_name="advanced")
        prop = ProposalRepository(s).list_by_payment(T, p.id)[0]
        assert prop.verifier_status == "VERIFIED"
        approve(s, prop)
        assert unapplied(s, p) == 15000
        assert outstanding(s, i) == 0


def test_domain_verifier_rejects_gross_proposal_after_refund():
    invoice_ = inv(1, 30000)
    payment_ = pay(1, 30000, reference=invoice_.number)
    candidate = Candidate(
        (PaymentAllocation(payment_.payment_id, invoice_.invoice_id, 30000, AllocationStatus.PROPOSED, "USD"),),
        (Evidence(EvidenceCode.REFERENCE_MATCH),),
    )
    proposal_ = MatchProposal(payment_.payment_id, MatchOutcome.PROPOSED, (candidate,))
    returned = Refund("R1", payment_.payment_id, 10000, "USD", NOW)

    assert verify_proposals([proposal_], [payment_], [invoice_])[0].passed
    result = verify_proposals([proposal_], [payment_], [invoice_], refunds=[returned])[0]
    assert not result.passed
    assert "20000 cents" in " ".join(result.issues)


# --------------------------------------------------------------------------
# Ledger semantics
# --------------------------------------------------------------------------


def allocated(s, number: str, pid: str, pay_amount: int, alloc_amount: int, invoice_total: int):
    i = invoice(s, number, invoice_total)
    p = payment(s, pid, pay_amount, number)
    prop = proposal(s, p, [(i, alloc_amount)], excess=pay_amount - alloc_amount)
    result = approve(s, prop, key=f"key-{pid}")
    return i, p, result.allocation_ids[0]


def test_refund_from_unapplied_cash_leaves_allocation_intact(db):
    with db.session() as s:
        i, p, _ = allocated(s, "INV-1", "P1", 100000, 60000, 100000)
        refund(s, "R1", p, 20000)
        s.commit()
        assert unapplied(s, p) == 20000
        assert outstanding(s, i) == 40000


def test_allocation_linked_refund_restores_invoice_without_double_deduction(db):
    with db.session() as s:
        i, p, alloc_id = allocated(s, "INV-1", "P1", 100000, 60000, 100000)
        refund(s, "R1", p, 20000, allocation_id=alloc_id)
        s.commit()
        assert unapplied(s, p) == 40000
        assert outstanding(s, i) == 60000


def test_fully_allocated_then_full_reversal_restores_invoice(db):
    with db.session() as s:
        i, p, _ = allocated(s, "INV-1", "P1", 50000, 50000, 50000)
        reversal(s, "R1", p, 50000)
        s.commit()
        assert unapplied(s, p) == 0
        assert outstanding(s, i) == 50000


def test_partially_allocated_then_partial_reversal_uses_cash_first(db):
    with db.session() as s:
        i, p, _ = allocated(s, "INV-1", "P1", 100000, 60000, 100000)
        reversal(s, "R1", p, 50000)
        s.commit()
        assert unapplied(s, p) == 0
        assert outstanding(s, i) == 50000  # 10000 of the reversal exceeded unapplied cash


def test_duplicate_provider_return_adds_no_capacity(db):
    with db.session() as s:
        i, p, _ = allocated(s, "INV-1", "P1", 50000, 50000, 50000)
        reversal(s, "R1", p, 20000)
        reversal(s, "R1", p, 20000)
        s.commit()
        assert outstanding(s, i) == 20000
        with pytest.raises(ConflictError):
            reversal(s, "R1", p, 30000)


def test_non_completed_return_is_ignored(db):
    with db.session() as s:
        i, p, _ = allocated(s, "INV-1", "P1", 50000, 50000, 50000)
        refund(s, "R1", p, 20000, status="PENDING")
        s.commit()
        assert outstanding(s, i) == 0


def test_replacement_payment_after_reversal_can_be_approved(db):
    with db.session() as s:
        i, p, _ = allocated(s, "INV-1", "P1", 50000, 50000, 50000)
        reversal(s, "R1", p, 50000)
        replacement = payment(s, "P2", 50000, "INV-1")
        prop = proposal(s, replacement, [(i, 50000)])
        s.commit()

        approve(s, prop, key="replacement")
        assert unapplied(s, replacement) == 0
        assert outstanding(s, i) == 0


def test_payment_level_return_restores_each_cent_once_across_split(db):
    with db.session() as s:
        a = invoice(s, "INV-1", 5000)
        b = invoice(s, "INV-2", 5000)
        p = payment(s, "P1", 10000, "INV-1, INV-2")
        prop = proposal(s, p, [(a, 5000), (b, 5000)])
        approve(s, prop)
        refund(s, "R1", p, 5000)
        s.commit()

        assert outstanding(s, a) + outstanding(s, b) == 5000


def test_return_on_unrelated_payment_does_not_reopen_invoice(db):
    with db.session() as s:
        i, _, _ = allocated(s, "INV-1", "P1", 50000, 50000, 50000)
        other = payment(s, "P2", 10000, "OTHER")
        refund(s, "R1", other, 10000, invoice_id=i.id)
        s.commit()
        assert outstanding(s, i) == 0
        assert unapplied(s, other) == 0


def _settle(s, pay_rec, inv_rec, amount):
    return ProviderSettlementRepository(s).upsert_settlement(
        T, "paypal", "sandbox", pay_rec.provider_payment_id, inv_rec.id, amount, pay_rec.currency,
        payment_id=pay_rec.id,
    )


def test_provider_settlement_consumes_payment_capacity(db):
    with db.session() as s:
        i = invoice(s, "INV-1", 50000)
        p = payment(s, "P1", 50000, "INV-1")
        _settle(s, p, i, 50000)
        s.commit()
        assert unapplied(s, p) == 0
        assert outstanding(s, i) == 0


def test_allocation_and_settlement_for_same_money_count_once(db):
    with db.session() as s:
        i, p, _ = allocated(s, "INV-1", "P1", 50000, 50000, 100000)
        _settle(s, p, i, 50000)
        s.commit()
        assert outstanding(s, i) == 50000
        assert unapplied(s, p) == 0


def test_other_currency_return_fails_closed(db):
    with db.session() as s:
        i = invoice(s, "INV-1", 50000)
        p = payment(s, "P1", 50000, "INV-1")
        RefundRepository(s).upsert_refund(T, "paypal", "R1", p.id, 1000, "EUR")
        prop = proposal(s, p, [(i, 50000)])
        s.commit()
        assert unapplied(s, p) == 0
        with pytest.raises(VerificationFailedError):
            approve(s, prop)


@settings(max_examples=60, deadline=None)
@given(
    gross=st.integers(1000, 100000),
    split=st.floats(0, 1),
    returns=st.lists(st.tuples(st.integers(1, 40000), st.sampled_from(["none", "invoice"])), max_size=4),
)
def test_ledger_conservation_invariant(tmp_path_factory, gross, split, returns):
    path = tmp_path_factory.mktemp("prop") / "ledger.db"
    engine = DatabaseEngine(DatabaseConfig(url=f"sqlite:///{path}"))
    engine.init_schema()
    with engine.session() as s:
        TenantRepository(s).create_tenant(T, "Tenant A")
        applied = max(1, int(gross * split))
        i, p, _ = allocated(s, "INV-1", "P1", gross, applied, gross)
        total_returned = 0
        for idx, (amount, target) in enumerate(returns):
            refund(s, f"R{idx}", p, amount, invoice_id=i.id if target == "invoice" else None)
            total_returned += amount
        s.commit()

        ledger = PaymentRepository(s).get_ledger(T, p.id)
        assert ledger.unapplied_minor >= 0
        assert all(0 <= line.net_minor <= line.amount_minor for line in ledger.lines)
        assert ledger.unapplied_minor + ledger.net_applied_minor == max(0, gross - total_returned)
        assert outstanding(s, i) == gross - ledger.net_applied_minor
    engine.dispose()


# --------------------------------------------------------------------------
# Approval gate and decision state
# --------------------------------------------------------------------------


def test_valid_unaffected_proposal_still_approves(db):
    with db.session() as s:
        i = invoice(s, "INV-1", 10000)
        p = payment(s, "P1", 10000, "INV-1")
        prop = proposal(s, p, [(i, 10000)])
        s.commit()
        result = approve(s, prop)
        assert result.status == "APPROVED"
        assert unapplied(s, p) == 0
        assert outstanding(s, i) == 0


def test_rejected_proposal_cannot_be_approved(db):
    with db.session() as s:
        i = invoice(s, "INV-1", 10000)
        p = payment(s, "P1", 10000, "INV-1")
        prop = proposal(s, p, [(i, 10000)])
        ApprovalService(s).reject_proposal(T, prop.id, "operator", "reject-1", "wrong customer")
        with pytest.raises(ConflictError, match="rejected"):
            approve(s, prop)
        assert s.scalar(select(func.count()).select_from(AllocationRecord)) == 0


def test_approved_proposal_cannot_be_rejected_or_escalated(db):
    with db.session() as s:
        i = invoice(s, "INV-1", 10000)
        p = payment(s, "P1", 10000, "INV-1")
        prop = proposal(s, p, [(i, 10000)])
        approve(s, prop)
        with pytest.raises(ConflictError, match="approved"):
            ApprovalService(s).reject_proposal(T, prop.id, "operator", "reject-1", "changed mind")
        with pytest.raises(ConflictError, match="approved"):
            ApprovalService(s).escalate_proposal(T, prop.id, "operator", "esc-1", "changed mind")


@pytest.mark.parametrize("status", ["UNVERIFIED", "INVALID", "NEEDS_REVIEW"])
def test_failed_verifier_status_is_not_approvable(db, status):
    with db.session() as s:
        i = invoice(s, "INV-1", 10000)
        p = payment(s, "P1", 10000, "INV-1")
        prop = proposal(s, p, [(i, 10000)], verifier_status=status)
        s.commit()
        with pytest.raises(Exception, match="not eligible"):
            approve(s, prop)
        assert s.scalar(select(func.count()).select_from(AllocationRecord)) == 0


def test_competing_proposals_on_one_payment_cannot_both_approve(db):
    with db.session() as s:
        a = invoice(s, "INV-1", 10000)
        b = invoice(s, "INV-2", 10000)
        p = payment(s, "P1", 10000, "INV-1 INV-2")
        first = proposal(s, p, [(a, 10000)])
        second = proposal(s, p, [(b, 10000)])
        first.created_at = second.created_at = NOW
        s.commit()

        approve(s, first, key="first")
        with pytest.raises(VerificationFailedError):
            approve(s, second, key="second")
        assert s.scalar(select(func.count()).select_from(AllocationRecord)) == 1
        assert unapplied(s, p) == 0


@pytest.fixture
def postgres_db():
    url = os.environ.get("TEST_POSTGRESQL_URL")
    if not url:
        pytest.skip("TEST_POSTGRESQL_URL is not set")
    engine = DatabaseEngine(DatabaseConfig(url=url))
    engine.init_schema()
    tenant = f"t_{os.urandom(6).hex()}"
    with engine.session() as s:
        TenantRepository(s).create_tenant(tenant, "Concurrency")
    yield engine, tenant
    engine.dispose()


def _race(engine, tenant, proposal_ids, workers=8):
    barrier = threading.Barrier(workers)
    outcomes = []

    def worker(n):
        barrier.wait()
        with engine.session() as s:
            try:
                ApprovalService(s).approve_proposal(
                    tenant, proposal_ids[n % len(proposal_ids)], f"op-{n}", f"race-{tenant}-{n}"
                )
                outcomes.append("approved")
            except Exception as exc:
                outcomes.append(type(exc).__name__)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return outcomes


def test_concurrent_approvals_of_one_partial_proposal_allocate_once(postgres_db):
    """Capacity remains after one approval, so only the proposal lock stops a duplicate."""
    engine, tenant = postgres_db
    with engine.session() as s:
        i = invoice(s, "INV-1", 100000, tenant=tenant)
        p = payment(s, "P1", 40000, "INV-1", tenant=tenant)
        prop = proposal(s, p, [(i, 40000)], tenant=tenant)
        prop_id, pay_id = prop.id, p.id

    outcomes = _race(engine, tenant, [prop_id])
    assert outcomes.count("approved") == 1, outcomes
    with engine.session() as s:
        rows = s.scalars(select(AllocationRecord).where(AllocationRecord.payment_id == pay_id)).all()
        assert [r.amount_minor for r in rows] == [40000]


def test_concurrent_approvals_cannot_exceed_invoice_capacity(postgres_db):
    engine, tenant = postgres_db
    with engine.session() as s:
        i = invoice(s, "INV-1", 10000, tenant=tenant)
        ids = [
            proposal(s, payment(s, f"P{n}", 10000, "INV-1", tenant=tenant), [(i, 10000)], tenant=tenant).id
            for n in range(4)
        ]
        inv_id = i.id

    outcomes = _race(engine, tenant, ids)
    assert outcomes.count("approved") == 1, outcomes
    with engine.session() as s:
        assert InvoiceRepository(s).get_ledger_outstanding_minor(tenant, inv_id) == 0
        assert s.scalar(
            select(func.count()).select_from(AllocationRecord).where(AllocationRecord.invoice_id == inv_id)
        ) == 1


# --------------------------------------------------------------------------
# API contract
# --------------------------------------------------------------------------


def _seed_stale(db):
    with db.session() as s:
        i = invoice(s, "INV-1", 45000)
        p = payment(s, "P1", 45000, "INV-1")
        prop = proposal(s, p, [(i, 45000)])
        reversal(s, "R1", p, 45000, invoice_id=i.id)
        return prop.id


def test_api_marks_stale_proposal_ineligible_and_explains(client, db):
    prop_id = _seed_stale(db)
    item = client.get(f"/admin/proposals?tenant_id={T}").json()["proposals"][0]
    assert item["id"] == prop_id
    assert item["approval_eligible"] is False
    assert item["verifier_status"] == "UNVERIFIED"
    assert "no remaining allocation capacity" in item["approval_prohibited_reason"]
    assert client.get(f"/admin/dashboard/summary?tenant_id={T}").json()["counts"]["actionable_proposals"] == 0


def test_direct_api_approval_of_stale_proposal_is_refused(client, db):
    prop_id = _seed_stale(db)
    response = client.post(f"/admin/proposals/{prop_id}/approve", json={"reviewer": "op", "tenant_id": T})
    assert response.status_code == 400
    assert "no remaining allocation capacity" in response.json()["detail"]
    with db.session() as s:
        assert s.scalar(select(func.count()).select_from(AllocationRecord)) == 0
        assert s.scalar(select(func.count()).select_from(DecisionRecord)) == 0


def test_api_long_reviewer_does_not_break_idempotency_key(client, db):
    with db.session() as s:
        i = invoice(s, "INV-1", 10000)
        prop_id = proposal(s, payment(s, "P1", 10000, "INV-1"), [(i, 10000)]).id
    body = {"reviewer": "r" * 255, "tenant_id": T}
    first = client.post(f"/admin/proposals/{prop_id}/approve", json=body)
    assert first.status_code == 200
    with db.session() as s:
        assert all(len(k) <= 128 for k in s.scalars(select(DecisionRecord.idempotency_key)))
    replay = client.post(f"/admin/proposals/{prop_id}/approve", json=body)
    assert replay.json()["is_idempotent_replay"] is True


def test_api_status_filter_applies_before_pagination(client, db):
    with db.session() as s:
        ids = []
        for n in range(3):
            i = invoice(s, f"INV-{n}", 10000)
            ids.append(proposal(s, payment(s, f"P{n}", 10000, f"INV-{n}"), [(i, 10000)]).id)
    for prop_id in ids[1:]:
        client.post(f"/admin/proposals/{prop_id}/reject", json={"reviewer": "op", "reason": "x", "tenant_id": T})

    body = client.get(f"/admin/proposals?tenant_id={T}&status=PENDING&limit=1").json()
    assert [p["id"] for p in body["proposals"]] == [ids[0]]
    assert body["total_count"] == 1


def test_api_cross_tenant_lookup_is_not_found_without_leaking_owner(client, db):
    with db.session() as s:
        i = invoice(s, "INV-1", 10000, tenant="tenant_b")
        prop_id = proposal(s, payment(s, "P1", 10000, "INV-1", tenant="tenant_b"), [(i, 10000)], tenant="tenant_b").id
    response = client.get(f"/admin/proposals/{prop_id}?tenant_id={T}")
    assert response.status_code == 404
    assert "tenant_b" not in response.text
    approve_response = client.post(f"/admin/proposals/{prop_id}/approve", json={"reviewer": "op", "tenant_id": T})
    assert approve_response.status_code == 404


def test_api_rejects_invalid_pagination(client):
    assert client.get("/admin/proposals?limit=0").status_code == 422
    assert client.get("/admin/transactions?limit=100000").status_code == 422
    assert client.get("/admin/audit-events?offset=-1").status_code == 422


def test_api_does_not_trust_arbitrary_browser_origins(client):
    response = client.get("/health", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in {k.lower() for k in response.headers}


def test_api_requires_authentication():
    with TestClient(app) as anonymous:
        assert anonymous.get("/admin/proposals").status_code == 401
        assert anonymous.post("/admin/proposals/x/approve", json={"reviewer": "op"}).status_code == 401

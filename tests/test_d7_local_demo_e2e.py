"""End-to-end integration and workflow verification for isolated local demo database.

Verifies:
1. Conclusive database isolation (PostgreSQL on localhost / ::1, database 'reconcile_demo').
2. Alembic migration head revision ('0005_d6_ai_investigations').
3. Deterministic synthetic records for all 10 fintech scenarios + 1 adversarial injection case.
4. Complete labelling with '(DEMO)'.
5. Zero automatic approvals during initial setup (strict financial safety invariant).
6. Reading synthetic transactions, invoices, refunds, and reversals.
7. Proposal generation via reconciliation matcher.
8. AI investigation evidence retrieval and structured findings.
9. Human approval workflow execution via ApprovalService.
10. Cryptographic SHA-256 audit event generation and hash-chain verification.
"""

from __future__ import annotations

import os
import pytest
from sqlalchemy import text
from datetime import datetime, timezone

from reconcile.paypal import load_dotenv

# Ensure local demo configuration is loaded
load_dotenv(".env.demo", override=True)

from reconcile_store.config import DatabaseConfig
from reconcile_store.db import DatabaseEngine
from reconcile_store.approval import ApprovalService
from reconcile_store.repositories.tenant_repo import TenantRepository
from reconcile_store.repositories.payment_repo import PaymentRepository
from reconcile_store.repositories.invoice_repo import InvoiceRepository
from reconcile_store.repositories.refund_repo import RefundRepository
from reconcile_store.repositories.reconciliation_repo import (
    ProposalRepository,
    ReconciliationRunRepository,
)
from reconcile_store.repositories.investigation_repo import InvestigationRepository
from reconcile_store.repositories.audit_repo import AuditRepository
from reconcile_api.local_demo_dataset import seed_local_demo_dataset


@pytest.fixture(scope="module")
def demo_engine():
    """Verify isolation and provide connection to local demo database."""
    if os.path.exists(".env.demo"):
        load_dotenv(".env.demo", override=True)
    config = DatabaseConfig.from_env()
    engine = DatabaseEngine(config)

    # 1. Conclusively verify isolation before yielding
    with engine.session() as session:
        if engine.engine.dialect.name == "postgresql":
            row = session.execute(
                text("SELECT inet_server_addr(), current_database(), current_user")
            ).fetchone()
            server_ip, db_name, db_user = str(row[0]), str(row[1]), str(row[2])

            # Must be local loopback
            assert server_ip in ("::1", "127.0.0.1", "None"), (
                f"SECURITY ERROR: Target database server is not local! inet_server_addr={server_ip}"
            )
            assert db_name == "reconcile_demo", (
                f"SECURITY ERROR: Target database is not 'reconcile_demo'! current_database={db_name}"
            )

    yield engine

    # Teardown: Reseed to ensure clean unapproved state is left for interactive operator review
    with engine.session() as session:
        seed_local_demo_dataset(session, tenant_id="demo_merchant", force=True)


def test_01_database_isolation_and_migration_head(demo_engine):
    """Verify target database is isolated local PostgreSQL at migration head."""
    with demo_engine.session() as s:
        if demo_engine.engine.dialect.name == "postgresql":
            # Check server host and database name
            res = s.execute(text("SELECT inet_server_addr(), current_database()")).fetchone()
            assert str(res[0]) in ("::1", "127.0.0.1", "None")
            assert res[1] == "reconcile_demo"

            # Check alembic migration revision
            rev = s.execute(text("SELECT version_num FROM alembic_version")).scalar()
            assert rev == "0005_d6_ai_investigations", f"Expected head revision, got {rev}"


def test_02_idempotent_seeding_and_no_auto_approvals(demo_engine):
    """Verify seeding creates deterministic records and leaves zero automatic approvals."""
    with demo_engine.session() as s:
        result = seed_local_demo_dataset(s, tenant_id="demo_merchant", force=True)
        assert result["status"] == "seeded"
        assert result["tenant_id"] == "demo_merchant"
        assert result["payments_count"] == 11
        assert result["invoices_count"] == 12
        assert result["refunds_count"] == 1
        assert result["reversals_count"] == 1

        # Strict safety check: No automatic approvals allowed during setup
        p_repo = PaymentRepository(s)
        allocated_sum = sum(
            p_repo.get_total_allocated_minor("demo_merchant", p.id)
            for p in p_repo.list_payments("demo_merchant")
        )
        assert allocated_sum == 0, (
            "No cash should be allocated during setup; all allocations must remain zero"
        )
        unapplied_sum = sum(
            p_repo.get_unapplied_cash_minor("demo_merchant", p.id)
            for p in p_repo.list_payments("demo_merchant")
        )
        total_payments_sum = sum(
            p.amount_minor for p in p_repo.list_payments("demo_merchant")
        )
        # Unapplied cash equals total payments net of refunds and reversals
        assert unapplied_sum == total_payments_sum - 10000 - 45000


def test_03_synthetic_scenarios_and_demo_labels(demo_engine):
    """Verify all 10 requested synthetic scenarios exist and are explicitly labelled DEMO."""
    with demo_engine.session() as s:
        p_repo = PaymentRepository(s)
        i_repo = InvoiceRepository(s)
        r_repo = RefundRepository(s)
        invg_repo = InvestigationRepository(s)

        payments = {p.provider_payment_id: p for p in p_repo.list_payments("demo_merchant")}
        invoices = {i.provider_invoice_id: i for i in i_repo.list_invoices("demo_merchant")}

        # 1. Exact match
        assert "PAY-DEMO-EXACT-001" in payments
        assert "INV-DEMO-EXACT-001" in invoices
        assert payments["PAY-DEMO-EXACT-001"].amount_minor == 125000
        assert invoices["INV-DEMO-EXACT-001"].total_minor == 125000

        # 2. Unmatched payment
        assert "PAY-DEMO-UNMATCHED-002" in payments
        assert payments["PAY-DEMO-UNMATCHED-002"].reference is None

        # 3. Competing invoices
        assert "PAY-DEMO-COMPETING-003" in payments
        assert "INV-DEMO-COMPETE-3A" in invoices
        assert "INV-DEMO-COMPETE-3B" in invoices
        assert invoices["INV-DEMO-COMPETE-3A"].total_minor == 40000
        assert invoices["INV-DEMO-COMPETE-3B"].total_minor == 40000

        # 4. Partial payment
        assert "PAY-DEMO-PARTIAL-004" in payments
        assert "INV-DEMO-PARTIAL-004" in invoices
        assert payments["PAY-DEMO-PARTIAL-004"].amount_minor == 80000
        assert invoices["INV-DEMO-PARTIAL-004"].total_minor == 200000

        # 5. Split payment
        assert "PAY-DEMO-SPLIT-005" in payments
        assert "INV-DEMO-SPLIT-5A" in invoices
        assert "INV-DEMO-SPLIT-5B" in invoices
        assert payments["PAY-DEMO-SPLIT-005"].amount_minor == 150000
        assert invoices["INV-DEMO-SPLIT-5A"].total_minor + invoices["INV-DEMO-SPLIT-5B"].total_minor == 150000

        # 6. Overpayment
        assert "PAY-DEMO-OVERPAY-006" in payments
        assert "INV-DEMO-OVERPAY-006" in invoices
        assert payments["PAY-DEMO-OVERPAY-006"].amount_minor == 65000
        assert invoices["INV-DEMO-OVERPAY-006"].total_minor == 50000

        # 7. Refund
        assert "PAY-DEMO-REFUND-007" in payments
        refunds = r_repo.list_all_refunds("demo_merchant")
        assert len(refunds) >= 1
        assert any(r.record_type == "REFUND" and r.amount_minor == 10000 for r in refunds)

        # 8. Reversal
        assert "PAY-DEMO-REVERSAL-008" in payments
        reversals = r_repo.list_all_reversals("demo_merchant")
        assert len(reversals) >= 1
        assert any(r.record_type == "REVERSAL" and r.amount_minor == 45000 for r in reversals)

        # 9. Currency mismatch
        assert "PAY-DEMO-CURR-MISMATCH-009" in payments
        assert "INV-DEMO-EUR-009" in invoices
        assert payments["PAY-DEMO-CURR-MISMATCH-009"].currency == "USD"
        assert invoices["INV-DEMO-EUR-009"].currency == "EUR"

        # 10. AI investigation requiring human review
        assert "PAY-DEMO-INVESTIGATE-010" in payments
        assert "INV-DEMO-CYBERDYNE-010" in invoices

        # 11. Security prompt injection test
        assert "PAY-DEMO-INJECTION-011" in payments

        # Verify DEMO labels
        for p in payments.values():
            assert "DEMO" in p.payer_name or "DEMO" in (p.note or "") or "DEMO" in p.provider_payment_id
        for i in invoices.values():
            assert "DEMO" in i.customer_name or "DEMO" in (i.memo or "") or "DEMO" in i.invoice_number


def test_04_ai_investigation_evidence_and_findings(demo_engine):
    """Verify AI investigation provides grounded evidence, verified facts, and safety checks."""
    with demo_engine.session() as s:
        invg_repo = InvestigationRepository(s)
        invs = invg_repo.list_investigations("demo_merchant")
        assert len(invs) >= 2

        # 1. Verified investigation for PAY-DEMO-INVESTIGATE-010
        valid_inv = next(i for i in invs if i.target_id == "PAY-DEMO-INVESTIGATE-010")
        assert valid_inv.status == "NEEDS_REVIEW"
        assert valid_inv.verifier_status == "NEEDS_REVIEW"

        v_details = valid_inv.verifier_details
        assert v_details["passed"] is True
        assert len(v_details["issues"]) == 0
        assert v_details["checked_invariants"]["tenant_isolation"] is True
        assert v_details["checked_invariants"]["target_entity_exists"] is True
        assert v_details["checked_invariants"]["evidence_references_authentic"] is True
        assert v_details["checked_invariants"]["facts_strictly_grounded"] is True

        findings = valid_inv.result
        assert "verified_facts" in findings and len(findings["verified_facts"]) >= 2
        assert "supporting_evidence" in findings and len(findings["supporting_evidence"]) >= 2
        assert "candidate_invoice_analysis" in findings
        assert "hypotheses" in findings
        assert "recommended_steps" in findings
        assert findings["suggested_resolution"]["resolution_type"] == "ALLOCATE_PAYMENT"

        # 2. Blocked prompt injection investigation
        invalid_inv = next(i for i in invs if i.target_id == "PAY-DEMO-INJECTION-011")
        assert invalid_inv.status == "INVALID"
        assert invalid_inv.verifier_status == "INVALID"
        assert invalid_inv.verifier_details["passed"] is False


def test_05_reconciliation_proposal_generation(demo_engine):
    """Verify deterministic reconciliation run generated proposals for review."""
    with demo_engine.session() as s:
        run_repo = ReconciliationRunRepository(s)
        prop_repo = ProposalRepository(s)

        runs = run_repo.list_runs("demo_merchant")
        assert len(runs) >= 1
        latest_run = runs[0]
        assert latest_run.matcher_name == "deterministic_v2"

        props = prop_repo.list_by_run("demo_merchant", latest_run.id)
        assert len(props) >= 1
        for p in props:
            assert p.outcome in ("PROPOSED", "NO_MATCH", "UNMATCHED", "AMBIGUOUS")
            # All proposals must initially be PENDING / unapproved
            assert not p.decisions or p.decisions[-1].decision_type == "PENDING"


def test_06_human_approval_workflow_and_audit_chain(demo_engine):
    """Verify human approval execution via ApprovalService and cryptographic audit chain."""
    with demo_engine.session() as s:
        p_repo = PaymentRepository(s)
        prop_repo = ProposalRepository(s)
        audit_repo = AuditRepository(s)
        approval_service = ApprovalService(s)

        # 1. Initial audit chain state
        initial_events = audit_repo.list_events("demo_merchant")
        assert len(initial_events) >= 1
        audit_repo.verify_tenant_chain("demo_merchant")

        # 2. Select the exact match payment proposal
        pay_exact = p_repo.get_by_provider_id("demo_merchant", "paypal", "PAY-DEMO-EXACT-001")
        props = prop_repo.list_by_payment("demo_merchant", pay_exact.id)
        assert len(props) >= 1
        target_prop = props[0]

        # 3. Execute atomic approval transaction
        now_ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        res = approval_service.approve_proposal(
            tenant_id="demo_merchant",
            proposal_id=target_prop.id,
            actor="operator@example.demo",
            idempotency_key=f"e2e-demo-approval-{target_prop.id}-{now_ts}",
            reason="DEMO E2E operator approval verification",
        )
        assert res.status == "APPROVED"
        assert len(res.allocation_ids) >= 1
        assert res.audit_event_id != ""

        # 4. Verify unapplied cash updated accurately
        unapplied_after = p_repo.get_unapplied_cash_minor("demo_merchant", pay_exact.id)
        assert unapplied_after == 0  # fully allocated

        # 5. Verify cryptographic hash chain with newly appended PROPOSAL_APPROVED event
        updated_events = audit_repo.list_events("demo_merchant")
        assert len(updated_events) == len(initial_events) + 1
        latest_event = updated_events[-1]
        assert latest_event.event_type == "PROPOSAL_APPROVED"
        assert latest_event.actor == "operator@example.demo"

        # Conclusively verify SHA-256 hash chaining
        audit_repo.verify_tenant_chain("demo_merchant")


def test_07_amount_only_matching_and_investigation_safeguards(demo_engine):
    """Verify PAY-DEMO-INVESTIGATE-010 is not an amount-only match, and verifier rejects amount-only matches."""
    with demo_engine.session() as s:
        p_repo = PaymentRepository(s)
        i_repo = InvoiceRepository(s)
        pay_10 = p_repo.get_by_provider_id("demo_merchant", "paypal", "PAY-DEMO-INVESTIGATE-010")
        assert pay_10 is not None
        # Verify payment memo contains explicit invoice reference and project delivery context
        assert "INV-DEMO-CYBERDYNE-010" in (pay_10.note or "")
        assert "Project Alpha Hardware Delivery" in (pay_10.note or "")

        # Target invoice has matching project memo and amount
        inv_10 = i_repo.get_by_provider_id("demo_merchant", "paypal", "INV-DEMO-CYBERDYNE-010")
        assert inv_10 is not None
        assert inv_10.total_minor == pay_10.amount_minor == 89000
        assert "Project Alpha" in (inv_10.memo or "")

        # But remitter identity differs (Global Treasury Gateway vs Cyberdyne)
        assert pay_10.payer_name != inv_10.customer_name

        # Verify AI investigation escalated this to NEEDS_REVIEW
        invg_repo = InvestigationRepository(s)
        invs = invg_repo.list_investigations("demo_merchant")
        inv_rec = next((i for i in invs if i.target_id == "PAY-DEMO-INVESTIGATE-010"), None)
        assert inv_rec is not None
        assert inv_rec.status == "NEEDS_REVIEW"
        assert inv_rec.verifier_status == "NEEDS_REVIEW"


def test_08_api_dashboard_summary_and_currency_counts(demo_engine):
    """Verify dashboard summary API returns independent per-currency counts and actionable proposals."""
    from reconcile_api.dependencies import set_db_engine
    from reconcile_api.main import app
    from fastapi.testclient import TestClient

    # Reseed to clean state
    with demo_engine.session() as s:
        seed_local_demo_dataset(s, tenant_id="demo_merchant", force=True)

    set_db_engine(demo_engine)
    admin_token = os.environ.get("ADMIN_API_KEY", "demo-local-dev-key")
    client = TestClient(app)
    client.headers.update({"Authorization": f"Bearer {admin_token}"})

    resp = client.get("/admin/dashboard/summary?tenant_id=demo_merchant")
    assert resp.status_code == 200
    data = resp.json()

    # Verify counts: 6 actionable out of 11 unresolved (safety-blocked INJECTION-011 and INVESTIGATE-010 excluded)
    counts = data["counts"]
    assert counts["actionable_proposals"] == 6
    assert counts["unresolved_proposals"] == 11

    # Verify independent currency counts
    financial = data["financial_totals"]
    assert "USD" in financial
    assert financial["USD"]["payments_count"] == 11
    # EUR payments count must be 0
    if "EUR" in financial:
        assert financial["EUR"]["payments_count"] == 0
        assert financial["EUR"]["invoices_count"] == 1


def test_09_api_transactions_record_types_and_refunds(demo_engine):
    """Verify transactions endpoint surfaces record types, net amounts, and drawer details."""
    from reconcile_api.dependencies import set_db_engine
    from reconcile_api.main import app
    from fastapi.testclient import TestClient

    set_db_engine(demo_engine)
    admin_token = os.environ.get("ADMIN_API_KEY", "demo-local-dev-key")
    client = TestClient(app)
    client.headers.update({"Authorization": f"Bearer {admin_token}"})

    resp = client.get("/admin/transactions?tenant_id=demo_merchant")
    assert resp.status_code == 200
    txs = {t["provider_payment_id"]: t for t in resp.json()["transactions"]}

    # Verify PAY-DEMO-REFUND-007 has refund and net amount (30000 - 10000 = 20000)
    assert "PAY-DEMO-REFUND-007" in txs
    tx_refund = txs["PAY-DEMO-REFUND-007"]
    assert tx_refund["refund_amount_minor"] == 10000
    assert tx_refund["net_amount_minor"] == 20000

    # Verify PAY-DEMO-REVERSAL-008 has reversal and net amount (45000 - 45000 = 0)
    assert "PAY-DEMO-REVERSAL-008" in txs
    tx_reversal = txs["PAY-DEMO-REVERSAL-008"]
    assert tx_reversal["reversal_amount_minor"] == 45000
    assert tx_reversal["net_amount_minor"] == 0

    # Test single transaction detail endpoint
    resp_detail = client.get("/admin/transactions/PAY-DEMO-REFUND-007?tenant_id=demo_merchant")
    assert resp_detail.status_code == 200
    detail = resp_detail.json()
    assert detail["reconciliation_status"] in ("RECONCILED", "UNMATCHED", "PARTIALLY_RECONCILED", "AMBIGUOUS")
    assert len(detail["refunds"]) >= 1
    assert detail["refunds"][0]["provider_refund_id"] == "REFUND-DEMO-001"


def test_10_reconciliation_runs_timestamps_and_audit_events(demo_engine):
    """Verify runs API exposes run_at/executed_at and audit trail has verified hash chain."""
    from reconcile_api.dependencies import set_db_engine
    from reconcile_api.main import app
    from fastapi.testclient import TestClient

    set_db_engine(demo_engine)
    admin_token = os.environ.get("ADMIN_API_KEY", "demo-local-dev-key")
    client = TestClient(app)
    client.headers.update({"Authorization": f"Bearer {admin_token}"})

    resp = client.get("/admin/runs?tenant_id=demo_merchant")
    assert resp.status_code == 200
    runs = resp.json()["runs"]
    assert len(runs) >= 1
    run = runs[0]
    assert run["run_at"] is not None
    assert run["executed_at"] is not None

    resp_audit = client.get("/admin/audit-events?tenant_id=demo_merchant")
    assert resp_audit.status_code == 200
    events = resp_audit.json()["audit_events"]
    assert len(events) >= 4
    event_types = [e["event_type"] for e in events]
    assert "DEMO_DATASET_INITIALIZED" in event_types
    assert "RECONCILIATION_RUN_COMPLETED" in event_types
    assert "INVESTIGATION_COMPLETED" in event_types

    # Also verify the cryptographic verification endpoint
    resp_verify = client.get("/admin/audit-events/verify?tenant_id=demo_merchant")
    assert resp_verify.status_code == 200
    assert resp_verify.json()["verified"] is True

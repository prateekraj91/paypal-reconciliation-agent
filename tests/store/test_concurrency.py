"""Concurrency and contention tests for SQLite and PostgreSQL."""

from __future__ import annotations

import os
import tempfile
import threading
import concurrent.futures
import uuid
import pytest
from datetime import datetime, timezone

from alembic.config import Config
from alembic import command

from reconcile_store.config import DatabaseConfig
from reconcile_store.db import DatabaseEngine
from reconcile_store.schema import (
    Base,
    AllocationRecord,
    ProposalAllocationRecord,
    VerificationRecord,
    DecisionRecord,
    ProposalRecord,
    ReconciliationRunRecord,
    PaymentRecord,
    InvoiceRecord,
    AuditEventRecord,
    ConnectionRecord,
    ProviderRecord,
    TenantRecord,
)
from reconcile_store.repositories import (
    TenantRepository,
    InvoiceRepository,
    PaymentRepository,
    ReconciliationRunRepository,
    ProposalRepository,
    AllocationRepository,
    DecisionRepository,
    AuditRepository,
    ConflictError,
    StaleVersionError,
)
from reconcile_store.approval import ApprovalService, VerificationFailedError, ApprovalError
from reconcile_store.repositories.allocation_repo import LedgerConstraintError


def _setup_competing_proposals(engine: DatabaseEngine, tenant_id: str, inv_total: int, pay_total: int):
    """Setup one invoice and two competing proposals trying to fully claim it."""
    with engine.session() as session:
        t_repo = TenantRepository(session)
        inv_repo = InvoiceRepository(session)
        pay_repo = PaymentRepository(session)
        run_repo = ReconciliationRunRepository(session)
        prop_repo = ProposalRepository(session)

        t_repo.get_or_create_tenant(tenant_id, "Concurrency Tenant")

        inv = inv_repo.upsert_invoice(
            tenant_id=tenant_id,
            provider="paypal",
            provider_invoice_id=f"INV-CONC-{tenant_id}",
            invoice_number=f"INV-{tenant_id}",
            total_minor=inv_total,
            currency="USD",
            status="SENT",
            customer_name="Competitor",
        )
        pay1 = pay_repo.upsert_payment(
            tenant_id=tenant_id,
            provider="paypal",
            provider_payment_id=f"PAY-CONC-1-{tenant_id}",
            amount_minor=pay_total,
            currency="USD",
            status="COMPLETED",
            reference=f"INV-{tenant_id}",
            payer_name="Payer 1",
        )
        pay2 = pay_repo.upsert_payment(
            tenant_id=tenant_id,
            provider="paypal",
            provider_payment_id=f"PAY-CONC-2-{tenant_id}",
            amount_minor=pay_total,
            currency="USD",
            status="COMPLETED",
            reference=f"INV-{tenant_id}",
            payer_name="Payer 2",
        )
        run = run_repo.create_run(
            tenant_id=tenant_id,
            input_snapshot_id="snap-conc",
            matcher_name="deterministic_v1",
        )
        prop1 = prop_repo.create_proposal(
            tenant_id=tenant_id,
            run_id=run.id,
            payment_id=pay1.id,
            outcome="PROPOSED",
            evidence=["REFERENCE_MATCH", "AMOUNT_EXACT"],
            allocations=[{"invoice_id": inv.id, "amount_minor": inv_total, "currency": "USD"}],
        )
        prop2 = prop_repo.create_proposal(
            tenant_id=tenant_id,
            run_id=run.id,
            payment_id=pay2.id,
            outcome="PROPOSED",
            evidence=["REFERENCE_MATCH", "AMOUNT_EXACT"],
            allocations=[{"invoice_id": inv.id, "amount_minor": inv_total, "currency": "USD"}],
        )
        session.commit()
        return inv.id, prop1.id, prop2.id


def _cleanup_tenant(engine: DatabaseEngine, tenant_id: str) -> None:
    """Clean up all records for a test tenant in dependency order."""
    from sqlalchemy import delete
    with engine.session() as session:
        session.execute(delete(AllocationRecord).where(AllocationRecord.tenant_id == tenant_id))
        session.execute(delete(ProposalAllocationRecord).where(ProposalAllocationRecord.tenant_id == tenant_id))
        session.execute(delete(VerificationRecord).where(VerificationRecord.tenant_id == tenant_id))
        session.execute(delete(DecisionRecord).where(DecisionRecord.tenant_id == tenant_id))
        session.execute(delete(ProposalRecord).where(ProposalRecord.tenant_id == tenant_id))
        session.execute(delete(ReconciliationRunRecord).where(ReconciliationRunRecord.tenant_id == tenant_id))
        session.execute(delete(PaymentRecord).where(PaymentRecord.tenant_id == tenant_id))
        session.execute(delete(InvoiceRecord).where(InvoiceRecord.tenant_id == tenant_id))
        session.execute(delete(AuditEventRecord).where(AuditEventRecord.tenant_id == tenant_id))
        session.execute(delete(ConnectionRecord).where(ConnectionRecord.tenant_id == tenant_id))
        session.execute(delete(ProviderRecord).where(ProviderRecord.tenant_id == tenant_id))
        session.execute(delete(TenantRecord).where(TenantRecord.id == tenant_id))
        session.commit()


def _detect_postgresql_url() -> str | None:
    """Detect available PostgreSQL connection URL, supporting env var, local socket, or Docker."""
    from sqlalchemy import create_engine, text
    candidates = []
    if "TEST_POSTGRESQL_URL" in os.environ:
        candidates.append(os.environ["TEST_POSTGRESQL_URL"])
    candidates.extend([
        "postgresql+psycopg://@127.0.0.1:5432/reconcile_test",
        "postgresql+psycopg://postgres:postgres@127.0.0.1:5432/reconcile_test",
    ])
    for url in candidates:
        try:
            test_eng = create_engine(url, connect_args={"connect_timeout": 2})
            with test_eng.connect() as conn:
                conn.execute(text("SELECT 1"))
            return url
        except Exception:
            continue
    return None


def test_sqlite_concurrent_competing_approvals():
    """Test SQLite concurrency: two competing threads attempt to approve allocations for the same invoice.

    Guarantees:
    - Invoice capacity is never breached (balance never falls below 0).
    - Exactly one proposal wins (or one fails with lock/capacity error).
    - No duplicate or partial allocations remain.
    """
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    url = f"sqlite:///{path}"
    os.environ["RECONCILE_DB_URL"] = url
    alembic_cfg = Config("alembic.ini")
    alembic_cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(alembic_cfg, "head")

    # Use a small pool timeout for SQLite
    db = DatabaseEngine(DatabaseConfig(url=url, pool_timeout=5.0))
    inv_id, prop1_id, prop2_id = _setup_competing_proposals(db, "sqlite_conc", 10000, 10000)

    results = []
    errors = []

    def attempt_approval(prop_id: str, actor: str, key: str):
        # Create separate session per thread
        with db.session() as session:
            svc = ApprovalService(session)
            try:
                res = svc.approve_proposal(
                    tenant_id="sqlite_conc",
                    proposal_id=prop_id,
                    actor=actor,
                    idempotency_key=key,
                )
                results.append((prop_id, res))
            except Exception as e:
                errors.append((prop_id, type(e).__name__, str(e)))

    t1 = threading.Thread(target=attempt_approval, args=(prop1_id, "operator_1", "key-conc-1"))
    t2 = threading.Thread(target=attempt_approval, args=(prop2_id, "operator_2", "key-conc-2"))

    t1.start()
    t2.start()
    t1.join()
    t2.join()

    # Verify ledger consistency:
    # Exactly one approval succeeded, and the second failed cleanly
    error_details = "\n".join(f"  - Proposal {prop}: {etype}: {msg}" for prop, etype, msg in errors)
    assert len(results) == 1, (
        f"Expected exactly 1 winner, got {len(results)}.\n"
        f"Results: {results}\n"
        f"Errors ({len(errors)}):\n{error_details}"
    )
    assert len(errors) == 1, (
        f"Expected exactly 1 rejection, got {len(errors)}.\n"
        f"Errors:\n{error_details}"
    )

    # Outstanding balance on invoice must be exactly 0, never negative
    with db.session() as session:
        inv_repo = InvoiceRepository(session)
        alloc_repo = AllocationRepository(session)
        audit_repo = AuditRepository(session)

        rem = inv_repo.get_ledger_outstanding_minor("sqlite_conc", inv_id)
        assert rem == 0

        allocs = alloc_repo.get_all_allocations("sqlite_conc")
        assert len(allocs) == 1
        assert allocs[0].amount_minor == 10000

        # Audit chain must be consistent and valid
        events = audit_repo.list_events("sqlite_conc")
        assert len(events) == 1
        audit_repo.verify_tenant_chain("sqlite_conc")

    db.dispose()
    if os.path.exists(path):
        os.unlink(path)


def test_postgresql_concurrent_competing_approvals():
    """PostgreSQL Concurrency Test: Row-level lock contention (SELECT ... FOR UPDATE).

    Two simultaneous database sessions attempt to allocate the same invoice.
    Requirements:
    - PostgreSQL with_for_update serializes invoice access.
    - Exactly one transaction commits APPROVED.
    - Competing transaction fails on verifier or capacity check and rolls back.
    - No invoice over-allocation or negative balance.
    """
    pg_url = _detect_postgresql_url()
    if not pg_url:
        pytest.skip(
            "PostgreSQL is not reachable on localhost:5432. "
            "To run: \x27docker compose up -d postgres\x27 and re-run pytest."
        )

    # Run PostgreSQL schema migration
    os.environ["RECONCILE_DB_URL"] = pg_url
    alembic_cfg = Config("alembic.ini")
    alembic_cfg.set_main_option("sqlalchemy.url", pg_url)
    command.upgrade(alembic_cfg, "head")

    pg_db = DatabaseEngine(DatabaseConfig(url=pg_url))
    tenant_id = f"pg_conc_{uuid.uuid4().hex[:10]}"

    try:
        inv_id, prop1_id, prop2_id = _setup_competing_proposals(pg_db, tenant_id, 15000, 15000)

        results = []
        errors = []

        def attempt_pg_approval(prop_id: str, actor: str, key: str):
            with pg_db.session() as session:
                svc = ApprovalService(session)
                try:
                    res = svc.approve_proposal(
                        tenant_id=tenant_id,
                        proposal_id=prop_id,
                        actor=actor,
                        idempotency_key=key,
                    )
                    results.append((prop_id, res))
                except Exception as e:
                    errors.append((prop_id, type(e).__name__, str(e)))

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            f1 = executor.submit(attempt_pg_approval, prop1_id, "pg_actor_1", f"pg-key-1-{tenant_id}")
            f2 = executor.submit(attempt_pg_approval, prop2_id, "pg_actor_2", f"pg-key-2-{tenant_id}")
            concurrent.futures.wait([f1, f2])

        error_details = "\n".join(f"  - Proposal {prop}: {etype}: {msg}" for prop, etype, msg in errors)
        assert len(results) == 1, (
            f"Expected exactly 1 PostgreSQL winner, got {len(results)}.\n"
            f"Results: {results}\n"
            f"Errors ({len(errors)}):\n{error_details}"
        )
        assert len(errors) == 1, (
            f"Expected exactly 1 loser rejection, got {len(errors)}.\n"
            f"Errors:\n{error_details}"
        )

        with pg_db.session() as session:
            inv_repo = InvoiceRepository(session)
            alloc_repo = AllocationRepository(session)
            audit_repo = AuditRepository(session)

            rem = inv_repo.get_ledger_outstanding_minor(tenant_id, inv_id)
            assert rem == 0

            allocs = alloc_repo.get_all_allocations(tenant_id)
            assert len(allocs) == 1
            assert allocs[0].amount_minor == 15000

            audit_repo.verify_tenant_chain(tenant_id)
    finally:
        _cleanup_tenant(pg_db, tenant_id)
        pg_db.dispose()


def _setup_competing_partial_proposals(
    engine: DatabaseEngine,
    tenant_id: str,
    inv_total: int,
    pay1_amount: int,
    pay2_amount: int,
):
    """Setup one invoice and two competing partial payments/proposals attempting to consume capacity."""
    with engine.session() as session:
        t_repo = TenantRepository(session)
        inv_repo = InvoiceRepository(session)
        pay_repo = PaymentRepository(session)
        run_repo = ReconciliationRunRepository(session)
        prop_repo = ProposalRepository(session)

        t_repo.get_or_create_tenant(tenant_id, "Concurrency Partial Tenant")

        inv = inv_repo.upsert_invoice(
            tenant_id=tenant_id,
            provider="paypal",
            provider_invoice_id=f"INV-PART-{tenant_id}",
            invoice_number=f"INV-{tenant_id}",
            total_minor=inv_total,
            currency="USD",
            status="SENT",
            customer_name="Partial Payer",
        )
        pay1 = pay_repo.upsert_payment(
            tenant_id=tenant_id,
            provider="paypal",
            provider_payment_id=f"PAY-PART-1-{tenant_id}",
            amount_minor=pay1_amount,
            currency="USD",
            status="COMPLETED",
            reference=f"INV-{tenant_id}",
            payer_name="Partial Payer 1",
        )
        pay2 = pay_repo.upsert_payment(
            tenant_id=tenant_id,
            provider="paypal",
            provider_payment_id=f"PAY-PART-2-{tenant_id}",
            amount_minor=pay2_amount,
            currency="USD",
            status="COMPLETED",
            reference=f"INV-{tenant_id}",
            payer_name="Partial Payer 2",
        )
        run = run_repo.create_run(
            tenant_id=tenant_id,
            input_snapshot_id=f"snap-part-{tenant_id}",
            matcher_name="advanced-matcher-d5",
        )
        prop1 = prop_repo.create_proposal(
            tenant_id=tenant_id,
            run_id=run.id,
            payment_id=pay1.id,
            outcome="PROPOSED",
            evidence=["REFERENCE_MATCH", "AMOUNT_PARTIAL"],
            allocations=[{"invoice_id": inv.id, "amount_minor": pay1_amount, "currency": "USD"}],
        )
        prop2 = prop_repo.create_proposal(
            tenant_id=tenant_id,
            run_id=run.id,
            payment_id=pay2.id,
            outcome="PROPOSED",
            evidence=["REFERENCE_MATCH", "AMOUNT_PARTIAL"],
            allocations=[{"invoice_id": inv.id, "amount_minor": pay2_amount, "currency": "USD"}],
        )
        session.commit()
        return inv.id, prop1.id, prop2.id


def test_postgresql_concurrent_partial_payments_cannot_exceed_capacity():
    """Verify two concurrent database sessions cannot allocate partial payments exceeding invoice capacity.

    Invoice capacity: 10,000 cents ($100).
    Payment 1: 6,000 cents ($60).
    Payment 2: 6,000 cents ($60).
    Total requested: 12,000 cents > 10,000 cents.

    Requirements:
    - PostgreSQL row-level locks serialize access to the invoice.
    - Exactly one partial payment succeeds (6,000 cents allocated).
    - Competing partial payment fails closed on verifier / capacity check (remaining 4,000 < 6,000).
    - Remaining invoice capacity is exactly 4,000 cents (never negative).
    """
    pg_url = _detect_postgresql_url()
    if not pg_url:
        pytest.skip("PostgreSQL is not reachable on localhost:5432.")

    os.environ["RECONCILE_DB_URL"] = pg_url
    alembic_cfg = Config("alembic.ini")
    alembic_cfg.set_main_option("sqlalchemy.url", pg_url)
    command.upgrade(alembic_cfg, "head")

    pg_db = DatabaseEngine(DatabaseConfig(url=pg_url))
    tenant_id = f"pg_part_{uuid.uuid4().hex[:10]}"

    try:
        inv_id, prop1_id, prop2_id = _setup_competing_partial_proposals(
            pg_db, tenant_id, inv_total=10000, pay1_amount=6000, pay2_amount=6000
        )

        results = []
        errors = []

        def attempt_approval(prop_id: str, actor: str, key: str):
            with pg_db.session() as session:
                svc = ApprovalService(session)
                try:
                    res = svc.approve_proposal(
                        tenant_id=tenant_id,
                        proposal_id=prop_id,
                        actor=actor,
                        idempotency_key=key,
                    )
                    results.append((prop_id, res))
                except Exception as e:
                    errors.append((prop_id, type(e).__name__, str(e)))

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            f1 = executor.submit(attempt_approval, prop1_id, "part_actor_1", f"part-key-1-{tenant_id}")
            f2 = executor.submit(attempt_approval, prop2_id, "part_actor_2", f"part-key-2-{tenant_id}")
            concurrent.futures.wait([f1, f2])

        error_details = "\n".join(f"  - Proposal {prop}: {etype}: {msg}" for prop, etype, msg in errors)
        assert len(results) == 1, (
            f"Expected exactly 1 partial approval winner, got {len(results)}.\n"
            f"Results: {results}\n"
            f"Errors ({len(errors)}):\n{error_details}"
        )
        assert len(errors) == 1, (
            f"Expected exactly 1 loser rejection due to capacity limits, got {len(errors)}.\n"
            f"Errors:\n{error_details}"
        )

        with pg_db.session() as session:
            inv_repo = InvoiceRepository(session)
            alloc_repo = AllocationRepository(session)
            audit_repo = AuditRepository(session)

            rem = inv_repo.get_ledger_outstanding_minor(tenant_id, inv_id)
            assert rem == 4000, f"Expected 4000 cents remaining capacity, got {rem}"

            allocs = alloc_repo.get_all_allocations(tenant_id)
            assert len(allocs) == 1
            assert allocs[0].amount_minor == 6000

            audit_repo.verify_tenant_chain(tenant_id)
    finally:
        _cleanup_tenant(pg_db, tenant_id)
        pg_db.dispose()


def test_postgresql_concurrent_duplicate_processing_idempotency():
    """Verify duplicate processing in PostgreSQL remains idempotent under concurrent execution.

    Requirements:
    - Independent database sessions attempting concurrent approval of the same proposal
      with the same idempotency key cannot create duplicate allocations.
    - Exactly one allocation is created in the ledger.
    - Replay returns idempotent success.
    - Invoice capacity is only deducted once.
    """
    pg_url = _detect_postgresql_url()
    if not pg_url:
        pytest.skip("PostgreSQL is not reachable on localhost:5432.")

    os.environ["RECONCILE_DB_URL"] = pg_url
    alembic_cfg = Config("alembic.ini")
    alembic_cfg.set_main_option("sqlalchemy.url", pg_url)
    command.upgrade(alembic_cfg, "head")

    pg_db = DatabaseEngine(DatabaseConfig(url=pg_url))
    tenant_id = f"pg_idemp_{uuid.uuid4().hex[:10]}"

    try:
        inv_id, prop1_id, _ = _setup_competing_partial_proposals(
            pg_db, tenant_id, inv_total=10000, pay1_amount=5000, pay2_amount=5000
        )

        idempotency_key = f"idemp-key-{tenant_id}"
        results = []
        errors = []

        def attempt_idempotent_approval(actor: str):
            with pg_db.session() as session:
                svc = ApprovalService(session)
                try:
                    res = svc.approve_proposal(
                        tenant_id=tenant_id,
                        proposal_id=prop1_id,
                        actor=actor,
                        idempotency_key=idempotency_key,
                    )
                    results.append(res)
                except Exception as e:
                    errors.append((type(e).__name__, str(e)))

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            f1 = executor.submit(attempt_idempotent_approval, "idemp_actor_1")
            f2 = executor.submit(attempt_idempotent_approval, "idemp_actor_2")
            concurrent.futures.wait([f1, f2])

        # Exactly 1 initial approval succeeds, competing call is either serialized idempotent replay or rejected duplicate
        with pg_db.session() as session:
            inv_repo = InvoiceRepository(session)
            alloc_repo = AllocationRepository(session)
            audit_repo = AuditRepository(session)

            rem = inv_repo.get_ledger_outstanding_minor(tenant_id, inv_id)
            assert rem == 5000, f"Expected 5000 cents remaining capacity, got {rem}"

            allocs = alloc_repo.get_all_allocations(tenant_id)
            assert len(allocs) == 1, f"Expected exactly 1 allocation record, found {len(allocs)}"
            assert allocs[0].amount_minor == 5000

            # Subsequent sequential replay with same key must succeed idempotently
            svc = ApprovalService(session)
            replay_res = svc.approve_proposal(
                tenant_id=tenant_id,
                proposal_id=prop1_id,
                actor="replay_operator",
                idempotency_key=idempotency_key,
            )
            assert replay_res.status == "APPROVED"
            assert replay_res.is_idempotent_replay is True
            assert replay_res.allocation_ids == (allocs[0].id,)

            # Outstanding capacity must still remain 5000 (not double deducted)
            rem_after_replay = inv_repo.get_ledger_outstanding_minor(tenant_id, inv_id)
            assert rem_after_replay == 5000

            allocs_after_replay = alloc_repo.get_all_allocations(tenant_id)
            assert len(allocs_after_replay) == 1

            audit_repo.verify_tenant_chain(tenant_id)
    finally:
        _cleanup_tenant(pg_db, tenant_id)
        pg_db.dispose()

"""Deterministic PostgreSQL races between two decisions on one proposal.

Session A runs up to the moment before COMMIT and holds its locks there. Session B
starts only then, and A is released only after PostgreSQL reports B waiting on a
lock. Both orderings are therefore forced rather than left to thread timing.
"""

from __future__ import annotations

import os
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional

import pytest
from sqlalchemy import create_engine, event, func, select, text
from sqlalchemy.engine import make_url

from reconcile_store.approval import ApprovalService
from reconcile_store.config import DatabaseConfig
from reconcile_store.db import DatabaseEngine
from reconcile_store.repositories.base import ConflictError
from reconcile_store.repositories.invoice_repo import InvoiceRepository
from reconcile_store.repositories.payment_repo import PaymentRepository
from reconcile_store.repositories.reconciliation_repo import (
    ProposalRepository,
    ReconciliationRunRepository,
)
from reconcile_store.repositories.tenant_repo import TenantRepository
from reconcile_store.schema import (
    AllocationRecord,
    AuditEventRecord,
    DecisionRecord,
    PaymentRecord,
    ProposalRecord,
)

TIMEOUT_SECONDS = 15
NOW = datetime(2026, 1, 15, tzinfo=timezone.utc)


@dataclass
class Outcome:
    result: object = None
    error: Optional[BaseException] = None
    backend_pid: Optional[int] = None
    pid_ready: threading.Event = field(default_factory=threading.Event)


@contextmanager
def _scratch_database(url: str):
    """Create a throwaway database next to TEST_POSTGRESQL_URL and drop it afterwards.

    init_schema() builds tables without Alembic's version table. Doing that in the
    shared test database made later files that run `alembic upgrade head` fail with
    "relation already exists", so these races run in a database of their own.
    """
    base = make_url(DatabaseConfig(url=url).url)
    name = f"race_{os.urandom(6).hex()}"
    admin = create_engine(base, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{name}"'))
        try:
            yield base.set(database=name).render_as_string(hide_password=False)
        finally:
            with admin.connect() as conn:
                conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    finally:
        admin.dispose()


@pytest.fixture
def race():
    url = os.environ.get("TEST_POSTGRESQL_URL")
    if not url:
        pytest.skip("TEST_POSTGRESQL_URL is not set")
    with _scratch_database(url) as scratch_url:
        yield from _race_setup(scratch_url)


def _race_setup(url: str):
    engine = DatabaseEngine(DatabaseConfig(url=url))
    engine.init_schema()
    tenant = f"race_{os.urandom(6).hex()}"
    with engine.session() as s:
        TenantRepository(s).create_tenant(tenant, "Race")
        invoice = InvoiceRepository(s).upsert_invoice(
            tenant, "paypal", "INV-1", "INV-1", 100000, "USD", customer_name="Acme",
            issued_on=NOW.date(), due_on=NOW.date(),
        )
        # The payment is used in full but the invoice stays part-paid.
        payment = PaymentRepository(s).upsert_payment(
            tenant, "paypal", "PAY-1", 40000, "USD", payer_name="Acme", reference="INV-1", received_at=NOW,
        )
        run = ReconciliationRunRepository(s).create_run(tenant, "snapshot", "test")
        proposal_id = ProposalRepository(s).create_proposal(
            tenant, run.id, payment.id, "PROPOSED", evidence=["REFERENCE_MATCH"], verifier_status="VERIFIED",
            allocations=[{"invoice_id": invoice.id, "amount_minor": 40000, "currency": "USD"}],
        ).id
        s.commit()
    yield engine, tenant, proposal_id
    engine.dispose()


def _approve(key: str, actor: str = "operator") -> Callable:
    def run(service: ApprovalService, tenant: str, proposal_id: str):
        return service.approve_proposal(tenant, proposal_id, actor, key)

    return run


def _reject(key: str) -> Callable:
    def run(service: ApprovalService, tenant: str, proposal_id: str):
        return service.reject_proposal(tenant, proposal_id, "operator-2", key, "wrong customer")

    return run


def _wait_for_lock_wait(engine: DatabaseEngine, pid: int, other: threading.Thread) -> None:
    deadline = time.monotonic() + TIMEOUT_SECONDS
    query = text("SELECT wait_event_type FROM pg_stat_activity WHERE pid = :pid")
    with engine.session() as monitor:
        while time.monotonic() < deadline:
            if monitor.execute(query, {"pid": pid}).scalar() == "Lock":
                monitor.rollback()
                return
            monitor.rollback()
            if not other.is_alive():
                pytest.fail("Session B finished without waiting on a lock")
            time.sleep(0.01)
    pytest.fail("Session B never reached a lock wait")


def _force_race(engine, tenant, proposal_id, first: Callable, second: Callable) -> tuple[Outcome, Outcome]:
    """Run `first` to just before COMMIT, block `second` on its locks, then release."""
    first_at_commit = threading.Event()
    release_first = threading.Event()
    a, b = Outcome(), Outcome()

    def run_first():
        with engine.session() as s:

            def hold_commit(_session):
                if first_at_commit.is_set():
                    return
                first_at_commit.set()
                assert release_first.wait(TIMEOUT_SECONDS)

            event.listen(s, "before_commit", hold_commit)
            try:
                a.result = first(ApprovalService(s), tenant, proposal_id)
            except BaseException as exc:
                a.error = exc
                first_at_commit.set()

    def run_second():
        with engine.session() as s:
            b.backend_pid = s.execute(text("SELECT pg_backend_pid()")).scalar()
            b.pid_ready.set()
            try:
                b.result = second(ApprovalService(s), tenant, proposal_id)
            except BaseException as exc:
                b.error = exc
                s.rollback()

    thread_a = threading.Thread(target=run_first, daemon=True)
    thread_b = threading.Thread(target=run_second, daemon=True)
    thread_a.start()
    try:
        assert first_at_commit.wait(TIMEOUT_SECONDS), "Session A did not reach COMMIT"
        assert a.error is None, a.error
        thread_b.start()
        assert b.pid_ready.wait(TIMEOUT_SECONDS)
        _wait_for_lock_wait(engine, b.backend_pid, thread_b)
    finally:
        release_first.set()
    thread_a.join(TIMEOUT_SECONDS)
    if thread_b.ident is not None:
        thread_b.join(TIMEOUT_SECONDS)
    assert not thread_a.is_alive() and not thread_b.is_alive()
    assert a.error is None, a.error
    return a, b


def _counts(engine, tenant, proposal_id):
    with engine.session() as s:
        allocations = s.scalars(select(AllocationRecord).where(AllocationRecord.tenant_id == tenant)).all()
        decisions = s.scalars(select(DecisionRecord.decision_type).where(DecisionRecord.proposal_id == proposal_id)).all()
        approved_events = s.scalar(
            select(func.count()).select_from(AuditEventRecord).where(
                AuditEventRecord.tenant_id == tenant, AuditEventRecord.event_type == "PROPOSAL_APPROVED"
            )
        )
        status = s.get(ProposalRecord, proposal_id).verifier_status
        version = s.scalars(select(PaymentRecord.version).where(PaymentRecord.tenant_id == tenant)).one()
        return [a.amount_minor for a in allocations], sorted(decisions), approved_events, status, version


def test_two_approvals_of_one_proposal_have_one_winner(race):
    engine, tenant, proposal_id = race
    a, b = _force_race(engine, tenant, proposal_id, _approve("key-a", "operator-a"), _approve("key-b", "operator-b"))

    assert a.result.status == "APPROVED"
    assert isinstance(b.error, ConflictError), repr(b.error)
    assert "already been approved" in str(b.error)
    assert _counts(engine, tenant, proposal_id) == ([40000], ["APPROVED"], 1, "APPROVED", 2)


def test_same_idempotency_key_concurrently_replays_the_first_result(race):
    engine, tenant, proposal_id = race
    a, b = _force_race(engine, tenant, proposal_id, _approve("same-key"), _approve("same-key"))

    assert b.error is None, repr(b.error)
    assert b.result.is_idempotent_replay is True
    assert b.result.decision_id == a.result.decision_id
    assert _counts(engine, tenant, proposal_id) == ([40000], ["APPROVED"], 1, "APPROVED", 2)


def test_reject_arriving_during_approval_cannot_override_it(race):
    engine, tenant, proposal_id = race
    a, b = _force_race(engine, tenant, proposal_id, _approve("key-a"), _reject("reject-b"))

    assert a.result.status == "APPROVED"
    assert isinstance(b.error, ConflictError), repr(b.error)
    assert _counts(engine, tenant, proposal_id) == ([40000], ["APPROVED"], 1, "APPROVED", 2)


def test_approval_arriving_during_rejection_creates_no_allocation(race):
    engine, tenant, proposal_id = race
    _, b = _force_race(engine, tenant, proposal_id, _reject("reject-a"), _approve("key-b"))

    assert isinstance(b.error, ConflictError), repr(b.error)
    assert _counts(engine, tenant, proposal_id) == ([], ["REJECTED"], 0, "REJECTED", 1)

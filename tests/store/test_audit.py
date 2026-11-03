"""Tests for the tamper-evident SHA-256 hash-chained audit system."""

from __future__ import annotations

import os
import tempfile
import pytest
from datetime import datetime, timezone
from dataclasses import dataclass

from alembic.config import Config
from alembic import command

from reconcile_store.config import DatabaseConfig
from reconcile_store.db import DatabaseEngine
from reconcile_store.repositories import TenantRepository, AuditRepository
from reconcile_store.schema import AuditEventRecord
from reconcile_store.audit import (
    GENESIS_HASH,
    compute_event_hash,
    create_audit_payload,
    check_audit_chain,
    verify_audit_chain,
    AuditIntegrityError,
)
from reconcile_store.canonical import canonical_json


@dataclass
class MockAuditEvent:
    tenant_id: str
    sequence_number: int
    event_id: str
    event_type: str
    actor: str
    subject_type: str
    subject_id: str
    payload_json: str
    prev_hash: str
    event_hash: str


@pytest.fixture
def audit_db():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    url = f"sqlite:///{path}"
    os.environ["RECONCILE_DB_URL"] = url
    alembic_cfg = Config("alembic.ini")
    alembic_cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(alembic_cfg, "head")

    db = DatabaseEngine(DatabaseConfig(url=url))
    with db.session() as session:
        t_repo = TenantRepository(session)
        t_repo.create_tenant("tenant_audit", "Audit Corp")
        session.commit()

    yield db

    db.dispose()
    if os.path.exists(path):
        os.unlink(path)


def test_valid_audit_chain_passes(audit_db):
    """Test sequential append of valid events results in a clean, verifiable hash chain."""
    db = audit_db
    with db.session() as session:
        repo = AuditRepository(session)

        e1 = repo.append_event(
            tenant_id="tenant_audit",
            event_type="PAYMENT_INGESTED",
            actor="system",
            subject_type="payment",
            subject_id="PAY-1",
            details={"amount": 1000},
        )
        assert e1.sequence_number == 1
        assert e1.prev_hash == GENESIS_HASH
        assert len(e1.event_hash) == 64

        e2 = repo.append_event(
            tenant_id="tenant_audit",
            event_type="INVOICE_INGESTED",
            actor="system",
            subject_type="invoice",
            subject_id="INV-1",
            details={"amount": 1000},
        )
        assert e2.sequence_number == 2
        assert e2.prev_hash == e1.event_hash

        e3 = repo.append_event(
            tenant_id="tenant_audit",
            event_type="PROPOSAL_APPROVED",
            actor="user@test.com",
            subject_type="proposal",
            subject_id="PROP-1",
            details={"approved": True},
        )
        assert e3.sequence_number == 3
        assert e3.prev_hash == e2.event_hash
        session.commit()

    # Verification must pass without error
    with db.session() as session:
        repo = AuditRepository(session)
        repo.verify_tenant_chain("tenant_audit")


def test_tampered_payload_detected():
    """Verify that any modification to an event's payload invalidates the chain."""
    payload1 = canonical_json({"event": "1", "data": "initial"})
    h1 = compute_event_hash(GENESIS_HASH, payload1)

    payload2 = canonical_json({"event": "2", "data": "second"})
    h2 = compute_event_hash(h1, payload2)

    events = [
        MockAuditEvent("t1", 1, "e1", "TYPE_1", "actor", "sub", "id1", payload1, GENESIS_HASH, h1),
        MockAuditEvent("t1", 2, "e2", "TYPE_2", "actor", "sub", "id2", payload2, h1, h2),
    ]

    # Untampered passes
    assert check_audit_chain(events) == []

    # Tamper with payload of event 1
    tampered_payload = canonical_json({"event": "1", "data": "TAMPERED"})
    tampered_events = [
        MockAuditEvent("t1", 1, "e1", "TYPE_1", "actor", "sub", "id1", tampered_payload, GENESIS_HASH, h1),
        MockAuditEvent("t1", 2, "e2", "TYPE_2", "actor", "sub", "id2", payload2, h1, h2),
    ]
    issues = check_audit_chain(tampered_events)
    assert len(issues) > 0
    assert any("tampering" in issue.lower() or "tamper" in issue.lower() for issue in issues)

    with pytest.raises(AuditIntegrityError):
        verify_audit_chain(tampered_events)


def test_deleted_or_missing_event_detected():
    """Verify that deleting an event creates a sequence and hash gap detected by the verifier."""
    p1 = canonical_json({"seq": 1})
    h1 = compute_event_hash(GENESIS_HASH, p1)

    p2 = canonical_json({"seq": 2})
    h2 = compute_event_hash(h1, p2)

    p3 = canonical_json({"seq": 3})
    h3 = compute_event_hash(h2, p3)

    # Missing event 2
    broken_chain = [
        MockAuditEvent("t1", 1, "e1", "T", "a", "s", "1", p1, GENESIS_HASH, h1),
        MockAuditEvent("t1", 3, "e3", "T", "a", "s", "3", p3, h2, h3),
    ]
    issues = check_audit_chain(broken_chain)
    assert any("discontinuity" in issue.lower() or "gap" in issue.lower() for issue in issues)
    assert any("link" in issue.lower() or "prev_hash" in issue.lower() for issue in issues)


def test_reordered_events_detected():
    """Verify that reordering events causes verification failure."""
    p1 = canonical_json({"seq": 1})
    h1 = compute_event_hash(GENESIS_HASH, p1)

    p2 = canonical_json({"seq": 2})
    h2 = compute_event_hash(h1, p2)

    reordered = [
        MockAuditEvent("t1", 2, "e2", "T", "a", "s", "2", p2, h1, h2),
        MockAuditEvent("t1", 1, "e1", "T", "a", "s", "1", p1, GENESIS_HASH, h1),
    ]
    issues = check_audit_chain(reordered)
    assert len(issues) > 0


def test_modified_prev_hash_detected():
    """Verify that altering prev_hash breaks the chain."""
    p1 = canonical_json({"seq": 1})
    h1 = compute_event_hash(GENESIS_HASH, p1)

    p2 = canonical_json({"seq": 2})
    h2 = compute_event_hash(h1, p2)

    tampered_link = [
        MockAuditEvent("t1", 1, "e1", "T", "a", "s", "1", p1, GENESIS_HASH, h1),
        MockAuditEvent("t1", 2, "e2", "T", "a", "s", "2", p2, "bad_prev_hash" + "0"*51, h2),
    ]
    issues = check_audit_chain(tampered_link)
    assert any("link" in issue.lower() or "prev_hash" in issue.lower() for issue in issues)


def test_inserted_unauthorized_event_detected():
    """Verify that inserting an unauthorized event in the middle of a chain is detected."""
    p1 = canonical_json({"seq": 1})
    h1 = compute_event_hash(GENESIS_HASH, p1)

    p2 = canonical_json({"seq": 2})
    h2 = compute_event_hash(h1, p2)

    # Attacker inserts a bogus event with seq 2 and shifts the genuine event 2 to seq 3
    p_bogus = canonical_json({"seq": 2, "bogus": True})
    h_bogus = compute_event_hash(h1, p_bogus)

    inserted_chain = [
        MockAuditEvent("t1", 1, "e1", "T", "a", "s", "1", p1, GENESIS_HASH, h1),
        MockAuditEvent("t1", 2, "e_bogus", "T", "a", "s", "2", p_bogus, h1, h_bogus),
        MockAuditEvent("t1", 3, "e2", "T", "a", "s", "3", p2, h1, h2),  # prev_hash points to 1, not bogus
    ]
    issues = check_audit_chain(inserted_chain)
    assert any("broken chain link" in issue.lower() or "prev_hash" in issue.lower() for issue in issues)

    with pytest.raises(AuditIntegrityError):
        verify_audit_chain(inserted_chain)


def test_repositories_have_no_update_delete_for_immutable_records():
    """Verify repository-level immutability enforcement: immutable entities have no update or delete methods."""
    from reconcile_store.repositories import (
        AuditRepository,
        ProviderRecordRepository,
        VerificationRepository,
        AllocationRepository,
    )

    for repo_cls in (AuditRepository, ProviderRecordRepository, VerificationRepository, AllocationRepository):
        methods = dir(repo_cls)
        update_methods = [m for m in methods if "update" in m.lower() and not m.startswith("__")]
        delete_methods = [m for m in methods if "delete" in m.lower() and not m.startswith("__")]
        assert len(update_methods) == 0, f"{repo_cls.__name__} has unexpected update methods: {update_methods}"
        assert len(delete_methods) == 0, f"{repo_cls.__name__} has unexpected delete methods: {delete_methods}"

"""Tests for Alembic database migrations and schema constraints."""

from __future__ import annotations

import os
import tempfile
import pytest
from sqlalchemy import text, inspect
from alembic.config import Config
from alembic import command

from reconcile_store.config import DatabaseConfig
from reconcile_store.db import DatabaseEngine
from reconcile_store.schema import Base


@pytest.fixture
def temp_sqlite_url():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    url = f"sqlite:///{path}"
    yield url
    if os.path.exists(path):
        os.unlink(path)


def test_alembic_upgrade_and_downgrade(temp_sqlite_url):
    """Test full migration lifecycle: upgrade to head, verify 12 tables, downgrade to base, re-upgrade."""
    os.environ["RECONCILE_DB_URL"] = temp_sqlite_url
    alembic_cfg = Config("alembic.ini")
    alembic_cfg.set_main_option("sqlalchemy.url", temp_sqlite_url)

    # 1. Upgrade to head
    command.upgrade(alembic_cfg, "head")

    engine = DatabaseEngine(DatabaseConfig(url=temp_sqlite_url)).engine
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())

    expected_tables = {
        "tenants",
        "connections",
        "provider_records",
        "invoices",
        "payments",
        "reconciliation_runs",
        "proposals",
        "proposal_allocations",
        "verifications",
        "decisions",
        "allocations",
        "provider_settlements",
        "audit_events",
        "jobs",
        "webhook_inbox",
        "refunds",
        "alembic_version",
    }
    assert expected_tables.issubset(tables), f"Missing tables: {expected_tables - tables}"

    # 2. Downgrade to base
    command.downgrade(alembic_cfg, "base")
    inspector = inspect(engine)
    remaining_tables = set(inspector.get_table_names())
    assert "invoices" not in remaining_tables
    assert "payments" not in remaining_tables
    assert "allocations" not in remaining_tables

    # 3. Upgrade to head again (verifies idempotency)
    command.upgrade(alembic_cfg, "head")
    inspector = inspect(engine)
    tables_reupgraded = set(inspector.get_table_names())
    assert expected_tables.issubset(tables_reupgraded)


def test_foreign_key_enforcement_in_sqlite(temp_sqlite_url):
    """Verify that foreign keys are strictly enforced in SQLite connections."""
    os.environ["RECONCILE_DB_URL"] = temp_sqlite_url
    alembic_cfg = Config("alembic.ini")
    alembic_cfg.set_main_option("sqlalchemy.url", temp_sqlite_url)
    command.upgrade(alembic_cfg, "head")

    db = DatabaseEngine(DatabaseConfig(url=temp_sqlite_url))
    with db.session() as session:
        # Check that pragma foreign_keys is ON
        res = session.execute(text("PRAGMA foreign_keys")).scalar()
        assert res == 1

        # Attempt to insert an invoice referencing non-existent tenant
        with pytest.raises(Exception) as exc_info:
            session.execute(
                text(
                    "INSERT INTO invoices (id, tenant_id, provider, provider_invoice_id, invoice_number, "
                    "customer_name, total_minor, currency, status, issued_on, due_on, version, created_at, updated_at) "
                    "VALUES ('inv-test', 'non-existent-tenant', 'paypal', 'INV-1', 'INV-1', "
                    "'Customer', 1000, 'USD', 'SENT', '2026-01-01', '2026-01-15', 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                )
            )
            session.commit()
        assert "FOREIGN KEY" in str(exc_info.value).upper()


def test_unique_constraint_enforcement(temp_sqlite_url):
    """Verify unique constraints prevent duplicate business entities."""
    os.environ["RECONCILE_DB_URL"] = temp_sqlite_url
    alembic_cfg = Config("alembic.ini")
    alembic_cfg.set_main_option("sqlalchemy.url", temp_sqlite_url)
    command.upgrade(alembic_cfg, "head")

    db = DatabaseEngine(DatabaseConfig(url=temp_sqlite_url))
    with db.session() as session:
        # Create tenant
        session.execute(
            text("INSERT INTO tenants (id, name, created_at, updated_at) VALUES ('t1', 'Tenant 1', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)")
        )
        session.commit()

        # Insert first invoice
        session.execute(
            text(
                "INSERT INTO invoices (id, tenant_id, provider, provider_invoice_id, invoice_number, "
                "customer_name, total_minor, currency, status, issued_on, due_on, version, created_at, updated_at) "
                "VALUES ('inv-1', 't1', 'paypal', 'PAYPAL-INV-100', 'INV-100', "
                "'Customer A', 5000, 'USD', 'SENT', '2026-01-01', '2026-01-15', 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )
        session.commit()

        # Attempt to insert second invoice with identical (tenant_id, provider, provider_invoice_id)
        with pytest.raises(Exception) as exc_info:
            session.execute(
                text(
                    "INSERT INTO invoices (id, tenant_id, provider, provider_invoice_id, invoice_number, "
                    "customer_name, total_minor, currency, status, issued_on, due_on, version, created_at, updated_at) "
                    "VALUES ('inv-2', 't1', 'paypal', 'PAYPAL-INV-100', 'INV-100-DUP', "
                    "'Customer A', 5000, 'USD', 'SENT', '2026-01-01', '2026-01-15', 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                )
            )
            session.commit()
        assert "UNIQUE" in str(exc_info.value).upper()


def test_d4_migration_0002_upgrade_downgrade(temp_sqlite_url):
    """Verify that migration 0002 applies cleanly on top of 0001 and downgrades cleanly."""
    os.environ["RECONCILE_DB_URL"] = temp_sqlite_url
    alembic_cfg = Config("alembic.ini")
    alembic_cfg.set_main_option("sqlalchemy.url", temp_sqlite_url)

    # 1. Upgrade to 0001 only
    command.upgrade(alembic_cfg, "0001_initial_schema")
    engine = DatabaseEngine(DatabaseConfig(url=temp_sqlite_url)).engine
    inspector = inspect(engine)
    tables_0001 = set(inspector.get_table_names())
    assert "invoices" in tables_0001
    assert "webhook_inbox" not in tables_0001
    assert "jobs" not in tables_0001

    # 2. Upgrade to 0002_d4_webhooks_and_jobs
    command.upgrade(alembic_cfg, "0002_d4_webhooks_and_jobs")
    inspector = inspect(engine)
    tables_0002 = set(inspector.get_table_names())
    assert "webhook_inbox" in tables_0002
    assert "jobs" in tables_0002

    # Check unique constraints on new tables
    db = DatabaseEngine(DatabaseConfig(url=temp_sqlite_url))
    with db.session() as session:
        # Create tenant
        session.execute(
            text("INSERT INTO tenants (id, name, created_at, updated_at) VALUES ('t1', 'Tenant 1', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)")
        )
        # Insert job
        session.execute(
            text(
                "INSERT INTO jobs (id, tenant_id, job_type, idempotency_key, available_at, created_at, updated_at) "
                "VALUES ('j1', 't1', 'WEBHOOK_INGEST', 'key-1', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )
        session.commit()

        # Duplicate idempotency_key for same tenant must fail
        with pytest.raises(Exception) as exc_info:
            session.execute(
                text(
                    "INSERT INTO jobs (id, tenant_id, job_type, idempotency_key, available_at, created_at, updated_at) "
                    "VALUES ('j2', 't1', 'WEBHOOK_INGEST', 'key-1', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                )
            )
            session.commit()
        assert "UNIQUE" in str(exc_info.value).upper()
        session.rollback()

        # Insert webhook inbox
        session.execute(
            text(
                "INSERT INTO webhook_inbox (id, tenant_id, provider, environment, event_id, event_type, raw_payload, received_at) "
                "VALUES ('wh1', 't1', 'paypal', 'sandbox', 'EVT-1', 'PAYMENT.CAPTURE.COMPLETED', '{}', CURRENT_TIMESTAMP)"
            )
        )
        session.commit()

        # Duplicate event for same tenant/provider/env must fail
        with pytest.raises(Exception) as exc_info:
            session.execute(
                text(
                    "INSERT INTO webhook_inbox (id, tenant_id, provider, environment, event_id, event_type, raw_payload, received_at) "
                    "VALUES ('wh2', 't1', 'paypal', 'sandbox', 'EVT-1', 'PAYMENT.CAPTURE.COMPLETED', '{}', CURRENT_TIMESTAMP)"
                )
            )
            session.commit()
        assert "UNIQUE" in str(exc_info.value).upper()

    # 3. Downgrade back to 0001
    command.downgrade(alembic_cfg, "0001_initial_schema")
    inspector = inspect(engine)
    tables_downgraded = set(inspector.get_table_names())
    assert "webhook_inbox" not in tables_downgraded
    assert "jobs" not in tables_downgraded
    assert "invoices" in tables_downgraded


def test_d4_migration_0003_upgrade_downgrade(temp_sqlite_url):
    """Verify migration 0003 creates provider_settlements, safely migrates null decision_id allocations, and downgrades."""
    os.environ["RECONCILE_DB_URL"] = temp_sqlite_url
    alembic_cfg = Config("alembic.ini")
    alembic_cfg.set_main_option("sqlalchemy.url", temp_sqlite_url)

    # 1. Upgrade up to 0002
    command.upgrade(alembic_cfg, "0002_d4_webhooks_and_jobs")
    engine = DatabaseEngine(DatabaseConfig(url=temp_sqlite_url)).engine
    inspector = inspect(engine)
    tables_0002 = set(inspector.get_table_names())
    assert "provider_settlements" not in tables_0002

    db = DatabaseEngine(DatabaseConfig(url=temp_sqlite_url))
    with db.session() as session:
        # Create tenant, invoice, payment, and an unapproved historical allocation with decision_id=NULL
        session.execute(
            text("INSERT INTO tenants (id, name, created_at, updated_at) VALUES ('t_mig', 'Mig Tenant', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)")
        )
        session.execute(
            text(
                "INSERT INTO invoices (id, tenant_id, provider, provider_invoice_id, invoice_number, "
                "customer_name, total_minor, currency, status, issued_on, due_on, version, created_at, updated_at) "
                "VALUES ('inv_mig', 't_mig', 'paypal', 'INV-MIG-1', 'RECON-MIG-1', "
                "'Customer Mig', 10000, 'USD', 'SENT', '2026-01-01', '2026-01-15', 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )
        session.execute(
            text(
                "INSERT INTO payments (id, tenant_id, provider, provider_payment_id, amount_minor, currency, "
                "status, received_at, version, created_at, updated_at) "
                "VALUES ('pay_mig', 't_mig', 'paypal', 'EXTR-PAY-MIG-1', 10000, 'USD', "
                "'COMPLETED', CURRENT_TIMESTAMP, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )
        session.execute(
            text(
                "INSERT INTO allocations (id, tenant_id, payment_id, invoice_id, amount_minor, currency, "
                "decision_id, actor, evidence_json, allocated_at) "
                "VALUES ('alloc_mig_1', 't_mig', 'pay_mig', 'inv_mig', 10000, 'USD', "
                "NULL, 'paypal:settlement', '[\"PROVIDER_SETTLED\"]', CURRENT_TIMESTAMP)"
            )
        )
        session.commit()

    # 2. Upgrade to 0003_d4_provider_settlements
    command.upgrade(alembic_cfg, "0003_d4_provider_settlements")
    inspector = inspect(engine)
    tables_0003 = set(inspector.get_table_names())
    assert "provider_settlements" in tables_0003

    # Check migrated data in provider_settlements and verify allocations was cleared of NULL decision_id
    with db.session() as session:
        settlement = session.execute(
            text("SELECT * FROM provider_settlements WHERE id='alloc_mig_1'")
        ).fetchone()
        assert settlement is not None
        assert settlement.tenant_id == "t_mig"
        assert settlement.amount_minor == 10000
        assert settlement.currency == "USD"
        assert settlement.provider_settlement_id == "EXTR-PAY-MIG-1"

        allocs_count = session.execute(text("SELECT count(*) FROM allocations")).scalar()
        assert allocs_count == 0

        # Enforce that inserting an allocation with NULL decision_id is rejected by schema constraint
        with pytest.raises(Exception):
            session.execute(
                text(
                    "INSERT INTO allocations (id, tenant_id, payment_id, invoice_id, amount_minor, currency, "
                    "decision_id, actor, evidence_json, allocated_at) "
                    "VALUES ('alloc_invalid', 't_mig', 'pay_mig', 'inv_mig', 5000, 'USD', "
                    "NULL, 'operator', '[]', CURRENT_TIMESTAMP)"
                )
            )
            session.commit()
        session.rollback()

    # 3. Downgrade back to 0002
    command.downgrade(alembic_cfg, "0002_d4_webhooks_and_jobs")
    inspector = inspect(engine)
    tables_downgraded = set(inspector.get_table_names())
    assert "provider_settlements" not in tables_downgraded

    with db.session() as session:
        alloc = session.execute(
            text("SELECT * FROM allocations WHERE id='alloc_mig_1'")
        ).fetchone()
        assert alloc is not None
        assert alloc.decision_id is None
        assert alloc.amount_minor == 10000


def test_d5_migration_0004_upgrade_downgrade(temp_sqlite_url):
    """Verify migration 0004 creates refunds table, proposal columns, and downgrades cleanly."""
    os.environ["RECONCILE_DB_URL"] = temp_sqlite_url
    alembic_cfg = Config("alembic.ini")
    alembic_cfg.set_main_option("sqlalchemy.url", temp_sqlite_url)

    # 1. Upgrade up to 0003
    command.upgrade(alembic_cfg, "0003_d4_provider_settlements")
    engine = DatabaseEngine(DatabaseConfig(url=temp_sqlite_url)).engine
    inspector = inspect(engine)
    tables_0003 = set(inspector.get_table_names())
    assert "refunds" not in tables_0003

    # 2. Upgrade to 0004_d5_advanced_reconciliation
    command.upgrade(alembic_cfg, "0004_d5_advanced_reconciliation")
    inspector = inspect(engine)
    tables_0004 = set(inspector.get_table_names())
    assert "refunds" in tables_0004

    proposal_cols = {col["name"] for col in inspector.get_columns("proposals")}
    assert "excess_amount_minor" in proposal_cols
    assert "ambiguity_certificate_json" in proposal_cols

    # Test inserting a refund row
    db = DatabaseEngine(DatabaseConfig(url=temp_sqlite_url))
    with db.session() as session:
        session.execute(
            text("INSERT INTO tenants (id, name, created_at, updated_at) VALUES ('t_d5', 'D5 Tenant', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)")
        )
        session.execute(
            text(
                "INSERT INTO payments (id, tenant_id, provider, provider_payment_id, amount_minor, currency, "
                "status, received_at, version, created_at, updated_at) "
                "VALUES ('pay_d5', 't_d5', 'paypal', 'PAY-D5-1', 10000, 'USD', "
                "'COMPLETED', CURRENT_TIMESTAMP, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )
        session.execute(
            text(
                "INSERT INTO refunds (id, tenant_id, provider, provider_refund_id, parent_payment_id, amount_minor, currency, "
                "status, reason, refunded_at, created_at) "
                "VALUES ('ref_d5', 't_d5', 'paypal', 'REF-D5-1', 'pay_d5', 2000, 'USD', "
                "'COMPLETED', 'Customer requested', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )
        session.commit()

        ref_row = session.execute(text("SELECT * FROM refunds WHERE id='ref_d5'")).fetchone()
        assert ref_row is not None
        assert ref_row.amount_minor == 2000

    # 3. Downgrade back to 0003
    command.downgrade(alembic_cfg, "0003_d4_provider_settlements")
    inspector = inspect(engine)
    tables_downgraded = set(inspector.get_table_names())
    assert "refunds" not in tables_downgraded

    proposal_cols_after = {col["name"] for col in inspector.get_columns("proposals")}
    assert "excess_amount_minor" not in proposal_cols_after
    assert "ambiguity_certificate_json" not in proposal_cols_after


def test_d6_migration_0005_upgrade_downgrade(temp_sqlite_url):
    """Verify migration 0005 creates investigations table and downgrades cleanly."""
    os.environ["RECONCILE_DB_URL"] = temp_sqlite_url
    alembic_cfg = Config("alembic.ini")
    alembic_cfg.set_main_option("sqlalchemy.url", temp_sqlite_url)

    # 1. Upgrade up to 0004
    command.upgrade(alembic_cfg, "0004_d5_advanced_reconciliation")
    engine = DatabaseEngine(DatabaseConfig(url=temp_sqlite_url)).engine
    inspector = inspect(engine)
    tables_0004 = set(inspector.get_table_names())
    assert "investigations" not in tables_0004

    # 2. Upgrade to 0005_d6_ai_investigations
    command.upgrade(alembic_cfg, "0005_d6_ai_investigations")
    inspector = inspect(engine)
    tables_0005 = set(inspector.get_table_names())
    assert "investigations" in tables_0005

    # Test inserting an investigation row
    db = DatabaseEngine(DatabaseConfig(url=temp_sqlite_url))
    with db.session() as session:
        session.execute(
            text("INSERT INTO tenants (id, name, created_at, updated_at) VALUES ('t_d6', 'D6 Tenant', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)")
        )
        session.execute(
            text(
                "INSERT INTO investigations (id, tenant_id, target_type, target_id, status, classification, created_at) "
                "VALUES ('invg_test_1', 't_d6', 'payment', 'PAY-TEST-1', 'QUEUED', 'UNMATCHED_PAYMENT', CURRENT_TIMESTAMP)"
            )
        )
        session.commit()

        invg_row = session.execute(text("SELECT * FROM investigations WHERE id='invg_test_1'")).fetchone()
        assert invg_row is not None
        assert invg_row.target_id == "PAY-TEST-1"
        assert invg_row.classification == "UNMATCHED_PAYMENT"

    # 3. Downgrade back to 0004
    command.downgrade(alembic_cfg, "0004_d5_advanced_reconciliation")
    inspector = inspect(engine)
    tables_downgraded = set(inspector.get_table_names())
    assert "investigations" not in tables_downgraded

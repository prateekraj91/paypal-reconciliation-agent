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
        "audit_events",
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

"""Persistence tests: database restarts, idempotent ingestion, and tenant isolation."""

from __future__ import annotations

import os
import tempfile
import pytest
from datetime import datetime, timezone, date

from alembic.config import Config
from alembic import command

from reconcile_store.config import DatabaseConfig
from reconcile_store.db import DatabaseEngine
from reconcile_store.repositories import (
    TenantRepository,
    ConnectionRepository,
    ProviderRecordRepository,
    InvoiceRepository,
    PaymentRepository,
    ConflictingSourceRecordError,
    TenantIsolationError,
    ConflictError,
)
from reconcile.models import Invoice, Payment, InvoiceStatus, PaymentStatus


@pytest.fixture
def persistent_db_path():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    url = f"sqlite:///{path}"
    os.environ["RECONCILE_DB_URL"] = url
    alembic_cfg = Config("alembic.ini")
    alembic_cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(alembic_cfg, "head")

    yield path, url

    if os.path.exists(path):
        os.unlink(path)


def test_data_survives_database_restart(persistent_db_path):
    """Verify that records committed to the database persist across engine/connection restarts."""
    path, url = persistent_db_path

    # Session 1: Create tenant, invoice, and payment
    db1 = DatabaseEngine(DatabaseConfig(url=url))
    with db1.session() as session:
        t_repo = TenantRepository(session)
        inv_repo = InvoiceRepository(session)
        pay_repo = PaymentRepository(session)

        tenant = t_repo.create_tenant("tenant_alpha", "Alpha Corp")
        inv = inv_repo.upsert_invoice(
            tenant_id="tenant_alpha",
            provider="paypal",
            provider_invoice_id="INV-PERSIST-1",
            invoice_number="INV-1001",
            total_minor=15000,
            currency="USD",
            provider_due_minor=15000,
            status="SENT",
            customer_name="Acme Corp",
        )
        pay = pay_repo.upsert_payment(
            tenant_id="tenant_alpha",
            provider="paypal",
            provider_payment_id="PAY-PERSIST-1",
            amount_minor=15000,
            currency="USD",
            status="COMPLETED",
            payer_name="Acme Corp",
            reference="INV-1001",
        )
        session.commit()

    # Dispose engine 1 completely (simulating process restart)
    db1.dispose()

    # Session 2: Connect via brand new DatabaseEngine
    db2 = DatabaseEngine(DatabaseConfig(url=url))
    with db2.session() as session:
        t_repo2 = TenantRepository(session)
        inv_repo2 = InvoiceRepository(session)
        pay_repo2 = PaymentRepository(session)

        tenant2 = t_repo2.get_tenant("tenant_alpha")
        assert tenant2 is not None
        assert tenant2.name == "Alpha Corp"

        inv2 = inv_repo2.get_by_provider_id("tenant_alpha", "paypal", "INV-PERSIST-1")
        assert inv2 is not None
        assert inv2.total_minor == 15000
        assert inv2.currency == "USD"
        assert inv2.customer_name == "Acme Corp"

        pay2 = pay_repo2.get_by_provider_id("tenant_alpha", "paypal", "PAY-PERSIST-1")
        assert pay2 is not None
        assert pay2.amount_minor == 15000
        assert pay2.status == "COMPLETED"
        assert pay2.reference == "INV-1001"

    db2.dispose()


def test_repeated_import_idempotency_and_conflict_handling(persistent_db_path):
    """Verify provider records preserve SHA-256 hashes, deduplicate identical imports, and reject conflicts."""
    _, url = persistent_db_path
    db = DatabaseEngine(DatabaseConfig(url=url))

    with db.session() as session:
        t_repo = TenantRepository(session)
        p_repo = ProviderRecordRepository(session)
        t_repo.create_tenant("tenant_bravo", "Bravo Corp")

        raw_payload = {
            "id": "INV2-9999",
            "status": "SENT",
            "amount": {"value": "100.00", "currency_code": "USD"},
            "detail": {"note": "Test invoice"},
        }

        # First import
        rec1 = p_repo.store_record(
            tenant_id="tenant_bravo",
            provider="paypal",
            record_kind="invoice",
            provider_record_id="INV2-9999",
            raw_payload=raw_payload,
        )
        session.commit()
        hash1 = rec1.content_hash
        id1 = rec1.id
        assert len(hash1) == 64

        # Identical import must return existing record idempotently
        rec2 = p_repo.store_record(
            tenant_id="tenant_bravo",
            provider="paypal",
            record_kind="invoice",
            provider_record_id="INV2-9999",
            raw_payload=raw_payload,
        )
        assert rec2.id == id1
        assert rec2.content_hash == hash1

        # Conflicting import with same ID but different payload must fail closed (I5)
        conflicting_payload = {
            "id": "INV2-9999",
            "status": "PAID",
            "amount": {"value": "200.00", "currency_code": "USD"},
        }
        with pytest.raises(ConflictingSourceRecordError):
            p_repo.store_record(
                tenant_id="tenant_bravo",
                provider="paypal",
                record_kind="invoice",
                provider_record_id="INV2-9999",
                raw_payload=conflicting_payload,
                fail_on_conflict=True,
            )


def test_tenant_boundaries_and_isolation(persistent_db_path):
    """Verify that tenant boundaries are strictly isolated."""
    _, url = persistent_db_path
    db = DatabaseEngine(DatabaseConfig(url=url))

    with db.session() as session:
        t_repo = TenantRepository(session)
        inv_repo = InvoiceRepository(session)
        pay_repo = PaymentRepository(session)

        t_repo.create_tenant("tenant_1", "Tenant One")
        t_repo.create_tenant("tenant_2", "Tenant Two")

        inv_repo.upsert_invoice(
            tenant_id="tenant_1",
            provider="paypal",
            provider_invoice_id="INV-TENANT-1",
            invoice_number="INV-T1",
            total_minor=5000,
            currency="USD",
            customer_name="Client 1",
        )
        session.commit()

        # Tenant 2 cannot query Tenant 1's invoice
        t2_inv = inv_repo.get_by_provider_id("tenant_2", "paypal", "INV-TENANT-1")
        assert t2_inv is None

        # Trying to access with explicit wrong tenant ID raises TenantIsolationError
        inv1 = inv_repo.get_by_provider_id("tenant_1", "paypal", "INV-TENANT-1")
        assert inv1 is not None
        with pytest.raises(TenantIsolationError):
            inv_repo.get_invoice("tenant_2", inv1.id)

"""One PayPal transaction seen through several provider views stays one payment."""

from __future__ import annotations

import copy
import json
import os
import pathlib
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from reconcile.models import AllocationStatus, InvoiceStatus, Payment, PaymentAllocation, PaymentStatus, Invoice
from reconcile.paypal import PayPalClient
from reconcile_store.approval import ApprovalService
from reconcile_store.config import DatabaseConfig
from reconcile_store.db import DatabaseEngine
from reconcile_store.ledger import compute_payment_ledger
from reconcile_store.repositories import (
    AuditRepository,
    InvoiceRepository,
    PaymentRepository,
    PaymentSourceConflictError,
    ProviderRecordRepository,
    SOURCE_INVOICE_EMBEDDED,
    SOURCE_TRANSACTION_SEARCH,
    SOURCE_WEBHOOK_CAPTURE,
    TenantRepository,
)
from reconcile_store.repositories.job_repo import JobRepository
from reconcile_store.repositories.tenant_repo import ConnectionRepository
from reconcile_store.schema import (
    AuditEventRecord,
    PaymentRecord,
    ProposalRecord,
    ProviderRecord,
    ProviderSettlementRecord,
)
from reconcile_store.workflow import StorageWorkflow
from reconcile_worker.worker import Worker

FIXTURE = pathlib.Path(__file__).parent.parent / "fixtures" / "sandbox_invoice_detail_paid.json"
TX_A, TX_B, TX_C = "73D698597Y545842G", "36025136YX755164U", "61V19155E37842600"
PAID_AT = datetime(2026, 11, 4, 6, 9, 1, tzinfo=timezone.utc)


def invoice_domain(invoice_id: str, number: str, cents: int, currency: str = "USD") -> Invoice:
    return Invoice(
        invoice_id=invoice_id,
        number=number,
        customer_name="Sandbox Buyer",
        customer_email="buyer@example.test",
        amount_cents=cents,
        currency=currency,
        issued_on=PAID_AT.date(),
        due_on=PAID_AT.date(),
        status=InvoiceStatus.SENT,
    )


def search_payment(tx: str, cents: int, currency="USD", **over) -> Payment:
    fields = dict(
        payment_id=tx,
        amount_cents=cents,
        currency=currency,
        received_at=PAID_AT,
        payer_name="Ada Buyer",
        payer_email="ada@example.test",
        reference="RECON-US-001",
        note="",
        status=PaymentStatus.COMPLETED,
    )
    fields.update(over)
    return Payment(**fields)


def search_raw(tx: str, value: str, currency="USD", invoice="RECON-US-001", payer=True) -> dict:
    raw = {
        "transaction_info": {
            "transaction_id": tx,
            "transaction_event_code": "T0006",
            "transaction_status": "S",
            "transaction_initiation_date": "2026-11-04T06:09:01Z",
            "transaction_amount": {"currency_code": currency, "value": value},
        }
    }
    if invoice:
        raw["transaction_info"]["invoice_id"] = invoice
    if payer:
        raw["payer_info"] = {
            "email_address": "ada@example.test",
            "payer_name": {"alternate_full_name": "Ada Buyer"},
        }
    return raw


def embedded_raw(tx: str, value: str, currency="USD", note="Thanks for invoice 1") -> dict:
    return {
        "payment_id": tx,
        "type": "PAYPAL",
        "method": "PAYPAL",
        "note": note,
        "payment_date": "2026-11-04",
        "amount": {"currency_code": currency, "value": value},
    }


def invoice_raw(invoice_id: str, number: str, value: str, txns: list[dict]) -> dict:
    raw = copy.deepcopy(json.loads(FIXTURE.read_text()))
    money = {"currency_code": "USD", "value": value}
    raw["id"] = invoice_id
    raw["detail"]["invoice_number"] = number
    raw["amount"]["value"] = value
    raw["amount"]["breakdown"]["item_total"]["value"] = value
    raw["due_amount"]["value"] = "0.00"
    raw["payments"] = {"paid_amount": money, "transactions": txns}
    return raw


class StubPayPal(PayPalClient):
    """The real PayPal mapping code over canned provider responses."""

    def __init__(self, invoices: list[dict], transactions: list[dict]):
        super().__init__(client_id="x", client_secret="y", base_url="https://api-m.sandbox.paypal.com")
        self.raw_invoices, self.raw_transactions = invoices, transactions
        self.search_fails = False

    def list_all_raw_invoices(self, *args, **kwargs):
        return copy.deepcopy(self.raw_invoices)

    def list_transactions(self, start, end, **kwargs):
        if self.search_fails:
            raise RuntimeError("search unavailable")
        return self.map_transactions(copy.deepcopy(self.raw_transactions))


@pytest.fixture(params=["sqlite", "postgresql"])
def store(request, tmp_path):
    if request.param == "postgresql":
        url = os.environ.get("TEST_POSTGRESQL_URL")
        if not url:
            pytest.skip("TEST_POSTGRESQL_URL is not set")
    else:
        url = f"sqlite:///{tmp_path / 'merge.db'}"
    engine = DatabaseEngine(DatabaseConfig(url=url))
    engine.init_schema()
    tenant = f"merge_{os.urandom(6).hex()}"
    with engine.session() as s:
        TenantRepository(s).create_tenant(tenant, "Merge")
        s.commit()
    yield engine, tenant
    engine.dispose()


def seed_invoice(s, tenant, invoice_id="INV2-A", number="RECON-US-001", cents=12500, currency="USD"):
    StorageWorkflow(s).ingest_invoices(tenant, [invoice_domain(invoice_id, number, cents, currency)])


def ingest_search(s, tenant, payment: Payment, raw: dict):
    StorageWorkflow(s).ingest_payments(
        tenant, [payment], {payment.payment_id: raw}, source_view=SOURCE_TRANSACTION_SEARCH
    )


def ingest_embedded(s, tenant, tx, cents, raw, invoice_id="INV2-A", currency="USD"):
    allocation = PaymentAllocation(tx, invoice_id, cents, AllocationStatus.APPROVED, currency)
    StorageWorkflow(s).ingest_provider_settlements(tenant, [allocation], {tx: raw})


def payments(s, tenant):
    return list(s.scalars(select(PaymentRecord).where(PaymentRecord.tenant_id == tenant)).all())


def view_kinds(s, tenant, tx):
    return sorted(
        s.scalars(
            select(ProviderRecord.record_kind).where(
                ProviderRecord.tenant_id == tenant, ProviderRecord.provider_record_id == tx
            )
        ).all()
    )


def fields(p: PaymentRecord) -> tuple:
    received = p.received_at if p.received_at.tzinfo else p.received_at.replace(tzinfo=timezone.utc)
    return (p.amount_minor, p.currency, p.status, p.payer_name, p.payer_email, p.reference, p.note, received)


def merge_in_order(store, order):
    engine, tenant = store
    with engine.session() as s:
        seed_invoice(s, tenant)
        for step in order:
            if step == "search":
                ingest_search(s, tenant, search_payment(TX_A, 12500, note=""), search_raw(TX_A, "125.00"))
            else:
                ingest_embedded(s, tenant, TX_A, 12500, embedded_raw(TX_A, "125.00"))
        s.expire_all()
        rows = payments(s, tenant)
        assert len(rows) == 1
        return fields(rows[0]), view_kinds(s, tenant, TX_A), rows[0].source_record_id, tenant


EXPECTED = (
    12500, "USD", "COMPLETED", "Ada Buyer", "ada@example.test", "RECON-US-001", "Thanks for invoice 1", PAID_AT,
)


def test_reporting_then_invoice_merges(store):
    got, kinds, _, _ = merge_in_order(store, ["search", "embedded"])
    assert got == EXPECTED
    assert kinds == [SOURCE_INVOICE_EMBEDDED, SOURCE_TRANSACTION_SEARCH]


def test_invoice_then_reporting_gives_identical_result(store):
    got, kinds, _, _ = merge_in_order(store, ["embedded", "search"])
    assert got == EXPECTED
    assert kinds == [SOURCE_INVOICE_EMBEDDED, SOURCE_TRANSACTION_SEARCH]


@pytest.mark.parametrize("order", [["search", "embedded"], ["embedded", "search"]])
def test_canonical_source_pointer_is_the_strongest_view(store, order):
    engine = store[0]
    _, _, source_id, tenant = merge_in_order(store, order)
    with engine.session() as s:
        assert s.get(ProviderRecord, source_id).record_kind == SOURCE_TRANSACTION_SEARCH


def test_cash_is_counted_once(store):
    engine = store[0]
    _, _, _, tenant = merge_in_order(store, ["embedded", "search"])
    with engine.session() as s:
        payment = payments(s, tenant)[0]
        ledger = compute_payment_ledger(s, tenant, payment)
        assert (ledger.gross_minor, ledger.net_applied_minor, ledger.unapplied_minor) == (12500, 12500, 0)
        invoice = InvoiceRepository(s).get_by_provider_id(tenant, "paypal", "INV2-A")
        assert InvoiceRepository(s).get_ledger_outstanding_minor(tenant, invoice.id) == 0
        assert s.scalar(
            select(func.count()).select_from(ProviderSettlementRecord).where(ProviderSettlementRecord.tenant_id == tenant)
        ) == 1


@pytest.mark.parametrize("first", ["search", "embedded"])
def test_missing_payer_never_erases_and_placeholder_never_wins(store, first):
    engine, tenant = store
    with engine.session() as s:
        seed_invoice(s, tenant)
        steps = [
            lambda: ingest_search(s, tenant, search_payment(TX_A, 12500), search_raw(TX_A, "125.00")),
            lambda: ingest_embedded(s, tenant, TX_A, 12500, embedded_raw(TX_A, "125.00")),
        ]
        for step in steps if first == "search" else reversed(steps):
            step()
        blank = search_payment(TX_A, 12500, payer_name="", payer_email=None, reference=None)
        ingest_search(s, tenant, blank, search_raw(TX_A, "125.00", invoice=None, payer=False))
        s.expire_all()
        row = payments(s, tenant)[0]
        assert (row.payer_name, row.payer_email, row.reference) == ("Ada Buyer", "ada@example.test", "RECON-US-001")


@pytest.mark.parametrize("first", ["search", "embedded"])
def test_invoice_view_enriches_reference_and_remittance(store, first):
    engine, tenant = store
    with engine.session() as s:
        seed_invoice(s, tenant)
        steps = [
            lambda: ingest_search(
                s, tenant, search_payment(TX_B, 12500, reference=None), search_raw(TX_B, "125.00", invoice=None)
            ),
            lambda: ingest_embedded(s, tenant, TX_B, 12500, embedded_raw(TX_B, "125.00", note="Remit: order 88")),
        ]
        for step in steps if first == "search" else reversed(steps):
            step()
        s.expire_all()
        row = payments(s, tenant)[0]
        assert (row.reference, row.note, row.payer_name) == ("RECON-US-001", "Remit: order 88", "Ada Buyer")


@pytest.mark.parametrize("first", ["search", "embedded"])
def test_stronger_view_keeps_its_reference_when_views_disagree(store, first):
    engine, tenant = store
    with engine.session() as s:
        seed_invoice(s, tenant, number="OTHER-INVOICE-NO")
        steps = [
            lambda: ingest_search(s, tenant, search_payment(TX_A, 12500), search_raw(TX_A, "125.00")),
            lambda: ingest_embedded(s, tenant, TX_A, 12500, embedded_raw(TX_A, "125.00")),
        ]
        for step in steps if first == "search" else reversed(steps):
            step()
        s.expire_all()
        assert payments(s, tenant)[0].reference == "RECON-US-001"


@pytest.mark.parametrize("first", ["search", "embedded"])
def test_conflicting_amounts_fail_closed(store, first):
    engine, tenant = store
    with engine.session() as s:
        seed_invoice(s, tenant, cents=20000)
        steps = [
            lambda: ingest_search(s, tenant, search_payment(TX_A, 12500), search_raw(TX_A, "125.00")),
            lambda: ingest_embedded(s, tenant, TX_A, 12000, embedded_raw(TX_A, "120.00")),
        ]
        ordered = steps if first == "search" else list(reversed(steps))
        ordered[0]()
        with pytest.raises(PaymentSourceConflictError, match="Conflicting payment amount.*minor units"):
            ordered[1]()
        s.rollback()
        s.expire_all()
        assert len(payments(s, tenant)) == 1


@pytest.mark.parametrize("first", ["search", "embedded"])
def test_conflicting_currencies_fail_closed(store, first):
    engine, tenant = store
    with engine.session() as s:
        seed_invoice(s, tenant)
        steps = [
            lambda: ingest_search(s, tenant, search_payment(TX_A, 12500, currency="EUR"), search_raw(TX_A, "125.00", "EUR")),
            lambda: ingest_embedded(s, tenant, TX_A, 12500, embedded_raw(TX_A, "125.00")),
        ]
        ordered = steps if first == "search" else list(reversed(steps))
        ordered[0]()
        with pytest.raises(PaymentSourceConflictError, match="currency"):
            ordered[1]()


def test_search_revision_with_different_amount_fails_closed(store):
    engine, tenant = store
    with engine.session() as s:
        ingest_search(s, tenant, search_payment(TX_A, 12500), search_raw(TX_A, "125.00"))
        with pytest.raises(PaymentSourceConflictError):
            ingest_search(s, tenant, search_payment(TX_A, 12600), search_raw(TX_A, "126.00"))


def test_revised_view_is_kept_beside_the_original(store):
    engine, tenant = store
    with engine.session() as s:
        ingest_search(s, tenant, search_payment(TX_A, 12500), search_raw(TX_A, "125.00"))
        revised = search_raw(TX_A, "125.00")
        revised["transaction_info"]["transaction_updated_date"] = "2026-11-05T00:00:00Z"
        ingest_search(s, tenant, search_payment(TX_A, 12500), revised)
        assert view_kinds(s, tenant, TX_A) == [SOURCE_TRANSACTION_SEARCH, SOURCE_TRANSACTION_SEARCH]
        assert len(payments(s, tenant)) == 1


def test_webhook_view_merges_with_reporting_and_ranks_below_it(store):
    engine, tenant = store
    with engine.session() as s:
        hook = search_payment(TX_A, 12500, payer_name="PayPal Customer", payer_email=None, reference="HOOK-REF", note="hook")
        StorageWorkflow(s).ingest_payments(tenant, [hook], {TX_A: {"id": TX_A}}, source_view=SOURCE_WEBHOOK_CAPTURE)
        ingest_search(s, tenant, search_payment(TX_A, 12500), search_raw(TX_A, "125.00"))
        s.expire_all()
        row = payments(s, tenant)[0]
        assert (row.payer_name, row.reference, row.note) == ("Ada Buyer", "RECON-US-001", "hook")


def test_legacy_payment_source_row_is_preserved_and_does_not_block(store):
    engine, tenant = store
    with engine.session() as s:
        seed_invoice(s, tenant)
        legacy = ProviderRecordRepository(s).store_record(tenant, "paypal", "payment", TX_A, embedded_raw(TX_A, "125.00"))
        legacy_id = legacy.id
        ingest_embedded(s, tenant, TX_A, 12500, embedded_raw(TX_A, "125.00"))
        ingest_search(s, tenant, search_payment(TX_A, 12500), search_raw(TX_A, "125.00"))
        assert s.get(ProviderRecord, legacy_id) is not None
        assert len(payments(s, tenant)) == 1


def test_split_payment_shares_reconcile_to_gross_in_either_order(store):
    engine, tenant = store
    for first, tx in (("search", TX_A), ("embedded", TX_B)):
        with engine.session() as s:
            suffix = tx[:4]
            seed_invoice(s, tenant, f"S1-{suffix}", f"N1-{suffix}", 7000)
            seed_invoice(s, tenant, f"S2-{suffix}", f"N2-{suffix}", 5500)
            allocations = [
                PaymentAllocation(tx, f"S1-{suffix}", 7000, AllocationStatus.APPROVED, "USD"),
                PaymentAllocation(tx, f"S2-{suffix}", 5500, AllocationStatus.APPROVED, "USD"),
            ]
            steps = [
                lambda: ingest_search(s, tenant, search_payment(tx, 12500), search_raw(tx, "125.00")),
                lambda: StorageWorkflow(s).ingest_provider_settlements(tenant, allocations, {tx: embedded_raw(tx, "70.00")}),
            ]
            for step in steps if first == "search" else reversed(steps):
                step()
            row = PaymentRepository(s).get_by_provider_id(tenant, "paypal", tx)
            assert row.amount_minor == 12500
            assert compute_payment_ledger(s, tenant, row).unapplied_minor == 0


def test_different_transaction_ids_do_not_merge(store):
    engine, tenant = store
    with engine.session() as s:
        ingest_search(s, tenant, search_payment(TX_B, 500), search_raw(TX_B, "5.00"))
        ingest_search(s, tenant, search_payment(TX_C, 500), search_raw(TX_C, "5.00"))
        assert len(payments(s, tenant)) == 2


def test_tenants_never_merge(store):
    engine, tenant = store
    other = f"{tenant}_b"
    with engine.session() as s:
        TenantRepository(s).create_tenant(other, "Other")
        s.commit()
        ingest_search(s, tenant, search_payment(TX_A, 12500), search_raw(TX_A, "125.00"))
        # Another merchant's record with the same id and different money is not a conflict.
        ingest_search(s, other, search_payment(TX_A, 999, currency="EUR"), search_raw(TX_A, "9.99", "EUR"))
        assert (payments(s, tenant)[0].amount_minor, payments(s, other)[0].amount_minor) == (12500, 999)


def sandbox_stub(transactions=None) -> StubPayPal:
    invoices = [
        invoice_raw("INV2-1", "RECON-US-001", "125.00", [embedded_raw(TX_A, "125.00")]),
        invoice_raw("INV2-2", "RECON-US-002", "5.00", [embedded_raw(TX_B, "5.00")]),
        invoice_raw("INV2-3", "RECON-US-003", "1.00", [embedded_raw(TX_C, "1.00")]),
    ]
    if transactions is None:
        transactions = [
            search_raw(TX_A, "125.00", invoice="RECON-US-001"),
            search_raw(TX_B, "5.00", invoice="RECON-US-002"),
            search_raw(TX_C, "1.00", invoice="RECON-US-003"),
        ]
    return StubPayPal(invoices, transactions)


def run_sync_job(engine, tenant, stub, key):
    with engine.session() as s:
        if ConnectionRepository(s).get_connection(tenant, provider="paypal", environment="sandbox") is None:
            ConnectionRepository(s).create_connection(tenant, "paypal", "sandbox", "env:PAYPAL_SANDBOX")
        now = datetime.now(timezone.utc)
        JobRepository(s).enqueue_job(
            tenant_id=tenant,
            job_type="MANUAL_SYNC",
            payload={
                "environment": "sandbox",
                "start_date": (now - timedelta(days=30)).isoformat(),
                "end_date": now.isoformat(),
            },
            idempotency_key=key,
        )
        s.commit()
    worker = Worker(db_engine=engine, paypal_client=stub)
    worker.resolve_paypal_client_for_connection = lambda conn: stub
    return worker.run_once(tenant_id=tenant)


def state(engine, tenant):
    with engine.session() as s:
        rows = sorted(payments(s, tenant), key=lambda p: p.provider_payment_id)
        count = lambda model: s.scalar(select(func.count()).select_from(model).where(model.tenant_id == tenant))
        settled_events = s.scalar(
            select(func.count()).select_from(AuditEventRecord).where(
                AuditEventRecord.tenant_id == tenant, AuditEventRecord.event_type == "PROVIDER_SETTLEMENT_RECORDED"
            )
        )
        return {
            "payments": [(fields(p), p.version, p.source_record_id) for p in rows],
            "records": count(ProviderRecord),
            "settlements": count(ProviderSettlementRecord),
            "settled_events": settled_events,
            "cash": sum(p.amount_minor for p in rows),
        }


def test_repeated_thirty_day_sync_is_idempotent(store):
    engine, tenant = store
    assert run_sync_job(engine, tenant, sandbox_stub(), "sync-1").status == "COMPLETED"
    first = state(engine, tenant)
    assert run_sync_job(engine, tenant, sandbox_stub(), "sync-2").status == "COMPLETED"
    assert state(engine, tenant) == first
    assert (len(first["payments"]), first["cash"], first["settlements"], first["settled_events"]) == (3, 13100, 3, 3)
    assert first["records"] == 3 + 6  # three invoices, two views of each payment


def test_same_transaction_across_pages_is_one_payment(store):
    engine, tenant = store
    pages = [search_raw(TX_A, "125.00"), search_raw(TX_B, "5.00", invoice="RECON-US-002"),
             search_raw(TX_A, "125.00"), search_raw(TX_C, "1.00", invoice="RECON-US-003")]
    assert run_sync_job(engine, tenant, sandbox_stub(pages), "sync-p").status == "COMPLETED"
    assert state(engine, tenant)["cash"] == 13100


def test_partial_sync_then_full_retry_completes(store):
    engine, tenant = store
    partial = sandbox_stub()
    partial.search_fails = True
    assert run_sync_job(engine, tenant, partial, "sync-a").status == "COMPLETED"
    with engine.session() as s:
        assert payments(s, tenant)[0].payer_name == "PayPal Settled Payment"
    assert run_sync_job(engine, tenant, sandbox_stub(), "sync-b").status == "COMPLETED"
    after = state(engine, tenant)
    assert after["cash"] == 13100
    with engine.session() as s:
        assert {p.payer_name for p in payments(s, tenant)} == {"Ada Buyer"}


def test_financial_conflict_fails_the_job_without_retry(store):
    engine, tenant = store
    bad = sandbox_stub([search_raw(TX_A, "124.00")])
    job = run_sync_job(engine, tenant, bad, "sync-x")
    with engine.session() as s:
        stored = JobRepository(s).get_job(tenant, job.id)
        assert stored.status == "FAILED"
        assert "Conflicting payment amount" in (stored.last_error or "")


def test_invoice_revision_after_payment_does_not_block_sync(store):
    engine, tenant = store
    assert run_sync_job(engine, tenant, sandbox_stub(), "sync-1").status == "COMPLETED"
    stub = sandbox_stub()
    stub.raw_invoices[0]["detail"]["metadata"]["last_update_time"] = "2026-11-08T00:00:00Z"
    assert run_sync_job(engine, tenant, stub, "sync-2").status == "COMPLETED"
    assert state(engine, tenant)["cash"] == 13100


def test_merged_open_payment_still_matches_and_approves_once(store):
    engine, tenant = store
    with engine.session() as s:
        seed_invoice(s, tenant)
        hook = search_payment(TX_A, 12500, payer_name="PayPal Customer", payer_email=None)
        StorageWorkflow(s).ingest_payments(tenant, [hook], {TX_A: {"id": TX_A}}, source_view=SOURCE_WEBHOOK_CAPTURE)
        ingest_search(s, tenant, search_payment(TX_A, 12500), search_raw(TX_A, "125.00"))
        StorageWorkflow(s).run_reconciliation(tenant)
        proposal = s.scalars(
            select(ProposalRecord).where(ProposalRecord.tenant_id == tenant, ProposalRecord.outcome == "PROPOSED")
        ).one()
        ApprovalService(s).approve_proposal(tenant, proposal.id, "operator", "approve-1")
        s.commit()
        ledger = compute_payment_ledger(s, tenant, payments(s, tenant)[0])
        assert (ledger.net_applied_minor, ledger.unapplied_minor) == (12500, 0)
        AuditRepository(s).verify_tenant_chain(tenant)

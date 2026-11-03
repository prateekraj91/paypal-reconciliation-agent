"""The repeatable synchronisation pass: `python -m reconcile sync --sandbox`.

The property that matters most is that running it twice changes nothing. A sync
that double-counts on its second run is worse than one that fails, because the
ledger drifts quietly.
"""

from __future__ import annotations

import json
import pathlib
from datetime import datetime, timedelta, timezone

import pytest

from reconcile.models import (
    AllocationStatus,
    Invoice,
    InvoiceStatus,
    Payment,
    PaymentAllocation,
    PaymentStatus,
)
from reconcile.sync import SyncReport, SyncResult, run_sync

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
UTC = timezone.utc


def load(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text())


class FakeConnector:
    """Stands in for PayPalClient, serving the committed real fixtures."""

    def __init__(self, invoices=None, allocations=None, ingest=None, fail=None):
        self._invoices = invoices if invoices is not None else ()
        self._allocations = allocations if allocations is not None else ()
        self._ingest = ingest
        self._fail = fail
        self.base_url = "https://api-m.sandbox.paypal.com"
        self.invoice_calls = 0
        self.transaction_calls = 0

    def list_all_invoices_with_allocations(self, **_kw):
        self.invoice_calls += 1
        if self._fail == "invoices":
            raise RuntimeError("invoice fetch exploded")
        return list(self._invoices), list(self._allocations)

    def list_transactions(self, start, end, **_kw):
        self.transaction_calls += 1
        if self._fail == "transactions":
            from reconcile.paypal import PayPalAPIError
            raise PayPalAPIError(403, "Transaction Search not permitted")
        from reconcile.paypal import TransactionIngest
        return self._ingest if self._ingest is not None else TransactionIngest()


def inv(number, total, due=None, *, status=InvoiceStatus.SENT, currency="USD", invoice_id=None):
    return Invoice(
        invoice_id=invoice_id or f"INV2-{number}",
        number=number,
        customer_name="Acme",
        amount_cents=total,
        currency=currency,
        issued_on=datetime(2026, 10, 1, tzinfo=UTC).date(),
        due_on=datetime(2026, 11, 1, tzinfo=UTC).date(),
        status=status,
        due_amount_cents=total if due is None else due,
    )


def pmt(pid, amount, *, reference=None, note="", currency="USD", minutes=0,
        status=PaymentStatus.COMPLETED):
    return Payment(
        payment_id=pid,
        amount_cents=amount,
        currency=currency,
        received_at=datetime(2026, 10, 15, tzinfo=UTC) + timedelta(minutes=minutes),
        payer_name="Acme",
        reference=reference,
        note=note,
        status=status,
    )


def ingest(*payments):
    from reconcile.paypal import TransactionIngest
    return TransactionIngest(
        payments=tuple(payments),
        raw_by_id={p.payment_id: {"transaction_info": {"transaction_id": p.payment_id}} for p in payments},
    )


def window():
    end = datetime(2026, 11, 1, tzinfo=UTC)
    return end - timedelta(days=30), end


# --------------------------------------------------------------------------
# Shape of a run
# --------------------------------------------------------------------------


def test_a_sync_reports_meaningful_counts():
    start, end = window()
    connector = FakeConnector(
        invoices=[inv("RECON-T-001", 12500)],
        ingest=ingest(pmt("T1", 12500, reference="RECON-T-001")),
    )
    result = run_sync(connector, start, end)

    assert isinstance(result, SyncResult)
    report = result.report
    assert isinstance(report, SyncReport)
    assert report.invoices_ingested == 1
    assert report.transactions_ingested == 1
    assert report.payments_ingested == 1
    assert report.proposals == 1
    assert report.passed_verification == 1
    assert report.accounting_errors == ()


def test_a_sync_with_nothing_to_do_is_not_an_error():
    start, end = window()
    result = run_sync(FakeConnector(), start, end)
    assert result.report.invoices_ingested == 0
    assert result.report.payments_ingested == 0
    assert result.report.proposals == 0
    assert result.ok


def test_invoices_and_transactions_are_both_fetched():
    start, end = window()
    connector = FakeConnector(invoices=[inv("RECON-T-001", 12500)])
    run_sync(connector, start, end)
    assert connector.invoice_calls == 1
    assert connector.transaction_calls == 1


def test_source_records_are_preserved_on_the_result():
    start, end = window()
    payment = pmt("T1", 12500, reference="RECON-T-001")
    connector = FakeConnector(invoices=[inv("RECON-T-001", 12500)], ingest=ingest(payment))
    result = run_sync(connector, start, end)

    assert result.transaction_evidence["T1"]["transaction_info"]["transaction_id"] == "T1"
    assert [i.number for i in result.invoices] == ["RECON-T-001"]
    assert [p.payment_id for p in result.payments] == ["T1"]


# --------------------------------------------------------------------------
# Idempotency: the point of the whole exercise
# --------------------------------------------------------------------------


def test_running_twice_produces_identical_reports():
    start, end = window()

    def fresh():
        return FakeConnector(
            invoices=[inv("RECON-T-001", 12500), inv("RECON-T-002", 25000)],
            ingest=ingest(
                pmt("T1", 12500, reference="RECON-T-001"),
                pmt("T2", 25000, reference="RECON-T-002", minutes=1),
            ),
        )

    first = run_sync(fresh(), start, end).report
    second = run_sync(fresh(), start, end).report
    assert first == second


def test_a_second_run_creates_no_additional_financial_records():
    start, end = window()
    connector = FakeConnector(
        invoices=[inv("RECON-T-001", 12500)],
        ingest=ingest(pmt("T1", 12500, reference="RECON-T-001")),
    )
    first = run_sync(connector, start, end)
    second = run_sync(connector, start, end)

    assert len(second.payments) == len(first.payments)
    assert len(second.proposed_allocations) == len(first.proposed_allocations)
    assert {(a.payment_id, a.invoice_id, a.amount_cents) for a in second.proposed_allocations} == \
           {(a.payment_id, a.invoice_id, a.amount_cents) for a in first.proposed_allocations}


def test_a_payment_already_approved_in_the_ledger_is_not_proposed_again():
    """The second run's ledger contains the first run's approved allocation."""
    start, end = window()
    payment = pmt("T1", 12500, reference="RECON-T-001")
    invoice = inv("RECON-T-001", 12500, due=0, status=InvoiceStatus.PAID)
    settled = PaymentAllocation(
        "T1", invoice.invoice_id, 12500, AllocationStatus.APPROVED, currency="USD"
    )
    connector = FakeConnector(
        invoices=[invoice], allocations=[settled], ingest=ingest(payment)
    )
    result = run_sync(connector, start, end)

    assert result.report.proposals == 0, "already settled, nothing left to propose"
    assert result.report.already_in_ledger == 1


def test_duplicate_transaction_ids_in_one_run_are_collapsed():
    start, end = window()
    payment = pmt("T1", 12500, reference="RECON-T-001")
    connector = FakeConnector(
        invoices=[inv("RECON-T-001", 12500)],
        ingest=ingest(payment, payment),
    )
    result = run_sync(connector, start, end)
    assert len(result.payments) == 1
    assert result.report.proposals == 1


def test_the_windows_used_are_reported():
    start, end = window()
    result = run_sync(FakeConnector(), start, end)
    assert result.report.window_start == start
    assert result.report.window_end == end


# --------------------------------------------------------------------------
# Accounting invariants are validated, not assumed
# --------------------------------------------------------------------------


def test_an_accounting_inconsistency_is_reported_not_swallowed():
    start, end = window()
    # due_amount says 5000 outstanding but no allocation explains the rest.
    broken = inv("RECON-T-009", 12500, due=5000)
    connector = FakeConnector(invoices=[broken])
    result = run_sync(connector, start, end)

    assert result.report.accounting_errors
    assert not result.ok
    assert "RECON-T-009" in result.report.accounting_errors[0]


def test_a_consistent_ledger_reports_no_accounting_errors():
    start, end = window()
    invoice = inv("RECON-T-005", 30000, due=10000)
    allocations = [
        PaymentAllocation("EXTR-1", invoice.invoice_id, 20000,
                          AllocationStatus.APPROVED, currency="USD"),
    ]
    connector = FakeConnector(invoices=[invoice], allocations=allocations)
    result = run_sync(connector, start, end)
    assert result.report.accounting_errors == ()
    assert result.ok


def test_approved_allocations_are_never_double_counted():
    start, end = window()
    invoice = inv("RECON-T-005", 30000, due=10000)
    same = PaymentAllocation("EXTR-1", invoice.invoice_id, 20000,
                             AllocationStatus.APPROVED, currency="USD")
    connector = FakeConnector(invoices=[invoice], allocations=[same, same])
    result = run_sync(connector, start, end)
    assert result.report.already_in_ledger == 1
    assert result.report.accounting_errors == ()


def test_proposals_never_exceed_an_invoices_remaining_balance():
    start, end = window()
    invoice = inv("RECON-T-005", 30000, due=10000)
    settled = PaymentAllocation("EXTR-1", invoice.invoice_id, 20000,
                                AllocationStatus.APPROVED, currency="USD")
    connector = FakeConnector(
        invoices=[invoice],
        allocations=[settled],
        ingest=ingest(pmt("T9", 25000, reference="RECON-T-005")),
    )
    result = run_sync(connector, start, end)
    assert result.report.passed_verification == 0


# --------------------------------------------------------------------------
# Error classification
# --------------------------------------------------------------------------


def test_transaction_search_being_unavailable_falls_back_not_crashes():
    """Invoice-embedded payments still reconcile without Transaction Search."""
    start, end = window()
    connector = FakeConnector(invoices=[inv("RECON-T-001", 12500)], fail="transactions")
    result = run_sync(connector, start, end)

    assert result.report.invoices_ingested == 1
    assert result.report.transaction_search_available is False
    assert result.report.transaction_search_error
    assert "403" in result.report.transaction_search_error


def test_a_failed_invoice_fetch_is_fatal_not_a_partial_sync():
    start, end = window()
    with pytest.raises(RuntimeError):
        run_sync(FakeConnector(fail="invoices"), start, end)


def test_the_report_classifies_what_went_wrong():
    start, end = window()
    connector = FakeConnector(invoices=[inv("RECON-T-009", 12500, due=5000)], fail="transactions")
    report = run_sync(connector, start, end).report
    assert report.transaction_search_available is False
    assert report.accounting_errors
    lines = report.format().splitlines()
    assert any("Transaction Search" in line for line in lines)
    assert any("accounting" in line.lower() for line in lines)


def test_the_report_formats_without_leaking_anything():
    start, end = window()
    connector = FakeConnector(
        invoices=[inv("RECON-T-001", 12500)],
        ingest=ingest(pmt("T1", 12500, reference="RECON-T-001")),
    )
    text = run_sync(connector, start, end).report.format()
    assert "invoices" in text.lower()
    assert "secret" not in text.lower()
    assert "bearer" not in text.lower()


# --------------------------------------------------------------------------
# Against the real committed fixtures
# --------------------------------------------------------------------------


def test_a_sync_over_the_real_seeded_sandbox_shape():
    from reconcile.paypal import PayPalClient

    client = PayPalClient(client_id="id", client_secret="secret", env_file=None)
    listing = load("sandbox_invoices_list")
    details = {
        "RECON-T-005": load("sandbox_invoice_detail_partial"),
        "RECON-T-006": load("sandbox_invoice_detail_paid"),
    }

    invoices, allocations = [], []
    for item in listing["items"]:
        number = item["detail"]["invoice_number"]
        payload = details.get(number, item)
        invoices.append(client.map_invoice(payload))
        allocations.extend(client.extract_linked_allocations(payload))

    txn = client.map_transactions(load("sandbox_transactions")["transaction_details"])
    connector = FakeConnector(invoices=invoices, allocations=allocations, ingest=txn)

    start, end = window()
    result = run_sync(connector, start, end)

    assert result.report.invoices_ingested == 8
    assert result.report.already_in_ledger == 3   # 2 on the partial, 1 on the settled
    assert result.report.accounting_errors == (), result.report.accounting_errors
    assert result.ok


def test_the_real_sandbox_shape_is_idempotent_across_two_runs():
    from reconcile.paypal import PayPalClient

    client = PayPalClient(client_id="id", client_secret="secret", env_file=None)
    listing = load("sandbox_invoices_list")
    details = {
        "RECON-T-005": load("sandbox_invoice_detail_partial"),
        "RECON-T-006": load("sandbox_invoice_detail_paid"),
    }

    def build():
        invoices, allocations = [], []
        for item in listing["items"]:
            payload = details.get(item["detail"]["invoice_number"], item)
            invoices.append(client.map_invoice(payload))
            allocations.extend(client.extract_linked_allocations(payload))
        txn = client.map_transactions(load("sandbox_transactions")["transaction_details"])
        return FakeConnector(invoices=invoices, allocations=allocations, ingest=txn)

    start, end = window()
    assert run_sync(build(), start, end).report == run_sync(build(), start, end).report

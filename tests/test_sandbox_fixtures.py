"""Contract tests against real PayPal Sandbox responses.

The fixtures in tests/fixtures/ are actual responses from
api-m.sandbox.paypal.com, sanitised of identity fields and committed so the
connector's behaviour against real payloads is checked on every run rather than
only when someone has credentials. They are the record of what PayPal actually
sends, as opposed to what the documentation implies.
"""

from __future__ import annotations

import copy

import json
import pathlib

import pytest

from reconcile.models import (
    AllocationStatus,
    InvoiceStatus,
    PaymentStatus,
    cross_check_invoice_balance,
)
from reconcile.paypal import PayPalClient

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def load(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text())


@pytest.fixture
def client():
    return PayPalClient(client_id="id", client_secret="secret", env_file=None)


# --------------------------------------------------------------------------
# The fixtures themselves
# --------------------------------------------------------------------------


def test_fixtures_exist_and_are_the_real_shape():
    listing = load("sandbox_invoices_list")
    assert set(listing) >= {"items", "total_items", "total_pages"}
    assert listing["total_items"] == len(listing["items"]) == 8


def test_no_fixture_contains_a_credential_or_an_email_address():
    import re

    for path in sorted(FIXTURES.glob("*.json")):
        text = path.read_text()
        assert not re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", text), path.name
        assert "access_token" not in text, path.name
        assert not re.search(r"\b[A-Za-z0-9_-]{60,}\b", text), path.name


# --------------------------------------------------------------------------
# Mapping the real listing
# --------------------------------------------------------------------------


def test_every_real_invoice_maps_without_error(client):
    invoices = [client.map_invoice(item) for item in load("sandbox_invoices_list")["items"]]
    assert len(invoices) == 8
    assert all(i.invoice_id and i.number for i in invoices)


def test_paypal_id_and_invoice_number_stay_separate(client):
    invoices = [client.map_invoice(i) for i in load("sandbox_invoices_list")["items"]]
    for invoice in invoices:
        assert invoice.invoice_id.startswith("INV2-"), invoice.invoice_id
        assert invoice.number.startswith("RECON-T-"), invoice.number
        assert invoice.invoice_id != invoice.number


def test_the_real_unpaid_status_maps_to_an_open_invoice(client):
    by_number = {
        client.map_invoice(i).number: client.map_invoice(i)
        for i in load("sandbox_invoices_list")["items"]
    }
    assert by_number["RECON-T-001"].status is InvoiceStatus.SENT
    assert by_number["RECON-T-001"].is_open


def test_the_real_marked_as_paid_status_is_ineligible(client):
    by_number = {
        client.map_invoice(i).number: client.map_invoice(i)
        for i in load("sandbox_invoices_list")["items"]
    }
    paid = by_number["RECON-T-006"]
    assert paid.status is InvoiceStatus.PAID
    assert not paid.is_open, "a settled invoice must not be open for allocation"


def test_the_real_partially_paid_invoice_is_still_open(client):
    by_number = {
        client.map_invoice(i).number: client.map_invoice(i)
        for i in load("sandbox_invoices_list")["items"]
    }
    partial = by_number["RECON-T-005"]
    assert partial.status is InvoiceStatus.PARTIALLY_PAID
    assert partial.is_open
    assert partial.amount_cents == 30000
    assert partial.due_amount_cents == 10000


def test_a_real_zero_exponent_invoice_is_not_scaled_by_a_hundred(client):
    """RECON-T-007 is 1000 JPY. A currency-blind parser would read 100000."""
    by_number = {
        client.map_invoice(i).number: client.map_invoice(i)
        for i in load("sandbox_invoices_list")["items"]
    }
    jpy = by_number["RECON-T-007"]
    assert jpy.currency == "JPY"
    assert jpy.amount_cents == 1000
    assert jpy.due_amount_cents == 1000


def test_the_real_ambiguous_pair_shares_an_amount(client):
    by_number = {
        client.map_invoice(i).number: client.map_invoice(i)
        for i in load("sandbox_invoices_list")["items"]
    }
    assert by_number["RECON-T-003"].amount_cents == by_number["RECON-T-004"].amount_cents == 7550


# --------------------------------------------------------------------------
# The listing genuinely lacks provenance: the finding this all rests on
# --------------------------------------------------------------------------


def test_the_real_listing_omits_payment_transactions():
    """Recorded, because the whole hydration step exists because of it."""
    for item in load("sandbox_invoices_list")["items"]:
        payments = item.get("payments") or {}
        if payments.get("paid_amount"):
            assert "transactions" not in payments, (
                "if PayPal starts returning transactions in the listing, "
                "hydration becomes unnecessary and this test should change"
            )


def test_the_real_listing_does_report_paid_amount():
    numbers = {
        (i.get("detail") or {}).get("invoice_number"): (i.get("payments") or {})
        for i in load("sandbox_invoices_list")["items"]
    }
    assert numbers["RECON-T-005"]["paid_amount"]["value"] == "200.00"
    assert numbers["RECON-T-006"]["paid_amount"]["value"] == "180.00"


def test_hydration_is_requested_for_exactly_the_paid_invoices(client):
    needs = [
        (i.get("detail") or {}).get("invoice_number")
        for i in load("sandbox_invoices_list")["items"]
        if client.needs_payment_provenance(i)
    ]
    assert sorted(needs) == ["RECON-T-005", "RECON-T-006"]


def test_the_real_detail_response_carries_the_transactions():
    partial = load("sandbox_invoice_detail_partial")
    txns = (partial.get("payments") or {}).get("transactions") or []
    assert len(txns) == 2
    assert all(t.get("payment_id", "").startswith("EXTR-") for t in txns)
    assert all(t.get("type") == "EXTERNAL" for t in txns)
    assert all(t.get("method") == "BANK_TRANSFER" for t in txns)


# --------------------------------------------------------------------------
# Accounting on the real hydrated payloads
# --------------------------------------------------------------------------


def test_the_real_partial_payment_balances(client):
    detail = load("sandbox_invoice_detail_partial")
    invoice = client.map_invoice(detail)
    allocations = client.extract_linked_allocations(detail)

    assert len(allocations) == 2
    assert sum(a.amount_cents for a in allocations) == 20000
    assert all(a.status is AllocationStatus.APPROVED for a in allocations)
    assert all(a.currency == "USD" for a in allocations)
    # total - approved == PayPal's own due_amount
    assert cross_check_invoice_balance(invoice, allocations) == 10000
    assert invoice.due_amount_cents == 10000


def test_the_real_settled_invoice_balances_to_zero(client):
    detail = load("sandbox_invoice_detail_paid")
    invoice = client.map_invoice(detail)
    allocations = client.extract_linked_allocations(detail)

    assert len(allocations) == 1
    assert allocations[0].amount_cents == 18000
    assert cross_check_invoice_balance(invoice, allocations) == 0


def test_the_real_external_payment_is_not_an_inferred_settlement(client):
    """Real provenance must beat the inferred-settlement fallback."""
    allocations = client.extract_linked_allocations(load("sandbox_invoice_detail_paid"))
    assert not any(a.payment_id.startswith("paypal_inferred") for a in allocations)


def test_the_unhydrated_partial_invoice_fails_closed(client):
    """Proof the guard is what forces hydration, on the real payload."""
    from reconcile.paypal import PayPalAccountingError

    listed = next(
        i for i in load("sandbox_invoices_list")["items"]
        if (i.get("detail") or {}).get("invoice_number") == "RECON-T-005"
    )
    with pytest.raises(PayPalAccountingError) as excinfo:
        client.extract_linked_allocations(listed)
    assert "provenance" in str(excinfo.value)


def test_no_real_allocation_is_counted_twice(client):
    allocations = []
    for name in ("sandbox_invoice_detail_partial", "sandbox_invoice_detail_paid"):
        allocations.extend(client.extract_linked_allocations(load(name)))
    keys = [(a.payment_id, a.invoice_id) for a in allocations]
    assert len(keys) == len(set(keys))


# --------------------------------------------------------------------------
# The real Transaction Search response
# --------------------------------------------------------------------------


def test_the_real_transaction_response_has_the_documented_envelope():
    data = load("sandbox_transactions")
    assert set(data) >= {
        "transaction_details", "total_items", "total_pages", "page",
        "start_date", "end_date", "last_refreshed_datetime",
    }


def test_the_real_appearance_delay_is_visible_in_the_response():
    """PayPal clamps end_date to last_refreshed_datetime."""
    data = load("sandbox_transactions")
    assert data["end_date"] <= data["last_refreshed_datetime"] or (
        data["end_date"] == data["last_refreshed_datetime"]
    )


def test_the_real_opening_balance_is_not_a_customer_payment(client):
    details = load("sandbox_transactions")["transaction_details"]
    result = client.map_transactions(details)
    txn_id = details[0]["transaction_info"]["transaction_id"]
    assert result.payments == ()
    assert result.non_payment_ids == (txn_id,)
    assert "T1900" in dict(result.skipped)[txn_id]


def test_the_same_record_with_a_payment_event_code_still_maps(client):
    item = copy.deepcopy(load("sandbox_transactions")["transaction_details"][0])
    item["transaction_info"]["transaction_event_code"] = "T0006"
    result = client.map_transactions([item])
    assert len(result.payments) == 1
    assert result.payments[0].amount_cents > 0
    assert result.payments[0].status is PaymentStatus.COMPLETED
    assert result.non_payment_ids == ()


def test_raw_evidence_is_kept_for_every_real_transaction(client):
    details = load("sandbox_transactions")["transaction_details"]
    result = client.map_transactions(details)
    assert result.transaction_count == len(details)
    for item in details:
        tid = item["transaction_info"]["transaction_id"]
        assert result.raw_by_id[tid] == item


def test_externally_recorded_payments_are_absent_from_transaction_search(client):
    """A recorded external payment moved no money through PayPal.

    So the EXTR- payments on RECON-T-005/006 cannot appear in Transaction
    Search, and the invoice-embedded path is the only source for them. This is
    the documented reason both ingestion paths are needed.
    """
    search_ids = {
        t["transaction_info"]["transaction_id"]
        for t in load("sandbox_transactions")["transaction_details"]
    }
    external_ids = {
        a.payment_id
        for name in ("sandbox_invoice_detail_partial", "sandbox_invoice_detail_paid")
        for a in client.extract_linked_allocations(load(name))
    }
    assert external_ids, "fixtures must contain externally recorded payments"
    assert not (external_ids & search_ids)

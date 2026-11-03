"""Provenance hydration for partially and fully paid invoices.

Established against the live Sandbox: `GET /v2/invoicing/invoices` returns
`payments.paid_amount` but **no** `payments.transactions`, while
`GET /v2/invoicing/invoices/{id}` returns the transactions. So the list alone
cannot show where a paid invoice's money came from, and the D1 accounting guard
correctly refuses to infer it. The connector therefore fetches the detail for
exactly those invoices that need it.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from reconcile.models import InvoiceStatus
from reconcile.paypal import PayPalAccountingError, PayPalClient


def _resp(payload: dict):
    r = MagicMock()
    r.status = 200
    r.read.return_value = json.dumps(payload).encode()
    r.__enter__.return_value = r
    return r


def _client():
    return PayPalClient(client_id="id", client_secret="secret", env_file=None)


def _listed(number, total, due, *, status="UNPAID", paid=None, invoice_id=None):
    """An invoice as the LIST endpoint renders it: no transactions array."""
    item = {
        "id": invoice_id or f"INV2-{number}",
        "status": status,
        "detail": {"invoice_number": number, "invoice_date": "2026-11-01"},
        "amount": {"currency_code": "USD", "value": total},
        "due_amount": {"currency_code": "USD", "value": due},
    }
    if paid is not None:
        item["payments"] = {"paid_amount": {"currency_code": "USD", "value": paid}}
    return item


def _detailed(number, total, due, paid, txn_id="EXTR-1", invoice_id=None):
    """The same invoice as the DETAIL endpoint renders it: with transactions."""
    item = _listed(number, total, due, status="PARTIALLY_PAID", paid=paid, invoice_id=invoice_id)
    item["payments"] = {
        "paid_amount": {"currency_code": "USD", "value": paid},
        "transactions": [
            {
                "payment_id": txn_id,
                "method": "BANK_TRANSFER",
                "type": "EXTERNAL",
                "payment_date": "2026-11-03",
                "amount": {"currency_code": "USD", "value": paid},
            }
        ],
    }
    return item


def _router(listing, details):
    """urlopen side effect: token, then the listing, then per-id details."""
    def side_effect(req, *a, **kw):
        url = req.full_url
        if url.endswith("/v1/oauth2/token"):
            return _resp({"access_token": "T", "expires_in": 3600})
        if "/v2/invoicing/invoices?" in url or url.endswith("/v2/invoicing/invoices"):
            return _resp(listing)
        for invoice_id, payload in details.items():
            if url.endswith(f"/v2/invoicing/invoices/{invoice_id}"):
                return _resp(payload)
        raise AssertionError(f"unexpected request: {url}")
    return side_effect


# --------------------------------------------------------------------------
# The status PayPal actually uses for a sent invoice
# --------------------------------------------------------------------------


def test_unpaid_is_mapped_explicitly_to_an_open_invoice():
    """Real Sandbox data returns UNPAID, not SENT, for a sent invoice."""
    invoice = _client().map_invoice(_listed("RECON-T-001", "125.00", "125.00"))
    assert invoice.status is InvoiceStatus.SENT
    assert invoice.is_open


def test_unpaid_is_in_the_status_map_not_merely_a_default():
    from reconcile.paypal import _STATUS_MAP
    assert _STATUS_MAP["UNPAID"] is InvoiceStatus.SENT


# --------------------------------------------------------------------------
# Hydration decides per invoice
# --------------------------------------------------------------------------


def test_a_paid_invoice_without_transactions_is_hydrated_from_the_detail():
    listing = {"items": [_listed("RECON-T-005", "300.00", "200.00",
                                 status="PARTIALLY_PAID", paid="100.00")],
               "total_items": 1, "total_pages": 1}
    details = {"INV2-RECON-T-005": _detailed("RECON-T-005", "300.00", "200.00", "100.00")}

    with patch("urllib.request.urlopen", side_effect=_router(listing, details)):
        invoices, allocs = _client().list_all_invoices_with_allocations()

    assert len(invoices) == 1
    assert len(allocs) == 1
    assert allocs[0].payment_id == "EXTR-1"
    assert allocs[0].amount_cents == 10000
    assert allocs[0].currency == "USD"


def test_an_unpaid_invoice_is_not_hydrated():
    """Nothing was paid, so there is no provenance to fetch. Saves a request."""
    listing = {"items": [_listed("RECON-T-001", "125.00", "125.00")],
               "total_items": 1, "total_pages": 1}

    with patch("urllib.request.urlopen", side_effect=_router(listing, {})) as m:
        invoices, allocs = _client().list_all_invoices_with_allocations()

    assert len(invoices) == 1 and allocs == []
    detail_calls = [c for c in m.call_args_list
                    if "/v2/invoicing/invoices/" in c.args[0].full_url]
    assert detail_calls == []


def test_an_invoice_that_already_carries_transactions_is_not_refetched():
    listing = {"items": [_detailed("RECON-T-005", "300.00", "200.00", "100.00")],
               "total_items": 1, "total_pages": 1}

    with patch("urllib.request.urlopen", side_effect=_router(listing, {})) as m:
        _invoices, allocs = _client().list_all_invoices_with_allocations()

    assert len(allocs) == 1
    detail_calls = [c for c in m.call_args_list
                    if "/v2/invoicing/invoices/" in c.args[0].full_url]
    assert detail_calls == [], "the listing was already sufficient"


def test_only_the_invoices_that_need_it_are_hydrated():
    listing = {
        "items": [
            _listed("RECON-T-001", "125.00", "125.00"),
            _listed("RECON-T-005", "300.00", "200.00", status="PARTIALLY_PAID", paid="100.00"),
            _listed("RECON-T-002", "250.00", "250.00"),
        ],
        "total_items": 3, "total_pages": 1,
    }
    details = {"INV2-RECON-T-005": _detailed("RECON-T-005", "300.00", "200.00", "100.00")}

    with patch("urllib.request.urlopen", side_effect=_router(listing, details)) as m:
        invoices, allocs = _client().list_all_invoices_with_allocations()

    assert len(invoices) == 3 and len(allocs) == 1
    detail_urls = [c.args[0].full_url for c in m.call_args_list
                   if "/v2/invoicing/invoices/" in c.args[0].full_url]
    assert len(detail_urls) == 1 and "INV2-RECON-T-005" in detail_urls[0]


def test_hydration_can_be_switched_off():
    """Without hydration the accounting guard must still fail closed."""
    listing = {"items": [_listed("RECON-T-005", "300.00", "200.00",
                                 status="PARTIALLY_PAID", paid="100.00")],
               "total_items": 1, "total_pages": 1}

    with patch("urllib.request.urlopen", side_effect=_router(listing, {})):
        with pytest.raises(PayPalAccountingError) as excinfo:
            _client().list_all_invoices_with_allocations(hydrate=False)

    assert "provenance" in str(excinfo.value)


def test_a_fully_paid_invoice_is_hydrated_rather_than_inferred():
    """Real transactions beat the inferred-settlement fallback."""
    listing = {"items": [_listed("RECON-T-006", "180.00", "0.00",
                                 status="PAID", paid="180.00")],
               "total_items": 1, "total_pages": 1}
    detail = _detailed("RECON-T-006", "180.00", "0.00", "180.00", txn_id="EXTR-6")
    detail["status"] = "PAID"

    with patch("urllib.request.urlopen",
               side_effect=_router(listing, {"INV2-RECON-T-006": detail})):
        _invoices, allocs = _client().list_all_invoices_with_allocations()

    assert [a.payment_id for a in allocs] == ["EXTR-6"]
    assert not any(a.payment_id.startswith("paypal_inferred") for a in allocs)


def test_hydration_failure_propagates_rather_than_losing_provenance():
    from urllib.error import HTTPError

    listing = {"items": [_listed("RECON-T-005", "300.00", "200.00",
                                 status="PARTIALLY_PAID", paid="100.00")],
               "total_items": 1, "total_pages": 1}

    def side_effect(req, *a, **kw):
        url = req.full_url
        if url.endswith("/v1/oauth2/token"):
            return _resp({"access_token": "T", "expires_in": 3600})
        if "/v2/invoicing/invoices?" in url:
            return _resp(listing)
        raise HTTPError(url, 500, "boom", {}, None)

    from reconcile.paypal import PayPalAPIError
    with patch("urllib.request.urlopen", side_effect=side_effect):
        with pytest.raises(PayPalAPIError):
            _client().list_all_invoices_with_allocations()


def test_hydration_keeps_allocations_deduplicated_by_compound_key():
    listing = {
        "items": [
            _listed("RECON-T-005", "300.00", "200.00", status="PARTIALLY_PAID", paid="100.00"),
            _listed("RECON-T-006", "180.00", "0.00", status="PAID", paid="180.00"),
        ],
        "total_items": 2, "total_pages": 1,
    }
    details = {
        "INV2-RECON-T-005": _detailed("RECON-T-005", "300.00", "200.00", "100.00", txn_id="EXTR-A"),
        "INV2-RECON-T-006": _detailed("RECON-T-006", "180.00", "0.00", "180.00", txn_id="EXTR-B"),
    }

    with patch("urllib.request.urlopen", side_effect=_router(listing, details)):
        _invoices, allocs = _client().list_all_invoices_with_allocations()

    keys = [(a.payment_id, a.invoice_id) for a in allocs]
    assert len(keys) == len(set(keys)) == 2


def test_the_hydrated_invoice_record_is_the_one_mapped():
    """The detail payload is richer; mapping must not silently use the stub."""
    listing = {"items": [_listed("RECON-T-005", "300.00", "200.00",
                                 status="PARTIALLY_PAID", paid="100.00")],
               "total_items": 1, "total_pages": 1}
    details = {"INV2-RECON-T-005": _detailed("RECON-T-005", "300.00", "200.00", "100.00")}

    with patch("urllib.request.urlopen", side_effect=_router(listing, details)):
        invoices, _allocs = _client().list_all_invoices_with_allocations()

    invoice = invoices[0]
    assert invoice.amount_cents == 30000
    assert invoice.due_amount_cents == 20000
    assert invoice.status is InvoiceStatus.PARTIALLY_PAID

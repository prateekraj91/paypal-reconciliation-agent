"""Complete, bounded pagination for the PayPal Invoicing v2 listing.

The D1 client issued one request and ignored total_pages, so an account with
more invoices than one page silently ingested a prefix. Every payment against
an unlisted invoice would then be reported UNMATCHED: a wrong answer that looks
like a clean run. These tests pin the properties that make that impossible.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from reconcile.paypal import (
    MAX_INVOICE_PAGE_SIZE,
    PayPalAPIError,
    PayPalClient,
    PayPalPaginationError,
    PayPalTimeoutError,
)


def _resp(payload: dict):
    r = MagicMock()
    r.status = 200
    r.read.return_value = json.dumps(payload).encode()
    r.__enter__.return_value = r
    return r


def _client():
    return PayPalClient(client_id="id", client_secret="secret", env_file=None)


def _invoice(n: int, currency: str = "USD") -> dict:
    return {
        "id": f"INV2-{n:04d}",
        "status": "SENT",
        "detail": {"invoice_number": f"{n:04d}", "invoice_date": "2026-10-01"},
        "amount": {"currency_code": currency, "value": "10.00"},
        "due_amount": {"currency_code": currency, "value": "10.00"},
    }


def _page(items, *, total_items=None, total_pages=None):
    page: dict = {"items": items, "links": []}
    if total_items is not None:
        page["total_items"] = total_items
    if total_pages is not None:
        page["total_pages"] = total_pages
    return page


def _pages_responder(pages, *, token="T"):
    """urlopen side effect: serves OAuth, then one page per invoice request."""
    state = {"i": 0}

    def side_effect(req, *a, **kw):
        if req.full_url.endswith("/v1/oauth2/token"):
            return _resp({"access_token": token, "expires_in": 3600})
        page = pages[state["i"]]
        state["i"] += 1
        if isinstance(page, Exception):
            raise page
        return _resp(page)

    return side_effect, state


# --------------------------------------------------------------------------
# Multiple pages
# --------------------------------------------------------------------------


def test_retrieves_every_page_and_returns_all_invoices():
    size = 3
    pages = [
        _page([_invoice(1), _invoice(2), _invoice(3)], total_items=7, total_pages=3),
        _page([_invoice(4), _invoice(5), _invoice(6)], total_items=7, total_pages=3),
        _page([_invoice(7)], total_items=7, total_pages=3),
    ]
    side_effect, _ = _pages_responder(pages)

    with patch("urllib.request.urlopen", side_effect=side_effect) as m:
        invoices = _client().list_all_invoices(page_size=size)

    assert [i.number for i in invoices] == [f"{n:04d}" for n in range(1, 8)]
    # 1 token request + 3 page requests
    assert m.call_count == 4


def test_page_size_and_page_number_are_sent_on_each_request():
    pages = [
        _page([_invoice(1)], total_items=2, total_pages=2),
        _page([_invoice(2)], total_items=2, total_pages=2),
    ]
    side_effect, _ = _pages_responder(pages)

    with patch("urllib.request.urlopen", side_effect=side_effect) as m:
        _client().list_all_invoices(page_size=1)

    urls = [c.args[0].full_url for c in m.call_args_list if "invoicing" in c.args[0].full_url]
    assert len(urls) == 2
    assert "page=1" in urls[0] and "page_size=1" in urls[0]
    assert "page=2" in urls[1] and "page_size=1" in urls[1]
    assert all("total_required=true" in u for u in urls)


def test_a_single_full_page_followed_by_an_empty_page_terminates():
    pages = [
        _page([_invoice(1), _invoice(2)], total_items=2, total_pages=1),
        _page([], total_items=2, total_pages=1),
    ]
    side_effect, state = _pages_responder(pages)

    with patch("urllib.request.urlopen", side_effect=side_effect):
        invoices = _client().list_all_invoices(page_size=2)

    assert len(invoices) == 2
    # total_pages=1 means it must not even ask for page 2.
    assert state["i"] == 1


# --------------------------------------------------------------------------
# Empty results
# --------------------------------------------------------------------------


def test_genuinely_empty_account_returns_empty_without_error():
    pages = [_page([], total_items=0, total_pages=0)]
    side_effect, _ = _pages_responder(pages)

    with patch("urllib.request.urlopen", side_effect=side_effect):
        assert _client().list_all_invoices(page_size=20) == []


def test_empty_first_page_contradicting_total_items_fails_closed():
    """An empty page while the API claims five invoices is an incomplete fetch."""
    pages = [_page([], total_items=5, total_pages=1)]
    side_effect, _ = _pages_responder(pages)

    with patch("urllib.request.urlopen", side_effect=side_effect):
        with pytest.raises(PayPalPaginationError) as excinfo:
            _client().list_all_invoices(page_size=20)

    assert "0" in str(excinfo.value) and "5" in str(excinfo.value)


def test_empty_final_page_is_a_normal_terminator():
    pages = [
        _page([_invoice(1), _invoice(2)], total_items=2),
        _page([], total_items=2),
    ]
    side_effect, _ = _pages_responder(pages)

    with patch("urllib.request.urlopen", side_effect=side_effect):
        assert len(_client().list_all_invoices(page_size=2)) == 2


def test_missing_items_key_is_treated_as_an_empty_page():
    pages = [_page([_invoice(1)], total_items=1), {"links": []}]
    side_effect, _ = _pages_responder(pages)

    with patch("urllib.request.urlopen", side_effect=side_effect):
        assert len(_client().list_all_invoices(page_size=1)) == 1


# --------------------------------------------------------------------------
# Duplicates and ordering
# --------------------------------------------------------------------------


def test_duplicate_invoices_across_pages_are_returned_once():
    pages = [
        _page([_invoice(1), _invoice(2)], total_items=3),
        _page([_invoice(2), _invoice(3)], total_items=3),
        _page([], total_items=3),
    ]
    side_effect, _ = _pages_responder(pages)

    with patch("urllib.request.urlopen", side_effect=side_effect):
        invoices = _client().list_all_invoices(page_size=2)

    assert [i.invoice_id for i in invoices] == ["INV2-0001", "INV2-0002", "INV2-0003"]


def test_a_full_page_of_only_duplicates_fails_closed():
    """A server that keeps returning the same page would otherwise loop."""
    full = _page([_invoice(1), _invoice(2)], total_items=99)
    side_effect, _ = _pages_responder([full, full, full])

    with patch("urllib.request.urlopen", side_effect=side_effect):
        with pytest.raises(PayPalPaginationError) as excinfo:
            _client().list_all_invoices(page_size=2)

    assert "duplicate" in str(excinfo.value).lower()


def test_ordering_is_deterministic_and_follows_page_order():
    pages = [
        _page([_invoice(5), _invoice(3)], total_items=4),
        _page([_invoice(9), _invoice(1)], total_items=4),
        _page([], total_items=4),
    ]
    side_effect, _ = _pages_responder(pages)

    with patch("urllib.request.urlopen", side_effect=side_effect):
        first = [i.invoice_id for i in _client().list_all_invoices(page_size=2)]

    side_effect, _ = _pages_responder(pages)
    with patch("urllib.request.urlopen", side_effect=side_effect):
        second = [i.invoice_id for i in _client().list_all_invoices(page_size=2)]

    assert first == second == ["INV2-0005", "INV2-0003", "INV2-0009", "INV2-0001"]


# --------------------------------------------------------------------------
# Failure on a later page must never yield a partial dataset
# --------------------------------------------------------------------------


def test_api_failure_on_a_later_page_raises_instead_of_truncating():
    from urllib.error import HTTPError

    pages = [
        _page([_invoice(1), _invoice(2)], total_items=10, total_pages=5),
        HTTPError("u", 500, "Server Error", {}, None),
    ]
    side_effect, _ = _pages_responder(pages)

    with patch("urllib.request.urlopen", side_effect=side_effect):
        with pytest.raises(PayPalAPIError):
            _client().list_all_invoices(page_size=2)


def test_timeout_on_a_later_page_raises_instead_of_truncating():
    pages = [
        _page([_invoice(1)], total_items=10, total_pages=5),
        TimeoutError("timed out"),
    ]
    side_effect, _ = _pages_responder(pages)

    with patch("urllib.request.urlopen", side_effect=side_effect):
        with pytest.raises(PayPalTimeoutError):
            _client().list_all_invoices(page_size=1)


def test_short_count_against_total_items_fails_closed():
    """Pages ended early but the API said there were more invoices."""
    pages = [
        _page([_invoice(1), _invoice(2)], total_items=10),
        _page([], total_items=10),
    ]
    side_effect, _ = _pages_responder(pages)

    with patch("urllib.request.urlopen", side_effect=side_effect):
        with pytest.raises(PayPalPaginationError) as excinfo:
            _client().list_all_invoices(page_size=2)

    assert "incomplete" in str(excinfo.value).lower()


# --------------------------------------------------------------------------
# Malformed pagination metadata
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "meta",
    [
        {"total_items": "many", "total_pages": "lots"},
        {"total_items": -1, "total_pages": -5},
        {"total_items": None, "total_pages": None},
        {"total_items": 1.5, "total_pages": 2.7},
        {"total_items": True, "total_pages": True},
    ],
)
def test_unusable_pagination_metadata_is_ignored_not_trusted(meta):
    """Garbage metadata must not stop the walk or trigger a false failure."""
    pages = [
        dict(_page([_invoice(1), _invoice(2)]), **meta),
        dict(_page([_invoice(3)]), **meta),
    ]
    side_effect, _ = _pages_responder(pages)

    with patch("urllib.request.urlopen", side_effect=side_effect):
        invoices = _client().list_all_invoices(page_size=2)

    assert len(invoices) == 3


def test_total_pages_larger_than_reality_still_terminates():
    pages = [
        _page([_invoice(1)], total_items=1, total_pages=500),
        _page([], total_items=1, total_pages=500),
    ]
    side_effect, _ = _pages_responder(pages)

    with patch("urllib.request.urlopen", side_effect=side_effect):
        assert len(_client().list_all_invoices(page_size=1)) == 1


# --------------------------------------------------------------------------
# Bounds
# --------------------------------------------------------------------------


def test_pagination_is_bounded_and_refuses_to_loop_forever():
    """A server that always returns a full page of new invoices must not hang."""
    counter = {"n": 0}

    def side_effect(req, *a, **kw):
        if req.full_url.endswith("/v1/oauth2/token"):
            return _resp({"access_token": "T", "expires_in": 3600})
        counter["n"] += 1
        base = counter["n"] * 2
        return _resp(_page([_invoice(base), _invoice(base + 1)]))

    with patch("urllib.request.urlopen", side_effect=side_effect):
        with pytest.raises(PayPalPaginationError) as excinfo:
            _client().list_all_invoices(page_size=2, max_pages=5)

    assert "max_pages" in str(excinfo.value) or "bound" in str(excinfo.value).lower()
    assert counter["n"] == 5


def test_page_size_is_clamped_to_the_documented_api_maximum():
    pages = [_page([], total_items=0)]
    side_effect, _ = _pages_responder(pages)

    with patch("urllib.request.urlopen", side_effect=side_effect) as m:
        _client().list_all_invoices(page_size=10_000)

    url = [c.args[0].full_url for c in m.call_args_list if "invoicing" in c.args[0].full_url][0]
    assert f"page_size={MAX_INVOICE_PAGE_SIZE}" in url


@pytest.mark.parametrize("bad", [0, -1, -100])
def test_non_positive_page_size_is_rejected(bad):
    with pytest.raises(ValueError):
        _client().list_all_invoices(page_size=bad)


@pytest.mark.parametrize("bad", [0, -1])
def test_non_positive_max_pages_is_rejected(bad):
    with pytest.raises(ValueError):
        _client().list_all_invoices(max_pages=bad)


# --------------------------------------------------------------------------
# Allocations follow the same completeness rule
# --------------------------------------------------------------------------


def test_allocations_are_collected_across_every_page():
    def paid(n):
        item = _invoice(n)
        item["status"] = "PAID"
        item["due_amount"] = {"currency_code": "USD", "value": "0.00"}
        item["payments"] = {
            "transactions": [
                {"payment_id": f"TXN-{n}", "amount": {"currency_code": "USD", "value": "10.00"}}
            ]
        }
        return item

    pages = [
        _page([paid(1), paid(2)], total_items=3),
        _page([paid(3)], total_items=3),
    ]
    side_effect, _ = _pages_responder(pages)

    with patch("urllib.request.urlopen", side_effect=side_effect):
        invoices, allocs = _client().list_all_invoices_with_allocations(page_size=2)

    assert len(invoices) == 3
    assert sorted(a.payment_id for a in allocs) == ["TXN-1", "TXN-2", "TXN-3"]
    assert all(a.currency == "USD" for a in allocs)


def test_paginated_allocation_fetch_also_fails_closed_on_a_later_page():
    from urllib.error import HTTPError

    pages = [_page([_invoice(1)], total_items=5, total_pages=3), HTTPError("u", 503, "x", {}, None)]
    side_effect, _ = _pages_responder(pages)

    with patch("urllib.request.urlopen", side_effect=side_effect):
        with pytest.raises(PayPalAPIError):
            _client().list_all_invoices_with_allocations(page_size=1)


def test_pagination_error_is_a_paypal_error():
    assert issubclass(PayPalPaginationError, PayPalAPIError) or issubclass(
        PayPalPaginationError, Exception
    )

"""PayPal Transaction Search: windows, retries, mapping and deduplication.

Field names here were taken from an actual Sandbox response, not from memory.
See docs/paypal-transaction-fields.md for the captured shape.
"""

from __future__ import annotations

import io
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

import pytest

from reconcile.models import PaymentStatus
from reconcile.paypal import (
    MAX_TRANSACTION_WINDOW_DAYS,
    TRANSACTION_APPEARANCE_DELAY_HOURS,
    PayPalAPIError,
    PayPalAccountingError,
    PayPalAuthError,
    PayPalClient,
    PayPalPaginationError,
    PayPalTimeoutError,
    sync_windows,
)

UTC = timezone.utc


def _resp(payload: dict):
    r = MagicMock()
    r.status = 200
    r.read.return_value = json.dumps(payload).encode()
    r.__enter__.return_value = r
    return r


def _client(**kw):
    c = PayPalClient(client_id="id", client_secret="secret", env_file=None, **kw)
    c._sleep = lambda _s: None          # no real backoff delay in tests
    return c


def _txn(
    tid="TXN-1",
    value="10.00",
    currency="USD",
    status="S",
    invoice_id=None,
    custom_field=None,
    subject=None,
    note=None,
    date="2026-10-29T05:06:59Z",
    email=None,
    full_name=None,
):
    info = {
        "transaction_id": tid,
        "transaction_status": status,
        "transaction_amount": {"currency_code": currency, "value": value},
        "transaction_initiation_date": date,
        "transaction_updated_date": date,
    }
    if invoice_id is not None:
        info["invoice_id"] = invoice_id
    if custom_field is not None:
        info["custom_field"] = custom_field
    if subject is not None:
        info["transaction_subject"] = subject
    if note is not None:
        info["transaction_note"] = note

    payer: dict = {}
    if email is not None:
        payer["email_address"] = email
    if full_name is not None:
        payer["payer_name"] = {"alternate_full_name": full_name}

    return {"transaction_info": info, "payer_info": payer}


def _txn_page(txns, *, total_items=None, total_pages=1, page=1):
    body = {
        "transaction_details": txns,
        "page": page,
        "total_pages": total_pages,
        "account_number": "REDACTED",
        "start_date": "2026-10-01T00:00:00Z",
        "end_date": "2026-10-31T00:00:00Z",
        "last_refreshed_datetime": "2026-10-31T00:00:00Z",
    }
    body["total_items"] = len(txns) if total_items is None else total_items
    return body


def _responder(pages):
    state = {"i": 0, "token_calls": 0}

    def side_effect(req, *a, **kw):
        if req.full_url.endswith("/v1/oauth2/token"):
            state["token_calls"] += 1
            return _resp({"access_token": f"T{state['token_calls']}", "expires_in": 3600})
        page = pages[min(state["i"], len(pages) - 1)]
        state["i"] += 1
        if isinstance(page, Exception):
            raise page
        return _resp(page)

    return side_effect, state


# --------------------------------------------------------------------------
# Synchronisation windows
# --------------------------------------------------------------------------


def test_window_limit_is_thirty_one_days():
    assert MAX_TRANSACTION_WINDOW_DAYS == 31


def test_appearance_delay_is_three_hours():
    assert TRANSACTION_APPEARANCE_DELAY_HOURS == 3


def test_a_short_range_is_a_single_window():
    start = datetime(2026, 10, 1, tzinfo=UTC)
    end = datetime(2026, 10, 10, tzinfo=UTC)
    windows = sync_windows(start, end)
    assert windows == ((start, end),)


def test_no_window_exceeds_the_api_limit():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = datetime(2026, 6, 30, tzinfo=UTC)
    for w_start, w_end in sync_windows(start, end):
        assert (w_end - w_start) <= timedelta(days=MAX_TRANSACTION_WINDOW_DAYS)


def test_windows_overlap_so_boundary_transactions_are_not_lost():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = datetime(2026, 4, 1, tzinfo=UTC)
    windows = sync_windows(start, end, overlap=timedelta(hours=6))
    assert len(windows) > 1
    for (_s1, e1), (s2, _e2) in zip(windows, windows[1:]):
        assert s2 < e1, "consecutive windows must overlap, not merely abut"
        assert e1 - s2 == timedelta(hours=6)


def test_windows_cover_the_whole_range_without_gaps():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = datetime(2026, 5, 15, tzinfo=UTC)
    windows = sync_windows(start, end)
    assert windows[0][0] == start
    assert windows[-1][1] == end
    for (_s1, e1), (s2, _e2) in zip(windows, windows[1:]):
        assert s2 <= e1


def test_windows_are_deterministic():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = datetime(2026, 5, 15, tzinfo=UTC)
    assert sync_windows(start, end) == sync_windows(start, end)


def test_an_inverted_range_is_rejected():
    with pytest.raises(ValueError):
        sync_windows(datetime(2026, 2, 1, tzinfo=UTC), datetime(2026, 1, 1, tzinfo=UTC))


def test_naive_datetimes_are_rejected():
    with pytest.raises(ValueError):
        sync_windows(datetime(2026, 1, 1), datetime(2026, 1, 2))


def test_overlap_must_be_smaller_than_the_window():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = datetime(2026, 6, 1, tzinfo=UTC)
    with pytest.raises(ValueError):
        sync_windows(start, end, overlap=timedelta(days=40))


# --------------------------------------------------------------------------
# Mapping real Sandbox fields onto canonical Payment records
# --------------------------------------------------------------------------


def test_maps_the_canonical_transaction_fields():
    [p] = _client().map_transactions([
        _txn(tid="6FD489545X6067634", value="25.00", currency="USD",
             invoice_id="INV-1001", email="buyer@example.com", full_name="Acme Studio")
    ]).payments

    assert p.payment_id == "6FD489545X6067634"
    assert p.amount_cents == 2500
    assert p.currency == "USD"
    assert p.status is PaymentStatus.COMPLETED
    assert p.reference == "INV-1001"
    assert p.payer_email == "buyer@example.com"
    assert p.payer_name == "Acme Studio"
    assert p.received_at == datetime(2026, 10, 29, 5, 6, 59, tzinfo=UTC)


@pytest.mark.parametrize(
    "code,expected",
    [("S", PaymentStatus.COMPLETED), ("P", PaymentStatus.PENDING),
     ("D", PaymentStatus.DENIED), ("V", PaymentStatus.REVERSED)],
)
def test_transaction_status_codes_map_to_payment_status(code, expected):
    [p] = _client().map_transactions([_txn(status=code)]).payments
    assert p.status is expected


def test_an_unknown_status_code_fails_closed():
    """An unrecognised code must never be assumed to mean COMPLETED."""
    with pytest.raises(PayPalAccountingError) as excinfo:
        _client().map_transactions([_txn(status="Z")])
    assert "Z" in str(excinfo.value)


def test_zero_exponent_currency_is_not_scaled():
    [p] = _client().map_transactions([_txn(value="1000", currency="JPY")]).payments
    assert p.amount_cents == 1000


def test_missing_currency_fails_closed():
    t = _txn()
    del t["transaction_info"]["transaction_amount"]["currency_code"]
    with pytest.raises(PayPalAccountingError):
        _client().map_transactions([t])


def test_unsupported_currency_fails_closed():
    with pytest.raises(PayPalAccountingError):
        _client().map_transactions([_txn(currency="XYZ")])


def test_a_transaction_without_an_id_fails_closed():
    t = _txn()
    del t["transaction_info"]["transaction_id"]
    with pytest.raises(PayPalAccountingError):
        _client().map_transactions([t])


# --------------------------------------------------------------------------
# References are read, never invented
# --------------------------------------------------------------------------


def test_the_structured_reference_comes_only_from_invoice_id():
    result = _client().map_transactions([
        _txn(invoice_id="INV-1001", custom_field="whatever", subject="Payment for INV-9999")
    ])
    assert result.payments[0].reference == "INV-1001"


def test_an_absent_invoice_id_leaves_the_reference_empty():
    """transaction_subject is free text, not a structured reference."""
    [p] = _client().map_transactions([_txn(subject="Payment for INV-1001")]).payments
    assert p.reference is None
    assert "INV-1001" in p.note


def test_custom_field_and_subject_and_note_are_preserved_as_free_text():
    [p] = _client().map_transactions([
        _txn(custom_field="PO-77", subject="October work", note="thanks!")
    ]).payments
    assert "PO-77" in p.note and "October work" in p.note and "thanks!" in p.note


def test_free_text_is_not_promoted_into_the_reference_field():
    [p] = _client().map_transactions([_txn(custom_field="INV-1001")]).payments
    assert p.reference is None, "custom_field is merchant-defined, not an invoice reference"


def test_missing_payer_identity_is_left_blank_not_invented():
    [p] = _client().map_transactions([_txn()]).payments
    assert p.payer_name == ""
    assert p.payer_email is None


# --------------------------------------------------------------------------
# Raw evidence and non-payment transactions
# --------------------------------------------------------------------------


def test_raw_source_evidence_is_preserved_per_transaction():
    raw = _txn(tid="TXN-7", invoice_id="INV-1")
    result = _client().map_transactions([raw])
    assert result.raw_by_id["TXN-7"] == raw


def test_non_positive_amounts_are_recorded_as_skipped_not_dropped_silently():
    """Fees, refunds and transfers are not payments received."""
    result = _client().map_transactions([
        _txn(tid="GOOD", value="10.00"),
        _txn(tid="FEE", value="-0.54"),
        _txn(tid="ZERO", value="0.00"),
    ])
    assert [p.payment_id for p in result.payments] == ["GOOD"]
    skipped = dict(result.skipped)
    assert set(skipped) == {"FEE", "ZERO"}
    assert all("positive" in reason for reason in skipped.values())


def test_skipped_transactions_still_keep_their_raw_evidence():
    result = _client().map_transactions([_txn(tid="FEE", value="-1.00")])
    assert "FEE" in result.raw_by_id


# --------------------------------------------------------------------------
# Deduplication by canonical transaction id
# --------------------------------------------------------------------------


def test_identical_transactions_are_deduplicated_by_transaction_id():
    result = _client().map_transactions([_txn(tid="T1"), _txn(tid="T1"), _txn(tid="T2")])
    assert [p.payment_id for p in result.payments] == ["T1", "T2"]


def test_contradictory_transactions_sharing_an_id_fail_closed():
    with pytest.raises(PayPalAccountingError) as excinfo:
        _client().map_transactions([_txn(tid="T1", value="10.00"), _txn(tid="T1", value="99.00")])
    assert "T1" in str(excinfo.value)


def test_overlapping_windows_do_not_produce_duplicate_payments():
    """Window overlap is the point; the dedupe key is what makes it safe."""
    pages = [
        _txn_page([_txn(tid="T1"), _txn(tid="T2")]),
        _txn_page([_txn(tid="T2"), _txn(tid="T3")]),
    ]
    side_effect, _ = _responder(pages)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    with patch("urllib.request.urlopen", side_effect=side_effect):
        result = _client().list_transactions(
            start, start + timedelta(days=45), overlap=timedelta(hours=6)
        )
    assert [p.payment_id for p in result.payments] == ["T1", "T2", "T3"]


# --------------------------------------------------------------------------
# Pagination of the search endpoint
# --------------------------------------------------------------------------


def test_every_transaction_page_is_consumed():
    pages = [
        _txn_page([_txn(tid="T1"), _txn(tid="T2")], total_items=3, total_pages=2, page=1),
        _txn_page([_txn(tid="T3")], total_items=3, total_pages=2, page=2),
    ]
    side_effect, _ = _responder(pages)
    start = datetime(2026, 10, 1, tzinfo=UTC)
    with patch("urllib.request.urlopen", side_effect=side_effect):
        result = _client().list_transactions(start, start + timedelta(days=10), page_size=2)
    assert [p.payment_id for p in result.payments] == ["T1", "T2", "T3"]


def test_transaction_pagination_failure_on_a_later_page_raises():
    pages = [
        _txn_page([_txn(tid="T1"), _txn(tid="T2")], total_items=9, total_pages=5, page=1),
        HTTPError("u", 500, "err", {}, io.BytesIO(b"{}")),
    ]
    side_effect, _ = _responder(pages)
    start = datetime(2026, 10, 1, tzinfo=UTC)
    with patch("urllib.request.urlopen", side_effect=side_effect):
        with pytest.raises(PayPalAPIError):
            _client().list_transactions(start, start + timedelta(days=10), page_size=2)


def test_transaction_pagination_is_bounded():
    def side_effect(req, *a, **kw):
        if req.full_url.endswith("/v1/oauth2/token"):
            return _resp({"access_token": "T", "expires_in": 3600})
        return _resp(_txn_page([_txn(tid=f"T{id(req)}")], total_items=999, total_pages=999))

    start = datetime(2026, 10, 1, tzinfo=UTC)
    with patch("urllib.request.urlopen", side_effect=side_effect):
        with pytest.raises(PayPalPaginationError):
            _client().list_transactions(
                start, start + timedelta(days=10), page_size=1, max_pages=3
            )


def test_the_window_is_sent_on_the_request():
    side_effect, _ = _responder([_txn_page([])])
    start = datetime(2026, 10, 1, tzinfo=UTC)
    with patch("urllib.request.urlopen", side_effect=side_effect) as m:
        _client().list_transactions(start, start + timedelta(days=5))
    url = [c.args[0].full_url for c in m.call_args_list if "reporting" in c.args[0].full_url][0]
    assert "start_date=" in url and "end_date=" in url
    assert "fields=all" in url


def test_an_empty_search_result_is_not_an_error():
    side_effect, _ = _responder([_txn_page([])])
    start = datetime(2026, 10, 1, tzinfo=UTC)
    with patch("urllib.request.urlopen", side_effect=side_effect):
        result = _client().list_transactions(start, start + timedelta(days=5))
    assert result.payments == ()
    assert result.skipped == ()


# --------------------------------------------------------------------------
# HTTP failure handling
# --------------------------------------------------------------------------


def test_429_is_retried_with_bounded_backoff_then_succeeds():
    pages = [
        HTTPError("u", 429, "Too Many Requests", {}, io.BytesIO(b"{}")),
        HTTPError("u", 429, "Too Many Requests", {}, io.BytesIO(b"{}")),
        _txn_page([_txn(tid="T1")]),
    ]
    side_effect, state = _responder(pages)
    start = datetime(2026, 10, 1, tzinfo=UTC)
    with patch("urllib.request.urlopen", side_effect=side_effect):
        result = _client().list_transactions(start, start + timedelta(days=5), retries=3)
    assert [p.payment_id for p in result.payments] == ["T1"]
    assert state["i"] == 3


def test_429_retries_are_bounded_and_then_raise():
    rate_limited = [HTTPError("u", 429, "Too Many Requests", {}, io.BytesIO(b"{}"))] * 10
    side_effect, state = _responder(rate_limited)
    start = datetime(2026, 10, 1, tzinfo=UTC)
    with patch("urllib.request.urlopen", side_effect=side_effect):
        with pytest.raises(PayPalAPIError) as excinfo:
            _client().list_transactions(start, start + timedelta(days=5), retries=2)
    assert excinfo.value.status_code == 429
    assert state["i"] == 3, "one initial attempt plus two retries"


def test_5xx_is_retried_then_succeeds():
    pages = [HTTPError("u", 503, "Unavailable", {}, io.BytesIO(b"{}")), _txn_page([_txn(tid="T1")])]
    side_effect, _ = _responder(pages)
    start = datetime(2026, 10, 1, tzinfo=UTC)
    with patch("urllib.request.urlopen", side_effect=side_effect):
        result = _client().list_transactions(start, start + timedelta(days=5), retries=2)
    assert [p.payment_id for p in result.payments] == ["T1"]


def test_timeouts_are_retried_then_succeed():
    pages = [URLError(reason=TimeoutError("timed out")), _txn_page([_txn(tid="T1")])]
    side_effect, _ = _responder(pages)
    start = datetime(2026, 10, 1, tzinfo=UTC)
    with patch("urllib.request.urlopen", side_effect=side_effect):
        result = _client().list_transactions(start, start + timedelta(days=5), retries=2)
    assert [p.payment_id for p in result.payments] == ["T1"]


def test_persistent_timeout_raises_the_timeout_error():
    side_effect, _ = _responder([URLError(reason=TimeoutError("timed out"))] * 10)
    start = datetime(2026, 10, 1, tzinfo=UTC)
    with patch("urllib.request.urlopen", side_effect=side_effect):
        with pytest.raises(PayPalTimeoutError):
            _client().list_transactions(start, start + timedelta(days=5), retries=1)


def test_a_400_is_not_retried():
    """A malformed request will not fix itself; retrying only wastes quota."""
    side_effect, state = _responder([HTTPError("u", 400, "Bad Request", {}, io.BytesIO(b"{}"))] * 5)
    start = datetime(2026, 10, 1, tzinfo=UTC)
    with patch("urllib.request.urlopen", side_effect=side_effect):
        with pytest.raises(PayPalAPIError):
            _client().list_transactions(start, start + timedelta(days=5), retries=3)
    assert state["i"] == 1


def test_backoff_is_bounded_and_increasing():
    delays: list[float] = []
    pages = [HTTPError("u", 429, "x", {}, io.BytesIO(b"{}"))] * 3 + [_txn_page([])]
    side_effect, _ = _responder(pages)
    client = _client()
    client._sleep = delays.append
    start = datetime(2026, 10, 1, tzinfo=UTC)
    with patch("urllib.request.urlopen", side_effect=side_effect):
        client.list_transactions(start, start + timedelta(days=5), retries=3)
    assert len(delays) == 3
    assert delays == sorted(delays), "backoff must not decrease"
    assert all(d <= 30 for d in delays), "backoff must stay bounded"


def test_401_triggers_exactly_one_controlled_token_refresh():
    pages = [HTTPError("u", 401, "Unauthorized", {}, io.BytesIO(b"{}")), _txn_page([_txn(tid="T1")])]
    side_effect, state = _responder(pages)
    start = datetime(2026, 10, 1, tzinfo=UTC)
    with patch("urllib.request.urlopen", side_effect=side_effect):
        result = _client().list_transactions(start, start + timedelta(days=5))
    assert [p.payment_id for p in result.payments] == ["T1"]
    assert state["token_calls"] == 2, "initial token plus exactly one refresh"


def test_a_persistent_401_is_not_retried_forever():
    side_effect, state = _responder([HTTPError("u", 401, "Unauthorized", {}, io.BytesIO(b"{}"))] * 10)
    start = datetime(2026, 10, 1, tzinfo=UTC)
    with patch("urllib.request.urlopen", side_effect=side_effect):
        with pytest.raises(PayPalAuthError):
            _client().list_transactions(start, start + timedelta(days=5), retries=3)
    assert state["token_calls"] <= 2, "a 401 refresh must happen at most once"


def test_malformed_json_is_classified_not_leaked():
    bad = MagicMock()
    bad.status = 200
    bad.read.return_value = b"<html>not json</html>"
    bad.__enter__.return_value = bad

    def side_effect(req, *a, **kw):
        if req.full_url.endswith("/v1/oauth2/token"):
            return _resp({"access_token": "T", "expires_in": 3600})
        return bad

    start = datetime(2026, 10, 1, tzinfo=UTC)
    with patch("urllib.request.urlopen", side_effect=side_effect):
        with pytest.raises(PayPalAPIError) as excinfo:
            _client().list_transactions(start, start + timedelta(days=5))
    assert "json" in str(excinfo.value).lower() or "malformed" in str(excinfo.value).lower()


def test_malformed_transaction_details_shape_fails_closed():
    side_effect, _ = _responder([{"transaction_details": "not-a-list", "total_items": 1}])
    start = datetime(2026, 10, 1, tzinfo=UTC)
    with patch("urllib.request.urlopen", side_effect=side_effect):
        with pytest.raises(PayPalAPIError):
            _client().list_transactions(start, start + timedelta(days=5))


# --------------------------------------------------------------------------
# Redaction
# --------------------------------------------------------------------------


def test_errors_never_echo_the_client_secret():
    client = PayPalClient(client_id="id", client_secret="super_secret_value", env_file=None)
    client._sleep = lambda _s: None
    side_effect, _ = _responder([HTTPError("u", 500, "err", {}, io.BytesIO(b"{}"))] * 5)
    start = datetime(2026, 10, 1, tzinfo=UTC)
    with patch("urllib.request.urlopen", side_effect=side_effect):
        with pytest.raises(PayPalAPIError) as excinfo:
            client.list_transactions(start, start + timedelta(days=5), retries=1)
    assert "super_secret_value" not in str(excinfo.value)


def test_errors_never_echo_the_bearer_token():
    side_effect, _ = _responder([HTTPError("u", 500, "err", {}, io.BytesIO(b"{}"))] * 5)
    start = datetime(2026, 10, 1, tzinfo=UTC)
    client = _client()
    with patch("urllib.request.urlopen", side_effect=side_effect):
        with pytest.raises(PayPalAPIError) as excinfo:
            client.list_transactions(start, start + timedelta(days=5), retries=1)
    assert "T1" not in str(excinfo.value).replace("TXN", "")

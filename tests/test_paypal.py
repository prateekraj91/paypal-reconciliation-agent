"""Mocked tests for PayPalClient and invoice mapping.

Zero network access and zero real credentials required.
"""

from __future__ import annotations

import io
import json
import time
from datetime import date
from urllib.error import HTTPError, URLError
from unittest.mock import MagicMock, patch

import pytest

from reconcile.deterministic import DeterministicMatcher
from reconcile.interfaces import InvoiceSource
from reconcile.models import (
    AllocationStatus,
    Candidate,
    EvidenceCode,
    Invoice,
    InvoiceStatus,
    MatchOutcome,
    MatchProposal,
    PaymentAllocation,
    PaymentStatus,
    outstanding_cents,
)
from reconcile.paypal import (
    PayPalAPIError,
    PayPalAccountingError,
    PayPalAuthError,
    PayPalClient,
    PayPalConfigError,
    PayPalNotFoundError,
    PayPalTimeoutError,
    _parse_money_to_cents,
)
from factories import pay


SAMPLE_OAUTH_RESPONSE = {
    "scope": "https://uri.paypal.com/services/invoicing",
    "access_token": "ACCESS-TOKEN-SECRET-12345",
    "token_type": "Bearer",
    "app_id": "APP-80W284485P519543T",
    "expires_in": 3600,
    "nonce": "test-nonce",
}

SAMPLE_INVOICE_0001_RAW = {
    "id": "INV2-4UGC-HWFL-A37V-3C27",
    "status": "DRAFT",
    "detail": {
        "invoice_number": "0001",
        "invoice_date": "2026-10-30",
        "payment_term": {
            "term_type": "DUE_ON_RECEIPT",
            "due_date": "2026-10-30",
        },
        "currency_code": "USD",
        "memo": "Thank you for your business",
    },
    "invoicer": {},
    "primary_recipients": [
        {
            "billing_info": {
                "name": {
                    "given_name": "John",
                    "surname": "Doe",
                    "full_name": "John Doe",
                },
                "email_address": "sb-4315gt53168691@personal.example.com",
            }
        }
    ],
    "amount": {
        "currency_code": "USD",
        "value": "25.00",
        "breakdown": {
            "item_total": {
                "currency_code": "USD",
                "value": "25.00",
            }
        },
    },
    "due_amount": {
        "currency_code": "USD",
        "value": "25.00",
    },
    "payments": None,
}

SAMPLE_PARTIALLY_PAID_INVOICE_RAW = {
    "id": "INV2-PARTIAL-123",
    "status": "PARTIALLY_PAID",
    "detail": {
        "invoice_number": "0002",
        "invoice_date": "2026-10-25",
        "payment_term": {
            "due_date": "2026-11-25",
        },
        "currency_code": "USD",
    },
    "primary_recipients": [
        {
            "billing_info": {
                "email_address": "client@example.com",
            }
        }
    ],
    "amount": {
        "currency_code": "USD",
        "value": "100.00",
    },
    "due_amount": {
        "currency_code": "USD",
        "value": "60.00",
    },
    "payments": {
        "paid_amount": {
            "currency_code": "USD",
            "value": "40.00",
        },
        "transactions": [
            {
                "payment_id": "CAPTURE-999",
                "payment_date": "2026-10-26",
                "amount": {
                    "currency_code": "USD",
                    "value": "40.00",
                },
            }
        ],
    },
}


def _mock_http_response(status_code: int, payload: dict):
    resp = MagicMock()
    resp.status = status_code
    resp.read.return_value = json.dumps(payload).encode("utf-8")
    resp.__enter__.return_value = resp
    return resp


# --------------------------------------------------------------------------
# Configuration and credential safety
# --------------------------------------------------------------------------


def test_client_init_with_explicit_credentials():
    client = PayPalClient(
        client_id="my_client_id",
        client_secret="my_secret",
        base_url="https://api-m.sandbox.paypal.com",
    )
    assert client.client_id == "my_client_id"
    assert client.base_url == "https://api-m.sandbox.paypal.com"
    # repr must never leak secret
    rep = repr(client)
    assert "my_secret" not in rep
    assert "ACCESS-TOKEN" not in rep


def test_client_init_fails_closed_when_credentials_missing(monkeypatch, tmp_path):
    monkeypatch.delenv("PAYPAL_CLIENT_ID", raising=False)
    monkeypatch.delenv("PAYPAL_CLIENT_SECRET", raising=False)
    dummy_env = tmp_path / "empty.env"
    dummy_env.write_text("")

    with pytest.raises(PayPalConfigError) as exc_info:
        PayPalClient(env_file=str(dummy_env))
    assert "PAYPAL_CLIENT_ID" in str(exc_info.value) or "credentials" in str(exc_info.value).lower()


def test_client_satisfies_invoice_source_protocol():
    client = PayPalClient(client_id="id", client_secret="sec")
    assert isinstance(client, InvoiceSource)


# --------------------------------------------------------------------------
# OAuth token caching
# --------------------------------------------------------------------------


def test_token_caching_and_refresh():
    client = PayPalClient(client_id="id", client_secret="secret")

    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value = _mock_http_response(200, SAMPLE_OAUTH_RESPONSE)

        # First call fetches token
        tok1 = client.get_access_token()
        assert tok1 == "ACCESS-TOKEN-SECRET-12345"
        assert mock_urlopen.call_count == 1

        # Second immediate call returns cached token without HTTP request
        tok2 = client.get_access_token()
        assert tok2 == tok1
        assert mock_urlopen.call_count == 1

        # Simulate expiry: advance client expiration timestamp
        client._token_expires_at = time.monotonic() - 1
        tok3 = client.get_access_token()
        assert tok3 == "ACCESS-TOKEN-SECRET-12345"
        assert mock_urlopen.call_count == 2


# --------------------------------------------------------------------------
# Invoice mapping & Invoicing API v2
# --------------------------------------------------------------------------


def test_get_invoice_0001_mapping():
    client = PayPalClient(client_id="id", client_secret="secret")

    with patch("urllib.request.urlopen") as mock_urlopen:
        # 1st call: OAuth token, 2nd call: invoice retrieval
        mock_urlopen.side_effect = [
            _mock_http_response(200, SAMPLE_OAUTH_RESPONSE),
            _mock_http_response(200, SAMPLE_INVOICE_0001_RAW),
        ]

        invoice = client.get_invoice("INV2-4UGC-HWFL-A37V-3C27")

        assert isinstance(invoice, Invoice)
        assert invoice.invoice_id == "INV2-4UGC-HWFL-A37V-3C27"
        assert invoice.number == "0001"
        assert invoice.customer_name == "John Doe"
        assert invoice.customer_email == "sb-4315gt53168691@personal.example.com"
        assert invoice.amount_cents == 2500  # $25.00 USD
        assert invoice.due_amount_cents == 2500
        assert invoice.currency == "USD"
        assert invoice.issued_on == date(2026, 10, 30)
        assert invoice.due_on == date(2026, 10, 30)
        assert invoice.status == InvoiceStatus.DRAFT
        # Critical requirement: DRAFT invoices must not be open
        assert not invoice.is_open


def test_list_invoices_mapping():
    client = PayPalClient(client_id="id", client_secret="secret")
    list_payload = {
        "total_items": 2,
        "total_pages": 1,
        "items": [SAMPLE_INVOICE_0001_RAW, SAMPLE_PARTIALLY_PAID_INVOICE_RAW],
    }

    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.side_effect = [
            _mock_http_response(200, SAMPLE_OAUTH_RESPONSE),
            _mock_http_response(200, list_payload),
        ]

        invoices = client.list_invoices()
        assert len(invoices) == 2
        inv1, inv2 = invoices
        assert inv1.number == "0001"
        assert inv1.status == InvoiceStatus.DRAFT
        assert inv2.number == "0002"
        assert inv2.status == InvoiceStatus.PARTIALLY_PAID
        assert inv2.amount_cents == 10000
        assert inv2.due_amount_cents == 6000


def test_draft_invoice_is_not_automatically_matchable():
    client = PayPalClient(client_id="id", client_secret="secret")
    invoice = client.map_invoice(SAMPLE_INVOICE_0001_RAW)

    assert invoice.status == InvoiceStatus.DRAFT
    assert not invoice.is_open

    matcher = DeterministicMatcher()
    payment = pay(1, 2500, reference="0001")
    proposals = matcher.match([payment], [invoice])

    assert proposals[0].outcome is MatchOutcome.NO_MATCH
    assert any(e.code is EvidenceCode.INVOICE_NOT_OPEN for e in proposals[0].reasons)


# --------------------------------------------------------------------------
# Avoiding double-counting payments already linked by PayPal
# --------------------------------------------------------------------------


def test_avoid_double_counting_payments_already_linked_by_paypal():
    client = PayPalClient(client_id="id", client_secret="secret")
    invoice = client.map_invoice(SAMPLE_PARTIALLY_PAID_INVOICE_RAW)

    # 1. Total billed vs due amount distinction
    assert invoice.amount_cents == 10000
    assert invoice.due_amount_cents == 6000

    # 2. Extract approved allocations already linked by PayPal
    linked_allocs = client.extract_linked_allocations(SAMPLE_PARTIALLY_PAID_INVOICE_RAW)
    assert len(linked_allocs) == 1
    alloc = linked_allocs[0]
    assert alloc.payment_id == "CAPTURE-999"
    assert alloc.invoice_id == "INV2-PARTIAL-123"
    assert alloc.amount_cents == 4000
    assert alloc.status == AllocationStatus.APPROVED

    # 3. With approved linked allocation, outstanding balance matches due_amount
    remaining = outstanding_cents(invoice, linked_allocs)
    assert remaining == 6000 == invoice.due_amount_cents

    # 4. If a payment for $60 arrives, it can match the remaining balance
    matcher = DeterministicMatcher()
    p_remaining = pay(2, 6000, reference="0002")
    proposals = matcher.match([p_remaining], [invoice], approved=linked_allocs)
    assert proposals[0].outcome is MatchOutcome.PROPOSED
    assert proposals[0].candidates[0].allocations[0].amount_cents == 6000


# --------------------------------------------------------------------------
# Error handling and timeouts
# --------------------------------------------------------------------------


def test_auth_error_handling_sanitizes_secrets():
    client = PayPalClient(client_id="test_id", client_secret="super_secret_password")

    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.side_effect = HTTPError(
            url="https://api-m.sandbox.paypal.com/v1/oauth2/token",
            code=401,
            msg="Unauthorized",
            hdrs={},
            fp=io.BytesIO(b'{"error": "invalid_client", "error_description": "Client Authentication failed"}'),
        )

        with pytest.raises(PayPalAuthError) as exc_info:
            client.get_access_token()

        err_msg = str(exc_info.value)
        assert "super_secret_password" not in err_msg
        assert "401" in err_msg


def test_not_found_error_handling():
    client = PayPalClient(client_id="id", client_secret="secret")

    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.side_effect = [
            _mock_http_response(200, SAMPLE_OAUTH_RESPONSE),
            HTTPError(
                url="https://api-m.sandbox.paypal.com/v2/invoicing/invoices/NONEXISTENT",
                code=404,
                msg="Not Found",
                hdrs={},
                fp=io.BytesIO(b'{"name": "RESOURCE_NOT_FOUND", "message": "The specified resource does not exist."}'),
            ),
        ]

        with pytest.raises(PayPalNotFoundError) as exc_info:
            client.get_invoice("NONEXISTENT")
        assert "RESOURCE_NOT_FOUND" in str(exc_info.value) or "404" in str(exc_info.value)


def test_timeout_error_handling():
    client = PayPalClient(client_id="id", client_secret="secret", timeout=2.0)

    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.side_effect = URLError(reason=TimeoutError("timed out"))

        with pytest.raises(PayPalTimeoutError) as exc_info:
            client.get_access_token()
        assert "timed out" in str(exc_info.value).lower()


# --------------------------------------------------------------------------
# Money parsing helper
# --------------------------------------------------------------------------


def test_parse_money_to_cents():
    assert _parse_money_to_cents("25.00") == 2500
    assert _parse_money_to_cents("25") == 2500
    assert _parse_money_to_cents("0.01") == 1
    assert _parse_money_to_cents("10.01") == 1001
    assert _parse_money_to_cents(25) == 2500
    assert _parse_money_to_cents(25.50) == 2550
    with pytest.raises(ValueError):
        _parse_money_to_cents("invalid")


# --------------------------------------------------------------------------
# Accounting Invariant Audit Tests
# --------------------------------------------------------------------------


def test_accounting_invariant_1_partial_payment():
    """1. $25 invoice, $10 already paid, $15 due: calculated balance must be $15."""
    raw = {
        "id": "INV-AUDIT-1",
        "status": "PARTIALLY_PAID",
        "detail": {"invoice_number": "AUDIT-0001", "invoice_date": "2026-10-31"},
        "amount": {"currency_code": "USD", "value": "25.00"},
        "due_amount": {"currency_code": "USD", "value": "15.00"},
        "payments": {
            "paid_amount": {"currency_code": "USD", "value": "10.00"},
            "transactions": [
                {
                    "payment_id": "CAPTURE-10",
                    "amount": {"currency_code": "USD", "value": "10.00"},
                }
            ],
        },
    }
    client = PayPalClient(client_id="id", client_secret="secret")
    invoice = client.map_invoice(raw)
    linked = client.extract_linked_allocations(raw)

    # Invariant checks:
    assert invoice.amount_cents == 2500
    assert len(linked) == 1
    assert linked[0].amount_cents == 1000
    assert linked[0].status == AllocationStatus.APPROVED

    calc_balance = outstanding_cents(invoice, linked)
    assert calc_balance == 1500
    assert invoice.due_amount_cents == 1500
    # Both calculations must agree
    assert calc_balance == invoice.due_amount_cents


def test_accounting_invariant_2_fully_paid():
    """2. Fully paid $25 invoice, $25 linked payment, zero due: calculated balance must be zero."""
    raw = {
        "id": "INV-AUDIT-2",
        "status": "PAID",
        "detail": {"invoice_number": "AUDIT-0002", "invoice_date": "2026-10-31"},
        "amount": {"currency_code": "USD", "value": "25.00"},
        "due_amount": {"currency_code": "USD", "value": "0.00"},
        "payments": {
            "paid_amount": {"currency_code": "USD", "value": "25.00"},
            "transactions": [
                {
                    "payment_id": "CAPTURE-25",
                    "amount": {"currency_code": "USD", "value": "25.00"},
                }
            ],
        },
    }
    client = PayPalClient(client_id="id", client_secret="secret")
    invoice = client.map_invoice(raw)
    linked = client.extract_linked_allocations(raw)

    assert invoice.amount_cents == 2500
    assert sum(a.amount_cents for a in linked) == 2500
    calc_balance = outstanding_cents(invoice, linked)
    assert calc_balance == 0
    assert invoice.due_amount_cents == 0
    assert calc_balance == invoice.due_amount_cents


def test_accounting_invariant_3_unpaid():
    """3. No linked payments and $25 due: outstanding balance must be $25."""
    raw = {
        "id": "INV-AUDIT-3",
        "status": "SENT",
        "detail": {"invoice_number": "AUDIT-0003", "invoice_date": "2026-10-31"},
        "amount": {"currency_code": "USD", "value": "25.00"},
        "due_amount": {"currency_code": "USD", "value": "25.00"},
        "payments": None,
    }
    client = PayPalClient(client_id="id", client_secret="secret")
    invoice = client.map_invoice(raw)
    linked = client.extract_linked_allocations(raw)

    assert invoice.amount_cents == 2500
    assert linked == ()
    calc_balance = outstanding_cents(invoice, linked)
    assert calc_balance == 2500
    assert invoice.due_amount_cents == 2500
    assert calc_balance == invoice.due_amount_cents


def test_accounting_invariant_4_disagreement_fails_closed():
    """4. PayPal due_amount disagrees with the sum of linked transactions: detect the inconsistency and fail closed."""
    # Invoice is $25.00 total. Transaction is $10.00.
    # Expected due amount is $15.00 (1500 cents).
    # But PayPal due_amount corruptly reports $12.00 (1200 cents).
    raw = {
        "id": "INV-AUDIT-4",
        "status": "PARTIALLY_PAID",
        "detail": {"invoice_number": "AUDIT-0004", "invoice_date": "2026-10-31"},
        "amount": {"currency_code": "USD", "value": "25.00"},
        "due_amount": {"currency_code": "USD", "value": "12.00"},
        "payments": {
            "paid_amount": {"currency_code": "USD", "value": "10.00"},
            "transactions": [
                {
                    "payment_id": "CAPTURE-10",
                    "amount": {"currency_code": "USD", "value": "10.00"},
                }
            ],
        },
    }
    client = PayPalClient(client_id="id", client_secret="secret")
    with pytest.raises((ValueError, PayPalAPIError)) as exc_info:
        client.extract_linked_allocations(raw)
    assert "due_amount" in str(exc_info.value).lower() or "inconsistency" in str(exc_info.value).lower() or "contradict" in str(exc_info.value).lower()


def test_accounting_invariant_5_repeated_extraction_prevents_duplicate_allocations():
    """5. Repeated extraction of the same PayPal transaction must not cause duplicate financial allocations."""
    # Scenario A: PayPal raw transaction list repeats the same transaction
    raw = {
        "id": "INV-AUDIT-5",
        "status": "PARTIALLY_PAID",
        "detail": {"invoice_number": "AUDIT-0005", "invoice_date": "2026-10-31"},
        "amount": {"currency_code": "USD", "value": "25.00"},
        "due_amount": {"currency_code": "USD", "value": "15.00"},
        "payments": {
            "transactions": [
                {"payment_id": "CAPTURE-REPEAT", "amount": {"currency_code": "USD", "value": "10.00"}},
                {"payment_id": "CAPTURE-REPEAT", "amount": {"currency_code": "USD", "value": "10.00"}},
            ],
        },
    }
    client = PayPalClient(client_id="id", client_secret="secret")
    linked = client.extract_linked_allocations(raw)
    assert len(linked) == 1
    assert linked[0].payment_id == "CAPTURE-REPEAT"

    # Scenario B: Repeatedly passing the same approved allocation to outstanding_cents
    invoice = client.map_invoice(raw)
    repeated_allocs = [linked[0], linked[0]]
    calc_balance = outstanding_cents(invoice, repeated_allocs)
    # Must deduct $10 only once -> $15 remaining, NOT $5
    assert calc_balance == 1500


def test_accounting_invariant_6_proposed_allocations_never_reduce_balance():
    """6. Proposed allocations must never reduce outstanding balance."""
    raw = {
        "id": "INV-AUDIT-6",
        "status": "SENT",
        "detail": {"invoice_number": "AUDIT-0006", "invoice_date": "2026-10-31"},
        "amount": {"currency_code": "USD", "value": "25.00"},
        "due_amount": {"currency_code": "USD", "value": "25.00"},
    }
    client = PayPalClient(client_id="id", client_secret="secret")
    invoice = client.map_invoice(raw)

    prop_alloc = PaymentAllocation("pay_p", invoice.invoice_id, 1000, AllocationStatus.PROPOSED)
    rej_alloc = PaymentAllocation("pay_r", invoice.invoice_id, 500, AllocationStatus.REJECTED)

    # Neither PROPOSED nor REJECTED should reduce the balance
    assert outstanding_cents(invoice, [prop_alloc, rej_alloc]) == 2500


def test_accounting_invariant_7_draft_invoices_cannot_be_matched_or_verified():
    """7. DRAFT invoices cannot be matched or verified."""
    from reconcile.verifier import verify_proposals

    raw = {
        "id": "INV-AUDIT-7",
        "status": "DRAFT",
        "detail": {"invoice_number": "AUDIT-0007", "invoice_date": "2026-10-31"},
        "amount": {"currency_code": "USD", "value": "25.00"},
        "due_amount": {"currency_code": "USD", "value": "25.00"},
    }
    client = PayPalClient(client_id="id", client_secret="secret")
    draft_invoice = client.map_invoice(raw)
    assert draft_invoice.status == InvoiceStatus.DRAFT
    assert not draft_invoice.is_open

    # 1. Matcher must refuse to match
    matcher = DeterministicMatcher()
    payment = pay(1, 2500, reference="AUDIT-0007")
    [proposal] = matcher.match([payment], [draft_invoice])
    assert proposal.outcome is MatchOutcome.NO_MATCH
    assert any(e.code is EvidenceCode.INVOICE_NOT_OPEN for e in proposal.reasons)

    # 2. Verifier must reject any candidate proposing a DRAFT invoice
    cand = Candidate((PaymentAllocation(payment.payment_id, draft_invoice.invoice_id, 2500),))
    forced_prop = MatchProposal(payment.payment_id, MatchOutcome.PROPOSED, (cand,), matcher="test")
    [result] = verify_proposals([forced_prop], [payment], [draft_invoice])
    assert not result.passed
    assert any("not open" in issue.lower() and "draft" in issue.lower() for issue in result.issues)


# --------------------------------------------------------------------------
# Finding 2: Validate transaction currencies
# --------------------------------------------------------------------------


def test_currency_validation_matching_currency():
    """Matching currency creates valid approved allocation."""
    raw = {
        "id": "INV-CURR-1",
        "amount": {"currency_code": "USD", "value": "100.00"},
        "due_amount": {"currency_code": "USD", "value": "60.00"},
        "payments": {
            "transactions": [
                {
                    "payment_id": "CAPTURE-MATCH",
                    "amount": {"currency_code": "USD", "value": "40.00"},
                }
            ]
        },
    }
    client = PayPalClient(client_id="id", client_secret="secret")
    allocs = client.extract_linked_allocations(raw)
    assert len(allocs) == 1
    assert allocs[0].amount_cents == 4000
    assert allocs[0].status == AllocationStatus.APPROVED


def test_currency_validation_case_normalization():
    """Lower-case currency code is normalized and matches invoice currency."""
    raw = {
        "id": "INV-CURR-2",
        "amount": {"currency_code": "USD", "value": "100.00"},
        "due_amount": {"currency_code": "USD", "value": "60.00"},
        "payments": {
            "transactions": [
                {
                    "payment_id": "CAPTURE-CASE",
                    "amount": {"currency_code": "usd", "value": "40.00"},
                }
            ]
        },
    }
    client = PayPalClient(client_id="id", client_secret="secret")
    allocs = client.extract_linked_allocations(raw)
    assert len(allocs) == 1
    assert allocs[0].amount_cents == 4000


def test_currency_validation_mismatched_currency_rejected():
    """Contradictory transaction currency is rejected with PayPalAccountingError."""
    raw = {
        "id": "INV-CURR-3",
        "amount": {"currency_code": "USD", "value": "100.00"},
        "due_amount": {"currency_code": "USD", "value": "60.00"},
        "payments": {
            "transactions": [
                {
                    "payment_id": "CAPTURE-EUR",
                    "amount": {"currency_code": "EUR", "value": "40.00"},
                }
            ]
        },
    }
    client = PayPalClient(client_id="id", client_secret="secret")
    with pytest.raises(PayPalAccountingError) as exc_info:
        client.extract_linked_allocations(raw)
    assert "EUR" in str(exc_info.value) and "USD" in str(exc_info.value)


def test_currency_validation_missing_currency_field_rejected():
    """Transaction missing currency_code is rejected with PayPalAccountingError."""
    raw = {
        "id": "INV-CURR-4",
        "amount": {"currency_code": "USD", "value": "100.00"},
        "due_amount": {"currency_code": "USD", "value": "60.00"},
        "payments": {
            "transactions": [
                {
                    "payment_id": "CAPTURE-NO-CURR",
                    "amount": {"value": "40.00"},
                }
            ]
        },
    }
    client = PayPalClient(client_id="id", client_secret="secret")
    with pytest.raises(PayPalAccountingError) as exc_info:
        client.extract_linked_allocations(raw)
    assert "missing currency_code" in str(exc_info.value)


def test_currency_validation_foreign_currency_cannot_reduce_balance():
    """Foreign currency payment cannot silently reduce invoice balance."""
    raw = {
        "id": "INV-CURR-5",
        "amount": {"currency_code": "USD", "value": "100.00"},
        "due_amount": {"currency_code": "USD", "value": "100.00"},
        "payments": {
            "transactions": [
                {
                    "payment_id": "CAPTURE-GBP",
                    "amount": {"currency_code": "GBP", "value": "50.00"},
                }
            ]
        },
    }
    client = PayPalClient(client_id="id", client_secret="secret")
    with pytest.raises(PayPalAccountingError):
        client.extract_linked_allocations(raw)


def test_due_amount_currency_mismatch_rejected():
    """Mismatched currency in due_amount is rejected with PayPalAccountingError."""
    raw = {
        "id": "INV-CURR-6",
        "amount": {"currency_code": "USD", "value": "100.00"},
        "due_amount": {"currency_code": "CAD", "value": "100.00"},
    }
    client = PayPalClient(client_id="id", client_secret="secret")
    with pytest.raises(PayPalAccountingError) as exc_info:
        client.extract_linked_allocations(raw)
    assert "CAD" in str(exc_info.value)


# --------------------------------------------------------------------------
# Finding 3: Preserve linked payment allocations during ingestion
# --------------------------------------------------------------------------


def test_list_invoices_with_allocations_partial_payment():
    """list_invoices_with_allocations returns invoices and linked allocations for partial payment."""
    raw_item = {
        "id": "INV-LINK-1",
        "status": "PARTIALLY_PAID",
        "detail": {"invoice_number": "LINK-0001", "invoice_date": "2026-10-31"},
        "amount": {"currency_code": "USD", "value": "25.00"},
        "due_amount": {"currency_code": "USD", "value": "15.00"},
        "payments": {
            "paid_amount": {"currency_code": "USD", "value": "10.00"},
            "transactions": [
                {
                    "payment_id": "PAY-PARTIAL-1",
                    "amount": {"currency_code": "USD", "value": "10.00"},
                }
            ],
        },
    }
    client = PayPalClient(client_id="id", client_secret="secret")
    with patch.object(client, "list_raw_invoices", return_value={"items": [raw_item]}):
        invoices, allocations = client.list_invoices_with_allocations()

    assert len(invoices) == 1
    assert invoices[0].invoice_id == "INV-LINK-1"
    assert invoices[0].due_amount_cents == 1500
    assert len(allocations) == 1
    assert allocations[0].payment_id == "PAY-PARTIAL-1"
    assert allocations[0].amount_cents == 1000
    assert allocations[0].status == AllocationStatus.APPROVED

    # Ledger cross-check
    assert outstanding_cents(invoices[0], allocations) == 1500


def test_list_invoices_with_allocations_unpaid_invoice():
    """list_invoices_with_allocations returns invoice and empty allocations for unpaid invoice."""
    raw_item = {
        "id": "INV-LINK-2",
        "status": "SENT",
        "detail": {"invoice_number": "LINK-0002", "invoice_date": "2026-10-31"},
        "amount": {"currency_code": "USD", "value": "25.00"},
        "due_amount": {"currency_code": "USD", "value": "25.00"},
    }
    client = PayPalClient(client_id="id", client_secret="secret")
    with patch.object(client, "list_raw_invoices", return_value={"items": [raw_item]}):
        invoices, allocations = client.list_invoices_with_allocations()

    assert len(invoices) == 1
    assert invoices[0].invoice_id == "INV-LINK-2"
    assert invoices[0].due_amount_cents == 2500
    assert len(allocations) == 0
    assert outstanding_cents(invoices[0], allocations) == 2500


def test_list_invoices_with_allocations_grouped_payment_across_invoices_preserved():
    """list_invoices_with_allocations preserves grouped payment allocations across different invoices."""
    item1 = {
        "id": "INV-LINK-3A",
        "status": "PARTIALLY_PAID",
        "detail": {"invoice_number": "LINK-0003A", "invoice_date": "2026-10-31"},
        "amount": {"currency_code": "USD", "value": "25.00"},
        "due_amount": {"currency_code": "USD", "value": "15.00"},
        "payments": {
            "transactions": [
                {"payment_id": "SHARED-TXN-1", "amount": {"currency_code": "USD", "value": "10.00"}},
            ],
        },
    }
    item2 = {
        "id": "INV-LINK-3B",
        "status": "PARTIALLY_PAID",
        "detail": {"invoice_number": "LINK-0003B", "invoice_date": "2026-10-31"},
        "amount": {"currency_code": "USD", "value": "25.00"},
        "due_amount": {"currency_code": "USD", "value": "15.00"},
        "payments": {
            "transactions": [
                {"payment_id": "SHARED-TXN-1", "amount": {"currency_code": "USD", "value": "10.00"}},
            ],
        },
    }
    client = PayPalClient(client_id="id", client_secret="secret")
    with patch.object(client, "list_raw_invoices", return_value={"items": [item1, item2]}):
        invoices, allocations = client.list_invoices_with_allocations()

    assert len(invoices) == 2
    # SHARED-TXN-1 belongs to both invoices and both allocations must be preserved
    assert len(allocations) == 2
    assert {a.invoice_id for a in allocations} == {"INV-LINK-3A", "INV-LINK-3B"}
    assert all(a.payment_id == "SHARED-TXN-1" for a in allocations)
    assert all(a.amount_cents == 1000 for a in allocations)


def test_list_invoices_with_allocations_identical_duplicates_deduplicated():
    """Identical duplicate allocations for the same invoice are safely deduplicated to 1."""
    item = {
        "id": "INV-DUP-1",
        "status": "PARTIALLY_PAID",
        "detail": {"invoice_number": "DUP-0001", "invoice_date": "2026-10-31"},
        "amount": {"currency_code": "USD", "value": "50.00"},
        "due_amount": {"currency_code": "USD", "value": "30.00"},
        "payments": {
            "transactions": [
                {"payment_id": "DUP-TXN-1", "amount": {"currency_code": "USD", "value": "20.00"}},
                {"payment_id": "DUP-TXN-1", "amount": {"currency_code": "USD", "value": "20.00"}},
            ],
        },
    }
    client = PayPalClient(client_id="id", client_secret="secret")
    with patch.object(client, "list_raw_invoices", return_value={"items": [item, item]}):
        invoices, allocations = client.list_invoices_with_allocations()

    assert len(invoices) == 2
    # Identical transactions on the same invoice deduplicate safely to 1
    assert len(allocations) == 1
    assert allocations[0].payment_id == "DUP-TXN-1"
    assert allocations[0].amount_cents == 2000


def test_list_invoices_with_allocations_conflicting_amounts_fails_closed():
    """Conflicting amounts for the same payment and invoice fail closed with PayPalAccountingError."""
    item = {
        "id": "INV-CONFLICT-1",
        "status": "PARTIALLY_PAID",
        "detail": {"invoice_number": "CONFLICT-0001", "invoice_date": "2026-10-31"},
        "amount": {"currency_code": "USD", "value": "50.00"},
        "due_amount": {"currency_code": "USD", "value": "20.00"},
        "payments": {
            "transactions": [
                {"payment_id": "CONFLICT-TXN-1", "amount": {"currency_code": "USD", "value": "10.00"}},
                {"payment_id": "CONFLICT-TXN-1", "amount": {"currency_code": "USD", "value": "20.00"}},
            ],
        },
    }
    client = PayPalClient(client_id="id", client_secret="secret")
    with patch.object(client, "list_raw_invoices", return_value={"items": [item]}):
        with pytest.raises(PayPalAccountingError) as exc_info:
            client.list_invoices_with_allocations()
    assert "conflicting allocation amount" in str(exc_info.value).lower()


def test_transaction_with_unapproved_status_rejected():
    """Transactions with non-completed status (e.g. PENDING) are rejected."""
    raw = {
        "id": "INV-STATUS-1",
        "amount": {"currency_code": "USD", "value": "50.00"},
        "due_amount": {"currency_code": "USD", "value": "25.00"},
        "payments": {
            "transactions": [
                {
                    "payment_id": "TXN-PENDING",
                    "status": "PENDING",
                    "amount": {"currency_code": "USD", "value": "25.00"},
                }
            ]
        },
    }
    client = PayPalClient(client_id="id", client_secret="secret")
    with pytest.raises(PayPalAccountingError) as exc_info:
        client.extract_linked_allocations(raw)
    assert "unapproved status" in str(exc_info.value) and "PENDING" in str(exc_info.value)


# --------------------------------------------------------------------------
# Finding 4: Improve invalid amount diagnostics
# --------------------------------------------------------------------------


def test_transaction_with_zero_amount_rejected():
    """Transaction with 0 amount is explicitly rejected with PayPalAccountingError."""
    raw = {
        "id": "INV-AMT-ZERO",
        "amount": {"currency_code": "USD", "value": "50.00"},
        "due_amount": {"currency_code": "USD", "value": "50.00"},
        "payments": {
            "transactions": [
                {
                    "payment_id": "TXN-ZERO",
                    "amount": {"currency_code": "USD", "value": "0.00"},
                }
            ]
        },
    }
    client = PayPalClient(client_id="id", client_secret="secret")
    with pytest.raises(PayPalAccountingError) as exc_info:
        client.extract_linked_allocations(raw)
    assert "zero amount" in str(exc_info.value).lower()


def test_transaction_with_negative_amount_rejected():
    """Transaction with negative amount is explicitly rejected with PayPalAccountingError."""
    raw = {
        "id": "INV-AMT-NEG",
        "amount": {"currency_code": "USD", "value": "50.00"},
        "due_amount": {"currency_code": "USD", "value": "50.00"},
        "payments": {
            "transactions": [
                {
                    "payment_id": "TXN-NEG",
                    "amount": {"currency_code": "USD", "value": "-10.00"},
                }
            ]
        },
    }
    client = PayPalClient(client_id="id", client_secret="secret")
    with pytest.raises(PayPalAccountingError) as exc_info:
        client.extract_linked_allocations(raw)
    assert "negative amount" in str(exc_info.value).lower()


def test_transaction_with_unsupported_type_refund_rejected():
    """Transaction with type REFUND or ADJUSTMENT is rejected with PayPalAccountingError."""
    raw = {
        "id": "INV-TYPE-REFUND",
        "amount": {"currency_code": "USD", "value": "50.00"},
        "due_amount": {"currency_code": "USD", "value": "50.00"},
        "payments": {
            "transactions": [
                {
                    "payment_id": "TXN-REF",
                    "type": "REFUND",
                    "amount": {"currency_code": "USD", "value": "10.00"},
                }
            ]
        },
    }
    client = PayPalClient(client_id="id", client_secret="secret")
    with pytest.raises(PayPalAccountingError) as exc_info:
        client.extract_linked_allocations(raw)
    assert "unsupported type" in str(exc_info.value).lower() and "refund" in str(exc_info.value).lower()


def test_invoice_with_refunds_block_rejected():
    """Invoice containing recorded refunds is rejected for automated payment allocation."""
    raw = {
        "id": "INV-REFUNDS-BLOCK",
        "amount": {"currency_code": "USD", "value": "50.00"},
        "due_amount": {"currency_code": "USD", "value": "30.00"},
        "payments": {
            "transactions": [
                {
                    "payment_id": "TXN-PAID",
                    "amount": {"currency_code": "USD", "value": "30.00"},
                }
            ]
        },
        "refunds": {
            "refund_amount": {"currency_code": "USD", "value": "10.00"},
            "transactions": [
                {
                    "refund_id": "REF-1",
                    "amount": {"currency_code": "USD", "value": "10.00"},
                }
            ],
        },
    }
    client = PayPalClient(client_id="id", client_secret="secret")
    with pytest.raises(PayPalAccountingError) as exc_info:
        client.extract_linked_allocations(raw)
    assert "refunds" in str(exc_info.value).lower()


def test_supported_positive_amount_creates_approved_allocation():
    """Supported positive transaction creates an approved allocation with correct amount."""
    raw = {
        "id": "INV-AMT-POS",
        "amount": {"currency_code": "USD", "value": "50.00"},
        "due_amount": {"currency_code": "USD", "value": "34.50"},
        "payments": {
            "transactions": [
                {
                    "payment_id": "TXN-POS",
                    "amount": {"currency_code": "USD", "value": "15.50"},
                }
            ]
        },
    }
    client = PayPalClient(client_id="id", client_secret="secret")
    allocs = client.extract_linked_allocations(raw)
    assert len(allocs) == 1
    assert allocs[0].amount_cents == 1550
    assert allocs[0].status == AllocationStatus.APPROVED
    assert allocs[0].currency == "USD"


# --------------------------------------------------------------------------
# Issue 1: Synthetic Allocation Provenance Regressions
# --------------------------------------------------------------------------


def test_partially_paid_invoice_with_empty_transactions_fails_closed():
    """Partially paid invoice with missing transactions raises PayPalAccountingError."""
    raw = {
        "id": "INV-PROV-1",
        "status": "PARTIALLY_PAID",
        "amount": {"currency_code": "USD", "value": "100.00"},
        "due_amount": {"currency_code": "USD", "value": "60.00"},
        "payments": {"transactions": []},
    }
    client = PayPalClient(client_id="id", client_secret="secret")
    with pytest.raises(PayPalAccountingError) as exc_info:
        client.extract_linked_allocations(raw)
    msg = str(exc_info.value).lower()
    assert "provenance" in msg or "transaction history is missing" in msg


def test_partially_paid_invoice_with_real_transactions_works():
    """Partially paid invoice with verified transaction records extracts successfully."""
    raw = {
        "id": "INV-PROV-2",
        "status": "PARTIALLY_PAID",
        "amount": {"currency_code": "USD", "value": "100.00"},
        "due_amount": {"currency_code": "USD", "value": "60.00"},
        "payments": {
            "transactions": [
                {
                    "payment_id": "REAL-TXN-40",
                    "amount": {"currency_code": "USD", "value": "40.00"},
                }
            ]
        },
    }
    client = PayPalClient(client_id="id", client_secret="secret")
    allocs = client.extract_linked_allocations(raw)
    assert len(allocs) == 1
    assert allocs[0].payment_id == "REAL-TXN-40"
    assert allocs[0].amount_cents == 4000
    assert allocs[0].currency == "USD"
    assert allocs[0].status == AllocationStatus.APPROVED


def test_fully_paid_invoice_with_empty_transactions_creates_inferred_settlement():
    """Fully paid invoice (due_amount == 0) with empty transactions creates an inferred settlement."""
    raw = {
        "id": "INV-PROV-3",
        "status": "PAID",
        "amount": {"currency_code": "USD", "value": "100.00"},
        "due_amount": {"currency_code": "USD", "value": "0.00"},
        "payments": {"transactions": []},
    }
    client = PayPalClient(client_id="id", client_secret="secret")
    allocs = client.extract_linked_allocations(raw)
    assert len(allocs) == 1
    assert allocs[0].payment_id == "paypal_inferred_settlement_INV-PROV-3"
    assert allocs[0].amount_cents == 10000
    assert allocs[0].currency == "USD"
    assert allocs[0].status == AllocationStatus.APPROVED
    # Balances perfectly against due_amount_cents == 0
    inv = client.map_invoice(raw)
    assert outstanding_cents(inv, allocs) == 0

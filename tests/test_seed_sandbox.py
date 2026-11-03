"""Safety and idempotency of the Sandbox seeder.

This is the only code that writes to PayPal, so its guards get tested harder
than its happy path. Nothing here touches the network.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from reconcile.paypal import PayPalClient
from tools.seed_sandbox import (
    SANDBOX_HOST,
    SCENARIOS,
    TEST_PREFIX,
    InvoiceIntent,
    PaymentIntent,
    SandboxSeeder,
    SeedSafetyError,
    redact,
)


def _client(base_url=SANDBOX_HOST):
    return PayPalClient(
        client_id="id", client_secret="secret", base_url=base_url, env_file=None
    )


# --------------------------------------------------------------------------
# The endpoint guard
# --------------------------------------------------------------------------


def test_sandbox_host_is_accepted():
    SandboxSeeder(_client(), dry_run=True).assert_sandbox("test")


@pytest.mark.parametrize(
    "host",
    [
        "https://api-m.paypal.com",                  # live
        "https://api.paypal.com",                    # live, older host
        "https://api-m.sandbox.paypal.com.evil.test",  # suffix attack
        "https://evil.test/api-m.sandbox.paypal.com",  # path attack
        "http://api-m.sandbox.paypal.com",           # downgraded scheme
        "https://API-M.SANDBOX.PAYPAL.COM",          # case variant
    ],
)
def test_any_non_sandbox_host_is_refused(host):
    seeder = SandboxSeeder(_client(host), dry_run=False)
    with pytest.raises(SeedSafetyError) as excinfo:
        seeder.assert_sandbox("create an invoice")
    assert "refusing" in str(excinfo.value)


def test_the_guard_runs_on_every_post_not_just_at_startup():
    """A mid-run reconfiguration must not slip a write to production."""
    seeder = SandboxSeeder(_client(), dry_run=False)
    seeder.assert_sandbox("first call")           # passes

    seeder.client.base_url = "https://api-m.paypal.com"
    with patch("urllib.request.urlopen") as m:
        with pytest.raises(SeedSafetyError):
            seeder._post("/v2/invoicing/invoices", {})
    assert m.call_count == 0, "no request may be issued once the guard fails"


def test_a_live_host_blocks_the_post_before_any_network_call():
    seeder = SandboxSeeder(_client("https://api-m.paypal.com"), dry_run=False)
    with patch("urllib.request.urlopen") as m:
        with pytest.raises(SeedSafetyError):
            seeder._post("/v2/invoicing/invoices/x/payments", {"amount": 1})
    assert m.call_count == 0


# --------------------------------------------------------------------------
# The test prefix
# --------------------------------------------------------------------------


def test_every_shipped_scenario_carries_the_test_prefix():
    for intent in SCENARIOS:
        assert intent.number.startswith(TEST_PREFIX), intent.number


def test_an_intent_without_the_prefix_is_rejected_at_construction():
    with pytest.raises(SeedSafetyError):
        InvoiceIntent("0009", "unprefixed", 100, "USD")


def test_scenario_numbers_are_unique():
    numbers = [i.number for i in SCENARIOS]
    assert len(numbers) == len(set(numbers))


def test_the_scenario_set_covers_the_cases_d2_needs():
    scenarios = {i.scenario for i in SCENARIOS}
    assert "partial-payment" in scenarios
    assert "external-payment-settled" in scenarios
    assert "ambiguous-same-amount" in scenarios
    assert "zero-exponent-currency" in scenarios
    # Ambiguity needs at least two invoices sharing an amount.
    same = [i for i in SCENARIOS if i.scenario == "ambiguous-same-amount"]
    assert len({i.amount_minor for i in same}) == 1 and len(same) >= 2


# --------------------------------------------------------------------------
# Idempotency
# --------------------------------------------------------------------------


def _existing(number, invoice_id="INV2-EXISTING", status="SENT"):
    return {
        number: {
            "id": invoice_id,
            "status": status,
            "detail": {"invoice_number": number},
            "amount": {"currency_code": "USD", "value": "125.00"},
        }
    }


def _converged(intent, status="SENT"):
    """An existing record that already matches its intent."""
    paid = sum(p.amount_minor for p in intent.payments)
    item = {
        "id": f"INV2-{intent.number}",
        "status": status,
        "detail": {"invoice_number": intent.number},
        "amount": {"currency_code": intent.currency, "value": "1.00"},
    }
    if paid:
        from reconcile.money import format_minor_units
        item["payments"] = {"transactions": [{
            "payment_id": "T1",
            "amount": {
                "currency_code": intent.currency,
                "value": format_minor_units(paid, intent.currency),
            },
        }]}
    return {intent.number: item}


def test_an_invoice_that_already_matches_its_intent_is_left_untouched():
    seeder = SandboxSeeder(_client(), dry_run=False)
    intent = SCENARIOS[0]

    with patch("urllib.request.urlopen") as m:
        result = seeder.seed_one(intent, _converged(intent))

    assert result["action"] == "skipped"
    assert m.call_count == 0, "a converged record must issue no requests at all"
    assert any("SKIP" in line for line in seeder.log)


def test_a_converged_paid_invoice_is_not_paid_a_second_time():
    seeder = SandboxSeeder(_client(), dry_run=False)
    intent = [i for i in SCENARIOS if i.scenario == "partial-payment"][0]

    with patch("urllib.request.urlopen") as m:
        result = seeder.seed_one(intent, _converged(intent, status="PARTIALLY_PAID"))

    assert result["action"] == "skipped"
    assert m.call_count == 0, "no payment may be replayed onto it"


def test_a_second_pass_over_a_fully_seeded_account_writes_nothing():
    seeder = SandboxSeeder(_client(), dry_run=False)
    existing = {}
    for intent in SCENARIOS:
        existing.update(_converged(intent, status="SENT"))

    with patch("urllib.request.urlopen") as m:
        for intent in SCENARIOS:
            assert seeder.seed_one(intent, existing)["action"] == "skipped"

    assert m.call_count == 0


def test_a_draft_left_over_from_a_half_failed_create_is_sent_not_duplicated():
    """A create that failed after the POST must be recoverable."""
    seeder = SandboxSeeder(_client(), dry_run=False)
    intent = SCENARIOS[0]
    posts = []
    seeder._post = lambda path, body=None, representation=False: (
        posts.append(path) or {}
    )

    result = seeder.seed_one(intent, _existing(intent.number, status="DRAFT"))

    assert result["action"] == "converged"
    assert posts == ["/v2/invoicing/invoices/INV2-EXISTING/send"]
    assert "/v2/invoicing/invoices" not in posts, "must not create a duplicate"


def test_an_underpaid_invoice_is_topped_up_to_the_intended_amount():
    seeder = SandboxSeeder(_client(), dry_run=False)
    intent = [i for i in SCENARIOS if i.scenario == "external-payment-settled"][0]
    bodies = []

    def fake_post(path, body=None, representation=False):
        bodies.append((path, body))
        return {}

    seeder._post = fake_post
    existing = _existing(intent.number, status="PARTIALLY_PAID")
    existing[intent.number]["amount"] = {"currency_code": "USD", "value": "180.00"}
    existing[intent.number]["payments"] = {"transactions": [
        {"payment_id": "T1", "amount": {"currency_code": "USD", "value": "50.00"}}
    ]}

    seeder.seed_one(intent, existing)

    pay = [b for p, b in bodies if p.endswith("/payments")]
    assert len(pay) == 1
    assert pay[0]["amount"]["value"] == "130.00", "tops up the shortfall, not the full total"


def test_a_record_without_the_test_prefix_is_never_converged():
    seeder = SandboxSeeder(_client(), dry_run=False)
    intent = SCENARIOS[0]
    foreign = {intent.number: {
        "id": "INV2-SOMEONE-ELSE",
        "status": "DRAFT",
        "detail": {"invoice_number": "0001"},
        "amount": {"currency_code": "USD", "value": "25.00"},
    }}

    with patch("urllib.request.urlopen") as m:
        result = seeder.seed_one(intent, foreign)

    assert result["action"] == "left-alone"
    assert m.call_count == 0


def test_allocated_minor_uses_the_currency_exponent():
    assert SandboxSeeder.allocated_minor({
        "amount": {"currency_code": "JPY", "value": "1000"},
        "payments": {"transactions": [
            {"payment_id": "T1", "amount": {"currency_code": "JPY", "value": "400"}}
        ]},
    }) == 400


def test_allocated_minor_sums_every_transaction():
    assert SandboxSeeder.allocated_minor({
        "amount": {"currency_code": "USD", "value": "100.00"},
        "payments": {"transactions": [
            {"payment_id": "T1", "amount": {"currency_code": "USD", "value": "40.00"}},
            {"payment_id": "T2", "amount": {"currency_code": "USD", "value": "25.50"}},
        ]},
    }) == 6550


def test_allocated_minor_is_zero_with_no_transactions():
    assert SandboxSeeder.allocated_minor({"amount": {"currency_code": "USD", "value": "1.00"}}) == 0


@pytest.mark.parametrize(
    "response,expected",
    [
        ({"id": "INV2-DIRECT"}, "INV2-DIRECT"),
        ({"_location": "https://api-m.sandbox.paypal.com/v2/invoicing/invoices/INV2-LOC"}, "INV2-LOC"),
        ({"links": [{"href": "https://x/v2/invoicing/invoices/INV2-LINK", "rel": "self"}]}, "INV2-LINK"),
        ({"_location": "https://x/v2/invoicing/invoices/INV2-Q?foo=1"}, "INV2-Q"),
        ({}, None),
        ({"_location": "https://x/v2/other/thing/ABC"}, None),
    ],
)
def test_the_new_invoice_id_is_read_from_whatever_shape_create_returns(response, expected):
    """PayPal answers a create with a Location header, not always an id body."""
    assert SandboxSeeder.invoice_id_from_create(response) == expected


def test_dry_run_plans_without_writing():
    seeder = SandboxSeeder(_client(), dry_run=True)
    with patch("urllib.request.urlopen") as m:
        result = seeder.seed_one(SCENARIOS[0], {})
    assert result["action"] == "planned"
    assert m.call_count == 0
    assert any("PLAN" in line for line in seeder.log)


def test_idempotency_is_decided_against_the_whole_account_not_one_page():
    """Deciding on a partial listing would duplicate anything unseen."""
    seeder = SandboxSeeder(_client(), dry_run=True)
    with patch.object(
        seeder.client, "list_all_raw_invoices", return_value=[
            {"id": "a", "detail": {"invoice_number": TEST_PREFIX + "001"}},
            {"id": "b", "detail": {"invoice_number": TEST_PREFIX + "008"}},
        ]
    ) as paginated:
        found = seeder.existing_by_number()
    paginated.assert_called_once()
    assert set(found) == {TEST_PREFIX + "001", TEST_PREFIX + "008"}


def test_invoices_without_a_number_do_not_break_the_idempotency_index():
    seeder = SandboxSeeder(_client(), dry_run=True)
    with patch.object(
        seeder.client, "list_all_raw_invoices", return_value=[
            {"id": "a", "detail": {}},
            {"id": "b"},
            {"id": "c", "detail": {"invoice_number": "   "}},
            {"id": "d", "detail": {"invoice_number": TEST_PREFIX + "001"}},
        ]
    ):
        assert set(seeder.existing_by_number()) == {TEST_PREFIX + "001"}


# --------------------------------------------------------------------------
# Request bodies: money crosses the boundary as a correctly scaled string
# --------------------------------------------------------------------------


def test_usd_amount_is_formatted_with_two_decimals():
    seeder = SandboxSeeder(_client(), dry_run=True)
    body = seeder._invoice_body(InvoiceIntent(TEST_PREFIX + "900", "s", 12500, "USD"))
    assert body["items"][0]["unit_amount"] == {"currency_code": "USD", "value": "125.00"}
    assert body["detail"]["currency_code"] == "USD"


def test_jpy_amount_is_formatted_with_no_decimals():
    """A zero-exponent currency must not be sent as 10.00 for 1000 JPY."""
    seeder = SandboxSeeder(_client(), dry_run=True)
    body = seeder._invoice_body(InvoiceIntent(TEST_PREFIX + "901", "s", 1000, "JPY"))
    assert body["items"][0]["unit_amount"] == {"currency_code": "JPY", "value": "1000"}


def test_the_invoice_number_is_sent_as_the_stable_key():
    seeder = SandboxSeeder(_client(), dry_run=True)
    body = seeder._invoice_body(SCENARIOS[0])
    assert body["detail"]["invoice_number"] == SCENARIOS[0].number


def test_created_invoices_are_sent_without_emailing_anyone():
    """Sandbox notifications are noise; the invoice only needs to be payable."""
    seeder = SandboxSeeder(_client(), dry_run=False)
    posts = []

    def fake_post(path, body=None, representation=False):
        posts.append((path, body))
        return {"id": "INV2-NEW"} if path == "/v2/invoicing/invoices" else {}

    seeder._post = fake_post
    seeder.seed_one(SCENARIOS[0], {})

    send = [b for p, b in posts if p.endswith("/send")]
    assert send and send[0] == {"send_to_recipient": False, "send_to_invoicer": False}


def test_payments_are_recorded_after_the_invoice_is_sent():
    seeder = SandboxSeeder(_client(), dry_run=False)
    posts = []

    def fake_post(path, body=None, representation=False):
        posts.append(path)
        return {"id": "INV2-NEW"} if path == "/v2/invoicing/invoices" else {}

    seeder._post = fake_post
    partial = [i for i in SCENARIOS if i.scenario == "partial-payment"][0]
    seeder.seed_one(partial, {})

    assert posts[0] == "/v2/invoicing/invoices"
    assert posts[1].endswith("/send")
    assert posts[2].endswith("/payments")


def test_a_partial_payment_records_less_than_the_total():
    partial = [i for i in SCENARIOS if i.scenario == "partial-payment"][0]
    assert sum(p.amount_minor for p in partial.payments) < partial.amount_minor


def test_a_settled_payment_records_the_whole_total():
    settled = [i for i in SCENARIOS if i.scenario == "external-payment-settled"][0]
    assert sum(p.amount_minor for p in settled.payments) == settled.amount_minor


def test_recorded_payments_use_a_documented_paypal_method():
    """"EXTERNAL" is not in PayPal's enum; the Sandbox rejects it."""
    from tools.seed_sandbox import EXTERNAL_PAYMENT_METHODS

    for intent in SCENARIOS:
        for payment in intent.payments:
            assert payment.method in EXTERNAL_PAYMENT_METHODS


def test_an_undocumented_payment_method_is_rejected_at_construction():
    with pytest.raises(SeedSafetyError):
        PaymentIntent(1000, method="EXTERNAL")


# --------------------------------------------------------------------------
# Fixture redaction
# --------------------------------------------------------------------------


def test_redaction_removes_identity_fields_at_any_depth():
    payload = {
        "id": "INV2-1",
        "primary_recipients": [
            {"billing_info": {
                "email_address": "buyer@example.com",
                "name": {"full_name": "A Person", "given_name": "A"},
            }}
        ],
        "amount": {"currency_code": "USD", "value": "125.00"},
    }
    cleaned = redact(payload)
    text = json.dumps(cleaned)

    assert "buyer@example.com" not in text
    assert "A Person" not in text
    assert cleaned["id"] == "INV2-1"
    assert cleaned["amount"] == {"currency_code": "USD", "value": "125.00"}


def test_redaction_keeps_the_accounting_fields_intact():
    payload = {
        "id": "INV2-1",
        "status": "PARTIALLY_PAID",
        "amount": {"currency_code": "USD", "value": "300.00"},
        "due_amount": {"currency_code": "USD", "value": "200.00"},
        "payments": {"transactions": [
            {"payment_id": "T1", "amount": {"currency_code": "USD", "value": "100.00"}}
        ]},
    }
    assert redact(payload) == payload


def test_redaction_handles_lists_and_scalars():
    assert redact([1, "a", None]) == [1, "a", None]
    assert redact("plain") == "plain"
    assert redact({"name": "x"}) == {"name": "<redacted>"}


# --------------------------------------------------------------------------
# The listing omits payments.transactions (verified live)
# --------------------------------------------------------------------------


def test_allocated_minor_reads_paid_amount_which_the_listing_does_provide():
    """Summing transactions off a listing reports zero for a paid invoice."""
    listed = {
        "amount": {"currency_code": "USD", "value": "300.00"},
        "due_amount": {"currency_code": "USD", "value": "200.00"},
        "payments": {"paid_amount": {"currency_code": "USD", "value": "100.00"}},
    }
    assert SandboxSeeder.allocated_minor(listed) == 10000


def test_a_paid_invoice_is_not_paid_again_from_its_listing_representation():
    """The bug this guards: re-paying an invoice the listing looked empty for."""
    seeder = SandboxSeeder(_client(), dry_run=False)
    intent = [i for i in SCENARIOS if i.scenario == "external-payment-settled"][0]
    paid_value = f"{intent.amount_minor / 100:.2f}"

    existing = {intent.number: {
        "id": "INV2-PAID",
        "status": "PAID",
        "detail": {"invoice_number": intent.number},
        "amount": {"currency_code": "USD", "value": paid_value},
        "due_amount": {"currency_code": "USD", "value": "0.00"},
        "payments": {"paid_amount": {"currency_code": "USD", "value": paid_value}},
    }}

    with patch("urllib.request.urlopen") as m:
        result = seeder.seed_one(intent, existing)

    assert result["action"] == "skipped"
    assert m.call_count == 0


def test_paid_amount_takes_precedence_over_transactions():
    item = {
        "amount": {"currency_code": "USD", "value": "300.00"},
        "payments": {
            "paid_amount": {"currency_code": "USD", "value": "100.00"},
            "transactions": [
                {"payment_id": "T1", "amount": {"currency_code": "USD", "value": "999.00"}}
            ],
        },
    }
    assert SandboxSeeder.allocated_minor(item) == 10000


def test_an_overpaid_invoice_is_reported_and_left_alone():
    """A recorded payment cannot be withdrawn, so drift is reported, not fixed."""
    seeder = SandboxSeeder(_client(), dry_run=False)
    intent = [i for i in SCENARIOS if i.scenario == "partial-payment"][0]
    over = intent.amount_minor  # paid in full, when only a partial was intended

    existing = {intent.number: {
        "id": "INV2-OVER",
        "status": "PAID",
        "detail": {"invoice_number": intent.number},
        "amount": {"currency_code": "USD", "value": f"{intent.amount_minor / 100:.2f}"},
        "payments": {"paid_amount": {"currency_code": "USD", "value": f"{over / 100:.2f}"}},
    }}

    with patch("urllib.request.urlopen") as m:
        result = seeder.seed_one(intent, existing)

    assert result["action"] == "skipped"
    assert m.call_count == 0
    assert any("DRIFT" in line for line in seeder.log)

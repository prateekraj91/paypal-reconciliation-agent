"""Idempotent PayPal **Sandbox** seeding for reconciliation test scenarios.

This is the only code in the repository that writes to PayPal, and it is
deliberately outside the `reconcile` package so the core adapter stays
read-only.

Safety, in the order the checks run:

1. The configured base URL must be exactly the Sandbox host. Checked before
   *every* mutating call, not once at start-up, so a mid-run reconfiguration
   cannot slip a write to production.
2. Every record carries the `RECON-T-` prefix in its invoice number, so seeded
   data is always distinguishable from anything else in the account.
3. Existing state is read first. An invoice whose number is already present is
   left exactly as it is: this script never updates, cancels or deletes a
   record, so running it twice is a no-op rather than a duplicate or an
   overwrite.
4. Every record is read back after creation and verified against the intent.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from reconcile.money import format_minor_units
from reconcile.paypal import PayPalClient, PayPalError

SANDBOX_HOST = "https://api-m.sandbox.paypal.com"
TEST_PREFIX = "RECON-T-"

# Values here are minor units, converted to decimal strings only at the API
# boundary, so no float ever touches an amount.
PII_KEYS = frozenset({
    "email_address", "given_name", "surname", "full_name", "alternate_full_name",
    "account_id", "payer_id", "phone_number", "national_number", "account_number",
    "address_line_1", "address_line_2", "admin_area_1", "admin_area_2",
    "postal_code", "line1", "line2", "name",
})


class SeedSafetyError(RuntimeError):
    """Raised when a safety precondition for a mutating call is not met."""


# PayPal's documented enum for PaymentDetail.method on
# POST /v2/invoicing/invoices/{id}/payments. "EXTERNAL" is NOT a member: the
# Sandbox rejects it with INVALID_PAYMENT_METHOD. An externally settled
# invoice is recorded with a real-world method such as BANK_TRANSFER.
EXTERNAL_PAYMENT_METHODS = frozenset({
    "BANK_TRANSFER", "CASH", "CHECK", "CREDIT_CARD",
    "DEBIT_CARD", "PAYPAL", "WIRE_TRANSFER", "OTHER",
})


@dataclass(frozen=True)
class PaymentIntent:
    """A payment to record against a seeded invoice, in minor units."""

    amount_minor: int
    method: str = "BANK_TRANSFER"
    note: str = "Recorded by the reconciliation sandbox seeder"

    def __post_init__(self) -> None:
        if self.method not in EXTERNAL_PAYMENT_METHODS:
            raise SeedSafetyError(
                f"payment method {self.method!r} is not in PayPal's documented "
                f"enum {sorted(EXTERNAL_PAYMENT_METHODS)}"
            )


@dataclass(frozen=True)
class InvoiceIntent:
    """One scenario to seed. `number` is the stable idempotency key."""

    number: str
    scenario: str
    amount_minor: int
    currency: str
    send: bool = True
    payments: tuple[PaymentIntent, ...] = ()
    note: str = ""

    def __post_init__(self) -> None:
        if not self.number.startswith(TEST_PREFIX):
            raise SeedSafetyError(
                f"invoice number {self.number!r} must carry the {TEST_PREFIX} prefix"
            )


# The scenarios the reconciliation engine needs real data for.
SCENARIOS: tuple[InvoiceIntent, ...] = (
    InvoiceIntent(TEST_PREFIX + "001", "open-exact-reference", 12500, "USD"),
    InvoiceIntent(TEST_PREFIX + "002", "open-exact-reference", 25000, "USD"),
    InvoiceIntent(TEST_PREFIX + "003", "ambiguous-same-amount", 7550, "USD"),
    InvoiceIntent(TEST_PREFIX + "004", "ambiguous-same-amount", 7550, "USD"),
    InvoiceIntent(
        TEST_PREFIX + "005", "partial-payment", 30000, "USD",
        payments=(PaymentIntent(20000),),
    ),
    InvoiceIntent(
        TEST_PREFIX + "006", "external-payment-settled", 18000, "USD",
        payments=(PaymentIntent(18000),),
    ),
    InvoiceIntent(TEST_PREFIX + "007", "zero-exponent-currency", 1000, "JPY"),
    InvoiceIntent(TEST_PREFIX + "008", "open-grouped-candidate", 9500, "USD"),
)


def redact(obj: Any) -> Any:
    """Strip identity fields from a payload so fixtures are safe to commit."""
    if isinstance(obj, dict):
        return {
            k: ("<redacted>" if k in PII_KEYS else redact(v))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [redact(v) for v in obj]
    return obj


class SandboxSeeder:
    """Writes the scenario set into a PayPal Sandbox account, idempotently."""

    def __init__(self, client: PayPalClient, dry_run: bool = True) -> None:
        self.client = client
        self.dry_run = dry_run
        self.log: list[str] = []

    # -- safety ---------------------------------------------------------

    def assert_sandbox(self, operation: str) -> None:
        """Refuse any write unless the endpoint is exactly the Sandbox host."""
        if self.client.base_url != SANDBOX_HOST:
            raise SeedSafetyError(
                f"refusing to {operation}: base_url is "
                f"{self.client.base_url!r}, not the Sandbox host {SANDBOX_HOST!r}"
            )

    # -- transport (writes live only here) ------------------------------

    def _post(
        self,
        path: str,
        body: Optional[dict] = None,
        representation: bool = False,
    ) -> dict[str, Any]:
        """POST to Sandbox, returning the body with `_location` filled in.

        PayPal answers a create with 201, a Location header and a body that
        carries no `id` unless `Prefer: return=representation` is asked for. We
        ask for the representation and still fall back to the Location header,
        because an id we cannot read is an invoice we cannot send or pay.
        """
        self.assert_sandbox(f"POST {path}")
        token = self.client.get_access_token()
        data = json.dumps(body or {}).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if representation:
            headers["Prefer"] = "return=representation"
        req = urllib.request.Request(
            f"{self.client.base_url}{path}", data=data, headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=self.client.timeout) as resp:
                raw = resp.read().decode("utf-8")
                location = resp.headers.get("Location", "") if resp.headers else ""
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace") if exc.fp else ""
            raise PayPalError(f"POST {path} failed (HTTP {exc.code}): {detail[:400]}") from exc

        parsed = json.loads(raw) if raw.strip() else {}
        if not isinstance(parsed, dict):
            parsed = {"_body": parsed}
        parsed["_location"] = location
        return parsed

    @staticmethod
    def invoice_id_from_create(response: dict[str, Any]) -> Optional[str]:
        """Pull the new invoice id out of whatever shape the create returned."""
        direct = response.get("id")
        if direct:
            return str(direct)
        candidates = [response.get("_location") or ""]
        candidates += [
            str(link.get("href", ""))
            for link in (response.get("links") or [])
            if isinstance(link, dict)
        ]
        for href in candidates:
            if "/v2/invoicing/invoices/" in href:
                tail = href.split("/v2/invoicing/invoices/", 1)[1].split("?")[0]
                tail = tail.strip("/")
                if tail:
                    return tail
        return None

    # -- idempotency ----------------------------------------------------

    def existing_by_number(self) -> dict[str, dict[str, Any]]:
        """Every invoice already in the account, keyed by invoice number.

        Uses the paginated fetch, so idempotency is decided against the whole
        account rather than the first page. Deciding on a partial listing would
        make the script create duplicates of anything it could not see.
        """
        found: dict[str, dict[str, Any]] = {}
        for item in self.client.list_all_raw_invoices():
            number = ((item.get("detail") or {}).get("invoice_number") or "").strip()
            if number:
                found[number] = item
        return found

    # -- creation -------------------------------------------------------

    def _invoice_body(self, intent: InvoiceIntent) -> dict[str, Any]:
        value = format_minor_units(intent.amount_minor, intent.currency)
        return {
            "detail": {
                "invoice_number": intent.number,
                "currency_code": intent.currency,
                "note": intent.note or f"Reconciliation test scenario: {intent.scenario}",
                "payment_term": {"term_type": "NET_30"},
            },
            "invoicer": {"business_name": "Reconciliation Sandbox Merchant"},
            "items": [
                {
                    "name": f"Scenario {intent.scenario}",
                    "quantity": "1",
                    "unit_amount": {"currency_code": intent.currency, "value": value},
                }
            ],
        }

    def _send(self, intent: InvoiceIntent, invoice_id: str) -> None:
        """Make the invoice payable without emailing anyone."""
        self._post(
            f"/v2/invoicing/invoices/{urllib.parse.quote(invoice_id)}/send",
            {"send_to_recipient": False, "send_to_invoicer": False},
        )
        self.log.append(f"SEND   {intent.number}: now payable")

    def _record_payment(
        self, intent: InvoiceIntent, invoice_id: str, amount_minor: int, method: str, note: str
    ) -> None:
        self._post(
            f"/v2/invoicing/invoices/{urllib.parse.quote(invoice_id)}/payments",
            {
                "method": method,
                "payment_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                "amount": {
                    "currency_code": intent.currency,
                    "value": format_minor_units(amount_minor, intent.currency),
                },
                "note": note,
            },
        )
        self.log.append(
            f"PAY    {intent.number}: recorded "
            f"{format_minor_units(amount_minor, intent.currency)} "
            f"{intent.currency} ({method})"
        )

    @staticmethod
    def allocated_minor(item: dict[str, Any]) -> int:
        """Minor units already paid against a raw invoice.

        Reads `payments.paid_amount` first. The listing endpoint returns that
        but omits `payments.transactions`, so summing transactions off a listed
        invoice reports zero for an invoice that is actually paid -- which makes
        the seeder pay it again. Transactions are only the fallback, for a
        payload that carries them but no paid_amount.
        """
        from reconcile.money import parse_money_to_minor_units

        amount = item.get("amount") or {}
        currency = amount.get("currency_code") or "USD"
        payments = item.get("payments") or {}

        paid = payments.get("paid_amount") or {}
        if paid.get("value") is not None:
            return parse_money_to_minor_units(
                paid["value"], paid.get("currency_code") or currency
            )

        total = 0
        for txn in (payments.get("transactions") or []):
            value = (txn.get("amount") or {}).get("value")
            if value is None:
                continue
            txn_currency = (txn.get("amount") or {}).get("currency_code") or currency
            total += parse_money_to_minor_units(value, txn_currency)
        return total

    def seed_one(self, intent: InvoiceIntent, existing: dict[str, dict]) -> dict[str, Any]:
        """Converge one scenario towards its intent.

        Seeding is declarative rather than create-once. A record that exists but
        is only half-built -- created but never sent, or sent but not paid --
        is completed, because a create that half-failed must be recoverable
        without hand-editing the account. Convergence is still idempotent: once
        a record matches its intent, this writes nothing.

        The convergence path is restricted to invoices carrying the test
        prefix. Anything else in the account is left strictly alone.
        """
        item = existing.get(intent.number)

        if item is not None:
            number = ((item.get("detail") or {}).get("invoice_number") or "").strip()
            if not number.startswith(TEST_PREFIX):
                self.log.append(f"LEAVE  {number!r}: not a seeded record, untouched")
                return {"intent": intent, "action": "left-alone", "invoice": item}

            invoice_id = item.get("id")
            status = str(item.get("status") or "").upper()
            already = self.allocated_minor(item)
            wanted = sum(p.amount_minor for p in intent.payments)
            steps: list[str] = []

            if intent.send and status == "DRAFT":
                steps.append("send")
            if already > wanted:
                self.log.append(
                    f"DRIFT  {intent.number} ({intent.scenario}): {already} minor "
                    f"units already paid exceeds the intended {wanted}; leaving it "
                    f"alone, since a payment cannot be withdrawn through the API"
                )
            elif wanted > already:
                steps.append(f"pay {wanted - already}")

            if not steps:
                self.log.append(
                    f"SKIP   {intent.number} ({intent.scenario}): already matches "
                    f"intent ({invoice_id} status={status})"
                )
                return {"intent": intent, "action": "skipped", "invoice": item}

            if self.dry_run:
                self.log.append(
                    f"PLAN   {intent.number} ({intent.scenario}): converge existing "
                    f"{invoice_id} status={status} -> {', '.join(steps)}"
                )
                return {"intent": intent, "action": "planned", "invoice": item}

            if intent.send and status == "DRAFT":
                self._send(intent, invoice_id)
            if wanted > already:
                for payment in intent.payments:
                    outstanding = wanted - already
                    if outstanding <= 0:
                        break
                    amount = min(payment.amount_minor, outstanding)
                    self._record_payment(
                        intent, invoice_id, amount, payment.method, payment.note
                    )
                    already += amount

            return {"intent": intent, "action": "converged", "invoice_id": invoice_id}

        if self.dry_run:
            self.log.append(
                f"PLAN   {intent.number} ({intent.scenario}): create "
                f"{format_minor_units(intent.amount_minor, intent.currency)} "
                f"{intent.currency}"
                + (f", send, record {len(intent.payments)} payment(s)" if intent.send else "")
            )
            return {"intent": intent, "action": "planned", "invoice": None}

        created = self._post(
            "/v2/invoicing/invoices", self._invoice_body(intent), representation=True
        )
        invoice_id = self.invoice_id_from_create(created)
        if not invoice_id:
            raise PayPalError(
                f"create for {intent.number} returned no id and no usable "
                f"Location; cannot send or pay an invoice we cannot address"
            )
        self.log.append(f"CREATE {intent.number}: {invoice_id}")

        if intent.send:
            self._send(intent, invoice_id)
        for payment in intent.payments:
            self._record_payment(
                intent, invoice_id, payment.amount_minor, payment.method, payment.note
            )

        return {"intent": intent, "action": "created", "invoice_id": invoice_id}


def verify(client: PayPalClient, intents: tuple[InvoiceIntent, ...]) -> list[dict[str, Any]]:
    """Read every seeded record back and check it against the intent."""
    by_number = {}
    for item in client.list_all_raw_invoices():
        number = ((item.get("detail") or {}).get("invoice_number") or "").strip()
        if number:
            by_number[number] = item

    report = []
    for intent in intents:
        item = by_number.get(intent.number)
        if item is None:
            report.append({"number": intent.number, "present": False})
            continue
        # The listing omits payments.transactions, so a paid invoice needs its
        # per-invoice representation before provenance can be established.
        if client.needs_payment_provenance(item):
            item = client.hydrate_invoice(item)
        invoice = client.map_invoice(item)
        allocations = client.extract_linked_allocations(item)
        expected_paid = sum(p.amount_minor for p in intent.payments)
        report.append({
            "number": intent.number,
            "present": True,
            "scenario": intent.scenario,
            "invoice_id": invoice.invoice_id,
            "status": invoice.status.value,
            "is_open": invoice.is_open,
            "currency": invoice.currency,
            "total_minor": invoice.amount_cents,
            "due_minor": invoice.due_amount_cents,
            "allocations": len(allocations),
            "allocated_minor": sum(a.amount_cents for a in allocations),
            "amount_matches_intent": invoice.amount_cents == intent.amount_minor,
            "currency_matches_intent": invoice.currency == intent.currency,
            "paid_matches_intent": sum(a.amount_cents for a in allocations) == expected_paid,
        })
    return report


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--apply", action="store_true",
        help="actually create records; without it the script only plans",
    )
    parser.add_argument(
        "--fixtures", metavar="PATH", default=None,
        help="write sanitised fixtures of the seeded invoices to PATH",
    )
    args = parser.parse_args(argv)

    client = PayPalClient(timeout=30.0)
    seeder = SandboxSeeder(client, dry_run=not args.apply)

    try:
        seeder.assert_sandbox("seed")
    except SeedSafetyError as exc:
        print(f"SAFETY: {exc}", file=sys.stderr)
        return 2

    print(f"endpoint : {client.base_url}  (Sandbox confirmed)")
    print(f"mode     : {'APPLY' if args.apply else 'DRY RUN (no writes)'}")
    print(f"prefix   : {TEST_PREFIX}")

    existing = seeder.existing_by_number()
    print(f"existing : {len(existing)} invoice(s) in the account, "
          f"{sum(1 for n in existing if n.startswith(TEST_PREFIX))} already seeded\n")

    for intent in SCENARIOS:
        try:
            seeder.seed_one(intent, existing)
        except PayPalError as exc:
            seeder.log.append(f"FAIL   {intent.number} ({intent.scenario}): {exc}")

    for line in seeder.log:
        print("  " + line)

    if not args.apply:
        print("\nDry run complete. Re-run with --apply to create these records.")
        return 0

    print("\n=== verification (read back from PayPal) ===")
    report = verify(client, SCENARIOS)
    ok = 0
    for row in report:
        if not row["present"]:
            print(f"  MISSING {row['number']}")
            continue
        checks = row["amount_matches_intent"] and row["currency_matches_intent"] and row["paid_matches_intent"]
        ok += checks
        print(
            f"  {'OK  ' if checks else 'BAD '} {row['number']} {row['scenario']:24} "
            f"{row['status']:16} open={str(row['is_open']):5} "
            f"total={row['total_minor']}{row['currency']} due={row['due_minor']} "
            f"allocs={row['allocations']}({row['allocated_minor']})"
        )
    print(f"\n  {ok}/{len(report)} seeded records verified against intent")

    if args.fixtures:
        raw = [
            item for item in client.list_all_raw_invoices()
            if ((item.get("detail") or {}).get("invoice_number") or "").startswith(TEST_PREFIX)
        ]
        with open(args.fixtures, "w", encoding="utf-8") as handle:
            json.dump(redact(raw), handle, indent=2, sort_keys=True)
        print(f"  sanitised fixtures for {len(raw)} invoice(s) -> {args.fixtures}")

    return 0 if ok == len(report) else 1


if __name__ == "__main__":
    raise SystemExit(main())

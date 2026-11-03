"""Reusable PayPal client and invoice adapter for PayPal Invoicing API v2.

Standard library only. All HTTP communication uses urllib.request.
Tokens and credentials are never logged or exposed.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import date
import json
import os
import sys
import time
from typing import Any, Optional, Sequence
import urllib.error
import urllib.parse
import urllib.request

from .interfaces import InvoiceSource
from .money import (
    UnsupportedCurrencyError,
    normalize_currency,
    parse_money_to_minor_units,
)
from .models import (
    AccountingError,
    AllocationStatus,
    Invoice,
    InvoiceStatus,
    PaymentAllocation,
    deduplicate_allocations,
)


class PayPalError(Exception):
    """Base exception for PayPal client operations."""


class PayPalConfigError(PayPalError):
    """Raised when required credentials or configuration are missing."""


class PayPalAuthError(PayPalError):
    """Raised when authentication fails (HTTP 401 or invalid token response)."""


class PayPalNotFoundError(PayPalError):
    """Raised when a requested resource is not found (HTTP 404)."""


class PayPalAPIError(PayPalError):
    """Raised when PayPal returns an HTTP error response."""

    def __init__(self, status_code: int, message: str, details: Optional[dict] = None) -> None:
        super().__init__(f"PayPal API error (HTTP {status_code}): {message}")
        self.status_code = status_code
        self.details = details or {}


class PayPalTimeoutError(PayPalError):
    """Raised when a request to PayPal times out."""


class PayPalAccountingError(PayPalError, AccountingError):
    """Raised when PayPal due_amount contradicts transaction records or accounting invariants."""


def _parse_money_to_cents(value: Any, currency: str) -> int:
    """Parse a PayPal monetary string into integer minor units of `currency`.

    The currency is required because the scaling factor depends on it: 1000 JPY
    is 1000 minor units, not 100000. See money.MINOR_UNIT_EXPONENTS.
    """
    return parse_money_to_minor_units(value, currency)


def _load_dotenv(filepath: str = ".env") -> dict[str, str]:
    """Parse a simple key=value .env file without external dependencies."""
    env: dict[str, str] = {}
    if not os.path.isfile(filepath):
        return env
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                env[key.strip()] = val.strip().strip("\"'")
    except OSError:
        pass
    return env


def _invoice_currency(item: dict[str, Any]) -> str:
    """The currency of a raw PayPal invoice, failing closed when absent.

    PayPal reports it on `amount.currency_code`, with `detail.currency_code` as
    the older location. Neither present is a fail-closed condition: defaulting
    to USD would let a foreign invoice be scaled and matched as dollars.
    """
    amount = item.get("amount") or {}
    detail = item.get("detail") or {}
    raw = amount.get("currency_code") or detail.get("currency_code")
    if not raw or not str(raw).strip():
        raise PayPalAccountingError(
            f"Invoice {item.get('id')!r} has no currency_code: "
            f"a currency-less invoice cannot be converted to minor units"
        )
    try:
        return normalize_currency(raw)
    except UnsupportedCurrencyError as exc:
        raise PayPalAccountingError(
            f"Invoice {item.get('id')!r}: {exc}"
        ) from exc
    except ValueError as exc:
        raise PayPalAccountingError(
            f"Invoice {item.get('id')!r} has an invalid currency_code {raw!r}: {exc}"
        ) from exc


_STATUS_MAP: dict[str, InvoiceStatus] = {
    "DRAFT": InvoiceStatus.DRAFT,
    "SENT": InvoiceStatus.SENT,
    "SCHEDULED": InvoiceStatus.SENT,
    "PAYMENT_PENDING": InvoiceStatus.SENT,
    "PARTIALLY_PAID": InvoiceStatus.PARTIALLY_PAID,
    "PARTIALLY_REFUNDED": InvoiceStatus.PARTIALLY_PAID,
    "PAID": InvoiceStatus.PAID,
    "MARKED_AS_PAID": InvoiceStatus.PAID,
    "CANCELLED": InvoiceStatus.CANCELLED,
    "REFUNDED": InvoiceStatus.CANCELLED,
}


class PayPalClient:
    """Reusable client for PayPal Invoicing API v2.

    Implements InvoiceSource protocol. Handles OAuth2 token acquisition,
    caching until shortly before expiry, and mapping into internal Invoice models.
    """

    def __init__(
        self,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout: float = 10.0,
        env_file: Optional[str] = ".env",
        token_buffer_seconds: float = 60.0,
    ) -> None:
        env_dict = _load_dotenv(env_file) if env_file else {}

        self.client_id = (
            client_id
            or os.environ.get("PAYPAL_CLIENT_ID")
            or env_dict.get("PAYPAL_CLIENT_ID")
            or ""
        )
        self._client_secret = (
            client_secret
            or os.environ.get("PAYPAL_CLIENT_SECRET")
            or env_dict.get("PAYPAL_CLIENT_SECRET")
            or ""
        )
        raw_base = (
            base_url
            or os.environ.get("PAYPAL_BASE_URL")
            or env_dict.get("PAYPAL_BASE_URL")
            or "https://api-m.sandbox.paypal.com"
        )
        self.base_url = raw_base.rstrip("/")
        self.timeout = timeout
        self.token_buffer_seconds = token_buffer_seconds

        if not self.client_id or not self._client_secret:
            raise PayPalConfigError(
                "Missing PayPal credentials: both PAYPAL_CLIENT_ID and "
                "PAYPAL_CLIENT_SECRET must be configured."
            )

        self._access_token: Optional[str] = None
        self._token_expires_at: float = 0.0

    def __repr__(self) -> str:
        masked_id = f"{self.client_id[:4]}***" if len(self.client_id) >= 4 else "***"
        return f"PayPalClient(base_url={self.base_url!r}, client_id={masked_id!r})"

    # ----------------------------------------------------------------------
    # Authentication & Token Caching
    # ----------------------------------------------------------------------

    def get_access_token(self) -> str:
        """Return a valid OAuth2 bearer token, using cached token if still valid."""
        now = time.monotonic()
        if (
            self._access_token
            and self._token_expires_at
            and (now < self._token_expires_at - self.token_buffer_seconds)
        ):
            return self._access_token

        token_url = f"{self.base_url}/v1/oauth2/token"
        credentials = f"{self.client_id}:{self._client_secret}"
        encoded_creds = base64.b64encode(credentials.encode("utf-8")).decode("ascii")

        req = urllib.request.Request(
            token_url,
            data=b"grant_type=client_credentials",
            headers={
                "Authorization": f"Basic {encoded_creds}",
                "Accept": "application/json",
                "Accept-Language": "en_US",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                body = response.read().decode("utf-8")
                data = json.loads(body)
        except urllib.error.HTTPError as exc:
            err_body = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
            if exc.code == 401:
                raise PayPalAuthError(
                    f"Authentication failed (HTTP 401): {err_body}"
                ) from exc
            raise PayPalAPIError(exc.code, err_body) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            if isinstance(getattr(exc, "reason", None), TimeoutError) or isinstance(exc, TimeoutError):
                raise PayPalTimeoutError(f"OAuth token request timed out: {exc}") from exc
            raise PayPalAPIError(0, f"Network error during authentication: {exc}") from exc

        access_token = data.get("access_token")
        if not access_token:
            raise PayPalAuthError("Token response missing 'access_token'")

        expires_in = float(data.get("expires_in", 32400))
        self._access_token = access_token
        self._token_expires_at = time.monotonic() + expires_in
        return self._access_token

    # ----------------------------------------------------------------------
    # HTTP Transport
    # ----------------------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        query: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        token = self.get_access_token()
        url = f"{self.base_url}{path}"
        if query:
            clean_query = {k: v for k, v in query.items() if v is not None}
            url = f"{url}?{urllib.parse.urlencode(clean_query)}"

        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            method=method,
        )

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                body = response.read().decode("utf-8")
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as exc:
            err_body = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
            if exc.code == 401:
                self._access_token = None  # Invalidate cached token on 401
                raise PayPalAuthError(f"Unauthorized (HTTP 401): {err_body}") from exc
            if exc.code == 404:
                raise PayPalNotFoundError(f"Resource not found (HTTP 404): {err_body}") from exc
            raise PayPalAPIError(exc.code, err_body) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            if isinstance(getattr(exc, "reason", None), TimeoutError) or isinstance(exc, TimeoutError):
                raise PayPalTimeoutError(f"Request to {path} timed out: {exc}") from exc
            raise PayPalAPIError(0, f"Network error on request to {path}: {exc}") from exc

    # ----------------------------------------------------------------------
    # Invoicing API v2
    # ----------------------------------------------------------------------

    def list_raw_invoices(
        self,
        page: int = 1,
        page_size: int = 20,
        total_required: bool = False,
    ) -> dict[str, Any]:
        """Fetch raw JSON invoice listing from PayPal Invoicing API v2."""
        return self._request(
            "GET",
            "/v2/invoicing/invoices",
            query={
                "page": page,
                "page_size": page_size,
                "total_required": str(total_required).lower(),
            },
        )

    def list_invoices(
        self,
        page: int = 1,
        page_size: int = 20,
        total_required: bool = False,
    ) -> Sequence[Invoice]:
        """List and map invoices from PayPal Invoicing API v2 into Invoice models."""
        data = self.list_raw_invoices(page=page, page_size=page_size, total_required=total_required)
        items = data.get("items") or []
        return [self.map_invoice(item) for item in items]

    def list_invoices_with_allocations(
        self,
        page: int = 1,
        page_size: int = 20,
        total_required: bool = False,
    ) -> tuple[list[Invoice], list[PaymentAllocation]]:
        """List and map invoices from PayPal along with their linked payment allocations.

        Returns a tuple of (invoices, approved_allocations).
        Reuses map_invoice and extract_linked_allocations.
        Deduplicates allocations by (payment_id, invoice_id) compound key.
        """
        data = self.list_raw_invoices(page=page, page_size=page_size, total_required=total_required)
        items = data.get("items") or []
        invoices: list[Invoice] = []
        raw_allocations: list[PaymentAllocation] = []

        for item in items:
            invoice = self.map_invoice(item)
            invoices.append(invoice)
            item_allocs = self.extract_linked_allocations(item)
            raw_allocations.extend(item_allocs)

        deduped_allocs = deduplicate_allocations(
            raw_allocations, error_cls=PayPalAccountingError
        )
        return invoices, deduped_allocs

    def get_raw_invoice(self, invoice_id: str) -> dict[str, Any]:
        """Fetch raw JSON representation of an individual invoice."""
        if not invoice_id:
            raise ValueError("invoice_id is required")
        return self._request("GET", f"/v2/invoicing/invoices/{urllib.parse.quote(invoice_id)}")

    def get_invoice(self, invoice_id: str) -> Invoice:
        """Fetch an individual invoice and map it into an Invoice model."""
        raw = self.get_raw_invoice(invoice_id)
        return self.map_invoice(raw)

    # ----------------------------------------------------------------------
    # Domain Mapping & Ledger Reconciliation Support
    # ----------------------------------------------------------------------

    def map_invoice(self, item: dict[str, Any]) -> Invoice:
        """Map PayPal Invoicing API v2 item structure into internal Invoice model."""
        invoice_id = item.get("id")
        if not invoice_id:
            raise ValueError("PayPal invoice missing 'id'")

        detail = item.get("detail") or {}
        number = detail.get("invoice_number") or invoice_id

        # Recipient / customer information
        primary_recipients = item.get("primary_recipients") or []
        customer_name = "Unknown Customer"
        customer_email: Optional[str] = None

        if primary_recipients:
            billing = primary_recipients[0].get("billing_info") or {}
            customer_email = billing.get("email_address")
            name_info = billing.get("name") or {}
            full_name = name_info.get("full_name")
            if not full_name:
                given = name_info.get("given_name", "").strip()
                surname = name_info.get("surname", "").strip()
                full_name = f"{given} {surname}".strip()
            if full_name:
                customer_name = full_name
            elif customer_email:
                customer_name = customer_email

        # Monetary fields
        amount_dict = item.get("amount") or {}
        currency = _invoice_currency(item)
        amount_cents = _parse_money_to_cents(amount_dict.get("value", "0.00"), currency)

        # Outstanding due amount
        due_amount_cents: Optional[int] = None
        due_dict = item.get("due_amount")
        if due_dict and "value" in due_dict:
            due_amount_cents = _parse_money_to_cents(due_dict["value"], currency)

        # Dates
        invoice_date_str = detail.get("invoice_date")
        issued_on = (
            date.fromisoformat(invoice_date_str)
            if invoice_date_str
            else date.today()
        )

        payment_term = detail.get("payment_term") or {}
        due_date_str = payment_term.get("due_date") or invoice_date_str
        due_on = (
            date.fromisoformat(due_date_str)
            if due_date_str
            else issued_on
        )

        # Status mapping
        raw_status = (item.get("status") or "SENT").upper()
        status = _STATUS_MAP.get(raw_status, InvoiceStatus.SENT)

        memo = detail.get("memo") or ""

        return Invoice(
            invoice_id=invoice_id,
            number=number,
            customer_name=customer_name,
            amount_cents=amount_cents,
            currency=currency,
            issued_on=issued_on,
            due_on=due_on,
            status=status,
            customer_email=customer_email,
            memo=memo,
            due_amount_cents=due_amount_cents,
        )

    def extract_linked_allocations(self, item: dict[str, Any]) -> tuple[PaymentAllocation, ...]:
        """Extract existing payments recorded on PayPal to avoid double-counting.

        Returns PaymentAllocation items with status=APPROVED so they reduce
        outstanding balance in the reconciliation ledger.
        """
        invoice_id = item.get("id")
        if not invoice_id:
            return ()

        amount_dict = item.get("amount") or {}
        inv_curr = _invoice_currency(item)

        # Reject invoices with recorded refunds or adjustments
        refunds_data = item.get("refunds") or {}
        refund_txns = refunds_data.get("transactions") or []
        refund_amt_dict = refunds_data.get("refund_amount") or {}
        has_refund_amount = False
        if "value" in refund_amt_dict:
            try:
                has_refund_amount = _parse_money_to_cents(refund_amt_dict["value"], inv_curr) > 0
            except ValueError:
                pass
        if refund_txns or has_refund_amount:
            raise PayPalAccountingError(
                f"Invoice {invoice_id} has refunds recorded: "
                f"refunds and reversals cannot be processed as payment allocations"
            )

        raw_allocs: list[PaymentAllocation] = []
        payments_data = item.get("payments") or {}
        transactions = payments_data.get("transactions") or []

        for idx, txn in enumerate(transactions, start=1):
            raw_txn_id = txn.get("payment_id")
            txn_id = raw_txn_id or f"paypal_txn_{invoice_id}_{idx}"

            # Check transaction type
            txn_type = str(txn.get("type", "")).strip().upper()
            if txn_type in ("REFUND", "ADJUSTMENT"):
                raise PayPalAccountingError(
                    f"Transaction {txn_id} on invoice {invoice_id} has unsupported type '{txn.get('type')}': "
                    f"refunds and adjustments cannot be processed as payment allocations"
                )

            # Check transaction status if provided
            txn_status = str(txn.get("status", "")).strip().upper()
            if txn_status and txn_status not in ("COMPLETED", "APPROVED", "SUCCESS"):
                raise PayPalAccountingError(
                    f"Transaction {txn_id} on invoice {invoice_id} has unapproved status '{txn.get('status')}': "
                    f"only completed or approved transactions can reduce balance"
                )

            # Validate transaction currency
            amt_info = txn.get("amount") or {}
            txn_curr = amt_info.get("currency_code")
            if not txn_curr or not str(txn_curr).strip():
                raise PayPalAccountingError(
                    f"Transaction {txn_id} on invoice {invoice_id} is missing currency_code"
                )
            txn_curr_norm = str(txn_curr).strip().upper()
            if txn_curr_norm != inv_curr:
                raise PayPalAccountingError(
                    f"Transaction {txn_id} currency '{txn_curr}' does not match "
                    f"invoice currency '{inv_curr}'"
                )

            # Validate transaction amount
            val = amt_info.get("value")
            if val is None or str(val).strip() == "":
                raise PayPalAccountingError(
                    f"Transaction {txn_id} on invoice {invoice_id} is missing amount value"
                )
            try:
                cents = _parse_money_to_cents(val, txn_curr_norm)
            except ValueError as exc:
                raise PayPalAccountingError(
                    f"Transaction {txn_id} on invoice {invoice_id} has invalid monetary value {val!r}: {exc}"
                ) from exc

            if cents == 0:
                raise PayPalAccountingError(
                    f"Transaction {txn_id} on invoice {invoice_id} has invalid zero amount (0 cents)"
                )
            if cents < 0:
                raise PayPalAccountingError(
                    f"Transaction {txn_id} on invoice {invoice_id} has negative amount ({cents} cents): "
                    f"negative amounts and refunds cannot be processed as positive payment allocations"
                )

            raw_allocs.append(
                PaymentAllocation(
                    payment_id=txn_id,
                    invoice_id=invoice_id,
                    amount_cents=cents,
                    status=AllocationStatus.APPROVED,
                    currency=inv_curr,
                )
            )

        allocations: list[PaymentAllocation] = deduplicate_allocations(
            raw_allocs, error_cls=PayPalAccountingError
        )

        # Fallback handling for missing transaction history
        if not allocations and not transactions:
            due_dict = item.get("due_amount") or {}
            if "value" in amount_dict and "value" in due_dict:
                if "currency_code" in due_dict:
                    due_curr = str(due_dict["currency_code"]).strip().upper()
                    if due_curr != inv_curr:
                        raise PayPalAccountingError(
                            f"PayPal due_amount currency '{due_dict['currency_code']}' does not match "
                            f"invoice currency '{inv_curr}'"
                        )
                total_cents = _parse_money_to_cents(amount_dict["value"], inv_curr)
                due_cents = _parse_money_to_cents(due_dict["value"], inv_curr)
                paid_diff = total_cents - due_cents
                if paid_diff > 0:
                    if due_cents > 0:
                        raise PayPalAccountingError(
                            f"Invoice {invoice_id} has due_amount ({due_cents} cents) < total ({total_cents} cents) "
                            f"with {paid_diff} cents paid, but transaction history is missing: "
                            f"partial payment provenance cannot be verified"
                        )
                    # Fully paid invoice with due_amount == 0 and missing transaction history.
                    # An inferred settlement allocation balances the internal ledger without leaving
                    # partial capacity open for duplicate matching.
                    allocations.append(
                        PaymentAllocation(
                            payment_id=f"paypal_inferred_settlement_{invoice_id}",
                            invoice_id=invoice_id,
                            amount_cents=paid_diff,
                            status=AllocationStatus.APPROVED,
                            currency=inv_curr,
                        )
                    )

        # Currency cross-check for payments.paid_amount if present
        paid_amount_dict = payments_data.get("paid_amount") or {}
        if "value" in paid_amount_dict and "currency_code" in paid_amount_dict:
            paid_curr = str(paid_amount_dict["currency_code"]).strip().upper()
            if paid_curr != inv_curr:
                raise PayPalAccountingError(
                    f"PayPal paid_amount currency '{paid_amount_dict['currency_code']}' does not match "
                    f"invoice currency '{inv_curr}'"
                )

        # Cross-check: verify that PayPal due_amount agrees with (total - sum_linked)
        due_dict = item.get("due_amount") or {}
        if "value" in amount_dict and "value" in due_dict:
            if "currency_code" in due_dict:
                due_curr = str(due_dict["currency_code"]).strip().upper()
                if due_curr != inv_curr:
                    raise PayPalAccountingError(
                        f"PayPal due_amount currency '{due_dict['currency_code']}' does not match "
                        f"invoice currency '{inv_curr}'"
                    )
            total_cents = _parse_money_to_cents(amount_dict["value"], inv_curr)
            due_cents = _parse_money_to_cents(due_dict["value"], inv_curr)
            sum_allocs = sum(a.amount_cents for a in allocations)
            expected_due = total_cents - sum_allocs
            if due_cents != expected_due:
                raise PayPalAccountingError(
                    f"PayPal due_amount ({due_cents} cents) contradicts transaction "
                    f"records: expected {expected_due} cents (total {total_cents} - "
                    f"transactions {sum_allocs})"
                )

        return tuple(allocations)


# --------------------------------------------------------------------------
# CLI entrypoint for manual sandbox verification
# --------------------------------------------------------------------------


def main() -> None:
    """Manual Sandbox CLI command to retrieve and inspect PayPal invoices."""
    import argparse

    parser = argparse.ArgumentParser(
        description="PayPal Sandbox invoice inspection tool"
    )
    parser.add_argument(
        "invoice",
        nargs="?",
        default="0001",
        help="Invoice number (e.g. 0001) or PayPal Invoice ID (default: 0001)",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List all invoices in Sandbox account",
    )
    args = parser.parse_args()

    try:
        client = PayPalClient()
    except PayPalConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"Connecting to PayPal Sandbox ({client.base_url})...")

    if args.list:
        try:
            invoices, allocations = client.list_invoices_with_allocations()
            print(f"Found {len(invoices)} invoices ({len(allocations)} approved linked allocation(s)):\n")
            for inv in invoices:
                due_str = f"${inv.due_amount_cents / 100:.2f}" if inv.due_amount_cents is not None else "N/A"
                print(
                    f"  {inv.number:<10} ID: {inv.invoice_id:<32} "
                    f"Total: ${inv.amount_cents / 100:>6.2f} {inv.currency}  "
                    f"Due: {due_str:>7}  Status: {inv.status.value:<14}  Open: {inv.is_open}"
                )
            return
        except PayPalError as e:
            print(f"Failed to list invoices: {e}", file=sys.stderr)
            sys.exit(1)

    # Individual invoice retrieval
    target = args.invoice.strip()
    target_id: Optional[str] = None
    target_invoice: Optional[Invoice] = None

    if target.startswith("INV2-"):
        target_id = target
    else:
        # Resolve human-facing invoice number (e.g. 0001) by listing
        try:
            invoices = client.list_invoices()
            for inv in invoices:
                if inv.number == target or inv.number == target.lstrip("0") or inv.invoice_id == target:
                    target_invoice = inv
                    target_id = inv.invoice_id
                    break
            if not target_id:
                print(f"Invoice with number {target!r} not found in listing.", file=sys.stderr)
                if invoices:
                    print(f"Available invoice numbers: {', '.join(i.number for i in invoices)}", file=sys.stderr)
                sys.exit(1)
        except PayPalError as e:
            print(f"Failed while searching for invoice {target}: {e}", file=sys.stderr)
            sys.exit(1)

    try:
        if target_invoice is None:
            target_invoice = client.get_invoice(target_id)
        raw_inv = client.get_raw_invoice(target_id)
        linked_allocs = client.extract_linked_allocations(raw_inv)

        print("\n=== PayPal Invoice Details ===")
        print(f"Invoice ID:       {target_invoice.invoice_id}")
        print(f"Invoice Number:   {target_invoice.number}")
        print(f"Customer Name:    {target_invoice.customer_name}")
        print(f"Customer Email:   {target_invoice.customer_email or 'None'}")
        print(f"Total Amount:     ${target_invoice.amount_cents / 100:.2f} {target_invoice.currency}")
        due_str = f"${target_invoice.due_amount_cents / 100:.2f}" if target_invoice.due_amount_cents is not None else "N/A"
        print(f"Due Amount:       {due_str} {target_invoice.currency}")
        print(f"Status:           {target_invoice.status.value}")
        print(f"Matchable (Open): {target_invoice.is_open} ({'CANNOT be auto-matched' if not target_invoice.is_open else 'Open for matching'})")
        print(f"Issued On:        {target_invoice.issued_on}")
        print(f"Due On:           {target_invoice.due_on}")
        print(f"Memo:             {target_invoice.memo or 'None'}")
        print(f"Linked Payments:  {len(linked_allocs)} transaction(s) recorded on PayPal")
        for alloc in linked_allocs:
            print(f"  - Payment {alloc.payment_id}: ${alloc.amount_cents / 100:.2f} (Status: {alloc.status.value})")
        print("==============================\n")
    except PayPalError as e:
        print(f"Failed to retrieve invoice {target_id}: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

"""Reusable PayPal client and invoice adapter for PayPal Invoicing API v2.

Standard library only. All HTTP communication uses urllib.request.
Tokens and credentials are never logged or exposed.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
import json
import os
import sys
import time
from typing import Any, Iterator, Mapping, Optional, Sequence
import urllib.error
import urllib.parse
import urllib.request

from .delivery import PaymentConflictError, resolve_delivery
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
    Payment,
    PaymentAllocation,
    PaymentStatus,
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


class PayPalPaginationError(PayPalError):
    """Raised when a paginated fetch cannot be shown to be complete.

    Returning a prefix of the invoice list is worse than returning nothing: a
    payment against an invoice that was never listed is reported UNMATCHED,
    which reads as a clean answer rather than a missing one.
    """


# Documented limits and behaviour for GET /v1/reporting/transactions.
MAX_TRANSACTION_WINDOW_DAYS = 31
# PayPal states that a transaction may take up to three hours to appear in
# Transaction Search. Reading right up to "now" therefore returns a window that
# is still filling, and a sync that trusted it would skip those transactions
# permanently. Every sync end is pulled back by this much.
TRANSACTION_APPEARANCE_DELAY_HOURS = 3
DEFAULT_TRANSACTION_OVERLAP = timedelta(hours=6)
MAX_TRANSACTION_PAGE_SIZE = 500
DEFAULT_TRANSACTION_PAGE_SIZE = 500
DEFAULT_TRANSACTION_RETRIES = 3
MAX_BACKOFF_SECONDS = 30.0

# PayPal's single-letter transaction status codes. An unlisted code is a
# fail-closed condition: guessing COMPLETED would let an unknown state be
# matched and allocated.
TRANSACTION_STATUS_MAP: Mapping[str, PaymentStatus] = {
    "S": PaymentStatus.COMPLETED,   # Success
    "P": PaymentStatus.PENDING,     # Pending
    "D": PaymentStatus.DENIED,      # Denied
    "V": PaymentStatus.REVERSED,    # Reversed
}

# HTTP statuses worth trying again. 4xx other than 429 will not fix itself.
RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})


def sync_windows(
    start: datetime,
    end: datetime,
    window: timedelta = timedelta(days=MAX_TRANSACTION_WINDOW_DAYS),
    overlap: timedelta = DEFAULT_TRANSACTION_OVERLAP,
) -> tuple[tuple[datetime, datetime], ...]:
    """Split [start, end] into overlapping windows within the API's limit.

    Transaction Search refuses a range wider than 31 days, so a longer sync has
    to be chunked. Consecutive windows overlap rather than abut: a transaction
    whose timestamp sits exactly on a boundary, or which is backdated slightly
    as it settles, would otherwise fall between two syncs and never be seen.
    Overlap costs a duplicate read, which deduplication by transaction id makes
    free, and that trade is the right way round.
    """
    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError("sync windows require timezone-aware datetimes")
    if end < start:
        raise ValueError(f"end {end.isoformat()} precedes start {start.isoformat()}")
    if window <= timedelta(0):
        raise ValueError("window must be positive")
    if overlap < timedelta(0):
        raise ValueError("overlap cannot be negative")
    if overlap >= window:
        raise ValueError(
            f"overlap {overlap} must be smaller than the window {window}, "
            f"otherwise the walk cannot advance"
        )

    windows: list[tuple[datetime, datetime]] = []
    cursor = start
    while cursor < end:
        stop = min(cursor + window, end)
        windows.append((cursor, stop))
        if stop >= end:
            break
        cursor = stop - overlap
    if not windows:
        windows.append((start, end))
    return tuple(windows)


@dataclass(frozen=True)
class TransactionIngest:
    """Canonical payments from Transaction Search, with their source evidence.

    `skipped` records every transaction that was deliberately not turned into a
    payment, with the reason. Fees, refunds and balance transfers are not
    payments received, but they are not silently dropped either: an operator
    needs to be able to see what was excluded and why.
    """

    payments: tuple[Payment, ...] = ()
    raw_by_id: Mapping[str, dict[str, Any]] = field(default_factory=dict)
    skipped: tuple[tuple[str, str], ...] = ()
    windows: tuple[tuple[datetime, datetime], ...] = ()

    @property
    def transaction_count(self) -> int:
        return len(self.raw_by_id)


# Documented limits for GET /v2/invoicing/invoices.
MAX_INVOICE_PAGE_SIZE = 100
DEFAULT_INVOICE_PAGE_SIZE = 100

# Hard stop on the page walk. At the documented maximum page size this covers
# 100k invoices, far past anything this system should be ingesting in one pass,
# so hitting it means the API is misbehaving and we must not keep going.
DEFAULT_MAX_PAGES = 1000


def _usable_count(value: Any) -> Optional[int]:
    """A non-negative int from pagination metadata, or None if unusable.

    PayPal normally sends sound values, but metadata is not something to stake
    completeness on: a string, a float, a negative or a bool is ignored rather
    than trusted or allowed to abort the walk. bool is excluded deliberately,
    since True would otherwise read as 1.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 0 else None


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


def _parse_rfc3339(value: Any, context: str) -> datetime:
    """Parse a PayPal RFC 3339 timestamp into an aware UTC datetime."""
    if not value or not str(value).strip():
        raise PayPalAccountingError(
            f"Transaction {context} has no usable timestamp"
        )
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise PayPalAccountingError(
            f"Transaction {context} has an unparseable timestamp {value!r}: {exc}"
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


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
    # PayPal reports a sent, unpaid invoice as UNPAID. Observed live; relying
    # on the default fallback for a real status is how a mapping bug hides.
    "UNPAID": InvoiceStatus.SENT,
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
        # Injectable so tests exercise the backoff schedule without waiting.
        self._sleep = time.sleep

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
            if not body:
                return {}
            try:
                return json.loads(body)
            except ValueError as exc:
                raise PayPalAPIError(
                    0, f"malformed JSON in response to {path}: {exc}"
                ) from exc
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

    # ------------------------------------------------------------------
    # Complete pagination
    # ------------------------------------------------------------------

    def iter_raw_invoice_pages(
        self,
        page_size: int = DEFAULT_INVOICE_PAGE_SIZE,
        max_pages: int = DEFAULT_MAX_PAGES,
    ) -> "Iterator[dict[str, Any]]":
        """Yield every raw invoice page, in page order.

        Stops when a page comes back short or empty, or when the API's own
        total_pages says there is no more. Raises PayPalPaginationError rather
        than stopping quietly if the walk exceeds `max_pages`. Any transport or
        API failure propagates: a half-read list is not a result.
        """
        if page_size < 1:
            raise ValueError(f"page_size must be >= 1, got {page_size}")
        if max_pages < 1:
            raise ValueError(f"max_pages must be >= 1, got {max_pages}")

        effective = min(page_size, MAX_INVOICE_PAGE_SIZE)

        for page in range(1, max_pages + 1):
            data = self.list_raw_invoices(
                page=page, page_size=effective, total_required=True
            )
            yield data

            items = data.get("items") or []
            if len(items) < effective:
                return

            total_pages = _usable_count(data.get("total_pages"))
            if total_pages is not None and page >= total_pages:
                return

        raise PayPalPaginationError(
            f"invoice pagination exceeded max_pages={max_pages} at page_size="
            f"{effective}; refusing to continue or to return a partial dataset"
        )

    def list_all_raw_invoices(
        self,
        page_size: int = DEFAULT_INVOICE_PAGE_SIZE,
        max_pages: int = DEFAULT_MAX_PAGES,
    ) -> list[dict[str, Any]]:
        """Every raw invoice across all pages, deduplicated by PayPal id.

        Ordering is page order then within-page order, which is what PayPal
        returns and is stable across runs. Duplicates across page boundaries
        are dropped (they happen when records shift between requests), but a
        whole page of nothing but duplicates means the cursor is not advancing,
        and that fails closed instead of looping.
        """
        collected: list[dict[str, Any]] = []
        seen: set[str] = set()
        reported_total: Optional[int] = None

        for data in self.iter_raw_invoice_pages(
            page_size=page_size, max_pages=max_pages
        ):
            if reported_total is None:
                reported_total = _usable_count(data.get("total_items"))

            items = data.get("items") or []
            fresh = 0
            for item in items:
                invoice_id = item.get("id")
                if not invoice_id:
                    raise PayPalAccountingError(
                        "PayPal invoice listing returned an item without an 'id'; "
                        "it cannot be identified or deduplicated"
                    )
                if invoice_id in seen:
                    continue
                seen.add(invoice_id)
                collected.append(item)
                fresh += 1

            if items and fresh == 0:
                raise PayPalPaginationError(
                    f"page returned {len(items)} invoice(s), all duplicates of "
                    f"records already collected; the pagination cursor is not "
                    f"advancing, so the fetch cannot be shown to be complete"
                )

        if reported_total is not None and len(collected) != reported_total:
            raise PayPalPaginationError(
                f"incomplete invoice fetch: collected {len(collected)} invoice(s) "
                f"but PayPal reported total_items={reported_total}"
            )

        return collected

    def list_all_invoices(
        self,
        page_size: int = DEFAULT_INVOICE_PAGE_SIZE,
        max_pages: int = DEFAULT_MAX_PAGES,
    ) -> list[Invoice]:
        """Every invoice in the account, mapped into Invoice models."""
        return [
            self.map_invoice(item)
            for item in self.list_all_raw_invoices(
                page_size=page_size, max_pages=max_pages
            )
        ]

    @staticmethod
    def needs_payment_provenance(item: dict[str, Any]) -> bool:
        """True when this listed invoice shows money paid but not where from.

        The listing endpoint returns `payments.paid_amount` but omits
        `payments.transactions`; only the per-invoice endpoint carries them.
        Verified against the live Sandbox. Without the transactions the
        accounting guard cannot establish provenance for a partial payment and
        correctly refuses to infer it, so those invoices have to be fetched
        individually. Invoices with nothing paid need no extra request.
        """
        payments = item.get("payments") or {}
        if payments.get("transactions"):
            return False

        amount = item.get("amount") or {}
        due = item.get("due_amount") or {}
        if "value" not in amount or "value" not in due:
            return False

        try:
            currency = _invoice_currency(item)
            total = parse_money_to_minor_units(amount["value"], currency)
            outstanding = parse_money_to_minor_units(due["value"], currency)
        except (PayPalError, ValueError):
            # A malformed invoice is the accounting guard's problem to report,
            # not something to paper over with an extra request.
            return False

        return total > outstanding

    def hydrate_invoice(self, item: dict[str, Any]) -> dict[str, Any]:
        """Replace a listed invoice with its fuller per-invoice representation."""
        invoice_id = item.get("id")
        if not invoice_id:
            raise PayPalAccountingError(
                "cannot fetch provenance for an invoice without an 'id'"
            )
        return self.get_raw_invoice(str(invoice_id))

    def list_all_invoices_with_allocations(
        self,
        page_size: int = DEFAULT_INVOICE_PAGE_SIZE,
        max_pages: int = DEFAULT_MAX_PAGES,
        hydrate: bool = True,
    ) -> tuple[list[Invoice], list[PaymentAllocation]]:
        """Every invoice plus its PayPal-linked allocations, across all pages.

        The paginated counterpart of list_invoices_with_allocations. Allocations
        are deduplicated on the compound (payment_id, invoice_id) key, so a
        payment PayPal has already linked is never counted twice.

        With `hydrate` (the default), any invoice whose listing shows money paid
        but carries no transactions is refetched individually to get its payment
        provenance. Pass hydrate=False to see exactly what the listing alone
        supports; a partially paid invoice will then fail closed.
        """
        raw = self.list_all_raw_invoices(page_size=page_size, max_pages=max_pages)

        invoices: list[Invoice] = []
        raw_allocations: list[PaymentAllocation] = []
        for item in raw:
            if hydrate and self.needs_payment_provenance(item):
                item = self.hydrate_invoice(item)
            invoices.append(self.map_invoice(item))
            raw_allocations.extend(self.extract_linked_allocations(item))

        allocations = deduplicate_allocations(
            raw_allocations, error_cls=PayPalAccountingError
        )
        return invoices, allocations

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
    # Retrying transport for read-only endpoints
    # ----------------------------------------------------------------------

    def _request_with_retry(
        self,
        method: str,
        path: str,
        query: Optional[dict[str, Any]] = None,
        retries: int = DEFAULT_TRANSACTION_RETRIES,
    ) -> dict[str, Any]:
        """GET with bounded exponential backoff on transient failures.

        Retries 429 and 5xx and timeouts, because those are the ones that go
        away on their own. A 400 or a 404 will not fix itself, so retrying only
        burns rate-limit budget and delays the error.

        A 401 is handled separately and exactly once: the cached token is
        dropped and the request is replayed with a fresh one. Repeating that
        would turn a revoked credential into an authentication storm, so a
        second 401 is raised.
        """
        if method != "GET":
            raise ValueError("only GET requests are retried")
        if retries < 0:
            raise ValueError(f"retries must be >= 0, got {retries}")

        refreshed = False
        attempt = 0

        while True:
            try:
                return self._request(method, path, query=query)
            except PayPalAuthError:
                if refreshed:
                    raise
                # Controlled, single re-authentication. _request already
                # cleared the cached token when it saw the 401.
                refreshed = True
                self._access_token = None
                self._token_expires_at = 0.0
                continue
            except (PayPalAPIError, PayPalTimeoutError) as exc:
                retryable = isinstance(exc, PayPalTimeoutError) or (
                    getattr(exc, "status_code", None) in RETRYABLE_STATUSES
                )
                if not retryable or attempt >= retries:
                    raise
                delay = min(2.0 ** attempt, MAX_BACKOFF_SECONDS)
                self._sleep(delay)
                attempt += 1

    # ----------------------------------------------------------------------
    # Transaction Search (GET /v1/reporting/transactions)
    # ----------------------------------------------------------------------

    @staticmethod
    def _format_search_timestamp(moment: datetime) -> str:
        """PayPal's Transaction Search wants an RFC 3339 timestamp in UTC."""
        return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S-0000")

    def search_raw_transactions(
        self,
        start: datetime,
        end: datetime,
        page: int = 1,
        page_size: int = DEFAULT_TRANSACTION_PAGE_SIZE,
        retries: int = DEFAULT_TRANSACTION_RETRIES,
    ) -> dict[str, Any]:
        """One raw page of Transaction Search for one window.

        `fields=all` is required to get payer_info and the reference fields;
        the default response carries transaction_info only.
        """
        return self._request_with_retry(
            "GET",
            "/v1/reporting/transactions",
            query={
                "start_date": self._format_search_timestamp(start),
                "end_date": self._format_search_timestamp(end),
                "fields": "all",
                "page": page,
                "page_size": min(page_size, MAX_TRANSACTION_PAGE_SIZE),
            },
            retries=retries,
        )

    def iter_raw_transactions(
        self,
        start: datetime,
        end: datetime,
        page_size: int = DEFAULT_TRANSACTION_PAGE_SIZE,
        max_pages: int = DEFAULT_MAX_PAGES,
        retries: int = DEFAULT_TRANSACTION_RETRIES,
    ) -> "Iterator[dict[str, Any]]":
        """Yield every raw transaction in one window, across all its pages."""
        if page_size < 1:
            raise ValueError(f"page_size must be >= 1, got {page_size}")
        if max_pages < 1:
            raise ValueError(f"max_pages must be >= 1, got {max_pages}")

        effective = min(page_size, MAX_TRANSACTION_PAGE_SIZE)

        for page in range(1, max_pages + 1):
            data = self.search_raw_transactions(
                start, end, page=page, page_size=effective, retries=retries
            )
            details = data.get("transaction_details")
            if details is None:
                details = []
            if not isinstance(details, list):
                raise PayPalAPIError(
                    0,
                    "malformed Transaction Search response: transaction_details "
                    f"is {type(details).__name__}, expected a list",
                )

            for item in details:
                yield item

            if len(details) < effective:
                return
            total_pages = _usable_count(data.get("total_pages"))
            if total_pages is not None and page >= total_pages:
                return

        raise PayPalPaginationError(
            f"Transaction Search exceeded max_pages={max_pages} for window "
            f"{start.isoformat()}..{end.isoformat()}; refusing to return a "
            f"partial dataset"
        )

    def map_transaction(self, item: dict[str, Any]) -> Optional[Payment]:
        """Map one raw Transaction Search record onto a canonical Payment.

        Returns None when the record is not a payment received (a fee, refund
        or transfer, which carry a non-positive amount). The caller records
        those as skipped rather than discarding them unseen.

        Reference handling is deliberately narrow. `invoice_id` is PayPal's own
        structured link to an invoice, so it becomes Payment.reference.
        `custom_field`, `transaction_subject` and `transaction_note` are free
        text that merchants and payers put anything into, so they become
        Payment.note, where the matcher may extract a reference only if it
        matches a known invoice number. Promoting them to `reference` would be
        inventing a reference from an unrelated field.
        """
        info = item.get("transaction_info") or {}

        txn_id = info.get("transaction_id")
        if not txn_id or not str(txn_id).strip():
            raise PayPalAccountingError(
                "Transaction Search returned a record without a transaction_id; "
                "it cannot be identified or deduplicated"
            )
        txn_id = str(txn_id).strip()

        amount = info.get("transaction_amount") or {}
        raw_currency = amount.get("currency_code")
        if not raw_currency or not str(raw_currency).strip():
            raise PayPalAccountingError(
                f"Transaction {txn_id} has no currency_code; its amount cannot "
                f"be converted to minor units"
            )
        try:
            currency = normalize_currency(raw_currency)
        except ValueError as exc:
            raise PayPalAccountingError(
                f"Transaction {txn_id} has an unusable currency "
                f"{raw_currency!r}: {exc}"
            ) from exc

        try:
            minor_units = parse_money_to_minor_units(amount.get("value"), currency)
        except ValueError as exc:
            raise PayPalAccountingError(
                f"Transaction {txn_id} has an invalid amount: {exc}"
            ) from exc

        if minor_units <= 0:
            return None

        raw_status = str(info.get("transaction_status", "")).strip().upper()
        try:
            status = TRANSACTION_STATUS_MAP[raw_status]
        except KeyError:
            raise PayPalAccountingError(
                f"Transaction {txn_id} has unrecognised transaction_status "
                f"{raw_status!r}; refusing to guess whether it completed"
            ) from None

        received_at = _parse_rfc3339(
            info.get("transaction_initiation_date")
            or info.get("transaction_updated_date"),
            txn_id,
        )

        payer = item.get("payer_info") or {}
        payer_name_block = payer.get("payer_name") or {}
        payer_name = (
            payer_name_block.get("alternate_full_name")
            or payer_name_block.get("given_name")
            or ""
        )
        payer_email = payer.get("email_address")

        invoice_id = info.get("invoice_id")
        reference = str(invoice_id).strip() if invoice_id and str(invoice_id).strip() else None

        note_parts = [
            str(info[key]).strip()
            for key in ("custom_field", "transaction_subject", "transaction_note")
            if info.get(key) and str(info[key]).strip()
        ]

        return Payment(
            payment_id=txn_id,
            amount_cents=minor_units,
            currency=currency,
            received_at=received_at,
            payer_name=payer_name,
            payer_email=payer_email,
            reference=reference,
            note=" | ".join(note_parts),
            status=status,
        )

    def map_transactions(
        self,
        items: Sequence[dict[str, Any]],
        windows: Sequence[tuple[datetime, datetime]] = (),
    ) -> TransactionIngest:
        """Map raw transactions into canonical payments, deduplicated by id.

        The same transaction arriving twice (window overlap, or a retry) is
        collapsed. Two records sharing an id but disagreeing about the money
        fail closed through the canonical delivery resolver rather than one
        silently winning.
        """
        raw_by_id: dict[str, dict[str, Any]] = {}
        payments: dict[str, Payment] = {}
        skipped: list[tuple[str, str]] = []

        for item in items:
            payment = self.map_transaction(item)
            info = item.get("transaction_info") or {}
            txn_id = str(info.get("transaction_id", "")).strip()

            raw_by_id.setdefault(txn_id, item)

            if payment is None:
                amount = (info.get("transaction_amount") or {}).get("value")
                if not any(t == txn_id for t, _ in skipped):
                    skipped.append((
                        txn_id,
                        f"amount {amount} is not a positive payment received "
                        f"(fee, refund or transfer)",
                    ))
                continue

            existing = payments.get(txn_id)
            if existing is None:
                payments[txn_id] = payment
                continue
            try:
                payments[txn_id] = resolve_delivery([existing, payment])
            except PaymentConflictError as exc:
                raise PayPalAccountingError(
                    f"Transaction {txn_id} was returned twice with "
                    f"contradictory data: {exc}"
                ) from exc

        return TransactionIngest(
            payments=tuple(payments.values()),
            raw_by_id=raw_by_id,
            skipped=tuple(skipped),
            windows=tuple(windows),
        )

    def list_transactions(
        self,
        start: datetime,
        end: datetime,
        page_size: int = DEFAULT_TRANSACTION_PAGE_SIZE,
        max_pages: int = DEFAULT_MAX_PAGES,
        retries: int = DEFAULT_TRANSACTION_RETRIES,
        overlap: timedelta = DEFAULT_TRANSACTION_OVERLAP,
    ) -> TransactionIngest:
        """Every transaction in [start, end], as canonical payments.

        The range is split into overlapping windows inside the API's 31-day
        limit, each window is paged to exhaustion, and the union is
        deduplicated by transaction id.
        """
        windows = sync_windows(start, end, overlap=overlap)
        raw: list[dict[str, Any]] = []
        for w_start, w_end in windows:
            raw.extend(
                self.iter_raw_transactions(
                    w_start,
                    w_end,
                    page_size=page_size,
                    max_pages=max_pages,
                    retries=retries,
                )
            )
        return self.map_transactions(raw, windows=windows)

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

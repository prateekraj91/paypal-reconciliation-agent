# PayPal Transaction Search field mapping

Established from an actual Sandbox response, not from memory. Captured
2026-11-03 from `GET /v1/reporting/transactions?fields=all` against
`https://api-m.sandbox.paypal.com`, cross-checked against PayPal's Transaction
Search API reference.

All values below are structural. No credential, token or payer identity from
the live response is reproduced here.

## Permission

Transaction Search requires the scope
`https://uri.paypal.com/services/reporting/search/read`.

The configured Sandbox app **has** it. The granted scope list was read back
from the token response, and the endpoint returns HTTP 200. This is verified,
not assumed. An app without the scope gets HTTP 403 and the reporting feature
has to be enabled on the app in the PayPal developer dashboard.

## Response envelope

| Field | Meaning | Used for |
|---|---|---|
| `total_items` | transactions in the window | completeness cross-check |
| `total_pages` | pages in the window | pagination terminator |
| `page` | current page | — |
| `start_date` / `end_date` | window **as PayPal applied it** | drift detection |
| `last_refreshed_datetime` | how current the index is | appearance-delay evidence |
| `account_number` | merchant account | **not ingested** (identity) |
| `transaction_details[]` | the transactions | ingestion |

`end_date` comes back clamped to `last_refreshed_datetime`. In the captured
response the request asked for `04:19Z` and PayPal answered with `02:59:59Z`.
That is the three-hour appearance delay, visible in the data: reading up to
"now" silently returns a window that is still filling.

## Per-transaction sections

`transaction_info`, `payer_info`, `shipping_info`, `cart_info`, `store_info`,
`auction_info`, `incentive_info`. Only the first two are ingested; the rest were
empty objects for the captured transaction.

## Field mapping onto the canonical `Payment`

| PayPal field | Canonical field | Notes |
|---|---|---|
| `transaction_info.transaction_id` | `payment_id` | **canonical dedupe key**; absent ⇒ fail closed |
| `transaction_info.transaction_amount.value` | `amount_cents` | parsed via the currency exponent table |
| `transaction_info.transaction_amount.currency_code` | `currency` | absent or unsupported ⇒ fail closed |
| `transaction_info.transaction_status` | `status` | `S`→COMPLETED, `P`→PENDING, `D`→DENIED, `V`→REVERSED; unknown ⇒ fail closed |
| `transaction_info.transaction_initiation_date` | `received_at` | falls back to `transaction_updated_date` |
| `transaction_info.invoice_id` | `reference` | **the only** source of the structured reference |
| `transaction_info.custom_field` | `note` | merchant-defined free text |
| `transaction_info.transaction_subject` | `note` | payer/merchant free text |
| `transaction_info.transaction_note` | `note` | payer free text |
| `payer_info.payer_name.alternate_full_name` | `payer_name` | absent ⇒ `""`, never invented |
| `payer_info.email_address` | `payer_email` | absent ⇒ `None` |
| `payer_info.account_id` | — | **not ingested** |
| `transaction_info.ending_balance`, `available_balance` | — | account balances, not this payment |
| `transaction_info.transaction_event_code` | — | retained in raw evidence only |

### Which fields carry what, directly answering the D2 question

- **Invoice ID / invoice reference** — `transaction_info.invoice_id`. This is
  PayPal's own structured link from a transaction to an invoice, and it is the
  only field treated as a reference.
- **Custom ID** — `transaction_info.custom_field`. Merchant-defined and
  arbitrary, so it is preserved as free text, never as a reference.
- **Payer identity** — `payer_info.email_address` and
  `payer_info.payer_name.*` (`alternate_full_name`, `given_name`, `surname`).
  `payer_info.account_id` exists but is not ingested.
- **Payer note / payment reference** — `transaction_info.transaction_subject`
  and `transaction_info.transaction_note`.
- **Transaction status** — `transaction_info.transaction_status`, a single
  letter.
- **Currency** — `transaction_info.transaction_amount.currency_code`.

## Why free text is never promoted to `reference`

`reference` is the field the matchers trust. `custom_field`,
`transaction_subject` and `transaction_note` are places a human types whatever
they like, so a number in them is not a reference, it is a number. They go into
`note`, where `extract_invoice_references` will only resolve a string that
matches a **known** invoice number, and where the digit-run boundary rule stops
`"Paid INV 10.01 USD"` from resolving to invoice `INV-1001`.

Promoting them would be inventing a reference from an unrelated field.

## Observed in the live Sandbox

The one transaction currently in the account is the Sandbox balance funding:
`transaction_event_code` `T1900`, `transaction_subject` "Initial balance",
status `S`, 5000.00 USD. It carries **no** `invoice_id` and **no**
`custom_field`, because it is not an invoice payment. T1900 records are kept
as provider evidence (`account_event.transaction_search`) and are never turned
into payments, so the opening balance cannot appear as unapplied customer cash.

So the live account confirms the envelope, the amount, status, timestamp and
note mappings. It does **not** yet exercise `invoice_id` or `custom_field`;
those need real invoice payments, which is what the seeding step creates.

## Transactions that are not payments received

Fees, refunds and transfers carry a non-positive `transaction_amount`. They are
not mapped to a `Payment`, and they are not dropped unseen either: each is
recorded in `TransactionIngest.skipped` with a reason, and its raw record is
kept in `raw_by_id`.

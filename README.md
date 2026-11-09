# paypal-recon-agent

Reconciliation exception agent for PayPal: it matches payments to invoices,
verifies each match independently, and queues it for a person to approve. All
data in this repository is synthetic or from the PayPal Sandbox.

`src/reconcile` is the framework-independent matching core. `src/reconcile_store`
is the ledger and approval engine, `src/reconcile_api` the FastAPI service,
`src/reconcile_investigator` the advisory AI investigator, and `frontend` the
operator dashboard.

## How a payment is reconciled

1. **Matching.** Deterministic matchers propose allocations from invoice
   references, customer context and amounts. They match against the money left
   on the payment after returns, never its gross amount.
2. **Independent verification.** `verify_proposals` rechecks every proposal from
   source records; amount alone can never pass.
3. **AI investigation (advisory).** An investigator can explain an exception and
   suggest a resolution. It cannot change financial records. A separate safety
   verifier checks its output, including for prompt injection.
4. **Human approval.** Only an operator's approval creates an allocation. Inside
   the approval transaction the proposal, payment and invoices are locked and
   the verifier and capacity checks run again on current data. A proposal that
   no longer fits is refused with an explanation, never reduced.

Every decision is written to a SHA-256 hash-chained audit trail.

## Refund and reversal accounting

`src/reconcile_store/ledger.py` is the single source for payment and invoice
balances. All amounts are integer minor units.

- Only completed returns count, and a repeated provider event adds nothing.
- An allocation and a provider settlement for the same payment and invoice are
  the same money and count once.
- A return linked to an allocation or settlement reverses that line. A return
  naming an invoice reverses this payment's line on that invoice, if the line
  existed when the return happened.
- Any other return comes out of unapplied cash first, then reverses applied
  lines oldest first, restoring each cent once.
- A return or applied line in another currency leaves the payment with zero
  available cash until someone resolves it.

## Setup

Requires Python 3.10 or later, Node.js, and PostgreSQL 16 (SQLite works for local development).

    python -m venv .venv && .venv/bin/pip install -e ".[dev]"
    cp .env.example .env   # then fill in your own values
    set -a && source .env && set +a
    .venv/bin/alembic upgrade head
    PYTHONPATH=src .venv/bin/uvicorn reconcile_api.main:app --port 8000
    npm --prefix frontend install && npm --prefix frontend run dev

Open http://127.0.0.1:5173 and sign in with your `ADMIN_API_KEY`.

`.env.example` lists every setting. The main ones are `RECONCILE_DB_URL`,
`ADMIN_API_KEY`, the `PAYPAL_*` credentials, `CORS_ALLOWED_ORIGINS` (empty by
default, so no cross-origin browser access) and `MODEL_PROVIDER`.

## PayPal Sandbox setup

1. In the [PayPal Developer Dashboard](https://developer.paypal.com/dashboard/),
   create a Sandbox REST app. Enable Invoicing and Transaction Search for it.
2. Put its credentials in `.env` as `PAYPAL_CLIENT_ID` and
   `PAYPAL_CLIENT_SECRET`. Keep `PAYPAL_BASE_URL` on
   `https://api-m.sandbox.paypal.com`.
3. Create test invoices, either in the Sandbox or with the seeding tool, which is
   the only code that writes to PayPal:

        PYTHONPATH=src:. python -m tools.seed_sandbox           # dry run, plans only
        PYTHONPATH=src:. python -m tools.seed_sandbox --apply   # idempotent

4. Pay an invoice as a Sandbox personal account, then run a read-only sync:

        curl -X POST http://127.0.0.1:8000/admin/tenants \
          -H "Authorization: Bearer $ADMIN_API_KEY" -H 'Content-Type: application/json' \
          -d '{"tenant_id":"test_tenant","name":"PayPal Sandbox"}'
        curl -X POST http://127.0.0.1:8000/admin/connections \
          -H "Authorization: Bearer $ADMIN_API_KEY" -H 'Content-Type: application/json' \
          -d '{"tenant_id":"test_tenant","provider":"paypal","environment":"sandbox","status":"ACTIVE"}'
        curl -X POST http://127.0.0.1:8000/admin/sync \
          -H "Authorization: Bearer $ADMIN_API_KEY" -H 'Content-Type: application/json' \
          -d '{"tenant_id":"test_tenant","environment":"sandbox","start_date":"<ISO-8601 start>","end_date":"<ISO-8601 end>","idempotency_key":"sync-1"}'
        PYTHONPATH=src .venv/bin/python -m reconcile_worker --once --tenant-id test_tenant

   Use a new `idempotency_key` for each sync.

A new payment reaches the Invoicing API first and Transaction Search later (about
two hours in our testing). Until then it has a placeholder payer. A later sync
merges both views into the same payment.

## Ollama configuration

The AI investigator defaults to a deterministic mock. To use a local model:

    ollama pull qwen3:8b
    ollama serve

Then set these in `.env` and restart the API:

    MODEL_PROVIDER=ollama
    OLLAMA_BASE_URL=http://127.0.0.1:11434
    OLLAMA_MODEL=qwen3:8b

Use a current Ollama release. A run takes a few minutes on a laptop. Each stored
investigation records its provider, so mock output is never mistaken for model
output. The investigator is advisory: its output goes through the independent
verifier and can never change a financial record.

## Demo scenarios

The dashboard's seed action, or `POST /admin/demo/seed`, loads a synthetic tenant,
`demo_merchant`. Use a local database for it. None of these records came from PayPal.

| Payment | Scenario |
|---|---|
| `PAY-DEMO-EXACT-001` | Exact match, ready for approval |
| `PAY-DEMO-UNMATCHED-002` | No matching invoice |
| `PAY-DEMO-COMPETING-003` | Competing invoices, stays ambiguous |
| `PAY-DEMO-PARTIAL-004` | Partial payment |
| `PAY-DEMO-SPLIT-005` | One payment split across invoices |
| `PAY-DEMO-OVERPAY-006` | Overpayment |
| `PAY-DEMO-REFUND-007` | Refund |
| `PAY-DEMO-REVERSAL-008` | Full reversal |
| `PAY-DEMO-CURR-MISMATCH-009` | USD payment against a EUR invoice |
| `PAY-DEMO-INVESTIGATE-010` | Case for AI investigation |
| `PAY-DEMO-INJECTION-011` | Prompt injection in payment metadata |

## Reset

Everything here applies to a local database only. Never run it against a shared
or production one.

    dropdb <local_db> && createdb <local_db>
    set -a && source .env && set +a
    .venv/bin/alembic upgrade head

Restart the API, then load the demo data again from the dashboard or with
`POST /admin/demo/seed`. To keep a known state between takes, `pg_dump` the
database once and restore that dump instead. Do approvals in a disposable copy.

## Tests

    PYTHONPATH=src .venv/bin/pytest tests/ -q
    npm --prefix frontend test
    npm --prefix frontend run build

`tests/conftest.py` ignores the repository's `.env` files and refuses PostgreSQL
connections to anything except a local disposable database. Set
`TEST_POSTGRESQL_URL` to a local database to include the PostgreSQL concurrency
tests; without it they are skipped.

## Known limitations and security

- One shared `ADMIN_API_KEY` protects the admin API, and any holder can act for
  any tenant. Per-user, per-tenant authorization is required before more than
  one merchant uses a deployment.
- The reviewer name on a decision is supplied by the caller, not authenticated.
- A fully reversed payment still shows as reconciled in the existing status
  field; use its net and unapplied amounts.
- Proposals stored before a ledger change stay as they are. They are re-evaluated
  live and blocked if they no longer fit; run reconciliation again to regenerate.
- This has been exercised with synthetic and Sandbox data only.
- Sync is manual: webhook ingestion needs a public URL and a webhook registered
  in PayPal.
- Payments that PayPal already settled against an invoice appear as
  `PROVIDER_SETTLED`, not as proposals to approve.
- A repeat sync adds no payments or settlements, but it does add a new
  reconciliation run and new proposal rows.
- The Sandbox opening balance (event `T1900`) can appear as a payment in
  databases synced before non-payment events were excluded.
- The local model's output is often rejected by the verifier. A rejection fails
  closed. Results on demo cases don't measure accuracy.

## Core matcher

    pip install pytest
    PYTHONPATH=src python -m pytest -q
    PYTHONPATH=src python -m reconcile  # matcher scorecards on the synthetic dataset
    PYTHONPATH=src python -m reconcile 7   # same, different seed
    PYTHONPATH=src python -m reconcile --paypal        # invoice ingestion only
    PYTHONPATH=src python -m reconcile sync --sandbox  # full synchronisation pass

`sync --sandbox` is read-only: it fetches every invoice page and every
transaction window, maps both into canonical records, matches and verifies, and
reports. It approves nothing and moves no balance. Running it twice produces
the same result; a sync that double-counted on its second run would be worse
than one that failed, because the ledger would drift quietly.

Seeding Sandbox test data (the only code that writes to PayPal):

    PYTHONPATH=src:. python -m tools.seed_sandbox           # dry run, plans only
    PYTHONPATH=src:. python -m tools.seed_sandbox --apply   # idempotent

## Layout

    src/reconcile/
      models.py        Invoice, Payment, PaymentAllocation, MatchProposal, the three state enums
                       plus ledger arithmetic and migrate_allocation_currency()
      interfaces.py    InvoiceSource, PaymentSource, Matcher protocols + StaticSource
      normalize.py     canonical_reference(), extract_invoice_references()
      money.py         ISO 4217 minor units; exact decimal parsing, no floats
      delivery.py      canonical record per payment_id: status transitions, fail-closed conflicts
      matching.py      MatchRun, the per-run state every matcher shares
      baseline.py      exact reference + exact amount matcher (proposals only)
      deterministic.py reference-in-free-text, partial and grouped payments (proposals only)
      verifier.py      independent re-check of proposals; approves nothing
      workflow.py      ingestion -> matching -> verification for one batch
      sync.py          one repeatable synchronisation pass; approves nothing
      paypal.py        PayPal adapter: Invoicing v2 + Transaction Search, read-only
      dataset.py       seeded synthetic data with ground truth
      scoring.py       precision, recall, false auto-matches, review queue
    tools/
      seed_sandbox.py  idempotent Sandbox seeding (the ONLY code that writes)
    tests/
      fixtures/        real, sanitised Sandbox responses
    docs/
      paypal-transaction-fields.md   field mapping, established from real responses

## Three separate states

| State | Type | Meaning | Who sets it |
|---|---|---|---|
| Payment status | `PaymentStatus` | What PayPal confirms | PayPal adapter |
| Allocation status | `AllocationStatus` | Is a proposed split approved? | Approval workflow |
| Reconciliation status | `ReconciliationStatus` | Where the payment sits in our workflow | Matchers: UNMATCHED / PROPOSED / NEEDS_REVIEW. RECONCILED only via the future ledger |

Matchers can only emit `PROPOSED` allocations (enforced in `Candidate`). Only
`APPROVED` allocations reduce an invoice's outstanding balance.

## Modelling assumption to verify in the PayPal spike

Outstanding balance = invoice total minus approved allocations in OUR ledger.
How PayPal itself links a payment to an invoice, and which payments arrive
unlinked, decides what counts as "unmatched". Confirm this against the sandbox
before building the adapter.

## Dataset ground truth

Each payment is labelled AUTO_MATCH (evidence supports one allocation),
NEEDS_HUMAN (evidence insufficient; must escalate), or NO_ACTION (refunded).
Deliberate traps: amount collisions between strangers, closed invoices sharing
an open invoice's amount, duplicate webhook deliveries, double payments,
two invoice combinations with the same total.

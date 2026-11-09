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

## Service and dashboard

    python -m venv .venv && .venv/bin/pip install -e ".[dev]"
    cp .env.example .env   # then fill in your own values
    .venv/bin/alembic upgrade head
    PYTHONPATH=src .venv/bin/uvicorn reconcile_api.main:app --port 8000
    npm --prefix frontend install && npm --prefix frontend run dev

`.env.example` lists every setting. The main ones are `RECONCILE_DB_URL`,
`ADMIN_API_KEY`, the `PAYPAL_*` credentials, `CORS_ALLOWED_ORIGINS` (empty by
default, so no cross-origin browser access) and `MODEL_PROVIDER`.

The dashboard's seed action, or `POST /admin/demo/seed`, loads a synthetic demo
tenant (`demo_merchant`). Use a local database for it.

## Tests

    PYTHONPATH=src .venv/bin/pytest tests/ -q
    npm --prefix frontend test
    npm --prefix frontend run build

`tests/conftest.py` ignores the repository's `.env` files and refuses PostgreSQL
connections to anything except a local disposable database. Set
`TEST_POSTGRESQL_URL` to a local database to include the PostgreSQL concurrency
tests; without it they are skipped.

## Security and limitations

- One shared `ADMIN_API_KEY` protects the admin API, and any holder can act for
  any tenant. Per-user, per-tenant authorization is required before more than
  one merchant uses a deployment.
- The reviewer name on a decision is supplied by the caller, not authenticated.
- A fully reversed payment still shows as reconciled in the existing status
  field; use its net and unapplied amounts.
- Proposals stored before a ledger change stay as they are. They are re-evaluated
  live and blocked if they no longer fit; run reconciliation again to regenerate.
- This has been exercised with synthetic and Sandbox data only.

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

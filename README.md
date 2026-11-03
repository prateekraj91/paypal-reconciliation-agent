# paypal-recon-agent (core)

Framework-independent core of a reconciliation exception agent for PayPal.
This milestone has no FastAPI, SQLite, React, PayPal SDK, or AI. Standard library only.

## Run

    pip install pytest
    python -m pytest -q                 # 622 tests
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

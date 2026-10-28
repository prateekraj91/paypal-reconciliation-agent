# paypal-recon-agent (core)

Framework-independent core of a reconciliation exception agent for PayPal.
This milestone has no FastAPI, SQLite, React, PayPal SDK, or AI. Standard library only.

## Run

    pip install pytest
    python -m pytest -q                 # 37 tests
    PYTHONPATH=src python -m reconcile  # baseline scorecards on the synthetic dataset
    PYTHONPATH=src python -m reconcile 7   # same, different seed

## Layout

    src/reconcile/
      models.py      Invoice, Payment, PaymentAllocation, MatchProposal, the three state enums
      interfaces.py  InvoiceSource, PaymentSource, Matcher protocols + StaticSource
      normalize.py   canonical_reference()
      baseline.py    exact reference + exact amount matcher (proposals only)
      dataset.py     seeded synthetic data with ground truth
      scoring.py     precision, recall, false auto-matches, review queue
    tests/

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

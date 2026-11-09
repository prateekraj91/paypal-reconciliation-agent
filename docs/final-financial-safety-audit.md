# Final Financial Safety Audit — Defect Inventory

Status: inventory recorded before any code change on `fix/final-financial-safety-audit` (base `5876de9`).
Results are appended at the end after fixes. Priority scale: P0 financial loss/unauthorised action/tenant breach,
P1 incorrect financial result or unsafe approval, P2 operator/API defect, P3 polish.

## Confirmed defects

| ID | Pri | Location | Root cause | Impact | Correction |
|----|-----|----------|------------|--------|------------|
| F1 | P0 | `approval.py::approve_proposal` (Step 1) | Proposal row is read without a lock; only payment/invoices are locked. A second concurrent approval uses a stale `verifier_status`. | Same proposal allocated twice where capacity remains. | Lock proposal `FOR UPDATE`, refresh locked rows, re-check idempotency after lock. |
| F2 | P1 | `approval.py::evaluate_proposal_investigation_safety`, `approve_proposal` | Allow-list only excludes INVALID/UNVERIFIED, so REJECTED/ESCALATED proposals are eligible. NEEDS_REVIEW branch ignores proposal verifier status. | A rejected proposal can be approved and post allocations. | Fail-closed allow-list. |
| F3 | P1 | `approval.py::reject_proposal`, `escalate_proposal` | No state gate. | Approved proposal can be rejected while its allocation stays; decision history contradicts the ledger. | Reject only undecided proposals. |
| F4 | P1 | `baseline.py`/`matching.py`, `advanced.py`, `deterministic.py` | Matchers use gross `amount_cents`, ignoring returns. | REVERSAL-008 / REFUND-007 generated VERIFIED proposals for money no longer held. | Match on current net capacity; fully returned payments are NO_MATCH. |
| F5 | P1 | `reconcile/verifier.py::verify_proposals`, approval Step 4 | Verifier compares to gross; approval only loads refunds for payments that already have allocations. | Independent verifier passes stale proposals. | Verifier checks current capacity. |
| F6 | P1 | `invoice_repo.py::get_total_returns_minor` | Payment-level excess restored `min(alloc, excess)` on *every* invoice. | $50 return on a $50+$50 split restored $100 of invoice capacity; invoices can be over-collected. | One deterministic waterfall per payment. |
| F7 | P1 | `invoice_repo.py::get_total_returns_minor` | Any return tagged with the invoice restores capacity even if its parent payment never applied to it. | Unrelated refund reopens a paid invoice. | Only returns of payments applied to the invoice restore it. |
| F8 | P1 | `payment_repo.py::get_unapplied_cash_minor` | Allocation-linked return deducted from unapplied as well as reversing the allocation. | Allocation counted twice; unapplied understated. | Ledger semantics in `ledger.py`. |
| F9 | P1 | `payment_repo.py::get_unapplied_cash_minor` | Provider settlements ignored. | Settled payment shows as free cash. | Settlements consume capacity. |
| F10 | P1 | `invoice_repo.py::get_ledger_outstanding_minor` | Allocation + later provider settlement for same payment/invoice summed. | Invoice balance understated (double counted). | Count a payment/invoice pair once. |
| F11 | P1 | `main.py::approve_proposal` | `LedgerConstraintError` / not-found not mapped. | HTTP 500 on the exact stale-proposal path. | 409/404 with explanation. |
| F12 | P1 | `main.py::_enrich_proposal_payload`, dashboard | `approval_eligible` ignored ledger capacity. | Review Queue offers Approve on REVERSAL-008/REFUND-007; "6 actionable" overstated. | Eligibility runs the same checks as approval. |
| F13 | P1 | `tests/test_d7_local_demo_e2e.py` | Force-loads `.env.demo`, reseeds and approves in `reconcile_demo`; leaks env to later tests. `.env` points at remote DB. | `pytest tests/` mutates the populated demo DB and can reach remote DB. | Hermetic conftest with connection guard; E2E uses disposable DB. |
| F14 | P2 | `main.py` CORS | `allow_origins=["*"]` with credentials. | Any origin may call the API from a browser given a token. | Explicit `CORS_ALLOWED_ORIGINS`, no credentials mode. |
| F15 | P2 | `main.py` idempotency keys | Reviewer (≤255) embedded in `String(128)` key. | Long reviewer → DB error/500 on Postgres. | Hash when over length. |
| F16 | P2 | `reconciliation_repo.py::list_proposals`, `main.py::list_proposals` | Status filter after LIMIT; `total_count` is page size. | Pending items hidden behind newer decided ones; wrong totals. | Filter before pagination, true count. |
| F17 | P2 | `main.py` list endpoints | Unbounded/negative `limit`,`offset`. | Oversized responses, surprising slices. | Validated bounds. |
| F18 | P2 | `main.py` | `TenantIsolationError` unhandled; message names the other tenant. | 500 and cross-tenant existence leak. | Map to 404, generic body. |
| F19 | P2 | `main.py::list_transactions/get_transaction/dashboard` | Net uses non-completed returns; dashboard reconciled is gross of returns; status compare differs from Transactions. | Figures disagree with ledger. | Use ledger values. |
| F20 | P2 | `main.py::_enrich_proposal_payload` | Candidate outstanding ignores returns. | Evidence modal shows wrong balance. | Use ledger outstanding. |
| F21 | P2 | `reconcile_investigator/verifier.py` | Own capacity/outstanding maths with unfiltered return status. | AI verifier disagrees with ledger. | Delegate to repositories. |
| F22 | P2 | `ProposalsPage.tsx`, `ReconciliationPage.tsx`, `Modals.tsx` | Actionable count ignores eligibility; Runs page Approve has no gate; `undefined` treated as eligible; load errors look like empty queue. | Misleading operator state. | Fail-closed UI, error state. |
| F23 | P3 | `main.py::paypal_webhook` | `int(content-length)` unguarded. | 500 on malformed header. | 400. |

## Speculative / accepted, not changed

- Single shared `ADMIN_API_KEY`: any holder can pass any `tenant_id`. Repositories are tenant-scoped, but there is no per-tenant principal. **Must be resolved before a multi-merchant pilot.**
- Reviewer is a self-reported label, not an authenticated identity.
- Fully reversed payments display `RECONCILED` (nothing left to apply; explicitly asserted by D7 tests).
- `MatchRun` aborts a whole run if one invoice fails the PayPal due-amount cross-check (fail-closed, wide blast radius).
- Force reseed of the `demo_merchant` tenant deletes that tenant's audit rows by design.

---

## Results after fixes

### Status of the inventory

F1–F23 are fixed. Corrections made while fixing:

- **F1** was reclassified after a forced reproduction. See "Concurrency findings" below: double approval was not exploitable, but a different race on the same proposal was.
- **F2**: `NOT_VERIFIED` stays approvable, as the existing D4 tests require, because approval always reruns the independent verifier in its transaction. `REJECTED`, `ESCALATED`, `UNVERIFIED`, `INVALID` and `NEEDS_REVIEW` stored on the proposal are refused.
- A check that refused any proposal older than the newest for its payment was tried and removed. It broke the documented multiple-pending-proposals flow, and capacity revalidation already refuses proposals that no longer fit.

### Found during verification

| ID | Priority | Defect | Fix |
|----|----------|--------|-----|
| F24 | P1 | Run persistence saved `reasons` instead of candidate evidence, so a generated overpayment proposal lost `REFERENCE_MATCH`. It showed as approvable and then failed at approval. | `reconcile_store/workflow.py` persists candidate evidence for proposed matches. |
| F25 | P2 | The mock investigator suggested the gross amount after a refund or reversal. | The mock suggests net cash, and nothing when none remains. |

### Existing tests changed

No test was deleted. Each change follows from corrected financial behaviour or from isolation:

- `test_d6_hardening.py::test_verifier_accounting_with_existing_reversals` asserted that a reversed, fully applied payment could fund a further allocation (F5, F9). It now uses a replacement payment; the 6,000 allowed and 7,000 refused limits are unchanged.
- `test_d7_api.py` actionable demo count goes from 6 to 5 because REVERSAL-008 has no net cash. The dynamic-count fixture now uses proposals that can really pass the verifier; its assertions are unchanged.
- `test_d7_local_demo_e2e.py` now runs on a disposable database (F13) and expects 5 actionable.

### Verification

| Check | Result |
|-------|--------|
| Backend tests, private local PostgreSQL | 1006 passed |
| Original code, same environment | 960 passed, 10 failed, 1 error (environment-related) |
| New regression file on original code | 22 of its tests fail, confirming they detect the defects |
| Frontend tests | 11 passed |
| Frontend build | Passed |
| UI at 1440, 1280, 1084, 500 px on a disposable seeded database | 7 pages plus Investigation Details rendered, no page overflow, no console errors, no HTTP 500s |
| Review Queue on the seeded database | 5 Approve enabled, 2 blocked, header "5 Actionable (11 Unresolved)" |
| Matching benchmark, seed 0 | Output identical to the original code |
| `git diff --check` | Clean |
| `npm audit`, `pip-audit` | 0 known vulnerabilities |

UI keyboard coverage was limited to confirming Tab reaches a control. No screen-reader testing was done.

### Secret scan

gitleaks and trufflehog were not available, so this was a regex scan of the working tree, `frontend/dist` and all git history. No private keys or provider tokens were found. The database URL matches are `docker-compose.yml` environment-variable interpolation for its local container and fixture URLs in tests. `.env.example` holds placeholders. `.env`, `.env.demo` and `recon.db` are ignored.

### Read-only check of the populated `reconcile_demo`

Run in a `READ ONLY` transaction with the new code; the database was not reseeded.

| Item | Result |
|------|--------|
| Payments | 11 USD, 0 EUR |
| Invoices | 11 USD, 1 EUR |
| Unresolved | 11 |
| Allocations, decisions | 0, 0 |
| INJECTION-011 | INVALID, prohibited |
| INVESTIGATE-010 | NEEDS_REVIEW, prohibited |
| EXACT-001 | VERIFIED, eligible |
| REVERSAL-008 | 0 capacity, prohibited |
| REFUND-007 | $200 capacity (at or below net cash), prohibited at its stored $300 |
| Audit chain | Intact |
| **Actionable** | **3, not the 6 originally expected** |

The 3 are EXACT-001, PARTIAL-004 and SPLIT-005. REFUND-007 and REVERSAL-008 are stale gross proposals. OVERPAY-006 was stored without its reference evidence (F24), so it would have failed at approval on the old code too. Rerunning reconciliation there gives 5, as on the seeded database, but that writes to the database and was not done.

### Concurrency findings (F1 reclassified)

The original F1 claim was that two approvals of one proposal could double-allocate. That is **not exploitable on the original code**. The earlier eight-thread test passed there because the original already locked the payment row, and after that lock the verifier reports "payment is already allocated in the ledger". `uq_allocation_tenant_pay_inv` is a second barrier.

[`test_approval_race_postgres.py`](../tests/store/test_approval_race_postgres.py) forces each ordering with two sessions. Session A stops just before COMMIT holding its locks; session B starts, and A is released only once `pg_stat_activity` shows B waiting on a lock.

| Forced race | Original code | Current code |
|-------------|---------------|--------------|
| Approve, then a second approve (different key) | One allocation, one decision, one event. Loser gets a verifier error | Same state. Loser gets a clear conflict |
| Approve, then approve with the same key | One allocation. The retry gets an **error** instead of the first result | Retry returns the first result |
| Approve, then reject | **Confirmed defect:** $400 allocated, APPROVED and REJECTED decisions both recorded, proposal left REJECTED | Reject refused; proposal stays APPROVED |
| Reject, then approve | **Confirmed defect:** $400 allocated on a proposal already being rejected, both decisions recorded | Approve refused; no allocation |

Dropping the unique constraint in a throwaway database did not change the double-approval result on either version, so the verifier check, not the constraint, stops it on the original. Removing only the proposal lock from a scratch copy of the current code brought back both reject races and the same-key error, so the lock is required for those.

Classification: the approve-versus-reject races and the same-key retry are confirmed defects, fixed. Prevention of double allocation by the proposal lock is defensive hardening: the original was already safe there. All four tests passed in 20 consecutive repeat runs.

### Fresh demonstration database

`reconcile_demo_final` is a separate database on the same local PostgreSQL server, created from `template0`, migrated to `0005_d6_ai_investigations` and seeded by `POST /admin/demo/seed`. Connection settings are in the ignored `.env.demo.final`. A connection guard refused anything but this database during the build, apart from read-only checksums of `reconcile_demo`, which were identical before and after across all 18 tables.

Five proposals are actionable: EXACT-001, OVERPAY-006, PARTIAL-004, REFUND-007 (at its $200 net) and SPLIT-005. REVERSAL-008 is NO_MATCH with no capacity. INJECTION-011 (INVALID) and INVESTIGATE-010 (NEEDS_REVIEW) are blocked. AI investigations use the mock provider, not a live model. No approvals were made in this database; approval outcomes were exercised on a throwaway copy built the same way, which produced identical proposals.

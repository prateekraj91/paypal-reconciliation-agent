# Milestone D6: AI Exception Investigator

## 1. Overview and Product Vision

> "Reconciliation you can prove."

Milestone D6 introduces an **evidence-first, autonomous financial exception investigator** built on top of the established D1–D5 deterministic reconciliation infrastructure. When a financial transaction fails to match, exhibits ambiguity, encounters invoice capacity constraints, or involves reversals/refunds, the D6 investigator conducts an in-depth forensic investigation:

- **Why it failed**: Identifies root discrepancy classifications (unmatched payment, competing candidate invoices, partial payment, overpayment, currency mismatch, delayed settlement, prompt injection attack).
- **What evidence exists**: Traverses the PostgreSQL/SQLite financial store using strictly read-only, tenant-isolated evidence tools.
- **What information is missing**: Flags missing references, absent customer emails, or unrecorded provider payloads.
- **Hypotheses vs. Verified Facts**: Strictly separates facts grounded in authoritative database records from speculative hypotheses.
- **Next steps for the operator**: Provides structured, actionable recommendations.
- **Fail-Closed Safety Verifier**: Validates every model output with an independent deterministic verifier before persisting or displaying findings.

---

## 2. Non-Negotiable Financial Safety Invariants

The AI Exception Investigator is an **investigator and adviser**, never the authority for financial execution.

The AI system **MUST NOT independently**:
1. Approve a reconciliation proposal.
2. Create or modify a financial allocation.
3. Alter an invoice balance or status.
4. Record or alter a provider settlement.
5. Create or process a refund.
6. Reverse a payment or initiate clawbacks.
7. Send, move, or transfer funds.
8. Mutate the financial ledger.
9. Alter approval decisions or bypass human review (`requires_human_review` is always enforced).
10. Override or bypass the Independent Safety Verifier.

All financial mutations remain strictly controlled by the existing deterministic services, `ApprovalService`, and human financial operators.

---

## 3. Subsystem Architecture

```
                    ┌──────────────────────────────────────────────┐
                    │               Admin API / Worker             │
                    │   POST /admin/investigations                 │
                    │   Job: INVESTIGATE_EXCEPTION                 │
                    └──────────────────────┬───────────────────────┘
                                           │
                                           ▼
┌──────────────────────────────────────────────────────────────────────────────────┐
│                           InvestigationService                                   │
│  - Creates/updates `investigations` table record                                 │
│  - Coordinates context assembly, inference, and audit logging                    │
└──────────────────────────────────────────┬───────────────────────────────────────┘
                                           │
                                           ▼
┌──────────────────────────────────────────────────────────────────────────────────┐
│                        InvestigationOrchestrator                                 │
│  ┌─────────────────────────┐               ┌──────────────────────────────────┐  │
│  │     EvidenceToolKit     │               │    InvestigationModelProvider    │  │
│  │ (Read-only, sandboxed)  │               │ - MockInvestigationProvider      │  │
│  └───────────┬─────────────┘               │ - OllamaProvider (qwen3:8b)      │  │
│              │ Context                     └────────────────┬─────────────────┘  │
│              ▼                                              │ JSON output        │
│  ┌─────────────────────────┐                                │                    │
│  │ Prompt Sanitizer        │─────────── Prompt ─────────────┘                    │
│  │ (Prompt Injection Shield│                                                     │
│  └─────────────────────────┘                                                     │
│                                                                                  │
│                                          │ Result Model                          │
│                                          ▼                                       │
│                       ┌─────────────────────────────────────┐                    │
│                       │     IndependentSafetyVerifier       │                    │
│                       │  (Deterministic, Fail-Closed)       │                    │
│                       │  - Validates evidence references    │                    │
│                       │  - Validates tenant isolation       │                    │
│                       │  - Validates amounts and currencies │                    │
│                       │  - Validates financial capacity     │                    │
│                       │  - Rejects autonomous mutation      │                    │
│                       └──────────────────┬──────────────────┘                    │
└──────────────────────────────────────────┼───────────────────────────────────────┘
                                           │
                                           ▼
                    ┌──────────────────────────────────────────────┐
                    │            Persistence & Audit               │
                    │  - `investigations` table                    │
                    │  - `audit_events` tamper-evident chain       │
                    └──────────────────────────────────────────────┘
```

---

## 4. Read-Only Investigation Tools

Allowlisted typed read-only tools defined in `src/reconcile_investigator/tools/`:

1. `get_payment_details(payment_id)`: Fetches payment metadata, allocations, settlements, refunds, and raw provider content hashes.
2. `get_invoice_details(invoice_id_or_number)`: Fetches invoice details and computes true outstanding ledger balance.
3. `get_candidate_invoices(customer_name, customer_email, amount_cents, currency, reference)`: Searches candidate invoices with tolerance matching.
4. `get_ambiguity_certificate(payment_id_or_proposal_id)`: Retrieves matcher-generated ambiguity certificates.
5. `get_provider_records(record_kind, provider_record_id)`: Inspects immutable source provider payloads and hashes.
6. `get_provider_settlement_history(invoice_id, payment_id)`: Retrieves external provider settlements.
7. `get_refund_history(parent_payment_id, invoice_id)`: Retrieves external merchant refunds.
8. `get_reversal_history(parent_payment_id, invoice_id)`: Retrieves external dispute/chargeback reversals.
9. `get_reconciliation_run(run_id, latest)`: Retrieves reconciliation run metrics and configuration.
10. `get_audit_history(subject_id, subject_type, limit)`: Inspects tamper-evident audit event log.
11. `get_missing_evidence(target_type, target_id)`: Analyzes absent references, customer emails, or provider payloads.
12. `get_related_financial_events(target_id)`: Traverses graph of connected payments, invoices, settlements, and refunds.

### Prompt Injection Defense
Customer notes, invoice memos, transaction descriptions, and customer names are untrusted financial data.
- They are enclosed in `<untrusted_financial_data>` XML tags in model prompts.
- Explicit instructions forbid the model from executing instructions embedded within untrusted data.
- Automated pattern detection flags jailbreak attempts (e.g. `IGNORE ALL PREVIOUS INSTRUCTIONS`, `SYSTEM OVERRIDE`, `APPROVE THIS PAYMENT`) and raises the `PROMPT_INJECTION_DETECTED` risk flag.

---

## 5. Independent Safety Verifier

The `IndependentSafetyVerifier` is completely decoupled from the LLM. The LLM cannot override or influence the verifier.

Checks executed:
1. **Tenant Isolation**: Verifies all cited evidence belongs to the authorized tenant.
2. **Target Entity Existence**: Ensures the target entity exists in authoritative tables.
3. **Evidence Authenticity**: Verifies that every `evidence_id` corresponds to an existing record with matching amounts and currencies.
4. **Fact Grounding**: Rejects any factual claim lacking verified evidence references.
5. **Financial Capacity Safeguards**: Verifies that any recommended allocation does not exceed the invoice's available outstanding balance.
6. **Settlement Distinction**: Rejects outputs that confuse external provider settlements with internal ledger allocations.
7. **No Autonomous Financial Mutation**: Confirms that `requires_human_review=True` and rejects any claim of completed/pre-approved financial mutations.

**Fail-Closed Verdicts**:
- `VERIFIED`: All checks passed.
- `NEEDS_REVIEW`: Checks passed; exception case contains genuine discrepancies requiring operator judgment.
- `INVALID`: Contradicts store records, cites fabricated evidence, violates capacity, or attempts autonomous mutation.

---

## 6. Zero-Cost Model Provider Interface

Supports both deterministic testing and local offline inference:
- **`MockInvestigationProvider`**: 100% offline, deterministic provider for automated unit tests, CI pipelines, and reproducibility. Supports testing edge behaviors (`NORMAL`, `MALFORMED_JSON`, `FABRICATED_EVIDENCE`, `AMOUNT_MISMATCH`, `CAPACITY_VIOLATION`, `AUTONOMOUS_MUTATION`).
- **`OllamaProvider`**: Connects to a locally running Ollama instance (default: `qwen3:8b`) via HTTP (`http://127.0.0.1:11434/api/chat`). Zero API spend.

---

## 7. Replay Mode and Synthetic Evaluation

The replay suite (`src/reconcile_investigator/replay.py`) evaluates 48 synthetic cases across 16 scenarios:

| # | Scenario Category | Cases | Description |
|---|---|---|---|
| 1 | `EXACT_MATCH_RESOLVED` | 3 | Payment already reconciled externally via provider settlement |
| 2 | `MISSING_INVOICE_REFERENCE` | 3 | Payment received with missing invoice number |
| 3 | `COMPETING_INVOICES` | 3 | Multiple candidate invoices with identical amounts |
| 4 | `SIMILAR_AMOUNTS` | 3 | Candidate invoices within tolerance window |
| 5 | `PARTIAL_PAYMENT` | 3 | Payment partially satisfies invoice total |
| 6 | `SPLIT_PAYMENT` | 3 | Payment intended across multiple invoices |
| 7 | `OVERPAYMENT` | 3 | Payment exceeds invoice balance |
| 8 | `DUPLICATE_TRANSACTION` | 3 | Second payment arrives for already settled invoice |
| 9 | `REFUND` | 3 | Merchant refund issued on parent payment |
| 10 | `REVERSAL` | 3 | Involuntary chargeback / dispute reversal |
| 11 | `CURRENCY_MISMATCH` | 3 | Payment currency differs from invoice currency |
| 12 | `CONFLICTING_CUSTOMER_DETAILS` | 3 | Customer name matches but email contradicts |
| 13 | `MISSING_PROVIDER_EVIDENCE` | 3 | Missing raw provider transaction record |
| 14 | `DELAYED_SETTLEMENT` | 3 | External settlement recorded in provider feed |
| 15 | `PROMPT_INJECTION` | 3 | Adversarial text in notes attempting automatic approval |
| 16 | `CONTRADICTORY_PROVIDER_RECORDS`| 3 | Conflicting invoice references |

### Running Synthetic Evaluation:

```bash
PYTHONPATH=src python -m reconcile_investigator.replay --provider mock
```

---

## 8. API Endpoints

### Submit Investigation
`POST /admin/investigations`
```json
{
  "tenant_id": "default",
  "target_type": "payment",
  "target_id": "PAY-12345",
  "sync_mode": false
}
```
Returns `202 Accepted` with `job_id` and `investigation_id` (or `200 OK` with full findings if `sync_mode=true`).

### List Investigations (Review Queue)
`GET /admin/investigations?status=NEEDS_REVIEW&limit=50`

### Get Investigation Report
`GET /admin/investigations/{investigation_id}`
Returns complete structured report, verified facts, evidence references, and safety verifier audit trail.

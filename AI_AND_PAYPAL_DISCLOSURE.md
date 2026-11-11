# AI and PayPal Tools Disclosure

This document details the usage of artificial intelligence technologies, PayPal APIs, and synthetic data in the **PayPal Reconciliation Agent** project, developed for PayPal's *Build What's Next with PayPal and AI* Hackathon.

---

## 1. PayPal Sandbox vs. Production Functionality

- **Strict Sandbox Environment**: All PayPal operations were conducted entirely within the **PayPal Sandbox** environment (`api-m.sandbox.paypal.com`).
- **No Production Access or Funds**: No production endpoints, live PayPal accounts, or real funds were utilized.
- **Zero Real Customer Data**: No real personally identifiable information (PII), live credit cards, or actual financial records were ingested or processed. All merchant identities and transactions are either PayPal Sandbox accounts or synthetically generated test scenarios.

---

## 2. Actual API Integrations vs. Simulated Components

### Actual PayPal API Integrations
The application integrates with the following live PayPal Sandbox endpoints using standard REST and OAuth 2.0 protocols:

| PayPal API Component | Endpoint / Service | Implementation Status & Role |
|---|---|---|
| **OAuth 2.0 Authentication** | `/v1/oauth2/token` | Implemented. Handles automated token acquisition, expiration tracking, and bearer token caching for API interactions. |
| **Invoicing API v2** | `/v2/invoicing/invoices` | Implemented. Used to fetch invoice obligations, verify payment status, and track invoice balance states. Also used by test seeding tools to generate Sandbox test invoices. |
| **Transaction Search API** | `/v1/reporting/transactions` | Implemented. Ingests settlement and transaction events from PayPal reporting. Merges reporting views with invoicing records by PayPal transaction ID. |
| **Webhook Signature Verification** | `/v1/notifications/verify-webhook-signature` | Implemented. Cryptographically validates PayPal webhook authenticity using PayPal's public certificates and signature headers. |
| **Sandbox Buyer/Merchant Flow** | PayPal Sandbox Web Portal | Verified. Issued real Sandbox invoice `RECON-T-901` ($42.50 USD) and paid via a Sandbox personal buyer account. Ingested through both Invoicing and Transaction Search APIs into verified transaction `89H37763BV8635251`. |

### Simulated & Deliberately Excluded Components
- **Manual Sync vs. Live Webhook Delivery**: While the webhook receiver and signature verification logic are fully implemented and unit-tested, live automated webhook ingress requires persistent public HTTPS endpoints. In the local demonstration environment, ingestion is driven by an on-demand, idempotent manual sync worker (`MANUAL_SYNC`) connecting directly to PayPal Sandbox APIs.
- **Orders API v2**: The `POST /v2/checkout/orders` endpoint was evaluated during development but returned HTTP 403 Forbidden for our Sandbox application credentials. Consequently, order capture flows were excluded in favor of Invoicing v2 and Transaction Search flows.
- **Multi-Tenant Scoping (`test_tenant` vs. `demo_merchant`)**:
  - `test_tenant`: Contains **real PayPal Sandbox data** ingested directly from PayPal's Sandbox APIs.
  - `demo_merchant`: Contains **synthetic stress-test scenarios** (exact matches, partial splits, overpayments, refunds, reversals, currency mismatches, and adversarial prompt injections) designed to rigorously evaluate matching and security boundaries. These scenarios are strictly isolated and never represented as data fetched from PayPal.

---

## 3. Local Ollama / Qwen3 Usage vs. External Models

- **Zero Cloud LLM Spend / Zero Hosted Inference**: All AI-assisted investigations in the application runtime are executed locally using **Qwen3:8b** running inside an **Ollama** instance (`http://127.0.0.1:11434`).
- **Local Processing & Data Boundary**: Financial data is processed locally by Qwen3:8b through Ollama. No payment data or memos are sent to an external LLM provider. The application separately communicates with PayPal Sandbox APIs for payment and invoice integration.
- **Deterministic Mock Provider**: For automated CI/CD pipelines and unit testing (`pytest`), a deterministic mock provider (`MODEL_PROVIDER=mock`) is provided to ensure reproducible, fast, and offline test execution without requiring a running Ollama daemon.
- **AI-Assisted Development Disclosure**: Claude Code (Anthropic) was utilized during the development lifecycle as an interactive pair programming tool for architecture auditing, writing unit tests, drafting documentation, and debugging code. All architectural choices, code changes, and test assertions were reviewed and validated by human developers.

---

## 4. AI-Generated Investigation vs. Deterministic Financial Verification

The system maintains a strict mathematical separation between probabilistic AI reasoning and deterministic financial accounting:

```
┌────────────────────────────────────────────────────────┐
│               Incoming PayPal Payment                  │
└──────────────────────────┬─────────────────────────────┘
                           ▼
┌────────────────────────────────────────────────────────┐
│            Deterministic Matching Engine               │
│   (Exact, Partial, Split, Refund, Reversal Rules)      │
└────────────┬─────────────────────────────┬─────────────┘
             │ Matched                     │ Unmatched / Ambiguous
             ▼                             ▼
┌─────────────────────────┐   ┌──────────────────────────┐
│  Independent Verifier   │   │  Local AI Investigator   │
│  (DB Ground Truth Check)│   │   (Qwen3:8b via Ollama)  │
└────────────┬────────────┘   └────────────┬─────────────┘
             │                             │
             │                             ▼
             │                ┌──────────────────────────┐
             │                │  Safety Verifier Gate    │
             │                │  (Checks Cited DB Facts) │
             │                └────────────┬─────────────┘
             │ Passes                      │ Accepts as Advisory
             ▼                             ▼
┌────────────────────────────────────────────────────────┐
│                  Human Review Queue                    │
│    (Locked Approval Gate · Prompt-Injection Shield)    │
└──────────────────────────┬─────────────────────────────┘
                           │ Authorized Operator Approval
                           ▼
┌────────────────────────────────────────────────────────┐
│   Atomic PostgreSQL Mutation & Hash-Chained Audit Log  │
└────────────────────────────────────────────────────────┘
```

- **The AI Model Cannot Mutate Financial Records**: The AI investigator has zero write permissions to the database or financial ledger. It cannot approve proposals, allocate funds, trigger refunds, or mark invoices as paid.
- **Advisory Investigation Only**: The model operates strictly in an explanatory, advisory capacity on edge cases that the deterministic matcher cannot resolve with high confidence (e.g. cross-currency transactions, customer name discrepancies).
- **Independent Safety Verifier**: An independent, deterministic safety verifier evaluates every output produced by the LLM. The model is required to cite verified internal database evidence identifiers (`ev_pay_*`, `ev_inv_*`). If the model hallucinates evidence, makes ungrounded claims, or fails to prove its assertions, the verifier rejects the proposal (`INVALID`) and fails closed.
- **Deterministic Prompt-Injection Defense**: Adversarial prompt injections hidden inside payment remittance fields (such as `PAY-DEMO-INJECTION-011`) are detected by deterministic boundary guards (`PROMPT_INJECTION_SUSPECT`). The system fails closed with an inverted, high-contrast security warning and disables all approval actions. The AI model is never allowed to override this security barrier.

### Quantitative Evaluation Results: Actual LLM vs. Deterministic Mock Provider

The exception investigation capability was evaluated against benchmark exception suites using both the actual local LLM (`qwen3:8b` via Ollama) and the rule-based mock investigator (`mock-investigator-v1`). The results from the deterministic mock provider and the actual generative LLM must be kept clearly distinguished: the mock provider is a deterministic test fixture used for offline CI/CD regression testing, whereas the Ollama evaluation measures real generative inference.

| Evaluation Metric | Actual Local LLM (`qwen3:8b` via Ollama) | Deterministic Mock Provider (`mock-investigator-v1`) | Distinction & Invariants |
|---|---|---|---|
| **Evaluated Cases** | **4 cases** (representative exception sample) | **48 cases** (exhaustive synthetic test suite) | Real hardware inference vs. rule-based test harness |
| **Classification Accuracy** | **50%** (2 of 4 cases classified correctly) | **100%** (48 of 48 cases classified correctly) | Actual LLM misclassified external settlement as overpayment |
| **Evidence Validity** | **75%** (3 of 4 cases cited valid records) | **100%** (48 of 48 cases cited valid records) | Actual LLM cited an ungrounded invoice ID in 1 case |
| **Unsupported Claim Rate** | **25%** (1 of 4 cases had an ungrounded claim) | **0%** (0 of 48 cases had ungrounded claims) | Caught and rejected by independent safety verifier |
| **Prompt-Injection Resistance** | **100%** (1 of 1 attack attempt resisted) | **100%** (1 of 1 attack attempt resisted) | Refused to follow adversarial remittance instructions |
| **Unauthorized Financial Actions** | **0** (zero mutations permitted or executed) | **0** (zero mutations permitted or executed) | **Core Invariant**: Model has zero ledger write permissions |
| **Verifier Pass Rate** | **75%** (3 accepted as `NEEDS_REVIEW`, 1 rejected as `INVALID`) | **100%** (48 accepted as `NEEDS_REVIEW`, 0 `INVALID`) | Verifier failed closed on the ungrounded LLM case |

**Key Distinctions**:
- **Deterministic Mock Provider**: Serves solely as a deterministic test harness for unit tests (`pytest`) and offline development. It achieves 100% across metrics because its outputs are generated by deterministic rules matching known fixture scenarios. It does not measure generative AI reasoning or model accuracy.
- **Actual Qwen3:8b Local LLM**: Reflects real generative performance on local hardware. The model produced an unsupported claim on 1 of the 4 representative test cases (25% unsupported claim rate), resulting in 50% classification accuracy and 75% evidence validity. Crucially, the independent safety verifier operated as designed: it detected the ungrounded citation, marked the proposal `INVALID`, and failed closed—preventing an invalid recommendation from being presented as verified facts.

---

## 5. Human Approval Responsibilities & Financial Authority

- **Human-in-the-Loop Required**: Ledger mutations (allocations and reconciliations) can **only** be finalized by an explicit action from an authorized human operator.
- **Locked Approval Controls**: If a payment has unresolved flags, failed verifications, or prompt-injection markers, the dashboard's "Approve" button is visually disabled, styled with a dashed lock border, and rejects interaction.
- **Real-Time Atomic Re-Verification**: When a human operator clicks "Approve", the backend re-executes all capacity and verification checks inside an isolated PostgreSQL transaction under `SELECT FOR UPDATE` row locks. If invoice balance or payment availability has changed in the intervening time, the approval is safely aborted.
- **Tamper-Evident Audit Trail**: Every human operator action (approval, rejection, tenant switch) is written to a tamper-evident, SHA-256 hash-chained audit trail containing timestamp, actor ID, subject ID, and payload hash. The cryptographic hash chaining makes tampering detectable upon verification, but does not claim to make the database physically immutable.


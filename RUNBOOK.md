# Operational Runbook: PayPal Reconciliation Agent

This runbook provides verified, step-by-step instructions to configure, run, test, and demonstrate the **PayPal Reconciliation Agent** locally.

---

## 1. System Requirements & Prerequisites

Ensure the following tools are installed on your workstation (macOS or Linux):

- **Python**: Version 3.11 or higher
- **PostgreSQL**: Version 16 or higher (running on local port 5432)
- **Node.js**: Version 18 or higher (with `npm`)
- **Ollama**: (Optional for local AI inference) with the `qwen3:8b` model pulled

---

## 2. Environment Configuration

1. Clone the repository and navigate to the project root:
   ```bash
   git clone https://github.com/prateekraj91/paypal-reconciliation-agent.git
   cd paypal-reconciliation-agent
   ```

2. Initialize your local environment file from the template:
   ```bash
   cp .env.example .env
   ```

3. Review the environment variables in `.env`:
   - `RECONCILE_DB_URL`: PostgreSQL connection string (e.g. `postgresql+psycopg://postgres:postgres@localhost:5432/reconcile_test`).
   - `ADMIN_API_KEY`: Secret API token used to authenticate to the FastAPI backend and operator dashboard (default placeholder: set a secure string).
   - `PAYPAL_CLIENT_ID` / `PAYPAL_CLIENT_SECRET`: PayPal Sandbox credentials (optional for synthetic replay, required for live Sandbox sync).
   - `PAYPAL_BASE_URL`: Defaults to `https://api-m.sandbox.paypal.com`.
   - `PAYPAL_ENVIRONMENT`: Defaults to `sandbox`.
   - `MODEL_PROVIDER`: Set to `mock` for deterministic test suites, or `ollama` for local AI investigations.
   - `OLLAMA_BASE_URL`: `http://127.0.0.1:11434`.
   - `OLLAMA_MODEL`: `qwen3:8b`.

> [!IMPORTANT]
> Never commit `.env` or production credentials to source control. `.env` is ignored by `.gitignore`.

---

## 3. Database Initialization

1. Start PostgreSQL (e.g. on macOS via Homebrew):
   ```bash
   brew services start postgresql@16
   ```

2. Create the reconciliation database (if not already created):
   ```bash
   createdb reconcile_test
   ```

3. Create and activate a Python virtual environment:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   ```

4. Install Python dependencies:
   ```bash
   pip install --upgrade pip setuptools wheel
   pip install -r requirements.txt
   pip install -e ".[dev,store,api]"
   ```

5. Run database migrations to create the schema:
   ```bash
   alembic upgrade head
   ```

---

## 4. Running the Backend Service

Start the FastAPI application on local port 8000:

```bash
PYTHONPATH=src uvicorn reconcile_api.main:app --host 127.0.0.1 --port 8000 --reload
```

### Health Verification

In another terminal, verify service liveness and readiness:

```bash
# Liveness probe
curl -s http://127.0.0.1:8000/health
# Expected: {"status":"ok","service":"paypal-reconciliation-agent"}

# Readiness probe (verifies database connectivity)
curl -s http://127.0.0.1:8000/ready
# Expected: {"status":"ready","database":"connected"}
```

---

## 5. Running the Frontend Dashboard

1. Navigate to the `frontend` directory and install dependencies:
   ```bash
   cd frontend
   npm install
   ```

2. Start the Vite development server:
   ```bash
   npm run dev -- --host 127.0.0.1
   ```

3. Open your browser and navigate to:
   ```
   http://127.0.0.1:5173
   ```

4. Authenticate by entering the `ADMIN_API_KEY` specified in your `.env` file.

---

## 6. (Optional) Local Ollama LLM Setup

To run local AI investigations with zero API spend and complete data privacy:

1. Launch the Ollama daemon:
   ```bash
   ollama serve
   ```

2. Pull the Qwen3:8b model (in a separate terminal):
   ```bash
   ollama pull qwen3:8b
   ```

3. Verify the model is available locally:
   ```bash
   curl -s http://127.0.0.1:11434/api/tags | grep qwen3:8b
   ```

4. Set `MODEL_PROVIDER=ollama` in your `.env` before running live investigations.

---

## 7. Running the Test Suite

### Backend Test Suite (pytest)
Runs 1,078 tests across unit, integration, and security layers:
```bash
PYTHONPATH=src pytest
```
*Expected result: 1,042 passed, 36 skipped (external service skips).*

### Frontend Test Suite (Node.js test runner)
Tests monochrome styling, state badge borders, and security protections:
```bash
cd frontend && npm test
```
*Expected result: 33 passed, 0 failed.*

### TypeScript Check & Production Build
```bash
cd frontend && npm run build
```
*Expected result: `tsc -b` and `vite build` complete with zero errors.*

---

## 8. Seeding Demo Scenarios & Tenant Architecture

The system provides dual-tenant scoping:

| Tenant ID | Display Name | Purpose & Source Data |
|---|---|---|
| `test_tenant` | **Test Tenant** | **Real PayPal Sandbox data** ingested from PayPal Invoicing v2 and Transaction Search. |
| `demo_merchant` | **Demo Merchant Corp (Global)** | **Synthetic stress-testing scenarios** covering edge cases, partial splits, currency mismatches, and prompt injections. |

### Seeding Demo Data

You can seed or reseed the rich demo dataset either via the dashboard or using the REST API:

- **Via Dashboard**: Click **Reseed Demo Data** at the bottom of the left sidebar.
- **Via API**:
  ```bash
  curl -X POST http://127.0.0.1:8000/admin/demo/seed \
    -H "Authorization: Bearer <YOUR_ADMIN_API_KEY>" \
    -H "Content-Type: application/json" \
    -d '{"tenant_id": "demo_merchant", "force": true}'
  ```

---

## 9. Operator Demo Walkthrough

Follow this sequence to demonstrate all core capabilities in under three minutes:

### Beat 1: Overview & Financial Health
1. Open `http://127.0.0.1:5173` and authenticate with `ADMIN_API_KEY`.
2. Notice the initial active tenant is **Demo Merchant Corp (Global)**.
3. Observe the metric cards:
   - **Reconciliation Rate**: Current matching percentage.
   - **Open Exceptions**: Unallocated or ambiguous items needing investigation.
   - **Review Queue**: Actionable proposals waiting for human approval.
   - **Currency Ledger Balances**: Integer minor-unit balances without cross-currency conflation.

### Beat 2: Real PayPal Sandbox Data (`test_tenant`)
1. Switch the **Active Tenant** dropdown in the sidebar to **Test Tenant**.
2. Notice the top bar updates to `tenant: test_tenant`.
3. Click **Invoices & Ledger**:
   - Inspect invoice `RECON-T-901` ($42.50 USD), marked `PAID ($0.00)` and `RECONCILED`.
4. Click **Transactions**:
   - Locate PayPal Sandbox transaction `89H37763BV8635251` ($42.50 USD, John Doe).
   - Click the row to open the Transaction Detail drawer.
   - Point out that this payment was paid in PayPal Sandbox by a Sandbox buyer, ingested across both Invoicing v2 and Reporting APIs, and matched deterministically to `RECON-T-901`.
5. Switch the **Active Tenant** back to **Demo Merchant Corp (Global)**.

### Beat 3: Human Review Queue & Evidence Drill-Down
1. Click **Review Queue** in the sidebar.
2. View pending proposals:
   - Point out that every row displays candidate invoices, match reasons, and a dedicated **Evidence** button.
   - Show that matching is evidence-first, never allocating on amount alone.
3. Click **Evidence** on proposal `prop_125904757dea450c806c6b3b8f005be2` (`PAY-DEMO-INVESTIGATE-010`):
   - Notice the detailed corroborating signals, candidate invoice evaluation, and independent verifier status.

### Beat 4: Adversarial Prompt-Injection Defense (`PAY-DEMO-INJECTION-011`)
1. In the Review Queue, observe the top row: `PAY-DEMO-INJECTION-011` (payer: Hank Scorpio, $500.00 USD).
2. Point out the classification badge: `PROMPT_INJECTION_SUSPECT`.
3. Notice that the **Approve** button has a dashed outline, a lock icon, and is completely disabled.
4. Click **Evidence**:
   - Notice the prominent inverted security block:  
     `APPROVAL STRICTLY PROHIBITED (FAIL-CLOSED) — Adversarial prompt injection tokens detected in payment remittance.`
   - Explain: payment memos are treated strictly as data. An attacker's prompt injection instructions cannot force an approval.

### Beat 5: Local AI Exception Investigation (Qwen3:8b via Ollama)
1. Click **AI Investigations** in the sidebar.
2. Open investigation `invg_640dddf5fc4f4e1ea5a518c73b66dce0` (`CURRENCY_MISMATCH`, local `qwen3:8b`):
   - Show the **Verified Facts** section: every fact references a cited database evidence ID (`ev_pay_*`, `ev_inv_*`).
   - Show the advisory section: **Model-Raised Flags (Advisory)**.
   - Scroll to the bottom: the **Approve Proposed Resolution** button is locked with the notice:  
     *"Approval held: model-raised flags need a human check"*.
   - Explain: the AI model cannot execute ledger changes; it can only advise, and the safety verifier enforces grounded evidence.

### Beat 6: Atomic Human Approval
1. Return to the **Review Queue**.
2. Locate a valid, verified proposal such as `PAY-DEMO-EXACT-001` or `PAY-DEMO-OVERPAY-006`.
3. Click **Approve** and confirm the dialog:
   - The backend acquires row-level locks, re-verifies capacity and invariants, writes the allocation, and updates the invoice balance atomically.

### Beat 7: Cryptographic Audit Trail
1. Click **Audit Trail** in the sidebar.
2. View the chronological log of system actions:
   - Each event includes an event type, actor, subject, timestamp, and SHA-256 hash.
   - Click **Verify Integrity**: the backend validates the entire hash chain from genesis to head, confirming zero tampering.

---

## 10. Monochrome UI Principles (PR #17)

The frontend uses a strictly monochrome design system:
- **Verified / Reconciled**: Thin solid borders with checkmark (`✓`).
- **Needs Review / Held**: Double borders with clock or exclamation.
- **Model-Raised Flags (Advisory)**: Dashed borders with alert symbol (`⚠`).
- **Invalid / Fail-Closed**: Inverted high-contrast blocks with heavy solid borders and `✕`.
- **Disabled Buttons**: Dashed outlines with lock icons.

This ensures critical financial safety states are recognizable by shape, weight, and contrast, independent of color perception.


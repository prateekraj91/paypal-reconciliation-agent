# D4: Verified Webhooks, Automatic Ingestion, Background Processing & Deployment

## 1. Architecture Overview

The D4 architecture delivers real-time, asynchronous, provable PayPal payment reconciliation while preserving existing financial invariants and strict separation of concerns:

```
[ PayPal Sandbox ] 
       │ (Signed Webhook Delivery)
       ▼
[ FastAPI App: POST /webhooks/paypal ]
       │ 1. Validate transmission headers
       │ 2. Idempotent receipt persistence (webhook_inbox)
       │ 3. Postback signature verification (/v1/notifications/verify-webhook-signature)
       │ 4. Atomic job creation (jobs table)
       │ 5. Return HTTP 202 Accepted
       ▼
[ PostgreSQL / SQLite Durable Job Queue ]
       │
       │ Row-level atomic claim (SELECT FOR UPDATE SKIP LOCKED)
       ▼
[ Background Worker: python -m reconcile_worker ]
       │ 1. Validate & heartbeat lease
       │ 2. Fetch authoritative provider records (PayPal REST Invoicing & Transactions)
       │ 3. Payment-before-invoice deferral check
       │ 4. Normalise to canonical models (standard-library-only core)
       │ 5. Ingest into persistence store & execute DeterministicMatcher
       │ 6. Run independent Verifier
       │ 7. Persist MatchProposals and audit events (NEVER auto-approved)
       │ 8. Mark job COMPLETED and webhook receipt PROCESSED
```

### Layered Separation
- **`reconcile/`**: Pure deterministic reconciliation core. Standard library only. Storage-free.
- **`reconcile_store/`**: SQLAlchemy 2.x persistence, financial ledger, approval service, tamper-evident hash-chained audit system, database-backed background job queue, and webhook inbox.
- **`reconcile_api/`**: FastAPI HTTP application providing public health, readiness, PayPal webhook ingestion, and authenticated administrative endpoints.
- **`reconcile_worker/`**: Standalone background worker process claiming queued jobs with atomic row locking, lease expiry recovery, and exponential retry.

---

## 2. Webhook Signature Verification Flow

PayPal webhook authentication uses PayPal's official postback verification API (`POST /v1/notifications/verify-webhook-signature`) rather than guessed local cryptography:

1. **Incoming Request**: Webhook request reaches `POST /webhooks/paypal`.
2. **Body Size Enforcement**: Payload size is bounded by `MAX_REQUEST_BODY_BYTES` (default 1MB). Oversized requests are rejected with `HTTP 413 Content Too Large`.
3. **Header Validation**: All mandatory transmission headers must be present:
   - `PAYPAL-TRANSMISSION-ID`
   - `PAYPAL-TRANSMISSION-TIME`
   - `PAYPAL-CERT-URL`
   - `PAYPAL-AUTH-ALGO`
   - `PAYPAL-TRANSMISSION-SIG`
   If any header is missing, the request is rejected with `HTTP 400 Bad Request`.
4. **Idempotent Receipt Recording**: The raw event payload is preserved verbatim in the `webhook_inbox` table with `verification_status="VERIFICATION_PENDING"`. Duplicate event IDs for the same tenant and environment return `HTTP 200 OK` ("already received") without redundant processing.
5. **Postback Verification Call**: A structured postback is dispatched to PayPal:
   ```json
   {
     "transmission_id": "...",
     "transmission_time": "...",
     "cert_url": "...",
     "auth_algo": "...",
     "transmission_sig": "...",
     "webhook_id": "<Configured Webhook ID>",
     "webhook_event": { ...raw event JSON... }
   }
   ```
6. **Classification of Outcomes**:
   - `verification_status == "SUCCESS"`: Webhook receipt updated to `VERIFIED`. Processing job enqueued. Returns `HTTP 202 Accepted`.
   - `verification_status == "FAILURE"` or HTTP 400: Webhook receipt updated to `REJECTED`. No job created. Returns `HTTP 400 Bad Request`.
   - Timeout or HTTP 5xx: Receipt remains `VERIFICATION_PENDING`. Returns `HTTP 503 Service Unavailable`, prompting PayPal to safely retry delivery.

---

## 3. Webhook Inbox & Event Lifecycle

Receipts are tracked through explicit, non-downgradable state transitions:

```
[ Inbound POST ]
       │
       ▼
   RECEIVED / VERIFICATION_PENDING
       │
       ├──(Verification Succeeded)──► VERIFIED ──► QUEUED ──► PROCESSING ──► PROCESSED
       │                                                          │
       │                                                          └──(Error)──► RETRY / FAILED
       └──(Verification Failed)────► REJECTED (Terminated)
```

### Invariants Enforced:
- Once an event is marked `VERIFIED`, it cannot be transitioned to `REJECTED`.
- Once an event is marked `REJECTED`, an ordinary duplicate delivery cannot elevate it to `VERIFIED`.
- Raw webhook payloads are stored immutably as source evidence.

---

## 4. Background Job Queue & Worker Lifecycle

The job queue is database-backed (`jobs` table), avoiding external operational dependencies like Redis, RabbitMQ, or Celery.

### Atomic Claiming
- **PostgreSQL**: Claiming uses `SELECT ... FOR UPDATE SKIP LOCKED` on eligible rows (`status IN ('QUEUED', 'RETRY') AND available_at <= now() OR (status = 'RUNNING' AND lease_expires_at <= now())`). Multiple concurrent workers will never claim the same job.
- **SQLite**: Claims use transactional serialization and atomic status transition.

### Lease Expiration & Crash Recovery
- When a worker claims a job, it acquires a lease (`lease_expires_at = now() + lease_duration`).
- If a worker crashes or drops connectivity, the lease expires.
- Any subsequent worker will reclaim the job once `lease_expires_at <= now()`.
- Financial side effects remain idempotent: all database upserts and proposals are idempotent and deduplicated.

### Error Handling & Backoff
- **Retryable failures** (network timeouts, PayPal rate limits, invoice-not-yet-available): Scheduled for retry with exponential backoff (`available_at = now() + min(5 * 2^(attempt-1), 300)`) and status `RETRY`.
- **Permanent failures** (malformed JSON, unsupported event): Marked `FAILED` with sanitized error message.
- **Exhausted retries**: When `attempt_count >= max_attempts` (default: 5), status transitions to `FAILED`.

---

## 5. Payment Arriving Before Its Invoice

In real-world billing, a customer may settle a payment before the merchant system has finished synchronising the corresponding invoice record locally.

### Resolution Protocol:
1. When a payment event arrives (e.g., `PAYMENT.CAPTURE.COMPLETED`), the worker extracts the invoice reference (e.g. from `custom_id` or `invoice_id`).
2. The worker checks if the invoice exists in the local database.
3. If not found locally, the worker attempts an authoritative fetch from PayPal (`client.get_raw_invoice(invoice_ref)`).
4. If PayPal returns 404 (invoice not yet issued or synchronised):
   - The worker **ingests and retains the payment** in the database as an open payment (ensuring no financial evidence is lost).
   - Raises `InvoiceNotYetAvailableError`.
   - The job is set to `RETRY` with a short delay (10 seconds).
   - **Crucially: No false match or premature proposal is created.**
5. Once the invoice arrives (via subsequent webhook or manual sync), the retrying worker finds the invoice, executes deterministic matching, and persists the verified `MatchProposal`.

---

## 6. Financial Ledger & Verifier Invariants

- **Zero Automatic Approval**: Inbound webhooks generate `MatchProposal` records only. Approval and ledger allocation require explicit human decision.
- **No Write-Back to PayPal**: The system never updates or mutates invoice or payment records on PayPal.
- **Tamper-Evident Audit Chain**: Every proposal, payment ingestion, and run records a SHA-256 hash-chained event in `audit_events`.

---

## 7. Configuration & Environment Variables

| Variable | Description | Default / Example |
|---|---|---|
| `RECONCILE_DB_URL` | PostgreSQL or SQLite database connection URL | `postgresql+psycopg://postgres:postgres@localhost:5432/reconcile_test` |
| `PAYPAL_ENVIRONMENT` | PayPal environment (`sandbox` or `live`) | `sandbox` |
| `PAYPAL_CLIENT_ID` | PayPal OAuth REST Client ID | `your_client_id` |
| `PAYPAL_CLIENT_SECRET` | PayPal OAuth REST Client Secret | `your_client_secret` |
| `PAYPAL_BASE_URL` | PayPal REST API base URL | `https://api-m.sandbox.paypal.com` |
| `PAYPAL_WEBHOOK_ID` | Webhook identifier registered in PayPal Sandbox | `WH-SANDBOX-ID` |
| `ADMIN_API_KEY` | Bearer token for protecting administrative endpoints | Secure random token |
| `DEFAULT_TENANT_ID` | Tenant scoping for un-scoped requests | `default` |
| `MAX_REQUEST_BODY_BYTES`| Maximum accepted request body size | `1048576` (1MB) |

---

## 8. Operational Commands

### Local Development Setup
```bash
# 1. Install dependencies with store and api extras
pip install -e ".[store,api,dev]"

# 2. Run database migrations to head
alembic upgrade head

# 3. Start the FastAPI API server
uvicorn reconcile_api.main:app --host 0.0.0.0 --port 8000 --reload

# 4. Start the background worker process (in a separate terminal)
python -m reconcile_worker

# 5. Run single-pass worker execution (for cron or batch runs)
python -m reconcile_worker --once
```

### Docker Compose
```bash
# Start PostgreSQL, API, and background worker
docker compose up -d

# Check health and logs
docker compose ps
docker compose logs -f api
docker compose logs -f worker
```

### Running Test Suite
```bash
# Run all tests on SQLite and PostgreSQL
TEST_POSTGRESQL_URL="postgresql+psycopg://@127.0.0.1:5432/reconcile_test" pytest tests/

# Run API and Webhook tests specifically
pytest tests/test_d4_api.py tests/store/test_d4_queue_and_inbox.py tests/store/test_d4_worker.py tests/store/test_d4_workflow_integration.py
```

### Triggering Manual Sync
```bash
curl -X POST http://localhost:8000/admin/sync \
  -H "Authorization: Bearer <ADMIN_API_KEY>" \
  -H "Content-Type: application/json" \
  -d '{"page_size": 100}'
```

---

## 9. Render Deployment Guide

The repository includes a complete `render.yaml` Blueprint definition.

### Step-by-Step Dashboard Deployment:
1. Log in to [Render Dashboard](https://dashboard.render.com).
2. Click **New +** > **Blueprint**.
3. Connect the repository (`paypal-reconciliation-agent`).
4. Select the branch `feat/d4-webhooks-ingestion-deploy`.
5. Render will automatically detect `render.yaml` and configure:
   - `reconcile-db`: Managed PostgreSQL database.
   - `reconcile-api`: FastAPI web service (`alembic upgrade head` pre-deploy, `uvicorn` start).
   - `reconcile-worker`: Background worker service (`python -m reconcile_worker`).
6. Set the secret environment variables in the Render Dashboard:
   - `PAYPAL_CLIENT_ID`
   - `PAYPAL_CLIENT_SECRET`
   - `PAYPAL_WEBHOOK_ID`
7. Click **Apply**.
8. Once deployed, copy the public URL (e.g. `https://reconcile-api.onrender.com`).
9. In PayPal Developer Dashboard (Sandbox Apps):
   - Add Webhook URL: `https://reconcile-api.onrender.com/webhooks/paypal`.
   - Subscribe to events: `Invoicing` and `Payment capture completed`.
   - Copy the Webhook ID and update `PAYPAL_WEBHOOK_ID` on Render.

---

## 10. Known Limitations & Next Steps

- **D4 Scope**: D4 establishes the verified ingestion, durable queue, background worker, and deployment infrastructure. UI dashboards, exceptions queues, and AI investigation agents belong to milestones D5–D8.
- **Simulator Events vs Real Webhooks**: PayPal's developer simulator sends dummy mock headers that fail official cryptographic signature verification by design. Real end-to-end webhook validation requires generating real Sandbox checkout / invoice transactions against the deployed endpoint.

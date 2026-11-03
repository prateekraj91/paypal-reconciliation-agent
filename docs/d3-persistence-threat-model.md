# Threat Model: D3 Persistence, Financial Ledger, and Audit Layer

## 1. Overview and Scope

This document analyzes security risks, threat vectors, and architectural mitigations for the D3 persistence layer, financial ledger, and hash-chained audit system of the PayPal Reconciliation Agent.

**Important Disclaimer:** This document reflects an engineering threat analysis for D3 development. It does not claim regulatory certification (SOC 1/2, PCI-DSS, or ISO 27001) or unconstrained production readiness.

---

## 2. Trust Boundaries and Assets

### 2.1 Critical Assets
1. **Application-Owned Financial Ledger (`allocations`)**: Represents authoritative financial truth regarding which payment funds have been legally/operationally allocated to which invoices.
2. **Canonical Financial Records (`invoices`, `payments`)**: Core financial identity and monetary balances.
3. **Tamper-Evident Audit Chain (`audit_events`)**: Cryptographic record of consequential business decisions.
4. **Original Ingestion Evidence (`provider_records`)**: Immutable raw payloads providing legal provenance.
5. **Provider Authentication References (`connections`)**: Pointers to external secret management vaults.

### 2.2 Untrusted Boundaries
- **Billing/Payment Ingestion (PayPal APIs / Webhooks)**: Raw JSON payloads can be malformed, contradictory, duplicated, or delayed.
- **AI/ML Proposers & Matchers**: Model outputs are considered untrusted suggestions. They can hallucinate, omit, or misalign allocations.
- **Tenant Contexts**: Multi-tenant operational environment where tenants must never access, allocate, or infer records belonging to other tenants.

---

## 3. Threat Vectors & Mitigations

### T1: Credential and Secret Exposure
- **Threat**: PayPal OAuth client secrets, refresh tokens, or bearer tokens leaked into database records, logs, or error traces.
- **Mitigations**:
  - `connections.credential_reference` only stores symbolic pointers (e.g., `vault:secret/tenant_a/paypal_live`), never raw secrets or bearer tokens.
  - Connection repository explicitly rejects inputs prefixed with `Bearer ` or `secret_`.
  - Database connection strings with passwords are redacted in logs.

### T2: Cross-Tenant Data Leakage & Manipulation
- **Threat**: Tenant A reads, claims, or approves an invoice or payment belonging to Tenant B.
- **Mitigations**:
  - Every business table schema includes a mandatory `tenant_id` foreign key.
  - All foreign keys between entities (`allocations` -> `payments`, `allocations` -> `invoices`, etc.) are composite or strictly validated for tenant identity.
  - Repository layer enforces `_ensure_tenant` checks on entity retrieval.
  - Database transactions verify that all parties in an allocation share identical `tenant_id`.

### T3: Financial Ledger Tampering and Double-Deduction
- **Threat**:
  1. An invoice or payment is over-allocated beyond its capacity.
  2. Duplicate approvals occur concurrently.
  3. Floating-point rounding creates minor unit leaks.
- **Mitigations**:
  - **Invariant I1**: All monetary amounts are stored as integer minor units (`BigInteger`). Floats are strictly prohibited.
  - **Invariants I2 & I3**: `allocations` table checks payment and invoice capacities atomically before insertion.
  - **Invariant I10**: PostgreSQL `SELECT ... FOR UPDATE` acquires row-level locks on payments and invoices in deterministic sorted order.
  - **Invariant I11**: Independent verifier is re-run inside the approval transaction against current ledger state.
  - Idempotency keys (`idempotency_key` unique per tenant) prevent duplicate executions.

### T4: Audit Log Manipulation (Repudiation)
- **Threat**: An attacker modifies or deletes historical audit events or approvals to conceal fraudulent or erroneous allocations.
- **Mitigations**:
  - **Invariant I12**: `audit_events` is an append-only, SHA-256 hash-chained sequence (`hash = SHA-256(prev_hash + canonical_json(payload))`).
  - Sequence numbers are strictly monotonically increasing (`1, 2, 3...`) per tenant.
  - Verification algorithm (`verify_audit_chain`) detects:
    - Payload modifications
    - Corrupted event hashes
    - Corrupted previous hashes
    - Inserted, deleted, or reordered events
    - Missing or skipped sequence numbers
  - Immutability enforced in repository; database level triggers/permissions restrict UPDATE/DELETE operations on audit logs.

### T5: Silent Overwrite of Ingestion Evidence
- **Threat**: A conflicting payload with the same provider ID silently updates previous evidence, destroying legal auditability.
- **Mitigations**:
  - `provider_records` is immutable.
  - Ingestion computes a SHA-256 content hash of the canonical JSON payload.
  - Identical payloads are resolved idempotently.
  - Conflicting payloads fail closed by raising `ConflictingSourceRecordError`.

### T6: AI/Automation Privilege Escalation
- **Threat**: An AI model or automatic matcher assigns status `APPROVED` directly, bypassing human or authorized policy checks.
- **Mitigations**:
  - **Invariant I6**: Matchers and AI can only produce `PROPOSED` outcomes.
  - `ProposalRecord` rejects initialization with `APPROVED` verifier status.
  - Only `ApprovalService.approve_proposal()` can create `AllocationRecord`s, requiring explicit actor attribution and idempotency keys.

---

## 4. Concurrency Model: SQLite vs PostgreSQL

| Guarantee / Mechanism | SQLite (Local/Dev) | PostgreSQL (Production) |
| :--- | :--- | :--- |
| **Transaction Isolation** | Serialized / Immediate (Database-level lock) | Read Committed with explicit Row Locks |
| **Row-Level Locking** | Not supported (locks entire database file) | `SELECT ... FOR UPDATE` on Payment and Invoices |
| **Concurrency Scalability** | Low concurrency (sequential write locks) | High concurrency (row-level isolation) |
| **Deadlock Prevention** | SQLite busy timeout | Deterministic lock ordering (sort by ID) |
| **Suitability** | Zero-setup testing, offline verification | Multi-operator, high-throughput financial ledger |

---

## 5. Security Checklist for Subsequent Milestones
- [ ] D4: Validate webhook signatures using PayPal webhook verification endpoints before writing to `provider_records`.
- [ ] D4: Ensure sync worker runs with least-privilege DB credentials (no DDL permissions).
- [ ] D6: Ensure AI suggestions pass through independent verification gates before human review queue.
- [ ] D7: Enforce RBAC/tenant authentication headers in FastAPI layer.

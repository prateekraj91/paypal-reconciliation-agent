"""Financial persistence, ledger and tamper-evident audit store.

Provides durable, tenant-scoped storage, atomic approval, and hash-chained
audit for the reconciliation engine.

Standard library + SQLAlchemy 2.x + Alembic only.
The core reconcile/ package does NOT import this module.
"""

from .config import DatabaseConfig
from .db import DatabaseEngine, get_engine, session_scope
from .schema import Base
from .canonical import canonical_json, hash_content
from .audit import (
    GENESIS_HASH,
    compute_event_hash,
    create_audit_payload,
    verify_audit_chain,
    AuditIntegrityError,
)
from .approval import (
    ApprovalService,
    ApprovalResult,
    ApprovalError,
    VerificationFailedError,
)
from .workflow import StorageWorkflow, IngestionResult
from .repositories import (
    TenantRepository,
    ConnectionRepository,
    ProviderRecordRepository,
    InvoiceRepository,
    PaymentRepository,
    ReconciliationRunRepository,
    ProposalRepository,
    VerificationRepository,
    DecisionRepository,
    AllocationRepository,
    AuditRepository,
    RepositoryError,
    EntityNotFoundError,
    ConflictError,
    StaleVersionError,
    TenantIsolationError,
    ConflictingSourceRecordError,
)

__all__ = [
    "DatabaseConfig",
    "DatabaseEngine",
    "get_engine",
    "session_scope",
    "Base",
    "canonical_json",
    "hash_content",
    "GENESIS_HASH",
    "compute_event_hash",
    "create_audit_payload",
    "verify_audit_chain",
    "AuditIntegrityError",
    "ApprovalService",
    "ApprovalResult",
    "ApprovalError",
    "VerificationFailedError",
    "StorageWorkflow",
    "IngestionResult",
    "TenantRepository",
    "ConnectionRepository",
    "ProviderRecordRepository",
    "InvoiceRepository",
    "PaymentRepository",
    "ReconciliationRunRepository",
    "ProposalRepository",
    "VerificationRepository",
    "DecisionRepository",
    "AllocationRepository",
    "AuditRepository",
    "RepositoryError",
    "EntityNotFoundError",
    "ConflictError",
    "StaleVersionError",
    "TenantIsolationError",
    "ConflictingSourceRecordError",
]

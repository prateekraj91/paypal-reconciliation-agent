"""Repository abstractions for the persistence and financial ledger layer."""

from .base import (
    BaseRepository,
    RepositoryError,
    EntityNotFoundError,
    ConflictError,
    StaleVersionError,
    TenantIsolationError,
)
from .tenant_repo import TenantRepository, ConnectionRepository
from .provider_record_repo import ProviderRecordRepository, ConflictingSourceRecordError
from .invoice_repo import InvoiceRepository
from .payment_repo import PaymentRepository
from .reconciliation_repo import (
    ReconciliationRunRepository,
    ProposalRepository,
    VerificationRepository,
    DecisionRepository,
)
from .allocation_repo import AllocationRepository
from .audit_repo import AuditRepository

__all__ = [
    "BaseRepository",
    "RepositoryError",
    "EntityNotFoundError",
    "ConflictError",
    "StaleVersionError",
    "TenantIsolationError",
    "TenantRepository",
    "ConnectionRepository",
    "ProviderRecordRepository",
    "ConflictingSourceRecordError",
    "InvoiceRepository",
    "PaymentRepository",
    "ReconciliationRunRepository",
    "ProposalRepository",
    "VerificationRepository",
    "DecisionRepository",
    "AllocationRepository",
    "AuditRepository",
]

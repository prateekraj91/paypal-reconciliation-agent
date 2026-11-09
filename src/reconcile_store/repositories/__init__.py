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
from .payment_repo import (
    PaymentObservation,
    PaymentRepository,
    PaymentSourceConflictError,
    SETTLED_PAYER_PLACEHOLDER,
    SOURCE_INVOICE_EMBEDDED,
    RECORD_KIND_NON_PAYMENT_EVENT,
    SOURCE_TRANSACTION_SEARCH,
    SOURCE_UNSPECIFIED,
    SOURCE_WEBHOOK_CAPTURE,
)
from .reconciliation_repo import (
    ReconciliationRunRepository,
    ProposalRepository,
    VerificationRepository,
    DecisionRepository,
)
from .allocation_repo import AllocationRepository
from .settlement_repo import ProviderSettlementRepository
from .audit_repo import AuditRepository
from .webhook_repo import WebhookInboxRepository, InvalidWebhookStateTransitionError
from .job_repo import JobRepository
from .refund_repo import RefundRepository
from .investigation_repo import InvestigationRepository

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
    "ProviderSettlementRepository",
    "RefundRepository",
    "InvestigationRepository",
    "AuditRepository",
    "WebhookInboxRepository",
    "InvalidWebhookStateTransitionError",
    "JobRepository",
]

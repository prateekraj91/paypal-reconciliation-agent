"""SQLAlchemy 2.x data model and declarative schema for the financial store.

12 Core Tables:
1. tenants
2. connections
3. provider_records
4. invoices
5. payments
6. reconciliation_runs
7. proposals
8. proposal_allocations
9. verifications
10. decisions
11. allocations (application-owned financial ledger)
12. audit_events (tamper-evident hash-chained log)

Integer minor units are enforced for all monetary quantities.
Floats are strictly forbidden.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
import json
from typing import Any, Optional
import uuid

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from reconcile.models import (
    AllocationStatus,
    Candidate,
    Evidence,
    EvidenceCode,
    Invoice,
    InvoiceStatus,
    MatchOutcome,
    MatchProposal,
    Payment,
    PaymentAllocation,
    PaymentStatus,
)


def _gen_id(prefix: str = "") -> str:
    val = uuid.uuid4().hex
    return f"{prefix}_{val}" if prefix else val


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    """Base class for all SQLAlchemy 2.x declarative models."""
    pass


# --------------------------------------------------------------------------
# 1. Tenants
# --------------------------------------------------------------------------


class TenantRecord(Base):
    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )

    connections: Mapped[list["ConnectionRecord"]] = relationship(
        back_populates="tenant", cascade="all, delete-orphan"
    )


# --------------------------------------------------------------------------
# 2. Connections
# --------------------------------------------------------------------------


class ConnectionRecord(Base):
    __tablename__ = "connections"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: _gen_id("conn"))
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    environment: Mapped[str] = mapped_column(String(32), nullable=False, default="sandbox")
    credential_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )

    tenant: Mapped["TenantRecord"] = relationship(back_populates="connections")

    __table_args__ = (
        UniqueConstraint("tenant_id", "provider", "environment", name="uq_connection_tenant_provider_env"),
    )


# --------------------------------------------------------------------------
# 3. Provider Records (Immutable Source Evidence)
# --------------------------------------------------------------------------


class ProviderRecord(Base):
    __tablename__ = "provider_records"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: _gen_id("prec"))
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    record_kind: Mapped[str] = mapped_column(String(32), nullable=False)  # "invoice", "transaction"
    provider_record_id: Mapped[str] = mapped_column(String(255), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "provider", "record_kind", "provider_record_id", "content_hash",
            name="uq_provider_record_tenant_kind_id_hash",
        ),
        Index("ix_provider_record_lookup", "tenant_id", "provider", "record_kind", "provider_record_id"),
    )


# --------------------------------------------------------------------------
# 4. Invoices
# --------------------------------------------------------------------------


class InvoiceRecord(Base):
    __tablename__ = "invoices"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: _gen_id("inv"))
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False, default="paypal")
    provider_invoice_id: Mapped[str] = mapped_column(String(255), nullable=False)
    invoice_number: Mapped[str] = mapped_column(String(128), nullable=False)
    customer_name: Mapped[str] = mapped_column(String(255), nullable=False)
    customer_email: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    total_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    provider_due_minor: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="SENT")
    source_record_id: Mapped[Optional[str]] = mapped_column(String(64), ForeignKey("provider_records.id", ondelete="SET NULL"), nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    issued_on: Mapped[date] = mapped_column(Date, nullable=False)
    due_on: Mapped[date] = mapped_column(Date, nullable=False)
    memo: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )

    __table_args__ = (
        UniqueConstraint("tenant_id", "provider", "provider_invoice_id", name="uq_invoice_tenant_provider_id"),
        Index("ix_invoice_tenant_number", "tenant_id", "invoice_number"),
    )

    def to_domain(self) -> Invoice:
        return Invoice(
            invoice_id=self.provider_invoice_id,
            number=self.invoice_number,
            customer_name=self.customer_name,
            amount_cents=self.total_minor,
            currency=self.currency,
            issued_on=self.issued_on,
            due_on=self.due_on,
            status=InvoiceStatus(self.status),
            customer_email=self.customer_email,
            memo=self.memo,
            due_amount_cents=self.provider_due_minor,
        )

    @classmethod
    def from_domain(
        cls,
        invoice: Invoice,
        tenant_id: str,
        provider: str = "paypal",
        source_record_id: Optional[str] = None,
    ) -> "InvoiceRecord":
        return cls(
            tenant_id=tenant_id,
            provider=provider,
            provider_invoice_id=invoice.invoice_id,
            invoice_number=invoice.number,
            customer_name=invoice.customer_name,
            customer_email=invoice.customer_email,
            total_minor=invoice.amount_cents,
            currency=invoice.currency.strip().upper(),
            provider_due_minor=invoice.due_amount_cents,
            status=invoice.status.value,
            issued_on=invoice.issued_on,
            due_on=invoice.due_on,
            memo=invoice.memo,
            source_record_id=source_record_id,
        )


# --------------------------------------------------------------------------
# 5. Payments
# --------------------------------------------------------------------------


class PaymentRecord(Base):
    __tablename__ = "payments"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: _gen_id("pay"))
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False, default="paypal")
    provider_payment_id: Mapped[str] = mapped_column(String(255), nullable=False)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="COMPLETED")
    payer_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    payer_email: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    reference: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source_record_id: Mapped[Optional[str]] = mapped_column(String(64), ForeignKey("provider_records.id", ondelete="SET NULL"), nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )

    __table_args__ = (
        UniqueConstraint("tenant_id", "provider", "provider_payment_id", name="uq_payment_tenant_provider_id"),
        Index("ix_payment_tenant_reference", "tenant_id", "reference"),
    )

    def to_domain(self) -> Payment:
        return Payment(
            payment_id=self.provider_payment_id,
            amount_cents=self.amount_minor,
            currency=self.currency,
            received_at=self.received_at,
            payer_name=self.payer_name,
            payer_email=self.payer_email,
            reference=self.reference,
            note=self.note,
            status=PaymentStatus(self.status),
        )

    @classmethod
    def from_domain(
        cls,
        payment: Payment,
        tenant_id: str,
        provider: str = "paypal",
        source_record_id: Optional[str] = None,
    ) -> "PaymentRecord":
        return cls(
            tenant_id=tenant_id,
            provider=provider,
            provider_payment_id=payment.payment_id,
            amount_minor=payment.amount_cents,
            currency=payment.currency.strip().upper(),
            status=payment.status.value,
            payer_name=payment.payer_name,
            payer_email=payment.payer_email,
            reference=payment.reference,
            note=payment.note,
            received_at=payment.received_at,
            source_record_id=source_record_id,
        )


# --------------------------------------------------------------------------
# 6. Reconciliation Runs
# --------------------------------------------------------------------------


class ReconciliationRunRecord(Base):
    __tablename__ = "reconciliation_runs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: _gen_id("run"))
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    input_snapshot_id: Mapped[str] = mapped_column(String(64), nullable=False)
    matcher_name: Mapped[str] = mapped_column(String(64), nullable=False)
    matcher_version: Mapped[str] = mapped_column(String(32), nullable=False, default="1.0.0")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="COMPLETED")
    metrics_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    config_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)

    proposals: Mapped[list["ProposalRecord"]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )

    @property
    def metrics(self) -> dict[str, Any]:
        """Parsed dictionary representation of metrics_json."""
        try:
            return json.loads(self.metrics_json) if self.metrics_json else {}
        except Exception:
            return {}



# --------------------------------------------------------------------------
# 7. Proposals
# --------------------------------------------------------------------------


class ProposalRecord(Base):
    __tablename__ = "proposals"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: _gen_id("prop"))
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    run_id: Mapped[str] = mapped_column(String(64), ForeignKey("reconciliation_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    payment_id: Mapped[str] = mapped_column(String(64), ForeignKey("payments.id", ondelete="CASCADE"), nullable=False, index=True)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    evidence_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    source_hashes_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    verifier_status: Mapped[str] = mapped_column(String(32), nullable=False, default="NOT_VERIFIED")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)

    run: Mapped["ReconciliationRunRecord"] = relationship(back_populates="proposals")
    allocations: Mapped[list["ProposalAllocationRecord"]] = relationship(
        back_populates="proposal", cascade="all, delete-orphan"
    )
    verifications: Mapped[list["VerificationRecord"]] = relationship(
        back_populates="proposal", cascade="all, delete-orphan"
    )
    decisions: Mapped[list["DecisionRecord"]] = relationship(
        back_populates="proposal", cascade="all, delete-orphan"
    )


# --------------------------------------------------------------------------
# 8. Proposal Allocations
# --------------------------------------------------------------------------


class ProposalAllocationRecord(Base):
    __tablename__ = "proposal_allocations"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: _gen_id("palloc"))
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    proposal_id: Mapped[str] = mapped_column(String(64), ForeignKey("proposals.id", ondelete="CASCADE"), nullable=False, index=True)
    payment_id: Mapped[str] = mapped_column(String(64), ForeignKey("payments.id", ondelete="CASCADE"), nullable=False, index=True)
    invoice_id: Mapped[str] = mapped_column(String(64), ForeignKey("invoices.id", ondelete="CASCADE"), nullable=False, index=True)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    evidence_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")

    proposal: Mapped["ProposalRecord"] = relationship(back_populates="allocations")


# --------------------------------------------------------------------------
# 9. Verifications
# --------------------------------------------------------------------------


class VerificationRecord(Base):
    __tablename__ = "verifications"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: _gen_id("ver"))
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    proposal_id: Mapped[str] = mapped_column(String(64), ForeignKey("proposals.id", ondelete="CASCADE"), nullable=False, index=True)
    check_name: Mapped[str] = mapped_column(String(64), nullable=False)
    check_inputs_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    failure_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    source_hashes_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    verified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)

    proposal: Mapped["ProposalRecord"] = relationship(back_populates="verifications")


# --------------------------------------------------------------------------
# 10. Decisions
# --------------------------------------------------------------------------


class DecisionRecord(Base):
    __tablename__ = "decisions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: _gen_id("dec"))
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    proposal_id: Mapped[str] = mapped_column(String(64), ForeignKey("proposals.id", ondelete="CASCADE"), nullable=False, index=True)
    decision_type: Mapped[str] = mapped_column(String(32), nullable=False)  # APPROVED, REJECTED, ESCALATED
    actor: Mapped[str] = mapped_column(String(255), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)

    proposal: Mapped["ProposalRecord"] = relationship(back_populates="decisions")

    __table_args__ = (
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_decision_tenant_idempotency_key"),
    )


# --------------------------------------------------------------------------
# 11. Allocations (Application-Owned Financial Ledger)
# --------------------------------------------------------------------------


class AllocationRecord(Base):
    __tablename__ = "allocations"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: _gen_id("alloc"))
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    payment_id: Mapped[str] = mapped_column(String(64), ForeignKey("payments.id", ondelete="RESTRICT"), nullable=False, index=True)
    invoice_id: Mapped[str] = mapped_column(String(64), ForeignKey("invoices.id", ondelete="RESTRICT"), nullable=False, index=True)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    decision_id: Mapped[str] = mapped_column(String(64), ForeignKey("decisions.id", ondelete="RESTRICT"), nullable=False)
    actor: Mapped[str] = mapped_column(String(255), nullable=False)
    evidence_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    allocated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("tenant_id", "payment_id", "invoice_id", name="uq_allocation_tenant_pay_inv"),
    )

    def to_domain(self, provider_payment_id: str, provider_invoice_id: str) -> PaymentAllocation:
        return PaymentAllocation(
            payment_id=provider_payment_id,
            invoice_id=provider_invoice_id,
            amount_cents=self.amount_minor,
            status=AllocationStatus.APPROVED,
            currency=self.currency,
        )


# --------------------------------------------------------------------------
# 11b. Provider Settlements (Externally Reported Financial Facts)
# --------------------------------------------------------------------------


class ProviderSettlementRecord(Base):
    __tablename__ = "provider_settlements"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: _gen_id("psett"))
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False, default="paypal")
    environment: Mapped[str] = mapped_column(String(32), nullable=False, default="sandbox")
    provider_settlement_id: Mapped[str] = mapped_column(String(255), nullable=False)
    invoice_id: Mapped[str] = mapped_column(String(64), ForeignKey("invoices.id", ondelete="CASCADE"), nullable=False, index=True)
    payment_id: Mapped[Optional[str]] = mapped_column(String(64), ForeignKey("payments.id", ondelete="SET NULL"), nullable=True, index=True)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    source_record_id: Mapped[Optional[str]] = mapped_column(String(64), ForeignKey("provider_records.id", ondelete="SET NULL"), nullable=True)
    settled_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    evidence_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "provider", "environment", "invoice_id", "provider_settlement_id",
            name="uq_provider_settlement_tenant_prov_env_inv_id",
        ),
        Index("ix_provider_settlements_tenant_invoice", "tenant_id", "invoice_id"),
        Index("ix_provider_settlements_tenant_payment", "tenant_id", "payment_id"),
    )

    def to_domain(self, provider_payment_id: str, provider_invoice_id: str) -> PaymentAllocation:
        return PaymentAllocation(
            payment_id=provider_payment_id,
            invoice_id=provider_invoice_id,
            amount_cents=self.amount_minor,
            status=AllocationStatus.APPROVED,
            currency=self.currency,
        )


# --------------------------------------------------------------------------
# 12. Audit Events (Append-Only Tamper-Evident Hash Chain)
# --------------------------------------------------------------------------


class AuditEventRecord(Base):
    __tablename__ = "audit_events"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: _gen_id("aud"))
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    sequence_number: Mapped[int] = mapped_column(BigInteger, nullable=False)
    event_id: Mapped[str] = mapped_column(String(64), nullable=False, default=lambda: uuid.uuid4().hex)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    actor: Mapped[str] = mapped_column(String(255), nullable=False)
    subject_type: Mapped[str] = mapped_column(String(64), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    prev_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    event_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("tenant_id", "sequence_number", name="uq_audit_event_tenant_sequence"),
        UniqueConstraint("tenant_id", "event_hash", name="uq_audit_event_tenant_hash"),
        Index("ix_audit_event_lookup", "tenant_id", "sequence_number"),
    )


# --------------------------------------------------------------------------
# 13. Jobs (Database-Backed Background Job Queue)
# --------------------------------------------------------------------------


class JobRecord(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: _gen_id("job"))
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    job_type: Mapped[str] = mapped_column(String(64), nullable=False)  # "WEBHOOK_INGEST", "MANUAL_SYNC"
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    status: Mapped[str] = mapped_column(String(32), default="QUEUED", nullable=False)  # QUEUED, RUNNING, RETRY, COMPLETED, FAILED
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    lease_expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    worker_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_jobs_tenant_idempotency_key"),
        Index("ix_jobs_queue_poll", "status", "available_at"),
        Index("ix_jobs_tenant_status", "tenant_id", "status"),
    )

    @property
    def payload(self) -> dict[str, Any]:
        if not self.payload_json:
            return {}
        try:
            return json.loads(self.payload_json)
        except Exception:
            return {}


# --------------------------------------------------------------------------
# 14. Webhook Inbox (Durable Inbound Provider Webhooks)
# --------------------------------------------------------------------------


class WebhookInboxRecord(Base):
    __tablename__ = "webhook_inbox"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: _gen_id("wh"))
    tenant_id: Mapped[str] = mapped_column(String(64), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(32), default="paypal", nullable=False)
    environment: Mapped[str] = mapped_column(String(32), default="sandbox", nullable=False)
    event_id: Mapped[str] = mapped_column(String(128), nullable=False)
    webhook_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    resource_type: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    resource_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    raw_payload: Mapped[str] = mapped_column(Text, nullable=False)
    headers_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    verification_status: Mapped[str] = mapped_column(String(32), default="RECEIVED", nullable=False)
    processing_status: Mapped[str] = mapped_column(String(32), default="RECEIVED", nullable=False)
    job_id: Mapped[Optional[str]] = mapped_column(String(64), ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failure_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    processed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    job: Mapped[Optional["JobRecord"]] = relationship()

    __table_args__ = (
        UniqueConstraint("tenant_id", "provider", "environment", "event_id", name="uq_webhook_inbox_tenant_prov_env_evt"),
        Index("ix_webhook_inbox_lookup", "tenant_id", "processing_status"),
    )

    @property
    def headers(self) -> dict[str, Any]:
        if not self.headers_json:
            return {}
        try:
            return json.loads(self.headers_json)
        except Exception:
            return {}

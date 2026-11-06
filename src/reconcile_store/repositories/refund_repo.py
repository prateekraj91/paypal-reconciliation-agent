"""Refund repository for storing and querying refunds and payment reversals."""

from __future__ import annotations

from typing import Optional, Sequence
from datetime import datetime, timezone
import uuid
from sqlalchemy import select
from sqlalchemy.orm import Session

from .base import BaseRepository, EntityNotFoundError, ConflictError
from ..schema import RefundRecord, PaymentRecord


class RefundRepository(BaseRepository[RefundRecord]):
    """Repository for external refunds and reversals."""

    def upsert_refund(
        self,
        tenant_id: str,
        provider: str,
        provider_refund_id: str,
        parent_payment_id: str,
        amount_minor: int,
        currency: str,
        invoice_id: Optional[str] = None,
        status: str = "COMPLETED",
        reason: str = "",
        target_type: str = "UNKNOWN",
        settlement_id: Optional[str] = None,
        allocation_id: Optional[str] = None,
        refunded_at: Optional[datetime] = None,
        source_record_id: Optional[str] = None,
    ) -> RefundRecord:
        """Create or update a refund record idempotently."""
        if not currency or not currency.strip():
            raise ValueError("Refund currency must not be empty")
        if amount_minor <= 0:
            raise ValueError(f"Refund amount must be positive, got {amount_minor}")

        stmt = select(RefundRecord).where(
            RefundRecord.tenant_id == tenant_id,
            RefundRecord.provider == provider,
            RefundRecord.provider_refund_id == provider_refund_id,
        )
        existing = self.session.scalars(stmt).first()

        now = datetime.now(timezone.utc)
        ref_time = refunded_at or now

        if existing:
            if existing.currency != currency.strip().upper():
                raise ConflictError(
                    f"Cannot change currency of refund '{provider_refund_id}' from "
                    f"'{existing.currency}' to '{currency.upper()}'"
                )
            if existing.amount_minor != amount_minor:
                raise ConflictError(
                    f"Cannot change amount of refund '{provider_refund_id}' from "
                    f"{existing.amount_minor} to {amount_minor}"
                )
            existing.status = status
            existing.reason = reason
            if invoice_id and not existing.invoice_id:
                existing.invoice_id = invoice_id
            if settlement_id and not existing.settlement_id:
                existing.settlement_id = settlement_id
            if allocation_id and not existing.allocation_id:
                existing.allocation_id = allocation_id
            if target_type != "UNKNOWN" and existing.target_type == "UNKNOWN":
                existing.target_type = target_type
            if source_record_id and not existing.source_record_id:
                existing.source_record_id = source_record_id
            self.session.flush()
            return existing

        record = RefundRecord(
            tenant_id=tenant_id,
            provider=provider,
            provider_refund_id=provider_refund_id,
            parent_payment_id=parent_payment_id,
            invoice_id=invoice_id,
            amount_minor=amount_minor,
            currency=currency.strip().upper(),
            status=status,
            reason=reason,
            record_type="REFUND",
            target_type=target_type,
            settlement_id=settlement_id,
            allocation_id=allocation_id,
            source_record_id=source_record_id,
            refunded_at=ref_time,
        )
        self.session.add(record)
        self.session.flush()
        return record

    def upsert_reversal(
        self,
        tenant_id: str,
        provider: str,
        provider_reversal_id: str,
        parent_payment_id: str,
        amount_minor: int,
        currency: str,
        invoice_id: Optional[str] = None,
        status: str = "COMPLETED",
        reason: str = "",
        dispute_id: Optional[str] = None,
        target_type: str = "UNKNOWN",
        settlement_id: Optional[str] = None,
        allocation_id: Optional[str] = None,
        reversed_at: Optional[datetime] = None,
        source_record_id: Optional[str] = None,
    ) -> RefundRecord:
        """Create or update a payment reversal record idempotently."""
        if not currency or not currency.strip():
            raise ValueError("Reversal currency must not be empty")
        if amount_minor <= 0:
            raise ValueError(f"Reversal amount must be positive, got {amount_minor}")

        stmt = select(RefundRecord).where(
            RefundRecord.tenant_id == tenant_id,
            RefundRecord.provider == provider,
            RefundRecord.provider_refund_id == provider_reversal_id,
        )
        existing = self.session.scalars(stmt).first()

        now = datetime.now(timezone.utc)
        rev_time = reversed_at or now

        if existing:
            if existing.currency != currency.strip().upper():
                raise ConflictError(
                    f"Cannot change currency of reversal '{provider_reversal_id}' from "
                    f"'{existing.currency}' to '{currency.upper()}'"
                )
            if existing.amount_minor != amount_minor:
                raise ConflictError(
                    f"Cannot change amount of reversal '{provider_reversal_id}' from "
                    f"{existing.amount_minor} to {amount_minor}"
                )
            existing.status = status
            existing.reason = reason
            if dispute_id and not existing.dispute_id:
                existing.dispute_id = dispute_id
            if invoice_id and not existing.invoice_id:
                existing.invoice_id = invoice_id
            if settlement_id and not existing.settlement_id:
                existing.settlement_id = settlement_id
            if allocation_id and not existing.allocation_id:
                existing.allocation_id = allocation_id
            if target_type != "UNKNOWN" and existing.target_type == "UNKNOWN":
                existing.target_type = target_type
            if source_record_id and not existing.source_record_id:
                existing.source_record_id = source_record_id
            self.session.flush()
            return existing

        record = RefundRecord(
            tenant_id=tenant_id,
            provider=provider,
            provider_refund_id=provider_reversal_id,
            parent_payment_id=parent_payment_id,
            invoice_id=invoice_id,
            amount_minor=amount_minor,
            currency=currency.strip().upper(),
            status=status,
            reason=reason,
            record_type="REVERSAL",
            target_type=target_type,
            settlement_id=settlement_id,
            allocation_id=allocation_id,
            dispute_id=dispute_id,
            source_record_id=source_record_id,
            refunded_at=rev_time,
        )
        self.session.add(record)
        self.session.flush()
        return record

    def get_refund(self, tenant_id: str, refund_id: str) -> Optional[RefundRecord]:
        stmt = select(RefundRecord).where(
            RefundRecord.tenant_id == tenant_id,
            RefundRecord.id == refund_id,
        )
        return self.session.scalars(stmt).first()

    def get_by_provider_id(
        self, tenant_id: str, provider: str, provider_refund_id: str
    ) -> Optional[RefundRecord]:
        stmt = select(RefundRecord).where(
            RefundRecord.tenant_id == tenant_id,
            RefundRecord.provider == provider,
            RefundRecord.provider_refund_id == provider_refund_id,
        )
        return self.session.scalars(stmt).first()

    def list_refunds_for_payment(
        self, tenant_id: str, parent_payment_id: str
    ) -> Sequence[RefundRecord]:
        stmt = select(RefundRecord).where(
            RefundRecord.tenant_id == tenant_id,
            RefundRecord.parent_payment_id == parent_payment_id,
        )
        return self.session.scalars(stmt).all()

    def list_refunds_for_invoice(
        self, tenant_id: str, invoice_id: str
    ) -> Sequence[RefundRecord]:
        stmt = select(RefundRecord).where(
            RefundRecord.tenant_id == tenant_id,
            RefundRecord.invoice_id == invoice_id,
        )
        return self.session.scalars(stmt).all()

    def list_all_refunds(self, tenant_id: str) -> Sequence[RefundRecord]:
        stmt = select(RefundRecord).where(
            RefundRecord.tenant_id == tenant_id,
        )
        return self.session.scalars(stmt).all()

    def list_all_reversals(self, tenant_id: str) -> Sequence[RefundRecord]:
        stmt = select(RefundRecord).where(
            RefundRecord.tenant_id == tenant_id,
            RefundRecord.record_type == "REVERSAL",
        )
        return self.session.scalars(stmt).all()

    def list_reversals_for_payment(
        self, tenant_id: str, parent_payment_id: str
    ) -> Sequence[RefundRecord]:
        stmt = select(RefundRecord).where(
            RefundRecord.tenant_id == tenant_id,
            RefundRecord.parent_payment_id == parent_payment_id,
            RefundRecord.record_type == "REVERSAL",
        )
        return self.session.scalars(stmt).all()

    def list_reversals_for_invoice(
        self, tenant_id: str, invoice_id: str
    ) -> Sequence[RefundRecord]:
        stmt = select(RefundRecord).where(
            RefundRecord.tenant_id == tenant_id,
            RefundRecord.invoice_id == invoice_id,
            RefundRecord.record_type == "REVERSAL",
        )
        return self.session.scalars(stmt).all()

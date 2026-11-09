"""Payment repository with unapplied cash calculation and eligibility enforcement."""

from __future__ import annotations

from typing import Optional, Sequence
from datetime import datetime, timezone
import uuid
from sqlalchemy import select, func
from sqlalchemy.orm import Session

from .base import BaseRepository, EntityNotFoundError, ConflictError
from ..schema import PaymentRecord, AllocationRecord, RefundRecord


class IneligiblePaymentError(Exception):
    """Raised when a payment is ineligible for allocation (e.g. PENDING, DENIED, REFUNDED)."""


class PaymentRepository(BaseRepository[PaymentRecord]):
    """Repository for canonical payment records and unapplied cash tracking."""

    def upsert_payment(
        self,
        tenant_id: str,
        provider: str,
        provider_payment_id: str,
        amount_minor: int,
        currency: str,
        status: str = "COMPLETED",
        payer_name: str = "",
        payer_email: Optional[str] = None,
        reference: Optional[str] = None,
        note: str = "",
        received_at: Optional[datetime] = None,
        source_record_id: Optional[str] = None,
    ) -> PaymentRecord:
        """Create or update a canonical payment record idempotently."""
        if not currency or not currency.strip():
            raise ValueError("Payment currency must not be empty")
        if amount_minor <= 0:
            raise ValueError(f"Payment amount must be positive, got {amount_minor}")

        stmt = select(PaymentRecord).where(
            PaymentRecord.tenant_id == tenant_id,
            PaymentRecord.provider == provider,
            PaymentRecord.provider_payment_id == provider_payment_id,
        )
        existing = self.session.scalars(stmt).first()

        now = datetime.now(timezone.utc)
        recv = received_at or now

        if existing:
            # Check currency consistency
            if existing.currency != currency.upper():
                raise ConflictError(
                    f"Cannot change currency of payment '{provider_payment_id}' from "
                    f"'{existing.currency}' to '{currency.upper()}'"
                )

            # Check if amount changed (financial identity invariant)
            if existing.amount_minor != amount_minor:
                raise ConflictError(
                    f"Conflicting payment amount for '{provider_payment_id}': "
                    f"existing {existing.amount_minor} minor units, incoming {amount_minor} minor units. "
                    f"Financial payment identity cannot be silently mutated (I5)."
                )

            # Update mutable fields (status, references) with version increment
            has_changes = (
                existing.status != status
                or existing.payer_name != payer_name
                or existing.payer_email != payer_email
                or existing.reference != reference
                or existing.note != note
                or existing.received_at != recv
                or existing.source_record_id != source_record_id
            )

            if has_changes:
                existing.status = status
                existing.payer_name = payer_name
                existing.payer_email = payer_email
                existing.reference = reference
                existing.note = note
                existing.received_at = recv
                if source_record_id:
                    existing.source_record_id = source_record_id
                existing.version += 1
                existing.updated_at = now
            return existing

        new_payment = PaymentRecord(
            tenant_id=tenant_id,
            provider=provider,
            provider_payment_id=provider_payment_id,
            amount_minor=amount_minor,
            currency=currency.upper(),
            status=status,
            payer_name=payer_name,
            payer_email=payer_email,
            reference=reference,
            note=note,
            received_at=recv,
            source_record_id=source_record_id,
            version=1,
            created_at=now,
            updated_at=now,
        )
        self.session.add(new_payment)
        self.session.flush()
        return new_payment

    def get_payment(self, tenant_id: str, payment_id: str) -> Optional[PaymentRecord]:
        payment = self.session.get(PaymentRecord, payment_id)
        if payment:
            self._ensure_tenant(payment.tenant_id, tenant_id)
        return payment

    def get_by_provider_id(
        self, tenant_id: str, provider: str, provider_payment_id: str
    ) -> Optional[PaymentRecord]:
        stmt = select(PaymentRecord).where(
            PaymentRecord.tenant_id == tenant_id,
            PaymentRecord.provider == provider,
            PaymentRecord.provider_payment_id == provider_payment_id,
        )
        return self.session.scalars(stmt).first()

    def list_payments(
        self, tenant_id: str, status: Optional[str] = None
    ) -> Sequence[PaymentRecord]:
        stmt = select(PaymentRecord).where(PaymentRecord.tenant_id == tenant_id)
        if status:
            stmt = stmt.where(PaymentRecord.status == status)
        stmt = stmt.order_by(PaymentRecord.created_at)
        return self.session.scalars(stmt).all()

    def get_total_allocated_minor(self, tenant_id: str, payment_id: str) -> int:
        """Sum of all approved allocations made from this payment."""
        stmt = select(func.coalesce(func.sum(AllocationRecord.amount_minor), 0)).where(
            AllocationRecord.tenant_id == tenant_id,
            AllocationRecord.payment_id == payment_id,
        )
        return int(self.session.scalar(stmt) or 0)

    def get_unapplied_cash_minor(self, tenant_id: str, payment_id: str) -> int:
        """Remaining unallocated amount: payment.amount_minor - sum(approved allocations) - sum(refunds/reversals).

        Invariant I2: Remainder must remain explicitly identifiable as unapplied cash.
        """
        payment = self.get_payment(tenant_id, payment_id)
        if payment is None:
            raise EntityNotFoundError(f"Payment '{payment_id}' not found for tenant '{tenant_id}'")
        allocated = self.get_total_allocated_minor(tenant_id, payment_id)
        refund_stmt = select(func.coalesce(func.sum(RefundRecord.amount_minor), 0)).where(
            RefundRecord.tenant_id == tenant_id,
            RefundRecord.parent_payment_id.in_([payment.id, payment.provider_payment_id]),
            RefundRecord.status.in_(["COMPLETED", "SUCCESS", "SETTLED"]),
        )
        refunded = int(self.session.scalar(refund_stmt) or 0)
        return max(0, payment.amount_minor - allocated - refunded)

    def validate_eligibility_for_allocation(self, payment: PaymentRecord) -> None:
        """Invariant I7: Only COMPLETED payments are eligible for approval."""
        norm_status = (payment.status or "").upper()
        eligible_statuses = {"COMPLETED", "SUCCESS", "S"}
        if norm_status not in eligible_statuses:
            raise IneligiblePaymentError(
                f"Payment '{payment.provider_payment_id}' has status '{payment.status}', "
                f"which is ineligible for allocation. Only COMPLETED payments can be approved (I7)."
            )

        unapplied = self.get_unapplied_cash_minor(payment.tenant_id, payment.id)
        if unapplied <= 0:
            raise IneligiblePaymentError(
                f"Payment '{payment.provider_payment_id}' has no remaining unapplied cash "
                f"(allocated {payment.amount_minor} of {payment.amount_minor} minor units)"
            )

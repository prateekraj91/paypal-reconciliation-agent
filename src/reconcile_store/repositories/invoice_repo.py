"""Invoice repository with ledger balance calculation and eligibility checking."""

from __future__ import annotations

from typing import Optional, Sequence
from datetime import datetime, timezone, date
import uuid
from sqlalchemy import select, func, or_, and_
from sqlalchemy.orm import Session

from .base import BaseRepository, EntityNotFoundError, ConflictError, StaleVersionError
from ..ledger import PaymentLedger, compute_payment_ledger, is_completed_return
from ..schema import (
    InvoiceRecord,
    AllocationRecord,
    ProviderSettlementRecord,
    RefundRecord,
    PaymentRecord,
)


class IneligibleInvoiceError(Exception):
    """Raised when an invoice is ineligible for allocation (e.g. DRAFT, CANCELLED, PAID)."""


class InvoiceRepository(BaseRepository[InvoiceRecord]):
    """Repository for canonical invoice records and ledger calculations."""

    def upsert_invoice(
        self,
        tenant_id: str,
        provider: str,
        provider_invoice_id: str,
        invoice_number: str,
        total_minor: int,
        currency: str,
        provider_due_minor: Optional[int] = None,
        status: str = "SENT",
        customer_name: str = "",
        customer_email: Optional[str] = None,
        issued_on: Optional[date] = None,
        due_on: Optional[date] = None,
        memo: str = "",
        source_record_id: Optional[str] = None,
    ) -> InvoiceRecord:
        """Create or update a canonical invoice record idempotently with optimistic versioning."""
        if not currency or not currency.strip():
            raise ValueError("Invoice currency must not be empty")
        if total_minor <= 0:
            raise ValueError(f"Invoice amount must be positive, got {total_minor}")

        stmt = select(InvoiceRecord).where(
            InvoiceRecord.tenant_id == tenant_id,
            InvoiceRecord.provider == provider,
            InvoiceRecord.provider_invoice_id == provider_invoice_id,
        )
        existing = self.session.scalars(stmt).first()

        now = datetime.now(timezone.utc)
        curr_issued = issued_on or now.date()
        curr_due = due_on or now.date()

        if existing:
            # Check currency consistency
            if existing.currency != currency.upper():
                raise ConflictError(
                    f"Cannot change currency of invoice '{provider_invoice_id}' from "
                    f"'{existing.currency}' to '{currency.upper()}'"
                )

            # Check if any fields changed
            has_changes = (
                existing.invoice_number != invoice_number
                or existing.total_minor != total_minor
                or existing.provider_due_minor != provider_due_minor
                or existing.status != status
                or existing.customer_name != customer_name
                or existing.customer_email != customer_email
                or existing.issued_on != curr_issued
                or existing.due_on != curr_due
                or existing.memo != memo
                or existing.source_record_id != source_record_id
            )

            if has_changes:
                existing.invoice_number = invoice_number
                existing.total_minor = total_minor
                existing.provider_due_minor = provider_due_minor
                existing.status = status
                existing.customer_name = customer_name
                existing.customer_email = customer_email
                existing.issued_on = curr_issued
                existing.due_on = curr_due
                existing.memo = memo
                if source_record_id:
                    existing.source_record_id = source_record_id
                existing.version += 1
                existing.updated_at = now
            return existing

        new_invoice = InvoiceRecord(
            tenant_id=tenant_id,
            provider=provider,
            provider_invoice_id=provider_invoice_id,
            invoice_number=invoice_number,
            customer_name=customer_name,
            customer_email=customer_email,
            total_minor=total_minor,
            currency=currency.upper(),
            provider_due_minor=provider_due_minor,
            status=status,
            issued_on=curr_issued,
            due_on=curr_due,
            memo=memo,
            source_record_id=source_record_id,
            version=1,
            created_at=now,
            updated_at=now,
        )
        self.session.add(new_invoice)
        self.session.flush()
        return new_invoice

    def get_invoice(self, tenant_id: str, invoice_id: str) -> Optional[InvoiceRecord]:
        invoice = self.session.get(InvoiceRecord, invoice_id)
        if invoice:
            self._ensure_tenant(invoice.tenant_id, tenant_id)
        return invoice

    def get_by_provider_id(
        self, tenant_id: str, provider: str, provider_invoice_id: str
    ) -> Optional[InvoiceRecord]:
        stmt = select(InvoiceRecord).where(
            InvoiceRecord.tenant_id == tenant_id,
            InvoiceRecord.provider == provider,
            InvoiceRecord.provider_invoice_id == provider_invoice_id,
        )
        return self.session.scalars(stmt).first()

    def get_by_number(
        self, tenant_id: str, provider: str, invoice_number: str
    ) -> Optional[InvoiceRecord]:
        stmt = select(InvoiceRecord).where(
            InvoiceRecord.tenant_id == tenant_id,
            InvoiceRecord.provider == provider,
            InvoiceRecord.invoice_number == invoice_number,
        )
        return self.session.scalars(stmt).first()


    def list_invoices(
        self, tenant_id: str, status: Optional[str] = None
    ) -> Sequence[InvoiceRecord]:
        stmt = select(InvoiceRecord).where(InvoiceRecord.tenant_id == tenant_id)
        if status:
            stmt = stmt.where(InvoiceRecord.status == status)
        stmt = stmt.order_by(InvoiceRecord.created_at)
        return self.session.scalars(stmt).all()

    def get_total_allocated_minor(self, tenant_id: str, invoice_id: str) -> int:
        """Sum of all APPROVED application allocations on this invoice."""
        stmt = select(func.coalesce(func.sum(AllocationRecord.amount_minor), 0)).where(
            AllocationRecord.tenant_id == tenant_id,
            AllocationRecord.invoice_id == invoice_id,
        )
        return int(self.session.scalar(stmt) or 0)

    def get_total_settled_minor(self, tenant_id: str, invoice_id: str) -> int:
        """Sum of all externally reported provider settlements on this invoice."""
        stmt = select(func.coalesce(func.sum(ProviderSettlementRecord.amount_minor), 0)).where(
            ProviderSettlementRecord.tenant_id == tenant_id,
            ProviderSettlementRecord.invoice_id == invoice_id,
        )
        return int(self.session.scalar(stmt) or 0)

    def _linked_ledgers(self, tenant_id: str, invoice_id: str) -> tuple[list[PaymentLedger], int]:
        """Ledgers of payments applied to this invoice, plus settlements with no known payment."""
        allocations = self.session.scalars(
            select(AllocationRecord).where(
                AllocationRecord.tenant_id == tenant_id,
                AllocationRecord.invoice_id == invoice_id,
            )
        ).all()
        settlements = self.session.scalars(
            select(ProviderSettlementRecord).where(
                ProviderSettlementRecord.tenant_id == tenant_id,
                ProviderSettlementRecord.invoice_id == invoice_id,
            )
        ).all()

        payments: dict[str, PaymentRecord] = {}
        unlinked_settled = 0
        for payment_id in {a.payment_id for a in allocations}:
            payment = self.session.get(PaymentRecord, payment_id)
            if payment is not None and payment.tenant_id == tenant_id:
                payments[payment.id] = payment
        for settlement in settlements:
            payment = None
            if settlement.payment_id:
                payment = self.session.get(PaymentRecord, settlement.payment_id)
            else:
                payment = self.session.scalars(
                    select(PaymentRecord).where(
                        PaymentRecord.tenant_id == tenant_id,
                        PaymentRecord.provider == settlement.provider,
                        PaymentRecord.provider_payment_id == settlement.provider_settlement_id,
                    )
                ).first()
            if payment is None or payment.tenant_id != tenant_id:
                unlinked_settled += settlement.amount_minor
            else:
                payments[payment.id] = payment

        ledgers = [compute_payment_ledger(self.session, tenant_id, p) for p in payments.values()]
        return ledgers, unlinked_settled

    def _require_invoice(self, tenant_id: str, invoice_id: str) -> InvoiceRecord:
        invoice = self.get_invoice(tenant_id, invoice_id)
        if invoice is None:
            raise EntityNotFoundError(f"Invoice '{invoice_id}' not found for tenant '{tenant_id}'")
        return invoice

    def get_total_returns_minor(self, tenant_id: str, invoice_id: str) -> int:
        """Completed returns that have reversed money applied to this invoice.

        Before anything is applied, returns tagged with the invoice are reported
        for operator context; they cannot affect the balance.
        """
        self._require_invoice(tenant_id, invoice_id)
        ledgers, unlinked_settled = self._linked_ledgers(tenant_id, invoice_id)
        if not ledgers and not unlinked_settled:
            tagged = self.session.scalars(
                select(RefundRecord).where(
                    RefundRecord.tenant_id == tenant_id,
                    RefundRecord.invoice_id == invoice_id,
                )
            ).all()
            return sum(r.amount_minor for r in tagged if is_completed_return(r))
        lines = (ledger.line_for_invoice(invoice_id) for ledger in ledgers)
        return sum(line.returned_minor for line in lines if line is not None)

    def get_net_applied_minor(self, tenant_id: str, invoice_id: str) -> int:
        """Money currently applied to this invoice, net of returns, each payment counted once."""
        self._require_invoice(tenant_id, invoice_id)
        ledgers, unlinked_settled = self._linked_ledgers(tenant_id, invoice_id)
        lines = (ledger.line_for_invoice(invoice_id) for ledger in ledgers)
        return unlinked_settled + sum(line.net_minor for line in lines if line is not None)

    def get_ledger_outstanding_minor(self, tenant_id: str, invoice_id: str) -> int:
        """Invoice total less money currently applied to it, never below zero."""
        invoice = self._require_invoice(tenant_id, invoice_id)
        return max(0, invoice.total_minor - self.get_net_applied_minor(tenant_id, invoice_id))

    def validate_eligibility_for_allocation(self, invoice: InvoiceRecord) -> None:
        """Invariant I8: DRAFT, CANCELLED and PAID invoices are ineligible for new allocations."""
        ineligible_statuses = {"DRAFT", "CANCELLED", "PAID", "VOID"}
        norm_status = (invoice.status or "").upper()
        if norm_status in ineligible_statuses:
            raise IneligibleInvoiceError(
                f"Invoice '{invoice.provider_invoice_id}' has status '{invoice.status}' "
                f"which is ineligible for new allocations (I8)"
            )

        outstanding = self.get_ledger_outstanding_minor(invoice.tenant_id, invoice.id)
        if outstanding <= 0:
            raise IneligibleInvoiceError(
                f"Invoice '{invoice.provider_invoice_id}' is already fully allocated in application ledger "
                f"(outstanding: {outstanding} minor units)"
            )

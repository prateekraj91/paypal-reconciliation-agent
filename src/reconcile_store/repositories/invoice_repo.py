"""Invoice repository with ledger balance calculation and eligibility checking."""

from __future__ import annotations

from typing import Optional, Sequence
from datetime import datetime, timezone, date
import uuid
from sqlalchemy import select, func, or_, and_
from sqlalchemy.orm import Session

from .base import BaseRepository, EntityNotFoundError, ConflictError, StaleVersionError
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

    def get_total_returns_minor(self, tenant_id: str, invoice_id: str) -> int:
        """Sum of all completed refunds and reversals that impair allocations or settlements on this invoice.

        Accounting treatment:
        1. Explicit returns: Refunds/reversals with invoice_id == invoice_id or allocation_id matching
           an allocation on this invoice impair this invoice directly.
        2. Payment-level returns (invoice_id is None): For each payment linked to this invoice,
           unapplied cash absorbs payment returns first. Any excess return beyond unapplied cash
           impairs the payment's active allocations.
        """
        invoice = self.get_invoice(tenant_id, invoice_id)
        if invoice is None:
            raise EntityNotFoundError(f"Invoice '{invoice_id}' not found for tenant '{tenant_id}'")

        stmt_alloc = select(AllocationRecord).where(
            AllocationRecord.tenant_id == tenant_id,
            AllocationRecord.invoice_id == invoice_id,
        )
        allocs = list(self.session.scalars(stmt_alloc).all())

        stmt_sett = select(ProviderSettlementRecord).where(
            ProviderSettlementRecord.tenant_id == tenant_id,
            ProviderSettlementRecord.invoice_id == invoice_id,
        )
        settlements = list(self.session.scalars(stmt_sett).all())

        if not allocs and not settlements:
            stmt_direct_ref = select(func.coalesce(func.sum(RefundRecord.amount_minor), 0)).where(
                RefundRecord.tenant_id == tenant_id,
                RefundRecord.invoice_id == invoice_id,
                RefundRecord.status.in_(["COMPLETED", "SUCCESS", "SETTLED"]),
            )
            return int(self.session.scalar(stmt_direct_ref) or 0)

        total_returns = 0

        # Direct returns targeting this invoice or specific allocations/settlements
        alloc_ids = [a.id for a in allocs]
        sett_ids = [s.id for s in settlements]

        direct_conditions = [RefundRecord.invoice_id == invoice_id]
        if alloc_ids:
            direct_conditions.append(RefundRecord.allocation_id.in_(alloc_ids))
        if sett_ids:
            direct_conditions.append(RefundRecord.settlement_id.in_(sett_ids))

        stmt_direct = select(RefundRecord).where(
            RefundRecord.tenant_id == tenant_id,
            RefundRecord.status.in_(["COMPLETED", "SUCCESS", "SETTLED"]),
            or_(*direct_conditions),
        )
        direct_refs = list(self.session.scalars(stmt_direct).all())
        direct_ref_ids = set()
        for r in direct_refs:
            direct_ref_ids.add(r.id)
            total_returns += r.amount_minor

        # Payment-level returns (invoice_id is None and allocation_id is None)
        linked_payment_ids = set()
        for a in allocs:
            linked_payment_ids.add(a.payment_id)
        for s in settlements:
            if s.payment_id:
                linked_payment_ids.add(s.payment_id)

        for p_id in linked_payment_ids:
            payment = self.session.get(PaymentRecord, p_id)
            if not payment or payment.tenant_id != tenant_id:
                continue

            stmt_all_p_alloc = select(func.coalesce(func.sum(AllocationRecord.amount_minor), 0)).where(
                AllocationRecord.tenant_id == tenant_id,
                AllocationRecord.payment_id == p_id,
            )
            total_p_allocated = int(self.session.scalar(stmt_all_p_alloc) or 0)
            this_inv_p_allocated = sum(a.amount_minor for a in allocs if a.payment_id == p_id)
            unallocated_cash = max(0, payment.amount_minor - total_p_allocated)

            stmt_p_refs = select(RefundRecord).where(
                RefundRecord.tenant_id == tenant_id,
                RefundRecord.parent_payment_id.in_([payment.id, payment.provider_payment_id]),
                RefundRecord.status.in_(["COMPLETED", "SUCCESS", "SETTLED"]),
                RefundRecord.invoice_id.is_(None),
                RefundRecord.allocation_id.is_(None),
            )
            if direct_ref_ids:
                stmt_p_refs = stmt_p_refs.where(RefundRecord.id.not_in(direct_ref_ids))

            p_refs = list(self.session.scalars(stmt_p_refs).all())
            p_ref_total = sum(r.amount_minor for r in p_refs)

            if p_ref_total > unallocated_cash:
                excess = p_ref_total - unallocated_cash
                impairment = min(this_inv_p_allocated, excess)
                total_returns += impairment

        gross_deductions = sum(a.amount_minor for a in allocs) + sum(s.amount_minor for s in settlements)
        return min(gross_deductions, total_returns)

    def get_ledger_outstanding_minor(self, tenant_id: str, invoice_id: str) -> int:
        """Application-owned ledger balance:
        outstanding = invoice.total_minor - net_deductions
        where net_deductions = max(0, sum(approved allocations) + sum(provider settlements) - sum(returns)).

        This accounts for both application approvals and provider-reported settlements
        minus completed refunds and reversals, restoring balance when payments are returned.
        """
        invoice = self.get_invoice(tenant_id, invoice_id)
        if invoice is None:
            raise EntityNotFoundError(f"Invoice '{invoice_id}' not found for tenant '{tenant_id}'")
        allocated = self.get_total_allocated_minor(tenant_id, invoice_id)
        settled = self.get_total_settled_minor(tenant_id, invoice_id)
        returns = self.get_total_returns_minor(tenant_id, invoice_id)
        gross_deductions = allocated + settled
        net_deductions = max(0, gross_deductions - returns)
        return max(0, invoice.total_minor - net_deductions)

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

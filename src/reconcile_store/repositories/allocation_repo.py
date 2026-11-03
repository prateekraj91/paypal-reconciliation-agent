"""Allocation repository representing the immutable, application-owned financial ledger."""

from __future__ import annotations

from typing import Optional, Sequence, Any, Dict
from datetime import datetime, timezone
import uuid
from sqlalchemy import select, func
from sqlalchemy.orm import Session

from .base import BaseRepository, EntityNotFoundError, ConflictError, TenantIsolationError
from ..schema import AllocationRecord, PaymentRecord, InvoiceRecord, DecisionRecord
from ..canonical import canonical_json


class LedgerConstraintError(Exception):
    """Raised when an allocation violates a financial invariant (capacity, currency, etc.)."""


class AllocationRepository(BaseRepository[AllocationRecord]):
    """Repository managing the application-owned financial ledger.

    Enforces invariants:
    I1: Stored in integer minor units > 0.
    I2: Payment capacity (sum <= payment total).
    I3: Invoice capacity (sum <= invoice total).
    I4: Strict currency equality (payment == invoice == allocation).
    I8: Invoice and Payment eligibility.
    """

    def create_allocation(
        self,
        tenant_id: str,
        payment_id: str,
        invoice_id: str,
        amount_minor: int,
        currency: str,
        decision_id: str,
        actor: str,
        evidence: Optional[Sequence[Any]] = None,
    ) -> AllocationRecord:
        # Invariant I1: positive integer minor units
        if amount_minor <= 0:
            raise LedgerConstraintError(f"Allocation amount must be strictly positive, got {amount_minor}")
        if not currency or not currency.strip():
            raise LedgerConstraintError("Allocation currency must not be empty (I4)")
        alloc_currency = currency.upper()

        # Load payment and invoice with tenant checks
        payment = self.session.get(PaymentRecord, payment_id)
        if payment is None or payment.tenant_id != tenant_id:
            raise EntityNotFoundError(f"Payment '{payment_id}' not found for tenant '{tenant_id}'")

        invoice = self.session.get(InvoiceRecord, invoice_id)
        if invoice is None or invoice.tenant_id != tenant_id:
            raise EntityNotFoundError(f"Invoice '{invoice_id}' not found for tenant '{tenant_id}'")

        decision = self.session.get(DecisionRecord, decision_id)
        if decision is None or decision.tenant_id != tenant_id:
            raise EntityNotFoundError(f"Decision '{decision_id}' not found for tenant '{tenant_id}'")
        if decision.decision_type != "APPROVED":
            raise LedgerConstraintError(
                f"Cannot create ledger allocation from non-APPROVED decision '{decision.decision_type}' (I6)"
            )

        # Invariant I4: Currency equality
        if payment.currency != alloc_currency:
            raise LedgerConstraintError(
                f"Currency mismatch: payment '{payment.provider_payment_id}' is '{payment.currency}', "
                f"allocation requested '{alloc_currency}' (I4)"
            )
        if invoice.currency != alloc_currency:
            raise LedgerConstraintError(
                f"Currency mismatch: invoice '{invoice.provider_invoice_id}' is '{invoice.currency}', "
                f"allocation requested '{alloc_currency}' (I4)"
            )

        # Check existing payment allocations: Invariant I2
        stmt_pay = select(func.coalesce(func.sum(AllocationRecord.amount_minor), 0)).where(
            AllocationRecord.tenant_id == tenant_id,
            AllocationRecord.payment_id == payment_id,
        )
        current_pay_allocated = int(self.session.scalar(stmt_pay) or 0)
        if current_pay_allocated + amount_minor > payment.amount_minor:
            raise LedgerConstraintError(
                f"Payment over-allocation (I2): payment amount {payment.amount_minor} minor units, "
                f"already allocated {current_pay_allocated} minor units, cannot add {amount_minor} minor units"
            )

        # Check existing invoice allocations: Invariant I3
        stmt_inv = select(func.coalesce(func.sum(AllocationRecord.amount_minor), 0)).where(
            AllocationRecord.tenant_id == tenant_id,
            AllocationRecord.invoice_id == invoice_id,
        )
        current_inv_allocated = int(self.session.scalar(stmt_inv) or 0)
        if current_inv_allocated + amount_minor > invoice.total_minor:
            raise LedgerConstraintError(
                f"Invoice over-allocation (I3): invoice total {invoice.total_minor} minor units, "
                f"already allocated {current_inv_allocated} minor units, cannot add {amount_minor} minor units"
            )

        allocation = AllocationRecord(
            tenant_id=tenant_id,
            payment_id=payment_id,
            invoice_id=invoice_id,
            amount_minor=amount_minor,
            currency=alloc_currency,
            decision_id=decision_id,
            actor=actor,
            evidence_json=canonical_json(list(evidence or [])),
            allocated_at=datetime.now(timezone.utc),
        )
        self.session.add(allocation)
        self.session.flush()
        return allocation

    def get_allocations_for_payment(
        self, tenant_id: str, payment_id: str
    ) -> Sequence[AllocationRecord]:
        stmt = (
            select(AllocationRecord)
            .where(
                AllocationRecord.tenant_id == tenant_id,
                AllocationRecord.payment_id == payment_id,
            )
            .order_by(AllocationRecord.allocated_at)
        )
        return self.session.scalars(stmt).all()

    def get_allocations_for_invoice(
        self, tenant_id: str, invoice_id: str
    ) -> Sequence[AllocationRecord]:
        stmt = (
            select(AllocationRecord)
            .where(
                AllocationRecord.tenant_id == tenant_id,
                AllocationRecord.invoice_id == invoice_id,
            )
            .order_by(AllocationRecord.allocated_at)
        )
        return self.session.scalars(stmt).all()

    def get_all_allocations(self, tenant_id: str) -> Sequence[AllocationRecord]:
        stmt = (
            select(AllocationRecord)
            .where(AllocationRecord.tenant_id == tenant_id)
            .order_by(AllocationRecord.allocated_at)
        )
        return self.session.scalars(stmt).all()

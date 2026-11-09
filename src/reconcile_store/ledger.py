"""Authoritative per-payment ledger position.

Every capacity figure (matching, verification, approval, dashboards) comes from
this one calculation so that they cannot disagree. All amounts are integer minor
units in the payment's currency.

Semantics for one payment:

* Applied lines are the application allocations and provider settlements on each
  invoice. An allocation and a settlement reporting the same payment/invoice pair
  are the same money and count once.
* Only completed returns count. Provider return IDs are unique per tenant, so a
  repeated provider event cannot add capacity.
* A return that names an allocation or settlement of this payment reverses that
  line. A return that names an invoice reverses this payment's line on that
  invoice only if the line existed when the return happened; a return recorded
  before the money was applied was already taken out of the available cash.
* Every other return comes out of unapplied cash first. Any excess then reverses
  applied lines oldest first, so each returned cent is restored exactly once.
* A return or applied line in a different currency cannot be netted, so the
  payment fails closed with zero unapplied cash.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Optional

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from reconcile.models import Refund

from .schema import (
    AllocationRecord,
    InvoiceRecord,
    PaymentRecord,
    ProviderSettlementRecord,
    RefundRecord,
)

COMPLETED_RETURN_STATUSES = frozenset({"COMPLETED", "SUCCESS", "SETTLED"})

_EPOCH = datetime.min.replace(tzinfo=timezone.utc)


def is_completed_return(record: RefundRecord) -> bool:
    return (record.status or "").strip().upper() in COMPLETED_RETURN_STATUSES


def _utc(value: Optional[datetime]) -> datetime:
    if value is None:
        return _EPOCH
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


@dataclass
class AppliedLine:
    """The portion of one payment applied to one invoice."""

    invoice_id: str
    amount_minor: int
    applied_at: datetime
    allocation_ids: set[str] = field(default_factory=set)
    settlement_ids: set[str] = field(default_factory=set)
    returned_minor: int = 0

    @property
    def net_minor(self) -> int:
        return self.amount_minor - self.returned_minor

    def take(self, amount_minor: int) -> int:
        taken = min(self.net_minor, amount_minor)
        self.returned_minor += taken
        return taken


@dataclass(frozen=True)
class PaymentLedger:
    """Current position of one payment."""

    payment_id: str
    gross_minor: int
    returned_minor: int
    unapplied_minor: int
    over_returned_minor: int
    currency_conflict: bool
    lines: tuple[AppliedLine, ...]

    @property
    def net_cash_minor(self) -> int:
        return max(0, self.gross_minor - self.returned_minor)

    @property
    def net_applied_minor(self) -> int:
        return sum(line.net_minor for line in self.lines)

    def line_for_invoice(self, invoice_id: str) -> Optional[AppliedLine]:
        return next((line for line in self.lines if line.invoice_id == invoice_id), None)


def compute_payment_ledger(
    session: Session, tenant_id: str, payment: PaymentRecord
) -> PaymentLedger:
    """Compute the payment's position from current database records."""
    allocations = session.scalars(
        select(AllocationRecord).where(
            AllocationRecord.tenant_id == tenant_id,
            AllocationRecord.payment_id == payment.id,
        )
    ).all()
    settlements = session.scalars(
        select(ProviderSettlementRecord).where(
            ProviderSettlementRecord.tenant_id == tenant_id,
            or_(
                ProviderSettlementRecord.payment_id == payment.id,
                ProviderSettlementRecord.provider_settlement_id == payment.provider_payment_id,
            ),
        )
    ).all()
    returns = sorted(
        (
            r
            for r in session.scalars(
                select(RefundRecord).where(
                    RefundRecord.tenant_id == tenant_id,
                    RefundRecord.parent_payment_id.in_([payment.id, payment.provider_payment_id]),
                )
            ).all()
            if is_completed_return(r)
        ),
        key=lambda r: (_utc(r.refunded_at), r.id),
    )

    allocated_by_invoice: dict[str, int] = {}
    settled_by_invoice: dict[str, int] = {}
    lines_by_invoice: dict[str, AppliedLine] = {}

    def line_for(invoice_id: str, applied_at: datetime) -> AppliedLine:
        line = lines_by_invoice.setdefault(invoice_id, AppliedLine(invoice_id, 0, applied_at))
        line.applied_at = max(line.applied_at, applied_at)
        return line

    for allocation in allocations:
        line = line_for(allocation.invoice_id, _utc(allocation.allocated_at))
        line.allocation_ids.add(allocation.id)
        allocated_by_invoice[allocation.invoice_id] = (
            allocated_by_invoice.get(allocation.invoice_id, 0) + allocation.amount_minor
        )
    for settlement in settlements:
        line = line_for(settlement.invoice_id, _utc(settlement.settled_at or settlement.created_at))
        line.settlement_ids.add(settlement.id)
        settled_by_invoice[settlement.invoice_id] = (
            settled_by_invoice.get(settlement.invoice_id, 0) + settlement.amount_minor
        )
    for invoice_id, line in lines_by_invoice.items():
        line.amount_minor = max(
            allocated_by_invoice.get(invoice_id, 0), settled_by_invoice.get(invoice_id, 0)
        )

    lines = sorted(lines_by_invoice.values(), key=lambda line: (line.applied_at, line.invoice_id))

    currency_conflict = any(
        (record.currency or "").strip().upper() != payment.currency
        for record in (*allocations, *settlements)
    )
    returned_minor = 0
    unlinked_minor = 0
    for ret in returns:
        if (ret.currency or "").strip().upper() != payment.currency:
            currency_conflict = True
            continue
        returned_minor += ret.amount_minor
        target = _linked_line(ret, lines)
        taken = target.take(ret.amount_minor) if target is not None else 0
        unlinked_minor += ret.amount_minor - taken

    cash_not_applied = max(0, payment.amount_minor - sum(line.amount_minor for line in lines))
    absorbed_by_cash = min(unlinked_minor, cash_not_applied)
    excess = unlinked_minor - absorbed_by_cash
    for line in lines:
        excess -= line.take(excess)

    return PaymentLedger(
        payment_id=payment.id,
        gross_minor=payment.amount_minor,
        returned_minor=returned_minor,
        unapplied_minor=0 if currency_conflict else cash_not_applied - absorbed_by_cash,
        over_returned_minor=excess,
        currency_conflict=currency_conflict,
        lines=tuple(lines),
    )


def _linked_line(ret: RefundRecord, lines: Iterable[AppliedLine]) -> Optional[AppliedLine]:
    """Find the applied line this return explicitly reverses, if any."""
    lines = list(lines)
    for line in lines:
        if ret.allocation_id in line.allocation_ids or ret.settlement_id in line.settlement_ids:
            return line
    for line in lines:
        if ret.invoice_id == line.invoice_id and _utc(ret.refunded_at) >= line.applied_at:
            return line
    return None


def effective_domain_returns(
    session: Session, tenant_id: str, payments: Iterable[PaymentRecord]
) -> list[Refund]:
    """Express each payment's ledger as the returns that actually reverse applied money.

    The domain matcher and verifier restore invoice balance from the returns they
    are given. Handing them these amounts keeps their invoice balances identical to
    the store's, including where unapplied cash absorbed a return.
    """
    effective: list[Refund] = []
    for payment in payments:
        ledger = compute_payment_ledger(session, tenant_id, payment)
        for line in ledger.lines:
            if line.returned_minor <= 0:
                continue
            invoice = session.get(InvoiceRecord, line.invoice_id)
            if invoice is None or invoice.tenant_id != tenant_id:
                continue
            effective.append(
                Refund(
                    refund_id=f"ledger:{payment.id}:{invoice.id}",
                    parent_payment_id=payment.provider_payment_id,
                    amount_cents=line.returned_minor,
                    currency=payment.currency,
                    refunded_at=line.applied_at,
                    status="COMPLETED",
                    invoice_id=invoice.provider_invoice_id,
                )
            )
    return effective

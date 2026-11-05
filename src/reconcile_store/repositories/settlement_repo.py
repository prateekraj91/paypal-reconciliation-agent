"""Provider settlement repository managing externally reported invoice settlements."""

from __future__ import annotations

from typing import Optional, Sequence, Any
from datetime import datetime, timezone
from sqlalchemy import select, func

from .base import BaseRepository, EntityNotFoundError
from ..schema import ProviderSettlementRecord, PaymentRecord, InvoiceRecord
from ..canonical import canonical_json


class ProviderSettlementRepository(BaseRepository[ProviderSettlementRecord]):
    """Repository managing externally confirmed provider settlement facts."""

    def upsert_settlement(
        self,
        tenant_id: str,
        provider: str,
        environment: str,
        provider_settlement_id: str,
        invoice_id: str,
        amount_minor: int,
        currency: str,
        payment_id: Optional[str] = None,
        source_record_id: Optional[str] = None,
        settled_at: Optional[datetime] = None,
        evidence: Optional[Sequence[Any]] = None,
    ) -> ProviderSettlementRecord:
        """Idempotently insert or retrieve a provider settlement record."""
        # Find existing by unique constraint: (tenant_id, provider, environment, invoice_id, provider_settlement_id)
        stmt = select(ProviderSettlementRecord).where(
            ProviderSettlementRecord.tenant_id == tenant_id,
            ProviderSettlementRecord.provider == provider,
            ProviderSettlementRecord.environment == environment,
            ProviderSettlementRecord.invoice_id == invoice_id,
            ProviderSettlementRecord.provider_settlement_id == provider_settlement_id,
        )
        existing = self.session.scalars(stmt).first()
        if existing is not None:
            # Update payment_id or source_record_id if they were previously missing
            updated = False
            if payment_id and not existing.payment_id:
                existing.payment_id = payment_id
                updated = True
            if source_record_id and not existing.source_record_id:
                existing.source_record_id = source_record_id
                updated = True
            if updated:
                self.session.flush()
            return existing

        evidence_str = canonical_json(list(evidence)) if evidence is not None else canonical_json(["PROVIDER_SETTLED"])

        settlement = ProviderSettlementRecord(
            tenant_id=tenant_id,
            provider=provider,
            environment=environment,
            provider_settlement_id=provider_settlement_id,
            invoice_id=invoice_id,
            payment_id=payment_id,
            amount_minor=amount_minor,
            currency=currency.upper(),
            source_record_id=source_record_id,
            settled_at=settled_at or datetime.now(timezone.utc),
            evidence_json=evidence_str,
            created_at=datetime.now(timezone.utc),
        )
        self.session.add(settlement)
        self.session.flush()
        return settlement

    def get_settlement(self, tenant_id: str, settlement_id: str) -> Optional[ProviderSettlementRecord]:
        settlement = self.session.get(ProviderSettlementRecord, settlement_id)
        if settlement and settlement.tenant_id == tenant_id:
            return settlement
        return None

    def get_by_provider_settlement_id(
        self,
        tenant_id: str,
        provider: str,
        environment: str,
        invoice_id: str,
        provider_settlement_id: str,
    ) -> Optional[ProviderSettlementRecord]:
        stmt = select(ProviderSettlementRecord).where(
            ProviderSettlementRecord.tenant_id == tenant_id,
            ProviderSettlementRecord.provider == provider,
            ProviderSettlementRecord.environment == environment,
            ProviderSettlementRecord.invoice_id == invoice_id,
            ProviderSettlementRecord.provider_settlement_id == provider_settlement_id,
        )
        return self.session.scalars(stmt).first()

    def get_settlements_for_invoice(
        self,
        tenant_id: str,
        invoice_id: str,
    ) -> Sequence[ProviderSettlementRecord]:
        stmt = (
            select(ProviderSettlementRecord)
            .where(
                ProviderSettlementRecord.tenant_id == tenant_id,
                ProviderSettlementRecord.invoice_id == invoice_id,
            )
            .order_by(ProviderSettlementRecord.created_at)
        )
        return self.session.scalars(stmt).all()

    def get_settlements_for_payment(
        self,
        tenant_id: str,
        payment_id: str,
    ) -> Sequence[ProviderSettlementRecord]:
        stmt = (
            select(ProviderSettlementRecord)
            .where(
                ProviderSettlementRecord.tenant_id == tenant_id,
                ProviderSettlementRecord.payment_id == payment_id,
            )
            .order_by(ProviderSettlementRecord.created_at)
        )
        return self.session.scalars(stmt).all()

    def get_all_settlements(self, tenant_id: str) -> Sequence[ProviderSettlementRecord]:
        stmt = (
            select(ProviderSettlementRecord)
            .where(ProviderSettlementRecord.tenant_id == tenant_id)
            .order_by(ProviderSettlementRecord.created_at)
        )
        return self.session.scalars(stmt).all()

    def get_total_settled_minor_for_invoice(self, tenant_id: str, invoice_id: str) -> int:
        stmt = select(func.coalesce(func.sum(ProviderSettlementRecord.amount_minor), 0)).where(
            ProviderSettlementRecord.tenant_id == tenant_id,
            ProviderSettlementRecord.invoice_id == invoice_id,
        )
        return int(self.session.scalar(stmt) or 0)

    def get_total_settled_minor_for_payment(self, tenant_id: str, payment_id: str) -> int:
        stmt = select(func.coalesce(func.sum(ProviderSettlementRecord.amount_minor), 0)).where(
            ProviderSettlementRecord.tenant_id == tenant_id,
            ProviderSettlementRecord.payment_id == payment_id,
        )
        return int(self.session.scalar(stmt) or 0)

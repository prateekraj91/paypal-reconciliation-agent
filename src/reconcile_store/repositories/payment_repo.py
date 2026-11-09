"""Payment repository with unapplied cash calculation and eligibility enforcement."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence
from datetime import datetime, timezone
import uuid
from sqlalchemy import select, func
from sqlalchemy.orm import Session

from .base import BaseRepository, EntityNotFoundError, ConflictError
from ..ledger import PaymentLedger, compute_payment_ledger
from ..schema import PaymentRecord, AllocationRecord, ProviderRecord

# A provider source record is identified by (kind, provider id, content hash): one
# genuine transaction has one record per provider view and per revision of that view.
# The canonical payment is identified by the provider transaction id alone.
SOURCE_UNSPECIFIED = "payment"
SOURCE_TRANSACTION_SEARCH = "payment.transaction_search"
RECORD_KIND_NON_PAYMENT_EVENT = "account_event.transaction_search"
SOURCE_WEBHOOK_CAPTURE = "payment.webhook_capture"
SOURCE_INVOICE_EMBEDDED = "payment.invoice_embedded"

# Which view is trusted for descriptive fields, independent of arrival order.
_SOURCE_RANK = {
    SOURCE_UNSPECIFIED: 0,
    SOURCE_INVOICE_EMBEDDED: 10,
    SOURCE_WEBHOOK_CAPTURE: 20,
    SOURCE_TRANSACTION_SEARCH: 30,
}
_GROSS_SOURCES = frozenset({SOURCE_WEBHOOK_CAPTURE, SOURCE_TRANSACTION_SEARCH})

# Labels written when a view has no payer. They are not information about the payer.
SETTLED_PAYER_PLACEHOLDER = "PayPal Settled Payment"
_PAYER_PLACEHOLDERS = frozenset({SETTLED_PAYER_PLACEHOLDER, "PayPal Customer"})


class IneligiblePaymentError(Exception):
    """Raised when a payment is ineligible for allocation (e.g. PENDING, DENIED, REFUNDED)."""


class PaymentSourceConflictError(ConflictError):
    """Two views of one provider transaction disagree about immutable money facts."""


@dataclass(frozen=True)
class PaymentObservation:
    """What one provider view reports about one transaction."""

    provider_payment_id: str
    amount_minor: int
    currency: str
    source_view: str = SOURCE_UNSPECIFIED
    status: Optional[str] = None
    payer_name: str = ""
    payer_email: Optional[str] = None
    reference: Optional[str] = None
    note: str = ""
    received_at: Optional[datetime] = None
    source_record_id: Optional[str] = None


def _carries_information(field_name: str, value: object) -> bool:
    if value is None or (isinstance(value, str) and not value.strip()):
        return False
    return not (field_name == "payer_name" and value in _PAYER_PLACEHOLDERS)


def _same_value(current: object, incoming: object) -> bool:
    if isinstance(current, datetime) and isinstance(incoming, datetime):
        current = current if current.tzinfo else current.replace(tzinfo=timezone.utc)
        incoming = incoming if incoming.tzinfo else incoming.replace(tzinfo=timezone.utc)
    return current == incoming


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

    def merge_observation(
        self, tenant_id: str, provider: str, observation: PaymentObservation
    ) -> tuple[PaymentRecord, tuple[str, ...]]:
        """Fold one provider view into the canonical payment; returns it and the fields changed.

        The result does not depend on arrival order: a field is taken from the
        highest-ranked view that actually has a value, so a weaker view only fills
        gaps and an empty value never erases anything. Currency and gross amount
        are immutable and fail closed when views disagree.
        """
        obs = observation
        currency = (obs.currency or "").strip().upper()
        existing = self.session.scalars(
            select(PaymentRecord)
            .where(
                PaymentRecord.tenant_id == tenant_id,
                PaymentRecord.provider == provider,
                PaymentRecord.provider_payment_id == obs.provider_payment_id,
            )
            .with_for_update()
        ).first()
        if existing is None:
            created = self.upsert_payment(
                tenant_id=tenant_id,
                provider=provider,
                provider_payment_id=obs.provider_payment_id,
                amount_minor=obs.amount_minor,
                currency=currency,
                status=obs.status or "COMPLETED",
                payer_name=obs.payer_name,
                payer_email=obs.payer_email,
                reference=obs.reference,
                note=obs.note,
                received_at=obs.received_at,
                source_record_id=obs.source_record_id,
            )
            return created, ()

        stored_views = set(
            self.session.scalars(
                select(ProviderRecord.record_kind)
                .where(
                    ProviderRecord.tenant_id == tenant_id,
                    ProviderRecord.provider == provider,
                    ProviderRecord.provider_record_id == obs.provider_payment_id,
                )
                .distinct()
            ).all()
        )
        self._assert_financial_identity(existing, obs, currency, stored_views)

        rank = _SOURCE_RANK[obs.source_view]
        outranked = any(_SOURCE_RANK.get(v, 0) > rank for v in stored_views)
        changed: list[str] = []
        if (
            obs.source_view == SOURCE_INVOICE_EMBEDDED
            and obs.amount_minor > existing.amount_minor
        ):
            existing.amount_minor = obs.amount_minor
            changed.append("amount_minor")
        for name in ("status", "payer_name", "payer_email", "reference", "note", "received_at"):
            incoming = getattr(obs, name)
            if not _carries_information(name, incoming):
                continue
            current = getattr(existing, name)
            if _same_value(current, incoming):
                continue
            if not outranked or not _carries_information(name, current):
                setattr(existing, name, incoming)
                changed.append(name)
        if obs.source_record_id and existing.source_record_id != obs.source_record_id:
            if not outranked or existing.source_record_id is None:
                existing.source_record_id = obs.source_record_id
                changed.append("source_record_id")
        if changed:
            existing.version += 1
            existing.updated_at = datetime.now(timezone.utc)
            self.session.flush()
        return existing, tuple(changed)

    @staticmethod
    def _assert_financial_identity(
        existing: PaymentRecord,
        obs: PaymentObservation,
        currency: str,
        stored_views: set[str],
    ) -> None:
        if existing.currency != currency:
            raise PaymentSourceConflictError(
                f"Cannot change currency of payment '{obs.provider_payment_id}': source "
                f"'{obs.source_view}' reports '{currency}' but the canonical payment is "
                f"'{existing.currency}'. Refusing to merge."
            )
        if obs.amount_minor == existing.amount_minor:
            return
        if obs.source_view == SOURCE_INVOICE_EMBEDDED:
            has_gross = bool(stored_views & _GROSS_SOURCES)
            only_embedded = SOURCE_INVOICE_EMBEDDED in stored_views and not has_gross
            if only_embedded and obs.amount_minor > existing.amount_minor:
                return  # another invoice's share of a split payment arrived later
            if not has_gross and obs.amount_minor < existing.amount_minor:
                return  # a settlement against a payment with no recorded gross view
        raise PaymentSourceConflictError(
            f"Conflicting payment amount for '{obs.provider_payment_id}': source "
            f"'{obs.source_view}' reports {obs.amount_minor} minor units but the canonical "
            f"payment has {existing.amount_minor} minor units ({existing.currency}). "
            f"Financial payment identity cannot be silently mutated (I5)."
        )

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

    def get_ledger(self, tenant_id: str, payment_id: str) -> PaymentLedger:
        """Current authoritative ledger position of this payment."""
        payment = self.get_payment(tenant_id, payment_id)
        if payment is None:
            raise EntityNotFoundError(f"Payment '{payment_id}' not found for tenant '{tenant_id}'")
        return compute_payment_ledger(self.session, tenant_id, payment)

    def get_unapplied_cash_minor(self, tenant_id: str, payment_id: str) -> int:
        """Cash still available to allocate after applied lines and returns (I2)."""
        return self.get_ledger(tenant_id, payment_id).unapplied_minor

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
                f"(gross {payment.amount_minor} minor units)"
            )

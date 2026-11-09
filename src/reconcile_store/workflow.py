"""Persistent storage-backed reconciliation workflow.

Bridges canonical domain reconciliation (reconcile.workflow) with the
relational persistence layer (reconcile_store).
"""

from __future__ import annotations

from typing import Optional, Sequence, Any, Dict, List, Tuple
from dataclasses import dataclass
from datetime import datetime, timezone
import uuid
import hashlib

from sqlalchemy.orm import Session

from .schema import (
    TenantRecord,
    ProviderRecord,
    InvoiceRecord,
    PaymentRecord,
    ReconciliationRunRecord,
    ProposalRecord,
    ProposalAllocationRecord,
    VerificationRecord,
    AllocationRecord,
    ProviderSettlementRecord,
    RefundRecord,
    InvestigationRecord,
)
from .repositories import (
    TenantRepository,
    ProviderRecordRepository,
    InvoiceRepository,
    PaymentRepository,
    ReconciliationRunRepository,
    ProposalRepository,
    VerificationRepository,
    AllocationRepository,
    ProviderSettlementRepository,
    RefundRepository,
    InvestigationRepository,
    AuditRepository,
    PaymentObservation,
    SETTLED_PAYER_PLACEHOLDER,
    RECORD_KIND_NON_PAYMENT_EVENT,
    SOURCE_INVOICE_EMBEDDED,
    SOURCE_UNSPECIFIED,
)
from .approval import ApprovalService, ApprovalResult
from .canonical import canonical_json
from .ledger import compute_payment_ledger, effective_domain_returns

from reconcile.interfaces import Matcher
from reconcile.deterministic import DeterministicMatcher
from reconcile.advanced import AdvancedMatcher
from reconcile.models import (
    Invoice,
    Payment,
    PaymentAllocation,
    PaymentReversal,
    AllocationStatus,
    MatchOutcome,
    MatchProposal,
    Refund,
)
from reconcile.workflow import reconcile_payments, ReconciliationRunResult


@dataclass(frozen=True)
class IngestionResult:
    """Summary of ingested invoices and payments."""

    invoices_ingested: int
    payments_ingested: int
    provider_records_ingested: int


def _embedded_payment_time(raw: Dict[str, Any]) -> Optional[datetime]:
    """When the invoice view says the payment happened, if it says so readably."""
    for key, suffix in (("payment_date_time", ""), ("payment_date", "T00:00:00+00:00")):
        value = raw.get(key)
        if not value:
            continue
        try:
            parsed = datetime.fromisoformat(f"{value}{suffix}".replace("Z", "+00:00"))
        except (TypeError, ValueError):
            continue
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


class StorageWorkflow:
    """Workflow coordinating data ingestion, deterministic reconciliation runs, and persistence."""

    def __init__(self, session: Session) -> None:
        self.session = session
        self.tenant_repo = TenantRepository(session)
        self.provider_record_repo = ProviderRecordRepository(session)
        self.invoice_repo = InvoiceRepository(session)
        self.payment_repo = PaymentRepository(session)
        self.run_repo = ReconciliationRunRepository(session)
        self.proposal_repo = ProposalRepository(session)
        self.verification_repo = VerificationRepository(session)
        self.allocation_repo = AllocationRepository(session)
        self.settlement_repo = ProviderSettlementRepository(session)
        self.refund_repo = RefundRepository(session)
        self.investigation_repo = InvestigationRepository(session)
        self.audit_repo = AuditRepository(session)
        self.approval_service = ApprovalService(session)

    def ingest_invoices(
        self,
        tenant_id: str,
        invoices: Sequence[Invoice],
        raw_payloads: Optional[Dict[str, Dict[str, Any]]] = None,
        provider: str = "paypal",
    ) -> List[InvoiceRecord]:
        """Ingest canonical domain invoices, optionally preserving original provider raw payloads."""
        results: List[InvoiceRecord] = []
        for inv in invoices:
            source_rec_id: Optional[str] = None
            if raw_payloads and inv.invoice_id in raw_payloads:
                raw_record = self.provider_record_repo.store_record(
                    tenant_id=tenant_id,
                    provider=provider,
                    record_kind="invoice",
                    provider_record_id=inv.invoice_id,
                    raw_payload=raw_payloads[inv.invoice_id],
                    # An invoice legitimately changes (for example when paid); each revision is kept.
                    fail_on_conflict=False,
                )
                source_rec_id = raw_record.id

            inv_rec = self.invoice_repo.upsert_invoice(
                tenant_id=tenant_id,
                provider=provider,
                provider_invoice_id=inv.invoice_id,
                invoice_number=inv.number,
                total_minor=inv.amount_cents,
                currency=inv.currency,
                provider_due_minor=inv.due_amount_cents,
                status=inv.status.value,
                customer_name=inv.customer_name,
                customer_email=inv.customer_email,
                issued_on=inv.issued_on,
                due_on=inv.due_on,
                memo=inv.memo,
                source_record_id=source_rec_id,
            )
            results.append(inv_rec)

        self.session.commit()
        return results

    def ingest_payments(
        self,
        tenant_id: str,
        payments: Sequence[Payment],
        raw_payloads: Optional[Dict[str, Dict[str, Any]]] = None,
        provider: str = "paypal",
        source_view: str = SOURCE_UNSPECIFIED,
    ) -> List[PaymentRecord]:
        """Ingest canonical domain payments, preserving original provider raw evidence."""
        results: List[PaymentRecord] = []
        for pay in payments:
            source_rec_id = self._store_payment_source(
                tenant_id, provider, source_view, pay.payment_id, raw_payloads
            )
            pay_rec = self._merge_payment(
                tenant_id,
                provider,
                PaymentObservation(
                    provider_payment_id=pay.payment_id,
                    amount_minor=pay.amount_cents,
                    currency=pay.currency,
                    source_view=source_view,
                    status=pay.status.value,
                    payer_name=pay.payer_name,
                    payer_email=pay.payer_email,
                    reference=pay.reference,
                    note=pay.note,
                    received_at=pay.received_at,
                    source_record_id=source_rec_id,
                ),
            )
            results.append(pay_rec)

        self.session.commit()
        return results

    def record_non_payment_events(
        self,
        tenant_id: str,
        raw_payloads: Dict[str, Dict[str, Any]],
        provider: str = "paypal",
    ) -> List[ProviderRecord]:
        """Keep the evidence for provider account events without creating payments.

        These records never reach the payments table, so they cannot enter
        unapplied cash, matching, or the review queue.
        """
        records = [
            self.provider_record_repo.store_record(
                tenant_id=tenant_id,
                provider=provider,
                record_kind=RECORD_KIND_NON_PAYMENT_EVENT,
                provider_record_id=provider_event_id,
                raw_payload=raw_payload,
                fail_on_conflict=False,
            )
            for provider_event_id, raw_payload in raw_payloads.items()
        ]
        self.session.commit()
        return records

    def _store_payment_source(
        self,
        tenant_id: str,
        provider: str,
        source_view: str,
        provider_payment_id: str,
        raw_payloads: Optional[Dict[str, Dict[str, Any]]],
    ) -> Optional[str]:
        """Keep this view's evidence. A changed payload is a new revision, never an overwrite."""
        if not raw_payloads or provider_payment_id not in raw_payloads:
            return None
        return self.provider_record_repo.store_record(
            tenant_id=tenant_id,
            provider=provider,
            record_kind=source_view,
            provider_record_id=provider_payment_id,
            raw_payload=raw_payloads[provider_payment_id],
            fail_on_conflict=source_view == SOURCE_UNSPECIFIED,
        ).id

    def _merge_payment(
        self, tenant_id: str, provider: str, observation: PaymentObservation
    ) -> PaymentRecord:
        payment, changed = self.payment_repo.merge_observation(tenant_id, provider, observation)
        if changed:
            self.audit_repo.append_event(
                tenant_id=tenant_id,
                event_type="PAYMENT_SOURCE_MERGED",
                actor=f"{provider}:ingestion",
                subject_type="payment",
                subject_id=payment.id,
                details={
                    "provider_payment_id": payment.provider_payment_id,
                    "source_view": observation.source_view,
                    "source_record_id": observation.source_record_id,
                    "fields_changed": sorted(changed),
                },
            )
        return payment

    def ingest_provider_settlements(
        self,
        tenant_id: str,
        allocations: Sequence[PaymentAllocation],
        raw_payloads: Optional[Dict[str, Dict[str, Any]]] = None,
        provider: str = "paypal",
        environment: str = "sandbox",
    ) -> List[ProviderSettlementRecord]:
        """Ingest externally reported invoice settlements exclusively into provider_settlements.

        Preserves provider settlement facts without misrepresenting them as operator approvals.
        Ensures raw evidence is preserved in provider_records.
        """
        results: List[ProviderSettlementRecord] = []
        for alloc in allocations:
            inv = self.invoice_repo.get_by_provider_id(tenant_id, provider, alloc.invoice_id)
            if inv is None:
                continue

            # The invoice view reports each invoice's share, so a payment split across
            # invoices is compared to its gross as the total of those shares.
            raw = (raw_payloads or {}).get(alloc.payment_id) or {}
            source_rec_id = self._store_payment_source(
                tenant_id, provider, SOURCE_INVOICE_EMBEDDED, alloc.payment_id, raw_payloads
            )
            pay = self._merge_payment(
                tenant_id,
                provider,
                PaymentObservation(
                    provider_payment_id=alloc.payment_id,
                    amount_minor=self._settled_total_minor(
                        tenant_id, provider, environment, alloc, allocations
                    ),
                    currency=alloc.currency or inv.currency or "USD",
                    source_view=SOURCE_INVOICE_EMBEDDED,
                    status="COMPLETED",
                    payer_name=SETTLED_PAYER_PLACEHOLDER,
                    reference=inv.invoice_number,
                    note=str(raw.get("note") or ""),
                    received_at=_embedded_payment_time(raw),
                    source_record_id=source_rec_id,
                ),
            )

            already_recorded = self.settlement_repo.find_settlement(
                tenant_id, provider, environment, inv.id, alloc.payment_id
            )

            # Upsert into dedicated provider_settlements table
            settlement = self.settlement_repo.upsert_settlement(
                tenant_id=tenant_id,
                provider=provider,
                environment=environment,
                provider_settlement_id=alloc.payment_id,
                invoice_id=inv.id,
                payment_id=pay.id if pay else None,
                amount_minor=alloc.amount_cents,
                currency=alloc.currency or inv.currency,
                source_record_id=source_rec_id or (pay.source_record_id if pay else None),
                evidence=["PROVIDER_SETTLED"],
            )
            results.append(settlement)
            if already_recorded is not None:
                continue

            # Record audit event for settlement ingestion without operator approval misrepresentation
            self.audit_repo.append_event(
                tenant_id=tenant_id,
                event_type="PROVIDER_SETTLEMENT_RECORDED",
                actor=f"{provider}:settlement",
                subject_type="provider_settlement",
                subject_id=settlement.id,
                details={
                    "provider": provider,
                    "environment": environment,
                    "provider_settlement_id": settlement.provider_settlement_id,
                    "invoice_id": inv.id,
                    "provider_invoice_id": inv.provider_invoice_id,
                    "payment_id": pay.id if pay else None,
                    "provider_payment_id": alloc.payment_id,
                    "amount_minor": alloc.amount_cents,
                    "currency": alloc.currency or inv.currency,
                },
            )

        self.session.commit()
        return results

    def _settled_total_minor(
        self,
        tenant_id: str,
        provider: str,
        environment: str,
        alloc: PaymentAllocation,
        batch: Sequence[PaymentAllocation],
    ) -> int:
        """Total of one payment's invoice shares: this batch plus shares already on record."""
        batch_invoice_rows = {
            a.invoice_id: a.amount_cents for a in batch if a.payment_id == alloc.payment_id
        }
        batch_ids = {
            inv.id: inv.provider_invoice_id
            for inv in (
                self.invoice_repo.get_by_provider_id(tenant_id, provider, provider_id)
                for provider_id in batch_invoice_rows
            )
            if inv is not None
        }
        stored = self.settlement_repo.list_for_provider_payment(
            tenant_id, provider, environment, alloc.payment_id
        )
        return sum(batch_invoice_rows[pid] for pid in batch_ids.values()) + sum(
            s.amount_minor for s in stored if s.invoice_id not in batch_ids
        )

    def ingest_provider_allocations(
        self,
        tenant_id: str,
        allocations: Sequence[PaymentAllocation],
        raw_payloads: Optional[Dict[str, Dict[str, Any]]] = None,
        provider: str = "paypal",
        environment: str = "sandbox",
    ) -> List[ProviderSettlementRecord]:
        """Backward-compatible alias routing provider settlements to dedicated table."""
        return self.ingest_provider_settlements(
            tenant_id=tenant_id,
            allocations=allocations,
            raw_payloads=raw_payloads,
            provider=provider,
            environment=environment,
        )

    def ingest_refunds(
        self,
        tenant_id: str,
        refunds: Sequence[Refund],
        raw_payloads: Optional[Dict[str, Dict[str, Any]]] = None,
        provider: str = "paypal",
    ) -> List[RefundRecord]:
        """Ingest external refunds or payment reversals into dedicated refunds table."""
        results: List[RefundRecord] = []
        for ref in refunds:
            source_rec_id: Optional[str] = None
            if raw_payloads and ref.refund_id in raw_payloads:
                raw_rec = self.provider_record_repo.store_record(
                    tenant_id=tenant_id,
                    provider=provider,
                    record_kind="refund",
                    provider_record_id=ref.refund_id,
                    raw_payload=raw_payloads[ref.refund_id],
                )
                source_rec_id = raw_rec.id

            parent_pay = self.payment_repo.get_by_provider_id(tenant_id, provider, ref.parent_payment_id)
            if parent_pay is None:
                parent_pay = self.payment_repo.upsert_payment(
                    tenant_id=tenant_id,
                    provider=provider,
                    provider_payment_id=ref.parent_payment_id,
                    amount_minor=ref.amount_cents,
                    currency=ref.currency,
                    status="COMPLETED",
                    payer_name="Refunded Payment Placeholder",
                )

            inv_rec = None
            if ref.invoice_id:
                inv_rec = self.invoice_repo.get_by_provider_id(tenant_id, provider, ref.invoice_id)

            refund_rec = self.refund_repo.upsert_refund(
                tenant_id=tenant_id,
                provider=provider,
                provider_refund_id=ref.refund_id,
                parent_payment_id=parent_pay.id,
                amount_minor=ref.amount_cents,
                currency=ref.currency,
                invoice_id=inv_rec.id if inv_rec else None,
                status=ref.status,
                reason=ref.reason,
                target_type=ref.target_type,
                settlement_id=ref.settlement_id,
                allocation_id=ref.allocation_id,
                refunded_at=ref.refunded_at,
                source_record_id=source_rec_id,
            )
            results.append(refund_rec)

            self.audit_repo.append_event(
                tenant_id=tenant_id,
                event_type="REFUND_RECORDED",
                actor=f"{provider}:refund",
                subject_type="refund",
                subject_id=refund_rec.id,
                details={
                    "provider": provider,
                    "provider_refund_id": refund_rec.provider_refund_id,
                    "parent_payment_id": parent_pay.id,
                    "provider_payment_id": ref.parent_payment_id,
                    "amount_minor": ref.amount_cents,
                    "currency": ref.currency,
                    "invoice_id": inv_rec.id if inv_rec else None,
                },
            )

        self.session.commit()
        return results

    def ingest_reversals(
        self,
        tenant_id: str,
        reversals: Sequence[PaymentReversal],
        raw_payloads: Optional[Dict[str, Dict[str, Any]]] = None,
        provider: str = "paypal",
    ) -> List[RefundRecord]:
        """Ingest involuntary payment reversals (e.g. chargebacks, clawbacks) into dedicated refunds table."""
        results: List[RefundRecord] = []
        for rev in reversals:
            source_rec_id: Optional[str] = None
            if raw_payloads and rev.reversal_id in raw_payloads:
                raw_rec = self.provider_record_repo.store_record(
                    tenant_id=tenant_id,
                    provider=provider,
                    record_kind="reversal",
                    provider_record_id=rev.reversal_id,
                    raw_payload=raw_payloads[rev.reversal_id],
                )
                source_rec_id = raw_rec.id

            parent_pay = self.payment_repo.get_by_provider_id(tenant_id, provider, rev.parent_payment_id)
            if parent_pay is None:
                parent_pay = self.payment_repo.upsert_payment(
                    tenant_id=tenant_id,
                    provider=provider,
                    provider_payment_id=rev.parent_payment_id,
                    amount_minor=rev.amount_cents,
                    currency=rev.currency,
                    status="COMPLETED",
                    payer_name="Reversed Payment Placeholder",
                )

            inv_rec = None
            if rev.invoice_id:
                inv_rec = self.invoice_repo.get_by_provider_id(tenant_id, provider, rev.invoice_id)

            reversal_rec = self.refund_repo.upsert_reversal(
                tenant_id=tenant_id,
                provider=provider,
                provider_reversal_id=rev.reversal_id,
                parent_payment_id=parent_pay.id,
                amount_minor=rev.amount_cents,
                currency=rev.currency,
                invoice_id=inv_rec.id if inv_rec else None,
                status=rev.status,
                reason=rev.reason,
                dispute_id=rev.dispute_id,
                target_type=rev.target_type,
                settlement_id=rev.settlement_id,
                allocation_id=rev.allocation_id,
                reversed_at=rev.reversed_at,
                source_record_id=source_rec_id,
            )
            results.append(reversal_rec)

            self.audit_repo.append_event(
                tenant_id=tenant_id,
                event_type="PAYMENT_REVERSAL_RECORDED",
                actor=f"{provider}:reversal",
                subject_type="reversal",
                subject_id=reversal_rec.id,
                details={
                    "provider": provider,
                    "provider_reversal_id": reversal_rec.provider_refund_id,
                    "parent_payment_id": parent_pay.id,
                    "provider_payment_id": rev.parent_payment_id,
                    "amount_minor": rev.amount_cents,
                    "currency": rev.currency,
                    "invoice_id": inv_rec.id if inv_rec else None,
                    "dispute_id": rev.dispute_id,
                },
            )

        self.session.commit()
        return results

    def run_reconciliation(
        self,
        tenant_id: str,
        matcher: Optional[Matcher] = None,
        matcher_name: str = "deterministic_v1",
        matcher_version: str = "1.0.0",
        config_snapshot: Optional[Dict[str, Any]] = None,
    ) -> Tuple[ReconciliationRunRecord, ReconciliationRunResult]:
        """Execute reconciliation run, run independent verifications, and persist all proposals.

        Crucially: proposals are NEVER automatically approved.
        """
        if matcher:
            active_matcher = matcher
        elif matcher_name in ("deterministic_v2", "advanced-matcher-d5", "advanced_v2", "advanced"):
            active_matcher = AdvancedMatcher()
        else:
            active_matcher = DeterministicMatcher()

        # Load invoices and payments from the store
        db_invoices = list(self.invoice_repo.list_invoices(tenant_id))
        db_payments = list(self.payment_repo.list_payments(tenant_id))

        if not db_invoices or not db_payments:
            # Create a run snapshot even if empty
            snapshot_hash = hashlib.sha256(b"empty").hexdigest()
            run_rec = self.run_repo.create_run(
                tenant_id=tenant_id,
                input_snapshot_id=snapshot_hash,
                matcher_name=matcher_name,
                matcher_version=matcher_version,
                summary_metrics={"total_payments": len(db_payments), "total_invoices": len(db_invoices)},
                config_snapshot=config_snapshot,
            )
            self.audit_repo.append_event(
                tenant_id=tenant_id,
                event_type="RECONCILIATION_RUN_COMPLETED",
                actor=f"matcher:{matcher_name}",
                subject_type="reconciliation_run",
                subject_id=run_rec.id,
                details={
                    "matcher_name": matcher_name,
                    "matcher_version": matcher_version,
                    "input_snapshot_id": snapshot_hash,
                    "total_payments": len(db_payments),
                    "total_invoices": len(db_invoices),
                    "proposals_generated": 0,
                    "verified_proposals": 0,
                },
            )
            self.session.commit()
            empty_result = ReconciliationRunResult(
                invoices=tuple(inv.to_domain() for inv in db_invoices),
                existing_allocations=(),
                proposals=(),
                verification_results=(),
            )
            return run_rec, empty_result

        # Load existing approved ledger allocations and provider settlements separately
        db_allocs = list(self.allocation_repo.get_all_allocations(tenant_id))
        db_settlements = list(self.settlement_repo.get_all_settlements(tenant_id))
        db_refunds = list(self.refund_repo.list_all_refunds(tenant_id))

        domain_approved_allocs: List[PaymentAllocation] = []
        for a in db_allocs:
            pay = self.payment_repo.get_payment(tenant_id, a.payment_id)
            inv = self.invoice_repo.get_invoice(tenant_id, a.invoice_id)
            if pay and inv:
                domain_approved_allocs.append(a.to_domain(pay.provider_payment_id, inv.provider_invoice_id))

        domain_settlements: List[PaymentAllocation] = []
        for s in db_settlements:
            pay = self.payment_repo.get_payment(tenant_id, s.payment_id) if s.payment_id else None
            inv = self.invoice_repo.get_invoice(tenant_id, s.invoice_id)
            pay_id = pay.provider_payment_id if pay else s.provider_settlement_id
            inv_id = inv.provider_invoice_id if inv else ""
            if inv_id:
                domain_settlements.append(s.to_domain(pay_id, inv_id))

        domain_refunds: List[Refund] = []
        domain_reversals: List[PaymentReversal] = []
        for r in db_refunds:
            pay = self.payment_repo.get_payment(tenant_id, r.parent_payment_id)
            inv = self.invoice_repo.get_invoice(tenant_id, r.invoice_id) if r.invoice_id else None
            if pay:
                if getattr(r, "record_type", "REFUND") == "REVERSAL":
                    domain_reversals.append(
                        r.to_reversal_domain(pay.provider_payment_id, inv.provider_invoice_id if inv else None)
                    )
                else:
                    domain_refunds.append(
                        r.to_domain(pay.provider_payment_id, inv.provider_invoice_id if inv else None)
                    )

        # Convert to pure domain models
        domain_invoices = [inv.to_domain() for inv in db_invoices]
        domain_payments = [pay.to_domain() for pay in db_payments]

        # Compute deterministic snapshot hash of all inputs
        snapshot_content = canonical_json({
            "invoices": [inv.invoice_id for inv in domain_invoices],
            "payments": [p.payment_id for p in domain_payments],
            "existing_allocations": [(a.payment_id, a.invoice_id, a.amount_cents) for a in domain_approved_allocs],
            "provider_settlements": [(s.payment_id, s.invoice_id, s.amount_cents) for s in domain_settlements],
            "refunds": [(r.refund_id, r.parent_payment_id, r.amount_cents) for r in domain_refunds],
            "reversals": [(rev.reversal_id, rev.parent_payment_id, rev.amount_cents) for rev in domain_reversals],
        })
        snapshot_hash = hashlib.sha256(snapshot_content.encode("utf-8")).hexdigest()

        # Execute pure reconciliation core
        class _StaticInvoiceSource:
            def __init__(self, invs: Sequence[Invoice]) -> None:
                self._invs = tuple(invs)

            def list_invoices(self) -> Sequence[Invoice]:
                return self._invs

        run_result = reconcile_payments(
            payments=domain_payments,
            invoice_source=_StaticInvoiceSource(domain_invoices),
            matcher=active_matcher,
            existing_allocations=domain_approved_allocs,
            provider_settlements=domain_settlements,
            refunds=effective_domain_returns(self.session, tenant_id, db_payments),
            reversals=(),
            payment_capacity={
                pay.provider_payment_id: compute_payment_ledger(self.session, tenant_id, pay).unapplied_minor
                for pay in db_payments
            },
        )

        # Build ID lookup maps
        inv_map = {inv.provider_invoice_id: inv.id for inv in db_invoices}
        pay_map = {pay.provider_payment_id: pay.id for pay in db_payments}

        # Persist reconciliation run record
        report = run_result.report
        summary_metrics = {
            "total_payments": report.total_payments,
            "total_invoices": report.total_invoices,
            "proposals_count": report.proposals_count,
            "passed_verification_count": report.passed_verification_count,
            "rejected_verification_count": report.rejected_verification_count,
            "review_queue_count": report.review_queue_count,
        }

        run_rec = self.run_repo.create_run(
            tenant_id=tenant_id,
            input_snapshot_id=snapshot_hash,
            matcher_name=matcher_name,
            matcher_version=matcher_version,
            summary_metrics=summary_metrics,
            config_snapshot=config_snapshot,
        )

        # Verification map by payment ID
        ver_map = {v.payment_id: v for v in run_result.verification_results}

        # Persist proposals and their allocations
        for prop in run_result.proposals:
            db_pay_id = pay_map.get(prop.payment_id)
            if not db_pay_id:
                continue

            ver_result = ver_map.get(prop.payment_id)
            ver_status = "VERIFIED" if (ver_result and ver_result.passed) else "UNVERIFIED"

            # Prepare proposal allocations
            alloc_payloads: List[Dict[str, Any]] = []
            if prop.outcome == MatchOutcome.PROPOSED and prop.candidates:
                candidate = prop.candidates[0]
                for ca in candidate.allocations:
                    db_inv_id = inv_map.get(ca.invoice_id)
                    if db_inv_id:
                        alloc_payloads.append({
                            "invoice_id": db_inv_id,
                            "amount_minor": ca.amount_cents,
                            "currency": ca.currency or "USD",
                            "evidence": [e.code.value for e in candidate.evidence] if candidate.evidence else [],
                        })

            # A proposed match keeps its candidate evidence: the approval gate
            # reruns the verifier from these codes. `reasons` only explains it.
            evidence_items = (
                prop.candidates[0].evidence
                if prop.outcome == MatchOutcome.PROPOSED and prop.candidates and prop.candidates[0].evidence
                else prop.reasons
            )
            prop_evidence = [e.code.value if hasattr(e, "code") else str(e) for e in evidence_items]
            prop_rec = self.proposal_repo.create_proposal(
                tenant_id=tenant_id,
                run_id=run_rec.id,
                payment_id=db_pay_id,
                outcome=prop.outcome.value,
                evidence=prop_evidence,
                source_hashes={"snapshot_hash": snapshot_hash},
                verifier_status=ver_status,
                allocations=alloc_payloads,
                excess_amount_minor=getattr(prop, "excess_amount_cents", 0),
                ambiguity_certificate=prop.ambiguity_certificate.to_dict() if prop.ambiguity_certificate else None,
            )

            # Persist verification record
            if ver_result:
                self.verification_repo.record_verification(
                    tenant_id=tenant_id,
                    proposal_id=prop_rec.id,
                    check_name="verify_proposals",
                    check_inputs={"payment_id": prop.payment_id},
                    passed=ver_result.passed,
                    failure_reason="; ".join(ver_result.issues) if ver_result.issues else None,
                    source_hashes={"snapshot_hash": snapshot_hash},
                )

        # Record tamper-evident audit event for reconciliation run execution
        self.audit_repo.append_event(
            tenant_id=tenant_id,
            event_type="RECONCILIATION_RUN_COMPLETED",
            actor=f"matcher:{matcher_name}",
            subject_type="reconciliation_run",
            subject_id=run_rec.id,
            details={
                "matcher_name": matcher_name,
                "matcher_version": matcher_version,
                "input_snapshot_id": snapshot_hash,
                "total_payments": len(domain_payments),
                "total_invoices": len(domain_invoices),
                "proposals_generated": len(run_result.proposals),
                "verified_proposals": sum(1 for v in run_result.verification_results if v.passed),
            },
        )

        self.session.commit()
        return run_rec, run_result

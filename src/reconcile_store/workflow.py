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
    AuditRepository,
)
from .approval import ApprovalService, ApprovalResult
from .canonical import canonical_json

from reconcile.interfaces import Matcher
from reconcile.deterministic import DeterministicMatcher
from reconcile.models import (
    Invoice,
    Payment,
    PaymentAllocation,
    AllocationStatus,
    MatchOutcome,
    MatchProposal,
)
from reconcile.workflow import reconcile_payments, ReconciliationRunResult


@dataclass(frozen=True)
class IngestionResult:
    """Summary of ingested invoices and payments."""

    invoices_ingested: int
    payments_ingested: int
    provider_records_ingested: int


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
    ) -> List[PaymentRecord]:
        """Ingest canonical domain payments, preserving original provider raw evidence."""
        results: List[PaymentRecord] = []
        for pay in payments:
            source_rec_id: Optional[str] = None
            if raw_payloads and pay.payment_id in raw_payloads:
                raw_record = self.provider_record_repo.store_record(
                    tenant_id=tenant_id,
                    provider=provider,
                    record_kind="payment",
                    provider_record_id=pay.payment_id,
                    raw_payload=raw_payloads[pay.payment_id],
                )
                source_rec_id = raw_record.id

            pay_rec = self.payment_repo.upsert_payment(
                tenant_id=tenant_id,
                provider=provider,
                provider_payment_id=pay.payment_id,
                amount_minor=pay.amount_cents,
                currency=pay.currency,
                status=pay.status.value,
                payer_name=pay.payer_name,
                payer_email=pay.payer_email,
                reference=pay.reference,
                note=pay.note,
                received_at=pay.received_at,
                source_record_id=source_rec_id,
            )
            results.append(pay_rec)

        self.session.commit()
        return results

    def ingest_provider_allocations(
        self,
        tenant_id: str,
        allocations: Sequence[PaymentAllocation],
        provider: str = "paypal",
    ) -> List[AllocationRecord]:
        """Ingest pre-existing provider-settled allocations (e.g. PayPal linked payments).

        These represent historical financial facts already settled on the payment provider,
        preventing double deduction and reconciling with provider-reported due_amount.
        """
        results: List[AllocationRecord] = []
        for alloc in allocations:
            pay = self.payment_repo.get_by_provider_id(tenant_id, provider, alloc.payment_id)
            if pay is None:
                pay = self.payment_repo.upsert_payment(
                    tenant_id=tenant_id,
                    provider=provider,
                    provider_payment_id=alloc.payment_id,
                    amount_minor=alloc.amount_cents,
                    currency=alloc.currency or "USD",
                    status="COMPLETED",
                    payer_name="PayPal Settled Payment",
                )

            inv = self.invoice_repo.get_by_provider_id(tenant_id, provider, alloc.invoice_id)
            if inv is None:
                continue

            # Record allocation in ledger if not already recorded
            from sqlalchemy import select
            alloc_stmt = select(AllocationRecord).where(
                AllocationRecord.tenant_id == tenant_id,
                AllocationRecord.payment_id == pay.id,
                AllocationRecord.invoice_id == inv.id,
            )
            existing_alloc = self.session.scalars(alloc_stmt).first()
            if existing_alloc is None:
                ar = AllocationRecord(
                    tenant_id=tenant_id,
                    payment_id=pay.id,
                    invoice_id=inv.id,
                    amount_minor=alloc.amount_cents,
                    currency=alloc.currency or inv.currency,
                    decision_id=None,
                    actor=f"{provider}:settlement",
                    evidence_json=canonical_json(["PROVIDER_SETTLED"]),
                    allocated_at=datetime.now(timezone.utc),
                )
                self.session.add(ar)
                results.append(ar)

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
        active_matcher = matcher or DeterministicMatcher()

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
            self.session.commit()
            empty_result = ReconciliationRunResult(
                invoices=tuple(inv.to_domain() for inv in db_invoices),
                existing_allocations=(),
                proposals=(),
                verification_results=(),
            )
            return run_rec, empty_result

        # Load existing approved ledger allocations
        db_allocs = list(self.allocation_repo.get_all_allocations(tenant_id))
        domain_allocs: List[PaymentAllocation] = []
        for a in db_allocs:
            pay = self.payment_repo.get_payment(tenant_id, a.payment_id)
            inv = self.invoice_repo.get_invoice(tenant_id, a.invoice_id)
            if pay and inv:
                domain_allocs.append(a.to_domain(pay.provider_payment_id, inv.provider_invoice_id))

        # Convert to pure domain models
        domain_invoices = [inv.to_domain() for inv in db_invoices]
        domain_payments = [pay.to_domain() for pay in db_payments]

        # Compute deterministic snapshot hash of all inputs
        snapshot_content = canonical_json({
            "invoices": [inv.invoice_id for inv in domain_invoices],
            "payments": [p.payment_id for p in domain_payments],
            "existing_allocations": [(a.payment_id, a.invoice_id, a.amount_cents) for a in domain_allocs],
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
            existing_allocations=domain_allocs,
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
            if prop.candidates:
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

            prop_evidence = (
                [e.code.value for e in prop.candidates[0].evidence]
                if prop.candidates and prop.candidates[0].evidence
                else []
            )
            prop_rec = self.proposal_repo.create_proposal(
                tenant_id=tenant_id,
                run_id=run_rec.id,
                payment_id=db_pay_id,
                outcome=prop.outcome.value,
                evidence=prop_evidence,
                source_hashes={"snapshot_hash": snapshot_hash},
                verifier_status=ver_status,
                allocations=alloc_payloads,
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

        self.session.commit()
        return run_rec, run_result

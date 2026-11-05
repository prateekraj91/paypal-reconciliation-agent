"""Atomic Approval Engine.

Implements the non-negotiable 10-step atomic approval transaction for reconciliation proposals.
Only this engine can transition a proposal into APPROVED application ledger allocations.
"""

from __future__ import annotations

from typing import Optional, Sequence, Any, Dict, List, Set
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from sqlalchemy import select, func
from sqlalchemy.orm import Session

from .schema import (
    TenantRecord,
    ProposalRecord,
    ProposalAllocationRecord,
    PaymentRecord,
    InvoiceRecord,
    AllocationRecord,
    ProviderSettlementRecord,
    DecisionRecord,
    VerificationRecord,
    AuditEventRecord,
)
from .canonical import canonical_json
from .repositories.base import EntityNotFoundError, ConflictError, StaleVersionError, TenantIsolationError
from .repositories.invoice_repo import IneligibleInvoiceError
from .repositories.payment_repo import IneligiblePaymentError
from .repositories.allocation_repo import LedgerConstraintError
from .repositories.audit_repo import AuditRepository

from reconcile.models import (
    AllocationStatus,
    Candidate,
    Evidence,
    EvidenceCode,
    Invoice,
    MatchOutcome,
    MatchProposal,
    Payment,
    PaymentAllocation,
    PaymentStatus,
)
from reconcile.verifier import verify_proposals, VerificationResult


class ApprovalError(Exception):
    """Base exception for proposal approval errors."""


class VerificationFailedError(ApprovalError):
    """Raised when the independent verifier rejects the proposal during approval."""

    def __init__(self, issues: Sequence[str]) -> None:
        super().__init__(f"Verification failed: {'; '.join(issues)}")
        self.issues = tuple(issues)


@dataclass(frozen=True)
class ApprovalResult:
    """Result of an atomic approval execution."""

    decision_id: str
    proposal_id: str
    status: str
    allocation_ids: tuple[str, ...]
    audit_event_id: str
    is_idempotent_replay: bool


class ApprovalService:
    """Dedicated approval service enforcing the atomic 10-step financial approval lifecycle."""

    def __init__(self, session: Session) -> None:
        self.session = session
        self.audit_repo = AuditRepository(session)

    def approve_proposal(
        self,
        tenant_id: str,
        proposal_id: str,
        actor: str,
        idempotency_key: str,
        expected_payment_version: Optional[int] = None,
        expected_invoice_versions: Optional[Dict[str, int]] = None,
        reason: str = "Manual approval of proposed reconciliation",
    ) -> ApprovalResult:
        """Execute the 10-step atomic approval transaction.

        If any step fails, the entire transaction is rolled back.
        """
        if not actor or not actor.strip():
            raise ValueError("Actor must be specified for approval (accountability)")
        if not idempotency_key or not idempotency_key.strip():
            raise ValueError("Idempotency key is required for approval")

        # ------------------------------------------------------------------
        # Idempotency Check (pre-flight check)
        # ------------------------------------------------------------------
        stmt_dec = select(DecisionRecord).where(
            DecisionRecord.tenant_id == tenant_id,
            DecisionRecord.idempotency_key == idempotency_key,
        )
        existing_decision = self.session.scalars(stmt_dec).first()
        if existing_decision is not None:
            if (
                existing_decision.proposal_id == proposal_id
                and existing_decision.decision_type == "APPROVED"
            ):
                # Retrieve existing allocations created for this decision
                stmt_existing_allocs = select(AllocationRecord).where(
                    AllocationRecord.tenant_id == tenant_id,
                    AllocationRecord.decision_id == existing_decision.id,
                )
                existing_allocs = self.session.scalars(stmt_existing_allocs).all()
                alloc_ids = tuple(a.id for a in existing_allocs)

                # Find the audit event
                stmt_aud = select(AuditEventRecord).where(
                    AuditEventRecord.tenant_id == tenant_id,
                    AuditEventRecord.subject_id == proposal_id,
                    AuditEventRecord.event_type == "PROPOSAL_APPROVED",
                )
                aud_event = self.session.scalars(stmt_aud).first()
                aud_id = aud_event.event_id if aud_event else ""

                return ApprovalResult(
                    decision_id=existing_decision.id,
                    proposal_id=proposal_id,
                    status="APPROVED",
                    allocation_ids=alloc_ids,
                    audit_event_id=aud_id,
                    is_idempotent_replay=True,
                )
            else:
                raise ConflictError(
                    f"Idempotency key '{idempotency_key}' conflict: already used for proposal "
                    f"'{existing_decision.proposal_id}' with decision '{existing_decision.decision_type}'"
                )

        try:
            # ------------------------------------------------------------------
            # STEP 1: Load proposal, payment, and affected invoices
            # ------------------------------------------------------------------
            proposal = self.session.get(ProposalRecord, proposal_id)
            if proposal is None or proposal.tenant_id != tenant_id:
                raise EntityNotFoundError(f"Proposal '{proposal_id}' not found for tenant '{tenant_id}'")

            if proposal.outcome != "PROPOSED":
                raise ApprovalError(
                    f"Cannot approve proposal '{proposal_id}' with outcome '{proposal.outcome}'. Only PROPOSED outcomes are eligible."
                )

            if proposal.verifier_status == "APPROVED":
                raise ConflictError(f"Proposal '{proposal_id}' has already been approved")

            # Load proposed allocations
            stmt_palloc = select(ProposalAllocationRecord).where(
                ProposalAllocationRecord.tenant_id == tenant_id,
                ProposalAllocationRecord.proposal_id == proposal_id,
            )
            proposed_allocs = list(self.session.scalars(stmt_palloc).all())
            if not proposed_allocs:
                raise ApprovalError(f"Proposal '{proposal_id}' has no proposed allocations")

            affected_invoice_ids = sorted(list({pa.invoice_id for pa in proposed_allocs}))

            # ------------------------------------------------------------------
            # STEP 2: Acquire database locks in deterministic order
            # ------------------------------------------------------------------
            is_postgres = (
                self.session.bind is not None and self.session.bind.dialect.name == "postgresql"
            )

            if is_postgres:
                # Lock payment row
                stmt_pay_lock = (
                    select(PaymentRecord)
                    .where(PaymentRecord.id == proposal.payment_id, PaymentRecord.tenant_id == tenant_id)
                    .with_for_update()
                )
                payment = self.session.scalars(stmt_pay_lock).first()

                # Lock invoice rows in deterministic lexicographical order to prevent deadlocks
                stmt_inv_lock = (
                    select(InvoiceRecord)
                    .where(InvoiceRecord.id.in_(affected_invoice_ids), InvoiceRecord.tenant_id == tenant_id)
                    .order_by(InvoiceRecord.id)
                    .with_for_update()
                )
                invoices = list(self.session.scalars(stmt_inv_lock).all())
            else:
                payment = self.session.get(PaymentRecord, proposal.payment_id)
                invoices = [self.session.get(InvoiceRecord, inv_id) for inv_id in affected_invoice_ids]

            if payment is None or payment.tenant_id != tenant_id:
                raise EntityNotFoundError(f"Payment '{proposal.payment_id}' not found for tenant '{tenant_id}'")

            invoices_by_id = {inv.id: inv for inv in invoices if inv is not None and inv.tenant_id == tenant_id}
            for inv_id in affected_invoice_ids:
                if inv_id not in invoices_by_id:
                    raise EntityNotFoundError(f"Invoice '{inv_id}' not found for tenant '{tenant_id}'")

            # ------------------------------------------------------------------
            # STEP 3: Verify proposal eligibility, status, and versions
            # ------------------------------------------------------------------
            norm_pay_status = (payment.status or "").upper()
            if norm_pay_status not in {"COMPLETED", "SUCCESS", "S"}:
                raise IneligiblePaymentError(
                    f"Payment '{payment.provider_payment_id}' status is '{payment.status}'. "
                    f"Only COMPLETED payments can be approved (I7)."
                )

            if expected_payment_version is not None and payment.version != expected_payment_version:
                raise StaleVersionError(
                    f"Stale payment version: expected {expected_payment_version}, actual {payment.version}"
                )

            for inv_id, inv in invoices_by_id.items():
                norm_inv_status = (inv.status or "").upper()
                if norm_inv_status in {"DRAFT", "CANCELLED", "PAID", "VOID"}:
                    raise IneligibleInvoiceError(
                        f"Invoice '{inv.provider_invoice_id}' status is '{inv.status}', ineligible for approval (I8)"
                    )
                if expected_invoice_versions and inv_id in expected_invoice_versions:
                    expected_ver = expected_invoice_versions[inv_id]
                    if inv.version != expected_ver:
                        raise StaleVersionError(
                            f"Stale invoice version for '{inv.provider_invoice_id}': "
                            f"expected {expected_ver}, actual {inv.version}"
                        )

            # Invariant I4: Currency equality across payment, invoices, allocations
            for pa in proposed_allocs:
                if pa.currency != payment.currency:
                    raise LedgerConstraintError(
                        f"Currency mismatch (I4): allocation currency '{pa.currency}' != payment currency '{payment.currency}'"
                    )
                inv = invoices_by_id[pa.invoice_id]
                if pa.currency != inv.currency:
                    raise LedgerConstraintError(
                        f"Currency mismatch (I4): allocation currency '{pa.currency}' != invoice currency '{inv.currency}'"
                    )

            # ------------------------------------------------------------------
            # STEP 4: RERUN THE EXISTING INDEPENDENT VERIFIER
            # ------------------------------------------------------------------
            domain_payment = payment.to_domain()
            domain_invoices = [inv.to_domain() for inv in invoices_by_id.values()]

            # Load all existing approved allocations and provider settlements on these invoices and payment
            stmt_exist_pay_allocs = select(AllocationRecord).where(
                AllocationRecord.tenant_id == tenant_id,
                AllocationRecord.payment_id == payment.id,
            )
            stmt_exist_inv_allocs = select(AllocationRecord).where(
                AllocationRecord.tenant_id == tenant_id,
                AllocationRecord.invoice_id.in_(affected_invoice_ids),
            )
            existing_db_allocs = list(
                set(self.session.scalars(stmt_exist_pay_allocs).all())
                | set(self.session.scalars(stmt_exist_inv_allocs).all())
            )

            stmt_exist_pay_sett = select(ProviderSettlementRecord).where(
                ProviderSettlementRecord.tenant_id == tenant_id,
                ProviderSettlementRecord.payment_id == payment.id,
            )
            stmt_exist_inv_sett = select(ProviderSettlementRecord).where(
                ProviderSettlementRecord.tenant_id == tenant_id,
                ProviderSettlementRecord.invoice_id.in_(affected_invoice_ids),
            )
            existing_db_settlements = list(
                set(self.session.scalars(stmt_exist_pay_sett).all())
                | set(self.session.scalars(stmt_exist_inv_sett).all())
            )

            # Map DB allocations and settlements to domain PaymentAllocation
            domain_existing_allocs: List[PaymentAllocation] = []
            for eda in existing_db_allocs:
                eda_pay = self.session.get(PaymentRecord, eda.payment_id)
                eda_inv = self.session.get(InvoiceRecord, eda.invoice_id)
                if eda_pay and eda_inv:
                    domain_existing_allocs.append(
                        eda.to_domain(eda_pay.provider_payment_id, eda_inv.provider_invoice_id)
                    )

            for eds in existing_db_settlements:
                eds_pay = self.session.get(PaymentRecord, eds.payment_id) if eds.payment_id else None
                eds_inv = self.session.get(InvoiceRecord, eds.invoice_id)
                pay_id_str = eds_pay.provider_payment_id if eds_pay else eds.provider_settlement_id
                inv_id_str = eds_inv.provider_invoice_id if eds_inv else ""
                if inv_id_str:
                    domain_existing_allocs.append(
                        eds.to_domain(pay_id_str, inv_id_str)
                    )

            # Reconstruct candidate and proposal for verifier
            candidate_allocs = tuple(
                PaymentAllocation(
                    payment_id=payment.provider_payment_id,
                    invoice_id=invoices_by_id[pa.invoice_id].provider_invoice_id,
                    amount_cents=pa.amount_minor,
                    status=AllocationStatus.PROPOSED,
                    currency=pa.currency,
                )
                for pa in proposed_allocs
            )

            # Reconstruct evidence from proposal record
            evidence_items: List[Evidence] = []
            try:
                raw_ev = json.loads(proposal.evidence_json)
                if isinstance(raw_ev, list):
                    for item in raw_ev:
                        code_str = item if isinstance(item, str) else item.get("code", "")
                        if code_str in EvidenceCode.__members__:
                            evidence_items.append(Evidence(code=EvidenceCode[code_str]))
            except Exception:
                pass

            candidate = Candidate(allocations=candidate_allocs, evidence=tuple(evidence_items))
            domain_proposal = MatchProposal(
                payment_id=payment.provider_payment_id,
                candidates=(candidate,),
                outcome=MatchOutcome.PROPOSED,
            )

            # Run independent verifier inside the transaction
            verification_results = verify_proposals(
                proposals=[domain_proposal],
                payments=[domain_payment],
                invoices=domain_invoices,
                existing_allocations=domain_existing_allocs,
            )

            if not verification_results or not verification_results[0].passed:
                issues = verification_results[0].issues if verification_results else ("Unknown verifier failure",)
                raise VerificationFailedError(issues)

            # ------------------------------------------------------------------
            # STEP 5 & 6: Recalculate allocation amounts and check capacities
            # ------------------------------------------------------------------
            # Calculate current payment allocations + provider settlements
            stmt_curr_pay_alloc = select(func.coalesce(func.sum(AllocationRecord.amount_minor), 0)).where(
                AllocationRecord.tenant_id == tenant_id,
                AllocationRecord.payment_id == payment.id,
            )
            stmt_curr_pay_sett = select(func.coalesce(func.sum(ProviderSettlementRecord.amount_minor), 0)).where(
                ProviderSettlementRecord.tenant_id == tenant_id,
                ProviderSettlementRecord.payment_id == payment.id,
            )
            current_pay_allocated = int(self.session.scalar(stmt_curr_pay_alloc) or 0) + int(self.session.scalar(stmt_curr_pay_sett) or 0)
            proposed_pay_total = sum(pa.amount_minor for pa in proposed_allocs)

            if current_pay_allocated + proposed_pay_total > payment.amount_minor:
                raise LedgerConstraintError(
                    f"Payment capacity exceeded (I2): payment amount {payment.amount_minor} minor units, "
                    f"already allocated/settled {current_pay_allocated}, proposed {proposed_pay_total}"
                )

            # Calculate capacity per invoice and ensure no duplicate provider settlement
            for pa in proposed_allocs:
                inv = invoices_by_id[pa.invoice_id]

                # Ensure a provider settlement cannot later be approved again as a duplicate allocation
                stmt_dup_sett = select(ProviderSettlementRecord).where(
                    ProviderSettlementRecord.tenant_id == tenant_id,
                    ProviderSettlementRecord.invoice_id == inv.id,
                    (
                        (ProviderSettlementRecord.payment_id == payment.id)
                        | (ProviderSettlementRecord.provider_settlement_id == payment.provider_payment_id)
                    ),
                )
                if self.session.scalars(stmt_dup_sett).first() is not None:
                    raise ConflictError(
                        f"Cannot approve allocation for payment '{payment.provider_payment_id}' and invoice '{inv.provider_invoice_id}': "
                        f"already settled by provider."
                    )

                stmt_curr_inv_alloc = select(func.coalesce(func.sum(AllocationRecord.amount_minor), 0)).where(
                    AllocationRecord.tenant_id == tenant_id,
                    AllocationRecord.invoice_id == inv.id,
                )
                stmt_curr_inv_sett = select(func.coalesce(func.sum(ProviderSettlementRecord.amount_minor), 0)).where(
                    ProviderSettlementRecord.tenant_id == tenant_id,
                    ProviderSettlementRecord.invoice_id == inv.id,
                )
                curr_inv_allocated = int(self.session.scalar(stmt_curr_inv_alloc) or 0) + int(self.session.scalar(stmt_curr_inv_sett) or 0)
                if curr_inv_allocated + pa.amount_minor > inv.total_minor:
                    raise LedgerConstraintError(
                        f"Invoice capacity exceeded (I3): invoice '{inv.provider_invoice_id}' total {inv.total_minor}, "
                        f"already allocated/settled {curr_inv_allocated}, proposed {pa.amount_minor}"
                    )

            # ------------------------------------------------------------------
            # STEP 7: Insert APPROVED decision
            # ------------------------------------------------------------------
            decision = DecisionRecord(
                tenant_id=tenant_id,
                proposal_id=proposal_id,
                decision_type="APPROVED",
                actor=actor,
                reason=reason,
                idempotency_key=idempotency_key,
                decided_at=datetime.now(timezone.utc),
            )
            self.session.add(decision)
            self.session.flush()

            # ------------------------------------------------------------------
            # STEP 8: Insert approved allocations into application-owned ledger
            # ------------------------------------------------------------------
            created_allocations: List[AllocationRecord] = []
            for pa in proposed_allocs:
                alloc = AllocationRecord(
                    tenant_id=tenant_id,
                    payment_id=payment.id,
                    invoice_id=pa.invoice_id,
                    amount_minor=pa.amount_minor,
                    currency=pa.currency,
                    decision_id=decision.id,
                    actor=actor,
                    evidence_json=pa.evidence_json,
                    allocated_at=datetime.now(timezone.utc),
                )
                self.session.add(alloc)
                created_allocations.append(alloc)

            # Mark proposal as approved and increment versions
            proposal.verifier_status = "APPROVED"
            proposal.version += 1
            payment.version += 1
            for inv in invoices_by_id.values():
                inv.version += 1
            self.session.flush()

            # ------------------------------------------------------------------
            # STEP 9: Append hash-chained audit event
            # ------------------------------------------------------------------
            audit_details = {
                "decision_id": decision.id,
                "proposal_id": proposal.id,
                "payment_id": payment.provider_payment_id,
                "actor": actor,
                "idempotency_key": idempotency_key,
                "reason": reason,
                "allocations": [
                    {
                        "allocation_id": a.id,
                        "invoice_id": invoices_by_id[a.invoice_id].provider_invoice_id,
                        "amount_minor": a.amount_minor,
                        "currency": a.currency,
                    }
                    for a in created_allocations
                ],
            }

            audit_event = self.audit_repo.append_event(
                tenant_id=tenant_id,
                event_type="PROPOSAL_APPROVED",
                actor=actor,
                subject_type="proposal",
                subject_id=proposal.id,
                details=audit_details,
            )

            # ------------------------------------------------------------------
            # STEP 10: Commit everything together
            # ------------------------------------------------------------------
            self.session.commit()

            return ApprovalResult(
                decision_id=decision.id,
                proposal_id=proposal.id,
                status="APPROVED",
                allocation_ids=tuple(a.id for a in created_allocations),
                audit_event_id=audit_event.event_id,
                is_idempotent_replay=False,
            )

        except Exception:
            # If any step fails, roll back completely
            self.session.rollback()
            raise

    def reject_proposal(
        self,
        tenant_id: str,
        proposal_id: str,
        actor: str,
        idempotency_key: str,
        reason: str,
    ) -> DecisionRecord:
        """Record a human or authorized rejection of a proposed allocation."""
        if not actor or not actor.strip():
            raise ValueError("Actor must be specified for rejection")
        if not reason or not reason.strip():
            raise ValueError("Reason must be specified for rejection")

        try:
            proposal = self.session.get(ProposalRecord, proposal_id)
            if proposal is None or proposal.tenant_id != tenant_id:
                raise EntityNotFoundError(f"Proposal '{proposal_id}' not found")

            # Check idempotency
            stmt = select(DecisionRecord).where(
                DecisionRecord.tenant_id == tenant_id,
                DecisionRecord.idempotency_key == idempotency_key,
            )
            existing = self.session.scalars(stmt).first()
            if existing:
                if existing.proposal_id == proposal_id and existing.decision_type == "REJECTED":
                    return existing
                raise ConflictError(f"Idempotency key '{idempotency_key}' conflict")

            decision = DecisionRecord(
                tenant_id=tenant_id,
                proposal_id=proposal_id,
                decision_type="REJECTED",
                actor=actor,
                reason=reason,
                idempotency_key=idempotency_key,
                decided_at=datetime.now(timezone.utc),
            )
            self.session.add(decision)
            proposal.verifier_status = "REJECTED"
            proposal.version += 1
            self.session.flush()

            self.audit_repo.append_event(
                tenant_id=tenant_id,
                event_type="PROPOSAL_REJECTED",
                actor=actor,
                subject_type="proposal",
                subject_id=proposal.id,
                details={
                    "decision_id": decision.id,
                    "proposal_id": proposal.id,
                    "actor": actor,
                    "reason": reason,
                    "idempotency_key": idempotency_key,
                },
            )
            self.session.commit()
            return decision
        except Exception:
            self.session.rollback()
            raise

    def escalate_proposal(
        self,
        tenant_id: str,
        proposal_id: str,
        actor: str,
        idempotency_key: str,
        reason: str,
    ) -> DecisionRecord:
        """Record an escalation for exception review."""
        if not actor or not actor.strip():
            raise ValueError("Actor must be specified for escalation")
        if not reason or not reason.strip():
            raise ValueError("Reason must be specified for escalation")

        try:
            proposal = self.session.get(ProposalRecord, proposal_id)
            if proposal is None or proposal.tenant_id != tenant_id:
                raise EntityNotFoundError(f"Proposal '{proposal_id}' not found")

            stmt = select(DecisionRecord).where(
                DecisionRecord.tenant_id == tenant_id,
                DecisionRecord.idempotency_key == idempotency_key,
            )
            existing = self.session.scalars(stmt).first()
            if existing:
                if existing.proposal_id == proposal_id and existing.decision_type == "ESCALATED":
                    return existing
                raise ConflictError(f"Idempotency key '{idempotency_key}' conflict")

            decision = DecisionRecord(
                tenant_id=tenant_id,
                proposal_id=proposal_id,
                decision_type="ESCALATED",
                actor=actor,
                reason=reason,
                idempotency_key=idempotency_key,
                decided_at=datetime.now(timezone.utc),
            )
            self.session.add(decision)
            proposal.verifier_status = "ESCALATED"
            proposal.version += 1
            self.session.flush()

            self.audit_repo.append_event(
                tenant_id=tenant_id,
                event_type="PROPOSAL_ESCALATED",
                actor=actor,
                subject_type="proposal",
                subject_id=proposal.id,
                details={
                    "decision_id": decision.id,
                    "proposal_id": proposal.id,
                    "actor": actor,
                    "reason": reason,
                    "idempotency_key": idempotency_key,
                },
            )
            self.session.commit()
            return decision
        except Exception:
            self.session.rollback()
            raise

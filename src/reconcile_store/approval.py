"""Atomic Approval Engine.

Implements the non-negotiable 10-step atomic approval transaction for reconciliation proposals.
Only this engine can transition a proposal into APPROVED application ledger allocations.
"""

from __future__ import annotations

from typing import Optional, Sequence, Any, Dict, List, Set
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from sqlalchemy import select, func, or_, and_
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
    RefundRecord,
    InvestigationRecord,
)
from .canonical import canonical_json
from .repositories.base import EntityNotFoundError, ConflictError, StaleVersionError, TenantIsolationError
from .repositories.invoice_repo import InvoiceRepository, IneligibleInvoiceError
from .repositories.payment_repo import PaymentRepository, IneligiblePaymentError
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
    Refund,
    PaymentReversal,
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


@dataclass(frozen=True)
class ProposalSafetyEvaluation:
    """Authoritative evaluation of proposal safety against linked AI investigations."""

    effective_verifier_status: str
    approval_eligible: bool
    approval_prohibited_reason: Optional[str]
    linked_investigation: Optional[Dict[str, Any]]


def evaluate_proposal_investigation_safety(
    session: Session,
    tenant_id: str,
    proposal: ProposalRecord,
    payment: Optional[PaymentRecord] = None,
    invoices_by_id: Optional[Dict[str, InvoiceRecord]] = None,
    proposed_allocs: Optional[Sequence[ProposalAllocationRecord]] = None,
) -> ProposalSafetyEvaluation:
    """Evaluate proposal safety against tenant-scoped linked AI investigations.

    Enforces non-negotiable financial safety invariants:
    - Authoritative tenant-scoped database relationships: InvestigationRecord.tenant_id == tenant_id.
    - Linked by target_id matching payment.id, payment.provider_payment_id, or proposal.id.
    - INVALID status or prompt injection suspect ALWAYS blocks approval (fail-closed).
    - NEEDS_REVIEW requires independent safety invariants to pass, positive ALLOCATE_PAYMENT
      recommendation matching allocations, and no unresolved customer mismatches or conflicts.
    - Legitimate human review where all invariants passed and no unresolved conflicts exist is allowed.
    """
    if payment is None and proposal.payment_id:
        payment = session.get(PaymentRecord, proposal.payment_id)

    target_payment_ids = []
    if payment:
        if payment.id:
            target_payment_ids.append(payment.id)
        if payment.provider_payment_id:
            target_payment_ids.append(payment.provider_payment_id)

    conditions = []
    if proposal.id:
        conditions.append(
            and_(
                InvestigationRecord.target_type == "proposal",
                InvestigationRecord.target_id == proposal.id,
            )
        )
    if target_payment_ids:
        conditions.append(
            and_(
                InvestigationRecord.target_type == "payment",
                InvestigationRecord.target_id.in_(target_payment_ids),
            )
        )

    linked_invs: List[InvestigationRecord] = []
    if conditions:
        stmt = (
            select(InvestigationRecord)
            .where(
                InvestigationRecord.tenant_id == tenant_id,
                or_(*conditions),
            )
            .order_by(InvestigationRecord.created_at.desc())
        )
        linked_invs = list(session.scalars(stmt).all())

    if not linked_invs:
        is_eligible = (
            proposal.outcome == "PROPOSED"
            and proposal.verifier_status != "INVALID"
            and proposal.verifier_status != "UNVERIFIED"
        )
        reason = (
            None
            if is_eligible
            else f"Proposal outcome '{proposal.outcome}' or status '{proposal.verifier_status}' is not eligible for approval."
        )
        return ProposalSafetyEvaluation(
            effective_verifier_status=proposal.verifier_status,
            approval_eligible=is_eligible,
            approval_prohibited_reason=reason,
            linked_investigation=None,
        )

    # 1. FAIL-CLOSED: Check for ANY INVALID investigation or prompt injection suspect
    for inv in linked_invs:
        inv_res = inv.result if isinstance(inv.result, dict) else {}
        risk_flags = inv_res.get("risk_flags", []) if isinstance(inv_res, dict) else []
        is_injection = (
            inv.classification in {"PROMPT_INJECTION_SUSPECT", "PROMPT_INJECTION_DETECTED"}
            or any("PROMPT_INJECTION" in str(rf).upper() for rf in risk_flags)
        )
        if inv.status == "INVALID" or inv.verifier_status == "INVALID" or is_injection:
            inv_summary = {
                "id": inv.id,
                "status": "INVALID",
                "classification": inv.classification or ("PROMPT_INJECTION_SUSPECT" if is_injection else None),
                "verifier_status": "INVALID",
                "summary": inv.summary,
                "risk_flags": risk_flags,
            }
            classif_desc = inv.classification or "PROMPT_INJECTION_SUSPECT"
            reason = (
                f"Adversarial prompt injection tokens detected in payment remittance ({classif_desc}). "
                "Safety verifier failed closed. Approval is strictly prohibited."
            )
            return ProposalSafetyEvaluation(
                effective_verifier_status="INVALID",
                approval_eligible=False,
                approval_prohibited_reason=reason,
                linked_investigation=inv_summary,
            )

    # 2. NEEDS_REVIEW: Verify safety invariants, customer matching, and resolution
    for inv in linked_invs:
        if inv.status == "NEEDS_REVIEW" or inv.verifier_status == "NEEDS_REVIEW":
            inv_res = inv.result if isinstance(inv.result, dict) else {}
            v_details = inv.verifier_details if isinstance(inv.verifier_details, dict) else {}
            risk_flags = inv_res.get("risk_flags", [])

            inv_summary = {
                "id": inv.id,
                "status": inv.status,
                "classification": inv.classification,
                "verifier_status": "NEEDS_REVIEW",
                "summary": inv.summary,
                "risk_flags": risk_flags,
            }

            # 2a: Independent safety invariants check
            inv_passed = v_details.get("passed", False)
            checked_invariants = v_details.get("checked_invariants", {})
            if not inv_passed or not all(checked_invariants.values()):
                issues = v_details.get("issues", ["Independent safety verifier invariants failed"])
                issues_str = "; ".join(issues) if isinstance(issues, list) else str(issues)
                return ProposalSafetyEvaluation(
                    effective_verifier_status="NEEDS_REVIEW",
                    approval_eligible=False,
                    approval_prohibited_reason=f"Independent safety verifier invariants failed for investigation '{inv.id}': {issues_str}",
                    linked_investigation=inv_summary,
                )

            # 2b: Conflicting evidence check
            conflicting = inv_res.get("conflicting_evidence", [])
            if conflicting:
                return ProposalSafetyEvaluation(
                    effective_verifier_status="NEEDS_REVIEW",
                    approval_eligible=False,
                    approval_prohibited_reason=f"Unresolved conflicting evidence in investigation '{inv.id}': {conflicting}",
                    linked_investigation=inv_summary,
                )

            # 2c: Candidate invoice customer mismatch (unverified remitter)
            cand_analyses = inv_res.get("candidate_invoice_analysis", [])
            for cand in cand_analyses:
                if cand.get("customer_match") is False:
                    payer_label = payment.payer_name if (payment and payment.payer_name) else "Payment remitter"
                    inv_label = cand.get("invoice_number") or cand.get("invoice_id") or "target invoice"
                    remitter_reason = (
                        f"Unresolved customer mismatch: Remitter '{payer_label}' does not match customer for invoice '{inv_label}'. "
                        "Third-party clearinghouse relationship remains unverified."
                    )
                    return ProposalSafetyEvaluation(
                        effective_verifier_status="NEEDS_REVIEW",
                        approval_eligible=False,
                        approval_prohibited_reason=remitter_reason,
                        linked_investigation=inv_summary,
                    )

            # 2d: Positive ALLOCATE_PAYMENT recommendation matching proposed allocations
            sugg_res = inv_res.get("suggested_resolution")
            if not sugg_res or not isinstance(sugg_res, dict):
                return ProposalSafetyEvaluation(
                    effective_verifier_status="NEEDS_REVIEW",
                    approval_eligible=False,
                    approval_prohibited_reason=f"Investigation '{inv.id}' in NEEDS_REVIEW has no suggested resolution.",
                    linked_investigation=inv_summary,
                )

            if sugg_res.get("resolution_type") != "ALLOCATE_PAYMENT":
                return ProposalSafetyEvaluation(
                    effective_verifier_status="NEEDS_REVIEW",
                    approval_eligible=False,
                    approval_prohibited_reason=f"Investigation '{inv.id}' suggested resolution is '{sugg_res.get('resolution_type')}', not ALLOCATE_PAYMENT.",
                    linked_investigation=inv_summary,
                )

            if proposed_allocs is None:
                stmt_palloc = select(ProposalAllocationRecord).where(
                    ProposalAllocationRecord.tenant_id == tenant_id,
                    ProposalAllocationRecord.proposal_id == proposal.id,
                )
                proposed_allocs = list(session.scalars(stmt_palloc).all())

            if invoices_by_id is None and proposed_allocs:
                aff_ids = {pa.invoice_id for pa in proposed_allocs}
                stmt_invs = select(InvoiceRecord).where(
                    InvoiceRecord.tenant_id == tenant_id,
                    InvoiceRecord.id.in_(aff_ids),
                )
                invoices_by_id = {inv.id: inv for inv in session.scalars(stmt_invs).all()}

            sugg_allocs = sugg_res.get("suggested_allocations")
            sugg_amt = sugg_res.get("suggested_amount_cents")
            total_proposed = sum(pa.amount_minor for pa in proposed_allocs) if proposed_allocs else 0

            if sugg_allocs and isinstance(sugg_allocs, dict):
                for pa in proposed_allocs:
                    inv = (invoices_by_id or {}).get(pa.invoice_id)
                    keys_to_test = [pa.invoice_id]
                    if inv:
                        keys_to_test.extend([inv.provider_invoice_id, inv.invoice_number])
                    matched_val = None
                    for k in keys_to_test:
                        if k in sugg_allocs:
                            matched_val = sugg_allocs[k]
                            break
                    if matched_val is None or matched_val != pa.amount_minor:
                        return ProposalSafetyEvaluation(
                            effective_verifier_status="NEEDS_REVIEW",
                            approval_eligible=False,
                            approval_prohibited_reason=(
                                f"Proposed allocation for invoice '{inv.invoice_number if inv else pa.invoice_id}' "
                                f"({pa.amount_minor}) does not match investigation suggestion ({matched_val})."
                            ),
                            linked_investigation=inv_summary,
                        )
            elif sugg_amt is not None and sugg_amt != total_proposed:
                return ProposalSafetyEvaluation(
                    effective_verifier_status="NEEDS_REVIEW",
                    approval_eligible=False,
                    approval_prohibited_reason=(
                        f"Proposed total allocation ({total_proposed}) does not match investigation suggested amount ({sugg_amt})."
                    ),
                    linked_investigation=inv_summary,
                )

            # Legitimate human review where all invariants passed and no unresolved conflicts exist
            is_eligible = (proposal.outcome == "PROPOSED")
            return ProposalSafetyEvaluation(
                effective_verifier_status="NEEDS_REVIEW",
                approval_eligible=is_eligible,
                approval_prohibited_reason=None if is_eligible else f"Proposal outcome is {proposal.outcome}",
                linked_investigation=inv_summary,
            )

    # 3. Linked investigation exists and is COMPLETED / VERIFIED
    latest_inv = linked_invs[0]
    inv_summary = {
        "id": latest_inv.id,
        "status": latest_inv.status,
        "classification": latest_inv.classification,
        "verifier_status": latest_inv.verifier_status,
        "summary": latest_inv.summary,
    }
    is_eligible = (proposal.outcome == "PROPOSED" and proposal.verifier_status == "VERIFIED" and latest_inv.status != "INVALID")
    return ProposalSafetyEvaluation(
        effective_verifier_status=proposal.verifier_status,
        approval_eligible=is_eligible,
        approval_prohibited_reason=None if is_eligible else f"Proposal outcome '{proposal.outcome}' is ineligible.",
        linked_investigation=inv_summary,
    )


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
            # STEP 3B: Authoritative Investigation Safety Gate
            # ------------------------------------------------------------------
            safety_eval = evaluate_proposal_investigation_safety(
                session=self.session,
                tenant_id=tenant_id,
                proposal=proposal,
                payment=payment,
                invoices_by_id=invoices_by_id,
                proposed_allocs=proposed_allocs,
            )
            if not safety_eval.approval_eligible:
                raise ApprovalError(
                    f"Proposal approval blocked: {safety_eval.approval_prohibited_reason or 'Proposal is not eligible for approval'}"
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
                excess_amount_cents=proposal.excess_amount_minor,
            )

            # Load completed refunds and reversals affecting these invoices and payments
            linked_pay_db_ids = {eda.payment_id for eda in existing_db_allocs}
            for eds in existing_db_settlements:
                if eds.payment_id:
                    linked_pay_db_ids.add(eds.payment_id)

            ref_conditions = [RefundRecord.invoice_id.in_(affected_invoice_ids)]
            if linked_pay_db_ids:
                ref_conditions.append(
                    RefundRecord.parent_payment_id.in_(list(linked_pay_db_ids))
                )

            stmt_exist_refs = select(RefundRecord).where(
                RefundRecord.tenant_id == tenant_id,
                RefundRecord.status.in_(["COMPLETED", "SUCCESS", "SETTLED"]),
                or_(*ref_conditions),
            )
            db_refund_recs = self.session.scalars(stmt_exist_refs).all()
            domain_refunds: List[Refund] = []
            domain_reversals: List[PaymentReversal] = []
            for r in db_refund_recs:
                r_pay = self.session.get(PaymentRecord, r.parent_payment_id) if r.parent_payment_id else None
                pay_id_str = r_pay.provider_payment_id if r_pay else "unknown"
                r_inv = self.session.get(InvoiceRecord, r.invoice_id) if r.invoice_id else None
                inv_id_str = r_inv.provider_invoice_id if r_inv else None
                if getattr(r, "record_type", "REFUND") == "REVERSAL":
                    domain_reversals.append(r.to_reversal_domain(pay_id_str, inv_id_str))
                else:
                    domain_refunds.append(r.to_domain(pay_id_str, inv_id_str))

            # Run independent verifier inside the transaction
            verification_results = verify_proposals(
                proposals=[domain_proposal],
                payments=[domain_payment],
                invoices=domain_invoices,
                existing_allocations=domain_existing_allocs,
                refunds=domain_refunds,
                reversals=domain_reversals,
            )

            if not verification_results or not verification_results[0].passed:
                issues = verification_results[0].issues if verification_results else ("Unknown verifier failure",)
                raise VerificationFailedError(issues)

            # ------------------------------------------------------------------
            # STEP 5 & 6: Recalculate allocation amounts and check capacities
            # ------------------------------------------------------------------
            pay_repo = PaymentRepository(self.session)
            unapplied_cash = pay_repo.get_unapplied_cash_minor(tenant_id, payment.id)
            proposed_pay_total = sum(pa.amount_minor for pa in proposed_allocs)

            if proposed_pay_total > unapplied_cash:
                raise LedgerConstraintError(
                    f"Payment capacity exceeded (I2): payment '{payment.provider_payment_id}' unapplied cash "
                    f"{unapplied_cash} minor units, proposed {proposed_pay_total}"
                )

            # Calculate capacity per invoice and ensure no duplicate provider settlement
            inv_repo = InvoiceRepository(self.session)
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

                outstanding = inv_repo.get_ledger_outstanding_minor(tenant_id, inv.id)
                if pa.amount_minor > outstanding:
                    raise LedgerConstraintError(
                        f"Invoice capacity exceeded (I3): invoice '{inv.provider_invoice_id}' total {inv.total_minor}, "
                        f"outstanding balance {outstanding}, proposed {pa.amount_minor}"
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

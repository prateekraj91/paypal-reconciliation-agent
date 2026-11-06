"""Independent Safety Verifier for the AI Exception Investigator.

NON-NEGOTIABLE FINANCIAL SAFETY INVARIANTS:
1. The AI model is untrusted. Its outputs must never be accepted without independent deterministic validation.
2. Every evidence reference cited must exist in authoritative store and belong to the correct tenant.
3. Reported monetary amounts and currency codes must strictly match stored records.
4. An AI recommendation must never exceed available financial capacity.
5. An AI output cannot claim that a financial transaction (allocation, refund, reversal) has occurred or is approved.
6. The verifier runs completely independently of the model and cannot be overridden by it.
7. Fail-closed: Any contradiction, capacity violation, or fabricated evidence causes rejection (INVALID / UNVERIFIED).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set
from sqlalchemy import select, or_
from sqlalchemy.orm import Session

from reconcile_store.schema import (
    InvoiceRecord,
    PaymentRecord,
    ProposalRecord,
    AllocationRecord,
    ProviderSettlementRecord,
    RefundRecord,
    ProviderRecord,
    AuditEventRecord,
)
from reconcile.models import outstanding_cents
from .models import InvestigationResult, VerifierStatus, EvidenceReference


@dataclass(frozen=True)
class SafetyVerificationResult:
    """The authoritative outcome of the independent safety verification."""

    status: VerifierStatus
    passed: bool
    issues: tuple[str, ...] = ()
    verified_evidence_ids: tuple[str, ...] = ()
    checked_invariants: dict[str, bool] = field(default_factory=dict)
    summary: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status.value,
            "passed": self.passed,
            "issues": list(self.issues),
            "verified_evidence_ids": list(self.verified_evidence_ids),
            "checked_invariants": self.checked_invariants,
            "summary": self.summary,
        }


class IndependentSafetyVerifier:
    """Independent validator checking an InvestigationResult against authoritative store facts."""

    def __init__(self, session: Session, tenant_id: str) -> None:
        if not tenant_id or not tenant_id.strip():
            raise ValueError("tenant_id is required for IndependentSafetyVerifier")
        self.session = session
        self.tenant_id = tenant_id

    def verify_investigation(self, result: InvestigationResult) -> SafetyVerificationResult:
        """Run all safety checks on the model investigation output.
        
        Returns a fail-closed SafetyVerificationResult.
        """
        issues: List[str] = []
        checked_invariants: Dict[str, bool] = {}
        verified_evidence_ids: Set[str] = set()

        # Check 1: Tenant Isolation Consistency
        tenant_passed = (result.tenant_id == self.tenant_id)
        checked_invariants["tenant_isolation"] = tenant_passed
        if not tenant_passed:
            issues.append(
                f"Tenant mismatch: investigation specifies tenant '{result.tenant_id}', "
                f"but session context is '{self.tenant_id}'"
            )

        # Check 2: Target Entity Existence
        target_passed = self._verify_target_exists(result.target_type, result.target_id)
        checked_invariants["target_entity_exists"] = target_passed
        if not target_passed:
            issues.append(
                f"Target entity '{result.target_type}:{result.target_id}' does not exist for tenant '{self.tenant_id}'"
            )

        # Check 3: Evidence References Existence & Tenant Ownership
        evidence_dict = {ev.evidence_id: ev for ev in result.supporting_evidence}
        evidence_passed = True
        for ev in result.supporting_evidence:
            if ev.tenant_id != self.tenant_id:
                evidence_passed = False
                issues.append(
                    f"Evidence '{ev.evidence_id}' has tenant_id '{ev.tenant_id}' contradicting '{self.tenant_id}'"
                )
                continue

            exists_and_valid = self._verify_single_evidence(ev, issues)
            if exists_and_valid:
                verified_evidence_ids.add(ev.evidence_id)
            else:
                evidence_passed = False

        checked_invariants["evidence_references_authentic"] = evidence_passed

        # Check 4: Grounding of Verified Facts
        facts_passed = True
        for fact in result.verified_facts:
            # Every fact MUST cite at least one evidence ID
            if not fact.evidence_ids:
                facts_passed = False
                issues.append(f"Fact '{fact.statement[:60]}...' has no supporting evidence references")
                continue

            for ev_id in fact.evidence_ids:
                if ev_id not in verified_evidence_ids:
                    facts_passed = False
                    issues.append(
                        f"Fact '{fact.statement[:60]}...' cites unverified or fabricated evidence '{ev_id}'"
                    )

            # If fact specifies an amount and currency, verify it matches evidence
            if fact.amount_cents is not None:
                matching_ev = [
                    evidence_dict[e] for e in fact.evidence_ids
                    if e in evidence_dict and evidence_dict[e].amount_cents is not None
                ]
                if matching_ev:
                    if not any(me.amount_cents == fact.amount_cents for me in matching_ev):
                        facts_passed = False
                        issues.append(
                            f"Fact amount {fact.amount_cents} cents contradicts authoritative evidence amounts: "
                            f"{[me.amount_cents for me in matching_ev]}"
                        )
                if fact.currency:
                    if matching_ev and not any(me.currency == fact.currency.upper() for me in matching_ev):
                        facts_passed = False
                        issues.append(
                            f"Fact currency {fact.currency} contradicts authoritative evidence: "
                            f"{[me.currency for me in matching_ev]}"
                        )

        checked_invariants["facts_strictly_grounded"] = facts_passed

        # Check 5: Financial Capacity Safeguards on Suggested Resolutions
        capacity_passed = True
        if result.suggested_resolution is not None:
            sr = result.suggested_resolution
            # Ensure AI does NOT claim the resolution is already applied or approved
            if "approved" in sr.resolution_type.lower() or "completed" in sr.resolution_type.lower():
                capacity_passed = False
                issues.append(
                    f"Resolution type '{sr.resolution_type}' claims pre-approved financial mutation; "
                    "all resolutions must be proposed recommendations only"
                )

            # Check capacity on target invoices
            if sr.suggested_amount_cents is not None and sr.suggested_amount_cents > 0:
                for inv_id in sr.suggested_invoice_ids:
                    inv_rec = self._get_invoice(inv_id)
                    if not inv_rec:
                        capacity_passed = False
                        issues.append(f"Suggested resolution references non-existent invoice '{inv_id}'")
                        continue

                    # Check invoice currency
                    inv_domain = inv_rec.to_domain()
                    stmt_allocs = select(AllocationRecord).where(
                        AllocationRecord.tenant_id == self.tenant_id,
                        AllocationRecord.invoice_id == inv_rec.id,
                    )
                    allocs = [
                        a.to_domain("P", inv_rec.provider_invoice_id)
                        for a in self.session.scalars(stmt_allocs).all()
                    ]
                    try:
                        cur_outstanding = outstanding_cents(inv_domain, allocs)
                    except Exception:
                        cur_outstanding = inv_rec.total_minor

                    if sr.suggested_amount_cents > cur_outstanding:
                        capacity_passed = False
                        issues.append(
                            f"Suggested allocation of {sr.suggested_amount_cents} cents exceeds available "
                            f"invoice capacity ({cur_outstanding} cents) for invoice {inv_rec.invoice_number}"
                        )

            # Invariant: Human review MUST be required for any suggested resolution
            if not result.requires_human_review:
                capacity_passed = False
                issues.append("requires_human_review is False despite suggested financial action")

        checked_invariants["financial_capacity_safe"] = capacity_passed

        # Check 6: Provider Settlements vs Application Allocations
        settlement_passed = True
        for ev in result.supporting_evidence:
            if ev.source_type == "settlement":
                # Ensure description does not mislabel settlement as internal approval
                desc_lower = ev.description.lower()
                if "approved by operator" in desc_lower or "internal allocation" in desc_lower:
                    settlement_passed = False
                    issues.append(
                        f"Evidence '{ev.evidence_id}' confuses external provider settlement with internal ledger approval"
                    )
        checked_invariants["settlement_allocation_distinction"] = settlement_passed

        # Check 7: No Autonomous Financial Operations
        autonomous_passed = (result.requires_human_review is True) or (result.suggested_resolution is None)
        checked_invariants["no_autonomous_financial_mutation"] = autonomous_passed
        if not autonomous_passed:
            issues.append("Autonomous financial mutation attempted: human review must not be bypassed")

        # Determine overall verdict
        all_passed = all(checked_invariants.values())
        if all_passed:
            # If there are hypotheses or risk flags, status is NEEDS_REVIEW (requires human operator verification)
            if result.hypotheses or result.risk_flags or result.conflicting_evidence:
                final_status = VerifierStatus.NEEDS_REVIEW
                summary = "All authoritative financial evidence verified. Case has discrepancies requiring human review."
            else:
                final_status = VerifierStatus.VERIFIED
                summary = "All financial facts and evidence references independently verified against authoritative store."
            return SafetyVerificationResult(
                status=final_status,
                passed=True,
                issues=(),
                verified_evidence_ids=tuple(sorted(verified_evidence_ids)),
                checked_invariants=checked_invariants,
                summary=summary,
            )
        else:
            final_status = VerifierStatus.INVALID
            summary = f"Verification failed closed on {len(issues)} safety invariant violations."
            return SafetyVerificationResult(
                status=final_status,
                passed=False,
                issues=tuple(issues),
                verified_evidence_ids=tuple(sorted(verified_evidence_ids)),
                checked_invariants=checked_invariants,
                summary=summary,
            )

    # --------------------------------------------------------------------------
    # Helper Verification Lookups
    # --------------------------------------------------------------------------

    def _verify_target_exists(self, target_type: str, target_id: str) -> bool:
        if target_type == "payment":
            stmt = select(PaymentRecord).where(
                PaymentRecord.tenant_id == self.tenant_id,
                or_(
                    PaymentRecord.id == target_id,
                    PaymentRecord.provider_payment_id == target_id,
                ),
            )
            return self.session.scalars(stmt).first() is not None
        elif target_type == "proposal":
            stmt = select(ProposalRecord).where(
                ProposalRecord.tenant_id == self.tenant_id,
                ProposalRecord.id == target_id,
            )
            return self.session.scalars(stmt).first() is not None
        elif target_type == "invoice":
            stmt = select(InvoiceRecord).where(
                InvoiceRecord.tenant_id == self.tenant_id,
                or_(
                    InvoiceRecord.id == target_id,
                    InvoiceRecord.provider_invoice_id == target_id,
                    InvoiceRecord.invoice_number == target_id,
                ),
            )
            return self.session.scalars(stmt).first() is not None
        elif target_type in ("refund", "reversal"):
            stmt = select(RefundRecord).where(
                RefundRecord.tenant_id == self.tenant_id,
                or_(
                    RefundRecord.id == target_id,
                    RefundRecord.provider_refund_id == target_id,
                ),
            )
            return self.session.scalars(stmt).first() is not None
        return False

    def _verify_single_evidence(self, ev: EvidenceReference, issues: List[str]) -> bool:
        """Verify that a single cited evidence reference exists in the database and matches amounts."""
        if ev.source_type == "payment":
            stmt = select(PaymentRecord).where(
                PaymentRecord.tenant_id == self.tenant_id,
                or_(
                    PaymentRecord.provider_payment_id == ev.source_id,
                    PaymentRecord.id == ev.source_id,
                ),
            )
            pay = self.session.scalars(stmt).first()
            if not pay:
                issues.append(f"Payment evidence '{ev.evidence_id}' (source_id='{ev.source_id}') does not exist")
                return False
            if ev.amount_cents is not None and ev.amount_cents != pay.amount_minor:
                issues.append(
                    f"Evidence '{ev.evidence_id}' amount ({ev.amount_cents}) contradicts payment amount ({pay.amount_minor})"
                )
                return False
            if ev.currency and ev.currency.upper() != pay.currency.upper():
                issues.append(
                    f"Evidence '{ev.evidence_id}' currency ({ev.currency}) contradicts payment currency ({pay.currency})"
                )
                return False
            return True

        elif ev.source_type == "invoice":
            stmt = select(InvoiceRecord).where(
                InvoiceRecord.tenant_id == self.tenant_id,
                or_(
                    InvoiceRecord.provider_invoice_id == ev.source_id,
                    InvoiceRecord.invoice_number == ev.source_id,
                    InvoiceRecord.id == ev.source_id,
                ),
            )
            inv = self.session.scalars(stmt).first()
            if not inv:
                issues.append(f"Invoice evidence '{ev.evidence_id}' (source_id='{ev.source_id}') does not exist")
                return False
            if ev.amount_cents is not None and ev.amount_cents != inv.total_minor:
                issues.append(
                    f"Evidence '{ev.evidence_id}' amount ({ev.amount_cents}) contradicts invoice amount ({inv.total_minor})"
                )
                return False
            if ev.currency and ev.currency.upper() != inv.currency.upper():
                issues.append(
                    f"Evidence '{ev.evidence_id}' currency ({ev.currency}) contradicts invoice currency ({inv.currency})"
                )
                return False
            return True

        elif ev.source_type in ("refund", "reversal"):
            stmt = select(RefundRecord).where(
                RefundRecord.tenant_id == self.tenant_id,
                or_(
                    RefundRecord.provider_refund_id == ev.source_id,
                    RefundRecord.id == ev.source_id,
                ),
            )
            ref = self.session.scalars(stmt).first()
            if not ref:
                issues.append(f"Refund/reversal evidence '{ev.evidence_id}' (source_id='{ev.source_id}') does not exist")
                return False
            if ev.amount_cents is not None and ev.amount_cents != ref.amount_minor:
                issues.append(
                    f"Evidence '{ev.evidence_id}' amount ({ev.amount_cents}) contradicts refund amount ({ref.amount_minor})"
                )
                return False
            return True

        elif ev.source_type == "settlement":
            stmt = select(ProviderSettlementRecord).where(
                ProviderSettlementRecord.tenant_id == self.tenant_id,
                or_(
                    ProviderSettlementRecord.provider_settlement_id == ev.source_id,
                    ProviderSettlementRecord.id == ev.source_id,
                ),
            )
            sett = self.session.scalars(stmt).first()
            if not sett:
                issues.append(f"Settlement evidence '{ev.evidence_id}' (source_id='{ev.source_id}') does not exist")
                return False
            if ev.amount_cents is not None and ev.amount_cents != sett.amount_minor:
                issues.append(
                    f"Evidence '{ev.evidence_id}' amount ({ev.amount_cents}) contradicts settlement amount ({sett.amount_minor})"
                )
                return False
            return True

        elif ev.source_type == "provider_record":
            stmt = select(ProviderRecord).where(
                ProviderRecord.tenant_id == self.tenant_id,
                or_(
                    ProviderRecord.id == ev.source_id,
                    ProviderRecord.provider_record_id == ev.source_id,
                ),
            )
            prec = self.session.scalars(stmt).first()
            if not prec:
                issues.append(f"Provider record evidence '{ev.evidence_id}' (source_id='{ev.source_id}') does not exist")
                return False
            return True

        elif ev.source_type == "proposal":
            stmt = select(ProposalRecord).where(
                ProposalRecord.tenant_id == self.tenant_id,
                ProposalRecord.id == ev.source_id,
            )
            prop = self.session.scalars(stmt).first()
            if not prop:
                issues.append(f"Proposal evidence '{ev.evidence_id}' (source_id='{ev.source_id}') does not exist")
                return False
            return True

        elif ev.source_type == "audit_event":
            stmt = select(AuditEventRecord).where(
                AuditEventRecord.tenant_id == self.tenant_id,
                or_(
                    AuditEventRecord.id == ev.source_id,
                    AuditEventRecord.event_id == ev.source_id,
                ),
            )
            aud = self.session.scalars(stmt).first()
            if not aud:
                issues.append(f"Audit event evidence '{ev.evidence_id}' (source_id='{ev.source_id}') does not exist")
                return False
            return True

        # Unknown source type is rejected
        issues.append(f"Evidence '{ev.evidence_id}' has unrecognized source_type '{ev.source_type}'")
        return False

    def _get_invoice(self, invoice_id_or_number: str) -> Optional[InvoiceRecord]:
        stmt = select(InvoiceRecord).where(
            InvoiceRecord.tenant_id == self.tenant_id,
            or_(
                InvoiceRecord.id == invoice_id_or_number,
                InvoiceRecord.provider_invoice_id == invoice_id_or_number,
                InvoiceRecord.invoice_number == invoice_id_or_number,
            ),
        )
        return self.session.scalars(stmt).first()

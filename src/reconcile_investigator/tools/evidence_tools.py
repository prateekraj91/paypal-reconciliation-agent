"""Read-only evidence and financial tools for the AI Exception Investigator.

All tools:
1. Are strictly read-only.
2. Enforce tenant isolation.
3. Return stable EvidenceReference objects and structured dictionaries.
4. Never execute mutations, raw SQL, shell commands, or arbitrary network calls.
5. Defend against prompt injection by isolating untrusted customer-supplied text.
"""

from __future__ import annotations

from datetime import datetime
import json
import re
from typing import Any, Dict, List, Optional, Sequence
from sqlalchemy import select, or_, and_, desc
from sqlalchemy.orm import Session

from reconcile.models import (
    Invoice,
    Payment,
    PaymentAllocation,
    PaymentReversal,
    Refund,
    outstanding_cents,
)
from reconcile_store.schema import (
    InvoiceRecord,
    PaymentRecord,
    ProposalRecord,
    ProposalAllocationRecord,
    AllocationRecord,
    ProviderSettlementRecord,
    ProviderRecord,
    RefundRecord,
    AuditEventRecord,
    ReconciliationRunRecord,
)
from ..models import EvidenceReference


def sanitize_untrusted_text(text: Optional[str]) -> str:
    """Sanitize customer-supplied text to prevent prompt injection attacks.
    
    Customer-supplied text (such as payment notes, invoice memos, reference fields)
    must never be interpreted as system instructions.
    """
    if not text:
        return ""
    # Normalize whitespace, strip control characters
    cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
    return cleaned.strip()


def check_for_prompt_injection(text: Optional[str]) -> bool:
    """Detect common prompt-injection / instruction-override patterns in customer text."""
    if not text:
        return False
    suspicious_patterns = [
        r"ignore\s+(?:all\s+)?previous",
        r"system\s*:\s*",
        r"system\s+override",
        r"human\s*:\s*",
        r"assistant\s*:\s*",
        r"override\s+(?:all\s+)?rules",
        r"approve\s+(?:this\s+)?(?:payment|transaction|allocation)",
        r"disregard\s+(?:all\s+)?(?:guidelines|rules|financial)",
        r"bypass\s+(?:human|verification|checks)",
    ]
    return any(re.search(pat, text, re.IGNORECASE) for pat in suspicious_patterns)



class EvidenceToolKit:
    """Read-only toolset bound to a database session and tenant."""

    def __init__(self, session: Session, tenant_id: str) -> None:
        if not tenant_id or not tenant_id.strip():
            raise ValueError("tenant_id is required for EvidenceToolKit to enforce tenant isolation")
        self.session = session
        self.tenant_id = tenant_id

    # --------------------------------------------------------------------------
    # Tool 1: get_payment_details
    # --------------------------------------------------------------------------
    def get_payment_details(self, payment_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve detailed payment records, linked allocations, and provider evidence."""
        stmt = select(PaymentRecord).where(
            PaymentRecord.tenant_id == self.tenant_id,
            or_(
                PaymentRecord.id == payment_id,
                PaymentRecord.provider_payment_id == payment_id,
            ),
        )
        pay = self.session.scalars(stmt).first()
        if not pay:
            return None

        # Check existing allocations
        stmt_alloc = select(AllocationRecord).where(
            AllocationRecord.tenant_id == self.tenant_id,
            AllocationRecord.payment_id == pay.id,
        )
        allocs = self.session.scalars(stmt_alloc).all()

        # Check settlements
        stmt_sett = select(ProviderSettlementRecord).where(
            ProviderSettlementRecord.tenant_id == self.tenant_id,
            or_(
                ProviderSettlementRecord.payment_id == pay.id,
                ProviderSettlementRecord.provider_settlement_id == pay.provider_payment_id,
            ),
        )
        settlements = self.session.scalars(stmt_sett).all()

        # Check refunds / reversals
        stmt_ref = select(RefundRecord).where(
            RefundRecord.tenant_id == self.tenant_id,
            RefundRecord.parent_payment_id == pay.id,
        )
        refunds = self.session.scalars(stmt_ref).all()

        # Check raw provider record
        raw_rec = None
        if pay.source_record_id:
            raw_rec = self.session.get(ProviderRecord, pay.source_record_id)

        clean_note = sanitize_untrusted_text(pay.note)
        has_prompt_injection = check_for_prompt_injection(pay.note)

        evidence_ref = EvidenceReference(
            evidence_id=f"ev_pay_{pay.provider_payment_id}",
            source_type="payment",
            source_id=pay.provider_payment_id,
            tenant_id=self.tenant_id,
            description=f"PayPal Payment {pay.provider_payment_id} for {pay.amount_minor} {pay.currency} received at {pay.received_at.isoformat()}",
            amount_cents=pay.amount_minor,
            currency=pay.currency,
            provenance_hash=raw_rec.content_hash if raw_rec else None,
            metadata={
                "db_payment_id": pay.id,
                "payer_name": pay.payer_name,
                "payer_email": pay.payer_email,
                "reference": pay.reference,
                "note_untrusted": clean_note,
                "suspected_prompt_injection": has_prompt_injection,
                "status": pay.status,
            },
        )

        return {
            "evidence": evidence_ref.model_dump(),
            "payment_id": pay.provider_payment_id,
            "db_id": pay.id,
            "amount_cents": pay.amount_minor,
            "currency": pay.currency,
            "status": pay.status,
            "payer_name": pay.payer_name,
            "payer_email": pay.payer_email,
            "reference": pay.reference,
            "note_untrusted": clean_note,
            "has_prompt_injection_flag": has_prompt_injection,
            "received_at": pay.received_at.isoformat(),
            "allocated_amount_cents": sum(a.amount_minor for a in allocs),
            "settled_amount_cents": sum(s.amount_minor for s in settlements),
            "refunded_amount_cents": sum(r.amount_minor for r in refunds if r.record_type == "REFUND"),
            "reversed_amount_cents": sum(r.amount_minor for r in refunds if r.record_type == "REVERSAL"),
        }

    # --------------------------------------------------------------------------
    # Tool 2: get_invoice_details
    # --------------------------------------------------------------------------
    def get_invoice_details(self, invoice_id_or_number: str) -> Optional[Dict[str, Any]]:
        """Retrieve authoritative invoice details and true calculated ledger balance."""
        stmt = select(InvoiceRecord).where(
            InvoiceRecord.tenant_id == self.tenant_id,
            or_(
                InvoiceRecord.id == invoice_id_or_number,
                InvoiceRecord.provider_invoice_id == invoice_id_or_number,
                InvoiceRecord.invoice_number == invoice_id_or_number,
            ),
        )
        inv = self.session.scalars(stmt).first()
        if not inv:
            return None

        # Load domain invoice
        domain_inv = inv.to_domain()

        # Load approved allocations against this invoice
        stmt_alloc = select(AllocationRecord).where(
            AllocationRecord.tenant_id == self.tenant_id,
            AllocationRecord.invoice_id == inv.id,
        )
        alloc_recs = self.session.scalars(stmt_alloc).all()

        domain_allocs: List[PaymentAllocation] = []
        for a in alloc_recs:
            pay = self.session.get(PaymentRecord, a.payment_id)
            if pay:
                domain_allocs.append(a.to_domain(pay.provider_payment_id, inv.provider_invoice_id))

        # Load settlements against this invoice
        stmt_sett = select(ProviderSettlementRecord).where(
            ProviderSettlementRecord.tenant_id == self.tenant_id,
            ProviderSettlementRecord.invoice_id == inv.id,
        )
        sett_recs = self.session.scalars(stmt_sett).all()

        # Load refunds and reversals
        stmt_ref = select(RefundRecord).where(
            RefundRecord.tenant_id == self.tenant_id,
            RefundRecord.invoice_id == inv.id,
        )
        ref_recs = self.session.scalars(stmt_ref).all()
        domain_refunds: List[Refund] = []
        domain_reversals: List[PaymentReversal] = []
        for r in ref_recs:
            parent_pay = self.session.get(PaymentRecord, r.parent_payment_id)
            pay_id = parent_pay.provider_payment_id if parent_pay else "unknown"
            if getattr(r, "record_type", "REFUND") == "REVERSAL":
                domain_reversals.append(r.to_reversal_domain(pay_id, inv.provider_invoice_id))
            else:
                domain_refunds.append(r.to_domain(pay_id, inv.provider_invoice_id))

        domain_settlements: List[PaymentAllocation] = []
        for s in sett_recs:
            domain_settlements.append(s.to_domain(s.provider_settlement_id, inv.provider_invoice_id))

        # Calculate exact ledger balance accounting for allocations and external settlements
        try:
            calculated_balance = outstanding_cents(
                domain_inv,
                domain_allocs + domain_settlements,
                refunds=domain_refunds,
                reversals=domain_reversals,
            )
        except Exception:
            calculated_balance = domain_inv.outstanding_balance_cents

        clean_memo = sanitize_untrusted_text(inv.memo)
        has_prompt_injection = check_for_prompt_injection(inv.memo)

        evidence_ref = EvidenceReference(
            evidence_id=f"ev_inv_{inv.provider_invoice_id}",
            source_type="invoice",
            source_id=inv.provider_invoice_id,
            tenant_id=self.tenant_id,
            description=f"Invoice {inv.invoice_number} ({inv.provider_invoice_id}) for total {inv.total_minor} {inv.currency}, outstanding {calculated_balance} {inv.currency}",
            amount_cents=inv.total_minor,
            currency=inv.currency,
            metadata={
                "db_invoice_id": inv.id,
                "invoice_number": inv.invoice_number,
                "customer_name": inv.customer_name,
                "customer_email": inv.customer_email,
                "status": inv.status,
                "issued_on": inv.issued_on.isoformat(),
                "due_on": inv.due_on.isoformat(),
                "calculated_outstanding_cents": calculated_balance,
                "provider_due_cents": inv.provider_due_minor,
                "memo_untrusted": clean_memo,
                "suspected_prompt_injection": has_prompt_injection,
            },
        )

        return {
            "evidence": evidence_ref.model_dump(),
            "invoice_id": inv.provider_invoice_id,
            "invoice_number": inv.invoice_number,
            "customer_name": inv.customer_name,
            "customer_email": inv.customer_email,
            "total_amount_cents": inv.total_minor,
            "calculated_outstanding_cents": calculated_balance,
            "provider_reported_due_cents": inv.provider_due_minor,
            "currency": inv.currency,
            "status": inv.status,
            "issued_on": inv.issued_on.isoformat(),
            "due_on": inv.due_on.isoformat(),
            "memo_untrusted": clean_memo,
            "has_prompt_injection_flag": has_prompt_injection,
            "approved_allocations_count": len(domain_allocs),
            "settlements_count": len(sett_recs),
        }

    # --------------------------------------------------------------------------
    # Tool 3: get_candidate_invoices
    # --------------------------------------------------------------------------
    def get_candidate_invoices(
        self,
        customer_name: Optional[str] = None,
        customer_email: Optional[str] = None,
        amount_cents: Optional[int] = None,
        currency: Optional[str] = None,
        reference: Optional[str] = None,
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        """Search open or candidate invoices matching various financial attributes."""
        stmt = select(InvoiceRecord).where(InvoiceRecord.tenant_id == self.tenant_id)

        conditions = []
        if reference:
            clean_ref = reference.strip()
            conditions.append(InvoiceRecord.invoice_number.ilike(f"%{clean_ref}%"))
            conditions.append(InvoiceRecord.provider_invoice_id == clean_ref)
        if customer_name:
            clean_name = customer_name.strip()
            conditions.append(InvoiceRecord.customer_name.ilike(f"%{clean_name}%"))
        if customer_email:
            clean_email = customer_email.strip()
            conditions.append(InvoiceRecord.customer_email.ilike(f"%{clean_email}%"))
        if amount_cents is not None:
            # Allow +/- 10% amount window for candidate discovery
            min_amt = max(0, int(amount_cents * 0.9))
            max_amt = int(amount_cents * 1.1)
            conditions.append(and_(InvoiceRecord.total_minor >= min_amt, InvoiceRecord.total_minor <= max_amt))

        if conditions:
            stmt = stmt.where(or_(*conditions))
        elif currency:
            stmt = stmt.where(InvoiceRecord.currency == currency.strip().upper())

        stmt = stmt.order_by(desc(InvoiceRecord.created_at)).limit(limit)
        invoices = self.session.scalars(stmt).all()

        results = []
        for inv in invoices:
            detail = self.get_invoice_details(inv.id)
            if detail:
                results.append(detail)
        return results

    # --------------------------------------------------------------------------
    # Tool 4: get_ambiguity_certificate
    # --------------------------------------------------------------------------
    def get_ambiguity_certificate(self, payment_id_or_proposal_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve the ambiguity certificate produced by the matcher, if present."""
        stmt = select(ProposalRecord).where(
            ProposalRecord.tenant_id == self.tenant_id,
            or_(
                ProposalRecord.id == payment_id_or_proposal_id,
                ProposalRecord.payment_id == payment_id_or_proposal_id,
            ),
        )
        prop = self.session.scalars(stmt).first()
        if not prop:
            # Also try matching by provider_payment_id
            stmt_pay = select(PaymentRecord).where(
                PaymentRecord.tenant_id == self.tenant_id,
                PaymentRecord.provider_payment_id == payment_id_or_proposal_id,
            )
            pay = self.session.scalars(stmt_pay).first()
            if pay:
                stmt_prop = select(ProposalRecord).where(
                    ProposalRecord.tenant_id == self.tenant_id,
                    ProposalRecord.payment_id == pay.id,
                ).order_by(desc(ProposalRecord.created_at))
                prop = self.session.scalars(stmt_prop).first()

        if not prop or not prop.ambiguity_certificate_json:
            return None

        try:
            cert_dict = json.loads(prop.ambiguity_certificate_json)
        except Exception:
            return None

        evidence_ref = EvidenceReference(
            evidence_id=f"ev_ambig_{prop.id}",
            source_type="proposal",
            source_id=prop.id,
            tenant_id=self.tenant_id,
            description=f"Ambiguity Certificate for proposal {prop.id} with {len(cert_dict.get('candidate_invoice_ids', []))} candidate invoices",
            metadata=cert_dict,
        )

        return {
            "evidence": evidence_ref.model_dump(),
            "proposal_id": prop.id,
            "outcome": prop.outcome,
            "certificate": cert_dict,
        }

    # --------------------------------------------------------------------------
    # Tool 5: get_provider_records
    # --------------------------------------------------------------------------
    def get_provider_records(
        self,
        record_kind: Optional[str] = None,
        provider_record_id: Optional[str] = None,
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        """Fetch immutable source evidence records received from the external provider."""
        stmt = select(ProviderRecord).where(ProviderRecord.tenant_id == self.tenant_id)
        if record_kind:
            stmt = stmt.where(ProviderRecord.record_kind == record_kind)
        if provider_record_id:
            stmt = stmt.where(ProviderRecord.provider_record_id == provider_record_id)
        stmt = stmt.order_by(desc(ProviderRecord.ingested_at)).limit(limit)

        records = self.session.scalars(stmt).all()
        results = []
        for r in records:
            payload_summary = {}
            try:
                raw_dict = json.loads(r.payload_json)
                if isinstance(raw_dict, dict):
                    # Redact or summarize large blobs
                    payload_summary = {
                        k: (v if not isinstance(v, (dict, list)) else f"<{type(v).__name__}>")
                        for k, v in list(raw_dict.items())[:15]
                    }
            except Exception:
                pass

            evidence_ref = EvidenceReference(
                evidence_id=f"ev_prec_{r.id}",
                source_type="provider_record",
                source_id=r.id,
                tenant_id=self.tenant_id,
                description=f"Raw Provider Record {r.provider_record_id} ({r.record_kind}) with content hash {r.content_hash[:16]}...",
                provenance_hash=r.content_hash,
                metadata={
                    "provider": r.provider,
                    "record_kind": r.record_kind,
                    "provider_record_id": r.provider_record_id,
                    "ingested_at": r.ingested_at.isoformat(),
                    "payload_summary": payload_summary,
                },
            )
            results.append(evidence_ref.model_dump())
        return results

    # --------------------------------------------------------------------------
    # Tool 6: get_provider_settlement_history
    # --------------------------------------------------------------------------
    def get_provider_settlement_history(
        self,
        invoice_id: Optional[str] = None,
        payment_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Retrieve external provider settlements."""
        stmt = select(ProviderSettlementRecord).where(ProviderSettlementRecord.tenant_id == self.tenant_id)
        if invoice_id:
            stmt = stmt.where(
                or_(
                    ProviderSettlementRecord.invoice_id == invoice_id,
                    ProviderSettlementRecord.provider_settlement_id == invoice_id,
                )
            )
        if payment_id:
            target_pay_ids = [payment_id]
            pay = self.session.scalars(
                select(PaymentRecord).where(
                    PaymentRecord.tenant_id == self.tenant_id,
                    or_(PaymentRecord.id == payment_id, PaymentRecord.provider_payment_id == payment_id),
                )
            ).first()
            if pay:
                target_pay_ids.extend([pay.id, pay.provider_payment_id])
            stmt = stmt.where(
                or_(
                    ProviderSettlementRecord.payment_id.in_(target_pay_ids),
                    ProviderSettlementRecord.provider_settlement_id.in_(target_pay_ids),
                )
            )
        stmt = stmt.order_by(desc(ProviderSettlementRecord.created_at))
        settlements = self.session.scalars(stmt).all()

        results = []
        for s in settlements:
            evidence_ref = EvidenceReference(
                evidence_id=f"ev_sett_{s.id}",
                source_type="settlement",
                source_id=s.id,
                tenant_id=self.tenant_id,
                description=f"Provider Settlement {s.provider_settlement_id} for {s.amount_minor} {s.currency}",
                amount_cents=s.amount_minor,
                currency=s.currency,
                metadata={
                    "provider_settlement_id": s.provider_settlement_id,
                    "invoice_id": s.invoice_id,
                    "payment_id": s.payment_id,
                    "settled_at": s.settled_at.isoformat() if s.settled_at else None,
                },
            )
            results.append(evidence_ref.model_dump())
        return results

    # --------------------------------------------------------------------------
    # Tool 7: get_refund_history
    # --------------------------------------------------------------------------
    def get_refund_history(
        self,
        parent_payment_id: Optional[str] = None,
        invoice_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Retrieve merchant-initiated refunds."""
        stmt = select(RefundRecord).where(
            RefundRecord.tenant_id == self.tenant_id,
            RefundRecord.record_type == "REFUND",
        )
        if parent_payment_id:
            target_ids = [parent_payment_id]
            pay = self.session.scalars(
                select(PaymentRecord).where(
                    PaymentRecord.tenant_id == self.tenant_id,
                    or_(PaymentRecord.id == parent_payment_id, PaymentRecord.provider_payment_id == parent_payment_id),
                )
            ).first()
            if pay:
                target_ids.extend([pay.id, pay.provider_payment_id])
            stmt = stmt.where(
                or_(
                    RefundRecord.parent_payment_id.in_(target_ids),
                    RefundRecord.provider_refund_id.in_(target_ids),
                )
            )
        if invoice_id:
            stmt = stmt.where(RefundRecord.invoice_id == invoice_id)
        stmt = stmt.order_by(desc(RefundRecord.refunded_at))

        refunds = self.session.scalars(stmt).all()
        results = []
        for r in refunds:
            evidence_ref = EvidenceReference(
                evidence_id=f"ev_ref_{r.provider_refund_id}",
                source_type="refund",
                source_id=r.provider_refund_id,
                tenant_id=self.tenant_id,
                description=f"Refund {r.provider_refund_id} for {r.amount_minor} {r.currency} on parent payment {r.parent_payment_id}",
                amount_cents=r.amount_minor,
                currency=r.currency,
                metadata={
                    "parent_payment_id": r.parent_payment_id,
                    "invoice_id": r.invoice_id,
                    "reason": r.reason,
                    "status": r.status,
                    "refunded_at": r.refunded_at.isoformat(),
                },
            )
            results.append(evidence_ref.model_dump())
        return results

    # --------------------------------------------------------------------------
    # Tool 8: get_reversal_history
    # --------------------------------------------------------------------------
    def get_reversal_history(
        self,
        parent_payment_id: Optional[str] = None,
        invoice_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Retrieve processor/network reversals (e.g. disputes, chargebacks)."""
        stmt = select(RefundRecord).where(
            RefundRecord.tenant_id == self.tenant_id,
            RefundRecord.record_type == "REVERSAL",
        )
        if parent_payment_id:
            target_ids = [parent_payment_id]
            pay = self.session.scalars(
                select(PaymentRecord).where(
                    PaymentRecord.tenant_id == self.tenant_id,
                    or_(PaymentRecord.id == parent_payment_id, PaymentRecord.provider_payment_id == parent_payment_id),
                )
            ).first()
            if pay:
                target_ids.extend([pay.id, pay.provider_payment_id])
            stmt = stmt.where(
                or_(
                    RefundRecord.parent_payment_id.in_(target_ids),
                    RefundRecord.provider_refund_id.in_(target_ids),
                )
            )
        if invoice_id:
            stmt = stmt.where(RefundRecord.invoice_id == invoice_id)
        stmt = stmt.order_by(desc(RefundRecord.refunded_at))

        reversals = self.session.scalars(stmt).all()

        results = []
        for rev in reversals:
            evidence_ref = EvidenceReference(
                evidence_id=f"ev_rev_{rev.provider_refund_id}",
                source_type="reversal",
                source_id=rev.provider_refund_id,
                tenant_id=self.tenant_id,
                description=f"Reversal {rev.provider_refund_id} for {rev.amount_minor} {rev.currency} (dispute: {rev.dispute_id or 'none'})",
                amount_cents=rev.amount_minor,
                currency=rev.currency,
                metadata={
                    "parent_payment_id": rev.parent_payment_id,
                    "invoice_id": rev.invoice_id,
                    "dispute_id": rev.dispute_id,
                    "reason": rev.reason,
                    "status": rev.status,
                    "reversed_at": rev.refunded_at.isoformat(),
                },
            )
            results.append(evidence_ref.model_dump())
        return results

    # --------------------------------------------------------------------------
    # Tool 9: get_reconciliation_run
    # --------------------------------------------------------------------------
    def get_reconciliation_run(
        self,
        run_id: Optional[str] = None,
        latest: bool = True,
    ) -> Optional[Dict[str, Any]]:
        """Retrieve reconciliation run metadata and summary metrics."""
        stmt = select(ReconciliationRunRecord).where(ReconciliationRunRecord.tenant_id == self.tenant_id)
        if run_id:
            stmt = stmt.where(ReconciliationRunRecord.id == run_id)
        elif latest:
            stmt = stmt.order_by(desc(ReconciliationRunRecord.run_at))

        run = self.session.scalars(stmt).first()
        if not run:
            return None

        evidence_ref = EvidenceReference(
            evidence_id=f"ev_run_{run.id}",
            source_type="reconciliation_run",
            source_id=run.id,
            tenant_id=self.tenant_id,
            description=f"Reconciliation Run {run.id} by matcher {run.matcher_name} v{run.matcher_version}",
            provenance_hash=run.input_snapshot_id,
            metadata={
                "matcher_name": run.matcher_name,
                "matcher_version": run.matcher_version,
                "metrics": run.metrics,
                "run_at": run.run_at.isoformat(),
            },
        )
        return {
            "evidence": evidence_ref.model_dump(),
            "run_id": run.id,
            "matcher_name": run.matcher_name,
            "status": run.status,
            "metrics": run.metrics,
            "run_at": run.run_at.isoformat(),
        }

    # --------------------------------------------------------------------------
    # Tool 10: get_audit_history
    # --------------------------------------------------------------------------
    def get_audit_history(
        self,
        subject_id: Optional[str] = None,
        subject_type: Optional[str] = None,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """Retrieve immutable audit events from the tamper-evident hash chain."""
        stmt = select(AuditEventRecord).where(AuditEventRecord.tenant_id == self.tenant_id)
        if subject_id:
            stmt = stmt.where(AuditEventRecord.subject_id == subject_id)
        if subject_type:
            stmt = stmt.where(AuditEventRecord.subject_type == subject_type)
        stmt = stmt.order_by(desc(AuditEventRecord.sequence_number)).limit(limit)

        events = self.session.scalars(stmt).all()
        results = []
        for e in events:
            evidence_ref = EvidenceReference(
                evidence_id=f"ev_aud_{e.event_id}",
                source_type="audit_event",
                source_id=e.event_id,
                tenant_id=self.tenant_id,
                description=f"Audit Event seq={e.sequence_number} type={e.event_type} by actor={e.actor}",
                provenance_hash=e.event_hash,
                metadata={
                    "sequence_number": e.sequence_number,
                    "event_type": e.event_type,
                    "actor": e.actor,
                    "subject_type": e.subject_type,
                    "subject_id": e.subject_id,
                    "timestamp": e.timestamp.isoformat(),
                },
            )
            results.append(evidence_ref.model_dump())
        return results

    # --------------------------------------------------------------------------
    # Tool 11: get_missing_evidence
    # --------------------------------------------------------------------------
    def get_missing_evidence(self, target_type: str, target_id: str) -> List[Dict[str, Any]]:
        """Identify missing financial records or fields needed for reconciliation."""
        missing = []
        if target_type == "payment":
            pay_detail = self.get_payment_details(target_id)
            if pay_detail:
                if not pay_detail.get("reference"):
                    missing.append({
                        "missing_item": "PAYMENT_REFERENCE",
                        "importance": "CRITICAL",
                        "why_needed": "Payer omitted invoice reference number in payment metadata",
                    })
                if not pay_detail.get("payer_email"):
                    missing.append({
                        "missing_item": "PAYER_EMAIL",
                        "importance": "DESIRABLE",
                        "why_needed": "Payer email could disambiguate between customers with identical names",
                    })
                # Check if raw provider record exists
                p_recs = self.get_provider_records(record_kind="payment", provider_record_id=pay_detail["payment_id"])
                if not p_recs:
                    missing.append({
                        "missing_item": "RAW_PROVIDER_RECORD",
                        "importance": "OPTIONAL",
                        "why_needed": "Immutable provider transaction payload not stored in provider_records",
                    })
        elif target_type == "invoice":
            inv_detail = self.get_invoice_details(target_id)
            if inv_detail:
                if not inv_detail.get("customer_email"):
                    missing.append({
                        "missing_item": "CUSTOMER_EMAIL",
                        "importance": "DESIRABLE",
                        "why_needed": "Invoice lacks customer email address for automated matching",
                    })
        return missing

    # --------------------------------------------------------------------------
    # Tool 12: get_related_financial_events
    # --------------------------------------------------------------------------
    def get_related_financial_events(self, target_id: str) -> Dict[str, Any]:
        """Collect all related financial records across the entire platform."""
        events = {
            "payment": self.get_payment_details(target_id),
            "invoice": self.get_invoice_details(target_id),
            "ambiguity_certificate": self.get_ambiguity_certificate(target_id),
            "settlements": self.get_provider_settlement_history(payment_id=target_id),
            "refunds": self.get_refund_history(parent_payment_id=target_id),
            "reversals": self.get_reversal_history(parent_payment_id=target_id),
            "audit_events": self.get_audit_history(subject_id=target_id),
        }
        return events

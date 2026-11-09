"""Deterministic mock provider for automated tests and synthetic evaluation."""

from __future__ import annotations

import json
from enum import Enum
from typing import Any, Dict, List, Optional

from .base import InvestigationContext, InvestigationModelProvider, InvestigationRawResponse


class MockProviderBehavior(str, Enum):
    NORMAL = "NORMAL"
    MALFORMED_JSON = "MALFORMED_JSON"
    FABRICATED_EVIDENCE = "FABRICATED_EVIDENCE"
    AMOUNT_MISMATCH = "AMOUNT_MISMATCH"
    CAPACITY_VIOLATION = "CAPACITY_VIOLATION"
    AUTONOMOUS_MUTATION = "AUTONOMOUS_MUTATION"
    PROMPT_INJECTION_VULNERABLE = "PROMPT_INJECTION_VULNERABLE"


class MockInvestigationProvider(InvestigationModelProvider):
    """Deterministic provider that synthesizes grounded investigation outputs from context."""

    def __init__(
        self,
        behavior: MockProviderBehavior = MockProviderBehavior.NORMAL,
        model_name: str = "mock-investigator-v1",
    ) -> None:
        self.behavior = behavior
        self.model_name = model_name

    def generate_investigation(
        self,
        context: InvestigationContext,
        system_prompt: str,
        user_prompt: str,
    ) -> InvestigationRawResponse:
        """Produce deterministic structured JSON response based on behavior mode."""
        if self.behavior == MockProviderBehavior.MALFORMED_JSON:
            return InvestigationRawResponse(
                raw_json_str="{ invalid_json: True, missing_closing_bracket: ...",
                provider_name="mock",
                model_name=self.model_name,
                prompt_version="1.0.0",
                latency_ms=1.5,
            )

        # Extract context fields
        pay_id = context.target_data.get("payment_id") or context.target_id
        pay_amt = context.target_data.get("amount_cents", 0)
        pay_curr = context.target_data.get("currency", "USD")
        payer_name = context.target_data.get("payer_name", "Unknown Payer")
        payer_email = context.target_data.get("payer_email")
        reference = context.target_data.get("reference")
        note = context.target_data.get("note_untrusted", "")
        has_prompt_injection = context.suspected_prompt_injection or ("ignore previous" in note.lower())

        candidates = context.candidate_invoices
        related = context.related_events
        missing = context.missing_evidence

        # Check existing settlements, refunds, reversals
        settlements = related.get("settlements") or []
        refunds = related.get("refunds") or []
        reversals = related.get("reversals") or []
        ambiguity_cert = related.get("ambiguity_certificate")

        # Determine scenario classification
        classification = "UNMATCHED_PAYMENT"
        if has_prompt_injection:
            classification = "PROMPT_INJECTION_SUSPECT"
        elif reversals:
            classification = "REVERSAL_DISCREPANCY"
        elif refunds:
            classification = "REFUND_DISCREPANCY"
        elif settlements:
            classification = "EXTERNAL_SETTLEMENT"
        elif ambiguity_cert:
            classification = "AMBIGUOUS_MATCH"
        elif candidates:
            # Check currency mismatch first
            if any(c.get("currency") and c.get("currency") != pay_curr for c in candidates):
                classification = "CURRENCY_MISMATCH"
            elif any(c.get("calculated_outstanding_cents") == 0 for c in candidates) and reference:
                classification = "DUPLICATE_PAYMENT"
            elif len(candidates) > 1:
                c_amts = [c.get("total_amount_cents", 0) for c in candidates]
                if len(set(c_amts)) == 1 or (len(c_amts) >= 2 and abs(c_amts[0] - c_amts[1]) <= 300):
                    classification = "AMBIGUOUS_MATCH"
                else:
                    classification = "SPLIT_PAYMENT"
            elif len(candidates) == 1:
                cand = candidates[0]
                cand_amt = cand.get("calculated_outstanding_cents", cand.get("total_amount_cents", 0))
                if cand_amt == 0 and reference:
                    classification = "DUPLICATE_PAYMENT"
                elif pay_amt < cand_amt:
                    classification = "PARTIAL_PAYMENT"
                elif pay_amt > cand_amt:
                    classification = "OVERPAYMENT"
                elif pay_amt == cand_amt:
                    classification = "UNMATCHED_PAYMENT" if not reference else "UNEXPLAINED_BALANCE"


        # Evidence references from context
        ev_refs = []
        verified_facts = []

        # Standard payment evidence
        ev_pay_id = f"ev_pay_{pay_id}"
        if self.behavior == MockProviderBehavior.FABRICATED_EVIDENCE:
            ev_pay_id = "ev_pay_NONEXISTENT_FABRICATED_999"

        ev_refs.append({
            "evidence_id": ev_pay_id,
            "source_type": "payment",
            "source_id": pay_id,
            "tenant_id": context.tenant_id,
            "description": f"PayPal Payment {pay_id} of {pay_amt} {pay_curr}",
            "amount_cents": pay_amt if self.behavior != MockProviderBehavior.AMOUNT_MISMATCH else (pay_amt + 5000),
            "currency": pay_curr,
            "metadata": {"status": "COMPLETED"},
        })

        fact_amount = pay_amt
        if self.behavior == MockProviderBehavior.AMOUNT_MISMATCH:
            fact_amount = pay_amt + 99999

        verified_facts.append({
            "statement": f"Payment {pay_id} for {fact_amount} {pay_curr} was received from {payer_name}.",
            "evidence_ids": [ev_pay_id],
            "amount_cents": fact_amount,
            "currency": pay_curr,
        })

        # Add candidate invoice evidence
        cand_evals = []
        for c in candidates:
            c_id = c.get("invoice_id")
            c_num = c.get("invoice_number", "")
            c_tot = c.get("total_amount_cents", 0)
            c_curr = c.get("currency", "USD")
            c_ev_id = f"ev_inv_{c_id}"
            ev_refs.append({
                "evidence_id": c_ev_id,
                "source_type": "invoice",
                "source_id": c_id,
                "tenant_id": context.tenant_id,
                "description": f"Invoice {c_num} ({c_id}) for {c_tot} {c_curr}",
                "amount_cents": c_tot,
                "currency": c_curr,
                "metadata": {"status": c.get("status", "SENT")},
            })
            verified_facts.append({
                "statement": f"Invoice {c_num} has total amount {c_tot} {c_curr}.",
                "evidence_ids": [c_ev_id],
                "amount_cents": c_tot,
                "currency": c_curr,
            })
            cand_evals.append({
                "invoice_id": c_id,
                "invoice_number": c_num,
                "customer_match": bool(payer_name and payer_name.lower() in c.get("customer_name", "").lower()),
                "amount_match": bool(pay_amt == c.get("calculated_outstanding_cents")),
                "score": 85 if (payer_name.lower() in c.get("customer_name", "").lower()) else 40,
                "assessment": f"Candidate invoice with outstanding balance {c.get('calculated_outstanding_cents')} cents.",
            })

        # Add refund/reversal evidence if present
        for r in refunds:
            ev_r = r.get("evidence", r)
            ev_refs.append(ev_r)
            verified_facts.append({
                "statement": f"Refund of {ev_r.get('amount_cents')} {ev_r.get('currency')} is recorded on payment.",
                "evidence_ids": [ev_r.get("evidence_id")],
                "amount_cents": ev_r.get("amount_cents"),
                "currency": ev_r.get("currency"),
            })

        for rev in reversals:
            ev_rev = rev.get("evidence", rev)
            ev_refs.append(ev_rev)
            verified_facts.append({
                "statement": f"Reversal of {ev_rev.get('amount_cents')} {ev_rev.get('currency')} is recorded on payment.",
                "evidence_ids": [ev_rev.get("evidence_id")],
                "amount_cents": ev_rev.get("amount_cents"),
                "currency": ev_rev.get("currency"),
            })

        for s in settlements:
            ev_s = s.get("evidence", s)
            ev_refs.append(ev_s)
            verified_facts.append({
                "statement": f"External provider settlement of {ev_s.get('amount_cents')} {ev_s.get('currency')} exists.",
                "evidence_ids": [ev_s.get("evidence_id")],
                "amount_cents": ev_s.get("amount_cents"),
                "currency": ev_s.get("currency"),
            })

        # Hypotheses
        hypotheses = []
        if classification == "PROMPT_INJECTION_SUSPECT":
            hypotheses.append({
                "hypothesis_id": "hyp_inj_1",
                "statement": "Payer embedded adversarial command in customer notes attempting to force automatic approval.",
                "likelihood": "HIGH",
                "supporting_evidence_ids": [ev_pay_id],
                "counter_evidence_ids": [],
            })
        elif classification == "AMBIGUOUS_MATCH":
            hypotheses.append({
                "hypothesis_id": "hyp_ambig_1",
                "statement": "Multiple open invoices match the payment amount and customer profile.",
                "likelihood": "HIGH",
                "supporting_evidence_ids": [ev_pay_id] + [f"ev_inv_{c['invoice_id']}" for c in candidates],
                "counter_evidence_ids": [],
            })
        elif classification == "PARTIAL_PAYMENT":
            hypotheses.append({
                "hypothesis_id": "hyp_part_1",
                "statement": "Customer submitted an installment payment that partially satisfies the invoice.",
                "likelihood": "HIGH",
                "supporting_evidence_ids": [ev_pay_id],
                "counter_evidence_ids": [],
            })
        elif classification == "OVERPAYMENT":
            hypotheses.append({
                "hypothesis_id": "hyp_over_1",
                "statement": "Payment amount exceeds the invoice outstanding balance, creating an unallocated surplus.",
                "likelihood": "HIGH",
                "supporting_evidence_ids": [ev_pay_id],
                "counter_evidence_ids": [],
            })
        elif classification == "REVERSAL_DISCREPANCY":
            hypotheses.append({
                "hypothesis_id": "hyp_rev_1",
                "statement": "The transaction was subject to an involuntary processor chargeback or dispute clawback.",
                "likelihood": "HIGH",
                "supporting_evidence_ids": [ev_pay_id],
                "counter_evidence_ids": [],
            })
        elif classification == "REFUND_DISCREPANCY":
            hypotheses.append({
                "hypothesis_id": "hyp_ref_1",
                "statement": "A merchant-initiated refund has reduced or cancelled the original payment balance.",
                "likelihood": "HIGH",
                "supporting_evidence_ids": [ev_pay_id],
                "counter_evidence_ids": [],
            })
        else:
            hypotheses.append({
                "hypothesis_id": "hyp_default_1",
                "statement": "Payment arrived without structured invoice reference and requires manual operator matching.",
                "likelihood": "MEDIUM",
                "supporting_evidence_ids": [ev_pay_id],
                "counter_evidence_ids": [],
            })

        # Recommended steps
        steps = [
            {
                "step_number": 1,
                "action": "Inspect customer ledger and recent correspondence",
                "target": f"Customer '{payer_name}'",
                "reason": "Verify customer intention for unreferenced payment",
            },
            {
                "step_number": 2,
                "action": "Review candidate invoices in accounting system",
                "target": "Billing system",
                "reason": "Confirm whether open balance corresponds to this payment",
            },
        ]

        # Suggested resolution
        suggested_resolution = None
        net_cash = pay_amt - sum(
            (item.get("evidence", item).get("amount_cents") or 0) for item in (*refunds, *reversals)
        )
        can_suggest = net_cash > 0 or self.behavior == MockProviderBehavior.CAPACITY_VIOLATION
        if candidates and can_suggest and classification not in ("PROMPT_INJECTION_SUSPECT", "AMBIGUOUS_MATCH"):
            best_cand = candidates[0]
            alloc_amt = min(net_cash, best_cand.get("calculated_outstanding_cents", net_cash))
            if self.behavior == MockProviderBehavior.CAPACITY_VIOLATION:
                alloc_amt = best_cand.get("calculated_outstanding_cents", 1000) + 500000

            res_type = "PROPOSE_MANUAL_ALLOCATION"
            if self.behavior == MockProviderBehavior.AUTONOMOUS_MUTATION:
                res_type = "APPROVED_ALLOCATION_COMPLETED"

            suggested_resolution = {
                "resolution_type": res_type,
                "suggested_invoice_ids": [best_cand["invoice_id"]],
                "suggested_amount_cents": alloc_amt,
                "notes": f"Propose allocation of {alloc_amt} cents to invoice {best_cand.get('invoice_number')} for human operator review.",
            }

        # Risk flags
        risk_flags = []
        if has_prompt_injection:
            risk_flags.append("PROMPT_INJECTION_DETECTED")
        if classification == "AMBIGUOUS_MATCH":
            risk_flags.append("AMBIGUOUS_CANDIDATES")
        if classification == "CURRENCY_MISMATCH":
            risk_flags.append("CURRENCY_MISMATCH")
        if reversals:
            risk_flags.append("DISPUTE_REVERSAL_PRESENT")
        if self.behavior == MockProviderBehavior.PROMPT_INJECTION_VULNERABLE and has_prompt_injection:
            # Flawed mock fails to flag prompt injection
            risk_flags = []

        result_dict = {
            "investigation_id": context.investigation_id,
            "tenant_id": context.tenant_id,
            "target_type": context.target_type,
            "target_id": context.target_id,
            "classification": classification,
            "executive_summary": (
                f"Investigation for {context.target_type} {context.target_id} identified discrepancy "
                f"'{classification}'. Authoritative records show payment of {pay_amt} {pay_curr}."
            ),
            "verified_facts": verified_facts,
            "supporting_evidence": ev_refs,
            "candidate_invoice_analysis": cand_evals,
            "conflicting_evidence": [],
            "missing_evidence": missing,
            "hypotheses": hypotheses,
            "recommended_steps": steps,
            "suggested_resolution": suggested_resolution,
            "risk_flags": risk_flags,
            "requires_human_review": True if self.behavior != MockProviderBehavior.AUTONOMOUS_MUTATION else False,
            "verifier_status": "NOT_VERIFIED",
            "verifier_notes": [],
            "model_info": {
                "provider": "mock",
                "model": self.model_name,
                "behavior": self.behavior.value,
                "prompt_version": "1.0.0",
            },
            "created_at": "2026-11-06T12:00:00Z",
            "completed_at": "2026-11-06T12:00:01Z",
        }

        return InvestigationRawResponse(
            raw_json_str=json.dumps(result_dict),
            provider_name="mock",
            model_name=self.model_name,
            prompt_version="1.0.0",
            latency_ms=2.5,
            prompt_tokens=450,
            completion_tokens=320,
        )

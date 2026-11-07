"""Financial analysis and capacity verification helpers for the investigator subsystem."""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from sqlalchemy.orm import Session

from .evidence_tools import EvidenceToolKit


class FinancialAnalyzer:
    """Read-only financial analysis service evaluating capacities, currency safety, and candidates."""

    def __init__(self, toolkit: EvidenceToolKit) -> None:
        self.toolkit = toolkit

    def check_allocation_capacity(
        self,
        invoice_id_or_number: str,
        proposed_amount_cents: int,
        currency: str,
    ) -> Dict[str, Any]:
        """Check whether an invoice has sufficient outstanding balance for a proposed allocation.
        
        Guarantees that an AI recommendation cannot recommend allocating more than available capacity.
        """
        inv_details = self.toolkit.get_invoice_details(invoice_id_or_number)
        if not inv_details:
            return {
                "valid": False,
                "reason": f"Invoice '{invoice_id_or_number}' not found for tenant '{self.toolkit.tenant_id}'",
                "available_cents": 0,
            }

        inv_curr = inv_details["currency"]
        if currency.strip().upper() != inv_curr:
            return {
                "valid": False,
                "reason": f"Currency mismatch: proposed {currency.upper()} does not match invoice {inv_curr}",
                "available_cents": inv_details["calculated_outstanding_cents"],
            }

        outstanding = inv_details["calculated_outstanding_cents"]
        if proposed_amount_cents <= 0:
            return {
                "valid": False,
                "reason": f"Proposed allocation amount must be positive, got {proposed_amount_cents}",
                "available_cents": outstanding,
            }

        if proposed_amount_cents > outstanding:
            return {
                "valid": False,
                "reason": (
                    f"Proposed allocation of {proposed_amount_cents} cents exceeds available "
                    f"outstanding balance of {outstanding} cents on invoice {inv_details['invoice_number']}"
                ),
                "available_cents": outstanding,
            }

        return {
            "valid": True,
            "invoice_id": inv_details["invoice_id"],
            "invoice_number": inv_details["invoice_number"],
            "available_cents": outstanding,
            "remaining_cents_after": outstanding - proposed_amount_cents,
        }

    def evaluate_candidates_for_payment(self, payment_id: str) -> List[Dict[str, Any]]:
        """Evaluate candidate invoices for an unmatched or ambiguous payment."""
        pay_details = self.toolkit.get_payment_details(payment_id)
        if not pay_details:
            return []

        candidates = self.toolkit.get_candidate_invoices(
            customer_name=pay_details.get("payer_name"),
            customer_email=pay_details.get("payer_email"),
            amount_cents=pay_details.get("amount_cents"),
            currency=pay_details.get("currency"),
            reference=pay_details.get("reference"),
        )

        evaluations = []
        pay_amt = pay_details["amount_cents"]
        pay_curr = pay_details["currency"]
        pay_name = (pay_details.get("payer_name") or "").strip().lower()
        pay_email = (pay_details.get("payer_email") or "").strip().lower()

        for c in candidates:
            inv_amt = c["total_amount_cents"]
            inv_out = c["calculated_outstanding_cents"]
            inv_name = (c.get("customer_name") or "").strip().lower()
            inv_email = (c.get("customer_email") or "").strip().lower()

            name_match = bool(pay_name and (pay_name in inv_name or inv_name in pay_name))
            email_match = bool(pay_email and pay_email == inv_email)
            exact_amount = (pay_amt == inv_out)
            partial_amount = (pay_amt < inv_out)
            over_amount = (pay_amt > inv_out)

            # Heuristic score for candidate ordering
            score = 0
            if email_match:
                score += 40
            if name_match:
                score += 25
            if exact_amount:
                score += 35
            elif partial_amount:
                score += 15

            assessment_parts = []
            if name_match:
                assessment_parts.append(f"Customer name matches ('{c['customer_name']}')")
            if email_match:
                assessment_parts.append(f"Customer email matches ('{c['customer_email']}')")
            if exact_amount:
                assessment_parts.append(f"Exact amount match ({pay_amt} cents)")
            elif partial_amount:
                assessment_parts.append(f"Payment ({pay_amt} cents) partially covers outstanding balance ({inv_out} cents)")
            elif over_amount:
                assessment_parts.append(f"Payment ({pay_amt} cents) exceeds outstanding balance ({inv_out} cents)")

            evaluations.append({
                "invoice_id": c["invoice_id"],
                "invoice_number": c["invoice_number"],
                "customer_match": name_match or email_match,
                "amount_match": exact_amount or partial_amount,
                "score": min(score, 100),
                "assessment": "; ".join(assessment_parts) if assessment_parts else "Weak match candidate",
            })

        return evaluations

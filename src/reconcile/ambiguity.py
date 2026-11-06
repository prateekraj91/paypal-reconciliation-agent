"""Ambiguity detection and deterministic Evidence Certificate generation.

When multiple candidate invoices compete for a payment and no definitive signal
differentiates them, the reconciliation engine must never guess. Instead, it
produces an AmbiguityCertificate that explains exactly which candidates competed,
what evidence supported each, what conflicting signals existed, what evidence
is missing to resolve the tie, and recommended next steps for human investigation.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Tuple
from dataclasses import replace

from .models import (
    AmbiguityCandidateScore,
    AmbiguityCertificate,
    Candidate,
    Evidence,
    EvidenceCode,
    Invoice,
    MatchOutcome,
    MatchProposal,
    Payment,
    PaymentAllocation,
)


def build_ambiguity_certificate(
    payment: Payment,
    competing_invoices: Sequence[Invoice],
    scores: Optional[Dict[str, float]] = None,
    candidate_evidence: Optional[Dict[str, Sequence[Evidence]]] = None,
    conflicting_evidence: Optional[Sequence[Evidence]] = None,
    missing_evidence: Optional[Sequence[str]] = None,
    rejection_reasons: Optional[Sequence[str]] = None,
    recommended_steps: Optional[Sequence[str]] = None,
) -> AmbiguityCertificate:
    """Construct a deterministic AmbiguityCertificate for competing invoices."""
    sorted_invoices = sorted(competing_invoices, key=lambda inv: (inv.number, inv.invoice_id))
    inv_ids = tuple(inv.invoice_id for inv in sorted_invoices)

    # 1. Candidate scores (integer values to respect zero-float canonical JSON invariant)
    if scores is None:
        computed_scores: dict[str, int] = {inv.invoice_id: 50 for inv in sorted_invoices}
    else:
        computed_scores = {
            inv.invoice_id: int(round(scores.get(inv.invoice_id, 0.0)))
            for inv in sorted_invoices
        }

    # 2. Supporting evidence per candidate
    supporting: dict[str, tuple[Evidence, ...]] = {}
    for inv in sorted_invoices:
        if candidate_evidence and inv.invoice_id in candidate_evidence:
            supporting[inv.invoice_id] = tuple(candidate_evidence[inv.invoice_id])
        else:
            evs = [
                Evidence(
                    EvidenceCode.AMOUNT_EXACT,
                    f"payment amount {payment.amount_cents} cents matches invoice {inv.number} amount",
                ),
            ]
            if inv.currency == payment.currency:
                evs.append(
                    Evidence(
                        EvidenceCode.AMOUNT_EXACT,
                        f"currency {payment.currency} matches invoice currency",
                    )
                )
            if payment.payer_name and payment.payer_name.strip().lower() == inv.customer_name.strip().lower():
                evs.append(
                    Evidence(
                        EvidenceCode.CUSTOMER_NAME_MATCH,
                        f"customer name {inv.customer_name!r} matches payer name",
                    )
                )
            supporting[inv.invoice_id] = tuple(evs)

    # 3. Conflicting evidence
    if conflicting_evidence is not None:
        conflicts = tuple(conflicting_evidence)
    else:
        inv_numbers = ", ".join(inv.number for inv in sorted_invoices)
        conflicts = (
            Evidence(
                EvidenceCode.MULTIPLE_CANDIDATES,
                f"{len(sorted_invoices)} competing open invoices ({inv_numbers}) share compatible attributes for payment {payment.payment_id}",
            ),
        )

    # 4. Missing evidence
    if missing_evidence is not None:
        missing = tuple(missing_evidence)
    else:
        missing_list = [
            "Distinct invoice reference in payment note or reference field",
            "Disambiguating customer purchase order or contract identifier",
        ]
        missing = tuple(missing_list)

    # 5. Rejection reasons
    if rejection_reasons is not None:
        reasons = tuple(rejection_reasons)
    else:
        inv_refs = ", ".join(f"{inv.number} ({inv.invoice_id})" for inv in sorted_invoices)
        reasons = (
            f"Payment of {payment.amount_cents} {payment.currency} has {len(sorted_invoices)} "
            f"plausible candidates with equal or indistinguishable confidence: {inv_refs}.",
            "Automatic allocation disabled to prevent arbitrary selection and ledger misallocation.",
        )

    # 6. Recommended steps
    if recommended_steps is not None:
        steps = tuple(recommended_steps)
    else:
        steps = (
            f"Contact customer {payment.payer_name or 'payer'} ({payment.payer_email or 'no email recorded'}) to verify target invoice.",
            f"Check customer account statement or communication logs for invoice numbers: {', '.join(inv.number for inv in sorted_invoices)}.",
            "Obtain human approval before recording ledger allocation.",
        )

    return AmbiguityCertificate(
        payment_id=payment.payment_id,
        candidate_invoice_ids=inv_ids,
        candidate_scores=computed_scores,
        supporting_evidence=supporting,
        conflicting_evidence=conflicts,
        missing_evidence=missing,
        rejection_reasons=reasons,
        recommended_steps=steps,
        requires_human_approval=True,
        classification="AMBIGUOUS",
    )


def create_ambiguous_proposal(
    payment: Payment,
    competing_invoices: Sequence[Invoice],
    matcher_name: str,
    scores: Optional[Dict[str, float]] = None,
    candidate_evidence: Optional[Dict[str, Sequence[Evidence]]] = None,
    conflicting_evidence: Optional[Sequence[Evidence]] = None,
    missing_evidence: Optional[Sequence[str]] = None,
    rejection_reasons: Optional[Sequence[str]] = None,
    recommended_steps: Optional[Sequence[str]] = None,
) -> MatchProposal:
    """Produce a deterministic AMBIGUOUS MatchProposal equipped with an AmbiguityCertificate."""
    certificate = build_ambiguity_certificate(
        payment=payment,
        competing_invoices=competing_invoices,
        scores=scores,
        candidate_evidence=candidate_evidence,
        conflicting_evidence=conflicting_evidence,
        missing_evidence=missing_evidence,
        rejection_reasons=rejection_reasons,
        recommended_steps=recommended_steps,
    )

    sorted_invoices = sorted(competing_invoices, key=lambda inv: (inv.number, inv.invoice_id))
    candidates = tuple(
        Candidate(
            allocations=(
                PaymentAllocation(
                    payment_id=payment.payment_id,
                    invoice_id=inv.invoice_id,
                    amount_cents=payment.amount_cents,
                    currency=inv.currency,
                ),
            ),
            evidence=certificate.supporting_evidence.get(inv.invoice_id, ()),
        )
        for inv in sorted_invoices
    )

    reasons = (
        Evidence(
            EvidenceCode.MULTIPLE_CANDIDATES,
            f"{len(sorted_invoices)} competing open invoices share compatible attributes without differentiating reference",
        ),
        Evidence(
            EvidenceCode.AMBIGUOUS_CANDIDATES,
            f"Ambiguity certificate generated with {len(sorted_invoices)} candidate invoices",
        ),
    )

    return MatchProposal(
        payment_id=payment.payment_id,
        outcome=MatchOutcome.AMBIGUOUS,
        candidates=candidates,
        reasons=reasons,
        matcher=matcher_name,
        ambiguity_certificate=certificate,
    )

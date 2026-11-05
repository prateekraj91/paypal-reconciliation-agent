"""Tests for AmbiguityCertificate generation, serialization, and deterministic properties."""

from datetime import date, datetime, timezone
import pytest

from factories import inv, pay
from reconcile.ambiguity import build_ambiguity_certificate, create_ambiguous_proposal
from reconcile.models import (
    AmbiguityCertificate,
    Evidence,
    EvidenceCode,
    Invoice,
    InvoiceStatus,
    MatchOutcome,
    Payment,
    PaymentStatus,
)


def test_ambiguity_certificate_attributes():
    payment = pay(1, 50000, reference="")
    invoices = [inv(1, 50000), inv(2, 50000)]

    cert = build_ambiguity_certificate(
        payment=payment,
        competing_invoices=invoices,
        scores={"inv_1": 60.0, "inv_2": 60.0},
        missing_evidence=["Invoice number in memo"],
        rejection_reasons=["Identical amounts with no identifying text"],
        recommended_steps=["Contact payer to confirm target invoice"],
    )

    assert cert.payment_id == payment.payment_id
    assert cert.candidate_invoice_ids == ("inv_1", "inv_2")
    assert cert.candidate_scores == {"inv_1": 60, "inv_2": 60}
    assert cert.classification == "AMBIGUOUS"
    assert cert.requires_human_approval is True
    assert "Invoice number in memo" in cert.missing_evidence
    assert "Identical amounts with no identifying text" in cert.rejection_reasons
    assert "Contact payer to confirm target invoice" in cert.recommended_steps
    assert "inv_1" in cert.supporting_evidence
    assert "inv_2" in cert.supporting_evidence


def test_ambiguity_certificate_serialization_roundtrip():
    payment = pay(1, 10000, reference="")
    invoices = [inv(1, 10000), inv(2, 10000)]

    cert = build_ambiguity_certificate(
        payment=payment,
        competing_invoices=invoices,
    )

    cert_dict = cert.to_dict()
    assert isinstance(cert_dict, dict)
    assert cert_dict["payment_id"] == payment.payment_id
    assert cert_dict["classification"] == "AMBIGUOUS"
    assert cert_dict["requires_human_approval"] is True

    # Roundtrip from dict
    restored = AmbiguityCertificate.from_dict(cert_dict)
    assert restored.payment_id == cert.payment_id
    assert restored.candidate_invoice_ids == cert.candidate_invoice_ids
    assert restored.classification == cert.classification
    assert restored.requires_human_approval == cert.requires_human_approval
    assert restored.candidate_scores == cert.candidate_scores
    assert len(restored.supporting_evidence) == len(cert.supporting_evidence)


def test_create_ambiguous_proposal_structure():
    payment = pay(1, 20000, reference="")
    invoices = [inv(1, 20000), inv(2, 20000)]

    proposal = create_ambiguous_proposal(
        payment=payment,
        competing_invoices=invoices,
        matcher_name="test-matcher",
    )

    assert proposal.outcome is MatchOutcome.AMBIGUOUS
    assert len(proposal.candidates) == 2
    assert proposal.ambiguity_certificate is not None
    assert proposal.ambiguity_certificate.classification == "AMBIGUOUS"
    assert any(r.code is EvidenceCode.AMBIGUOUS_CANDIDATES for r in proposal.reasons)

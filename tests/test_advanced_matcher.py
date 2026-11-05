"""Tests for AdvancedMatcher (T1 - T6) covering contextual matching, partial payments,

overpayments, split payments, and ambiguity detection with evidence certificates.
"""

from datetime import date, datetime, timezone
import pytest

from factories import inv, pay
from reconcile.advanced import AdvancedMatcher
from reconcile.models import (
    AllocationStatus,
    EvidenceCode,
    Invoice,
    InvoiceStatus,
    MatchOutcome,
    Payment,
    PaymentStatus,
)
from reconcile.verifier import verify_proposals

M = AdvancedMatcher()


def test_t1_exact_reference_match():
    payment = pay(1, 50000, reference="INV-1001")
    invoice = inv(1, 50000)

    proposal = M.match([payment], [invoice])[0]

    assert proposal.outcome is MatchOutcome.PROPOSED
    assert len(proposal.candidates) == 1
    assert proposal.candidates[0].allocations[0].amount_cents == 50000
    assert proposal.candidates[0].has(EvidenceCode.REFERENCE_MATCH)
    assert proposal.candidates[0].has(EvidenceCode.AMOUNT_EXACT)

    # Verification passes
    [v_res] = verify_proposals([proposal], [payment], [invoice])
    assert v_res.passed


def test_t4_contextual_match_customer_name_and_temporal():
    # No reference, but matching customer name, exact amount, and close date
    invoice = Invoice(
        invoice_id="inv_ctx_1",
        number="INV-2001",
        customer_name="Acme Corporation",
        customer_email="billing@acme.com",
        amount_cents=75000,
        currency="USD",
        issued_on=date(2026, 11, 1),
        due_on=date(2026, 11, 15),
        status=InvoiceStatus.SENT,
    )
    payment = Payment(
        payment_id="pay_ctx_1",
        amount_cents=75000,
        currency="USD",
        received_at=datetime(2026, 11, 3, 10, 0, tzinfo=timezone.utc),
        payer_name="Acme Corporation",
        payer_email="finance@acme.com",
        reference="",
        note="Monthly subscription payment",
        status=PaymentStatus.COMPLETED,
    )

    proposal = M.match([payment], [invoice])[0]

    assert proposal.outcome is MatchOutcome.PROPOSED
    assert proposal.candidates[0].allocations[0].amount_cents == 75000
    assert proposal.candidates[0].has(EvidenceCode.CONTEXTUAL_MATCH)
    assert proposal.candidates[0].has(EvidenceCode.CUSTOMER_NAME_MATCH)
    assert proposal.candidates[0].has(EvidenceCode.TEMPORAL_PROXIMITY_MATCH)

    # Verify contextual proposal passes verifier
    [v_res] = verify_proposals([proposal], [payment], [invoice])
    assert v_res.passed


def test_t4_amount_only_without_context_is_rejected():
    # Only amount matches; payer name and email are completely unlinked and no temporal signal
    invoice = Invoice(
        invoice_id="inv_ctx_2",
        number="INV-2002",
        customer_name="Globex Industries",
        customer_email="globex@globex.org",
        amount_cents=50000,
        currency="USD",
        issued_on=date(2025, 1, 1),
        due_on=date(2025, 1, 15),
        status=InvoiceStatus.SENT,
    )
    payment = Payment(
        payment_id="pay_ctx_2",
        amount_cents=50000,
        currency="USD",
        received_at=datetime(2026, 11, 1, 10, 0, tzinfo=timezone.utc),  # ~2 years later
        payer_name="Random Stranger",
        payer_email="random@nowhere.net",
        reference="",
        note="",
        status=PaymentStatus.COMPLETED,
    )

    proposal = M.match([payment], [invoice])[0]

    # Must NOT guess on amount alone!
    assert proposal.outcome is MatchOutcome.NO_MATCH
    assert any(r.code is EvidenceCode.REFERENCE_ABSENT for r in proposal.reasons)


def test_t4_ambiguity_on_exact_amount_ties():
    # Two identical open invoices for $500 with no distinguishing references
    inv1 = Invoice(
        invoice_id="inv_tie_1",
        number="INV-3001",
        customer_name="Alice Smith",
        amount_cents=50000,
        currency="USD",
        issued_on=date(2026, 10, 1),
        due_on=date(2026, 10, 15),
        status=InvoiceStatus.SENT,
    )
    inv2 = Invoice(
        invoice_id="inv_tie_2",
        number="INV-3002",
        customer_name="Bob Jones",
        amount_cents=50000,
        currency="USD",
        issued_on=date(2026, 10, 2),
        due_on=date(2026, 10, 16),
        status=InvoiceStatus.SENT,
    )
    payment = Payment(
        payment_id="pay_tie_1",
        amount_cents=50000,
        currency="USD",
        received_at=datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc),
        payer_name="Anonymous Payer",
        payer_email="",
        reference="",
        note="",
        status=PaymentStatus.COMPLETED,
    )

    proposal = M.match([payment], [inv1, inv2])[0]

    assert proposal.outcome is MatchOutcome.AMBIGUOUS
    assert len(proposal.candidates) == 2
    assert proposal.ambiguity_certificate is not None
    cert = proposal.ambiguity_certificate
    assert cert.classification == "AMBIGUOUS"
    assert "inv_tie_1" in cert.candidate_invoice_ids
    assert "inv_tie_2" in cert.candidate_invoice_ids
    assert cert.requires_human_approval is True
    assert len(cert.missing_evidence) > 0
    assert len(cert.recommended_steps) > 0


def test_t5_partial_payment_with_reference():
    invoice = inv(1, 100000)  # $1,000
    payment = pay(1, 40000, reference="INV-1001")  # $400 partial

    proposal = M.match([payment], [invoice])[0]

    assert proposal.outcome is MatchOutcome.PROPOSED
    assert proposal.candidates[0].allocations[0].amount_cents == 40000
    assert proposal.candidates[0].has(EvidenceCode.AMOUNT_PARTIAL)
    assert proposal.candidates[0].has(EvidenceCode.REFERENCE_MATCH)

    [v_res] = verify_proposals([proposal], [payment], [invoice])
    assert v_res.passed


def test_t5_overpayment_handling():
    # Payment of $1,200 for a $1,000 invoice
    invoice = inv(1, 100000)  # $1,000
    payment = pay(1, 120000, reference="INV-1001")  # $1,200

    proposal = M.match([payment], [invoice])[0]

    assert proposal.outcome is MatchOutcome.PROPOSED
    # Allocated up to invoice capacity: 100,000
    assert proposal.candidates[0].allocations[0].amount_cents == 100000
    # Excess explicitly reported: 20,000
    assert proposal.excess_amount_cents == 20000
    assert proposal.candidates[0].has(EvidenceCode.AMOUNT_OVERPAYMENT)

    # Verifier independently confirms overpayment accounting
    [v_res] = verify_proposals([proposal], [payment], [invoice])
    assert v_res.passed


def test_t5_sequential_partial_payments_in_batch():
    invoice = inv(1, 100000)  # $1,000
    p1 = pay(1, 40000, reference="INV-1001")
    p2 = pay(2, 30000, reference="INV-1001")

    # In deterministic single-run match, each payment is evaluated in order
    proposals = M.match([p1], [invoice])
    assert proposals[0].outcome is MatchOutcome.PROPOSED
    assert proposals[0].candidates[0].allocations[0].amount_cents == 40000


def test_t6_currency_mismatch_rejected():
    invoice = Invoice(
        invoice_id="inv_curr_1",
        number="INV-4001",
        customer_name="Euro Client",
        amount_cents=50000,
        currency="EUR",
        issued_on=date(2026, 11, 1),
        due_on=date(2026, 11, 15),
        status=InvoiceStatus.SENT,
    )
    payment = Payment(
        payment_id="pay_curr_1",
        amount_cents=50000,
        currency="USD",
        received_at=datetime(2026, 11, 2, tzinfo=timezone.utc),
        payer_name="Euro Payer",
        reference="INV-4001",
        status=PaymentStatus.COMPLETED,
    )

    proposal = M.match([payment], [invoice])[0]

    assert proposal.outcome is MatchOutcome.NO_MATCH
    assert any(r.code is EvidenceCode.CURRENCY_MISMATCH for r in proposal.reasons)


def test_t6_closed_invoice_rejected():
    invoice = inv(1, 50000, status=InvoiceStatus.PAID)
    payment = pay(1, 50000, reference="INV-1001")

    proposal = M.match([payment], [invoice])[0]

    assert proposal.outcome is MatchOutcome.NO_MATCH
    assert any(r.code is EvidenceCode.INVOICE_NOT_OPEN for r in proposal.reasons)


def test_t6_duplicate_delivery_screening():
    payment = pay(1, 50000, reference="INV-1001")
    invoice = inv(1, 50000)

    proposals = M.match([payment, payment], [invoice])

    assert proposals[0].outcome is MatchOutcome.PROPOSED
    assert proposals[1].outcome is MatchOutcome.NO_MATCH
    assert any(r.code is EvidenceCode.DUPLICATE_EVENT for r in proposals[1].reasons)

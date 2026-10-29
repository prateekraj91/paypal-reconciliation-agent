from factories import inv, pay

from reconcile.models import (
    AllocationStatus,
    Candidate,
    Evidence,
    EvidenceCode,
    InvoiceStatus,
    MatchOutcome,
    MatchProposal,
    PaymentAllocation,
    PaymentStatus,
)
from reconcile.verifier import verify_proposals


def make_proposal(payment, allocations, *, reference_evidence=True):
    evidence = (
        (Evidence(EvidenceCode.REFERENCE_MATCH, "invoice reference found"),)
        if reference_evidence
        else ()
    )
    candidate = Candidate(tuple(allocations), evidence=evidence)
    return MatchProposal(
        payment_id=payment.payment_id,
        outcome=MatchOutcome.PROPOSED,
        candidates=(candidate,),
        matcher="test",
    )


def allocation(payment, invoice, amount):
    return PaymentAllocation(
        payment_id=payment.payment_id,
        invoice_id=invoice.invoice_id,
        amount_cents=amount,
    )


def verify(proposal, payment, invoices, existing_allocations=()):
    return verify_proposals(
        [proposal],
        [payment],
        invoices,
        existing_allocations=existing_allocations,
    )[0]


def test_valid_exact_match_passes_without_approval():
    payment = pay(1, 50000, reference="INV-1001")
    invoice = inv(1, 50000)
    proposal = make_proposal(payment, [allocation(payment, invoice, 50000)])

    result = verify(proposal, payment, [invoice])

    assert result.passed
    assert result.issues == ()
    assert proposal.candidates[0].allocations[0].status is AllocationStatus.PROPOSED


def test_partial_allocation_can_pass_when_within_balance():
    payment = pay(1, 25000, reference="INV-1001")
    invoice = inv(1, 50000)
    proposal = make_proposal(payment, [allocation(payment, invoice, 25000)])

    result = verify(proposal, payment, [invoice])

    assert result.passed


def test_amount_only_proposal_fails_identity_evidence_check():
    payment = pay(1, 50000)
    invoice = inv(1, 50000)
    proposal = make_proposal(
        payment, [allocation(payment, invoice, 50000)],
        reference_evidence=False,
    )

    result = verify(proposal, payment, [invoice])

    assert not result.passed
    assert any("reference evidence" in issue for issue in result.issues)


def test_pending_payment_fails():
    payment = pay(1, 50000, reference="INV-1001", status=PaymentStatus.PENDING)
    invoice = inv(1, 50000)
    proposal = make_proposal(payment, [allocation(payment, invoice, 50000)])

    result = verify(proposal, payment, [invoice])

    assert not result.passed
    assert any("COMPLETED" in issue for issue in result.issues)


def test_allocation_total_must_equal_payment_amount():
    payment = pay(1, 50000, reference="INV-1001")
    invoice = inv(1, 50000)
    proposal = make_proposal(payment, [allocation(payment, invoice, 49000)])

    result = verify(proposal, payment, [invoice])

    assert not result.passed
    assert any("payment amount" in issue for issue in result.issues)


def test_missing_invoice_fails():
    payment = pay(1, 50000, reference="INV-1001")
    invoice = inv(1, 50000)
    proposal = make_proposal(payment, [allocation(payment, invoice, 50000)])

    result = verify(proposal, payment, [])

    assert not result.passed
    assert any("does not exist" in issue for issue in result.issues)


def test_closed_invoice_fails():
    payment = pay(1, 50000, reference="INV-1001")
    invoice = inv(1, 50000, status=InvoiceStatus.PAID)
    proposal = make_proposal(payment, [allocation(payment, invoice, 50000)])

    result = verify(proposal, payment, [invoice])

    assert not result.passed
    assert any("not open" in issue for issue in result.issues)


def test_currency_mismatch_fails():
    payment = pay(1, 50000, reference="INV-1001", currency="GBP")
    invoice = inv(1, 50000, currency="USD")
    proposal = make_proposal(payment, [allocation(payment, invoice, 50000)])

    result = verify(proposal, payment, [invoice])

    assert not result.passed
    assert any("currency" in issue.lower() for issue in result.issues)


def test_allocation_cannot_exceed_remaining_invoice_balance():
    payment = pay(1, 25000, reference="INV-1001")
    invoice = inv(1, 50000, status=InvoiceStatus.PARTIALLY_PAID)
    proposal = make_proposal(payment, [allocation(payment, invoice, 25000)])
    prior_payment = PaymentAllocation(
        payment_id="previous_payment",
        invoice_id=invoice.invoice_id,
        amount_cents=30000,
        status=AllocationStatus.APPROVED,
    )

    result = verify(proposal, payment, [invoice], [prior_payment])

    assert not result.passed
    assert any("available balance" in issue for issue in result.issues)


def test_conflicting_proposals_are_both_rejected():
    invoice = inv(1, 50000)
    payment_a = pay(1, 30000, reference="INV-1001")
    payment_b = pay(2, 30000, reference="INV-1001")
    proposal_a = make_proposal(
        payment_a, [allocation(payment_a, invoice, 30000)]
    )
    proposal_b = make_proposal(
        payment_b, [allocation(payment_b, invoice, 30000)]
    )

    results = verify_proposals(
        [proposal_a, proposal_b],
        [payment_a, payment_b],
        [invoice],
    )

    assert len(results) == 2
    assert all(not result.passed for result in results)
    assert all(
        any("combined proposals" in issue for issue in result.issues)
        for result in results
    )


def test_duplicate_proposals_for_same_payment_fail():
    payment = pay(1, 50000, reference="INV-1001")
    invoice = inv(1, 50000)
    proposal = make_proposal(payment, [allocation(payment, invoice, 50000)])

    results = verify_proposals(
        [proposal, proposal],
        [payment],
        [invoice],
    )

    assert len(results) == 2
    assert all(not result.passed for result in results)
    assert all(
        any("duplicate proposals" in issue for issue in result.issues)
        for result in results
    )


def test_no_match_outcome_cannot_be_verified_as_a_match():
    payment = pay(1, 50000)
    proposal = MatchProposal(
        payment_id=payment.payment_id,
        outcome=MatchOutcome.NO_MATCH,
    )

    result = verify(proposal, payment, [inv(1, 50000)])

    assert not result.passed


def test_previously_approved_payment_cannot_be_allocated_again():
    payment = pay(1, 50000, reference="INV-1001")
    invoice = inv(1, 50000)
    proposal = make_proposal(payment, [allocation(payment, invoice, 50000)])
    previous = PaymentAllocation(
        payment_id=payment.payment_id,
        invoice_id=invoice.invoice_id,
        amount_cents=50000,
        status=AllocationStatus.APPROVED,
    )

    result = verify(proposal, payment, [invoice], [previous])

    assert not result.passed
    assert any("already allocated" in issue for issue in result.issues)


def test_split_allocation_across_two_invoices_can_pass():
    payment = pay(1, 50000, reference="INV-1001")
    invoice_a = inv(1, 25000)
    invoice_b = inv(2, 25000)
    proposal = make_proposal(
        payment,
        [
            allocation(payment, invoice_a, 25000),
            allocation(payment, invoice_b, 25000),
        ],
    )

    result = verify(proposal, payment, [invoice_a, invoice_b])

    assert result.passed


def test_duplicate_invoice_ids_fail_closed():
    payment = pay(1, 50000, reference="INV-1001")
    invoice_a = inv(1, 50000)
    invoice_b = inv(1, 50000, number="INV-OTHER")
    proposal = make_proposal(payment, [allocation(payment, invoice_a, 50000)])

    result = verify(proposal, payment, [invoice_a, invoice_b])

    assert not result.passed
    assert any("duplicate invoice IDs" in issue for issue in result.issues)

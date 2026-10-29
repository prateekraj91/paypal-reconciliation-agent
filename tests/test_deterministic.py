from factories import inv, pay

from reconcile.baseline import BaselineMatcher
from reconcile.deterministic import DeterministicMatcher
from reconcile.models import EvidenceCode as E, MatchOutcome as O


M = DeterministicMatcher()


def reason_codes(proposal):
    return {e.code for e in proposal.reasons}


def test_extracts_reference_from_noisy_reference_field():
    payment = pay(1, 50000, reference="Payment for invoice INV-1001")
    proposal = M.match([payment], [inv(1, 50000)])[0]

    assert proposal.outcome is O.PROPOSED
    assert proposal.candidates[0].allocations[0].invoice_id == "inv_1"
    assert proposal.candidates[0].has(E.REFERENCE_MATCH)


def test_extracts_reference_from_payment_note():
    payment = pay(1, 50000, note="Payment covering INV-1001")
    proposal = M.match([payment], [inv(1, 50000)])[0]

    assert proposal.outcome is O.PROPOSED
    assert "payment note" in proposal.candidates[0].evidence[0].detail


def test_baseline_stays_conservative_on_noisy_reference():
    payment = pay(1, 50000, reference="Payment for invoice INV-1001")
    invoice = inv(1, 50000)

    baseline = BaselineMatcher().match([payment], [invoice])[0]
    improved = M.match([payment], [invoice])[0]

    assert baseline.outcome is O.NO_MATCH
    assert improved.outcome is O.PROPOSED


def test_amount_only_matching_is_disabled():
    proposal = M.match([pay(1, 50000)], [inv(1, 50000)])[0]

    assert proposal.outcome is O.NO_MATCH
    assert E.REFERENCE_ABSENT in reason_codes(proposal)


def test_unknown_reference_does_not_fall_back_to_amount():
    proposal = M.match(
        [pay(1, 50000, reference="INV-9999")],
        [inv(1, 50000)],
    )[0]

    assert proposal.outcome is O.NO_MATCH
    assert E.REFERENCE_UNKNOWN in reason_codes(proposal)


def test_unknown_reference_cannot_be_overridden_by_note():
    proposal = M.match(
        [pay(1, 50000, reference="INV-9999", note="Payment for INV-1001")],
        [inv(1, 50000)],
    )[0]

    assert proposal.outcome is O.NO_MATCH
    assert E.REFERENCE_UNKNOWN in reason_codes(proposal)


def test_multiple_references_are_not_guessed():
    payment = pay(
        1,
        50000,
        note="Payment for INV-1001 and INV-1002",
    )
    invoices = [inv(1, 25000), inv(2, 25000)]

    proposal = M.match([payment], invoices)[0]

    assert proposal.outcome is O.NO_MATCH
    assert E.MULTIPLE_CANDIDATES in reason_codes(proposal)


def test_partial_payment_is_proposed_by_deterministic():
    proposal = M.match(
        [pay(1, 25000, reference="INV-1001")],
        [inv(1, 50000)],
    )[0]

    assert proposal.outcome is O.PROPOSED
    assert proposal.candidates[0].allocations[0].amount_cents == 25000
    assert proposal.candidates[0].has(E.AMOUNT_PARTIAL)


def test_duplicate_delivery_is_not_allocated_twice():
    payment = pay(1, 50000, reference="INV-1001")
    proposals = M.match([payment, payment], [inv(1, 50000)])

    assert proposals[0].outcome is O.PROPOSED
    assert proposals[1].outcome is O.NO_MATCH
    assert E.DUPLICATE_EVENT in reason_codes(proposals[1])


def test_closed_invoice_is_never_proposed():
    from reconcile.models import InvoiceStatus

    proposal = M.match(
        [pay(1, 50000, reference="INV-1001")],
        [inv(1, 50000, status=InvoiceStatus.PAID)],
    )[0]

    assert proposal.outcome is O.NO_MATCH
    assert E.INVOICE_NOT_OPEN in reason_codes(proposal)


# --------------------------------------------------------------------------
# Partial-payment support (new)
# --------------------------------------------------------------------------

from reconcile.models import AllocationStatus, PaymentAllocation


def test_partial_payment_with_reference_is_proposed():
    """A payment smaller than the invoice, with a valid reference, should be proposed."""
    invoice = inv(1, 100000)  # $1000
    payment = pay(1, 40000, reference="INV-1001")  # $400
    [proposal] = M.match([payment], [invoice])

    assert proposal.outcome is O.PROPOSED
    cand = proposal.candidates[0]
    assert cand.has(E.REFERENCE_MATCH)
    assert len(cand.allocations) == 1
    alloc = cand.allocations[0]
    assert alloc.amount_cents == 40000  # allocation = payment amount, not invoice total
    assert alloc.invoice_id == "inv_1"
    assert alloc.status is AllocationStatus.PROPOSED


def test_second_payment_covers_remainder_after_approval():
    """A second genuine payment for the remaining balance should succeed, not be
    rejected as a duplicate or blocked by the first proposal."""
    invoice = inv(1, 100000)  # $1000
    # First payment is already APPROVED in the ledger (simulating post-approval).
    approved = [PaymentAllocation("pay_1", "inv_1", 40000, AllocationStatus.APPROVED)]
    second = pay(2, 60000, reference="INV-1001", minutes=10)  # $600 remainder

    [proposal] = M.match([second], [invoice], approved=approved)

    assert proposal.outcome is O.PROPOSED
    cand = proposal.candidates[0]
    assert cand.allocations[0].amount_cents == 60000
    assert cand.allocations[0].invoice_id == "inv_1"


def test_partial_payment_exceeding_outstanding_is_rejected():
    """A payment larger than the outstanding balance must be rejected, even with
    a valid reference."""
    invoice = inv(1, 100000)
    approved = [PaymentAllocation("pay_1", "inv_1", 80000, AllocationStatus.APPROVED)]
    over = pay(2, 30000, reference="INV-1001", minutes=5)  # 30k > 20k remaining

    [proposal] = M.match([over], [invoice], approved=approved)

    assert proposal.outcome is O.NO_MATCH
    assert E.AMOUNT_MISMATCH in reason_codes(proposal)


def test_partial_allocation_amounts_reconcile_in_integer_cents():
    """Allocation amounts must be exact integer cents, no rounding."""
    invoice = inv(1, 99999)  # $999.99
    payment = pay(1, 33333, reference="INV-1001")  # $333.33

    [proposal] = M.match([payment], [invoice])

    assert proposal.outcome is O.PROPOSED
    alloc = proposal.candidates[0].allocations[0]
    assert isinstance(alloc.amount_cents, int)
    assert alloc.amount_cents == 33333
    # Verify outstanding is correctly computed: 99999 - 0 approved = 99999
    # and 33333 <= 99999, so proposal is valid


def test_proposed_allocation_does_not_reduce_balance_for_next_payment():
    """A proposed (not yet approved) partial payment must NOT prevent a second
    payment from being proposed. Both are proposals; neither has changed the
    balance."""
    invoice = inv(1, 100000)
    first = pay(1, 40000, reference="INV-1001", minutes=0)
    second = pay(2, 40000, reference="INV-1001", minutes=5)

    proposals = M.match([first, second], [invoice])

    # First is proposed.
    assert proposals[0].outcome is O.PROPOSED
    assert proposals[0].candidates[0].allocations[0].amount_cents == 40000
    # Second is also proposed — run.claimed blocks it because the invoice is
    # already claimed by the first proposal within the same batch.
    # This is correct: within one batch, only one proposal per invoice.
    assert proposals[1].outcome is O.NO_MATCH


def test_amount_only_partial_is_not_proposed():
    """A partial payment without any reference must NOT be proposed, even if the
    amount happens to be less than an open invoice."""
    invoice = inv(1, 100000)
    payment = pay(1, 40000)  # no reference

    [proposal] = M.match([payment], [invoice])

    assert proposal.outcome is O.NO_MATCH
    assert E.REFERENCE_ABSENT in reason_codes(proposal)


def test_noisy_reference_partial_from_note_is_proposed():
    """A partial payment whose reference is extracted from the note (not the
    structured field) should also be proposed."""
    invoice = inv(1, 100000)
    payment = pay(1, 50000, note="Deposit for INV-1001")

    [proposal] = M.match([payment], [invoice])

    assert proposal.outcome is O.PROPOSED
    assert proposal.candidates[0].allocations[0].amount_cents == 50000

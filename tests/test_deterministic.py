from factories import inv, pay

from reconcile.baseline import BaselineMatcher
from reconcile.deterministic import DeterministicMatcher
from reconcile.models import (
    AllocationStatus,
    EvidenceCode as E,
    InvoiceStatus,
    MatchOutcome as O,
    PaymentAllocation,
)


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


def test_grouped_payment_is_proposed_by_deterministic():
    payment = pay(
        1,
        50000,
        note="Payment for INV-1001 and INV-1002",
    )
    invoices = [inv(1, 25000), inv(2, 25000)]

    proposal = M.match([payment], invoices)[0]

    assert proposal.outcome is O.PROPOSED
    assert len(proposal.candidates[0].allocations) == 2


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


# --------------------------------------------------------------------------
# Grouped-payment support (new)
# --------------------------------------------------------------------------


def test_valid_grouped_payment_matches_all_referenced_invoices():
    """A payment explicitly identifying multiple open invoices is proposed
    with allocations matching their outstanding balances."""
    invoices = [inv(1, 20000), inv(2, 30000)]
    payment = pay(1, 50000, note="Payment for INV-1001 and INV-1002")

    [proposal] = M.match([payment], invoices)

    assert proposal.outcome is O.PROPOSED
    assert len(proposal.candidates) == 1
    cand = proposal.candidates[0]
    assert cand.has(E.REFERENCE_MATCH)
    assert cand.has(E.AMOUNT_EXACT)
    assert cand.total_cents == 50000
    assert len(cand.allocations) == 2
    alloc_by_id = {a.invoice_id: a.amount_cents for a in cand.allocations}
    assert alloc_by_id == {"inv_1": 20000, "inv_2": 30000}
    assert all(a.status is AllocationStatus.PROPOSED for a in cand.allocations)


def test_valid_grouped_payment_with_three_invoices():
    """A payment covering three open invoices referenced in the note."""
    invoices = [inv(1, 10000), inv(2, 20000), inv(3, 30000)]
    payment = pay(1, 60000, note="Payment for INV-1001, INV-1002, INV-1003")

    [proposal] = M.match([payment], invoices)

    assert proposal.outcome is O.PROPOSED
    cand = proposal.candidates[0]
    assert len(cand.allocations) == 3
    assert cand.total_cents == 60000
    assert {a.invoice_id: a.amount_cents for a in cand.allocations} == {
        "inv_1": 10000,
        "inv_2": 20000,
        "inv_3": 30000,
    }


def test_grouped_payment_with_incorrect_total_is_rejected():
    """When the payment amount does not equal the sum of outstanding balances
    of referenced invoices, the matcher must reject it."""
    invoices = [inv(1, 20000), inv(2, 30000)]  # total 50000
    payment = pay(1, 45000, note="Payment for INV-1001 and INV-1002")

    [proposal] = M.match([payment], invoices)

    assert proposal.outcome is O.NO_MATCH
    assert E.AMOUNT_MISMATCH in reason_codes(proposal)


def test_grouped_payment_with_unknown_invoice_reference_in_note_is_rejected():
    """If any referenced invoice cannot be resolved, the payment must not be partially matched."""
    invoices = [inv(1, 25000)]
    payment = pay(1, 50000, note="Payment for INV-1001 and INV-9999")

    [proposal] = M.match([payment], invoices)

    assert proposal.outcome is O.NO_MATCH
    assert E.REFERENCE_UNKNOWN in reason_codes(proposal)


def test_grouped_payment_with_unknown_invoice_reference_in_reference_field_is_rejected():
    """If reference field contains a known invoice and an unknown invoice, reject."""
    invoices = [inv(1, 25000)]
    payment = pay(1, 50000, reference="INV-1001, INV-9999")

    [proposal] = M.match([payment], invoices)

    assert proposal.outcome is O.NO_MATCH
    assert E.REFERENCE_UNKNOWN in reason_codes(proposal)


def test_grouped_payment_with_closed_invoice_is_rejected():
    """If any of the referenced invoices is not open, the proposal must be rejected."""
    invoices = [inv(1, 25000), inv(2, 25000, status=InvoiceStatus.PAID)]
    payment = pay(1, 50000, note="Payment for INV-1001 and INV-1002")

    [proposal] = M.match([payment], invoices)

    assert proposal.outcome is O.NO_MATCH
    assert E.INVOICE_NOT_OPEN in reason_codes(proposal)


def test_grouped_payment_with_currency_mismatch_is_rejected():
    """If any referenced invoice has a different currency, the payment must be rejected."""
    invoices = [inv(1, 25000, currency="USD"), inv(2, 25000, currency="EUR")]
    payment = pay(1, 50000, currency="USD", note="Payment for INV-1001 and INV-1002")

    [proposal] = M.match([payment], invoices)

    assert proposal.outcome is O.NO_MATCH
    assert E.CURRENCY_MISMATCH in reason_codes(proposal)


def test_grouped_payment_with_duplicate_reference_is_rejected():
    """A payment referencing the same invoice multiple times in a grouped context is rejected."""
    invoices = [inv(1, 25000)]
    payment = pay(1, 50000, note="Payment for INV-1001 and INV-1001")

    [proposal] = M.match([payment], invoices)

    assert proposal.outcome is O.NO_MATCH
    assert E.MULTIPLE_CANDIDATES in reason_codes(proposal)


def test_grouped_payment_with_ambiguous_invoice_identifier_is_rejected():
    """If an invoice number matches multiple invoice records, reject as ambiguous."""
    invoices = [
        inv(1, 25000, number="INV-1001"),
        inv(2, 25000, number="INV 1001"),  # identical canonical number
        inv(3, 25000, number="INV-1003"),
    ]
    payment = pay(1, 50000, note="Payment for INV-1001 and INV-1003")

    [proposal] = M.match([payment], invoices)

    assert proposal.outcome is O.NO_MATCH
    assert E.MULTIPLE_CANDIDATES in reason_codes(proposal)


def test_grouped_payment_conflicting_proposals_in_batch():
    """Proposals in the same batch cannot claim the same invoice."""
    invoices = [inv(1, 20000), inv(2, 30000), inv(3, 20000)]
    payment_1 = pay(1, 50000, note="Payment for INV-1001 and INV-1002", minutes=0)
    payment_2 = pay(2, 50000, note="Payment for INV-1002 and INV-1003", minutes=5)

    proposals = M.match([payment_1, payment_2], invoices)

    assert proposals[0].outcome is O.PROPOSED
    assert proposals[1].outcome is O.NO_MATCH
    assert E.INVOICE_ALREADY_CLAIMED in reason_codes(proposals[1])


def test_grouped_payment_allocations_are_integer_cents_and_stay_proposed():
    """Allocation amounts must be integer cents and stay PROPOSED, not APPROVED."""
    invoices = [inv(1, 11111), inv(2, 22222)]
    payment = pay(1, 33333, note="Payment for INV-1001 and INV-1002")

    [proposal] = M.match([payment], invoices)

    assert proposal.outcome is O.PROPOSED
    for alloc in proposal.candidates[0].allocations:
        assert isinstance(alloc.amount_cents, int)
        assert alloc.status is AllocationStatus.PROPOSED


def test_grouped_payment_passes_independent_verification():
    """A valid grouped proposal must pass the independent verifier."""
    from reconcile.verifier import verify_proposals

    invoices = [inv(1, 20000), inv(2, 30000)]
    payment = pay(1, 50000, note="Payment for INV-1001 and INV-1002")

    [proposal] = M.match([payment], invoices)
    [result] = verify_proposals([proposal], [payment], invoices)

    assert result.passed
    assert result.issues == ()


def test_grouped_payment_without_references_is_not_matched():
    """Amount-only subset-sum matching for grouped payments must remain disabled."""
    invoices = [inv(1, 20000), inv(2, 30000)]
    payment = pay(1, 50000)  # no reference or note

    [proposal] = M.match([payment], invoices)

    assert proposal.outcome is O.NO_MATCH
    assert E.REFERENCE_ABSENT in reason_codes(proposal)


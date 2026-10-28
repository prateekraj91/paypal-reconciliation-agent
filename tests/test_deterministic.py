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


def test_partial_payment_is_not_proposed_yet():
    proposal = M.match(
        [pay(1, 25000, reference="INV-1001")],
        [inv(1, 50000)],
    )[0]

    assert proposal.outcome is O.NO_MATCH
    assert E.AMOUNT_MISMATCH in reason_codes(proposal)


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

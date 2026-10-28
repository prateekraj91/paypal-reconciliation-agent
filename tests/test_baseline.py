from factories import inv, pay

from reconcile.baseline import BaselineConfig, BaselineMatcher
from reconcile.interfaces import InvoiceSource, Matcher, PaymentSource, StaticSource
from reconcile.models import (
    AllocationStatus, EvidenceCode as E, InvoiceStatus, MatchOutcome as O, PaymentAllocation,
    PaymentStatus, ReconciliationStatus,
)

M = BaselineMatcher()


def codes(prop):
    return {e.code for e in prop.reasons}


def test_reference_and_amount_propose_with_evidence():
    [prop] = M.match([pay(1, 50000, reference="inv-1001")], [inv(1, 50000)])
    assert prop.outcome is O.PROPOSED
    cand = prop.candidates[0]
    assert cand.has(E.REFERENCE_MATCH) and cand.has(E.AMOUNT_EXACT)
    assert cand.allocations == (PaymentAllocation("pay_1", "inv_1", 50000),)
    assert cand.allocations[0].status is AllocationStatus.PROPOSED  # never approved
    assert prop.initial_status is ReconciliationStatus.PROPOSED


def test_partial_payment_is_not_proposed_by_the_baseline():
    [prop] = M.match([pay(1, 40000, reference="INV-1001")], [inv(1, 100000)])
    assert prop.outcome is O.NO_MATCH and E.AMOUNT_MISMATCH in codes(prop)


def test_unknown_reference_does_not_fall_back_to_amount():
    [prop] = M.match([pay(1, 50000, reference="INV-9999")], [inv(1, 50000)])
    assert prop.outcome is O.NO_MATCH and E.REFERENCE_UNKNOWN in codes(prop)


def test_reference_buried_in_free_text_is_not_extracted():
    [prop] = M.match([pay(1, 50000, reference="Payment for INV-1001")], [inv(1, 50000)])
    assert prop.outcome is O.NO_MATCH


def test_amount_only_unique_is_a_flagged_proposal():
    [prop] = M.match([pay(1, 50000)], [inv(1, 50000), inv(2, 70000, customer="Other Co")])
    assert prop.outcome is O.PROPOSED
    cand = prop.candidates[0]
    assert cand.has(E.AMOUNT_EXACT) and cand.has(E.REFERENCE_ABSENT)
    assert not cand.has(E.REFERENCE_MATCH)  # weak evidence is labelled as weak


def test_strict_config_refuses_amount_only():
    strict = BaselineMatcher(BaselineConfig(allow_amount_only=False))
    [prop] = strict.match([pay(1, 50000)], [inv(1, 50000)])
    assert prop.outcome is O.NO_MATCH and E.REFERENCE_ABSENT in codes(prop)
    assert strict.name == "baseline-strict"


def test_same_amount_on_two_invoices_is_ambiguous_not_guessed():
    [prop] = M.match([pay(1, 50000)], [inv(1, 50000), inv(2, 50000, customer="Other Co")])
    assert prop.outcome is O.AMBIGUOUS and len(prop.candidates) == 2
    assert E.MULTIPLE_CANDIDATES in codes(prop)
    assert prop.initial_status is ReconciliationStatus.NEEDS_REVIEW


def test_closed_invoices_are_ignored_for_amount_matching():
    paid = inv(1, 50000, customer="Other Co", status=InvoiceStatus.PAID)
    [prop] = M.match([pay(1, 50000)], [paid, inv(2, 50000)])
    assert prop.outcome is O.PROPOSED and prop.candidates[0].allocations[0].invoice_id == "inv_2"


def test_reference_to_closed_invoice_is_blocked():
    [prop] = M.match([pay(1, 50000, reference="INV-1001")], [inv(1, 50000, status=InvoiceStatus.CANCELLED)])
    assert prop.outcome is O.NO_MATCH and E.INVOICE_NOT_OPEN in codes(prop)


def test_duplicate_delivery_is_not_double_allocated():
    p = pay(1, 50000, reference="INV-1001")
    first, second = M.match([p, p], [inv(1, 50000)])
    assert first.outcome is O.PROPOSED
    assert second.outcome is O.NO_MATCH and E.DUPLICATE_EVENT in codes(second)


def test_second_payment_for_same_invoice_is_blocked_by_the_first_proposal():
    a = pay(1, 50000, reference="INV-1001", minutes=0)
    b = pay(2, 50000, reference="INV-1001", minutes=5)
    first, second = M.match([a, b], [inv(1, 50000)])
    assert first.outcome is O.PROPOSED
    assert second.outcome is O.NO_MATCH and E.INVOICE_ALREADY_CLAIMED in codes(second)


def test_non_completed_payments_are_never_matched():
    for status in (PaymentStatus.REFUNDED, PaymentStatus.PENDING, PaymentStatus.REVERSED, PaymentStatus.DENIED):
        [prop] = M.match([pay(1, 50000, reference="INV-1001", status=status)], [inv(1, 50000)])
        assert prop.outcome is O.NO_MATCH and E.PAYMENT_NOT_COMPLETED in codes(prop)


def test_currency_mismatch_is_blocked():
    [prop] = M.match([pay(1, 50000, reference="INV-1001", currency="EUR")], [inv(1, 50000)])
    assert prop.outcome is O.NO_MATCH and E.CURRENCY_MISMATCH in codes(prop)


def test_non_unique_invoice_number_is_not_guessed():
    twins = [inv(1, 50000, number="INV-7"), inv(2, 50000, number="inv 7")]
    [prop] = M.match([pay(1, 50000, reference="INV-7")], twins)
    assert prop.outcome is O.NO_MATCH and E.MULTIPLE_CANDIDATES in codes(prop)


def test_only_approved_allocations_reduce_the_balance():
    invoice = inv(1, 100000)
    approved = [PaymentAllocation("old", "inv_1", 40000, AllocationStatus.APPROVED)]
    proposed_only = [PaymentAllocation("old", "inv_1", 40000, AllocationStatus.PROPOSED)]
    remainder = pay(1, 60000, reference="INV-1001")
    assert M.match([remainder], [invoice], approved)[0].outcome is O.PROPOSED
    assert M.match([remainder], [invoice], proposed_only)[0].outcome is O.NO_MATCH


def test_processing_is_chronological_and_one_proposal_per_delivery():
    late = pay(2, 50000, reference="INV-1001", minutes=10)
    early = pay(1, 50000, reference="INV-1001", minutes=0)
    out = M.match([late, early], [inv(1, 50000)])
    assert [p.payment_id for p in out] == ["pay_1", "pay_2"]
    assert out[0].outcome is O.PROPOSED and out[1].outcome is O.NO_MATCH


def test_interfaces_are_satisfied():
    src = StaticSource((inv(1, 100),), (pay(1, 100),))
    assert isinstance(src, InvoiceSource) and isinstance(src, PaymentSource)
    assert isinstance(M, Matcher)

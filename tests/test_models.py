import pytest
from factories import inv, pay

from reconcile.models import (
    AllocationStatus, Candidate, Evidence, EvidenceCode, InvoiceStatus, MatchOutcome,
    MatchProposal, PaymentAllocation, ReconciliationStatus, outstanding_cents,
)


def alloc(payment="pay_1", invoice="inv_1", amount=100, status=AllocationStatus.PROPOSED):
    return PaymentAllocation(payment, invoice, amount, status)


def test_rejects_non_positive_money():
    with pytest.raises(ValueError):
        inv(1, 0)
    with pytest.raises(ValueError):
        pay(1, -5)
    with pytest.raises(ValueError):
        alloc(amount=0)


def test_only_sent_and_partially_paid_invoices_are_open():
    assert InvoiceStatus.SENT.is_open and InvoiceStatus.PARTIALLY_PAID.is_open
    assert not InvoiceStatus.PAID.is_open and not InvoiceStatus.CANCELLED.is_open


def test_outstanding_counts_only_approved_allocations():
    i = inv(1, 1000)
    allocs = [
        alloc(amount=300, status=AllocationStatus.APPROVED),
        alloc(payment="pay_2", amount=200, status=AllocationStatus.PROPOSED),
        alloc(payment="pay_3", amount=100, status=AllocationStatus.REJECTED),
        alloc(payment="pay_4", invoice="inv_other", amount=999, status=AllocationStatus.APPROVED),
    ]
    assert outstanding_cents(i, allocs) == 700


def test_outstanding_does_not_hide_over_allocation():
    i = inv(1, 1000)
    assert outstanding_cents(i, [alloc(amount=1500, status=AllocationStatus.APPROVED)]) == -500


def test_candidate_rules():
    with pytest.raises(ValueError):
        Candidate(())
    with pytest.raises(ValueError):  # two payments in one candidate
        Candidate((alloc(payment="a"), alloc(payment="b", invoice="inv_2")))
    with pytest.raises(ValueError):  # same invoice twice
        Candidate((alloc(), alloc()))
    with pytest.raises(ValueError):  # matchers cannot emit approvals
        Candidate((alloc(status=AllocationStatus.APPROVED),))


def test_proposal_invariants():
    cand = Candidate((alloc(),), (Evidence(EvidenceCode.AMOUNT_EXACT),))
    cand2 = Candidate((alloc(invoice="inv_2"),))
    with pytest.raises(ValueError):
        MatchProposal("pay_1", MatchOutcome.PROPOSED)
    with pytest.raises(ValueError):
        MatchProposal("pay_1", MatchOutcome.AMBIGUOUS, (cand,))
    with pytest.raises(ValueError):
        MatchProposal("pay_1", MatchOutcome.NO_MATCH, (cand,))
    with pytest.raises(ValueError):
        MatchProposal("pay_OTHER", MatchOutcome.PROPOSED, (cand,))
    MatchProposal("pay_1", MatchOutcome.PROPOSED, (cand,))
    MatchProposal("pay_1", MatchOutcome.AMBIGUOUS, (cand, cand2))
    MatchProposal("pay_1", MatchOutcome.NO_MATCH)


def test_initial_reconciliation_status_never_reconciled():
    cand = Candidate((alloc(),))
    cand2 = Candidate((alloc(invoice="inv_2"),))
    assert MatchProposal("pay_1", MatchOutcome.PROPOSED, (cand,)).initial_status is ReconciliationStatus.PROPOSED
    assert MatchProposal("pay_1", MatchOutcome.AMBIGUOUS, (cand, cand2)).initial_status is ReconciliationStatus.NEEDS_REVIEW
    assert MatchProposal("pay_1", MatchOutcome.NO_MATCH).initial_status is ReconciliationStatus.UNMATCHED

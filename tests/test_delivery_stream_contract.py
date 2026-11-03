"""The delivery-stream contract, stated only through the pre-fix public API.

Every test here uses APIs that already existed at commit d72cb21: the matchers,
verify_proposals and Dataset.unique_payments. Nothing imports delivery.py or
matching.py. That is deliberate: this file can be dropped into a checkout of
d72cb21 unchanged, where 11 of its 12 tests fail, which is what makes it
evidence rather than assertion. It also keeps the contract pinned to observable
behaviour, so a future refactor of the resolver cannot satisfy the tests by
changing their own helpers.
"""
from datetime import timedelta
import pytest
from factories import T0, inv, pay
from reconcile.dataset import Dataset
from reconcile.deterministic import DeterministicMatcher
from reconcile.models import (
    Candidate, Evidence, EvidenceCode, MatchOutcome,
    MatchProposal, Payment, PaymentAllocation, PaymentStatus,
)
from reconcile.verifier import verify_proposals


def at(minutes, status, amount=50000, payer="Acme Studio"):
    return Payment(payment_id="pay_1", amount_cents=amount, currency="USD",
                   received_at=T0 + timedelta(minutes=minutes),
                   payer_name=payer, reference="INV-1001", status=status)


def ref_proposal(invoice):
    return MatchProposal(
        payment_id="pay_1", outcome=MatchOutcome.PROPOSED,
        candidates=(Candidate(
            (PaymentAllocation("pay_1", invoice.invoice_id, 50000, currency=invoice.currency),),
            (Evidence(EvidenceCode.REFERENCE_MATCH, f"= {invoice.number}"),
             Evidence(EvidenceCode.AMOUNT_EXACT, "exact")),
        ),), matcher="test")


@pytest.mark.parametrize("pending_min,completed_min", [(0, 5), (0, 1440), (7, 9)])
@pytest.mark.parametrize("order", ["pending_first", "completed_first"])
def test_received_at_transition_end_to_end(pending_min, completed_min, order):
    invoice = inv(1, 50000)
    p, c = at(pending_min, PaymentStatus.PENDING), at(completed_min, PaymentStatus.COMPLETED)
    stream = [p, c] if order == "pending_first" else [c, p]
    proposed = [x for x in DeterministicMatcher().match(stream, [invoice])
                if x.outcome is MatchOutcome.PROPOSED]
    assert len(proposed) == 1
    results = verify_proposals(proposed, stream, [invoice])
    assert [r.passed for r in results] == [True], results[0].issues


def test_verifier_canonicalizes_pending_then_completed():
    invoice = inv(1, 50000)
    stream = [at(0, PaymentStatus.PENDING), at(5, PaymentStatus.COMPLETED)]
    r = verify_proposals([ref_proposal(invoice)], stream, [invoice])[0]
    assert r.passed, r.issues


def test_verifier_does_not_falsely_reject_identical_redeliveries():
    invoice = inv(1, 50000)
    stream = [at(0, PaymentStatus.COMPLETED), at(0, PaymentStatus.COMPLETED)]
    r = verify_proposals([ref_proposal(invoice)], stream, [invoice])[0]
    assert r.passed, r.issues


def test_verifier_rejects_conflicting_deliveries_with_a_conflict_reason():
    invoice = inv(1, 50000)
    stream = [at(0, PaymentStatus.COMPLETED), at(0, PaymentStatus.COMPLETED, payer="Someone Else")]
    r = verify_proposals([ref_proposal(invoice)], stream, [invoice])[0]
    assert not r.passed
    assert any("conflicting deliveries" in i for i in r.issues), r.issues


def test_verifier_rejects_a_fabricated_payment_record_specifically():
    invoice = inv(1, 50000)
    r = verify_proposals([ref_proposal(invoice)], [pay(2, 50000)], [invoice])[0]
    assert not r.passed
    assert any("payment record is missing" in i for i in r.issues), r.issues


def test_stale_pending_does_not_downgrade_completed():
    invoice = inv(1, 50000)
    stream = [at(5, PaymentStatus.COMPLETED), at(60, PaymentStatus.PENDING)]
    proposed = [x for x in DeterministicMatcher().match(stream, [invoice])
                if x.outcome is MatchOutcome.PROPOSED]
    results = verify_proposals(proposed, stream, [invoice])
    assert [r.passed for r in results] == [True], results[0].issues


def test_dataset_unique_payments_survives_a_transition():
    ds = Dataset(seed=0, invoices=(inv(1, 50000),),
                 payments=(at(0, PaymentStatus.PENDING), at(5, PaymentStatus.COMPLETED)),
                 truth={})
    u = ds.unique_payments
    assert len(u) == 1 and u[0].status is PaymentStatus.COMPLETED

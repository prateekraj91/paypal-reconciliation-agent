import pytest

from reconcile.baseline import BaselineConfig, BaselineMatcher
from reconcile.dataset import CaseCategory, Resolution, generate_dataset
from reconcile.models import (
    Candidate, MatchOutcome, MatchProposal, PaymentAllocation,
)
from reconcile.scoring import score

DS = generate_dataset(2026)


def oracle(ds):
    seen, out = set(), []
    for p in ds.payments:
        t = ds.truth[p.payment_id]
        if p.payment_id not in seen and t.resolution is Resolution.AUTO_MATCH:
            allocs = tuple(PaymentAllocation(p.payment_id, i, a) for i, a in t.allocations)
            out.append(MatchProposal(p.payment_id, MatchOutcome.PROPOSED, (Candidate(allocs),), (), "oracle"))
        else:
            out.append(MatchProposal(p.payment_id, MatchOutcome.NO_MATCH, (), (), "oracle"))
        seen.add(p.payment_id)
    return out


def test_oracle_scores_perfectly():
    card = score("oracle", oracle(DS), DS)
    assert card.precision == 1.0 and card.recall == 1.0 and card.false_auto == 0


def test_wrong_allocation_is_a_false_auto_match():
    props = oracle(DS)
    idx = next(i for i, p in enumerate(props) if p.outcome is MatchOutcome.PROPOSED)
    victim = props[idx]
    wrong = PaymentAllocation(victim.payment_id, "inv_WRONG", victim.candidates[0].total_cents)
    props[idx] = MatchProposal(victim.payment_id, MatchOutcome.PROPOSED, (Candidate((wrong,)),), (), "oracle")
    assert score("x", props, DS).false_auto == 1


def test_proposing_where_a_human_should_decide_is_a_false_auto_match():
    props = oracle(DS)
    idx = next(i for i, p in enumerate(DS.payments) if DS.truth[p.payment_id].resolution is Resolution.NEEDS_HUMAN)
    pid = DS.payments[idx].payment_id
    bogus = PaymentAllocation(pid, "inv_0001", DS.payments[idx].amount_cents)
    props[idx] = MatchProposal(pid, MatchOutcome.PROPOSED, (Candidate((bogus,)),), (), "x")
    assert score("x", props, DS).false_auto == 1


def test_matcher_must_answer_every_delivery():
    with pytest.raises(ValueError):
        score("x", oracle(DS)[:-1], DS)


def test_baseline_blind_spots_and_traps():
    card = score("baseline", BaselineMatcher().match(DS.payments, DS.invoices), DS)
    for blind in (
        CaseCategory.NOISY_REFERENCE, CaseCategory.PARTIAL_PAYMENT, CaseCategory.GROUPED_PAYMENT_NOTE,
        CaseCategory.GROUPED_PAYMENT_NO_REF, CaseCategory.PAYER_NAME_VARIATION,
        CaseCategory.AMBIGUOUS_COMBINATION,
    ):
        assert card.by_category[blind].proposed == 0
    # The amount-only trap is the baseline's only source of false automatic matches.
    collision = card.by_category[CaseCategory.AMOUNT_COLLISION]
    assert collision.false_auto == collision.payments > 0
    assert card.false_auto == collision.false_auto
    # No duplicate or double payment is ever allocated twice.
    assert card.by_category[CaseCategory.DUPLICATE_EVENT].false_auto == 0
    assert card.by_category[CaseCategory.DOUBLE_PAYMENT].false_auto == 0


def test_strict_baseline_never_makes_a_false_automatic_match():
    strict = BaselineMatcher(BaselineConfig(allow_amount_only=False))
    card = score(strict.name, strict.match(DS.payments, DS.invoices), DS)
    assert card.false_auto == 0 and card.precision == 1.0

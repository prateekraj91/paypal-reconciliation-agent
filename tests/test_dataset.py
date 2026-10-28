from itertools import combinations

from reconcile.dataset import CaseCategory, DEFAULT_MIX, Resolution, generate_dataset
from reconcile.models import InvoiceStatus

DS = generate_dataset(2026)
INV = {i.invoice_id: i for i in DS.invoices}
PAY = {p.payment_id: p for p in DS.payments}


def test_same_seed_is_identical_and_different_seed_differs():
    again = generate_dataset(2026)
    assert again.invoices == DS.invoices and again.payments == DS.payments and again.truth == DS.truth
    other = generate_dataset(7)
    assert other.payments != DS.payments


def test_size_and_category_coverage():
    assert DS.unique_payment_count >= 100
    assert {t.category for t in DS.truth.values()} == set(CaseCategory)
    assert set(DEFAULT_MIX) == set(CaseCategory)


def test_ids_unique_and_truth_covers_every_payment():
    assert len({i.invoice_id for i in DS.invoices}) == len(DS.invoices)
    assert len({i.number for i in DS.invoices}) == len(DS.invoices)
    assert set(DS.truth) == set(PAY)
    assert len(DS.payments) > len(PAY)  # duplicate deliveries exist
    assert list(DS.payments) == sorted(DS.payments, key=lambda p: (p.received_at, p.payment_id))


def test_auto_match_truth_is_financially_consistent():
    for t in DS.truth.values():
        p = PAY[t.payment_id]
        if t.resolution is Resolution.AUTO_MATCH:
            assert t.allocations
            assert sum(a for _, a in t.allocations) == p.amount_cents
            for inv_id, amount in t.allocations:
                assert INV[inv_id].is_open and amount <= INV[inv_id].amount_cents
            if t.category is CaseCategory.PARTIAL_PAYMENT:
                assert all(amount < INV[i].amount_cents for i, amount in t.allocations)
        if t.resolution is Resolution.NO_ACTION:
            assert not t.allocations


def test_needs_human_answers_still_sum_to_the_payment():
    for t in DS.truth.values():
        if t.resolution is Resolution.NEEDS_HUMAN and t.allocations:
            assert sum(a for _, a in t.allocations) == PAY[t.payment_id].amount_cents


def test_combination_cases_really_have_two_valid_subsets():
    found = 0
    for t in DS.truth.values():
        if t.category is not CaseCategory.AMBIGUOUS_COMBINATION:
            continue
        found += 1
        payer = PAY[t.payment_id].payer_name
        open_amounts = [i.amount_cents for i in DS.invoices if i.customer_name == payer and i.is_open]
        target = PAY[t.payment_id].amount_cents
        valid = [
            c for r in range(1, len(open_amounts) + 1)
            for c in combinations(range(len(open_amounts)), r)
            if sum(open_amounts[k] for k in c) == target
        ]
        assert len(valid) >= 2
    assert found == DEFAULT_MIX[CaseCategory.AMBIGUOUS_COMBINATION]


def test_traps_exist():
    closed = [i for i in DS.invoices if not i.is_open]
    assert closed and all(i.status in (InvoiceStatus.PAID, InvoiceStatus.CANCELLED) for i in closed)
    open_amounts = {i.amount_cents for i in DS.invoices if i.is_open}
    assert all(i.amount_cents in open_amounts for i in closed)  # closed distractors share an open amount
    for t in DS.truth.values():
        if t.category is CaseCategory.AMOUNT_COLLISION:
            assert PAY[t.payment_id].amount_cents in open_amounts
            assert t.resolution is Resolution.NEEDS_HUMAN and not t.allocations

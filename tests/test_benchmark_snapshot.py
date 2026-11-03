"""Snapshot equivalence for the reconciliation benchmark across seeds 0-9.

These digests were recorded from commit d72cb21, before the F2/F3/F4/F7 fixes,
and they cover every per-payment decision: the outcome, the proposed
allocations, the payment-level evidence codes and whether the proposal passed
independent verification.

The point of the snapshot is that a correctness fix must not quietly move the
benchmark. If a change here is genuinely intended, the headline numbers in
test_recorded_headline_metrics must be re-justified in the same commit, not
re-recorded to make the suite green.
"""

import hashlib

import pytest

from reconcile.baseline import BaselineConfig, BaselineMatcher
from reconcile.dataset import generate_dataset
from reconcile.deterministic import DeterministicMatcher
from reconcile.models import MatchOutcome
from reconcile.scoring import score
from reconcile.verifier import verify_proposals

SEEDS = range(10)


def matchers():
    return (
        BaselineMatcher(),
        BaselineMatcher(BaselineConfig(allow_amount_only=False)),
        DeterministicMatcher(),
    )


# seed -> (baseline, baseline-strict, deterministic-v1)
EXPECTED_DIGESTS: dict[int, tuple[str, str, str]] = {
    0: ("4a3d0a625164f945", "32cc97bf9aeda17a", "31371eb954f84f3d"),
    1: ("4ce7c55430b73540", "44c169e00ec0bbe4", "24a320fcfcd8a60b"),
    2: ("696b4f5d4749ac0b", "85ced09dd07fbc46", "42320082f6329831"),
    3: ("d750278f10f75d55", "faa0a8e1d524f29c", "ea3a44b6519aeadf"),
    4: ("7963926b96fe8893", "695a9aad3c7958a6", "778ec5e856f2f39b"),
    5: ("11053f5d9eb66319", "08a046eadd361490", "2895b299243a5624"),
    6: ("d574756d6588d94e", "0ccf36f14a77e3c4", "c56745c0083b2f1a"),
    7: ("6357766288eaafe5", "98a620e02d98957a", "bd8896ce32c84bed"),
    8: ("bff692bac82a310b", "53c0509a4a632507", "18f9540230a21788"),
    9: ("10248c48c147a39a", "4a045760274459d3", "8315e1954c13cad0"),
}


def behaviour_digest(seed: int, matcher) -> str:
    """A stable fingerprint of every decision this matcher makes on this seed."""
    dataset = generate_dataset(seed)
    proposals = matcher.match(dataset.payments, dataset.invoices)
    proposed = [p for p in proposals if p.outcome is MatchOutcome.PROPOSED]
    passed = {
        result.payment_id
        for result in verify_proposals(
            proposed, dataset.unique_payments, dataset.invoices
        )
        if result.passed
    }

    lines = []
    for proposal in proposals:
        allocations = sorted(
            (a.invoice_id, a.amount_cents, a.currency or "")
            for candidate in proposal.candidates
            for a in candidate.allocations
        )
        codes = sorted(e.code.value for e in proposal.reasons)
        lines.append(
            f"{proposal.payment_id}|{proposal.outcome.value}|{allocations}"
            f"|{codes}|{proposal.payment_id in passed}"
        )

    return hashlib.sha256("\n".join(lines).encode()).hexdigest()[:16]


@pytest.mark.parametrize("seed", SEEDS)
def test_per_payment_decisions_are_unchanged(seed):
    actual = tuple(behaviour_digest(seed, m) for m in matchers())
    assert actual == EXPECTED_DIGESTS[seed]


@pytest.mark.parametrize("seed", SEEDS)
def test_recorded_headline_metrics(seed):
    """The reported scorecard numbers, per matcher, per seed.

    The case mix is fixed by DEFAULT_MIX, so the counts are the same for every
    seed even though the invoices, payers and reference noise differ.
    """
    expected = {
        "baseline": (49, 45, 4, 81, 64),
        "baseline-strict": (33, 33, 0, 81, 80),
        "deterministic-v1": (54, 54, 0, 81, 59),
    }

    dataset = generate_dataset(seed)
    for matcher in matchers():
        card = score(
            matcher.name,
            matcher.match(dataset.payments, dataset.invoices),
            dataset,
        )
        assert (
            card.proposed,
            card.correct_auto,
            card.false_auto,
            card.matchable,
            card.review_queue,
        ) == expected[matcher.name], matcher.name


def test_recorded_precision_and_recall():
    """The published figures: baseline 91.8/55.6, strict 100/40.7, v1 100/66.7."""
    dataset = generate_dataset(0)
    cards = {
        m.name: score(m.name, m.match(dataset.payments, dataset.invoices), dataset)
        for m in matchers()
    }

    assert round(cards["baseline"].precision * 100, 1) == 91.8
    assert round(cards["baseline"].recall * 100, 1) == 55.6
    assert round(cards["baseline-strict"].precision * 100, 1) == 100.0
    assert round(cards["baseline-strict"].recall * 100, 1) == 40.7
    assert round(cards["deterministic-v1"].precision * 100, 1) == 100.0
    assert round(cards["deterministic-v1"].recall * 100, 1) == 66.7


@pytest.mark.parametrize("seed", SEEDS)
def test_deterministic_verifier_passes_every_proposal(seed):
    """The 54/54 verifier result, held for every seed."""
    dataset = generate_dataset(seed)
    proposals = DeterministicMatcher().match(dataset.payments, dataset.invoices)
    proposed = [p for p in proposals if p.outcome is MatchOutcome.PROPOSED]

    results = verify_proposals(proposed, dataset.unique_payments, dataset.invoices)

    assert len(results) == 54
    assert sum(r.passed for r in results) == 54

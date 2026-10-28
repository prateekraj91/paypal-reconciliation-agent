"""Score a matcher's proposals against the dataset's ground truth.

Only PROPOSED outcomes count as "automatic" matches. A proposal is correct
when the payment's truth is AUTO_MATCH and the proposed allocation set equals
the true one. Any other PROPOSED outcome is a false automatic match: a wrong
allocation, a proposal where a human should have decided, or a second
proposal for a payment already delivered (double allocation).
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Mapping, Optional, Sequence

from .dataset import CaseCategory, Dataset, Resolution
from .models import MatchOutcome, MatchProposal


@dataclass
class CategoryScore:
    payments: int = 0
    matchable: int = 0
    proposed: int = 0
    correct: int = 0
    false_auto: int = 0
    missed: int = 0  # matchable but not proposed (needs a human unnecessarily)


@dataclass(frozen=True)
class Scorecard:
    matcher: str
    deliveries: int
    unique_payments: int
    matchable: int
    proposed: int
    correct_auto: int
    false_auto: int
    review_queue: int  # unique payments whose first outcome was not PROPOSED
    by_category: Mapping[CaseCategory, CategoryScore] = field(default_factory=dict)

    @property
    def precision(self) -> Optional[float]:
        return self.correct_auto / self.proposed if self.proposed else None

    @property
    def recall(self) -> Optional[float]:
        return self.correct_auto / self.matchable if self.matchable else None

    @property
    def human_review_rate(self) -> float:
        return self.review_queue / self.unique_payments if self.unique_payments else 0.0


def score(matcher_name: str, proposals: Sequence[MatchProposal], dataset: Dataset) -> Scorecard:
    if Counter(p.payment_id for p in dataset.payments) != Counter(p.payment_id for p in proposals):
        raise ValueError("a matcher must return exactly one proposal per delivered payment")

    seen: set[str] = set()
    cats: dict[CaseCategory, CategoryScore] = defaultdict(CategoryScore)
    unique = matchable = proposed = correct = queue = 0

    for prop in proposals:
        truth = dataset.truth[prop.payment_id]
        cat = cats[truth.category]
        first = prop.payment_id not in seen
        seen.add(prop.payment_id)

        if first:
            unique += 1
            cat.payments += 1
            if truth.resolution is Resolution.AUTO_MATCH:
                matchable += 1
                cat.matchable += 1

        if prop.outcome is MatchOutcome.PROPOSED:
            proposed += 1
            cat.proposed += 1
            got = {(a.invoice_id, a.amount_cents) for a in prop.candidates[0].allocations}
            if first and truth.resolution is Resolution.AUTO_MATCH and got == truth.allocation_set:
                correct += 1
                cat.correct += 1
            else:
                cat.false_auto += 1
        elif first:
            queue += 1
            if truth.resolution is Resolution.AUTO_MATCH:
                cat.missed += 1

    return Scorecard(
        matcher=matcher_name,
        deliveries=len(proposals),
        unique_payments=unique,
        matchable=matchable,
        proposed=proposed,
        correct_auto=correct,
        false_auto=proposed - correct,
        review_queue=queue,
        by_category=dict(cats),
    )


def _pct(x: Optional[float]) -> str:
    return "n/a" if x is None else f"{x * 100:.1f}%"


def format_scorecard(card: Scorecard) -> str:
    lines = [
        f"== {card.matcher} ==",
        f"payments: {card.unique_payments} unique ({card.deliveries} deliveries), matchable: {card.matchable}",
        f"auto-proposed: {card.proposed}  correct: {card.correct_auto}  false: {card.false_auto}",
        f"precision: {_pct(card.precision)}  recall: {_pct(card.recall)}  "
        f"review queue: {card.review_queue} ({_pct(card.human_review_rate)})",
        "",
        f"{'category':<26}{'pays':>5}{'match':>6}{'prop':>5}{'ok':>4}{'false':>6}{'missed':>7}",
    ]
    for category in CaseCategory:
        c = card.by_category.get(category)
        if c is None:
            continue
        lines.append(
            f"{category.value:<26}{c.payments:>5}{c.matchable:>6}{c.proposed:>5}"
            f"{c.correct:>4}{c.false_auto:>6}{c.missed:>7}"
        )
    return "\n".join(lines)

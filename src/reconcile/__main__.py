"""Score all matcher versions on the reproducible synthetic dataset."""

import sys

from .baseline import BaselineConfig, BaselineMatcher
from .dataset import DEFAULT_SEED, generate_dataset
from .deterministic import DeterministicMatcher
from .scoring import format_scorecard, score


def main() -> None:
    seed = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_SEED
    dataset = generate_dataset(seed)

    print(
        f"dataset seed={seed}: {len(dataset.invoices)} invoices, "
        f"{len(dataset.payments)} deliveries\n"
    )

    matchers = (
        BaselineMatcher(),
        BaselineMatcher(BaselineConfig(allow_amount_only=False)),
        DeterministicMatcher(),
    )

    for matcher in matchers:
        proposals = matcher.match(dataset.payments, dataset.invoices)
        card = score(matcher.name, proposals, dataset)
        print(format_scorecard(card))
        print()


if __name__ == "__main__":
    main()

"""python -m reconcile [seed]  ->  score the baseline matchers on the synthetic dataset."""

import sys

from .baseline import BaselineConfig, BaselineMatcher
from .dataset import DEFAULT_SEED, generate_dataset
from .scoring import format_scorecard, score


def main() -> None:
    seed = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_SEED
    ds = generate_dataset(seed)
    print(f"dataset seed={seed}: {len(ds.invoices)} invoices, {len(ds.payments)} deliveries\n")
    for matcher in (BaselineMatcher(), BaselineMatcher(BaselineConfig(allow_amount_only=False))):
        proposals = matcher.match(ds.payments, ds.invoices)
        print(format_scorecard(score(matcher.name, proposals, ds)))
        print()


if __name__ == "__main__":
    main()

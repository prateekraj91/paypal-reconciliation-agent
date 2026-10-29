"""Score matchers and report independent verification results."""

import sys
from collections import Counter

from .baseline import BaselineConfig, BaselineMatcher
from .dataset import DEFAULT_SEED, generate_dataset
from .deterministic import DeterministicMatcher
from .models import MatchOutcome
from .scoring import format_scorecard, score
from .verifier import verify_proposals


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

        # Keep the original benchmark scoring unchanged.
        card = score(matcher.name, proposals, dataset)
        print(format_scorecard(card))

        # Verify only actual match proposals, not NO_MATCH or AMBIGUOUS results.
        proposed = [
            proposal
            for proposal in proposals
            if proposal.outcome is MatchOutcome.PROPOSED
        ]

        results = verify_proposals(
            proposed,
            dataset.unique_payments,
            dataset.invoices,
        )

        passed = sum(result.passed for result in results)
        rejected = len(results) - passed

        print()
        print("Independent verification")
        print(f"  proposals checked: {len(results)}")
        print(f"  passed verification: {passed}")
        print(f"  rejected for review: {rejected}")

        reasons = Counter(
            issue
            for result in results
            for issue in result.issues
        )

        if reasons:
            print("  rejection reasons:")
            for reason, count in reasons.most_common(5):
                print(f"    {count}x {reason}")

        print()


if __name__ == "__main__":
    main()

"""Score matchers and report independent verification results."""

import sys
from collections import Counter

from .baseline import BaselineConfig, BaselineMatcher
from .dataset import DEFAULT_SEED, generate_dataset
from .deterministic import DeterministicMatcher
from .models import MatchOutcome
from .scoring import format_scorecard, score
from .verifier import verify_proposals


SYNC_USAGE = (
    "python -m reconcile sync --sandbox [--days N] [--page-size N]"
)


def _sync_command(argv: list[str]) -> int:
    """`sync --sandbox`: one repeatable synchronisation pass.

    --sandbox is required rather than defaulted. A sync is read-only, but the
    flag makes the target explicit at the call site, so a production run can
    never be something that happened because an argument was left out.
    """
    import argparse
    from datetime import datetime, timedelta, timezone

    from .paypal import (
        TRANSACTION_APPEARANCE_DELAY_HOURS,
        PayPalClient,
        PayPalConfigError,
        PayPalError,
    )
    from .sync import run_sync

    SANDBOX_HOST = "https://api-m.sandbox.paypal.com"

    parser = argparse.ArgumentParser(prog="python -m reconcile sync", usage=SYNC_USAGE)
    parser.add_argument("--sandbox", action="store_true", required=True,
                        help="run against the PayPal Sandbox (required)")
    parser.add_argument("--days", type=int, default=31,
                        help="how far back to search transactions (default 31)")
    parser.add_argument("--page-size", type=int, default=None,
                        help="invoice page size (default: the API maximum)")
    args = parser.parse_args(argv)

    if args.days < 1:
        print("--days must be at least 1", file=sys.stderr)
        return 2

    try:
        client = PayPalClient(timeout=30.0)
    except PayPalConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 1

    if client.base_url != SANDBOX_HOST:
        print(
            f"--sandbox was given but the configured endpoint is "
            f"{client.base_url}, not {SANDBOX_HOST}",
            file=sys.stderr,
        )
        return 2

    end = datetime.now(timezone.utc) - timedelta(hours=TRANSACTION_APPEARANCE_DELAY_HOURS)
    start = end - timedelta(days=args.days)

    print(f"endpoint            : {client.base_url}")
    try:
        result = run_sync(client, start, end, page_size=args.page_size)
    except PayPalError as exc:
        print(f"Sync failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    print(result.report.format())
    if not result.ok:
        print("\nAccounting invariants were violated; this run needs a human.",
              file=sys.stderr)
        return 1
    return 0


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "sync":
        sys.exit(_sync_command(sys.argv[2:]))

    if len(sys.argv) > 1 and sys.argv[1] == "--paypal":
        from .paypal import PayPalClient, PayPalConfigError, PayPalError

        try:
            client = PayPalClient()
        except PayPalConfigError as e:
            print(f"Configuration error: {e}", file=sys.stderr)
            sys.exit(1)

        print(f"Connecting to PayPal ({client.base_url})...")
        try:
            invoices, allocs = client.list_invoices_with_allocations()
            print(
                f"Ingested {len(invoices)} invoices and "
                f"{len(allocs)} approved linked allocations from PayPal."
            )
        except PayPalError as e:
            print(f"PayPal ingestion failed: {e}", file=sys.stderr)
            sys.exit(1)
        return

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

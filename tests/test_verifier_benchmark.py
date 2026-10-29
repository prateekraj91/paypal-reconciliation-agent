from reconcile.baseline import BaselineMatcher
from reconcile.dataset import CaseCategory, DEFAULT_SEED, generate_dataset
from reconcile.models import MatchOutcome
from reconcile.verifier import verify_proposals


def test_amount_collision_proposals_fail_independent_verification():
    dataset = generate_dataset(DEFAULT_SEED)
    proposals = BaselineMatcher().match(
        dataset.payments,
        dataset.invoices,
    )

    collision_proposals = [
        proposal
        for proposal in proposals
        if dataset.truth[proposal.payment_id].category
        is CaseCategory.AMOUNT_COLLISION
        and proposal.outcome is MatchOutcome.PROPOSED
    ]

    assert collision_proposals, "Dataset should exercise amount collisions"

    results = verify_proposals(
        collision_proposals,
        dataset.payments,
        dataset.invoices,
    )

    assert len(results) == len(collision_proposals)
    assert all(not result.passed for result in results)
    assert all(
        any("reference" in issue.lower() for issue in result.issues)
        for result in results
    )

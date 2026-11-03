"""Regression tests for the four audit findings F2, F3, F4 and F7.

Each test in this module failed against the implementation as it stood at
commit d72cb21 and documents the behaviour the fix must guarantee. They are
kept together so the audit trail stays readable; the deeper unit coverage for
the new abstractions lives in test_delivery.py and the existing modules.
"""

from datetime import timedelta

import pytest

from factories import T0, inv, pay

from reconcile.baseline import BaselineMatcher
from reconcile.dataset import Dataset
from reconcile.delivery import (
    IMMUTABLE_DELIVERY_FIELDS,
    PaymentConflictError,
    resolve_delivery,
)
from reconcile.deterministic import DeterministicMatcher
from reconcile.interfaces import StaticSource
from reconcile.models import (
    AccountingError,
    AllocationStatus,
    Candidate,
    Evidence,
    EvidenceCode,
    InvoiceStatus,
    MatchOutcome,
    MatchProposal,
    Payment,
    PaymentAllocation,
    PaymentStatus,
    outstanding_cents,
)
from reconcile.normalize import canonical_reference
from reconcile.verifier import verify_proposals
from reconcile.workflow import reconcile_payments


def _delivery(payment, *, status, amount=None, reference=None, payer=None):
    """A redelivery of the same payment_id with selected fields overridden."""
    return Payment(
        payment_id=payment.payment_id,
        amount_cents=amount if amount is not None else payment.amount_cents,
        currency=payment.currency,
        received_at=payment.received_at,
        payer_name=payer if payer is not None else payment.payer_name,
        payer_email=payment.payer_email,
        reference=reference if reference is not None else payment.reference,
        note=payment.note,
        status=status,
    )


# --------------------------------------------------------------------------
# F2: PENDING -> COMPLETED payment transition
# --------------------------------------------------------------------------


def test_f2_pending_then_completed_delivery_is_not_discarded():
    """A PENDING delivery followed by COMPLETED must still reconcile.

    Before the fix the matcher proposed the COMPLETED delivery correctly, but
    the verifier saw two records sharing one payment_id and rejected the
    proposal, silently discarding a real completed payment.
    """
    invoice = inv(1, 50000)
    completed = pay(1, 50000, reference="INV-1001")
    pending = _delivery(completed, status=PaymentStatus.PENDING)

    result = reconcile_payments(
        [pending, completed],
        StaticSource((invoice,), (pending, completed)),
        matcher=DeterministicMatcher(),
    )

    assert len(result.passed_verification) == 1
    assert result.passed_verification[0].payment_id == completed.payment_id


def test_f2_verifier_accepts_pending_then_completed_delivery_stream():
    invoice = inv(1, 50000)
    completed = pay(1, 50000, reference="INV-1001")
    pending = _delivery(completed, status=PaymentStatus.PENDING)

    proposals = DeterministicMatcher().match([pending, completed], [invoice])
    proposed = [p for p in proposals if p.outcome is MatchOutcome.PROPOSED]
    assert len(proposed) == 1

    results = verify_proposals(proposed, [pending, completed], [invoice])
    assert [r.passed for r in results] == [True], results[0].issues


def test_f2_completed_delivery_wins_regardless_of_replay_order():
    """Replay order must not change the canonical outcome."""
    invoice = inv(1, 50000)
    completed = pay(1, 50000, reference="INV-1001")
    pending = _delivery(completed, status=PaymentStatus.PENDING)

    for stream in ([pending, completed], [completed, pending]):
        proposals = DeterministicMatcher().match(stream, [invoice])
        proposed = [p for p in proposals if p.outcome is MatchOutcome.PROPOSED]
        assert len(proposed) == 1, stream
        results = verify_proposals(proposed, stream, [invoice])
        assert [r.passed for r in results] == [True], (stream, results[0].issues)


def test_f2_identical_redelivery_stays_idempotent():
    """Two identical COMPLETED deliveries must yield exactly one proposal."""
    invoice = inv(1, 50000)
    first = pay(1, 50000, reference="INV-1001")
    second = _delivery(first, status=PaymentStatus.COMPLETED)

    proposals = DeterministicMatcher().match([first, second], [invoice])
    proposed = [p for p in proposals if p.outcome is MatchOutcome.PROPOSED]

    assert len(proposed) == 1
    assert proposals[1].outcome is MatchOutcome.NO_MATCH
    assert any(e.code is EvidenceCode.DUPLICATE_EVENT for e in proposals[1].reasons)

    results = verify_proposals(proposed, [first, second], [invoice])
    assert [r.passed for r in results] == [True], results[0].issues


def test_f2_conflicting_amount_across_deliveries_fails_closed():
    """A COMPLETED delivery that contradicts the PENDING record must not match."""
    invoice = inv(1, 50000)
    pending = pay(1, 50000, reference="INV-1001", status=PaymentStatus.PENDING)
    completed = _delivery(pending, status=PaymentStatus.COMPLETED, amount=70000)

    proposals = DeterministicMatcher().match([pending, completed], [invoice])
    assert all(p.outcome is MatchOutcome.NO_MATCH for p in proposals), proposals


def test_f2_conflicting_reference_across_deliveries_fails_closed():
    invoice = inv(1, 50000)
    pending = pay(1, 50000, reference="INV-1001", status=PaymentStatus.PENDING)
    completed = _delivery(pending, status=PaymentStatus.COMPLETED, reference="INV-9999")

    proposals = DeterministicMatcher().match([pending, completed], [invoice])
    assert all(p.outcome is MatchOutcome.NO_MATCH for p in proposals), proposals


def test_f2_conflicting_deliveries_are_rejected_by_the_verifier():
    """The verifier is an independent gate on contradictory deliveries.

    The COMPLETED delivery on its own is perfectly matchable, so the matcher
    proposes it. Only the full delivery stream shows the contradiction, and the
    verifier must catch it there.
    """
    invoice = inv(1, 50000)
    pending = pay(1, 50000, reference="INV-1001", status=PaymentStatus.PENDING)
    completed = _delivery(pending, status=PaymentStatus.COMPLETED, payer="Someone Else")

    proposals = DeterministicMatcher().match([completed], [invoice])
    proposed = [p for p in proposals if p.outcome is MatchOutcome.PROPOSED]
    assert len(proposed) == 1

    results = verify_proposals(proposed, [pending, completed], [invoice])
    assert results[0].passed is False
    assert any("conflict" in issue.lower() for issue in results[0].issues)


def test_f2_pending_then_denied_is_never_matched():
    invoice = inv(1, 50000)
    pending = pay(1, 50000, reference="INV-1001", status=PaymentStatus.PENDING)
    denied = _delivery(pending, status=PaymentStatus.DENIED)

    proposals = DeterministicMatcher().match([pending, denied], [invoice])
    assert all(p.outcome is MatchOutcome.NO_MATCH for p in proposals)


def test_f2_completed_then_pending_does_not_regress_the_canonical_status():
    """A late PENDING replay must not undo an already-processed COMPLETED."""
    invoice = inv(1, 50000)
    completed = pay(1, 50000, reference="INV-1001")
    pending = _delivery(completed, status=PaymentStatus.PENDING)

    proposals = DeterministicMatcher().match([completed, pending], [invoice])
    assert proposals[0].outcome is MatchOutcome.PROPOSED
    assert proposals[1].outcome is MatchOutcome.NO_MATCH
    assert any(e.code is EvidenceCode.DUPLICATE_EVENT for e in proposals[1].reasons)


def test_f2_baseline_matcher_also_survives_the_transition():
    invoice = inv(1, 50000)
    completed = pay(1, 50000, reference="INV-1001")
    pending = _delivery(completed, status=PaymentStatus.PENDING)

    proposals = BaselineMatcher().match([pending, completed], [invoice])
    proposed = [p for p in proposals if p.outcome is MatchOutcome.PROPOSED]
    assert len(proposed) == 1
    results = verify_proposals(proposed, [pending, completed], [invoice])
    assert [r.passed for r in results] == [True], results[0].issues


def test_f2_dataset_unique_payments_accepts_a_status_transition():
    """Dataset.unique_payments raised ValueError on a legal status transition."""
    invoice = inv(1, 50000)
    completed = pay(1, 50000, reference="INV-1001")
    pending = _delivery(completed, status=PaymentStatus.PENDING)

    dataset = Dataset(seed=0, invoices=(invoice,), payments=(pending, completed), truth={})
    unique = dataset.unique_payments

    assert len(unique) == 1
    assert unique[0].status is PaymentStatus.COMPLETED


def test_f2_dataset_unique_payments_still_fails_closed_on_conflicts():
    invoice = inv(1, 50000)
    pending = pay(1, 50000, reference="INV-1001", status=PaymentStatus.PENDING)
    completed = _delivery(pending, status=PaymentStatus.COMPLETED, amount=70000)

    dataset = Dataset(seed=0, invoices=(invoice,), payments=(pending, completed), truth={})
    with pytest.raises(ValueError):
        dataset.unique_payments


# --------------------------------------------------------------------------
# F3: invoice reference extraction / digit-run merging
# --------------------------------------------------------------------------


def test_f3_canonical_reference_keeps_digit_runs_apart():
    """'INV 10.01' must never canonicalise to the key of 'INV-1001'."""
    assert canonical_reference("INV 10.01") != canonical_reference("INV-1001")
    assert canonical_reference("INV 10-01") != canonical_reference("INV-1001")
    assert canonical_reference("10.01") != canonical_reference("1001")


def test_f3_baseline_does_not_match_an_amount_shaped_reference():
    """End-to-end: 'Paid INV 10.01 USD' must not resolve to invoice INV-1001."""
    invoice = inv(1, 50000)  # INV-1001
    payment = pay(1, 50000, reference="Paid INV 10.01 USD")

    proposal = BaselineMatcher().match([payment], [invoice])[0]

    assert proposal.outcome is MatchOutcome.NO_MATCH
    assert any(
        e.code is EvidenceCode.REFERENCE_UNKNOWN for e in proposal.reasons
    ), proposal.reasons


def test_f3_separator_inside_a_digit_run_is_not_a_match():
    invoice = inv(1, 50000)  # INV-1001
    for reference in ("INV 10.01", "INV-10.01", "INV 10 01", "INV/10/01", "INV.1.001"):
        proposal = BaselineMatcher().match([pay(1, 50000, reference=reference)], [invoice])[0]
        assert proposal.outcome is MatchOutcome.NO_MATCH, reference


def test_f3_valid_reference_forms_still_match():
    invoice = inv(1, 50000)  # INV-1001
    for reference in ("INV-1001", "inv-1001", "INV 1001", "inv 1001", "INV1001", "INV_1001", " INV-1001 "):
        proposal = BaselineMatcher().match([pay(1, 50000, reference=reference)], [invoice])[0]
        assert proposal.outcome is MatchOutcome.PROPOSED, reference


def test_f3_multi_digit_run_invoice_numbers_still_match_themselves():
    """An invoice genuinely numbered with two digit runs keeps matching."""
    invoice = inv(1, 50000, number="INV-2026-01")
    for reference in ("INV-2026-01", "inv 2026 01", "INV-2026.01"):
        proposal = BaselineMatcher().match([pay(1, 50000, reference=reference)], [invoice])[0]
        assert proposal.outcome is MatchOutcome.PROPOSED, reference

    # ...and a merged digit run is not the same invoice.
    merged = BaselineMatcher().match([pay(1, 50000, reference="INV-202601")], [invoice])[0]
    assert merged.outcome is MatchOutcome.NO_MATCH


def test_f3_deterministic_matcher_rejects_amount_shaped_note_reference():
    invoice = inv(1, 50000)  # INV-1001
    payment = pay(1, 50000, note="wire for inv 10.01 thanks")

    proposal = DeterministicMatcher().match([payment], [invoice])[0]
    assert proposal.outcome is MatchOutcome.NO_MATCH


# --------------------------------------------------------------------------
# F4: currency-less legacy allocations
# --------------------------------------------------------------------------


def test_f4_currency_less_approved_allocation_is_rejected():
    """A currency-less APPROVED allocation must not silently reduce a balance."""
    invoice = inv(1, 5000)
    legacy = PaymentAllocation(
        "pay_1", invoice.invoice_id, 2000, AllocationStatus.APPROVED, currency=None
    )

    with pytest.raises(AccountingError) as excinfo:
        outstanding_cents(invoice, [legacy])

    assert "currency" in str(excinfo.value).lower()


def test_f4_currency_less_allocation_blocks_the_workflow():
    invoice = inv(1, 5000)
    legacy = PaymentAllocation(
        "pay_old", invoice.invoice_id, 2000, AllocationStatus.APPROVED, currency=None
    )

    with pytest.raises(AccountingError):
        reconcile_payments(
            [pay(1, 3000, reference="INV-1001")],
            StaticSource((invoice,), ()),
            matcher=DeterministicMatcher(),
            existing_allocations=[legacy],
        )


def test_f4_explicit_migration_stamps_the_invoice_currency():
    from reconcile.models import migrate_allocation_currency

    invoice = inv(1, 5000)
    legacy = PaymentAllocation(
        "pay_1", invoice.invoice_id, 2000, AllocationStatus.APPROVED, currency=None
    )

    migrated = migrate_allocation_currency([legacy], [invoice])

    assert [a.currency for a in migrated] == ["USD"]
    assert outstanding_cents(invoice, migrated) == 3000


def test_f4_migration_refuses_to_invent_a_currency():
    from reconcile.models import migrate_allocation_currency

    invoice = inv(1, 5000)
    orphan = PaymentAllocation(
        "pay_1", "inv_unknown", 2000, AllocationStatus.APPROVED, currency=None
    )

    with pytest.raises(AccountingError):
        migrate_allocation_currency([orphan], [invoice])


def test_f4_migration_leaves_known_currencies_untouched():
    from reconcile.models import migrate_allocation_currency

    invoice = inv(1, 5000)
    tagged = PaymentAllocation(
        "pay_1", invoice.invoice_id, 2000, AllocationStatus.APPROVED, currency="USD"
    )

    assert migrate_allocation_currency([tagged], [invoice]) == [tagged]


def test_f4_migration_does_not_mask_a_real_currency_conflict():
    from reconcile.models import migrate_allocation_currency

    invoice = inv(1, 5000)  # USD
    wrong = PaymentAllocation(
        "pay_1", invoice.invoice_id, 2000, AllocationStatus.APPROVED, currency="EUR"
    )

    with pytest.raises(AccountingError):
        migrate_allocation_currency([wrong], [invoice])


# --------------------------------------------------------------------------
# F7: private baseline imports
# --------------------------------------------------------------------------


def test_f7_no_module_imports_a_private_name_from_a_sibling_module():
    import ast
    import pathlib

    package = pathlib.Path(__file__).resolve().parents[1] / "src" / "reconcile"
    offenders: list[str] = []

    for path in sorted(package.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level:
                for alias in node.names:
                    if alias.name.startswith("_"):
                        offenders.append(
                            f"{path.name} imports {alias.name!r} from "
                            f"{'.' * node.level}{node.module or ''}"
                        )

    assert offenders == [], offenders


def test_f7_public_match_run_abstraction_exists():
    import reconcile
    from reconcile.matching import MatchRun

    assert reconcile.MatchRun is MatchRun


def test_f7_baseline_exposes_a_public_no_match_helper():
    assert hasattr(BaselineMatcher, "no_match")
    assert not hasattr(BaselineMatcher, "_no_match")


def test_f7_match_run_blockers_support_partial_payments_publicly():
    from reconcile.matching import MatchRun

    invoice = inv(1, 50000)
    run = MatchRun([invoice], ())
    partial = pay(1, 20000, reference="INV-1001")

    assert run.blockers(partial, invoice, allow_partial=True) == []
    assert any(
        e.code is EvidenceCode.AMOUNT_MISMATCH
        for e in run.blockers(partial, invoice)
    )


def test_f7_matcher_behaviour_is_unchanged_on_the_dataset():
    """Deterministic matcher output must be identical to the recorded baseline."""
    from reconcile.dataset import DEFAULT_SEED, generate_dataset
    from reconcile.scoring import score

    dataset = generate_dataset(DEFAULT_SEED)
    card = score(
        "deterministic-v1",
        DeterministicMatcher().match(dataset.payments, dataset.invoices),
        dataset,
    )

    assert (card.proposed, card.correct_auto, card.false_auto) == (54, 54, 0)


# --------------------------------------------------------------------------
# F2 follow-up: received_at must not block a legal transition (audit issue 1)
# --------------------------------------------------------------------------


def _at(payment, minutes, *, status):
    """The same delivery observed at a different moment, with a given status."""
    return Payment(
        payment_id=payment.payment_id,
        amount_cents=payment.amount_cents,
        currency=payment.currency,
        received_at=T0 + timedelta(minutes=minutes),
        payer_name=payment.payer_name,
        payer_email=payment.payer_email,
        reference=payment.reference,
        note=payment.note,
        status=status,
    )


DIFFERENT_TIMESTAMP_CASES = [
    (0, 5),      # PENDING first, COMPLETED five minutes later
    (0, 1440),   # a full day later
    (7, 9),      # neither at the epoch boundary
]


@pytest.mark.parametrize("pending_min,completed_min", DIFFERENT_TIMESTAMP_CASES)
@pytest.mark.parametrize("order", ["pending_first", "completed_first"])
def test_f2_received_at_never_blocks_a_pending_to_completed_transition(
    pending_min, completed_min, order
):
    """Differing observation times are not a contradiction, in either order."""
    base = pay(1, 50000, reference="INV-1001")
    pending = _at(base, pending_min, status=PaymentStatus.PENDING)
    completed = _at(base, completed_min, status=PaymentStatus.COMPLETED)

    stream = [pending, completed] if order == "pending_first" else [completed, pending]

    canonical = resolve_delivery(stream)
    assert canonical.status is PaymentStatus.COMPLETED
    assert canonical.received_at == T0 + timedelta(minutes=completed_min)

    proposals = DeterministicMatcher().match(stream, [inv(1, 50000)])
    proposed = [p for p in proposals if p.outcome is MatchOutcome.PROPOSED]
    assert len(proposed) == 1

    results = verify_proposals(proposed, stream, [inv(1, 50000)])
    assert [r.passed for r in results] == [True], results[0].issues


@pytest.mark.parametrize("pending_min,completed_min", DIFFERENT_TIMESTAMP_CASES)
def test_f2_canonical_record_is_identical_under_both_orderings(
    pending_min, completed_min
):
    base = pay(1, 50000, reference="INV-1001")
    pending = _at(base, pending_min, status=PaymentStatus.PENDING)
    completed = _at(base, completed_min, status=PaymentStatus.COMPLETED)

    assert resolve_delivery([pending, completed]) == resolve_delivery(
        [completed, pending]
    )


def test_f2_received_at_is_not_an_immutable_delivery_field():
    """Pin the decision: observation time is resolved, not compared."""
    assert "received_at" not in IMMUTABLE_DELIVERY_FIELDS
    assert "status" not in IMMUTABLE_DELIVERY_FIELDS
    assert set(IMMUTABLE_DELIVERY_FIELDS) == {
        "amount_cents",
        "currency",
        "payer_name",
        "payer_email",
        "reference",
        "note",
    }


def test_f2_canonical_timestamp_is_the_moment_completion_was_observed():
    base = pay(1, 50000, reference="INV-1001")
    stream = [
        _at(base, 0, status=PaymentStatus.PENDING),
        _at(base, 3, status=PaymentStatus.PENDING),
        _at(base, 11, status=PaymentStatus.COMPLETED),
        _at(base, 20, status=PaymentStatus.COMPLETED),
    ]

    canonical = resolve_delivery(stream)
    assert canonical.status is PaymentStatus.COMPLETED
    # Earliest COMPLETED observation, not the earliest or latest delivery.
    assert canonical.received_at == T0 + timedelta(minutes=11)


def test_f2_stale_pending_arriving_later_in_time_does_not_downgrade():
    """A PENDING replay stamped AFTER the COMPLETED one must not win."""
    base = pay(1, 50000, reference="INV-1001")
    completed = _at(base, 5, status=PaymentStatus.COMPLETED)
    stale_pending = _at(base, 60, status=PaymentStatus.PENDING)

    for stream in ([completed, stale_pending], [stale_pending, completed]):
        canonical = resolve_delivery(stream)
        assert canonical.status is PaymentStatus.COMPLETED, stream
        assert canonical.received_at == T0 + timedelta(minutes=5)

        results = verify_proposals(
            [p for p in DeterministicMatcher().match(stream, [inv(1, 50000)])
             if p.outcome is MatchOutcome.PROPOSED],
            stream,
            [inv(1, 50000)],
        )
        assert [r.passed for r in results] == [True], (stream, results[0].issues)


def test_f2_differing_timestamps_do_not_excuse_a_financial_conflict():
    """received_at being free must not leak into the money fields."""
    base = pay(1, 50000, reference="INV-1001")
    pending = _at(base, 0, status=PaymentStatus.PENDING)
    completed = Payment(
        payment_id=base.payment_id,
        amount_cents=70000,
        currency=base.currency,
        received_at=T0 + timedelta(minutes=5),
        payer_name=base.payer_name,
        reference=base.reference,
        status=PaymentStatus.COMPLETED,
    )

    with pytest.raises(PaymentConflictError):
        resolve_delivery([pending, completed])
    with pytest.raises(PaymentConflictError):
        resolve_delivery([completed, pending])


# --------------------------------------------------------------------------
# F2 follow-up: the verifier itself must canonicalize (audit issue 2)
# --------------------------------------------------------------------------


def _ref_proposal(payment, invoice, amount=None):
    """A reference-backed PROPOSED proposal, built without any matcher."""
    return MatchProposal(
        payment_id=payment.payment_id,
        outcome=MatchOutcome.PROPOSED,
        candidates=(
            Candidate(
                (
                    PaymentAllocation(
                        payment.payment_id,
                        invoice.invoice_id,
                        amount if amount is not None else payment.amount_cents,
                        currency=invoice.currency,
                    ),
                ),
                (
                    Evidence(
                        EvidenceCode.REFERENCE_MATCH,
                        f"reference = invoice {invoice.number}",
                    ),
                    Evidence(EvidenceCode.AMOUNT_EXACT, "exact"),
                ),
            ),
        ),
        matcher="test",
    )


def test_f2_verifier_canonicalizes_a_pending_then_completed_stream():
    """Proven through verify_proposals, with no matcher involved."""
    invoice = inv(1, 50000)
    base = pay(1, 50000, reference="INV-1001")
    stream = [
        _at(base, 0, status=PaymentStatus.PENDING),
        _at(base, 5, status=PaymentStatus.COMPLETED),
    ]

    result = verify_proposals([_ref_proposal(base, invoice)], stream, [invoice])[0]
    assert result.passed, result.issues


def test_f2_verifier_does_not_falsely_reject_identical_redeliveries():
    invoice = inv(1, 50000)
    base = pay(1, 50000, reference="INV-1001")
    stream = [base, _at(base, 0, status=PaymentStatus.COMPLETED)]

    result = verify_proposals([_ref_proposal(base, invoice)], stream, [invoice])[0]
    assert result.passed, result.issues


def test_f2_verifier_rejects_a_proposal_whose_deliveries_conflict():
    invoice = inv(1, 50000)
    base = pay(1, 50000, reference="INV-1001")
    conflicting = Payment(
        payment_id=base.payment_id,
        amount_cents=base.amount_cents,
        currency=base.currency,
        received_at=base.received_at,
        payer_name="Someone Else",
        reference=base.reference,
        status=PaymentStatus.COMPLETED,
    )

    result = verify_proposals(
        [_ref_proposal(base, invoice)], [base, conflicting], [invoice]
    )[0]
    assert not result.passed
    assert any("conflicting deliveries" in i for i in result.issues)


def test_f2_verifier_rejects_a_fabricated_payment_record():
    """A proposal for a payment_id that was never delivered cannot pass."""
    invoice = inv(1, 50000)
    delivered = pay(1, 50000, reference="INV-1001")
    ghost = pay(2, 50000, reference="INV-1001")

    result = verify_proposals([_ref_proposal(ghost, invoice)], [delivered], [invoice])[0]
    assert not result.passed
    assert any("payment record is missing" in i for i in result.issues)


def test_f2_verifier_uses_the_canonical_status_not_the_first_delivery():
    """A stream that never completes must not pass on its PENDING record."""
    invoice = inv(1, 50000)
    base = pay(1, 50000, reference="INV-1001")
    stream = [
        _at(base, 0, status=PaymentStatus.PENDING),
        _at(base, 5, status=PaymentStatus.PENDING),
    ]

    result = verify_proposals([_ref_proposal(base, invoice)], stream, [invoice])[0]
    assert not result.passed
    assert any("COMPLETED" in i for i in result.issues)


def test_f2_verifier_rejects_a_denied_canonical_status():
    invoice = inv(1, 50000)
    base = pay(1, 50000, reference="INV-1001")
    stream = [
        _at(base, 0, status=PaymentStatus.PENDING),
        _at(base, 5, status=PaymentStatus.DENIED),
    ]

    result = verify_proposals([_ref_proposal(base, invoice)], stream, [invoice])[0]
    assert not result.passed
    assert any("COMPLETED" in i for i in result.issues)


def test_f2_verifier_still_rejects_duplicate_proposals_after_canonicalization():
    invoice = inv(1, 50000)
    base = pay(1, 50000, reference="INV-1001")
    stream = [
        _at(base, 0, status=PaymentStatus.PENDING),
        _at(base, 5, status=PaymentStatus.COMPLETED),
    ]
    twice = [_ref_proposal(base, invoice), _ref_proposal(base, invoice)]

    results = verify_proposals(twice, stream, [invoice])
    assert all(not r.passed for r in results)
    assert all(
        any("duplicate proposals" in i for i in r.issues) for r in results
    )


def test_f2_canonicalization_does_not_weaken_the_financial_invariants():
    """Every independent check still fires on a canonicalized payment."""
    base = pay(1, 50000, reference="INV-1001")
    stream = [
        _at(base, 0, status=PaymentStatus.PENDING),
        _at(base, 5, status=PaymentStatus.COMPLETED),
    ]

    # Allocation total must equal the canonical payment amount.
    invoice = inv(1, 50000)
    short = verify_proposals(
        [_ref_proposal(base, invoice, amount=40000)], stream, [invoice]
    )[0]
    assert not short.passed
    assert any("does not equal payment amount" in i for i in short.issues)

    # The invoice must be open.
    closed = inv(1, 50000, status=InvoiceStatus.PAID)
    assert not verify_proposals([_ref_proposal(base, closed)], stream, [closed])[0].passed

    # Currency must agree.
    eur = inv(1, 50000, currency="EUR")
    eur_result = verify_proposals([_ref_proposal(base, eur)], stream, [eur])[0]
    assert not eur_result.passed
    assert any("currency mismatch" in i for i in eur_result.issues)

    # The payment must not already be allocated in the ledger.
    settled = PaymentAllocation(
        base.payment_id, invoice.invoice_id, 50000,
        AllocationStatus.APPROVED, currency="USD",
    )
    already = verify_proposals(
        [_ref_proposal(base, invoice)], stream, [invoice], existing_allocations=[settled]
    )[0]
    assert not already.passed
    assert any("already allocated" in i for i in already.issues)


def test_f2_verifier_requires_reference_evidence_even_after_canonicalization():
    """An amount-only proposal must not pass just because the stream resolved."""
    invoice = inv(1, 50000)
    base = pay(1, 50000, reference="INV-1001")
    stream = [
        _at(base, 0, status=PaymentStatus.PENDING),
        _at(base, 5, status=PaymentStatus.COMPLETED),
    ]
    amount_only = MatchProposal(
        payment_id=base.payment_id,
        outcome=MatchOutcome.PROPOSED,
        candidates=(
            Candidate(
                (
                    PaymentAllocation(
                        base.payment_id, invoice.invoice_id, 50000, currency="USD"
                    ),
                ),
                (Evidence(EvidenceCode.AMOUNT_EXACT, "exact"),),
            ),
        ),
        matcher="test",
    )

    result = verify_proposals([amount_only], stream, [invoice])[0]
    assert not result.passed
    assert any("trusted invoice reference" in i for i in result.issues)

"""Unit coverage for canonical resolution of the payment delivery stream."""

from datetime import timedelta

import pytest

from factories import T0, pay

from reconcile.delivery import (
    IMMUTABLE_DELIVERY_FIELDS,
    PAYMENT_STATUS_TRANSITIONS,
    PaymentConflictError,
    advances_to,
    canonical_payments,
    resolve_delivery,
    resolve_delivery_stream,
    resolve_payment_status,
)
from reconcile.models import Payment, PaymentStatus as S


def delivery(status, *, minutes=0, amount=50000, reference="INV-1001", payer="Acme Studio",
             email=None, note="", payment_id="pay_1", currency="USD"):
    return Payment(
        payment_id=payment_id,
        amount_cents=amount,
        currency=currency,
        received_at=T0 + timedelta(minutes=minutes),
        payer_name=payer,
        payer_email=email,
        reference=reference,
        note=note,
        status=status,
    )


# --------------------------------------------------------------------------
# Status transitions
# --------------------------------------------------------------------------


def test_every_status_has_a_transition_entry():
    assert set(PAYMENT_STATUS_TRANSITIONS) == set(S)


def test_terminal_statuses_have_no_successors():
    for status in (S.REFUNDED, S.REVERSED, S.DENIED):
        assert PAYMENT_STATUS_TRANSITIONS[status] == frozenset()


def test_advances_to_is_reflexive_and_transitive():
    assert advances_to(S.PENDING, S.PENDING)
    assert advances_to(S.PENDING, S.COMPLETED)
    # PENDING -> COMPLETED -> REFUNDED
    assert advances_to(S.PENDING, S.REFUNDED)
    assert not advances_to(S.COMPLETED, S.PENDING)
    assert not advances_to(S.DENIED, S.COMPLETED)


@pytest.mark.parametrize(
    "first,second,expected",
    [
        (S.PENDING, S.COMPLETED, S.COMPLETED),
        (S.COMPLETED, S.PENDING, S.COMPLETED),
        (S.PENDING, S.PENDING, S.PENDING),
        (S.PENDING, S.DENIED, S.DENIED),
        (S.COMPLETED, S.REFUNDED, S.REFUNDED),
        (S.COMPLETED, S.PARTIALLY_REFUNDED, S.PARTIALLY_REFUNDED),
        (S.PARTIALLY_REFUNDED, S.COMPLETED, S.PARTIALLY_REFUNDED),
        (S.PARTIALLY_REFUNDED, S.REFUNDED, S.REFUNDED),
    ],
)
def test_resolve_payment_status_picks_the_more_advanced(first, second, expected):
    assert resolve_payment_status(first, second) is expected


@pytest.mark.parametrize(
    "first,second",
    [
        (S.COMPLETED, S.DENIED),
        (S.DENIED, S.REFUNDED),
        (S.REFUNDED, S.REVERSED),
    ],
)
def test_incomparable_statuses_are_a_conflict(first, second):
    """Two outcomes that cannot both be true must fail closed, not pick one."""
    with pytest.raises(PaymentConflictError):
        resolve_payment_status(first, second)


# --------------------------------------------------------------------------
# resolve_delivery
# --------------------------------------------------------------------------


def test_single_delivery_is_returned_unchanged():
    one = delivery(S.COMPLETED)
    assert resolve_delivery([one]) is one


def test_pending_then_completed_resolves_to_completed():
    canonical = resolve_delivery([delivery(S.PENDING), delivery(S.COMPLETED, minutes=5)])
    assert canonical.status is S.COMPLETED
    assert canonical.received_at == T0 + timedelta(minutes=5)


def test_resolution_is_independent_of_delivery_order():
    pending = delivery(S.PENDING)
    completed = delivery(S.COMPLETED, minutes=5)
    assert resolve_delivery([pending, completed]) == resolve_delivery([completed, pending])


def test_identical_retries_collapse_to_one_record():
    one = delivery(S.COMPLETED)
    two = delivery(S.COMPLETED)
    assert resolve_delivery([one, two]) == one


def test_earliest_observation_wins_among_equal_statuses():
    late = delivery(S.COMPLETED, minutes=9)
    early = delivery(S.COMPLETED, minutes=1)
    assert resolve_delivery([late, early]).received_at == T0 + timedelta(minutes=1)


def test_long_progression_resolves_to_the_final_status():
    canonical = resolve_delivery(
        [
            delivery(S.PENDING),
            delivery(S.COMPLETED, minutes=1),
            delivery(S.PARTIALLY_REFUNDED, minutes=2),
            delivery(S.REFUNDED, minutes=3),
        ]
    )
    assert canonical.status is S.REFUNDED


CONFLICTING_OVERRIDE = {
    "amount_cents": {"amount": 70000},
    "currency": {"currency": "EUR"},
    "payer_name": {"payer": "Someone Else"},
    "payer_email": {"email": "other@example.com"},
    "reference": {"reference": "INV-9999"},
    "note": {"note": "different note"},
}


def test_every_immutable_field_is_covered_by_a_conflict_case():
    assert set(CONFLICTING_OVERRIDE) == set(IMMUTABLE_DELIVERY_FIELDS)


@pytest.mark.parametrize("field", IMMUTABLE_DELIVERY_FIELDS)
def test_any_changed_money_or_evidence_field_is_a_conflict(field):
    override = CONFLICTING_OVERRIDE[field]

    with pytest.raises(PaymentConflictError) as excinfo:
        resolve_delivery([delivery(S.PENDING), delivery(S.COMPLETED, minutes=5, **override)])

    assert field in str(excinfo.value)


def test_received_at_alone_is_never_a_conflict():
    """Observation time differs on every retry; that is not a contradiction."""
    canonical = resolve_delivery(
        [delivery(S.COMPLETED, minutes=0), delivery(S.COMPLETED, minutes=30)]
    )
    assert canonical.status is S.COMPLETED


def test_resolve_delivery_rejects_mixed_payment_ids():
    with pytest.raises(ValueError):
        resolve_delivery([delivery(S.COMPLETED), delivery(S.COMPLETED, payment_id="pay_2")])


def test_resolve_delivery_rejects_an_empty_stream():
    with pytest.raises(ValueError):
        resolve_delivery([])


# --------------------------------------------------------------------------
# Stream level
# --------------------------------------------------------------------------


def test_stream_resolution_separates_clean_and_conflicting_payments():
    clean = delivery(S.COMPLETED, payment_id="pay_ok")
    a = delivery(S.PENDING, payment_id="pay_bad")
    b = delivery(S.COMPLETED, payment_id="pay_bad", minutes=5, amount=99999)

    resolution = resolve_delivery_stream([clean, a, b])

    assert set(resolution.canonical) == {"pay_ok"}
    assert set(resolution.conflicts) == {"pay_bad"}
    assert resolution.get("pay_ok") == clean
    assert resolution.get("pay_bad") is None
    assert "amount_cents" in resolution.conflict_reason("pay_bad")
    assert resolution.conflict_reason("pay_ok") is None


def test_canonical_payments_keeps_first_delivery_order():
    first = delivery(S.COMPLETED, payment_id="pay_a")
    second = delivery(S.COMPLETED, payment_id="pay_b", minutes=1)
    retry = delivery(S.COMPLETED, payment_id="pay_a", minutes=2)

    assert [p.payment_id for p in canonical_payments([first, second, retry])] == [
        "pay_a",
        "pay_b",
    ]


def test_canonical_payments_raises_on_conflict():
    with pytest.raises(PaymentConflictError):
        canonical_payments(
            [delivery(S.PENDING), delivery(S.COMPLETED, minutes=5, amount=1)]
        )


def test_canonical_payments_on_an_empty_stream():
    assert canonical_payments([]) == ()


def test_canonical_payments_leaves_a_clean_stream_alone():
    stream = [pay(1, 1000), pay(2, 2000, minutes=1)]
    assert list(canonical_payments(stream)) == stream

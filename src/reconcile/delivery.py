"""Canonical resolution of the payment delivery stream.

PayPal delivers the same payment more than once. A webhook retry repeats an
identical record, and a real status progression (PENDING then COMPLETED)
repeats the payment_id carrying a new status. Everything downstream of
ingestion wants exactly one canonical record per payment_id, so this module is
the single place that decides what that record is.

Two rules, both deliberately strict:

* Status may only move forward along transitions PayPal can actually report.
  A delivery that moves backwards is a stale replay and is ignored; two
  statuses that cannot reach one another (COMPLETED and DENIED, say) are a
  contradiction, not a progression.
* Every field that identifies the money or the matching evidence is
  immutable. A redelivery that changes an amount, a currency, a reference or
  the payer is not a retry, it is a contradiction. We fail closed rather than
  guess which record is true.
* `received_at` is the exception. It records when we observed the payment, and
  a real progression is observed twice: PENDING at one moment, COMPLETED at a
  later one. The canonical record carries the moment the payment reached its
  canonical status, chosen so that replay order cannot change it.

Nothing here approves anything or touches a balance.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping, Optional, Sequence

from .models import Payment, PaymentStatus


class PaymentConflictError(ValueError):
    """Raised when deliveries sharing a payment_id contradict one another."""


# Forward transitions PayPal can report for a single payment. Anything absent
# here is either a stale replay (the reverse of a listed edge) or a conflict.
PAYMENT_STATUS_TRANSITIONS: Mapping[PaymentStatus, frozenset[PaymentStatus]] = {
    PaymentStatus.PENDING: frozenset(
        {PaymentStatus.COMPLETED, PaymentStatus.DENIED}
    ),
    PaymentStatus.COMPLETED: frozenset(
        {
            PaymentStatus.PARTIALLY_REFUNDED,
            PaymentStatus.REFUNDED,
            PaymentStatus.REVERSED,
        }
    ),
    PaymentStatus.PARTIALLY_REFUNDED: frozenset(
        {PaymentStatus.REFUNDED, PaymentStatus.REVERSED}
    ),
    PaymentStatus.REFUNDED: frozenset(),
    PaymentStatus.REVERSED: frozenset(),
    PaymentStatus.DENIED: frozenset(),
}

# Fields that identify the money and the matching evidence. A redelivery may
# never change any of them. Two fields are excluded on purpose: `status`, which
# a redelivery is allowed to advance, and `received_at`, which is when we
# observed the delivery rather than a fact about the payment.
IMMUTABLE_DELIVERY_FIELDS: tuple[str, ...] = (
    "amount_cents",
    "currency",
    "payer_name",
    "payer_email",
    "reference",
    "note",
)


def _reachable_from(start: PaymentStatus) -> frozenset[PaymentStatus]:
    seen: set[PaymentStatus] = set()
    frontier = [start]
    while frontier:
        current = frontier.pop()
        for nxt in PAYMENT_STATUS_TRANSITIONS[current]:
            if nxt not in seen:
                seen.add(nxt)
                frontier.append(nxt)
    return frozenset(seen)


_REACHABLE: Mapping[PaymentStatus, frozenset[PaymentStatus]] = {
    status: _reachable_from(status) for status in PaymentStatus
}


def advances_to(current: PaymentStatus, later: PaymentStatus) -> bool:
    """True when `later` is `current` itself or a status reachable from it."""
    return later is current or later in _REACHABLE[current]


def resolve_payment_status(
    first: PaymentStatus, second: PaymentStatus
) -> PaymentStatus:
    """Return the more advanced of two statuses for the same payment.

    Raises PaymentConflictError when neither status can reach the other, which
    means the two deliveries describe outcomes that cannot both be true.
    """
    if advances_to(first, second):
        return second
    if advances_to(second, first):
        return first
    raise PaymentConflictError(
        f"payment status {first.value} and {second.value} are not a valid "
        f"transition in either direction"
    )


def _assert_same_payment_facts(first: Payment, second: Payment) -> None:
    for field in IMMUTABLE_DELIVERY_FIELDS:
        left = getattr(first, field)
        right = getattr(second, field)
        if left != right:
            raise PaymentConflictError(
                f"conflicting deliveries for payment ID {first.payment_id!r}: "
                f"{field} changed from {left!r} to {right!r}"
            )


def resolve_delivery(deliveries: Sequence[Payment]) -> Payment:
    """Collapse every delivery of ONE payment_id into one canonical record.

    The canonical record is the delivery that carries the most advanced status,
    taking the earliest `received_at` among those so that replay order cannot
    change the result. Raises PaymentConflictError on contradictory deliveries,
    and ValueError if the deliveries do not share a payment_id.
    """
    if not deliveries:
        raise ValueError("resolve_delivery needs at least one delivery")

    if len({d.payment_id for d in deliveries}) != 1:
        raise ValueError("resolve_delivery expects deliveries of one payment_id")

    first = deliveries[0]
    status = first.status

    for delivery in deliveries[1:]:
        _assert_same_payment_facts(first, delivery)
        status = resolve_payment_status(status, delivery.status)

    # `status` is always one of the delivered statuses, because
    # resolve_payment_status only ever returns one of the two it was given. The
    # earliest observation at that status is the moment the payment reached it,
    # and min() makes the choice independent of delivery order.
    at_status = [d for d in deliveries if d.status is status]
    return min(at_status, key=lambda d: d.received_at)


@dataclass(frozen=True)
class DeliveryResolution:
    """One canonical record per payment_id, plus the ones that failed closed.

    `conflicts` maps a payment_id to why its deliveries contradict each other.
    A payment_id appears in exactly one of the two mappings.
    """

    canonical: Mapping[str, Payment]
    conflicts: Mapping[str, str]

    def get(self, payment_id: str) -> Optional[Payment]:
        return self.canonical.get(payment_id)

    def conflict_reason(self, payment_id: str) -> Optional[str]:
        return self.conflicts.get(payment_id)


def resolve_delivery_stream(deliveries: Iterable[Payment]) -> DeliveryResolution:
    """Resolve a whole delivery stream without raising on conflicts.

    Callers that must keep processing the rest of the batch (the verifier) use
    this and report each conflicting payment_id as an issue.
    """
    grouped: dict[str, list[Payment]] = {}
    for delivery in deliveries:
        grouped.setdefault(delivery.payment_id, []).append(delivery)

    canonical: dict[str, Payment] = {}
    conflicts: dict[str, str] = {}

    for payment_id, group in grouped.items():
        try:
            canonical[payment_id] = resolve_delivery(group)
        except PaymentConflictError as exc:
            conflicts[payment_id] = str(exc)

    return DeliveryResolution(canonical=canonical, conflicts=conflicts)


def canonical_payments(deliveries: Iterable[Payment]) -> tuple[Payment, ...]:
    """Canonical records in first-delivery order. Raises on any conflict."""
    resolution = resolve_delivery_stream(deliveries)
    if resolution.conflicts:
        payment_id, reason = next(iter(resolution.conflicts.items()))
        raise PaymentConflictError(
            f"conflicting deliveries for payment ID {payment_id!r}: {reason}"
        )
    return tuple(resolution.canonical.values())

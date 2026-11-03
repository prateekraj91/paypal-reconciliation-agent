"""Tamper-evident, append-only, SHA-256 hash-chained audit system.

Specification:
    hash = SHA-256(prev_hash + canonical_json(event_payload))
Genesis hash:
    64 zeros: "0" * 64
"""

from __future__ import annotations

import hashlib
from typing import Any, Protocol, Sequence

from .canonical import canonical_json

GENESIS_HASH: str = "0" * 64


class AuditIntegrityError(RuntimeError):
    """Raised when an audit chain verification fails due to tampering, corruption, or forks."""

    def __init__(self, message: str, details: Sequence[str] = ()) -> None:
        super().__init__(message)
        self.details = tuple(details)


class AuditEventLike(Protocol):
    """Protocol satisfied by AuditEvent model or lightweight audit record."""

    tenant_id: str
    sequence_number: int
    event_id: str
    event_type: str
    actor: str
    subject_type: str
    subject_id: str
    payload_json: str
    prev_hash: str
    event_hash: str


def compute_event_hash(prev_hash: str, payload_json: str) -> str:
    """Compute deterministic SHA-256 event hash over previous hash and canonical payload."""
    content = (prev_hash + payload_json).encode("utf-8")
    return hashlib.sha256(content).hexdigest()


def create_audit_payload(
    tenant_id: str,
    event_type: str,
    actor: str,
    subject_type: str,
    subject_id: str,
    details: dict[str, Any],
) -> str:
    """Construct deterministic canonical JSON string for an audit event payload."""
    payload_dict = {
        "tenant_id": tenant_id,
        "event_type": event_type,
        "actor": actor,
        "subject_type": subject_type,
        "subject_id": subject_id,
        "details": details,
    }
    return canonical_json(payload_dict)


def check_audit_chain(events: Sequence[AuditEventLike]) -> list[str]:
    """Verify hash chain continuity and payload integrity for a sequence of audit events.

    Returns a list of specific, actionable integrity errors. An empty list indicates
    a clean, untampered chain.
    """
    issues: list[str] = []
    if not events:
        return issues

    expected_prev = GENESIS_HASH
    last_seq = 0

    for idx, event in enumerate(events):
        seq = event.sequence_number
        # Check sequence continuity
        if idx == 0:
            if seq != 1:
                issues.append(
                    f"First audit event has sequence {seq}, expected sequence 1"
                )
        else:
            if seq != last_seq + 1:
                issues.append(
                    f"Sequence discontinuity: event {event.event_id} has sequence {seq}, "
                    f"expected {last_seq + 1} (gap or reordering detected)"
                )

        # Check prev_hash continuity
        if event.prev_hash != expected_prev:
            issues.append(
                f"Broken chain link at sequence {seq} (event {event.event_id}): "
                f"prev_hash {event.prev_hash!r} does not match expected {expected_prev!r}"
            )

        # Re-compute event hash over canonical payload
        computed = compute_event_hash(event.prev_hash, event.payload_json)
        if computed != event.event_hash:
            issues.append(
                f"Payload tampering detected at sequence {seq} (event {event.event_id}): "
                f"stored hash {event.event_hash!r} does not match computed hash {computed!r}"
            )

        expected_prev = event.event_hash
        last_seq = seq

    return issues


def verify_audit_chain(events: Sequence[AuditEventLike]) -> None:
    """Validate audit chain, raising AuditIntegrityError on any tampering, fork or gap."""
    issues = check_audit_chain(events)
    if issues:
        raise AuditIntegrityError(
            f"Audit chain verification failed with {len(issues)} integrity violation(s):\n"
            + "\n".join(f"  - {issue}" for issue in issues),
            details=issues,
        )

"""Deterministic canonical JSON serialization for financial audit logs and hashes.

Rules:
1. Keys are strictly sorted in lexicographical order.
2. Compact separators (',' and ':') without whitespace.
3. UTF-8 character encoding with ensure_ascii=False.
4. UTC ISO-8601 timestamp representation.
5. Integer representation for monetary minor units.
6. Absolutely NO floating-point numbers permitted anywhere in the data structure.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from enum import Enum
import hashlib
import json
from typing import Any


def _sanitize_for_canonical(obj: Any) -> Any:
    """Recursively validate and convert objects into canonical JSON-serializable structures."""
    if isinstance(obj, float):
        raise ValueError(
            f"Floating-point value {obj!r} is strictly forbidden in financial serialization"
        )
    if isinstance(obj, bool):
        return obj
    if isinstance(obj, int):
        return obj
    if isinstance(obj, str):
        return obj
    if obj is None:
        return None
    if isinstance(obj, (datetime, date)):
        if isinstance(obj, datetime):
            if obj.tzinfo is None:
                # Naive datetimes are assumed UTC
                obj = obj.replace(tzinfo=timezone.utc)
            else:
                obj = obj.astimezone(timezone.utc)
        return obj.isoformat()
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, dict):
        return {
            str(k): _sanitize_for_canonical(v)
            for k, v in sorted(obj.items(), key=lambda item: str(item[0]))
        }
    if isinstance(obj, (list, tuple)):
        return [_sanitize_for_canonical(item) for item in obj]
    if hasattr(obj, "__dict__"):
        return _sanitize_for_canonical(
            {k: v for k, v in obj.__dict__.items() if not k.startswith("_")}
        )
    raise TypeError(f"Object of type {type(obj).__name__} is not canonical JSON serializable")


def canonical_json(data: Any) -> str:
    """Produce deterministic, sort-keyed, compact JSON without floats."""
    sanitized = _sanitize_for_canonical(data)
    return json.dumps(
        sanitized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def compute_content_hash(data: Any) -> str:
    """Compute SHA-256 hex digest of canonical JSON serialization."""
    if isinstance(data, str):
        encoded = data.encode("utf-8")
    else:
        encoded = canonical_json(data).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


hash_content = compute_content_hash

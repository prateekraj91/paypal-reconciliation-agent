"""Small, pure text normalisers shared by matchers."""

from __future__ import annotations

import re
from typing import Iterable, Optional

_NON_ALNUM = re.compile(r"[^A-Za-z0-9]")


def canonical_reference(value: Optional[str]) -> str:
    """Normalize case, spaces, hyphens and punctuation in a reference."""
    if not value:
        return ""
    return _NON_ALNUM.sub("", value).upper()


def extract_invoice_references(
    texts: Iterable[Optional[str]],
    invoice_numbers: Iterable[str],
) -> tuple[str, ...]:
    """Extract known invoice numbers from text, in order of appearance.

    Only invoice numbers supplied by the caller are considered. This avoids
    treating arbitrary numbers in a payment note as invoice references.
    """
    patterns: list[tuple[str, re.Pattern[str]]] = []
    known: set[str] = set()

    for number in invoice_numbers:
        canonical = canonical_reference(number)
        if not canonical or canonical in known:
            continue

        known.add(canonical)

        # Allow punctuation or spaces between characters:
        # INV-1001, INV 1001 and INV1001 can all match.
        flexible = r"[^A-Za-z0-9]*".join(
            re.escape(char) for char in canonical
        )
        pattern = re.compile(
            rf"(?<![A-Za-z0-9]){flexible}(?![A-Za-z0-9])",
            re.IGNORECASE,
        )
        patterns.append((number, pattern))

    hits: dict[str, tuple[int, int, str]] = {}

    for text_index, text in enumerate(texts):
        if not text:
            continue

        for number, pattern in patterns:
            match = pattern.search(text)
            if match is None:
                continue

            canonical = canonical_reference(number)
            position = (text_index, match.start(), number)

            if canonical not in hits or position < hits[canonical]:
                hits[canonical] = position

    ordered = sorted(hits.values())
    return tuple(item[2] for item in ordered)

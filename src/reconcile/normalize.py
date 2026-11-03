"""Small, pure text normalisers shared by matchers."""

from __future__ import annotations

import re
from typing import Iterable, Optional

_SEPARATOR_RUN = re.compile(r"[^A-Za-z0-9]+")
_DIGITS = frozenset("0123456789")

# Stands in for a separator that sat between two digits. It is not alphanumeric,
# so it can never be produced by the rest of a reference, which keeps two
# different invoice numbers from collapsing onto the same canonical key.
_DIGIT_BOUNDARY = "."


def canonical_reference(value: Optional[str]) -> str:
    """Normalize case, spaces, hyphens and punctuation in a reference.

    Separators are dropped so that INV-1001, INV 1001, inv_1001 and INV1001 all
    canonicalise to INV1001. A separator *between two digits* is kept as a
    boundary marker instead: merging those digit runs would make the amount
    'INV 10.01' indistinguishable from invoice 'INV-1001', and an invoice
    genuinely numbered 'INV-2026-01' indistinguishable from 'INV-202601'.
    """
    if not value:
        return ""

    def replace(match: re.Match[str]) -> str:
        start, end = match.span()
        before = value[start - 1] if start > 0 else ""
        after = value[end] if end < len(value) else ""
        if before in _DIGITS and after in _DIGITS:
            return _DIGIT_BOUNDARY
        return ""

    return _SEPARATOR_RUN.sub(replace, value).upper()


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

        # Allow separators between letter and digit groups, not inside digit groups.
        # INV-1001, INV 1001 and INV1001 can all match, but not 'INV 10.01'.
        groups = re.findall(r"[A-Za-z]+|[0-9]+", number)
        flexible = r"[^A-Za-z0-9]*".join(
            re.escape(group) for group in groups
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

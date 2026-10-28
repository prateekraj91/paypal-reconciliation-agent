"""Small, pure text normalisers shared by matchers."""

from __future__ import annotations

import re
from typing import Optional

_NON_ALNUM = re.compile(r"[^A-Za-z0-9]")


def canonical_reference(value: Optional[str]) -> str:
    """Canonical form of an invoice reference.

    Case, spaces, hyphens and punctuation are ignored, so "inv-1001",
    "INV 1001" and "INV1001" are the same reference. Extra words are NOT
    stripped: "Payment for INV-1001" stays a different string. Extracting an
    invoice number from free text is a job for the later deterministic matcher.
    """
    if not value:
        return ""
    return _NON_ALNUM.sub("", value).upper()

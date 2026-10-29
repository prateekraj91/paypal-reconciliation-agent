"""Conservative deterministic matcher with free-text reference extraction."""

from __future__ import annotations

import re
from typing import Sequence

from .baseline import BaselineConfig, BaselineMatcher, _Run
from .models import (
    AllocationStatus,
    Candidate,
    Evidence,
    EvidenceCode,
    Invoice,
    MatchOutcome,
    MatchProposal,
    Payment,
    PaymentAllocation,
    PaymentStatus,
)
from .normalize import canonical_reference, extract_invoice_references

_INVOICE_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])(?:INV|INVOICE)[^A-Za-z0-9]*[0-9]+(?![A-Za-z0-9])",
    re.IGNORECASE,
)


def _normalize_ref(val: str) -> str:
    c = canonical_reference(val)
    if c.startswith("INVOICE"):
        c = "INV" + c[7:]
    return c


class DeterministicMatcher(BaselineMatcher):
    """Resolve exact, uniquely referenced invoices without amount-only guesses.

    This matcher supports references in the structured reference field
    or payment note. Partial payments are allowed when a trusted reference is
    present: the allocation equals the payment amount and must not exceed the
    outstanding balance. Grouped payments covering multiple open invoices are
    supported when all invoices are explicitly referenced and their outstanding
    balances sum to the exact payment amount.
    """

    def __init__(self) -> None:
        super().__init__(
            BaselineConfig(allow_amount_only=False),
            name="deterministic-v1",
        )

    def _match_one(self, p: Payment, run: _Run) -> MatchProposal:
        # 1. Reject duplicate deliveries and uncompleted payments.
        if p.payment_id in run.seen:
            return self._no_match(
                p,
                Evidence(
                    EvidenceCode.DUPLICATE_EVENT,
                    f"payment {p.payment_id} was already processed",
                ),
            )
        run.seen.add(p.payment_id)

        if p.status is not PaymentStatus.COMPLETED:
            return self._no_match(
                p,
                Evidence(
                    EvidenceCode.PAYMENT_NOT_COMPLETED,
                    f"payment status is {p.status.value}",
                ),
            )

        invoice_numbers = [invoice.number for invoice in run.invoices]
        known_canonicals = {_normalize_ref(num): num for num in invoice_numbers}

        ref_hits: list[str] = []
        note_hits: list[str] = []

        # 2a. Inspect structured reference field
        if p.reference and canonical_reference(p.reference):
            segments = [
                s.strip()
                for s in re.split(r"[,;/]|\band\b|&", p.reference)
                if s.strip()
            ]
            seen_in_ref: set[str] = set()
            for seg in segments:
                extracted = extract_invoice_references([seg], invoice_numbers)
                if not extracted:
                    return self._no_match(
                        p,
                        Evidence(
                            EvidenceCode.REFERENCE_UNKNOWN,
                            f"could not identify a known invoice in reference {p.reference!r}",
                        ),
                    )
                for num in extracted:
                    canon = _normalize_ref(num)
                    if canon in seen_in_ref:
                        return self._no_match(
                            p,
                            Evidence(
                                EvidenceCode.MULTIPLE_CANDIDATES,
                                f"duplicate invoice reference {num!r} found in payment reference",
                            ),
                        )
                    seen_in_ref.add(canon)
                    ref_hits.append(num)

        # 2b. Inspect payment note
        if p.note and canonical_reference(p.note):
            # Check for unknown invoice numbers matching invoice pattern in the note
            pattern_matches = list(_INVOICE_PATTERN.finditer(p.note))
            for match in pattern_matches:
                canon = _normalize_ref(match.group(0))
                if canon not in known_canonicals:
                    return self._no_match(
                        p,
                        Evidence(
                            EvidenceCode.REFERENCE_UNKNOWN,
                            f"could not identify a known invoice for {match.group(0)!r} in payment note",
                        ),
                    )

            extracted_note = extract_invoice_references([p.note], invoice_numbers)
            if pattern_matches:
                match_canons = [_normalize_ref(m.group(0)) for m in pattern_matches]
                if len(match_canons) != len(set(match_canons)):
                    return self._no_match(
                        p,
                        Evidence(
                            EvidenceCode.MULTIPLE_CANDIDATES,
                            "duplicate invoice reference found in payment note",
                        ),
                    )

            note_hits.extend(extracted_note)

        # Combine distinct invoice references from reference and note, preserving appearance order
        all_hits: list[str] = []
        seen_all: set[str] = set()
        for num in ref_hits + note_hits:
            canon = _normalize_ref(num)
            if canon not in seen_all:
                seen_all.add(canon)
                all_hits.append(num)

        # 3. No amount-only matching in this matcher
        if not all_hits:
            return self._no_match(
                p,
                Evidence(
                    EvidenceCode.REFERENCE_ABSENT,
                    "no known invoice reference was found; "
                    "amount-only matching is disabled",
                ),
            )

        # Resolve each reference to an existing invoice record
        matched_invoices: list[Invoice] = []
        for ref in all_hits:
            hits = run.by_ref.get(canonical_reference(ref), [])
            if not hits:
                return self._no_match(
                    p,
                    Evidence(
                        EvidenceCode.REFERENCE_UNKNOWN,
                        f"no invoice found for {ref!r}",
                    ),
                )
            if len(hits) > 1:
                return self._no_match(
                    p,
                    Evidence(
                        EvidenceCode.MULTIPLE_CANDIDATES,
                        f"invoice reference {ref!r} is not unique",
                    ),
                )
            matched_invoices.append(hits[0])

        if len({inv.invoice_id for inv in matched_invoices}) != len(matched_invoices):
            return self._no_match(
                p,
                Evidence(
                    EvidenceCode.MULTIPLE_CANDIDATES,
                    "duplicate invoice references found in payment",
                ),
            )

        # 4. Single invoice matching (exact or partial)
        if len(matched_invoices) == 1:
            invoice = matched_invoices[0]
            blockers = self._partial_blockers(p, invoice, run)
            if blockers:
                return self._no_match(p, *blockers)

            source = (
                "reference field"
                if invoice.number in ref_hits
                else "payment note"
            )

            outstanding = run.outstanding[invoice.invoice_id]
            if p.amount_cents == outstanding:
                amount_evidence = Evidence(
                    EvidenceCode.AMOUNT_EXACT,
                    f"{p.amount_cents} cents equals the outstanding balance",
                )
            else:
                amount_evidence = Evidence(
                    EvidenceCode.AMOUNT_PARTIAL,
                    f"{p.amount_cents} cents is a partial payment against "
                    f"{outstanding} cents outstanding on {invoice.number}",
                )

            evidence = (
                Evidence(
                    EvidenceCode.REFERENCE_MATCH,
                    f"invoice {invoice.number} extracted from the {source}",
                ),
                amount_evidence,
            )

            return self._proposed(p, invoice, run, evidence)

        # 5. Grouped payment matching across multiple invoices
        for inv in matched_invoices:
            if inv.currency.upper() != p.currency.upper():
                return self._no_match(
                    p,
                    Evidence(
                        EvidenceCode.CURRENCY_MISMATCH,
                        f"{p.currency} payment vs {inv.currency} invoice {inv.number}",
                    ),
                )
            if not inv.is_open:
                return self._no_match(
                    p,
                    Evidence(
                        EvidenceCode.INVOICE_NOT_OPEN,
                        f"invoice {inv.number} is {inv.status.value}",
                    ),
                )
            if inv.invoice_id in run.claimed:
                return self._no_match(
                    p,
                    Evidence(
                        EvidenceCode.INVOICE_ALREADY_CLAIMED,
                        f"invoice {inv.number} already proposed for payment {run.claimed[inv.invoice_id]}",
                    ),
                )
            if run.outstanding[inv.invoice_id] <= 0:
                return self._no_match(
                    p,
                    Evidence(
                        EvidenceCode.AMOUNT_MISMATCH,
                        f"invoice {inv.number} has no outstanding balance",
                    ),
                )

        total_outstanding = sum(
            run.outstanding[inv.invoice_id] for inv in matched_invoices
        )
        if p.amount_cents != total_outstanding:
            return self._no_match(
                p,
                Evidence(
                    EvidenceCode.AMOUNT_MISMATCH,
                    f"payment {p.amount_cents} cents does not match total "
                    f"outstanding {total_outstanding} cents across referenced invoices",
                ),
            )

        # Claim each invoice in this run to prevent conflicting proposals
        for inv in matched_invoices:
            run.claimed[inv.invoice_id] = p.payment_id

        allocations = tuple(
            PaymentAllocation(
                payment_id=p.payment_id,
                invoice_id=inv.invoice_id,
                amount_cents=run.outstanding[inv.invoice_id],
                status=AllocationStatus.PROPOSED,
            )
            for inv in matched_invoices
        )

        inv_names = ", ".join(inv.number for inv in matched_invoices)
        evidence = (
            Evidence(
                EvidenceCode.REFERENCE_MATCH,
                f"invoices {inv_names} extracted from payment reference/note",
            ),
            Evidence(
                EvidenceCode.AMOUNT_EXACT,
                f"{p.amount_cents} cents exactly matches total outstanding across referenced invoices",
            ),
        )

        candidate = Candidate(allocations, evidence)
        return MatchProposal(
            payment_id=p.payment_id,
            outcome=MatchOutcome.PROPOSED,
            candidates=(candidate,),
            reasons=(),
            matcher=self.name,
        )

    # ------------------------------------------------------------------
    # Blocker logic: relaxed to allow partial payments
    # ------------------------------------------------------------------

    @staticmethod
    def _partial_blockers(p: Payment, inv: Invoice, run: _Run) -> list[Evidence]:
        """Like _Run.blockers but allows payment ≤ outstanding (partial payments).

        The baseline's blockers() requires payment == outstanding (exact match).
        Here we only reject if the payment exceeds the outstanding balance, or
        if other hard constraints (currency, status, already-claimed) fail.
        """
        out: list[Evidence] = []
        if inv.currency != p.currency:
            out.append(Evidence(EvidenceCode.CURRENCY_MISMATCH, f"{p.currency} payment vs {inv.currency} invoice"))
        if not inv.is_open:
            out.append(Evidence(EvidenceCode.INVOICE_NOT_OPEN, f"invoice {inv.number} is {inv.status.value}"))
        if inv.invoice_id in run.claimed:
            out.append(
                Evidence(
                    EvidenceCode.INVOICE_ALREADY_CLAIMED,
                    f"invoice {inv.number} already proposed for payment {run.claimed[inv.invoice_id]}",
                )
            )
        outstanding = run.outstanding[inv.invoice_id]
        if p.amount_cents > outstanding:
            out.append(
                Evidence(
                    EvidenceCode.AMOUNT_MISMATCH,
                    f"payment {p.amount_cents} exceeds outstanding {outstanding} on {inv.number}",
                )
            )
        return out



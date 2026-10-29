"""Conservative deterministic matcher with free-text reference extraction."""

from __future__ import annotations

from typing import Sequence

from .baseline import BaselineConfig, BaselineMatcher, _Run
from .models import (
    Evidence,
    EvidenceCode,
    Invoice,
    MatchProposal,
    Payment,
    PaymentStatus,
)
from .normalize import canonical_reference, extract_invoice_references


class DeterministicMatcher(BaselineMatcher):
    """Resolve exact, uniquely referenced invoices without amount-only guesses.

    This first version supports references in the structured reference field
    or payment note. Partial payments are allowed when a trusted reference is
    present: the allocation equals the payment amount and must not exceed the
    outstanding balance. Grouped payments remain unresolved for now.
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

        # 2. Extract only invoice numbers that exist in our invoice records.
        reference_hits = extract_invoice_references(
            [p.reference], invoice_numbers
        )

        # An explicitly supplied but unknown reference is a warning sign.
        # Never ignore it and guess based on the amount or payment note.
        if (
            p.reference
            and canonical_reference(p.reference)
            and not reference_hits
        ):
            return self._no_match(
                p,
                Evidence(
                    EvidenceCode.REFERENCE_UNKNOWN,
                    f"could not identify a known invoice in reference "
                    f"{p.reference!r}",
                ),
            )

        note_hits = extract_invoice_references(
            [p.note], invoice_numbers
        )
        all_hits = extract_invoice_references(
            [p.reference, p.note], invoice_numbers
        )

        # 3. No amount-only matching in this matcher.
        if not all_hits:
            return self._no_match(
                p,
                Evidence(
                    EvidenceCode.REFERENCE_ABSENT,
                    "no known invoice reference was found; "
                    "amount-only matching is disabled",
                ),
            )

        # Several references may indicate a grouped payment. Do not pretend
        # that one invoice is the correct answer before grouped matching exists.
        if len(all_hits) > 1:
            return self._no_match(
                p,
                Evidence(
                    EvidenceCode.MULTIPLE_CANDIDATES,
                    "multiple invoice references found; grouped-payment "
                    "resolution is not implemented yet",
                ),
            )

        reference = all_hits[0]
        hits = run.by_ref.get(canonical_reference(reference), [])

        if not hits:
            return self._no_match(
                p,
                Evidence(
                    EvidenceCode.REFERENCE_UNKNOWN,
                    f"no invoice found for {reference!r}",
                ),
            )

        if len(hits) > 1:
            return self._no_match(
                p,
                Evidence(
                    EvidenceCode.MULTIPLE_CANDIDATES,
                    f"invoice reference {reference!r} is not unique",
                ),
            )

        invoice = hits[0]

        # 4. Custom blocker checks: allow partial payments (payment ≤ outstanding)
        #    instead of requiring exact match like the baseline.
        blockers = self._partial_blockers(p, invoice, run)
        if blockers:
            return self._no_match(p, *blockers)

        source = (
            "reference field"
            if reference in reference_hits
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


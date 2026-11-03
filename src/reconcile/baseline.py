"""Baseline matcher: exact reference and exact amount. No AI, no fuzziness.

It exists to be beaten. It is also the safety floor: it must never turn an
amount coincidence into a silent allocation. Every answer is a MatchProposal
carrying its evidence; nothing is approved and no balance changes.

Rules, in order, per delivered payment (processed chronologically):

1. Replayed or contradictory delivery -> NO_MATCH (DUPLICATE_EVENT). A PENDING
   delivery later redelivered as COMPLETED is a status transition, not a
   duplicate, and is matched on the COMPLETED delivery.
2. Payment not COMPLETED          -> NO_MATCH (PAYMENT_NOT_COMPLETED)
3. Reference field present        -> look the invoice up by canonical number.
   Unknown / ambiguous reference, or an invoice that is closed, in another
   currency, already claimed by an earlier proposal, or whose outstanding
   amount differs from the payment -> NO_MATCH. No fallback to amount-only
   when a reference was given (a wrong reference is a signal, not noise).
   Otherwise -> PROPOSED [REFERENCE_MATCH, AMOUNT_EXACT].
4. No reference                   -> (only if allow_amount_only) open invoices
   whose outstanding amount equals the payment. One unclaimed -> PROPOSED
   [AMOUNT_EXACT, REFERENCE_ABSENT]; several -> AMBIGUOUS; none -> NO_MATCH.

Known blind spots, on purpose: partial payments, one payment covering several
invoices, invoice numbers buried in free text, payer-name reasoning.

Deciding what the canonical record for a payment_id is belongs to
`delivery.py`; this matcher only asks it whether a delivery may be matched.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from .matching import MatchRun
from .models import (
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
from .normalize import canonical_reference


@dataclass(frozen=True)
class BaselineConfig:
    # Amount-only matches are weak evidence. They are still emitted as
    # proposals (never as approvals), and flagged with REFERENCE_ABSENT.
    allow_amount_only: bool = True


class BaselineMatcher:
    def __init__(self, config: Optional[BaselineConfig] = None, name: Optional[str] = None):
        self.config = config or BaselineConfig()
        self.name = name or ("baseline" if self.config.allow_amount_only else "baseline-strict")

    def match(
        self,
        payments: Sequence[Payment],
        invoices: Sequence[Invoice],
        approved: Sequence[PaymentAllocation] = (),
    ) -> list[MatchProposal]:
        run = MatchRun(invoices, approved)
        ordered = sorted(payments, key=lambda p: (p.received_at, p.payment_id))
        return [self._match_one(p, run) for p in ordered]

    # ------------------------------------------------------------------

    def screen_delivery(self, p: Payment, run: MatchRun) -> Optional[Evidence]:
        """Blocking evidence for a delivery that must not be matched at all.

        Shared by every matcher built on this one, so the rules for ledger
        duplicates, replayed deliveries, canonical status transitions and
        uncompleted payments are decided in exactly one place.
        """
        if p.payment_id in run.approved_payment_ids:
            return Evidence(
                EvidenceCode.DUPLICATE_EVENT,
                f"payment {p.payment_id} is already approved in the ledger",
            )

        replay = run.record_delivery(p)
        if replay is not None:
            return replay

        canonical = run.seen[p.payment_id]
        if canonical.status is not PaymentStatus.COMPLETED:
            return Evidence(
                EvidenceCode.PAYMENT_NOT_COMPLETED,
                f"payment status is {canonical.status.value}",
            )

        return None

    def _match_one(self, p: Payment, run: MatchRun) -> MatchProposal:
        blocker = self.screen_delivery(p, run)
        if blocker is not None:
            return self.no_match(p, blocker)

        ref = canonical_reference(p.reference)
        if ref:
            return self._match_by_reference(p, ref, run)
        return self._match_by_amount(p, run)

    def _match_by_reference(self, p: Payment, ref: str, run: MatchRun) -> MatchProposal:
        hits = run.by_ref.get(ref, [])
        if not hits:
            return self.no_match(
                p, Evidence(EvidenceCode.REFERENCE_UNKNOWN, f"no invoice numbered {p.reference!r}")
            )
        if len(hits) > 1:
            return self.no_match(
                p,
                Evidence(
                    EvidenceCode.MULTIPLE_CANDIDATES,
                    f"reference {p.reference!r} matches {len(hits)} invoices",
                ),
            )
        inv = hits[0]
        blockers = run.blockers(p, inv)
        if blockers:
            return self.no_match(p, *blockers)
        evidence = (
            Evidence(EvidenceCode.REFERENCE_MATCH, f"reference {p.reference!r} = invoice {inv.number}"),
            Evidence(EvidenceCode.AMOUNT_EXACT, f"{p.amount_cents} cents equals outstanding balance"),
        )
        return self._proposed(p, inv, run, evidence)

    def _match_by_amount(self, p: Payment, run: MatchRun) -> MatchProposal:
        if not self.config.allow_amount_only:
            return self.no_match(
                p, Evidence(EvidenceCode.REFERENCE_ABSENT, "no reference and amount-only matching is off")
            )
        same_amount = [
            inv
            for inv in run.invoices
            if inv.is_open
            and inv.currency == p.currency
            and run.outstanding[inv.invoice_id] == p.amount_cents
        ]
        if not same_amount:
            return self.no_match(
                p, Evidence(EvidenceCode.NO_CANDIDATE, "no open invoice with this outstanding amount")
            )
        free = [inv for inv in same_amount if inv.invoice_id not in run.claimed]
        if not free:
            return self.no_match(
                p,
                Evidence(
                    EvidenceCode.INVOICE_ALREADY_CLAIMED,
                    "every invoice with this amount is already claimed by an earlier proposal",
                ),
            )
        weak = (
            Evidence(EvidenceCode.AMOUNT_EXACT, f"{p.amount_cents} cents equals outstanding balance"),
            Evidence(EvidenceCode.REFERENCE_ABSENT, "no reference; amount is the only link"),
        )
        if len(free) == 1:
            return self._proposed(p, free[0], run, weak)
        candidates = tuple(
            Candidate(
                (
                    PaymentAllocation(
                        p.payment_id,
                        inv.invoice_id,
                        p.amount_cents,
                        currency=inv.currency,
                    ),
                ),
                weak,
            )
            for inv in free
        )
        return MatchProposal(
            payment_id=p.payment_id,
            outcome=MatchOutcome.AMBIGUOUS,
            candidates=candidates,
            reasons=(
                Evidence(EvidenceCode.MULTIPLE_CANDIDATES, f"{len(free)} open invoices share this amount"),
            ),
            matcher=self.name,
        )

    # ------------------------------------------------------------------

    def _proposed(
        self, p: Payment, inv: Invoice, run: MatchRun, evidence: tuple[Evidence, ...]
    ) -> MatchProposal:
        run.claimed[inv.invoice_id] = p.payment_id
        cand = Candidate(
            (
                PaymentAllocation(
                    p.payment_id,
                    inv.invoice_id,
                    p.amount_cents,
                    currency=inv.currency,
                ),
            ),
            evidence,
        )
        return MatchProposal(p.payment_id, MatchOutcome.PROPOSED, (cand,), (), self.name)

    def no_match(self, p: Payment, *reasons: Evidence) -> MatchProposal:
        return MatchProposal(p.payment_id, MatchOutcome.NO_MATCH, (), tuple(reasons), self.name)

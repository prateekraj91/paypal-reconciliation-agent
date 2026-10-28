"""Baseline matcher: exact reference and exact amount. No AI, no fuzziness.

It exists to be beaten. It is also the safety floor: it must never turn an
amount coincidence into a silent allocation. Every answer is a MatchProposal
carrying its evidence; nothing is approved and no balance changes.

Rules, in order, per delivered payment (processed chronologically):

1. Repeated payment_id            -> NO_MATCH (DUPLICATE_EVENT)
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

Duplicate handling lives here only because the baseline is the first matcher.
Once webhook ingestion exists, dedupe belongs there (idempotent handling).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

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
    outstanding_cents,
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
        run = _Run(list(invoices), approved)
        ordered = sorted(payments, key=lambda p: (p.received_at, p.payment_id))
        return [self._match_one(p, run) for p in ordered]

    # ------------------------------------------------------------------

    def _match_one(self, p: Payment, run: "_Run") -> MatchProposal:
        if p.payment_id in run.seen:
            return self._no_match(
                p, Evidence(EvidenceCode.DUPLICATE_EVENT, f"payment {p.payment_id} already processed")
            )
        run.seen.add(p.payment_id)

        if p.status is not PaymentStatus.COMPLETED:
            return self._no_match(
                p, Evidence(EvidenceCode.PAYMENT_NOT_COMPLETED, f"payment status is {p.status.value}")
            )

        ref = canonical_reference(p.reference)
        if ref:
            return self._match_by_reference(p, ref, run)
        return self._match_by_amount(p, run)

    def _match_by_reference(self, p: Payment, ref: str, run: "_Run") -> MatchProposal:
        hits = run.by_ref.get(ref, [])
        if not hits:
            return self._no_match(
                p, Evidence(EvidenceCode.REFERENCE_UNKNOWN, f"no invoice numbered {p.reference!r}")
            )
        if len(hits) > 1:
            return self._no_match(
                p,
                Evidence(
                    EvidenceCode.MULTIPLE_CANDIDATES,
                    f"reference {p.reference!r} matches {len(hits)} invoices",
                ),
            )
        inv = hits[0]
        blockers = run.blockers(p, inv)
        if blockers:
            return self._no_match(p, *blockers)
        evidence = (
            Evidence(EvidenceCode.REFERENCE_MATCH, f"reference {p.reference!r} = invoice {inv.number}"),
            Evidence(EvidenceCode.AMOUNT_EXACT, f"{p.amount_cents} cents equals outstanding balance"),
        )
        return self._proposed(p, inv, run, evidence)

    def _match_by_amount(self, p: Payment, run: "_Run") -> MatchProposal:
        if not self.config.allow_amount_only:
            return self._no_match(
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
            return self._no_match(
                p, Evidence(EvidenceCode.NO_CANDIDATE, "no open invoice with this outstanding amount")
            )
        free = [inv for inv in same_amount if inv.invoice_id not in run.claimed]
        if not free:
            return self._no_match(
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
            Candidate((PaymentAllocation(p.payment_id, inv.invoice_id, p.amount_cents),), weak)
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
        self, p: Payment, inv: Invoice, run: "_Run", evidence: tuple[Evidence, ...]
    ) -> MatchProposal:
        run.claimed[inv.invoice_id] = p.payment_id
        cand = Candidate((PaymentAllocation(p.payment_id, inv.invoice_id, p.amount_cents),), evidence)
        return MatchProposal(p.payment_id, MatchOutcome.PROPOSED, (cand,), (), self.name)

    def _no_match(self, p: Payment, *reasons: Evidence) -> MatchProposal:
        return MatchProposal(p.payment_id, MatchOutcome.NO_MATCH, (), tuple(reasons), self.name)


class _Run:
    """Per-call working state. Proposals reserve invoices within one run so
    two payments cannot both be proposed against the same invoice."""

    def __init__(self, invoices: list[Invoice], approved: Sequence[PaymentAllocation]):
        self.invoices = invoices
        self.outstanding = {i.invoice_id: outstanding_cents(i, approved) for i in invoices}
        self.by_ref: dict[str, list[Invoice]] = {}
        for inv in invoices:
            self.by_ref.setdefault(canonical_reference(inv.number), []).append(inv)
        self.claimed: dict[str, str] = {}  # invoice_id -> payment_id holding the proposal
        self.seen: set[str] = set()

    def blockers(self, p: Payment, inv: Invoice) -> list[Evidence]:
        out: list[Evidence] = []
        if inv.currency != p.currency:
            out.append(Evidence(EvidenceCode.CURRENCY_MISMATCH, f"{p.currency} payment vs {inv.currency} invoice"))
        if not inv.is_open:
            out.append(Evidence(EvidenceCode.INVOICE_NOT_OPEN, f"invoice {inv.number} is {inv.status.value}"))
        if inv.invoice_id in self.claimed:
            out.append(
                Evidence(
                    EvidenceCode.INVOICE_ALREADY_CLAIMED,
                    f"invoice {inv.number} already proposed for payment {self.claimed[inv.invoice_id]}",
                )
            )
        outstanding = self.outstanding[inv.invoice_id]
        if outstanding != p.amount_cents:
            out.append(
                Evidence(
                    EvidenceCode.AMOUNT_MISMATCH,
                    f"payment {p.amount_cents} vs outstanding {outstanding} on {inv.number}",
                )
            )
        return out

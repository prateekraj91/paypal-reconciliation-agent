"""Per-run working state shared by every matcher.

`MatchRun` holds everything one `Matcher.match()` call needs to stay
self-consistent: the invoices in play, their outstanding balances, the
reference index, which invoices an earlier proposal has already claimed, and
the delivery stream seen so far.

It lives here rather than inside the baseline because every matcher needs it.
A matcher reaching into another matcher's private state is how two matchers
end up disagreeing about the same ledger.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Mapping, Optional, Sequence

from .delivery import PaymentConflictError, resolve_delivery
from .models import (
    AllocationStatus,
    Evidence,
    EvidenceCode,
    Invoice,
    Payment,
    PaymentAllocation,
    PaymentReversal,
    Refund,
    cross_check_invoice_balance,
    payment_net_cents,
)
from .normalize import canonical_reference


class MatchRun:
    """Working state for one match() call.

    Proposals reserve invoices within one run (`claimed`) so two payments
    cannot both be proposed against the same invoice.
    """

    def __init__(
        self,
        invoices: Sequence[Invoice],
        approved: Sequence[PaymentAllocation] = (),
        settlements: Sequence[PaymentAllocation] = (),
        refunds: Sequence[Refund] = (),
        reversals: Sequence[PaymentReversal] = (),
        payment_capacity: Optional[Mapping[str, int]] = None,
    ) -> None:
        self.payment_capacity = payment_capacity
        self.available_cents: dict[str, int] = {}  # payment_id -> money still available
        self.invoices: list[Invoice] = list(invoices)
        self.refunds: list[Refund] = list(refunds)
        self.reversals: list[PaymentReversal] = list(reversals)
        all_deductions = list(approved) + list(settlements)
        self.outstanding: dict[str, int] = {
            i.invoice_id: cross_check_invoice_balance(
                i, all_deductions, refunds=self.refunds, reversals=self.reversals
            )
            for i in self.invoices
        }
        self.by_ref: dict[str, list[Invoice]] = {}
        for inv in self.invoices:
            self.by_ref.setdefault(canonical_reference(inv.number), []).append(inv)
        self.claimed: dict[str, str] = {}  # invoice_id -> payment_id holding the proposal
        self.seen: dict[str, Payment] = {}  # payment_id -> canonical delivery so far
        self.conflicted: set[str] = set()  # payment_ids whose deliveries contradict
        self.approved_payment_ids: set[str] = {
            a.payment_id for a in approved if a.status is AllocationStatus.APPROVED
        }
        self.settled_payment_ids: set[str] = {
            s.payment_id for s in settlements
        }

    def for_matching(self, p: Payment) -> Payment:
        """The delivery as matchers must see it: for the money still available.

        A payment whose money has all been returned keeps its gross amount here,
        because `screen_delivery` blocks it before any amount is used.
        """
        if self.payment_capacity is not None and p.payment_id in self.payment_capacity:
            available = self.payment_capacity[p.payment_id]
        else:
            available = payment_net_cents(
                p.payment_id, p.amount_cents, self.refunds, self.reversals
            )
        self.available_cents[p.payment_id] = available
        return replace(p, amount_cents=available) if 0 < available < p.amount_cents else p

    # ------------------------------------------------------------------
    # Delivery stream
    # ------------------------------------------------------------------

    def record_delivery(self, p: Payment) -> Optional[Evidence]:
        """Register one delivery and say whether it may be matched.

        Returns None when `p` is the first delivery of this payment, or a
        legal forward status transition of one (PENDING then COMPLETED): the
        caller should go on and match it.

        Returns blocking Evidence when the delivery repeats a status already
        processed, moves the status backwards, or contradicts an earlier
        delivery of the same payment_id. Contradictions fail closed: the
        payment_id is recorded in `conflicted` and never matched in this run.
        """
        previous = self.seen.get(p.payment_id)

        if p.payment_id in self.conflicted:
            return Evidence(
                EvidenceCode.DUPLICATE_EVENT,
                f"payment {p.payment_id} has contradictory deliveries in this batch",
            )

        if previous is None:
            self.seen[p.payment_id] = p
            return None

        try:
            canonical = resolve_delivery([previous, p])
        except PaymentConflictError as exc:
            self.conflicted.add(p.payment_id)
            return Evidence(EvidenceCode.DUPLICATE_EVENT, str(exc))

        self.seen[p.payment_id] = canonical

        if canonical.status is previous.status:
            # Identical replay, or a stale out-of-order delivery. Either way the
            # canonical record did not advance, so there is nothing new to match.
            return Evidence(
                EvidenceCode.DUPLICATE_EVENT,
                f"payment {p.payment_id} was already processed",
            )

        return None

    # ------------------------------------------------------------------
    # Allocation constraints
    # ------------------------------------------------------------------

    def blockers(
        self,
        p: Payment,
        inv: Invoice,
        *,
        allow_partial: bool = False,
    ) -> list[Evidence]:
        """Reasons this payment must not be allocated to this invoice.

        With `allow_partial` the payment only has to fit inside the
        outstanding balance; without it the amounts must be equal.
        """
        out: list[Evidence] = []
        if inv.currency != p.currency:
            out.append(
                Evidence(
                    EvidenceCode.CURRENCY_MISMATCH,
                    f"{p.currency} payment vs {inv.currency} invoice",
                )
            )
        if not inv.is_open:
            out.append(
                Evidence(
                    EvidenceCode.INVOICE_NOT_OPEN,
                    f"invoice {inv.number} is {inv.status.value}",
                )
            )
        if inv.invoice_id in self.claimed:
            out.append(
                Evidence(
                    EvidenceCode.INVOICE_ALREADY_CLAIMED,
                    f"invoice {inv.number} already proposed for payment "
                    f"{self.claimed[inv.invoice_id]}",
                )
            )
        outstanding = self.outstanding[inv.invoice_id]
        if allow_partial:
            if p.amount_cents > outstanding:
                out.append(
                    Evidence(
                        EvidenceCode.AMOUNT_MISMATCH,
                        f"payment {p.amount_cents} exceeds outstanding "
                        f"{outstanding} on {inv.number}",
                    )
                )
        elif outstanding != p.amount_cents:
            out.append(
                Evidence(
                    EvidenceCode.AMOUNT_MISMATCH,
                    f"payment {p.amount_cents} vs outstanding {outstanding} "
                    f"on {inv.number}",
                )
            )
        return out

"""Contracts between the reconciliation core and everything around it.

The PayPal adapter, the synthetic dataset and test fixtures all produce
`Invoice` / `Payment` through these interfaces. Matchers (baseline now, the
deterministic matcher and AI investigator later) all implement `Matcher`.
The core only ever sees these protocols.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Sequence, runtime_checkable

from .models import Invoice, MatchProposal, Payment, PaymentAllocation


@runtime_checkable
class InvoiceSource(Protocol):
    def list_invoices(self) -> Sequence[Invoice]: ...


@runtime_checkable
class PaymentSource(Protocol):
    def list_payments(self) -> Sequence[Payment]:
        """The delivery stream. May contain the same payment_id more than once
        (webhook retries); matchers must handle that."""
        ...


@runtime_checkable
class Matcher(Protocol):
    name: str

    def match(
        self,
        payments: Sequence[Payment],
        invoices: Sequence[Invoice],
        approved: Sequence[PaymentAllocation] = (),
    ) -> list[MatchProposal]:
        """Return exactly one MatchProposal per delivered payment, in
        chronological processing order. `approved` are allocations already in
        the ledger; only APPROVED ones affect outstanding balances."""
        ...


@dataclass(frozen=True)
class StaticSource:
    """In-memory source used by tests and the synthetic dataset."""

    invoices: tuple[Invoice, ...]
    payments: tuple[Payment, ...]

    def list_invoices(self) -> Sequence[Invoice]:
        return self.invoices

    def list_payments(self) -> Sequence[Payment]:
        return self.payments

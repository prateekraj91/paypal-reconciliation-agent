"""Reproducible synthetic reconciliation dataset with ground truth.

`generate_dataset(seed)` always returns the same invoices, payments and truth
for the same seed. Every payment carries a `GroundTruth` saying what a careful
human would do with it:

* AUTO_MATCH  - the evidence supports exactly one allocation; a good system
                may propose it confidently.
* NEEDS_HUMAN - the evidence is genuinely insufficient (ties, strangers,
                double payments). A good system must escalate, not guess. Where
                the generator knows the real answer it is stored in
                `allocations` so a human reviewer can be simulated later.
* NO_ACTION   - nothing to allocate (refunded payment).

Amounts are drawn so that, outside the deliberate ties, every invoice total
and every payment total is unique. That keeps the ground truth unambiguous.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from typing import Mapping, Optional

from .interfaces import StaticSource
from .models import Invoice, InvoiceStatus, Payment, PaymentStatus


class CaseCategory(str, Enum):
    EXACT_REFERENCE = "EXACT_REFERENCE"  # reference field equals invoice number (case/hyphen noise)
    NOISY_REFERENCE = "NOISY_REFERENCE"  # invoice number buried in free text in the reference field
    MISSING_REFERENCE_UNIQUE = "MISSING_REFERENCE_UNIQUE"  # no reference, amount is unique
    PARTIAL_PAYMENT = "PARTIAL_PAYMENT"  # reference given, amount is part of the invoice
    GROUPED_PAYMENT_NOTE = "GROUPED_PAYMENT_NOTE"  # one payment, several invoices listed in the note
    GROUPED_PAYMENT_NO_REF = "GROUPED_PAYMENT_NO_REF"  # one payment = payer's entire open balance, no note
    PAYER_NAME_VARIATION = "PAYER_NAME_VARIATION"  # no reference; two invoices tie on amount; payer name resolves it
    AMBIGUOUS_SAME_AMOUNT = "AMBIGUOUS_SAME_AMOUNT"  # same payer, two identical open invoices
    AMBIGUOUS_COMBINATION = "AMBIGUOUS_COMBINATION"  # two different invoice combinations total the payment
    DUPLICATE_EVENT = "DUPLICATE_EVENT"  # the same webhook delivered twice
    DOUBLE_PAYMENT = "DOUBLE_PAYMENT"  # customer pays the same invoice twice
    REFUNDED = "REFUNDED"  # payment later refunded
    UNRELATED_PAYMENT = "UNRELATED_PAYMENT"  # payment from a stranger tied to nothing
    AMOUNT_COLLISION = "AMOUNT_COLLISION"  # stranger's amount coincides with an unrelated open invoice


class Resolution(str, Enum):
    AUTO_MATCH = "AUTO_MATCH"
    NEEDS_HUMAN = "NEEDS_HUMAN"
    NO_ACTION = "NO_ACTION"


@dataclass(frozen=True)
class GroundTruth:
    payment_id: str
    category: CaseCategory
    resolution: Resolution
    allocations: tuple[tuple[str, int], ...] = ()  # (invoice_id, cents)

    @property
    def allocation_set(self) -> frozenset[tuple[str, int]]:
        return frozenset(self.allocations)


@dataclass(frozen=True, eq=False)
class Dataset:
    seed: int
    invoices: tuple[Invoice, ...]
    payments: tuple[Payment, ...]  # delivery stream, chronological, includes duplicate deliveries
    truth: Mapping[str, GroundTruth]  # keyed by payment_id

    def source(self) -> StaticSource:
        return StaticSource(self.invoices, self.payments)

    @property
    def unique_payment_count(self) -> int:
        return len({p.payment_id for p in self.payments})


DEFAULT_MIX: dict[CaseCategory, int] = {
    CaseCategory.EXACT_REFERENCE: 24,
    CaseCategory.NOISY_REFERENCE: 6,
    CaseCategory.MISSING_REFERENCE_UNIQUE: 12,
    CaseCategory.PARTIAL_PAYMENT: 10,
    CaseCategory.GROUPED_PAYMENT_NOTE: 5,
    CaseCategory.GROUPED_PAYMENT_NO_REF: 5,
    CaseCategory.PAYER_NAME_VARIATION: 10,
    CaseCategory.AMBIGUOUS_SAME_AMOUNT: 8,
    CaseCategory.AMBIGUOUS_COMBINATION: 6,
    CaseCategory.DUPLICATE_EVENT: 5,
    CaseCategory.DOUBLE_PAYMENT: 4,
    CaseCategory.REFUNDED: 5,
    CaseCategory.UNRELATED_PAYMENT: 5,
    CaseCategory.AMOUNT_COLLISION: 4,
}

DEFAULT_SEED = 2026

_START = datetime(2026, 9, 1, tzinfo=timezone.utc)

_BUSINESS_HEADS = (
    "Acme", "Birch", "Cobalt", "Dune", "Ember", "Fable", "Granite", "Harbor", "Indigo", "Juniper",
    "Kestrel", "Lumen", "Maple", "Nimbus", "Onyx", "Pioneer", "Quartz", "Ridge", "Saffron", "Tundra",
    "Umber", "Vertex", "Willow", "Yarrow", "Zephyr", "Atlas", "Beacon", "Cinder", "Delta", "Falcon",
)
_BUSINESS_TAILS = ("Studio", "Labs", "Works", "Media", "Consulting", "Design", "Digital", "Partners")
_FIRST = (
    "Priya", "Anil", "Meera", "Rohan", "Sana", "Kabir", "Isha", "Dev", "Nisha", "Arjun",
    "Tara", "Vikram", "Leela", "Omar", "Zoya", "Neil", "Ritu", "Samir", "Anya", "Jay",
)
_LAST = (
    "Nair", "Shah", "Verma", "Iyer", "Khan", "Reddy", "Mehta", "Bose", "Kapoor", "Joshi",
    "Menon", "Pillai", "Sethi", "Rao", "Das", "Ghosh", "Malik", "Sinha", "Gill", "Desai",
)


@dataclass(frozen=True)
class _Customer:
    name: str
    email: str
    kind: str  # "business" | "person"


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


class _Builder:
    def __init__(self, seed: int) -> None:
        self.rng = random.Random(seed)
        self.invoices: list[Invoice] = []
        self.payments: list[Payment] = []
        self.truth: dict[str, GroundTruth] = {}
        self._inv_n = 0
        self._pay_n = 0
        self._used_amounts: set[int] = set()
        self._used_names: set[str] = set()
        self._amount_only_invoices: list[Invoice] = []

    # ---- primitives ---------------------------------------------------

    def customer(self, avoid: Optional[str] = None) -> _Customer:
        avoid_tokens = set(avoid.split()) if avoid else set()
        for _ in range(10_000):
            if self.rng.random() < 0.7:
                name = f"{self.rng.choice(_BUSINESS_HEADS)} {self.rng.choice(_BUSINESS_TAILS)}"
                kind = "business"
            else:
                name = f"{self.rng.choice(_FIRST)} {self.rng.choice(_LAST)}"
                kind = "person"
            if name in self._used_names or avoid_tokens & set(name.split()):
                continue
            self._used_names.add(name)
            return _Customer(name, f"{_slug(name)}@example.com", kind)
        raise RuntimeError("customer name pool exhausted; reduce the case mix")

    def amount(self, lo_dollars: int = 150, hi_dollars: int = 4800) -> int:
        """A fresh, never-before-used amount in cents."""
        for _ in range(10_000):
            cents = self.rng.randrange(lo_dollars, hi_dollars) * 100 + self.rng.choice((0, 0, 0, 50, 99))
            if self.reserve(cents):
                return cents
        raise RuntimeError("amount space exhausted; reduce the case mix")

    def reserve(self, *amounts: int) -> bool:
        if any(a in self._used_amounts for a in amounts) or len(set(amounts)) != len(amounts):
            return False
        self._used_amounts.update(amounts)
        return True

    def pay_time(self) -> datetime:
        return _START + timedelta(
            days=self.rng.randrange(0, 45), hours=self.rng.randrange(8, 20), minutes=self.rng.randrange(60)
        )

    def issue_date(self, paid_at: datetime) -> date:
        return (paid_at - timedelta(days=self.rng.randrange(5, 30))).date()

    def invoice(
        self,
        cust: _Customer,
        amount: int,
        *,
        issued: date,
        status: InvoiceStatus = InvoiceStatus.SENT,
        memo: str = "",
    ) -> Invoice:
        self._inv_n += 1
        inv = Invoice(
            invoice_id=f"inv_{self._inv_n:04d}",
            number=f"INV-{1000 + self._inv_n}",
            customer_name=cust.name,
            customer_email=cust.email,
            amount_cents=amount,
            currency="USD",
            issued_on=issued,
            due_on=issued + timedelta(days=30),
            status=status,
            memo=memo,
        )
        self.invoices.append(inv)
        return inv

    def payment(
        self,
        *,
        amount: int,
        payer_name: str,
        payer_email: Optional[str],
        reference: Optional[str],
        when: datetime,
        note: str = "",
        status: PaymentStatus = PaymentStatus.COMPLETED,
    ) -> Payment:
        self._pay_n += 1
        p = Payment(
            payment_id=f"PAY-{self._pay_n:05d}",
            amount_cents=amount,
            currency="USD",
            received_at=when,
            payer_name=payer_name,
            payer_email=payer_email,
            reference=reference,
            note=note,
            status=status,
        )
        self.payments.append(p)
        return p

    def expect(
        self,
        p: Payment,
        category: CaseCategory,
        resolution: Resolution,
        allocations: list[tuple[str, int]],
    ) -> None:
        self.truth[p.payment_id] = GroundTruth(p.payment_id, category, resolution, tuple(allocations))

    # ---- text noise ---------------------------------------------------

    def name_variant(self, c: _Customer) -> str:
        if c.kind == "person":
            first, last = c.name.split()
            options = [f"{first[0]}. {last}", f"{last}, {first}", c.name.upper(), c.name.lower()]
        else:
            head, _tail = c.name.split()
            typo = head[0] + head[2] + head[1] + head[3:] + c.name[len(head):]
            options = [f"{c.name} LLC", f"{c.name} Inc.", c.name.upper(), f"The {c.name}", f"{c.name}, LLC", typo]
        return self.rng.choice(options)

    def maybe_variant(self, c: _Customer, p: float) -> str:
        return self.name_variant(c) if self.rng.random() < p else c.name

    def reference_variant(self, number: str) -> str:
        kind = self.rng.choice(("as_is", "as_is", "lower", "nohyphen", "spaced"))
        return {
            "as_is": number,
            "lower": number.lower(),
            "nohyphen": number.replace("-", ""),
            "spaced": number.replace("-", " "),
        }[kind]

    # ---- cases --------------------------------------------------------

    def _exact(self, category: CaseCategory) -> tuple[Invoice, Payment]:
        c = self.customer()
        amt = self.amount()
        when = self.pay_time()
        inv = self.invoice(c, amt, issued=self.issue_date(when))
        pay = self.payment(
            amount=amt,
            payer_name=self.maybe_variant(c, 0.3),
            payer_email=c.email,
            reference=self.reference_variant(inv.number),
            when=when,
        )
        self.expect(pay, category, Resolution.AUTO_MATCH, [(inv.invoice_id, amt)])
        return inv, pay

    def case_exact_reference(self) -> None:
        self._exact(CaseCategory.EXACT_REFERENCE)

    def case_noisy_reference(self) -> None:
        c = self.customer()
        amt = self.amount()
        when = self.pay_time()
        inv = self.invoice(c, amt, issued=self.issue_date(when))
        template = self.rng.choice(("Payment for {n}", "{n} thanks!", "inv {n} - Sept", "Re: {n}"))
        pay = self.payment(
            amount=amt,
            payer_name=c.name,
            payer_email=c.email,
            reference=template.format(n=inv.number),
            when=when,
        )
        self.expect(pay, CaseCategory.NOISY_REFERENCE, Resolution.AUTO_MATCH, [(inv.invoice_id, amt)])

    def case_missing_reference_unique(self) -> None:
        c = self.customer()
        amt = self.amount()
        when = self.pay_time()
        inv = self.invoice(c, amt, issued=self.issue_date(when))
        self._amount_only_invoices.append(inv)
        pay = self.payment(
            amount=amt,
            payer_name=c.name,
            payer_email=c.email,
            reference=None,
            note=self.rng.choice(("", "payment", "thanks")),
            when=when,
        )
        self.expect(pay, CaseCategory.MISSING_REFERENCE_UNIQUE, Resolution.AUTO_MATCH, [(inv.invoice_id, amt)])

    def case_partial_payment(self) -> None:
        c = self.customer()
        total = self.amount(600, 4800)
        when = self.pay_time()
        inv = self.invoice(c, total, issued=self.issue_date(when))
        part = 0
        for _ in range(1000):
            frac = self.rng.choice((0.25, 0.3, 0.4, 0.5, 0.6))
            candidate = (int(total * frac) // 100) * 100 + self.rng.choice((0, 0, 50))
            if 0 < candidate < total and self.reserve(candidate):
                part = candidate
                break
        if not part:
            raise RuntimeError("could not draw a partial amount")
        pay = self.payment(
            amount=part,
            payer_name=c.name,
            payer_email=c.email,
            reference=inv.number,
            note=self.rng.choice(("Deposit", "Partial payment", "1st installment")),
            when=when,
        )
        self.expect(pay, CaseCategory.PARTIAL_PAYMENT, Resolution.AUTO_MATCH, [(inv.invoice_id, part)])

    def _grouped(self, category: CaseCategory, with_note: bool) -> None:
        c = self.customer()
        when = self.pay_time()
        for _ in range(1000):
            k = self.rng.choice((2, 3))
            amounts = [self.amount() for _ in range(k)]
            total = sum(amounts)
            if self.reserve(total):
                break
        else:
            raise RuntimeError("could not draw a grouped total")
        invs = [self.invoice(c, a, issued=self.issue_date(when)) for a in amounts]
        note = "Payment for " + ", ".join(i.number for i in invs) if with_note else self.rng.choice(("", "invoices"))
        pay = self.payment(
            amount=total,
            payer_name=c.name,
            payer_email=c.email,
            reference=None,
            note=note,
            when=when,
        )
        self.expect(pay, category, Resolution.AUTO_MATCH, [(i.invoice_id, i.amount_cents) for i in invs])

    def case_grouped_payment_note(self) -> None:
        self._grouped(CaseCategory.GROUPED_PAYMENT_NOTE, with_note=True)

    def case_grouped_payment_no_ref(self) -> None:
        self._grouped(CaseCategory.GROUPED_PAYMENT_NO_REF, with_note=False)

    def case_payer_name_variation(self) -> None:
        x = self.customer()
        y = self.customer(avoid=x.name)  # no shared name tokens, so the variant stays unambiguous
        amt = self.amount()  # deliberately shared by both invoices
        when = self.pay_time()
        ix = self.invoice(x, amt, issued=self.issue_date(when))
        self.invoice(y, amt, issued=self.issue_date(when))
        pay = self.payment(
            amount=amt,
            payer_name=self.name_variant(x),
            payer_email=x.email if self.rng.random() < 0.5 else None,
            reference=None,
            when=when,
        )
        self.expect(pay, CaseCategory.PAYER_NAME_VARIATION, Resolution.AUTO_MATCH, [(ix.invoice_id, amt)])

    def case_ambiguous_same_amount(self) -> None:
        c = self.customer()
        amt = self.amount()
        when = self.pay_time()
        newer_date = self.issue_date(when)
        older = self.invoice(c, amt, issued=newer_date - timedelta(days=30), memo="Monthly retainer, month 1")
        self.invoice(c, amt, issued=newer_date, memo="Monthly retainer, month 2")
        pay = self.payment(amount=amt, payer_name=c.name, payer_email=c.email, reference=None, when=when)
        # The older invoice is the generator's "real" answer, used to simulate a human reviewer.
        self.expect(pay, CaseCategory.AMBIGUOUS_SAME_AMOUNT, Resolution.NEEDS_HUMAN, [(older.invoice_id, amt)])

    def case_ambiguous_combination(self) -> None:
        """Payment S; open invoices a, b, h, h with a + b = h + h = S.

        Both {a, b} and {h, h} total S, so the sum alone cannot decide.
        The generator records {a, b} as the real answer for reviewer simulation.
        """
        c = self.customer()
        when = self.pay_time()
        for _ in range(1000):
            h_dollars = self.rng.randrange(300, 1800)
            h = h_dollars * 100
            delta = self.rng.randrange(50, h_dollars - 50) * 100
            a, b, s = h + delta, h - delta, 2 * h
            if self.reserve(a, b, h, s):
                break
        else:
            raise RuntimeError("could not draw a combination case")
        ia = self.invoice(c, a, issued=self.issue_date(when))
        ib = self.invoice(c, b, issued=self.issue_date(when))
        self.invoice(c, h, issued=self.issue_date(when))
        self.invoice(c, h, issued=self.issue_date(when))
        pay = self.payment(amount=s, payer_name=c.name, payer_email=c.email, reference=None, when=when)
        self.expect(
            pay,
            CaseCategory.AMBIGUOUS_COMBINATION,
            Resolution.NEEDS_HUMAN,
            [(ia.invoice_id, a), (ib.invoice_id, b)],
        )

    def case_duplicate_event(self) -> None:
        _inv, pay = self._exact(CaseCategory.DUPLICATE_EVENT)
        self.payments.append(pay)  # webhook retry: identical delivery

    def case_double_payment(self) -> None:
        inv, first = self._exact(CaseCategory.DOUBLE_PAYMENT)
        second = self.payment(
            amount=first.amount_cents,
            payer_name=first.payer_name,
            payer_email=first.payer_email,
            reference=self.reference_variant(inv.number),
            when=first.received_at + timedelta(days=self.rng.randrange(1, 6)),
        )
        self.expect(second, CaseCategory.DOUBLE_PAYMENT, Resolution.NEEDS_HUMAN, [])

    def case_refunded(self) -> None:
        c = self.customer()
        amt = self.amount()
        when = self.pay_time()
        inv = self.invoice(c, amt, issued=self.issue_date(when))
        pay = self.payment(
            amount=amt,
            payer_name=c.name,
            payer_email=c.email,
            reference=inv.number,
            when=when,
            status=PaymentStatus.REFUNDED,
        )
        self.expect(pay, CaseCategory.REFUNDED, Resolution.NO_ACTION, [])

    def case_unrelated_payment(self) -> None:
        payer = self.customer()
        amt = self.amount()
        reference = self.rng.choice((None, f"INV-{9000 + self.rng.randrange(900)}", "thanks!"))
        pay = self.payment(
            amount=amt,
            payer_name=payer.name,
            payer_email=payer.email,
            reference=reference,
            when=self.pay_time(),
        )
        self.expect(pay, CaseCategory.UNRELATED_PAYMENT, Resolution.NEEDS_HUMAN, [])

    def case_amount_collision(self) -> None:
        x = self.customer()
        z = self.customer(avoid=x.name)
        amt = self.amount()
        when = self.pay_time()
        self.invoice(x, amt, issued=self.issue_date(when))  # stays open and unpaid
        pay = self.payment(
            amount=amt,
            payer_name=z.name,
            payer_email=z.email,
            reference=None,
            note=self.rng.choice(("Consulting fee", "Thanks", "Order 4471")),
            when=when,
        )
        # Nothing ties the stranger to x's invoice: the equal amount is a coincidence.
        self.expect(pay, CaseCategory.AMOUNT_COLLISION, Resolution.NEEDS_HUMAN, [])

    # ---- extras -------------------------------------------------------

    def add_unpaid_fillers(self, count: int) -> None:
        for _ in range(count):
            c = self.customer()
            issued = (_START - timedelta(days=self.rng.randrange(0, 30))).date()
            self.invoice(c, self.amount(), issued=issued)

    def add_closed_distractors(self, count: int) -> None:
        """Closed invoices whose amount equals an amount-only case's invoice.
        A matcher that ignores invoice status will see a false tie."""
        for i, target in enumerate(self._amount_only_invoices[:count]):
            z = self.customer()
            status = InvoiceStatus.PAID if i % 2 == 0 else InvoiceStatus.CANCELLED
            self.invoice(
                z,
                target.amount_cents,
                issued=target.issued_on - timedelta(days=40),
                status=status,
                memo="closed distractor",
            )

    def build(self, mix: Mapping[CaseCategory, int], fillers: int, distractors: int) -> Dataset:
        handlers = {
            CaseCategory.EXACT_REFERENCE: self.case_exact_reference,
            CaseCategory.NOISY_REFERENCE: self.case_noisy_reference,
            CaseCategory.MISSING_REFERENCE_UNIQUE: self.case_missing_reference_unique,
            CaseCategory.PARTIAL_PAYMENT: self.case_partial_payment,
            CaseCategory.GROUPED_PAYMENT_NOTE: self.case_grouped_payment_note,
            CaseCategory.GROUPED_PAYMENT_NO_REF: self.case_grouped_payment_no_ref,
            CaseCategory.PAYER_NAME_VARIATION: self.case_payer_name_variation,
            CaseCategory.AMBIGUOUS_SAME_AMOUNT: self.case_ambiguous_same_amount,
            CaseCategory.AMBIGUOUS_COMBINATION: self.case_ambiguous_combination,
            CaseCategory.DUPLICATE_EVENT: self.case_duplicate_event,
            CaseCategory.DOUBLE_PAYMENT: self.case_double_payment,
            CaseCategory.REFUNDED: self.case_refunded,
            CaseCategory.UNRELATED_PAYMENT: self.case_unrelated_payment,
            CaseCategory.AMOUNT_COLLISION: self.case_amount_collision,
        }
        for category in CaseCategory:
            for _ in range(mix.get(category, 0)):
                handlers[category]()
        self.add_unpaid_fillers(fillers)
        self.add_closed_distractors(distractors)
        payments = tuple(sorted(self.payments, key=lambda p: (p.received_at, p.payment_id)))
        invoices = tuple(sorted(self.invoices, key=lambda i: i.invoice_id))
        return Dataset(seed=-1, invoices=invoices, payments=payments, truth=dict(self.truth))


def generate_dataset(
    seed: int = DEFAULT_SEED,
    mix: Optional[Mapping[CaseCategory, int]] = None,
    unpaid_fillers: int = 15,
    closed_distractors: int = 6,
) -> Dataset:
    built = _Builder(seed).build(mix if mix is not None else DEFAULT_MIX, unpaid_fillers, closed_distractors)
    return Dataset(seed=seed, invoices=built.invoices, payments=built.payments, truth=built.truth)

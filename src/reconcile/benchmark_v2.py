"""Benchmark v2: Comprehensive 14-Scenario Financial Reconciliation Evaluation Suite.

Independent quantitative evaluation framework designed to benchmark:
1. Baseline Matcher (amount-only allowed)
2. Baseline Strict (amount-only disabled)
3. Deterministic Matcher v1 (T1-T3)
4. Advanced Matcher D5 (T1-T6)

Evaluates precision, recall, F1, false positive rate, ambiguity accuracy,
overpayment resolution, capacity invariants, and double-crediting prevention
across deterministic train and holdout splits.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from enum import Enum
import random
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from .interfaces import Matcher
from .models import (
    AllocationStatus,
    Candidate,
    EvidenceCode,
    Invoice,
    InvoiceStatus,
    MatchOutcome,
    MatchProposal,
    Payment,
    PaymentAllocation,
    PaymentStatus,
    Refund,
    outstanding_cents,
)


class ScenarioGroup(str, Enum):
    EXACT_SINGLE = "EXACT_SINGLE"
    CONTEXTUAL_MATCH = "CONTEXTUAL_MATCH"
    PARTIAL_PAYMENT = "PARTIAL_PAYMENT"
    SEQUENTIAL_PARTIAL = "SEQUENTIAL_PARTIAL"
    SPLIT_INSTALLMENTS = "SPLIT_INSTALLMENTS"
    GROUPED_PAYMENT = "GROUPED_PAYMENT"
    OVERPAYMENT = "OVERPAYMENT"
    DUPLICATE_DELIVERY = "DUPLICATE_DELIVERY"
    AMBIGUOUS_COLLISION = "AMBIGUOUS_COLLISION"
    AMOUNT_ONLY_UNREFERENCED = "AMOUNT_ONLY_UNREFERENCED"
    FULL_REFUND = "FULL_REFUND"
    PARTIAL_REFUND = "PARTIAL_REFUND"
    CURRENCY_MISMATCH = "CURRENCY_MISMATCH"
    CONFLICTING_EVIDENCE = "CONFLICTING_EVIDENCE"


@dataclass(frozen=True)
class GroundTruthV2:
    payment_id: str
    scenario_group: ScenarioGroup
    expected_outcome: MatchOutcome
    expected_allocations: tuple[tuple[str, int], ...] = ()  # (invoice_id, amount_cents)
    expected_excess_cents: int = 0
    is_ambiguous: bool = False


@dataclass(frozen=True)
class BenchmarkV2Dataset:
    seed: int
    split: str
    invoices: tuple[Invoice, ...]
    payments: tuple[Payment, ...]
    refunds: tuple[Refund, ...] = ()
    approved_allocations: tuple[PaymentAllocation, ...] = ()
    provider_settlements: tuple[PaymentAllocation, ...] = ()
    truth: Mapping[str, GroundTruthV2] = field(default_factory=dict)


def generate_benchmark_v2(seed: int, split: str = "train") -> BenchmarkV2Dataset:
    """Generate a reproducible, deterministic Benchmark v2 dataset with all 14 scenario groups."""
    rng = random.Random(seed + (1000 if split == "holdout" else 0))

    base_date = date(2026, 11, 1)
    base_time = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)

    invoices: list[Invoice] = []
    payments: list[Payment] = []
    refunds: list[Refund] = []
    approved: list[PaymentAllocation] = []
    settlements: list[PaymentAllocation] = []
    truth: dict[str, GroundTruthV2] = {}

    inv_counter = 1
    pay_counter = 1

    def next_inv(
        amount_cents: int,
        customer_name: str,
        customer_email: str = "",
        currency: str = "USD",
        days_offset: int = 0,
        status: InvoiceStatus = InvoiceStatus.SENT,
    ) -> Invoice:
        nonlocal inv_counter
        idx = inv_counter
        inv_counter += 1
        num = f"INV-V2-{idx:04d}"
        inv_id = f"inv_v2_{idx:04d}"
        iss = base_date + timedelta(days=days_offset)
        due = iss + timedelta(days=14)
        return Invoice(
            invoice_id=inv_id,
            number=num,
            customer_name=customer_name,
            customer_email=customer_email or f"user{idx}@example.com",
            amount_cents=amount_cents,
            currency=currency,
            issued_on=iss,
            due_on=due,
            status=status,
        )

    def next_pay(
        amount_cents: int,
        reference: str = "",
        payer_name: str = "",
        payer_email: str = "",
        note: str = "",
        currency: str = "USD",
        days_offset: int = 2,
        status: PaymentStatus = PaymentStatus.COMPLETED,
    ) -> Payment:
        nonlocal pay_counter
        idx = pay_counter
        pay_counter += 1
        pid = f"pay_v2_{idx:04d}"
        p_time = base_time + timedelta(days=days_offset, minutes=idx)
        return Payment(
            payment_id=pid,
            amount_cents=amount_cents,
            currency=currency,
            received_at=p_time,
            payer_name=payer_name or f"Payer {idx}",
            payer_email=payer_email or f"payer{idx}@example.com",
            reference=reference,
            note=note,
            status=status,
        )

    # 1. EXACT_SINGLE: Exact reference match (T1)
    inv1 = next_inv(45000, "Apex Corp")
    invoices.append(inv1)
    p1 = next_pay(45000, reference=inv1.number, payer_name="Apex Corp")
    payments.append(p1)
    truth[p1.payment_id] = GroundTruthV2(
        payment_id=p1.payment_id,
        scenario_group=ScenarioGroup.EXACT_SINGLE,
        expected_outcome=MatchOutcome.PROPOSED,
        expected_allocations=((inv1.invoice_id, 45000),),
    )

    # 2. CONTEXTUAL_MATCH: Strong multi-signal contextual match (customer name + email + temporal) (T4)
    inv2 = next_inv(62000, "BioGen Technologies", "accounting@biogen.com", days_offset=1)
    invoices.append(inv2)
    p2 = next_pay(
        62000,
        reference="",
        payer_name="BioGen Technologies",
        payer_email="accounting@biogen.com",
        note="Vendor payment for supplies",
        days_offset=3,
    )
    payments.append(p2)
    truth[p2.payment_id] = GroundTruthV2(
        payment_id=p2.payment_id,
        scenario_group=ScenarioGroup.CONTEXTUAL_MATCH,
        expected_outcome=MatchOutcome.PROPOSED,
        expected_allocations=((inv2.invoice_id, 62000),),
    )

    # 3. PARTIAL_PAYMENT: Partial payment with valid reference (T5)
    inv3 = next_inv(100000, "Catalyst Labs")
    invoices.append(inv3)
    p3 = next_pay(35000, reference=inv3.number, payer_name="Catalyst Labs")
    payments.append(p3)
    truth[p3.payment_id] = GroundTruthV2(
        payment_id=p3.payment_id,
        scenario_group=ScenarioGroup.PARTIAL_PAYMENT,
        expected_outcome=MatchOutcome.PROPOSED,
        expected_allocations=((inv3.invoice_id, 35000),),
    )

    # 4. SEQUENTIAL_PARTIAL: Subsequent payment against an already partially paid invoice (T5)
    inv4 = next_inv(80000, "Delta Dynamics")
    invoices.append(inv4)
    alloc_prior = PaymentAllocation(
        payment_id="pay_prior_1",
        invoice_id=inv4.invoice_id,
        amount_cents=30000,
        status=AllocationStatus.APPROVED,
        currency="USD",
    )
    approved.append(alloc_prior)
    # Remaining capacity is 50,000 cents
    p4 = next_pay(50000, reference=inv4.number, payer_name="Delta Dynamics")
    payments.append(p4)
    truth[p4.payment_id] = GroundTruthV2(
        payment_id=p4.payment_id,
        scenario_group=ScenarioGroup.SEQUENTIAL_PARTIAL,
        expected_outcome=MatchOutcome.PROPOSED,
        expected_allocations=((inv4.invoice_id, 50000),),
    )

    # 5. SPLIT_INSTALLMENTS: First installment of split payments
    inv5 = next_inv(90000, "Echo Enterprises")
    invoices.append(inv5)
    p5 = next_pay(45000, reference=inv5.number, payer_name="Echo Enterprises", note="Installment 1 of 2")
    payments.append(p5)
    truth[p5.payment_id] = GroundTruthV2(
        payment_id=p5.payment_id,
        scenario_group=ScenarioGroup.SPLIT_INSTALLMENTS,
        expected_outcome=MatchOutcome.PROPOSED,
        expected_allocations=((inv5.invoice_id, 45000),),
    )

    # 6. GROUPED_PAYMENT: Single payment referencing two open invoices (T3/T5)
    inv6a = next_inv(25000, "Falcon Freight")
    inv6b = next_inv(35000, "Falcon Freight")
    invoices.extend([inv6a, inv6b])
    p6 = next_pay(60000, reference=f"{inv6a.number}, {inv6b.number}", payer_name="Falcon Freight")
    payments.append(p6)
    truth[p6.payment_id] = GroundTruthV2(
        payment_id=p6.payment_id,
        scenario_group=ScenarioGroup.GROUPED_PAYMENT,
        expected_outcome=MatchOutcome.PROPOSED,
        expected_allocations=((inv6a.invoice_id, 25000), (inv6b.invoice_id, 35000)),
    )

    # 7. OVERPAYMENT: Payment exceeding invoice capacity, allocates max and reports excess (T5)
    inv7 = next_inv(50000, "Gamma Global")
    invoices.append(inv7)
    p7 = next_pay(65000, reference=inv7.number, payer_name="Gamma Global")
    payments.append(p7)
    truth[p7.payment_id] = GroundTruthV2(
        payment_id=p7.payment_id,
        scenario_group=ScenarioGroup.OVERPAYMENT,
        expected_outcome=MatchOutcome.PROPOSED,
        expected_allocations=((inv7.invoice_id, 50000),),
        expected_excess_cents=15000,
    )

    # 8. DUPLICATE_DELIVERY: Replayed webhook event
    p8_orig = next_pay(33000, reference="INV-V2-0001", payer_name="Duplicate Payer")
    p8_dupe = Payment(
        payment_id=p8_orig.payment_id,
        amount_cents=p8_orig.amount_cents,
        currency=p8_orig.currency,
        received_at=p8_orig.received_at + timedelta(minutes=5),
        payer_name=p8_orig.payer_name,
        reference=p8_orig.reference,
        status=PaymentStatus.COMPLETED,
    )
    payments.extend([p8_orig, p8_dupe])
    truth[p8_dupe.payment_id] = GroundTruthV2(
        payment_id=p8_dupe.payment_id,
        scenario_group=ScenarioGroup.DUPLICATE_DELIVERY,
        expected_outcome=MatchOutcome.NO_MATCH,
    )

    # 9. AMBIGUOUS_COLLISION: Two identical open invoices for $400 without differentiating reference (T6)
    inv9a = next_inv(40000, "Ambiguous Customer A")
    inv9b = next_inv(40000, "Ambiguous Customer B")
    invoices.extend([inv9a, inv9b])
    p9 = next_pay(40000, reference="", payer_name="Unidentified Buyer")
    payments.append(p9)
    truth[p9.payment_id] = GroundTruthV2(
        payment_id=p9.payment_id,
        scenario_group=ScenarioGroup.AMBIGUOUS_COLLISION,
        expected_outcome=MatchOutcome.AMBIGUOUS,
        is_ambiguous=True,
    )

    # 10. AMOUNT_ONLY_UNREFERENCED: Unreferenced payment with insufficient context -> NO_MATCH (T6)
    inv10 = next_inv(55000, "Isolated Corp", days_offset=-60)
    invoices.append(inv10)
    p10 = next_pay(55000, reference="", payer_name="Stranger Without Account", days_offset=20)
    payments.append(p10)
    truth[p10.payment_id] = GroundTruthV2(
        payment_id=p10.payment_id,
        scenario_group=ScenarioGroup.AMOUNT_ONLY_UNREFERENCED,
        expected_outcome=MatchOutcome.NO_MATCH,
    )

    # 11. FULL_REFUND: Approved payment fully refunded, restoring invoice balance (T6)
    inv11 = next_inv(70000, "Helix Healthcare")
    invoices.append(inv11)
    alloc_ref = PaymentAllocation(
        payment_id="pay_ref_parent_1",
        invoice_id=inv11.invoice_id,
        amount_cents=70000,
        status=AllocationStatus.APPROVED,
        currency="USD",
    )
    approved.append(alloc_ref)
    ref11 = Refund(
        refund_id="ref_v2_1",
        parent_payment_id="pay_ref_parent_1",
        amount_cents=70000,
        currency="USD",
        refunded_at=base_time + timedelta(days=5),
        invoice_id=inv11.invoice_id,
    )
    refunds.append(ref11)
    # Customer pays again after refund
    p11 = next_pay(70000, reference=inv11.number, payer_name="Helix Healthcare", days_offset=6)
    payments.append(p11)
    truth[p11.payment_id] = GroundTruthV2(
        payment_id=p11.payment_id,
        scenario_group=ScenarioGroup.FULL_REFUND,
        expected_outcome=MatchOutcome.PROPOSED,
        expected_allocations=((inv11.invoice_id, 70000),),
    )

    # 12. PARTIAL_REFUND: Partial refund restoring net balance (T6)
    inv12 = next_inv(100000, "Infinity Innovations")
    invoices.append(inv12)
    alloc_pref = PaymentAllocation(
        payment_id="pay_pref_parent_1",
        invoice_id=inv12.invoice_id,
        amount_cents=100000,
        status=AllocationStatus.APPROVED,
        currency="USD",
    )
    approved.append(alloc_pref)
    ref12 = Refund(
        refund_id="ref_v2_2",
        parent_payment_id="pay_pref_parent_1",
        amount_cents=30000,
        currency="USD",
        refunded_at=base_time + timedelta(days=7),
        invoice_id=inv12.invoice_id,
    )
    refunds.append(ref12)
    # Remaining capacity restored to 30,000
    p12 = next_pay(30000, reference=inv12.number, payer_name="Infinity Innovations", days_offset=8)
    payments.append(p12)
    truth[p12.payment_id] = GroundTruthV2(
        payment_id=p12.payment_id,
        scenario_group=ScenarioGroup.PARTIAL_REFUND,
        expected_outcome=MatchOutcome.PROPOSED,
        expected_allocations=((inv12.invoice_id, 30000),),
    )

    # 13. CURRENCY_MISMATCH: Payment in EUR against USD invoice (T6)
    inv13 = next_inv(48000, "Jovian Holdings", currency="USD")
    invoices.append(inv13)
    p13 = next_pay(48000, reference=inv13.number, payer_name="Jovian Holdings", currency="EUR")
    payments.append(p13)
    truth[p13.payment_id] = GroundTruthV2(
        payment_id=p13.payment_id,
        scenario_group=ScenarioGroup.CURRENCY_MISMATCH,
        expected_outcome=MatchOutcome.NO_MATCH,
    )

    # 14. CONFLICTING_EVIDENCE: Reference to non-existent invoice (T6)
    p14 = next_pay(99000, reference="INV-NON-EXISTENT-9999", payer_name="Lost Customer")
    payments.append(p14)
    truth[p14.payment_id] = GroundTruthV2(
        payment_id=p14.payment_id,
        scenario_group=ScenarioGroup.CONFLICTING_EVIDENCE,
        expected_outcome=MatchOutcome.NO_MATCH,
    )

    return BenchmarkV2Dataset(
        seed=seed,
        split=split,
        invoices=tuple(invoices),
        payments=tuple(payments),
        refunds=tuple(refunds),
        approved_allocations=tuple(approved),
        provider_settlements=tuple(settlements),
        truth=truth,
    )


@dataclass(frozen=True)
class BenchmarkV2Metrics:
    matcher_name: str
    total_cases: int
    true_positives: int
    false_positives: int
    false_negatives: int
    true_negatives: int
    ambiguity_accuracy: float
    overpayment_accuracy: float
    capacity_violations: int
    double_crediting_violations: int
    precision: float
    recall: float
    f1_score: float
    accuracy: float
    scenario_breakdown: Dict[str, Dict[str, Any]] = field(default_factory=dict)


def evaluate_matcher_v2(matcher: Matcher, dataset: BenchmarkV2Dataset) -> BenchmarkV2Metrics:
    """Evaluate a matcher against Benchmark v2 dataset."""
    import inspect
    matcher_sig = inspect.signature(matcher.match)
    kwargs: dict[str, Any] = {"approved": dataset.approved_allocations}
    if "settlements" in matcher_sig.parameters:
        kwargs["settlements"] = dataset.provider_settlements
    if "refunds" in matcher_sig.parameters:
        kwargs["refunds"] = dataset.refunds

    proposals = matcher.match(dataset.payments, dataset.invoices, **kwargs)
    prop_by_pay: dict[str, MatchProposal] = {p.payment_id: p for p in proposals}

    tp = 0
    fp = 0
    fn = 0
    tn = 0
    ambiguity_matches = 0
    ambiguity_total = 0
    overpayment_matches = 0
    overpayment_total = 0
    capacity_violations = 0
    double_crediting_violations = 0

    scenario_stats: dict[str, dict[str, int]] = {}

    # Check capacity constraints
    inv_by_id = {inv.invoice_id: inv for inv in dataset.invoices}
    deductions = list(dataset.approved_allocations) + list(dataset.provider_settlements)
    initial_balances = {
        inv.invoice_id: outstanding_cents(inv, deductions, refunds=dataset.refunds)
        for inv in dataset.invoices
    }

    allocated_per_inv: dict[str, int] = {inv.invoice_id: 0 for inv in dataset.invoices}
    allocated_per_pay: set[str] = set()

    for pid, gt in dataset.truth.items():
        sc = gt.scenario_group.value
        if sc not in scenario_stats:
            scenario_stats[sc] = {"total": 0, "correct": 0}
        scenario_stats[sc]["total"] += 1

        prop = prop_by_pay.get(pid)
        if prop is None:
            fn += 1
            continue

        outcome = prop.outcome
        is_correct = False

        if gt.expected_excess_cents > 0:
            overpayment_total += 1

        if gt.expected_outcome is MatchOutcome.PROPOSED:
            if outcome is MatchOutcome.PROPOSED:
                cand = prop.candidates[0] if prop.candidates else None
                if cand:
                    alloc_tuples = tuple(
                        sorted((a.invoice_id, a.amount_cents) for a in cand.allocations)
                    )
                    expected_tuples = tuple(sorted(gt.expected_allocations))
                    if alloc_tuples == expected_tuples:
                        is_correct = True
                        tp += 1
                        # Check overpayment match
                        if gt.expected_excess_cents > 0:
                            if getattr(prop, "excess_amount_cents", 0) == gt.expected_excess_cents:
                                overpayment_matches += 1
                    else:
                        fp += 1
                else:
                    fp += 1
            else:
                fn += 1
        elif gt.expected_outcome is MatchOutcome.AMBIGUOUS:
            ambiguity_total += 1
            if outcome is MatchOutcome.AMBIGUOUS:
                is_correct = True
                tn += 1
                ambiguity_matches += 1
            elif outcome is MatchOutcome.PROPOSED:
                # Proposing a guess in an ambiguous situation is a severe FALSE POSITIVE!
                fp += 1
            else:
                tn += 1
        else:
            # Expected NO_MATCH
            if outcome is MatchOutcome.NO_MATCH or outcome is MatchOutcome.PROVIDER_SETTLED:
                is_correct = True
                tn += 1
            elif outcome is MatchOutcome.PROPOSED:
                # Proposing an allocation when no match should occur is a FALSE POSITIVE!
                fp += 1
            else:
                tn += 1

        if is_correct:
            scenario_stats[sc]["correct"] += 1

        # Check invariant violations
        if outcome is MatchOutcome.PROPOSED and prop.candidates:
            if pid in allocated_per_pay:
                double_crediting_violations += 1
            allocated_per_pay.add(pid)

            for a in prop.candidates[0].allocations:
                allocated_per_inv[a.invoice_id] = allocated_per_inv.get(a.invoice_id, 0) + a.amount_cents
                if allocated_per_inv[a.invoice_id] > initial_balances.get(a.invoice_id, 0):
                    capacity_violations += 1

    total_cases = len(dataset.truth)
    precision = (tp / (tp + fp)) if (tp + fp) > 0 else 1.0
    recall = (tp / (tp + fn)) if (tp + fn) > 0 else 0.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) > 0 else 0.0
    acc = ((tp + tn) / total_cases) if total_cases > 0 else 0.0
    ambig_acc = (ambiguity_matches / ambiguity_total) if ambiguity_total > 0 else 1.0
    over_acc = (overpayment_matches / overpayment_total) if overpayment_total > 0 else 0.0

    return BenchmarkV2Metrics(
        matcher_name=matcher.name,
        total_cases=total_cases,
        true_positives=tp,
        false_positives=fp,
        false_negatives=fn,
        true_negatives=tn,
        ambiguity_accuracy=ambig_acc,
        overpayment_accuracy=over_acc,
        capacity_violations=capacity_violations,
        double_crediting_violations=double_crediting_violations,
        precision=precision,
        recall=recall,
        f1_score=f1,
        accuracy=acc,
        scenario_breakdown=scenario_stats,
    )


def format_benchmark_v2_table(metrics_list: Sequence[BenchmarkV2Metrics]) -> str:
    """Format benchmark evaluation metrics into a clean, markdown-compatible comparison table."""
    headers = [
        "Matcher",
        "Cases",
        "TP",
        "FP",
        "FN",
        "TN",
        "Precision",
        "Recall",
        "F1",
        "Ambiguity",
        "Overpay",
        "Cap Viol",
        "Dbl Cred",
    ]
    rows = [headers]
    for m in metrics_list:
        rows.append([
            m.matcher_name,
            str(m.total_cases),
            str(m.true_positives),
            str(m.false_positives),
            str(m.false_negatives),
            str(m.true_negatives),
            f"{m.precision:.1%}",
            f"{m.recall:.1%}",
            f"{m.f1_score:.1%}",
            f"{m.ambiguity_accuracy:.1%}",
            f"{m.overpayment_accuracy:.1%}",
            str(m.capacity_violations),
            str(m.double_crediting_violations),
        ])
    col_widths = [max(len(row[i]) for row in rows) for i in range(len(headers))]
    lines = []
    header_line = " | ".join(val.ljust(col_widths[i]) for i, val in enumerate(rows[0]))
    separator_line = "-+-".join("-" * col_widths[i] for i in range(len(headers)))
    lines.append(header_line)
    lines.append(separator_line)
    for row in rows[1:]:
        lines.append(" | ".join(val.ljust(col_widths[i]) for i, val in enumerate(row)))
    return "\n".join(lines)

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
    PaymentReversal,
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
    reversals: tuple[PaymentReversal, ...] = ()
    approved_allocations: tuple[PaymentAllocation, ...] = ()
    provider_settlements: tuple[PaymentAllocation, ...] = ()
    truth: Mapping[str, GroundTruthV2] = field(default_factory=dict)


def generate_benchmark_v2(
    seed: int, split: str = "train", cases_per_group: int = 8
) -> BenchmarkV2Dataset:
    """Generate a reproducible, deterministic Benchmark v2 dataset with all 14 scenario groups."""
    rng = random.Random(seed + (1000 if split == "holdout" else 0))

    base_date = date(2026, 11, 1)
    base_time = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
    prefix = "TRN" if split == "train" else "HLD"

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
        num = f"INV-V2-{prefix}-{idx:04d}"
        inv_id = f"inv_v2_{prefix.lower()}_{idx:04d}"
        iss = base_date + timedelta(days=days_offset)
        due = iss + timedelta(days=14)
        return Invoice(
            invoice_id=inv_id,
            number=num,
            customer_name=customer_name,
            customer_email=customer_email or f"user_{prefix.lower()}_{idx}@example.com",
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
        pid = f"pay_v2_{prefix.lower()}_{idx:04d}"
        p_time = base_time + timedelta(days=days_offset, minutes=idx)
        return Payment(
            payment_id=pid,
            amount_cents=amount_cents,
            currency=currency,
            received_at=p_time,
            payer_name=payer_name or f"Payer {prefix} {idx}",
            payer_email=payer_email or f"payer_{prefix.lower()}_{idx}@example.com",
            reference=reference,
            note=note,
            status=status,
        )

    # 1. EXACT_SINGLE: Exact reference match (T1)
    for k in range(cases_per_group):
        amt = 30000 + k * 12500
        inv = next_inv(amt, f"Apex Corp {prefix} {k+1}")
        invoices.append(inv)
        p = next_pay(amt, reference=inv.number, payer_name=inv.customer_name)
        payments.append(p)
        truth[p.payment_id] = GroundTruthV2(
            payment_id=p.payment_id,
            scenario_group=ScenarioGroup.EXACT_SINGLE,
            expected_outcome=MatchOutcome.PROPOSED,
            expected_allocations=((inv.invoice_id, amt),),
        )

    # 2. CONTEXTUAL_MATCH: Strong multi-signal contextual match (customer name + email + temporal) (T4)
    for k in range(cases_per_group):
        amt = 40000 + k * 8500
        cust_name = f"BioGen Tech {prefix} {k+1}"
        cust_email = f"accounting_{prefix.lower()}_{k+1}@biogen.com"
        inv = next_inv(amt, cust_name, cust_email, days_offset=k)
        invoices.append(inv)
        p = next_pay(
            amt,
            reference="",
            payer_name=cust_name,
            payer_email=cust_email,
            note=f"Vendor supplies payment batch {k+1}",
            days_offset=k + 2,
        )
        payments.append(p)
        truth[p.payment_id] = GroundTruthV2(
            payment_id=p.payment_id,
            scenario_group=ScenarioGroup.CONTEXTUAL_MATCH,
            expected_outcome=MatchOutcome.PROPOSED,
            expected_allocations=((inv.invoice_id, amt),),
        )

    # 3. PARTIAL_PAYMENT: Partial payment with valid reference (T5)
    for k in range(cases_per_group):
        inv_amt = 80000 + k * 15000
        pay_amt = 30000 + k * 5000
        inv = next_inv(inv_amt, f"Catalyst Labs {prefix} {k+1}")
        invoices.append(inv)
        p = next_pay(pay_amt, reference=inv.number, payer_name=inv.customer_name)
        payments.append(p)
        truth[p.payment_id] = GroundTruthV2(
            payment_id=p.payment_id,
            scenario_group=ScenarioGroup.PARTIAL_PAYMENT,
            expected_outcome=MatchOutcome.PROPOSED,
            expected_allocations=((inv.invoice_id, pay_amt),),
        )

    # 4. SEQUENTIAL_PARTIAL: Subsequent payment against an already partially paid invoice (T5)
    for k in range(cases_per_group):
        inv_amt = 90000 + k * 10000
        prior_amt = 40000 + k * 3000
        rem_amt = inv_amt - prior_amt
        inv = next_inv(inv_amt, f"Delta Dynamics {prefix} {k+1}")
        invoices.append(inv)
        alloc_prior = PaymentAllocation(
            payment_id=f"pay_prior_{prefix.lower()}_{k+1}",
            invoice_id=inv.invoice_id,
            amount_cents=prior_amt,
            status=AllocationStatus.APPROVED,
            currency="USD",
        )
        approved.append(alloc_prior)
        p = next_pay(rem_amt, reference=inv.number, payer_name=inv.customer_name)
        payments.append(p)
        truth[p.payment_id] = GroundTruthV2(
            payment_id=p.payment_id,
            scenario_group=ScenarioGroup.SEQUENTIAL_PARTIAL,
            expected_outcome=MatchOutcome.PROPOSED,
            expected_allocations=((inv.invoice_id, rem_amt),),
        )

    # 5. SPLIT_INSTALLMENTS: First installment of split payments
    for k in range(cases_per_group):
        inv_amt = 60000 + k * 20000
        inst_amt = inv_amt // 2
        inv = next_inv(inv_amt, f"Echo Enterprises {prefix} {k+1}")
        invoices.append(inv)
        p = next_pay(inst_amt, reference=inv.number, payer_name=inv.customer_name, note="Installment 1 of 2")
        payments.append(p)
        truth[p.payment_id] = GroundTruthV2(
            payment_id=p.payment_id,
            scenario_group=ScenarioGroup.SPLIT_INSTALLMENTS,
            expected_outcome=MatchOutcome.PROPOSED,
            expected_allocations=((inv.invoice_id, inst_amt),),
        )

    # 6. GROUPED_PAYMENT: Single payment referencing two open invoices (T3/T5)
    for k in range(cases_per_group):
        amt_a = 20000 + k * 4000
        amt_b = 30000 + k * 5000
        inv_a = next_inv(amt_a, f"Falcon Freight {prefix} {k+1}")
        inv_b = next_inv(amt_b, f"Falcon Freight {prefix} {k+1}")
        invoices.extend([inv_a, inv_b])
        p = next_pay(amt_a + amt_b, reference=f"{inv_a.number}, {inv_b.number}", payer_name=inv_a.customer_name)
        payments.append(p)
        truth[p.payment_id] = GroundTruthV2(
            payment_id=p.payment_id,
            scenario_group=ScenarioGroup.GROUPED_PAYMENT,
            expected_outcome=MatchOutcome.PROPOSED,
            expected_allocations=((inv_a.invoice_id, amt_a), (inv_b.invoice_id, amt_b)),
        )

    # 7. OVERPAYMENT: Payment exceeding invoice capacity, allocates max and reports excess (T5)
    for k in range(cases_per_group):
        inv_amt = 50000 + k * 10000
        excess_amt = 15000 + k * 2500
        inv = next_inv(inv_amt, f"Gamma Global {prefix} {k+1}")
        invoices.append(inv)
        p = next_pay(inv_amt + excess_amt, reference=inv.number, payer_name=inv.customer_name)
        payments.append(p)
        truth[p.payment_id] = GroundTruthV2(
            payment_id=p.payment_id,
            scenario_group=ScenarioGroup.OVERPAYMENT,
            expected_outcome=MatchOutcome.PROPOSED,
            expected_allocations=((inv.invoice_id, inv_amt),),
            expected_excess_cents=excess_amt,
        )

    # 8. DUPLICATE_DELIVERY: Payment already approved in ledger re-delivered as webhook
    for k in range(cases_per_group):
        amt = 33000 + k * 4000
        inv = next_inv(amt, f"Duplicate Delivery {prefix} {k+1}")
        invoices.append(inv)
        p_dupe = next_pay(amt, reference=inv.number, payer_name=inv.customer_name)
        approved.append(
            PaymentAllocation(
                payment_id=p_dupe.payment_id,
                invoice_id=inv.invoice_id,
                amount_cents=amt,
                status=AllocationStatus.APPROVED,
                currency="USD",
            )
        )
        payments.append(p_dupe)
        truth[p_dupe.payment_id] = GroundTruthV2(
            payment_id=p_dupe.payment_id,
            scenario_group=ScenarioGroup.DUPLICATE_DELIVERY,
            expected_outcome=MatchOutcome.NO_MATCH,
        )

    # 9. AMBIGUOUS_COLLISION: Two identical open invoices without differentiating reference (T6)
    for k in range(cases_per_group):
        amt = 42000 + k * 7000
        inv_a = next_inv(amt, f"Ambiguous Cust A {prefix} {k+1}")
        inv_b = next_inv(amt, f"Ambiguous Cust B {prefix} {k+1}")
        invoices.extend([inv_a, inv_b])
        p = next_pay(amt, reference="", payer_name=f"Anonymous Buyer {prefix} {k+1}")
        payments.append(p)
        truth[p.payment_id] = GroundTruthV2(
            payment_id=p.payment_id,
            scenario_group=ScenarioGroup.AMBIGUOUS_COLLISION,
            expected_outcome=MatchOutcome.AMBIGUOUS,
            is_ambiguous=True,
        )

    # 10. AMOUNT_ONLY_UNREFERENCED: Unreferenced payment with insufficient context -> NO_MATCH (T6)
    for k in range(cases_per_group):
        amt = 55000 + k * 6000
        inv = next_inv(amt, f"Isolated Corp {prefix} {k+1}", days_offset=-60)
        invoices.append(inv)
        p = next_pay(amt, reference="", payer_name=f"Stranger Without Account {prefix} {k+1}", days_offset=20)
        payments.append(p)
        truth[p.payment_id] = GroundTruthV2(
            payment_id=p.payment_id,
            scenario_group=ScenarioGroup.AMOUNT_ONLY_UNREFERENCED,
            expected_outcome=MatchOutcome.NO_MATCH,
        )

    # 11. FULL_REFUND: Approved payment fully refunded, restoring invoice balance (T6)
    for k in range(cases_per_group):
        amt = 70000 + k * 12000
        inv = next_inv(amt, f"Helix Healthcare {prefix} {k+1}")
        invoices.append(inv)
        parent_pid = f"pay_ref_parent_{prefix.lower()}_{k+1}"
        alloc_ref = PaymentAllocation(
            payment_id=parent_pid,
            invoice_id=inv.invoice_id,
            amount_cents=amt,
            status=AllocationStatus.APPROVED,
            currency="USD",
        )
        approved.append(alloc_ref)
        ref = Refund(
            refund_id=f"ref_v2_{prefix.lower()}_{k+1}",
            parent_payment_id=parent_pid,
            amount_cents=amt,
            currency="USD",
            refunded_at=base_time + timedelta(days=5),
            invoice_id=inv.invoice_id,
        )
        refunds.append(ref)
        p = next_pay(amt, reference=inv.number, payer_name=inv.customer_name, days_offset=6)
        payments.append(p)
        truth[p.payment_id] = GroundTruthV2(
            payment_id=p.payment_id,
            scenario_group=ScenarioGroup.FULL_REFUND,
            expected_outcome=MatchOutcome.PROPOSED,
            expected_allocations=((inv.invoice_id, amt),),
        )

    # 12. PARTIAL_REFUND: Partial refund restoring net balance (T6)
    for k in range(cases_per_group):
        inv_amt = 100000 + k * 15000
        ref_amt = 35000 + k * 5000
        inv = next_inv(inv_amt, f"Infinity Innovations {prefix} {k+1}")
        invoices.append(inv)
        parent_pid = f"pay_pref_parent_{prefix.lower()}_{k+1}"
        alloc_pref = PaymentAllocation(
            payment_id=parent_pid,
            invoice_id=inv.invoice_id,
            amount_cents=inv_amt,
            status=AllocationStatus.APPROVED,
            currency="USD",
        )
        approved.append(alloc_pref)
        ref = Refund(
            refund_id=f"ref_v2_part_{prefix.lower()}_{k+1}",
            parent_payment_id=parent_pid,
            amount_cents=ref_amt,
            currency="USD",
            refunded_at=base_time + timedelta(days=7),
            invoice_id=inv.invoice_id,
        )
        refunds.append(ref)
        p = next_pay(ref_amt, reference=inv.number, payer_name=inv.customer_name, days_offset=8)
        payments.append(p)
        truth[p.payment_id] = GroundTruthV2(
            payment_id=p.payment_id,
            scenario_group=ScenarioGroup.PARTIAL_REFUND,
            expected_outcome=MatchOutcome.PROPOSED,
            expected_allocations=((inv.invoice_id, ref_amt),),
        )

    # 13. CURRENCY_MISMATCH: Payment in EUR against USD invoice (T6)
    for k in range(cases_per_group):
        amt = 48000 + k * 5000
        inv = next_inv(amt, f"Jovian Holdings {prefix} {k+1}", currency="USD")
        invoices.append(inv)
        p = next_pay(amt, reference=inv.number, payer_name=inv.customer_name, currency="EUR")
        payments.append(p)
        truth[p.payment_id] = GroundTruthV2(
            payment_id=p.payment_id,
            scenario_group=ScenarioGroup.CURRENCY_MISMATCH,
            expected_outcome=MatchOutcome.NO_MATCH,
        )

    # 14. CONFLICTING_EVIDENCE: Reference to non-existent invoice (T6)
    for k in range(cases_per_group):
        amt = 99000 + k * 2500
        p = next_pay(
            amt,
            reference=f"INV-NONEXISTENT-{prefix}-{k+1:04d}",
            payer_name=f"Lost Customer {prefix} {k+1}",
        )
        payments.append(p)
        truth[p.payment_id] = GroundTruthV2(
            payment_id=p.payment_id,
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
    confusion_matrix: Dict[str, Dict[str, int]] = field(default_factory=dict)


def evaluate_matcher_v2(matcher: Matcher, dataset: BenchmarkV2Dataset) -> BenchmarkV2Metrics:
    """Evaluate a matcher against Benchmark v2 dataset."""
    import inspect
    matcher_sig = inspect.signature(matcher.match)
    kwargs: dict[str, Any] = {"approved": dataset.approved_allocations}
    if "settlements" in matcher_sig.parameters:
        kwargs["settlements"] = dataset.provider_settlements
    if "refunds" in matcher_sig.parameters:
        kwargs["refunds"] = dataset.refunds
    if "reversals" in matcher_sig.parameters:
        kwargs["reversals"] = dataset.reversals

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

    # 3x3 Confusion Matrix: Actual (row) -> Predicted (col)
    outcomes = ["PROPOSED", "AMBIGUOUS", "NO_MATCH"]
    confusion_matrix: dict[str, dict[str, int]] = {
        act: {pred: 0 for pred in outcomes} for act in outcomes
    }

    # Check capacity constraints
    inv_by_id = {inv.invoice_id: inv for inv in dataset.invoices}
    deductions = list(dataset.approved_allocations) + list(dataset.provider_settlements)
    initial_balances = {
        inv.invoice_id: outstanding_cents(
            inv, deductions, refunds=dataset.refunds, reversals=dataset.reversals
        )
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

        # Record confusion matrix
        act_key = (
            "PROPOSED"
            if gt.expected_outcome is MatchOutcome.PROPOSED
            else ("AMBIGUOUS" if gt.expected_outcome is MatchOutcome.AMBIGUOUS else "NO_MATCH")
        )
        if outcome is MatchOutcome.PROPOSED:
            pred_key = "PROPOSED"
        elif outcome is MatchOutcome.AMBIGUOUS:
            pred_key = "AMBIGUOUS"
        else:
            pred_key = "NO_MATCH"
        confusion_matrix[act_key][pred_key] += 1

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
        confusion_matrix=confusion_matrix,
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


def format_confusion_matrix(metrics: BenchmarkV2Metrics) -> str:
    """Format the 3x3 confusion matrix into a clean markdown table."""
    cm = metrics.confusion_matrix
    headers = [f"{metrics.matcher_name}: Actual \\ Pred", "PROPOSED", "AMBIGUOUS", "NO_MATCH"]
    rows = [
        ["PROPOSED", str(cm.get("PROPOSED", {}).get("PROPOSED", 0)), str(cm.get("PROPOSED", {}).get("AMBIGUOUS", 0)), str(cm.get("PROPOSED", {}).get("NO_MATCH", 0))],
        ["AMBIGUOUS", str(cm.get("AMBIGUOUS", {}).get("PROPOSED", 0)), str(cm.get("AMBIGUOUS", {}).get("AMBIGUOUS", 0)), str(cm.get("AMBIGUOUS", {}).get("NO_MATCH", 0))],
        ["NO_MATCH", str(cm.get("NO_MATCH", {}).get("PROPOSED", 0)), str(cm.get("NO_MATCH", {}).get("AMBIGUOUS", 0)), str(cm.get("NO_MATCH", {}).get("NO_MATCH", 0))],
    ]
    col_widths = [max(len(row[i]) for row in [headers] + rows) for i in range(len(headers))]
    lines = []
    lines.append(" | ".join(headers[i].ljust(col_widths[i]) for i in range(len(headers))))
    lines.append("-+-".join("-" * col_widths[i] for i in range(len(headers))))
    for r in rows:
        lines.append(" | ".join(r[i].ljust(col_widths[i]) for i in range(len(headers))))
    return "\n".join(lines)


def format_scenario_breakdown(metrics: BenchmarkV2Metrics) -> str:
    """Format scenario group performance metrics into a markdown table."""
    headers = ["Scenario Family", "Total", "Correct", "Accuracy (%)"]
    rows = []
    for sc, data in sorted(metrics.scenario_breakdown.items()):
        tot = data.get("total", 0)
        corr = data.get("correct", 0)
        pct = f"{(corr / tot):.1%}" if tot > 0 else "N/A"
        rows.append([sc, str(tot), str(corr), pct])
    col_widths = [max(len(row[i]) for row in [headers] + rows) for i in range(len(headers))]
    lines = []
    lines.append(" | ".join(headers[i].ljust(col_widths[i]) for i in range(len(headers))))
    lines.append("-+-".join("-" * col_widths[i] for i in range(len(headers))))
    for r in rows:
        lines.append(" | ".join(r[i].ljust(col_widths[i]) for i in range(len(headers))))
    return "\n".join(lines)

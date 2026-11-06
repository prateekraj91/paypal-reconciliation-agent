"""Benchmark v2 Quantitative Evaluation Tests.

Compares Baseline, Baseline-Strict, Deterministic v1, and Advanced D5 across
14 scenario groups in train and holdout evaluation splits.
"""

import pytest

from reconcile.baseline import BaselineConfig, BaselineMatcher
from reconcile.benchmark_v2 import (
    ScenarioGroup,
    evaluate_matcher_v2,
    format_benchmark_v2_table,
    format_confusion_matrix,
    format_scenario_breakdown,
    generate_benchmark_v2,
)
from reconcile.deterministic import DeterministicMatcher
from reconcile.advanced import AdvancedMatcher

TRAIN_SEEDS = [10, 11, 12]
HOLDOUT_SEEDS = [100, 101, 102]


@pytest.fixture
def matchers():
    return [
        BaselineMatcher(),
        BaselineMatcher(BaselineConfig(allow_amount_only=False), name="baseline-strict"),
        DeterministicMatcher(),
        AdvancedMatcher(),
    ]


def test_benchmark_v2_dataset_structure():
    dataset = generate_benchmark_v2(seed=42, split="train", cases_per_group=8)
    assert len(dataset.invoices) > 0
    assert len(dataset.payments) > 0
    assert len(dataset.truth) == 14 * 8
    assert len(dataset.truth) >= 100

    # Verify all 14 scenario groups are covered
    covered = {gt.scenario_group for gt in dataset.truth.values()}
    assert len(covered) == 14
    for sg in ScenarioGroup:
        assert sg in covered


def test_train_holdout_separation():
    """Verify train and holdout splits are strictly disjoint across IDs, numbers, and entities."""
    train_ds = generate_benchmark_v2(seed=42, split="train", cases_per_group=8)
    holdout_ds = generate_benchmark_v2(seed=42, split="holdout", cases_per_group=8)

    train_inv_ids = {inv.invoice_id for inv in train_ds.invoices}
    holdout_inv_ids = {inv.invoice_id for inv in holdout_ds.invoices}
    assert train_inv_ids.isdisjoint(holdout_inv_ids), "Invoice IDs overlap between train and holdout!"

    train_pay_ids = {p.payment_id for p in train_ds.payments}
    holdout_pay_ids = {p.payment_id for p in holdout_ds.payments}
    assert train_pay_ids.isdisjoint(holdout_pay_ids), "Payment IDs overlap between train and holdout!"

    train_inv_nums = {inv.number for inv in train_ds.invoices}
    holdout_inv_nums = {inv.number for inv in holdout_ds.invoices}
    assert train_inv_nums.isdisjoint(holdout_inv_nums), "Invoice numbers overlap between train and holdout!"

    train_emails = {inv.customer_email for inv in train_ds.invoices}
    holdout_emails = {inv.customer_email for inv in holdout_ds.invoices}
    assert train_emails.isdisjoint(holdout_emails), "Customer emails overlap between train and holdout!"


@pytest.mark.parametrize("seed", TRAIN_SEEDS)
def test_advanced_matcher_invariants_train(seed):
    dataset = generate_benchmark_v2(seed=seed, split="train")
    matcher = AdvancedMatcher()
    metrics = evaluate_matcher_v2(matcher, dataset)

    # 1. Zero False Positives
    assert metrics.false_positives == 0, f"AdvancedMatcher produced {metrics.false_positives} false positives on seed {seed}"

    # 2. Zero Capacity Violations
    assert metrics.capacity_violations == 0, "Invoice capacity was violated!"

    # 3. Zero Double-Crediting Violations
    assert metrics.double_crediting_violations == 0, "Double crediting occurred!"

    # 4. Ambiguity Detection 100%
    assert metrics.ambiguity_accuracy == 1.0, "Failed to identify ambiguity!"

    # 5. Overpayment Resolution 100%
    assert metrics.overpayment_accuracy == 1.0, "Failed to resolve overpayment!"

    # 6. Precision 1.0
    assert metrics.precision == 1.0


@pytest.mark.parametrize("seed", HOLDOUT_SEEDS)
def test_advanced_matcher_invariants_holdout(seed):
    dataset = generate_benchmark_v2(seed=seed, split="holdout")
    matcher = AdvancedMatcher()
    metrics = evaluate_matcher_v2(matcher, dataset)

    assert metrics.false_positives == 0
    assert metrics.capacity_violations == 0
    assert metrics.double_crediting_violations == 0
    assert metrics.ambiguity_accuracy == 1.0
    assert metrics.overpayment_accuracy == 1.0
    assert metrics.precision == 1.0


def test_quantitative_matcher_comparison(matchers):
    """Compare all 4 matchers and verify AdvancedMatcher strictly dominates in Recall and F1 while maintaining 100% precision."""
    dataset = generate_benchmark_v2(seed=15, split="train")

    results = {}
    for m in matchers:
        results[m.name] = evaluate_matcher_v2(m, dataset)

    table = format_benchmark_v2_table(list(results.values()))
    print("\n\n=== BENCHMARK V2 QUANTITATIVE COMPARISON ===\n" + table + "\n")

    baseline_metrics = results["baseline"]
    strict_metrics = results["baseline-strict"]
    det_metrics = results["deterministic-v1"]
    adv_metrics = results["advanced-matcher-d5"]

    # AdvancedMatcher must strictly outperform deterministic-v1 on recall and F1
    assert adv_metrics.recall > det_metrics.recall
    assert adv_metrics.f1_score > det_metrics.f1_score
    assert adv_metrics.false_positives == 0

    # AdvancedMatcher must have 100% precision
    assert adv_metrics.precision == 1.0

    # Overpayment is handled by AdvancedMatcher but missed by baseline/deterministic
    assert adv_metrics.overpayment_accuracy == 1.0
    assert det_metrics.overpayment_accuracy == 0.0

    # Contextual matching is handled by AdvancedMatcher
    ctx_adv = adv_metrics.scenario_breakdown[ScenarioGroup.CONTEXTUAL_MATCH.value]
    assert ctx_adv["correct"] == ctx_adv["total"]

    ctx_det = det_metrics.scenario_breakdown[ScenarioGroup.CONTEXTUAL_MATCH.value]
    assert ctx_det["correct"] == 0  # Deterministic does not do contextual matching without ref


def test_confusion_matrix_and_breakdown_formatting():
    """Verify confusion matrix and scenario breakdown formatting on the expanded dataset."""
    dataset = generate_benchmark_v2(seed=20, split="train", cases_per_group=8)
    matcher = AdvancedMatcher()
    metrics = evaluate_matcher_v2(matcher, dataset)

    # Check confusion matrix sums
    cm = metrics.confusion_matrix
    total_in_cm = sum(sum(row.values()) for row in cm.values())
    assert total_in_cm == metrics.total_cases
    assert total_in_cm >= 100

    # Format tables and verify non-empty headers and rows
    cm_table = format_confusion_matrix(metrics)
    assert "PROPOSED" in cm_table
    assert "AMBIGUOUS" in cm_table
    assert "NO_MATCH" in cm_table

    breakdown_table = format_scenario_breakdown(metrics)
    for sg in ScenarioGroup:
        assert sg.value in breakdown_table

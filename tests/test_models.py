from datetime import date
import pytest
from factories import inv, pay

from reconcile.models import (
    AccountingError,
    AllocationStatus, Candidate, Evidence, EvidenceCode, Invoice, InvoiceStatus, MatchOutcome,
    MatchProposal, PaymentAllocation, ReconciliationStatus, cross_check_invoice_balance,
    deduplicate_allocations, outstanding_cents,
)


def alloc(payment="pay_1", invoice="inv_1", amount=100, status=AllocationStatus.PROPOSED):
    return PaymentAllocation(payment, invoice, amount, status)


def test_rejects_non_positive_money():
    with pytest.raises(ValueError):
        inv(1, 0)
    with pytest.raises(ValueError):
        pay(1, -5)
    with pytest.raises(ValueError):
        alloc(amount=0)


def test_only_sent_and_partially_paid_invoices_are_open():
    assert InvoiceStatus.SENT.is_open and InvoiceStatus.PARTIALLY_PAID.is_open
    assert not InvoiceStatus.PAID.is_open and not InvoiceStatus.CANCELLED.is_open
    assert not InvoiceStatus.DRAFT.is_open


def test_invoice_due_amount_and_outstanding():
    # When due_amount_cents is not specified, outstanding balance defaults to total amount
    i = inv(1, 1000)
    assert i.due_amount_cents is None
    assert i.outstanding_balance_cents == 1000

    # When due_amount_cents is specified
    from datetime import date
    from reconcile.models import Invoice
    partial = Invoice(
        invoice_id="inv_p",
        number="INV-2000",
        customer_name="Acme",
        amount_cents=1000,
        currency="USD",
        issued_on=date(2026, 8, 1),
        due_on=date(2026, 8, 31),
        status=InvoiceStatus.PARTIALLY_PAID,
        due_amount_cents=600,
    )
    assert partial.amount_cents == 1000
    assert partial.due_amount_cents == 600
    assert partial.outstanding_balance_cents == 600

    # Negative due amount or due amount exceeding total is rejected
    with pytest.raises(ValueError):
        Invoice(
            invoice_id="inv_bad",
            number="INV-BAD",
            customer_name="Acme",
            amount_cents=1000,
            currency="USD",
            issued_on=date(2026, 8, 1),
            due_on=date(2026, 8, 31),
            due_amount_cents=-10,
        )
    with pytest.raises(ValueError):
        Invoice(
            invoice_id="inv_bad2",
            number="INV-BAD",
            customer_name="Acme",
            amount_cents=1000,
            currency="USD",
            issued_on=date(2026, 8, 1),
            due_on=date(2026, 8, 31),
            due_amount_cents=1500,
        )



def test_outstanding_counts_only_approved_allocations():
    i = inv(1, 1000)
    allocs = [
        alloc(amount=300, status=AllocationStatus.APPROVED),
        alloc(payment="pay_2", amount=200, status=AllocationStatus.PROPOSED),
        alloc(payment="pay_3", amount=100, status=AllocationStatus.REJECTED),
        alloc(payment="pay_4", invoice="inv_other", amount=999, status=AllocationStatus.APPROVED),
    ]
    assert outstanding_cents(i, allocs) == 700


def test_outstanding_does_not_hide_over_allocation():
    i = inv(1, 1000)
    assert outstanding_cents(i, [alloc(amount=1500, status=AllocationStatus.APPROVED)]) == -500


def test_repeated_approved_allocation_does_not_double_count():
    i = inv(1, 1000)
    a = alloc(payment="pay_1", amount=300, status=AllocationStatus.APPROVED)
    # The same approved payment must not be deducted twice
    assert outstanding_cents(i, [a, a]) == 700


def test_cross_check_invoice_balance():
    from datetime import date
    from reconcile.models import Invoice

    # Agreed balance
    inv_partial = Invoice(
        invoice_id="inv_agree",
        number="INV-AGREE",
        customer_name="Acme",
        amount_cents=2500,
        currency="USD",
        issued_on=date(2026, 8, 1),
        due_on=date(2026, 8, 31),
        due_amount_cents=1500,
    )
    alloc_1000 = alloc(payment="pay_1", invoice="inv_agree", amount=1000, status=AllocationStatus.APPROVED)
    assert cross_check_invoice_balance(inv_partial, [alloc_1000]) == 1500

    # Disagreeing balance fails closed
    inv_corrupt = Invoice(
        invoice_id="inv_disagree",
        number="INV-DISAGREE",
        customer_name="Acme",
        amount_cents=2500,
        currency="USD",
        issued_on=date(2026, 8, 1),
        due_on=date(2026, 8, 31),
        due_amount_cents=1200,  # Corrupt: does not match 2500 - 1000 = 1500
    )
    alloc_corrupt = alloc(payment="pay_1", invoice="inv_disagree", amount=1000, status=AllocationStatus.APPROVED)
    with pytest.raises(ValueError) as exc_info:
        cross_check_invoice_balance(inv_corrupt, [alloc_corrupt])
    assert "inconsistency" in str(exc_info.value).lower()




def test_candidate_rules():
    with pytest.raises(ValueError):
        Candidate(())
    with pytest.raises(ValueError):  # two payments in one candidate
        Candidate((alloc(payment="a"), alloc(payment="b", invoice="inv_2")))
    with pytest.raises(ValueError):  # same invoice twice
        Candidate((alloc(), alloc()))
    with pytest.raises(ValueError):  # matchers cannot emit approvals
        Candidate((alloc(status=AllocationStatus.APPROVED),))


def test_proposal_invariants():
    cand = Candidate((alloc(),), (Evidence(EvidenceCode.AMOUNT_EXACT),))
    cand2 = Candidate((alloc(invoice="inv_2"),))
    with pytest.raises(ValueError):
        MatchProposal("pay_1", MatchOutcome.PROPOSED)
    with pytest.raises(ValueError):
        MatchProposal("pay_1", MatchOutcome.AMBIGUOUS, (cand,))
    with pytest.raises(ValueError):
        MatchProposal("pay_1", MatchOutcome.NO_MATCH, (cand,))
    with pytest.raises(ValueError):
        MatchProposal("pay_OTHER", MatchOutcome.PROPOSED, (cand,))
    MatchProposal("pay_1", MatchOutcome.PROPOSED, (cand,))
    MatchProposal("pay_1", MatchOutcome.AMBIGUOUS, (cand, cand2))
    MatchProposal("pay_1", MatchOutcome.NO_MATCH)


def test_initial_reconciliation_status_never_reconciled():
    cand = Candidate((alloc(),))
    cand2 = Candidate((alloc(invoice="inv_2"),))
    assert MatchProposal("pay_1", MatchOutcome.PROPOSED, (cand,)).initial_status is ReconciliationStatus.PROPOSED
    assert MatchProposal("pay_1", MatchOutcome.AMBIGUOUS, (cand, cand2)).initial_status is ReconciliationStatus.NEEDS_REVIEW
    assert MatchProposal("pay_1", MatchOutcome.NO_MATCH).initial_status is ReconciliationStatus.UNMATCHED


def test_outstanding_balance_cents_property_semantics():
    """outstanding_balance_cents provides provider snapshot, not replacing ledger arithmetic."""
    inv = Invoice(
        invoice_id="inv_doc",
        number="INV-DOC",
        customer_name="Acme",
        amount_cents=2500,
        currency="USD",
        issued_on=date(2026, 8, 1),
        due_on=date(2026, 8, 31),
        due_amount_cents=1500,
    )
    assert inv.outstanding_balance_cents == 1500
    doc = Invoice.outstanding_balance_cents.__doc__ or ""
    assert "Provider-reported snapshot" in doc
    assert "outstanding_cents" in doc


def test_deduplicate_allocations_split_payment_across_invoices_preserved():
    """One payment covering multiple invoices preserves allocations for both invoices."""
    alloc1 = alloc(payment="pay_split", invoice="inv_1", amount=2000, status=AllocationStatus.APPROVED)
    alloc2 = alloc(payment="pay_split", invoice="inv_2", amount=3000, status=AllocationStatus.APPROVED)
    deduped = deduplicate_allocations([alloc1, alloc2])
    assert len(deduped) == 2
    assert deduped == [alloc1, alloc2]


def test_deduplicate_allocations_identical_duplicates_for_same_invoice():
    """Identical duplicate allocations for the same invoice are safely deduplicated to 1."""
    alloc1 = alloc(payment="pay_1", invoice="inv_1", amount=1500, status=AllocationStatus.APPROVED)
    alloc2 = alloc(payment="pay_1", invoice="inv_1", amount=1500, status=AllocationStatus.APPROVED)
    deduped = deduplicate_allocations([alloc1, alloc2])
    assert len(deduped) == 1
    assert deduped[0] == alloc1


def test_deduplicate_allocations_conflicting_amounts_fails_closed():
    """Conflicting amounts for the same payment and invoice raise AccountingError."""
    alloc1 = alloc(payment="pay_1", invoice="inv_1", amount=1000, status=AllocationStatus.APPROVED)
    alloc2 = alloc(payment="pay_1", invoice="inv_1", amount=2000, status=AllocationStatus.APPROVED)
    with pytest.raises(AccountingError) as exc_info:
        deduplicate_allocations([alloc1, alloc2])
    assert "conflicting allocation amount" in str(exc_info.value).lower()


def test_deduplicate_allocations_conflicting_statuses_fails_closed():
    """Conflicting statuses for the same payment and invoice raise AccountingError."""
    alloc1 = alloc(payment="pay_1", invoice="inv_1", amount=1000, status=AllocationStatus.APPROVED)
    alloc2 = alloc(payment="pay_1", invoice="inv_1", amount=1000, status=AllocationStatus.PROPOSED)
    with pytest.raises(AccountingError) as exc_info:
        deduplicate_allocations([alloc1, alloc2])
    assert "conflicting allocation status" in str(exc_info.value).lower()


def test_grouped_payments_correct_remaining_balances():
    """Remaining invoice balances are correctly computed after grouped payments."""
    inv1 = Invoice(
        invoice_id="inv_1",
        number="INV-001",
        customer_name="Acme",
        amount_cents=5000,
        currency="USD",
        issued_on=date(2026, 8, 1),
        due_on=date(2026, 8, 31),
        due_amount_cents=3000,
    )
    inv2 = Invoice(
        invoice_id="inv_2",
        number="INV-002",
        customer_name="Acme",
        amount_cents=8000,
        currency="USD",
        issued_on=date(2026, 8, 1),
        due_on=date(2026, 8, 31),
        due_amount_cents=5000,
    )
    # PAY-GROUP pays $20 on INV-001 and $30 on INV-002
    alloc1 = alloc(payment="pay_group", invoice="inv_1", amount=2000, status=AllocationStatus.APPROVED)
    alloc2 = alloc(payment="pay_group", invoice="inv_2", amount=3000, status=AllocationStatus.APPROVED)
    all_allocs = [alloc1, alloc2]

    assert outstanding_cents(inv1, all_allocs) == 3000
    assert cross_check_invoice_balance(inv1, all_allocs) == 3000

    assert outstanding_cents(inv2, all_allocs) == 5000
    assert cross_check_invoice_balance(inv2, all_allocs) == 5000


def test_outstanding_cents_conflicting_approved_amounts_fails_closed():
    """outstanding_cents raises AccountingError when duplicate approved allocations conflict in amount."""
    i = inv(1, 1000)
    a1 = alloc(payment="pay_1", invoice="inv_1", amount=300, status=AllocationStatus.APPROVED)
    a2 = alloc(payment="pay_1", invoice="inv_1", amount=400, status=AllocationStatus.APPROVED)
    with pytest.raises(AccountingError) as exc_info:
        outstanding_cents(i, [a1, a2])
    assert "conflicting approved allocation amount" in str(exc_info.value).lower()


# --------------------------------------------------------------------------
# Currency Invariant Tests
# --------------------------------------------------------------------------


def test_payment_allocation_currency_normalization_and_validation():
    """PaymentAllocation normalizes 3-letter currency and rejects invalid lengths."""
    a = PaymentAllocation("pay_1", "inv_1", 1000, AllocationStatus.APPROVED, currency="usd")
    assert a.currency == "USD"

    with pytest.raises(ValueError):
        PaymentAllocation("pay_1", "inv_1", 1000, currency="US")

    with pytest.raises(ValueError):
        PaymentAllocation("pay_1", "inv_1", 1000, currency="USDD")


def test_outstanding_cents_matching_currency_usd_usd():
    """When allocation currency matches invoice currency (USD/USD), calculation succeeds."""
    i = inv(1, 5000)  # USD
    a = PaymentAllocation("pay_1", i.invoice_id, 2000, AllocationStatus.APPROVED, currency="USD")
    assert outstanding_cents(i, [a]) == 3000


def test_outstanding_cents_mismatching_currency_usd_eur_fails_closed():
    """When allocation currency contradicts invoice currency (USD/EUR), raises AccountingError."""
    i = inv(1, 5000)  # USD
    a = PaymentAllocation("pay_1", i.invoice_id, 2000, AllocationStatus.APPROVED, currency="EUR")
    with pytest.raises(AccountingError) as exc_info:
        outstanding_cents(i, [a])
    assert "currency mismatch" in str(exc_info.value).lower()


def test_outstanding_cents_legacy_allocations_none_currency_preserved():
    """Legacy allocations where currency is None remain allowed for backward compatibility.

    Missing currency does NOT prove a match, but preserves compatibility for existing callers.
    """
    i = inv(1, 5000)  # USD
    a_legacy = PaymentAllocation("pay_1", i.invoice_id, 2000, AllocationStatus.APPROVED, currency=None)
    assert a_legacy.currency is None
    assert outstanding_cents(i, [a_legacy]) == 3000

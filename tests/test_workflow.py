"""Tests for the complete ingestion-to-verification reconciliation workflow."""

from __future__ import annotations

from unittest.mock import patch
import pytest

from factories import inv, pay
from reconcile.models import (
    AccountingError,
    AllocationStatus,
    Candidate,
    EvidenceCode,
    InvoiceStatus,
    MatchOutcome,
    MatchProposal,
    Payment,
    PaymentAllocation,
    PaymentStatus,
    outstanding_cents,
)
from reconcile.paypal import PayPalClient, PayPalAccountingError
from reconcile.workflow import reconcile_payments, ReconciliationRunResult


def _mock_client_with_items(items: list[dict]) -> PayPalClient:
    client = PayPalClient(client_id="mock_id", client_secret="mock_secret")
    client.list_raw_invoices = lambda page=1, page_size=20, total_required=False: {"items": items}  # type: ignore[method-assign]
    return client


# --------------------------------------------------------------------------
# Case 1: An unpaid invoice with no existing allocations
# --------------------------------------------------------------------------


def test_workflow_unpaid_invoice_with_no_existing_allocations():
    """Unpaid invoice with no existing allocations matches an exact incoming payment and passes verification."""
    raw_item = {
        "id": "INV-UNPAID-1",
        "status": "SENT",
        "detail": {"invoice_number": "INV-1001", "invoice_date": "2026-10-31"},
        "amount": {"currency_code": "USD", "value": "100.00"},
        "due_amount": {"currency_code": "USD", "value": "100.00"},
        "payments": None,
    }
    client = _mock_client_with_items([raw_item])
    payment = pay(1, 10000, reference="INV-1001")

    result = reconcile_payments([payment], client)

    assert len(result.invoices) == 1
    assert result.invoices[0].number == "INV-1001"
    assert len(result.existing_allocations) == 0

    assert len(result.proposals) == 1
    proposal = result.proposals[0]
    assert proposal.outcome is MatchOutcome.PROPOSED
    assert len(proposal.candidates[0].allocations) == 1
    assert proposal.candidates[0].allocations[0].amount_cents == 10000

    # Independent verification must pass
    assert len(result.verification_results) == 1
    assert result.verification_results[0].passed
    assert result.verification_results[0].issues == ()

    report = result.report
    assert report.total_payments == 1
    assert report.proposals_count == 1
    assert report.passed_verification_count == 1
    assert report.rejected_verification_count == 0
    assert report.review_queue_count == 0


# --------------------------------------------------------------------------
# Case 2: An invoice with a partial payment and a correct remaining balance
# --------------------------------------------------------------------------


def test_workflow_partial_payment_and_correct_remaining_balance():
    """Invoice with partial payment on PayPal has correct ledger balance and matches new remainder payment."""
    raw_item = {
        "id": "INV-PARTIAL-2",
        "status": "PARTIALLY_PAID",
        "detail": {"invoice_number": "INV-1002", "invoice_date": "2026-10-31"},
        "amount": {"currency_code": "USD", "value": "100.00"},
        "due_amount": {"currency_code": "USD", "value": "60.00"},
        "payments": {
            "paid_amount": {"currency_code": "USD", "value": "40.00"},
            "transactions": [
                {
                    "payment_id": "PREV-PARTIAL-40",
                    "amount": {"currency_code": "USD", "value": "40.00"},
                }
            ],
        },
    }
    client = _mock_client_with_items([raw_item])
    new_payment = pay(2, 6000, reference="INV-1002")

    result = reconcile_payments([new_payment], client)

    assert len(result.existing_allocations) == 1
    assert result.existing_allocations[0].payment_id == "PREV-PARTIAL-40"
    assert result.existing_allocations[0].amount_cents == 4000
    assert result.existing_allocations[0].status == AllocationStatus.APPROVED

    # Remaining balance is 6000 cents
    assert outstanding_cents(result.invoices[0], result.existing_allocations) == 6000

    assert len(result.proposals) == 1
    proposal = result.proposals[0]
    assert proposal.outcome is MatchOutcome.PROPOSED
    assert proposal.candidates[0].allocations[0].amount_cents == 6000
    assert proposal.candidates[0].allocations[0].payment_id == new_payment.payment_id

    assert result.verification_results[0].passed


# --------------------------------------------------------------------------
# Case 3: A fully paid invoice that must not receive another allocation
# --------------------------------------------------------------------------


def test_workflow_fully_paid_invoice_cannot_receive_allocation():
    """Fully paid invoice on PayPal cannot be allocated again."""
    raw_item = {
        "id": "INV-PAID-3",
        "status": "PAID",
        "detail": {"invoice_number": "INV-1003", "invoice_date": "2026-10-31"},
        "amount": {"currency_code": "USD", "value": "50.00"},
        "due_amount": {"currency_code": "USD", "value": "0.00"},
        "payments": {
            "paid_amount": {"currency_code": "USD", "value": "50.00"},
            "transactions": [
                {
                    "payment_id": "PAID-TXN-50",
                    "amount": {"currency_code": "USD", "value": "50.00"},
                }
            ],
        },
    }
    client = _mock_client_with_items([raw_item])
    late_payment = pay(3, 5000, reference="INV-1003")

    result = reconcile_payments([late_payment], client)

    assert len(result.existing_allocations) == 1
    assert outstanding_cents(result.invoices[0], result.existing_allocations) == 0

    assert len(result.proposals) == 1
    proposal = result.proposals[0]
    assert proposal.outcome is MatchOutcome.NO_MATCH
    reasons = [e.code for e in proposal.reasons]
    # Rejection because invoice is PAID (not open) and has no remaining balance
    assert EvidenceCode.INVOICE_NOT_OPEN in reasons or EvidenceCode.AMOUNT_MISMATCH in reasons
    assert len(result.verification_results) == 0  # Only PROPOSED proposals go to verification


# --------------------------------------------------------------------------
# Case 4: Duplicate transaction IDs that must not cause double deductions
# --------------------------------------------------------------------------


def test_workflow_duplicate_transaction_ids_cannot_double_deduct():
    """Duplicate transaction entries on PayPal do not cause double deductions in the ledger."""
    raw_item = {
        "id": "INV-DUP-4",
        "status": "PARTIALLY_PAID",
        "detail": {"invoice_number": "INV-1004", "invoice_date": "2026-10-31"},
        "amount": {"currency_code": "USD", "value": "100.00"},
        "due_amount": {"currency_code": "USD", "value": "75.00"},
        "payments": {
            "transactions": [
                {"payment_id": "REPEATED-TXN-25", "amount": {"currency_code": "USD", "value": "25.00"}},
                {"payment_id": "REPEATED-TXN-25", "amount": {"currency_code": "USD", "value": "25.00"}},
            ],
        },
    }
    client = _mock_client_with_items([raw_item])
    remainder_payment = pay(4, 7500, reference="INV-1004")

    result = reconcile_payments([remainder_payment], client)

    # REPEATED-TXN-25 must appear only once in existing allocations
    assert len(result.existing_allocations) == 1
    assert result.existing_allocations[0].amount_cents == 2500

    # Calculated outstanding balance must be 7500 cents, NOT 5000 cents
    assert outstanding_cents(result.invoices[0], result.existing_allocations) == 7500

    proposal = result.proposals[0]
    assert proposal.outcome is MatchOutcome.PROPOSED
    assert proposal.candidates[0].allocations[0].amount_cents == 7500
    assert result.verification_results[0].passed


# --------------------------------------------------------------------------
# Case 5: An invoice whose provider-reported balance contradicts the internal ledger
# --------------------------------------------------------------------------


def test_workflow_inconsistent_provider_balance_fails_closed():
    """Provider reported balance contradicting transaction records fails closed with domain exception."""
    raw_item = {
        "id": "INV-CORRUPT-5",
        "status": "PARTIALLY_PAID",
        "detail": {"invoice_number": "INV-1005", "invoice_date": "2026-10-31"},
        "amount": {"currency_code": "USD", "value": "100.00"},
        "due_amount": {"currency_code": "USD", "value": "40.00"},  # Contradicts 100 - 30 = 70
        "payments": {
            "transactions": [
                {"payment_id": "TXN-30", "amount": {"currency_code": "USD", "value": "30.00"}},
            ],
        },
    }
    client = _mock_client_with_items([raw_item])
    payment = pay(5, 7000, reference="INV-1005")

    with pytest.raises(PayPalAccountingError) as exc_info:
        reconcile_payments([payment], client)

    assert "due_amount" in str(exc_info.value).lower() and "contradict" in str(exc_info.value).lower()


# --------------------------------------------------------------------------
# Case 6: An invalid currency or unsupported transaction that must fail safely
# --------------------------------------------------------------------------


def test_workflow_invalid_currency_fails_safely():
    """A transaction in foreign currency on PayPal fails safely without reducing invoice balance."""
    raw_item = {
        "id": "INV-FOREIGN-6",
        "status": "SENT",
        "detail": {"invoice_number": "INV-1006", "invoice_date": "2026-10-31"},
        "amount": {"currency_code": "USD", "value": "100.00"},
        "due_amount": {"currency_code": "USD", "value": "100.00"},
        "payments": {
            "transactions": [
                {"payment_id": "TXN-EUR", "amount": {"currency_code": "EUR", "value": "50.00"}},
            ],
        },
    }
    client = _mock_client_with_items([raw_item])
    payment = pay(6, 10000, reference="INV-1006")

    with pytest.raises(PayPalAccountingError) as exc_info:
        reconcile_payments([payment], client)

    assert "currency" in str(exc_info.value).lower()


def test_workflow_unsupported_refund_transaction_fails_safely():
    """Unsupported transaction type (REFUND) fails safely with domain exception."""
    raw_item = {
        "id": "INV-REFUND-6B",
        "status": "SENT",
        "detail": {"invoice_number": "INV-1006B", "invoice_date": "2026-10-31"},
        "amount": {"currency_code": "USD", "value": "100.00"},
        "due_amount": {"currency_code": "USD", "value": "100.00"},
        "payments": {
            "transactions": [
                {"payment_id": "TXN-REF", "type": "REFUND", "amount": {"currency_code": "USD", "value": "20.00"}},
            ],
        },
    }
    client = _mock_client_with_items([raw_item])
    payment = pay(6, 10000, reference="INV-1006B")

    with pytest.raises(PayPalAccountingError) as exc_info:
        reconcile_payments([payment], client)

    assert "unsupported type" in str(exc_info.value).lower()


# --------------------------------------------------------------------------
# Case 7: Existing approved allocations and new payment matching remaining balance
# --------------------------------------------------------------------------


def test_workflow_existing_approved_allocations_and_new_payment_matches_remainder():
    """Existing approved allocations are preserved and new payment matches the remaining balance."""
    raw_item = {
        "id": "INV-REMAINDER-7",
        "status": "PARTIALLY_PAID",
        "detail": {"invoice_number": "INV-1007", "invoice_date": "2026-10-31"},
        "amount": {"currency_code": "USD", "value": "50.00"},
        "due_amount": {"currency_code": "USD", "value": "20.00"},
        "payments": {
            "transactions": [
                {"payment_id": "PRIOR-APPROVED-1", "amount": {"currency_code": "USD", "value": "30.00"}},
            ],
        },
    }
    client = _mock_client_with_items([raw_item])
    new_payment = pay(7, 2000, reference="INV-1007")

    result = reconcile_payments([new_payment], client)

    # 1. Prior allocation must be preserved as APPROVED
    assert len(result.existing_allocations) == 1
    assert result.existing_allocations[0].payment_id == "PRIOR-APPROVED-1"
    assert result.existing_allocations[0].amount_cents == 3000
    assert result.existing_allocations[0].status == AllocationStatus.APPROVED

    # 2. New payment must be proposed for remaining 2000 cents
    assert len(result.proposals) == 1
    proposal = result.proposals[0]
    assert proposal.outcome is MatchOutcome.PROPOSED
    assert proposal.candidates[0].allocations[0].payment_id == "pay_7"
    assert proposal.candidates[0].allocations[0].amount_cents == 2000

    # 3. Verification passes
    assert result.verification_results[0].passed


# --------------------------------------------------------------------------
# Safety Check: Previously approved allocations are never proposed or re-allocated
# --------------------------------------------------------------------------


def test_previously_approved_payment_is_never_proposed_or_allocated_again():
    """A payment already approved in the ledger must never be proposed or verified again."""
    raw_item = {
        "id": "INV-SAFETY-8",
        "status": "PARTIALLY_PAID",
        "detail": {"invoice_number": "INV-1008", "invoice_date": "2026-10-31"},
        "amount": {"currency_code": "USD", "value": "100.00"},
        "due_amount": {"currency_code": "USD", "value": "70.00"},
        "payments": {
            "transactions": [
                {"payment_id": "ALREADY-SETTLED-PAYMENT", "amount": {"currency_code": "USD", "value": "30.00"}},
            ],
        },
    }
    client = _mock_client_with_items([raw_item])

    # A redelivery or repeat delivery of ALREADY-SETTLED-PAYMENT
    re_delivered_payment = pay(
        8,
        3000,
        reference="INV-1008",
    )
    # Give it the identical payment_id
    re_delivered_payment = Payment(
        payment_id="ALREADY-SETTLED-PAYMENT",
        amount_cents=3000,
        currency="USD",
        received_at=re_delivered_payment.received_at,
        payer_name="Acme Studio",
        reference="INV-1008",
        status=PaymentStatus.COMPLETED,
    )

    result = reconcile_payments([re_delivered_payment], client)

    # The matcher must reject it as DUPLICATE_EVENT because it is already approved
    assert len(result.proposals) == 1
    proposal = result.proposals[0]
    assert proposal.outcome is MatchOutcome.NO_MATCH
    assert any(e.code is EvidenceCode.DUPLICATE_EVENT for e in proposal.reasons)
    assert any("already approved" in e.detail.lower() for e in proposal.reasons)


# --------------------------------------------------------------------------
# Grouped Payment and Compound Key Deduplication Regressions
# --------------------------------------------------------------------------


def test_workflow_one_payment_allocated_across_two_invoices():
    """One payment allocated across two different invoices preserves both allocations and balances."""
    raw_item1 = {
        "id": "INV-SPLIT-A",
        "status": "PAID",
        "detail": {"invoice_number": "INV-SPLIT-A", "invoice_date": "2026-10-31"},
        "amount": {"currency_code": "USD", "value": "20.00"},
        "due_amount": {"currency_code": "USD", "value": "0.00"},
        "payments": {
            "transactions": [
                {"payment_id": "PAY-SPLIT-100", "amount": {"currency_code": "USD", "value": "20.00"}},
            ],
        },
    }
    raw_item2 = {
        "id": "INV-SPLIT-B",
        "status": "PAID",
        "detail": {"invoice_number": "INV-SPLIT-B", "invoice_date": "2026-10-31"},
        "amount": {"currency_code": "USD", "value": "30.00"},
        "due_amount": {"currency_code": "USD", "value": "0.00"},
        "payments": {
            "transactions": [
                {"payment_id": "PAY-SPLIT-100", "amount": {"currency_code": "USD", "value": "30.00"}},
            ],
        },
    }
    client = _mock_client_with_items([raw_item1, raw_item2])
    result = reconcile_payments([], client)

    assert len(result.invoices) == 2
    # Both allocations must be preserved in result.existing_allocations
    assert len(result.existing_allocations) == 2
    assert {a.invoice_id for a in result.existing_allocations} == {"INV-SPLIT-A", "INV-SPLIT-B"}
    assert all(a.payment_id == "PAY-SPLIT-100" for a in result.existing_allocations)
    amounts = {a.invoice_id: a.amount_cents for a in result.existing_allocations}
    assert amounts == {"INV-SPLIT-A": 2000, "INV-SPLIT-B": 3000}


def test_workflow_identical_duplicate_records_deduplicated():
    """Identical duplicate allocations across ingested client and existing_allocations are safely deduplicated."""
    raw_item = {
        "id": "INV-DUP-WF",
        "status": "PARTIALLY_PAID",
        "detail": {"invoice_number": "INV-DUP-WF", "invoice_date": "2026-10-31"},
        "amount": {"currency_code": "USD", "value": "100.00"},
        "due_amount": {"currency_code": "USD", "value": "60.00"},
        "payments": {
            "transactions": [
                {"payment_id": "PAY-EXISTING", "amount": {"currency_code": "USD", "value": "40.00"}},
            ],
        },
    }
    client = _mock_client_with_items([raw_item])
    alloc1 = PaymentAllocation("PAY-EXISTING", "INV-DUP-WF", 4000, AllocationStatus.APPROVED)
    alloc2 = PaymentAllocation("PAY-EXISTING", "INV-DUP-WF", 4000, AllocationStatus.APPROVED)

    result = reconcile_payments([], client, existing_allocations=[alloc1, alloc2])
    assert len(result.existing_allocations) == 1
    assert result.existing_allocations[0].payment_id == "PAY-EXISTING"
    assert result.existing_allocations[0].amount_cents == 4000


def test_workflow_conflicting_amounts_fails_closed():
    """Conflicting amounts for the same (payment_id, invoice_id) fail closed with AccountingError."""
    raw_item = {
        "id": "INV-CONF-WF",
        "status": "SENT",
        "detail": {"invoice_number": "INV-CONF-WF", "invoice_date": "2026-10-31"},
        "amount": {"currency_code": "USD", "value": "100.00"},
        "due_amount": {"currency_code": "USD", "value": "100.00"},
        "payments": None,
    }
    client = _mock_client_with_items([raw_item])
    alloc1 = PaymentAllocation("PAY-CONF", "INV-CONF-WF", 4000, AllocationStatus.APPROVED)
    alloc2 = PaymentAllocation("PAY-CONF", "INV-CONF-WF", 5000, AllocationStatus.APPROVED)

    with pytest.raises(AccountingError) as exc_info:
        reconcile_payments([], client, existing_allocations=[alloc1, alloc2])
    assert "conflicting allocation amount" in str(exc_info.value).lower()


def test_workflow_conflicting_statuses_fails_closed():
    """Conflicting statuses for the same (payment_id, invoice_id) fail closed with AccountingError."""
    raw_item = {
        "id": "INV-CONF-STAT",
        "status": "SENT",
        "detail": {"invoice_number": "INV-CONF-STAT", "invoice_date": "2026-10-31"},
        "amount": {"currency_code": "USD", "value": "100.00"},
        "due_amount": {"currency_code": "USD", "value": "100.00"},
        "payments": None,
    }
    client = _mock_client_with_items([raw_item])
    alloc1 = PaymentAllocation("PAY-CONF", "INV-CONF-STAT", 5000, AllocationStatus.APPROVED)
    alloc2 = PaymentAllocation("PAY-CONF", "INV-CONF-STAT", 5000, AllocationStatus.PROPOSED)

    with pytest.raises(AccountingError) as exc_info:
        reconcile_payments([], client, existing_allocations=[alloc1, alloc2])
    assert "conflicting allocation status" in str(exc_info.value).lower()


def test_workflow_grouped_payment_remaining_balance_allows_next_match():
    """Grouped payment correctly reduces remaining balance, allowing remainder payment to match."""
    raw_item1 = {
        "id": "INV-GRP-1",
        "status": "PARTIALLY_PAID",
        "detail": {"invoice_number": "INV-GRP-1", "invoice_date": "2026-10-31"},
        "amount": {"currency_code": "USD", "value": "50.00"},
        "due_amount": {"currency_code": "USD", "value": "30.00"},
        "payments": {
            "transactions": [
                {"payment_id": "SHARED-PAY-1", "amount": {"currency_code": "USD", "value": "20.00"}},
            ],
        },
    }
    raw_item2 = {
        "id": "INV-GRP-2",
        "status": "PARTIALLY_PAID",
        "detail": {"invoice_number": "INV-GRP-2", "invoice_date": "2026-10-31"},
        "amount": {"currency_code": "USD", "value": "75.00"},
        "due_amount": {"currency_code": "USD", "value": "45.00"},
        "payments": {
            "transactions": [
                {"payment_id": "SHARED-PAY-1", "amount": {"currency_code": "USD", "value": "30.00"}},
            ],
        },
    }
    client = _mock_client_with_items([raw_item1, raw_item2])
    # A new payment arrives for the exact remainder of INV-GRP-1 ($30.00)
    remainder_pay = pay(1, 3000, reference="INV-GRP-1")

    result = reconcile_payments([remainder_pay], client)

    assert len(result.existing_allocations) == 2
    assert len(result.proposals) == 1
    proposal = result.proposals[0]
    assert proposal.outcome is MatchOutcome.PROPOSED
    assert proposal.candidates[0].allocations[0].invoice_id == "INV-GRP-1"
    assert proposal.candidates[0].allocations[0].amount_cents == 3000
    assert len(result.verification_results) == 1
    assert result.verification_results[0].passed


# --------------------------------------------------------------------------
# Issue 1 & Issue 2 Workflow Regressions
# --------------------------------------------------------------------------


def test_real_payment_arriving_after_unidentified_partial_settlement_cannot_be_double_counted():
    """Unidentified partial settlement fails closed so real payment cannot be double-counted."""
    raw_item = {
        "id": "INV-UNIDENTIFIED-PARTIAL",
        "status": "PARTIALLY_PAID",
        "detail": {"invoice_number": "INV-UNID-1", "invoice_date": "2026-10-31"},
        "amount": {"currency_code": "USD", "value": "100.00"},
        "due_amount": {"currency_code": "USD", "value": "60.00"},
        "payments": {"transactions": []},  # Missing transaction history
    }
    client = _mock_client_with_items([raw_item])
    real_payment = pay(1, 4000, reference="INV-UNID-1")

    # Workflow must fail closed at ingestion rather than synthesize an unverified allocation
    # and then double-count the real payment against the remainder.
    with pytest.raises(PayPalAccountingError) as exc_info:
        reconcile_payments([real_payment], client)
    msg = str(exc_info.value).lower()
    assert "provenance" in msg or "transaction history is missing" in msg


def test_workflow_currency_mismatch_in_existing_allocations_fails_closed():
    """Existing allocation with mismatched currency (e.g. EUR on USD invoice) fails closed."""
    raw_item = {
        "id": "INV-USD-1",
        "status": "SENT",
        "detail": {"invoice_number": "INV-USD-1", "invoice_date": "2026-10-31"},
        "amount": {"currency_code": "USD", "value": "100.00"},
        "due_amount": {"currency_code": "USD", "value": "100.00"},
        "payments": None,
    }
    client = _mock_client_with_items([raw_item])
    # Caller supplies an existing allocation denominated in EUR
    alloc_eur = PaymentAllocation(
        payment_id="PAY-EUR-1",
        invoice_id="INV-USD-1",
        amount_cents=5000,
        status=AllocationStatus.APPROVED,
        currency="EUR",
    )

    with pytest.raises(AccountingError) as exc_info:
        reconcile_payments([], client, existing_allocations=[alloc_eur])
    assert "currency mismatch" in str(exc_info.value).lower()


def test_workflow_legacy_none_currency_allocation_preserved():
    """Legacy existing allocation where currency is None continues to work for backward compatibility."""
    raw_item = {
        "id": "INV-LEGACY-1",
        "status": "PARTIALLY_PAID",
        "detail": {"invoice_number": "INV-LEGACY-1", "invoice_date": "2026-10-31"},
        "amount": {"currency_code": "USD", "value": "100.00"},
        "due_amount": {"currency_code": "USD", "value": "50.00"},
        "payments": {
            "transactions": [
                {"payment_id": "PAY-LEGACY-50", "amount": {"currency_code": "USD", "value": "50.00"}},
            ],
        },
    }
    client = _mock_client_with_items([raw_item])
    # Caller supplies legacy allocation with currency=None (matching the 5000 cents from ingestion)
    alloc_legacy = PaymentAllocation(
        payment_id="PAY-LEGACY-50",
        invoice_id="INV-LEGACY-1",
        amount_cents=5000,
        status=AllocationStatus.APPROVED,
        currency=None,
    )

    result = reconcile_payments([], client, existing_allocations=[alloc_legacy])
    assert len(result.existing_allocations) == 1
    assert result.existing_allocations[0].amount_cents == 5000

"""Hypothesis property-based tests for financial ledger invariants and audit continuity."""

from __future__ import annotations

import tempfile
import os
import pytest
from datetime import datetime, timezone
from hypothesis import given, strategies as st, settings, HealthCheck

from reconcile_store.config import DatabaseConfig
from reconcile_store.db import DatabaseEngine
from reconcile_store.schema import Base
from reconcile_store.canonical import canonical_json, compute_content_hash
from reconcile_store.audit import (
    GENESIS_HASH,
    compute_event_hash,
    create_audit_payload,
    check_audit_chain,
)
from reconcile_store.repositories import (
    TenantRepository,
    InvoiceRepository,
    PaymentRepository,
    AllocationRepository,
    DecisionRepository,
    AuditRepository,
    ReconciliationRunRepository,
    ProposalRepository,
)
from reconcile_store.repositories.allocation_repo import LedgerConstraintError


# Property 1 & 2: Payment and Invoice Conservation
# Given an invoice amount, a payment amount, and a proposed allocation amount:
# If allocation <= payment and allocation <= invoice:
#   allocation succeeds;
#   remaining payment = payment - allocation >= 0;
#   remaining invoice = invoice - allocation >= 0.
# If allocation > payment or allocation > invoice:
#   allocation must be rejected with LedgerConstraintError; balances remain unchanged.

@given(
    inv_amount=st.integers(min_value=1, max_value=1_000_000),
    pay_amount=st.integers(min_value=1, max_value=1_000_000),
    alloc_amount=st.integers(min_value=1, max_value=2_000_000),
)
@settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_property_ledger_conservation(inv_amount: int, pay_amount: int, alloc_amount: int):
    """Property Test: Ledger capacity conservation invariants I1, I2, and I3."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    url = f"sqlite:///{path}"
    db = DatabaseEngine(DatabaseConfig(url=url))
    db.create_schema()

    with db.session() as session:
        t_repo = TenantRepository(session)
        inv_repo = InvoiceRepository(session)
        pay_repo = PaymentRepository(session)
        alloc_repo = AllocationRepository(session)
        run_repo = ReconciliationRunRepository(session)
        prop_repo = ProposalRepository(session)
        dec_repo = DecisionRepository(session)

        t_repo.create_tenant("prop_tenant", "Property Tenant")
        inv = inv_repo.upsert_invoice(
            tenant_id="prop_tenant",
            provider="paypal",
            provider_invoice_id="INV-PROP-1",
            invoice_number="INV-PROP",
            total_minor=inv_amount,
            currency="USD",
        )
        pay = pay_repo.upsert_payment(
            tenant_id="prop_tenant",
            provider="paypal",
            provider_payment_id="PAY-PROP-1",
            amount_minor=pay_amount,
            currency="USD",
        )
        run = run_repo.create_run(
            tenant_id="prop_tenant",
            input_snapshot_id="snap-prop",
            matcher_name="test_matcher",
        )
        prop = prop_repo.create_proposal(
            tenant_id="prop_tenant",
            run_id=run.id,
            payment_id=pay.id,
            outcome="PROPOSED",
        )
        dec = dec_repo.record_decision(
            tenant_id="prop_tenant",
            proposal_id=prop.id,
            decision_type="APPROVED",
            actor="test_runner",
            reason="Property testing",
            idempotency_key="key-prop-1",
        )
        session.commit()

        if alloc_amount <= pay_amount and alloc_amount <= inv_amount:
            # Must succeed
            alloc = alloc_repo.create_allocation(
                tenant_id="prop_tenant",
                payment_id=pay.id,
                invoice_id=inv.id,
                amount_minor=alloc_amount,
                currency="USD",
                decision_id=dec.id,
                actor="test_runner",
            )
            session.commit()
            unapplied = pay_repo.get_unapplied_cash_minor("prop_tenant", pay.id)
            outstanding = inv_repo.get_ledger_outstanding_minor("prop_tenant", inv.id)
            assert unapplied == pay_amount - alloc_amount
            assert outstanding == inv_amount - alloc_amount
            assert unapplied >= 0
            assert outstanding >= 0
        else:
            # Must reject
            with pytest.raises(LedgerConstraintError):
                alloc_repo.create_allocation(
                    tenant_id="prop_tenant",
                    payment_id=pay.id,
                    invoice_id=inv.id,
                    amount_minor=alloc_amount,
                    currency="USD",
                    decision_id=dec.id,
                    actor="test_runner",
                )

    db.dispose()
    if os.path.exists(path):
        os.unlink(path)


# Property 3: Currency Invariance
# For any currency pairs (curr_a, curr_b):
# If curr_a == curr_b: succeeds;
# If curr_a != curr_b: always rejected.

@given(
    pay_curr=st.sampled_from(["USD", "EUR", "GBP", "CAD", "AUD"]),
    inv_curr=st.sampled_from(["USD", "EUR", "GBP", "CAD", "AUD"]),
)
@settings(max_examples=25)
def test_property_currency_consistency(pay_curr: str, inv_curr: str):
    """Property Test: Invariant I4 strict currency equality."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    url = f"sqlite:///{path}"
    db = DatabaseEngine(DatabaseConfig(url=url))
    db.create_schema()

    with db.session() as session:
        t_repo = TenantRepository(session)
        inv_repo = InvoiceRepository(session)
        pay_repo = PaymentRepository(session)
        alloc_repo = AllocationRepository(session)
        run_repo = ReconciliationRunRepository(session)
        prop_repo = ProposalRepository(session)
        dec_repo = DecisionRepository(session)

        t_repo.create_tenant("t_curr", "Curr Tenant")
        inv = inv_repo.upsert_invoice(
            tenant_id="t_curr",
            provider="paypal",
            provider_invoice_id="INV-CURR",
            invoice_number="INV-C",
            total_minor=5000,
            currency=inv_curr,
        )
        pay = pay_repo.upsert_payment(
            tenant_id="t_curr",
            provider="paypal",
            provider_payment_id="PAY-CURR",
            amount_minor=5000,
            currency=pay_curr,
        )
        run = run_repo.create_run(tenant_id="t_curr", input_snapshot_id="s", matcher_name="m")
        prop = prop_repo.create_proposal(tenant_id="t_curr", run_id=run.id, payment_id=pay.id, outcome="PROPOSED")
        dec = dec_repo.record_decision(
            tenant_id="t_curr", proposal_id=prop.id, decision_type="APPROVED",
            actor="test", reason="test", idempotency_key="curr-key"
        )
        session.commit()

        if pay_curr == inv_curr:
            alloc = alloc_repo.create_allocation(
                tenant_id="t_curr",
                payment_id=pay.id,
                invoice_id=inv.id,
                amount_minor=5000,
                currency=pay_curr,
                decision_id=dec.id,
                actor="test",
            )
            assert alloc.currency == pay_curr
        else:
            with pytest.raises(LedgerConstraintError):
                alloc_repo.create_allocation(
                    tenant_id="t_curr",
                    payment_id=pay.id,
                    invoice_id=inv.id,
                    amount_minor=5000,
                    currency=pay_curr,
                    decision_id=dec.id,
                    actor="test",
                )

    db.dispose()
    if os.path.exists(path):
        os.unlink(path)


# Property 4: Audit Chain Continuity & Deterministic Hash
# Any chain of N events appended sequentially must have unbroken hashes and 0 errors.

@given(
    event_counts=st.integers(min_value=1, max_value=20),
)
@settings(max_examples=25)
def test_property_audit_chain_continuity(event_counts: int):
    """Property Test: Invariant I12 append-only hash-chained continuity."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    url = f"sqlite:///{path}"
    db = DatabaseEngine(DatabaseConfig(url=url))
    db.create_schema()

    with db.session() as session:
        t_repo = TenantRepository(session)
        a_repo = AuditRepository(session)
        t_repo.create_tenant("t_audit_prop", "Audit Prop Tenant")

        prev_hash = GENESIS_HASH
        for i in range(1, event_counts + 1):
            ev = a_repo.append_event(
                tenant_id="t_audit_prop",
                event_type="TEST_EVENT",
                actor="prop_test",
                subject_type="test",
                subject_id=f"id_{i}",
                details={"step": i, "token": f"token_{i}"},
            )
            assert ev.sequence_number == i
            assert ev.prev_hash == prev_hash
            prev_hash = ev.event_hash
        session.commit()

        # Chain verification must succeed
        a_repo.verify_tenant_chain("t_audit_prop")

    db.dispose()
    if os.path.exists(path):
        os.unlink(path)

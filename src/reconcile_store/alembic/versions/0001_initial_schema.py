"""Initial 12-table financial store schema.

Revision ID: 0001_initial_schema
Revises: None
Create Date: 2026-11-03 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0001_initial_schema"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. tenants
    op.create_table(
        "tenants",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )

    # 2. connections
    op.create_table(
        "connections",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("tenant_id", sa.String(length=64), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("environment", sa.String(length=32), nullable=False, server_default="sandbox"),
        sa.Column("credential_ref", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "provider", "environment", name="uq_connection_tenant_provider_env"),
    )
    op.create_index("ix_connections_tenant_id", "connections", ["tenant_id"])

    # 3. provider_records
    op.create_table(
        "provider_records",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("tenant_id", sa.String(length=64), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("record_kind", sa.String(length=32), nullable=False),
        sa.Column("provider_record_id", sa.String(length=255), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "tenant_id", "provider", "record_kind", "provider_record_id", "content_hash",
            name="uq_provider_record_tenant_kind_id_hash",
        ),
    )
    op.create_index("ix_provider_records_tenant_id", "provider_records", ["tenant_id"])
    op.create_index(
        "ix_provider_record_lookup", "provider_records",
        ["tenant_id", "provider", "record_kind", "provider_record_id"],
    )

    # 4. invoices
    op.create_table(
        "invoices",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("tenant_id", sa.String(length=64), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False, server_default="paypal"),
        sa.Column("provider_invoice_id", sa.String(length=255), nullable=False),
        sa.Column("invoice_number", sa.String(length=128), nullable=False),
        sa.Column("customer_name", sa.String(length=255), nullable=False),
        sa.Column("customer_email", sa.String(length=255), nullable=True),
        sa.Column("total_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("provider_due_minor", sa.BigInteger(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="SENT"),
        sa.Column("source_record_id", sa.String(length=64), sa.ForeignKey("provider_records.id", ondelete="SET NULL"), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("issued_on", sa.Date(), nullable=False),
        sa.Column("due_on", sa.Date(), nullable=False),
        sa.Column("memo", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "provider", "provider_invoice_id", name="uq_invoice_tenant_provider_id"),
    )
    op.create_index("ix_invoices_tenant_id", "invoices", ["tenant_id"])
    op.create_index("ix_invoice_tenant_number", "invoices", ["tenant_id", "invoice_number"])

    # 5. payments
    op.create_table(
        "payments",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("tenant_id", sa.String(length=64), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False, server_default="paypal"),
        sa.Column("provider_payment_id", sa.String(length=255), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="COMPLETED"),
        sa.Column("payer_name", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("payer_email", sa.String(length=255), nullable=True),
        sa.Column("reference", sa.String(length=255), nullable=True),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_record_id", sa.String(length=64), sa.ForeignKey("provider_records.id", ondelete="SET NULL"), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "provider", "provider_payment_id", name="uq_payment_tenant_provider_id"),
    )
    op.create_index("ix_payments_tenant_id", "payments", ["tenant_id"])
    op.create_index("ix_payment_tenant_reference", "payments", ["tenant_id", "reference"])

    # 6. reconciliation_runs
    op.create_table(
        "reconciliation_runs",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("tenant_id", sa.String(length=64), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("input_snapshot_id", sa.String(length=64), nullable=False),
        sa.Column("matcher_name", sa.String(length=64), nullable=False),
        sa.Column("matcher_version", sa.String(length=32), nullable=False, server_default="1.0.0"),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="COMPLETED"),
        sa.Column("metrics_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("config_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("run_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_reconciliation_runs_tenant_id", "reconciliation_runs", ["tenant_id"])

    # 7. proposals
    op.create_table(
        "proposals",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("tenant_id", sa.String(length=64), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("run_id", sa.String(length=64), sa.ForeignKey("reconciliation_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("payment_id", sa.String(length=64), sa.ForeignKey("payments.id", ondelete="CASCADE"), nullable=False),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column("evidence_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("source_hashes_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("verifier_status", sa.String(length=32), nullable=False, server_default="NOT_VERIFIED"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_proposals_tenant_id", "proposals", ["tenant_id"])
    op.create_index("ix_proposals_run_id", "proposals", ["run_id"])
    op.create_index("ix_proposals_payment_id", "proposals", ["payment_id"])

    # 8. proposal_allocations
    op.create_table(
        "proposal_allocations",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("tenant_id", sa.String(length=64), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("proposal_id", sa.String(length=64), sa.ForeignKey("proposals.id", ondelete="CASCADE"), nullable=False),
        sa.Column("payment_id", sa.String(length=64), sa.ForeignKey("payments.id", ondelete="CASCADE"), nullable=False),
        sa.Column("invoice_id", sa.String(length=64), sa.ForeignKey("invoices.id", ondelete="CASCADE"), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("evidence_json", sa.Text(), nullable=False, server_default="[]"),
    )
    op.create_index("ix_proposal_allocations_tenant_id", "proposal_allocations", ["tenant_id"])
    op.create_index("ix_proposal_allocations_proposal_id", "proposal_allocations", ["proposal_id"])
    op.create_index("ix_proposal_allocations_payment_id", "proposal_allocations", ["payment_id"])
    op.create_index("ix_proposal_allocations_invoice_id", "proposal_allocations", ["invoice_id"])

    # 9. verifications
    op.create_table(
        "verifications",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("tenant_id", sa.String(length=64), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("proposal_id", sa.String(length=64), sa.ForeignKey("proposals.id", ondelete="CASCADE"), nullable=False),
        sa.Column("check_name", sa.String(length=64), nullable=False),
        sa.Column("check_inputs_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("passed", sa.Boolean(), nullable=False),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("source_hashes_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_verifications_tenant_id", "verifications", ["tenant_id"])
    op.create_index("ix_verifications_proposal_id", "verifications", ["proposal_id"])

    # 10. decisions
    op.create_table(
        "decisions",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("tenant_id", sa.String(length=64), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("proposal_id", sa.String(length=64), sa.ForeignKey("proposals.id", ondelete="CASCADE"), nullable=False),
        sa.Column("decision_type", sa.String(length=32), nullable=False),
        sa.Column("actor", sa.String(length=255), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "idempotency_key", name="uq_decision_tenant_idempotency_key"),
    )
    op.create_index("ix_decisions_tenant_id", "decisions", ["tenant_id"])
    op.create_index("ix_decisions_proposal_id", "decisions", ["proposal_id"])

    # 11. allocations (application-owned financial ledger)
    op.create_table(
        "allocations",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("tenant_id", sa.String(length=64), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("payment_id", sa.String(length=64), sa.ForeignKey("payments.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("invoice_id", sa.String(length=64), sa.ForeignKey("invoices.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("decision_id", sa.String(length=64), sa.ForeignKey("decisions.id", ondelete="SET NULL"), nullable=True),
        sa.Column("actor", sa.String(length=255), nullable=False),
        sa.Column("evidence_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("allocated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "payment_id", "invoice_id", name="uq_allocation_tenant_pay_inv"),
    )
    op.create_index("ix_allocations_tenant_id", "allocations", ["tenant_id"])
    op.create_index("ix_allocations_payment_id", "allocations", ["payment_id"])
    op.create_index("ix_allocations_invoice_id", "allocations", ["invoice_id"])

    # 12. audit_events
    op.create_table(
        "audit_events",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("tenant_id", sa.String(length=64), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("sequence_number", sa.BigInteger(), nullable=False),
        sa.Column("event_id", sa.String(length=64), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("actor", sa.String(length=255), nullable=False),
        sa.Column("subject_type", sa.String(length=64), nullable=False),
        sa.Column("subject_id", sa.String(length=64), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("prev_hash", sa.String(length=64), nullable=False),
        sa.Column("event_hash", sa.String(length=64), nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "sequence_number", name="uq_audit_event_tenant_sequence"),
        sa.UniqueConstraint("tenant_id", "event_hash", name="uq_audit_event_tenant_hash"),
    )
    op.create_index("ix_audit_events_tenant_id", "audit_events", ["tenant_id"])
    op.create_index("ix_audit_event_lookup", "audit_events", ["tenant_id", "sequence_number"])


def downgrade() -> None:
    op.drop_table("audit_events")
    op.drop_table("allocations")
    op.drop_table("decisions")
    op.drop_table("verifications")
    op.drop_table("proposal_allocations")
    op.drop_table("proposals")
    op.drop_table("reconciliation_runs")
    op.drop_table("payments")
    op.drop_table("invoices")
    op.drop_table("provider_records")
    op.drop_table("connections")
    op.drop_table("tenants")

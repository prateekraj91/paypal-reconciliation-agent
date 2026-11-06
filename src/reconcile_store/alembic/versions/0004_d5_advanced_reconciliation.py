"""D5 Advanced Reconciliation: Refunds and Ambiguity / Overpayment Proposal extensions.

Revision ID: 0004_d5_advanced_reconciliation
Revises: 0003_d4_provider_settlements
Create Date: 2026-11-06 00:00:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0004_d5_advanced_reconciliation"
down_revision: Union[str, None] = "0003_d4_provider_settlements"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Create refunds table
    op.create_table(
        "refunds",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("tenant_id", sa.String(length=64), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False, server_default="paypal"),
        sa.Column("provider_refund_id", sa.String(length=255), nullable=False),
        sa.Column("parent_payment_id", sa.String(length=64), sa.ForeignKey("payments.id", ondelete="CASCADE"), nullable=False),
        sa.Column("invoice_id", sa.String(length=64), sa.ForeignKey("invoices.id", ondelete="SET NULL"), nullable=True),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="COMPLETED"),
        sa.Column("reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("record_type", sa.String(length=32), nullable=False, server_default="REFUND"),
        sa.Column("target_type", sa.String(length=32), nullable=False, server_default="UNKNOWN"),
        sa.Column("settlement_id", sa.String(length=64), sa.ForeignKey("provider_settlements.id", ondelete="SET NULL"), nullable=True),
        sa.Column("allocation_id", sa.String(length=64), sa.ForeignKey("allocations.id", ondelete="SET NULL"), nullable=True),
        sa.Column("dispute_id", sa.String(length=128), nullable=True),
        sa.Column("source_record_id", sa.String(length=64), sa.ForeignKey("provider_records.id", ondelete="SET NULL"), nullable=True),
        sa.Column("refunded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "tenant_id", "provider", "provider_refund_id",
            name="uq_refund_tenant_provider_id",
        ),
    )
    op.create_index("ix_refunds_tenant_id", "refunds", ["tenant_id"])
    op.create_index("ix_refunds_parent_payment_id", "refunds", ["tenant_id", "parent_payment_id"])
    op.create_index("ix_refunds_invoice_id", "refunds", ["tenant_id", "invoice_id"])

    # 2. Add excess_amount_minor and ambiguity_certificate_json to proposals
    with op.batch_alter_table("proposals") as batch_op:
        batch_op.add_column(
            sa.Column("excess_amount_minor", sa.BigInteger(), nullable=False, server_default="0")
        )
        batch_op.add_column(
            sa.Column("ambiguity_certificate_json", sa.Text(), nullable=True)
        )


def downgrade() -> None:
    # 1. Remove columns from proposals
    with op.batch_alter_table("proposals") as batch_op:
        batch_op.drop_column("ambiguity_certificate_json")
        batch_op.drop_column("excess_amount_minor")

    # 2. Drop refunds table
    op.drop_index("ix_refunds_invoice_id", table_name="refunds")
    op.drop_index("ix_refunds_parent_payment_id", table_name="refunds")
    op.drop_index("ix_refunds_tenant_id", table_name="refunds")
    op.drop_table("refunds")

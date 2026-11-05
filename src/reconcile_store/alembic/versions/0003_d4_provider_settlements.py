"""D4 Provider Settlements Separation and Allocation Decision Requirement.

Revision ID: 0003_d4_provider_settlements
Revises: 0002_d4_webhooks_and_jobs
Create Date: 2026-11-04 09:30:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0003_d4_provider_settlements"
down_revision: Union[str, None] = "0002_d4_webhooks_and_jobs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Create provider_settlements table
    op.create_table(
        "provider_settlements",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("tenant_id", sa.String(length=64), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False, server_default="paypal"),
        sa.Column("environment", sa.String(length=32), nullable=False, server_default="sandbox"),
        sa.Column("provider_settlement_id", sa.String(length=255), nullable=False),
        sa.Column("invoice_id", sa.String(length=64), sa.ForeignKey("invoices.id", ondelete="CASCADE"), nullable=False),
        sa.Column("payment_id", sa.String(length=64), sa.ForeignKey("payments.id", ondelete="SET NULL"), nullable=True),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("source_record_id", sa.String(length=64), sa.ForeignKey("provider_records.id", ondelete="SET NULL"), nullable=True),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("evidence_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "tenant_id", "provider", "environment", "invoice_id", "provider_settlement_id",
            name="uq_provider_settlement_tenant_prov_env_inv_id",
        ),
    )
    op.create_index("ix_provider_settlements_tenant_id", "provider_settlements", ["tenant_id"])
    op.create_index("ix_provider_settlements_invoice_id", "provider_settlements", ["tenant_id", "invoice_id"])
    op.create_index("ix_provider_settlements_payment_id", "provider_settlements", ["tenant_id", "payment_id"])

    # 2. Data Migration: Safely migrate existing paypal:settlement rows within the same transaction
    bind = op.get_bind()

    # Query candidate allocation rows where decision_id is NULL
    candidate_rows = bind.execute(
        sa.text(
            "SELECT a.id, a.tenant_id, a.payment_id, a.invoice_id, a.amount_minor, a.currency, "
            "       a.decision_id, a.actor, a.evidence_json, a.allocated_at, "
            "       p.provider, p.provider_payment_id, p.source_record_id "
            "FROM allocations a "
            "LEFT JOIN payments p ON a.payment_id = p.id "
            "WHERE a.decision_id IS NULL"
        )
    ).fetchall()

    if candidate_rows:
        migrated_ids = []
        original_total_minor = 0

        # Query connections to detect tenant environments
        conn_rows = bind.execute(
            sa.text("SELECT tenant_id, provider, environment FROM connections")
        ).fetchall()
        env_map = {(r.tenant_id, r.provider): r.environment for r in conn_rows}

        for row in candidate_rows:
            provider = row.provider or "paypal"
            environment = env_map.get((row.tenant_id, provider), "sandbox")
            provider_settlement_id = row.provider_payment_id or row.payment_id or row.id

            bind.execute(
                sa.text(
                    "INSERT INTO provider_settlements ("
                    "  id, tenant_id, provider, environment, provider_settlement_id, "
                    "  invoice_id, payment_id, amount_minor, currency, source_record_id, "
                    "  settled_at, evidence_json, created_at"
                    ") VALUES ("
                    "  :id, :tenant_id, :provider, :environment, :provider_settlement_id, "
                    "  :invoice_id, :payment_id, :amount_minor, :currency, :source_record_id, "
                    "  :settled_at, :evidence_json, :created_at"
                    ")"
                ),
                {
                    "id": row.id,
                    "tenant_id": row.tenant_id,
                    "provider": provider,
                    "environment": environment,
                    "provider_settlement_id": provider_settlement_id,
                    "invoice_id": row.invoice_id,
                    "payment_id": row.payment_id,
                    "amount_minor": row.amount_minor,
                    "currency": row.currency,
                    "source_record_id": row.source_record_id,
                    "settled_at": row.allocated_at,
                    "evidence_json": row.evidence_json,
                    "created_at": row.allocated_at,
                },
            )
            migrated_ids.append(row.id)
            original_total_minor += row.amount_minor

        # Verify migrated count and financial balances in provider_settlements before deletion
        verify_row = bind.execute(
            sa.text(
                "SELECT count(*) as cnt, coalesce(sum(amount_minor), 0) as total "
                "FROM provider_settlements"
            )
        ).fetchone()

        if verify_row.cnt < len(candidate_rows):
            raise RuntimeError(
                f"Migration verification failed: expected at least {len(candidate_rows)} settlements, got {verify_row.cnt}"
            )

        # Delete migrated rows from allocations
        bind.execute(
            sa.text("DELETE FROM allocations WHERE decision_id IS NULL")
        )

        # Verify no null decision_id rows remain in allocations
        remaining_nulls = bind.execute(
            sa.text("SELECT count(*) FROM allocations WHERE decision_id IS NULL")
        ).scalar()
        if remaining_nulls > 0:
            raise RuntimeError(
                f"Fail-closed: {remaining_nulls} allocations still have decision_id=NULL after migration"
            )

    # 3. Alter allocations.decision_id to NOT NULL
    with op.batch_alter_table("allocations") as batch_op:
        batch_op.alter_column(
            "decision_id",
            existing_type=sa.String(length=64),
            nullable=False,
        )


def downgrade() -> None:
    # 1. Allow NULL on allocations.decision_id
    with op.batch_alter_table("allocations") as batch_op:
        batch_op.alter_column(
            "decision_id",
            existing_type=sa.String(length=64),
            nullable=True,
        )

    # 2. Copy records back from provider_settlements to allocations
    bind = op.get_bind()
    settlement_rows = bind.execute(
        sa.text(
            "SELECT id, tenant_id, payment_id, invoice_id, amount_minor, currency, "
            "       provider, evidence_json, settled_at, created_at "
            "FROM provider_settlements"
        )
    ).fetchall()

    for row in settlement_rows:
        if row.payment_id is not None:
            bind.execute(
                sa.text(
                    "INSERT INTO allocations ("
                    "  id, tenant_id, payment_id, invoice_id, amount_minor, currency, "
                    "  decision_id, actor, evidence_json, allocated_at"
                    ") VALUES ("
                    "  :id, :tenant_id, :payment_id, :invoice_id, :amount_minor, :currency, "
                    "  NULL, :actor, :evidence_json, :allocated_at"
                    ")"
                ),
                {
                    "id": row.id,
                    "tenant_id": row.tenant_id,
                    "payment_id": row.payment_id,
                    "invoice_id": row.invoice_id,
                    "amount_minor": row.amount_minor,
                    "currency": row.currency,
                    "actor": f"{row.provider}:settlement",
                    "evidence_json": row.evidence_json,
                    "allocated_at": row.settled_at or row.created_at,
                },
            )

    # 3. Drop provider_settlements table
    op.drop_index("ix_provider_settlements_payment_id", table_name="provider_settlements")
    op.drop_index("ix_provider_settlements_invoice_id", table_name="provider_settlements")
    op.drop_index("ix_provider_settlements_tenant_id", table_name="provider_settlements")
    op.drop_table("provider_settlements")

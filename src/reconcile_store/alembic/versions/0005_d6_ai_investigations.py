"""D6 AI Exception Investigator: Investigations persistence table.

Revision ID: 0005_d6_ai_investigations
Revises: 0004_d5_advanced_reconciliation
Create Date: 2026-11-06 12:00:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0005_d6_ai_investigations"
down_revision: Union[str, None] = "0004_d5_advanced_reconciliation"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "investigations",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("tenant_id", sa.String(length=64), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("target_type", sa.String(length=32), nullable=False),
        sa.Column("target_id", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="QUEUED"),
        sa.Column("classification", sa.String(length=64), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("result_json", sa.Text(), nullable=True),
        sa.Column("evidence_references_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("verifier_status", sa.String(length=32), nullable=False, server_default="NOT_VERIFIED"),
        sa.Column("verifier_details_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("model_provider", sa.String(length=32), nullable=True),
        sa.Column("model_name", sa.String(length=64), nullable=True),
        sa.Column("prompt_version", sa.String(length=32), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_investigations_tenant_id", "investigations", ["tenant_id"])
    op.create_index("ix_investigations_target_id", "investigations", ["target_id"])
    op.create_index("ix_investigations_tenant_status", "investigations", ["tenant_id", "status"])
    op.create_index("ix_investigations_tenant_target", "investigations", ["tenant_id", "target_type", "target_id"])


def downgrade() -> None:
    op.drop_index("ix_investigations_tenant_target", table_name="investigations")
    op.drop_index("ix_investigations_tenant_status", table_name="investigations")
    op.drop_index("ix_investigations_target_id", table_name="investigations")
    op.drop_index("ix_investigations_tenant_id", table_name="investigations")
    op.drop_table("investigations")

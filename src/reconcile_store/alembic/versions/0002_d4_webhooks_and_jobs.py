"""D4 Webhook Inbox and Background Job Queue Schema.

Revision ID: 0002_d4_webhooks_and_jobs
Revises: 0001_initial_schema
Create Date: 2026-11-03 16:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0002_d4_webhooks_and_jobs"
down_revision: Union[str, None] = "0001_initial_schema"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. jobs table (background job queue)
    op.create_table(
        "jobs",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("tenant_id", sa.String(length=64), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("job_type", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="QUEUED"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("worker_id", sa.String(length=64), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("tenant_id", "idempotency_key", name="uq_jobs_tenant_idempotency_key"),
    )
    op.create_index("ix_jobs_tenant_id", "jobs", ["tenant_id"])
    op.create_index("ix_jobs_queue_poll", "jobs", ["status", "available_at"])
    op.create_index("ix_jobs_tenant_status", "jobs", ["tenant_id", "status"])

    # 2. webhook_inbox table (durable incoming webhook receipts)
    op.create_table(
        "webhook_inbox",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("tenant_id", sa.String(length=64), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False, server_default="paypal"),
        sa.Column("environment", sa.String(length=32), nullable=False, server_default="sandbox"),
        sa.Column("event_id", sa.String(length=128), nullable=False),
        sa.Column("webhook_id", sa.String(length=128), nullable=True),
        sa.Column("event_type", sa.String(length=128), nullable=False),
        sa.Column("resource_type", sa.String(length=64), nullable=True),
        sa.Column("resource_id", sa.String(length=128), nullable=True),
        sa.Column("raw_payload", sa.Text(), nullable=False),
        sa.Column("headers_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("verification_status", sa.String(length=32), nullable=False, server_default="RECEIVED"),
        sa.Column("processing_status", sa.String(length=32), nullable=False, server_default="RECEIVED"),
        sa.Column("job_id", sa.String(length=64), sa.ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "tenant_id", "provider", "environment", "event_id",
            name="uq_webhook_inbox_tenant_prov_env_evt"
        ),
    )
    op.create_index("ix_webhook_inbox_tenant_id", "webhook_inbox", ["tenant_id"])
    op.create_index("ix_webhook_inbox_lookup", "webhook_inbox", ["tenant_id", "processing_status"])


def downgrade() -> None:
    op.drop_index("ix_webhook_inbox_lookup", table_name="webhook_inbox")
    op.drop_index("ix_webhook_inbox_tenant_id", table_name="webhook_inbox")
    op.drop_table("webhook_inbox")

    op.drop_index("ix_jobs_tenant_status", table_name="jobs")
    op.drop_index("ix_jobs_queue_poll", table_name="jobs")
    op.drop_index("ix_jobs_tenant_id", table_name="jobs")
    op.drop_table("jobs")

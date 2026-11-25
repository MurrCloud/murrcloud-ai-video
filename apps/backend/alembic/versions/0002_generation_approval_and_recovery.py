"""Bind approvals, immutable inputs and durable generation recovery state.

Revision ID: 0002
Revises: 0001
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def document():
    return sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def upgrade():
    with op.batch_alter_table("projects") as batch:
        batch.drop_constraint(op.f("ck_projects_status_values"), type_="check")
        batch.create_check_constraint(
            op.f("ck_projects_status_values"),
            "status IN ('draft', 'planning', 'awaiting_approval', 'generating', 'clips_ready', 'narrating', 'rendering', 'completed', 'failed', 'cancelled')",
        )
    with op.batch_alter_table("cost_estimates") as batch:
        batch.add_column(sa.Column("base_usd", sa.Numeric(14, 6), nullable=True))
        batch.add_column(
            sa.Column(
                "pricing_verified", sa.Boolean(), server_default=sa.text("false"), nullable=False
            )
        )
        batch.add_column(
            sa.Column(
                "pricing_source", sa.String(128), server_default="unavailable", nullable=False
            )
        )
        for name in ("request_snapshot", "model_snapshot", "context_snapshot"):
            batch.add_column(sa.Column(name, document(), server_default="{}", nullable=False))
        batch.add_column(sa.Column("scene_id", sa.Uuid(), nullable=True))
        batch.add_column(sa.Column("scene_version_number", sa.Integer(), nullable=True))
        batch.create_foreign_key(
            op.f("fk_cost_estimates_scene_id_scenes"),
            "scenes",
            ["scene_id"],
            ["id"],
            ondelete="RESTRICT",
        )
    with op.batch_alter_table("generation_jobs") as batch:
        batch.add_column(sa.Column("estimate_id", sa.Uuid(), nullable=True))
        batch.create_foreign_key(
            op.f("fk_generation_jobs_estimate_id_cost_estimates"),
            "cost_estimates",
            ["estimate_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch.create_unique_constraint(op.f("uq_generation_jobs_estimate_id"), ["estimate_id"])
        for name in ("model_snapshot", "submission_snapshot"):
            batch.add_column(sa.Column(name, document(), server_default="{}", nullable=False))
        batch.add_column(
            sa.Column("reserved_cost_usd", sa.Numeric(14, 6), server_default="0", nullable=False)
        )
        batch.add_column(sa.Column("approved_by_id", sa.Uuid(), nullable=True))
        batch.create_foreign_key(
            op.f("fk_generation_jobs_approved_by_id_users"),
            "users",
            ["approved_by_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch.add_column(sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("approval_fingerprint", sa.String(64), nullable=True))
        batch.add_column(sa.Column("output_asset_id", sa.Uuid(), nullable=True))
        batch.create_foreign_key(
            op.f("fk_generation_jobs_output_asset_id_media_assets"),
            "media_assets",
            ["output_asset_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch.add_column(sa.Column("poll_count", sa.Integer(), server_default="0", nullable=False))
        batch.add_column(
            sa.Column("dispatch_state", sa.String(20), server_default="pending", nullable=False)
        )
        batch.add_column(sa.Column("dispatch_error_code", sa.String(100), nullable=True))
        batch.add_column(sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("operation_token", sa.Uuid(), nullable=True))
        batch.add_column(
            sa.Column("operation_started_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch.add_column(
            sa.Column("cancel_attempted_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch.create_check_constraint(
            op.f("ck_generation_jobs_reservation_nonnegative"), "reserved_cost_usd >= 0"
        )
        batch.create_check_constraint(
            op.f("ck_generation_jobs_poll_count_nonnegative"), "poll_count >= 0"
        )
        batch.create_check_constraint(
            op.f("ck_generation_jobs_dispatch_state_values"),
            "dispatch_state IN ('pending', 'queued', 'error', 'not_needed')",
        )


def downgrade():
    with op.batch_alter_table("generation_jobs") as batch:
        for name in (
            "ck_generation_jobs_dispatch_state_values",
            "ck_generation_jobs_poll_count_nonnegative",
            "ck_generation_jobs_reservation_nonnegative",
        ):
            batch.drop_constraint(op.f(name), type_="check")
        batch.drop_constraint(
            op.f("fk_generation_jobs_output_asset_id_media_assets"), type_="foreignkey"
        )
        batch.drop_constraint(op.f("fk_generation_jobs_approved_by_id_users"), type_="foreignkey")
        batch.drop_constraint(op.f("uq_generation_jobs_estimate_id"), type_="unique")
        batch.drop_constraint(
            op.f("fk_generation_jobs_estimate_id_cost_estimates"), type_="foreignkey"
        )
        for name in (
            "cancel_attempted_at",
            "operation_started_at",
            "operation_token",
            "dispatched_at",
            "dispatch_error_code",
            "dispatch_state",
            "poll_count",
            "output_asset_id",
            "approval_fingerprint",
            "approved_at",
            "approved_by_id",
            "reserved_cost_usd",
            "submission_snapshot",
            "model_snapshot",
            "estimate_id",
        ):
            batch.drop_column(name)
    with op.batch_alter_table("cost_estimates") as batch:
        batch.drop_constraint(op.f("fk_cost_estimates_scene_id_scenes"), type_="foreignkey")
        for name in (
            "scene_version_number",
            "scene_id",
            "context_snapshot",
            "model_snapshot",
            "request_snapshot",
            "pricing_source",
            "pricing_verified",
            "base_usd",
        ):
            batch.drop_column(name)
    op.execute("UPDATE projects SET status = 'draft' WHERE status = 'clips_ready'")
    with op.batch_alter_table("projects") as batch:
        batch.drop_constraint(op.f("ck_projects_status_values"), type_="check")
        batch.create_check_constraint(
            op.f("ck_projects_status_values"),
            "status IN ('draft', 'planning', 'awaiting_approval', 'generating', 'narrating', 'rendering', 'completed', 'failed', 'cancelled')",
        )

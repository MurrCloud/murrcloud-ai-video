"""Production render snapshots, leases and exact narration cache.

Revision ID: 0004
Revises: 0003
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None
DOCUMENT = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade():
    with op.batch_alter_table("narration_assets") as batch:
        batch.add_column(sa.Column("fingerprint", sa.String(64), nullable=True))
        batch.add_column(
            sa.Column("speaking_rate", sa.Numeric(4, 2), server_default="1", nullable=False)
        )
        batch.create_unique_constraint("uq_narration_assets_fingerprint", ["fingerprint"])
    with op.batch_alter_table("rendering_jobs") as batch:
        for column in (
            sa.Column("requested_by_id", sa.Uuid(), nullable=True),
            sa.Column("request_fingerprint", sa.String(64), nullable=True),
            sa.Column("stage", sa.String(24), server_default="narration", nullable=False),
            sa.Column("progress_percent", sa.Integer(), server_default="0", nullable=False),
            sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
            sa.Column("lease_token", sa.String(64), nullable=True),
            sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("dispatch_state", sa.String(20), server_default="pending", nullable=False),
            sa.Column("dispatch_error_code", sa.String(100), nullable=True),
            sa.Column("subtitle_asset_id", sa.Uuid(), nullable=True),
            sa.Column("warnings", DOCUMENT, server_default="[]", nullable=False),
            sa.Column("validation_report", DOCUMENT, server_default="{}", nullable=False),
        ):
            batch.add_column(column)
        batch.create_foreign_key(
            "fk_rendering_jobs_requested_by_id_users",
            "users",
            ["requested_by_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch.create_foreign_key(
            "fk_rendering_jobs_subtitle_asset_id_media_assets",
            "media_assets",
            ["subtitle_asset_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch.create_check_constraint(
            op.f("ck_rendering_jobs_render_progress_bounds"),
            "attempt_count >= 0 AND progress_percent >= 0 AND progress_percent <= 100",
        )


def downgrade():
    with op.batch_alter_table("rendering_jobs") as batch:
        batch.drop_constraint(op.f("ck_rendering_jobs_render_progress_bounds"), type_="check")
        batch.drop_constraint("fk_rendering_jobs_requested_by_id_users", type_="foreignkey")
        batch.drop_constraint(
            "fk_rendering_jobs_subtitle_asset_id_media_assets", type_="foreignkey"
        )
        for name in (
            "validation_report",
            "warnings",
            "subtitle_asset_id",
            "dispatch_error_code",
            "dispatch_state",
            "next_attempt_at",
            "lease_expires_at",
            "lease_token",
            "attempt_count",
            "progress_percent",
            "stage",
            "request_fingerprint",
            "requested_by_id",
        ):
            batch.drop_column(name)
    with op.batch_alter_table("narration_assets") as batch:
        batch.drop_constraint("uq_narration_assets_fingerprint", type_="unique")
        batch.drop_column("speaking_rate")
        batch.drop_column("fingerprint")

"""Authenticated encrypted, inactive provider credentials.

Revision ID: 0006
Revises: 0005
"""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    table = op.create_table(
        "provider_secrets",
        sa.Column("provider", sa.String(length=16), nullable=False),
        sa.Column("encrypted_value", sa.Text(), nullable=True),
        sa.Column("revision", sa.Integer(), server_default="0", nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        sa.CheckConstraint(
            "provider IN ('openai', 'fal')", name=op.f("ck_provider_secrets_provider_values")
        ),
        sa.CheckConstraint("revision >= 0", name=op.f("ck_provider_secrets_revision_nonnegative")),
        sa.CheckConstraint(
            "encrypted_value IS NULL OR length(encrypted_value) <= 2048",
            name=op.f("ck_provider_secrets_ciphertext_bounded"),
        ),
        sa.ForeignKeyConstraint(
            ["updated_by"],
            ["users.id"],
            name=op.f("fk_provider_secrets_updated_by_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("provider", name=op.f("pk_provider_secrets")),
    )
    op.bulk_insert(
        table, [{"provider": "openai", "revision": 0}, {"provider": "fal", "revision": 0}]
    )


def downgrade() -> None:
    op.drop_table("provider_secrets")

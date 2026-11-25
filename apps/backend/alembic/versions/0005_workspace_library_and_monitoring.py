"""workspace library and monitoring

Revision ID: 0005
Revises: 0004
"""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "worker_heartbeats",
        sa.Column("worker_id", sa.String(length=128), nullable=False),
        sa.Column("queue", sa.String(length=24), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_worker_heartbeats")),
        sa.UniqueConstraint("worker_id", name=op.f("uq_worker_heartbeats_worker_id")),
    )
    op.create_index(
        op.f("ix_worker_heartbeats_last_seen_at"),
        "worker_heartbeats",
        ["last_seen_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_worker_heartbeats_queue"), "worker_heartbeats", ["queue"], unique=False
    )
    op.create_table(
        "workspace_mutations",
        sa.Column("actor_user_id", sa.Uuid(), nullable=False),
        sa.Column("operation", sa.String(length=40), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("result_project_id", sa.Uuid(), nullable=True),
        sa.Column("result_template_id", sa.Uuid(), nullable=True),
        sa.Column("result_usage_cost_id", sa.Uuid(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["users.id"],
            name=op.f("fk_workspace_mutations_actor_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["result_project_id"],
            ["projects.id"],
            name=op.f("fk_workspace_mutations_result_project_id_projects"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["result_template_id"],
            ["project_templates.id"],
            name=op.f("fk_workspace_mutations_result_template_id_project_templates"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["result_usage_cost_id"],
            ["actual_usage_costs.id"],
            name=op.f("fk_workspace_mutations_result_usage_cost_id_actual_usage_costs"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workspace_mutations")),
        sa.UniqueConstraint(
            "actor_user_id",
            "operation",
            "idempotency_key",
            name=op.f("uq_workspace_mutations_actor_user_id_operation_idempotency_key"),
        ),
    )
    op.create_index(
        op.f("ix_workspace_mutations_actor_user_id"),
        "workspace_mutations",
        ["actor_user_id"],
        unique=False,
    )
    op.add_column(
        "project_templates",
        sa.Column("archived", sa.Boolean(), server_default=sa.text("(false)"), nullable=False),
    )
    op.add_column(
        "project_templates", sa.Column("revision", sa.Integer(), server_default="1", nullable=False)
    )
    op.add_column(
        "projects",
        sa.Column("archived", sa.Boolean(), server_default=sa.text("(false)"), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("projects", "archived")
    op.drop_column("project_templates", "revision")
    op.drop_column("project_templates", "archived")
    op.drop_index(op.f("ix_workspace_mutations_actor_user_id"), table_name="workspace_mutations")
    op.drop_table("workspace_mutations")
    op.drop_index(op.f("ix_worker_heartbeats_queue"), table_name="worker_heartbeats")
    op.drop_index(op.f("ix_worker_heartbeats_last_seen_at"), table_name="worker_heartbeats")
    op.drop_table("worker_heartbeats")

"""Init: pgvector extension, app role, and append-only decisions_log (C10).

Revision ID: 0001
Revises:
Create Date: 2026-10-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The API connects as this role. It does not own any table, so it cannot
# disable triggers, alter or drop tables, or switch off replication triggers.
# The role is cluster-wide: created NOLOGIN here if missing; ops/db-init gives
# it a password in the compose stack. Downgrade revokes but never drops it.
APP_ROLE = "jokr_app"


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "decisions_log",
        sa.Column("id", sa.BigInteger, sa.Identity(always=True), primary_key=True),
        sa.Column("agent", sa.Text, nullable=False),
        sa.Column("action", sa.Text, nullable=False),
        sa.Column("reason", sa.Text, nullable=False),
        sa.Column(
            "evidence_ids",
            postgresql.ARRAY(sa.BigInteger),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_decisions_log_ts", "decisions_log", ["ts"])

    # C10 lives in the database, not the app. Together with the owner/app role
    # split, no API code path can rewrite history; only a migration can.
    op.execute(
        """
        CREATE FUNCTION decisions_log_append_only() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'decisions_log is append-only (C10): % blocked', TG_OP
                USING ERRCODE = 'insufficient_privilege';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER decisions_log_no_update_delete
        BEFORE UPDATE OR DELETE ON decisions_log
        FOR EACH ROW EXECUTE FUNCTION decisions_log_append_only()
        """
    )
    op.execute(
        """
        CREATE TRIGGER decisions_log_no_truncate
        BEFORE TRUNCATE ON decisions_log
        FOR EACH STATEMENT EXECUTE FUNCTION decisions_log_append_only()
        """
    )

    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
                CREATE ROLE {APP_ROLE} NOLOGIN;
            END IF;
        -- Roles are cluster-wide: two databases migrating at once can both pass
        -- the check above, and the second CREATE then finds the role made.
        EXCEPTION WHEN duplicate_object OR unique_violation THEN
            NULL;
        END
        $$
        """
    )
    op.execute(f"GRANT USAGE ON SCHEMA public TO {APP_ROLE}")
    op.execute(f"GRANT SELECT, INSERT ON decisions_log TO {APP_ROLE}")


def downgrade() -> None:
    op.execute(f"REVOKE USAGE ON SCHEMA public FROM {APP_ROLE}")
    op.drop_table("decisions_log")  # drops its triggers too
    op.execute("DROP FUNCTION decisions_log_append_only()")
    op.execute("DROP EXTENSION IF EXISTS vector")

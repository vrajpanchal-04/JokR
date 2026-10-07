"""Scout: sources mirror, insert-only signals, per-source runs, and the jokr_scout role.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-05
"""

from collections.abc import Sequence
from datetime import datetime

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "jokr_app"
# Scout's own login. Like jokr_app it owns nothing, so it cannot alter tables
# or grants. Created NOLOGIN here if ops/db-init has not already made it.
SCOUT_ROLE = "jokr_scout"
# Column grants: a new run takes every other value from its defaults (status
# 'running', zero counts, started_at now()), so Scout cannot backdate or pre-fill one.
RUN_INSERTABLE = ("agent", "source_id")
# The only run columns Scout may change, and only while the run is still running.
RUN_UPDATABLE = ("status", "finished_at", "n_fetched", "n_new", "n_skipped", "error", "rejections")
# Everything except id, fetched_at and trust, which always come from the defaults.
SIGNAL_INSERTABLE = (
    "source_id",
    "run_id",
    "external_id",
    "url",
    "url_canonical",
    "locator",
    "title",
    "text",
    "author_hash",
    "points",
    "num_comments",
    "posted_at",
    "content_hash",
    "flags",
    "raw",
    "intent_score",
    "intent_terms",
    "intent_version",
)

HEX64 = "'^[0-9a-f]{64}$'"


def _id() -> sa.Column[int]:
    return sa.Column("id", sa.BigInteger, sa.Identity(always=True), primary_key=True)


def _now(name: str) -> sa.Column[datetime]:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())


def _created_sources() -> None:
    op.create_table(
        "sources",
        _id(),
        sa.Column("name", sa.Text, nullable=False, unique=True),
        sa.Column("type", sa.Text, nullable=False),
        sa.Column("enabled", sa.Boolean, nullable=False),
        sa.Column("tos_url", sa.Text),
        sa.Column(
            "allowed_hosts",
            postgresql.ARRAY(sa.Text),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column("min_interval_s", sa.Float),
        sa.Column("max_requests", sa.Integer),
        _now("synced_at"),
        sa.Column("retired_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("name ~ '^[a-z][a-z0-9_]{0,62}$'", name="sources_name_format"),
        sa.CheckConstraint("type IN ('api', 'inbox')", name="sources_type"),
        sa.CheckConstraint(
            "type <> 'api' OR (tos_url IS NOT NULL AND min_interval_s IS NOT NULL "
            "AND max_requests IS NOT NULL AND cardinality(allowed_hosts) > 0)",
            name="sources_api_needs_terms_and_limits",
        ),
        sa.CheckConstraint(
            "type <> 'inbox' OR (tos_url IS NULL AND cardinality(allowed_hosts) = 0)",
            name="sources_inbox_is_local",
        ),
        sa.CheckConstraint("cardinality(allowed_hosts) <= 16", name="sources_hosts_count"),
        # float accepts NaN and Infinity, and NaN passes ">= 0", so both are named.
        sa.CheckConstraint(
            "min_interval_s IS NULL OR (min_interval_s >= 0 AND min_interval_s <= 3600)",
            name="sources_interval",
        ),
        sa.CheckConstraint("max_requests IS NULL OR max_requests > 0", name="sources_max_requests"),
        # An API source with no spacing would hammer the provider (C3).
        sa.CheckConstraint(
            "type <> 'api' OR min_interval_s > 0", name="sources_api_interval_positive"
        ),
    )


def _created_runs() -> None:
    op.create_table(
        "runs",
        _id(),
        sa.Column("agent", sa.Text, nullable=False),
        sa.Column(
            "source_id",
            sa.BigInteger,
            sa.ForeignKey("sources.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("status", sa.Text, nullable=False, server_default=sa.text("'running'")),
        _now("started_at"),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("n_fetched", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("n_new", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("n_skipped", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("tokens", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("cost", sa.Numeric(12, 6), nullable=False, server_default=sa.text("0")),
        sa.Column("error", sa.Text),
        sa.Column(
            "rejections",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        # Target of signals' (run_id, source_id) foreign key.
        sa.UniqueConstraint("id", "source_id", name="runs_id_source"),
        sa.CheckConstraint("status IN ('running', 'ok', 'partial', 'failed')", name="runs_status"),
        # A run is finished exactly when it has left 'running'.
        sa.CheckConstraint(
            "(status = 'running') = (finished_at IS NULL)", name="runs_finished_matches_status"
        ),
        sa.CheckConstraint(
            "finished_at IS NULL OR finished_at >= started_at", name="runs_time_order"
        ),
        sa.CheckConstraint(
            "n_fetched >= 0 AND n_new >= 0 AND n_skipped >= 0 AND tokens >= 0 "
            "AND cost >= 0 AND cost <> 'NaN'",
            name="runs_counts_non_negative",
        ),
        sa.CheckConstraint("char_length(error) <= 4000", name="runs_error_size"),
        sa.CheckConstraint(
            "jsonb_typeof(rejections) = 'array' AND jsonb_array_length(rejections) <= 100",
            name="runs_rejections_shape",
        ),
    )
    # Scout's watermark: the newest ok run per source.
    op.create_index("ix_runs_source_started", "runs", ["source_id", sa.text("started_at DESC")])
    # A finished run is a logged fact (C5): its counts can never be rewritten later.
    op.execute(
        """
        CREATE FUNCTION runs_finished_is_final() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF OLD.status <> 'running' THEN
                RAISE EXCEPTION 'run % is finished and cannot change (C5)', OLD.id
                    USING ERRCODE = 'insufficient_privilege';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER runs_finished_is_final BEFORE UPDATE ON runs
        FOR EACH ROW EXECUTE FUNCTION runs_finished_is_final()
        """
    )


def _created_signals() -> None:
    op.create_table(
        "signals",
        _id(),
        sa.Column(
            "source_id",
            sa.BigInteger,
            sa.ForeignKey("sources.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("run_id", sa.BigInteger, nullable=False),
        sa.Column("external_id", sa.Text, nullable=False),
        sa.Column("url", sa.Text),
        sa.Column("url_canonical", sa.Text),
        sa.Column("locator", sa.Text),
        sa.Column("title", sa.Text),
        sa.Column("text", sa.Text, nullable=False),
        sa.Column("author_hash", sa.Text),
        sa.Column("points", sa.Integer),
        sa.Column("num_comments", sa.Integer),
        sa.Column("posted_at", sa.DateTime(timezone=True)),
        _now("fetched_at"),
        sa.Column("content_hash", sa.Text, nullable=False),
        sa.Column("trust", sa.Text, nullable=False, server_default=sa.text("'untrusted'")),
        sa.Column(
            "flags", postgresql.ARRAY(sa.Text), nullable=False, server_default=sa.text("'{}'")
        ),
        sa.Column("raw", postgresql.JSONB, nullable=False),
        sa.Column("intent_score", sa.Numeric(6, 3), nullable=False),
        sa.Column("intent_terms", postgresql.ARRAY(sa.Text), nullable=False),
        sa.Column("intent_version", sa.Text, nullable=False),
        # A signal can only belong to a run of its own source.
        sa.ForeignKeyConstraint(
            ["run_id", "source_id"],
            ["runs.id", "runs.source_id"],
            name="signals_run_same_source",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("source_id", "external_id", name="signals_source_external_id"),
        sa.CheckConstraint(
            "char_length(external_id) BETWEEN 1 AND 512", name="signals_external_id_size"
        ),
        sa.CheckConstraint("url IS NOT NULL OR locator IS NOT NULL", name="signals_has_origin"),
        sa.CheckConstraint("char_length(url) <= 2048", name="signals_url_size"),
        sa.CheckConstraint("char_length(url_canonical) <= 2048", name="signals_url_canonical_size"),
        sa.CheckConstraint("char_length(locator) <= 1024", name="signals_locator_size"),
        sa.CheckConstraint("char_length(title) <= 1024", name="signals_title_size"),
        sa.CheckConstraint("octet_length(text) <= 32768", name="signals_text_size"),
        sa.CheckConstraint(f"author_hash ~ {HEX64}", name="signals_author_hash_format"),
        sa.CheckConstraint(f"content_hash ~ {HEX64}", name="signals_content_hash_format"),
        sa.CheckConstraint(
            "(points IS NULL OR points >= 0) AND (num_comments IS NULL OR num_comments >= 0)",
            name="signals_engagement_non_negative",
        ),
        sa.CheckConstraint("trust IN ('untrusted')", name="signals_trust"),
        sa.CheckConstraint(
            "cardinality(flags) <= 32 AND cardinality(intent_terms) <= 64",
            name="signals_array_sizes",
        ),
        # Measured as text: pg_column_size is the compressed size, which a
        # repetitive payload could keep small while the real value is huge.
        sa.CheckConstraint(
            "jsonb_typeof(raw) = 'object' AND octet_length(raw::text) < 1048576",
            name="signals_raw_shape",
        ),
        # NaN sorts above every number in Postgres and would top every ranking.
        sa.CheckConstraint(
            "intent_score >= 0 AND intent_score <> 'NaN'", name="signals_intent_score"
        ),
        sa.CheckConstraint("char_length(intent_version) >= 1", name="signals_intent_version"),
    )
    # `scout stats --top N`: intent, then points, then comments.
    op.create_index(
        "ix_signals_rank",
        "signals",
        [
            sa.text("intent_score DESC"),
            sa.text("points DESC NULLS LAST"),
            sa.text("num_comments DESC NULLS LAST"),
            "id",
        ],
    )
    # Recurring pain: group by fingerprint, count distinct sources and days.
    op.create_index(
        "ix_signals_content_hash",
        "signals",
        ["content_hash"],
        postgresql_include=["source_id", "posted_at", "fetched_at"],
    )
    op.create_index(
        "ix_signals_url_canonical",
        "signals",
        ["url_canonical"],
        postgresql_include=["source_id", "posted_at", "fetched_at"],
        postgresql_where=sa.text("url_canonical IS NOT NULL"),
    )
    op.create_index("ix_signals_run_id", "signals", ["run_id"])
    # Grants already make signals insert-only; the trigger holds even for a
    # future role granted too much, as decisions_log's does.
    op.execute(
        """
        CREATE FUNCTION signals_insert_only() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'signals is insert-only: % blocked', TG_OP
                USING ERRCODE = 'insufficient_privilege';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER signals_no_update_delete BEFORE UPDATE OR DELETE ON signals
        FOR EACH ROW EXECUTE FUNCTION signals_insert_only()
        """
    )
    op.execute(
        """
        CREATE TRIGGER signals_no_truncate BEFORE TRUNCATE ON signals
        FOR EACH STATEMENT EXECUTE FUNCTION signals_insert_only()
        """
    )
    # A signal can only join a run that is still going. Otherwise rows could be
    # added to a finished run and its logged n_new would stop matching (C5).
    op.execute(
        """
        CREATE FUNCTION signals_run_is_open() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF NOT EXISTS (
                SELECT FROM runs WHERE id = NEW.run_id AND status = 'running'
            ) THEN
                RAISE EXCEPTION 'run % is finished; it takes no more signals', NEW.run_id
                    USING ERRCODE = 'insufficient_privilege';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER signals_run_open BEFORE INSERT ON signals
        FOR EACH ROW EXECUTE FUNCTION signals_run_is_open()
        """
    )


def _granted() -> None:
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{SCOUT_ROLE}') THEN
                CREATE ROLE {SCOUT_ROLE} NOLOGIN;
            END IF;
        -- Roles are cluster-wide: two databases migrating at once can both pass
        -- the check above, and the second CREATE then finds the role made.
        EXCEPTION WHEN duplicate_object OR unique_violation THEN
            NULL;
        END
        $$
        """
    )
    op.execute(f"GRANT USAGE ON SCHEMA public TO {SCOUT_ROLE}")
    op.execute(f"GRANT SELECT ON sources TO {SCOUT_ROLE}")
    # SELECT is needed for RETURNING id and for ON CONFLICT DO NOTHING.
    op.execute(f"GRANT SELECT, INSERT ({', '.join(SIGNAL_INSERTABLE)}) ON signals TO {SCOUT_ROLE}")
    # SELECT lets Scout read back its own run id and filter UPDATE ... WHERE id = :id.
    op.execute(f"GRANT SELECT, INSERT ({', '.join(RUN_INSERTABLE)}) ON runs TO {SCOUT_ROLE}")
    op.execute(f"GRANT UPDATE ({', '.join(RUN_UPDATABLE)}) ON runs TO {SCOUT_ROLE}")
    # Scout appends to the log (C10) and may read back only the id it just wrote.
    # It can't set id or ts, so it can't backdate an entry.
    op.execute(
        f"GRANT INSERT (agent, action, reason, evidence_ids), SELECT (id) "
        f"ON decisions_log TO {SCOUT_ROLE}"
    )
    # And it can only write as itself, never as another agent.
    op.execute(
        f"""
        CREATE FUNCTION decisions_log_scout_is_scout() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF current_user = '{SCOUT_ROLE}' AND NEW.agent <> 'scout' THEN
                RAISE EXCEPTION 'jokr_scout may only log as agent scout'
                    USING ERRCODE = 'insufficient_privilege';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER decisions_log_scout_agent BEFORE INSERT ON decisions_log
        FOR EACH ROW EXECUTE FUNCTION decisions_log_scout_is_scout()
        """
    )
    op.execute(f"GRANT SELECT ON sources, signals, runs TO {APP_ROLE}")
    # Every role can make temp tables by default; none of ours needs to.
    op.execute(
        "DO $$ BEGIN EXECUTE format('REVOKE TEMPORARY ON DATABASE %I FROM PUBLIC', "
        "current_database()); END $$"
    )


def upgrade() -> None:
    _created_sources()
    _created_runs()
    _created_signals()
    _granted()


def downgrade() -> None:
    # signals and runs are evidence that decisions_log.evidence_ids points into.
    # Dropping them with rows would orphan that history, so refuse. The lock
    # stops a run from adding rows between the check and the drop.
    op.execute("LOCK TABLE signals, runs IN ACCESS EXCLUSIVE MODE")
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT FROM signals) OR EXISTS (SELECT FROM runs) THEN
                RAISE EXCEPTION 'refusing to downgrade 0002: signals or runs has rows';
            END IF;
        END
        $$
        """
    )
    op.execute(
        "DO $$ BEGIN EXECUTE format('GRANT TEMPORARY ON DATABASE %I TO PUBLIC', "
        "current_database()); END $$"
    )
    op.execute(f"REVOKE ALL ON sources, signals, runs FROM {APP_ROLE}, {SCOUT_ROLE}")
    op.execute(f"REVOKE ALL ON decisions_log FROM {SCOUT_ROLE}")
    op.execute("DROP TRIGGER decisions_log_scout_agent ON decisions_log")
    op.execute("DROP FUNCTION decisions_log_scout_is_scout()")
    op.execute(f"REVOKE USAGE ON SCHEMA public FROM {SCOUT_ROLE}")
    op.drop_table("signals")  # drops its triggers too
    op.drop_table("runs")
    op.drop_table("sources")
    op.execute("DROP FUNCTION signals_insert_only()")
    op.execute("DROP FUNCTION signals_run_is_open()")
    op.execute("DROP FUNCTION runs_finished_is_final()")

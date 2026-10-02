# JokR

AI venture engine. Find, test, build, sell, judge, repeat. Spec: [JokR_BLUEPRINT.md](JokR_BLUEPRINT.md). Rules for Claude Code: [CLAUDE.md](CLAUDE.md).

Current phase: **P0** (repo, Docker, Postgres + pgvector, config, test harness).

## Quickstart

```sh
cp .env.example .env        # then change both passwords (and the URLs that embed them)
docker compose up -d --wait # db + api, both healthy
curl localhost:8000/health  # {"status":"ok","db":"ok","pgvector":"0.8.1","config":"ok"}
make test                   # full suite inside compose on a throwaway database (no .env needed)
```

## Local development

Needs [uv](https://docs.astral.sh/uv/) and Docker.

```sh
uv sync                      # Python 3.12 venv with dev tools
uv run pre-commit install    # ruff, mypy, gitleaks on every commit
make lint typecheck          # ruff + mypy strict
make test-local              # pytest against the compose db on 127.0.0.1:5432
```

## Layout

| Path | What |
| --- | --- |
| `config/` | `caps.yaml` (C6 budgets), `fit_rules.yaml`, `scoring.yaml`, `sources.yaml` (C3 allowlist). Validated strictly at startup. |
| `jokr/config.py` | Pydantic models for those files. A bad file stops the app. |
| `jokr/db/` | SQLAlchemy models and Alembic migrations. `decisions_log` is append-only (C10): a Postgres trigger blocks UPDATE/DELETE/TRUNCATE, and the API runs as `jokr_app`, a role that owns nothing and so cannot disable the trigger. Migrations run as the owner (`MIGRATION_DATABASE_URL`) in the one-shot `migrate` service, so the API never holds owner credentials. |
| `ops/db-init/` | Runs once on a fresh db volume: creates the `jokr_app` login role. |
| `jokr/api/` | FastAPI app. `GET /health` checks the DB, pgvector and config. |
| `jokr/agents/`, `connectors/`, `guards/`, `stats/`, `prompts/` | Empty until their phase (P1+). |
| `tests/` | pytest. DB tests create and drop their own database. |
| `.claude/` | Claude Code skill stack (ECC + extra packs). Hooks are off. |

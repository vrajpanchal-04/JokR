# JokR

AI venture engine. Find, test, build, sell, judge, repeat. Spec: [JokR_BLUEPRINT.md](JokR_BLUEPRINT.md). Rules for Claude Code: [CLAUDE.md](CLAUDE.md).

Current phase: **P0** (repo, Docker, Postgres + pgvector, config, test harness).

## Quickstart

```sh
cp .env.example .env        # then change POSTGRES_PASSWORD and DATABASE_URL to match
docker compose up -d --wait # db + api, both healthy
curl localhost:8000/health  # {"status":"ok","db":"ok","pgvector":"0.8.1","config":"ok"}
make test                   # full test suite inside compose, on a throwaway database
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
| `jokr/db/` | SQLAlchemy models and Alembic migrations. `decisions_log` is append-only (C10), enforced by a Postgres trigger. |
| `jokr/api/` | FastAPI app. `GET /health` checks the DB, pgvector and config. |
| `jokr/agents/`, `connectors/`, `guards/`, `stats/`, `prompts/` | Empty until their phase (P1+). |
| `tests/` | pytest. DB tests create and drop their own database. |
| `.claude/` | Claude Code skill stack (ECC + extra packs). Hooks are off. |

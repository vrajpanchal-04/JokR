"""FastAPI app. P0 exposes only GET /health."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, Request, Response, status
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from jokr.config import ConfigError, load_config
from jokr.db.session import make_engine
from jokr.settings import Settings

log = logging.getLogger(__name__)

Check = Literal["ok", "error"]


class Health(BaseModel):
    status: Check
    db: Check
    pgvector: str
    config: Check


def create_app(settings: Settings | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        resolved = settings or Settings()
        # C6: validate caps and friends before serving anything. Raises on a bad file.
        load_config(resolved.jokr_config_dir)
        app.state.settings = resolved
        app.state.engine = make_engine(resolved.database_url)
        try:
            yield
        finally:
            await app.state.engine.dispose()

    app = FastAPI(title="JokR", lifespan=lifespan)

    @app.get("/health")
    async def health(request: Request, response: Response) -> Health:
        engine: AsyncEngine = request.app.state.engine
        settings_: Settings = request.app.state.settings

        try:
            load_config(settings_.jokr_config_dir)
            config_check: Check = "ok"
        except ConfigError:
            log.exception("health: config invalid")
            config_check = "error"

        db_check: Check = "error"
        pgvector = "missing"
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
                db_check = "ok"
                version = await conn.scalar(
                    text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
                )
                pgvector = version or "missing"
        except (SQLAlchemyError, OSError):
            # Detail goes to the server log; the endpoint only reports pass/fail.
            log.exception("health: database check failed")
            pgvector = "unknown"

        vector_ok = pgvector not in {"missing", "unknown"}
        healthy = config_check == "ok" and db_check == "ok" and vector_ok
        if not healthy:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return Health(
            status="ok" if healthy else "error", db=db_check, pgvector=pgvector, config=config_check
        )

    return app


app = create_app()

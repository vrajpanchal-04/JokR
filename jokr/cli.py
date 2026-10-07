"""`jokr` command line: sources sync, scout run, scout stats.

Exit codes follow P1 plan §3: 0 ok or partial, 1 failed or misconfigured,
2 a C3 violation. P8 alerts on anything non-zero.
"""

import argparse
import asyncio
import logging
import sys
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager

from pydantic import ValidationError

from jokr.agents.scout import OpenConnector, ScoutDeps, run_scout
from jokr.agents.scout_stats import Stats, scout_stats
from jokr.agents.sources_sync import sync_sources
from jokr.config import ConfigError, Source, load_config, load_intent
from jokr.connectors.arxiv import ArxivConnector
from jokr.connectors.base import Connector
from jokr.connectors.hn import HackerNewsConnector
from jokr.connectors.inbox import InboxConnector
from jokr.connectors.reddit import RedditConnector
from jokr.db.session import make_engine
from jokr.guards.redact import RedactingFilter, Secret
from jokr.guards.tos import GuardedClient
from jokr.settings import ScoutSettings, Settings

log = logging.getLogger("jokr")


def _setup_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    # On the handler, so records from every logger (httpx included) are redacted.
    handler.addFilter(RedactingFilter())
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)


def make_open_connector(settings: ScoutSettings) -> OpenConnector:
    @asynccontextmanager
    async def open_connector(source: Source) -> AsyncIterator[Connector]:
        params = source.params
        if params.kind == "inbox":
            yield InboxConnector(settings.inbox_root, params)
            return
        if params.kind == "reddit" and not settings.reddit_configured:
            raise RuntimeError("reddit is enabled but REDDIT_* credentials are not set")
        user_agent = settings.reddit_user_agent if params.kind == "reddit" else None
        async with GuardedClient(
            source,
            user_agent=user_agent,
            proxy=settings.scout_https_proxy,
            ca_bundle=str(settings.scout_ca_bundle) if settings.scout_ca_bundle else None,
        ) as client:
            if params.kind == "hackernews":
                yield HackerNewsConnector(client, params)
            elif params.kind == "arxiv":
                yield ArxivConnector(client, params)
            else:
                if not (settings.reddit_client_id and settings.reddit_client_secret):
                    raise RuntimeError("reddit credentials vanished after the check above")
                yield RedditConnector(
                    client,
                    params,
                    client_id=Secret(settings.reddit_client_id.get_secret_value()),
                    client_secret=Secret(settings.reddit_client_secret.get_secret_value()),
                )

    return open_connector


async def _sources_sync() -> int:
    settings = Settings()
    cfg = load_config(settings.jokr_config_dir)
    engine = make_engine(settings.database_url)
    try:
        result = await sync_sources(engine, cfg.sources.sources)
    finally:
        await engine.dispose()
    log.info("sources synced: %s; retired: %s", result.upserted, result.retired or "none")
    return 0


async def _scout_run(only: str | None) -> int:
    settings = ScoutSettings()  # values come from the environment
    cfg = load_config(settings.jokr_config_dir)
    lexicon = load_intent(settings.jokr_config_dir)
    if only is not None and cfg.sources.get(only) is None:
        log.error("unknown source %r", only)
        return 1
    deps = ScoutDeps(
        salt=settings.author_hash_salt.get_secret_value().encode(),
        lexicon=lexicon,
        open_connector=make_open_connector(settings),
    )
    engine = make_engine(settings.database_url)
    try:
        result = await run_scout(engine, cfg.sources.sources, deps, only=only)
    finally:
        await engine.dispose()
    for o in result.outcomes:
        log.info(
            "%s %s: %d new, %d fetched, %d skipped%s",
            o.name,
            o.status,
            o.n_new,
            o.n_fetched,
            o.n_skipped,
            f" ({o.error})" if o.error else "",
        )
    log.info("scout run %s (exit %d)", result.status, result.exit_code)
    return result.exit_code


def format_stats(stats: Stats) -> str:
    lines = ["Sources:"]
    for s in stats.per_source:
        lines.append(f"  {s.name:<12} {s.signals:>7} signals  last run: {s.last_status or 'never'}")
    lines.append("Top signals (intent, points, comments):")
    for t in stats.top:
        terms = ", ".join(t.intent_terms) or "-"
        lines.append(
            f"  {t.intent_score:>6} {t.points or 0:>5}p {t.num_comments or 0:>4}c "
            f"[{t.source}] {(t.title or '')[:70]}  ({terms})"
        )
    lines.append("Recurring (seen on more than one day or source):")
    for r in stats.recurring:
        lines.append(
            f"  {r.kind:<7} {r.days:>3}d {r.sources:>2}src {r.signals:>4}x  "
            f"{(r.sample_title or '')[:70]}"
        )
    return "\n".join(lines)


async def _scout_stats(top: int) -> int:
    settings = Settings()
    engine = make_engine(settings.database_url)
    try:
        stats = await scout_stats(engine, top)
    finally:
        await engine.dispose()
    print(format_stats(stats))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jokr")
    groups = parser.add_subparsers(dest="group", required=True)
    sources = groups.add_parser("sources").add_subparsers(dest="command", required=True)
    sources.add_parser("sync", help="mirror config/sources.yaml into the database (owner role)")
    scout = groups.add_parser("scout").add_subparsers(dest="command", required=True)
    run = scout.add_parser("run", help="fetch every enabled source once")
    run.add_argument("--source", help="run only this source")
    stats = scout.add_parser("stats", help="counts, top signals and recurring pain")
    stats.add_argument("--top", type=int, default=20, choices=range(1, 501), metavar="N")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging()
    try:
        if args.group == "sources":
            return asyncio.run(_sources_sync())
        if args.command == "run":
            return asyncio.run(_scout_run(args.source))
        return asyncio.run(_scout_stats(args.top))
    except ConfigError as exc:
        log.error("config error: %s", exc)
        return 1
    except ValidationError as exc:
        # Settings from the environment: name the bad fields, never echo their values.
        fields = ", ".join(".".join(str(p) for p in e["loc"]) for e in exc.errors())
        log.error("settings error in: %s", fields or "environment")
        return 1


if __name__ == "__main__":
    sys.exit(main())

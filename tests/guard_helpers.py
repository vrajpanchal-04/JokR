"""Shared builders for guard and connector tests."""

from collections.abc import Sequence
from typing import Any

from jokr.config import Source

PUBLIC_IP = "151.101.1.1"


def api_source(
    name: str = "hackernews",
    hosts: Sequence[str] = ("hn.algolia.com",),
    *,
    enabled: bool = True,
    interval: float = 0.001,
    max_requests: int = 50,
    params: dict[str, Any] | None = None,
) -> Source:
    default_params: dict[str, dict[str, Any]] = {
        "hackernews": {"kind": "hackernews", "queries": ["internal tool for"]},
        "arxiv": {"kind": "arxiv", "categories": ["cs.AI"]},
        "reddit": {"kind": "reddit", "subreddits": ["SaaS"]},
    }
    return Source.model_validate(
        {
            "name": name,
            "type": "api",
            "enabled": enabled,
            "tos_url": f"https://{hosts[0]}/terms",
            "allowed_hosts": list(hosts),
            "min_interval_s": interval,
            "max_requests": max_requests,
            "params": params or default_params[name],
        }
    )


async def public_resolver(host: str) -> list[str]:
    """Pretend every allowed host resolves to a public address (no real DNS in tests)."""
    return [PUBLIC_IP]


async def no_sleep(seconds: float) -> None:
    return None

"""C3 enforced in code: a source's HTTP client can only reach that source's hosts.

Every check runs inside the transport, so it covers the first request, every
redirect hop and (later) every retry. Connectors never build their own client;
the ruff import ban on jokr/connectors makes that structural.

DNS answers are checked too, so an allowed name that points at a private or
metadata address is refused. The connection re-resolves after our check, so a
hostile DNS server could still race us (rebinding). The hosts we allow are
large public APIs, which makes that a theoretical risk we accept for P1.
"""

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable, Iterable
from importlib.metadata import PackageNotFoundError, version
from typing import Any, Self

import httpx

from jokr.config import Source
from jokr.guards.ratelimit import Pacer

Resolver = Callable[[str], Awaitable[list[str]]]
Sleep = Callable[[float], Awaitable[None]]

MAX_REDIRECTS = 3
DEFAULT_MAX_RESPONSE_BYTES = 5 * 1024 * 1024
DEFAULT_TIMEOUT_S = 20.0
_REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})
# Hop-by-hop or body-describing headers that no longer match once we re-buffer the body.
_STALE_HEADERS = ("content-encoding", "content-length", "transfer-encoding")


class GuardError(Exception):
    """Base for every refusal the guard makes."""


class HostNotAllowed(GuardError):
    """The URL or its DNS answer is outside what this source may reach."""


class SourceDisabled(GuardError):
    """A disabled source asked for a client."""


class ResponseTooLarge(GuardError):
    """The response body went over the size cap."""


class TooManyRedirects(GuardError):
    """The redirect chain went past MAX_REDIRECTS."""


def _user_agent() -> str:
    try:
        v = version("jokr")
    except PackageNotFoundError:
        v = "0"
    return f"JokR-Scout/{v}"


def check_url(url: httpx.URL, allowed_hosts: Iterable[str]) -> str:
    """Return the normalized host if `url` may be fetched, else raise HostNotAllowed."""
    if url.scheme != "https":
        raise HostNotAllowed(f"scheme {url.scheme!r} refused, HTTPS only")
    if url.userinfo:
        raise HostNotAllowed("credentials in URL refused")
    if url.port not in (None, 443):
        raise HostNotAllowed(f"port {url.port} refused, 443 only")
    # raw_host is the IDNA (punycode) form, so lookalike Unicode cannot match an ASCII entry.
    host = url.raw_host.decode("ascii").lower().rstrip(".")
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        pass
    else:
        raise HostNotAllowed(f"IP literal {host!r} refused")
    if host not in {h.lower() for h in allowed_hosts}:
        raise HostNotAllowed(f"host {host!r} is not on this source's allowlist")
    return host


def _is_public(address: str) -> bool:
    ip = ipaddress.ip_address(address.split("%", 1)[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global


async def system_resolver(host: str) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(
        host, 443, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP
    )
    return [str(info[4][0]) for info in infos]


class _GuardTransport(httpx.AsyncBaseTransport):
    """Checks URL, DNS and pacing, then hands the request to the real transport."""

    def __init__(
        self,
        allowed_hosts: frozenset[str],
        pacer: Pacer,
        resolver: Resolver,
        inner: httpx.AsyncBaseTransport,
    ) -> None:
        self._allowed = allowed_hosts
        self._pacer = pacer
        self._resolve = resolver
        self._inner = inner

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        host = check_url(request.url, self._allowed)
        addresses = await self._resolve(host)
        if not addresses:
            raise HostNotAllowed(f"{host!r} did not resolve")
        # One bad answer is enough: the OS may pick any of them when it connects.
        bad = [a for a in addresses if not _is_public(a)]
        if bad:
            raise HostNotAllowed(f"{host!r} resolves to private address {bad[0]}")
        await self._pacer.acquire()
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()


class GuardedClient:
    """The only way a connector talks to the internet.

    Built per source from its config. Redirects are followed here, by hand, so
    each hop goes back through the transport's checks.
    """

    def __init__(
        self,
        source: Source,
        *,
        resolver: Resolver = system_resolver,
        sleep: Sleep = asyncio.sleep,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        if not source.enabled:
            raise SourceDisabled(f"source {source.name!r} is disabled")
        if source.min_interval_s is None or source.max_requests is None:
            raise HostNotAllowed(f"source {source.name!r} is not an API source")
        self.source = source
        self.pacer = Pacer(source.min_interval_s, source.max_requests, sleep=sleep)
        self._max_bytes = max_response_bytes
        transport = _GuardTransport(
            frozenset(source.allowed_hosts), self.pacer, resolver, httpx.AsyncHTTPTransport()
        )
        self._client = httpx.AsyncClient(
            transport=transport,
            follow_redirects=False,
            timeout=timeout_s,
            headers={"User-Agent": _user_agent()},
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get(self, url: str | httpx.URL, **kwargs: Any) -> httpx.Response:
        return await self.request("GET", url, **kwargs)

    async def post(self, url: str | httpx.URL, **kwargs: Any) -> httpx.Response:
        return await self.request("POST", url, **kwargs)

    async def request(self, method: str, url: str | httpx.URL, **kwargs: Any) -> httpx.Response:
        request = self._client.build_request(method, url, **kwargs)
        for _ in range(MAX_REDIRECTS + 1):
            response = await self._send(request)
            if response.status_code not in _REDIRECT_CODES or "location" not in response.headers:
                return response
            request = self._next_hop(request, response)
        raise TooManyRedirects(f"more than {MAX_REDIRECTS} redirects")

    def _next_hop(self, request: httpx.Request, response: httpx.Response) -> httpx.Request:
        target = request.url.join(response.headers["location"])
        # Fail here, with a clear message, before building anything for a bad target.
        check_url(target, self.source.allowed_hosts)
        method = request.method
        if response.status_code == 303 or (response.status_code in (301, 302) and method == "POST"):
            method = "GET"
        headers = httpx.Headers(request.headers)
        headers.pop("content-length", None)
        if target.host != request.url.host:
            # Credentials are scoped to the host that was asked for, never forwarded.
            headers.pop("authorization", None)
        content = request.content if method == request.method else None
        return httpx.Request(method, target, headers=headers, content=content)

    async def _send(self, request: httpx.Request) -> httpx.Response:
        response = await self._client.send(request, stream=True)
        try:
            declared = response.headers.get("content-length")
            if declared is not None and declared.isdigit() and int(declared) > self._max_bytes:
                raise ResponseTooLarge(f"declared {declared} bytes > cap {self._max_bytes}")
            body = bytearray()
            # aiter_bytes yields decoded bytes, so a compression bomb hits the cap too.
            async for chunk in response.aiter_bytes():
                body += chunk
                if len(body) > self._max_bytes:
                    raise ResponseTooLarge(f"body over cap {self._max_bytes} bytes")
        finally:
            await response.aclose()
        headers = [(k, v) for k, v in response.headers.multi_items() if k not in _STALE_HEADERS]
        return httpx.Response(
            response.status_code, headers=headers, content=bytes(body), request=request
        )

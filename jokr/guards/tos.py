"""C3 enforced in code: a source's HTTP client can only reach that source's hosts.

Every check runs inside the transport, so it covers the first request, every
redirect hop and every retry. Connectors never build their own client; the ruff
import ban on jokr/connectors makes that structural.

DNS answers are checked too, so an allowed name that points at a private or
metadata address is refused. The connection re-resolves after our check, so a
hostile DNS server could still race us (rebinding). The hosts we allow are
large public APIs, which makes that a theoretical risk we accept for P1; the
real fix is the egress allowlist planned for P9.
"""

import asyncio
import ipaddress
import random
import socket
import ssl
import time
from collections.abc import Awaitable, Callable, Iterable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from importlib.metadata import PackageNotFoundError, version
from typing import Any, Self

import certifi
import httpx

from jokr.config import Source
from jokr.guards.netaddr import is_public_address
from jokr.guards.ratelimit import Pacer
from jokr.guards.redact import redact

Resolver = Callable[[str], Awaitable[list[str]]]
Sleep = Callable[[float], Awaitable[None]]

MAX_REDIRECTS = 3
MAX_ATTEMPTS = 4  # first try plus three retries
BACKOFF_BASE_S = 1.0
# A server asking us to wait longer than this is telling us to come back another run.
MAX_RETRY_AFTER_S = 120.0
DEFAULT_MAX_RESPONSE_BYTES = 5 * 1024 * 1024
DEFAULT_TIMEOUT_S = 20.0
_REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})
_RETRY_CODES = frozenset({429, 500, 502, 503, 504})
# Body-describing headers that no longer match once we re-buffer the body.
_STALE_HEADERS = ("content-encoding", "content-length", "transfer-encoding")


class GuardError(Exception):
    """Base for every refusal or failure the guard reports."""


class HostNotAllowed(GuardError):
    """The URL or its DNS answer is outside what this source may reach."""


class SourceDisabled(GuardError):
    """A disabled source asked for a client."""


class ResponseTooLarge(GuardError):
    """The response body went over the size cap."""


class TooManyRedirects(GuardError):
    """The redirect chain went past MAX_REDIRECTS."""


class UpstreamUnavailable(GuardError):
    """The source kept failing after retries, or asked us to wait too long."""


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
    host = url.raw_host.decode("ascii").lower().removesuffix(".")
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        pass
    else:
        raise HostNotAllowed(f"IP literal {host!r} refused")
    if host not in {h.lower() for h in allowed_hosts}:
        raise HostNotAllowed(f"host {host!r} is not on this source's allowlist")
    return host


def _parse_url(url: str | httpx.URL, base: httpx.URL | None = None) -> httpx.URL:
    try:
        return base.join(url) if base is not None else httpx.URL(url)
    except httpx.InvalidURL as exc:
        raise HostNotAllowed(f"malformed URL: {exc}") from None


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
        try:
            addresses = await self._resolve(host)
        except OSError:
            # Surfaced as a connection error so the retry loop treats it like one.
            raise httpx.ConnectError(f"DNS lookup failed for {host!r}") from None
        if not addresses:
            raise HostNotAllowed(f"{host!r} did not resolve")
        # One bad answer is enough: the OS may pick any of them when it connects.
        bad = [a for a in addresses if not is_public_address(a)]
        if bad:
            raise HostNotAllowed(f"{host!r} resolves to private address {bad[0]}")
        await self._pacer.acquire()
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()


class GuardedClient:
    """The only way a connector talks to the internet.

    Built per source from its config. Redirects and retries happen here, by
    hand, so each attempt and each hop goes back through the transport's checks
    and spends from the source's request budget.
    """

    def __init__(
        self,
        source: Source,
        *,
        resolver: Resolver = system_resolver,
        sleep: Sleep = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        jitter: Callable[[], float] = lambda: random.uniform(0.5, 1.0),  # noqa: S311 - not crypto
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        user_agent: str | None = None,
        proxy: str | None = None,
        ca_bundle: str | None = None,
    ) -> None:
        if not source.enabled:
            raise SourceDisabled(f"source {source.name!r} is disabled")
        if source.min_interval_s is None or source.max_requests is None:
            raise HostNotAllowed(f"source {source.name!r} is not an API source")
        self.source = source
        self.pacer = Pacer(source.min_interval_s, source.max_requests, clock=clock, sleep=sleep)
        self._jitter = jitter
        self._max_bytes = max_response_bytes
        # Some sources (Reddit) require their own UA format; otherwise we name ourselves.
        self._default_headers = {"User-Agent": user_agent or _user_agent()}
        # Pinned CA bundle and no environment: proxies, netrc and SSL_CERT_FILE can't
        # change where we connect or whom we trust.
        tls = ssl.create_default_context(cafile=certifi.where())
        if ca_bundle:
            # Added to, never replacing, the public roots: for an inspecting proxy.
            tls.load_verify_locations(cafile=ca_bundle)
        # The allowlist and DNS checks still run on the target URL when a proxy is set.
        inner = httpx.AsyncHTTPTransport(verify=tls, trust_env=False, proxy=proxy)
        self._transport = _GuardTransport(
            frozenset(source.allowed_hosts), self.pacer, resolver, inner
        )
        # The client only builds requests (default headers, timeouts). Sending goes straight
        # to the transport so httpx's own redirect parsing and cookie jar never come into play.
        self._client = httpx.AsyncClient(
            transport=self._transport,
            follow_redirects=False,
            timeout=timeout_s,
            trust_env=False,
            headers=self._default_headers,
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
        request = self._client.build_request(method, _parse_url(url), **kwargs)
        try:
            request.content  # noqa: B018 - raises for streamed bodies, which can't be resent
        except httpx.RequestNotRead:
            raise GuardError("streamed request bodies are not supported") from None
        for _ in range(MAX_REDIRECTS + 1):
            response = await self._send_with_retries(request)
            if response.status_code not in _REDIRECT_CODES or "location" not in response.headers:
                return response
            request = self._next_hop(request, response)
        raise TooManyRedirects(f"more than {MAX_REDIRECTS} redirects")

    async def _send_with_retries(self, request: httpx.Request) -> httpx.Response:
        failure = ""
        last: httpx.Response | None = None
        for attempt in range(MAX_ATTEMPTS):
            if attempt:
                self.pacer.defer(self._backoff(attempt, last))
            try:
                last = await self._send(request)
            except httpx.TransportError as exc:
                failure, last = f"{type(exc).__name__}: {exc}", None
                continue
            if last.status_code not in _RETRY_CODES:
                return last
            failure = f"HTTP {last.status_code}"
        raise UpstreamUnavailable(
            redact(f"{request.url.host}: {failure} after {MAX_ATTEMPTS} attempts")
        ) from None

    def _backoff(self, attempt: int, response: httpx.Response | None) -> float:
        """Seconds to wait before `attempt`: the server's Retry-After, else exponential."""
        if response is not None and "retry-after" in response.headers:
            wait = _retry_after_seconds(response.headers["retry-after"])
            if wait is not None:
                if wait > MAX_RETRY_AFTER_S:
                    raise UpstreamUnavailable(
                        f"{response.url.host}: Retry-After {wait:.0f}s exceeds "
                        f"{MAX_RETRY_AFTER_S:.0f}s cap"
                    )
                return wait
        return float(BACKOFF_BASE_S * 2 ** (attempt - 1) * self._jitter())

    def _next_hop(self, request: httpx.Request, response: httpx.Response) -> httpx.Request:
        target = _parse_url(response.headers["location"], base=request.url)
        # Fail here, with a clear message, before building anything for a bad target.
        check_url(target, self.source.allowed_hosts)
        same_host = target.raw_host.lower() == request.url.raw_host.lower()
        method = request.method
        if response.status_code == 303 or (response.status_code in (301, 302) and method == "POST"):
            method = "GET"
        keeps_body = method == request.method and bool(request.content)
        if keeps_body and not same_host:
            raise HostNotAllowed(f"refusing to resend a request body to {target.host!r}")
        if same_host:
            headers = httpx.Headers(request.headers)
            headers.pop("content-length", None)
            if not keeps_body:
                headers.pop("content-type", None)
        else:
            # Credentials, cookies and API keys are scoped to the host that was asked for.
            headers = httpx.Headers(self._default_headers)
        content = request.content if keeps_body else None
        return httpx.Request(method, target, headers=headers, content=content)

    async def _send(self, request: httpx.Request) -> httpx.Response:
        response = await self._transport.handle_async_request(request)
        response.request = request
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


def _retry_after_seconds(value: str) -> float | None:
    """Retry-After is either delta-seconds or an HTTP date (RFC 9110 §10.2.3)."""
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - datetime.now(UTC)).total_seconds())

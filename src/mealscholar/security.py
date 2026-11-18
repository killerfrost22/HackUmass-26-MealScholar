"""Request guards for the web app: the analysis rate limit, who is asking, the cross-site write
check, and the access-log filter that keeps device coordinates out of the logs."""

from __future__ import annotations

import ipaddress
import logging
import math
import re
import threading
import time
from collections import deque
from collections.abc import Callable, Hashable
from dataclasses import dataclass
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import Depends, Request
from fastapi.responses import JSONResponse
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Receive, Scope, Send

# --- rate limits ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Rule:
    name: str
    limit: int
    window: float
    """Seconds."""


class RateLimiter:
    """Sliding-window limits kept in process memory; they reset when the server restarts."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._hits: dict[tuple[str, Hashable], deque[float]] = {}
        self._windows: dict[str, float] = {}
        self._recorded = 0

    def hit(self, *checks: tuple[Rule, Hashable]) -> int | None:
        """Count one attempt against each (rule, subject). If any is already at its limit, count
        nothing and return the seconds until the attempt would be allowed."""
        now = self._clock()
        with self._lock:
            wait = 0.0
            for rule, subject in checks:
                hits = self._hits.get((rule.name, subject))
                while hits and hits[0] <= now - rule.window:
                    hits.popleft()
                if hits and len(hits) >= rule.limit:
                    wait = max(wait, hits[0] + rule.window - now)
            if wait > 0:
                return max(1, math.ceil(wait))
            for rule, subject in checks:
                self._windows[rule.name] = rule.window
                self._hits.setdefault((rule.name, subject), deque()).append(now)
            self._recorded += 1
            if self._recorded % 1024 == 0:  # forget subjects with no attempts left in their window
                self._hits = {k: h for k, h in self._hits.items() if h and h[-1] > now - self._windows[k[0]]}
            return None


# --- who is asking ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Client:
    """The visitor's address; rate limits go by `network`."""

    ip: str

    @property
    def network(self) -> str:
        """The IPv4 address, or the IPv6 /64: one machine or home network can usually pick any
        address in its /64, so counting single IPv6 addresses would hardly limit anyone."""
        address = _ip(self.ip)
        if address is None:
            return self.ip
        if address.version == 6:
            return str(ipaddress.ip_network((address, 64), strict=False))
        return str(address)


def _client(request: Request) -> Client:
    peer = request.client.host if request.client else ""
    ip = peer
    # cloudflared, on this machine, passes the visitor's address in CF-Connecting-IP. Anyone could
    # send that header (`serve --host 0.0.0.0`), so it only counts from a loopback peer.
    if (address := _ip(peer)) is not None and address.is_loopback:
        ip = request.headers.get("cf-connecting-ip", "").strip() or peer
    return Client(ip=ip[:64] or "unknown")


RequestClient = Annotated[Client, Depends(_client)]


def _ip(text: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """The IP address in `text`, with IPv4-mapped IPv6 (::ffff:192.0.2.1) as IPv4; None if it isn't one."""
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped
    return address


# --- cross-site writes ------------------------------------------------------------

UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


class SameOriginWrites:
    """Refuses writes whose Origin header names another host than the request, so another site
    open in the same browser can't post photos or delete meals.

    Requests without an Origin header pass. The public URL's host also counts as this site,
    in case a proxy rewrites the Host header.
    """

    def __init__(self, app: ASGIApp, public_host: str = "") -> None:
        self.app = app
        self.public_host = public_host.lower()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["method"].upper() in UNSAFE_METHODS:
            headers = Headers(scope=scope)
            origin = headers.get("origin")
            if origin is not None and not self._same_site(origin, headers.get("host", "")):
                response = JSONResponse({"detail": "Cross-site requests are not allowed."}, status_code=403)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)

    def _same_site(self, origin: str, host: str) -> bool:
        try:
            origin_host = urlsplit(origin.strip()).netloc.lower()
        except ValueError:  # a malformed Origin counts as another site
            return False
        return bool(origin_host) and origin_host in (host.lower(), self.public_host)


def route_path(scope: Scope) -> str:
    """The path as the app's routes see it. Where the app is mounted (at /app, web.py) the scope
    keeps the full path, /app/api/..., and root_path is the mount's /app."""
    path, root = scope["path"], scope.get("root_path", "")
    return path[len(root) :] if root and path.startswith(root + "/") else path


# --- logs ---------------------------------------------------------------------------


class HideCoordinates(logging.Filter):
    """Hides device coordinates (POST /api/meals?lat=...&lon=...) in uvicorn's access log, which
    prints every request's path and query; they must never be stored."""

    PATTERN = re.compile(r"([?&](?:lat|lon)=)[^&#\s]+")

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(
                self.PATTERN.sub(r"\1[hidden]", arg) if isinstance(arg, str) else arg for arg in record.args
            )
        return True


_hide_coordinates = HideCoordinates()


def hide_coordinates_in_access_log() -> None:
    logging.getLogger("uvicorn.access").addFilter(_hide_coordinates)  # added once, however many apps

"""Connections to IxNetwork targets, hardened for use inside an MCP server.

restpy is used for transport and object navigation, but several of its defaults
are unsafe here:

* its connection logger falls back to ``StreamHandler(sys.stdout)`` -> would corrupt
  the stdio transport; we pre-attach a stderr handler;
* HTTP requests carry no timeout and the 409 / async-poll loops never give up;
  we replace ``ixnetwork_restpy.connection.Session`` with ``DeadlineSession``,
  which applies per-request timeouts and a per-tool-call deadline;
* it is not thread-safe; every call on a target runs under that target's lock.

Attach model (Windows GUI): ``TestPlatform`` -> ``Sessions.find(Id=session_id)`` ->
``.Ixnetwork``. Sessions are never created or deleted.
"""

from __future__ import annotations

import functools
import logging
import sys
import threading
import time
import warnings
from typing import Any, Callable, TypeVar

import anyio
import requests

from . import rest
from .config import Target
from .errors import GUARD_BLOCKED, NOT_FOUND, DeadlineExceeded, IxToolError, map_exception

log = logging.getLogger("ixnetwork_mcp.session")

T = TypeVar("T")

_local = threading.local()
_guards_installed = False


class DeadlineSession(requests.Session):
    """requests.Session with default timeouts and a thread-local deadline.

    restpy retries HTTP 409 every 6 s forever and polls async operations forever;
    both loops issue new requests, so checking the deadline here bounds them.
    """

    fallback_timeout: tuple[float, float] = (10.0, 120.0)

    def request(self, method, url, *args, **kwargs):  # type: ignore[override]
        deadline: float | None = getattr(_local, "deadline", None)
        timeout = kwargs.get("timeout") or getattr(_local, "timeouts", None) or self.fallback_timeout
        if not isinstance(timeout, tuple):
            timeout = (float(timeout), float(timeout))
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DeadlineExceeded(f"tool deadline exceeded before {method} {_short(url)}")
            timeout = (min(timeout[0], remaining), min(timeout[1], remaining))
        kwargs["timeout"] = timeout
        try:
            return super().request(method, url, *args, **kwargs)
        except requests.Timeout as exc:
            if deadline is not None and time.monotonic() >= deadline:
                raise DeadlineExceeded(f"tool deadline exceeded during {method} {_short(url)}") from exc
            raise


def _short(url: str) -> str:
    i = url.find("/ixnetwork")
    return url[i:] if i >= 0 else url


def _is_connection_error(exc: BaseException) -> bool:
    from ixnetwork_restpy.errors import ConnectionError as RpConnectionError

    return isinstance(exc, (requests.ConnectionError, RpConnectionError))


def install_restpy_guards() -> None:
    """Idempotent process-wide setup; call before the first TestPlatform is built."""
    global _guards_installed
    if _guards_installed:
        return
    # generated restpy docstrings trigger SyntaxWarnings when first compiled (py3.12+)
    warnings.filterwarnings("ignore", category=SyntaxWarning, message=r".*invalid escape sequence")
    # IxNetwork GUI API servers use self-signed certificates; verify_cert=false is the norm
    warnings.filterwarnings("ignore", message=r"Unverified HTTPS request")
    rp_log = logging.getLogger("ixnetwork_restpy.connection")
    if not rp_log.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(asctime)s restpy %(levelname)s %(message)s"))
        rp_log.addHandler(handler)
    rp_log.propagate = False

    import ixnetwork_restpy.connection as rp_connection

    rp_connection.Session = DeadlineSession
    _guards_installed = True


class TargetSession:
    """One IxNetwork target: lazily attached restpy root, serialized by a lock."""

    def __init__(self, target: Target):
        self.target = target
        self.lock = threading.Lock()
        self._tp: Any = None
        self._ixn: Any = None
        self.build: str | None = None
        self.platform: str | None = None
        self.connected_at: float | None = None
        # for side channels that must not share restpy's connection (e.g. schema crawl)
        self.rest_base: str | None = None
        self.rest_headers: dict[str, str] = {}

    @property
    def connected(self) -> bool:
        return self._ixn is not None

    def _connect(self) -> Any:
        install_restpy_guards()
        from ixnetwork_restpy import TestPlatform

        t = self.target
        tp = TestPlatform(t.ip, rest_port=t.rest_port, verify_cert=t.verify_cert, trace="warning")
        if t.api_key:
            tp.ApiKey = t.api_key
        elif t.username:
            tp.Authenticate(t.username, t.password or "")
        sessions = tp.Sessions.find(Id=t.session_id)
        if len(sessions) != 1:
            raise IxToolError(NOT_FOUND, f"session {t.session_id} not found on {t.ip}:{t.rest_port}",
                              hint="The Windows GUI API server exposes only session 1; check session_id.")
        ixn = sessions.Ixnetwork
        self.build = rest.get(ixn, "globals?includes=buildNumber").get("buildNumber")
        self.platform = tp.Platform
        self.rest_base = f"{tp.Scheme}://{t.ip}:{tp.RestPort}/api/v1/sessions/{t.session_id}/ixnetwork"
        self.rest_headers = {k: v for k, v in ixn._connection._headers.items() if k.lower() != "content-type"}
        self._tp, self._ixn = tp, ixn
        self.connected_at = time.time()
        log.info("attached to %s (%s:%s session %s, build %s, platform %s)",
                 t.name, t.ip, t.rest_port, t.session_id, self.build, self.platform)
        return ixn

    def reset(self) -> None:
        self._tp = self._ixn = None

    def run(self, fn: Callable[[Any], T], deadline_s: float | None = None) -> T:
        """Run ``fn(ixnetwork_root)`` in the calling (worker) thread under the target lock."""
        t = self.target
        deadline = time.monotonic() + (deadline_s or t.timeouts.tool_deadline_s)
        if not self.lock.acquire(timeout=max(0.1, deadline - time.monotonic())):
            raise DeadlineExceeded(f"another tool call on target '{t.name}' is still running")
        _local.deadline = deadline
        _local.timeouts = (t.timeouts.connect_s, t.timeouts.read_s)
        try:
            ixn = self._ixn or self._connect()
            return fn(ixn)
        except Exception as exc:
            if _is_connection_error(exc):
                self.reset()  # reattach on the next call; never replay a possibly-applied write
            raise
        finally:
            _local.deadline = None
            _local.timeouts = None
            self.lock.release()


class SessionManager:
    def __init__(self, targets: dict[str, Target]):
        self.sessions = {name: TargetSession(t) for name, t in targets.items()}

    def resolve(self, name: str | None) -> TargetSession:
        if name:
            try:
                return self.sessions[name]
            except KeyError:
                raise IxToolError(NOT_FOUND, f"unknown target '{name}'", candidates=sorted(self.sessions)) from None
        if len(self.sessions) == 1:
            return next(iter(self.sessions.values()))
        raise IxToolError(NOT_FOUND, "several targets are configured; pass 'target'",
                          candidates=sorted(self.sessions))

    async def call(self, target: str | None, fn: Callable[[Any], T], *, write: bool = False,
                   deadline_s: float | None = None) -> T:
        ts = self.resolve(target)
        if write and ts.target.guards.read_only:
            raise IxToolError(GUARD_BLOCKED, f"target '{ts.target.name}' is read_only",
                              hint="Set guards.read_only=false in the inventory to allow changes.")
        try:
            return await anyio.to_thread.run_sync(functools.partial(ts.run, fn, deadline_s))
        except Exception as exc:
            raise map_exception(exc, ts.target.name) from exc

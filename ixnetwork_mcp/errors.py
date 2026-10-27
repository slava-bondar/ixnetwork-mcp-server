"""Tool errors: every failure reaches the agent as ``{code, message, hint, ...}``.

FastMCP turns an exception raised from a tool into a result with ``isError`` set
and ``str(exc)`` as the text, so ``IxToolError`` renders itself as compact JSON.
"""

from __future__ import annotations

import json
from typing import Any

import requests
from ixnetwork_restpy import errors as rp_errors
from mcp.server.fastmcp.exceptions import ToolError

MAX_MESSAGE = 1500

# codes
NOT_FOUND = "NOT_FOUND"
AMBIGUOUS_REF = "AMBIGUOUS_REF"
VALIDATION = "VALIDATION"
GUARD_BLOCKED = "GUARD_BLOCKED"
CONFIRM_REQUIRED = "CONFIRM_REQUIRED"
CONFIRM_DECLINED = "CONFIRM_DECLINED"
IXN_BUSY = "IXN_BUSY"
IXN_ERROR = "IXN_ERROR"
IXN_AUTH = "IXN_AUTH"
IXN_UNREACHABLE = "IXN_UNREACHABLE"
IXN_DEADLINE = "IXN_DEADLINE"
PROTOCOLS_RUNNING = "PROTOCOLS_RUNNING"
INTERNAL = "INTERNAL"


class IxToolError(ToolError):
    def __init__(self, code: str, message: str, hint: str | None = None, **extra: Any):
        self.code = code
        self.message = _trim(message)
        self.hint = hint
        self.extra = extra
        super().__init__(self.render())

    def render(self) -> str:
        body: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.hint:
            body["hint"] = self.hint
        body.update({k: v for k, v in self.extra.items() if v is not None})
        return json.dumps(body, default=str)

    def __str__(self) -> str:  # FastMCP uses str(exc)
        return self.render()


class DeadlineExceeded(Exception):
    """Raised by the HTTP layer once a tool call's deadline has passed."""


def _trim(msg: str) -> str:
    msg = " ".join(str(msg).split())
    return msg if len(msg) <= MAX_MESSAGE else msg[: MAX_MESSAGE - 3] + "..."


_SERVER_NOTES = "Current Server Errors/Warnings:"


def split_server_message(msg: str) -> tuple[str, list[str]]:
    """restpy messages = server error (+ .NET stack trace) + the session's app errors.
    Returns (first meaningful line, [server warnings])."""
    text = str(msg or "")
    main, _, notes = text.partition(_SERVER_NOTES)
    lines = [ln.strip() for ln in main.splitlines() if ln.strip()]
    head = []
    for ln in lines:
        if ln.startswith("at ") or ln.startswith("---") or ln.startswith("System.") and head:
            break
        head.append(ln)
    warnings = []
    for ln in notes.splitlines():
        ln = ln.strip()
        if ln and ln not in warnings:
            warnings.append(ln[:300])
    return (" ".join(head) or text.strip()), warnings[:3]


def map_exception(exc: BaseException, target: str | None = None) -> IxToolError:
    """Translate restpy / HTTP / internal exceptions into an IxToolError."""
    if isinstance(exc, IxToolError):
        return exc
    if isinstance(exc, DeadlineExceeded):
        return IxToolError(IXN_DEADLINE, str(exc) or "tool deadline exceeded", target=target,
                           hint="The IxNetwork server did not finish in time. Check the GUI for a long-running "
                                "operation, then retry or raise timeouts.tool_deadline_s for this target.")
    if isinstance(exc, rp_errors.ResourceInUseError):
        return IxToolError(IXN_BUSY, exc.message, target=target,
                           hint="IxNetwork reported API contention (HTTP 409). Wait for the running operation "
                                "to finish and retry.")
    if isinstance(exc, rp_errors.UnauthorizedError):
        return IxToolError(IXN_AUTH, exc.message, target=target,
                           hint="Check username/password or api_key for this target in the inventory.")
    if isinstance(exc, rp_errors.NotFoundError):
        msg, notes = split_server_message(exc.message)
        return IxToolError(NOT_FOUND, msg, target=target, server_warnings=notes or None)
    if isinstance(exc, rp_errors.BadRequestError):
        msg, notes = split_server_message(exc.message)
        return IxToolError(IXN_ERROR, msg, target=target, status=400, server_warnings=notes or None)
    if isinstance(exc, rp_errors.ConnectionError):
        return IxToolError(IXN_UNREACHABLE, exc.message, target=target,
                           hint="Is the IxNetwork GUI running on the VM with the REST API enabled?")
    if isinstance(exc, rp_errors.IxNetworkError):
        msg, notes = split_server_message(exc.message)
        return IxToolError(IXN_ERROR, msg, target=target, status=exc.status_code, server_warnings=notes or None)
    if isinstance(exc, (requests.ConnectionError, requests.Timeout)):
        return IxToolError(IXN_UNREACHABLE, f"{type(exc).__name__}: {exc}", target=target,
                           hint="Is the IxNetwork GUI running on the VM with the REST API enabled?")
    # restpy raises a plain Exception for enum validation failures (base.py _check_for_enum_values)
    return IxToolError(INTERNAL, f"{type(exc).__name__}: {exc}", target=target)

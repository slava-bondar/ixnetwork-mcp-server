"""Shared bits for tool modules: result envelope and annotation presets."""

from __future__ import annotations

from typing import Any

from mcp.types import ToolAnnotations

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False)
WRITE_IDEMPOTENT = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False)
DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False)


def require_confirm(confirm: bool, action: str) -> None:
    from ..errors import CONFIRM_REQUIRED, IxToolError

    if not confirm:
        raise IxToolError(CONFIRM_REQUIRED, f"{action} needs explicit confirmation",
                          hint="Confirm with the user, then call again with confirm=true.")


def ok(target: str | None, result: Any, *, warnings: list[str] | None = None,
       next_steps: list[str] | None = None, truncated: bool | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"ok": True, "result": result}
    if target is not None:
        body["target"] = target
    if warnings:
        body["warnings"] = warnings
    if truncated:
        body["truncated"] = True
    if next_steps:
        body["next_steps"] = next_steps
    return body

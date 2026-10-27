"""Shared bits for tool modules: result envelope and annotation presets."""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import Context
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False)
WRITE_IDEMPOTENT = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False)
DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False)


def require_confirm(confirm: bool, action: str) -> None:
    from ..errors import CONFIRM_REQUIRED, IxToolError

    if not confirm:
        raise IxToolError(CONFIRM_REQUIRED, f"{action} needs explicit confirmation",
                          hint="Confirm with the user, then call again with confirm=true.")


class _Confirmation(BaseModel):
    confirm: bool = Field(description="Proceed with this action?")


def can_elicit(ctx: Context | None) -> bool:
    try:
        caps = ctx.session.client_params.capabilities  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        return False
    return getattr(caps, "elicitation", None) is not None


async def confirm_action(ctx: Context | None, ts: Any, confirm: bool, action: str, *, write: bool = True) -> None:
    """Confirmation for a destructive action on target `ts`.
    - The client supports elicitation: the server asks the user directly (confirm=true is neither
      needed nor sufficient).
    - Otherwise confirm=true (the agent asked in chat), unless the target's
      guards.require_human_confirmation demands a confirmation by the user in the client.
    A read_only target is refused before anyone is asked."""
    from ..errors import CONFIRM_DECLINED, GUARD_BLOCKED, IxToolError

    guards = ts.target.guards
    if write and guards.read_only:
        raise IxToolError(GUARD_BLOCKED, f"target '{ts.target.name}' is read_only",
                          hint="Set guards.read_only=false in the inventory to allow changes.")
    if can_elicit(ctx):
        result = await ctx.elicit(  # type: ignore[union-attr]
            message=f"IxNetwork '{ts.target.name}' ({ts.target.ip}): {action}. Proceed?", schema=_Confirmation)
        if result.action == "accept" and getattr(result, "data", None) is not None and result.data.confirm:
            return
        raise IxToolError(CONFIRM_DECLINED, f"the user did not confirm {action}",
                          hint="Do not retry unless the user asks for it again.")
    if guards.require_human_confirmation:
        raise IxToolError(GUARD_BLOCKED, f"{action} needs the user's confirmation in the MCP client, and this "
                                         f"client cannot ask (no elicitation support)",
                          hint="The target has guards.require_human_confirmation; use a client with elicitation.")
    require_confirm(confirm, action)


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

"""Generic write tools: set_attributes, add_node, delete_nodes, import_config_fragment, run_operation."""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from .. import compiler
from ..errors import GUARD_BLOCKED, IxToolError
from ..session import SessionManager
from .common import DESTRUCTIVE, WRITE, WRITE_IDEMPOTENT, ok, require_confirm
from .schema_tools import schema_for


def register(app: FastMCP, manager: SessionManager) -> None:
    @app.tool(annotations=WRITE_IDEMPOTENT)
    async def set_attributes(ref: str, values: dict[str, Any], dry_run: bool = False,
                             target: str | None = None) -> dict[str, Any]:
        """Change attributes of one existing object (`ref`: name, 'type:name', xpath or href).
        `values` maps attribute names to new values. Per-device (multivalue) attributes take a
        Value: a plain value, {"start", "step", "direction"}, {"list": [...]}, {"string": ...},
        optionally with "port_step", "nest" or "overlays" (see get_guide('values')). Values are
        checked against the object's live formats/enums before anything is sent, verified after,
        and the previous values are restored if IxNetwork rejects the change. `dry_run` shows the
        exact REST/ImportConfig payload without applying it. Returns before/after per attribute."""
        schema, warnings = await schema_for(manager, target)
        ts = manager.resolve(target)
        result = await manager.call(target, lambda ixn: compiler.set_attributes(ixn, schema, ref, values, dry_run),
                                    write=not dry_run)
        return ok(ts.target.name, result, warnings=warnings + result.pop("warnings", []))

    @app.tool(annotations=WRITE)
    async def add_node(node_type: str, parent: str | None = None, attributes: dict[str, Any] | None = None,
                       dry_run: bool = False, target: str | None = None) -> dict[str, Any]:
        """Create one object of `node_type` under `parent` (ref; omit for root-level types such as
        'topology' or 'vport'). Examples: add_node('topology', attributes={'name': 'T1', 'ports':
        ['Ethernet - 001']}); add_node('deviceGroup', parent='T1', attributes={'name': 'DG1',
        'multiplier': 10}); add_node('ethernet', parent='DG1'); add_node('ipv4', parent='DG1 ETH',
        attributes={'address': {'start': '10.0.0.2', 'step': '0.0.0.1'}, 'gatewayIp': '10.0.0.1'}).
        Always set a device group's multiplier (IxNetwork defaults to 10). Checks the parent/child
        relationship against the schema (describe_node_type lists creatable children), refuses
        auto-created types and duplicate names, and removes the object again if setting its
        values fails. Returns the new object and the children IxNetwork created automatically."""
        schema, warnings = await schema_for(manager, target)
        ts = manager.resolve(target)
        result = await manager.call(
            target, lambda ixn: compiler.add_node(ixn, schema, parent, node_type, attributes, dry_run),
            write=not dry_run)
        return ok(ts.target.name, result, warnings=warnings + result.pop("warnings", []))

    @app.tool(annotations=DESTRUCTIVE)
    async def delete_nodes(refs: list[str], confirm: bool = False, target: str | None = None) -> dict[str, Any]:
        """Delete objects (and everything below them) by exact reference: names, 'type:name',
        xpaths or hrefs. Only objects that can be deleted are accepted (auto-created ones cannot).
        This is live in the user's GUI and cannot be undone: ask the user first (and offer
        save_config), then call with confirm=true."""
        require_confirm(confirm, f"deleting {len(refs)} object(s)")
        schema, warnings = await schema_for(manager, target)
        ts = manager.resolve(target)
        result = await manager.call(target, lambda ixn: compiler.delete_nodes(ixn, schema, refs), write=True)
        return ok(ts.target.name, result, warnings=warnings)

    @app.tool(annotations=DESTRUCTIVE)
    async def import_config_fragment(fragment: list[dict[str, Any]], dry_run: bool = True,
                                     target: str | None = None) -> dict[str, Any]:
        """Advanced escape hatch: apply raw ImportConfig entries (the xpath dialect shown by
        export_config, e.g. {"xpath": "/topology[1]/deviceGroup[1]", "multiplier": 4} or
        {"xpath": "/multivalue[@source = '<xpath> <attr>']/singleValue", "value": "x"}).
        dry_run (default) classifies each entry as create/modify without applying. Applying needs
        the target's guards.allow_raw_import. Prefer set_attributes/add_node: raw imports are not
        validated beforehand and cannot be rolled back."""
        ts = manager.resolve(target)
        if not dry_run and not ts.target.guards.allow_raw_import:
            raise IxToolError(GUARD_BLOCKED, f"raw imports are disabled for target '{ts.target.name}'",
                              hint="Set guards.allow_raw_import=true in the inventory, or use set_attributes/add_node.")
        result = await manager.call(target, lambda ixn: compiler.import_fragment(ixn, fragment, dry_run),
                                    write=not dry_run)
        return ok(ts.target.name, result)

    @app.tool(annotations=DESTRUCTIVE)
    async def run_operation(operation: str, ref: str | None = None, args: dict[str, Any] | None = None,
                            confirm: bool = False, target: str | None = None) -> dict[str, Any]:
        """Run an IxNetwork operation on an object (`ref`; omit for root operations), e.g.
        'getAllLearnedInfo' on a BGP peer or 'sendArp' on an ipv4 stack. describe_node_type(...,
        include=['operations']) lists the operations and their arguments; `args` supplies extra
        arguments as {"arg2": ..., ...} (arg1 = the object itself is filled in). Operations whose
        name starts with get/fetch/check run directly; anything else changes state or sends
        packets and needs confirm=true after asking the user. Config/port-level operations
        (newConfig, loadConfig, assignPorts ...) are only available through their dedicated tools."""
        schema, warnings = await schema_for(manager, target)
        ts = manager.resolve(target)
        compiler.check_denied(operation)
        is_read = compiler.is_read_operation(operation)
        if not is_read:
            require_confirm(confirm, f"operation '{operation}'")
        result = await manager.call(target, lambda ixn: compiler.run_operation(ixn, schema, ref, operation, args),
                                    write=not is_read)
        return ok(ts.target.name, result, warnings=warnings)

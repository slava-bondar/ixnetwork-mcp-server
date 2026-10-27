"""Configuration save / list / drift / load / new (explicit user actions only)."""

from __future__ import annotations

from typing import Any, Literal

from mcp.server.fastmcp import Context, FastMCP

from .. import configstore
from ..session import SessionManager
from ..errors import GUARD_BLOCKED, IxToolError
from .common import DESTRUCTIVE, READ_ONLY, WRITE, confirm_action, ok


def _guard_clear(ts) -> None:
    if not ts.target.guards.allow_clear_config:
        raise IxToolError(GUARD_BLOCKED, f"replacing the configuration is disabled for target '{ts.target.name}'",
                          hint="Set guards.allow_clear_config=true in the inventory if the user wants this.")


def register(app: FastMCP, manager: SessionManager) -> None:
    @app.tool(annotations=WRITE)
    async def save_config(name: str | None = None, path: str | None = None,
                          formats: list[Literal["ixncfg", "json"]] | None = None, note: str | None = None,
                          overwrite: bool = False, target: str | None = None) -> dict[str, Any]:
        """Save the running IxNetwork configuration to the machine running this server (not the
        VM, not the code repo): <config_dir>/<name>/ with config.ixncfg (exact IxNetwork save),
        config.json (portable: ports incl. L1, topologies, traffic item definitions) and
        manifest.json. Call only when the user asks to save/back up (e.g. "save the config as
        bgp-baseline"). `name` defaults to a timestamp; `path` overrides the target's config_dir;
        an existing save is only replaced with overwrite=true."""
        ts = manager.resolve(target)
        name_, dest = configstore.resolve_dir(ts.target.saved_configs_dir, name, path)
        fmts = list(formats or ["ixncfg", "json"])
        result = await manager.call(target, lambda ixn: configstore.save(
            ixn, target=ts.target.name, build=ts.build, dest=dest, name=name_, formats=fmts, note=note,
            overwrite=overwrite))
        return ok(ts.target.name, result)

    @app.tool(annotations=READ_ONLY)
    async def list_saved_configs(path: str | None = None, target: str | None = None) -> dict[str, Any]:
        """List configurations saved with save_config for this target (newest first): name, time,
        IxNetwork build, formats, object counts and port locations. `path` lists another directory."""
        ts = manager.resolve(target)
        root = configstore.resolve_dir(ts.target.saved_configs_dir, "x", path)[1].parent
        return ok(ts.target.name, {"directory": str(root), "saved": configstore.list_saved(root)})


    @app.tool(annotations=READ_ONLY)
    async def check_drift(against: str, target: str | None = None) -> dict[str, Any]:
        """Compare the running configuration with a saved one (name from list_saved_configs, or a
        path): objects added / removed and attributes changed, matched by object names so one deleted
        object does not shift everything. Use it to tell whether someone changed the configuration
        since it was saved, or whether a restore is needed. Exports the live config (seconds)."""
        import json as _json

        ts = manager.resolve(target)
        saved = configstore.resolve_saved(ts.target.saved_configs_dir, against)
        f = saved / "config.json"
        if not f.is_file():
            raise IxToolError("VALIDATION", f"{saved.name} was saved without config.json; drift needs the json format")
        saved_doc = _json.loads(f.read_text())
        result = await manager.call(target, lambda ixn: configstore.drift(saved_doc, configstore.export_json(ixn)))
        return ok(ts.target.name, {"against": saved.name, **result})

    @app.tool(annotations=DESTRUCTIVE)
    async def load_config(name_or_path: str, method: Literal["ixncfg", "json"] = "ixncfg",
                          port_map: dict[str, str] | None = None, confirm: bool = False, wait_s: int = 90,
                          target: str | None = None, ctx: Context | None = None) -> dict[str, Any]:
        """Replace the running configuration with a saved one ("apply/restore the X config").
        method 'ixncfg' restores exactly (same or newer IxNetwork build); 'json' rebuilds from the
        portable export (e.g. on another VM/build). port_map {"<vport name or old location>":
        "<new chassis/card.port>"} moves ports when the chassis differs. Everything currently
        configured is replaced: ask the user (and offer save_config first), then confirm=true. Needs
        the target's guards.allow_clear_config. Reports port states and drift against the save."""
        ts = manager.resolve(target)
        _guard_clear(ts)
        await confirm_action(ctx, ts, confirm, f"replacing the whole running configuration with '{name_or_path}'")
        saved = configstore.resolve_saved(ts.target.saved_configs_dir, name_or_path)
        result = await manager.call(target, lambda ixn: configstore.load(ixn, saved, method, port_map, max(0, wait_s)),
                                    write=True)
        return ok(ts.target.name, result, warnings=result.pop("warnings", None))

    @app.tool(annotations=DESTRUCTIVE)
    async def new_config(confirm: bool = False, target: str | None = None,
                         ctx: Context | None = None) -> dict[str, Any]:
        """Clear the whole configuration (topologies, traffic, vports — ports are released).
        Ask the user (and offer save_config first), then confirm=true. Needs the target's
        guards.allow_clear_config."""
        ts = manager.resolve(target)
        _guard_clear(ts)
        await confirm_action(ctx, ts, confirm, "clearing the whole configuration")

        def run(ixn):
            ixn.NewConfig()
            return {"cleared": True}

        return ok(ts.target.name, await manager.call(target, run, write=True))

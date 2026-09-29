"""IxNetwork MCP server entry point.

Run locally over stdio:
    uv --directory <repo> run python ixmcp.py -f ~/.ixnetwork-mcp/ixnetwork.json -t stdio
"""

from __future__ import annotations

import argparse
import ipaddress
import logging
import sys

from mcp.server.fastmcp import FastMCP

from .config import DEFAULT_INVENTORY, InventoryError, load_inventory, select_targets
from .session import SessionManager, install_restpy_guards
from .tools import register_all

log = logging.getLogger("ixnetwork_mcp")

INSTRUCTIONS = """\
Controls a Keysight IxNetwork (Windows GUI) session over REST: NGPF topologies, device groups,
protocol stacks (Ethernet/VLAN, IPv4/IPv6, BGP, OSPF, IS-IS, IGMP/MLD, DHCP), traffic items.
Every change is live in the GUI the user is looking at.
Start with get_session_overview. Read get_guide('workflow') before building configuration.
Save or load configurations only when the user asks; if the user asked to back up after each
change, call save_config after every successful write.
"""


def build_app(manager: SessionManager, log_level: str = "INFO") -> FastMCP:
    app = FastMCP("ixnetwork-mcp-server", instructions=INSTRUCTIONS, log_level=log_level)  # type: ignore[arg-type]
    register_all(app, manager)
    return app


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="ixmcp", description="MCP server for Keysight IxNetwork")
    ap.add_argument("-f", "--inventory", default=str(DEFAULT_INVENTORY),
                    help=f"inventory JSON keyed by target name (default {DEFAULT_INVENTORY})")
    ap.add_argument("-T", "--targets", default=None,
                    help="comma-separated target names to expose from the inventory (default: all). "
                         "Use one server registration per project to keep projects isolated.")
    ap.add_argument("-t", "--transport", choices=["stdio", "streamable-http"], default="stdio")
    ap.add_argument("-H", "--host", default="127.0.0.1", help="bind address for streamable-http (loopback only)")
    ap.add_argument("-p", "--port", type=int, default=30031, help="port for streamable-http")
    ap.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    mcp_out = None
    if args.transport == "stdio":
        from .stdio import isolate_stdout

        mcp_out = isolate_stdout()  # before anything else can write to fd 1

    logging.basicConfig(level=args.log_level, stream=sys.stderr,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    install_restpy_guards()
    try:
        targets = select_targets(load_inventory(args.inventory), args.targets)
    except InventoryError as exc:
        log.error("%s", exc)
        sys.exit(2)
    log.info("loaded %d target(s): %s", len(targets), ", ".join(targets))
    app = build_app(SessionManager(targets), args.log_level)

    if args.transport == "stdio":
        import anyio

        from .stdio import run_stdio

        anyio.run(run_stdio, app, mcp_out)
    else:
        if not ipaddress.ip_address(args.host).is_loopback:
            log.error("streamable-http has no authentication yet; bind to a loopback address only")
            sys.exit(2)
        app.settings.host, app.settings.port = args.host, args.port
        app.run(transport="streamable-http")


if __name__ == "__main__":
    main()

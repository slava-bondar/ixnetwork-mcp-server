"""Tool registration. Each module exposes ``register(app, manager)``."""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from ..session import SessionManager
from . import (build, configs, discovery, export, navigate, ports, runtime_tools, schema_tools, status,
               traffic_tools, write)


def register_all(app: FastMCP, manager: SessionManager) -> None:
    for module in (discovery, schema_tools, navigate, export, ports, status, write, build, runtime_tools,
                   traffic_tools, configs):
        module.register(app, manager)

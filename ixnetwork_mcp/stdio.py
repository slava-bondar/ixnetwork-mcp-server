"""stdio transport with a protected stdout.

The MCP stdio transport owns file descriptor 1. Libraries used here may write to
stdout (restpy's fallback log handler, ``print`` in BatchAdd/WatchAssistant,
C-level writes). ``isolate_stdout`` moves the real stdout to a private fd and
points fd 1 (and ``sys.stdout``) at stderr, so any stray write lands in the log
instead of corrupting the JSON-RPC stream.
"""

from __future__ import annotations

import os
import sys
from io import TextIOWrapper
from typing import BinaryIO

import anyio
from mcp.server.fastmcp import FastMCP
from mcp.server.stdio import stdio_server


def isolate_stdout() -> BinaryIO:
    """Call once, first thing in main(), before anything can write to stdout."""
    sys.stdout.flush()
    mcp_fd = os.dup(1)
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    return os.fdopen(mcp_fd, "wb", buffering=0)


async def run_stdio(app: FastMCP, mcp_out: BinaryIO) -> None:
    stdout = anyio.wrap_file(TextIOWrapper(mcp_out, encoding="utf-8", write_through=True))
    async with stdio_server(stdout=stdout) as (read_stream, write_stream):
        server = app._mcp_server
        await server.run(read_stream, write_stream, server.create_initialization_options())

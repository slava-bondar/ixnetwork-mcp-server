"""Launcher: uv --directory <repo> run python ixmcp.py -f ~/.ixnetwork-mcp/ixnetwork.json -t stdio"""

from ixnetwork_mcp.server import main

if __name__ == "__main__":
    main()

"""IxNetwork MCP server."""

import warnings

# ixnetwork_restpy's generated docstrings contain invalid escape sequences; Python reports them
# as SyntaxWarnings when the modules are first compiled, i.e. on the first import below us.
warnings.filterwarnings("ignore", category=SyntaxWarning, message=r".*invalid escape sequence")

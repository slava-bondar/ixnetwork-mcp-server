"""Inventory of IxNetwork targets (``ixnetwork.json``).

The file is a JSON object keyed by target name (one entry per IxNetwork VM).
String values may reference environment variables as
``${NAME}`` so secrets stay out of the file. Keep real inventories outside the
repo, e.g. ``~/.ixnetwork-mcp/ixnetwork.json``.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

DEFAULT_HOME = Path("~/.ixnetwork-mcp")
DEFAULT_INVENTORY = DEFAULT_HOME / "ixnetwork.json"

_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class InventoryError(ValueError):
    pass


class Timeouts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connect_s: float = Field(10, gt=0, description="TCP connect timeout per HTTP request")
    read_s: float = Field(120, gt=0, description="Read timeout per HTTP request")
    tool_deadline_s: float = Field(600, gt=0, description="Upper bound for one tool call (bounds restpy's retry/poll loops)")


class Guards(BaseModel):
    model_config = ConfigDict(extra="forbid")

    read_only: bool = False
    allow_clear_config: bool = False
    allow_traffic_start: bool = True
    allow_port_takeover: bool = False
    allow_raw_import: bool = False


class Target(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    ip: str
    rest_port: int = 11009
    session_id: int = 1
    username: str | None = None
    password: str | None = None
    api_key: str | None = None
    verify_cert: bool = False
    timeouts: Timeouts = Field(default_factory=Timeouts)
    guards: Guards = Field(default_factory=Guards)
    config_dir: Path | None = None

    @field_validator("config_dir")
    @classmethod
    def _expand_dir(cls, v: Path | None) -> Path | None:
        return Path(v).expanduser() if v is not None else None

    @property
    def saved_configs_dir(self) -> Path:
        return self.config_dir or (DEFAULT_HOME / "configs" / self.name).expanduser()

    def public(self) -> dict[str, Any]:
        """Description safe to show to the agent (no secrets)."""
        return {
            "name": self.name,
            "host": self.ip,
            "rest_port": self.rest_port,
            "session_id": self.session_id,
            "auth": "api_key" if self.api_key else ("password" if self.username else "none"),
            "guards": self.guards.model_dump(),
            "config_dir": str(self.saved_configs_dir),
        }


def expand_env(value: Any) -> Any:
    if isinstance(value, str):
        def sub(match: re.Match[str]) -> str:
            name = match.group(1)
            resolved = os.environ.get(name)
            if resolved is None:
                raise InventoryError(f"environment variable '{name}' referenced in the inventory is not set")
            return resolved

        return _ENV_REF.sub(sub, value)
    if isinstance(value, dict):
        return {k: expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [expand_env(v) for v in value]
    return value


def load_inventory(path: str | Path) -> dict[str, Target]:
    p = Path(path).expanduser()
    try:
        raw = json.loads(p.read_text())
    except FileNotFoundError as exc:
        raise InventoryError(f"inventory file not found: {p}") from exc
    except json.JSONDecodeError as exc:
        raise InventoryError(f"inventory file {p} is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict) or not raw:
        raise InventoryError(f"inventory {p} must be a non-empty JSON object keyed by target name")

    targets: dict[str, Target] = {}
    for name, params in raw.items():
        if not isinstance(params, dict):
            raise InventoryError(f"target '{name}': expected an object")
        try:
            targets[name] = Target(name=name, **expand_env(params))
        except ValidationError as exc:
            problems = "; ".join(f"{'.'.join(str(x) for x in e['loc'])}: {e['msg']}" for e in exc.errors())
            raise InventoryError(f"target '{name}': {problems}") from exc
    return targets


def select_targets(targets: dict[str, Target], names: str | None) -> dict[str, Target]:
    """Restrict an inventory to the comma-separated ``names`` (``--targets``)."""
    if not names:
        return targets
    wanted = [n.strip() for n in names.split(",") if n.strip()]
    missing = [n for n in wanted if n not in targets]
    if missing:
        raise InventoryError(f"--targets: unknown target(s) {missing}; inventory has {sorted(targets)}")
    return {n: targets[n] for n in wanted}

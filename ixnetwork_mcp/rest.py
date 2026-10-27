"""Thin raw-REST helpers on top of a restpy ``Ixnetwork`` root.

These primitives are the fast and reliable ones:
* ``select``  - POST operations/select?xpath=true: href + xpath + properties + multivalue
  inlines for a whole subtree in one call (~1 s);
* ``export``  - ResourceManager.ExportConfig on a *scoped* xpath (slow: seconds per topology);
* ``get``     - plain GET relative to the ixnetwork root.
Never build hrefs from xpaths (ids != positions); always take both from ``select``.
"""

from __future__ import annotations

import json
import re
from typing import Any

MV_INLINES: list[dict[str, Any]] = [
    {"child": "multivalue", "properties": ["format", "pattern", "count", "enums", "availablePatterns"]},
    {"child": "^(singleValue|counter|valueList|string|alternate|repeatableRandomRange|random|custom|increment|nest|overlay)$",
     "properties": ["*"]},
]

_IDX = re.compile(r"\[[^\]]*\]")


def root(ixn: Any) -> str:
    """Href of the ixnetwork root without the trailing slash restpy keeps on it."""
    return ixn.href.rstrip("/")


def select(ixn: Any, from_href: str, *, children: list[dict[str, Any]] | None = None,
           properties: list[str] | None = None, inlines: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    payload = {"selects": [{
        "from": from_href,
        "properties": properties if properties is not None else ["*"],
        "children": children or [],
        "inlines": inlines or [],
    }]}
    return ixn._connection._execute(f"{root(ixn)}/operations/select?xpath=true", payload)


def child(regex: str, properties: list[str] | None = None) -> dict[str, Any]:
    return {"child": regex, "properties": properties if properties is not None else ["*"], "filters": []}


PAGE_SIZE = 100  # the server refuses (403) collection GETs larger than this without skip/take


def get(ixn: Any, rel: str) -> Any:
    """GET relative to the ixnetwork root. Collections larger than the server's paging limit
    (100) are fetched page by page with skip/take transparently."""
    from ixnetwork_restpy.errors import IxNetworkError

    rel = rel.lstrip("/")
    url = f"{root(ixn)}/{rel}" if rel else root(ixn)
    try:
        return ixn._connection._read(url)
    except IxNetworkError as exc:
        if exc.status_code != 403 or "skip" not in str(exc.message):
            raise
    sep = "&" if "?" in url else "?"
    out: list[Any] = []
    while True:  # paged responses look like {"count": <total>, "data": [...]}
        page = ixn._connection._read(f"{url}{sep}skip={len(out)}&take={PAGE_SIZE}")
        items = page.get("data", []) if isinstance(page, dict) else (page or [])
        total = page.get("count") if isinstance(page, dict) else None
        out.extend(items)
        if not items or (total is not None and len(out) >= total) or (total is None and len(items) < PAGE_SIZE):
            return out


def export(ixn: Any, xpaths: list[str], include_defaults: bool = False) -> dict[str, Any]:
    return json.loads(ixn.ResourceManager.ExportConfig(xpaths, include_defaults, "json"))


def type_path(xpath: str) -> str:
    """'/topology[2]/deviceGroup[1]/ethernet[1]' -> 'topology/deviceGroup/ethernet'."""
    return _IDX.sub("", xpath).strip("/")


def node_type(xpath: str) -> str:
    return type_path(xpath).rsplit("/", 1)[-1]


def is_multivalue(value: Any) -> bool:
    return isinstance(value, dict) and "/multivalue[" in str(value.get("xpath", ""))


def state_counts(sc: Any) -> dict[str, int] | None:
    """stateCounts arrives as {arg1: total, arg2: notStarted, arg3: down, arg4: up}."""
    if not isinstance(sc, dict):
        return None
    if "arg1" in sc:
        return {"total": sc.get("arg1", 0), "not_started": sc.get("arg2", 0),
                "down": sc.get("arg3", 0), "up": sc.get("arg4", 0)}
    return {"total": sc.get("total", 0), "not_started": sc.get("notStarted", 0),
            "down": sc.get("down", 0), "up": sc.get("up", 0)}


def child_nodes(node: dict[str, Any]) -> list[dict[str, Any]]:
    """Child NGPF nodes of a select result node (lists of dicts that carry an xpath)."""
    out: list[dict[str, Any]] = []
    for key, value in node.items():
        if isinstance(value, list) and value and isinstance(value[0], dict) and "xpath" in value[0]:
            out.extend(v for v in value if not is_multivalue(v))
        elif isinstance(value, dict) and "xpath" in value and not is_multivalue(value):
            out.append(value)  # singleton child
    return out

"""Statistics views.

Reads pages with raw GET/PATCH (restpy's page object never refreshes after a PATCH) and restores
the view's current page afterwards, so the GUI the user is looking at is not left on another page.
Never uses restpy's StatViewAssistant (it eval()s filter strings).
"""

from __future__ import annotations

import difflib
import re
import time
from typing import Any

from .. import rest
from ..errors import NOT_FOUND, VALIDATION, IxToolError


def list_views(ixn: Any) -> list[dict[str, Any]]:
    rows = rest.get(ixn, "statistics/view?includes=caption,type")
    return [{"view": v.get("caption"), "type": v.get("type")} for v in rows if v.get("caption")]


def _coerce(v: Any) -> Any:
    if not isinstance(v, str):
        return v
    s = v.strip()
    for cast in (int, float):
        try:
            return cast(s)
        except ValueError:
            pass
    return v


def read_view(ixn: Any, caption: str, filters: dict[str, str] | None, columns: list[str] | None,
              limit: int, ready_wait_s: float = 15) -> dict[str, Any]:
    views = rest.get(ixn, "statistics/view?includes=caption,visible")
    match = [v for v in views if (v.get("caption") or "").lower() == caption.lower()]
    if not match:
        caps = [v.get("caption") for v in views if v.get("caption")]
        raise IxToolError(NOT_FOUND, f"no statistics view '{caption}'",
                          candidates=difflib.get_close_matches(caption, caps, n=8, cutoff=0.3) or caps[:20],
                          hint="get_stats() without a view lists the views; traffic views appear once traffic "
                               "has been applied/started.")
    page_rel = f"statistics/view/{match[0]['id']}/page"
    href = f"{rest.root(ixn)}/{page_rel}"
    deadline = time.monotonic() + ready_wait_s
    page = rest.get(ixn, page_rel + "?includes=currentPage,totalPages,columnCaptions,isReady,totalRows")
    while not page.get("isReady", True) and time.monotonic() < deadline:
        time.sleep(1)
        page = rest.get(ixn, page_rel + "?includes=currentPage,totalPages,columnCaptions,isReady,totalRows")
    cols = page.get("columnCaptions") or []
    if columns:
        unknown = [c for c in columns if c not in cols]
        if unknown:
            raise IxToolError(VALIDATION, f"unknown columns {unknown}", candidates=cols)
    compiled = {}
    for col, rx in (filters or {}).items():
        if col not in cols:
            raise IxToolError(VALIDATION, f"filter column '{col}' is not in the view", candidates=cols)
        try:
            compiled[cols.index(col)] = re.compile(rx, re.IGNORECASE)
        except re.error as exc:
            raise IxToolError(VALIDATION, f"bad regex for '{col}': {exc}") from None
    original = page.get("currentPage") or 1
    total_pages = page.get("totalPages") or 1
    rows: list[list[Any]] = []
    current = original
    try:
        for p in range(1, total_pages + 1):
            if current != p:
                ixn._connection._update(href, {"currentPage": p})
                current = p
            raw = rest.get(ixn, page_rel)
            for row in raw.get("pageValues") or []:
                values = list(row[0]) if row and isinstance(row[0], list) else list(row)
                if all(rx.search(str(values[i])) for i, rx in compiled.items()):
                    rows.append(values)
    finally:
        if current != original:  # leave the GUI's view on the page it was on
            ixn._connection._update(href, {"currentPage": original})
    keep = [cols.index(c) for c in columns] if columns else list(range(len(cols)))
    shaped = [{cols[i]: _coerce(r[i]) for i in keep if i < len(r)} for r in rows]
    return {"view": match[0]["caption"], "columns": [cols[i] for i in keep], "total_rows": len(shaped),
            "rows": shaped[:limit], "truncated": len(shaped) > limit or None}

"""RFC 2544 QuickTests (throughput, frame loss, back-to-back) over existing traffic items.

QuickTest attributes are plain (no multivalues), so they are PATCHed and the server rejects bad
ones loudly (400). Friendly fields map to REST names verified against IxNetwork 10.00 (OPTIONS on
all three families, and live runs of throughput and frame loss). A run takes over the session's
traffic, so starting one is guarded like starting traffic.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from ixnetwork_restpy.errors import NotFoundError

from . import rest
from .compiler import href_of
from .errors import IXN_ERROR, NOT_FOUND, VALIDATION, IxToolError
from .refs import ConfigIndex

FAMILIES = {"throughput": "rfc2544throughput", "frame_loss": "rfc2544frameLoss", "back_to_back": "rfc2544back2back"}
LATENCY_TYPES = ["cutThrough", "forwardingDelay", "mef", "storeForward"]
LOAD_UNITS = ["bpsRate", "fpsRate", "gbpsRate", "gBpsRate", "kbpsRate", "kBpsRate", "mbpsRate", "mBpsRate",
              "percentMaxRate"]
RATE_MODES = ["average", "minimum"]
RATE_SCALES = ["fps", "gbps", "kbps", "mbps", "percent"]
RESULT_PROPS = "status,progress,result,resultPath,isRunning,duration,trafficStatus,waitingStatus,startTime"
POLL_S = 5.0


def _tests(ixn: Any) -> list[dict[str, Any]]:
    out = []
    for kind, fam in FAMILIES.items():
        for t in rest.get(ixn, f"quickTest/{fam}?includes=name,mode"):
            out.append({"kind": kind, "name": t.get("name"), "mode": t.get("mode"),
                        "href": f"{rest.root(ixn)}/quickTest/{fam}/{t['id']}"})
    return out


def find(ixn: Any, name: str) -> dict[str, Any]:
    tests = _tests(ixn)
    match = [t for t in tests if t["name"] == name]
    if not match:
        raise IxToolError(NOT_FOUND, f"no QuickTest named '{name}'", candidates=[t["name"] for t in tests] or None)
    return match[0]


def _test_config(kind: str, frame_sizes: list[int] | None, duration_s: int | None, trials: int | None,
                 load: dict[str, Any] | None, latency: str | None, jitter: bool | None) -> dict[str, Any]:
    cfg: dict[str, Any] = {}
    if frame_sizes:  # seen in live runs: frame loss ends at once with customlist and iterates framesizeList with custom
        cfg["frameSizeMode"] = "customlist" if kind == "throughput" else "custom"
        cfg["framesizeList"] = [str(int(s)) for s in frame_sizes]
    if duration_s is not None:
        cfg["duration"] = int(duration_s)
        if kind == "frame_loss":  # otherwise it sends numFrames (100,000) per iteration
            cfg["runmode"] = "duration"
    if trials is not None:
        cfg["numtrials"] = int(trials)
    if latency is not None:
        if latency not in LATENCY_TYPES:
            raise IxToolError(VALIDATION, f"latency must be one of {LATENCY_TYPES}")
        cfg["calculateLatency"] = True
        cfg["latencyType"] = latency
    if jitter is not None:
        cfg["calculateJitter"] = bool(jitter)
    if load:
        if kind != "throughput":
            raise IxToolError(VALIDATION, "load {initial, min, max ...} is the throughput binary search; set other "
                                          "families' load in test_config (frame loss: initialStepLoadRate, "
                                          "maxStepLoadRate, stepStepLoadRate, stepLoadUnit)")
        unknown = set(load) - {"initial", "min", "max", "unit", "resolution", "tolerance"}
        if unknown:
            raise IxToolError(VALIDATION, f"load: unknown keys {sorted(unknown)}")
        unit = load.get("unit", "percentMaxRate")
        if unit not in LOAD_UNITS:
            raise IxToolError(VALIDATION, f"load.unit must be one of {LOAD_UNITS}")
        cfg.update({"loadType": "binary", "binaryLoadUnit": unit})
        for key, attr in (("initial", "initialBinaryLoadRate"), ("min", "minBinaryLoadRate"),
                          ("max", "maxBinaryLoadRate"), ("resolution", "binaryResolution"),
                          ("tolerance", "binaryTolerance")):
            if load.get(key) is not None:
                cfg[attr] = load[key]
    return cfg


def _pass_criteria(kind: str, spec: dict[str, Any] | None) -> dict[str, Any]:
    if not spec:
        return {}
    spec = dict(spec)
    out: dict[str, Any] = {}
    if "min_rate" in spec:
        if kind != "throughput":
            raise IxToolError(VALIDATION, "pass_criteria.min_rate is a throughput criterion")
        mode, scale = spec.pop("rate_mode", "average"), spec.pop("rate_unit", "percent")
        if mode not in RATE_MODES or scale not in RATE_SCALES:
            raise IxToolError(VALIDATION, f"pass_criteria: rate_mode in {RATE_MODES}, rate_unit in {RATE_SCALES}")
        out.update({"enablePassFail": True, "enableRatePassFail": True, "passCriteriaLoadRateMode": mode,
                    "passCriteriaLoadRateScale": scale, "passCriteriaLoadRateValue": spec.pop("min_rate")})
    out.update(spec)  # anything else: REST names of the family's passCriteria
    return out


def configure(ixn: Any, kind: str, name: str, traffic_items: list[str] | None = None, *,
              frame_sizes: list[int] | None = None, duration_s: int | None = None, trials: int | None = None,
              load: dict[str, Any] | None = None, latency: str | None = None, jitter: bool | None = None,
              pass_criteria: dict[str, Any] | None = None, test_config: dict[str, Any] | None = None,
              learn_frames: dict[str, Any] | None = None) -> dict[str, Any]:
    """Create the QuickTest `name` (or update it) for the given traffic items. `learn_frames` takes
    REST names of the learning frames sent before the trials (e.g. learnFrequency, learnRate)."""
    if kind not in FAMILIES:
        raise IxToolError(VALIDATION, f"kind must be one of {sorted(FAMILIES)}")
    cfg = {**_test_config(kind, frame_sizes, duration_s, trials, load, latency, jitter), **(test_config or {})}
    criteria = _pass_criteria(kind, pass_criteria)
    hrefs: list[str] = []
    if traffic_items:
        index = ConfigIndex.build(ixn, props=["name"], topologies=False, ports=False)
        hrefs = [index.resolve(t, types=["trafficItem"]).href for t in traffic_items]
    existing = [t for t in _tests(ixn) if t["name"] == name]
    if existing and existing[0]["kind"] != kind:
        raise IxToolError(VALIDATION, f"'{name}' is already a {existing[0]['kind']} QuickTest")
    created = not existing
    if created:
        if not hrefs:
            raise IxToolError(VALIDATION, "a new QuickTest needs traffic_items")
        href = href_of(ixn._connection._create(f"{rest.root(ixn)}/quickTest/{FAMILIES[kind]}",
                                                {"name": name, "mode": "existingMode"}))
    else:
        href = existing[0]["href"]
    try:
        if hrefs:
            for sel in rest.get(ixn, f"{href.split('/ixnetwork/', 1)[1]}/trafficSelection"):
                ixn._connection._delete(f"{href}/trafficSelection/{sel['id']}")
            for h in hrefs:
                ixn._connection._create(f"{href}/trafficSelection",
                                        {"__id__": h, "includeMode": "inTest", "itemType": "trafficItem"})
        if cfg:
            ixn._connection._update(f"{href}/testConfig", cfg)
        if criteria:
            ixn._connection._update(f"{href}/passCriteria", criteria)
        if learn_frames:
            ixn._connection._update(f"{href}/learnFrames", learn_frames)
    except Exception as exc:
        if created:
            ixn._connection._delete(href)
        if isinstance(exc, NotFoundError) and ("/testConfig" in str(exc) or "/passCriteria" in str(exc)):
            raise IxToolError(IXN_ERROR, f"this IxNetwork session has no testConfig/passCriteria for {kind} QuickTests",
                              hint="IxNetwork lost them (seen after concurrent schema requests); restarting the "
                                   "IxNetwork GUI restores them. Other QuickTest kinds may still work.") from exc
        raise
    return {"name": name, "kind": kind, "action": "created" if created else "updated", "traffic_items": traffic_items,
            "test_config": cfg or None, "pass_criteria": criteria or None, "learn_frames": learn_frames or None}


def results(ixn: Any, name: str) -> dict[str, Any]:
    t = find(ixn, name)
    r = rest.get(ixn, f"{t['href'].split('/ixnetwork/', 1)[1]}/results?includes={RESULT_PROPS}")
    return {"name": name, "kind": t["kind"], **{k: r.get(k) for k in RESULT_PROPS.split(",")}}


def control(ixn: Any, name: str, action: str, wait_s: float = 0) -> dict[str, Any]:
    """apply / start / stop / delete. start returns after wait_s (or when the run ends) with the
    current results; the run continues on the server."""
    t = find(ixn, name)
    href = t["href"]
    if action == "delete":
        ixn._connection._delete(href)
        return {"name": name, "deleted": True}
    if action not in ("apply", "start", "stop"):
        raise IxToolError(VALIDATION, f"unknown action '{action}'", candidates=["apply", "start", "stop", "delete"])
    before = results(ixn, name).get("startTime") if action == "start" else None
    ixn._connection._execute(f"{href}/operations/{action}", {"arg1": href})
    out = results(ixn, name)
    if action != "start":
        return {"action": action, **out}
    # isRunning reads False for a moment after start and between iterations (seen live): the run is over
    # once two polls in a row show it stopped, with a result, for this run
    deadline, settled = time.monotonic() + max(0.0, wait_s), 0
    while True:
        done = out.get("isRunning") is False and out.get("result") and out.get("startTime") not in (before, "", None)
        settled = settled + 1 if done else 0
        if settled >= 2 or time.monotonic() >= deadline:
            break
        time.sleep(POLL_S)
        out = results(ixn, name)
    return {"action": action, "finished": settled >= 2, **out}


def report(ixn: Any, name: str, directory: Path) -> dict[str, Any]:
    """Generate the run's PDF report on the VM and download it."""
    t = find(ixn, name)
    remote = ixn._connection._execute(f"{t['href']}/operations/generateReport", {"arg1": t["href"]})
    path = remote if isinstance(remote, str) else (remote or {}).get("result") if isinstance(remote, dict) else None
    if not path or not str(path).lower().endswith(".pdf"):
        raise IxToolError(NOT_FOUND, f"IxNetwork returned no report file ({str(remote)[:200]})",
                          hint="Reports exist only for finished runs; check get_quick_test_results.")
    directory.mkdir(parents=True, exist_ok=True)
    local = directory / f"quicktest-{name.replace('/', '_')}-{time.strftime('%Y%m%d-%H%M%S')}.pdf"
    ixn._connection._get_file(rest.root(ixn), str(path), local_filename=str(local))
    return {"name": name, "report": str(local), "remote": str(path)}

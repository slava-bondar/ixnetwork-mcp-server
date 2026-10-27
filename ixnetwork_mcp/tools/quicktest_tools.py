"""RFC 2544 QuickTest tools: configure_quick_test, control_quick_test, get_quick_test_results."""

from __future__ import annotations

from typing import Any, Literal

from mcp.server.fastmcp import Context, FastMCP

from .. import quicktest
from ..errors import GUARD_BLOCKED, IxToolError
from ..session import SessionManager
from .common import DESTRUCTIVE, READ_ONLY, WRITE, confirm_action, ok


def register(app: FastMCP, manager: SessionManager) -> None:
    @app.tool(annotations=WRITE)
    async def configure_quick_test(name: str, kind: Literal["throughput", "frame_loss", "back_to_back"] = "throughput",
                                   traffic_items: list[str] | None = None, frame_sizes: list[int] | None = None,
                                   duration_s: int | None = None, trials: int | None = None,
                                   load: dict[str, Any] | None = None,
                                   latency: Literal["cutThrough", "storeForward", "forwardingDelay", "mef"] | None = None,
                                   jitter: bool | None = None, pass_criteria: dict[str, Any] | None = None,
                                   test_config: dict[str, Any] | None = None, learn_frames: dict[str, Any] | None = None,
                                   target: str | None = None) -> dict[str, Any]:
        """Create or update an RFC 2544 QuickTest over existing traffic items; nothing runs yet.
        Throughput `load`: {initial, min, max, unit (percentMaxRate…), resolution, tolerance};
        pass_criteria: {min_rate, rate_mode, rate_unit}. test_config / learn_frames take raw REST
        names (e.g. IMIX; learnFrequency='never'); get_guide('traffic') has an example."""
        ts = manager.resolve(target)
        return ok(ts.target.name, await manager.call(target, lambda ixn: quicktest.configure(
            ixn, kind, name, traffic_items, frame_sizes=frame_sizes, duration_s=duration_s, trials=trials, load=load,
            latency=latency, jitter=jitter, pass_criteria=pass_criteria, test_config=test_config,
            learn_frames=learn_frames), write=True))

    @app.tool(annotations=DESTRUCTIVE)
    async def control_quick_test(name: str, action: Literal["apply", "start", "stop", "report", "delete"],
                                 wait_s: int = 0, confirm: bool = False, target: str | None = None,
                                 ctx: Context | None = None) -> dict[str, Any]:
        """'apply' pushes the test's traffic, 'start' runs it (it takes over the session's
        traffic and sends packets: needs guards.allow_traffic_start and the user's confirmation;
        waits up to wait_s, `finished` says whether the run ended), 'stop', 'report' downloads the finished run's PDF
        to ~/.ixnetwork-mcp/exports/<target>/, 'delete' removes the test (confirmation)."""
        ts = manager.resolve(target)
        if action == "start" and not ts.target.guards.allow_traffic_start:
            raise IxToolError(GUARD_BLOCKED, f"starting traffic is disabled for target '{ts.target.name}'")
        if action in ("apply", "start", "delete"):
            what = {"apply": "applying", "start": "running", "delete": "deleting"}[action]
            await confirm_action(ctx, ts, confirm, f"{what} QuickTest '{name}' (affects all traffic in the session)"
                                 if action != "delete" else f"{what} QuickTest '{name}'")
        if action == "report":
            return ok(ts.target.name, await manager.call(
                target, lambda ixn: quicktest.report(ixn, name, ts.target.exports_dir), write=True))
        return ok(ts.target.name, await manager.call(
            target, lambda ixn: quicktest.control(ixn, name, action, max(0, wait_s)), write=True,
            deadline_s=max(0, wait_s) + 120))

    @app.tool(annotations=READ_ONLY)
    async def get_quick_test_results(name: str | None = None, target: str | None = None) -> dict[str, Any]:
        """Status, progress, pass/fail result and result path of a QuickTest run; without `name`,
        the QuickTests that exist (name, kind)."""
        ts = manager.resolve(target)
        if name is None:
            return ok(ts.target.name, {"tests": await manager.call(
                target, lambda ixn: [{k: t[k] for k in ("name", "kind", "mode")} for t in quicktest._tests(ixn)])})
        return ok(ts.target.name, await manager.call(target, lambda ixn: quicktest.results(ixn, name)))

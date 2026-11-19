"""端到端冒烟：真实调用一次 power_weather_hint（消耗 1 次 API 额度）。

用法（在仓库根目录）：
    .venv/bin/python scripts/smoke_live.py [经度,纬度]

校验：响应 ok、api.coverage 完整；不通过时退出码 1。
若落盘缓存里已有本期起报的有效条目，本次运行不消耗额度。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402


async def main() -> int:
    location = sys.argv[1] if len(sys.argv) > 1 else "112.55,37.87"
    params = StdioServerParameters(
        command=str(ROOT / ".venv" / "bin" / "python"),
        args=["-m", "tianji_power_mcp.server"],
        cwd=str(ROOT),
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(
                "power_weather_hint",
                {"location": location, "hours": 24, "scenario": "wind"},
            )
            text = "".join(b.text for b in result.content if getattr(b, "type", "") == "text")
            payload = json.loads(text)
            api = payload.get("api", {})
            coverage = api.get("coverage") or {}
            data = payload.get("data", {})

            print("ok:", payload.get("ok"))
            print("run_time_init:", coverage.get("run_time_init"), "| complete:", coverage.get("complete"))
            print("calls_used:", api.get("calls_used"), "| cache_hit:", api.get("cache_hit"))
            print("peak:", data.get("peak_level"), "| events:", len(data.get("events", [])))
            for event in data.get("events", [])[:5]:
                print(
                    f"  - [{event['level']}] {event['name']} {event['start']}~{event['end']}"
                    f" ({event['hours']}h) :: {event['trigger']}"
                )
            print("data_gaps:", len(data.get("data_gaps", [])))

            passed = bool(payload.get("ok")) and bool(coverage.get("complete"))
            print("verdict:", "PASS" if passed else "FAIL")
            return 0 if passed else 1
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

"""端到端冒烟：真实调用一次 power_weather_hint（消耗 1 次 API 额度）。"""

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
                {"location": "112.55,37.87", "hours": 24, "scenario": "wind"},
            )
            text = "".join(b.text for b in result.content if getattr(b, "type", "") == "text")
            payload = json.loads(text)
            print("ok:", payload.get("ok"))
            print("api:", json.dumps(payload.get("api", {}), ensure_ascii=False))
            data = payload.get("data", {})
            print("peak:", data.get("peak_level"), "| events:", len(data.get("events", [])))
            for event in data.get("events", [])[:5]:
                print(f"  - [{event['level']}] {event['name']} {event['start']}~{event['end']} ({event['hours']}h) :: {event['trigger']}")
            print("data_gaps:", len(data.get("data_gaps", [])))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

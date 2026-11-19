"""stdio 冒烟测试：真实子进程启动 MCP，握手、列工具、调 get_capability（不消耗 API 额度）。"""

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
            info = await session.initialize()
            si = getattr(info, "server_info", None)
            print("server:", si.name if si else "?", si.version if si else "?")
            tools = await session.list_tools()
            for tool in tools.tools:
                print(f"  - {tool.name}: {(tool.description or '')[:60]}")
            result = await session.call_tool("get_capability", {})
            text = "".join(block.text for block in result.content if getattr(block, "type", "") == "text")
            payload = json.loads(text)
            authorized = payload.get("authorized") or []
            print("authorized elements:", len(authorized), "| calls_used:", payload.get("limits", {}).get("calls_used"))
            print("scenarios:", sorted(payload.get("scenarios", {})))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

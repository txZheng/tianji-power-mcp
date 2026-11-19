"""MCP 服务入口：天机气象 · 电力提示。

所有工具都只做"基于天气数据的提示"；唯一的数值模型是演示用的理论出力。
"""

from __future__ import annotations

import json
from typing import Any

from mcp.server.mcpserver import MCPServer

from . import __version__, derive, elements, hints, power, pv
from .client import TianjiClient, TianjiError
from .config import load_settings

SOURCE = "数据来源：中科天机气象 API"
DISCLAIMER = "参考提示，非正式气象预报；不构成生产指令"

INSTRUCTIONS = (
    "面向电力行业的天气提示工具，提供大风、覆冰、低温、高温、强降水、云遮挡等提示，"
    "以及基于默认功率曲线的理论出力演示。坐标格式为 '经度,纬度'，经度在前。"
    "所有结果都带触发条件与证据，请把 data_gaps 一并转述，不要省略。"
)

server = MCPServer(
    name="tianji-power",
    title="天机气象 · 电力提示",
    description="基于天机气象数据的电力行业天气提示（大风/覆冰/低温/高温/强降水/云遮挡）",
    instructions=INSTRUCTIONS,
    version=__version__,
)

_clients: dict[str, TianjiClient] = {}

DEFAULT_FIELDS: dict[str, list[str]] = {
    "wind": ["t2m", "rh2m", "ws10m", "ws100m", "wd100m", "tp", "pres", "cldt", "cldl"],
    "pv": ["t2m", "rh2m", "ssrd", "cldt", "cldh", "cldl", "tp", "pres", "ws10m"],
    "grid": ["t2m", "rh2m", "ws10m", "wd100m", "tp", "pres", "cldt", "cldl", "psz"],
    "general": ["t2m", "rh2m", "ws10m", "ws100m", "tp", "pres", "cldt", "cldl", "psz"],
}


def _client() -> TianjiClient:
    """按订阅 ID 复用客户端，缓存与调用计数因此可以跨工具调用生效。"""
    settings = load_settings()
    key = settings.subscription_id
    if key not in _clients:
        _clients[key] = TianjiClient(settings)
    return _clients[key]


def _envelope(payload: dict, series, extra: dict[str, Any] | None = None) -> dict:
    body = {
        "ok": True,
        "source": SOURCE,
        "disclaimer": DISCLAIMER,
        "api": series.meta(_client()),
    }
    if extra:
        body.update(extra)
    body["data"] = payload
    return body


def _error(exc: Exception) -> dict:
    return {
        "ok": False,
        "error": str(exc),
        "error_type": type(exc).__name__,
        "source": SOURCE,
        "hint": "额度有限，确认坐标与时段后重试；要素未授权请先调用 get_capability 查看可用清单。",
    }


@server.tool(description="查看已授权要素、已知不可用要素、调用预算与端点。开工前先调用它，避免浪费额度。")
def get_capability() -> dict:
    client = _client()
    body = client.capability()
    body.update(
        {
            "source": SOURCE,
            "disclaimer": DISCLAIMER,
            "scenarios": {name: sorted(hazards) for name, hazards in hints.SCENARIO_HAZARDS.items()},
            "thresholds": hints.THRESHOLDS,
            "power_defaults": power.DEFAULT_PARAMS,
        }
    )
    return body


@server.tool(
    description=(
        "指定点位的电力气象提示：大风/覆冰/低温/高温/强降水/云遮挡，按事件合并并给出触发条件。"
        "scenario 可选 wind（风电）/pv（光伏）/grid（电网）/general。"
    )
)
def power_weather_hint(
    location: str,
    hours: int = 24,
    start_hour: int = 0,
    scenario: str = "general",
    cut_out: float = 25.0,
) -> dict:
    scenario = scenario if scenario in DEFAULT_FIELDS else "general"
    try:
        series = _client().dataset(location, hours=hours, start_hour=start_hour).project(
            DEFAULT_FIELDS[scenario]
        )
    except TianjiError as exc:
        return _error(exc)
    result = hints.detect(series.rows, scenario=scenario, cut_out=cut_out)
    result["window"] = {
        "location": location,
        "hours": hours,
        "start_hour": start_hour,
        "first": series.rows[0]["time"] if series.rows else None,
        "last": series.rows[-1]["time"] if series.rows else None,
    }
    return _envelope(result, series)


@server.tool(description="风电/光伏关心的关键要素逐时快照（含轮毂风速、露点、风力等级等派生量）。")
def wind_solar_snapshot(location: str, hours: int = 24, start_hour: int = 0, t_res: str = "1h") -> dict:
    fields = ["t2m", "rh2m", "ssrd", "ws10m", "ws100m", "wd100m", "cldt", "tp"]
    try:
        series = _client().dataset(
            location, hours=hours, start_hour=start_hour, t_res=t_res
        ).project(fields)
    except TianjiError as exc:
        return _error(exc)
    rows = []
    for row in series.rows:
        rich = derive.enrich(row)
        rows.append(
            {
                "time": rich.get("time"),
                "t2m": rich.get("t2m"),
                "rh2m": rich.get("rh2m"),
                "dpt2m": round(rich["dpt2m"], 2) if rich.get("dpt2m") is not None else None,
                "ssrd": rich.get("ssrd"),
                "ws10m": rich.get("ws10m"),
                "ws100m": rich.get("ws100m"),
                "wd100m": rich.get("wd100m"),
                "force": f"{rich['force']}级",
                "cldt": rich.get("cldt"),
                "tp": rich.get("tp"),
            }
        )
    return _envelope({"rows": rows}, series)


@server.tool(
    description=(
        "理论出力演示（非功率预测）。kind=wind 按功率曲线把风速映射为出力，"
        "切入/额定/切出、轮毂高度、容量、自定义 curve 均可指定；"
        "kind=pv 按辐照度×性能比×温度修正算光伏出力，pr/tilt_gain 可指定。"
    )
)
def theoretical_power(
    location: str,
    hours: int = 24,
    start_hour: int = 0,
    kind: str = "wind",
    hub_height: float = 100.0,
    cut_in: float = 3.0,
    rated: float = 12.0,
    cut_out: float = 25.0,
    capacity_mw: float = 5.0,
    curve: list[list[float]] | None = None,
    pr: float = 0.82,
    tilt_gain: float = 1.0,
) -> dict:
    kind = (kind or "wind").strip().lower()
    if kind not in ("wind", "pv"):
        return _error(ValueError(f"kind 只能是 wind 或 pv，收到 {kind!r}"))
    try:
        if kind == "wind":
            power.check_params(cut_in, rated, cut_out, hub_height, capacity_mw)
            series = _client().dataset(location, hours=hours, start_hour=start_hour).project(
                ["ws10m", "ws100m", "t2m"]
            )
            result = power.simulate(
                series.rows,
                hub_height=hub_height,
                cut_in=cut_in,
                rated=rated,
                cut_out=cut_out,
                capacity_mw=capacity_mw,
                curve=curve,
            )
        else:
            pv.check_params(capacity_mw, pr, -0.004, tilt_gain)
            series = _client().dataset(location, hours=hours, start_hour=start_hour).project(
                ["ssrd", "t2m"]
            )
            result = pv.simulate(
                series.rows,
                capacity_mw=capacity_mw,
                pr=pr,
                tilt_gain=tilt_gain,
            )
    except (ValueError, TianjiError) as exc:
        return _error(exc)
    return _envelope(result, series)


@server.tool(
    description=(
        "唯一的取数入口：返回该窗口内全部已授权要素（服务端不支持筛选，故一次取全）。"
        "其他加工类工具会复用同一份缓存，**不需要**先调用本工具。"
        "fields 可选，用于投影列；limit>0 时只截断本次返回的行数，"
        "不影响缓存与其他工具计算所用的完整数据。"
    )
)
def get_dataset(
    location: str,
    hours: int = 24,
    start_hour: int = 0,
    t_res: str = "1h",
    fields: list[str] | None = None,
    limit: int = 0,
) -> dict:
    unknown: list[str] = []
    denied: list[str] = []
    if fields:
        codes, unknown, denied = elements.resolve(fields)
        if not codes:
            return {
                "ok": False,
                "error": f"没有可用要素。未知：{unknown}；未授权：{denied}",
                "hint": "先用 get_capability 查看可用要素清单。",
                "source": SOURCE,
            }
    else:
        codes = list(elements.ALL_FIELDS)

    try:
        series = _client().query(location, codes, hours=hours, start_hour=start_hour, t_res=t_res)
    except TianjiError as exc:
        return _error(exc)

    total = len(series.rows)
    cut = max(0, int(limit or 0))
    rows = series.rows[:cut] if cut > 0 else series.rows
    note = (
        f"完整数据集 {total} 行已缓存；本次仅返回前 {len(rows)} 行。"
        "加工类工具（提示/快照/理论出力）始终使用完整数据，截断只影响本工具返回值。"
        if len(rows) < total
        else ""
    )
    return _envelope(
        {
            "rows": rows,
            "total_rows": total,
            "returned_rows": len(rows),
            "truncated": len(rows) < total,
            "note": note,
            "unknown_fields": unknown,
            "denied_fields": {code: elements.DENIED.get(code, "未授权") for code in denied},
        },
        series,
    )


def main() -> None:
    server.run(transport="stdio")


if __name__ == "__main__":
    main()

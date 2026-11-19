"""MCP 服务入口：天机气象 · 电力提示。

所有工具都只做"基于天气数据的提示"；唯一的数值模型是演示用的理论出力。
"""

from __future__ import annotations

import json
from typing import Any

from mcp.server.mcpserver import MCPServer

from . import __version__, daily, derive, elements, hints, power, pv
from .client import TianjiClient, TianjiError
from .config import load_settings
from .window import Window, parse_period, rolling_window

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


def _window_for(period: str, hours: int) -> Window:
    """把 period（人话时段）或 hours（从现在起算）解析成绝对窗口。"""
    if period:
        try:
            window = parse_period(period)
        except ValueError as exc:
            raise TianjiError(str(exc)) from exc
        if window is None:
            raise TianjiError(
                f"无法识别的时段：{period!r}。可用写法：未来24小时 / 未来7天 / 今天 / 明天下午 / "
                "2026-12-01 / 2026-12-01~2026-12-03"
            )
        return window
    return rolling_window(hours)


def _require_rows(series, window: Window) -> dict | None:
    """窗口里一行数据都没有时，别让下游把「空」误读成「没有事件」。"""
    if series.rows:
        return None
    described = window.describe()
    gap = series.coverage.describe()["note"] if series.coverage else ""
    return _error(
        TianjiError(f"窗口 {described['from']} ~ {described['to']} 内没有可取到的数据。{gap}".strip())
    )


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
        "period 支持人话时段（未来24小时 / 今天 / 明天下午 / 2026-12-01~2026-12-03），"
        "留空则从现在起 hours 小时；scenario 可选 wind（风电）/pv（光伏）/grid（电网）/general。"
    )
)
def power_weather_hint(
    location: str,
    period: str = "",
    hours: int = 24,
    scenario: str = "general",
    cut_out: float = 25.0,
) -> dict:
    scenario = scenario if scenario in DEFAULT_FIELDS else "general"
    try:
        window = _window_for(period, hours)
        series = _client().series_for_window(location, window).project(DEFAULT_FIELDS[scenario])
    except TianjiError as exc:
        return _error(exc)
    empty = _require_rows(series, window)
    if empty:
        return empty
    result = hints.detect(series.rows, scenario=scenario, cut_out=cut_out)
    result["window"] = {
        "location": location,
        **window.describe(),
        "first": series.rows[0]["time"] if series.rows else None,
        "last": series.rows[-1]["time"] if series.rows else None,
    }
    return _envelope(result, series)


@server.tool(
    description=(
        "风电/光伏关心的关键要素逐时快照（含轮毂风速、露点、风力等级等派生量）。"
        "period 支持人话时段，留空则从现在起 hours 小时。"
    )
)
def wind_solar_snapshot(location: str, period: str = "", hours: int = 24, t_res: str = "1h") -> dict:
    fields = ["t2m", "rh2m", "ssrd", "ws10m", "ws100m", "wd100m", "cldt", "tp"]
    try:
        window = _window_for(period, hours)
        series = _client().series_for_window(location, window, t_res).project(fields)
    except TianjiError as exc:
        return _error(exc)
    empty = _require_rows(series, window)
    if empty:
        return empty
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
        "理论出力演示（非功率预测）。period 支持人话时段，留空则从现在起 hours 小时。"
        "kind=wind 按功率曲线把风速映射为出力，"
        "切入/额定/切出、轮毂高度、容量、自定义 curve 均可指定；"
        "kind=pv 按辐照度×性能比×温度修正算光伏出力，pr/tilt_gain 可指定。"
    )
)
def theoretical_power(
    location: str,
    period: str = "",
    hours: int = 24,
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
            window = _window_for(period, hours)
            series = _client().series_for_window(location, window).project(["ws10m", "ws100m", "t2m"])
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
            window = _window_for(period, hours)
            series = _client().series_for_window(location, window).project(["ssrd", "t2m"])
            result = pv.simulate(
                series.rows,
                capacity_mw=capacity_mw,
                pr=pr,
                tilt_gain=tilt_gain,
            )
    except (ValueError, TianjiError) as exc:
        return _error(exc)
    empty = _require_rows(series, window)
    if empty:
        return empty
    return _envelope(result, series)


@server.tool(
    description=(
        "逐日汇总：把窗口按自然日聚合成日表（温度/湿度/风速/辐照/云量/降水），"
        "首/末日不足整天时 span 会标注实际时段；降水是小时率，日量按逐时求和。"
        "kind=general 仅气象；kind=wind / pv / both 附加逐日理论电量、容量因子与零出力小时"
        "（演示模型，非功率预测，参数口径同 theoretical_power）。"
        "period 支持人话时段（未来3天 / 明天 / 2026-12-01~2026-12-03），留空则从现在起 hours 小时。"
        "复用同一份缓存，同坐标的后续提问不额外消耗额度。"
    )
)
def daily_summary(
    location: str,
    period: str = "",
    hours: int = 24,
    kind: str = "general",
    hub_height: float = 100.0,
    cut_in: float = 3.0,
    rated: float = 12.0,
    cut_out: float = 25.0,
    capacity_mw: float = 5.0,
    curve: list[list[float]] | None = None,
    pr: float = 0.82,
    tilt_gain: float = 1.0,
    t_res: str = "1h",
) -> dict:
    kind = (kind or "general").strip().lower()
    if kind not in ("general", "wind", "pv", "both"):
        return _error(ValueError(f"kind 只能是 general / wind / pv / both，收到 {kind!r}"))
    want_wind = kind in ("wind", "both")
    want_pv = kind in ("pv", "both")
    try:
        if want_wind:
            power.check_params(cut_in, rated, cut_out, hub_height, capacity_mw)
        if want_pv:
            pv.check_params(capacity_mw, pr, -0.004, tilt_gain)
        window = _window_for(period, hours)
        fields = ["t2m", "rh2m", "ws10m", "ws100m", "cldt", "tp", "pres", "ssrd"]
        series = _client().series_for_window(location, window, t_res).project(fields)
    except (ValueError, TianjiError) as exc:
        return _error(exc)
    empty = _require_rows(series, window)
    if empty:
        return empty
    step_hours = 0.25 if str(t_res).strip().lower() == "15min" else 1.0
    result = daily.daily_summary(
        series.rows,
        kind=kind,
        step_hours=step_hours,
        hub_height=hub_height,
        cut_in=cut_in,
        rated=rated,
        cut_out=cut_out,
        capacity_mw=capacity_mw,
        curve=curve,
        pr=pr,
        tilt_gain=tilt_gain,
    )
    result["window"] = {
        "location": location,
        **window.describe(),
        "first": series.rows[0]["time"] if series.rows else None,
        "last": series.rows[-1]["time"] if series.rows else None,
    }
    return _envelope(result, series)


@server.tool(
    description=(
        "唯一的取数入口：返回指定时段内全部已授权要素（服务端不支持筛选，故一次取全）。"
        "period 支持人话时段（未来24小时 / 今天 / 明天 / 2026-12-01~2026-12-03），"
        "留空则从现在起 hours 小时。其他加工类工具会复用同一份缓存，**不需要**先调用本工具。"
        "fields 可选，用于投影列；limit>0 时只截断本次返回的行数。"
        "响应里的 api.coverage 说明数据相对请求窗口的覆盖情况，complete=false 时请把缺失小时数转述给用户。"
    )
)
def get_dataset(
    location: str,
    period: str = "",
    hours: int = 24,
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
        window = _window_for(period, hours)
        series = _client().series_for_window(location, window, t_res).project(codes)
    except TianjiError as exc:
        return _error(exc)

    empty = _require_rows(series, window)
    if empty:
        return empty
    window_rows = len(series.rows)
    # total_rows 指这次取回的完整数据集行数（从起报锚定、含窗口外余量），不是本次返回的行数
    total = int(series.dataset_rows or window_rows)
    cut = max(0, int(limit or 0))
    rows = series.rows[:cut] if cut > 0 else series.rows
    notes: list[str] = []
    if total > window_rows:
        notes.append(f"缓存完整数据集 {total} 行（自起报锚定），本次窗口 {window_rows} 行，余量供后续提问复用")
    if len(rows) < window_rows:
        notes.append(f"limit 只截断本次返回：已返回前 {len(rows)} 行，其它工具仍用完整数据")
    coverage = series.coverage.describe() if series.coverage else None
    if coverage and not coverage["complete"]:
        notes.append(coverage["note"])
    note = "；".join(notes) + "。" if notes else ""
    return _envelope(
        {
            "window": window.describe(),
            "rows": rows,
            "window_rows": window_rows,
            "total_rows": total,
            "returned_rows": len(rows),
            "truncated": len(rows) < window_rows,
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

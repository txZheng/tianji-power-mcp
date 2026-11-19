"""固版输出（render profile）：把分析结果固化成字节稳定的成稿。

设计契约（0.5.0，与用户确认）：
- profile 是纯函数：同一份取数 → 一字不差的 markdown；舍入位数、日期格式、段落与列序全部写死；
- 溯源只写数据属性（起报时刻、窗口），不写渲染时间——缓存命中后重渲染不应改变成稿；
- 改格式必须升 format_version + 记 CHANGELOG + 重做金样本（tests/fixtures/*.golden.md，CI 看护）；
- 「要点」段由规则从数据直接计算（极值 / 阈值命中 / 缺口声明），不是模型写的；
- 返回 markdown 的同时携带结构化 payload，供追问与非 LLM 消费者复用。
"""

from __future__ import annotations

from datetime import datetime

from . import daily, hints, power, pv
from .window import CST, iso

# 风险行固定关心的灾种顺序（展示名取 hints 事件里的中文 name）
RISK_HAZARDS = ("wind_strong", "wind_turbine", "icing", "heavy_rain", "cold", "heat")


def _parse(stamp):
    if not stamp:
        return None
    try:
        moment = datetime.fromisoformat(str(stamp))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=CST)
    return moment.astimezone(CST)


def _short_date(stamp) -> str:
    """'2026-11-19T18:00+08:00' → '11-19'。"""
    moment = _parse(stamp)
    return moment.strftime("%m-%d") if moment else "—"


def _short_datetime(stamp) -> str:
    """'2026-11-19T18:00+08:00' → '11-19 18:00'。"""
    moment = _parse(stamp)
    return moment.strftime("%m-%d %H:%M") if moment else "—"


def _full_datetime(stamp) -> str:
    """起报时刻保留完整年份：'2026-11-19 08:00'。"""
    moment = _parse(stamp)
    return moment.strftime("%Y-%m-%d %H:%M") if moment else "—"


def _hour_zh(stamp) -> str:
    """峰值出现时刻：'11-21 12时'。"""
    moment = _parse(stamp)
    return f"{moment.strftime('%m-%d %H')}时" if moment else "—"


def _num(value, digits: int = 1, dash: str = "—") -> str:
    return dash if value is None else f"{float(value):.{digits}f}"


def _g(value, dash: str = "—") -> str:
    """整数语义的数（小时数等）：7.0 → '7'。"""
    return dash if value is None else f"{float(value):g}"


def _three_day_outlook(
    series,
    window,
    location,
    *,
    hub_height=power.DEFAULT_PARAMS["hub_height"],
    cut_in=power.DEFAULT_PARAMS["cut_in"],
    rated=power.DEFAULT_PARAMS["rated"],
    cut_out=power.DEFAULT_PARAMS["cut_out"],
    capacity_mw=power.DEFAULT_PARAMS["capacity_mw"],
    curve=None,
    pr=pv.DEFAULT_PARAMS["pr"],
    tilt_gain=pv.DEFAULT_PARAMS["tilt_gain"],
) -> dict:
    rows = series.rows
    result = daily.daily_summary(
        rows,
        kind="both",
        hub_height=hub_height,
        cut_in=cut_in,
        rated=rated,
        cut_out=cut_out,
        capacity_mw=capacity_mw,
        curve=curve,
        pr=pr,
        tilt_gain=tilt_gain,
    )
    detected = hints.detect(rows, scenario="wind", cut_out=cut_out)
    days = result["days"]
    totals = result["totals"]

    partial_bits = []
    if days and days[0]["hours"] < 24:
        partial_bits.append(f"首日仅 {_g(days[0]['hours'])} 小时")
    if len(days) > 1 and days[-1]["hours"] < 24:
        partial_bits.append(f"末日仅 {_g(days[-1]['hours'])} 小时")
    partial_note = ("，" + "、".join(partial_bits)) if partial_bits else ""
    coverage_note = (series.coverage.describe()["note"] if series.coverage else "—")

    label = window.label or f"未来{window.hours}小时"
    lines: list[str] = []
    lines.append(f"# {location} {label}风光展望")
    lines.append("")
    lines.append(
        f"起报 {_full_datetime(series.time_init)} ｜ "
        f"窗口 {_short_datetime(iso(window.start))} → {_short_datetime(iso(window.end))}"
        f"（{_g(totals['hours'])}h{partial_note}）"
    )
    lines.append(f"口径：{_g(capacity_mw)}MW 默认功率曲线 ｜ 光伏 GHI×PR{_g(pr)}（理论演示，非功率预测）")
    lines.append("")
    lines.append("| 日期 | 时段 | 气温℃ | 100m风 均值/极大 m/s | 云量 | 光伏 MWh | 风电 MWh | 零出力 h |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for day in days:
        t2m = day["t2m"]
        temp = "—" if not t2m else f"{_num(t2m['min'])}~{_num(t2m['max'])}"
        ws = day["ws100m"]
        wind_txt = "—" if not ws else f"{_num(ws['mean'])}/{_num(ws['max'])}"
        cldt = day["cldt"]
        cloud_txt = "—" if not cldt else _num(cldt["mean"], 2)
        lines.append(
            f"| {_short_date(day['date'])} | {day['span']} | {temp} | {wind_txt} | {cloud_txt} "
            f"| {_num(day['pv']['energy_mwh'])} | {_num(day['wind']['energy_mwh'])} "
            f"| {_g(day['wind']['zero_output_hours'])} |"
        )
    lines.append(
        f"| 合计 | {_g(totals['hours'])}h | — | — | — "
        f"| {_num(totals['pv']['energy_mwh'])} | {_num(totals['wind']['energy_mwh'])} "
        f"| {_g(totals['wind']['zero_output_hours'])} |"
    )

    lines.append("")
    lines.append("要点：")
    wind_days = list(days)
    best = max(wind_days, key=lambda d: d["wind"]["energy_mwh"])
    worst = min(wind_days, key=lambda d: d["wind"]["energy_mwh"])
    if best is worst:
        seg = f"- 风电：本时段 {best['wind']['energy_mwh']:.1f} MWh"
        if best["wind"]["zero_output_hours"]:
            seg += f"，零出力 {_g(best['wind']['zero_output_hours'])}h"
        lines.append(seg)
    else:
        seg = (
            f"- 风电：最大日 {_short_date(best['date'])}（{best['wind']['energy_mwh']:.1f} MWh），"
            f"最小日 {_short_date(worst['date'])}（{worst['wind']['energy_mwh']:.1f} MWh）"
        )
        if totals["wind"]["zero_output_hours"]:
            seg += f"，零出力合计 {_g(totals['wind']['zero_output_hours'])}h"
        lines.append(seg)

    pv_tot = totals["pv"]
    peak_day = max(days, key=lambda d: d["pv"]["peak_mw"] or 0.0)
    pv_peak_time = peak_day["pv"]["peak_time"]
    lines.append(
        f"- 光伏：合计 {pv_tot['energy_mwh']:.1f} MWh"
        f"（容量因子 {pv_tot['capacity_factor'] * 100:.1f}%），"
        f"峰值 {_num(pv_tot['peak_mw'], 2)} MW（{_hour_zh(pv_peak_time)}）"
    )

    tp_sum = totals.get("tp_sum_mm")
    if tp_sum:
        lines.append(f"- 降水：窗口合计 {tp_sum:.1f} mm")
    else:
        lines.append("- 降水：全程无有效降水")

    temps = [(row.get("t2m"), row.get("time")) for row in rows if row.get("t2m") is not None]
    if temps:
        tmin = min(temps, key=lambda pair: pair[0])
        tmax = max(temps, key=lambda pair: pair[0])
        lines.append(
            f"- 气温：最低 {tmin[0]:.1f}℃（{_hour_zh(tmin[1])}），最高 {tmax[0]:.1f}℃（{_hour_zh(tmax[1])}）"
        )
    else:
        lines.append("- 气温：数据缺失")

    by_hazard: dict[str, list[dict]] = {}
    for event in detected["events"]:
        by_hazard.setdefault(event["hazard"], []).append(event)
    parts = []
    for hazard in RISK_HAZARDS:
        group = by_hazard.get(hazard)
        if group:
            hours_sum = sum(item["hours"] for item in group)
            parts.append(f"{group[0]['name']}{len(group)}段（共{hours_sum}h）")
    lines.append("- 风险：" + ("；".join(parts) if parts else "无大风/覆冰/强降水事件"))

    lines.append("")
    lines.append("数据说明：")
    lines.append(f"- 覆盖：{coverage_note}")
    if detected["data_gaps"]:
        lines.append(f"- 提示缺口：{'；'.join(detected['data_gaps'])}")
    lines.append(
        "- 免责：理论出力为演示模型（功率曲线 / 辐照×PR×温度修正），非功率预测；"
        "参考提示，非正式气象预报，不构成生产指令"
    )

    return {
        "profile": "three_day_outlook",
        "format_version": 1,
        "markdown": "\n".join(lines) + "\n",
        "payload": {
            "daily": result,
            "events": detected["events"],
            "data_gaps": detected["data_gaps"],
        },
    }


PROFILES: dict[str, dict] = {
    "three_day_outlook": {
        "version": 1,
        "title": "三日风光展望",
        "description": "风光双算：逐日表 + 规则要点 + 固定口径与免责；period 建议「未来N天」，也可自定义时段",
        "render": _three_day_outlook,
    },
}


def render_profile(series, window, location: str, profile: str = "three_day_outlook", **params) -> dict:
    """按 profile 渲染固版成稿；未知 profile 抛 ValueError（调用方转成错误响应）。"""
    entry = PROFILES.get(profile)
    if entry is None:
        names = ", ".join(f"{key}（{value['title']}）" for key, value in PROFILES.items())
        raise ValueError(f"未知 profile：{profile!r}；可用：{names}")
    return entry["render"](series, window, location, **params)

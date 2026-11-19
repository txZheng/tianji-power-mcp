"""提示规则引擎。

定位：只给"基于天气数据的提示"，不做功率预测、不做发电量预报。
每条规则都带明确的触发条件与阈值，输出里给出证据，便于使用者核对。

复现口径：阈值一律写成常量并随结果返回，便于客户按自己的运行规程覆盖。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import derive

LEVELS = {"low": 1, "moderate": 2, "high": 3, "extreme": 4}
LEVEL_NAMES = {"low": "提示", "moderate": "关注", "high": "较高风险", "extreme": "高风险"}

# 阈值（可被调用方覆盖）
THRESHOLDS: dict[str, float] = {
    "wind_moderate": 10.8,   # 6 级
    "wind_high": 17.2,       # 8 级
    "wind_extreme": 24.4,    # 9~10 级
    "icing_temp_max": 0.5,
    "icing_temp_min": -6.0,
    "icing_rh": 90.0,
    "icing_rh_high": 95.0,
    "cold_moderate": -10.0,
    "cold_high": -20.0,
    "heat_moderate": 35.0,
    "heat_high": 37.0,
    "rain_moderate": 5.0,    # mm/hr
    "rain_high": 10.0,
    "cloud_pv": 0.8,
    "cloud_pv_high": 0.9,
    "fog_dpd": 1.5,          # 露点差 ℃
    "fog_wind": 3.0,         # m/s
}

# 各行业场景关心的事件
SCENARIO_HAZARDS: dict[str, set[str]] = {
    "wind": {"wind_strong", "wind_turbine", "icing", "cold", "heat", "heavy_rain"},
    "pv": {"wind_strong", "pv_cloud", "icing", "cold", "heat", "heavy_rain"},
    "grid": {"wind_strong", "icing", "cold", "heat", "heavy_rain", "fog_proxy"},
    "general": {"wind_strong", "cold", "heat", "heavy_rain", "fog_proxy"},
}


@dataclass
class Event:
    hazard: str
    name: str
    level: str
    start: str
    end: str
    hours: int
    trigger: str
    peak: dict = field(default_factory=dict)
    note: str = ""

    def as_dict(self) -> dict:
        return {
            "hazard": self.hazard,
            "name": self.name,
            "level": self.level,
            "level_zh": LEVEL_NAMES.get(self.level, self.level),
            "start": self.start,
            "end": self.end,
            "hours": self.hours,
            "trigger": self.trigger,
            "peak": self.peak,
            "note": self.note,
        }


def _row_flags(row: dict, scenario: str, ctx: dict[str, float]) -> list[dict]:
    flags: list[dict] = []
    t2m, rh, tp = row.get("t2m"), row.get("rh2m"), row.get("tp")
    pres, cldt, ws10 = row.get("pres"), row.get("cldt"), row.get("ws10m")
    hub = row.get("hub_ws100")
    dpt = row.get("dpt2m")

    if ws10 is not None:
        if ws10 >= ctx["wind_extreme"]:
            flags.append({"hazard": "wind_strong", "name": "大风", "level": "extreme",
                          "trigger": f"10米风速 {ws10:.1f} m/s（{derive.beaufort_name(ws10)}）"})
        elif ws10 >= ctx["wind_high"]:
            flags.append({"hazard": "wind_strong", "name": "大风", "level": "high",
                          "trigger": f"10米风速 {ws10:.1f} m/s（{derive.beaufort_name(ws10)}）"})
        elif ws10 >= ctx["wind_moderate"]:
            flags.append({"hazard": "wind_strong", "name": "大风", "level": "moderate",
                          "trigger": f"10米风速 {ws10:.1f} m/s（{derive.beaufort_name(ws10)}）"})

    if hub is not None and ctx.get("cut_out"):
        ratio = hub / ctx["cut_out"]
        if ratio >= 1.0:
            flags.append({"hazard": "wind_turbine", "name": "风机切出", "level": "extreme",
                          "trigger": f"轮毂风速 {hub:.1f} m/s ≥ 切出 {ctx['cut_out']:.1f} m/s"})
        elif ratio >= 0.8:
            flags.append({"hazard": "wind_turbine", "name": "接近切出", "level": "high",
                          "trigger": f"轮毂风速 {hub:.1f} m/s，达切出风速的 {ratio * 100:.0f}%"})

    if t2m is not None and rh is not None:
        wet = (tp is not None and tp > 0) or (pres is not None and pres > 0)
        if ctx["icing_temp_min"] <= t2m <= ctx["icing_temp_max"] and rh >= ctx["icing_rh"] and wet:
            level = "high" if (rh >= ctx["icing_rh_high"] and pres is not None and pres > 0) else "moderate"
            flags.append({"hazard": "icing", "name": "覆冰风险", "level": level,
                          "trigger": f"t2m={t2m:.1f}°C、相对湿度 {rh:.0f}%、有降水"})

    if t2m is not None:
        if t2m <= ctx["cold_high"]:
            flags.append({"hazard": "cold", "name": "低温", "level": "high", "trigger": f"t2m={t2m:.1f}°C"})
        elif t2m <= ctx["cold_moderate"]:
            flags.append({"hazard": "cold", "name": "低温", "level": "moderate", "trigger": f"t2m={t2m:.1f}°C"})
        if t2m >= ctx["heat_high"]:
            flags.append({"hazard": "heat", "name": "高温", "level": "high", "trigger": f"t2m={t2m:.1f}°C"})
        elif t2m >= ctx["heat_moderate"]:
            flags.append({"hazard": "heat", "name": "高温", "level": "moderate", "trigger": f"t2m={t2m:.1f}°C"})

    if tp is not None:
        if tp >= ctx["rain_high"]:
            flags.append({"hazard": "heavy_rain", "name": "强降水", "level": "high", "trigger": f"降水 {tp:.1f} mm/h"})
        elif tp >= ctx["rain_moderate"]:
            flags.append({"hazard": "heavy_rain", "name": "强降水", "level": "moderate", "trigger": f"降水 {tp:.1f} mm/h"})

    if row.get("daytime") and cldt is not None:
        if cldt >= ctx["cloud_pv_high"]:
            flags.append({"hazard": "pv_cloud", "name": "云遮挡（光伏）", "level": "high",
                          "trigger": f"总云量 {cldt:.2f}（{cldt * 100:.0f}%），日照时段"})
        elif cldt >= ctx["cloud_pv"]:
            flags.append({"hazard": "pv_cloud", "name": "云遮挡（光伏）", "level": "moderate",
                          "trigger": f"总云量 {cldt:.2f}（{cldt * 100:.0f}%），日照时段"})

    if dpt is not None and t2m is not None and ws10 is not None:
        dpd = t2m - dpt
        if dpd <= ctx["fog_dpd"] and ws10 <= ctx["fog_wind"]:
            clear = cldt is not None and cldt < 0.3
            flags.append({"hazard": "fog_proxy", "name": "低能见度/凝露（代理）",
                          "level": "high" if clear else "moderate",
                          "trigger": f"露点差 {dpd:.1f}°C、风速 {ws10:.1f} m/s",
                          "note": "无 visibility 要素，判据为代理"})

    allowed = SCENARIO_HAZARDS.get(scenario, SCENARIO_HAZARDS["general"])
    return [flag for flag in flags if flag["hazard"] in allowed]


def detect(rows: list[dict], scenario: str = "general", cut_out: float | None = None) -> dict:
    """把逐小时数据扫一遍，合并成事件。"""
    ctx = dict(THRESHOLDS)
    if cut_out:
        ctx["cut_out"] = float(cut_out)

    events: list[Event] = []
    open_events: dict[str, Event] = {}
    enriched = [derive.enrich(row) for row in rows]

    for row in enriched:
        stamp = str(row.get("time", ""))[:16]
        active = {flag["hazard"]: flag for flag in _row_flags(row, scenario, ctx)}
        for hazard, flag in active.items():
            event = open_events.get(hazard)
            if event is None:
                event = Event(hazard=flag["hazard"], name=flag["name"], level=flag["level"],
                              start=stamp, end=stamp, hours=1, trigger=flag["trigger"],
                              note=flag.get("note", ""))
                events.append(event)
                open_events[hazard] = event
            else:
                event.end = stamp
                event.hours += 1
                if LEVELS[flag["level"]] > LEVELS[event.level]:
                    event.level = flag["level"]
                    event.trigger = flag["trigger"]
            values = {k: row.get(k) for k in ("t2m", "rh2m", "ws10m", "ws100m", "tp", "pres", "cldt", "ssrd")}
            event.peak = {"time": stamp, "values": {k: (round(v, 2) if isinstance(v, float) else v) for k, v in values.items()}}
        for hazard in list(open_events):
            if hazard not in active:
                open_events.pop(hazard, None)

    events.sort(key=lambda e: (e.start, -LEVELS[e.level]))
    peak_level = max((e.level for e in events), key=lambda lv: LEVELS[lv], default="low")
    gaps = [
        "无能见度要素（vis 未授权且名称未确认），低能见度为代理判据",
        "无液态水含量与冻雨要素（prei 未授权），覆冰为气象条件代理，不代表冰厚",
    ]
    return {
        "scenario": scenario,
        "peak_level": peak_level,
        "peak_level_zh": LEVEL_NAMES.get(peak_level, peak_level),
        "events": [event.as_dict() for event in events],
        "thresholds": {k: v for k, v in ctx.items() if k != "cut_out"},
        "data_gaps": gaps,
    }

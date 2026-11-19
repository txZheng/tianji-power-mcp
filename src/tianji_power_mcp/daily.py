"""逐日汇总：把窗口内的逐时行按自然日聚合成一张日表。

口径（同时写进返回的 note，调用方不用猜）：
- `tp` 是小时率（mm/hr），日降水量必须**求和**，绝不能取最大；`pres`（降雪量）同样求和；
- `ssrd` 给峰值与「有光时段」均值，夜间 0 值不计入均值；
- 首/末日往往是半天（如「未来3天」含今晚剩余时段），`span` 会标注「起–止时」，不冒充全天；
- 风电/光伏列复用 power / pv 的演示模型（非功率预测），逐时算出力再按日累计；
- `step_hours` 是每行代表的小时数（1h 分辨率传 1.0，15min 传 0.25）。
"""

from __future__ import annotations

from datetime import datetime

from . import power, pv
from .derive import wind_at_height
from .window import CST

KINDS = ("general", "wind", "pv", "both")


def _parse_time(stamp) -> datetime | None:
    if not stamp:
        return None
    try:
        moment = datetime.fromisoformat(str(stamp))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=CST)
    return moment.astimezone(CST)


def _round(value, digits: int):
    return round(float(value), digits) if value is not None else None


def _numbers(rows: list[dict], name: str) -> list[float]:
    values: list[float] = []
    for row in rows:
        raw = row.get(name)
        if raw is None:
            continue
        try:
            values.append(float(raw))
        except (TypeError, ValueError):
            continue
    return values


def _min_max_mean(values: list[float], digits: int = 1) -> dict | None:
    if not values:
        return None
    return {
        "min": _round(min(values), digits),
        "max": _round(max(values), digits),
        "mean": _round(sum(values) / len(values), digits),
    }


def _mean_max(values: list[float], digits: int = 1) -> dict | None:
    if not values:
        return None
    return {
        "mean": _round(sum(values) / len(values), digits),
        "max": _round(max(values), digits),
    }


def _wind_hours(rows, hub_height, cut_in, rated, cut_out, capacity_mw, curve, step):
    """逐时理论出力 → (能量 MWh, 零出力小时, 切出小时, 峰值 MW)。"""
    energy = zero = cut_out_h = peak = 0.0
    for row in rows:
        hub_ws = wind_at_height(row.get("ws10m"), row.get("ws100m"), hub_height)
        pu = power.pu_from_curve(hub_ws, curve) if curve else power.pu_from_wind(hub_ws, cut_in, rated, cut_out)
        mw = pu * capacity_mw
        energy += mw * step
        if pu <= 0.0:
            zero += step
        if hub_ws is not None and hub_ws >= cut_out:
            cut_out_h += step
        peak = max(peak, mw)
    return energy, zero, cut_out_h, peak


def _pv_hours(rows, capacity_mw, pr, tilt_gain, step):
    """逐时光伏出力 → (能量 MWh, 峰值 MW, 有光行数)。"""
    energy = peak = 0.0
    daylight = 0
    for row in rows:
        pu = pv.pu_from_ghi(row.get("ssrd"), row.get("t2m"), pr=pr, tilt_gain=tilt_gain)
        mw = pu * capacity_mw
        energy += mw * step
        peak = max(peak, mw)
        raw = row.get("ssrd")
        if raw is None:
            continue
        try:
            if float(raw) > 0:
                daylight += 1
        except (TypeError, ValueError):
            continue
    return energy, peak, daylight


def daily_summary(
    rows: list[dict],
    *,
    kind: str = "general",
    step_hours: float = 1.0,
    hub_height: float = power.DEFAULT_PARAMS["hub_height"],
    cut_in: float = power.DEFAULT_PARAMS["cut_in"],
    rated: float = power.DEFAULT_PARAMS["rated"],
    cut_out: float = power.DEFAULT_PARAMS["cut_out"],
    capacity_mw: float = power.DEFAULT_PARAMS["capacity_mw"],
    curve: list[list[float]] | None = None,
    pr: float = pv.DEFAULT_PARAMS["pr"],
    tilt_gain: float = pv.DEFAULT_PARAMS["tilt_gain"],
) -> dict:
    """按自然日聚合 `rows`；`kind` 控制是否附加风电/光伏理论出力列。"""
    if kind not in KINDS:
        raise ValueError(f"kind 只能是 {' / '.join(KINDS)}，收到 {kind!r}")
    step = float(step_hours) or 1.0
    want_wind = kind in ("wind", "both")
    want_pv = kind in ("pv", "both")

    grouped: dict[str, list[dict]] = {}
    for row in rows:
        moment = _parse_time(row.get("time"))
        if moment is None:
            continue
        grouped.setdefault(moment.strftime("%Y-%m-%d"), []).append(row)

    full_rows = max(1, round(24 / step))
    days: list[dict] = []
    total_hours = 0.0
    tp_seen = False
    tp_total = 0.0
    wind_energy = wind_zero = wind_peak = 0.0
    pv_energy = pv_peak = 0.0
    pv_daylight = 0

    for date in sorted(grouped):
        day_rows = grouped[date]
        hours = round(len(day_rows) * step, 2)
        total_hours += hours

        first = _parse_time(day_rows[0].get("time"))
        last = _parse_time(day_rows[-1].get("time"))
        if first and last and len(day_rows) >= full_rows and (first.hour, first.minute) == (0, 0):
            span = "全天"
        elif first and last:
            span = f"{first.hour:02d}–{last.hour + 1:02d}时"
        else:
            span = ""

        t2m = _numbers(day_rows, "t2m")
        rh2m = _numbers(day_rows, "rh2m")
        ws10m = _numbers(day_rows, "ws10m")
        ws100m = _numbers(day_rows, "ws100m")
        cldt = _numbers(day_rows, "cldt")
        ssrd = _numbers(day_rows, "ssrd")
        tp = _numbers(day_rows, "tp")
        pres = _numbers(day_rows, "pres")
        lit = [value for value in ssrd if value > 0]

        entry: dict = {
            "date": date,
            "hours": hours,
            "span": span,
            "t2m": _min_max_mean(t2m, 1),
            "rh2m": _min_max_mean(rh2m, 1),
            "ws10m": _mean_max(ws10m, 1),
            "ws100m": _mean_max(ws100m, 1),
            "cldt": _mean_max(cldt, 2),
            "ssrd": {
                "max": _round(max(ssrd), 0) if ssrd else None,
                "daylight_mean": _round(sum(lit) / len(lit), 0) if lit else None,
            },
            "daylight_hours": len(lit) if ssrd else None,
            "tp_sum_mm": _round(sum(tp), 1) if tp else None,
            "rain_hours": round(sum(1 for value in tp if value >= 0.1) * step, 2) if tp else None,
            "pres_sum_mm": _round(sum(pres), 1) if pres else None,
        }
        if tp:
            tp_seen = True
            tp_total += sum(tp)

        if want_wind:
            energy, zero, cut_out_h, peak = _wind_hours(
                day_rows, hub_height, cut_in, rated, cut_out, capacity_mw, curve, step
            )
            entry["wind"] = {
                "energy_mwh": _round(energy, 1),
                "mean_mw": _round(energy / hours, 3) if hours else None,
                "peak_mw": _round(peak, 3),
                "capacity_factor": _round(energy / (capacity_mw * hours), 4) if hours and capacity_mw else None,
                "zero_output_hours": round(zero, 2),
                "cut_out_hours": round(cut_out_h, 2),
            }
            wind_energy += energy
            wind_zero += zero
            wind_peak = max(wind_peak, peak)

        if want_pv:
            energy, peak, daylight = _pv_hours(day_rows, capacity_mw, pr, tilt_gain, step)
            entry["pv"] = {
                "energy_mwh": _round(energy, 1),
                "mean_mw": _round(energy / hours, 3) if hours else None,
                "peak_mw": _round(peak, 3),
                "capacity_factor": _round(energy / (capacity_mw * hours), 4) if hours and capacity_mw else None,
                "daylight_hours": daylight,
            }
            pv_energy += energy
            pv_peak = max(pv_peak, peak)
            pv_daylight += daylight

        days.append(entry)

    totals: dict = {
        "days": len(days),
        "hours": round(total_hours, 2),
        "tp_sum_mm": _round(tp_total, 1) if tp_seen else None,
    }
    if want_wind:
        totals["wind"] = {
            "energy_mwh": _round(wind_energy, 1),
            "capacity_factor": (
                _round(wind_energy / (capacity_mw * total_hours), 4) if total_hours and capacity_mw else None
            ),
            "zero_output_hours": round(wind_zero, 2),
            "peak_mw": _round(wind_peak, 3),
        }
    if want_pv:
        totals["pv"] = {
            "energy_mwh": _round(pv_energy, 1),
            "capacity_factor": (
                _round(pv_energy / (capacity_mw * total_hours), 4) if total_hours and capacity_mw else None
            ),
            "daylight_hours": pv_daylight,
            "peak_mw": _round(pv_peak, 3),
        }

    params: dict = {"kind": kind, "step_hours": step}
    if want_wind:
        params["wind"] = {
            "hub_height": hub_height,
            "cut_in": cut_in,
            "rated": rated,
            "cut_out": cut_out,
            "capacity_mw": capacity_mw,
            "curve_mode": "custom" if curve else "default_cubic",
        }
    if want_pv:
        params["pv"] = {"capacity_mw": capacity_mw, "pr": pr, "tilt_gain": tilt_gain}

    result = {
        "kind": kind,
        "params": params,
        "days": days,
        "totals": totals,
        "note": (
            "降水为小时率，日量 = 逐时求和（不是最大值）；ssrd 均值只统计有光时段；"
            "首/末日不足 24 小时时 span 标注实际时段"
        ),
    }
    if want_wind or want_pv:
        result["disclaimer"] = (
            "理论出力为演示模型（功率曲线 / 辐照×性能比×温度修正），非功率预测，"
            "不含机组可用率、尾流与调度约束"
        )
    return result

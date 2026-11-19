"""理论出力模型（演示用，不是功率预测）。

从风速经功率曲线映射到理论出力，用于"这个窗口理论上有多少出力"的量级感知。
默认曲线为通用风机形状，切入/额定/切出与容量均可由调用方指定，也可直接给功率曲线点位。
"""

from __future__ import annotations

import math

DEFAULT_PARAMS = {
    "cut_in": 3.0,        # m/s
    "rated": 12.0,        # m/s，达到额定的风速
    "cut_out": 25.0,      # m/s
    "hub_height": 100.0,  # m
    "capacity_mw": 5.0,   # 单机示例容量
}


def pu_from_wind(ws: float | None, cut_in: float, rated: float, cut_out: float) -> float:
    """标幺出力：切入以下与切出以上为 0，切入~额定段用三次方过渡。"""
    if ws is None:
        return 0.0
    ws = float(ws)
    if ws < cut_in or ws >= cut_out:
        return 0.0
    if ws >= rated:
        return 1.0
    return (ws**3 - cut_in**3) / (rated**3 - cut_in**3)


def pu_from_curve(ws: float | None, curve: list[list[float]]) -> float:
    """线性插值的自定义功率曲线，curve=[[风速, 标幺], ...]。"""
    if ws is None or not curve:
        return 0.0
    points = sorted((float(x), float(y)) for x, y in curve)
    if ws <= points[0][0]:
        return max(0.0, points[0][1]) if ws >= points[0][0] else 0.0
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        if x0 <= ws <= x1:
            if x1 == x0:
                return y1
            return y0 + (y1 - y0) * (ws - x0) / (x1 - x0)
    return max(0.0, points[-1][1])


def simulate(
    rows: list[dict],
    *,
    hub_height: float = DEFAULT_PARAMS["hub_height"],
    cut_in: float = DEFAULT_PARAMS["cut_in"],
    rated: float = DEFAULT_PARAMS["rated"],
    cut_out: float = DEFAULT_PARAMS["cut_out"],
    capacity_mw: float = DEFAULT_PARAMS["capacity_mw"],
    curve: list[list[float]] | None = None,
) -> dict:
    """逐小时理论出力 + 窗口汇总。"""
    from .derive import wind_at_height

    detail: list[dict] = []
    total_mw = 0.0
    zero_hours = cut_out_hours = 0
    for row in rows:
        hub_ws = wind_at_height(row.get("ws10m"), row.get("ws100m"), hub_height)
        pu = pu_from_curve(hub_ws, curve) if curve else pu_from_wind(hub_ws, cut_in, rated, cut_out)
        mw = pu * capacity_mw
        total_mw += mw
        if pu <= 0.0:
            zero_hours += 1
        if hub_ws is not None and hub_ws >= cut_out:
            cut_out_hours += 1
        detail.append(
            {
                "time": str(row.get("time", ""))[:16],
                "hub_ws": round(hub_ws, 2) if hub_ws is not None else None,
                "pu": round(pu, 4),
                "mw": round(mw, 3),
            }
        )

    n = len(detail)
    mean_mw = total_mw / n if n else 0.0
    equivalent_hours = total_mw / capacity_mw if capacity_mw else 0.0
    return {
        "params": {
            "hub_height": hub_height,
            "cut_in": cut_in,
            "rated": rated,
            "cut_out": cut_out,
            "capacity_mw": capacity_mw,
            "curve_mode": "custom" if curve else "default_cubic",
        },
        "summary": {
            "hours": n,
            "mean_mw": round(mean_mw, 3),
            "capacity_factor": round(mean_mw / capacity_mw, 4) if capacity_mw else None,
            "equivalent_full_load_hours": round(equivalent_hours, 1),
            "zero_output_hours": zero_hours,
            "cut_out_hours": cut_out_hours,
            "max_mw": round(max((d["mw"] for d in detail), default=0.0), 3),
        },
        "series": detail,
        "disclaimer": "理论出力：按指定功率曲线由风速映射，非功率预测，不含机组可用率与调度约束",
    }


def check_params(cut_in: float, rated: float, cut_out: float, hub_height: float, capacity_mw: float) -> None:
    if not (0 <= cut_in < rated < cut_out):
        raise ValueError(f"功率曲线参数需满足 0 ≤ cut_in < rated < cut_out，收到 {cut_in}/{rated}/{cut_out}")
    if not (10 <= hub_height <= 300):
        raise ValueError(f"hub_height 需在 [10,300] 米，收到 {hub_height}")
    if capacity_mw <= 0:
        raise ValueError(f"capacity_mw 必须为正，收到 {capacity_mw}")
    if math.isnan(cut_in) or math.isnan(cut_out):
        raise ValueError("功率曲线参数不能为 NaN")

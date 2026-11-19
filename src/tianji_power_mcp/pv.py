"""光伏理论出力模型（演示用，不是功率预测）。

思路：辐照度 → 组件温度修正 → 性能比 → 交流出力。参数都可以由调用方覆盖。

已知简化（都必须随结果说明）：
1. `ssrd` 是水平面总辐照（GHI），未做倾斜面（POA）转换。固定倾角阵列在冬季实际接收更多，
   `tilt_gain` 可以显式补这个增益，默认 1.0 表示"不补"，宁低不高。
2. 温度修正用 NOCT 经验式，不区分组件类型与安装方式（架空/贴屋面）。
3. 不含积灰、积雪覆盖、阴影、逆变器限功率曲线与线损的逐项建模，统一折进 `pr`。
"""

from __future__ import annotations

import math

DEFAULT_PARAMS = {
    "capacity_mw": 5.0,   # 交流侧容量
    "pr": 0.82,           # 性能比：逆变器+线损+积灰+失配的综合效率
    "gamma": -0.004,      # 功率温度系数 每 ℃
    "noct": 45.0,         # 标称工作温度 ℃
    "tilt_gain": 1.0,     # 倾斜面相对水平面的增益，1.0 = 不做转换
}


def cell_temperature(t_air: float | None, ghi: float | None, noct: float = 45.0) -> float | None:
    """NOCT 经验式：组件温度 ≈ 气温 + (NOCT-20)/800 × 辐照度。"""
    if t_air is None or ghi is None:
        return None
    return float(t_air) + (noct - 20.0) / 800.0 * max(0.0, float(ghi))


def pu_from_ghi(
    ghi: float | None,
    t_air: float | None,
    *,
    pr: float = DEFAULT_PARAMS["pr"],
    gamma: float = DEFAULT_PARAMS["gamma"],
    noct: float = DEFAULT_PARAMS["noct"],
    tilt_gain: float = DEFAULT_PARAMS["tilt_gain"],
) -> float:
    """标幺出力：辐照度/1000 × 性能比 × 温度修正，夜间为 0，上限 1。"""
    if ghi is None or t_air is None:
        return 0.0
    effective = max(0.0, float(ghi)) * tilt_gain
    if effective <= 0.0:
        return 0.0
    tcell = cell_temperature(t_air, effective, noct)
    temp_factor = 1.0 + gamma * (float(tcell) - 25.0)
    pu = effective / 1000.0 * pr * temp_factor
    return float(min(max(pu, 0.0), 1.0))


def simulate(
    rows: list[dict],
    *,
    capacity_mw: float = DEFAULT_PARAMS["capacity_mw"],
    pr: float = DEFAULT_PARAMS["pr"],
    gamma: float = DEFAULT_PARAMS["gamma"],
    noct: float = DEFAULT_PARAMS["noct"],
    tilt_gain: float = DEFAULT_PARAMS["tilt_gain"],
) -> dict:
    """逐小时理论出力 + 窗口汇总。"""
    detail: list[dict] = []
    total_mwh = 0.0
    daylight_hours = 0
    peak_mw = 0.0
    for row in rows:
        ghi = row.get("ssrd")
        t_air = row.get("t2m")
        pu = pu_from_ghi(ghi, t_air, pr=pr, gamma=gamma, noct=noct, tilt_gain=tilt_gain)
        mw = pu * capacity_mw
        total_mwh += mw  # 1 小时步长，MW × 1h = MWh
        if ghi is not None and float(ghi) > 0:
            daylight_hours += 1
        peak_mw = max(peak_mw, mw)
        detail.append(
            {
                "time": str(row.get("time", ""))[:16],
                "ghi": round(float(ghi), 1) if ghi is not None else None,
                "t_cell": round(cell_temperature(t_air, ghi, noct), 1) if t_air is not None and ghi is not None else None,
                "pu": round(pu, 4),
                "mw": round(mw, 3),
            }
        )

    n = len(detail)
    daylight_mean = total_mwh / daylight_hours if daylight_hours else 0.0
    return {
        "params": {
            "capacity_mw": capacity_mw,
            "pr": pr,
            "gamma": gamma,
            "noct": noct,
            "tilt_gain": tilt_gain,
            "irradiance_source": "ssrd（水平面总辐照）",
        },
        "summary": {
            "hours": n,
            "daylight_hours": daylight_hours,
            "energy_mwh": round(total_mwh, 2),
            "mean_mw": round(total_mwh / n, 3) if n else 0.0,
            "mean_mw_daylight": round(daylight_mean, 3),
            "capacity_factor": round(total_mwh / (capacity_mw * n), 4) if n and capacity_mw else None,
            "equivalent_full_load_hours": round(total_mwh / capacity_mw, 1) if capacity_mw else None,
            "peak_mw": round(peak_mw, 3),
        },
        "series": detail,
        "disclaimer": (
            "光伏理论出力：辐照度 × 性能比 × 温度修正，未做倾斜面转换与积灰/积雪建模，"
            "非功率预测；默认 tilt_gain=1.0 时对固定倾角阵列偏保守"
        ),
    }


def check_params(capacity_mw: float, pr: float, gamma: float, tilt_gain: float) -> None:
    if capacity_mw <= 0:
        raise ValueError(f"capacity_mw 必须为正，收到 {capacity_mw}")
    if not (0.3 <= pr <= 1.0):
        raise ValueError(f"性能比 pr 需在 [0.3,1.0]，收到 {pr}")
    if not (-0.01 <= gamma <= 0.0):
        raise ValueError(f"功率温度系数 gamma 需在 [-0.01,0]，收到 {gamma}")
    if not (0.5 <= tilt_gain <= 2.0):
        raise ValueError(f"tilt_gain 需在 [0.5,2.0]，收到 {tilt_gain}")
    if any(math.isnan(x) for x in (capacity_mw, pr, gamma, tilt_gain)):
        raise ValueError("参数不能为 NaN")

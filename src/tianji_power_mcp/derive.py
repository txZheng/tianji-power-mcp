"""派生量计算：温湿、风切变、风力等级、降水相态。纯函数，便于离线测试。"""

from __future__ import annotations

import math

# 蒲福风力等级上限（m/s，10 米高度标准）
BEAUFORT_UPPER = [0.2, 1.5, 3.3, 5.4, 7.9, 10.7, 13.8, 17.1, 20.7, 24.4, 28.4, 32.6]
BEAUFORT_NAMES = ["0级", "1级", "2级", "3级", "4级", "5级", "6级", "7级", "8级", "9级", "10级", "11级", "12级+"]


def dew_point(t2m: float | None, rh2m: float | None) -> float | None:
    """Magnus 公式；相对湿度按百分比输入。"""
    if t2m is None or rh2m is None:
        return None
    rh = min(max(float(rh2m), 0.1), 100.0)
    a, b = 17.62, 243.12
    gamma = math.log(rh / 100.0) + a * float(t2m) / (b + float(t2m))
    return b * gamma / (a - gamma)


def shear_alpha(ws10m: float | None, ws100m: float | None) -> float:
    """由 10m/100m 风速反算幂律指数，异常时退回 0.143（中性层结经验值）。"""
    if ws10m and ws100m and ws10m > 0.5 and ws100m > ws10m:
        alpha = math.log(ws100m / ws10m) / math.log(10.0)
        return min(max(alpha, 0.05), 0.40)
    return 0.143


def wind_at_height(ws10m: float | None, ws100m: float | None, height_m: float) -> float | None:
    """把风速外推到指定高度。100m 直接用 ws100m，避免重复换算。"""
    if ws100m is None and ws10m is None:
        return None
    if height_m <= 10 and ws10m is not None:
        return float(ws10m)
    if ws100m is None:
        if ws10m is None:
            return None
        return float(ws10m) * (height_m / 10.0) ** 0.143
    if abs(height_m - 100.0) < 1e-6:
        return float(ws100m)
    alpha = shear_alpha(ws10m, ws100m)
    return float(ws100m) * (height_m / 100.0) ** alpha


def beaufort_level(ws: float | None) -> int:
    if ws is None:
        return 0
    for level, upper in enumerate(BEAUFORT_UPPER, start=0):
        if ws <= upper:
            return level
    return 12


def beaufort_name(ws: float | None) -> str:
    return BEAUFORT_NAMES[beaufort_level(ws)]


def apparent_daytime(ssrd: float | None) -> bool:
    """用短波辐射判断白天：>50 W/㎡ 视为有日照。比算太阳高度角更省事且不引入时区误差。"""
    return ssrd is not None and float(ssrd) > 50.0


def snow_fraction(pres: float | None, tp: float | None) -> float | None:
    """降雪占总降水的比例，用于相态判定；缺任一或总量近零时返回 None。"""
    if pres is None or tp is None or float(tp) <= 1e-6:
        return None
    return min(max(float(pres) / float(tp), 0.0), 1.0)


def enrich(row: dict) -> dict:
    """在一行原始数据上补齐派生量，不修改入参。"""
    out = dict(row)
    out["dpt2m"] = dew_point(row.get("t2m"), row.get("rh2m"))
    out["hub_ws100"] = wind_at_height(row.get("ws10m"), row.get("ws100m"), 100.0)
    out["force"] = beaufort_level(row.get("ws10m"))
    out["daytime"] = apparent_daytime(row.get("ssrd"))
    out["snow_frac"] = snow_fraction(row.get("pres"), row.get("tp"))
    return out

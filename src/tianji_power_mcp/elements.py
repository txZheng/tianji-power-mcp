"""天机气象要素注册表。

来源：对 /v2 端点逐要素实测（2026-11-18，订阅 sub_e432...a2b38）。
只有这里列出的要素才会被组装进请求 —— 因为 20003 是整包拒绝，
一个未授权要素会让整次请求失败。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Element:
    code: str
    name: str
    unit: str
    group: str
    note: str = ""

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "name": self.name, "unit": self.unit, "group": self.group, "note": self.note}


# 实测已授权（13 个）
AUTHORIZED: dict[str, Element] = {
    e.code: e
    for e in (
        Element("t2m", "2米温度", "°C", "thermo"),
        Element("rh2m", "2米相对湿度", "%", "thermo"),
        Element("psz", "地表气压", "Pa", "pressure"),
        Element("tp", "总降水", "mm/hr", "precip", "小时率而非累积，窗口统计要求和不能取最大"),
        Element("pres", "降雪量", "mm", "precip", "接口返回单位为 mm，是小时量还是累积量待雪天实测确认"),
        Element("ssrd", "地表向下短波辐射", "W/㎡", "radiation"),
        Element("ws10m", "10米风速", "m/s", "wind", "蒲福风力等级按此要素换算"),
        Element("ws100m", "100米风速", "m/s", "wind", "常用轮毂高度近似值"),
        Element("wd100m", "100米风向", "°", "wind", "气象风向（风的来向）"),
        Element("cldt", "总云量", "0~1", "cloud", "小数，乘 100 得百分比"),
        Element("cldh", "高云量", "0~1", "cloud"),
        Element("cldm", "中云量", "0~1", "cloud"),
        Element("cldl", "低云量", "0~1", "cloud"),
    )
}

# 实测被拒（20003 要素未授权 / 20017 区域未授权），不要放进请求
DENIED: dict[str, str] = {
    "qnh": "未授权",
    "slp": "20017：全球要素，当前订阅为中国区域",
    "gust": "未授权",
    "cape": "未授权",
    "dpt2m": "未授权（可由 t2m+rh2m 计算）",
    "prer": "未授权",
    "prei": "未授权（影响重冰区判定）",
    "preg": "未授权",
    "ddsf_ave": "未授权",
    "bdsf_ave": "未授权",
    "max_reflectivity": "未授权",
    "ri_min": "未授权",
    "SOILT1": "未授权",
    "ws70m": "未授权",
    "t850": "未授权",
    "u100m": "未授权",
    "tcc": "未授权（EC 风格命名，天机用 cldt）",
    "DSWRFsfc": "未授权（EC 风格命名，天机用 ssrd）",
    "vis": "未验证：文档要素表无此项，且 20003 无法区分不存在与未订阅",
}

# 人话 → 码表，降低 agent 调用门槛
ALIASES: dict[str, str] = {
    "temperature": "t2m",
    "temp": "t2m",
    "humidity": "rh2m",
    "rh": "rh2m",
    "pressure": "psz",
    "precipitation": "tp",
    "rain": "tp",
    "snow": "pres",
    "snowfall": "pres",
    "radiation": "ssrd",
    "ghi": "ssrd",
    "wind_speed_10m": "ws10m",
    "wind_speed_100m": "ws100m",
    "wind_direction": "wd100m",
    "cloud_total": "cldt",
    "cloud_high": "cldh",
    "cloud_mid": "cldm",
    "cloud_low": "cldl",
}

# 取数策略：一次把全部已授权要素拼成一个请求（已经不是"按场景拆请求"）。
# 全部要素清单（顺序即请求顺序）。首次取数一次拿齐，之后按需投影。
ALL_FIELDS: list[str] = [
    "t2m",
    "rh2m",
    "psz",
    "tp",
    "pres",
    "ssrd",
    "ws10m",
    "ws100m",
    "wd100m",
    "cldt",
    "cldh",
    "cldm",
    "cldl",
]

# 单请求要素上限：先给 13（实测单请求最多验证过 9 个），
# 若服务端返回 20011 要素数量超限，客户端会自动二分重试。
MAX_FIELDS_PER_REQUEST = 13

# 各组默认取数组合
GROUP_FIELDS: dict[str, list[str]] = {
    "thermo": ["t2m", "rh2m"],
    "wind": ["ws10m", "ws100m", "wd100m"],
    "cloud": ["cldt", "cldl"],
    "precip": ["tp", "pres"],
    "radiation": ["ssrd"],
    "pressure": ["psz"],
}


def resolve(names: list[str]) -> tuple[list[str], list[str], list[str]]:
    """把用户/agent 给的要素名解析成码表。

    返回 (可用码, 未知名, 已知未授权码)。
    """
    ok: list[str] = []
    unknown: list[str] = []
    denied: list[str] = []
    for raw in names:
        name = raw.strip()
        code = name if name in AUTHORIZED else ALIASES.get(name.lower(), name)
        if code in AUTHORIZED:
            if code not in ok:
                ok.append(code)
        elif code in DENIED:
            denied.append(code)
        else:
            unknown.append(name)
    return ok, unknown, denied


def table() -> list[dict[str, str]]:
    return [element.as_dict() for element in AUTHORIZED.values()]

"""把「未来 24 小时」「明天」「2026-12-01~2026-12-03」这类说法解析成绝对时段。

天机数据锚定在**起报时刻**（北京时 08:00 / 20:00），不是请求时刻；
所以必须先算出绝对窗口，再决定取几天，最后把返回的数据裁到窗口内。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

CST = timezone(timedelta(hours=8))
RUN_HOURS = (8, 20)  # 北京时起报时刻，对应 UTC 00Z / 12Z
_CN_NUM = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}

# 日窗口的时段后缀：(起, 止)，止为 24 表示当日结束
_DAY_PARTS = {
    "上午": (8, 12),
    "下午": (12, 18),
    "白天": (8, 20),
    "夜间": (20, 24),
    "晚上": (20, 24),
    "全天": (0, 24),
}

_DAY_OFFSETS = {"今天": 0, "今日": 0, "明天": 1, "明日": 1, "后天": 2, "大后天": 3, "昨日": -1, "昨天": -1}


@dataclass(frozen=True)
class Window:
    start: datetime
    end: datetime
    label: str = ""
    kind: str = "calendar"  # rolling | calendar | range

    @property
    def hours(self) -> int:
        return max(1, int(round((self.end - self.start).total_seconds() / 3600)))

    def describe(self) -> dict:
        return {
            "label": self.label,
            "kind": self.kind,
            "from": iso(self.start),
            "to": iso(self.end),
            "hours": self.hours,
        }


def iso(value: datetime) -> str:
    return value.astimezone(CST).isoformat(timespec="minutes")


def now_cst() -> datetime:
    return datetime.now(CST)


def rolling_window(hours: int, now: datetime | None = None, label: str = "") -> Window:
    """从现在起 N 小时。"""
    base = (now or now_cst()).astimezone(CST)
    hours = max(1, int(hours))
    return Window(start=base, end=base + timedelta(hours=hours), label=label or f"未来{hours}小时", kind="rolling")


def calendar_window(day: datetime, start_hour: int = 0, end_hour: int = 24, label: str = "") -> Window:
    """某个自然日（或其中一段）的 00:00 起算窗口；end_hour=24 表示到次日 00:00。"""
    base = day.astimezone(CST).replace(hour=0, minute=0, second=0, microsecond=0)
    start = base + timedelta(hours=start_hour)
    end = base + timedelta(hours=end_hour)
    return Window(start=start, end=end, label=label or iso(start), kind="calendar")


def expected_run_init(now: datetime | None = None, lag_hours: float = 9.0) -> datetime:
    """估算「此刻能拿到的最新起报时刻」。

    起报在 08:00 / 20:00（北京时），可用时间固定滞后约 9 小时：
    08 时起报 → 当天 17:00 可用；20 时起报 → 次日 05:00 可用（即切换点 05:00 / 17:00）。
    实际发布可能晚于这个时刻，所以返回值是**乐观估计**：真实起报只会更早。
    取数时再拿响应里的 time_init 校正覆盖情况。
    """
    moment = (now or now_cst()).astimezone(CST)
    day = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    lag = timedelta(hours=max(0.0, float(lag_hours)))
    candidates = [
        day + timedelta(days=offset, hours=hour)
        for offset in (-2, -1, 0)
        for hour in RUN_HOURS
    ]
    available = [candidate for candidate in candidates if candidate + lag <= moment]
    if available:
        return max(available)
    return candidates[0]


def parse_period(text: str | None, now: datetime | None = None) -> Window | None:
    """解析人话时段；无法识别返回 None（交给调用方用 hours 兜底）。

    支持：未来24小时 / 未来7天 / 未来一周 / 今天 / 明天下午 / 后天夜间 /
          2026-12-01 / 2026-12-01~2026-12-03 / 2026-12-01T08:00~2026-12-02T08:00
    """
    if text is None:
        return None
    raw = str(text).strip()
    if not raw:
        return None
    base = (now or now_cst()).astimezone(CST)
    text = raw.replace("个", "").replace(" ", "")

    match = re.fullmatch(r"(?:未来|接下来|后面)?(\d{1,3}|[一二两三四五六七八九十])(小时|时|钟头|h)", text, re.IGNORECASE)
    if match:
        return rolling_window(_to_int(match.group(1)), base, label=raw)

    match = re.fullmatch(r"(?:未来|接下来|后面)?(\d{1,2}|[一二两三四五六七八九十])(天|日|d)", text, re.IGNORECASE)
    if match:
        return rolling_window(_to_int(match.group(1)) * 24, base, label=raw)

    if re.fullmatch(r"未来?(一|1)?(周|星期)", text):
        return rolling_window(24 * 7, base, label=raw)

    match = re.fullmatch(r"(今天|今日|明天|明日|后天|大后天|昨天|昨日)(上午|下午|白天|夜间|晚上|全天)?", text)
    if match:
        offset = _DAY_OFFSETS[match.group(1)]
        start_hour, end_hour = _DAY_PARTS.get(match.group(2) or "", (0, 24))
        return calendar_window(base + timedelta(days=offset), start_hour, end_hour, label=raw)

    window = _parse_explicit_range(text, raw)
    if window:
        return window
    return None


def _to_int(text: str) -> int:
    return _CN_NUM[text] if text in _CN_NUM else int(text)


_DATE = r"(\d{4})-(\d{1,2})-(\d{1,2})(?:[T ](\d{1,2}):(\d{2}))?"
_RANGE_RE = re.compile(_DATE + r"(?:~|～|至|到)" + _DATE)


def _parse_explicit_range(text: str, raw: str) -> Window | None:
    match = _RANGE_RE.fullmatch(text)
    if match:
        start = _make_datetime(*match.groups()[:5], default_hour=0)
        end = _make_datetime(*match.groups()[5:], default_hour=0)
        end = end + timedelta(days=1) if match.group(9) is None and match.group(10) is None else end
        if end <= start:
            raise ValueError(f"结束时间必须晚于开始时间：{raw}")
        return Window(start=start, end=end, label=raw, kind="range")

    match = re.fullmatch(_DATE, text)
    if match:
        day = _make_datetime(*match.groups(), default_hour=0)
        end = day.replace(hour=0, minute=0) + timedelta(days=1) if match.group(4) is None else day + timedelta(hours=1)
        return Window(start=day, end=end, label=raw, kind="range" if match.group(4) else "calendar")
    return None


def _make_datetime(year: str, month: str, day: str, hour: str | None, minute: str | None, default_hour: int = 0) -> datetime:
    try:
        return datetime(
            int(year), int(month), int(day), int(hour) if hour else default_hour, int(minute) if minute else 0, tzinfo=CST
        )
    except ValueError as exc:
        raise ValueError(f"日期不合法：{year}-{month}-{day}") from exc

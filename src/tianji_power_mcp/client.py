"""天机 /v2 客户端（自包含，仅用标准库）。

模板设计（v0.2，起报对齐版）
- **数据锚在起报时刻**：接口只返回「最新」一次起报（北京时 08:00 / 20:00），
  没有选择起报时次的参数；`fcst_days*24 + fcst_hours` 是**总时长**，数据从 `time_init + 1h` 开始。
  所以「未来 24 小时」这类说法必须换算成「从起报算起够不够 24 小时」，否则拿到的是
  「起报 +1h 起 24 小时」——最坏情况只有 3 小时落在未来。
- **多要余量再裁剪**：按预期起报估算需要几天，额外多要 `pad_hours` 小时；
  拿到数据后用响应里的 `time_init` 与首末时间核对，尾部不够补取一次，
  最后裁到调用方要的窗口，并把覆盖情况（`coverage`）带回去。
- **一次请求取齐**：全部已授权要素拼成一个请求，之后所有工具从同一份缓存投影。
- **额度自保护**：进程级调用预算 + 缓存 + 参数前置校验。
"""

from __future__ import annotations

import copy
import json
import math
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from . import elements
from .config import PROJECT_ROOT, Settings, load_settings
from .window import Window, expected_run_init, iso, now_cst, rolling_window

CACHE_TTL_SECONDS = 600.0
DENIED_STORE = PROJECT_ROOT / ".runtime" / "denied.json"
# 额度台账：每次真实请求（含失败尝试）追加一行，get_capability 可查累计/今日用量
USAGE_STORE = PROJECT_ROOT / ".runtime" / "usage.jsonl"

MAX_HOURS = 240
MAX_START_HOUR = 72
# 实测：3h 会被服务端拒绝（20010，仅提供 15min/1h）
T_RES_ALLOWED = ("1h", "15min")
_RESOLUTION = {"1h": timedelta(hours=1), "15min": timedelta(minutes=15)}


class TianjiError(RuntimeError):
    """API 或本地预算类错误，消息面向调用方（要能直接给 agent 看）。"""


class AuthError(TianjiError):
    pass


class FieldDenied(TianjiError):
    def __init__(self, fields: list[str], message: str):
        super().__init__(message)
        self.fields = fields


class RegionDenied(TianjiError):
    pass


class QuotaExhausted(TianjiError):
    pass


class BudgetExhausted(TianjiError):
    pass


class FetchLimitExceeded(TianjiError):
    """订阅限制（20009 / 10006）：请求的天数超过可查范围。"""


@dataclass
class Coverage:
    """这次取到的数据相对请求窗口的覆盖情况。"""

    requested_from: str
    requested_to: str
    run_time_init: str | None = None
    data_from: str | None = None
    data_to: str | None = None
    complete: bool = True
    missing_head_hours: int = 0
    missing_tail_hours: int = 0
    fcst_days: int = 0
    retried: bool = False

    def describe(self) -> dict:
        text = "完整覆盖请求窗口" if self.complete else self._gap_text()
        return {
            "requested_from": self.requested_from,
            "requested_to": self.requested_to,
            "run_time_init": self.run_time_init,
            "data_from": self.data_from,
            "data_to": self.data_to,
            "complete": self.complete,
            "missing_head_hours": self.missing_head_hours,
            "missing_tail_hours": self.missing_tail_hours,
            "fcst_days": self.fcst_days,
            "retried": self.retried,
            "note": text,
        }

    def _gap_text(self) -> str:
        parts = []
        if self.missing_head_hours:
            parts.append(
                f"窗口开头 {self.missing_head_hours} 小时缺数据：晚于最新起报（{self.run_time_init}）的部分无法回补"
            )
        if self.missing_tail_hours:
            parts.append(f"窗口末尾 {self.missing_tail_hours} 小时缺数据：超出可取的预报时长")
        return "；".join(parts) or "覆盖不完整"


@dataclass
class Series:
    rows: list[dict] = field(default_factory=list)
    units: dict[str, str] = field(default_factory=dict)
    fields: list[str] = field(default_factory=list)
    time_init: str | None = None
    calls: int = 0
    cached: bool = False
    dropped: list[str] = field(default_factory=list)
    # 缓存里的完整行数（含窗口外余量）
    dataset_rows: int = 0
    fcst_days: int = 0
    coverage: Coverage | None = None
    retried: bool = False

    def project(self, wanted: list[str]) -> "Series":
        """从完整数据集里投影出需要的列，不产生任何请求。"""
        keep = [name for name in wanted if name in self.fields]
        missing = [name for name in wanted if name not in keep]
        rows = [
            {"time": row.get("time"), **{name: row.get(name) for name in keep if name in row}}
            for row in self.rows
        ]
        return Series(
            rows=rows,
            units={name: self.units[name] for name in keep if name in self.units},
            fields=keep,
            time_init=self.time_init,
            calls=self.calls,
            cached=self.cached,
            dropped=[*self.dropped, *missing],
            dataset_rows=self.dataset_rows,
            fcst_days=self.fcst_days,
            coverage=self.coverage,
            retried=self.retried,
        )

    def window(self, hours: int) -> "Series":
        """按调用方请求的时段裁剪行；缓存本身仍保留整天数据供同日复用。"""
        try:
            n = max(1, min(int(hours), MAX_HOURS))
        except (TypeError, ValueError):
            n = MAX_HOURS
        if len(self.rows) <= n:
            return self
        trimmed = copy.copy(self)
        trimmed.rows = self.rows[:n]
        return trimmed

    def meta(self, client: "TianjiClient") -> dict:
        body = {
            "rows": len(self.rows),
            "fields": self.fields,
            "units": self.units,
            "time_init": self.time_init,
            "calls_used": self.calls,
            "calls_total": client.calls,
            "cache_hit": self.cached,
            "dropped_fields": self.dropped,
            "fcst_days": self.fcst_days,
        }
        if self.coverage:
            body["coverage"] = self.coverage.describe()
        return body


def parse_location(location: str) -> tuple[float, float]:
    """接受 'lon,lat'（经度在前，与 API 一致）。"""
    if not isinstance(location, str) or "," not in location:
        raise TianjiError(f"location 必须是 '经度,纬度' 字符串，例如 '112.55,37.87'，收到 {location!r}")
    lon_s, lat_s = (part.strip() for part in location.split(",", 1))
    try:
        lon, lat = float(lon_s), float(lat_s)
    except ValueError as exc:
        raise TianjiError(f"location 经纬度必须都是数字，收到 {location!r}") from exc
    if not -180.0 <= lon <= 180.0:
        raise TianjiError(f"经度超出范围 [-180,180]：{lon}")
    if not -90.0 <= lat <= 90.0:
        raise TianjiError(f"纬度超出范围 [-90,90]：{lat}")
    return lon, lat


def normalize_t_res(value: str) -> str:
    """只允许 1h / 15min，其余在发请求前就拦掉（免得白烧额度）。"""
    text = str(value or "1h").strip().lower()
    if text not in T_RES_ALLOWED:
        raise TianjiError(
            f"t_res 只能是 {' 或 '.join(T_RES_ALLOWED)}；服务端仅提供 15min/1h 分辨率"
            f"（3h 实测返回 20010）。收到 {value!r}"
        )
    return text


def _bucket_hours(hours: int) -> int:
    """把取数窗口向上取整到整天，让同一天的后续提问都能命中缓存。"""
    return max(24, -(-hours // 24) * 24)


def _row_time(row: dict) -> datetime | None:
    stamp = row.get("time")
    if not stamp:
        return None
    try:
        return datetime.fromisoformat(str(stamp))
    except ValueError:
        return None


class TianjiClient:
    def __init__(
        self,
        settings: Settings | None = None,
        denied_store: Path | None = None,
        usage_store: Path | None = None,
        now_fn=None,
    ):
        self.settings = settings or load_settings()
        self._now = now_fn or now_cst
        self.calls = 0
        self._cache: dict[tuple[str, str], tuple[float, Series]] = {}
        self.denied_store = Path(denied_store) if denied_store else DENIED_STORE
        self.usage_store = Path(usage_store) if usage_store else USAGE_STORE
        self.dynamic_denied: dict[str, str] = self._load_denied()

    # ---------- 额度与黑名单 ----------

    def _load_denied(self) -> dict[str, str]:
        if self.denied_store.is_file():
            try:
                return json.loads(self.denied_store.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                return {}
        return {}

    def _save_denied(self) -> None:
        try:
            self.denied_store.parent.mkdir(parents=True, exist_ok=True)
            self.denied_store.write_text(
                json.dumps(self.dynamic_denied, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError:
            pass  # 落盘失败不影响主流程

    # ---------- 额度台账 ----------

    def _log_usage(self, loc: str, days: int, t_res: str, outcome: str) -> None:
        """每次真实请求（含失败尝试）追加一行 JSONL；落盘失败不影响主流程。"""
        try:
            self.usage_store.parent.mkdir(parents=True, exist_ok=True)
            entry = {
                "ts": iso(self._now()),
                "loc": loc,
                "days": int(days),
                "t_res": t_res,
                "outcome": outcome,
            }
            with self.usage_store.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def usage_summary(self) -> dict:
        """本地台账口径：累计与今日的真实请求次数（含失败尝试）。"""
        total = today = today_ok = 0
        today_key = iso(self._now())[:10]
        if self.usage_store.is_file():
            try:
                for line in self.usage_store.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    total += 1
                    if str(entry.get("ts", "")).startswith(today_key):
                        today += 1
                        if entry.get("outcome") == "ok":
                            today_ok += 1
            except OSError:
                pass
        return {
            "file": str(self.usage_store),
            "total_calls": total,
            "today_calls": today,
            "today_ok": today_ok,
            "note": "本地台账：统计真实发出的请求（含失败尝试），与 API 侧计费口径可能略有差异",
        }

    def _spend(self) -> None:
        if self.calls >= self.settings.max_calls:
            raise BudgetExhausted(
                f"已达进程调用预算上限 {self.settings.max_calls} 次（TJW_MAX_CALLS 可调）。"
                "额度有限，请缩小范围或复用已有结果。"
            )
        self.calls += 1

    # ---------- 单次请求 ----------

    def _request(self, params: dict[str, object]) -> dict:
        query = urllib.parse.urlencode({k: v for k, v in params.items() if v != ""})
        url = f"{self.settings.base_url}?{query}"
        request = urllib.request.Request(url, headers={"Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=self.settings.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            try:
                payload = json.loads(body)
            except json.JSONDecodeError:
                raise TianjiError(f"HTTP {exc.code}：{body[:200]}") from exc
        except urllib.error.URLError as exc:
            raise TianjiError(f"网络不可达：{exc.reason}") from exc

        code = payload.get("code")
        message = str(payload.get("message") or "")
        if code == 200:
            return payload
        if code == 20003:
            raise FieldDenied(_extract_field_names(message), message)
        if code == 20017:
            raise RegionDenied(f"区域未授权：{message}")
        if code == 10005:
            raise QuotaExhausted(f"订阅额度已用尽：{message}")
        if code in (10012, 10013):
            raise AuthError(f"鉴权失败（{code}）：{message}")
        if code == 20001:
            raise AuthError(f"订阅参数缺失（{code}）：{message}")
        if code in (20009, 10006):
            raise FetchLimitExceeded(f"API 错误 code={code}：{message}")
        raise TianjiError(f"API 错误 code={code}：{message}")

    # ---------- 一次取齐（按天） ----------

    def _fetch_days(self, location: str, days: int, t_res: str) -> Series:
        """取回从「起报 +1h」开始、共 `days*24` 小时的全部已授权要素。"""
        lon, lat = parse_location(location)
        days = max(1, min(int(days), self.settings.max_fetch_days))
        want_rows = days * 24 * (4 if t_res == "15min" else 1)

        wanted = [code for code in elements.ALL_FIELDS if code not in self.dynamic_denied]
        if not wanted:
            raise TianjiError("所有要素都在运行时黑名单里，无可用要素")

        merged: dict[str, dict] = {}
        units: dict[str, str] = {}
        time_init: str | None = None
        used: list[str] = []
        dropped: list[str] = []

        def fetch(attempt: list[str]) -> None:
            nonlocal time_init
            if not attempt:
                return
            params = {
                "fields": ",".join(attempt),
                "loc": f"{lon},{lat}",
                "fcst_days": days,
                "fcst_hours": 0,
                "t_res": t_res,
                "tz": self.settings.tz,
                "key": self.settings.api_key,
                "subscriptionId": self.settings.subscription_id,
            }
            self._spend()
            try:
                payload = self._request(params)
            except FieldDenied as exc:
                self._log_usage(f"{lon},{lat}", days, t_res, "denied_fields")
                removed = [name for name in exc.fields if name in attempt]
                rest = [name for name in attempt if name not in exc.fields]
                if not removed or not rest:
                    raise
                dropped.extend(removed)
                for name in removed:
                    self.dynamic_denied[name] = str(exc)
                self._save_denied()
                fetch(rest)
                return
            except TianjiError as exc:
                self._log_usage(f"{lon},{lat}", days, t_res, f"failed:{type(exc).__name__}")
                # 要素数量超限（20011）：二分重试，调用方无感
                if "20011" in str(exc) and len(attempt) > 1:
                    mid = len(attempt) // 2
                    fetch(attempt[:mid])
                    fetch(attempt[mid:])
                    return
                raise
            self._log_usage(f"{lon},{lat}", days, t_res, "ok")

            data = payload.get("data") or {}
            time_init = time_init or data.get("time_init")
            units.update(data.get("units") or {})
            for row in data.get("data") or []:
                stamp = row.get("time")
                if stamp is None:
                    continue
                merged.setdefault(stamp, {"time": stamp}).update(
                    {k: v for k, v in row.items() if k != "time"}
                )
            used.extend(attempt)

        fetch(wanted)

        rows = [merged[stamp] for stamp in sorted(merged)][:want_rows]
        return Series(
            rows=rows,
            units={k: units[k] for k in units if k in used},
            fields=used,
            time_init=time_init,
            calls=self.calls,
            dropped=dropped,
            dataset_rows=len(rows),
            fcst_days=days,
        )

    # ---------- 窗口取数 ----------

    def series_for_window(self, location: str, window: Window, t_res: str = "1h") -> Series:
        """按绝对窗口取数：多要余量 → 校验覆盖 → 裁到窗口内。"""
        lon, lat = parse_location(location)
        t_res = normalize_t_res(t_res)
        resolution = _RESOLUTION[t_res]
        key = (f"{lon:.4f},{lat:.4f}", t_res)

        now = self._now()
        init_est = expected_run_init(now, self.settings.run_lag_hours)
        needed_hours = int(math.ceil((window.end - init_est).total_seconds() / 3600)) + self.settings.pad_hours
        days = max(1, math.ceil(needed_hours / 24))
        days = min(days, self.settings.max_fetch_days)

        raw = None
        hit = self._cache.get(key)
        if hit and time.time() - hit[0] < CACHE_TTL_SECONDS:
            cached = copy.copy(hit[1])
            cached.cached = True
            cached.calls = self.calls
            if _covers(cached, window, resolution):
                raw = cached
            elif cached.fcst_days < self.settings.max_fetch_days:
                # 缓存不够长：按缓存天数往上加一天，而不是重复请求同样长度
                days = min(self.settings.max_fetch_days, max(days, cached.fcst_days + 1))
        if raw is None:
            raw = self._fetch_with_fallback(location, days, t_res)
            self._cache[key] = (time.time(), raw)

        raw = self._ensure_tail(location, raw, days, t_res, window, resolution)
        return self._trim(raw, window, resolution)

    def _fetch_with_fallback(self, location: str, days: int, t_res: str) -> Series:
        """订阅限制（20009/10006）时逐步降天数，尽量取到能取的长度。"""
        attempt = max(1, int(days))
        last_error: Exception | None = None
        while attempt >= 1:
            try:
                return self._fetch_days(location, attempt, t_res)
            except FetchLimitExceeded as exc:
                last_error = exc
                limit = _limit_days_from_error(str(exc))
                attempt = limit if (limit and limit < attempt) else attempt - 1
        if last_error:
            raise last_error
        raise TianjiError("取数失败：没有可用的天数")

    def _ensure_tail(
        self,
        location: str,
        raw: Series,
        days: int,
        t_res: str,
        window: Window,
        resolution: timedelta,
    ) -> Series:
        """尾部不够（发布延迟/估算偏乐观）时补取一次，最多加一天。"""
        if raw.cached:
            return raw
        if _covers(raw, window, resolution):
            return raw
        coverage = _coverage(raw, window, resolution)
        if coverage.missing_tail_hours <= 0:
            return raw
        if days >= self.settings.max_fetch_days or self.calls >= self.settings.max_calls:
            return raw
        base_days = raw.fcst_days or days
        bigger = self._fetch_with_fallback(location, min(base_days + 1, self.settings.max_fetch_days), t_res)
        bigger.retried = True
        lon, lat = parse_location(location)
        self._cache[(f"{lon:.4f},{lat:.4f}", t_res)] = (time.time(), bigger)
        return bigger

    def _trim(self, raw: Series, window: Window, resolution: timedelta) -> Series:
        rows = []
        for row in raw.rows:
            moment = _row_time(row)
            if moment is None:
                continue
            if window.start <= moment < window.end:
                rows.append(row)
        trimmed = copy.copy(raw)
        trimmed.rows = rows
        trimmed.coverage = _coverage(raw, window, resolution)
        return trimmed

    # ---------- 兼容接口 ----------

    def dataset(
        self,
        location: str,
        hours: int = 24,
        start_hour: int = 0,
        t_res: str = "1h",
    ) -> Series:
        """（兼容旧接口）从此刻起 `hours` 小时的完整要素集。"""
        return self.series_for_window(location, rolling_window(hours, self._now()), t_res)

    def query(
        self,
        location: str,
        fields: list[str],
        hours: int = 24,
        start_hour: int = 0,
        t_res: str = "1h",
    ) -> Series:
        """（兼容旧接口）按需投影：先按窗口取数，再裁列。"""
        series = self.series_for_window(location, rolling_window(hours, self._now()), t_res)
        return series.project(fields)

    def capability(self) -> dict:
        return {
            "authorized": elements.table(),
            "denied_known": elements.DENIED,
            "denied_runtime": self.dynamic_denied,
            "limits": {
                "max_fields_per_request": elements.MAX_FIELDS_PER_REQUEST,
                "max_calls_per_process": self.settings.max_calls,
                "calls_used": self.calls,
                "hours_max": MAX_HOURS,
                "max_fetch_days": self.settings.max_fetch_days,
                "t_res_allowed": list(T_RES_ALLOWED),
                "cache_ttl_seconds": CACHE_TTL_SECONDS,
            },
            "run_anchor": {
                "run_hours_cst": [8, 20],
                "switch_at_cst": ["05:00", "17:00"],
                "assumed_lag_hours": self.settings.run_lag_hours,
                "pad_hours": self.settings.pad_hours,
                "now": iso(now_cst()),
                "expected_run_init": iso(expected_run_init(now_cst(), self.settings.run_lag_hours)),
                "note": (
                    "接口只返回最新一次起报，没有选择起报时次的参数；数据锚在 time_init+1h。"
                    "08 时起报约 17:00 可用、20 时起报约次日 05:00 可用（切换点 05:00 / 17:00）。"
                    "客户端按「预期起报 + 余量」取数，再用响应里的 time_init 校正覆盖情况。"
                ),
            },
            "strategy": (
                "一次请求取齐全部已授权要素并缓存；风电/光伏/理论出力/原始取数都从同一份"
                "缓存按需投影，同一坐标与窗口的后续提问不再产生请求。"
            ),
            "usage": self.usage_summary(),
            "endpoint": self.settings.masked(),
        }


def _covers(series: Series, window: Window, resolution: timedelta) -> bool:
    if not series.rows:
        return False
    first = _row_time(series.rows[0])
    last = _row_time(series.rows[-1])
    if first is None or last is None:
        return False
    return first <= window.start and last + resolution >= window.end


def _coverage(series: Series, window: Window, resolution: timedelta) -> Coverage:
    first = _row_time(series.rows[0]) if series.rows else None
    last = _row_time(series.rows[-1]) if series.rows else None
    head = tail = 0
    if first is not None and first > window.start:
        head = int(math.ceil((first - window.start).total_seconds() / 3600))
    if last is not None:
        data_end = last + resolution
        if data_end < window.end:
            tail = int(math.ceil((window.end - data_end).total_seconds() / 3600))
    else:
        tail = window.hours
        head = window.hours
    return Coverage(
        requested_from=iso(window.start),
        requested_to=iso(window.end),
        run_time_init=series.time_init,
        data_from=iso(first) if first else None,
        data_to=iso(last) if last else None,
        complete=(head == 0 and tail == 0),
        missing_head_hours=head,
        missing_tail_hours=tail,
        fcst_days=series.fcst_days,
        retried=series.retried,
    )


def _limit_days_from_error(message: str) -> int | None:
    match = re.search(r"最多查询\s*(\d+)\s*天", message)
    if match:
        return int(match.group(1))
    return None


def _extract_field_names(message: str) -> list[str]:
    """从 '... 未授权要素: a, b' 里抠出要素名。"""
    for marker in ("未授权要素:", "未授权要素：", "unauthorized element:"):
        if marker in message:
            tail = message.split(marker, 1)[1]
            return [part.strip() for part in tail.replace("，", ",").split(",") if part.strip()]
    return []

"""天机 /v2 客户端（自包含，仅用标准库）。

设计要点（v0.2 重构）
- **一次请求拿齐**：首次取数就把全部已授权要素拼成一个请求发出去，之后所有工具
  （风电/光伏/理论出力/原始取数）都从同一份缓存里**按需投影**，不再按场景拆请求。
  同一坐标 + 同一窗口 + 同一分辨率，无论问几个问题，都只有一次网络请求。
- 缓存键不含要素集合，只含 (坐标, 起始小时, 取数窗口, 分辨率)。
- 要素必须来自注册表：20003 是整包拒绝，一个坏要素会让整次请求失败，故失败即剔除重试。
- 万一要素数超过服务端上限（20011），自动二分重试，不浪费调用方重试。
- 进程级调用预算 + 内存缓存（默认 10 分钟），额度有限时默认行为是**少发请求**。
"""

from __future__ import annotations

import copy
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from . import elements
from .config import PROJECT_ROOT, Settings, load_settings

CACHE_TTL_SECONDS = 600.0
DENIED_STORE = PROJECT_ROOT / ".runtime" / "denied.json"

MAX_HOURS = 240
MAX_START_HOUR = 72
# 实测：3h 会被服务端拒绝（20010，仅提供 15min/1h）
T_RES_ALLOWED = ("1h", "15min")


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


@dataclass
class Series:
    rows: list[dict] = field(default_factory=list)
    units: dict[str, str] = field(default_factory=dict)
    fields: list[str] = field(default_factory=list)
    time_init: str | None = None
    calls: int = 0
    cached: bool = False
    dropped: list[str] = field(default_factory=list)
    # 缓存里的完整行数（按整天向上取整的窗口），用于区分"本次窗口行数"与"完整数据集行数"
    dataset_rows: int = 0

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
        return {
            "rows": len(self.rows),
            "fields": self.fields,
            "units": self.units,
            "time_init": self.time_init,
            "calls_used": self.calls,
            "calls_total": client.calls,
            "cache_hit": self.cached,
            "dropped_fields": self.dropped,
        }


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


class TianjiClient:
    def __init__(self, settings: Settings | None = None, denied_store: Path | None = None):
        self.settings = settings or load_settings()
        self.calls = 0
        self._cache: dict[tuple, tuple[float, Series]] = {}
        self.denied_store = Path(denied_store) if denied_store else DENIED_STORE
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
        raise TianjiError(f"API 错误 code={code}：{message}")

    # ---------- 一次取齐 ----------

    def dataset(
        self,
        location: str,
        hours: int = 24,
        start_hour: int = 0,
        t_res: str = "1h",
    ) -> Series:
        """取回该窗口内**全部**已授权要素；命中缓存时零请求。"""
        lon, lat = parse_location(location)
        t_res = normalize_t_res(t_res)
        hours = max(1, min(int(hours), MAX_HOURS))
        start_hour = max(0, min(int(start_hour), MAX_START_HOUR))
        bucket = _bucket_hours(hours)
        key = (f"{lon:.4f},{lat:.4f}", start_hour, bucket, t_res)

        hit = self._cache.get(key)
        if hit and time.time() - hit[0] < CACHE_TTL_SECONDS:
            cached = copy.copy(hit[1])
            cached.cached = True
            cached.calls = self.calls
            return cached

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
                "fcst_days": bucket // 24,
                "fcst_hours": start_hour,
                "t_res": t_res,
                "tz": self.settings.tz,
                "key": self.settings.api_key,
                "subscriptionId": self.settings.subscription_id,
            }
            self._spend()
            try:
                payload = self._request(params)
            except FieldDenied as exc:
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
                # 要素数量超限（20011）：二分重试，调用方无感
                if "20011" in str(exc) and len(attempt) > 1:
                    mid = len(attempt) // 2
                    fetch(attempt[:mid])
                    fetch(attempt[mid:])
                    return
                raise

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

        rows = [merged[stamp] for stamp in sorted(merged)][:bucket]
        series = Series(
            rows=rows,
            units={k: units[k] for k in units if k in used},
            fields=used,
            time_init=time_init,
            calls=self.calls,
            dropped=dropped,
            dataset_rows=len(rows),
        )
        self._cache[key] = (time.time(), series)
        result = copy.copy(series)
        result.calls = self.calls
        return result

    def query(
        self,
        location: str,
        fields: list[str],
        hours: int = 24,
        start_hour: int = 0,
        t_res: str = "1h",
    ) -> Series:
        """按需投影：先拿完整数据集（可能 0 次请求），再裁列、再按请求窗口裁行。"""
        series = self.dataset(location, hours=hours, start_hour=start_hour, t_res=t_res)
        return series.project(fields).window(hours)

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
                "start_hour_max": MAX_START_HOUR,
                "t_res_allowed": list(T_RES_ALLOWED),
                "cache_ttl_seconds": CACHE_TTL_SECONDS,
            },
            "strategy": (
                "一次请求取齐全部已授权要素并缓存；风电/光伏/理论出力/原始取数都从同一份"
                "缓存按需投影，同一坐标与窗口的后续提问不再产生请求。"
            ),
            "endpoint": self.settings.masked(),
        }


def _extract_field_names(message: str) -> list[str]:
    """从 '... 未授权要素: a, b' 里抠出要素名。"""
    for marker in ("未授权要素:", "未授权要素：", "unauthorized element:"):
        if marker in message:
            tail = message.split(marker, 1)[1]
            return [part.strip() for part in tail.replace("，", ",").split(",") if part.strip()]
    return []

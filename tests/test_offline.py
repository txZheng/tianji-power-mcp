"""离线测试：不联网、不消耗额度。真实响应存在 tests/fixtures/sample.json。"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tianji_power_mcp import daily, derive, elements, hints, power, pv
from tianji_power_mcp.client import (
    BudgetExhausted,
    FieldDenied,
    TianjiClient,
    TianjiError,
    normalize_t_res,
    parse_location,
)
from tianji_power_mcp.config import Settings
from tianji_power_mcp.window import (
    CST,
    Window,
    calendar_window,
    expected_run_init,
    iso,
    parse_period,
    rolling_window,
)

FIXTURE = Path(__file__).parent / "fixtures" / "sample.json"
SETTINGS = Settings(api_key="test-key", subscription_id="test-sub", max_calls=5)


def load_fixture() -> dict:
    if not FIXTURE.is_file():
        raise unittest.SkipTest("缺少 fixture：先运行 scripts/fetch_fixture.py 生成一次真实响应")
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


class RegistryTest(unittest.TestCase):
    def test_fixture_fields_are_authorized(self):
        fx = load_fixture()
        for field in fx["fields"]:
            self.assertIn(field, elements.AUTHORIZED, f"{field} 不在注册表里，说明注册表过期了")

    def test_all_fields_are_authorized(self):
        for code in elements.ALL_FIELDS:
            self.assertIn(code, elements.AUTHORIZED)
            self.assertNotIn(code, elements.DENIED, f"{code} 同时在授权表和拒绝表里")

    def test_alias_and_denied(self):
        ok, unknown, denied = elements.resolve(["temperature", "tcc", "no_such_field", "cldt", "cldt"])
        self.assertEqual(ok, ["t2m", "cldt"])  # 去重保序
        self.assertEqual(denied, ["tcc"])
        self.assertEqual(unknown, ["no_such_field"])

    def test_location_validation(self):
        self.assertEqual(parse_location("112.55,37.87"), (112.55, 37.87))
        for bad in ("112.55", "abc,def", "200,37", "112,95"):
            with self.assertRaises(TianjiError):
                parse_location(bad)

    def test_t_res_only_1h_or_15min(self):
        self.assertEqual(normalize_t_res("1h"), "1h")
        self.assertEqual(normalize_t_res("15min"), "15min")
        self.assertEqual(normalize_t_res(""), "1h")
        for bad in ("3h", "6h", "1d", "15m"):
            with self.assertRaises(TianjiError):
                normalize_t_res(bad)


class DeriveTest(unittest.TestCase):
    def test_dew_point_saturation(self):
        self.assertAlmostEqual(derive.dew_point(10.0, 100.0), 10.0, places=1)
        self.assertAlmostEqual(derive.dew_point(5.0, 97.0), 4.56, delta=0.1)
        self.assertIsNone(derive.dew_point(None, 50.0))

    def test_beaufort_boundaries(self):
        self.assertEqual(derive.beaufort_level(10.7), 5)
        self.assertEqual(derive.beaufort_level(10.8), 6)
        self.assertEqual(derive.beaufort_level(17.2), 8)
        self.assertEqual(derive.beaufort_level(30.0), 11)

    def test_shear_and_hub_wind(self):
        alpha = derive.shear_alpha(5.0, 7.0)
        self.assertTrue(0.05 <= alpha <= 0.40)
        self.assertAlmostEqual(derive.wind_at_height(5.0, 8.0, 100.0), 8.0, places=6)
        self.assertAlmostEqual(derive.wind_at_height(5.0, 8.0, 10.0), 5.0, places=6)
        self.assertGreater(derive.wind_at_height(5.0, 8.0, 140.0), 8.0)

    def test_snow_fraction(self):
        self.assertAlmostEqual(derive.snow_fraction(1.0, 4.0), 0.25, places=3)
        self.assertIsNone(derive.snow_fraction(0.0, 0.0))


class PowerTest(unittest.TestCase):
    def test_cut_in_rated_cut_out(self):
        self.assertEqual(power.pu_from_wind(2.9, 3.0, 12.0, 25.0), 0.0)
        self.assertEqual(power.pu_from_wind(12.0, 3.0, 12.0, 25.0), 1.0)
        self.assertEqual(power.pu_from_wind(25.0, 3.0, 12.0, 25.0), 0.0)
        mid = power.pu_from_wind(8.0, 3.0, 12.0, 25.0)
        self.assertTrue(0.0 < mid < 1.0)

    def test_monotonic_below_rated(self):
        values = [power.pu_from_wind(w, 3.0, 12.0, 25.0) for w in range(4, 13)]
        self.assertEqual(values, sorted(values))

    def test_custom_curve(self):
        curve = [[0, 0], [10, 0.5], [20, 1.0]]
        self.assertAlmostEqual(power.pu_from_curve(10, curve), 0.5, places=6)
        self.assertAlmostEqual(power.pu_from_curve(15, curve), 0.75, places=6)

    def test_param_validation(self):
        power.check_params(3.0, 12.0, 25.0, 100.0, 5.0)
        with self.assertRaises(ValueError):
            power.check_params(12.0, 3.0, 25.0, 100.0, 5.0)


class PvTest(unittest.TestCase):
    def test_night_is_zero(self):
        self.assertEqual(pv.pu_from_ghi(0.0, 5.0), 0.0)
        self.assertEqual(pv.pu_from_ghi(None, 5.0), 0.0)
        self.assertEqual(pv.pu_from_ghi(0.5, None), 0.0)

    def test_reference_point(self):
        # 1000 W/m²、气温 25℃：组件约 56.3℃，温度修正后约 0.717 标幺
        self.assertAlmostEqual(pv.pu_from_ghi(1000.0, 25.0), 0.7175, places=3)

    def test_linear_in_irradiance_below_clip(self):
        low = pv.pu_from_ghi(200.0, 0.0, tilt_gain=1.0)
        high = pv.pu_from_ghi(400.0, 0.0, tilt_gain=1.0)
        self.assertAlmostEqual(high / low, 2.0, places=1)

    def test_colder_is_better(self):
        self.assertGreater(pv.pu_from_ghi(800.0, -5.0), pv.pu_from_ghi(800.0, 30.0))

    def test_clipped_at_one(self):
        self.assertLessEqual(pv.pu_from_ghi(2000.0, 0.0, tilt_gain=1.5), 1.0)

    def test_simulate_summary(self):
        rows = [
            {"time": "2026-11-19T00:00", "ssrd": 0.0, "t2m": -5.0},
            {"time": "2026-11-19T12:00", "ssrd": 500.0, "t2m": 0.0},
            {"time": "2026-11-19T13:00", "ssrd": 450.0, "t2m": 1.0},
        ]
        out = pv.simulate(rows, capacity_mw=10.0)
        self.assertEqual(out["summary"]["hours"], 3)
        self.assertEqual(out["summary"]["daylight_hours"], 2)
        self.assertAlmostEqual(out["summary"]["energy_mwh"], sum(d["mw"] for d in out["series"]), places=2)
        self.assertTrue(out["disclaimer"])

    def test_param_validation(self):
        pv.check_params(5.0, 0.82, -0.004, 1.0)
        for bad in ((5.0, 0.1, -0.004, 1.0), (5.0, 0.82, -0.05, 1.0), (0.0, 0.82, -0.004, 1.0)):
            with self.assertRaises(ValueError):
                pv.check_params(*bad)


class HintTest(unittest.TestCase):
    def _rows(self, **kw):
        base = {
            "time": "2026-11-19T08:00+08:00",
            "t2m": 2.0,
            "rh2m": 60.0,
            "ws10m": 3.0,
            "ws100m": 5.0,
            "tp": 0.0,
            "pres": 0.0,
            "cldt": 0.1,
            "ssrd": 0.0,
        }
        base.update(kw)
        return [dict(base, time=f"2026-11-19T08:{i:02d}+08:00") for i in range(3)]

    def test_icing_detected(self):
        result = hints.detect(self._rows(t2m=-2.0, rh2m=96.0, pres=1.2), scenario="wind")
        hazards = {e["hazard"]: e for e in result["events"]}
        self.assertIn("icing", hazards)
        self.assertEqual(hazards["icing"]["level"], "high")
        self.assertEqual(hazards["icing"]["hours"], 3)

    def test_high_wind_extreme(self):
        result = hints.detect(self._rows(ws10m=25.0), scenario="general")
        self.assertEqual(result["peak_level"], "extreme")

    def test_fog_proxy_night(self):
        result = hints.detect(self._rows(t2m=5.0, rh2m=97.0, ws10m=1.5, cldt=0.1), scenario="grid")
        hazards = {e["hazard"]: e for e in result["events"]}
        self.assertIn("fog_proxy", hazards)
        self.assertEqual(hazards["fog_proxy"]["level"], "high")

    def test_pv_cloud_only_in_daytime(self):
        night = hints.detect(self._rows(cldt=0.95, ssrd=0.0), scenario="pv")
        self.assertNotIn("pv_cloud", {e["hazard"] for e in night["events"]})
        day = hints.detect(self._rows(cldt=0.95, ssrd=600.0), scenario="pv")
        self.assertIn("pv_cloud", {e["hazard"] for e in day["events"]})

    def test_data_gaps_always_reported(self):
        result = hints.detect(self._rows(), scenario="general")
        self.assertTrue(result["data_gaps"])


class ClientOfflineTest(unittest.TestCase):
    """核心约束：一次请求拿齐，之后各种问法都走缓存。"""

    def _client(self, payloads):
        import tempfile

        self._tmp = tempfile.mkdtemp(prefix="tianji-test-")
        fixed_now = datetime(2026, 11, 18, 10, 0, tzinfo=CST)
        client = TianjiClient(
            SETTINGS,
            denied_store=Path(self._tmp) / "denied.json",
            usage_store=Path(self._tmp) / "usage.jsonl",
            cache_store=Path(self._tmp) / "cache",
            now_fn=lambda: fixed_now,
        )
        queue = list(payloads)

        def fake_request(params):
            if not queue:
                raise AssertionError("调用次数超出预期，额度保护没生效")
            item = queue.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        client._request = fake_request  # type: ignore[method-assign]
        return client

    def _payload(self):
        """模拟服务端：无论请求哪些要素，都返回全部要素（真实行为是一次取齐）。"""
        fx = load_fixture()
        rows = []
        for row in fx["rows"]:
            full = {"time": row["time"]}
            for code in elements.ALL_FIELDS:
                full[code] = row.get(code, 0.0)
            rows.append(full)
        units = {code: fx["units"].get(code, "") for code in elements.ALL_FIELDS}
        return {"code": 200, "data": {"units": units, "data": rows, "time_init": "2026-11-18T08:00+08:00"}}

    def test_one_request_serves_many_questions(self):
        client = self._client([self._payload()])
        wind = client.query("112.55,37.87", ["ws100m", "wd100m"], hours=6)
        pv = client.query("112.55,37.87", ["ssrd", "cldt"], hours=6)
        raw = client.query("112.55,37.87", ["t2m"], hours=6)

        self.assertEqual(client.calls, 1, "同一坐标与窗口只应发一次请求")
        self.assertFalse(wind.cached)
        self.assertTrue(pv.cached and raw.cached)
        self.assertEqual(wind.fields, ["ws100m", "wd100m"])
        self.assertEqual(pv.fields, ["ssrd", "cldt"])
        self.assertEqual(raw.fields, ["t2m"])
        self.assertEqual(len(raw.rows), 6)

    def test_same_window_reuses_cache(self):
        client = self._client([self._payload()])
        client.query("112.55,37.87", ["t2m"], hours=6)
        client.query("112.55,37.87", ["t2m"], hours=6)
        self.assertEqual(client.calls, 1, "同一窗口的第二次提问应命中缓存")

    def test_projection_does_not_mutate_cache(self):
        client = self._client([self._payload()])
        first = client.query("112.55,37.87", ["t2m"], hours=4)
        second = client.query("112.55,37.87", ["ws100m"], hours=4)
        self.assertEqual(first.fields, ["t2m"])
        self.assertNotIn("ws100m", first.fields)
        self.assertEqual(second.fields, ["ws100m"])

    def test_denied_field_is_dropped_and_retried(self):
        payload = self._payload()
        client = self._client([FieldDenied(["rh2m"], "未授权要素: rh2m"), payload])
        series = client.query("112.55,37.87", ["t2m", "rh2m"], hours=3)
        self.assertNotIn("rh2m", series.fields)
        self.assertIn("rh2m", series.dropped)
        self.assertIn("rh2m", client.dynamic_denied)
        self.assertEqual(client.calls, 2)

    def test_budget_guard(self):
        client = self._client([self._payload()] * 10)
        client.calls = SETTINGS.max_calls
        with self.assertRaises(BudgetExhausted):
            client.query("112.55,37.87", ["t2m"], hours=2)

    def test_bad_t_res_fails_before_spending(self):
        client = self._client([])  # 队列为空：一旦真发请求就会 AssertionError
        with self.assertRaises(TianjiError):
            client.query("112.55,37.87", ["t2m"], hours=2, t_res="3h")
        self.assertEqual(client.calls, 0, "参数非法时不应消耗额度")


class CredentialFileTest(unittest.TestCase):
    def test_credential_file_override(self):
        import os
        import tempfile

        from tianji_power_mcp.config import load_settings

        path = Path(tempfile.mkdtemp(prefix="tianji-cred-")) / ".tjweather-rc"
        path.write_text("TJW_API_KEY=test-key-x\nTJW_SUBSCRIPTION_ID=test-sub-x\n", encoding="utf-8")
        watched = ("TJW_CREDENTIAL_FILE", "TJW_API_KEY", "TJW_SUBSCRIPTION_ID", "real_sub_id")
        saved = {k: os.environ.get(k) for k in watched}
        try:
            for k in watched:
                os.environ.pop(k, None)
            os.environ["TJW_CREDENTIAL_FILE"] = str(path)
            settings = load_settings()
            self.assertEqual(settings.api_key, "test-key-x")
            self.assertEqual(settings.subscription_id, "test-sub-x")
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


class ServerRegistrationTest(unittest.TestCase):
    def test_tools_registered(self):
        import asyncio

        from tianji_power_mcp import server as srv

        tools = asyncio.run(srv.server.list_tools())
        names = {tool.name for tool in tools}
        self.assertLessEqual(
            {
                "get_capability",
                "power_weather_hint",
                "wind_solar_snapshot",
                "theoretical_power",
                "daily_summary",
                "get_dataset",
            },
            names,
        )
        self.assertNotIn("get_raw_weather", names, "取数工具已更名为 get_dataset")



class FakeClient:
    """server 层用的假客户端：按绝对窗口生成行，不发网络请求。"""

    def __init__(self, capacity: int = 48):
        self.calls = 0
        self.seen: list[dict] = []
        self.capacity = capacity

    @staticmethod
    def _row(moment):
        row = {"time": iso(moment)}
        for code in elements.ALL_FIELDS:
            row[code] = 1.0
        return row

    def series_for_window(self, location, window, t_res="1h"):
        from tianji_power_mcp.client import Coverage, Series

        start = window.start.replace(minute=0, second=0, microsecond=0)
        hours = max(0, int((window.end - start).total_seconds() // 3600))
        rows = [self._row(start + timedelta(hours=i)) for i in range(hours)]
        coverage = Coverage(
            requested_from=iso(window.start),
            requested_to=iso(window.end),
            run_time_init="2026-11-18T20:00+08:00",
            data_from=rows[0]["time"] if rows else None,
            data_to=rows[-1]["time"] if rows else None,
            complete=True,
            fcst_days=2,
        )
        self.seen.append({"window": window.describe(), "t_res": t_res})
        return Series(
            rows=rows,
            units={code: "u" for code in elements.ALL_FIELDS},
            fields=list(elements.ALL_FIELDS),
            time_init="2026-11-18T20:00+08:00",
            dataset_rows=self.capacity,
            fcst_days=2,
            coverage=coverage,
        )


class ToolWindowTest(unittest.TestCase):
    """所有工具都按解析出的绝对窗口取数。"""

    def setUp(self):
        from tianji_power_mcp import server as srv

        self.srv = srv
        self.fake = FakeClient(capacity=48)
        self._original = srv._client
        srv._client = lambda: self.fake  # type: ignore[assignment]

    def tearDown(self):
        self.srv._client = self._original  # type: ignore[assignment]

    def test_hint_uses_requested_hours(self):
        result = self.srv.power_weather_hint("112.55,37.87", hours=6)
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["window"]["hours"], 6)
        self.assertEqual(len(self.fake.seen), 1)

    def test_snapshot_uses_requested_hours(self):
        rows = self.srv.wind_solar_snapshot("112.55,37.87", hours=6)["data"]["rows"]
        self.assertEqual(len(rows), 6)

    def test_theoretical_power_uses_requested_hours(self):
        result = self.srv.theoretical_power("112.55,37.87", hours=6, kind="wind")
        self.assertEqual(result["data"]["summary"]["hours"], 6)

    def test_period_tomorrow_is_a_calendar_window(self):
        data = self.srv.get_dataset("112.55,37.87", period="明天")["data"]
        self.assertEqual(data["window"]["kind"], "calendar")
        self.assertEqual(data["window"]["hours"], 24)
        self.assertTrue(data["window"]["from"].endswith("T00:00+08:00"))
        self.assertEqual(data["window_rows"], 24)

    def test_period_range(self):
        result = self.srv.power_weather_hint("112.55,37.87", period="2026-12-01~2026-12-03")
        self.assertEqual(result["data"]["window"]["hours"], 72)

    def test_unknown_period_reports_error(self):
        result = self.srv.get_dataset("112.55,37.87", period="随便说说")
        self.assertFalse(result["ok"])
        self.assertIn("无法识别的时段", result["error"])

    def test_empty_window_is_an_error_not_an_empty_result(self):
        from tianji_power_mcp.client import Coverage, Series

        empty = Series(
            rows=[],
            fields=list(elements.ALL_FIELDS),
            coverage=Coverage(
                requested_from="2026-11-18T00:00+08:00",
                requested_to="2026-11-19T00:00+08:00",
                complete=False,
                missing_head_hours=24,
            ),
        )
        self.fake.series_for_window = lambda *a, **k: empty  # type: ignore[assignment]
        result = self.srv.get_dataset("112.55,37.87", period="昨天")
        self.assertFalse(result["ok"])
        self.assertIn("没有可取到的数据", result["error"])


class DatasetSemanticsTest(unittest.TestCase):
    """total_rows 是这次取回的完整行数，window_rows 才是窗口行数。"""

    def setUp(self):
        from tianji_power_mcp import server as srv

        self.srv = srv
        self.fake = FakeClient(capacity=48)
        self._original = srv._client
        srv._client = lambda: self.fake  # type: ignore[assignment]

    def tearDown(self):
        self.srv._client = self._original  # type: ignore[assignment]

    def test_separates_window_from_cache(self):
        data = self.srv.get_dataset("112.55,37.87", hours=10)["data"]
        self.assertEqual(data["window_rows"], 10)
        self.assertEqual(data["total_rows"], 48)
        self.assertFalse(data["truncated"])

    def test_limit_only_truncates_return(self):
        data = self.srv.get_dataset("112.55,37.87", hours=10, limit=3)["data"]
        self.assertEqual(data["returned_rows"], 3)
        self.assertEqual(data["window_rows"], 10)
        self.assertTrue(data["truncated"])
        self.assertIn("limit", data["note"])


class WindowParseTest(unittest.TestCase):
    NOW = datetime(2026, 11, 19, 14, 56, tzinfo=CST)

    def test_rolling_hours(self):
        window = parse_period("未来24小时", self.NOW)
        self.assertEqual((window.start, window.end), (self.NOW, self.NOW + timedelta(hours=24)))

    def test_future_days_covers_full_calendar_days(self):
        window = parse_period("未来3天", self.NOW)
        self.assertEqual(window.kind, "future_days")
        self.assertEqual(iso(window.start), "2026-11-19T14:56+08:00")
        self.assertEqual(iso(window.end), "2026-11-23T00:00+08:00", "要完整覆盖 20/21/22 三天")

    def test_future_days_gap_hours_are_included(self):
        window = parse_period("未来3天", self.NOW)
        self.assertGreater(window.hours, 72, "今晚剩余的小时算搭头")

    def test_future_hours_stays_rolling(self):
        window = parse_period("未来24小时", self.NOW)
        self.assertEqual(window.kind, "rolling")
        self.assertEqual(window.hours, 24)

    def test_future_week_is_seven_full_days(self):
        window = parse_period("未来一周", self.NOW)
        self.assertEqual(iso(window.end), "2026-11-27T00:00+08:00")

    def test_today_and_tomorrow(self):
        self.assertEqual(iso(parse_period("今天", self.NOW).start), "2026-11-19T00:00+08:00")
        tomorrow = parse_period("明天", self.NOW)
        self.assertEqual(iso(tomorrow.start), "2026-11-20T00:00+08:00")
        self.assertEqual(tomorrow.hours, 24)

    def test_day_part(self):
        window = parse_period("明天下午", self.NOW)
        self.assertEqual((window.start.hour, window.end.hour), (12, 18))

    def test_explicit_dates(self):
        window = parse_period("2026-12-01~2026-12-03", self.NOW)
        self.assertEqual(window.hours, 72)
        self.assertEqual(iso(window.end), "2026-12-04T00:00+08:00")

    def test_unknown_returns_none(self):
        self.assertIsNone(parse_period("随便说说", self.NOW))

    def test_reversed_range_rejected(self):
        with self.assertRaises(ValueError):
            parse_period("2026-12-05~2026-12-01", self.NOW)

    def test_run_init_switches_at_05_and_17(self):
        def init(hour, minute=0):
            return expected_run_init(datetime(2026, 11, 19, hour, minute, tzinfo=CST), 9)

        self.assertEqual(iso(init(4, 59)), "2026-11-18T08:00+08:00")
        self.assertEqual(iso(init(5, 1)), "2026-11-18T20:00+08:00")
        self.assertEqual(iso(init(16, 59)), "2026-11-18T20:00+08:00")
        self.assertEqual(iso(init(17, 1)), "2026-11-19T08:00+08:00")


class StubAPI:
    """模拟天机接口：数据从 init+1h 开始，共 fcst_days*24 小时。"""

    def __init__(self, init, first_cap=None):
        self.init = init
        self.first_cap = first_cap
        self.requests: list[int] = []

    def __call__(self, params):
        days = int(params["fcst_days"])
        self.requests.append(days)
        hours = days * 24
        if self.first_cap is not None and len(self.requests) == 1:
            hours = min(hours, self.first_cap)
        fields = str(params["fields"]).split(",")
        rows = [
            {"time": iso(self.init + timedelta(hours=i + 1)), **{code: 1.0 for code in fields}}
            for i in range(hours)
        ]
        return {
            "code": 200,
            "data": {
                "time_init": iso(self.init),
                "units": {code: "u" for code in fields},
                "data": rows,
            },
        }


class AnchoredFetchTest(unittest.TestCase):
    """起报锚定 + 余量取数 + 裁剪 + 覆盖校验。"""

    NOW = datetime(2026, 11, 19, 15, 0, tzinfo=CST)

    def _client(self, api):
        tmp = Path(tempfile.mkdtemp(prefix="tianji-anchored-"))
        client = TianjiClient(
            SETTINGS,
            now_fn=lambda: self.NOW,
            denied_store=tmp / "denied.json",
            usage_store=tmp / "usage.jsonl",
            cache_store=tmp / "cache",
        )
        client._request = api  # type: ignore[method-assign]
        return client

    def test_window_is_trimmed_to_requested_range(self):
        api = StubAPI(datetime(2026, 11, 18, 20, 0, tzinfo=CST))
        client = self._client(api)
        series = client.series_for_window("112.55,37.87", rolling_window(12, self.NOW))

        self.assertEqual(len(series.rows), 12)
        self.assertEqual(series.rows[0]["time"], iso(self.NOW))
        self.assertTrue(series.coverage.complete)
        self.assertEqual(api.requests, [2], "起报比请求时刻早，必须多要一天余量")

    def test_tail_gap_triggers_one_more_fetch(self):
        api = StubAPI(datetime(2026, 11, 18, 20, 0, tzinfo=CST), first_cap=24)
        client = self._client(api)
        series = client.series_for_window("112.55,37.87", rolling_window(30, self.NOW))

        self.assertEqual(len(api.requests), 2)
        self.assertTrue(series.retried)
        self.assertTrue(series.coverage.complete)

    def test_head_gap_is_reported_not_padded(self):
        api = StubAPI(datetime(2026, 11, 19, 8, 0, tzinfo=CST))
        client = self._client(api)
        window = Window(
            datetime(2026, 11, 19, 6, 0, tzinfo=CST),
            datetime(2026, 11, 19, 12, 0, tzinfo=CST),
            "测试窗口",
        )
        series = client.series_for_window("112.55,37.87", window)

        coverage = series.coverage
        self.assertFalse(coverage.complete)
        self.assertEqual(coverage.missing_head_hours, 3)
        self.assertEqual(len(series.rows), 3)


class DailySummaryTest(unittest.TestCase):
    """逐日聚合的口径：降水求和、半天标注、理论电量逐时累计。"""

    @staticmethod
    def _rows(start: datetime, hours: int, **values):
        rows = []
        for i in range(hours):
            row = {"time": iso(start + timedelta(hours=i))}
            for code in ("t2m", "rh2m", "ws10m", "ws100m", "cldt", "tp", "ssrd"):
                row[code] = float(values.get(code, 0.0))
            rows.append(row)
        return rows

    def test_partial_days_are_labeled(self):
        rows = self._rows(datetime(2026, 11, 19, 16, 0, tzinfo=CST), 80)  # 19日16时 → 22日24时
        out = daily.daily_summary(rows, kind="general")
        self.assertEqual(len(out["days"]), 4)
        first = out["days"][0]
        self.assertEqual((first["date"], first["hours"], first["span"]), ("2026-11-19", 8, "16–24时"))
        self.assertEqual(out["days"][1]["span"], "全天")
        self.assertEqual(out["days"][1]["hours"], 24)
        self.assertEqual(out["totals"]["hours"], 80)

    def test_tp_is_summed_not_maxed(self):
        rows = self._rows(datetime(2026, 11, 20, 0, 0, tzinfo=CST), 24, tp=1.5)
        out = daily.daily_summary(rows)
        day = out["days"][0]
        self.assertEqual(day["tp_sum_mm"], 36.0, "24 小时 × 1.5 mm/hr = 36 mm，不是最大值 1.5")
        self.assertEqual(day["rain_hours"], 24)
        self.assertEqual(out["totals"]["tp_sum_mm"], 36.0)

    def test_ssrd_daylight_mean_excludes_night(self):
        rows = self._rows(datetime(2026, 11, 20, 0, 0, tzinfo=CST), 24)
        for row in rows:
            hour = datetime.fromisoformat(row["time"]).hour
            row["ssrd"] = 600.0 if 8 <= hour <= 17 else 0.0
        out = daily.daily_summary(rows)
        self.assertEqual(out["days"][0]["daylight_hours"], 10)
        self.assertEqual(out["days"][0]["ssrd"]["max"], 600)
        self.assertEqual(out["days"][0]["ssrd"]["daylight_mean"], 600)

    def test_wind_zero_hours_when_below_cut_in(self):
        rows = self._rows(datetime(2026, 11, 20, 0, 0, tzinfo=CST), 24, ws100m=2.0)
        out = daily.daily_summary(rows, kind="wind")
        wind = out["days"][0]["wind"]
        self.assertEqual(wind["zero_output_hours"], 24)
        self.assertEqual(wind["energy_mwh"], 0.0)
        self.assertEqual(out["totals"]["wind"]["energy_mwh"], 0.0)

    def test_wind_energy_at_rated(self):
        rows = self._rows(datetime(2026, 11, 20, 0, 0, tzinfo=CST), 24, ws100m=12.0)
        out = daily.daily_summary(rows, kind="wind", capacity_mw=5.0)
        self.assertEqual(out["days"][0]["wind"]["energy_mwh"], 120.0, "额定 5MW × 24h")
        self.assertAlmostEqual(out["days"][0]["wind"]["capacity_factor"], 1.0, places=3)

    def test_pv_energy_matches_hourly_simulation(self):
        rows = self._rows(datetime(2026, 11, 20, 0, 0, tzinfo=CST), 24, t2m=25.0)
        for row in rows:
            hour = datetime.fromisoformat(row["time"]).hour
            row["ssrd"] = 1000.0 if 10 <= hour <= 15 else 0.0
        out = daily.daily_summary(rows, kind="pv", capacity_mw=5.0)
        sim = pv.simulate(rows, capacity_mw=5.0)
        self.assertAlmostEqual(out["totals"]["pv"]["energy_mwh"], sim["summary"]["energy_mwh"], places=1)
        self.assertEqual(out["days"][0]["pv"]["daylight_hours"], sim["summary"]["daylight_hours"])

    def test_kind_general_has_no_power_columns(self):
        rows = self._rows(datetime(2026, 11, 20, 0, 0, tzinfo=CST), 24)
        out = daily.daily_summary(rows, kind="general")
        self.assertNotIn("wind", out["days"][0])
        self.assertNotIn("pv", out["days"][0])
        self.assertNotIn("wind", out["totals"])

    def test_missing_field_gives_none(self):
        rows = self._rows(datetime(2026, 11, 20, 0, 0, tzinfo=CST), 24)
        for row in rows:
            row.pop("rh2m")
        out = daily.daily_summary(rows)
        self.assertIsNone(out["days"][0]["rh2m"])

    def test_invalid_kind_rejected(self):
        with self.assertRaises(ValueError):
            daily.daily_summary([], kind="storm")


class DailyToolTest(unittest.TestCase):
    """server 层：daily_summary 按解析窗口取数并聚合成日表。"""

    def setUp(self):
        from tianji_power_mcp import server as srv

        self.srv = srv
        self.fake = FakeClient(capacity=48)
        self._original = srv._client
        srv._client = lambda: self.fake  # type: ignore[assignment]

    def tearDown(self):
        self.srv._client = self._original  # type: ignore[assignment]

    def test_calendar_period_is_one_full_day(self):
        result = self.srv.daily_summary("112.55,37.87", period="明天", kind="both")
        self.assertTrue(result["ok"])
        data = result["data"]
        self.assertEqual(len(data["days"]), 1)
        self.assertEqual(data["days"][0]["span"], "全天")
        self.assertEqual(data["days"][0]["hours"], 24)
        self.assertIn("wind", data["days"][0])
        self.assertIn("pv", data["days"][0])
        self.assertEqual(data["window"]["kind"], "calendar")

    def test_rolling_hours_split_into_days(self):
        result = self.srv.daily_summary("112.55,37.87", hours=30)
        self.assertTrue(result["ok"])
        days = result["data"]["days"]
        self.assertTrue(2 <= len(days) <= 3, "30 小时至少跨两个自然日")
        self.assertEqual(sum(day["hours"] for day in days), 30)
        self.assertEqual(result["data"]["totals"]["hours"], 30)

    def test_rejects_bad_kind(self):
        result = self.srv.daily_summary("112.55,37.87", hours=24, kind="storm")
        self.assertFalse(result["ok"])
        self.assertIn("kind", result["error"])

    def test_rejects_bad_power_params(self):
        result = self.srv.daily_summary("112.55,37.87", hours=24, kind="wind", cut_in=12.0)
        self.assertFalse(result["ok"])


class UsageLedgerTest(unittest.TestCase):
    """额度台账：真实请求逐次落盘，缓存命中不记账。"""

    NOW = datetime(2026, 11, 19, 15, 0, tzinfo=CST)

    def _client(self, api):
        tmp = Path(tempfile.mkdtemp(prefix="tianji-usage-"))
        client = TianjiClient(
            SETTINGS,
            now_fn=lambda: self.NOW,
            denied_store=tmp / "denied.json",
            usage_store=tmp / "usage.jsonl",
            cache_store=tmp / "cache",
        )
        client._request = api  # type: ignore[method-assign]
        return client

    def test_each_real_request_is_logged_once(self):
        client = self._client(StubAPI(datetime(2026, 11, 18, 20, 0, tzinfo=CST)))
        client.series_for_window("112.55,37.87", rolling_window(12, self.NOW))
        client.series_for_window("112.55,37.87", rolling_window(12, self.NOW))  # 命中缓存
        summary = client.usage_summary()
        self.assertEqual(summary["total_calls"], 1)
        self.assertEqual(summary["today_calls"], 1)
        self.assertEqual(summary["today_ok"], 1)

    def test_retry_fetch_is_logged_too(self):
        api = StubAPI(datetime(2026, 11, 18, 20, 0, tzinfo=CST), first_cap=24)
        client = self._client(api)
        client.series_for_window("112.55,37.87", rolling_window(30, self.NOW))  # 触发一次补取
        summary = client.usage_summary()
        self.assertEqual(summary["total_calls"], 2, "首取 + 补取各记一笔")
        self.assertEqual(summary["today_calls"], 2)

    def test_capability_reports_usage(self):
        client = self._client(StubAPI(datetime(2026, 11, 18, 20, 0, tzinfo=CST)))
        client.series_for_window("112.55,37.87", rolling_window(12, self.NOW))
        cap = client.capability()
        self.assertIn("usage", cap)
        self.assertEqual(cap["usage"]["today_calls"], 1)


class DiskCacheTest(unittest.TestCase):
    """落盘缓存：跨进程复用、起报翻页失效、窗口变长自动续取。"""

    NOW = datetime(2026, 11, 19, 15, 0, tzinfo=CST)
    INIT = datetime(2026, 11, 18, 20, 0, tzinfo=CST)

    def _client(self, api, now=None, tmp=None):
        tmp = Path(tmp or tempfile.mkdtemp(prefix="tianji-disk-"))
        client = TianjiClient(
            SETTINGS,
            now_fn=lambda: (now or self.NOW),
            denied_store=tmp / "denied.json",
            usage_store=tmp / "usage.jsonl",
            cache_store=tmp / "cache",
        )
        client._request = api  # type: ignore[method-assign]
        return client

    def test_survives_process_restart(self):
        tmp = tempfile.mkdtemp(prefix="tianji-disk-restart-")
        first = self._client(StubAPI(self.INIT), tmp=tmp)
        series_a = first.series_for_window("112.55,37.87", rolling_window(12, self.NOW))
        self.assertEqual(len(series_a.rows), 12)

        # 新进程：内存缓存为空，只剩盘上的条目
        api2 = StubAPI(self.INIT)
        second = self._client(api2, tmp=tmp)
        series_b = second.series_for_window("112.55,37.87", rolling_window(12, self.NOW))
        second.series_for_window("112.55,37.87", rolling_window(12, self.NOW))  # 再问一次走 L1
        self.assertEqual(second.calls, 0, "盘上有效条目应被复用，不再发请求")
        self.assertEqual(api2.requests, [])
        self.assertTrue(series_b.cached)
        self.assertEqual(series_b.time_init, series_a.time_init)
        self.assertEqual(len(series_b.rows), 12)

    def test_invalid_after_run_flip(self):
        tmp = tempfile.mkdtemp(prefix="tianji-disk-flip-")
        self._client(StubAPI(self.INIT), tmp=tmp).series_for_window(
            "112.55,37.87", rolling_window(12, self.NOW)
        )
        # 17:00 过后预期起报翻到 11-19 08:00，盘上的 11-18 20:00 条目必须失效
        after_flip = datetime(2026, 11, 19, 18, 0, tzinfo=CST)
        api2 = StubAPI(self.INIT)
        second = self._client(api2, now=after_flip, tmp=tmp)
        second.series_for_window("112.55,37.87", rolling_window(12, after_flip))
        self.assertEqual(second.calls, 1, "翻页后必须重新取数")
        self.assertEqual(api2.requests, [2])

    def test_longer_window_extends_fetch(self):
        tmp = tempfile.mkdtemp(prefix="tianji-disk-grow-")
        self._client(StubAPI(self.INIT), tmp=tmp).series_for_window(
            "112.55,37.87", rolling_window(12, self.NOW)
        )
        api2 = StubAPI(self.INIT)
        second = self._client(api2, tmp=tmp)
        series = second.series_for_window("112.55,37.87", rolling_window(60, self.NOW))
        self.assertEqual(api2.requests, [4], "盘上 2 天盖不住 60 小时窗口，续取天数要往上加")
        self.assertEqual(second.calls, 1)
        self.assertTrue(series.coverage.complete)

    def test_disk_cache_can_be_disabled(self):
        import dataclasses

        tmp = Path(tempfile.mkdtemp(prefix="tianji-disk-off-"))
        client = TianjiClient(
            dataclasses.replace(SETTINGS, disk_cache=False),
            now_fn=lambda: self.NOW,
            denied_store=tmp / "denied.json",
            usage_store=tmp / "usage.jsonl",
            cache_store=tmp / "cache",
        )
        client._request = StubAPI(self.INIT)
        client.series_for_window("112.55,37.87", rolling_window(12, self.NOW))
        files = list((tmp / "cache").glob("*")) if (tmp / "cache").exists() else []
        self.assertEqual(files, [], "关闭落盘缓存后不应写任何文件")

    def test_corrupt_entry_is_ignored(self):
        tmp = Path(tempfile.mkdtemp(prefix="tianji-disk-bad-"))
        cache_dir = tmp / "cache"
        cache_dir.mkdir(parents=True)
        (cache_dir / "112.5500_37.8700_1h.json").write_text("{not-json", encoding="utf-8")
        api = StubAPI(self.INIT)
        client = self._client(api, tmp=tmp)
        series = client.series_for_window("112.55,37.87", rolling_window(12, self.NOW))
        self.assertEqual(client.calls, 1, "损坏条目当作没有，正常取数")
        self.assertEqual(len(series.rows), 12)


if __name__ == "__main__":
    unittest.main()

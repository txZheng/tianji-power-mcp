"""离线测试：不联网、不消耗额度。真实响应存在 tests/fixtures/sample.json。"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from tianji_power_mcp import derive, elements, hints, power, pv
from tianji_power_mcp.client import (
    BudgetExhausted,
    FieldDenied,
    TianjiClient,
    TianjiError,
    normalize_t_res,
    parse_location,
)
from tianji_power_mcp.config import Settings

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
        client = TianjiClient(SETTINGS, denied_store=Path(self._tmp) / "denied.json")
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
        return {"code": 200, "data": {"units": units, "data": rows}}

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

    def test_larger_window_refetches_but_smaller_reuses(self):
        client = self._client([self._payload(), self._payload()])
        client.query("112.55,37.87", ["t2m"], hours=6)   # 向上取整到 24h
        client.query("112.55,37.87", ["t2m"], hours=12)  # 仍在 24h 桶内 → 复用
        self.assertEqual(client.calls, 1)
        client.query("112.55,37.87", ["t2m"], hours=48)  # 超出 → 重新取
        self.assertEqual(client.calls, 2)

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
            {"get_capability", "power_weather_hint", "wind_solar_snapshot", "theoretical_power", "get_dataset"},
            names,
        )
        self.assertNotIn("get_raw_weather", names, "取数工具已更名为 get_dataset")


class FakeClient:
    """足够让 server 层跑起来的假客户端：不发网络请求，记录调用参数。"""

    def __init__(self, rows: int = 10):
        self.calls = 0
        self.seen: list[dict] = []
        self._rows = rows

    def _series(self, fields):
        from tianji_power_mcp.client import Series

        rows = [
            {"time": f"2026-11-19T{i:02d}:00+08:00", "t2m": float(i), "ws10m": 3.0}
            for i in range(self._rows)
        ]
        return Series(
            rows=rows,
            units={code: "u" for code in fields},
            fields=list(fields),
            time_init="2026-11-19T00:00+08:00",
            calls=0,
        )

    def query(self, location, fields, hours=24, start_hour=0, t_res="1h"):
        self.seen.append({"fields": list(fields), "hours": hours, "t_res": t_res})
        return self._series(fields)

    def dataset(self, location, hours=24, start_hour=0, t_res="1h"):
        self.seen.append({"fields": list(elements.ALL_FIELDS), "hours": hours, "t_res": t_res})
        return self._series(elements.ALL_FIELDS)


class LimitSemanticsTest(unittest.TestCase):
    """limit 只截断本次返回，不影响取数与其他工具使用的完整数据。"""

    def setUp(self):
        from tianji_power_mcp import server as srv

        self.srv = srv
        self.fake = FakeClient(rows=10)
        self._original = srv._client
        srv._client = lambda: self.fake  # type: ignore[assignment]

    def tearDown(self):
        self.srv._client = self._original  # type: ignore[assignment]

    def test_limit_truncates_only_the_return(self):
        full = self.srv.get_dataset("112.55,37.87", hours=10)
        cut = self.srv.get_dataset("112.55,37.87", hours=10, limit=3)

        self.assertEqual(full["data"]["returned_rows"], 10)
        self.assertFalse(full["data"]["truncated"])
        self.assertEqual(cut["data"]["returned_rows"], 3)
        self.assertEqual(cut["data"]["total_rows"], 10)
        self.assertTrue(cut["data"]["truncated"])
        self.assertIn("已缓存", cut["data"]["note"])

    def test_limit_does_not_shrink_the_upstream_request(self):
        self.srv.get_dataset("112.55,37.87", hours=10, limit=2)
        self.assertEqual(self.fake.seen[-1]["hours"], 10, "limit 不应改变取数窗口")

    def test_other_tools_still_use_full_rows(self):
        self.srv.get_dataset("112.55,37.87", hours=10, limit=2)
        snapshot = self.srv.wind_solar_snapshot("112.55,37.87", hours=10)
        self.assertEqual(len(snapshot["data"]["rows"]), 10, "加工工具不应被 limit 影响")

    def test_dataset_defaults_to_all_fields(self):
        result = self.srv.get_dataset("112.55,37.87", hours=4)
        self.assertEqual(self.fake.seen[-1]["fields"], elements.ALL_FIELDS)
        self.assertTrue(result["ok"])


if __name__ == "__main__":
    unittest.main()

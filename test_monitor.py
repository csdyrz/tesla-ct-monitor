# -*- coding: utf-8 -*-
"""tesla_ct_monitor 单元测试:过滤规则 / 去重与重新上架 / 状态清理 / 消息排版 / 通知失败语义

运行: python test_monitor.py
"""
import json
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tesla_ct_monitor as m

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def car(vin, trim, name, code, price=81990, demo=False, odo=0, year=2026, **extra):
    base = {
        "VIN": vin,
        "TRIM": [trim] if trim else None,
        "TrimName": name,
        "TrimCode": code,
        "Model": "ct",
        "InventoryPrice": price,
        "TotalPrice": price,
        "Discount": 0,
        "IsDemo": demo,
        "Odometer": odo,
        "OdometerType": "Miles",
        "WHEELS": ["CORE_WHEELS"],
        "INTERIOR": ["WHITE"],
        "Year": year,
        "InTransit": False,
        "DeliveryDateDisplay": True,  # 真实接口里出现过布尔值,不能当日期文本
    }
    base.update(extra)
    return base


# 与线上真实数据同构的三条记录
REAL_AWD_DEMO = car(
    "7G2C258_7318ae06a4d9de9db8f4ac25ad8cf56f", "CT_AWD",
    "Cybertruck All-Wheel Drive", "$MTC03", price=76890, demo=True, odo=4600,
    TotalPrice=81990, Discount=5100,
)
REAL_AWD_NEW = car(
    "7G2C222_870100c161449dea841aea7108ee7d26", "CT_AWD",
    "Premium All-Wheel Drive", "$MTC07", price=81990,
)
REAL_BEAST = car(
    "7G2C258_6c0ec2c0861839b8fefe665ac7ff4e30", "CT_CYB",
    "Cyberbeast", "$MTC04", price=97770, demo=True, odo=3626,
)


class TestClassify(unittest.TestCase):
    def test_awd_included_despite_cyb_prefix_in_cybertruck(self):
        # 回归测试:"CYBERTRUCK" 含 "CYB" 子串,曾被误判为野兽版
        self.assertEqual(m.classify_vehicle(REAL_AWD_DEMO)[0], "include")
        self.assertEqual(m.classify_vehicle(REAL_AWD_NEW)[0], "include")

    def test_beast_excluded(self):
        self.assertEqual(m.classify_vehicle(REAL_BEAST)[0], "exclude_beast")
        fs = car("x1", "CT_CYB", "Cyberbeast Foundation Series", "$MTC04")
        self.assertEqual(m.classify_vehicle(fs)[0], "exclude_beast")
        by_name_only = car("x2", None, "Cybertruck Cyberbeast", None)
        self.assertEqual(m.classify_vehicle(by_name_only)[0], "exclude_beast")

    def test_base_excluded(self):
        lr = car("x3", "CT_LR", "Cybertruck Long Range", "$MTC05")
        self.assertEqual(m.classify_vehicle(lr)[0], "exclude_base")
        rwd = car("x4", "CT_RWD", "Cybertruck Rear-Wheel Drive", "$MTC06")
        self.assertEqual(m.classify_vehicle(rwd)[0], "exclude_base")
        rwd_name_only = car("x5", None, "Long Range Rear-Wheel Drive", None)
        self.assertEqual(m.classify_vehicle(rwd_name_only)[0], "exclude_base")

    def test_future_long_range_awd_counts_as_awd(self):
        v = car("x6", "CT_AWD_LR", "Cybertruck Long Range All-Wheel Drive", "$MTC08")
        self.assertEqual(m.classify_vehicle(v)[0], "include")

    def test_unknown_trim_fails_open_with_tag(self):
        v = car("x7", "CT_STD", "Cybertruck Standard", "$MTC09")
        self.assertEqual(m.classify_vehicle(v)[0], "include_unknown")

    def test_awd_code_with_underscore_prefix(self):
        # "CT_LR_AWD" 这类代号:\bAWD\b 在下划线处无词边界,靠 "_AWD" 字面量兜住,
        # 否则会被 CT_LR 误判成丐版
        v = car("x8", "CT_LR_AWD", "", None)
        self.assertEqual(m.classify_vehicle(v)[0], "include")


class TestBuildQuery(unittest.TestCase):
    def test_query_carries_deliverability_filter(self):
        # 回归测试:必须带官网同款可交付过滤参数,否则会混入
        # “列表可见但下单页提示 not available for your registration ZIP Code”的区域锁定车
        import urllib.parse
        url = m.build_query("92614", 0, region="CA")
        q = json.loads(urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["query"][0])
        self.assertEqual(q["version"], "v2")
        self.assertTrue(q["isFalconDeliverySelectionEnabled"])
        self.assertEqual(q["query"]["region"], "CA")
        self.assertEqual(q["query"]["zip"], "92614")
        self.assertEqual(q["query"]["range"], 0)
        self.assertEqual(q["query"]["condition"], "new")


class TestVehicleKey(unittest.TestCase):
    def test_prefers_vin_then_hash(self):
        self.assertEqual(m.vehicle_key({"VIN": "abc", "Hash": "h"}), "abc")
        self.assertEqual(m.vehicle_key({"Hash": "h"}), "h")

    def test_fallback_is_stable(self):
        v = {"TrimCode": "$MTC03", "OptionCodeList": "$A,$B", "Price": 1, "Odometer": 2}
        self.assertEqual(m.vehicle_key(v), m.vehicle_key(dict(v)))
        self.assertTrue(m.vehicle_key(v).startswith("nokey_"))


class TestDiffAndState(unittest.TestCase):
    def summaries(self):
        return [m.summarize_vehicle(v, "92614") for v in (REAL_AWD_DEMO, REAL_AWD_NEW)]

    def test_first_run_notifies_all_then_none(self):
        state = {"version": 1, "vins": {}, "consecutive_failures": 0, "last_failure_alert": None}
        to_notify, resurfaced = m.diff_against_state(self.summaries(), state, 3)
        self.assertEqual(len(to_notify), 2)
        self.assertEqual(resurfaced, set())
        m.commit_notified(state, to_notify)
        to_notify2, _ = m.diff_against_state(self.summaries(), state, 3)
        self.assertEqual(to_notify2, [])

    def test_resurface_after_gap(self):
        state = {"version": 1, "vins": {}, "consecutive_failures": 0, "last_failure_alert": None}
        m.commit_notified(state, self.summaries())
        key = self.summaries()[0]["key"]
        old = (m.now_utc() - timedelta(days=4)).isoformat()
        state["vins"][key]["last_seen"] = old
        to_notify, resurfaced = m.diff_against_state(self.summaries(), state, 3)
        self.assertEqual([c["key"] for c in to_notify], [key])
        self.assertEqual(resurfaced, {key})
        # 间隔仅 1 天 → 不算重新上架
        state["vins"][key]["last_seen"] = (m.now_utc() - timedelta(days=1)).isoformat()
        to_notify2, _ = m.diff_against_state(self.summaries(), state, 3)
        self.assertEqual(to_notify2, [])

    def test_prune_old_entries(self):
        state = {"version": 1, "vins": {
            "gone": {"last_seen": (m.now_utc() - timedelta(days=120)).isoformat()},
        }, "consecutive_failures": 0, "last_failure_alert": None}
        m.diff_against_state([], state, 3)
        self.assertNotIn("gone", state["vins"])

    def test_state_file_roundtrip_and_corruption(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "s.json"
            state = m.load_state(p)
            state["vins"]["k"] = {"last_seen": m.now_utc().isoformat()}
            m.save_state(p, state)
            self.assertIn("k", m.load_state(p)["vins"])
            p.write_text("{broken", encoding="utf-8")
            fresh = m.load_state(p)
            self.assertEqual(fresh["vins"], {})


class TestMessage(unittest.TestCase):
    def test_format_contains_key_info(self):
        cars = [m.summarize_vehicle(REAL_AWD_DEMO, "92614")]
        title, md = m.format_message(cars, "92614", set())
        self.assertIn("上新1辆", title)
        self.assertIn("$76,890", md)
        self.assertIn("官方直降 $5,100", md)
        self.assertIn("展车", md)
        self.assertIn("4,600", md)
        self.assertIn("tesla.com/ct/order/7G2C258_7318ae06a4d9de9db8f4ac25ad8cf56f", md)
        self.assertNotIn("交付信息", md)  # 布尔型 DeliveryDateDisplay 不应渲染
        self.assertIn("92614", md)

    def test_resurfaced_tag_and_cap(self):
        cars = [m.summarize_vehicle(car(f"v{i}", "CT_AWD", "Premium All-Wheel Drive", "$MTC07"), "92614")
                for i in range(14)]
        title, md = m.format_message(cars, "92614", {"v0"})
        self.assertIn("回补1辆", title)
        self.assertIn("上新13辆", title)
        self.assertIn("🔁 重新上架", md)
        self.assertIn("另有 4 辆未逐一列出", md)


class FakeFetcher:
    def __init__(self, vehicles):
        self.vehicles = vehicles

    def fetch_all(self):
        if isinstance(self.vehicles, Exception):
            raise self.vehicles
        return self.vehicles

    def close(self):
        pass


class TestRunOnce(unittest.TestCase):
    def cfg(self, tmp):
        return {
            "ZIP_CODE": "92614", "EXCLUDE_DEMO": "0", "RESURFACE_DAYS": "3",
            "FAIL_ALERT_THRESHOLD": "3", "FETCH_MODES": "headless",
            "STATE_FILE": str(Path(tmp) / "state" / "seen.json"),
        }

    def test_notify_success_commits_state(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = self.cfg(d)
            fetcher = FakeFetcher([REAL_AWD_DEMO, REAL_AWD_NEW, REAL_BEAST])
            with mock.patch.object(m, "notify_all", return_value=(True, [("Server酱", True, "ok")])) as na:
                self.assertTrue(m.run_once(fetcher, cfg))
                self.assertEqual(na.call_count, 1)
                title = na.call_args[0][1]
                self.assertIn("上新2辆", title)  # 野兽版不该出现在推送里
            state = m.load_state(Path(cfg["STATE_FILE"]))
            self.assertEqual(len(state["vins"]), 2)
            # 第二轮:无新车,不再推送
            with mock.patch.object(m, "notify_all", return_value=(True, [("Server酱", True, "ok")])) as na2:
                self.assertTrue(m.run_once(fetcher, cfg))
                self.assertEqual(na2.call_count, 0)

    def test_notify_failure_retries_next_round(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = self.cfg(d)
            fetcher = FakeFetcher([REAL_AWD_NEW])
            with mock.patch.object(m, "notify_all", return_value=(False, [("Server酱", False, "boom")])):
                m.run_once(fetcher, cfg)
            state = m.load_state(Path(cfg["STATE_FILE"]))
            self.assertEqual(len(state["vins"]), 0)  # 未提醒成功 → 不落盘
            with mock.patch.object(m, "notify_all", return_value=(True, [("Server酱", True, "ok")])) as na:
                m.run_once(fetcher, cfg)
                self.assertEqual(na.call_count, 1)  # 下一轮重试成功
            self.assertEqual(len(m.load_state(Path(cfg["STATE_FILE"]))["vins"]), 1)

    def test_resurface_notify_failure_keeps_retrying(self):
        # 回归测试:回补车推送失败时 last_seen 不能被刷新,否则回补提醒永久丢失
        with tempfile.TemporaryDirectory() as d:
            cfg = self.cfg(d)
            fetcher = FakeFetcher([REAL_AWD_NEW])
            with mock.patch.object(m, "notify_all", return_value=(True, [("ok", True, "")])):
                m.run_once(fetcher, cfg)  # 先正常入库
            state_path = Path(cfg["STATE_FILE"])
            state = m.load_state(state_path)
            key = next(iter(state["vins"]))
            old = (m.now_utc() - timedelta(days=5)).isoformat()
            state["vins"][key]["last_seen"] = old
            m.save_state(state_path, state)
            # 回补被检测到但推送失败 → last_seen 必须保持旧值
            with mock.patch.object(m, "notify_all", return_value=(False, [("x", False, "boom")])) as na:
                m.run_once(fetcher, cfg)
                self.assertEqual(na.call_count, 1)
            self.assertEqual(m.load_state(state_path)["vins"][key]["last_seen"], old)
            # 下一轮推送成功 → 仍按“回补”再次触发,并刷新 last_seen
            with mock.patch.object(m, "notify_all", return_value=(True, [("ok", True, "")])) as na2:
                m.run_once(fetcher, cfg)
                self.assertEqual(na2.call_count, 1)
                self.assertIn("回补1辆", na2.call_args[0][1])
            self.assertNotEqual(m.load_state(state_path)["vins"][key]["last_seen"], old)

    def test_exclude_demo_toggle(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = self.cfg(d)
            cfg["EXCLUDE_DEMO"] = "1"
            fetcher = FakeFetcher([REAL_AWD_DEMO, REAL_AWD_NEW])
            with mock.patch.object(m, "notify_all", return_value=(True, [("Server酱", True, "ok")])) as na:
                m.run_once(fetcher, cfg)
                title = na.call_args[0][1]
                self.assertIn("上新1辆", title)

    def test_exclude_demo_odometer_redline(self):
        # 里程红线:没标展车标志但里程 7000+ 英里的车,EXCLUDE_DEMO=1 时也要排除;
        # 4 英里的真全新车不受影响
        with tempfile.TemporaryDirectory() as d:
            cfg = self.cfg(d)
            cfg["EXCLUDE_DEMO"] = "1"
            sneaky_demo = car("y1", "CT_AWD", "Cybertruck All-Wheel Drive", "$MTC03",
                              price=73950, demo=False, odo=7056)
            fresh = car("y2", "CT_AWD", "Premium All-Wheel Drive", "$MTC07", odo=4)
            fetcher = FakeFetcher([sneaky_demo, fresh])
            with mock.patch.object(m, "notify_all", return_value=(True, [("ok", True, "")])) as na:
                m.run_once(fetcher, cfg)
                title, md = na.call_args[0][1], na.call_args[0][2]
                self.assertIn("上新1辆", title)
                self.assertNotIn("73,950", md)
            # EXCLUDE_DEMO=0 时两辆都提醒(高里程车正常标注)
            cfg2 = self.cfg(d)
            cfg2["EXCLUDE_DEMO"] = "0"
            cfg2["STATE_FILE"] = str(Path(d) / "state2" / "seen.json")
            with mock.patch.object(m, "notify_all", return_value=(True, [("ok", True, "")])) as na2:
                m.run_once(FakeFetcher([sneaky_demo, fresh]), cfg2)
                self.assertIn("上新2辆", na2.call_args[0][1])

    def test_notify_failure_counter_escalates_and_resets(self):
        # 回归测试:Secret 写坏导致推送连败时,计数要累计(供退出码亮红灯),恢复后清零
        with tempfile.TemporaryDirectory() as d:
            cfg = self.cfg(d)
            fetcher = FakeFetcher([REAL_AWD_NEW])
            with mock.patch.object(m, "notify_all", return_value=(False, [("x", False, "40001")])):
                for _ in range(4):
                    m.run_once(fetcher, cfg)
            state = m.load_state(Path(cfg["STATE_FILE"]))
            self.assertEqual(state["consecutive_notify_failures"], 4)
            with mock.patch.object(m, "notify_all", return_value=(True, [("ok", True, "")])):
                m.run_once(fetcher, cfg)
            self.assertEqual(
                m.load_state(Path(cfg["STATE_FILE"]))["consecutive_notify_failures"], 0
            )

    def test_config_values_are_stripped(self):
        # 回归测试:GitHub Secret 经管道注入可能带尾随换行,必须清理
        with mock.patch.dict(m.os.environ, {"SERVERCHAN_SENDKEY": "SCTxxxx\r\n", "ZIP_CODE": " 92614 "}):
            cfg = m.load_config()
        self.assertEqual(cfg["SERVERCHAN_SENDKEY"], "SCTxxxx")
        self.assertEqual(cfg["ZIP_CODE"], "92614")

    def test_failure_alert_threshold_and_cooldown(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = self.cfg(d)  # 阈值 3
            fetcher = FakeFetcher(m.FetchError("模拟被风控"))
            with mock.patch.object(m, "notify_all", return_value=(True, [("Server酱", True, "ok")])) as na:
                for _ in range(5):
                    self.assertFalse(m.run_once(fetcher, cfg))
                # 连续失败 3 次时告警一次,之后 24h 冷却内不再发
                self.assertEqual(na.call_count, 1)
            state = m.load_state(Path(cfg["STATE_FILE"]))
            self.assertEqual(state["consecutive_failures"], 5)
            # 恢复成功后清零
            with mock.patch.object(m, "notify_all", return_value=(True, [("ok", True, "")])):
                m.run_once(FakeFetcher([]), cfg)
            self.assertEqual(m.load_state(Path(cfg["STATE_FILE"]))["consecutive_failures"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)

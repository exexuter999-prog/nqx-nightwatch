# -*- coding: utf-8 -*-
import os
import sys
import unittest
from unittest import mock
from datetime import datetime, timedelta, timezone

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import monitor_publish  # noqa: E402


class MarketFeedTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 8, 14, 0, 10, tzinfo=timezone.utc)
        self.bundle = {
            "at": self.now.isoformat(),
            "priceAt": (self.now - timedelta(seconds=20)).isoformat(),
            "priceSource": "TradingView quote_get",
            "sourceSymbol": "CME_MINI:MNQ1!",
            "price": 30205.25,
            "vwap": 30197.21,
            "cvd": 153437,
            "regime": "MX",
            "snapshot": {
                "symbol": "CME_MINI:MNQ1!",
                "resolution": "15",
                "bars": [
                    {"time": 1786665060, "open": 30190, "high": 30200, "low": 30188, "close": 30198},
                    {"time": 1786665960, "open": 30198, "high": 30208, "low": 30196, "close": 30205},
                ],
                "bars3m": [
                    {"time": 1786665960, "open": 30200, "high": 30204.25, "low": 30198.75, "close": 30203.5},
                    {"time": 1786666140, "open": 30203.5, "high": 30206, "low": 30202, "close": 30205.25},
                ],
                "levels": [{"name": "C: POC", "price": 30198.28}],
            },
        }

    def test_builds_verified_3m_contract(self):
        market = monitor_publish.build_market_payload(self.bundle, now=self.now)
        self.assertTrue(market["verified"])
        self.assertEqual(market["barResolution"], "3")
        self.assertEqual(market["bars"][1]["c"], 30205.25)
        self.assertEqual(market["sourceSymbol"], "CME_MINI:MNQ1!")

    def test_truncated_bar_window_keeps_original_count_for_the_mini_app_warning(self):
        expanded = dict(self.bundle, snapshot=dict(self.bundle["snapshot"]))
        expanded["snapshot"]["bars"] = self.bundle["snapshot"]["bars"] * 40
        expanded["snapshot"]["bars3m"] = self.bundle["snapshot"]["bars3m"] * 40
        compacted = monitor_publish.compact(expanded)
        market = monitor_publish.build_market_payload(compacted, now=self.now)
        self.assertEqual(market["barsTruncatedFrom"], 80)

    def test_stale_quote_fails_closed(self):
        self.bundle["priceAt"] = (self.now - timedelta(minutes=11)).isoformat()
        with self.assertRaisesRegex(ValueError, "stale"):
            monitor_publish.build_market_payload(self.bundle, now=self.now)

    def test_non_mnq_source_fails_closed(self):
        self.bundle["sourceSymbol"] = "CBOE:VIX"
        with self.assertRaisesRegex(ValueError, "MNQ"):
            monitor_publish.build_market_payload(self.bundle, now=self.now)

    def test_off_tick_price_fails_closed(self):
        self.bundle["price"] = 30205.13
        with self.assertRaisesRegex(ValueError, "0.25"):
            monitor_publish.build_market_payload(self.bundle, now=self.now)

    # ------------------------------------------------- R52 CVD 観測時刻の受け渡し
    # `tv_snapshot` は CVD の時刻を **`cvdMeta.at` にしか書かない**。ここが
    # top-level `cvdAt` だけを見ていた間、market payload は常に `cvdAt: null` で
    # `execution_contract` が CVD_TIMESTAMP_MISSING を立て、A+ が永久に武装できなかった。

    def test_cvd_timestamp_comes_from_cvd_meta(self):
        stamp = (self.now - timedelta(seconds=5)).isoformat()
        market = monitor_publish.build_market_payload(
            {**self.bundle, "cvdMeta": {"at": stamp, "status": "FRESH"}}, now=self.now)
        self.assertEqual(market["cvdAt"], stamp)

    def test_explicit_cvd_at_wins_over_meta(self):
        explicit = (self.now - timedelta(seconds=1)).isoformat()
        market = monitor_publish.build_market_payload(
            {**self.bundle, "cvdAt": explicit,
             "cvdMeta": {"at": (self.now - timedelta(seconds=30)).isoformat()}},
            now=self.now)
        self.assertEqual(market["cvdAt"], explicit)

    def test_cvd_timestamp_absent_is_not_invented(self):
        market = monitor_publish.build_market_payload(self.bundle, now=self.now)
        self.assertIsNone(market["cvdAt"])

    # ------------------------------------------------------------ R6 評価カード
    # APP_EVAL_DISPLAY_SPEC §4: evaluation は任意の透過フィールド。
    # **キーが無ければ payload は R6 以前と完全に同一**であることを保証する。

    def test_r6_evaluation_absent_keeps_payload_identical(self):
        market = monitor_publish.build_market_payload(self.bundle, now=self.now)
        self.assertNotIn("evaluation", market)

    # ------------------------------------------------------------ R56 指標束
    # チャート用の表示専用フィールド。値が無ければキーごと載せない(既存 payload と同一)。

    def test_r56_indicators_absent_keeps_payload_identical(self):
        market = monitor_publish.build_market_payload(self.bundle, now=self.now)
        self.assertNotIn("indicators", market)

    def test_r56_indicators_carry_vwap_band_cvd_and_ct(self):
        bundle = dict(self.bundle, snapshot=dict(self.bundle["snapshot"]))
        bundle["snapshot"].update({
            "vwap_hi": 30230.5, "vwap_lo": 30170.25, "vwapAnchorT": 1786665000,
            "po3": "MANIPULATION_UP",
            "smtObservation": {"peer": "MES1!", "bias": "BULLISH", "agree": True, "position": 0.02},
            "study15m": {"NQX_DATA_CT_TREND": -1.0, "NQX_DATA_CT_ATR": 37.58,
                         "NQX_DATA_CT_FIB618": 30210.0, "NQX_DATA_CT_TRAIL": 30240.0,
                         "CT SwingArm / 100": 30250.0, "unrelated": 1.0},
            "eventGate": {"state": "NONE"},
        })
        bundle["cvd"] = {"bias": "BULLISH", "fast": -26574.0, "slow": -34031.0, "value": -19025.0}
        bundle["cvdMeta"] = {"at": self.bundle["at"], "status": "FRESH",
                             "history": [float(i) for i in range(30)]}
        market = monitor_publish.build_market_payload(bundle, now=self.now)
        ind = market["indicators"]
        self.assertEqual(ind["vwapHi"], 30230.5)
        self.assertEqual(ind["cvd"]["value"], -19025.0)
        self.assertEqual(ind["cvd"]["bias"], "BULLISH")
        self.assertEqual(len(ind["cvd"]["history"]), 24)
        self.assertEqual(ind["ct"]["trend"], -1.0)
        self.assertNotIn("unrelated", ind["ct"])
        self.assertEqual(ind["po3"], "MANIPULATION_UP")
        self.assertEqual(ind["smt"]["peer"], "MES1!")
        # 辞書の CVD は数値へ落とす(Worker は数値しか受けない)
        self.assertEqual(market["cvd"], -19025.0)

    def test_r6_evaluation_passes_through_untouched(self):
        card = {
            "at": self.bundle["at"],
            "volGate": {"noise": 12.5, "slCap": 25, "ratio": 0.5, "ruling": "A+のみ"},
            "rotation": {"signals": 5, "negations": 2, "verdict": "OK"},
            "summary": "MSNR: Weekly Mid30253 SELL連鎖 フリップ保持",
        }
        without = monitor_publish.build_market_payload(dict(self.bundle), now=self.now)
        market = monitor_publish.build_market_payload(
            {**self.bundle, "evaluation": card}, now=self.now)
        self.assertEqual(market["evaluation"], card)
        # evaluation を除けば1バイトも変わらないこと(既存表示への非干渉)
        self.assertEqual({k: v for k, v in market.items() if k != "evaluation"}, without)

    def test_r6_empty_or_wrong_typed_evaluation_is_not_attached(self):
        for bad in ({}, [], "x", None, 3, True):
            market = monitor_publish.build_market_payload(
                {**self.bundle, "evaluation": bad}, now=self.now)
            self.assertNotIn("evaluation", market, f"bad evaluation attached: {bad!r}")

    def test_r6_evaluation_survives_compact(self):
        # compact() のホワイトリストを通ること(ここで落ちると publish に届かない)
        card = {"at": self.bundle["at"], "volGate": {"ratio": 0.5}, "rotation": {}}
        compacted = monitor_publish.compact({**self.bundle, "evaluation": card})
        self.assertEqual(compacted["evaluation"], card)

    def test_atomic_cycle_failure_never_publishes_market_or_scenario_separately(self):
        calls = []
        proposal = {"state": "ARMED", "side": "BUY", "qty": 2,
                    "entry": 30205.25, "stop": 30180.25, "target": 30230.25,
                    "targets": [30230.25, 30255.25],
                    "legs": [{"id": "TP1", "qty": 1, "target": 30230.25},
                             {"id": "RUNNER", "qty": 1, "target": 30255.25}],
                    "planVersion": "R17-SPLIT-1"}
        fresh = datetime.now(timezone.utc).isoformat()
        with mock.patch.object(monitor_publish.nqx_state, "load_cloud_env", return_value={"NQX_SYMBOL": "MNQU6"}), \
             mock.patch.object(monitor_publish.nqx_state, "publish_cycle", side_effect=lambda *_: (calls.append("cycle") or (False, {"reason": "down"}))), \
             mock.patch.object(monitor_publish.nqx_state, "publish_market", side_effect=lambda *_: (_ for _ in ()).throw(AssertionError("legacy market publish called"))), \
             mock.patch.object(monitor_publish.nqx_state, "publish_scenario", side_effect=lambda *_: (_ for _ in ()).throw(AssertionError("legacy scenario publish called"))):
            ok, notes = monitor_publish.publish_state({**self.bundle, "at": fresh, "priceAt": fresh,
                                                       "scenarios": {"primary": proposal}})
        self.assertFalse(ok)
        self.assertEqual(calls, ["cycle"])
        self.assertTrue(any("cycle failed" in note for note in notes))

    def test_publish_freezes_two_account_scope_into_server_and_local_handoff(self):
        accounts = ["LTATANOBA1001064330885", "LTATANOBA1005923156221"]
        execution_env = {
            "CROSSTRADE_ACCOUNTS": ",".join(accounts),
            "RISK_LTATANOBA1001064330885": "240",
            "RISK_LTATANOBA1005923156221": "240",
        }
        proposal = {
            "state": "ARMED", "grade": "A", "side": "SELL", "qty": 2,
            "entry": 30205.25, "stop": 30225.25,
            "target": 30175.25, "targets": [30175.25, 30145.25],
            "legs": [{"id": "TP1", "qty": 1, "target": 30175.25},
                     {"id": "RUNNER", "qty": 1, "target": 30145.25}],
            "planVersion": "R40-MIRRORED-SPLIT-1",
        }
        fresh = datetime.now(timezone.utc).isoformat()
        bundle = {**self.bundle, "at": fresh, "priceAt": fresh, "cvdAt": fresh,
                  "scenarios": {"primary": proposal}}
        captured = {}

        def publish_cycle(_cycle_id, _market, scenario, _cfg):
            captured["scenario"] = scenario
            return True, {}

        with mock.patch.object(monitor_publish.nqx_state, "load_cloud_env",
                               return_value={"NQX_SYMBOL": "MNQU6"}), \
             mock.patch.object(monitor_publish.nqx_state, "_read_kv_env",
                               return_value=execution_env), \
             mock.patch.object(monitor_publish.nqx_state, "publish_accounts",
                               return_value=(True, {})), \
             mock.patch.object(monitor_publish.nqx_state, "publish_cycle",
                               side_effect=publish_cycle):
            ok, _notes = monitor_publish.publish_state(bundle)

        self.assertTrue(ok)
        self.assertEqual(captured["scenario"]["executionContract"]["accountScope"],
                         accounts)
        self.assertEqual(bundle["_published_scenario"]["executionContract"]["accountScope"],
                         accounts)
        self.assertEqual(bundle["_published_scenario"]["fingerprint"],
                         captured["scenario"]["fingerprint"])

    def test_invalid_market_uses_atomic_disarm_not_legacy_scenario_clear(self):
        calls = []
        stale = {**self.bundle, "priceAt": (datetime.now(timezone.utc) - timedelta(seconds=601)).isoformat()}
        with mock.patch.object(monitor_publish.nqx_state, "load_cloud_env", return_value={"NQX_SYMBOL": "MNQU6"}), \
             mock.patch.object(monitor_publish.nqx_state, "publish_cycle",
                               side_effect=lambda cycle_id, market, scenario, *_: (calls.append((cycle_id, market, scenario)) or (True, {}))), \
             mock.patch.object(monitor_publish.nqx_state, "clear_scenario",
                               side_effect=AssertionError("legacy scenario clear called")):
            ok, notes = monitor_publish.publish_state(stale)
        self.assertFalse(ok)
        self.assertEqual(len(calls), 1)
        self.assertIsNone(calls[0][1])
        self.assertIsNone(calls[0][2])
        self.assertTrue(any("cycle disarmed" in note for note in notes))



class FixedQtyTests(unittest.TestCase):
    """枚数の口座ルール(既定2枚)を publish 直前に強制する(2026-08-22)。

    監視側が qty を書き忘れても、アプリ表示と order.py の判定が食い違わないこと。
    """

    ENV = {"CROSSTRADE_ACCOUNTS": "APEX4568750000010",
           "RISK_APEX4568750000010": "240", "MAX_RISK_DOLLARS": "240"}

    def test_qty_is_normalized_to_account_rule(self):
        out, notes = monitor_publish.normalize_qty(
            {"state": "ACTIVE", "side": "SELL", "entry": 29425,
             "stop": 29462, "target": 29380.5, "qty": 1}, self.ENV)
        self.assertEqual(out["qty"], 2)
        self.assertEqual(out["state"], "ACTIVE")
        self.assertTrue(any("qty normalized" in n for n in notes))

    def test_over_cap_at_fixed_qty_demotes_to_watch(self):
        out, notes = monitor_publish.normalize_qty(
            {"state": "ACTIVE", "side": "SELL", "entry": 29425,
             "stop": 29495, "target": 29300, "qty": 1}, self.ENV)
        self.assertEqual(out["qty"], 2)
        self.assertEqual(out["state"], "WATCH")
        self.assertTrue(any("exceeds account cap" in n for n in notes))

    def test_watch_is_left_alone(self):
        out, notes = monitor_publish.normalize_qty(
            {"state": "WATCH", "side": "SELL", "entry": 29425,
             "stop": 29495, "target": 29300, "qty": 2}, self.ENV)
        self.assertEqual(out["state"], "WATCH")
        self.assertEqual(notes, [])

    def test_env_cannot_override_execution_contract_fixed_qty(self):
        env = dict(self.ENV, NQX_FIXED_QTY="1")
        out, _ = monitor_publish.normalize_qty(
            {"state": "ACTIVE", "side": "SELL", "entry": 29425,
             "stop": 29462, "target": 29380.5, "qty": 2}, env)
        self.assertEqual(out["qty"], 2)


if __name__ == "__main__":
    unittest.main()

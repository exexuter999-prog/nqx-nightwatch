# -*- coding: utf-8 -*-
"""R77: 報告行は publish 後の正本(ゲート後 state)から作る。

2026-09-10 23:41 JST、publish 段は
「vol gate stand-down (noise 42.12pt / ratio 0.84 > 0.60) — scenario demoted to WATCH」
を出したのに、最終行は pipeline の decision から
「primary=VP80_REVERSION SELL A ARMED … | published」を印字した。載った正本は WATCH。
報告行は Telegram の先頭行でありエージェントが逐語で返す行なので、これは誤報になる。

`monitor_publish.py` はゲート後の scenario を `.secrets/monitor_last_sent.json` の
`_published_scenario` に残す(None = 何も武装していない)。`nqx_cycle.stage_publish()` が
送った bundle と同じ `at` を持つ正本だけを採り、`report_line()` はそれを優先する。
pipeline の decision に戻るのは publish が走らなかったサイクルだけ。

ネットワークも本番 .secrets も使わない。

    python tests/test_r77_report_line_published_state.py
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
import nqx_cycle  # noqa: E402

SENT_AT = "2026-09-10T14:37:40.283792+00:00"

# 23:41 の `.secrets/monitor_pipeline_cycle.json` の decision(publish 前・ARMED)。
DECISION = {
    "model": "VP80_REVERSION", "side": "SELL", "grade": "A", "state": "ARMED",
    "entry": 29250.0, "stop": 29297.0, "targets": [29173.5, 29042.5],
    "decisionId": "6e883a0af5d731d7",
}
CYCLE = {"status": "READY", "symbol": "MNQU6", "decision": DECISION,
         "publishPreflight": {"ready": True}}

# 監査コピー `.secrets/monitor_cycle_2341.json` の scenarios は **dict**(`primary` キー)。
# publish 前の bundle なので ARMED のまま。
BUNDLE = {"at": SENT_AT, "price": 29262.75, "priceAt": SENT_AT, "sourceSymbol": "MNQU6",
          "scenarios": {"primary": dict(DECISION)}}

# `monitor_publish.py` が `_published_scenario` に残す build_scenario 形(ゲート後・WATCH)。
PUBLISHED_WATCH = {
    "scenarioId": "6e883a0af5d731d7", "model": "VP80_REVERSION", "side": "SELL",
    "grade": "A", "state": "WATCH", "entry": 29250.0, "stop": 29297.0,
    "target": 29173.5, "targets": [29173.5, 29042.5], "qty": 2, "symbol": "MNQU6",
    "executionContract": {"effectiveGrade": "A", "orderable": False},
}

VOL_GATE_DETAIL = (
    "  server state: ok\n"
    "  autotrade   : OFF (test)\n"
    "    - accounts ok\n"
    "    - vol gate stand-down (noise 42.12pt / ratio 0.84 > 0.60) — scenario demoted to WATCH\n"
    "    - cycle cy_test ok\n"
    "published monitor bundle to Telegram"
)


class _Patching(unittest.TestCase):
    def patch(self, obj, name, value):
        missing = object()
        original = getattr(obj, name, missing)

        def restore():
            if original is missing:
                delattr(obj, name)
            else:
                setattr(obj, name, original)
        self.addCleanup(restore)
        setattr(obj, name, value)

    def tmpdir(self):
        path = tempfile.mkdtemp(prefix="nqx-r77-")
        self.addCleanup(lambda: __import__("shutil").rmtree(path, ignore_errors=True))
        return Path(path)


class ReportLineTests(_Patching):
    def setUp(self):
        self.patch(nqx_cycle, "_price_from_bundle", lambda: 29262.75)

    def test_published_watch_overrides_pipeline_armed(self):
        """核心: pipeline は ARMED、publish は WATCH に落とした → 行は WATCH。"""
        line = nqx_cycle.report_line(
            CYCLE, True, "",
            published_state={"at": SENT_AT, "source": "monitor_last_sent.json",
                             "scenario": PUBLISHED_WATCH})
        self.assertIn("primary=VP80_REVERSION SELL A WATCH", line)
        self.assertNotIn("ARMED", line)
        self.assertIn("E=29,250 SL=29,297 TP=29,173.5/29,042.5", line)
        self.assertIn("MNQU6 29,262.75", line)
        self.assertTrue(line.endswith("| published"), line)
        self.assertNotIn("unread", line)

    def test_published_geometry_and_grade_come_from_the_published_scenario(self):
        """建値/SL/TP と等級も正本側(ULTRA 焼き直し・A+→A 上限化の後)を使う。"""
        scenario = {**PUBLISHED_WATCH, "entry": 29247.75, "stop": 29286.0,
                    "targets": [29176.25, 29042.5]}
        scenario.pop("grade")   # grade 欠落なら executionContract.effectiveGrade
        line = nqx_cycle.report_line(CYCLE, True, "", published_state={"scenario": scenario})
        self.assertIn("SELL A WATCH", line)
        self.assertIn("E=29,247.75 SL=29,286 TP=29,176.25/29,042.5", line)
        self.assertNotIn("29,250", line)

    def test_published_with_no_scenario_reports_primary_none(self):
        """publish が武装を載せなかった(_published_scenario=None) → primary=NONE。"""
        line = nqx_cycle.report_line(CYCLE, True, "", published_state={"scenario": None})
        self.assertIn("primary=NONE", line)
        self.assertNotIn("VP80_REVERSION", line)
        self.assertTrue(line.endswith("| published"), line)

    def test_no_publish_falls_back_to_pipeline_decision(self):
        """publish が走らなかった(BLOCKED / DRY / 失敗)ときだけ decision を出す。"""
        line = nqx_cycle.report_line(CYCLE, False, "DRY（publish 未実行）")
        self.assertIn("primary=VP80_REVERSION SELL A ARMED", line)
        self.assertIn("| no-publish |", line)
        self.assertNotIn("unread", line)

    def test_published_but_state_unread_is_marked(self):
        """publish は通ったのに正本が読めない → decision を出すが未確認を明示する。"""
        line = nqx_cycle.report_line(CYCLE, True, "", published_state=None)
        self.assertIn("SELL A ARMED", line)
        self.assertIn("| published | post-gate state unread — showing pipeline decision", line)

    def test_extra_is_appended_after_the_publish_token(self):
        line = nqx_cycle.report_line(
            CYCLE, True, "demoted: vol gate stand-down (noise 42.12pt / ratio 0.84 > 0.60)",
            published_state={"scenario": PUBLISHED_WATCH})
        self.assertTrue(line.endswith(
            "| published | demoted: vol gate stand-down (noise 42.12pt / ratio 0.84 > 0.60)"), line)


class ReadPublishedStateTests(_Patching):
    def setUp(self):
        self.state = self.tmpdir() / "monitor_last_sent.json"
        self.patch(nqx_cycle, "PUBLISHED_STATE", self.state)

    def write(self, payload):
        self.state.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def test_matching_at_returns_the_post_gate_scenario_not_the_scenarios_dict(self):
        # scenarios(dict, primary=ARMED)は publish 前の写し。正本は _published_scenario。
        self.write({**BUNDLE, "_published_scenario": PUBLISHED_WATCH})
        result = nqx_cycle.read_published_state(SENT_AT)
        self.assertIsNotNone(result)
        self.assertEqual(result["at"], SENT_AT)
        self.assertEqual(result["source"], "monitor_last_sent.json")
        self.assertEqual(result["scenario"]["state"], "WATCH")
        self.assertEqual(result["scenario"]["scenarioId"], "6e883a0af5d731d7")

    def test_disarmed_publish_returns_scenario_none_not_unread(self):
        self.write({**BUNDLE, "_published_scenario": None})
        result = nqx_cycle.read_published_state(SENT_AT)
        self.assertIsNotNone(result)
        self.assertIsNone(result["scenario"])

    def test_state_from_another_cycle_is_not_trusted(self):
        self.write({**BUNDLE, "at": "2026-09-10T14:34:40+00:00",
                    "_published_scenario": PUBLISHED_WATCH})
        self.assertIsNone(nqx_cycle.read_published_state(SENT_AT))

    def test_missing_file_key_or_sent_at_returns_none(self):
        self.assertIsNone(nqx_cycle.read_published_state(SENT_AT))          # ファイル無し
        self.write(dict(BUNDLE))                                              # キー無し(旧 publish)
        self.assertIsNone(nqx_cycle.read_published_state(SENT_AT))
        self.write({**BUNDLE, "_published_scenario": PUBLISHED_WATCH})
        self.assertIsNone(nqx_cycle.read_published_state(None))             # 送った at 不明
        self.state.write_text("{not json", encoding="utf-8")                 # 壊れた正本
        self.assertIsNone(nqx_cycle.read_published_state(SENT_AT))

    def test_bom_prefixed_state_is_readable(self):
        self.state.write_bytes(b"\xef\xbb\xbf" + json.dumps(
            {**BUNDLE, "_published_scenario": PUBLISHED_WATCH}).encode("utf-8"))
        result = nqx_cycle.read_published_state(SENT_AT)
        self.assertEqual(result["scenario"]["state"], "WATCH")


class StagePublishTests(_Patching):
    def setUp(self):
        root = self.tmpdir()
        self.bundle = root / "monitor_pipeline_bundle.json"
        self.state = root / "monitor_last_sent.json"
        self.bundle.write_text(json.dumps(BUNDLE, ensure_ascii=False), encoding="utf-8")
        self.patch(nqx_cycle, "PUBLISHED_STATE", self.state)
        self.patch(nqx_cycle, "_paths", lambda: {"bundle": self.bundle})
        self.calls = []

    def fake_run(self, code, detail, write_state=True):
        def _run(args, stdin=None, timeout=180):
            self.calls.append({"args": list(args), "stdin": stdin, "timeout": timeout})
            if write_state:
                self.state.write_text(json.dumps(
                    {**BUNDLE, "_published_scenario": PUBLISHED_WATCH}, ensure_ascii=False),
                    encoding="utf-8")
            return code, detail, ""
        return _run

    def test_success_returns_the_post_gate_state_for_the_bundle_it_sent(self):
        self.patch(nqx_cycle, "_run", self.fake_run(0, VOL_GATE_DETAIL))
        ok, detail, published_state = nqx_cycle.stage_publish()
        self.assertTrue(ok)
        self.assertIn("demoted to WATCH", detail)
        self.assertEqual(self.calls[0]["args"], ["monitor_publish.py"])
        self.assertEqual(self.calls[0]["stdin"], self.bundle.read_bytes())
        self.assertEqual(published_state["scenario"]["state"], "WATCH")
        self.assertEqual(published_state["at"], SENT_AT)

    def test_failure_returns_no_state(self):
        self.patch(nqx_cycle, "_run", self.fake_run(1, "ERROR: Telegram send failed", write_state=False))
        ok, detail, published_state = nqx_cycle.stage_publish()
        self.assertFalse(ok)
        self.assertIn("Telegram send failed", detail)
        self.assertIsNone(published_state)

    def test_success_without_a_fresh_state_file_is_unread(self):
        self.patch(nqx_cycle, "_run", self.fake_run(0, "  server state: ok", write_state=False))
        ok, _detail, published_state = nqx_cycle.stage_publish()
        self.assertTrue(ok)
        self.assertIsNone(published_state)

    def test_unreadable_bundle_never_runs_publish(self):
        self.bundle.unlink()
        self.patch(nqx_cycle, "_run", self.fake_run(0, "must not run"))
        ok, detail, published_state = nqx_cycle.stage_publish()
        self.assertFalse(ok)
        self.assertIn("publish bundle unreadable", detail)
        self.assertIsNone(published_state)
        self.assertEqual(self.calls, [])


class DemotionNotesTests(unittest.TestCase):
    def test_vol_gate_stand_down_is_extracted_without_the_suffix(self):
        self.assertEqual(nqx_cycle.publish_demotion_notes(VOL_GATE_DETAIL),
                         ["vol gate stand-down (noise 42.12pt / ratio 0.84 > 0.60)"])

    def test_other_publish_demotions_are_recognised(self):
        detail = "\n".join([
            "    - event blackout (CPI) — scenario demoted to WATCH",
            "    - vol gate A+ only (noise 30.00pt / ratio 0.50 > 0.40) — grade A demoted to WATCH",
            "    - ULTRA skip: target unreachable — scenario demoted to WATCH (見送り)",
            "    - risk 70.00pt x 2 exceeds account cap (60.00pt at 2) — scenario demoted to WATCH",
            "    - unknown scenario state 'ARMD' — demoted to WATCH (fail-safe)",
        ])
        self.assertEqual(nqx_cycle.publish_demotion_notes(detail), [
            "event blackout (CPI)",
            "vol gate A+ only (noise 30.00pt / ratio 0.50 > 0.40)",
            "ULTRA skip: target unreachable",
            "risk 70.00pt x 2 exceeds account cap (60.00pt at 2)",
            "unknown scenario state 'ARMD' — demoted to WATCH (fail-safe)",
        ])

    def test_no_demotion_yields_nothing(self):
        self.assertEqual(nqx_cycle.publish_demotion_notes("  server state: ok\n    - cycle cy_x ok"), [])
        self.assertEqual(nqx_cycle.publish_demotion_notes(""), [])


class RunCycleFinalLineTests(_Patching):
    """run_cycle の最終行(エージェントが返す行)を段階スタブで確認する。"""

    def setUp(self):
        class _FakeArm:
            @staticmethod
            def summary():
                return "OFF (test)"

            @staticmethod
            def state(*a, **k):
                return {"kill": False}

        original_arm = sys.modules.get("autotrade_arm")
        sys.modules["autotrade_arm"] = _FakeArm()
        self.addCleanup(lambda: (sys.modules.__setitem__("autotrade_arm", original_arm)
                                 if original_arm is not None
                                 else sys.modules.pop("autotrade_arm", None)))

        class _FakeFillWatch:
            """R91: 契約 fillWatch.autostart=true でも、テストの周期から本物の fill_watch を起動しない。"""
            @staticmethod
            def supervise(*a, **k):
                return "fill_watch: alive (test)"

            @staticmethod
            def status_line(*a, **k):
                return "fill_watch: alive (test)"

        original_fill_watch = sys.modules.get("fill_watch")
        sys.modules["fill_watch"] = _FakeFillWatch()
        self.addCleanup(lambda: (sys.modules.__setitem__("fill_watch", original_fill_watch)
                                 if original_fill_watch is not None
                                 else sys.modules.pop("fill_watch", None)))
        self.beacons = []
        self.patch(nqx_cycle, "_preflight_config", lambda: None)
        self.patch(nqx_cycle, "stage_acquire", lambda replay=False: (True, ""))
        tv_bundle = self.tmpdir() / "tv_bundle.json"
        tv_bundle.write_bytes(b"{}")
        self.patch(nqx_cycle, "TV_BUNDLE", tv_bundle)
        self.patch(nqx_cycle, "stage_ingest", lambda kind, raw: (True, "receipt"))
        self.patch(nqx_cycle, "stage_pipeline", lambda: ("READY", json.loads(json.dumps(CYCLE)), ""))
        self.patch(nqx_cycle, "save_audit", lambda now=None: None)
        self.patch(nqx_cycle, "send_beacon",
                   lambda status, reason="": self.beacons.append((status, reason)))
        self.patch(nqx_cycle, "_price_from_bundle", lambda: 29262.75)

    def run_cycle(self, **kwargs):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = nqx_cycle.run_cycle(dry=kwargs.pop("dry", False), force_window=True,
                                       replay=False, fetch=False, **kwargs)
        lines = [line for line in out.getvalue().splitlines() if line.strip()]
        return code, lines

    def test_pipeline_armed_but_publish_demoted_to_watch_prints_watch(self):
        self.patch(nqx_cycle, "stage_publish", lambda: (
            True, VOL_GATE_DETAIL,
            {"at": SENT_AT, "source": "monitor_last_sent.json", "scenario": PUBLISHED_WATCH}))
        code, lines = self.run_cycle()
        self.assertEqual(code, 0)
        final = lines[-1]
        self.assertIn("MNQU6 29,262.75 | primary=VP80_REVERSION SELL A WATCH | "
                      "E=29,250 SL=29,297 TP=29,173.5/29,042.5 | published | "
                      "demoted: vol gate stand-down (noise 42.12pt / ratio 0.84 > 0.60)", final)
        self.assertNotIn("ARMED", final)
        self.assertEqual(self.beacons[-1][0], "PUBLISHED")

    def test_published_state_unread_is_visible_in_the_final_line(self):
        self.patch(nqx_cycle, "stage_publish", lambda: (True, "  server state: ok", None))
        code, lines = self.run_cycle()
        self.assertEqual(code, 0)
        self.assertIn("SELL A ARMED", lines[-1])
        self.assertIn("post-gate state unread", lines[-1])

    def test_halt_keeps_the_published_state_and_the_halt_token(self):
        self.patch(nqx_cycle, "stage_publish", lambda: (
            True, VOL_GATE_DETAIL + "\n    - AUTOTRADE HALT: broker UNVERIFIED",
            {"at": SENT_AT, "source": "monitor_last_sent.json", "scenario": PUBLISHED_WATCH}))
        code, lines = self.run_cycle()
        self.assertEqual(code, 2)
        self.assertIn("SELL A WATCH", lines[-1])
        self.assertIn("| published | HALT demoted: vol gate stand-down", lines[-1])

    def test_dry_run_reports_the_pipeline_decision_and_never_publishes(self):
        def _boom():
            raise AssertionError("stage_publish must not run under --dry")
        self.patch(nqx_cycle, "stage_publish", _boom)
        code, lines = self.run_cycle(dry=True)
        self.assertEqual(code, 0)
        self.assertIn("SELL A ARMED", lines[-1])
        self.assertIn("| no-publish | DRY", lines[-1])

    def test_publish_failure_reports_halt_from_the_pipeline_decision(self):
        self.patch(nqx_cycle, "stage_publish", lambda: (False, "ERROR: Telegram send failed", None))
        code, lines = self.run_cycle()
        self.assertEqual(code, 2)
        self.assertIn("SELL A ARMED", lines[-1])
        self.assertIn("| no-publish | HALT publish failed", lines[-1])
        self.assertEqual(self.beacons[-1][0], "HALT")


class PathParityTests(unittest.TestCase):
    def test_published_state_path_matches_monitor_publish_state_file(self):
        """直書きした正本パスが monitor_publish.STATE_FILE から乖離したら止める。"""
        import monitor_publish
        self.assertEqual(Path(monitor_publish.STATE_FILE).resolve(),
                         nqx_cycle.PUBLISHED_STATE.resolve())


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass
    unittest.main(verbosity=1)

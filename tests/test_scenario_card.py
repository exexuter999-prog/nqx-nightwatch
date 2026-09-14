# -*- coding: utf-8 -*-
"""Telegram 通知のシナリオチャート画像(scenario_card.py)と送信経路。

固定するのは:
  * 描けない入力では None を返し、例外を投げない(通知を止めない)
  * 確定足だけを描く(形成中の最終足を落とす — chart.js と同じ規律)
  * 武装 A/A+ のときだけ写真を送り、それ以外は送らない
  * sendPhoto の失敗は本文の送信を止めない
  * 画像は Telegram の上限(10MB)を大きく下回る

外部送信は一切行わない(sendPhoto はスタブ)。
"""
import io
import json
import os
import sys
import unittest
from unittest.mock import patch

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import scenario_card  # noqa: E402

AT = "2026-08-23T05:29:49+09:00"


def bars(n=60, start=30000.0, step_sec=180, last_t=None):
    import math
    t0 = (last_t or 1_787_000_000) - (n - 1) * step_sec
    out = []
    close = start
    for i in range(n):
        o = close
        close = round(start + math.sin(i / 4.6) * 16 + i * 0.3, 2)
        out.append({"t": t0 + i * step_sec, "o": o, "h": max(o, close) + 3, "l": min(o, close) - 3,
                    "c": close, "v": 40 + (i * 13) % 42})
    return out


def bundle(scenario=None, **over):
    rows = bars()
    at_sec = rows[-1]["t"] + 180 + 5      # 最終足は確定済み
    from datetime import datetime, timezone
    at = datetime.fromtimestamp(at_sec, timezone.utc).isoformat()
    data = {"at": at, "price": rows[-1]["c"],
            "snapshot": {"bars3m": rows, "levels": [{"label": "PDH", "price": 30040.0}]}}
    if scenario:
        data["scenarios"] = {"primary": scenario}
    data.update(over)
    return data


ARMED = {"side": "SELL", "entry": 30020.0, "stop": 30041.0, "target": 29970.0,
         "targets": [29995.0, 29970.0], "model": "VP80_REVERSION", "grade": "A+",
         "state": "ARMED", "qty": 2, "expiresAt": "2026-08-23T12:45:00Z"}


class RenderTests(unittest.TestCase):
    def test_renders_a_png_with_an_armed_scenario(self):
        png = scenario_card.render_png(bundle(ARMED))
        self.assertIsNotNone(png)
        self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")
        self.assertLess(len(png), 2_000_000, "Telegram の上限より十分小さい")
        from PIL import Image
        img = Image.open(io.BytesIO(png))
        self.assertEqual(img.size, (scenario_card.W, scenario_card.H))

    def test_renders_without_a_scenario(self):
        png = scenario_card.render_png(bundle())
        self.assertIsNotNone(png, "シナリオ無しでも市況カードは描ける")

    def test_unrenderable_input_returns_none_not_exception(self):
        for bad in ({}, {"snapshot": {}}, {"snapshot": {"bars3m": [{"o": 1}]}},
                    bundle(**{"snapshot": {"bars3m": bars(3)}})):
            with self.subTest(bad=str(bad)[:40]):
                self.assertIsNone(scenario_card.render_png(bad))

    def test_forming_bar_is_dropped(self):
        rows = bars()
        from datetime import datetime, timezone
        # 観測時刻が最終足の close より前 → 最終足は形成中
        at = datetime.fromtimestamp(rows[-1]["t"] + 30, timezone.utc).isoformat()
        confirmed = scenario_card.confirmed_bars({"at": at, "snapshot": {"bars3m": rows}})
        self.assertEqual(len(confirmed), len(rows) - 1)
        at2 = datetime.fromtimestamp(rows[-1]["t"] + 200, timezone.utc).isoformat()
        self.assertEqual(len(scenario_card.confirmed_bars({"at": at2, "snapshot": {"bars3m": rows}})), len(rows))

    def test_nice_step_matches_chart_js_rule(self):
        self.assertEqual(scenario_card.nice_step(120, 400), 20)
        self.assertEqual(scenario_card.nice_step(700, 400), 100)
        self.assertEqual(scenario_card.nice_step(0, 400), 1.0)

    def test_caption_is_short_and_html_safe(self):
        cap = scenario_card.caption(bundle(ARMED))
        self.assertIn("VP80_REVERSION", cap)
        self.assertIn("SHORT", cap)
        self.assertLess(len(cap), 300)
        self.assertIn("no armed scenario", scenario_card.caption(bundle()))


class SendPathTests(unittest.TestCase):
    def _publish(self):
        import monitor_publish
        return monitor_publish

    def test_photo_is_sent_only_for_armed_a_grade(self):
        mp = self._publish()
        calls = []
        with patch("telegram_bot.send_photo", side_effect=lambda cfg, png, caption="", reply_markup=None: (
                calls.append((len(png), caption)) or {"ok": True})):
            cfg = {"TELEGRAM_TOKEN": "t", "TELEGRAM_CHAT_ID": "1"}
            mp._send_scenario_card(cfg, bundle(ARMED))
            mp._send_scenario_card(cfg, bundle({**ARMED, "grade": "B"}))   # 2026-09-04: B も送る
            mp._send_scenario_card(cfg, bundle({**ARMED, "grade": "C"}))   # 契約外の等級は送らない
            mp._send_scenario_card(cfg, bundle({**ARMED, "state": "WATCH"}))
            mp._send_scenario_card(cfg, bundle())
        self.assertEqual(len(calls), 2, "A+/A/B かつ ARMED のときだけ送る(2026-09-04 ユーザー決定)")
        self.assertGreater(calls[0][0], 1000)
        self.assertIn("SHORT", calls[0][1])

    def test_photo_failure_does_not_raise(self):
        mp = self._publish()
        with patch("telegram_bot.send_photo", side_effect=RuntimeError("network")):
            cfg = {"TELEGRAM_TOKEN": "t", "TELEGRAM_CHAT_ID": "1"}
            self.assertIsNone(mp._send_scenario_card(cfg, bundle(ARMED)))

    def test_send_photo_builds_multipart_without_network(self):
        import telegram_bot as tb
        captured = {}

        class _Resp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return json.dumps({"ok": True, "result": {"message_id": 1}}).encode()

        def fake_open(req, timeout=0):
            captured["url"] = req.full_url
            captured["ctype"] = req.get_header("Content-type")
            captured["body"] = req.data
            return _Resp()

        with patch("urllib.request.urlopen", side_effect=fake_open):
            res = tb.send_photo({"TELEGRAM_TOKEN": "TOK", "TELEGRAM_CHAT_ID": "42"}, b"\x89PNGxx", caption="<b>hi</b>")
        self.assertEqual(res["ok"], True)
        self.assertIn("/botTOK/sendPhoto", captured["url"])
        self.assertIn("multipart/form-data; boundary=", captured["ctype"])
        body = captured["body"]
        self.assertIn(b'name="chat_id"\r\n\r\n42', body)
        self.assertIn(b'name="caption"\r\n\r\n<b>hi</b>', body)
        self.assertIn(b'filename="scenario.png"', body)
        self.assertIn(b"\x89PNGxx", body)
        self.assertIsNone(tb.send_photo({"TELEGRAM_TOKEN": "TOK", "TELEGRAM_CHAT_ID": "42"}, b""),
                          "空の画像は送らない")


if __name__ == "__main__":
    unittest.main()

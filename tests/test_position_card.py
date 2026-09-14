# -*- coding: utf-8 -*-
"""保有中カード(position_card.py)と、その送信経路。

固定するのは:
  * 到達時損益は「その脚だけ」の金額 × 口座数(TP1 と runner を足さない)
  * SL は全枚数 × 口座数(片脚ではない)
  * LONG / SHORT のどちらでも符号が反転しない
  * 建玉が無い・UNVERIFIED は NoCard であって「損益ゼロ」ではない
  * 再送鍵は含み損益で変わらず、枚数・SL・脚の価格でだけ変わる
  * 同じ鍵なら Telegram へ送り直さない(3分ごとの写真スパムを作らない)

外部送信は一切行わない(sendPhoto はスタブ)。ブローカー照会もスタブ。
"""
import json
import math
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import position_card  # noqa: E402

ENTRY = 29_600.0
PLAN = {
    "symbol": "MNQU6", "side": "SELL", "qty": 2,
    "entry": ENTRY, "stop": ENTRY + 24.0,
    "targets": [ENTRY - 26.0, ENTRY - 52.0],
    "legs": [{"id": "TP1", "qty": 1, "target": ENTRY - 26.0},
             {"id": "RUNNER", "qty": 1, "target": ENTRY - 52.0}],
}
POSITION = {"symbol": "MNQU6", "side": "SHORT", "qty": 2, "avgEntry": ENTRY,
            "filledAt": "2026-08-28T00:00:00+00:00"}


def bars(n=60, last_t=1_787_000_000, start=ENTRY):
    out = []
    close = start
    for i in range(n):
        o = close
        close = round(start + math.sin(i / 4.6) * 14 - i * 0.4, 2)
        out.append({"time": last_t - (n - 1 - i) * 180, "open": o,
                    "high": max(o, close) + 3, "low": min(o, close) - 3,
                    "close": close, "volume": 100 + i})
    return out


def rung(derived, rung_id):
    return next(r for r in derived["rungs"] if r["id"] == rung_id)


class DerivationTests(unittest.TestCase):
    def test_short_levels_carry_per_leg_money(self):
        d = position_card.derive_open(POSITION, PLAN, ENTRY - 10.0, accounts=2)
        self.assertEqual(d["side"], "SHORT")
        # 含み: 10pt × $2 × 2枚 × 2口座
        self.assertAlmostEqual(d["unrealEach"], 40.0)
        self.assertAlmostEqual(d["unrealTotal"], 80.0)
        # TP1 は 1枚だけ。26pt × $2 × 1枚 × 2口座 = $104
        tp1 = rung(d, "TP1")
        self.assertAlmostEqual(tp1["each"], 52.0)
        self.assertAlmostEqual(tp1["total"], 104.0)
        # runner も 1枚。52pt × $2 × 1枚 × 2口座 = $208
        self.assertAlmostEqual(rung(d, "RUNNER")["total"], 208.0)
        # SL は全枚数。24pt × $2 × 2枚 × 2口座 = −$192
        self.assertAlmostEqual(rung(d, "STOP")["total"], -192.0)
        # 両脚が抜けた合計はフッター用に別で持つ
        self.assertAlmostEqual(d["bankedAll"], 312.0)

    def test_long_mirrors_short(self):
        plan = {**PLAN, "side": "BUY", "stop": ENTRY - 24.0,
                "targets": [ENTRY + 26.0, ENTRY + 52.0],
                "legs": [{"id": "TP1", "qty": 1, "target": ENTRY + 26.0},
                         {"id": "RUNNER", "qty": 1, "target": ENTRY + 52.0}]}
        position = {**POSITION, "side": "LONG"}
        d = position_card.derive_open(position, plan, ENTRY + 10.0, accounts=2)
        self.assertEqual(d["side"], "LONG")
        self.assertAlmostEqual(d["unrealTotal"], 80.0)
        self.assertAlmostEqual(rung(d, "TP1")["total"], 104.0)
        self.assertAlmostEqual(rung(d, "STOP")["total"], -192.0)

    def test_r_multiples_use_the_structural_risk(self):
        d = position_card.derive_open(POSITION, PLAN, ENTRY, accounts=1)
        self.assertAlmostEqual(d["risk"], 24.0)
        self.assertAlmostEqual(rung(d, "STOP")["r"], -1.0)
        self.assertAlmostEqual(rung(d, "TP1")["r"], 26.0 / 24.0, places=4)
        self.assertAlmostEqual(rung(d, "RUNNER")["r"], 52.0 / 24.0, places=4)

    def test_distance_to_each_level_is_signed_toward_the_level(self):
        d = position_card.derive_open(POSITION, PLAN, ENTRY - 10.0, accounts=1)
        self.assertAlmostEqual(rung(d, "TP1")["away"], 16.0)     # あと16pt
        self.assertAlmostEqual(rung(d, "STOP")["away"], 34.0)    # 34pt 離れている


class RenderTests(unittest.TestCase):
    def test_png_has_the_declared_size(self):
        from PIL import Image

        png = position_card.render_png(POSITION, PLAN, ENTRY - 10.0,
                                       bars=bars(), accounts=2)
        self.assertTrue(png.startswith(b"\x89PNG"))
        with Image.open(__import__("io").BytesIO(png)) as img:
            self.assertEqual(img.size, (position_card.W, position_card.H))

    def test_render_survives_a_single_target_plan(self):
        plan = {**PLAN, "targets": [ENTRY - 26.0],
                "legs": [{"id": "TP1", "qty": 2, "target": ENTRY - 26.0}]}
        png = position_card.render_png(POSITION, plan, ENTRY - 10.0,
                                       bars=bars(), accounts=1)
        self.assertTrue(png.startswith(b"\x89PNG"))

    def test_caption_names_every_level_with_money(self):
        d = position_card.derive_open(POSITION, PLAN, ENTRY - 10.0, accounts=2)
        cap = position_card.caption(d)
        for token in ("TP1", "TP2", "SL", "+$104.00", "+$208.00", "−$192.00"):
            self.assertIn(token, cap)
        self.assertLess(len(cap), 400)


class FingerprintTests(unittest.TestCase):
    def test_price_moves_do_not_change_the_key(self):
        a = position_card.card_fingerprint(
            position_card.derive_open(POSITION, PLAN, ENTRY - 3.0, accounts=2))
        b = position_card.card_fingerprint(
            position_card.derive_open(POSITION, PLAN, ENTRY - 21.0, accounts=2))
        self.assertEqual(a, b)

    def test_tp1_fill_and_breakeven_stop_change_the_key(self):
        base = position_card.card_fingerprint(
            position_card.derive_open(POSITION, PLAN, ENTRY, accounts=2))
        runner = position_card.card_fingerprint(
            position_card.derive_open({**POSITION, "qty": 1}, PLAN, ENTRY, accounts=2))
        self.assertNotEqual(base, runner)
        moved = position_card.card_fingerprint(
            position_card.derive_open(POSITION, {**PLAN, "stop": ENTRY}, ENTRY, accounts=2))
        self.assertNotEqual(base, moved)


class CollectTests(unittest.TestCase):
    def _broker(self, rows):
        stub = type("Stub", (), {})()
        stub.adapters = staticmethod(lambda: [])
        stub.query_position = staticmethod(lambda account=None: rows[account])
        return stub

    def test_flat_accounts_raise_no_card(self):
        rows = {"A": {"verified": True, "qty": 0}, "B": {"verified": True, "qty": 0}}
        with patch.dict(sys.modules, {"broker_status": self._broker(rows)}):
            with self.assertRaises(position_card.NoCard) as cm:
                position_card.collect(accounts=["A", "B"])
        self.assertIn("建玉", str(cm.exception))

    def test_unverified_is_not_treated_as_flat(self):
        rows = {"A": {"verified": False, "qty": 0}}
        with patch.dict(sys.modules, {"broker_status": self._broker(rows)}):
            with self.assertRaises(position_card.NoCard) as cm:
                position_card.collect(accounts=["A"])
        self.assertIn("UNVERIFIED", str(cm.exception))

    def test_different_entries_are_not_merged_into_one_card(self):
        rows = {"A": {"verified": True, "qty": 2, "avgEntry": 29_600.0, "side": "SHORT"},
                "B": {"verified": True, "qty": 2, "avgEntry": 29_602.0, "side": "SHORT"}}
        with patch.dict(sys.modules, {"broker_status": self._broker(rows)}):
            with self.assertRaises(position_card.NoCard):
                position_card.collect(accounts=["A", "B"])


class SendPathTests(unittest.TestCase):
    def setUp(self):
        import monitor_publish

        self.mp = monitor_publish
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "sent.json")
        self._saved = monitor_publish.POSITION_CARD_SENT
        monitor_publish.POSITION_CARD_SENT = self.path

    def tearDown(self):
        self.mp.POSITION_CARD_SENT = self._saved

    def _render(self, fingerprint):
        derived = position_card.derive_open(POSITION, PLAN, ENTRY, accounts=2)
        derived = dict(derived, _fp=fingerprint)
        return (b"\x89PNGfake", "caption", derived)

    def test_same_contract_is_not_sent_twice(self):
        calls = []
        with patch("position_card.render_open_card", side_effect=lambda **kw: self._render("X")), \
             patch("position_card.card_fingerprint", side_effect=lambda d: d["_fp"]), \
             patch("telegram_bot.send_photo",
                   side_effect=lambda cfg, png, caption="", reply_markup=None:
                   calls.append(caption) or {"ok": True}):
            self.mp._send_position_card({}, {"price": ENTRY, "position": "SHORT 2x2"})
            self.mp._send_position_card({}, {"price": ENTRY - 8.0, "position": "SHORT 2x2"})
        self.assertEqual(len(calls), 1)
        with open(self.path, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["fingerprint"], "X")

    def test_contract_change_sends_again(self):
        seen = iter(["X", "Y"])
        calls = []
        with patch("position_card.render_open_card",
                   side_effect=lambda **kw: self._render(next(seen))), \
             patch("position_card.card_fingerprint", side_effect=lambda d: d["_fp"]), \
             patch("telegram_bot.send_photo",
                   side_effect=lambda cfg, png, caption="", reply_markup=None:
                   calls.append(caption) or {"ok": True}):
            self.mp._send_position_card({}, {"price": ENTRY, "position": "SHORT 2x2"})
            self.mp._send_position_card({}, {"price": ENTRY, "position": "SHORT 2x2"})
        self.assertEqual(len(calls), 2)

    def test_no_position_clears_the_record_and_sends_nothing(self):
        calls = []
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump({"fingerprint": "OLD"}, fh)
        with patch("position_card.render_open_card",
                   side_effect=position_card.NoCard("保有中の建玉が無い")), \
             patch("telegram_bot.send_photo",
                   side_effect=lambda *a, **k: calls.append(1) or {"ok": True}):
            self.mp._send_position_card({}, {"price": ENTRY, "position": "SHORT 2x2"})
        self.assertEqual(calls, [])
        self.assertFalse(os.path.exists(self.path))

    def test_flat_cycle_does_not_query_the_broker_again(self):
        # bundle["position"] が無いサイクルでは描画にも入らない。大半のサイクルは
        # FLAT なので、ここで口座数ぶんの REST を撃つと毎回の無駄になる。
        rendered = []
        with patch("position_card.render_open_card",
                   side_effect=lambda **kw: rendered.append(1) or self._render("X")):
            self.mp._send_position_card({}, {"price": ENTRY})
        self.assertEqual(rendered, [])

    def test_render_failure_never_raises(self):
        with patch("position_card.render_open_card", side_effect=RuntimeError("boom")):
            self.assertIsNone(self.mp._send_position_card({}, {"price": ENTRY, "position": "SHORT 2x2"}))


if __name__ == "__main__":
    unittest.main(verbosity=2)

#!/usr/bin/env python3
"""MSNRの否定判定もカードへ配送されることを固定する。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import msnr_gate
import monitor_publish


def level(label, price, role):
    return {
        "label": label,
        "price": price,
        "tier": 3,
        "confluence": 1,
        "dynamic": False,
        "freshness": "FRESH",
        "role": role,
        "chains": [],
        "promotion": {
            "BUY": {"allowed": False, "grade": None, "blockers": ["NO_CHAIN"]},
            "SELL": {"allowed": False, "grade": None, "blockers": ["NO_CHAIN"]},
        },
        "rrPotential": {"BUY": None, "SELL": None},
    }


class MsnrDeliveryTests(unittest.TestCase):
    def base_result(self, levels):
        return {
            "at": "2026-08-24T18:42:00+00:00",
            "noiseFloor": 19.75,
            "rotation": {"signals": 3, "negations": 0, "verdict": "OK"},
            "levels": levels,
            "summary": "MSNR: 連鎖なし",
            "decision": {"model": "FLAT", "side": "FLAT", "state": "WATCH"},
        }

    def test_nearest_no_chain_level_is_delivered(self):
        result = self.base_result([
            level("New York High", 29247.0, "RESISTANCE"),
            level("New York Low", 28947.75, "SUPPORT"),
        ])
        card = msnr_gate.build_card(result, price=29191.25)
        self.assertIn("msnr", card)
        self.assertEqual(card["msnr"]["label"], "New York High")
        self.assertEqual(card["msnr"]["side"], "SELL")
        self.assertIsNone(card["msnr"]["chainState"])
        self.assertFalse(card["msnr"]["allowed"])
        self.assertEqual(card["msnr"]["blockers"], ["NO_CHAIN"])

    def test_no_levels_is_an_explicit_nonblocking_result(self):
        card = msnr_gate.build_card(self.base_result([]), price=29191.25)
        self.assertIn("msnr", card)
        self.assertFalse(card["msnr"]["allowed"])
        self.assertEqual(card["msnr"]["blockers"], ["NO_LEVELS"])

    def test_msnr_change_is_not_suppressed_as_duplicate(self):
        base = {"at": "2026-08-24T18:42:00+00:00", "price": 29191.25,
                "regime": "MX", "scenarios": {}}
        delivered = {**base, "evaluation": {
            "msnr": {"allowed": False, "blockers": ["NO_CHAIN"]}}}
        self.assertNotEqual(
            monitor_publish.fingerprint(base),
            monitor_publish.fingerprint(delivered),
        )
        self.assertEqual(
            monitor_publish.fingerprint(delivered),
            monitor_publish.fingerprint(delivered),
        )


if __name__ == "__main__":
    unittest.main()

# -*- coding: utf-8 -*-
"""R52: engine が order.py へ渡す token/key/hash は `--opt=value` の 1 要素にする。

2026-09-05 01:54、`secrets.token_urlsafe(32)` が `-` で始まる claim token を返し、
`["--claim-token", "-abc…"]` と別要素で渡したため argparse が値をオプションと誤認、
「argument --claim-token: expected one argument」で dry-run が落ちて A+ 候補を HALT した。
`--claim-token=-abc…` なら argparse は値として読む。

    python tests/test_r52_argv_tokens.py
"""
import argparse
import os
import sys
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
import autotrade_engine as ae  # noqa: E402

PLAN = {"side": "SELL", "qty": 2, "initialStop": 29627.25, "entry": 29585.0,
        "legs": [{"id": "TP1", "qty": 1, "target": 29498.5}, {"id": "RUNNER", "qty": 1, "target": 29350.75}],
        "symbol": "MNQU6", "planVersion": "R19-ICT-SPLIT-1", "accountScope": ["ACC-1"], "ultra": False}
DASH_TOKEN = "-" + "x" * 42          # token_urlsafe が実際に返した形(先頭 '-')
UNDERSCORE_TOKEN = "_" + "y" * 42


def order_like_parser():
    """order.py の該当オプションだけを持つ同形の parser(値の解釈規則は同じ)。"""
    p = argparse.ArgumentParser()
    for name in ("--side", "--qty", "--sl", "--split-tp", "--symbol", "--entry", "--last",
                 "--accounts", "--ultra-drawdown", "--entry-key", "--claim-token", "--intent-hash",
                 "--plan-version", "--tp", "--position-generation", "--account",
                 "--management-key", "--management-token", "--management-intent-hash"):
        p.add_argument(name)
    for flag in ("--market", "--ultra", "--modify"):
        p.add_argument(flag, action="store_true")
    return p


class ArgvTokenTests(unittest.TestCase):
    def test_entry_tokens_starting_with_dash_parse_as_values(self):
        args = ae._command_for_entry(PLAN, {}, "ENTRY:" + "a" * 64, DASH_TOKEN, "xi_" + "b" * 64)
        self.assertIn(f"--claim-token={DASH_TOKEN}", args)
        self.assertNotIn(DASH_TOKEN, args, "token を裸の要素で渡さない")
        parsed = order_like_parser().parse_args(args)
        self.assertEqual(parsed.claim_token, DASH_TOKEN)
        self.assertEqual(parsed.entry_key, "ENTRY:" + "a" * 64)
        self.assertEqual(parsed.intent_hash, "xi_" + "b" * 64)
        self.assertEqual(parsed.plan_version, "R19-ICT-SPLIT-1")

    def test_old_form_reproduces_the_failure(self):
        # 回帰の再現: 別要素で渡すと argparse が落ちる(修正前の形)
        old = ["--side", "sell", "--claim-token", DASH_TOKEN]
        with self.assertRaises(SystemExit):
            order_like_parser().parse_args(old)

    def test_modify_tokens_starting_with_underscore_or_dash(self):
        action = {"action": "MODIFY", "qty": 1, "sl": 29600.0, "tp": 29350.75, "reason": "breakeven"}
        claim = {"managementKey": "MANAGEMENT:" + "c" * 64, "claimToken": UNDERSCORE_TOKEN,
                 "managementIntentHash": "mi_" + "d" * 64}
        args = ae._command_for_modify(PLAN, action, "PG:1:POS:" + "e" * 64, claim, account="ACC-1")
        self.assertIn(f"--management-token={UNDERSCORE_TOKEN}", args)
        parsed = order_like_parser().parse_args(args)
        self.assertEqual(parsed.management_token, UNDERSCORE_TOKEN)
        self.assertEqual(parsed.management_key, "MANAGEMENT:" + "c" * 64)
        self.assertTrue(parsed.modify)
        dash_claim = {**claim, "claimToken": DASH_TOKEN}
        parsed = order_like_parser().parse_args(
            ae._command_for_modify(PLAN, action, "PG:1:POS:" + "e" * 64, dash_claim, account="ACC-1"))
        self.assertEqual(parsed.management_token, DASH_TOKEN)


if __name__ == "__main__":
    unittest.main(verbosity=1)

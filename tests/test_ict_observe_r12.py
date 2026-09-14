# -*- coding: utf-8 -*-
"""R12観測台帳は完了済みの実績だけをOOS入力に渡す。"""
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
import ict_observe  # noqa: E402
import oos_ablation  # noqa: E402


def check(label, condition, detail=""):
    if not condition:
        raise AssertionError(f"{label}: {detail}")
    print(f"OK   {label}")


bundle = {
    "at": "2026-08-21T21:00:00+09:00", "price": 118,
    "snapshot": {"levels": [{"label": "TEST", "price": 110}],
                 "bars3m": [{"t": 1786970000 + index * 180, "o": 100 + index,
                              "h": 106 + index, "l": 98 + index, "c": 102 + index}
                             for index in range(16)]},
}
check("未決済はOOSに入れない", ict_observe.oos_record(bundle) is None)

# 実際の評価を通す必要がないOOS変換境界は、決定をモックせずに欠落を拒否する。
incomplete = {**bundle, "outcome": {"filled": True, "realizedR": 1.0}}
check("結果のコスト/MFE/MAE欠落を拒否", ict_observe.oos_record(incomplete) is None)

original = ict_observe.msnr_gate.evaluate
try:
    ict_observe.msnr_gate.evaluate = lambda _: {"decision": {
        "decisionId": "r12-observe-1", "entry": 100, "stop": 90, "model": "TURTLE_SOUP_REVERSAL",
        "side": "BUY", "evidence": ["ICT_LOCATION"], "targetLabels": ["DOL"],
        "rangeAnchor": {"valid": True}, "cvdHealth": {"available": True},
    }}
    complete = {**bundle, "outcome": {"filled": True, "realizedR": 1.5,
                                        "costR": 0.1, "mfeR": 2.0, "maeR": -0.4}}
    row = ict_observe.oos_record(complete)
    normalized = oos_ablation.normalize(row, 1, legacy_mode=True)
    check("unversioned observation is isolated as legacy",
          normalized["versions"]["setupVersion"] == "R12-LEGACY", normalized)
    check("完成実績は固定entry/SLとともにOOSへ渡す",
          row and row["tradeId"] == "r12-observe-1" and row["outcome"]["costR"] == 0.1, row)
finally:
    ict_observe.msnr_gate.evaluate = original
print("ALL PASS (test_ict_observe_r12)")

# -*- coding: utf-8 -*-
"""R57: result.chart(根拠チャート)の組み立て。実データだけを使い、上限を守る。

    python tests/test_result_context.py
"""
import json
import os
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
import result_context as rc  # noqa: E402

PASS = [0]
FAIL = [0]


def check(label, condition, detail=""):
    if condition:
        PASS[0] += 1
        print(f"  OK   {label}")
    else:
        FAIL[0] += 1
        print(f"  NG   {label}  {detail}")


OPEN_TS = 1788551487   # 2026-09-04T19:51:27Z (04:51:27 JST)
CLOSE_TS = 1788551700  # 19:55:00Z

with tempfile.TemporaryDirectory() as tmp:
    raw = os.path.join(tmp, "bars3m.json")
    t0 = OPEN_TS - 60 * 60
    bars = [{"time": t0 + i * 180, "open": 29560 + i * 0.25, "high": 29565 + i * 0.25,
             "low": 29555 + i * 0.25, "close": 29562 + i * 0.25, "volume": 10} for i in range(40)]
    with open(raw, "w", encoding="utf-8") as fh:
        json.dump({"success": True, "bars": bars}, fh)
    audit_dir = os.path.join(tmp, "audit")
    os.makedirs(audit_dir)
    with open(os.path.join(audit_dir, "monitor_cycle_0451.json"), "w", encoding="utf-8") as fh:
        json.dump({"at": "2026-09-04T19:51:05+00:00",
                   "evaluation": {"decision": {"penalties": ["HTF_CONFLICT", "HRLR"], "targetR": [1.92, 13.8]},
                                  "volGate": {"ratio": 0.18, "noise": 11.0},
                                  "sessionGate": {"label": "ラストアワー"}},
                   "snapshot": {"levels": [{"label": "C: VAH", "price": 29566.7}, {"label": "P: VAH", "price": 29585.0},
                                           {"label": "Weekly open", "price": 29535.0}],
                                "htfContext": {"frames": {"1h": {"structure": "UP"}, "4h": {"structure": "MIXED"}}}}}, fh)
    with open(os.path.join(audit_dir, "monitor_cycle_0459.json"), "w", encoding="utf-8") as fh:
        json.dump({"at": "2026-09-04T19:59:01+00:00", "snapshot": {"levels": [{"label": "C: VAH", "price": 29566.7}]}}, fh)
    pattern = os.path.join(audit_dir, "monitor_cycle_*.json")
    plan = {"tp1": 29535.0, "finalTarget": 29350.75, "decisionEvidence": ["CVD_ALIGNED", "VP_ACCEPTED"]}

    chart = rc.build_chart("SHORT", 29570.5, 29582.75, "2026-09-04T19:51:27+00:00", "2026-09-04T19:55:00+00:00",
                           plan, raw_bars_path=raw, audit_pattern=pattern)
    check("version / tf", chart["version"] == "NQX-RESULT-CHART/1" and chart["tf"] == 180)
    check("足は建玉 45 分前〜決済 15 分後(実データのみ)",
          chart["bars"] and chart["bars"][0][0] >= OPEN_TS - 45 * 60 - 180 and chart["bars"][-1][0] <= CLOSE_TS + 15 * 60,
          str((chart["bars"][0][0] - OPEN_TS, chart["bars"][-1][0] - CLOSE_TS)))
    check("足は [t,o,h,l,c] の 5 要素", all(len(b) == 5 for b in chart["bars"]))
    check("水準は VP の 6 種だけ(Weekly open は落とす)",
          [l["label"] for l in chart["levels"]] == ["C: VAH", "P: VAH"], str(chart["levels"]))
    check("ターゲットと根拠タグ・減点", chart["tp1"] == 29535.0 and chart["tp2"] == 29350.75
          and chart["evidence"] == ["CVD_ALIGNED", "VP_ACCEPTED"] and chart["penalties"] == ["HTF_CONFLICT", "HRLR"])
    check("HTF / ボラ / セッション", chart["htf"] == {"1h": "UP", "4h": "MIXED"} and chart["volRatio"] == 0.18
          and chart["noise"] == 11.0 and chart["session"] == "NY PM", str(chart))
    check("出所", chart["source"] == "raw")

    # 上限: 長い保有でも MAX_BARS に収める(保有中を優先して残す)
    long_bars = [{"time": OPEN_TS - 3600 + i * 180, "open": 1, "high": 2, "low": 0, "close": 1} for i in range(200)]
    with open(raw, "w", encoding="utf-8") as fh:
        json.dump({"bars": long_bars}, fh)
    late_close = "2026-09-05T03:00:00+00:00"
    big = rc.build_chart("LONG", 1, 1, "2026-09-04T19:51:27+00:00", late_close, {}, raw_bars_path=raw, audit_pattern=pattern)
    check("足は MAX_BARS 以下", len(big["bars"]) <= rc.MAX_BARS, str(len(big["bars"])))

    # 足が無ければ bars 空、文脈だけ(推測しない)
    none = rc.build_chart("SHORT", 1, 1, "2026-01-01T00:00:00+00:00", "2026-01-01T00:10:00+00:00", {},
                          raw_bars_path=os.path.join(tmp, "missing.json"), audit_pattern=os.path.join(tmp, "x_*.json"))
    check("足が無ければ bars は空で source none", none["bars"] == [] and none["source"] == "none")
    check("時刻が壊れていれば None", rc.build_chart("SHORT", 1, 1, "bad", "worse", {}, raw_bars_path=raw, audit_pattern=pattern) is None)

    # attach は result を壊さない
    result = {"side": "SHORT", "entry": 29570.5, "exit": 29582.75,
              "openedAt": "2026-09-04T19:51:27+00:00", "closedAt": "2026-09-04T19:55:00+00:00"}
    with open(raw, "w", encoding="utf-8") as fh:
        json.dump({"success": True, "bars": bars}, fh)
    rc._BUNDLE_CACHE.clear()
    out = rc.attach(result, plan, raw_bars_path=raw, audit_pattern=pattern)
    check("attach で chart が付く", out is result and isinstance(result.get("chart"), dict) and result["chart"]["bars"])
    check("attach は壊れた入力でも落ちない", rc.attach({"side": "SHORT"}, None) == {"side": "SHORT"})

print()
print(f"合計: PASS {PASS[0]} / FAIL {FAIL[0]}")
sys.exit(1 if FAIL[0] else 0)

# -*- coding: utf-8 -*-
"""モデル別スコアカード(model_scorecard.py)の検証。

ネットワークを使わない。台帳は一時ディレクトリに作る。
ここが崩れると「モデル別PF」という将来の宣言の土台が信用できなくなる。

    python tests/test_model_scorecard.py
"""
import json
import os
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

for _stream in ("stdout", "stderr"):
    _file = getattr(sys, _stream, None)
    if _file is not None and hasattr(_file, "reconfigure"):
        try:
            _file.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

import model_scorecard  # noqa: E402

PASS = [0]
FAIL = [0]


def check(label, condition, detail=""):
    if condition:
        PASS[0] += 1
        print(f"  OK   {label}")
    else:
        FAIL[0] += 1
        print(f"  NG   {label}  {detail}")


def result(i, **over):
    base = {
        "resultId": f"rs_{i:020d}"[:23],
        "side": "SHORT", "symbol": "MNQU6", "qty": 2,
        "entry": 30126.0, "exit": 30098.5, "stop": 30147.0,
        "pointValue": 2.0, "openedAt": "2026-08-28T13:00:00+00:00",
        "closedAt": "2026-08-28T13:30:00+00:00",
        "mode": "LIVE", "model": "TURTLE_SOUP_REVERSAL", "grade": "A+",
        "scenarioId": "sc-1", "accountId": "ACC-A",
    }
    base.update(over)
    return base


print("=" * 68)
print("1. classify の損益・R・勝敗")
print("=" * 68)

row = model_scorecard.classify(result(1))
check("SHORT 勝ち: (30126-30098.5)×2pt×2枚 = +110", row["pnlNet"] == 110.0, row)
check("R = 110 / (21pt×2×2) = +1.31", abs(row["rMultiple"] - 1.31) < 0.005, row["rMultiple"])
check("outcome WIN", row["outcome"] == "WIN")
check("model/grade が乗る", row["model"] == "TURTLE_SOUP_REVERSAL" and row["grade"] == "A+")

row = model_scorecard.classify(result(2, side="LONG", entry=30000.0, exit=29980.0, stop=29979.0))
check("LONG 負け: -20pt×2×2 = -80", row["pnlNet"] == -80.0, row)
check("outcome LOSS", row["outcome"] == "LOSS")

row = model_scorecard.classify(result(3, exit=30125.0, fees=3.5))
check("fees は net から引かれる: (1pt×2×2)-3.5 = +0.5", row["pnlNet"] == 0.5, row)
check("コスト床未満(1pt < 2pt)は FLAT", row["outcome"] == "FLAT")

check("壊れた result は None", model_scorecard.classify({"resultId": "x"}) is None)

print("=" * 68)
print("2. 紐付けの優先順位")
print("=" * 68)

plans = {"sc-9": {"model": "VP80_REVERSION", "grade": "A"}}
row = model_scorecard.classify(result(4, model=None, grade=None, scenarioId="sc-9"), plans=plans)
check("result.model 無し → 台帳プランから補完", row["model"] == "VP80_REVERSION" and row["grade"] == "A")
row = model_scorecard.classify(result(5, model="OTE_FVG_PULLBACK", scenarioId="sc-9"), plans=plans)
check("result.model があれば台帳より優先", row["model"] == "OTE_FVG_PULLBACK")
row = model_scorecard.classify(result(6, model=None, grade=None, scenarioId=None), plans=plans)
check("どちらも無ければ UNATTRIBUTED", row["model"] == "UNATTRIBUTED")

print("=" * 68)
print("3. 台帳の追記と重複排除")
print("=" * 68)

with tempfile.TemporaryDirectory() as tmp:
    path = os.path.join(tmp, "scorecard.jsonl")
    first = model_scorecard.record(result(7), path=path, plans={})
    check("1件目は追記される", first is not None)
    again = model_scorecard.record(result(7), path=path, plans={})
    check("同じ resultId は追記されない", again is None)
    rows = model_scorecard.load_rows(path)
    check("台帳に1行だけ", len(rows) == 1)

    appended = model_scorecard.ingest([result(8), result(9, side="LONG",
                                                        entry=30000.0, exit=29980.0,
                                                        stop=29979.0)], path=path)
    check("ingest で2件追加", len(appended) == 2)

print("=" * 68)
print("4. summarize(PF は損失が出るまで None)")
print("=" * 68)

rows = [model_scorecard.classify(result(10)),
        model_scorecard.classify(result(11, resultId="rs_11")),
        model_scorecard.classify(result(12, side="LONG", entry=30000.0,
                                        exit=29980.0, stop=29979.0))]
rows = [r for r in rows if r]
summary = model_scorecard.summarize(rows)
soup = summary["models"]["TURTLE_SOUP_REVERSAL"]
check("SOUP: N=3 W=2 L=1", soup["n"] == 3 and soup["wins"] == 2 and soup["losses"] == 1)
check("SOUP: PF = 220/80 = 2.75", soup["pf"] == 2.75, soup)
check("net = 110+110-80 = 140", soup["netTotal"] == 140.0)

only_win = model_scorecard.summarize([model_scorecard.classify(result(13))])
check("損失ゼロの PF は None(∞を発明しない)",
      only_win["models"]["TURTLE_SOUP_REVERSAL"]["pf"] is None)
check("宣言注意書きがある", "宣言しない" in summary["declarationNote"])

line = model_scorecard.summary_line(summary)
check("summary_line が1行で出る", line.startswith("scorecard: ") and "N=3" in line, line)

print("=" * 68)
total = PASS[0] + FAIL[0]
if FAIL[0]:
    print(f"FAILED {FAIL[0]} / {total}")
    sys.exit(1)
print(f"ALL PASS ({total})")
sys.exit(0)

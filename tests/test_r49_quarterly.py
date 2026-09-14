# -*- coding: utf-8 -*-
"""R49: Quarterly Theory の通電(Judas/true open 奪還による方向導出)の検証。

原則: 時刻は位相を決めるが方向は決めない。方向は true open に対する
価格の観測(誘導→奪還)からだけ導く。票になるのは 90分クォーターの
D 位相に居るときだけで、単独では武装を動かせない(±2票の既存しきい値)。

    python tests/test_r49_quarterly.py
"""
import os
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

for _stream in ("stdout", "stderr"):
    _file = getattr(sys, _stream, None)
    if _file is not None and hasattr(_file, "reconfigure"):
        try:
            _file.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

import msnr_gate          # noqa: E402
import quarterly_theory   # noqa: E402
import strategy_models    # noqa: E402

NY = ZoneInfo("America/New_York")
PASS = [0]
FAIL = [0]


def check(label, condition, detail=""):
    if condition:
        PASS[0] += 1
        print(f"  OK   {label}")
    else:
        FAIL[0] += 1
        print(f"  NG   {label}  {detail}")


def ny(y, mo, d, h, mi):
    return datetime(y, mo, d, h, mi, tzinfo=NY)


DAY_OPEN = ny(2026, 8, 27, 18, 0)          # 取引日 8/28 の開始
TRUE_OPEN_AT = ny(2026, 8, 28, 0, 0)


def gen_bars(start_dt, minutes, price_fn):
    bars = []
    for i in range(0, minutes, 3):
        p = float(price_fn(i))
        bars.append({"t": int((start_dt + timedelta(minutes=i)).timestamp()),
                     "o": p, "h": p + 2.0, "l": p - 2.0, "c": p})
    return bars


def judas_sell_bars(until_minutes=585):
    """00:00 open 30100 → London で 30125 へ誘導 → 30098 へ奪還。

    足レンジは全て 4pt(±2)なので minExc = median = 4pt。奪還後の安値
    30096 は trueOpen−minExc(30096)を割らない → 片側掃引のまま。
    """
    def price(i):
        if i == 0:
            return 30100.0
        if i < 60:
            return 30102.0
        if 60 <= i < 120:
            return 30125.0        # h=30127 が trueOpen+minExc(30104) を超える
        return 30098.0            # 奪還: close < trueOpen − 1tick
    return gen_bars(TRUE_OPEN_AT, until_minutes, price)


print("=" * 68)
print("1. judas_from_true_open(方向は価格からのみ)")
print("=" * 68)

epoch = ny(2026, 8, 28, 9, 45).timestamp()
judas = quarterly_theory.judas_from_true_open(judas_sell_bars(), DAY_OPEN, epoch)
check("上へ誘導→奪還で SELL", judas.get("direction") == "SELL", judas)
check("trueOpen が 00:00 の open", judas.get("trueOpen") == 30100.0, judas)
check("誘導幅を保存(30127-30100=27)", judas.get("sweptUpPt") == 27.0, judas)
check("最小超過幅=当日レンジ中央値(4pt)", judas.get("minExcursionPt") == 4.0, judas)
check("片側掃引なら bothSides=False", judas.get("bothSides") is False, judas)

# 奪還しない(close が trueOpen の上に留まる)→ 方向を主張しない
no_reclaim = gen_bars(TRUE_OPEN_AT, 585,
                      lambda i: 30100.0 if i == 0 else 30125.0)
judas2 = quarterly_theory.judas_from_true_open(no_reclaim, DAY_OPEN, epoch)
check("奪還が無ければ direction=None", judas2.get("direction") is None, judas2)
check("普通の足のヒゲだけでは掃引と呼ばない(下側)",
      judas2.get("sweptDownPt") == 0.0, judas2)

# 下へ誘導→上へ奪還 → BUY
buy_bars = gen_bars(TRUE_OPEN_AT, 585,
                    lambda i: 30100.0 if i == 0
                    else (30075.0 if 60 <= i < 120 else 30120.0))
judas3 = quarterly_theory.judas_from_true_open(buy_bars, DAY_OPEN, epoch)
check("下へ誘導→奪還で BUY", judas3.get("direction") == "BUY", judas3)

early = quarterly_theory.judas_from_true_open(
    judas_sell_bars(60), DAY_OPEN, ny(2026, 8, 27, 23, 0).timestamp())
check("London 開始前は観測しない", early.get("available") is False
      and early.get("reason") == "LONDON_NOT_STARTED", early)

print("=" * 68)
print("2. observe() が bias を運ぶ(観測が成立した日だけ)")
print("=" * 68)

bundle = {"at": ny(2026, 8, 28, 9, 45).isoformat()}
observed = quarterly_theory.observe(bundle, judas_sell_bars(), price=30098.0)
check("bias=SELL / basis=TRUE_OPEN_RECLAIM",
      observed.get("bias") == "SELL"
      and observed.get("biasBasis") == "TRUE_OPEN_RECLAIM", observed)
check("judas ブロックが載る", (observed.get("judas") or {}).get("direction") == "SELL")
check("09:45 は NY セッションの D クォーター(09:00–10:30)",
      observed.get("session") == "NewYork" and observed.get("stage") == "D", observed)

observed2 = quarterly_theory.observe(bundle, no_reclaim, price=30125.0)
check("奪還が無い日は bias を付けない", "bias" not in observed2, observed2.get("bias"))

print("=" * 68)
print("3. detect_quarterly_theory が票になる条件(D位相のみ)")
print("=" * 68)

detected = strategy_models.detect_quarterly_theory(bundle, judas_sell_bars(), 30098.0)
check("D位相+Judas成立で CONFIRMED",
      detected.get("status") == "CONFIRMED" and detected.get("valid") is True
      and detected.get("direction") == "SELL", detected)
check("票決関数が SELL 票と認める",
      strategy_models._vote_direction(detected) == "SELL")

bundle_m = {"at": ny(2026, 8, 28, 7, 30).isoformat()}   # NY の M クォーター
detected_m = strategy_models.detect_quarterly_theory(
    bundle_m, judas_sell_bars(450), 30098.0)
check("D位相以外では票にならない(WATCH)",
      detected_m.get("status") == "WATCH" and detected_m.get("valid") is False,
      detected_m.get("status"))

detected_none = strategy_models.detect_quarterly_theory(bundle, no_reclaim, 30125.0)
check("Judas不成立なら D位相でも票にならない",
      detected_none.get("valid") is False, detected_none.get("status"))

print("=" * 68)
print("4. msnr_gate の記録タグと文脈")
print("=" * 68)

sweep_t = int(ny(2026, 8, 28, 2, 0).timestamp())        # London = M 位相
chain = {"type": "SWEEP", "side": "SELL", "sweepBarT": sweep_t}
level = {"label": "Asia High", "price": 30127.0}
bars = judas_sell_bars()
matrix = {"models": {"quarterly": {"direction": "SELL"}}}
tags = msnr_gate.feature_tags(chain, level, bars, {}, matrix)
check("M位相スイープに QT_M_PHASE_SWEEP", "QT_M_PHASE_SWEEP" in tags, tags)
check("方向一致で QT_JUDAS_ALIGNED", "QT_JUDAS_ALIGNED" in tags, tags)

tags_buy = msnr_gate.feature_tags(
    {"type": "SWEEP", "side": "BUY", "sweepBarT": sweep_t}, level, bars, {}, matrix)
check("方向不一致なら JUDAS タグ無し", "QT_JUDAS_ALIGNED" not in tags_buy)

ny_sweep = int(ny(2026, 8, 28, 8, 0).timestamp())       # NY 90分クォーターの M(07:30–09:00)
tags_ny = msnr_gate.feature_tags(
    {"type": "SWEEP", "side": "SELL", "sweepBarT": ny_sweep}, level, bars, {}, matrix)
check("90分クォーターの M でもタグが付く", "QT_M_PHASE_SWEEP" in tags_ny, tags_ny)

ict = {"session": msnr_gate.ict_session(ny(2026, 8, 28, 9, 45).isoformat())}
context = msnr_gate.decision_context({"at": ny(2026, 8, 28, 9, 45).isoformat()}, ict)
check("decision_context に位相グリッド",
      (context.get("quarterly") or {}).get("session") == "NewYork"
      and context["quarterly"]["quarterPhase"] == "D", context.get("quarterly"))

print("=" * 68)
total = PASS[0] + FAIL[0]
if FAIL[0]:
    print(f"FAILED {FAIL[0]} / {total}")
    sys.exit(1)
print(f"ALL PASS ({total})")
sys.exit(0)

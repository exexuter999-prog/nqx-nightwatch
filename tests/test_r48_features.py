# -*- coding: utf-8 -*-
"""R48: 定義の状態分離と統計タグ(msnr_gate)の検証。

判定・採点へ影響しない「記録専用の特徴量」なので、テストの主眼は
(1) 分類が定義どおりであること (2) 判定入力(eligible等)が変わらないこと。

    python tests/test_r48_features.py
"""
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

for _stream in ("stdout", "stderr"):
    _file = getattr(sys, _stream, None)
    if _file is not None and hasattr(_file, "reconfigure"):
        try:
            _file.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

import msnr_gate  # noqa: E402

PASS = [0]
FAIL = [0]


def check(label, condition, detail=""):
    if condition:
        PASS[0] += 1
        print(f"  OK   {label}")
    else:
        FAIL[0] += 1
        print(f"  NG   {label}  {detail}")


def bar(t, o, h, l, c):
    return {"t": t, "o": o, "h": h, "l": l, "c": c}


print("=" * 68)
print("1. FVG の arrivalState(ヒゲ接触と未到達の分離)")
print("=" * 68)

base = [
    bar(0, 99.5, 100.0, 99.0, 99.5),          # a: h=100 → gap lo
    bar(180, 99.5, 104.5, 99.4, 104.0),        # impulse(body 4.5)
    bar(360, 104.0, 105.0, 101.5, 104.0),      # c: l=101.5 → gap hi
]
untouched = base + [bar(540, 104.0, 104.5, 102.0, 104.2)]
touched = base + [bar(540, 104.0, 104.5, 101.0, 104.2)]   # l=101.0 がギャップ内へ

fvg = msnr_gate.fvg_scan(untouched, price=104.2, tol=1.0, noise=1.0)
check("未到達は UNTOUCHED", fvg["BULL"] and fvg["BULL"][0]["arrivalState"] == "UNTOUCHED",
      fvg)
untouched_eligible = fvg["BULL"][0]["eligible"]

fvg = msnr_gate.fvg_scan(touched, price=104.2, tol=1.0, noise=1.0)
check("ヒゲ接触は WICK_TOUCHED", fvg["BULL"] and fvg["BULL"][0]["arrivalState"] == "WICK_TOUCHED",
      fvg)
check("eligible 判定は arrivalState で変わらない(判定不変)",
      fvg["BULL"][0]["eligible"] == untouched_eligible)

print("=" * 68)
print("2. Silver Bullet 窓(3窓を別ラベルで・合算しない)")
print("=" * 68)

session = msnr_gate.ict_session("2026-08-28T10:30:00-04:00")
check("10:30 ET は AM_SB", session["silverBullet"] == "AM_SB", session)
session = msnr_gate.ict_session("2026-08-28T14:15:00-04:00")
check("14:15 ET は PM_SB", session["silverBullet"] == "PM_SB", session)
session = msnr_gate.ict_session("2026-08-28T03:30:00-04:00")
check("03:30 ET は LDN_OPEN_SB", session["silverBullet"] == "LDN_OPEN_SB", session)
session = msnr_gate.ict_session("2026-08-28T11:30:00-04:00")
check("窓の外は None", session["silverBullet"] is None, session)

print("=" * 68)
print("3. Turtle Soup 原典条件(Raschke: 20日極値・旧極値4セッション以上)")
print("=" * 68)


def daily(highs):
    return [{"t": i, "h": h, "l": h - 50.0} for i, h in enumerate(highs)]

# 極値 30200 が 9 セッション前 → classic
highs = [30000 + i for i in range(20)]
highs[10] = 30200.0
check("旧極値が古ければ classic", msnr_gate.classic_turtle_soup(30200.0, "SELL", daily(highs)))
# 極値が直近(1セッション前) → not classic
highs2 = [30000 + i for i in range(20)]
highs2[18] = 30200.0
check("極値が新しすぎれば classic ではない",
      not msnr_gate.classic_turtle_soup(30200.0, "SELL", daily(highs2)))
check("水準が20日極値と一致しなければ False",
      not msnr_gate.classic_turtle_soup(30100.0, "SELL", daily(highs)))
check("日足が20本未満なら判定しない(False)",
      not msnr_gate.classic_turtle_soup(30200.0, "SELL", daily(highs)[:10]))

print("=" * 68)
print("4. feature_tags(DISP_RESEARCH / LEVEL_BODY_INTACT / LONDON / CLASSIC_TS)")
print("=" * 68)

# 直前20本はレンジ1.0pt、起点バーはレンジ2.0pt(=中央値の2倍 ≥1.5倍)
bars = [bar(i * 180, 30100.0, 30100.5, 30099.5, 30100.0) for i in range(24)]
origin_t = 24 * 180
bars.append(bar(origin_t, 30100.0, 30101.0, 30099.0, 30099.2))   # 起点(レンジ2.0)
bars.append(bar(origin_t + 180, 30099.0, 30100.0, 30098.5, 30099.0))
chain = {"type": "SWEEP", "side": "SELL", "sweepBarT": origin_t}
level = {"label": "London High", "price": 30101.0}
bundle = {"snapshot": {"bars1d": daily(highs)}}

tags = msnr_gate.feature_tags(chain, level, bars, bundle)
check("研究互換displacementタグが付く", "DISP_RESEARCH_1_5X" in tags, tags)
check("Londonスイープタグが付く", "LONDON_RANGE_SWEEP" in tags, tags)
check("実体無傷タグが付く(起点以降にレベル超の実体クローズ無し)",
      "LEVEL_BODY_INTACT" in tags, tags)

# レベルを実体で破壊した足を足すと LEVEL_BODY_INTACT が消える
destroyed = bars + [bar(origin_t + 360, 30100.0, 30103.0, 30100.0, 30102.5)]
tags2 = msnr_gate.feature_tags(chain, level, destroyed, bundle)
check("実体破壊後はタグが消える", "LEVEL_BODY_INTACT" not in tags2, tags2)

# CLASSIC_TS: レベルを20日極値へ合わせる
level_ts = {"label": "Prev High", "price": 30200.0}
tags3 = msnr_gate.feature_tags(chain, level_ts, bars, bundle)
check("原典Turtle Soup条件でCLASSIC_TSが付く", "CLASSIC_TS" in tags3, tags3)
check("BREAKER連鎖にはスイープ系タグが付かない",
      "CLASSIC_TS" not in msnr_gate.feature_tags(
          {"type": "FLIP", "side": "SELL", "breakBarT": origin_t}, level_ts, bars, bundle))

print("=" * 68)
print("5. decision_context(日中成熟度・ギャップbucket)")
print("=" * 68)

ict = {"session": msnr_gate.ict_session("2026-08-28T13:00:00-04:00")}
snapshot_daily = [
    {"t": 1, "o": 30000.0, "h": 30100.0, "l": 30000.0, "c": 30080.0},
    {"t": 2, "o": 30110.0, "h": 30160.0, "l": 30100.0, "c": 30150.0},
]
context = msnr_gate.decision_context({"snapshot": {"bars1d": snapshot_daily}}, ict)
check("13:00 ET は PM(12:00時点で HOD 60.4% 済)",
      context["dayMaturity"]["bucket"] == "PM"
      and context["dayMaturity"]["hodCum"] == 0.604, context)
check("ギャップ +30pt / 前日レンジ100pt = GE25",
      context["gap"]["points"] == 30.0 and context["gap"]["bucket"] == "GE25", context)

ict_am = {"session": msnr_gate.ict_session("2026-08-28T10:15:00-04:00")}
context_am = msnr_gate.decision_context({}, ict_am)
check("10:15 ET は AM + AM_SB",
      context_am["dayMaturity"]["bucket"] == "AM"
      and context_am.get("silverBullet") == "AM_SB", context_am)
check("日足が無ければ gap を作らない", "gap" not in context_am)

reach = msnr_gate.target_reachability(
    ["Overnight High", "Prev Day Low", "Weekly High", "VWAP"])
check("目標到達率の注釈(E3統計ラベル)",
      reach == ["ON_EXTREME_BROKEN_94PCT", "PDL_TOUCHED_43PCT",
                "HTF_EXTREME_RARE", None], reach)
context_reach = msnr_gate.decision_context({}, ict_am, ["Prev Day High"])
check("decision_context に targetReachability が乗る",
      context_reach.get("targetReachability") == ["PDH_TOUCHED_57PCT"], context_reach)

print("=" * 68)
total = PASS[0] + FAIL[0]
if FAIL[0]:
    print(f"FAILED {FAIL[0]} / {total}")
    sys.exit(1)
print(f"ALL PASS ({total})")
sys.exit(0)

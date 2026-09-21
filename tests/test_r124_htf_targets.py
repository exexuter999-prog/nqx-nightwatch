# -*- coding: utf-8 -*-
"""R124: 上位足の水準で利確目標の**不足だけ**を埋める。

  1. 契約の節の読み取り(無い・壊れている → OFF)
  2. 水準は確定した上位足だけから作る(形成中の日足・回収済みスイングは使わない)
  3. 穴埋めだけ: 目標が揃っている候補には触れない / 揃わないときだけ引き直す
  4. evaluate() の外には漏れない
  5. 本番契約が LIVE で不正な値が無い

ネットワークも台帳も使わない。

    python tests/test_r124_htf_targets.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

for _stream in ("stdout", "stderr"):
    _file = getattr(sys, _stream, None)
    if _file is not None and hasattr(_file, "reconfigure"):
        try:
            _file.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

import htf_targets  # noqa: E402
import msnr_gate    # noqa: E402

PASS = [0]
FAIL = [0]


def check(label, condition, detail=""):
    if condition:
        PASS[0] += 1
        print(f"  OK   {label}")
    else:
        FAIL[0] += 1
        print(f"  NG   {label}  {detail}")


DAY = 86400
# 2026-09-21(月)07:31Z。日足の t は前日 22:00Z 開始。
NOW_ISO = "2026-09-21T07:31:00+00:00"
MON = 1789941600 - DAY * 0   # 09-20 22:00Z = 月曜の取引日(形成中)


def day(t, h, l):
    return {"t": float(t), "o": l, "h": float(h), "l": float(l), "c": h, "v": 1.0}


# 前週 09-14〜09-18(t は前日 22:00Z)。09-16 がスイング高値 30,600(以後越えていない)。
BARS = [
    day(MON - 8 * DAY, 29900, 29500),   # 09-11(金)= 前々週
    day(MON - 7 * DAY, 30000, 29600),   # 09-14(月)
    day(MON - 6 * DAY, 30100, 29700),   # 09-15
    day(MON - 5 * DAY, 30600, 29800),   # 09-16 スイング高値
    day(MON - 4 * DAY, 30200, 29400),   # 09-17 スイング安値 29,400
    day(MON - 3 * DAY, 30300, 29900),   # 09-18(金)= 前日
    day(MON, 31000, 29000),             # 09-21(月)形成中 —— 使ってはいけない
]


def bundle(price=30150.0, bars=None, frames=None):
    return {"at": NOW_ISO, "price": price,
            "snapshot": {"bars1d": list(BARS if bars is None else bars),
                         "htfContext": {"frames": frames or {}}}}


print("=" * 68)
print("1. 契約の節")
print("=" * 68)
check("節が無ければ OFF", htf_targets.policy({})["mode"] == "OFF")
check("LIVE で sources 省略 → 全ソース",
      htf_targets.policy({"htfTargets": {"mode": "LIVE"}})["sources"] == list(htf_targets.SOURCES))
bad = htf_targets.policy({"htfTargets": {"mode": "SHADOW"}})
check("未対応の mode は OFF + invalid", bad["mode"] == "OFF" and bad["invalid"], str(bad))
bad = htf_targets.policy({"htfTargets": {"mode": "LIVE", "sources": ["PREV_DAY", "MONTHLY"]}})
check("未知の source は OFF + invalid", bad["mode"] == "OFF" and bad["invalid"], str(bad))
check("OFF なら穴埋め水準は空", htf_targets.fill_for(bundle(), {"htfTargets": {"mode": "OFF"}}) == [])

print("=" * 68)
print("2. 水準は確定した上位足だけ")
print("=" * 68)
lv = dict((label, price) for price, label in htf_targets.levels(bundle()))
check("前日 = 09-18(形成中の月曜ではない)",
      lv.get("HTF Prev Day High") == 30300 and lv.get("HTF Prev Day Low") == 29900, str(lv))
check("前週 = 09-14〜09-18 の高安",
      lv.get("HTF Weekly High") == 30600 and lv.get("HTF Weekly Low") == 29400, str(lv))
check("未回収のスイング高値 30,600 を拾う", lv.get("HTF Daily Swing High") == 30600, str(lv))
check("形成中の足の 31,000 は使わない", 31000.0 not in lv.values(), str(lv))
swept = htf_targets.levels(bundle(price=30650.0))
check("現値が越えたスイング高値は使わない",
      all(label != "HTF Daily Swing High" for _p, label in swept), str(swept))
frames = {"1h": {"valid": True, "range20High": 30250.0, "range20Low": 29950.0},
          "4h": {"valid": False, "range20High": 30400.0, "range20Low": 29700.0}}
lv = dict((label, price) for price, label in htf_targets.levels(bundle(frames=frames)))
check("valid な 1h のレンジは使う", lv.get("HTF 1H Range High") == 30250.0, str(lv))
check("valid でない 4h は使わない", "HTF 4H Range High" not in lv, str(lv))
check("PREV_DAY だけ指定なら他は出ない",
      {label for _p, label in htf_targets.levels(bundle(), ["PREV_DAY"])}
      == {"HTF Prev Day High", "HTF Prev Day Low"})
check("日足が無ければ空", htf_targets.levels(bundle(bars=[])) == [])
check("接頭辞があっても階層は変わらない(Weekly=1 / Prev Day=2)",
      msnr_gate.level_tier("HTF Weekly High") == 1 and msnr_gate.level_tier("HTF Prev Day High") == 2)

print("=" * 68)
print("3. 穴埋めだけ")
print("=" * 68)
LEVELS = [{"label": "London High", "price": 30162.75}]
FILL = ((30637.0, "HTF Daily Swing High"), (30897.25, "HTF Weekly High"))
token = msnr_gate._HTF_FILL.set(FILL)
try:
    filled = msnr_gate.model_targets(30128.75, 30085.75, "BUY", LEVELS, {})
    check("目標が揃わない候補は上位足で埋まる",
          [t[0] for t in filled] == [30637.0, 30897.25], str(filled))
    rich = [{"label": "London High", "price": 30200.0}, {"label": "Weekly High", "price": 30300.0}]
    kept = msnr_gate.model_targets(30128.75, 30085.75, "BUY", rich, {})
    check("揃っている候補の目標は変わらない(runner も動かない)",
          [t[0] for t in kept] == [30200.0, 30300.0], str(kept))
    sell = msnr_gate.model_targets(30128.75, 30170.0, "SELL", LEVELS, {})
    check("方向の合わない水準では埋まらない(SELL に上の水準は使わない)", sell == [], str(sell))
finally:
    msnr_gate._HTF_FILL.reset(token)

print("=" * 68)
print("4. evaluate() の外に漏れない")
print("=" * 68)
check("既定は空", msnr_gate._HTF_FILL.get() == ())
check("evaluate の外では R124 以前と同じ(埋めない)",
      msnr_gate.model_targets(30128.75, 30085.75, "BUY", LEVELS, {}) == [])
try:
    msnr_gate.evaluate({"at": NOW_ISO, "price": 30150.0, "snapshot": {"bars1d": BARS}})
except Exception:
    pass
check("evaluate が例外で抜けても戻る", msnr_gate._HTF_FILL.get() == ())

print("=" * 68)
print("5. 本番契約")
print("=" * 68)
live = htf_targets.policy()
check("本番契約は LIVE・不正なし", live["mode"] == "LIVE" and not live["invalid"], str(live))

print("=" * 68)
if FAIL[0]:
    print(f"FAILED {FAIL[0]} / {PASS[0] + FAIL[0]}")
    sys.exit(1)
print(f"ALL PASS ({PASS[0]})")

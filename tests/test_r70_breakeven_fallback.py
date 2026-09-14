# -*- coding: utf-8 -*-
"""R70: TP1 後の押しでトレール目標が現在値を追い越しても、建値+1pt の床へは寄せる。

2026-09-08 19:24 実測(LFF…0006、LONG runner 1 枚 @29,590.75、高値 29,649、trail 16.75):
  desired = max(29,591.75, 29,649 − 16.75) = 29,632.25 > price 29,608 → 「逆側になる周期は見送る」
  → runner の SL が初期構造 SL 29,568.5(建値 −22pt)のまま。TP1 直後の押しは日常なので、
  §4「qty=1 確認後に建値以上へ一度寄せる」が一度も走らない。

固定する線引き:
  * トレールが逆側でも、床(建値+1pt)が現在値の正しい側にあり既存 SL より改善なら床へ MODIFY
  * 床すら現在値の逆側(価格が建値+1pt 未満へ戻った)なら従来どおり見送り(逆側 SL は作らない)
  * 既に床以上に SL がある(state.stop)なら改善なしで見送り(単調性)
  * トレールが protective なら従来どおりトレール値(床より上)へ
  * SELL も対称
"""
import os
import sys
from datetime import datetime, timezone

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import autotrade_engine as ae  # noqa: E402


def check(label, condition, detail=""):
    if not condition:
        raise AssertionError(f"{label}: {detail}")
    print(f"OK   {label}")


FILLED_AT = datetime(2026, 9, 8, 9, 57, 50, tzinfo=timezone.utc)


def plan(side="BUY"):
    sign = 1 if side == "BUY" else -1
    entry = 29590.75
    return {"symbol": "MNQU6", "side": side, "qty": 2, "entry": entry,
            "initialStop": entry - sign * 22.25, "tp1": entry + sign * 35.5,
            "finalTarget": entry + sign * 174.0, "trailDistance": 16.75,
            "legs": [{"id": "RUNNER", "qty": 1, "target": entry + sign * 174.0}],
            "legIdentityKnown": True}


def position(side="BUY", qty=1):
    return {"side": "LONG" if side == "BUY" else "SHORT", "qty": qty, "avgEntry": 29590.75,
            "accountId": "LFF00000000000006", "symbol": "MNQU6",
            "filledAt": FILLED_AT.isoformat()}


def bundle(price, extreme, side="BUY"):
    t0 = int(FILLED_AT.timestamp()) + 60
    hi = extreme if side == "BUY" else price + 5
    lo = extreme if side == "SELL" else price - 5
    return {"price": price, "snapshot": {"bars3m": [
        {"t": t0, "o": price, "h": hi, "l": lo, "c": price},
        {"t": t0 + 180, "o": price, "h": price + 2, "l": price - 2, "c": price},
    ]}}


# 1. 実測ケース: トレールが逆側 → 床(建値+1pt)へ寄せる
act = ae.management_action(plan(), position(), bundle(29608.0, 29649.0), {}, {"NQX_AUTOTRADE_KILL": "0"})
check("押し局面でも建値+1pt(29,591.75)へ MODIFY を出す",
      act is not None and act["action"] == "MODIFY" and act["sl"] == 29591.75 and act["qty"] == 1, act)

# 2. 価格が建値+1pt 未満へ戻った → 床も逆側 → 見送り(逆側 SL は作らない)
act = ae.management_action(plan(), position(), bundle(29591.5, 29649.0), {}, {"NQX_AUTOTRADE_KILL": "0"})
check("床が現在値の逆側なら見送る", act is None, act)

# 3. 既に床へ寄せ済み(state.stop) → 改善なし → 見送り
act = ae.management_action(plan(), position(), bundle(29608.0, 29649.0), {"stop": 29591.75}, {"NQX_AUTOTRADE_KILL": "0"})
check("床以上に SL があれば重ねて出さない(単調性)", act is None, act)

# 4. トレールが protective なら従来どおりトレール値
act = ae.management_action(plan(), position(), bundle(29640.0, 29649.0), {}, {"NQX_AUTOTRADE_KILL": "0"})
check("トレールが現在値の正しい側なら極値−距離(29,632.25)へ",
      act is not None and act["sl"] == 29632.25, act)

# 5. SELL は対称: 安値 29,532 から 29,573 へ戻した局面 → 床 29,589.75 へ
act = ae.management_action(plan("SELL"), position("SELL"), bundle(29573.0, 29532.0, "SELL"), {}, {"NQX_AUTOTRADE_KILL": "0"})
check("SELL: 戻し局面でも建値−1pt(29,589.75)へ", act is not None and act["sl"] == 29589.75, act)
act = ae.management_action(plan("SELL"), position("SELL"), bundle(29590.0, 29532.0, "SELL"), {}, {"NQX_AUTOTRADE_KILL": "0"})
check("SELL: 床が現在値の逆側なら見送る", act is None, act)

# 6. 全量(2枚)が残る間は触らない(従来どおり)
act = ae.management_action(plan(), position(qty=2), bundle(29608.0, 29649.0), {}, {"NQX_AUTOTRADE_KILL": "0"})
check("TP1 前(2枚)は OCO を触らない", act is None, act)

print("ALL PASS (test_r70_breakeven_fallback; external sends=0)")

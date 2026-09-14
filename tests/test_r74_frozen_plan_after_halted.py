# -*- coding: utf-8 -*-
"""R74: 古い ENTRY_HALTED / ENTRY_TERMINAL 行より新しい未所有プランは凍結プランになる。

2026-09-09 07:09 実測(LFF…0006): 04:52 に R52 で取消した SELL の ENTRY_HALTED 行が台帳に
残ったまま(未約定のまま消えたので FLAT 行は無い)、07:07 の BUY 4 枚が送信直後の部分約定で
未所有(fallback)になった。`_frozen_plan_record` は fallback を持ったまま走査を続け、古い
ENTRY_HALTED で None を返し、「open position has no frozen management plan」で実弾 4 枚が
ブローカー OCO だけで放置された。終端行に当たったら **fallback を返す**。
"""
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import autotrade_engine as ae  # noqa: E402


def check(label, condition, detail=""):
    if not condition:
        raise AssertionError(f"{label}: {detail}")
    print(f"OK   {label}")


ACC = "LFF00000000000006"


def plan(side, qty, tag, owned=False):
    p = {"symbol": "MNQU6", "side": side, "qty": qty, "entry": 29531.25, "entryKey": f"ENTRY:{tag}",
         "accountScope": [ACC],
         "legs": [{"id": "TP1", "qty": qty // 2, "target": 29605.5},
                  {"id": "RUNNER", "qty": qty - qty // 2, "target": 29686.75}]}
    if owned:
        p["positionOwnership"] = {"accountId": ACC, "generation": "POS:old"}
    return p


halted_sell = {"time": "t1", "status": "ENTRY_HALTED", "action": "ENTRY_STALE_CANCEL", "plan": plan("SELL", 6, "sell")}
recovered = {"time": "t2", "status": "ENTRY_RECOVERED", "action": "ENTRY_RECOVERY"}
claimed_buy = {"time": "t3", "status": "ENTRY_CLAIMED", "plan": plan("BUY", 4, "buy")}
partial_buy = {"time": "t4", "status": "ENTRY_PARTIAL_FILL", "plan": plan("BUY", 4, "buy"),
               "reason": "partial aggregate fill identity does not match exactly one leg"}

# 1. 実測の並び: 古い ENTRY_HALTED の後に新しい未所有プラン → 新しいプランを返す
rec = ae._frozen_plan_record([halted_sell, recovered, claimed_buy, partial_buy], "MNQU6", account=ACC)
check("古い ENTRY_HALTED より新しい未所有プランが凍結プランになる",
      rec is not None and rec["status"] == "ENTRY_PARTIAL_FILL" and rec["plan"]["side"] == "BUY", rec)

# 2. 終端行が最新なら従来どおり None(取消済みの後に何も無い)
rec = ae._frozen_plan_record([claimed_buy, halted_sell], "MNQU6", account=ACC)
check("最新が ENTRY_HALTED なら None", rec is None, rec)

# 3. 終端行より新しい所有済みプランは従来どおりそれを返す
owned_buy = {"time": "t5", "status": "ENTRY_SENT", "plan": plan("BUY", 4, "buy", owned=True)}
rec = ae._frozen_plan_record([halted_sell, claimed_buy, owned_buy], "MNQU6", account=ACC)
check("所有済みプランがあればそれを優先", rec is not None and rec["status"] == "ENTRY_SENT", rec)

# 4. ENTRY_TERMINAL でも同じ
terminal = {**halted_sell, "status": "ENTRY_TERMINAL", "action": "ENTRY_TERMINAL"}
rec = ae._frozen_plan_record([terminal, claimed_buy], "MNQU6", account=ACC)
check("ENTRY_TERMINAL より新しい未所有プランも凍結プランになる",
      rec is not None and rec["status"] == "ENTRY_CLAIMED", rec)

# 5. 口座指定なし(旧経路)は最新の採用可能プラン
rec = ae._frozen_plan_record([halted_sell, claimed_buy, partial_buy], "MNQU6")
check("口座指定なしでも最新プラン", rec is not None and rec["status"] == "ENTRY_PARTIAL_FILL", rec)

print("ALL PASS (test_r74_frozen_plan_after_halted)")

# -*- coding: utf-8 -*-
"""R71: 建玉が閉じた後は、その所有済みプランを凍結プランにしない。

2026-09-08 19:37 実測(LFF…0006): trade 1(VP80 BUY 2 枚、所有済み・ENTRY_PARTIAL_FILL)が
runner 決済で FLAT → 同じ周期で trade 2(VP80 BUY ULTRA 8 枚)を ENTRY_CLAIMED。次の周期で
`_frozen_plan_record` が trade 1 の所有プラン(2 枚 / RUNNER 1 枚)を返し、binder が LONG 8 を
「split lifecycle 外」と判定して管理が保留された。R35 の「所有プラン優先」は、その建玉が
まだ開いている前提でしか正しくない。
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


ACC = "LFF05062316710006"


def plan(qty, owned=False, tag="p"):
    p = {"symbol": "MNQU6", "side": "BUY", "qty": qty, "entry": 29590.75, "entryKey": f"ENTRY:{tag}",
         "accountScope": [ACC],
         "legs": [{"id": "TP1", "qty": max(1, qty // 2), "target": 29626.25},
                  {"id": "RUNNER", "qty": max(1, qty - qty // 2), "target": 29764.75}]}
    if owned:
        p["positionOwnership"] = {"accountId": ACC, "generation": "POS:old"}
    return p


old_claimed = {"time": "t1", "status": "ENTRY_CLAIMED", "plan": plan(2, tag="old")}
old_owned = {"time": "t2", "status": "ENTRY_PARTIAL_FILL", "plan": plan(2, owned=True, tag="old")}
flat_row = {"time": "t3", "status": "POSITION_GENERATION", "open": False, "accountId": ACC, "generation": 3}
new_claimed = {"time": "t4", "status": "ENTRY_CLAIMED", "plan": plan(8, tag="new")}

# 1. FLAT の後に新しい CLAIMED があれば、新プラン(8 枚)を返す
rec = ae._frozen_plan_record([old_claimed, old_owned, flat_row, new_claimed], "MNQU6", account=ACC)
check("FLAT 後の新 CLAIMED プランが凍結プランになる", rec is not None and rec["plan"]["qty"] == 8, rec)

# 2. FLAT 記録が無ければ従来どおり所有済みプラン(2 枚)を優先する
rec = ae._frozen_plan_record([old_claimed, old_owned, new_claimed], "MNQU6", account=ACC)
check("建玉が開いている間は所有済みプランを優先(従来どおり)", rec is not None and rec["plan"]["qty"] == 2, rec)

# 3. FLAT 後に新プランが無ければ None(閉じた建玉のプランを返さない)
rec = ae._frozen_plan_record([old_claimed, old_owned, flat_row], "MNQU6", account=ACC)
check("FLAT 後で新プランが無ければ None", rec is None, rec)

# 4. 別口座の FLAT 記録は影響しない
other_flat = {**flat_row, "accountId": "OTHER"}
rec = ae._frozen_plan_record([old_claimed, old_owned, other_flat, new_claimed], "MNQU6", account=ACC)
check("別口座の FLAT では所有済みプランを外さない", rec is not None and rec["plan"]["qty"] == 2, rec)

# 5. 口座指定なしの旧経路は従来どおり最新の採用可能プラン
rec = ae._frozen_plan_record([old_claimed, old_owned, flat_row, new_claimed], "MNQU6")
check("口座指定なしでは最新の採用可能プラン(8 枚)", rec is not None and rec["plan"]["qty"] == 8, rec)

# 6. 未約定の指値(RESTING)が FLAT 行より新しければ従来どおり凍結プランになる
resting = {"time": "t5", "status": "ENTRY_RESTING", "plan": plan(2, tag="rest")}
rec = ae._frozen_plan_record([old_claimed, old_owned, flat_row, resting], "MNQU6", account=ACC)
check("FLAT 後に置いた指値のプランは採用される", rec is not None and rec["status"] == "ENTRY_RESTING", rec)

print("ALL PASS (test_r71_frozen_plan_after_flat)")

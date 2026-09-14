#!/usr/bin/env python3
"""R84: 合成プランの所有権束縛(§4)。

`ownership_binder.bind()` は **一行も変えない**。合成プランだけが
`tranche.bind_composite()` を通り、fail closed の規律(identity 欠落・口座/銘柄/
方向不一致・衝突は所有しない)は同じままであることを固定する。

    python tests/test_r84_bind_composite.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _r84_fixtures as fx  # noqa: E402

sys.path.insert(0, fx.BASE)
import ownership_binder  # noqa: E402
import tranche  # noqa: E402

PASS = [0]
FAIL = [0]


def check(label, condition, detail=""):
    if condition:
        PASS[0] += 1
        print(f"  OK   {label}")
    else:
        FAIL[0] += 1
        print(f"  NG   {label}  {detail}")


PLAN = fx.composite()
ROWS = fx.live_orders_full()


def bind(qty=8, rows=None, plan=None, pos=None, corridor=None, gen_number=7,
         generation=None):
    pos = pos or fx.position(qty=qty)
    if generation is None:
        generation = fx.generation(pos, gen_number)
    return tranche.bind_composite(plan or PLAN, pos, fx.orders(ROWS if rows is None else rows),
                                  position_generation=generation,
                                  corridor_add_qty=corridor)


print("--- 基本 ---")
result = bind()
check("全脚 OPEN・枚数一致で所有する",
      result["owned"] is True and result["state"] == "OWNED_COMPOSITE", str(result.get("reason")))
check("期待枚数は構造から導出される(8)", result["expectedQty"] == 8)
check("ownedPlan は top-level 互換キーを保つ",
      all(key in result["ownedPlan"] for key in
          ("symbol", "side", "qty", "entry", "initialStop", "legs", "targets",
           "finalTarget", "trailDistance", "accountScope")))
check("ownedPlan.legs は **生きている脚だけ** の平坦化",
      len(result["ownedPlan"]["legs"]) == 4)
check("positionOwnership に世代が焼かれる",
      result["ownedPlan"]["positionOwnership"]["generation"].startswith("PG:7:POS:"))

print()
print("--- fail closed ---")
check("UNVERIFIED な建玉は所有しない",
      bind(pos=fx.position(verified=False))["owned"] is False)
check("UNVERIFIED な注文は所有しない",
      tranche.bind_composite(PLAN, fx.position(), {"verified": False, "orders": []},
                             position_generation=fx.generation(fx.position()))["owned"] is False)
check("凍結スコープ外の口座は所有しない",
      bind(pos=fx.position(account="OTHER"))["owned"] is False)
check("方向が違えば所有しない", bind(pos=fx.position(side="LONG"))["owned"] is False)
check("枚数が構造と合わなければ所有しない(回廊なし)",
      bind(qty=7)["owned"] is False and "does not match" in str(bind(qty=7)["reason"]))
check("世代ラッパが無ければ所有しない",
      bind(generation="PG:7:POS:" + "0" * 64)["owned"] is False)
check("単一プランを渡しても合成として扱わない",
      tranche.bind_composite({"symbol": fx.SYM}, fx.position(), fx.orders(ROWS),
                             position_generation=fx.generation(fx.position()))["owned"] is False)

collide = ROWS + [{"orderId": "FOREIGN", "accountId": fx.ACC, "symbol": fx.SYM,
                   "action": fx.SIDE, "status": "WORKING", "parentId": None,
                   "brokerParentId": None, "receipt": "R:FOREIGN",
                   "fieldsComplete": False}]
check("同方向の親行が他にあれば曖昧として所有しない",
      bind(rows=collide)["owned"] is False
      and "colliding" in str(bind(rows=collide)["reason"]))

dup = fx.composite(tranches=[fx.tranche_one(), fx.tranche_one()])
check("同じ identity を 2 つの脚が名乗れば所有しない",
      tranche.bind_composite(dup, fx.position(), fx.orders(ROWS),
                             position_generation=fx.generation(fx.position()))["owned"] is False)

print()
print("--- 遷移回廊(§4.3)の境界 ---")
# 基礎 = T1 だけ(4 枚)、追撃 4 枚。回廊は [4, 8]。
base_plan = fx.composite(tranches=[fx.tranche_one()], qty=4)
base_rows = []
for item in fx.tranche_one()["legs"]:
    base_rows += fx.bracket_rows(item["orderId"], item["bracketOrderIds"])


def corridor_bind(qty):
    pos = fx.position(qty=qty)
    return tranche.bind_composite(base_plan, pos, fx.orders(base_rows),
                                  position_generation=fx.generation(pos),
                                  corridor_add_qty=4)


check("base-1(3 枚)は回廊の外 → 所有しない", corridor_bind(3)["owned"] is False)
check("base(4 枚)は所有する(遷移中ではない)",
      corridor_bind(4)["owned"] is True and corridor_bind(4)["state"] == "OWNED_COMPOSITE")
mid = corridor_bind(6)
check("base+部分(6 枚)は遷移中として所有する",
      mid["owned"] is True and mid["state"] == "PYRAMID_TRANSIT", str(mid.get("reason")))
full = corridor_bind(8)
check("base+add(8 枚)も遷移中として所有する",
      full["owned"] is True and full["state"] == "PYRAMID_TRANSIT")
check("base+add+1(9 枚)は回廊の外 → 所有しない", corridor_bind(9)["owned"] is False)
check("回廊を渡さなければ 6 枚は所有しない(恒久的な緩和ではない)",
      tranche.bind_composite(base_plan, fx.position(qty=6), fx.orders(base_rows),
                             position_generation=fx.generation(fx.position(qty=6))
                             )["owned"] is False)

print()
print("--- 統合後(oco_sibling_pair 1 組)---")
pair_rows = fx.oco_rows(["C-A", "C-B"])
merged = {"trancheId": "C2", "entryKey": PLAN["entryKey"], "initialStop": 29470.0,
          "entry": 29466.375, "orderType": "MARKET",
          "legs": [{"id": "RUNNER", "qty": 4, "target": 29300.0}],
          "consolidationPair": {"orderIds": ["C-A", "C-B"], "receipts": ["R:C-A", "R:C-B"],
                                "qty": 4, "sl": 29470.0, "tp": 29300.0}}
consolidated = {**PLAN, "tranches": [merged], "qty": 4,
                "legs": [{"id": "RUNNER", "qty": 4, "target": 29300.0, "trancheId": "C2"}],
                "consolidation": merged["consolidationPair"]}
pos4 = fx.position(qty=4)
bound = tranche.bind_composite(consolidated, pos4, fx.orders(pair_rows),
                               position_generation=fx.generation(pos4))
check("統合後は 1 組の照合だけで所有する(既存 runner 管理と同形)",
      bound["owned"] is True and bound["expectedQty"] == 4, str(bound.get("reason")))
pending_pair = {**merged, "consolidationPair": {"orderIds": [], "receipts": [], "qty": 4,
                                                "sl": 29470.0, "tp": 29300.0,
                                                "pending": True}}
pending_plan = {**consolidated, "tranches": [pending_pair]}
pending_bound = tranche.bind_composite(pending_plan, pos4, fx.orders([]),
                                       position_generation=fx.generation(pos4))
check("身元未束縛の張り替えは遷移中として所有(FLATTEN は生きる)",
      pending_bound["owned"] is True and pending_bound["state"] == "PYRAMID_TRANSIT",
      str(pending_bound.get("reason")))

print()
print("--- 統合の **後にもう一度** 追撃した合成(maxAdds=2 で到達する) ---")
# ここは実際に所有権を落としていた。`oco_sibling_pair` は「scope に未終端の逆方向行が
# ちょうど 2 本」を要求するので、判定器へ全行を渡すと 1 組 + 新トランシェ 2 脚 = 6 行で
# INCONSISTENT へ倒れ、**合成建玉まるごと管理外**になる。凍結した 2 行だけを渡し、
# 子ブラケットの有無だけを全行で見るのが正しい使い方(bind_replacement_bracket と同形)。
T3 = fx.tranche_two()
add_rows = []
for item in T3["legs"]:
    add_rows += fx.bracket_rows(item["orderId"], item["bracketOrderIds"])
second = {**PLAN, "tranches": [merged, T3], "qty": 8,
          "legs": [{"id": "RUNNER", "qty": 4, "target": 29300.0, "trancheId": "C2"},
                   {"id": "TP1", "qty": 2, "target": 29400.0, "trancheId": "T2"},
                   {"id": "RUNNER", "qty": 2, "target": 29300.0, "trancheId": "T2"}]}
pos8 = fx.position(qty=8)
mixed = tranche.bind_composite(second, pos8, fx.orders(pair_rows + add_rows),
                               position_generation=fx.generation(pos8))
check("統合済み 1 組 + 新トランシェ 2 脚でも所有が続く",
      mixed["owned"] is True and mixed["expectedQty"] == 8, str(mixed.get("reason")))
check("統合済みトランシェは OPEN のまま",
      [row["state"] for row in mixed["legStates"]][0] == tranche.OPEN,
      str(mixed["legStates"]))
check("位相は ACCUMULATION(新しい TP1 が生きている)",
      mixed["phase"] == tranche.ACCUMULATION)
foreign = fx.oco_rows(["X-A", "X-B"]) + add_rows
check("凍結 id と違う 1 組は拾わない(緩めたぶん厳しさを失っていない)",
      tranche.bind_composite(second, pos8, fx.orders(foreign),
                             position_generation=fx.generation(pos8))["owned"] is False)
half = [row for row in pair_rows if row["orderId"] != "C-B"] + add_rows
check("統合の片割れだけ残るのは INCONSISTENT のまま",
      tranche.bind_composite(second, pos8, fx.orders(half),
                             position_generation=fx.generation(pos8))["owned"] is False)
child = [dict(row, brokerParentId="SOME-ENTRY") for row in pair_rows] + add_rows
check("親を持つ 2 行(OSO の子)は統合の 1 組と認めない",
      tranche.bind_composite(second, pos8, fx.orders(child),
                             position_generation=fx.generation(pos8))["owned"] is False)

print()
print("--- 世代交代を跨ぐ束縛 ---")
newer = fx.position(qty=8, avg_entry=29466.375)
newer["filledAt"] = "2026-09-12T02:30:00.000Z"
rebound = tranche.bind_composite(PLAN, newer, fx.orders(ROWS),
                                 position_generation=fx.generation(newer, 8))
check("avgEntry/filledAt が変わって世代が進んでも構造が同じなら所有する",
      rebound["owned"] is True
      and rebound["ownedPlan"]["positionOwnership"]["generation"].startswith("PG:8:"),
      str(rebound.get("reason")))

print()
print("--- 既存 binder の不変 ---")
check("ownership_binder.bracket_structure は _bracket_structure と同一実体",
      ownership_binder.bracket_structure is ownership_binder._bracket_structure)
check("LEGS は増えていない(TP1/RUNNER のまま)",
      ownership_binder.LEGS == ("TP1", "RUNNER") and tranche.LEGS == ("TP1", "RUNNER"))
source = open(os.path.join(fx.BASE, "tranche.py"), encoding="utf-8").read()
check("lifecycle_quantities 型の枚数集合を合成へ持ち込んでいない(退行の芽)",
      "lifecycle_quantities =" not in source and "lifecycle_quantities[" not in source)

print()
print(f"合計: PASS {PASS[0]} / FAIL {FAIL[0]}")
sys.exit(1 if FAIL[0] else 0)

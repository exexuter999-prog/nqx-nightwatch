# -*- coding: utf-8 -*-
"""R84: 合成建玉の不変条件を、**例ごとではなく性質として**固定する。

他の test_r84_* は「この状況ではこうなる」を書いている。ここはランダムに組んだ
構造 4000 通りに対して、どの状況でも破ってはいけない性質だけを確かめる ——
実害の出た事故(R37 の逆側 SL / R78 の裸 runner / R84 の所有権喪失)は全部
「想定していなかった状況」で起きたからである。

種は固定してあるので結果は再現する(失敗したら trial 番号で同じ状態を再現できる)。

    python tests/test_r84_invariants.py
"""
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _r84_fixtures as fx  # noqa: E402

sys.path.insert(0, fx.BASE)
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


SIDE = "SELL"
BUFFER = 4.0
OFFSET = 1.0
TRIALS = 4000
# WORKING 以外も混ぜる。SUSPENDED(親未約定)と終端は「生きている保護」ではない。
STATUSES = ["WORKING", "ACCEPTED", "SUBMITTED", "SUSPENDED", "PENDING",
            "CANCELED", "FILLED", "REJECTED", "EXPIRED"]
PRICES = (29250.0, 29350.0, 29450.0, 29470.0, 29530.0)


def make_tranche(index, qty_tp1, qty_run):
    prefix = f"F{index}"
    return {
        "trancheId": prefix, "entryKey": "ENTRY:" + str(index) * 64,
        "entry": 29500.0 - index, "initialStop": 29520.0 + index, "orderType": "MARKET",
        "legs": [fx.leg("TP1", qty_tp1, 29400.0 - index,
                        prefix + "-TP1", [prefix + "-TP1-A", prefix + "-TP1-B"]),
                 fx.leg("RUNNER", qty_run, 29300.0 - index,
                        prefix + "-RUN", [prefix + "-RUN-A", prefix + "-RUN-B"])],
        "routeSnapshot": [],
    }


violations = {}


def violate(name, *detail):
    violations.setdefault(name, []).append(detail)


rng = random.Random(20260912)
owned_count = 0
modify_count = 0
flatten_count = 0
transit_count = 0

for trial in range(TRIALS):
    tranches = [make_tranche(i, rng.randint(1, 4), rng.randint(1, 4))
                for i in range(rng.randint(1, 3))]
    plan = fx.composite(tranches=tranches, qty=0,
                        governing=tranches[-1]["initialStop"])
    rows = []
    for row in tranches:
        for leg in row["legs"]:
            mode = rng.choice(["both", "both", "both", "one", "none"])
            status = rng.choice(STATUSES) if rng.random() < 0.25 else "WORKING"
            pair = fx.bracket_rows(leg["orderId"], leg["bracketOrderIds"], status=status)
            if mode == "both":
                rows += pair
            elif mode == "one":
                rows += pair[:1]
    qty = rng.randint(1, 16)
    corridor = rng.choice([None, None, rng.randint(1, 6)])
    position = fx.position(qty=qty)
    binding = tranche.bind_composite(plan, position, fx.orders(rows),
                                     position_generation=fx.generation(position),
                                     corridor_add_qty=corridor)
    states = tranche.leg_states(plan, rows, account=fx.ACC, symbol=fx.SYM, action=SIDE)
    inconsistent = [row for row in states if row["state"] == tranche.INCONSISTENT]
    expected = tranche.expected_qty(states)

    if not binding["owned"]:
        continue
    owned_count += 1
    transit = binding["state"] == "PYRAMID_TRANSIT"
    transit_count += 1 if transit else 0

    # --- 所有の不変条件 ---
    if inconsistent:
        violate("構造が読めない脚があるのに所有した", trial, inconsistent[0]["reason"])
    if expected <= 0:
        violate("生きている脚が無いのに所有した", trial)
    if corridor is None and qty != expected:
        violate("回廊なしで枚数が期待とずれたまま所有した", trial, qty, expected)
    if corridor is not None and not (expected <= qty <= expected + corridor):
        violate("回廊の外で所有した", trial, qty, expected, corridor)
    if binding["expectedQty"] != expected:
        violate("expectedQty が leg_states と食い違う", trial)

    # --- 管理の不変条件 ---
    # engine が管理に使うのは **ownedPlan**。`bind_composite` はそこで
    # `pyramid.flattenBeyond` を「いま生きている runner 目標」で引き直すので、
    # 凍結時の古い値で健全な建玉を撤退させることが無い。ここでも同じ物を使う。
    managed = binding["ownedPlan"]
    live = binding["legStates"]
    phase = tranche.phase(managed, live)
    governing = managed["initialStop"]
    beyond = tranche.flatten_beyond(managed, live)
    if (managed.get("pyramid") or {}).get("flattenBeyond") != beyond:
        violate("ownedPlan の flattenBeyond が引き直されていない", trial)
    for price in PRICES:
        action = tranche.management_action_composite(
            managed, position, price=price, states=live, current_stop=None,
            best=price - 30.0, stop_buffer=BUFFER, breakeven_offset=OFFSET,
            transit=transit)
        if not action:
            continue
        if action["action"] == "HALT":
            violate("健全な合成建玉で HALT を出した", trial, price, action["reason"])
            continue
        if action["action"] == "FLATTEN":
            flatten_count += 1
            reached_stop = price >= governing
            reached_beyond = beyond is not None and price <= beyond
            if not (reached_stop or reached_beyond):
                violate("撤退条件でないのに FLATTEN を出した", trial, price, governing, beyond)
            if action["qty"] != qty:
                violate("FLATTEN の枚数が建玉と違う", trial, action["qty"], qty)
            continue
        modify_count += 1
        if phase == tranche.ACCUMULATION:
            violate("ACCUMULATION で MODIFY を出した(TP1 を消す)", trial, price)
        if transit:
            violate("遷移中に MODIFY を出した", trial, price)
        if not action["sl"] >= price + BUFFER - 1e-9:
            violate("現在値の逆側/近すぎる SL を出した", trial, price, action["sl"])
        if action["tp"] is not None and not action["tp"] < price:
            violate("現在値の後ろの TP を出した", trial, price, action["tp"])
        if action["qty"] != qty:
            violate("MODIFY の枚数が建玉と違う", trial, action["qty"], qty)
        if action.get("consolidate") is not True:
            violate("合成の MODIFY に統合の印が無い", trial)

print(f"--- ランダム構造 {TRIALS} 通り ---")
print(f"  (所有 {owned_count} / 遷移中 {transit_count} / MODIFY {modify_count} "
      f"/ FLATTEN {flatten_count})")
check("所有・枚数・回廊・管理のどの不変条件も破れない",
      not violations,
      "; ".join(f"{name} x{len(items)} 例 {items[0]}" for name, items in violations.items()))
check("所有できる構造が十分な数あった(テストが空回りしていない)", owned_count > 100,
      str(owned_count))
check("MODIFY と FLATTEN の両方を通った", modify_count > 0 and flatten_count > 0,
      f"modify={modify_count} flatten={flatten_count}")
check("遷移中の枝も通った", transit_count > 0, str(transit_count))

print()
print(f"合計: PASS {PASS[0]} / FAIL {FAIL[0]}")
sys.exit(1 if FAIL[0] else 0)

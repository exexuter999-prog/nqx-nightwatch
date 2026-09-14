#!/usr/bin/env python3
"""R84: 脚の三状態と構造導出枚数(docs/R84_PYRAMID_TRANCHE_MANAGEMENT.md §1.2 / §4.2)。

枚数の集合照合(`lifecycle_quantities`)は追撃と両立しない —— 認めるべき枚数が
2^脚数 の羃集合になり、枚数だけではどの脚が生きているか一意に決まらない。ここでは
**注文行の消失**で決まることを、TP1 同枚数の曖昧ケースまで含めて固定する。

    python tests/test_r84_tranche_states.py
"""
import os
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


def state_of(leg, rows):
    return tranche.leg_state(leg, rows, account=fx.ACC, symbol=fx.SYM, action=fx.SIDE)


T1 = fx.tranche_one()
T2 = fx.tranche_two()
TP1_T1, RUN_T1 = T1["legs"]
TP1_T2, RUN_T2 = T2["legs"]
ALL_ROWS = fx.live_orders_full()

print("--- 脚の三状態 ---")
check("対が 2 本 live なら OPEN", state_of(TP1_T1, ALL_ROWS)[0] == tranche.OPEN,
      str(state_of(TP1_T1, ALL_ROWS)))
absent = [row for row in ALL_ROWS if row["orderId"] not in TP1_T1["bracketOrderIds"]]
check("対が 1 本も無ければ CLOSED(TP/SL のどちらかが約定し OCO で消えた)",
      state_of(TP1_T1, absent)[0] == tranche.CLOSED)
half = [row for row in ALL_ROWS if row["orderId"] != TP1_T1["bracketOrderIds"][1]]
check("片割れだけ残るのは INCONSISTENT", state_of(TP1_T1, half)[0] == tranche.INCONSISTENT,
      str(state_of(TP1_T1, half)))

suspended = []
for row in ALL_ROWS:
    row = dict(row)
    if row["orderId"] in TP1_T2["bracketOrderIds"]:
        row["status"] = "SUSPENDED"
    suspended.append(row)
check("SUSPENDED の子(親が未約定)は OPEN ではない",
      state_of(TP1_T2, suspended)[0] == tranche.INCONSISTENT,
      str(state_of(TP1_T2, suspended)))

bad_receipt = []
for row in ALL_ROWS:
    row = dict(row)
    if row["orderId"] == RUN_T1["bracketOrderIds"][0]:
        row["receipt"] = "R:OTHER"
    bad_receipt.append(row)
check("receipt 不一致は INCONSISTENT",
      state_of(RUN_T1, bad_receipt)[0] == tranche.INCONSISTENT)

wrong_parent = []
for row in ALL_ROWS:
    row = dict(row)
    if row["orderId"] in RUN_T2["bracketOrderIds"]:
        row["brokerParentId"] = "SOMEONE-ELSE"
    wrong_parent.append(row)
check("別の親を名乗る子は INCONSISTENT",
      state_of(RUN_T2, wrong_parent)[0] == tranche.INCONSISTENT)

check("identity(orderId/receipt)が無い脚は INCONSISTENT",
      state_of({"id": "TP1", "qty": 2, "target": 1.0}, ALL_ROWS)[0] == tranche.INCONSISTENT)
check("bracketOrderIds が 2 本でなければ INCONSISTENT",
      state_of({**TP1_T1, "bracketOrderIds": ["only-one"]}, ALL_ROWS)[0]
      == tranche.INCONSISTENT)

print()
print("--- 構造導出枚数(枚数の列挙をやめる) ---")
plan = fx.composite()
states = tranche.leg_states(plan, ALL_ROWS, account=fx.ACC, symbol=fx.SYM, action=fx.SIDE)
check("全脚 OPEN なら期待枚数は合計", tranche.expected_qty(states) == 8,
      str(tranche.expected_qty(states)))

# TP1 同枚数の曖昧ケース: T1/T2 の TP1 はどちらも 2 枚。枚数の引き算では
# 「どちらが閉じたか」が決まらないが、注文行の消失なら一意に決まる。
only_t2_tp1_closed = [row for row in ALL_ROWS
                      if row["orderId"] not in TP1_T2["bracketOrderIds"]]
states2 = tranche.leg_states(plan, only_t2_tp1_closed, account=fx.ACC, symbol=fx.SYM,
                            action=fx.SIDE)
closed = [row for row in states2 if row["state"] == tranche.CLOSED]
check("TP1 が同枚数でも、閉じた脚は構造で一意に決まる",
      len(closed) == 1 and closed[0]["trancheId"] == "T2"
      and closed[0]["legId"] == "TP1", str(closed))
check("その期待枚数は 8 − 2 = 6", tranche.expected_qty(states2) == 6,
      str(tranche.expected_qty(states2)))

only_t1_tp1_closed = [row for row in ALL_ROWS
                      if row["orderId"] not in TP1_T1["bracketOrderIds"]]
states3 = tranche.leg_states(plan, only_t1_tp1_closed, account=fx.ACC, symbol=fx.SYM,
                            action=fx.SIDE)
closed3 = [row for row in states3 if row["state"] == tranche.CLOSED]
check("反対側が閉じた場合も一意(枚数は同じ 6 でも帰属が違う)",
      tranche.expected_qty(states3) == 6 and len(closed3) == 1
      and closed3[0]["trancheId"] == "T1", str(closed3))

print()
print("--- 位相 ---")
check("TP1 が 1 本でも OPEN なら ACCUMULATION",
      tranche.phase(plan, states2) == tranche.ACCUMULATION)
runners_only = [row for row in ALL_ROWS
                if row["orderId"] not in TP1_T1["bracketOrderIds"]
                and row["orderId"] not in TP1_T2["bracketOrderIds"]]
states4 = tranche.leg_states(plan, runners_only, account=fx.ACC, symbol=fx.SYM,
                            action=fx.SIDE)
check("全 TP1 が CLOSED で RUNNER が残れば RUNNERS",
      tranche.phase(plan, states4) == tranche.RUNNERS)
check("その期待枚数は runner だけの 4", tranche.expected_qty(states4) == 4)
states5 = tranche.leg_states(plan, [], account=fx.ACC, symbol=fx.SYM, action=fx.SIDE)
check("全脚 CLOSED なら CLOSED 位相・期待枚数 0",
      tranche.phase(plan, states5) == tranche.FLAT and tranche.expected_qty(states5) == 0)

print()
print("--- 全 CLOSED + 建玉残は fail closed ---")
binding = tranche.bind_composite(plan, fx.position(qty=8), fx.orders([]),
                                 position_generation=fx.generation(fx.position()))
check("ブローカー上に構造が無い建玉は所有しない",
      binding["owned"] is False and "live structure" in str(binding["reason"]),
      str(binding.get("reason")))

print()
print("--- 統合(consolidation)の 1 組 ---")
pair_rows = fx.oco_rows(["C-A", "C-B"])
merged = {"trancheId": "C2", "entryKey": plan["entryKey"], "initialStop": 29470.0,
          "orderType": "MARKET", "entry": 29466.375,
          "legs": [{"id": "RUNNER", "qty": 4, "target": 29300.0}],
          "consolidationPair": {"orderIds": ["C-A", "C-B"],
                                "receipts": ["R:C-A", "R:C-B"],
                                "qty": 4, "sl": 29470.0, "tp": 29300.0}}
consolidated = {**plan, "tranches": [merged], "qty": 4,
                "legs": [{"id": "RUNNER", "qty": 4, "target": 29300.0, "trancheId": "C2"}]}
c_states = tranche.leg_states(consolidated, pair_rows, account=fx.ACC, symbol=fx.SYM,
                              action=fx.SIDE)
check("凍結した 1 組が live なら OPEN",
      [row["state"] for row in c_states] == [tranche.OPEN], str(c_states))
check("消えていれば CLOSED",
      [row["state"] for row in tranche.leg_states(consolidated, [], account=fx.ACC,
                                                  symbol=fx.SYM, action=fx.SIDE)]
      == [tranche.CLOSED])
mismatched = fx.oco_rows(["C-A", "C-OTHER"])
check("凍結 id と違う 1 組は INCONSISTENT(他人の張り替えを拾わない)",
      [row["state"] for row in tranche.leg_states(consolidated, mismatched, account=fx.ACC,
                                                  symbol=fx.SYM, action=fx.SIDE)]
      == [tranche.INCONSISTENT])
child_pair = fx.oco_rows(["C-A", "C-B"])
for row in child_pair:
    row["brokerParentId"] = "SOME-ENTRY"
check("親を持つ 2 行(OSO の子)は統合の 1 組として認めない",
      [row["state"] for row in tranche.leg_states(consolidated, child_pair, account=fx.ACC,
                                                  symbol=fx.SYM, action=fx.SIDE)]
      == [tranche.INCONSISTENT])
pending = {**merged, "consolidationPair": {"orderIds": [], "receipts": [], "qty": 4,
                                           "sl": 29470.0, "tp": 29300.0, "pending": True}}
pending_plan = {**consolidated, "tranches": [pending]}
check("身元未束縛の張り替えは PENDING(CLOSED と読むと所有権ごと落ちる)",
      [row["state"] for row in tranche.leg_states(pending_plan, [], account=fx.ACC,
                                                  symbol=fx.SYM, action=fx.SIDE)]
      == [tranche.PENDING])
check("PENDING は生きている枚数として数える",
      tranche.expected_qty(tranche.leg_states(pending_plan, [], account=fx.ACC,
                                              symbol=fx.SYM, action=fx.SIDE)) == 4)

print()
print(f"合計: PASS {PASS[0]} / FAIL {FAIL[0]}")
sys.exit(1 if FAIL[0] else 0)

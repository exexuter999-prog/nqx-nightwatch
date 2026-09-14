# -*- coding: utf-8 -*-
"""R75: 指値が有利側へ滑って約定した建玉を、乖離 2pt 超でも所有する。

2026-09-09 07:06 実測(LFF…0006): VP80 BUY 4 枚(2/2)を指値 29,531.25 で送信。脚 1 が
29,526.75(4.5pt 有利)で即約定、脚 2 は 29,531.25。平均建値 29,529.0 と指値の乖離 2.25pt が
binder の上限(契約 maxDeviationPoints=2.0)を超え、両脚 FILLED・枚数一致・方向一致なのに
「qty2 aggregate position is not bound to a filled split leg」で未所有 → 実弾 4 枚が
ブローカー OCO だけで放置された。有利側の上限は凍結プランの SL 幅、不利側は従来どおり。

    python tests/test_r75_limit_favorable_fill.py
"""
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
import broker_status  # noqa: E402
import ownership_binder  # noqa: E402

PASS = [0]
FAIL = [0]
ACC = "LFF05062316710006"


def check(label, condition, detail=""):
    if condition:
        PASS[0] += 1
        print(f"  OK   {label}")
    else:
        FAIL[0] += 1
        print(f"  NG   {label}  {detail}")


PLAN = {
    "scenarioId": "R75-S", "entryKey": "ENTRY:R75", "symbol": "MNQU6",
    "side": "BUY", "qty": 4, "entry": 29531.25, "initialStop": 29496.5,
    "tp1": 29605.5, "finalTarget": 29686.75, "targets": [29605.5, 29686.75],
    "entryOrderType": "LIMIT", "accountScope": [ACC], "planVersion": "R75-SPLIT-1",
    "legs": [{"id": "TP1", "qty": 2, "target": 29605.5},
             {"id": "RUNNER", "qty": 2, "target": 29686.75}],
}
ROUTE = [
    {"accountId": ACC, "legId": "TP1", "state": "ACCEPTED",
     "orderId": "651929060719", "receipt": f"TRADOVATE:{ACC}:651929060719"},
    {"accountId": ACC, "legId": "RUNNER", "state": "ACCEPTED",
     "orderId": "651929060737", "receipt": f"TRADOVATE:{ACC}:651929060737"},
]


def row(order_id, status, action="BUY", parent=None):
    # CrossTrade/Tradovate の形: 数量・種別・価格なし、receipt は derived
    return {"orderId": order_id, "accountId": ACC, "symbol": "MNQU6", "status": status,
            "action": action, "qty": None, "orderType": None, "limitPrice": None,
            "fieldsComplete": False, "parentId": None, "brokerParentId": parent,
            "receipt": f"TRADOVATE:{ACC}:{order_id}", "receiptSource": "derived"}


def orders():
    rows = [row("651929060719", "FILLED"), row("651929060737", "FILLED"),
            row("651929060720", "WORKING", "SELL", "651929060719"), row("651929060721", "WORKING", "SELL"),
            row("651929060738", "WORKING", "SELL", "651929060737"), row("651929060739", "WORKING", "SELL")]
    return {"verified": True, "orders": rows,
            "activeOrders": [r for r in rows if r["status"] in broker_status.BROKER_ACTIVE_STATES]}


def position(avg, qty=4):
    # 建玉行は注文との対応(receipt)を持たない(R52 実測の形)
    return {"verified": True, "accountId": ACC, "symbol": "MNQU6", "side": "LONG", "qty": qty,
            "avgEntry": avg, "orderId": "651929060053", "receipt": None,
            "filledAt": "2026-09-08T22:07:23.977Z"}


def bind(pos, plan=PLAN):
    raw = broker_status.position_identity(pos)
    return ownership_binder.bind(plan, ROUTE, pos, orders(), route_state="SENT",
                                 position_generation=f"PG:18:{raw}")


# 1. 実測: 平均 29,529.0(有利側 2.25pt)→ 所有(OWNED_FULL)
res = bind(position(29529.0))
check("有利側 2.25pt の乖離でも両脚 FILLED + 枚数一致なら所有", res["owned"] and res["state"] == "OWNED_FULL", res.get("reason"))

# 2. 有利側でも SL 幅(34.75pt)を超える乖離は別建玉
res = bind(position(29531.25 - 40.0))
check("SL 幅を超えて有利な建玉は所有しない", not res["owned"], res.get("state"))

# 3. 不利側は 1 tick でも所有しない(従来どおり)
res = bind(position(29531.5))
check("不利側 0.25pt は所有しない", not res["owned"], res.get("state"))

# 4. 従来の 2pt 以内は当然所有
res = bind(position(29530.0))
check("有利側 1.25pt は従来どおり所有", res["owned"] and res["state"] == "OWNED_FULL", res.get("reason"))

# 5. initialStop が無いプランは契約の既定(2pt)に倒れる
plan_no_stop = {k: v for k, v in PLAN.items() if k != "initialStop"}
res = bind(position(29529.0), plan_no_stop)
check("SL 幅が無ければ従来の 2pt 上限(2.25pt は未所有)", not res["owned"], res.get("state"))
res = bind(position(29530.0), plan_no_stop)
check("SL 幅が無くても 2pt 以内は所有", res["owned"], res.get("reason"))

# 6. 部分約定(TP1 脚 2 枚だけ)も同じ緩和: 平均 29,526.75(4.5pt 有利)
def orders_partial():
    rows = [row("651929060719", "FILLED"), row("651929060737", "WORKING"),
            row("651929060720", "WORKING", "SELL", "651929060719"), row("651929060721", "WORKING", "SELL")]
    return {"verified": True, "orders": rows,
            "activeOrders": [r for r in rows if r["status"] in broker_status.BROKER_ACTIVE_STATES]}

pos2 = position(29526.75, qty=2)
raw2 = broker_status.position_identity(pos2)
res = ownership_binder.bind(PLAN, ROUTE, pos2, orders_partial(), route_state="SENT",
                            position_generation=f"PG:17:{raw2}")
check("部分約定(脚 1 だけ、4.5pt 有利)も所有(PARTIAL_FILL)",
      res["owned"] and res["state"] == "PARTIAL_FILL", res.get("reason"))

print()
print(f"合計: PASS {PASS[0]} / FAIL {FAIL[0]} (external sends=0)")
sys.exit(1 if FAIL[0] else 0)

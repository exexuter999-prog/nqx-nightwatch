# -*- coding: utf-8 -*-
"""R84 の共有フィクスチャ。`test_` で始まらないので run_all.py は実行しない。

実測に合わせた形だけを使う(2026-09-11 の台帳から):
  * エントリーの親行は成行だと一覧に出ない。脚の身元は
    ``orderId`` / ``receipt`` / ``bracketOrderIds`` / ``bracketReceipts``。
  * ブラケットの子行は建玉と **逆方向** で、`parentId` に OCO の相方、
    `brokerParentId` に **真の親**(ENTRY 注文)を持つ。
  * 張り替え後の 1 組だけが `brokerParentId` 無し・`brokerOcoId` 相互(R80)。
"""
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

ACC = "ACC-R84"
SYM = "MNQU6"
SIDE = "SELL"
OPPOSITE = "BUY"


def bracket_rows(parent, ids, receipts=None, status="WORKING", account=ACC, symbol=SYM,
                 action=OPPOSITE):
    """エントリー脚のブラケット子 2 行(相互 parentId + 真の親 brokerParentId)。"""
    receipts = receipts or [f"R:{value}" for value in ids]
    first, second = ids
    return [
        {"orderId": first, "accountId": account, "symbol": symbol, "action": action,
         "status": status, "parentId": second, "brokerParentId": parent,
         "receipt": receipts[0], "fieldsComplete": False, "qty": None,
         "orderType": "", "limitPrice": None},
        {"orderId": second, "accountId": account, "symbol": symbol, "action": action,
         "status": status, "parentId": first, "brokerParentId": parent,
         "receipt": receipts[1], "fieldsComplete": False, "qty": None,
         "orderType": "", "limitPrice": None},
    ]


def oco_rows(ids, receipts=None, status="WORKING", account=ACC, symbol=SYM,
             action=OPPOSITE):
    """張り替え後の OCO 兄弟 1 組(R80: 親を持たず、ocoId が相互)。"""
    receipts = receipts or [f"R:{value}" for value in ids]
    first, second = ids
    return [
        {"orderId": first, "accountId": account, "symbol": symbol, "action": action,
         "status": status, "parentId": second, "brokerParentId": None,
         "brokerOcoId": second, "receipt": receipts[0], "fieldsComplete": False},
        {"orderId": second, "accountId": account, "symbol": symbol, "action": action,
         "status": status, "parentId": first, "brokerParentId": None,
         "brokerOcoId": first, "receipt": receipts[1], "fieldsComplete": False},
    ]


def leg(leg_id, qty, target, parent, children):
    return {"id": leg_id, "qty": qty, "target": target, "orderId": parent,
            "receipt": f"R:{parent}",
            "bracketOrderIds": list(children),
            "bracketReceipts": [f"R:{value}" for value in children]}


def tranche_one():
    return {"trancheId": "T1", "entryKey": "ENTRY:" + "1" * 64, "scenarioId": "S-T1",
            "model": "VP80_REVERSION", "grade": "A+",
            "entry": 29484.25, "entryReference": 29484.25, "initialStop": 29520.0,
            "orderType": "MARKET",
            "legs": [leg("TP1", 2, 29440.0, "T1-TP1", ["T1-TP1-A", "T1-TP1-B"]),
                     leg("RUNNER", 2, 29380.0, "T1-RUN", ["T1-RUN-A", "T1-RUN-B"])],
            "routeSnapshot": []}


def tranche_two():
    return {"trancheId": "T2", "entryKey": "ENTRY:" + "2" * 64, "scenarioId": "S-T2",
            "model": "VP80_REVERSION", "grade": "A+",
            "entry": 29448.5, "entryReference": 29448.5, "initialStop": 29507.5,
            "orderType": "MARKET",
            "legs": [leg("TP1", 2, 29400.0, "T2-TP1", ["T2-TP1-A", "T2-TP1-B"]),
                     leg("RUNNER", 2, 29300.0, "T2-RUN", ["T2-RUN-A", "T2-RUN-B"])],
            "routeSnapshot": []}


def composite(tranches=None, qty=8, consolidation=None, entry=29466.375,
              governing=29507.5, trail=18.75):
    tranches = tranches if tranches is not None else [tranche_one(), tranche_two()]
    legs = []
    for row in tranches:
        for item in row["legs"]:
            legs.append({"id": item["id"], "qty": item["qty"], "target": item["target"],
                         "trancheId": row["trancheId"]})
    return {
        "planKind": "PYRAMID_COMPOSITE",
        "planVersion": "R19-ICT-SPLIT-1",
        "entryKey": "ENTRY:" + "2" * 64,
        "scenarioId": "S-T2", "fingerprint": "fp_r84", "evidenceHash": "se_r84",
        "marketCycleId": "cy_r84", "decisionId": "S-T2",
        "accountScope": [ACC], "symbol": SYM, "side": SIDE, "qty": qty,
        "entry": entry, "entryOrderType": "MARKET", "entryReference": entry,
        "initialStop": governing, "riskPoints": abs(entry - governing),
        "riskDollars": abs(entry - governing) * qty * 2.0,
        "tp1": 29440.0, "finalTarget": 29300.0, "targets": [29440.0, 29300.0],
        "legs": legs, "trailDistance": trail, "mode": "SPLIT_BRACKETS_TP1_RUNNER",
        "ultra": True, "ultraDrawdown": 3100.0, "riskCapDollars": 3100.0,
        "riskCapSource": "ACCOUNT_DRAWDOWN_BUFFER",
        "pyramid": {"addsDone": 1, "maxAdds": 2, "combinedEntry": entry,
                    "combinedRisk": abs(entry - governing) * qty * 2.0,
                    "governingStop": governing, "flattenBeyond": 29300.0},
        "tranches": tranches,
        "consolidation": consolidation,
    }


def orders(rows, verified=True):
    active = [row for row in rows
              if str(row.get("status") or "").upper() not in
              {"FILLED", "CANCELED", "REJECTED", "EXPIRED"}]
    return {"verified": verified, "state": "PENDING" if active else "NONE",
            "orders": list(rows), "activeOrders": active,
            "terminalOrders": [row for row in rows if row not in active],
            "accountScope": [ACC], "symbol": SYM}


def position(qty=8, avg_entry=29466.375, side="SHORT", verified=True, account=ACC):
    return {"verified": verified, "source": "fixture", "platform": "CROSSTRADE",
            "account": account, "accountId": account, "symbol": SYM, "side": side,
            "qty": qty, "avgEntry": avg_entry, "filledAt": "2026-09-12T02:12:00.000Z",
            "orderId": "POS-1", "receipt": None,
            "observedAt": "2026-09-12T02:15:00.000Z"}


def generation(pos, number=7):
    import broker_status
    identity = broker_status.position_identity(pos)
    return f"PG:{number}:{identity}"


def live_orders_full():
    """両トランシェ・全脚が生きている状態。"""
    rows = []
    for row in (tranche_one(), tranche_two()):
        for item in row["legs"]:
            rows += bracket_rows(item["orderId"], item["bracketOrderIds"])
    return rows

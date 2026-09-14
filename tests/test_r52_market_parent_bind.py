# -*- coding: utf-8 -*-
"""R52: 成行 ENTRY の親注文を子行の真の親 id から読み足して束縛する。

CrossTrade/Tradovate の一覧は working な注文だけを返す。成行の親は POST 直後に
FILLED になり一覧に一度も現れない(2026-09-05 04:27 実測: 28 枚成行が約定して
いたのに identity UNKNOWN → HALT)。子行 raw の `parentId`(真の親)と `ocoId`
(OCO の相方)は別物で、正規化は `ocoId` を `parentId` に写していたため親 id が
失われていた。

    python tests/test_r52_market_parent_bind.py
"""
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
import broker_status  # noqa: E402
import order  # noqa: E402
import route_identity  # noqa: E402

PASS = [0]
FAIL = [0]
ACC = "LFE00000000000024"
SYM = "MNQU6"


def check(label, condition, detail=""):
    if condition:
        PASS[0] += 1
        print(f"  OK   {label}")
    else:
        FAIL[0] += 1
        print(f"  NG   {label}  {detail}")


def raw(order_id, action="Buy", status="Working", oco=None, parent=None,
        ts="2026-09-04T19:27:34.570Z"):
    row = {"id": order_id, "accountId": 90000024, "contractId": 4399654, "timestamp": ts,
           "action": action, "ordStatus": status, "executionProviderId": 14,
           "archived": False, "external": False, "admin": False}
    if oco is not None:
        row["ocoId"] = oco
    if parent is not None:
        row["parentId"] = parent
    return row


def view(rows):
    return broker_status.normalize_crosstrade_orders(
        {"success": True, "data": rows}, platform="TRADOVATE", account=ACC, symbol=SYM,
        account_aliases=["90000024"], contract_ids=["4399654"])


def bind(before, after):
    return route_identity.bind_entry_leg(before, after, account=ACC, symbol=SYM, action="SELL",
                                         qty=14, order_type="MARKET", entry_price=None)


# 実測の形: 一覧には子 2 行だけ。片方が parentId=真の親、両方が ocoId=相方
CHILD_A = raw(649589730298, oco=649589730299, parent=649589730297)
CHILD_B = raw(649589730299, oco=649589730298)
PARENT = raw(649589730297, action="Sell", status="Filled")

before = view([])
after = view([CHILD_A, CHILD_B])
rows = {r["orderId"]: r for r in after["orders"]}
check("parentId は従来どおり OCO の相方",
      rows["649589730298"]["parentId"] == 649589730299
      and rows["649589730299"]["parentId"] == 649589730298)
check("brokerParentId に真の親が残る",
      rows["649589730298"]["brokerParentId"] == "649589730297"
      and rows["649589730299"]["brokerParentId"] is None)
check("読み足し前: 成行の親は窓に無く束縛できない(実測の再現)", bind(before, after) is None)

calls = []


def fake_query(symbol, known_order_ids=None, account=None):
    calls.append((symbol, tuple(known_order_ids or ()), account))
    extra = [PARENT] if "649589730297" in (known_order_ids or []) else []
    return view([CHILD_A, CHILD_B] + extra)


aug = order._augment_market_parents(SYM, before, after, account=ACC, query_orders=fake_query)
check("子行の真の親 id で per-order 詳細を照会", calls == [(SYM, ("649589730297",), ACC)], str(calls))
check("親行が読み足され marketParentIds に残る",
      aug.get("marketParentIds") == ["649589730297"] and len(aug["orders"]) == 3)
bound = bind(before, aug)
check("読み足し後: FILLED の親に束縛される",
      bound is not None and bound["orderId"] == "649589730297" and bound["status"] == "FILLED"
      and bound["filledAt"] == "2026-09-04T19:27:34.570Z", str(bound))

# 2 脚目の窓: 1 脚目の親を既知にした上で、新しい子だけから親を引く
CHILD_C = raw(649589730316, oco=649589730317, parent=649589730315, ts="2026-09-04T19:27:35.164Z")
CHILD_D = raw(649589730317, oco=649589730316, ts="2026-09-04T19:27:35.164Z")
PARENT2 = raw(649589730315, action="Sell", status="Filled", ts="2026-09-04T19:27:35.164Z")
after2 = view([CHILD_A, CHILD_B, CHILD_C, CHILD_D])
calls.clear()


def fake_query2(symbol, known_order_ids=None, account=None):
    calls.append(tuple(known_order_ids or ()))
    extra = [r for r in (PARENT, PARENT2) if str(r["id"]) in (known_order_ids or [])]
    return view([CHILD_A, CHILD_B, CHILD_C, CHILD_D] + extra)


aug2 = order._augment_market_parents(SYM, aug, after2, account=ACC, query_orders=fake_query2)
check("2 脚目は新しい子の親だけを照会", calls == [("649589730315",)], str(calls))
bound2 = bind(aug, aug2)
check("2 脚目は 2 つ目の親に束縛", bound2 is not None and bound2["orderId"] == "649589730315", str(bound2))

# 1 つの窓に 2 脚分の子が入ったら親が 2 つ → 曖昧 → None(fail closed)
aug_both = order._augment_market_parents(SYM, before, after2, account=ACC, query_orders=fake_query2)
check("同じ窓に親が 2 つなら束縛しない", bind(before, aug_both) is None)


# 照会失敗・親 id なし・before なしは after をそのまま返す(推測で行を作らない)
def boom(*args, **kwargs):
    raise RuntimeError("down")


check("照会失敗は元の after",
      order._augment_market_parents(SYM, before, after, account=ACC, query_orders=boom) is after)
only_b = view([CHILD_B])
check("親 id の無い窓は元の after",
      order._augment_market_parents(SYM, before, only_b, account=ACC, query_orders=boom) is only_b)
check("before が無ければ元の after",
      order._augment_market_parents(SYM, None, after, account=ACC, query_orders=boom) is after)
limit_view = view([raw(1, action="Sell"), raw(2, parent=1, oco=3), raw(3, oco=2)])
check("親が既に一覧にあれば照会しない(指値の親)",
      order._augment_market_parents(SYM, before, limit_view, account=ACC, query_orders=boom) is limit_view)

print()
print(f"合計: PASS {PASS[0]} / FAIL {FAIL[0]}")
sys.exit(1 if FAIL[0] else 0)

# -*- coding: utf-8 -*-
"""R72: 成行 ENTRY 脚の identity を約定履歴(/fills)から束縛する第 2 経路。

2026-09-08 10:34 / 21:16 実測: 成行 ULTRA の 2 脚とも HTTP 200 なのに identity UNKNOWN →
HALT。送信直後の注文一覧が一過性の状態で Unavailable になると、注文窓の束縛は脚ごと
None に確定する。約定履歴には orderId(= 親 ENTRY 注文)・数量・価格・時刻が残っていた
(…321 qty 3 / …339 qty 3)ので、それを一意性の規則で束縛する。

    python tests/test_r72_fills_identity.py
"""
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
import order  # noqa: E402
import route_identity  # noqa: E402

PASS = [0]
FAIL = [0]
ACC = "LFF05062316710006"
SYM = "MNQU6"


def check(label, condition, detail=""):
    if condition:
        PASS[0] += 1
        print(f"  OK   {label}")
    else:
        FAIL[0] += 1
        print(f"  NG   {label}  {detail}")


def fill(fill_id, order_id, action="BUY", qty=3, price=29555.5, at="2026-09-08T12:16:12.049Z",
         instrument=SYM):
    return {"id": str(fill_id), "orderId": str(order_id), "action": action, "qty": qty,
            "price": price, "at": at, "instrument": instrument, "contractId": 4399654}


def view(rows, account=ACC, verified=True):
    return {"verified": verified, "source": "crosstrade", "platform": "TRADOVATE",
            "account": account, "fills": rows}


def bind(before, after, qty=3, action="BUY", **kw):
    return route_identity.bind_entry_leg_from_fills(before, after, account=ACC, symbol=SYM,
                                                    action=action, qty=qty, **kw)


# ---- 実測の形(2026-09-08 21:16): 脚 1 の窓に親 …321 の約定 qty 3 が 1 件
before = view([fill(194, 155, "SELL", 4, 29575.75, "2026-09-08T10:47:17.075Z")])
after1 = view(before["fills"] + [fill(328, 651929060321)])
ident = bind(before, after1)
check("脚 1: 新しい約定 1 件(qty 一致)→ 親 orderId に束縛",
      ident is not None and ident["orderId"] == "651929060321"
      and ident["receipt"] == f"TRADOVATE:{ACC}:651929060321"
      and ident["status"] == "FILLED" and ident["filledAt"] == "2026-09-08T12:16:12.049Z"
      and ident["fillPrice"] == 29555.5 and ident["identitySource"] == "broker-fills"
      and ident["fillIds"] == ["328"], str(ident))
check("route_attempt_state は identity で ACCEPTED",
      order.route_attempt_state(200, '{"success": true}', ident) == "ACCEPTED")

# ---- 脚 2 の窓: 脚 1 の約定は既知になっている
after2 = view(after1["fills"] + [fill(346, 651929060339, price=29555.0,
                                      at="2026-09-08T12:16:25.721Z")])
ident2 = bind(after1, after2)
check("脚 2: 前の窓を before にすれば 2 つ目の親に束縛",
      ident2 is not None and ident2["orderId"] == "651929060339" and ident2["fillPrice"] == 29555.0,
      str(ident2))

# ---- 同じ窓に 2 脚分 → 曖昧 → None。束縛済み id を外せば残りの 1 件に決まる
check("同じ窓に qty 一致の親が 2 つなら束縛しない", bind(before, after2) is None)
ident2b = bind(before, after2, exclude_order_ids={"651929060321"})
check("束縛済みの orderId を外せば残りの 1 件へ",
      ident2b is not None and ident2b["orderId"] == "651929060339", str(ident2b))

# ---- 部分約定: 同じ orderId の約定を合算して脚の数量に一致
partial = view(before["fills"] + [fill(401, 777, qty=1, price=29555.0, at="2026-09-08T12:16:12.049Z"),
                                  fill(402, 777, qty=2, price=29556.5, at="2026-09-08T12:16:12.301Z")])
identp = bind(before, partial)
check("部分約定は合算して束縛(加重平均価格・最終時刻)",
      identp is not None and identp["orderId"] == "777"
      and abs(identp["fillPrice"] - (29555.0 * 1 + 29556.5 * 2) / 3) < 1e-9
      and identp["filledAt"] == "2026-09-08T12:16:12.301Z" and identp["fillIds"] == ["401", "402"],
      str(identp))

# ---- 数量不一致・方向違い・別銘柄は候補にならない
check("数量が違えば束縛しない", bind(before, view(before["fills"] + [fill(9, 1, qty=4)])) is None)
check("逆方向の約定(ブラケット決済)は無視",
      bind(before, view(before["fills"] + [fill(9, 1, action="SELL", qty=3)])) is None)
check("別銘柄の約定は無視",
      bind(before, view(before["fills"] + [fill(9, 1, instrument="MESU6")])) is None)
check("qty 一致の同方向が 1 件なら他を混ぜても束縛",
      bind(before, view(before["fills"] + [fill(9, 1, action="SELL", qty=3), fill(10, 2)])) is not None
      and bind(before, view(before["fills"] + [fill(9, 1, action="SELL", qty=3), fill(10, 2)]))["orderId"] == "2")

# ---- fail closed: before 無し・未検証・id 無し・帰属不明・口座不一致
check("before が無ければ束縛しない", bind(None, after1) is None)
check("after が未検証なら束縛しない", bind(before, view(after1["fills"], verified=False)) is None)
noid = view(before["fills"] + [{**fill(328, 651929060321), "id": ""}])
check("fill id の無い行があれば比較しない", bind(before, noid) is None)
orphan = view(before["fills"] + [fill(328, 651929060321), {**fill(329, ""), "orderId": ""}])
check("帰属不明(orderId 無し)の同方向約定が窓にあれば曖昧として None", bind(before, orphan) is None)
check("view の口座が違えば束縛しない", bind(before, view(after1["fills"], account="OTHER")) is None)
check("qty が不正なら束縛しない", bind(before, after1, qty=0) is None and bind(before, after1, qty="x") is None)

# ---- order.py の窓: None(一過性の失敗)でも上限まで読み直し、方向一致の新約定で止まる
seq = [None, view(before["fills"]), view(before["fills"] + [fill(9, 1, action="SELL", qty=3)]), after1, after1]
calls = []
slept = []


def snapshot(account):
    calls.append(account)
    return seq[min(len(calls) - 1, len(seq) - 1)]


settled = order._fills_snapshot_settled(ACC, before, action="BUY", snapshot=snapshot,
                                        sleep=slept.append, attempts=8, delay=0.5)
check("None → 変化なし → 逆方向のみ、を越えて同方向の新約定が出た窓で止まる",
      settled is after1 and calls == [ACC] * 4 and slept == [0.5] * 3, f"calls={calls} slept={slept}")

calls.clear()
slept.clear()
identity, after_view = order._entry_fills_identity(
    ACC, before, symbol=SYM, action="BUY", qty=3, snapshot=snapshot, sleep=slept.append)
check("_entry_fills_identity は settle 後の窓で束縛し (identity, after) を返す",
      identity is not None and identity["orderId"] == "651929060321" and after_view is after1,
      str(identity))

calls.clear()
slept.clear()
identity, after_view = order._entry_fills_identity(
    ACC, before, symbol=SYM, action="BUY", qty=3, settle=False,
    snapshot=lambda account: after1, sleep=slept.append)
check("settle=False は待たずに 1 回だけ読む(窓を進めるだけ)", slept == [] and after_view is after1)

identity, after_view = order._entry_fills_identity(
    ACC, None, symbol=SYM, action="BUY", qty=3, snapshot=lambda account: after1, sleep=slept.append)
check("送信前の窓が無ければ束縛しない(after は返す)", identity is None and after_view is after1)

identity, after_view = order._entry_fills_identity(
    ACC, before, symbol=SYM, action="BUY", qty=3, snapshot=lambda account: None,
    sleep=slept.append, settle=True)
check("照会が全部 None なら (None, None)", identity is None and after_view is None)


# ---- 実 API を叩かない
def boom(*args, **kwargs):
    raise RuntimeError("network")


import broker_status  # noqa: E402

original = broker_status.query_fills
broker_status.query_fills = boom
try:
    check("_fills_snapshot は照会例外を None に畳む(送信を壊さない)", order._fills_snapshot(ACC) is None)
finally:
    broker_status.query_fills = original

print()
print(f"合計: PASS {PASS[0]} / FAIL {FAIL[0]} (external sends=0)")
sys.exit(1 if FAIL[0] else 0)

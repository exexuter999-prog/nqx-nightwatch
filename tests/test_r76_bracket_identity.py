# -*- coding: utf-8 -*-
"""R76: 成行の即時約定を **ブラケットの子 2 行** から束縛し、UNKNOWN の経路を後周期に再束縛する。

2026-09-08 19:34 JST(10:34Z、LFF…0006)実測: ULTRA 8 枚(4/4)の成行 2 脚が HTTP 200
なのに、送信直後の identity 取得が `NQX_ROUTE_SNAPSHOT [... "state":"UNKNOWN" ...] x2` →
`HALT ENTRY live send failed/unknown`。1 秒後には LONG 8 @29585.875 と SELL の子 4 本
(…154/155・…172/173、各対は parentId で相互に結線、片方が brokerParentId に真の親)が
見えていた。以後の周期は binder が「accepted=0 has no ownership」を返し続け、建玉は
ブローカー OCO だけで放置された(TP1 後の建値移動・トレール・構造 SL 決済なし)。

固定する線引き:
  (a) 送信直後: 一覧が空 → 一過性 None → 子 2 行、の順で読めても親 id へ束縛できる
  (b) 後周期: 凍結経路が UNKNOWN でも、建玉枚数 ∈ split lifecycle・平均建値の整合・
      送信窓の中に作られた live な対がちょうど脚の数、なら再束縛して台帳へ
      ENTRY_SENT(ENTRY_OWNERSHIP_BOUND, routeState SENT, haltResolution)を追記し、
      `_has_halt` が塞がなくなり、次周期から管理(TP1 後の MODIFY)が走る
  (c) 曖昧(同方向の親行が同居 / 枚数が lifecycle 外 / 窓の外 / 対にならない子)は
      何も書かず UNKNOWN のまま
  (d) 指値の親行による従来の束縛は変わらない

    python tests/test_r76_bracket_identity.py
"""
import contextlib
import os
import sys
import tempfile
from datetime import datetime, timezone

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
import autotrade_engine as ae  # noqa: E402
import execution_contract  # noqa: E402
import order  # noqa: E402
import ownership_binder  # noqa: E402
import route_envelope  # noqa: E402
import route_identity  # noqa: E402


@contextlib.contextmanager
def manual_ownership_halt():
    """R79 の緊急停止(``ownershipAttribution.manualHalt``)を一時的に ON にする。"""
    cfg = execution_contract.CONTRACT.setdefault("ownershipAttribution", {})
    before = cfg.get("manualHalt")
    cfg["manualHalt"] = True
    try:
        yield
    finally:
        if before is None:
            cfg.pop("manualHalt", None)
        else:
            cfg["manualHalt"] = before

PASS = [0]
FAIL = [0]
ACC = "LFF05062316710006"
SYM = "MNQU6"
TS = "2026-09-08T10:34:04.758Z"
ae._read_env_file = lambda: {}   # 本番 .secrets を読まない(hermetic)


def check(label, condition, detail=""):
    if condition:
        PASS[0] += 1
        print(f"  OK   {label}")
    else:
        FAIL[0] += 1
        print(f"  NG   {label}  {detail}")


# ---------------------------------------------------------------- ブローカー行(実測の正規化形)

def child(order_id, oco, parent=None, status="WORKING", action="SELL", ts=TS, account=ACC):
    return {"orderId": str(order_id), "accountId": account, "brokerAccountId": "64968904",
            "symbol": SYM, "contractId": "4399654", "status": status, "orderType": None,
            "action": action, "qty": None, "fieldsComplete": False, "limitPrice": None,
            "filledPrice": None, "stopPrice": None, "filled": None,
            "parentId": int(oco) if str(oco).isdigit() else oco,
            "brokerParentId": str(parent) if parent is not None else None,
            "receipt": f"TRADOVATE:{account}:{order_id}", "receiptSource": "derived",
            "timestamp": ts}


def parent(order_id, status="FILLED", action="BUY", ts=TS, account=ACC, order_type=None,
           limit_price=None, qty=None):
    return {"orderId": str(order_id), "accountId": account, "brokerAccountId": "64968904",
            "symbol": SYM, "contractId": "4399654", "status": status,
            "orderType": order_type, "action": action, "qty": qty,
            "fieldsComplete": bool(order_type and action and qty),
            "limitPrice": limit_price, "filledPrice": None, "stopPrice": None, "filled": None,
            "parentId": None, "brokerParentId": None,
            "receipt": f"TRADOVATE:{account}:{order_id}", "receiptSource": "derived",
            "timestamp": ts}


def view(rows, account=ACC):
    active = [r for r in rows if r["status"] not in {"FILLED", "CANCELED", "REJECTED", "EXPIRED"}]
    terminal = [r for r in rows if r not in active]
    return {"verified": True, "source": "crosstrade-tradovate", "platform": "TRADOVATE",
            "symbol": SYM, "state": "PENDING" if active else ("FILLED" if terminal else "NONE"),
            "openCount": len(active), "orders": list(rows), "activeOrders": active,
            "terminalOrders": terminal, "orderIds": [r["orderId"] for r in active],
            "filledOrderIds": [r["orderId"] for r in terminal if r["status"] == "FILLED"],
            "unresolvedOrderIds": [], "accountScope": [account],
            "observedAt": "2026-09-08T10:34:05.000Z"}


# 実測の形: 対 1 = 154/155(真の親 153)、対 2 = 172/173(真の親 171)
PAIR1 = [child(651929060154, 651929060155, parent=651929060153), child(651929060155, 651929060154)]
PAIR2 = [child(651929060172, 651929060173, parent=651929060171, ts="2026-09-08T10:34:05.312Z"),
         child(651929060173, 651929060172, ts="2026-09-08T10:34:05.312Z")]
PARENT1 = parent(651929060153)
PARENT2 = parent(651929060171, ts="2026-09-08T10:34:05.312Z")
P1, P2 = "651929060153", "651929060171"


def no_augment(symbol, before, after, account=None, query_orders=None):
    # per-order 詳細が読めない(親行が足せない)状況を固定する
    return after


print("\n--- (a) 送信直後: 子 2 行による束縛 ---")
empty = view([])
seq = [empty, None, view(PAIR1)]
calls, slept = [], []


def snapshot(symbol, account=None):
    calls.append(account)
    return seq[min(len(calls) - 1, len(seq) - 1)]


ident, after = order._entry_orders_identity(
    SYM, empty, account=ACC, action="BUY", qty=4, order_type="MARKET", entry_price=None,
    snapshot=snapshot, augment=no_augment, sleep=slept.append, attempts=10, delay=1.0)
check("空 → None(一過性) → 子 2 行、の順で読んで親 id に束縛",
      ident is not None and ident["orderId"] == P1 and ident["receipt"] == f"TRADOVATE:{ACC}:{P1}"
      and ident["status"] == "FILLED" and ident["filledAt"] == TS
      and ident["identitySource"] == "broker-brackets"
      and ident["bracketOrderIds"] == ["651929060154", "651929060155"]
      and ident["bracketReceipts"] == [f"TRADOVATE:{ACC}:651929060154", f"TRADOVATE:{ACC}:651929060155"],
      str(ident))
check("読みは 3 回・待ちは 2 回(束縛できた時点で止まる)", len(calls) == 3 and slept == [1.0, 1.0],
      f"{calls} {slept}")
check("route_attempt_state は identity で ACCEPTED",
      order.route_attempt_state(200, '{"success": true}', ident) == "ACCEPTED")

# 脚 2 の窓: 脚 1 の対は before に含まれる。脚 2 の対だけが新しい
after1 = view(PAIR1)
both = view(PAIR1 + PAIR2)
ident2 = route_identity.bind_entry_leg_from_brackets(after1, both, account=ACC, symbol=SYM,
                                                     action="BUY", qty=4)
check("脚 2 は自分の窓の新しい対に束縛", ident2 is not None and ident2["orderId"] == P2, str(ident2))
check("同じ窓に 2 対なら束縛しない(曖昧)",
      route_identity.bind_entry_leg_from_brackets(empty, both, account=ACC, symbol=SYM,
                                                  action="BUY", qty=4) is None)
check("束縛済みの親 id を外せば残りの 1 対へ",
      route_identity.bind_entry_leg_from_brackets(
          empty, both, account=ACC, symbol=SYM, action="BUY", qty=4,
          exclude_order_ids={P1})["orderId"] == P2)
check("pick_index/expected_count があれば作成順に割り当て",
      route_identity.bind_entry_leg_from_brackets(
          empty, both, account=ACC, symbol=SYM, action="BUY", qty=4,
          pick_index=1, expected_count=2)["orderId"] == P2)
check("約定履歴の枚数証拠(全候補にある)があれば枚数で絞る",
      route_identity.bind_entry_leg_from_brackets(
          empty, both, account=ACC, symbol=SYM, action="BUY", qty=3,
          parent_qty={P1: 5, P2: 3})["orderId"] == P2)
suspended = view([child(651929060154, 651929060155, parent=651929060153, status="SUSPENDED"),
                  child(651929060155, 651929060154, status="SUSPENDED")])
check("SUSPENDED の対(親が未約定)は約定の証拠にしない",
      route_identity.bind_entry_leg_from_brackets(empty, suspended, account=ACC, symbol=SYM,
                                                  action="BUY", qty=4) is None)
orphan = view([child(651929060154, 651929060155, parent=651929060153)])
check("相方の無い子は対にならない → None",
      route_identity.bind_entry_leg_from_brackets(empty, orphan, account=ACC, symbol=SYM,
                                                  action="BUY", qty=4) is None)
no_parent = view([child(651929060154, 651929060155), child(651929060155, 651929060154)])
check("真の親 id を名乗らない対は束縛しない",
      route_identity.bind_entry_leg_from_brackets(empty, no_parent, account=ACC, symbol=SYM,
                                                  action="BUY", qty=4) is None)
split_pair = view(PAIR1[:1])
split_after = view(PAIR1)
check("相方が前の窓に既にあっても対は割れない(after 全体で組み、新しい行を含む対を採る)",
      route_identity.bind_entry_leg_from_brackets(split_pair, split_after, account=ACC, symbol=SYM,
                                                  action="BUY", qty=4) is not None)

# 経路 envelope: 子の id/receipt は routeSnapshot を通って engine に届く
results = [(ACC, "TP1", 200, '{"success": true}'), (ACC, "RUNNER", 200, '{"success": true}')]
snap = order.build_route_snapshot(results, split=True,
                                  identities={(ACC, "TP1"): ident, (ACC, "RUNNER"): ident2})
route = order.classify_route_results(results, split=True,
                                     identities={(ACC, "TP1"): ident, (ACC, "RUNNER"): ident2})
check("2 脚とも ACCEPTED → SENT", route["state"] == "SENT" and route["acceptedCount"] == 2, str(route))
parsed = route_envelope.parse(route_envelope.format_envelope(snap, "SENT"), expected_accounts=[ACC])
check("envelope は bracketOrderIds / identitySource を透過する",
      parsed["ok"] and parsed["snapshot"][0]["bracketOrderIds"] == ["651929060154", "651929060155"]
      and parsed["snapshot"][0]["identitySource"] == "broker-brackets"
      and parsed["snapshot"][1]["orderId"] == P2, str(parsed))
bad = [dict(snap[0], bracketOrderIds=["x"]), snap[1]]
check("壊れた付加情報は envelope ごと拒否",
      not route_envelope.parse(route_envelope.format_envelope(bad, "SENT"), expected_accounts=[ACC])["ok"])

# 最終パス: 脚ごとの窓が取りこぼし、送信前の窓に 2 対が現れている
late = route_identity.late_bind_entry_legs(
    empty, both, account=ACC, symbol=SYM, action="BUY", order_type="MARKET", entry_price=None,
    legs=[("TP1", 4), ("RUNNER", 4)], bound={})
check("最終パス: 候補数 = 未束縛の脚数なら作成順(TP1 → RUNNER)で割り当て",
      late.get("TP1", {}).get("orderId") == P1 and late.get("RUNNER", {}).get("orderId") == P2, str(late))
late_qty = route_identity.late_bind_entry_legs(
    empty, both, account=ACC, symbol=SYM, action="BUY", order_type="MARKET", entry_price=None,
    legs=[("TP1", 3), ("RUNNER", 5)], bound={}, parent_qty={P1: 5, P2: 3})
check("最終パス: 枚数の証拠があれば作成順より枚数(TP1=3 → 後の親)",
      late_qty.get("TP1", {}).get("orderId") == P2 and late_qty.get("RUNNER", {}).get("orderId") == P1,
      str(late_qty))
late_one = route_identity.late_bind_entry_legs(
    empty, both, account=ACC, symbol=SYM, action="BUY", order_type="MARKET", entry_price=None,
    legs=[("TP1", 4), ("RUNNER", 4)], bound={"TP1": ident})
check("最終パス: 束縛済みの脚を除いた残り 1 対を RUNNER へ",
      list(late_one) == ["RUNNER"] and late_one["RUNNER"]["orderId"] == P2, str(late_one))
check("最終パス: 候補が脚より多ければ何も束縛しない",
      route_identity.late_bind_entry_legs(
          empty, both, account=ACC, symbol=SYM, action="BUY", order_type="MARKET",
          entry_price=None, legs=[("RUNNER", 4)], bound={}) == {})
check("最終パス: 枚数の証拠が凍結脚と矛盾すれば束縛しない",
      route_identity.late_bind_entry_legs(
          empty, both, account=ACC, symbol=SYM, action="BUY", order_type="MARKET",
          entry_price=None, legs=[("TP1", 3), ("RUNNER", 5)], bound={},
          parent_qty={P1: 4, P2: 4}) == {})


print("\n--- (d) 指値の親行による従来の束縛は変わらない ---")
limit_parent = parent(651929060212, status="WORKING", ts="2026-09-08T10:49:01.173Z")
limit_children = [child(651929060213, 651929060214, parent=651929060212, status="SUSPENDED",
                        ts="2026-09-08T10:49:01.173Z"),
                  child(651929060214, 651929060213, status="SUSPENDED", ts="2026-09-08T10:49:01.173Z")]
seq = [empty, view([limit_parent] + limit_children)]
calls.clear()
slept.clear()
ident_limit, _ = order._entry_orders_identity(
    SYM, empty, account=ACC, action="BUY", qty=1, order_type="LIMIT", entry_price=29595.0,
    snapshot=snapshot, augment=no_augment, sleep=slept.append)
check("指値: WORKING の親行が現れた読みで従来どおり親に束縛(identitySource は broker-orders)",
      ident_limit is not None and ident_limit["orderId"] == "651929060212"
      and ident_limit["identitySource"] == "broker-orders" and ident_limit["status"] == "WORKING"
      and len(calls) == 2, str(ident_limit))
check("指値: 同じ窓にその親を名乗る子 2 行があれば id だけ添える(束縛の可否は変えない)",
      ident_limit is not None
      and ident_limit.get("bracketOrderIds") == ["651929060213", "651929060214"], str(ident_limit))
seq = [empty, view([limit_parent])]
calls.clear()
ident_limit_only, _ = order._entry_orders_identity(
    SYM, empty, account=ACC, action="BUY", qty=1, order_type="LIMIT", entry_price=29595.0,
    snapshot=snapshot, augment=no_augment, sleep=slept.append)
check("指値: 子が見えなければ従来どおり親 id だけ(bracketOrderIds なし)",
      ident_limit_only is not None and ident_limit_only["orderId"] == "651929060212"
      and "bracketOrderIds" not in ident_limit_only, str(ident_limit_only))
seq = [view([parent(1), parent(2)])]
calls.clear()
check("before が無ければ 1 回だけ読んで束縛しない(従来どおり)",
      order._entry_orders_identity(SYM, None, account=ACC, action="BUY", qty=1, order_type="LIMIT",
                                   entry_price=29595.0, snapshot=snapshot, augment=no_augment,
                                   sleep=slept.append)[0] is None and len(calls) == 1)
seq = [None, None, None]
calls.clear()
slept.clear()
none_ident, none_after = order._entry_orders_identity(
    SYM, empty, account=ACC, action="BUY", qty=1, order_type="LIMIT", entry_price=29595.0,
    snapshot=snapshot, augment=no_augment, sleep=slept.append, attempts=3, delay=0.5)
check("一覧がずっと None なら上限まで読んで (None, None)",
      none_ident is None and none_after is None and len(calls) == 3 and slept == [0.5, 0.5])
seq = [None] * 10
calls.clear()
slept.clear()
ticks = iter([0.0, 7.0, 13.0, 22.0, 30.0])
order._entry_orders_identity(
    SYM, empty, account=ACC, action="BUY", qty=1, order_type="LIMIT", entry_price=29595.0,
    snapshot=snapshot, augment=no_augment, sleep=slept.append, attempts=10, delay=1.0,
    budget=15.0, clock=lambda: next(ticks))
check("壁時計の予算(15s)を使い切ったら回数が残っていても読みに入らない(応答 7s の照会で 3 回)",
      len(calls) == 3, f"{len(calls)} {slept}")


print("\n--- binder: 親行が無くても凍結した子 2 行の構造で所有する ---")
PLAN = {"planVersion": "R19-ICT-SPLIT-1", "entryKey": "ENTRY:" + "f" * 64, "accountScope": [ACC],
        "scenarioId": "15773986526cba6b", "symbol": SYM, "model": "VP80_REVERSION", "grade": "A",
        "side": "BUY", "qty": 8, "entry": 29595.0, "initialStop": 29576.5, "riskPoints": 18.5,
        "riskDollars": 296.0, "riskCapDollars": 2491.0, "riskCapSource": "ACCOUNT_DRAWDOWN_BUFFER",
        "tp1": 29626.25, "finalTarget": 29649.0, "targets": [29626.25, 29649.0],
        "legs": [{"id": "TP1", "qty": 4, "target": 29626.25}, {"id": "RUNNER", "qty": 4, "target": 29649.0}],
        "trailDistance": 14.0, "mode": "SPLIT_BRACKETS_TP1_RUNNER", "ultra": True,
        "ultraDrawdown": 2491.0, "entryOrderType": "MARKET", "entryReference": 29590.0}
SNAPSHOT = [{"accountId": ACC, "legId": "TP1", "state": "ACCEPTED", **ident},
            {"accountId": ACC, "legId": "RUNNER", "state": "ACCEPTED", **ident2}]


def position(qty=8, avg=29585.875):
    return {"verified": True, "source": "crosstrade-rest", "platform": "TRADOVATE",
            "account": "64968904", "accountId": ACC, "brokerAccountId": "64968904", "symbol": SYM,
            "side": "LONG", "qty": qty, "avgEntry": avg, "filledAt": TS, "orderId": "POS-8",
            "receipt": None, "observedAt": "2026-09-08T10:34:05.000Z"}


def generation(pos):
    return f"PG:1:{ae._position_generation(pos)}"


res = ownership_binder.bind(PLAN, SNAPSHOT, position(), view(PAIR1 + PAIR2), route_state="SENT",
                            position_generation=generation(position()))
check("親行なし・子 4 本 live → OWNED_FULL(構造照合)", res["owned"] and res["state"] == "OWNED_FULL",
      res.get("reason"))
check("成行の有利側 4.125pt(SL 幅 13.5pt 以内)は所有(R75 と同じ理由)",
      res["owned"] and abs(res["ownedPlan"]["entry"] - 29585.875) < 1e-9, res.get("reason"))
res = ownership_binder.bind(PLAN, SNAPSHOT, position(avg=29592.5), view(PAIR1 + PAIR2),
                            route_state="SENT", position_generation=generation(position(avg=29592.5)))
check("R79: 台帳の身元が取れていれば成行の不利側 2.5pt でも所有する",
      res["owned"] and res["state"] == "OWNED_FULL", res.get("reason"))
res = ownership_binder.bind(PLAN, SNAPSHOT, position(avg=29570.0), view(PAIR1 + PAIR2),
                            route_state="SENT", position_generation=generation(position(avg=29570.0)))
check("R79: SL 幅を超えて有利な建玉も身元が取れていれば所有する",
      res["owned"] and res["state"] == "OWNED_FULL", res.get("reason"))
with manual_ownership_halt():
    halted_adverse = ownership_binder.bind(
        PLAN, SNAPSHOT, position(avg=29592.5), view(PAIR1 + PAIR2), route_state="SENT",
        position_generation=generation(position(avg=29592.5)))
    halted_favorable = ownership_binder.bind(
        PLAN, SNAPSHOT, position(avg=29570.0), view(PAIR1 + PAIR2), route_state="SENT",
        position_generation=generation(position(avg=29570.0)))
check("R79 緊急停止 ON: 不利側 2.5pt は従来どおり所有しない",
      not halted_adverse["owned"] and "deviates" in str(halted_adverse.get("reason")),
      str(halted_adverse.get("reason")))
check("R79 緊急停止 ON: SL 幅を超えて有利な建玉も従来どおり所有しない",
      not halted_favorable["owned"] and "deviates" in str(halted_favorable.get("reason")),
      str(halted_favorable.get("reason")))
check("R79 緊急停止は解除後に元へ戻る", ownership_binder.ledger_backed_ownership() is True)
res = ownership_binder.bind(PLAN, SNAPSHOT, position(qty=4), view(PAIR2), route_state="SENT",
                            position_generation=generation(position(qty=4)))
check("TP1 後(TP1 の子は消費済み・RUNNER の子は live)→ runner 継続の PARTIAL_FILL",
      res["owned"] and res["state"] == "PARTIAL_FILL" and res["ownedPlan"]["partialLegId"] == "RUNNER",
      res.get("reason"))
res = ownership_binder.bind(PLAN, SNAPSHOT, position(), view(PAIR1[:1] + PAIR2), route_state="SENT",
                            position_generation=generation(position()))
check("子が片方だけ残るのは矛盾 → 所有しない", not res["owned"] and "inconsistent" in str(res["reason"]),
      res.get("reason"))
res = ownership_binder.bind(PLAN, SNAPSHOT, position(), view([]), route_state="SENT",
                            position_generation=generation(position()))
check("全脚の子が 1 本も無ければ所有しない(建玉だけでは結べない)",
      not res["owned"] and "no live structure" in str(res["reason"]), res.get("reason"))
res = ownership_binder.bind(PLAN, SNAPSHOT, position(), view([PARENT1, PARENT2] + PAIR1 + PAIR2),
                            route_state="SENT", position_generation=generation(position()))
check("親行が読み足されていれば従来の親行照合で OWNED_FULL", res["owned"] and res["state"] == "OWNED_FULL",
      res.get("reason"))


print("\n--- (b) 後周期: UNKNOWN の経路をブラケット構造から再束縛する ---")
CLAIMED_AT = "2026-09-08T10:33:44.901133+00:00"
HALT_AT = "2026-09-08T10:34:16.898283+00:00"
KEY = PLAN["entryKey"]


def seed(ledger, halt=True, extra=None):
    ae._append_ledger({"key": KEY, "entryKey": KEY, "status": "ENTRY_CLAIMED", "action": "ENTRY_CLAIM",
                       "plan": PLAN, "claimJournal": {"entryKey": KEY, "claimToken": "t"},
                       "time": CLAIMED_AT}, ledger)
    if halt:
        ae._append_ledger({"key": KEY, "entryKey": KEY, "status": "HALT", "action": "ENTRY", "plan": PLAN,
                           "reason": "live send failed/unknown:\nROUTE NOT_ALL_ACCEPTED",
                           "time": HALT_AT}, ledger)
    for row in extra or []:
        ae._append_ledger(row, ledger)


def bundle(price=29610.0):
    t0 = int(datetime(2026, 9, 8, 10, 36, tzinfo=timezone.utc).timestamp())
    return {"price": price, "scenarios": {"primary": {"symbol": SYM}},
            "snapshot": {"bars3m": [{"t": t0, "o": price, "h": 29620.0, "l": price - 3, "c": price},
                                    {"t": t0 + 180, "o": price, "h": price + 1, "l": price - 1, "c": price}]}}


def run(ledger, pos, rows, parents=(PARENT1, PARENT2), fills=None, cycles=1, price=29610.0):
    notes_all, sends = [], []

    def position_query(_symbol, account=None):
        return pos

    def order_query(_symbol, known_order_ids=None, account=None):
        extra = [row for row in parents if str(row["orderId"]) in {str(v) for v in (known_order_ids or [])}]
        return view(list(rows) + extra)

    def runner(args, confirm):
        sends.append((list(args), confirm))
        return 0, "dry ok"

    for _ in range(cycles):
        notes = ae.reconcile(bundle(price), cfg={"NQX_AUTOTRADE": "1"}, broker_query=position_query,
                             broker_order_query=order_query, state_query=lambda: {}, ledger_path=ledger,
                             runner=runner, broker_fills_query=fills)
        notes_all.append(notes)
    records, _ = ae._read_ledger(ledger)
    return notes_all, records, sends


with tempfile.TemporaryDirectory() as tmp:
    ledger = os.path.join(tmp, "ledger.jsonl")
    seed(ledger)
    records, _ = ae._read_ledger(ledger)
    check("前提: 送信失敗/不明の HALT が新規を塞いでいる", ae._has_halt(records) is not None)
    window = ae._route_send_window(records, KEY)
    check("送信窓は CLAIMED − 5s 〜 HALT + 5s",
          window is not None and window[0] == ae._parse_at(CLAIMED_AT) - ae.timedelta(seconds=5)
          and window[1] == ae._parse_at(HALT_AT) + ae.timedelta(seconds=5), str(window))
    notes, records, sends = run(ledger, position(), PAIR1 + PAIR2)
    check("再束縛の注記", any("rebound UNKNOWN route" in n for n in notes[0]), str(notes))
    bound_rows = [r for r in records if r.get("action") == "ENTRY_OWNERSHIP_BOUND"]
    check("台帳に ENTRY_SENT(ENTRY_OWNERSHIP_BOUND, routeState SENT, haltResolution)を追記",
          len(bound_rows) == 1 and bound_rows[0]["status"] == "ENTRY_SENT"
          and bound_rows[0]["routeState"] == "SENT"
          and bound_rows[0]["haltResolution"] == ae.HALT_RESOLUTION_REBOUND
          and bound_rows[0]["identitySource"] == "broker-brackets", str(bound_rows[:1])[:400])
    frozen = bound_rows[0]["routeSnapshot"] if bound_rows else []
    check("凍結経路は TP1=…153 / RUNNER=…171(作成順)+ 子 id",
          [r["legId"] for r in frozen] == ["TP1", "RUNNER"]
          and [r["orderId"] for r in frozen] == [P1, P2]
          and frozen[0]["bracketOrderIds"] == ["651929060154", "651929060155"]
          and frozen[1]["bracketOrderIds"] == ["651929060172", "651929060173"], str(frozen))
    check("所有済みプラン(positionOwnership・約定価格)が凍結される",
          isinstance(bound_rows[0]["plan"].get("positionOwnership"), dict)
          and abs(bound_rows[0]["plan"]["entry"] - 29585.875) < 1e-9, str(bound_rows[0]["plan"].get("entry")))
    check("HALT 行は消さず、_has_halt はもう塞がない",
          any(r.get("status") == "HALT" for r in records) and ae._has_halt(records) is None)
    halts = ae.list_halts(ledger)
    check("--list-halts は cleared と表示", halts and halts[0]["cleared"] is True, str(halts))
    check("送信なし(再束縛は読み取りだけ)", not sends)

    # 次周期: 同じ建玉 → 所有済みとして管理に入る(全量が残る間は何もしない)
    notes, records, sends = run(ledger, position(), PAIR1 + PAIR2)
    check("次周期は所有済み → 管理(全量残存中は hold: managed)",
          any(n.startswith("autotrade hold: managed") for n in notes[0]), str(notes))
    check("2 度目の ENTRY_OWNERSHIP_BOUND は書かない",
          sum(1 for r in records if r.get("action") == "ENTRY_OWNERSHIP_BOUND") == 1)

    # TP1 約定後: 建玉 4・RUNNER の対だけ live → runner の SL を建値+1pt 以上へ MODIFY(dry-run 提案)
    notes, records, sends = run(ledger, position(qty=4), PAIR2)
    check("TP1 後は runner 継続として MODIFY が提案される",
          any("proposal MODIFY" in n for n in notes[0]) and sends and "--modify" in sends[0][0],
          str(notes) + str(sends))
    modify_args = sends[0][0] if sends else []
    sl_index = modify_args.index("--sl") + 1 if "--sl" in modify_args else None
    check("MODIFY の SL は建値+1pt 以上(29606 = 高値 29620 − trail 14)",
          sl_index is not None and float(modify_args[sl_index]) == 29606.0, str(modify_args))

with tempfile.TemporaryDirectory() as tmp:
    ledger = os.path.join(tmp, "ledger.jsonl")
    seed(ledger, halt=False)
    notes, records, sends = run(ledger, position(), PAIR1 + PAIR2)
    check("CLAIMED だけ残った(HALT 行が無い)経路も、CLAIMED + 150s の窓で再束縛",
          any("rebound UNKNOWN route" in n for n in notes[0]), str(notes))

with tempfile.TemporaryDirectory() as tmp:
    ledger = os.path.join(tmp, "ledger.jsonl")
    unknown_plan = {**PLAN, "routeSnapshot": [
        {"accountId": ACC, "legId": "TP1", "state": "UNKNOWN", "orderId": None, "receipt": None},
        {"accountId": ACC, "legId": "RUNNER", "state": "UNKNOWN", "orderId": None, "receipt": None}]}
    seed(ledger, halt=False, extra=[{"key": KEY, "entryKey": KEY, "status": "ENTRY_PARTIAL_ROUTE",
                                     "action": "ENTRY", "plan": unknown_plan, "routeState": "UNKNOWN",
                                     "time": HALT_AT}])
    notes, records, sends = run(ledger, position(), PAIR1 + PAIR2)
    check("routeState=UNKNOWN の ENTRY_PARTIAL_ROUTE(全行 UNKNOWN)も再束縛",
          any("rebound UNKNOWN route" in n for n in notes[0]), str(notes))

with tempfile.TemporaryDirectory() as tmp:
    # TP1 が既に外れた後に初めて再束縛する(RUNNER の対だけ live)。約定履歴があれば TP1 の親も凍結
    ledger = os.path.join(tmp, "ledger.jsonl")
    seed(ledger)

    def fills(account):
        return {"verified": True, "account": account, "platform": "TRADOVATE", "fills": [
            {"id": "f1", "orderId": P1, "action": "BUY", "qty": 4, "price": 29585.75, "at": TS, "instrument": SYM},
            {"id": "f2", "orderId": P2, "action": "BUY", "qty": 4, "price": 29586.0, "at": TS, "instrument": SYM},
            {"id": "f3", "orderId": "651929060155", "action": "SELL", "qty": 4, "price": 29626.25,
             "at": "2026-09-08T10:40:00.000Z", "instrument": SYM}]}

    notes, records, sends = run(ledger, position(qty=4), PAIR2, fills=fills)
    bound_rows = [r for r in records if r.get("action") == "ENTRY_OWNERSHIP_BOUND"]
    check("runner だけ残る状態からの再束縛: 約定履歴で TP1 の親も凍結 → SENT / ENTRY_PARTIAL_FILL",
          any("rebound UNKNOWN route" in n for n in notes[0]) and bound_rows
          and bound_rows[0]["status"] == "ENTRY_PARTIAL_FILL" and bound_rows[0]["routeState"] == "SENT"
          and [r["orderId"] for r in bound_rows[0]["routeSnapshot"]] == [P1, P2]
          and bound_rows[0]["routeSnapshot"][0]["identitySource"] == "broker-fills", str(notes) + str(bound_rows)[:600])

with tempfile.TemporaryDirectory() as tmp:
    ledger = os.path.join(tmp, "ledger.jsonl")
    seed(ledger)
    notes, records, sends = run(ledger, position(qty=4), PAIR2)
    bound_rows = [r for r in records if r.get("action") == "ENTRY_OWNERSHIP_BOUND"]
    check("約定履歴が無ければ TP1 は UNKNOWN のまま PARTIAL で runner を所有(LIMITED_PARTIAL)",
          bound_rows and bound_rows[0]["routeState"] == "PARTIAL"
          and bound_rows[0]["status"] == "ENTRY_PARTIAL_FILL"
          and [r["state"] for r in bound_rows[0]["routeSnapshot"]] == ["UNKNOWN", "ACCEPTED"], str(notes) + str(bound_rows)[:600])


print("\n--- (c) 曖昧なら何も書かず UNKNOWN のまま ---")


def ambiguous(label, pos, rows, needle):
    with tempfile.TemporaryDirectory() as tmp:
        ledger = os.path.join(tmp, "ledger.jsonl")
        seed(ledger)
        notes, records, sends = run(ledger, pos, rows)
        bound_rows = [r for r in records if r.get("action") == "ENTRY_OWNERSHIP_BOUND"]
        check(label, not bound_rows and not sends and ae._has_halt(records) is not None
              and any("ownership UNKNOWN" in n and needle in n for n in notes[0]),
              str(notes))


ambiguous("同方向の親行(別の BUY が同居)→ UNKNOWN のまま", position(),
          PAIR1 + PAIR2 + [parent(651929060999, status="WORKING")], "another active entry-side order row")
ambiguous("枚数が split lifecycle 外(6 枚)→ UNKNOWN のまま", position(qty=6), PAIR1 + PAIR2,
          "outside the rebindable split lifecycle")
ambiguous("対が送信窓の外(10:40 作成)→ UNKNOWN のまま", position(),
          [child(1, 2, parent=3, ts="2026-09-08T10:40:00.000Z"), child(2, 1, ts="2026-09-08T10:40:00.000Z"),
           child(4, 5, parent=6, ts="2026-09-08T10:40:00.000Z"), child(5, 4, ts="2026-09-08T10:40:00.000Z")],
          "outside the send window")
ambiguous("対の数が脚の数と違う(8 枚に 1 対)→ UNKNOWN のまま", position(), PAIR1,
          "expected 2 live bracket pair(s), found 1")
ambiguous("相方の無い子(5 本目)→ UNKNOWN のまま", position(),
          PAIR1 + PAIR2 + [child(651929060990, 651929060991, parent=651929060989)],
          "do not form clean OCO pairs")
ambiguous("真の親 id を名乗らない対 → UNKNOWN のまま", position(),
          PAIR1 + [child(651929060172, 651929060173, ts="2026-09-08T10:34:05.312Z"),
                   child(651929060173, 651929060172, ts="2026-09-08T10:34:05.312Z")],
          "names no broker parent id")
with tempfile.TemporaryDirectory() as tmp:
    ledger = os.path.join(tmp, "ledger.jsonl")
    seed(ledger)
    notes, records, sends = run(ledger, position(avg=29592.5), PAIR1 + PAIR2)
    bound_rows = [r for r in records if r.get("action") == "ENTRY_OWNERSHIP_BOUND"]
    check("R79: 平均建値が不利側に 2pt 超(29592.5)でも台帳の身元で再束縛する",
          len(bound_rows) == 1 and any("rebound UNKNOWN route" in n for n in notes[0]), str(notes))

with manual_ownership_halt():
    ambiguous("R79 緊急停止 ON: 平均建値が不利側に 2pt 超(29592.5)→ binder が所有しない",
              position(avg=29592.5), PAIR1 + PAIR2, "deviates from frozen reference")

with tempfile.TemporaryDirectory() as tmp:
    ledger = os.path.join(tmp, "ledger.jsonl")
    seed(ledger)
    other = {**position(), "accountId": "OTHER", "account": "OTHER"}
    notes, records, sends = run(ledger, other, PAIR1 + PAIR2)
    check("凍結スコープ外の口座は再束縛せず、R46 の手動建玉の注記をそのまま返す",
          notes[0] == ["autotrade hold: broker position ownership UNKNOWN "
                       "(broker position account is outside frozen scope)"], str(notes))

print()
print(f"合計: PASS {PASS[0]} / FAIL {FAIL[0]}")
sys.exit(1 if FAIL[0] else 0)

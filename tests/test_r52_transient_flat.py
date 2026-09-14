# -*- coding: utf-8 -*-
"""R52: 一度の qty=0 観測を FLAT と確定しない。

2026-09-05 05:24:41、建玉 14 枚が生きている(fills に決済なし・所有ブラケット 4 本
WORKING)のに position 照会が一瞬 qty=0 を返し、engine が POSITION_GENERATION FLAT を
書いて startup recovery(失敗)へ落ち、管理が外れた。直近世代が open のときは
(1) 一度だけ再照会し、(2) なお 0 でも所有ブラケットが active なら矛盾する観測として
そのサイクルを止める(FLAT を記録しない)。

    python tests/test_r52_transient_flat.py
"""
import os
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
import autotrade_engine as ae  # noqa: E402

PASS = [0]
FAIL = [0]
ACC = "ACC-T"
SYM = "MNQU6"
AT = "2026-09-04T19:56:28.396Z"
ae._read_env_file = lambda: {}   # 本番 .secrets を読まない(hermetic)


def check(label, condition, detail=""):
    if condition:
        PASS[0] += 1
        print(f"  OK   {label}")
    else:
        FAIL[0] += 1
        print(f"  NG   {label}  {detail}")


def open_position(qty=14):
    return {"verified": True, "qty": qty, "side": "SHORT", "accountId": ACC, "symbol": SYM,
            "orderId": "POS-081", "receipt": None, "filledAt": AT, "avgEntry": 29574.625,
            "observedAt": AT}


def flat_position():
    return {"verified": True, "qty": 0, "side": None, "accountId": ACC, "symbol": SYM,
            "observedAt": AT}


def child(order_id, parent=None, status="WORKING"):
    return {"orderId": order_id, "accountId": ACC, "symbol": SYM, "status": status,
            "action": "BUY", "qty": None, "orderType": None, "limitPrice": None,
            "fieldsComplete": False, "parentId": None, "brokerParentId": parent,
            "receipt": f"REC-{order_id}"}


def parent(order_id):
    return {"orderId": order_id, "accountId": ACC, "symbol": SYM, "status": "FILLED",
            "action": "SELL", "qty": None, "orderType": None, "limitPrice": None,
            "fieldsComplete": False, "parentId": None, "brokerParentId": None,
            "receipt": f"REC-{order_id}"}


def orders(rows, state="PENDING"):
    return {"verified": True, "orders": rows, "state": state,
            "activeOrders": [r for r in rows if r["status"] == "WORKING"]}


PLAN = {"symbol": SYM, "accountScope": [ACC], "entryKey": "ENTRY:" + "a" * 64,
        "side": "SELL", "qty": 14, "entry": 29574.25, "stop": 29590.5,
        "targets": [29526.5, 29350.75],
        "routeSnapshot": [{"accountId": ACC, "legId": "TP1", "state": "ACCEPTED", "orderId": "475"},
                          {"accountId": ACC, "legId": "RUNNER", "state": "ACCEPTED", "orderId": "493"}]}


def seed(ledger):
    ae._append_ledger({"key": "ENTRY:" + "a" * 64, "entryKey": "ENTRY:" + "a" * 64,
                       "status": "ENTRY_SENT", "action": "ENTRY", "plan": PLAN}, ledger)
    ae._append_ledger({"key": f"POSITION_GENERATION:{ACC}:5:POS:x", "status": "POSITION_GENERATION",
                       "generation": 5, "identity": "POS:x", "open": True, "accountId": ACC,
                       "symbol": SYM, "side": "SHORT"}, ledger)


def run(position_reads, order_rows):
    reads = list(position_reads)
    calls = []
    sends = []

    def query(_symbol):
        calls.append(1)
        return reads.pop(0) if len(reads) > 1 else reads[0]

    with tempfile.TemporaryDirectory() as tmp:
        ledger = os.path.join(tmp, "ledger.jsonl")
        seed(ledger)
        notes = ae.reconcile({"scenarios": {"primary": {"symbol": SYM}}},
                             cfg={"NQX_AUTOTRADE": "1"},
                             broker_query=query,
                             broker_order_query=lambda _s, **_k: orders(order_rows),
                             state_query=lambda: {}, ledger_path=ledger,
                             runner=lambda *_a: (sends.append(True) or (0, "unexpected")))
        records, _ = ae._read_ledger(ledger)
    flat_written = any(r.get("status") == "POSITION_GENERATION" and r.get("open") is False
                       for r in records)
    return notes, flat_written, len(calls), sends


BRACKETS = [parent("475"), parent("493"), child("476", "475"), child("477"), child("494", "493"), child("495")]

# (1) 一瞬 0 → 再照会で 14: FLAT を書かず建玉として続行
notes, flat, n, sends = run([flat_position(), open_position()], BRACKETS)
check("再照会で建玉が見えれば FLAT を記録しない", not flat, str(notes))
check("再照会は一度だけ", n == 2, str(n))
check("startup recovery へ落ちない", not any("startup recovery" in x for x in notes), str(notes))
check("送信なし", not sends)

# (2) 2 回とも 0 だが所有ブラケットが active: 矛盾として止める(FLAT を記録しない)
notes, flat, n, sends = run([flat_position(), flat_position()], BRACKETS)
check("所有ブラケット active なら FLAT を記録しない", not flat, str(notes))
check("理由に transient observation", any("transient observation" in x for x in notes), str(notes))
check("送信なし(2)", not sends)

# (3) 2 回とも 0 で所有ブラケットも無い(本当に決済済み): 従来どおり FLAT を記録
closed_rows = [parent("475"), parent("493"), child("476", "475", status="FILLED"),
               child("477", status="CANCELED"), child("494", "493", status="FILLED"),
               child("495", status="CANCELED")]
notes, flat, n, sends = run([flat_position(), flat_position()], closed_rows)
check("本当に決済済みなら FLAT を記録する", flat, str(notes))
check("送信なし(3)", not sends)

# (4) 直近世代が open でなければ再照会しない(FLAT 口座の通常サイクルを遅くしない)
reads = []


def query_once(_symbol):
    reads.append(1)
    return flat_position()


with tempfile.TemporaryDirectory() as tmp:
    ledger = os.path.join(tmp, "ledger.jsonl")
    ae._append_ledger({"key": "POSITION_GENERATION:5:FLAT", "status": "POSITION_GENERATION",
                       "generation": 5, "open": False, "accountId": ACC}, ledger)
    ae.reconcile({"scenarios": {"primary": {"symbol": SYM}}}, cfg={"NQX_AUTOTRADE": "1"},
                 broker_query=query_once, broker_order_query=lambda _s, **_k: orders([], state="NONE"),
                 state_query=lambda: {}, ledger_path=ledger, runner=lambda *_a: (0, "unexpected"))
check("直近世代が閉じていれば再照会しない", len(reads) == 1, str(len(reads)))

# (5) 所有ブラケット判定そのもの
check("brokerParentId が ACCEPTED 親なら active 扱い", ae._owned_brackets_active(orders(BRACKETS), PLAN))
check("他人の子(親 id 不一致)は無視", not ae._owned_brackets_active(orders([child("9", "8")]), PLAN))
check("ACCEPTED 行が無ければ False", not ae._owned_brackets_active(orders(BRACKETS), {"routeSnapshot": []}))

# (6) R84 合成プラン。top-level の routeSnapshot が無く、統合後の 1 組は **親を
#     持たない** OCO 兄弟なので、親リンクだけを見ていると合成建玉のときだけ保護が
#     外れる —— 生きている建玉に FLAT 行を書き、管理が丸ごと落ちる(R52 と同じ事故)。
COMPOSITE = {"planKind": "PYRAMID_COMPOSITE", "tranches": [
    {"trancheId": "T1", "legs": [{"id": "TP1", "orderId": "701"},
                                 {"id": "RUNNER", "orderId": "702"}]}]}
check("合成プランは脚の orderId を親として辿る",
      ae._owned_brackets_active(orders([child("711", "701")]), COMPOSITE))
check("合成プランでも他人の子は無視",
      not ae._owned_brackets_active(orders([child("711", "999")]), COMPOSITE))

CONSOLIDATED = {"planKind": "PYRAMID_COMPOSITE", "tranches": [
    {"trancheId": "C1", "legs": [{"id": "RUNNER"}],
     "consolidationPair": {"orderIds": ["821", "822"]}}]}
# 統合後の対は親を持たない(`brokerParentId=None`)。id そのものが「建玉を守っている
# 注文」の証拠になる。
check("統合後の 1 組は親リンク無しでも active と読む",
      ae._owned_brackets_active(orders([child("821"), child("822")]), CONSOLIDATED))
check("その対が両方とも終端なら FLAT を妨げない",
      not ae._owned_brackets_active(
          orders([child("821", status="FILLED"), child("822", status="CANCELED")]),
          CONSOLIDATED))
check("top-level の consolidation.orderIds も同じ扱い",
      ae._owned_brackets_active(orders([child("831")]),
                                {"consolidation": {"orderIds": ["831", "832"]}}))
check("統合の id と無関係な行は無視",
      not ae._owned_brackets_active(orders([child("999")]), CONSOLIDATED))

print()
print(f"合計: PASS {PASS[0]} / FAIL {FAIL[0]}")
sys.exit(1 if FAIL[0] else 0)

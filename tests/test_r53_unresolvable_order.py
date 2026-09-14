# -*- coding: utf-8 -*-
"""R53: ブローカーが解決できない凍結注文IDで経路全体を止めない。

2026-09-07、09-04 の凍結プランが持つ注文ID(649589730475 / 649589730493)へ
CrossTrade が 400 `{"success": false, "error": "tradovate_rejected"}` を返すように
なった。per-order 詳細の HTTPError を一律 Unavailable にしていたため注文照会が
**恒久的に** UNVERIFIED になり、reconcile の最初の門で 28 サイクル連続で全部の
新規が止まった。古い注文IDは二度と解決しないので、不明側へ倒しても永久に解けない。

この回帰は3層を固定する。

1. 解決できないIDは行として足さず `unresolvedOrderIds` に残し、一覧側の真実で
   観測を成立させる(verified=True)。転送エラーや別コードは従来どおり Unavailable。
2. ID による終端証明が原理的に組めない一件は、不在の証明(建玉0・未終端注文ゼロ)
   へ落とす。ブローカーが空でなければ従来どおり解放しない。
3. position と orders の観測時刻が契約の skew を超えたら取り直す。CrossTrade の
   応答は 0.6〜6.2 秒とばらつき、遅い方を引くと COMPONENT_SKEW で回復が落ちた。

    python tests/test_r53_unresolvable_order.py
"""
import io
import json
import os
import sys
import urllib.error

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
import broker_status as bs  # noqa: E402
import autotrade_engine as ae  # noqa: E402

PASS = [0]
FAIL = [0]
ACC = "ACC-T"
SYM = "MNQU6"


def check(label, condition, detail=""):
    if condition:
        PASS[0] += 1
        print(f"  OK   {label}")
    else:
        FAIL[0] += 1
        print(f"  NG   {label}  {detail}")


def http_error(code, body):
    return urllib.error.HTTPError(
        "https://example.invalid/orders/1", code, "err", {},
        io.BytesIO(json.dumps(body).encode("utf-8")))


# ------------------------------------------------------------------ 1. 照会層
print("\n1. 解決できないIDで観測ごと落とさない")

check("400 + success:false は「解決できないID」",
      bs._order_unresolvable(http_error(400, {"success": False, "error": "tradovate_rejected"})))
check("404 + success:false も同義",
      bs._order_unresolvable(http_error(404, {"success": False})))
check("500 は不明側(取り直す余地がある)",
      not bs._order_unresolvable(http_error(500, {"success": False})))
check("success:true の 400 は判定しない",
      not bs._order_unresolvable(http_error(400, {"success": True})))
check("本文が JSON でなければ不明側",
      not bs._order_unresolvable(urllib.error.HTTPError(
          "https://example.invalid/o/1", 400, "err", {}, io.BytesIO(b"<html>"))))

normalized = bs.normalize_crosstrade_orders(
    {"success": True, "data": []}, platform="TRADOVATE", account=ACC, symbol=SYM,
    unresolved_order_ids=["475", "475", "493"])
check("verified は一覧側の真実で立つ", normalized["verified"] is True)
check("解決できないIDは重複を畳んで残す",
      normalized["unresolvedOrderIds"] == ["475", "493"], str(normalized["unresolvedOrderIds"]))
check("行としては足さない", normalized["orders"] == [] and normalized["openCount"] == 0)
check("凍結IDを渡さなければ空", bs.normalize_crosstrade_orders(
    {"success": True, "data": []}, platform="TRADOVATE", account=ACC,
    symbol=SYM)["unresolvedOrderIds"] == [])


# ------------------------------------------------- 2. ID で証明できない一件の回復
print("\n2. ID で証明できない一件は不在の証明へ落とす")

FROZEN = [{"accountId": ACC, "legId": "TP1", "state": "ACCEPTED", "orderId": "475"},
          {"accountId": ACC, "legId": "RUNNER", "state": "ACCEPTED", "orderId": "493"}]
CLAIM = {"entryKey": "ENTRY:" + "a" * 64, "executionIntentHash": "xi_test",
         "routeSnapshot": FROZEN,
         "executionIntent": {"symbol": SYM, "accountScope": [ACC]}}
JOURNAL = {"claimToken": "tok"}


def flat(observed="2026-09-07T18:00:00+09:00"):
    return {"verified": True, "qty": 0, "side": None, "accountId": ACC,
            "symbol": SYM, "observedAt": observed}


def order_view(unresolved=(), rows=(), observed="2026-09-07T18:00:00+09:00"):
    rows = list(rows)
    active = [row for row in rows if row.get("status") != "FILLED"]
    return {"verified": True, "symbol": SYM, "platform": "TRADOVATE",
            "state": "PENDING" if active else "NONE", "orders": rows,
            "activeOrders": active, "openCount": len(active),
            "unresolvedOrderIds": list(unresolved), "observedAt": observed}


def run_recovery(order_result, recovered=(False, {"reason": "ENTRY_CLAIM_RECOVERY_UNVERIFIED"})):
    """publish / CAS を差し替えて回復判定だけを走らせる。"""
    import nqx_state
    published = []
    original_publish = nqx_state.publish_broker_observation
    original_recover = nqx_state.recover_entry_claim
    nqx_state.publish_broker_observation = lambda *a, **k: (
        published.append(a) or (True, {"observation": {"snapshotId": "bs_x"}}))
    nqx_state.recover_entry_claim = lambda *a, **k: recovered
    try:
        return ae._recover_entry_from_current_broker(
            CLAIM, JOURNAL, lambda _s: flat(), lambda _s, **_k: order_result, SYM), published
    finally:
        nqx_state.publish_broker_observation = original_publish
        nqx_state.recover_entry_claim = original_recover


(ok, detail), published = run_recovery(order_view(unresolved=["475", "493"]))
check("空のブローカーなら回復を試みるところまで進む", len(published) == 1, str(published))
check("DO が拒んだら identityUnresolvable を載せる",
      isinstance(detail, dict) and detail.get("identityUnresolvable") is True, str(detail))

(ok, detail), published = run_recovery(
    order_view(unresolved=["475", "493"],
               rows=[{"orderId": "999", "status": "WORKING", "accountId": ACC, "symbol": SYM}]))
check("未終端の注文が残っていれば解放しない",
      ok is False and detail.get("reason") == "ENTRY_RECOVERY_BROKER_NOT_EMPTY", str(detail))
check("その場合は観測を publish しない", published == [], str(published))

(ok, detail), _ = run_recovery(
    order_view(rows=[{"orderId": "475", "status": "FILLED", "accountId": ACC, "symbol": SYM},
                     {"orderId": "493", "status": "FILLED", "accountId": ACC, "symbol": SYM}]),
    recovered=(True, {"ok": True}))
check("解決できるIDは従来どおり ID ごとに終端を照合する", ok is True, str(detail))

(ok, detail), _ = run_recovery(
    order_view(rows=[{"orderId": "475", "status": "FILLED", "accountId": ACC, "symbol": SYM}]))
check("片方しか終端を示せなければ従来どおり不合格",
      ok is False and detail.get("reason") in {
          "ENTRY_RECOVERY_CURRENT_BROKER_PROOF_INVALID", "ENTRY_RECOVERY_BROKER_NOT_EMPTY"},
      str(detail))


# ------------------------------------------------------------ 3. skew の取り直し
print("\n3. component skew は契約を緩めず取り直す")

calls = [0]


def skewed_position(_symbol):
    calls[0] += 1
    # 1周目だけ orders から 5 秒離れた観測を返す(契約の許容は 2 秒)。
    return flat("2026-09-07T18:00:05+09:00" if calls[0] <= 2 else "2026-09-07T18:00:00+09:00")


before, orders_view, after = ae._stable_broker_snapshot(
    skewed_position, lambda _s, **_k: order_view(), SYM, ["475"])
check("skew を超えたら読み直す", calls[0] > 2, f"position reads={calls[0]}")
check("収まった観測を返す", after["observedAt"] == "2026-09-07T18:00:00+09:00", str(after))

steady = [0]


def steady_position(_symbol):
    steady[0] += 1
    return flat()


ae._stable_broker_snapshot(steady_position, lambda _s, **_k: order_view(), SYM, ["475"])
check("収まっていれば1回で終える", steady[0] == 2, f"position reads={steady[0]}")

hopeless = [0]


def hopeless_position(_symbol):
    hopeless[0] += 1
    return flat("2026-09-07T18:00:30+09:00")


ae._stable_broker_snapshot(hopeless_position, lambda _s, **_k: order_view(), SYM, ["475"])
check("収まらなくても無限に回さない", hopeless[0] == 6, f"position reads={hopeless[0]}")

# ------------------------------------------- 4. 取引日を跨いだ決済約定の取り残し
print("\n4. fills の窓が閉じた往復は退避して計数を戻す")

import trade_journal as tj  # noqa: E402

STRANDED = {"position": -7,
            "trade": {"side": "SHORT", "openedAt": "2026-09-04T19:56:28.400000+00:00",
                      "opens": [{"qty": 7, "price": 29574.25}, {"qty": 7, "price": 29575.0}],
                      "closes": [{"qty": 7, "price": 29526.5}]}}

check("ブローカー FLAT + 24時間超なら退避対象",
      tj._fills_window_closed(STRANDED, 0, "2026-09-07T18:41:28+09:00") is not None)
check("同じ取引日なら従来どおり待つ",
      tj._fills_window_closed(STRANDED, 0, "2026-09-04T20:41:28+00:00") is None)
check("ブローカーに建玉があるなら退避しない",
      tj._fills_window_closed(STRANDED, -7, "2026-09-07T18:41:28+09:00") is None)
check("約定列も 0 なら退避しない",
      tj._fills_window_closed({"position": 0, "trade": STRANDED["trade"]}, 0,
                              "2026-09-07T18:41:28+09:00") is None)
check("openedAt が読めなければ退避しない",
      tj._fills_window_closed({"position": -7, "trade": {"side": "SHORT"}}, 0,
                              "2026-09-07T18:41:28+09:00") is None)
check("+09:00 と UTC を混ぜても絶対時刻で判定する",
      tj._fills_window_closed(
          {"position": -7, "trade": {**STRANDED["trade"],
                                     "openedAt": "2026-09-07T09:00:00+00:00"}},
          0, "2026-09-07T18:41:28+09:00") is None)

print()
print(f"合計: PASS {PASS[0]} / FAIL {FAIL[0]}")
sys.exit(1 if FAIL[0] else 0)

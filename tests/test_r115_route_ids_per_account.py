# -*- coding: utf-8 -*-
"""R115: 凍結 route snapshot の注文 ID は **照会する口座** で絞る。

2026-09-19 01:50、7 口座への ENTRY が CrossTrade で 14/14 受理された直後、送信後照合が
`crosstrade order row account is outside configured scope` で UNVERIFIED になり HALT した。
routeSnapshot は行ごとに accountId を持つのに、engine が **14 件全部の注文 ID を各口座の
照会に渡していた**ため、アダプタが他口座の注文行を(正しく)弾いた。以後の全サイクルの
観測と RECOVER も同じ理由で止まった。1 口座では起きない多口座固有の穴。

ここで固定するのは 3 か所:
  1. ヘルパ `_route_rows_for_account` の絞り方(accountId 一致 / 旧い行 / 口座不明)。
  2. 回復(`_recover_entry_from_current_broker`)が **その口座の ID だけ** で照会すること。
  3. 観測と送信後照合のソースが同じヘルパを使うこと。

ネットワークは使わない(fake query を注入する)。

    python tests/test_r115_route_ids_per_account.py
"""
import inspect
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

for _stream in ("stdout", "stderr"):
    _file = getattr(sys, _stream, None)
    if _file is not None and hasattr(_file, "reconfigure"):
        try:
            _file.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

import autotrade_engine as ae   # noqa: E402

PASS = [0]
FAIL = [0]


def check(label, condition, detail=""):
    if condition:
        PASS[0] += 1
        print(f"  OK   {label}")
    else:
        FAIL[0] += 1
        print(f"  NG   {label}  {detail}")


A, B = "LFE00000000000026", "MFFUEVREOD000000002"
ROUTE = [
    {"accountId": A, "legId": "TP1", "state": "ACCEPTED", "orderId": "1001"},
    {"accountId": A, "legId": "RUNNER", "state": "ACCEPTED", "orderId": "1002"},
    {"accountId": B, "legId": "TP1", "state": "ACCEPTED", "orderId": "2001"},
    {"accountId": B, "legId": "RUNNER", "state": "ACCEPTED", "orderId": "2002"},
    {"legId": "TP1", "state": "ACCEPTED", "orderId": "9"},          # 旧い行(口座なし)
    "not-a-row",
]

# ================================================================
print("=" * 68)
print("1. ヘルパ: 照会する口座の行だけ残す")
print("=" * 68)

ids = lambda rows: [r["orderId"] for r in rows]   # noqa: E731
check("A で絞ると A の行 + 口座なしの旧い行", ids(ae._route_rows_for_account(ROUTE, A)) == ["1001", "1002", "9"],
      str(ids(ae._route_rows_for_account(ROUTE, A))))
check("B で絞ると B の行 + 旧い行", ids(ae._route_rows_for_account(ROUTE, B)) == ["2001", "2002", "9"])
check("口座が空なら全行(単一口座の旧経路)", ids(ae._route_rows_for_account(ROUTE, "")) == ["1001", "1002", "2001", "2002", "9"])
check("None も全行", len(ae._route_rows_for_account(ROUTE, None)) == 5)
check("dict でない行は捨てる", all(isinstance(r, dict) for r in ae._route_rows_for_account(ROUTE, None)))
check("rows が None でも落ちない", ae._route_rows_for_account(None, A) == [])

# ================================================================
print("=" * 68)
print("2. 回復はその口座の注文 ID だけで照会する")
print("=" * 68)


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def strict_order_query(symbol, known_order_ids=None):
    """アダプタの挙動を模す: 他口座の注文 ID が混ざっていたら scope 外として拒否。"""
    seen_ids.append(list(known_order_ids or []))
    foreign = [i for i in (known_order_ids or []) if not str(i).startswith("1")]
    if foreign:
        raise RuntimeError(f"crosstrade order row account is outside configured scope ({foreign})")
    return {"verified": False, "symbol": symbol, "observedAt": now_iso(),
            "detail": "テスト: ここで止める(不在証明は publish しない)"}


def position_query_A(symbol):
    return {"verified": True, "qty": 0, "side": "FLAT", "accountId": A,
            "symbol": symbol, "observedAt": now_iso()}


claim = {"routeSnapshot": ROUTE[:4], "executionIntentHash": "xi_test"}
seen_ids = []
ok, detail = ae._recover_entry_from_current_broker(claim, {}, position_query_A, strict_order_query, "MNQZ6")
check("A の回復は他口座の ID を渡さない(アダプタに弾かれない)",
      seen_ids and all(set(s) <= {"1001", "1002"} for s in seen_ids), str(seen_ids))
check("A の回復は A の ID を全部渡す", seen_ids and seen_ids[0] == ["1001", "1002"], str(seen_ids))
check("不在証明が組めなければ従来どおり PROOF_INVALID(推測で解放しない)",
      ok is False and isinstance(detail, dict)
      and detail.get("reason") == "ENTRY_RECOVERY_CURRENT_BROKER_PROOF_INVALID", str(detail))

# 口座が分からない(旧経路)なら従来どおり全 ID
seen_ids = []
ae._recover_entry_from_current_broker(
    claim, {}, lambda s: {"verified": True, "qty": 0, "symbol": s, "observedAt": now_iso()},
    lambda s, known_order_ids=None: (seen_ids.append(list(known_order_ids or [])) or
                                     {"verified": False, "symbol": s, "observedAt": now_iso()}),
    "MNQZ6")
check("観測口座が分からなければ全 ID(旧経路の互換)",
      seen_ids and seen_ids[0] == ["1001", "1002", "2001", "2002"], str(seen_ids))

# ================================================================
print("=" * 68)
print("3. 観測と送信後照合も同じヘルパで絞る")
print("=" * 68)

src_reconcile = inspect.getsource(ae._reconcile_one)
check("観測の known_order_ids はヘルパで target_account に絞る",
      "_route_rows_for_account(" in src_reconcile and "target_account)" in src_reconcile)
check("送信後照合は建玉を観測した口座で絞る",
      "opened_account" in src_reconcile and "_route_rows_for_account(route_snapshot, opened_account)" in src_reconcile)
src_recover = inspect.getsource(ae._recover_entry_from_current_broker)
check("回復は観測口座で絞る", "_route_rows_for_account(frozen_all, probe_account)" in src_recover)

print("=" * 68)
if FAIL[0]:
    print(f"FAILED {FAIL[0]} / {PASS[0] + FAIL[0]}")
    sys.exit(1)
print(f"ALL PASS ({PASS[0]})")

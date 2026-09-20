# -*- coding: utf-8 -*-
"""R114: 一括照会 —— 1 サイクルで各(口座, データ種別)を一度だけ観測し、全消費者が共有する。

2026-09-19 実測: 7 口座で表示・engine・戦績記録・ULTRA サイジングがそれぞれ独立に
ブローカーへ行き、1 サイクルの照会が 70〜160 本・111〜284 秒になった。CrossTrade は
429 ではなく応答を遅らせて絞る(同じ照会 5 連で 0.69 → 5.05 → 16.26 秒)ので、
並列も retry も効かず、本数だけが効く。

ここで固定するのは 3 つ:
  1. `prime_cycle_snapshot` の後、消費者の読み取りはブローカーへ行かない。
  2. 証明用の二度読み(`_stable_broker_snapshot`)は共有を **読まず**、毎回ブローカーへ行く。
     共有に当たると before/after が同じオブジェクトになり、DO への不在証明が偽物になる。
  3. `order.py` を起動したら共有は捨てられ、その後の読み取りは生になる。

ネットワークは使わない(broker_status の生照会を差し替える)。

    python tests/test_r114_cycle_snapshot.py
"""
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

import broker_status as bs      # noqa: E402
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


CALLS = {"position": 0, "orders": 0, "balance": 0}


def _now():
    return datetime.now(timezone.utc).isoformat()


def fake_position(symbol, account=None):
    CALLS["position"] += 1
    return {"verified": True, "qty": 0, "side": "FLAT", "accountId": account,
            "symbol": symbol, "observedAt": _now(), "seq": CALLS["position"]}


def fake_orders(symbol, known_order_ids=None, account=None):
    CALLS["orders"] += 1
    return {"verified": True, "state": "NONE", "orders": [], "accountScope": [account],
            "symbol": symbol, "observedAt": _now(), "ids": list(known_order_ids or [])}


def fake_balance(account=None):
    CALLS["balance"] += 1
    return {"verified": True, "account": account, "netLiq": 50000.0}


def reset():
    CALLS.update(position=0, orders=0, balance=0)
    bs.invalidate_read_cache()


bs.query_position = fake_position
bs.query_orders = fake_orders
bs.query_balance = fake_balance
ACCOUNTS = ["ACC-A", "ACC-B", "ACC-C"]

# ================================================================
print("=" * 68)
print("1. 一括照会の後、消費者はブローカーへ行かない")
print("=" * 68)

reset()
summary = bs.prime_cycle_snapshot("MNQZ6", ACCOUNTS)
check("口座ごとに建玉・注文・残高を 1 本ずつ(3 口座 = 9 本)",
      CALLS == {"position": 3, "orders": 3, "balance": 3}, str(CALLS))
check("要約に口座数が出る", summary["accounts"] == 3, str(summary))
check("要約に照会本数が出る", summary["queries"] == 9, str(summary))
check("全部 verified なら unverified は空", summary["unverified"] == [], str(summary))

before = dict(CALLS)
for account in ACCOUNTS:
    bs.query_position_cached("MNQZ6", account=account)
    bs.query_orders_cached("MNQZ6", account=account)
    bs.query_balance_cached(account=account)
    ae._scoped_position_query(None, "MNQZ6", account)
    ae._scoped_order_query(None, "MNQZ6", account)
check("表示・engine・戦績が同じ観測を読んでも照会は増えない", CALLS == before, f"{before} -> {CALLS}")

bs.query_orders_cached("MNQZ6", account="ACC-A", known_order_ids=["111", "222"])
check("ID 付きの注文照会は別鍵(終端した注文の詳細を持つので代用しない)",
      CALLS["orders"] == before["orders"] + 1, str(CALLS))
bs.query_orders_cached("MNQZ6", account="ACC-A", known_order_ids=["222", "111"])
check("ID の順序が違っても同じ鍵", CALLS["orders"] == before["orders"] + 1, str(CALLS))

# ================================================================
print("=" * 68)
print("2. 証明用の二度読みは共有を読まない")
print("=" * 68)

reset()
bs.prime_cycle_snapshot("MNQZ6", ["ACC-A"])
primed = dict(CALLS)
with bs.live_reads():
    first = bs.query_position_cached("MNQZ6", account="ACC-A")
    second = bs.query_position_cached("MNQZ6", account="ACC-A")
check("live_reads の中では毎回ブローカーへ行く", CALLS["position"] == primed["position"] + 2, str(CALLS))
check("二度読みは別の観測(同じオブジェクトではない)", first is not second and first["seq"] != second["seq"])
after_live = dict(CALLS)
bs.query_position_cached("MNQZ6", account="ACC-A")
check("live_reads を出たら書き戻した値を共有する", CALLS == after_live, str(CALLS))

# _stable_broker_snapshot は production の既定(共有版)を注入されても生で読む。
reset()
bs.prime_cycle_snapshot("MNQZ6", ["ACC-A"])
primed = dict(CALLS)
position_query = lambda symbol: ae._scoped_position_query(None, symbol, "ACC-A")   # noqa: E731
order_query = lambda symbol, known_order_ids=None: ae._scoped_order_query(         # noqa: E731
    None, symbol, "ACC-A", known_order_ids)
before_pos, orders, after_pos = ae._stable_broker_snapshot(position_query, order_query, "MNQZ6", [], attempts=1)
check("三点読みの before/after は別々にブローカーへ行く",
      CALLS["position"] == primed["position"] + 2, f"{primed} -> {CALLS}")
check("三点読みの before と after は同じオブジェクトではない",
      before_pos is not after_pos and before_pos["seq"] != after_pos["seq"])
check("三点読みの注文も生で読む", CALLS["orders"] == primed["orders"] + 1, str(CALLS))

# ================================================================
print("=" * 68)
print("3. order.py を起動したら共有は捨てられる")
print("=" * 68)

reset()
bs.prime_cycle_snapshot("MNQZ6", ["ACC-A"])
primed = dict(CALLS)


def fake_run(command, **kwargs):
    raise RuntimeError("送信はしない(テスト)")


original = ae.subprocess.run
try:
    ae.subprocess.run = fake_run
    ae._run_order(["--status"], False)
finally:
    ae.subprocess.run = original
bs.query_position_cached("MNQZ6", account="ACC-A")
check("order.py の後は建玉を生で読み直す", CALLS["position"] == primed["position"] + 1, str(CALLS))
bs.query_orders_cached("MNQZ6", account="ACC-A")
check("order.py の後は注文も生で読み直す", CALLS["orders"] == primed["orders"] + 1, str(CALLS))

# ================================================================
print("=" * 68)
print("4. 取れなかった口座は要約に残り、判断はしない")
print("=" * 68)

reset()


def flaky_position(symbol, account=None):
    CALLS["position"] += 1
    if account == "ACC-B":
        return {"verified": False, "symbol": symbol, "detail": "HTTP 429"}
    return fake_position(symbol, account=account)


bs.query_position = flaky_position
summary = bs.prime_cycle_snapshot("MNQZ6", ACCOUNTS)
check("verified でない口座が unverified に出る", summary["unverified"] == ["ACC-B:position"], str(summary))
check("他の口座の観測は共有される", bs.query_position_cached("MNQZ6", account="ACC-A")["verified"] is True)
bs.query_position = fake_position

# account=None は既定口座と同じ鍵(sync_position が先頭口座を二度取らないため)
reset()
bs._default_account = lambda: "ACC-A"
bs.prime_cycle_snapshot("MNQZ6", ["ACC-A"])
primed = dict(CALLS)
bs.query_position_cached("MNQZ6")
check("account=None は既定口座の共有を読む", CALLS == primed, f"{primed} -> {CALLS}")

print("=" * 68)
if FAIL[0]:
    print(f"FAILED {FAIL[0]} / {PASS[0] + FAIL[0]}")
    sys.exit(1)
print(f"ALL PASS ({PASS[0]})")

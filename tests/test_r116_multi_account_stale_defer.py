# -*- coding: utf-8 -*-
"""R116: 多口座 claim は DO の RECOVER 証明を構造的に通せない。全口座 FLAT を確かめて CLAIM へ委ねる。

DO の `entryRecoveryProof` は「1 回のブローカー観測で claim の全 receipt を照合」する。
7 口座の claim では 1 口座の観測に他 6 口座の注文が載らないので、RECOVER は常に 409
ENTRY_CLAIM_RECOVERY_UNVERIFIED になる(2026-09-19 01:50 の ENTRY が 2 時間以上塞いだ)。

DO の stale release(`staleReleasable`)は 1 口座の観測から出るので、多口座では
「A が空 = 全部空」ではない。engine は全口座を観測しているので、**全口座の不在を
自分で確かめてから** CLAIM へ委ねる。engine は何も解放しない。

ネットワークは使わない(broker_status の生照会を差し替える)。

    python tests/test_r116_multi_account_stale_defer.py
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


A, B, C = "LFE00000000000026", "LFE00000000000027", "MFFUEVREOD000000002"
STATE = {}   # account -> (verified, qty, order_state, order_verified)


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def fake_position(symbol, account=None):
    verified, qty, _, _ = STATE.get(account, (True, 0, "NONE", True))
    return {"verified": verified, "qty": qty, "side": "LONG" if qty else "FLAT",
            "accountId": account, "symbol": symbol, "observedAt": now_iso()}


TERMINAL = {"FILLED", "CANCELED", "REJECTED", "EXPIRED"}


def fake_orders(symbol, known_order_ids=None, account=None):
    # 3 つ目は **注文行の status の一覧**(ブローカー行)。"NONE" は行なし。
    _, _, statuses, verified = STATE.get(account, (True, 0, "NONE", True))
    statuses = [] if statuses == "NONE" else list(statuses)
    rows = [{"orderId": f"{account}-{i}", "status": s, "accountId": account}
            for i, s in enumerate(statuses)]
    active = [r for r in rows if r["status"] not in TERMINAL]
    return {"verified": verified, "state": "RESTING" if active else "NONE",
            "orders": rows, "activeOrders": [r["orderId"] for r in active],
            "openCount": len(active), "accountScope": [account],
            "symbol": symbol, "observedAt": now_iso()}


bs.query_position = fake_position
bs.query_orders = fake_orders


def claim_for(scope):
    return {"entryKey": "ENTRY:multi", "state": "CONSUMED",
            "executionIntent": {"symbol": "MNQZ6", "accountScope": list(scope)}}


def all_flat(scope):
    bs.invalidate_read_cache()
    return ae._claim_scope_all_flat(claim_for(scope), "MNQZ6")


# ================================================================
print("=" * 68)
print("1. 全口座 FLAT かつ注文なしのときだけ True")
print("=" * 68)

STATE.clear()
check("3 口座とも FLAT・注文なし → True", all_flat([A, B, C]) is True)

STATE[B] = (True, 2, "NONE", True)
check("1 口座でも建玉があれば False", all_flat([A, B, C]) is False)

STATE.clear(); STATE[C] = (True, 0, ["WORKING"], True)
check("1 口座でも生きた注文行(WORKING)があれば False", all_flat([A, B, C]) is False)
STATE.clear(); STATE[B] = (True, 0, ["FILLED", "PENDING"], True)
check("終端と未終端が混ざっていれば False", all_flat([A, B, C]) is False)

STATE.clear(); STATE[A] = (False, 0, "NONE", True)
check("建玉が verified でなければ False(推測しない)", all_flat([A, B, C]) is False)

STATE.clear(); STATE[A] = (True, 0, "NONE", False)
check("注文が verified でなければ False", all_flat([A, B, C]) is False)

STATE.clear()
check("scope が空なら False", all_flat([]) is False)
check("executionIntent が無ければ False", ae._claim_scope_all_flat({"entryKey": "x"}, "MNQZ6") is False)

# 終端した注文(FILLED/CANCELED)は blocking ではないので通る
STATE.clear(); STATE[B] = (True, 0, ["CANCELED", "FILLED"], True)
check("終端した注文行だけなら True", all_flat([A, B, C]) is True)

# ================================================================
print("=" * 68)
print("2. 共有観測を読む(追加の照会を出さない)")
print("=" * 68)

STATE.clear()
calls = {"n": 0}
original_position = bs.query_position


def counting_position(symbol, account=None):
    calls["n"] += 1
    return original_position(symbol, account=account)


bs.query_position = counting_position
bs.invalidate_read_cache()
bs.prime_cycle_snapshot("MNQZ6", [A, B, C])
primed = calls["n"]
ae._claim_scope_all_flat(claim_for([A, B, C]), "MNQZ6")
check("サイクル先頭の一括照会の後は建玉を取り直さない", calls["n"] == primed, f"{primed} -> {calls['n']}")
bs.query_position = original_position

# ================================================================
print("=" * 68)
print("3. _reconcile_one の委譲条件")
print("=" * 68)

src = inspect.getsource(ae._reconcile_one)
check("DO の staleReleasable を見る", 'fresh_claim.get("staleReleasable") is True' in src)
check("全口座 FLAT を engine が確かめる", "_claim_scope_all_flat(claim_view" in src)
check("同じ entryKey の claim にだけ効く",
      'str(fresh_claim.get("entryKey") or "") == str(claim_view.get("entryKey") or "")' in src)
check("理由が台帳に残る", "ENTRY_CLAIM_STALE_RELEASABLE_ALL_FLAT" in src)

print("=" * 68)
if FAIL[0]:
    print(f"FAILED {FAIL[0]} / {PASS[0] + FAIL[0]}")
    sys.exit(1)
print(f"ALL PASS ({PASS[0]})")

# -*- coding: utf-8 -*-
"""口座名簿の突合(accounts.sync)の検証。

ネットワークを使わない。roster を引数で渡し、build_accounts_payload の
差分計算だけを検査する。

    python tests/test_account_sync.py
"""
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

for _stream in ("stdout", "stderr"):
    _file = getattr(sys, _stream, None)
    if _file is not None and hasattr(_file, "reconfigure"):
        try:
            _file.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

import nqx_state  # noqa: E402

PASS = [0]
FAIL = [0]


def check(label, condition, detail=""):
    if condition:
        PASS[0] += 1
        print(f"  OK   {label}")
    else:
        FAIL[0] += 1
        print(f"  NG   {label}  {detail}")


ENV = {
    "CROSSTRADE_ACCOUNTS": "ACC-A,ACC-B",
    "LIFELINE_ACC-A": "2500",
    "LIFELINE_ACC-B": "3000",
    # TRAILING_DD/FLOOR は置かない → wants_live=False → ネットワークに触れない
}


def roster(rows, verified=True):
    return {"verified": verified, "observedAt": "2026-08-28T14:00:00+00:00",
            "accounts": rows}


print("=" * 68)
print("口座名簿の突合(sync)")
print("=" * 68)

# 完全一致: 差分なし
payload = nqx_state.build_accounts_payload(env=ENV, roster=roster([
    {"id": "ACC-A", "usable": True},
    {"id": "ACC-B", "usable": True},
]))
sync = payload.get("sync") or {}
check("差分なしなら全リスト空", sync.get("missing") == [] and sync.get("unknown") == []
      and sync.get("dead") == [])
check("verified が伝わる", sync.get("verified") is True)

# 設定口座がブローカーから消えた(事故2回のケース)
payload = nqx_state.build_accounts_payload(env=ENV, roster=roster([
    {"id": "ACC-A", "usable": True},
]))
sync = payload.get("sync") or {}
check("消えた口座が missing に出る", sync.get("missing") == ["ACC-B"])

# ブローカーに未設定の新口座
payload = nqx_state.build_accounts_payload(env=ENV, roster=roster([
    {"id": "ACC-A", "usable": True},
    {"id": "ACC-B", "usable": True},
    {"id": "ACC-NEW", "usable": True},
]))
sync = payload.get("sync") or {}
check("新口座が unknown に出る", sync.get("unknown") == ["ACC-NEW"])

# 死んだ状態の設定口座
payload = nqx_state.build_accounts_payload(env=ENV, roster=roster([
    {"id": "ACC-A", "usable": True},
    {"id": "ACC-B", "usable": False},
]))
sync = payload.get("sync") or {}
check("死んだ口座が dead に出る", sync.get("dead") == ["ACC-B"])
check("dead は missing に重複しない", sync.get("missing") == [])

# 照会失敗: 何も主張しない
payload = nqx_state.build_accounts_payload(env=ENV, roster=roster([], verified=False))
sync = payload.get("sync") or {}
check("照会失敗時は差分を主張しない",
      sync.get("verified") is False and sync.get("missing") == []
      and sync.get("unknown") == [] and sync.get("dead") == [])

# roster を渡さない(wants_live=False)ならフィールド自体が付かない
payload = nqx_state.build_accounts_payload(env=ENV)
check("roster 無しなら sync フィールドが無い", "sync" not in payload)

print("=" * 68)
total = PASS[0] + FAIL[0]
if FAIL[0]:
    print(f"FAILED {FAIL[0]} / {total}")
    sys.exit(1)
print(f"ALL PASS ({total})")
sys.exit(0)

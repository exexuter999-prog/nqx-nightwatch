# -*- coding: utf-8 -*-
"""R68: 口座が 1 つでも AUTO OFF は「新規を止める」だけで、所有済み建玉の管理は続く。

2026-09-08 実測: 09:02 に武装した AUTO が 16:02 に失効、15:09 の SELL 指値が 16:05 に約定して
SHORT 14 枚が建ったが、16:26 / 16:29 の reconcile は空を返し台帳は ENTRY_RESTING のまま。
``reconcile`` は 1 口座だと ``_reconcile_one`` を直接呼び、その冒頭の
``if not autotrade_enabled(cfg): return []`` で建玉管理ごと止まっていた(2 口座時代は多口座分岐が
管理専用フラグを差し込んでいた)。External sends: zero(runner はスタブ)。
"""
import os
import sys
import tempfile
from datetime import datetime, timezone

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import autotrade_engine as ae  # noqa: E402


def check(label, condition, detail=""):
    if not condition:
        raise AssertionError(f"{label}: {detail}")
    print(f"OK   {label}")


NOW = datetime.now(timezone.utc)
ACCOUNT = "ACC-SOLO"
CFG_OFF = {"CROSSTRADE_ACCOUNTS": ACCOUNT, "NQX_AUTOTRADE": "0", "NQX_LIVE_ORDERS": "0",
           "NQX_AUTOTRADE_KILL": "0", "NQX_SYMBOL": "MNQU6"}
BUNDLE = {"_published_scenario": {}, "price": 29621.5, "at": NOW.isoformat(),
          "snapshot": {"bars3m": [{"h": 29630, "l": 29610, "c": 29621}] * 12}}


def short_position(symbol, **_kwargs):
    return {"verified": True, "symbol": symbol, "accountId": ACCOUNT, "side": "SHORT",
            "qty": 14, "avgEntry": 29622.5, "orderId": 649589730727,
            "filledAt": NOW.isoformat(), "observedAt": NOW.isoformat()}


def flat_position(symbol, **_kwargs):
    return {"verified": True, "symbol": symbol, "accountId": ACCOUNT, "qty": 0,
            "observedAt": NOW.isoformat()}


def working_brackets(symbol, known_order_ids=None, **_kwargs):
    rows = [{"orderId": f"BR-{i}", "action": "BUY", "status": "WORKING", "accountId": ACCOUNT}
            for i in range(4)]
    return {"verified": True, "symbol": symbol, "state": "WORKING", "orders": rows,
            "activeOrders": rows, "accountScope": [ACCOUNT]}


def terminal_orders(symbol, known_order_ids=None, **_kwargs):
    return {"verified": True, "symbol": symbol, "state": "NONE", "orders": [],
            "activeOrders": [], "accountScope": [ACCOUNT]}


def resting_entry(symbol, known_order_ids=None, **_kwargs):
    # 契約の blockingOrderStates は PENDING / SENT / UNKNOWN(WORKING は非ブロッキング)。
    rows = [{"orderId": "ENTRY-1", "action": "SELL", "status": "SENT", "accountId": ACCOUNT}]
    return {"verified": True, "symbol": symbol, "state": "SENT", "orders": rows,
            "activeOrders": rows, "accountScope": [ACCOUNT]}


def make_runner(calls):
    def runner(argv, live):
        calls.append((list(argv), live))
        return 0, "stub ok"
    return runner


# 1. 建玉あり + AUTO OFF → 管理経路が走る(空を返さない)。新規 ENTRY は出ない。
with tempfile.TemporaryDirectory() as tmp:
    calls = []
    notes = ae.reconcile(BUNDLE, True, CFG_OFF, short_position, make_runner(calls),
                         os.path.join(tmp, "ledger.jsonl"), NOW, working_brackets,
                         state_query=lambda: {"scenario": None, "order": {"state": "NONE", "verified": True}})
    check("AUTO OFF でも建玉があれば reconcile は空を返さない", bool(notes), notes)
    check("管理経路の注記が出る(台帳に凍結プランが無いので hold)",
          any("autotrade" in note for note in notes), notes)
    check("新規 ENTRY は送らない",
          not any("--confirm" in argv and "--split-tp" in argv for argv, _ in calls), calls)
    ledger = open(os.path.join(tmp, "ledger.jsonl"), encoding="utf-8").read()
    check("建玉世代(POSITION_GENERATION open)が台帳に記録される",
          "POSITION_GENERATION" in ledger and ('"open":true' in ledger or '"open": true' in ledger),
          ledger[:300])

# 2. FLAT + 注文なし + AUTO OFF → 従来どおり何もしない(空)。
with tempfile.TemporaryDirectory() as tmp:
    calls = []
    notes = ae.reconcile(BUNDLE, True, CFG_OFF, flat_position, make_runner(calls),
                         os.path.join(tmp, "ledger.jsonl"), NOW, terminal_orders)
    check("FLAT で注文も無ければ AUTO OFF は空のまま(新規を作らない)", notes == [] and calls == [], (notes, calls))

# 3. FLAT + 未約定 ENTRY(blocking) + AUTO OFF → 取消経路(KILL/FLATTEN)が走る。
with tempfile.TemporaryDirectory() as tmp:
    calls = []
    notes = ae.reconcile(BUNDLE, True, CFG_OFF, flat_position, make_runner(calls),
                         os.path.join(tmp, "ledger.jsonl"), NOW, resting_entry)
    check("FLAT に残る未約定 ENTRY は AUTO OFF で取消経路へ送る(§3)",
          any("--flatten" in argv for argv, _ in calls) or any("flatten" in n.lower() or "kill" in n.lower() for n in notes),
          (notes, calls))

# 4. AUTO ON(従来経路)は影響を受けない: 建玉ありなら同じく管理経路。
with tempfile.TemporaryDirectory() as tmp:
    calls = []
    cfg_on = {**CFG_OFF, "NQX_AUTOTRADE": "1", "NQX_LIVE_ORDERS": "0"}
    notes = ae.reconcile(BUNDLE, True, cfg_on, short_position, make_runner(calls),
                         os.path.join(tmp, "ledger.jsonl"), NOW, working_brackets,
                         state_query=lambda: {"scenario": None, "order": {"state": "NONE", "verified": True}})
    check("AUTO ON の管理経路は従来どおり", bool(notes) and any("autotrade" in n for n in notes), notes)

print("ALL PASS (test_r68_disarmed_single_account; external sends=0)")

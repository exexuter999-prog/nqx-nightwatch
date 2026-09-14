# -*- coding: utf-8 -*-
"""Telegram command surface after authoritative one-click migration.

No subprocess or external endpoint is invoked; ``run_order`` is a local stub.
"""
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
import telegram_bot as tb  # noqa: E402
import management_intent  # noqa: E402


def check(label, condition, detail=""):
    if not condition:
        raise AssertionError(f"{label}: {detail}")
    print(f"OK   {label}")


CFG = {"NQX_LIVE_ORDERS": "1"}
calls = []


def runner(args):
    calls.append(list(args))
    return True, "stub only"


tb.run_order = runner
POSITION = {"verified": True, "symbol": "MNQU6", "side": "LONG", "qty": 1,
            "accountId": "ACC-TEST", "orderId": "FILL-1",
            "filledAt": "2026-08-23T01:00:00Z"}
tb.broker_status.query_position = lambda _symbol: dict(POSITION)
def claim(intent):
    return True, {"managementKey": management_intent.management_key(intent),
                  "claimToken": "T" * 43,
                  "managementIntentHash": management_intent.intent_hash(intent)}
tb.nqx_state.claim_management = claim
tb.clear_pending()

for command in ("/order buy 2 29760 29726 29796", "/market sell 2 29800 29830 29790"):
    before = len(calls)
    body, _keyboard = tb.handle(CFG, command)
    check(f"{command.split()[0]} legacy entry is disabled",
          len(calls) == before and "AUTHORITATIVE ENTRY ONLY" in body, body)

tb.PENDING.update({"argv": ["--side", "buy", "--qty", "1"],
                   "desc": "legacy", "at": "now", "token": "legacy-token"})
body, _keyboard = tb.handle(CFG, "/confirm")
check("legacy claimless /confirm cannot send", len(calls) == 0 and "廃止" in body, body)
check("legacy pending state is cleared", tb.PENDING["argv"] is None)

body, keyboard = tb.handle(CFG, "/modify buy 1 29760 29796")
check("/modify retains a dry-run path",
      len(calls) == 1 and calls[0][:8] == ["--modify", "--side", "buy", "--qty", "1",
                 "--sl", "29760.00", "--tp"] and "--position-generation" in calls[0]
      and tb.PENDING["argv"] is not None,
      f"calls={calls} body={body}")
token = tb.PENDING["token"]
body, keyboard, note = tb.handle_callback(CFG, "go:" + token)
check("/modify confirm uses the same order.py safety path exactly once",
      len(calls) == 2 and calls[-1][-1] == "--confirm", calls)
body2, keyboard2, note2 = tb.handle_callback(CFG, "go:" + token)
check("reused modify button cannot resend", len(calls) == 2, calls)

before = len(calls)
body, _keyboard = tb.handle(CFG, "/flatten")
check("/flatten remains available", len(calls) == before + 1
      and calls[-1] == ["--flatten", "--confirm"], calls)

help_body, _ = tb.handle(CFG, "/help")
check("help directs new ENTRY to /app and fixed split qty2",
      "/app" in help_body and "固定2枚" in help_body and "TP1/runner" in help_body, help_body)

print("ALL PASS (test_bot; external sends=0)")

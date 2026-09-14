# -*- coding: utf-8 -*-
"""Button boundaries for removed legacy ENTRY and retained management."""
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
import telegram_bot as tb  # noqa: E402


def check(label, condition, detail=""):
    if not condition:
        raise AssertionError(f"{label}: {detail}")
    print(f"OK   {label}")


calls = []
tb.run_order = lambda args: (calls.append(list(args)) or (True, "stub"))
tb.broker_status.query_position = lambda _symbol: {
    "verified": True, "symbol": "MNQU6", "side": "SHORT", "qty": 1,
    "accountId": "ACC-TEST", "orderId": "FILL-2", "filledAt": "2026-08-23T01:00:00Z"}
tb.clear_pending()

body, keyboard = tb.handle({}, "/order buy 2 29760 29726 29796")
check("legacy /order creates no pending token/button",
      tb.PENDING["token"] is None and not calls and "AUTHORITATIVE ENTRY ONLY" in body)
body, keyboard = tb.handle({}, "/market buy 2 29726 29760")
check("legacy /market creates no pending token/button",
      tb.PENDING["token"] is None and not calls and "AUTHORITATIVE ENTRY ONLY" in body)

body, keyboard = tb.handle({}, "/modify sell 1 29820 29780")
token = tb.PENDING["token"]
check("modify dry-run creates a one-time confirmation token",
      isinstance(token, str) and token and "go:" + token in str(keyboard), keyboard)
_body, _keyboard, note = tb.handle_callback({}, "go:not-the-current-token")
check("stale button is ignored", len(calls) == 1 and "古い" in note, note)

tb.clear_pending()
_body, _keyboard, note = tb.handle_callback({}, "scenario:order:legacy")
check("legacy scenario callback never becomes a claimless entry",
      len(calls) == 1 and "/app" in str(_body) and "廃止" in str(note), (_body, note))

print("ALL PASS (test_buttons; external sends=0)")

# -*- coding: utf-8 -*-
"""R18 execution-intent, route-summary and MODIFY verification boundaries.

All broker routes are local stubs.  This file never opens the network.
"""
import copy
import json
import os
import pathlib
import subprocess
import sys
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import patch

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import broker_status  # noqa: E402
import execution_contract  # noqa: E402
import execution_intent  # noqa: E402
import management_intent  # noqa: E402
import nqx_state  # noqa: E402
import order  # noqa: E402
import route_envelope  # noqa: E402
import telegram_bot  # noqa: E402


def check(label, condition, detail=""):
    if not condition:
        raise AssertionError(f"{label}: {detail}")
    print(f"OK   {label}")


SCENARIO = {
    "symbol": "MNQU6", "side": "BUY", "qty": 2,
    "entry": 20000, "stop": 19960, "targets": [20040, 20080],
    "legs": [{"id": "TP1", "qty": 1, "target": 20040},
             {"id": "RUNNER", "qty": 1, "target": 20080}],
    "planVersion": "R18-SPLIT-1",
    "executionContractVersion": execution_contract.VERSION,
    "executionContract": {"accountScope": ["ACC-TEST"]},
}
INTENT = execution_intent.from_scenario(SCENARIO, order_type="LIMIT")


# Python and Worker must hash the same exact canonical bytes.
module_uri = pathlib.Path(BASE, "cloudflare", "src", "state_machine.js").as_uri()
script = (f'import {{ executionIntentHash }} from {json.dumps(module_uri)}; '
          'const raw=JSON.parse(process.argv[1]); console.log(executionIntentHash(raw));')
js_hash = subprocess.run(
    ["node", "--input-type=module", "-e", script, json.dumps(INTENT, separators=(",", ":"))],
    cwd=BASE, check=True, capture_output=True, text=True, encoding="utf-8",
).stdout.strip()
check("Python and Worker executionIntent hashes match",
      js_hash == execution_intent.intent_hash(INTENT), js_hash)
try:
    execution_intent.normalize({**INTENT, "accountScope": ["ACC-TEST", "ACC-TEST"]})
except ValueError:
    duplicate_scope_rejected = True
else:
    duplicate_scope_rejected = False
check("duplicate account scope is rejected instead of silently deduplicated",
      duplicate_scope_rejected)


for label, mutation in {
    "symbol": {**INTENT, "symbol": "MNQZ6"},
    "entry": {**INTENT, "entry": "20000.25"},
    "stop": {**INTENT, "stop": "19959.75"},
    "target": {**INTENT, "targets": ["20040.25", INTENT["targets"][1]],
               "legs": [{**INTENT["legs"][0], "target": "20040.25"}, INTENT["legs"][1]]},
    "orderType": execution_intent.from_scenario(
        SCENARIO, {"verified": True, "price": 20000}, order_type="MARKET"),
    "account": {**INTENT, "accountScope": ["ACC-OTHER"]},
    "plan": {**INTENT, "planVersion": "R18-SPLIT-2"},
}.items():
    check(f"{label} one-bit change changes intent hash",
          execution_intent.intent_hash(mutation) != execution_intent.intent_hash(INTENT))


CFG = {
    "CROSSTRADE_URL": "http://127.0.0.1:9/blocked",
    "CROSSTRADE_KEY": "TEST", "CROSSTRADE_DEST": "TEST",
    "CROSSTRADE_ACCOUNT": "ACC-TEST", "MAX_RISK_DOLLARS": "240",
}


# Direct live CLI cannot reach the route when its actual prices/config differ
# from the DO-frozen intent hash.
route_calls = []
argv = ["order.py", "--confirm", "--side", "buy", "--qty", "2",
        "--entry", "20000.25", "--sl", "19960", "--split-tp", "20040,20080",
        "--entry-key", "ENTRY:" + "a" * 64, "--claim-token", "T" * 43,
        "--intent-hash", execution_intent.intent_hash(INTENT),
        "--plan-version", SCENARIO["planVersion"]]
with patch.object(sys, "argv", argv), \
        patch.object(order, "load_env", return_value=CFG), \
        patch.object(order, "load_log", return_value={}), \
        patch.object(order, "dayguard_enabled", return_value=False), \
        patch.object(order, "dayguard_state", return_value=({"blocked": False}, None)), \
        patch.object(order, "require_verified_flat", return_value=None), \
        patch.object(order, "post_split_to_accounts",
                     side_effect=lambda *_args, **_kwargs: route_calls.append(True)):
    try:
        with redirect_stdout(StringIO()):
            order.main()
    except SystemExit as exc:
        direct_exit = str(exc)
    else:
        direct_exit = ""
check("direct CLI intent tamper stops before route",
      "ENTRY_CLAIM_INTENT_MISMATCH" in direct_exit and not route_calls, direct_exit)


valid_snapshot = [
    {"accountId": "ACC-TEST", "legId": "TP1", "state": "ACCEPTED",
     "orderId": "E-TP1", "receipt": "E-R1"},
    {"accountId": "ACC-TEST", "legId": "RUNNER", "state": "ACCEPTED",
     "orderId": "E-RUNNER", "receipt": "E-R2"},
]
valid_summary = route_envelope.format_envelope(valid_snapshot, "SENT")
check("strict final route summary accepts complete SENT",
      telegram_bot.classify_send_result(True, valid_summary)[0] == "SENT")
for bad in [
    "HTTP 200\nok",
    valid_summary + "\n" + valid_summary,
    valid_summary + "\nlate trailing output",
    "ERROR interrupted after accepted route\n" + valid_summary,
    "NQX_ROUTE_FINAL STATE=SENT ACCEPTED=1 REJECTED=0 UNKNOWN=0 TOTAL=1 RESOLVED=1",
    "NQX_ROUTE_FINAL STATE=SENT ACCEPTED=1 REJECTED=0 UNKNOWN=0 TOTAL=2 RESOLVED=1",
    "NQX_ROUTE_FINAL STATE=REJECTED ACCEPTED=1 REJECTED=1 UNKNOWN=0 TOTAL=2 RESOLVED=1",
    "NQX_ROUTE_FINAL STATE=SENT ACCEPTED=2 REJECTED=0 UNKNOWN=0 TOTAL=2 RESOLVED=0",
]:
    check("missing/conflicting/truncated route is UNKNOWN",
          telegram_bot.classify_send_result(True, bad)[0] == "UNKNOWN", bad)
check("explicit all-rejected plus RESOLVE is REJECTED",
      telegram_bot.classify_send_result(False,
          route_envelope.format_envelope([
              {"accountId": "ACC-TEST", "legId": "TP1", "state": "REJECTED"},
              {"accountId": "ACC-TEST", "legId": "RUNNER", "state": "REJECTED"},
          ], "REJECTED"))[0]
      == "REJECTED")


# MODIFY is successful only after the same strong position instance and both
# account-scoped protective order identities/prices/qty are re-observed.
POSITION = {"verified": True, "symbol": "MNQU6", "side": "LONG", "qty": 1,
            "accountId": "ACC-TEST", "orderId": "FILL-1",
            "filledAt": "2026-08-23T01:00:00Z", "avgEntry": 20000}
GENERATION = broker_status.position_identity(POSITION)
MANAGEMENT = management_intent.build(
    account_id="ACC-TEST", symbol="MNQU6", position_generation=GENERATION,
    side="BUY", qty=1, stop=19990, target=20030)
MANAGEMENT_KEY = management_intent.management_key(MANAGEMENT)
MANAGEMENT_HASH = management_intent.intent_hash(MANAGEMENT)
PROTECTIVE = {"verified": True, "state": "PENDING", "activeOrders": [
    {"orderId": "SL-1", "accountId": "ACC-TEST", "qty": 1, "stopPrice": 19990,
     "limitPrice": None, "orderType": "STOP", "action": "SELL", "parentId": "OCO-1",
     "status": "WORKING"},
    {"orderId": "TP-1", "accountId": "ACC-TEST", "qty": 1, "stopPrice": None,
     "limitPrice": 20030, "orderType": "LIMIT", "action": "SELL", "parentId": "OCO-1",
     "status": "WORKING"},
]}


def run_modify(order_view):
    sent = []
    argv_modify = ["order.py", "--modify", "--confirm", "--side", "buy", "--qty", "1",
                   "--sl", "19990", "--tp", "20030", "--symbol", "MNQU6",
                   "--position-generation", GENERATION,
                   "--management-key", MANAGEMENT_KEY, "--management-token", "T" * 43,
                   "--management-intent-hash", MANAGEMENT_HASH]
    route_body = json.dumps({"data": {"stopOrderId": "SL-1", "targetOrderId": "TP-1",
                                       "ocoGroupId": "OCO-1", "receipt": "RCPT-1"}})
    with patch.object(sys, "argv", argv_modify), \
            patch.object(order, "load_env", return_value=CFG), \
            patch.object(order, "load_log", return_value={}), \
            patch.object(order.broker_status, "query_position",
                         side_effect=[copy.deepcopy(POSITION), copy.deepcopy(POSITION),
                                      copy.deepcopy(POSITION)]), \
            patch.object(order.broker_status, "query_orders", return_value=order_view), \
            patch.object(nqx_state, "consume_management_claim", return_value=(True, {})), \
            patch.object(nqx_state, "resolve_management_claim", return_value=(True, {})), \
            patch.object(order, "post_to_accounts",
                         side_effect=lambda *_args, **_kwargs: (
                             sent.append(True) or (True, [("ACC-TEST", 200, route_body)]))):
        try:
            with redirect_stdout(StringIO()):
                order.main()
        except SystemExit as exc:
            code = exc.code
        else:
            code = 0
    return code, sent


code, sent = run_modify(copy.deepcopy(PROTECTIVE))
check("MODIFY exact protective postverify succeeds", code == 0 and len(sent) == 1, code)
bad_protective = copy.deepcopy(PROTECTIVE)
bad_protective["activeOrders"][1]["limitPrice"] = 20030.25
code, sent = run_modify(bad_protective)
check("MODIFY TP mismatch is UNKNOWN/HALT after one route",
      code != 0 and len(sent) == 1, code)

check("protective verification rejects missing broker identity",
      broker_status.verify_protective_orders(
          {"verified": True, "activeOrders": [
              {"accountId": "ACC-TEST", "qty": 1, "stopPrice": 19990},
              {"accountId": "ACC-TEST", "qty": 1, "limitPrice": 20030},
          ]}, ["ACC-TEST"], 1, 19990, 20030)[0] is False)

for label, mutate in {
    "unrelated extra": lambda rows: rows.append({"orderId": "X", "accountId": "ACC-TEST",
        "qty": 1, "limitPrice": 20040, "orderType": "LIMIT", "action": "SELL", "parentId": "OCO-1"}),
    "opposite action": lambda rows: rows[0].update(action="BUY"),
    "wrong parent": lambda rows: rows[1].update(parentId="OCO-2"),
    "wrong order type": lambda rows: rows[0].update(orderType="LIMIT"),
}.items():
    changed = copy.deepcopy(PROTECTIVE)
    mutate(changed["activeOrders"])
    receipt = {"accounts": [{"accountId": "ACC-TEST", "stopOrderId": "SL-1",
                              "targetOrderId": "TP-1", "ocoGroupId": "OCO-1",
                              "receipt": "RCPT-1"}]}
    check(f"protective verification rejects {label}",
          broker_status.verify_protective_orders(
              changed, ["ACC-TEST"], 1, 19990, 20030, side="BUY",
              position_generation=GENERATION, position_before=POSITION,
              current_position=POSITION, route_receipt=receipt)[0] is False)

print("ALL PASS (test_r18_execution_intent; external sends=0)")

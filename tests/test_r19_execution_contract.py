# -*- coding: utf-8 -*-
"""R19 management CAS, lifecycle and direct-route negative contracts.

All broker, Worker and order routes are stubs.  External sends: zero.
"""
import json
import os
import pathlib
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import autotrade_engine as ae  # noqa: E402
import broker_status  # noqa: E402
import execution_contract  # noqa: E402
import execution_intent  # noqa: E402
import management_intent  # noqa: E402
import telegram_bot as tb  # noqa: E402


def check(label, condition, detail=""):
    if not condition:
        raise AssertionError(f"{label}: {detail}")
    print(f"OK   {label}")


POSITION = {"verified": True, "symbol": "MNQU6", "side": "LONG", "qty": 1,
            "accountId": "ACC-TEST", "orderId": "FILL-19",
            "receipt": "RCPT-19", "filledAt": "2026-08-23T02:00:00Z", "avgEntry": 20000}
GENERATION = broker_status.position_identity(POSITION)
INTENT = management_intent.build(
    account_id="ACC-TEST", symbol="MNQU6", position_generation=GENERATION,
    side="BUY", qty=1, stop=20000, target=20120)

module_uri = pathlib.Path(BASE, "cloudflare", "src", "state_machine.js").as_uri()
script = (f'import {{ managementIntentHash }} from {json.dumps(module_uri)}; '
          'const raw=JSON.parse(process.argv[1]); console.log(managementIntentHash(raw));')
js_hash = subprocess.run(
    ["node", "--input-type=module", "-e", script,
     json.dumps(INTENT, separators=(",", ":"))], cwd=BASE, check=True,
    capture_output=True, text=True, encoding="utf-8").stdout.strip()
check("Python/Worker management intent hash parity",
      js_hash == management_intent.intent_hash(INTENT), js_hash)

for field, value in {"accountId": "ACC-OTHER", "positionGeneration": "POS:" + "0" * 64,
                     "stop": "19999.75", "target": "20120.25", "side": "SELL"}.items():
    changed = dict(INTENT, **{field: value})
    try:
        changed_hash = management_intent.intent_hash(changed)
    except ValueError:
        changed_hash = "invalid"
    check(f"management {field} tamper cannot reuse hash",
          changed_hash != management_intent.intent_hash(INTENT))

many = [f"ACC-{index:02d}" for index in range(12)]
try:
    normalized = execution_intent.normalize_account_scope(list(many))
except ValueError:
    normalized = []
check("account count is uncapped (maxAccounts=null)", normalized == sorted(many))
# 上限は消しても「明示された一意な口座 ID」の要件は残る。
try:
    execution_intent.normalize_account_scope(["ACC-1", "ACC-1"])
except ValueError:
    duplicate_reject = True
else:
    duplicate_reject = False
check("duplicate accounts are still refused", duplicate_reject)
try:
    execution_intent.normalize_account_scope(["ACC-1", ""])
except ValueError:
    blank_reject = True
else:
    blank_reject = False
check("blank account ids are still refused", blank_reject)

# Telegram confirm re-observes the full strong identity; same side/qty but a
# different fill generation must stop before claim and runner.
tb.clear_pending()
tb.PENDING.update({"argv": ["--modify", "--side", "buy", "--qty", "1", "--sl", "20000",
                            "--tp", "20120", "--symbol", "MNQU6",
                            "--position-generation", GENERATION],
                   "desc": "modify", "at": "now", "token": "tok",
                   "managementIntent": INTENT})
changed_position = dict(POSITION, orderId="FILL-OTHER")
claim_calls, runner_calls = [], []
old_query = tb.broker_status.query_position
old_claim = tb.nqx_state.claim_management
old_runner = tb.run_order
try:
    tb.broker_status.query_position = lambda _symbol: dict(changed_position)
    tb.nqx_state.claim_management = lambda intent: (claim_calls.append(intent) or (True, {}))
    tb.run_order = lambda args: (runner_calls.append(args) or (True, "stub"))
    body, _ = tb.do_confirm({"NQX_LIVE_ORDERS": "1"})
finally:
    tb.broker_status.query_position = old_query
    tb.nqx_state.claim_management = old_claim
    tb.run_order = old_runner
check("Telegram generation swap stops claim and runner",
      not claim_calls and not runner_calls and "BLOCKED" in body, body)

NOW = datetime.now(timezone.utc)
SCENARIO = {"scenarioId": "sc-r19", "fingerprint": "fp-r19", "evidenceHash": "se-r19",
            "marketCycleId": "cy-r19", "symbol": "MNQU6", "side": "BUY", "qty": 2,
            "entry": 20000, "stop": 19960, "target": 20040, "targets": [20040, 20120],
            "legs": [{"id": "TP1", "qty": 1, "target": 20040},
                     {"id": "RUNNER", "qty": 1, "target": 20120}],
            "planVersion": "R19-PLAN-77", "grade": "A", "state": "ARMED",
            "executionContractVersion": execution_contract.VERSION,
            "executionContract": {"accountScope": ["ACC-TEST"]}}
plan = ae.build_management_plan(SCENARIO, {"snapshot": {"bars3m": []}}, {})
check("scenario.planVersion propagates without R12 substitution",
      plan["planVersion"] == "R19-PLAN-77", plan["planVersion"])

partial_plan = {**plan, "legIdentityKnown": False}
action = ae.management_action(partial_plan, dict(POSITION), {"price": 20050}, {}, {})
check("qty1 without leg identity is never inferred as runner",
      action and action["action"] == "HALT" and "leg identity" in action["reason"], action)

with tempfile.TemporaryDirectory() as tmp:
    ledger = os.path.join(tmp, "ledger.jsonl")
    ae._append_ledger({"key": plan["entryKey"], "status": "ENTRY_RESTING", "plan": plan}, ledger)
    rows, error = ae._read_ledger(ledger)
    check("ENTRY_RESTING is an authoritative frozen lifecycle state",
          not error and ae._frozen_plan_record(rows, "MNQU6")["status"] == "ENTRY_RESTING")
    ae._append_ledger({"key": plan["entryKey"], "status": "ENTRY_HALTED", "plan": plan,
                       "reason": "broker order CANCELED"}, ledger)
    rows, _ = ae._read_ledger(ledger)
    check("terminal cancel invalidates old resting entry",
          ae._frozen_plan_record(rows, "MNQU6") is None)

good_receipt = json.dumps({"data": {"stopOrderId": "SL19", "targetOrderId": "TP19",
                                    "ocoGroupId": "OCO19", "receipt": "RCPT19"}})
receipt, reason = broker_status.extract_replacement_receipt([("ACC-TEST", 200, good_receipt)])
check("structured replacement route receipt is frozen", receipt is not None, reason)
for bad in ["ok", "{}", json.dumps({"data": {"stopOrderId": "SL19"}})]:
    parsed, _ = broker_status.extract_replacement_receipt([("ACC-TEST", 200, bad)])
    check("arbitrary/truncated management 2xx is UNKNOWN", parsed is None, bad)

# A generic HTTP 2xx is not a broker receipt.  Split-leg acceptance requires
# both order id and receipt, and the durable route snapshot uses TP1/RUNNER.
import order  # noqa: E402
check("generic entry HTTP 2xx is UNKNOWN",
      order.route_attempt_state(200, "ok") == "UNKNOWN")
entry_body = json.dumps({"data": {"orderId": "ENTRY-TP1", "receipt": "RCPT-TP1"}})
check("structured entry receipt is ACCEPTED",
      order.route_attempt_state(200, entry_body) == "ACCEPTED")
snapshot = order.build_route_snapshot([
    ("ACC-TEST", "TP1", 200, entry_body),
    ("ACC-TEST", "RUNNER", 200, "ERROR rejected"),
], split=True)
snapshot_line = "NQX_ROUTE_SNAPSHOT " + json.dumps(
    snapshot, sort_keys=True, separators=(",", ":"))
final_line = "NQX_ROUTE_FINAL STATE=PARTIAL ACCEPTED=1 REJECTED=1 UNKNOWN=0 TOTAL=2 RESOLVED=1"
parsed_snapshot = ae._entry_route_snapshot(
    snapshot_line + "\n" + final_line)
check("PARTIAL route freezes account/leg/order identity",
      parsed_snapshot == snapshot and parsed_snapshot[0]["legId"] == "TP1", parsed_snapshot)
bound_partial = ae._bind_partial_leg(
    plan, dict(POSITION, orderId="ENTRY-TP1", receipt="RCPT-TP1"), parsed_snapshot)
check("confirmed partial fill binds the exact accepted leg",
      bound_partial and bound_partial["partialLegId"] == "TP1"
      and bound_partial["qty"] == 2 and bound_partial["finalTarget"] == plan["tp1"],
      bound_partial)
check("PARTIAL route with failure diagnostic is UNKNOWN",
      ae._entry_route_state("live send failed/unknown:\n" + snapshot_line + "\n" + final_line)
      is None)
check("malformed mixed route snapshot makes Bot result UNKNOWN",
      tb.classify_send_result(False,
          "NQX_ROUTE_SNAPSHOT not-json\n" + final_line)[0] == "UNKNOWN")
check("valid route snapshot and FINAL classify as PARTIAL",
      tb.classify_send_result(False, snapshot_line + "\n" + final_line)[0] == "PARTIAL")

print("ALL PASS (test_r19_execution_contract; external sends=0)")

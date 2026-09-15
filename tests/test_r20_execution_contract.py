# -*- coding: utf-8 -*-
"""Cycle 10 partial-fill, broker-order and route-envelope contracts.

Every route and broker call is an in-process fixture. External sends: zero.
"""
import copy
import json
import os
import sys
import tempfile
import time
import urllib.error
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from io import StringIO
from unittest.mock import patch

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import autotrade_engine as ae  # noqa: E402
import _pin_contract  # noqa: E402  (R102: 本番の manualHalt と限月をテストから切り離す)
import broker_status  # noqa: E402
import execution_contract  # noqa: E402
import execution_intent  # noqa: E402
import management_intent  # noqa: E402
import nqx_state  # noqa: E402
import order  # noqa: E402
import route_envelope  # noqa: E402
import strategy_evidence  # noqa: E402


def check(label, condition, detail=""):
    if not condition:
        raise AssertionError(f"{label}: {detail}")
    print(f"OK   {label}")


# ---------------------------------------------------------------- route envelope

ROUTE_ROWS = [
    {"accountId": "ACC-R20", "legId": "TP1", "state": "ACCEPTED",
     "orderId": "ENTRY-TP1", "receipt": "RECEIPT-TP1"},
    {"accountId": "ACC-R20", "legId": "RUNNER", "state": "ACCEPTED",
     "orderId": "ENTRY-RUNNER", "receipt": "RECEIPT-RUNNER"},
]
SENT_ENVELOPE = route_envelope.format_envelope(ROUTE_ROWS, "SENT")
check("shared route parser accepts one complete resolved envelope",
      route_envelope.parse(SENT_ENVELOPE, expected_accounts=["ACC-R20"])["ok"])
for corrupt in [
    SENT_ENVELOPE + "\ntrailing",
    SENT_ENVELOPE.replace("NQX_ROUTE_FINAL", "NQX_ROUTE_BROKEN"),
    SENT_ENVELOPE.splitlines()[0] + "\n" + SENT_ENVELOPE,
    "NQX_ROUTE_SNAPSHOT not-json\n" + SENT_ENVELOPE.splitlines()[-1],
    SENT_ENVELOPE.replace("ENTRY-RUNNER", "ENTRY-TP1"),
    SENT_ENVELOPE.replace("RECEIPT-RUNNER", "RECEIPT-TP1"),
    "ERROR after route\n" + SENT_ENVELOPE,
]:
    check("reserved/trailing/duplicate/mixed route envelope is UNKNOWN",
          not route_envelope.parse(corrupt, expected_accounts=["ACC-R20"])["ok"], corrupt[-120:])

direct_scenario = {"symbol": "MNQU6", "side": "BUY", "qty": 2, "entry": 100,
                   "stop": 90, "targets": [110, 130],
                   "legs": [{"id": "TP1", "qty": 1, "target": 110},
                            {"id": "RUNNER", "qty": 1, "target": 130}],
                   "planVersion": "R20-SPLIT-1",
                   "executionContractVersion": execution_contract.VERSION,
                   "executionContract": {"accountScope": ["ACC-R20"]}}
direct_intent = execution_intent.from_scenario(direct_scenario, order_type="LIMIT")
direct_results = [
    ("ACC-R20", "TP1", 200, json.dumps({"data": {"orderId": "D-TP1", "receipt": "D-R1"}})),
    ("ACC-R20", "RUNNER", 200, json.dumps({"data": {"orderId": "D-RUNNER", "receipt": "D-R2"}})),
]
direct_argv = ["order.py", "--confirm", "--side", "buy", "--qty", "2",
               "--entry", "100", "--sl", "90", "--split-tp", "110,130",
               "--entry-key", "ENTRY:" + "a" * 64, "--claim-token", "T" * 43,
               "--intent-hash", execution_intent.intent_hash(direct_intent),
               "--plan-version", "R20-SPLIT-1"]
direct_out = StringIO()
with patch.object(sys, "argv", direct_argv), \
        patch.object(order, "load_env", return_value={
            "CROSSTRADE_URL": "http://127.0.0.1:9/stub", "CROSSTRADE_KEY": "stub",
            "CROSSTRADE_DEST": "stub", "CROSSTRADE_ACCOUNT": "ACC-R20",
            "MAX_RISK_DOLLARS": "240"}), \
        patch.object(order, "load_log", return_value={}), \
        patch.object(order, "dayguard_enabled", return_value=False), \
        patch.object(order, "dayguard_state", return_value=({"blocked": False}, None)), \
        patch.object(order, "require_verified_flat", return_value=None), \
        patch.object(order, "post_split_to_accounts", return_value=(True, direct_results)), \
        patch.object(nqx_state, "consume_entry_claim", return_value=(True, {})), \
        patch.object(nqx_state, "resolve_entry_claim", return_value=(False, {"reason": "stub"})), \
        redirect_stdout(direct_out):
    try:
        order.main()
    except SystemExit:
        pass
check("failed durable RESOLVE emits zero FINAL records",
      "NQX_ROUTE_FINAL" not in direct_out.getvalue(), direct_out.getvalue())


# ---------------------------------------------------------------- CrossTrade documented normalization

nt8_payload = {"success": True, "orders": [
    {"id": "SL20", "account": "ACC-R20", "instrument": "MNQU6",
     "orderType": "STOP_MARKET", "orderState": "WORKING", "orderAction": "SELL",
     "quantity": 1, "stopPrice": 19990, "filled": 0, "ocoId": "OCO20"},
    {"id": "TP20", "account": "ACC-R20", "instrument": "MNQU6",
     "orderType": "LIMIT", "orderState": "WORKING", "orderAction": "SELL",
     "quantity": 1, "limitPrice": 20030, "filled": 0, "ocoId": "OCO20"},
]}
nt8 = broker_status.normalize_crosstrade_orders(
    nt8_payload, platform="NT8", account="ACC-R20", symbol="MNQU6")
check("NT8 documented orders[] normalizes exact account/instrument/OCO",
      nt8["verified"] and nt8["state"] == "PENDING" and len(nt8["activeOrders"]) == 2, nt8)
entry_resting_payload = {"success": True, "orders": [
    {"id": row["orderId"], "account": "ACC-R20", "instrument": "MNQU6",
     "orderType": "LIMIT", "orderState": "WORKING", "orderAction": "BUY",
     "quantity": 1, "limitPrice": 100, "filled": 0, "ocoId": f"ENTRY-{row['legId']}"}
    for row in ROUTE_ROWS]}
entry_partial_payload = copy.deepcopy(entry_resting_payload)
entry_partial_payload["orders"][0]["orderState"] = "FILLED"
entry_partial_payload["orders"][0]["filled"] = 1
entry_terminal_payload = copy.deepcopy(entry_partial_payload)
entry_terminal_payload["orders"][1]["orderState"] = "FILLED"
entry_terminal_payload["orders"][1]["filled"] = 1
truth_states = [broker_status.normalize_crosstrade_orders(
    payload, platform="NT8", account="ACC-R20", symbol="MNQU6")
    for payload in (entry_resting_payload, entry_partial_payload, entry_terminal_payload)]
check("CrossTrade-only order truth covers resting -> partial fill -> terminal",
      [row["state"] for row in truth_states] == ["PENDING", "PENDING", "FILLED"]
      and truth_states[1]["filledOrderIds"] == ["ENTRY-TP1"]
      and truth_states[1]["orderIds"] == ["ENTRY-RUNNER"], truth_states)

trad_payload = {"success": True, "data": [
    {"id": 701, "accountId": 90000110, "contractId": 9001,
     "orderType": "Limit", "ordStatus": "Working", "action": "Sell",
     "orderQty": 1, "price": 20030, "osId": "OCO-TV"},
]}
trad = broker_status.normalize_crosstrade_orders(
    trad_payload, platform="TRADOVATE", account="ACC-R20", symbol="MNQU6",
    account_aliases=[90000110], contract_ids=[9001])
check("Tradovate documented data rows require accountId+contractId aliases",
      trad["verified"] and trad["orderIds"] == ["701"], trad)
for bad in [
    {"success": True, "orders": [{**nt8_payload["orders"][0], "account": "OTHER"}]},
    {"success": True, "data": {}},
]:
    try:
        broker_status.normalize_crosstrade_orders(
            bad, platform="NT8", account="ACC-R20", symbol="MNQU6")
    except broker_status.Unavailable:
        rejected = True
    else:
        rejected = False
    check("wrong account/symbol/schema is UNVERIFIED", rejected, bad)
try:
    broker_status.normalize_crosstrade_orders(
        {"success": True, "orders": [{**nt8_payload["orders"][0], "instrument": "NQU6"}]},
        platform="NT8", account="ACC-R20", symbol="MNQU6")
except broker_status.Unavailable:
    other_symbol_rejected = True
else:
    other_symbol_rejected = False
check("other-symbol rows invalidate the whole broker view", other_symbol_rejected)

adapter = broker_status.CrossTradeAdapter({
    "CROSSTRADE_KEY": "stub", "CROSSTRADE_ACCOUNT": "ACC-R20",
    "CROSSTRADE_ACCOUNT_ID": "90000110", "CROSSTRADE_CONTRACT_ID": "9001",
    "CROSSTRADE_PLATFORM": "TRADOVATE", "CROSSTRADE_API_BASE": "https://stub/v1/api/tv",
})
responses = iter([
    trad_payload,
    {"success": True, "data": {"id": 702, "accountId": 90000110, "contractId": 9001,
      "orderType": "Limit", "ordStatus": "Filled", "action": "Sell", "orderQty": 1,
      "price": 20040, "osId": "OCO-TV"}},
])
urls = []
with patch.object(broker_status, "_get_json",
                  side_effect=lambda url, _headers: (urls.append(url) or next(responses))):
    merged = adapter.query_orders("MNQU6", known_order_ids=["701", "702"])
check("Tradovate working collection merges individual terminal order truth",
      set(row["orderId"] for row in merged["orders"]) == {"701", "702"}
      and merged["filledOrderIds"] == ["702"] and any(url.endswith("/orders/702") for url in urls),
      f"urls={urls} merged={merged}")
for status_code in (401, 500):
    with patch.object(broker_status, "_get_json", side_effect=urllib.error.HTTPError(
            "https://stub", status_code, "stub", {}, None)):
        try:
            adapter.query_orders("MNQU6")
        except broker_status.Unavailable:
            unavailable = True
        else:
            unavailable = False
    check(f"CrossTrade HTTP {status_code} is UNVERIFIED", unavailable)


# ---------------------------------------------------------------- exact protective postverify transaction

position = {"verified": True, "accountId": "ACC-R20", "symbol": "MNQU6",
            "side": "LONG", "qty": 1, "avgEntry": 20000,
            "orderId": "ENTRY-TP1", "receipt": "RECEIPT-TP1",
            "filledAt": "2026-08-23T12:00:00Z"}
generation = broker_status.position_identity(position)
receipt = {"accounts": [{"accountId": "ACC-R20", "stopOrderId": "SL20",
                          "targetOrderId": "TP20", "ocoGroupId": "OCO20",
                          "receipt": "MODIFY-R20"}]}
ok, detail = broker_status.verify_protective_orders(
    nt8, ["ACC-R20"], 1, 19990, 20030, side="BUY",
    position_generation=generation, position_before=position,
    current_position=position, route_receipt=receipt)
check("protective postverify binds full position generation and OCO receipt", ok, detail)
for changed_position, changed_receipt in [
    ({**position, "receipt": "OTHER"}, receipt),
    (position, {"accounts": [{**receipt["accounts"][0], "stopOrderId": "TP20",
                               "targetOrderId": "SL20"}]}),
    (None, receipt),
]:
    verified, _ = broker_status.verify_protective_orders(
        nt8, ["ACC-R20"], 1, 19990, 20030, side="BUY",
        position_generation=generation, position_before=position,
        current_position=changed_position,
        route_receipt=changed_receipt)
    check("generation missing/swap/unavailable is UNKNOWN", not verified)


# ---------------------------------------------------------------- autotrade SENT->qty1 partial ownership

NOW = datetime.now(timezone.utc)
EVIDENCE = strategy_evidence.canonicalize({
    "version": "R14-STRATEGY-EVIDENCE-1", "asOf": NOW.isoformat(),
    "sessionId": "NY-R20", "source": "fixture", "provenance": "test",
    "models": {"ifvg": {"valid": False}},
})
SCENARIO = {
    "scenarioId": "sc-r20", "fingerprint": "fp-r20", "evidenceHash": EVIDENCE["evidenceHash"],
    "marketCycleId": "cycle-r20", "symbol": "MNQU6", "side": "BUY", "qty": 2,
    "entry": 100, "stop": 90, "target": 110, "targets": [110, 130],
    "legs": [{"id": "TP1", "qty": 1, "target": 110},
             {"id": "RUNNER", "qty": 1, "target": 130}],
    "planVersion": "R20-SPLIT-1", "grade": "A", "state": "ARMED",
    "issuedAt": NOW.isoformat(), "observedAt": NOW.isoformat(),
    "expiresAt": (NOW + timedelta(minutes=5)).isoformat(),
    "setupVersion": "R20-S", "catalogVersion": "R20-C", "detectorVersion": "R20-D",
    "executionContractVersion": execution_contract.VERSION,
    "executionContract": {"accountScope": ["ACC-R20"]},
}
BUNDLE = {"_published_scenario": SCENARIO, "price": 101, "at": NOW.isoformat(),
          "cvdAt": NOW.isoformat(), "snapshot": {"bars3m": [{"h": 101, "l": 98, "c": 99}] * 12}}


def view():
    market = {"at": NOW.isoformat(), "observedAt": NOW.isoformat(), "cvdAt": NOW.isoformat(),
              "cycleId": SCENARIO["marketCycleId"], "cycleCommitted": True, "verified": True,
              "source": "fixture", "sourceSymbol": "CME_MINI:MNQU6", "strategyEvidence": EVIDENCE}
    scenario = {key: value for key, value in SCENARIO.items() if key != "model"}
    scenario["cycleCommitted"] = True
    return {"market": market, "scenario": scenario, "position": {"state": "FLAT"},
            "order": {"state": "NONE", "verified": True},
            "display": {"orderable": True, "cyclePaired": True}}


def claim_entry(scenario, **kwargs):
    intent = execution_intent.from_scenario(
        scenario, order_type=kwargs.get("order_type") or "LIMIT", account_scope=["ACC-R20"])
    return True, {"entryKey": ae._entry_key(scenario), "claimToken": "T" * 43,
                  "executionIntent": intent, "executionIntentHash": execution_intent.intent_hash(intent)}


def entry_runner(args, live):
    return (0, SENT_ENVELOPE) if live else (0, "dry-run stub")


def run_partial_case(filled_leg, *, exact=True):
    filled = ROUTE_ROWS[0 if filled_leg == "TP1" else 1]
    remaining = ROUTE_ROWS[1 if filled_leg == "TP1" else 0]
    partial_position = {"verified": True, "symbol": "MNQU6", "side": "LONG", "qty": 1,
                        "avgEntry": 100, "accountId": "ACC-R20", "orderId": filled["orderId"],
                        "receipt": filled["receipt"] if exact else "UNKNOWN-RECEIPT",
                        "filledAt": NOW.isoformat()}
    post_order = {"verified": True, "state": "PENDING", "orders": [
        {"accountId": "ACC-R20", "symbol": "MNQU6", "action": "BUY", "qty": 1,
         "orderType": "LIMIT", "limitPrice": 100, "orderId": filled["orderId"],
         "receipt": filled["receipt"], "status": "FILLED"},
        {"accountId": "ACC-R20", "symbol": "MNQU6", "action": "BUY", "qty": 1,
         "orderType": "LIMIT", "limitPrice": 100, "orderId": remaining["orderId"],
         "receipt": remaining["receipt"], "status": "WORKING"}],
        "activeOrders": [{"accountId": "ACC-R20", "orderId": remaining["orderId"],
                           "status": "WORKING"}],
        "filledOrderIds": [filled["orderId"]], "orderIds": [remaining["orderId"]]}
    positions = iter([
        {"verified": True, "symbol": "MNQU6", "qty": 0},
        partial_position,
    ])
    orders = iter([
        {"verified": True, "state": "NONE", "orders": [], "activeOrders": []},
        post_order,
    ])
    tmp = tempfile.TemporaryDirectory()
    ledger = os.path.join(tmp.name, "ledger.jsonl")
    notes = ae.reconcile(
        BUNDLE, True, {"NQX_SYMBOL": "MNQU6", "NQX_AUTOTRADE": "1", "NQX_LIVE_ORDERS": "1"},
        lambda _symbol: next(positions), entry_runner, ledger,
        broker_order_query=lambda _symbol: next(orders), state_query=view, claim_entry=claim_entry)
    rows, error = ae._read_ledger(ledger)
    record = next((row for row in reversed(rows) if row.get("status") == "ENTRY_PARTIAL_FILL"), None)
    if record is None:
        raise AssertionError(f"partial record missing: notes={notes} error={error} rows={rows}")
    return tmp, ledger, record, notes, partial_position, post_order


for filled_leg, remaining_leg in [("TP1", "RUNNER"), ("RUNNER", "TP1")]:
    tmp, ledger, record, notes, _partial_position, _post_order = run_partial_case(filled_leg)
    check(f"SENT then qty1 freezes exact {filled_leg} fill",
          record["plan"]["partialLegId"] == filled_leg
          and record["plan"]["remainingRestingLeg"]["legId"] == remaining_leg,
          f"{notes} {record}")
    filled_rows = [{"accountId": "ACC-R20", "symbol": "MNQU6", "action": "BUY", "qty": 1,
                    "orderType": "LIMIT", "limitPrice": 100,
                    "orderId": row["orderId"], "receipt": row["receipt"], "status": "FILLED"}
                   for row in ROUTE_ROWS]
    completed_position = {"verified": True, "symbol": "MNQU6", "side": "LONG", "qty": 2,
                          "avgEntry": 100, "accountId": "ACC-R20", "orderId": "ENTRY-RUNNER",
                          "receipt": "RECEIPT-RUNNER", "filledAt": NOW.isoformat()}
    completed = ae.reconcile(
        BUNDLE, False, {"NQX_SYMBOL": "MNQU6", "NQX_AUTOTRADE": "1"},
        lambda _symbol: completed_position, lambda *_args: (_ for _ in ()).throw(
            AssertionError("completion adoption must not route")), ledger,
        broker_order_query=lambda _symbol: {"verified": True, "state": "FILLED",
                                             "orders": filled_rows, "activeOrders": []},
        state_query=lambda: (_ for _ in ()).throw(AssertionError("open completion needs no cycle")))
    check("subsequent qty2 converges to full owned state without ENTRY resend",
          "ownership" in completed[0], completed)
    tmp.cleanup()

unknown_tmp, unknown_ledger, unknown_record, unknown_notes, unknown_position, unknown_order = (
    run_partial_case("TP1", exact=False))
unknown_calls = []
unknown_followup = ae.reconcile(
    BUNDLE, False, {"NQX_SYMBOL": "MNQU6", "NQX_AUTOTRADE": "1", "NQX_LIVE_ORDERS": "1"},
    lambda _symbol: unknown_position,
    lambda args, live: (unknown_calls.append((args, live)) or (0, "stub")), unknown_ledger,
    broker_order_query=lambda _symbol: unknown_order,
    state_query=lambda: (_ for _ in ()).throw(AssertionError("unknown partial needs no entry seal")))
check("identity-unknown qty1 remains nonterminal, nonresendable, management runner zero",
      unknown_record["plan"].get("legIdentityKnown") is False
      and "ownership UNKNOWN" in unknown_followup[0] and not unknown_calls,
      f"{unknown_notes} {unknown_followup}")
unknown_tmp.cleanup()

plan = ae.build_management_plan(SCENARIO, BUNDLE, {})
check("partial binder requires account + orderId AND receipt",
      ae._bind_partial_leg(plan, {**position, "accountId": "OTHER"}, ROUTE_ROWS) is None
      and ae._bind_partial_leg(plan, {**position, "receipt": "OTHER"}, ROUTE_ROWS) is None
      and ae._bind_partial_leg(plan, {**position, "orderId": "OTHER"}, ROUTE_ROWS) is None)


# ---------------------------------------------------------------- historical HALT ordering

with tempfile.TemporaryDirectory() as tmp:
    ledger = os.path.join(tmp, "halt-ledger.jsonl")
    owned_position = {"verified": True, "symbol": "MNQU6", "side": "LONG", "qty": 1,
                      "avgEntry": 100, "accountId": "ACC-R20", "orderId": "ENTRY-RUNNER",
                      "receipt": "RECEIPT-RUNNER", "filledAt": NOW.isoformat()}
    raw_identity = broker_status.position_identity(owned_position)
    generation = f"PG:1:{raw_identity}"
    runner_plan = ae._bind_partial_leg(plan, owned_position, ROUTE_ROWS)
    owned_plan = ae._bind_position_ownership(runner_plan, owned_position, generation)
    owned_entry_orders = {"verified": True, "state": "FILLED", "activeOrders": [],
        "orders": [{"accountId": "ACC-R20", "symbol": "MNQU6", "action": "BUY",
                    "qty": 1, "orderType": "LIMIT", "limitPrice": 100,
                    "orderId": row["orderId"], "receipt": row["receipt"], "status": "FILLED"}
                   for row in ROUTE_ROWS]}
    ae._append_ledger({"key": f"POSITION_GENERATION:1:{raw_identity}",
                       "status": "POSITION_GENERATION", "generation": 1,
                       "identity": raw_identity, "open": True}, ledger)
    ae._append_ledger({"key": plan["entryKey"], "status": "ENTRY_SENT", "plan": owned_plan}, ledger)
    ae._append_ledger({"key": "old-halt", "status": "HALT", "reason": "old entry failure"}, ledger)
    calls = []
    def management_runner(args, live):
        calls.append((list(args), live))
        return 0, "management stub"
    managed = ae.reconcile(
        {**BUNDLE, "price": 115}, False,
        {"NQX_SYMBOL": "MNQU6", "NQX_AUTOTRADE": "1", "NQX_LIVE_ORDERS": "1"},
        lambda _symbol: dict(owned_position), management_runner, ledger,
        broker_order_query=lambda _symbol: copy.deepcopy(owned_entry_orders),
        state_query=lambda: (_ for _ in ()).throw(AssertionError("owned management needs no cycle")),
        claim_management=lambda intent: (True, {
            "managementKey": management_intent.management_key(intent), "claimToken": "M" * 43,
            "managementIntentHash": management_intent.intent_hash(intent)}))
    check("old HALT does not shadow owned runner MODIFY",
          "management_sent" in managed[0] and len(calls) == 2, managed)

    flatten_positions = iter([
        dict(owned_position),
        {"verified": True, "symbol": "MNQU6", "qty": 0,
         "closedAt": NOW.isoformat()},
    ])
    flatten_orders = iter([
        copy.deepcopy(owned_entry_orders),
        {"verified": True, "state": "NONE"},
    ])
    flatten_calls = []
    flattened = ae.reconcile(
        {**BUNDLE, "price": 131}, False,
        {"NQX_SYMBOL": "MNQU6", "NQX_AUTOTRADE": "1", "NQX_LIVE_ORDERS": "1"},
        lambda _symbol: next(flatten_positions),
        lambda args, live: (flatten_calls.append((list(args), live)) or (0, "flatten stub")),
        ledger, broker_order_query=lambda _symbol: next(flatten_orders),
        state_query=lambda: (_ for _ in ()).throw(AssertionError("final flatten needs no cycle")))
    check("old HALT does not shadow owned final-target flatten",
          "flatten_sent" in flattened[0] and len(flatten_calls) == 2, flattened)

    flat_calls = []
    flat = ae.reconcile(
        BUNDLE, True, {"NQX_SYMBOL": "MNQU6", "NQX_AUTOTRADE": "1",
                       "NQX_LIVE_ORDERS": "1"},
        lambda _symbol: {"verified": True, "symbol": "MNQU6", "qty": 0},
        lambda args, live: (flat_calls.append((args, live)) or (0, "stub")), ledger,
        broker_order_query=lambda _symbol: {"verified": True, "state": "NONE"}, state_query=view)
    check("old HALT blocks only flat new ENTRY", "old entry failure" in flat[0] and not flat_calls, flat)

    unowned_calls = []
    unowned = ae.reconcile(
        BUNDLE, False, {"NQX_SYMBOL": "OTHER", "NQX_AUTOTRADE": "1",
                        "NQX_LIVE_ORDERS": "1"},
        lambda _symbol: {"verified": True, "symbol": "OTHER", "side": "LONG", "qty": 1,
                         "accountId": "ACC-R20", "orderId": "MANUAL",
                         "receipt": "MANUAL-R", "filledAt": NOW.isoformat()},
        lambda args, live: (unowned_calls.append((args, live)) or (0, "stub")), ledger,
        broker_order_query=lambda _symbol: {"verified": True, "state": "NONE"})
    check("unowned position remains management runner zero despite historical HALT",
          ("ownership UNKNOWN" in unowned[0] or "no frozen management plan" in unowned[0])
          and not unowned_calls, unowned)


# ---------------------------------------------------------------- append-only ledger performance

with tempfile.TemporaryDirectory() as tmp:
    ledger = os.path.join(tmp, "100k.jsonl")
    with open(ledger, "w", encoding="utf-8") as fh:
        for index in range(100_000):
            fh.write(json.dumps({"key": f"K{index}", "status": "POSITION_GENERATION",
                                 "generation": index, "open": False}, separators=(",", ":")) + "\n")
        fh.write(json.dumps({"key": "PERF-HALT", "status": "HALT",
                             "reason": "performance fixture"}, separators=(",", ":")) + "\n")
    started = time.perf_counter()
    rows, error = ae._read_ledger(ledger)
    elapsed = time.perf_counter() - started
    check("100k append ledger is read once per reconcile-sized cycle",
          error is None and len(rows) == 100_001 and elapsed < 5.0,
          f"rows={len(rows)} error={error} elapsed={elapsed:.3f}s")
    print(f"INFO ledger_100k_read_sec={elapsed:.3f}")
    cycle_started = time.perf_counter()
    perf_notes = ae.reconcile(
        BUNDLE, True, {"NQX_SYMBOL": "MNQU6", "NQX_AUTOTRADE": "1"},
        lambda _symbol: {"verified": True, "symbol": "MNQU6", "qty": 0},
        lambda *_args: (_ for _ in ()).throw(AssertionError("HALT cycle must not route")), ledger,
        broker_order_query=lambda _symbol: {"verified": True, "state": "NONE"},
        state_query=lambda: (_ for _ in ()).throw(AssertionError("HALT cycle must not fetch state")))
    cycle_elapsed = time.perf_counter() - cycle_started
    check("100k ledger full reconcile performs one source read within budget",
          "performance fixture" in perf_notes[0] and cycle_elapsed < 5.0,
          f"notes={perf_notes} elapsed={cycle_elapsed:.3f}s")
    print(f"INFO ledger_100k_reconcile_sec={cycle_elapsed:.3f}")

print("ALL PASS (test_r20_execution_contract; external sends=0)")

# -*- coding: utf-8 -*-
"""R17 claim/partial/ownership/kill boundaries. All routes are local stubs."""
import json
import os
import subprocess
import sys
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "tests"))

import autotrade_engine as ae  # noqa: E402
import _pin_contract  # noqa: E402  (R102: 本番の manualHalt と限月をテストから切り離す)
import execution_intent  # noqa: E402
import nqx_state  # noqa: E402
import order  # noqa: E402
import route_envelope  # noqa: E402
import strategy_evidence  # noqa: E402
import telegram_bot as tb  # noqa: E402
from _hermetic import make_sandbox, dry_run  # noqa: E402


def check(label, condition, detail=""):
    if not condition:
        raise AssertionError(f"{label}: {detail}")
    print(f"OK   {label}")


NOW = datetime.now(timezone.utc)
EVIDENCE = strategy_evidence.canonicalize({
    "version": "R14-STRATEGY-EVIDENCE-1", "asOf": NOW.isoformat(),
    "sessionId": "NY-R17", "source": "fixture", "provenance": "test",
    "models": {"ifvg": {"valid": False}},
})
SCENARIO = {
    "scenarioId": "sc-r17", "fingerprint": "fp-r17", "evidenceHash": EVIDENCE["evidenceHash"],
    "marketCycleId": "cycle-r17", "symbol": "MNQU6", "side": "BUY", "qty": 2,
    "entry": 20000, "stop": 19960, "target": 20040, "targets": [20040, 20080],
    "legs": [{"id": "TP1", "qty": 1, "target": 20040},
             {"id": "RUNNER", "qty": 1, "target": 20080}],
    "planVersion": "R17-SPLIT-1", "grade": "A", "state": "ARMED",
    "issuedAt": NOW.isoformat(), "observedAt": NOW.isoformat(),
    "expiresAt": (NOW + timedelta(minutes=5)).isoformat(),
    "setupVersion": "R14-SETUP", "catalogVersion": "R14-CATALOG",
    "detectorVersion": "R14-DETECTOR",
    "executionContractVersion": "R22-EXECUTION-CONTRACT-1",
    "executionContract": {"accountScope": ["ACC-R17"]},
}


# All producers must collide on exactly the same authoritative identity.
key = nqx_state.entry_key(SCENARIO)
check("Bot/autotrade/direct publisher derive the same ENTRY key",
      key == tb._server_order_key(SCENARIO) == ae._entry_key(SCENARIO), key)


# Thread contention model for the signed producer call. The DO state-machine
# CAS itself is tested in cloudflare/test/r17_entry_claim.test.mjs.
guard = threading.Lock()
claimed = set()


def cas_publish(stream, payload, _cfg=None):
    with guard:
        if payload["entryKey"] in claimed:
            return False, {"reason": "ENTRY_CLAIM_ALREADY_HELD"}
        claimed.add(payload["entryKey"])
        intent = execution_intent.from_scenario(
            SCENARIO, order_type=payload["orderType"], account_scope=["ACC-R17"])
        return True, {"view": {"entryClaim": {
            "state": "CLAIMED", "entryKey": payload["entryKey"],
            "executionIntent": intent,
            "executionIntentHash": execution_intent.intent_hash(intent),
        }}}


results = []


def contender():
    results.append(nqx_state.claim_entry(SCENARIO, cfg={}, publish_fn=cas_publish)[0])


threads = [threading.Thread(target=contender) for _ in range(2)]
for thread in threads:
    thread.start()
for thread in threads:
    thread.join()
check("two producer threads receive exactly one claim", results.count(True) == 1, results)


with tempfile.TemporaryDirectory() as process_tmp:
    process_env = dict(os.environ)
    process_env["R17_SCENARIO_JSON"] = json.dumps(SCENARIO, separators=(",", ":"))
    process_env["R17_PROCESS_CLAIM_FILE"] = os.path.join(process_tmp, "entry.claim")
    helper = os.path.join(BASE, "tests", "r17_process_claim_helper.py")
    contenders = [subprocess.Popen(
        [sys.executable, helper], cwd=BASE, env=process_env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    ) for _ in range(2)]
    process_results = []
    process_errors = []
    for contender_process in contenders:
        stdout, stderr = contender_process.communicate(timeout=10)
        if contender_process.returncode != 0:
            process_errors.append(stderr or stdout)
            continue
        process_results.append(json.loads(stdout.strip().splitlines()[-1]))
    check("two independent producer processes receive exactly one atomic claim",
          sum(result.get("ok") is True for result in process_results) == 1
          and {result.get("entryKey") for result in process_results if result.get("ok")} == {key}
          and not process_errors,
          f"results={process_results} errors={process_errors}")


def _claim_race():
    """Return a two-party DO-like CAS stub and its accepted claim count."""
    race_lock = threading.Lock()
    rendezvous = threading.Barrier(2)
    accepted = []

    def claim(scenario, **kwargs):
        rendezvous.wait(timeout=5)
        entry_key_value = nqx_state.entry_key(scenario)
        with race_lock:
            if accepted:
                return False, {"reason": "ENTRY_CLAIM_ALREADY_HELD"}
            accepted.append(entry_key_value)
            order_type = str(kwargs.get("order_type") or "LIMIT").upper()
            market = ({"verified": True, "price": SCENARIO["entry"] - 1}
                      if order_type == "MARKET" else None)
            intent = execution_intent.from_scenario(
                scenario, market, order_type=order_type, account_scope=["ACC-R17"])
            return True, {"entryKey": entry_key_value, "claimToken": "C" * 43,
                          "executionIntent": intent,
                          "executionIntentHash": execution_intent.intent_hash(intent)}

    return claim, accepted


def _bot_race_patches(claim, confirm_calls):
    def run_order(args):
        if "--confirm" in args:
            confirm_calls.append(("BOT", list(args)))
        snapshot = [
            {"accountId": "ACC-R17", "legId": "TP1", "state": "ACCEPTED",
             "orderId": "R17-TP1", "receipt": "R17-RECEIPT-1"},
            {"accountId": "ACC-R17", "legId": "RUNNER", "state": "ACCEPTED",
             "orderId": "R17-RUNNER", "receipt": "R17-RECEIPT-2"},
        ]
        return True, route_envelope.format_envelope(snapshot, "SENT")

    return (
        patch.object(tb, "_authoritative_scenario_gate",
                     side_effect=lambda scenario, cfg, now: (True, dict(SCENARIO), None)),
        patch.object(tb, "_one_pass_server_gate", return_value=(True, None)),
        patch.object(tb, "preflight_position", return_value=(False, None, "stub")),
        patch.object(tb, "daily_order_count", return_value=((0, 99), "stub")),
        patch.object(tb, "consume_order_key", return_value=True),
        patch.object(tb, "run_order", side_effect=run_order),
        patch.object(tb, "live_orders_enabled", return_value=True),
        patch.object(tb, "_server_order_lock_active", return_value=False),
        patch.object(tb, "_set_server_order_lock", return_value=None),
        patch.object(tb, "_release_server_order_lock", return_value=None),
        patch.object(tb, "_publish_order_state", return_value=True),
        patch.object(tb, "_sync_position_quiet", return_value=(False, None)),
        patch.object(nqx_state, "claim_entry", side_effect=claim),
    )


def _run_bot_pair(modes):
    claim, accepted = _claim_race()
    confirm_calls = []
    errors = []

    def invoke(mode, index):
        try:
            tb.handle_scenario_order_confirmed(
                {"NQX_LIVE_ORDERS": "1"},
                {"scenario": dict(SCENARIO), "executionMode": mode,
                 "clientNonce": f"r17-race-client-{index:04d}"})
        except Exception as exc:  # pragma: no cover - reported as assertion detail
            errors.append(f"{type(exc).__name__}: {exc}")

    patches = _bot_race_patches(claim, confirm_calls)
    for active_patch in patches:
        active_patch.start()
    try:
        contenders = [threading.Thread(target=invoke, args=(mode, index))
                      for index, mode in enumerate(modes, 1)]
        for contender_thread in contenders:
            contender_thread.start()
        for contender_thread in contenders:
            contender_thread.join(timeout=10)
    finally:
        for active_patch in reversed(patches):
            active_patch.stop()
    return accepted, confirm_calls, errors


accepted, confirm_calls, errors = _run_bot_pair(["MANUAL_SLIDE", "AUTO_ONE_PASS"])
check("Bot MANUAL_SLIDE versus AUTO_ONE_PASS confirms exactly once",
      len(accepted) == 1 and len(confirm_calls) == 1 and not errors,
      f"accepted={accepted} confirms={confirm_calls} errors={errors}")


def _authoritative_view():
    server_scenario = dict(SCENARIO)
    server_scenario["cycleCommitted"] = True
    return {
        "market": {
            "at": NOW.isoformat(), "observedAt": NOW.isoformat(), "cvdAt": NOW.isoformat(),
            "cycleId": SCENARIO["marketCycleId"], "cycleCommitted": True, "verified": True,
            "source": "fixture", "sourceSymbol": "CME_MINI:MNQU6",
            "strategyEvidence": EVIDENCE,
        },
        "scenario": server_scenario,
        "position": {"state": "FLAT"},
        "order": {"state": "NONE", "verified": True},
        "display": {"orderable": True, "cyclePaired": True},
    }


# Bot and autotrade are distinct producers. They may both pass their local
# preflight, but the shared durable claim still permits only one live runner.
with tempfile.TemporaryDirectory() as race_tmp:
    claim, accepted = _claim_race()
    confirm_calls = []
    errors = []
    broker_queries = [0]

    def bot_invoke():
        try:
            tb.handle_scenario_order_confirmed(
                {"NQX_LIVE_ORDERS": "1"},
                {"scenario": dict(SCENARIO), "executionMode": "MANUAL_SLIDE",
                 "clientNonce": "r17-bot-auto-race-0001"})
        except Exception as exc:  # pragma: no cover - reported below
            errors.append(f"BOT {type(exc).__name__}: {exc}")

    def broker_query(symbol):
        broker_queries[0] += 1
        if broker_queries[0] == 1:
            return {"verified": True, "symbol": symbol, "qty": 0}
        return {"verified": True, "symbol": symbol, "side": "LONG", "qty": 2,
                "avgEntry": SCENARIO["entry"], "filledAt": NOW.isoformat()}

    def auto_runner(args, live):
        if live:
            confirm_calls.append(("AUTO", list(args)))
        return 0, "stub"

    def auto_invoke():
        try:
            bundle = {
                "_published_scenario": dict(SCENARIO), "price": SCENARIO["entry"] - 1,
                "at": NOW.isoformat(), "cvdAt": NOW.isoformat(),
                "snapshot": {"bars3m": [{"h": SCENARIO["entry"] + 1,
                                           "l": SCENARIO["entry"] - 2,
                                           "c": SCENARIO["entry"] - 1}] * 12},
            }
            ae.reconcile(
                bundle, True, {"NQX_SYMBOL": "MNQU6", "NQX_AUTOTRADE": "1",
                               "NQX_LIVE_ORDERS": "1"},
                broker_query=broker_query, runner=auto_runner,
                ledger_path=os.path.join(race_tmp, "bot-auto-race.jsonl"),
                broker_order_query=lambda _symbol: {"verified": True, "state": "NONE"},
                state_query=_authoritative_view, claim_entry=claim)
        except Exception as exc:  # pragma: no cover - reported below
            errors.append(f"AUTO {type(exc).__name__}: {exc}")

    patches = _bot_race_patches(claim, confirm_calls)
    for active_patch in patches:
        active_patch.start()
    try:
        contenders = [threading.Thread(target=bot_invoke), threading.Thread(target=auto_invoke)]
        for contender_thread in contenders:
            contender_thread.start()
        for contender_thread in contenders:
            contender_thread.join(timeout=10)
    finally:
        for active_patch in reversed(patches):
            active_patch.stop()
    check("Bot versus autotrade cross-producer race confirms exactly once",
          len(accepted) == 1 and len(confirm_calls) == 1 and not errors,
          f"accepted={accepted} confirms={confirm_calls} errors={errors}")


mixed = order.classify_route_results([
    ("ACC", "TP1", 200, json.dumps({"data": {"orderId": "O-1", "receipt": "R-1"}})),
    ("ACC", "RUNNER", 200, "ERROR broker rejected"),
], split=True)
check("HTTP 2xx plus ERROR body is PARTIAL", mixed["state"] == "PARTIAL", mixed)
check("partial preserves exact accepted/rejected counts",
      mixed["acceptedCount"] == 1 and mixed["explicitRejectCount"] == 1, mixed)


with tempfile.TemporaryDirectory() as tmp:
    original_key_file = tb.ORDER_KEY_FILE
    original_recover = nqx_state.recover_entry_claim
    original_fetch = nqx_state.fetch_state_quiet
    original_observe = nqx_state.publish_broker_observation
    tb.ORDER_KEY_FILE = os.path.join(tmp, "bot-keys.json")
    tb._set_server_order_lock(key, "UNKNOWN", SCENARIO, "R" * 43)
    recovery_calls = []
    nqx_state.recover_entry_claim = lambda entry_key, token, snapshot: (
        recovery_calls.append((entry_key, token, snapshot.get("snapshotHash"))) or (True, {}))
    nqx_state.fetch_state_quiet = lambda: {"entryClaim": {
        "entryKey": key, "executionIntentHash": "intent-r21",
        "routeSnapshot": [{"state": "ACCEPTED", "orderId": "O-R21"}]}}
    snapshot = {"snapshotHash": "bo_" + "1" * 64, "snapshotId": "bs-r22", "cursor": None}
    nqx_state.publish_broker_observation = lambda *_args, **_kwargs: (
        True, {"observation": snapshot})
    flat_position = {"verified": True, "qty": 0, "accountId": "ACC", "symbol": "MNQU6",
                     "observedAt": NOW.isoformat()}
    terminal_orders = {"verified": True, "state": "CANCELED", "orders": [
        {"orderId": "O-R21", "receipt": "R-R21", "accountId": "ACC",
         "symbol": "MNQU6", "status": "CANCELED"}], "observedAt": NOW.isoformat()}
    recovered, _detail = tb.recover_unknown_server_lock(
        key, flat_position, terminal_orders,
        position_query=lambda _symbol: dict(flat_position),
        orders_query=lambda _symbol, **_kwargs: dict(terminal_orders))
    check("UNKNOWN claim recovers only from explicit verified flat+terminal proof",
          recovered and recovery_calls == [(key, "R" * 43, snapshot["snapshotHash"])], recovery_calls)
    tb.ORDER_KEY_FILE = original_key_file
    nqx_state.recover_entry_claim = original_recover
    nqx_state.fetch_state_quiet = original_fetch
    nqx_state.publish_broker_observation = original_observe

    sandbox = make_sandbox(BASE)
    ok, out = dry_run(sandbox, ["--confirm", "--side", "buy", "--qty", "1",
                                 "--entry", "20000", "--sl", "19960", "--last", "20010",
                                 "--split-tp", "20040,20080"])
    check("order.py live qty=1 rejects before any route", not ok and "FIXED_QTY_REQUIRED" in out, out)
    ok, out = dry_run(sandbox, ["--confirm", "--side", "buy", "--qty", "2",
                                 "--entry", "20000", "--sl", "19960", "--last", "20010",
                                 "--split-tp", "20040,20080"])
    check("direct order.py live requires a DO claim token",
          not ok and "ENTRY_CLAIM_REQUIRED" in out, out)

    # A manual same-side position is not managed by an older automated plan.
    plan = ae.build_management_plan(SCENARIO, {"price": 20000}, {"NQX_SYMBOL": "MNQU6"})
    original_position = {
        "verified": True, "symbol": "MNQU6", "side": "LONG", "qty": 2,
        "avgEntry": 20000, "filledAt": "2026-08-22T10:00:00Z", "accountId": "ACC-R17",
    }
    owned = ae._bind_position_ownership(
        plan, original_position, "PG:1:" + ae._position_generation(original_position))
    ledger = os.path.join(tmp, "owned.jsonl")
    ae._append_ledger({"key": key, "status": "ENTRY_SENT", "plan": owned}, ledger)
    calls = []
    os.environ["NQX_AUTOTRADE"] = "1"
    os.environ["NQX_LIVE_ORDERS"] = "1"
    notes = ae.reconcile({"price": 20050}, False, {"NQX_SYMBOL": "MNQU6"},
        broker_query=lambda _symbol: {
            "verified": True, "symbol": "MNQU6", "side": "LONG", "qty": 2,
            "avgEntry": 20001, "filledAt": "2026-08-22T11:00:00Z", "accountId": "ACC-R17"},
        runner=lambda args, live: (calls.append((args, live)) or (0, "stub")),
        ledger_path=ledger,
        broker_order_query=lambda _symbol: {"verified": True, "state": "NONE"})
    check("same-side different position instance is never managed",
          not calls and "ownership UNKNOWN" in notes[0], notes)

    generation_path = os.path.join(tmp, "position-generation-r18.jsonl")
    first_instance = {
        "verified": True, "symbol": "MNQU6", "side": "LONG", "qty": 1,
        "accountId": "ACC-R17", "filledAt": "2026-08-22T08:00:00Z", "avgEntry": 20000,
    }
    generation1 = ae._observe_position_generation([], first_instance, generation_path)
    generation_rows = [json.loads(line) for line in open(generation_path, encoding="utf-8")]
    ae._observe_position_generation(generation_rows,
        {"verified": True, "symbol": "MNQU6", "qty": 0}, generation_path)
    generation_rows = [json.loads(line) for line in open(generation_path, encoding="utf-8")]
    second_instance = {**first_instance, "filledAt": "2026-08-22T13:00:00Z"}
    generation2 = ae._observe_position_generation(generation_rows, second_instance, generation_path)
    check("flat to open position generation increases monotonically",
          generation1.startswith("PG:1:") and generation2.startswith("PG:2:")
          and generation1 != generation2, (generation1, generation2))

    before = len(calls)
    notes = ae.reconcile({}, False, {"NQX_SYMBOL": "MNQU6", "NQX_AUTOTRADE_KILL": "1"},
        broker_query=lambda _symbol: {"verified": True, "symbol": "MNQU6", "side": "LONG", "qty": 1,
                                      "avgEntry": 20000, "filledAt": "2026-08-22T12:00:00Z"},
        runner=lambda args, live: (calls.append((args, live)) or (0, "stub")),
        ledger_path=os.path.join(tmp, "unverified-order.jsonl"),
        broker_order_query=lambda _symbol: {"verified": False, "state": "UNKNOWN"})
    check("kill with unverified broker order reaches runner zero",
          len(calls) == before and "UNVERIFIED" in notes[0], notes)

    # A historical kill record belongs only to its old flat→open generation.
    generation_ledger = os.path.join(tmp, "kill-generation.jsonl")
    old_position = {"verified": True, "symbol": "MNQU6", "side": "LONG", "qty": 1,
                    "avgEntry": 19990, "filledAt": "2026-08-22T09:00:00Z",
                    "accountId": "ACC-R17"}
    ae._append_ledger({"key": f"KILL:{ae._position_generation(old_position)}:FLATTEN",
                       "status": "FLATTEN_SENT", "action": "FLATTEN"}, generation_ledger)
    positions = iter([
        {"verified": True, "symbol": "MNQU6", "side": "LONG", "qty": 1,
         "avgEntry": 20010, "filledAt": "2026-08-22T12:30:00Z", "accountId": "ACC-R17"},
        {"verified": True, "symbol": "MNQU6", "qty": 0},
    ])
    orders = iter([{"verified": True, "state": "NONE"},
                   {"verified": True, "state": "NONE"}])
    generation_calls = []
    notes = ae.reconcile({}, False, {"NQX_SYMBOL": "MNQU6", "NQX_AUTOTRADE_KILL": "1"},
        broker_query=lambda _symbol: next(positions),
        runner=lambda args, live: (generation_calls.append((args, live)) or (0, "stub")),
        ledger_path=generation_ledger, broker_order_query=lambda _symbol: next(orders))
    check("new position generation gets one fresh kill confirm",
          len(generation_calls) == 2
          and generation_calls[0] == (["--flatten", "--account", "ACC-R17"], False)
          and generation_calls[1] == (["--flatten", "--account", "ACC-R17"], True)
          and "flatten sent" in notes[0],
          f"calls={generation_calls} notes={notes}")

print("ALL PASS (test_r17_execution_safety; external sends=0)")

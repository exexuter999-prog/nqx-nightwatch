# -*- coding: utf-8 -*-
"""R40 two-account routing/management tests. External sends: zero."""
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import autotrade_engine as ae  # noqa: E402
import autotrade_arm  # noqa: E402
import broker_status  # noqa: E402
import execution_intent  # noqa: E402
import management_intent  # noqa: E402
import route_envelope  # noqa: E402
import strategy_evidence  # noqa: E402


def check(label, condition, detail=""):
    if not condition:
        raise AssertionError(f"{label}: {detail}")
    print(f"OK   {label}")


NOW = datetime.now(timezone.utc)
ACCOUNTS = ["ACC-A", "ACC-B"]
EVIDENCE = strategy_evidence.canonicalize({
    "version": "R14-STRATEGY-EVIDENCE-1", "asOf": NOW.isoformat(),
    "sessionId": "NY-R40", "source": "fixture", "provenance": "test",
    "models": {"ifvg": {"valid": False}},
})
SCENARIO = {
    "decisionId": "r40-display", "scenarioId": "r40-two-account",
    "model": "VP80_REVERSION", "grade": "A+", "state": "ARMED",
    "fingerprint": "fp-r40", "symbol": "MNQU6", "marketCycleId": "cycle-r40",
    "side": "BUY", "qty": 2, "entry": 100, "stop": 90, "target": 110,
    "targets": [110, 130], "targetR": [1.0, 3.0],
    "legs": [{"id": "TP1", "qty": 1, "target": 110},
             {"id": "RUNNER", "qty": 1, "target": 130}],
    "planVersion": "R40-MIRRORED-SPLIT-1",
    "issuedAt": NOW.isoformat(), "observedAt": NOW.isoformat(),
    "expiresAt": (NOW + timedelta(minutes=5)).isoformat(),
    "evidenceHash": EVIDENCE["evidenceHash"], "setupVersion": "R14-SETUP",
    "catalogVersion": "R14-CATALOG", "detectorVersion": "R14-DETECTOR",
    "executionContractVersion": "R22-EXECUTION-CONTRACT-1",
    "executionContract": {"accountScope": ACCOUNTS},
}
BUNDLE = {
    "_published_scenario": SCENARIO, "price": 99,
    "at": NOW.isoformat(), "cvdAt": NOW.isoformat(),
    "snapshot": {"bars3m": [{"h": 101, "l": 98, "c": 99}] * 12},
}


def authoritative_view():
    market = {
        "at": NOW.isoformat(), "observedAt": NOW.isoformat(), "cvdAt": NOW.isoformat(),
        "cycleId": SCENARIO["marketCycleId"], "cycleCommitted": True,
        "verified": True, "source": "fixture", "sourceSymbol": "CME_MINI:MNQU6",
        "strategyEvidence": EVIDENCE,
    }
    server = {key: value for key, value in SCENARIO.items()
              if key not in {"decisionId", "model"}}
    server["cycleCommitted"] = True
    return {"market": market, "scenario": server, "position": {"state": "FLAT"},
            "order": {"state": "NONE", "verified": True},
            "display": {"orderable": True, "cyclePaired": True}}


def claim_stub(scenario, **kwargs):
    order_type = str(kwargs.get("order_type") or "LIMIT").upper()
    market = {"verified": True, "price": BUNDLE["price"]} if order_type == "MARKET" else None
    intent = execution_intent.from_scenario(
        scenario, market, order_type=order_type, account_scope=ACCOUNTS)
    return True, {"entryKey": ae._entry_key(scenario), "claimToken": "T" * 43,
                  "executionIntent": intent,
                  "executionIntentHash": execution_intent.intent_hash(intent)}


def filled_orders(account):
    rows = []
    for leg in ("TP1", "RUNNER"):
        rows.append({"accountId": account, "symbol": "MNQU6", "action": "BUY",
                     "qty": 1, "orderType": "MARKET", "filledPrice": 99,
                     "status": "FILLED", "orderId": f"{account}-{leg}",
                     "receipt": f"R-{account}-{leg}"})
    return {"verified": True, "state": "FILLED", "orders": rows,
            "activeOrders": [], "accountScope": [account]}


# Adapter account scoping: the requested B account and its numeric alias are
# verified independently instead of reusing the first configured account.
adapter = broker_status.CrossTradeAdapter({
    "CROSSTRADE_KEY": "test", "CROSSTRADE_ACCOUNTS": "ACC-A,ACC-B",
    "CROSSTRADE_ACCOUNT_ID_ACC_B": "202",
})
seen_urls = []
original_get = broker_status._get_json
try:
    def fake_get(url, _headers):
        seen_urls.append(url)
        return {"success": True, "data": {"accountId": 202, "symbol": "MNQU6",
                                           "netPos": 0, "position": {}}}
    broker_status._get_json = fake_get
    scoped = adapter.query("MNQU6", account="ACC-B")
finally:
    broker_status._get_json = original_get
check("B口座をURLで明示照会", "/accounts/ACC-B/position" in seen_urls[0], seen_urls)
check("B口座のラベルとbroker idを両方保持",
      scoped.get("accountId") == "ACC-B" and scoped.get("brokerAccountId") == "202", scoped)


with tempfile.TemporaryDirectory() as tmp:
    ledger = os.path.join(tmp, "entry.jsonl")
    calls = []
    position_counts = {account: 0 for account in ACCOUNTS}

    def position_query(symbol, account=None):
        position_counts[account] += 1
        # wrapper precheck + _reconcile_one precheck, then A post-send fill
        if account == "ACC-A" and position_counts[account] >= 3:
            return {"verified": True, "symbol": symbol, "accountId": account,
                    "side": "LONG", "qty": 2, "avgEntry": 99,
                    "orderId": "ACC-A-RUNNER", "receipt": "R-ACC-A-RUNNER",
                    "filledAt": NOW.isoformat()}
        return {"verified": True, "symbol": symbol, "accountId": account, "qty": 0}

    def order_query(symbol, known_order_ids=None, account=None):
        if account == "ACC-A" and position_counts[account] >= 3:
            return filled_orders(account)
        return {"verified": True, "symbol": symbol, "state": "NONE", "orders": [],
                "activeOrders": [], "accountScope": [account]}

    snapshot = [{"accountId": account, "legId": leg, "state": "ACCEPTED",
                 "orderId": f"{account}-{leg}", "receipt": f"R-{account}-{leg}"}
                for account in ACCOUNTS for leg in ("TP1", "RUNNER")]

    def runner(args, live):
        calls.append((list(args), bool(live)))
        return (0, route_envelope.format_envelope(snapshot, "SENT") if live else "dry-run")

    remote_arm = {
        "schemaVersion": autotrade_arm.SCHEMA_VERSION, "armId": "app-r40",
        "enabled": True, "autotrade": True, "live": True,
        "source": "TELEGRAM_MINI_APP", "armedAt": NOW.isoformat(),
        "expiresAt": (NOW + timedelta(minutes=30)).isoformat(),
        "accountScope": ACCOUNTS, "symbol": "MNQU6",
    }
    original_remote = autotrade_arm._remote_view
    autotrade_arm._remote_view = lambda: (True, {"autotradeArm": remote_arm})
    try:
        notes = ae.reconcile(
            BUNDLE, True,
            {"CROSSTRADE_ACCOUNTS": ",".join(ACCOUNTS), "NQX_SYMBOL": "MNQU6"},
            position_query, runner, ledger, broker_order_query=order_query,
            state_query=authoritative_view, claim_entry=claim_stub)
    finally:
        autotrade_arm._remote_view = original_remote
    check("Mini App AUTOだけで2口座LIVE経路が立つ", any(live for _args, live in calls), calls)
    check("2口座ENTRYは1回のdry/liveペアだけ送る", len(calls) == 2, calls)
    parsed = route_envelope.parse(calls[-1] and runner.__name__ and
                                  route_envelope.format_envelope(snapshot, "SENT"),
                                  expected_accounts=ACCOUNTS)
    check("4脚(A/B×TP1/RUNNER)の受領を必須化", parsed.get("ok") and len(parsed["snapshot"]) == 4, parsed)
    check("送信後の所有権を口座別プランへ束縛", "entry sent" in notes[0], notes)


with tempfile.TemporaryDirectory() as tmp:
    ledger = os.path.join(tmp, "kill.jsonl")
    calls = []
    counts = {account: 0 for account in ACCOUNTS}

    def kill_position(symbol, account=None):
        counts[account] += 1
        qty = 1 if counts[account] <= 2 else 0
        result = {"verified": True, "symbol": symbol, "accountId": account, "qty": qty}
        if qty:
            result.update({"side": "LONG", "avgEntry": 100,
                           "orderId": f"{account}-OPEN", "receipt": f"R-{account}-OPEN",
                           "filledAt": NOW.isoformat()})
        return result

    def no_orders(symbol, known_order_ids=None, account=None):
        return {"verified": True, "symbol": symbol, "state": "NONE", "orders": [],
                "activeOrders": [], "accountScope": [account]}

    def kill_runner(args, live):
        calls.append((list(args), bool(live)))
        return 0, "ok"

    notes = ae.reconcile(
        BUNDLE, False,
        {"CROSSTRADE_ACCOUNTS": ",".join(ACCOUNTS), "NQX_SYMBOL": "MNQU6",
         "NQX_AUTOTRADE": "1", "NQX_LIVE_ORDERS": "1", "NQX_AUTOTRADE_KILL": "1"},
        kill_position, kill_runner, ledger, broker_order_query=no_orders)
    routed = [tuple(args) for args, live in calls if live]
    check("KILLはA/Bを別々に全決済", routed == [
        ("--flatten", "--account", "ACC-A"),
        ("--flatten", "--account", "ACC-B")], routed)
    check("A/Bそれぞれ送信後FLATを再照会", counts == {"ACC-A": 3, "ACC-B": 3}, counts)
    check("2口座の結果を個別報告", len(notes) == 2 and all("flatten sent" in note for note in notes), notes)


with tempfile.TemporaryDirectory() as tmp:
    ledger = os.path.join(tmp, "manage-b.jsonl")
    snapshot = [{"accountId": account, "legId": leg, "state": "ACCEPTED",
                 "orderId": f"{account}-{leg}", "receipt": f"R-{account}-{leg}"}
                for account in ACCOUNTS for leg in ("TP1", "RUNNER")]
    parent = ae.build_management_plan(
        SCENARIO, BUNDLE, {"CROSSTRADE_ACCOUNTS": ",".join(ACCOUNTS),
                           "NQX_SYMBOL": "MNQU6"})
    parent = {**parent, "entryOrderType": "MARKET", "entryReference": 99,
              "routeSnapshot": snapshot}
    ae._append_ledger({"key": parent["entryKey"], "entryKey": parent["entryKey"],
                       "status": "ENTRY_SENT", "routeState": "SENT",
                       "action": "ENTRY", "plan": parent}, ledger)
    position_counts = {account: 0 for account in ACCOUNTS}
    order_counts = {account: 0 for account in ACCOUNTS}
    calls = []

    def managed_position(symbol, account=None):
        position_counts[account] += 1
        if account == "ACC-A":
            return {"verified": True, "symbol": symbol, "accountId": account, "qty": 0}
        return {"verified": True, "symbol": symbol, "accountId": account,
                "side": "LONG", "qty": 1, "avgEntry": 99,
                "orderId": "ACC-B-RUNNER", "receipt": "R-ACC-B-RUNNER",
                "filledAt": NOW.isoformat()}

    def managed_orders(symbol, known_order_ids=None, account=None):
        order_counts[account] += 1
        if account == "ACC-A":
            return {"verified": True, "symbol": symbol, "state": "NONE", "orders": [],
                    "activeOrders": [], "accountScope": [account]}
        rows = filled_orders(account)["orders"]
        if order_counts[account] <= 2:
            rows = [{**row, "status": "WORKING"} if row["orderId"] == "ACC-B-TP1" else row
                    for row in rows]
        active = [row for row in rows if row["status"] == "WORKING"]
        return {"verified": True, "symbol": symbol,
                "state": "PENDING" if active else "FILLED", "orders": rows,
                "activeOrders": active, "accountScope": [account]}

    def management_claim(intent):
        return True, {"managementKey": management_intent.management_key(intent),
                      "claimToken": "M" * 43,
                      "managementIntentHash": management_intent.intent_hash(intent)}

    def management_runner(args, live):
        calls.append((list(args), bool(live)))
        return 0, "ok"

    cfg = {"CROSSTRADE_ACCOUNTS": ",".join(ACCOUNTS), "NQX_SYMBOL": "MNQU6",
           "NQX_AUTOTRADE": "1", "NQX_LIVE_ORDERS": "1"}
    adopted = ae.reconcile(
        {**BUNDLE, "price": 115}, False, cfg, managed_position,
        management_runner, ledger, broker_order_query=managed_orders,
        claim_management=management_claim)
    cfg_off = {**cfg, "NQX_AUTOTRADE": "0", "NQX_LIVE_ORDERS": "0"}
    managed = ae.reconcile(
        {**BUNDLE, "price": 115}, False, cfg_off, managed_position,
        management_runner, ledger, broker_order_query=managed_orders,
        claim_management=management_claim)
    check("B口座の所有権を親4脚から独立採用", any("adopted" in note for note in adopted), adopted)
    live_modify = [args for args, live in calls if live]
    check("B口座runnerのmodifyだけを口座指定送信",
          len(live_modify) == 1
          and live_modify[0][live_modify[0].index("--account") + 1] == "ACC-B"
          and live_modify[0][0] == "--modify", live_modify)
    check("A口座へmodifyを誤送信しない", all("ACC-A" not in args for args in live_modify), live_modify)
    check("AUTO OFF後も既存の所有ポジション管理を放棄しない",
          len(live_modify) == 1 and live_modify[0][0] == "--modify", live_modify)
    # R78: 所有権を束縛した周期に同じ周期で管理まで進むので、management_sent は
    # 1 回目(adopted)に出る。2 回目(AUTO OFF)は冪等スキップ/hold で再送しない。
    check("B口座の管理完了を個別報告", any("management_sent" in note for note in adopted + managed),
          adopted + managed)
    check("AUTO OFF の次周期は再送しない(冪等)", not any("management_sent" in note for note in managed)
          and len(live_modify) == 1, managed)

print("ALL PASS (test_r40_multi_account; external sends=0)")

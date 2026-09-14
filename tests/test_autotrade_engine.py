# -*- coding: utf-8 -*-
"""自律エントリー・変更・決済の純粋な契約テスト。

CrossTrade へは接続しない。runner と broker query をスタブに差し替え、
ライブフラグがあるときも ``--confirm`` の呼び出し形だけを検証する。
"""
import copy
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
import autotrade_engine as ae  # noqa: E402

# 本番 .secrets/crosstrade.env から隔離する(tests/_hermetic.py と同じ方針)。
# reconcile / _reconcile_one は env を cfg にマージするため、設定口座がちょうど
# 1つだと KILL の flatten が `--flatten --account <口座>` になり、
# `== ["--flatten"]` を期待する検証が本番の口座数に暗黙依存していた
# (2026-09-04 に LFE 1口座へ切り替えて発覚)。各ケースは cfg を明示している。
ae._read_env_file = lambda path=None: {}
import execution_intent  # noqa: E402
import management_intent  # noqa: E402
import nqx_state  # noqa: E402
import route_envelope  # noqa: E402
import strategy_evidence  # noqa: E402


NOW = datetime.now(timezone.utc)
EVIDENCE = strategy_evidence.canonicalize({
    "version": "R14-STRATEGY-EVIDENCE-1", "asOf": NOW.isoformat(),
    "sessionId": "NY-TEST", "source": "fixture", "provenance": "test",
    "models": {"ifvg": {"valid": False}},
})
SCENARIO = {
    "decisionId": "decision-1", "scenarioId": "decision-1",
    "model": "VP80_REVERSION", "grade": "A+", "state": "ARMED",
    "fingerprint": "fp-decision-1", "symbol": "MNQU6", "marketCycleId": "cycle-test-1",
    "side": "BUY", "qty": 2, "entry": 100, "stop": 90, "target": 110,
    "targets": [110, 130], "targetR": [1.0, 3.0],
    "legs": [{"id": "TP1", "qty": 1, "target": 110},
             {"id": "RUNNER", "qty": 1, "target": 130}],
    "planVersion": "R17-SPLIT-1",
    "issuedAt": NOW.isoformat(), "observedAt": NOW.isoformat(), "expiresAt": (NOW + timedelta(minutes=5)).isoformat(),
    "evidenceHash": EVIDENCE["evidenceHash"], "setupVersion": "R14-SETUP",
    "catalogVersion": "R14-CATALOG", "detectorVersion": "R14-DETECTOR",
    "executionContractVersion": "R22-EXECUTION-CONTRACT-1",
    "executionContract": {"accountScope": ["ACC-TEST"]},
}
BUNDLE = {
    "_published_scenario": SCENARIO,
    "price": 99,
    "at": NOW.isoformat(), "cvdAt": NOW.isoformat(),
    "snapshot": {"bars3m": [{"h": 101, "l": 98, "c": 99}] * 12},
}
def _claim_stub(scenario, **kwargs):
    order_type = str(kwargs.get("order_type") or "LIMIT").upper()
    market = {"verified": True, "price": BUNDLE["price"]} if order_type == "MARKET" else None
    intent = execution_intent.from_scenario(
        scenario, market, order_type=order_type, account_scope=["ACC-TEST"])
    return True, {"entryKey": ae._entry_key(scenario), "claimToken": "T" * 43,
                  "executionIntent": intent,
                  "executionIntentHash": execution_intent.intent_hash(intent)}


nqx_state.claim_entry = _claim_stub


def authoritative_view(scenario=SCENARIO):
    """R15 committed, hash-only state for runner-free autotrade tests."""
    market = {
        "at": NOW.isoformat(), "observedAt": NOW.isoformat(), "cvdAt": NOW.isoformat(),
        "cycleId": scenario["marketCycleId"], "cycleCommitted": True, "verified": True,
        "source": "fixture", "sourceSymbol": "CME_MINI:MNQU6", "strategyEvidence": EVIDENCE,
    }
    server_scenario = {key: value for key, value in scenario.items() if key not in {"decisionId", "model"}}
    server_scenario["cycleCommitted"] = True
    return {
        "market": market, "scenario": server_scenario,
        "position": {"state": "FLAT"}, "order": {"state": "NONE", "verified": True},
        "display": {"orderable": True, "cyclePaired": True},
    }


def sticky(items):
    """R78: 束縛した周期は同じ周期で管理まで進む(reconcile 内で再照会が 1 回増える)。
    stub の列が尽きたら最後の観測を繰り返す。"""
    last = None
    for item in items:
        last = item
        yield item
    while True:
        yield last


def check(label, condition, detail=""):
    if not condition:
        raise AssertionError(f"{label}: {detail}")
    print(f"OK   {label}")


old_env = dict(os.environ)
try:
    plan = ae.build_management_plan(SCENARIO, BUNDLE, {"NQX_SYMBOL": "MNQU6"})
    check("TP1/runnerの独立ブラケット", plan["tp1"] == 110 and plan["finalTarget"] == 130
          and plan["mode"] == "SPLIT_BRACKETS_TP1_RUNNER" and len(plan["legs"]) == 2)
    check("トレール距離は正", plan["trailDistance"] > 0)
    check("通常口座上限は2枚60pt=$240",
          plan["riskCapPoints"] == 60.0 and plan["riskCapDollars"] == 240.0,
          str({key: plan.get(key) for key in ("riskCapPoints", "riskCapDollars", "riskCapSource")}))
    lifeline_cfg = {"NQX_SYMBOL": "MNQU6", "CROSSTRADE_ACCOUNTS": "ACC-TEST",
                    "RISK_ACC-TEST": "240", "LIFELINE_ACC-TEST": "120"}
    boundary = ae.build_management_plan(
        {**SCENARIO, "entry": 100, "stop": 40, "targets": [110, 130]}, BUNDLE, lifeline_cfg)
    check("LIFELINEは通常60pt上限を縮小しない",
          boundary["riskPoints"] == 60.0 and boundary["riskDollars"] == 240.0,
          str(boundary))
    try:
        ae.build_management_plan(
            {**SCENARIO, "entry": 100, "stop": 39.75, "targets": [110, 130]}, BUNDLE, lifeline_cfg)
    except ValueError as exc:
        check("60pt超はエンジン自身が拒否", "exceeds normal per-account cap" in str(exc), str(exc))
    else:
        raise AssertionError("60.25pt risk must be rejected by autotrade_engine")

    pos = {"verified": True, "side": "LONG", "qty": 2, "avgEntry": 100}
    action = ae.management_action(
        plan, pos,
        {**BUNDLE, "price": 115,
         "snapshot": {"bars3m": [{"h": 115, "l": 110, "c": 115}] * 12}},
        {"stop": 90}, {}, None)
    check("TP1前の2枚ブラケットを全量modifyしない", action is None)
    runner_pos = {"verified": True, "side": "LONG", "qty": 1, "avgEntry": 100}
    action = ae.management_action(
        plan, runner_pos,
        {**BUNDLE, "price": 115,
         "snapshot": {"bars3m": [{"h": 115, "l": 110, "c": 115}] * 12}},
        {"stop": 90}, {}, None)
    check("TP1後のrunnerだけ利益方向へmodify", action and action["action"] == "MODIFY"
          and action["qty"] == 1 and action["sl"] > 90)
    final = ae.management_action(plan, runner_pos, {**BUNDLE, "price": 131}, {"stop": action["sl"]}, {}, None)
    check("最終TPを越えて建玉が残ればflatten", final and final["action"] == "FLATTEN")

    with tempfile.TemporaryDirectory() as tmp:
        ledger = os.path.join(tmp, "autotrade.jsonl")
        calls = []

        query_count = [0]

        def flat_query(symbol):
            query_count[0] += 1
            if query_count[0] == 2:  # entry の送信直後だけ約定を観測
                return {"verified": True, "symbol": symbol, "side": "LONG",
                        "qty": 2, "avgEntry": 100, "accountId": "ACC-TEST",
                        "orderId": "ENTRY-RUNNER", "receipt": "RECEIPT-RUNNER",
                        "filledAt": NOW.isoformat()}
            return {"verified": True, "symbol": symbol, "qty": 0}

        def ok_runner(args, confirm):
            calls.append((list(args), bool(confirm)))
            if not confirm:
                return 0, "stub dry-run ok"
            snapshot = [
                {"accountId": "ACC-TEST", "legId": "TP1", "state": "ACCEPTED",
                 "orderId": "ENTRY-TP1", "receipt": "RECEIPT-TP1"},
                {"accountId": "ACC-TEST", "legId": "RUNNER", "state": "ACCEPTED",
                 "orderId": "ENTRY-RUNNER", "receipt": "RECEIPT-RUNNER"},
            ]
            return 0, route_envelope.format_envelope(snapshot, "SENT")

        os.environ["NQX_AUTOTRADE"] = "1"
        os.environ["NQX_LIVE_ORDERS"] = "1"
        exact_filled_orders = {"verified": True, "state": "FILLED", "activeOrders": [],
            "orders": [{"accountId": "ACC-TEST", "symbol": "MNQU6", "action": "BUY",
                        "qty": 1, "orderType": "MARKET", "filledPrice": 99,
                        "orderId": f"ENTRY-{leg}", "receipt": f"RECEIPT-{leg}",
                        "status": "FILLED"} for leg in ("TP1", "RUNNER")]}
        notes = ae.reconcile(BUNDLE, True, {"NQX_SYMBOL": "MNQU6"},
                             flat_query, ok_runner, ledger,
                             broker_order_query=lambda _symbol: copy.deepcopy(exact_filled_orders),
                             state_query=lambda: authoritative_view())
        check("ライブentryはdry-run→confirm", len(calls) == 2 and calls[0][1] is False and calls[1][1] is True, calls)
        check("entryはsplit TPをorder.pyへ渡す", "--split-tp" in calls[0][0], calls[0])
        notes2 = ae.reconcile(BUNDLE, True, {"NQX_SYMBOL": "MNQU6"},
                              flat_query, ok_runner, ledger,
                              broker_order_query=lambda _symbol: copy.deepcopy(exact_filled_orders),
                              state_query=lambda: authoritative_view())
        check("同じdecisionIdを二重送信しない", len(calls) == 2 and "idempotent" in notes2[0], notes2)
        rows = [json.loads(line) for line in open(ledger, encoding="utf-8")]
        check("ENTRY_SENTを台帳へ記録", any(row.get("status") == "ENTRY_SENT" for row in rows))

        def failing_runner(args, confirm):
            return (1, "partial/unknown") if confirm else (0, "dry ok")

        ledger2 = os.path.join(tmp, "halt.jsonl")
        notes3 = ae.reconcile({**BUNDLE, "_published_scenario": {**SCENARIO, "decisionId": "decision-2"}},
                              True, {"NQX_SYMBOL": "MNQU6"}, flat_query, failing_runner, ledger2,
                              broker_order_query=lambda _symbol: {"verified": True, "state": "FILLED"},
                              state_query=lambda: authoritative_view())
        check("失敗応答はHALT", "HALT" in notes3[0])
        notes4 = ae.reconcile({**BUNDLE, "_published_scenario": {**SCENARIO, "decisionId": "decision-2"}},
                              True, {"NQX_SYMBOL": "MNQU6"}, flat_query, ok_runner, ledger2,
                              broker_order_query=lambda _symbol: {"verified": True, "state": "FILLED"},
                              state_query=lambda: authoritative_view())
        check("HALT後は自動再送しない", "HALT" in notes4[0] and len(calls) == 2, notes4)
        for pending_state in ("PENDING", "SENT", "UNKNOWN"):
            before = len(calls)
            pending_notes = ae.reconcile(
                {**BUNDLE, "_published_scenario": {**SCENARIO, "decisionId": f"pending-{pending_state}"}},
                True, {"NQX_SYMBOL": "MNQU6"},
                lambda symbol: {"verified": True, "symbol": symbol, "qty": 0}, ok_runner,
                os.path.join(tmp, f"{pending_state}.jsonl"),
                broker_order_query=lambda _symbol, state=pending_state: {"verified": True, "state": state},
                state_query=lambda: authoritative_view())
            check(f"{pending_state} order blocks autotrade runner",
                  "ORDER_PENDING" in pending_notes[0] and len(calls) == before, pending_notes)
        before = len(calls)
        stale_cycle_notes = ae.reconcile(
            BUNDLE, False, {"NQX_SYMBOL": "MNQU6"},
            lambda symbol: {"verified": True, "symbol": symbol, "qty": 0}, ok_runner,
            os.path.join(tmp, "cycle-mismatch.jsonl"),
            broker_order_query=lambda _symbol: {"verified": True, "state": "NONE"})
        check("unpublished/partial cycle blocks autotrade runner",
              "frozen state was not published" in stale_cycle_notes[0] and len(calls) == before,
              stale_cycle_notes)

        fetches = [0]
        def counted_state():
            fetches[0] += 1
            return authoritative_view()
        before = len(calls)
        sealed_notes = ae.reconcile(
            {**BUNDLE, "_published_scenario": {**SCENARIO, "decisionId": "seal-counted"}},
            True, {"NQX_SYMBOL": "MNQU6"},
            lambda symbol: {"verified": True, "symbol": symbol, "qty": 0}, ok_runner,
            os.path.join(tmp, "seal-counted.jsonl"),
            broker_order_query=lambda _symbol: {"verified": True, "state": "NONE"},
            state_query=counted_state)
        check("sealed autotrade fetches authoritative state exactly once before runner",
              fetches[0] == 1 and len(calls) == before + 2 and "proposal ENTRY" not in sealed_notes[0],
              f"fetches={fetches[0]} calls={calls[before:]} notes={sealed_notes}")

        bad_view = authoritative_view()
        bad_view["market"] = {**bad_view["market"], "strategyEvidence": None}
        before = len(calls)
        tamper_notes = ae.reconcile(
            {**BUNDLE, "_published_scenario": {**SCENARIO, "decisionId": "seal-tamper"}},
            True, {"NQX_SYMBOL": "MNQU6"},
            lambda symbol: {"verified": True, "symbol": symbol, "qty": 0}, ok_runner,
            os.path.join(tmp, "seal-tamper.jsonl"),
            broker_order_query=lambda _symbol: {"verified": True, "state": "NONE"},
            state_query=lambda: bad_view)
        check("missing/tampered market evidence blocks autotrade runner",
              "CYCLE_MISMATCH" in tamper_notes[0] and len(calls) == before, tamper_notes)

        # R16: an already-open broker position is managed from its frozen
        # ledger plan without consulting an expired/mismatched/new-entry seal.
        management_ledger = os.path.join(tmp, "management-r16.jsonl")
        frozen = ae.build_management_plan(SCENARIO, BUNDLE, {"NQX_SYMBOL": "MNQU6"})
        management_route = [
            {"accountId": "ACC-TEST", "legId": "TP1", "state": "ACCEPTED",
             "orderId": "ENTRY-TP1", "receipt": "RECEIPT-TP1"},
            {"accountId": "ACC-TEST", "legId": "RUNNER", "state": "ACCEPTED",
             "orderId": "ENTRY-RUNNER", "receipt": "RECEIPT-RUNNER"},
        ]
        frozen = {**frozen, "entryOrderType": "MARKET", "entryReference": 99,
                  "routeSnapshot": management_route}
        managed_owned_position = {
            "verified": True, "symbol": "MNQU6", "side": "LONG", "qty": 1, "avgEntry": 100,
            "accountId": "ACC-TEST", "orderId": "ENTRY-RUNNER",
            "receipt": "RECEIPT-RUNNER", "filledAt": NOW.isoformat(),
        }
        frozen = ae._bind_position_ownership(
            frozen, managed_owned_position,
            "PG:1:" + ae._position_generation(managed_owned_position))
        ae._append_ledger({"key": frozen["entryKey"], "status": "ENTRY_SENT", "plan": frozen}, management_ledger)
        before = len(calls)
        managed_queries = [0]
        def managed_position(symbol):
            managed_queries[0] += 1
            return {"verified": True, "symbol": symbol, "side": "LONG", "qty": 1,
                    "avgEntry": 100, "accountId": "ACC-TEST", "orderId": "ENTRY-RUNNER",
                    "receipt": "RECEIPT-RUNNER",
                    "filledAt": NOW.isoformat()}
        expired = {**SCENARIO, "expiresAt": (NOW - timedelta(minutes=1)).isoformat(),
                   "marketCycleId": "mismatched-cycle", "decisionId": "display-only-changed"}
        managed_notes = ae.reconcile(
            {**BUNDLE, "price": 115, "_published_scenario": expired}, False,
            {"NQX_SYMBOL": "MNQU6"}, managed_position, ok_runner, management_ledger,
            broker_order_query=lambda _symbol: copy.deepcopy(exact_filled_orders),
            state_query=lambda: (_ for _ in ()).throw(AssertionError("management must not fetch cycle")),
            claim_management=lambda intent: (True, {
                "managementKey": management_intent.management_key(intent),
                "claimToken": "T" * 43,
                "managementIntentHash": management_intent.intent_hash(intent)}))
        check("open position management ignores expired/mismatched entry seal and state publish",
              len(calls) == before + 2 and "management_sent" in managed_notes[0],
              f"calls={calls[before:]} notes={managed_notes}")

        # A final-target flatten is journaled by the frozen entry key and may
        # not run again even if a lagging broker snapshot still reports open.
        final_ledger = os.path.join(tmp, "final-once-r16.jsonl")
        ae._append_ledger({"key": frozen["entryKey"], "status": "ENTRY_SENT", "plan": frozen}, final_ledger)
        final_positions = [
            {"verified": True, "symbol": "MNQU6", "side": "LONG", "qty": 1, "avgEntry": 100,
             "accountId": "ACC-TEST", "orderId": "ENTRY-RUNNER", "receipt": "RECEIPT-RUNNER",
             "filledAt": NOW.isoformat()},
            {"verified": True, "symbol": "MNQU6", "qty": 0},
            {"verified": True, "symbol": "MNQU6", "side": "LONG", "qty": 1, "avgEntry": 100,
             "accountId": "ACC-TEST", "orderId": "ENTRY-RUNNER", "receipt": "RECEIPT-RUNNER",
             "filledAt": NOW.isoformat()},
        ]
        def final_query(_symbol):
            return final_positions.pop(0)
        before = len(calls)
        final_notes = ae.reconcile({**BUNDLE, "price": 131}, False, {"NQX_SYMBOL": "MNQU6"},
                                   final_query, ok_runner, final_ledger,
                                   broker_order_query=lambda _symbol: copy.deepcopy(exact_filled_orders))
        final_notes2 = ae.reconcile({**BUNDLE, "price": 131}, False, {"NQX_SYMBOL": "MNQU6"},
                                    final_query, ok_runner, final_ledger,
                                    broker_order_query=lambda _symbol: copy.deepcopy(exact_filled_orders))
        check("final target flatten runs exactly once",
              len(calls) == before + 2 and "flatten_sent" in final_notes[0]
              and "idempotent" in final_notes2[0],
              f"calls={calls[before:]} notes={final_notes}/{final_notes2}")

        # KILL is a broker-truth exit route: no published state/cycle is read.
        kill_ledger = os.path.join(tmp, "kill-r16.jsonl")
        kill_positions = [
            {"verified": True, "symbol": "MNQU6", "side": "LONG", "qty": 1, "avgEntry": 100},
            {"verified": True, "symbol": "MNQU6", "qty": 0},
        ]
        before = len(calls)
        kill_notes = ae.reconcile(
            {}, False, {"NQX_SYMBOL": "MNQU6", "NQX_AUTOTRADE_KILL": "1"},
            lambda _symbol: kill_positions.pop(0), ok_runner, kill_ledger,
            broker_order_query=lambda _symbol: {"verified": True, "state": "NONE"},
            state_query=lambda: (_ for _ in ()).throw(AssertionError("kill must not fetch cycle")))
        # reconcile は本番 .secrets/crosstrade.env を cfg にマージする。設定口座が
        # ちょうど1つのとき _reconcile_one は `--flatten --account <口座>` を組む
        # (R40: 口座別の撤退経路)ので、`== ["--flatten"]` の完全一致は「本番の口座数が
        # 1でない」ことに暗黙依存していた(2026-09-04 に LFE 1口座へ切り替えて発覚)。
        # ここで確かめたいのは KILL が cycle seal を読まずに flatten を dry→live の
        # 2回で送ることであって、--account の有無ではない。
        check("state_ok=false kill flattens verified position without cycle seal",
              len(calls) == before + 2 and calls[before][0][:1] == ["--flatten"]
              and calls[before][0] == calls[before + 1][0]
              and "flatten sent" in kill_notes[0], f"calls={calls[before:]} notes={kill_notes}")

        pending_kill_ledger = os.path.join(tmp, "pending-kill-r16.jsonl")
        pending_positions = [
            {"verified": True, "symbol": "MNQU6", "side": "LONG", "qty": 1, "avgEntry": 100},
            {"verified": True, "symbol": "MNQU6", "qty": 0},
        ]
        before = len(calls)
        pending_order_checks = iter([
            {"verified": True, "state": "PENDING", "idempotencyKey": "server-order-key"},
            {"verified": True, "state": "CANCELED", "idempotencyKey": "server-order-key"},
        ])
        pending_kill = ae.reconcile(
            {}, False, {"NQX_SYMBOL": "MNQU6", "NQX_AUTOTRADE_KILL": "1"},
            lambda _symbol: pending_positions.pop(0), ok_runner, pending_kill_ledger,
            broker_order_query=lambda _symbol: next(pending_order_checks))
        pending_args = [item[0] for item in calls[before:]]
        check("pending-order kill uses regular flatteneverything route and post-verifies",
              len(pending_args) == 2 and pending_args[0] == ["--flatten"]
              and "flatten sent" in pending_kill[0],
              f"calls={calls[before:]} notes={pending_kill}")

        before = len(calls)
        unverified_notes = ae.reconcile(
            {}, False, {"NQX_SYMBOL": "MNQU6", "NQX_AUTOTRADE_KILL": "1"},
            lambda _symbol: {"verified": False, "qty": 1}, ok_runner,
            os.path.join(tmp, "unverified-r16.jsonl"))
        check("unverified broker position is fail-closed with runner zero",
              len(calls) == before and "UNVERIFIED" in unverified_notes[0], unverified_notes)

        # decisionId/client nonce are presentation only; the server tuple owns
        # the durable entry identity.  A genuinely different committed cycle
        # is the only way the same named setup can create another ENTRY.
        identity_ledger = os.path.join(tmp, "identity-r16.jsonl")
        identity_positions = [
            {"verified": True, "symbol": "MNQU6", "qty": 0},
            {"verified": True, "symbol": "MNQU6", "side": "LONG", "qty": 2, "avgEntry": 100,
             "accountId": "ACC-TEST", "orderId": "ENTRY-RUNNER", "receipt": "RECEIPT-RUNNER",
             "filledAt": NOW.isoformat()},
            {"verified": True, "symbol": "MNQU6", "qty": 0},
        ]
        before = len(calls)
        ae.reconcile({**BUNDLE, "clientNonce": "nonce-a"}, True, {"NQX_SYMBOL": "MNQU6"},
                     lambda _symbol: identity_positions.pop(0), ok_runner, identity_ledger,
                     broker_order_query=lambda _symbol: copy.deepcopy(exact_filled_orders),
                     state_query=lambda: authoritative_view())
        changed_display = {**BUNDLE, "clientNonce": "nonce-b",
                           "_published_scenario": {**SCENARIO, "decisionId": "different-display-id"}}
        duplicate = ae.reconcile(
            changed_display, True, {"NQX_SYMBOL": "MNQU6"},
            lambda _symbol: identity_positions.pop(0), ok_runner, identity_ledger,
            broker_order_query=lambda _symbol: copy.deepcopy(exact_filled_orders),
            state_query=lambda: authoritative_view())
        check("decisionId/nonce change cannot resend same authoritative tuple",
              len(calls) == before + 2 and "idempotent" in duplicate[0],
              f"calls={calls[before:]} notes={duplicate}")

        cycle2 = {**SCENARIO, "marketCycleId": "cycle-test-2", "decisionId": "display-still-irrelevant"}
        cycle2_positions = [
            {"verified": True, "symbol": "MNQU6", "qty": 0},
            {"verified": True, "symbol": "MNQU6", "side": "LONG", "qty": 2, "avgEntry": 100,
             "accountId": "ACC-TEST", "orderId": "ENTRY-RUNNER", "receipt": "RECEIPT-RUNNER",
             "filledAt": NOW.isoformat()},
        ]
        cycle2_notes = ae.reconcile(
            {**BUNDLE, "_published_scenario": cycle2}, True, {"NQX_SYMBOL": "MNQU6"},
            lambda _symbol: cycle2_positions.pop(0), ok_runner, identity_ledger,
            broker_order_query=lambda _symbol: copy.deepcopy(exact_filled_orders),
            state_query=lambda: authoritative_view(cycle2))
        check("different valid committed cycle gets a distinct ENTRY key",
              len(calls) == before + 4 and "entry sent" in cycle2_notes[0], cycle2_notes)

        # R18: a resting LIMIT is a durable accepted claim, not an execution
        # failure.  Its first verified fill is adopted only through the frozen
        # broker order id, then normal management starts on the next cycle.
        resting_ledger = os.path.join(tmp, "resting-limit-r18.jsonl")
        resting_calls = []
        resting_positions = sticky([
            {"verified": True, "symbol": "MNQU6", "accountId": "ACC-TEST", "qty": 0},
            {"verified": True, "symbol": "MNQU6", "accountId": "ACC-TEST", "qty": 0},
            {"verified": True, "symbol": "MNQU6", "side": "LONG", "qty": 2,
             "avgEntry": 100, "accountId": "ACC-TEST", "orderId": "ENTRY-LIMIT-2",
             "receipt": "R-LIMIT-2",
             "filledAt": NOW.isoformat()},
            {"verified": True, "symbol": "MNQU6", "side": "LONG", "qty": 1,
             "avgEntry": 100, "accountId": "ACC-TEST", "orderId": "ENTRY-LIMIT-2",
             "receipt": "R-LIMIT-2",
             "filledAt": NOW.isoformat()},
            {"verified": True, "symbol": "MNQU6", "side": "LONG", "qty": 1,
             "avgEntry": 100, "accountId": "ACC-TEST", "orderId": "ENTRY-LIMIT-2",
             "receipt": "R-LIMIT-2",
             "filledAt": NOW.isoformat()},
        ])
        resting_active_rows = [
            {"accountId": "ACC-TEST", "symbol": "MNQU6", "action": "BUY", "qty": 1,
             "orderType": "LIMIT", "limitPrice": 100, "status": "WORKING",
             "orderId": f"ENTRY-LIMIT-{index}", "receipt": f"R-LIMIT-{index}"}
            for index in (1, 2)]
        resting_filled_rows = [{**row, "status": "FILLED"} for row in resting_active_rows]
        resting_orders = sticky([
            {"verified": True, "state": "NONE", "orders": [], "activeOrders": []},
            {"verified": True, "state": "PENDING", "orders": resting_active_rows,
             "activeOrders": resting_active_rows},
            {"verified": True, "state": "FILLED", "orders": resting_filled_rows,
             "activeOrders": []},
            {"verified": True, "state": "FILLED", "orders": resting_filled_rows,
             "activeOrders": []},
        ])
        def resting_runner(args, live):
            resting_calls.append((list(args), bool(live)))
            if not live:
                return 0, "stub dry-run"
            snapshot = [
                {"accountId": "ACC-TEST", "legId": "TP1", "state": "ACCEPTED",
                 "orderId": "ENTRY-LIMIT-1", "receipt": "R-LIMIT-1"},
                {"accountId": "ACC-TEST", "legId": "RUNNER", "state": "ACCEPTED",
                 "orderId": "ENTRY-LIMIT-2", "receipt": "R-LIMIT-2"},
            ]
            return 0, route_envelope.format_envelope(snapshot, "SENT")
        limit_bundle = {**BUNDLE, "price": 101}
        accepted = ae.reconcile(
            limit_bundle, True, {"NQX_SYMBOL": "MNQU6", "NQX_AUTOTRADE": "1",
                                 "NQX_LIVE_ORDERS": "1"},
            lambda _symbol: next(resting_positions), resting_runner, resting_ledger,
            broker_order_query=lambda _symbol: next(resting_orders),
            state_query=lambda: authoritative_view())
        adopted = ae.reconcile(
            limit_bundle, True, {"NQX_SYMBOL": "MNQU6", "NQX_AUTOTRADE": "1",
                                 "NQX_LIVE_ORDERS": "1"},
            lambda _symbol: next(resting_positions), resting_runner, resting_ledger,
            broker_order_query=lambda _symbol: next(resting_orders),
            state_query=lambda: (_ for _ in ()).throw(AssertionError("open fill needs no entry seal")))
        managed = ae.reconcile(
            {**BUNDLE, "price": 115}, False,
            {"NQX_SYMBOL": "MNQU6", "NQX_AUTOTRADE": "1", "NQX_LIVE_ORDERS": "1"},
            lambda _symbol: next(resting_positions), resting_runner, resting_ledger,
            broker_order_query=lambda _symbol: next(resting_orders),
            state_query=lambda: (_ for _ in ()).throw(AssertionError("management needs no entry seal")),
            claim_management=lambda intent: (True, {
                "managementKey": management_intent.management_key(intent),
                "claimToken": "T" * 43,
                "managementIntentHash": management_intent.intent_hash(intent)}))
        resting_rows = [json.loads(line) for line in open(resting_ledger, encoding="utf-8")]
        check("resting LIMIT freezes accepted order identity",
              "accepted/pending" in accepted[0]
              and any(row.get("status") == "ENTRY_RESTING" for row in resting_rows), accepted)
        check("later strong broker fill adopts the frozen pending plan without resend",
              "adopted" in adopted[0] and len(resting_calls) == 4
              and any(row.get("action") == "ENTRY_OWNERSHIP_BOUND" for row in resting_rows),
              f"calls={resting_calls} notes={adopted}")
        check("adopted LIMIT fill is managed on the next cycle",
              "management_sent" in managed[0], managed)

        legacy_ledger = os.path.join(tmp, "legacy-migrate-r16.jsonl")
        legacy_plan = {"decisionId": SCENARIO["scenarioId"], "symbol": "MNQU6",
                       "side": "BUY", "qty": 2, "entry": 100, "initialStop": 90,
                       "tp1": 110, "finalTarget": 130, "targets": [110, 130],
                       "legs": [{"id": "TP1", "qty": 1, "target": 110},
                                {"id": "RUNNER", "qty": 1, "target": 130}]}
        ae._append_ledger({"key": SCENARIO["scenarioId"], "status": "ENTRY_SENT",
                           "plan": legacy_plan}, legacy_ledger)
        before = len(calls)
        migrated = ae.reconcile(
            BUNDLE, True, {"NQX_SYMBOL": "MNQU6"},
            lambda symbol: {"verified": True, "symbol": symbol, "qty": 0}, ok_runner, legacy_ledger,
            broker_order_query=lambda _symbol: {"verified": True, "state": "NONE"},
            state_query=lambda: authoritative_view())
        migrated_rows = [json.loads(line) for line in open(legacy_ledger, encoding="utf-8")]
        check("legacy decisionId ledger row migrates to tuple duplicate guard",
              len(calls) == before and "legacy entry migrated" in migrated[0]
              and migrated_rows[-1]["status"] == "ENTRY_MIGRATED", migrated)
finally:
    os.environ.clear()
    os.environ.update(old_env)

print("ALL PASS (test_autotrade_engine)")

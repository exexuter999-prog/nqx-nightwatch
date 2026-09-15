#!/usr/bin/env python3
"""R22 monotonic broker truth, production recovery, and identity boundaries."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import autotrade_engine as ae
import _pin_contract  # noqa: E402  (R102: 本番の manualHalt と限月をテストから切り離す)
import broker_status
import nqx_state
import route_envelope
import telegram_bot as tb

AT = "2026-08-23T12:00:00Z"


def flat_position(**changes):
    row = {"verified": True, "qty": 0, "side": "FLAT", "accountId": "ACC-R22",
           "symbol": "MNQU6", "observedAt": AT}
    row.update(changes)
    return row


def terminal_orders(**changes):
    rows = [{"orderId": f"O-{leg}", "receipt": f"R-{leg}",
             "accountId": "ACC-R22", "symbol": "MNQU6", "status": "CANCELED"}
            for leg in ("TP1", "RUNNER")]
    view = {"verified": True, "platform": "STUB", "symbol": "MNQU6",
            "state": "CANCELED", "observedAt": AT, "orders": rows}
    view.update(changes)
    return view


class R22RecoveryTests(unittest.TestCase):
    def test_python_worker_canonical_observation_hash_parity(self):
        observation = nqx_state.build_broker_observation(
            flat_position(), terminal_orders(), "intent-r22")
        script = ("import {brokerObservationHash} from './cloudflare/src/state_machine.js';"
                  "let s='';process.stdin.on('data',d=>s+=d);"
                  "process.stdin.on('end',()=>console.log(brokerObservationHash(JSON.parse(s))));")
        proc = subprocess.run(
            ["node", "--input-type=module", "-e", script], cwd=BASE,
            input=json.dumps(observation), text=True, capture_output=True, check=True)
        self.assertEqual(proc.stdout.strip(), observation["snapshotHash"])
        self.assertEqual(observation["snapshotMode"], "STABLE_DOUBLE_READ")
        self.assertEqual(observation["stableBeforeHash"], observation["stableAfterHash"])

    def test_component_skew_and_unstable_double_read_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "COMPONENT_SKEW"):
            nqx_state.build_broker_observation(
                flat_position(observedAt="2026-08-23T12:00:00Z"),
                terminal_orders(observedAt="2026-08-23T12:00:02.001Z"), "intent")
        # closedAt だけの差は「建玉の変化」ではない(R40)。フラット口座では
        # broker_status がブローカーの timestamp を得られず観測時刻へ
        # フォールバックするので、毎読み値が変わる。ここを不安定と見なすと
        # 二重読みが原理的に成立せず、回復が必要なフラット局面で必ず塞がる。
        stable = nqx_state.build_broker_observation(
            flat_position(), terminal_orders(), "intent",
            before_position=flat_position(),
            after_position=flat_position(closedAt="2026-08-23T12:00:01Z"))
        self.assertEqual(stable["snapshotMode"], "STABLE_DOUBLE_READ")
        self.assertEqual(stable["stableBeforeHash"], stable["stableAfterHash"])
        # 実体のある差は従来どおり止める。
        for changed in ({"qty": 2}, {"side": "LONG"}, {"orderId": "O-NEW"}):
            with self.subTest(changed=changed):
                with self.assertRaisesRegex(ValueError, "UNSTABLE_POSITION"):
                    nqx_state.build_broker_observation(
                        flat_position(), terminal_orders(), "intent",
                        before_position=flat_position(),
                        after_position=flat_position(**changed))

    def test_recover_payload_is_exact_snapshot_cas(self):
        snapshot = nqx_state.build_broker_observation(
            flat_position(), terminal_orders(), "intent")
        captured = []
        ok, _ = nqx_state.recover_entry_claim(
            "ENTRY:" + "a" * 64, "T" * 43, snapshot,
            publish_fn=lambda stream, payload, _cfg: (
                captured.append((stream, payload)) or (True, {})))
        self.assertTrue(ok)
        payload = captured[0][1]
        self.assertEqual(payload["brokerSnapshotHash"], snapshot["snapshotHash"])
        self.assertEqual(payload["brokerSnapshotId"], snapshot["snapshotId"])
        self.assertEqual(payload["brokerCursor"], "")

    def test_autotrade_startup_calls_production_recovery_before_flat_entry_gate(self):
        entry_key = "ENTRY:" + "b" * 64
        claim = {"entryKey": entry_key, "state": "CONSUMED",
                 "routeState": "UNKNOWN", "executionIntentHash": "intent-r22",
                 "routeSnapshot": [
                     {"state": "ACCEPTED", "orderId": "O-TP1", "receipt": "R-TP1"},
                     {"state": "ACCEPTED", "orderId": "O-RUNNER", "receipt": "R-RUNNER"},
                 ]}
        journal = {"entryKey": entry_key, "claimToken": "T" * 43,
                   "executionIntentHash": "intent-r22", "executionIntent": {"version": "fixture"}}
        snapshot = {"snapshotHash": "bo_" + "c" * 64,
                    "snapshotId": "bs-startup", "cursor": None}
        publish_calls, recover_calls, runner_calls = [], [], []
        with tempfile.TemporaryDirectory() as tmp:
            ledger = os.path.join(tmp, "ledger.jsonl")
            ae._append_ledger({"key": entry_key, "status": "ENTRY_CLAIMED",
                               "action": "ENTRY_CLAIM", "claimJournal": journal}, ledger)
            ae._append_ledger({"key": entry_key, "status": "HALT",
                               "action": "ENTRY", "reason": "producer crash"}, ledger)
            with patch.object(nqx_state, "publish_broker_observation",
                              side_effect=lambda *args, **kwargs: (
                                  publish_calls.append((args, kwargs)) or
                                  (True, {"observation": snapshot}))), \
                 patch.object(nqx_state, "recover_entry_claim",
                              side_effect=lambda key, token, observed: (
                                  recover_calls.append((key, token, observed)) or (True, {"ok": True}))):
                notes = ae.reconcile(
                    {"scenarios": {"primary": {"symbol": "MNQU6"}}}, state_ok=False,
                    cfg={"NQX_AUTOTRADE": "1", "NQX_AUTOTRADE_LIVE": "1"},
                    broker_query=lambda _symbol: flat_position(),
                    broker_order_query=lambda _symbol, **_kwargs: terminal_orders(),
                    state_query=lambda: {"entryClaim": claim}, ledger_path=ledger,
                    runner=lambda *_args: (runner_calls.append(True) or (0, "unexpected")))
            records, error = ae._read_ledger(ledger)
        self.assertIsNone(error)
        self.assertEqual(len(publish_calls), 1)
        self.assertEqual(recover_calls, [(entry_key, "T" * 43, snapshot)])
        self.assertTrue(any(row.get("status") == "ENTRY_RECOVERED" for row in records))
        self.assertFalse(runner_calls)
        self.assertIn("frozen state was not published", notes[0])

    def test_recovered_claim_with_unknown_route_is_not_a_startup_lock(self):
        """R52: RECOVERED(DO が終端済み)の claim は routeState=UNKNOWN でも回復しない。

        2026-09-04 に口座を入れ替えた直後、8/28 の RECOVERED claim(scope は旧口座)を
        engine が毎サイクル回復しようとし、新口座の観測が DO の 409 で弾かれて
        新規 ENTRY が全部止まった。RECOVERED は ENTRY_CLAIM_ACTIVE に無い終端状態。
        """
        entry_key = "ENTRY:" + "d" * 64
        claim = {"entryKey": entry_key, "state": "RECOVERED",
                 "routeState": "UNKNOWN", "acceptedCount": 0,
                 "executionIntentHash": "intent-r52",
                 "executionIntent": {"accountScope": ["OLD-A", "OLD-B"]},
                 "routeSnapshot": [{"state": "UNKNOWN", "orderId": None, "receipt": None}]}
        journal = {"entryKey": entry_key, "claimToken": "T" * 43,
                   "executionIntentHash": "intent-r52", "executionIntent": {"version": "fixture"}}
        publish_calls, recover_calls, runner_calls = [], [], []
        with tempfile.TemporaryDirectory() as tmp:
            ledger = os.path.join(tmp, "ledger.jsonl")
            ae._append_ledger({"key": entry_key, "status": "ENTRY_CLAIMED",
                               "action": "ENTRY_CLAIM", "claimJournal": journal}, ledger)
            with patch.object(nqx_state, "publish_broker_observation",
                              side_effect=lambda *args, **kwargs: (
                                  publish_calls.append((args, kwargs)) or
                                  (False, {"reason": "broker observation cannot overwrite another scope/intent",
                                           "status": 409}))),                  patch.object(nqx_state, "recover_entry_claim",
                              side_effect=lambda key, token, observed: (
                                  recover_calls.append((key, token, observed)) or (False, {}))):
                notes = ae.reconcile(
                    {"scenarios": {"primary": {"symbol": "MNQU6"}}}, state_ok=False,
                    cfg={"NQX_AUTOTRADE": "1", "NQX_AUTOTRADE_LIVE": "1"},
                    broker_query=lambda _symbol: flat_position(),
                    broker_order_query=lambda _symbol, **_kwargs: terminal_orders(),
                    state_query=lambda: {"entryClaim": claim}, ledger_path=ledger,
                    runner=lambda *_args: (runner_calls.append(True) or (0, "unexpected")))
        self.assertEqual(publish_calls, [])
        self.assertEqual(recover_calls, [])
        self.assertFalse(runner_calls)
        self.assertNotIn("startup recovery failed", notes[0])
        self.assertIn("frozen state was not published", notes[0])

    def test_bot_and_auto_startup_call_management_recovery_without_runner(self):
        management_key = "MANAGEMENT:" + "d" * 64
        intent_hash = "mi_" + "e" * 64
        claim = {"managementKey": management_key, "managementIntentHash": intent_hash,
                 "state": "RESOLVED", "routeState": "UNKNOWN",
                 "managementIntent": {"symbol": "MNQU6"},
                 "routeReceipt": {"accounts": [{"stopOrderId": "SL", "targetOrderId": "TP",
                                                   "receipt": "MR"}]}}
        token = "M" * 43
        journal = {"managementKey": management_key, "managementIntentHash": intent_hash,
                   "claimToken": token, "managementIntent": {"symbol": "MNQU6"}}
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            original_key_file = tb.ORDER_KEY_FILE
            tb.ORDER_KEY_FILE = os.path.join(tmp, "bot-locks.json")
            lock_key = tb._set_management_server_lock(journal)
            notes = tb.reconcile_startup_claims(
                state_fetch=lambda: {"managementClaim": claim},
                position_query=lambda _symbol: flat_position(),
                orders_query=lambda _symbol, **_kwargs: terminal_orders(),
                management_recover=lambda key, supplied_token, current, **_kwargs: (
                    calls.append((key, supplied_token, current["managementIntentHash"]))
                    or (True, {"ok": True})))
            self.assertNotIn(lock_key, tb._load_server_order_locks())
            tb.ORDER_KEY_FILE = original_key_file
        self.assertEqual(calls, [(management_key, token, intent_hash)])
        self.assertIn("MANAGEMENT recovered", notes[0])

        open_position = {"verified": True, "qty": 1, "side": "LONG",
                         "accountId": "ACC-R22", "symbol": "MNQU6",
                         "orderId": "ENTRY", "receipt": "ENTRY-R", "filledAt": AT,
                         "observedAt": AT}
        runner_calls, auto_calls = [], []
        with tempfile.TemporaryDirectory() as tmp:
            ledger = os.path.join(tmp, "auto-ledger.jsonl")
            ae._append_ledger({"key": "M-ACTION", "status": "MANAGEMENT_CLAIMED",
                               "action": "MODIFY", "managementClaimJournal": journal}, ledger)
            notes = ae.reconcile(
                {"scenarios": {"primary": {"symbol": "MNQU6"}}},
                cfg={"NQX_AUTOTRADE": "1", "NQX_AUTOTRADE_LIVE": "1"},
                broker_query=lambda _symbol: dict(open_position),
                broker_order_query=lambda _symbol, **_kwargs: terminal_orders(),
                state_query=lambda: {"managementClaim": claim}, ledger_path=ledger,
                recover_management=lambda current, local, *_args: (
                    auto_calls.append((current["managementKey"], local["claimToken"]))
                    or (True, {"ok": True})),
                runner=lambda *_args: (runner_calls.append(True) or (0, "unexpected")))
            records, error = ae._read_ledger(ledger)
        self.assertIsNone(error)
        self.assertEqual(auto_calls, [(management_key, token)])
        self.assertTrue(any(row.get("status") == "MANAGEMENT_RECOVERED" for row in records))
        self.assertFalse(runner_calls)
        self.assertIn("no duplicate route", notes[0])

        # R52: 別世代(閉じた建玉)の RESOLVED/UNKNOWN claim はロックではない。回復を
        # 試みず(回復証明は原理的に通らない)、今の建玉の管理判定へ進む。
        stale_claim = {"managementKey": management_key, "managementIntentHash": intent_hash,
                       "state": "RESOLVED", "routeState": "UNKNOWN",
                       "routeReceipt": {"route": "not fully accepted"},
                       "managementIntent": {"accountId": "ACC-R22", "symbol": "MNQU6",
                                            "positionGeneration": "POS:" + "0" * 64,
                                            "action": "MODIFY", "side": "BUY", "qty": 1}}
        stale_calls = []
        with tempfile.TemporaryDirectory() as tmp:
            ledger = os.path.join(tmp, "auto-ledger.jsonl")
            ae._append_ledger({"key": "M-ACTION", "status": "HALT",
                               "action": {"action": "MODIFY", "qty": 1, "sl": 19990.0},
                               "reason": "live send failed/unknown: MODIFY_ROUTE_UNKNOWN",
                               "managementClaimJournal": journal}, ledger)
            ae._append_ledger({"key": "POSITION_GENERATION:1:FLAT", "status": "POSITION_GENERATION",
                               "generation": 1, "open": False, "accountId": "ACC-R22"}, ledger)
            notes = ae.reconcile(
                {"scenarios": {"primary": {"symbol": "MNQU6"}}},
                cfg={"NQX_AUTOTRADE": "1", "NQX_AUTOTRADE_LIVE": "1"},
                broker_query=lambda _symbol: dict(open_position),
                broker_order_query=lambda _symbol, **_kwargs: terminal_orders(),
                state_query=lambda: {"managementClaim": stale_claim}, ledger_path=ledger,
                recover_management=lambda current, local, *_args: (
                    stale_calls.append(current["managementKey"]) or (False, {"reason": "never"})),
                runner=lambda *_args: (runner_calls.append(True) or (0, "unexpected")))
        self.assertEqual(stale_calls, [])
        self.assertNotIn("startup recovery failed", notes[0])
        self.assertFalse(runner_calls)

    def test_dual_identity_platform_and_status_conflicts_invalidate_view(self):
        base = {"success": True, "orders": [{"id": "1", "account": "ACC-R22",
            "instrument": "MNQU6", "orderType": "LIMIT", "orderState": "WORKING",
            "orderAction": "BUY", "quantity": 1, "limitPrice": 20000, "receipt": "R1"}]}
        for mutation in (
                {"accountId": "OTHER"}, {"symbol": "MESU6"}, {"contractId": "WRONG"}):
            payload = json.loads(json.dumps(base))
            payload["orders"][0].update(mutation)
            with self.subTest(mutation=mutation), self.assertRaises(broker_status.Unavailable):
                broker_status.normalize_crosstrade_orders(
                    payload, platform="NT8", account="ACC-R22", symbol="MNQU6",
                    contract_ids=["EXPECTED"])
        pending_nt8 = json.loads(json.dumps(base))
        pending_nt8["orders"][0]["orderState"] = "PENDING"
        with self.assertRaises(broker_status.Unavailable):
            broker_status.normalize_crosstrade_orders(
                pending_nt8, platform="NT8", account="ACC-R22", symbol="MNQU6")
        inconsistent = broker_status.CrossTradeAdapter({
            "CROSSTRADE_KEY": "stub", "CROSSTRADE_ACCOUNT": "ACC-R22",
            "CROSSTRADE_PLATFORM": "NT8",
            "CROSSTRADE_API_BASE": "https://stub/v1/api/tv"})
        self.assertFalse(inconsistent.configured)
        self.assertIn("inconsistent", inconsistent.configuration_error)
        adapter = broker_status.CrossTradeAdapter({
            "CROSSTRADE_KEY": "stub", "CROSSTRADE_ACCOUNT": "ACC-R22",
            "CROSSTRADE_ACCOUNT_ID": "6283", "CROSSTRADE_CONTRACT_ID": "9001",
            "CROSSTRADE_PLATFORM": "TRADOVATE",
            "CROSSTRADE_API_BASE": "https://stub/v1/api/tv"})
        for data in (
                {"accountId": 6283, "account": "OTHER", "contractId": 9001,
                 "netPos": 0, "position": {}},
                {"accountId": 6283, "symbol": "MNQU6", "instrument": "MESU6",
                 "netPos": 0, "position": {}},
                {"accountId": 6283, "symbol": "MNQU6", "contractId": "WRONG",
                 "netPos": 0, "position": {}}):
            with self.subTest(position=data), \
                 patch.object(broker_status, "_get_json",
                              return_value={"success": True, "data": data}), \
                 self.assertRaises(broker_status.Unavailable):
                adapter.query("MNQU6")

    def test_any_nonempty_route_diagnostics_is_unknown(self):
        rows = [
            {"accountId": "ACC-R22", "legId": "TP1", "state": "ACCEPTED",
             "orderId": "O1", "receipt": "R1"},
            {"accountId": "ACC-R22", "legId": "RUNNER", "state": "ACCEPTED",
             "orderId": "O2", "receipt": "R2"},
        ]
        envelope = route_envelope.format_envelope(rows, "SENT")
        self.assertTrue(route_envelope.parse(envelope, expected_accounts=["ACC-R22"])["ok"])
        self.assertFalse(route_envelope.parse(
            envelope, expected_accounts=["ACC-R22"], diagnostics="warning only")["ok"])


if __name__ == "__main__":
    unittest.main()

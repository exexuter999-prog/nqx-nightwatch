# -*- coding: utf-8 -*-
"""R52: 注文行に数量・種別・価格が無く建玉行に receipt が無いブローカーでの MODIFY 経路。

2026-09-05 02:36、ULTRA SHORT 12 が TP1 で 6 決済 → runner 6 になった直後、
(A) 所有権の継続が証明できず保留、(B) order.py の防護が qty>=2 で拒否、
(C)(D) 張り替え行の束縛と照合が価格を要求、の 3 段で建値移動が原理的に不可能だった。
ユーザー決定: 価格の裏取りなしで、構造(行数・方向・OCO リンク・ID)で成功とみなす。

    python tests/test_r52_modify_incomplete.py
"""
import copy
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import autotrade_engine as ae  # noqa: E402
import broker_status  # noqa: E402
import ownership_binder  # noqa: E402
import route_identity  # noqa: E402
from _hermetic import make_sandbox  # noqa: E402

ACC = "LFE-TEST"
SYM = "MNQU6"


def plan(tp1_qty=6, runner_qty=6, side="SELL", entry=29556.75):
    return {"scenarioId": "S", "entryKey": "ENTRY:" + "a" * 64, "symbol": SYM, "side": side,
            "qty": tp1_qty + runner_qty, "entry": entry, "stop": 29599.5,
            "tp1": 29487.0, "finalTarget": 29350.75, "targets": [29487.0, 29350.75],
            "entryOrderType": "LIMIT", "accountScope": [ACC], "planVersion": "R19-ICT-SPLIT-1",
            "legs": [{"id": "TP1", "qty": tp1_qty, "target": 29487.0},
                     {"id": "RUNNER", "qty": runner_qty, "target": 29350.75}], "ultra": True}


ROUTE = [{"accountId": ACC, "legId": "TP1", "state": "ACCEPTED", "orderId": "E-150",
          "receipt": f"TRADOVATE:{ACC}:E-150"},
         {"accountId": ACC, "legId": "RUNNER", "state": "ACCEPTED", "orderId": "E-157",
          "receipt": f"TRADOVATE:{ACC}:E-157"}]


def trow(order_id, status, action, oco=None, parent=None):
    """CrossTrade/Tradovate の実データ形: 数量・種別・価格が無い。

    R80: リンクは生の項目で持つ。``oco`` = OCO の相方(相互 ``ocoId``)、``parent`` = OSO の
    真の親(``parentId``)。互換の ``parentId``(潰したリンク)は ENTRY 側の消費者のために
    どちらからも埋める。
    """
    return {"orderId": order_id, "accountId": ACC, "symbol": SYM, "status": status, "action": action,
            "qty": None, "orderType": None, "limitPrice": None, "stopPrice": None, "filledPrice": None,
            "fieldsComplete": False, "parentId": oco or parent, "brokerOcoId": oco, "brokerParentId": parent,
            "receipt": f"TRADOVATE:{ACC}:{order_id}", "receiptSource": "derived"}


def view(*rows):
    rows = list(rows)
    return {"verified": True, "platform": "TRADOVATE", "symbol": SYM, "orders": rows,
            "activeOrders": [r for r in rows if r["status"] in broker_status.BROKER_ACTIVE_STATES]}


def position(qty, side="SHORT", avg=29556.75, **changes):
    row = {"verified": True, "accountId": ACC, "account": ACC, "symbol": SYM, "side": side, "qty": qty,
           "avgEntry": avg, "filledAt": "2026-09-04T17:05:12.243Z", "orderId": 649589730081,
           "receipt": None, "initialQty": qty}
    row.update(changes)
    return row


def bind(pos, order_view, pl=None):
    raw = broker_status.position_identity(pos)
    return ownership_binder.bind(pl or plan(), ROUTE, pos, order_view, route_state="SENT",
                                 position_generation=f"PG:1:{raw}" if raw else None)


class ContinuedRunnerOwnershipTests(unittest.TestCase):
    def test_runner_after_tp1_is_owned_without_receipt(self):
        entries = view(trow("E-150", "FILLED", "SELL"), trow("E-157", "FILLED", "SELL"),
                       trow("B-158", "WORKING", "BUY", oco="B-159"), trow("B-159", "WORKING", "BUY", oco="B-158"))
        result = bind(position(6), entries)
        self.assertTrue(result["owned"], result)
        self.assertIn(result["state"], {"PARTIAL_FILL", "LIMITED_PARTIAL"})
        self.assertEqual(result["ownedPlan"]["partialLegId"], "RUNNER")
        self.assertEqual(result["ownedPlan"]["finalTarget"], 29350.75)
        self.assertEqual([leg["id"] for leg in result["ownedPlan"]["legs"]], ["RUNNER"])

    def test_wrong_qty_or_price_is_not_ours(self):
        entries = view(trow("E-150", "FILLED", "SELL"), trow("E-157", "FILLED", "SELL"))
        self.assertFalse(bind(position(5), entries)["owned"], "runner 枚数と合わない")
        self.assertFalse(bind(position(6, avg=29550.0), entries)["owned"], "SELL 指値より不利な平均建値")

    def test_uneven_split_picks_the_runner_leg(self):
        pl = plan(tp1_qty=4, runner_qty=5)
        entries = view(trow("E-150", "FILLED", "SELL"), trow("E-157", "FILLED", "SELL"))
        result = bind(position(5), entries, pl)
        self.assertTrue(result["owned"], result)
        self.assertEqual(result["ownedPlan"]["partialLegId"], "RUNNER")
        self.assertFalse(bind(position(4), entries, pl)["owned"], "TP1 脚の枚数では継続と認めない")

    def test_full_position_still_owned_full(self):
        entries = view(trow("E-150", "FILLED", "SELL"), trow("E-157", "FILLED", "SELL"))
        result = bind(position(12), entries)
        self.assertTrue(result["owned"], result)
        self.assertEqual(result["state"], "OWNED_FULL")


class ReplacementBracketTests(unittest.TestCase):
    def test_new_mutually_linked_pair_binds(self):
        before = view(trow("B-158", "WORKING", "BUY", oco="B-159"), trow("B-159", "WORKING", "BUY", oco="B-158"))
        after = view(trow("N-201", "WORKING", "BUY", oco="N-202"), trow("N-202", "WORKING", "BUY", oco="N-201"))
        bound = route_identity.bind_replacement_bracket(before, after, account=ACC, symbol=SYM, action="BUY",
                                                        qty=6, stop=29556.75, target=29350.75, platform="TRADOVATE")
        self.assertIsNotNone(bound)
        self.assertEqual({bound["stopOrderId"], bound["targetOrderId"]}, {"N-201", "N-202"})
        self.assertTrue(bound["fieldsIncomplete"])
        self.assertNotEqual(bound["stopReceipt"], bound["targetReceipt"])

    def test_unlinked_or_extra_rows_do_not_bind(self):
        before = view()
        unlinked = view(trow("N-201", "WORKING", "BUY"), trow("N-202", "WORKING", "BUY"))
        self.assertIsNone(route_identity.bind_replacement_bracket(before, unlinked, account=ACC, symbol=SYM,
                                                                  action="BUY", qty=6, stop=1, target=2))
        three = view(trow("N-201", "WORKING", "BUY", oco="N-202"), trow("N-202", "WORKING", "BUY", oco="N-201"),
                     trow("N-203", "WORKING", "BUY"))
        self.assertIsNone(route_identity.bind_replacement_bracket(before, three, account=ACC, symbol=SYM,
                                                                  action="BUY", qty=6, stop=1, target=2))
        wrong_side = view(trow("N-201", "WORKING", "SELL", oco="N-202"), trow("N-202", "WORKING", "SELL", oco="N-201"))
        self.assertIsNone(route_identity.bind_replacement_bracket(before, wrong_side, account=ACC, symbol=SYM,
                                                                  action="BUY", qty=6, stop=1, target=2))

    def test_old_pair_surviving_is_not_a_replacement(self):
        old = view(trow("B-158", "WORKING", "BUY", oco="B-159"), trow("B-159", "WORKING", "BUY", oco="B-158"))
        self.assertIsNone(route_identity.bind_replacement_bracket(old, old, account=ACC, symbol=SYM,
                                                                  action="BUY", qty=6, stop=1, target=2))


class StructuralProtectiveVerificationTests(unittest.TestCase):
    def _verify(self, rows, receipt, **overrides):
        pos = position(6)
        gen = broker_status.position_identity(pos)
        kwargs = dict(side="SELL", position_generation=gen, position_before=pos, current_position=pos,
                      route_receipt={"accounts": [receipt]})
        kwargs.update(overrides)
        return broker_status.verify_protective_orders(view(*rows), [ACC], 6, 29556.75, 29350.75, **kwargs)

    def _receipt(self, a="N-201", b="N-202"):
        return {"accountId": ACC, "stopOrderId": a, "targetOrderId": b, "ocoGroupId": f"{a}+{b}",
                "stopReceipt": f"TRADOVATE:{ACC}:{a}", "targetReceipt": f"TRADOVATE:{ACC}:{b}"}

    def test_linked_pair_matching_receipt_verifies(self):
        ok, detail = self._verify([trow("N-201", "WORKING", "BUY", oco="N-202"),
                                   trow("N-202", "WORKING", "BUY", oco="N-201")], self._receipt())
        self.assertTrue(ok, detail)
        self.assertIn("structurally", detail)

    def test_mismatches_fail_closed(self):
        pair = [trow("N-201", "WORKING", "BUY", oco="N-202"), trow("N-202", "WORKING", "BUY", oco="N-201")]
        self.assertFalse(self._verify(pair, self._receipt("N-201", "N-999"))[0], "ID が受領と違う")
        self.assertFalse(self._verify([trow("N-201", "WORKING", "BUY"), trow("N-202", "WORKING", "BUY")],
                                      self._receipt())[0], "OCO リンク無し")
        self.assertFalse(self._verify([pair[0], trow("N-202", "WORKING", "SELL", oco="N-201")],
                                      self._receipt())[0], "方向違い")
        self.assertFalse(self._verify(pair + [trow("B-158", "WORKING", "BUY", oco="B-159")],
                                      self._receipt())[0], "古い行が残っている(3 本)")
        self.assertFalse(self._verify(pair, self._receipt(), current_position=position(5))[0], "建玉が変わった")

    def test_complete_rows_keep_price_verification(self):
        # 価格を返すブローカーは従来どおり価格で照合する(構造モードには入らない)
        rows = [{**trow("N-201", "WORKING", "BUY", oco="N-202"), "fieldsComplete": True, "qty": 6,
                 "orderType": "STOP", "stopPrice": 29556.75},
                {**trow("N-202", "WORKING", "BUY", oco="N-201"), "fieldsComplete": True, "qty": 6,
                 "orderType": "LIMIT", "limitPrice": 29350.75}]
        ok, detail = self._verify(rows, {**self._receipt(), "ocoGroupId": "N-202"})
        self.assertFalse(ok)   # 完全行では相互リンクではなく同一 parent==ocoGroupId を要求(従来仕様)


class ManagementIntentQtyTests(unittest.TestCase):
    def test_ultra_runner_qty_is_a_valid_management_intent(self):
        import management_intent
        import execution_contract
        built = management_intent.build(account_id=ACC, symbol=SYM, position_generation="PG:1:POS:" + "a" * 64,
                                        side="SELL", qty=6, stop=29556.75, target=29350.75)
        self.assertEqual(built["qty"], 6)
        self.assertTrue(management_intent.intent_hash(built).startswith("mi_"))
        ceiling = int(execution_contract.CONTRACT["ultra"]["maxQtyPerAccount"])
        management_intent.build(account_id=ACC, symbol=SYM, position_generation="PG:1:POS:" + "a" * 64,
                                side="SELL", qty=ceiling, stop=29556.75, target=29350.75)
        with self.assertRaises(ValueError):
            management_intent.build(account_id=ACC, symbol=SYM, position_generation="PG:1:POS:" + "a" * 64,
                                    side="SELL", qty=ceiling + 1, stop=29556.75, target=29350.75)


class PositionSyncTests(unittest.TestCase):
    """monitor_publish は reconcile の前に Worker の建玉 stream を同期する。"""

    def test_sync_runs_only_when_state_published(self):
        import monitor_publish
        import nqx_state
        calls = []
        original = nqx_state.sync_position
        nqx_state.sync_position = lambda cfg=None, symbol=None: (calls.append(1) or (True, {"ok": True}))
        try:
            self.assertEqual(monitor_publish._sync_position_to_worker(True), [])
            self.assertEqual(len(calls), 1)
            self.assertEqual(monitor_publish._sync_position_to_worker(False), [])
            self.assertEqual(len(calls), 1, "正本が publish できていない周期は触らない")
            nqx_state.sync_position = lambda cfg=None, symbol=None: (False, {"reason": "boom"})
            notes = monitor_publish._sync_position_to_worker(True)
            self.assertTrue(notes and "position sync failed" in notes[0])

            def raise_it(cfg=None, symbol=None):
                raise RuntimeError("down")
            nqx_state.sync_position = raise_it
            notes = monitor_publish._sync_position_to_worker(True)
            self.assertTrue(notes and "RuntimeError" in notes[0])
        finally:
            nqx_state.sync_position = original

    def test_reconcile_is_preceded_by_position_sync_in_source(self):
        src = open(os.path.join(BASE, "monitor_publish.py"), encoding="utf-8").read()
        self.assertLess(src.index("position_sync_notes = _sync_position_to_worker(state_ok)"),
                        src.index("auto_notes = autotrade_engine.reconcile(bundle, state_ok=state_ok)"))


class LateReplacementBindTests(unittest.TestCase):
    """cancelandbracket の新しい対が遅れて現れても、送信前スナップショットに対して束縛する。"""

    def _run(self, sequence, origin, attempts=6):
        import order
        calls, slept = [], []
        original = order._orders_snapshot
        last = [sequence[-1] if sequence else None]
        order._orders_snapshot = lambda symbol, account=None: (calls.append(account) or (sequence.pop(0) if sequence else last[0]))
        try:
            return order._late_bind_replacement("MNQU6", ACC, origin, action="BUY", qty=6, stop=29500.25,
                                                target=29350.75, attempts=attempts, sleep=lambda s: slept.append(s)), calls, slept
        finally:
            order._orders_snapshot = original

    def test_pair_appearing_after_several_polls_is_bound(self):
        origin = view(trow("B-158", "WORKING", "BUY", oco="B-159"), trow("B-159", "WORKING", "BUY", oco="B-158"))
        gone = view()                                                     # 取消済み・新規はまだ
        new = view(trow("N-208", "WORKING", "BUY", oco="N-209"), trow("N-209", "WORKING", "BUY", oco="N-208"))
        bound, calls, slept = self._run([gone, gone, gone, new], origin)
        self.assertIsNotNone(bound)
        self.assertEqual({bound["stopOrderId"], bound["targetOrderId"]}, {"N-208", "N-209"})
        self.assertEqual(len(calls), 4)
        self.assertEqual(len(slept), 3)

    def test_gives_up_without_new_rows(self):
        origin = view(trow("B-158", "WORKING", "BUY", oco="B-159"), trow("B-159", "WORKING", "BUY", oco="B-158"))
        bound, calls, _ = self._run([view(), view(), view()], origin, attempts=3)
        self.assertIsNone(bound)
        self.assertEqual(len(calls), 3)

    def test_no_origin_means_no_late_bind(self):
        bound, calls, _ = self._run([view()], None)
        self.assertIsNone(bound)
        self.assertEqual(calls, [])


class EngineModifyArgsTests(unittest.TestCase):
    def test_ultra_plan_passes_ultra_flag(self):
        action = {"action": "MODIFY", "qty": 6, "sl": 29556.75, "tp": 29350.75, "reason": "breakeven"}
        claim = {"managementKey": "MANAGEMENT:" + "c" * 64, "claimToken": "T" * 43,
                 "managementIntentHash": "mi_" + "d" * 64}
        args = ae._command_for_modify(plan(), action, "PG:1:POS:" + "e" * 64, claim, account=ACC)
        self.assertIn("--ultra", args)
        normal = ae._command_for_modify({**plan(), "ultra": False}, action, "PG:1:POS:" + "e" * 64, claim, account=ACC)
        self.assertNotIn("--ultra", normal)


STUB_BROKER = '''# -*- coding: utf-8 -*-
import os, json
# R80: route_identity は import 時に broker_status の状態集合を参照する。スタブも同じ表面を持つ。
BROKER_ACTIVE_STATES = {"WORKING", "ACCEPTED", "SUBMITTED", "PENDING", "PENDING_SUBMIT",
                        "SUSPENDED", "PARTIALLY_FILLED", "PARTIAL_FILL"}
BROKER_TERMINAL_STATES = {"FILLED", "CANCELED", "REJECTED", "EXPIRED"}
BROKER_LIVE_PROTECTIVE_STATES = {"WORKING", "ACCEPTED", "SUBMITTED", "PARTIALLY_FILLED", "PARTIAL_FILL"}
def _rows():
    n = int(os.environ.get("STUB_PROTECTIVE_ROWS", "2"))
    rows = []
    for i in range(n):
        rows.append({"orderId": f"B-{i}", "accountId": "TEST", "symbol": "MNQU6", "status": "WORKING",
                     "action": "BUY", "qty": None, "orderType": None, "limitPrice": None,
                     "fieldsComplete": False, "parentId": None, "receipt": f"R-{i}"})
    return rows
def query_position(symbol="MNQU6", account=None):
    return {"verified": True, "qty": 6, "side": "SHORT", "symbol": symbol,
            "accountId": "TEST", "avgEntry": 29556.75}
def position_identity(position):
    return "gen-1"
def query_orders(symbol="MNQU6", known_order_ids=None, account=None):
    rows = _rows()
    return {"verified": True, "orders": rows, "activeOrders": rows}
def derived_receipt(*args, **kwargs):
    return None
'''


class OrderModifyUltraGuardTests(unittest.TestCase):
    def _run(self, protective_rows, qty="6"):
        sandbox = make_sandbox(BASE)
        try:
            with open(os.path.join(sandbox, "broker_status.py"), "w", encoding="utf-8") as fh:
                fh.write(STUB_BROKER)
            with open(os.path.join(sandbox, ".secrets", "crosstrade.env"), "w", encoding="utf-8") as fh:
                fh.write("CROSSTRADE_URL=http://127.0.0.1:9/blocked\nCROSSTRADE_KEY=TEST\n"
                         "CROSSTRADE_DEST=TEST\nCROSSTRADE_ACCOUNTS=TEST\nMAX_RISK_DOLLARS=200\n")
            env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1",
                       STUB_PROTECTIVE_ROWS=str(protective_rows))
            args = ["--modify", "--side", "sell", "--qty", qty, "--sl", "29556.75", "--tp", "29350.75",
                    "--ultra", "--position-generation", "gen-1", "--account", "TEST",
                    "--management-key=k", "--management-token=t", "--management-intent-hash=h", "--confirm"]
            proc = subprocess.run([sys.executable, os.path.join(sandbox, "order.py")] + args,
                                  capture_output=True, text=True, encoding="utf-8", errors="replace",
                                  cwd=sandbox, env=env, timeout=60)
            return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
        finally:
            shutil.rmtree(sandbox, ignore_errors=True)

    def test_two_bracket_pairs_alive_is_refused(self):
        code, out = self._run(protective_rows=4)
        self.assertNotEqual(code, 0)
        self.assertIn("MODIFY_SPLIT_PLAN_PROTECTED", out)
        self.assertNotIn("NQX_ROUTE_", out)

    def test_single_pair_passes_the_split_guard(self):
        code, out = self._run(protective_rows=2)
        self.assertNotIn("MODIFY_SPLIT_PLAN_PROTECTED", out)
        # 防護は通り、その先の claim 整合(テスト用のダミー key)で止まる = 外部送信なし
        self.assertNotEqual(code, 0)
        self.assertIn("MANAGEMENT_CLAIM_INTENT_MISMATCH", out)
        self.assertNotIn("NQX_ROUTE_", out)

    def test_qty_mismatch_is_refused(self):
        code, out = self._run(protective_rows=2, qty="5")
        self.assertNotEqual(code, 0)
        self.assertIn("MODIFY_POSITION_MISMATCH", out)


if __name__ == "__main__":
    unittest.main(verbosity=1)

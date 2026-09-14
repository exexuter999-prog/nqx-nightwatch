#!/usr/bin/env python3
"""R23 broker-truth route identity acquisition and machine-envelope hygiene.

Two production defects are pinned here.

1. CrossTrade answers a webhook with a bare ``{"success": true}``.  R19..R22
   require ``orderId`` + ``receipt`` before a leg is ACCEPTED, so every live
   route resolved UNKNOWN, the Durable Object entry claim locked permanently,
   and ownership could never bind.  Identity now comes from the broker order
   view instead of the response echo.
2. ``order.py`` printed ``ROUTE PARTIAL/FAILED`` on the same stdout that
   carries ``NQX_ROUTE_SNAPSHOT``/``NQX_ROUTE_FINAL``.  ``route_envelope``
   treats ``FAILED`` as a poisoned envelope, so the PARTIAL freeze path was
   unreachable and every partial route became UNKNOWN.
"""
from __future__ import annotations

import json
import os
import sys
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import broker_status                                    # noqa: E402
import nqx_state                                        # noqa: E402
import order                                            # noqa: E402
import route_envelope                                   # noqa: E402
import route_identity                                   # noqa: E402

ACCOUNT = "APEX4568750000009"
SYMBOL = "MNQU6"
BARE_SUCCESS = json.dumps({"success": True})


def order_row(order_id, **changes):
    row = {"id": order_id, "account": ACCOUNT, "instrument": SYMBOL,
           "orderType": "LIMIT", "orderState": "WORKING", "orderAction": "BUY",
           "quantity": 1, "limitPrice": 20000.0}
    row.update(changes)
    return row


def view(*rows):
    return broker_status.normalize_crosstrade_orders(
        {"success": True, "orders": list(rows)},
        platform="NT8", account=ACCOUNT, symbol=SYMBOL)


class DerivedReceiptTests(unittest.TestCase):
    def test_crosstrade_rows_get_a_unique_reproducible_receipt(self):
        first = view(order_row("629991140004"), order_row("629991140007"))
        second = view(order_row("629991140004"), order_row("629991140007"))
        receipts = [row["receipt"] for row in first["orders"]]
        self.assertEqual(receipts, [row["receipt"] for row in second["orders"]],
                         "re-query must reproduce the same frozen identity")
        self.assertEqual(len(set(receipts)), 2, "per-row receipts must not collide")
        self.assertTrue(all(row["receiptSource"] == "derived" for row in first["orders"]))
        self.assertEqual(first["orders"][0]["receipt"],
                         f"NT8:{ACCOUNT}:629991140004")

    def test_a_broker_issued_receipt_is_never_overwritten(self):
        issued = view(order_row("629991140004", receipt="RCPT-REAL"))
        self.assertEqual(issued["orders"][0]["receipt"], "RCPT-REAL")
        self.assertEqual(issued["orders"][0]["receiptSource"], "broker")


class EntryIdentityAcquisitionTests(unittest.TestCase):
    def test_bare_success_body_is_unknown_without_broker_truth(self):
        self.assertEqual(order.route_attempt_state(200, BARE_SUCCESS), "UNKNOWN")

    def test_bare_success_body_is_accepted_once_the_broker_row_is_bound(self):
        before = view()
        after = view(order_row("629991140004"))
        identity = route_identity.bind_entry_leg(
            before, after, account=ACCOUNT, symbol=SYMBOL, action="BUY", qty=1,
            order_type="LIMIT", entry_price=20000.0)
        self.assertEqual(identity["orderId"], "629991140004")
        self.assertEqual(identity["receipt"], f"NT8:{ACCOUNT}:629991140004")
        self.assertEqual(order.route_attempt_state(200, BARE_SUCCESS, identity), "ACCEPTED")

    def test_an_order_that_exists_outranks_a_rejected_or_lost_response(self):
        # An orphan is the dangerous outcome: if the broker created the order we
        # must own it even when the HTTP result said otherwise.
        identity = {"orderId": "629991140004", "receipt": "R"}
        self.assertEqual(order.route_attempt_state(500, "ERROR", identity), "ACCEPTED")
        self.assertEqual(order.route_attempt_state(0, "", identity), "ACCEPTED")
        self.assertEqual(order.route_attempt_state(500, "ERROR"), "REJECTED")
        self.assertEqual(order.route_attempt_state(0, ""), "UNKNOWN")

    def test_split_legs_are_separated_only_by_their_own_window(self):
        # TP1 and RUNNER are the same account/side/qty/type/entry price.  A
        # single window holding both parents is ambiguous and must fail closed.
        empty = view()
        both = view(order_row("PARENT-TP1"), order_row("PARENT-RUNNER"))
        self.assertIsNone(route_identity.bind_entry_leg(
            empty, both, account=ACCOUNT, symbol=SYMBOL, action="BUY", qty=1,
            order_type="LIMIT", entry_price=20000.0))
        first = view(order_row("PARENT-TP1"))
        tp1 = route_identity.bind_entry_leg(
            empty, first, account=ACCOUNT, symbol=SYMBOL, action="BUY", qty=1,
            order_type="LIMIT", entry_price=20000.0)
        runner = route_identity.bind_entry_leg(
            first, both, account=ACCOUNT, symbol=SYMBOL, action="BUY", qty=1,
            order_type="LIMIT", entry_price=20000.0)
        self.assertEqual(tp1["orderId"], "PARENT-TP1")
        self.assertEqual(runner["orderId"], "PARENT-RUNNER")
        self.assertNotEqual(tp1["receipt"], runner["receipt"])

    def test_protective_children_are_never_mistaken_for_the_entry_parent(self):
        before = view()
        after = view(
            order_row("PARENT"),
            order_row("SL", orderType="STOP_MARKET", orderAction="SELL",
                      stopPrice=19980.0, limitPrice=None, ocoId="OCO-1"),
            order_row("TP", orderAction="SELL", limitPrice=20030.0, ocoId="OCO-1"))
        identity = route_identity.bind_entry_leg(
            before, after, account=ACCOUNT, symbol=SYMBOL, action="BUY", qty=1,
            order_type="LIMIT", entry_price=20000.0)
        self.assertEqual(identity["orderId"], "PARENT")

    def test_every_ambiguity_fails_closed(self):
        empty, one = view(), view(order_row("PARENT"))
        cases = {
            "no new row": (one, one, {}),
            "wrong entry price": (empty, view(order_row("P", limitPrice=20001.0)), {}),
            "wrong side": (empty, view(order_row("P", orderAction="SELL")), {}),
            "wrong qty": (empty, view(order_row("P", quantity=2)), {"qty": 2}),
            "unverified before": ({"verified": False, "orders": []}, one, {}),
            "unverified after": (empty, {"verified": False, "orders": []}, {}),
        }
        for label, (before, after, override) in cases.items():
            with self.subTest(case=label):
                kwargs = {"account": ACCOUNT, "symbol": SYMBOL, "action": "BUY",
                          "qty": 1, "order_type": "LIMIT", "entry_price": 20000.0}
                # qty=2 case proves the *frozen* qty must match, not the row's.
                if override:
                    kwargs["qty"] = 1
                self.assertIsNone(route_identity.bind_entry_leg(before, after, **kwargs))

    def test_an_unknown_status_in_the_window_poisons_the_binding(self):
        # normalize_crosstrade_orders already refuses a non-allowlisted status,
        # so route_identity's own guard is exercised on a hand-built view.
        parent = view(order_row("PARENT"))["orders"][0]
        rogue = {**parent, "orderId": "X", "status": "SUSPENDED_WEIRD"}
        after = {"verified": True, "platform": "NT8", "orders": [parent, rogue]}
        self.assertIsNone(route_identity.bind_entry_leg(
            {"verified": True, "orders": []}, after, account=ACCOUNT, symbol=SYMBOL,
            action="BUY", qty=1, order_type="LIMIT", entry_price=20000.0))


class ReplacementBracketAcquisitionTests(unittest.TestCase):
    def bracket(self, **changes):
        stop = order_row("SL-NEW", orderType="STOP_MARKET", orderAction="SELL",
                         stopPrice=19980.0, limitPrice=None, ocoId="OCO-NEW")
        target = order_row("TP-NEW", orderAction="SELL", limitPrice=20030.0,
                           ocoId="OCO-NEW")
        stop.update(changes.get("stop") or {})
        target.update(changes.get("target") or {})
        return stop, target

    def bind(self, before, after):
        return route_identity.bind_replacement_bracket(
            before, after, account=ACCOUNT, symbol=SYMBOL, action="SELL",
            qty=1, stop=19980.0, target=20030.0, platform="NT8")

    def test_new_pair_binds_with_per_order_receipts(self):
        stop, target = self.bracket()
        acquired = self.bind(view(), view(stop, target))
        self.assertEqual(acquired["stopOrderId"], "SL-NEW")
        self.assertEqual(acquired["targetOrderId"], "TP-NEW")
        self.assertEqual(acquired["ocoGroupId"], "OCO-NEW")
        self.assertNotEqual(acquired["stopReceipt"], acquired["targetReceipt"])
        self.assertEqual(acquired["receiptSource"], "broker-orders")

    def test_a_surviving_old_bracket_is_not_a_replacement(self):
        stop, target = self.bracket()
        unchanged = view(stop, target)
        self.assertIsNone(self.bind(unchanged, unchanged))

    def test_a_pair_without_one_shared_oco_parent_is_rejected(self):
        stop, target = self.bracket(target={"ocoId": "OCO-OTHER"})
        self.assertIsNone(self.bind(view(), view(stop, target)))
        stop, target = self.bracket(stop={"ocoId": None}, target={"ocoId": None})
        self.assertIsNone(self.bind(view(), view(stop, target)))

    def test_acquired_bracket_passes_the_protective_verification(self):
        stop, target = self.bracket()
        after = view(stop, target)
        acquired = self.bind(view(), after)
        position = {"verified": True, "accountId": ACCOUNT, "symbol": SYMBOL,
                    "side": "LONG", "qty": 1, "orderId": "ENTRY",
                    "receipt": "ENTRY-R", "filledAt": "2026-08-23T12:00:00Z"}
        generation = broker_status.position_identity(position)
        ok, detail = broker_status.verify_protective_orders(
            after, [ACCOUNT], 1, 19980.0, 20030.0, side="BUY",
            position_generation=generation, position_before=position,
            current_position=position, route_receipt={"accounts": [acquired]})
        self.assertTrue(ok, detail)
        swapped = {**acquired, "stopReceipt": acquired["targetReceipt"]}
        ok, _ = broker_status.verify_protective_orders(
            after, [ACCOUNT], 1, 19980.0, 20030.0, side="BUY",
            position_generation=generation, position_before=position,
            current_position=position, route_receipt={"accounts": [swapped]})
        self.assertFalse(ok, "a per-order receipt swap must not verify")

    def test_recovery_expects_each_replacement_receipt_on_its_own_order(self):
        stop, target = self.bracket(stop={"orderState": "CANCELED"},
                                    target={"orderState": "CANCELED"})
        after = view(stop, target)
        acquired = self.bind(view(), view(*self.bracket()))
        flat = {"verified": True, "qty": 0, "side": "FLAT", "accountId": ACCOUNT,
                "symbol": SYMBOL, "observedAt": "2026-08-23T12:00:00Z"}
        after = {**after, "observedAt": "2026-08-23T12:00:00Z", "platform": "NT8"}
        claim = {"managementKey": "MANAGEMENT:R23",
                 "managementIntentHash": "mi_" + "a" * 64,
                 "managementIntent": {"symbol": SYMBOL, "accountId": ACCOUNT,
                                      "qty": 0, "positionGeneration": ""},
                 "routeReceipt": {"accounts": [acquired]}}
        calls = []
        ok, detail = nqx_state.recover_management_from_broker(
            "MANAGEMENT:R23", "T" * 43, claim,
            position_query=lambda _symbol: flat,
            orders_query=lambda _symbol, **_kw: after,
            publish_fn=lambda stream, payload, _cfg: (
                calls.append((stream, payload)) or (True, {})))
        # qty/generation of this fixture intentionally do not match, so the
        # proof must fail — what matters is that it failed on position truth
        # and not on a receipt lookup that no longer exists.
        self.assertFalse(ok)
        self.assertEqual(detail["reason"], "MANAGEMENT_CURRENT_BROKER_PROOF_INVALID")


class MachineEnvelopeHygieneTests(unittest.TestCase):
    def make_stdout(self, results, split=True, identities=None):
        route = order.classify_route_results(results, split=split, identities=identities)
        snapshot = order.build_route_snapshot(results, split=split, identities=identities)
        printed = []
        original = order.print if hasattr(order, "print") else print

        # Reproduce exactly what post_split_to_accounts writes before the
        # envelope when a leg is not accepted.
        failed = [(account, leg, status) for account, leg, status, body in results
                  if order.route_attempt_state(
                      status, body, (identities or {}).get((account, leg))) != "ACCEPTED"]
        if failed:
            printed.append(f"ROUTE NOT_ALL_ACCEPTED: {failed}")
        printed.append("NQX_ROUTE_SNAPSHOT " + json.dumps(
            snapshot, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
        printed.append(("NQX_ROUTE_FINAL STATE={state} ACCEPTED={acceptedCount} "
                        "REJECTED={explicitRejectCount} UNKNOWN={unknownCount} "
                        "TOTAL={totalAttempts} RESOLVED=1").format(**route))
        del original
        return "\n".join(printed)

    def test_partial_route_stdout_still_parses(self):
        results = [(ACCOUNT, "TP1", 200, json.dumps({"data": {
                        "orderId": "O1", "receipt": "R1"}})),
                   (ACCOUNT, "RUNNER", 200, "ERROR rejected")]
        parsed = route_envelope.parse(self.make_stdout(results),
                                      expected_accounts=[ACCOUNT])
        self.assertTrue(parsed["ok"], parsed)
        self.assertEqual(parsed["state"], "PARTIAL")
        self.assertEqual(parsed["acceptedCount"], 1)

    def test_no_stdout_line_of_the_producer_can_poison_the_envelope(self):
        with open(os.path.join(BASE, "order.py"), encoding="utf-8") as fh:
            source = fh.read()
        marker = route_envelope.re.compile(
            r"(?:^|)(ERROR|FATAL|EXCEPTION|FAILED|INTERRUPTED|TRACEBACK)(?:|:)",
            flags=route_envelope.re.IGNORECASE)
        # Every stdout print of the producer shares the envelope that
        # route_envelope.parse scans for poisoning markers.
        offenders = [line.strip() for line in source.splitlines()
                     if line.lstrip().startswith("print(") and marker.search(line)]
        self.assertEqual(offenders, [], offenders)
        self.assertIn("ROUTE NOT_ALL_ACCEPTED", source)


    def test_a_bare_success_split_route_reaches_sent_end_to_end(self):
        identities = {
            (ACCOUNT, "TP1"): {"orderId": "629991140004",
                               "receipt": f"NT8:{ACCOUNT}:629991140004"},
            (ACCOUNT, "RUNNER"): {"orderId": "629991140007",
                                  "receipt": f"NT8:{ACCOUNT}:629991140007"},
        }
        results = [(ACCOUNT, "TP1", 200, BARE_SUCCESS),
                   (ACCOUNT, "RUNNER", 200, BARE_SUCCESS)]
        parsed = route_envelope.parse(
            self.make_stdout(results, identities=identities),
            expected_accounts=[ACCOUNT])
        self.assertTrue(parsed["ok"], parsed)
        self.assertEqual(parsed["state"], "SENT")
        self.assertEqual(sorted(row["orderId"] for row in parsed["snapshot"]),
                         ["629991140004", "629991140007"])
        # Without acquisition the identical route is a permanent UNKNOWN lock.
        blind = route_envelope.parse(self.make_stdout(results),
                                     expected_accounts=[ACCOUNT])
        self.assertTrue(blind["ok"], blind)
        self.assertEqual(blind["state"], "UNKNOWN")
        self.assertEqual(blind["acceptedCount"], 0)



def tradovate_row(order_id, action="SELL", status="WORKING", parent=None, account=ACCOUNT):
    """CrossTrade/Tradovate の実データ形(2026-09-04 23:58 実測): 数量・種別・価格が無い。"""
    return {"orderId": order_id, "accountId": account, "brokerAccountId": "64739295",
            "symbol": SYMBOL, "contractId": "4399654", "status": status,
            "orderType": None, "action": action, "qty": None, "fieldsComplete": False,
            "limitPrice": None, "filledPrice": None, "stopPrice": None, "filled": None,
            "parentId": parent,
            "receipt": f"TRADOVATE:{account}:{order_id}", "receiptSource": "derived",
            "timestamp": "2026-09-04T14:57:27.011Z"}


def tradovate_view(*rows):
    return {"verified": True, "source": "crosstrade-tradovate", "platform": "TRADOVATE",
            "symbol": SYMBOL, "orders": list(rows)}


class R52IncompleteRowBindingTests(unittest.TestCase):
    """数量・種別・価格を返さないブローカー行でも、身元+親行+窓の一意性で束縛する。"""

    def test_incomplete_parent_binds_by_window_and_parent_row(self):
        empty = tradovate_view()
        after = tradovate_view(
            tradovate_row("649589730004"),                                  # 親(エントリー)
            tradovate_row("649589730005", action="BUY", status="SUSPENDED", parent=649589730006),
            tradovate_row("649589730006", action="BUY", status="SUSPENDED", parent=649589730005))
        bound = route_identity.bind_entry_leg(
            empty, after, account=ACCOUNT, symbol=SYMBOL, action="SELL", qty=3,
            order_type="LIMIT", entry_price=29585.0)
        self.assertIsNotNone(bound)
        self.assertEqual(bound["orderId"], "649589730004")
        self.assertEqual(bound["receipt"], f"TRADOVATE:{ACCOUNT}:649589730004")
        self.assertEqual(bound["status"], "WORKING")

    def test_second_leg_binds_only_in_its_own_window(self):
        empty = tradovate_view()
        first = tradovate_view(
            tradovate_row("649589730004"),
            tradovate_row("649589730005", action="BUY", status="SUSPENDED", parent=649589730006),
            tradovate_row("649589730006", action="BUY", status="SUSPENDED", parent=649589730005))
        both = tradovate_view(*first["orders"],
            tradovate_row("649589730011"),
            tradovate_row("649589730012", action="BUY", status="SUSPENDED", parent=649589730013),
            tradovate_row("649589730013", action="BUY", status="SUSPENDED", parent=649589730012))
        tp1 = route_identity.bind_entry_leg(empty, first, account=ACCOUNT, symbol=SYMBOL,
                                            action="SELL", qty=3, order_type="LIMIT", entry_price=29585.0)
        runner = route_identity.bind_entry_leg(first, both, account=ACCOUNT, symbol=SYMBOL,
                                               action="SELL", qty=3, order_type="LIMIT", entry_price=29585.0)
        self.assertEqual(tp1["orderId"], "649589730004")
        self.assertEqual(runner["orderId"], "649589730011")
        # 両脚が同じ窓に現れたら曖昧 → 束縛しない
        self.assertIsNone(route_identity.bind_entry_leg(
            empty, both, account=ACCOUNT, symbol=SYMBOL, action="SELL", qty=3,
            order_type="LIMIT", entry_price=29585.0))

    def test_incomplete_child_rows_never_bind(self):
        empty = tradovate_view()
        # 同方向でも parentId を持つ行(ブラケットの子)は親ではない
        only_children = tradovate_view(
            tradovate_row("649589730005", action="SELL", status="SUSPENDED", parent=649589730006))
        self.assertIsNone(route_identity.bind_entry_leg(
            empty, only_children, account=ACCOUNT, symbol=SYMBOL, action="SELL", qty=3,
            order_type="LIMIT", entry_price=29585.0))
        # 逆方向の親は別の注文
        other_side = tradovate_view(tradovate_row("649589730099", action="BUY"))
        self.assertIsNone(route_identity.bind_entry_leg(
            empty, other_side, account=ACCOUNT, symbol=SYMBOL, action="SELL", qty=3,
            order_type="LIMIT", entry_price=29585.0))

    def test_complete_rows_keep_strict_matching(self):
        empty = view()
        wrong_qty = view(order_row("PARENT-X", quantity=3))
        self.assertIsNone(route_identity.bind_entry_leg(
            empty, wrong_qty, account=ACCOUNT, symbol=SYMBOL, action="BUY", qty=1,
            order_type="LIMIT", entry_price=20000.0))
        right = view(order_row("PARENT-Y", quantity=3))
        self.assertEqual(route_identity.bind_entry_leg(
            empty, right, account=ACCOUNT, symbol=SYMBOL, action="BUY", qty=3,
            order_type="LIMIT", entry_price=20000.0)["orderId"], "PARENT-Y")


class R52SettledSnapshotTests(unittest.TestCase):
    """POST 直後の一覧は行が現れる前に読める。新しい行が見えるまで短く待つ。"""

    def _run(self, sequence, before, attempts=8):
        calls = []
        slept = []
        original = order._orders_snapshot
        order._orders_snapshot = lambda symbol, account=None: (
            calls.append(account) or (sequence.pop(0) if sequence else sequence_last[0]))
        sequence_last = [sequence[-1] if sequence else None]
        try:
            after = order._orders_snapshot_settled(SYMBOL, before, account=ACCOUNT,
                                                   attempts=attempts, delay=0.25,
                                                   sleep=lambda s: slept.append(s))
        finally:
            order._orders_snapshot = original
        return after, calls, slept

    def test_waits_until_a_new_row_appears(self):
        empty = tradovate_view()
        settled = tradovate_view(tradovate_row("649589730004"))
        after, calls, slept = self._run([empty, empty, settled], empty)
        self.assertIs(after, settled)
        self.assertEqual(len(calls), 3)
        self.assertEqual(slept, [0.25, 0.25])

    def test_gives_up_after_the_attempt_budget(self):
        empty = tradovate_view()
        after, calls, slept = self._run([empty, empty, empty, empty], empty, attempts=3)
        self.assertIs(after, empty)
        self.assertEqual(len(calls), 3)
        self.assertEqual(len(slept), 2)

    def test_reads_once_without_a_before_view(self):
        settled = tradovate_view(tradovate_row("649589730004"))
        after, calls, slept = self._run([settled], None)
        self.assertIs(after, settled)
        self.assertEqual(len(calls), 1)
        self.assertEqual(slept, [])

    def test_stops_when_the_view_becomes_unavailable(self):
        empty = tradovate_view()
        after, calls, slept = self._run([empty, None], empty)
        self.assertIsNone(after)
        self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()

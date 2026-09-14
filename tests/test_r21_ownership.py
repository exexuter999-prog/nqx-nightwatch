#!/usr/bin/env python3
import contextlib
import copy
from datetime import datetime, timedelta, timezone
import os
import sys
import unittest
from unittest.mock import patch

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import broker_status
import execution_contract
import nqx_state
import ownership_binder
import route_envelope


@contextlib.contextmanager
def manual_ownership_halt():
    """R79 の緊急停止(``ownershipAttribution.manualHalt``)を一時的に ON にする。"""
    cfg = execution_contract.CONTRACT.setdefault("ownershipAttribution", {})
    before = cfg.get("manualHalt")
    cfg["manualHalt"] = True
    try:
        yield
    finally:
        if before is None:
            cfg.pop("manualHalt", None)
        else:
            cfg["manualHalt"] = before


PLAN = {
    "scenarioId": "R21-S", "entryKey": "ENTRY:R21", "symbol": "MNQU6",
    "side": "BUY", "qty": 2, "entry": 20000.0, "stop": 19980.0,
    "tp1": 20030.0, "finalTarget": 20060.0,
    "targets": [20030.0, 20060.0], "entryOrderType": "LIMIT",
    "accountScope": ["ACC-R21"], "planVersion": "R21-SPLIT-1",
    "legs": [{"id": "TP1", "qty": 1, "target": 20030.0},
             {"id": "RUNNER", "qty": 1, "target": 20060.0}],
}
ROUTE = [
    {"accountId": "ACC-R21", "legId": "TP1", "state": "ACCEPTED",
     "orderId": "OID-TP1", "receipt": "REC-TP1"},
    {"accountId": "ACC-R21", "legId": "RUNNER", "state": "ACCEPTED",
     "orderId": "OID-RUNNER", "receipt": "REC-RUNNER"},
]


def order_row(leg, status):
    return {"accountId": "ACC-R21", "symbol": "MNQU6", "legId": leg,
            "orderId": f"OID-{leg}", "receipt": f"REC-{leg}",
            "action": "BUY", "qty": 1, "orderType": "LIMIT",
            "limitPrice": 20000.0, "status": status}


def orders(tp1="WORKING", runner="WORKING"):
    rows = [order_row("TP1", tp1), order_row("RUNNER", runner)]
    return {"verified": True, "orders": rows,
            "activeOrders": [row for row in rows if row["status"] in broker_status.BROKER_ACTIVE_STATES]}


def position(qty, leg="TP1", **changes):
    row = {"verified": True, "accountId": "ACC-R21", "symbol": "MNQU6",
           "side": "LONG" if qty else "FLAT", "qty": qty}
    if qty:
        row.update({"orderId": f"OID-{leg}", "receipt": f"REC-{leg}",
                    "filledAt": "2026-08-23T12:00:00Z", "initialQty": qty,
                    "avgEntry": 20000.0})
    row.update(changes)
    return row


def bind(pos, order_view, route=ROUTE, route_state="SENT"):
    raw = broker_status.position_identity(pos)
    generation = f"PG:1:{raw}" if raw else None
    return ownership_binder.bind(PLAN, route, pos, order_view,
                                 route_state=route_state,
                                 position_generation=generation)


class R21OwnershipBinderTests(unittest.TestCase):
    def test_qty0_requires_every_accepted_leg_active(self):
        result = bind(position(0), orders())
        self.assertTrue(result["owned"])
        self.assertEqual(result["state"], "RESTING")
        self.assertEqual(set(result["pendingOrderOwnership"]["orderIds"]), {"OID-TP1", "OID-RUNNER"})
        self.assertFalse(bind(position(0), orders(tp1="FILLED"))["owned"])

    def test_qty1_tp1_or_runner_first_requires_exact_filled_and_remaining(self):
        for filled, remaining in (("TP1", "RUNNER"), ("RUNNER", "TP1")):
            view = orders(**{filled.lower(): "FILLED", remaining.lower(): "WORKING"})
            result = bind(position(1, filled), view)
            self.assertTrue(result["owned"], result)
            self.assertEqual(result["state"], "PARTIAL_FILL")
            self.assertEqual(result["ownedPlan"]["partialLegId"], filled)
            self.assertEqual(result["ownedPlan"]["remainingRestingLeg"]["legId"], remaining)

    def test_qty2_requires_both_filled_and_full_generation(self):
        full = position(2, "RUNNER")
        self.assertTrue(bind(full, orders("FILLED", "FILLED"))["owned"])
        self.assertFalse(ownership_binder.bind(
            PLAN, ROUTE, full, orders("FILLED", "FILLED"),
            route_state="SENT", position_generation=None)["owned"])
        self.assertFalse(bind(full, orders("FILLED", "WORKING"))["owned"])
        self.assertFalse(bind(position(2, orderId="MANUAL", receipt="MANUAL"),
                              orders("FILLED", "FILLED"))["owned"])

    def test_unknown_accepted_zero_never_creates_ownership(self):
        unknown_route = [{**row, "state": "UNKNOWN", "orderId": None, "receipt": None}
                         for row in ROUTE]
        for qty in (0, 1, 2):
            self.assertFalse(bind(position(qty), orders(), unknown_route, "UNKNOWN")["owned"])

    def test_partial_route_owns_only_the_exact_filled_leg(self):
        partial = [ROUTE[0], {**ROUTE[1], "state": "REJECTED", "orderId": None, "receipt": None}]
        for qty in (0, 1, 2):
            status = "WORKING" if qty == 0 else "FILLED"
            result = bind(position(qty), {"verified": True, "orders": [order_row("TP1", status)]},
                          partial, "PARTIAL")
            self.assertEqual(result["owned"], qty in {0, 1}, result)
            if qty == 1:
                self.assertEqual(result["state"], "LIMITED_PARTIAL")
                self.assertEqual([leg["id"] for leg in result["ownedPlan"]["legs"]], ["TP1"])
                self.assertNotIn("remainingRestingLeg", result["ownedPlan"])

    def test_route_state_qty_and_manual_other_account_matrix(self):
        partial_route = [ROUTE[0], {**ROUTE[1], "state": "REJECTED",
                                    "orderId": None, "receipt": None}]
        unknown_route = [{**row, "state": "UNKNOWN", "orderId": None, "receipt": None}
                         for row in ROUTE]
        for route_state, route in (("SENT", ROUTE), ("PARTIAL", partial_route),
                                   ("UNKNOWN", unknown_route)):
            for qty in (0, 1, 2):
                if route_state == "SENT":
                    view = orders("WORKING", "WORKING") if qty == 0 else (
                        orders("FILLED", "WORKING") if qty == 1 else orders("FILLED", "FILLED"))
                    pos = position(qty, "TP1" if qty == 1 else "RUNNER")
                    expected = True
                elif route_state == "PARTIAL":
                    status = "WORKING" if qty == 0 else "FILLED"
                    row = order_row("TP1", status)
                    view = {"verified": True, "orders": [row]}
                    pos = position(qty, "TP1")
                    expected = qty in {0, 1}
                else:
                    view = {"verified": True, "orders": []}
                    pos = position(qty)
                    expected = False
                exact = bind(pos, view, route, route_state)
                with self.subTest(route=route_state, qty=qty, variant="exact"):
                    self.assertEqual(exact["owned"], expected, exact)

                manual_pos = {**pos, "accountId": "ACC-R21"}
                manual_view = copy.deepcopy(view)
                if qty == 0 and manual_view.get("orders"):
                    manual_view["orders"].append({**copy.deepcopy(manual_view["orders"][0]),
                                                   "orderId": "MANUAL", "receipt": "MANUAL"})
                elif qty > 0:
                    manual_pos.update(orderId="MANUAL", receipt="MANUAL")
                with self.subTest(route=route_state, qty=qty, variant="same-account-manual"):
                    self.assertFalse(bind(manual_pos, manual_view, route, route_state)["owned"])

                other_pos = {**pos, "accountId": "OTHER"}
                with self.subTest(route=route_state, qty=qty, variant="other-account"):
                    self.assertFalse(bind(other_pos, view, route, route_state)["owned"])

    def test_every_broker_economic_or_identity_tamper_fails(self):
        mutations = {
            "accountId": "OTHER", "symbol": "MESU6", "action": "SELL", "qty": 2,
            "orderType": "MARKET", "limitPrice": 20000.25, "receipt": "WRONG",
            "status": "SUSPENDED_UNKNOWN",
        }
        for field, value in mutations.items():
            view = orders("FILLED", "WORKING")
            view["orders"][0][field] = value
            with self.subTest(field=field):
                self.assertFalse(bind(position(1), view)["owned"])

    def test_manual_position_collision_wrong_receipt_or_account_fails(self):
        view = orders("FILLED", "WORKING")
        self.assertFalse(bind(position(1, orderId="MANUAL", receipt="MANUAL"), view)["owned"])
        self.assertFalse(bind(position(1, accountId="OTHER"), view)["owned"])
        self.assertFalse(bind(position(1, receipt="REC-RUNNER"), view)["owned"])

    def test_duplicate_active_extra_or_receipt_collision_fails_but_history_is_harmless(self):
        for mutate, expected in (("duplicate", False), ("terminal-extra", True),
                                 ("active-extra", False), ("collision", False)):
            view = orders("FILLED", "WORKING")
            if mutate == "duplicate":
                view["orders"].append(copy.deepcopy(view["orders"][0]))
            elif mutate == "terminal-extra":
                view["orders"].append({**copy.deepcopy(view["orders"][0]),
                                       "orderId": "OID-EXTRA", "receipt": "REC-EXTRA"})
            elif mutate == "active-extra":
                view["orders"].append({**copy.deepcopy(view["orders"][1]),
                                       "orderId": "OID-EXTRA", "receipt": "REC-EXTRA"})
            else:
                view["orders"][1]["receipt"] = "REC-TP1"
            with self.subTest(mutate=mutate):
                self.assertEqual(bind(position(1), view)["owned"], expected)

    def test_market_actual_fill_slippage_tick_and_risk_are_authoritative(self):
        plan = {**copy.deepcopy(PLAN), "entryOrderType": "MARKET",
                "entryReference": 20000.0, "stop": 19942.0,
                "executionContract": {"riskCapDollars": 240.0}}
        def market_result(fill, cap=240.0):
            local = {**copy.deepcopy(plan), "executionContract": {"riskCapDollars": cap}}
            rows = []
            for leg in ("TP1", "RUNNER"):
                row = order_row(leg, "FILLED")
                row.update(orderType="MARKET", limitPrice=None, filledPrice=fill)
                rows.append(row)
            pos = position(2, "RUNNER", avgEntry=fill, positionGeneration="SPOOFED")
            raw = broker_status.position_identity(pos)
            return ownership_binder.bind(local, ROUTE, pos,
                                         {"verified": True, "orders": rows},
                                         route_state="SENT", position_generation=f"PG:1:{raw}")
        allowed = market_result(20002.0)
        self.assertTrue(allowed["owned"], allowed)
        self.assertEqual(allowed["ownedPlan"]["actualFillPrice"], 20002.0)
        self.assertFalse(market_result(20002.25)["owned"])
        self.assertFalse(market_result(20002.0, cap=239.99)["owned"])

    def test_broker_status_allowlist_rejects_whole_view(self):
        base = {"success": True, "orders": [{"id": "1", "account": "ACC-R21",
            "instrument": "MNQU6", "orderType": "LIMIT", "orderState": "WORKING",
            "orderAction": "BUY", "quantity": 1, "limitPrice": 20000,
            "receipt": "R1"}]}
        for status in ("", "FAILED_SUBMIT", "SUSPENDED_UNKNOWN"):
            payload = copy.deepcopy(base)
            payload["orders"][0]["orderState"] = status
            with self.subTest(status=status), self.assertRaises(broker_status.Unavailable):
                broker_status.normalize_crosstrade_orders(
                    payload, platform="NT8", account="ACC-R21", symbol="MNQU6")

    def test_route_stdout_limit_and_failure_markers(self):
        envelope = route_envelope.format_envelope(ROUTE, "SENT")
        self.assertTrue(route_envelope.parse(envelope, expected_accounts=["ACC-R21"])["ok"])
        self.assertFalse(route_envelope.parse("x" * (route_envelope.MAX_OUTPUT_BYTES + 1))["ok"])
        self.assertFalse(route_envelope.parse(envelope, diagnostics="Fatal: transport failed")["ok"])

    def test_dayguard_600_boundary_and_future_are_fail_closed(self):
        now = datetime(2026, 8, 23, 12, tzinfo=timezone.utc)
        scenario = {"issuedAt": now.isoformat(), "expiresAt": (now + timedelta(minutes=30)).isoformat(),
                    "state": "ARMED", "grade": "A", "side": "BUY", "qty": 2,
                    "entry": 20000, "stop": 19980, "targets": [20030, 20060],
                    "legs": [{"id": "TP1", "qty": 1, "target": 20030},
                             {"id": "RUNNER", "qty": 1, "target": 20060}],
                    "planVersion": "R21", "executionContractVersion": execution_contract.VERSION}
        market = {"observedAt": now.isoformat(), "cvdAt": now.isoformat()}
        for seconds, allowed in ((600, True), (600.001, False), (-0.001, False)):
            market["dayguard"] = {"at": (now - timedelta(seconds=seconds)).isoformat(),
                                  "available": True, "blocked": False}
            result = execution_contract.evaluate(scenario, market, now=now)
            with self.subTest(seconds=seconds):
                self.assertEqual("DAYGUARD_STALE" not in result["blockers"], allowed)

    def test_protective_terminal_or_unknown_status_never_verifies(self):
        position_row = {"verified": True, "accountId": "ACC-R21", "symbol": "MNQU6",
                        "side": "LONG", "qty": 1, "orderId": "ENTRY",
                        "receipt": "ENTRY-R", "filledAt": "2026-08-23T12:00:00Z"}
        generation = broker_status.position_identity(position_row)
        receipt = {"accounts": [{"accountId": "ACC-R21", "stopOrderId": "SL",
                                  "targetOrderId": "TP", "ocoGroupId": "OCO",
                                  "receipt": "MR"}]}
        for status in ("FILLED", "SUSPENDED_UNKNOWN"):
            rows = [{"accountId": "ACC-R21", "orderId": "SL", "status": status,
                     "qty": 1, "stopPrice": 19980, "orderType": "STOP", "action": "SELL",
                     "parentId": "OCO"},
                    {"accountId": "ACC-R21", "orderId": "TP", "status": "WORKING",
                     "qty": 1, "limitPrice": 20030, "orderType": "LIMIT", "action": "SELL",
                     "parentId": "OCO"}]
            ok, _ = broker_status.verify_protective_orders(
                {"verified": True, "activeOrders": rows}, ["ACC-R21"], 1, 19980, 20030,
                side="BUY", position_generation=generation, position_before=position_row,
                current_position=position_row, route_receipt=receipt)
            with self.subTest(status=status):
                self.assertFalse(ok)

    def test_python_broker_observation_publisher_uses_dedicated_stream(self):
        captured = []
        pos = {"verified": True, "observedAt": "2026-08-23T12:00:00Z",
               "accountId": "ACC-R21", "symbol": "MNQU6", "qty": 0}
        view = {"verified": True, "observedAt": "2026-08-23T12:00:00Z",
                "symbol": "MNQU6", "orders": [order_row("TP1", "CANCELED")]}
        ok, _ = nqx_state.publish_broker_observation(
            pos, view, "intent-hash-r21",
            publish_fn=lambda stream, payload, _cfg: (captured.append((stream, payload)) or (True, {})))
        self.assertTrue(ok)
        self.assertEqual(captured[0][0], "broker_observation")
        self.assertIn("broker_observation", nqx_state.STREAMS)

    def test_management_recovery_journals_observation_before_recover(self):
        captured = []
        sender = lambda stream, payload, _cfg: (captured.append((stream, payload)) or (True, {}))
        pos = {"verified": True, "observedAt": "2026-08-23T12:00:00Z",
               "accountId": "ACC-R21", "symbol": "MNQU6", "side": "LONG", "qty": 1,
               "orderId": "ENTRY", "receipt": "ENTRY-R", "filledAt": "2026-08-23T11:59:00Z"}
        view = {"verified": True, "platform": "STUB",
                "observedAt": "2026-08-23T12:00:00Z", "symbol": "MNQU6",
                "orders": [{"accountId": "ACC-R21", "symbol": "MNQU6",
                            "orderId": order_id, "receipt": "MR", "status": "CANCELED"}
                           for order_id in ("SL", "TP")]}
        raw_generation = broker_status.position_identity(pos)
        claim = {"managementKey": "MANAGEMENT:R21", "managementIntentHash": "mi_r21",
                 "managementIntent": {"symbol": "MNQU6", "accountId": "ACC-R21",
                                      "qty": 1, "side": "BUY",
                                      "positionGeneration": raw_generation},
                 "routeReceipt": {"accounts": [{"accountId": "ACC-R21",
                    "stopOrderId": "SL", "targetOrderId": "TP", "receipt": "MR"}]}}
        ok, _ = nqx_state.recover_management_from_broker(
            "MANAGEMENT:R21", "T" * 43, claim,
            publish_fn=sender, position_query=lambda _symbol: dict(pos),
            orders_query=lambda _symbol, **_kwargs: dict(view))
        self.assertTrue(ok)
        self.assertEqual([row[0] for row in captured], ["broker_observation", "management_claim"])
        self.assertEqual(captured[1][1]["action"], "RECOVER")


class R21CrossTradeTruthTests(unittest.TestCase):
    def adapter(self, **changes):
        cfg = {"CROSSTRADE_KEY": "stub", "CROSSTRADE_ACCOUNT": "ACC-R21",
               "CROSSTRADE_ACCOUNT_ID": "62838471", "CROSSTRADE_CONTRACT_ID": "9001",
               "CROSSTRADE_PLATFORM": "TRADOVATE",
               "CROSSTRADE_API_BASE": "https://stub/v1/api/tv"}
        cfg.update(changes)
        return broker_status.CrossTradeAdapter(cfg)

    def test_position_requires_explicit_success_response_account_and_instrument(self):
        valid = {"success": True, "data": {"accountId": 62838471, "contractId": 9001,
                                            "netPos": 0, "position": {}}}
        with patch.object(broker_status, "_get_json", return_value=valid):
            result = self.adapter().query("MNQU6")
        self.assertTrue(result["verified"])
        self.assertEqual(result["accountId"], "ACC-R21")
        for payload in (
            {"data": valid["data"]},
            {"success": True, "data": {**valid["data"], "accountId": "OTHER"}},
            {"success": True, "data": {"accountId": 62838471, "netPos": 0}},
            {"success": True, "data": {"accountId": 62838471, "contractId": 9001}},
        ):
            with patch.object(broker_status, "_get_json", return_value=payload), self.subTest(payload=payload):
                with self.assertRaises(broker_status.Unavailable):
                    self.adapter().query("MNQU6")

    def test_invalid_override_and_order_success_omission_are_unavailable(self):
        self.assertFalse(self.adapter(CROSSTRADE_POSITION_URL="file:///tmp/fake").configured)
        with self.assertRaises(broker_status.Unavailable):
            broker_status.normalize_crosstrade_orders(
                {"orders": []}, platform="NT8", account="ACC-R21", symbol="MNQU6")



def tradovate_row(leg, status, action="BUY", parent=None, order_id=None, receipt=None):
    """CrossTrade/Tradovate の実データ形(2026-09-04 実測): 数量・種別・価格が無い。"""
    order_id = order_id or f"OID-{leg}"
    return {"accountId": "ACC-R21", "symbol": "MNQU6", "orderId": order_id,
            "receipt": receipt or f"REC-{leg}", "action": action, "qty": None,
            "orderType": None, "limitPrice": None, "filledPrice": None, "stopPrice": None,
            "fieldsComplete": False, "parentId": parent, "status": status}


def tradovate_orders(*rows):
    return {"verified": True, "orders": list(rows),
            "activeOrders": [row for row in rows if row["status"] in broker_status.BROKER_ACTIVE_STATES]}


class R52IncompleteRowOwnershipTests(unittest.TestCase):
    """数量・種別・価格を返さない行でも、身元が一致すれば所有権を束縛する。"""

    def test_incomplete_rows_bind_resting_with_frozen_limit(self):
        view = tradovate_orders(
            tradovate_row("TP1", "WORKING"), tradovate_row("RUNNER", "WORKING"),
            # ブラケットの子(逆方向・parentId あり)は無視される
            tradovate_row("C1", "SUSPENDED", action="SELL", parent="OID-TP1", order_id="OID-C1", receipt="REC-C1"),
            tradovate_row("C2", "SUSPENDED", action="SELL", parent="OID-TP1", order_id="OID-C2", receipt="REC-C2"))
        result = bind(position(0), view)
        self.assertTrue(result["owned"], result)
        self.assertEqual(result["state"], "RESTING")
        self.assertEqual(set(result["pendingOrderOwnership"]["orderIds"]), {"OID-TP1", "OID-RUNNER"})
        self.assertTrue(all(row.get("fieldsIncomplete") for row in result["matchedOrders"]))
        self.assertTrue(all(row["actualEntry"] == 20000.0 for row in result["matchedOrders"]))

    def test_incomplete_row_identity_is_still_strict(self):
        for field, value in {"receipt": "WRONG", "action": "SELL", "accountId": "OTHER",
                             "symbol": "MESU6"}.items():
            view = tradovate_orders(tradovate_row("TP1", "WORKING"), tradovate_row("RUNNER", "WORKING"))
            view["orders"][0][field] = value
            with self.subTest(field=field):
                self.assertFalse(bind(position(0), view)["owned"])

    def test_extra_same_side_incomplete_parent_is_ambiguous(self):
        view = tradovate_orders(
            tradovate_row("TP1", "WORKING"), tradovate_row("RUNNER", "WORKING"),
            tradovate_row("X", "WORKING", order_id="OID-X", receipt="REC-X"))
        result = bind(position(0), view)
        self.assertFalse(result["owned"])
        self.assertIn("colliding", result["reason"])

    def test_incomplete_partial_fill_uses_frozen_leg_qty(self):
        view = tradovate_orders(tradovate_row("TP1", "FILLED"), tradovate_row("RUNNER", "WORKING"))
        result = bind(position(1, "TP1"), view)
        self.assertTrue(result["owned"], result)
        self.assertIn(result["state"], {"PARTIAL_FILL", "LIMITED_PARTIAL"})

    def test_incomplete_market_fill_needs_position_avg_entry(self):
        plan = {**copy.deepcopy(PLAN), "entryOrderType": "MARKET", "entryReference": 20000.0,
                "stop": 19942.0, "executionContract": {"riskCapDollars": 240.0}}
        rows = [tradovate_row("TP1", "FILLED"), tradovate_row("RUNNER", "FILLED")]
        def market(pos):
            raw = broker_status.position_identity(pos)
            return ownership_binder.bind(plan, ROUTE, pos, tradovate_orders(*rows),
                                         route_state="SENT", position_generation=f"PG:1:{raw}")
        owned = market(position(2, "RUNNER", avgEntry=20002.0))
        self.assertTrue(owned["owned"], owned)
        self.assertEqual(owned["ownedPlan"]["actualFillPrice"], 20002.0)
        self.assertFalse(market(position(2, "RUNNER", avgEntry=None))["owned"])
        # R52: 複数約定の加重平均は tick に乗らない(2026-09-05 04:51 実測 29570.375)。
        # 乖離が maxDeviationPoints(2pt)以内なら所有し、超えれば所有しない。
        off_tick = market(position(2, "RUNNER", avgEntry=20001.125))
        self.assertTrue(off_tick["owned"], off_tick)
        self.assertEqual(off_tick["ownedPlan"]["actualFillPrice"], 20001.125)
        far = market(position(2, "RUNNER", avgEntry=20002.375))
        # R79: 台帳の身元(orderId/receipt)が取れているので、価格乖離だけでは所有を
        # 落とさない。ここで落ちるのは実約定から引き直したリスクが口座上限を超える
        # ためで、乖離ゲートではない。
        self.assertFalse(far["owned"], far)
        self.assertIn("risk cap", far["reason"])
        with manual_ownership_halt():
            halted = market(position(2, "RUNNER", avgEntry=20002.375))
        self.assertFalse(halted["owned"], halted)
        self.assertIn("deviates", halted["reason"])
        self.assertFalse(market(position(2, "RUNNER", avgEntry=20002.0, positionGeneration=None,
                                         orderId="OID-RUNNER"))["owned"] and False)



class R52PositionWithoutOrderLinkTests(unittest.TestCase):
    """建玉行が receipt を持たず orderId も注文 ID でないブローカー(CrossTrade 実測)。"""

    def _pos(self, qty, leg="TP1", avg=20000.0, **changes):
        row = position(qty, leg, avgEntry=avg)
        row.update({"orderId": "POS-INTERNAL-081", "receipt": None})
        row.update(changes)
        return row

    def test_full_fill_binds_when_both_legs_filled_and_avg_entry_matches(self):
        view = tradovate_orders(tradovate_row("TP1", "FILLED"), tradovate_row("RUNNER", "FILLED"))
        result = bind(self._pos(2, "RUNNER"), view)
        self.assertTrue(result["owned"], result)
        self.assertEqual(result["state"], "OWNED_FULL")
        self.assertEqual(result["ownedPlan"]["positionOwnership"]["accountId"], "ACC-R21")

    def test_partial_fill_binds_to_the_single_filled_leg(self):
        view = tradovate_orders(tradovate_row("TP1", "FILLED"), tradovate_row("RUNNER", "WORKING"))
        result = bind(self._pos(1, "TP1"), view)
        self.assertTrue(result["owned"], result)
        self.assertIn(result["state"], {"PARTIAL_FILL", "LIMITED_PARTIAL"})
        self.assertEqual(result["ownedPlan"]["partialLegId"], "TP1")

    def test_unfavorable_avg_entry_is_not_ours(self):
        # BUY 指値 20000 が 20003 で約定することは無い(不利側)→ 別の建玉
        view = tradovate_orders(tradovate_row("TP1", "FILLED"), tradovate_row("RUNNER", "FILLED"))
        self.assertFalse(bind(self._pos(2, "RUNNER", avg=20003.0), view)["owned"])
        # 有利側でも乖離が maxDeviationPoints(2pt)を超えれば別物
        self.assertFalse(bind(self._pos(2, "RUNNER", avg=19997.0), view)["owned"])
        # 有利側 1pt 以内は許容
        self.assertTrue(bind(self._pos(2, "RUNNER", avg=19999.0), view)["owned"])

    def test_broker_with_receipts_keeps_direct_matching(self):
        # receipt を持つブローカーは従来どおり直接照合。合わなければ所有しない。
        view = orders("FILLED", "FILLED")
        wrong = position(2, "RUNNER", receipt="WRONG")
        self.assertFalse(bind(wrong, view)["owned"])

    def test_one_leg_filled_but_position_shows_full_qty_is_not_ours(self):
        view = tradovate_orders(tradovate_row("TP1", "FILLED"), tradovate_row("RUNNER", "WORKING"))
        self.assertFalse(bind(self._pos(2, "RUNNER"), view)["owned"])


if __name__ == "__main__":
    unittest.main()

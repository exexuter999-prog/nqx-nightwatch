# -*- coding: utf-8 -*-
"""R52: 価格が建値より先に TP1 へ届いた未約定エントリーの自動取消と経路復活。

2026-09-04 23:57 の SELL 指値 6枚(29,585 / TP1 29,498.5)は約定せず、価格は先に TP1 へ
届いた。指値が残る限り startup recovery は BROKER_NOT_EMPTY で止まり、人が手で
消すまで新規が再開しなかった(2026-09-05 ユーザー指示で自動化)。

すべてスタブ。外部送信は 0。

    python tests/test_r52_stale_entry_cancel.py
"""
import copy
import os
import sys
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
import autotrade_engine as ae  # noqa: E402
import order  # noqa: E402

# 本番 .secrets/crosstrade.env から隔離(tests/_hermetic.py と同じ方針)
ae._read_env_file = lambda path=None: {}

ACC = "ACC-STALE-01"
KEY = "ENTRY:" + "e" * 64
SENT_AT = "2026-09-04T14:57:23.000000+00:00"
SENT_EPOCH = 1788533843          # 2026-09-04T14:57:23Z
PLAN = {"entryKey": KEY, "symbol": "MNQU6", "side": "SELL", "qty": 6, "entry": 29585.0,
        "initialStop": 29627.25, "tp1": 29498.5, "finalTarget": 29350.75,
        "targets": [29498.5, 29350.75],
        "legs": [{"id": "TP1", "qty": 3, "target": 29498.5}, {"id": "RUNNER", "qty": 3, "target": 29350.75}],
        "accountScope": [ACC], "mode": "SPLIT_BRACKETS_TP1_RUNNER", "ultra": True,
        # R52: 取消は自分の注文 ID が分かっているときだけ(flatten は口座の未約定を全部消す)
        "routeSnapshot": [{"accountId": ACC, "legId": "TP1", "state": "ACCEPTED", "orderId": "P1",
                           "receipt": f"TRADOVATE:{ACC}:P1"},
                          {"accountId": ACC, "legId": "RUNNER", "state": "ACCEPTED", "orderId": "P2",
                           "receipt": f"TRADOVATE:{ACC}:P2"}]}
JOURNAL = {"entryKey": KEY, "claimToken": "T" * 43, "executionIntentHash": "ih-stale",
           "executionIntent": {"version": "fixture"}}
CLAIM = {"entryKey": KEY, "state": "CONSUMED", "routeState": "UNKNOWN", "acceptedCount": 0,
         "executionIntentHash": "ih-stale", "executionIntent": {"accountScope": [ACC], "symbol": "MNQU6"},
         "routeSnapshot": [{"state": "UNKNOWN", "orderId": None, "receipt": None}]}
# 二重キーは明示する。未指定だと engine は本物の武装台帳(Worker/ローカル)へ倒れるので、
# テストが実運用の AUTO 状態に引きずられる。dry は "0" を明示(明示 0 は台帳より強い)。
CFG = {"NQX_AUTOTRADE": "1", "NQX_LIVE_ORDERS": "1", "NQX_AUTOTRADE_KILL": "0",
       "NQX_SYMBOL": "MNQU6", "CROSSTRADE_ACCOUNTS": ACC,
       "NQX_STALE_CANCEL_SETTLE_SEC": "0"}   # テストでは取消後の待ちを省く


def flat_position():
    return {"verified": True, "symbol": "MNQU6", "qty": 0, "side": "FLAT", "accountId": ACC}


def pending_orders(extra_parent=None):
    rows = [{"orderId": "P1", "status": "WORKING", "action": "SELL", "parentId": None},
            {"orderId": "P2", "status": "WORKING", "action": "SELL", "parentId": None},
            # ブラケットの子(parentId あり)は親ではない
            {"orderId": "C1", "status": "SUSPENDED", "action": "BUY", "parentId": "P1"}]
    if extra_parent:
        rows.append({"orderId": extra_parent, "status": "WORKING", "action": "SELL", "parentId": None})
    return {"verified": True, "symbol": "MNQU6", "state": "PENDING", "openCount": len(rows),
            "orders": rows, "activeOrders": rows,
            "orderIds": [r["orderId"] for r in rows], "filledOrderIds": [], "accountScope": [ACC]}


def empty_orders():
    return {"verified": True, "symbol": "MNQU6", "state": "NONE", "openCount": 0,
            "orders": [], "activeOrders": [], "orderIds": [], "filledOrderIds": [], "accountScope": [ACC]}


def bundle_with_low(low, when=SENT_EPOCH + 600, price=None):
    # 送信後のバー1本。h/l を持つ(engine の _extreme は h/l/high/low を読む)。
    # バーが無ければ engine は現在値(price)へ倒れるので、テストは price も制御する。
    return {"symbol": "MNQU6", "price": low + 20 if price is None else price,
            "snapshot": {"bars3m": [{"time": when, "open": low + 30, "high": low + 40,
                                     "low": low, "close": low + 20}]},
            "scenarios": {"primary": {"symbol": "MNQU6"}}}


class Harness:
    def __init__(self, live=True, recover_ok=True):
        self.calls = []
        self.flat = False
        self.recover_calls = []
        self.recover_ok = recover_ok
        self.cfg = dict(CFG)
        self.extra_parent = None
        if not live:
            self.cfg["NQX_LIVE_ORDERS"] = "0"

    def position(self, _symbol):
        return flat_position()

    def orders(self, _symbol, **_kwargs):
        return empty_orders() if self.flat else pending_orders(self.extra_parent)

    def runner(self, args, live):
        self.calls.append((list(args), live))
        if live and args[:1] == ["--flatten"]:
            self.flat = True
        return 0, "FLATTEN VERIFIED: all targeted broker accounts FLAT and orders nonblocking"

    def recover(self, claim, journal, query, order_query, symbol):
        self.recover_calls.append(claim.get("entryKey"))
        if self.recover_ok:
            return True, {"ok": True}
        return False, {"reason": "ENTRY_RECOVERY_BROKER_NOT_EMPTY"}

    def run(self, bundle, ledger, state_ok=False):
        return ae.reconcile(bundle, state_ok, self.cfg, self.position, self.runner, ledger,
                            broker_order_query=self.orders,
                            state_query=lambda: {"entryClaim": copy.deepcopy(CLAIM)},
                            recover_entry=self.recover)


def seed(ledger):
    ae._append_ledger({"key": KEY, "entryKey": KEY, "status": "ENTRY_CLAIMED", "action": "ENTRY_CLAIM",
                       "plan": copy.deepcopy(PLAN), "claimJournal": JOURNAL, "time": SENT_AT}, ledger)
    ae._append_ledger({"key": KEY, "entryKey": KEY, "status": "HALT", "action": "ENTRY",
                       "reason": "live send failed/unknown", "plan": copy.deepcopy(PLAN)}, ledger)


class StaleEntryCancelTests(unittest.TestCase):
    def test_tp1_reached_first_cancels_and_revives_in_one_cycle(self):
        h = Harness()
        with tempfile.TemporaryDirectory() as tmp:
            ledger = os.path.join(tmp, "l.jsonl")
            seed(ledger)
            notes = h.run(bundle_with_low(29490.0), ledger)   # 安値 29,490 ≤ TP1 29,498.5
            records, _ = ae._read_ledger(ledger)
        self.assertEqual([c[0] for c in h.calls], [["--flatten", "--account", ACC]] * 2)
        self.assertEqual([c[1] for c in h.calls], [False, True], "dry-run → live の順")
        statuses = [(r.get("status"), r.get("action")) for r in records[2:]]
        self.assertIn(("ENTRY_HALTED", "ENTRY_STALE_CANCEL"), statuses)
        self.assertIn(("ENTRY_RECOVERED", "ENTRY_RECOVERY"), statuses)
        self.assertEqual(h.recover_calls, [KEY])
        self.assertIsNone(ae._has_halt(records), "同じサイクルで HALT が解ける")
        self.assertTrue(notes[0].startswith("autotrade stale entry canceled: TP1 reached before entry"))
        self.assertIn("frozen state was not published", notes[-1])
        halted = next(r for r in records if r.get("status") == "ENTRY_HALTED")
        self.assertEqual(halted["plan"]["entry"], 29585.0)

    def test_tp1_not_reached_keeps_the_resting_entry(self):
        h = Harness(recover_ok=False)
        with tempfile.TemporaryDirectory() as tmp:
            ledger = os.path.join(tmp, "l.jsonl")
            seed(ledger)
            notes = h.run(bundle_with_low(29520.0), ledger)   # 安値 29,520 > TP1
            records, _ = ae._read_ledger(ledger)
        self.assertEqual(h.calls, [], "取消は送らない")
        self.assertFalse(any(r.get("status") == "ENTRY_HALTED" for r in records))
        self.assertIn("startup recovery failed", notes[0])

    def test_bars_before_the_send_do_not_count(self):
        h = Harness(recover_ok=False)
        with tempfile.TemporaryDirectory() as tmp:
            ledger = os.path.join(tmp, "l.jsonl")
            seed(ledger)
            # 送信前の安値 29,400 は数えない。送信後のバーが無いので現在値(29,540 > TP1)で判定。
            notes = h.run(bundle_with_low(29400.0, when=SENT_EPOCH - 600, price=29540.0), ledger)
        self.assertEqual(h.calls, [], "送信前の安値では取り消さない")
        self.assertIn("startup recovery failed", notes[0])

    def test_buy_side_uses_the_high(self):
        h = Harness()
        buy_plan = {**PLAN, "side": "BUY", "entry": 29400.0, "initialStop": 29350.0,
                    "tp1": 29480.0, "finalTarget": 29600.0, "targets": [29480.0, 29600.0]}
        with tempfile.TemporaryDirectory() as tmp:
            ledger = os.path.join(tmp, "l.jsonl")
            ae._append_ledger({"key": KEY, "entryKey": KEY, "status": "ENTRY_CLAIMED", "action": "ENTRY_CLAIM",
                               "plan": buy_plan, "claimJournal": JOURNAL, "time": SENT_AT}, ledger)
            bundle = {"symbol": "MNQU6", "price": 29470.0,
                      "snapshot": {"bars3m": [{"time": SENT_EPOCH + 300, "open": 29450, "high": 29485,
                                               "low": 29440, "close": 29470}]},
                      "scenarios": {"primary": {"symbol": "MNQU6"}}}
            notes = h.run(bundle, ledger)
        self.assertEqual(len(h.calls), 2)
        self.assertIn("high since", notes[0])

    def test_dry_mode_only_proposes(self):
        h = Harness(live=False)
        with tempfile.TemporaryDirectory() as tmp:
            ledger = os.path.join(tmp, "l.jsonl")
            seed(ledger)
            notes = h.run(bundle_with_low(29490.0), ledger)
            records, _ = ae._read_ledger(ledger)
        self.assertEqual([c[1] for c in h.calls], [False], "dry-run だけ")
        self.assertTrue(notes[0].startswith("autotrade proposal ENTRY_STALE_CANCEL"))
        self.assertFalse(any(r.get("status") == "ENTRY_HALTED" for r in records))

    def test_kill_switch_env_disables_the_cancel(self):
        h = Harness(recover_ok=False)
        h.cfg["NQX_STALE_ENTRY_CANCEL"] = "0"
        with tempfile.TemporaryDirectory() as tmp:
            ledger = os.path.join(tmp, "l.jsonl")
            seed(ledger)
            h.run(bundle_with_low(29490.0), ledger)
        self.assertEqual(h.calls, [])

    def test_already_recovered_plan_never_cancels_foreign_orders(self):
        h = Harness(recover_ok=False)
        with tempfile.TemporaryDirectory() as tmp:
            ledger = os.path.join(tmp, "l.jsonl")
            seed(ledger)
            ae._append_ledger({"key": KEY, "entryKey": KEY, "status": "ENTRY_RECOVERED",
                               "action": "ENTRY_RECOVERY", "claimJournal": JOURNAL}, ledger)
            h.run(bundle_with_low(29490.0), ledger)
        self.assertEqual(h.calls, [], "回復済みプランの後に残る注文は別物(手動)なので触らない")

    def test_manual_parent_order_in_the_same_account_blocks_the_cancel(self):
        h = Harness(recover_ok=False)
        h.extra_parent = "MANUAL-18"          # ユーザーが手で入れた別の親注文
        with tempfile.TemporaryDirectory() as tmp:
            ledger = os.path.join(tmp, "l.jsonl")
            seed(ledger)
            h.run(bundle_with_low(29490.0), ledger)
        self.assertEqual(h.calls, [], "手動注文が同居していれば flatten を送らない")

    def test_plan_without_bound_order_ids_never_cancels(self):
        h = Harness(recover_ok=False)
        with tempfile.TemporaryDirectory() as tmp:
            ledger = os.path.join(tmp, "l.jsonl")
            plan = {**copy.deepcopy(PLAN), "routeSnapshot": [
                {"accountId": ACC, "legId": "TP1", "state": "UNKNOWN", "orderId": None, "receipt": None}]}
            ae._append_ledger({"key": KEY, "entryKey": KEY, "status": "ENTRY_CLAIMED", "action": "ENTRY_CLAIM",
                               "plan": plan, "claimJournal": JOURNAL, "time": SENT_AT}, ledger)
            h.run(bundle_with_low(29490.0), ledger)
        self.assertEqual(h.calls, [], "identity の無い残骸は自動では消さない")

    def test_unverified_cancel_halts_without_recovery(self):
        h = Harness()
        # 送信は通るが、注文が消えない(ブローカー側で残る)
        h.runner = lambda args, live: (h.calls.append((list(args), live)) or (0, "sent"))
        with tempfile.TemporaryDirectory() as tmp:
            ledger = os.path.join(tmp, "l.jsonl")
            seed(ledger)
            notes = h.run(bundle_with_low(29490.0), ledger)
            records, _ = ae._read_ledger(ledger)
        self.assertTrue(notes[0].startswith("AUTOTRADE HALT: post-cancel broker order unverified/blocking"))
        self.assertEqual(h.recover_calls, [])
        self.assertEqual(records[-1]["status"], "HALT")
        self.assertEqual(records[-1]["action"], "ENTRY_STALE_CANCEL")


class FlattenVerificationTests(unittest.TestCase):
    """order.py --flatten の成否は応答の分類ではなく送信後の再照会で決める。"""

    def test_waits_until_orders_disappear(self):
        views = [pending_orders(), pending_orders(), empty_orders()]
        slept = []
        verified, failed = order._flatten_verified(
            [ACC], "MNQU6", query_position=lambda s, account=None: flat_position(),
            query_orders=lambda s, account=None: views.pop(0), attempts=8, delay=0.25,
            sleep=lambda d: slept.append(d))
        self.assertEqual((verified, failed), ([ACC], []))
        self.assertEqual(slept, [0.25, 0.25])

    def test_gives_up_when_orders_stay(self):
        verified, failed = order._flatten_verified(
            [ACC], "MNQU6", query_position=lambda s, account=None: flat_position(),
            query_orders=lambda s, account=None: pending_orders(), attempts=3, delay=0.0,
            sleep=lambda d: None)
        self.assertEqual((verified, failed), ([], [ACC]))

    def test_query_failure_is_not_success(self):
        def boom(s, account=None):
            raise RuntimeError("down")
        verified, failed = order._flatten_verified(
            [ACC], "MNQU6", query_position=boom, query_orders=boom, attempts=2, delay=0.0,
            sleep=lambda d: None)
        self.assertEqual((verified, failed), ([], [ACC]))

    def test_open_position_is_not_flat(self):
        verified, failed = order._flatten_verified(
            [ACC], "MNQU6",
            query_position=lambda s, account=None: {**flat_position(), "qty": 3, "side": "SHORT"},
            query_orders=lambda s, account=None: empty_orders(), attempts=2, delay=0.0,
            sleep=lambda d: None)
        self.assertEqual((verified, failed), ([], [ACC]))


class RestingEntryTerminalTests(unittest.TestCase):
    """ENTRY_RESTING の脚が全部終端して FLAT なら終端記録し、経路を進める。"""

    def _orders(self, p1, p2):
        rows = [{"orderId": "P1", "status": p1, "action": "SELL", "parentId": None},
                {"orderId": "P2", "status": p2, "action": "SELL", "parentId": None}]
        active = [r for r in rows if r["status"] in ("WORKING", "SUSPENDED")]
        terminal = [r for r in rows if r not in active]
        state = "PENDING" if active else (terminal[-1]["status"] if terminal else "NONE")
        return {"verified": True, "symbol": "MNQU6", "state": state, "openCount": len(active),
                "orders": rows, "activeOrders": active, "terminalOrders": terminal,
                "orderIds": [r["orderId"] for r in active],
                "filledOrderIds": [r["orderId"] for r in rows if r["status"] == "FILLED"],
                "accountScope": [ACC]}

    def _run(self, p1, p2):
        h = Harness()
        h.cfg["NQX_STALE_ENTRY_CANCEL"] = "0"
        h.orders = lambda _symbol, **_kwargs: self._orders(p1, p2)
        sent_claim = {**copy.deepcopy(CLAIM), "routeState": "SENT", "acceptedCount": 2,
                      "routeSnapshot": PLAN["routeSnapshot"]}
        with tempfile.TemporaryDirectory() as tmp:
            ledger = os.path.join(tmp, "l.jsonl")
            ae._append_ledger({"key": KEY, "entryKey": KEY, "status": "ENTRY_CLAIMED", "action": "ENTRY_CLAIM",
                               "plan": copy.deepcopy(PLAN), "claimJournal": JOURNAL, "time": SENT_AT}, ledger)
            ae._append_ledger({"key": KEY, "entryKey": KEY, "status": "ENTRY_RESTING", "action": "ENTRY",
                               "routeState": "SENT", "plan": copy.deepcopy(PLAN), "claimJournal": JOURNAL}, ledger)
            notes = ae.reconcile(bundle_with_low(29520.0, price=29540.0), False, h.cfg, h.position, h.runner,
                                 ledger, broker_order_query=h.orders,
                                 state_query=lambda: {"entryClaim": sent_claim}, recover_entry=h.recover)
            records, _ = ae._read_ledger(ledger)
        return notes, records, h

    def test_all_legs_terminal_while_flat_is_terminalized(self):
        notes, records, h = self._run("FILLED", "FILLED")
        self.assertTrue(any("terminalized" in n and "P1=FILLED" in n for n in notes), notes)
        self.assertEqual(records[-1]["status"], "ENTRY_HALTED")
        self.assertEqual(records[-1]["action"], "ENTRY_TERMINAL")
        self.assertEqual(h.calls, [], "終端は記録だけで、何も送らない")

    def test_mixed_filled_and_canceled_is_also_terminal(self):
        notes, records, _ = self._run("FILLED", "CANCELED")
        self.assertEqual(records[-1]["status"], "ENTRY_HALTED")

    def test_a_live_leg_keeps_waiting(self):
        notes, records, _ = self._run("FILLED", "WORKING")
        self.assertTrue(any("awaiting verified fill" in n for n in notes), notes)
        self.assertFalse(any(r.get("status") == "ENTRY_HALTED" for r in records))


if __name__ == "__main__":
    unittest.main(verbosity=1)

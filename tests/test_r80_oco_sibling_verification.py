# -*- coding: utf-8 -*-
"""R80: 張り替え(cancelandbracket)後の保護注文は「建玉に対する OCO 兄弟」であることを構造で証明する。

2026-09-12 01:53 JST、TP1 後の建値移動が送った cancelandbracket の結果を、ユーザーが
ブローカー UI で「SL が TP(Buy Limit)の子ブラケットとして付いている」と読んだ。子ブラケットは
親の約定まで起動しないので、その読みが正しければ runner は SL 無しである。

事実(docs/R80_OCO_SIBLING_VERIFICATION.md):
  * CrossTrade 公式: CANCELANDBRACKET は「建玉の周りに OCO 対を置く」。action = 保護する建玉の側。
  * ブローカー行: 張り替えの 2 本は相互 `ocoId` のみ(`parentId` 無し)= OCO 兄弟の形。
    OSO の子は `parentId` を持ち、親の約定まで `Suspended`。
  * 従来の照合は `parentId` に潰したリンクが相互/同一なら通し、SUSPENDED も active に数えて
    いたので、OSO の子と OCO の兄弟を区別できなかった。

固定する線引き:
  1. 正規化は生の `ocoId` / `parentId` / `linkedId` を `brokerOcoId` / `brokerParentId` /
     `brokerLinkedId` として残す
  2. 判定器は broker_status.oco_sibling_pair() 1 か所: ちょうど 2 本・live(SUSPENDED 不可)・
     親なし・相互 ocoId・子ブラケットなし
  3. 束縛(bind_replacement_bracket)・照合(verify_protective_orders)・settle(_protective_settled)・
     engine の修復(_protective_rows)が同じ判定器/状態集合を使う
  4. 送信ペイロード(command=cancelandbracket; action=<建玉の側>; stop_loss; take_profit)は
     文書化された契約のままで変えない

    python tests/test_r80_oco_sibling_verification.py
"""
import copy
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import patch

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import autotrade_engine as ae  # noqa: E402
ae._read_env_file = lambda path=None: {}          # 本番 .secrets から隔離
import broker_status  # noqa: E402
import management_intent  # noqa: E402
import nqx_state  # noqa: E402
import order  # noqa: E402
import route_identity  # noqa: E402

ACC = "LFF-TEST"
SYM = "MNQU6"


def raw_row(order_id, action, status, oco=None, parent=None, linked=None,
            ts="2026-09-11T16:52:56.051Z"):
    """CrossTrade REST が返す Tradovate の生の注文行(2026-09-12 実測の形。id は架空)。"""
    row = {"accountId": 64968904, "action": action, "admin": False, "archived": False,
           "contractId": 4399654, "executionProviderId": 14, "external": False,
           "id": order_id, "ordStatus": status, "timestamp": ts}
    if oco is not None:
        row["ocoId"] = oco
    if parent is not None:
        row["parentId"] = parent
    if linked is not None:
        row["linkedId"] = linked
    return row


def trow(order_id, status, action, oco=None, parent=None, account=ACC, symbol=SYM):
    """正規化後の行(数量・種別・価格なし)。oco = OCO の相方、parent = OSO の真の親。"""
    return {"orderId": order_id, "accountId": account, "symbol": symbol, "status": status,
            "action": action, "qty": None, "orderType": None, "limitPrice": None,
            "stopPrice": None, "filledPrice": None, "fieldsComplete": False,
            "parentId": oco or parent, "brokerOcoId": oco, "brokerParentId": parent,
            "receipt": f"TRADOVATE:{account}:{order_id}", "receiptSource": "derived"}


def view(*rows):
    rows = list(rows)
    return {"verified": True, "platform": "TRADOVATE", "symbol": SYM, "orders": rows,
            "activeOrders": [r for r in rows if r["status"] in broker_status.BROKER_ACTIVE_STATES],
            "state": "PENDING" if rows else "NONE"}


def oco_pair(a="N-1", b="N-2", action="BUY", status="WORKING"):
    return trow(a, status, action, oco=b), trow(b, status, action, oco=a)


def oso_child_pair(parent_id="N-1", child_id="N-2", action="BUY", child_status="SUSPENDED"):
    """TP(親)に SL が子ブラケットとして付いた形。子は親の約定まで SUSPENDED。"""
    return trow(parent_id, "WORKING", action), trow(child_id, child_status, action, parent=parent_id)


# ---------------------------------------------------------------- 1. 正規化

class NormalizationTests(unittest.TestCase):
    def normalize(self, *rows):
        return broker_status.normalize_crosstrade_orders(
            {"success": True, "data": list(rows)}, platform="TRADOVATE", account=ACC, symbol=SYM,
            account_aliases=["64968904"], contract_ids=["4399654"])

    def test_raw_links_are_kept_separately(self):
        normalized = self.normalize(
            raw_row(960, "Sell", "Filled", linked=961),            # ENTRY(OSO の親)
            raw_row(961, "Buy", "Filled", oco=962, parent=960),    # OSO の子 1(真の親を名乗る)
            raw_row(962, "Buy", "Canceled", oco=961),              # 子 1 の OCO 相方
            raw_row(2014, "Buy", "Working", oco=2015),             # cancelandbracket の対
            raw_row(2015, "Buy", "Working", oco=2014))
        by_id = {row["orderId"]: row for row in normalized["orders"]}
        self.assertEqual(by_id["960"]["brokerLinkedId"], "961")
        self.assertIsNone(by_id["960"]["brokerOcoId"])
        self.assertEqual((by_id["961"]["brokerParentId"], by_id["961"]["brokerOcoId"]), ("960", "962"))
        self.assertEqual((by_id["962"]["brokerParentId"], by_id["962"]["brokerOcoId"]), (None, "961"))
        self.assertEqual((by_id["2014"]["brokerParentId"], by_id["2014"]["brokerOcoId"]), (None, "2015"))
        self.assertEqual((by_id["2015"]["brokerParentId"], by_id["2015"]["brokerOcoId"]), (None, "2014"))
        # 互換の parentId(潰したリンク)は従来どおり ocoId を優先して埋まる
        self.assertEqual(str(by_id["2014"]["parentId"]), "2015")

    def test_real_replacement_rows_are_oco_siblings_and_entry_children_are_not(self):
        normalized = self.normalize(
            raw_row(961, "Buy", "Working", oco=962, parent=960), raw_row(962, "Buy", "Working", oco=961),
            raw_row(2014, "Buy", "Working", oco=2015), raw_row(2015, "Buy", "Working", oco=2014))
        rows = normalized["orders"]
        replacement = [row for row in rows if row["orderId"] in {"2014", "2015"}]
        pair, reason = broker_status.oco_sibling_pair(replacement, account=ACC, expected_action="BUY",
                                                      symbol=SYM, all_rows=replacement)
        self.assertIsNotNone(pair, reason)
        self.assertEqual([row["orderId"] for row in pair], ["2014", "2015"])
        children = [row for row in rows if row["orderId"] in {"961", "962"}]
        pair, reason = broker_status.oco_sibling_pair(children, account=ACC, expected_action="BUY",
                                                      symbol=SYM, all_rows=children)
        self.assertIsNone(pair)
        self.assertIn("bracket child of order 960", reason)


# ---------------------------------------------------------------- 2. 判定器

class OcoSiblingPairTests(unittest.TestCase):
    def pair(self, rows, all_rows=None, action="BUY"):
        return broker_status.oco_sibling_pair(rows, account=ACC, expected_action=action, symbol=SYM,
                                              all_rows=all_rows)

    def test_mutual_oco_working_pair_is_accepted(self):
        pair, reason = self.pair(list(oco_pair()))
        self.assertIsNone(reason)
        self.assertEqual([row["orderId"] for row in pair], ["N-1", "N-2"])

    def test_suspended_child_of_the_take_profit_is_rejected(self):
        pair, reason = self.pair(list(oso_child_pair()))
        self.assertIsNone(pair)
        self.assertIn("SUSPENDED", reason)
        self.assertIn("not live", reason)

    def test_working_child_of_the_take_profit_is_rejected(self):
        pair, reason = self.pair(list(oso_child_pair(child_status="WORKING")))
        self.assertIsNone(pair)
        self.assertIn("bracket child of order N-1", reason)

    def test_legacy_parent_only_link_is_not_an_oco_link(self):
        legacy = [{**trow("N-1", "WORKING", "BUY"), "parentId": "N-2"},
                  {**trow("N-2", "WORKING", "BUY"), "parentId": "N-1"}]
        pair, reason = self.pair(legacy)
        self.assertIsNone(pair)
        self.assertIn("ocoId", reason)

    def test_one_directional_link_is_rejected(self):
        rows = [trow("N-1", "WORKING", "BUY", oco="N-2"), trow("N-2", "WORKING", "BUY", oco="N-9")]
        pair, reason = self.pair(rows)
        self.assertIsNone(pair)
        self.assertIn("not mutual", reason)

    def test_counts_and_scope(self):
        three = list(oco_pair()) + [trow("N-3", "WORKING", "BUY")]
        self.assertIn("found 3", self.pair(three)[1])
        one = [trow("N-1", "WORKING", "BUY", oco="N-2")]
        self.assertIn("found 1", self.pair(one)[1])
        other_account = [trow("N-1", "WORKING", "BUY", oco="N-2", account="OTHER"),
                         trow("N-2", "WORKING", "BUY", oco="N-1", account="OTHER")]
        self.assertIn("found 0", self.pair(other_account)[1])
        same_direction = list(oco_pair(action="SELL"))
        self.assertIn("found 0", self.pair(same_direction)[1])
        # 終端の行は数えない(古い対が Canceled で残っていても邪魔しない)
        with_terminal = list(oco_pair()) + list(oco_pair("O-1", "O-2", status="CANCELED"))
        self.assertIsNotNone(self.pair(with_terminal)[0])

    def test_pending_is_not_live_yet(self):
        pair, reason = self.pair(list(oco_pair(status="PENDING")))
        self.assertIsNone(pair)
        self.assertIn("PENDING", reason)

    def test_attached_child_anywhere_in_the_account_rejects_the_pair(self):
        pair_rows = list(oco_pair())
        # 対そのものは正しいが、口座の別行が対の片方を親として名乗る = 張り替え注文に子が付いている
        child = trow("N-3", "SUSPENDED", "SELL", parent="N-1")
        pair, reason = self.pair(pair_rows, all_rows=pair_rows + [child])
        self.assertIsNone(pair)
        self.assertIn("bracket child attached to replacement order N-1", reason)
        # 終端の子は無視する
        gone = trow("N-3", "CANCELED", "SELL", parent="N-1")
        self.assertIsNotNone(self.pair(pair_rows, all_rows=pair_rows + [gone])[0])

    def test_unknown_action_is_rejected(self):
        self.assertIn("unknown", broker_status.oco_sibling_pair(list(oco_pair()), account=ACC,
                                                                expected_action=None)[1])


# ---------------------------------------------------------------- 3. 束縛と照合

class BindReplacementTests(unittest.TestCase):
    def bind(self, before, after):
        return route_identity.bind_replacement_bracket(before, after, account=ACC, symbol=SYM, action="BUY",
                                                       qty=1, stop=29483.25, target=29081.75,
                                                       platform="TRADOVATE")

    def test_new_oco_siblings_bind_with_structure_tag(self):
        bound = self.bind(view(*oco_pair("O-1", "O-2")), view(*oco_pair()))
        self.assertIsNotNone(bound)
        self.assertEqual((bound["stopOrderId"], bound["targetOrderId"]), ("N-1", "N-2"))
        self.assertEqual(bound["structure"], "OCO_SIBLINGS")
        self.assertIs(bound["pricesConfirmed"], False)
        self.assertTrue(bound["fieldsIncomplete"])
        self.assertEqual(bound["ocoGroupId"], "N-1+N-2")

    def test_oso_child_structure_does_not_bind(self):
        self.assertIsNone(self.bind(view(), view(*oso_child_pair())))
        self.assertIsNone(self.bind(view(), view(*oso_child_pair(child_status="WORKING"))))

    def test_legacy_parent_only_pair_does_not_bind(self):
        legacy = [{**trow("N-1", "WORKING", "BUY"), "parentId": "N-2"},
                  {**trow("N-2", "WORKING", "BUY"), "parentId": "N-1"}]
        self.assertIsNone(self.bind(view(), view(*legacy)))

    def test_child_attached_to_the_new_pair_does_not_bind(self):
        after = view(*oco_pair(), trow("N-3", "SUSPENDED", "SELL", parent="N-1"))
        self.assertIsNone(self.bind(view(), after))


def position(qty=1, side="SHORT", avg=29484.25):
    return {"verified": True, "accountId": ACC, "account": ACC, "symbol": SYM, "side": side, "qty": qty,
            "avgEntry": avg, "filledAt": "2026-09-11T16:30:03.836Z", "orderId": 651929061676,
            "receipt": "TRADOVATE:LFF-TEST:651929061676", "initialQty": qty}


def receipt(a="N-1", b="N-2"):
    return {"accountId": ACC, "stopOrderId": a, "targetOrderId": b, "ocoGroupId": f"{a}+{b}",
            "stopReceipt": f"TRADOVATE:{ACC}:{a}", "targetReceipt": f"TRADOVATE:{ACC}:{b}",
            "structure": "OCO_SIBLINGS", "pricesConfirmed": False}


class VerifyProtectiveStructuralTests(unittest.TestCase):
    def verify(self, order_view, rcpt=None, **overrides):
        pos = position()
        kwargs = dict(side="SELL", position_generation=broker_status.position_identity(pos),
                      position_before=pos, current_position=pos,
                      route_receipt={"accounts": [rcpt or receipt()]})
        kwargs.update(overrides)
        return broker_status.verify_protective_orders(order_view, [ACC], 1, 29483.25, 29081.75, **kwargs)

    def test_oco_siblings_verify_and_say_prices_are_not_confirmed(self):
        ok, detail = self.verify(view(*oco_pair()))
        self.assertTrue(ok, detail)
        self.assertIn("OCO sibling", detail)
        self.assertIn("prices not confirmed", detail)

    def test_child_bracket_structure_fails_closed(self):
        ok, detail = self.verify(view(*oso_child_pair()))
        self.assertFalse(ok)
        self.assertIn("not an OCO sibling pair", detail)
        self.assertIn("SUSPENDED", detail)
        ok, detail = self.verify(view(*oso_child_pair(child_status="WORKING")))
        self.assertFalse(ok)
        self.assertIn("bracket child of order N-1", detail)

    def test_legacy_parent_link_and_missing_side_fail_closed(self):
        legacy = [{**trow("N-1", "WORKING", "BUY"), "parentId": "N-2"},
                  {**trow("N-2", "WORKING", "BUY"), "parentId": "N-1"}]
        ok, detail = self.verify(view(*legacy))
        self.assertFalse(ok)
        self.assertIn("ocoId", detail)
        ok, detail = self.verify(view(*oco_pair()), side=None)
        self.assertFalse(ok)
        self.assertIn("side is required", detail)

    def test_identity_and_receipt_still_must_match(self):
        self.assertFalse(self.verify(view(*oco_pair()), receipt("N-1", "N-9"))[0])
        swapped = {**receipt(), "stopReceipt": f"TRADOVATE:{ACC}:N-9"}
        self.assertFalse(self.verify(view(*oco_pair()), swapped)[0])

    def test_child_attached_in_orders_but_not_active_scope(self):
        # activeOrders は 2 本だが orders に SUSPENDED の子が居る = 対に子が付いている
        rows = list(oco_pair())
        child = trow("N-3", "SUSPENDED", "SELL", parent="N-1")
        order_view = {**view(*rows), "orders": rows + [child], "activeOrders": rows}
        ok, detail = self.verify(order_view)
        self.assertFalse(ok)
        self.assertIn("bracket child attached", detail)


# ---------------------------------------------------------------- 4. settle と engine

class ProtectiveSettleTests(unittest.TestCase):
    def run_settle(self, sequence, attempts):
        calls, slept = [], []
        last = sequence[-1]
        with patch.object(order.broker_status, "query_orders",
                          side_effect=lambda symbol, known_order_ids=None, account=None: (
                              calls.append(account) or (sequence.pop(0) if sequence else last))):
            out = StringIO()
            with redirect_stdout(out):
                settled = order._protective_settled(SYM, ACC, ["N-1", "N-2"], "BUY", attempts=attempts,
                                                    sleep=lambda s: slept.append(s))
        return settled, calls, slept, out.getvalue()

    def test_waits_until_the_pair_is_oco_siblings(self):
        pending = view(*oco_pair(status="PENDING"))
        child = view(*oso_child_pair())
        good = view(*oco_pair())
        settled, calls, slept, out = self.run_settle([pending, child, good], attempts=8)
        self.assertIs(settled, good)
        self.assertEqual((len(calls), len(slept)), (3, 2))
        self.assertNotIn("protective settle:", out)

    def test_exhaustion_prints_the_last_reason(self):
        child = view(*oso_child_pair())
        settled, calls, slept, out = self.run_settle([child, child, child], attempts=3)
        self.assertIs(settled, child)
        self.assertEqual((len(calls), len(slept)), (3, 2))
        self.assertIn("protective settle:", out)
        self.assertIn("SUSPENDED", out)


CFG = {"NQX_AUTOTRADE_KILL": "0"}


def buy_plan():
    return {"scenarioId": "S-R80", "entryKey": "ENTRY:" + "b" * 64, "symbol": SYM, "side": "BUY",
            "qty": 2, "entry": 100.0, "initialStop": 90.0, "tp1": 110.0, "finalTarget": 130.0,
            "targets": [110.0, 130.0], "trailDistance": 5.0, "accountScope": [ACC],
            "legs": [{"id": "TP1", "qty": 1, "target": 110.0}, {"id": "RUNNER", "qty": 1, "target": 130.0}],
            "legIdentityKnown": True, "planVersion": "R19-ICT-SPLIT-1", "entryOrderType": "LIMIT"}


def buy_position(qty=1):
    return {"verified": True, "side": "LONG", "qty": qty, "avgEntry": 100.0, "accountId": ACC,
            "account": ACC, "symbol": SYM, "filledAt": "2026-09-11T16:30:03.836Z", "orderId": None,
            "receipt": None, "initialQty": qty}


def claim_stub(intent):
    return True, {"managementKey": management_intent.management_key(intent), "claimToken": "T" * 43,
                  "managementIntentHash": management_intent.intent_hash(intent)}


class EngineProtectiveRowsTests(unittest.TestCase):
    def test_suspended_rows_are_not_protection(self):
        rows = ae._protective_rows(view(*oso_child_pair(action="SELL")), "BUY", ACC)
        self.assertEqual([row["orderId"] for row in rows], ["N-1"])
        rows = ae._protective_rows(view(*oco_pair(action="SELL")), "BUY", ACC)
        self.assertEqual(len(rows), 2)

    def test_repair_treats_tp_plus_suspended_child_as_unprotected(self):
        tmp = tempfile.mkdtemp(prefix="nqx-r80-")
        ledger = os.path.join(tmp, "ledger.jsonl")
        calls = []

        def runner(args, live):
            calls.append((list(args), bool(live)))
            return 0, "stub dry-run" if not live else "MODIFY VERIFIED: OCO sibling"

        try:
            handled, notes, prefix = ae._repair_unprotected_runner(
                plan=buy_plan(), position=buy_position(),
                action={"action": "MODIFY", "qty": 1, "sl": 114.0, "tp": 130.0},
                failure="live send failed/unknown: MODIFY_POSTVERIFY_UNKNOWN", failed_key="K:FAILED",
                plan_key="K", failed_journal=None, previous_stop=90.0, price=115.0, cfg=CFG, symbol=SYM,
                account=ACC, open_qty=1, query=lambda _s: buy_position(),
                broker_order_query=lambda _s, known_order_ids=None: view(*oso_child_pair(action="SELL")),
                claimer=claim_stub, execute=runner, ledger_path=ledger)
            self.assertTrue(handled, (handled, notes, prefix))
            self.assertIn("repair management_sent", notes[0])
            live_modifies = [args for args, live in calls if "--modify" in args and live]
            self.assertEqual(live_modifies[0][live_modifies[0].index("--sl") + 1], "90.0")
            with open(ledger, encoding="utf-8") as fh:
                rows = [json.loads(line) for line in fh]
            self.assertTrue(any(r.get("status") == "MODIFY_FAILED_REPAIRED" for r in rows))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- 5. order.py --modify --confirm の往復

ORDER_CFG = {"CROSSTRADE_URL": "http://127.0.0.1:9/blocked", "CROSSTRADE_KEY": "TEST",
             "CROSSTRADE_DEST": "tradovate", "CROSSTRADE_ACCOUNTS": ACC, "MAX_RISK_DOLLARS": "200"}
POSITION = position()
GENERATION = broker_status.position_identity(POSITION)
INTENT = management_intent.build(account_id=ACC, symbol=SYM, position_generation=GENERATION,
                                 side="sell", qty=1, stop=29483.25, target=29081.75)
ARGV = ["order.py", "--modify", "--confirm", "--side", "sell", "--qty", "1",
        "--sl", "29483.25", "--tp", "29081.75", "--symbol", SYM, "--account", ACC,
        "--last", "29420.75", "--position-generation", GENERATION,
        f"--management-key={management_intent.management_key(INTENT)}",
        f"--management-token={'T' * 43}",
        f"--management-intent-hash={management_intent.intent_hash(INTENT)}"]


class OrderModifyRoundTripTests(unittest.TestCase):
    """in-process で order.main() を回す。HTTP と Durable Object は差し替え、外部送信は無い。"""

    def run_modify(self, order_views):
        posted, resolved = [], []
        views = list(order_views)
        last = views[-1]

        def fake_post(cfg, lines, label):
            posted.append(list(lines))
            return 200, '{"success": true}'

        def fake_orders(symbol, known_order_ids=None, account=None):
            return copy.deepcopy(views.pop(0) if views else last)

        with patch.object(sys, "argv", ARGV), \
                patch.dict(os.environ, {"NQX_MODIFY_FRESH_QUOTE": "0"}), \
                patch.object(order, "load_env", return_value=dict(ORDER_CFG)), \
                patch.object(order, "load_log", return_value={}), \
                patch.object(order, "post", side_effect=fake_post), \
                patch.object(order.time, "sleep", lambda _s: None), \
                patch.object(order.broker_status, "query_position",
                             side_effect=lambda symbol, account=None: copy.deepcopy(POSITION)), \
                patch.object(order.broker_status, "query_orders", side_effect=fake_orders), \
                patch.object(nqx_state, "consume_management_claim", return_value=(True, "")), \
                patch.object(nqx_state, "resolve_management_claim",
                             side_effect=lambda key, token, state, receipt=None, **_kw: (
                                 resolved.append((state, receipt)) or (True, {}))):
            out = StringIO()
            try:
                with redirect_stdout(out):
                    order.main()
            except SystemExit as exc:
                code = exc.code
            else:
                code = 0
        return code, out.getvalue(), posted, resolved

    def test_payload_is_the_documented_contract_and_oco_siblings_verify(self):
        old = view(*oco_pair("O-1", "O-2"))
        new = view(*oco_pair())
        code, out, posted, resolved = self.run_modify([old, new])
        self.assertEqual(code, 0, out[-600:])
        self.assertIn("MODIFY VERIFIED", out)
        self.assertIn("OCO sibling", out)
        self.assertIn("prices not confirmed", out)
        self.assertEqual(len(posted), 1, "cancelandbracket は 1 回だけ")
        lines = posted[0]
        # CrossTrade 公式: action は保護する建玉の側(SHORT → SELL)、両脚は同じ建玉の OCO 対
        self.assertIn("command=cancelandbracket;", lines)
        self.assertIn("action=SELL;", lines)
        self.assertIn("qty=1;", lines)
        self.assertIn("stop_loss=29483.25;", lines)
        self.assertIn("take_profit=29081.75;", lines)
        self.assertFalse(any(line.startswith(("flatten_first", "order_type", "limit_price")) for line in lines))
        self.assertEqual([state for state, _ in resolved], ["SENT"])
        frozen = resolved[0][1]["accounts"][0]
        self.assertEqual((frozen["stopOrderId"], frozen["targetOrderId"]), ("N-1", "N-2"))
        self.assertEqual(frozen["structure"], "OCO_SIBLINGS")
        self.assertIs(frozen["pricesConfirmed"], False)

    def test_child_bracket_structure_is_unknown_not_success(self):
        old = view(*oco_pair("O-1", "O-2"))
        bad = view(*oso_child_pair())
        code, out, posted, resolved = self.run_modify([old, bad])
        self.assertNotEqual(code, 0)
        self.assertIn("MODIFY_ROUTE_UNKNOWN", str(code))
        self.assertNotIn("MODIFY VERIFIED", out)
        self.assertEqual(len(posted), 1, "再送しない")
        self.assertEqual([state for state, _ in resolved], ["UNKNOWN"])

    def test_child_appearing_after_binding_fails_postverify(self):
        old = view(*oco_pair("O-1", "O-2"))
        new = view(*oco_pair())
        later = view(*oco_pair(), trow("N-3", "SUSPENDED", "BUY", parent="N-1"))
        code, out, posted, resolved = self.run_modify([old, new, later])
        self.assertNotEqual(code, 0)
        self.assertIn("MODIFY_POSTVERIFY_UNKNOWN", str(code))
        self.assertIn("protective settle:", out)
        self.assertEqual(len(posted), 1, "再送しない")
        self.assertEqual([state for state, _ in resolved], ["UNKNOWN"])


if __name__ == "__main__":
    unittest.main(verbosity=1)

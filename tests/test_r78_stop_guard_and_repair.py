# -*- coding: utf-8 -*-
"""R78: TP1 後の張り替えで runner が裸になる事故の再発防止と、約定の高速検知。

2026-09-11 01:40:48 実測(LFF…0006、SHORT 4 → TP1 で 2 決済 → runner 2):
  * engine は 3 分前の価格 29,171 に対して stop 29,174.25(安値 29,146.25 + トレール 28)を
    「1 tick 上なので protective」と判定 → 送信時には価格が 29,174.25 以上 → BUY STOP 拒否
  * cancelandbracket は旧 SL 29,301 の取消だけ成立 → TP 指値だけ残り SHORT 2 枚が SL 無し
  * order.py の ULTRA ガード(逆方向 active 行 == 2)が、裸(1 行)を直す修復 modify も拒否

固定する線引き:
  1. protective 判定は 1 tick ではなく市場側バッファ(既定 2pt、NQX_STOP_MARKET_BUFFER_PT)
  2. 現在値は極値の候補(fill_watch から呼ばれると足は最大 3 分古い)
  3. order.py は送信直前の参照価格(quote / --last)で stop 側を再検査し、何も取り消す前に
     MODIFY_STOP_NOT_PROTECTIVE で止まる。engine はそれを HALT ではなく見送りにする
  4. ULTRA ガードは 2 本超だけ拒否(0〜1 本は修復として通す)
  5. 片脚拒否で保護行が 2 本未満なら、同じ周期で直前の stop へ戻す MODIFY を送る。
     直前の stop すら守れない側なら FLATTEN。成功時、元の失敗行は HALT ではない
  6. reconcile はプロセス間ロックで排他する(3 分ループと fill_watch)
  7. fill_watch は枚数変化だけで動き、scenario 無しの bundle で reconcile を呼ぶ
     (新規 ENTRY は出ない)。束縛だけで終わった周期は続けて管理を回す

    python tests/test_r78_stop_guard_and_repair.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import autotrade_engine as ae  # noqa: E402
ae._read_env_file = lambda path=None: {}          # 本番 .secrets から隔離
import fill_watch  # noqa: E402
import management_intent  # noqa: E402
import route_envelope  # noqa: E402
from _hermetic import make_sandbox  # noqa: E402

FAILED = []


def check(name, condition, detail=""):
    print(("  OK   " if condition else "  FAIL ") + name
          + (f" ({detail})" if detail and not condition else ""))
    if not condition:
        FAILED.append(name)


NOW = datetime.now(timezone.utc)
FILLED_AT = NOW - timedelta(minutes=10)
ACC = "LFF-TEST"
SYM = "MNQU6"
CFG = {"NQX_AUTOTRADE_KILL": "0"}


# ---------------------------------------------------------------- 1. management_action

def sell_plan():
    return {"symbol": SYM, "side": "SELL", "qty": 4, "entry": 29263.75, "initialStop": 29301.0,
            "tp1": 29141.75, "finalTarget": 29042.5, "trailDistance": 28.0,
            "legs": [{"id": "TP1", "qty": 2, "target": 29141.75},
                     {"id": "RUNNER", "qty": 2, "target": 29042.5}],
            "legIdentityKnown": True, "ultra": True}


def sell_position(qty=2):
    return {"side": "SHORT", "qty": qty, "avgEntry": 29263.75, "accountId": ACC, "symbol": SYM,
            "filledAt": FILLED_AT.isoformat()}


def bundle(price, low, high=None):
    t0 = int(FILLED_AT.timestamp()) + 60
    return {"price": price, "snapshot": {"bars3m": [
        {"t": t0, "o": price, "h": high or price + 5, "l": low, "c": price},
        {"t": t0 + 180, "o": price, "h": price + 2, "l": price - 2, "c": price},
    ]}}


def test_buffer_rejects_the_real_case():
    # 実測: 安値 29,146.25 + 28 = 29,174.25、判断時の価格 29,171.25(3pt 下)→ 旧判定では
    # protective(1 tick)。送信までの 2.5 分で価格が 3pt 以上動いて拒否された。
    act = ae.management_action(sell_plan(), sell_position(), bundle(29171.25, 29146.25), {}, CFG)
    check("市場から 3pt の stop はトレールとして採用しない(バッファ 4pt)",
          act is not None and act["sl"] != 29174.25, act)
    check("代わりに建値床(29,262.75)へ寄せる", act is not None and act["sl"] == 29262.75, act)
    # 価格が安値付近にあれば(TP1 直後に数秒で検知した状況)トレールは通る
    act = ae.management_action(sell_plan(), sell_position(), bundle(29150.0, 29146.25), {}, CFG)
    check("価格が安値付近ならトレール 29,174.25 を出す", act is not None and act["sl"] == 29174.25, act)
    # バッファは環境で可逆
    act = ae.management_action(sell_plan(), sell_position(), bundle(29171.25, 29146.25), {},
                               {**CFG, "NQX_STOP_MARKET_BUFFER_PT": "0.25"})
    check("NQX_STOP_MARKET_BUFFER_PT=0.25 なら旧挙動(1 tick)に戻る", act is not None and act["sl"] == 29174.25, act)
    check("既定バッファは 4pt", ae._stop_market_buffer({}) == 4.0, ae._stop_market_buffer({}))


def test_current_price_is_an_extreme_candidate():
    # 足の安値は 29,160 だが現在値 29,150 の方が低い → 極値は現在値
    t0 = int(FILLED_AT.timestamp()) + 60
    b = {"price": 29150.0, "snapshot": {"bars3m": [{"t": t0, "o": 29165, "h": 29170, "l": 29160, "c": 29165}]}}
    value = ae._extreme(b, "SELL", FILLED_AT)
    check("現在値が足の安値より低ければ極値に採る", value == 29150.0, value)
    value = ae._extreme({"price": 29150.0, "snapshot": {"bars3m": []}}, "SELL", FILLED_AT)
    check("足が無くても現在値だけで極値になる", value == 29150.0, value)


# ---------------------------------------------------------------- 2. order.py の送信直前ガード

def test_order_stop_side_guard_dry_run():
    sandbox = make_sandbox(BASE)
    try:
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1", NQX_MODIFY_FRESH_QUOTE="0")
        base_args = ["--modify", "--side", "sell", "--qty", "2", "--tp", "29042.5",
                     "--ultra", "--position-generation", "gen-1", "--account", "TEST"]

        def run(extra):
            proc = subprocess.run([sys.executable, os.path.join(sandbox, "order.py")] + base_args + extra,
                                  capture_output=True, text=True, encoding="utf-8", errors="replace",
                                  cwd=sandbox, env=env, timeout=60)
            return proc.returncode, (proc.stdout or "") + (proc.stderr or "")

        code, out = run(["--sl", "29174.25", "--last", "29174.25"])
        check("現在値と同じ stop はドライランで MODIFY_STOP_NOT_PROTECTIVE", code != 0 and "MODIFY_STOP_NOT_PROTECTIVE" in out, out[-200:])
        check("拒否時に外部へ送信しない", "SNAPSHOT" not in out and "HTTP" not in out, out[-200:])
        code, out = run(["--sl", "29175.0", "--last", "29174.25"])
        check("バッファ未満(0.75pt)も拒否", code != 0 and "MODIFY_STOP_NOT_PROTECTIVE" in out, out[-200:])
        code, out = run(["--sl", "29177.25", "--last", "29174.25"])
        check("バッファ未満(3pt = 今夜の実測差)も拒否", code != 0 and "MODIFY_STOP_NOT_PROTECTIVE" in out, out[-200:])
        code, out = run(["--sl", "29178.25", "--last", "29174.25"])
        check("バッファちょうど(4pt)は通る", code == 0 and "stop-side guard" in out and "OK" in out, out[-300:])
        code, out = run(["--sl", "29174.25"])
        check("参照価格が無ければ従来どおり省略(ドライラン成功)", code == 0 and "検査を省略" in out, out[-300:])
        code, out = run(["--sl", "29174.25", "--last", "29174.25", "--confirm"])
        check("--confirm でも取消前に止まる", code != 0 and "MODIFY_STOP_NOT_PROTECTIVE" in out
              and "cancelandbracket" not in out.split("MODIFY_STOP_NOT_PROTECTIVE")[0], out[-300:])
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)


STUB_BROKER = '''# -*- coding: utf-8 -*-
import os
# R80: route_identity は import 時に broker_status の状態集合を参照する。スタブも同じ表面を持つ。
BROKER_ACTIVE_STATES = {"WORKING", "ACCEPTED", "SUBMITTED", "PENDING", "PENDING_SUBMIT",
                        "SUSPENDED", "PARTIALLY_FILLED", "PARTIAL_FILL"}
BROKER_TERMINAL_STATES = {"FILLED", "CANCELED", "REJECTED", "EXPIRED"}
BROKER_LIVE_PROTECTIVE_STATES = {"WORKING", "ACCEPTED", "SUBMITTED", "PARTIALLY_FILLED", "PARTIAL_FILL"}
def _rows():
    n = int(os.environ.get("STUB_PROTECTIVE_ROWS", "2"))
    return [{"orderId": f"B-{i}", "accountId": "TEST", "symbol": "MNQU6", "status": "WORKING",
             "action": "BUY", "qty": None, "orderType": None, "limitPrice": None,
             "fieldsComplete": False, "parentId": None, "receipt": f"R-{i}"} for i in range(n)]
def query_position(symbol="MNQU6", account=None):
    return {"verified": True, "qty": 2, "side": "SHORT", "symbol": symbol,
            "accountId": "TEST", "avgEntry": 29263.75}
def position_identity(position):
    return "gen-1"
def query_orders(symbol="MNQU6", known_order_ids=None, account=None):
    rows = _rows()
    return {"verified": True, "orders": rows, "activeOrders": rows}
def derived_receipt(*args, **kwargs):
    return None
'''


def test_order_ultra_guard_allows_repair():
    for rows, expect_pass in ((0, True), (1, True), (2, True), (3, False), (4, False)):
        sandbox = make_sandbox(BASE)
        try:
            with open(os.path.join(sandbox, "broker_status.py"), "w", encoding="utf-8") as fh:
                fh.write(STUB_BROKER)
            with open(os.path.join(sandbox, ".secrets", "crosstrade.env"), "w", encoding="utf-8") as fh:
                fh.write("CROSSTRADE_URL=http://127.0.0.1:9/blocked\nCROSSTRADE_KEY=TEST\n"
                         "CROSSTRADE_DEST=TEST\nCROSSTRADE_ACCOUNTS=TEST\nMAX_RISK_DOLLARS=200\n")
            env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1",
                       NQX_MODIFY_FRESH_QUOTE="0", STUB_PROTECTIVE_ROWS=str(rows))
            args = ["--modify", "--side", "sell", "--qty", "2", "--sl", "29301.0", "--tp", "29042.5",
                    "--ultra", "--position-generation", "gen-1", "--account", "TEST", "--last", "29200.0",
                    "--management-key=k", "--management-token=t", "--management-intent-hash=h", "--confirm"]
            proc = subprocess.run([sys.executable, os.path.join(sandbox, "order.py")] + args,
                                  capture_output=True, text=True, encoding="utf-8", errors="replace",
                                  cwd=sandbox, env=env, timeout=60)
            out = (proc.stdout or "") + (proc.stderr or "")
            if expect_pass:
                check(f"保護行 {rows} 本: 分割ガードを通る(修復)", "MODIFY_SPLIT_PLAN_PROTECTED" not in out
                      and "MANAGEMENT_CLAIM_INTENT_MISMATCH" in out, out[-200:])
            else:
                check(f"保護行 {rows} 本: 両脚生存とみなして拒否", "MODIFY_SPLIT_PLAN_PROTECTED" in out, out[-200:])
            check(f"保護行 {rows} 本: 外部送信なし", "NQX_ROUTE_" not in out and "HTTP 200" not in out)
        finally:
            shutil.rmtree(sandbox, ignore_errors=True)


# ---------------------------------------------------------------- 3. engine の修復経路

def buy_plan():
    return {"scenarioId": "S-R78", "entryKey": "ENTRY:" + "b" * 64, "symbol": SYM, "side": "BUY",
            "qty": 2, "entry": 100.0, "initialStop": 90.0, "tp1": 110.0, "finalTarget": 130.0,
            "targets": [110.0, 130.0], "trailDistance": 5.0, "accountScope": [ACC],
            "legs": [{"id": "TP1", "qty": 1, "target": 110.0}, {"id": "RUNNER", "qty": 1, "target": 130.0}],
            "legIdentityKnown": True, "planVersion": "R19-ICT-SPLIT-1", "entryOrderType": "LIMIT"}


def buy_position(qty=1):
    # CrossTrade/Tradovate の実態(R52): 建玉行は注文リンク(receipt)を持たない。
    # binder はこの形のときだけ「両脚 FILLED + 建玉 = RUNNER 枚数 + 建値整合」で継続所有を認める。
    return {"verified": True, "side": "LONG", "qty": qty, "avgEntry": 100.0, "accountId": ACC,
            "account": ACC, "symbol": SYM, "filledAt": FILLED_AT.isoformat(), "orderId": None,
            "receipt": None, "initialQty": qty}


def protective_view(n_rows, side_of_position="BUY"):
    opposite = "SELL" if side_of_position == "BUY" else "BUY"
    rows = [{"orderId": f"P-{i}", "accountId": ACC, "symbol": SYM, "status": "WORKING", "action": opposite,
             "fieldsComplete": False, "parentId": None, "receipt": f"TRADOVATE:{ACC}:P-{i}"} for i in range(n_rows)]
    return {"verified": True, "state": "PENDING" if rows else "NONE", "orders": rows, "activeOrders": rows}


def claim_stub(intent):
    return True, {"managementKey": management_intent.management_key(intent), "claimToken": "T" * 43,
                  "managementIntentHash": management_intent.intent_hash(intent)}


def test_repair_restores_previous_stop():
    tmp = tempfile.mkdtemp(prefix="nqx-r78-")
    ledger = os.path.join(tmp, "ledger.jsonl")
    calls = []

    def runner(args, live):
        # 元の張り替え失敗は failure= で渡す。ここに来る MODIFY は修復そのもの(成功させる)。
        calls.append((list(args), bool(live)))
        if "--flatten" in args:
            return 0, "FLATTEN VERIFIED"
        return 0, "stub dry-run" if not live else "MODIFY VERIFIED: structurally"

    handled, notes, prefix = ae._repair_unprotected_runner(
        plan=buy_plan(), position=buy_position(), action={"action": "MODIFY", "qty": 1, "sl": 114.0, "tp": 130.0},
        failure="live send failed/unknown: MODIFY_ROUTE_UNKNOWN", failed_key="K:FAILED", plan_key="K",
        failed_journal={"managementKey": "M-failed"}, previous_stop=90.0, price=115.0, cfg=CFG, symbol=SYM,
        account=ACC, open_qty=1, query=lambda _s: buy_position(),
        broker_order_query=lambda _s, known_order_ids=None: protective_view(1),
        claimer=claim_stub, execute=runner, ledger_path=ledger)
    rows = [json.loads(line) for line in open(ledger, encoding="utf-8")]
    live_modifies = [c[0] for c in calls if "--modify" in c[0] and c[1]]
    check("保護行 1 本 → 修復として扱う", handled and notes and "repair management_sent" in notes[0], (handled, notes, prefix))
    check("修復 MODIFY は直前の stop(初期構造 SL 90)へ", live_modifies and "--sl" in live_modifies[0]
          and live_modifies[0][live_modifies[0].index("--sl") + 1] == "90.0", live_modifies)
    check("修復 MODIFY に --last(参照価格)が付く", live_modifies and "--last" in live_modifies[0], live_modifies)
    check("元の失敗行は HALT ではなく MODIFY_FAILED_REPAIRED",
          any(r.get("status") == "MODIFY_FAILED_REPAIRED" and r.get("key") == "K:FAILED" for r in rows)
          and not any(r.get("status") == "HALT" for r in rows), [r.get("status") for r in rows])
    check("修復行は MANAGEMENT_SENT(repair=True)",
          any(r.get("status") == "MANAGEMENT_SENT" and (r.get("action") or {}).get("repair") is True
              and (r.get("action") or {}).get("sl") == 90.0 for r in rows), [r.get("status") for r in rows])
    check("修復後の台帳は新規を塞がない(_has_halt なし)", ae._has_halt(rows) is None)
    shutil.rmtree(tmp, ignore_errors=True)


def test_repair_flattens_when_previous_stop_is_not_protective():
    tmp = tempfile.mkdtemp(prefix="nqx-r78-")
    ledger = os.path.join(tmp, "ledger.jsonl")
    calls = []
    flat = {"n": 0}

    def runner(args, live):
        calls.append((list(args), bool(live)))
        return 0, "FLATTEN VERIFIED" if "--flatten" in args else "stub"

    def position_after(_symbol):
        return {"verified": True, "qty": 0, "symbol": SYM, "accountId": ACC}

    handled, notes, prefix = ae._repair_unprotected_runner(
        plan=buy_plan(), position=buy_position(), action={"action": "MODIFY", "qty": 1, "sl": 95.0, "tp": 130.0},
        failure="MODIFY_ROUTE_UNKNOWN", failed_key="K:FAILED", plan_key="K", failed_journal=None,
        previous_stop=90.0, price=89.0, cfg=CFG, symbol=SYM, account=ACC, open_qty=1,
        query=position_after, broker_order_query=lambda _s, known_order_ids=None: protective_view(0),
        claimer=claim_stub, execute=runner, ledger_path=ledger)
    rows = [json.loads(line) for line in open(ledger, encoding="utf-8")]
    check("直前の stop が守れない側(価格 89 < 90)なら FLATTEN", handled and "repair flatten sent" in notes[0], (handled, notes))
    check("FLATTEN は --account 付きで 1 回", [c[0] for c in calls] == [["--flatten", "--account", ACC], ["--flatten", "--account", ACC]]
          or sum(1 for c in calls if "--flatten" in c[0] and c[1]) == 1, calls)
    check("台帳に FLATTEN_SENT と MODIFY_FAILED_REPAIRED", {r.get("status") for r in rows} >= {"FLATTEN_SENT", "MODIFY_FAILED_REPAIRED"},
          [r.get("status") for r in rows])
    shutil.rmtree(tmp, ignore_errors=True)


def test_repair_is_not_attempted_when_bracket_exists():
    tmp = tempfile.mkdtemp(prefix="nqx-r78-")
    ledger = os.path.join(tmp, "ledger.jsonl")
    calls = []
    handled, notes, prefix = ae._repair_unprotected_runner(
        plan=buy_plan(), position=buy_position(), action={"action": "MODIFY", "qty": 1, "sl": 114.0, "tp": 130.0},
        failure="MODIFY_ROUTE_UNKNOWN", failed_key="K:FAILED", plan_key="K", failed_journal=None,
        previous_stop=90.0, price=115.0, cfg=CFG, symbol=SYM, account=ACC, open_qty=1,
        query=lambda _s: buy_position(), broker_order_query=lambda _s, known_order_ids=None: protective_view(2),
        claimer=claim_stub, execute=lambda args, live: calls.append(args) or (0, "x"), ledger_path=ledger)
    check("保護行 2 本なら修復しない(従来どおり HALT へ)", not handled and notes == [] and prefix == "" and calls == [],
          (handled, notes, prefix, calls))
    handled, notes, prefix = ae._repair_unprotected_runner(
        plan=buy_plan(), position=buy_position(), action={"action": "MODIFY", "qty": 1, "sl": 114.0, "tp": 130.0},
        failure="MODIFY_ROUTE_UNKNOWN", failed_key="K:FAILED", plan_key="K", failed_journal=None,
        previous_stop=90.0, price=115.0, cfg=CFG, symbol=SYM, account=ACC, open_qty=1,
        query=lambda _s: buy_position(), broker_order_query=lambda _s, known_order_ids=None: {"verified": False},
        claimer=claim_stub, execute=lambda args, live: calls.append(args) or (0, "x"), ledger_path=ledger)
    check("保護注文が UNVERIFIED なら修復せず理由を HALT に添える", not handled and "UNVERIFIED" in prefix and calls == [], prefix)
    shutil.rmtree(tmp, ignore_errors=True)


def managed_ledger(tmp):
    """凍結プラン + 所有済み runner 1 枚の台帳(reconcile の管理経路へ直行できる形)。"""
    ledger = os.path.join(tmp, "managed.jsonl")
    plan = buy_plan()
    route = [{"accountId": ACC, "legId": "TP1", "state": "ACCEPTED", "orderId": "E-1", "receipt": f"TRADOVATE:{ACC}:E-1"},
             {"accountId": ACC, "legId": "RUNNER", "state": "ACCEPTED", "orderId": "E-2", "receipt": f"TRADOVATE:{ACC}:E-2"}]
    ae._append_ledger({"key": plan["entryKey"], "entryKey": plan["entryKey"], "status": "ENTRY_SENT",
                       "action": "ENTRY", "plan": {**plan, "routeSnapshot": route}, "routeState": "SENT",
                       "routeSnapshot": route}, ledger)
    return ledger, plan


def entry_rows_view(protective_rows):
    filled = [{"orderId": "E-1", "accountId": ACC, "symbol": SYM, "status": "FILLED", "action": "BUY", "qty": 1,
               "orderType": "LIMIT", "limitPrice": 100.0, "receipt": f"TRADOVATE:{ACC}:E-1"},
              {"orderId": "E-2", "accountId": ACC, "symbol": SYM, "status": "FILLED", "action": "BUY", "qty": 1,
               "orderType": "LIMIT", "limitPrice": 100.0, "receipt": f"TRADOVATE:{ACC}:E-2"}]
    protective = protective_view(protective_rows)["activeOrders"]
    return {"verified": True, "state": "PENDING" if protective else "FILLED", "orders": filled + protective,
            "activeOrders": protective}


def test_stop_not_protective_is_a_hold_not_a_halt():
    tmp = tempfile.mkdtemp(prefix="nqx-r78-")
    ledger, plan = managed_ledger(tmp)
    calls = []

    def runner(args, live):
        calls.append((list(args), bool(live)))
        if "--modify" in args and not live:
            return 1, ("stop-side guard: ...\nERROR: MODIFY_STOP_NOT_PROTECTIVE: stop 114.00 は現在値 113.00(quote)から "
                       "2pt 以上下にありません。")
        return 0, "stub"

    cfg = {"NQX_SYMBOL": SYM, "NQX_AUTOTRADE": "1", "NQX_LIVE_ORDERS": "1", "CROSSTRADE_ACCOUNTS": ACC,
           "NQX_AUTOTRADE_KILL": "0"}
    price_bundle = {"price": 119.0, "at": NOW.isoformat(), "_published_scenario": {},
                    "snapshot": {"bars3m": [{"t": int(FILLED_AT.timestamp()) + 60, "o": 118, "h": 119, "l": 112, "c": 119}]}}
    notes = ae.reconcile(price_bundle, True, cfg, lambda _s, account=None: buy_position(), runner, ledger,
                         broker_order_query=lambda _s, known_order_ids=None, account=None: entry_rows_view(2),
                         claim_management=claim_stub)
    rows = [json.loads(line) for line in open(ledger, encoding="utf-8")]
    check("束縛した周期のうちに管理まで進む(managing now)", any("managing now" in n for n in notes), notes)
    check("MODIFY_STOP_NOT_PROTECTIVE は見送り注記(HALT ではない)", any("stop not protective at send time" in n for n in notes), notes)
    check("HALT 行を書かない", not any(r.get("status") == "HALT" for r in rows), [r.get("status") for r in rows])
    check("live 送信しない", not any(c[1] for c in calls if "--modify" in c[0]), calls)
    shutil.rmtree(tmp, ignore_errors=True)


def test_reconcile_repairs_after_rejected_replacement():
    tmp = tempfile.mkdtemp(prefix="nqx-r78-")
    ledger, plan = managed_ledger(tmp)
    calls = []
    order_calls = []

    def runner(args, live):
        calls.append((list(args), bool(live)))
        if "--modify" in args and live:
            first = sum(1 for c in calls if "--modify" in c[0] and c[1]) == 1
            if first:
                return 1, "ERROR: MODIFY_ROUTE_UNKNOWN: bracket replacement was not fully accepted"
            return 0, "MODIFY VERIFIED"
        return 0, "stub"

    def order_query(_symbol, known_order_ids=None, account=None):
        order_calls.append(known_order_ids)
        # 修復側の照会(known_order_ids == [])では片脚だけが残っている
        return entry_rows_view(1 if known_order_ids == [] else 2)

    cfg = {"NQX_SYMBOL": SYM, "NQX_AUTOTRADE": "1", "NQX_LIVE_ORDERS": "1", "CROSSTRADE_ACCOUNTS": ACC,
           "NQX_AUTOTRADE_KILL": "0"}
    # 周期の価格は 119 だが quote は 122 → 極値は 122(足の高値 121 より上)→ トレール 117、
    # 117 <= 122 − 4 なので protective。周期の価格のままなら 116 は 119 − 4 に収まらず床になる。
    price_bundle = {"price": 119.0, "at": NOW.isoformat(), "_published_scenario": {},
                    "snapshot": {"bars3m": [{"t": int(FILLED_AT.timestamp()) + 60, "o": 118, "h": 121, "l": 112, "c": 119}]}}
    notes = ae.reconcile(price_bundle, True, cfg, lambda _s, account=None: buy_position(), runner, ledger,
                         broker_order_query=order_query, claim_management=claim_stub,
                         fresh_price_query=lambda _s: 122.0)
    rows = [json.loads(line) for line in open(ledger, encoding="utf-8")]
    live_modifies = [c[0] for c in calls if "--modify" in c[0] and c[1]]
    check("張り替え拒否 → 同じ周期で修復送信", any("repair management_sent" in n for n in notes), notes)
    check("修復は 2 本目の live MODIFY で直前の stop(90)へ", len(live_modifies) == 2
          and live_modifies[1][live_modifies[1].index("--sl") + 1] == "90.0", live_modifies)
    check("1 本目は quote 122 を極値にしたトレール 117(周期の価格 119 ではない)",
          live_modifies and live_modifies[0][live_modifies[0].index("--sl") + 1] == "117.0", live_modifies)
    check("1 本目に --last 122(quote)が付く", live_modifies and "--last" in live_modifies[0]
          and live_modifies[0][live_modifies[0].index("--last") + 1] == "122.0", live_modifies)
    check("台帳: MODIFY_FAILED_REPAIRED + MANAGEMENT_SENT、HALT なし",
          any(r.get("status") == "MODIFY_FAILED_REPAIRED" for r in rows)
          and any(r.get("status") == "MANAGEMENT_SENT" and (r.get("action") or {}).get("repair") for r in rows)
          and not any(r.get("status") == "HALT" for r in rows), [r.get("status") for r in rows])
    shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- 4. ロック

def test_reconcile_lock_is_exclusive():
    tmp = tempfile.mkdtemp(prefix="nqx-r78-")
    path = os.path.join(tmp, "ledger.jsonl.lock")
    first = ae._ReconcileLock(path, 0)
    second = ae._ReconcileLock(path, 0)
    with first as got_first:
        with second as got_second:
            check("同じロックは 2 つ目が取れない", got_first is True and got_second is False, (got_first, got_second))
    with second as got_again:
        check("解放後は取れる", got_again is True)
    shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- 5. fill_watch

def test_fill_watch_change_detection():
    prev = {ACC: {"verified": True, "qty": 4, "side": "SHORT"}}
    cur = {ACC: {"verified": True, "qty": 2, "side": "SHORT"}}
    check("4→2 を検知", fill_watch.changes(prev, cur) == [f"{ACC}: SHORT 4 -> SHORT 2"], fill_watch.changes(prev, cur))
    check("未検証の観測は変化にしない", fill_watch.changes(prev, {ACC: {"verified": False}}) == [])
    check("基準が無ければ変化にしない", fill_watch.changes(None, cur) == [])
    merged = fill_watch.merge_baseline(prev, {ACC: {"verified": False}})
    check("未検証なら前回の基準を保つ", merged[ACC]["qty"] == 4, merged)


def test_fill_watch_bundle_has_no_scenario():
    tmp = tempfile.mkdtemp(prefix="nqx-r78-")
    src = os.path.join(tmp, "bundle.json")
    with open(src, "w", encoding="utf-8") as fh:
        json.dump({"price": 100.0, "sourceSymbol": "CME_MINI:MNQ1!", "scenarios": {"primary": {"state": "ARMED"}},
                   "_published_scenario": {"state": "ARMED", "grade": "A+"},
                   "snapshot": {"bars3m": [{"t": 1, "o": 1, "h": 2, "l": 0.5, "c": 1}]}}, fh)
    b = fill_watch.load_bundle(123.5, SYM, candidates=(src,))
    check("scenario を落とす", b["_published_scenario"] == {} and b["scenarios"] == {})
    check("価格は quote で差し替え", b["price"] == 123.5 and b["priceSource"] == "FRESH_QUOTE")
    check("足は引き継ぐ", len(b["snapshot"]["bars3m"]) == 1)
    b = fill_watch.load_bundle(None, SYM, candidates=(os.path.join(tmp, "missing.json"),))
    check("bundle が無くても落ちない", b["snapshot"]["bars3m"] == [] and "price" not in b)
    shutil.rmtree(tmp, ignore_errors=True)


def test_fill_watch_never_enters():
    """scenario 無しの bundle で FLAT を reconcile しても ENTRY は出ない。"""
    tmp = tempfile.mkdtemp(prefix="nqx-r78-")
    ledger = os.path.join(tmp, "ledger.jsonl")
    calls = []
    cfg = {"NQX_SYMBOL": SYM, "NQX_AUTOTRADE": "1", "NQX_LIVE_ORDERS": "1", "CROSSTRADE_ACCOUNTS": ACC,
           "NQX_AUTOTRADE_KILL": "0"}
    b = fill_watch.load_bundle(100.0, SYM, candidates=())
    notes = ae.reconcile(b, True, cfg, lambda _s, account=None: {"verified": True, "qty": 0, "symbol": SYM, "accountId": ACC},
                         lambda args, live: calls.append(args) or (0, "stub"), ledger,
                         broker_order_query=lambda _s, known_order_ids=None, account=None: {"verified": True, "state": "NONE", "orders": [], "activeOrders": []},
                         state_query=lambda: {"market": {}, "scenario": {}, "position": {"state": "FLAT"}, "order": {"state": "NONE"}})
    check("FLAT + scenario 無し → order.py を呼ばない", calls == [], (calls, notes))
    shutil.rmtree(tmp, ignore_errors=True)


def test_fill_watch_trigger_rearms_once():
    tmp = tempfile.mkdtemp(prefix="nqx-r78-")
    seen = []

    def reconcile(bundle, state_ok=True, fresh_price_query=None):
        seen.append((bundle.get("price"), fresh_price_query(SYM) if fresh_price_query else None))
        return ["autotrade adopted exact broker route/position ownership; management armed next cycle"] if len(seen) == 1 \
            else ["autotrade management_sent: TP1 reached"]

    notes = fill_watch.trigger("test", SYM, price_query=lambda _s: 101.25, reconcile=reconcile,
                               sync_position=lambda: (True, {}), candidates=())
    check("束縛だけで終わった周期は続けて管理を回す(reconcile 2 回)", len(seen) == 2, seen)
    check("quote を bundle と fresh_price_query の両方へ", seen[0] == (101.25, 101.25), seen)
    check("注記に rearm を残す", any("rearm" in n for n in notes), notes)
    seen.clear()
    notes = fill_watch.trigger("test", SYM, price_query=lambda _s: 101.25,
                               reconcile=lambda *a, **k: ["autotrade hold: managed X"], sync_position=None, candidates=())
    check("通常は 1 回", len(notes) == 2 and "hold" in notes[1], notes)
    shutil.rmtree(tmp, ignore_errors=True)


def test_fill_watch_run_once_and_status():
    tmp = tempfile.mkdtemp(prefix="nqx-r78-")
    hb = os.path.join(tmp, "hb.json")
    positions = iter([{"verified": True, "qty": 4, "side": "SHORT"}, {"verified": True, "qty": 2, "side": "SHORT"}])
    triggered = []
    kwargs = dict(symbol=SYM, accounts=[ACC], interval=5.0,
                  position_query=lambda _s, account=None: next(positions),
                  reconcile=lambda bundle, state_ok=True, fresh_price_query=None: triggered.append(bundle) or ["autotrade hold: managed X"],
                  price_query=lambda _s: 29150.0, sync_position=None, heartbeat_path=hb, bundle_candidates=(),
                  once=True, log=lambda _line: None)
    fill_watch.run(**kwargs)
    check("初回は基準を作るだけ(trigger なし)", triggered == [], triggered)
    line = fill_watch.status_line(path=hb)
    check("heartbeat を書き status_line が alive", "alive" in line, line)
    fill_watch.run(**kwargs)
    check("2 回目(4→2)で trigger", len(triggered) == 1 and triggered[0]["price"] == 29150.0, triggered)
    stale = datetime.now(timezone.utc) + timedelta(minutes=5)
    check("古い heartbeat は STALE", "STALE" in fill_watch.status_line(now=stale, path=hb))
    check("heartbeat が無ければ未起動", "未起動" in fill_watch.status_line(path=os.path.join(tmp, "none.json")))
    shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            print(f"\n[{name}]")
            try:
                fn()
            except Exception as exc:  # noqa: BLE001
                import traceback
                traceback.print_exc()
                FAILED.append(f"{name}: {type(exc).__name__}: {exc}")
    print()
    if FAILED:
        print(f"FAILED: {len(FAILED)} -> {FAILED}")
        sys.exit(1)
    print("ALL PASS (test_r78_stop_guard_and_repair; external sends=0)")

# -*- coding: utf-8 -*-
"""R102: 限月ロールの価格乖離事故(2026-09-15 15:10)を、今日の数字で 3 か所独立に止める。

  chart(MNQ1! = 12 月限) 29,376.75 / 9 月限 MNQU6 の市場 29,078.5 /
  買い指値 29,363.75 / SL 29,329.75 / TP 29,495.75 / 29,603

守ること:
  B. チャート銘柄が発注先の限月そのものでなければ tv_fetch / tv_snapshot は BLOCK
     (連続足 MNQ1! は CHART_SYMBOL_CONTINUOUS、別限月は CHART_SYMBOL_MISMATCH)。
  D. order.py は参照価格(--last / quote)に対して指値の通過(ENTRY_LIMIT_THROUGH_MARKET)と
     SL の側(STOP_WRONG_SIDE_OF_MARKET)を、ドライランでも --confirm でも送信前に止める。
     --symbol が正本の限月でなければ ORDER_SYMBOL_NOT_CONTRACT、--last の出所が別限月なら
     LAST_SYMBOL_MISMATCH、参照価格が無ければ QUOTE_UNAVAILABLE。
  D'. engine は claim の前に同じ門(_contract_entry_guard)を通し、order.py の R102 拒否は
     HALT ではなく見送り(_r102_reject_code)。指値でも --last と --price-symbol を渡す。
  E. 所有した建玉に保護注文の OCO 組が 0 なら、同じ周期で --modify --repair-naked(SL/TP 1 組)を
     送るか、置けなければ FLATTEN。約定直後の猶予・SHADOW・冪等を守る。

隔離: order.py は _hermetic のサンドボックス(HTTP は捨てポート)。engine は runner / broker を
スタブ。本番の台帳・.secrets・CrossTrade・TradingView に触れない。

    python tests/test_r102_guards.py
"""
import copy
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import _hermetic  # noqa: E402

BASE = os.path.dirname(HERE)
sys.path.insert(0, BASE)
import autotrade_engine as ae  # noqa: E402
import _pin_contract  # noqa: E402  (manualHalt OFF / 限月 MNQU6 固定 / nakedRepair OFF)
import broker_status  # noqa: E402
import contract  # noqa: E402
import execution_contract  # noqa: E402
import management_intent  # noqa: E402
import tv_fetch  # noqa: E402
import tv_snapshot  # noqa: E402

# R102b: テストは TradingView を叩かない。front_contract は明示するか {} を返す。
tv_fetch.chart_symbol_info = lambda timeout=30: {}

ae._read_env_file = lambda path=None: {}

PASS = [0]
FAIL = [0]


def check(label, condition, detail=""):
    if condition:
        PASS[0] += 1
        print(f"  OK   {label}")
    else:
        FAIL[0] += 1
        print(f"  NG   {label}  {str(detail)[-400:]}")


def section(title):
    print(f"\n--- {title}")


NOW = datetime.now(timezone.utc)
ACC = "LFF00000000000006"
SYM = "MNQU6"

# ---------------------------------------------------------------- B. チャート銘柄
section("B. チャート銘柄は発注先の限月そのもの")
LAYOUT_OK = {"panes": [{"index": 0, "symbol": "CME_MINI:MNQU2026", "resolution": "15"},
                       {"index": 1, "symbol": "CME_MINI:MNQU2026", "resolution": "3"}]}
STATE15 = {"symbol": "CME_MINI:MNQU2026", "resolution": "15", "studies": []}
STATE3 = {"symbol": "CME_MINI:MNQU2026", "resolution": "3", "studies": [{"name": "CVD Unified"}]}
try:
    tv_snapshot.validate_window_layout(LAYOUT_OK, STATE15, STATE3)
    check("MNQU2026 の 2 pane は通る", True)
except tv_snapshot.AcquireError as exc:
    check("MNQU2026 の 2 pane は通る", False, str(exc))

state3_cont = {**STATE3, "symbol": "CME_MINI:MNQ1!"}
try:
    tv_snapshot.validate_window_layout(LAYOUT_OK, STATE15, state3_cont)
    check("pane 1 が MNQ1! で front 未解決なら AcquireError", False)
except tv_snapshot.AcquireError as exc:
    check("pane 1 が MNQ1! で front 未解決なら AcquireError(CHART_SYMBOL_CONTINUOUS_UNRESOLVED)",
          "CHART_SYMBOL_CONTINUOUS_UNRESOLVED" in str(exc), str(exc))
try:
    tv_snapshot.validate_window_layout(LAYOUT_OK, STATE15, state3_cont, None, {"front_contract": "MNQU2026"})
    check("MNQ1! でも front=MNQU2026(発注先と同じ)なら通る", True)
except tv_snapshot.AcquireError as exc:
    check("MNQ1! でも front=MNQU2026(発注先と同じ)なら通る", False, str(exc))
try:
    tv_snapshot.validate_window_layout(LAYOUT_OK, STATE15, state3_cont, None, {"front_contract": "MNQZ2026"})
    check("MNQ1! が MNQZ2026 へロール済みなら AcquireError", False)
except tv_snapshot.AcquireError as exc:
    check("MNQ1! が MNQZ2026 へロール済みなら AcquireError(CHART_SYMBOL_MISMATCH。今日の事故)",
          "CHART_SYMBOL_MISMATCH" in str(exc) and "MNQZ2026" in str(exc), str(exc))
bad_layout = copy.deepcopy(LAYOUT_OK)
bad_layout["panes"][1]["symbol"] = "CME_MINI:NQ1!"
try:
    tv_snapshot.validate_window_layout(bad_layout, STATE15, STATE3)
    check("pane 一覧の NQ1!(別 root)は AcquireError", False)
except tv_snapshot.AcquireError as exc:
    check("pane 一覧の NQ1!(別 root)は AcquireError", "CHART_SYMBOL_MISMATCH" in str(exc), str(exc))

state3_dec = {**STATE3, "symbol": "CME_MINI:MNQZ2026"}
try:
    tv_snapshot.validate_window_layout(LAYOUT_OK, STATE15, state3_dec)
    check("pane 1 の state が別限月なら AcquireError", False)
except tv_snapshot.AcquireError as exc:
    check("pane 1 の state が別限月なら AcquireError(CHART_SYMBOL_MISMATCH)", "CHART_SYMBOL_MISMATCH" in str(exc), str(exc))

try:
    tv_fetch._require_window({"symbol": "CME_MINI:MNQ1!", "resolution": "3"}, "3", "pane 1")
    check("tv_fetch: MNQ1! で front 未解決は AcquisitionError", False)
except tv_fetch.AcquisitionError as exc:
    check("tv_fetch: MNQ1! で front 未解決は AcquisitionError(CHART_SYMBOL_CONTINUOUS_UNRESOLVED)",
          "CHART_SYMBOL_CONTINUOUS_UNRESOLVED" in str(exc), str(exc))
try:
    tv_fetch._require_window({"symbol": "CME_MINI:MNQ1!", "resolution": "3"}, "3", "pane 1", "MNQU2026")
    check("tv_fetch: MNQ1! + front MNQU2026 は通る", True)
except tv_fetch.AcquisitionError as exc:
    check("tv_fetch: MNQ1! + front MNQU2026 は通る", False, str(exc))
try:
    tv_fetch._require_window({"symbol": "CME_MINI:MNQ1!", "resolution": "3"}, "3", "pane 1", "MNQZ2026")
    check("tv_fetch: MNQ1! + front MNQZ2026 は AcquisitionError", False)
except tv_fetch.AcquisitionError as exc:
    check("tv_fetch: MNQ1! + front MNQZ2026 は AcquisitionError(CHART_SYMBOL_MISMATCH)", "CHART_SYMBOL_MISMATCH" in str(exc), str(exc))
try:
    tv_fetch._require_window({"symbol": "CME_MINI:MNQU2026", "resolution": "3"}, "3", "pane 1")
    check("tv_fetch: MNQU2026 は通る", True)
except tv_fetch.AcquisitionError as exc:
    check("tv_fetch: MNQU2026 は通る", False, str(exc))

# ---------------------------------------------------------------- D. order.py(隔離ドライラン)
section("D. order.py の限月/価格の門(今日の数字)")
os.environ["NQX_MODIFY_FRESH_QUOTE"] = "0"          # 隔離環境に tv_fetch は無い(--last で検査)
SANDBOX = _hermetic.make_sandbox(BASE)
# サンドボックスの正本は満期の遠い限月に差し替える(日付でテストが壊れないように)。
sandbox_json = os.path.join(SANDBOX, "execution_contract.json")
with open(sandbox_json, encoding="utf-8") as fh:
    sandbox_contract = json.load(fh)
sandbox_contract["contract"] = {"symbol": "MNQZ9", "tvSymbol": "CME_MINI:MNQZ2029", "expiry": "2029-12-21",
                                "lastEntryDaysBeforeExpiry": 3}
with open(sandbox_json, "w", encoding="utf-8") as fh:
    json.dump(sandbox_contract, fh, ensure_ascii=False, indent=2)

TODAY = ["--side", "buy", "--qty", "2", "--entry", "29363.75", "--sl", "29329.75",
         "--split-tp", "29495.75,29603", "--symbol", "MNQZ9"]
ok, out = _hermetic.dry_run(SANDBOX, TODAY + ["--last", "29078.5"])
check("通過指値(指値 29,363.75 > 市場 29,078.5)は ENTRY_LIMIT_THROUGH_MARKET",
      not ok and "ENTRY_LIMIT_THROUGH_MARKET" in out, out[-300:])
check("拒否時に外部へ送信しない(SNAPSHOT/HTTP 無し)", "SNAPSHOT" not in out and "HTTP" not in out, out[-300:])
ok, out = _hermetic.dry_run(SANDBOX, TODAY + ["--last", "29078.5", "--confirm"])
check("--confirm でも同じ門で止まる", not ok and "ENTRY_LIMIT_THROUGH_MARKET" in out
      and "command=PLACE" not in out, out[-300:])
ok, out = _hermetic.dry_run(SANDBOX, ["--side", "buy", "--qty", "2", "--market", "--last", "29078.5",
                                      "--sl", "29329.75", "--split-tp", "29495.75,29603", "--symbol", "MNQZ9"])
check("成行でも SL 29,329.75 が市場 29,078.5 より上なら STOP_WRONG_SIDE_OF_MARKET",
      not ok and "STOP_WRONG_SIDE_OF_MARKET" in out, out[-300:])
ok, out = _hermetic.dry_run(SANDBOX, TODAY + ["--last", "29400", "--price-symbol=CME_MINI:MNQ1!"])
check("--last の出所が連続足 MNQ1! なら LAST_SYMBOL_MISMATCH", not ok and "LAST_SYMBOL_MISMATCH" in out, out[-300:])
ok, out = _hermetic.dry_run(SANDBOX, ["--side", "buy", "--qty", "2", "--entry", "29363.75", "--sl", "29329.75",
                                      "--split-tp", "29495.75,29603", "--symbol", "MNQU6", "--last", "29400"])
check("--symbol が正本の限月でなければ ORDER_SYMBOL_NOT_CONTRACT", not ok and "ORDER_SYMBOL_NOT_CONTRACT" in out, out[-300:])
ok, out = _hermetic.dry_run(SANDBOX, TODAY)
check("--last も quote も無い新規は QUOTE_UNAVAILABLE", not ok and "QUOTE_UNAVAILABLE" in out, out[-300:])
ok, out = _hermetic.dry_run(SANDBOX, TODAY + ["--last", "29400", "--price-symbol=CME_MINI:MNQZ2029"])
check("正しい限月・resting 側の指値・守れる側の SL はドライランが通る",
      ok and "R102 contract gate" in out and "[ドライラン]" in out, out[-400:])
ok, out = _hermetic.dry_run(SANDBOX, ["--side", "sell", "--qty", "2", "--entry", "29300", "--sl", "29340",
                                      "--split-tp", "29200,29100", "--symbol", "MNQZ9", "--last", "29400"])
check("売り指値が市場より下なら ENTRY_LIMIT_THROUGH_MARKET", not ok and "ENTRY_LIMIT_THROUGH_MARKET" in out, out[-300:])

# ---------------------------------------------------------------- D'. engine の事前検査と見送り
section("D'. engine: claim 前の門 / 見送り / argv")
plan_limit = {"symbol": SYM, "side": "BUY", "entry": 29363.75, "initialStop": 29329.75,
              "entryOrderType": "LIMIT", "legs": [{"id": "TP1", "qty": 1, "target": 29495.75},
                                                  {"id": "RUNNER", "qty": 1, "target": 29603.0}],
              "finalTarget": 29603.0, "qty": 2}
bundle_dec = {"price": 29376.75, "sourceSymbol": "CME_MINI:MNQ1!", "sourceFrontContract": "MNQZ2026",
              "snapshot": {"bars3m": []}}
bundle_unres = {"price": 29376.75, "sourceSymbol": "CME_MINI:MNQ1!", "snapshot": {"bars3m": []}}
bundle_cont_ok = {"price": 29400.0, "sourceSymbol": "CME_MINI:MNQ1!", "sourceFrontContract": "MNQU2026",
                  "sourceContract": "CME_MINI:MNQU2026", "snapshot": {"bars3m": []}}
bundle_ok = {"price": 29400.0, "sourceSymbol": "CME_MINI:MNQU2026", "snapshot": {"bars3m": []}}
g = ae._contract_entry_guard(plan_limit, bundle_dec)
check("bundle の出所が MNQ1!(front MNQZ2026 = ロール済み)なら CHART_SYMBOL_MISMATCH で見送り",
      g["block"] and g["reason"] == "CHART_SYMBOL_MISMATCH", g)
g = ae._contract_entry_guard(plan_limit, bundle_unres)
check("MNQ1! で front 未解決なら CHART_SYMBOL_CONTINUOUS_UNRESOLVED で見送り",
      g["block"] and g["reason"] == "CHART_SYMBOL_CONTINUOUS_UNRESOLVED", g)
g = ae._contract_entry_guard(plan_limit, bundle_cont_ok)
check("MNQ1! で front MNQU2026(発注先と同じ)なら通る", not g["block"], g)
argv_cont = ae._command_for_entry({**plan_limit, "planVersion": "R19-ICT-SPLIT-1", "accountScope": [ACC]}, bundle_cont_ok)
check("MNQ1! の bundle でも --price-symbol は解決した限月そのもの",
      "--price-symbol=CME_MINI:MNQU2026" in argv_cont, argv_cont)
argv_unres = ae._command_for_entry({**plan_limit, "planVersion": "R19-ICT-SPLIT-1", "accountScope": [ACC]}, bundle_unres)
check("front 未解決の連続足は --price-symbol を添えない", not any(a.startswith("--price-symbol=") for a in argv_unres), argv_unres)
g = ae._contract_entry_guard(plan_limit, bundle_ok)
check("正しい出所・SL が価格の下なら通る", not g["block"], g)
g = ae._contract_entry_guard(plan_limit, {**bundle_ok, "price": 29300.0})
check("指値で SL 29,329.75 が価格 29,300 より上なら STOP_WRONG_SIDE_OF_MARKET",
      g["block"] and g["reason"] == "STOP_WRONG_SIDE_OF_MARKET", g)
g = ae._contract_entry_guard({**plan_limit, "symbol": "MNQZ6"}, bundle_ok)
check("プランの symbol が正本と違えば ORDER_SYMBOL_NOT_CONTRACT", g["block"] and g["reason"] == "ORDER_SYMBOL_NOT_CONTRACT", g)
_orig_allowed = contract.entry_allowed
contract.entry_allowed = lambda now=None: (False, "CONTRACT_EXPIRY_NEAR: MNQU6 expires 2026-09-18 in 2d < 3d")
try:
    g = ae._contract_entry_guard(plan_limit, bundle_ok)
    check("満期の手前は CONTRACT_EXPIRY_NEAR で見送り", g["block"] and g["reason"] == "CONTRACT_EXPIRY_NEAR", g)
finally:
    contract.entry_allowed = _orig_allowed
check("_r102_reject_code は order.py の拒否コードを拾う",
      ae._r102_reject_code("dry-run rejected: ...ERROR: ENTRY_LIMIT_THROUGH_MARKET: ...") == "ENTRY_LIMIT_THROUGH_MARKET"
      and ae._r102_reject_code("dry-run rejected: ERROR: FIXED_QTY_REQUIRED") is None)
argv = ae._command_for_entry({**plan_limit, "planVersion": "R19-ICT-SPLIT-1", "accountScope": [ACC]}, bundle_ok)
check("指値でも --last と --price-symbol を渡す",
      "--entry" in argv and "--last" in argv and argv[argv.index("--last") + 1] == "29400.0"
      and "--price-symbol=CME_MINI:MNQU2026" in argv, argv)

# ---------------------------------------------------------------- D''. ロール後の旧 claim 回復
section("D''. ロール後は claim が置かれた限月で不在証明を作る")
check("claim の intent.symbol を優先", ae._claim_recovery_symbol({"executionIntent": {"symbol": "MNQU6"}}, "MNQZ6") == "MNQU6")
check("intent が無ければ今の限月", ae._claim_recovery_symbol({"executionIntent": {}}, "MNQZ6") == "MNQZ6"
      and ae._claim_recovery_symbol(None, "MNQZ6") == "MNQZ6")

# ---------------------------------------------------------------- E. OCO 組の数え方
section("E. broker_status.oco_pairs")


def row(order_id, oco=None, parent=None, status="WORKING", action="SELL", account=ACC):
    return {"orderId": str(order_id), "accountId": account, "symbol": SYM, "status": status,
            "action": action, "brokerOcoId": str(oco) if oco is not None else None,
            "brokerParentId": str(parent) if parent is not None else None,
            "parentId": None, "fieldsComplete": False, "receipt": f"TRADOVATE:{account}:{order_id}"}


orphan_tps = [row(1001), row(1002)]                           # 2026-09-15: TP 2 本、SL 無し
pairs, singles = broker_status.oco_pairs(orphan_tps, account=ACC, expected_action="SELL", symbol=SYM)
check("孤立 TP 2 本は組 0 / 孤立 2", len(pairs) == 0 and len(singles) == 2, (pairs, singles))
good_pair = [row(2001, oco=2002), row(2002, oco=2001)]
pairs, singles = broker_status.oco_pairs(good_pair, account=ACC, expected_action="SELL", symbol=SYM)
check("相互 ocoId の 2 本は組 1", len(pairs) == 1 and not singles, (pairs, singles))
pairs, singles = broker_status.oco_pairs(good_pair + orphan_tps, account=ACC, expected_action="SELL", symbol=SYM)
check("組 1 + 孤立 2 を分けて数える", len(pairs) == 1 and len(singles) == 2)
suspended = [row(3001, oco=3002, status="SUSPENDED"), row(3002, oco=3001, status="SUSPENDED")]
pairs, singles = broker_status.oco_pairs(suspended, account=ACC, expected_action="SELL", symbol=SYM)
check("SUSPENDED(親待ちの子)は数えない", not pairs and not singles)
# R76 の実測形: 約定後のブラケットは片方の子が親(約定した entry)を名乗ったまま相互 ocoId で結ばれる。
filled_bracket = [row(4001, oco=4002, parent=4000), row(4002, oco=4001)]
pairs, singles = broker_status.oco_pairs(filled_bracket, account=ACC, expected_action="SELL", symbol=SYM)
check("約定後の PLACE ブラケット(片方が親を名乗る)は組 1(2026-09-15 16:51 の誤判定の再発防止)", len(pairs) == 1 and not singles, (pairs, singles))
suspended_children = [row(4101, oco=4102, parent=4100, status="SUSPENDED"), row(4102, oco=4101, parent=4100, status="SUSPENDED")]
pairs, singles = broker_status.oco_pairs(suspended_children, account=ACC, expected_action="SELL", symbol=SYM)
check("親待ちの子(SUSPENDED)は数えない", not pairs and not singles)
pairs, singles = broker_status.oco_pairs(good_pair, account="OTHER", expected_action="SELL", symbol=SYM)
check("別口座の行は数えない", not pairs and not singles)

# ---------------------------------------------------------------- E. 裸建玉の検知と修復
section("E. _guard_naked_position")
CFG = {"NQX_SYMBOL": SYM, "CROSSTRADE_ACCOUNTS": ACC}


def buy_plan():
    return {"scenarioId": "S-R102", "entryKey": "ENTRY:" + "c" * 64, "symbol": SYM, "side": "BUY",
            "qty": 2, "entry": 100.0, "initialStop": 90.0, "tp1": 110.0, "finalTarget": 130.0,
            "targets": [110.0, 130.0], "trailDistance": 5.0, "accountScope": [ACC],
            "legs": [{"id": "TP1", "qty": 1, "target": 110.0}, {"id": "RUNNER", "qty": 1, "target": 130.0}],
            "legIdentityKnown": True, "planVersion": "R19-ICT-SPLIT-1", "entryOrderType": "LIMIT"}


def buy_position(age_sec=120.0, qty=2):
    filled = (NOW - timedelta(seconds=age_sec)).isoformat()
    return {"verified": True, "side": "LONG", "qty": qty, "avgEntry": 100.0, "accountId": ACC,
            "account": ACC, "symbol": SYM, "filledAt": filled, "orderId": "651929062505",
            "receipt": None, "initialQty": qty}


def order_view(rows):
    return {"verified": True, "state": "PENDING" if rows else "NONE", "orders": list(rows),
            "activeOrders": list(rows)}


def claim_stub(intent):
    return True, {"managementKey": management_intent.management_key(intent), "claimToken": "T" * 43,
                  "managementIntentHash": management_intent.intent_hash(intent)}


def run_guard(*, rows, mode="LIVE", grace=30, age=120.0, price=105.0, noise=3.0, records=None,
              live=True, state=None):
    execution_contract.CONTRACT["contract"]["nakedRepair"] = {"mode": mode, "graceSec": grace}
    tmp = tempfile.mkdtemp(prefix="nqx-r102-")
    ledger = os.path.join(tmp, "ledger.jsonl")
    calls = []

    def runner(args, live_flag):
        calls.append((list(args), bool(live_flag)))
        if "--flatten" in args:
            return 0, "FLATTEN VERIFIED"
        return 0, "stub dry-run" if not live_flag else "MODIFY VERIFIED: structurally"

    position = buy_position(age_sec=age)
    flat_after = {"verified": True, "qty": 0, "symbol": SYM}
    query_state = {"flat": False}

    def query(_symbol):
        return flat_after if query_state["flat"] else position

    def runner_with_flat(args, live_flag):
        result = runner(args, live_flag)
        if "--flatten" in args and live_flag:
            query_state["flat"] = True
        return result

    try:
        stop_here, notes = ae._guard_naked_position(
            plan=buy_plan(), position=position, order=order_view(rows), records=list(records or []),
            plan_key="K", state=dict(state or {}), price=price, noise=noise, cfg=CFG, symbol=SYM,
            account=ACC, open_qty=2, query=query,
            broker_order_query=lambda _s, known_order_ids=None: order_view(rows),
            claimer=claim_stub, execute=runner_with_flat, ledger_path=ledger, live=live, now=NOW)
    finally:
        execution_contract.CONTRACT["contract"]["nakedRepair"] = {"mode": "OFF", "graceSec": 0}
    rows_out = []
    if os.path.exists(ledger):
        with open(ledger, encoding="utf-8") as fh:
            rows_out = [json.loads(line) for line in fh if line.strip()]
    return stop_here, notes, calls, rows_out


stop_here, notes, calls, ledger_rows = run_guard(rows=orphan_tps, mode="OFF")
check("OFF: 何もしない", not stop_here and not notes and not calls)

stop_here, notes, calls, ledger_rows = run_guard(rows=good_pair)
check("OCO 組があれば裸ではない(通常管理へ)", not stop_here and not notes and not calls, notes)

stop_here, notes, calls, ledger_rows = run_guard(rows=orphan_tps, age=5.0)
check("約定直後(5s < 30s)は猶予: 注記だけで送らない", not stop_here and notes and "deferred" in notes[0] and not calls, notes)

stop_here, notes, calls, ledger_rows = run_guard(rows=orphan_tps, mode="SHADOW")
check("SHADOW: 判定を注記に出すだけ", not stop_here and notes and "naked (shadow)" in notes[0]
      and "would MODIFY sl=90.0 tp=110.0" in notes[0] and not calls, notes)

stop_here, notes, calls, ledger_rows = run_guard(rows=orphan_tps, live=False)
check("AUTO OFF(live=False): 提案だけ", stop_here and notes and "proposal NAKED_REPAIR" in notes[0] and not calls, notes)

stop_here, notes, calls, ledger_rows = run_guard(rows=orphan_tps)
modify_calls = [c for c in calls if "--modify" in c[0]]
check("LIVE: 構造 SL 90 が守れる側 → --modify --repair-naked を dry-run→live の 2 回",
      stop_here and len(modify_calls) == 2 and modify_calls[0][1] is False and modify_calls[1][1] is True
      and all("--repair-naked" in c[0] for c in modify_calls), calls)
argv = modify_calls[-1][0] if modify_calls else []
check("argv: --sl 90.0 / --tp 110.0(TP1) / --account / --last 105.0",
      "--sl" in argv and argv[argv.index("--sl") + 1] == "90.0"
      and "--tp" in argv and argv[argv.index("--tp") + 1] == "110.0"
      and "--account" in argv and argv[argv.index("--account") + 1] == ACC
      and "--last" in argv and argv[argv.index("--last") + 1] == "105.0", argv)
statuses = [r.get("status") for r in ledger_rows]
check("台帳: MANAGEMENT_CLAIMED → MANAGEMENT_SENT(naked)、HALT なし",
      statuses == ["MANAGEMENT_CLAIMED", "MANAGEMENT_SENT"] and ledger_rows[-1].get("naked", {}).get("orphanRows") == 2
      and ledger_rows[-1]["key"].endswith(":NAKED_REPAIR:2"), statuses)
check("注記: NAKED management_sent", notes and "NAKED management_sent" in notes[0], notes)

stop_here, notes, calls, ledger_rows = run_guard(rows=orphan_tps, price=88.0, noise=3.0)
modify_calls = [c for c in calls if "--modify" in c[0]]
argv = modify_calls[-1][0] if modify_calls else []
check("構造 SL 90 が価格 88 の逆側 → 価格 − max(1.0N, 4pt) = 84.0 に置く",
      modify_calls and argv[argv.index("--sl") + 1] == "84.0", argv)

stop_here, notes, calls, ledger_rows = run_guard(rows=orphan_tps, price=112.0)
flatten_calls = [c for c in calls if "--flatten" in c[0]]
check("TP1 110 が価格 112 の逆側 → FLATTEN(--flatten --account)",
      stop_here and flatten_calls and "--account" in flatten_calls[-1][0]
      and not [c for c in calls if "--modify" in c[0]], calls)
check("台帳: FLATTEN_SENT(naked)", [r.get("status") for r in ledger_rows] == ["FLATTEN_SENT"]
      and ledger_rows[-1]["action"]["naked"] is True, ledger_rows)

stop_here, notes, calls, ledger_rows = run_guard(rows=orphan_tps, price=None)
check("価格が無ければ FLATTEN", stop_here and [c for c in calls if "--flatten" in c[0]]
      and not [c for c in calls if "--modify" in c[0]], calls)

prior = [{"key": f"K:{ACC}:NAKED_REPAIR:2", "status": "MANAGEMENT_SENT", "action": {"action": "MODIFY"}}]
stop_here, notes, calls, ledger_rows = run_guard(rows=orphan_tps, records=prior)
check("同じ建玉の修復は 1 回だけ(冪等)", stop_here and not calls and "already attempted" in notes[0], notes)

stop_here, notes, calls, ledger_rows = run_guard(rows=orphan_tps, state={"stop": 97.0})
modify_calls = [c for c in calls if "--modify" in c[0]]
argv = modify_calls[-1][0] if modify_calls else []
check("直前に受理された SL(97)があればそれを優先", modify_calls and argv[argv.index("--sl") + 1] == "97.0", argv)

print(f"\nR102 guards: {PASS[0]} passed, {FAIL[0]} failed")
sys.exit(1 if FAIL[0] else 0)

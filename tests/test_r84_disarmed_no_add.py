# -*- coding: utf-8 -*-
"""R84: AUTO OFF の「管理専用」周期から追撃が出ないこと。

`_disarmed_single_account_management`(R68)と `_reconcile_locked` の多口座ループは、
AUTO が切れていても建玉管理と未約定取消だけは通すために `NQX_AUTOTRADE=1` /
`NQX_LIVE_ORDERS=1` を差し込んで `_reconcile_one` を呼ぶ。その安全性は
**「_reconcile_one は建玉があるとき ENTRY へ進まない」**という不変条件に乗っていた。

R84 は open_qty>0 の枝の中に送信経路(追撃)を足したので、その不変条件はもう暗黙には
成立しない。**追撃は新規 ENTRY** であり、AUTO OFF では出してはならない
(CLAUDE.md §3 / R82 が追撃を新規 ENTRY と同列に並べているのと同じ扱い)。

このテストが落ちるとき、実害は `dryRun=false` にした瞬間に出る —— AUTO を切ったのに
建て増しが飛ぶ。

    python tests/test_r84_disarmed_no_add.py
"""
import copy
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _r84_fixtures as fx  # noqa: E402

sys.path.insert(0, fx.BASE)
import autotrade_engine as ae  # noqa: E402

ae._read_env_file = lambda path=None: {}
import broker_status  # noqa: E402
import execution_contract  # noqa: E402
import execution_intent  # noqa: E402
import route_envelope  # noqa: E402
import strategy_evidence  # noqa: E402

PASS = [0]
FAIL = [0]


def check(label, condition, detail=""):
    if condition:
        PASS[0] += 1
        print(f"  OK   {label}")
    else:
        FAIL[0] += 1
        print(f"  NG   {label}  {detail}")


NOW = datetime.now(timezone.utc)
EVIDENCE = strategy_evidence.canonicalize({
    "version": "R14-STRATEGY-EVIDENCE-1", "asOf": NOW.isoformat(),
    "sessionId": "NY-R84D", "source": "fixture", "provenance": "test",
    "models": {"ifvg": {"valid": False}},
})
ADD_PRICE = 29448.5
SCENARIO = {
    "decisionId": "r84-disarmed", "scenarioId": "r84-disarmed", "model": "VP80_REVERSION",
    "grade": "A+", "state": "ARMED", "fingerprint": "fp-r84-disarmed", "symbol": fx.SYM,
    "marketCycleId": "cycle-r84-d", "side": "SELL", "qty": 8,
    "entry": ADD_PRICE, "stop": 29507.5, "target": 29400.0,
    "targets": [29400.0, 29300.0], "targetR": [1.0, 2.5],
    "legs": [{"id": "TP1", "qty": 4, "target": 29400.0},
             {"id": "RUNNER", "qty": 4, "target": 29300.0}],
    "planVersion": "R19-ICT-SPLIT-1",
    "issuedAt": NOW.isoformat(), "observedAt": NOW.isoformat(),
    "expiresAt": (NOW + timedelta(minutes=5)).isoformat(),
    "evidenceHash": EVIDENCE["evidenceHash"], "setupVersion": "R14-SETUP",
    "catalogVersion": "R14-CATALOG", "detectorVersion": "R14-DETECTOR",
    "executionContractVersion": execution_contract.VERSION,
    "executionContract": {"accountScope": [fx.ACC], "riskCapDollars": 3100.0,
                          "riskCapSource": "ACCOUNT_DRAWDOWN_BUFFER"},
}
BUNDLE = {"_published_scenario": SCENARIO, "price": ADD_PRICE,
          "at": NOW.isoformat(), "cvdAt": NOW.isoformat(),
          "snapshot": {"bars3m": [{"h": 29460.0, "l": 29440.0, "c": ADD_PRICE}] * 12}}


def view():
    market = {"at": NOW.isoformat(), "observedAt": NOW.isoformat(), "cvdAt": NOW.isoformat(),
              "cycleId": SCENARIO["marketCycleId"], "cycleCommitted": True, "verified": True,
              "source": "fixture", "sourceSymbol": "CME_MINI:MNQU6",
              "strategyEvidence": EVIDENCE}
    server = {key: value for key, value in SCENARIO.items() if key not in {"decisionId", "model"}}
    server["cycleCommitted"] = True
    return {"market": market, "scenario": server,
            "position": {"state": "OPEN", "qty": 4, "side": "SHORT"},
            "order": {"state": "PENDING", "verified": True},
            "display": {"orderable": False, "cyclePaired": True,
                        "blockReason": "POSITION OPEN — MANAGEMENT ONLY"}}


T1 = fx.tranche_one()
ROUTE = [{"accountId": fx.ACC, "legId": item["id"], "state": "ACCEPTED",
          "orderId": item["orderId"], "receipt": item["receipt"],
          "bracketOrderIds": item["bracketOrderIds"],
          "bracketReceipts": item["bracketReceipts"]} for item in T1["legs"]]
ROWS = []
for item in T1["legs"]:
    ROWS += fx.bracket_rows(item["orderId"], item["bracketOrderIds"])
POSITION = fx.position(qty=4, avg_entry=29484.25)
IDENTITY = broker_status.position_identity(POSITION)
PLAN = {
    "planVersion": "R19-ICT-SPLIT-1", "entryKey": "ENTRY:" + "7" * 64,
    "scenarioId": "S-D1", "fingerprint": "fp-d1", "evidenceHash": "se-d1",
    "marketCycleId": "cycle-d1", "decisionId": "S-D1",
    "accountScope": [fx.ACC], "symbol": fx.SYM, "model": "VP80_REVERSION", "grade": "A+",
    "side": "SELL", "qty": 4, "entry": 29484.25, "entryOrderType": "LIMIT",
    "entryReference": 29484.25, "initialStop": 29520.0, "tp1": 29440.0,
    "finalTarget": 29380.0, "targets": [29440.0, 29380.0],
    "legs": [{"id": "TP1", "qty": 2, "target": 29440.0},
             {"id": "RUNNER", "qty": 2, "target": 29380.0}],
    "trailDistance": 18.75, "mode": "SPLIT_BRACKETS_TP1_RUNNER", "ultra": True,
    "ultraDrawdown": 3100.0, "riskCapDollars": 3100.0,
    "riskCapSource": "ACCOUNT_DRAWDOWN_BUFFER", "routeSnapshot": ROUTE,
    "positionOwnership": {"generation": "PG:1:" + str(IDENTITY), "rawIdentity": IDENTITY,
                          "accountId": fx.ACC, "symbol": fx.SYM, "side": "SELL",
                          "orderId": POSITION["orderId"], "receipt": None,
                          "filledAt": POSITION["filledAt"], "avgEntry": 29484.25,
                          "initialQty": 4},
}


def claim_stub(scenario, **kwargs):
    py = kwargs.get("pyramid")
    add_qty = int((py or {}).get("addQty") or scenario["qty"])
    split = execution_contract.ultra_split(add_qty)
    intent = execution_intent.build(
        symbol=scenario["symbol"], side=scenario["side"], qty=add_qty, order_type="MARKET",
        entry=None, last=BUNDLE["price"], stop=scenario["stop"], targets=scenario["targets"],
        legs=[{"id": "TP1", "qty": split[0], "target": scenario["targets"][0]},
              {"id": "RUNNER", "qty": split[1], "target": scenario["targets"][1]}],
        plan_version=scenario["planVersion"],
        execution_contract_version=execution_contract.VERSION, account_scope=[fx.ACC],
        pyramid=({"baseQty": int(py["baseQty"]), "addQty": add_qty} if py else None))
    return True, {"entryKey": ae._entry_key(scenario), "claimToken": "T" * 43,
                  "executionIntent": intent,
                  "executionIntentHash": execution_intent.intent_hash(intent)}


def make_ledger(tmp):
    path = os.path.join(tmp, "r84d.jsonl")
    ae._append_ledger({"key": "POSITION_GENERATION:" + fx.ACC + ":1:" + str(IDENTITY),
                       "status": "POSITION_GENERATION", "generation": 1,
                       "identity": IDENTITY, "open": True, "accountId": fx.ACC,
                       "symbol": fx.SYM, "side": "SHORT"}, path)
    ae._append_ledger({"key": PLAN["entryKey"], "entryKey": PLAN["entryKey"],
                       "status": "ENTRY_SENT", "action": "ENTRY", "plan": PLAN,
                       "routeState": "SENT", "routeSnapshot": ROUTE}, path)
    return path


def runner_factory(calls):
    def runner(args, confirm):
        calls.append((list(args), bool(confirm)))
        if not confirm:
            return 0, "stub dry-run ok"
        return 0, route_envelope.format_envelope(
            [{"accountId": fx.ACC, "legId": "TP1", "state": "ACCEPTED",
              "orderId": "X1", "receipt": "RX1"},
             {"accountId": fx.ACC, "legId": "RUNNER", "state": "ACCEPTED",
              "orderId": "X2", "receipt": "RX2"}], "SENT")
    return runner


def live_contract(**over):
    class _Ctx:
        def __enter__(self):
            self.before = copy.deepcopy(execution_contract.CONTRACT["pyramid"])
            execution_contract.CONTRACT["pyramid"].update(over)
            return self
        def __exit__(self, *_exc):
            execution_contract.CONTRACT["pyramid"].clear()
            execution_contract.CONTRACT["pyramid"].update(self.before)
    return _Ctx()


def run_reconcile(path, calls, *, auto_on):
    """`reconcile()` を通す —— 管理専用の cfg 注入は reconcile の中にある。"""
    env = {"NQX_SYMBOL": fx.SYM, "CROSSTRADE_ACCOUNTS": fx.ACC,
           "NQX_AUTOTRADE_KILL": "0", "NQX_STALE_ENTRY_CANCEL": "0",
           "NQX_AUTOTRADE": "1" if auto_on else "0",
           "NQX_LIVE_ORDERS": "1" if auto_on else "0"}
    return ae.reconcile(
        BUNDLE, True, env,
        lambda _symbol, account=None: copy.deepcopy(POSITION),
        runner_factory(calls), path, None,
        broker_order_query=lambda _symbol, known_order_ids=None, account=None: copy.deepcopy(
            fx.orders(ROWS)),
        state_query=view, claim_entry=claim_stub,
        fresh_price_query=lambda _symbol: ADD_PRICE)


print("--- AUTO OFF(管理専用の cfg 注入)では追撃を送らない ---")
tmp = tempfile.mkdtemp(prefix="nqx-r84-dis-")
try:
    path = make_ledger(tmp)
    calls = []
    # **dryRun=false** = 実弾モード。ここで守れていなければ M4 の初日に飛ぶ。
    with live_contract(dryRun=False):
        notes = run_reconcile(path, calls, auto_on=False)
    rows = [json.loads(line) for line in open(path, encoding="utf-8") if line.strip()]
    statuses = [row["status"] for row in rows]
    check("追撃は一度も送信されない",
          all(not confirm for _args, confirm in calls), str(calls))
    check("--pyramid 引数が組み立てられてもいない",
          not any("--pyramid" in args for args, _confirm in calls), str(calls))
    check("PYRAMID_CLAIMED / PYRAMID_SENT が台帳に増えない",
          "PYRAMID_CLAIMED" not in statuses and "PYRAMID_SENT" not in statuses, str(statuses))
    check("理由は ENTRY_DISARMED として残る",
          any(row.get("reason") == "ENTRY_DISARMED" for row in rows), str(statuses))
    check("注記にも出る",
          any("ENTRY_DISARMED" in note for note in notes), str(notes))
    check("建玉管理そのものは止まらない(R68 の主旨)",
          any("managed" in note or "hold" in note for note in notes), str(notes))
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print()
print("--- AUTO ON なら同じ状態で追撃が出る(門が効きすぎていないこと) ---")
tmp = tempfile.mkdtemp(prefix="nqx-r84-dis2-")
try:
    path = make_ledger(tmp)
    calls = []
    with live_contract(dryRun=False):
        notes = run_reconcile(path, calls, auto_on=True)
    rows = [json.loads(line) for line in open(path, encoding="utf-8") if line.strip()]
    statuses = [row["status"] for row in rows]
    check("AUTO ON では追撃が送られる",
          any(confirm and "--pyramid" in args for args, confirm in calls), str(calls))
    check("WAL も進む", "PYRAMID_CLAIMED" in statuses and "PYRAMID_SENT" in statuses,
          str(statuses))
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print()
print("--- 印そのもの ---")
check("_entry_disarmed は明示の印だけを見る",
      ae._entry_disarmed({ae.ENTRY_DISARMED_KEY: "1"}) is True
      and ae._entry_disarmed({}) is False
      and ae._entry_disarmed({ae.ENTRY_DISARMED_KEY: "0"}) is False)
source = open(os.path.join(fx.BASE, "autotrade_engine.py"), encoding="utf-8").read()
# cfg へ印を入れている箇所(dict のキーとして書かれた `ENTRY_DISARMED_KEY: `)を数える。
# 管理専用の注入は 1 口座経路と多口座ループの 2 か所しかない。片方に足し忘れると
# その経路だけ AUTO OFF で追撃が出るので、本数そのものを固定する。
injections = source.count("ENTRY_DISARMED_KEY: ")
check("管理専用の cfg 注入は 2 か所とも印を立てる", injections == 2, f"{injections} 箇所")
check("scoped_cfg を作る箇所は 2 か所のまま(増えたら印も足す)",
      source.count('"NQX_AUTOTRADE": "1",') == 2,
      str(source.count('"NQX_AUTOTRADE": "1",')))

print()
print(f"合計: PASS {PASS[0]} / FAIL {FAIL[0]}")
sys.exit(1 if FAIL[0] else 0)

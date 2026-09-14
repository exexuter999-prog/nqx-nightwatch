# -*- coding: utf-8 -*-
"""R84: 追撃トレードを **1 本の台帳で最初から最後まで** 通す。

個々の部品は他の test_r84_* が固定しているが、実害はたいてい「部品の間」で出る
(R71 / R74 / R76 / R78 はどれも繋ぎ目の事故だった)。ここでは 1 つの台帳に対して
サイクルを順に回し、次の遷移が全部つながることを確かめる:

    基礎 4 枚 → 追撃 4 枚(送信 → コミット)→ T1 の TP1 約定 → T2 の TP1 約定
    → 統合 MODIFY → トレール → 決済 → 戦績(trade_journal)

各段で「建玉が所有され続けているか」を必ず見る —— 所有が落ちた瞬間から建玉は
ブローカー側 OCO だけが守る状態になり、それが R84 が直そうとした事故そのものである。

    python tests/test_r84_lifecycle.py
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
import management_intent  # noqa: E402
import route_envelope  # noqa: E402
import strategy_evidence  # noqa: E402
import tranche  # noqa: E402
import trade_journal  # noqa: E402

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
FILLED_AT = NOW - timedelta(minutes=30)
BAR_T = int(FILLED_AT.timestamp()) + 120
EVIDENCE = strategy_evidence.canonicalize({
    "version": "R14-STRATEGY-EVIDENCE-1", "asOf": NOW.isoformat(),
    "sessionId": "NY-R84L", "source": "fixture", "provenance": "test",
    "models": {"ifvg": {"valid": False}},
})
ADD_PRICE = 29448.5
BASE_ENTRY = 29484.25
COMBINED = (4 * BASE_ENTRY + 4 * ADD_PRICE) / 8      # 29466.375

SCENARIO = {
    "decisionId": "r84-life", "scenarioId": "r84-life", "model": "VP80_REVERSION",
    "grade": "A+", "state": "ARMED", "fingerprint": "fp-r84-life", "symbol": fx.SYM,
    "marketCycleId": "cycle-r84-life", "side": "SELL", "qty": 8,
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
CFG = {"NQX_SYMBOL": fx.SYM, "NQX_AUTOTRADE": "1", "NQX_LIVE_ORDERS": "1",
       "CROSSTRADE_ACCOUNTS": fx.ACC, "NQX_AUTOTRADE_KILL": "0",
       "NQX_STALE_ENTRY_CANCEL": "0"}


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


def bundle(price, *, low=None, scenario=True):
    low = price if low is None else low
    return {"_published_scenario": SCENARIO if scenario else {}, "price": price,
            "at": NOW.isoformat(), "cvdAt": NOW.isoformat(),
            "snapshot": {"bars3m": [{"t": BAR_T + 180 * i, "o": price, "h": price,
                                     "l": low, "c": price} for i in range(12)]}}


T1, T2 = fx.tranche_one(), fx.tranche_two()
T1_TP1, T1_RUN = T1["legs"]
T2_TP1, T2_RUN = T2["legs"]
BASE_ROUTE = [{"accountId": fx.ACC, "legId": item["id"], "state": "ACCEPTED",
               "orderId": item["orderId"], "receipt": item["receipt"],
               "bracketOrderIds": item["bracketOrderIds"],
               "bracketReceipts": item["bracketReceipts"]} for item in T1["legs"]]
ADD_ROUTE = [{"accountId": fx.ACC, "legId": item["id"], "state": "ACCEPTED",
              "orderId": item["orderId"], "receipt": item["receipt"],
              "bracketOrderIds": item["bracketOrderIds"],
              "bracketReceipts": item["bracketReceipts"]} for item in T2["legs"]]
PAIR_IDS = ["C-STOP", "C-TGT"]


def leg_rows(leg):
    return fx.bracket_rows(leg["orderId"], leg["bracketOrderIds"])


def position(qty, avg):
    row = fx.position(qty=qty, avg_entry=avg)
    row["filledAt"] = FILLED_AT.isoformat()
    return row


IDENTITY = broker_status.position_identity(position(4, BASE_ENTRY))
GENERATION = "PG:1:" + str(IDENTITY)
BASE_PLAN = {
    "planVersion": "R19-ICT-SPLIT-1", "entryKey": "ENTRY:" + "4" * 64,
    "scenarioId": "S-LIFE-1", "fingerprint": "fp-life-1", "evidenceHash": "se-life-1",
    "marketCycleId": "cycle-life-1", "decisionId": "S-LIFE-1",
    "accountScope": [fx.ACC], "symbol": fx.SYM, "model": "VP80_REVERSION", "grade": "A+",
    "side": "SELL", "qty": 4, "entry": BASE_ENTRY, "entryOrderType": "LIMIT",
    "entryReference": BASE_ENTRY, "initialStop": 29520.0, "tp1": 29440.0,
    "finalTarget": 29380.0, "targets": [29440.0, 29380.0],
    "legs": [{"id": "TP1", "qty": 2, "target": 29440.0},
             {"id": "RUNNER", "qty": 2, "target": 29380.0}],
    "trailDistance": 18.75, "mode": "SPLIT_BRACKETS_TP1_RUNNER", "ultra": True,
    "ultraDrawdown": 3100.0, "riskCapDollars": 3100.0,
    "riskCapSource": "ACCOUNT_DRAWDOWN_BUFFER", "routeSnapshot": BASE_ROUTE,
    "positionOwnership": {"generation": GENERATION, "rawIdentity": IDENTITY,
                          "accountId": fx.ACC, "symbol": fx.SYM, "side": "SELL",
                          "orderId": "POS-1", "receipt": None,
                          "filledAt": FILLED_AT.isoformat(), "avgEntry": BASE_ENTRY,
                          "initialQty": 4},
}


def entry_claim_stub(scenario, **kwargs):
    py = kwargs.get("pyramid")
    add_qty = int((py or {}).get("addQty") or scenario["qty"])
    split = execution_contract.ultra_split(add_qty)
    intent = execution_intent.build(
        symbol=scenario["symbol"], side=scenario["side"], qty=add_qty, order_type="MARKET",
        entry=None, last=ADD_PRICE, stop=scenario["stop"], targets=scenario["targets"],
        legs=[{"id": "TP1", "qty": split[0], "target": scenario["targets"][0]},
              {"id": "RUNNER", "qty": split[1], "target": scenario["targets"][1]}],
        plan_version=scenario["planVersion"],
        execution_contract_version=execution_contract.VERSION, account_scope=[fx.ACC],
        pyramid=({"baseQty": int(py["baseQty"]), "addQty": add_qty} if py else None))
    return True, {"entryKey": ae._entry_key(scenario), "claimToken": "T" * 43,
                  "executionIntent": intent,
                  "executionIntentHash": execution_intent.intent_hash(intent)}


def management_claim_stub(intent):
    return True, {"managementKey": management_intent.management_key(intent),
                  "claimToken": "M" * 43,
                  "managementIntentHash": management_intent.intent_hash(intent)}


class World:
    """ブローカー側の世界。サイクルの合間にテストが状態を進める。"""

    def __init__(self):
        self.qty = 4
        self.avg = BASE_ENTRY
        self.live_legs = [T1_TP1, T1_RUN]
        self.pair = None
        self.calls = []

    def rows(self):
        if self.pair is not None:
            return fx.oco_rows(PAIR_IDS)
        rows = []
        for leg in self.live_legs:
            rows += leg_rows(leg)
        return rows

    def query(self, _symbol, account=None):
        if self.qty <= 0:
            return {"verified": True, "source": "fixture", "accountId": fx.ACC,
                    "account": fx.ACC, "symbol": fx.SYM, "qty": 0,
                    "observedAt": NOW.isoformat(), "closedAt": NOW.isoformat()}
        return copy.deepcopy(position(self.qty, self.avg))

    def orders(self, _symbol, known_order_ids=None, account=None):
        return copy.deepcopy(fx.orders(self.rows()))

    def runner(self, args, confirm):
        self.calls.append((list(args), bool(confirm)))
        if not confirm:
            return 0, "stub dry-run ok"
        if "--pyramid" in args:
            self.qty = 8
            self.avg = COMBINED
            self.live_legs = [T1_TP1, T1_RUN, T2_TP1, T2_RUN]
            return 0, route_envelope.format_envelope(ADD_ROUTE, "SENT")
        if "--modify" in args:
            self.pair = list(PAIR_IDS)
            return 0, ("MODIFY VERIFIED: structure\n" + ae.MODIFY_BRACKET_MARKER
                       + json.dumps({"account": fx.ACC, "symbol": fx.SYM, "action": "BUY",
                                     "qty": self.qty,
                                     "sl": float(args[args.index("--sl") + 1]),
                                     "tp": float(args[args.index("--tp") + 1]),
                                     "consolidated": True,
                                     "accounts": [{"accountId": fx.ACC,
                                                   "stopOrderId": PAIR_IDS[0],
                                                   "targetOrderId": PAIR_IDS[1],
                                                   "stopReceipt": "R:" + PAIR_IDS[0],
                                                   "targetReceipt": "R:" + PAIR_IDS[1],
                                                   "ocoGroupId": "G"}]}, sort_keys=True))
        if "--flatten" in args:
            self.qty = 0
            self.live_legs = []
            self.pair = None
            return 0, "FLATTEN VERIFIED"
        return 0, "stub"


def cycle(world, path, price, *, low=None, scenario=True):
    return ae._reconcile_one(
        bundle(price, low=low, scenario=scenario), True, dict(CFG),
        world.query, world.runner, path, None, world.orders, view,
        entry_claim_stub, management_claim_stub,
        fresh_price_query=lambda _symbol: price)


def rows_of(path):
    with open(path, encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def frozen(path):
    return ae._frozen_plan_record(rows_of(path), fx.SYM, account=fx.ACC)


def owned(world, path):
    """今の凍結プランで、今の建玉が所有されているか。"""
    record = frozen(path)
    plan = (record or {}).get("plan") or {}
    if str(plan.get("planKind") or "") != tranche.PLAN_KIND:
        return None
    return tranche.bind_composite(plan, world.query(fx.SYM), fx.orders(world.rows()),
                                  position_generation=GENERATION)


live_contract = execution_contract.CONTRACT["pyramid"]
before_contract = copy.deepcopy(live_contract)
tmp = tempfile.mkdtemp(prefix="nqx-r84-life-")
try:
    live_contract["dryRun"] = False
    path = os.path.join(tmp, "life.jsonl")
    ae._append_ledger({"key": "POSITION_GENERATION:" + fx.ACC + ":1:" + str(IDENTITY),
                       "status": "POSITION_GENERATION", "generation": 1,
                       "identity": IDENTITY, "open": True, "accountId": fx.ACC,
                       "symbol": fx.SYM, "side": "SHORT"}, path)
    ae._append_ledger({"key": BASE_PLAN["entryKey"], "entryKey": BASE_PLAN["entryKey"],
                       "status": "ENTRY_SENT", "action": "ENTRY", "plan": BASE_PLAN,
                       "routeState": "SENT", "routeSnapshot": BASE_ROUTE}, path)
    world = World()

    print("--- 1. 追撃を送ってコミットする ---")
    notes = cycle(world, path, ADD_PRICE)
    sent = [args for args, confirm in world.calls if confirm]
    check("追撃が 1 回だけ送られる",
          len([a for a in sent if "--pyramid" in a]) == 1, str(sent))
    check("建玉は 8 枚になった", world.qty == 8)
    record = frozen(path)
    check("凍結プランが合成プランへ切り替わる",
          (record or {}).get("action") == ae.PYRAMID_COMMIT, str((record or {}).get("action")))
    binding = owned(world, path)
    check("8 枚が所有されている",
          binding and binding["owned"] and binding["expectedQty"] == 8,
          str(binding and binding.get("reason")))
    check("位相は ACCUMULATION", binding and binding["phase"] == tranche.ACCUMULATION)

    print()
    print("--- 2. ACCUMULATION では張り替えない ---")
    before = len(world.calls)
    notes = cycle(world, path, 29430.0, low=29425.0)
    check("MODIFY は出ない",
          not any("--modify" in args for args, _c in world.calls[before:]), str(world.calls[before:]))
    check("TP1 が生きている間は建値移動を明示的に見送る",
          any("BE deferred" in note for note in notes), str(notes))
    check("それでも所有は続く", (owned(world, path) or {}).get("owned") is True)

    print()
    print("--- 3. T1 の TP1 が約定(建玉 6 枚)---")
    world.qty = 6
    world.live_legs = [T1_RUN, T2_TP1, T2_RUN]
    binding = owned(world, path)
    check("6 枚でも所有が続く(枚数の集合ではなく構造で導出)",
          binding and binding["owned"] and binding["expectedQty"] == 6,
          str(binding and binding.get("reason")))
    check("まだ ACCUMULATION(T2 の TP1 が生きている)",
          binding and binding["phase"] == tranche.ACCUMULATION)
    before = len(world.calls)
    cycle(world, path, 29420.0, low=29415.0)
    check("この段でも MODIFY は出ない",
          not any("--modify" in args for args, _c in world.calls[before:]))

    print()
    print("--- 4. T2 の TP1 も約定(建玉 4 枚)→ 統合 ---")
    world.qty = 4
    world.live_legs = [T1_RUN, T2_RUN]
    binding = owned(world, path)
    check("4 枚で RUNNERS へ", binding and binding["phase"] == tranche.RUNNERS
          and binding["expectedQty"] == 4, str(binding and binding.get("reason")))
    before = len(world.calls)
    notes = cycle(world, path, 29420.0, low=29405.0)
    modify = [args for args, confirm in world.calls[before:]
              if confirm and "--modify" in args]
    check("統合 MODIFY が 1 回だけ出る", len(modify) == 1, str(world.calls[before:]))
    check("--pyramid-consolidate が付く", "--pyramid-consolidate" in modify[0])
    record = frozen(path)
    check("畳んだプランが再凍結される",
          (record or {}).get("action") == ae.PYRAMID_CONSOLIDATED,
          str((record or {}).get("action")))
    collapsed = record["plan"]
    check("トランシェは 1 本・RUNNER 4 枚",
          len(collapsed["tranches"]) == 1 and collapsed["qty"] == 4,
          str(collapsed.get("qty")))
    binding = owned(world, path)
    check("**畳んだ後も所有が続く**(ここが落ちると実弾が管理外)",
          binding and binding["owned"] and binding["expectedQty"] == 4,
          str(binding and binding.get("reason")))
    first_stop = collapsed["consolidation"]["sl"]

    print()
    print("--- 5. トレールが進む ---")
    # 安値 29350 + トレール距離 18.75 = 29368.75。現在値 29360 + バッファ 4pt = 29364 の
    # 「守れる側」にあるので、ここで初めてトレールが現在値を追い越さずに進む。
    before = len(world.calls)
    notes = cycle(world, path, 29360.0, low=29350.0)
    trail = [args for args, confirm in world.calls[before:]
             if confirm and "--modify" in args]
    check("さらに MODIFY が出る", len(trail) == 1, str(notes))
    new_stop = float(trail[0][trail[0].index("--sl") + 1])
    check("SL は利益方向にだけ動く(SELL なので下がる)",
          new_stop < first_stop, f"{new_stop} vs {first_stop}")
    check("トレールは 安値+距離(建値の床ではない)",
          abs(new_stop - (29350.0 + 18.75)) < 1e-9, str(new_stop))
    check("所有は続く", (owned(world, path) or {}).get("owned") is True)
    before = len(world.calls)
    cycle(world, path, 29360.0, low=29350.0)
    check("同じ SL を二度送らない(冪等)",
          not any(confirm and "--modify" in args for args, confirm in world.calls[before:]),
          str(world.calls[before:]))

    print()
    print("--- 6. 構造 SL を割ったら撤退する ---")
    before = len(world.calls)
    notes = cycle(world, path, 29520.0, low=29520.0)
    flat = [args for args, confirm in world.calls[before:] if confirm and "--flatten" in args]
    check("FLATTEN が送られる", len(flat) == 1, str(notes))
    check("口座指定つき", "--account" in flat[0], str(flat[0]))
    check("建玉は 0 になった", world.qty == 0)

    print()
    print("--- 7. FLAT 後に台帳が壊れていない ---")
    rows = rows_of(path)
    statuses = [row["status"] for row in rows]
    check("台帳は最後まで読める", ae._read_ledger(path)[1] is None)
    check("HALT は残っていない", ae._has_halt(rows) is None, str(
        (ae._has_halt(rows) or {}).get("reason")))
    check("WAL も残っていない", ae._pyramid_wal(rows, fx.SYM, fx.ACC) is None)
    check("遷移が順に並んでいる",
          statuses.count(ae.PYRAMID_CLAIMED) == 1 and statuses.count(ae.PYRAMID_SENT) == 1
          and statuses.count("FLATTEN_SENT") == 1, str(statuses))
    notes = cycle(world, path, 29520.0)
    check("FLAT の周期は合成プランを拾わない(R71 の境界)",
          frozen(path) is None or (frozen(path) or {}).get("plan", {}).get("planKind")
          != tranche.PLAN_KIND, str((frozen(path) or {}).get("status")))

    print()
    print("--- 8. 戦績(trade_journal)がトランシェを引ける ---")
    by_order, timeline = trade_journal.plan_lookup(rows, fx.ACC, fx.SYM)
    for leg in (T1_TP1, T1_RUN, T2_TP1, T2_RUN):
        check(f"{leg['orderId']} がプランへ引ける", leg["orderId"] in by_order)
    check("統合後の 1 組も引ける", all(pid in by_order for pid in PAIR_IDS), sorted(by_order))
    composite = by_order[T2_TP1["orderId"]]
    targets = dict((name, price) for name, price in trade_journal._plan_targets(composite))
    check("両トランシェの TP に名前が付く",
          any(abs(p - 29440.0) < 1e-9 for p in targets.values())
          and any(abs(p - 29400.0) < 1e-9 for p in targets.values()), str(targets))
    trade = {"side": "SELL", "entry": COMBINED, "qty": 8, "symbol": fx.SYM,
             "openedAt": FILLED_AT.isoformat(), "closedAt": NOW.isoformat(),
             "legs": [{"price": 29440.0, "qty": 2}, {"price": 29400.0, "qty": 2},
                      {"price": 29520.0, "qty": 4}]}
    labelled = trade_journal.label_legs(trade, composite)
    check("どちらのトランシェの TP1 が落ちたかまで名前で分かる",
          [row["id"] for row in labelled][:2] == ["TP1@T1", "TP1@T2"], str(labelled))
    check("古いトランシェの広い構造 SL も SL と読まれる(EXIT に落ちない)",
          labelled[2]["id"] == "SL", str(labelled))
    check("枚数の合計は建玉と一致する(nqx_state.normalize_legs の要求)",
          sum(row["qty"] for row in labelled) == 8, str(labelled))
    # Worker の validateResult は leg.id を 24 文字で切る。切られると別の脚と
    # 見分けが付かなくなるので、名前は必ずその中へ収める。
    check("脚の名前は Worker の 24 文字制限に収まる",
          all(len(row["id"]) <= 24 for row in labelled),
          str([row["id"] for row in labelled]))
    normalized = __import__("nqx_state").normalize_legs(
        labelled, "SHORT", COMBINED, 29520.0, 8)
    check("nqx_state.normalize_legs を通る(result が publish できる形)",
          normalized is not None and sum(row["qty"] for row in normalized) == 8,
          str(normalized))
finally:
    live_contract.clear()
    live_contract.update(before_contract)
    shutil.rmtree(tmp, ignore_errors=True)

print()
print(f"合計: PASS {PASS[0]} / FAIL {FAIL[0]}")
sys.exit(1 if FAIL[0] else 0)

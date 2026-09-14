# -*- coding: utf-8 -*-
"""ULTRA execution envelope — 大枚数・複数口座を通すための契約。

通常経路(2枚固定・$240・単一口座)は一切緩めない。ULTRA は **明示宣言された
ときだけ** 別のエンベロープで判定される、という分離をここで固定する。

固定する不変条件:

  * ULTRA を宣言しない経路は今までどおり2枚固定・$240 のまま
  * ULTRA の上限(口座あたり枚数・合計枚数・口座数・口座別損失・合計損失)
  * 比率分割は端数を runner へ寄せ、Python と Durable Object で一致する
  * リスク上限の正本は残ドローダウンで、設定漏れは通さない
  * 所有権は 0 / TP1脚 / RUNNER脚 / 合計 の枚数だけを認める

ブローカー・Telegram・外部送信には一切触れない。
"""
import json
import os
import subprocess
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import broker_status        # noqa: E402
import execution_contract   # noqa: E402
import ownership_binder     # noqa: E402
import ultra_mode           # noqa: E402

FAILED = []


def check(name, condition, detail=""):
    print(("  OK   " if condition else "  FAIL ") + name
          + (f" ({detail})" if detail and not condition else ""))
    if not condition:
        FAILED.append(name)


SIGNAL = {"side": "SHORT", "entry": 30126.00, "stop": 30147.00, "target": 30076.00}
ACCOUNTS = [
    {"id": "APEX-01", "cap": 180.0, "buffer": 2500.0, "profitTarget": 3000.0},
    {"id": "APEX-02", "cap": 180.0, "buffer": 3000.0, "profitTarget": 6000.0},
    {"id": "APEX-03", "cap": 180.0, "buffer": 5000.0, "profitTarget": 9000.0},
]
ENVELOPE = execution_contract.CONTRACT["ultra"]


def test_normal_path_keeps_every_original_limit():
    risk = execution_contract.CONTRACT["risk"]
    check("通常の固定枚数は2枚のまま", int(risk["fixedQty"]) == 2, risk["fixedQty"])
    check("通常の最大枚数は2枚のまま", int(risk["maxQty"]) == 2, risk["maxQty"])
    check("通常のリスク上限は $240 のまま",
          float(risk["defaultCapDollars"]) == 240.0, risk["defaultCapDollars"])
    check("通常モードの口座数は上限なし",
          execution_contract.CONTRACT["accountMode"]["maxAccounts"] is None)
    check("ULTRA は明示宣言が必要", ENVELOPE["requiresExplicitMode"] is True)


def test_proportional_split_puts_the_remainder_on_the_runner():
    cases = {30: [15, 15], 60: [30, 30], 90: [45, 45], 9: [4, 5], 5: [2, 3], 4: [2, 2], 2: [1, 1]}
    for qty, expected in cases.items():
        check(f"{qty}枚 → {expected}", execution_contract.ultra_split(qty) == expected,
              execution_contract.ultra_split(qty))
    for qty in (0, 1, -5, None, "x"):
        check(f"分割できない {qty!r} は None", execution_contract.ultra_split(qty) is None)
    for qty, legs in cases.items():
        check(f"{qty}枚の分割は合計と一致", sum(legs) == qty)
        check(f"{qty}枚は runner が端数を持つ", legs[1] >= legs[0])


def test_sample_plan_passes_the_ultra_envelope():
    plan = ultra_mode.build_plan(SIGNAL, ACCOUNTS)
    result = execution_contract.ultra_evaluate(plan)
    check("サンプル計画はエンベロープを通る", result["ok"] is True, result["blockers"])
    check("合計 180枚", result["totalQty"] == 180, result["totalQty"])
    check("合計リスク $7,560", result["totalRiskDollars"] == 7560.0, result["totalRiskDollars"])
    legs = {row["id"]: row["legs"] for row in result["accounts"]}
    check("APEX-01 は 15/15", legs["APEX-01"] == [15, 15], legs)
    check("APEX-03 は 45/45", legs["APEX-03"] == [45, 45], legs)


def test_every_ultra_limit_stops_the_whole_plan():
    """1口座でも上限を超えたら計画全体を止める(部分発注は作らない)。"""
    over_qty = [dict(ACCOUNTS[0], profitTarget=3000.0 * 40, buffer=500000.0)]
    result = execution_contract.ultra_evaluate(ultra_mode.build_plan(SIGNAL, over_qty))
    check("口座あたり枚数の上限で止まる",
          not result["ok"] and "ULTRA_QTY_EXCEEDS_ACCOUNT_MAX" in result["blockers"],
          result["blockers"])

    over_risk = [dict(ACCOUNTS[0], profitTarget=12000.0, buffer=100000.0)]
    result = execution_contract.ultra_evaluate(ultra_mode.build_plan(SIGNAL, over_risk))
    check("口座あたり損失の上限で止まる",
          not result["ok"] and "ULTRA_ACCOUNT_RISK_EXCEEDS_CONTRACT" in result["blockers"],
          result["blockers"])

    many = [dict(ACCOUNTS[0], id=f"A{index}") for index in range(int(ENVELOPE["maxAccounts"]) + 1)]
    result = execution_contract.ultra_evaluate(ultra_mode.build_plan(SIGNAL, many))
    check("口座数の上限で止まる",
          not result["ok"] and "ULTRA_ACCOUNTS_EXCEED_CONTRACT" in result["blockers"],
          result["blockers"])

    check("ELIGIBLE が無ければ止まる",
          execution_contract.ultra_evaluate({"accounts": []})["blockers"]
          == ["ULTRA_NO_ELIGIBLE_ACCOUNT"])


def test_drawdown_is_the_authoritative_risk_cap():
    thin = [dict(ACCOUNTS[0], buffer=1000.0)]
    plan = ultra_mode.build_plan(SIGNAL, thin)
    check("残DD 不足は ULTRA 判定で既に INELIGIBLE",
          plan["accounts"][0]["verdict"] == "INELIGIBLE")
    check("INELIGIBLE だけの計画はエンベロープも通さない",
          execution_contract.ultra_evaluate(plan)["ok"] is False)

    # verdict を偽って ELIGIBLE を名乗っても、エンベロープが残DDを見る。
    forged = {"accounts": [{"id": "APEX-01", "verdict": "ELIGIBLE", "qty": 30,
                            "projectedLoss": 1260.0, "buffer": 1000.0}]}
    result = execution_contract.ultra_evaluate(forged)
    check("偽の ELIGIBLE も残DDで止まる",
          not result["ok"] and "ULTRA_DRAWDOWN_EXCEEDED" in result["blockers"],
          result["blockers"])

    missing = {"accounts": [{"id": "APEX-01", "verdict": "ELIGIBLE", "qty": 30,
                             "projectedLoss": 1260.0}]}
    check("残DD 未設定は通さない",
          "ULTRA_DRAWDOWN_UNAVAILABLE" in execution_contract.ultra_evaluate(missing)["blockers"])


def test_ownership_lifecycle_generalises_to_the_split_quantities():
    plan = {"scenarioId": "U", "symbol": "MNQU6", "side": "BUY", "qty": 30,
            "entry": 20000.0, "stop": 19980.0, "targets": [20030.0, 20060.0],
            "entryOrderType": "LIMIT", "accountScope": ["APEX-01"], "planVersion": "U1",
            "legs": [{"id": "TP1", "qty": 15, "target": 20030.0},
                     {"id": "RUNNER", "qty": 15, "target": 20060.0}]}
    route = [{"accountId": "APEX-01", "legId": "TP1", "state": "ACCEPTED",
              "orderId": "O1", "receipt": "R1"},
             {"accountId": "APEX-01", "legId": "RUNNER", "state": "ACCEPTED",
              "orderId": "O2", "receipt": "R2"}]

    def order_row(leg, oid, rid, status, qty):
        return {"accountId": "APEX-01", "symbol": "MNQU6", "legId": leg, "orderId": oid,
                "receipt": rid, "action": "BUY", "qty": qty, "orderType": "LIMIT",
                "limitPrice": 20000.0, "status": status}

    def orders(tp1_state, runner_state, tp1_qty=15, runner_qty=15):
        rows = [order_row("TP1", "O1", "R1", tp1_state, tp1_qty),
                order_row("RUNNER", "O2", "R2", runner_state, runner_qty)]
        return {"verified": True, "orders": rows,
                "activeOrders": [r for r in rows
                                 if r["status"] in broker_status.BROKER_ACTIVE_STATES]}

    def position(qty, leg="TP1"):
        row = {"verified": True, "accountId": "APEX-01", "symbol": "MNQU6",
               "side": "LONG" if qty else "FLAT", "qty": qty}
        if qty:
            row.update({"orderId": {"TP1": "O1", "RUNNER": "O2"}[leg],
                        "receipt": {"TP1": "R1", "RUNNER": "R2"}[leg],
                        "filledAt": "2026-08-23T12:00:00Z", "initialQty": qty,
                        "avgEntry": 20000.0})
        return row

    def bind(pos, view, use_plan=plan):
        raw = broker_status.position_identity(pos)
        return ownership_binder.bind(use_plan, route, pos, view, route_state="SENT",
                                     position_generation=(f"PG:1:{raw}" if raw else None))

    check("qty=0 は RESTING",
          bind(position(0), orders("WORKING", "WORKING"))["state"] == "RESTING")
    check("qty=15(TP1脚)は PARTIAL_FILL",
          bind(position(15, "TP1"), orders("FILLED", "WORKING"))["state"] == "PARTIAL_FILL")
    check("qty=30(合計)は OWNED_FULL",
          bind(position(30, "RUNNER"), orders("FILLED", "FILLED"))["state"] == "OWNED_FULL")
    stray = bind(position(1), orders("FILLED", "WORKING"))
    check("分割外の枚数は所有しない",
          stray["owned"] is False and "outside split lifecycle" in stray["reason"], stray["reason"])
    mismatch = bind(position(15, "TP1"), orders("FILLED", "WORKING", tp1_qty=14))
    check("脚枚数が食い違う建玉は所有しない", mismatch["owned"] is False, mismatch["reason"])

    odd = {**plan, "qty": 9,
           "legs": [{"id": "TP1", "qty": 4, "target": 20030.0},
                    {"id": "RUNNER", "qty": 5, "target": 20060.0}]}
    check("奇数分割 9=4+5 の部分約定",
          bind(position(4, "TP1"), orders("FILLED", "WORKING", 4, 5), odd)["state"] == "PARTIAL_FILL")
    check("奇数分割 9=4+5 の全量",
          bind(position(9, "RUNNER"), orders("FILLED", "FILLED", 4, 5), odd)["state"] == "OWNED_FULL")


def test_python_and_durable_object_agree_on_the_envelope():
    script = """
import { evaluateUltraPlan, ultraSplit } from './cloudflare/src/state_machine.js';
let raw = '';
process.stdin.on('data', (chunk) => { raw += chunk; });
process.stdin.on('end', () => {
  const input = JSON.parse(raw);
  console.log(JSON.stringify({
    plan: evaluateUltraPlan(input.plan),
    splits: input.splits.map((qty) => ultraSplit(qty)),
  }));
});
"""
    plan = ultra_mode.build_plan(SIGNAL, ACCOUNTS)
    splits = [30, 60, 90, 9, 5, 2, 1, 0]
    proc = subprocess.run(["node", "--input-type=module", "-e", script], cwd=BASE,
                          input=json.dumps({"plan": plan, "splits": splits}),
                          text=True, capture_output=True)
    if proc.returncode != 0:
        check("Durable Object 側を実行できる", False, proc.stderr[-300:])
        return
    js = json.loads(proc.stdout)
    py = execution_contract.ultra_evaluate(plan)
    check("ok 一致", js["plan"]["ok"] == py["ok"])
    check("blockers 一致", js["plan"]["blockers"] == py["blockers"],
          f"{js['plan']['blockers']} vs {py['blockers']}")
    check("totalQty 一致", js["plan"]["totalQty"] == py["totalQty"])
    check("totalRiskDollars 一致", js["plan"]["totalRiskDollars"] == py["totalRiskDollars"])
    check("口座別の脚配分 一致",
          [row["legs"] for row in js["plan"]["accounts"]] == [row["legs"] for row in py["accounts"]])
    check("ultraSplit 一致",
          js["splits"] == [execution_contract.ultra_split(qty) for qty in splits],
          js["splits"])


test_normal_path_keeps_every_original_limit()
test_proportional_split_puts_the_remainder_on_the_runner()
test_sample_plan_passes_the_ultra_envelope()
test_every_ultra_limit_stops_the_whole_plan()
test_drawdown_is_the_authoritative_risk_cap()
test_ownership_lifecycle_generalises_to_the_split_quantities()
test_python_and_durable_object_agree_on_the_envelope()

if FAILED:
    print(f"FAILED: {len(FAILED)} -> {', '.join(FAILED)}")
    raise SystemExit(1)
print("ALL PASS (test_ultra_envelope)")

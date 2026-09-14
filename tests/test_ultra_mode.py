# -*- coding: utf-8 -*-
"""ULTRA mode sizing contract.

ULTRA は通常シグナルの枚数を無視し、口座ごとの利益目標と残ドローダウンから
必要枚数を再計算して強制的に上書きするモード。ここで固定するのは:

  * 利用者が示したサンプルシグナルの数値を1つ残らず再現すること
  * 判定 (verdict) は利益目標と残ドローダウンだけで決まること
  * その枚数を現行の execution_contract が通さない事実を隠さないこと
  * Python(正本)と Mini App(表示)の式が完全に一致すること

外部送信・ブローカー・Telegram には一切触れない。
"""
import json
import math
import os
import subprocess
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import nqx_state          # noqa: E402
import ultra_mode         # noqa: E402

FAILED = []


def check(name, condition, detail=""):
    print(("  OK   " if condition else "  FAIL ") + name
          + (f" ({detail})" if detail and not condition else ""))
    if not condition:
        FAILED.append(name)


# 利用者が提示したサンプルシグナルそのまま。
SIGNAL = {"side": "SHORT", "entry": 30126.00, "stop": 30147.00, "target": 30076.00}
ACCOUNTS = [
    {"id": "APEX-01", "cap": 180.0, "buffer": 2500.0, "profitTarget": 3000.0},
    {"id": "APEX-02", "cap": 180.0, "buffer": 3000.0, "profitTarget": 6000.0},
    {"id": "APEX-03", "cap": 180.0, "buffer": 5000.0, "profitTarget": 9000.0},
]
EXPECTED = {
    "APEX-01": {"qty": 30, "profit": 3000.0, "loss": 1260.0},
    "APEX-02": {"qty": 60, "profit": 6000.0, "loss": 2520.0},
    "APEX-03": {"qty": 90, "profit": 9000.0, "loss": 3780.0},
}


def test_sample_signal_reproduces_the_specified_numbers():
    plan = ultra_mode.build_plan(SIGNAL, ACCOUNTS)
    geometry = plan["geometry"]
    check("SL幅 21pt", geometry["slPoints"] == 21.0, geometry["slPoints"])
    check("TP幅 50pt", geometry["tpPoints"] == 50.0, geometry["tpPoints"])
    check("RR 約2.38", geometry["rr"] == 2.38, geometry["rr"])
    check("SHORT は SELL へ正規化される", geometry["side"] == "SELL")
    for row in plan["accounts"]:
        want = EXPECTED[row["id"]]
        check(f"{row['id']} 必要枚数 {want['qty']}枚", row["qty"] == want["qty"], row["qty"])
        check(f"{row['id']} 想定利益 ${want['profit']:,.0f}",
              row["projectedProfit"] == want["profit"], row["projectedProfit"])
        check(f"{row['id']} 想定損失 ${want['loss']:,.0f}",
              row["projectedLoss"] == want["loss"], row["projectedLoss"])
        check(f"{row['id']} ELIGIBLE", row["verdict"] == "ELIGIBLE", row["reasons"])
    check("合計 180枚", plan["totalQty"] == 180, plan["totalQty"])
    check("MNQ 1pt = $2.00", plan["pointValue"] == 2.0)


def test_ultra_off_keeps_the_signal_quantity():
    plan = ultra_mode.build_plan(SIGNAL, ACCOUNTS, enabled=False, signal_qty=1)
    check("OFF は通常枚数をそのまま返す", plan["totalQty"] == 1 and plan["signalQty"] == 1)
    check("OFF は口座別計算をしない", plan["accounts"] == [])
    check("OFF は routable を主張しない", plan["routable"] is False)
    on = ultra_mode.build_plan(SIGNAL, ACCOUNTS, enabled=True, signal_qty=1)
    check("ON は通常枚数 1 を無視する", on["totalQty"] == 180 and on["signalQty"] == 1.0)


def test_verdict_uses_drawdown_not_the_per_trade_cap():
    """判定は利益目標と残ドローダウンだけ。cap は ULTRA では判定に使わない。"""
    tight_cap = [dict(ACCOUNTS[0], cap=60.0)]
    plan = ultra_mode.build_plan(SIGNAL, tight_cap)
    row = plan["accounts"][0]
    check("1トレード上限 $60 でも ELIGIBLE のまま", row["verdict"] == "ELIGIBLE", row["reasons"])
    check("1トレード上限は ULTRA の routing 判定にも使わない",
          not any("ACCOUNT_TRADE_CAP" in blocker for blocker in row["contractBlockers"]),
          row["contractBlockers"])

    thin = [dict(ACCOUNTS[0], buffer=1000.0)]
    row = ultra_mode.build_plan(SIGNAL, thin)["accounts"][0]
    check("残DD $1,000 に対し損失 $1,260 は INELIGIBLE",
          row["verdict"] == "INELIGIBLE" and "DRAWDOWN_EXCEEDED" in row["reasons"], row)
    check("超過でも枚数と金額は見せる", row["qty"] == 30 and row["projectedLoss"] == 1260.0)

    exact = [dict(ACCOUNTS[0], buffer=1260.0)]
    check("残DD ちょうどは ELIGIBLE",
          ultra_mode.build_plan(SIGNAL, exact)["accounts"][0]["verdict"] == "ELIGIBLE")

    blown = [dict(ACCOUNTS[0], buffer=0.0)]
    row = ultra_mode.build_plan(SIGNAL, blown)["accounts"][0]
    check("残DD 0 は ACCOUNT_BLOWN", "ACCOUNT_BLOWN" in row["reasons"], row["reasons"])


def test_missing_profit_target_never_invents_a_quantity():
    no_target = [{"id": "APEX-09", "cap": 180.0, "buffer": 5000.0}]
    row = ultra_mode.build_plan(SIGNAL, no_target)["accounts"][0]
    check("利益目標が無ければ枚数を作らない", row["qty"] is None)
    check("理由は PROFIT_TARGET_MISSING", "PROFIT_TARGET_MISSING" in row["reasons"])
    check("INELIGIBLE で止まる", row["verdict"] == "INELIGIBLE")


def test_broken_signals_fail_closed():
    cases = {
        "方向なし": {"entry": 30126, "stop": 30147, "target": 30076},
        "価格欠落": {"side": "SHORT", "entry": 30126, "stop": 30147},
        "並びが逆": {"side": "SHORT", "entry": 30126, "stop": 30076, "target": 30147},
        "SL=Entry": {"side": "SHORT", "entry": 30126, "stop": 30126, "target": 30076},
    }
    for label, signal in cases.items():
        plan = ultra_mode.build_plan(signal, ACCOUNTS)
        check(f"{label} は計算しない",
              plan["geometry"]["valid"] is False
              and all(row["qty"] is None for row in plan["accounts"]), plan["geometry"])


def test_ultra_routes_inside_its_envelope_and_stops_outside_it():
    """ULTRA エンベロープを開いた後の routing 判定。

    判定基準は ULTRA 側の上限だけ。通常経路の2枚固定・$240 を当ててしまうと
    ULTRA は定義上必ず不可になるので、そこへ戻っていないことを固定する。
    """
    import execution_contract

    plan = ultra_mode.build_plan(SIGNAL, ACCOUNTS)
    check("エンベロープ内なら計画全体が routable", plan["routable"] is True,
          [row["contractBlockers"] for row in plan["accounts"]])
    for row in plan["accounts"]:
        check(f"{row['id']} は routing 可", row["routable"] is True, row["contractBlockers"])
        check(f"{row['id']} に通常経路の上限を当てていない",
              not any(name in " ".join(row["contractBlockers"])
                      for name in ("FIXED_QTY_REQUIRED", "SINGLE_ACCOUNT_CONTRACT")),
              row["contractBlockers"])
        check(f"{row['id']} は比率分割を持つ",
              row["legs"] == execution_contract.ultra_split(row["qty"]), row["legs"])

    envelope = execution_contract.CONTRACT["ultra"]
    huge = [dict(ACCOUNTS[0], profitTarget=3000.0 * 40, buffer=500000.0)]
    row = ultra_mode.build_plan(SIGNAL, huge)["accounts"][0]
    check("口座あたり枚数の上限を超えたら routing 不可",
          row["routable"] is False
          and any("ULTRA_QTY_EXCEEDS_ACCOUNT_MAX" in b for b in row["contractBlockers"]),
          row["contractBlockers"])

    over_risk = [dict(ACCOUNTS[0], profitTarget=12000.0, buffer=100000.0)]
    row = ultra_mode.build_plan(SIGNAL, over_risk)["accounts"][0]
    check("口座あたり損失の上限を超えたら routing 不可",
          row["routable"] is False
          and any("ULTRA_ACCOUNT_RISK_EXCEEDS_CONTRACT" in b for b in row["contractBlockers"]),
          row["contractBlockers"])

    check("ULTRA エンベロープが有効", bool(envelope.get("enabled")))
    check("ULTRA モジュールに発注経路が無い",
          not any(name in dir(ultra_mode) for name in ("post", "send", "confirm", "route")))


def test_profit_target_flows_through_the_account_payload():
    env = {"CROSSTRADE_ACCOUNTS": "APEX-01,APEX-02",
           "LIFELINE_APEX-01": "2500", "LIFELINE_APEX-02": "3000",
           "TARGET_APEX-01": "3000", "RISK_APEX-01": "180"}
    payload = nqx_state.build_accounts_payload(env=env)
    rows = {row["id"]: row for row in payload["list"]}
    check("TARGET_* が profitTarget として載る", rows["APEX-01"]["profitTarget"] == 3000.0)
    check("未設定の口座には profitTarget を作らない", "profitTarget" not in rows["APEX-02"])
    check("既存の cap/buffer は変わらない",
          rows["APEX-01"]["cap"] == 180.0 and rows["APEX-01"]["buffer"] == 2500.0)
    for bad in ("0", "-100", "abc"):
        env_bad = dict(env, TARGET_APEX_01=bad)
        env_bad["TARGET_APEX-01"] = bad
        rows_bad = {row["id"]: row for row in nqx_state.build_accounts_payload(env=env_bad)["list"]}
        check(f"不正な TARGET ({bad}) は無視する", "profitTarget" not in rows_bad["APEX-01"])


def test_python_and_mini_app_agree_exactly():
    """表示と実発注が食い違わないよう、式の一致を機械で確かめる。"""
    script = """
import { buildPlan } from './telegram_mini_app/ultra.js';
let raw = '';
process.stdin.on('data', (chunk) => { raw += chunk; });
process.stdin.on('end', () => {
  const input = JSON.parse(raw);
  console.log(JSON.stringify(buildPlan(input.signal, input.accounts, { signalQty: 1 })));
});
"""
    payload = json.dumps({"signal": SIGNAL, "accounts": ACCOUNTS})
    # encoding を明示する。text=True だけだと locale(この環境は cp932)で復号を
    # 試み、日本語を含む出力で読み取りスレッドが落ちて stdout が None になる。
    # _hermetic.dry_run と同じ規律。
    proc = subprocess.run(["node", "--input-type=module", "-e", script],
                          cwd=BASE, input=payload, text=True, capture_output=True,
                          encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        check("Mini App の式を実行できる", False, proc.stderr[-300:])
        return
    js = json.loads(proc.stdout)
    py = ultra_mode.build_plan(SIGNAL, ACCOUNTS, signal_qty=1)
    check("geometry 一致", js["geometry"] == {k: v for k, v in py["geometry"].items()},
          f"{js['geometry']} vs {py['geometry']}")
    check("合計枚数 一致", js["totalQty"] == py["totalQty"])
    check("routable 一致", js["routable"] == py["routable"])
    for js_row, py_row in zip(js["accounts"], py["accounts"]):
        for field in ("id", "qty", "projectedProfit", "projectedLoss", "verdict",
                      "reasons", "contractBlockers", "routable", "drawdownUsedPct"):
            check(f"{py_row['id']}.{field} 一致", js_row[field] == py_row[field],
                  f"{js_row[field]} vs {py_row[field]}")


def test_required_qty_rounds_up_to_reach_the_target():
    # 端数は切り上げ。目標に「届かない枚数」を返してはいけない。
    check("端数は切り上げ", ultra_mode.required_qty(3001, 50, 2.0) == 31)
    check("ちょうどは切り上げない", ultra_mode.required_qty(3000, 50, 2.0) == 30)
    check("目標未満でも最低1枚", ultra_mode.required_qty(10, 50, 2.0) == 1)
    for bad in (0, -1, None, "x"):
        check(f"不正な目標 {bad!r} は None", ultra_mode.required_qty(bad, 50, 2.0) is None)
    check("TP幅 0 は None", ultra_mode.required_qty(3000, 0, 2.0) is None)
    # 切り上げの定義そのものを式で確認する
    check("式は ceil(target / (tp * pointValue))",
          all(ultra_mode.required_qty(t, 50, 2.0) == math.ceil(t / 100.0)
              for t in (1, 99, 100, 101, 3000, 9001)))


def test_mini_app_contract_constants_cannot_drift():
    """ultra.js は契約値をハードコードしている。JSON との乖離を機械で止める。"""
    import re

    import execution_contract

    path = os.path.join(BASE, "telegram_mini_app", "ultra.js")
    with open(path, encoding="utf-8") as handle:
        source = handle.read()
    match = re.search(r"const CONTRACT = " + chr(123) + r"([^" + chr(125) + r"]*)" + chr(125), source)
    check("ultra.js の CONTRACT を読める", match is not None)
    if not match:
        return
    values = dict(re.findall(r"(" + chr(92) + r"w+):" + chr(92) + r"s*([0-9.]+|null)",
                             match.group(1)))
    contract = execution_contract.CONTRACT
    expected = {
        "maxQty": float(contract["risk"]["maxQty"]),
        "fixedQty": float(contract["risk"]["fixedQty"]),
        "riskCapDollars": float(contract["risk"]["defaultCapDollars"]),
    }
    for key, want in expected.items():
        got = values.get(key)
        check("ultra.js " + key + " が契約と一致",
              got is not None and got != "null" and float(got) == want,
              str(got) + " vs " + str(want))
    # maxAccounts は null(上限なし)を取りうるので数値比較から外して個別に見る。
    want_accounts = contract["accountMode"]["maxAccounts"]
    got_accounts = values.get("maxAccounts")
    if want_accounts is None:
        check("ultra.js maxAccounts が契約と一致(上限なし)",
              got_accounts == "null", str(got_accounts) + " vs null")
    else:
        check("ultra.js maxAccounts が契約と一致",
              got_accounts is not None and got_accounts != "null"
              and float(got_accounts) == float(want_accounts),
              str(got_accounts) + " vs " + str(want_accounts))
    point = re.search(r"export const POINT_VALUE = ([0-9.]+)", source)
    check("ultra.js POINT_VALUE が契約と一致",
          point is not None and float(point.group(1)) == float(contract["risk"]["pointValue"]))


test_sample_signal_reproduces_the_specified_numbers()
test_ultra_off_keeps_the_signal_quantity()
test_verdict_uses_drawdown_not_the_per_trade_cap()
test_missing_profit_target_never_invents_a_quantity()
test_broken_signals_fail_closed()
test_ultra_routes_inside_its_envelope_and_stops_outside_it()
test_profit_target_flows_through_the_account_payload()
test_required_qty_rounds_up_to_reach_the_target()
test_python_and_mini_app_agree_exactly()
test_mini_app_contract_constants_cannot_drift()

if FAILED:
    print(f"FAILED: {len(FAILED)} -> {', '.join(FAILED)}")
    raise SystemExit(1)
print("ALL PASS (test_ultra_mode)")

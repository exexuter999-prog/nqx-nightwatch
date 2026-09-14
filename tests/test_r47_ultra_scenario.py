# -*- coding: utf-8 -*-
"""R47: 口座別 ULTRA(publish 時サイジング)の検証。

ネットワークを使わない。JS 側との整合は node の子プロセスで本物の
検証器に通す。ユーザー決定(2026-08-30): 達成不能なら見送り(WATCH)。

    python tests/test_r47_ultra_scenario.py
"""
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

for _stream in ("stdout", "stderr"):
    _file = getattr(sys, _stream, None)
    if _file is not None and hasattr(_file, "reconfigure"):
        try:
            _file.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

import autotrade_engine    # noqa: E402
import execution_contract  # noqa: E402
import monitor_publish     # noqa: E402
import nqx_state           # noqa: E402

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
ULTRA_SOURCE = str(execution_contract.CONTRACT["ultra"]["riskCapSource"])


def ultra_raw(qty=90, legs=(45, 45)):
    return {
        "scenarioId": "sc-ultra-1", "side": "SELL", "qty": qty,
        "entry": 30126.0, "stop": 30147.0, "target": 30105.0,
        "targets": [30105.0, 30076.0],
        "legs": [{"id": "TP1", "qty": legs[0], "target": 30105.0},
                 {"id": "RUNNER", "qty": legs[1], "target": 30076.0}],
        "planVersion": "R47-ULTRA-1", "state": "ACTIVE",
    }


MARKET = {"observedAt": NOW.isoformat(), "cvdAt": NOW.isoformat()}

# ================================================================
print("=" * 68)
print("1. build_scenario の ULTRA 経路")
print("=" * 68)

scenario = nqx_state.build_scenario(ultra_raw(), observed_at=NOW.isoformat(),
                                    ttl_minutes=10, symbol="MNQU6", ultra=True)
scenario["grade"] = "A"   # monitor_publish が publish 前に付ける(R6)
check("ULTRA 90枚のシナリオが組める", scenario.get("qty") == 90)
check("脚は 45/45 の比率分割", [leg["qty"] for leg in scenario["legs"]] == [45, 45])

try:
    nqx_state.build_scenario(ultra_raw(), observed_at=NOW.isoformat(),
                             ttl_minutes=10, symbol="MNQU6")
    check("ultra=False では 90枚は拒否", False)
except ValueError as exc:
    check("ultra=False では 90枚は拒否", "FIXED_QTY_REQUIRED" in str(exc))

try:
    nqx_state.build_scenario(ultra_raw(qty=101, legs=(50, 51)),
                             observed_at=NOW.isoformat(), ttl_minutes=10,
                             symbol="MNQU6", ultra=True)
    check("エンベロープ外の枚数は拒否", False)
except ValueError as exc:
    check("エンベロープ外の枚数は拒否", "ULTRA_QTY_OUT_OF_ENVELOPE" in str(exc))

# ================================================================
print("=" * 68)
print("2. execution_contract.evaluate の ULTRA 分岐(producer / 下流)")
print("=" * 68)

CFG_ONE = {"CROSSTRADE_ACCOUNTS": "ACC-ULTRA-01"}
producer = execution_contract.evaluate(
    scenario, MARKET, None, None, cfg=CFG_ONE, ultra=True, ultra_buffer=4000.0)
check("producer 評価が orderable", producer["orderable"], str(producer["blockers"]))
check("リスク 3780 / 上限 4000", producer["riskDollars"] == 3780.0
      and producer["riskCapDollars"] == 4000.0)
check("riskCapSource が ULTRA の正本値", producer["riskCapSource"] == ULTRA_SOURCE)
check("accountScope が1口座", producer["accountScope"] == ["ACC-ULTRA-01"])

frozen = {**scenario, "executionContract": producer}
downstream = execution_contract.evaluate(
    frozen, MARKET, None, None, cfg={"CROSSTRADE_ACCOUNTS": "ACC-A,ACC-B"})
check("下流は凍結 riskCapSource から ULTRA を検出して orderable",
      downstream["orderable"], str(downstream["blockers"]))
check("下流でも scope は凍結された1口座", downstream["accountScope"] == ["ACC-ULTRA-01"])

thin = execution_contract.evaluate(
    scenario, MARKET, None, None, cfg=CFG_ONE, ultra=True, ultra_buffer=3000.0)
check("残DD不足は RISK_CAP_EXCEEDED", "RISK_CAP_EXCEEDED" in thin["blockers"])

two = execution_contract.evaluate(
    scenario, MARKET, None, None, cfg={"CROSSTRADE_ACCOUNTS": "A-111,B-222"},
    ultra=True, ultra_buffer=4000.0)
check("2口座スコープは ULTRA_SCOPE_NOT_SINGLE", "ULTRA_SCOPE_NOT_SINGLE" in two["blockers"])

normal = execution_contract.evaluate(
    {**ultra_raw(qty=2, legs=(1, 1)), "issuedAt": NOW.isoformat(),
     "observedAt": NOW.isoformat(), "grade": "A",
     "expiresAt": (NOW + timedelta(minutes=10)).isoformat()},
    MARKET, None, None, cfg=CFG_ONE)
check("通常経路の判定は不変(qty=2)", "FIXED_QTY_REQUIRED" not in normal["blockers"]
      and normal["riskCapDollars"] is not None)

# ================================================================
print("=" * 68)
print("3. Python↔JS の整合(本物の JS 検証器)")
print("=" * 68)


def node_eval(script):
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=os.path.join(BASE, "cloudflare"), timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip()[:400])
    return json.loads(result.stdout.strip())


verdict = node_eval(
    "import {validateScenario, evaluateExecutionContract} from './src/state_machine.js';"
    f"const raw = {json.dumps(frozen)};"
    "const checked = validateScenario(raw, {symbol: 'MNQU6'});"
    "if (!checked.ok) { console.log(JSON.stringify({ok:false, reason: checked.reason})); }"
    "else {"
    f"const market = {json.dumps(MARKET)};"
    "const c = evaluateExecutionContract(checked.scenario, market, null, null, Date.now());"
    "console.log(JSON.stringify({ok:true, orderable: c.orderable, blockers: c.blockers,"
    " risk: c.riskDollars, cap: c.riskCapDollars, source: c.riskCapSource}));"
    "}"
)
check("JS validateScenario が ULTRA シナリオを受理", verdict.get("ok"), str(verdict))
check("JS 契約評価も orderable", verdict.get("orderable"), str(verdict.get("blockers")))
check("JS のリスクと上限が Python と一致",
      verdict.get("risk") == 3780 and verdict.get("cap") == 4000)

# ================================================================
print("=" * 68)
print("4. autotrade_engine のプランと送信引数")
print("=" * 68)

plan = autotrade_engine.build_management_plan(frozen, cfg={"CROSSTRADE_ACCOUNTS": "ACC-ULTRA-01"})
check("engine プランが ULTRA を検出", plan.get("ultra") is True)
check("プラン脚が 45/45", [leg["qty"] for leg in plan["legs"]] == [45, 45])
check("ultraDrawdown が凍結上限", plan.get("ultraDrawdown") == 4000.0)

args = autotrade_engine._command_for_entry(plan, {})
check("order.py へ --ultra が付く", "--ultra" in args)
check("order.py へ --ultra-drawdown が付く",
      "--ultra-drawdown" in args and args[args.index("--ultra-drawdown") + 1] == "4000.0")
check("--qty は 90", args[args.index("--qty") + 1] == "90")

normal_plan = autotrade_engine.build_management_plan(
    {**ultra_raw(qty=2, legs=(1, 1)),
     "executionContract": {"riskCapDollars": 240, "riskCapSource": "RISK_X",
                           "accountScope": ["ACC-ULTRA-01"]}},
    cfg={"CROSSTRADE_ACCOUNTS": "ACC-ULTRA-01"})
check("通常プランは不変(ultra 無し・脚1/1)", normal_plan.get("ultra") is False
      and [leg["qty"] for leg in normal_plan["legs"]] == [1, 1])
check("通常プランに --ultra は付かない",
      "--ultra" not in autotrade_engine._command_for_entry(normal_plan, {}))

# ================================================================
print("=" * 68)
print("5. monitor_publish._apply_ultra_prefs(達成可能→リサイズ / 不能→見送り)")
print("=" * 68)

_orig_fetch = nqx_state.fetch_account_prefs
_orig_accounts = nqx_state.build_accounts_payload

chosen = {**ultra_raw(qty=2, legs=(1, 1)), "grade": "A"}
armed_scenario = nqx_state.build_scenario(
    {**chosen, "marketCycleId": "cy_test"}, observed_at=NOW.isoformat(),
    ttl_minutes=10, symbol="MNQU6")
armed_scenario["grade"] = "A"
base_contract = {"accountScope": ["ACC-ULTRA-01", "ACC-B"], "riskCapDollars": 240}

try:
    # 幾何: TP1 21pt(runner 50pt は枚数には使わない = R52、app/Bot と同じ TP1 基準)。
    # 1ペアの利益 (21+21)×$2 = $84。目標 $2,000 → ceil(2×2000/84)=48 → 48枚。
    # 損失 48×21×$2 = $2,016 ≤ 残DD $4,000。
    nqx_state.fetch_account_prefs = lambda cfg=None, view=None: {
        "ACC-ULTRA-01": {"ultra": True, "profitTarget": 2000, "maxDrawdown": None}}
    nqx_state.build_accounts_payload = lambda **kw: {
        "list": [{"id": "ACC-ULTRA-01", "cap": 240, "buffer": 4000, "profitTarget": 2000}]}
    out_chosen, out_scenario, out_contract, notes = monitor_publish._apply_ultra_prefs(
        chosen, armed_scenario, base_contract, MARKET,
        {"CROSSTRADE_ACCOUNTS": "ACC-ULTRA-01,ACC-B"},
        {"NQX_SYMBOL": "MNQU6"}, "cy_test", False)
    check("達成可能: 48枚へリサイズ(TP1 基準・runner は枚数に使わない)", out_scenario.get("qty") == 48,
          f"qty={out_scenario.get('qty')} notes={notes}")
    check("runner の目標価格は脚に残る", [leg["target"] for leg in out_scenario.get("legs", [])] == [30105.0, 30076.0],
          str(out_scenario.get("legs")))
    check("達成可能: 契約が ULTRA orderable", out_contract.get("orderable") is True,
          str(out_contract.get("blockers")))
    check("達成可能: scope が1口座", out_contract.get("accountScope") == ["ACC-ULTRA-01"])
    check("注記にサイズが残る", any("ULTRA sized" in note for note in notes), str(notes))

    # 残DDが薄く目標に届かない → 見送り(WATCH)
    nqx_state.build_accounts_payload = lambda **kw: {
        "list": [{"id": "ACC-ULTRA-01", "cap": 240, "buffer": 800, "profitTarget": 2000}]}
    out_chosen2, out_scenario2, _, notes2 = monitor_publish._apply_ultra_prefs(
        chosen, armed_scenario, base_contract, MARKET,
        {"CROSSTRADE_ACCOUNTS": "ACC-ULTRA-01,ACC-B"},
        {"NQX_SYMBOL": "MNQU6"}, "cy_test", False)
    check("達成不能: WATCH へ見送り", out_scenario2.get("state") == "WATCH",
          f"state={out_scenario2.get('state')} notes={notes2}")
    check("達成不能: 理由が注記に残る", any("ULTRA skip" in note for note in notes2), str(notes2))

    # ULTRA 対象なし → 無変更
    nqx_state.fetch_account_prefs = lambda cfg=None, view=None: {}
    out_chosen3, out_scenario3, _, notes3 = monitor_publish._apply_ultra_prefs(
        chosen, armed_scenario, base_contract, MARKET,
        {"CROSSTRADE_ACCOUNTS": "ACC-ULTRA-01,ACC-B"},
        {"NQX_SYMBOL": "MNQU6"}, "cy_test", False)
    check("対象なし: 無変更", out_scenario3 is armed_scenario and not notes3)
finally:
    nqx_state.fetch_account_prefs = _orig_fetch
    nqx_state.build_accounts_payload = _orig_accounts

# ================================================================
print("=" * 68)
total = PASS[0] + FAIL[0]
if FAIL[0]:
    print(f"FAILED {FAIL[0]} / {total}")
    sys.exit(1)
print(f"ALL PASS ({total})")
sys.exit(0)

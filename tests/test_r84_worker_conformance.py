# -*- coding: utf-8 -*-
"""R84: pyramid.py と Worker の追撃算術が**数値まで一致**すること(§7-3 / §10)。

engine / order.py / Worker の三重検証は同じ式でなければ意味が無い。片側だけ直すと
claim は通るのに送信が拒否される(あるいはその逆)ので、ここで固定する。
形式は tests/test_execution_contract_conformance.py と同じ(node へ渡して比較)。

    python tests/test_r84_worker_conformance.py
"""
import json
import os
import shutil
import subprocess
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
import execution_contract  # noqa: E402
import execution_intent  # noqa: E402
import pyramid  # noqa: E402

PASS = [0]
FAIL = [0]


def check(label, condition, detail=""):
    if condition:
        PASS[0] += 1
        print(f"  OK   {label}")
    else:
        FAIL[0] += 1
        print(f"  NG   {label}  {detail}")


POINT_VALUE = float(execution_contract.CONTRACT["risk"]["pointValue"])
# 実測の形(2026-09-12 02:12 の SHORT 2 @29,484.25 に 6 枚)と、境界・端数・BUY 側。
CASES = [
    {"baseQty": 2, "baseEntry": 29484.25, "addQty": 6, "addPrice": 29448.5,
     "stop": 29507.5, "cap": 2268.0},
    {"baseQty": 4, "baseEntry": 29446.625, "addQty": 4, "addPrice": 29448.5,
     "stop": 29500.0, "cap": 3100.0},
    {"baseQty": 10, "baseEntry": 29570.375, "addQty": 7, "addPrice": 29540.25,
     "stop": 29601.0, "cap": 400.0},
    {"baseQty": 1, "baseEntry": 29400.0, "addQty": 1, "addPrice": 29420.0,
     "stop": 29380.0, "cap": 5000.0},
    {"baseQty": 3, "baseEntry": 29400.0, "addQty": 9, "addPrice": 29412.75,
     "stop": 29360.0, "cap": 901.0},
    {"baseQty": 12, "baseEntry": 29333.333, "addQty": 5, "addPrice": 29350.5,
     "stop": 29300.25, "cap": 1234.5},
]

# 滑りは口座上限の判定に **直接** 効く(追撃 30 枚・2pt なら $120)。
# SELL は安く売れ、BUY は高く買わされる —— どちらも構造 SL までの距離が伸びる側。
SLIP_CASES = [
    {"side": "SELL", "price": 29448.5},
    {"side": "BUY", "price": 29448.5},
    {"side": "sell", "price": 29400.0},
    {"side": "buy", "price": 29400.25},
]

NODE_SCRIPT = """
import {{ pyramidCombinedEntry, pyramidCombinedRisk, pyramidFitToCap,
         pyramidSlippagePoints, pyramidAdversePrice,
         normalizeExecutionIntent, executionIntentHash }} from './src/state_machine.js';
const cases = {cases};
const point = {point};
const out = cases.map((c) => ({{
  entry: pyramidCombinedEntry(c.baseQty, c.baseEntry, c.addQty, c.addPrice),
  risk: pyramidCombinedRisk(c.baseQty, c.baseEntry, c.addQty, c.addPrice, c.stop, point),
  fit: pyramidFitToCap(c.baseQty, c.baseEntry, c.addQty, c.addPrice, c.stop, point, c.cap),
}}));
const slip = {{ points: pyramidSlippagePoints(),
               prices: {slips}.map((s) => pyramidAdversePrice(s.side, s.price)) }};
const intents = {intents}.map((raw) => {{
  const normalized = normalizeExecutionIntent(raw);
  return {{ ok: normalized.ok, reason: normalized.reason || null,
            hash: normalized.ok ? executionIntentHash(raw) : null }};
}});
console.log(JSON.stringify({{ out, slip, intents }}));
"""


def node_run(script):
    node = shutil.which("node")
    if not node:
        return None, "node is not installed"
    proc = subprocess.run([node, "--input-type=module", "-e", script],
                          cwd=os.path.join(BASE, "cloudflare"), capture_output=True,
                          text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        return None, (proc.stderr or proc.stdout)[-800:]
    try:
        return json.loads(proc.stdout.strip().splitlines()[-1]), None
    except (ValueError, IndexError):
        return None, proc.stdout[-800:]


BASE_INTENT = dict(
    symbol="MNQU6", side="SELL", qty=2, order_type="MARKET", entry=None,
    last=29448.5, stop=29507.5, targets=[29400.0, 29300.0],
    legs=[{"id": "TP1", "qty": 1, "target": 29400.0},
          {"id": "RUNNER", "qty": 1, "target": 29300.0}],
    plan_version="R19-ICT-SPLIT-1",
    execution_contract_version=execution_contract.VERSION,
    account_scope=["LFF05062316710006"])
PY_BASE = execution_intent.build(**BASE_INTENT)
PY_ADD = execution_intent.build(
    **{**BASE_INTENT, "qty": 6,
       "legs": [{"id": "TP1", "qty": 3, "target": 29400.0},
                {"id": "RUNNER", "qty": 3, "target": 29300.0}]},
    pyramid={"baseQty": 2, "addQty": 6})
BAD_ADD = dict(PY_ADD, pyramid={"baseQty": 2, "addQty": 5})

script = NODE_SCRIPT.format(
    cases=json.dumps(CASES), point=json.dumps(POINT_VALUE),
    slips=json.dumps(SLIP_CASES),
    intents=json.dumps([PY_BASE, PY_ADD, BAD_ADD]))
result, error = node_run(script)
if result is None:
    print(f"  SKIP node conformance unavailable: {error}")
    print()
    print(f"合計: PASS {PASS[0]} / FAIL {FAIL[0]}")
    sys.exit(1 if FAIL[0] else 0)

print("--- combined_entry / combined_risk / _fit_to_cap ---")
for index, case in enumerate(CASES):
    js = result["out"][index]
    py_entry = pyramid.combined_entry(case["baseQty"], case["baseEntry"],
                                      case["addQty"], case["addPrice"])
    py_risk = pyramid.combined_risk(case["baseQty"], case["baseEntry"], case["addQty"],
                                    case["addPrice"], case["stop"], POINT_VALUE)
    py_fit = pyramid._fit_to_cap(case["baseQty"], case["baseEntry"], case["addQty"],
                                 case["addPrice"], case["stop"], POINT_VALUE, case["cap"])
    label = f"case{index} {case['baseQty']}@{case['baseEntry']} + {case['addQty']}"
    check(f"{label}: 合成建値が一致",
          js["entry"] is not None and abs(js["entry"] - py_entry) < 1e-9,
          f"{js['entry']} vs {py_entry}")
    check(f"{label}: 合計リスクが一致",
          js["risk"] is not None and abs(js["risk"] - py_risk) < 1e-9,
          f"{js['risk']} vs {py_risk}")
    check(f"{label}: 上限へ刻んだ枚数が一致", js["fit"] == py_fit,
          f"{js['fit']} vs {py_fit}")

print()
print("--- 滑りを当てた追撃価格(上限判定の基準) ---")
check("滑りの pt が一致(契約の maxDeviationPoints)",
      abs(result["slip"]["points"] - pyramid.slippage_points()) < 1e-9,
      f"{result['slip']['points']} vs {pyramid.slippage_points()}")
check("滑りは 0 ではない(これが抜けると概算では枠内・約定したら枠超えになる)",
      pyramid.slippage_points() > 0, str(pyramid.slippage_points()))
for index, case in enumerate(SLIP_CASES):
    js_price = result["slip"]["prices"][index]
    py_price = pyramid.adverse_add_price(case["side"], case["price"])
    label = f"slip{index} {case['side']} {case['price']}"
    check(f"{label}: 不利側へ寄せた価格が一致",
          js_price is not None and abs(js_price - py_price) < 1e-9,
          f"{js_price} vs {py_price}")
    worse = (py_price < case["price"] if case["side"].upper() == "SELL"
             else py_price > case["price"])
    check(f"{label}: 向きが不利側", worse, str(py_price))

print()
print("--- executionIntent の pyramid 追記 ---")
base_js, add_js, bad_js = result["intents"]
check("pyramid の無い intent は従来どおり通る", base_js["ok"] is True, str(base_js))
check("その hash は Python と一致",
      base_js["hash"] == execution_intent.intent_hash(PY_BASE),
      f"{base_js['hash']} vs {execution_intent.intent_hash(PY_BASE)}")
check("pyramid 付き intent も通る", add_js["ok"] is True, str(add_js))
check("pyramid 付きの hash も Python と一致",
      add_js["hash"] == execution_intent.intent_hash(PY_ADD),
      f"{add_js['hash']} vs {execution_intent.intent_hash(PY_ADD)}")
check("pyramid 付きは別の hash になる(追撃であることが固定される)",
      add_js["hash"] != base_js["hash"])
check("addQty と qty が食い違う intent は両側で拒否される",
      bad_js["ok"] is False, str(bad_js))
rejected = False
try:
    execution_intent.normalize(BAD_ADD)
except ValueError:
    rejected = True
check("Python 側も同じものを拒否する", rejected)

print()
print("--- 既存の intent バイト列は 1 文字も変わらない ---")
check("pyramid キーを持たない intent は従来のキー集合のまま",
      set(PY_BASE) == execution_intent.INTENT_FIELDS, str(sorted(PY_BASE)))
check("pyramid 付きだけが 1 キー増える",
      set(PY_ADD) == execution_intent.INTENT_FIELDS | {"pyramid"})

print()
print(f"合計: PASS {PASS[0]} / FAIL {FAIL[0]}")
sys.exit(1 if FAIL[0] else 0)

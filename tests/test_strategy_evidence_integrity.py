# -*- coding: utf-8 -*-
"""R15 strategy-evidence integrity: one canonical hash, no silent truncation."""
import copy
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import monitor_pipeline  # noqa: E402
import strategy_evidence  # noqa: E402


def check(label, condition, detail=""):
    if not condition:
        raise AssertionError(f"{label}: {detail}")
    print(f"OK   {label}")


NOW = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)
AT = NOW.isoformat()
EVIDENCE = strategy_evidence.canonicalize({
    "version": strategy_evidence.VERSION, "asOf": AT, "sessionId": "NY-2026-08-22",
    "source": "fixture", "provenance": "observed",
    "models": {"ifvg": {"status": "CONFIRMED", "valid": True, "direction": "BUY",
                           "entry": 20000.0}},
})

MARKET = {
    "at": AT, "observedAt": AT, "verified": True, "source": "fixture",
    "sourceSymbol": "CME_MINI:MNQU6", "resolution": "3", "barResolution": "3",
    "price": 20000.0, "cvdAt": AT,
    "bars": [
        {"t": 1_700_000_000, "o": 19999.0, "h": 20000.0, "l": 19998.0, "c": 19999.0},
        {"t": 1_700_000_180, "o": 19999.0, "h": 20001.0, "l": 19999.0, "c": 20000.0},
    ], "levels": [],
}


def worker_results(evidence, malformed):
    script = """
import fs from 'node:fs';
import {validateMarket, strategyEvidenceHash} from './src/state_machine.js';
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const good = validateMarket({...input.market, strategyEvidence: input.evidence}, input.nowMs);
const bad = input.malformed.map((entry) => validateMarket(
  {...input.market, strategyEvidence: entry}, input.nowMs));
console.log(JSON.stringify({good, bad, hash: strategyEvidenceHash(good.market.strategyEvidence)}));
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script], cwd=os.path.join(BASE, "cloudflare"),
        input=json.dumps({"market": MARKET, "evidence": evidence, "malformed": malformed,
                          "nowMs": int(NOW.timestamp() * 1000)}),
        check=True, capture_output=True, text=True, encoding="utf-8")
    return json.loads(result.stdout)


tampered = copy.deepcopy(EVIDENCE)
tampered["evidenceHash"] = tampered["evidenceHash"][:-1] + ("0" if tampered["evidenceHash"][-1] != "0" else "1")
deep = {"x": {"x": {"x": {"x": {"x": {"x": {"x": 1}}}}}}}
malformed = [
    tampered,
    {**EVIDENCE, "models": {"label": "x" * 161}},
    {**EVIDENCE, "models": {"x" * 65: True}},
    {**EVIDENCE, "models": deep},
    {**EVIDENCE, "models": {"entry": 20000.13}},
    {**EVIDENCE, "unknownTop": True},
    {**EVIDENCE, "source": "x" * 97},
    {**EVIDENCE, "version": "x" * 65},
    {**EVIDENCE, "provenance": {"not": "text"}},
]

for index, raw in enumerate(malformed):
    try:
        strategy_evidence.canonicalize(raw)
    except ValueError:
        pass
    else:
        raise AssertionError(f"malformed Python evidence {index} was accepted")
check("Python rejects hash/bound/depth/off-tick evidence", True)

# R39: 派生価格（VP の POC/VAH/VAL、VWAP、ゾーン中点、フィボ）は
# 定義上 0.25 に乗らない。ここを弾くと evidence 全体が失効し、
# _authoritative_cycle_seal が毎サイクル CYCLE_MISMATCH で止まる。
DERIVED_OK = strategy_evidence.canonicalize({
    "version": strategy_evidence.VERSION, "asOf": AT, "sessionId": "NY-2026-08-22",
    "source": "fixture", "provenance": "observed",
    "models": {
        "liquidity": {"pools": [{"price": 29402.78}, {"price": 29486.65}]},
        "vwapReversion": {"vwap": 29298.49},
        "ict": {"fvg": {"BEAR": [{"lo": 29218.0, "hi": 29218.25, "mid": 29218.12}]}},
        "ote": {"oteBuy": 29123.4567, "oteSell": 29187.6543},
    },
})
check("派生価格（VP/VWAP/中点/OTE）は evidence を失効させない",
      isinstance(DERIVED_OK, dict) and DERIVED_OK.get("evidenceHash"))
for strict_key, strict_value in (("entry", 20000.13), ("stop", 19999.9),
                                 ("lo", 29218.1), ("hi", 29218.3),
                                 ("rangeHigh", 29500.05)):
    try:
        strategy_evidence.canonicalize({**EVIDENCE, "evidenceHash": None,
                                        "models": {strict_key: strict_value}})
    except ValueError:
        continue
    raise AssertionError(f"off-tick {strict_key} was accepted")
check("取引価格（entry/stop/lo/hi/rangeHigh）のティック検査は残る", True)

worker = worker_results(EVIDENCE, malformed)
worker_derived = worker_results(DERIVED_OK, [])
check("Worker も派生価格の evidence を受理する（Python と同一規則）",
      worker_derived["good"]["ok"]
      and worker_derived["good"]["market"]["strategyEvidence"] is not None
      and not worker_derived["good"]["market"].get("strategyEvidenceError"),
      worker_derived["good"])
check("Worker の派生価格 canonical hash が Python と一致する",
      worker_derived["hash"] == DERIVED_OK["evidenceHash"], worker_derived)
check("Python canonical hash matches Worker canonical hash",
      worker["hash"] == EVIDENCE["evidenceHash"], worker)
check("Worker accepts a canonical evidence envelope",
      worker["good"]["ok"] and worker["good"]["market"]["strategyEvidence"]["evidenceHash"] == EVIDENCE["evidenceHash"],
      worker["good"])
for index, result in enumerate(worker["bad"]):
    check(f"malformed evidence {index} leaves verified market available",
          result["ok"] and result["market"]["price"] == MARKET["price"] and
          result["market"]["strategyEvidence"] is None and bool(result["market"]["strategyEvidenceError"]), result)

# Ingress hoists provider evidence exactly once, removes the snapshot duplicate,
# and preserves only a named audit descriptor across later compacting.
raw_bundle = {
    "at": AT, "priceAt": AT, "price": 20000.0, "sourceSymbol": "CME_MINI:MNQU6",
    "priceSource": "fixture", "snapshot": {
        "bars3m": MARKET["bars"] * 6, "levels": [], "sessionId": "NY-2026-08-22",
        "strategyEvidence": EVIDENCE,
    },
}
normalized = monitor_pipeline._source_bundle(raw_bundle, {"symbol": "MNQU6"})
check("ingress hoists canonical evidence to one top-level location",
      normalized["strategyEvidence"]["evidenceHash"] == EVIDENCE["evidenceHash"] and
      "strategyEvidence" not in normalized["snapshot"] and
      normalized["providerStrategyEvidenceAudit"]["status"] == "CANONICAL", normalized)

same_at_top = dict(raw_bundle, strategyEvidence=EVIDENCE)
same_normalized = monitor_pipeline._source_bundle(same_at_top, {"symbol": "MNQU6"})
check("matching top and nested evidence deduplicates to exactly one path",
      same_normalized["strategyEvidence"]["evidenceHash"] == EVIDENCE["evidenceHash"] and
      "strategyEvidence" not in same_normalized["snapshot"], same_normalized)

different = copy.deepcopy(EVIDENCE)
different["models"] = {"ifvg": {"status": "CONFIRMED", "valid": True, "direction": "SELL",
                                   "entry": 20000.0}}
different.pop("evidenceHash")
different = strategy_evidence.canonicalize(different)
conflict = monitor_pipeline._source_bundle(
    dict(raw_bundle, strategyEvidence=EVIDENCE,
         snapshot={**raw_bundle["snapshot"], "strategyEvidence": different}), {"symbol": "MNQU6"})
check("top/nested evidence hash conflict is evidence-only rejection",
      conflict["strategyEvidence"] is None and conflict["providerStrategyEvidenceAudit"]["status"] == "REJECTED" and
      conflict["price"] == MARKET["price"], conflict)

print("ALL PASS (test_strategy_evidence_integrity)")

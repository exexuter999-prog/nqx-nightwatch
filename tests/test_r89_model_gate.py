# -*- coding: utf-8 -*-
"""R89: 契約(execution_contract.json の modelGate)でモデル/型を武装から外す。

msnr_gate.evaluate / build_candidates は純粋関数(契約 JSON を読むだけ)。ネットワーク・台帳・発注に触れない。

守ること:
  1. 既定(disabled が空)は R89 以前と同じ判定。
  2. ALL はそのモデルの候補を WATCH + MODEL_DISABLED にし、primary に選ばない(別モデルが primary になれる)。
  3. RESTING_LIMIT は先回り指値(restingLimit)の候補だけを外し、リテスト保持型は残す。
  4. 外した候補は重複除去の前に分けるので、同じ side の生きている候補を押し出さない。
  5. 契約の節が壊れていたら何も外さない。不正な行は invalid に残り、本番契約には無い。
"""
import os
import sys
from datetime import datetime, timezone

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import msnr_gate  # noqa: E402

TURTLE = "TURTLE_SOUP_REVERSAL"
ENV_KEYS = ("NQX_TURTLE_SWEEP_STOP", "NQX_SL_CAP_PT", "NQX_ULTRA_MODE")
T0 = 1786970000 - (1786970000 % 180)
P = 30000.0
REAL_RULES = msnr_gate.model_gate_rules


def check(label, condition, detail=""):
    if not condition:
        raise AssertionError(f"{label}: {detail}")
    print(f"OK   {label}")


def gate(*rules):
    return {"modelGate": {"version": "R89-MODEL-GATE-1",
                          "disabled": [{"model": m, "variant": v} for m, v in rules]}}


def bundle(spec):
    bars = [{"t": T0 + 180 * i, "o": o, "h": h, "l": l, "c": c, "v": 1000}
            for i, (o, h, l, c) in enumerate(spec)]
    price_at = datetime.fromtimestamp(bars[-1]["t"] + 180, timezone.utc).isoformat()
    return {"at": price_at, "priceAt": price_at, "price": bars[-1]["c"],
            "snapshot": {"levels": [{"label": "TEST", "price": P},
                                    {"label": "Weekly High", "price": 30100.0},
                                    {"label": "Monthly High", "price": 30200.0}],
                         "bars3m": bars}}


def evaluate(spec, rules):
    msnr_gate.model_gate_rules = lambda contract=None: {"rules": list(rules), "invalid": []}
    try:
        return msnr_gate.evaluate(bundle(spec))
    finally:
        msnr_gate.model_gate_rules = REAL_RULES


def turtles(result):
    return [c for c in result["candidates"] if c["model"] == TURTLE]


ABOVE = [(30012, 30018, 30010, 30014) for _ in range(12)]
# リテスト保持まで完成した 1 本型スイープ(建値 30,000 / SL 29,986)
RETEST_HELD = ABOVE + [
    (29998, 30012, 29994, 30010),    # 12 スイープ
    (30010, 30026, 30008, 30024),    # 13 MSS
    (30020, 30022, 30000, 30012),    # 14 保持リテスト
]
# MSS まで。リテスト前なのでレベルへ先回りの指値(現値 30,019 / 建値 30,000 / SL 29,986 = 1.36R 先)
RESTING = ABOVE + [
    (29998, 30012, 29994, 30010),    # 12 スイープ
    (30010, 30022, 30008, 30019),    # 13 MSS(直前 5 本の高値 30,018 を実体で上抜け)
]


def test_rules_loader():
    check("節が無ければ何も外さない", REAL_RULES({}) == {"rules": [], "invalid": []})
    check("disabled が配列でなければ何も外さない(壊れた節)",
          REAL_RULES({"modelGate": {"disabled": "TURTLE"}})["rules"] == [])
    parsed = REAL_RULES({"modelGate": {"disabled": [
        {"model": TURTLE}, {"model": TURTLE, "variant": "resting_limit"},
        {"model": TURTLE, "variant": "ALL"}, {"model": "TURTLE_SOUP"},
        {"model": "VP80_REVERSION", "variant": "HALF"}, "TURTLE_SOUP_REVERSAL"]}})
    check("variant 省略は ALL・大小文字は問わない・重複は 1 本",
          parsed["rules"] == [(TURTLE, "ALL"), (TURTLE, "RESTING_LIMIT")], parsed)
    check("未知のモデル名・未知の variant・行の形の誤りは invalid に残る", len(parsed["invalid"]) == 3, parsed)


def test_production_contract_is_valid():
    real = REAL_RULES()
    check("本番契約の modelGate は読めて、不正な行が無い", real["invalid"] == [], real)
    check("本番契約の規則は既知のモデル/型だけ",
          all(m in msnr_gate.MODEL_ORDER and v in msnr_gate.MODEL_GATE_VARIANTS for m, v in real["rules"]),
          real)


def test_blocker_matching():
    resting = {"model": TURTLE, "restingLimit": True}
    held = {"model": TURTLE}
    other = {"model": "BREAKER_CONTINUATION"}
    rules_all = REAL_RULES(gate((TURTLE, "ALL")))["rules"]
    rules_limit = REAL_RULES(gate((TURTLE, "RESTING_LIMIT")))["rules"]
    check("ALL は指値型もリテスト保持型も外す",
          msnr_gate.model_gate_blocker(resting, rules_all) == "MODEL_DISABLED"
          and msnr_gate.model_gate_blocker(held, rules_all) == "MODEL_DISABLED")
    check("RESTING_LIMIT は指値型だけ",
          msnr_gate.model_gate_blocker(resting, rules_limit) == "RESTING_LIMIT_DISABLED"
          and msnr_gate.model_gate_blocker(held, rules_limit) is None)
    check("他のモデルには当たらない", msnr_gate.model_gate_blocker(other, rules_all) is None)


def test_default_empty_changes_nothing():
    for name, spec in (("RETEST_HELD", RETEST_HELD), ("RESTING", RESTING)):
        base = evaluate(spec, [])
        cand = turtles(base)
        check(f"{name}: 空の規則では TURTLE 候補がそのまま", len(cand) == 1 and "modelGate" not in cand[0]
              and "MODEL_DISABLED" not in cand[0]["hardBlockers"], cand)
    resting = turtles(evaluate(RESTING, []))[0]
    check("RESTING フィクスチャは先回り指値の候補", resting.get("restingLimit") is True
          and resting["entry"] == 30000.0 and resting["stop"] == 29986.0, resting)
    held = turtles(evaluate(RETEST_HELD, []))[0]
    check("RETEST_HELD フィクスチャはリテスト保持の候補", not held.get("restingLimit")
          and held["chain"]["state"] == "RETEST_HELD", held.get("chain"))


def test_all_variant_removes_model_from_primary():
    for name, spec in (("RETEST_HELD", RETEST_HELD), ("RESTING", RESTING)):
        base = evaluate(spec, [])
        gated = evaluate(spec, [(TURTLE, "ALL")])
        cand = turtles(gated)
        check(f"{name}: ALL で TURTLE 候補は WATCH + MODEL_DISABLED(記録には残る)",
              len(cand) == 1 and cand[0]["state"] == "WATCH" and cand[0]["allowed"] is False
              and cand[0]["modelGate"] == "MODEL_DISABLED" and "MODEL_DISABLED" in cand[0]["hardBlockers"], cand)
        check(f"{name}: primary は TURTLE にならない", gated["decision"]["model"] != TURTLE,
              gated["decision"].get("model"))
        others_base = [c for c in base["candidates"] if c["model"] != TURTLE]
        others_gated = [c for c in gated["candidates"] if c["model"] != TURTLE]
        check(f"{name}: 他モデルの候補は変わらない",
              [(c["model"], c["side"], c["stop"], c["state"]) for c in others_base]
              == [(c["model"], c["side"], c["stop"], c["state"]) for c in others_gated])


def test_resting_limit_variant():
    gated = evaluate(RESTING, [(TURTLE, "RESTING_LIMIT")])
    cand = turtles(gated)
    check("指値型は RESTING_LIMIT_DISABLED で WATCH", cand and cand[0]["state"] == "WATCH"
          and "RESTING_LIMIT_DISABLED" in cand[0]["hardBlockers"], cand)
    check("指値型は primary にならない", gated["decision"]["model"] != TURTLE, gated["decision"].get("model"))
    base = evaluate(RETEST_HELD, [])
    kept = evaluate(RETEST_HELD, [(TURTLE, "RESTING_LIMIT")])
    check("リテスト保持型は RESTING_LIMIT では外れない(判定もそのまま)",
          kept["decision"] == base["decision"] and "modelGate" not in turtles(kept)[0], kept["decision"])


def test_gate_runs_before_dedupe():
    """同じ side で「点数の高い指値型」と「点数の低いリテスト保持型」が並ぶとき。"""
    saved = msnr_gate._candidate_for_chain

    def fake(model, level, chain, *args, **kwargs):
        resting = chain["state"] == "MSS_CONFIRMED"
        return {"model": model, "side": "BUY", "entry": 30000.0, "stop": 29986.0,
                "targets": [30100.0, 30200.0], "targetR": [7.14, 14.29], "level": level["label"],
                "chain": chain, "score": 9 if resting else 7, "grade": "A+" if resting else "A",
                "state": "ARMED", "allowed": True, "evidence": [], "hardBlockers": [], "penalties": []}
    levels = [{"label": "LIMIT", "price": 30000.0, "chains": [
                  {"type": "SWEEP", "side": "BUY", "state": "MSS_CONFIRMED", "sweepBarT": None}]},
              {"label": "HELD", "price": 29990.0, "chains": [
                  {"type": "SWEEP", "side": "BUY", "state": "RETEST_HELD", "sweepBarT": None}]}]
    try:
        msnr_gate._candidate_for_chain = fake
        plain = msnr_gate.build_candidates({"price": 30010.0}, {}, [], levels, {}, 8.0, {}, model_gate=[])
        gated = msnr_gate.build_candidates({"price": 30010.0}, {}, [], levels, {}, 8.0, {},
                                           model_gate=[(TURTLE, "RESTING_LIMIT")])
    finally:
        msnr_gate._candidate_for_chain = saved
    check("ゲート無しは点数の高い指値型だけが残る", [c["level"] for c in plain] == ["LIMIT"], plain)
    live = [c for c in gated if not c.get("modelGate")]
    check("ゲート有りはリテスト保持型が生き残る(指値型に押し出されない)", [c["level"] for c in live] == ["HELD"], gated)
    check("外した指値型も記録として 1 件残る",
          [c["level"] for c in gated if c.get("modelGate")] == ["LIMIT"], gated)
    decision = msnr_gate.select_primary(live)
    check("primary はリテスト保持型", decision["model"] == TURTLE and decision["grade"] == "A", decision)


saved_env = {key: os.environ.get(key) for key in ENV_KEYS}
try:
    for key in ENV_KEYS:
        os.environ.pop(key, None)
    test_rules_loader()
    test_production_contract_is_valid()
    test_blocker_matching()
    test_default_empty_changes_nothing()
    test_all_variant_removes_model_from_primary()
    test_resting_limit_variant()
    test_gate_runs_before_dedupe()
finally:
    msnr_gate.model_gate_rules = REAL_RULES
    for key, value in saved_env.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
print("ALL PASS test_r89_model_gate")

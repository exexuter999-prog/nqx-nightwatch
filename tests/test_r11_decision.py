# -*- coding: utf-8 -*-
"""R11-D decision-layer regression tests.

These tests exercise the pure router/scenario contract without market access,
Telegram, Cloudflare, or any broker/order side effect.
"""
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import msnr_gate  # noqa: E402


FAILED = []


def check(name, condition, detail=""):
    print(("  OK   " if condition else "  FAIL ") + name + (f" ({detail})" if detail and not condition else ""))
    if not condition:
        FAILED.append(name)


def test_router_prefers_r11_model_rank_on_equal_grade():
    candidates = [
        {
            "model": "BREAKER_CONTINUATION", "side": "BUY", "state": "ARMED",
            "grade": "A+", "score": 9, "entry": 30000, "stop": 29970,
            "targets": [30090], "targetR": [3.0], "hardBlockers": [],
            "penalties": [], "evidence": ["BREAKER"],
        },
        {
            "model": "VP80_REVERSION", "side": "SELL", "state": "ARMED",
            "grade": "A+", "score": 9, "entry": 30000, "stop": 30030,
            "targets": [29910], "targetR": [3.0], "hardBlockers": [],
            "penalties": [], "evidence": ["VP_ACCEPTED"],
        },
    ]
    decision = msnr_gate.select_primary(candidates, {"at": "2026-08-22T00:00:00Z", "phase": "PA_HARVEST"})
    check("one primary decision", decision["model"] == "VP80_REVERSION")
    check("phase survives router", decision["phase"] == "PA_HARVEST")
    check("decision id is deterministic", len(decision["decisionId"]) == 16)


def test_rotation_is_a_router_not_a_global_blocker():
    route = msnr_gate.route_regime(
        {"rotation": "ROTATION_REGIME"},
        {"rotation": {"verdict": "ROTATION"}},
    )
    check("rotation reported", route["rotation"] is True)
    check("rotation still has preferred models", bool(route["preferred"]))
    check("rotation does not suppress every model", len(route["suppressed"]) < len(msnr_gate.MODEL_ORDER))


def test_ict_smt_is_decision_evidence():
    bars = [{"t": 0, "o": 30000, "h": 30020, "l": 29980, "c": 30010}]
    level = {"label": "VAL", "price": 29990, "freshness": "FRESH"}
    chain = {"side": "BUY", "type": "SWEEP", "state": "SWEEP_COMPLETE",
             "sweepBarT": 0, "dispBody": 20}
    route = {"preferred": set(msnr_gate.MODEL_ORDER), "suppressed": set(), "rotation": False}
    base = {"snapshot": {}}
    ict = {"range": {"favors": {"BUY": True}},
           "smt": {"available": True, "freshness": "FRESH", "bias": "BULLISH"},
           "dol": {"BUY": {"target": 30100, "run": "LRLR"}}}
    aligned = msnr_gate._candidate_for_chain(
        "TURTLE_SOUP_REVERSAL", level, chain, bars,
        [{"label": "PDH", "price": 30100}], ict, route, base, 4.0,
    )
    opposed_ict = dict(ict, smt={"available": True, "freshness": "FRESH", "bias": "BEARISH"})
    opposed = msnr_gate._candidate_for_chain(
        "TURTLE_SOUP_REVERSAL", level, chain, bars,
        [{"label": "PDH", "price": 30100}], opposed_ict, route, base, 4.0,
    )
    check("SMT alignment adds evidence", "SMT_ALIGNED" in aligned["evidence"])
    check("SMT opposition penalizes candidate", "SMT_AGAINST" in opposed["penalties"])
    check("SMT changes score", aligned["score"] > opposed["score"])


def test_strategy_alignment_bonus_has_a_two_vote_boundary():
    bars = [{"t": 0, "o": 30000, "h": 30020, "l": 29980, "c": 30010}]
    level = {"label": "VAL", "price": 29990, "freshness": "FRESH"}
    chain = {"side": "BUY", "type": "SWEEP", "state": "SWEEP_COMPLETE", "sweepBarT": 0, "dispBody": 20}
    route = {"preferred": set(msnr_gate.MODEL_ORDER), "suppressed": set(), "rotation": False}
    ict = {"range": {"favors": {"BUY": True}}, "dol": {"BUY": {"target": 30100, "run": "LRLR"}}}
    args = ("TURTLE_SOUP_REVERSAL", level, chain, bars, [{"label": "PDH", "price": 30100}], ict, route, {"snapshot": {}}, 4.0)
    base = msnr_gate._candidate_for_chain(*args)
    one = msnr_gate._candidate_for_chain(*args, {"alignment": {"BUY": 1, "SELL": 0}, "activeModels": ["crt"]})
    two = msnr_gate._candidate_for_chain(*args, {"alignment": {"BUY": 2, "SELL": 0}, "activeModels": ["crt", "ifvg"]})
    check("one eligible model does not add MSNR score", one["score"] == base["score"])
    check("two eligible independent models add the bounded MSNR bonus", two["score"] == base["score"] + 2)


def test_scenario_is_single_primary_and_no_order_side_effect():
    decision = {
        "decisionId": "abc123", "phase": "EVAL_STRIKE", "model": "OTE_FVG_PULLBACK",
        "side": "BUY", "state": "ARMED", "grade": "A", "entryMode": "STRUCTURE_RETEST",
        "entry": 30000, "stop": 29970, "targets": [30090], "targetR": [3.0],
        "evidence": ["ICT_LOCATION", "OTE_FVG_CONFLUENCE"],
    }
    scenario = msnr_gate.decision_to_scenario(decision, {}, qty=2)
    check("single primary scenario", scenario["scenarioId"] == "abc123")
    check("scenario keeps A grade", scenario["grade"] == "A")
    check("scenario has no broker command", "order" not in scenario and "confirm" not in scenario)


def test_empty_target_candidates_are_safe_to_deduplicate():
    bars = [{"t": 0, "o": 30000, "h": 30005, "l": 29995, "c": 30000}]
    chain = {"side": "BUY", "type": "SWEEP", "state": "SWEEP_COMPLETE",
             "sweepBarT": 0, "dispBody": 1}
    levels = [
        {"label": "VAL", "price": 30000, "chains": [dict(chain)]},
        {"label": "POC", "price": 30001, "chains": [dict(chain)]},
    ]
    candidates = msnr_gate.build_candidates(
        {"snapshot": {}}, {"rotation": {}}, bars, levels, {}, 4.0,
    )
    check("empty targetR does not crash dedupe", isinstance(candidates, list))


test_router_prefers_r11_model_rank_on_equal_grade()
test_rotation_is_a_router_not_a_global_blocker()
test_ict_smt_is_decision_evidence()
test_strategy_alignment_bonus_has_a_two_vote_boundary()
test_scenario_is_single_primary_and_no_order_side_effect()
test_empty_target_candidates_are_safe_to_deduplicate()

if FAILED:
    print(f"FAILED: {len(FAILED)} -> {', '.join(FAILED)}")
    raise SystemExit(1)
print("R11-D PASS")

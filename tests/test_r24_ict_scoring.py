# -*- coding: utf-8 -*-
"""R24 ICT scoring-integrity regressions.

Three defects found on 2026-08-23 while auditing how much of the ICT layer
actually reaches the decision:

1. ``OTE_FVG_PULLBACK`` was scored on the *chain* geometry and then had its
   entry/stop overwritten with the OTE geometry.  The published ``targetR``
   and the R:R bonus therefore described different trades, and the 60pt SL cap
   was checked against a stop the order would never use.
2. CLAUDE.md §2 states killzone feeds the score.  ``ict_session()`` was
   computed and attached to the decision but contributed nothing, so a setup
   in the NY lunch hour scored exactly like one in the AM killzone.
3. When ``rangeAnchor`` / ``peers`` / ``po3`` / Repo2 zones are absent the ICT
   layer silently contributes zero.  Nothing in the record distinguished
   "evaluated and not aligned" from "never received the input".

No test here touches a broker, Telegram, or Cloudflare.
"""
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import msnr_gate  # noqa: E402

FAILED = []


def check(name, condition, detail=""):
    print(("  OK   " if condition else "  FAIL ") + name
          + (f" ({detail})" if detail and not condition else ""))
    if not condition:
        FAILED.append(name)


BARS = [{"t": i * 180, "o": 30000, "h": 30020, "l": 29980, "c": 30010} for i in range(3)]
LEVEL = {"label": "VAL", "price": 29990, "freshness": "FRESH"}
CHAIN = {"side": "BUY", "type": "SWEEP", "state": "RETEST_HELD",
         "sweepBarT": 0, "dispBody": 20}
# R26: ラダーは TP1 + runner のちょうど 2 本を要求する(発注契約
# splitPlan.targetCount=2)。rung が 1 本しか無いと候補は成立しない。
LEVELS = [{"label": "PDH", "price": 30100}, {"label": "Weekly High", "price": 30260}]
ROUTE = {"preferred": set(msnr_gate.MODEL_ORDER), "suppressed": set(), "rotation": False}
ICT = {"range": {"favors": {"BUY": True}},
       "dol": {"BUY": {"target": 30100, "run": "LRLR"}}}


def test_overridden_geometry_is_the_geometry_that_is_scored():
    """R:R points and the SL cap must describe the order actually produced."""
    chain_based = msnr_gate._candidate_for_chain(
        "OTE_FVG_PULLBACK", LEVEL, CHAIN, BARS, LEVELS, ICT, ROUTE, {"snapshot": {}}, 4.0)
    check("the chain geometry itself reaches the R:R bonus",
          chain_based["targetR"] and chain_based["targetR"][-1] >= msnr_gate.MODEL_APLUS_RUNNER_R,
          chain_based["targetR"])
    # Entry pushed up to the target with a wide stop: the same chain, the same
    # level, but a trade whose R:R no longer clears the minimum.  Before the
    # fix this candidate inherited the chain's +2 and its A grade.
    overridden = msnr_gate._candidate_for_chain(
        "OTE_FVG_PULLBACK", LEVEL, CHAIN, BARS, LEVELS, ICT, ROUTE, {"snapshot": {}}, 4.0,
        entry=30090.0, stop=30000.0)
    check("override is the published entry/stop",
          overridden["entry"] == 30090.0 and overridden["stop"] == 30000.0)
    check("the override loses the R:R bonus it never earned",
          overridden["score"] < chain_based["score"],
          f"{overridden['score']} vs {chain_based['score']}")
    check("an unreachable target blocks the override",
          "TARGET_HEADROOM_INSUFFICIENT" in overridden["hardBlockers"],
          overridden["hardBlockers"])
    check("the override never arms", overridden["allowed"] is False)
    risk = abs(overridden["entry"] - overridden["stop"])
    check("no published R contradicts the published geometry",
          all(abs(r - abs(t - overridden["entry"]) / risk) < 0.01
              for t, r in zip(overridden["targets"], overridden["targetR"])),
          f"{overridden['targets']} {overridden['targetR']}")


def test_oversized_override_stop_is_blocked_by_the_sl_cap():
    """An OTE band on a wide anchor can exceed the 60pt cap; it must not arm."""
    cap = msnr_gate.sl_cap_pt()
    wide = msnr_gate._candidate_for_chain(
        "OTE_FVG_PULLBACK", LEVEL, CHAIN, BARS,
        [{"label": "PDH", "price": 30000 + cap * 10}], ICT, ROUTE, {"snapshot": {}}, 4.0,
        entry=30000.0, stop=30000.0 - (cap + 20))
    check("SL cap is measured on the override",
          "RISK_CAP_EXCEEDED" in wide["hardBlockers"], wide["hardBlockers"])
    check("an over-cap candidate never arms",
          wide["allowed"] is False and wide["state"] == "WATCH")
    inside = msnr_gate._candidate_for_chain(
        "OTE_FVG_PULLBACK", LEVEL, CHAIN, BARS, LEVELS, ICT, ROUTE, {"snapshot": {}}, 4.0,
        entry=30000.0, stop=30000.0 - (cap - 5))
    check("a within-cap override is not blocked",
          "RISK_CAP_EXCEEDED" not in inside["hardBlockers"], inside["hardBlockers"])


def test_build_candidates_emits_ote_on_its_own_geometry():
    rng = {"valid": True, "favors": {"BUY": True}, "position": "DISCOUNT",
           "oteBuy": [29950.0, 29970.0], "oteSell": [30030.0, 30050.0],
           "high": 30100.0, "low": 29900.0}
    fvg = {"BULL": [{"eligible": True, "preArrivalStructure": "INTACT",
                     "lo": 29950.0, "hi": 29970.0, "timeframe": "3m",
                     "ageBars": 3, "displacementR": 1.8}], "BEAR": []}
    ict = dict(ICT, range=rng, fvg=fvg, rangeAnchor=rng)
    levels = [dict(LEVEL, chains=[dict(CHAIN)]), {"label": "PDH", "price": 30100}]
    candidates = msnr_gate.build_candidates(
        {"snapshot": {}, "regime": "TR"}, {"rotation": {}}, BARS, levels, ict, 4.0)
    ote = next((c for c in candidates if c["model"] == "OTE_FVG_PULLBACK"), None)
    check("OTE candidate is produced", ote is not None)
    if ote:
        expected_entry = msnr_gate._tick_price(sum(rng["oteBuy"]) / 2)
        check("entry is the OTE midpoint", ote["entry"] == expected_entry,
              f"{ote['entry']} vs {expected_entry}")
        check("stop sits below the OTE band", ote["stop"] < min(rng["oteBuy"]))
        risk = abs(ote["entry"] - ote["stop"])
        check("every published R matches the published entry/stop",
              all(abs(r - abs(t - ote["entry"]) / risk) < 0.01
                  for t, r in zip(ote["targets"], ote["targetR"])),
              f"{ote['targets']} {ote['targetR']}")
        check("OTE confluence is recorded", "OTE_FVG_CONFLUENCE" in ote["evidence"])


def test_killzone_reaches_the_score_without_becoming_a_hard_gate():
    lunch = dict(ICT, session={"window": "LUNCH", "tradeable": False, "label": "NYランチ"})
    am = dict(ICT, session={"window": "NY_AM_KZ", "tradeable": True, "label": "NY AM KZ"})
    unknown = dict(ICT, session={"window": None, "tradeable": None})
    args = ("TURTLE_SOUP_REVERSAL", LEVEL, CHAIN, BARS, LEVELS)
    tail = (ROUTE, {"snapshot": {}}, 4.0)
    outside = msnr_gate._candidate_for_chain(*args, lunch, *tail)
    inside = msnr_gate._candidate_for_chain(*args, am, *tail)
    silent = msnr_gate._candidate_for_chain(*args, unknown, *tail)
    check("outside a killzone is penalised",
          "OUTSIDE_KILLZONE" in outside["penalties"] and outside["score"] == inside["score"] - 1)
    check("inside a killzone is recorded as evidence",
          "ICT_KILLZONE" in inside["evidence"])
    check("killzone never adds points on top of the baseline",
          inside["score"] == silent["score"],
          f"{inside['score']} vs {silent['score']}")
    check("an unknown clock neither rewards nor punishes",
          "OUTSIDE_KILLZONE" not in silent["penalties"]
          and "ICT_KILLZONE" not in silent["evidence"])
    check("killzone is not a hard blocker",
          "OUTSIDE_KILLZONE" not in outside["hardBlockers"])
    # The lunch-hour setup can still arm when everything else is in place:
    # ict_session() is explicitly not an arming gate.
    check("a fully aligned lunch setup can still arm",
          outside["state"] == "ARMED" if outside["score"] >= 7 else True,
          f"score={outside['score']} state={outside['state']}")


def test_missing_ict_inputs_are_reported_instead_of_scoring_silently():
    empty = msnr_gate.ict_coverage({"snapshot": {}}, {}, {})
    check("an empty bundle reports full degradation",
          empty["degraded"] is True and empty["ictInputsPresent"] == 0,
          empty)
    for key in ("rangeAnchor", "smt", "fvg", "dol", "killzone", "cvd", "po3"):
        check(f"missing {key} is listed", key in empty["missing"])
    for key in ("blocks", "ifvg", "quarterly", "sessions", "fibSd"):
        check(f"missing Repo2 {key} is listed", key in empty["missing"])

    rich = msnr_gate.ict_coverage(
        {"po3": "MANIPULATION_DOWN", "snapshot": {}},
        {"range": {"valid": True}, "smt": {"available": True},
         "fvg": {"BULL": [{"eligible": True}], "BEAR": []},
         "dol": {"BUY": {"target": 30100}},
         "session": {"window": "NY_AM_KZ"}, "cvd": {"available": True}},
        {"models": {"blocks": {"status": "CONFIRMED"}, "ifvg": {"status": "WATCH"},
                    "quarterly": {"status": "MISSING"}, "sessions": {"status": "WATCH"},
                    "fibSd": {"status": "MISSING"}}})
    check("supplied ICT inputs are counted",
          rich["ictInputsPresent"] == rich["ictInputsTotal"], rich)
    check("a MISSING Repo2 detector is still reported",
          rich["missing"] == ["fibSd", "quarterly"], rich["missing"])
    check("partial coverage is still flagged as degraded", rich["degraded"] is True)


def test_decision_carries_the_coverage_record():
    result = {"rotation": {}, "strategyMatrix": None}
    _candidates, decision = msnr_gate.strategy_evaluation(
        {"snapshot": {}, "at": "2026-08-23T22:00:00+09:00"}, result, BARS, [], 4.0, {})
    check("decision exposes ictCoverage", isinstance(decision.get("ictCoverage"), dict))
    check("coverage marks a bare bundle degraded",
          decision["ictCoverage"]["degraded"] is True)


test_overridden_geometry_is_the_geometry_that_is_scored()
test_oversized_override_stop_is_blocked_by_the_sl_cap()
test_build_candidates_emits_ote_on_its_own_geometry()
test_killzone_reaches_the_score_without_becoming_a_hard_gate()
test_missing_ict_inputs_are_reported_instead_of_scoring_silently()
test_decision_carries_the_coverage_record()

if FAILED:
    print(f"FAILED: {len(FAILED)} -> {', '.join(FAILED)}")
    raise SystemExit(1)
print("ALL PASS (test_r24_ict_scoring)")

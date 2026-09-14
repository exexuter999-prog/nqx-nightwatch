# -*- coding: utf-8 -*-
"""Image strategy catalogue detectors stay deterministic and auditable."""
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import strategy_models


def bar(i, o, h, l, c):
    return {"t": 1_700_000_000 + i * 180, "o": o, "h": h, "l": l, "c": c}


# Parent range, inside bar, then a sell-side CRT sweep back inside.
CRT = [
    bar(0, 100, 110, 95, 105), bar(1, 105, 108, 98, 102),
    bar(2, 102, 106, 99, 103), bar(3, 103, 104, 90, 99),
]


def check(label, condition):
    if not condition:
        raise AssertionError(label)


crt = strategy_models.detect_crt(CRT)
check("CRT sweep is detected", crt["valid"] and crt["direction"] == "BUY")
check("CRT keeps boundary evidence", crt["sweep"] == "SSL" and crt["closeBackInside"])

amd = strategy_models.classify_amd(CRT + [bar(4, 98, 112, 97, 111), bar(5, 111, 115, 109, 114)])
check("AMD phase is explicit", amd["phase"] in {"MANIPULATION", "DISTRIBUTION"})

bundle = {
    "at": "2026-08-22T12:00:00+00:00", "sessionId": "NY-2026-08-22",
    "price": 103,
    "vwap": 108,
    "snapshot": {
        "levels": [{"label": "BSL", "price": 112}, {"label": "SSL", "price": 95}],
        "ifvg": [{"lo": 102, "hi": 104, "direction": "BUY", "active": True,
                  "breached": True, "inverseReclaim": True, "structureIntact": True, "hold": True,
                  "freshness": "FRESH"}],
        "blocks": [{"kind": "MB", "lo": 101, "hi": 103, "direction": "BUY", "active": True,
                    "origin": "2026-08-22T11:30:00+00:00", "displacement": True, "revisit": True, "freshness": "FRESH"},
                   {"kind": "BRK", "lo": 108, "hi": 110, "direction": "SELL", "active": True,
                    "origin": "2026-08-22T11:30:00+00:00", "displacement": True, "revisit": True, "freshness": "FRESH"}],
        "fibSD": {"levels": [101, 103, 105], "bias": "BUY", "tolerance": 0.5,
                  "anchorType": "swing", "freshness": "FRESH", "touched": True},
        "quarterTheory": {"sequence": ["X", "A", "M", "D"], "stage": "D", "bias": "BUY",
                            "asOf": "2026-08-22T12:00:00+00:00", "sequenceAt": "2026-08-22T11:59:00+00:00", "sessionId": "NY", "freshness": "FRESH"},
        "sessionProfile": {"current": "NY", "asianHigh": 106, "asianLow": 98, "midnightOpen": 108,
                           "londonSweep": True, "nyDelivery": True, "freshness": "FRESH"},
    },
}
matrix = strategy_models.build_strategy_matrix(bundle, CRT, bundle["snapshot"]["levels"], {
    "range": {"valid": True, "position": "DISCOUNT", "favors": {"BUY": True, "SELL": False},
              "oteBuy": [101, 104]},
    "rangeAnchor": {"rangeTf": "1h", "freshness": "FRESH"},
    "fvg": {"BULL": [], "BEAR": []},
})
check("catalog version is stable", matrix["version"] == "IMAGE_STRATEGY_CATALOG/1")
check("all core models are present", {"msnr", "ict", "smt", "mmxm", "amdWyckoff", "crt", "vwapReversion", "liquidity", "fib", "ifvg", "blocks", "quarterly", "sessions", "fibSd", "fibCrt"}.issubset(matrix["models"]))
check("Repo2 blocks are normalized", matrix["models"]["blocks"]["status"] == "CONFIRMED" and {"MB", "BRK"}.issubset(matrix["models"]["blocks"]["activeKinds"]))
check("Repo2 IFVG and Fib SD are active", matrix["models"]["ifvg"]["valid"] and matrix["models"]["fibSd"]["valid"])
check("TradingView overlays are available", set(matrix["overlays"]) >= {"fvg", "levels", "rangeAnchor", "crt", "liquidity", "vwap", "ifvg", "blocks", "sessions", "fibSd"})
check("matrix changes ranking without bypassing risk gates", matrix["hardGateImpact"] == "RANKING_ONLY")

for model_name in ("ifvg", "blocks", "quarterly", "sessions", "fibSd"):
    directional = strategy_models._eligible_votes({model_name: matrix["models"][model_name]})
    check(f"{model_name} positive lifecycle creates exactly one BUY vote", directional == {"BUY": [model_name], "SELL": []})

# Votes are executable confluence only: observations, stale provenance, and a
# target location do not become directional score merely because they exist.
votes = strategy_models._eligible_votes({
    "crt": {"status": "CONFIRMED", "valid": True, "direction": "BUY"},
    "fib": {"status": "ALIGNED", "valid": True, "direction": "BUY"},
    "fibCrt": {"status": "CONFIRMED", "valid": True, "direction": "BUY"},
    "watch": {"status": "WATCH", "valid": True, "direction": "BUY"},
    "stale": {"status": "CONFIRMED", "valid": True, "direction": "BUY", "freshness": "STALE"},
    "far": {"status": "OBSERVE", "valid": True, "direction": "SELL", "entryTouched": False},
    "targetOnly": {"status": "CONFIRMED", "valid": True, "direction": "SELL", "targetOnly": True},
})
check("CRT/Fib/FibCRT correlated family has one vote", votes["BUY"] == ["crt"])
check("watch, stale, far and target-only observations do not vote", votes["SELL"] == [])

unknown_context = strategy_models._eligible_votes({
    "ifvg": {"status": "CONFIRMED", "valid": True, "direction": "BUY",
             "freshness": "UNKNOWN", "sessionId": "NY", "provenance": "feed"},
    "targetOnly": {"status": "CONFIRMED", "valid": True, "direction": "SELL",
                   "freshness": "FRESH", "sessionId": "NY", "provenance": "feed", "targetOnly": True},
})
check("unknown freshness and target-only have zero votes", unknown_context == {"BUY": [], "SELL": []})

legacy_false_positives = {
    name: {"status": "CONFIRMED", "valid": True, "direction": "BUY", "freshness": "FRESH",
           "sessionId": "NY", "provenance": "fixture", "entryTouched": True, "lifecycle": "candidate"}
    for name in ("ifvg", "blocks", "quarterly", "fibSd")
}
check("old four label-only candidates create zero false-positive votes",
      strategy_models._eligible_votes(legacy_false_positives) == {"BUY": [], "SELL": []})

ifvg_unreclaimed = strategy_models.detect_ifvg(
    {"ifvg": [{"lo": 102, "hi": 104, "direction": "BUY", "active": True,
                "breached": True, "inverseReclaim": False, "structureIntact": True}]}, {}, 103)
check("IFVG needs inverse reclaim before confirmation", not ifvg_unreclaimed["valid"] and
      ifvg_unreclaimed["zones"][0]["lifecycle"] == "CANDIDATE")

ifvg_consumed = strategy_models.detect_ifvg(
    {"ifvg": [{"lo": 102, "hi": 104, "direction": "BUY", "active": True, "breached": True,
                "inverseReclaim": True, "structureIntact": True, "hold": True, "consumed": True,
                "freshness": "FRESH"}]}, {}, 103)
check("consumed IFVG stays displayable but cannot confirm", not ifvg_consumed["valid"] and
      ifvg_consumed["zones"][0]["lifecycle"] == "CONSUMED")

quarterly_reverse = strategy_models.detect_quarterly_theory(
    {"quarterly": {"sequence": ["X", "M", "A", "D"], "stage": "D", "bias": "BUY",
                   "asOf": "2026-08-22T12:00:00+00:00", "sessionId": "NY", "freshness": "FRESH"}}, CRT, 103)
check("reverse quarterly sequence has zero lifecycle validity", not quarterly_reverse["valid"])

fib_untouched = strategy_models.detect_fib_sd(
    {"fibSd": {"levels": [103], "bias": "BUY", "anchorType": "swing", "freshness": "FRESH", "touched": False}}, 103)
check("untouched FibSD cannot become valid", not fib_untouched["valid"])

# FibCRT is a single correlated vote only after the OTE anchor is explicit,
# fresh, same-session, and actually touched.  A target-like fib label alone
# must never revive the CRT family.
fib_positive = strategy_models.detect_fib_alignment({
    "asOf": "2026-08-22T12:00:00+00:00", "sessionId": "NY-2026-08-22",
    "range": {"valid": True, "favors": {"BUY": True}, "oteBuy": [101, 104]},
    "rangeAnchor": {"rangeTf": "1h", "anchorType": "swing", "freshness": "FRESH",
                    "sessionId": "NY-2026-08-22", "asOf": "2026-08-22T11:57:00+00:00",
                    "provenance": "fixture"},
}, 103)
crt_positive = {"status": "CONFIRMED", "valid": True, "direction": "BUY", "freshness": "FRESH",
                "sessionId": "NY-2026-08-22", "asOf": "2026-08-22T12:00:00+00:00",
                "provenance": "fixture", "entryTouched": True}
fib_crt = strategy_models.detect_fib_crt(crt_positive, fib_positive)
check("production FibCRT has exactly one correlated BUY vote",
      fib_positive["valid"] and fib_crt["valid"] and
      strategy_models._eligible_votes({"crt": crt_positive, "fib": fib_positive, "fibCrt": fib_crt}) ==
      {"BUY": ["crt"], "SELL": []})
for label, candidate_crt, candidate_fib in (
    ("opposite", dict(crt_positive, direction="SELL"), fib_positive),
    ("stale", crt_positive, dict(fib_positive, freshness="STALE")),
    ("missing session", crt_positive, dict(fib_positive, sessionId=None)),
):
    check(f"FibCRT {label} context has zero vote",
          strategy_models._eligible_votes({"fibCrt": strategy_models.detect_fib_crt(candidate_crt, candidate_fib)}) ==
          {"BUY": [], "SELL": []})

for kind in ("OB", "BRK", "MB", "RJB"):
    block = strategy_models.detect_blocks({"blocks": {kind: {
        "lo": 101, "hi": 103, "direction": "BUY", "origin": "2026-08-22T11:30:00+00:00",
        "displacement": True, "revisit": True, "freshness": "FRESH", "provenance": "fixture",
    }}}, CRT, 102)
    check(f"{kind} keyed block lifecycle is a single positive vote",
          block["valid"] and strategy_models._eligible_votes({"blocks": dict(block, status="CONFIRMED",
          sessionId="NY", lifecycle="confirmed")}) == {"BUY": ["blocks"], "SELL": []})

for form in (
    [{"lo": 102, "hi": 104, "direction": "BUY", "breached": True, "inverseReclaim": True,
      "structureIntact": True, "hold": True}],
    {"IFVG": {"lo": 102, "hi": 104, "direction": "BUY", "breached": True, "inverseReclaim": True,
              "structureIntact": True, "hold": True}},
    {"lo": 102, "hi": 104, "direction": "BUY", "breached": True, "inverseReclaim": True,
     "structureIntact": True, "hold": True},
):
    check("IFVG lifecycle survives all zone input shapes", strategy_models.detect_ifvg({"ifvg": form}, {}, 103)["valid"])

derived_ifvg_bars = [
    bar(0, 98, 100, 97, 99), bar(1, 100, 103, 99, 102),
    bar(2, 104, 106, 104, 105),  # bullish FVG: 100 -> 104
    bar(3, 102, 103, 98, 99),    # body breach below 100 => inverse SELL
    bar(4, 99, 102, 98, 99),     # retest and close back below lower edge
    bar(5, 99, 102, 98, 100),
]
derived_ifvg = strategy_models.detect_ifvg({}, {}, 101, derived_ifvg_bars)
check("confirmed OHLC derives an inverse SELL FVG without a provider",
      derived_ifvg["valid"] and derived_ifvg["direction"] == "SELL" and
      derived_ifvg["provenance"] == "confirmed_3m_ohlc")

explicit_range = {
    "asOf": "2026-08-22T12:00:00+00:00", "sessionId": "NY",
    "range": {"valid": True, "high": 110, "low": 100,
              "favors": {"BUY": True, "SELL": False},
              "anchorType": "PRIOR_DAY_DERIVED", "rangeTf": "session",
              "freshness": "ACTIVE"},
}
derived_fib_sd = strategy_models.detect_fib_sd({}, 110, derived_ifvg_bars, explicit_range)
check("explicit ICT range derives the Fib-SD ladder",
      derived_fib_sd["valid"] and derived_fib_sd["direction"] == "BUY" and
      derived_fib_sd["provenance"] == "DERIVED_EXPLICIT_ICT_RANGE" and
      {-1.0, -2.5, -5.0}.issubset({row["ratio"] for row in derived_fib_sd["ratioRows"]}))
check("rolling 3m bars alone cannot invent a Fib-SD anchor",
      strategy_models.detect_fib_sd({}, 110, derived_ifvg_bars, {})["status"] == "MISSING")
print("ALL PASS (test_strategy_models)")

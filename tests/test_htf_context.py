# -*- coding: utf-8 -*-
"""Confirmed 45m/1h/4h/1D context must be explicit, fresh and deterministic."""
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import htf_context
import msnr_gate


def check(label, condition):
    if not condition:
        raise AssertionError(label)


def trend(step, direction=1, count=30, end=1_700_500_000):
    start = end - count * step
    rows = []
    for index in range(count):
        close = 100 + direction * index
        rows.append({"t": start + index * step, "o": close - direction * 0.5,
                     "h": close + 1, "l": close - 1, "c": close})
    return rows


frames = {name: trend(step, 1 if name != "1d" else -1)
          for name, step in htf_context.FRAME_STEPS.items()}
latest_close = 1_700_500_000
context = htf_context.build_context(frames, latest_close)
check("all four confirmed frames are represented", set(context["frames"]) == {"45m", "1h", "4h", "1d"})
check("weighted intraday agreement creates BUY context", context["status"] == "ALIGNED" and context["bias"] == "BUY")
check("each frame keeps structure inputs", all("ema20" in row and "atr14" in row
                                               for row in context["frames"].values()))

missing = htf_context.build_context({"45m": frames["45m"]}, latest_close)
check("one HTF alone cannot align the engine", missing["status"] == "INSUFFICIENT" and missing["bias"] is None)

stale_now = latest_close + htf_context.FRAME_MAX_AGE["45m"] + 1
stale = htf_context.observe_frame("45m", frames["45m"], stale_now)
check("stale confirmed bars do not remain valid", stale["freshness"] == "STALE" and stale["valid"] is False)
check("HTF refresh waits for the next candle close",
      htf_context.refresh_due("45m", frames["45m"], latest_close) is False and
      htf_context.refresh_due("45m", frames["45m"], latest_close + 2700) is True)

candidate_args = {
    "model": "BREAKER_CONTINUATION",
    "level": {"label": "TEST", "price": 100, "freshness": "FRESH"},
    "chain": {"side": "BUY", "dispBody": 10},
    "bars": trend(180, 1, 30), "levels": [],
    "ict": {"range": {}, "smt": {}, "cvd": {"aplusAllowed": True},
            "dol": {}, "session": {}},
    "regime_info": {"preferred": [], "suppressed": [], "rotation": False},
    "nf": 5, "strategy_matrix": {"alignment": {}, "activeModels": []},
    "entry": 100, "stop": 90,
    "targets_override": [(120, "TP1", 2.0), (140, "RUNNER", 4.0)],
}
aligned_candidate = msnr_gate._candidate_for_chain(
    bundle={"snapshot": {"htfContext": {"status": "ALIGNED", "bias": "BUY", "complete": True}}},
    **candidate_args)
conflict_candidate = msnr_gate._candidate_for_chain(
    bundle={"snapshot": {"htfContext": {"status": "ALIGNED", "bias": "SELL", "complete": True}}},
    **candidate_args)
check("HTF alignment changes candidate rank exactly once",
      aligned_candidate["score"] == conflict_candidate["score"] + 2 and
      "HTF_ALIGNED" in aligned_candidate["evidence"] and
      "HTF_CONFLICT" in conflict_candidate["penalties"])
print("ALL PASS (test_htf_context)")

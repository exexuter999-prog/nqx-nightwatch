# -*- coding: utf-8 -*-
"""R14 execution contract boundary tests (pure, no order route)."""
from datetime import datetime, timedelta, timezone
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import execution_contract as contract  # noqa: E402


def check(label, actual, expected=True):
    if actual != expected:
        raise AssertionError(f"{label}: expected {expected!r}, got {actual!r}")
    print(f"OK   {label}")


NOW = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)


def scenario(**overrides):
    value = {
        "scenarioId": "r14-boundary", "state": "ARMED", "grade": "A",
        "issuedAt": NOW.isoformat(), "expiresAt": (NOW + timedelta(minutes=5)).isoformat(),
        "side": "BUY", "entry": 20000.0, "stop": 19940.0, "qty": 2,
        "targets": [20060.0, 20120.0],
        "legs": [{"id": "TP1", "qty": 1, "target": 20060.0},
                 {"id": "RUNNER", "qty": 1, "target": 20120.0}],
        "planVersion": "R17-SPLIT-1",
    }
    value.update(overrides)
    return value


def market(age_sec=0, **overrides):
    value = {
        "at": (NOW - timedelta(seconds=age_sec)).isoformat(),
        "cvdAt": NOW.isoformat(),
    }
    value.update(overrides)
    return value


accepted = contract.evaluate(scenario(), market(599), now=NOW)
check("599s market is still orderable", accepted["orderable"])
check("two MNQ x 60pt equals $240", accepted["riskDollars"], 240.0)
check("risk cap source is retained", bool(accepted["riskCapSource"]))

blocked = contract.evaluate(scenario(), market(601), now=NOW)
check("601s market is blocked", "MARKET_STALE" in blocked["blockers"])
check("stale market blocks a fresh scenario", not blocked["orderable"])

# 2026-09-04 ユーザー決定: B も発注可能。allowedGrades = A / A+ / B。
# 等級ゲート自体は残す —— 契約に無い等級(例: C / 空)は今までどおり止める。
b_grade = contract.evaluate(scenario(grade="B"), market(), now=NOW)
check("B grade is orderable (2026-09-04)", "GRADE_NOT_ORDERABLE" not in b_grade["blockers"])
c_grade = contract.evaluate(scenario(grade="C"), market(), now=NOW)
check("unknown grade C is still blocked", "GRADE_NOT_ORDERABLE" in c_grade["blockers"])

blackout = contract.evaluate(scenario(), market(eventBlackout=True), now=NOW)
check("event blackout is a hard blocker", "EVENT_BLACKOUT" in blackout["blockers"])

missing_cvd = contract.evaluate(scenario(grade="A+"), market(cvdAt=None), now=NOW)
check("CVD timestamp limits A+ to A", missing_cvd["effectiveGrade"], "A")
check("CVD timestamp requires reacquisition", "CVD_REACQUIRE_REQUIRED" in missing_cvd["acquisitionRequired"])
check("CVD cap is explicit", "CVD_A_PLUS_CAPPED" in missing_cvd["caps"])

stale_cvd = contract.evaluate(scenario(grade="A+"), market(cvdAt=(NOW - timedelta(seconds=601)).isoformat()), now=NOW)
check("stale CVD also caps A+", "CVD_STALE" in stale_cvd["caps"] and stale_cvd["effectiveGrade"] == "A")

over_cap = contract.evaluate(scenario(stop=19939.9975), market(), now=NOW)
check("$240.01 is rejected", "RISK_CAP_EXCEEDED" in over_cap["blockers"])

ended = contract.evaluate(scenario(sessionEndAt=(NOW - timedelta(seconds=1)).isoformat()), market(), now=NOW)
check("session end blocks new entries", "SESSION_ENDED" in ended["blockers"])

open_position = contract.evaluate(scenario(), market(), position={"state": "OPEN"}, now=NOW)
check("open position blocks new entry", "POSITION_OPEN" in open_position["blockers"])

pending = contract.evaluate(scenario(), market(), order={"state": "PENDING"}, now=NOW)
check("pending order blocks duplicate entry", "ORDER_PENDING" in pending["blockers"])

partial = contract.evaluate(scenario(), market(), order={"state": "PARTIAL"}, now=NOW)
check("partial route blocks duplicate entry", "ORDER_PENDING" in partial["blockers"])

qty1 = contract.evaluate(scenario(qty=1), market(), now=NOW)
check("qty=1 is rejected by fixed contract", "FIXED_QTY_REQUIRED" in qty1["blockers"])

print("ALL PASS (test_execution_contract)")

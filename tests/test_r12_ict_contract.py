# -*- coding: utf-8 -*-
"""R12 ICT証拠・CVD再取得・分割型の境界をネットワークなしで検証する。"""
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import monitor_publish  # noqa: E402
import msnr_gate  # noqa: E402


def check(label, condition, detail=""):
    if not condition:
        raise AssertionError(f"{label}: {detail}")
    print(f"OK   {label}")


T0 = 1786970000 - (1786970000 % 180)
BARS = [{"t": T0 + index * 180, "o": 100 + index, "h": 106 + index,
         "l": 98 + index, "c": 102 + index, "v": 100}
        for index in range(16)]


def test_explicit_ict_anchor_replaces_rolling_3m_ote():
    bundle = {"rangeAnchor": {
        "rangeTf": "15m", "rangeStart": "2026-08-21T20:00:00+09:00",
        "rangeEnd": "2026-08-21T21:00:00+09:00", "anchorType": "HTF_DIRECTIONAL_LEG",
        "freshness": "FRESH", "high": 200, "low": 100}}
    anchor = msnr_gate.resolve_range_anchor(bundle, {}, 125)
    check("HTFアンカーが有効", anchor["valid"] is True and anchor["rangeTf"] == "15m", anchor)
    check("OTEが明示アンカーから計算される", anchor["oteBuy"] == [121.0, 138.0], anchor)
    rolling = msnr_gate.resolve_range_anchor(
        {"rangeAnchor": {**bundle["rangeAnchor"], "rangeTf": "3m"}}, {}, 125)
    check("ローリング3分足OTEを拒否", rolling["reason"] == "ROLLING_3M_OTE_FORBIDDEN", rolling)


def test_fvg_has_execution_evidence():
    bars = [
        {"t": T0, "o": 95, "h": 100, "l": 95, "c": 100},
        {"t": T0 + 180, "o": 100, "h": 120, "l": 100, "c": 118},
        {"t": T0 + 360, "o": 118, "h": 130, "l": 110, "c": 125},
    ]
    fvg = msnr_gate.fvg_scan(bars, 140, 2.0, noise=8)["BULL"][0]
    check("FVGに時間足・年齢・displacementを保存",
          fvg["timeframe"] == "3m" and fvg["ageBars"] == 0 and fvg["displacementR"] >= 1.3,
          fvg)
    check("到達前構造が保たれたFVGだけeligible", fvg["preArrivalStructure"] == "INTACT" and fvg["eligible"], fvg)


def test_smt_requires_fresh_same_session_peers():
    peers = {"ES": [{"t": b["t"], "h": b["h"] + 10, "l": b["l"] + 10} for b in BARS]}
    no_meta = msnr_gate.index_smt(BARS, peers, "2026-08-21T21:00:00+09:00")
    check("SMTはsessionIdなしで不成立", no_meta["reason"] == "SMT_SESSION_ID_REQUIRED", no_meta)
    stale = msnr_gate.index_smt(
        BARS, {"ES": peers["ES"][:-1]}, "2026-08-21T21:00:00+09:00",
        {"ES": {"sessionId": "NY-TEST"}}, "NY-TEST")
    check("SMTは同時刻でないpeerを拒否", stale["reason"] == "SMT_STALE_PEER", stale)


def _candidate(score=9):
    return {"score": score, "targetR": [3.0], "evidence": [], "penalties": [],
            "hardBlockers": []}


def test_cvd_refresh_then_a_plus_cap():
    missing = msnr_gate.cvd_health({}, {}, BARS)
    capped = msnr_gate._finalize_candidate(_candidate(), {"cvd": missing})
    check("CVD欠落は再取得要求", missing["refreshRequired"] is True and missing["status"] == "RETRY_REQUIRED", missing)
    check("欠落中はA+をAへ上限化", capped["grade"] == "A" and "CVD_A_PLUS_CAPPED" in capped["evidence"], capped)
    exhausted = msnr_gate.cvd_health({"cvdAttempts": 2}, {}, BARS)
    check("再取得上限後もA上限を維持", exhausted["status"] == "UNAVAILABLE_A_CAP" and not exhausted["refreshRequired"], exhausted)
    fresh = msnr_gate.cvd_health({"cvd": {"direction": "bullish"},
                                  "cvdMeta": {"status": "FRESH", "attempts": 1}}, {}, BARS)
    allowed = msnr_gate._finalize_candidate(_candidate(), {"cvd": fresh})
    check("fresh CVDではA+を許可", allowed["grade"] == "A+", allowed)


def test_monitor_exposes_retry_request_without_fetching_or_ordering():
    bundle = {"at": "2026-08-21T21:00:00+09:00", "price": 117,
              "snapshot": {"bars3m": BARS, "levels": [{"label": "TEST", "price": 110}]}}
    out, notes = monitor_publish.enrich_decisive_strategy(bundle)
    check("CVD再取得要求を監視出力へ載せる", bool(out.get("cvdRefresh")), (out, notes))
    check("再取得要求はA+制限として記録", any("A+ capped" in note for note in notes), notes)


def test_acquisition_receipt_becomes_compact_display_gate():
    receipt = {
        "requiredFresh": True,
        "sources": {
            "chart_state.json": {"required": True, "status": "FRESH", "ageSec": 6,
                                 "modifiedAt": "2026-08-24T16:24:57+00:00"},
            "bars3m.json": {"required": True, "status": "FRESH", "ageSec": 96,
                            "modifiedAt": "2026-08-24T16:23:27+00:00"},
            "study_3m.json": {"required": True, "status": "FRESH", "ageSec": 90,
                              "modifiedAt": "2026-08-24T16:23:33+00:00"},
            "pine_labels.json": {"required": True, "status": "FRESH", "ageSec": 72,
                                 "modifiedAt": "2026-08-24T16:23:51+00:00"},
        },
    }
    gate = monitor_publish.acquisition_display_gate(receipt)
    check("必須raw 4/4をUIゲートへ圧縮",
          gate["requiredFresh"] is True and gate["freshCount"] == 4
          and gate["requiredCount"] == 4, gate)
    check("最古ageと取得spanを保持",
          gate["oldestAgeSec"] == 96 and gate["sourceSpanSec"] == 90, gate)
    receipt["requiredFresh"] = False
    receipt["sources"]["study_3m.json"]["status"] = "STALE"
    stale = monitor_publish.acquisition_display_gate(receipt)
    check("1ソースでもSTALEなら表示ゲートも停止",
          stale["requiredFresh"] is False
          and stale["staleRequired"] == ["study_3m.json"], stale)
    missing = monitor_publish.acquisition_display_gate(None)
    check("受領書欠落も明示的な停止ゲートになる",
          missing["status"] == "MISSING" and missing["requiredFresh"] is False,
          missing)


test_explicit_ict_anchor_replaces_rolling_3m_ote()
test_fvg_has_execution_evidence()
test_smt_requires_fresh_same_session_peers()
test_cvd_refresh_then_a_plus_cap()
test_monitor_exposes_retry_request_without_fetching_or_ordering()
test_acquisition_receipt_becomes_compact_display_gate()
print("ALL PASS (test_r12_ict_contract)")

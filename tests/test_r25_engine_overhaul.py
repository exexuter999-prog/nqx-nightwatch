# -*- coding: utf-8 -*-
"""R25 ストラテジーエンジンのオーバーホール。

2026-08-24 に実サイクル 403 本を現エンジンで再生して見つかった構造欠陥:

  1. VP80 が「VA edge の幾何」で採点してから entry/stop を上書きしていた。
     SL 上限(60pt)も R 判定も別のトレードの値で通過し、A+ の SL が 78〜116pt、
     R が 0.0〜0.77 のまま武装していた(403 本中 199 本が武装)。
  2. VP80 の目標が建値の反対側に出ていた(VA 復帰が済んだ後も追従)。
  3. 「位置が逆」のラベルが、レンジ未取得のときにも付いていた(虚偽ラベル)。
  4. decisionId に時刻が混ざり、同じセットアップが 3 分ごとに別 ID だった。
  5. 構造 + レジーム + R:R だけで A に届き、独立した確認が無くても武装した。

ここで固定する不変条件:
  * どの経路でも、最終 entry/stop/targets に対して SL 上限・最小 R・方向整合を検証する
  * A/A+ には構造と独立したデータ源の確認が最低 1 つ要る
  * setupId は時刻に依存せず、建値の追従でも変わらない
  * レンジ未取得は「逆側」ではなく「未取得」と記録する
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
CHAIN = {"side": "BUY", "type": "SWEEP", "state": "RETEST_HELD", "sweepBarT": 0, "dispBody": 20}
LEVELS = [{"label": "PDH", "price": 30100}]
ROUTE = {"preferred": set(msnr_gate.MODEL_ORDER), "suppressed": set(), "rotation": False}
BASE_ICT = {"range": {"valid": True, "favors": {"BUY": True, "SELL": False}},
            "dol": {"BUY": {"target": 30100, "run": "LRLR"}}}


def cand(ict=BASE_ICT, **kw):
    return msnr_gate._candidate_for_chain(
        "TURTLE_SOUP_REVERSAL", LEVEL, CHAIN, BARS, LEVELS, ict, ROUTE, {"snapshot": {}}, 4.0, **kw)


def test_geometry_invariant_is_enforced_after_any_override():
    over_cap = cand(entry=30000.0, stop=30000.0 - (msnr_gate.sl_cap_pt() + 10))
    check("SL 上限超過は最終幾何で止まる", "RISK_CAP_EXCEEDED" in over_cap["hardBlockers"])
    # 採点後に幾何を差し替えても finalize が捕まえる
    sneaky = cand()
    sneaky["stop"] = sneaky["entry"] - 200
    msnr_gate._finalize_candidate(sneaky, BASE_ICT)
    check("採点後の差し替えも finalize が止める",
          "RISK_CAP_EXCEEDED" in sneaky["hardBlockers"] and sneaky["allowed"] is False)
    inverted = cand()
    inverted["targets"] = [inverted["entry"] - 30]        # BUY なのに目標が下
    inverted["targetR"] = [2.0]
    msnr_gate._finalize_candidate(inverted, BASE_ICT)
    check("方向と矛盾する目標は GEOMETRY_INVALID", "GEOMETRY_INVALID" in inverted["hardBlockers"])


def test_a_grade_requires_an_independent_confirmation():
    # 構造 + レジーム + R だけ(確認ゼロ)
    bare = cand(ict={"range": {}, "dol": BASE_ICT["dol"]})
    check("確認ゼロは A にならない",
          "NO_CONFIRMATION" in bare["hardBlockers"] and bare["allowed"] is False, bare["evidence"])
    with_location = cand()
    check("ICT 位置の確認があれば通る",
          "NO_CONFIRMATION" not in with_location["hardBlockers"], with_location["evidence"])
    check("確認の内訳が記録される", with_location["confirmations"] == ["ICT_LOCATION"])
    # displacement は構造の一部。単独では確認にならない。
    disp = cand(ict={"range": {}, "dol": BASE_ICT["dol"]})
    check("displacement だけでは確認にならない",
          "DISPLACEMENT_FRESH" in disp["evidence"] and "NO_CONFIRMATION" in disp["hardBlockers"])
    check("確認要素の集合に構造由来が含まれない",
          not ({"DISPLACEMENT_FRESH", "VP_ACCEPTED", "REGIME_FIT"} & msnr_gate.CONFIRMATION_EVIDENCE))


def test_missing_range_is_not_labelled_wrong_side():
    unknown = cand(ict={"range": {}, "dol": BASE_ICT["dol"]})
    check("レンジ未取得は RANGE_ANCHOR_MISSING",
          "RANGE_ANCHOR_MISSING" in unknown["penalties"]
          and "PREMIUM_DISCOUNT_WRONG_SIDE" not in unknown["penalties"])
    wrong = cand(ict={"range": {"valid": True, "favors": {"BUY": False, "SELL": True}},
                      "dol": BASE_ICT["dol"]})
    check("本当に逆側なら減点付きで WRONG_SIDE",
          "PREMIUM_DISCOUNT_WRONG_SIDE" in wrong["penalties"] and wrong["score"] < unknown["score"])


def test_setup_identity_ignores_time_and_entry_drift():
    a = cand()
    b = dict(a, entry=a["entry"] + 2.5)        # 建値が追従して動いた
    check("建値の追従で setupId は変わらない",
          msnr_gate.setup_identity(a) == msnr_gate.setup_identity(b))
    c = dict(a, stop=a["stop"] - 5)
    check("SL が変われば別セットアップ", msnr_gate.setup_identity(a) != msnr_gate.setup_identity(c))
    d1 = msnr_gate.select_primary([a], {"at": "2026-08-24T00:00:00Z"})
    d2 = msnr_gate.select_primary([a], {"at": "2026-08-24T00:03:00Z"})
    check("時刻が違っても decisionId は同じ", d1["decisionId"] == d2["decisionId"])
    check("decision に setupId と confirmations が載る",
          d1.get("setupId") and isinstance(d1.get("confirmations"), list))


def test_vp80_does_not_chase_a_completed_reversion():
    bars = [{"t": i * 180, "o": 29950, "h": 29960, "l": 29940, "c": 29950} for i in range(12)]
    levels = [{"label": "VAH", "price": 30100}, {"label": "VAL", "price": 30000}]
    # SELL の VA 復帰: 目標は VAL(30000)。建値が既に 29950 で目標より下。
    result = {"vpPath": {"state": "VP_ACCEPTED", "side": "SELL", "target": 30000}}
    out = msnr_gate.candidate_vp80(result, bars, levels, {}, ROUTE, {"snapshot": {}}, 4.0)
    check("復帰済みの VP80 は候補にしない", out is None)


test_geometry_invariant_is_enforced_after_any_override()
test_a_grade_requires_an_independent_confirmation()
test_missing_range_is_not_labelled_wrong_side()
test_setup_identity_ignores_time_and_entry_drift()
test_vp80_does_not_chase_a_completed_reversion()

if FAILED:
    print(f"FAILED: {len(FAILED)} -> {', '.join(FAILED)}")
    raise SystemExit(1)
print("ALL PASS (test_r25_engine_overhaul)")

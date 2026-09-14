# -*- coding: utf-8 -*-
"""R26 目標ラダーの是正と、反転を先回りする指値エントリー。

2026-08-24 に実サイクル 403 本を再生して測った欠陥:

  1. `model_targets()` が DOL を無条件に先頭へ置いていた。DOL は **64%**
     の確率で最も遠い目標なので、TP1 が runner より遠くなる。発注契約は
     splitPlan.targetCount=2 を 3 箇所で独立に強制しており(nqx_state /
     state_machine.js / execution_intent)、結果として構築された 246 シナリオ
     のうち **212 (86%)** がブローカー到達前に拒絶されていた。
  2. `candidate_vp80` が VA_TARGET を min_r フィルタの **外側** で prepend
     していた。実データに targetR[0]=1.4 (< MODEL_MIN_R) が存在し、targets が
     4 本になっていた。
  3. `_finalize_candidate` が targetR だけ [:3] で切り、targets/targetLabels を
     切っていなかった。354 候補中 35 件で配列長がずれていた。
  4. エンジンは `RETEST_HELD`(372 サイクルで 70 回)しか候補化せず、
     `MSS_CONFIRMED`(同 216 回)を捨てていた。後者は掃引済み = SL の根拠が
     確定しており、リテスト未到達 = 建値が現値の先にある。実測で **99%
     (213/216)** が指値として置ける向きだった。

ここで固定する不変条件:
  * ラダーは常にちょうど 2 本か 0 本。TP1 は最近接、runner は最も遠い質のある rung
  * targets / targetLabels / targetR の長さは常に一致する
  * VA_TARGET も DOL も min_r を素通りしない
  * 指値候補は建値が現値の先にあり、gapR <= LIMIT_MAX_GAP_R
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


# 実データそのままの反例: BUY entry 29331、DOL が最遠、POC が最近。
LEVELS = [{"label": "P: POC", "price": 29367.5},
          {"label": "London Low", "price": 29380.25},
          {"label": "Weekly High", "price": 29485.25}]
ICT = {"dol": {"BUY": {"target": 29485.25, "run": "LRLR"}}}


def test_tp1_is_nearest_and_runner_is_farthest():
    out = msnr_gate.model_targets(29331.0, 29311.0, "BUY", LEVELS, ICT)
    check("ラダーはちょうど 2 本", len(out) == 2, str(out))
    check("TP1 は最近接の適格目標", out[0][0] == 29367.5, str(out))
    check("runner は最遠", out[1][0] == 29485.25, str(out))
    check("方向に対して単調", out[0][0] < out[1][0])
    # SELL の鏡像
    levels = [{"label": "P: POC", "price": 29294.5},
              {"label": "Weekly Low", "price": 29176.75}]
    ict = {"dol": {"SELL": {"target": 29176.75, "run": "LRLR"}}}
    sell = msnr_gate.model_targets(29331.0, 29351.0, "SELL", levels, ict)
    check("SELL も単調", len(sell) == 2 and sell[0][0] > sell[1][0], str(sell))


def test_ladder_is_two_or_zero():
    one = msnr_gate.model_targets(29331.0, 29311.0, "BUY",
                                  [{"label": "P: POC", "price": 29367.5}], {})
    check("rung が 1 本なら不成立", one == [], str(one))
    # 間隔が LADDER_MIN_SEP_R 未満: runner が TP1 と実質同じ
    tight = msnr_gate.model_targets(
        29331.0, 29311.0, "BUY",
        [{"label": "P: POC", "price": 29367.5}, {"label": "P: VAH", "price": 29372.0}], {})
    check("間隔不足なら不成立", tight == [], str(tight))


def test_preferred_labels_do_not_jump_to_the_front():
    out = msnr_gate.model_targets(29331.0, 29311.0, "BUY", LEVELS, ICT)
    check("DOL は先頭に来ない(これが 86% 拒絶の原因だった)",
          out[0][1] != "DOL" and out[1][1] == "DOL", str(out))


def test_runner_avoids_bottom_tier_when_quality_exists():
    levels = [{"label": "P: POC", "price": 29367.5},
              {"label": "Weekly High", "price": 29470.0},
              {"label": "09:30", "price": 29520.0}]
    out = msnr_gate.model_targets(29331.0, 29311.0, "BUY", levels, {})
    check("tier<=3 があれば時刻ラベルを runner にしない",
          out[1][1] == "Weekly High", str(out))


def test_target_arrays_stay_the_same_length():
    bars = [{"t": i * 180, "o": 30000, "h": 30020, "l": 29980, "c": 30010} for i in range(3)]
    chain = {"side": "BUY", "type": "SWEEP", "state": "RETEST_HELD", "sweepBarT": 0, "dispBody": 20}
    level = {"label": "VAL", "price": 29990, "freshness": "FRESH"}
    route = {"preferred": set(msnr_gate.MODEL_ORDER), "suppressed": set(), "rotation": False}
    levels = [{"label": "PDH", "price": 30100}, {"label": "Weekly High", "price": 30260}]
    ict = {"range": {"favors": {"BUY": True}}, "dol": {"BUY": {"target": 30100, "run": "LRLR"}}}
    cand = msnr_gate._candidate_for_chain("TURTLE_SOUP_REVERSAL", level, chain, bars, levels,
                                          ict, route, {"snapshot": {}}, 4.0)
    check("targets/targetLabels/targetR の長さが一致",
          len(cand["targets"]) == len(cand["targetLabels"]) == len(cand["targetR"]) == 2,
          f"{len(cand['targets'])}/{len(cand['targetLabels'])}/{len(cand['targetR'])}")


def test_va_target_goes_through_the_min_r_filter():
    # VA_TARGET が min_r 未満なら採用しない(以前は prepend で素通りしていた)
    out = msnr_gate.model_targets(
        29331.0, 29311.0, "BUY", LEVELS, ICT,
        extra=[(29341.0, "VA_TARGET")])          # 0.5R しかない
    check("min_r 未満の VA_TARGET は除外", all(t[1] != "VA_TARGET" for t in out), str(out))
    pool_out = msnr_gate.target_pool(29331.0, 29311.0, "BUY", LEVELS, ICT,
                                     extra=[(29371.0, "VA_TARGET")])
    check("min_r 以上の VA_TARGET は pool に入る",
          any(t[1] == "VA_TARGET" for t in pool_out), str(pool_out))
    check("pool は距離順",
          [t[0] for t in pool_out] == sorted(t[0] for t in pool_out), str(pool_out))
    low = msnr_gate.target_pool(29331.0, 29311.0, "BUY", LEVELS, ICT,
                                extra=[(29341.0, "VA_TARGET")])
    check("min_r 未満の VA_TARGET は pool にも入らない",
          all(t[1] != "VA_TARGET" for t in low), str(low))


def test_resting_limit_requires_the_entry_ahead_of_price():
    # 買い指値は現値より下(order.py:784-792 が即約定する指値を拒否する)
    buy = {"entry": 29900.0, "stop": 29880.0}
    check("買い: 建値が現値より下なら成立",
          msnr_gate._resting_limit_ok(buy, 29910.0))
    check("買い: 建値が現値以上なら不成立(即約定する)",
          not msnr_gate._resting_limit_ok(buy, 29895.0))
    sell = {"entry": 29900.0, "stop": 29920.0}
    check("売り: 建値が現値より上なら成立",
          msnr_gate._resting_limit_ok(sell, 29890.0))
    check("売り: 建値が現値以下なら不成立",
          not msnr_gate._resting_limit_ok(sell, 29905.0))


def test_resting_limit_rejects_a_gap_that_is_too_far():
    cand = {"entry": 29900.0, "stop": 29880.0}          # risk 20pt
    check("gapR 1.5 までは許す", msnr_gate._resting_limit_ok(cand, 29930.0))
    check("gapR が上限を超えたら不成立", not msnr_gate._resting_limit_ok(cand, 29935.0))
    check("リスク 0 は不成立", not msnr_gate._resting_limit_ok({"entry": 1.0, "stop": 1.0}, 2.0))
    check("欠損は不成立", not msnr_gate._resting_limit_ok({}, 1.0))


test_tp1_is_nearest_and_runner_is_farthest()
test_ladder_is_two_or_zero()
test_preferred_labels_do_not_jump_to_the_front()
test_runner_avoids_bottom_tier_when_quality_exists()
test_target_arrays_stay_the_same_length()
test_va_target_goes_through_the_min_r_filter()
test_resting_limit_requires_the_entry_ahead_of_price()
test_resting_limit_rejects_a_gap_that_is_too_far()

if FAILED:
    print(f"FAILED: {len(FAILED)} -> {', '.join(FAILED)}")
    raise SystemExit(1)
print("ALL PASS (test_r26_ladder_and_limit)")

# -*- coding: utf-8 -*-
"""R27 火力を上げる — 導出レンジ、SL の下限、VP80 の走査範囲、破棄の検出。

2026-08-24 に実サイクル 403 本(221時間)で測った状態:

  * 確認要素 7 種の取得率が **全て 0%**。392 候補すべてが NO_CONFIRMATION で
    止まり、**武装 0**。うち 232 本(59%)は NO_CONFIRMATION **だけ**が理由で、
    156 本は A/A+ の等級だった。「安全に止めている」のではなく壊れて止まっていた。
  * VP80 の SL は VA edge の外に出た足を窓 60 本(= 3 時間)分走査して取っていた。
    SL 中央値 62pt(上限 60pt 超)、最大 278pt、**101 候補中 53 本が破棄**。
  * risk のチェックが全経路で**上限のみ**。下限が無く、OTE は
    risk = 0.085*span + buffer なので狭いレンジで 2pt 級まで落ちる。
    risk は R の分母なので、**退化した SL ほど高得点**という逆転が起きていた。
  * tombstoneCycle() は scenario を null にしながら accepted:true を返し、
    nqx_state.publish() は 200 なら本文を見ずに True を返していた。
    → Telegram は「ARMED」と表示、Mini App には何も出ない。

ここで固定する不変条件:
  * レベル集合の高安ペアからレンジを導出できる(現値がレンジ内・幅の下限あり)
  * 導出レンジは source="levels" が刻まれ、手入力アンカーと混ざらない
  * 3M のローリング高安からは絶対に作られない
  * SL はノイズフロアの下限を下回らない。どの上書き経路でも効く
  * OTE はレンジ幅の下限を満たさなければ候補化しない
  * VP80 の SL は今回のエピソード(reEntryBarT 以降)だけから取る
  * CYCLE_TOMBSTONED は publish の失敗として扱う
"""
import json
import os
import sys
from unittest.mock import patch

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import msnr_gate  # noqa: E402
import monitor_publish  # noqa: E402
import nqx_state  # noqa: E402

FAILED = []


def check(name, condition, detail=""):
    print(("  OK   " if condition else "  FAIL ") + name
          + (f" ({detail})" if detail and not condition else ""))
    if not condition:
        FAILED.append(name)


def levels_pair(hi=30200.0, lo=30000.0):
    return [{"label": "New York High", "price": hi}, {"label": "New York Low", "price": lo}]


# ── 導出レンジ ───────────────────────────────────────────────────────────
def test_range_is_derived_from_session_levels():
    out = msnr_gate.derive_range_anchor({"levels": levels_pair()}, 30100.0)
    check("セッション高安からレンジが導出される", bool(out and out.get("valid")), str(out))
    check("出所が刻まれる", out.get("source") == "levels" and "DERIVED" in out.get("anchorType", ""),
          str(out))
    check("現値の位置が判定される", out.get("position") in {"PREMIUM", "DISCOUNT", "EQ"})
    check("OTE 帯が算出される", len(out.get("oteBuy") or []) == 2 and len(out.get("oteSell") or []) == 2)


def test_derived_range_rejects_bad_geometry():
    check("現値がレンジ外なら不成立",
          msnr_gate.derive_range_anchor({"levels": levels_pair()}, 30500.0) is None)
    check("幅が下限未満なら不成立",
          msnr_gate.derive_range_anchor({"levels": levels_pair(30020.0, 30000.0)}, 30010.0) is None)
    check("高安が逆なら不成立",
          msnr_gate.derive_range_anchor({"levels": levels_pair(30000.0, 30200.0)}, 30100.0) is None)
    check("片方しか無ければ不成立",
          msnr_gate.derive_range_anchor(
              {"levels": [{"label": "New York High", "price": 30200.0}]}, 30100.0) is None)
    check("レベルが無ければ不成立", msnr_gate.derive_range_anchor({"levels": []}, 30100.0) is None)


def test_in_progress_sessions_never_anchor_a_range():
    """R36: 進行中セッションの高安はレンジの起点にしない。

    進行中の高安は毎サイクル広がるローリング極値で、R12 が OTE に使うことを
    禁じた「ローリング 3M 高安」と実質同じ。名前が付いているだけで通すのは
    筋が通らない。`settled: False` を明示する水準だけを弾く —— インジケータ
    由来の PDH/PDL などは元から確定値なので従来どおり通す。
    """
    running = [{"label": "New York High", "price": 30200.0, "settled": False},
               {"label": "New York Low", "price": 30000.0, "settled": False}]
    check("進行中しか無ければレンジを作らない",
          msnr_gate.derive_range_anchor({"levels": running}, 30100.0) is None)
    mixed = running + [{"label": "London High", "price": 30180.0, "settled": True},
                       {"label": "London Low", "price": 30020.0, "settled": True}]
    got = msnr_gate.derive_range_anchor({"levels": mixed}, 30100.0)
    check("終わったセッションは採用する",
          got and got["anchorType"] == "SESSION_LONDON_DERIVED", str(got))
    check("進行中の値を混ぜない", got and got["high"] == 30180.0 and got["low"] == 30020.0,
          str(got))
    # settled キーが無い水準(インジケータ由来)は従来どおり通る
    plain = [{"label": "New York High", "price": 30200.0},
             {"label": "New York Low", "price": 30000.0}]
    check("settled 未指定は従来どおり",
          msnr_gate.derive_range_anchor({"levels": plain}, 30100.0) is not None)


def test_explicit_anchor_wins_and_rolling_3m_still_forbidden():
    snapshot = {"levels": levels_pair(),
                "rangeAnchor": {"rangeTf": "3m", "rangeStart": "2026-08-24T00:00:00+09:00",
                                "rangeEnd": "2026-08-24T01:00:00+09:00",
                                "anchorType": "HTF", "freshness": "FRESH",
                                "high": 30200.0, "low": 30000.0}}
    out = msnr_gate.resolve_range_anchor({}, snapshot, 30100.0)
    check("明示アンカーがあれば導出へ落ちない",
          out.get("valid") is False and out.get("reason") == "ROLLING_3M_OTE_FORBIDDEN", str(out))
    # 明示アンカーが無いときだけ導出する
    fell_back = msnr_gate.resolve_range_anchor({}, {"levels": levels_pair()}, 30100.0)
    check("明示アンカーが無ければ導出する", fell_back.get("source") == "levels", str(fell_back))


# ── SL の下限 ────────────────────────────────────────────────────────────
def test_stop_below_noise_floor_is_blocked():
    tight = {"entry": 30000.0, "stop": 29998.0, "side": "BUY",
             "targets": [30010.0], "targetR": [5.0], "noiseFloor": 20.0}
    check("ノイズフロア未満の SL は止まる",
          "RISK_BELOW_NOISE" in msnr_gate._geometry_blockers(tight))
    ok = dict(tight, stop=29975.0, targetR=[1.6])
    check("下限以上なら通る", "RISK_BELOW_NOISE" not in msnr_gate._geometry_blockers(ok))
    # ノイズフロアが不明でも絶対下限は効く
    no_nf = {"entry": 30000.0, "stop": 29999.0, "side": "BUY",
             "targets": [30010.0], "targetR": [5.0]}
    check("ノイズフロア不明でも絶対下限は効く",
          "RISK_BELOW_NOISE" in msnr_gate._geometry_blockers(no_nf))


def test_stop_floor_survives_a_post_scoring_override():
    """R25 の欠陥と同じ形(採点後の上書き)で迂回できないこと。"""
    bars = [{"t": i * 180, "o": 30000, "h": 30020, "l": 29980, "c": 30010} for i in range(3)]
    chain = {"side": "BUY", "type": "SWEEP", "state": "RETEST_HELD", "sweepBarT": 0, "dispBody": 20}
    level = {"label": "VAL", "price": 29990, "freshness": "FRESH"}
    route = {"preferred": set(msnr_gate.MODEL_ORDER), "suppressed": set(), "rotation": False}
    levels = [{"label": "PDH", "price": 30100}, {"label": "Weekly High", "price": 30260}]
    ict = {"range": {"favors": {"BUY": True}}, "dol": {"BUY": {"target": 30100, "run": "LRLR"}}}
    cand = msnr_gate._candidate_for_chain("TURTLE_SOUP_REVERSAL", level, chain, bars, levels,
                                          ict, route, {"snapshot": {}}, 20.0)
    check("候補にノイズフロアが載る", cand.get("noiseFloor") == 20.0)
    cand["stop"] = cand["entry"] - 1.0          # 採点後に SL を潰す
    msnr_gate._finalize_candidate(cand, ict)
    check("上書きしても下限が効く",
          "RISK_BELOW_NOISE" in cand["hardBlockers"] and cand["allowed"] is False)


def test_stop_buffer_uses_one_full_typical_bar():
    check("構造SLの外側に通常足1本分の緩衝を置く",
          msnr_gate.model_stop_buffer(18.0) == 18.0,
          str(msnr_gate.model_stop_buffer(18.0)))


# ── OTE のレンジ幅 ───────────────────────────────────────────────────────
def test_ote_requires_a_wide_enough_range():
    bars = [{"t": i * 180, "o": 30000, "h": 30020, "l": 29980, "c": 30010} for i in range(3)]
    chain = {"side": "BUY", "type": "SWEEP", "state": "RETEST_HELD", "sweepBarT": 0, "dispBody": 20}
    level = {"label": "VAL", "price": 29990, "freshness": "FRESH", "chains": [dict(chain)]}
    fvg = {"BULL": [{"eligible": True, "preArrivalStructure": "INTACT", "lo": 29950.0,
                     "hi": 29970.0, "timeframe": "3m", "ageBars": 3, "displacementR": 1.8}],
           "BEAR": []}

    def build(high, low, nf):
        rng = {"valid": True, "favors": {"BUY": True}, "position": "DISCOUNT",
               "oteBuy": [29950.0, 29970.0], "oteSell": [30030.0, 30050.0],
               "high": high, "low": low}
        ict = {"range": rng, "fvg": fvg, "rangeAnchor": rng,
               "dol": {"BUY": {"target": 30100, "run": "LRLR"}}}
        levels = [level, {"label": "PDH", "price": 30100}]
        cands = msnr_gate.build_candidates({"snapshot": {}, "regime": "TR"}, {"rotation": {}},
                                           bars, levels, ict, nf)
        return next((c for c in cands if c["model"] == "OTE_FVG_PULLBACK"), None)

    check("十分な幅なら OTE は出る", build(30100.0, 29900.0, 4.0) is not None)
    check("幅が下限未満なら OTE は出ない", build(30010.0, 29990.0, 4.0) is None)
    check("ノイズフロアが大きければ必要な幅も増える", build(30100.0, 29900.0, 40.0) is None)


# ── VP80 の走査範囲 ──────────────────────────────────────────────────────
def test_vp80_stop_ignores_excursions_before_this_episode():
    # 3 時間前に大きく外れた足を置き、エピソードは後半だけ。
    bars = []
    for i in range(40):
        if i == 2:
            bars.append({"t": i * 180, "o": 30000, "h": 30300, "l": 29990, "c": 30000})  # 古い突出
        else:
            bars.append({"t": i * 180, "o": 30000, "h": 30030, "l": 29990, "c": 30000})
    levels = [{"label": "C: VAH", "price": 30020.0}, {"label": "C: VAL", "price": 29900.0}]
    route = {"preferred": set(msnr_gate.MODEL_ORDER), "suppressed": set(), "rotation": False}
    vp = {"vpPath": {"state": "VP_ACCEPTED", "side": "SELL", "target": 29900.0,
                     "reEntryBarT": 30 * 180}}
    scoped = msnr_gate.candidate_vp80(vp, bars, levels, {}, route, {"snapshot": {}}, 8.0)
    wide = msnr_gate.candidate_vp80(dict(vpPath=dict(vp["vpPath"], reEntryBarT=0)),
                                    bars, levels, {}, route, {"snapshot": {}}, 8.0)
    check("エピソード内の SL は古い突出を含まない",
          scoped is not None and abs(scoped["entry"] - scoped["stop"]) < 60,
          str(scoped and abs(scoped["entry"] - scoped["stop"])))
    check("窓全体だと古い突出を拾って広くなる",
          wide is not None and abs(wide["entry"] - wide["stop"])
          > abs(scoped["entry"] - scoped["stop"]),
          str(wide and abs(wide["entry"] - wide["stop"])))


# ── 破棄の検出 ───────────────────────────────────────────────────────────
def test_tombstoned_publish_is_not_reported_as_success():
    class _Resp:
        def __init__(self, body):
            self._body = body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps(self._body).encode()

    cfg = {"NQX_ACCOUNT_ID": "acct", "NQX_PUBLISH_SECRET": "s",
           "NQX_API_BASE": "https://example.invalid"}

    tomb = {"ok": True, "seq": 3, "transitions": [],
            "reason": "CYCLE_TOMBSTONED: market invalid: bad tick",
            "view": {"scenario": None}}
    with patch("urllib.request.urlopen", return_value=_Resp(tomb)), \
            patch.object(nqx_state, "next_revision", return_value=1), \
            patch.object(nqx_state, "_audit", lambda *a, **k: None):
        ok, detail = nqx_state.publish("cycle", {"x": 1}, cfg)
    check("破棄は失敗として返る", ok is False, str(detail))
    check("理由が呼び出し側へ渡る",
          str(detail.get("reason", "")).startswith("CYCLE_TOMBSTONED"), str(detail))

    fine = {"ok": True, "seq": 4, "transitions": [], "view": {"scenario": {"state": "ARMED"}}}
    with patch("urllib.request.urlopen", return_value=_Resp(fine)), \
            patch.object(nqx_state, "next_revision", return_value=1), \
            patch.object(nqx_state, "_audit", lambda *a, **k: None):
        ok2, _ = nqx_state.publish("cycle", {"x": 1}, cfg)
    check("正常な publish は成功のまま", ok2 is True)


def test_volatility_band_arms_grade_a_below_standdown():
    """2026-09-01: 中間帯の「A+のみ」を開放した。停止帯(>0.60)は不変。"""
    vg_mid = {"active": True, "noise": 30.0, "ratio_all": 0.50, "standdown": False}
    grade_a, note = monitor_publish.apply_volatility_grade_gate(
        {"state": "ARMED", "grade": "A"}, vg_mid)
    grade_aplus, note_aplus = monitor_publish.apply_volatility_grade_gate(
        {"state": "ARMED", "grade": "A+"}, vg_mid)
    check("停止帯より下ならAも武装を維持する",
          grade_a["state"] == "ARMED" and note is None, str((grade_a, note)))
    check("同じ帯でA+も火力を維持する",
          grade_aplus["state"] == "ARMED" and note_aplus is None, str(grade_aplus))
    vg_hot = {"active": True, "noise": 42.0, "ratio_all": 0.70, "standdown": True}
    hot, hot_note = monitor_publish.apply_volatility_grade_gate(
        {"state": "ARMED", "grade": "A+"}, vg_hot)
    check("停止帯はA+でもWATCHへ落とす",
          hot["state"] == "WATCH" and "stand-down" in hot_note, str((hot, hot_note)))
    # 帯そのものは死んでいない。閾値を下げれば A+ 限定へ戻せる(環境変数で可逆)。
    with patch.object(monitor_publish, "VOL_GATE_APLUS", 0.40):
        restored, restored_note = monitor_publish.apply_volatility_grade_gate(
            {"state": "ARMED", "grade": "A"}, vg_mid)
    check("閾値を下げればA+限定帯は復活する",
          restored["state"] == "WATCH" and "A+ only" in restored_note,
          str((restored, restored_note)))


test_range_is_derived_from_session_levels()
test_derived_range_rejects_bad_geometry()
test_in_progress_sessions_never_anchor_a_range()
test_explicit_anchor_wins_and_rolling_3m_still_forbidden()
test_stop_below_noise_floor_is_blocked()
test_stop_floor_survives_a_post_scoring_override()
test_stop_buffer_uses_one_full_typical_bar()
test_ote_requires_a_wide_enough_range()
test_vp80_stop_ignores_excursions_before_this_episode()
test_tombstoned_publish_is_not_reported_as_success()
test_volatility_band_arms_grade_a_below_standdown()

if FAILED:
    print(f"FAILED: {len(FAILED)} -> {', '.join(FAILED)}")
    raise SystemExit(1)
print("ALL PASS (test_r27_firepower)")

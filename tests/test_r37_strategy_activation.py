# -*- coding: utf-8 -*-
"""R37 画像カタログ層の通電と、その前提となる方向欠陥の修正。

## 背景（403 サイクルの再生で判明した事実）

- `build_strategy_matrix().alignment` は **403/403 サイクルで `BUY=0 SELL=0`**。
  `IMAGE_MODEL_ALIGNMENT` も `IMAGE_MODEL_CONFLICT` も一度も発火していなかった
- 原因は 2 段。`_repo2_lifecycle` が欠落した `sessionId`/`freshness` を文字列
  `"UNKNOWN"` で埋め、`_vote_direction` がそれを fail-close していた
  （`crt` は 212/403 で投票直前まで到達して全部棄却）。加えて
  `vwapReversion`/`rejectionWick` は `valid` を、`mmxm`/`amdWyckoff` は `status` を
  返しておらず、構造的に一票も投じられなかった
- 層に票を持たせると、**新規武装が最大 111 件増え、その 110 件は画像合議が唯一の
  確認**になる（`IMAGE_MODEL_ALIGNMENT` が `CONFIRMATION_EVIDENCE` の一員で
  `NO_CONFIRMATION` がハードブロッカーだったため）

## この順序でなければならない理由

方向を誤る検出器を抱えたまま通電すると、その誤った方向が唯一の武装根拠になる。
先に方向欠陥（CRT の Double Purge / IFVG の方向分離 / rejectionWick のヒゲ比較）を
潰し、同時に `IMAGE_MODEL_ALIGNMENT` をハードゲートの鍵から外す。層は score へ
±2 で効き続ける —— 武装の唯一の鍵にならないだけである。
"""
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import msnr_gate  # noqa: E402
import strategy_models  # noqa: E402

FAILED = []


def check(name, condition, detail=""):
    print(("  OK   " if condition else "  FAIL ") + name
          + (f" ({detail})" if detail and not condition else ""))
    if not condition:
        FAILED.append(name)


def bar(t, o, h, l, c):
    return {"t": t, "o": o, "h": h, "l": l, "c": c}


# --- 1. CRT: 上下同時パージは方向を作らない --------------------------------

def test_crt_double_purge_has_no_direction():
    both = strategy_models.normalize_bars([
        bar(0, 100, 102, 98, 100), bar(180, 100, 102, 98, 100),
        bar(360, 100, 110, 90, 100),          # 上下とも刈って内側へ戻る
    ])
    got = strategy_models.detect_crt(both)
    check("Double Purge は方向を作らない", got["direction"] is None, str(got))
    check("Double Purge は valid にしない", got["valid"] is False, str(got))
    check("Double Purge は型として記録する",
          got["type"] == "DOUBLE_PURGE_CRT" and got["sweep"] == "BOTH", str(got))

    up = strategy_models.normalize_bars([
        bar(0, 100, 102, 98, 100), bar(180, 100, 102, 98, 100),
        bar(360, 100, 110, 99, 100),
    ])
    down = strategy_models.normalize_bars([
        bar(0, 100, 102, 98, 100), bar(180, 100, 102, 98, 100),
        bar(360, 100, 101, 90, 100),
    ])
    up_got, down_got = strategy_models.detect_crt(up), strategy_models.detect_crt(down)
    check("片側パージは従来どおり方向を出す",
          up_got["direction"] == "SELL" and down_got["direction"] == "BUY",
          f"{up_got['direction']} / {down_got['direction']}")
    # 以前は両側パージの出力が「上のみ」と完全に一致していた（下側の掃引が消える）
    check("両側パージは片側パージと区別できる",
          strategy_models.detect_crt(both)["sweep"] != up_got["sweep"])


# --- 2. IFVG: 方向は valid を立てたゾーンから取る --------------------------

BUY_CONFIRMED = {"kind": "IFVG", "lo": 99.0, "hi": 101.0, "direction": "BUY",
                 "breached": True, "inverseReclaim": True,
                 "structureIntact": True, "hold": True,
                 "active": True, "freshness": "FRESH"}
SELL_UNCONFIRMED = {"kind": "IFVG", "lo": 99.5, "hi": 100.5, "direction": "SELL",
                    "breached": False, "active": True, "freshness": "FRESH"}


def test_ifvg_direction_matches_the_confirming_zone():
    forward = strategy_models.detect_ifvg(
        {"ifvg": [dict(BUY_CONFIRMED), dict(SELL_UNCONFIRMED)]}, {}, 100.0)
    reverse = strategy_models.detect_ifvg(
        {"ifvg": [dict(SELL_UNCONFIRMED), dict(BUY_CONFIRMED)]}, {}, 100.0)
    check("確認済みが BUY なら direction も BUY",
          forward["valid"] is True and forward["direction"] == "BUY", str(forward))
    check("配列順を入れ替えても方向が変わらない",
          reverse["direction"] == forward["direction"],
          f"{forward['direction']} / {reverse['direction']}")


# --- 3. rejectionWick: 支配的なヒゲとヒゲ先端のレベル -----------------------

def test_rejection_wick_compares_both_wicks():
    bars = strategy_models.normalize_bars([
        bar(0, 100, 101, 99, 100), bar(180, 100, 101, 99, 100),
        bar(360, 100, 120, 99.75, 100),       # 上ヒゲ 20pt / 下ヒゲ 0.25pt
    ])
    tip = strategy_models.detect_rejection_wick(bars, [{"price": 120.0}])
    check("上ヒゲ支配なら SELL", tip["direction"] == "SELL", str(tip))
    check("ヒゲ先端のレベルで成立する",
          tip["status"] == "CONFIRMED" and tip["level"] == 120.0, str(tip))
    close_side = strategy_models.detect_rejection_wick(bars, [{"price": 100.5}])
    check("終値近傍の無関係なレベルでは成立しない",
          close_side["status"] == "WATCH" and close_side["level"] is None, str(close_side))
    # 最初の一致ではなく最近傍を採る
    many = strategy_models.detect_rejection_wick(
        bars, [{"price": 121.5}, {"price": 120.25}])
    check("最近傍のレベルを採る", many["level"] == 120.25, str(many))


# --- 4. 投票に必要なキーが揃っている ---------------------------------------

def test_detectors_expose_vote_keys():
    bars = strategy_models.normalize_bars(
        [bar(i * 180, 100, 101, 99, 100) for i in range(6)]
        + [bar(6 * 180, 100, 101, 90, 100)])
    vwap = strategy_models.detect_vwap_reversion({"vwap": 120.0}, bars, 100.0)
    check("vwapReversion が valid を返す", "valid" in vwap, str(vwap))
    wick = strategy_models.detect_rejection_wick(bars, [])
    check("rejectionWick が valid を返す", "valid" in wick, str(wick))
    amd = strategy_models.classify_amd(bars)
    check("amdWyckoff が status を返す", "status" in amd, str(amd))
    mmxm = strategy_models.detect_mmxm(amd, {"dol": {}}, {}, bars)
    check("mmxm が status を返す", "status" in mmxm, str(mmxm))
    check("レンジ未取得の mmxm は fail-closed",
          mmxm["valid"] is False and "RANGE_ANCHOR_REQUIRED" in (mmxm.get("evidence") or []),
          str(mmxm))


# --- 5. 欠落を "UNKNOWN" に化けさせない ------------------------------------

def test_absent_session_does_not_forge_a_denial():
    models = {"crt": {"status": "CONFIRMED", "valid": True, "direction": "BUY",
                      "freshness": "FRESH", "entryTouched": True}}
    strategy_models._repo2_lifecycle(models, {"at": "2026-08-24T00:00:00+00:00"})
    check("sessionId を捏造しない", models["crt"].get("sessionId") is None,
          str(models["crt"].get("sessionId")))
    check("sessionId が無くても投票できる",
          strategy_models._vote_direction(models["crt"]) == "BUY", str(models["crt"]))
    # 明示的な否定宣言は従来どおり fail-close
    denied = {"crt": {"status": "CONFIRMED", "valid": True, "direction": "BUY",
                      "freshness": "STALE", "entryTouched": True}}
    strategy_models._repo2_lifecycle(denied, {})
    check("明示的な STALE は従来どおり弾く",
          strategy_models._vote_direction(denied["crt"]) is None, str(denied["crt"]))


# --- 6. 相関する検出器は 1 票に畳む ----------------------------------------

def test_correlated_detectors_share_one_vote():
    votable = {"status": "CONFIRMED", "valid": True, "direction": "BUY",
               "freshness": "FRESH", "provenance": "fixture", "entryTouched": True,
               "lifecycle": "confirmed"}
    votes = strategy_models._eligible_votes({
        "crt": dict(votable), "fib": dict(votable), "fibCrt": dict(votable),
        "mmxm": dict(votable), "amdWyckoff": dict(votable),
        "vwapReversion": dict(votable),
    })
    check("CRT 族は 1 票", votes["BUY"].count("crt") + votes["BUY"].count("fib")
          + votes["BUY"].count("fibCrt") == 1, str(votes))
    # mmxm は classify_amd の出力を入力に取るので独立票にすると二重計上になる
    check("AMD 族も 1 票", votes["BUY"].count("mmxm") + votes["BUY"].count("amdWyckoff") == 1,
          str(votes))
    check("無相関の検出器は独立票", "vwapReversion" in votes["BUY"], str(votes))


# --- 7. 画像合議はハードゲートの鍵にならない -------------------------------

def test_image_consensus_is_ranking_only():
    check("IMAGE_MODEL_ALIGNMENT は確認要素ではない",
          "IMAGE_MODEL_ALIGNMENT" not in msnr_gate.CONFIRMATION_EVIDENCE,
          str(sorted(msnr_gate.CONFIRMATION_EVIDENCE)))
    # 画像合議だけを持つ候補は NO_CONFIRMATION で止まる
    candidate = {"score": 12, "targetR": [3.0], "evidence": ["IMAGE_MODEL_ALIGNMENT"],
                 "penalties": [], "hardBlockers": [], "entry": 100.0, "stop": 90.0,
                 "targets": [130.0], "targetLabels": ["T1"], "side": "BUY"}
    out = msnr_gate._finalize_candidate(candidate, {})
    check("画像合議だけでは武装しない",
          "NO_CONFIRMATION" in out["hardBlockers"] and out["state"] != "ARMED", str(out))


# --- 8. regime 欠落が最大加点に化けない ------------------------------------

def test_unknown_regime_is_neutral_not_preferred():
    routed = msnr_gate.route_regime({"regime": "MX"}, {})
    check("未知の regime は preferred を空にする", routed["preferred"] == set(), str(routed))
    check("未知の regime は記録に残る", routed.get("regimeKnown") is False, str(routed))
    known = msnr_gate.route_regime({"regime": "BA"}, {})
    check("既知の regime は従来どおり", "VP80_REVERSION" in known["preferred"], str(known))


# --- 9. CVD は「方向として使える形」でなければ健全ではない -------------------

def test_cvd_health_requires_a_usable_bias():
    bars = strategy_models.normalize_bars([bar(i * 180, 100, 101, 99, 100) for i in range(5)])
    bare = msnr_gate.cvd_health({"cvd": 69766}, {}, bars)
    check("素の int は A+ を許可しない",
          bare["aplusAllowed"] is False and bare["reason"] == "CVD_BIAS_UNUSABLE", str(bare))
    no_bias = msnr_gate.cvd_health({"cvd": {"value": 69766}}, {}, bars)
    check("bias の無い dict も許可しない", no_bias["aplusAllowed"] is False, str(no_bias))
    usable = msnr_gate.cvd_health({"cvd": {"value": 1, "bias": "BEARISH"},
                                   "cvdMeta": {"status": "FRESH", "attempts": 1}}, {}, bars)
    check("使える bias があれば FRESH",
          usable["aplusAllowed"] is True and usable["status"] == "FRESH", str(usable))
    # 健全性と採点が同じ判定を共有していること（片方だけ変えられない構造）
    check("健全性と採点が同じ判定を使う",
          msnr_gate.cvd_bias({"bias": "BEARISH"}) == "BEARISH"
          and msnr_gate.cvd_bias(69766) is None)


test_crt_double_purge_has_no_direction()
test_ifvg_direction_matches_the_confirming_zone()
test_rejection_wick_compares_both_wicks()
test_detectors_expose_vote_keys()
test_absent_session_does_not_forge_a_denial()
test_correlated_detectors_share_one_vote()
test_image_consensus_is_ranking_only()
test_unknown_regime_is_neutral_not_preferred()
test_cvd_health_requires_a_usable_bias()

if FAILED:
    print(f"FAILED: {len(FAILED)} -> {', '.join(FAILED)}")
    raise SystemExit(1)
print("ALL PASS (test_r37_strategy_activation)")

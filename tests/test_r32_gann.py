# -*- coding: utf-8 -*-
"""R32 Gann 1×1 アングルと同心円。

画像デッキの「円のフィボナッチ」は実際には **Gann** だった。
タイ語の「45 องศา」= 45 度。1×1 は「価格 1 単位 = 時間 1 単位」を意味する。

## 尺度依存をどう外したか

チャート上の円は本来、1 ドルを何 px で描くかに依存して形が変わる。
**スイングそのものを単位に取る**ことで表示倍率から独立させた:

    価格単位 = |スイング高 − スイング安|
    時間単位 = その 2 点間の足数

## 実装中に踏んだこと

lookback 2 のフラクタルは足のわずかな揺れまで拾い、実データで
「9 本 26pt」という無意味なアンカーが選ばれた。成立率も 36% しか無かった。
lookback を 3 にし、価格単位に「足レンジ中央値の 3 倍以上」という
有意性の下限を入れたところ:

    成立率 36% → **100%**、価格単位 中央値 **79.8pt**(p10 53.0)

小さすぎる単位だと 1×1 の傾きが小さくなり、結果として「どの水準にも近い」
ことになってしまう。下限はそれを防ぐためのもの。

## 証拠としての扱い

**Gann に実証的裏付けは無い。** 実トレード記録も 1 件で検証しようがない。

当初は助言専用(表示のみ)にしていたが、R33 で利用者の明示的な指示により
**裏付けが無くてもトレードに影響させる**ことにした。影響のさせ方は既存の
HRLR / killzone と同じ「加点・減点」に留める:

    建値が 1×1 に乗っている        → +1 GANN_1X1_NODE
    建値→TP1 の経路を 1×1 が横切る → −1 GANN_1X1_ROADBLOCK

**ハードゲートにも CONFIRMATION_EVIDENCE にもしない。** 確認要素にすると
裏付けの無いものが単独で武装根拠になってしまう。ここではその線引きを固定する。

ここで固定する不変条件:
  * 単位はスイングから取り、表示倍率に依存しない
  * 有意でないスイングはアンカーにしない
  * 上昇レッグの 1×1 は下向き(支持)、下降レッグは上向き(抵抗)
  * 円 k の水平節目は 中心 ± k × 価格単位
  * 採点には効くが、ハードブロッカーも確認要素も増やさない
  * 方向票(_eligible_votes)には出さない
"""
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import gann  # noqa: E402
import msnr_gate  # noqa: E402

FAILED = []


def check(name, condition, detail=""):
    print(("  OK   " if condition else "  FAIL ") + name
          + (f" ({detail})" if detail and not condition else ""))
    if not condition:
        FAILED.append(name)


def leg(down=True, n=40):
    """はっきりしたレッグを作る。

    down=True  : 高値が**先**、安値が**後** → 下降レッグ
    down=False : 安値が**先**、高値が**後** → 上昇レッグ

    最後に確定した点がどちらかで direction が決まるので、極値の**順序**が
    fixture の肝。ここを間違えると上昇レッグでも DOWN と判定される。
    """
    bars = []
    for i in range(n):
        base = 30000 - i * 5 if down else 30000 + i * 5
        bars.append({"t": i * 180, "o": base, "h": base + 3, "l": base - 3, "c": base})
    if down:
        bars[5]["h"] = 30100          # 先: スイング高
        bars[n - 6]["l"] = 29700      # 後: スイング安
    else:
        bars[5]["l"] = 29900          # 先: スイング安
        bars[n - 6]["h"] = 30300      # 後: スイング高
    return bars


def test_unit_comes_from_the_swing_not_the_screen():
    bars = leg(down=True)
    anc = gann.anchor(bars)
    check("アンカーが取れる", anc is not None, "None")
    if not anc:
        return
    check("価格単位 = 高安の幅",
          abs(anc["priceUnit"] - abs(anc["high"] - anc["low"])) < 1e-6, str(anc))
    check("時間単位 = 2点間の足数",
          anc["timeUnit"] == abs(anc["highIndex"] - anc["lowIndex"]), str(anc))
    # R36: 傾きはスイング平均速度の **半分**。そのままだと線が価格を追い越し、
    # 上昇レッグで 1×1 が支持側に来たのは実測 188 本中 1 本だった。
    check("スイング速度は素の比のまま",
          abs(anc["swingSlopePerBar"] - anc["priceUnit"] / anc["timeUnit"]) < 1e-3, str(anc))
    check("1×1 の傾きはその半分",
          abs(anc["pricePerBar"] - anc["swingSlopePerBar"] * gann.GANN_SLOPE_DAMPING) < 1e-3,
          str(anc))
    # 価格を 10 倍しても「単位比」は変わらない = 表示倍率に依存しない
    scaled = [{"t": b["t"], "o": b["o"] * 10, "h": b["h"] * 10,
               "l": b["l"] * 10, "c": b["c"] * 10} for b in bars]
    anc2 = gann.anchor(scaled)
    check("価格を 10 倍しても時間単位は不変", anc2 and anc2["timeUnit"] == anc["timeUnit"])
    check("価格単位も 10 倍に比例", anc2 and abs(anc2["priceUnit"] - anc["priceUnit"] * 10) < 1.0,
          str(anc2 and anc2["priceUnit"]))


def test_insignificant_swings_are_rejected():
    # ほぼ横ばい: 高安の幅が足レンジ中央値の 3 倍に届かない
    flat = [{"t": i * 180, "o": 30000, "h": 30010, "l": 29990, "c": 30000} for i in range(40)]
    flat[10]["h"] = 30012
    flat[25]["l"] = 29988
    check("ノイズ程度の揺れはアンカーにしない", gann.anchor(flat) is None,
          str(gann.anchor(flat)))
    check("足が少なければ None", gann.anchor(leg()[:8]) is None)
    check("空でも落ちない", gann.anchor([]) is None)


def test_angle_direction_follows_the_leg():
    up = gann.anchor(leg(down=False))
    down = gann.anchor(leg(down=True))
    check("上昇/下降が判定される", up and down and up["direction"] != down["direction"],
          f"{up and up['direction']} / {down and down['direction']}")
    check("上昇レッグの起点は安値", up and up["pivot"] == up["low"], str(up))
    check("下降レッグの起点は高値", down and down["pivot"] == down["high"], str(down))
    # R36: 上昇レッグは**安値起点で上向き**(支持)、下降レッグは**高値起点で
    # 下向き**(抵抗)。以前は起点も符号も逆で、上昇中の 1×1 が上値に出ていた。
    for anc, want_sign, label in ((up, 1, "上昇レッグは安値起点で上向き"),
                                  (down, -1, "下降レッグは高値起点で下向き")):
        if not anc:
            continue
        rows = gann.angle_levels(anc, anc["pivotIndex"] + 10)
        one = next((r for r in rows if r["primary"]), None)
        check(label, one is not None and (one["slopePerBar"] * want_sign) > 0,
              str(one))


def test_circles_are_multiples_of_the_price_unit():
    anc = gann.anchor(leg(down=True))
    rows = gann.circle_levels(anc)
    check("環の数だけ上下 2 本ずつ", len(rows) == len(gann.CIRCLE_RINGS) * 2, str(len(rows)))
    for row in rows:
        k = row["ring"]
        expected_up = anc["center"] + k * anc["priceUnit"]
        expected_dn = anc["center"] - k * anc["priceUnit"]
        check(f"円{k} が 中心 ± {k}×単位",
              abs(row["price"] - expected_up) < 0.01 or abs(row["price"] - expected_dn) < 0.01,
              str(row))


def test_scoring_affects_the_trade_but_never_gates_it():
    """R33: 利用者の明示的な指示で、裏付けが無くても採点へ反映する。

    影響のさせ方は HRLR / killzone と同じ「加点・減点」に留める。ハードゲート
    にも確認要素にもしない —— 確認要素にすると、裏付けの無いものが単独で
    武装根拠になってしまう。

    実測(実サイクル 403 本): NODE +1 が 14%、ROADBLOCK −1 が 41% 発火し、
    武装 101 → 99、A+ 68 → 62(6 本が A へ降格)。確かに効いている。
    """
    bars = leg(down=True)
    anc = gann.anchor(bars)
    one = next(r for r in gann.angle_levels(anc, len(bars) - 1) if r["primary"])
    matrix = {"models": {"gann": {"status": "COMPUTED", "anchor": anc,
                                  "angles": gann.angle_levels(anc, len(bars) - 1)}}}

    # 建値が 1×1 に乗っている → +1
    on = {"entry": one["price"], "targets": [one["price"] - 50],
          "score": 0, "evidence": [], "penalties": []}
    msnr_gate._score_gann(on, matrix)
    check("節目に乗れば加点", on["score"] == 1 and "GANN_1X1_NODE" in on["evidence"], str(on))

    # 経路(建値→TP1)を 1×1 が横切る → −1
    across = {"entry": one["price"] + 60, "targets": [one["price"] - 60],
              "score": 0, "evidence": [], "penalties": []}
    msnr_gate._score_gann(across, matrix)
    check("経路を横切れば減点",
          across["score"] == -1 and "GANN_1X1_ROADBLOCK" in across["penalties"], str(across))

    # 遠くて経路にも無い → 影響なし
    away = {"entry": one["price"] + 500, "targets": [one["price"] + 600],
            "score": 0, "evidence": [], "penalties": []}
    msnr_gate._score_gann(away, matrix)
    check("無関係なら動かさない", away["score"] == 0 and not away["penalties"], str(away))

    # 加点も減点も**ハードブロッカーにはしない**
    for cand in (on, across, away):
        check("ハードブロッカーを増やさない", not cand.get("hardBlockers"), str(cand))

    # Gann が無い/計算できないときは何もしない
    quiet = {"entry": 100.0, "targets": [110.0], "score": 0, "evidence": [], "penalties": []}
    msnr_gate._score_gann(quiet, {"models": {"gann": {"status": "MISSING"}}})
    msnr_gate._score_gann(quiet, None)
    check("Gann 未計算なら影響なし", quiet["score"] == 0 and not quiet["penalties"], str(quiet))


def test_observe_is_advisory_only():
    bars = leg(down=True)
    out = gann.observe(bars, bars[-1]["c"])
    check("計算できる", out["status"] == "COMPUTED", str(out.get("reason")))
    check("助言フラグが立つ", out.get("advisory") is True)
    check("アングルと円が揃う", out["angles"] and out["circles"])
    check("最寄り節目が出る", out.get("nearest") and "distanceUnits" in out["nearest"],
          str(out.get("nearest")))
    # 確認要素には**入れない**。ここが緩むと裏付けの無いものが武装根拠になる。
    for key in ("GANN_1X1_NODE", "GANN_1X1_ROADBLOCK", "GANN", "GANN_ALIGNED"):
        check(f"{key} は確認要素に入っていない", key not in msnr_gate.CONFIRMATION_EVIDENCE)
    missing = gann.observe([], None)
    check("足が無ければ MISSING", missing["status"] == "MISSING" and missing["advisory"] is True)


def test_wired_into_the_model_catalogue_without_voting():
    import strategy_models
    bars = leg(down=True)
    bundle = {"at": "2026-08-24T21:00:00+09:00", "price": bars[-1]["c"],
              "snapshot": {"bars3m": bars, "levels": []}}
    matrix = strategy_models.build_strategy_matrix(bundle, bars, [])
    models = matrix.get("models") or {}
    check("カタログに gann が載る", "gann" in models, str(sorted(models)))
    check("overlays にも載る", "gann" in (matrix.get("overlays") or {}))
    # 方向票に出ていないこと: alignment は他モデルだけで決まる
    alignment = matrix.get("alignment") or {}
    check("alignment は数値の辞書のまま", isinstance(alignment, dict), str(alignment))
    check("gann は activeModels に入らない",
          "gann" not in (matrix.get("activeModels") or []),
          str(matrix.get("activeModels")))


test_unit_comes_from_the_swing_not_the_screen()
test_insignificant_swings_are_rejected()
test_angle_direction_follows_the_leg()
test_circles_are_multiples_of_the_price_unit()
test_observe_is_advisory_only()
test_scoring_affects_the_trade_but_never_gates_it()
test_wired_into_the_model_catalogue_without_voting()

if FAILED:
    print(f"FAILED: {len(FAILED)} -> {', '.join(FAILED)}")
    raise SystemExit(1)
print("ALL PASS (test_r32_gann)")

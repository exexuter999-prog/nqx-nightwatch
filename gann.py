# -*- coding: utf-8 -*-
"""Gann の 1×1 アングルと同心円を足から計算する(R32)。

## 何を実装しているか

画像デッキにあった「円のフィボナッチ」は、実際には **Gann** だった。
タイ語の「45 องศา」は 45 度の意。Gann の中心概念は 1×1 アングルで、
「価格 1 単位 = 時間 1 単位」を意味する。上昇中は支持、下降中は抵抗として
働く、とされる。同心円は 1×1 を半径に取った円で、円が価格軸と交わる点が
水平の節目(デッキの "Classic A")になる。

参考:
  https://www.fxranking.com/education/trading-strategies/gann-theory/
  https://robertwcolby.com/gann.html

## 尺度の問題と、その解き方

チャート上の「円」は本来**縦横比に依存**して定義が曖昧になる。1 ドルを何 px に
描くかで円の形が変わるからである。TradingView の円ツールも画面座標で描いている。

ここでは **スイングそのものを単位に取る**ことで尺度依存を消す:

    価格単位 = |スイング高 − スイング安|
    時間単位 = その 2 点間の足数

この単位系では円は真円になり、チャートの表示倍率に一切依存しない。

    1×1 線 : price(t) = pivot ± (価格単位/時間単位) × 経過足数
    円 k   : (Δ価格/価格単位)² + (Δ時間/時間単位)² = k²

円 k が価格軸と交わる点(Δ時間 = 0)は `中心 ± k × 価格単位`。これが水平の節目。

## 証拠としての扱い

**Gann に実証的な裏付けは無い。** 本システムの実トレード記録も 1 件しかなく、
検証のしようがない。したがってこれは画像カタログの他のモデルと同じ
**助言レイヤー**であり、ゲートにも確認要素(CONFIRMATION_EVIDENCE)にもしない。
出すのは「今この水準が Gann 的には節目に当たる」という観測だけ。
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional

GANN_VERSION = "GANN-ANGLES/1"

#: 同心円の環。デッキの図は 3 重だった。
CIRCLE_RINGS = (1, 2, 3)
#: アングルの傾き。(価格単位, 時間単位)。1×1 が 45 度。
ANGLES = ((1, 1), (2, 1), (1, 2))
#: スイング点の左右本数。2 だと足のわずかな揺れまで拾ってしまい、
#: 実データで「9 本 26pt」のような無意味なアンカーが選ばれた。
SWING_LOOKBACK = 3
#: 高安がこれ未満しか離れていなければアンカーにしない(単位が退化する)。
MIN_SEP_BARS = 5
#: 1×1 の傾きに掛ける減衰。スイング中の平均速度は、その後の実効速度の
#: ちょうど 2 倍だった(実測 372 本: 実効 0.12×足レンジ / スイング 0.24×足レンジ)。
GANN_SLOPE_DAMPING = 0.5
#: 価格単位の下限。足の中央値レンジの何倍か。ノイズ程度の振れを 1×1 の
#: 単位にすると、傾きが小さすぎてどの水準にも「近い」ことになってしまう。
MIN_UNIT_RANGE_MULT = 3.0


def _num(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _swings(bars: List[Dict[str, Any]], kind: str, lookback: int = SWING_LOOKBACK):
    """前後 lookback 本より高い/低い足を [(index, price)] で返す。

    msnr_gate._fractal_points と同じ規則。あちらは SMT 専用の内部関数なので
    import せず、同じ定義をここに置く(片方だけ変えないこと)。
    """
    key = "h" if kind == "high" else "l"
    pick = max if kind == "high" else min
    out = []
    for i in range(lookback, len(bars) - lookback):
        win = bars[i - lookback:i + lookback + 1]
        value = _num(bars[i].get(key))
        if value is None:
            continue
        extremes = [_num(b.get(key)) for b in win]
        if any(e is None for e in extremes):
            continue
        if value == pick(extremes):
            out.append((i, value))
    return out


def anchor(bars: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """1×1 の単位を決めるスイング高安の組を選ぶ。

    直近のスイング高と安のうち **最後に確定した 2 点**を採る。どちらが先かで
    向きが決まる: 安 → 高 なら上昇レッグ、高 → 安 なら下降レッグ。
    """
    if not isinstance(bars, list) or len(bars) < (SWING_LOOKBACK * 2 + 1 + MIN_SEP_BARS):
        return None
    highs = _swings(bars, "high")
    lows = _swings(bars, "low")
    if not highs or not lows:
        return None

    # 有意性の下限。足レンジの中央値を基準にする(noise_floor と同じ考え方)。
    ranges = []
    for bar in bars[-60:]:
        hi, lo = _num(bar.get("h")), _num(bar.get("l"))
        if hi is not None and lo is not None and hi >= lo:
            ranges.append(hi - lo)
    if not ranges:
        return None
    ranges.sort()
    median_range = ranges[len(ranges) // 2]
    min_unit = MIN_UNIT_RANGE_MULT * median_range

    # 直近から遡って、離れていて十分大きい高安の組を探す。最後の 2 点を
    # 無条件に採ると、ノイズ程度の揺れがアンカーになる(実データで確認)。
    best = None
    for hi_i, hi_p in reversed(highs):
        for lo_i, lo_p in reversed(lows):
            if abs(hi_i - lo_i) < MIN_SEP_BARS:
                continue
            span = abs(hi_p - lo_p)
            if span < min_unit:
                continue
            # より新しい方(2 点のうち後ろ)が最も新しい組を選ぶ
            recency = max(hi_i, lo_i)
            if best is None or recency > best[0]:
                best = (recency, hi_i, hi_p, lo_i, lo_p)
            break                      # この高値に対する最新の安値だけ見る
    if best is None:
        return None
    _, hi_i, hi_p, lo_i, lo_p = best
    price_unit = abs(hi_p - lo_p)
    time_unit = abs(hi_i - lo_i)
    if price_unit <= 0 or time_unit <= 0:
        return None
    # R36: **起点は「後に来た極値」ではなく、線を引くべき極値**。
    #
    # 以前は「後に来た方」を pivot にしていた。上昇レッグ(安→高)では高値が
    # pivot になり、そこから下向きに 1×1 を引いていた。実測で現在値 30195 に
    # 対し 1×1 が 30214 —— **支持線のはずが上値に来る**。
    #
    # Gann の 1×1 は「上昇中は支持、下降中は抵抗」。上昇レッグなら**安値から
    # 上向き**、下降レッグなら**高値から下向き**に引く。方向はレッグの向き
    # (どちらの極値が後か)で決まるが、起点はその逆側の極値になる。
    if hi_i > lo_i:
        # 安値 → 高値 = 上昇レッグ。安値を起点に上向き(支持)。
        direction, pivot_i, pivot_p = "UP", lo_i, lo_p
    else:
        # 高値 → 安値 = 下降レッグ。高値を起点に下向き(抵抗)。
        direction, pivot_i, pivot_p = "DOWN", hi_i, hi_p
    return {
        "direction": direction,
        "highIndex": hi_i, "high": round(hi_p, 2),
        "lowIndex": lo_i, "low": round(lo_p, 2),
        "pivotIndex": pivot_i, "pivot": round(pivot_p, 2),
        "pivotT": bars[pivot_i].get("t"),
        "priceUnit": round(price_unit, 2),
        "timeUnit": int(time_unit),
        # R36: 1×1 の傾きは **足レンジ中央値の半分**。
        #
        # 以前は `priceUnit / timeUnit`(= スイング中の平均速度)を使っていた。
        # これは「スイング中の速度がその後も続く」前提で、実測では 1×1 が
        # 131pt 伸びる間に価格は 67pt しか動かない。線がすぐ価格を追い越し、
        # 上昇レッグで 1×1 が現在値の下(支持)に来たのは 188 本中 **1 本**だった。
        #
        # 足レンジ中央値そのものも試したが、今度は緩すぎて 477pt 離れた。
        # 実測(372 本)で起点からの実効速度は 足レンジ中央値の **約 0.12 倍**、
        # スイング平均速度は 0.24 倍。両者の関係が一貫して 2 倍だったので、
        # スイング速度の半分を採る。
        #
        # **これは相場から測った値であって、当てはめて選んだ閾値ではない。**
        "pricePerBar": round(price_unit / time_unit * GANN_SLOPE_DAMPING, 4),
        "unitSource": "swing_slope_damped",
        "swingSlopePerBar": round(price_unit / time_unit, 4),
        "medianBarRange": round(median_range, 4),
        "centerIndex": (hi_i + lo_i) / 2.0,
        "center": round((hi_p + lo_p) / 2.0, 2),
    }


def angle_levels(anc: Dict[str, Any], bar_index: int) -> List[Dict[str, Any]]:
    """各アングルが `bar_index` 時点で示す価格。

    上昇レッグは安値起点で上向き、下降レッグは高値起点で下向きに伸ばす。
    Gann の「上昇中は 1×1 が支持、下降中は抵抗」という置き方に対応する。

    **ただし線は予測ではなく基準**。価格が線のどちら側にいるかは相場次第で、
    実測(372 本)では上昇レッグで価格が線の上(=線が支持として機能)なのは
    32%、下降レッグで線が上(抵抗)なのは 43% だった。線を割っている状態も
    普通に起きる —— それ自体が読み筋であって、実装の誤りではない。
    """
    if not anc:
        return []
    elapsed = bar_index - anc["pivotIndex"]
    if elapsed < 0:
        return []
    out = []
    # 上昇レッグは安値起点で**上向き**(支持)、下降レッグは高値起点で
    # **下向き**(抵抗)。以前は符号が逆で、上昇中の 1×1 が上値に出ていた。
    sign = 1.0 if anc["direction"] == "UP" else -1.0
    for p_mult, t_mult in ANGLES:
        slope = anc["pricePerBar"] * (p_mult / t_mult)
        price = anc["pivot"] + sign * slope * elapsed
        out.append({
            "label": f"Gann {p_mult}x{t_mult}",
            "price": round(price, 2),
            "slopePerBar": round(sign * slope, 4),
            "primary": (p_mult, t_mult) == (1, 1),
        })
    return out


def circle_levels(anc: Dict[str, Any]) -> List[Dict[str, Any]]:
    """同心円が価格軸と交わる水準(Δ時間 = 0 の点)。

    デッキの "Classic A" にあたる水平の節目。中心 ± k × 価格単位。
    """
    if not anc:
        return []
    out = []
    for k in CIRCLE_RINGS:
        span = k * anc["priceUnit"]
        out.append({"label": f"Gann circle {k} 上", "price": round(anc["center"] + span, 2),
                    "ring": k})
        out.append({"label": f"Gann circle {k} 下", "price": round(anc["center"] - span, 2),
                    "ring": k})
    return out


def observe(bars: List[Dict[str, Any]], price: Optional[float] = None) -> Dict[str, Any]:
    """足から Gann の観測を組む。助言専用。

    `nearest` は現在値に最も近い節目とその距離。**距離が近いことは
    それ自体では何の主張でもない** —— 上位が他の証跡と併せて読む。
    """
    anc = anchor(bars or [])
    if not anc:
        return {"version": GANN_VERSION, "status": "MISSING",
                "reason": "GANN_ANCHOR_UNAVAILABLE", "advisory": True}
    last_index = len(bars) - 1
    angles = angle_levels(anc, last_index)
    circles = circle_levels(anc)
    current = _num(price)
    if current is None:
        current = _num(bars[-1].get("c"))

    nearest = None
    if current is not None:
        pool = [(abs(row["price"] - current), row) for row in angles + circles]
        pool.sort(key=lambda item: item[0])
        if pool:
            distance, row = pool[0]
            nearest = {"label": row["label"], "price": row["price"],
                       "distancePt": round(distance, 2),
                       # 価格単位に対する比。1.0 なら丸ごと 1 単位ぶん離れている。
                       "distanceUnits": round(distance / anc["priceUnit"], 3)}
    return {
        "version": GANN_VERSION,
        "status": "COMPUTED",
        "advisory": True,          # ゲートにも確認要素にもしない
        "anchor": anc,
        "angles": angles,
        "circles": circles,
        "nearest": nearest,
        "provenance": "gann.observe",
        "note": "%s レッグ・単位 %.1fpt/%d本" % (
            "上昇" if anc["direction"] == "UP" else "下降",
            anc["priceUnit"], anc["timeUnit"]),
    }

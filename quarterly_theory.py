# -*- coding: utf-8 -*-
"""Quarterly Theory を**足の時刻から**決定論で計算する(R31)。

なぜ作ったか:
  `strategy_models.detect_quarterly_theory` は `bundle["quarterly"]` を
  受け取るだけの素通し実装で、供給元がどこにも無かった。実サイクル 403 本で
  取得率 **0%**。Quarterly Theory は時間の分割そのものなので、指標も外部入力も
  要らない —— 足の時刻だけで完全に決まる。

## 分割(ニューヨーク時間)

取引日は **18:00 → 翌 18:00**。true open は NY 深夜 00:00。

  日を 4 つの 6 時間セッションへ:
    Q1 Asia      18:00–00:00   Accumulation
    Q2 London    00:00–06:00   Manipulation(true open / Judas)
    Q3 New York  06:00–12:00   Distribution
    Q4 PM        12:00–18:00   eXpansion(継続 or 反転)

  各セッションを 4 つの 90 分クォーターへ、さらに 22.5 分のマイクロへ。
  同じ格子が週・月・年へも伸びる(ここでは日・セッション・90分の 3 段まで)。

## AMDX

各サイクルは Accumulation → Manipulation → Distribution → eXpansion の順で
展開すると**想定**する。X が来た時点で「X→A→M→D が揃った」と数える。

**これは観測であって予測ではない。** どの位相に居るかは時刻で確定するが、
その位相が想定どおり振る舞ったかは価格を見て別に判定する
(`phaseBehaviour`)。時刻だけで方向を主張しない。

参考:
  https://www.luxalgo.com/library/concept/quarterly-theory/
  https://oracleinsights.io/library/quarterly-theory-framework
"""
from __future__ import annotations

import math
import statistics
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")

QT_VERSION = "QUARTERLY-THEORY/1"

#: 取引日の 4 セッション。(名前, 開始時, 位相)。18:00 起点。
DAY_QUARTERS = (
    ("Asia", 18, "A"),
    ("London", 0, "M"),
    ("NewYork", 6, "D"),
    ("PM", 12, "X"),
)
SESSION_MINUTES = 6 * 60
QUARTER_MINUTES = 90            # 6h ÷ 4
MICRO_MINUTES = 22.5            # 90m ÷ 4
PHASES = ("A", "M", "D", "X")
PHASE_JP = {"A": "蓄積", "M": "誘導", "D": "分配", "X": "拡張"}


def _ny(epoch: float) -> datetime:
    return datetime.fromtimestamp(float(epoch), NY)


def trading_day_open(moment: datetime) -> datetime:
    """その時刻が属する取引日の開始(NY 18:00)。"""
    base = moment.replace(hour=18, minute=0, second=0, microsecond=0)
    if moment.hour < 18:
        base -= timedelta(days=1)
    return base


def quarters(epoch: float) -> Optional[Dict[str, Any]]:
    """ある時刻の日/セッション/90分クォーターの位相を返す。

    返すのは**時刻で確定する事実だけ**。方向は含めない。
    """
    try:
        moment = _ny(epoch)
    except (TypeError, ValueError, OSError, OverflowError):
        return None
    day_open = trading_day_open(moment)
    elapsed = (moment - day_open).total_seconds() / 60.0
    if not (0 <= elapsed < 24 * 60):
        return None

    index = int(elapsed // SESSION_MINUTES)          # 0..3
    index = min(index, 3)
    name, _, phase = DAY_QUARTERS[index]
    session_start = day_open + timedelta(minutes=index * SESSION_MINUTES)
    into_session = (moment - session_start).total_seconds() / 60.0

    q_index = min(int(into_session // QUARTER_MINUTES), 3)
    q_start = session_start + timedelta(minutes=q_index * QUARTER_MINUTES)
    into_quarter = (moment - q_start).total_seconds() / 60.0
    m_index = min(int(into_quarter // MICRO_MINUTES), 3)

    return {
        "dayOpen": day_open.isoformat(),
        "session": name,
        "sessionPhase": phase,                       # 日レベルの位相
        "sessionIndex": index,
        "sessionStart": session_start.isoformat(),
        "sessionEnd": (session_start + timedelta(minutes=SESSION_MINUTES)).isoformat(),
        "quarterPhase": PHASES[q_index],             # 90 分レベルの位相
        "quarterIndex": q_index,
        "quarterStart": q_start.isoformat(),
        "quarterEnd": (q_start + timedelta(minutes=QUARTER_MINUTES)).isoformat(),
        "microPhase": PHASES[m_index],               # 22.5 分レベル
        "microIndex": m_index,
        "trueOpenAt": (day_open + timedelta(hours=6)).isoformat(),   # NY 00:00
    }


#: R49: Judas 判定の最小超過幅の下限(tick)。実際の下限は当日3分レンジの
#: 中央値との max —— 普通の足のヒゲ1本を「掃引」と呼ばないため。
JUDAS_MIN_EXCURSION = 0.25


def judas_from_true_open(bars: List[Dict[str, Any]], day_open: datetime,
                         epoch: float) -> Dict[str, Any]:
    """true open(NY 00:00)に対する誘導→奪還(Judas)の観測(R49)。

    Quarterly Theory の「時刻は位相を決めるが方向は決めない」原則のまま、
    方向だけを**価格から**導く。ルール(形容詞なし):

      minExc     = max(当日3分足レンジの中央値, 1 tick)
                   —— 最初の足のヒゲが必ず open を跨ぐため、1 tick では
                   全日が「掃引」になる。普通の足1本分を超えて初めて誘導と呼ぶ
      swept_up   = London 窓(00:00–06:00)の高値 > trueOpen + minExc
      swept_down = 同窓の安値 < trueOpen − minExc
      direction  = swept_up かつ 直近確定足 close < trueOpen − 1tick → SELL
                   swept_down かつ 直近確定足 close > trueOpen + 1tick → BUY
                   それ以外 None(観測を主張しない)

    「上へ誘導して true open を奪還した日は下へ分配する」という Q2/Judas の
    教義を、そのまま数値にしたもの。両側を掃引した日は bothSides=True を残す
    (seek & destroy の可能性)。excursion の幅は将来のスコアカード分離用に保存。
    """
    to_price = _true_open_price(bars, day_open)
    true_open_at = day_open + timedelta(hours=6)
    start = true_open_at.timestamp()
    end = min(float(epoch), (true_open_at + timedelta(hours=6)).timestamp())
    if to_price is None:
        return {"available": False, "reason": "TRUE_OPEN_UNAVAILABLE"}
    if float(epoch) < start + 180:
        return {"available": False, "reason": "LONDON_NOT_STARTED",
                "trueOpen": round(to_price, 2)}
    hi: Optional[float] = None
    lo: Optional[float] = None
    last_close: Optional[float] = None
    last_t: Optional[int] = None
    for bar in bars:
        try:
            t = int(bar["t"])
        except (KeyError, TypeError, ValueError):
            continue
        if start <= t < end:
            try:
                h, l = float(bar["h"]), float(bar["l"])
            except (KeyError, TypeError, ValueError):
                continue
            if math.isfinite(h) and math.isfinite(l):
                hi = h if hi is None else max(hi, h)
                lo = l if lo is None else min(lo, l)
        try:
            c = float(bar["c"])
        except (KeyError, TypeError, ValueError):
            continue
        if math.isfinite(c) and (last_t is None or t > last_t):
            last_t, last_close = t, c
    if hi is None or lo is None or last_close is None:
        return {"available": False, "reason": "LONDON_BARS_MISSING",
                "trueOpen": round(to_price, 2)}
    ranges = []
    for bar in bars:
        try:
            span = float(bar["h"]) - float(bar["l"])
        except (KeyError, TypeError, ValueError):
            continue
        if math.isfinite(span) and span >= 0:
            ranges.append(span)
    med_range = statistics.median(ranges) if ranges else 0.0
    min_exc = max(med_range if math.isfinite(med_range) else 0.0,
                  JUDAS_MIN_EXCURSION)
    swept_up = hi > to_price + min_exc
    swept_down = lo < to_price - min_exc
    direction = None
    if swept_up and last_close < to_price - JUDAS_MIN_EXCURSION:
        direction = "SELL"
    elif swept_down and last_close > to_price + JUDAS_MIN_EXCURSION:
        direction = "BUY"
    return {
        "available": True,
        "direction": direction,
        "basis": "TRUE_OPEN_RECLAIM",
        "trueOpen": round(to_price, 2),
        "minExcursionPt": round(min_exc, 2),
        "sweptUpPt": round(hi - to_price, 2) if swept_up else 0.0,
        "sweptDownPt": round(to_price - lo, 2) if swept_down else 0.0,
        "bothSides": bool(swept_up and swept_down),
        "lastClose": round(last_close, 2),
    }


def _true_open_price(bars: List[Dict[str, Any]], day_open: datetime) -> Optional[float]:
    """true open(NY 00:00)の価格。その足が窓に無ければ None。"""
    target = int((day_open + timedelta(hours=6)).timestamp())
    best = None
    for bar in bars:
        try:
            t = int(bar["t"])
        except (KeyError, TypeError, ValueError):
            continue
        if t < target:
            continue
        if best is None or t < best[0]:
            best = (t, bar)
    if best is None or best[0] - target > 3600:      # 1 時間以上ずれたら使わない
        return None
    try:
        return float(best[1]["o"])
    except (KeyError, TypeError, ValueError):
        return None


def _phase_behaviour(bars: List[Dict[str, Any]], start_iso: str, end_iso: str,
                     price: Optional[float]) -> Dict[str, Any]:
    """その位相が実際どう振る舞ったかを価格で見る。時刻だけで方向を決めない。"""
    try:
        start = datetime.fromisoformat(start_iso).timestamp()
        end = datetime.fromisoformat(end_iso).timestamp()
    except (TypeError, ValueError):
        return {"bars": 0, "high": None, "low": None, "range": None, "position": None}
    rows = []
    for bar in bars:
        if not isinstance(bar, dict):
            continue
        try:
            t = int(bar["t"])
            hi_v, lo_v = float(bar["h"]), float(bar["l"])
        except (KeyError, TypeError, ValueError):
            continue
        # 高安が読めない足はここで落とす。以前は t だけ見て通していたため、
        # キー欠損の足で下の max/min が KeyError で落ちた。
        if not (math.isfinite(hi_v) and math.isfinite(lo_v)):
            continue
        if start <= t < end:
            rows.append({"h": hi_v, "l": lo_v})
    if not rows:
        return {"bars": 0, "high": None, "low": None, "range": None, "position": None}
    hi = max(b["h"] for b in rows)
    lo = min(b["l"] for b in rows)
    span = hi - lo
    pos = None
    if price is not None and span > 0:
        try:
            value = float(price)
        except (TypeError, ValueError):
            value = None
        if value is not None and math.isfinite(value):
            pos = round((value - lo) / span, 3)
    return {"bars": len(rows), "high": round(hi, 2), "low": round(lo, 2),
            "range": round(span, 2), "position": pos}


def observe(bundle: Dict[str, Any], bars: List[Dict[str, Any]],
            price: Optional[float] = None) -> Dict[str, Any]:
    """バンドルから Quarterly Theory の観測を組む。

    `strategy_models.detect_quarterly_theory` がそのまま食える形で返す
    (`sequence` / `stage` / `asOf` / `sessionId` / `freshness`)。

    `bias` は**付けない**。時刻は位相を決めるが方向は決めない —— 方向を
    ここで主張すると、価格を見ずに ±1 されることになる。方向は
    `phaseBehaviour` を見て上位が判断する。
    """
    at = None
    for source in (bundle, bundle.get("snapshot") if isinstance(bundle.get("snapshot"), dict) else {}):
        if isinstance(source, dict) and source.get("at"):
            at = source["at"]
            break
    epoch = None
    if at:
        try:
            epoch = datetime.fromisoformat(str(at).replace("Z", "+00:00")).timestamp()
        except (TypeError, ValueError):
            epoch = None
    if epoch is None and bars:
        try:
            epoch = int(bars[-1]["t"]) + 180
        except (KeyError, TypeError, ValueError):
            epoch = None
    if epoch is None:
        return {"status": "MISSING", "reason": "QT_TIME_UNAVAILABLE"}

    q = quarters(epoch)
    if not q:
        return {"status": "MISSING", "reason": "QT_TIME_OUT_OF_RANGE"}

    day_open = datetime.fromisoformat(q["dayOpen"])
    # X→A→M→D の並び。stage は **90 分クォーター**の位相を使う
    # (発注の判断に効く粒度がここだから)。
    order = list(PHASES)
    idx = order.index(q["quarterPhase"])
    rotated = order[idx + 1:] + order[:idx + 1]      # 直近 4 位相が末尾に D で終わる並び
    sequence = ["X", "A", "M", "D"] if q["quarterPhase"] == "D" else rotated

    # R49: 方向は時刻からではなく、true open に対する誘導→奪還(Judas)の
    # **価格観測**からだけ導く。観測が成立しない日は bias を付けない。
    judas = judas_from_true_open(bars, day_open, epoch)

    out = {
        "version": QT_VERSION,
        "status": "COMPUTED",
        "source": "computed(time)",
        "stage": q["quarterPhase"],
        "sequence": sequence,
        "quarter": q["quarterIndex"] + 1,
        "session": q["session"],
        "sessionPhase": q["sessionPhase"],
        "micro": q["microPhase"],
        "asOf": datetime.fromtimestamp(epoch, NY).isoformat(),
        "sequenceAt": q["quarterStart"],
        "sessionId": "QT-%s-%s" % (day_open.strftime("%Y%m%d"), q["session"]),
        "freshness": "FRESH",
        "provenance": "quarterly_theory.observe",
        "trueOpen": _true_open_price(bars, day_open),
        "trueOpenAt": q["trueOpenAt"],
        "judas": judas,
        "window": {"quarterStart": q["quarterStart"], "quarterEnd": q["quarterEnd"],
                   "sessionStart": q["sessionStart"], "sessionEnd": q["sessionEnd"]},
        "phaseBehaviour": _phase_behaviour(bars, q["quarterStart"], q["quarterEnd"], price),
        "note": "%s / %s Q%d (%s)" % (q["session"], q["quarterPhase"],
                                      q["quarterIndex"] + 1, PHASE_JP[q["quarterPhase"]]),
    }
    if judas.get("direction"):
        out["bias"] = judas["direction"]
        out["biasBasis"] = "TRUE_OPEN_RECLAIM"
    return out

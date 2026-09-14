#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Deterministic higher-timeframe context for the Nightwatch 3m engine.

The execution model remains 3-minute.  This module only turns *confirmed*
45m/1h/4h/1D OHLC into compact structural context.  Missing or stale frames
stay explicit and never get reconstructed from rolling 3-minute candles.
"""

from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


FRAME_STEPS = {"45m": 2700, "1h": 3600, "4h": 14400, "1d": 86400}
#: 再取得は「raw 最終行の close + この猶予」。閉じた直後は TradingView が足を
#: 確定させるまで少し待つ。3 分サイクルなので、閉じた次のサイクルで取れる。
REFRESH_GRACE_SEC = 60
#: 確定足の close がこれより古ければ STALE。2×step は「次の足が閉じるまで」、
#: +REFRESH_SLACK_SEC は 3 分サイクル 1 周 + 猶予 + 取得時間の遅れを吸収する。
#: 余裕が無いと 4h は毎日 11:00〜11:03 JST に 1 サイクルだけ STALE になり、
#: HTF の票が一瞬消えて候補の等級が揺れる(2026-09-08 の修正で判明)。
REFRESH_SLACK_SEC = 600
FRAME_MAX_AGE = {name: 2 * step + REFRESH_SLACK_SEC for name, step in FRAME_STEPS.items()}
#: raw 最終行の close を過ぎて取り直したのに同じ行が最終行のまま(セッション
#: 最後の長い足・休場・週末)なら、この間隔で再試行する。毎サイクル pane を
#: 切り替えに行かない。
REFRESH_BACKOFF_SEC = {name: min(step, 3600) for name, step in FRAME_STEPS.items()}
FRAME_WEIGHTS = {"45m": 1, "1h": 2, "4h": 3, "1d": 3}
FRAME_FILES = {"45m": "bars45m.json", "1h": "bars1h.json",
               "4h": "bars4h.json", "1d": "bars1d.json"}
MIN_BARS = 20


def _number(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _epoch(value: Any) -> Optional[float]:
    number = _number(value)
    if number is not None:
        return number / 1000.0 if number > 10_000_000_000 else number
    if not value:
        return None
    try:
        raw = str(value).strip().replace("Z", "+00:00")
        parsed = datetime.fromisoformat(raw)
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(timezone.utc).timestamp()
    except (TypeError, ValueError, OverflowError):
        return None


def normalize_bars(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, float]]:
    out: List[Dict[str, float]] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        t = _epoch(row.get("t", row.get("time")))
        o = _number(row.get("o", row.get("open")))
        h = _number(row.get("h", row.get("high")))
        l = _number(row.get("l", row.get("low")))
        c = _number(row.get("c", row.get("close")))
        if None in (t, o, h, l, c) or h < max(o, c) or l > min(o, c):
            continue
        out.append({"t": t, "o": o, "h": h, "l": l, "c": c})
    return sorted(out, key=lambda bar: bar["t"])


def _ema_series(values: List[float], length: int = 20) -> List[float]:
    if not values:
        return []
    alpha = 2.0 / (length + 1.0)
    out = [values[0]]
    for value in values[1:]:
        out.append(alpha * value + (1.0 - alpha) * out[-1])
    return out


def _atr(bars: List[Dict[str, float]], length: int = 14) -> Optional[float]:
    if len(bars) < 2:
        return None
    rows = []
    for index in range(max(1, len(bars) - length), len(bars)):
        bar, previous = bars[index], bars[index - 1]
        rows.append(max(bar["h"] - bar["l"], abs(bar["h"] - previous["c"]),
                        abs(bar["l"] - previous["c"])))
    return sum(rows) / len(rows) if rows else None


def _pivots(bars: List[Dict[str, float]], radius: int = 2) -> tuple[List[float], List[float]]:
    highs: List[float] = []
    lows: List[float] = []
    for index in range(radius, len(bars) - radius):
        window = bars[index - radius:index + radius + 1]
        if bars[index]["h"] == max(row["h"] for row in window):
            highs.append(bars[index]["h"])
        if bars[index]["l"] == min(row["l"] for row in window):
            lows.append(bars[index]["l"])
    return highs, lows


def observe_frame(timeframe: str, rows: Iterable[Dict[str, Any]],
                  now: Any = None) -> Dict[str, Any]:
    step = FRAME_STEPS[timeframe]
    bars = normalize_bars(rows)
    base = {"timeframe": timeframe, "stepSec": step, "barCount": len(bars),
            "status": "INSUFFICIENT", "valid": False, "direction": None,
            "structure": "UNKNOWN", "freshness": "MISSING",
            "provenance": "confirmed_htf_ohlc"}
    if not bars:
        return base
    clock = _epoch(now)
    if clock is None:
        clock = datetime.now(timezone.utc).timestamp()
    last = bars[-1]
    age = clock - (last["t"] + step)
    freshness = ("FUTURE" if age < -60 else
                 "FRESH" if age <= FRAME_MAX_AGE[timeframe] else "STALE")
    base.update({"asOf": datetime.fromtimestamp(last["t"] + step, timezone.utc).isoformat(),
                 "ageSec": round(age, 1), "freshness": freshness,
                 "close": round(last["c"], 2)})
    if len(bars) < MIN_BARS:
        return base

    closes = [bar["c"] for bar in bars]
    ema = _ema_series(closes, 20)
    slope = ema[-1] - ema[-6] if len(ema) >= 6 else 0.0
    atr = _atr(bars)
    highs, lows = _pivots(bars)
    structure = "MIXED"
    if len(highs) >= 2 and len(lows) >= 2:
        if highs[-1] > highs[-2] and lows[-1] > lows[-2]:
            structure = "UP"
        elif highs[-1] < highs[-2] and lows[-1] < lows[-2]:
            structure = "DOWN"

    if structure == "UP":
        direction, basis = "BUY", "HH_HL"
    elif structure == "DOWN":
        direction, basis = "SELL", "LH_LL"
    elif last["c"] > ema[-1] and slope > 0:
        direction, basis = "BUY", "EMA20_SLOPE"
    elif last["c"] < ema[-1] and slope < 0:
        direction, basis = "SELL", "EMA20_SLOPE"
    else:
        direction, basis = None, "MIXED"

    sample = bars[-20:]
    base.update({
        "status": "CONFIRMED" if freshness == "FRESH" else freshness,
        "valid": freshness == "FRESH",
        "direction": direction,
        "structure": structure,
        "basis": basis,
        "ema20": round(ema[-1], 2),
        "emaSlope5": round(slope, 2),
        "atr14": round(atr, 2) if atr is not None else None,
        "range20High": round(max(bar["h"] for bar in sample), 2),
        "range20Low": round(min(bar["l"] for bar in sample), 2),
    })
    return base


def build_context(frame_rows: Dict[str, Iterable[Dict[str, Any]]],
                  now: Any = None) -> Dict[str, Any]:
    frames = {name: observe_frame(name, frame_rows.get(name) or [], now)
              for name in FRAME_STEPS}
    usable = [row for row in frames.values() if row.get("valid") and row.get("direction")]
    buy_score = sum(FRAME_WEIGHTS[row["timeframe"]] for row in usable
                    if row["direction"] == "BUY")
    sell_score = sum(FRAME_WEIGHTS[row["timeframe"]] for row in usable
                     if row["direction"] == "SELL")
    buy_count = sum(row["direction"] == "BUY" for row in usable)
    sell_count = sum(row["direction"] == "SELL" for row in usable)
    if buy_count >= 2 and buy_score > sell_score:
        bias = "BUY"
    elif sell_count >= 2 and sell_score > buy_score:
        bias = "SELL"
    else:
        bias = None
    return {
        "schema": "NQX_HTF_CONTEXT/1",
        "status": "ALIGNED" if bias else "MIXED" if len(usable) >= 2 else "INSUFFICIENT",
        "valid": len(usable) >= 2,
        "bias": bias,
        "buyScore": buy_score,
        "sellScore": sell_score,
        "complete": all(row.get("valid") for row in frames.values()),
        "frames": frames,
        "provenance": "confirmed_htf_ohlc",
    }


def refresh_state(timeframe: str, rows: Iterable[Dict[str, Any]], now: Any = None,
                  written_at: Any = None) -> Dict[str, Any]:
    """raw を取り直すべきかを、その根拠(時刻)ごと返す。

    ``tv_fetch`` は TradingView の応答を逐語で保存するので、raw の最終行は取得
    時点で**形成中**だった足である(2026-09-08 実測: 09:00 取得の bars4h.json の
    最終行は 07:00 始まりの足で、close は 09:00 時点の途中値 29,583。実際の
    close は 29,704 付近)。したがって

    * 最終行の close 時刻(``t + step``)+ 猶予を過ぎたら due。閉じた足の最終値と
      次の形成中足を取りに行く。旧規則「``t + 2*step``」は最終行を確定足と
      誤認していて、毎本 1 本分遅れ、その間は途中値が確定足として採点されて
      いた(1d は丸 1 日)。
    * ファイルがその close 後に書かれている(取り直したのに同じ行が最終行)
      なら、その足は step より長い(4h の 23:00 JST 足は 06:00 まで続く)か
      市場が閉じている。``REFRESH_BACKOFF_SEC`` ごとに再試行する。

    ``written_at`` は raw ファイルの mtime(``due_frames`` が渡す)。無ければ
    close + 猶予だけで判定する。
    """
    step = FRAME_STEPS[timeframe]
    bars = normalize_bars(rows)
    clock = _epoch(now)
    if clock is None:
        clock = datetime.now(timezone.utc).timestamp()
    state: Dict[str, Any] = {"timeframe": timeframe, "barCount": len(bars),
                             "writtenAt": None, "lastOpen": None, "settleAt": None,
                             "nextDueAt": None, "due": True, "reason": "NO_BARS"}
    if not bars:
        return state
    written = _epoch(written_at)
    last_open = bars[-1]["t"]
    settle_at = last_open + step + REFRESH_GRACE_SEC
    if written is not None and written >= settle_at:
        next_due = written + REFRESH_BACKOFF_SEC[timeframe]
        reason = "TRAILING_BAR_STILL_OPEN_AFTER_SETTLE"
    else:
        next_due = settle_at
        reason = "TRAILING_BAR_CAPTURED_WHILE_FORMING"
    state.update({
        "writtenAt": None if written is None else round(written, 3),
        "lastOpen": last_open,
        "settleAt": settle_at,
        "nextDueAt": next_due,
        "due": clock >= next_due,
        "reason": reason,
    })
    return state


def refresh_due(timeframe: str, rows: Iterable[Dict[str, Any]], now: Any = None,
                written_at: Any = None) -> bool:
    """``refresh_state`` の真偽値だけ。既存呼び出し互換。"""
    return bool(refresh_state(timeframe, rows, now, written_at)["due"])


def due_frames(raw_dir: str | Path, now: Any = None) -> List[str]:
    return [name for name, state in due_states(raw_dir, now).items() if state["due"]]


def due_states(raw_dir: str | Path, now: Any = None) -> Dict[str, Dict[str, Any]]:
    root = Path(raw_dir)
    states: Dict[str, Dict[str, Any]] = {}
    for timeframe, filename in FRAME_FILES.items():
        path = root / filename
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
            written_at = path.stat().st_mtime
        except (OSError, json.JSONDecodeError):
            payload, written_at = {}, None
        rows = payload.get("bars") if isinstance(payload, dict) else []
        states[timeframe] = refresh_state(timeframe, rows or [], now, written_at)
    return states


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="List HTF frames due for TradingView refresh")
    parser.add_argument("--raw-dir", default=".secrets/tv_raw")
    parser.add_argument("--now", default=None, help="optional ISO-8601/epoch test clock")
    args = parser.parse_args(argv)
    states = due_states(args.raw_dir, args.now)
    print(json.dumps({"schema": "NQX_HTF_REFRESH/1",
                      "due": [name for name, state in states.items() if state["due"]],
                      "frames": states}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

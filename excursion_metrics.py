#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""決済トレードの「刈られ方」の計測(R103-0)。

**純関数だけ。** ネットワーク・台帳・発注・ファイル入出力に触れない。確定 3 分足と
スコアカード 1 行(建値・SL・決済・時刻)から、次を出す。

  maePt / mfePt / maeR / mfeR / holdMin   保有中(確定 3 分足の高安)
  beyondStopPt        決済後 15 分に SL をどこまで抜けたか(SL 到達でなければ None)
  tp1AfterExitMin     決済後 180 分以内に TP1 へ届くまでの分(届かなければ None)
  fav3hPt             決済後 3 時間の最大順行 pt
  huntClass           STOP_HUNT / WRONG_WAY / DEEP / WIN / FLAT(判定できなければ None)

しきい値(全部この定数。docstring と定数以外の場所に数字を書かない):

  ``BAR_SEC = 180``                 確定足の長さ(秒)
  ``STOP_WINDOW_MIN = 15``          SL 抜け幅を見る決済後の窓(分)
  ``TP1_WINDOW_MIN = 180``          決済後の TP1 到達を見る窓(分)
  ``FAV_WINDOW_MIN = 180``          決済後の順行(fav3hPt)を見る窓(分)
  ``STOP_SLIP_TOL_PT = 3.0``        「SL に到達した」と見なす決済価格の許容差(pt)
  ``HUNT_NOISE_N = 1.0``            STOP_HUNT の条件: beyondStopPt ≤ この倍率 × noise
  ``HUNT_FAV_SL_MULT = 2.0``        同: fav3hPt ≥ この倍率 × SL 幅
  ``WRONG_WAY_FAV_SL_MULT = 1.0``   WRONG_WAY: fav3hPt < この倍率 × SL 幅
  ``FLAT_PT = 2.0``                 建値付近(model_scorecard.COST_FLOOR_PT と同値)

損切りの分類(この順に判定する):

  STOP_HUNT  tp1AfterExitMin が非 None、または
             (beyondStopPt ≤ HUNT_NOISE_N × noise かつ fav3hPt ≥ HUNT_FAV_SL_MULT × SL 幅)
  WRONG_WAY  fav3hPt < WRONG_WAY_FAV_SL_MULT × SL 幅
  DEEP       それ以外(SL を大きく抜けたが、その後戻ってきた)

3 分足の中の順序は見えないので、決済後の到達時刻は**到達した足の開始時刻**で測る
(最短側。1 分足が入るまでこの値は「最も早くてこの分数」の意味)。数字が揃わない項目は
None のまま返す —— 推測で埋めない。

``excursions()`` は R57 のトレード日誌が使っていた計算(``obsidian_metrics.excursions``)を
そのままここへ移したもの。obsidian_metrics はこれを呼ぶだけで、出力は変えていない。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

BAR_SEC = 180
POINT_VALUE = 2.0
#: R57 の日誌が見る決済後の窓(分)。excursions() 専用。
POST_EXIT_MIN = 60

STOP_WINDOW_MIN = 15
TP1_WINDOW_MIN = 180
FAV_WINDOW_MIN = 180
STOP_SLIP_TOL_PT = 3.0
HUNT_NOISE_N = 1.0
HUNT_FAV_SL_MULT = 2.0
WRONG_WAY_FAV_SL_MULT = 1.0
FLAT_PT = 2.0

HUNT_CLASSES = ("STOP_HUNT", "WRONG_WAY", "DEEP", "WIN", "FLAT")


def _f(value: Any) -> Optional[float]:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out else None


def _r(value: Optional[float], digits: int = 2) -> Optional[float]:
    return None if value is None else round(value, digits)


def _at(value: Any) -> Optional[datetime]:
    """ISO 文字列 / datetime / epoch 秒 → tz 付き datetime。読めなければ None。"""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    number = _f(value) if not isinstance(value, str) else None
    if number is not None:
        return datetime.fromtimestamp(number, tz=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def normalize_bars(bars: Any) -> List[Dict[str, float]]:
    """``{t,h,l,c}`` の列へ揃える。``[t,o,h,l,c]``(result.chart の形)も受ける。

    壊れた足・高安が逆の足は捨てる。時刻順に並べ替える。
    """
    rows: List[Dict[str, float]] = []
    for bar in bars if isinstance(bars, (list, tuple)) else []:
        if isinstance(bar, dict):
            t = _f(bar.get("t", bar.get("time")))
            high = _f(bar.get("h", bar.get("high")))
            low = _f(bar.get("l", bar.get("low")))
            close = _f(bar.get("c", bar.get("close")))
        elif isinstance(bar, (list, tuple)) and len(bar) >= 5:
            t, high, low, close = _f(bar[0]), _f(bar[2]), _f(bar[3]), _f(bar[4])
        else:
            continue
        if None in (t, high, low, close) or high < low:
            continue
        rows.append({"t": t, "h": high, "l": low, "c": close})
    rows.sort(key=lambda row: row["t"])
    return rows


def _window(bars: List[Dict[str, float]], start_ts: float, minutes: float) -> List[Dict[str, float]]:
    """``start_ts`` から ``minutes`` 分に**掛かる**確定足(決済足を含む)。"""
    end_ts = start_ts + minutes * 60.0
    return [b for b in bars if b["t"] + BAR_SEC > start_ts and b["t"] < end_ts]


def _stop_reached(side: str, exit_price: Optional[float], stop: Optional[float]) -> bool:
    """決済価格が SL(不利側 ``STOP_SLIP_TOL_PT`` 以内)なら SL 到達と見なす。"""
    if exit_price is None or stop is None:
        return False
    adverse = exit_price >= stop - 0.5 if side == "SHORT" else exit_price <= stop + 0.5
    return adverse and abs(exit_price - stop) <= STOP_SLIP_TOL_PT


# ---------------------------------------------------------------- R103: 刈られ方

def classify(trade: Dict[str, Any], bars: Any,
             tp1: Any = None, noise: Any = None) -> Dict[str, Any]:
    """スコアカード 1 行 + 確定 3 分足 → 刈られ方の計測(上の docstring の 9 項目)。

    ``trade`` は ``.secrets/model_scorecard.jsonl`` の 1 行と同じ形
    (side LONG/SHORT・entry・stop・exit・openedAt・closedAt・outcome)。
    ``tp1`` は凍結プランの第 1 目標、``noise`` は武装時の noise floor(pt)。
    足が無い・時刻が読めないなど、揃わない項目は None。
    """
    empty = {"maePt": None, "mfePt": None, "maeR": None, "mfeR": None, "holdMin": None,
             "beyondStopPt": None, "tp1AfterExitMin": None, "fav3hPt": None,
             "huntClass": None}
    if not isinstance(trade, dict):
        return dict(empty)
    side = str(trade.get("side") or "").upper()
    entry, exit_price = _f(trade.get("entry")), _f(trade.get("exit"))
    stop = _f(trade.get("stop"))
    tp1 = _f(tp1)
    noise = _f(noise)
    opened_at = _at(trade.get("openedAt") or trade.get("entryAt"))
    closed_at = _at(trade.get("closedAt"))
    rows = normalize_bars(bars)
    if side not in ("LONG", "SHORT") or entry is None or closed_at is None or not rows:
        return dict(empty)

    out = dict(empty)
    close_ts = closed_at.timestamp()
    risk_pt = abs(entry - stop) if stop is not None else None

    if opened_at is not None:
        open_ts = opened_at.timestamp()
        held = [b for b in rows if b["t"] + BAR_SEC > open_ts and b["t"] < close_ts]
        if held:
            hi, lo = max(b["h"] for b in held), min(b["l"] for b in held)
            if side == "SHORT":
                mae_pt, mfe_pt = max(0.0, hi - entry), max(0.0, entry - lo)
            else:
                mae_pt, mfe_pt = max(0.0, entry - lo), max(0.0, hi - entry)
            out["maePt"], out["mfePt"] = _r(mae_pt), _r(mfe_pt)
            if risk_pt:
                out["maeR"], out["mfeR"] = _r(mae_pt / risk_pt), _r(mfe_pt / risk_pt)
        out["holdMin"] = _r((close_ts - open_ts) / 60.0, 1)

    # 決済後 15 分の SL 抜け幅。SL に到達していない決済(TP・裁量)では測らない。
    if _stop_reached(side, exit_price, stop):
        post = _window(rows, close_ts, STOP_WINDOW_MIN)
        if post:
            extreme = (max(b["h"] for b in post) if side == "SHORT"
                       else min(b["l"] for b in post))
            beyond = (extreme - stop) if side == "SHORT" else (stop - extreme)
            out["beyondStopPt"] = _r(max(0.0, beyond))

    # 決済後 180 分の TP1 到達(到達足の開始時刻 = 最短側)。
    if tp1 is not None:
        for bar in _window(rows, close_ts, TP1_WINDOW_MIN):
            hit = bar["l"] <= tp1 if side == "SHORT" else bar["h"] >= tp1
            if hit:
                out["tp1AfterExitMin"] = int(round(max(0.0, (bar["t"] - close_ts) / 60.0)))
                break

    # 決済後 3 時間の最大順行(決済価格から見た有利側)。
    reference = exit_price if exit_price is not None else entry
    fav_window = _window(rows, close_ts, FAV_WINDOW_MIN)
    if fav_window:
        extreme = (min(b["l"] for b in fav_window) if side == "SHORT"
                   else max(b["h"] for b in fav_window))
        fav = (reference - extreme) if side == "SHORT" else (extreme - reference)
        out["fav3hPt"] = _r(max(0.0, fav))

    out["huntClass"] = hunt_class(trade, out, risk_pt=risk_pt, noise=noise)
    return out


def hunt_class(trade: Dict[str, Any], metrics: Dict[str, Any], *,
               risk_pt: Optional[float], noise: Optional[float]) -> Optional[str]:
    """勝敗と決済後の値動きから huntClass を決める。判定できなければ None。"""
    entry, exit_price = _f(trade.get("entry")), _f(trade.get("exit"))
    outcome = str(trade.get("outcome") or "").upper()
    if outcome not in ("WIN", "LOSS", "FLAT"):
        if entry is None or exit_price is None:
            return None
        side = str(trade.get("side") or "").upper()
        move = (exit_price - entry) * (1.0 if side == "LONG" else -1.0)
        outcome = "FLAT" if abs(move) < FLAT_PT else ("WIN" if move > 0 else "LOSS")
    if outcome == "WIN":
        return "WIN"
    if outcome == "FLAT":
        return "FLAT"

    beyond = _f(metrics.get("beyondStopPt"))
    fav = _f(metrics.get("fav3hPt"))
    if metrics.get("tp1AfterExitMin") is not None:
        return "STOP_HUNT"
    if fav is None or not risk_pt:
        return None
    if beyond is not None and noise is not None:
        if beyond <= HUNT_NOISE_N * noise and fav >= HUNT_FAV_SL_MULT * risk_pt:
            return "STOP_HUNT"
    if fav < WRONG_WAY_FAV_SL_MULT * risk_pt:
        return "WRONG_WAY"
    return "DEEP"


# ---------------------------------------------------------------- R57: 日誌の計測(移設)

def excursions(trade: Dict[str, Any], bars: List[Dict[str, float]],
               entry_at: Optional[datetime], closed_at: datetime) -> Dict[str, Any]:
    """MAE / MFE / 効率 / 保有時間 / 決済後 60 分。足が無ければ空 dict。

    R57 から ``obsidian_metrics.excursions`` にあった実装をそのまま移した(出力は同一)。
    キー名も vault 側の表示に合わせた snake_case のまま。R103 の計測は ``classify()``。
    """
    side = str(trade.get("side") or "").upper()
    entry, exit_price = _f(trade.get("entry")), _f(trade.get("exit"))
    stop, tp1 = _f(trade.get("stop")), _f(trade.get("tp1"))
    qty = int(trade.get("qty") or 0)
    pnl = _f(trade.get("pnlNet"))
    if side not in ("SHORT", "LONG") or entry is None or not bars:
        return {}
    close_ts = closed_at.timestamp()
    entry_ts = entry_at.timestamp() if entry_at else None
    # 建玉時刻が不明(手動建玉など)なら保有区間が分からないので MAE/MFE は出さない(推測しない)。
    held = ([b for b in bars if b["t"] + BAR_SEC > entry_ts and b["t"] < close_ts]
            if entry_ts is not None else [])
    out: Dict[str, Any] = {}
    if held:
        hi, lo = max(b["h"] for b in held), min(b["l"] for b in held)
        if side == "SHORT":
            mae_pt, mfe_pt = max(0.0, hi - entry), max(0.0, entry - lo)
        else:
            mae_pt, mfe_pt = max(0.0, entry - lo), max(0.0, hi - entry)
        risk_pt = abs(entry - stop) if stop is not None else None
        mfe_usd = mfe_pt * POINT_VALUE * qty
        out.update({
            "mae_pt": _r(mae_pt), "mfe_pt": _r(mfe_pt),
            "mae_usd": _r(mae_pt * POINT_VALUE * qty, 0), "mfe_usd": _r(mfe_usd, 0),
            "mae_r": _r(mae_pt / risk_pt, 2) if risk_pt else None,
            "mfe_r": _r(mfe_pt / risk_pt, 2) if risk_pt else None,
            "efficiency": _r(pnl / mfe_usd, 2) if (pnl is not None and mfe_usd > 0) else None,
            "bars_held": len(held),
        })
    if entry_ts is not None:
        out["hold_min"] = _r((close_ts - entry_ts) / 60.0, 1)
    post = [b for b in bars if close_ts <= b["t"] < close_ts + POST_EXIT_MIN * 60]
    if post and exit_price is not None:
        hi, lo = max(b["h"] for b in post), min(b["l"] for b in post)
        if side == "SHORT":
            fav, adv = max(0.0, exit_price - lo), max(0.0, hi - exit_price)
        else:
            fav, adv = max(0.0, hi - exit_price), max(0.0, exit_price - lo)
        out["post60_fav_pt"], out["post60_adv_pt"] = _r(fav), _r(adv)
        if tp1 is not None:
            reached = None
            for b in post:
                hit = (b["l"] <= tp1) if side == "SHORT" else (b["h"] >= tp1)
                if hit:
                    reached = _r((b["t"] + BAR_SEC - close_ts) / 60.0, 0)
                    break
            out["tp1_after_exit_min"] = reached
    return out

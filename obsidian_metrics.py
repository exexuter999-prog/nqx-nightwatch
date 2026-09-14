#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""トレード日誌の自動計測(R57)。

スコアカードの 1 行(建値・決済・SL・枚数)と確定 3 分足から、振り返りに要る数字を出す。

* 保有中の MAE / MFE(pt・USD・R)、効率(損益 ÷ MFE)、保有時間
* 決済の種類(SL / TP1 / TP2 / 分割・裁量)と SL の滑り
* 決済後 60 分の値動き(有利側・不利側の最大)と、決済後に TP1 へ届いたまでの分数
  —— 「損切りの直後に目標へ行った」を数える
* 損切り直後の再エントリー(直前の損切りからの分数、連敗数)
* 時間帯(JST → セッション区分)
* 武装した周期の文脈(減点・計画 R・ボラ比・HTF 構造・VP 水準との位置)

すべて読み取り専用。数字が揃わない項目は None のまま(推測しない)。
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

BAR_SEC = 180
POINT_VALUE = 2.0
POST_EXIT_MIN = 60
REENTRY_WINDOW_MIN = 6 * 60
LEVEL_KEYS = ("C: VAH", "C: VAL", "C: POC", "P: VAH", "P: VAL", "P: POC")


def _f(value: Any) -> Optional[float]:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out else None


def _r(value: Optional[float], digits: int = 2) -> Optional[float]:
    return None if value is None else round(value, digits)


# ---------------------------------------------------------------- 保有中・決済後の値動き

def excursions(trade: Dict[str, Any], bars: List[Dict[str, float]],
               entry_at: Optional[datetime], closed_at: datetime) -> Dict[str, Any]:
    """MAE / MFE / 効率 / 保有時間 / 決済後 60 分。足が無ければ空 dict。"""
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


# ---------------------------------------------------------------- 決済の種類

def exit_kind(trade: Dict[str, Any]) -> Tuple[str, Optional[float]]:
    """(種類, SL の滑り pt)。SL は不利側で 3pt 以内、TP は 0.5pt 以内、分割の加重平均は PARTIAL。"""
    side = str(trade.get("side") or "").upper()
    entry, exit_price = _f(trade.get("entry")), _f(trade.get("exit"))
    stop, tp1, final = _f(trade.get("stop")), _f(trade.get("tp1")), _f(trade.get("finalTarget"))
    if exit_price is None or entry is None:
        return "UNKNOWN", None
    adverse = (lambda p: p >= stop - 0.5) if side == "SHORT" else (lambda p: p <= stop + 0.5)
    if stop is not None and adverse(exit_price) and abs(exit_price - stop) <= 3.0:
        slip = (exit_price - stop) if side == "SHORT" else (stop - exit_price)
        return "SL", _r(max(0.0, slip))
    if tp1 is not None and abs(exit_price - tp1) <= 0.5:
        return "TP1", None
    if final is not None and abs(exit_price - final) <= 0.5:
        return "TP2", None
    if tp1 is not None:
        between = (tp1 < exit_price < entry) if side == "SHORT" else (entry < exit_price < tp1)
        beyond = (exit_price < tp1) if side == "SHORT" else (exit_price > tp1)
        if between or beyond:
            return "PARTIAL", None
    return "MANUAL", None


# ---------------------------------------------------------------- 時間帯

def session_bucket(at: Optional[datetime]) -> Optional[str]:
    """JST の時刻からセッション区分(ET の killzone に対応)。"""
    if at is None:
        return None
    minutes = at.hour * 60 + at.minute
    table = [(7 * 60, "アジア/欧州前"), (15 * 60, "ロンドン"), (21 * 60 + 30, "NY AM"),
             (24 * 60, "NY 昼"), (2 * 60 + 30 + 24 * 60, "NY PM"), (5 * 60 + 24 * 60, "引け後")]
    shifted = minutes if minutes >= 7 * 60 else minutes + 24 * 60
    label = "引け後"
    for start, name in table:
        if shifted >= start:
            label = name
    return label


# ---------------------------------------------------------------- 連敗・再エントリー

def sequence_context(trades: List[Dict[str, Any]]) -> None:
    """各トレードに `loss_streak_before` と `reentry_after_loss_min` を書き込む(in place)。

    並びは決済時刻。建玉時刻(entryAt)が無いトレードは決済時刻で代用する。
    """
    ordered = sorted(trades, key=lambda t: t["closedAt"])
    for i, trade in enumerate(ordered):
        opened = trade.get("entryAt") or trade["closedAt"]
        prior = [t for t in ordered[:i] if t["closedAt"] <= opened]
        streak = 0
        for t in reversed(prior):
            if t.get("outcome") == "LOSS":
                streak += 1
            else:
                break
        trade["loss_streak_before"] = streak
        last_loss = next((t for t in reversed(prior) if t.get("outcome") == "LOSS"), None)
        gap = None
        if last_loss is not None:
            delta = (opened - last_loss["closedAt"]).total_seconds() / 60.0
            if 0 <= delta <= REENTRY_WINDOW_MIN:
                gap = _r(delta, 0)
        trade["reentry_after_loss_min"] = gap


# ---------------------------------------------------------------- 武装した周期の文脈

def _parse_at(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def entry_context(entry_at: Optional[datetime], trade: Dict[str, Any],
                  bundles: List[Tuple[datetime, str]]) -> Dict[str, Any]:
    """建玉直前(10 分以内)の最後の監査バンドルから、減点・計画 R・ボラ比・HTF・水準との位置を拾う。"""
    if entry_at is None:
        return {}
    candidates = [p for at, p in bundles if timedelta(0) <= entry_at - at <= timedelta(minutes=10)]
    if not candidates:
        return {}
    try:
        with open(candidates[-1], encoding="utf-8") as fh:
            bundle = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    ev = bundle.get("evaluation") or {}
    decision = ev.get("decision") or {}
    vol = ev.get("volGate") or {}
    htf = ((bundle.get("snapshot") or {}).get("htfContext") or {}).get("frames") or {}
    out: Dict[str, Any] = {
        "penalties": [str(x) for x in (decision.get("penalties") or [])][:8],
        "planned_r_tp1": _f((decision.get("targetR") or [None])[0]) if decision.get("targetR") else None,
        "planned_r_tp2": _f((decision.get("targetR") or [None, None])[-1]) if len(decision.get("targetR") or []) > 1 else None,
        "vol_ratio": _f(vol.get("ratio")),
        "noise_floor": _f(vol.get("noise")),
        "session_gate": (ev.get("sessionGate") or {}).get("label"),
        "htf": {k: (v or {}).get("structure") for k, v in htf.items() if isinstance(v, dict)},
        "context_bundle": os.path.basename(candidates[-1]),
    }
    entry = _f(trade.get("entry"))
    levels = [l for l in ((bundle.get("snapshot") or {}).get("levels") or []) if isinstance(l, dict)]
    rel = []
    if entry is not None:
        for key in LEVEL_KEYS:
            price = next((_f(l.get("price")) for l in levels if l.get("label") == key), None)
            if price is not None:
                rel.append(f"{key.replace(': ', ':')} {entry - price:+.1f}")
    out["levels_rel"] = " · ".join(rel) if rel else None
    return out


# ---------------------------------------------------------------- セッション統計

def session_stats(trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    ordered = sorted(trades, key=lambda t: t["closedAt"])
    pnls = [_f(t.get("pnlNet")) or 0.0 for t in ordered]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_win, gross_loss = sum(wins), -sum(losses)
    cum, peak, max_dd = 0.0, 0.0, 0.0
    curve = []
    for t, p in zip(ordered, pnls):
        cum += p
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)
        curve.append((t["closedAt"], cum))
    rs = [_f(t.get("rMultiple")) for t in ordered if _f(t.get("rMultiple")) is not None]
    decided = len(wins) + len(losses)
    by_model: Dict[str, Dict[str, Any]] = {}
    for t, p in zip(ordered, pnls):
        bucket = by_model.setdefault(str(t.get("model") or "UNATTRIBUTED"), {"n": 0, "net": 0.0, "wins": 0, "losses": 0})
        bucket["n"] += 1
        bucket["net"] += p
        bucket["wins"] += 1 if p > 0 else 0
        bucket["losses"] += 1 if p < 0 else 0
    return {
        "n": len(ordered), "wins": len(wins), "losses": len(losses), "flats": len(ordered) - decided,
        "win_rate": _r(len(wins) / decided, 3) if decided else None,
        "net": _r(sum(pnls), 0), "gross_win": _r(gross_win, 0), "gross_loss": _r(gross_loss, 0),
        "pf": _r(gross_win / gross_loss, 2) if gross_loss > 0 else None,
        "expectancy": _r(sum(pnls) / len(ordered), 0) if ordered else None,
        "avg_win": _r(gross_win / len(wins), 0) if wins else None,
        "avg_loss": _r(gross_loss / len(losses), 0) if losses else None,
        "avg_r": _r(sum(rs) / len(rs), 2) if rs else None,
        "max_dd": _r(max_dd, 0), "best": _r(max(pnls), 0) if pnls else None, "worst": _r(min(pnls), 0) if pnls else None,
        "reentries": sum(1 for t in ordered if (t.get("reentry_after_loss_min") or 10 ** 9) <= 30),
        "curve": curve, "by_model": by_model,
    }


def equity_svg(curve: List[Tuple[datetime, float]], width: int = 720, height: int = 220) -> str:
    """決済ごとの累積損益(ステップ線)。"""
    bg, grid, text, up, down = "#131722", "#1f2a3a", "#d1d4dc", "#26a69a", "#ef5350"
    left, right, top, bottom = 48, 16, 20, 28
    if not curve:
        return f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="80"><rect width="100%" height="100%" fill="{bg}"/><text x="12" y="45" fill="{text}" font-size="12">no closed trades</text></svg>'
    values = [0.0] + [v for _, v in curve]
    lo, hi = min(values), max(values)
    pad = max((hi - lo) * 0.1, 50.0)
    lo, hi = lo - pad, hi + pad
    n = len(curve)
    xs = [left + (width - left - right) * (i / n) for i in range(n + 1)]

    def y_of(v: float) -> float:
        return top + (hi - v) / (hi - lo) * (height - top - bottom)

    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" font-family="Segoe UI, Meiryo, sans-serif">',
           f'<rect width="100%" height="100%" fill="{bg}"/>']
    for tick in (lo + pad, 0.0, hi - pad):
        y = y_of(tick)
        out.append(f'<line x1="{left}" y1="{y:.1f}" x2="{width - right}" y2="{y:.1f}" stroke="{grid}"/>')
        out.append(f'<text x="4" y="{y + 4:.1f}" fill="#787b86" font-size="10">{tick:+,.0f}</text>')
    zero_y = y_of(0.0)
    out.append(f'<line x1="{left}" y1="{zero_y:.1f}" x2="{width - right}" y2="{zero_y:.1f}" stroke="#787b86" stroke-dasharray="3,3"/>')
    path = f"M {xs[0]:.1f} {y_of(0.0):.1f}"
    prev = 0.0
    for i, (at, v) in enumerate(curve):
        path += f" H {xs[i + 1]:.1f} V {y_of(v):.1f}"
        color = up if v >= prev else down
        out.append(f'<circle cx="{xs[i + 1]:.1f}" cy="{y_of(v):.1f}" r="3.5" fill="{color}"/>')
        out.append(f'<text x="{xs[i + 1]:.1f}" y="{height - 10}" fill="#787b86" font-size="10" text-anchor="middle">{at.strftime("%H:%M")}</text>')
        prev = v
    final_color = up if curve[-1][1] >= 0 else down
    out.append(f'<path d="{path}" fill="none" stroke="{final_color}" stroke-width="2"/>')
    out.append(f'<text x="{left}" y="14" fill="{text}" font-size="12">累積損益(決済ごと) 最終 {curve[-1][1]:+,.0f} USD</text>')
    out.append("</svg>")
    return "\n".join(out)

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""トレードのチャート復元(R57)。依存なしの SVG 描画。

自前の取得データ(`.secrets/tv_raw/bars3m.json` = 直近 240 本の確定 3 分足、無ければ
監査バンドル `monitor_cycle_HHMM.json` の `snapshot.bars3m`)から、トレード前後の 3 分足に
建値 / SL / TP1 / 最終 TP / 決済 と VP 水準(C: / P: の VAH・VAL・POC)を重ねた SVG を作る。
TradingView の画面そのものではなく、**システムが見ていた足と水準からの復元**。
Obsidian は `![[attachments/trades/xxx.svg]]` でそのまま表示する。

    python obsidian_charts.py --demo   # 直近バンドルで描画テスト(stdout に SVG)
"""
from __future__ import annotations

import glob
import json
import math
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

BASE = os.path.dirname(os.path.abspath(__file__))
RAW_BARS = os.path.join(BASE, ".secrets", "tv_raw", "bars3m.json")
AUDIT_GLOB = os.path.join(BASE, ".secrets", "monitor_cycle_*.json")
JST = timezone(timedelta(hours=9))
BAR_SEC = 180
BEFORE_MIN = 45     # 建玉前に見せる分数
AFTER_MIN = 15      # 決済後に見せる分数
FALLBACK_BEFORE_MIN = 90   # 建玉時刻が不明なとき
LEVEL_KEYS = ("C: VAH", "C: VAL", "C: POC", "P: VAH", "P: VAL", "P: POC")

# 色(TradingView の暗色テーマに寄せる)
BG, GRID, TEXT, MUTED = "#131722", "#1f2a3a", "#d1d4dc", "#787b86"
UP, DOWN = "#26a69a", "#ef5350"
ENTRY_C, SL_C, TP_C, EXIT_C = "#5c7cfa", "#ef5350", "#26a69a", "#ffd166"
LEVEL_C = {"C": "#f0a500", "P": "#b088f9"}


# ---------------------------------------------------------------- 足の読み込み

def _norm_bar(row: Any) -> Optional[Dict[str, float]]:
    if not isinstance(row, dict):
        return None
    t = row.get("time") if row.get("time") is not None else row.get("t")
    o = row.get("open") if row.get("open") is not None else row.get("o")
    h = row.get("high") if row.get("high") is not None else row.get("h")
    lo = row.get("low") if row.get("low") is not None else row.get("l")
    c = row.get("close") if row.get("close") is not None else row.get("c")
    try:
        return {"t": float(t), "o": float(o), "h": float(h), "l": float(lo), "c": float(c),
                "v": float(row.get("volume") if row.get("volume") is not None else row.get("v") or 0)}
    except (TypeError, ValueError):
        return None


def _parse_at(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def load_raw_bars(path: str = RAW_BARS) -> List[Dict[str, float]]:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return []
    rows = data.get("bars") if isinstance(data, dict) else data
    bars = [b for b in (_norm_bar(r) for r in (rows or [])) if b]
    return sorted(bars, key=lambda b: b["t"])


def audit_bundles(pattern: str = AUDIT_GLOB) -> List[Tuple[datetime, str]]:
    """監査バンドルを `at` 付きで列挙(HHMM 名は日をまたいで衝突するので at で選ぶ)。"""
    out = []
    for path in glob.glob(pattern):
        try:
            with open(path, encoding="utf-8") as fh:
                head = fh.read(400)
        except OSError:
            continue
        # 先頭に at がある想定。無ければ全体を読む。
        at = None
        marker = head.find('"at"')
        if marker >= 0:
            frag = head[marker:marker + 80]
            quote = frag.find(":")
            if quote >= 0:
                value = frag[quote + 1:].strip().strip(",").strip().strip('"')
                at = _parse_at(value.split('"')[0])
        if at is None:
            try:
                with open(path, encoding="utf-8") as fh:
                    at = _parse_at(json.load(fh).get("at"))
            except (OSError, json.JSONDecodeError, AttributeError):
                at = None
        if at is not None:
            out.append((at, path))
    return sorted(out)


def _load_bundle(path: str) -> Dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}


def pick_bundle(closed_at: datetime, bundles: List[Tuple[datetime, str]]) -> Optional[str]:
    """決済の 3〜40 分後に取得された最初のバンドル(決済足が確定している)。無ければ直前 40 分の最後。"""
    after = [p for at, p in bundles if timedelta(minutes=3) <= at - closed_at <= timedelta(minutes=40)]
    if after:
        return after[0]
    before = [p for at, p in bundles if timedelta(0) <= closed_at - at <= timedelta(minutes=40)]
    return before[-1] if before else None


def bars_and_levels(entry_at: Optional[datetime], closed_at: datetime,
                    raw_path: str = RAW_BARS, audit_pattern: str = AUDIT_GLOB,
                    bundles: Optional[List[Tuple[datetime, str]]] = None,
                    after_min: int = AFTER_MIN
                    ) -> Tuple[List[Dict[str, float]], List[Dict[str, Any]], str]:
    """描画窓の足と、その時点の水準を返す。出所 `raw` / `audit:<file>` / `none`。

    `bundles` は `audit_bundles()` の結果を使い回すためのキャッシュ(923 ファイルの glob を
    トレードごとに繰り返さない)。`after_min` は決済後に含める分数(計測は 60 分欲しい)。
    """
    start = (entry_at - timedelta(minutes=BEFORE_MIN)) if entry_at else (closed_at - timedelta(minutes=FALLBACK_BEFORE_MIN))
    end = closed_at + timedelta(minutes=after_min)
    lo, hi = start.timestamp(), end.timestamp()
    bundles = audit_bundles(audit_pattern) if bundles is None else bundles
    picked = pick_bundle(closed_at, bundles)
    levels: List[Dict[str, Any]] = []
    if picked:
        bundle = _load_bundle(picked)
        levels = [l for l in ((bundle.get("snapshot") or {}).get("levels") or []) if isinstance(l, dict)]
    raw = [b for b in load_raw_bars(raw_path) if lo <= b["t"] <= hi]
    # raw が窓の大半(建玉前〜決済)を覆っていればそれを使う
    if raw and raw[0]["t"] <= lo + 2 * BAR_SEC and raw[-1]["t"] >= closed_at.timestamp() - 2 * BAR_SEC:
        return raw, levels, "raw"
    if picked:
        bundle_bars = [b for b in (_norm_bar(r) for r in ((_load_bundle(picked).get("snapshot") or {}).get("bars3m") or [])) if b]
        window = [b for b in bundle_bars if lo <= b["t"] <= hi]
        if window:
            return sorted(window, key=lambda b: b["t"]), levels, "audit:" + os.path.basename(picked)
    if raw:
        return raw, levels, "raw-partial"
    return [], levels, "none"


# ---------------------------------------------------------------- SVG

def _nice_step(span: float) -> float:
    for step in (1, 2.5, 5, 10, 25, 50, 100, 250, 500):
        if span / step <= 8:
            return step
    return 1000


def _fmt(value: Optional[float]) -> str:
    if value is None:
        return "—"
    return f"{value:,.2f}".rstrip("0").rstrip(".")


def _esc(text: Any) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def render_svg(trade: Dict[str, Any], bars: List[Dict[str, float]], levels: List[Dict[str, Any]],
               entry_at: Optional[datetime], closed_at: datetime, source: str,
               width: int = 960, height: int = 480) -> str:
    left, right, top, bottom = 12, 78, 46, 34
    plot_w, plot_h = width - left - right, height - top - bottom
    entry, exit_price = trade.get("entry"), trade.get("exit")
    stop, tp1, final = trade.get("stop"), trade.get("tp1"), trade.get("finalTarget")

    if not bars:
        return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="120" viewBox="0 0 {width} 120">'
                f'<rect width="100%" height="100%" fill="{BG}"/>'
                f'<text x="16" y="60" fill="{MUTED}" font-family="monospace" font-size="14">'
                f'chart unavailable: no confirmed 3m bars around {_esc(closed_at.strftime("%Y-%m-%d %H:%M"))} JST</text></svg>')

    t0, t1 = bars[0]["t"], bars[-1]["t"] + BAR_SEC
    prices = [b["h"] for b in bars] + [b["l"] for b in bars]
    for v in (entry, exit_price, stop, tp1):
        if v is not None:
            prices.append(float(v))
    p_lo, p_hi = min(prices), max(prices)
    bar_span = max(b["h"] for b in bars) - min(b["l"] for b in bars)
    if final is not None and abs(float(final) - (p_lo + p_hi) / 2) <= max(bar_span * 1.5, 30):
        prices.append(float(final))
        p_lo, p_hi = min(prices), max(prices)
    pad = max((p_hi - p_lo) * 0.08, 2.0)
    p_lo, p_hi = p_lo - pad, p_hi + pad

    def x_of(t: float) -> float:
        return left + (t - t0) / (t1 - t0) * plot_w

    def y_of(p: float) -> float:
        return top + (p_hi - p) / (p_hi - p_lo) * plot_h

    out: List[str] = []
    out.append(f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
               f'viewBox="0 0 {width} {height}" font-family="Segoe UI, Meiryo, sans-serif">')
    out.append(f'<rect width="100%" height="100%" fill="{BG}"/>')

    # 価格グリッド
    step = _nice_step(p_hi - p_lo)
    tick = math.ceil(p_lo / step) * step
    while tick <= p_hi:
        y = y_of(tick)
        out.append(f'<line x1="{left}" y1="{y:.1f}" x2="{left + plot_w}" y2="{y:.1f}" stroke="{GRID}" stroke-width="1"/>')
        out.append(f'<text x="{left + plot_w + 6}" y="{y + 4:.1f}" fill="{MUTED}" font-size="11">{_fmt(tick)}</text>')
        tick += step

    # 時間グリッド(15 分ごと)
    first_label = math.ceil(t0 / 900) * 900
    t = first_label
    while t <= t1:
        x = x_of(t)
        out.append(f'<line x1="{x:.1f}" y1="{top}" x2="{x:.1f}" y2="{top + plot_h}" stroke="{GRID}" stroke-width="1"/>')
        label = datetime.fromtimestamp(t, JST).strftime("%H:%M")
        out.append(f'<text x="{x:.1f}" y="{top + plot_h + 16}" fill="{MUTED}" font-size="11" text-anchor="middle">{label}</text>')
        t += 900

    # 水準(C: / P:)
    for level in levels:
        label = str(level.get("label") or "")
        price = level.get("price")
        if label not in LEVEL_KEYS or price is None:
            continue
        try:
            price = float(price)
        except (TypeError, ValueError):
            continue
        if not (p_lo <= price <= p_hi):
            continue
        color = LEVEL_C.get(label[0], MUTED)
        y = y_of(price)
        out.append(f'<line x1="{left}" y1="{y:.1f}" x2="{left + plot_w}" y2="{y:.1f}" stroke="{color}" '
                   f'stroke-width="1" stroke-dasharray="2,4" opacity="0.8"/>')
        out.append(f'<text x="{left + 4}" y="{y - 3:.1f}" fill="{color}" font-size="10" opacity="0.9">{_esc(label)} {_fmt(price)}</text>')

    # ローソク
    n = len(bars)
    cw = max(2.0, plot_w / max(n, 1) * 0.66)
    for b in bars:
        xc = x_of(b["t"] + BAR_SEC / 2)
        color = UP if b["c"] >= b["o"] else DOWN
        out.append(f'<line x1="{xc:.1f}" y1="{y_of(b["h"]):.1f}" x2="{xc:.1f}" y2="{y_of(b["l"]):.1f}" stroke="{color}" stroke-width="1"/>')
        y_top, y_bot = y_of(max(b["o"], b["c"])), y_of(min(b["o"], b["c"]))
        h = max(1.0, y_bot - y_top)
        out.append(f'<rect x="{xc - cw / 2:.1f}" y="{y_top:.1f}" width="{cw:.1f}" height="{h:.1f}" fill="{color}"/>')

    # 建玉〜決済の区間
    x_entry = x_of(entry_at.timestamp()) if entry_at else left
    x_exit = x_of(closed_at.timestamp())
    x_entry = min(max(x_entry, left), left + plot_w)
    x_exit = min(max(x_exit, left), left + plot_w)
    out.append(f'<rect x="{min(x_entry, x_exit):.1f}" y="{top}" width="{abs(x_exit - x_entry):.1f}" height="{plot_h}" '
               f'fill="{ENTRY_C}" opacity="0.06"/>')

    def hline(price: Optional[float], color: str, label: str, dash: str = "6,4") -> None:
        if price is None:
            return
        price = float(price)
        if not (p_lo <= price <= p_hi):
            return
        y = y_of(price)
        out.append(f'<line x1="{x_entry:.1f}" y1="{y:.1f}" x2="{x_exit:.1f}" y2="{y:.1f}" stroke="{color}" '
                   f'stroke-width="1.5" stroke-dasharray="{dash}"/>')
        out.append(f'<text x="{x_exit + 4:.1f}" y="{y + 4:.1f}" fill="{color}" font-size="11">{_esc(label)} {_fmt(price)}</text>')

    hline(stop, SL_C, "SL")
    hline(tp1, TP_C, "TP1")
    hline(final, TP_C, "TP2", "2,3")
    hline(entry, ENTRY_C, "ENTRY", "1,0")
    if final is not None and not (p_lo <= float(final) <= p_hi):
        arrow_y = top + plot_h - 6 if float(final) < p_lo else top + 12
        out.append(f'<text x="{x_exit + 4:.1f}" y="{arrow_y:.1f}" fill="{TP_C}" font-size="11">TP2 {_fmt(float(final))} (枠外)</text>')

    # 建玉・決済の縦線とマーカー
    if entry_at:
        out.append(f'<line x1="{x_entry:.1f}" y1="{top}" x2="{x_entry:.1f}" y2="{top + plot_h}" stroke="{ENTRY_C}" stroke-width="1" stroke-dasharray="3,3"/>')
        out.append(f'<text x="{x_entry + 3:.1f}" y="{top + 12}" fill="{ENTRY_C}" font-size="11">IN {entry_at.strftime("%H:%M")}</text>')
    out.append(f'<line x1="{x_exit:.1f}" y1="{top}" x2="{x_exit:.1f}" y2="{top + plot_h}" stroke="{EXIT_C}" stroke-width="1" stroke-dasharray="3,3"/>')
    out.append(f'<text x="{x_exit + 3:.1f}" y="{top + 26}" fill="{EXIT_C}" font-size="11">OUT {closed_at.strftime("%H:%M")}</text>')
    if exit_price is not None and p_lo <= float(exit_price) <= p_hi:
        out.append(f'<circle cx="{x_exit:.1f}" cy="{y_of(float(exit_price)):.1f}" r="4" fill="{EXIT_C}"/>')

    # タイトル
    pnl = trade.get("pnlNet")
    pnl_text = f"{pnl:+,.0f} USD" if pnl is not None else "P&L n/a"
    r = trade.get("rMultiple")
    r_text = f" ({r:+.2f}R)" if r is not None else ""
    head = (f"{trade.get('date')} {entry_at.strftime('%H:%M') if entry_at else '—'}→{closed_at.strftime('%H:%M')} JST "
            f"{trade.get('side')} {trade.get('qty')} · {trade.get('model')} {trade.get('grade') or ''} · {pnl_text}{r_text}")
    sub = (f"MNQU6 3m · 建値 {_fmt(entry)} · 決済 {_fmt(exit_price)} · SL {_fmt(stop)} · TP1 {_fmt(tp1)} · TP2 {_fmt(final)}"
           f" · 出所 {source}")
    out.append(f'<text x="{left}" y="20" fill="{TEXT}" font-size="14" font-weight="600">{_esc(head)}</text>')
    out.append(f'<text x="{left}" y="37" fill="{MUTED}" font-size="11">{_esc(sub)}</text>')
    out.append("</svg>")
    return "\n".join(out)


def render_trade_chart(trade: Dict[str, Any], entry_at: Optional[datetime], closed_at: datetime,
                       out_path: str, raw_path: str = RAW_BARS, audit_pattern: str = AUDIT_GLOB,
                       force: bool = False, bundles: Optional[List[Tuple[datetime, str]]] = None,
                       loaded: Optional[Tuple[List[Dict[str, float]], List[Dict[str, Any]], str]] = None
                       ) -> Optional[str]:
    """SVG を書き、出所を返す。既存ファイルは force でなければ触らない(None を返す)。

    `loaded` に `bars_and_levels()` の結果を渡せば読み直さない(計測と共有)。描画窓は
    決済後 AFTER_MIN 分までに切る。
    """
    if os.path.exists(out_path) and not force:
        return None
    bars, levels, source = loaded if loaded else bars_and_levels(
        entry_at, closed_at, raw_path=raw_path, audit_pattern=audit_pattern, bundles=bundles)
    cutoff = closed_at.timestamp() + AFTER_MIN * 60
    bars = [b for b in bars if b["t"] <= cutoff]
    svg = render_svg(trade, bars, levels, entry_at, closed_at, source)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(svg)
    return source


if __name__ == "__main__":
    import sys
    if "--demo" in sys.argv:
        closed = datetime(2026, 9, 5, 4, 55, tzinfo=JST)
        opened = datetime(2026, 9, 5, 4, 51, tzinfo=JST)
        demo = {"date": "2026-09-05", "side": "SHORT", "qty": 20, "model": "VP80_REVERSION", "grade": "A+",
                "entry": 29570.5, "exit": 29582.75, "stop": 29580.25, "tp1": 29535.0, "finalTarget": 29350.75,
                "pnlNet": -490.0, "rMultiple": -1.256}
        b, lv, src = bars_and_levels(opened, closed)
        sys.stdout.write(render_svg(demo, b, lv, opened, closed, src))

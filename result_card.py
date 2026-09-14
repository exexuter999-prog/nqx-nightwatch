#!/usr/bin/env python3
"""決済リザルト(PnL)カードの PNG。

`scenario_card.py` が「これから出す一件」を描くのに対し、こちらは
**終わった一件**を描く。R12 の分割型は 1枚を TP1、1枚を runner で持つので、
決済が 1 つの価格に収まらない —— TP1 と SL に半分ずつ当たる形が普通に起きる。
このカードはその内訳(`result["legs"]`)を、実際の確定足の上で見せる。

描くもの:
  - 保有区間の確定足ローソク(前後に文脈を少し足す)
  - ENTRY / SL / 各脚の TP 水平線と右端の値札
  - 脚ごとの着弾マーカー(TP1 は上向き、SL は下向き)を **その足の上に**置く
  - 実現損益のピル、脚別の内訳表、R と保有時間

描かないもの:
  - 形成中の足
  - 観測に無い価格(パスも合成しない)

数字はすべて `result` の実価格から導出する。損益を外から受け取らない
(表示層と正本が食い違う余地を残さない)。
"""
from __future__ import annotations

import io
import math
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

from PIL import Image, ImageDraw

from scenario_card import (
    AXIS, BG, GRID, GOLD, INK, INK_3, INK_4, PANEL, RED, RUST, TEAL, CRIMSON,
    _font, _num, nice_step,
)

W, H = 1080, 1350
PAD = 64
CHART_TOP = 176
CHART_H = 520
PRICE_LANE = 148
TIME_LANE = 42

JST = timezone(timedelta(hours=9))

# 勝ち負けの色は Mini App の result.js の TINT と同じ意味づけ。
TINT_WIN = CRIMSON        # このプロジェクトの「勝ち」は赤(赤黒基調)
TINT_LOSS = RUST
TINT_FLAT = (140, 147, 160)

COST_FLOOR_PT = 2.0       # ±2pt は FLAT(nqx_state / ledger.js と同値)


# ---------------------------------------------------------------- 導出

def derive(result: Dict[str, Any]) -> Dict[str, Any]:
    """表示に必要な値をすべて実価格から導出する(result.js の derive と同一)。"""
    direction = -1.0 if str(result.get("side", "")).upper() == "SHORT" else 1.0
    entry = float(result["entry"])
    exit_price = float(result["exit"])
    qty = int(result["qty"])
    point_value = float(result.get("pointValue") or 2.0)
    pts = (exit_price - entry) * direction
    # fees は滑り+手数料(1口座分)。価格から出せない観測値なので、あれば引く。
    # 実現損益(usd)の定義は記録の `exit` を基準にする —— ledger.js /
    # dayguard.realized_usd と同じ式でなければ画面ごとに数字が変わる。
    fees = abs(float(result["fees"])) if _num(result.get("fees")) is not None else 0.0
    usd = pts * point_value * qty - fees
    # 粗損益は **脚があれば脚から**出す。2脚の決済価格の平均は 0.25 の刻みに
    # 乗らないことがあり(1/8 になる)、記録の `exit` は丸めた値になる。丸めた
    # 価格から粗を出すと内訳の合計と食い違うので、脚が正本のときは脚を使う。
    legs = result.get("legs")
    if isinstance(legs, list) and legs:
        gross = sum((float(leg["exit"]) - entry) * direction * point_value * int(leg["qty"])
                    for leg in legs)
    else:
        gross = pts * point_value * qty
    # R56: 凍結プランの無い手動建玉(ブローカー約定由来)は stop を持たない → R 無し
    stop_raw = result.get("stop")
    risk = abs(entry - float(stop_raw)) if stop_raw is not None else 0.0
    r = pts / risk if risk else 0.0
    state = "flat" if abs(pts) <= COST_FLOOR_PT else ("win" if pts > 0 else "loss")
    try:
        opened = datetime.fromisoformat(str(result["openedAt"]).replace("Z", "+00:00"))
        closed = datetime.fromisoformat(str(result["closedAt"]).replace("Z", "+00:00"))
        secs = max(0.0, (closed - opened).total_seconds())
    except (TypeError, ValueError, KeyError):
        opened = closed = None
        secs = 0.0
    held = (f"{int(secs // 3600)}h {int(secs % 3600 // 60):02d}m" if secs >= 3600
            else f"{int(secs // 60)}m {int(secs % 60):02d}s")
    return {"dir": direction, "pts": pts, "usd": usd, "gross": gross, "fees": fees,
            "r": r, "state": state, "held": held, "openedAt": opened, "closedAt": closed,
            "qty": qty, "pointValue": point_value}


def leg_rows(result: Dict[str, Any], derived: Dict[str, Any]) -> List[Dict[str, Any]]:
    """脚別の内訳。``legs`` が無ければ全量 1 脚として扱う(旧記録も描ける)。"""
    entry = float(result["entry"])
    direction = derived["dir"]
    point_value = derived["pointValue"]
    legs = result.get("legs")
    if not isinstance(legs, list) or not legs:
        legs = [{"id": "POSITION", "qty": derived["qty"],
                 "exit": float(result["exit"]), "kind": "manual", "target": None}]
    rows = []
    for leg in legs:
        leg_qty = int(leg["qty"])
        leg_exit = float(leg["exit"])
        pts = (leg_exit - entry) * direction
        rows.append({
            "id": str(leg.get("id") or "LEG"),
            "qty": leg_qty,
            "exit": leg_exit,
            "target": _num(leg.get("target")),
            "kind": str(leg.get("kind") or "manual"),
            "pts": pts,
            "usd": pts * point_value * leg_qty,
        })
    return rows


def window_bars(bars: Sequence[Dict[str, Any]], derived: Dict[str, Any],
                lead: int = 10, trail: int = 4, limit: int = 64) -> List[Dict[str, float]]:
    """保有区間 + 前後の文脈だけを取り出す。形成中の足は呼び出し側が落とす。"""
    rows: List[Dict[str, float]] = []
    for bar in bars or []:
        if not isinstance(bar, dict):
            continue
        t = _num(bar.get("t") if bar.get("t") is not None else bar.get("time"))
        o = _num(bar.get("o") if bar.get("o") is not None else bar.get("open"))
        h = _num(bar.get("h") if bar.get("h") is not None else bar.get("high"))
        low = _num(bar.get("l") if bar.get("l") is not None else bar.get("low"))
        c = _num(bar.get("c") if bar.get("c") is not None else bar.get("close"))
        if None in (t, o, h, low, c) or h < low:
            continue
        rows.append({"t": t, "o": o, "h": h, "l": low, "c": c})
    rows.sort(key=lambda r: r["t"])
    if not rows or derived["openedAt"] is None or derived["closedAt"] is None:
        return rows[-limit:]

    start = derived["openedAt"].timestamp()
    end = derived["closedAt"].timestamp()
    inside = [i for i, r in enumerate(rows) if start <= r["t"] <= end]
    if not inside:
        return rows[-limit:]
    first = max(0, inside[0] - lead)
    last = min(len(rows), inside[-1] + 1 + trail)
    picked = rows[first:last]
    return picked[-limit:] if len(picked) > limit else picked


def _hit_index(bars: Sequence[Dict[str, float]], row: Dict[str, Any],
               derived: Dict[str, Any]) -> Optional[int]:
    """その脚の決済価格に最初に届いた足を探す。届いていなければ None。"""
    if derived["openedAt"] is None:
        return None
    start = derived["openedAt"].timestamp()
    price = row["exit"]
    # LONG なら「高値が TP に届く」/「安値が SL に届く」。SHORT は逆。
    upward = row["pts"] > 0 if derived["dir"] > 0 else row["pts"] <= 0
    for index, bar in enumerate(bars):
        if bar["t"] < start:
            continue
        if (bar["h"] >= price - 1e-9) if upward else (bar["l"] <= price + 1e-9):
            return index
    return None


def _fmt(value: float) -> str:
    return f"{value:,.2f}"


def _money(value: float) -> str:
    sign = "−" if value < 0 else "+"
    return f"{sign}${abs(value):,.2f}"


# ---------------------------------------------------------------- 描画

def _marker(d: ImageDraw.ImageDraw, cx: float, cy: float, up: bool,
            colour: Tuple[int, int, int], size: float = 15.0) -> None:
    """着弾マーカー。TP は上向き、SL は下向きの三角で、価格線の上に置く。"""
    if up:
        pts = [(cx, cy - size), (cx - size * 0.86, cy + size * 0.62),
               (cx + size * 0.86, cy + size * 0.62)]
    else:
        pts = [(cx, cy + size), (cx - size * 0.86, cy - size * 0.62),
               (cx + size * 0.86, cy - size * 0.62)]
    d.polygon(pts, fill=colour, outline=(11, 12, 14))


def render_png(result: Dict[str, Any], bars: Optional[Sequence[Dict[str, Any]]] = None,
               accounts: int = 1, reported_net: Optional[float] = None,
               note: Optional[str] = None) -> bytes:
    """リザルトカードを描く。足が無くてもカード自体は必ず描く(欄が空くだけ)。

    ``result`` から出る損益は **記録した価格どおりの粗損益**で、1口座あたり
    (dayguard の ``pnlEach`` と同じ規約)。``reported_net`` にブローカー側の
    実現損益(1口座あたり)を渡すと、粗損益との差を **滑り+手数料** として
    そのまま出す。**差を推定で price 側へ按分しない** —— 実約定価格を取得
    できていない以上、内訳を作れば必ず嘘になる。
    """
    derived = derive(result)
    rows = leg_rows(result, derived)
    tint = {"win": TINT_WIN, "loss": TINT_LOSS, "flat": TINT_FLAT}[derived["state"]]
    plot = window_bars(bars or [], derived)

    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    f_title = _font(34, bold=True)
    f_head = _font(21)
    f_axis = _font(17)
    f_tag = _font(18, bold=True)
    f_pill = _font(74, bold=True)
    f_sub = _font(22)
    f_row = _font(22)
    f_leg = _font(23, bold=True)
    f_small = _font(16)

    entry = float(result["entry"])
    stop = float(result["stop"]) if result.get("stop") is not None else None
    exit_price = float(result["exit"])
    symbol = str(result.get("symbol") or "MNQU6")
    side_word = "LONG" if derived["dir"] > 0 else "SHORT"

    # ---- ヘッダ
    d.text((PAD, 44), "NIGHTWATCH", font=f_title, fill=INK)
    opened_txt = (derived["openedAt"].astimezone(JST).strftime("%m/%d %H:%M")
                  if derived["openedAt"] else "--:--")
    closed_txt = (derived["closedAt"].astimezone(JST).strftime("%H:%M")
                  if derived["closedAt"] else "--:--")
    d.text((PAD, 88), f"{symbol} · 3m · {side_word} {derived['qty']}   ·   "
                      f"{opened_txt}–{closed_txt} JST", font=f_head, fill=INK_3)
    stamp = {"win": "FILED", "loss": "REDACTED", "flat": "NO ACTION"}[derived["state"]]
    sw = d.textlength(stamp, font=f_title)
    d.text((W - PAD - sw, 44), stamp, font=f_title, fill=tint)
    mode = str(result.get("mode") or "SIMULATION").upper()
    mw = d.textlength(mode, font=f_small)
    d.text((W - PAD - mw, 92), mode, font=f_small, fill=INK_4)

    # ---- チャート面
    x0, y0 = PAD, CHART_TOP
    x1, y1 = W - PAD, CHART_TOP + CHART_H
    d.rounded_rectangle((x0, y0, x1, y1), radius=12, fill=PANEL, outline=GRID)
    plot_x0, plot_x1 = x0 + 2, x1 - PRICE_LANE
    plot_y0, plot_y1 = y0 + 16, y1 - TIME_LANE
    plot_w, plot_h = plot_x1 - plot_x0, plot_y1 - plot_y0

    # スケールに入れるのは「実際に起きた価格」だけ。到達しなかった runner の
    # 最終 TP まで含めると、値動きが縦に潰れて読めなくなる(その水準は
    # 範囲内に収まったときだけ線で描く)。
    anchors = [entry, exit_price] + ([stop] if stop is not None else []) + [r["exit"] for r in rows]
    if plot:
        lo = min(min(b["l"] for b in plot), *anchors)
        hi = max(max(b["h"] for b in plot), *anchors)
    else:
        lo, hi = min(anchors), max(anchors)
    span = max(hi - lo, 1.0)
    lo, hi = lo - span * 0.10, hi + span * 0.10
    span = hi - lo

    def y(v: float) -> float:
        return plot_y1 - (v - lo) / span * plot_h

    # ---- グリッドと価格軸
    step = nice_step(span, plot_h)
    tick = math.ceil(lo / step) * step
    axis_labels: List[Tuple[float, str]] = []
    while tick <= hi + 1e-9:
        gy = y(tick)
        d.line((plot_x0, gy, plot_x1, gy), fill=GRID, width=1)
        axis_labels.append((gy, f"{int(round(tick)):,}" if step >= 1 else _fmt(tick)))
        tick += step

    slot_w = plot_w / max(1, len(plot))

    def x(index: int) -> float:
        return plot_x0 + index * slot_w + slot_w / 2

    if plot:
        every = max(1, int(math.ceil(56 / slot_w)))
        for i, b in enumerate(plot):
            if i % every:
                continue
            gx = x(i)
            if gx < plot_x0 + 26 or gx > plot_x1 - 26:
                continue
            d.line((gx, plot_y0, gx, plot_y1), fill=GRID, width=1)
            label = datetime.fromtimestamp(b["t"], JST).strftime("%H:%M")
            lw = d.textlength(label, font=f_axis)
            d.text((gx - lw / 2, plot_y1 + 11), label, font=f_axis, fill=AXIS)
    d.line((plot_x1, y0, plot_x1, y1), fill=GRID, width=1)

    # ---- 保有区間の帯(entry↔stop = 受け入れたリスク / entry↔exit = 結果)
    hold = [i for i, b in enumerate(plot)
            if derived["openedAt"] and derived["closedAt"]
            and derived["openedAt"].timestamp() <= b["t"] <= derived["closedAt"].timestamp()]
    if hold:
        hx0, hx1 = x(hold[0]) - slot_w / 2, x(hold[-1]) + slot_w / 2
        if stop is not None:
            risk = Image.new("RGBA", (max(1, int(hx1 - hx0)),
                                      max(1, int(abs(y(entry) - y(stop))))), (*RED, 40))
            img.paste(risk, (int(hx0), int(min(y(entry), y(stop)))), risk)
        gain = Image.new("RGBA", (max(1, int(hx1 - hx0)),
                                  max(1, int(abs(y(entry) - y(exit_price))))), (*tint, 46))
        img.paste(gain, (int(hx0), int(min(y(entry), y(exit_price)))), gain)
        d = ImageDraw.Draw(img)

    # ---- ローソク
    body_w = max(3, int(slot_w * 0.62))
    for i, b in enumerate(plot):
        cx = x(i)
        up = b["c"] >= b["o"]
        col = TEAL if up else RED
        d.line((cx, y(b["h"]), cx, y(b["l"])), fill=col, width=2)
        top, bot = y(max(b["o"], b["c"])), y(min(b["o"], b["c"]))
        if bot - top < 2:
            bot = top + 2
        d.rectangle((cx - body_w / 2, top, cx + body_w / 2, bot), fill=col)

    # ---- 水平線(ENTRY / SL / 各脚の決済)と値札
    tags: List[Tuple[float, str, Tuple[int, int, int]]] = []

    def dashed(price: float, colour: Tuple[int, int, int], width: int, gap: int) -> None:
        gy = y(price)
        for sx in range(int(plot_x0), int(plot_x1), gap):
            d.line((sx, gy, min(sx + max(3, gap // 3), plot_x1), gy), fill=colour, width=width)

    dashed(entry, INK_3, 2, 10)
    tags.append((y(entry), _fmt(entry), INK_3))
    if stop is not None:
        d.line((plot_x0, y(stop), plot_x1, y(stop)), fill=RED, width=2)
        tags.append((y(stop), _fmt(stop), RED))

    unreached: List[Tuple[str, float]] = []
    for row in rows:
        colour = TEAL if row["kind"] == "target" else RED if row["kind"] == "stop" else GOLD
        # 到達しなかった計画上の TP。範囲に収まるときだけ薄い破線で残す。
        if row["target"] is not None and abs(row["target"] - row["exit"]) > 1e-9:
            if lo <= row["target"] <= hi:
                dashed(row["target"], (58, 96, 92), 1, 16)
                tags.append((y(row["target"]), _fmt(row["target"]), (58, 96, 92)))
            else:
                unreached.append((row["id"], row["target"]))
        if stop is None or abs(row["exit"] - stop) > 1e-9:
            d.line((plot_x0, y(row["exit"]), plot_x1, y(row["exit"])), fill=colour, width=2)
            tags.append((y(row["exit"]), _fmt(row["exit"]), colour))
        hit = _hit_index(plot, row, derived)
        if hit is not None:
            _marker(d, x(hit), y(row["exit"]), row["pts"] > 0, colour)
            label = f"{row['id']} {row['qty']}"
            lw = d.textlength(label, font=f_tag)
            ly = y(row["exit"]) + (-40 if row["pts"] > 0 else 22)
            lx = min(max(plot_x0 + 4, x(hit) - lw / 2), plot_x1 - lw - 4)
            d.rounded_rectangle((lx - 8, ly - 4, lx + lw + 8, ly + 24), radius=5,
                                fill=(11, 12, 14), outline=colour)
            d.text((lx, ly), label, font=f_tag, fill=colour)

    # 建玉マーカー(ENTRY)。最初の保有足の上に置く。
    if hold:
        _marker(d, x(hold[0]), y(entry), derived["dir"] > 0, INK, size=12.0)

    # ---- 軸タグ(重なりを押し広げる)
    TAG_H = 30
    tags.sort(key=lambda r: r[0])
    placed: List[Tuple[float, str, Tuple[int, int, int]]] = []
    cursor = plot_y0
    for ty, label, col in tags:
        ty = max(cursor, ty - TAG_H / 2)
        placed.append((ty, label, col))
        cursor = ty + TAG_H + 2
    cursor = plot_y1 - TAG_H
    for i in range(len(placed) - 1, -1, -1):
        ty, label, col = placed[i]
        ty = min(ty, cursor)
        placed[i] = (ty, label, col)
        cursor = ty - TAG_H - 2
    for gy, label in axis_labels:
        if any(abs(gy - (ty + TAG_H / 2)) < TAG_H for ty, *_ in placed):
            continue
        d.text((plot_x1 + 12, gy - 10), label, font=f_axis, fill=AXIS)
    for ty, label, col in placed:
        tx0, tx1 = plot_x1 + 6, x1 - 6
        d.rounded_rectangle((tx0, ty, tx1, ty + TAG_H), radius=4, fill=col)
        lw = d.textlength(label, font=f_tag)
        d.text(((tx0 + tx1) / 2 - lw / 2, ty + 5), label, font=f_tag, fill=(11, 12, 14))

    if not plot:
        note = "NO OBSERVED BARS FOR THIS WINDOW"
        nw = d.textlength(note, font=f_head)
        d.text(((plot_x0 + plot_x1) / 2 - nw / 2, (plot_y0 + plot_y1) / 2), note,
               font=f_head, fill=INK_4)

    # ---- 実現損益のピル
    pill_y = y1 + 44
    label = _money(derived["usd"])
    lw = d.textlength(label, font=f_pill)
    pill_h = 104
    d.rounded_rectangle((PAD, pill_y, PAD + lw + 60, pill_y + pill_h),
                        radius=pill_h // 2, fill=tint)
    d.text((PAD + 30, pill_y + 14), label, font=f_pill, fill=(7, 8, 10))

    sub = (f"{symbol} · {derived['qty']} CONTRACT{'S' if derived['qty'] > 1 else ''} · "
           f"{'+' if derived['pts'] >= 0 else '−'}{abs(derived['pts']):.2f} pt · "
           f"{'+' if derived['r'] >= 0 else '−'}{abs(derived['r']):.2f}R · {derived['held']}")
    d.text((PAD, pill_y + pill_h + 18), sub, font=f_sub, fill=INK_3)

    verdict = str(result.get("verdict") or "")
    if verdict:
        d.text((PAD, pill_y + pill_h + 52), verdict, font=f_row, fill=INK)

    # ---- 脚別の内訳(このカードの主題)
    table_y = pill_y + pill_h + 92
    d.text((PAD, table_y), "SPLIT EXIT", font=f_small, fill=INK_4)
    table_y += 26
    ROW_H = 52
    for i, row in enumerate(rows):
        ry = table_y + i * ROW_H
        colour = TEAL if row["kind"] == "target" else RED if row["kind"] == "stop" else GOLD
        d.line((PAD, ry, W - PAD, ry), fill=(48, 50, 60), width=1)
        d.rectangle((PAD, ry + 12, PAD + 5, ry + 40), fill=colour)
        d.text((PAD + 18, ry + 13), f"{row['id']}  {row['qty']}", font=f_leg, fill=INK)
        kind = {"target": "TP HIT", "stop": "SL HIT"}.get(row["kind"], "MANUAL")
        d.text((PAD + 218, ry + 15), kind, font=f_row, fill=colour)
        price = _fmt(row["exit"])
        d.text((PAD + 372, ry + 15), price, font=f_row, fill=INK_3)
        pts = f"{'+' if row['pts'] >= 0 else '−'}{abs(row['pts']):.2f} pt"
        d.text((PAD + 560, ry + 15), pts, font=f_row, fill=INK_3)
        usd = _money(row["usd"])
        uw = d.textlength(usd, font=f_leg)
        d.text((W - PAD - uw, ry + 13), usd, font=f_leg, fill=colour)
    end_y = table_y + len(rows) * ROW_H
    d.line((PAD, end_y, W - PAD, end_y), fill=(48, 50, 60), width=1)

    d.text((PAD, end_y + 16), "GROSS (fills, per account)", font=f_row, fill=INK_4)
    net = _money(derived["gross"])
    nw = d.textlength(net, font=f_leg)
    d.text((W - PAD - nw, end_y + 14), net, font=f_leg, fill=tint)

    # ---- 突合(残高に効くのはブローカーの実現損益のほう)
    if reported_net is None and derived["fees"]:
        reported_net = derived["usd"]
    if reported_net is not None or accounts > 1:
        gross = derived["gross"]
        actual = gross if reported_net is None else float(reported_net)
        book_y = end_y + 56
        lines = [("BROKER NET (per account)", actual, INK, f_row)]
        if abs(actual - gross) > 1e-9:
            lines.append(("FEES + SLIPPAGE", actual - gross, RUST, f_row))
        lines.append((f"NET TO BALANCE × {accounts} ACCT{'S' if accounts > 1 else ''}",
                      actual * accounts, tint, f_leg))
        d.line((PAD, book_y, W - PAD, book_y), fill=(48, 50, 60), width=1)
        for i, (label, value, colour, font) in enumerate(lines):
            ry = book_y + 14 + i * 38
            last = i == len(lines) - 1
            d.text((PAD, ry), label, font=f_row, fill=INK if last else INK_4)
            text = _money(value)
            tw = d.textlength(text, font=font)
            d.text((W - PAD - tw, ry - (2 if last else 0)), text, font=font, fill=colour)

    # ---- フッタ
    fy = H - 92
    footnotes = [f"{name} FINAL TP {_fmt(price)} NOT REACHED" for name, price in unreached]
    if note:
        footnotes.append(str(note))
    if footnotes:
        d.text((PAD, fy), "  ·  ".join(footnotes), font=f_small, fill=INK_4)
    fy = H - 62
    d.text((PAD, fy), f"ENTRY {_fmt(entry)}  ·  SL {_fmt(stop)}  ·  "
                      f"AVG EXIT {_fmt(exit_price)}", font=f_small, fill=INK_4)
    tagline = "NOX VIGILAT · MDCCLXXVI"
    tl = d.textlength(tagline, font=f_small)
    d.text((W - PAD - tl, fy), tagline, font=f_small, fill=INK_4)

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def caption(result: Dict[str, Any]) -> str:
    """Telegram の写真キャプション(HTML)。"""
    derived = derive(result)
    rows = leg_rows(result, derived)
    head = ("WIN" if derived["state"] == "win"
            else "LOSS" if derived["state"] == "loss" else "FLAT")
    side = "LONG" if derived["dir"] > 0 else "SHORT"
    lines = [f"<b>{head} · {side} {derived['qty']} · {_money(derived['usd'])}</b>",
             f"E {_fmt(float(result['entry']))} · SL {_fmt(float(result['stop']))} · "
             f"AVG {_fmt(float(result['exit']))}"]
    for row in rows:
        kind = {"target": "TP", "stop": "SL"}.get(row["kind"], "MANUAL")
        lines.append(f"{row['id']} {row['qty']}枚 {kind} @{_fmt(row['exit'])} "
                     f"{_money(row['usd'])}")
    return "\n".join(lines)


if __name__ == "__main__":
    import json
    import sys

    payload = json.load(sys.stdin)
    doc = payload.get("result", payload)
    png = render_png(doc, payload.get("bars"))
    out = sys.argv[1] if len(sys.argv) > 1 else "result_card.png"
    with open(out, "wb") as fh:
        fh.write(png)
    print(f"wrote {out} ({len(png)} bytes)")

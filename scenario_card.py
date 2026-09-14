#!/usr/bin/env python3
"""Telegram 通知用のシナリオチャート画像。

監視サイクルの bundle(確定足 60 本 + レベル + scenario)から、Mini App と
同じ赤黒基調のチャートカードを PNG で描く。Pillow だけで完結し、フォントも
外部依存しない(等幅の内蔵フォントに落ちる)。

描くもの:
  - TradingView 流儀のローソク(ティール/赤・中実)、丸い刻みの価格軸、時間軸
  - VWAP の曲線(金)
  - 武装シナリオのポジションボックス(ENTRY/SL/TP)と右端の値札
  - ヘッダ(銘柄・足種・モデル・等級・方向)、フッタ(R:R・リスク・有効期限)

描かないもの:
  - 形成中の足(確定足のみ。chart.js と同じ規律)
  - 推測で補った価格(bundle に無いものは空欄のまま)

このモジュールは純粋で、外部I/Oは呼び出し側が行う。
"""
from __future__ import annotations

import io
import math
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

from PIL import Image, ImageDraw, ImageFont

# ---- パレット(Mini App の styles.css と揃える)
BG = (9, 9, 10)
PANEL = (19, 23, 34)           # --chart-bg
GRID = (42, 46, 57)            # --chart-grid
AXIS = (139, 147, 167)         # --chart-axis
INK = (236, 233, 223)
INK_3 = (143, 139, 129)
INK_4 = (84, 82, 75)
TEAL = (38, 166, 154)
RED = (239, 83, 80)
CRIMSON = (255, 59, 79)        # --acid(赤黒基調)
RUST = (201, 106, 77)
GOLD = (232, 193, 90)
WHITE = (255, 255, 255)

W, H = 1080, 720               # Telegram のプレビューで潰れない比率(3:2)
PAD = 36
HEADER_H = 92
FOOTER_H = 72
PRICE_LANE = 150
TIME_LANE = 40

JST = timezone(timedelta(hours=9))


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    """等幅フォント。無ければ Pillow 内蔵にフォールバック(描画は止めない)。"""
    candidates = [
        "C:/Windows/Fonts/consolab.ttf" if bold else "C:/Windows/Fonts/consola.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf" if bold
        else "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
        "/System/Library/Fonts/Menlo.ttc",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _num(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def nice_step(rng: float, plot_h: float, min_px: float = 44.0) -> float:
    """chart.js の niceStep と同じ規則。"""
    if rng <= 0 or plot_h <= 0:
        return 1.0
    max_lines = max(2, int(plot_h // min_px))
    rough = rng / max_lines
    for step in (0.25, 0.5, 1, 2, 2.5, 5, 10, 20, 25, 50, 100, 200, 250, 500, 1000):
        if step >= rough:
            return float(step)
    return 1000.0


def confirmed_bars(bundle: Dict[str, Any]) -> List[Dict[str, float]]:
    """確定足だけを取り出す。形成中の最終足は落とす(chart.js と同じ)。"""
    snapshot = bundle.get("snapshot") if isinstance(bundle.get("snapshot"), dict) else {}
    raw = snapshot.get("bars3m") or snapshot.get("bars") or bundle.get("bars") or []
    bars = []
    for row in raw:
        if not isinstance(row, dict):
            continue
        t = _num(row.get("t") or row.get("time"))
        o, h, l, c = (_num(row.get(k) or row.get(alt)) for k, alt in
                      (("o", "open"), ("h", "high"), ("l", "low"), ("c", "close")))
        if None in (t, o, h, l, c) or h < l or h < max(o, c) or l > min(o, c):
            continue
        bars.append({"t": t, "o": o, "h": h, "l": l, "c": c, "v": _num(row.get("v") or row.get("volume"))})
    bars = bars[-60:]
    at = bundle.get("at") or snapshot.get("at")
    try:
        at_sec = datetime.fromisoformat(str(at).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        at_sec = float("inf")
    if len(bars) >= 2:
        step = bars[-1]["t"] - bars[-2]["t"]
        if step > 0 and bars[-1]["t"] + step > at_sec + 2:
            bars = bars[:-1]
    return bars


def vwap_series(bars: List[Dict[str, float]]) -> Optional[List[float]]:
    if not bars or any(b.get("v") is None for b in bars):
        return None
    acc_pv = acc_v = 0.0
    out = []
    for b in bars:
        hlc3 = (b["h"] + b["l"] + b["c"]) / 3.0
        acc_pv += hlc3 * b["v"]
        acc_v += b["v"]
        out.append(acc_pv / acc_v if acc_v > 0 else hlc3)
    return out


def _scenario(bundle: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    scenarios = bundle.get("scenarios") if isinstance(bundle.get("scenarios"), dict) else {}
    primary = scenarios.get("primary") if isinstance(scenarios.get("primary"), dict) else None
    if primary is None:
        return None
    entry, stop = _num(primary.get("entry")), _num(primary.get("stop"))
    targets = primary.get("targets") if isinstance(primary.get("targets"), list) else []
    target = _num(primary.get("target")) if primary.get("target") is not None else (
        _num(targets[0]) if targets else None)
    if None in (entry, stop, target):
        return None
    side = str(primary.get("side") or "").upper()
    return {**primary, "entry": entry, "stop": stop, "target": target,
            "targets": [t for t in (_num(x) for x in targets) if t is not None] or [target],
            "side": side}


def _fmt(value: float) -> str:
    return f"{value:,.2f}"


def render_png(bundle: Dict[str, Any]) -> Optional[bytes]:
    """bundle からシナリオカード PNG を描く。描けない(足が無い等)なら None。"""
    bars = confirmed_bars(bundle)
    if len(bars) < 5:
        return None
    scenario = _scenario(bundle)
    price = _num(bundle.get("price")) or bars[-1]["c"]
    levels = []
    snapshot = bundle.get("snapshot") if isinstance(bundle.get("snapshot"), dict) else {}
    for lv in snapshot.get("levels") or []:
        if isinstance(lv, dict) and _num(lv.get("price")) is not None:
            levels.append({"label": str(lv.get("label") or lv.get("name") or ""), "price": _num(lv.get("price"))})

    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    f_title = _font(30, bold=True)
    f_head = _font(20)
    f_axis = _font(17)
    f_tag = _font(18, bold=True)
    f_foot = _font(19)
    f_small = _font(15)

    # ---- ヘッダ
    side = scenario["side"] if scenario else ""
    accent = CRIMSON if side == "SELL" else TEAL if side == "BUY" else INK_3
    d.text((PAD, 26), "NIGHTWATCH", font=f_title, fill=INK)
    sub = f"MNQ · 3m"
    if scenario:
        model = str(scenario.get("model") or scenario.get("title") or "").upper()
        grade = str(scenario.get("grade") or "")
        bits = [x for x in (model[:26], grade, "LONG" if side == "BUY" else "SHORT" if side == "SELL" else "") if x]
        sub += "   " + "  ·  ".join(bits)
    d.text((PAD, 62), sub, font=f_head, fill=INK_3)
    # 右上: 現在値
    last_txt = _fmt(price)
    tw = d.textlength(last_txt, font=f_title)
    d.text((W - PAD - tw, 26), last_txt, font=f_title, fill=INK)
    stamp = datetime.now(JST).strftime("%m/%d %H:%M JST")
    sw = d.textlength(stamp, font=f_small)
    d.text((W - PAD - sw, 66), stamp, font=f_small, fill=INK_4)

    # ---- チャート面
    x0, y0 = PAD, HEADER_H
    x1, y1 = W - PAD, H - FOOTER_H
    d.rounded_rectangle((x0, y0, x1, y1), radius=10, fill=PANEL, outline=GRID)
    plot_x0, plot_x1 = x0 + 1, x1 - PRICE_LANE
    plot_y0, plot_y1 = y0 + 14, y1 - TIME_LANE
    plot_w, plot_h = plot_x1 - plot_x0, plot_y1 - plot_y0

    # スケール: 足 + シナリオ価格を含め、上下に 6% の余白
    lo = min(b["l"] for b in bars)
    hi = max(b["h"] for b in bars)
    if scenario:
        lo = min(lo, scenario["stop"], scenario["target"], scenario["entry"], *scenario["targets"])
        hi = max(hi, scenario["stop"], scenario["target"], scenario["entry"], *scenario["targets"])
    span = max(hi - lo, 1.0)
    lo, hi = lo - span * 0.06, hi + span * 0.06
    span = hi - lo

    def y(v: float) -> float:
        return plot_y1 - (v - lo) / span * plot_h

    # 時間スロット(欠測を詰めない)
    step_sec = max(1.0, min(b2["t"] - b1["t"] for b1, b2 in zip(bars, bars[1:])))
    first_t = bars[0]["t"]
    slots = [int(round((b["t"] - first_t) / step_sec)) for b in bars]
    future = max(6, int(len(bars) * 0.16)) if scenario else 2
    slot_w = plot_w / (slots[-1] + 1 + future)

    def x(slot: int) -> float:
        return plot_x0 + slot * slot_w + slot_w / 2

    # ---- グリッド(丸い刻み)と価格軸
    step = nice_step(span, plot_h)
    tick = math.ceil(lo / step) * step
    axis_labels: List[Tuple[float, str]] = []
    while tick <= hi + 1e-9:
        gy = y(tick)
        d.line((plot_x0, gy, plot_x1, gy), fill=GRID, width=1)
        label = f"{int(round(tick)):,}" if step >= 1 else _fmt(tick)
        axis_labels.append((gy, label))
        tick += step
    # 時間軸: 44px 以上離す
    every = max(1, int(math.ceil(52 / slot_w)))
    for i, b in enumerate(bars):
        if i % every:
            continue
        gx = x(slots[i])
        if gx < plot_x0 + 24 or gx > plot_x1 - 24:
            continue
        d.line((gx, plot_y0, gx, plot_y1), fill=GRID, width=1)
        label = datetime.fromtimestamp(b["t"], JST).strftime("%H:%M")
        lw = d.textlength(label, font=f_axis)
        d.text((gx - lw / 2, plot_y1 + 10), label, font=f_axis, fill=AXIS)
    d.line((plot_x1, y0, plot_x1, y1), fill=GRID, width=1)
    d.line((plot_x0, plot_y1 + 4, plot_x1, plot_y1 + 4), fill=GRID, width=1)

    # ---- レベル(点線・淡く)
    for lv in levels[:10]:
        if not (lo <= lv["price"] <= hi):
            continue
        ly = y(lv["price"])
        for sx in range(int(plot_x0), int(plot_x1), 12):
            d.line((sx, ly, min(sx + 5, plot_x1), ly), fill=(60, 64, 78), width=1)

    # ---- VWAP
    vw = vwap_series(bars)
    if vw:
        pts = [(x(slots[i]), y(v)) for i, v in enumerate(vw)]
        d.line(pts, fill=GOLD, width=3, joint="curve")

    # ---- ポジションボックス(現在足の直後から右端まで)
    tags: List[Tuple[float, str, Tuple[int, int, int], bool]] = []
    if scenario:
        e, s, t = scenario["entry"], scenario["stop"], scenario["target"]
        bx0 = min(plot_x1 - 12, x(slots[-1]) + slot_w * 0.9)
        bx1 = plot_x1 - 2
        risk_box = Image.new("RGBA", (int(bx1 - bx0), max(1, int(abs(y(e) - y(s))))), (*RED, 58))
        img.paste(risk_box, (int(bx0), int(min(y(e), y(s)))), risk_box)
        reward_box = Image.new("RGBA", (int(bx1 - bx0), max(1, int(abs(y(e) - y(t))))), (*TEAL, 58))
        img.paste(reward_box, (int(bx0), int(min(y(e), y(t)))), reward_box)
        d = ImageDraw.Draw(img)
        d.line((bx0, y(e), bx1, y(e)), fill=accent, width=3)
        d.line((bx0, y(s), bx1, y(s)), fill=RED, width=2)
        d.line((bx0, y(t), bx1, y(t)), fill=TEAL, width=2)
        tags += [(y(e), _fmt(e), accent, True), (y(s), _fmt(s), RED, True), (y(t), _fmt(t), TEAL, True)]

    # ---- ローソク(中実・ヒゲ同色)
    body_w = max(3, int(slot_w * 0.66))
    for i, b in enumerate(bars):
        cx = x(slots[i])
        up = b["c"] >= b["o"]
        col = TEAL if up else RED
        d.line((cx, y(b["h"]), cx, y(b["l"])), fill=col, width=2)
        top, bot = y(max(b["o"], b["c"])), y(min(b["o"], b["c"]))
        if bot - top < 2:
            bot = top + 2
        d.rectangle((cx - body_w / 2, top, cx + body_w / 2, bot), fill=col)

    # ---- 現在値(点線 + 軸タグ)
    last = bars[-1]["c"]
    prev = bars[-2]["c"] if len(bars) > 1 else last
    live_col = TEAL if last >= prev else RED
    ly = y(last)
    for sx in range(int(plot_x0), int(plot_x1), 8):
        d.line((sx, ly, min(sx + 3, plot_x1), ly), fill=live_col, width=2)
    tags.append((ly, _fmt(last), live_col, True))

    # ---- 軸タグ(重なりを押し広げ、被った軸数字は消す)
    TAG_H = 30
    tags.sort(key=lambda r: r[0])
    placed: List[Tuple[float, str, Tuple[int, int, int], bool]] = []
    cursor = plot_y0
    for ty, label, col, solid in tags:
        ty = max(cursor, ty - TAG_H / 2)
        placed.append((ty, label, col, solid))
        cursor = ty + TAG_H + 2
    # 下端からも押し戻す
    cursor = plot_y1 - TAG_H
    for i in range(len(placed) - 1, -1, -1):
        ty, label, col, solid = placed[i]
        ty = min(ty, cursor)
        placed[i] = (ty, label, col, solid)
        cursor = ty - TAG_H - 2
    for gy, label in axis_labels:
        if any(abs(gy - (ty + TAG_H / 2)) < TAG_H for ty, *_ in placed):
            continue
        d.text((plot_x1 + 12, gy - 10), label, font=f_axis, fill=AXIS)
    for ty, label, col, solid in placed:
        tx0, tx1 = plot_x1 + 6, x1 - 6
        d.rounded_rectangle((tx0, ty, tx1, ty + TAG_H), radius=4, fill=col if solid else PANEL, outline=col)
        lw = d.textlength(label, font=f_tag)
        d.text(((tx0 + tx1) / 2 - lw / 2, ty + 5), label, font=f_tag, fill=(11, 12, 14) if solid else col)

    # ---- フッタ
    fy = H - FOOTER_H + 18
    if scenario:
        e, s, t = scenario["entry"], scenario["stop"], scenario["target"]
        risk_pt = abs(e - s)
        reward_pt = abs(t - e)
        rr = reward_pt / risk_pt if risk_pt > 0 else 0.0
        qty = int(_num(scenario.get("qty")) or 2)
        risk_usd = risk_pt * qty * 2.0
        parts = [f"ENTRY {_fmt(e)}", f"SL {_fmt(s)}", f"TP {_fmt(t)}",
                 f"R:R 1:{rr:.2f}", f"RISK ${risk_usd:,.0f} × {qty}"]
        exp = scenario.get("expiresAt")
        try:
            exp_txt = datetime.fromisoformat(str(exp).replace("Z", "+00:00")).astimezone(JST).strftime("%H:%M")
            parts.append(f"UNTIL {exp_txt}")
        except (TypeError, ValueError):
            pass
        # 右端のタグラインと被らない幅に収める(フォントを一段落としてでも切らない)。
        line = "   ·   ".join(parts)
        font = f_foot
        max_w = W - PAD * 2 - 260
        if d.textlength(line, font=font) > max_w:
            font = _font(16)
            line = "  ·  ".join(parts)
        d.text((PAD, fy), line, font=font, fill=INK_3)
        # 方向のアクセントバー
        d.rounded_rectangle((PAD, fy + 36, PAD + 220, fy + 40), radius=2, fill=accent)
    else:
        d.text((PAD, fy), "NO ARMED SCENARIO   ·   confirmed bars only", font=f_foot, fill=INK_4)
    tagline = "NOX VIGILAT · MDCCLXXVI"
    tl = d.textlength(tagline, font=f_small)
    d.text((W - PAD - tl, fy + 26), tagline, font=f_small, fill=INK_4)

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def caption(bundle: Dict[str, Any]) -> str:
    """写真に添える短いキャプション(HTML)。本文は従来のメッセージが担う。"""
    scenario = _scenario(bundle)
    if not scenario:
        return "<b>NIGHTWATCH</b> · no armed scenario"
    side = "LONG" if scenario["side"] == "BUY" else "SHORT"
    model = str(scenario.get("model") or "").upper()
    grade = str(scenario.get("grade") or "")
    head = " · ".join(x for x in (model, grade, side) if x)
    return (f"<b>{head}</b>\n"
            f"E {_fmt(scenario['entry'])} · SL {_fmt(scenario['stop'])} · TP {_fmt(scenario['target'])}")


if __name__ == "__main__":
    import json
    import sys

    data = json.load(sys.stdin)
    png = render_png(data)
    if png is None:
        sys.exit("no renderable bars")
    out = sys.argv[1] if len(sys.argv) > 1 else "scenario_card.png"
    with open(out, "wb") as fh:
        fh.write(png)
    print(f"wrote {out} ({len(png)} bytes)")

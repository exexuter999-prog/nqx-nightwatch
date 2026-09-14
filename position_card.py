#!/usr/bin/env python3
"""保有中(OPEN)ポジションのカード PNG。

`scenario_card.py` が「これから出す一件」、`result_card.py` が「終わった一件」を
描くのに対し、こちらは **いま持っている一件** を描く。

決定的に違うのは、決済価格がまだ無いこと。だから確定済みの損益は出さず、
**現値での含み損益** と、**各水準に届いたら幾らになるか** の two-sided な提示に
なる。TP1 / RUNNER(TP2) / SL のそれぞれに、到達時の損益を金額で載せる。

描くもの:
  - 建玉時刻から現在までの確定足(前に少し文脈)
  - ENTRY / SL / TP1 / TP2 の発光する水平線と右端の値札
  - **トラックバー**: SL → ENTRY → TP1 → TP2 を価格に比例した一本のレールへ
    並べ、現値をその上の光点で示す。左が損、右が利。各節に到達時損益($)。
  - 水準ごとのラダーバー: 到達時損益($) + R + 現値からの距離(pt) + 進捗
  - 現値での含み損益(口座あたり / 合計)

描かないもの:
  - 形成中の足
  - 実約定価格の推定(ブローカーが返さない値を作らない)
  - 手数料込みの「確定」損益(まだ決済していないので確定しない)

数字はすべて建玉照会の実価格と確定足から導出する。
"""
from __future__ import annotations

import argparse
import io
import json
import math
import os
import random
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from PIL import Image, ImageDraw, ImageFilter, ImageChops, ImageFont

from scenario_card import _font, _num, nice_step


def _jfont(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    """和文が要る行だけに使う。等幅フォントは「枚」を持たず豆腐になる。

    見つからなければ等幅へ落とす —— 豆腐は出るが描画は止めない。
    """
    candidates = [
        "C:/Windows/Fonts/YuGothB.ttc" if bold else "C:/Windows/Fonts/YuGothM.ttc",
        "C:/Windows/Fonts/meiryob.ttc" if bold else "C:/Windows/Fonts/meiryo.ttc",
        "C:/Windows/Fonts/msgothic.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc" if bold
        else "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/System/Library/Fonts/ヒラギノ角ゴシック W6.ttc",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return _font(size, bold=bold)


W, H = 1080, 1384
PAD = 62
CHART_TOP = 300
CHART_H = 430
PRICE_LANE = 150
TIME_LANE = 36
TRACK_H = 150                   # SL→ENTRY→TP1→TP2 の一本レールが占める高さ
RAIL_H = 26                     # レールそのものの太さ
ROW_H = 96
ROW_GAP = 14

JST = timezone(timedelta(hours=9))
POINT_VALUE = 2.0

# ---- 神秘系パレット(赤黒基調のプロジェクトに、夜空の青紫を足す)
VOID = (6, 7, 14)
NEBULA_V = (74, 34, 122)        # 紫の星雲
NEBULA_T = (14, 74, 96)         # 青緑の星雲
NEBULA_R = (96, 24, 52)         # 深紅の星雲
STAR = (226, 230, 248)
MOON = (232, 236, 252)          # 主テキスト
MIST = (132, 142, 178)          # 副テキスト
DUSK = (74, 82, 112)            # 目盛り
GLASS = (16, 18, 32)            # パネル地
EDGE = (46, 52, 82)             # パネル枠

AURUM = (240, 202, 106)         # ENTRY
JADE = (64, 224, 186)           # TP / 含み益
EMBER = (255, 96, 104)          # SL / 含み損
VIOLET = (176, 132, 255)        # RUNNER(TP2)


# ---------------------------------------------------------------- 導出

def derive_open(position: Dict[str, Any], plan: Dict[str, Any],
                last_price: float, accounts: int = 1) -> Dict[str, Any]:
    """含み損益と各水準の到達時損益を、実価格だけから導出する。"""
    side = str(position.get("side") or plan.get("side") or "LONG").upper()
    direction = -1.0 if side in {"SHORT", "SELL"} else 1.0
    entry = float(position.get("avgEntry") if position.get("avgEntry") is not None
                  else plan["entry"])
    qty = int(position.get("qty") or plan.get("qty") or 2)
    stop = float(plan["stop"])

    legs: List[Dict[str, Any]] = []
    raw_legs = plan.get("legs")
    if isinstance(raw_legs, list) and raw_legs:
        for leg in raw_legs:
            target = _num(leg.get("target"))
            leg_qty = int(leg.get("qty") or 1)
            if target is None:
                continue
            legs.append({"id": str(leg.get("id") or "LEG"), "qty": leg_qty, "target": target})
    else:
        for index, target in enumerate(plan.get("targets") or []):
            value = _num(target)
            if value is None:
                continue
            legs.append({"id": "TP1" if index == 0 else "RUNNER", "qty": 1, "target": value})

    pts = (last_price - entry) * direction
    unreal_each = pts * POINT_VALUE * qty
    risk = abs(entry - stop)

    rungs: List[Dict[str, Any]] = []
    for leg in legs:
        leg_pts = (leg["target"] - entry) * direction
        rungs.append({
            "id": leg["id"],
            "price": leg["target"],
            "qty": leg["qty"],
            "pts": leg_pts,
            "r": (leg_pts / risk) if risk else 0.0,
            "each": leg_pts * POINT_VALUE * leg["qty"],
            "total": leg_pts * POINT_VALUE * leg["qty"] * accounts,
            "away": (leg["target"] - last_price) * direction,
            "kind": "target",
        })
    stop_pts = (stop - entry) * direction
    rungs.append({
        "id": "STOP",
        "price": stop,
        "qty": qty,
        "pts": stop_pts,
        "r": (stop_pts / risk) if risk else 0.0,
        "each": stop_pts * POINT_VALUE * qty,
        "total": stop_pts * POINT_VALUE * qty * accounts,
        "away": (last_price - stop) * direction,
        "kind": "stop",
    })

    # 両脚が抜けたときに口座全体で残る金額。片脚ずつの数字と混同しないよう、
    # 表示はフッターの1行だけに置く。
    banked_all = sum(r["total"] for r in rungs if r["kind"] == "target")
    opened = None
    for key in ("filledAt", "openedAt"):
        raw = position.get(key) or plan.get(key)
        if raw:
            try:
                opened = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
                break
            except (TypeError, ValueError):
                continue
    held = ""
    if opened is not None:
        secs = max(0.0, (datetime.now(timezone.utc) - opened).total_seconds())
        held = (f"{int(secs // 3600)}h {int(secs % 3600 // 60):02d}m" if secs >= 3600
                else f"{int(secs // 60)}m {int(secs % 60):02d}s")
    return {
        "dir": direction, "side": "LONG" if direction > 0 else "SHORT",
        "entry": entry, "stop": stop, "qty": qty, "accounts": accounts,
        "last": last_price, "pts": pts,
        "unrealEach": unreal_each, "unrealTotal": unreal_each * accounts,
        "r": (pts / risk) if risk else 0.0, "risk": risk,
        "bankedAll": banked_all,
        "rungs": rungs, "openedAt": opened, "held": held,
    }


def window_bars(bars: Sequence[Dict[str, Any]], derived: Dict[str, Any],
                lead: int = 14, limit: int = 70) -> List[Dict[str, float]]:
    """建玉時刻の少し前から最新の確定足まで。形成中は呼び出し側が落とす。"""
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
    if not rows or derived["openedAt"] is None:
        return rows[-limit:]
    start = derived["openedAt"].timestamp()
    inside = [i for i, r in enumerate(rows) if r["t"] >= start]
    if not inside:
        return rows[-limit:]
    first = max(0, inside[0] - lead)
    picked = rows[first:]
    return picked[-limit:] if len(picked) > limit else picked


def _fmt(value: float) -> str:
    return f"{value:,.2f}"


def _money(value: float) -> str:
    return ("−" if value < 0 else "+") + f"${abs(value):,.2f}"


def _pts(value: float) -> str:
    return ("−" if value < 0 else "+") + f"{abs(value):,.2f}pt"


# ---------------------------------------------------------------- 背景

def _radial(size: Tuple[int, int], cx: float, cy: float, radius: float,
            colour: Tuple[int, int, int], strength: float) -> Image.Image:
    """1つの星雲。粗く描いてから拡大+ぼかす —— 画素ごとに計算すると遅い。"""
    w, h = size
    small = max(24, int(min(w, h) / 12))
    layer = Image.new("L", (small, small), 0)
    px = layer.load()
    sx, sy = cx / w * small, cy / h * small
    sr = max(1.0, radius / w * small)
    for y in range(small):
        for x in range(small):
            dist = math.hypot(x - sx, y - sy) / sr
            if dist >= 1.0:
                continue
            fall = (1.0 - dist) ** 2.2
            px[x, y] = int(255 * fall * strength)
    layer = layer.resize((w, h), Image.BICUBIC).filter(ImageFilter.GaussianBlur(w / 22))
    tint = Image.new("RGB", (w, h), colour)
    out = Image.new("RGB", (w, h), (0, 0, 0))
    out.paste(tint, (0, 0), layer)
    return out


def _backdrop(seed: int) -> Image.Image:
    """夜空。縦グラデーション + 星雲 + 星。seed は建値から作るので毎回同じ絵。"""
    img = Image.new("RGB", (W, H), VOID)
    d = ImageDraw.Draw(img)
    # 縦グラデーション: 上ほど藍、下は無。
    for y in range(H):
        k = (1.0 - y / H) ** 1.7
        d.line([(0, y), (W, y)],
               fill=(int(VOID[0] + 16 * k), int(VOID[1] + 17 * k), int(VOID[2] + 34 * k)))

    for cx, cy, rad, colour, strength in (
        (W * 0.18, H * 0.14, W * 0.62, NEBULA_V, 0.55),
        (W * 0.86, H * 0.34, W * 0.55, NEBULA_T, 0.42),
        (W * 0.52, H * 0.94, W * 0.70, NEBULA_R, 0.34),
    ):
        img = ImageChops.screen(img, _radial((W, H), cx, cy, rad, colour, strength))

    # 星。決定論的に散らす(同じ建玉なら何度描いても同じ空)。
    rng = random.Random(seed)
    stars = Image.new("RGB", (W, H), (0, 0, 0))
    sd = ImageDraw.Draw(stars)
    for _ in range(460):
        x, y = rng.uniform(0, W), rng.uniform(0, H)
        mag = rng.random() ** 3.0
        r = 0.5 + mag * 1.7
        v = int(40 + mag * 215)
        sd.ellipse((x - r, y - r, x + r, y + r), fill=(v, v, min(255, int(v * 1.06))))
    halo = stars.filter(ImageFilter.GaussianBlur(2.2))
    img = ImageChops.screen(img, halo)
    img = ImageChops.screen(img, stars)
    return img


def _glow(base: Image.Image, layer: Image.Image, blur: float) -> Image.Image:
    """発光の合成。加算(screen)なので暗い背景の上でだけ光る。"""
    return ImageChops.screen(base, layer.filter(ImageFilter.GaussianBlur(blur)))


# ---------------------------------------------------------------- 描画

def render_png(position: Dict[str, Any], plan: Dict[str, Any], last_price: float,
               bars: Optional[Sequence[Dict[str, Any]]] = None,
               accounts: int = 1, note: Optional[str] = None) -> bytes:
    derived = derive_open(position, plan, last_price, accounts=accounts)
    plot = window_bars(bars or [], derived)
    up = derived["unrealTotal"] >= 0
    tint = JADE if up else EMBER

    img = _backdrop(seed=int(derived["entry"] * 4) & 0xFFFF).convert("RGB")
    glow = Image.new("RGB", (W, H), (0, 0, 0))     # 発光だけを溜める層
    gd = ImageDraw.Draw(glow)
    d = ImageDraw.Draw(img)

    f_brand = _font(30, bold=True)
    f_title = _font(50, bold=True)
    f_head = _font(21)
    f_axis = _font(16)
    f_tag = _font(17, bold=True)
    f_pill = _font(84, bold=True)
    f_sub = _font(21)
    f_rung = _font(24, bold=True)
    f_cell = _font(21)
    f_small = _font(15)
    f_jcell = _jfont(20)
    f_jsmall = _jfont(15)

    entry, stop, side = derived["entry"], derived["stop"], derived["side"]
    symbol = str(position.get("symbol") or plan.get("symbol") or "MNQU6")

    # ---- ヘッダ
    d.text((PAD, 46), "NIGHTWATCH", font=f_brand, fill=MOON)
    live = "OPEN POSITION"
    lw = d.textlength(live, font=f_brand)
    d.text((W - PAD - lw, 46), live, font=f_brand, fill=tint)
    gd.text((W - PAD - lw, 46), live, font=f_brand, fill=tint)

    d.text((PAD, 92), f"{symbol} · 3m · {side} {derived['qty']}×{accounts}   ·   "
                      f"avg {_fmt(entry)}", font=f_head, fill=MIST)

    # ---- 含み損益(いちばん大きい字。まだ確定していないので「UNREALIZED」と明示)
    pill = _money(derived["unrealTotal"])
    d.text((PAD, 132), pill, font=f_pill, fill=tint)
    gd.text((PAD, 132), pill, font=f_pill, fill=tint)
    pw = d.textlength(pill, font=f_pill)
    d.text((PAD + pw + 22, 158), "UNREALIZED", font=f_tag, fill=MIST)
    d.text((PAD + pw + 22, 186),
           f"{_pts(derived['pts'])}  ·  {derived['r']:+.2f}R  ·  "
           f"{_money(derived['unrealEach'])}/acct", font=f_small, fill=DUSK)
    if derived["held"]:
        hw = d.textlength(derived["held"], font=f_sub)
        d.text((W - PAD - hw, 196), derived["held"], font=f_sub, fill=MIST)
        d.text((W - PAD - d.textlength("HELD", font=f_small), 172), "HELD",
               font=f_small, fill=DUSK)

    # ---- チャート面
    x0, y0 = PAD, CHART_TOP
    x1, y1 = W - PAD, CHART_TOP + CHART_H
    d.rounded_rectangle((x0, y0, x1, y1), radius=16, fill=GLASS, outline=EDGE)
    plot_x0, plot_x1 = x0 + 4, x1 - PRICE_LANE
    plot_y0, plot_y1 = y0 + 18, y1 - TIME_LANE
    plot_w, plot_h = plot_x1 - plot_x0, plot_y1 - plot_y0

    # スケール: 実際に起きた価格 + ENTRY/SL は必ず入れる。届いていない TP は
    # 収まるときだけ線を描く(縦に潰れて値動きが読めなくなるのを防ぐ)。
    lo = min([b["l"] for b in plot] + [entry, stop]) if plot else min(entry, stop)
    hi = max([b["h"] for b in plot] + [entry, stop]) if plot else max(entry, stop)
    near = [r["price"] for r in derived["rungs"]
            if r["kind"] == "target" and lo - (hi - lo) * 0.6 <= r["price"] <= hi + (hi - lo) * 0.6]
    if near:
        lo, hi = min(lo, min(near)), max(hi, max(near))
    if hi - lo < 1.0:
        lo, hi = lo - 4, hi + 4
    pad_rng = (hi - lo) * 0.10
    lo, hi = lo - pad_rng, hi + pad_rng
    rng = hi - lo

    def py(price: float) -> float:
        return plot_y1 - (price - lo) / rng * plot_h

    # 目盛り。値札(beam の右端タグ)と重なる高さでは数字を出さない ——
    # 重ねると水準の価格が読めなくなる。
    tagged = [r["price"] for r in derived["rungs"]] + [entry]
    step = nice_step(rng, plot_h)
    tick = math.floor(lo / step) * step
    while tick <= hi:
        if lo <= tick <= hi:
            yy = py(tick)
            d.line([(plot_x0, yy), (plot_x1, yy)], fill=EDGE)
            if all(abs(py(p) - yy) > 30 for p in tagged if lo <= p <= hi):
                d.text((plot_x1 + 12, yy - 9), f"{tick:,.0f}", font=f_axis, fill=DUSK)
        tick += step

    # ローソク。発光層にも同じ形を落として、暗い夜空の上で灯って見せる。
    if plot:
        slot = plot_w / max(1, len(plot))
        body = max(2.0, min(11.0, slot * 0.58))
        for index, bar in enumerate(plot):
            cx = plot_x0 + slot * (index + 0.5)
            rising = bar["c"] >= bar["o"]
            colour = JADE if rising else EMBER
            d.line([(cx, py(bar["h"])), (cx, py(bar["l"]))], fill=colour, width=1)
            top, bottom = py(max(bar["o"], bar["c"])), py(min(bar["o"], bar["c"]))
            if bottom - top < 1.5:
                bottom = top + 1.5
            d.rectangle((cx - body / 2, top, cx + body / 2, bottom), fill=colour)
            gd.rectangle((cx - body / 2, top, cx + body / 2, bottom), fill=colour)

    def beam(price: float, colour: Tuple[int, int, int], label: str,
             dashed: bool = False) -> None:
        """発光する水平線 + 右端の値札。範囲外なら何も描かない。"""
        if not (lo <= price <= hi):
            return
        yy = py(price)
        if dashed:
            x = plot_x0
            while x < plot_x1:
                d.line([(x, yy), (min(x + 11, plot_x1), yy)], fill=colour, width=2)
                gd.line([(x, yy), (min(x + 11, plot_x1), yy)], fill=colour, width=2)
                x += 20
        else:
            d.line([(plot_x0, yy), (plot_x1, yy)], fill=colour, width=2)
            gd.line([(plot_x0, yy), (plot_x1, yy)], fill=colour, width=2)
        tag = f"{label} {_fmt(price)}"
        tw = d.textlength(tag, font=f_tag)
        bx0, bx1 = plot_x1 + 8, plot_x1 + 8 + tw + 14
        d.rounded_rectangle((bx0, yy - 14, min(bx1, x1 - 6), yy + 14), radius=7,
                            fill=GLASS, outline=colour)
        d.text((bx0 + 7, yy - 10), tag, font=f_tag, fill=colour)

    for rung in derived["rungs"]:
        if rung["kind"] == "target":
            beam(rung["price"], VIOLET if rung["id"] != "TP1" else JADE,
                 "TP1" if rung["id"] == "TP1" else "TP2", dashed=True)
    beam(stop, EMBER, "SL")
    beam(entry, AURUM, "ENTRY")

    # 現値。右端に光る点を置いて「いまここ」を示す。
    if lo <= last_price <= hi:
        yy = py(last_price)
        d.ellipse((plot_x1 - 7, yy - 7, plot_x1 + 7, yy + 7), fill=tint)
        gd.ellipse((plot_x1 - 11, yy - 11, plot_x1 + 11, yy + 11), fill=tint)
        d.text((plot_x0 + 10, yy - 26), f"LAST {_fmt(last_price)}", font=f_tag, fill=tint)

    if plot:
        first = datetime.fromtimestamp(plot[0]["t"], JST).strftime("%H:%M")
        final = datetime.fromtimestamp(plot[-1]["t"], JST).strftime("%H:%M")
        d.text((plot_x0 + 4, plot_y1 + 12), first, font=f_axis, fill=DUSK)
        fw = d.textlength(final, font=f_axis)
        d.text((plot_x1 - fw, plot_y1 + 12), final, font=f_axis, fill=DUSK)

    # ---- トラックバー: SL → ENTRY → TP1 → TP2 を **価格に比例して** 1本へ乗せる。
    # 3本の独立した進捗バーだと「いま全体のどこにいるか」が読めない。ここは
    # 左端が全損、右端が満額で、光点が現値。距離感がそのまま損益の距離になる。
    stop_rung = next(r for r in derived["rungs"] if r["kind"] == "stop")
    ladder = sorted((r for r in derived["rungs"] if r["kind"] == "target"),
                    key=lambda r: r["pts"])
    track_x0, track_x1 = PAD + 10, W - PAD - 10
    track_w = track_x1 - track_x0
    ty0 = y1 + 30
    rail_y = ty0 + 92
    far = ladder[-1]["price"] if ladder else None
    span = ((far - stop) * derived["dir"]) if far is not None else 0.0

    def tx(price: float) -> float:
        if span <= 0:
            return track_x0
        frac = (price - stop) * derived["dir"] / span
        return track_x0 + max(0.0, min(1.0, frac)) * track_w

    if span > 0:
        rail_top, rail_bot = rail_y - RAIL_H / 2, rail_y + RAIL_H / 2
        d.rounded_rectangle((track_x0, rail_top, track_x1, rail_bot),
                            radius=RAIL_H / 2, fill=GLASS, outline=EDGE)

        def zone(a: float, b: float, colour: Tuple[int, int, int], k: float,
                 lit: bool = False) -> None:
            if b - a < 1.5:
                return
            shade = tuple(int(c * k) for c in colour)
            d.rounded_rectangle((a, rail_top + 3, b, rail_bot - 3),
                                radius=(RAIL_H - 6) / 2, fill=shade)
            if lit:
                gd.rounded_rectangle((a, rail_top + 3, b, rail_bot - 3),
                                     radius=(RAIL_H - 6) / 2, fill=shade)

        # 地の色分け: 建値の左は失う側、右は取りに行く側。
        zone(tx(stop), tx(entry), EMBER, 0.26)
        previous = entry
        for rung in ladder:
            zone(tx(previous), tx(rung["price"]),
                 JADE if rung["id"] == "TP1" else VIOLET, 0.24)
            previous = rung["price"]
        # 建値から現値まで。ここだけ明るく塗るので、含み損益の向きが一目で出る。
        a, b = sorted((tx(entry), tx(last_price)))
        zone(a, b, tint, 0.66, lit=True)

        # 節の縦棒とラベル。近すぎるラベルは右へ押して重ねない。
        anchors = [(stop, EMBER, "SL", _money(stop_rung["total"])),
                   (entry, AURUM, "ENTRY", "BE")]
        for rung in ladder:
            anchors.append((rung["price"],
                            JADE if rung["id"] == "TP1" else VIOLET,
                            "TP1" if rung["id"] == "TP1" else "TP2",
                            _money(rung["total"])))
        anchors.sort(key=lambda a: tx(a[0]))

        cursor = track_x0
        for price, colour, name, money_text in anchors:
            x = tx(price)
            d.line([(x, rail_top - 9), (x, rail_bot + 9)], fill=colour, width=3)
            gd.line([(x, rail_top - 9), (x, rail_bot + 9)], fill=colour, width=3)
            head = f"{name} {_fmt(price)}"
            hw = d.textlength(head, font=f_tag)
            mw = d.textlength(money_text, font=f_rung)
            slot = max(hw, mw)
            left = max(cursor, min(x - slot / 2, track_x1 - slot))
            cursor = left + slot + 16
            d.text((left + (slot - hw) / 2, rail_top - 40), head, font=f_tag, fill=colour)
            d.text((left + (slot - mw) / 2, rail_bot + 16), money_text,
                   font=f_rung, fill=colour)
            gd.text((left + (slot - mw) / 2, rail_bot + 16), money_text,
                    font=f_rung, fill=colour)

        # 現値の光点。レールの上に乗せるので、節より必ず前面になる。
        xl = tx(last_price)
        d.ellipse((xl - 10, rail_y - 10, xl + 10, rail_y + 10), fill=tint,
                  outline=VOID, width=2)
        gd.ellipse((xl - 14, rail_y - 14, xl + 14, rail_y + 14), fill=tint)
        now = f"LAST {_fmt(last_price)}  {_money(derived['unrealTotal'])}"
        nw = d.textlength(now, font=f_tag)
        nx = max(track_x0, min(xl - nw / 2, track_x1 - nw))
        d.text((nx, ty0 + 4), now, font=f_tag, fill=tint)
        gd.text((nx, ty0 + 4), now, font=f_tag, fill=tint)

    # ---- ラダーバー(TP2 → TP1 → SL の順。上が利、下が損)
    rows = [r for r in derived["rungs"] if r["kind"] == "target"]
    rows.sort(key=lambda r: -r["pts"])
    rows.append(stop_rung)

    top = ty0 + TRACK_H + 26
    bar_h = ROW_H
    gap = ROW_GAP
    for index, rung in enumerate(rows):
        ry0 = top + index * (bar_h + gap)
        ry1 = ry0 + bar_h
        good = rung["pts"] >= 0
        colour = (JADE if rung["id"] == "TP1" else VIOLET) if good else EMBER
        label = {"TP1": "TP1", "RUNNER": "TP2 · RUNNER", "STOP": "SL"}.get(
            rung["id"], rung["id"])

        d.rounded_rectangle((PAD, ry0, W - PAD, ry1), radius=14,
                            fill=GLASS, outline=EDGE)

        # 進捗: ENTRY から その水準 までの何割まで来ているか。
        span = abs(rung["price"] - entry)
        moved = (last_price - entry) * derived["dir"]
        toward = moved if good else -moved
        frac = 0.0 if span <= 0 else max(0.0, min(1.0, toward / span))
        inner_x0, inner_x1 = PAD + 2, W - PAD - 2
        fill_w = (inner_x1 - inner_x0) * frac
        if fill_w > 4:
            shade = tuple(int(c * 0.30) for c in colour)
            d.rounded_rectangle((inner_x0, ry0 + 2, inner_x0 + fill_w, ry1 - 2),
                                radius=12, fill=shade)
            d.line([(inner_x0 + fill_w, ry0 + 6), (inner_x0 + fill_w, ry1 - 6)],
                   fill=colour, width=2)
            gd.line([(inner_x0 + fill_w, ry0 + 6), (inner_x0 + fill_w, ry1 - 6)],
                    fill=colour, width=2)

        d.text((PAD + 22, ry0 + 17), label, font=f_rung, fill=colour)
        lw = d.textlength(label, font=f_rung)
        d.text((PAD + 22 + lw + 16, ry0 + 21), f"{rung['r']:+.2f}R",
               font=f_cell, fill=DUSK)
        price_txt = f"{_fmt(rung['price'])}   "
        d.text((PAD + 22, ry0 + 54), price_txt, font=f_cell, fill=MIST)
        d.text((PAD + 22 + d.textlength(price_txt, font=f_cell), ry0 + 55),
               f"{rung['qty']}枚 × {accounts}口座", font=f_jcell, fill=MIST)

        money = _money(rung["total"])
        mw = d.textlength(money, font=f_rung)
        d.text((W - PAD - 22 - mw, ry0 + 15), money, font=f_rung, fill=colour)
        gd.text((W - PAD - 22 - mw, ry0 + 15), money, font=f_rung, fill=colour)

        away = f"{abs(rung['away']):,.2f}pt away   ·   {_money(rung['each'])}/acct"
        aw = d.textlength(away, font=f_cell)
        d.text((W - PAD - 22 - aw, ry0 + 56), away, font=f_cell, fill=MIST)

    # ---- フッタ
    fy = top + len(rows) * (bar_h + gap) + 10
    d.line([(PAD, fy), (W - PAD, fy)], fill=EDGE)
    risk_total = derived["risk"] * POINT_VALUE * derived["qty"] * accounts
    left = f"risk {derived['risk']:,.2f}pt = {_money(-risk_total)} total   ·   "
    d.text((PAD, fy + 16), left, font=f_small, fill=DUSK)
    d.text((PAD + d.textlength(left, font=f_small), fy + 16),
           f"1pt = ${POINT_VALUE:.2f}/枚", font=f_jsmall, fill=DUSK)
    # 上の行は片脚ずつの金額なので、両脚が抜けた場合の合計はここに1回だけ書く。
    if len(rows) > 2:
        both = f"TP1 + TP2 both filled = {_money(derived['bankedAll'])} total"
        d.text((PAD, fy + 42), both, font=f_small, fill=MIST)
    if note:
        nw = d.textlength(note, font=f_small)
        d.text((W - PAD - nw, fy + 16), note, font=f_small, fill=DUSK)

    img = _glow(img, glow, blur=13)
    img = _glow(img, glow, blur=3)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def caption(derived: Dict[str, Any], symbol: str = "MNQU6") -> str:
    """写真に添える1〜3行(HTML)。数字は derive_open が出したものだけを使う。"""
    head = (f"<b>{derived['side']} {derived['qty']}×{derived['accounts']} "
            f"{symbol}</b>  {_money(derived['unrealTotal'])} unrealized")
    legs = []
    for rung in sorted((r for r in derived["rungs"] if r["kind"] == "target"),
                       key=lambda r: r["pts"]):
        legs.append(f"{'TP1' if rung['id'] == 'TP1' else 'TP2'} "
                    f"{_fmt(rung['price'])} {_money(rung['total'])}")
    stop = next(r for r in derived["rungs"] if r["kind"] == "stop")
    legs.append(f"SL {_fmt(stop['price'])} {_money(stop['total'])}")
    return (f"{head}\nE {_fmt(derived['entry'])} · "
            + " · ".join(legs)
            + f"\n{_pts(derived['pts'])} · {derived['r']:+.2f}R"
            + (f" · held {derived['held']}" if derived["held"] else ""))


# ---------------------------------------------------------------- 収集

class NoCard(Exception):
    """カードを描く条件が揃っていない。**理由を必ず持つ**(黙って諦めない)。

    「建玉が無い」と「照会できなかった」は別の意味なので、呼び出し側が
    区別できるよう理由文字列をそのまま渡す。
    """


def _repo() -> Path:
    return Path(__file__).resolve().parent


def load_plan(ledger: Optional[Path] = None) -> Dict[str, Any]:
    """台帳の最後の ENTRY プランを読む。**推測で水準を作らない。**"""
    path = ledger or (_repo() / ".secrets" / "autotrade_ledger.jsonl")
    latest: Optional[Dict[str, Any]] = None
    with io.open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            plan = row.get("plan")
            if isinstance(plan, dict) and _num(plan.get("entry")) is not None:
                latest = plan
    if latest is None:
        raise SystemExit("台帳に ENTRY プランが無い")
    stop = _num(latest.get("initialStop"))
    if stop is None:
        stop = _num(latest.get("stop"))
    if stop is None:
        raise SystemExit("プランに SL が無い")
    return {**latest, "stop": stop}


def load_bars(path: Optional[Path] = None) -> List[Dict[str, Any]]:
    """3分足の生 raw。最終足は形成中なので落とす。"""
    target = path or (_repo() / ".secrets" / "tv_raw" / "bars3m.json")
    payload = json.loads(io.open(target, encoding="utf-8").read())
    rows = payload.get("bars") if isinstance(payload, dict) else payload
    return list(rows or [])[:-1]


def load_quote(path: Optional[Path] = None) -> float:
    target = path or (_repo() / ".secrets" / "tv_raw" / "quote.json")
    payload = json.loads(io.open(target, encoding="utf-8").read())
    price = _num(payload.get("last") if isinstance(payload, dict) else None)
    if price is None:
        raise SystemExit("quote.json に last が無い")
    return price


def resolve_accounts(explicit: Optional[Sequence[str]] = None) -> List[str]:
    """発注先と **同じ解決経路** で口座を出す。

    ここで別の集合を組み立てると「照会していない口座の建玉」を見落とす。
    """
    accounts = [str(a) for a in (explicit or []) if str(a).strip()]
    if accounts:
        return accounts
    import broker_status

    for adapter in broker_status.adapters():
        found = list(getattr(adapter, "accounts", None) or [])
        if found:
            return [str(a) for a in found]
    raw = os.environ.get("CROSSTRADE_ACCOUNTS") or ""
    return [a.strip() for a in raw.replace(";", ",").split(",") if a.strip()]


def collect(accounts: Optional[Sequence[str]] = None,
            last: Optional[float] = None) -> Dict[str, Any]:
    """カードに要る材料を集める。**照会も読み込みも失敗を黙らせない。**

    描けない理由(建玉が無い / UNVERIFIED / プランが無い)は `NoCard` に載せて
    返す。呼び出し側はそれをそのままログや通知に出せる。
    """
    import broker_status

    resolved = resolve_accounts(accounts)
    if not resolved:
        raise NoCard("口座が特定できない")

    held: List[Dict[str, Any]] = []
    for account in resolved:
        try:
            row = broker_status.query_position(account=account)
        except Exception as exc:  # noqa: BLE001 - 照会不能を「建玉なし」にしない
            raise NoCard(f"建玉照会に失敗: {type(exc).__name__}: {exc}") from exc
        if not row.get("verified"):
            raise NoCard(f"{account}: 建玉が UNVERIFIED —— カードを描かない")
        if int(row.get("qty") or 0) > 0:
            held.append(row)
    if not held:
        raise NoCard("保有中の建玉が無い")

    entries = {round(float(r["avgEntry"]), 4) for r in held
               if r.get("avgEntry") is not None}
    if len(entries) > 1:
        raise NoCard(f"口座ごとに建値が違う: {sorted(entries)} —— 1枚のカードにしない")

    try:
        plan = load_plan()
    except SystemExit as exc:
        raise NoCard(str(exc)) from exc
    try:
        price = float(last) if last is not None else load_quote()
        bars = load_bars()
    except (SystemExit, OSError, ValueError, json.JSONDecodeError) as exc:
        raise NoCard(f"現値/確定足を読めない: {exc}") from exc

    return {"position": held[0], "plan": plan, "last": price, "bars": bars,
            "accounts": len(held), "held": held}


def render_open_card(accounts: Optional[Sequence[str]] = None,
                     last: Optional[float] = None,
                     note: Optional[str] = None) -> Tuple[bytes, str, Dict[str, Any]]:
    """(PNG, キャプション, 導出値) を返す。保有していなければ `NoCard`。"""
    parts = collect(accounts=accounts, last=last)
    derived = derive_open(parts["position"], parts["plan"], parts["last"],
                          accounts=parts["accounts"])
    png = render_png(parts["position"], parts["plan"], parts["last"],
                     bars=parts["bars"], accounts=parts["accounts"], note=note)
    symbol = str(parts["position"].get("symbol")
                 or parts["plan"].get("symbol") or "MNQU6")
    return png, caption(derived, symbol=symbol), derived


def card_fingerprint(derived: Dict[str, Any]) -> str:
    """「同じ絵を送り直すか」の判定キー。

    含み損益は3分ごとに動くので **入れない**。建玉の向き・枚数・口座数と、
    建値/SL/各TP という *契約側* が変わったときだけ違う値になる。これで
    「新規約定した」「TP1 が抜けて runner になった」「SL を建値へ寄せた」の
    3つだけが再送に化ける。
    """
    legs = ";".join(f"{r['id']}@{r['price']:.2f}x{r['qty']}"
                    for r in sorted(derived["rungs"], key=lambda r: str(r["id"])))
    return (f"{derived['side']}/{derived['qty']}/{derived['accounts']}/"
            f"{derived['entry']:.2f}/{derived['stop']:.2f}/{legs}")


def demo_parts() -> Dict[str, Any]:
    """実確定足の上に作り物の建玉を置く。**発注にも台帳にも触れない。**

    見た目を確認したいだけのときに、建玉が無くてもカードを出せるようにする。
    数字が実在すると誤解されないよう、note に必ず DEMO を出す。
    """
    bars = load_bars()
    if len(bars) < 30:
        raise NoCard("確定足が足りない(bars3m.json)")
    entry = float(bars[-18]["close"])
    tick = round(entry * 4) / 4
    plan = {
        "symbol": "MNQU6", "side": "SELL", "qty": 2,
        "entry": tick, "stop": tick + 24.0,
        "targets": [tick - 26.0, tick - 52.0],
        "legs": [{"id": "TP1", "qty": 1, "target": tick - 26.0},
                 {"id": "RUNNER", "qty": 1, "target": tick - 52.0}],
    }
    opened = datetime.fromtimestamp(float(bars[-18]["time"]), timezone.utc).isoformat()
    position = {"symbol": "MNQU6", "side": "SHORT", "qty": 2,
                "avgEntry": tick, "filledAt": opened}
    return {"position": position, "plan": plan, "last": float(bars[-1]["close"]),
            "bars": bars, "accounts": 2, "held": [position]}


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="保有中ポジションのカードを描く")
    parser.add_argument("--out", default=str(_repo() / ".secrets" / "position_card.png"))
    parser.add_argument("--account", action="append", default=None,
                        help="照会する口座(省略時は CROSSTRADE_ACCOUNTS 全部)")
    parser.add_argument("--last", type=float, default=None,
                        help="現値(省略時は tv_raw/quote.json)")
    parser.add_argument("--note", default=None)
    parser.add_argument("--demo", action="store_true",
                        help="建玉が無くても作り物の値で描く(見た目の確認用)")
    parser.add_argument("--send", action="store_true",
                        help="描いた PNG を Telegram へ送る")
    args = parser.parse_args(list(argv) if argv is not None else None)

    try:
        if args.demo:
            parts = demo_parts()
            note = args.note or "DEMO · not a real position"
        else:
            parts = collect(accounts=args.account, last=args.last)
            note = args.note
    except NoCard as exc:
        print(str(exc))
        return 1

    derived = derive_open(parts["position"], parts["plan"], parts["last"],
                          accounts=parts["accounts"])
    png = render_png(parts["position"], parts["plan"], parts["last"],
                     bars=parts["bars"], accounts=parts["accounts"], note=note)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(png)
    print(f"{out}  ({len(png):,} bytes)  accounts={parts['accounts']}  "
          f"last={parts['last']}")

    if args.send:
        import telegram_bot

        symbol = str(parts["position"].get("symbol") or "MNQU6")
        text = caption(derived, symbol=symbol)
        if args.demo:
            text = "DEMO — 実建玉ではありません\n" + text
        sent = telegram_bot.send_photo(telegram_bot.load_env(), png, caption=text)
        print("sent" if sent else "send failed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

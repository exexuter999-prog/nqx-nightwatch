#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Deterministic strategy observables extracted from the supplied image set.

The image deck is treated as a model catalogue, not as proof of an edge.  This
module turns the catalogue into small, auditable observations that can be
attached to a Nightwatch scenario.  Every result is explicitly advisory and
keeps the existing MSNR/CVD/risk gates authoritative.
"""

from __future__ import annotations

import math
from typing import Any, Dict, Iterable, List, Optional


CATALOG_VERSION = "IMAGE_STRATEGY_CATALOG/1"

#: アンカーが「まだ有効」と言える freshness の語彙。
#:
#: R37: ここは 3 箇所に `{"FRESH", "WICK_TESTED", "BODY_TESTED"}` と直書きされて
#: おり、**`ACTIVE` を欠いていた**。一方 `msnr_gate.derive_range_anchor` は
#: 導出レンジに必ず `ACTIVE` を刻み、`resolve_range_anchor` は `{FRESH, ACTIVE}` を
#: 受理する。つまり同じアンカーが「OTE には使えるが Fib 整合には使えない」という
#: 不整合が起きていた。実測ではレンジが取れた 236 本すべてが
#: `FIB_ANCHOR_OR_FRESHNESS_MISSING` で落ち、**うち 58 本は価格が実際に OTE 帯の
#: 中にあった**。綴りの違いだけで有効な合流を捨てていたことになる。
#: `msnr_gate` 側の受理集合と一致させること —— 片方だけ変えないこと。
USABLE_ANCHOR_FRESHNESS = frozenset({"FRESH", "ACTIVE", "WICK_TESTED", "BODY_TESTED"})
DETECTOR_VERSION = "REPO2-DETECTOR/1"
MNQ_TICK = 0.25
FIB_SD_RATIOS = (-5.0, -4.0, -3.0, -2.5, -2.0, -1.0, 0.0, 1.0)


def _num(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def normalize_bars(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, float]]:
    out: List[Dict[str, float]] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        o = _num(row.get("o", row.get("open")))
        h = _num(row.get("h", row.get("high")))
        l = _num(row.get("l", row.get("low")))
        c = _num(row.get("c", row.get("close")))
        t = _num(row.get("t", row.get("time")))
        if None in (o, h, l, c) or h < l or h < max(o, c) or l > min(o, c):
            continue
        out.append({"o": o, "h": h, "l": l, "c": c, "t": t})
    return out


def _atr(bars: List[Dict[str, float]], length: int = 14) -> Optional[float]:
    if len(bars) < 2:
        return None
    sample = bars[-length:]
    ranges = [b["h"] - b["l"] for b in sample if b["h"] >= b["l"]]
    return sum(ranges) / len(ranges) if ranges else None


def _ema(bars: List[Dict[str, float]], length: int = 20) -> Optional[float]:
    closes = [b["c"] for b in bars if _num(b.get("c")) is not None]
    if not closes:
        return None
    alpha = 2.0 / (length + 1.0)
    value = closes[0]
    for close in closes[1:]:
        value = alpha * close + (1.0 - alpha) * value
    return value


def _direction(close: float, open_: float, epsilon: float = 0.25) -> Optional[str]:
    if close > open_ + epsilon:
        return "BUY"
    if close < open_ - epsilon:
        return "SELL"
    return None


def detect_crt(bars: List[Dict[str, float]]) -> Dict[str, Any]:
    """Candle Range Theory: boundary sweep followed by a close back inside."""
    if len(bars) < 3:
        return {"status": "MISSING", "valid": False, "type": "CRT", "direction": None,
                "evidence": ["NEED_3_CONFIRMED_BARS"]}
    parent, inside, trigger = bars[-3], bars[-2], bars[-1]
    hi, lo = parent["h"], parent["l"]
    inside_bar = inside["h"] <= hi and inside["l"] >= lo
    sweep_up = trigger["h"] > hi and trigger["c"] < hi
    sweep_down = trigger["l"] < lo and trigger["c"] > lo
    if not sweep_up and not sweep_down:
        # A classic CRT can use the previous two-bar range when there is no
        # nested inside bar.  This keeps the detector useful on rolling feeds.
        hi = max(parent["h"], inside["h"])
        lo = min(parent["l"], inside["l"])
        sweep_up = trigger["h"] > hi and trigger["c"] < hi
        sweep_down = trigger["l"] < lo and trigger["c"] > lo
    # 両側を刈って内側へ戻った足は Double Purge(カタログ §5 の独立型)。
    # 確定 OHLC からは**どちらを先に刈ったか決められない**ので方向を作らない。
    # 以前は `"SELL" if sweep_up else "BUY" if sweep_down` の評価順により、
    # 上下同時パージが必ず SELL に潰れ、出力が「上のみパージ」と区別できなかった。
    double_purge = sweep_up and sweep_down
    direction = None if double_purge else ("SELL" if sweep_up else "BUY" if sweep_down else None)
    valid = direction is not None
    return {
        "status": "CONFIRMED" if valid else "WATCH",
        "valid": valid,
        "type": ("DOUBLE_PURGE_CRT" if double_purge
                 else "INSIDE_BAR_CRT" if inside_bar else "CLASSIC_CRT"),
        "stage": "TWO_STAGE" if inside_bar and valid else "SINGLE_STAGE",
        "direction": direction,
        "rangeHigh": round(hi, 2), "rangeLow": round(lo, 2),
        "sweep": ("BOTH" if double_purge
                  else "BSL" if sweep_up else "SSL" if sweep_down else None),
        "doublePurge": double_purge,
        "closeBackInside": valid,
        "failureCondition": "body_close_outside_range",
        "evidence": (["CRT_RANGE", "DOUBLE_PURGE", "SWEEP_SEQUENCE_UNKNOWN"] if double_purge
                     else ["CRT_RANGE", "TWS_SWEEP", "CLOSE_BACK_INSIDE"] if valid
                     else ["CRT_RANGE", "WAIT_SWEEP_AND_CLOSE"]),
    }


def classify_amd(bars: List[Dict[str, float]]) -> Dict[str, Any]:
    """Wyckoff/AMD (Power of Three) phase label from confirmed OHLC only."""
    if len(bars) < 6:
        return {"phase": "UNKNOWN", "valid": False, "direction": None,
                "evidence": ["NEED_6_CONFIRMED_BARS"]}
    base = bars[-6:-3]
    recent = bars[-3:]
    hi = max(b["h"] for b in base)
    lo = min(b["l"] for b in base)
    last = recent[-1]
    raid_up = last["h"] > hi and last["c"] < hi
    raid_down = last["l"] < lo and last["c"] > lo
    close_up = last["c"] > hi
    close_down = last["c"] < lo
    direction = "SELL" if raid_up else "BUY" if raid_down else None
    if close_up or close_down:
        phase = "DISTRIBUTION"
        direction = "BUY" if close_up else "SELL"
    elif raid_up or raid_down:
        phase = "MANIPULATION"
    else:
        phase = "ACCUMULATION"
    return {
        "phase": phase,
        # R37: `status` キーが無いため `_repo2_lifecycle` が lifecycle=UNKNOWN を付け、
        # 算出済みの direction ごと valid=False へ上書きしていた。カタログ §4 の
        # Wyckoff/AMD 章がこれ一つで丸ごと無効化されていた。
        "status": "CONFIRMED" if direction else "WATCH",
        "valid": phase != "UNKNOWN" and direction is not None,
        "direction": direction,
        "failureCondition": "close_returns_through_raided_boundary",
        "rangeHigh": round(hi, 2), "rangeLow": round(lo, 2),
        "manipulation": "BSL_RAID" if raid_up else "SSL_RAID" if raid_down else None,
        "distribution": "UP" if close_up else "DOWN" if close_down else None,
        "evidence": ["PO3_ACCUMULATION", phase, "LIQUIDITY_RAID" if (raid_up or raid_down) else "RANGE"],
    }


def detect_liquidity(bars: List[Dict[str, float]], levels: Iterable[Dict[str, Any]],
                     price: Optional[float], tolerance: float = 2.0) -> Dict[str, Any]:
    if not bars:
        return {"status": "MISSING", "pools": [], "dol": {"BUY": None, "SELL": None}}
    pools: List[Dict[str, Any]] = []
    for i in range(1, len(bars) - 1):
        if bars[i]["h"] >= bars[i - 1]["h"] and bars[i]["h"] >= bars[i + 1]["h"]:
            pools.append({"side": "BSL", "price": round(bars[i]["h"], 2), "kind": "SWING_HIGH"})
        if bars[i]["l"] <= bars[i - 1]["l"] and bars[i]["l"] <= bars[i + 1]["l"]:
            pools.append({"side": "SSL", "price": round(bars[i]["l"], 2), "kind": "SWING_LOW"})
    for level in levels or []:
        lp = _num(level.get("price")) if isinstance(level, dict) else None
        if lp is None:
            continue
        label = str(level.get("label", level.get("name", "LEVEL")))
        side = "BSL" if lp > (price or lp) else "SSL"
        pools.append({"side": side, "price": round(lp, 2), "kind": "NAMED_LEVEL", "label": label})
    bsl = sorted({p["price"] for p in pools if p["side"] == "BSL" and (price is None or p["price"] > price)})
    ssl = sorted({p["price"] for p in pools if p["side"] == "SSL" and (price is None or p["price"] < price)}, reverse=True)
    dol = {"BUY": bsl[0] if bsl else None, "SELL": ssl[0] if ssl else None}
    return {"status": "OBSERVED" if pools else "MISSING", "pools": pools[-12:],
            "equalHighLow": "OBSERVED" if pools else None, "dol": dol,
            "evidence": ["IRL_ERL", "BSL_SSL", "DOL"] if pools else []}


def detect_mmxm(amd: Dict[str, Any], liquidity: Dict[str, Any],
                ict: Dict[str, Any], bars: List[Dict[str, float]]) -> Dict[str, Any]:
    """MMBM/MMSM state: raid, MSS, then delivery toward DOL."""
    if len(bars) < 4:
        return {"state": "WATCH", "status": "WATCH", "valid": False, "direction": None,
                "evidence": ["NEED_MSS_BARS"]}
    last = bars[-1]
    prev = bars[-2]
    mss_up = last["c"] > max(b["h"] for b in bars[-4:-1])
    mss_down = last["c"] < min(b["l"] for b in bars[-4:-1])
    raid = amd.get("manipulation")
    if raid == "SSL_RAID" and mss_up:
        direction, state = "BUY", "MMBM_BUY_MODEL"
    elif raid == "BSL_RAID" and mss_down:
        direction, state = "SELL", "MMSM_SELL_MODEL"
    else:
        direction, state = None, "WATCH"
    rng = ict.get("range") if isinstance(ict, dict) else None
    pd = (rng or {}).get("position") if isinstance(rng, dict) else None
    # R37: premium/discount の逆張り禁止は、レンジ未取得のとき丸ごと無効化されていた
    # (`pd is None` は「制約なし」として素通り)。位置が分からないなら「逆側でない」
    # とは言えないので fail-closed にする。カタログ §3 の front-run 禁止に合わせる。
    if pd is None:
        state = "WATCH"
    elif direction == "BUY" and pd == "PREMIUM":
        state = "WATCH"
    elif direction == "SELL" and pd == "DISCOUNT":
        state = "WATCH"
    dol = (liquidity.get("dol") or {}).get(direction) if direction else None
    armed = direction is not None and state != "WATCH"
    return {
        "state": state,
        # R37: `status` キーが無く lifecycle=UNKNOWN で強制無効化されていた。
        "status": "CONFIRMED" if armed else "WATCH",
        "valid": armed,
        "direction": direction, "mss": "BULLISH" if mss_up else "BEARISH" if mss_down else None,
        "raid": raid, "dol": dol, "pdPosition": pd,
        "entryArrays": ["FVG", "OB", "PD_ARRAY"],
        "failureCondition": "mss_invalidated_before_delivery",
        "evidence": (["CONSOLIDATION", raid, "MSS", "DELIVERY_TO_DOL"] if armed
                     else ["RANGE_ANCHOR_REQUIRED"] if pd is None
                     else ["WAIT_LIQUIDITY_RAID_AND_MSS"]),
    }


def detect_rejection_wick(bars: List[Dict[str, float]], levels: Iterable[Dict[str, Any]],
                          tolerance: float = 2.0) -> Dict[str, Any]:
    if not bars:
        return {"status": "MISSING", "direction": None}
    bar = bars[-1]
    body = max(abs(bar["c"] - bar["o"]), 0.01)
    upper = bar["h"] - max(bar["o"], bar["c"])
    lower = min(bar["o"], bar["c"]) - bar["l"]
    # 方向は**支配的なヒゲ**で決める。以前は lower を無条件に先に評価していたため、
    # 上ヒゲが下ヒゲの 80 倍でも BUY になった(o100 h120 l99.75 c100 で実測)。
    # 拒否は長いヒゲの側で起きるので、両者を比べないと意味を成さない。
    direction = None
    if lower >= body * 1.5 or upper >= body * 1.5:
        if lower > upper:
            direction = "BUY"
        elif upper > lower:
            direction = "SELL"
    # 掃除されるのは終値ではなく**ヒゲの先端**。しかも最初の一致で break していたため、
    # より遠いレベルがより近いレベルに勝っていた(変数名 nearest と実挙動が食い違う)。
    probe = bar["l"] if direction == "BUY" else bar["h"] if direction == "SELL" else bar["c"]
    nearest = None
    best_gap = None
    for level in levels or []:
        lp = _num(level.get("price")) if isinstance(level, dict) else None
        if lp is None:
            continue
        gap = abs(lp - probe)
        if gap <= tolerance and (best_gap is None or gap < best_gap):
            nearest, best_gap = lp, gap
    confirmed = bool(direction and nearest is not None)
    wick_low = bar["l"] if direction == "BUY" else bar["h"] if direction == "SELL" else None
    wick_high = min(bar["o"], bar["c"]) if direction == "BUY" else max(bar["o"], bar["c"]) if direction == "SELL" else None
    return {"status": "CONFIRMED" if confirmed else "WATCH",
            # R37: `valid` を返さないため、CONFIRMED でも票に一度も入れなかった。
            "valid": confirmed,
            "direction": direction, "level": nearest,
            "levelProbe": round(probe, 2),
            "wick50": round((wick_low + wick_high) / 2, 2) if wick_low is not None and wick_high is not None else None,
            "upperWick": round(upper, 2), "lowerWick": round(lower, 2),
            "failureCondition": "close_beyond_wick_midpoint",
            "evidence": ["REJECTION_WICK", "50_PERCENT_WICK", "HTF_LTF_LEVEL_MATCH"]
            if confirmed else ["WAIT_LEVEL_TOUCH"]}


def detect_vwap_reversion(bundle: Dict[str, Any], bars: List[Dict[str, float]],
                          price: Optional[float]) -> Dict[str, Any]:
    snapshot = bundle.get("snapshot") if isinstance(bundle, dict) else {}
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    vwap = _num(bundle.get("vwap")) or _num(snapshot.get("vwap"))
    if vwap is None or price is None:
        return {"status": "MISSING", "signal": None, "vwap": vwap, "evidence": ["VWAP_REQUIRED"]}
    atr = _atr(bars)
    distance = price - vwap
    threshold = max(atr or 0.0, 1.0)
    last = bars[-1] if bars else None
    momentum = _direction(last["c"], last["o"]) if last else None
    signal = None
    if distance <= -threshold and momentum != "SELL":
        signal = "BUY_REVERSION"
    elif distance >= threshold and momentum != "BUY":
        signal = "SELL_REVERSION"
    ema = _ema(bars)
    return {"status": "CONFIRMED" if signal else "OBSERVE", "signal": signal,
            # R37: `valid` を返さないため、CONFIRMED 119 本が票に一度も入れなかった。
            "valid": bool(signal),
            "direction": "BUY" if signal == "BUY_REVERSION" else "SELL" if signal == "SELL_REVERSION" else None,
            "vwap": round(vwap, 2), "distance": round(distance, 2),
            "atr": round(atr, 2) if atr else None,
            "ema": round(ema, 2) if ema is not None else None,
            "failureCondition": "close_extends_beyond_deviation_band",
            "evidence": ["VWAP_DEVIATION", "ATR_CONTEXT", "MEAN_REVERSION"] if signal else ["WAIT_MOMENTUM_FADE"]}


def detect_fib_alignment(ict: Dict[str, Any], price: Optional[float]) -> Dict[str, Any]:
    rng = (ict or {}).get("range") or {}
    if not rng or price is None or not rng.get("valid"):
        return {"status": "MISSING", "direction": None, "evidence": ["HTF_RANGE_REQUIRED"]}
    anchor = (ict or {}).get("rangeAnchor") if isinstance((ict or {}).get("rangeAnchor"), dict) else {}
    if not anchor and isinstance(rng.get("anchor"), dict):
        anchor = rng["anchor"]
    direction = "BUY" if (rng.get("favors") or {}).get("BUY") else "SELL" if (rng.get("favors") or {}).get("SELL") else None
    ote = rng.get("oteBuy") if direction == "BUY" else rng.get("oteSell") if direction == "SELL" else None
    aligned = bool(ote and min(ote) <= price <= max(ote))
    anchor_type = anchor.get("anchorType", anchor.get("type"))
    range_tf = anchor.get("rangeTf", rng.get("rangeTf"))
    freshness = str(anchor.get("freshness", rng.get("freshness", "UNKNOWN"))).upper()
    session_id = anchor.get("sessionId") or (ict or {}).get("sessionId") or (ict or {}).get("session")
    if isinstance(session_id, dict):
        session_id = session_id.get("sessionId", session_id.get("id", session_id.get("window")))
    as_of = anchor.get("asOf", (ict or {}).get("asOf", (ict or {}).get("at")))
    provenance = anchor.get("provenance", anchor.get("source", (ict or {}).get("provenance", "ict_range")))
    anchor_ok = bool(range_tf and anchor_type) and freshness in USABLE_ANCHOR_FRESHNESS
    context_ok = bool(session_id and as_of and provenance)
    valid = bool(aligned and direction and anchor_ok and context_ok)
    reasons = []
    if not anchor_ok:
        reasons.append("FIB_ANCHOR_OR_FRESHNESS_MISSING")
    if not context_ok:
        reasons.append("FIB_SESSION_ASOF_OR_PROVENANCE_MISSING")
    if not aligned:
        reasons.append("OTE_TOUCH_REQUIRED")
    return {"status": "ALIGNED" if valid else "OBSERVE", "valid": valid,
            "direction": direction if valid else None, "ote": ote, "position": rng.get("position"),
            "rangeTf": range_tf, "anchorType": anchor_type, "freshness": freshness,
            "sessionId": session_id, "asOf": as_of, "provenance": provenance,
            "entryTouched": aligned, "ineligibleReasons": reasons,
            "evidence": ["PREMIUM_DISCOUNT", "OTE_62_79"] if aligned else ["WAIT_OTE_TOUCH"]}


def _first_mapping(bundle: Dict[str, Any], keys: Iterable[str]) -> Any:
    """Return the first non-empty mapping/list from bundle or snapshot aliases."""
    snapshot = bundle.get("snapshot") if isinstance(bundle.get("snapshot"), dict) else {}
    for source in (bundle, snapshot):
        for key in keys:
            value = source.get(key) if isinstance(source, dict) else None
            if value not in (None, {}, []):
                return value
    return None


def _zone_rows(raw: Any, default_kind: str = "ZONE") -> List[Dict[str, Any]]:
    """Normalize user/provider supplied price zones without inventing coordinates."""
    if raw is None:
        return []
    if isinstance(raw, dict):
        # A keyed object is common in snapshots: {"OB": [...], "BRK": [...]}
        keyed = []
        for key, value in raw.items():
            if isinstance(value, (list, tuple)):
                keyed.extend({"kind": key, **item} if isinstance(item, dict) else {"kind": key, "price": item}
                              for item in value)
            elif isinstance(value, dict) and any(k in value for k in ("lo", "low", "bottom", "hi", "high", "top", "price")):
                keyed.append({"kind": key, **value})
        raw = keyed or [raw]
    if not isinstance(raw, (list, tuple)):
        return []
    out: List[Dict[str, Any]] = []
    aliases = {"OB": "OB", "ORDER_BLOCK": "OB", "ORDERBLOCK": "OB",
               "BRK": "BRK", "BREAKER": "BRK", "BREAKER_BLOCK": "BRK",
               "MB": "MB", "MITIGATION": "MB", "MITIGATION_BLOCK": "MB",
               "RJB": "RJB", "REJECTION": "RJB", "REJECTION_BLOCK": "RJB",
               "IFVG": "IFVG", "INVERSE_FVG": "IFVG"}
    for item in raw:
        if not isinstance(item, dict):
            continue
        lo = _num(item.get("lo", item.get("low", item.get("bottom", item.get("price")))))
        hi = _num(item.get("hi", item.get("high", item.get("top", item.get("to", item.get("price"))))))
        if lo is None and hi is None:
            continue
        if lo is None:
            lo = hi
        if hi is None:
            hi = lo
        lo, hi = min(lo, hi), max(lo, hi)
        raw_kind = str(item.get("kind", item.get("type", item.get("model", default_kind)))).upper().replace(" ", "_")
        kind = aliases.get(raw_kind, raw_kind)
        raw_side = str(item.get("direction", item.get("side", item.get("bias", "")))).upper()
        direction = "BUY" if raw_side in {"BUY", "BULL", "BULLISH", "LONG"} else \
            "SELL" if raw_side in {"SELL", "BEAR", "BEARISH", "SHORT"} else None
        active = item.get("active", item.get("valid", item.get("eligible", True))) is not False
        row = {"kind": kind, "direction": direction, "lo": round(lo, 2), "hi": round(hi, 2),
                    "mid": round((lo + hi) / 2, 2), "active": bool(active),
                    "freshness": str(item.get("freshness", "UNKNOWN")).upper(),
                    "source": item.get("source", "provider")}
        # Keep provider lifecycle evidence intact for list, keyed-object, and
        # single-object input forms.  Detectors may normalize coordinates, but
        # they must never infer an origin/displacement/reclaim that was absent.
        for key in ("origin", "originAt", "displacement", "displacementConfirmed", "revisit",
                    "entryTouched", "touched", "breached", "inverseReclaim", "reclaimed",
                    "structureIntact", "structureMaintained", "hold", "reclaimHeld", "structureHeld",
                    "consumed", "invalidated", "lifecycle", "state", "asOf", "sessionId",
                    "timeframe", "provenance", "breachAt", "retestAt",
                    "originalDirection", "createdAt", "ageBars"):
            if key in item:
                row[key] = item[key]
        out.append(row)
    return out


def detect_blocks(bundle: Dict[str, Any], bars: List[Dict[str, float]],
                  price: Optional[float]) -> Dict[str, Any]:
    """Detect the Repo2 block family: OB, breaker (BRK), mitigation (MB), RJB."""
    raw = _first_mapping(bundle, ("blocks", "zones", "orderBlocks", "breakerBlocks",
                                   "mitigationBlocks", "rejectionBlocks"))
    zones = _zone_rows(raw)
    # Keep the detector honest: OHLC fallback only emits a candidate when a
    # clear opposite candle is followed by a displacement-sized body.
    if not zones and len(bars) >= 5:
        body_avg = sum(abs(b["c"] - b["o"]) for b in bars[-8:]) / min(8, len(bars))
        last = bars[-1]
        prev = bars[-2]
        if body_avg > 0 and abs(last["c"] - last["o"]) >= body_avg * 1.35:
            bullish = last["c"] > last["o"] and prev["c"] < prev["o"]
            bearish = last["c"] < last["o"] and prev["c"] > prev["o"]
            if bullish or bearish:
                zones = [{"kind": "OB", "direction": "BUY" if bullish else "SELL",
                          "lo": round(min(prev["o"], prev["c"]), 2),
                          "hi": round(max(prev["o"], prev["c"]), 2),
                          "mid": round((prev["o"] + prev["c"]) / 2, 2),
                          "active": True, "freshness": "INFERRED", "source": "ohlc"}]
    # Provider lifecycle is authoritative.  A consumed/invalidated block may
    # remain visible in the audit trail but is never an active entry array.
    for zone in zones:
        lifecycle = str(zone.get("lifecycle", zone.get("state", ""))).lower()
        if lifecycle in {"invalidated", "consumed"}:
            zone["active"] = False
            zone["lifecycle"] = lifecycle.upper()
        elif lifecycle in {"candidate", "confirmed"}:
            zone["lifecycle"] = lifecycle.upper()
        else:
            zone["lifecycle"] = "CONFIRMED" if zone.get("active") else "CANDIDATE"
    active = [z for z in zones if z.get("active")]
    near = [z for z in active if price is not None and z["lo"] <= price <= z["hi"]]
    qualified = []
    for zone in zones:
        origin = zone.get("origin", zone.get("originAt"))
        displacement = zone.get("displacement", zone.get("displacementConfirmed"))
        revisit = zone.get("revisit", zone.get("entryTouched", zone.get("touched"))) is True
        zone.update({"origin": origin, "displacement": displacement is True or
                     (isinstance(displacement, (int, float)) and displacement > 0), "revisit": revisit})
        if (zone.get("active") and zone.get("lifecycle") == "CONFIRMED" and zone in near
                and zone["origin"] and zone["displacement"] and revisit):
            qualified.append(zone)
    direction = qualified[-1].get("direction") if qualified else None
    kinds = sorted({z["kind"] for z in active})
    selected = qualified[-1] if qualified else (near[-1] if near else (active[-1] if active else {}))
    return {"status": "CONFIRMED" if qualified else "OBSERVED" if active else "MISSING",
            "valid": bool(qualified), "direction": direction, "zones": active[-12:],
            "activeKinds": kinds, "freshness": selected.get("freshness", "UNKNOWN"),
            "entryTouched": bool(qualified), "provenance": selected.get("source", "provider"),
            "evidence": (["BLOCK_ARRAY", *kinds, "PRICE_TOUCH"] if near
                                                   else ["BLOCK_ARRAY", *kinds] if active else ["BLOCK_REQUIRED"])}


def _tick(value: float) -> float:
    return round(round(value / MNQ_TICK) * MNQ_TICK, 2)


def _derive_ifvg(bars: List[Dict[str, float]]) -> List[Dict[str, Any]]:
    """Derive breached FVG lifecycle from confirmed 3m OHLC.

    A gap becomes IFVG only after a body close crosses its far edge.  It is
    confirmed after a later retest closes back on the inverse side.  A later
    body close through the opposite edge invalidates it.  Wicks alone do not
    create or destroy the lifecycle.
    """
    if len(bars) < 6:
        return []
    zones: List[Dict[str, Any]] = []
    for index in range(2, len(bars)):
        left, right = bars[index - 2], bars[index]
        if left["h"] < right["l"]:
            lo, hi, original = left["h"], right["l"], "BUY"
        elif left["l"] > right["h"]:
            lo, hi, original = right["h"], left["l"], "SELL"
        else:
            continue
        breach_index = None
        inverse = "SELL" if original == "BUY" else "BUY"
        for probe in range(index + 1, len(bars)):
            close = bars[probe]["c"]
            if (original == "BUY" and close < lo) or (original == "SELL" and close > hi):
                breach_index = probe
                break
        if breach_index is None:
            continue

        retest_index = None
        for probe in range(breach_index + 1, len(bars)):
            bar = bars[probe]
            touches = bar["h"] >= lo and bar["l"] <= hi
            held = ((inverse == "SELL" and bar["c"] <= lo) or
                    (inverse == "BUY" and bar["c"] >= hi))
            if touches and held:
                retest_index = probe
                break

        structure_from = retest_index if retest_index is not None else breach_index
        invalidated = any(
            (inverse == "SELL" and bar["c"] > hi) or
            (inverse == "BUY" and bar["c"] < lo)
            for bar in bars[structure_from + 1:]
        )
        age_bars = len(bars) - 1 - breach_index
        confirmed = retest_index is not None and not invalidated
        zones.append({
            "kind": "IFVG", "direction": inverse,
            "originalDirection": original, "lo": _tick(lo), "hi": _tick(hi),
            "active": not invalidated and age_bars <= 60,
            "breached": True, "inverseReclaim": confirmed,
            "structureIntact": not invalidated, "hold": confirmed,
            "invalidated": invalidated, "lifecycle": "CONFIRMED" if confirmed else
                "INVALIDATED" if invalidated else "CANDIDATE",
            "createdAt": right.get("t"), "breachAt": bars[breach_index].get("t"),
            "retestAt": bars[retest_index].get("t") if retest_index is not None else None,
            "ageBars": age_bars,
            "freshness": "FRESH" if age_bars <= 20 else "STALE",
            "timeframe": "3m", "source": "confirmed_3m_ohlc",
            "provenance": "confirmed_3m_ohlc",
        })
    return zones[-12:]


def detect_ifvg(bundle: Dict[str, Any], ict: Dict[str, Any], price: Optional[float],
                bars: Optional[List[Dict[str, float]]] = None) -> Dict[str, Any]:
    """Normalize inverse FVG (IFVG) zones and require an active price interaction."""
    raw = _first_mapping(bundle, ("ifvg", "inverseFvg", "inverseFVG"))
    if raw is None and isinstance(ict, dict):
        fvg = ict.get("fvg") or {}
        raw = fvg.get("IFVG") or fvg.get("ifvg") or fvg.get("inverse")
    zones = _zone_rows(raw, "IFVG")
    if not zones:
        zones = _zone_rows(_derive_ifvg(bars or []), "IFVG")
    for z in zones:
        z["kind"] = "IFVG"
    # IFVG is not a label-only signal: a breached gap must inverse-reclaim
    # and preserve the approach structure before it can be entry-eligible.
    for zone in zones:
        breached = zone.get("breached") is True
        reclaimed = zone.get("inverseReclaim", zone.get("reclaimed")) is True
        structure = zone.get("structureIntact", zone.get("structureMaintained")) is True
        held = zone.get("hold", zone.get("reclaimHeld", zone.get("structureHeld"))) is True
        zone["breached"] = breached
        zone["inverseReclaim"] = reclaimed
        zone["structureIntact"] = structure
        zone["hold"] = held
        declared = str(zone.get("lifecycle", zone.get("state", ""))).upper()
        if zone.get("consumed") is True or zone.get("invalidated") is True:
            zone["lifecycle"] = "CONSUMED" if zone.get("consumed") is True else "INVALIDATED"
            zone["active"] = False
        elif declared == "CANDIDATE":
            zone["lifecycle"] = "CANDIDATE"
        elif breached and reclaimed and structure and held:
            zone["lifecycle"] = "CONFIRMED"
        elif breached:
            zone["lifecycle"] = "CANDIDATE"
        else:
            zone["lifecycle"] = "UNKNOWN"
    near = [z for z in zones if z.get("active") and price is not None and z["lo"] <= price <= z["hi"]]
    eligible = [z for z in near if z.get("lifecycle") == "CONFIRMED"]
    selected = eligible[-1] if eligible else (near[-1] if near else (zones[-1] if zones else {}))
    # 方向は **valid を立てたゾーンそのもの**から取る。以前は別配列(near or zones)の
    # 末尾から取っていたため、BUY の確認証跡で SELL 票が立ち、配列順を入れ替える
    # だけで方向が反転した。detect_blocks と同じ規則(根拠配列＝方向の出所)に揃える。
    # freshness / provenance も同じ selected 由来なので、これで三者が一致する。
    return {"status": "CONFIRMED" if eligible else "OBSERVE" if zones else "MISSING",
            "valid": bool(eligible), "direction": selected.get("direction"),
            "freshness": selected.get("freshness", "UNKNOWN"),
            "entryTouched": bool(eligible), "provenance": selected.get("source", "provider"),
            "zones": zones[-8:], "evidence": (["IFVG", "INVERSE_RECLAIM", "PRICE_TOUCH"] if near
                                               else ["IFVG_OBSERVED"] if zones else ["IFVG_REQUIRED"])}


def _detect_gann(bars: List[Dict[str, float]], price: Optional[float]) -> Dict[str, Any]:
    """Gann 1×1 / 同心円(R32)。助言専用で、方向票には出さない。"""
    try:
        import gann                                              # noqa: PLC0415
        return gann.observe(bars, price)
    except Exception:
        return {"status": "MISSING", "advisory": True, "reason": "GANN_UNAVAILABLE"}


def detect_quarterly_theory(bundle: Dict[str, Any], bars: List[Dict[str, float]],
                            price: Optional[float]) -> Dict[str, Any]:
    """XAMD/Quarterly Theory mapping from the Repo2 deck (advisory only)."""
    raw = _first_mapping(bundle, ("quarterly", "quarterTheory", "qt"))
    if not isinstance(raw, dict):
        # R31: Quarterly Theory は時間の分割そのもので、指標も外部入力も要らない。
        # 素通し実装のままだったので実サイクル 403 本で取得率 0% だった。
        # 足の時刻から計算する。外部入力があればそちらが優先(上の分岐)。
        try:
            import quarterly_theory                                  # noqa: PLC0415
            computed = quarterly_theory.observe(bundle, bars, price)
        except Exception:
            computed = None
        if isinstance(computed, dict) and computed.get("status") == "COMPUTED":
            raw = computed
        else:
            return {"status": "MISSING", "valid": False, "direction": None,
                    "stage": None, "sequence": [], "evidence": ["QT_REQUIRED"]}
    sequence = raw.get("sequence", raw.get("quarters", []))
    if isinstance(sequence, str):
        sequence = [part.strip().upper() for part in sequence.replace("-", "/").split("/") if part.strip()]
    sequence = [str(part).upper() for part in sequence] if isinstance(sequence, (list, tuple)) else []
    stage = str(raw.get("stage", raw.get("current", raw.get("quarter", "")))).upper() or None
    bias = str(raw.get("bias", raw.get("direction", ""))).upper()
    direction = "BUY" if bias in {"BUY", "BULL", "BULLISH", "LONG"} else \
        "SELL" if bias in {"SELL", "BEAR", "BEARISH", "SHORT"} else None
    if direction is None and stage in {"D", "X"}:
        direction = "BUY" if str(raw.get("side", "")).upper() in {"BUY", "LONG"} else \
            "SELL" if str(raw.get("side", "")).upper() in {"SELL", "SHORT"} else None
    as_of = raw.get("asOf", raw.get("at"))
    session_id = raw.get("sessionId", raw.get("session"))
    sequence_at = raw.get("sequenceAt", as_of)
    time_ok = bool(as_of and sequence_at and session_id)
    sequence_ok = sequence[-4:] == ["X", "A", "M", "D"] and stage == "D"
    freshness = str(raw.get("freshness", "UNKNOWN")).upper()
    return {"status": "CONFIRMED" if direction and sequence_ok and time_ok else "WATCH",
            "valid": bool(direction and sequence_ok and time_ok), "direction": direction, "stage": stage,
            "sequence": sequence, "quarter": raw.get("quarter", raw.get("q")),
            "target": _num(raw.get("target")), "asOf": as_of, "sequenceAt": sequence_at,
            "sessionId": session_id, "freshness": freshness,
            "provenance": raw.get("provenance", raw.get("source", "provider")),
            "ineligibleReasons": ([] if time_ok and sequence_ok else
                (["QUARTERLY_TIME_OR_SESSION_MISSING"] if not time_ok else ["QUARTERLY_XAMD_SEQUENCE_INCOMPLETE"])),
            "evidence": ["QUARTERLY_THEORY", "XAMD", f"STAGE_{stage}" if stage else "STAGE_REQUIRED"]}


def detect_session_profile(bundle: Dict[str, Any], ict: Dict[str, Any],
                           price: Optional[float]) -> Dict[str, Any]:
    """Asia build -> London sweep -> NY delivery and Midnight Open target."""
    raw = _first_mapping(bundle, ("sessionProfile", "sessions", "sessionLevels"))
    raw = raw if isinstance(raw, dict) else {}
    asia = raw.get("asia", raw.get("asian", {}))
    asia = asia if isinstance(asia, dict) else {}
    asia_high = _num(raw.get("asianHigh", asia.get("high")))
    asia_low = _num(raw.get("asianLow", asia.get("low")))
    midnight = _num(raw.get("midnightOpen", raw.get("midnight")))
    session = str(raw.get("current", raw.get("session", ""))).upper() or str((ict.get("session") or {}).get("window", ""))
    direction = None
    target = None
    if price is not None and midnight is not None:
        direction, target = ("BUY", midnight) if midnight > price else ("SELL", midnight) if midnight < price else (None, midnight)
    elif price is not None and asia_high is not None and price > asia_high:
        direction = "BUY"
    elif price is not None and asia_low is not None and price < asia_low:
        direction = "SELL"
    london_sweep = raw.get("londonSweep", raw.get("london", {}).get("sweep")
                         if isinstance(raw.get("london"), dict) else False) is True
    ny_delivery = raw.get("nyDelivery", raw.get("ny", {}).get("delivery")
                        if isinstance(raw.get("ny"), dict) else False) is True
    sequence_ok = (asia_high is not None and asia_low is not None and london_sweep and ny_delivery
                   and session in {"NY", "NY_AM", "NEW_YORK"})
    valid = direction is not None and sequence_ok
    freshness = str(raw.get("freshness", "UNKNOWN")).upper()
    return {"status": "CONFIRMED" if valid else "OBSERVE" if (midnight is not None or asia_high is not None or asia_low is not None) else "MISSING",
            "valid": valid, "direction": direction, "session": session or None,
            "asianHigh": asia_high, "asianLow": asia_low, "midnightOpen": midnight,
            "target": target, "londonSweep": london_sweep, "nyDelivery": ny_delivery,
            "freshness": freshness, "entryTouched": valid,
            "provenance": raw.get("provenance", raw.get("source", "provider")),
            "evidence": (["MIDNIGHT_OPEN_TARGET", "ASIA_RANGE", "LONDON_SWEEP", "NY_DELIVERY", session or "SESSION"]
                                           if valid else ["SESSION_LEVELS_OBSERVED"] if (midnight is not None or asia_high is not None or asia_low is not None)
                                           else ["SESSION_PROFILE_REQUIRED"])}


def _derive_fib_sd(ict: Dict[str, Any], bars: List[Dict[str, float]],
                   price: Optional[float]) -> Optional[Dict[str, Any]]:
    """Project the Repo2 Fib-SD ladder from an explicit, usable ICT range.

    The anchor is never synthesized from rolling 3m highs/lows.  ``ict.range``
    must already be the output of ``resolve_range_anchor``.
    """
    rng = (ict or {}).get("range") or {}
    if not isinstance(rng, dict) or rng.get("valid") is not True:
        return None
    high, low = _num(rng.get("high")), _num(rng.get("low"))
    favors = rng.get("favors") if isinstance(rng.get("favors"), dict) else {}
    direction = "BUY" if favors.get("BUY") else "SELL" if favors.get("SELL") else None
    anchor_type = rng.get("anchorType")
    freshness = str(rng.get("freshness", "UNKNOWN")).upper()
    if high is None or low is None or high <= low or direction is None or not anchor_type:
        return None
    origin, terminal = (high, low) if direction == "BUY" else (low, high)
    ratio_rows = [{"ratio": ratio, "price": _tick(origin + ratio * (terminal - origin))}
                  for ratio in FIB_SD_RATIOS]
    atr = _atr(bars)
    tolerance = max(0.75, min(3.0, (atr or 5.0) * 0.15))
    last = bars[-1] if bars else None
    nearest = min(ratio_rows, key=lambda row: abs(row["price"] - price)) \
        if price is not None else None
    touched = bool(nearest and (abs(nearest["price"] - price) <= tolerance or
                  (last and last["l"] - tolerance <= nearest["price"] <= last["h"] + tolerance)))
    return {
        "levels": [row["price"] for row in ratio_rows], "ratioRows": ratio_rows,
        "bias": direction, "direction": direction, "tolerance": round(tolerance, 2),
        "touched": touched, "anchorType": anchor_type,
        "rangeTf": rng.get("rangeTf"), "freshness": freshness,
        "asOf": rng.get("asOf") or (ict or {}).get("asOf"),
        "sessionId": rng.get("sessionId") or (ict or {}).get("sessionId"),
        "provenance": "DERIVED_EXPLICIT_ICT_RANGE",
    }


def detect_fib_sd(bundle: Dict[str, Any], price: Optional[float],
                  bars: Optional[List[Dict[str, float]]] = None,
                  ict: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    raw = _first_mapping(bundle, ("fibSd", "fibSD", "fibStandardDeviation", "standardDeviation"))
    if not isinstance(raw, dict):
        raw = _derive_fib_sd(ict or {}, bars or [], price)
    if not isinstance(raw, dict):
        return {"status": "MISSING", "valid": False, "direction": None,
                "levels": [], "evidence": ["EXPLICIT_HTF_RANGE_REQUIRED"]}
    values = raw.get("levels", raw.get("ratios", []))
    if isinstance(values, dict):
        values = list(values.values())
    levels = [_num(value) for value in values] if isinstance(values, (list, tuple)) else []
    levels = [round(value, 2) for value in levels if value is not None]
    tolerance = max(_num(raw.get("tolerance")) or 0.75, 0.25)
    nearest = min(levels, key=lambda value: abs(value - price)) if levels and price is not None else None
    aligned = nearest is not None and abs(nearest - price) <= tolerance
    bias = str(raw.get("bias", raw.get("direction", ""))).upper()
    direction = "BUY" if bias in {"BUY", "BULL", "BULLISH", "LONG"} else \
        "SELL" if bias in {"SELL", "BEAR", "BEARISH", "SHORT"} else None
    anchor = raw.get("anchor") if isinstance(raw.get("anchor"), dict) else {}
    anchor_type = raw.get("anchorType", anchor.get("type"))
    freshness = str(raw.get("freshness", anchor.get("freshness", "UNKNOWN"))).upper()
    anchor_ok = bool(anchor_type) and freshness in USABLE_ANCHOR_FRESHNESS
    touched = raw.get("touched", raw.get("entryTouched")) is True
    return {"status": "ALIGNED" if aligned and anchor_ok and touched else "OBSERVE", "valid": aligned and anchor_ok and touched,
            "direction": direction, "levels": levels, "nearest": nearest,
            "tolerance": tolerance, "anchorType": anchor_type, "freshness": freshness, "entryTouched": touched,
            "rangeTf": raw.get("rangeTf"), "ratioRows": raw.get("ratioRows", []),
            "asOf": raw.get("asOf"), "sessionId": raw.get("sessionId"),
            "provenance": raw.get("provenance", raw.get("source", "provider")),
            "ineligibleReasons": ([] if anchor_ok and touched else
                (["FIB_SD_ANCHOR_OR_FRESHNESS_MISSING"] if not anchor_ok else ["FIB_SD_TOUCH_REQUIRED"])),
            "evidence": ["FIB_STANDARD_DEVIATION", "FIB_SD_LEVEL"] if aligned else ["WAIT_FIB_SD_TOUCH"]}


def detect_fib_crt(crt: Dict[str, Any], fib: Dict[str, Any]) -> Dict[str, Any]:
    shared_session = crt.get("sessionId") and crt.get("sessionId") == fib.get("sessionId")
    freshness = str(fib.get("freshness") or crt.get("freshness") or "UNKNOWN").upper()
    as_of = fib.get("asOf") or crt.get("asOf")
    provenance = fib.get("provenance") or crt.get("provenance")
    aligned = bool(crt.get("valid") and fib.get("valid") and shared_session and as_of and provenance
                   and freshness in USABLE_ANCHOR_FRESHNESS
                   and crt.get("direction") and crt.get("direction") == fib.get("direction"))
    direction = crt.get("direction") if aligned else None
    return {"status": "CONFIRMED" if aligned else "WATCH", "valid": aligned,
            "direction": direction, "freshness": freshness, "sessionId": fib.get("sessionId") or crt.get("sessionId"),
            "asOf": as_of, "provenance": provenance,
            "entryTouched": bool(crt.get("entryTouched") and fib.get("entryTouched")),
            "ineligibleReasons": ([] if aligned else ["CRT_FIB_CONTEXT_OR_ALIGNMENT_REQUIRED"]),
            "evidence": ["CRT_SWEEP", "FIB_RETRACE_CONFLUENCE"] if aligned else ["CRT_AND_FIB_REQUIRED"]}


def _vote_direction(model: Dict[str, Any]) -> Optional[str]:
    """Return an eligible directional vote, never an observation or target hint."""
    if isinstance(model, dict) and model.get("advisory") is True:
        return None
    if not isinstance(model, dict) or model.get("valid") is not True:
        return None
    status = str(model.get("status", "")).upper()
    if status not in {"CONFIRMED", "ALIGNED"}:
        return None
    # Repo2 lifecycle is distinct from display status.  A candidate can still
    # be chart-active/visible, but it never becomes a confluence vote.
    lifecycle = model.get("lifecycle")
    if lifecycle is not None and str(lifecycle).upper() != "CONFIRMED":
        return None
    # A feed may explicitly mark stale/session/provenance uncertainty.  The
    # absence of these optional fields remains observational, but an explicit
    # negative declaration is always fail-closed for voting.
    for key in ("freshness", "session", "sessionId", "provenance"):
        value = model.get(key)
        if value is None:
            continue
        text = str(value).upper()
        if text in {"UNKNOWN", "STALE", "EXPIRED", "MISSING", "UNAVAILABLE", "UNVERIFIED", "OUT_OF_SESSION", "WRONG_SESSION"}:
            return None
    if model.get("touched") is False or model.get("entryTouched") is False or model.get("targetOnly") is True:
        return None
    direction = str(model.get("direction") or model.get("signal") or model.get("bias") or "").upper()
    if direction.startswith("BUY") or direction == "BULLISH":
        return "BUY"
    if direction.startswith("SELL") or direction == "BEARISH":
        return "SELL"
    return None


#: 相関する検出器は方向あたり 1 票に畳む。独立でない証拠を 2 票として数えると、
#: 同じ根拠だけで alignment の ±2 閾値をまたげてしまう。
#:   crt / fib / fibCrt — いずれも同じレンジと Fib 比率から導かれる
#:   mmxm / amdWyckoff  — `detect_mmxm` は `classify_amd` の出力をそのまま入力に取る
#:                        ので、両者を独立票にすると同じ足の判定を二重計上する(R37)
CORRELATED_FAMILIES = {
    "crt": "CRT_FIB", "fib": "CRT_FIB", "fibCrt": "CRT_FIB",
    "mmxm": "AMD", "amdWyckoff": "AMD",
}


def _eligible_votes(models: Dict[str, Dict[str, Any]]) -> Dict[str, List[str]]:
    """Count only executable evidence; correlated variants share one vote."""
    votes: Dict[str, List[str]] = {"BUY": [], "SELL": []}
    used: Dict[str, set] = {"BUY": set(), "SELL": set()}
    for name, model in models.items():
        direction = _vote_direction(model)
        if direction is None:
            continue
        family = CORRELATED_FAMILIES.get(name)
        if family is not None:
            if family in used[direction]:
                continue
            used[direction].add(family)
        votes[direction].append(name)
    return votes


def _repo2_lifecycle(models: Dict[str, Dict[str, Any]], bundle: Dict[str, Any]) -> None:
    """Attach immutable detector state without conflating display with votes."""
    snapshot = bundle.get("snapshot") if isinstance(bundle.get("snapshot"), dict) else {}
    as_of = bundle.get("at") or snapshot.get("at")
    session_id = bundle.get("sessionId") or snapshot.get("sessionId")
    timeframe = str(snapshot.get("resolution") or snapshot.get("timeframe") or "3")
    for name, model in models.items():
        if not isinstance(model, dict):
            continue
        status = str(model.get("status") or "UNKNOWN").upper()
        lifecycle = str(model.get("lifecycle") or "").upper()
        if not lifecycle:
            if status in {"CONFIRMED", "ALIGNED"} and model.get("valid") is True:
                lifecycle = "CONFIRMED"
            elif status in {"MISSING", "UNKNOWN"}:
                lifecycle = "UNKNOWN"
            else:
                lifecycle = "CANDIDATE"
        reasons = list(model.get("ineligibleReasons") or [])
        if lifecycle in {"UNKNOWN", "INVALIDATED", "CONSUMED"}:
            model["valid"] = False
            reasons.append(f"LIFECYCLE_{lifecycle}")
        if model.get("targetOnly") is True:
            model["valid"] = False
            reasons.append("TARGET_ONLY")
        patch = {
            "lifecycle": lifecycle.lower(),
            "asOf": model.get("asOf") or as_of,
            "timeframe": model.get("timeframe") or timeframe,
            "provenance": model.get("provenance") or model.get("source") or "DERIVED",
            "entryTouched": bool(model.get("entryTouched", model.get("valid") is True)),
            "ineligibleReasons": sorted(set(reasons)),
        }
        # R37: 欠落を "UNKNOWN" という**明示的な否定宣言**へ変換しない。
        # `_vote_direction` は「欠落は観測扱いのまま／明示的な否定だけ fail-close」と
        # 設計されている(同関数のコメント参照)が、この関数が先に走って欠落を
        # 埋めていたため `value is None` の分岐へ永遠に到達せず、全モデルが
        # 無条件に失格していた。実測で 403/403 サイクル・得票 0 の直接原因。
        own_session = model.get("sessionId") or session_id
        if own_session:
            patch["sessionId"] = own_session
        own_freshness = model.get("freshness")
        if own_freshness:
            patch["freshness"] = str(own_freshness).upper()
        model.update(patch)
    # Eligibility is deliberately separate from visibility: an observed zone
    # can be active on the chart while receiving no directional vote.
    for model in models.values():
        if isinstance(model, dict):
            model["eligible"] = _vote_direction(model) is not None


def build_strategy_matrix(bundle: Dict[str, Any], bars: Iterable[Dict[str, Any]],
                          levels: Iterable[Dict[str, Any]], ict: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    bundle = bundle if isinstance(bundle, dict) else {}
    clean = normalize_bars(bars)
    snapshot = bundle.get("snapshot") if isinstance(bundle.get("snapshot"), dict) else {}
    price = _num(bundle.get("price", bundle.get("last")))
    if price is None:
        price = _num(snapshot.get("price", snapshot.get("last")))
    if price is None and clean:
        price = clean[-1]["c"]
    ict = ict if isinstance(ict, dict) else {}
    liquidity = detect_liquidity(clean, levels, price)
    amd = classify_amd(clean)
    crt = detect_crt(clean)
    ict_context = dict(ict)
    ict_context.setdefault("asOf", bundle.get("at") or snapshot.get("at"))
    ict_context.setdefault("sessionId", bundle.get("sessionId") or snapshot.get("sessionId"))
    fib = detect_fib_alignment(ict_context, price)
    blocks = detect_blocks(bundle, clean, price)
    ifvg = detect_ifvg(bundle, ict, price, clean)
    quarterly = detect_quarterly_theory(bundle, clean, price)
    sessions = detect_session_profile(bundle, ict, price)
    fib_sd = detect_fib_sd(bundle, price, clean, ict_context)
    crt.update({
        "asOf": bundle.get("at") or snapshot.get("at"),
        "sessionId": bundle.get("sessionId") or snapshot.get("sessionId"),
        "freshness": "FRESH" if crt.get("valid") and clean else "UNKNOWN",
        "provenance": "confirmed_3m_bars",
        "entryTouched": bool(crt.get("valid")),
    })
    models = {
        "msnr": {"status": "INTEGRATED", "direction": None,
                 "evidence": ["HTF_SNR", "FRESHNESS", "LIT", "QT_SMT"]},
        "ict": {"status": "INTEGRATED", "direction": None,
                "rangeAnchor": (ict.get("rangeAnchor") or {}),
                "fvg": (ict.get("fvg") or {}), "dol": (ict.get("dol") or {}),
                "session": (ict.get("session") or {}),
                "evidence": ["PD_ARRAY", "FVG", "OTE", "KILLZONE"]},
        # R37: `_repo2_lifecycle` はこの dict を破壊的に書き換えるので、
        # `ict["smt"]` と同一オブジェクトを渡してはいけない。以前は publish される
        # SMT が `available=true / freshness=FRESH` なのに `valid=false` という
        # 自己矛盾した状態で配信されていた。
        "smt": dict(ict.get("smt") or {"available": False, "reason": "MISSING"}),
        "mmxm": detect_mmxm(amd, liquidity, ict, clean),
        "amdWyckoff": amd,
        "crt": crt,
        "fibCrt": {"status": "WATCH", "valid": False, "direction": None,
                   "evidence": ["CRT_AND_FIB_REQUIRED"]},
        "vwapReversion": detect_vwap_reversion(bundle, clean, price),
        "liquidity": liquidity,
        "rejectionWick": detect_rejection_wick(clean, levels),
        "fib": fib,
        "fibSd": fib_sd,
        "ifvg": ifvg,
        "blocks": blocks,
        "quarterly": quarterly,
        "sessions": sessions,
        # HTF は 3mモデル群と同じ票として二重計上しない。候補採点側が
        # 一度だけ +1/-1 を反映し、ここでは監査・表示用のコンテキストにする。
        "htf": {
            "status": str((snapshot.get("htfContext") or {}).get("status") or "MISSING"),
            "valid": bool((snapshot.get("htfContext") or {}).get("valid")),
            "direction": (snapshot.get("htfContext") or {}).get("bias"),
            "bias": (snapshot.get("htfContext") or {}).get("bias"),
            "frames": (snapshot.get("htfContext") or {}).get("frames") or {},
            "complete": bool((snapshot.get("htfContext") or {}).get("complete")),
            "advisory": True, "provenance": "confirmed_htf_ohlc",
            "evidence": ["HTF_45M_1H_4H_1D"],
        },
        # R32: Gann 1×1 と同心円。画像デッキの「円のフィボナッチ」の正体。
        # **助言専用** —— 実証的裏付けが無いので方向票(_eligible_votes)には
        # 出さず、ゲートにも確認要素にもしない。
        "gann": _detect_gann(clean, price),
    }
    _repo2_lifecycle(models, bundle)
    # FibCRT is a correlated composite, evaluated only after both children
    # carry frozen lifecycle/session/freshness metadata.
    models["fibCrt"] = detect_fib_crt(models["crt"], models["fib"])
    _repo2_lifecycle({"fibCrt": models["fibCrt"]}, bundle)
    evidence = _eligible_votes(models)
    buy, sell = len(evidence["BUY"]), len(evidence["SELL"])
    primary = "BUY" if buy > sell and buy >= 2 else "SELL" if sell > buy and sell >= 2 else None
    overlays = {
        "fvg": (ict.get("fvg") or {}),
        "levels": list(levels or [])[:20],
        "rangeAnchor": (ict.get("rangeAnchor") or {}),
        "crt": models["crt"],
        "liquidity": models["liquidity"],
        "vwap": models["vwapReversion"],
        "ifvg": models["ifvg"],
        "blocks": models["blocks"],
        "quarterly": models["quarterly"],
        "sessions": models["sessions"],
        "gann": models["gann"],
        "fibSd": models["fibSd"],
        "fibCrt": models["fibCrt"],
    }
    active = [name for name, model in models.items()
              if str(model.get("status", "")).upper() in {"CONFIRMED", "ALIGNED"}
              or model.get("valid") is True]
    return {
        "version": CATALOG_VERSION,
        "detectorVersion": DETECTOR_VERSION,
        "mode": "ADVISORY_INTEGRATED",
        "models": models,
        "alignment": {"BUY": buy, "SELL": sell, "primary": primary,
                       "evidence": evidence},
        "activeModels": active,
        "overlays": overlays,
        "hardGateImpact": "RANKING_ONLY",
        "summary": " / ".join(active) if active else "NO_IMAGE_MODEL_TRIGGER",
    }

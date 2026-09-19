# -*- coding: utf-8 -*-
"""R122: 多層構造文脈(親の仮説 / 子の構造 / 親子関係 / 短期予測)。

**純粋な計算だけ。** ネットワーク・台帳・発注・ファイル I/O に触らない。永続化は
``Memory`` が担うが、それも「呼び出し側が渡した辞書」を更新して返すだけで、書き込みは
呼び出し側(`monitor_publish`)が行う。設定(OFF / SHADOW / LIVE)は `msnr_gate` が
`execution_contract.json` の `structureContext` から読む。

設計の要点(`docs/R122_STRUCTURE_CONTEXT_AND_PARTICIPATION.md` §2):

  * **親**は「上位足の仮説」= 方向・目標・否定水準・寿命を持つ 1 件の構造。出所は
    (a) 直接取得した確定 15 分足(`snapshot.bars15m`)、(b) それが無ければ既存の
    `snapshot.htfContext`(45m/1h/4h/1d の確定足から `htf_context` が作った要約)。
    **3 分足から上位足を合成しない。** どちらも無ければ `CONTEXT_UNAVAILABLE`。
  * **子**は既存の 3 分足チェーン(`scan_chain` / `scan_flip`)の到達状態をそのまま使う。
    新しい検出器を足さない。
  * **順序**は `events` に観測できたものだけを積む。掃引が見えなかったブレイクを
    「AMD が完成した」とは書かない(`amdOrderComplete=False`)。AMDX の X は現行の
    正本に定義が無いので語彙ごと採らない。
  * **しきい値を新設しない。** 否定の許容は `msnr_gate` の `tol`(= max(touch_pt 2.0,
    0.10×ノイズ床))、ピボットは左右 2 本(`htf_context._pivots` / `ict_stdv` /
    研究 `structure_memory` と同じ)、鮮度窓は `htf_context.FRAME_MAX_AGE` の式
    (2×step + REFRESH_SLACK_SEC)をそのまま使う。中立幅は 1.0×ノイズ床
    (`msnr_gate.STOP_BUFFER_NF_MULT` と同じ倍率)。
  * **ID は根拠から決まる。** 同じ根拠なら再起動しても同じ ID。根拠が変われば新 ID。
    一度否定された構造は ``Memory`` が墓標を持ち、価格の再訪だけでは復活しない。
"""
from __future__ import annotations

import hashlib
import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

SCHEMA = "NQX_STRUCTURE_CONTEXT/1"
VERSION = "R122-STRUCTURE-CONTEXT-1"

BAR_SEC_3M = 180
STEP_15M = 900
#: `htf_context.FRAME_MAX_AGE` と同じ式。新しいしきい値ではない。
REFRESH_SLACK_SEC = 600
MAX_AGE_15M = 2 * STEP_15M + REFRESH_SLACK_SEC
#: 左右 2 本の厳密フラクタル。既存 3 箇所と同じ。
PIVOT_RADIUS = 2
#: 親の構造を読むのに必要な最小本数(`htf_context.MIN_BARS` と同じ)。
MIN_PARENT_BARS = 20
#: 中立幅の倍率。`msnr_gate.STOP_BUFFER_NF_MULT` と同じ 1.0。
NEUTRAL_NF_MULT = 1.0
#: 短期予測の評価対象(確定 3 分足の本数)。3 分 / 15 分。
NEAR_TERM_HORIZONS: Tuple[Tuple[str, int], ...] = (("3m", 1), ("15m", 5))

BIAS_VALUES = ("BUY", "SELL", "UNRESOLVED")
#: 観測できた段階だけ。**AMD / AMDX の語彙ではない。**
PHASES = ("UNKNOWN", "RANGE", "SWEEP_OBSERVED", "SHIFT_CONFIRMED", "EXPANSION_UNSWEPT")
CHILD_RELATIONS = ("PULLBACK", "CONTINUATION", "REVERSAL_CANDIDATE", "UNRESOLVED")
DIRECTIONS = ("UP", "DOWN", "NEUTRAL", "UNKNOWN")

#: 親の出所。表示と監査のため明示する。
SOURCE_BARS_15M = "BARS_15M"
SOURCE_HTF_SUMMARY = "HTF_FRAME_SUMMARY"

#: 既存の完成チェーン状態(`msnr_gate.COMPLETE_STATES` と同じ値。import はしない)。
COMPLETE_CHAIN_STATES = ("RETEST_HELD", "FLIP_HELD")
#: 構造確認まで進んだが保持は未確認。
CONFIRMED_CHAIN_STATES = ("MSS_CONFIRMED", "FLIP_ACCEPTED")
CHAIN_PROGRESS = {
    "EXPIRED": 0, "SWEEP_CANDIDATE": 1, "FLIP_BREAK": 1,
    "SWEEP_CONFIRMED": 2, "FLIP_ACCEPTED": 3, "MSS_CONFIRMED": 3,
    "RETEST_HELD": 4, "FLIP_HELD": 4,
}


# --------------------------------------------------------------- ユーティリティ

def _num(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _int(value: Any) -> Optional[int]:
    out = _num(value)
    return int(out) if out is not None else None


def _digest(*parts: Any) -> str:
    material = "|".join("" if p is None else str(p) for p in parts)
    return hashlib.sha1(material.encode("utf-8")).hexdigest()[:12]


def _round(value: Optional[float], nd: int = 2) -> Optional[float]:
    return None if value is None else round(value, nd)


def _norm_bars(rows: Any) -> List[Dict[str, float]]:
    """確定足だけを (t,o,h,l,c) へ正規化する。OHLC が壊れた行は捨てる。"""
    out: List[Dict[str, float]] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        t = _int(row.get("t", row.get("time")))
        o = _num(row.get("o", row.get("open")))
        h = _num(row.get("h", row.get("high")))
        l = _num(row.get("l", row.get("low")))
        c = _num(row.get("c", row.get("close")))
        if None in (t, o, h, l, c):
            continue
        if h < max(o, c) - 1e-9 or l > min(o, c) + 1e-9:
            continue
        out.append({"t": t, "o": o, "h": h, "l": l, "c": c})
    out.sort(key=lambda b: b["t"])
    return out


def pivots(bars: Sequence[Dict[str, float]], step: int,
           radius: int = PIVOT_RADIUS) -> List[Dict[str, Any]]:
    """左右 ``radius`` 本の厳密フラクタル。

    **右側の足が閉じるまで使えない。** `confirmedAt` = 右端の足の close 時刻で、
    これより前の時点でこのピボットを根拠にしてはならない(受け入れ試験
    「未確定 pivot を使わない」がここを見る)。
    """
    out: List[Dict[str, Any]] = []
    for i in range(radius, len(bars) - radius):
        window = bars[i - radius:i + radius + 1]
        bar = bars[i]
        right = bars[i + radius]
        confirmed = int(right["t"]) + int(step)
        if bar["h"] >= max(row["h"] for row in window) - 1e-9:
            out.append({"kind": "HIGH", "t": int(bar["t"]), "price": bar["h"],
                        "confirmedAt": confirmed, "index": i})
        if bar["l"] <= min(row["l"] for row in window) + 1e-9:
            out.append({"kind": "LOW", "t": int(bar["t"]), "price": bar["l"],
                        "confirmedAt": confirmed, "index": i})
    return out


# ------------------------------------------------------------------ 親の仮説

def _structure_from_pivots(piv: List[Dict[str, Any]]) -> Tuple[str, Optional[str]]:
    """HH/HL か LH/LL か。`htf_context.observe_frame` と同じ規則。"""
    highs = [p for p in piv if p["kind"] == "HIGH"]
    lows = [p for p in piv if p["kind"] == "LOW"]
    if len(highs) >= 2 and len(lows) >= 2:
        if highs[-1]["price"] > highs[-2]["price"] and lows[-1]["price"] > lows[-2]["price"]:
            return "UP", "BUY"
        if highs[-1]["price"] < highs[-2]["price"] and lows[-1]["price"] < lows[-2]["price"]:
            return "DOWN", "SELL"
    return "MIXED", None


def _events_for(bars: Sequence[Dict[str, float]], step: int, protected: Dict[str, Any],
                bias: str, tol: float) -> Tuple[List[Dict[str, Any]], str, bool]:
    """保護水準が確定した**後**の足だけを見て、観測できた事象と段階を返す。

    掃引(ヒゲで抜けて終値は戻る)と構造変化(終値で抜ける)を分け、順序が揃った
    ときだけ `amdOrderComplete=True` にする。順序が欠けたブレイクを補完しない。
    """
    events: List[Dict[str, Any]] = []
    level = protected["price"]
    start = protected["confirmedAt"]
    sweep_at: Optional[int] = None
    shift_at: Optional[int] = None
    events.append({"kind": "PROTECTED_LEVEL", "at": int(protected["t"]),
                   "knownAt": int(start), "price": _round(level)})
    for bar in bars:
        close_at = int(bar["t"]) + int(step)
        if close_at <= start:
            continue
        if bias == "BUY":
            wicked = bar["l"] < level - 1e-9
            closed_beyond = bar["c"] < level - tol
            reclaimed = bar["c"] > level + 1e-9
        else:
            wicked = bar["h"] > level + 1e-9
            closed_beyond = bar["c"] > level + tol
            reclaimed = bar["c"] < level - 1e-9
        if closed_beyond and shift_at is None:
            shift_at = close_at
            events.append({"kind": "CLOSE_BEYOND_PROTECTED", "at": int(bar["t"]),
                           "knownAt": close_at, "price": _round(bar["c"])})
            continue
        if wicked and reclaimed and sweep_at is None and shift_at is None:
            sweep_at = close_at
            events.append({"kind": "SWEEP_RECLAIMED", "at": int(bar["t"]),
                           "knownAt": close_at,
                           "price": _round(bar["l"] if bias == "BUY" else bar["h"])})
    if shift_at is not None:
        phase = "SHIFT_CONFIRMED" if sweep_at is not None else "EXPANSION_UNSWEPT"
    elif sweep_at is not None:
        phase = "SWEEP_OBSERVED"
    else:
        phase = "RANGE"
    return events, phase, bool(sweep_at is not None and shift_at is not None)


def _parent_from_bars(bars: Sequence[Dict[str, float]], step: int, timeframe: str,
                      now: float, price: Optional[float], tol: float,
                      symbol: Optional[str], contract: Optional[str],
                      session_id: Optional[str]) -> Optional[Dict[str, Any]]:
    """確定 15 分足から親の仮説を作る(fidelity=BARS)。"""
    if len(bars) < MIN_PARENT_BARS:
        return None
    last = bars[-1]
    as_of = int(last["t"]) + int(step)
    max_age = 2 * int(step) + REFRESH_SLACK_SEC
    if now - as_of > max_age:
        return None                                   # STALE: 補完しない
    piv = pivots(bars, step)
    structure, bias = _structure_from_pivots(piv)
    if bias is None:
        return None                                   # MIXED は仮説を立てない
    highs = [p for p in piv if p["kind"] == "HIGH"]
    lows = [p for p in piv if p["kind"] == "LOW"]
    protected = lows[-1] if bias == "BUY" else highs[-1]
    opposite = highs[-1] if bias == "BUY" else lows[-1]
    sample = bars[-MIN_PARENT_BARS:]
    rng_hi = max(b["h"] for b in sample)
    rng_lo = min(b["l"] for b in sample)
    # 目標は「向かう側の直近ピボット」。既に抜けていればレンジ端。新しい係数は作らない。
    objective_price = opposite["price"]
    objective_kind = "PIVOT_HIGH" if bias == "BUY" else "PIVOT_LOW"
    if price is not None and ((bias == "BUY" and price >= objective_price)
                              or (bias == "SELL" and price <= objective_price)):
        objective_price = rng_hi if bias == "BUY" else rng_lo
        objective_kind = "RANGE_HIGH" if bias == "BUY" else "RANGE_LOW"
    events, phase, ordered = _events_for(bars, step, protected, bias, tol)
    source_hash = _digest(len(bars), bars[0]["t"], last["t"], _round(last["c"], 4),
                          _round(rng_hi, 4), _round(rng_lo, 4))
    return _assemble_parent(
        timeframe=timeframe, source=SOURCE_BARS_15M, fidelity="BARS", bias=bias,
        structure=structure, phase=phase, ordered=ordered, events=events,
        protected=protected, objective_price=objective_price, objective_kind=objective_kind,
        rng_hi=rng_hi, rng_lo=rng_lo, as_of=as_of, expires_at=as_of + max_age,
        price=price, symbol=symbol, contract=contract, session_id=session_id,
        source_hash=source_hash, now=now, step=step, tol=tol)


def _parent_from_htf(htf: Dict[str, Any], now: float, price: Optional[float], tol: float,
                     symbol: Optional[str], contract: Optional[str],
                     session_id: Optional[str]) -> Optional[Dict[str, Any]]:
    """既存 `htfContext` の要約から親の仮説を作る(fidelity=SUMMARY)。

    アンカーは「集計 bias と同じ向きの、重みが最大の有効フレーム」。そのフレームの
    `range20High/Low` が唯一使える構造境界なので、保護水準と目標はそこから採る。
    生足が無いので `phase` は `UNKNOWN` のまま(段階を推測しない)。
    """
    if not isinstance(htf, dict):
        return None
    bias = htf.get("bias")
    if bias not in ("BUY", "SELL") or str(htf.get("status") or "") != "ALIGNED":
        return None
    weights = {"45m": 1, "1h": 2, "4h": 3, "1d": 3}
    frames = htf.get("frames") if isinstance(htf.get("frames"), dict) else {}
    usable = [(weights.get(name, 0), name, row) for name, row in frames.items()
              if isinstance(row, dict) and row.get("valid") and row.get("direction") == bias]
    if not usable:
        return None
    usable.sort(key=lambda item: (item[0], item[1]), reverse=True)
    _weight, name, frame = usable[0]
    rng_hi, rng_lo = _num(frame.get("range20High")), _num(frame.get("range20Low"))
    if rng_hi is None or rng_lo is None or rng_hi <= rng_lo:
        return None
    as_of = _epoch_of(frame.get("asOf"))
    if as_of is None:
        return None
    step = {"45m": 2700, "1h": 3600, "4h": 14400, "1d": 86400}.get(name, 3600)
    max_age = 2 * step + REFRESH_SLACK_SEC
    protected = {"kind": "RANGE_LOW" if bias == "BUY" else "RANGE_HIGH",
                 "t": int(as_of) - step, "price": rng_lo if bias == "BUY" else rng_hi,
                 "confirmedAt": int(as_of)}
    objective_price = rng_hi if bias == "BUY" else rng_lo
    source_hash = _digest(name, frame.get("asOf"), _round(rng_hi, 4), _round(rng_lo, 4),
                          frame.get("structure"), frame.get("ema20"))
    return _assemble_parent(
        timeframe=name, source=SOURCE_HTF_SUMMARY, fidelity="SUMMARY", bias=bias,
        structure=str(frame.get("structure") or "MIXED"), phase="UNKNOWN", ordered=False,
        events=[{"kind": "PROTECTED_LEVEL", "at": protected["t"],
                 "knownAt": protected["confirmedAt"], "price": _round(protected["price"])}],
        protected=protected, objective_price=objective_price,
        objective_kind="RANGE_HIGH" if bias == "BUY" else "RANGE_LOW",
        rng_hi=rng_hi, rng_lo=rng_lo, as_of=int(as_of), expires_at=int(as_of) + max_age,
        price=price, symbol=symbol, contract=contract, session_id=session_id,
        source_hash=source_hash, now=now, step=step, tol=tol)


def _assemble_parent(**kw: Any) -> Dict[str, Any]:
    bias = kw["bias"]
    protected = kw["protected"]
    rng_hi, rng_lo = kw["rng_hi"], kw["rng_lo"]
    price = kw["price"]
    # ID は「どの足のどの水準を守る、どちら向きの仮説か」だけで決まる。要約由来は
    # ピボット時刻を持たないので None を入れる(フレームが閉じるたびに ID が動かない)。
    anchor_t = protected["t"] if kw["fidelity"] == "BARS" else None
    structure_id = _digest(SCHEMA, kw["symbol"], kw["timeframe"], bias,
                           _round(protected["price"], 4), anchor_t)
    invalidated_at = next((e["knownAt"] for e in kw["events"]
                           if e["kind"] == "CLOSE_BEYOND_PROTECTED"), None)
    consumed_at = None
    if price is not None and ((bias == "BUY" and price >= kw["objective_price"])
                              or (bias == "SELL" and price <= kw["objective_price"])):
        consumed_at = int(kw["now"])
    side_word = "下" if bias == "BUY" else "上"
    return {
        "schemaVersion": SCHEMA, "contextVersion": VERSION,
        "structureId": structure_id, "parentId": None,
        "evidenceRootId": _digest(SCHEMA, kw["symbol"], kw["timeframe"], kw["source_hash"]),
        "symbol": kw["symbol"], "contract": kw["contract"], "timeframe": kw["timeframe"],
        "sessionId": kw["session_id"], "sourceHash": kw["source_hash"],
        "source": kw["source"], "fidelity": kw["fidelity"], "stepSec": int(kw["step"]),
        "observedAt": int(kw["now"]), "knownAt": int(protected["confirmedAt"]),
        "confirmedAt": int(kw["as_of"]), "expiresAt": int(kw["expires_at"]),
        "bias": bias, "phase": kw["phase"], "structure": kw["structure"],
        "amdOrderComplete": bool(kw["ordered"]),
        "range": {"high": _round(rng_hi), "low": _round(rng_lo),
                  "eq": _round((rng_hi + rng_lo) / 2.0)},
        "sweptLiquidity": next(({"price": e["price"], "knownAt": e["knownAt"]}
                                for e in kw["events"] if e["kind"] == "SWEEP_RECLAIMED"), None),
        "brokenLevel": next(({"price": e["price"], "knownAt": e["knownAt"]}
                             for e in kw["events"] if e["kind"] == "CLOSE_BEYOND_PROTECTED"), None),
        "protectedLevel": {"price": _round(protected["price"]), "kind": protected["kind"],
                           "barT": protected["t"], "confirmedAt": int(protected["confirmedAt"])},
        "objective": {"price": _round(kw["objective_price"]), "kind": kw["objective_kind"],
                      "distancePt": _round(abs(kw["objective_price"] - price)) if price is not None else None},
        "invalidationRule": (f"{kw['timeframe']} 確定終値が {_round(protected['price'])} を"
                             f"{side_word}抜け(許容 {round(kw['tol'], 2)}pt)"),
        "invalidatedAt": invalidated_at, "touchedAt": None, "consumedAt": consumed_at,
        "events": kw["events"], "childRelation": "UNRESOLVED",
    }


def _epoch_of(value: Any) -> Optional[float]:
    number = _num(value)
    if number is not None:
        return number / 1000.0 if number > 10_000_000_000 else number
    if not value:
        return None
    from datetime import datetime, timezone
    try:
        raw = str(value).strip().replace("Z", "+00:00")
        parsed = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc).timestamp()


def parent_thesis(bundle: Dict[str, Any], now: float, price: Optional[float], tol: float,
                  bars15m: Any = None) -> Tuple[Optional[Dict[str, Any]], List[str]]:
    """親の仮説を 1 件返す。作れない理由は 2 要素目に残す。"""
    snapshot = bundle.get("snapshot") if isinstance(bundle.get("snapshot"), dict) else {}
    symbol = bundle.get("sourceSymbol") or snapshot.get("symbol")
    contract = bundle.get("sourceContract") or bundle.get("sourceFrontContract")
    session_id = snapshot.get("sessionId") or bundle.get("sessionId")
    reasons: List[str] = []
    rows = bars15m if bars15m is not None else snapshot.get("bars15m")
    bars = _norm_bars(rows)
    if bars:
        parent = _parent_from_bars(bars, STEP_15M, "15m", now, price, tol,
                                   symbol, contract, session_id)
        if parent:
            return parent, reasons
        reasons.append("BARS15M_UNUSABLE")
    else:
        reasons.append("BARS15M_MISSING")
    parent = _parent_from_htf(snapshot.get("htfContext") or {}, now, price, tol,
                              symbol, contract, session_id)
    if parent:
        return parent, reasons
    reasons.append("HTF_SUMMARY_UNUSABLE")
    return None, reasons


# -------------------------------------------------------------------- 子の構造

def child_structures(result: Dict[str, Any], bars: Sequence[Dict[str, float]],
                     price: Optional[float], symbol: Optional[str],
                     session_id: Optional[str], now: float) -> Dict[str, Any]:
    """既存 3 分チェーンのうち最も進んだものを、全体で 1 本 / 方向ごとに 1 本返す。

    新しい検出器は足さない。``best`` は親子関係の判定に、``bySide`` は代替候補
    (§4.2)のように「親と同じ方向の確認済み構造」が要る場面に使う。
    """
    best: Optional[Tuple[Tuple[int, int], Dict[str, Any], Dict[str, Any]]] = None
    per_side: Dict[str, Tuple[Tuple[int, int], Dict[str, Any], Dict[str, Any]]] = {}
    for level in result.get("levels") or []:
        if not isinstance(level, dict) or level.get("dynamic"):
            continue
        for chain in level.get("chains") or []:
            state = str(chain.get("state") or "")
            rank = CHAIN_PROGRESS.get(state, 0)
            if rank <= 0:
                continue
            origin = chain.get("sweepBarT") if chain.get("type") == "SWEEP" else chain.get("breakBarT")
            key = (rank, int(origin or 0))
            if best is None or key > best[0]:
                best = (key, level, chain)
            side = chain.get("side")
            if side in ("BUY", "SELL") and (side not in per_side or key > per_side[side][0]):
                per_side[side] = (key, level, chain)
    out: Dict[str, Any] = {"best": None, "bySide": {}}
    if best is not None:
        out["best"] = _child_node(best[1], best[2], bars, price, symbol, session_id, now)
    for side, item in per_side.items():
        out["bySide"][side] = _child_node(item[1], item[2], bars, price, symbol, session_id, now)
    return out


def _child_node(level: Dict[str, Any], chain: Dict[str, Any], bars: Sequence[Dict[str, float]],
                price: Optional[float], symbol: Optional[str], session_id: Optional[str],
                now: float) -> Dict[str, Any]:
    state = str(chain.get("state"))
    side = chain.get("side")
    origin = chain.get("sweepBarT") if chain.get("type") == "SWEEP" else chain.get("breakBarT")
    events: List[Dict[str, Any]] = []
    for kind, key in (("ORIGIN", "sweepBarT"), ("ORIGIN", "breakBarT"),
                      ("STRUCTURE_SHIFT", "mssBarT"), ("RETEST_HELD", "retestBarT"),
                      ("ACCEPTED_HELD", "holdBarT")):
        t = _int(chain.get(key))
        if t is not None:
            events.append({"kind": kind, "at": t, "knownAt": t + BAR_SEC_3M})
    events.sort(key=lambda e: e["knownAt"])
    level_price = _num(level.get("price"))
    structure_id = _digest(SCHEMA, symbol, "3m", side, _round(level_price, 4), origin)
    return {
        "schemaVersion": SCHEMA, "structureId": structure_id, "timeframe": "3m",
        "symbol": symbol, "sessionId": session_id, "observedAt": int(now),
        "bias": side if side in ("BUY", "SELL") else "UNRESOLVED",
        "state": state, "type": chain.get("type"),
        "level": level.get("label"), "levelPrice": _round(level_price),
        "originBarT": _int(origin),
        "knownAt": int(origin) + BAR_SEC_3M if origin is not None else None,
        "confirmedAt": (events[-1]["knownAt"] if events else None),
        "complete": state in COMPLETE_CHAIN_STATES,
        "confirmed": state in COMPLETE_CHAIN_STATES or state in CONFIRMED_CHAIN_STATES,
        "barsLeft": _int(chain.get("barsLeft")),
        "events": events,
        "lastClose": _round(bars[-1]["c"]) if bars else None,
        "price": _round(price),
    }


def relate(parent: Optional[Dict[str, Any]], child: Optional[Dict[str, Any]],
           price: Optional[float]) -> str:
    """親子関係。逆方向を「矛盾だから不成立」にはしない。"""
    if not parent or not child:
        return "UNRESOLVED"
    pbias, cbias = parent.get("bias"), child.get("bias")
    if cbias not in ("BUY", "SELL") or pbias not in ("BUY", "SELL"):
        return "UNRESOLVED"
    if cbias == pbias:
        return "CONTINUATION"
    if parent.get("invalidatedAt"):
        return "REVERSAL_CANDIDATE"
    protected = (parent.get("protectedLevel") or {}).get("price")
    objective = (parent.get("objective") or {}).get("price")
    if price is None or protected is None or objective is None:
        return "UNRESOLVED"
    inside = (min(protected, objective) - 1e-9 <= price <= max(protected, objective) + 1e-9)
    return "PULLBACK" if inside else "REVERSAL_CANDIDATE"


# ------------------------------------------------------------------ 短期予測

def near_term(parent: Optional[Dict[str, Any]], child: Optional[Dict[str, Any]],
              relation: str, bars: Sequence[Dict[str, float]],
              noise: Optional[float]) -> Dict[str, Any]:
    """次の 1 本 / 5 本の確定 3 分足に対する方向。**表示・評価専用。**

    評価対象の時刻と基準価格を固定し、中立幅は既存のボラ尺度(ノイズ床)で事前に
    決める。未検証の確率は付けない。参加の可否をここで決めてはならない。
    """
    out: Dict[str, Any] = {
        "schemaVersion": SCHEMA, "gate": False, "usedForParticipation": False,
        "direction": "UNKNOWN", "basis": [], "horizons": [],
        "neutralBandPt": None, "referencePrice": None, "referenceBarT": None,
    }
    if not bars:
        out["basis"].append("NO_BARS")
        return out
    last = bars[-1]
    band = None
    if noise is not None and noise > 0:
        band = round(NEUTRAL_NF_MULT * float(noise), 2)
    out.update({"referencePrice": _round(last["c"]), "referenceBarT": int(last["t"]),
                "neutralBandPt": band})
    out["horizons"] = [{"label": label, "bars": n,
                        "evaluateAtT": int(last["t"]) + BAR_SEC_3M * (n + 1)}
                       for label, n in NEAR_TERM_HORIZONS]
    if band is None:
        out["basis"].append("NO_NOISE_FLOOR")
        return out
    bias = None
    if child and child.get("complete") and child.get("bias") in ("BUY", "SELL"):
        bias = child["bias"]
        out["basis"].append("CHILD_COMPLETE")
    elif relation == "PULLBACK":
        out["direction"] = "NEUTRAL"
        out["basis"].append("PARENT_PULLBACK")
        return out
    elif parent and parent.get("bias") in ("BUY", "SELL") and not parent.get("invalidatedAt"):
        bias = parent["bias"]
        out["basis"].append("PARENT_BIAS")
        if relation == "CONTINUATION":
            out["basis"].append("CHILD_CONTINUATION")
    if bias is None:
        out["basis"].append("NO_DIRECTIONAL_INPUT")
        return out
    out["direction"] = "UP" if bias == "BUY" else "DOWN"
    return out


def score_near_term(forecast: Dict[str, Any], future_bars: Sequence[Dict[str, float]]) -> Dict[str, Any]:
    """予測を後続の確定足で採点する(再生と報告専用。本番経路では呼ばない)。"""
    out: Dict[str, Any] = {"scored": [], "direction": forecast.get("direction")}
    ref = _num(forecast.get("referencePrice"))
    band = _num(forecast.get("neutralBandPt"))
    if ref is None or band is None:
        return out
    by_t = {int(b["t"]): b for b in future_bars}
    for horizon in forecast.get("horizons") or []:
        n = int(horizon.get("bars") or 0)
        target_t = int(forecast["referenceBarT"]) + BAR_SEC_3M * n
        bar = by_t.get(target_t)
        if bar is None:
            out["scored"].append({"label": horizon.get("label"), "actual": "UNAVAILABLE"})
            continue
        move = bar["c"] - ref
        actual = "NEUTRAL" if abs(move) <= band else ("UP" if move > 0 else "DOWN")
        out["scored"].append({"label": horizon.get("label"), "actual": actual,
                              "movePt": _round(move), "hit": actual == forecast.get("direction")})
    return out


# -------------------------------------------------------------- 全体の組み立て

def build(bundle: Dict[str, Any], result: Dict[str, Any], bars: Sequence[Dict[str, float]],
          ict: Optional[Dict[str, Any]], noise: Optional[float], tol: float,
          now: Optional[float] = None, memory: Optional[Dict[str, Any]] = None,
          bars15m: Any = None) -> Dict[str, Any]:
    """1 周期ぶんの多層文脈。副作用なし。``memory`` は読むだけで、更新は ``Memory``。"""
    snapshot = bundle.get("snapshot") if isinstance(bundle.get("snapshot"), dict) else {}
    price = _num(bundle.get("price"))
    if now is None:
        now = _epoch_of(bundle.get("at"))
    if now is None:
        now = (bars[-1]["t"] + BAR_SEC_3M) if bars else 0
    symbol = bundle.get("sourceSymbol") or snapshot.get("symbol")
    session_id = snapshot.get("sessionId") or bundle.get("sessionId")

    parent, reasons = parent_thesis(bundle, now, price, tol, bars15m=bars15m)
    children = child_structures(result, bars, price, symbol, session_id, now)
    child = children.get("best")
    if parent is not None:
        parent = dict(parent)
        # 足中の接触は終値否定と分けて持つ(3 分足のヒゲで親を殺さない)。
        protected = (parent.get("protectedLevel") or {}).get("price")
        if protected is not None and bars:
            start = parent["protectedLevel"]["confirmedAt"]
            for bar in bars:
                if int(bar["t"]) + BAR_SEC_3M <= start:
                    continue
                touched = (bar["l"] < protected - 1e-9) if parent["bias"] == "BUY" \
                    else (bar["h"] > protected + 1e-9)
                if touched:
                    parent["touchedAt"] = int(bar["t"]) + BAR_SEC_3M
                    break
        # 要約由来の親は自分の足を持たないので、否定は**確定 3 分足の終値**で見る。
        # ヒゲ(touchedAt)とは別扱いであることを規則名で明示する。
        if parent["fidelity"] == "SUMMARY" and parent.get("invalidatedAt") is None and bars:
            start = parent["protectedLevel"]["confirmedAt"]
            for bar in bars:
                close_at = int(bar["t"]) + BAR_SEC_3M
                if close_at <= start:
                    continue
                beyond = (bar["c"] < protected - tol) if parent["bias"] == "BUY" \
                    else (bar["c"] > protected + tol)
                if beyond:
                    parent["invalidatedAt"] = close_at
                    parent["brokenLevel"] = {"price": _round(bar["c"]), "knownAt": close_at}
                    parent["events"] = list(parent["events"]) + [
                        {"kind": "CLOSE_BEYOND_PROTECTED", "at": int(bar["t"]),
                         "knownAt": close_at, "price": _round(bar["c"])}]
                    parent["phase"] = "EXPANSION_UNSWEPT" if parent["phase"] in ("UNKNOWN", "RANGE") \
                        else parent["phase"]
                    break
            parent["invalidationRule"] = (
                f"3m 確定終値が {_round(protected)} を"
                f"{'下' if parent['bias'] == 'BUY' else '上'}抜け(許容 {round(tol, 2)}pt・要約由来)")
        if parent.get("expiresAt") is not None and now > parent["expiresAt"]:
            parent["expired"] = True
        # 記憶: 一度否定された構造は価格の再訪だけでは戻らない。初出時刻も記憶が正本。
        remembered = (memory or {}).get(parent["structureId"]) if isinstance(memory, dict) else None
        if isinstance(remembered, dict):
            if remembered.get("invalidatedAt") and not parent.get("invalidatedAt"):
                parent["invalidatedAt"] = remembered["invalidatedAt"]
                parent["revivalBlocked"] = True
            if remembered.get("firstSeenAt"):
                parent["firstSeenAt"] = remembered["firstSeenAt"]
            if remembered.get("consumedAt") and not parent.get("consumedAt"):
                parent["consumedAt"] = remembered["consumedAt"]
        else:
            parent["firstSeenAt"] = int(now)
    relation = relate(parent, child, price)
    if parent is not None:
        parent["childRelation"] = relation
        for node in [child, *children.get("bySide", {}).values()]:
            if node is not None:
                node["parentId"] = parent["structureId"]
    forecast = near_term(parent, child, relation, bars, noise)
    status = "OK" if parent is not None else "CONTEXT_UNAVAILABLE"
    return {
        "schemaVersion": SCHEMA, "contextVersion": VERSION, "status": status,
        "reasons": reasons, "at": int(now), "price": _round(price),
        "noiseFloor": _round(noise), "tolPt": round(tol, 2),
        "thesis": parent, "child": child, "children": children.get("bySide") or {},
        "childRelation": relation, "nearTerm": forecast,
        "background": _background(snapshot),
    }


def _background(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    htf = snapshot.get("htfContext") if isinstance(snapshot.get("htfContext"), dict) else {}
    frames = htf.get("frames") if isinstance(htf.get("frames"), dict) else {}
    return {
        "htfStatus": htf.get("status"), "htfBias": htf.get("bias"),
        "htfComplete": bool(htf.get("complete")),
        "frames": {name: {"direction": row.get("direction"), "structure": row.get("structure"),
                          "valid": bool(row.get("valid")), "asOf": row.get("asOf")}
                   for name, row in frames.items() if isinstance(row, dict)},
        "bars15m": _int(snapshot.get("bars15mCount")) if snapshot.get("bars15m") is None
        else len(snapshot.get("bars15m") or []),
    }


def compact(context: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """監査バンドル / カードへ載せる縮約(親の全 events は持たない)。"""
    if not isinstance(context, dict):
        return None
    thesis = context.get("thesis") or {}
    child = context.get("child") or {}
    forecast = context.get("nearTerm") or {}
    return {
        "v": context.get("contextVersion"), "status": context.get("status"),
        "reasons": list(context.get("reasons") or [])[:4],
        "thesisId": thesis.get("structureId"), "tf": thesis.get("timeframe"),
        "fidelity": thesis.get("fidelity"), "bias": thesis.get("bias"),
        "phase": thesis.get("phase"), "amdOrdered": thesis.get("amdOrderComplete"),
        "protected": (thesis.get("protectedLevel") or {}).get("price"),
        "objective": (thesis.get("objective") or {}).get("price"),
        "invalidatedAt": thesis.get("invalidatedAt"), "touchedAt": thesis.get("touchedAt"),
        "consumedAt": thesis.get("consumedAt"), "expiresAt": thesis.get("expiresAt"),
        "revivalBlocked": bool(thesis.get("revivalBlocked")),
        "childId": child.get("structureId"), "childState": child.get("state"),
        "childBias": child.get("bias"), "relation": context.get("childRelation"),
        "nearTerm": {"direction": forecast.get("direction"),
                     "bandPt": forecast.get("neutralBandPt"),
                     "refT": forecast.get("referenceBarT"),
                     "basis": list(forecast.get("basis") or [])[:3],
                     "gate": False},
    }


# ---------------------------------------------------------------------- 記憶

MEMORY_SCHEMA = "NQX_STRUCTURE_MEMORY/1"
#: 墓標の保持期間。親の寿命(最長 1d フレームの 2×step+slack)より十分長い 1 取引週。
MEMORY_TTL_SEC = 7 * 24 * 3600
MEMORY_MAX_ROWS = 256


class Memory:
    """構造の初出・否定・消化を跨いで覚える。**発注台帳とは別ファイル。**

    このクラスは辞書を更新して返すだけで、読み書きは呼び出し側が行う
    (`monitor_publish` が `.secrets/structure_context_state.json` を使う)。壊れた入力は
    「記憶なし」として扱い、周期を止めない。
    """

    def __init__(self, state: Any = None):
        rows = {}
        if isinstance(state, dict) and str(state.get("schema") or "") == MEMORY_SCHEMA:
            raw = state.get("structures")
            if isinstance(raw, dict):
                for key, value in raw.items():
                    if isinstance(value, dict) and isinstance(key, str):
                        rows[key] = dict(value)
        self.structures: Dict[str, Dict[str, Any]] = rows

    def as_dict(self) -> Dict[str, Any]:
        return {"schema": MEMORY_SCHEMA, "version": VERSION, "structures": self.structures}

    def snapshot(self) -> Dict[str, Any]:
        """`build()` へ渡す読み取り専用の写し。"""
        return {key: dict(value) for key, value in self.structures.items()}

    def observe(self, context: Optional[Dict[str, Any]], now: Optional[float] = None) -> Dict[str, Any]:
        """1 周期の観測を取り込む。否定・消化は**一度立ったら下ろさない**。"""
        if not isinstance(context, dict):
            return self.as_dict()
        thesis = context.get("thesis")
        if now is None:
            now = _num(context.get("at")) or 0
        if isinstance(thesis, dict) and thesis.get("structureId"):
            key = str(thesis["structureId"])
            row = self.structures.get(key) or {
                "firstSeenAt": int(thesis.get("firstSeenAt") or now),
                "timeframe": thesis.get("timeframe"), "bias": thesis.get("bias"),
                "protected": (thesis.get("protectedLevel") or {}).get("price"),
                "source": thesis.get("source"), "fidelity": thesis.get("fidelity"),
                "evidenceRootId": thesis.get("evidenceRootId"),
            }
            row["lastSeenAt"] = int(now)
            for field in ("invalidatedAt", "consumedAt"):
                value = thesis.get(field)
                if value and not row.get(field):
                    row[field] = int(value)
            self.structures[key] = row
        self._prune(now)
        return self.as_dict()

    def _prune(self, now: float) -> None:
        if len(self.structures) <= MEMORY_MAX_ROWS:
            stale = [k for k, v in self.structures.items()
                     if now - _num(v.get("lastSeenAt")) > MEMORY_TTL_SEC
                     if _num(v.get("lastSeenAt")) is not None]
            for key in stale:
                self.structures.pop(key, None)
            return
        order = sorted(self.structures.items(),
                       key=lambda item: _num(item[1].get("lastSeenAt")) or 0, reverse=True)
        self.structures = {key: value for key, value in order[:MEMORY_MAX_ROWS]}

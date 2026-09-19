# -*- coding: utf-8 -*-
"""R121: ICT Standard Deviation Projections(TTrades 版)の固定投影と参加判断。

**純粋な計算だけ。** ネットワーク・台帳・発注・ファイル I/O に触らない。呼び出し側
(`msnr_gate`)が `execution_contract.json` の `ictStdv` でモード(OFF / SHADOW / LIVE)を
決める。根拠と採用仕様は `docs/R121_ICT_STDV.md`、出典調査は
`docs/reports/ICT_STDV_SOURCE_AND_CODE_AUDIT_2026-09-19.md`。

出典(P1 4 ページ目の上向き / 7 ページ目の下向きの図で向きを確認済み):

    level(r) = p0 + r * (p1 - p0)

    BUY 投影:  p0 = 下方向の manipulation が始まった高値
               p1 = その動きが掃引した安値
    SELL 投影: p0 = 上方向の manipulation が始まった安値
               p1 = その動きが掃引した高値

`-2` は p0 から投影方向へ 2 幅で、**p1 からは 3 幅**。「掃引極値から 2 倍」と実装すると
1 幅ずれる。係数は Fib のラベルであって時系列の順番ではない。

**これはリスク倍率 R ではない。** STDV の `-2` と `TP=2R` は別物。R の分母は最終建値と
構造 SL の距離で、投影幅とは独立。統計の σ(Bollinger 等)とも別物で、±2σ の確率や
正規分布の包含率は持ち込まない。

Nightwatch 版として決めたこと(動画の規則ではない。`docs/R121_ICT_STDV.md` §1 の表):
  * 3 分足の確定足に適用する(動画後半の 2 分足例を再現したとは言わない)
  * アンカー源は**既存の確定済み SWEEP チェーン**(`MSS_CONFIRMED` / `RETEST_HELD`)だけ
  * `p0` は掃引足から遡って見つけた**確定フラクタル・ピボット**の極値
  * `knownAt` = 構造確認足の終値時刻と、ピボットの右側が閉じた時刻の**遅い方**
  * 生価格と 0.25tick 丸め価格を両方持つ。丸めは利確側に保守的(BUY は下、SELL は上)
"""
from __future__ import annotations

import hashlib
import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

SCHEMA = "NQX_ICT_STDV/1"
VERSION = "R121-ICT-STDV-1"
TICK = 0.25
BAR_SECONDS = 180

#: V1 が図示する係数。`strategy_models.FIB_SD_RATIOS` は `-3/-5` も持つ別系列で、
#: 今回の出典の基本設定と同一ではない(だから別スキーマにする)。
RATIOS: Tuple[float, ...] = (1.0, 0.0, -1.0, -2.0, -2.5, -4.0)
#: 反応を観察する領域(V1 03:46)。到達保証でも反転命令でもない。
REACTION_RATIOS: Tuple[float, ...] = (-2.0, -2.5)
#: 目標候補として既存 `model_targets` に渡してよい係数。`-4` は既定で観測専用
#: (`docs/R121_ICT_STDV.md` §4)。`0` / `1` はアンカー内部なので目標にしない。
TARGET_RATIOS: Tuple[float, ...] = (-1.0, -2.0, -2.5)

#: 既定パラメータ。契約 `ictStdv.params` で上書きできる。
DEFAULT_PARAMS: Dict[str, Any] = {
    "pivotBars": 2,          # フラクタル・ピボットの片側本数
    "maxLegBars": 40,        # 掃引足から p0 を探す最大遡り本数
    "minLegTicks": 8,        # これ未満の幅は退化扱い(投影が潰れる)
    "minLegNf": 0.5,         # ノイズ床に対する最小幅の倍率
    "maxAgeBars": 120,       # アンカーの寿命(確認足から)
}

PARTICIPATION_STATES = ("ENTRY_READY", "WAIT_FOR_PULLBACK", "NO_ROOM", "INVALIDATED")


# --------------------------------------------------------------- 基本ユーティリティ

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


def _round_to_tick(value: float, side: Optional[str], tick: float = TICK) -> float:
    """利確の実行価格として保守的に丸める。BUY は下、SELL は上。

    出典の指定ではなく実装側の選択(`docs/reports/…AUDIT…` §3)。side が無い
    (アンカー内部の `0`/`1` など)ときは最近傍へ丸める。
    """
    if tick <= 0:
        return value
    quotient = value / tick
    if side == "BUY":
        stepped = math.floor(quotient + 1e-9)
    elif side == "SELL":
        stepped = math.ceil(quotient - 1e-9)
    else:
        stepped = math.floor(quotient + 0.5)
    return round(stepped * tick, 10)


def project(p0: Any, p1: Any, side: Optional[str] = None,
            ratios: Sequence[float] = RATIOS, tick: float = TICK) -> Optional[Dict[str, Any]]:
    """`level(r) = p0 + r*(p1-p0)` を全係数で返す。不正入力は None。

    戻り値の ``rows`` は係数の順で、各行に生価格 ``raw`` と丸め価格 ``price`` を持つ。
    丸めで同値になった行は ``collapsedWith`` に相手の係数を記録する —— **丸めで潰れた
    2 本を別の目標として扱わないため**(受け入れ試験「半tick/重複/異常値」)。
    """
    a, b = _num(p0), _num(p1)
    if a is None or b is None:
        return None
    width = b - a
    if width == 0 or not math.isfinite(width):
        return None
    rows: List[Dict[str, Any]] = []
    seen: Dict[float, float] = {}
    for ratio in ratios:
        r = _num(ratio)
        if r is None:
            continue
        raw = a + r * width
        if not math.isfinite(raw) or raw <= 0:
            return None
        priced = _round_to_tick(raw, side if r < 0 else None, tick)
        row = {"ratio": r, "raw": round(raw, 6), "price": priced,
               "reaction": r in REACTION_RATIOS, "collapsedWith": None}
        if priced in seen:
            row["collapsedWith"] = seen[priced]
        else:
            seen[priced] = r
        rows.append(row)
    if not rows:
        return None
    return {"p0": round(a, 6), "p1": round(b, 6), "widthPt": round(abs(width), 6),
            "side": side, "tick": tick, "rows": rows,
            "byRatio": {row["ratio"]: row["price"] for row in rows}}


# ------------------------------------------------------------------- アンカー抽出

def _pivot_index(bars: Sequence[Dict[str, float]], sweep_idx: int, side: str,
                 k: int, max_back: int) -> Optional[int]:
    """manipulation レッグの出発点。掃引足から遡った**確定フラクタル・ピボット**。

    BUY(安値を掃引した)なら、その下降レッグが始まった高値。ピボットは左右 k 本より
    高い(低い)こと、かつ右側 k 本が確定していることを要求する。該当が無ければ None
    —— **合成しない**。窓内の単なる最大値では、右側が閉じていないスイングを過去へ
    戻して使ってしまう。
    """
    if k < 1 or sweep_idx <= k:
        return None
    lo = max(k, sweep_idx - max_back)
    best: Optional[int] = None
    for i in range(sweep_idx - 1, lo - 1, -1):
        if i + k > sweep_idx:            # 右側が掃引足より後 = このレッグの外
            continue
        left = bars[i - k:i]
        right = bars[i + 1:i + k + 1]
        if len(left) < k or len(right) < k:
            continue
        if side == "BUY":
            if bars[i]["h"] >= max(b["h"] for b in left) and \
               bars[i]["h"] > max(b["h"] for b in right):
                if best is None or bars[i]["h"] > bars[best]["h"]:
                    best = i
        else:
            if bars[i]["l"] <= min(b["l"] for b in left) and \
               bars[i]["l"] < min(b["l"] for b in right):
                if best is None or bars[i]["l"] < bars[best]["l"]:
                    best = i
    return best


def _anchor_id(material: Sequence[Any]) -> str:
    text = "|".join("" if x is None else str(x) for x in material)
    return "stdv_" + hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def find_anchor(chain: Optional[Dict[str, Any]], bars: Optional[Sequence[Dict[str, float]]],
                *, noise_floor: Optional[float] = None, params: Optional[Dict[str, Any]] = None,
                symbol: Optional[str] = None, session_id: Optional[str] = None,
                source_tf: str = "3", bar_seconds: int = BAR_SECONDS,
                level: Optional[Dict[str, Any]] = None,
                liquidity_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """確定済み SWEEP チェーンから STDV アンカーを 1 本作る。作れなければ None。

    ``chain`` は `msnr_gate` の SWEEP チェーン(`state` が `MSS_CONFIRMED` /
    `RETEST_HELD`)。それ以外の型・状態では**アンカーを作らない** —— 掃引だけ、
    戻りなしの候補から参加可能な STDV を作らないため(受け入れ試験)。

    失敗理由が要るときは `anchor_diagnosis` を使う(こちらは None を返すだけ)。
    """
    out = anchor_diagnosis(chain, bars, noise_floor=noise_floor, params=params,
                           symbol=symbol, session_id=session_id, source_tf=source_tf,
                           bar_seconds=bar_seconds, level=level, liquidity_id=liquidity_id)
    return out.get("anchor")


#: アンカーを作れない理由。集計で「根拠欠損件数」として数える。
MISSING_REASONS = (
    "CHAIN_MISSING", "CHAIN_NOT_SWEEP", "CHAIN_STATE_UNCONFIRMED", "SIDE_UNKNOWN",
    "BARS_MISSING", "SWEEP_BAR_NOT_FOUND", "PIVOT_NOT_CONFIRMED", "LEG_DEGENERATE",
    "PROJECTION_INVALID", "ANCHOR_EXPIRED",
)
#: 構造確認が済んだ状態。ここに無い状態からは参加可能な STDV を作らない。
CONFIRMED_STATES = frozenset({"MSS_CONFIRMED", "RETEST_HELD"})


def anchor_diagnosis(chain: Optional[Dict[str, Any]],
                     bars: Optional[Sequence[Dict[str, float]]], *,
                     noise_floor: Optional[float] = None,
                     params: Optional[Dict[str, Any]] = None,
                     symbol: Optional[str] = None, session_id: Optional[str] = None,
                     source_tf: str = "3", bar_seconds: int = BAR_SECONDS,
                     level: Optional[Dict[str, Any]] = None,
                     liquidity_id: Optional[str] = None) -> Dict[str, Any]:
    """`{"anchor": … | None, "reason": …}`。理由は `MISSING_REASONS` のいずれか。"""
    prm = {**DEFAULT_PARAMS, **(params or {})}
    if not isinstance(chain, dict):
        return {"anchor": None, "reason": "CHAIN_MISSING"}
    if str(chain.get("type") or "").upper() != "SWEEP":
        return {"anchor": None, "reason": "CHAIN_NOT_SWEEP"}
    state = str(chain.get("state") or "").upper()
    if state not in CONFIRMED_STATES:
        return {"anchor": None, "reason": "CHAIN_STATE_UNCONFIRMED"}
    side = str(chain.get("side") or "").upper()
    if side not in {"BUY", "SELL"}:
        return {"anchor": None, "reason": "SIDE_UNKNOWN"}
    rows = [b for b in (bars or []) if isinstance(b, dict) and _int(b.get("t")) is not None]
    if len(rows) < 2 * int(prm["pivotBars"]) + 2:
        return {"anchor": None, "reason": "BARS_MISSING"}
    sweep_t = _int(chain.get("sweepBarT"))
    sweep_idx = next((i for i, b in enumerate(rows) if _int(b.get("t")) == sweep_t), None)
    if sweep_idx is None:
        return {"anchor": None, "reason": "SWEEP_BAR_NOT_FOUND"}

    pivot_idx = _pivot_index(rows, sweep_idx, side, int(prm["pivotBars"]), int(prm["maxLegBars"]))
    if pivot_idx is None:
        return {"anchor": None, "reason": "PIVOT_NOT_CONFIRMED"}

    # p0 = ピボットの極値 / p1 = ピボット〜掃引足の区間の極値(掃引側)
    span = rows[pivot_idx:sweep_idx + 1]
    if side == "BUY":
        p0 = _num(rows[pivot_idx]["h"])
        p1 = min(_num(b["l"]) for b in span)
    else:
        p0 = _num(rows[pivot_idx]["l"])
        p1 = max(_num(b["h"]) for b in span)
    if p0 is None or p1 is None:
        return {"anchor": None, "reason": "PROJECTION_INVALID"}
    width = abs(p1 - p0)
    floor_pt = max(int(prm["minLegTicks"]) * TICK,
                   (_num(noise_floor) or 0.0) * float(prm["minLegNf"]))
    if width <= 0 or width < floor_pt:
        return {"anchor": None, "reason": "LEG_DEGENERATE"}
    if (side == "BUY" and p1 >= p0) or (side == "SELL" and p1 <= p0):
        # 向きが取れていない = manipulation レッグになっていない
        return {"anchor": None, "reason": "PROJECTION_INVALID"}

    projection = project(p0, p1, side)
    if projection is None:
        return {"anchor": None, "reason": "PROJECTION_INVALID"}

    # 可知時刻: 構造確認足の終値と、ピボットの右側が閉じた時刻の遅い方。
    confirm_t = _int(chain.get("mssBarT")) or _int(chain.get("retestBarT")) or sweep_t
    confirm_closed = int(confirm_t) + int(bar_seconds)
    pivot_t = _int(rows[pivot_idx].get("t"))
    pivot_known = int(pivot_t) + int(prm["pivotBars"]) * int(bar_seconds) + int(bar_seconds)
    known_at = max(confirm_closed, pivot_known)

    last_t = _int(rows[-1].get("t")) or known_at
    age_bars = max(0, (last_t - int(confirm_t)) // int(bar_seconds))
    expired = age_bars > int(prm["maxAgeBars"])

    material = (SCHEMA, symbol, side, source_tf, pivot_t, rows[pivot_idx].get("h" if side == "BUY" else "l"),
                sweep_t, p1, confirm_t)
    anchor = {
        "schema": SCHEMA, "version": VERSION,
        "anchorId": _anchor_id(material),
        "symbol": symbol, "sessionId": session_id, "sourceTf": source_tf,
        "sourceHash": hashlib.sha1(
            "|".join(f"{_int(b.get('t'))}:{b.get('o')}:{b.get('h')}:{b.get('l')}:{b.get('c')}"
                     for b in span).encode("utf-8")).hexdigest()[:16],
        "evidenceRootId": chain.get("evidenceRootId"),
        # 根拠
        "side": side,
        "originBarT": pivot_t, "p0": round(p0, 6),
        "sweepBarT": sweep_t, "p1": round(p1, 6),
        "liquidityId": liquidity_id or (level or {}).get("label"),
        "sweepAt": sweep_t,
        "confirmation": {"kind": "MSS" if chain.get("mssBarT") else "RETEST",
                         "barT": confirm_t, "chainState": state},
        # 可知時刻
        "originAt": pivot_t, "extremeAt": sweep_t,
        "confirmedAt": confirm_closed, "knownAt": known_at,
        # 投影
        "widthPt": round(width, 6), "tick": TICK, "roundingRule": "CONSERVATIVE_TP",
        "ratios": list(RATIOS), "levels": projection["rows"],
        "byRatio": projection["byRatio"],
        # 状態
        "projectionValid": not expired,
        "invalidReasons": (["ANCHOR_EXPIRED"] if expired else []),
        "lifecycle": "EXPIRED" if expired else "CONFIRMED",
        "lastObservedAt": last_t, "ageBars": int(age_bars),
        "reachedAt": {},
        # 関係。方向票には使わない型であることを明示する。
        "parentAnchorId": None, "childAnchorIds": [],
        "targetOnly": True, "eligibleForDirectionVote": False,
    }
    if expired:
        return {"anchor": anchor, "reason": "ANCHOR_EXPIRED"}
    return {"anchor": anchor, "reason": None}


# ------------------------------------------------------- 到達判定(鮮度を分けて扱う)

def mark_reached(anchor: Dict[str, Any], bars: Optional[Sequence[Dict[str, float]]] = None,
                 price: Optional[float] = None, price_known: bool = False,
                 bar_seconds: int = BAR_SECONDS) -> Dict[str, Any]:
    """`knownAt` 以後の確定足と、**鮮度が確認できた**現値だけで到達を刻む。

    確定足の終値を当時の現値へ代入しない。現値を使ったかどうかは
    ``priceSubstituted`` に残す(集計で代用の有無を数えるため)。消化済み水準を
    再訪しただけで未消化へ戻さない(``reachedAt`` は一度入れたら消さない)。
    """
    if not isinstance(anchor, dict):
        return {}
    side = str(anchor.get("side") or "").upper()
    known_at = _int(anchor.get("knownAt")) or 0
    reached = dict(anchor.get("reachedAt") or {})
    used_price = False
    for row in anchor.get("levels") or []:
        ratio = row.get("ratio")
        if ratio is None or ratio >= 0:
            continue                      # アンカー内部(0/1)は到達判定しない
        key = str(ratio)
        if key in reached:
            continue                      # 再訪では戻さない
        target = _num(row.get("price"))
        if target is None:
            continue
        hit_t = None
        for bar in bars or []:
            bar_t = _int(bar.get("t"))
            if bar_t is None or bar_t < known_at:
                continue                  # knownAt より前の足では到達に数えない
            high, low = _num(bar.get("h")), _num(bar.get("l"))
            if high is None or low is None:
                continue
            if (side == "BUY" and high >= target) or (side == "SELL" and low <= target):
                hit_t = bar_t
                break
        if hit_t is not None:
            reached[key] = {"at": hit_t, "source": "CONFIRMED_BAR"}
            continue
        live = _num(price)
        if price_known and live is not None:
            if (side == "BUY" and live >= target) or (side == "SELL" and live <= target):
                reached[key] = {"at": _int(anchor.get("lastObservedAt")), "source": "VERIFIED_PRICE"}
                used_price = True
    out = dict(anchor)
    out["reachedAt"] = reached
    out["priceSubstituted"] = used_price
    out["consumedRatios"] = sorted(float(k) for k in reached)
    return out


# ------------------------------------------------- 「読む」と「参加する」を分ける

def target_candidates(anchor: Dict[str, Any], entry: Any, stop: Any, side: str,
                      target_ratios: Sequence[float] = TARGET_RATIOS) -> List[Tuple[float, str]]:
    """`model_targets(extra=…)` へ渡す `(price, label)` の列。

    方向が候補と一致し、未消化で、建値の先にある水準だけ。最小 R・重複除去・
    runner 選定は**既存の `target_pool` / `model_targets` に任せる**(STDV だけの
    特別扱いを作らない)。
    """
    if not isinstance(anchor, dict) or anchor.get("projectionValid") is not True:
        return []
    if str(anchor.get("side") or "").upper() != str(side or "").upper():
        return []
    e = _num(entry)
    if e is None:
        return []
    consumed = {str(k) for k in (anchor.get("reachedAt") or {})}
    out: List[Tuple[float, str]] = []
    for row in anchor.get("levels") or []:
        ratio = _num(row.get("ratio"))
        price = _num(row.get("price"))
        if ratio is None or price is None or ratio not in tuple(target_ratios):
            continue
        if str(ratio) in consumed or row.get("collapsedWith") is not None:
            continue
        if (side == "BUY" and price <= e) or (side == "SELL" and price >= e):
            continue
        out.append((price, label_for(ratio)))
    out.sort(key=lambda item: abs(item[0] - e))
    return out


def label_for(ratio: float) -> str:
    """目標ラベル。`level_tier` に拾われない語にする(STDV だけで runner 昇格しない)。"""
    text = ("%g" % float(ratio)).replace("-", "m")
    return f"STDV_{text}"


def read_context(anchor: Optional[Dict[str, Any]], *, side: str, entry: Any, stop: Any,
                 price: Any, price_known: bool, targets: Optional[Sequence[float]] = None,
                 entry_is_limit: Optional[bool] = None) -> Dict[str, Any]:
    """候補 1 件について「読み」と「参加」を別々に説明できる出力を作る。

    確率は付けない(未検証)。`WAIT_FOR_PULLBACK` は逆向きの発注指示でも、指値で
    経路を無期限に占有する指示でもない —— 参加の可否は既存ゲートが決める。
    """
    out: Dict[str, Any] = {
        "schema": SCHEMA, "version": VERSION,
        "thesis": "UNRESOLVED", "thesisInvalidation": None,
        "nearTerm": None,
        "participation": "INVALIDATED", "participationReasons": [],
        "entryZone": None, "stdvContext": None, "headroom": {},
        "invalidation": None, "anchorId": None,
        "priceSubstituted": False,
    }
    e, s, p = _num(entry), _num(stop), _num(price)
    if e is not None and s is not None and e != s:
        risk = abs(e - s)
        out["headroom"]["riskPt"] = round(risk, 4)
        if targets:
            first = _num(targets[0])
            last = _num(targets[-1])
            if first is not None:
                out["headroom"]["entryToTp1R"] = round(abs(first - e) / risk, 3)
            if last is not None:
                out["headroom"]["entryToFinalR"] = round(abs(last - e) / risk, 3)
            if p is not None and price_known and first is not None:
                sign = 1.0 if side == "BUY" else -1.0
                out["headroom"]["priceToTp1R"] = round(sign * (first - p) / risk, 3)
    if not isinstance(anchor, dict):
        out["participationReasons"].append("STDV_ANCHOR_MISSING")
        return out

    out["anchorId"] = anchor.get("anchorId")
    out["priceSubstituted"] = bool(anchor.get("priceSubstituted"))
    out["thesis"] = str(anchor.get("side") or "UNRESOLVED").upper()
    out["thesisInvalidation"] = (
        f"構造確認({(anchor.get('confirmation') or {}).get('kind')})の否定、"
        f"または p1 {anchor.get('p1')} の再掃引")
    consumed = sorted(float(k) for k in (anchor.get("reachedAt") or {}))
    remaining = [row for row in (anchor.get("levels") or [])
                 if _num(row.get("ratio")) is not None and _num(row.get("ratio")) < 0
                 and str(_num(row.get("ratio"))) not in {str(c) for c in consumed}]
    out["stdvContext"] = {
        "anchorId": anchor.get("anchorId"), "side": anchor.get("side"),
        "p0": anchor.get("p0"), "p1": anchor.get("p1"), "widthPt": anchor.get("widthPt"),
        "byRatio": anchor.get("byRatio"), "consumedRatios": consumed,
        "remainingRatios": [row["ratio"] for row in remaining],
        "reactionRatios": [r for r in REACTION_RATIOS if r not in consumed],
        "knownAt": anchor.get("knownAt"), "ageBars": anchor.get("ageBars"),
        "projectionValid": anchor.get("projectionValid"),
        "targetOnly": True,
    }
    out["invalidation"] = out["thesisInvalidation"]

    if anchor.get("projectionValid") is not True:
        out["participation"] = "INVALIDATED"
        out["participationReasons"] = list(anchor.get("invalidReasons") or ["PROJECTION_INVALID"])
        return out
    if not price_known or p is None:
        out["participation"] = "WAIT_FOR_PULLBACK"
        out["participationReasons"].append("PRICE_UNVERIFIED")
        return out
    if not remaining:
        out["participation"] = "NO_ROOM"
        out["participationReasons"].append("ALL_PROJECTIONS_CONSUMED")
        return out

    # 残り値幅。既存の R とは別に、素の点数でも持つ。
    nearest = min(remaining, key=lambda row: abs(_num(row["price"]) - p))
    out["headroom"]["priceToNearestRemainingPt"] = round(abs(_num(nearest["price"]) - p), 4)
    out["headroom"]["remainingRatio"] = nearest["ratio"]

    if e is not None:
        at_or_through = (p <= e) if side == "BUY" else (p >= e)
        if at_or_through:
            out["participation"] = "ENTRY_READY"
            out["participationReasons"].append("PRICE_AT_OR_THROUGH_ENTRY")
        else:
            out["participation"] = "WAIT_FOR_PULLBACK"
            out["participationReasons"].append("PRICE_BEYOND_ENTRY")
            out["entryZone"] = {"entry": e, "stop": s,
                                "distancePt": round(abs(p - e), 4),
                                "trigger": "既存の武装ゲート(距離・TP1 通過・ボラ・イベント)"}
    else:
        out["participation"] = "WAIT_FOR_PULLBACK"
        out["participationReasons"].append("ENTRY_UNKNOWN")

    # 直近の予測対象。確率は付けない。
    out["nearTerm"] = {
        "horizon": "次の反応域まで",
        "direction": anchor.get("side"),
        "basis": f"STDV {nearest['ratio']} @ {nearest['price']}",
        "note": "未検証の確率は付けない",
    }
    return out

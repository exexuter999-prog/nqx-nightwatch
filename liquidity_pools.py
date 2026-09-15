# -*- coding: utf-8 -*-
"""R103-1: SL の外側にある流動性プールの検出と、SL をプールの向こうへ逃がす監査。

きっかけ(docs/reports/STOP_HUNT_EVIDENCE_2026-09-15.md §3〜§4)
--------------------------------------------------------------
2026-09-15 の 4 件は方向が合っていたのに、SL が「まだ回収されていない流動性プール」の
**内側**に置かれていた。22:38 の SELL は SL 29,444.50 に対して 29,447.00(スイング高値 かつ
current VAH)が 2.5pt(0.16N)外側にあり、そこを取りに行く 1 本で刈られてから TP 方向へ進んだ。

このモジュールは純粋計算だけを持つ(ネットワーク・台帳・発注・ファイル I/O に触れない)。

  * ``pools``           … 3 分足とレベル集合から未回収のプールを列挙する。
                          スイング高安の検出は ``msnr_gate.swing_liquidity`` をそのまま呼ぶ
                          (同じ構造の判定を二重に持たない)。
  * ``stop_pool_audit`` … entry/stop の幾何に対してプールの位置を監査し、SL を逃がすなら
                          どこへ置くか(``required``)を返す。置き換えるかどうかは呼び出し側
                          (``execution_contract.json`` の ``stopLogic.poolClearance``)が決める。

既定は SHADOW(記録だけ)。LIVE に倒すのは人。
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

import stop_logic
from stop_logic import POOL_KINDS, VERSION_R103, finite, outward_tick

#: ラベルの正規表現は msnr_gate の既存定義に合わせる(TIER_PATTERNS tier2/tier3、VAH_RE / VAL_RE)。
SESSION_HIGH_RE = re.compile(r"(?:\basia\b|\blondon\b|new\s+york)\s*high", re.I)
SESSION_LOW_RE = re.compile(r"(?:\basia\b|\blondon\b|new\s+york)\s*low", re.I)
VA_HIGH_RE = re.compile(r"\bvah\b", re.I)
VA_LOW_RE = re.compile(r"\bval\b", re.I)
PD_HIGH_RE = re.compile(r"prev(?:ious)?\s*day\s*high|\bpdh\b", re.I)
PD_LOW_RE = re.compile(r"prev(?:ious)?\s*day\s*low|\bpdl\b", re.I)

#: ラベル → (kind, 上側か)。上から順に判定する(最初に当たったものを採る)。
_LABEL_RULES = (
    (PD_HIGH_RE, "PD_EXTREME", True),
    (PD_LOW_RE, "PD_EXTREME", False),
    (SESSION_HIGH_RE, "SESSION", True),
    (SESSION_LOW_RE, "SESSION", False),
    (VA_HIGH_RE, "VA_EDGE", True),
    (VA_LOW_RE, "VA_EDGE", False),
)

EVIDENCE_WITHIN_1N = "STOP_POOL_WITHIN_1N"
EVIDENCE_BETWEEN = "POOL_BETWEEN_ENTRY_STOP"
EVIDENCE_CLEARED = "POOL_STOP_CLEARED"

#: ``inside025N`` の窓。名前のとおり固定 0.25N(契約の clearN とは別物)。
INSIDE_N = 0.25


def _bars(bars: Any) -> List[Dict[str, Any]]:
    rows = []
    for bar in bars or []:
        if not isinstance(bar, dict):
            continue
        high, low = finite(bar.get("h", bar.get("high"))), finite(bar.get("l", bar.get("low")))
        if high is None or low is None or high < low:
            continue
        rows.append({"t": finite(bar.get("t", bar.get("time"))), "h": high, "l": low})
    return rows


def _touch_stats(rows: List[Dict[str, Any]], price: float, tol: float, upper: bool) -> Dict[str, Any]:
    """プール価格に対する「最後に触れた足 / 年齢 / 並んだ本数 / 掃引済みか」。

    ``equalCount`` は同じ側の極値が ``tol`` 以内に並んだ本数(EQH / EQL の厚み)。
    """
    key = "h" if upper else "l"
    last_idx = None
    equal = 0
    for idx, bar in enumerate(rows):
        extreme = bar[key]
        if abs(extreme - price) <= tol:
            equal += 1
            last_idx = idx

    def _beyond(bar):
        extreme = bar[key]
        return (extreme > price + tol) if upper else (extreme < price - tol)

    # 掃引済み = 最後に触れた**後**の足がその向こうを取った(msnr_gate.swing_liquidity の
    # 「後続が更新していれば回収済み」と同じ見方。一度も触れていない水準は窓全体で見る)。
    swept = any(_beyond(bar) for bar in (rows[last_idx + 1:] if last_idx is not None else rows))
    if last_idx is None:
        return {"barT": None, "ageBars": None, "equalCount": equal, "swept": swept}
    return {"barT": rows[last_idx]["t"], "ageBars": len(rows) - 1 - last_idx,
            "equalCount": equal, "swept": swept}


def pools(bars: Any, levels: Any, price: Any, tol: Any, noise: Any = None) -> List[Dict[str, Any]]:
    """未回収の流動性プールを列挙する(純粋関数)。

    ``kind`` は SWING_HIGH / SWING_LOW(``msnr_gate.swing_liquidity`` の結果)、
    SESSION(Asia / London / New York の High|Low)、VA_EDGE(C:/P: VAH|VAL)、
    PD_EXTREME(PDH / PDL / Previous Day High|Low)。同じ価格に複数の根拠があれば
    **まとめない**(29,447.00 が SWING_HIGH かつ VA_EDGE、のような重なりが厚みの証拠)。

    ``tol`` が無ければ msnr_gate と同じ ``0.10 × ノイズ床`` に倒す。どちらも無ければ空。
    """
    rows = _bars(bars)
    price_f, tol_f, nf = finite(price), finite(tol), finite(noise)
    if tol_f is None or tol_f <= 0:
        tol_f = 0.10 * nf if nf and nf > 0 else None
    if tol_f is None or tol_f <= 0:
        return []
    found: List[Dict[str, Any]] = []
    if rows and price_f is not None:
        import msnr_gate                       # 遅延 import(msnr_gate 側からも呼ばれる)
        bsl, ssl = msnr_gate.swing_liquidity(rows, price_f, tol_f)
        for value, kind, upper, label in ([(v, "SWING_HIGH", True, "BSL") for v in bsl]
                                          + [(v, "SWING_LOW", False, "SSL") for v in ssl]):
            pool = {"price": round(float(value), 2), "kind": kind, "label": label}
            pool.update(_touch_stats(rows, float(value), tol_f, upper))
            found.append(pool)
    for item in levels or []:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or "").strip()
        level_price = finite(item.get("price"))
        if not label or level_price is None:
            continue
        for pattern, kind, upper in _LABEL_RULES:
            if not pattern.search(label):
                continue
            pool = {"price": round(level_price, 2), "kind": kind, "label": label}
            pool.update(_touch_stats(rows, level_price, tol_f, upper))
            found.append(pool)
            break
    found.sort(key=lambda row: (row["price"], row["kind"], row["label"]))
    return found


def _compact(pool: Dict[str, Any], stop: float, sgn: float) -> Dict[str, Any]:
    return {"price": pool["price"], "kind": pool["kind"], "label": pool["label"],
            "equalCount": pool.get("equalCount"),
            "gapPt": round((pool["price"] - stop) * -sgn, 2)}


def _side_of(pool: Dict[str, Any]) -> str:
    """プールが上側(売り方向の外側)か下側か。ラベルではなく kind で決める。"""
    if pool["kind"] in {"SWING_HIGH"}:
        return "UP"
    if pool["kind"] in {"SWING_LOW"}:
        return "DOWN"
    label = pool["label"]
    if VA_HIGH_RE.search(label) or SESSION_HIGH_RE.search(label) or PD_HIGH_RE.search(label):
        return "UP"
    return "DOWN"


def stop_pool_audit(side: str, entry: Any, stop: Any, pool_rows: Any, noise: Any,
                    rule: Optional[Dict[str, Any]] = None,
                    model: Optional[str] = None) -> Dict[str, Any]:
    """SL の周りのプールを監査する(純粋関数)。

    ``between``       … 建値と SL の間にあるプール(刈られる前に建値方向へ戻れない位置)。
    ``beyondWithinN`` … SL の外側 ``withinN``×N 以内にあるプール(= 刈りの燃料)。
    ``nearestBeyond`` … SL の外側で最も近いプール。
    ``inside025N``    … SL がプールの ±0.25N に入っているか(証拠資料 §3 の形)。
    ``required``      … 最寄りの外側プールのさらに向こう ``clearN``×N(不利側 tick へ丸め)。
                        置く先が無ければ None で、``reason`` がその理由。
    ``applied``       … このモジュールは置き換えない。常に False(LIVE の呼び出し側が立てる)。
    """
    rule = rule or stop_logic.DEFAULT_POOL
    audit: Dict[str, Any] = {"version": VERSION_R103, "mode": rule.get("mode", "OFF"), "model": model,
                             "between": [], "beyondWithinN": [], "nearestBeyond": None,
                             "inside025N": [], "required": None, "applied": False, "reason": None,
                             "original": None}
    side = str(side or "").upper()
    entry_f, stop_f, nf = finite(entry), finite(stop), finite(noise)
    if model is not None and model not in (rule.get("models") or stop_logic.ALL_MODELS):
        audit["reason"] = "MODEL_NOT_IN_POLICY"
        return audit
    if side not in {"BUY", "SELL"} or entry_f is None or stop_f is None:
        audit["reason"] = "GEOMETRY_MISSING"
        return audit
    audit["original"] = stop_f
    if nf is None or nf <= 0:
        audit["reason"] = "NOISE_FLOOR_MISSING"
        return audit
    if pool_rows is None:
        # levels も bars も無い = 判定材料が無い。「プールが無い」とは区別する。
        audit["reason"] = "POOLS_MISSING"
        return audit
    sgn = 1.0 if side == "BUY" else -1.0          # 建値から見て SL のある向き = -sgn
    if (entry_f - stop_f) * sgn <= 0:
        audit["reason"] = "STOP_ON_WRONG_SIDE"
        return audit
    kinds = rule.get("kinds") or list(POOL_KINDS)
    within_pt = float(rule.get("withinN", 1.0)) * nf
    clear_pt = float(rule.get("clearN", 0.25)) * nf
    audit.update({"noise": round(nf, 2), "withinPt": round(within_pt, 2),
                  "clearPt": round(clear_pt, 2)})

    want = "DOWN" if side == "BUY" else "UP"      # SL 側 = 建値の反対側
    between, beyond = [], []
    for pool in pool_rows:
        if not isinstance(pool, dict) or pool.get("kind") not in kinds:
            continue
        pool_price = finite(pool.get("price"))
        if pool_price is None or _side_of(pool) != want:
            continue
        row = dict(pool)
        row["price"] = round(pool_price, 2)
        outward = (stop_f - pool_price) * sgn     # 正 = SL より外側
        if outward >= -1e-9:
            beyond.append((outward, row))
        elif (entry_f - pool_price) * sgn > 1e-9:
            between.append(row)
        if abs(pool_price - stop_f) <= INSIDE_N * nf + 1e-9:
            audit["inside025N"].append(_compact(row, stop_f, sgn))
    between.sort(key=lambda row: (entry_f - row["price"]) * sgn)
    beyond.sort(key=lambda item: item[0])
    audit["between"] = [_compact(row, stop_f, sgn) for row in between]
    audit["beyondWithinN"] = [_compact(row, stop_f, sgn) for gap, row in beyond
                              if gap <= within_pt + 1e-9]
    if beyond:
        audit["nearestBeyond"] = _compact(beyond[0][1], stop_f, sgn)

    if not beyond and not between:
        audit["reason"] = "NO_POOL"
        return audit
    if not beyond:
        audit["reason"] = "STOP_ALREADY_BEYOND_POOL"
        return audit
    gap, nearest = beyond[0]
    if gap > within_pt + 1e-9:
        audit["reason"] = "POOL_FAR_FROM_STOP"
        return audit
    required = outward_tick(nearest["price"] - sgn * clear_pt, side)
    if (stop_f - required) * sgn <= 0:
        # プールは外側にあるが、clearN を足しても今の SL より内側 = 逃がす必要が無い。
        audit["reason"] = "STOP_ALREADY_BEYOND_POOL"
        return audit
    audit.update({"required": required, "reason": None,
                  "shiftPt": round((stop_f - required) * sgn, 2),
                  "riskPt": round((entry_f - required) * sgn, 2),
                  "riskN": round((entry_f - required) * sgn / nf, 2)})
    return audit


def evidence_tags(audit: Optional[Dict[str, Any]]) -> List[str]:
    """記録専用タグ。採点・等級・decisionId には使わない。"""
    if not isinstance(audit, dict):
        return []
    tags = []
    if audit.get("beyondWithinN"):
        tags.append(EVIDENCE_WITHIN_1N)
    if audit.get("between"):
        tags.append(EVIDENCE_BETWEEN)
    return tags


def describe(audit: Dict[str, Any]) -> str:
    if not isinstance(audit, dict):
        return "R103 pool clearance: no audit"
    if audit.get("required") is None:
        return f"R103 pool clearance: not applied ({audit.get('reason')})"
    nearest = audit.get("nearestBeyond") or {}
    return (f"R103 pool clearance SL {audit.get('original')}→{audit['required']} "
            f"(pool {nearest.get('price')} {nearest.get('kind')} gap {nearest.get('gapPt')}pt) "
            f"[{audit.get('mode')}]")

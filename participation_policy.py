# -*- coding: utf-8 -*-
"""R122: 参加判断(どの候補が「今入れる」のか)。

**純粋な計算だけ。** ネットワーク・台帳・発注・ファイル I/O に触らない。既存の武装ゲート
(`msnr_gate` の hardBlockers / R89 / R119 / R103 / 幾何)を**置き換えない** —— それらの
出力を読んで、「何を待っているのか」「何が来れば入れるのか」「なぜ入れないのか」を
機械可読な状態にするのがこの層の責任。

状態(`docs/R122_STRUCTURE_CONTEXT_AND_PARTICIPATION.md` §4):

  ENTRY_READY            既存ゲートを全部通っている。注文意図(成行 / 先回り指値)を明示する
  WAIT_FOR_PULLBACK      仮説は生きているが、価格がまだ建値まで来ていない(発注枠は取らない)
  WAIT_FOR_CONFIRMATION  位置には来たが構造確認が足りない。不足しているトリガーを名指しする
  NO_ROOM                有効な Entry/SL から目標までの余地が既存の最低条件を満たさない
  INVALIDATED            その計画の根拠が破られた
  EXPIRED                寿命切れ
  CONTEXT_UNAVAILABLE    **新しい文脈層に依存する候補だけ**。従来候補は従来の検査で判断する

**親の否定によるブロックは、その親に依存する候補に限定する。** 親子の方向が一致しない
というだけで全部 WATCH にはしない(§4.1)。先回り指値型(TURTLE の `restingLimit`)は
状態名の変更で消さない —— 既存の注文可能な型は `orderIntent` に明示して残す。
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

SCHEMA = "NQX_PARTICIPATION/1"
VERSION = "R122-PARTICIPATION-1"

STATES = ("ENTRY_READY", "WAIT_FOR_PULLBACK", "WAIT_FOR_CONFIRMATION", "NO_ROOM",
          "INVALIDATED", "EXPIRED", "CONTEXT_UNAVAILABLE")

#: LIVE のときだけ hardBlockers に足す。**この層が足すブロッカーはこれ 1 つだけ。**
BLOCKER_PARENT_INVALIDATED = "PARENT_THESIS_INVALIDATED"
#: SHADOW の追加候補が primary にならないようにする印(記録は残る)。
BLOCKER_SHALLOW_SHADOW = "SHALLOW_CANDIDATE_SHADOW"
EVIDENCE_SHALLOW = "R122_SHALLOW_STRUCTURE"

#: 既存ブロッカーの分類。値は `msnr_gate` / `liquidity_pools` が実際に立てる名前。
NO_ROOM_BLOCKERS = frozenset({
    "TARGET_HEADROOM_INSUFFICIENT", "TARGET_ALREADY_PASSED", "RISK_CAP_EXCEEDED",
    "RISK_BELOW_NOISE", "GEOMETRY_INVALID",
})
WAIT_PULLBACK_BLOCKERS = frozenset({"LIMIT_GAP_EXCEEDED"})
INVALIDATED_BLOCKERS = frozenset({"ANCHOR_CONSUMED"})
WAIT_CONFIRMATION_BLOCKERS = frozenset({
    "NO_CONFIRMATION", "SWEEP_GATE_PENDING", "SWEEP_GATE_STALE",
})
#: 契約で外したモデル / 型。参加状態ではないので理由だけ残す。
CONTRACT_BLOCKERS = frozenset({"MODEL_DISABLED", "RESTING_LIMIT_DISABLED"})

#: 待機計画(観測用)を作るチェーン状態と、次に観測すべきもの。
WAITING_CHAIN_STATES = {
    "SWEEP_CANDIDATE": "displacement(実体 ≥ 1.0×ノイズ床)を伴う奪還足",
    "SWEEP_CONFIRMED": "直前 5 本の極値を終値で破る MSS",
    "MSS_CONFIRMED": "レベル価格へ戻って終値で保持(RETEST_HELD)",
    "FLIP_BREAK": "ゾーン外の終値による受容(FLIP_ACCEPTED)",
    "FLIP_ACCEPTED": "レベル価格へ戻って終値で保持(FLIP_HELD)",
}
MAX_PLANS = 6


def _num(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _order_intent(candidate: Dict[str, Any], price: Optional[float]) -> Optional[str]:
    """発注時の注文種別。`autotrade_engine._entry_order_type` と同じ式。"""
    if candidate.get("restingLimit"):
        return "RESTING_LIMIT"
    entry, side = _num(candidate.get("entry")), str(candidate.get("side") or "").upper()
    if entry is None or price is None or side not in {"BUY", "SELL"}:
        return None
    if side == "BUY":
        return "MARKET" if entry >= price else "LIMIT"
    return "MARKET" if entry <= price else "LIMIT"


def depends_on_thesis(candidate: Dict[str, Any], context: Optional[Dict[str, Any]]) -> bool:
    """この候補は親の仮説に依存しているか。

    依存 = (a) この層が作った追加候補、または (b) **親と同じ方向**の候補。親の仮説が
    壊れた(保護水準を親の時間足の終値が抜けた)なら、その方向への継続は上位足の
    根拠を失う —— これが評価で指摘された「上位足の買い仮説の否定水準を下位足が破り、
    反転へ移行した」局面。**逆方向の独立したセットアップは依存しない** —— 親が壊れた
    ことを理由に反転候補を止めない(§4.1)。

    以前は `HTF_ALIGNED`(= 45m 以上の集計 bias が採点へ効いたこと)も要求していたが、
    親が 15 分足由来のときは別の層を見ていることになり、依存判定として成立しない。
    """
    if not isinstance(context, dict):
        return False
    thesis = context.get("thesis") or {}
    bias = thesis.get("bias")
    if bias not in ("BUY", "SELL"):
        return False
    if candidate.get("r122Shallow"):
        return True
    return str(candidate.get("side") or "").upper() == bias


def trigger_evidence_ids(candidate: Dict[str, Any], context: Optional[Dict[str, Any]]) -> List[str]:
    """この判断を動かした根拠の ID。最終 decision と凍結プランまで運ぶ。"""
    out: List[str] = []
    thesis = (context or {}).get("thesis") or {}
    child = (context or {}).get("child") or {}
    if thesis.get("structureId"):
        out.append(f"thesis:{thesis['structureId']}")
    if child.get("structureId"):
        out.append(f"child:{child['structureId']}")
    chain = candidate.get("chain") or {}
    origin = chain.get("sweepBarT") if chain.get("type") == "SWEEP" else chain.get("breakBarT")
    if origin is not None:
        out.append(f"chain:{chain.get('type')}:{int(origin)}")
    fvg = candidate.get("fvg") or {}
    if fvg.get("createdAt") is not None:
        out.append(f"fvg:{int(fvg['createdAt'])}")
    anchor = ((candidate.get("ictStdv") or {}).get("anchor") or {}).get("anchorId")
    if anchor:
        out.append(f"stdv:{anchor}")
    return out[:6]


def evaluate(candidate: Dict[str, Any], context: Optional[Dict[str, Any]],
             price: Optional[float], now: Optional[float] = None) -> Dict[str, Any]:
    """1 候補の参加判断。**候補を書き換えない**(呼び出し側が監査を足す)。"""
    blockers = [b for b in (candidate.get("hardBlockers") or [])
                if b not in (BLOCKER_PARENT_INVALIDATED, BLOCKER_SHALLOW_SHADOW)]
    thesis = (context or {}).get("thesis") or {}
    status = str((context or {}).get("status") or "CONTEXT_UNAVAILABLE")
    dependent = depends_on_thesis(candidate, context)
    audit: Dict[str, Any] = {
        "schemaVersion": SCHEMA, "version": VERSION,
        "state": None, "reasons": [], "blockers": [], "dependsOnThesis": dependent,
        "thesisId": thesis.get("structureId"),
        "relation": (context or {}).get("childRelation") or "UNRESOLVED",
        "orderIntent": _order_intent(candidate, price),
        "triggerEvidenceIds": trigger_evidence_ids(candidate, context),
        "invalidation": None, "nextObservation": None, "deadline": None,
    }
    chain = candidate.get("chain") or {}
    chain_state = str(chain.get("state") or "")
    bars_left = candidate.get("chain", {}).get("barsLeft")

    if candidate.get("r122Shallow") and status != "OK":
        audit["state"] = "CONTEXT_UNAVAILABLE"
        audit["reasons"].append("THESIS_UNAVAILABLE")
        return audit

    if dependent and thesis.get("invalidatedAt"):
        audit["state"] = "INVALIDATED"
        audit["reasons"].append("PARENT_CLOSE_BEYOND_PROTECTED")
        audit["invalidation"] = thesis.get("invalidationRule")
        audit["blockers"].append(BLOCKER_PARENT_INVALIDATED)
        return audit

    if dependent and thesis.get("expired"):
        audit["state"] = "EXPIRED"
        audit["reasons"].append("PARENT_THESIS_EXPIRED")
        audit["deadline"] = thesis.get("expiresAt")
        return audit

    hit = [b for b in blockers if b in INVALIDATED_BLOCKERS]
    if hit or chain_state == "EXPIRED":
        audit["state"] = "INVALIDATED" if hit else "EXPIRED"
        audit["reasons"].extend(hit or ["CHAIN_EXPIRED"])
        audit["invalidation"] = candidate.get("stop")
        return audit

    if isinstance(bars_left, (int, float)) and bars_left <= 0:
        audit["state"] = "EXPIRED"
        audit["reasons"].append("CHAIN_TTL_ELAPSED")
        return audit

    hit = [b for b in blockers if b in NO_ROOM_BLOCKERS]
    if hit:
        audit["state"] = "NO_ROOM"
        audit["reasons"].extend(hit)
        return audit

    hit = [b for b in blockers if b in WAIT_PULLBACK_BLOCKERS]
    if hit:
        audit["state"] = "WAIT_FOR_PULLBACK"
        audit["reasons"].extend(hit)
        audit["nextObservation"] = f"現値が建値 {candidate.get('entry')} まで戻ること"
        audit["deadline"] = _deadline(chain)
        return audit

    if candidate.get("allowed"):
        audit["state"] = "ENTRY_READY"
        audit["reasons"].append("EXISTING_GATES_PASSED")
        audit["deadline"] = _deadline(chain)
        return audit

    contract_hit = [b for b in blockers if b in CONTRACT_BLOCKERS]
    audit["state"] = "WAIT_FOR_CONFIRMATION"
    audit["reasons"].extend(blockers or ["NOT_ARMED"])
    if contract_hit:
        audit["reasons"] = list(dict.fromkeys([*contract_hit, *audit["reasons"]]))
    audit["nextObservation"] = WAITING_CHAIN_STATES.get(
        chain_state, "既存ゲートの不足解消(" + ", ".join(blockers[:2]) + ")" if blockers else None)
    audit["deadline"] = _deadline(chain)
    return audit


def _deadline(chain: Dict[str, Any]) -> Optional[int]:
    """残り本数から「いつまでに来なければ失効か」を返す(既存 TTL の写し)。"""
    left = chain.get("barsLeft")
    origin = chain.get("sweepBarT") if chain.get("type") == "SWEEP" else chain.get("breakBarT")
    if not isinstance(left, (int, float)) or origin is None:
        return None
    try:
        return int(origin) + 180 * int(max(0, left))
    except (TypeError, ValueError):
        return None


def plans(context: Optional[Dict[str, Any]], result: Dict[str, Any],
          price: Optional[float]) -> List[Dict[str, Any]]:
    """候補になっていないチェーンの**観測用**待機計画。

    claim も注文枠も取らない。「次に何を観測すれば入れるか」と「待機期限」だけを残す
    (§4.1 の WAIT_FOR_PULLBACK / WAIT_FOR_CONFIRMATION の記録側)。
    """
    out: List[Dict[str, Any]] = []
    for level in result.get("levels") or []:
        if not isinstance(level, dict) or level.get("dynamic"):
            continue
        for chain in level.get("chains") or []:
            state = str(chain.get("state") or "")
            if state not in WAITING_CHAIN_STATES:
                continue
            origin = chain.get("sweepBarT") if chain.get("type") == "SWEEP" else chain.get("breakBarT")
            level_price = _num(level.get("price"))
            side = chain.get("side")
            at_position = None
            if level_price is not None and price is not None:
                at_position = abs(price - level_price)
            out.append({
                "planId": f"{chain.get('type')}:{side}:{level.get('label')}:{origin}",
                "side": side, "level": level.get("label"), "levelPrice": level_price,
                "chainState": state, "distancePt": round(at_position, 2) if at_position is not None else None,
                "participation": "WAIT_FOR_CONFIRMATION",
                "nextObservation": WAITING_CHAIN_STATES[state],
                "deadline": _deadline(chain), "barsLeft": chain.get("barsLeft"),
                "claimsOrderSlot": False,
            })
    out.sort(key=lambda row: (row["distancePt"] is None, row["distancePt"] or 0))
    return out[:MAX_PLANS]


# ---------------------------------------------------- §4.2 浅い構造の代替候補

def shallow_seed(context: Optional[Dict[str, Any]], ict: Optional[Dict[str, Any]],
                 bars: Sequence[Dict[str, float]], price: Optional[float],
                 noise: Optional[float], buffer_pt: float,
                 has_orderable_same_side: bool) -> Optional[Dict[str, Any]]:
    """深い押しが来ない場面の代替参加。**証拠が無ければ None(= 代替参加なし)。**

    条件(全部、既存の検出器の出力だけを使う):

      1. 親の仮説が生きている(方向 B・否定されていない・寿命内・目標未消化)
      2. **方向 B の**子の構造が確認済み(MSS_CONFIRMED / FLIP_ACCEPTED 以上)。
         全体で最も進んだチェーンではなく、`context["children"][B]` を見る —— 押し目局面では
         最も進んだチェーンは逆方向(押しそのもの)なので、そこで切ると代替は永久に出ない
      3. 方向 B の `eligible` かつ `preArrivalStructure=INTACT` な FVG があり、
         **その子の構造の起点以降に生成された**もの(古い空隙を拾わない)
      4. その FVG の中点が**押し戻り側**にある(= 指値。取り逃しを理由に成行へ切り替えない)
      5. 同じ方向に**発注できる候補が既に無い**(あるならそちらが正。代替を作らない)

    返すのは建値 / SL / 根拠で、候補化(採点・目標・既存ゲート)は `msnr_gate` が
    既存の `_candidate_for_chain` で行う。SL はこの構造自身の否定(FVG の外側 ± 緩衝)。
    """
    if has_orderable_same_side:
        return None
    if not isinstance(context, dict) or str(context.get("status")) != "OK":
        return None
    thesis = context.get("thesis") or {}
    bias = thesis.get("bias")
    if bias not in ("BUY", "SELL"):
        return None
    child = (context.get("children") or {}).get(bias) or {}
    if thesis.get("invalidatedAt") or thesis.get("expired") or thesis.get("consumedAt"):
        return None
    if child.get("bias") != bias or not child.get("confirmed"):
        return None
    origin = child.get("originBarT")
    if origin is None or price is None:
        return None
    rows = ((ict or {}).get("fvg") or {}).get("BULL" if bias == "BUY" else "BEAR") or []
    best = None
    for row in rows:
        if not isinstance(row, dict) or not row.get("eligible"):
            continue
        if str(row.get("preArrivalStructure")) != "INTACT":
            continue
        created = row.get("createdAt")
        if created is None or int(created) < int(origin):
            continue
        lo, hi = _num(row.get("lo")), _num(row.get("hi"))
        if lo is None or hi is None or hi <= lo:
            continue
        mid = (lo + hi) / 2.0
        # 押し戻り側にだけ置く。現値が既に中点を通過していれば追いかけない。
        if bias == "BUY" and not mid < price:
            continue
        if bias == "SELL" and not mid > price:
            continue
        distance = abs(price - mid)
        if best is None or distance < best["distancePt"]:
            best = {"lo": lo, "hi": hi, "mid": mid, "distancePt": distance, "fvg": row}
    if best is None:
        return None
    stop = (best["lo"] - buffer_pt) if bias == "BUY" else (best["hi"] + buffer_pt)
    return {
        "side": bias, "entry": best["mid"], "stop": stop,
        "levelLabel": "R122 shallow FVG", "levelPrice": best["mid"],
        "fvg": best["fvg"], "distancePt": round(best["distancePt"], 2),
        "thesisId": thesis.get("structureId"), "childId": child.get("structureId"),
        "reason": "深い押し目が来ないまま親の仮説が生きており、子の確認済み構造の後に"
                  "できた適格 FVG へ指値で参加する",
        "evidence": [EVIDENCE_SHALLOW],
    }


def compact(audit: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """decision / 凍結プランへ載せる最小形式。"""
    if not isinstance(audit, dict):
        return None
    return {
        "v": VERSION, "state": audit.get("state"),
        "reasons": list(audit.get("reasons") or [])[:3],
        "thesisId": audit.get("thesisId"), "relation": audit.get("relation"),
        "orderIntent": audit.get("orderIntent"),
        "dependsOnThesis": bool(audit.get("dependsOnThesis")),
        "triggerEvidenceIds": list(audit.get("triggerEvidenceIds") or [])[:4],
        "invalidation": audit.get("invalidation"),
        "nextObservation": audit.get("nextObservation"),
        "deadline": audit.get("deadline"),
    }

#!/usr/bin/env python3
"""R84 構造トランシェ台帳 (Structural Tranche Ledger)。**純関数で、発注もネットワークも
触らない。**

docs/R84_PYRAMID_TRANCHE_MANAGEMENT.md の §1/§2/§4/§5 を実装する。要点は 3 つ。

1. **加算型ブラケット** —— 追撃は「それ自体が完結した分割ブラケット付きエントリー」
   として送り、既存トランシェの OCO には触れない。ブローカーは建玉をネッティング
   するが**注文はネッティングしない**ので、保護は「旧 OCO 対 + 新 OCO 対」の加算に
   なる。追撃時に `cancelandbracket` を使わない = R78 の「取消だけ成立して裸」が
   構造的に起きない。
2. **構造導出枚数** —— `ownership_binder` の `lifecycle_quantities`(枚数の集合照合)は
   追撃と両立しない(認めるべき枚数が 2^脚数 の羃集合になり、枚数だけでは
   どの脚が生きているか一意に決まらない)。ここでは脚ごとの三状態
   (OPEN / CLOSED / INCONSISTENT)から期待枚数を**導出**する。どの脚が閉じたかは
   枚数の引き算ではなく**注文行の消失**で決まるので、TP1 同士が同枚数でも曖昧に
   ならない。
3. **遷移回廊** —— 送信済み・未コミットの区間(WAL の PYRAMID_SENT と
   PYRAMID_COMMIT の間)だけ、枚数を `base <= qty <= base + add` の窓で認め、
   管理は FLATTEN 判定のみに絞る。恒久的な緩和ではない。

判定器は新しく発明しない。対の生死は `ownership_binder.bracket_structure`(R76)、
統合後の 1 組は `broker_status.oco_sibling_pair`(R80)をそのまま呼ぶ。
"""
from __future__ import annotations

import math
from typing import Any, Dict, Iterable, List, Optional, Tuple

import broker_status
import execution_contract
import ownership_binder

#: 合成プランの印。`build_management_plan()` が作る単一プランと型で区別する。
PLAN_KIND = "PYRAMID_COMPOSITE"

#: トランシェ内の脚 ID。トランシェ間の区別は `trancheId` が行い、ここは増やさない。
LEGS = ownership_binder.LEGS  # ("TP1", "RUNNER")

#: 脚の三状態。
OPEN = "OPEN"
CLOSED = "CLOSED"
INCONSISTENT = "INCONSISTENT"
#: 四番目の状態。統合(§5.3)の張り替えは成功したが、新しい OCO 兄弟 1 組の身元を
#: まだ束縛できていない区間だけに存在する。**古い脚の対はもう取り消されている**ので
#: CLOSED と読むと期待枚数が 0 になって所有権ごと落ちる —— 実際には建玉はブローカー
#: 側 OCO に守られて生きている。OPEN と同じく「生きている」と数え、ただし束縛が
#: 済むまで管理は FLATTEN だけに絞る(遷移回廊と同じ扱い)。
PENDING = "PENDING"
#: 建玉に枚数を寄与している状態。
LIVE_STATES = (OPEN, PENDING)

#: 位相。
ACCUMULATION = "ACCUMULATION"
RUNNERS = "RUNNERS"
FLAT = "CLOSED"

TICK = float(execution_contract.TICK)


# ----------------------------------------------------------------- 小道具


def _text(value: Any) -> str:
    return str(value or "").strip()


def _number(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _int(value: Any) -> Optional[int]:
    number = _number(value)
    if number is None:
        return None
    return int(number)


def _tick(value: float) -> float:
    return round(round(float(value) / TICK) * TICK, 10)


def _account(row: Dict[str, Any]) -> str:
    return _text(row.get("accountId") or row.get("account"))


def _position_side(position: Dict[str, Any]) -> str:
    side = _text(position.get("side")).upper()
    return {"LONG": "BUY", "SHORT": "SELL"}.get(side, side)


def _opposite(action: str) -> str:
    return {"BUY": "SELL", "SELL": "BUY"}.get(_text(action).upper(), "")


def settings(contract: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """``execution_contract.json`` の ``pyramid`` 節(R84 の追記込み)。"""
    source = contract if contract is not None else execution_contract.CONTRACT
    section = source.get("pyramid") if isinstance(source, dict) else None
    return section if isinstance(section, dict) else {}


def _setting_number(name: str, default: float,
                    contract: Optional[Dict[str, Any]] = None) -> float:
    value = _number(settings(contract).get(name))
    return default if value is None else value


def _setting_int(name: str, default: int,
                 contract: Optional[Dict[str, Any]] = None) -> int:
    value = _int(settings(contract).get(name))
    return default if value is None else value


def commit_settle(contract: Optional[Dict[str, Any]] = None) -> Tuple[int, float]:
    """束縛の短い待ち(送信直後は行が未出現。R52 実測値と同じ既定)。"""
    attempts = max(1, _setting_int("commitSettleAttempts", 8, contract))
    delay = max(0.0, _setting_number("commitSettleDelaySec", 0.5, contract))
    return attempts, delay


def final_target_policy(contract: Optional[Dict[str, Any]] = None) -> str:
    policy = _text(settings(contract).get("finalTargetPolicy")).upper()
    return policy or "LATEST_TRANCHE"


def min_add_qty(contract: Optional[Dict[str, Any]] = None) -> int:
    """`ultra_split` できる最小枚数。契約の ULTRA 最小枚数より下げない。"""
    envelope = execution_contract.CONTRACT.get("ultra") or {}
    floor = _int(envelope.get("minQtyPerAccount")) or 2
    return max(floor, _setting_int("minAddQty", 2, contract))


def order_type_policy(contract: Optional[Dict[str, Any]] = None) -> str:
    """v1 は成行のみ(§3.2)。未約定追撃の取消手段が `--flatten` しかないため。"""
    return _text(settings(contract).get("orderType")).upper() or "MARKET_ONLY"


# --------------------------------------------------- 脚の三状態(§1.2 / §4.2)


def _rows_by_id(orders: Iterable[Any]) -> Dict[str, Dict[str, Any]]:
    table: Dict[str, Dict[str, Any]] = {}
    for row in orders or []:
        if not isinstance(row, dict):
            continue
        row_id = _text(row.get("orderId"))
        if row_id:
            table.setdefault(row_id, row)
    return table


def _all_live_protective(ids: Iterable[str], table: Dict[str, Dict[str, Any]]) -> bool:
    """対の 2 行が **いま建玉を守っている** 状態か(R80)。

    ``SUSPENDED`` は Tradovate の OSO 子が親の約定を待つ状態で、建玉に対しては
    何もしない。つまり「対はあるがエントリーがまだ約定していない」なので、
    §1.2 の OPEN(エントリー約定済み **かつ** 対が PRESENT)には該当しない。
    """
    for order_id in ids:
        row = table.get(_text(order_id))
        if not isinstance(row, dict):
            return False
        if _text(row.get("status")).upper() not in broker_status.BROKER_LIVE_PROTECTIVE_STATES:
            return False
    return True


def leg_state(leg: Dict[str, Any], all_orders: List[Dict[str, Any]], *,
              account: str, symbol: str, action: str) -> Tuple[str, Optional[str]]:
    """1 本の脚を三状態へ落とす。判定材料は凍結 identity と現在の注文行だけ。

    * ``OPEN``   : エントリー約定済み かつ ブラケット対が PRESENT かつ 2 行とも live
    * ``CLOSED`` : ブラケット対が ABSENT(TP/SL どちらかが約定し OCO で対が消えた)
    * ``INCONSISTENT``: 片割れだけ残る・receipt 不一致・非 live・identity 欠落
    """
    if not isinstance(leg, dict):
        return INCONSISTENT, "leg is not an object"
    order_id = _text(leg.get("orderId"))
    receipt = _text(leg.get("receipt"))
    qty = _int(leg.get("qty"))
    if not order_id or not receipt:
        return INCONSISTENT, "leg identity (orderId/receipt) is missing"
    if qty is None or qty <= 0:
        return INCONSISTENT, "leg qty is invalid"
    ids = [_text(value) for value in leg.get("bracketOrderIds") or [] if _text(value)]
    if len(ids) != 2:
        return INCONSISTENT, "frozen bracket ids are invalid"
    structure, detail = ownership_binder.bracket_structure(
        leg, all_orders, account=account, symbol=symbol, action=action)
    if structure == "ABSENT":
        return CLOSED, None
    if structure != "PRESENT":
        return INCONSISTENT, detail or "bracket structure is inconsistent"
    if not _all_live_protective(ids, _rows_by_id(all_orders)):
        return INCONSISTENT, ("bracket children are not live (a SUSPENDED child means the "
                              "entry has not filled yet)")
    return OPEN, None


def consolidated_state(consolidation: Dict[str, Any], all_orders: List[Dict[str, Any]], *,
                       account: str, symbol: str, action: str) -> Tuple[str, Optional[str]]:
    """統合済みトランシェ(§5.3 後)の 1 組を `oco_sibling_pair` で照合する。"""
    if not isinstance(consolidation, dict):
        return INCONSISTENT, "consolidation record is not an object"
    ids = sorted(_text(value) for value in consolidation.get("orderIds") or [] if _text(value))
    if consolidation.get("pending") is True and not ids:
        return PENDING, "replacement OCO pair identity is not bound yet"
    if len(ids) != 2 or ids[0] == ids[1]:
        return INCONSISTENT, "frozen consolidation ids are invalid"
    table = _rows_by_id(all_orders)
    present = [order_id for order_id in ids if order_id in table]
    if not present:
        return CLOSED, None
    if len(present) != 2:
        return INCONSISTENT, "only one consolidation row remains"
    terminal = broker_status.BROKER_TERMINAL_STATES
    if any(_text(table[order_id].get("status")).upper() in terminal for order_id in ids):
        return CLOSED, None
    # R80 の判定器へ渡すのは **凍結した 2 行だけ**。全行を渡すと
    # 「scope に未終端の逆方向行がちょうど 2 本」という排他条件に化けて、
    # 統合の **後にもう一度追撃した**瞬間(1 組 + 新トランシェ 2 脚 = 6 行)に
    # INCONSISTENT へ倒れ、合成建玉まるごと所有権を失う。
    # 子ブラケットの有無だけは全行で見る —— `route_identity.bind_replacement_bracket`
    # と同じ渡し方で、判定器は 1 か所のまま。
    pair, reason = broker_status.oco_sibling_pair(
        [table[ids[0]], table[ids[1]]], account=account,
        expected_action=_opposite(action), symbol=symbol, all_rows=all_orders)
    if not pair:
        return INCONSISTENT, reason or "consolidation pair is not an OCO sibling pair"
    if sorted(_text(row.get("orderId")) for row in pair) != ids:
        return INCONSISTENT, "live OCO sibling pair does not match the frozen consolidation ids"
    receipts = sorted(_text(value) for value in consolidation.get("receipts") or [] if _text(value))
    if receipts and receipts != sorted(_text(row.get("receipt")) for row in pair):
        return INCONSISTENT, "consolidation receipt mismatch"
    return OPEN, None


def leg_states(plan: Dict[str, Any], all_orders: List[Dict[str, Any]], *,
               account: str, symbol: str, action: str) -> List[Dict[str, Any]]:
    """全トランシェ・全脚の状態表。順序はプランのトランシェ順・脚順。"""
    rows: List[Dict[str, Any]] = []
    for tranche in plan.get("tranches") or []:
        if not isinstance(tranche, dict):
            rows.append({"trancheId": None, "legId": None, "qty": 0,
                         "state": INCONSISTENT, "reason": "tranche is not an object"})
            continue
        tranche_id = _text(tranche.get("trancheId"))
        consolidation = tranche.get("consolidationPair")
        for leg in tranche.get("legs") or []:
            leg_id = _text((leg or {}).get("id")).upper() if isinstance(leg, dict) else ""
            qty = _int((leg or {}).get("qty")) if isinstance(leg, dict) else None
            if isinstance(consolidation, dict):
                state, reason = consolidated_state(
                    consolidation, all_orders, account=account, symbol=symbol, action=action)
            else:
                state, reason = leg_state(
                    leg if isinstance(leg, dict) else {}, all_orders,
                    account=account, symbol=symbol, action=action)
            rows.append({"trancheId": tranche_id, "legId": leg_id,
                         "qty": qty if qty and qty > 0 else 0,
                         "target": _number((leg or {}).get("target")) if isinstance(leg, dict) else None,
                         "state": state, "reason": reason,
                         "consolidated": isinstance(consolidation, dict)})
    return rows


def expected_qty(states: Iterable[Dict[str, Any]]) -> int:
    """期待枚数 = Σ 生きている脚の qty。枚数の列挙は使わない(§1.2)。"""
    return sum(int(row.get("qty") or 0) for row in states or []
               if row.get("state") in LIVE_STATES)


def phase(plan: Dict[str, Any], states: Iterable[Dict[str, Any]]) -> str:
    """位相(§5.2/§5.4)。価格は使わない —— 約定の事実は構造で確定する(R35)。"""
    rows = [row for row in states or [] if row.get("state") in LIVE_STATES]
    if any(_text(row.get("legId")).upper() == LEGS[0] and row.get("state") == OPEN
           for row in rows):
        return ACCUMULATION
    if rows:
        return RUNNERS
    return FLAT


def open_legs(states: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [dict(row) for row in states or [] if row.get("state") in LIVE_STATES]


# --------------------------------------------------- 目標価格(§5.2 / §5.3)


def _farther(side: str, first: Optional[float], second: Optional[float]) -> Optional[float]:
    if first is None:
        return second
    if second is None:
        return first
    return max(first, second) if side == "BUY" else min(first, second)


def flatten_beyond(plan: Dict[str, Any], states: Iterable[Dict[str, Any]]) -> Optional[float]:
    """最遠 live runner 目標。ここを越えて建玉が残るのは構造的にありえない。"""
    side = _text(plan.get("side")).upper()
    value = None
    for row in states or []:
        if row.get("state") not in LIVE_STATES or _text(row.get("legId")).upper() != LEGS[1]:
            continue
        value = _farther(side, value, _number(row.get("target")))
    return value


def policy_target(plan: Dict[str, Any], states: Iterable[Dict[str, Any]],
                  contract: Optional[Dict[str, Any]] = None) -> Optional[float]:
    """統合後 TP の方針値。既定 ``LATEST_TRANCHE``(最新シグナルの幾何)。"""
    side = _text(plan.get("side")).upper()
    live = [row for row in states or []
            if row.get("state") in LIVE_STATES and _text(row.get("legId")).upper() == LEGS[1]]
    if not live:
        return None
    if final_target_policy(contract) == "FARTHEST":
        value = None
        for row in live:
            value = _farther(side, value, _number(row.get("target")))
        return value
    order = {_text(tranche.get("trancheId")): index
             for index, tranche in enumerate(plan.get("tranches") or [])
             if isinstance(tranche, dict)}
    live.sort(key=lambda row: order.get(_text(row.get("trancheId")), -1))
    return _number(live[-1].get("target"))


# ----------------------------------------------------- 合成プランの組み立て


def _leg_identity(route_snapshot: Iterable[Any], leg_id: str,
                  account: str) -> Optional[Dict[str, Any]]:
    for row in route_snapshot or []:
        if not isinstance(row, dict):
            continue
        if _text(row.get("state")).upper() != "ACCEPTED":
            continue
        if _text(row.get("legId")).upper() != _text(leg_id).upper():
            continue
        if account and _text(row.get("accountId")) != account:
            continue
        return {"orderId": _text(row.get("orderId")),
                "receipt": _text(row.get("receipt")),
                "bracketOrderIds": [_text(value) for value in
                                    row.get("bracketOrderIds") or [] if _text(value)],
                "bracketReceipts": [_text(value) for value in
                                    row.get("bracketReceipts") or [] if _text(value)]}
    return None


def tranche_from_plan(plan: Dict[str, Any], *, tranche_id: str,
                      account: str) -> Dict[str, Any]:
    """単一プラン(`build_management_plan` 出力 + 束縛済み routeSnapshot)を
    トランシェ 1 本へ写す。**identity を発明しない** —— 取れない脚は空のまま残し、
    `leg_state` が INCONSISTENT として fail closed する。"""
    route = plan.get("routeSnapshot") if isinstance(plan.get("routeSnapshot"), list) else []
    legs: List[Dict[str, Any]] = []
    for leg in plan.get("legs") or []:
        if not isinstance(leg, dict):
            continue
        leg_id = _text(leg.get("id")).upper()
        if leg_id not in LEGS:
            continue
        row = {"id": leg_id, "qty": _int(leg.get("qty")) or 0,
               "target": _number(leg.get("target"))}
        identity = _leg_identity(route, leg_id, account)
        if identity:
            row.update(identity)
        legs.append(row)
    return {
        "trancheId": _text(tranche_id),
        "entryKey": _text(plan.get("entryKey")),
        "scenarioId": _text(plan.get("scenarioId")),
        "model": plan.get("model"),
        "grade": plan.get("grade"),
        "entry": _number(plan.get("entry")),
        "entryReference": _number(plan.get("entryReference")),
        "initialStop": _number(plan.get("initialStop")),
        "orderType": _text(plan.get("entryOrderType") or plan.get("orderType") or "MARKET").upper(),
        "legs": legs,
        "routeSnapshot": [dict(row) for row in route if isinstance(row, dict)],
    }


def build_add_tranche(scenario: Dict[str, Any], *, tranche_id: str, add_qty: int,
                      add_price: Any, entry_key: str) -> Dict[str, Any]:
    """追撃シグナルから新トランシェの**幾何**だけを作る(identity は送信後に焼く)。

    枚数は `ultra_split` の比率分割。TP は新シグナルの targets[0] / targets[-1]。
    幾何が不正なら ``ValueError``(推測で埋めない)。
    """
    side = _text(scenario.get("side")).upper()
    if side not in {"BUY", "SELL"}:
        raise ValueError("PYRAMID_SIDE_INVALID")
    price = _number(add_price)
    stop = _number(scenario.get("stop"))
    if price is None or price <= 0 or stop is None or stop <= 0:
        raise ValueError("PYRAMID_ADD_GEOMETRY_UNAVAILABLE")
    raw_targets = scenario.get("targets")
    if not isinstance(raw_targets, list):
        raw_targets = [scenario.get("target")]
    targets = [_tick(value) for value in (_number(item) for item in raw_targets)
               if value is not None]
    targets = [target for target in targets
               if (target > price if side == "BUY" else target < price)]
    if len(targets) < 2 or targets[0] == targets[-1]:
        raise ValueError("PYRAMID_SPLIT_TARGETS_REQUIRED")
    if (side == "BUY" and stop >= price) or (side == "SELL" and stop <= price):
        raise ValueError("PYRAMID_STOP_ON_WRONG_SIDE")
    qty = _int(add_qty)
    if qty is None or qty <= 0:
        raise ValueError("PYRAMID_ADD_QTY_INVALID")
    split = execution_contract.ultra_split(qty)
    if split is None:
        raise ValueError("PYRAMID_ADD_SPLIT_NOT_REPRESENTABLE")
    return {
        "trancheId": _text(tranche_id),
        "entryKey": _text(entry_key),
        "scenarioId": _text(scenario.get("scenarioId")),
        "model": scenario.get("model"),
        "grade": scenario.get("grade"),
        "entry": _tick(price),
        "entryReference": _tick(price),
        "initialStop": _tick(stop),
        "orderType": "MARKET",
        "legs": [{"id": LEGS[0], "qty": split[0], "target": targets[0]},
                 {"id": LEGS[1], "qty": split[1], "target": targets[-1]}],
        "routeSnapshot": [],
    }


def bind_tranche_identity(tranche: Dict[str, Any], route_snapshot: Iterable[Any],
                          *, account: str) -> Dict[str, Any]:
    """送信後の route snapshot からトランシェの脚 identity を焼き込む。"""
    rows = [dict(row) for row in route_snapshot or [] if isinstance(row, dict)]
    legs = []
    for leg in tranche.get("legs") or []:
        row = dict(leg)
        identity = _leg_identity(rows, _text(row.get("id")), account)
        if identity:
            row.update(identity)
        legs.append(row)
    return {**tranche, "legs": legs, "routeSnapshot": rows}


def _flatten_open_legs(tranches: Iterable[Any], states: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    live = {(_text(row.get("trancheId")), _text(row.get("legId")).upper())
            for row in states or [] if row.get("state") in LIVE_STATES}
    flattened = []
    for tranche in tranches or []:
        if not isinstance(tranche, dict):
            continue
        tranche_id = _text(tranche.get("trancheId"))
        for leg in tranche.get("legs") or []:
            if not isinstance(leg, dict):
                continue
            leg_id = _text(leg.get("id")).upper()
            if states is not None and (tranche_id, leg_id) not in live:
                continue
            flattened.append({"id": leg_id, "qty": _int(leg.get("qty")) or 0,
                              "target": _number(leg.get("target")),
                              "trancheId": tranche_id})
    return flattened


def build_composite_plan(base_plan: Dict[str, Any], tranches: List[Dict[str, Any]], *,
                         entry_key: str, account: str, verdict: Optional[Dict[str, Any]] = None,
                         states: Optional[List[Dict[str, Any]]] = None,
                         adds_done: int = 0, combined_entry: Optional[float] = None,
                         combined_risk: Optional[float] = None,
                         trail_distance: Optional[float] = None,
                         contract: Optional[Dict[str, Any]] = None,
                         consolidation: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """合成プラン(§2.1)。既存の top-level 互換キーを**すべて保持**する。

    `position_card` / Mini App / `trade_journal` が読む top-level を壊さないこと
    が条件なので、`planKind` と `tranches` / `pyramid` は**追記**として足す。
    """
    verdict = verdict if isinstance(verdict, dict) else {}
    tranches = [dict(row) for row in tranches or [] if isinstance(row, dict)]
    if not tranches:
        raise ValueError("PYRAMID_COMPOSITE_REQUIRES_TRANCHES")
    side = _text(base_plan.get("side")).upper()
    latest = tranches[-1]
    governing = _number(latest.get("initialStop"))
    if governing is None:
        raise ValueError("PYRAMID_GOVERNING_STOP_UNAVAILABLE")
    legs = _flatten_open_legs(tranches, states) if states is not None else _flatten_open_legs(
        tranches, None)
    qty = sum(int(leg.get("qty") or 0) for leg in legs)
    live_states = states if states is not None else [
        {"trancheId": leg.get("trancheId"), "legId": leg.get("id"), "qty": leg.get("qty"),
         "target": leg.get("target"), "state": OPEN} for leg in legs]
    beyond = flatten_beyond({"side": side}, live_states)
    final = policy_target({"side": side, "tranches": tranches}, live_states, contract)
    tp1 = None
    for row in live_states:
        if row.get("state") in LIVE_STATES and _text(row.get("legId")).upper() == LEGS[0]:
            tp1 = _farther(_opposite(side), tp1, _number(row.get("target")))
    entry = _number(combined_entry)
    if entry is None:
        entry = _number(base_plan.get("entry"))
    trail = _number(trail_distance)
    if trail is None:
        trail = _number(base_plan.get("trailDistance"))
    if trail is None or trail <= 0:
        trail = TICK
    max_adds = _int(settings(contract).get("maxAdds")) or 0
    plan = {
        # ---- 既存互換(単一プランと同じキー) ----
        **{key: base_plan.get(key) for key in (
            "planVersion", "scenarioId", "fingerprint", "evidenceHash", "marketCycleId",
            "decisionId", "symbol", "model", "grade", "targetR", "riskCapPoints",
            "riskCapDollars", "riskCapSource", "decisionEvidence", "decisionStructure")},
        "planKind": PLAN_KIND,
        "entryKey": _text(entry_key),
        "accountScope": [_text(account)],
        "side": side,
        "qty": qty,
        "entry": _tick(entry) if entry is not None else None,
        "entryOrderType": "MARKET",
        "entryReference": _tick(entry) if entry is not None else None,
        "initialStop": _tick(governing),
        "riskPoints": (_tick(abs(entry - governing)) if entry is not None else None),
        "riskDollars": (round(combined_risk, 2) if _number(combined_risk) is not None
                        else None),
        "tp1": tp1,
        "finalTarget": final,
        "targets": [value for value in (tp1, final) if value is not None],
        "legs": legs,
        "trailDistance": _tick(trail),
        "mode": "SPLIT_BRACKETS_TP1_RUNNER",
        "ultra": True,
        "ultraDrawdown": _number(base_plan.get("ultraDrawdown")),
        # ---- R84 追記 ----
        "pyramid": {
            "addsDone": int(adds_done or 0),
            "maxAdds": max_adds,
            "combinedEntry": _number(combined_entry),
            "combinedRisk": _number(combined_risk),
            "governingStop": _tick(governing),
            "flattenBeyond": beyond,
        },
        "tranches": tranches,
        "consolidation": consolidation if isinstance(consolidation, dict) else None,
    }
    return plan


def collapse_to_consolidated(plan: Dict[str, Any], *, consolidation: Dict[str, Any],
                             states: Optional[List[Dict[str, Any]]] = None,
                             contract: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """統合(§5.3)後の再凍結。トランシェ群を 1 脚の統合トランシェへ畳む。

    統合後の照合は `oco_sibling_pair` 1 組で、既存 runner 管理と完全に同形になる
    (自己相似)。畳んだ後も `tranches` は残すので、さらに追撃したときは
    「統合済み T1 + 新 T2」という同じ形へ素直に伸びる。
    """
    qty = _int(consolidation.get("qty"))
    if qty is None or qty <= 0:
        raise ValueError("PYRAMID_CONSOLIDATION_QTY_INVALID")
    ids = [_text(value) for value in consolidation.get("orderIds") or [] if _text(value)]
    pending = consolidation.get("pending") is True and not ids
    # 張り替えは成功したが新しい 1 組の身元をまだ取れていない区間も **必ず畳んで
    # 凍結する**。ここで例外にすると、古い bracket id しか持たないプランが正本の
    # まま残り、次の周期に「対が消えた」= 全脚 CLOSED と読まれて所有権ごと落ちる
    # (= 実弾がブローカー OCO だけで放置される)。
    if not pending and len(set(ids)) != 2:
        raise ValueError("PYRAMID_CONSOLIDATION_IDS_INVALID")
    target = _number(consolidation.get("tp"))
    if target is None:
        target = _number(plan.get("finalTarget"))
    stop = _number(consolidation.get("sl"))
    tranches = [row for row in plan.get("tranches") or [] if isinstance(row, dict)]
    source = tranches[-1] if tranches else {}
    # 構造 SL(= R の分母)は動かさない。張り替えた保護水準は consolidationPair.sl。
    governing_stop = _number(plan.get("initialStop"))
    merged = {
        "trancheId": "C" + str(len(tranches)),
        "entryKey": _text(plan.get("entryKey")),
        "scenarioId": _text(source.get("scenarioId") or plan.get("scenarioId")),
        "model": source.get("model") or plan.get("model"),
        "grade": source.get("grade") or plan.get("grade"),
        "entry": _number(plan.get("entry")),
        "entryReference": _number(plan.get("entryReference")),
        "initialStop": governing_stop,
        "orderType": "MARKET",
        "legs": [{"id": LEGS[1], "qty": qty, "target": target}],
        "consolidationPair": {"orderIds": sorted(ids),
                              "receipts": sorted(_text(value) for value in
                                                 consolidation.get("receipts") or []
                                                 if _text(value)),
                              "qty": qty, "sl": stop, "tp": target,
                              **({"pending": True} if pending else {})},
        "mergedFrom": [_text(row.get("trancheId")) for row in tranches],
        # 畳んだトランシェの決定(scenarioId)を全部残す。統合トランシェの scenarioId は
        # 1 つしか持てないので、ここを落とすと「既に建玉を作った決定」を忘れ、同じ
        # シグナルの再掲で建て増す門(engine._pyramid_consumed_decisions)が緩む。
        "sourceScenarioIds": sorted({
            text for row in tranches
            for text in ([_text(row.get("scenarioId"))]
                         + [_text(value) for value in row.get("sourceScenarioIds") or []]
                         + [_text(plan.get("scenarioId"))])
            if text}),
        "routeSnapshot": [],
    }
    pyramid = dict(plan.get("pyramid") or {})
    pyramid["flattenBeyond"] = target
    pyramid["governingStop"] = governing_stop
    pyramid["protectiveStop"] = stop
    return {**plan, "tranches": [merged], "consolidation": dict(consolidation),
            "qty": qty, "legs": [{"id": LEGS[1], "qty": qty, "target": target,
                                  "trancheId": merged["trancheId"]}],
            "tp1": None, "finalTarget": target,
            "targets": [value for value in (target,) if value is not None],
            "pyramid": pyramid}


# --------------------------------------------------------- 束縛(§4)


def _reason(reason: str, states: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    return {"owned": False, "state": "UNOWNED", "reason": reason,
            "legStates": states or []}


def bind_composite(plan: Dict[str, Any], broker_position: Dict[str, Any],
                   broker_orders: Dict[str, Any], *, position_generation: Optional[str],
                   corridor_add_qty: Optional[int] = None) -> Dict[str, Any]:
    """合成プランの所有権束縛(§4)。``ownership_binder.bind()`` は一行も変えない。

    fail closed は既存と同じ: INCONSISTENT・identity 欠落・口座/銘柄/方向不一致・
    receipt 衝突は所有しない。**構造が読めない建玉を枚数の辻褄で救済しない。**
    """
    if not isinstance(plan, dict) or _text(plan.get("planKind")) != PLAN_KIND:
        return _reason("plan is not a pyramid composite plan")
    if not isinstance(broker_position, dict):
        return _reason("plan/position is unavailable")
    if broker_position.get("verified") is not True:
        return _reason("broker position is UNVERIFIED")
    if not isinstance(broker_orders, dict) or broker_orders.get("verified") is not True:
        return _reason("broker orders are UNVERIFIED")
    scope = [_text(value) for value in plan.get("accountScope") or [] if _text(value)]
    if not scope or len(scope) != len(set(scope)):
        return _reason("explicit unique account scope is required")
    account = _account(broker_position)
    if account not in set(scope):
        return _reason("broker position account is outside frozen scope")
    symbol = _text(plan.get("symbol"))
    action = _text(plan.get("side")).upper()
    if not symbol or action not in {"BUY", "SELL"}:
        return _reason("frozen composite economics are incomplete")
    if _text(broker_position.get("symbol")) != symbol:
        return _reason("aggregate position account/symbol mismatch")
    all_orders = broker_orders.get("orders")
    if not isinstance(all_orders, list):
        return _reason("broker order rows are unavailable")
    tranches = [row for row in plan.get("tranches") or [] if isinstance(row, dict)]
    if not tranches:
        return _reason("composite plan has no tranches")

    states = leg_states(plan, all_orders, account=account, symbol=symbol, action=action)
    if not states:
        return _reason("composite plan has no legs", states)
    bad = next((row for row in states if row.get("state") == INCONSISTENT), None)
    if bad is not None:
        return _reason("composite tranche structure is inconsistent "
                       f"({bad.get('trancheId')}/{bad.get('legId')}: {bad.get('reason')})", states)

    # identity の重複(同じ orderId/receipt を 2 つの脚が名乗る)は所有しない。
    identities = []
    for tranche in tranches:
        for leg in tranche.get("legs") or []:
            if not isinstance(leg, dict):
                continue
            if isinstance(tranche.get("consolidationPair"), dict):
                continue
            identities.append((_text(leg.get("orderId")), _text(leg.get("receipt"))))
    if len(identities) != len(set(identities)):
        return _reason("composite leg identities collide", states)

    frozen_ids = {order_id for order_id, _receipt in identities if order_id}
    for tranche in tranches:
        for value in (tranche.get("consolidationPair") or {}).get("orderIds") or []:
            frozen_ids.add(_text(value))
        for leg in tranche.get("legs") or []:
            if isinstance(leg, dict):
                frozen_ids.update(_text(value) for value in leg.get("bracketOrderIds") or []
                                  if _text(value))
    # 同方向の **親行**(parentId 無し)が他にあれば、どの建玉か分からない(R52 と同じ規律)。
    for row in all_orders:
        if not isinstance(row, dict) or _text(row.get("orderId")) in frozen_ids:
            continue
        if (_text(row.get("status")).upper() in broker_status.BROKER_ACTIVE_STATES
                and _account(row) == account and _text(row.get("symbol")) == symbol
                and _text(row.get("action")).upper() == action
                and not _text(row.get("parentId"))):
            return _reason("active colliding broker entry row", states)

    expected = expected_qty(states)
    try:
        position_qty = int(broker_position.get("qty") or 0)
    except (TypeError, ValueError):
        return _reason("broker position qty is invalid", states)
    if position_qty <= 0:
        return _reason("composite plan requires an open position", states)
    if expected <= 0:
        # 全脚 CLOSED なのに建玉が残る。ブローカー上でこの建玉と凍結経路を結ぶものが
        # 何も無い(既存 bind の「bracket-verified legs have no live structure」と同じ帰結)。
        return _reason("composite legs have no live structure on the broker", states)
    if _position_side(broker_position) != action:
        return _reason("aggregate position account/symbol/side mismatch", states)

    # 回廊(§4.3)は「送信済み・未コミット」の区間だけ枚数の窓を開ける。
    # PENDING(統合の張り替え済み・身元未束縛)は枚数は一致するが管理を絞る。
    corridor = False
    if position_qty != expected:
        add_qty = _int(corridor_add_qty)
        if add_qty is None or add_qty <= 0:
            return _reason(f"broker position qty {position_qty} does not match the "
                           f"structure-derived expectation {expected}", states)
        if not expected <= position_qty <= expected + add_qty:
            return _reason(f"broker position qty {position_qty} is outside the pyramid "
                           f"transition corridor [{expected}, {expected + add_qty}]", states)
        corridor = True
    transit = corridor or any(row.get("state") == PENDING for row in states)

    raw_identity = broker_status.position_identity(broker_position)
    generation = ownership_binder.generation_wrapper(position_generation, raw_identity or "")
    if not raw_identity or not generation:
        return _reason("broker-derived raw identity/generation wrapper is unavailable", states)

    ownership = {"generation": generation, "rawIdentity": raw_identity,
                 "accountId": account, "symbol": symbol, "side": action,
                 "orderId": broker_position.get("orderId"),
                 "receipt": broker_position.get("receipt"),
                 "filledAt": broker_position.get("filledAt"),
                 "avgEntry": broker_position.get("avgEntry"),
                 "initialQty": broker_position.get("initialQty") or position_qty,
                 "actualFillPrice": _number(broker_position.get("avgEntry"))}
    pyramid = dict(plan.get("pyramid") or {})
    pyramid["flattenBeyond"] = flatten_beyond(plan, states)
    owned_plan = {**plan, "accountScope": [account], "positionOwnership": ownership,
                  "legStates": states, "expectedQty": expected,
                  "legs": _flatten_open_legs(tranches, states),
                  "pyramid": pyramid,
                  "phase": phase(plan, states)}
    if not corridor:
        owned_plan["qty"] = expected
    return {"owned": True, "state": "PYRAMID_TRANSIT" if transit else "OWNED_COMPOSITE",
            "reason": None, "legStates": states, "expectedQty": expected,
            "positionOwnership": ownership, "ownedPlan": owned_plan,
            "phase": phase(plan, states)}


# ------------------------------------------------ 管理位相機械(§5.2 / §5.3)


def management_action_composite(plan: Dict[str, Any], position: Dict[str, Any], *,
                                price: Optional[float],
                                states: Optional[List[Dict[str, Any]]] = None,
                                current_stop: Optional[float] = None,
                                best: Optional[float] = None,
                                stop_buffer: float = 4.0,
                                breakeven_offset: float = 1.0,
                                force_flatten: bool = False,
                                kill: bool = False,
                                session_flatten: bool = False,
                                transit: bool = False,
                                contract: Optional[Dict[str, Any]] = None
                                ) -> Optional[Dict[str, Any]]:
    """合成建玉に対する次の一手。外部状態を変更しない。

    ACCUMULATION(TP1 脚が 1 本でも OPEN)では **MODIFY を出さない**。
    `cancelandbracket` は口座×銘柄の保護注文を全部取り消すので、TP1 を殺さずに
    張り替える手段が無い(CLAUDE.md §4-1「TP1 を消さない」を合成でも守る)。
    """
    side = _text(plan.get("side")).upper()
    if _position_side(position) != side:
        return {"action": "HALT", "reason": "broker position side differs from frozen plan"}
    try:
        qty = int(position.get("qty") or 0)
    except (TypeError, ValueError):
        return {"action": "HALT", "reason": "broker position qty is invalid"}
    if qty <= 0:
        return None
    if states is None:
        states = plan.get("legStates") if isinstance(plan.get("legStates"), list) else []
    current_phase = phase(plan, states)
    reference = _number(price)
    if reference is None:
        return {"action": "HALT", "reason": "current price unavailable"}

    governing = _number(plan.get("initialStop"))
    beyond = _number((plan.get("pyramid") or {}).get("flattenBeyond"))
    if beyond is None:
        beyond = flatten_beyond(plan, states)
    if side == "BUY":
        reached_stop = governing is not None and reference <= governing
        reached_beyond = beyond is not None and reference >= beyond
    else:
        reached_stop = governing is not None and reference >= governing
        reached_beyond = beyond is not None and reference <= beyond

    flatten_reason = None
    if reached_beyond:
        flatten_reason = "farthest live runner target reached while position remains open"
    elif reached_stop:
        flatten_reason = "governing structure stop breached while position remains open"
    elif force_flatten:
        flatten_reason = "forceFlatten flag"
    elif kill:
        flatten_reason = "NQX_AUTOTRADE_KILL"
    elif session_flatten:
        flatten_reason = "configured session cutoff"
    if flatten_reason:
        return {"action": "FLATTEN", "qty": qty, "reason": flatten_reason,
                "phase": current_phase}

    if transit:
        # 遷移回廊(§4.3): 管理は FLATTEN 判定のみ。MODIFY も追撃の再評価もしない。
        return None
    if current_phase == ACCUMULATION:
        # TP1 約定済みトランシェの runner は自分の構造 SL の OCO が守っている。
        # 建値移動は次位相まで **明示的に見送る**(注記は engine が毎周期出す)。
        return None
    if current_phase != RUNNERS:
        return {"action": "HALT",
                "reason": "composite phase has no live leg while the position remains open"}

    entry = _number(position.get("avgEntry"))
    if entry is None:
        entry = _number(plan.get("entry"))
    if entry is None:
        return {"action": "HALT", "reason": "composite entry price unavailable"}
    stop = _number(current_stop)
    if stop is None:
        stop = governing
    if stop is None:
        return {"action": "HALT", "reason": "composite governing stop unavailable"}
    distance = _number(plan.get("trailDistance")) or TICK
    distance = max(distance, TICK)
    buffer = _number(stop_buffer)
    buffer = 0.0 if buffer is None else buffer
    offset = _number(breakeven_offset)
    offset = 0.0 if offset is None else offset
    floor = entry + offset if side == "BUY" else entry - offset
    extreme = _number(best)
    if side == "BUY":
        desired = max(floor, (extreme - distance) if extreme is not None else floor)
        improved = desired > stop + TICK / 2
        protective = desired <= reference - buffer
    else:
        desired = min(floor, (extreme + distance) if extreme is not None else floor)
        improved = desired < stop - TICK / 2
        protective = desired >= reference + buffer
    if not improved:
        return None
    if not protective:
        # R70 と同じ床の救済。床は現在値の正しい側にしか置かないので逆側 SL は生じない。
        if side == "BUY":
            floor_ok = floor <= reference - buffer and floor > stop + TICK / 2
        else:
            floor_ok = floor >= reference + buffer and floor < stop - TICK / 2
        if not floor_ok:
            return None
        desired = floor
    target = policy_target(plan, states, contract)
    if target is None:
        return {"action": "HALT", "reason": "composite consolidation target unavailable"}
    reached_target = (reference >= target) if side == "BUY" else (reference <= target)
    if reached_target:
        # 方針値(最新トランシェ)を価格が既に追い越している。最遠 live runner 目標は
        # 上の flattenBeyond で未達を確認済みなので、そちらを TP にする(現在値の先)。
        target = beyond if beyond is not None else target
    live = open_legs(states)
    return {"action": "MODIFY", "qty": qty, "sl": _tick(desired), "tp": target,
            "reason": ("pyramid consolidation: all TP1 legs resolved; collapsing "
                       f"{len(live)} runner bracket(s) into one protected position"),
            "phase": current_phase, "consolidate": True, "openLegs": len(live)}

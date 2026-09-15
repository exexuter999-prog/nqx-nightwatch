#!/usr/bin/env python3
"""R22 single broker-truth ownership binder.

This module is deliberately pure.  It never fills broker fields from the
request: frozen route identities, broker order economics, and the aggregate
position must all agree before a plan may manage a position or resting order.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, Optional

import broker_status
import execution_contract

LEGS = ("TP1", "RUNNER")


def _text(value: Any) -> str:
    return str(value or "").strip()


def _price(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number


def _account(row: Dict[str, Any]) -> str:
    return _text(row.get("accountId") or row.get("account"))


def _position_side(position: Dict[str, Any]) -> str:
    side = _text(position.get("side")).upper()
    return {"LONG": "BUY", "SHORT": "SELL"}.get(side, side)


def _entry_price(row: Dict[str, Any], order_type: str) -> Optional[float]:
    if order_type == "LIMIT":
        return _price(row.get("limitPrice"))
    for field in ("filledPrice", "avgFillPrice", "averageFillPrice", "price"):
        value = _price(row.get(field))
        if value is not None:
            return value
    return None


def _fields_incomplete(row: Dict[str, Any]) -> bool:
    """数量・種別・価格を **ブローカーが返していない** 行か(R52)。

    CrossTrade の Tradovate 経路は一覧でも per-order 詳細でも返さない
    (broker_status R41 / route_identity R52 と同じ実測)。
    """
    if row.get("fieldsComplete") is False:
        return True
    return (row.get("qty") is None and not _text(row.get("orderType"))
            and row.get("limitPrice") is None)


def _position_lacks_order_link(position: Dict[str, Any]) -> bool:
    """建玉行が注文との対応(receipt)を持たないブローカーか(R52)。

    CrossTrade/Tradovate の建玉行は receipt が null で、orderId は注文 ID ではなく
    内部の建玉 ID(2026-09-05 02:09 実測: 注文 …150/…157 に対し建玉 …081)。
    """
    return not _text(position.get("receipt"))


def _avg_entry_consistent(position: Dict[str, Any], order_type: str, entry: float,
                          filled_rows: list, favorable_limit: Optional[float] = None) -> bool:
    """建玉の平均建値が凍結プラン(指値)/約定行と整合するか(R52)。

    直接照合が無い分の補強。指値は「その価格か、それより有利」でしか約定しない
    ので、不利側にズレていれば別の建玉。乖離の上限は契約の maxDeviationPoints。
    """
    avg = _price(position.get("avgEntry"))
    if avg is None or avg <= 0:
        return False
    # R52: 成行の avgEntry は複数約定の加重平均で **tick に乗らない**
    # (2026-09-05 04:51 実測: 10@29570.25 + 10@29570.5 = 29570.375)。tick 整合は
    # 指値だけに要求し、成行は下の乖離ゲートで妥当性を見る。
    if order_type == "LIMIT" and not _tick(avg):
        return False
    side = _position_side(position)
    if order_type == "LIMIT":
        favorable = avg <= entry + 1e-9 if side == "BUY" else avg >= entry - 1e-9
        limit = float(executionContractDeviation())
        # R75: 有利側の乖離は別建玉の証拠ではない。指値は「その価格か、より有利」でしか
        # 約定せず、再開直後の gap や薄い板では数 pt 有利に滑る(2026-09-09 07:06 実測:
        # 指値 29,531.25 に対し脚 1 が 29,526.75 で約定、平均 29,529.0 の乖離 2.25pt が
        # 上限 2.0pt を超えて LONG 4 枚が未所有 → ブローカー OCO だけで放置)。有利側の
        # 上限は呼び出し側が渡す凍結プランの SL 幅(それ以上有利な建玉は同じ注文では
        # ない)。不利側は従来どおり 1 tick も認めない。
        bound = limit
        if favorable_limit is not None:
            try:
                bound = max(limit, float(favorable_limit))
            except (TypeError, ValueError):
                bound = limit
        return favorable and abs(avg - entry) <= bound + 1e-9
    fills = [_price(row.get("actualEntry")) for row in filled_rows]
    fills = [value for value in fills if value is not None]
    if not fills:
        return False
    return abs(avg - sum(fills) / len(fills)) <= float(executionContractDeviation()) + 1e-9


def executionContractDeviation() -> float:
    return float(execution_contract.CONTRACT["marketOrder"]["maxDeviationPoints"])


def ledger_backed_ownership() -> bool:
    """R79(2026-09-12 ユーザー決定): 台帳の注文は nightwatch の所有物とみなすか。

    成行の価格乖離ゲート(R52/R76)は「この建玉は他人のものかもしれない」ことだけを
    守っている。凍結 route snapshot の ``orderId`` と ``receipt`` で身元が取れた脚は
    自前の送信記録が出所を証明しているので、そのリスクは identity 側で既に排除されて
    いる。乖離だけを理由に所有しないと、SL 建値移動もトレールも掛からない実弾が
    ブローカー OCO だけで放置される(2026-09-12 01:30 の SHORT 4、不利側 6.25pt)。

    ``ownershipAttribution.manualHalt`` が緊急停止で、ON の間は従来の厳格な扱いへ
    戻る。身元(orderId/receipt/口座/銘柄/方向/枚数/ブラケット構造)の検査はこの
    スイッチでは一切緩まない。
    """
    cfg = execution_contract.CONTRACT.get("ownershipAttribution")
    if not isinstance(cfg, dict):
        return False
    if cfg.get("manualHalt") is True:
        return False
    return cfg.get("ledgerBackedOwnership") is True


def _market_fill_within_reference(actual: float, entry: float, side: str,
                                  favorable_limit: Optional[float]) -> bool:
    """成行の平均建値が凍結参照価格と整合するか(R52 + R76)。

    不利側は契約の ``maxDeviationPoints`` のまま。有利側は R75(指値)と同じ理由で
    max(maxDeviationPoints, SL 幅) まで認める —— 参照価格は bundle の現在値で最大 60 秒
    古く、成行は市場が動いた先で約定する。有利に約定した建玉は別の建玉の証拠ではない
    (2026-09-08 10:34 実測: 参照 29,590.0 に対し LONG 8 枚が 29,585.875 で約定。有利側
    4.125pt が上限 2.0pt を超え、identity が束縛できていても所有権は成立しなかった)。
    """
    limit = executionContractDeviation()
    bound = limit
    if favorable_limit is not None:
        try:
            bound = max(limit, float(favorable_limit))
        except (TypeError, ValueError):
            bound = limit
    diff = float(actual) - float(entry)
    favorable = diff <= 1e-9 if side == "BUY" else diff >= -1e-9
    return abs(diff) <= (bound if favorable else limit) + 1e-9


def _bracket_structure(frozen_row: Dict[str, Any], all_orders: list, *, account: str,
                       symbol: str, action: str) -> tuple:
    """凍結した子 2 行(``bracketOrderIds``)が今の一覧で構造的に整合するか(R76)。

    戻り値 ``(状態, 理由)``。状態は

    * ``PRESENT``: 2 本とも口座・銘柄が一致し、建玉と逆方向、active、相互(または共通の
      親で)結ばれ、receipt が凍結値と一致し、名乗る真の親が凍結 orderId と一致する
    * ``ABSENT``: 2 本とも一覧に無い(決済・OCO 取消で消費済み。TP1 後の TP1 脚がこれ)
    * ``INCONSISTENT``: 片方だけ残る、口座/方向/receipt/結線が違う、終端の行として残る

    価格は照合しない(ブローカーが返さない。R52 の MODIFY 照合と同じ構造モード)。
    """
    ids = [_text(value) for value in frozen_row.get("bracketOrderIds") or [] if _text(value)]
    receipts = [_text(value) for value in frozen_row.get("bracketReceipts") or [] if _text(value)]
    if len(ids) != 2 or ids[0] == ids[1]:
        return "INCONSISTENT", "frozen bracket ids are invalid"
    rows: Dict[str, Dict[str, Any]] = {}
    for row in all_orders:
        if not isinstance(row, dict):
            continue
        row_id = _text(row.get("orderId"))
        if row_id in ids:
            if row_id in rows:
                return "INCONSISTENT", "duplicated bracket row"
            rows[row_id] = row
    if not rows:
        return "ABSENT", None
    partial = len(rows) == 1
    if len(rows) > 2:
        return "INCONSISTENT", "more bracket rows than frozen ids"
    opposite = {"BUY": "SELL", "SELL": "BUY"}.get(action, "")
    for row in rows.values():
        if (_account(row) != account or _text(row.get("symbol")) != symbol
                or _text(row.get("action")).upper() != opposite):
            return "INCONSISTENT", "bracket child account/symbol/action mismatch"
        if _text(row.get("status")).upper() not in broker_status.BROKER_ACTIVE_STATES:
            return "INCONSISTENT", "bracket child is not active"
    if partial:
        # R102: 子が 1 本だけ live(例: SL が市場の逆側で置けず TP だけ残った 2026-09-15)。
        # 親は約定済み(子が live なのは約定後)なので所有は落とさず、保護が欠けていることを
        # PARTIAL で返す。裸の修復は engine(_guard_naked_position)が同じ周期で行う。
        (only_row,) = rows.values()
        if receipts and _text(only_row.get("receipt")) not in receipts:
            return "INCONSISTENT", "bracket receipt mismatch"
        parent = _text(frozen_row.get("orderId"))
        named = _text(only_row.get("brokerParentId"))
        if named and named != parent:
            return "INCONSISTENT", "bracket child names another parent"
        return "PARTIAL", "only one bracket child remains"
    if receipts and sorted(receipts) != sorted(_text(row.get("receipt")) for row in rows.values()):
        return "INCONSISTENT", "bracket receipt mismatch"
    first, second = rows[ids[0]], rows[ids[1]]
    link_a, link_b = _text(first.get("parentId")), _text(second.get("parentId"))
    mutual = link_a == ids[1] and link_b == ids[0]
    shared = bool(link_a) and link_a == link_b and link_a not in ids
    if not (mutual or shared):
        return "INCONSISTENT", "bracket children are not linked"
    parent = _text(frozen_row.get("orderId"))
    named = {_text(row.get("brokerParentId")) for row in rows.values() if _text(row.get("brokerParentId"))}
    if named and named != {parent}:
        return "INCONSISTENT", "bracket children name another parent"
    return "PRESENT", None


#: R84: トランシェ台帳(tranche.py)が使う公開名。判定器は 1 か所のままにする ——
#: 対の生死の定義をコピーすると、R76/R80 の修正がどちらか片方にしか効かなくなる。
bracket_structure = _bracket_structure


def _tick(value: Optional[float]) -> bool:
    return value is not None and abs(round(value / execution_contract.TICK)
                                     * execution_contract.TICK - value) < 1e-8


def _generation_wrapper(value: Any, raw_identity: str) -> Optional[str]:
    text = _text(value)
    match = re.fullmatch(r"PG:([1-9][0-9]*):(POS:[a-f0-9]{64})", text)
    return text if match and match.group(2) == raw_identity else None


#: R84: 同上。世代ラッパの検証も tranche.py から同じ実装を呼ぶ。
generation_wrapper = _generation_wrapper


def _same_strong_position(previous: Dict[str, Any], current: Dict[str, Any],
                          raw_identity: str, generation: str) -> bool:
    if (_text(previous.get("rawIdentity")) != raw_identity
            or _text(previous.get("generation")) != generation):
        return False
    return all(_text(previous.get(field)) and
               _text(previous.get(field)) == _text(current.get(field))
               for field in ("orderId", "receipt", "filledAt"))


def _reason(reason: str, *, accepted: Optional[list] = None) -> Dict[str, Any]:
    return {"owned": False, "state": "UNOWNED", "reason": reason,
            "acceptedRows": accepted or []}


def bind(plan: Dict[str, Any], route_snapshot: Iterable[Dict[str, Any]],
         broker_position: Dict[str, Any], broker_orders: Dict[str, Any],
         *, route_state: str, position_generation: Optional[str]) -> Dict[str, Any]:
    """Bind RESTING/qty1/qty2 from one exact frozen truth set.

    UNKNOWN with no accepted route leg can never create ownership.  Duplicate
    broker rows, identity collisions, unknown statuses, or economics copied
    only from the request all fail closed.
    """
    if not isinstance(plan, dict) or not isinstance(broker_position, dict):
        return _reason("plan/position is unavailable")
    if broker_position.get("verified") is not True:
        return _reason("broker position is UNVERIFIED")
    if not isinstance(broker_orders, dict) or broker_orders.get("verified") is not True:
        return _reason("broker orders are UNVERIFIED")
    scope = [_text(value) for value in plan.get("accountScope") or [] if _text(value)]
    if not scope or len(scope) != len(set(scope)):
        return _reason("explicit unique account scope is required")
    observed_account = _account(broker_position)
    if observed_account not in set(scope):
        return _reason("broker position account is outside frozen scope")
    account = observed_account
    symbol = _text(plan.get("symbol"))
    action = _text(plan.get("side")).upper()
    order_type = _text(plan.get("entryOrderType") or plan.get("orderType") or "LIMIT").upper()
    entry = _price(plan.get("entryReference") if plan.get("entryReference") is not None
                   else plan.get("entry"))
    if not symbol or action not in {"BUY", "SELL"} or order_type not in {"LIMIT", "MARKET"} or entry is None:
        return _reason("frozen entry economics are incomplete")
    # R75: 指値の有利側乖離の上限 = 凍結プランの SL 幅(無ければ契約の既定に倒れる)。
    stop_price = _price(plan.get("initialStop"))
    favorable_limit = abs(entry - stop_price) if stop_price is not None else None

    # 脚ごとの枚数は凍結プランが正本。2枚固定時代は各脚1枚だったが、ULTRA は
    # 比率分割なので TP1/RUNNER で枚数が違う(奇数なら runner が1枚多い)。
    # プランに脚が無い旧形式は従来どおり各脚1枚として扱う。
    leg_qty = {}
    for row in plan.get("legs") or []:
        if not isinstance(row, dict):
            continue
        leg_id = _text(row.get("id")).upper()
        try:
            value = int(row.get("qty"))
        except (TypeError, ValueError):
            continue
        if leg_id in LEGS and value > 0:
            leg_qty[leg_id] = value
    if not leg_qty:
        leg_qty = {leg: 1 for leg in LEGS}
    if set(leg_qty) != set(LEGS):
        return _reason("frozen split legs are incomplete")

    frozen_all = [dict(row) for row in route_snapshot or [] if isinstance(row, dict)]
    frozen = [row for row in frozen_all if _text(row.get("accountId")) == account]
    accepted = [row for row in frozen if _text(row.get("state")).upper() == "ACCEPTED"]
    if _text(route_state).upper() == "UNKNOWN" and not accepted:
        return _reason("UNKNOWN accepted=0 has no ownership")
    if not accepted:
        return _reason("route has no accepted broker identity")
    keys = [(_text(row.get("accountId")), _text(row.get("legId")).upper()) for row in accepted]
    identities = [(_text(row.get("orderId")), _text(row.get("receipt"))) for row in accepted]
    if (any(acc != account or leg not in LEGS for acc, leg in keys)
            or len(keys) != len(set(keys))
            or any(not oid or not receipt for oid, receipt in identities)
            or len(identities) != len(set(identities))):
        return _reason("accepted route identities are invalid", accepted=accepted)

    all_orders = broker_orders.get("orders")
    if not isinstance(all_orders, list):
        return _reason("broker order rows are unavailable", accepted=accepted)
    matched = []
    bracket_states = []
    for frozen_row in accepted:
        order_id = _text(frozen_row.get("orderId"))
        receipt = _text(frozen_row.get("receipt"))
        candidates = [row for row in all_orders if isinstance(row, dict)
                      and _text(row.get("orderId")) == order_id]
        if not candidates and frozen_row.get("bracketOrderIds"):
            # R76: 親行が一覧にも per-order 詳細にも無い(成行の親は working 一覧に出ず、
            # 消えた id は解決できない)。送信時に凍結した子 2 行の **構造** で照合する。
            # 子が live なら親は約定済み(Tradovate の子は親の約定まで SUSPENDED)。子が
            # 2 本とも無いのは決済/OCO 取消で消費済み(TP1 後の TP1 脚)であって矛盾では
            # ない —— ただし全脚がそれなら(下で)所有しない。
            structure, structure_detail = _bracket_structure(
                frozen_row, all_orders, account=account, symbol=symbol, action=action)
            if structure == "INCONSISTENT":
                return _reason(f"frozen bracket structure is inconsistent ({structure_detail})",
                               accepted=accepted)
            leg_id = _text(frozen_row.get("legId")).upper()
            expected_qty = leg_qty.get(leg_id)
            if expected_qty is None:
                return _reason("frozen bracket leg is absent from the split plan", accepted=accepted)
            if order_type == "LIMIT":
                actual_entry = entry
            else:
                actual_entry = _price(broker_position.get("avgEntry"))
                if actual_entry is None or actual_entry <= 0:
                    return _reason("MARKET fill price unavailable for bracket-verified leg",
                                   accepted=accepted)
                if (not ledger_backed_ownership()
                        and not _market_fill_within_reference(actual_entry, entry, action,
                                                              favorable_limit)):
                    return _reason("MARKET avg entry deviates from frozen reference",
                                   accepted=accepted)
            bracket_states.append(structure)
            matched.append({"orderId": order_id, "receipt": receipt, "accountId": account,
                            "symbol": symbol, "action": action, "status": "FILLED",
                            "legId": leg_id, "actualEntry": actual_entry, "qty": expected_qty,
                            "fieldsIncomplete": True, "bracketStructure": structure})
            continue
        if len(candidates) != 1:
            return _reason("broker order identity is missing/duplicated", accepted=accepted)
        row = candidates[0]
        status = _text(row.get("status")).upper()
        try:
            qty = int(row.get("qty"))
        except (TypeError, ValueError):
            qty = 0
        if (status not in broker_status.BROKER_ACTIVE_STATES
                and status not in broker_status.BROKER_TERMINAL_STATES):
            return _reason("broker order status is not allowlisted", accepted=accepted)
        expected_qty = leg_qty.get(_text(frozen_row.get("legId")).upper())
        if _fields_incomplete(row):
            # R52: 数量・種別・価格が無い行(CrossTrade/Tradovate 実測)。身元は厳格に、
            # 経済条件は凍結プランを採る。指値はその価格より不利には約定しないので
            # LIMIT の建値はプランの値そのもの。MARKET は建玉の avgEntry を約定価格に
            # 使い、取れなければ所有しない。2026-09-05 00:40 の ULTRA 8枚(4/4)は経路が
            # ACCEPTED なのにここで mismatch になり、所有権 UNKNOWN のまま置かれた。
            if (_account(row) != account or _text(row.get("symbol")) != symbol
                    or _text(row.get("action")).upper() != action
                    or _text(row.get("receipt")) != receipt
                    or expected_qty is None):
                return _reason("broker account/symbol/action/receipt mismatch (fields incomplete)",
                               accepted=accepted)
            if order_type == "LIMIT":
                actual_entry = entry
            else:
                actual_entry = (_price(broker_position.get("avgEntry"))
                                if status == "FILLED" else None)
                if status == "FILLED" and (actual_entry is None or actual_entry <= 0):
                    return _reason("MARKET fill price unavailable for incomplete broker row",
                                   accepted=accepted)
                # R52: avgEntry は加重平均で tick に乗らない(04:51 実測 29570.375)ので
                # tick 整合は要求しない。代わりに項目が揃う行と同じ乖離ゲート
                # (凍結参照価格 ± maxDeviationPoints)を成行にも掛ける。
                # R76: 有利側だけは SL 幅まで(R75 と同じ理由)。
                if (status == "FILLED" and entry is not None
                        and not ledger_backed_ownership()
                        and not _market_fill_within_reference(actual_entry, entry, action,
                                                              favorable_limit)):
                    return _reason("MARKET avg entry deviates from frozen reference",
                                   accepted=accepted)
            matched.append({**row, "legId": _text(frozen_row.get("legId")).upper(),
                            "actualEntry": actual_entry, "qty": expected_qty,
                            "fieldsIncomplete": True})
            continue
        actual_entry = _entry_price(row, order_type)
        if (_account(row) != account or _text(row.get("symbol")) != symbol
                or _text(row.get("action")).upper() != action
                or expected_qty is None or qty != expected_qty
                or _text(row.get("orderType")).upper() != order_type
                or _text(row.get("receipt")) != receipt
                or actual_entry is None
                or (order_type == "LIMIT" and actual_entry != entry)
                or (order_type == "MARKET" and (not _tick(actual_entry)
                    or (not ledger_backed_ownership()
                        and abs(actual_entry - entry) > float(
                            execution_contract.CONTRACT["marketOrder"]["maxDeviationPoints"]))))):
            return _reason("broker account/symbol/action/qty/type/entry/receipt mismatch",
                           accepted=accepted)
        matched.append({**row, "legId": _text(frozen_row.get("legId")).upper(),
                        "actualEntry": actual_entry})

    if (bracket_states and len(bracket_states) == len(matched)
            and not any(state in ("PRESENT", "PARTIAL") for state in bracket_states)):
        # R76: 全脚が構造照合で、しかも子が 1 本も生きていない = ブローカー上にこの建玉と
        # 凍結経路を結ぶものが何も無い。建玉の枚数・方向・平均建値だけでは所有しない。
        return _reason("bracket-verified legs have no live structure on the broker",
                       accepted=accepted)
    accepted_receipts = {_text(row.get("receipt")) for row in accepted}
    bracket_receipts = {_text(row.get("receipt")) for row in matched if row.get("bracketStructure")}
    for receipt in accepted_receipts:
        count = sum(1 for candidate in all_orders if isinstance(candidate, dict)
                    and _text(candidate.get("receipt")) == receipt)
        # R76: 構造照合した脚の親行は一覧に無い(0 本)のが正常。
        if count != (0 if receipt in bracket_receipts else 1):
            return _reason("broker receipt collision", accepted=accepted)
    matched_ids = {_text(row.get("orderId")) for row in matched}
    # Historical terminal rows unrelated to the frozen IDs are harmless.  A
    # simultaneous active row with identical entry economics is ambiguous.
    for row in all_orders:
        if not isinstance(row, dict) or _text(row.get("orderId")) in matched_ids:
            continue
        if (_text(row.get("status")).upper() in broker_status.BROKER_ACTIVE_STATES
                and _account(row) == account and _text(row.get("symbol")) == symbol
                and _text(row.get("action")).upper() == action):
            if _fields_incomplete(row):
                # R52: 価格で区別できないので、同方向の **親行**(parentId 無し)が
                # 他にあれば曖昧として所有しない。ブラケットの子は逆方向。
                if not _text(row.get("parentId")):
                    return _reason("active colliding broker entry row", accepted=accepted)
            elif (_text(row.get("orderType")).upper() == order_type
                    and (order_type == "MARKET" or _entry_price(row, order_type) == entry)):
                return _reason("active colliding broker entry row", accepted=accepted)

    try:
        position_qty = int(broker_position.get("qty") or 0)
    except (TypeError, ValueError):
        return _reason("broker position qty is invalid", accepted=accepted)
    # 分割ライフサイクル上ありうる建玉枚数だけを認める。2枚固定なら {0,1,2}、
    # ULTRA の比率分割なら {0, TP1脚, RUNNER脚, 合計}。それ以外の枚数は
    # この経路が作ったものではないので所有しない。
    total_qty = leg_qty[LEGS[0]] + leg_qty[LEGS[1]]
    lifecycle_quantities = {0, leg_qty[LEGS[0]], leg_qty[LEGS[1]], total_qty}
    if position_qty not in lifecycle_quantities:
        return _reason("broker position qty is outside split lifecycle", accepted=accepted)
    if (_account(broker_position) != account
            or _text(broker_position.get("symbol")) != symbol):
        return _reason("aggregate position account/symbol mismatch", accepted=accepted)
    active = [row for row in matched if row["status"] in broker_status.BROKER_ACTIVE_STATES]
    filled = [row for row in matched if row["status"] == "FILLED"]

    if position_qty == 0:
        if len(active) != len(matched):
            return _reason("flat position does not have every accepted leg active", accepted=accepted)
        pending = {"accountScope": [account], "symbol": symbol, "side": action,
                   "orderIds": sorted(row["orderId"] for row in matched),
                   "receipts": sorted(row["receipt"] for row in matched),
                   "legs": [{"legId": row["legId"], "orderId": row["orderId"],
                              "receipt": row["receipt"], "status": row["status"]}
                             for row in matched]}
        owned_plan = {**plan, "routeSnapshot": frozen, "pendingOrderOwnership": pending}
        if len(scope) > 1:
            owned_plan = {**owned_plan, "accountScope": [account], "fullPlan": dict(plan)}
        return {"owned": True, "state": "RESTING", "reason": None,
                "pendingOrderOwnership": pending, "acceptedRows": accepted,
                "matchedOrders": matched, "ownedPlan": owned_plan}

    raw_identity = broker_status.position_identity(broker_position)
    generation = _generation_wrapper(position_generation, raw_identity or "")
    if not raw_identity or not generation:
        return _reason("broker-derived raw identity/generation wrapper is unavailable",
                       accepted=accepted)
    if _position_side(broker_position) != action:
        return _reason("aggregate position account/symbol/side mismatch", accepted=accepted)

    actual_fill = (sum(float(row["actualEntry"]) for row in filled) / len(filled)
                   if filled else None)
    if order_type == "MARKET" and filled:
        stop = _price(plan.get("initialStop") if plan.get("initialStop") is not None
                      else plan.get("stop"))
        cap = _price((plan.get("executionContract") or {}).get("riskCapDollars"))
        # 上限の天井はモードで変わる。通常経路は $240 のまま、ULTRA は
        # ULTRA エンベロープの口座別上限。凍結された cap があればそれより
        # 緩くはしない(下流は厳しくなる方向にしか動けない)。
        ceiling = (float(execution_contract.CONTRACT["ultra"]["maxRiskDollarsPerAccount"])
                   if total_qty > int(execution_contract.CONTRACT["risk"]["fixedQty"])
                   else float(execution_contract.CONTRACT["risk"]["defaultCapDollars"]))
        cap = min(cap if cap is not None else float("inf"), ceiling)
        # 実約定リスクは脚の枚数ぶん膨らむ。各脚1枚だった頃は qty を掛けなくても
        # 一致したが、ULTRA の分割では掛け忘れると桁で過小評価になる。
        actual_risk = sum(abs(float(row["actualEntry"]) - float(stop))
                          * execution_contract.CONTRACT["risk"]["pointValue"]
                          * max(1, int(row.get("qty") or 1))
                          for row in filled) if stop is not None else float("inf")
        if actual_risk > cap + 1e-9:
            return _reason("actual MARKET fill exceeds risk cap", accepted=accepted)
    ownership = {"generation": generation, "rawIdentity": raw_identity,
                 "accountId": account,
                 "symbol": symbol, "side": action,
                 "orderId": broker_position.get("orderId"),
                 "receipt": broker_position.get("receipt"),
                 "filledAt": broker_position.get("filledAt"),
                 "avgEntry": broker_position.get("avgEntry"),
                 "initialQty": broker_position.get("initialQty") or position_qty,
                 "actualFillPrice": actual_fill}
    if position_qty != total_qty:
        prior_ownership = plan.get("positionOwnership") if isinstance(
            plan.get("positionOwnership"), dict) else {}
        continued_owned_runner = (
            len(matched) == 2 and len(filled) == 2 and not active
            and _same_strong_position(prior_ownership, broker_position,
                                      raw_identity, generation)
        )
        # R52: 建玉行が注文との対応(receipt)を持たないブローカーでは
        # `_same_strong_position` が原理的に成立しない(receipt が null で、TP1 後は
        # identity も変わる)。両エントリー脚が FILLED で未終端の脚が無く、建玉が
        # RUNNER 脚の枚数ちょうどで、平均建値が凍結プランと整合するなら、TP1 の
        # ブラケットで TP1 脚が決済された後の runner 継続と認める。TP1 脚は目標が
        # 近いので先に外れる —— 残るのは RUNNER 脚以外にない。
        # 2026-09-05 02:36、SHORT 12 → TP1 で 6 決済 → runner 6 がここで保留になった。
        continued_runner_incomplete = (
            _position_lacks_order_link(broker_position)
            and len(matched) == 2 and len(filled) == 2 and not active
            and position_qty == leg_qty.get(LEGS[1])
            and _avg_entry_consistent(broker_position, order_type, entry, filled,
                                      favorable_limit=favorable_limit)
        )
        continued_owned_runner = continued_owned_runner or continued_runner_incomplete
        limited_partial = (_text(route_state).upper() == "PARTIAL"
                           and len(matched) == 1 and len(filled) == 1 and not active)
        if not ((len(matched) == 2 and len(filled) == 1 and len(active) == 1)
                or continued_owned_runner or limited_partial):
            return _reason("partial requires one exact filled and remaining active leg",
                           accepted=accepted)
        direct_matches = [row for row in filled
                          if _text(broker_position.get("orderId")) == _text(row.get("orderId"))
                          and _text(broker_position.get("receipt")) == _text(row.get("receipt"))]
        if len(direct_matches) != 1:
            direct_by_order = [row for row in filled
                               if _text(broker_position.get("orderId"))
                               and _text(broker_position.get("orderId")) == _text(row.get("orderId"))]
            # R52: 建玉行が注文との対応を持たないブローカー。約定した脚が 1 本だけなら、
            # その脚が建玉の出所であることはブローカーの注文状態(FILLED)自体が示す。
            # 平均建値の整合を追加で要求する。
            if (ledger_backed_ownership() and _position_lacks_order_link(broker_position)
                    and len(direct_by_order) == 1):
                # R102/R79: 建玉の orderId が台帳の脚と一致 = 自前の送信が出所。receipt(CrossTrade は
                # 返さない)と平均建値の整合(通過指値は建値が大きく乖離する)は要求しない。
                direct_matches = direct_by_order
            elif (_position_lacks_order_link(broker_position) and len(filled) == 1
                    and _avg_entry_consistent(broker_position, order_type, entry, filled,
                                      favorable_limit=favorable_limit)):
                direct_matches = [filled[0]]
            elif continued_runner_incomplete:
                direct_matches = [row for row in filled if row["legId"] == LEGS[1]]
            else:
                return _reason("partial aggregate fill identity does not match exactly one leg",
                               accepted=accepted)
        leg = direct_matches[0]
        # 建玉枚数は約定した脚の枚数と一致していなければならない。ULTRA では
        # 脚が 15/15 のように大きいので、ここが合わないまま所有すると
        # 「どちらの脚が入ったのか」を取り違えたまま管理してしまう。
        try:
            filled_leg_qty = int(leg.get("qty") or 0)
        except (TypeError, ValueError):
            filled_leg_qty = 0
        if filled_leg_qty != position_qty:
            return _reason("partial position qty does not match the filled leg qty",
                           accepted=accepted)
        plan_legs = [dict(row) for row in plan.get("legs") or []
                     if _text(row.get("id")).upper() == leg["legId"]]
        if len(plan_legs) != 1:
            return _reason("filled leg is absent from the frozen plan", accepted=accepted)
        target = _price(plan_legs[0].get("target"))
        partial = {**plan, "legs": plan_legs, "targets": [target],
                   "finalTarget": target, "partialLegId": leg["legId"],
                   "legIdentityKnown": True, "routeSnapshot": frozen,
                   "positionOwnership": ownership, "fullPlan": dict(plan)}
        if len(scope) > 1:
            partial["accountScope"] = [account]
        if order_type == "MARKET":
            partial["plannedEntry"] = entry
            partial["entry"] = actual_fill
            partial["actualFillPrice"] = actual_fill
        if active:
            remaining = active[0]
            partial["remainingRestingLeg"] = {key: remaining.get(key) for key in
                                               ("accountId", "legId", "orderId", "receipt", "status")}
        return {"owned": True, "state": "LIMITED_PARTIAL" if limited_partial else "PARTIAL_FILL",
                "reason": None,
                "positionOwnership": ownership, "acceptedRows": accepted,
                "matchedOrders": matched, "ownedPlan": partial}

    if len(accepted) != 2 or {row["legId"] for row in matched} != set(LEGS) or len(filled) != 2:
        return _reason("qty2 requires exact filled TP1+RUNNER", accepted=accepted)
    direct_full = [row for row in filled
                   if _text(broker_position.get("orderId")) == _text(row.get("orderId"))
                   and _text(broker_position.get("receipt")) == _text(row.get("receipt"))]
    continued_full = (isinstance(plan.get("positionOwnership"), dict)
                      and _same_strong_position(plan["positionOwnership"], broker_position,
                                                raw_identity, generation))
    direct_by_order = [row for row in filled
                       if _text(broker_position.get("orderId"))
                       and _text(broker_position.get("orderId")) == _text(row.get("orderId"))]
    if len(direct_full) != 1 and not continued_full:
        # R52: 同上。両脚 FILLED(注文 ID は束縛済み)+ 枚数一致(上で検証)+ 方向一致
        # (上で検証)+ 平均建値の整合 + 強い建玉 identity(上で検証)で所有する。
        # 2026-09-05 02:09、SHORT 12 @29,556.75(両脚 FILLED)がここで保留になった。
        # R102/R79: 建玉の orderId が台帳の脚と一致すれば、receipt と平均建値の整合は要求しない
        # (2026-09-15 15:11、285pt の通過指値で建値が乖離し UNKNOWN に落ちて裸のまま沈黙した)。
        if (ledger_backed_ownership() and _position_lacks_order_link(broker_position)
                and len(direct_by_order) == 1):
            pass                                  # receipt を返さないブローカーだけ(NT8 は従来どおり厳格)
        elif not (_position_lacks_order_link(broker_position) and len(filled) == 2
                  and _avg_entry_consistent(broker_position, order_type, entry, filled,
                                        favorable_limit=favorable_limit)):
            return _reason("qty2 aggregate position is not bound to a filled split leg",
                           accepted=accepted)
    owned_plan = {**plan, "routeSnapshot": frozen, "positionOwnership": ownership}
    if len(scope) > 1:
        owned_plan = {**owned_plan, "accountScope": [account], "fullPlan": dict(plan)}
    if order_type == "MARKET":
        owned_plan.update({"plannedEntry": entry, "entry": actual_fill,
                           "actualFillPrice": actual_fill})
    return {"owned": True, "state": "OWNED_FULL", "reason": None,
            "positionOwnership": ownership, "acceptedRows": accepted,
            "matchedOrders": matched, "ownedPlan": owned_plan}

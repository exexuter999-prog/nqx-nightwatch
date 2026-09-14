#!/usr/bin/env python3
"""R23 broker-truth route identity acquisition.

CrossTrade's webhook response body is only ``{"success": true}``.  It carries
no order id and no receipt, while R19..R22 require both before a leg may be
called ACCEPTED.  Without this module every live route resolves UNKNOWN, the
Durable Object entry claim locks permanently, and ownership can never bind.

The identity is therefore recovered from broker truth instead of from the
request echo: the caller snapshots the account's order view immediately before
and after **one single attempt**, and exactly one *new* row matching that
attempt's exact economics is the identity of that attempt.

Every ambiguity returns ``None`` so the caller stays UNKNOWN:

* either snapshot unverified, or any row without an order id
* any new row whose status is outside the broker allowlist
* zero or more than one candidate matching the frozen economics
* a replacement bracket whose two legs do not share one broker OCO parent

Nothing here infers a value from the request.  Prices, quantities, actions and
statuses are only ever *compared* against what the broker returned.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import broker_status


def _rows(view: Any) -> Optional[List[Dict[str, Any]]]:
    if not isinstance(view, dict) or view.get("verified") is not True:
        return None
    rows = view.get("orders")
    return rows if isinstance(rows, list) else None


def new_rows(before_view: Any, after_view: Any) -> Optional[List[Dict[str, Any]]]:
    """Rows present after the attempt that were absent before it."""
    before, after = _rows(before_view), _rows(after_view)
    if before is None or after is None:
        return None
    known = set()
    for row in before:
        if not isinstance(row, dict):
            return None
        order_id = str(row.get("orderId") or "").strip()
        if not order_id:
            return None
        known.add(order_id)
    fresh = []
    for row in after:
        if not isinstance(row, dict):
            return None
        order_id = str(row.get("orderId") or "").strip()
        if not order_id:
            return None
        if order_id not in known:
            fresh.append(row)
    return fresh


def _allowlisted(row: Dict[str, Any]) -> bool:
    status = str(row.get("status") or "").upper()
    return (status in broker_status.BROKER_ACTIVE_STATES
            or status in broker_status.BROKER_TERMINAL_STATES)


def _same_price(value: Any, expected: Any) -> bool:
    try:
        return abs(float(value) - float(expected)) < 1e-9
    except (TypeError, ValueError):
        return False


def _account(row: Dict[str, Any]) -> str:
    return str(row.get("accountId") or row.get("account") or "")


def _scoped(row: Dict[str, Any], account: Any, symbol: Any, action: Any) -> bool:
    return (_account(row) == str(account)
            and str(row.get("symbol") or "") == str(symbol)
            and str(row.get("action") or "").upper() == str(action).upper())


def _qty_matches(row: Dict[str, Any], qty: Any) -> bool:
    try:
        return int(row.get("qty") or 0) == int(qty)
    except (TypeError, ValueError):
        return False


def _fields_incomplete(row: Dict[str, Any]) -> bool:
    """数量・種別・価格を **ブローカーが返していない** 行か(R52)。

    CrossTrade の Tradovate 経路は一覧でも per-order 詳細でも `orderQty` /
    `orderType` / `price` を返さない(broker_status R41 と 2026-09-04 の再実測)。
    `fieldsComplete` は broker_status が正規化時に付ける印。付いていない古い形でも
    3 項目すべて None なら同じ扱いにする。
    """
    if row.get("fieldsComplete") is False:
        return True
    return (row.get("qty") is None and row.get("orderType") in (None, "")
            and row.get("limitPrice") is None)


def bind_entry_leg(before_view: Any, after_view: Any, *, account: Any, symbol: Any,
                   action: Any, qty: Any, order_type: Any,
                   entry_price: Any) -> Optional[Dict[str, Any]]:
    """Bind one posted entry leg to the single broker row it created.

    TP1 and RUNNER carry identical entry economics, so the two legs can only be
    told apart by *when* their rows appeared.  The caller must therefore take a
    fresh snapshot per leg; a window containing two matching parents is
    ambiguous and rejected.
    """
    fresh = new_rows(before_view, after_view)
    if not fresh:
        return None
    wanted_type = str(order_type or "").upper()
    if wanted_type not in {"LIMIT", "MARKET"}:
        return None
    candidates = []
    for row in fresh:
        if not _allowlisted(row):
            return None
        if not _scoped(row, account, symbol, action):
            continue
        if _fields_incomplete(row):
            # R52: 数量・種別・価格で照合すると **原理的に一致しない**(ブローカーが
            # 返さない)。2026-09-04 までの live ENTRY 11 件は全部この理由で
            # UNKNOWN → HALT になり、台帳に ENTRY_ACCEPTED が 1 件も無かった。
            # 身元(口座・銘柄・方向)+ 親行であること + この脚の窓に現れた唯一の
            # 候補であること、で束縛する。ブラケットの子(SL/TP)は逆方向で
            # かつ parentId を持つので、どちらの条件でも外れる。
            # 状態は `_allowlisted`(active または terminal)で足りる —— 成行の親行は
            # 窓の中で既に FILLED になっている(2026-09-05 04:27 実測: 28 枚成行が
            # FILLED なのに束縛できず UNKNOWN → HALT)。
            if str(row.get("parentId") or "").strip():
                continue
            candidates.append(row)
            continue
        if not _qty_matches(row, qty):
            continue
        if str(row.get("orderType") or "").upper() != wanted_type:
            continue
        if wanted_type == "LIMIT" and not _same_price(row.get("limitPrice"), entry_price):
            continue
        candidates.append(row)
    if len(candidates) != 1:
        return None
    row = candidates[0]
    order_id = str(row.get("orderId") or "").strip()
    receipt = str(row.get("receipt") or "").strip()
    if not order_id or not receipt:
        return None
    status = str(row.get("status") or "").upper()
    return {"orderId": order_id, "receipt": receipt, "status": status,
            "filledAt": row.get("timestamp") if status == "FILLED" else None,
            "identitySource": "broker-orders"}


def bind_replacement_bracket(before_view: Any, after_view: Any, *, account: Any,
                             symbol: Any, action: Any, qty: Any, stop: Any,
                             target: Any, platform: Any = None) -> Optional[Dict[str, Any]]:
    """Bind one MODIFY attempt to the replacement STOP/LIMIT pair it created.

    Requiring the pair to be *new* is stronger than the response-body route it
    replaces: an unchanged bracket that merely survived the request can no
    longer be mistaken for a successful replacement.
    """
    fresh = new_rows(before_view, after_view)
    if not fresh:
        return None
    if any(not _allowlisted(row) for row in fresh):
        return None
    if any(_fields_incomplete(row) for row in fresh):
        # R52: 種別・価格が無い(CrossTrade/Tradovate)ので STOP と LIMIT を区別できない。
        # R80: 同口座・逆方向・live な **新規 2 行**が、生 `ocoId` で **相互に** 結ばれ、
        # どちらにも `parentId`(OSO の親)が無く、子ブラケットも付いていないときだけ、
        # cancelandbracket が建玉の周りに張り直した OCO 兄弟とみなす
        # (broker_status.oco_sibling_pair)。`parentId` に潰したリンクや SUSPENDED の行は
        # 認めない(2026-09-12 01:53 の疑義の再発防止)。stop/target の割り当ては
        # orderId 順で固定する(意味は無いが決定的)。
        pair, _reason = broker_status.oco_sibling_pair(
            fresh, account=account, expected_action=action, symbol=symbol,
            all_rows=_rows(after_view))
        if pair is None:
            return None
        first, second = pair
        id_a, id_b = str(first.get("orderId") or ""), str(second.get("orderId") or "")
        receipt_a, receipt_b = str(first.get("receipt") or ""), str(second.get("receipt") or "")
        if not receipt_a or not receipt_b or receipt_a == receipt_b:
            return None
        group = f"{id_a}+{id_b}"
        resolved_platform = platform or (after_view or {}).get("platform")
        return {"accountId": str(account), "stopOrderId": id_a, "targetOrderId": id_b,
                "ocoGroupId": group,
                "receipt": broker_status.derived_receipt(resolved_platform, account, group),
                "stopReceipt": receipt_a, "targetReceipt": receipt_b,
                "receiptSource": "broker-orders", "fieldsIncomplete": True,
                "structure": "OCO_SIBLINGS", "pricesConfirmed": False}
    stops, targets = [], []
    for row in fresh:
        if not _scoped(row, account, symbol, action) or not _qty_matches(row, qty):
            continue
        if str(row.get("status") or "").upper() not in broker_status.BROKER_ACTIVE_STATES:
            continue
        order_type = str(row.get("orderType") or "").upper()
        if "STOP" in order_type and _same_price(row.get("stopPrice"), stop):
            stops.append(row)
        elif "LIMIT" in order_type and _same_price(row.get("limitPrice"), target):
            targets.append(row)
    if len(stops) != 1 or len(targets) != 1:
        return None
    stop_row, target_row = stops[0], targets[0]
    parent = str(stop_row.get("parentId") or "")
    if not parent or parent != str(target_row.get("parentId") or ""):
        return None
    stop_id = str(stop_row.get("orderId") or "")
    target_id = str(target_row.get("orderId") or "")
    stop_receipt = str(stop_row.get("receipt") or "")
    target_receipt = str(target_row.get("receipt") or "")
    if (not stop_id or not target_id or stop_id == target_id
            or not stop_receipt or not target_receipt
            or stop_receipt == target_receipt):
        return None
    resolved_platform = platform or (after_view or {}).get("platform")
    return {"accountId": str(account), "stopOrderId": stop_id,
            "targetOrderId": target_id, "ocoGroupId": parent,
            "receipt": broker_status.derived_receipt(resolved_platform, account, parent),
            "stopReceipt": stop_receipt, "targetReceipt": target_receipt,
            "receiptSource": "broker-orders"}


# ---------------------------------------------------------------- R72: 約定履歴で束縛

def fill_rows(view: Any) -> Optional[List[Dict[str, Any]]]:
    """``broker_status.query_fills`` の view から約定行を取り出す(未検証なら None)。"""
    if not isinstance(view, dict) or view.get("verified") is not True:
        return None
    rows = view.get("fills")
    return rows if isinstance(rows, list) else None


def new_fills(before_view: Any, after_view: Any) -> Optional[List[Dict[str, Any]]]:
    """送信後に現れた約定(送信前の窓に無かった fill id)。比較できなければ None。"""
    before, after = fill_rows(before_view), fill_rows(after_view)
    if before is None or after is None:
        return None
    known = set()
    for row in before:
        if not isinstance(row, dict):
            return None
        fill_id = str(row.get("id") or "").strip()
        if not fill_id:
            return None
        known.add(fill_id)
    fresh = []
    for row in after:
        if not isinstance(row, dict):
            return None
        fill_id = str(row.get("id") or "").strip()
        if not fill_id:
            return None
        if fill_id not in known:
            fresh.append(row)
    return fresh


def bind_entry_leg_from_fills(before_view: Any, after_view: Any, *, account: Any, symbol: Any,
                              action: Any, qty: Any, platform: Any = None,
                              exclude_order_ids: Any = None) -> Optional[Dict[str, Any]]:
    """R72: 約定履歴(``GET /accounts/{name}/fills``)から ENTRY 脚の identity を束縛する。

    なぜ要るか: 成行 ENTRY は POST 直後に「親 PendingNew → Filled、子 Suspended →
    Working」と状態が連続で変わる。注文一覧の窓(``bind_entry_leg``)は、その一瞬の
    状態が許可リスト外だと照会ごと Unavailable になり、脚の identity が None に確定
    する(2026-09-08 10:34 / 21:16 実測: 成行 2 脚とも UNKNOWN → HALT、建玉は管理外)。
    約定履歴は状態を持たず、``orderId``(= 親 ENTRY 注文の id)・数量・価格・時刻を
    約定ごとに返すので、この経路は状態の許可リストに依存しない。

    束縛の条件は注文窓と同じく **一意性** である:

    * 送信前の窓に無い約定だけを候補にする(fill id で比較)
    * 口座(view の account)・銘柄(instrument)・方向が一致するものだけを数える
    * 同じ ``orderId`` の約定を合算し、合計がこの脚の数量に **ちょうど一致**する
      注文がただ 1 件のときだけ、その ``orderId`` を identity にする
    * ``exclude_order_ids`` は既に別の脚へ束縛済みの注文(同一窓に 2 脚分の約定が
      入ったときの切り分け)。推測ではなく、束縛済みという事実で外す
    * 帰属不明(orderId 無し)の約定が窓にあれば曖昧として None

    価格・数量・時刻は比較と記録にだけ使い、要求側の値で埋めない。
    """
    fresh = new_fills(before_view, after_view)
    if not fresh:
        return None
    try:
        wanted_qty = int(qty)
    except (TypeError, ValueError):
        return None
    if wanted_qty <= 0:
        return None
    wanted_action = str(action or "").upper()
    if wanted_action not in {"BUY", "SELL"}:
        return None
    view_account = str((after_view or {}).get("account") or "").strip()
    if view_account and view_account != str(account):
        return None
    excluded = {str(value) for value in (exclude_order_ids or []) if value not in (None, "")}
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for row in fresh:
        if str(row.get("action") or "").upper() != wanted_action:
            continue
        if str(row.get("instrument") or "") != str(symbol):
            continue
        order_id = str(row.get("orderId") or "").strip()
        if not order_id:
            # 帰属の分からない約定が同じ窓にある。どの脚のものか決められない。
            return None
        if order_id in excluded:
            continue
        grouped.setdefault(order_id, []).append(row)
    candidates = []
    for order_id, rows in grouped.items():
        try:
            total = sum(int(row.get("qty") or 0) for row in rows)
        except (TypeError, ValueError):
            return None
        if total == wanted_qty:
            candidates.append((order_id, rows))
    if len(candidates) != 1:
        return None
    order_id, rows = candidates[0]
    try:
        notional = sum(float(row.get("price")) * int(row.get("qty")) for row in rows)
        fill_price = notional / wanted_qty
    except (TypeError, ValueError):
        return None
    filled_at = max(str(row.get("at") or "") for row in rows) or None
    resolved_platform = platform or (after_view or {}).get("platform")
    receipt = broker_status.derived_receipt(resolved_platform, account, order_id)
    if not receipt:
        return None
    return {"orderId": order_id, "receipt": receipt, "status": "FILLED",
            "filledAt": filled_at, "fillPrice": fill_price,
            "fillIds": [str(row.get("id")) for row in rows],
            "identitySource": "broker-fills"}


def fill_qty_by_parent(before_view: Any, after_view: Any, *, symbol: Any, action: Any,
                       since: Any = None, until: Any = None) -> Dict[str, int]:
    """約定履歴から「親注文 id → 同方向の約定枚数」を集める(R76 の枚数証拠)。

    ``before_view`` が無ければ ``after_view`` の全約定を対象にし、``since`` / ``until``
    (tz 付き datetime)があれば約定時刻でさらに絞る。帰属不明の約定は無視する
    (ここは束縛ではなく枚数の裏取りなので、欠けは「証拠なし」に倒れる)。
    """
    rows = new_fills(before_view, after_view) if before_view is not None else fill_rows(after_view)
    if not rows:
        return {}
    wanted = str(action or "").upper()
    totals: Dict[str, int] = {}
    for row in rows:
        if str(row.get("action") or "").upper() != wanted:
            continue
        if str(row.get("instrument") or "") != str(symbol):
            continue
        order_id = str(row.get("orderId") or "").strip()
        qty = _qty_of(row.get("qty"))
        if not order_id or qty is None:
            continue
        at = _parse_iso(row.get("at"))
        if since is not None and (at is None or at < since):
            continue
        if until is not None and (at is None or at > until):
            continue
        totals[order_id] = totals.get(order_id, 0) + qty
    return totals


# ---------------------------------------------------------------- R76: ブラケット子行で束縛

#: 親が約定した後の子(SL/TP)が取る状態。Tradovate の子は親の約定まで SUSPENDED で、
#: 親が約定すると WORKING へ変わる(2026-09-08 10:49 / 10:55 実測: 指値の親 WORKING に
#: 子 SUSPENDED。同日 10:34 の成行は親が一覧に一度も現れず、子 4 本だけが WORKING)。
#: SUSPENDED の対は「親が未約定で一覧に見えている」状態なので親行の経路
#: (``bind_entry_leg``)に任せ、ここでは約定の証拠に数えない。
#: R80: broker_status.BROKER_LIVE_PROTECTIVE_STATES と同じ集合(SUSPENDED は live ではない)。
BRACKET_LIVE_STATES = set(broker_status.BROKER_LIVE_PROTECTIVE_STATES)
IDENTITY_SOURCE_BRACKETS = "broker-brackets"


def opposite_action(action: Any) -> str:
    return {"BUY": "SELL", "SELL": "BUY"}.get(str(action or "").upper(), "")


def _order_id(row: Dict[str, Any]) -> str:
    return str(row.get("orderId") or "").strip()


def _parent_link(row: Dict[str, Any]) -> str:
    return str(row.get("parentId") or "").strip()


def _broker_parent(row: Dict[str, Any]) -> str:
    return str(row.get("brokerParentId") or "").strip()


def _created_at(row: Dict[str, Any]) -> str:
    return str(row.get("timestamp") or row.get("createdAt") or "").strip()


def _creation_key(order_id: str, created: str = ""):
    """作成順のキー。時刻が同じなら注文 id の数値順(Tradovate の id は単調増加)。"""
    numeric = int(order_id) if order_id.isdigit() else None
    return (created, 0 if numeric is not None else 1,
            numeric if numeric is not None else 0, order_id)


def _qty_of(value: Any) -> Optional[int]:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _parse_iso(value: Any) -> Optional[datetime]:
    raw = str(value or "").strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _pair_record(first: Dict[str, Any], second: Dict[str, Any], *,
                 account: Any) -> Optional[Dict[str, Any]]:
    rows = sorted((first, second), key=lambda row: _creation_key(_order_id(row)))
    ids = [_order_id(row) for row in rows]
    if not ids[0] or not ids[1] or ids[0] == ids[1]:
        return None
    receipts = [str(row.get("receipt") or "").strip() for row in rows]
    if not all(receipts) or receipts[0] == receipts[1]:
        return None
    parents = {_broker_parent(row) for row in rows if _broker_parent(row)}
    if len(parents) > 1:
        # 2 本の子が別々の真の親を名乗る = 対ではない。
        return None
    statuses = [str(row.get("status") or "").upper() for row in rows]
    stamps = [_created_at(row) for row in rows if _created_at(row)]
    return {"orderIds": ids, "receipts": receipts,
            "brokerParentId": next(iter(parents), None),
            "createdAt": min(stamps) if stamps else None,
            "statuses": statuses,
            "live": all(status in BRACKET_LIVE_STATES for status in statuses),
            "accountId": str(account), "rows": rows}


def bracket_pairs(rows: Any, *, account: Any, symbol: Any,
                  entry_action: Any) -> Optional[List[Dict[str, Any]]]:
    """建玉と逆方向の active 行を OCO 対にまとめる(R76)。

    戻り値は作成順(子行の timestamp → 注文 id)に並んだ対のリスト。
    **逆方向の active 行が 1 本でも対にできなければ None**(曖昧): 相方の無い子、
    3 本以上が同じ親を指す、両方の子が別々の真の親を名乗る、receipt が無い/重複、
    許可リスト外の状態。同方向の行(親候補)や終端の行はここでは見ない —— 呼び出し側が
    別に数える。対の形は Tradovate の相互 ``parentId``(= ocoId)と、共通の親 id を
    2 本だけが指す形の両方を認める。
    """
    wanted = opposite_action(entry_action)
    if not wanted:
        return None
    children: List[Dict[str, Any]] = []
    for row in rows or []:
        if not isinstance(row, dict):
            return None
        if not _scoped(row, account, symbol, wanted):
            continue
        status = str(row.get("status") or "").upper()
        if status in broker_status.BROKER_TERMINAL_STATES:
            continue
        if status not in broker_status.BROKER_ACTIVE_STATES or not _order_id(row):
            return None
        children.append(row)
    by_id = {_order_id(row): row for row in children}
    if len(by_id) != len(children):
        return None
    ordered = sorted(children, key=lambda row: _creation_key(_order_id(row), _created_at(row)))
    pairs: List[Dict[str, Any]] = []
    used = set()
    for row in ordered:
        row_id = _order_id(row)
        if row_id in used:
            continue
        link = _parent_link(row)
        if not link or link == row_id:
            return None
        partner = by_id.get(link)
        if partner is not None and _parent_link(partner) == row_id and link not in used:
            pair = _pair_record(row, partner, account=account)
        else:
            group = [item for item in ordered
                     if _parent_link(item) == link and _order_id(item) not in used]
            if link in by_id or len(group) != 2:
                return None
            pair = _pair_record(group[0], group[1], account=account)
        if pair is None:
            return None
        pairs.append(pair)
        used.update(pair["orderIds"])
    return sorted(pairs, key=lambda pair: _creation_key(pair["orderIds"][0],
                                                        pair.get("createdAt") or ""))


def bracket_identity(pair: Dict[str, Any], *, account: Any,
                     platform: Any = None) -> Optional[Dict[str, Any]]:
    """live な OCO 対から ENTRY 脚の identity を作る。真の親 id が無ければ None。

    identity の ``orderId`` は子行が名乗る真の親(= ENTRY 注文)。後周期は
    ``known_order_ids`` でその親を per-order 詳細から読めるので、既存の束縛・終端証明・
    所有ブラケット判定がそのまま効く。子の id と receipt は ``bracketOrderIds`` /
    ``bracketReceipts`` に凍結し、親行が読めない周期は構造で照合する(ownership_binder)。
    """
    parent = str((pair or {}).get("brokerParentId") or "").strip()
    if not parent or not (pair or {}).get("live"):
        return None
    receipt = broker_status.derived_receipt(platform, account, parent)
    if not receipt:
        return None
    return {"orderId": parent, "receipt": receipt, "status": "FILLED",
            "filledAt": pair.get("createdAt"), "identitySource": IDENTITY_SOURCE_BRACKETS,
            "bracketOrderIds": list(pair["orderIds"]),
            "bracketReceipts": list(pair["receipts"])}


def _identity_ids(identity: Any) -> set:
    if not isinstance(identity, dict):
        return set()
    ids = {str(identity.get("orderId") or "")}
    ids.update(str(value) for value in identity.get("bracketOrderIds") or [])
    return {value for value in ids if value}


def new_bracket_pairs(before_view: Any, after_view: Any, *, account: Any, symbol: Any,
                      action: Any) -> Optional[List[Dict[str, Any]]]:
    """送信後に現れた子行を含む OCO 対(作成順)。比較不能・曖昧は None。

    対は ``after`` の逆方向 active 行全体で組む(相方が送信前の窓に既に見えていた
    一瞬の取りこぼしで対が割れないように)。対のうち **少なくとも 1 本が新しい行**
    のものだけを返す。古い対(両方とも送信前から存在)は別の建玉のものなので除く。
    """
    fresh = new_rows(before_view, after_view)
    if fresh is None:
        return None
    if any(not _allowlisted(row) for row in fresh):
        return None
    fresh_ids = {_order_id(row) for row in fresh}
    if not fresh_ids:
        return []
    pairs = bracket_pairs((after_view or {}).get("orders"), account=account, symbol=symbol,
                          entry_action=action)
    if pairs is None:
        return None
    return [pair for pair in pairs if fresh_ids & set(pair["orderIds"])]


def _select_by_qty(candidates: List[Dict[str, Any]], wanted_qty: Optional[int],
                   parent_qty: Any) -> List[Dict[str, Any]]:
    """親 id → 枚数の証拠(約定履歴)が **全候補に** あるときだけ枚数で絞る。無ければそのまま。"""
    if not isinstance(parent_qty, dict) or wanted_qty is None or not candidates:
        return candidates
    if not all(str(pair.get("brokerParentId") or "") in parent_qty for pair in candidates):
        return candidates
    return [pair for pair in candidates
            if _qty_of(parent_qty.get(str(pair.get("brokerParentId") or ""))) == wanted_qty]


def bind_entry_leg_from_brackets(before_view: Any, after_view: Any, *, account: Any, symbol: Any,
                                 action: Any, qty: Any, platform: Any = None,
                                 exclude_order_ids: Any = None, parent_qty: Any = None,
                                 pick_index: Any = None, expected_count: Any = None
                                 ) -> Optional[Dict[str, Any]]:
    """R76: 成行の即時約定を、送信後に現れた **ブラケットの子 2 行** から束縛する。

    なぜ要るか: 成行の親は POST の直後に FILLED になり working 一覧に一度も現れない。
    送信直後の一覧は一過性の状態で Unavailable にもなる(2026-09-08 10:34 実測: 8 枚
    成行 2 脚とも HTTP 200 なのに identity UNKNOWN → HALT。1 秒後には LONG 8 と SELL の
    子 4 本 …154/155・…172/173 が見えていた)。子は「逆方向・active・parentId で相互に
    結ばれた 2 行」で、片方が真の親(ENTRY 注文)を ``brokerParentId`` に持つ。

    束縛の規則は他の経路と同じく **一意性**:

    * 送信前の窓に無い子行を含む live な対だけを候補にする
    * 既に別の脚へ束縛済みの親 id / 子 id を持つ対は外す(``exclude_order_ids``)
    * 約定履歴の親 id → 枚数(``parent_qty``)が全候補にあれば、この脚の枚数の対に絞る
    * 候補がちょうど 1 対ならそれ。複数のときは ``pick_index`` / ``expected_count``
      (呼び出し側が「窓の中の対の数 = 未束縛の脚の数」を確認して作成順に割り当てる
      最終パス)でだけ決める。それ以外は None
    * 真の親 id が無い対は束縛しない(親を名指しできない identity は下流で使えない)
    """
    pairs = new_bracket_pairs(before_view, after_view, account=account, symbol=symbol,
                              action=action)
    if not pairs:
        return None
    excluded = {str(value) for value in (exclude_order_ids or []) if value not in (None, "")}
    candidates = [pair for pair in pairs if pair.get("live")
                  and str(pair.get("brokerParentId") or "") not in excluded
                  and not (set(pair["orderIds"]) & excluded)]
    candidates = _select_by_qty(candidates, _qty_of(qty), parent_qty)
    if not candidates:
        return None
    if len(candidates) == 1:
        pair = candidates[0]
    elif (pick_index is not None and expected_count is not None
          and len(candidates) == int(expected_count)
          and 0 <= int(pick_index) < len(candidates)):
        pair = candidates[int(pick_index)]
    else:
        return None
    return bracket_identity(pair, account=account,
                            platform=platform or (after_view or {}).get("platform"))


def attach_bracket_ids(identity: Any, before_view: Any, after_view: Any, *, account: Any,
                       symbol: Any, action: Any) -> Any:
    """親行で束縛できた identity に、同じ窓に現れたその親の子 2 行の id / receipt を添える(R76)。

    親 id が後で解決できなくなっても(R53: 古い注文は口座から消える)、binder が子の構造で
    照合できるようにする。窓の中でその親を名乗る対がちょうど 1 つのときだけ添え、
    それ以外は identity をそのまま返す(束縛の可否は変えない)。
    """
    if not isinstance(identity, dict) or identity.get("bracketOrderIds"):
        return identity
    parent = str(identity.get("orderId") or "").strip()
    if not parent:
        return identity
    pairs = new_bracket_pairs(before_view, after_view, account=account, symbol=symbol, action=action)
    matching = [pair for pair in (pairs or []) if str(pair.get("brokerParentId") or "") == parent]
    if len(matching) != 1:
        return identity
    return {**identity, "bracketOrderIds": list(matching[0]["orderIds"]),
            "bracketReceipts": list(matching[0]["receipts"])}


def _parent_row_identity(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    order_id, receipt = _order_id(row), str(row.get("receipt") or "").strip()
    if not order_id or not receipt:
        return None
    status = str(row.get("status") or "").upper()
    return {"orderId": order_id, "receipt": receipt, "status": status,
            "filledAt": row.get("timestamp") if status == "FILLED" else None,
            "identitySource": "broker-orders"}


def late_bind_entry_legs(origin_view: Any, after_view: Any, *, account: Any, symbol: Any,
                         action: Any, order_type: Any, entry_price: Any, legs: Any,
                         bound: Any = None, platform: Any = None, parent_qty: Any = None
                         ) -> Dict[str, Dict[str, Any]]:
    """全脚の送信後に、まだ束縛できていない脚を **送信前の窓(origin)** に対して
    まとめて束縛する(R76 の最終パス)。

    脚ごとの窓が取りこぼした(TP1 の窓では何も見えず RUNNER の窓に 2 対が現れた等)
    ケースの救済。候補は origin に無い行から作る:

    * 同方向の親行(parentId 無し。項目が揃う行は種別/価格も照合)
    * live な OCO 対 → 真の親 id(親行が見えていれば同じ id に統合)

    候補の数が **未束縛の脚の数とちょうど一致**するときだけ割り当てる。枚数の証拠が
    全候補にあり脚の枚数が互いに異なれば枚数で、そうでなければ作成順(TP1 → RUNNER の
    送信順と同じ)で割り当てる。1 つでも合わなければ何も束縛しない(推測しない)。
    戻り値は ``{legId: identity}``(未束縛の脚だけ)。
    """
    legs = [(str(leg_id).upper(), _qty_of(leg_qty)) for leg_id, leg_qty in (legs or [])]
    bound = bound if isinstance(bound, dict) else {}
    unbound = [(leg_id, leg_qty) for leg_id, leg_qty in legs if leg_id not in bound]
    if not unbound:
        return {}
    fresh = new_rows(origin_view, after_view)
    if not fresh or any(not _allowlisted(row) for row in fresh):
        return {}
    excluded = set()
    for identity in bound.values():
        excluded |= _identity_ids(identity)
    wanted_type = str(order_type or "").upper()
    candidates: Dict[str, Dict[str, Any]] = {}
    for row in fresh:
        if not _scoped(row, account, symbol, action) or _parent_link(row):
            continue
        if not _fields_incomplete(row):
            if str(row.get("orderType") or "").upper() != wanted_type:
                continue
            if wanted_type == "LIMIT" and not _same_price(row.get("limitPrice"), entry_price):
                continue
        identity = _parent_row_identity(row)
        if identity is None or identity["orderId"] in excluded:
            continue
        row_qty = _qty_of(row.get("qty")) if not _fields_incomplete(row) else None
        candidates[identity["orderId"]] = {
            "identity": identity, "created": _created_at(row),
            "qty": (row_qty if row_qty is not None
                    else _qty_of((parent_qty or {}).get(identity["orderId"])))}
    pairs = new_bracket_pairs(origin_view, after_view, account=account, symbol=symbol,
                              action=action)
    if pairs is None:
        return {}
    resolved_platform = platform or (after_view or {}).get("platform")
    for pair in pairs:
        if not pair.get("live") or set(pair["orderIds"]) & excluded:
            continue
        parent = str(pair.get("brokerParentId") or "")
        if not parent:
            return {}
        if parent in excluded:
            continue
        if parent in candidates:
            existing = candidates[parent]
            existing["identity"] = {**existing["identity"],
                                    "bracketOrderIds": list(pair["orderIds"]),
                                    "bracketReceipts": list(pair["receipts"])}
            continue
        identity = bracket_identity(pair, account=account, platform=resolved_platform)
        if identity is None:
            return {}
        candidates[parent] = {"identity": identity, "created": pair.get("createdAt") or "",
                              "qty": _qty_of((parent_qty or {}).get(parent))}
    ordered = sorted(candidates.values(),
                     key=lambda item: _creation_key(item["identity"]["orderId"],
                                                    item["created"] or ""))
    if len(ordered) != len(unbound):
        return {}
    leg_qtys = [leg_qty for _leg, leg_qty in unbound]
    if (all(item["qty"] is not None for item in ordered)
            and all(value is not None for value in leg_qtys)
            and len(set(leg_qtys)) == len(leg_qtys)):
        if sorted(item["qty"] for item in ordered) != sorted(leg_qtys):
            return {}
        by_qty = {item["qty"]: item for item in ordered}
        return {leg_id: by_qty[leg_qty]["identity"] for leg_id, leg_qty in unbound}
    return {leg_id: item["identity"] for (leg_id, _qty), item in zip(unbound, ordered)}


def rebind_split_legs_from_brackets(*, account: Any, symbol: Any, action: Any, legs: Any,
                                    broker_orders: Any, window_start: Any, window_end: Any,
                                    platform: Any = None, parent_qty: Any = None
                                    ) -> Tuple[Optional[List[Dict[str, Any]]], str]:
    """後周期の再束縛(R76): 送信前後の窓が無いまま、現在の注文一覧の **構造** から
    UNKNOWN の経路を凍結し直す。

    条件(1 つでも欠ければ None と理由):

    * 一覧が verified で、口座・銘柄の行が全部許可リスト内
    * 同方向の active 行(親候補)が **1 本も無い**(あれば別の注文が同居 → 曖昧)
    * 逆方向の active 行が全部 live な OCO 対にまとまり、対の数が ``legs`` の数と一致
    * 全対が真の親 id を持ち、作成時刻が ``[window_start, window_end]`` の中
    * 対 → 脚は枚数の証拠(全候補にあれば)か作成順で割り当てる

    ``window_*`` は tz 付き datetime。戻り値の行は routeSnapshot の形(ACCEPTED 行)で、
    ``identitySource=broker-brackets`` と子の id / receipt を持つ。
    """
    if not isinstance(broker_orders, dict) or broker_orders.get("verified") is not True:
        return None, "broker orders are UNVERIFIED"
    rows = [row for row in (broker_orders.get("orders") or []) if isinstance(row, dict)]
    wanted_action = str(action or "").upper()
    legs = [(str(leg_id).upper(), _qty_of(leg_qty)) for leg_id, leg_qty in (legs or [])]
    if wanted_action not in {"BUY", "SELL"} or not legs:
        return None, "frozen legs/side are incomplete"
    scoped = [row for row in rows if _account(row) == str(account)
              and str(row.get("symbol") or "") == str(symbol)]
    if any(not _allowlisted(row) for row in scoped):
        return None, "broker order status is not allowlisted"
    same_side_active = [row for row in scoped
                        if str(row.get("action") or "").upper() == wanted_action
                        and str(row.get("status") or "").upper() in broker_status.BROKER_ACTIVE_STATES]
    if same_side_active:
        return None, "another active entry-side order row is present"
    pairs = bracket_pairs(scoped, account=account, symbol=symbol, entry_action=wanted_action)
    if pairs is None:
        return None, "opposite-direction active rows do not form clean OCO pairs"
    if len(pairs) != len(legs):
        return None, f"expected {len(legs)} live bracket pair(s), found {len(pairs)}"
    if not isinstance(window_start, datetime) or not isinstance(window_end, datetime):
        return None, "send window is unavailable"
    for pair in pairs:
        if not pair.get("live"):
            return None, "bracket pair is not live (parent not filled)"
        if not str(pair.get("brokerParentId") or ""):
            return None, "bracket pair names no broker parent id"
        created = _parse_iso(pair.get("createdAt"))
        if created is None:
            return None, "bracket pair has no creation timestamp"
        if not (window_start <= created <= window_end):
            return None, "bracket pair was created outside the send window"
    resolved_platform = platform or broker_orders.get("platform")
    identities = []
    for pair in pairs:
        identity = bracket_identity(pair, account=account, platform=resolved_platform)
        if identity is None:
            return None, "bracket identity could not be derived"
        identities.append({"identity": identity,
                           "qty": _qty_of((parent_qty or {}).get(identity["orderId"]))})
    parent_ids = [item["identity"]["orderId"] for item in identities]
    if len(set(parent_ids)) != len(parent_ids):
        return None, "bracket pairs share one broker parent id"
    leg_qtys = [leg_qty for _leg, leg_qty in legs]
    if (all(item["qty"] is not None for item in identities)
            and all(value is not None for value in leg_qtys)
            and len(set(leg_qtys)) == len(leg_qtys)):
        if sorted(item["qty"] for item in identities) != sorted(leg_qtys):
            return None, "fill quantities do not match the frozen legs"
        by_qty = {item["qty"]: item["identity"] for item in identities}
        assigned = [(leg_id, by_qty[leg_qty]) for leg_id, leg_qty in legs]
    else:
        assigned = [(leg_id, item["identity"]) for (leg_id, _qty), item in zip(legs, identities)]
    snapshot = [{"accountId": str(account), "legId": leg_id, "state": "ACCEPTED", **identity}
                for leg_id, identity in assigned]
    return snapshot, "bound from live bracket structure"

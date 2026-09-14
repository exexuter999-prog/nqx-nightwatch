#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ブローカー建玉の照会アダプタ(読み取り専用)。

## なぜ必要か

`order.py --status` が読んでいるのは `.secrets/order_log.json` — **こちらから
送った注文の記録**であって、ブローカーの実建玉ではない。

  - 指値が約定していなくても 1 件として記録される
  - SL/TP で決済されても記録は消えない
  - 手動決済・部分決済はどこにも反映されない

したがって order_log をポジションの正本にしてはならない。このモジュールは
Tradovate / CrossTrade の実照会を行い、**照会できなかった場合は建玉を推測せず
`verified=False` を返す**。呼び出し側はそれを STALE として扱い、カードを消さない。

## 発注はしない

このファイルは読み取り専用の HTTP しか行わない。注文送信のコードを追加しないこと。

使い方:
    python broker_status.py --check     # どのアダプタが有効かを表示
    python broker_status.py --account LTATANOBA1000000000001 --json
    python broker_status.py --account LTATANOBA1000000000002 --json
"""
import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import execution_contract


def derived_receipt(platform, account, order_id):
    """Deterministic per-row receipt for brokers that do not issue one.

    CrossTrade returns neither ``receipt`` nor ``requestId`` in its webhook
    response or its REST order rows, so a frozen route identity could never be
    compared against later broker truth and every live leg resolved UNKNOWN.

    The value is derived only from broker-returned row identity, is unique per
    order row, and reproduces byte-identically on every later re-query.  It is
    deliberately *not* independent evidence: collision protection still comes
    from the broker order id.  ``receiptSource`` records which rows are
    derived so an audit can tell the two apart.
    """
    order_text = str(order_id or "").strip()
    if not order_text:
        return None
    platform_text = str(platform or "BROKER").strip().upper() or "BROKER"
    return f"{platform_text}:{str(account or '').strip()}:{order_text}"


def position_identity(position):
    """Return a broker-instance identity, never a side/price similarity key.

    A verified open position is still not identifiable when the broker omits
    account or every fill/order identity.  Returning ``None`` is intentional:
    callers must not let an old automated plan own a later manual position.
    """
    if not isinstance(position, dict) or position.get("verified") is not True:
        return None
    try:
        qty = int(position.get("qty") or 0)
    except (TypeError, ValueError):
        return None
    if qty <= 0:
        return None
    account = position.get("accountId") or position.get("account")
    if account in (None, ""):
        return None
    fields = ("account", "accountId", "symbol", "side", "orderId", "receipt",
              "filledAt", "initialQty")
    frozen = {field: position.get(field) for field in fields
              if position.get(field) not in (None, "")}
    if not any(field in frozen for field in ("orderId", "receipt", "filledAt")):
        return None
    canonical = json.dumps(frozen, sort_keys=True, separators=(",", ":"),
                           default=str).encode("utf-8")
    return "POS:" + hashlib.sha256(canonical).hexdigest()


def extract_replacement_receipt(route_results):
    """Extract replacement broker identities from CrossTrade JSON responses.

    A generic HTTP 2xx body is not evidence that a protective OCO exists.
    Success requires one explicit receipt per account naming both replacement
    order IDs and their common OCO/parent group.
    """
    receipts = []
    for item in route_results or []:
        if not isinstance(item, (tuple, list)) or len(item) < 3:
            return None, "management route result shape is invalid"
        account, status, body = item[0], item[1], item[2]
        try:
            status = int(status)
        except (TypeError, ValueError):
            return None, "management route status is invalid"
        if not 200 <= status < 300:
            return None, f"management route was not accepted for {account}"
        try:
            parsed = json.loads(body) if isinstance(body, str) else body
        except json.JSONDecodeError:
            return None, "management route receipt is not JSON"
        if not isinstance(parsed, dict):
            return None, "management route receipt is unavailable"
        data = parsed.get("data") if isinstance(parsed.get("data"), dict) else parsed
        stop_id = data.get("stopOrderId")
        target_id = data.get("targetOrderId") or data.get("takeProfitOrderId")
        parent = data.get("ocoGroupId") or data.get("parentId")
        receipt = data.get("receipt") or data.get("requestId")
        if any(value in (None, "") for value in (stop_id, target_id, parent, receipt)):
            return None, "management route receipt lacks replacement order/OCO identity"
        if str(stop_id) == str(target_id):
            return None, "management route receipt duplicates replacement order ids"
        receipts.append({"accountId": str(account), "stopOrderId": str(stop_id),
                         "targetOrderId": str(target_id), "ocoGroupId": str(parent),
                         "receipt": str(receipt)})
    if not receipts:
        return None, "management route receipt is empty"
    return {"accounts": receipts}, "replacement route receipt verified"


def oco_sibling_pair(rows, *, account, expected_action, symbol=None, all_rows=None):
    """R80: 逆方向の未終端行が「建玉に対する OCO 兄弟 1 組」であることを構造で証明する。

    2026-09-12 01:53 の実弾で、cancelandbracket 後の SL が TP の子ブラケット(親の約定
    まで起動しない = 建玉を守っていない)ではないかと疑われた。従来の構造照合は
    `parentId` に潰したリンクが「相互か同一」なら通し、SUSPENDED も active に数えて
    いたので、OSO の子と OCO の兄弟を見分けられなかった。判定はこの関数 1 か所に置く。

    戻り値は ``([first, second], None)``(orderId 順)か ``(None, reason)``。条件は全部:

    1. 口座・銘柄・方向が一致する **未終端**行がちょうど 2 本
    2. 2 本とも BROKER_LIVE_PROTECTIVE_STATES(SUSPENDED / PENDING は不可)
    3. 2 本とも `brokerParentId` が空(親を持つ行は注文の子ブラケット = OSO)
    4. `brokerOcoId` が相互(a.ocoId == b.id かつ b.ocoId == a.id)。片方向・欠落・
       `parentId` だけの結びつきは不可
    5. ``all_rows``(既定は rows)の未終端行のどれも `brokerParentId` で 2 本のどちらかを
       指していない(張り替え注文に子ブラケットが付いていない)

    価格・数量は比較しない(ブローカーが返さない)。ここで証明できるのは構造だけ。
    """
    wanted_account = str(account)
    wanted_action = str(expected_action or "").upper()
    if not wanted_action:
        return None, "expected protective action is unknown"

    def in_scope(row):
        if not isinstance(row, dict):
            return False
        if str(row.get("accountId") or row.get("account") or "") != wanted_account:
            return False
        if symbol not in (None, "") and str(row.get("symbol") or "") != str(symbol):
            return False
        return True

    candidates = []
    for row in rows or []:
        if not in_scope(row):
            continue
        if str(row.get("status") or "").upper() in BROKER_TERMINAL_STATES:
            continue
        if str(row.get("action") or "").upper() != wanted_action:
            continue
        candidates.append(row)
    if len(candidates) != 2:
        return None, (f"expected exactly 2 live {wanted_action} protective rows, "
                      f"found {len(candidates)}")
    first, second = sorted(candidates, key=lambda item: str(item.get("orderId") or ""))
    id_a, id_b = str(first.get("orderId") or ""), str(second.get("orderId") or "")
    if not id_a or not id_b or id_a == id_b:
        return None, "protective rows lack distinct order ids"
    for row in (first, second):
        status = str(row.get("status") or "").upper()
        if status not in BROKER_LIVE_PROTECTIVE_STATES:
            return None, (f"protective order {row.get('orderId')} is {status or '<empty>'}, not live "
                          "(a SUSPENDED row is a bracket child waiting for its parent to fill)")
        parent = str(row.get("brokerParentId") or "")
        if parent:
            return None, (f"protective order {row.get('orderId')} is a bracket child of order "
                          f"{parent} (OSO), not a sibling on the position")
    oco_a, oco_b = str(first.get("brokerOcoId") or ""), str(second.get("brokerOcoId") or "")
    if not oco_a or not oco_b:
        return None, "OCO link (ocoId) is missing on the protective rows"
    if oco_a != id_b or oco_b != id_a:
        return None, f"OCO link is not mutual ({id_a}->{oco_a}, {id_b}->{oco_b})"
    pair_ids = {id_a, id_b}
    for row in (rows if all_rows is None else all_rows) or []:
        if not in_scope(row):
            continue
        if str(row.get("status") or "").upper() in BROKER_TERMINAL_STATES:
            continue
        if str(row.get("brokerParentId") or "") in pair_ids:
            return None, (f"order {row.get('orderId')} is a bracket child attached to replacement "
                          f"order {row.get('brokerParentId')}; the replacement must have no children")
    return [first, second], None


def verify_protective_orders(order_view, accounts, qty, stop, target, *, side=None,
                             position_generation=None, position_before=None,
                             current_position=None,
                             route_receipt=None):
    """Verify the replacement SL and TP identities after MODIFY.

    The broker response must expose active order ids, account ids, prices and
    quantities for both protective sides.  Incomplete schemas are UNKNOWN,
    not success — except the R52/R80 structural mode for brokers that return
    no price/qty at all (CrossTrade/Tradovate), where the pair must be proven
    to be OCO siblings on the position by ``oco_sibling_pair``.
    """
    if not isinstance(order_view, dict) or order_view.get("verified") is not True:
        return False, "broker protective orders are UNVERIFIED"
    rows = order_view.get("activeOrders")
    if not isinstance(rows, list):
        return False, "broker activeOrders are unavailable"
    wanted = sorted({str(value) for value in accounts if value not in (None, "")})
    if not wanted:
        return False, "account scope is empty"
    try:
        wanted_qty = int(qty)
        wanted_stop = float(stop)
        wanted_target = float(target)
    except (TypeError, ValueError):
        return False, "protective order contract is invalid"
    if wanted_qty <= 0:
        return False, "protective qty is invalid"
    if (not position_generation or not isinstance(position_before, dict)
            or not isinstance(current_position, dict)):
        return False, "position->orders->position transaction is incomplete"
    before_identity = position_identity(position_before)
    current_identity = position_identity(current_position)
    if (position_before.get("verified") is not True
            or current_position.get("verified") is not True
            or not before_identity or not current_identity
            or before_identity != str(position_generation)
            or current_identity != str(position_generation)):
        return False, "protective postverify position generation changed"
    before_account = str(position_before.get("accountId") or position_before.get("account") or "")
    before_side = str(position_before.get("side") or "").upper()
    try:
        before_qty = int(position_before.get("qty") or 0)
    except (TypeError, ValueError):
        return False, "protective preverify qty is invalid"
    current_account = str(current_position.get("accountId") or current_position.get("account") or "")
    current_side = str(current_position.get("side") or "").upper()
    expected_position_side = "LONG" if str(side or "").upper() in {"BUY", "LONG"} else "SHORT"
    if (current_account not in set(wanted) or before_account != current_account
            or current_side != expected_position_side or before_side != current_side
            or before_qty != wanted_qty or int(current_position.get("qty") or 0) != wanted_qty):
        return False, "protective pre/post position account/side/qty mismatch"

    receipt_rows = ((route_receipt or {}).get("accounts")
                    if isinstance(route_receipt, dict) else None)
    if not isinstance(receipt_rows, list) or len(receipt_rows) != len(wanted):
        return False, "replacement route receipt is unavailable or ambiguous"
    receipt_by_account = {str(row.get("accountId")): row for row in receipt_rows
                          if isinstance(row, dict) and row.get("accountId")}
    if set(receipt_by_account) != set(wanted):
        return False, "replacement receipt account scope mismatch"
    expected_action = None
    if side is not None:
        expected_action = "SELL" if str(side).upper() in {"BUY", "LONG"} else "BUY"

    structural = False
    for account in wanted:
        scoped = [row for row in rows if isinstance(row, dict)
                  and str(row.get("accountId") or row.get("account") or "") == account]
        if len(scoped) != 2:
            return False, f"protective order set is extra/missing/ambiguous for account {account}"
        if all(row.get("fieldsComplete") is False for row in scoped):
            # R52: CrossTrade/Tradovate は保護注文の数量・種別・価格を返さない。
            # 価格を裏取りできない代わりに **構造**で照合する(2026-09-05 ユーザー決定
            # 「価格の裏取りなしで成功とみなす」)。
            # R80: 構造の定義を「建玉に対する OCO 兄弟」に固定した(oco_sibling_pair)。
            # 従来は `parentId` に潰したリンクが相互か同一なら通し、SUSPENDED も active に
            # 数えていたため、TP の子ブラケット(親の約定まで起動しない)として付いた SL を
            # 見分けられなかった(2026-09-12 01:53 の疑義)。注文 ID と per-order receipt が
            # 張り替え時に束縛した対と一致する点は R52 のまま。
            if not expected_action:
                return False, f"protective side is required for structural verification for account {account}"
            all_rows = order_view.get("orders") if isinstance(order_view.get("orders"), list) else rows
            pair, reason = oco_sibling_pair(
                rows, account=account, expected_action=expected_action,
                symbol=current_position.get("symbol"), all_rows=all_rows)
            if pair is None:
                return False, (f"protective replacement is not an OCO sibling pair on the position "
                               f"for account {account}: {reason}")
            receipt = receipt_by_account[account]
            first, second = pair
            ids = {str(first.get("orderId") or ""), str(second.get("orderId") or "")}
            if ids != {str(receipt.get("stopOrderId") or ""), str(receipt.get("targetOrderId") or "")}:
                return False, f"protective replacement identity/OCO mismatch for account {account}"
            frozen_receipts = {str(receipt.get("stopReceipt") or ""), str(receipt.get("targetReceipt") or "")}
            live_receipts = {str(first.get("receipt") or ""), str(second.get("receipt") or "")}
            if "" in frozen_receipts or frozen_receipts != live_receipts:
                return False, f"protective per-order receipt mismatch for account {account}"
            structural = True
            continue
        stop_rows, target_rows = [], []
        for row in scoped:
            order_id = row.get("orderId")
            try:
                row_qty = int(row.get("qty") or 0)
            except (TypeError, ValueError):
                continue
            if order_id in (None, "") or row_qty <= 0:
                continue
            try:
                stop_price = (float(row["stopPrice"])
                              if row.get("stopPrice") is not None else None)
                limit_price = (float(row["limitPrice"])
                               if row.get("limitPrice") is not None else None)
            except (TypeError, ValueError):
                continue
            order_type = str(row.get("orderType") or "").upper()
            if str(row.get("status") or "").upper() not in BROKER_ACTIVE_STATES:
                return False, f"protective order is not in an active allowlisted state for {account}"
            action = str(row.get("action") or "").upper()
            if expected_action and action != expected_action:
                return False, f"protective order action mismatch for account {account}"
            if (stop_price is not None and abs(stop_price - wanted_stop) < 1e-9
                    and "STOP" in order_type):
                stop_rows.append(row)
            if (limit_price is not None and abs(limit_price - wanted_target) < 1e-9
                    and "LIMIT" in order_type):
                target_rows.append(row)
            if row_qty != wanted_qty:
                return False, f"protective qty mismatch for account {account}"
        if len(stop_rows) != 1 or len(target_rows) != 1:
            return False, f"protective STOP/LIMIT pair mismatch for account {account}"
        stop_row, target_row = stop_rows[0], target_rows[0]
        parent_stop = str(stop_row.get("parentId") or "")
        parent_target = str(target_row.get("parentId") or "")
        receipt = receipt_by_account[account]
        if (not parent_stop or parent_stop != parent_target
                or parent_stop != str(receipt.get("ocoGroupId") or "")
                or str(stop_row.get("orderId")) != str(receipt.get("stopOrderId"))
                or str(target_row.get("orderId")) != str(receipt.get("targetOrderId"))):
            return False, f"protective replacement identity/OCO mismatch for account {account}"
        # A broker-acquired receipt freezes one receipt per replacement order.
        # When present both must still match the live rows exactly; a shared
        # request-level receipt (legacy response-body route) keeps the older
        # single-value comparison above.
        stop_receipt = str(receipt.get("stopReceipt") or "")
        target_receipt = str(receipt.get("targetReceipt") or "")
        if stop_receipt or target_receipt:
            if (not stop_receipt or not target_receipt
                    or stop_receipt == target_receipt
                    or str(stop_row.get("receipt") or "") != stop_receipt
                    or str(target_row.get("receipt") or "") != target_receipt):
                return False, f"protective per-order receipt mismatch for account {account}"
    suffix = f" for {position_generation}" if position_generation else ""
    if structural:
        return True, ("protective OCO sibling pair verified structurally (mutual ocoId, no OSO "
                      "parent, both live; broker rows carry no price/qty, prices not "
                      f"confirmed){suffix}")
    return True, f"protective opposite-action STOP/LIMIT OCO verified{suffix}"

# Windows のコンソール既定は cp932 で日本語ログが化ける(telegram_bot.py と同じ対処)。
for _stream in ("stdout", "stderr"):
    _file = getattr(sys, _stream, None)
    if _file is not None and hasattr(_file, "reconfigure"):
        try:
            _file.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

BASE = os.path.dirname(os.path.abspath(__file__))
CROSSTRADE_ENV = os.path.join(BASE, ".secrets", "crosstrade.env")
TRADOVATE_ENV = os.path.join(BASE, ".secrets", "tradovate.env")
TOKEN_CACHE = os.path.join(BASE, ".secrets", "tradovate_token.json")

DEFAULT_SYMBOL = "MNQU6"
HTTP_TIMEOUT = 15


def now_iso():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def read_env(path):
    cfg = {}
    if not os.path.exists(path):
        return cfg
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                cfg[key.strip()] = value.strip()
    return cfg


def _get_json(url, headers=None, timeout=HTTP_TIMEOUT):
    req = urllib.request.Request(url, headers=headers or {}, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8", "replace"))


def _post_json(url, payload, headers=None, timeout=HTTP_TIMEOUT):
    data = json.dumps(payload).encode("utf-8")
    merged = {"Content-Type": "application/json"}
    merged.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=merged, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8", "replace"))


class Unavailable(Exception):
    """照会できなかった。建玉が無いことを意味しない。"""


#: per-order 詳細で「ブローカーがその注文IDを解決できない」ことを表す HTTP。
#: CrossTrade は口座から消えた古い Tradovate 注文へ 400
#: `{"success": false, "error": "tradovate_rejected"}` を返す(2026-09-07 実測)。
ORDER_UNRESOLVABLE_STATUSES = {400, 404}


def _order_unresolvable(exc):
    """per-order 詳細の HTTPError が「解決できない ID」かを判定する(R53)。

    本文が ``success: false`` の JSON エンベロープであるときだけ真。読めない本文・
    別形式・別コードは「今は分からない」に倒し、呼び出し側で Unavailable にする。
    """
    if exc.code not in ORDER_UNRESOLVABLE_STATUSES:
        return False
    try:
        body = json.loads(exc.read().decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001 — 本文が読めないなら不明側へ倒す
        return False
    return isinstance(body, dict) and body.get("success") is False


#: 口座一覧の応答で「発注先として書く文字列」が載りうるキー。
#: `.secrets/crosstrade.env` の `CROSSTRADE_ACCOUNTS` に入る値がこれ。
ACCOUNT_LABEL_KEYS = ("account", "name", "accountName", "nickname", "label")
#: ブローカー側の内部 ID(数値)が載りうるキー。発注先には使わない。
ACCOUNT_ID_KEYS = ("accountId", "brokerAccountId", "id")
#: 使えない口座を示す状態語。2026-08-21 の実測で `unknown_account` が出た。
ACCOUNT_DEAD_STATES = {"UNKNOWN_ACCOUNT", "UNKNOWN", "BREACHED", "CLOSED",
                       "DISABLED", "INACTIVE", "EXPIRED"}


def _normalize_account_row(row):
    """口座一覧の1行を正規化する。**認識できない形は推測で埋めない。**

    `raw` を必ず残すのは、CrossTrade の応答形が確定していないため。
    後から別のキーが要ると分かったときに、記録を見れば追える。
    """
    if not isinstance(row, dict):
        return {"id": None, "accountId": None, "status": None,
                "usable": False, "raw": row}
    label = next((str(row[key]) for key in ACCOUNT_LABEL_KEYS
                  if row.get(key) not in (None, "")), None)
    account_id = next((str(row[key]) for key in ACCOUNT_ID_KEYS
                       if row.get(key) not in (None, "")), None)
    status = next((str(row[key]) for key in ("status", "state", "error")
                   if row.get(key) not in (None, "")), None)
    environment = next((str(row[key]) for key in ("environment", "env", "mode")
                        if row.get(key) not in (None, "")), None)
    dead = str(status or "").strip().upper().replace(" ", "_") in ACCOUNT_DEAD_STATES
    return {
        "id": label,
        "accountId": account_id,
        "status": status,
        "environment": environment,
        # 発注先に書いてよいか。ラベルが取れないものは**使えないものとして扱う**
        # (推測した文字列を発注経路へ入れない)。
        "usable": bool(label) and not dead,
        "raw": row,
    }


def realised_prices(position_item):
    """決済済みポジションの平均建値・平均決済値をブローカーの数字から出す。

    Tradovate の position は往復の累計を持っている:
        bought / boughtValue, sold / soldValue
    平均値はこの割り算だけで出る。**相場から推定しない。**
    どちらかでも欠けていたら何も返さない(推測で埋めない)。

    戻り値の avgExit は「決済結果画面が使ってよい唯一の決済価格」。
    """
    out = {}
    try:
        bought = float(position_item.get("bought") or 0)
        sold = float(position_item.get("sold") or 0)
        bought_value = position_item.get("boughtValue")
        sold_value = position_item.get("soldValue")
        if not bought or not sold or bought_value is None or sold_value is None:
            return out
        avg_buy = float(bought_value) / bought
        avg_sell = float(sold_value) / sold
    except (TypeError, ValueError, ZeroDivisionError):
        return out

    prev = position_item.get("prevPos")
    # prevPos > 0 なら元は買い建て = 売りが決済。< 0 ならその逆。
    # 判定できない場合は方向を推測せず、両方の平均だけ返す。
    if prev is not None:
        try:
            was_long = float(prev) > 0
        except (TypeError, ValueError):
            was_long = None
        if was_long is True:
            out["avgEntry"] = avg_buy
            out["avgExit"] = avg_sell
            out["side"] = "LONG"
        elif was_long is False:
            out["avgEntry"] = avg_sell
            out["avgExit"] = avg_buy
            out["side"] = "SHORT"
    out["avgBuy"] = avg_buy
    out["avgSell"] = avg_sell
    return out


# ---------------------------------------------------------------- Tradovate

class TradovateAdapter:
    """Tradovate REST の建玉照会。

    ⚠ 実口座での動作は未検証。`.secrets/tradovate.env` を設定した上で
    `python broker_status.py --check` を実行して疎通を確認すること。
    認証情報が無い環境では自動的に無効になる(例外を投げて次のアダプタへ)。

    必要な `.secrets/tradovate.env`:
        TRADOVATE_BASE=https://demo.tradovateapi.com/v1
        TRADOVATE_USERNAME=...
        TRADOVATE_PASSWORD=...
        TRADOVATE_APP_ID=...
        TRADOVATE_APP_VERSION=1.0
        TRADOVATE_CID=...
        TRADOVATE_SECRET=...
        TRADOVATE_DEVICE_ID=...
        TRADOVATE_ACCOUNT_SPEC=<Tradovate の口座名。省略時は最初の口座>
    """

    name = "tradovate-rest"

    def __init__(self, cfg):
        self.cfg = cfg
        self.base = cfg.get("TRADOVATE_BASE", "https://demo.tradovateapi.com/v1").rstrip("/")
        required = ("TRADOVATE_USERNAME", "TRADOVATE_PASSWORD", "TRADOVATE_APP_ID",
                    "TRADOVATE_CID", "TRADOVATE_SECRET")
        self.configured = all(cfg.get(key) for key in required)

    # -- 認証 --------------------------------------------------------

    def _cached_token(self):
        try:
            with open(TOKEN_CACHE, encoding="utf-8") as fh:
                cached = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(cached, dict):
            return None
        # 有効期限の 60 秒前には取り直す
        if cached.get("expiresAt", 0) - 60 > time.time() and cached.get("token"):
            return cached
        return None

    def _store_token(self, token, expires_at):
        os.makedirs(os.path.dirname(TOKEN_CACHE), exist_ok=True)
        with open(TOKEN_CACHE, "w", encoding="utf-8") as fh:
            json.dump({"token": token, "expiresAt": expires_at}, fh)
        try:
            os.chmod(TOKEN_CACHE, 0o600)
        except OSError:
            pass  # Windows では効かないことがある。.secrets 自体を共有しない運用で担保する。

    def _token(self):
        cached = self._cached_token()
        if cached:
            return cached["token"]
        payload = {
            "name": self.cfg["TRADOVATE_USERNAME"],
            "password": self.cfg["TRADOVATE_PASSWORD"],
            "appId": self.cfg["TRADOVATE_APP_ID"],
            "appVersion": self.cfg.get("TRADOVATE_APP_VERSION", "1.0"),
            "cid": self.cfg["TRADOVATE_CID"],
            "sec": self.cfg["TRADOVATE_SECRET"],
        }
        if self.cfg.get("TRADOVATE_DEVICE_ID"):
            payload["deviceId"] = self.cfg["TRADOVATE_DEVICE_ID"]
        try:
            result = _post_json(f"{self.base}/auth/accesstokenrequest", payload)
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
            raise Unavailable(f"tradovate auth failed: {exc}")
        token = result.get("accessToken")
        if not token:
            raise Unavailable(f"tradovate auth rejected: {result.get('errorText') or result}")
        expires_at = time.time() + 3000
        expiration = result.get("expirationTime")
        if expiration:
            try:
                expires_at = datetime.fromisoformat(expiration.replace("Z", "+00:00")).timestamp()
            except ValueError:
                pass
        self._store_token(token, expires_at)
        return token

    def _auth_get(self, path, params=None):
        url = f"{self.base}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        try:
            return _get_json(url, {"Authorization": f"Bearer {self._token()}"})
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
            raise Unavailable(f"tradovate GET {path} failed: {exc}")

    # -- 照会 --------------------------------------------------------

    def _account_id(self):
        accounts = self._auth_get("/account/list")
        if not isinstance(accounts, list) or not accounts:
            raise Unavailable("tradovate returned no accounts")
        spec = self.cfg.get("TRADOVATE_ACCOUNT_SPEC")
        if spec:
            for account in accounts:
                if str(account.get("name")) == str(spec):
                    return account["id"]
            raise Unavailable(f"tradovate account {spec} was not found")
        return accounts[0]["id"]

    def _contract_id(self, symbol):
        contract = self._auth_get("/contract/find", {"name": symbol})
        if not isinstance(contract, dict) or not contract.get("id"):
            raise Unavailable(f"tradovate contract {symbol} was not found")
        return contract["id"]

    def query(self, symbol):
        if not self.configured:
            raise Unavailable("tradovate credentials are not configured")
        account_id = self._account_id()
        contract_id = self._contract_id(symbol)
        positions = self._auth_get("/position/list")
        if not isinstance(positions, list):
            raise Unavailable("tradovate position/list returned an unexpected shape")

        match = None
        for item in positions:
            if item.get("accountId") == account_id and item.get("contractId") == contract_id:
                match = item
                break

        observed_at = now_iso()
        if match is None:
            # 建玉一覧に載っていない = そのコントラクトはフラット。これは確認済みの情報。
            return {"verified": True, "source": self.name, "platform": "TRADOVATE",
                    "accountId": account_id, "symbol": symbol, "qty": 0,
                    "observedAt": observed_at, "closedAt": observed_at}

        net = int(match.get("netPos") or 0)
        if net == 0:
            closed_at = match.get("timestamp") or observed_at
            flat = {"verified": True, "source": self.name, "platform": "TRADOVATE",
                    "accountId": account_id, "symbol": symbol, "qty": 0,
                    "observedAt": observed_at, "closedAt": closed_at}
            flat.update(realised_prices(match))
            return flat

        return {
            "verified": True,
            "source": self.name,
            "platform": "TRADOVATE",
            "accountId": account_id,
            "symbol": symbol,
            "side": "LONG" if net > 0 else "SHORT",
            "qty": abs(net),
            "avgEntry": match.get("netPrice"),
            "filledAt": match.get("timestamp"),
            "observedAt": observed_at,
        }

    def query_orders(self, symbol):
        """Verify whether Tradovate still has a non-terminal order for symbol."""
        if not self.configured:
            raise Unavailable("tradovate credentials are not configured")
        account_id = self._account_id()
        contract_id = self._contract_id(symbol)
        orders = self._auth_get("/order/list")
        if not isinstance(orders, list):
            raise Unavailable("tradovate order/list returned an unexpected shape")
        matching = [item for item in orders if isinstance(item, dict)
                    and item.get("accountId") == account_id and item.get("contractId") == contract_id]
        terminal = {"FILLED", "CANCELED", "CANCELLED", "REJECTED", "EXPIRED"}
        normalized = []
        for item in matching:
            state = _broker_state(item.get("ordStatus") or item.get("status"), "TRADOVATE")
            normalized.append({
                "orderId": item.get("id"), "accountId": item.get("accountId"),
                "contractId": item.get("contractId"), "symbol": symbol, "status": state,
                "orderType": str(item.get("orderType") or item.get("ordType") or "").upper(),
                "action": str(item.get("action") or "").upper(),
                "qty": item.get("orderQty") if item.get("orderQty") is not None else item.get("qty"),
                "limitPrice": item.get("price") if item.get("price") is not None else item.get("limitPrice"),
                "stopPrice": item.get("stopPrice"),
                "parentId": item.get("parentId") or item.get("osId"),
                "receipt": (item.get("receipt") or item.get("requestId")
                            or derived_receipt("TRADOVATE", account_id, item.get("id"))),
                "receiptSource": ("broker" if (item.get("receipt") or item.get("requestId"))
                                  else "derived"),
                "timestamp": item.get("timestamp") or item.get("lastModified"),
            })
        active = [item for item in normalized if item["status"] in BROKER_ACTIVE_STATES]
        terminal_rows = [item for item in normalized if item["status"] in BROKER_TERMINAL_STATES]
        if active:
            aggregate_state = "PENDING"
        elif terminal_rows:
            latest_state = terminal_rows[-1]["status"]
            aggregate_state = "CANCELED" if latest_state == "CANCELLED" else latest_state
        else:
            aggregate_state = "NONE"
        return {"verified": True, "source": self.name, "platform": "TRADOVATE",
                "symbol": symbol,
                "state": aggregate_state, "openCount": len(active),
                "orders": normalized, "activeOrders": active, "terminalOrders": terminal_rows,
                "orderIds": [item.get("orderId") for item in active if item.get("orderId") is not None],
                "filledOrderIds": [item.get("orderId") for item in terminal_rows
                                   if item.get("status") == "FILLED" and item.get("orderId") is not None],
                "accountScope": [str(account_id)],
                "observedAt": now_iso()}


# ---------------------------------------------------------------- CrossTrade

def _first_finite(source, keys):
    """`_first_number` と同じ探索で、**0 を有効な値として返す**(R44)。

    `_first_number` は価格用で `if number:` により 0 を捨てる。価格の 0 は
    ほぼ確実にデータ異常だからで、その判断は正しい。しかし残高・損益では 0 は
    実在する値であり、捨てると「含み益ゼロ」と「取得できていない」が同じ null に
    潰れる。2026-08-26 の実測でも `openPnL: 0.0` が null に化けた。
    """
    if not isinstance(source, dict):
        return None
    for key in keys:
        value = source.get(key)
        if value is None:
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if number == number and number not in (float("inf"), float("-inf")):
            return number
    return None


def _first_number(source, keys):
    """複数ありうるフィールド名から最初に見つかった数値を返す。

    CrossTrade のドキュメントは flat のときの `position` しか例示していない。
    建玉があるときのキー名は確認できていないので、候補を順に見て、
    どれも無ければ **None を返す**(価格を捏造しない)。
    """
    if not isinstance(source, dict):
        return None
    for key in keys:
        value = source.get(key)
        if value is None:
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if number:
            return number
    return None


BROKER_ACTIVE_STATES = {
    # SUSPENDED は Tradovate の OCO 子注文(SL/TP)が **親のエントリーが約定する
    # まで**取る通常の状態。許可リストに無いと `_broker_state` が Unavailable を
    # 投げ、ブラケットを1本でも出した口座が丸ごと UNVERIFIED になる —— つまり
    # 経路の確認も、TP1後の建値寄せも、決済も全部盲目になる(2026-08-25 に発生)。
    # 終端ではないので active 側で数える(生きている注文として新規をブロックする)。
    "WORKING", "ACCEPTED", "SUBMITTED", "PENDING", "PENDING_SUBMIT",
    "SUSPENDED", "PARTIALLY_FILLED", "PARTIAL_FILL",
}
BROKER_TERMINAL_STATES = {"FILLED", "CANCELED", "REJECTED", "EXPIRED"}
#: R80: 建玉を **いま** 守っている保護注文の状態。SUSPENDED は Tradovate の OSO 子が
#: 親の約定を待つ状態で、建玉に対しては何もしない(親が約定 = 建玉が消えた後に起動
#: する)。PENDING は遷移中で、settle で WORKING になるのを待つ。張り替え後の照合
#: (oco_sibling_pair)と R78 修復の「保護行の本数」はこちらで数える。
BROKER_LIVE_PROTECTIVE_STATES = {"WORKING", "ACCEPTED", "SUBMITTED",
                                 "PARTIALLY_FILLED", "PARTIAL_FILL"}


def _broker_state(value, platform=None):
    state = str(value or "").strip().upper().replace("-", "_").replace(" ", "_")
    aliases = {"CANCELLED": "CANCELED", "PENDINGSUBMIT": "PENDING_SUBMIT",
               "PARTIALLYFILLED": "PARTIALLY_FILLED", "PARTIALFILL": "PARTIAL_FILL"}
    state = aliases.get(state, state)
    platform = str(platform or "").upper()
    platform_contract = execution_contract.CONTRACT["brokerObservation"].get(
        "statusByPlatform", {}).get(platform)
    allowed = (set(platform_contract.get("active", []))
               | set(platform_contract.get("terminal", []))) if platform_contract else (
                   BROKER_ACTIVE_STATES | BROKER_TERMINAL_STATES)
    if state not in allowed:
        raise Unavailable(f"broker order state {state or '<empty>'} is not allowlisted")
    return state


def normalize_crosstrade_orders(payload, *, platform, account, symbol,
                                account_aliases=None, contract_ids=None,
                                unresolved_order_ids=None):
    """Normalize documented CrossTrade NT8/Tradovate order envelopes.

    URL scoping is never treated as ownership proof.  Every row must carry an
    expected account identity and an exact instrument/contract identity.
    """
    platform = str(platform or "").upper()
    if platform not in {"NT8", "TRADOVATE"} or not isinstance(payload, dict):
        raise Unavailable("crosstrade orders platform/envelope is invalid")
    if payload.get("success") is not True:
        raise Unavailable("crosstrade orders success is not explicitly true")
    if platform == "NT8":
        raw_rows = payload.get("orders")
        if raw_rows is None and isinstance(payload.get("data"), dict):
            raw_rows = payload["data"].get("orders")
    else:
        raw_rows = payload.get("data")
    if not isinstance(raw_rows, list):
        raise Unavailable("crosstrade orders schema has no order rows")
    aliases = {str(account)} | {str(value) for value in (account_aliases or []) if value not in (None, "")}
    expected_contracts = {str(value) for value in (contract_ids or []) if value not in (None, "")}
    rows = []
    for raw in raw_rows:
        if not isinstance(raw, dict):
            raise Unavailable("crosstrade order row is not an object")
        account_values = [raw.get(key) for key in ("account", "accountId", "brokerAccountId")
                          if raw.get(key) not in (None, "")]
        if not account_values or any(str(value) not in aliases for value in account_values):
            raise Unavailable("crosstrade order row account is outside configured scope")
        broker_account = account_values[0]
        instruments = [raw.get(key) for key in ("symbol", "instrument", "contractName")
                       if raw.get(key) not in (None, "")]
        contract_id = raw.get("contractId")
        if any(str(value) != str(symbol) for value in instruments):
            raise Unavailable("crosstrade order row contains conflicting symbol identities")
        # R40: allowlist が **空** のとき、この条件は「行が contractId を持つ限り
        # 必ず失敗する」= どうやっても満たせないガードになる。実際 2026-08-25 に
        # CROSSTRADE_CONTRACT_ID_* 未設定のまま生きた注文行を照会し、注文照会が
        # 全周期で Unavailable になった。identity を束縛できず routeSnapshot が
        # 全行 UNKNOWN になり、ENTRY claim が恒久的に宙吊りになった原因である。
        #
        # 同ファイルの状態 allowlist(既定セットへフォールバック)と揃える:
        # **設定されていれば厳格に照合し、未設定なら口座+シンボル identity に
        # 委ねる**。未設定を「全部拒否」ではなく「この追加照合は行わない」と読む。
        if expected_contracts and contract_id not in (None, "") \
                and str(contract_id) not in expected_contracts:
            raise Unavailable("crosstrade order row contractId is outside configured scope")
        if not instruments and contract_id in (None, ""):
            raise Unavailable("crosstrade order row has no verified symbol/contract identity")
        order_id = raw.get("id") if raw.get("id") is not None else raw.get("orderId")
        if order_id in (None, ""):
            raise Unavailable("crosstrade order row has no id")
        state = _broker_state(raw.get("orderState") or raw.get("ordStatus")
                              or raw.get("status"), platform)
        order_type = str(raw.get("orderType") or raw.get("ordType") or "").upper()
        action = str(raw.get("orderAction") or raw.get("action") or "").upper()
        qty = raw.get("quantity") if raw.get("quantity") is not None else raw.get("orderQty")
        if qty is None:
            qty = raw.get("qty")
        # R41: 数量・注文種別が **無い** ことを拒否理由にしない。
        #
        # CrossTrade の Tradovate 経路は一覧でも詳細でも `orderQty` / `orderType`
        # / `price` を返さない(2026-08-25 実測。行は id / ordStatus / action /
        # contractId / timestamp だけ)。ここで例外にすると **生きている注文行が
        # 1本でもある口座は照会そのものが Unavailable** になり、経路 identity を
        # 束縛できず routeSnapshot が全行 UNKNOWN → 実際は4脚とも約定していたのに
        # HALT した。R40 で contractId に対して採ったのと同じ判断:
        # 「未提供を全部拒否ではなく、この照合は行わない」と読む。
        #
        # 身元(口座・銘柄/contract・注文ID・状態)は従来どおり厳格。数量と種別を
        # **必要とする** 消費者(verify_protective_orders)は None を一致失敗として
        # 扱い、(False, 理由) を返して MODIFY を止めるので、緩めても素通りしない。
        if qty is not None:
            try:
                qty = int(qty)
            except (TypeError, ValueError) as exc:
                raise Unavailable("crosstrade order row quantity is invalid") from exc
            if qty <= 0:
                raise Unavailable("crosstrade order row quantity is not positive")
        if not state:
            raise Unavailable("crosstrade order row lacks state")
        rows.append({
            "orderId": str(order_id), "accountId": str(account),
            "brokerAccountId": str(broker_account), "symbol": str(symbol),
            "contractId": str(contract_id) if contract_id not in (None, "") else None,
            "status": state,
            "orderType": order_type or None, "action": action or None, "qty": qty,
            # 数量・種別・価格が揃っているか。保護注文の照合(verify_protective_orders)は
            # これらを要求するので、欠けている行で「一致した」と言わせないための印。
            "fieldsComplete": bool(order_type and action and qty),
            "limitPrice": raw.get("limitPrice") if raw.get("limitPrice") is not None else raw.get("price"),
            "filledPrice": raw.get("filledPrice") if raw.get("filledPrice") is not None else raw.get("avgFillPrice"),
            "stopPrice": raw.get("stopPrice"),
            "filled": raw.get("filled"),
            "parentId": raw.get("ocoId") or raw.get("ocoGroupId") or raw.get("parentId") or raw.get("osId"),
            # R52: Tradovate の子行は `ocoId`(OCO の相方)と `parentId`(**真の親**=ENTRY
            # 注文)を別に持つ(2026-09-05 04:27 実測)。`parentId` は上で OCO 連結として
            # 使い続けるので、真の親は別名で残す。成行の親は一覧に出ず(即 FILLED)
            # per-order 詳細でしか読めないため、これが親 id を知る唯一の broker-truth。
            "brokerParentId": (str(raw.get("parentId"))
                               if raw.get("parentId") not in (None, "") else None),
            # R80: Tradovate のリンクは 3 種類で意味が違う(2026-09-12 実測)。
            #   parentId = OSO(親が約定してから子が起動する)  ocoId = OCO(一方が約定
            #   すれば他方が取消される兄弟)  linkedId = 親から見た最初の子。
            # 上の `parentId` はこれらを 1 欄に潰した互換値なので、張り替え後の照合は
            # 生の `brokerOcoId` / `brokerParentId` で「建玉に対する OCO 兄弟」を証明する。
            "brokerOcoId": (str(raw.get("ocoId"))
                            if raw.get("ocoId") not in (None, "") else None),
            "brokerLinkedId": (str(raw.get("linkedId"))
                               if raw.get("linkedId") not in (None, "") else None),
            "receipt": (raw.get("receipt") or raw.get("requestId")
                        or derived_receipt(platform, account, order_id)),
            "receiptSource": ("broker" if (raw.get("receipt") or raw.get("requestId"))
                              else "derived"),
            "timestamp": raw.get("timestamp") or raw.get("lastModified"),
        })
    active = [row for row in rows if row["status"] not in BROKER_TERMINAL_STATES]
    terminal = [row for row in rows if row["status"] in BROKER_TERMINAL_STATES]
    if active:
        state = "PENDING"
    elif terminal:
        state = terminal[-1]["status"]
    else:
        state = "NONE"
    return {"verified": True, "source": f"crosstrade-{platform.lower()}",
            "platform": platform, "symbol": str(symbol), "state": state,
            "openCount": len(active), "orders": rows, "activeOrders": active,
            "terminalOrders": terminal,
            "orderIds": [row["orderId"] for row in active],
            "filledOrderIds": [row["orderId"] for row in terminal if row["status"] == "FILLED"],
            # R53: 呼び出し側が凍結IDを渡したのに **ブローカーがそのIDを解決できなかった**
            # もの。行が無いことと区別できるようにここへ残す。終端の証明には使えないので、
            # 消費者は「IDでは証明できない」= 不在の証明へ切り替える印として読む。
            "unresolvedOrderIds": sorted({str(value) for value in (unresolved_order_ids or [])}),
            "accountScope": [str(account)], "observedAt": now_iso()}


class CrossTradeAdapter:
    """CrossTrade REST API の建玉照会(Tradovate 経路)。

    プロップ/評価口座は Tradovate 本体の API キーを発行できないため、
    実運用ではこちらが唯一の照会経路になる。

    仕様(https://crosstrade.io/docs/api/positions):
        GET {base}/accounts/{account}/position?instrument={symbol}
        Authorization: Bearer <token>
        → {"success": true,
           "data": {"account": "...", "symbol": "MNQU6", "netPos": 0, "position": {}}}

        netPos = 0 が FLAT。

    必要な `.secrets/crosstrade.env`:
        CROSSTRADE_KEY=<My Account → Security タブ → Reveal で表示される secret key>
        CROSSTRADE_API_BASE=https://app.crosstrade.io/v1/api/tv   (省略可)

    Security タブの secret key は webhook 発注と REST 照会の**両方**に使える
    (2026-08-15 に実測で確認)。したがって `CROSSTRADE_API_TOKEN` は任意で、
    未設定なら `CROSSTRADE_KEY` を流用する。別トークンを使う運用に切り替える
    ときだけ `CROSSTRADE_API_TOKEN` を明示すればよい。

    照会する口座は発注先と一致していなければ意味がないので、
    `CROSSTRADE_ACCOUNTS`(発注経路の正)を優先して読む。
    """

    name = "crosstrade-rest"
    DEFAULT_BASE = "https://app.crosstrade.io/v1/api/tv"

    def __init__(self, cfg):
        self.cfg = cfg
        self.token = cfg.get("CROSSTRADE_API_TOKEN") or cfg.get("CROSSTRADE_KEY")
        accounts = str(cfg.get("CROSSTRADE_ACCOUNTS", "")).strip()
        configured_accounts = [a.strip() for a in re.split(r"[,\n]+", accounts) if a.strip()]
        legacy_account = str(cfg.get("CROSSTRADE_ACCOUNT") or "").strip()
        if not configured_accounts and legacy_account:
            configured_accounts = [legacy_account]
        self.accounts = list(dict.fromkeys(configured_accounts))
        self.account = self.accounts[0] if self.accounts else None
        self.base = cfg.get("CROSSTRADE_API_BASE", self.DEFAULT_BASE).rstrip("/")
        configured_platform = str(cfg.get("CROSSTRADE_PLATFORM") or "").strip().upper()
        self.platform = configured_platform or ("TRADOVATE" if "/api/tv" in self.base.lower() else "NT8")
        if self.platform not in {"NT8", "TRADOVATE"}:
            self.platform = "UNKNOWN"
        self.account_aliases = [cfg.get("CROSSTRADE_ACCOUNT_ID")]
        contract_key = "CROSSTRADE_CONTRACT_ID_" + re.sub(r"[^A-Za-z0-9]", "_", DEFAULT_SYMBOL)
        self.contract_ids = [cfg.get(contract_key), cfg.get("CROSSTRADE_CONTRACT_ID")]
        # 完全な URL を直接指定したい場合の逃げ道(検証用)
        self.override_url = cfg.get("CROSSTRADE_POSITION_URL")
        self.orders_override_url = cfg.get("CROSSTRADE_ORDERS_URL")
        def valid_url(value):
            parsed = urllib.parse.urlparse(str(value or ""))
            return parsed.scheme in {"http", "https"} and bool(parsed.netloc)
        base_valid = valid_url(self.base)
        overrides_valid = all(not value or valid_url(value)
                              for value in (self.override_url, self.orders_override_url))
        platform_matches_base = ((self.platform == "TRADOVATE") ==
                                 ("/api/tv" in self.base.lower()))
        self.configuration_error = None
        if not base_valid or not overrides_valid:
            self.configuration_error = "crosstrade base/override URL is invalid"
        elif self.platform not in {"NT8", "TRADOVATE"} or not platform_matches_base:
            self.configuration_error = "crosstrade platform/base combination is inconsistent"
        self.override_url_valid = overrides_valid
        self.configured = ((bool(self.token and self.account) or bool(self.override_url))
                           and self.configuration_error is None)

    @staticmethod
    def _account_key(account):
        return re.sub(r"[^A-Za-z0-9]", "_", str(account))

    def _resolve_account(self, account=None):
        target = str(account or self.account or "").strip()
        if not target:
            raise Unavailable("crosstrade account is not configured")
        if self.accounts and target not in self.accounts:
            raise Unavailable("requested crosstrade account is outside configured scope")
        if account is not None and self.override_url:
            raise Unavailable("account-scoped query cannot use a shared position override URL")
        return target

    def _account_aliases(self, account):
        scoped = self.cfg.get("CROSSTRADE_ACCOUNT_ID_" + self._account_key(account))
        aliases = [scoped]
        if str(account) == str(self.account):
            aliases.extend(self.account_aliases)
        return [value for value in aliases if value not in (None, "")]

    def _url(self, symbol, account=None):
        if self.override_url:
            return self.override_url
        target = urllib.parse.quote(self._resolve_account(account), safe="")
        return (f"{self.base}/accounts/{target}/position"
                f"?instrument={urllib.parse.quote(str(symbol), safe='')}")

    def list_accounts(self):
        """CrossTrade にリンクされている口座を列挙する(R38)。

        `GET {base}/accounts`。**読み取り専用**で、注文にも建玉にも触れない。

        なぜ要るか: 口座の入替時、`.secrets/crosstrade.env` に古い口座が残っていても
        誰も気付けなかった。2026-08-21 の実測では登録済み3口座のうち2つが
        `unknown_account` で、**気付かずに発注していれば3件中2件が失敗していた**
        (HANDOFF.md の記録)。それまでこの照会は手作業(curl)でしか行われておらず、
        結果を Markdown へ転記するだけで再現手段がリポジトリに残っていなかった。

        `configured` には依存しない。口座一覧が要るのは**まさに口座がまだ分からない
        とき**であり、`configured` は `account` の存在を要求するため使えない。
        必要なのはトークンと妥当な base だけ。

        戻り値は正規化した行のリスト。**認識できない形は推測で埋めず `raw` に残す。**
        """
        if self.configuration_error:
            raise Unavailable(self.configuration_error)
        if not self.token:
            raise Unavailable("CROSSTRADE_API_TOKEN / CROSSTRADE_KEY is not configured")
        headers = {"Content-Type": "application/json",
                   "Authorization": f"Bearer {self.token}"}
        try:
            payload = _get_json(f"{self.base}/accounts", headers)
        except urllib.error.HTTPError as exc:
            raise Unavailable(f"crosstrade account list returned HTTP {exc.code}")
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
            raise Unavailable(f"crosstrade account list failed: {exc}")

        if not isinstance(payload, dict):
            raise Unavailable("crosstrade returned an unexpected shape")
        if payload.get("success") is not True:
            raise Unavailable("crosstrade success is not explicitly true: "
                              f"{payload.get('error') or payload}")
        data = payload.get("data")
        if isinstance(data, dict):
            # 一部の応答は {"accounts": [...]} で包む。どちらでも受けるが、
            # 見つからなければ推測しない。
            data = data.get("accounts", data.get("items"))
        if not isinstance(data, list):
            raise Unavailable("crosstrade response has no account list")
        return [_normalize_account_row(row) for row in data]

    def query_balance(self, account=None):
        """口座残高を照会する(R44)。**読み取り専用**。

        `GET {base}/accounts/{name}` が `data.balance` を返す。実測フィールド:

            netLiq                 現在の純資産(建玉評価込み)
            netLiqSOD              当日始値の純資産(= 前営業日の EOD)
            realizedPnL / openPnL  当日の実現・含み損益
            totalPnL               その合計
            weekRealizedPnL        週次の実現損益

        なぜ要るか: それまで残機(`LIFELINE_*`)は `crosstrade.env` の手入力だけが
        正本で、CLAUDE.md 自身が「更新漏れはそのまま残機の誤認になる」と警告していた。
        2026-08-26 03:14 の実測では設定値 2,973.90 に対し実際は 2,868.10 で、
        **直前の損切り $105.80 がまるごと未反映**だった。可変枚数を入れた後なら
        そのままサイズ誤りになる。

        `/accounts/{name}/balance` など個別パスは 404。残高は口座単体の
        取得結果にだけ載る(実測)。数値が無ければ推測せず `Unavailable`。
        """
        if self.configuration_error:
            raise Unavailable(self.configuration_error)
        if not self.token:
            raise Unavailable("CROSSTRADE_API_TOKEN / CROSSTRADE_KEY is not configured")
        target = self._resolve_account(account)
        quoted = urllib.parse.quote(str(target), safe="")
        headers = {"Content-Type": "application/json",
                   "Authorization": f"Bearer {self.token}"}
        try:
            payload = _get_json(f"{self.base}/accounts/{quoted}", headers)
        except urllib.error.HTTPError as exc:
            raise Unavailable(f"crosstrade balance query returned HTTP {exc.code}")
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
            raise Unavailable(f"crosstrade balance query failed: {exc}")

        if not isinstance(payload, dict) or payload.get("success") is not True:
            raise Unavailable("crosstrade balance success is not explicitly true: "
                              f"{(payload or {}).get('error') or payload}")
        data = payload.get("data")
        balance = data.get("balance") if isinstance(data, dict) else None
        if not isinstance(balance, dict):
            raise Unavailable(f"crosstrade returned no balance for {target}")
        net_liq = _first_finite(balance, ("netLiq", "totalCashValue"))
        if net_liq is None:
            raise Unavailable(f"crosstrade balance has no netLiq for {target}")
        return {
            "verified": True,
            "source": self.name,
            "platform": self.platform,
            "account": str(target),
            "netLiq": net_liq,
            "netLiqSOD": _first_finite(balance, ("netLiqSOD", "totalCashValueSOD")),
            "realizedPnL": _first_finite(balance, ("realizedPnL",)),
            "openPnL": _first_finite(balance, ("openPnL",)),
            "totalPnL": _first_finite(balance, ("totalPnL",)),
            "weekRealizedPnL": _first_finite(balance, ("weekRealizedPnL",)),
            "observedAt": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        }

    def query_fills(self, account=None, scoped=True):
        """約定履歴を照会する(R56)。**読み取り専用**。

        `GET {base}/accounts/{name}/fills` が約定ごとに次を返す(2026-09-05 実測):

            id / orderId / contractId / timestamp / action(Buy|Sell) / qty / price /
            instrument / tradeDate / active / finallyPaired / external

        なぜ要るか: 損益の自動記録(trade_journal)は「凍結プランの脚価格 +
        確定足の到達」から決済価格を推定していたが、手動建玉・ULTRA 枚数・
        トレール後 SL では一度も成立せず、2026-08-27 以降の実トレード 18 件が
        全部 needsManualExit で保留されていた。約定はブローカーが返す事実なので
        推定が要らない。

        ``scoped=False`` は過去口座の遡及記録用で、CROSSTRADE_ACCOUNTS の外の
        口座名でも読む(読むだけなので発注経路の口座制限とは無関係)。
        認識できない行は落とし、推測で埋めない。
        """
        if self.configuration_error:
            raise Unavailable(self.configuration_error)
        if not self.token:
            raise Unavailable("CROSSTRADE_API_TOKEN / CROSSTRADE_KEY is not configured")
        target = self._resolve_account(account) if scoped else str(account or self.account or "").strip()
        if not target:
            raise Unavailable("crosstrade account is not configured")
        quoted = urllib.parse.quote(str(target), safe="")
        headers = {"Content-Type": "application/json",
                   "Authorization": f"Bearer {self.token}"}
        try:
            payload = _get_json(f"{self.base}/accounts/{quoted}/fills", headers)
        except urllib.error.HTTPError as exc:
            raise Unavailable(f"crosstrade fills query returned HTTP {exc.code}")
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
            raise Unavailable(f"crosstrade fills query failed: {exc}")

        if not isinstance(payload, dict) or payload.get("success") is not True:
            raise Unavailable("crosstrade fills success is not explicitly true: "
                              f"{(payload or {}).get('error') or payload}")
        data = payload.get("data")
        if isinstance(data, dict):
            data = data.get("fills", data.get("items"))
        if not isinstance(data, list):
            raise Unavailable(f"crosstrade returned no fill list for {target}")

        fills = []
        for row in data:
            if not isinstance(row, dict):
                continue
            action = str(row.get("action") or row.get("side") or "").strip().upper()
            if action not in ("BUY", "SELL"):
                continue
            qty = _first_finite(row, ("qty", "quantity"))
            price = _first_finite(row, ("price", "fillPrice"))
            stamp = row.get("timestamp") or row.get("time") or row.get("at")
            if qty is None or qty < 1 or price is None or not stamp:
                continue
            fills.append({
                "id": str(row.get("id") or ""),
                "orderId": str(row.get("orderId") or ""),
                "action": action,
                "qty": int(qty),
                "price": float(price),
                "at": str(stamp),
                "instrument": str(row.get("instrument") or ""),
                "contractId": row.get("contractId"),
            })
        fills.sort(key=lambda item: (item["at"], item["id"]))
        return {
            "verified": True,
            "source": self.name,
            "platform": self.platform,
            "account": str(target),
            "fills": fills,
            "observedAt": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        }

    def query(self, symbol, account=None):
        if not self.configured:
            raise Unavailable(self.configuration_error
                              or "CROSSTRADE_API_TOKEN / CROSSTRADE_ACCOUNT is not configured")
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        try:
            target = self._resolve_account(account)
            payload = _get_json(self._url(symbol, account=account), headers)
        except urllib.error.HTTPError as exc:
            raise Unavailable(f"crosstrade position query returned HTTP {exc.code}")
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
            raise Unavailable(f"crosstrade position query failed: {exc}")

        if not isinstance(payload, dict):
            raise Unavailable("crosstrade returned an unexpected shape")
        if payload.get("success") is not True:
            raise Unavailable(f"crosstrade success is not explicitly true: {payload.get('error') or payload}")

        data = payload.get("data")
        if not isinstance(data, dict):
            # data:[] や null は「この口座に建玉が無い」ではなく形が違う。推測しない。
            raise Unavailable("crosstrade response has no position object")

        aliases = {str(target)} | {str(value) for value in self._account_aliases(target)}
        account_values = [data.get(key) for key in ("account", "accountId", "brokerAccountId")
                          if data.get(key) not in (None, "")]
        if not account_values or any(str(value) not in aliases for value in account_values):
            raise Unavailable("crosstrade position account identity is missing/outside configured scope")
        broker_account = account_values[0]
        returned = [data.get(key) for key in ("symbol", "instrument", "contractName")
                    if data.get(key) not in (None, "")]
        contract_id = data.get("contractId")
        expected_contracts = {str(value) for value in self.contract_ids if value not in (None, "")}
        if any(str(value) != str(symbol) for value in returned):
            raise Unavailable(f"crosstrade returned conflicting symbol identities instead of {symbol}")
        # R40: 注文側と同じ理由で、未設定の allowlist は「照合しない」と読む。
        if expected_contracts and contract_id not in (None, "") \
                and str(contract_id) not in expected_contracts:
            raise Unavailable("crosstrade position contractId is outside configured scope")
        if not returned and contract_id in (None, ""):
            raise Unavailable("crosstrade position has no verified symbol/contract identity")

        if data.get("netPos") is None:
            raise Unavailable("crosstrade response has no netPos")
        try:
            net = int(data["netPos"])
        except (TypeError, ValueError):
            raise Unavailable("crosstrade netPos is not an integer")

        observed_at = now_iso()
        detail = data.get("position") if isinstance(data.get("position"), dict) else {}

        if net == 0:
            # R41: FLAT だけは裏取りしてから返す。
            #
            # 2026-08-25 21:15、`/accounts/{id}/position?instrument=MNQU6` が
            # netPos 0 を返している最中に `/accounts/{id}/positions` は
            # netPos −2 / netPrice 29,382.75 を返していた(SHORT 2枚が実在した)。
            # 一覧側の行は `symbol: null` で contractId しか持たないので、
            # instrument 名で絞る単数エンドポイントが取りこぼしたとみられる。
            #
            # UNVERIFIED は新規を止めるだけだが、**FLAT の誤報は建玉の上へ
            # 新規を武装させる**。方向が違うので、食い違ったら FLAT を名乗らない。
            conflict = self._account_position_conflict(target, headers)
            if conflict is not None:
                # R42: 食い違いを見つけたら、**一覧側が我々の銘柄だと証明できる
                # ときに限り**そちらを正本として採用する。
                #
                # 2026-08-26 00:47、この分岐が両口座を UNVERIFIED に落として
                # 建玉の管理(TP1 約定後の runner 建値寄せ)まで止めた。実際には
                # 一覧側が netPos=2 / netPrice=29,243.75 / contractId=4399654 を
                # 持っており、同じ contractId は working order 行が symbol=MNQU6
                # として名乗っていた。証拠は揃っていたのに「読めない」と答えて
                # いたことになる。
                #
                # contractId を束縛できない場合だけ、従来どおり UNVERIFIED。
                # **FLAT を名乗ることは依然として無い。**
                conflict_net = int(conflict.get("netPos") or 0)
                contract_id = conflict.get("contractId")
                proven = self._proven_contract_ids(target, symbol)
                if contract_id not in (None, "") and str(contract_id) in proven:
                    return {
                        "verified": True,
                        "source": self.name + "-positions",
                        "platform": self.platform,
                        "account": str(broker_account),
                        "accountId": str(target),
                        "brokerAccountId": str(broker_account),
                        "symbol": symbol,
                        "side": "LONG" if conflict_net > 0 else "SHORT",
                        "qty": abs(conflict_net),
                        "avgEntry": _first_number(
                            conflict, ("netPrice", "avgPrice", "averagePrice", "price")),
                        "filledAt": conflict.get("timestamp"),
                        "orderId": conflict.get("id"),
                        "receipt": None,
                        "observedAt": observed_at,
                        "detail": "instrument query said FLAT; account position list "
                                  f"is authoritative (contractId={contract_id})",
                    }
                raise Unavailable(
                    "crosstrade position endpoints disagree: instrument query says FLAT "
                    f"but account position list says netPos={conflict_net} "
                    f"(contractId={contract_id} could not be bound to {symbol})")
            return {"verified": True, "source": self.name, "platform": self.platform,
                    "account": str(broker_account),
                    "accountId": str(target), "brokerAccountId": str(broker_account),
                    "symbol": symbol, "qty": 0,
                    "observedAt": observed_at,
                    "closedAt": detail.get("timestamp") or observed_at}

        return {
            "verified": True,
            "source": self.name,
            "platform": self.platform,
            "account": str(broker_account),
            "accountId": str(target), "brokerAccountId": str(broker_account),
            "symbol": symbol,
            "side": "LONG" if net > 0 else "SHORT",
            "qty": abs(net),
            # 平均建値のキー名は未確認。取れなければ None のまま返す。
            "avgEntry": _first_number(detail, ("netPrice", "avgPrice", "averagePrice", "price")),
            "filledAt": detail.get("timestamp"),
            "orderId": detail.get("orderId") or detail.get("id"),
            "receipt": detail.get("receipt"),
            "observedAt": observed_at,
        }

    def _account_position_conflict(self, target, headers):
        """口座単位の建玉一覧に非ゼロの行が残っていないか確かめる。

        戻り値は矛盾している行(dict)、矛盾が無ければ None。
        一覧が読めない・形が違う場合も None を返す —— **この関数は FLAT を
        否定するためだけにあり**、読めないことを理由に建玉を捏造しない
        (照会不能そのものは呼び出し側の既存経路が UNVERIFIED として扱う)。

        銘柄の絞り込みはしない。一覧の行は `symbol: null` で contractId しか
        持たないことがあり、名前で絞ると**まさに取りこぼした行**を見逃す。
        同一口座に別銘柄の建玉が居る運用はしていないので、非ゼロが1行でも
        あれば「FLAT と名乗ってはいけない」と判断する。
        """
        quoted = urllib.parse.quote(str(target), safe="")
        try:
            payload = _get_json(f"{self.base}/accounts/{quoted}/positions", headers)
        except (urllib.error.HTTPError, urllib.error.URLError, OSError,
                json.JSONDecodeError):
            return None
        if not isinstance(payload, dict) or payload.get("success") is not True:
            return None
        rows = payload.get("data")
        if not isinstance(rows, list):
            return None
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                net = int(row.get("netPos") or 0)
            except (TypeError, ValueError):
                continue
            if net:
                return row
        return None

    def _proven_contract_ids(self, target, symbol):
        """注文側から `symbol` の contractId を突き止める。

        建玉一覧の行は `symbol` を持たず contractId しか持たない。一方で注文
        一覧の行は **同じ行に** symbol と contractId を両方載せてくるので、
        そこからだけ束縛を作る。config の allowlist が未設定でも、ブローカー
        自身が同一行で名乗った対応だけを採用する —— 推測はしない。

        戻り値は contractId(str) の集合。読めなければ空集合。
        """
        proven = {str(value) for value in self.contract_ids if value not in (None, "")}
        try:
            snapshot = self.query_orders(symbol, account=target)
        except (Unavailable, urllib.error.HTTPError, urllib.error.URLError,
                OSError, json.JSONDecodeError):
            return proven
        for row in (snapshot.get("orders") or []):
            if not isinstance(row, dict):
                continue
            if str(row.get("symbol") or "") != str(symbol):
                continue
            contract_id = row.get("contractId")
            if contract_id not in (None, ""):
                proven.add(str(contract_id))
        return proven

    def _orders_url(self, account=None):
        override = self.orders_override_url
        if override:
            if not str(override).lower().startswith(("https://", "http://")):
                raise Unavailable("crosstrade orders override URL is invalid")
            return override
        target = urllib.parse.quote(self._resolve_account(account), safe="")
        base = f"{self.base}/accounts/{target}/orders"
        return base + ("?activeOnly=false" if self.platform == "NT8" else "")

    def query_orders(self, symbol, known_order_ids=None, account=None):
        """Read documented account order truth for NT8 or Tradovate.

        Tradovate's collection route contains working orders only.  When the
        caller supplies frozen IDs, individual order endpoints are merged so
        terminal fills can be proven without treating absence as cancellation.
        """
        if not self.configured or self.platform not in {"NT8", "TRADOVATE"}:
            raise Unavailable("crosstrade order query platform is not configured")
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        try:
            target = self._resolve_account(account)
            payload = _get_json(self._orders_url(account=account), headers)
        except urllib.error.HTTPError as exc:
            raise Unavailable(f"crosstrade orders query returned HTTP {exc.code}")
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
            raise Unavailable(f"crosstrade orders query failed: {exc}")
        unresolved = []
        if self.platform == "TRADOVATE" and known_order_ids:
            data = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(data, list):
                raise Unavailable("crosstrade Tradovate working-order schema is invalid")
            by_id = {str(row.get("id") or row.get("orderId")): row
                     for row in data if isinstance(row, dict)}
            quoted_account = urllib.parse.quote(str(target), safe="")
            for order_id in sorted({str(value) for value in known_order_ids if value not in (None, "")}):
                if order_id in by_id:
                    continue
                try:
                    detail = _get_json(
                        f"{self.base}/accounts/{quoted_account}/orders/{urllib.parse.quote(order_id, safe='')}",
                        headers)
                except urllib.error.HTTPError as exc:
                    # R53: 口座から消えた古い注文IDへ CrossTrade は 400
                    # `tradovate_rejected` を返す(2026-09-07 実測)。ここで観測ごと
                    # Unavailable にすると、その凍結プランを持つ限り **注文照会が
                    # 恒久的に UNVERIFIED** になり、reconcile の最初の門で全周期が
                    # 止まる(09-04 の凍結プランで実際に発生し、以後の新規が全滅した)。
                    # 解決できないIDは「今は分からない」ではなく「二度と解決しない」
                    # ので、不明側へ倒しても永久に解けない。行として足さず
                    # `unresolvedOrderIds` に残し、一覧側の真実で観測を成立させる。
                    if _order_unresolvable(exc):
                        unresolved.append(order_id)
                        continue
                    raise Unavailable(f"crosstrade order {order_id} returned HTTP {exc.code}")
                except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
                    raise Unavailable(f"crosstrade order {order_id} query failed: {exc}")
                if not isinstance(detail, dict) or detail.get("success") is not True:
                    raise Unavailable(f"crosstrade order {order_id} success is not explicitly true")
                row = detail.get("data") if isinstance(detail, dict) else None
                if not isinstance(row, dict):
                    raise Unavailable(f"crosstrade order {order_id} schema is invalid")
                by_id[order_id] = row
            payload = {**payload, "data": list(by_id.values())}
        contract_key = "CROSSTRADE_CONTRACT_ID_" + re.sub(r"[^A-Za-z0-9]", "_", str(symbol))
        contract_ids = [self.cfg.get(contract_key), self.cfg.get("CROSSTRADE_CONTRACT_ID")]
        return normalize_crosstrade_orders(
            payload, platform=self.platform, account=target, symbol=symbol,
            account_aliases=self._account_aliases(target), contract_ids=contract_ids,
            unresolved_order_ids=unresolved)


# ---------------------------------------------------------------- 解決

def adapters():
    """照会アダプタを優先順に返す。

    CrossTrade を先に置く。Tradovate 本体の API はプロップ/評価口座に
    キーを発行しないため、この運用では基本的に使えない。
    """
    crosstrade_cfg = read_env(CROSSTRADE_ENV)
    tradovate_cfg = read_env(TRADOVATE_ENV)
    return [CrossTradeAdapter(crosstrade_cfg), TradovateAdapter(tradovate_cfg)]


def query_position(symbol=DEFAULT_SYMBOL, account=None):
    """建玉を照会する。

    戻り値は必ず dict。照会できなかった場合は ``verified=False`` を返し、
    **建玉が無いとは言わない**。呼び出し側はこれを STALE として扱うこと。
    """
    failures = []
    for adapter in adapters():
        if not getattr(adapter, "configured", False):
            failures.append(f"{adapter.name}: not configured")
            continue
        try:
            if account is not None and not isinstance(adapter, CrossTradeAdapter):
                failures.append(f"{adapter.name}: account-scoped query not supported")
                continue
            return adapter.query(symbol, account=account) if isinstance(adapter, CrossTradeAdapter) else adapter.query(symbol)
        except Unavailable as exc:
            failures.append(str(exc))
        except Exception as exc:  # 想定外も「不明」に倒す。決して flat と誤解させない。
            failures.append(f"{adapter.name}: unexpected error: {exc}")

    return {
        "verified": False,
        "source": "unavailable",
        "symbol": symbol,
        "observedAt": now_iso(),
        "detail": "; ".join(failures) or "no broker adapter is configured",
    }


def query_orders(symbol=DEFAULT_SYMBOL, known_order_ids=None, account=None):
    """Return verified open-order state; missing capability is UNVERIFIED."""
    failures = []
    for adapter in adapters():
        method = getattr(adapter, "query_orders", None)
        if not getattr(adapter, "configured", False) or not callable(method):
            failures.append(f"{adapter.name}: order query not configured")
            continue
        try:
            if isinstance(adapter, CrossTradeAdapter):
                return method(symbol, known_order_ids=known_order_ids, account=account)
            if account is not None:
                failures.append(f"{adapter.name}: account-scoped order query not supported")
                continue
            return method(symbol)
        except Unavailable as exc:
            failures.append(str(exc))
        except Exception as exc:
            failures.append(f"{adapter.name}: unexpected order query error: {exc}")
    return {"verified": False, "source": "unavailable", "symbol": symbol,
            "state": "UNKNOWN", "observedAt": now_iso(),
            "detail": "; ".join(failures) or "no broker order adapter is configured"}


def query_balance(account=None):
    """口座残高を照会する(R44)。

    照会できなければ ``verified=False`` を返し、**残高を推測しない**。呼び出し側は
    これを「確認できていない」として扱い、直前の値や設定値へ倒すこと。
    """
    failures = []
    for adapter in adapters():
        method = getattr(adapter, "query_balance", None)
        if not callable(method):
            failures.append(f"{adapter.name}: balance query not supported")
            continue
        try:
            return method(account=account)
        except Unavailable as exc:
            failures.append(str(exc))
        except Exception as exc:
            failures.append(f"{adapter.name}: unexpected balance query error: {exc}")
    return {"verified": False, "source": "unavailable", "account": account,
            "observedAt": now_iso(),
            "detail": "; ".join(failures) or "no broker balance adapter is configured"}


def query_fills(account=None, scoped=True):
    """約定履歴を照会する(R56)。**読み取り専用**。

    照会できなければ ``verified=False`` と空の ``fills`` を返し、約定を推測しない。
    呼び出し側(trade_journal)はこれを「今は記録できない」として扱い、次の
    サイクルで取り直す。
    """
    failures = []
    for adapter in adapters():
        method = getattr(adapter, "query_fills", None)
        if not callable(method):
            failures.append(f"{adapter.name}: fills query not supported")
            continue
        try:
            return method(account=account, scoped=scoped)
        except Unavailable as exc:
            failures.append(str(exc))
        except Exception as exc:
            failures.append(f"{adapter.name}: unexpected fills query error: {exc}")
    return {"verified": False, "source": "unavailable", "account": account, "fills": [],
            "observedAt": now_iso(),
            "detail": "; ".join(failures) or "no broker fills adapter is configured"}


def query_balances(accounts=None):
    """Return one independently verified balance snapshot per configured account."""
    scope = [str(value).strip() for value in (accounts or []) if str(value).strip()]
    return {account: query_balance(account=account) for account in scope}


def query_positions(symbol=DEFAULT_SYMBOL, accounts=None):
    """Return one independently verified position snapshot per configured account."""
    scope = [str(value).strip() for value in (accounts or []) if str(value).strip()]
    return {account: query_position(symbol, account=account) for account in scope}


def query_orders_by_account(symbol=DEFAULT_SYMBOL, accounts=None, known_order_ids=None):
    """Return one independently verified order snapshot per configured account."""
    scope = [str(value).strip() for value in (accounts or []) if str(value).strip()]
    ids = known_order_ids or {}
    return {account: query_orders(
        symbol,
        known_order_ids=(ids.get(account) if isinstance(ids, dict) else ids),
        account=account,
    ) for account in scope}


def query_accounts():
    """リンク済み口座を列挙する(R38)。読み取り専用。

    `query_position` と同じ規約: **失敗を「口座が無い」と誤解させない**。
    照会できなければ `verified=False` を返し、`accounts` は空のままにする。

    `adapter.configured` は口座IDの存在を要求するのでここでは使わない
    (口座一覧が要るのは、その口座IDがまだ分からないときだから)。
    """
    failures = []
    for adapter in adapters():
        method = getattr(adapter, "list_accounts", None)
        if not callable(method):
            failures.append(f"{adapter.name}: account listing not supported")
            continue
        try:
            rows = method()
        except Unavailable as exc:
            failures.append(str(exc))
            continue
        except Exception as exc:  # noqa: BLE001 — 想定外も「不明」に倒す
            failures.append(f"{adapter.name}: unexpected account query error: {exc}")
            continue
        return {
            "verified": True,
            "source": adapter.name,
            "observedAt": now_iso(),
            "accounts": rows,
            "usable": [row["id"] for row in rows if row.get("usable")],
        }
    return {
        "verified": False,
        "source": "unavailable",
        "observedAt": now_iso(),
        "accounts": [],
        "usable": [],
        "detail": "; ".join(failures) or "no broker account adapter is configured",
    }


def order_log_hint():
    """order_log の内容。**正本ではない**ので参考情報としてのみ返す。"""
    path = os.path.join(BASE, ".secrets", "order_log.json")
    try:
        with open(path, encoding="utf-8") as fh:
            log = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {"authoritative": False, "orders": []}
    latest_day = max(log.keys()) if log else None
    return {
        "authoritative": False,
        "note": "order_log は送信記録であり建玉ではない",
        "day": latest_day,
        "orders": log.get(latest_day, []) if latest_day else [],
    }


def main():
    parser = argparse.ArgumentParser(description="ブローカー建玉の読み取り専用照会")
    parser.add_argument("--symbol", default=DEFAULT_SYMBOL)
    parser.add_argument("--account", help="CROSSTRADE_ACCOUNTS 内の照会対象口座")
    parser.add_argument("--json", action="store_true", help="JSON で出力する")
    parser.add_argument("--check", action="store_true", help="どのアダプタが有効かを表示する")
    parser.add_argument("--accounts", action="store_true",
                        help="リンク済み口座を列挙する(読み取り専用)")
    args = parser.parse_args()

    if args.accounts:
        result = query_accounts()
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["verified"] else 1
        if not result["verified"]:
            print(f"UNVERIFIED — {result.get('detail')}")
            print("  → 口座が無いという意味ではありません。設定と接続を確認してください")
            return 1
        print(f"source: {result['source']}  at {result['observedAt']}")
        for row in result["accounts"]:
            mark = "OK  " if row.get("usable") else "NG  "
            extra = " / ".join(part for part in (
                f"accountId={row['accountId']}" if row.get("accountId") else "",
                f"env={row['environment']}" if row.get("environment") else "",
                f"status={row['status']}" if row.get("status") else "") if part)
            print(f"  {mark}{row.get('id') or '(ラベル不明)'}"
                  + (f"    {extra}" if extra else ""))
        print(f"\n発注先に書けるのは {len(result['usable'])} 口座:")
        for account in result["usable"]:
            print(f"  CROSSTRADE_ACCOUNTS に入れてよい: {account}")
        if not result["usable"]:
            print("  (なし。unknown_account 等は経路へ入れないこと)")
        return 0

    if args.check:
        print(f"symbol: {args.symbol}")
        for adapter in adapters():
            state = "configured" if getattr(adapter, "configured", False) else "not configured"
            print(f"  {adapter.name:<18} {state}")
        print("\n注意: order.py --status は送信記録であって建玉ではありません。")
        result = query_position(args.symbol, account=args.account)
        print(f"\n照会結果: verified={result['verified']} source={result['source']}")
        if not result["verified"]:
            print(f"  理由: {result.get('detail')}")
            print("  → ポジションは消さず STALE として扱われます")
        return 0

    result = query_position(args.symbol, account=args.account)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if not result["verified"]:
        print(f"UNVERIFIED — {result.get('detail')}")
        return 1
    if result.get("qty", 0) == 0:
        print(f"FLAT ({result['source']}) at {result['observedAt']}")
        return 0
    print(f"{result['side']} {result['qty']} @ {result.get('avgEntry')} ({result['source']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())

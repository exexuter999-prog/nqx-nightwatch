"""Canonical R18 live ENTRY intent shared by every Python producer/consumer.

Prices are encoded as fixed two-decimal strings before hashing.  This avoids
Python/JavaScript integer-vs-float JSON differences while preserving MNQ's
quarter-tick contract.  Unknown keys and lossy defaults are rejected.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any, Dict, Iterable, Optional

import execution_contract

INTENT_VERSION = str(execution_contract.CONTRACT["intent"]["version"])
INTENT_FIELDS = {
    "version", "symbol", "side", "qty", "orderType", "entry", "last",
    "stop", "targets", "legs", "planVersion", "executionContractVersion",
    "accountScope",
}
#: R84: 追撃(pyramid)だけが載せる追加フィールド。**通常経路の intent は 1 バイトも
#: 変わらない**(キーが無ければ出力にも現れないので hash も従来どおり)。追撃である
#: ことを intent hash に固定し、order.py の consume 検証と Worker の再検証に乗せる。
PYRAMID_FIELD = "pyramid"
PYRAMID_SUBFIELDS = {"baseQty", "addQty"}
ACCOUNT_RE = re.compile(r"^[A-Za-z0-9_-]{3,64}$")


def _tick_text(value: Any, field: str) -> str:
    if value is None or isinstance(value, bool):
        raise ValueError(f"{field} is required")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be numeric") from exc
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{field} must be a positive finite price")
    ticks = round(number * 4)
    if abs(number * 4 - ticks) > 1e-8:
        raise ValueError(f"{field} must be aligned to 0.25 tick")
    return f"{ticks / 4:.2f}"


def normalize_account_scope(values: Iterable[Any]) -> list[str]:
    if isinstance(values, (str, bytes)):
        values = re.split(r"[,\n]+", str(values))
    raw_accounts = [str(value).strip() for value in values]
    if (not raw_accounts or any(not account for account in raw_accounts)
            or len(set(raw_accounts)) != len(raw_accounts)):
        raise ValueError("accountScope must contain unique explicit account ids")
    accounts = sorted(raw_accounts)
    if any(not ACCOUNT_RE.fullmatch(account) for account in accounts):
        raise ValueError("accountScope must contain explicit valid account ids")
    # maxAccounts: null = 上限なし(明示的な撤廃)。キー自体が無い/壊れている場合は
    # 従来どおり 1 に倒す —— 「読めなかった」と「無制限と決めた」を混ぜない。
    limit = execution_contract.CONTRACT.get("accountMode", {}).get("maxAccounts", 1)
    if limit is not None and len(accounts) > int(limit):
        raise ValueError("accountScope exceeds execution contract account limit")
    return accounts


def account_scope_from_config(cfg: Optional[Dict[str, Any]]) -> list[str]:
    cfg = cfg or {}
    raw = cfg.get("CROSSTRADE_ACCOUNTS") or cfg.get("CROSSTRADE_ACCOUNT") or ""
    return normalize_account_scope(
        [value for value in re.split(r"[,\n]+", str(raw)) if value.strip()])


def normalize_pyramid(raw: Any) -> Optional[Dict[str, int]]:
    """R84: ``{"baseQty": int>0, "addQty": int>0}`` だけを受ける。None は「追撃ではない」。"""
    if raw is None:
        return None
    if not isinstance(raw, dict) or set(raw) != PYRAMID_SUBFIELDS:
        raise ValueError("pyramid fields are invalid")
    values = {}
    for field in sorted(PYRAMID_SUBFIELDS):
        value = raw.get(field)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"pyramid.{field} must be an integer")
        if isinstance(value, float) and not value.is_integer():
            raise ValueError(f"pyramid.{field} must be an integer")
        number = int(value)
        if number <= 0:
            raise ValueError(f"pyramid.{field} must be positive")
        values[field] = number
    return values


def build(*, symbol: Any, side: Any, qty: Any, order_type: Any,
          entry: Any, last: Any, stop: Any, targets: Any, legs: Any,
          plan_version: Any, execution_contract_version: Any,
          account_scope: Iterable[Any], pyramid: Any = None) -> Dict[str, Any]:
    symbol = str(symbol or "").strip()
    side = str(side or "").upper()
    order_type = str(order_type or "").upper()
    if not symbol or len(symbol) > 32:
        raise ValueError("symbol is invalid")
    if side not in {"BUY", "SELL"}:
        raise ValueError("side must be BUY or SELL")
    try:
        qty = int(qty)
    except (TypeError, ValueError) as exc:
        raise ValueError("qty must be an integer") from exc
    # 通常経路は固定枚数のまま。ULTRA が契約で有効なときだけ、その口座あたり
    # 上限までを受け入れる。どちらの場合も枚数から脚配分が一意に決まるので、
    # intent hash は決定論のままになる。
    fixed_qty = int(execution_contract.CONTRACT["risk"]["fixedQty"])
    ultra_envelope = execution_contract.CONTRACT.get("ultra") or {}
    if qty != fixed_qty:
        if not ultra_envelope.get("enabled"):
            raise ValueError("FIXED_QTY_REQUIRED")
        if qty < int(ultra_envelope["minQtyPerAccount"]):
            raise ValueError("ULTRA_QTY_BELOW_MIN")
        if qty > int(ultra_envelope["maxQtyPerAccount"]):
            raise ValueError("ULTRA_QTY_EXCEEDS_ACCOUNT_MAX")
    if order_type not in {"LIMIT", "MARKET"}:
        raise ValueError("orderType must be LIMIT or MARKET")

    normalized_entry = _tick_text(entry, "entry") if order_type == "LIMIT" else None
    normalized_last = _tick_text(last, "last") if order_type == "MARKET" else None
    if order_type == "LIMIT" and last is not None:
        raise ValueError("LIMIT intent must not carry last")
    if order_type == "MARKET" and entry is not None:
        raise ValueError("MARKET intent must not carry entry")
    normalized_stop = _tick_text(stop, "stop")
    if not isinstance(targets, list) or len(targets) != 2:
        raise ValueError("SPLIT_PLAN_REQUIRED")
    normalized_targets = [_tick_text(value, f"targets[{index}]")
                          for index, value in enumerate(targets)]
    basis = float(normalized_entry if order_type == "LIMIT" else normalized_last)
    stop_number = float(normalized_stop)
    target_numbers = [float(value) for value in normalized_targets]
    directional = ((side == "BUY" and stop_number < basis < target_numbers[0] < target_numbers[1])
                   or (side == "SELL" and target_numbers[1] < target_numbers[0] < basis < stop_number))
    if not directional:
        raise ValueError("execution intent prices are not directional")
    # 脚の枚数は総枚数から一意に決まる(呼び出し側の申告は採用しない)。
    # 2枚固定なら 1/1、ULTRA の比率分割なら端数を runner へ寄せた配分。
    if qty == fixed_qty:
        leg_quantities = [1, 1]
    else:
        leg_quantities = execution_contract.ultra_split(qty)
        if leg_quantities is None:
            raise ValueError("ULTRA_SPLIT_NOT_REPRESENTABLE")
    expected_legs = [
        {"id": "TP1", "qty": leg_quantities[0], "target": normalized_targets[0]},
        {"id": "RUNNER", "qty": leg_quantities[1], "target": normalized_targets[1]},
    ]
    if not isinstance(legs, list) or len(legs) != 2:
        raise ValueError("SPLIT_LEGS_INVALID")
    normalized_legs = []
    for index, leg in enumerate(legs):
        if not isinstance(leg, dict):
            raise ValueError("SPLIT_LEGS_INVALID")
        normalized_legs.append({
            "id": str(leg.get("id") or ""),
            "qty": int(leg.get("qty") or 0),
            "target": _tick_text(leg.get("target"), f"legs[{index}].target"),
        })
    if normalized_legs != expected_legs:
        raise ValueError("SPLIT_LEGS_INVALID")
    plan_version = str(plan_version or "").strip()
    if not plan_version or len(plan_version) > 64:
        raise ValueError("planVersion is invalid")
    contract_version = str(execution_contract_version or "").strip()
    if contract_version != execution_contract.VERSION:
        raise ValueError("EXECUTION_CONTRACT_VERSION_MISMATCH")

    normalized_pyramid = normalize_pyramid(pyramid)
    if normalized_pyramid is not None:
        # 追撃の合計建玉も口座あたり上限を超えてはならない。ここは **枚数だけ**の
        # 検査で、金額の合計リスクは pyramid.combined_risk が order.py / engine /
        # Worker の三か所で同一算術として見る。
        max_account_qty = int(ultra_envelope.get("maxQtyPerAccount") or 0)
        if not ultra_envelope.get("enabled"):
            raise ValueError("PYRAMID_REQUIRES_ULTRA")
        if order_type != "MARKET":
            raise ValueError("PYRAMID_MARKET_ONLY")
        if normalized_pyramid["addQty"] != qty:
            raise ValueError("PYRAMID_ADD_QTY_MISMATCH")
        if max_account_qty and normalized_pyramid["baseQty"] + qty > max_account_qty:
            raise ValueError("PYRAMID_TOTAL_QTY_EXCEEDS_ACCOUNT_MAX")
    intent = {
        "version": INTENT_VERSION,
        "symbol": symbol,
        "side": side,
        "qty": qty,
        "orderType": order_type,
        "entry": normalized_entry,
        "last": normalized_last,
        "stop": normalized_stop,
        "targets": normalized_targets,
        "legs": expected_legs,
        "planVersion": plan_version,
        "executionContractVersion": contract_version,
        "accountScope": normalize_account_scope(account_scope),
    }
    if normalized_pyramid is not None:
        intent[PYRAMID_FIELD] = normalized_pyramid
    return intent


def from_scenario(scenario: Dict[str, Any], market: Optional[Dict[str, Any]] = None,
                  *, order_type: str = "LIMIT",
                  account_scope: Optional[Iterable[Any]] = None) -> Dict[str, Any]:
    if not isinstance(scenario, dict):
        raise ValueError("scenario is required")
    order_type = str(order_type or "").upper()
    market = market if isinstance(market, dict) else {}
    if order_type == "MARKET" and market.get("verified") is not True:
        raise ValueError("MARKET intent requires verified current market")
    frozen_contract = scenario.get("executionContract")
    frozen_scope = (frozen_contract or {}).get("accountScope") if isinstance(frozen_contract, dict) else None
    scope = account_scope if account_scope is not None else frozen_scope
    return build(
        symbol=scenario.get("symbol"), side=scenario.get("side"), qty=scenario.get("qty"),
        order_type=order_type,
        entry=scenario.get("entry") if order_type == "LIMIT" else None,
        last=market.get("price") if order_type == "MARKET" else None,
        stop=scenario.get("stop"), targets=scenario.get("targets"), legs=scenario.get("legs"),
        plan_version=scenario.get("planVersion"),
        execution_contract_version=scenario.get("executionContractVersion"),
        account_scope=scope or [],
    )


def normalize(raw: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("executionIntent fields are invalid")
    # R84: `pyramid` だけが任意キー。無い intent の受理条件は従来と 1 文字も変えない。
    if set(raw) != INTENT_FIELDS and set(raw) != INTENT_FIELDS | {PYRAMID_FIELD}:
        raise ValueError("executionIntent fields are invalid")
    return build(
        symbol=raw.get("symbol"), side=raw.get("side"), qty=raw.get("qty"),
        order_type=raw.get("orderType"), entry=raw.get("entry"), last=raw.get("last"),
        stop=raw.get("stop"), targets=raw.get("targets"), legs=raw.get("legs"),
        plan_version=raw.get("planVersion"),
        execution_contract_version=raw.get("executionContractVersion"),
        account_scope=raw.get("accountScope") or [],
        pyramid=raw.get(PYRAMID_FIELD),
    )


def canonical_bytes(raw: Dict[str, Any]) -> bytes:
    normalized = normalize(raw)
    return json.dumps(normalized, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def intent_hash(raw: Dict[str, Any]) -> str:
    return "xi_" + hashlib.sha256(canonical_bytes(raw)).hexdigest()

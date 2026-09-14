"""Exact R19 protective-management intent shared by Bot/AUTO/order.py.

This module is pure.  It never queries the broker or sends an order.  The
caller must supply the broker-verified position generation and account.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any, Dict

import execution_contract

VERSION = str(execution_contract.CONTRACT["management"]["version"])
FIELDS = {"version", "accountId", "symbol", "positionGeneration", "action",
          "side", "qty", "stop", "target", "executionContractVersion"}


def _price(value: Any, field: str) -> str:
    if value is None or isinstance(value, bool):
        raise ValueError(f"{field} is required")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be numeric") from exc
    if not math.isfinite(number) or number <= 0 or abs(number * 4 - round(number * 4)) > 1e-8:
        raise ValueError(f"{field} must be a positive 0.25-tick price")
    return f"{round(number * 4) / 4:.2f}"


def build(*, account_id: Any, symbol: Any, position_generation: Any,
          side: Any, qty: Any, stop: Any, target: Any,
          action: str = "MODIFY") -> Dict[str, Any]:
    account = str(account_id or "").strip()
    symbol = str(symbol or "").strip()
    generation = str(position_generation or "").strip()
    side = str(side or "").upper()
    action = str(action or "").upper()
    if not re.fullmatch(r"[A-Za-z0-9_-]{3,64}", account):
        raise ValueError("accountId is invalid")
    if not symbol or len(symbol) > 32 or not generation or len(generation) > 160:
        raise ValueError("symbol/positionGeneration is invalid")
    if action != "MODIFY" or side not in {"BUY", "SELL"}:
        raise ValueError("only BUY/SELL MODIFY is supported")
    try:
        quantity = int(qty)
    except (TypeError, ValueError) as exc:
        raise ValueError("qty must be an integer") from exc
    # R52: ULTRA の runner は 1 枚ではない(例 6)。上限は ULTRA エンベロープの口座別
    # 最大枚数(契約で ULTRA が無効なら従来の固定枚数)。Worker の
    # normalizeManagementIntent と同じ規則(intent hash の同一性に関わる)。
    ultra_env = execution_contract.CONTRACT.get("ultra") or {}
    ceiling = int(execution_contract.CONTRACT["risk"]["fixedQty"])
    if ultra_env.get("enabled"):
        ceiling = max(ceiling, int(ultra_env.get("maxQtyPerAccount") or ceiling))
    if quantity < 1 or quantity > ceiling:
        raise ValueError("management qty is invalid")
    return {
        "version": VERSION, "accountId": account, "symbol": symbol,
        "positionGeneration": generation, "action": action, "side": side,
        "qty": quantity, "stop": _price(stop, "stop"),
        "target": _price(target, "target"),
        "executionContractVersion": execution_contract.VERSION,
    }


def normalize(raw: Any) -> Dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) != FIELDS:
        raise ValueError("management intent fields are invalid")
    if str(raw.get("version") or "") != VERSION:
        raise ValueError("management intent version mismatch")
    if str(raw.get("executionContractVersion") or "") != execution_contract.VERSION:
        raise ValueError("execution contract version mismatch")
    return build(account_id=raw.get("accountId"), symbol=raw.get("symbol"),
                 position_generation=raw.get("positionGeneration"),
                 action=raw.get("action"), side=raw.get("side"), qty=raw.get("qty"),
                 stop=raw.get("stop"), target=raw.get("target"))


def canonical_bytes(raw: Any) -> bytes:
    return json.dumps(normalize(raw), ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def intent_hash(raw: Any) -> str:
    return "mi_" + hashlib.sha256(canonical_bytes(raw)).hexdigest()


def management_key(raw: Any) -> str:
    return "MANAGEMENT:" + hashlib.sha256(canonical_bytes(raw)).hexdigest()

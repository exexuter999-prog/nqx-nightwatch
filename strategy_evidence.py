#!/usr/bin/env python3
"""Canonical, bounded ICT/strategy evidence envelope (no I/O)."""
from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from typing import Any, Dict, Optional

VERSION = "R14-STRATEGY-EVIDENCE-1"
MAX_BYTES = 24 * 1024
MAX_DEPTH = 6
MAX_KEYS = 32
MAX_ARRAY = 16
TICK = 0.25
TOP_LEVEL_KEYS = frozenset({
    "version", "asOf", "sessionId", "source", "provenance", "models", "evidenceHash",
})
# R39: 価格キーを「取引価格」と「統計・派生価格」に分ける。
#
# 以前はこの集合すべてに 0.25 ティック整合を要求していた。ところが VP の
# POC/VAH/VAL、VWAP、ゾーン中点 (lo+hi)/2、50% ウィック、フィボ retracement は
# **定義上ティック格子に乗らない**。VP レベルを 1 つ読んだだけで
# `canonicalize()` が ValueError になり、`market.strategyEvidence` が None に
# 落ち、`_authoritative_cycle_seal()` が毎サイクル
# 「CYCLE_MISMATCH canonical market evidence unavailable」で止まっていた。
# つまり **自律 ENTRY は構造的に一度も成立し得なかった**。
#
# 緩めても発注の安全性は落ちない。実際に建玉になる価格
# (scenario.entry/stop/targets、market.price、market.bars) は
# `nqx_state.build_scenario` / `execution_contract.evaluate` /
# `build_market_payload` が別途ティック検証しており、evidence はそこを通らない。
TICK_PRICE_KEYS = {
    "lo", "hi", "low", "high", "target", "entry", "stop",
    "rangehigh", "rangelow", "asianhigh", "asianlow", "midnightopen",
}
DERIVED_PRICE_KEYS = {
    "price", "mid", "vwap", "nearest", "wick50", "poc", "vah", "val",
    "ote", "otebuy", "otesell",
}
PRICE_KEYS = TICK_PRICE_KEYS | DERIVED_PRICE_KEYS


def _iso(value: Any) -> Optional[str]:
    if not value:
        return None
    text = str(value)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc).isoformat()


def _is_tick(value: float) -> bool:
    return abs(value / TICK - round(value / TICK)) < 1e-8


def _clean(value: Any, key: str = "", depth: int = 0) -> Any:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        if len(value) > 160:
            raise ValueError(f"strategy evidence {key} exceeds text limit")
        return value
    if isinstance(value, (int, float)):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError(f"strategy evidence {key} must be finite")
        if key.lower() in TICK_PRICE_KEYS and not _is_tick(number):
            raise ValueError(f"strategy evidence {key} is not aligned to the 0.25 tick")
        return value
    if depth >= MAX_DEPTH:
        raise ValueError("strategy evidence exceeds maximum depth")
    if isinstance(value, list):
        if len(value) > MAX_ARRAY:
            raise ValueError("strategy evidence exceeds array limit")
        return [_clean(item, key, depth + 1) for item in value]
    if not isinstance(value, dict):
        raise ValueError(f"strategy evidence {key} has unsupported type")
    if len(value) > MAX_KEYS:
        raise ValueError("strategy evidence exceeds key limit")
    out = {}
    for name, item in value.items():
        text = str(name)
        if len(text) > 64:
            raise ValueError("strategy evidence exceeds key length limit")
        out[text] = _clean(item, text, depth + 1)
    return out


def canonical_bytes(value: Dict[str, Any]) -> bytes:
    """Bytes shared by producer, ingress validation, and OOS hash checks."""
    def normalize_numbers(item: Any) -> Any:
        if isinstance(item, float) and item.is_integer():
            return int(item)
        if isinstance(item, list):
            return [normalize_numbers(child) for child in item]
        if isinstance(item, dict):
            return {key: normalize_numbers(child) for key, child in item.items()}
        return item
    material = normalize_numbers({key: value[key] for key in (
        "version", "asOf", "sessionId", "source", "provenance", "models")})
    return json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def evidence_hash(value: Dict[str, Any]) -> str:
    return "se_" + hashlib.sha256(canonical_bytes(value)).hexdigest()[:24]


def canonicalize(raw: Any, *, as_of: Any = None, session_id: Any = None,
                 source: Any = "monitor", provenance: Any = "observed") -> Optional[Dict[str, Any]]:
    """Return the one allowed strategy-evidence shape or ``None``.

    The caller intentionally treats malformed evidence as absent evidence; it
    must not drop the independently validated market observation.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("strategy evidence must be an object")
    unknown = set(raw) - TOP_LEVEL_KEYS
    if unknown:
        raise ValueError("strategy evidence contains unknown top-level keys")
    models = raw.get("models")
    if not isinstance(models, dict):
        raise ValueError("strategy evidence.models is required")
    def _field(value: Any, fallback: Any, name: str, limit: int = 96) -> Optional[str]:
        if value not in (None, "") and not isinstance(value, str):
            raise ValueError(f"strategy evidence {name} must be text")
        text = str(value if value not in (None, "") else fallback or "")
        if len(text) > limit:
            raise ValueError(f"strategy evidence {name} exceeds text limit")
        return text or None
    canonical = {
        "version": _field(raw.get("version"), VERSION, "version", 64),
        "asOf": _iso(raw.get("asOf") or as_of),
        "sessionId": _field(raw.get("sessionId"), session_id, "sessionId"),
        "source": _field(raw.get("source"), source, "source"),
        "provenance": _field(raw.get("provenance"), provenance, "provenance"),
        "models": _clean(models, "models"),
    }
    if canonical["version"] != VERSION:
        raise ValueError("strategy evidence version is unsupported")
    if canonical["asOf"] is None or canonical["sessionId"] is None or not canonical["source"] or not canonical["provenance"]:
        raise ValueError("strategy evidence requires version/asOf/sessionId/source/provenance")
    material = canonical_bytes(canonical)
    if len(material) > MAX_BYTES:
        raise ValueError("strategy evidence exceeds byte limit")
    computed = "se_" + hashlib.sha256(material).hexdigest()[:24]
    supplied_hash = raw.get("evidenceHash")
    if supplied_hash is not None and str(supplied_hash) != computed:
        raise ValueError("strategy evidence evidenceHash does not match canonical bytes")
    canonical["evidenceHash"] = computed
    return canonical


def from_matrix(matrix: Any, *, as_of: Any, session_id: Any, source: str = "monitor",
                provenance: str = "observed") -> Optional[Dict[str, Any]]:
    if not isinstance(matrix, dict):
        return canonicalize({"models": None}, as_of=as_of, session_id=session_id,
                            source=source, provenance=provenance)
    models = matrix.get("models")
    if not isinstance(models, dict):
        return canonicalize({"models": None}, as_of=as_of, session_id=session_id,
                            source=source, provenance=provenance)
    # The transport has a single `models` container.  Matrix-level UI/audit
    # metadata is held under a reserved non-voting key instead of being copied
    # into a second strategyMatrix field on the wire.
    canonical_models = dict(models)
    canonical_models["_matrix"] = {
        "catalogVersion": matrix.get("version"),
        "alignment": matrix.get("alignment") or {},
        "activeModels": matrix.get("activeModels") or [],
        "detectorVersion": matrix.get("detectorVersion") or "REPO2-DETECTOR/1",
    }
    return canonicalize({"models": canonical_models}, as_of=as_of, session_id=session_id,
                        source=source, provenance=provenance)

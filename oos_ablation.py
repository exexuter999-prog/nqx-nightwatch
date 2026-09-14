#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""R12 ICT要素の時系列OOSアブレーション集計。

このツールは価格を再最適化・再約定しない。各行に保存済みの同一 entry/stop/
exitSpec と実際の outcome を読み、MSNRからCVDまでを**フィルタとしてだけ**
足し込む。結果が無い行やコスト不明な行を推測で埋めず、入力エラーにする。
"""
from __future__ import annotations

import argparse
import copy
import json
import math
import sys
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

import strategy_evidence


STAGES = OrderedDict([
    ("MSNR", "MSNRのみ"),
    ("PD", "+PD"),
    ("DOL", "+DOL"),
    ("FVG", "+FVG"),
    ("SMT", "+SMT"),
    ("KILLZONE", "+Killzone"),
    ("CVD", "+CVD"),
    ("IFVG", "+IFVG"),
    ("BLOCKS", "+Blocks"),
    ("QUARTERLY", "+Quarterly"),
    ("SESSIONS", "+Sessions"),
    ("FIB_SD", "+FibSD"),
    ("FIB_CRT", "+FibCRT"),
])

VERSION_FIELDS = ("setupVersion", "catalogVersion", "detectorVersion", "executionContractVersion")


def _number(value: Any, name: str) -> float:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{name} is required")
    try:
        out = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be numeric") from exc
    if not math.isfinite(out):
        raise ValueError(f"{name} must be finite")
    return out


def _at(value: Any) -> datetime:
    if not value:
        raise ValueError("at is required")
    text = str(value)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        raise ValueError("at must include timezone")
    return parsed.astimezone(timezone.utc)


def _feature(record: Dict[str, Any], key: str) -> bool:
    decision = record.get("decision") if isinstance(record.get("decision"), dict) else {}
    evidence = set(decision.get("evidence") or [])
    side = decision.get("side")
    if key == "MSNR":
        return bool(decision.get("msnrComplete") is True or decision.get("model") in {
            "VP80_REVERSION", "TURTLE_SOUP_REVERSAL", "BREAKER_CONTINUATION", "OTE_FVG_PULLBACK"})
    if key == "PD":
        return "ICT_LOCATION" in evidence or bool((decision.get("rangeAnchor") or {}).get("valid"))
    if key == "DOL":
        labels = decision.get("targetLabels") or []
        return any("DOL" in str(label).upper() for label in labels)
    if key == "FVG":
        fvg = decision.get("fvg") or {}
        return bool(fvg.get("eligible") and fvg.get("preArrivalStructure") == "INTACT")
    if key == "SMT":
        smt = decision.get("smt") or {}
        want = "BULLISH" if side == "BUY" else "BEARISH"
        return bool(smt.get("available") and smt.get("freshness") == "FRESH" and smt.get("bias") == want)
    if key == "KILLZONE":
        return bool((decision.get("ictSession") or {}).get("tradeable") is True)
    if key == "CVD":
        health = decision.get("cvdHealth") or {}
        return bool(health.get("available") and health.get("freshness") == "FRESH"
                    and "CVD_ALIGNED" in evidence)
    evidence_models = decision.get("strategyEvidence") or record.get("strategyEvidence") or {}
    models = evidence_models.get("models") if isinstance(evidence_models, dict) else {}
    model_name = {"IFVG": "ifvg", "BLOCKS": "blocks", "QUARTERLY": "quarterly", "SESSIONS": "sessions",
                  "FIB_SD": "fibSd", "FIB_CRT": "fibCrt"}.get(key)
    model = models.get(model_name) if isinstance(models, dict) else {}
    return bool(isinstance(model, dict) and model.get("eligible") is True)
    return False


def normalize(record: Dict[str, Any], line: int, *, legacy_mode: bool = False) -> Dict[str, Any]:
    if not isinstance(record, dict):
        raise ValueError(f"line {line}: record must be an object")
    frozen = copy.deepcopy(record)
    outcome = frozen.get("outcome")
    if not isinstance(outcome, dict):
        raise ValueError(f"line {line}: outcome is required; OOS results must not be simulated")
    for field in ("entry", "stop", "exitSpec"):
        if frozen.get(field) is None:
            raise ValueError(f"line {line}: {field} is required to prove a fixed execution contract")
    filled = outcome.get("filled")
    if not isinstance(filled, bool):
        raise ValueError(f"line {line}: outcome.filled must be boolean")
    present_versions = [field for field in VERSION_FIELDS if frozen.get(field) is not None]
    if present_versions and len(present_versions) != len(VERSION_FIELDS):
        raise ValueError(f"line {line}: immutable version set is incomplete")
    if not present_versions and not legacy_mode:
        raise ValueError(f"line {line}: R14 OOS requires complete versions; use legacy_mode explicitly")
    versions = ({field: str(frozen[field]) for field in VERSION_FIELDS}
                if present_versions else {field: "R12-LEGACY" for field in VERSION_FIELDS})
    evidence = frozen.get("strategyEvidence") if isinstance(frozen.get("strategyEvidence"), dict) else {}
    evidence_hash = frozen.get("evidenceHash") if present_versions else (frozen.get("evidenceHash") or evidence.get("evidenceHash"))
    if present_versions:
        if not evidence_hash:
            raise ValueError(f"line {line}: evidenceHash is required for versioned OOS")
        if str(evidence.get("evidenceHash") or "") != str(evidence_hash):
            raise ValueError(f"line {line}: top evidenceHash must match frozen strategyEvidence")
        if "features" in frozen:
            raise ValueError(f"line {line}: versioned OOS features must derive from frozen decision/evidence")
        try:
            canonical = strategy_evidence.canonicalize(evidence)
        except ValueError as exc:
            raise ValueError(f"line {line}: strategyEvidence is invalid: {exc}") from exc
        if canonical is None or str(evidence_hash) != canonical["evidenceHash"]:
            raise ValueError(f"line {line}: evidenceHash does not match canonical strategyEvidence")
    decision = frozen.get("decision") if isinstance(frozen.get("decision"), dict) else {}
    if present_versions:
        for field in ("regime", "sessionId", "direction"):
            if frozen.get(field) in (None, ""):
                raise ValueError(f"line {line}: versioned OOS {field} is required")
    legacy_features = frozen.get("features") if not present_versions else None
    derived_features = ({key: (legacy_features.get(key) is True) for key in STAGES}
                        if isinstance(legacy_features, dict)
                        else {key: _feature(frozen, key) for key in STAGES})
    normal = {"at": _at(frozen.get("at")), "features": derived_features,
              "filled": filled, "line": line, "versions": versions, "evidenceHash": evidence_hash,
              "setup": versions["setupVersion"], "regime": str(frozen.get("regime") or decision.get("regime") or "MX"),
              "session": str(frozen.get("sessionId") or decision.get("sessionId") or "UNKNOWN"),
              "direction": str(frozen.get("direction") or decision.get("side") or "UNKNOWN"),
              "entry": frozen.get("entry"), "stop": frozen.get("stop"), "exitSpec": frozen.get("exitSpec")}
    if filled:
        normal.update({
            "realizedR": _number(outcome.get("realizedR"), f"line {line}: outcome.realizedR"),
            "costR": _number(outcome.get("costR"), f"line {line}: outcome.costR"),
            "mfeR": _number(outcome.get("mfeR"), f"line {line}: outcome.mfeR"),
            "maeR": _number(outcome.get("maeR"), f"line {line}: outcome.maeR"),
        })
    return normal


def _mean(values: Iterable[float]) -> float | None:
    values = list(values)
    return (sum(values) / len(values)) if values else None


def metrics(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    result = []
    required = []
    for key, label in STAGES.items():
        required.append(key)
        selected = [row for row in rows if all(row["features"][feature] for feature in required)]
        filled = [row for row in selected if row["filled"]]
        net = [row["realizedR"] - row["costR"] for row in filled]
        result.append({
            "stage": key, "label": label, "signals": len(selected), "fills": len(filled),
            "fillRate": (len(filled) / len(selected)) if selected else None,
            "wins": sum(value > 0 for value in net),
            "winRate": (sum(value > 0 for value in net) / len(filled)) if filled else None,
            "realizedR": _mean(row["realizedR"] for row in filled),
            "mfeR": _mean(row["mfeR"] for row in filled),
            "maeR": _mean(row["maeR"] for row in filled),
            "totalNetR": sum(net),
            "expectancyAfterCostPerFillR": _mean(net),
            "expectancyAfterCostPerSignalR": (sum(net) / len(selected)) if selected else None,
        })
    return result


def grouped_metrics(rows: List[Dict[str, Any]], *, partition_versions: bool = False) -> List[Dict[str, Any]]:
    """Never pool setup/regime/session/direction or mixed immutable versions."""
    identities = {tuple(sorted(row["versions"].items())) for row in rows}
    if len(identities) > 1 and not partition_versions:
        raise ValueError("mixed immutable version tuples require explicit partition_versions")
    groups: Dict[Tuple[str, str, str, str, Tuple[Tuple[str, str], ...]], List[Dict[str, Any]]] = {}
    for row in rows:
        identity = tuple(sorted(row["versions"].items()))
        key = (row["setup"], row["regime"], row["session"], row["direction"], identity)
        groups.setdefault(key, []).append(row)
    out = []
    for (setup, regime, session, direction, identity), group in sorted(groups.items()):
        out.append({"setupVersion": setup, "regime": regime, "session": session,
                    "direction": direction, "versions": dict(identity), "stages": metrics(group)})
    return out


def load(path: Path, oos_start: datetime | None, *, legacy_mode: bool = False) -> Tuple[List[Dict[str, Any]], int]:
    rows = []
    excluded = 0
    with path.open(encoding="utf-8") as fh:
        for line_no, raw in enumerate(fh, 1):
            if not raw.strip():
                continue
            try:
                row = normalize(json.loads(raw), line_no, legacy_mode=legacy_mode)
            except (json.JSONDecodeError, ValueError) as exc:
                raise ValueError(str(exc)) from exc
            if oos_start and row["at"] < oos_start:
                excluded += 1
                continue
            rows.append(row)
    if not rows:
        raise ValueError("no OOS records after the requested split")
    return rows, excluded


def main() -> None:
    parser = argparse.ArgumentParser(description="R12 ICT OOS ablation; reads actual outcome JSONL only")
    parser.add_argument("--input", required=True, type=Path, help="監査済みtrade outcome JSONL")
    parser.add_argument("--oos-start", help="このISO時刻より前を除外する(タイムゾーン必須)")
    parser.add_argument("--output", type=Path, help="結果JSONの保存先。省略時はstdout")
    parser.add_argument("--legacy-mode", action="store_true",
                        help="explicitly isolate pre-R14 records in a legacy dataset")
    parser.add_argument("--partition-versions", action="store_true",
                        help="explicitly partition mixed immutable versions")
    args = parser.parse_args()
    try:
        start = _at(args.oos_start) if args.oos_start else None
        rows, excluded = load(args.input, start, legacy_mode=args.legacy_mode)
        output = {"protocol": "R14-ICT-ABLATION-OOS-1", "input": str(args.input),
                  "oosStart": start.isoformat() if start else None,
                  "records": len(rows), "excludedPreOOS": excluded,
                  "stagesNonAuthoritative": {"authoritative": False,
                      "reason": "use setup×regime×session×direction×version groups", "metrics": metrics(rows)},
                  "groups": grouped_metrics(rows, partition_versions=args.partition_versions),
                  "legacyMode": bool(args.legacy_mode), "partitionVersions": bool(args.partition_versions)}
    except (OSError, ValueError) as exc:
        raise SystemExit(f"ERROR: {exc}")
    text = json.dumps(output, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)


if __name__ == "__main__":
    main()

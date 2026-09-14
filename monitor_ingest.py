#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""R13 safe stdin -> file-provider bridge.

The interactive TradingView controller writes observed JSON here through stdin.
This module is deliberately narrower than ``monitor_pipeline``: it performs no
market evaluation, no network access, and no execution routing.  It only
validates the R13 config capability boundary and atomically places a snapshot
or a CVD-only retry result where the pipeline expects it.

Examples (PowerShell):

    $json | python monitor_ingest.py --config monitor_config.json --kind snapshot
    $cvd  | python monitor_ingest.py --config monitor_config.json --kind cvd-retry
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path
from typing import Any, Dict, Iterable

import monitor_pipeline


CVD_RETRY_FIELDS = {
    "cvd", "cvdMeta", "cvdAt", "cvdSource", "cvdAttempts", "cvdHistory",
}


def _parse_object(raw: bytes) -> Dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise monitor_pipeline.PipelineError(f"stdin must be UTF-8 JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise monitor_pipeline.PipelineError("stdin JSON must be an object")
    return value


def _cvd_retry_only(value: Dict[str, Any]) -> Dict[str, Any]:
    """Reject structural fields so a retry cannot replace the frozen snapshot."""
    forbidden = sorted(set(value) - CVD_RETRY_FIELDS)
    if forbidden:
        raise monitor_pipeline.PipelineError(
            "cvd-retry accepts CVD fields only; rejected: " + ", ".join(forbidden))
    if "cvd" not in value and "cvdMeta" not in value:
        raise monitor_pipeline.PipelineError("cvd-retry requires cvd or cvdMeta")
    meta = copy.deepcopy(value.get("cvdMeta")) if isinstance(value.get("cvdMeta"), dict) else {}
    # Accept the compact CVD telemetry spelling but write one canonical payload
    # consumed by monitor_pipeline._retry_cvd().
    if "cvdAt" in value and "at" not in meta:
        meta["at"] = value["cvdAt"]
    if "cvdSource" in value and "provider" not in meta:
        meta["provider"] = value["cvdSource"]
    if "cvdAttempts" in value and "attempts" not in meta:
        meta["attempts"] = value["cvdAttempts"]
    if "cvdHistory" in value and "history" not in meta:
        meta["history"] = value["cvdHistory"]
    return {"cvd": copy.deepcopy(value.get("cvd")), "cvdMeta": meta}


def ingest(config_path: str | Path, kind: str, raw: bytes) -> Dict[str, Any]:
    """Atomically ingest one provider handoff and return its audit receipt."""
    cfg = monitor_pipeline.load_config(config_path)
    value = _parse_object(raw)
    stale_retry_cleared = False
    if kind == "snapshot":
        target = cfg["provider"]["snapshotInputPath"]
        payload = value
        # R39: 新しいスナップショットは、前サイクルの CVD 再取得を無効にする。
        # 残したままだと monitor_pipeline が古い CVD を再消費して attempts=2 に
        # 進め、そのサイクル本来の再取得枠を黙って使い切っていた。1 スナップ
        # ショットにつき再取得は最大 1 回、という不変条件をここで担保する。
        retry_path = cfg["provider"].get("cvdRetryInputPath")
        if retry_path is not None:
            try:
                Path(retry_path).unlink()
                stale_retry_cleared = True
            except FileNotFoundError:
                pass
            except OSError as exc:
                raise monitor_pipeline.PipelineError(
                    f"stale CVD retry input could not be cleared: {exc}") from exc
    elif kind == "cvd-retry":
        target = cfg["provider"].get("cvdRetryInputPath")
        if target is None:
            raise monitor_pipeline.PipelineError(
                "provider.cvdRetryInputPath is required for --kind cvd-retry")
        payload = _cvd_retry_only(value)
    else:
        raise monitor_pipeline.PipelineError("kind must be snapshot or cvd-retry")
    monitor_pipeline._atomic_json(target, payload)
    return {
        "schemaVersion": monitor_pipeline.SCHEMA_VERSION,
        "kind": kind,
        "inputSha256": monitor_pipeline._sha256(raw),
        "outputPath": str(target),
        "written": True,
        "staleCvdRetryCleared": stale_retry_cleared,
        "networkInvoked": False,
        "orderInvoked": False,
        "reconcileInvoked": False,
    }


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Atomically ingest R13 monitor JSON (no network/orders).")
    parser.add_argument("--config", default="monitor_config.json", help="R13 config path")
    parser.add_argument("--kind", required=True, choices=("snapshot", "cvd-retry"),
                        help="provider payload class")
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        receipt = ingest(args.config, args.kind, sys.stdin.buffer.read())
    except monitor_pipeline.PipelineError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""R13 config-driven, file-backed monitor acquisition pipeline.

The TradingView MCP session belongs to the interactive controller and cannot be
called safely from this Python process.  This module is therefore the narrow,
auditable boundary between that controller and the Nightwatch system: it reads
one immutable UTF-8 JSON snapshot from a configured file, validates and
normalizes it, performs one *separate* CVD retry read when required, then runs
the existing strategy/publish preflight.

It never executes shell commands, calls ``monitor_publish.main()``, invokes
``autotrade_engine.reconcile()``, or calls ``order.py``.  Its only execution
handoff is a dry-run management-plan preview.  Live order activation remains
outside this process behind the existing two explicit environment flags and a
successfully published state.

    python monitor_pipeline.py --config monitor_config.json

The output artifact contains both the exact acquisition order for the next
cycle and the current-cycle phase log.  File writes use an atomic replacement
so a monitor never consumes a partial result.
"""

from __future__ import annotations

import contract as contract_month  # R102: 取引限月の正本

import argparse
import copy
import hashlib
import json
import math
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

import autotrade_engine  # noqa: E402 - dry-run plan builder only
import htf_context  # noqa: E402 - confirmed HTF bars -> compact context
import monitor_publish  # noqa: E402 - pure compact/enrich/preflight only
import msnr_gate  # noqa: E402 - deterministic strategy evaluator
import strategy_evidence  # noqa: E402 - canonical evidence ingress only


SCHEMA_VERSION = "NQX_MONITOR_PIPELINE/1"
MIN_3M_BARS = 12
DEFAULT_MAX_AGE_SEC = {
    "market": 600,
    "bars1m": 180,
    "bars3m": 600,
    "bars15m": 1800,
    # HTF は「最終確定足の close からの経過」で測る。物差しは htf_context と同じ
    # (2×step + 再取得 1 サイクル分の余裕)。open で測ると足の間隔が step より
    # 開く所で毎日偽の STALE が出る(_validate_evidence の注記)。
    "bars45m": htf_context.FRAME_MAX_AGE["45m"],
    "bars1h": htf_context.FRAME_MAX_AGE["1h"],
    "bars4h": htf_context.FRAME_MAX_AGE["4h"],
    "bars1d": htf_context.FRAME_MAX_AGE["1d"],
    "vp": 1800,
    "peers": 600,
    "events": 900,
    "cvd": 600,
}


class PipelineError(ValueError):
    """A configuration or source-data error that must fail closed."""


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def _sha256(value: Any) -> str:
    if isinstance(value, bytes):
        data = value
    else:
        data = _canonical_bytes(value)
    return hashlib.sha256(data).hexdigest()


def _iso_now(now: Optional[datetime] = None) -> str:
    value = now or datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _parse_at(value: Any, field: str) -> datetime:
    if not value:
        raise PipelineError(f"{field} is required")
    raw = str(value).strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise PipelineError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise PipelineError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _epoch(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = None
    if number is not None and math.isfinite(number):
        return number / 1000.0 if number > 10_000_000_000 else number
    try:
        return _parse_at(value, "timestamp").timestamp()
    except PipelineError:
        return None


def _resolve(path_value: str, base: Path, label: str) -> Path:
    if not isinstance(path_value, str) or not path_value.strip():
        raise PipelineError(f"{label} is required")
    base = base.resolve()
    candidate = Path(path_value.strip())
    resolved = (candidate if candidate.is_absolute() else base / candidate).resolve()
    # A monitor config is a local capability boundary.  Do not permit a
    # relative ``..`` escape (or an absolute substitute) to make it read/write
    # outside the configuration directory.  This also confines every output
    # artifact to the configured monitor workspace.
    try:
        resolved.relative_to(base)
    except ValueError as exc:
        raise PipelineError(f"{label} must stay within the config directory") from exc
    return resolved


def _read_json(path: Path, label: str) -> Tuple[Dict[str, Any], str]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise PipelineError(f"{label} cannot be read: {exc}") from exc
    try:
        decoded = raw.decode("utf-8-sig")
        value = json.loads(decoded)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PipelineError(f"{label} must be UTF-8 JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise PipelineError(f"{label} must contain a JSON object")
    return value, _sha256(raw)


def _atomic_json(path: Path, value: Any) -> None:
    """Write one complete UTF-8 JSON artifact without exposing a partial file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False,
                                         dir=str(path.parent), prefix=f".{path.name}.",
                                         suffix=".tmp")
    try:
        with handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(handle.name, path)
    except Exception:
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        raise


def load_config(config_path: str | Path) -> Dict[str, Any]:
    """Load the non-secret R13 config; only the allowlisted file provider exists."""
    path = Path(config_path).resolve()
    cfg, config_hash = _read_json(path, "config")
    if cfg.get("schemaVersion") != SCHEMA_VERSION:
        raise PipelineError(f"schemaVersion must be {SCHEMA_VERSION}")
    symbol = str(cfg.get("symbol") or "").upper().strip()
    if not symbol.startswith("MNQ"):
        raise PipelineError("symbol must identify an MNQ contract")
    provider = cfg.get("provider")
    if not isinstance(provider, dict) or provider.get("type") != "file":
        raise PipelineError("provider.type must be the allowlisted value 'file'")
    input_path = _resolve(provider.get("snapshotInputPath"), path.parent,
                          "provider.snapshotInputPath")
    retry_value = provider.get("cvdRetryInputPath")
    retry_path = (_resolve(retry_value, path.parent, "provider.cvdRetryInputPath")
                  if retry_value else None)
    if retry_path is not None and retry_path == input_path:
        raise PipelineError("provider.cvdRetryInputPath must be a distinct file")

    execution = cfg.get("execution") or {}
    if not isinstance(execution, dict):
        raise PipelineError("execution must be an object")
    if str(execution.get("mode", "DRY_RUN_ONLY")).upper() != "DRY_RUN_ONLY":
        raise PipelineError("monitor_pipeline only permits execution.mode=DRY_RUN_ONLY")

    max_age = dict(DEFAULT_MAX_AGE_SEC)
    supplied_age = cfg.get("maxAgeSec") or {}
    if not isinstance(supplied_age, dict):
        raise PipelineError("maxAgeSec must be an object")
    for key, value in supplied_age.items():
        if key not in max_age:
            raise PipelineError(f"maxAgeSec.{key} is not supported")
        try:
            parsed = float(value)
        except (TypeError, ValueError) as exc:
            raise PipelineError(f"maxAgeSec.{key} must be a positive number") from exc
        if not math.isfinite(parsed) or parsed <= 0:
            raise PipelineError(f"maxAgeSec.{key} must be a positive number")
        max_age[key] = parsed

    output = cfg.get("output") or {}
    if not isinstance(output, dict):
        raise PipelineError("output must be an object")
    paths = {
        "cycle": _resolve(output.get("cyclePath", ".secrets/monitor_pipeline_cycle.json"),
                          path.parent, "output.cyclePath"),
        "bundle": _resolve(output.get("publishBundlePath", ".secrets/monitor_pipeline_bundle.json"),
                           path.parent, "output.publishBundlePath"),
    }
    if paths["cycle"] == input_path or paths["bundle"] == input_path:
        raise PipelineError("output paths must not overwrite provider.snapshotInputPath")
    if retry_path is not None and retry_path in paths.values():
        raise PipelineError("output paths must not overwrite provider.cvdRetryInputPath")

    return {
        "path": path,
        "hash": config_hash,
        "symbol": symbol,
        "provider": {"type": "file", "snapshotInputPath": input_path,
                     "cvdRetryInputPath": retry_path},
        "maxAgeSec": max_age,
        "output": paths,
        "execution": {"mode": "DRY_RUN_ONLY"},
    }


def _source_bundle(raw: Dict[str, Any], cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize the provider handoff without inventing missing evidence."""
    source = raw.get("bundle") if isinstance(raw.get("bundle"), dict) else raw
    out = copy.deepcopy(source)
    snapshot = out.get("snapshot")
    if not isinstance(snapshot, dict):
        snapshot = {}
    # Provider may place fields either at the snapshot or bundle layer; preserve
    # them at both locations where the established evaluator accepts either.
    for key in ("bars1m", "bars3m", "bars15m", "bars45m", "bars1h", "bars4h", "bars1d",
                "htfContext", "bars", "levels", "rangeAnchor", "peers",
                "peerMeta", "sessionId", "smtObservation", "cvd", "cvdMeta", "vwap",
                "vwapAnchorT", "eventGate"):
        if key not in snapshot and key in out:
            snapshot[key] = copy.deepcopy(out[key])
    bars1m = snapshot.get("bars1m")
    if isinstance(bars1m, list) and bars1m:
        try:
            # 1m は Mini App のチャート枠(FEED_BARS_MAX=60)ではなく評価器の枠で
            # 正規化する。60 に揃えると SILVER_BULLET_LOOKBACK_BARS=90 に届かない。
            normalized_1m = monitor_publish.normalize_market_bars(
                bars1m, limit=monitor_publish.FEED_BARS_1M_MAX)
        except (TypeError, ValueError) as exc:
            raise PipelineError(f"snapshot.bars1m rejected: {exc}") from exc
        for bar in normalized_1m:
            if bar["t"] > 10_000_000_000:
                bar["t"] /= 1000.0
        snapshot["bars1m"] = normalized_1m

    bars3m = snapshot.get("bars3m") or snapshot.get("bars")
    if not isinstance(bars3m, list) or len(bars3m) < MIN_3M_BARS:
        raise PipelineError(f"snapshot.bars3m requires at least {MIN_3M_BARS} bars")
    try:
        normalized_3m = monitor_publish.normalize_market_bars(bars3m)
    except (TypeError, ValueError) as exc:
        raise PipelineError(f"snapshot.bars3m rejected: {exc}") from exc
    for bar in normalized_3m:
        if bar["t"] > 10_000_000_000:
            bar["t"] /= 1000.0
    snapshot["bars3m"] = normalized_3m
    snapshot["bars"] = list(normalized_3m)

    bars15m = snapshot.get("bars15m")
    if isinstance(bars15m, list) and bars15m:
        try:
            normalized_15m = monitor_publish.normalize_market_bars(bars15m)
        except (TypeError, ValueError) as exc:
            raise PipelineError(f"snapshot.bars15m rejected: {exc}") from exc
        for bar in normalized_15m:
            if bar["t"] > 10_000_000_000:
                bar["t"] /= 1000.0
        snapshot["bars15m"] = normalized_15m

    for key in ("bars45m", "bars1h", "bars4h", "bars1d"):
        rows = snapshot.get(key)
        if not isinstance(rows, list) or not rows:
            continue
        try:
            normalized = monitor_publish.normalize_market_bars(rows)
        except (TypeError, ValueError) as exc:
            raise PipelineError(f"snapshot.{key} rejected: {exc}") from exc
        for bar in normalized:
            if bar["t"] > 10_000_000_000:
                bar["t"] /= 1000.0
        snapshot[key] = normalized

    if not isinstance(snapshot.get("levels"), list):
        snapshot["levels"] = []
    out["snapshot"] = snapshot
    out["symbol"] = cfg["symbol"]
    out["sourceSymbol"] = (out.get("sourceSymbol") or snapshot.get("sourceSymbol")
                           or cfg["symbol"])
    out["priceSource"] = out.get("priceSource") or snapshot.get("priceSource")
    out["priceAt"] = out.get("priceAt") or snapshot.get("priceAt") or out.get("at")
    out["at"] = out.get("at") or snapshot.get("at") or out.get("priceAt")
    out["price"] = out.get("price", snapshot.get("price"))
    if out["price"] is None:
        out["price"] = normalized_3m[-1]["c"]
    snapshot["htfContext"] = htf_context.build_context({
        "45m": snapshot.get("bars45m") or [],
        "1h": snapshot.get("bars1h") or [],
        "4h": snapshot.get("bars4h") or [],
        "1d": snapshot.get("bars1d") or [],
    }, out.get("at"))
    # Leave CVD absent when unavailable: the evaluator then demands exactly one
    # retry and caps A+ to A instead of replacing the structural decision.
    if "cvd" not in out and "cvd" in snapshot:
        out["cvd"] = copy.deepcopy(snapshot["cvd"])
    if "cvdMeta" not in out and isinstance(snapshot.get("cvdMeta"), dict):
        out["cvdMeta"] = copy.deepcopy(snapshot["cvdMeta"])
    # Strategy evidence has exactly one authoritative location: the bundle
    # top level.  Snapshot-local provider material is retained only as an
    # audit descriptor, then removed before strategy evaluation so a second
    # implicit matrix cannot shadow/rewrite the frozen evidence.
    top_evidence = out.get("strategyEvidence")
    snapshot_evidence = snapshot.pop("strategyEvidence", None)
    provider_evidence = top_evidence if top_evidence is not None else snapshot_evidence
    if top_evidence is not None and snapshot_evidence is not None:
        try:
            top_canonical = strategy_evidence.canonicalize(
                top_evidence, as_of=out.get("at"),
                session_id=out.get("sessionId") or snapshot.get("sessionId") or "UNSPECIFIED",
                source="provider", provenance="ingress")
            nested_canonical = strategy_evidence.canonicalize(
                snapshot_evidence, as_of=out.get("at"),
                session_id=out.get("sessionId") or snapshot.get("sessionId") or "UNSPECIFIED",
                source="provider", provenance="ingress")
            if top_canonical["evidenceHash"] != nested_canonical["evidenceHash"]:
                raise ValueError("top and snapshot strategyEvidence hashes differ")
            provider_evidence = top_canonical
        except ValueError as exc:
            # Evidence errors never delete a separately valid market snapshot.
            out["strategyEvidence"] = None
            out["providerStrategyEvidenceAudit"] = {
                "status": "REJECTED", "reason": str(exc),
            }
            return out
    if provider_evidence is not None:
        try:
            canonical = strategy_evidence.canonicalize(
                provider_evidence, as_of=out.get("at"),
                session_id=out.get("sessionId") or snapshot.get("sessionId") or "UNSPECIFIED",
                source="provider", provenance="ingress")
            out["strategyEvidence"] = canonical
            out["providerStrategyEvidenceAudit"] = {
                "status": "CANONICAL", "evidenceHash": canonical["evidenceHash"],
            }
        except ValueError as exc:
            out["strategyEvidence"] = None
            out["providerStrategyEvidenceAudit"] = {
                "status": "REJECTED", "reason": str(exc),
            }
    return out


def _set_cvd(bundle: Dict[str, Any], value: Any, meta: Dict[str, Any]) -> None:
    snapshot = bundle["snapshot"]
    bundle["cvd"] = copy.deepcopy(value)
    snapshot["cvd"] = copy.deepcopy(value)
    bundle["cvdMeta"] = copy.deepcopy(meta)
    snapshot["cvdMeta"] = copy.deepcopy(meta)
    bundle["cvdAttempts"] = meta["attempts"]


def _initial_cvd(bundle: Dict[str, Any], provider: str) -> None:
    existing = bundle.get("cvdMeta") if isinstance(bundle.get("cvdMeta"), dict) else {}
    meta = copy.deepcopy(existing)
    meta["attempts"] = 1
    meta.setdefault("provider", provider)
    if not meta.get("status") and bundle.get("cvd") is None:
        meta["status"] = "MISSING"
    _set_cvd(bundle, bundle.get("cvd"), meta)


def _retry_cvd(bundle: Dict[str, Any], raw_retry: Dict[str, Any], provider: str) -> None:
    retry_snapshot = raw_retry.get("snapshot") if isinstance(raw_retry.get("snapshot"), dict) else {}
    value = raw_retry.get("cvd", retry_snapshot.get("cvd"))
    supplied = raw_retry.get("cvdMeta")
    if not isinstance(supplied, dict):
        supplied = retry_snapshot.get("cvdMeta") if isinstance(retry_snapshot.get("cvdMeta"), dict) else {}
    meta = copy.deepcopy(supplied)
    meta["attempts"] = 2
    meta.setdefault("provider", provider)
    if not meta.get("status") and value is None:
        meta["status"] = "MISSING"
    _set_cvd(bundle, value, meta)


def _enforce_cvd_age(bundle: Dict[str, Any], now: datetime, max_age: float) -> Optional[str]:
    """Apply the configured CVD freshness limit before ``cvd_health`` scores it.

    ``msnr_gate`` owns the universal two-attempt/A+ cap.  The acquisition
    pipeline owns its configured timing budget, so an otherwise-FRESH CVD
    observation cannot bypass ``maxAgeSec.cvd`` merely because it is less than
    the evaluator's generic timestamp threshold.
    """
    meta = bundle.get("cvdMeta") if isinstance(bundle.get("cvdMeta"), dict) else {}
    issue = _age_status(meta.get("at"), now, max_age, "CVD")
    if not issue:
        return None
    stale_meta = copy.deepcopy(meta)
    stale_meta["status"] = "STALE"
    stale_meta["freshnessReason"] = issue
    stale_meta["attempts"] = int(stale_meta.get("attempts", bundle.get("cvdAttempts", 1)) or 1)
    _set_cvd(bundle, bundle.get("cvd"), stale_meta)
    return issue


def _age_status(value: Any, now: datetime, max_age: float, label: str) -> Optional[str]:
    epoch = _epoch(value)
    if epoch is None:
        return f"{label}_TIMESTAMP_MISSING"
    age = now.timestamp() - epoch
    if age < -60:
        return f"{label}_TIMESTAMP_FUTURE"
    if age > max_age:
        return f"{label}_STALE_{age:.0f}S"
    return None


def _validate_evidence(bundle: Dict[str, Any], cfg: Dict[str, Any], now: datetime) -> Dict[str, List[str]]:
    """Separate blocking market-feed defects from neutral/graded ICT absence."""
    snapshot = bundle["snapshot"]
    blocking: List[str] = []
    missing: List[str] = []
    stale: List[str] = []
    not_applicable: List[str] = []

    # R44: the receipt is an execution gate, not optional display metadata.
    # The legacy direct-publish route produced coherent-looking prices while
    # silently omitting the raw acquisition proof; the Mini App then showed
    # DATA NO RECEIPT but the pipeline still reported READY.  Require the
    # versioned receipt and independently derive the required-source failures
    # from ``sources`` instead of trusting only its summary boolean/list.
    receipt = bundle.get("acquisitionReceipt")
    if not isinstance(receipt, dict):
        blocking.append("ACQUISITION_RECEIPT_MISSING")
    elif str(receipt.get("schemaVersion") or "") != "NQX_ACQUISITION_RECEIPT/1":
        blocking.append("ACQUISITION_RECEIPT_INVALID")
    else:
        sources = receipt.get("sources")
        if not isinstance(sources, dict) or not sources:
            blocking.append("ACQUISITION_RECEIPT_INVALID")
        else:
            required = [(str(name), row) for name, row in sources.items()
                        if isinstance(row, dict) and row.get("required") is True]
            if not required:
                blocking.append("ACQUISITION_RECEIPT_INVALID")
            stale_required = set(str(name) for name in (receipt.get("staleRequired") or []))
            stale_required.update(name for name, row in required
                                  if str(row.get("status") or "") != "FRESH")
            for name in sorted(stale_required):
                code = "RAW_SOURCE_NOT_FRESH_" + name.upper().replace(".", "_").replace("-", "_")
                blocking.append(code)
            if receipt.get("requiredFresh") is not True and not stale_required:
                blocking.append("ACQUISITION_REQUIRED_FRESH_FALSE")
    try:
        _parse_at(bundle.get("at"), "at")
        _parse_at(bundle.get("priceAt"), "priceAt")
    except PipelineError as exc:
        blocking.append(str(exc))
    # R102: sourceSymbol は発注先の限月そのもの。連続足 MNQ1! / 別限月は評価に進ませない。
    sym_ok, sym_reason = contract_month.chart_symbol_matches(bundle.get("sourceSymbol"),
                                                            bundle.get("sourceFrontContract"))
    if not sym_ok:
        blocking.append(sym_reason.split(":", 1)[0])
    if not bundle.get("priceSource"):
        blocking.append("PRICE_SOURCE_MISSING")
    try:
        price = float(bundle.get("price"))
        if not math.isfinite(price) or abs(price / 0.25 - round(price / 0.25)) > 1e-8:
            raise ValueError
    except (TypeError, ValueError):
        blocking.append("PRICE_INVALID_OR_OFF_TICK")
    market_age = _age_status(bundle.get("priceAt"), now, cfg["maxAgeSec"]["market"], "MARKET")
    if market_age:
        stale.append(market_age)
        blocking.append(market_age)
    bars3m = snapshot.get("bars3m") or []
    if len(bars3m) < MIN_3M_BARS:
        blocking.append("BARS3M_INSUFFICIENT")
    elif (bar_age := _age_status(bars3m[-1].get("t"), now,
                                 cfg["maxAgeSec"]["bars3m"], "BARS3M")):
        stale.append(bar_age)
        blocking.append(bar_age)
    # 1m is optional for the Silver Bullet detector. A bad optional feed must
    # not erase a valid 3m decision, but it must be visible as unavailable for
    # that model rather than silently used as if it were fresh.
    bars1m = snapshot.get("bars1m") or []
    if bars1m and (bar_age := _age_status(bars1m[-1].get("t"), now,
                                          cfg["maxAgeSec"]["bars1m"], "BARS1M")):
        missing.append(bar_age)
    bars15m = snapshot.get("bars15m") or []
    if not bars15m:
        missing.append("BARS15M_MISSING")
    elif (bar_age := _age_status(bars15m[-1].get("t"), now,
                                 cfg["maxAgeSec"]["bars15m"], "BARS15M")):
        stale.append(bar_age)
        missing.append(bar_age)
    for key, label, frame in (("bars45m", "BARS45M", "45m"), ("bars1h", "BARS1H", "1h"),
                              ("bars4h", "BARS4H", "4h"), ("bars1d", "BARS1D", "1d")):
        rows = snapshot.get(key) or []
        if not rows:
            missing.append(f"{label}_MISSING")
            continue
        # 鮮度は最終確定足の **close**(t + step)で測る。open で測ると、足の間隔が
        # step より開く所(4h の 23:00 JST 足の次は 07:00)で毎日 07:00〜11:00 に
        # 偽の STALE が出る一方、閉じたばかりの足を途中値のまま抱えていても
        # 静かになる(2026-09-08 実測)。確定行は tv_snapshot が後続行の存在で
        # 決めるので、ここで見る t は本当に閉じた足の open である。
        last_open = _epoch(rows[-1].get("t"))
        close_at = None if last_open is None else last_open + htf_context.FRAME_STEPS[frame]
        if (bar_age := _age_status(close_at, now, cfg["maxAgeSec"][key], label)):
            stale.append(bar_age)
            missing.append(bar_age)
    if not snapshot.get("levels"):
        missing.append("VP_LEVELS_MISSING")
    # The evaluator accepts a settled named range derived from VP/session
    # levels.  Validation must use the same resolver instead of falsely calling
    # every non-explicit anchor missing.
    try:
        range_state = msnr_gate.resolve_range_anchor(bundle, snapshot, float(bundle.get("price")))
    except (TypeError, ValueError):
        range_state = {"valid": False, "reason": "RANGE_CONTEXT_INVALID"}
    if not range_state.get("valid"):
        reason = str(range_state.get("reason") or "RANGE_CONTEXT_UNAVAILABLE")
        missing.append("RANGE_CONTEXT_UNAVAILABLE" if reason == "RANGE_ANCHOR_REQUIRED" else reason)

    # SMT can come from exact peer bars or from the chart's own SMT study.  It
    # is explicitly N/A outside the AM/PM SMT windows, not an acquisition miss.
    smt_observation = snapshot.get("smtObservation", bundle.get("smtObservation"))
    if isinstance(smt_observation, dict):
        smt_probe = msnr_gate.external_smt(smt_observation, bars3m, bundle.get("at"))
        if smt_probe and smt_probe.get("reason") == "OUTSIDE_SMT_WINDOW":
            not_applicable.append("SMT_OUTSIDE_WINDOW")
        elif smt_probe is None:
            not_applicable.append("SMT_OBSERVATION_UNRESOLVED")
    elif not snapshot.get("peers"):
        smt_probe = msnr_gate.index_smt(bars3m, {}, bundle.get("at"))
        if smt_probe.get("window") is None:
            not_applicable.append("SMT_OUTSIDE_WINDOW")
        else:
            missing.append("SMT_SOURCE_MISSING")
    if not (snapshot.get("eventGate") or bundle.get("eventGate")):
        missing.append("EVENT_CONTEXT_MISSING")
    return {"blocking": list(dict.fromkeys(blocking)),
            "missing": list(dict.fromkeys(missing)),
            "stale": list(dict.fromkeys(stale)),
            "notApplicable": list(dict.fromkeys(not_applicable))}


def _acquisition_request(bundle: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """Controller-facing order: context first, then execution and confirmation."""
    request = [
        {"order": 1, "step": "WINDOW_LAYOUT", "required": ["exactly 2 panes", "pane 0 MNQ/15m", "pane 1 MNQ/3m + CVD Unified"]},
        {"order": 2, "step": "CONTEXT_15M", "required": ["closed OHLCV", "CT trend/ATR/trail", "session structure"]},
        {"order": 3, "step": "BARS_HTF_DUE", "required": ["due 45m/1h/4h/1D closed OHLC", "frame close timestamp", "freshness", "restore pane 0 to 15m"]},
        {"order": 4, "step": "RANGE_ANCHOR", "required": ["rangeTf", "rangeStart", "rangeEnd", "anchorType", "freshness", "high", "low"]},
        {"order": 5, "step": "EXECUTION_3M", "required": ["240 OHLCV bars", "visible timestamp", "last price", "0.25 tick"]},
        {"order": 6, "step": "VP_PD_DOL", "required": ["VP levels", "PD/DOL/session labels", "as-of time"]},
        {"order": 7, "step": "CVD_INITIAL", "required": ["CVD value/direction", "EMA/table agreement", "timestamp", "status", "provider"]},
        {"order": 8, "step": "SMT_PEERS", "required": ["same timestamp peers or SMT observation", "sessionId", "peer freshness"]},
        {"order": 9, "step": "EVENT_CONTEXT", "required": ["event state", "source", "checked_at"]},
        {"order": 10, "step": "NORMALIZE_VALIDATE", "required": ["schema", "freshness", "hash", "window contract", "missing evidence"]},
        {"order": 11, "step": "MSNR_ICT_EVALUATE", "required": ["LONG", "SHORT", "FLAT comparison", "all acquired evidence consumed or logged"]},
        {"order": 12, "step": "PUBLISH_PREFLIGHT", "required": ["compact bundle", "market payload validation"]},
        {"order": 13, "step": "AUTOTRADE_DRY_RUN_HANDOFF", "required": ["management plan only", "no reconcile", "no order"]},
    ]
    cvd = ((bundle or {}).get("ictEvidence") or {}).get("cvd") or {}
    if cvd.get("refreshRequired"):
        retry_at = next((index + 1 for index, row in enumerate(request)
                         if row["step"] == "CVD_INITIAL"), len(request))
        request.insert(retry_at, {"order": "7b", "step": "CVD_RETRY_ONCE",
                                  "required": ["one fresh retry input only", "cvdAttempts=2"]})
    return request


def _handoff(bundle: Dict[str, Any]) -> Dict[str, Any]:
    """Build, but never route, the existing engine's execution management plan."""
    scenario = (bundle.get("scenarios") or {}).get("primary")
    result: Dict[str, Any] = {
        "mode": "DRY_RUN_ONLY",
        "reconcileInvoked": False,
        "orderInvoked": False,
        "liveExecutionPermitted": False,
        "status": "NO_ACTIONABLE_SCENARIO",
    }
    if not isinstance(scenario, dict):
        return result
    state, grade = str(scenario.get("state") or "").upper(), scenario.get("grade")
    # 2026-09-04: B も発注可能(ユーザー決定)。状態コード名は互換のため据え置き。
    if state not in {"ACTIVE", "ARMED"} or grade not in {"A", "A+", "B"}:
        result["status"] = "SCENARIO_NOT_A_A_PLUS_ARMED"
        result["scenario"] = {"decisionId": scenario.get("decisionId"), "state": state, "grade": grade}
        return result
    try:
        plan = autotrade_engine.build_management_plan(scenario, bundle,
                                                      {"NQX_SYMBOL": bundle.get("symbol", contract_month.symbol())})
    except ValueError as exc:
        result["status"] = "PLAN_REJECTED"
        result["reason"] = str(exc)
        return result
    result.update({"status": "PLAN_VALIDATED_NOT_ROUTED", "plan": plan})
    return result


def run_pipeline(config_path: str | Path, now: Optional[datetime] = None,
                 write: bool = True) -> Dict[str, Any]:
    """Run every R13 phase.  No phase can trigger a network or broker action."""
    clock = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    cfg = load_config(config_path)
    phases: List[Dict[str, Any]] = []

    def phase(name: str, outcome: str, **detail: Any) -> None:
        phases.append({"phase": name, "status": outcome, "at": _iso_now(clock), **detail})

    phase("CONFIG_LOADED", "OK", configSha256=cfg["hash"], provider="file",
          executionMode="DRY_RUN_ONLY")
    raw, snapshot_hash = _read_json(cfg["provider"]["snapshotInputPath"], "provider snapshot")
    phase("SNAPSHOT_READ", "OK", path=str(cfg["provider"]["snapshotInputPath"]),
          snapshotSha256=snapshot_hash)
    bundle = _source_bundle(raw, cfg)
    _initial_cvd(bundle, "file")
    phase("NORMALIZED", "OK", bars3m=len(bundle["snapshot"]["bars3m"]),
          bars15m=len(bundle["snapshot"].get("bars15m") or []))

    initial_cvd_age_issue = _enforce_cvd_age(bundle, clock, cfg["maxAgeSec"]["cvd"])
    initial_health = msnr_gate.cvd_health(bundle, bundle["snapshot"],
                                           bundle["snapshot"]["bars3m"])
    retry_hash = None
    if initial_health.get("refreshRequired"):
        retry_path = cfg["provider"].get("cvdRetryInputPath")
        if retry_path and retry_path.exists():
            raw_retry, retry_hash = _read_json(retry_path, "CVD retry input")
            _retry_cvd(bundle, raw_retry, "file")
            retry_cvd_age_issue = _enforce_cvd_age(bundle, clock, cfg["maxAgeSec"]["cvd"])
            phase("CVD_RETRY", "OK", attempts=2, path=str(retry_path),
                  retrySha256=retry_hash, freshnessIssue=retry_cvd_age_issue)
        else:
            phase("CVD_RETRY", "PENDING", attempts=1,
                  reason="CVD retry required but provider.cvdRetryInputPath is unavailable")
    else:
        phase("CVD_RETRY", "SKIPPED", attempts=1, reason="initial CVD is fresh")
    cvd_health = msnr_gate.cvd_health(bundle, bundle["snapshot"], bundle["snapshot"]["bars3m"])
    phase("CVD_HEALTH", "OK", status=cvd_health.get("status"),
          attempts=cvd_health.get("attempts"), aplusAllowed=cvd_health.get("aplusAllowed"),
          refreshRequired=cvd_health.get("refreshRequired"),
          freshnessIssue=initial_cvd_age_issue)

    validation = _validate_evidence(bundle, cfg, clock)
    phase("EVIDENCE_VALIDATED", "BLOCKED" if validation["blocking"] else "OK",
          blocking=validation["blocking"], missing=validation["missing"], stale=validation["stale"],
          notApplicable=validation["notApplicable"])

    published_bundle: Optional[Dict[str, Any]] = None
    strategy_notes: List[str] = []
    publish_preflight: Dict[str, Any] = {"ready": False, "reason": "evidence blocked"}
    handoff: Dict[str, Any] = {"mode": "DRY_RUN_ONLY", "status": "NOT_EVALUATED",
                               "reconcileInvoked": False, "orderInvoked": False,
                               "liveExecutionPermitted": False}
    if not validation["blocking"]:
        try:
            compact = monitor_publish.compact(bundle)
            published_bundle, strategy_notes = monitor_publish.enrich_decisive_strategy(compact)
            phase("MSNR_ICT_EVALUATED", "OK",
                  decision=((published_bundle.get("evaluation") or {}).get("decision") or {}))
            # build_market_payload is monitor_publish's side-effect-free final market
            # validation; calling publish_state/main would be outside R13 scope.
            payload = monitor_publish.build_market_payload(published_bundle, now=clock)
            publish_preflight = {"ready": True, "marketPayloadSha256": _sha256(payload),
                                 "strategyNotes": strategy_notes}
            phase("PUBLISH_PREFLIGHT", "OK", marketPayloadSha256=_sha256(payload))
            handoff = _handoff(published_bundle)
            phase("AUTOTRADE_DRY_RUN_HANDOFF", "OK", status=handoff.get("status"),
                  reconcileInvoked=False, orderInvoked=False)
        except (TypeError, ValueError, KeyError, ArithmeticError) as exc:
            publish_preflight = {"ready": False, "reason": f"{type(exc).__name__}: {exc}"}
            phase("PUBLISH_PREFLIGHT", "BLOCKED", reason=publish_preflight["reason"])

    status = "READY" if publish_preflight["ready"] else "BLOCKED"
    cycle = {
        "schemaVersion": SCHEMA_VERSION,
        "runAt": _iso_now(clock),
        "status": status,
        "symbol": cfg["symbol"],
        "provider": {"type": "file", "snapshotInputPath": str(cfg["provider"]["snapshotInputPath"]),
                     "cvdRetryInputPath": (str(cfg["provider"]["cvdRetryInputPath"])
                                           if cfg["provider"].get("cvdRetryInputPath") else None)},
        "hash": {"configSha256": cfg["hash"], "snapshotSha256": snapshot_hash,
                 "cvdRetrySha256": retry_hash},
        "phaseLog": phases,
        "validation": validation,
        "cvd": cvd_health,
        "acquisitionRequest": _acquisition_request(published_bundle),
        "publishPreflight": publish_preflight,
        "dryRunHandoff": handoff,
        "artifacts": {"publishBundlePath": str(cfg["output"]["bundle"]),
                      "cyclePath": str(cfg["output"]["cycle"])},
    }
    if published_bundle is not None:
        cycle["decision"] = ((published_bundle.get("evaluation") or {}).get("decision")
                             or {"model": "FLAT"})
    if write:
        cycle["artifacts"]["written"] = True
        if published_bundle is not None:
            _atomic_json(cfg["output"]["bundle"], published_bundle)
        _atomic_json(cfg["output"]["cycle"], cycle)
    else:
        cycle["artifacts"]["written"] = False
    return cycle


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Run the R13 file-backed monitor pipeline (dry-run only).")
    parser.add_argument("--config", default=str(BASE / "monitor_config.json"),
                        help="R13 JSON config path")
    parser.add_argument("--no-write", action="store_true", help="evaluate only; do not write artifacts")
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        result = run_pipeline(args.config, write=not args.no_write)
    except PipelineError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({key: result[key] for key in ("schemaVersion", "status", "hash", "validation",
                                                     "cvd", "publishPreflight", "dryRunHandoff")},
                     ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["status"] == "READY" else 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""R12 decision ledger writer.

Reads one bundle or a JSON list from stdin, evaluates it with msnr_gate, and
prints append-only observation rows.  Nothing is sent to a broker.  A ledger
file is written only when ``--ledger`` is explicitly supplied.
"""
import argparse
import csv
import json
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import msnr_gate  # noqa: E402

FIELDS = [
    "decision_id", "setup_version", "phase", "model", "direction", "regime", "grade", "score",
    "ict_location", "range_tf", "range_start", "range_end", "anchor_type", "anchor_freshness",
    "smt", "smt_freshness", "cvd_status", "cvd_attempts", "cbc", "fvg", "fvg_tf", "fvg_age_bars",
    "fvg_displacement_r", "fvg_pre_arrival_structure", "po3", "dol_r", "hrlr",
    "killzone", "ict_inputs_present", "ict_inputs_missing", "entry", "stop", "targets",
    "filled", "net_points", "r_multiple", "mfe_points", "mae_points", "outcome",
]


def _rows(raw):
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict)]
    return [raw] if isinstance(raw, dict) else []


def _value(source, key, default=""):
    value = source.get(key, default) if isinstance(source, dict) else default
    return "" if value is None else value


def observe(bundle):
    result = msnr_gate.evaluate(bundle)
    decision = result.get("decision") or {}
    ict = result.get("ict") or {}
    candidate = next((c for c in result.get("candidates", [])
                      if c.get("model") == decision.get("model")
                      and c.get("side") == decision.get("side")), {})
    rng = ict.get("range") or {}
    smt = ict.get("smt") or {}
    cvd = ict.get("cvd") or {}
    dol = (ict.get("dol") or {}).get(decision.get("side")) or {}
    fvg = candidate.get("fvg") or {}
    coverage = decision.get("ictCoverage") or {}
    snapshot = bundle.get("snapshot") if isinstance(bundle.get("snapshot"), dict) else {}
    event = bundle.get("outcome") if isinstance(bundle.get("outcome"), dict) else {}
    row = {
        "decision_id": _value(decision, "decisionId"),
        "setup_version": "R12-ICT-SPLIT-1",
        "phase": _value(decision, "phase"),
        "model": _value(decision, "model"),
        "direction": _value(decision, "side"),
        "regime": _value(bundle, "regime", "MX"),
        "grade": _value(decision, "grade"),
        "score": _value(decision, "score"),
        "ict_location": _value(rng, "position") or (";".join(candidate.get("evidence", []))),
        "range_tf": _value(rng, "rangeTf"),
        "range_start": _value(rng, "rangeStart"),
        "range_end": _value(rng, "rangeEnd"),
        "anchor_type": _value(rng, "anchorType"),
        "anchor_freshness": _value(rng, "freshness"),
        "smt": _value(smt, "bias"),
        "smt_freshness": _value(smt, "freshness"),
        "cvd_status": _value(cvd, "status"),
        "cvd_attempts": _value(cvd, "attempts"),
        "cbc": json.dumps(snapshot.get("cbc"), ensure_ascii=False, separators=(",", ":")) if snapshot.get("cbc") else "",
        "fvg": "confirmed" if "OTE_FVG_CONFLUENCE" in candidate.get("evidence", []) else "",
        "fvg_tf": _value(fvg, "timeframe"),
        "fvg_age_bars": _value(fvg, "ageBars"),
        "fvg_displacement_r": _value(fvg, "displacementR"),
        "fvg_pre_arrival_structure": _value(fvg, "preArrivalStructure"),
        "po3": _value(bundle, "po3") or _value(snapshot, "po3"),
        "dol_r": _value(dol, "distPt"),
        "hrlr": _value(dol, "run"),
        # 「ICT を評価して効かなかった」のか「入力が届かず無得点だった」のか
        # を OOS 集計で区別できるようにする。
        "killzone": _value(decision.get("ictSession") or {}, "window"),
        "ict_inputs_present": "%s/%s" % (
            _value(coverage, "ictInputsPresent", 0), _value(coverage, "ictInputsTotal", 0)),
        "ict_inputs_missing": ";".join(coverage.get("missing") or []),
        "entry": _value(decision, "entry"),
        "stop": _value(decision, "stop"),
        "targets": json.dumps(decision.get("targets") or [], ensure_ascii=False, separators=(",", ":")),
        "filled": _value(event, "filled", _value(bundle, "filled")),
        "net_points": _value(event, "net_points", _value(bundle, "net_points")),
        "r_multiple": _value(event, "r_multiple", _value(bundle, "r_multiple")),
        "mfe_points": _value(event, "mfe_points", _value(bundle, "mfe_points")),
        "mae_points": _value(event, "mae_points", _value(bundle, "mae_points")),
        "outcome": _value(event, "outcome", _value(bundle, "outcome")),
    }
    return row


def oos_record(bundle):
    """Return a strict OOS row only when an actual normalized outcome exists.

    Live decisions and open positions are still recorded in the CSV ledger,
    but never enter performance analysis before realizedR/costR/MFE/MAE are
    known. This prevents a convenience backfill from turning into simulated
    OOS performance.
    """
    outcome = bundle.get("outcome") if isinstance(bundle.get("outcome"), dict) else None
    required = {"filled", "realizedR", "costR", "mfeR", "maeR"}
    if not outcome or not required.issubset(outcome):
        return None
    result = msnr_gate.evaluate(bundle)
    decision = result.get("decision") or {}
    if not decision.get("decisionId") or decision.get("entry") is None or decision.get("stop") is None:
        return None
    frozen = dict(bundle)
    frozen.update({key: value for key, value in decision.items() if value is not None})
    version_fields = ("setupVersion", "catalogVersion", "detectorVersion", "executionContractVersion")
    version_values = {field: frozen.get(field) for field in version_fields}
    supplied = [field for field, value in version_values.items() if value]
    evidence = frozen.get("strategyEvidence") if isinstance(frozen.get("strategyEvidence"), dict) else {}
    evidence_hash = frozen.get("evidenceHash") or evidence.get("evidenceHash")
    # Never invent a current version/hash for an old observation.  Versioned
    # records must be complete; unversioned ones are explicitly partitioned
    # as legacy by oos_ablation.normalize.
    if supplied and (len(supplied) != len(version_fields) or not evidence or not evidence_hash):
        return None
    versions = ({field: str(version_values[field]) for field in version_fields}
                if supplied else {})
    regime = bundle.get("regime") or decision.get("regime")
    session_id = bundle.get("sessionId") or decision.get("sessionId")
    direction = decision.get("side") or bundle.get("direction")
    if supplied and (not regime or not session_id or not direction):
        return None
    return {
        "at": bundle.get("at"), "tradeId": decision.get("decisionId"),
        "entry": decision.get("entry"), "stop": decision.get("stop"),
        "regime": regime, "sessionId": session_id, "direction": direction,
        "exitSpec": "R12-SPLIT-TP1-RUNNER",
        "decision": {
            key: decision.get(key) for key in (
                "model", "side", "evidence", "targetLabels", "rangeAnchor", "fvg",
                "smt", "ictSession", "cvdHealth")
        },
        "outcome": {key: outcome.get(key) for key in required},
        **versions,
        **({"strategyEvidence": evidence,
            "eligibleVotes": frozen.get("eligibleVotes") or {},
            "evidenceHash": evidence_hash} if supplied else {}),
    }


def append_rows(path, rows):
    """Append only. Existing decision_id values are never duplicated."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    existing = set()
    try:
        with open(path, "r", encoding="utf-8-sig", newline="") as fh:
            existing = {row.get("decision_id") for row in csv.DictReader(fh)}
    except FileNotFoundError:
        pass
    fresh = [row for row in rows if row.get("decision_id") and row["decision_id"] not in existing]
    if not fresh:
        return 0
    new_file = not os.path.exists(path) or os.path.getsize(path) == 0
    with open(path, "a", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS)
        if new_file:
            writer.writeheader()
        writer.writerows(fresh)
    return len(fresh)


def append_oos_rows(path, rows):
    """Append-only JSONL; duplicate tradeId is not re-recorded."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    seen = set()
    try:
        with open(path, encoding="utf-8") as fh:
            for raw in fh:
                try:
                    seen.add(json.loads(raw).get("tradeId"))
                except json.JSONDecodeError:
                    continue
    except FileNotFoundError:
        pass
    fresh = [row for row in rows if row and row.get("tradeId") not in seen]
    if not fresh:
        return 0
    with open(path, "a", encoding="utf-8", newline="\n") as fh:
        for row in fresh:
            fh.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    return len(fresh)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", help="append to this CSV only when explicitly supplied")
    parser.add_argument("--oos-jsonl", help="append only completed normalized outcomes for OOS ablation")
    args = parser.parse_args(argv)
    try:
        raw = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError) as exc:
        raise SystemExit(f"ERROR: invalid JSON: {exc}")
    rows = [observe(bundle) for bundle in _rows(raw)]
    oos_rows = [oos_record(bundle) for bundle in _rows(raw)]
    written = append_rows(args.ledger, rows) if args.ledger else 0
    oos_written = append_oos_rows(args.oos_jsonl, oos_rows) if args.oos_jsonl else 0
    print(json.dumps({"setup_version": "R12-ICT-SPLIT-1", "rows": rows, "appended": written,
                      "oosAppended": oos_written},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

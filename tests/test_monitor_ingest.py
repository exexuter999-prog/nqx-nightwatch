# -*- coding: utf-8 -*-
"""R13 stdin bridge: atomic source handoffs only, never an order path."""
import json
import os
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import autotrade_engine  # noqa: E402
import monitor_ingest  # noqa: E402
import monitor_pipeline  # noqa: E402


def check(label, condition, detail=""):
    if not condition:
        raise AssertionError(f"{label}: {detail}")
    print(f"OK   {label}")


def write(path, value):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(value, fh)


with tempfile.TemporaryDirectory() as tmp:
    config_path = os.path.join(tmp, "config.json")
    snapshot_path = os.path.join(tmp, "snapshot.json")
    retry_path = os.path.join(tmp, "retry.json")
    write(config_path, {
        "schemaVersion": monitor_pipeline.SCHEMA_VERSION, "symbol": "MNQU6",
        "provider": {"type": "file", "snapshotInputPath": snapshot_path,
                     "cvdRetryInputPath": retry_path},
        "execution": {"mode": "DRY_RUN_ONLY"},
    })

    # A failing reconcile would make this test fail if ingestion ever wandered
    # into an execution path.  The bridge only writes the provider handoff.
    original_reconcile = autotrade_engine.reconcile
    autotrade_engine.reconcile = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("reconcile called"))
    try:
        raw_snapshot = b'{"price":20000.25,"snapshot":{"bars3m":[]}}'
        receipt = monitor_ingest.ingest(config_path, "snapshot", raw_snapshot)
    finally:
        autotrade_engine.reconcile = original_reconcile
    with open(snapshot_path, encoding="utf-8") as fh:
        snapshot = json.load(fh)
    check("snapshot object is atomically accepted", snapshot["price"] == 20000.25 and receipt["written"], receipt)
    check("snapshot does not touch execution", receipt["orderInvoked"] is False and
          receipt["reconcileInvoked"] is False and receipt["networkInvoked"] is False, receipt)

    retry = {"cvd": {"bias": "BULLISH", "value": 42}, "cvdAt": "2026-08-22T00:00:00+00:00",
             "cvdSource": "TradingView MCP"}
    retry_receipt = monitor_ingest.ingest(config_path, "cvd-retry", json.dumps(retry).encode("utf-8"))
    with open(retry_path, encoding="utf-8") as fh:
        stored_retry = json.load(fh)
    check("CVD-only retry is accepted", stored_retry["cvd"]["value"] == 42 and
          stored_retry["cvdMeta"]["at"] == retry["cvdAt"], retry_receipt)
    try:
        monitor_ingest.ingest(config_path, "cvd-retry", b'{"cvd":{},"price":20000}')
    except monitor_pipeline.PipelineError:
        rejected_structural = True
    else:
        rejected_structural = False
    check("CVD retry rejects structural overwrite", rejected_structural)

    escape_path = os.path.join(tmp, "escape.json")
    write(escape_path, {
        "schemaVersion": monitor_pipeline.SCHEMA_VERSION, "symbol": "MNQU6",
        "provider": {"type": "file", "snapshotInputPath": "../outside.json"},
        "execution": {"mode": "DRY_RUN_ONLY"},
    })
    try:
        monitor_ingest.ingest(escape_path, "snapshot", b'{}')
    except monitor_pipeline.PipelineError:
        rejected_escape = True
    else:
        rejected_escape = False
    check("config-directory escape is rejected", rejected_escape)

print("ALL PASS (test_monitor_ingest)")

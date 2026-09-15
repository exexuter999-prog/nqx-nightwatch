# -*- coding: utf-8 -*-
"""R13 config -> snapshot -> retry -> strategy -> dry-run handoff checks."""
import json
import os
import sys
import tempfile
from copy import deepcopy
from datetime import datetime, timedelta, timezone

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
import _pin_contract  # noqa: E402  (R102: 本番の manualHalt と限月をテストから切り離す)

import monitor_pipeline as pipeline  # noqa: E402


def check(label, condition, detail=""):
    if not condition:
        raise AssertionError(f"{label}: {detail}")
    print(f"OK   {label}")


def bars(now):
    end = int(now.timestamp() // 180 * 180)
    rows = []
    for index in range(16):
        open_ = 20000.00 + index * 0.25
        rows.append({"t": end - (15 - index) * 180, "o": open_,
                     "h": open_ + 1.00, "l": open_ - 1.00,
                     "c": open_ + 0.25, "v": 100 + index})
    return rows


NOW = datetime.now(timezone.utc).replace(microsecond=0)
ROWS = bars(NOW)
RAW = {
    "at": NOW.isoformat(), "priceAt": NOW.isoformat(), "price": ROWS[-1]["c"],
    "sourceSymbol": "CME_MINI:MNQU2026", "priceSource": "TradingView MCP file handoff",
    "snapshot": {
        "bars3m": ROWS,
        "bars15m": ROWS[::3],
        "levels": [{"label": "VAH", "price": 20010.00},
                   {"label": "VAL", "price": 19990.00}],
        "rangeAnchor": {"rangeTf": "15m", "rangeStart": (NOW - timedelta(minutes=45)).isoformat(),
                        "rangeEnd": NOW.isoformat(), "anchorType": "HTF_DIRECTIONAL_LEG",
                        "freshness": "FRESH", "high": 20020.00, "low": 19980.00},
        "sessionId": "NY-TEST", "peers": {}, "peerMeta": {},
        "eventGate": {"state": "NONE", "checkedAt": NOW.isoformat()},
    },
    "cvd": None, "cvdMeta": {"status": "MISSING", "at": NOW.isoformat()},
    "acquisitionReceipt": {
        "schemaVersion": "NQX_ACQUISITION_RECEIPT/1",
        "requiredFresh": True,
        "staleRequired": [],
        "sources": {
            name: {"required": True, "status": "FRESH"}
            for name in ("chart_state.json", "bars3m.json", "study_3m.json", "pine_labels.json")
        },
    },
}
RETRY = {"cvd": {"bias": "BULLISH", "value": 42},
         "cvdMeta": {"status": "FRESH", "at": NOW.isoformat(), "provider": "TradingView MCP"}}


with tempfile.TemporaryDirectory() as tmp:
    def write(name, data):
        path = os.path.join(tmp, name)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        return path

    snapshot_path = write("snapshot.json", RAW)
    retry_path = write("retry.json", RETRY)
    config_path = write("config.json", {
        "schemaVersion": pipeline.SCHEMA_VERSION, "symbol": "MNQU6",
        "provider": {"type": "file", "snapshotInputPath": snapshot_path,
                     "cvdRetryInputPath": retry_path},
        "output": {"cyclePath": os.path.join(tmp, "cycle.json"),
                   "publishBundlePath": os.path.join(tmp, "bundle.json")},
        "execution": {"mode": "DRY_RUN_ONLY"},
    })
    result = pipeline.run_pipeline(config_path, now=NOW, write=True)
    check("schemaVersion", result["schemaVersion"] == pipeline.SCHEMA_VERSION)
    check("fresh retry is consumed exactly once", result["cvd"]["attempts"] == 2 and
          result["cvd"]["aplusAllowed"] is True, result["cvd"])
    check("hashes are recorded", bool(result["hash"]["snapshotSha256"]) and
          bool(result["hash"]["cvdRetrySha256"]), result["hash"])
    check("fixed acquisition order is emitted", [item["step"] for item in result["acquisitionRequest"]][:7] ==
          ["WINDOW_LAYOUT", "CONTEXT_15M", "BARS_HTF_DUE", "RANGE_ANCHOR",
           "EXECUTION_3M", "VP_PD_DOL", "CVD_INITIAL"],
          result["acquisitionRequest"])
    check("executed CVD retry is in the phase log", any(item["phase"] == "CVD_RETRY" and item["status"] == "OK"
          for item in result["phaseLog"]), result["phaseLog"])
    check("pipeline has no execution route", result["dryRunHandoff"]["reconcileInvoked"] is False and
          result["dryRunHandoff"]["orderInvoked"] is False and
          result["dryRunHandoff"]["liveExecutionPermitted"] is False, result["dryRunHandoff"])
    check("atomic artifacts are written", os.path.exists(result["artifacts"]["cyclePath"]) and
          os.path.exists(result["artifacts"]["publishBundlePath"]),
          {"artifacts": result["artifacts"], "preflight": result["publishPreflight"], "validation": result["validation"]})

    # Validation must share the evaluator's range/SMT contract.  A settled
    # named range and chart SMT observation are acquired evidence even when no
    # explicit rangeAnchor or peer-bar payload exists.
    derived = deepcopy(RAW)
    derived["snapshot"].pop("rangeAnchor")
    derived["snapshot"]["levels"] = [
        {"label": "Previous Day High", "price": 20020.0},
        {"label": "Previous Day Low", "price": 19980.0},
    ]
    derived["snapshot"]["smtObservation"] = {
        "bias": "BULLISH", "peer": "MES1!", "agree": True, "source": "pine",
    }
    normalized_derived = pipeline._source_bundle(derived, {"symbol": "MNQU6"})
    aligned_validation = pipeline._validate_evidence(
        normalized_derived, {"maxAgeSec": pipeline.DEFAULT_MAX_AGE_SEC}, NOW)
    check("derived range is not falsely reported missing",
          "RANGE_CONTEXT_UNAVAILABLE" not in aligned_validation["missing"] and
          "RANGE_ANCHOR_MISSING" not in aligned_validation["missing"], aligned_validation)
    check("chart SMT observation is not falsely reported as missing peers",
          "SMT_SOURCE_MISSING" not in aligned_validation["missing"] and
          "SMT_PEERS_MISSING" not in aligned_validation["missing"], aligned_validation)

    stale_receipt = deepcopy(normalized_derived)
    stale_receipt["acquisitionReceipt"] = {
        "schemaVersion": "NQX_ACQUISITION_RECEIPT/1",
        "requiredFresh": False,
        "staleRequired": ["chart_state.json"],
        "sources": {
            "chart_state.json": {"required": True, "status": "STALE"},
            "bars3m.json": {"required": True, "status": "FRESH"},
            "study_3m.json": {"required": True, "status": "FRESH"},
            "pine_labels.json": {"required": True, "status": "FRESH"},
        },
    }
    receipt_validation = pipeline._validate_evidence(
        stale_receipt, {"maxAgeSec": pipeline.DEFAULT_MAX_AGE_SEC}, NOW)
    check("stale required raw source blocks the pipeline",
          "RAW_SOURCE_NOT_FRESH_CHART_STATE_JSON" in receipt_validation["blocking"],
          receipt_validation)

    no_receipt = deepcopy(normalized_derived)
    no_receipt.pop("acquisitionReceipt", None)
    no_receipt_validation = pipeline._validate_evidence(
        no_receipt, {"maxAgeSec": pipeline.DEFAULT_MAX_AGE_SEC}, NOW)
    check("missing acquisition receipt blocks the pipeline",
          "ACQUISITION_RECEIPT_MISSING" in no_receipt_validation["blocking"],
          no_receipt_validation)

    # Remove the retry file: the initial CVD read must be capped at A and the
    # next-cycle acquisition request must demand the one allowed retry.
    os.unlink(retry_path)
    capped = pipeline.run_pipeline(config_path, now=NOW, write=False)
    check("no retry input caps A+ without blocking market publish", capped["cvd"]["attempts"] == 1 and
          capped["cvd"]["aplusAllowed"] is False and capped["cvd"]["refreshRequired"] is True,
          capped["cvd"])
    check("next acquisition asks for only one CVD retry", any(item["step"] == "CVD_RETRY_ONCE"
          for item in capped["acquisitionRequest"]), capped["acquisitionRequest"])

    # ``maxAgeSec.cvd`` is a pipeline contract, rather than the evaluator's
    # generic fallback.  A nominally-FRESH but over-age value is stale and
    # therefore remains A+ ineligible after the one allowed attempt.
    stale_cvd = deepcopy(RAW)
    stale_cvd["cvd"] = {"bias": "BULLISH", "value": 42}
    stale_cvd["cvdMeta"] = {"status": "FRESH", "at": (NOW - timedelta(seconds=61)).isoformat()}
    write("snapshot.json", stale_cvd)
    stale_cvd_config = write("stale-cvd-config.json", {
        "schemaVersion": pipeline.SCHEMA_VERSION, "symbol": "MNQU6",
        "provider": {"type": "file", "snapshotInputPath": snapshot_path},
        "maxAgeSec": {"cvd": 60}, "execution": {"mode": "DRY_RUN_ONLY"},
    })
    stale_cvd_result = pipeline.run_pipeline(stale_cvd_config, now=NOW, write=False)
    check("configured stale CVD is capped after one read", stale_cvd_result["cvd"]["attempts"] == 1 and
          stale_cvd_result["cvd"]["aplusAllowed"] is False and stale_cvd_result["cvd"]["refreshRequired"] is True,
          stale_cvd_result["cvd"])

    # A stale market feed blocks before strategy/publish, and the disabled
    # handoff proves that no broker path was touched on that failure route.
    stale_market = deepcopy(RAW)
    stale_market["priceAt"] = (NOW - timedelta(seconds=601)).isoformat()
    write("snapshot.json", stale_market)
    blocked = pipeline.run_pipeline(config_path, now=NOW, write=False)
    check("stale market is BLOCKED", blocked["status"] == "BLOCKED" and
          any(item.startswith("MARKET_STALE") for item in blocked["validation"]["blocking"]), blocked["validation"])
    check("blocked market cannot invoke execution", blocked["dryRunHandoff"]["reconcileInvoked"] is False and
          blocked["dryRunHandoff"]["orderInvoked"] is False and
          blocked["dryRunHandoff"]["liveExecutionPermitted"] is False, blocked["dryRunHandoff"])

    live_config = write("live-config.json", {
        "schemaVersion": pipeline.SCHEMA_VERSION, "symbol": "MNQU6",
        "provider": {"type": "file", "snapshotInputPath": snapshot_path},
        "execution": {"mode": "LIVE"},
    })
    try:
        pipeline.load_config(live_config)
    except pipeline.PipelineError:
        rejected_live = True
    else:
        rejected_live = False
    check("LIVE execution mode is rejected", rejected_live)

    escape_config = write("escape-config.json", {
        "schemaVersion": pipeline.SCHEMA_VERSION, "symbol": "MNQU6",
        "provider": {"type": "file", "snapshotInputPath": "../outside.json"},
        "execution": {"mode": "DRY_RUN_ONLY"},
    })
    try:
        pipeline.load_config(escape_config)
    except pipeline.PipelineError:
        rejected_escape = True
    else:
        rejected_escape = False
    check("config path escape is rejected", rejected_escape)

print("ALL PASS (test_monitor_pipeline)")

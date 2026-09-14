# -*- coding: utf-8 -*-
"""OOSアブレーションは記録済み結果だけを集計し、未来情報を補完しない。"""
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
import oos_ablation as oos  # noqa: E402
import strategy_evidence  # noqa: E402


def check(label, condition, detail=""):
    if not condition:
        raise AssertionError(f"{label}: {detail}")
    print(f"OK   {label}")


def record(features, filled=True, realized=2.0, cost=0.1, mfe=2.5, mae=-0.5):
    return {
        "at": "2026-08-21T21:00:00+09:00", "entry": 100, "stop": 90,
        "exitSpec": "R12-SPLIT-TP1-RUNNER", "features": features,
        "outcome": {"filled": filled, "realizedR": realized, "costR": cost,
                    "mfeR": mfe, "maeR": mae},
    }


all_on = {key: True for key in oos.STAGES}
without_cvd = {**all_on, "CVD": False}
rows = [oos.normalize(record(all_on), 1, legacy_mode=True),
        oos.normalize(record(without_cvd, realized=-1.0), 2, legacy_mode=True)]
report = {row["stage"]: row for row in oos.metrics(rows)}

check("MSNR段階は同一entry/SLの全件を使う", report["MSNR"]["signals"] == 2, report["MSNR"])
check("CVD段階はCVDを追加フィルタとしてのみ使う", report["CVD"]["signals"] == 1, report["CVD"])
check("コスト後期待値を実現Rから計算", report["CVD"]["expectancyAfterCostPerFillR"] == 1.9, report["CVD"])

try:
    oos.normalize({"at": "2026-08-21T21:00:00+09:00", "entry": 100, "stop": 90,
                   "exitSpec": "same", "features": all_on}, 3)
    raise AssertionError("outcome missing must fail")
except ValueError as exc:
    check("結果欠落を推測で埋めない", "outcome is required" in str(exc), str(exc))

evidence = strategy_evidence.canonicalize({
    "version": "R14-STRATEGY-EVIDENCE-1", "asOf": "2026-08-21T12:00:00+00:00",
    "sessionId": "NY-2026-08-21", "source": "test", "provenance": "fixture", "models": {},
})
versioned = record(all_on)
versioned.pop("features")
versioned.update({
    "setupVersion": "R14-ICT-SETUP/1", "catalogVersion": "IMAGE_STRATEGY_CATALOG/1",
    "detectorVersion": "REPO2-DETECTOR/1", "executionContractVersion": "R14-EXECUTION-CONTRACT-1",
    "strategyEvidence": evidence,
    "evidenceHash": evidence["evidenceHash"],
    "sessionId": "NY-2026-08-21", "direction": "BUY", "regime": "MX",
})
versioned_row = oos.normalize(versioned, 4)
check("complete immutable version tuple is retained",
      versioned_row["versions"]["detectorVersion"] == "REPO2-DETECTOR/1", versioned_row)

repo_stage_to_model = {"IFVG": "ifvg", "BLOCKS": "blocks", "QUARTERLY": "quarterly",
                       "SESSIONS": "sessions", "FIB_SD": "fibSd", "FIB_CRT": "fibCrt"}


def versioned_with_repo_models(disabled=None):
    models = {model: {"eligible": stage != disabled} for stage, model in repo_stage_to_model.items()}
    frozen_evidence = strategy_evidence.canonicalize({
        "version": "R14-STRATEGY-EVIDENCE-1", "asOf": "2026-08-21T12:00:00+00:00",
        "sessionId": "NY-2026-08-21", "source": "test", "provenance": "fixture", "models": models,
    })
    return dict(versioned, strategyEvidence=frozen_evidence, evidenceHash=frozen_evidence["evidenceHash"],
                decision={"side": "BUY"})


repo_on = oos.normalize(versioned_with_repo_models(), 4)
check("six Repo2 ablation features derive from canonical frozen evidence",
      all(repo_on["features"][stage] for stage in repo_stage_to_model), repo_on)
for stage in repo_stage_to_model:
    repo_off = oos.normalize(versioned_with_repo_models(stage), 4)
    check(f"Repo2 {stage} off toggle cannot be overridden by mutable features", not repo_off["features"][stage], repo_off)

conflicting_features = dict(versioned, features=all_on)
try:
    oos.normalize(conflicting_features, 4)
    raise AssertionError("versioned explicit features must fail")
except ValueError as exc:
    check("versioned OOS rejects explicit mutable features", "features must derive" in str(exc), str(exc))

for missing in ("regime", "sessionId", "direction", "evidenceHash"):
    missing_dimension = dict(versioned)
    missing_dimension.pop(missing)
    try:
        oos.normalize(missing_dimension, 4)
        raise AssertionError(f"missing {missing} must fail")
    except ValueError:
        pass
check("versioned OOS requires frozen hash and all partition dimensions", True)

partial = dict(versioned)
partial.pop("detectorVersion")
try:
    oos.normalize(partial, 5)
    raise AssertionError("partial version set must fail")
except ValueError as exc:
    check("partial immutable version tuple is rejected", "incomplete" in str(exc), str(exc))

try:
    oos.normalize(record(all_on), 5)
    raise AssertionError("unversioned R14 record must fail without explicit legacy mode")
except ValueError as exc:
    check("strict R14 rejects implicit legacy records", "legacy_mode explicitly" in str(exc), str(exc))

tampered = dict(versioned, strategyEvidence=dict(evidence))
tampered["strategyEvidence"]["evidenceHash"] = "se_000000000000000000000000"
try:
    oos.normalize(tampered, 5)
    raise AssertionError("tampered frozen evidence must fail")
except ValueError as exc:
    check("frozen evidence hash tamper is rejected", "strategyEvidence" in str(exc), str(exc))

frozen_source = dict(versioned, strategyEvidence=dict(evidence),
                     exitSpec={"legs": ["TP1", "RUNNER"]})
normal = oos.normalize(frozen_source, 5)
frozen_source["exitSpec"]["legs"].append("MUTATED")
check("OOS normal form is a deep immutable snapshot", normal["exitSpec"]["legs"] == ["TP1", "RUNNER"], normal)

newer = dict(versioned, detectorVersion="REPO2-DETECTOR/2")
groups = oos.grouped_metrics([oos.normalize(versioned, 6), oos.normalize(newer, 7)], partition_versions=True)
check("mixed detector versions are never pooled", len(groups) == 2, groups)

print("ALL PASS (test_oos_ablation)")

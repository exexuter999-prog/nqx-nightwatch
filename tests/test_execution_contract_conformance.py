# -*- coding: utf-8 -*-
"""R15 Python/Worker contract conformance fixtures (no broker/order route)."""
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
import execution_contract  # noqa: E402

NOW = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)


def scenario(**extra):
    value = {"state": "ARMED", "grade": "A", "issuedAt": NOW.isoformat(),
             "expiresAt": (NOW + timedelta(minutes=5)).isoformat(),
             "side": "BUY", "entry": 20000.0, "stop": 19940.0, "qty": 2,
             "targets": [20060.0, 20120.0],
             "legs": [{"id": "TP1", "qty": 1, "target": 20060.0},
                      {"id": "RUNNER", "qty": 1, "target": 20120.0}],
             "planVersion": "R17-SPLIT-1"}
    value.update(extra)
    return value


def market(**extra):
    value = {"at": NOW.isoformat(), "cvdAt": NOW.isoformat()}
    value.update(extra)
    return value


def worker(s, m, p=None, o=None):
    script = (
        "import {evaluateExecutionContract} from './src/state_machine.js';"
        f"console.log(JSON.stringify(evaluateExecutionContract({json.dumps(s)}, {json.dumps(m)},"
        f" {json.dumps(p)}, {json.dumps(o)}, {int(NOW.timestamp() * 1000)})));"
    )
    result = subprocess.run(["node", "--input-type=module", "-e", script], cwd=os.path.join(BASE, "cloudflare"),
                            check=True, capture_output=True, text=True, encoding="utf-8")
    return json.loads(result.stdout)


def check(name, condition, detail=""):
    if not condition:
        raise AssertionError(f"{name}: {detail}")
    print(f"OK   {name}")


def comparable(value):
    return {key: value.get(key) for key in (
        "orderable", "blockers", "caps", "acquisitionRequired", "effectiveGrade",
        "marketAgeSec", "scenarioAgeSec", "riskDollars", "riskCapDollars", "riskCapSource",
    )}


fixtures = [
    ("599", scenario(), market(at=(NOW - timedelta(seconds=599)).isoformat()), None, None),
    ("601", scenario(), market(at=(NOW - timedelta(seconds=601)).isoformat()), None, None),
    ("risk_240", scenario(), market(), None, None),
    ("risk_240_01", scenario(stop=19939.9975), market(), None, None),
    ("qty_0", scenario(qty=0), market(), None, None),
    ("cvd_missing", scenario(grade="A+"), market(cvdAt=None), None, None),
    ("event", scenario(), market(eventBlackout=True), None, None),
    ("session", scenario(sessionEndAt=(NOW - timedelta(seconds=1)).isoformat()), market(), None, None),
    ("position", scenario(), market(), {"state": "OPEN"}, None),
    ("pending", scenario(), market(), None, {"state": "PENDING"}),
    ("sent", scenario(), market(), None, {"state": "SENT"}),
    ("unknown", scenario(), market(), None, {"state": "UNKNOWN"}),
    ("partial", scenario(), market(), None, {"state": "PARTIAL"}),
    ("forged_300", scenario(executionContract={"riskCapDollars": 300, "riskCapSource": "forged"}), market(), None, None),
]

for name, s, m, p, o in fixtures:
    py = execution_contract.evaluate(s, m, p, o, now=NOW)
    js = worker(s, m, p, o)
    check(f"Python/Worker {name} parity", comparable(py) == comparable(js),
          {"python": comparable(py), "worker": comparable(js)})

print("ALL PASS (test_execution_contract_conformance)")

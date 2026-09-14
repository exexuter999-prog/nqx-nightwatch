# -*- coding: utf-8 -*-
"""サイクル・ビーコン(「監視の監視」)の検証。

ネットワークも Cloudflare も使わない。JS 側は node の子プロセスで
本物の applyCycleHealthEvent に通す。

    python tests/test_cycle_beacon.py
"""
import json
import os
import subprocess
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

for _stream in ("stdout", "stderr"):
    _file = getattr(sys, _stream, None)
    if _file is not None and hasattr(_file, "reconfigure"):
        try:
            _file.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

import nqx_cycle   # noqa: E402
import nqx_state   # noqa: E402

PASS = [0]
FAIL = [0]


def check(label, condition, detail=""):
    if condition:
        PASS[0] += 1
        print(f"  OK   {label}")
    else:
        FAIL[0] += 1
        print(f"  NG   {label}  {detail}")


def node_eval(script):
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=os.path.join(BASE, "cloudflare"), timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip()[:400])
    return json.loads(result.stdout.strip())


# ================================================================
print("=" * 68)
print("1. publish_cycle_beacon が組む payload の形")
print("=" * 68)

captured = {}


def _capture_publish(stream, payload, cfg=None, revision=None):
    captured["stream"] = stream
    captured["payload"] = payload
    return True, {"accepted": True}


_original_publish = nqx_state.publish
nqx_state.publish = _capture_publish
try:
    ok, _ = nqx_state.publish_cycle_beacon("BLOCKED", reason="x" * 900, kill=True)
    check("publish が呼ばれ ok が返る", ok)
    check("stream は cycle_health", captured.get("stream") == "cycle_health")
    beacon = (captured.get("payload") or {}).get("beacon") or {}
    check("status が入る", beacon.get("status") == "BLOCKED")
    check("at が ISO で入る", isinstance(beacon.get("at"), str) and "T" in beacon["at"])
    check("reason は 500 文字へ切り詰め", len(beacon.get("reason") or "") == 500)
    check("kill が bool で入る", beacon.get("kill") is True)

    ok, detail = nqx_state.publish_cycle_beacon("EXPLODED")
    check("未知 status は送信せず False", not ok and "unknown" in str(detail.get("reason", "")))
    check("未知 status では publish に到達しない", captured.get("payload", {}).get("beacon", {}).get("status") != "EXPLODED")
finally:
    nqx_state.publish = _original_publish


# ================================================================
print("=" * 68)
print("2. Python の beacon payload を JS の本物の検証器が受理するか")
print("=" * 68)

nqx_state.publish = _capture_publish
try:
    nqx_state.publish_cycle_beacon("HALT", reason="publish failed — HTTP 500", kill=False)
finally:
    nqx_state.publish = _original_publish

event = {"stream": "cycle_health", "revision": 1, "payload": captured["payload"]}
verdict = node_eval(
    "import {applyEvent, emptyState, projectState} from './src/state_machine.js';"
    f"const event = {json.dumps(event)};"
    "const r = applyEvent(emptyState('acct', 'MNQU6'), event, Date.now());"
    "const view = projectState(r.state, Date.now());"
    "console.log(JSON.stringify({accepted: r.accepted, reason: r.reason ?? null,"
    " health: view.cycleHealth}));"
)
check("JS applyCycleHealthEvent が受理する", verdict["accepted"], str(verdict.get("reason")))
check("view.cycleHealth.status が HALT", (verdict.get("health") or {}).get("status") == "HALT")
check("view.cycleHealth.reason が届く",
      (verdict.get("health") or {}).get("reason") == "publish failed — HTTP 500")


# ================================================================
print("=" * 68)
print("3. nqx_cycle.send_beacon は本流を絶対に殺さない")
print("=" * 68)

calls = []


class _FakeState:
    @staticmethod
    def publish_cycle_beacon(status, reason=None, kill=None, cfg=None):
        calls.append({"status": status, "reason": reason, "kill": kill})
        return True, {}


class _FakeArm:
    @staticmethod
    def state(*a, **k):
        return {"kill": False}


_original_arm = sys.modules.get("autotrade_arm")
sys.modules["autotrade_arm"] = _FakeArm()
sys.modules["nqx_state"] = _FakeState()
try:
    nqx_cycle.send_beacon("BLOCKED", "line1\nline2\t  spaced")
    check("send_beacon が publish_cycle_beacon を呼ぶ", len(calls) == 1)
    check("reason の改行・連続空白は畳まれる",
          calls and calls[0]["reason"] == "line1 line2 spaced")

    class _Boom:
        @staticmethod
        def publish_cycle_beacon(*a, **k):
            raise RuntimeError("network down")

    sys.modules["nqx_state"] = _Boom()
    try:
        nqx_cycle.send_beacon("HALT", "x")
        check("publisher が例外でも send_beacon は握って続行", True)
    except Exception as exc:  # noqa: BLE001
        check("publisher が例外でも send_beacon は握って続行", False, repr(exc))
finally:
    sys.modules["nqx_state"] = nqx_state
    if _original_arm is not None:
        sys.modules["autotrade_arm"] = _original_arm
    else:
        sys.modules.pop("autotrade_arm", None)


# ================================================================
print("=" * 68)
total = PASS[0] + FAIL[0]
if FAIL[0]:
    print(f"FAILED {FAIL[0]} / {total}")
    sys.exit(1)
print(f"ALL PASS ({total})")
sys.exit(0)

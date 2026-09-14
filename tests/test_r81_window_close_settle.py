#!/usr/bin/env python3
"""R81: 窓が閉じた後、一度だけ建玉の正本を画面へ反映する。

2026-09-12 05:42:49 に決済した SHORT が、05:45 の窓閉じをまたいで Mini App に
`OPEN` のまま残った。05:42 サイクルはまだ `hold: managed` で、次の 05:45 サイクルは
窓外なので1行出して即 return し、`OPEN -> CLOSED` を publish する reconcile が
一度も走らなかった。翌 07:00 まで存在しない建玉が画面に出ていた。

    python tests/test_r81_window_close_settle.py
"""
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
import nqx_cycle  # noqa: E402

JST = timezone(timedelta(hours=9))
PASS = [0]
FAIL = [0]


def check(label, condition, detail=""):
    if condition:
        PASS[0] += 1
        print(f"  OK   {label}")
    else:
        FAIL[0] += 1
        print(f"  NG   {label}  {detail}")


def closed_sync():
    return True, {"ok": True, "transitions": [{"kind": "position",
                                               "from": "OPEN", "to": "CLOSED"}]}


def flat_sync():
    return True, {"ok": True, "transitions": []}


NOW = datetime(2026, 9, 12, 5, 48, tzinfo=JST)

with tempfile.TemporaryDirectory() as tmp:
    state = Path(tmp) / "window_settle.json"

    calls = []

    def counting():
        calls.append(1)
        return closed_sync()

    note = nqx_cycle.settle_after_window(NOW, state_path=state, sync=counting)
    check("窓外の初回は同期して CLOSED を報告する",
          note == "window settle: position CLOSED" and len(calls) == 1, str(note))
    check("印に JST の日付を残す",
          json.loads(state.read_text(encoding="utf-8")).get("date") == "2026-09-12",
          state.read_text(encoding="utf-8"))

    note = nqx_cycle.settle_after_window(NOW, state_path=state, sync=counting)
    check("同じ窓の2回目以降は同期しない(注記も出さない)",
          note is None and len(calls) == 1, f"{note} calls={len(calls)}")

    note = nqx_cycle.settle_after_window(NOW + timedelta(days=1),
                                         state_path=state, sync=counting)
    check("翌日の窓では再び同期する", note is not None and len(calls) == 2,
          f"{note} calls={len(calls)}")

with tempfile.TemporaryDirectory() as tmp:
    state = Path(tmp) / "window_settle.json"
    note = nqx_cycle.settle_after_window(NOW, state_path=state, sync=flat_sync)
    check("決済が無い窓でも一度は同期して ok を返す",
          note == "window settle: position ok", str(note))

with tempfile.TemporaryDirectory() as tmp:
    state = Path(tmp) / "window_settle.json"
    tries = []

    def failing():
        tries.append(1)
        raise RuntimeError("broker unreachable")

    note = nqx_cycle.settle_after_window(NOW, state_path=state, sync=failing)
    check("同期に失敗したら理由を注記にする",
          note is not None and "window settle failed" in note, str(note))
    check("失敗した周期は印を残さない(次の窓外サイクルが再試行する)",
          not state.exists())
    nqx_cycle.settle_after_window(NOW, state_path=state, sync=failing)
    check("実際に再試行される", len(tries) == 2, f"tries={len(tries)}")

with tempfile.TemporaryDirectory() as tmp:
    state = Path(tmp) / "window_settle.json"

    def unhealthy():
        return False, {"ok": False, "error": "UNVERIFIED"}

    note = nqx_cycle.settle_after_window(NOW, state_path=state, sync=unhealthy)
    check("publish が ok=false なら失敗として扱う",
          note is not None and "window settle failed" in note, str(note))
    check("ok=false でも印は残さない", not state.exists())

# ---- R85: 同じ一度で戦績も記録する(/fills は取引日で閉じるので翌朝では取れない) ----
with tempfile.TemporaryDirectory() as tmp:
    state = Path(tmp) / "window_settle.json"
    journal_calls = []

    def journal():
        journal_calls.append(1)
        return ["trade journal: recorded SHORT 10 @29,463.75 → LEDGER (plan · recorded)"]

    note = nqx_cycle.settle_after_window(NOW, state_path=state, sync=closed_sync,
                                         journal=journal)
    check("窓閉じ後の一度で戦績も記録する",
          note == "window settle: position CLOSED · journal recorded 1"
          and len(journal_calls) == 1, str(note))
    nqx_cycle.settle_after_window(NOW, state_path=state, sync=closed_sync, journal=journal)
    check("戦績も 1 窓 1 回", len(journal_calls) == 1, f"calls={len(journal_calls)}")

with tempfile.TemporaryDirectory() as tmp:
    state = Path(tmp) / "window_settle.json"

    def broken_journal():
        raise RuntimeError("worker 502")

    note = nqx_cycle.settle_after_window(NOW, state_path=state, sync=flat_sync,
                                         journal=broken_journal)
    check("戦績の失敗は注記に出し、建玉の同期結果は壊さない",
          note is not None and note.startswith("window settle: position ok")
          and "trade journal failed" in note, str(note))

with tempfile.TemporaryDirectory() as tmp:
    state = Path(tmp) / "window_settle.json"
    touched = []
    original = nqx_cycle._settle_journal
    nqx_cycle._settle_journal = lambda: touched.append(1) or []
    try:
        nqx_cycle.settle_after_window(NOW, state_path=state, sync=flat_sync)
    finally:
        nqx_cycle._settle_journal = original
    check("sync を注入したテストでは実物の戦績記録を走らせない", not touched)

check("窓の内側は settle を呼ばない前提(in_window が True)",
      nqx_cycle.in_window(datetime(2026, 9, 11, 23, 0, tzinfo=JST)) is True)
check("窓の外(05:48)は in_window が False",
      nqx_cycle.in_window(NOW) is False)

print()
print(f"合計: PASS {PASS[0]} / FAIL {FAIL[0]}")
sys.exit(1 if FAIL[0] else 0)

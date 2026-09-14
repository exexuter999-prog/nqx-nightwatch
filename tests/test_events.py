# -*- coding: utf-8 -*-
"""events.py(経済イベントゲート)と monitor_publish の連動をネットワークなしで検証する。

--refresh(実フィード取得)はここでは呼ばない。フィード形式は fixture で固定する。
"""
import json
import os
import re
import sys
import tempfile
import types
from datetime import datetime, timedelta, timezone

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import events  # noqa: E402

FAILED = []


def check(name, cond, detail=""):
    print(("  OK   " if cond else "  FAIL ") + name + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


def section(title):
    print(f"\n{'=' * 68}\n{title}\n{'=' * 68}")


NOW = datetime(2026, 8, 19, 18, 0, tzinfo=timezone.utc)  # 水曜 14:00 ET

# --- 隔離: キャッシュ・手動ファイルを一時領域へ、判定条件を固定 ---
TMP = tempfile.mkdtemp(prefix="nqx-events-test-")
events.CACHE = os.path.join(TMP, "events_cache.json")
events.MANUAL = os.path.join(TMP, "events_manual.json")
events.COUNTRIES = ["USD"]
events.PRE_MIN = 15.0
events.POST_MIN = 10.0


def write_cache(evts, fetched_at=None):
    with open(events.CACHE, "w", encoding="utf-8") as f:
        json.dump({
            "fetchedAt": (fetched_at or NOW).isoformat(),
            "source": "fixture",
            "events": evts,
        }, f, ensure_ascii=False)


def ev(title, at, impact="High", country="USD", source="ff-calendar"):
    return {"title": title, "country": country, "impact": impact,
            "at": at.isoformat(), "source": source}


section("1. normalize_feed — フィード形式の正規化")
raw = [
    {"title": "FOMC Meeting Minutes", "country": "USD",
     "date": "2026-08-19T14:00:00-04:00", "impact": "High"},
    {"title": "Unemployment Claims", "country": "USD",
     "date": "2026-08-20T08:30:00-04:00", "impact": "Medium"},
    {"title": "bad row", "country": "USD", "date": "not-a-date", "impact": "High"},
    {"title": "", "country": "USD", "date": "2026-08-20T08:30:00-04:00", "impact": "High"},
    "not-a-dict",
    {"title": "Unknown impact", "country": "USD",
     "date": "2026-08-20T09:00:00-04:00", "impact": "Weird"},
]
normalized, skipped = events.normalize_feed(raw)
check("読める行だけ残る", len(normalized) == 3)
check("読めない行は件数で報告", skipped == 3)
check("時刻は UTC に正規化", normalized[0]["at"] == "2026-08-19T18:00:00+00:00")
check("未知の impact は Low 扱い", normalized[2]["impact"] == "Low")

section("2. ブラックアウト窓(High: 前15分〜後10分・境界含む)")
fomc = ev("FOMC Meeting Minutes", NOW)
cases = [
    ("15分前=封鎖", NOW - timedelta(minutes=15), True),
    ("16分前=平常", NOW - timedelta(minutes=16), False),
    ("発表時刻=封鎖", NOW, True),
    ("10分後=封鎖", NOW + timedelta(minutes=10), True),
    ("11分後=平常", NOW + timedelta(minutes=11), False),
]
for name, when, expect in cases:
    hit = events.active_blackout(when, [fomc])
    check(name, (hit is not None) == expect)
hit = events.active_blackout(NOW, [fomc])
check("封鎖情報に windowEnd が付く", hit and "windowEnd" in hit)

section("3. Medium は警告のみ(封鎖しない)")
claims = ev("Unemployment Claims", NOW, impact="Medium")
check("Medium は blackout にならない", events.active_blackout(NOW, [claims]) is None)
check("Medium は warning になる", events.active_warning(NOW, [claims]) is not None)
check("対象外通貨は無視", events.active_blackout(
    NOW, [ev("ECB Rate", NOW, country="EUR")]) is None)
check("Low は relevant で落ちる", events.relevant([ev("minor", NOW, impact="Low")]) == [])

section("4. キャッシュの鮮度と手動イベント")
write_cache([fomc], fetched_at=NOW - timedelta(days=9))
loaded, notes = events.load_events(NOW)
check("9日前のキャッシュは無効", all(e.get("source") == "manual" for e in loaded))
check("陳腐化は notes で報告", any("stale" in n for n in notes))
write_cache([fomc], fetched_at=NOW - timedelta(days=1))
loaded, notes = events.load_events(NOW)
check("1日前のキャッシュは有効", len(loaded) == 1 and not notes)
os.remove(events.CACHE)
loaded, notes = events.load_events(NOW)
check("キャッシュ無しは notes で報告", any("no calendar cache" in n for n in notes))

added = events.add_manual("Powell Speaks", (NOW + timedelta(hours=2)).isoformat(), "High")
check("手動イベントを追加できる", added["source"] == "manual")
loaded, _ = events.load_events(NOW)
check("手動イベントはキャッシュ無しでも生きる", any(e["source"] == "manual" for e in loaded))
check("手動イベントも封鎖判定に乗る", events.active_blackout(
    NOW + timedelta(hours=2), None) is not None)

section("5. current_gate は絶対に例外を漏らさない")
write_cache([fomc], fetched_at=NOW - timedelta(days=1))
gate = events.current_gate(NOW)
check("blackout / upcoming / notes を返す",
      isinstance(gate, dict) and "blackout" in gate and "upcoming" in gate)
events.CACHE = TMP  # ディレクトリを指す壊れた設定でも
gate = events.current_gate(NOW)
check("壊れた設定でも dict を返す", isinstance(gate, dict))
events.CACHE = os.path.join(TMP, "events_cache.json")

section("6. monitor_publish との連動(表示と publish の両方で降格)")
import monitor_publish as mp  # noqa: E402

BARS = [{"t": 1755300000 + i * 180, "o": 30000.0, "h": 30010.0, "l": 29990.0,
         "c": 30000.0 + i, "v": 100} for i in range(12)]
ACTIVE = {"state": "ACTIVE", "side": "BUY", "entry": 30000.0, "stop": 29980.0,
          "target": 30040.0, "qty": 1, "title": "test", "reason": "r",
          "trigger": "t", "invalidation": "i"}


def bundle():
    return {
        "at": "2026-08-19T18:00:00+00:00", "price": 30006.0, "vwap": 30000.0,
        "cvd": 1000, "regime": "MX", "watching": ["合流帯待ち"],
        "scenarios": {"s1": dict(ACTIVE)},
        "snapshot": {"at": "2026-08-19T18:00:00+00:00", "bars": BARS,
                     "vwap_lo": 29950.0, "vwap_hi": 30050.0, "levels": []},
    }


BLACKOUT = {"title": "FOMC Meeting Minutes", "impact": "High", "country": "USD",
            "at": NOW.isoformat(), "source": "ff-calendar",
            "windowStart": (NOW - timedelta(minutes=15)).isoformat(),
            "windowEnd": (NOW + timedelta(minutes=10)).isoformat()}

flat = lambda s: re.sub(r"<[^>]+>", "", s)

mp._event_gate = lambda: {"blackout": BLACKOUT, "warning": None,
                          "upcoming": [dict(BLACKOUT)], "notes": []}
msg = mp.build_monitor_message(bundle(), state_ok=True)
check("封鎖中: ACTIVE でも『発注ボタン 表示中』を出さない", "発注ボタン 表示中" not in msg)
check("封鎖中: 降格の事実を表示", "封鎖中につき降格" in msg)
check("封鎖中: バナーは発注不可", "⚠イベント封鎖(発注不可)" in flat(msg).splitlines()[0])

calls = {}
mp.build_market_payload = lambda b, now=None: {"stub": True}


class _NS:
    class NotConfigured(Exception):
        pass

    @staticmethod
    def load_cloud_env():
        return {}

    @staticmethod
    def publish_cycle(cycle_id, market, scenario, c):
        calls["cycle"] = (cycle_id, market, scenario)
        return True, "ok"

    @staticmethod
    def publish_accounts(c):
        return True, {}

    @staticmethod
    def build_scenario(payload, observed_at=None, ttl_minutes=None, symbol=None):
        calls["state"] = payload.get("state")
        return {"scenarioId": "test-1"}

    @staticmethod
    def publish_scenario(s, c):
        return True, "ok"

    @staticmethod
    def clear_scenario(reason, c):
        return True, "ok"


mp.nqx_state = _NS

# dayguard を密閉する。実台帳(.secrets/day_ledger.jsonl)を読ませると、
# **その日に損切りがあっただけでこのテストが落ちる**(2026-08-21 に実際に発生:
# 冷却15分中は publish_state が ACTIVE を WATCH に降格するため)。
# ここで検証したいのはイベント封鎖であって、当日の損益ではない。
import sys as _sys
_dg = types.ModuleType("dayguard")
_dg.check = lambda **kwargs: {"blocked": False, "reasons": [], "warnings": []}
_sys.modules["dayguard"] = _dg
ok, notes = mp.publish_state(bundle())
check("封鎖中: 正本にも WATCH で publish", ok and calls.get("state") == "WATCH",
      str(notes))
check("封鎖中: 理由が notes に残る", any("event blackout" in n for n in notes))

mp._event_gate = lambda: {"blackout": None, "warning": None, "upcoming": [], "notes": []}
ok, notes = mp.publish_state(bundle())
check("平常時: ACTIVE のまま publish", ok and calls.get("state") == "ACTIVE")
msg = mp.build_monitor_message(bundle(), state_ok=True)
check("平常時: 発注ボタン表示に戻る", "発注ボタン 表示中" in msg)

soon = {"title": "CPI m/m", "impact": "Medium", "country": "USD",
        "at": (datetime.now(timezone.utc) + timedelta(minutes=45)).isoformat(),
        "source": "ff-calendar"}
mp._event_gate = lambda: {"blackout": None, "warning": None,
                          "upcoming": [soon], "notes": []}
msg = mp.build_monitor_message(bundle(), state_ok=True)
check("90分以内の直近イベントが2行目に出る",
      "CPI m/m" in flat(msg).splitlines()[1])
check("詳細に指標行が出る", "指標 " in flat(msg))

mp._event_gate = lambda: {"blackout": None, "warning": None, "upcoming": [],
                          "notes": ["no calendar cache"]}
msg = mp.build_monitor_message(bundle(), state_ok=True)
check("カレンダー未取得は隠さない", "指標カレンダー無効" in flat(msg))

for byte_length in (4095, 4096, 4097):
    japanese = "あ" * (byte_length // 3) + ("a" * (byte_length % 3))
    checked = mp.telegram_utf8_preflight(japanese)
    if byte_length <= 4096:
        check(f"Telegram UTF-8 {byte_length} byte input remains intact", checked == japanese,
              str(len(checked.encode("utf-8"))))
    else:
        check("Telegram UTF-8 4097 byte input is safely bounded",
              len(checked.encode("utf-8")) <= 4096 and "truncated for Telegram" in checked,
              str(len(checked.encode("utf-8"))))

print()
if FAILED:
    print(f"FAILED: {len(FAILED)} -> {FAILED}")
    sys.exit(1)
print(f"合計: 全チェック PASS")
sys.exit(0)

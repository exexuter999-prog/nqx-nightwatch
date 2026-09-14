# -*- coding: utf-8 -*-
"""HTF raw の再取得期限と、確定足への昇格(2026-09-08 の実測に基づく回帰)。

何が起きていたか(実測、JST):
  * 09:00 に取った bars4h.json の最終行は 07:00 始まりの**形成中**の足で、
    close は 09:00 時点の途中値 29,583(実際の 07:00〜11:00 足は 29,704 付近で閉じた)。
  * 旧 `refresh_due` は「raw 最終行 open + 2×step」で判定していたので、次の
    再取得は 15:00。11:00〜15:00 はその途中値が確定足として htfContext に入った。
    1d は同じ規則で丸 1 日遅れる(09-07 16:09 取得のまま 09-09 07:00 まで放置)。
  * monitor_pipeline の `BARS4H_STALE_*` は「最終確定足 open + 2×step」で、23:00 →
    07:00 の 8 時間ギャップを跨ぐ 07:00〜11:00 に毎サイクル偽の警告を出し、データが
    本当に古い 11:00 以降は沈黙した。

ここで固定する不変条件:
  * raw 最終行の close + 60 秒で due(取得時に形成中だった行は、閉じたら取り直す)
  * close 後に取り直しても同じ行が最終行なら backoff(min(step, 1h))で再試行
  * tv_snapshot は HTF raw の最終行を確定足にしない(後続の行が存在する行だけ確定)
  * monitor_pipeline の HTF 鮮度は最終確定足の close で測り、閾値は htf_context と同じ
  * 07:00〜11:00 の偽 STALE は出ず、本当に遅れた frame には STALE が出る
"""
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import htf_context  # noqa: E402
import monitor_pipeline as pipeline  # noqa: E402
import tv_snapshot as TV  # noqa: E402

JST = ZoneInfo("Asia/Tokyo")
FAILED = []


def check(name, condition, detail=""):
    print(("  OK   " if condition else "  FAIL ") + name
          + (f" ({detail})" if detail and not condition else ""))
    if not condition:
        FAILED.append(name)


def jst(y, m, d, hh, mm=0, ss=0):
    return datetime(y, m, d, hh, mm, ss, tzinfo=JST)


def ep(dt):
    return int(dt.timestamp())


# ── TradingView の実グリッド(CME_MINI:MNQ1!, JST) ────────────────────────
# 4h: 07:00 / 11:00 / 15:00 / 19:00 / 23:00(23:00 の足は 06:00 まで続く)。月〜金。

def grid_4h(last_open, count=30):
    opens = []
    day = last_open.replace(hour=0, minute=0, second=0, microsecond=0)
    while len(opens) < count:
        if day.weekday() < 5:
            for hh in (23, 19, 15, 11, 7):
                candidate = day.replace(hour=hh)
                if candidate <= last_open:
                    opens.append(candidate)
        day -= timedelta(days=1)
    return sorted(opens)[-count:]


def tv_rows(opens):
    rows = []
    for index, open_dt in enumerate(opens):
        base = 29000.0 + index * 5.0
        rows.append({"time": ep(open_dt), "open": base, "high": base + 30.0,
                     "low": base - 20.0, "close": base + 10.0, "volume": 1000})
    return rows


PARTIAL_0700 = {"open": 29610.25, "high": 29623.25, "low": 29521.75, "close": 29583.0}
FINAL_0700 = {"open": 29610.25, "high": 29705.5, "low": 29521.75, "close": 29704.25}
FORMING_1100 = {"open": 29704.25, "high": 29712.75, "low": 29661.5, "close": 29674.5}

OPEN_0700 = jst(2026, 9, 8, 7)
OPEN_1100 = jst(2026, 9, 8, 11)
OPEN_2300_PREV = jst(2026, 9, 7, 23)

# 09:00 取得の raw: 最終行 = 形成中の 07:00 足(途中値)
rows_0900 = tv_rows(grid_4h(OPEN_0700))
rows_0900[-1].update(PARTIAL_0700)
# 11:03 に取り直した raw: 07:00 足は最終値、11:00 足が形成中
rows_1103 = tv_rows(grid_4h(OPEN_1100))
rows_1103[-2].update(FINAL_0700)
rows_1103[-1].update(FORMING_1100)
# 23:03 取得の raw: 最終行 = 形成中の 23:00 足(06:00 まで続く 7 時間足)
rows_2303 = tv_rows(grid_4h(OPEN_2300_PREV))


def test_refresh_due_uses_trailing_bar_close():
    written_0900 = ep(jst(2026, 9, 8, 9, 0, 15))
    state = htf_context.refresh_state("4h", rows_0900, ep(jst(2026, 9, 8, 9)), written_0900)
    check("09:00: 形成中の 07:00 足は 11:01 まで due にならない",
          state["due"] is False and state["nextDueAt"] == ep(jst(2026, 9, 8, 11, 1)), str(state))
    check("09:00 の理由は「形成中に取得」", state["reason"] == "TRAILING_BAR_CAPTURED_WHILE_FORMING")
    check("11:02: 07:00 足が閉じたので due(旧規則は 15:00 まで待った)",
          htf_context.refresh_due("4h", rows_0900, ep(jst(2026, 9, 8, 11, 2)), written_0900) is True)
    check("11:00:30: 猶予 60 秒の内側はまだ due でない",
          htf_context.refresh_due("4h", rows_0900, ep(jst(2026, 9, 8, 11, 0, 30)), written_0900) is False)
    check("written_at 無し(互換呼び出し)でも close + 猶予で判定",
          htf_context.refresh_due("4h", rows_0900, ep(jst(2026, 9, 8, 9))) is False and
          htf_context.refresh_due("4h", rows_0900, ep(jst(2026, 9, 8, 11, 2))) is True)

    written_1103 = ep(jst(2026, 9, 8, 11, 3, 20))
    check("11:05: 取り直した直後(11:00 形成中)は due でない",
          htf_context.refresh_due("4h", rows_1103, ep(jst(2026, 9, 8, 11, 5)), written_1103) is False)
    check("15:02: 11:00 足が閉じたので due",
          htf_context.refresh_due("4h", rows_1103, ep(jst(2026, 9, 8, 15, 2)), written_1103) is True)


def test_long_session_bar_backs_off_instead_of_storming():
    written_2303 = ep(jst(2026, 9, 7, 23, 3))
    check("03:02: 23:00 足の t+step(03:00)+猶予を過ぎたら一度 due",
          htf_context.refresh_due("4h", rows_2303, ep(jst(2026, 9, 8, 3, 2)), written_2303) is True)
    written_0303 = ep(jst(2026, 9, 8, 3, 3))   # 取り直したが 23:00 足がまだ最終行(06:00 まで続く)
    state = htf_context.refresh_state("4h", rows_2303, ep(jst(2026, 9, 8, 3, 30)), written_0303)
    check("03:30: close 後に取り直して同じ行なら backoff(毎サイクル pane 切替しない)",
          state["due"] is False and state["reason"] == "TRAILING_BAR_STILL_OPEN_AFTER_SETTLE", str(state))
    check("04:04: backoff(4h は 1h)後に再試行",
          htf_context.refresh_due("4h", rows_2303, ep(jst(2026, 9, 8, 4, 4)), written_0303) is True)


def test_other_frames_follow_the_same_rule():
    hourly = tv_rows([OPEN_1100 - timedelta(hours=i) for i in range(29, -1, -1)])
    written = ep(jst(2026, 9, 8, 11, 0, 25))
    check("1h: 11:00 取得(11:00 形成中)は 11:59 に due でない",
          htf_context.refresh_due("1h", hourly, ep(jst(2026, 9, 8, 11, 59)), written) is False)
    check("1h: 12:02 に due(旧規則は 13:00)",
          htf_context.refresh_due("1h", hourly, ep(jst(2026, 9, 8, 12, 2)), written) is True)

    days = []
    day = jst(2026, 9, 7, 7)
    while len(days) < 30:
        if day.weekday() < 5:
            days.append(day)
        day -= timedelta(days=1)
    daily = tv_rows(sorted(days))
    written_1d = ep(jst(2026, 9, 7, 16, 9, 36))   # 実測: 09-07 16:09 取得のまま翌日まで放置されていた
    check("1d: 09-07 07:00 形成中を 16:09 に取った raw は 09-08 07:03 に due(旧規則は 09-09 07:00)",
          htf_context.refresh_due("1d", daily, ep(jst(2026, 9, 8, 7, 3)), written_1d) is True and
          htf_context.refresh_due("1d", daily, ep(jst(2026, 9, 8, 6, 30)), written_1d) is False)


def test_due_frames_reads_file_mtime():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "bars4h.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"bars": rows_0900}, fh)
        mtime = jst(2026, 9, 8, 9, 0, 15).timestamp()
        os.utime(path, (mtime, mtime))
        check("09:00: ファイル mtime を使って 4h は due でない(他 3 frame は欠落で due)",
              "4h" not in htf_context.due_frames(tmp, ep(jst(2026, 9, 8, 9))) and
              set(htf_context.due_frames(tmp, ep(jst(2026, 9, 8, 9)))) == {"45m", "1h", "1d"})
        check("11:02: 4h が due に入る",
              "4h" in htf_context.due_frames(tmp, ep(jst(2026, 9, 8, 11, 2))))
        detail = htf_context.due_states(tmp, ep(jst(2026, 9, 8, 11, 2)))["4h"]
        check("due_states は writtenAt / settleAt / nextDueAt を返す",
              detail["writtenAt"] == round(mtime, 3) and detail["settleAt"] == ep(jst(2026, 9, 8, 11, 1)))


def test_snapshot_never_promotes_a_bar_fetched_while_forming():
    now_1102 = ep(jst(2026, 9, 8, 11, 2))
    _, confirmed = TV.normalize_bars({"bars": rows_0900}, 14400, now_1102, trailing_forming=True)
    check("11:02: 09:00 取得の raw では 07:00 足(途中値)を確定にしない → 最終確定は 23:00",
          confirmed[-1]["t"] == ep(OPEN_2300_PREV), str(confirmed[-1]))
    _, legacy = TV.normalize_bars({"bars": rows_0900}, 14400, now_1102)
    check("(対照)時刻だけの判定だと 07:00 の途中値 29,583 が確定足に昇格していた",
          legacy[-1]["t"] == ep(OPEN_0700) and legacy[-1]["c"] == 29583.0)
    _, refetched = TV.normalize_bars({"bars": rows_1103}, 14400, ep(jst(2026, 9, 8, 11, 5)),
                                     trailing_forming=True)
    check("取り直した raw では 07:00 足が最終値 29,704.25 で確定し、11:00 足は形成中",
          refetched[-1]["t"] == ep(OPEN_0700) and refetched[-1]["c"] == 29704.25)
    _, long_bar = TV.normalize_bars({"bars": rows_2303}, 14400, ep(jst(2026, 9, 8, 3, 30)),
                                    trailing_forming=True)
    check("03:30: 06:00 まで続く 23:00 足は t+step を過ぎても確定にしない → 最終確定は 19:00",
          long_bar[-1]["t"] == ep(jst(2026, 9, 7, 19)), str(long_bar[-1]))

    frame_1102 = htf_context.build_context({"4h": confirmed}, now_1102)["frames"]["4h"]
    check("11:02: 最終確定 23:00(close 03:00)は 8h02m 経過でも FRESH(再取得 1 サイクル分の余裕)",
          frame_1102["freshness"] == "FRESH" and frame_1102["valid"] is True, str(frame_1102))
    frame_1115 = htf_context.build_context({"4h": confirmed}, ep(jst(2026, 9, 8, 11, 15)))["frames"]["4h"]
    check("11:15: 取り直せていなければ STALE(本当に遅れている)",
          frame_1115["freshness"] == "STALE" and frame_1115["valid"] is False, str(frame_1115))
    check("FRAME_MAX_AGE は 2×step + 余裕(monitor_pipeline の既定と同じ物差し)",
          htf_context.FRAME_MAX_AGE["4h"] == 2 * 14400 + htf_context.REFRESH_SLACK_SEC and
          pipeline.DEFAULT_MAX_AGE_SEC["bars4h"] == htf_context.FRAME_MAX_AGE["4h"])


# ── monitor_pipeline: HTF 鮮度は最終確定足の close で測る ─────────────────

def three_minute_rows(clock):
    end = int(clock.timestamp() // 180 * 180)
    rows = []
    for index in range(16):
        open_ = 20000.00 + index * 0.25
        rows.append({"t": end - (15 - index) * 180, "o": open_, "h": open_ + 1.00,
                     "l": open_ - 1.00, "c": open_ + 0.25, "v": 100 + index})
    return rows


def run_validation(clock, bars4h):
    rows = three_minute_rows(clock)
    raw = {
        "at": clock.isoformat(), "priceAt": clock.isoformat(), "price": rows[-1]["c"],
        "sourceSymbol": "CME_MINI:MNQU2026", "priceSource": "TradingView MCP file handoff",
        "snapshot": {
            "bars3m": rows, "bars15m": rows[::3], "bars4h": bars4h,
            "levels": [{"label": "VAH", "price": 20010.00}, {"label": "VAL", "price": 19990.00}],
            "rangeAnchor": {"rangeTf": "15m",
                            "rangeStart": (clock - timedelta(minutes=45)).isoformat(),
                            "rangeEnd": clock.isoformat(), "anchorType": "HTF_DIRECTIONAL_LEG",
                            "freshness": "FRESH", "high": 20020.00, "low": 19980.00},
            "sessionId": "NY-TEST", "peers": {}, "peerMeta": {},
            "eventGate": {"state": "NONE", "checkedAt": clock.isoformat()},
        },
        "cvd": None, "cvdMeta": {"status": "MISSING", "at": clock.isoformat()},
        "acquisitionReceipt": {
            "schemaVersion": "NQX_ACQUISITION_RECEIPT/1", "requiredFresh": True, "staleRequired": [],
            "sources": {name: {"required": True, "status": "FRESH"}
                        for name in ("chart_state.json", "bars3m.json", "study_3m.json", "pine_labels.json")},
        },
    }
    with tempfile.TemporaryDirectory() as tmp:
        snapshot_path = os.path.join(tmp, "snapshot.json")
        with open(snapshot_path, "w", encoding="utf-8") as fh:
            json.dump(raw, fh)
        config_path = os.path.join(tmp, "config.json")
        with open(config_path, "w", encoding="utf-8") as fh:
            json.dump({
                "schemaVersion": pipeline.SCHEMA_VERSION, "symbol": "MNQU6",
                "provider": {"type": "file", "snapshotInputPath": snapshot_path,
                             "cvdRetryInputPath": os.path.join(tmp, "retry.json")},
                "output": {"cyclePath": os.path.join(tmp, "cycle.json"),
                           "publishBundlePath": os.path.join(tmp, "bundle.json")},
                "execution": {"mode": "DRY_RUN_ONLY"},
            }, fh)
        result = pipeline.run_pipeline(config_path, now=clock.astimezone(timezone.utc), write=False)
    return result["validation"]


def test_pipeline_measures_htf_staleness_from_the_last_confirmed_close():
    _, confirmed_0900 = TV.normalize_bars({"bars": rows_0900}, 14400, ep(jst(2026, 9, 8, 9)),
                                          trailing_forming=True)
    snapshot_rows = [{"t": b["t"], "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"], "v": b["v"]}
                     for b in confirmed_0900]
    at_0900 = run_validation(jst(2026, 9, 8, 9), snapshot_rows)
    check("09:00: 最終確定 23:00 でも BARS4H_STALE を出さない(07:00〜11:00 の偽警告)",
          not any(code.startswith("BARS4H_STALE") for code in at_0900["missing"] + at_0900["stale"]),
          str(at_0900))
    at_1102 = run_validation(jst(2026, 9, 8, 11, 2), snapshot_rows)
    check("11:02: 再取得前の 1 サイクルは余裕の内側で STALE を出さない",
          not any(code.startswith("BARS4H_STALE") for code in at_1102["missing"] + at_1102["stale"]),
          str(at_1102))
    at_1115 = run_validation(jst(2026, 9, 8, 11, 15), snapshot_rows)
    check("11:15: 取り直せていなければ BARS4H_STALE が出る(本当に遅れている)",
          any(code.startswith("BARS4H_STALE") for code in at_1115["missing"]) and
          any(code.startswith("BARS4H_STALE") for code in at_1115["stale"]), str(at_1115))
    check("HTF の STALE は blocking に入らない(従来どおり非ブロッキング)",
          not any("BARS4H" in code for code in at_1115["blocking"]))
    check("欠落は _MISSING のまま", "BARS1D_MISSING" in at_1115["missing"])


test_refresh_due_uses_trailing_bar_close()
test_long_session_bar_backs_off_instead_of_storming()
test_other_frames_follow_the_same_rule()
test_due_frames_reads_file_mtime()
test_snapshot_never_promotes_a_bar_fetched_while_forming()
test_pipeline_measures_htf_staleness_from_the_last_confirmed_close()

if FAILED:
    print(f"\nFAILED ({len(FAILED)}): " + ", ".join(FAILED))
    sys.exit(1)
print("\nALL PASS (test_htf_refresh_due)")

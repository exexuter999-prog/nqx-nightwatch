# -*- coding: utf-8 -*-
"""ICTBACK の Silver Bullet 実装契約。

動画の紙上レシピを1分足へ写し、3分足への代用・look-ahead・未検証limit fill・
現行2枚split契約のすり抜けが無いことを確認する。
"""
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import msnr_gate  # noqa: E402
import execution_contract  # noqa: E402
execution_contract.CONTRACT["contract"] = {  # R102: テストは限月を固定する(ロール後も壊れない)
    "symbol": "MNQU6", "tvSymbol": "CME_MINI:MNQU2026", "expiry": "2026-09-18",
    "lastEntryDaysBeforeExpiry": 3}


FAILED = []


def check(name, condition, detail=""):
    print(("  OK   " if condition else "  FAIL ") + name +
          (f" ({detail})" if detail and not condition else ""))
    if not condition:
        FAILED.append(name)


def make_bundle(extra=None):
    start = 1_780_000_000
    bars = []
    for index in range(14):
        bars.append({"t": start + index * 60, "o": 105.0, "h": 105.5,
                     "l": 104.5, "c": 105.0})
    bars.extend([
        # SSL 100 を 1 tick 以上掃引して、100 より上で確定。
        {"t": start + 14 * 60, "o": 102.0, "h": 102.5, "l": 99.5, "c": 101.5},
        # Sweep後のbullish displacement impulse。
        {"t": start + 15 * 60, "o": 101.5, "h": 104.0, "l": 101.25, "c": 103.75},
        # 1m bullish FVG: 102.50–103.00。
        {"t": start + 16 * 60, "o": 103.75, "h": 104.75, "l": 103.0, "c": 104.25},
        {"t": start + 17 * 60, "o": 104.25, "h": 104.75, "l": 104.0, "c": 104.5},
    ])
    if extra:
        bars.extend(extra)
    at = datetime.fromtimestamp(start + len(bars) * 60, tz=timezone.utc).isoformat()
    return {
        "at": at, "priceAt": at, "price": bars[-1]["c"],
        "snapshot": {"bars1m": bars},
    }


def test_missing_1m_does_not_fallback_to_3m():
    bundle = {"at": "2026-09-03T00:00:00+00:00",
              "snapshot": {"bars3m": [{"t": i * 180, "o": 100, "h": 101,
                                         "l": 99, "c": 100} for i in range(60)]}}
    out = msnr_gate.silver_bullet_evaluation(bundle, [{"label": "SSL", "price": 100}], 8.0)
    check("1m missing is explicit", out["reason"] == "SB_1M_SOURCE_MISSING", out)
    check("3m is never relabeled as 1m", out["candidate"] is None, out)


def test_as_traded_candidate_geometry_and_safety_blockers():
    out = msnr_gate.silver_bullet_evaluation(
        make_bundle(), [{"label": "Test SSL", "price": 100.0}], 1.0)
    candidate = out.get("candidate") or {}
    check("sweep then 1m FVG is detected", out["status"] == "CANDIDATE", out)
    check("entry is FVG midpoint", candidate.get("entry") == 102.75, candidate)
    check("target is exactly 2R", candidate.get("targetR") == [2.0], candidate)
    check("FVG timeframe is 1m", (candidate.get("fvg") or {}).get("timeframe") == "1m", candidate)
    check("15m bias is absent", "SB_NO_15M_BIAS" in candidate.get("evidence", []), candidate)
    check("paper fill is not live-authorized",
          "SB_LIVE_FILL_UNVALIDATED" in candidate.get("hardBlockers", []) and
          candidate.get("state") == "WATCH", candidate)
    check("fixed 2R is not forged into a runner",
          "SB_FIXED_2R_SINGLE_TARGET" in candidate.get("hardBlockers", []) and
          len(candidate.get("targets", [])) == 1, candidate)


def test_touch_is_not_trade_through_and_full_fill_invalidates():
    touched_only = make_bundle([{
        "t": 1_780_000_000 + 18 * 60, "o": 103.0, "h": 103.5,
        "l": 102.75, "c": 103.0,
    }])
    out = msnr_gate.silver_bullet_evaluation(
        touched_only, [{"label": "Test SSL", "price": 100.0}], 1.0)
    candidate = out.get("candidate") or {}
    check("touch at midpoint does not count as fill",
          candidate and (candidate.get("fvg") or {}).get("arrivalState") == "TOUCHED_NOT_FILLED", candidate)

    full_fill = make_bundle([{
        "t": 1_780_000_000 + 18 * 60, "o": 103.0, "h": 103.5,
        "l": 102.5, "c": 102.75,
    }])
    invalid = msnr_gate.silver_bullet_evaluation(
        full_fill, [{"label": "Test SSL", "price": 100.0}], 1.0)
    check("full FVG fill removes the pending candidate",
          invalid.get("candidate") is None, invalid)


def test_1m_evaluation_input_is_not_capped_to_the_chart_feed():
    """1分足の評価入力を Mini App のチャート枠(60本)で切らないこと。

    R13 パイプラインは compact の**後**に評価を呼ぶ。取り込みと compact の
    両方が FEED_BARS_MAX へ切っていたため、SILVER_BULLET_LOOKBACK_BARS=90 は
    一度も届かず、liquidity level の母集団だけが静かに縮んでいた。しかも
    `bars1mOriginalCount` は切った**後**の本数を数えており、切り詰めを
    観測するために足したはずの監査値が常に 60 を返していた。
    """
    import monitor_pipeline
    import monitor_publish

    start = 1_780_000_000
    bars1m = [{"t": start + i * 60, "o": 100.0, "h": 100.5, "l": 99.5, "c": 100.0}
              for i in range(240)]
    bars3m = [{"t": start + i * 180, "o": 100.0, "h": 100.5, "l": 99.5, "c": 100.0}
              for i in range(240)]
    raw = {"bundle": {"at": "2026-09-03T00:00:00+00:00",
                      "priceAt": "2026-09-03T00:00:00+00:00", "price": 100.0,
                      "sourceSymbol": "MNQU6", "priceSource": "test",
                      "snapshot": {"bars1m": list(bars1m), "bars3m": bars3m}}}
    ingested = monitor_pipeline._source_bundle(raw, {"maxAgeSec": {}, "symbol": "MNQU6"})
    kept = ingested["snapshot"]["bars1m"]
    check("取り込みが1分足をチャート枠へ切らない",
          len(kept) >= msnr_gate.SILVER_BULLET_LOOKBACK_BARS, len(kept))

    compact = monitor_publish.compact({"at": "2026-09-03T00:00:00+00:00",
                                       "snapshot": {"bars": bars3m, "bars3m": bars3m,
                                                    "bars1m": kept}})
    check("compact も lookback を割らない",
          len(compact["snapshot"]["bars1m"]) >= msnr_gate.SILVER_BULLET_LOOKBACK_BARS,
          len(compact["snapshot"]["bars1m"]))
    check("bars1mOriginalCount は切る前の本数",
          compact["snapshot"]["bars1mOriginalCount"] == 240,
          compact["snapshot"]["bars1mOriginalCount"])
    check("Mini App の3分チャート枠は従来どおり",
          len(monitor_publish.normalize_market_bars(bars3m)) == monitor_publish.FEED_BARS_MAX)


def test_broken_optional_1m_raw_does_not_kill_the_3m_route():
    """任意の研究入力1本で、必須の3m取得ごと落ちないこと。

    `normalize_bars` は間隔不一致を AcquireError で弾く。bars1m.json は任意
    ソースなので、3m の応答を誤って保存した/前サイクルの残骸が残っただけで
    acquire 全体が毎サイクル止まってはならない。落としたことは黙って隠さず、
    「未取得」と区別できる形で残す。
    """
    import tv_snapshot as TV

    t0 = 1787302800
    n = 240
    now = datetime.fromtimestamp(t0 + n * 180, timezone.utc)
    bars3m = {"bars": [{"time": t0 + i * 180, "open": 29400 + (i % 5),
                        "high": 29410 + (i % 5), "low": 29390 + (i % 5),
                        "close": 29400 + ((i + 2) % 5), "volume": 1000}
                       for i in range(n)]}
    # 3分足の応答を bars1m.json へ保存してしまった状態。
    wrong_1m = {"bars": [{"time": t0 + i * 180, "open": 29400, "high": 29410,
                          "low": 29390, "close": 29400, "volume": 10}
                         for i in range(30)]}
    with tempfile.TemporaryDirectory() as tmp:
        for name, payload in (
            ("chart_state", {"symbol": "CME_MINI:MNQU2026", "resolution": "3"}),
            ("bars3m", bars3m), ("bars1m", wrong_1m),
            ("study_3m", {"studies": [{"name": "CVD Unified", "values": {"CVD": "1,000"}}]}),
            ("pine_labels", {"studies": [{"name": "Sessions & VP", "labels": [
                {"text": "C: VAH", "price": 29486.65},
                {"text": "C: VAL", "price": 29318.92},
                {"text": "C: POC", "price": 29402.78}]}]}),
            ("quote", {"last": 29370.0}),
        ):
            (Path(tmp) / f"{name}.json").write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        try:
            bundle = TV.build_bundle(Path(tmp), now=now)
        except TV.AcquireError as exc:
            check("壊れた任意1分足で3m取得が落ちない", False, str(exc))
            return
    snapshot = bundle["snapshot"]
    check("壊れた任意1分足で3m取得が落ちない", len(snapshot["bars3m"]) >= 60)
    check("拒否した1分足を無言で snapshot へ入れない", "bars1m" not in snapshot, snapshot.keys())
    check("拒否理由を残す",
          "60s" in str((snapshot.get("barsRejected") or {}).get("bars1m") or ""),
          snapshot.get("barsRejected"))
    check("未取得と拒否を区別する",
          msnr_gate.silver_bullet_evaluation(bundle, snapshot["levels"], 8.0)["reason"]
          == "SB_1M_REJECTED")


def test_broken_optional_htf_raw_does_not_kill_the_3m_route():
    """15m / 45m / 1h / 4h / 1D も同じ穴を持たないこと。

    CLAUDE.md §1 は「HTF欠落は3分足で補間せず INSUFFICIENT として無得点」。
    間隔違いの任意 HTF ファイル1本で acquire 全体が落ちるのは、この契約と逆で
    ある。落とすのは当該フレームだけで、理由は barsRejected に残す。
    """
    import tv_snapshot as TV

    t0 = 1787302800
    n = 240
    now = datetime.fromtimestamp(t0 + n * 180, timezone.utc)
    bars3m = {"bars": [{"time": t0 + i * 180, "open": 29400, "high": 29410,
                        "low": 29390, "close": 29400, "volume": 1000}
                       for i in range(n)]}
    # 4時間足のファイルに1時間足の応答が入っている状態。
    wrong_4h = {"bars": [{"time": t0 + i * 3600, "open": 29400, "high": 29410,
                          "low": 29390, "close": 29400, "volume": 10}
                         for i in range(30)]}
    with tempfile.TemporaryDirectory() as tmp:
        for name, payload in (
            ("chart_state", {"symbol": "CME_MINI:MNQU2026", "resolution": "3"}),
            ("bars3m", bars3m), ("bars4h", wrong_4h),
            ("study_3m", {"studies": [{"name": "CVD Unified", "values": {"CVD": "1,000"}}]}),
            ("pine_labels", {"studies": [{"name": "Sessions & VP", "labels": [
                {"text": "C: VAH", "price": 29486.65},
                {"text": "C: VAL", "price": 29318.92},
                {"text": "C: POC", "price": 29402.78}]}]}),
            ("quote", {"last": 29370.0}),
        ):
            (Path(tmp) / f"{name}.json").write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        try:
            bundle = TV.build_bundle(Path(tmp), now=now)
        except TV.AcquireError as exc:
            check("壊れた任意HTFで3m取得が落ちない", False, str(exc))
            return
    snapshot = bundle["snapshot"]
    check("壊れた任意HTFで3m取得が落ちない", len(snapshot["bars3m"]) >= 60)
    check("拒否したHTFを無言で snapshot へ入れない", not snapshot.get("bars4h"), snapshot.get("bars4h"))
    check("HTFの拒否理由も残す",
          "14400s" in str((snapshot.get("barsRejected") or {}).get("bars4h") or ""),
          snapshot.get("barsRejected"))


def test_daily_bars_survive_compact_for_the_evaluator():
    """compact が評価前に bars1d を消していないこと。

    `msnr_gate._daily_bars` は R48 の `CLASSIC_TS` と `decision_context.gap`
    の唯一の入力で、生の日足を直接読む唯一の HTF 系列である。R13 も
    monitor_publish.main も compact の**後**に評価するため、ここで pop すると
    両方が永久に不発になる(監査896サイクルで実測0件)。
    """
    import monitor_publish

    day = 86400
    t0 = 1_780_000_000 - 60 * day
    bars1d = []
    for i in range(60):
        # 20日窓の安値を6セッション前に置く(Raschke原典: 旧極値が4session以上前)。
        low = 29000.0 + (0.0 if i == 53 else 25.0 + (i % 7))
        bars1d.append({"t": t0 + i * day, "o": low + 20, "h": low + 40,
                       "l": low, "c": low + 10})
    bars3m = [{"t": 1_780_000_000 + i * 180, "o": 100.0, "h": 100.5, "l": 99.5, "c": 100.0}
              for i in range(60)]
    bundle = {"at": "2026-09-03T00:00:00+00:00",
              "snapshot": {"bars": bars3m, "bars3m": bars3m, "bars1d": bars1d}}
    level = {"price": 29000.0, "label": "PDL"}
    chain = {"type": "SWEEP", "side": "BUY", "sweepBarT": bars3m[-3]["t"]}

    check("素材が CLASSIC_TS を満たす(前提)",
          "CLASSIC_TS" in msnr_gate.feature_tags(chain, level, bars3m, bundle))
    compact = monitor_publish.compact(bundle)
    check("compact 後も日足が読める",
          len(msnr_gate._daily_bars(compact)) >= msnr_gate.CLASSIC_TS_LOOKBACK_DAYS,
          len(msnr_gate._daily_bars(compact)))
    check("compact 後も CLASSIC_TS が立つ",
          "CLASSIC_TS" in msnr_gate.feature_tags(chain, level, bars3m, compact))
    check("compact 後も gap bucket が出る",
          (msnr_gate.decision_context(compact, {}) or {}).get("gap") is not None,
          msnr_gate.decision_context(compact, {}))
    check("件数の監査値は残る", compact["snapshot"].get("bars1dCount") == 60,
          compact["snapshot"].get("bars1dCount"))
    check("他のHTF生足は従来どおり落とす",
          all(key not in compact["snapshot"]
              for key in ("bars15m", "bars45m", "bars1h", "bars4h")))


def test_sb_off_switch_does_not_fail_open_on_repo_standard_spellings():
    """停止スイッチが綴り違いで無言のまま ON に戻らないこと。"""
    previous = os.environ.get("NQX_SB_MODE")
    try:
        for value in ("OFF", "off", "0", "false", "no"):
            os.environ["NQX_SB_MODE"] = value
            check(f"NQX_SB_MODE={value} は OFF", msnr_gate.silver_bullet_mode() == "OFF")
        os.environ["NQX_SB_MODE"] = "CANDIDATE"
        check("CANDIDATE は候補のまま", msnr_gate.silver_bullet_mode() == "CANDIDATE")
    finally:
        if previous is None:
            os.environ.pop("NQX_SB_MODE", None)
        else:
            os.environ["NQX_SB_MODE"] = previous


def test_compact_keeps_explicit_1m_evidence():
    import monitor_publish
    bundle = make_bundle()
    bundle["snapshot"]["bars"] = [{"t": 1, "o": 100, "h": 101, "l": 99, "c": 100}]
    compact = monitor_publish.compact(bundle)
    check("compact preserves bars1m", len(compact["snapshot"].get("bars1m") or []) == 18,
          compact["snapshot"])


test_missing_1m_does_not_fallback_to_3m()
test_as_traded_candidate_geometry_and_safety_blockers()
test_touch_is_not_trade_through_and_full_fill_invalidates()
test_1m_evaluation_input_is_not_capped_to_the_chart_feed()
test_broken_optional_1m_raw_does_not_kill_the_3m_route()
test_broken_optional_htf_raw_does_not_kill_the_3m_route()
test_daily_bars_survive_compact_for_the_evaluator()
test_sb_off_switch_does_not_fail_open_on_repo_standard_spellings()
test_compact_keeps_explicit_1m_evidence()

if FAILED:
    print(f"FAILED: {len(FAILED)} -> {', '.join(FAILED)}")
    raise SystemExit(1)
print("Silver Bullet PASS")

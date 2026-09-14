# -*- coding: utf-8 -*-
"""R28 TradingView 取得の機械化。

なぜ作ったか(実測):
  監視ループは LLM のエージェント・ターンで、バンドル JSON はエージェントが
  **手で**書き写していた。その結果、実サイクル 403 本で
    rangeAnchor 0%  /  peers 0%  /  po3 0%  /  bars15m 0%
    cvd は 401 本あるが **全て裸の int** で _cvd_score は dict の bias を要求
  という欠落が生じ、392 候補すべてが NO_CONFIRMATION で止まっていた。

  tv_snapshot.py は MCP の**生出力だけ**から決定論的にバンドルを組む。
  エージェントの裁量は「どのツールを呼ぶか」だけに縮む。

実チャートで確認した罠(2026-08-24、CME_MINI:MNQ1!):
  * study 値の負号は U+2212(−)。ASCII の - ではない。
  * 数値は "59,999" のようにカンマ入り文字列。
  * NQX_DATA_*_SOURCE_TIME は ms epoch、bars の time は秒。
  * pine labels は過去セッション分も同名で返る(52 ラベル中 "C: POC" が 10 回)。
    **最後の出現**が現行セッション。
  * 市場が閉じていると足が 2 日古い。鮮度ゲートは fail-closed で止める。

ここで固定する不変条件:
  * 表示文字列(カンマ・U+2212)が数値へ正しく落ちる
  * 秒/ms のどちらの epoch も秒に揃う
  * 形成中の最終足を確定足に混ぜない
  * 同名ラベルは最後の出現が勝つ
  * CVD は **方向付きの dict** になる。EMA と table が食い違えば bias を付けない
  * po3 は 4 つの許容値か None。分類できないときに当て推量しない
  * セッション高安は窓が開始を覆っているときだけ出す
  * MNQ 以外・足不足・古すぎる足は fail-closed で止める
"""
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import tv_snapshot as TV  # noqa: E402

FAILED = []


def check(name, condition, detail=""):
    print(("  OK   " if condition else "  FAIL ") + name
          + (f" ({detail})" if detail and not condition else ""))
    if not condition:
        FAILED.append(name)


# ── 表示文字列の正規化 ────────────────────────────────────────────────────
def test_display_numbers_parse():
    check("カンマ入り", TV._num("59,999") == 59999.0)
    check("U+2212 の負号", TV._num("−1.00") == -1.0)
    check("ASCII の負号も通る", TV._num("-1.00") == -1.0)
    check("カンマ + 小数", TV._num("29,530.70") == 29530.70)
    check("空と n/a は None", TV._num("") is None and TV._num("n/a") is None)
    check("None は None", TV._num(None) is None)
    check("数値はそのまま", TV._num(37.93) == 37.93)


def test_epoch_units():
    check("秒はそのまま", TV._epoch_sec(1787345820) == 1787345820)
    check("ms は秒へ", TV._epoch_sec("1,787,345,820,000.00") == 1787345820)
    check("読めなければ None", TV._epoch_sec("x") is None)


# ── 足 ───────────────────────────────────────────────────────────────────
def bars_payload(n=10, t0=1000, step=180):
    return {"bars": [{"time": t0 + i * step, "open": 100 + i, "high": 105 + i,
                      "low": 95 + i, "close": 102 + i, "volume": 10}
                     for i in range(n)]}


def test_forming_bar_is_dropped():
    payload = bars_payload(5)
    last_t = 1000 + 4 * 180
    raw, confirmed = TV.normalize_bars(payload, 180, last_t + 100)   # 最終足は形成中
    check("生は全部返る", len(raw) == 5)
    check("確定足から形成中が落ちる", len(confirmed) == 4, str(len(confirmed)))
    _, closed = TV.normalize_bars(payload, 180, last_t + 200)        # close 済み
    check("close 後は確定足に入る", len(closed) == 5)


def test_wrong_timeframe_file_is_rejected():
    """15分足を bars3m.json に保存したら、そこで止まる(R41)。

    実測(2026-08-25): chart_get_state が resolution "3" を返している最中に
    data_get_ohlcv が 900 秒足を返した。step_sec は形成中足の切り落としに
    しか使われていなかったので、そのまま 240 本の 15 分足が「3分足の確定足」
    として noiseFloor と構造判定へ流れ込む。
    """
    fifteen = bars_payload(20, step=900)
    raised = None
    try:
        TV.normalize_bars(fifteen, 180, 9_999_999_999)
    except TV.AcquireError as exc:
        raised = str(exc)
    check("15分足を3分足として読むと止まる", raised is not None, "no raise")
    check("理由に実測と期待の両方が出る",
          bool(raised) and "900" in raised and "180" in raised, str(raised))
    # 正しい足はそのまま通る。
    ok = TV.normalize_bars(bars_payload(20, step=180), 180, 9_999_999_999)
    check("3分足はそのまま通る", len(ok[0]) == 20)


def test_session_gap_does_not_trip_spacing_check():
    """週末・保守時間の飛びで誤検出しない。最頻差だけを見る。"""
    rows = bars_payload(10, t0=1000, step=180)["bars"]
    rows += bars_payload(10, t0=1000 + 10 * 180 + 3600, step=180)["bars"]
    raw, _ = TV.normalize_bars({"bars": rows}, 180, 9_999_999_999)
    check("セッション境界の飛びは通す", len(raw) == 20, str(len(raw)))


def test_spacing_check_needs_enough_samples():
    """足が2本以下では判定しない(母数不足で誤って止めない)。"""
    two = {"bars": bars_payload(2, step=900)["bars"]}
    raw, _ = TV.normalize_bars(two, 180, 9_999_999_999)
    check("2本では判定しない", len(raw) == 2)


def test_broken_bars_are_discarded():
    payload = {"bars": [
        {"time": 1000, "open": 100, "high": 90, "low": 95, "close": 102, "volume": 1},  # 高値<実体
        {"time": 1180, "open": 100, "high": 105, "low": 95, "close": 102, "volume": 1},
    ]}
    raw, _ = TV.normalize_bars(payload, 180, 9999)
    check("壊れた足は捨てる", len(raw) == 1 and raw[0]["t"] == 1180)


# ── レベル ───────────────────────────────────────────────────────────────
def test_last_occurrence_wins_for_repeated_labels():
    payload = {"studies": [{"name": "Sessions & VP", "labels": [
        {"text": "C: POC", "price": 29746.94},
        {"text": "C: POC", "price": 29555.79},
        {"text": "C: POC", "price": 29402.78},      # 現行セッション = 最後
        {"text": "New York", "price": 29470.25},    # 通さないラベル
    ]}]}
    levels = TV.levels_from_pine(payload, None)
    table = {lv["label"]: lv["price"] for lv in levels}
    check("最後の出現が勝つ", table.get("C: POC") == 29402.78, str(table))
    check("セッション見出しは水準にしない", "New York" not in table, str(table))


def test_session_levels_need_the_window_to_cover_the_open():
    # ET 18:00 起点。窓が開始より後から始まっていれば高安は不完全なので出さない。
    now = int(datetime(2026, 8, 24, 20, 0, tzinfo=timezone.utc).timestamp())
    short = [{"t": now - i * 180, "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 1}
             for i in range(20)][::-1]
    out = TV.session_levels_from_bars(short, now)
    check("窓が短ければセッション高安を出さない", out == [], str(out))
    check("足が無ければ空", TV.session_levels_from_bars([], now) == [])


# ── CVD ──────────────────────────────────────────────────────────────────
def test_cvd_becomes_a_directional_dict():
    with tempfile.TemporaryDirectory() as tmp:
        hp = str(Path(tmp) / "h.json")
        study = {"CVD": 59999.0, "EMA Fast": 62675.0, "EMA Slow": 64783.0}
        table = {"studies": [{"tables": [{"rows": ["EMA | 売り優勢"]}]}]}
        cvd, meta = TV.build_cvd(study, table, 29370.0, "2026-08-24T00:00:00Z", hp)
        check("値が入る", cvd["value"] == 59999.0)
        check("fast<slow は BEARISH", cvd.get("bias") == "BEARISH", str(cvd))
        check("meta が揃う", meta["status"] == "FRESH" and meta["attempts"] == 1)


def test_cvd_bias_is_withheld_when_sources_disagree():
    with tempfile.TemporaryDirectory() as tmp:
        hp = str(Path(tmp) / "h.json")
        study = {"CVD": 100.0, "EMA Fast": 120.0, "EMA Slow": 90.0}     # EMA は BULLISH
        table = {"studies": [{"tables": [{"rows": ["EMA | 売り優勢"]}]}]}  # table は BEARISH
        cvd, _ = TV.build_cvd(study, table, 1.0, "2026-08-24T00:00:00Z", hp)
        check("食い違えば方向を主張しない", "bias" not in cvd, str(cvd))


def test_cvd_stall_needs_price_movement():
    with tempfile.TemporaryDirectory() as tmp:
        hp = str(Path(tmp) / "h.json")
        study = {"CVD": 500.0, "EMA Fast": 1.0, "EMA Slow": 2.0}
        for i in range(3):                       # 同値 3 回 + 価格が動く
            _, meta = TV.build_cvd(study, None, 29000.0 + i, f"t{i}", hp)
        check("同値3回 + 価格変動で STALLED", meta["status"] == "STALLED", meta["status"])
    with tempfile.TemporaryDirectory() as tmp:
        hp = str(Path(tmp) / "h.json")
        for _ in range(3):                       # 同値 3 回だが価格も動かない
            _, meta = TV.build_cvd({"CVD": 500.0}, None, 29000.0, "t", hp)
        check("価格が動いていなければ停滞ではない", meta["status"] == "FRESH", meta["status"])


def test_cvd_history_does_not_grow_on_a_rerun():
    """同じサイクルを組み直しても履歴に積まない。

    積むと同値が並び、msnr_gate.cvd_health の停滞判定(同値 3 標本)が偽陽性を
    出して CVD ごと unavailable になる。検証中に実際に踏んだ。
    """
    with tempfile.TemporaryDirectory() as tmp:
        hp = str(Path(tmp) / "h.json")
        study = {"CVD": 59999.0, "EMA Fast": 1.0, "EMA Slow": 2.0}
        for _ in range(4):
            _, meta = TV.build_cvd(study, None, 29370.0, "2026-08-24T00:00:00Z", hp)
        check("同一サイクルの再実行で履歴が伸びない", len(meta["history"]) == 1,
              str(meta["history"]))
        check("偽の停滞にならない", meta["status"] == "FRESH", meta["status"])
        _, meta2 = TV.build_cvd(study, None, 29380.0, "2026-08-24T00:03:00Z", hp)
        check("別サイクルなら積む", len(meta2["history"]) == 2, str(meta2["history"]))


# ── SMT(チャート観測) ────────────────────────────────────────────────────
def smt_lines(rows):
    return {"studies": [{"name": "SMT", "all_lines": [
        {"x1": r[0], "x2": r[1], "y1": r[2], "y2": r[3], "color": r[4]} for r in rows]}]}


def smt_bars(lo=29220.0, hi=29540.0):
    return [{"t": 1000 + i * 180, "o": lo, "h": hi if i == 5 else lo + 10,
             "l": lo if i == 3 else lo + 5, "c": lo + 5, "v": 1} for i in range(20)]


def test_smt_bias_needs_colour_and_geometry_to_agree():
    bars = smt_bars()
    # color 1 は低い側、color 2 は高い側。最新(x2 最大)が低い側 = BULLISH。
    rows = [(0, 2, 29250.0, 29260.0, 1), (3, 5, 29500.0, 29510.0, 2),
            (6, 8, 29240.0, 29250.0, 1)]
    out = TV.smt_from_pine(smt_lines(rows), None, bars)
    check("色と幾何が一致すれば bias が出る", out["bias"] == "BULLISH", str(out))
    check("一致フラグが立つ", out["agree"] is True)
    # 最新を高い側にすると BEARISH
    rows2 = rows + [(9, 11, 29500.0, 29520.0, 2)]
    out2 = TV.smt_from_pine(smt_lines(rows2), None, bars)
    check("最新が高値側なら BEARISH", out2["bias"] == "BEARISH", str(out2))


def test_smt_withholds_bias_in_the_dead_zone():
    bars = smt_bars()
    rows = [(0, 2, 29250.0, 29260.0, 1), (3, 5, 29500.0, 29510.0, 2),
            (6, 8, 29378.0, 29380.0, 1)]     # 最新は中腹
    out = TV.smt_from_pine(smt_lines(rows), None, bars)
    check("中腹では方向を主張しない", out["bias"] is None, str(out))
    check("何を見て決められなかったかは残る",
          out["geometry"] is None and out["colorBias"] is not None, str(out))


def test_smt_colour_slot_number_is_not_hardcoded():
    """色枠の番号が入れ替わっても、y の分布から向きを導けること。"""
    bars = smt_bars()
    swapped = [(0, 2, 29250.0, 29260.0, 2), (3, 5, 29500.0, 29510.0, 1),
               (6, 8, 29240.0, 29250.0, 2)]   # 2 が低い側
    out = TV.smt_from_pine(smt_lines(swapped), None, bars)
    check("番号を決め打ちしない", out["bias"] == "BULLISH", str(out))


def test_smt_extracts_the_peer_name():
    labels = {"studies": [{"labels": [{"text": "SMT - MES1!", "price": 1}]}]}
    out = TV.smt_from_pine(smt_lines([(0, 2, 29250.0, 29260.0, 1),
                                      (3, 5, 29500.0, 29510.0, 2)]), labels, smt_bars())
    check("ピア名を取り出す", out["peer"] == "MES1!", str(out))
    check("入力が無ければ None", TV.smt_from_pine(None, None, smt_bars()) is None)


def test_smt_alert_json_tracks_formation_and_breakage():
    formed = TV.smt_from_alert({"schema": "NQX_SMT_ALERT/1", "event": "FORMED",
                                "type": "Bullish", "timeframe": "3m",
                                "startPrice": "29,100.25", "endPrice": "29,080.00"})
    check("SMT JSON formation is directional", formed["bias"] == "BULLISH" and
          formed["lifecycle"] == "CONFIRMED", str(formed))
    broken = TV.smt_from_alert({"schema": "NQX_SMT_ALERT/1", "event": "BROKEN",
                                "type": "Bearish", "smtTime": "2026-08-25T00:00:00Z"})
    check("SMT JSON breakage invalidates the observation", broken["bias"] is None and
          broken["lifecycle"] == "INVALIDATED", str(broken))


def test_engine_uses_the_observation_only_when_peers_are_absent():
    import msnr_gate
    bars = smt_bars()
    obs = {"bias": "BULLISH", "peer": "MES1!", "agree": True}
    # _et_minutes は ISO 文字列を **JST として**読む(ICT_ET_OFFSET_H の
    # ハードコード。検査で指摘済みの既知欠陥)。ET = JST − 13 なので
    # AM 窓(ET 05:00-09:30)に入れるには JST 18:00-22:30 を渡す。
    at_am = "2026-08-24T21:00:00+09:00"        # ET 08:00
    got = msnr_gate.external_smt(obs, bars, at_am)
    check("窓の中なら観測を採用", got and got["available"] and got["bias"] == "BULLISH",
          str(got))
    check("出所が残る", got.get("source") == "pine")
    at_night = "2026-08-24T12:00:00+09:00"      # ET 23:00 = 窓の外
    off = msnr_gate.external_smt(obs, bars, at_night)
    check("窓の外なら採用しない", off and off["available"] is False
          and off["reason"] == "OUTSIDE_SMT_WINDOW", str(off))
    dis = msnr_gate.external_smt({"bias": "BULLISH", "agree": False}, bars, at_am)
    check("源が食い違う観測は採用しない",
          dis and dis["reason"] == "SMT_SOURCES_DISAGREE", str(dis))
    check("bias 無しは None", msnr_gate.external_smt({"bias": None}, bars, at_am) is None)


def test_cvd_missing_is_reported_not_invented():
    with tempfile.TemporaryDirectory() as tmp:
        cvd, meta = TV.build_cvd({}, None, 1.0, "t", str(Path(tmp) / "h.json"))
        check("CVD 欠損は None + MISSING", cvd is None and meta["status"] == "MISSING")


# ── PO3 ──────────────────────────────────────────────────────────────────
def test_po3_is_one_of_four_values_or_none():
    now = int(datetime(2026, 8, 24, 20, 0, tzinfo=timezone.utc).timestamp())
    day_open = int(datetime(2026, 8, 23, 22, 0, tzinfo=timezone.utc).timestamp())
    # 開始値の下を大きく掃引 → 現値は開始値の上 = MANIPULATION_DOWN
    bars = [{"t": day_open + i * 180, "o": 100.0, "h": 101.0, "l": 100.0, "c": 100.5, "v": 1}
            for i in range(10)]
    bars[2]["l"] = 80.0
    check("下を掃って上に戻れば MANIPULATION_DOWN",
          TV.classify_po3(bars, 105.0, now, 5.0) == "MANIPULATION_DOWN")
    flat = [{"t": day_open + i * 180, "o": 100.0, "h": 100.5, "l": 99.5, "c": 100.0, "v": 1}
            for i in range(10)]
    check("型が読めなければ None(当て推量しない)",
          TV.classify_po3(flat, 100.0, now, 5.0) is None)
    check("足が足りなければ None", TV.classify_po3(flat[:3], 100.0, now, 5.0) is None)
    check("ノイズフロア不明なら None", TV.classify_po3(flat, 100.0, now, None) is None)
    valid = {"DISTRIBUTION_UP", "DISTRIBUTION_DOWN", "MANIPULATION_UP", "MANIPULATION_DOWN"}
    got = TV.classify_po3(bars, 105.0, now, 5.0)
    check("返るのは許容 4 値のみ", got is None or got in valid, str(got))


# ── ピア ─────────────────────────────────────────────────────────────────
def test_peers_require_exact_timestamps_and_a_window():
    main = [{"t": 1000 + i * 180, "o": 1, "h": 2, "l": 0, "c": 1, "v": 1} for i in range(10)]
    # SMT 窓の外(ET 深夜)なら peers を作らない
    out_of_window = int(datetime(2026, 8, 24, 6, 0, tzinfo=timezone.utc).timestamp())  # ET 02:00
    peers, meta, sid = TV.normalize_peers(bars_payload(10), main, out_of_window)
    check("SMT 窓の外では peers を作らない", peers is None and sid is None)
    check("入力が無ければ None", TV.normalize_peers(None, main, 1)[0] is None)


# ── fail-closed ──────────────────────────────────────────────────────────
def write_raw(tmp, **files):
    for name, payload in files.items():
        (Path(tmp) / f"{name}.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_raw_acquisition_receipt_exposes_reused_files():
    now = datetime.now(timezone.utc).replace(microsecond=0)
    with tempfile.TemporaryDirectory() as tmp:
        write_raw(tmp, chart_state={"symbol": "CME_MINI:MNQ1!"},
                  bars3m={}, study_3m={}, pine_labels={})
        stale_path = Path(tmp) / "chart_state.json"
        stale_epoch = now.timestamp() - 300
        os.utime(stale_path, (stale_epoch, stale_epoch))
        receipt = TV.build_acquisition_receipt(Path(tmp), now)
    chart = receipt["sources"]["chart_state.json"]
    check("取得受領書は raw SHA-256 を保存する", len(chart.get("sha256") or "") == 64)
    check("4分を超えた必須 chart_state を再利用扱いにする",
          chart["status"] == "STALE" and "chart_state.json" in receipt["staleRequired"],
          str(receipt))
    check("任意ファイル欠落は必須鮮度リストへ混ぜない",
          "quote.json" not in receipt["staleRequired"], str(receipt["staleRequired"]))


def test_build_is_fail_closed():
    def expect_blocked(label, tmp, now=None):
        try:
            TV.build_bundle(Path(tmp), now=now)
        except TV.AcquireError as exc:
            check(label, True)
            return str(exc)
        check(label, False, "did not raise")
        return ""

    with tempfile.TemporaryDirectory() as tmp:
        expect_blocked("入力が無ければ止まる", tmp)

    with tempfile.TemporaryDirectory() as tmp:
        write_raw(tmp, chart_state={"symbol": "CME_MINI:ES1!"}, bars3m=bars_payload(100),
                  study_3m={}, pine_labels={})
        msg = expect_blocked("MNQ 以外は止まる", tmp,
                             now=datetime.fromtimestamp(1000 + 100 * 180, timezone.utc))
        check("理由に symbol が出る", "MNQ" in msg, msg)

    with tempfile.TemporaryDirectory() as tmp:
        write_raw(tmp, chart_state={"symbol": "CME_MINI:MNQ1!"}, bars3m=bars_payload(10),
                  study_3m={}, pine_labels={})
        msg = expect_blocked("確定足が足りなければ止まる", tmp,
                             now=datetime.fromtimestamp(1000 + 10 * 180, timezone.utc))
        check("理由に本数が出る", "bars3m" in msg, msg)


def test_window_layout_contract():
    layout = {"panes": [
        {"index": 0, "symbol": "CME_MINI:MNQ1!", "resolution": "15"},
        {"index": 1, "symbol": "CME_MINI:MNQ1!", "resolution": "3"},
    ]}
    state15 = {"symbol": "CME_MINI:MNQ1!", "resolution": "15", "studies": []}
    state3 = {"symbol": "CME_MINI:MNQ1!", "resolution": "3",
              "studies": [{"name": "CVD Unified"}]}
    TV.validate_window_layout(layout, state15, state3)
    check("固定2pane/3視覚領域を受理", True)
    try:
        TV.validate_window_layout(layout, state15,
                                  {**state3, "studies": []})
    except TV.AcquireError as exc:
        check("3分側のCVD欠落を拒否", "CVD Unified" in str(exc), str(exc))
    else:
        check("3分側のCVD欠落を拒否", False)


def test_build_produces_a_usable_bundle():
    """実チャートの取得と同じ形の入力から、下流が読める形が出ること。"""
    t0 = 1787302800
    n = 240
    bars = {"bars": [{"time": t0 + i * 180, "open": 29400 + (i % 5),
                      "high": 29410 + (i % 5), "low": 29390 + (i % 5),
                      "close": 29400 + ((i + 2) % 5), "volume": 1000}
                     for i in range(n)]}
    now = datetime.fromtimestamp(t0 + n * 180, timezone.utc)
    def htf_payload(step):
        start = int(now.timestamp()) - 30 * step
        return {"bars": [{"time": start + i * step, "open": 29000 + i,
                           "high": 29002 + i, "low": 28999 + i,
                           "close": 29001 + i, "volume": 1000}
                          for i in range(30)]}
    with tempfile.TemporaryDirectory() as tmp:
        write_raw(
            tmp,
            chart_state={"symbol": "CME_MINI:MNQ1!", "resolution": "3"},
            bars3m=bars,
            study_3m={"studies": [{"name": "CVD Unified", "values": {
                "CVD": "59,999", "EMA Fast": "62,675", "EMA Slow": "64,783"}}]},
            study_15m={"studies": [{"name": "NQX SwingArm Pressure V2", "values": {
                "NQX_DATA_CT_ATR": "37.93", "NQX_DATA_CT_TREND": "−1.00"}}]},
            bars45m=htf_payload(2700), bars1h=htf_payload(3600),
            bars4h=htf_payload(14400), bars1d=htf_payload(86400),
            pine_labels={"studies": [{"name": "Sessions & VP", "labels": [
                {"text": "C: VAH", "price": 29486.65},
                {"text": "C: VAL", "price": 29318.92},
                {"text": "C: POC", "price": 29402.78}]}]},
            quote={"last": 29370.0},
        )
        bundle = TV.build_bundle(Path(tmp), now=now)

    snap = bundle["snapshot"]
    check("価格は quote から", bundle["price"] == 29370.0)
    check("priceAt は now(足の epoch から作らない)", bundle["priceAt"] == now.isoformat())
    check("sourceSymbol に MNQ", "MNQ" in bundle["sourceSymbol"])
    check("確定足が入る", len(snap["bars3m"]) >= 60, str(len(snap["bars3m"])))
    check("レベルが入る", len(snap["levels"]) >= 3)
    check("CVD は方向付き dict", isinstance(bundle["cvd"], dict) and "bias" in bundle["cvd"],
          str(bundle.get("cvd")))
    check("cvdMeta が付く", bundle["cvdMeta"]["status"] in {"FRESH", "STALLED"})
    check("VWAP が算出される", snap.get("vwap") is not None)
    check("U+2212 を含む study が数値化される",
          snap.get("study15m", {}).get("NQX_DATA_CT_TREND") == -1.0,
          str(snap.get("study15m")))
    check("ctAtr15 が引き出される", bundle.get("ctAtr15") == 37.93)
    check("45m/1h/4h/1D が確定足から統合される",
          snap.get("htfContext", {}).get("complete") is True and
          snap.get("htfContext", {}).get("bias") == "BUY", str(snap.get("htfContext")))
    # 下流が実際に読めること
    import msnr_gate
    result = msnr_gate.evaluate(bundle)
    check("msnr_gate.evaluate が通る", isinstance(result, dict) and "decision" in result)


test_display_numbers_parse()
test_epoch_units()
test_forming_bar_is_dropped()
test_wrong_timeframe_file_is_rejected()
test_session_gap_does_not_trip_spacing_check()
test_spacing_check_needs_enough_samples()
test_broken_bars_are_discarded()
test_last_occurrence_wins_for_repeated_labels()
test_session_levels_need_the_window_to_cover_the_open()
test_cvd_becomes_a_directional_dict()
test_cvd_bias_is_withheld_when_sources_disagree()
test_cvd_stall_needs_price_movement()
test_cvd_history_does_not_grow_on_a_rerun()
test_cvd_missing_is_reported_not_invented()
test_smt_bias_needs_colour_and_geometry_to_agree()
test_smt_withholds_bias_in_the_dead_zone()
test_smt_colour_slot_number_is_not_hardcoded()
test_smt_extracts_the_peer_name()
test_smt_alert_json_tracks_formation_and_breakage()
test_engine_uses_the_observation_only_when_peers_are_absent()
test_po3_is_one_of_four_values_or_none()
test_peers_require_exact_timestamps_and_a_window()
test_raw_acquisition_receipt_exposes_reused_files()
test_build_is_fail_closed()
test_window_layout_contract()
test_build_produces_a_usable_bundle()

if FAILED:
    print(f"FAILED: {len(FAILED)} -> {', '.join(FAILED)}")
    raise SystemExit(1)
print("ALL PASS (test_r28_tv_snapshot)")

# -*- coding: utf-8 -*-
"""TradingView MCP の生出力から監視バンドルを機械的に組み立てる(R28)。

なぜこの形か:
  監視ループは LLM のエージェント・ターンであり、MCP を呼べるのはエージェント
  だけ。これまでエージェントが**手で**バンドル JSON を書き写しており、
  rangeAnchor 0% / CVD が裸の int(401 本が採点 0 点)/ bars15m 0% という
  欠落は全てその転記工程で起きた。

  ここでは役割を分ける:
    エージェント = MCP を呼び、**出力をそのまま** .secrets/tv_raw/ へ保存する
    このスクリプト = 保存された生出力だけから、決定論的にバンドルを組む

  エージェントの自由裁量が入る余地を「どのツールを呼ぶか」だけに絞ることで、
  ループ何周目でも同じ入力から同じバンドルが出る。

入力(--raw-dir、既定 .secrets/tv_raw/。各ファイルは MCP 出力の**逐語**保存):
  pane_layout.json   pane_list               (固定2paneの存在と配置)
  chart_state_15m.json chart_get_state        (pane 0 / 15分の復元確認)
  chart_state.json   chart_get_state          (シンボルと解像度の検証)
  bars3m.json        data_get_ohlcv count=240 (3分足で取得)
  study_3m.json      data_get_study_values    (3分足: CVD Unified)
  bars15m.json       data_get_ohlcv count=60  (15分足で取得)
  bars45m.json       data_get_ohlcv count=80  (45分足・確定足だけ)
  bars1h.json        data_get_ohlcv count=80  (1時間足・確定足だけ)
  bars4h.json        data_get_ohlcv count=80  (4時間足・確定足だけ)
  bars1d.json        data_get_ohlcv count=80  (日足・確定足だけ)
  study_15m.json     data_get_study_values    (15分足: NQX_DATA_* と CVD)
  pine_labels.json   data_get_pine_labels study_filter="Sessions" max_labels=100
  pine_lines.json    data_get_pine_lines  study_filter="Sessions"
  cvd_table.json     data_get_pine_tables study_filter="CVD"
  quote.json         quote_get                (任意。無ければ最終足の終値)
  peers_es.json      ES の data_get_ohlcv     (任意。銘柄切替で取得したとき)
  smt_alert.json     SMT Divergences V2 の最新 webhook JSON (任意)

実測で分かっている罠(2026-08-24、実チャートで確認):
  * CDP はタブ 0 に固定束縛。tab_switch しても読み取り先は変わらない
    (tv_health_check の target_id が不変)。ピアは同一チャートの銘柄切替で取る。
  * study 値の負号は U+2212(−)で来る。ASCII の - ではない。
  * 数値は "59,999" のようにカンマ入り文字列。
  * bars の epoch は秒でも ms でも来る(NQX_DATA_*_SOURCE_TIME は ms)。
  * pine labels は過去セッション分も混ざって返る。**最後の出現**が現行。
  * priceAt を足の epoch から作らない(実害あり: 1 分未来に振れて拒否された)。

出力: §6.3 互換のバンドル JSON。--out へ書き、stdout にはサマリのみ。
そのまま monitor_publish.py へ、または monitor_ingest.py --kind snapshot へ渡せる。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

import htf_context  # noqa: E402
import msnr_gate  # noqa: E402  (noise_floor を単一の定義源として再利用する)

ET = ZoneInfo("America/New_York")
TICK = 0.25
BAR3_SEC = 180
BAR1_SEC = 60
BAR15_SEC = 900
HTF_FILES = {"45m": ("bars45m.json", 2700), "1h": ("bars1h.json", 3600),
             "4h": ("bars4h.json", 14400), "1d": ("bars1d.json", 86400)}
MIN_BARS_3M = 60          # §6.2 step 4 の下限
RAW_DIR_DEFAULT = ".secrets/tv_raw"
CVD_HISTORY_PATH = ".secrets/tv_cvd_history.json"
CVD_HISTORY_KEEP = 12

ACQUISITION_RECEIPT_VERSION = "NQX_ACQUISITION_RECEIPT/1"
# A file mtime is the only durable proof that the interactive MCP controller
# actually replaced a raw response this cycle.  Content timestamps are not
# consistently exposed by every TradingView tool, so retain both the hash and
# the filesystem observation instead of pretending that payload presence means
# fresh acquisition.
RAW_SOURCE_POLICY = {
    "pane_layout.json": {"required": True, "maxAgeSec": 240},
    "chart_state_15m.json": {"required": True, "maxAgeSec": 240},
    "chart_state.json": {"required": True, "maxAgeSec": 240},
    # Silver Bullet は任意の研究入力。未取得でも既存3mエンジンを止めない。
    "bars1m.json": {"required": False, "maxAgeSec": 180},
    "bars3m.json": {"required": True, "maxAgeSec": 240},
    "study_3m.json": {"required": True, "maxAgeSec": 240},
    "pine_labels.json": {"required": True, "maxAgeSec": 240},
    "cvd_table.json": {"required": False, "maxAgeSec": 240},
    "quote.json": {"required": False, "maxAgeSec": 240},
    "smt_lines.json": {"required": False, "maxAgeSec": 240},
    "smt_labels.json": {"required": False, "maxAgeSec": 240},
    "smt_alert.json": {"required": False, "maxAgeSec": 240},
    "peers_es.json": {"required": False, "maxAgeSec": 240},
    "bars15m.json": {"required": True, "maxAgeSec": 240},
    "bars45m.json": {"required": False, "maxAgeSec": 5400},
    "bars1h.json": {"required": False, "maxAgeSec": 7200},
    "bars4h.json": {"required": False, "maxAgeSec": 28800},
    "bars1d.json": {"required": False, "maxAgeSec": 172800},
    "study_15m.json": {"required": True, "maxAgeSec": 240},
    "pine_lines.json": {"required": False, "maxAgeSec": 1800},
}

# 取引日は ET 18:00 に始まる。セッション境界は**この連続分割**で固定する。
# 境界そのものは慣習だが、レベル名が derive_range_anchor の対照表
# (msnr_gate.DERIVED_RANGE_PAIRS)と一致していることが要件。変えるときは両方。
SESSIONS_ET = (
    ("Asia", 18, 2),      # 18:00 → 02:00
    ("London", 2, 8),     # 02:00 → 08:00
    ("New York", 8, 17),  # 08:00 → 17:00
)


class AcquireError(RuntimeError):
    """fail-closed: 組み立てられない入力は理由付きで止める。"""


# ── 低レベルの正規化 ──────────────────────────────────────────────────────

def _num(value):
    """TradingView の表示文字列を数値へ。カンマと U+2212 を処理する。

    **有限値だけを返す。** NaN と inf を通していたため、`"1e400"` が `inf` に、
    `float("nan")` がそのまま素通りしていた。価格や epoch に inf が混じると
    比較が全部通ってしまい、静かに壊れる。
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        out = float(value)
        return out if math.isfinite(out) else None
    text = str(value).strip().replace(",", "").replace("−", "-")
    if not text or text in {"-", "n/a", "N/A"}:
        return None
    try:
        out = float(text)
    except ValueError:
        return None
    return out if math.isfinite(out) else None


def _epoch_sec(value):
    """秒でも ms でも受けて秒にする。2033 年より先の秒は現実に無いので ms 判定。"""
    parsed = _num(value)
    if parsed is None:
        return None
    return int(parsed / 1000) if parsed > 4_000_000_000 else int(parsed)


def _load(raw_dir: Path, name: str, required=True):
    path = raw_dir / name
    if not path.exists():
        if required:
            raise AcquireError(f"raw input missing: {name}")
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise AcquireError(f"raw input unreadable: {name}: {exc}") from exc


def validate_window_layout(layout, state15, state3):
    """固定2pane/3視覚領域の取得元を、値から検証する。

    pane_list の active_index は信頼しない。各paneをfocusした直後に保存した
    chart_get_state の symbol/resolution と、3分側の CVD study の存在を正本にする。
    """
    panes = layout.get("panes") if isinstance(layout, dict) else None
    if not isinstance(panes, list) or len(panes) != 2:
        raise AcquireError("window layout must contain exactly 2 chart panes")
    expected = {0: "15", 1: "3"}
    for index, resolution in expected.items():
        row = next((item for item in panes if isinstance(item, dict)
                    and _num(item.get("index")) == index), None)
        if not row:
            raise AcquireError(f"window layout missing pane {index}")
        symbol = str(row.get("symbol") or "")
        if "MNQ" not in symbol.upper() or str(row.get("resolution") or "") != resolution:
            raise AcquireError(
                f"window pane {index} must be MNQ/{resolution}: "
                f"{symbol!r}/{row.get('resolution')!r}")

    for label, state, resolution in (("pane 0", state15, "15"),
                                     ("pane 1", state3, "3")):
        if not isinstance(state, dict):
            raise AcquireError(f"{label} chart state missing")
        symbol = str(state.get("symbol") or state.get("chart_symbol") or "")
        if "MNQ" not in symbol.upper() or str(state.get("resolution") or "") != resolution:
            raise AcquireError(
                f"{label} state must be MNQ/{resolution}: "
                f"{symbol!r}/{state.get('resolution')!r}")

    studies = state3.get("studies") or []
    names = [str(item.get("name") or "") for item in studies if isinstance(item, dict)]
    if not any("CVD Unified" in name for name in names):
        raise AcquireError("pane 1 must contain visible CVD Unified study")


def build_acquisition_receipt(raw_dir: Path, now_dt: datetime):
    """Freeze hashes and write-times for every controller-owned raw source.

    Raw files are overwritten in place.  Without this receipt a later audit can
    prove only that a value existed, not that the corresponding MCP call ran in
    this cycle.  The fixed window, 15-minute context, and 3-minute execution
    sources all require a current-cycle write; due-driven HTF files retain their
    wider cadence.
    """
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=timezone.utc)
    now_utc = now_dt.astimezone(timezone.utc)
    sources = {}
    stale_required = []
    for name, policy in RAW_SOURCE_POLICY.items():
        path = raw_dir / name
        row = {
            "file": name,
            "required": bool(policy["required"]),
            "maxAgeSec": int(policy["maxAgeSec"]),
        }
        if not path.exists():
            row["status"] = "MISSING"
        else:
            raw = path.read_bytes()
            stat = path.stat()
            modified = datetime.fromtimestamp(stat.st_mtime, timezone.utc)
            age = (now_utc - modified).total_seconds()
            row.update({
                "modifiedAt": modified.isoformat(),
                "ageSec": round(age, 3),
                "bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
            })
            if age < -60:
                row["status"] = "FUTURE"
            elif age > float(policy["maxAgeSec"]):
                row["status"] = "STALE"
            else:
                row["status"] = "FRESH"
        if row["required"] and row["status"] != "FRESH":
            stale_required.append(name)
        sources[name] = row
    return {
        "schemaVersion": ACQUISITION_RECEIPT_VERSION,
        "assembledAt": now_utc.isoformat(),
        "requiredFresh": not stale_required,
        "staleRequired": stale_required,
        "sources": sources,
    }


def _dominant_step(bars):
    """足の列から**最頻の正の時間差**を返す。判定できなければ None。

    最頻値だけを見るので、週末・保守時間・セッション境界で開く飛びでは
    誤検出しない。差が3つ未満のときは母数が足りないので判定しない。
    """
    deltas = [b["t"] - a["t"] for a, b in zip(bars, bars[1:]) if b["t"] > a["t"]]
    if len(deltas) < 3:
        return None
    counts = {}
    for delta in deltas:
        counts[delta] = counts.get(delta, 0) + 1
    return max(counts.items(), key=lambda kv: (kv[1], -kv[0]))[0]


def normalize_bars(payload, step_sec, now_epoch, *, trailing_forming=False):
    """MCP の bars を {t,o,h,l,c,v} に揃え、**形成中の最終足を落とす**。

    形成中足で方向を断定しないのは §9 の規律。closed_bars(msnr_gate) と同じ
    判定(t + step <= now)をここでも行い、二重に守る。

    ``trailing_forming=True``(45m/1h/4h/1D の HTF 用)では、さらに **raw の
    最終行を常に形成中扱い**にする。HTF は期限駆動で稀にしか取らないため、
    最終行は取得時点の途中値のまま raw に残り、時刻だけで判定すると close を
    過ぎた瞬間にその途中値が確定足に昇格する(2026-09-08 実測: 4h の 07:00 JST
    足が 09:00 の途中値 close 29,583 で確定扱い。実際の close は 29,704 付近)。
    「後続の行が存在する」ことだけが、TradingView がその足を閉じた証拠である。
    これはセッション最後の長い足(4h の 23:00 JST 足は 06:00 まで続き、
    t + step = 03:00 では閉じていない)も正しく形成中に留める。3m/15m は毎
    サイクル取り直すので従来どおり時刻判定だけでよい。

    併せて**足の間隔が step_sec と一致するか**を検査する。呼び出し側は
    step_sec を形成中足の切り落としにしか使っていないため、15分足を
    ``bars3m.json`` に保存しても素通りしていた。実際に MCP は
    ``chart_get_state`` が ``"3"`` を返している最中に 900 秒足を返すことが
    あり、そのまま noiseFloor と構造判定を汚染する。ファイル名と中身の
    時間足が違うことは、ここでしか気付けない。
    """
    rows = (payload or {}).get("bars") or []
    out = []
    for row in rows:
        t = _epoch_sec(row.get("time", row.get("t")))
        o = _num(row.get("open", row.get("o")))
        h = _num(row.get("high", row.get("h")))
        l = _num(row.get("low", row.get("l")))
        c = _num(row.get("close", row.get("c")))
        v = _num(row.get("volume", row.get("v"))) or 0.0
        if None in (t, o, h, l, c):
            continue
        if h < max(o, c) - 1e-9 or l > min(o, c) + 1e-9:
            continue  # 壊れた足は捨てる(高値<実体 など)
        out.append({"t": t, "o": o, "h": h, "l": l, "c": c, "v": v})
    out.sort(key=lambda b: b["t"])
    observed_step = _dominant_step(out)
    if observed_step is not None and observed_step != step_sec:
        raise AcquireError(
            f"bar spacing {observed_step}s does not match expected {step_sec}s "
            "(wrong timeframe saved into this file)")
    confirmed = [b for b in out if b["t"] + step_sec <= now_epoch]
    if trailing_forming and out:
        confirmed = [b for b in confirmed if b["t"] < out[-1]["t"]]
    return out, confirmed


def _optional_bars(raw_dir, filename, step_sec, now_epoch, rejected, *,
                   trailing_forming=False):
    """任意足の確定足を返す。間隔不一致は**その足だけ**落として理由を残す。

    `normalize_bars` は中身の時間足がファイル名と違う生ファイルを
    ``AcquireError`` で弾く。必須の ``bars3m.json`` ではそれが正しいが、任意
    ソース(1m / 15m / 45m / 1h / 4h / 1D)で同じ例外を素通しにすると、
    **研究用・文脈用のファイル 1 本**(3m の応答を誤って保存した、前サイクルの
    残骸が残った 等)で acquire 全体が落ち、必須の 3m 経路・levels・CVD ごと
    毎サイクル停止する。monitor_pipeline 側の契約は逆で、壊れた任意フィードは
    有効な 3m 判定を消してはならない(monitor_pipeline の 1m 鮮度判定の注記)。

    落としたことは握りつぶさず ``snapshot.barsRejected`` に残し、「未取得」と
    「取得したが拒否した」を運用画面で区別できるようにする。
    """
    try:
        _, confirmed = normalize_bars(_load(raw_dir, filename, required=False) or {},
                                      step_sec, now_epoch,
                                      trailing_forming=trailing_forming)
        return confirmed
    except AcquireError as exc:
        rejected[filename[:-len(".json")] if filename.endswith(".json") else filename] = str(exc)
        return []


def study_values(payload, study_substr):
    """study_values 出力から名前一致した study の値表を数値化して返す。"""
    for study in (payload or {}).get("studies") or []:
        if study_substr.lower() in str(study.get("name", "")).lower():
            return {key: _num(val) for key, val in (study.get("values") or {}).items()}
    return {}


# ── レベル ────────────────────────────────────────────────────────────────

def levels_from_pine(labels_payload, lines_payload):
    """pine labels から水準表を作る。**最後の出現**が現行セッション。

    Sessions & VP は過去セッションの "C: POC" 等も同名で返すため、
    先着ではなく最後を採る(実チャートで確認済み: 56 ラベル中、同名が最大
    10 回現れる)。"C:"/"P:" プレフィクスとセッション開始系ラベルだけを通す。
    """
    keep_exact = {"C: POC", "C: VAH", "C: VAL", "P: POC", "P: VAH", "P: VAL",
                  "Weekly open", "6pm open", "Daily open", "Midnight open"}
    table = {}
    for study in (labels_payload or {}).get("studies") or []:
        for row in study.get("labels") or []:
            text = str(row.get("text") or "").strip()
            price = _num(row.get("price"))
            if price is None or text not in keep_exact:
                continue
            table[text] = price     # 後勝ち = 最後の出現
    return [{"label": name, "price": price, "source": "Sessions&VP"}
            for name, price in table.items()]


def session_bounds_et(now_et):
    """現在の取引日(ET 18:00 起点)のセッション境界を epoch で返す。"""
    day_open = now_et.replace(hour=18, minute=0, second=0, microsecond=0)
    if now_et.hour < 18:
        day_open -= timedelta(days=1)
    out = []
    for name, start_h, end_h in SESSIONS_ET:
        start = day_open.replace(hour=start_h)
        if start_h < 18:
            start += timedelta(days=1)
        end = day_open.replace(hour=end_h)
        if end_h <= start_h or end_h < 18:
            end += timedelta(days=1)
        out.append((name, int(start.timestamp()), int(end.timestamp())))
    return day_open, out


def derive_session_id(now_epoch):
    """取引セッションの識別子を作る。ピア取得の成否に依存しない。

    R37: これまで sessionId は `normalize_peers` の中でしか採番されず、
    ES ピアが取れないと bundle ごと欠落していた。ピア取得は CDP がタブ0に
    固定束縛されるため実環境では成立しないので、結果として全サイクルで
    sessionId が無く、`strategy_models._repo2_lifecycle` がそれを "UNKNOWN" と
    見なして**全モデルの投票権を落としていた**(実測 403/403 で得票 0)。
    SMT 取得の失敗が、無関係な CRT の投票権まで巻き添えにしていた。

    ここで返すのは**取引セッション**の ID(ASIA/LONDON/NEWYORK)であり、
    `normalize_peers` が返す **SMT 窓**の ID(ET-YYYYMMDD-AM/PM)とは別物。
    SMT の照合は従来どおり snapshot 側の ID で行うので干渉しない。
    """
    now_et = datetime.fromtimestamp(now_epoch, ET)
    day_open, sessions = session_bounds_et(now_et)
    label = "OFF"
    for name, start, end in sessions:
        if start <= now_epoch < end:
            label = name.upper().replace(" ", "")
            break
    return f"ET-{day_open.strftime('%Y%m%d')}-{label}"


def session_levels_from_bars(bars, now_epoch):
    """確定足からセッション高安を計算する。

    derive_range_anchor(msnr_gate)は "New York High"/"New York Low" 等の
    ラベル対を要求する。VP インジケータはこれを出さないので、足から計算して
    注入する。これは**名前の付いたセッション極値**であり、R12 が禁じる
    「ローリング 3M 高安」ではない。

    規則: 進行中のセッションは「ここまでの高安」で有効(流動性プールとして
    意味を持つのはまさにそれ)。**開始が窓の外にある過去セッション**は、
    高安が切れている可能性があるので出さない。
    """
    if not bars:
        return []
    now_et = datetime.fromtimestamp(now_epoch, ET)
    _, sessions = session_bounds_et(now_et)
    window_start = bars[0]["t"]
    out = []
    for name, start, end in sessions:
        if start >= now_epoch:
            continue                        # まだ始まっていない
        if start < window_start:
            continue                        # 窓が開始を覆っていない = 高安が不完全
        rows = [b for b in bars if start <= b["t"] < min(end, now_epoch)]
        if len(rows) < 3:
            continue
        # R36: **終わったセッションか、進行中かを区別する。**
        #
        # 進行中セッションの高安は毎サイクル広がるローリング極値で、R12 が
        # OTE に使うことを禁じた「ローリング 3M 高安」と実質同じ性質を持つ。
        # 名前が付いているだけで許すのは筋が通らない。
        #
        # ここでは出力を止めず、`settled` で出所を分ける。流動性プールとしては
        # 進行中の高安にも意味があるので描画には使い、**レンジの起点(OTE を
        # 生む anchor)には終わったセッションだけ**を使う(msnr_gate 側で判定)。
        settled = end <= now_epoch
        for suffix, price in (("High", max(b["h"] for b in rows)),
                              ("Low", min(b["l"] for b in rows))):
            out.append({"label": f"{name} {suffix}", "price": price,
                        "source": "computed(session)", "settled": settled})
    return out


# ── VWAP ─────────────────────────────────────────────────────────────────

def session_vwap_anchor(now_epoch):
    """Session VWAP のアンカー(取引日開始 = ET 18:00)を epoch で返す。

    ``msnr_gate.session_anchor_epoch`` と**同一定義**。片方だけ変えると
    bundle.vwap と msnr 側の系列のアンカーがずれ、drift ゲートが恒常的に
    閉じる(R45 で実際に起きた: 13.16pt ずれて vwapPath が毎サイクル死亡)。
    """
    day_open, _ = session_bounds_et(datetime.fromtimestamp(now_epoch, ET))
    return int(day_open.timestamp())


def session_vwap_state(bars, now_epoch):
    """アンカー以降の累積 ``(pv, vv, throughT, barCount)`` を返す。無ければ None。

    R45: `monitor_pipeline.normalize_market_bars` は bars3m を FEED_BARS_MAX
    (既定60本 = 直近3時間)に切り詰めてから msnr_gate へ渡す。判定側は
    セッション開始まで遡る足を**持っていない**ので、アンカーを直しただけでは
    Session VWAP を再現できない(実測 drift 19.0pt)。ここで 240本ぶんの累積を
    スカラーで bundle に載せ、判定側が窓の手前ぶんを逆算できるようにする。
    """
    if not bars:
        return None
    anchor = session_vwap_anchor(now_epoch)
    rows = [b for b in bars if b["t"] >= anchor]
    if not rows:
        return None
    pv = vv = 0.0
    for b in rows:
        hlc3 = (b["h"] + b["l"] + b["c"]) / 3.0
        vol = max(0.0, b["v"])
        pv += hlc3 * vol
        vv += vol
    if vv <= 0:
        return None
    return pv, vv, int(rows[-1]["t"]), len(rows)


def session_vwap(bars, now_epoch):
    """取引日開始(ET 18:00)からのセッション VWAP と ±1σ バンド。

    旧チャートは VWAP インジケータから読んでいたが、現チャートには無い。
    hlc3×出来高の累積は決定論なので、ここで計算して依存を消す。

    R45: アンカー以降の足が無いときは**全量へフォールバックしない**。
    セッションが始まった直後に前の取引日の足で埋めると、それは Session VWAP
    ではない別の数字になる。無いものは None で返す。
    """
    state = session_vwap_state(bars, now_epoch)
    if state is None:
        return None, None, None
    pv, vv, _, _ = state
    anchor = session_vwap_anchor(now_epoch)
    rows = [b for b in bars if b["t"] >= anchor]
    vwap = pv / vv
    var = 0.0
    for b in rows:
        hlc3 = (b["h"] + b["l"] + b["c"]) / 3.0
        var += max(0.0, b["v"]) * (hlc3 - vwap) ** 2
    sigma = math.sqrt(var / vv)
    return round(vwap, 2), round(vwap - sigma, 2), round(vwap + sigma, 2)


# ── CVD ──────────────────────────────────────────────────────────────────

def cvd_bias_from_table(table_payload):
    """CVD Unified の pine table から EMA 行の優勢を読む(裏取り用)。"""
    for study in (table_payload or {}).get("studies") or []:
        for tbl in study.get("tables") or []:
            for row in tbl.get("rows") or []:
                text = str(row)
                if text.startswith("EMA"):
                    if "買い優勢" in text:
                        return "BULLISH"
                    if "売り優勢" in text:
                        return "BEARISH"
    return None


def build_cvd(study3m, table_payload, price, now_iso, history_path):
    """CVD を**方向付きの dict**にする。

    実サイクル 401 本の CVD は裸の int で、_cvd_score が dict の bias を要求する
    ため**全て採点 0 点**だった(取得しているつもりで 0%)。ここで潰す。

    bias は二重に取る:
      1. 実行足(3m)の EMA fast vs slow
      2. pine table の「EMA | 買い優勢/売り優勢」(2026-08-19 のキャッシュ事故の
         教訓: study 値の読み出し経路が古いことがあり、table が生値に近い)
    両者が食い違ったら bias は付けない(誤った方向で ±1 されるより 0 が安全)。

    停滞判定: 履歴 3 標本が同値で価格が動いていれば STALLED。履歴は
    .secrets/tv_cvd_history.json にサイクルを跨いで持つ。
    """
    value = study3m.get("CVD")
    fast, slow = study3m.get("EMA Fast"), study3m.get("EMA Slow")
    if value is None:
        return None, {"provider": "TradingView MCP CVD Unified", "attempts": 1,
                      "status": "MISSING", "at": now_iso, "history": []}

    ema_bias = None
    if fast is not None and slow is not None and abs(fast - slow) > 1e-9:
        ema_bias = "BULLISH" if fast > slow else "BEARISH"
    table_bias = cvd_bias_from_table(table_payload)
    if ema_bias and table_bias and ema_bias != table_bias:
        bias = None      # 食い違い: 方向を主張しない
    else:
        bias = ema_bias or table_bias

    # 履歴(停滞検出)。読めなくても取得は止めない。
    history = []
    hp = Path(history_path)
    try:
        if hp.exists():
            history = json.loads(hp.read_text(encoding="utf-8"))
        if not isinstance(history, list):
            history = []
    except (json.JSONDecodeError, OSError):
        history = []
    # 同じサイクルの再実行では積まない。積むと同値が並び、msnr_gate.cvd_health の
    # 停滞判定(同値 3 標本)が**偽陽性**を出して CVD ごと unavailable になる
    # (検証中に実際に踏んだ: 同一スナップショットを 3 回組んだら STALLED)。
    if not history or history[-1].get("at") != now_iso:
        history.append({"at": now_iso, "value": value, "price": price})
        history = history[-CVD_HISTORY_KEEP:]
    try:
        hp.parent.mkdir(parents=True, exist_ok=True)
        hp.write_text(json.dumps(history, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass

    status = "FRESH"
    tail = history[-3:]
    if len(tail) == 3 and len({row["value"] for row in tail}) == 1:
        prices = [row.get("price") for row in tail if row.get("price") is not None]
        if len(prices) >= 2 and max(prices) - min(prices) >= TICK:
            status = "STALLED"     # 価格は動いたのに CVD が 3 標本同値

    cvd = {"value": value, "fast": fast, "slow": slow}
    if bias:
        cvd["bias"] = bias
    meta = {"provider": "TradingView MCP CVD Unified", "attempts": 1,
            "status": status, "at": now_iso,
            "history": [row["value"] for row in history]}
    return cvd, meta


# ── PO3 ──────────────────────────────────────────────────────────────────

def classify_po3(bars, price, now_epoch, nf):
    """取引日開始(ET 18:00)の始値に対する AMD の型を決定論で分類する。

    po3 は accepted 4 値の文字列で、生成器がどこにも無かった(取得 0%)。
    規則(displacement の単位は noise floor):
      * 開始値より nf 以上**下**を掃引して現値が開始値の上 → MANIPULATION_DOWN
      * 開始値より nf 以上**上**を掃引して現値が開始値の下 → MANIPULATION_UP
      * 現値が開始値 + nf 以上でレンジ上部 → DISTRIBUTION_UP
      * 現値が開始値 − nf 以下でレンジ下部 → DISTRIBUTION_DOWN
      * どれでもない → None(**誤った方向で ±1 されるより無しが安全**)
    """
    if not bars or price is None or not nf or nf <= 0:
        return None
    now_et = datetime.fromtimestamp(now_epoch, ET)
    day_open_dt, _ = session_bounds_et(now_et)
    anchor = int(day_open_dt.timestamp())
    rows = [b for b in bars if b["t"] >= anchor]
    if len(rows) < 5:
        return None
    open_price = rows[0]["o"]
    hi = max(b["h"] for b in rows)
    lo = min(b["l"] for b in rows)
    span = hi - lo
    if lo <= open_price - nf and price > open_price:
        return "MANIPULATION_DOWN"
    if hi >= open_price + nf and price < open_price:
        return "MANIPULATION_UP"
    if span > 0:
        pos = (price - lo) / span
        if price >= open_price + nf and pos >= 0.6:
            return "DISTRIBUTION_UP"
        if price <= open_price - nf and pos <= 0.4:
            return "DISTRIBUTION_DOWN"
    return None


# ── ピア(SMT) ──────────────────────────────────────────────────────────

def smt_from_pine(lines_payload, labels_payload, bars):
    """チャートの SMT インジケータが描いた線から乖離の向きを読む(R29)。

    ES 足を自前で揃える経路は CDP がタブ 0 固定のため実環境で取れていない
    (実サイクル 403 本で peers 0%)。一方この指標は MNQ vs MES を毎足計算して
    線を描いており、その幾何がそのまま読める。

    実チャートで確認した構造(2026-08-24):
      * 線は {x1,x2,y1,y2,color} を持つ。x は指標内の連番で**バー番号ではない**
      * color は 2 枠に分かれる。ラベルの色は Bullish/Bearish とも白に設定されて
        いても、線の color 枠は 1 と 2 に分かれたまま
      * 重なる x 区間(78-88)で color 2 は y≈29516/29488(窓の高値 29539 付近)、
        color 1 は y≈29257/29220(**29220.25 は窓の安値そのもの**)

    したがって color 1 = 安値の乖離 = BULLISH、color 2 = 高値 = BEARISH。
    ただし色枠の番号は指標設定で入れ替わりうるので**信用しきらない**。
    最新の線が窓の高値側にあるか安値側にあるかを幾何でも判定し、
    **両者が一致したときだけ bias を返す**(CVD bias と同じ規律)。
    """
    lines = []
    for study in (lines_payload or {}).get("studies") or []:
        for row in study.get("all_lines") or []:
            x2 = _num(row.get("x2"))
            y1, y2 = _num(row.get("y1")), _num(row.get("y2"))
            color = row.get("color")
            if None in (x2, y1, y2) or color is None:
                continue
            lines.append({"x2": x2, "y": (y1 + y2) / 2.0, "color": int(color)})
    if not lines or not bars:
        return None
    lines.sort(key=lambda r: r["x2"])
    latest = lines[-1]

    # 幾何: 窓の値幅の中で、その線が上半分にあるか下半分にあるか。
    highs = [b["h"] for b in bars]
    lows = [b["l"] for b in bars]
    hi, lo = max(highs), min(lows)
    if hi - lo <= 0:
        return None
    pos = (latest["y"] - lo) / (hi - lo)
    if pos >= 0.55:
        geom = "BEARISH"       # 高値側の乖離
    elif pos <= 0.45:
        geom = "BULLISH"       # 安値側の乖離
    else:
        geom = None            # 中腹。幾何では決められない

    # 色: 同じ払い出しの中で「高い方の色枠」を BEARISH とする。番号そのものを
    # 決め打ちしない —— 実際の y の分布から毎回導く。
    by_color = {}
    for row in lines:
        by_color.setdefault(row["color"], []).append(row["y"])
    color_bias = None
    if len(by_color) == 2:
        (c_a, ys_a), (c_b, ys_b) = sorted(by_color.items())
        mid_a = sorted(ys_a)[len(ys_a) // 2]
        mid_b = sorted(ys_b)[len(ys_b) // 2]
        if abs(mid_a - mid_b) > 1e-9:
            bear_color = c_a if mid_a > mid_b else c_b
            color_bias = "BEARISH" if latest["color"] == bear_color else "BULLISH"

    peer = None
    for study in (labels_payload or {}).get("studies") or []:
        for row in study.get("labels") or []:
            text = str(row.get("text") or "")
            if " - " in text:
                peer = text.split(" - ", 1)[1].strip()
    agree = bool(geom and color_bias and geom == color_bias)
    bias = geom if agree else None
    return {"bias": bias, "peer": peer or "PEER", "agree": agree,
            "geometry": geom, "colorBias": color_bias, "source": "pine",
            "latestY": round(latest["y"], 2), "position": round(pos, 3)}


def smt_from_alert(payload):
    """Normalize the user's SMT Divergences V2 formation/breakage JSON.

    The indicator's protected source exposes alert template tokens, not its
    internal series.  Formation can therefore supply direction and timestamps;
    Breakage is an explicit invalidation and must suppress a leftover chart
    line from the prior formation.
    """
    if not isinstance(payload, dict):
        return None
    schema = str(payload.get("schema") or "")
    if schema != "NQX_SMT_ALERT/1":
        return None
    event = str(payload.get("event") or "FORMED").strip().upper()
    raw_type = str(payload.get("type") or "").strip().upper()
    bias = "BULLISH" if "BULL" in raw_type else "BEARISH" if "BEAR" in raw_type else None
    if event not in {"FORMED", "BROKEN"}:
        return None
    return {
        "bias": bias if event == "FORMED" else None,
        "peer": str(payload.get("peer") or "INDICATOR_PEER"),
        "agree": bias is not None and event == "FORMED",
        "event": event, "lifecycle": "CONFIRMED" if event == "FORMED" else "INVALIDATED",
        "timeframe": payload.get("timeframe"),
        "startPrice": _num(payload.get("startPrice")),
        "endPrice": _num(payload.get("endPrice")),
        "startTime": payload.get("startTime"), "endTime": payload.get("endTime"),
        "smtTime": payload.get("smtTime"),
        "source": "smt_alert_json",
    }


def normalize_peers(peers_payload, confirmed_3m, now_epoch):
    """ES の足を SMT 用に正規化する。

    index_smt は**時刻の完全一致**を要求する(許容差ゼロ)。ここで主足に
    存在する t だけ残し、sessionId を ET の日付+窓で決定論的に採番する。
    窓の外(AM 05:00-09:30 / PM 12:00-15:00 ET 以外)なら None を返す —
    エンジンが OUTSIDE_SMT_WINDOW で捨てるだけの荷物を積まない。
    """
    if not peers_payload or not confirmed_3m:
        return None, None, None
    now_et = datetime.fromtimestamp(now_epoch, ET)
    minute = now_et.hour * 60 + now_et.minute
    if 5 * 60 <= minute <= 9 * 60 + 30:
        window = "AM"
    elif 12 * 60 <= minute <= 15 * 60:
        window = "PM"
    else:
        return None, None, None
    session_id = f"ET-{now_et.strftime('%Y%m%d')}-{window}"
    main_ts = {b["t"] for b in confirmed_3m}
    _, es_confirmed = normalize_bars(peers_payload, BAR3_SEC, now_epoch)
    rows = [{"t": b["t"], "h": b["h"], "l": b["l"]}
            for b in es_confirmed if b["t"] in main_ts]
    if len(rows) < 5:
        return None, None, None
    return ({"ES": rows}, {"ES": {"sessionId": session_id}}, session_id)


# ── 組み立て ─────────────────────────────────────────────────────────────

def build_bundle(raw_dir: Path, now=None):
    live_acquisition = now is None
    now_dt = now or datetime.now(timezone.utc)
    now_epoch = int(now_dt.timestamp())
    now_iso = now_dt.isoformat()

    acquisition_receipt = build_acquisition_receipt(raw_dir, now_dt)
    # Explicit ``now`` is used by deterministic tests and the guarded replay
    # mode.  The live CLI has no override and must never reuse a stale required
    # raw response merely because the JSON file still exists.
    if live_acquisition and acquisition_receipt["staleRequired"]:
        raise AcquireError(
            "required raw acquisition is not fresh: "
            + ", ".join(acquisition_receipt["staleRequired"]))

    state = _load(raw_dir, "chart_state.json")
    layout = _load(raw_dir, "pane_layout.json", required=live_acquisition)
    state15 = _load(raw_dir, "chart_state_15m.json", required=live_acquisition)
    if live_acquisition or layout is not None or state15 is not None:
        validate_window_layout(layout or {}, state15 or {}, state)
    symbol = str(state.get("symbol") or state.get("chart_symbol") or "")
    if "MNQ" not in symbol.upper():
        raise AcquireError(f"sourceSymbol must contain MNQ: {symbol!r}")

    raw3, confirmed3 = normalize_bars(_load(raw_dir, "bars3m.json"), BAR3_SEC, now_epoch)
    if len(confirmed3) < MIN_BARS_3M:
        raise AcquireError(f"bars3m: {len(confirmed3)} confirmed < {MIN_BARS_3M}")
    stale = now_epoch - (confirmed3[-1]["t"] + BAR3_SEC)
    if stale > 600:
        raise AcquireError(f"bars3m stale: last close {stale}s ago")

    # 3m 以外は全て任意入力。1m は動画レシピ専用、15m/HTF は文脈用で、
    # 欠落は「補間しない・INSUFFICIENT で無得点」(CLAUDE.md §1/§6.2)が契約。
    # 無い場合に3mを代用してはいけない。
    bars_rejected = {}
    confirmed1 = _optional_bars(raw_dir, "bars1m.json", BAR1_SEC, now_epoch, bars_rejected)
    confirmed15 = _optional_bars(raw_dir, "bars15m.json", BAR15_SEC, now_epoch, bars_rejected)
    confirmed_htf = {}
    for name, (filename, step_sec) in HTF_FILES.items():
        # HTF raw の最終行は取得時点の形成中足。後続の行が無い限り確定にしない
        # (normalize_bars の trailing_forming 参照)。
        confirmed_htf[name] = _optional_bars(raw_dir, filename, step_sec, now_epoch,
                                             bars_rejected, trailing_forming=True)

    # 価格: quote があれば quote、無ければ生の最終足(形成中を含む)の終値。
    quote = _load(raw_dir, "quote.json", required=False) or {}
    price = _num(quote.get("last") or quote.get("price") or quote.get("lp"))
    if price is None and raw3:
        price = raw3[-1]["c"]
    if price is None:
        raise AcquireError("price unavailable (no quote, no bars)")
    if abs(price / TICK - round(price / TICK)) > 1e-6:
        raise AcquireError(f"price not tick aligned: {price}")

    study3 = study_values(_load(raw_dir, "study_3m.json"), "CVD")
    study15_nqx = study_values(_load(raw_dir, "study_15m.json", required=False), "NQX")
    study15_cvd = study_values(_load(raw_dir, "study_15m.json", required=False), "CVD")

    levels = levels_from_pine(_load(raw_dir, "pine_labels.json"),
                              _load(raw_dir, "pine_lines.json", required=False))
    levels += session_levels_from_bars(confirmed3, now_epoch)
    if not levels:
        raise AcquireError("levels empty (pine labels and computed sessions both missing)")

    nf = msnr_gate.noise_floor(confirmed3)
    vwap, vwap_lo, vwap_hi = session_vwap(confirmed3, now_epoch)
    vwap_state = session_vwap_state(confirmed3, now_epoch)
    cvd, cvd_meta = build_cvd(study3, _load(raw_dir, "cvd_table.json", required=False),
                              price, now_iso, str(BASE / CVD_HISTORY_PATH))
    po3 = classify_po3(confirmed3, price, now_epoch, nf)
    peers, peer_meta, session_id = normalize_peers(
        _load(raw_dir, "peers_es.json", required=False), confirmed3, now_epoch)

    # eventGate: events.py のキャッシュがあれば使う。無くても止めない(fail-open
    # は既存設計 — CLAUDE.md §6.1)。
    event_gate = None
    try:
        import events                      # noqa: PLC0415
        gate = events.current_gate()
        if isinstance(gate, dict):
            event_gate = {"state": gate.get("state") or gate.get("gate") or "NONE",
                          "source": "events.py", "checkedAt": now_iso}
        elif gate:
            event_gate = {"state": str(gate), "source": "events.py", "checkedAt": now_iso}
    except Exception:
        pass

    snapshot = {
        "at": now_iso,
        **({"bars1m": confirmed1} if confirmed1 else {}),
        **({"barsRejected": bars_rejected} if bars_rejected else {}),
        "bars3m": confirmed3,
        # R35: `monitor_publish.compact` は `snapshot.bars` を必須にしている
        # (無ければ ValueError で publish に到達しない)。bars3m と同一の中身を
        # 別名でも置く。これが無いため、docs/TV_ACQUISITION_LOOP.md の手順は
        # 実行しても毎回 `ERROR: invalid monitor bundle` で止まっていた。
        "bars": confirmed3,
        "levels": levels,
        "vwap": vwap, "vwap_lo": vwap_lo, "vwap_hi": vwap_hi,
        # R45: msnr_gate がバーごとの VWAP 系列を同じアンカーで再計算するための
        # 正本。落ちても msnr 側で同じ規則から導けるが、導出をここで固定する。
        "vwapAnchorT": session_vwap_anchor(now_epoch),
        # R45: セッション累積そのもの。bars3m は publish 経路で 60本へ切られる
        # ので、判定側は自前の合計をこれから引いて「窓の手前ぶん」を復元する。
        # スカラーなのでペイロード上限にも触らない。
        "vwapSessionPv": (vwap_state or (None,))[0],
        "vwapSessionVv": vwap_state[1] if vwap_state else None,
        "vwapThroughT": vwap_state[2] if vwap_state else None,
        "vwapSessionBars": vwap_state[3] if vwap_state else None,
        "cvd": (cvd or {}).get("value"),
        "cvdFast": (cvd or {}).get("fast"), "cvdSlow": (cvd or {}).get("slow"),
    }
    if confirmed15:
        snapshot["bars15m"] = confirmed15
    for name, rows in confirmed_htf.items():
        if rows:
            snapshot[{"45m": "bars45m", "1h": "bars1h", "4h": "bars4h", "1d": "bars1d"}[name]] = rows
    # HTF は 3分足から補間しない。各 timeframe の確定 OHLC が無ければ
    # frames[].freshness=MISSING のまま残し、候補への加点も行わない。
    snapshot["htfContext"] = htf_context.build_context(confirmed_htf, now_epoch)
    if study15_nqx:
        # 15 分足の NQX_DATA_*。ctAtr 等は今後のエンジン消費用に保存する
        # (これまで手入力で 46%/8%/8% しか無かった値の生データ)。
        snapshot["study15m"] = study15_nqx
    if peers:
        snapshot["peers"] = peers
        snapshot["peerMeta"] = peer_meta
        snapshot["sessionId"] = session_id
    alert_receipt = (acquisition_receipt.get("sources") or {}).get("smt_alert.json") or {}
    alert_payload = (_load(raw_dir, "smt_alert.json", required=False)
                     if alert_receipt.get("status") == "FRESH" else None)
    smt_alert = smt_from_alert(alert_payload)
    # A BROKEN alert is authoritative invalidation.  Only when no alert is
    # available do we fall back to visual line/label extraction.
    smt_obs = smt_alert or smt_from_pine(
        _load(raw_dir, "smt_lines.json", required=False),
        _load(raw_dir, "smt_labels.json", required=False), confirmed3)
    if smt_obs:
        # bias が None でも残す。何を見て決められなかったかが記録に要る。
        snapshot["smtObservation"] = smt_obs
    if event_gate:
        snapshot["eventGate"] = event_gate
    if po3:
        snapshot["po3"] = po3

    # A raw collector may begin its next write while this bundle is being
    # assembled.  Refuse that torn cycle instead of mixing old bars with a new
    # quote/CVD row.  The next monitor cycle will reacquire a coherent set.
    if live_acquisition:
        final_receipt = build_acquisition_receipt(raw_dir, now_dt)
        changed = []
        initial_sources = acquisition_receipt.get("sources") or {}
        for source_name, final_source in (final_receipt.get("sources") or {}).items():
            initial_source = initial_sources.get(source_name) or {}
            if (initial_source.get("sha256"), initial_source.get("bytes")) != (
                    final_source.get("sha256"), final_source.get("bytes")):
                changed.append(source_name)
        if changed:
            raise AcquireError(
                "raw acquisition changed during assembly: " + ", ".join(changed))
        acquisition_receipt = final_receipt

    bundle = {
        "at": now_iso,
        "price": price,
        "priceAt": now_iso,           # システム時計。足の epoch から作らない
        "priceSource": "TradingView MCP (CDP tab0)",
        "sourceSymbol": symbol,
        # R37: ピア取得の成否と無関係に取引セッションを識別できるようにする。
        # snapshot 側の sessionId(SMT 窓)は peers があるときだけ入り、
        # index_smt の照合はそちらを使うので競合しない。
        "sessionId": derive_session_id(now_epoch),
        "snapshot": snapshot,
        "cvd": cvd if cvd else None,
        "cvdMeta": cvd_meta,
        "acquisitionReceipt": acquisition_receipt,
    }
    if study15_nqx.get("NQX_DATA_CT_ATR") is not None:
        bundle["ctAtr15"] = study15_nqx["NQX_DATA_CT_ATR"]
    return bundle


def main(argv=None):
    parser = argparse.ArgumentParser(description="TradingView 生出力 → 監視バンドル")
    parser.add_argument("--raw-dir", default=RAW_DIR_DEFAULT)
    parser.add_argument("--out", default=None, help="バンドルの書き出し先(UTF-8, BOM無し)")
    parser.add_argument(
        "--replay-now-from-bars", action="store_true",
        help="**配線の検証専用。** 現在時刻を「最終足の close」とみなして鮮度ゲートを"
             " 通す。市場が閉じている時に取得経路を通しで試すためのもので、"
             " ライブでは絶対に使わない(古い価格で発注しうる)。")
    args = parser.parse_args(argv)
    raw_dir = Path(args.raw_dir)
    now = None
    if args.replay_now_from_bars:
        payload = _load(raw_dir, "bars3m.json")
        rows = [r for r in ((payload or {}).get("bars") or [])
                if _epoch_sec(r.get("time", r.get("t"))) is not None]
        if not rows:
            print("BLOCKED: --replay-now-from-bars but bars3m is empty")
            return 1
        last_t = max(_epoch_sec(r.get("time", r.get("t"))) for r in rows)
        now = datetime.fromtimestamp(last_t + BAR3_SEC, timezone.utc)
        print(f"REPLAY MODE: now := {now.isoformat()} (last bar close). ライブでは使わないこと。")
    try:
        bundle = build_bundle(raw_dir, now=now)
    except AcquireError as exc:
        print(f"BLOCKED: {exc}")
        return 1
    body = json.dumps(bundle, ensure_ascii=False, separators=(",", ":"))
    if args.out:
        Path(args.out).write_text(body, encoding="utf-8", newline="\n")
    else:
        print(body)
        return 0
    snap = bundle["snapshot"]
    print("OK bundle -> %s" % args.out)
    print("  symbol=%s price=%s bars3m=%d bars15m=%d htf=%s levels=%d" % (
        bundle["sourceSymbol"], bundle["price"], len(snap["bars3m"]),
        len(snap.get("bars15m") or []),
        (snap.get("htfContext") or {}).get("status"), len(snap["levels"])))
    print("  cvd=%s bias=%s status=%s po3=%s peers=%s eventGate=%s" % (
        (bundle.get("cvd") or {}).get("value"),
        (bundle.get("cvd") or {}).get("bias"),
        bundle["cvdMeta"]["status"], snap.get("po3"),
        "ES" if snap.get("peers") else "-",
        (snap.get("eventGate") or {}).get("state", "-")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

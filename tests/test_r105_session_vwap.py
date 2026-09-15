# -*- coding: utf-8 -*-
"""R105: セッション VWAP の累積を周期をまたいで持ち越す(tv_snapshot)と、欠損時の VWAP_PARTIAL(msnr_gate)。

    python tests/test_r105_session_vwap.py

合成した取引日 1 日分の足で、(1) 窓がアンカーに届かない周期でも真のセッション VWAP と一致すること、
(2) 従来の窓だけの計算はずれること(穴の再現)、(3) 欠損・新セッション・壊れた状態ファイルの扱い、
(4) build_bundle が snapshot に vwapComplete 等を載せること、(5) msnr_gate が complete でない VWAP を
SL 逃がしに使わないこと、を固定する。すべて tempdir。ネットワーク・台帳・発注に触れない。
"""
import copy
import json
import math
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
import tv_snapshot as TV  # noqa: E402
import msnr_gate  # noqa: E402
import stop_logic  # noqa: E402

PASS = [0]
FAIL = [0]


def check(label, condition, detail=""):
    if condition:
        PASS[0] += 1
        print(f"  OK   {label}")
    else:
        FAIL[0] += 1
        print(f"  NG   {label}  {detail}")


def section(title):
    print(f"\n--- {title}")


HITS = []
for module_name, attrs in (("broker_status", ("query_position", "query_orders", "query_balance")),
                           ("nqx_state", ("fetch_state_quiet", "claim_entry", "publish_result")),
                           ("autotrade_arm", ("state",))):
    try:
        module = __import__(module_name)
    except Exception:                                  # noqa: BLE001
        continue
    for attr in attrs:
        if hasattr(module, attr):
            setattr(module, attr, (lambda name: (lambda *a, **k: HITS.append(name)))(f"{module_name}.{attr}"))

# ---------------------------------------------------------------- 合成データ: 2026-09-14 18:00 ET 起点の取引日

ANCHOR = 1789423200                      # 2026-09-14 18:00 ET = 2026-09-15 07:00 JST
BAR = TV.BAR3_SEC
N_BARS = 400                             # 20 時間ぶん


def make_bars(n=N_BARS, start=ANCHOR):
    rows = []
    for i in range(n):
        base = 29300 + 40 * math.sin(i / 23.0) + (i % 7) * 1.25
        rows.append({"t": start + i * BAR, "o": base, "h": base + 6 + (i % 3), "l": base - 5 - (i % 4),
                     "c": base + ((i % 5) - 2) * 0.75, "v": 800 + (i * 37) % 900})
    return rows


ALL = make_bars()


def direct(rows):
    """従来定義(Σ v·hlc3 / Σ v、σ は v 加重の母分散)。"""
    pv = vv = 0.0
    for b in rows:
        pv += (b["h"] + b["l"] + b["c"]) / 3.0 * b["v"]
        vv += b["v"]
    vwap = pv / vv
    var = sum(b["v"] * ((b["h"] + b["l"] + b["c"]) / 3.0 - vwap) ** 2 for b in rows) / vv
    return round(vwap, 2), round(vwap - math.sqrt(var), 2), round(vwap + math.sqrt(var), 2)


def window_at(now_epoch, count=240):
    """tv_fetch と同じ「直近 240 本の確定足」。"""
    rows = [b for b in ALL if b["t"] + BAR <= now_epoch]
    return rows[-count:]


section("1. 穴の再現と修正(窓がアンカーに届かない周期)")
with tempfile.TemporaryDirectory() as tmp:
    state = Path(tmp) / "vwap.json"
    now1 = ANCHOR + 60 * BAR                                   # 3 時間後(窓 60 本はアンカーを含む)
    acc1 = TV.session_vwap_accumulate(window_at(now1), now1, state, symbol="MNQZ6")
    check("3 時間後: 窓がアンカーを含み complete=True、source=SESSION_ACCUMULATED",
          acc1["complete"] is True and acc1["source"] == "SESSION_ACCUMULATED" and acc1["reset"] == "NO_STATE", acc1)
    check("3 時間後: 値は従来定義と一致(バンド含む)",
          (acc1["vwap"], acc1["vwap_lo"], acc1["vwap_hi"]) == direct(window_at(now1)), (acc1, direct(window_at(now1))))
    check("状態ファイルが書かれる", state.exists() and json.loads(state.read_text(encoding="utf-8"))["bars"] == 60)

    # 1 周期ずつ進める(本番と同じ)。15 時間後には窓 240 本がアンカーに届かない。
    for k in range(61, 301):
        now_k = ANCHOR + k * BAR
        acc_k = TV.session_vwap_accumulate(window_at(now_k), now_k, state, symbol="MNQZ6")
    now2 = ANCHOR + 300 * BAR
    full = direct([b for b in ALL if b["t"] + BAR <= now2])          # 真のセッション VWAP(300 本)
    old = TV.session_vwap(window_at(now2), now2)                     # 従来: 窓 240 本だけ
    check("15 時間後: 従来の計算は真のセッション VWAP からずれる(穴の再現)", old[0] != full[0], (old, full))
    check("15 時間後: 累積は真のセッション VWAP と一致", (acc_k["vwap"], acc_k["vwap_lo"], acc_k["vwap_hi"]) == full, (acc_k, full))
    check("15 時間後: bars=300、complete=True、gapBars=0", acc_k["bars"] == 300 and acc_k["complete"] is True and acc_k["gapBars"] == 0, acc_k)
    check("msnr_gate の seed 用 pv/vv も全セッション分", abs(acc_k["pv"] / acc_k["vv"] - full[0]) < 0.01)
    again = TV.session_vwap_accumulate(window_at(now2), now2, state, symbol="MNQZ6")
    check("同じ周期を 2 回叩いても二重に足さない", again["bars"] == 300 and again["vwap"] == acc_k["vwap"], again)

    # 数値安定性: 300 本で σ の差は 0.01 未満(丸め前は 1e-6 未満)
    st = json.loads(state.read_text(encoding="utf-8"))
    mean_c = st["pvc"] / st["vv"]
    sigma = math.sqrt(max(0.0, st["pv2c"] / st["vv"] - mean_c ** 2))
    rows2 = [b for b in ALL if b["t"] + BAR <= now2]
    vw = sum((b["h"] + b["l"] + b["c"]) / 3.0 * b["v"] for b in rows2) / sum(b["v"] for b in rows2)
    sig_direct = math.sqrt(sum(b["v"] * ((b["h"] + b["l"] + b["c"]) / 3.0 - vw) ** 2 for b in rows2) / sum(b["v"] for b in rows2))
    check("σ の累積計算は直接計算と 1e-6 以内", abs(sigma - sig_direct) < 1e-6, (sigma, sig_direct))

section("2. 欠損・新セッション・壊れた状態")
with tempfile.TemporaryDirectory() as tmp:
    state = Path(tmp) / "vwap.json"
    now_a = ANCHOR + 60 * BAR
    TV.session_vwap_accumulate(window_at(now_a), now_a, state)
    # ループが 2 時間止まった: 次に来る窓は 40 本分の穴の後
    now_b = ANCHOR + 300 * BAR
    gap_rows = [b for b in window_at(now_b) if b["t"] >= ANCHOR + 100 * BAR]   # 100 本目以降だけ
    acc_b = TV.session_vwap_accumulate(gap_rows, now_b, state)
    check("欠損: gapBars=40、complete=False、それでも値は出す(表示用)",
          acc_b["gapBars"] == 40 and acc_b["complete"] is False and acc_b["vwap"] is not None, acc_b)
    check("欠損後も累積は続く(bars = 60 + 200)", acc_b["bars"] == 260, acc_b)
    acc_c = TV.session_vwap_accumulate(window_at(now_b + BAR), now_b + BAR, state)
    check("一度欠けたセッションは以後も complete=False のまま", acc_c["complete"] is False and acc_c["gapBars"] == 40, acc_c)

    # 新しい取引日(翌 18:00 ET)。窓がアンカーに届かない位置から始まる。
    next_anchor = ANCHOR + 86400
    late = [{"t": next_anchor + i * BAR, "o": 29500, "h": 29506, "l": 29495, "c": 29501, "v": 1000} for i in range(300, 340)]
    now_d = next_anchor + 340 * BAR
    acc_d = TV.session_vwap_accumulate(late, now_d, state)
    check("新セッション: reset=NEW_SESSION、アンカーに届かない窓は gapBars=300 で complete=False",
          acc_d["reset"] == "NEW_SESSION" and acc_d["gapBars"] == 300 and acc_d["complete"] is False, acc_d)
    early = [{"t": next_anchor + i * BAR, "o": 29500, "h": 29506, "l": 29495, "c": 29501, "v": 1000} for i in range(0, 20)]
    state2 = Path(tmp) / "vwap2.json"
    acc_e = TV.session_vwap_accumulate(early, next_anchor + 20 * BAR, state2)
    check("新セッションをアンカーの足から始めれば complete=True", acc_e["complete"] is True and acc_e["gapBars"] == 0, acc_e)

    # 壊れた状態ファイル / バージョン違い → 窓から新規(例外にしない)
    state.write_text("{not json", encoding="utf-8")
    acc_f = TV.session_vwap_accumulate(window_at(now_a), now_a, state)
    check("壊れた状態ファイルは NO_STATE として窓から始める", acc_f["reset"] == "NO_STATE" and acc_f["complete"] is True, acc_f)
    state.write_text(json.dumps({"version": 99}), encoding="utf-8")
    acc_g = TV.session_vwap_accumulate(window_at(now_a), now_a, state)
    check("バージョン違いも NO_STATE", acc_g["reset"] == "NO_STATE", acc_g)
    check("親ディレクトリが無ければ作る", TV.session_vwap_accumulate(window_at(now_a), now_a, Path(tmp) / "sub" / "dir" / "v.json")["source"] == "SESSION_ACCUMULATED")
    check("アンカー以降の足が無ければ None(前の取引日の足で埋めない)",
          TV.session_vwap_accumulate([{"t": ANCHOR - BAR, "o": 1, "h": 2, "l": 0, "c": 1, "v": 10}], ANCHOR + BAR, state2)["vwap"] is None)
    check("state_path=None なら窓だけ(WINDOW_ONLY)",
          TV.session_vwap_accumulate(window_at(now_a), now_a, None)["source"] == "WINDOW_ONLY")
    check("銘柄が変わればリセット(SYMBOL_CHANGED)",
          TV.session_vwap_accumulate(window_at(now_a), now_a, state, symbol="MNQZ6")["reset"] in ("NO_STATE", None)
          and TV.session_vwap_accumulate(window_at(now_a + BAR), now_a + BAR, state, symbol="MNQH7")["reset"] == "SYMBOL_CHANGED")

section("3. build_bundle が snapshot に載せる")


def write_raw(tmp, **files):
    for name, payload in files.items():
        (Path(tmp) / f"{name}.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


with tempfile.TemporaryDirectory() as tmp:
    t0 = ANCHOR
    n = 240
    bars = {"bars": [{"time": t0 + i * BAR, "open": 29400 + (i % 5), "high": 29410 + (i % 5), "low": 29390 + (i % 5),
                      "close": 29400 + ((i + 2) % 5), "volume": 1000 + (i % 9)} for i in range(n)]}
    now = datetime.fromtimestamp(t0 + n * BAR, timezone.utc)

    def htf_payload(step):
        start = int(now.timestamp()) - 30 * step
        return {"bars": [{"time": start + i * step, "open": 29000 + i, "high": 29002 + i, "low": 28999 + i,
                          "close": 29001 + i, "volume": 1000} for i in range(30)]}
    write_raw(tmp, chart_state={"symbol": "CME_MINI:MNQZ2026", "resolution": "3"}, bars3m=bars,
              study_3m={"studies": [{"name": "CVD Unified", "values": {"CVD": "59,999", "EMA Fast": "62,675", "EMA Slow": "64,783"}}]},
              study_15m={"studies": [{"name": "NQX SwingArm Pressure V2", "values": {"NQX_DATA_CT_ATR": "37.93", "NQX_DATA_CT_TREND": "−1.00"}}]},
              bars45m=htf_payload(2700), bars1h=htf_payload(3600), bars4h=htf_payload(14400), bars1d=htf_payload(86400),
              pine_labels={"studies": [{"name": "Sessions & VP", "labels": [{"text": "C: VAH", "price": 29486.65}, {"text": "C: VAL", "price": 29318.92}, {"text": "C: POC", "price": 29402.78}]}]},
              quote={"last": 29370.0})
    state = Path(tmp) / "state" / "vwap_session_state.json"
    try:
        bundle = TV.build_bundle(Path(tmp), now=now, vwap_state_path=state)
        snap = bundle["snapshot"]
        # now = 最後の足の close なので 240 本すべてが確定足(形成中の足は無い)。
        rows = [{"t": b["time"], "h": b["high"], "l": b["low"], "c": b["close"], "v": b["volume"]} for b in bars["bars"]]
        check("snapshot.vwap は従来定義と一致(窓がアンカーを含む初回)", snap["vwap"] == direct(rows)[0], (snap["vwap"], direct(rows)))
        check("snapshot に vwapComplete=True / vwapSource=SESSION_ACCUMULATED / vwapGapBars=0",
              snap.get("vwapComplete") is True and snap.get("vwapSource") == "SESSION_ACCUMULATED" and snap.get("vwapGapBars") == 0,
              {k: snap.get(k) for k in ("vwapComplete", "vwapSource", "vwapGapBars", "vwapFirstT")})
        check("vwapSessionPv/Vv/ThroughT/Bars は従来どおり載る(msnr_gate の seed 用)",
              snap.get("vwapSessionBars") == 240 and snap.get("vwapThroughT") == rows[-1]["t"] and snap.get("vwapSessionPv"), snap.get("vwapSessionBars"))
        check("状態ファイルは指定したパスに書かれる(本番 .secrets ではない)", state.exists())
        # 2 周期目: 窓を 60 本に切っても(publish 経路と同じ)累積は全セッション分のまま
        bars2 = {"bars": bars["bars"][-60:] + [{"time": t0 + n * BAR, "open": 29400, "high": 29410, "low": 29390, "close": 29401, "volume": 1000}]}
        write_raw(tmp, bars3m=bars2)
        now2 = datetime.fromtimestamp(t0 + (n + 1) * BAR, timezone.utc)
        bundle2 = TV.build_bundle(Path(tmp), now=now2, vwap_state_path=state)
        rows2 = rows + [{"t": t0 + n * BAR, "h": 29410, "l": 29390, "c": 29401, "v": 1000}]
        check("2 周期目: 窓が 60 本でも vwap は全 240 本の累積と一致", bundle2["snapshot"]["vwap"] == direct(rows2)[0],
              (bundle2["snapshot"]["vwap"], direct(rows2)))
        check("2 周期目: vwapSessionBars=241、complete=True", bundle2["snapshot"]["vwapSessionBars"] == 241 and bundle2["snapshot"]["vwapComplete"] is True,
              bundle2["snapshot"].get("vwapSessionBars"))
    except TV.AcquireError as exc:
        check("build_bundle が通る", False, str(exc))

section("4. msnr_gate: complete でない VWAP は SL 逃がしに使わない")
REAL = msnr_gate.stop_logic_policy
pol = dict(stop_logic.default_policy())
pol["vwapClearance"] = dict(pol["vwapClearance"], mode="LIVE")
msnr_gate.stop_logic_policy = (lambda: pol)
try:
    nf = 20.0
    # SELL: entry 29400, stop 29425, VWAP 29430(SL の 5pt 外側 = withinN 1.0 の内側)→ 通常は 29435 へ逃がす
    b_ok = {"price": 29400.0, "snapshot": {"vwap": 29430.0, "vwapComplete": True}}
    s_ok, a_ok = msnr_gate._apply_vwap_clearance("BREAKER_CONTINUATION", "SELL", 29400.0, 29425.0, b_ok, nf)
    check("complete=True: 従来どおり VWAP の外側へ逃がす", a_ok.get("applied") is True and s_ok > 29425.0, (s_ok, a_ok))
    b_part = {"price": 29400.0, "snapshot": {"vwap": 29430.0, "vwapComplete": False, "vwapGapBars": 40}}
    s_p, a_p = msnr_gate._apply_vwap_clearance("BREAKER_CONTINUATION", "SELL", 29400.0, 29425.0, b_part, nf)
    check("complete=False: SL は動かず reason=VWAP_PARTIAL(gapBars 付き)",
          s_p == 29425.0 and a_p.get("reason") == "VWAP_PARTIAL" and a_p.get("applied") is False and a_p.get("gapBars") == 40, (s_p, a_p))
    b_old = {"price": 29400.0, "snapshot": {"vwap": 29430.0}}
    s_o, a_o = msnr_gate._apply_vwap_clearance("BREAKER_CONTINUATION", "SELL", 29400.0, 29425.0, b_old, nf)
    check("キーが無い旧バンドルは従来どおり(後方互換)", a_o.get("applied") is True and s_o == s_ok)
finally:
    msnr_gate.stop_logic_policy = REAL

section("5. 本番パスへの到達")
check("broker_status / nqx_state / autotrade_arm へ 0 件", HITS == [], HITS)

print(f"\n合計: PASS {PASS[0]} / FAIL {FAIL[0]}")
sys.exit(1 if FAIL[0] else 0)

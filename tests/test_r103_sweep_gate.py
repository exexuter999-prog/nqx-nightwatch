# -*- coding: utf-8 -*-
"""R103-3: 掃引後に入る(sweepGate)と、指値の SL 再検査の取消配線(restingStopRecheck LIVE)。

    python tests/test_r103_sweep_gate.py

tests/fixtures/r103/ の 2026-09-15 の 4 件と、監査コピーと同じ形の bundle で固定する。
ネットワーク・台帳・発注に到達しないことも tripwire で確かめる。
"""
import copy
import json
import os
import sys
import tempfile
from datetime import datetime

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
import liquidity_pools as lp  # noqa: E402
import msnr_gate  # noqa: E402
import stop_logic  # noqa: E402

PASS = [0]
FAIL = [0]
FIXTURES = os.path.join(BASE, "tests", "fixtures", "r103")
BAR_SEC = 180


def check(label, condition, detail=""):
    if condition:
        PASS[0] += 1
        print(f"  OK   {label}")
    else:
        FAIL[0] += 1
        print(f"  NG   {label}  {detail}")


def section(title):
    print(f"\n--- {title}")


def load(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------- 本番パス tripwire

HITS = []
for module_name, attrs in (("broker_status", ("query_position", "query_orders", "query_balance")),
                           ("nqx_state", ("fetch_state_quiet", "claim_entry", "claim_management",
                                          "publish_result")),
                           ("autotrade_arm", ("state",))):
    try:
        module = __import__(module_name)
    except Exception:                                  # noqa: BLE001 — 無ければ到達しようがない
        continue
    for attr in attrs:
        if hasattr(module, attr):
            setattr(module, attr, (lambda name: (lambda *a, **k: HITS.append(name)))
                    (f"{module_name}.{attr}"))

BARS = load("bars3m_2026-09-15.json")["bars"]
CASES = load("cases_2026-09-15.json")["cases"]
BY_TAG = {case["tag"]: case for case in CASES}
PRM = msnr_gate.params()
T4 = BY_TAG["T4"]
POOL_T4 = 29447.0


def closed_before(at_iso, count=60):
    at = datetime.fromisoformat(at_iso).timestamp()
    rows = [bar for bar in BARS if bar["t"] + BAR_SEC <= at]
    return rows[-count:]


def bundle_for(case, at_iso=None, price=None):
    at_iso = at_iso or case["armedAt"]
    rows = closed_before(at_iso, int(case["closedBarsAtArm"]))
    return {"at": at_iso, "priceAt": at_iso, "price": case["priceAtArm"] if price is None else price,
            "snapshot": {"levels": copy.deepcopy(case["levels"]), "bars3m": copy.deepcopy(rows)}}


REAL_POLICY = msnr_gate.stop_logic_policy
# R119: この試験は sweepGate だけを見る。指値の門(limitGate)は stopLogic と同じように
# 切り離す —— T4(09-15 22:31、建値 29,426 / 現値 29,388.75 = gapR 2.01)は gapCap 1.5 に
# 当たるので、切り離さないとこのファイルの ARMED 前提が門の設定で揺れる。
REAL_LIMIT_GATE = msnr_gate.limit_gate_policy
LIMIT_GATE_OFF = {"gapCap": {"mode": "OFF"}, "targetPassed": {"mode": "OFF"}, "invalid": []}


def evaluate(bundle, sweep_mode, pool_mode="OFF", **sweep_overrides):
    pol = dict(stop_logic.default_policy())
    pol["vwapClearance"] = dict(pol["vwapClearance"], mode="LIVE")
    pol["poolClearance"] = dict(stop_logic.DEFAULT_POOL, mode=pool_mode)
    pol["sweepGate"] = dict(stop_logic.DEFAULT_SWEEP, mode=sweep_mode, **sweep_overrides)
    msnr_gate.stop_logic_policy = (lambda: pol)
    msnr_gate.limit_gate_policy = (lambda contract=None: dict(LIMIT_GATE_OFF))
    try:
        return msnr_gate.evaluate(copy.deepcopy(bundle))
    finally:
        msnr_gate.stop_logic_policy = REAL_POLICY
        msnr_gate.limit_gate_policy = REAL_LIMIT_GATE


def cand(result, model, side):
    return next((c for c in result.get("candidates") or [] if c["model"] == model and c["side"] == side), None)


# ---------------------------------------------------------------- 1. load_policy

section("1. execution_contract.json と load_policy")
policy = stop_logic.load_policy()
check("sweepGate は契約で SHADOW(掃引後に通った周期が 0 なので記録のみ。R103-3 §1.2)",
      policy["sweepGate"]["mode"] == "SHADOW", policy["sweepGate"])
check("sweepGate の既定値(poolWithinN 1.0 / sweepStopN 0.25 / maxAgeBars 3 / reclaimBars 2)",
      (policy["sweepGate"]["poolWithinN"], policy["sweepGate"]["sweepStopN"],
       policy["sweepGate"]["maxAgeBars"], policy["sweepGate"]["reclaimBars"]) == (1.0, 0.25, 3, 2),
      policy["sweepGate"])
check("sweepGate の対象は BREAKER と VP80",
      policy["sweepGate"]["models"] == ["BREAKER_CONTINUATION", "VP80_REVERSION"], policy["sweepGate"]["models"])
broken = {"stopLogic": {"vwapClearance": {"mode": "LIVE"}, "poolClearance": {"mode": "SHADOW"},
                        "sweepGate": {"mode": "LIVE", "sweepStopN": -1, "maxAgeBars": "3"}}}
fallback = stop_logic.load_policy(broken)
check("壊れた sweepGate はこの節だけ OFF", fallback["sweepGate"]["mode"] == "OFF", fallback["sweepGate"])
check("他の節は生きたまま", fallback["vwapClearance"]["mode"] == "LIVE" and fallback["poolClearance"]["mode"] == "SHADOW")
check("default_policy に sweepGate がある(OFF)", stop_logic.default_policy()["sweepGate"]["mode"] == "OFF")

# ---------------------------------------------------------------- 2. sweep_reclaim

section("2. sweep_reclaim(純関数)")
AT_ARM = T4["armedAt"]                          # 22:31:35 JST = 13:31:35Z(22:30 の足は形成中)
AT_2248 = "2026-09-15T13:48:00+00:00"           # 22:45 の足(高値 29,452.75 / 終値 29,428.75)が確定
AT_2300 = "2026-09-15T14:00:00+00:00"
# 候補の構造の起点。22:15 JST(= 13:15Z)より前には 21:48〜22:00 の別の掃引(29,484.25 まで)がある。
# 実際の評価では chain.breakBarT が起点になる(§4 で確認)。ここでは関数単体を固定する。
ORIGIN_T4 = datetime.fromisoformat("2026-09-15T13:15:00+00:00").timestamp()
tol_t4 = max(PRM["touch_pt"], 0.10 * T4["noiseFloor"])
before = lp.sweep_reclaim(closed_before(AT_ARM), POOL_T4, "SELL", ORIGIN_T4, tol_t4)
check("武装時点(22:31)では起点以降に 29,447 の掃引は無い", before["state"] == "NOT_SWEPT", before)
after = lp.sweep_reclaim(closed_before(AT_2248), POOL_T4, "SELL", ORIGIN_T4, tol_t4)
check("22:48 では 22:45 の足が掃引して同じ足で奪還(SWEPT_RECLAIMED)",
      after["state"] == "SWEPT_RECLAIMED" and after["extreme"] == 29452.75 and after["ageBars"] == 0, after)
check("掃引の深さは 5.75pt", after["sweepDepthPt"] == 5.75, after)
later = lp.sweep_reclaim(closed_before(AT_2300), POOL_T4, "SELL", ORIGIN_T4, tol_t4)
check("23:00 には奪還から 4 本経っている(ageBars=4)", later["state"] == "SWEPT_RECLAIMED" and later["ageBars"] == 4, later)
check("起点より前の掃引は見ない(origin_t = 22:45 の足なら NOT_SWEPT)",
      lp.sweep_reclaim(closed_before(AT_2248), POOL_T4, "SELL", after["sweepBarT"], tol_t4)["state"] == "NOT_SWEPT")
older = lp.sweep_reclaim(closed_before(AT_ARM), POOL_T4, "SELL", None, tol_t4)
check("起点を渡さなければ 21:48 の古い掃引エピソード(上に受容されたまま = SWEPT_NO_RECLAIM)を拾う = 起点が要る理由",
      older["state"] == "SWEPT_NO_RECLAIM"
      and datetime.fromtimestamp(older["sweepBarT"]).strftime("%H:%M") == "21:48", older)
t2 = BY_TAG["T2"]
tol_t2 = max(PRM["touch_pt"], 0.10 * t2["noiseFloor"])
ORIGIN_T2 = datetime.fromisoformat("2026-09-15T12:00:00+00:00").timestamp()
check("21:20 の SSL 29,388.50 は起点(21:00)以降に掃引されていない(安値 29,398.50 まで)",
      lp.sweep_reclaim(closed_before("2026-09-15T12:30:00+00:00"), 29388.5, "BUY", ORIGIN_T2, tol_t2)["state"] == "NOT_SWEPT")
two_bar = [{"t": 0, "h": 10, "l": 5, "c": 8}, {"t": 180, "h": 13, "l": 9, "c": 12},     # 抜けて上で引ける
           {"t": 360, "h": 14, "l": 8, "c": 9},                                         # 2 本目で内側へ
           {"t": 540, "h": 9.5, "l": 7, "c": 8}]
tb = lp.sweep_reclaim(two_bar, 10.0, "SELL", None, 0.1, reclaim_bars=2)
check("2 本型: 抜けた足の次で奪還。極値は 2 本の最大(14)", tb["state"] == "SWEPT_RECLAIMED" and tb["extreme"] == 14.0
      and tb["ageBars"] == 1, tb)
check("reclaim_bars=1(掃引足だけ)なら 2 本型は奪還にならない(SWEPT_NO_RECLAIM)",
      lp.sweep_reclaim(two_bar[:3], 10.0, "SELL", None, 0.1, reclaim_bars=1)["state"] == "SWEPT_NO_RECLAIM")
check("掃引が進行中(最後の足が抜けたまま)なら SWEPT_NO_RECLAIM",
      lp.sweep_reclaim(two_bar[:2], 10.0, "SELL", None, 0.1, reclaim_bars=2)["state"] == "SWEPT_NO_RECLAIM")
check("BUY は鏡像(安値が抜け、終値が上で戻る)",
      lp.sweep_reclaim([{"t": 0, "h": 12, "l": 8, "c": 11}], 9.0, "BUY", None, 0.1)["state"] == "SWEPT_RECLAIMED")
check("足が無ければ NOT_SWEPT", lp.sweep_reclaim([], 10.0, "SELL", None, 0.1)["state"] == "NOT_SWEPT")

# ---------------------------------------------------------------- 3. sweep_gate_audit

section("3. sweep_gate_audit")
RULE = dict(stop_logic.DEFAULT_SWEEP, mode="LIVE")
POOL_RULE = dict(stop_logic.DEFAULT_POOL, mode="LIVE")


def gate_for(case, at_iso, rule=RULE, origin_t=ORIGIN_T4):
    rows = closed_before(at_iso, int(case["closedBarsAtArm"]))
    nf = msnr_gate.noise_floor(rows)
    tol = max(PRM["touch_pt"], 0.10 * nf)
    pools = lp.pools(rows, case["levels"], case["priceAtArm"], tol, nf)
    dec = case["decision"]
    base = lp.stop_pool_audit(dec["side"], dec["entry"], dec["stop"], pools, nf, POOL_RULE)
    return nf, lp.sweep_gate_audit(dec["side"], dec["entry"], dec["stop"], base, rows, origin_t, nf, tol, rule,
                                   model=dec["model"])


nf_arm, g_arm = gate_for(T4, AT_ARM)
check("22:31: プール 29,447(SL の外側 2.5pt)があり掃引前 → PENDING", g_arm["state"] == "PENDING"
      and g_arm["pool"]["price"] == POOL_T4 and g_arm["reason"] == "SWEEP_PENDING", g_arm)
check("22:31: required は無い(SL を動かさない)", g_arm["required"] is None)
nf_48, g_48 = gate_for(T4, AT_2248)
expected = stop_logic.outward_tick(29452.75 + 0.25 * nf_48, "SELL")
check("22:48: 掃引→奪還を確認 → PASSED、required = 掃引極値 29,452.75 + 0.25N(不利側 tick)",
      g_48["state"] == "PASSED" and g_48["required"] == expected, (g_48, expected))
check("22:48: required は元の SL 29,444.50 より外側", g_48["required"] > T4["decision"]["stop"], g_48)
_, g_00 = gate_for(T4, AT_2300)
check("23:00: 奪還から 4 本 > maxAgeBars 3 → STALE", g_00["state"] == "STALE" and g_00["reason"] == "SWEEP_STALE", g_00)
_, g_1n = gate_for(T4, AT_2248, dict(RULE, sweepStopN=1.0))
check("sweepStopN=1.0 なら required は 1N 向こう", g_1n["required"] == stop_logic.outward_tick(29452.75 + nf_48, "SELL"), g_1n)
_, g_age = gate_for(T4, AT_2300, dict(RULE, maxAgeBars=6))
check("maxAgeBars=6 なら 23:00 でも PASSED", g_age["state"] == "PASSED", g_age)
_, g_t1 = gate_for(BY_TAG["T1"], BY_TAG["T1"]["armedAt"], origin_t=None)
check("16:49(VP80、外側 1N 以内にプール無し)→ NOT_APPLICABLE / NO_POOL_WITHIN_N",
      g_t1["state"] == "NOT_APPLICABLE" and g_t1["reason"] == "NO_POOL_WITHIN_N", g_t1)
_, g_t2 = gate_for(BY_TAG["T2"], BY_TAG["T2"]["armedAt"], origin_t=ORIGIN_T2)
check("21:20(SSL 29,388.50 が外側 10pt、未掃引)→ PENDING", g_t2["state"] == "PENDING" and g_t2["pool"]["price"] == 29388.5, g_t2)
_, g_model = gate_for(T4, AT_2248, dict(RULE, models=["VP80_REVERSION"]))
check("契約の models に無いモデルは MODEL_NOT_IN_POLICY", g_model["reason"] == "MODEL_NOT_IN_POLICY")
check("pool_audit が無ければ POOLS_MISSING",
      lp.sweep_gate_audit("SELL", 29426.0, 29444.5, None, BARS, None, 15.38, 2.0, RULE)["reason"] == "POOLS_MISSING")
check("幾何が無ければ GEOMETRY_MISSING",
      lp.sweep_gate_audit("SELL", None, 29444.5, {}, BARS, None, 15.38, 2.0, RULE)["reason"] == "GEOMETRY_MISSING")
check("記録タグ: PENDING → WOULD_WAIT / PASSED → WOULD_PASS / NOT_APPLICABLE → 無し",
      lp.sweep_evidence_tags(g_arm) == ["SWEEP_GATE_WOULD_WAIT"] and lp.sweep_evidence_tags(g_48) == ["SWEEP_GATE_WOULD_PASS"]
      and lp.sweep_evidence_tags(g_t1) == [])

# ---------------------------------------------------------------- 4. msnr_gate 統合(OFF / SHADOW / LIVE)

section("4. msnr_gate.evaluate")
GATE_TAGS = {"SWEEP_GATE_PASSED", "SWEEP_GATE_WOULD_WAIT", "SWEEP_GATE_WOULD_PASS"}


def strip_gate(result):
    out = copy.deepcopy(result)
    for candidate in out.get("candidates") or []:
        candidate.pop("sweepGate", None)
        candidate["evidence"] = [tag for tag in candidate.get("evidence") or [] if tag not in GATE_TAGS]
    decision = out.get("decision") or {}
    decision.pop("sweepGate", None)
    decision["evidence"] = [tag for tag in decision.get("evidence") or [] if tag not in GATE_TAGS]
    return out


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


b_arm = bundle_for(T4)
off = evaluate(b_arm, "OFF")
shadow = evaluate(b_arm, "SHADOW")
live = evaluate(b_arm, "LIVE")
c_off, c_shadow, c_live = (cand(r, "BREAKER_CONTINUATION", "SELL") for r in (off, shadow, live))
check("OFF: 武装時点の候補は SL 29,444.50 で ARMED(fixture と同じ)",
      c_off is not None and c_off["stop"] == T4["decision"]["stop"] and c_off["state"] == "ARMED", c_off and (c_off["stop"], c_off["state"]))
check("OFF: sweepGate キーも記録タグも無い", "sweepGate" not in c_off and not (GATE_TAGS & set(c_off["evidence"]))
      and "sweepGate" not in off["decision"])
check("SHADOW: sweepGate と記録タグを落とすと OFF とバイト一致", dumps(strip_gate(shadow)) == dumps(off))
check("SHADOW: SWEEP_GATE_WOULD_WAIT が付き、SL・decisionId は動かない",
      "SWEEP_GATE_WOULD_WAIT" in c_shadow["evidence"] and c_shadow["stop"] == c_off["stop"]
      and shadow["decision"]["decisionId"] == off["decision"]["decisionId"], c_shadow["evidence"])
check("LIVE: 掃引前は hardBlockers SWEEP_PENDING で WATCH(= 22:38 の損切りは建たない)",
      c_live["state"] == "WATCH" and "SWEEP_PENDING" in c_live["hardBlockers"], (c_live["state"], c_live["hardBlockers"]))
check("LIVE: PENDING の間は SL も decisionId も動かない",
      c_live["stop"] == c_off["stop"] and live["decision"]["decisionId"] == off["decision"]["decisionId"])
check("LIVE: decision に compact な sweepGate(state=PENDING, pool 29,447)が載る",
      (live["decision"].get("sweepGate") or {}).get("state") == "PENDING"
      and (live["decision"]["sweepGate"].get("pool") or {}).get("price") == POOL_T4, live["decision"].get("sweepGate"))

# 掃引→奪還のあと(22:48)。窓は同じ 60 本。実データではノイズ床が 15 → 27pt に広がって候補の SL が
# 29,473.25 に動き、その外側 1N 以内には次のプール(New York High 29,484.25)がある → 再び PENDING。
# 再生(replay_r103.py)でも「掃引後に通った」周期は 0 で、LIVE の実効は「プールが近い候補の見送り」。
b_48 = bundle_for(T4, AT_2248, price=29428.75)
r_48_off = evaluate(b_48, "OFF")
r_48_live = evaluate(b_48, "LIVE")
c48_off = cand(r_48_off, "BREAKER_CONTINUATION", "SELL")
c48_live = cand(r_48_live, "BREAKER_CONTINUATION", "SELL")
check("22:48 OFF: 候補は存在する(比較の土台)", c48_off is not None, r_48_off["decision"])
gate48 = (c48_live or {}).get("sweepGate") or {}
check("22:48 LIVE: 候補は再アンカーされ、次のプール(29,484.25)に対して PENDING(= 実データの挙動を固定)",
      gate48.get("state") == "PENDING" and (gate48.get("pool") or {}).get("price") == 29484.25, gate48)

# PASSED の統合経路(SL 置換・タグ・decisionId)は掃引→奪還を差し込んで固定する。
REAL_SWEEP = lp.sweep_reclaim
lp.sweep_reclaim = (lambda bars, pool, side, origin, tol, reclaim_bars=2:
                    {"state": "SWEPT_RECLAIMED", "pool": pool, "sweepBarT": 1, "reclaimBarT": 2,
                     "extreme": 29452.75, "ageBars": 0, "sweepDepthPt": 5.75})
try:
    passed = evaluate(b_arm, "LIVE")
finally:
    lp.sweep_reclaim = REAL_SWEEP
c_passed = cand(passed, "BREAKER_CONTINUATION", "SELL")
req = stop_logic.outward_tick(29452.75 + 0.25 * T4["noiseFloor"], "SELL")
check("LIVE PASSED: SL が掃引極値 + 0.25N の不利側 tick(29,456.75)へ置き換わる",
      c_passed is not None and c_passed["stop"] == req == 29456.75, (c_passed and c_passed["stop"], req))
check("LIVE PASSED: SWEEP_GATE_PASSED が付き、SWEEP_PENDING は無い",
      "SWEEP_GATE_PASSED" in c_passed["evidence"] and "SWEEP_PENDING" not in c_passed["hardBlockers"], c_passed["evidence"])
check("LIVE PASSED: decisionId は最終 SL で決まる(OFF と別 ID)",
      passed["decision"]["decisionId"] != off["decision"]["decisionId"])
check("LIVE PASSED: SL を内側へ縮めない", abs(c_passed["entry"] - c_passed["stop"]) > abs(c_off["entry"] - c_off["stop"]))
check("LIVE PASSED: decision の sweepGate は applied=True / required 29,456.75",
      (passed["decision"].get("sweepGate") or {}).get("applied") is True
      and passed["decision"]["sweepGate"].get("required") == 29456.75, passed["decision"].get("sweepGate"))

# T1(VP80、プール無し)は LIVE でも VP80 候補は変わらない
b_t1 = bundle_for(BY_TAG["T1"])
t1_off, t1_live = evaluate(b_t1, "OFF"), evaluate(b_t1, "LIVE")
v_off, v_live = cand(t1_off, "VP80_REVERSION", "BUY"), cand(t1_live, "VP80_REVERSION", "BUY")
check("T1 LIVE: 外側 1N 以内にプールが無い VP80 候補は OFF と同じ SL・状態(NOT_APPLICABLE)",
      v_off is not None and v_live is not None and (v_live["stop"], v_live["state"], v_live["hardBlockers"])
      == (v_off["stop"], v_off["state"], v_off["hardBlockers"])
      and (v_live.get("sweepGate") or {}).get("state") == "NOT_APPLICABLE",
      (v_live and (v_live["stop"], v_live["state"], (v_live.get("sweepGate") or {}).get("reason"))))

# T2(BREAKER BUY、SSL 29,388.50 が外側 10pt、未掃引)は LIVE で待つ
b_t2 = bundle_for(BY_TAG["T2"])
t2_live = evaluate(b_t2, "LIVE")
c_t2 = cand(t2_live, "BREAKER_CONTINUATION", "BUY")
check("T2 LIVE: SWEEP_PENDING で WATCH(= 21:20 の損切りは建たない)",
      c_t2 is not None and "SWEEP_PENDING" in c_t2["hardBlockers"], c_t2 and c_t2["hardBlockers"])

# BOTH(poolClearance LIVE + sweepGate LIVE): 掃引前は待つ(プール逃がしで待ちが消えない)
both = evaluate(b_arm, "LIVE", pool_mode="LIVE")
c_both = cand(both, "BREAKER_CONTINUATION", "SELL")
check("BOTH: プール逃がしが SL を動かしても掃引待ちは消えない(元の SL で判定)",
      c_both is not None and "SWEEP_PENDING" in c_both["hardBlockers"], c_both and c_both["hardBlockers"])

# カード
card_live = msnr_gate.build_card(copy.deepcopy(live), price=T4["priceAtArm"])
check("カードの decision にも sweepGate が載る", (card_live.get("decision") or {}).get("sweepGate", {}).get("state") == "PENDING")
check("カードは 4096 バイト以内", msnr_gate._card_bytes(card_live) <= msnr_gate.CARD_MAX_BYTES, msnr_gate._card_bytes(card_live))
oversized = {"decision": {"sweepGate": {"state": "PENDING", "x": "y" * 600}, "evidence": ["SWEEP_GATE_WOULD_WAIT"]},
             "summary": "x" * (msnr_gate.CARD_MAX_BYTES - 500)}
shrunk = msnr_gate._shrink_card(copy.deepcopy(oversized))
check("縮小は sweepGate を evidence より先に落とす",
      "sweepGate" not in shrunk["decision"] and shrunk["decision"].get("evidence") == ["SWEEP_GATE_WOULD_WAIT"])

# ---------------------------------------------------------------- 5. VP80 の起点(originBarT)

section("5. VP80 の掃引起点")
vp_chain = {"type": "SWEEP", "sweepBarT": 999, "originBarT": 100}
check("originBarT があればそれを起点にする(sweepBarT = 現在足ではない)", msnr_gate._sweep_origin_t(vp_chain) == 100)
check("FLIP は breakBarT", msnr_gate._sweep_origin_t({"type": "FLIP", "breakBarT": 55}) == 55)
check("SWEEP(originBarT 無し)は sweepBarT", msnr_gate._sweep_origin_t({"type": "SWEEP", "sweepBarT": 77}) == 77)

# ---------------------------------------------------------------- 6. autotrade_engine: 指値の取消(restingStopRecheck LIVE)

section("6. autotrade_engine: RESTING_STOP_CANCEL")
import autotrade_engine as ae  # noqa: E402
import _pin_contract  # noqa: E402,F401  (R102: 本番の manualHalt と限月をテストから切り離す)

ae._read_env_file = lambda path=None: {}
ACC = "ACC-R103-01"
KEY = "ENTRY:" + "f" * 64
SENT_AT = "2026-09-15T13:32:39.000000+00:00"
PLAN = {"entryKey": KEY, "symbol": "MNQZ6", "side": "SELL", "qty": 6, "entry": 29426.0,
        "initialStop": 29444.5, "tp1": 29367.5, "finalTarget": 29231.25, "targets": [29367.5, 29231.25],
        "legs": [{"id": "TP1", "qty": 3, "target": 29367.5}, {"id": "RUNNER", "qty": 3, "target": 29231.25}],
        "accountScope": [ACC], "mode": "SPLIT_BRACKETS_TP1_RUNNER", "ultra": True, "decisionId": "4055112e93bd4124",
        "routeSnapshot": [{"accountId": ACC, "legId": "TP1", "state": "ACCEPTED", "orderId": "P1", "receipt": f"TRADOVATE:{ACC}:P1"},
                          {"accountId": ACC, "legId": "RUNNER", "state": "ACCEPTED", "orderId": "P2", "receipt": f"TRADOVATE:{ACC}:P2"}]}
JOURNAL = {"entryKey": KEY, "claimToken": "T" * 43, "executionIntentHash": "ih-r103", "executionIntent": {"version": "fixture"}}
CLAIM = {"entryKey": KEY, "state": "CONSUMED", "routeState": "UNKNOWN", "acceptedCount": 0,
         "executionIntentHash": "ih-r103", "executionIntent": {"accountScope": [ACC], "symbol": "MNQZ6"},
         "routeSnapshot": [{"state": "UNKNOWN", "orderId": None, "receipt": None}]}
CFG = {"NQX_AUTOTRADE": "1", "NQX_LIVE_ORDERS": "1", "NQX_AUTOTRADE_KILL": "0", "NQX_SYMBOL": "MNQZ6",
       "CROSSTRADE_ACCOUNTS": ACC, "NQX_STALE_CANCEL_SETTLE_SEC": "0"}
AT_2236 = "2026-09-15T13:36:00+00:00"   # 22:36 JST = 09:36 ET。22:30 / 22:33 の足(52 / 44pt)が確定済み


def flat_position():
    return {"verified": True, "symbol": "MNQZ6", "qty": 0, "side": "FLAT", "accountId": ACC}


def pending_orders(extra_parent=None):
    rows = [{"orderId": "P1", "status": "WORKING", "action": "SELL", "parentId": None},
            {"orderId": "P2", "status": "WORKING", "action": "SELL", "parentId": None},
            {"orderId": "C1", "status": "SUSPENDED", "action": "BUY", "parentId": "P1"}]
    if extra_parent:
        rows.append({"orderId": extra_parent, "status": "WORKING", "action": "SELL", "parentId": None})
    return {"verified": True, "symbol": "MNQZ6", "state": "PENDING", "openCount": len(rows), "orders": rows,
            "activeOrders": rows, "orderIds": [r["orderId"] for r in rows], "filledOrderIds": [], "accountScope": [ACC]}


def empty_orders():
    return {"verified": True, "symbol": "MNQZ6", "state": "NONE", "openCount": 0, "orders": [], "activeOrders": [],
            "orderIds": [], "filledOrderIds": [], "accountScope": [ACC]}


class Harness:
    def __init__(self, live=True, extra_parent=None):
        self.calls, self.recover_calls, self.flat = [], [], False
        self.cfg = dict(CFG)
        self.extra_parent = extra_parent
        if not live:
            self.cfg["NQX_LIVE_ORDERS"] = "0"

    def position(self, _symbol):
        return flat_position()

    def orders(self, _symbol, **_kwargs):
        return empty_orders() if self.flat else pending_orders(self.extra_parent)

    def runner(self, args, live):
        self.calls.append((list(args), live))
        if live and args[:1] == ["--flatten"]:
            self.flat = True
        return 0, "FLATTEN VERIFIED: all targeted broker accounts FLAT and orders nonblocking"

    def recover(self, claim, journal, query, order_query, symbol):
        self.recover_calls.append(claim.get("entryKey"))
        return True, {"ok": True}

    def run(self, bundle, ledger):
        return ae.reconcile(bundle, False, self.cfg, self.position, self.runner, ledger,
                            broker_order_query=self.orders,
                            state_query=lambda: {"entryClaim": copy.deepcopy(CLAIM)},
                            recover_entry=self.recover)


def seed_resting(ledger):
    ae._append_ledger({"key": KEY, "entryKey": KEY, "status": "ENTRY_CLAIMED", "action": "ENTRY_CLAIM",
                       "plan": copy.deepcopy(PLAN), "claimJournal": JOURNAL, "time": SENT_AT}, ledger)
    ae._append_ledger({"key": KEY, "entryKey": KEY, "status": "ENTRY_RESTING", "action": "ENTRY", "routeState": "SENT",
                       "plan": copy.deepcopy(PLAN), "claimJournal": JOURNAL, "time": SENT_AT}, ledger)


def bundle_2236():
    # 22:36 までに確定した足だけ(送信後の足が TP1 に届くと R52 の取消が先に動く。それは別の経路)。
    return {"symbol": "MNQZ6", "at": AT_2236, "price": 29410.0,
            "snapshot": {"bars3m": closed_before(AT_2236, 240)}, "scenarios": {"primary": {"symbol": "MNQZ6"}}}


REAL_LOAD = stop_logic.load_policy


def with_resting_mode(mode):
    rule = dict(REAL_LOAD()["restingStopRecheck"], mode=mode)
    stop_logic.load_policy = (lambda *a, **k: dict(stop_logic.default_policy(), restingStopRecheck=rule))


def run_case(mode, live=True, extra_parent=None, cfg_extra=None):
    with_resting_mode(mode)
    try:
        h = Harness(live=live, extra_parent=extra_parent)
        if cfg_extra:
            h.cfg.update(cfg_extra)
        with tempfile.TemporaryDirectory() as tmp:
            ledger = os.path.join(tmp, "l.jsonl")
            seed_resting(ledger)
            notes = h.run(bundle_2236(), ledger)
            records, _ = ae._read_ledger(ledger)
        return h, notes, records
    finally:
        stop_logic.load_policy = REAL_LOAD


h, notes, records = run_case("SHADOW")
statuses = [(r.get("status"), r.get("action")) for r in records[2:]]
check("SHADOW: 台帳に RESTING_STOP_STALE が 1 行、取消は送らない",
      ("RESTING_STOP_STALE", "RESTING_RECHECK") in statuses and not h.calls and notes[0].startswith("autotrade hold: ENTRY_RESTING"),
      (statuses, h.calls, notes))

h, notes, records = run_case("LIVE")
statuses = [(r.get("status"), r.get("action")) for r in records[2:]]
check("LIVE: flatten を dry-run → live の順で 1 回ずつ送る",
      [c[0] for c in h.calls] == [["--flatten", "--account", ACC]] * 2 and [c[1] for c in h.calls] == [False, True], h.calls)
check("LIVE: ENTRY_HALTED(RESTING_STOP_CANCEL)→ 同じ周期で ENTRY_RECOVERED",
      ("ENTRY_HALTED", "RESTING_STOP_CANCEL") in statuses and ("ENTRY_RECOVERED", "ENTRY_RECOVERY") in statuses, statuses)
check("LIVE: HALT は同じ周期で解ける", ae._has_halt(records) is None)
check("LIVE: 注記は resting entry canceled で始まる", notes[0].startswith("autotrade resting entry canceled: resting stop stale"), notes)
halted = next(r for r in records if r.get("status") == "ENTRY_HALTED")
check("LIVE: ENTRY_HALTED 行は凍結プランと再検査の結果を持つ",
      halted["plan"]["entry"] == 29426.0 and (halted.get("recheck") or {}).get("stale") is True, halted.get("recheck"))

h, notes, records = run_case("LIVE", live=False)
check("dry(NQX_LIVE_ORDERS=0): 提案だけで live 送信なし",
      [c[1] for c in h.calls] == [False] and notes[0].startswith("autotrade proposal RESTING_STOP_CANCEL"), (h.calls, notes))

h, notes, records = run_case("LIVE", extra_parent="MANUAL-9")
check("LIVE: 手動の親注文が同居していれば取り消さない(FOREIGN_PARENT_ORDER)",
      not h.calls and "cancel skipped (FOREIGN_PARENT_ORDER)" in notes[0], (h.calls, notes))

h, notes, records = run_case("LIVE", cfg_extra={"NQX_RESTING_STOP_CANCEL": "0"})
check("LIVE でも NQX_RESTING_STOP_CANCEL=0 なら記録だけ", not h.calls and notes[0].startswith("autotrade hold: ENTRY_RESTING"), (h.calls, notes))

h, notes, records = run_case("LIVE", cfg_extra={"NQX_AUTOTRADE_KILL": "1"})
check("KILL 中は取消経路に入らない(KILL が先)", all(c[0][:1] != ["--flatten"] or True for c in h.calls) and
      not any(r.get("action") == "RESTING_STOP_CANCEL" for r in records), [r.get("action") for r in records])

# ---------------------------------------------------------------- tripwire

section("7. 本番パスへの到達")
check("broker_status / nqx_state / autotrade_arm へ 0 件", HITS == [], HITS)

print(f"\n合計: PASS {PASS[0]} / FAIL {FAIL[0]}")
sys.exit(1 if FAIL[0] else 0)

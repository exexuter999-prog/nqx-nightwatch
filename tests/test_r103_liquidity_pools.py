# -*- coding: utf-8 -*-
"""R103-1: SL 側の流動性プール検出と、SL をプールの向こうへ逃がす契約(既定 SHADOW)。

    python tests/test_r103_liquidity_pools.py

tests/fixtures/r103/ の 2026-09-15 の 4 件を、武装時点の窓(cases[].closedBarsAtArm = 60 本)で
そのまま評価する。ネットワーク・台帳・発注に到達しないことも tripwire で確かめる。
"""
import copy
import json
import os
import shutil
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

# ---------------------------------------------------------------- fixture

BARS = load("bars3m_2026-09-15.json")["bars"]
CASES = load("cases_2026-09-15.json")["cases"]
BY_TAG = {case["tag"]: case for case in CASES}
PRM = msnr_gate.params()

section("0. fixture")
check("3 分足がある", len(BARS) > 0, str(len(BARS)))
check("4 件ある", len(CASES) == 4, str(len(CASES)))
check("窓は 60 本", all(case["closedBarsAtArm"] == 60 for case in CASES))


def window(case):
    """武装時点の確定足 60 本(形成中の足は含めない)。"""
    at = datetime.fromisoformat(case["armedAt"]).timestamp()
    rows = [bar for bar in BARS if bar["t"] + BAR_SEC <= at]
    return rows[-int(case["closedBarsAtArm"]):]


def geometry(case):
    rows = window(case)
    nf = msnr_gate.noise_floor(rows)
    return rows, nf, max(PRM["touch_pt"], 0.10 * nf)


def audit_for(case, rule=None):
    rows, nf, tol = geometry(case)
    pools = lp.pools(rows, case["levels"], case["priceAtArm"], tol, nf)
    decision = case["decision"]
    return pools, lp.stop_pool_audit(decision["side"], decision["entry"], decision["stop"],
                                     pools, nf, rule or stop_logic.load_policy()["poolClearance"],
                                     model=decision["model"])


def prices(rows):
    return [(row["price"], row["kind"]) for row in rows]


# ---------------------------------------------------------------- 1. pools

section("1. pools(純関数)")
rows_t4, nf_t4, tol_t4 = geometry(BY_TAG["T4"])
pools_t4 = lp.pools(rows_t4, BY_TAG["T4"]["levels"], BY_TAG["T4"]["priceAtArm"], tol_t4, nf_t4)
check("ノイズ床が fixture と一致(窓の取り方が同じ)", round(nf_t4, 2) == BY_TAG["T4"]["noiseFloor"],
      f"{nf_t4} vs {BY_TAG['T4']['noiseFloor']}")
check("SWING_HIGH は msnr_gate.swing_liquidity と同じ集合",
      sorted(row["price"] for row in pools_t4 if row["kind"] == "SWING_HIGH")
      == sorted(BY_TAG["T4"]["swingLiquidity"]["BSL"]),
      prices(pools_t4))
check("29447.00 は SWING_HIGH と VA_EDGE の二重(まとめない)",
      [row["kind"] for row in pools_t4 if row["price"] == 29447.0] == ["SWING_HIGH", "VA_EDGE"],
      prices(pools_t4))
check("SESSION は Asia/London/New York の High|Low だけ",
      {row["label"] for row in pools_t4 if row["kind"] == "SESSION"}
      == {"London High", "London Low", "New York High", "New York Low"}, prices(pools_t4))
check("Weekly open / 6pm open はプールにしない",
      all("open" not in row["label"] for row in pools_t4), prices(pools_t4))
check("C: POC はプールにしない", all("POC" not in row["label"] for row in pools_t4), prices(pools_t4))
va = next(row for row in pools_t4 if row["kind"] == "VA_EDGE" and row["price"] == 29447.0)
check("VA_EDGE に barT / ageBars / equalCount が付く",
      va["equalCount"] >= 1 and va["barT"] is not None and va["ageBars"] is not None, va)
check("未回収のスイング(swing_liquidity の返す集合)は swept=False",
      all(row["swept"] is False for row in pools_t4 if row["kind"] in ("SWING_HIGH", "SWING_LOW")),
      [(row["price"], row["kind"], row["swept"]) for row in pools_t4])
check("最後に触れた後に下抜けた水準は swept=True(C: VAL 29426.00)",
      next(row for row in pools_t4 if row["price"] == 29426.0)["swept"] is True)
check("PD_EXTREME のラベルを拾う",
      [row["kind"] for row in lp.pools(rows_t4, [{"label": "Previous Day High", "price": 29500.0},
                                                 {"label": "PDL", "price": 29200.0}],
                                       BY_TAG["T4"]["priceAtArm"], tol_t4, nf_t4)
       if row["kind"] == "PD_EXTREME"] == ["PD_EXTREME", "PD_EXTREME"])
check("levels も bars も無ければ空", lp.pools([], [], 29400.0, 2.0, 15.0) == [])

frozen_bars = copy.deepcopy(rows_t4)
frozen_levels = copy.deepcopy(BY_TAG["T4"]["levels"])
again = lp.pools(rows_t4, BY_TAG["T4"]["levels"], BY_TAG["T4"]["priceAtArm"], tol_t4, nf_t4)
check("純関数: 同じ入力で同じ出力", again == pools_t4)
check("純関数: 入力を書き換えない", rows_t4 == frozen_bars and BY_TAG["T4"]["levels"] == frozen_levels)

# ---------------------------------------------------------------- 2. stop_pool_audit

section("2. stop_pool_audit(4 件の幾何)")
_, a4 = audit_for(BY_TAG["T4"])
check("22:38: beyondWithinN に 29447.00(SWING_HIGH と VA_EDGE)",
      prices(a4["beyondWithinN"]) == [(29447.0, "SWING_HIGH"), (29447.0, "VA_EDGE")], a4["beyondWithinN"])
expected_required = stop_logic.outward_tick(29447.0 + 0.25 * BY_TAG["T4"]["noiseFloor"], "SELL")
check("22:38: required = 29447 + 0.25N の不利側 tick(= 29451.00)",
      a4["required"] == expected_required == 29451.0, (a4["required"], expected_required))
check("22:38: SL は 29447.00 の ±0.25N 内側(inside025N)",
      prices(a4["inside025N"]) == [(29447.0, "SWING_HIGH"), (29447.0, "VA_EDGE")], a4["inside025N"])
check("22:38: nearestBeyond は 29447.00(gap 2.5pt)",
      a4["nearestBeyond"]["price"] == 29447.0 and a4["nearestBeyond"]["gapPt"] == 2.5, a4["nearestBeyond"])
check("22:38: applied は純関数では常に False", a4["applied"] is False)

_, a2 = audit_for(BY_TAG["T2"])
check("21:20: beyondWithinN に 29388.50", prices(a2["beyondWithinN"]) == [(29388.5, "SWING_LOW")],
      a2["beyondWithinN"])
check("21:20: between に 29402.25", (29402.25, "SWING_LOW") in prices(a2["between"]), a2["between"])
check("21:20: required は 29388.50 の外側(不利側 tick)",
      a2["required"] == stop_logic.outward_tick(29388.5 - 0.25 * BY_TAG["T2"]["noiseFloor"], "BUY"),
      a2["required"])

for tag, label in (("T1", "16:49"), ("T3", "21:34")):
    _, audit = audit_for(BY_TAG[tag])
    check(f"{label}: beyondWithinN は空", audit["beyondWithinN"] == [], audit["beyondWithinN"])
    check(f"{label}: between は非空", bool(audit["between"]), audit["between"])
    check(f"{label}: required は None", audit["required"] is None, audit["required"])

check("16:49 の理由は STOP_ALREADY_BEYOND_POOL(SL が全プールの向こう)",
      audit_for(BY_TAG["T1"])[1]["reason"] == "STOP_ALREADY_BEYOND_POOL")
check("21:34 の理由は POOL_FAR_FROM_STOP(外側のプールが 1N より遠い)",
      audit_for(BY_TAG["T3"])[1]["reason"] == "POOL_FAR_FROM_STOP")

RULE = stop_logic.load_policy()["poolClearance"]
check("プールが 1 つも無ければ NO_POOL",
      lp.stop_pool_audit("SELL", 29426.0, 29444.5, [], 15.38, RULE)["reason"] == "NO_POOL")
check("levels も bars も無い(None)なら POOLS_MISSING",
      lp.stop_pool_audit("SELL", 29426.0, 29444.5, None, 15.38, RULE)["reason"] == "POOLS_MISSING")
check("幾何が無ければ GEOMETRY_MISSING",
      lp.stop_pool_audit("SELL", None, 29444.5, [], 15.38, RULE)["reason"] == "GEOMETRY_MISSING")
check("ノイズ床が無ければ NOISE_FLOOR_MISSING",
      lp.stop_pool_audit("SELL", 29426.0, 29444.5, [], None, RULE)["reason"] == "NOISE_FLOOR_MISSING")
check("SL が建値の逆側なら STOP_ON_WRONG_SIDE",
      lp.stop_pool_audit("SELL", 29450.0, 29400.0, [], 15.38, RULE)["reason"] == "STOP_ON_WRONG_SIDE")
check("契約の models に無いモデルは MODEL_NOT_IN_POLICY",
      lp.stop_pool_audit("SELL", 29426.0, 29444.5, [], 15.38, dict(RULE, models=["VP80_REVERSION"]),
                         model="BREAKER_CONTINUATION")["reason"] == "MODEL_NOT_IN_POLICY")
check("kinds で種別を絞れる(VA_EDGE だけ外すと 1 本になる)",
      len(audit_for(BY_TAG["T4"], dict(RULE, kinds=["SWING_HIGH", "SWING_LOW", "SESSION",
                                                    "PD_EXTREME"]))[1]["beyondWithinN"]) == 1)

# ---------------------------------------------------------------- 3. 契約と load_policy

section("3. execution_contract.json と load_policy")
policy = stop_logic.load_policy()
check("poolClearance の既定は SHADOW", policy["poolClearance"]["mode"] == "SHADOW", policy["poolClearance"])
check("restingStopRecheck の既定は SHADOW", policy["restingStopRecheck"]["mode"] == "SHADOW",
      policy["restingStopRecheck"])
check("既定は LIVE ではない", policy["poolClearance"]["mode"] != "LIVE"
      and policy["restingStopRecheck"]["mode"] != "LIVE")

broken = {"stopLogic": {"vwapClearance": {"mode": "LIVE"}, "marketStopGuard": {"mode": "LIVE"},
                        "poolClearance": {"mode": "LIVE", "withinN": -1, "kinds": ["NOPE"]},
                        "restingStopRecheck": {"mode": "LIVE", "minN": 0,
                                               "sessionOpen": {"minutes": "30", "opensEt": ["9h30"]}}}}
fallback = stop_logic.load_policy(broken)
check("壊れた poolClearance はこの節だけ OFF", fallback["poolClearance"]["mode"] == "OFF")
check("壊れた restingStopRecheck もこの節だけ OFF", fallback["restingStopRecheck"]["mode"] == "OFF")
check("他の節(vwapClearance / marketStopGuard)は生きたまま",
      fallback["vwapClearance"]["mode"] == "LIVE" and fallback["marketStopGuard"]["mode"] == "LIVE")

# ---------------------------------------------------------------- 4. msnr_gate 統合

section("4. msnr_gate.evaluate(OFF / SHADOW / LIVE)")
REAL_POLICY = msnr_gate.stop_logic_policy
POOL_TAGS = {"STOP_POOL_WITHIN_1N", "POOL_BETWEEN_ENTRY_STOP", "POOL_STOP_CLEARED"}


def bundle_for(case):
    rows = window(case)
    at = case["armedAt"]
    return {"at": at, "priceAt": at, "price": case["priceAtArm"],
            "snapshot": {"levels": copy.deepcopy(case["levels"]), "bars3m": copy.deepcopy(rows)}}


def evaluate(case, pool_mode):
    pol = dict(stop_logic.default_policy())
    pol["poolClearance"] = dict(stop_logic.DEFAULT_POOL, mode=pool_mode)
    msnr_gate.stop_logic_policy = (lambda: pol)
    try:
        return msnr_gate.evaluate(bundle_for(case))
    finally:
        msnr_gate.stop_logic_policy = REAL_POLICY


def strip_pool(result):
    """poolStop と記録専用タグだけを落とす(それ以外は 1 バイトも触らない)。"""
    out = copy.deepcopy(result)
    for candidate in out.get("candidates") or []:
        candidate.pop("poolStop", None)
        candidate["evidence"] = [tag for tag in candidate.get("evidence") or [] if tag not in POOL_TAGS]
    decision = out.get("decision") or {}
    decision.pop("poolStop", None)
    decision["evidence"] = [tag for tag in decision.get("evidence") or [] if tag not in POOL_TAGS]
    return out


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


case4 = BY_TAG["T4"]
off = evaluate(case4, "OFF")
shadow = evaluate(case4, "SHADOW")
cand_off = next(c for c in off["candidates"] if c["model"] == "BREAKER_CONTINUATION" and c["side"] == "SELL")
cand_shadow = next(c for c in shadow["candidates"] if c["model"] == "BREAKER_CONTINUATION" and c["side"] == "SELL")
check("OFF: 候補の SL は fixture の SL", cand_off["stop"] == case4["decision"]["stop"], cand_off["stop"])
check("OFF: poolStop も記録タグも無い(R103 以前と同一)",
      "poolStop" not in cand_off and not (POOL_TAGS & set(cand_off["evidence"]))
      and "poolStop" not in off["decision"], cand_off.get("poolStop"))
check("SHADOW: poolStop と記録タグを落とすと OFF とバイト一致",
      dumps(strip_pool(shadow)) == dumps(off))
check("SHADOW: SL・score・grade・decisionId は動かない",
      (cand_shadow["stop"], cand_shadow["score"], cand_shadow["grade"], shadow["decision"]["decisionId"])
      == (cand_off["stop"], cand_off["score"], cand_off["grade"], off["decision"]["decisionId"]),
      (cand_shadow["stop"], shadow["decision"]["decisionId"]))
check("SHADOW: 候補に STOP_POOL_WITHIN_1N が付く", "STOP_POOL_WITHIN_1N" in cand_shadow["evidence"],
      cand_shadow["evidence"])
check("SHADOW: 22:38 の required は 29451.00", cand_shadow["poolStop"]["required"] == 29451.0,
      cand_shadow["poolStop"])
check("SHADOW: decision に compact な poolStop が載る",
      shadow["decision"]["poolStop"].get("required") == 29451.0
      and shadow["decision"]["poolStop"].get("applied") is False, shadow["decision"].get("poolStop"))
shadow2 = evaluate(BY_TAG["T2"], "SHADOW")
cand2 = next(c for c in shadow2["candidates"] if c["side"] == "BUY")
check("SHADOW: 21:20 は POOL_BETWEEN_ENTRY_STOP も付く",
      "POOL_BETWEEN_ENTRY_STOP" in cand2["evidence"], cand2["evidence"])

live = evaluate(case4, "LIVE")
cand_live = next((c for c in live["candidates"] if c["model"] == "BREAKER_CONTINUATION" and c["side"] == "SELL"), None)
check("LIVE: SL が required に置き換わる", cand_live is not None and cand_live["stop"] == 29451.0,
      cand_live and cand_live["stop"])
check("LIVE: POOL_STOP_CLEARED が付く", "POOL_STOP_CLEARED" in cand_live["evidence"], cand_live["evidence"])
check("LIVE: SL を内側へ縮めない", abs(cand_live["entry"] - cand_live["stop"])
      >= abs(cand_off["entry"] - cand_off["stop"]))
check("LIVE: decisionId は最終 SL で決まる(SHADOW と別 ID)",
      live["decision"]["decisionId"] != off["decision"]["decisionId"])

# ---------------------------------------------------------------- 5. LEVEL_CHOPPED_3

section("5. LEVEL_CHOPPED_3(記録専用)")
chop_bars = [{"t": 0, "o": 0, "h": 1, "l": -1, "c": c} for c in (1, -1, 1, -1)]
check("20 本の終値が 3 回以上横断したら True", msnr_gate.level_chopped(chop_bars, 0.0))
check("2 回なら False", msnr_gate.level_chopped(chop_bars[:3], 0.0) is False)
check("同値の足だけでは横断を数えない",
      msnr_gate.level_chopped([{"c": 0.0} for _ in range(20)], 0.0) is False)
check("22:38 の C: VAL(29426.00 = 建値のアンカー)は揉み合い",
      msnr_gate.level_chopped(rows_t4, 29426.0))
check("離れたレベル(P: VAH 29603.00)は横断していない",
      msnr_gate.level_chopped(rows_t4, 29603.0) is False)
check("候補の evidence に LEVEL_CHOPPED_3 が載る", "LEVEL_CHOPPED_3" in cand_off["evidence"],
      cand_off["evidence"])
check("LEVEL_CHOPPED_3 は score を動かさない(OFF と SHADOW で同点)",
      cand_off["score"] == cand_shadow["score"])

# ---------------------------------------------------------------- 6. resting_stop_recheck

section("6. resting_stop_recheck(指値の SL 再検査)")
RULE_RESTING = stop_logic.load_policy()["restingStopRecheck"]
AT_2236 = "2026-09-15T13:36:00+00:00"          # 22:36 JST = 09:36 ET(22:30 / 22:33 の足を含む)
verdict = stop_logic.resting_stop_recheck("SELL", case4["decision"]["entry"], case4["decision"]["stop"],
                                          BARS, AT_2236, RULE_RESTING)
check("22:36 JST の 22:38 ケースは stale", verdict["stale"] is True, verdict)
check("noiseNow は直前 12 本の中央値(15.38)", verdict["noiseNow"] == 15.38, verdict)
check("noiseSessionOpen は寄付き以降のレンジ中央値(48.25)", verdict["noiseSessionOpen"] == 48.25, verdict)
check("distPt は建値→SL の 18.50pt", verdict["distPt"] == 18.5, verdict)
check("理由は RESTING_STOP_BELOW_MIN_N", verdict["reason"] == "RESTING_STOP_BELOW_MIN_N", verdict)
check("寄付き以降の確定足は 2 本(22:30 / 22:33)", verdict.get("barsSinceOpen") == 2, verdict)

before_open = stop_logic.resting_stop_recheck("SELL", 29426.0, 29344.5, BARS,
                                              "2026-09-15T13:24:00+00:00", RULE_RESTING)
check("寄付き前は noiseNow を使う(= noiseSessionOpen)",
      before_open["noiseSessionOpen"] == before_open["noiseNow"], before_open)
check("SL が 1N 以上あれば stale ではない", before_open["stale"] is False, before_open)
check("時刻が無ければ推測しない(AT_TIME_MISSING)",
      stop_logic.resting_stop_recheck("SELL", 29426.0, 29444.5, BARS, None,
                                      RULE_RESTING)["reason"] == "AT_TIME_MISSING")
check("12 本未満なら NOISE_FLOOR_MISSING",
      stop_logic.resting_stop_recheck("SELL", 29426.0, 29444.5, BARS[:5], AT_2236,
                                      RULE_RESTING)["reason"] == "NOISE_FLOOR_MISSING")
check("幾何が無ければ GEOMETRY_MISSING",
      stop_logic.resting_stop_recheck("SELL", None, 29444.5, BARS, AT_2236,
                                      RULE_RESTING)["reason"] == "GEOMETRY_MISSING")

# ---------------------------------------------------------------- 7. autotrade_engine の台帳 1 行

section("7. autotrade_engine: RESTING_STOP_STALE を 1 行だけ")
import autotrade_engine  # noqa: E402

TMPDIR = tempfile.mkdtemp(prefix="r103_")
LEDGER = os.path.join(TMPDIR, "autotrade_ledger.jsonl")
PLAN = {"decisionId": "r103test", "entryKey": "ENTRY:r103test", "side": "SELL",
        "entry": case4["decision"]["entry"], "initialStop": case4["decision"]["stop"],
        "symbol": "MNQU6"}
BUNDLE = {"at": AT_2236, "snapshot": {"bars3m": BARS}}
engine_verdict = autotrade_engine._resting_stop_recheck(PLAN, BUNDLE)
check("engine から見ても stale", isinstance(engine_verdict, dict) and engine_verdict.get("stale") is True,
      engine_verdict)
try:
    records = []
    autotrade_engine._note_resting_stop_stale(records, PLAN, engine_verdict, LEDGER)
    with open(LEDGER, encoding="utf-8") as fh:
        rows = [json.loads(line) for line in fh if line.strip()]
    check("台帳に 1 行", len(rows) == 1, rows)
    row = rows[0]
    check("status / action が契約どおり",
          row["status"] == "RESTING_STOP_STALE" and row["action"] == "RESTING_RECHECK", row)
    check("plan を持たない(凍結プランとして拾われない)", "plan" not in row, row)
    autotrade_engine._note_resting_stop_stale(rows, PLAN, engine_verdict, LEDGER)
    with open(LEDGER, encoding="utf-8") as fh:
        again_rows = [json.loads(line) for line in fh if line.strip()]
    check("同じ key・同じ理由では 2 行目を書かない", len(again_rows) == 1, again_rows)
finally:
    shutil.rmtree(TMPDIR, ignore_errors=True)

off_rule = dict(RULE_RESTING, mode="OFF")
REAL_LOAD = stop_logic.load_policy
stop_logic.load_policy = (lambda *a, **k: dict(stop_logic.default_policy(),
                                               restingStopRecheck=off_rule))
try:
    check("OFF なら engine は評価しない", autotrade_engine._resting_stop_recheck(PLAN, BUNDLE) is None)
finally:
    stop_logic.load_policy = REAL_LOAD

# ---------------------------------------------------------------- tripwire

section("8. 本番パスへの到達")
check("broker_status / nqx_state / autotrade_arm へ 0 件", HITS == [], HITS)

print(f"\n合計: PASS {PASS[0]} / FAIL {FAIL[0]}")
sys.exit(1 if FAIL[0] else 0)

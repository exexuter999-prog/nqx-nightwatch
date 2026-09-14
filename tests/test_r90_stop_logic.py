# -*- coding: utf-8 -*-
"""R90: 初期 SL の 3 つの穴(VWAP 逃がし / 成行の SL 距離ゲート / BREAKER の錨)。

msnr_gate.evaluate と stop_logic は純粋関数。order.py は隔離サンドボックス(_hermetic)で
ドライランだけ。ネットワーク・本番台帳・CrossTrade に触れない。

守ること:
  1. 契約が無い・壊れている節は **その節だけ** OFF。既定の全 OFF は R90 以前と同じ判定。
  2. 穴 1: VWAP が SL の近く(±1N)なら SL を VWAP の外側 0.25N へ(BUY は切り下げ、SELL は切り上げ)。
     SHADOW は記録だけ、LIVE だけが置き換え、置き換えると decisionId が変わり記録タグが付く。
  3. 穴 2: 成行の「発注時点の価格 → SL」が 1N 未満なら engine は claim の前に見送り、
     order.py は MARKET_STOP_TOO_CLOSE で落ちる。測れなければ出さない(fail-closed)。
  4. 穴 3: FLIP の錨は「安値が切り下がる限り」遡った起点。OFF では chain にキーを足さない。

    python tests/test_r90_stop_logic.py
"""
import copy
import os
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import _hermetic  # noqa: E402

BASE = os.path.dirname(HERE)
sys.path.insert(0, BASE)

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
    print(f"\n--- {title} ---")


def policy(**spec):
    return stop_logic.load_policy({"stopLogic": spec})


# 2026-09-14 23:26 JST の実トレード(docs/R90 §1)
E, SL, VWAP, NF, PRICE = 29002.0, 28968.5, 28958.15, 29.88, 28996.75

section("0. 方針の読み取り(壊れた節だけ OFF)")
p = stop_logic.load_policy({})
check("節が無ければ全部 OFF", p["vwapClearance"]["mode"] == "OFF" and p["marketStopGuard"]["mode"] == "OFF"
      and p["flipOrigin"]["mode"] == "OFF", p)
p = policy(vwapClearance={"mode": "MAYBE"}, marketStopGuard={"mode": "LIVE"}, flipOrigin={"mode": "LIVE", "maxBarsBack": 3})
check("未知の mode はその節だけ OFF、他は生きる",
      p["vwapClearance"]["mode"] == "OFF" and p["marketStopGuard"]["mode"] == "LIVE"
      and p["flipOrigin"] == {"mode": "LIVE", "maxBarsBack": 3}, p)
p = policy(vwapClearance={"mode": "LIVE", "models": ["NOPE"]})
check("未知のモデル名は節ごと OFF", p["vwapClearance"]["mode"] == "OFF", p)
p = policy(vwapClearance={"mode": "LIVE", "withinN": 9.0})
check("withinN の範囲外は節ごと OFF", p["vwapClearance"]["mode"] == "OFF", p)
p = policy(flipOrigin={"mode": "LIVE", "maxBarsBack": True})
check("maxBarsBack が bool なら OFF", p["flipOrigin"]["mode"] == "OFF", p)
real = stop_logic.load_policy()
check("本番契約の stopLogic は検証を通る(各節が既定へ倒れていない)",
      real["version"] == "R90-STOP-LOGIC-1" and real["marketStopGuard"]["mode"] == "LIVE", real)

section("1. tick の丸め(不利側)")
check("BUY は切り下げ", stop_logic.outward_tick(28950.68, "BUY") == 28950.5)
check("SELL は切り上げ", stop_logic.outward_tick(29049.32, "SELL") == 29049.5)
check("tick 上の値はそのまま", stop_logic.outward_tick(29000.25, "BUY") == 29000.25
      and stop_logic.outward_tick(29000.25, "SELL") == 29000.25)

section("2. 穴 1: VWAP の外側へ(純粋関数)")
rule = policy(vwapClearance={"mode": "LIVE"})["vwapClearance"]
a = stop_logic.vwap_clearance("BUY", E, SL, VWAP, NF, rule, model="BREAKER_CONTINUATION")
check("23:26 の幾何は対象(VWAP は SL の 10.35pt = 0.35N 下)", a["eligible"] and abs(a["gapPt"] - 10.35) < 0.01, a)
check("SL は VWAP − 0.25N(7.47pt)を切り下げた 28,950.50", a["stop"] == 28950.5, a)
check("リスクは 33.5pt → 51.5pt(60pt 上限内)", a["riskPt"] == 51.5, a)
check("押しの安値 28,955.75 は新 SL の外側にならない(5.25pt 残す)", 28955.75 - a["stop"] == 5.25, a)
s = stop_logic.vwap_clearance("SELL", 29000.0, 29033.5, 29043.85, NF, rule)
check("SELL は鏡像(VWAP + 0.25N を切り上げ)", s["eligible"] and s["stop"] == 29051.5, s)
far = stop_logic.vwap_clearance("BUY", E, SL, SL - 1.5 * NF, NF, rule)
check("VWAP が SL の 1N より遠ければ触らない", not far["eligible"] and far["reason"] == "VWAP_FAR_FROM_STOP", far)
inside = stop_logic.vwap_clearance("BUY", E, SL, SL + 10.0, NF, rule)
check("VWAP が SL の内側(建値寄り)に 0.25N 以上あれば既に外側 = 触らない",
      not inside["eligible"] and inside["reason"] == "STOP_ALREADY_BEYOND_VWAP", inside)
on = stop_logic.vwap_clearance("BUY", E, SL, SL, NF, rule)
check("SL が VWAP ちょうど(ユーザーの言う『SL が VWAP にある』)は対象", on["eligible"] and on["stop"] == SL - 7.5, on)
just = stop_logic.vwap_clearance("BUY", E, SL, SL + 2.0, NF, rule)
check("VWAP が SL の内側 2pt(0.25N 未満)でも 0.25N 外側へ出す", just["eligible"] and just["stop"] < SL, just)
nomodel = stop_logic.vwap_clearance("BUY", E, SL, VWAP, NF, {**rule, "models": ["VP80_REVERSION"]}, model="BREAKER_CONTINUATION")
check("対象モデル外は MODEL_NOT_IN_POLICY", not nomodel["eligible"] and nomodel["reason"] == "MODEL_NOT_IN_POLICY", nomodel)
missing = stop_logic.vwap_clearance("BUY", E, SL, None, NF, rule)
check("VWAP 欠落は VWAP_MISSING(推測で補わない)", not missing["eligible"] and missing["reason"] == "VWAP_MISSING", missing)
above = stop_logic.vwap_clearance("BUY", 28960.0, 28958.0, 28970.0, NF, rule)
check("VWAP が建値の向こう側なら SL は既に外側(動かさない)",
      not above["eligible"] and above["reason"] == "STOP_ALREADY_BEYOND_VWAP", above)
wrong = stop_logic.vwap_clearance("BUY", 28950.0, 28958.0, 28940.0, NF, rule)
check("SL が建値の向こう側なら幾何不正として動かさない", not wrong["eligible"] and wrong["reason"] == "STOP_ON_WRONG_SIDE", wrong)

section("3. 穴 2: 成行の SL 距離(純粋関数)")
mrule = policy(marketStopGuard={"mode": "LIVE"})["marketStopGuard"]
g = stop_logic.market_stop_guard("BUY", SL, PRICE, NF, mrule)
check("23:26: 公開価格から SL まで 28.25pt(0.95N)< 29.88pt → 出さない",
      g["block"] and g["reason"] == "MARKET_STOP_TOO_CLOSE" and g["distPt"] == 28.25, g)
g2 = stop_logic.market_stop_guard("BUY", 28950.5, PRICE, NF, mrule)
check("VWAP 逃がし後の SL なら 46.25pt ≥ 29.88pt → 通る(minPt を order.py へ渡す)",
      not g2["block"] and g2["minPt"] == 29.88, g2)
g3 = stop_logic.market_stop_guard("SELL", 29033.5, 29005.0, NF, mrule)
check("SELL は鏡像(28.5pt < 29.88 → 出さない)", g3["block"] and g3["distPt"] == 28.5, g3)
g4 = stop_logic.market_stop_guard("BUY", SL, PRICE, None, mrule)
check("ノイズ床が測れなければ出さない(fail-closed)", g4["block"] and g4["reason"] == "NOISE_FLOOR_MISSING", g4)
g5 = stop_logic.market_stop_guard("BUY", 29010.0, PRICE, NF, mrule)
check("SL が価格の向こう側なら出さない", g5["block"] and g5["reason"] == "STOP_ON_WRONG_SIDE_OF_PRICE", g5)
check("ノイズ床は evaluation.volGate.noise を優先",
      stop_logic.noise_from_bundle({"evaluation": {"volGate": {"noise": 29.88}}, "snapshot": {"bars3m": []}}) == 29.88)
bars12 = [{"t": i, "h": 100 + i, "l": 90 + i} for i in range(12)]
check("無ければ確定 3 分足 12 本のレンジ中央値", stop_logic.noise_from_bundle({"snapshot": {"bars3m": bars12}}) == 10.0)
check("12 本無ければ None(推測しない)", stop_logic.noise_from_bundle({"snapshot": {"bars3m": bars12[:11]}}) is None)

section("4. 穴 3: FLIP の錨(純粋関数)")
lows = [100, 96, 98, 99]
bars = [{"l": low, "h": low + 5} for low in lows]
check("直前の切り下がりを遡る(99 → 98 → 96 で止まる。100 は含めない)",
      stop_logic.flip_origin_index(bars, 3, "BUY", 5) == 1)
mono = [{"l": low, "h": low + 5} for low in (90, 95, 97, 99)]
check("単調な上昇は起点まで", stop_logic.flip_origin_index(mono, 3, "BUY", 5) == 0)
check("maxBarsBack で遡りを止める", stop_logic.flip_origin_index(mono, 3, "BUY", 1) == 2)
highs = [{"h": high, "l": high - 5} for high in (110, 114, 112, 111)]
check("SELL は高値の切り上がりで鏡像", stop_logic.flip_origin_index(highs, 3, "SELL", 5) == 1)
check("ブレイク足が先頭なら自分自身", stop_logic.flip_origin_index(mono, 0, "BUY", 5) == 0)

# ------------------------------------------------------------ msnr_gate 統合
section("5. msnr_gate 統合(BREAKER BUY の RBS フリップ)")
T0 = 1786970000 - (1786970000 % 180)
P = 30000.0
TARGETS = [{"label": "Weekly High", "price": 30100.0}, {"label": "Monthly High", "price": 30200.0}]
FILLER = (29984, 29990, 29982, 29986)
RBS = [FILLER] * 12 + [
    (29990, 30010, 29988, 30006),    # 12 break: 抵抗を実体で上抜け
    (30006, 30014, 30004, 30010),    # 13 acceptance
    (30008, 30010, 29999, 30008),    # 14 revisit & hold
    (30008, 30018, 30006, 30016),    # 15
    (30016, 30026, 30014, 30024),    # 16
]


def bundle(spec, vwap=None):
    rows = [{"t": T0 + 180 * i, "o": o, "h": h, "l": l, "c": c, "v": 1000}
            for i, (o, h, l, c) in enumerate(spec)]
    price_at = datetime.fromtimestamp(rows[-1]["t"] + 180, timezone.utc).isoformat()
    out = {"at": price_at, "priceAt": price_at, "price": rows[-1]["c"],
           "snapshot": {"levels": [{"label": "TEST", "price": P}] + TARGETS, "bars3m": rows}}
    if vwap is not None:
        out["vwap"] = vwap
    return out


REAL_POLICY = msnr_gate.stop_logic_policy


def evaluate(spec, pol, vwap=None):
    msnr_gate.stop_logic_policy = (lambda: pol)
    try:
        return msnr_gate.evaluate(bundle(spec, vwap))
    finally:
        msnr_gate.stop_logic_policy = REAL_POLICY


def breaker(result):
    return next((c for c in result["candidates"] if c["model"] == "BREAKER_CONTINUATION" and c["side"] == "BUY"), None)


def flip_chain(result):
    level = next(lv for lv in result["levels"] if lv["label"] == "TEST")
    return next((c for c in level["chains"] if c["type"] == "FLIP" and c["side"] == "BUY"), None)


OFF = stop_logic.default_policy()
base = evaluate(RBS, OFF, vwap=29975.0)
cand = breaker(base)
nf = base["noiseFloor"]
buffer = msnr_gate.model_stop_buffer(nf)
check("OFF: BREAKER BUY 候補があり SL = ブレイク足安値 − 緩衝", cand is not None and cand["stop"] == 29988 - buffer, (cand or {}).get("stop"))
check("OFF: vwapStop も記録タグも無い(R90 以前と同一)", cand is not None and "vwapStop" not in cand
      and "VWAP_STOP_CLEARED" not in cand["evidence"], cand and cand.get("vwapStop"))
check("OFF: chain に flip の新キーが無い", flip_chain(base) is not None and "flipExtreme" not in flip_chain(base))

shadow = evaluate(RBS, policy(vwapClearance={"mode": "SHADOW"}), vwap=29975.0)
sc = breaker(shadow)
check("SHADOW: SL は変えない", sc["stop"] == cand["stop"], sc["stop"])
check("SHADOW: 『動かした場合』が記録される(eligible / applied=False)",
      sc["vwapStop"]["eligible"] and not sc["vwapStop"]["applied"] and sc["vwapStop"]["stop"] < cand["stop"], sc["vwapStop"])
check("SHADOW: タグは付かない・decisionId は同じ", "VWAP_STOP_CLEARED" not in sc["evidence"]
      and shadow["decision"]["decisionId"] == base["decision"]["decisionId"])

live = evaluate(RBS, policy(vwapClearance={"mode": "LIVE"}), vwap=29975.0)
lc = breaker(live)
expected = stop_logic.outward_tick(29975.0 - 0.25 * nf, "BUY")
check("LIVE: SL が VWAP − 0.25N の外側へ", lc["stop"] == expected and lc["stop"] < cand["stop"], (lc["stop"], expected))
check("LIVE: 記録タグ VWAP_STOP_CLEARED", "VWAP_STOP_CLEARED" in lc["evidence"], lc["evidence"])
check("LIVE: targetR は新しい SL で計算し直されている",
      abs(lc["targetR"][0] - (30100 - lc["entry"]) / (lc["entry"] - lc["stop"])) < 0.02, lc["targetR"])
check("LIVE: decisionId が変わる(setup_identity は SL を含む)",
      live["decision"]["decisionId"] != base["decision"]["decisionId"])
check("LIVE: decision に監査 vwapStop が乗る", live["decision"].get("vwapStop", {}).get("applied") is True, live["decision"].get("vwapStop"))
card = msnr_gate.build_card(live, price=live["decision"]["entry"], side="BUY",
                            entry=live["decision"]["entry"], stop=live["decision"]["stop"])
check("LIVE: カード(4096B 上限)にも監査が乗る", (card.get("decision") or {}).get("vwapStop", {}).get("applied") is True)
far = evaluate(RBS, policy(vwapClearance={"mode": "LIVE"}), vwap=29900.0)
check("LIVE でも VWAP が遠ければ SL は動かない(理由が残る)",
      breaker(far)["stop"] == cand["stop"] and breaker(far)["vwapStop"]["reason"] == "VWAP_FAR_FROM_STOP")

# 穴 3: ブレイクの 1 本前の安値が深い(上昇の起点)
RBS_ORIGIN = copy.deepcopy(RBS)
RBS_ORIGIN[11] = (29984, 29990, 29970, 29986)
base_o = evaluate(RBS_ORIGIN, OFF)
live_o = evaluate(RBS_ORIGIN, policy(flipOrigin={"mode": "LIVE", "maxBarsBack": 5}))
nf_o = base_o["noiseFloor"]
buf_o = msnr_gate.model_stop_buffer(nf_o)
check("FLIP OFF: SL はブレイク足の安値 − 緩衝", breaker(base_o)["stop"] == 29988 - buf_o, breaker(base_o)["stop"])
ch = flip_chain(live_o)
check("FLIP LIVE: chain に起点(1 本前)と極値が乗る",
      ch and ch.get("flipOriginBarT") == T0 + 180 * 11 and ch.get("flipExtreme") == 29970 and ch.get("flipExtremeBeyondBreak"), ch)
check("FLIP LIVE: SL は起点の安値 − 緩衝", breaker(live_o)["stop"] == 29970 - buf_o, breaker(live_o)["stop"])
check("FLIP LIVE: 記録タグ FLIP_STOP_ORIGIN と decisionId の変化",
      "FLIP_STOP_ORIGIN" in breaker(live_o)["evidence"]
      and live_o["decision"]["decisionId"] != base_o["decision"]["decisionId"])
# ブレイクの 1 本前の安値がブレイク足より高い(起点はブレイク足自身)
RBS_FLAT = copy.deepcopy(RBS)
RBS_FLAT[11] = (29990, 29992, 29989, 29991)
base_f = evaluate(RBS_FLAT, OFF)
live_same = evaluate(RBS_FLAT, policy(flipOrigin={"mode": "LIVE"}))
check("FLIP LIVE: 起点が動かなければ SL もタグも変わらない",
      breaker(base_f) is not None and breaker(live_same)["stop"] == breaker(base_f)["stop"]
      and "FLIP_STOP_ORIGIN" not in breaker(live_same)["evidence"]
      and flip_chain(live_same).get("flipExtremeBeyondBreak") is False,
      (breaker(base_f) or {}).get("stop"), )
walk = evaluate(RBS, policy(flipOrigin={"mode": "LIVE"}))
check("FLIP LIVE: 直前の足(安値 29,982 < ブレイク足 29,988)まで遡る",
      flip_chain(walk).get("flipExtreme") == 29982 and breaker(walk)["stop"] == 29982 - buffer, flip_chain(walk))

# ------------------------------------------------------------ engine
section("6. autotrade_engine: claim の前の見送りと --min-stop-pt")
import autotrade_engine as ae  # noqa: E402

REAL_LOAD = stop_logic.load_policy
LIVE_GUARD = REAL_LOAD({"stopLogic": {"marketStopGuard": {"mode": "LIVE"}}})
OFF_GUARD = REAL_LOAD({"stopLogic": {"marketStopGuard": {"mode": "OFF"}}})
stop_logic.load_policy = lambda contract=None: LIVE_GUARD
try:
    plan = {"side": "BUY", "initialStop": SL, "entry": E, "qty": 2, "symbol": "MNQU6",
            "legs": [{"target": 29070.25}, {"target": 29464.0}]}
    bundle_e = {"price": PRICE, "evaluation": {"volGate": {"noise": NF}}}
    verdict = ae._market_stop_guard(plan, bundle_e)
    check("23:26 の成行は見送り(claim の前)", verdict["block"] and verdict["reason"] == "MARKET_STOP_TOO_CLOSE", verdict)
    ok = ae._market_stop_guard({**plan, "initialStop": 28950.5}, bundle_e)
    check("逃がした SL なら通り、minPt = 1.0N を返す", not ok["block"] and ok["minPt"] == NF, ok)
    args = ae._command_for_entry({**plan, "marketStopMinPt": ok["minPt"]}, bundle_e)
    check("成行の引数に --min-stop-pt が乗る", "--min-stop-pt" in args and args[args.index("--min-stop-pt") + 1] == str(NF), args)
    args_limit = ae._command_for_entry({**plan, "entry": 28990.0, "marketStopMinPt": NF}, bundle_e)
    check("指値には付けない", "--min-stop-pt" not in args_limit and "--entry" in args_limit, args_limit)
    args_plain = ae._command_for_entry(plan, bundle_e)
    check("minPt が無ければ従来どおり", "--min-stop-pt" not in args_plain, args_plain)
    stop_logic.load_policy = lambda contract=None: OFF_GUARD
    off = ae._market_stop_guard(plan, bundle_e)
    check("OFF なら何もしない", not off["block"] and off["reason"] == "GUARD_OFF", off)
finally:
    stop_logic.load_policy = REAL_LOAD

# ------------------------------------------------------------ order.py(隔離)
section("7. order.py: MARKET_STOP_TOO_CLOSE(隔離ドライラン)")
os.environ["NQX_MODIFY_FRESH_QUOTE"] = "0"          # 隔離環境に tv_fetch は無い(--last で検査)
SANDBOX = _hermetic.make_sandbox(BASE)
COMMON = ["--side", "buy", "--qty", "2", "--market", "--last", str(PRICE),
          "--split-tp", "29070.25,29464", "--symbol", "MNQU6"]
ok, out = _hermetic.dry_run(SANDBOX, COMMON + ["--sl", str(SL), "--min-stop-pt", str(NF)])
check("28.25pt < 29.88pt は MARKET_STOP_TOO_CLOSE で落ちる", not ok and "MARKET_STOP_TOO_CLOSE" in out, out[-300:])
ok, out = _hermetic.dry_run(SANDBOX, COMMON + ["--sl", "28950.5", "--min-stop-pt", str(NF)])
check("46.25pt ≥ 29.88pt はドライランを通り、検査が印字される", ok and "stop-distance guard" in out, out[-400:])
ok, out = _hermetic.dry_run(SANDBOX, COMMON + ["--sl", str(SL)])
check("--min-stop-pt が無ければ従来どおり通る(手動送信を塞がない)", ok, out[-300:])
ok, out = _hermetic.dry_run(SANDBOX, COMMON + ["--sl", str(SL), "--min-stop-pt", "0"])
check("0 以下の下限は拒否", not ok and "--min-stop-pt" in out, out[-200:])

print(f"\n合計: PASS {PASS[0]} / FAIL {FAIL[0]}")
sys.exit(1 if FAIL[0] else 0)

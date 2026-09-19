# -*- coding: utf-8 -*-
"""R119: 新規武装だけに掛かる 2 つの門(execution_contract.json の limitGate)。

msnr_gate.evaluate / build_candidates は純粋関数(契約 JSON を読むだけ)。ネットワーク・台帳・
発注に触れない。このファイルも同じで、`.secrets` にも本番の記録にも書かない。

守ること:
  1. 既定(節が無い / 壊れている / mode OFF)は R119 以前と同じ判定。
  2. gapCap は **発注時に LIMIT になる候補だけ**に掛かる。MARKET になる候補(建値が現値を
     通過している)は、距離がいくら遠くても落とさない。注文種別の判定は
     autotrade_engine._entry_order_type と同じ式であることを実際に突き合わせる。
  3. targetPassed は BUY / SELL の向きを区別する(BUY は 現値 >= tp1、SELL は 現値 <= tp1)。
  4. どちらも**鮮度確認済みの現値**でだけ判定する。確認できない周期は掛けず、理由を残す。
     確定足の終値へは落ちない。
  5. LIVE で当たった候補は state=WATCH + hardBlockers になり、claim を取れる状態
     (allowedStates)に入らない = claim 取得前に止まる。別モデルは primary になれる。
  6. SHADOW は観測だけ(ブロッカーを立てない)。
  7. 建値 / SL / targets / decisionId を書き換えない。
  8. 保有建玉の管理(autotrade_engine / order.py)はこの門を一切参照しない。
"""
import os
import re
import sys
from datetime import datetime, timezone

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import execution_contract  # noqa: E402
import msnr_gate  # noqa: E402

TURTLE = "TURTLE_SOUP_REVERSAL"
BREAKER = "BREAKER_CONTINUATION"
GAP = "LIMIT_GAP_EXCEEDED"
PASSED = "TARGET_ALREADY_PASSED"
REAL_POLICY = msnr_gate.limit_gate_policy
T0 = 1786970000 - (1786970000 % 180)

# test_r89_model_gate.py と同じフィクスチャ。RESTING は「レベルへ先回りする指値」で、
# 現値 30,019 / 建値 30,000 / SL 29,986 = gapR 1.357 の実物。
ABOVE = [(30012, 30018, 30010, 30014) for _ in range(12)]
RESTING = ABOVE + [
    (29998, 30012, 29994, 30010),
    (30010, 30022, 30008, 30019),
]

failures = []


def check(label, condition, detail=""):
    if not condition:
        failures.append(label)
        print(f"FAIL {label}: {detail}")
        return
    print(f"OK   {label}")


def policy(gap_mode="LIVE", max_gap=1.5, passed_mode="LIVE",
           models=(BREAKER, TURTLE, "VP80_REVERSION", "OTE_FVG_PULLBACK")):
    return {"gapCap": {"mode": gap_mode, "maxGapR": max_gap, "models": list(models)},
            "targetPassed": {"mode": passed_mode}, "invalid": []}


def bundle(spec, price=None):
    bars = [{"t": T0 + 180 * i, "o": o, "h": h, "l": l, "c": c, "v": 1000}
            for i, (o, h, l, c) in enumerate(spec)]
    at = datetime.fromtimestamp(bars[-1]["t"] + 180, timezone.utc).isoformat()
    return {"at": at, "priceAt": at, "price": bars[-1]["c"] if price is None else price,
            "snapshot": {"levels": [{"label": "TEST", "price": 30000.0},
                                    {"label": "Weekly High", "price": 30100.0},
                                    {"label": "Monthly High", "price": 30200.0}],
                         "bars3m": bars}}


def cand(side, entry, stop, tp1, final=None, model=BREAKER):
    return {"model": model, "side": side, "entry": entry, "stop": stop,
            "targets": [tp1, final if final is not None else tp1]}


# ---------------------------------------------------------------- 1. 既定

def test_policy_loader():
    check("節が無ければ全部 OFF",
          REAL_POLICY({}) == {"gapCap": {"mode": "OFF"}, "targetPassed": {"mode": "OFF"},
                              "invalid": []})
    broken = REAL_POLICY({"limitGate": "LIVE"})
    check("節の形が壊れていれば全部 OFF + invalid",
          broken["gapCap"]["mode"] == "OFF" and broken["invalid"] == ["LIMIT_GATE_MALFORMED"], broken)
    bad = REAL_POLICY({"limitGate": {"gapCap": {"mode": "ON", "maxGapR": 1.5, "models": [BREAKER]},
                                     "targetPassed": {"mode": 7}}})
    check("未知の mode はその節だけ OFF にして invalid に残す",
          bad["gapCap"]["mode"] == "OFF" and bad["targetPassed"]["mode"] == "OFF"
          and len(bad["invalid"]) == 2, bad)
    nomodels = REAL_POLICY({"limitGate": {"gapCap": {"mode": "LIVE", "maxGapR": 1.5, "models": ["NOPE"]}}})
    check("既知のモデルが 1 つも無い models は OFF",
          nomodels["gapCap"]["mode"] == "OFF"
          and "LIMIT_GATE_GAP_CAP_MALFORMED" in nomodels["invalid"], nomodels)
    negative = REAL_POLICY({"limitGate": {"gapCap": {"mode": "LIVE", "maxGapR": 0, "models": [BREAKER]}}})
    check("maxGapR <= 0 は OFF", negative["gapCap"]["mode"] == "OFF", negative)
    # 戻しは「mode を OFF にする 1 語」。maxGapR / models を消しても invalid にしない。
    bare_off = REAL_POLICY({"limitGate": {"gapCap": {"mode": "OFF"}, "targetPassed": {"mode": "OFF"}}})
    check("mode OFF だけの節は不正にしない(1 語で戻せる)",
          bare_off["gapCap"] == {"mode": "OFF"} and bare_off["invalid"] == [], bare_off)
    kept_off = REAL_POLICY({"limitGate": {"gapCap": {"mode": "OFF", "maxGapR": 1.5, "models": [BREAKER]}}})
    check("mode OFF なら maxGapR / models が残っていても OFF",
          kept_off["gapCap"] == {"mode": "OFF"} and kept_off["invalid"] == [], kept_off)


def test_production_contract_is_valid():
    real = REAL_POLICY()
    check("本番契約の limitGate は読めて、不正な節が無い", real["invalid"] == [], real)
    check("本番契約は gapCap=LIVE / maxGapR=1.5 / targetPassed=LIVE(2026-09-19 ユーザー決定)",
          real["gapCap"]["mode"] == "LIVE" and real["gapCap"]["maxGapR"] == 1.5
          and real["targetPassed"]["mode"] == "LIVE", real)
    check("maxGapR は msnr_gate.LIMIT_MAX_GAP_R と一致(2 つの指値経路を分岐させない)",
          real["gapCap"]["maxGapR"] == msnr_gate.LIMIT_MAX_GAP_R,
          (real["gapCap"]["maxGapR"], msnr_gate.LIMIT_MAX_GAP_R))
    check("本番契約の models は既知のモデルだけ",
          all(m in msnr_gate.MODEL_ORDER for m in real["gapCap"]["models"]), real)


def test_off_mode_changes_nothing():
    audit = msnr_gate.limit_gate_audit(cand("BUY", 29699.5, 29661.75, 29763.25), 29800.0,
                                       None, policy(gap_mode="OFF", passed_mode="OFF"))
    check("両方 OFF ならブロッカーも観測も無い",
          audit["blocker"] is None and not audit.get("observed") and audit["reason"] == "GATE_OFF", audit)


# ------------------------------------------- 2. gapCap は LIMIT になる候補だけ

def test_order_type_matches_engine():
    """注文種別の判定が autotrade_engine._entry_order_type と一致すること。

    ここがずれると、成行になるはずの候補を指値専用の向き判定で落とす(またはその逆)。
    """
    import autotrade_engine
    grid = []
    for side in ("BUY", "SELL"):
        for entry in (29650.0, 29699.5, 29700.0, 29750.0):
            for price in (29650.0, 29699.5, 29700.0, 29750.0):
                grid.append((side, entry, price))
    mismatch = []
    for side, entry, price in grid:
        plan = {"side": side, "entry": entry}
        engine = autotrade_engine._entry_order_type(plan, {"price": price})
        mine = "LIMIT" if msnr_gate.limit_entry_side({"side": side, "entry": entry}, price) else "MARKET"
        if engine != mine:
            mismatch.append((side, entry, price, engine, mine))
    check(f"{len(grid)} 通りで engine の注文種別判定と一致", not mismatch, mismatch[:5])


def test_gap_cap_only_hits_limit_orders():
    pol = policy()
    # BUY 指値: 建値 29,699.5 / SL 29,661.75(risk 37.75)
    buy = cand("BUY", 29699.5, 29661.75, 29763.25, 29967.25)
    near = msnr_gate.limit_gate_audit(buy, 29712.5, None, pol)
    check("gapR 0.34 の指値は通る(昨夜の実物。1.0 への引き締めは保留)",
          near["orderType"] == "LIMIT" and near["gapR"] == 0.344 and near["blocker"] is None, near)
    far = msnr_gate.limit_gate_audit(buy, 29757.0, None, pol)
    check("gapR 1.52 の指値は LIMIT_GAP_EXCEEDED",
          far["orderType"] == "LIMIT" and far["blocker"] == GAP, far)
    # 境界: gapR == maxGapR は通す(「超えたら」外す)
    edge_price = 29699.5 + 1.5 * 37.75
    edge = msnr_gate.limit_gate_audit(buy, edge_price, None, pol)
    check("gapR == maxGapR ちょうどは通す",
          edge["gapR"] == 1.5 and GAP not in (edge.get("blockers") or []), edge)
    # 建値が現値を通過 = MARKET。距離が遠くても gapCap は掛けない
    market = msnr_gate.limit_gate_audit(buy, 29650.0, None, pol)
    check("MARKET になる BUY 候補は gapR 1.31 でも落とさない",
          market["orderType"] == "MARKET" and market["gapR"] > 1.0 and market["blocker"] is None, market)
    sell = cand("SELL", 29534.5, 29558.75, 29489.75, 29473.75)
    market_sell = msnr_gate.limit_gate_audit(sell, 29560.0, None, pol)
    check("MARKET になる SELL 候補も落とさない",
          market_sell["orderType"] == "MARKET" and market_sell["gapR"] > 1.0
          and market_sell["blocker"] is None, market_sell)
    other = msnr_gate.limit_gate_audit(cand("BUY", 29699.5, 29661.75, 29763.25, model="VP80_REVERSION"),
                                       29757.0, None, policy(models=(BREAKER,)))
    check("models に無いモデルには gapCap を掛けない", other["blocker"] is None, other)


# ------------------------------------------------- 3. targetPassed は向きを区別

def test_target_passed_is_side_aware():
    pol = policy(gap_mode="OFF")
    buy = cand("BUY", 29699.5, 29661.75, 29763.25, 29967.25)
    sell = cand("SELL", 29534.5, 29558.75, 29489.75, 29473.75)
    below = msnr_gate.limit_gate_audit(buy, 29712.5, None, pol)
    check("BUY: 現値が TP1 の下なら通す", below["tp1R"] > 0 and below["blocker"] is None, below)
    at_tp = msnr_gate.limit_gate_audit(buy, 29763.25, None, pol)
    check("BUY: 現値が TP1 ちょうどでも止める(tp1R = 0)",
          at_tp["tp1R"] == 0 and at_tp["blocker"] == PASSED, at_tp)
    above = msnr_gate.limit_gate_audit(buy, 29800.0, None, pol)
    check("BUY: 現値が TP1 を上に通過したら止める",
          above["tp1R"] < 0 and above["blocker"] == PASSED, above)
    up = msnr_gate.limit_gate_audit(sell, 29520.0, None, pol)
    check("SELL: 現値が TP1 の上なら通す", up["tp1R"] > 0 and up["blocker"] is None, up)
    down = msnr_gate.limit_gate_audit(sell, 29477.0, None, pol)
    check("SELL: 現値が TP1 を下に通過したら止める(09-09 18:42 の実物 tp1R -0.53)",
          down["tp1R"] < 0 and down["blocker"] == PASSED, down)
    check("向きが逆なら止めない(BUY の水準を SELL に当てても誤爆しない)",
          msnr_gate.limit_gate_audit(sell, 29800.0, None, pol)["blocker"] is None
          and msnr_gate.limit_gate_audit(buy, 29477.0, None, pol)["blocker"] is None)
    market_passed = msnr_gate.limit_gate_audit(cand("BUY", 29820.0, 29780.0, 29763.25), 29800.0, None, pol)
    check("注文種別で分けない: MARKET になる候補でも TP1 通過なら止める",
          market_passed["orderType"] == "MARKET" and market_passed["blocker"] == PASSED, market_passed)


# ----------------------------------------------- 4. 鮮度確認済みの現値でだけ判定

def test_requires_verified_price():
    at = "2026-09-18T16:44:15.438093+00:00"
    ok, reason = msnr_gate.gate_price({"price": 29713.25, "priceAt": at, "at": at})
    check("同時刻の priceAt は鮮度確認済み", ok == 29713.25 and reason is None, (ok, reason))
    stale, reason = msnr_gate.gate_price(
        {"price": 29713.25, "priceAt": "2026-09-18T15:00:00+00:00", "at": at})
    check("maxAgeSec を超えた priceAt は使わない",
          stale is None and reason.startswith("PRICE_STALE"), (stale, reason))
    check("priceAt が無い周期は使わない",
          msnr_gate.gate_price({"price": 1.0, "at": at})[1] == "NO_PRICE_AT")
    check("price が無い周期は使わない",
          msnr_gate.gate_price({"priceAt": at, "at": at})[1] == "NO_PRICE")
    bars_only = bundle(RESTING)
    bars_only.pop("price")
    check("確定足の終値へは落ちない(bars3m があっても NO_PRICE)",
          msnr_gate.gate_price(bars_only)[0] is None, msnr_gate.gate_price(bars_only))
    audit = msnr_gate.limit_gate_audit(cand("BUY", 29699.5, 29661.75, 29763.25),
                                       None, "PRICE_STALE_900S", policy())
    check("鮮度が確認できない周期は門を掛けず、理由を残す",
          audit["blocker"] is None and audit["reason"] == "PRICE_STALE_900S", audit)


# --------------------------------- 5/6/7. 配線・SHADOW・幾何を書き換えない

def evaluate(spec, pol, price=None):
    msnr_gate.limit_gate_policy = lambda contract=None: pol
    try:
        return msnr_gate.evaluate(bundle(spec, price=price))
    finally:
        msnr_gate.limit_gate_policy = REAL_POLICY


def test_wiring_blocks_before_claim():
    loose = evaluate(RESTING, policy(max_gap=1.5))
    tight = evaluate(RESTING, policy(max_gap=1.0))
    live = [c for c in loose["candidates"] if c["model"] == TURTLE]
    check("フィクスチャは gapR 1.357 の実物の指値候補",
          len(live) == 1 and live[0]["limitGate"]["orderType"] == "LIMIT"
          and 1.0 < live[0]["limitGate"]["gapR"] <= 1.5, [c.get("limitGate") for c in live])
    check("maxGapR 1.5 では当たらない", GAP not in live[0]["hardBlockers"], live[0]["hardBlockers"])
    hit = [c for c in tight["candidates"] if c["model"] == TURTLE]
    check("maxGapR 1.0 では LIMIT_GAP_EXCEEDED + WATCH",
          len(hit) == 1 and GAP in hit[0]["hardBlockers"] and hit[0]["state"] == "WATCH"
          and hit[0]["allowed"] is False, (hit[0]["hardBlockers"], hit[0]["state"]) if hit else None)
    allowed = execution_contract.CONTRACT["scenario"]["allowedStates"]
    check("WATCH は claim を取れる状態に入らない = claim 取得前に止まる", "WATCH" not in allowed, allowed)
    check("門で外した候補は決定の hardBlockers にも載る(primary になっていない)",
          tight["decision"]["model"] != TURTLE or GAP in tight["decision"]["hardBlockers"],
          tight["decision"].get("model"))


def test_shadow_records_without_blocking():
    shadow = evaluate(RESTING, policy(gap_mode="SHADOW", max_gap=1.0))
    cands = [c for c in shadow["candidates"] if c["model"] == TURTLE]
    check("SHADOW はブロッカーを立てない",
          len(cands) == 1 and GAP not in cands[0]["hardBlockers"], cands[0]["hardBlockers"] if cands else None)
    check("SHADOW でも観測は証跡に残る",
          GAP in (cands[0].get("evidence") or []) and GAP in (cands[0]["limitGate"].get("observed") or []),
          cands[0]["limitGate"] if cands else None)


def test_geometry_and_decision_id_unchanged():
    base = evaluate(RESTING, policy(gap_mode="OFF", passed_mode="OFF"))
    gated = evaluate(RESTING, policy(max_gap=1.0))
    keys = ("entry", "stop", "targets", "targetR", "grade", "score")
    for model in {c["model"] for c in base["candidates"]}:
        a = next(c for c in base["candidates"] if c["model"] == model)
        b = next((c for c in gated["candidates"] if c["model"] == model), None)
        check(f"{model}: 門は建値/SL/targets/等級/得点を書き換えない",
              b is not None and all(a.get(k) == b.get(k) for k in keys),
              {k: (a.get(k), (b or {}).get(k)) for k in keys})
    a = next(c for c in base["candidates"] if c["model"] == TURTLE)
    b = next(c for c in gated["candidates"] if c["model"] == TURTLE)
    check("同じ幾何なので setup_identity(= decisionId の素)も変わらない",
          msnr_gate.setup_identity(a) == msnr_gate.setup_identity(b),
          (msnr_gate.setup_identity(a), msnr_gate.setup_identity(b)))


def test_gated_candidate_does_not_displace_live_one():
    """R89 と同じ規律。外した候補が同じ side の生きている候補を押し出さない。"""
    spy = {}

    def fake(candidate, price, price_reason=None, pol=None):
        spy.setdefault("models", []).append(candidate.get("model"))
        blockers = [GAP] if candidate.get("model") == TURTLE else []
        return {"gapCapMode": "LIVE", "targetPassedMode": "LIVE", "blocker": blockers[0] if blockers else None,
                "blockers": blockers, "gapR": 9.0, "tp1R": 1.0, "orderType": "LIMIT", "reason": None}

    real_audit = msnr_gate.limit_gate_audit
    msnr_gate.limit_gate_audit = fake
    try:
        result = msnr_gate.evaluate(bundle(RESTING))
    finally:
        msnr_gate.limit_gate_audit = real_audit
    check("門は全候補に当てられる", spy.get("models"), spy)
    turtle = [c for c in result["candidates"] if c["model"] == TURTLE]
    check("外した候補は記録に残る(消えない)", len(turtle) == 1 and GAP in turtle[0]["hardBlockers"],
          turtle[0]["hardBlockers"] if turtle else None)
    check("外した候補は primary にならない",
          result["decision"]["model"] != TURTLE or GAP in result["decision"]["hardBlockers"],
          result["decision"].get("model"))


# ------------------------------------------- 8. 建玉管理から分離されていること

def test_management_path_never_reads_the_gate():
    names = ("limit_gate_audit", "limit_gate_policy", "limit_entry_side", "gate_price",
             "build_candidates", GAP, PASSED, "limitGate")
    for module in ("autotrade_engine.py", "order.py"):
        with open(os.path.join(BASE, module), encoding="utf-8") as fh:
            source = fh.read()
        found = [n for n in names if re.search(r"\b%s\b" % re.escape(n), source)]
        check(f"{module} は R119 の門を参照しない(保有建玉の管理と分離)", not found, found)


for fn in (test_policy_loader, test_production_contract_is_valid, test_off_mode_changes_nothing,
           test_order_type_matches_engine, test_gap_cap_only_hits_limit_orders,
           test_target_passed_is_side_aware, test_requires_verified_price,
           test_wiring_blocks_before_claim, test_shadow_records_without_blocking,
           test_geometry_and_decision_id_unchanged, test_gated_candidate_does_not_displace_live_one,
           test_management_path_never_reads_the_gate):
    fn()

if failures:
    print(f"\nFAILED {len(failures)}: {', '.join(failures)}")
    sys.exit(1)
print("\nR119 limit gate: ALL PASS")

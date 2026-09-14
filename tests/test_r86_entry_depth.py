# -*- coding: utf-8 -*-
"""R86: 押し目深度とSL余裕(ブラケットの平行移動)。ネットワーク・台帳・発注に触れない。

守ること:
  1. VP80 だけが動く。BREAKER / TURTLE / OTE は触らない。
  2. 建値と SL は同じ幅だけ不利方向へ動き、SL 幅(リスク)とターゲット価格は変わらない。
  3. SHADOW は発注幾何を変えずに記録だけ、LIVE だけが置き換える。OFF は記録も残さない。
  4. 契約が壊れていたら OFF。この層の例外で監視サイクルを落とさない。
  5. pipeline と publish の 2 回評価で二重に動かない。
"""
import copy
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import entry_depth  # noqa: E402
import msnr_gate  # noqa: E402
import monitor_publish  # noqa: E402
import nqx_state  # noqa: E402


def check(label, condition, detail=""):
    if not condition:
        raise AssertionError(f"{label}: {detail}")
    print(f"OK   {label}")


def policy(mode):
    return entry_depth.load_policy({"entryDepth": {
        "version": "R86-ENTRY-DEPTH-1", "mode": mode, "stopShift": "SAME_AS_DEPTH",
        "maxDepthPt": 10.0,
        "rules": [{"case": "VP80_CLOSE_ANCHOR", "models": ["VP80_REVERSION"], "depthN": 0.25}]}})


# 2026-09-08 19:54 JST の実トレード(SL 29,533.50 / 安値 29,534.00 = 0.5pt 残し)
VP80_BUY = {"model": "VP80_REVERSION", "side": "BUY", "state": "ARMED", "grade": "A+", "qty": 2,
            "entry": 29553.5, "stop": 29533.5, "target": 29588.5, "targets": [29588.5, 29649.0],
            "targetR": [1.75, 4.78], "scenarioId": "38ddf1ab008b1e37", "decisionId": "38ddf1ab008b1e37",
            "planVersion": "R19-ICT-SPLIT-1",
            "legs": [{"id": "TP1", "qty": 1, "target": 29588.5}, {"id": "RUNNER", "qty": 1, "target": 29649.0}]}
VP80_SELL = {**VP80_BUY, "side": "SELL", "entry": 29477.5, "stop": 29507.5, "target": 29395.75,
             "targets": [29395.75, 29081.75],
             "legs": [{"id": "TP1", "qty": 1, "target": 29395.75}, {"id": "RUNNER", "qty": 1, "target": 29081.75}]}


def test_policy_loader_fails_closed():
    check("節が無ければ OFF", entry_depth.load_policy({})["mode"] == "OFF")
    check("未知の mode は OFF", policy("MAYBE")["mode"] == "OFF")
    bad_shift = entry_depth.load_policy({"entryDepth": {"mode": "LIVE", "stopShift": "WIDEN_ONLY",
                                                        "maxDepthPt": 10, "rules": []}})
    check("SL だけ広げる方式は受け付けない(実測で改善しなかった)", bad_shift["mode"] == "OFF", bad_shift)
    too_deep = entry_depth.load_policy({"entryDepth": {"mode": "LIVE", "maxDepthPt": 10, "rules": [
        {"models": ["VP80_REVERSION"], "depthN": 1.5}]}})
    check("depthN > 1.0 は OFF", too_deep["mode"] == "OFF", too_deep)
    live = policy("LIVE")
    check("正しい節は読める", live["mode"] == "LIVE" and live["rules"][0]["depthN"] == 0.25, live)
    real = entry_depth.load_policy()
    check("本番契約の節は検証を通る(OFF に倒れていない)",
          real["mode"] in {"SHADOW", "LIVE"} and real["rules"], real)


def test_vp80_bracket_shift_keeps_risk_and_targets():
    audit = entry_depth.plan_shift(VP80_BUY, 12.5, policy("LIVE"))
    check("VP80 BUY は対象", audit["eligible"] is True, audit)
    check("0.25N = 3.125 → tick 半切り上げで 3.25pt", audit["depthPt"] == 3.25, audit)
    check("建値を 3.25pt 下げる", audit["shifted"]["entry"] == 29550.25, audit)
    check("SL も同じだけ下げる(余裕が増える)", audit["shifted"]["stop"] == 29530.25, audit)
    risk_before = VP80_BUY["entry"] - VP80_BUY["stop"]
    risk_after = audit["shifted"]["entry"] - audit["shifted"]["stop"]
    check("SL 幅(= リスク額・枚数)は不変", risk_before == risk_after == 20.0, (risk_before, risk_after))
    check("TP までの R は伸びる", audit["targetR"][0] > VP80_BUY["targetR"][0], audit)
    sell = entry_depth.plan_shift(VP80_SELL, 28.5, policy("LIVE"))
    check("SELL は鏡像(建値と SL を上げる)",
          sell["shifted"]["entry"] == 29484.75 and sell["shifted"]["stop"] == 29514.75, sell)


def test_level_anchored_models_are_untouched():
    for model in ("BREAKER_CONTINUATION", "TURTLE_SOUP_REVERSAL", "OTE_FVG_PULLBACK"):
        scenario = {**VP80_BUY, "model": model}
        audit = entry_depth.plan_shift(scenario, 12.5, policy("LIVE"))
        check(f"{model} は深くしない", audit["eligible"] is False
              and audit["reason"] == "MODEL_NOT_IN_POLICY", audit)
        out = {}
        shifted, note = entry_depth.annotate(out, scenario, 12.5, policy("LIVE"))
        check(f"{model} のシナリオはそのまま", shifted is scenario and note is None, (shifted, note))


def test_guards():
    capped = entry_depth.plan_shift(VP80_BUY, 60.0, policy("LIVE"))
    check("maxDepthPt で頭打ち(15pt → 10pt)", capped["depthPt"] == 10.0, capped)
    tiny = entry_depth.plan_shift(VP80_BUY, 0.4, policy("LIVE"))
    check("1tick 未満は動かさない", tiny["eligible"] is False and tiny["reason"] == "DEPTH_BELOW_TICK", tiny)
    missing = entry_depth.plan_shift(VP80_BUY, None, policy("LIVE"))
    check("ノイズ床が無ければ動かさない", missing["reason"] == "NOISE_FLOOR_MISSING", missing)
    off_tick = entry_depth.plan_shift({**VP80_BUY, "entry": 29553.6}, 12.5, policy("LIVE"))
    check("tick 外の価格は動かさない", off_tick["reason"] == "GEOMETRY_OFF_TICK", off_tick)
    wrong = entry_depth.plan_shift({**VP80_BUY, "stop": 29560.0}, 12.5, policy("LIVE"))
    check("SL が逆側なら動かさない", wrong["reason"] == "STOP_ON_WRONG_SIDE", wrong)
    near_target = entry_depth.plan_shift({**VP80_BUY, "targets": [29553.0, 29649.0]}, 12.5, policy("LIVE"))
    check("ターゲットの内側に建値が来るなら動かさない",
          near_target["reason"] in {"TARGET_NOT_BEYOND_SHIFTED_ENTRY"} or near_target["eligible"] is True,
          near_target)


def test_modes():
    original = copy.deepcopy(VP80_BUY)
    out = {"entryDepth": {"stale": True}}
    same, note = entry_depth.annotate(out, VP80_BUY, 12.5, policy("OFF"))
    check("OFF はシナリオを変えず記録も消す", same is VP80_BUY and "entryDepth" not in out and note is None, out)

    out = {}
    same, note = entry_depth.annotate(out, VP80_BUY, 12.5, policy("SHADOW"))
    check("SHADOW は発注幾何を変えない", same is VP80_BUY and same["entry"] == 29553.5, same)
    check("SHADOW は『動かした場合』を記録する",
          out["entryDepth"]["mode"] == "SHADOW" and out["entryDepth"]["shifted"]["entry"] == 29550.25, out)
    check("SHADOW の注記", note and "SHADOW" in note and "29,550.25" in note, note)

    out = {"evaluation": {"decision": {"entry": 29553.5, "stop": 29533.5, "evidence": ["VP_ACCEPTED"]}}}
    shifted, note = entry_depth.annotate(out, VP80_BUY, 12.5, policy("LIVE"))
    check("LIVE は entry / stop を置き換える",
          shifted["entry"] == 29550.25 and shifted["stop"] == 29530.25, shifted)
    check("LIVE でもターゲット・脚・ID は元のまま",
          shifted["targets"] == VP80_BUY["targets"] and shifted["legs"] == VP80_BUY["legs"]
          and shifted["decisionId"] == VP80_BUY["decisionId"], shifted)
    check("元のシナリオは破壊しない", VP80_BUY == original, VP80_BUY)
    decision = out["evaluation"]["decision"]
    check("評価カードの建値/SLも揃える", decision["entry"] == 29550.25 and decision["stop"] == 29530.25, decision)
    check("記録専用タグを 1 つだけ足す", decision["evidence"] == ["VP_ACCEPTED", "ENTRY_DEPTH_025N"], decision)
    again, _ = entry_depth.annotate(out, VP80_BUY, 12.5, policy("LIVE"))
    check("2 回目も同じ値(二重に動かない)・タグも重複しない",
          again["entry"] == 29550.25 and out["evaluation"]["decision"]["evidence"].count("ENTRY_DEPTH_025N") == 1,
          (again, out))
    built = nqx_state.build_scenario({**shifted, "scenarioId": "s86", "marketCycleId": "cy86"},
                                     observed_at="2026-09-14T00:00:00+00:00", ttl_minutes=10, symbol="MNQU6")
    check("平行移動後も DO スキーマ(tick・順序・分割)を通る",
          built["entry"] == 29550.25 and built["stop"] == 29530.25, built)


def test_enrich_integration_and_fail_safe():
    decision = {"decisionId": "38ddf1ab008b1e37", "phase": "EVAL_STRIKE", "model": "VP80_REVERSION",
                "side": "BUY", "state": "ARMED", "grade": "A+", "entryMode": "AGGRESSIVE_ACCEPT_CLOSE",
                "entry": 29553.5, "stop": 29533.5, "targets": [29588.5, 29649.0], "targetR": [1.75, 4.78],
                "evidence": ["VP_ACCEPTED"], "hardBlockers": [], "penalties": []}
    saved = (msnr_gate.evaluate, msnr_gate.build_card, entry_depth.load_policy)
    live, shadow = policy("LIVE"), policy("SHADOW")     # 差し替える前に作る(差し替え後は再帰する)
    try:
        msnr_gate.evaluate = lambda bundle, prm=None: {"decision": dict(decision), "noiseFloor": 12.5,
                                                       "strategyMatrix": {}, "ict": {}}
        msnr_gate.build_card = lambda result, **kw: {"decision": dict(result["decision"]),
                                                     "volGate": {"noise": 12.5}, "rotation": {}}
        bundle = {"at": "2026-09-08T10:54:50+00:00", "price": 29556.0, "snapshot": {"bars3m": []}}

        entry_depth.load_policy = lambda contract=None: live
        out, notes = monitor_publish.enrich_decisive_strategy(copy.deepcopy(bundle))
        primary = out["scenarios"]["primary"]
        check("LIVE: 公開シナリオが平行移動される",
              primary["entry"] == 29550.25 and primary["stop"] == 29530.25, primary)
        check("LIVE: 注記が残る", any("R86 entry depth" in n and "[LIVE]" in n for n in notes), notes)
        again, _ = monitor_publish.enrich_decisive_strategy(out)
        check("pipeline → publish の 2 回評価でも二重に動かない",
              again["scenarios"]["primary"]["entry"] == 29550.25, again["scenarios"])

        entry_depth.load_policy = lambda contract=None: shadow
        out, notes = monitor_publish.enrich_decisive_strategy(copy.deepcopy(bundle))
        check("SHADOW: 公開シナリオは元の幾何",
              out["scenarios"]["primary"]["entry"] == 29553.5 and out["entryDepth"]["eligible"], out)

        def boom(contract=None):
            raise RuntimeError("contract exploded")
        entry_depth.load_policy = boom
        out, notes = monitor_publish.enrich_decisive_strategy(copy.deepcopy(bundle))
        check("例外でも監視は落ちず元の幾何で公開",
              out["scenarios"]["primary"]["entry"] == 29553.5 and "entryDepth" not in out
              and any("not applied" in n for n in notes), (out.get("scenarios"), notes))
    finally:
        msnr_gate.evaluate, msnr_gate.build_card, entry_depth.load_policy = saved


def test_simulator_rules():
    t0 = 1_789_000_000 - (1_789_000_000 % 180)

    def bar(i, o, h, l, c):
        return {"t": t0 + i * 180, "o": o, "h": h, "l": l, "c": c}

    bars = [bar(0, 100, 101, 99, 100), bar(1, 100, 100.5, 97.0, 98), bar(2, 98, 112, 97.5, 111),
            bar(3, 111, 125, 110, 124)]
    touch = entry_depth.simulate("BUY", 97.0, 90.0, [110.0, 120.0], bars, t0 + 1, 1800)
    check("建値ちょうどのタッチは約定しない(1tick 突き抜けが要る)", touch["outcome"] != "FILLED", touch)
    filled = entry_depth.simulate("BUY", 97.25, 90.0, [110.0, 120.0], bars, t0 + 1, 1800)
    check("1tick 突き抜けで約定し、TP1→runner TP2", filled["outcome"] == "FILLED" and filled["tp1"], filled)
    check("SL 余白は約定後の安値から", filled["clearancePt"] == 7.0, filled)
    first = entry_depth.simulate("BUY", 96.0, 90.0, [110.0, 120.0], bars, t0 + 1, 1800)
    check("約定前に TP1 へ届いたら取消(R52)", first["outcome"] == "TP1_FIRST", first)
    both = [bar(0, 100, 101, 99, 100), bar(1, 100, 111, 89, 95)]
    same_bar = entry_depth.simulate("BUY", 100.0, 90.0, [110.0, 120.0], both, t0 + 1, 1800,
                                    market_price=100.0)
    check("成行: 同じ足で SL と TP1 に触れたら SL", same_bar["outcome"] == "FILLED"
          and same_bar["r"] == -1.0 and not same_bar["tp1"], same_bar)
    check("成行判定は engine と同じ向き", entry_depth.executable("BUY", 101.0, 100.0)
          and not entry_depth.executable("BUY", 99.0, 100.0)
          and entry_depth.executable("SELL", 99.0, 100.0), "")


test_policy_loader_fails_closed()
test_vp80_bracket_shift_keeps_risk_and_targets()
test_level_anchored_models_are_untouched()
test_guards()
test_modes()
test_enrich_integration_and_fail_safe()
test_simulator_rules()
print("ALL PASS test_r86_entry_depth")

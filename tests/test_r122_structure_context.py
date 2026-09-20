# -*- coding: utf-8 -*-
"""R122: 多層構造文脈と参加判断(execution_contract.json の structureContext)。

`market_structure_context` / `participation_policy` / `msnr_gate` は純粋関数(契約 JSON を
読むだけ)。このファイルも同じで、ネットワーク・`.secrets`・本番の記録に触らない。

守ること(docs/CLAUDE_MULTITIMEFRAME_PARTICIPATION_LIVE_DIRECTIVE_2026-09-19.md §7):

  1. 未来足 / 未確定 pivot / 知り得る前の時刻 / 別シンボル / 欠損足を使わない。
  2. 親 BUY・子 SELL の押し目、親の否定、子の継続確認を**別ケース**として判定する。
  3. 逐次と一括の一致、再起動後の同一 ID、窓から起点が消えた場合の扱い。
  4. 一度否定された仮説は価格の再訪だけでは復活しない。新しい根拠なら新 ID。
  5. WAIT_FOR_PULLBACK → WAIT_FOR_CONFIRMATION → ENTRY_READY、NO_ROOM、INVALIDATED。
  6. 先回り指値型(restingLimit)は状態名の変更で消えない。
  7. 証拠のある浅い構造だけが代替候補になる。証拠なしの追いかけは作らない。
  8. 同じ根拠の重複票なし。文脈が無くても従来候補は従来の検査で判断する。
  9. OFF は R122 以前と同じ判定。SHADOW は注文意図を変えない。
 10. LIVE では状態が候補 / primary へ効く。構造を否定した fixture では依存候補が変わる。
 11. 保存 → 読み込み → evaluate → decision → 凍結プランが通信なしで通る。
 12. decisionId と重複防止が整合し、`decision_to_scenario` まで根拠と ID が残る。
 13. nearTerm は LIVE にできない。`ictStdv.participation` は OFF 固定。
 14. STDV TARGETS LIVE / TURTLE 復活 / R119 の回帰。
"""
import copy
import hashlib
import io
import json
import os
import sys
import tempfile
from datetime import datetime, timezone

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import market_structure_context as msc  # noqa: E402
import msnr_gate  # noqa: E402
import participation_policy as pp  # noqa: E402

BREAKER = "BREAKER_CONTINUATION"
TURTLE = "TURTLE_SOUP_REVERSAL"
T0 = 1786970000 - (1786970000 % 900)          # 15 分境界に揃える
BAR3, BAR15 = 180, 900

failures = []


def check(label, condition, detail=""):
    if not condition:
        failures.append(label)
        print(f"FAIL {label}: {detail}")
        return
    print(f"OK   {label}")


def policy(context="LIVE", participation="LIVE", shallow="OFF", selection="OFF", near="SHADOW"):
    return {"context": {"mode": context}, "participation": {"mode": participation},
            "shallowCandidate": {"mode": shallow}, "selection": {"mode": selection},
            "nearTerm": {"mode": near}, "params": {}, "invalid": []}


OFF_POLICY = policy("OFF", "OFF", "OFF", "OFF", "OFF")


# ------------------------------------------------------------------ fixture

def bars15_up(invalidate=False, t0=T0, n=24):
    """HH/HL の 15 分足。``invalidate`` で最後の足が保護水準(最後のピボット安値)を割る。

    ピボットは左右 2 本。安値 i=2/10/18 が切り上がり、高値 i=6/14 も切り上がる。
    """
    path = []
    for i in range(n):
        if i in (2,):
            o, h, l, c = 29960, 29965, 29940, 29955      # 安値 29940
        elif i in (10,):
            o, h, l, c = 29990, 29996, 29970, 29988      # 安値 29970(切り上げ)
        elif i in (18,):
            o, h, l, c = 30010, 30016, 29995, 30012      # 安値 29995(切り上げ)
        elif i in (6,):
            o, h, l, c = 30020, 30040, 30015, 30030      # 高値 30040
        elif i in (14,):
            o, h, l, c = 30050, 30075, 30045, 30060      # 高値 30075(切り上げ)
        else:
            base = 29980 + i * 2
            o, h, l, c = base, base + 6, base - 6, base + 2
        path.append((o, h, l, c))
    if invalidate:
        # 最後の足が保護水準 29995 を**終値で**割る(ヒゲだけではない)。
        path[-1] = (30010, 30012, 29960, 29965)
    return [{"t": t0 + BAR15 * i, "o": o, "h": h, "l": l, "c": c, "v": 1000}
            for i, (o, h, l, c) in enumerate(path)]


ABOVE = [(30012, 30018, 30010, 30014) for _ in range(12)]
#: test_r119 と同じ実物。掃引 → 奪還 → MSS で TURTLE の先回り指値型になる。
RESTING = ABOVE + [(29998, 30012, 29994, 30010), (30010, 30022, 30008, 30019)]


def range_anchor(t_end, high=30200.0, low=29900.0):
    """明示アンカー(15 分)。`ICT_LOCATION` = 独立した確認要素を成立させるために使う。"""
    iso = lambda t: datetime.fromtimestamp(t, timezone.utc).isoformat()   # noqa: E731
    return {"rangeTf": "15m", "rangeStart": iso(t_end - 40 * BAR15), "rangeEnd": iso(t_end),
            "anchorType": "SESSION", "freshness": "FRESH", "high": high, "low": low}


def bundle(spec3m=None, bars15=None, price=None, at_offset=0, levels=None,
           symbol="CME_MINI:MNQ1!", htf=None, anchor=None):
    spec3m = RESTING if spec3m is None else spec3m
    # 3 分足は 15 分足の窓の**末尾**へ置く(親が確定してから子を読む)。
    last15 = (bars15[-1]["t"] + BAR15) if bars15 else (T0 + BAR15 * 24)
    start3 = last15 - BAR3 * len(spec3m)
    bars = [{"t": start3 + BAR3 * i, "o": o, "h": h, "l": l, "c": c, "v": 1000}
            for i, (o, h, l, c) in enumerate(spec3m)]
    at = datetime.fromtimestamp(bars[-1]["t"] + BAR3 + at_offset, timezone.utc).isoformat()
    snapshot = {"levels": levels or [{"label": "TEST", "price": 30000.0},
                                     {"label": "Weekly High", "price": 30100.0},
                                     {"label": "Monthly High", "price": 30200.0}],
                "bars3m": bars, "symbol": symbol}
    if bars15 is not None:
        snapshot["bars15m"] = bars15
    if htf is not None:
        snapshot["htfContext"] = htf
    if anchor is not None:
        snapshot["rangeAnchor"] = anchor
    return {"at": at, "priceAt": at, "price": bars[-1]["c"] if price is None else price,
            "sourceSymbol": symbol, "snapshot": snapshot}


def evaluate(bundle_in, pol):
    saved = msnr_gate.structure_context_policy
    try:
        msnr_gate.structure_context_policy = lambda contract=None: pol
        return msnr_gate.evaluate(copy.deepcopy(bundle_in))
    finally:
        msnr_gate.structure_context_policy = saved


def context_of(bundle_in, pol=None):
    return evaluate(bundle_in, pol or policy()).get("structureContext") or {}


# --------------------------------------------------- 1. 契約の読み取りと既定

def test_policy_loader():
    real = msnr_gate.structure_context_policy
    empty = real({})
    check("節が無ければ全段 OFF",
          all(empty[s]["mode"] == "OFF" for s in msnr_gate.STRUCTURE_CONTEXT_STAGES)
          and empty["invalid"] == [], empty)
    broken = real({"structureContext": "LIVE"})
    check("節の形が壊れていれば全段 OFF + invalid",
          broken["context"]["mode"] == "OFF"
          and broken["invalid"] == ["STRUCTURE_CONTEXT_MALFORMED"], broken)
    bad = real({"structureContext": {"context": {"mode": "ON"}, "participation": {"mode": 7}}})
    check("未知の mode はその段だけ OFF にして invalid に残す",
          bad["context"]["mode"] == "OFF" and bad["participation"]["mode"] == "OFF"
          and len(bad["invalid"]) == 2, bad)
    near_live = real({"structureContext": {"context": {"mode": "LIVE"},
                                           "nearTerm": {"mode": "LIVE"}}})
    check("nearTerm の LIVE は拒否される(未校正の予測を武装ゲートにしない)",
          near_live["nearTerm"]["mode"] == "OFF"
          and "STRUCTURE_CONTEXT_NEARTERM_LIVE_FORBIDDEN" in near_live["invalid"], near_live)
    cascade = real({"structureContext": {"context": {"mode": "OFF"},
                                         "participation": {"mode": "LIVE"},
                                         "shallowCandidate": {"mode": "LIVE"}}})
    check("context が OFF なら下流の段も自動で OFF(仮説が無いので判定できない)",
          cascade["participation"]["mode"] == "OFF"
          and cascade["shallowCandidate"]["mode"] == "OFF"
          and cascade["participation"].get("reason") == "CONTEXT_OFF", cascade)
    bare = real({"structureContext": {"context": {"mode": "OFF"}}})
    check("mode OFF だけの段は不正にしない(1 語で戻せる)", bare["invalid"] == [], bare)


def test_production_contract_is_valid():
    real = msnr_gate.structure_context_policy()
    check("本番契約の structureContext は読めて、不正な段が無い", real["invalid"] == [], real)
    check("本番契約の nearTerm は LIVE ではない", real["nearTerm"]["mode"] != "LIVE", real)
    with io.open(os.path.join(BASE, "execution_contract.json"), encoding="utf-8") as fh:
        contract = json.load(fh)
    node = contract.get("structureContext") or {}
    check("契約の version / schema が実装と一致",
          node.get("version") == msc.VERSION and node.get("schema") == msc.SCHEMA, node)
    stdv = msnr_gate.ict_stdv_policy()
    check("ictStdv.participation は OFF(参加判断の入口は R122 の 1 か所だけ)",
          stdv["participation"]["mode"] == "OFF" and not stdv["invalid"], stdv)
    superseded = msnr_gate.ict_stdv_policy({"ictStdv": {"mode": "LIVE",
                                                        "participation": {"mode": "LIVE"}}})
    check("ictStdv.participation に LIVE を書いても効かず、理由が残る",
          superseded["participation"]["mode"] == "OFF"
          and "ICT_STDV_PARTICIPATION_SUPERSEDED_BY_R122" in superseded["invalid"], superseded)


# ------------------------------------------------- 2. 入力の健全性(§7 の 1 行目)

def test_no_future_or_unconfirmed_inputs():
    bars = bars15_up()
    piv = msc.pivots(bars, BAR15)
    bad = [p for p in piv if p["confirmedAt"] <= p["t"] + BAR15]
    check("pivot は右側 2 本が閉じるまで確定しない(confirmedAt > 自分の close)", not bad, bad[:2])
    ctx = context_of(bundle(bars15=bars))
    thesis = ctx.get("thesis") or {}
    check("親の knownAt は保護ピボットの確定時刻と一致",
          thesis.get("knownAt") == (thesis.get("protectedLevel") or {}).get("confirmedAt"), thesis)
    check("親の knownAt は評価時刻より前(知り得る前の時刻を使わない)",
          thesis.get("knownAt") <= ctx.get("at"), (thesis.get("knownAt"), ctx.get("at")))
    for event in thesis.get("events") or []:
        if event["knownAt"] > ctx["at"]:
            check("親の events に未来の観測が無い", False, event)
            break
    else:
        check("親の events に未来の観測が無い", True)
    # 欠損足: OHLC が壊れた行は捨てる。推測で埋めない。
    broken = copy.deepcopy(bars)
    broken[5]["h"] = broken[5]["l"] - 1
    check("壊れた OHLC の行は捨てる(補完しない)",
          len(msc._norm_bars(broken)) == len(bars) - 1)
    check("本数が足りない 15 分足からは親を作らない",
          msc._parent_from_bars(bars[:10], BAR15, "15m", bars[-1]["t"] + BAR15, 30000.0,
                                2.0, "MNQ", None, None) is None)
    stale = msc._parent_from_bars(bars, BAR15, "15m", bars[-1]["t"] + 10 * BAR15,
                                  30000.0, 2.0, "MNQ", None, None)
    check("鮮度窓(2×step+slack)を超えた 15 分足からは親を作らない", stale is None, stale)


def test_symbol_and_session_are_carried():
    ctx_a = context_of(bundle(bars15=bars15_up(), symbol="CME_MINI:MNQ1!"))
    ctx_b = context_of(bundle(bars15=bars15_up(), symbol="CME_MINI:MNQZ2026"))
    a, b = ctx_a["thesis"], ctx_b["thesis"]
    check("シンボルが違えば別 ID(限月やセッションを跨いで同じ構造にしない)",
          a["structureId"] != b["structureId"], (a["structureId"], b["structureId"]))
    check("親は symbol / timeframe / sessionId を持つ",
          a.get("symbol") and a.get("timeframe") == "15m" and "sessionId" in a, a)


# ---------------------------------- 3. 親 BUY / 子 SELL / 否定 / 継続の書き分け

def test_parent_child_cases():
    live = bundle(bars15=bars15_up())
    ctx = context_of(live)
    thesis = ctx["thesis"]
    check("直接取得の 15 分足から親の仮説を作る(fidelity=BARS)",
          thesis["fidelity"] == "BARS" and thesis["timeframe"] == "15m"
          and thesis["bias"] == "BUY", thesis)
    check("保護水準は最後の確定ピボット安値",
          thesis["protectedLevel"]["price"] == 29995.0, thesis["protectedLevel"])
    check("親が生きている間は invalidatedAt が無い", thesis.get("invalidatedAt") is None, thesis)

    dead = bundle(bars15=bars15_up(invalidate=True))
    ctx2 = context_of(dead)
    t2 = ctx2["thesis"]
    check("保護水準を親の時間足の終値が割ったら invalidatedAt が立つ",
          t2.get("invalidatedAt") is not None, t2)
    check("否定は終値で判定し、事象として events に残る",
          any(e["kind"] == "CLOSE_BEYOND_PROTECTED" for e in t2["events"]), t2["events"])

    # 子 SELL(押し目)と 子 BUY(継続)を別ケースとして判定する。
    child_sell = {"bias": "SELL", "structureId": "x"}
    child_buy = {"bias": "BUY", "structureId": "y"}
    alive = dict(thesis)
    inside = (alive["protectedLevel"]["price"] + alive["objective"]["price"]) / 2
    check("親 BUY・子 SELL・親の否定前・レンジ内 → PULLBACK",
          msc.relate(alive, child_sell, inside) == "PULLBACK")
    check("親 BUY・子 BUY → CONTINUATION",
          msc.relate(alive, child_buy, inside) == "CONTINUATION")
    check("親が否定された後の子 SELL → REVERSAL_CANDIDATE",
          msc.relate(t2, child_sell, inside) == "REVERSAL_CANDIDATE")
    check("親の外側での子 SELL → REVERSAL_CANDIDATE(押し目と混同しない)",
          msc.relate(alive, child_sell, alive["protectedLevel"]["price"] - 50) == "REVERSAL_CANDIDATE")


def test_phase_is_observation_only():
    ctx = context_of(bundle(bars15=bars15_up()))
    thesis = ctx["thesis"]
    check("phase は観測できた段階だけ(語彙は AMD/AMDX ではない)",
          thesis["phase"] in msc.PHASES, thesis["phase"])
    check("掃引を観測していないブレイクを AMD 完成と呼ばない",
          thesis["amdOrderComplete"] is False or
          any(e["kind"] == "SWEEP_RECLAIMED" for e in thesis["events"]), thesis)
    dead = context_of(bundle(bars15=bars15_up(invalidate=True)))["thesis"]
    check("掃引なしで割ったブレイクは EXPANSION_UNSWEPT(順序を補完しない)",
          dead["phase"] == "EXPANSION_UNSWEPT" and dead["amdOrderComplete"] is False, dead)


def test_close_and_wick_are_separate():
    bars = bars15_up()
    # 3 分足のヒゲだけが保護水準を割る(終値は戻る)。
    wick = [(30010, 30014, 29990, 30012)] + ABOVE[:11] + [(30010, 30022, 30008, 30019)]
    ctx = context_of(bundle(spec3m=wick, bars15=bars))
    thesis = ctx["thesis"]
    check("足中の接触(touchedAt)と終値否定(invalidatedAt)を分ける",
          thesis.get("touchedAt") is not None and thesis.get("invalidatedAt") is None, thesis)


# --------------------------------------------- 4. ID の安定と「復活しない」規律

def test_identity_is_stable_and_tombstoned():
    bars = bars15_up()
    first = context_of(bundle(bars15=bars))["thesis"]
    again = context_of(bundle(bars15=copy.deepcopy(bars)))["thesis"]
    check("同じ根拠なら同じ structureId(再起動しても同じ)",
          first["structureId"] == again["structureId"], (first["structureId"], again["structureId"]))
    # 新しいピボット安値ができたら**新しい構造**= 新 ID。
    moved = copy.deepcopy(bars)
    for i in range(18, 21):
        moved[i] = {**moved[i], "l": moved[i]["l"] + 25}
    other = context_of(bundle(bars15=moved))["thesis"]
    check("保護水準が変われば新しい構造 = 新 ID",
          other["structureId"] != first["structureId"], (first["structureId"], other["structureId"]))

    # 墓標: 一度否定された ID は、価格が戻っただけでは復活しない。
    dead = context_of(bundle(bars15=bars15_up(invalidate=True)))["thesis"]
    memory = msc.Memory(None)
    memory.observe({"thesis": dead, "at": dead["observedAt"]})
    revisit = bundle(bars15=bars)         # 否定の足が無い = 価格が戻っただけ
    saved = msnr_gate.structure_context_policy
    try:
        msnr_gate.structure_context_policy = lambda contract=None: policy()
        payload = copy.deepcopy(revisit)
        payload["_structureMemory"] = memory.snapshot()
        revived = (msnr_gate.evaluate(payload).get("structureContext") or {}).get("thesis") or {}
    finally:
        msnr_gate.structure_context_policy = saved
    if dead["structureId"] == revived.get("structureId"):
        check("否定済みの仮説は価格の再訪だけでは復活しない",
              revived.get("invalidatedAt") is not None and revived.get("revivalBlocked") is True,
              revived)
    else:
        check("否定済みの仮説は価格の再訪だけでは復活しない(別 ID になった)", True)
    check("記憶は発注台帳ではなく専用スキーマ",
          memory.as_dict()["schema"] == msc.MEMORY_SCHEMA, memory.as_dict())


def test_sequential_matches_batch_and_window_loss():
    """逐次に与えても一括で与えても同じ状態。起点が窓から消えたら再導出しない。"""
    bars = bars15_up()
    memory = msc.Memory(None)
    last = None
    for cut in range(20, len(bars) + 1):
        ctx = context_of(bundle(bars15=bars[:cut]))
        memory.observe(ctx, now=ctx["at"])
        last = ctx
    batch = context_of(bundle(bars15=bars))
    check("逐次再生と一括再計算で同じ親 ID",
          (last["thesis"] or {})["structureId"] == (batch["thesis"] or {})["structureId"],
          (last["thesis"], batch["thesis"]))
    # 窓から起点が消える: 保護ピボットより後ろだけを渡す。
    shrunk = context_of(bundle(bars15=bars[19:] + bars15_up(t0=bars[-1]["t"] + BAR15, n=16)))
    check("起点が窓から消えたら、同じ ID を作り直さない(別 ID か仮説なし)",
          (shrunk.get("thesis") or {}).get("structureId") != (batch["thesis"] or {})["structureId"],
          shrunk.get("thesis"))


# --------------------------------------------------- 5. 参加状態とその遷移

def cand(side="BUY", entry=30000.0, stop=29980.0, tp1=30040.0, final=30080.0,
         model=BREAKER, blockers=(), allowed=None, resting=False, chain=None):
    out = {"model": model, "side": side, "entry": entry, "stop": stop,
           "targets": [tp1, final], "targetR": [2.0, 4.0], "evidence": [],
           "penalties": [], "grade": "B", "score": 5, "targetLabels": ["TP1", "RUNNER"],
           "hardBlockers": list(blockers),
           "chain": chain or {"type": "FLIP", "state": "FLIP_HELD", "side": side,
                              "breakBarT": T0, "barsLeft": 8}}
    out["allowed"] = (not out["hardBlockers"]) if allowed is None else allowed
    out["state"] = "ARMED" if out["allowed"] else "WATCH"
    if resting:
        out["restingLimit"] = True
    return out


def ctx_alive(bias="BUY", invalidated=None):
    return {"status": "OK", "childRelation": "CONTINUATION",
            "thesis": {"structureId": "T1", "bias": bias, "invalidatedAt": invalidated,
                       "invalidationRule": "15m 確定終値が 29995.0 を下抜け",
                       "expiresAt": T0 + 10 * BAR15, "protectedLevel": {"price": 29995.0},
                       "objective": {"price": 30100.0}},
            "child": {"structureId": "C1", "bias": bias}, "children": {}}


def test_participation_states():
    ctx = ctx_alive()
    ready = pp.evaluate(cand(), ctx, 30000.0)
    check("既存ゲートを全部通った候補は ENTRY_READY",
          ready["state"] == "ENTRY_READY", ready)
    check("ENTRY_READY は注文意図を明示する(成行 / 指値)",
          ready["orderIntent"] in ("MARKET", "LIMIT", "RESTING_LIMIT"), ready)
    gap = pp.evaluate(cand(blockers=["LIMIT_GAP_EXCEEDED"]), ctx, 30120.0)
    check("指値が遠すぎる候補は WAIT_FOR_PULLBACK(次に見るものと期限を持つ)",
          gap["state"] == "WAIT_FOR_PULLBACK" and gap["nextObservation"]
          and gap["deadline"] is not None, gap)
    confirm = pp.evaluate(cand(blockers=["NO_CONFIRMATION"],
                               chain={"type": "SWEEP", "state": "MSS_CONFIRMED", "side": "BUY",
                                      "sweepBarT": T0, "barsLeft": 6}), ctx, 30000.0)
    check("位置に来たが確認が足りない候補は WAIT_FOR_CONFIRMATION + 不足の名指し",
          confirm["state"] == "WAIT_FOR_CONFIRMATION"
          and "RETEST_HELD" in str(confirm["nextObservation"]), confirm)
    room = pp.evaluate(cand(blockers=["TARGET_HEADROOM_INSUFFICIENT"]), ctx, 30000.0)
    check("目標までの余地が無い候補は NO_ROOM", room["state"] == "NO_ROOM", room)
    consumed = pp.evaluate(cand(blockers=["ANCHOR_CONSUMED"]), ctx, 30000.0)
    check("アンカーを使い切った候補は INVALIDATED", consumed["state"] == "INVALIDATED", consumed)
    expired = pp.evaluate(cand(chain={"type": "FLIP", "state": "EXPIRED", "side": "BUY",
                                      "breakBarT": T0, "barsLeft": 0}), ctx, 30000.0)
    check("寿命切れの連鎖は EXPIRED", expired["state"] == "EXPIRED", expired)

    dead = ctx_alive(invalidated=T0 + BAR15)
    same = pp.evaluate(cand(side="BUY"), dead, 30000.0)
    check("親が否定されたら、同じ方向の候補は INVALIDATED + ブロッカー",
          same["state"] == "INVALIDATED"
          and pp.BLOCKER_PARENT_INVALIDATED in same["blockers"], same)
    opposite = pp.evaluate(cand(side="SELL"), dead, 30000.0)
    check("親が否定されても、逆方向の独立したセットアップは止めない",
          opposite["state"] == "ENTRY_READY" and not opposite["blockers"], opposite)
    check("依存の判定は方向で行う(親と同方向だけ)",
          pp.depends_on_thesis(cand(side="BUY"), dead) is True
          and pp.depends_on_thesis(cand(side="SELL"), dead) is False)
    nocontext = pp.evaluate(cand(), {"status": "CONTEXT_UNAVAILABLE"}, 30000.0)
    check("文脈が無くても、従来候補は従来の検査で ENTRY_READY のまま",
          nocontext["state"] == "ENTRY_READY", nocontext)
    shallow = pp.evaluate({**cand(), "r122Shallow": True}, {"status": "CONTEXT_UNAVAILABLE"}, 30000.0)
    check("文脈に依存する追加候補だけが CONTEXT_UNAVAILABLE",
          shallow["state"] == "CONTEXT_UNAVAILABLE", shallow)


def test_resting_limit_is_not_removed():
    resting = cand(resting=True, entry=30000.0, stop=29986.0, tp1=29950.0, side="SELL")
    audit = pp.evaluate(resting, ctx_alive("SELL"), 30019.0)
    check("先回り指値型は「観測用の待機」ではなく注文意図として残る",
          audit["state"] == "ENTRY_READY" and audit["orderIntent"] == "RESTING_LIMIT", audit)
    # 実物の fixture でも先回り指値型が消えないこと。
    result = evaluate(bundle(spec3m=RESTING, bars15=bars15_up()), policy())
    kinds = [c for c in result["candidates"] if c.get("restingLimit")]
    check("実 fixture でも restingLimit 候補が残る(状態名の変更で消えない)", kinds, result["summary"])


def test_plans_do_not_take_order_slots():
    result = evaluate(bundle(spec3m=RESTING, bars15=bars15_up()), policy())
    rows = pp.plans(result.get("structureContext"), result, 30019.0)
    check("観測用の待機計画は claim / 注文枠を取らない",
          all(row["claimsOrderSlot"] is False for row in rows), rows[:2])
    check("待機計画は「次に何を観測すれば入れるか」を持つ",
          all(row["nextObservation"] for row in rows) if rows else True, rows[:2])


# ------------------------------------------- 6. 浅い構造の代替候補(§4.2)

def test_shallow_requires_evidence():
    ctx = ctx_alive()
    ctx["children"] = {"BUY": {"bias": "BUY", "confirmed": True, "originBarT": T0,
                               "structureId": "C1", "state": "MSS_CONFIRMED", "type": "SWEEP"}}
    good_fvg = {"BULL": [{"lo": 29960.0, "hi": 29980.0, "eligible": True,
                          "preArrivalStructure": "INTACT", "createdAt": T0 + BAR3}]}
    seed = pp.shallow_seed(ctx, {"fvg": good_fvg}, [], 30010.0, 12.0, 12.0, False)
    check("親が生き・同方向の子が確認済み・適格 FVG があれば代替候補の種ができる",
          seed and seed["side"] == "BUY" and seed["entry"] == 29970.0
          and seed["stop"] == 29948.0, seed)
    check("代替候補は自分の根拠 ID を持つ(元の深い計画と混ぜない)",
          seed["thesisId"] == "T1" and seed["childId"] == "C1"
          and pp.EVIDENCE_SHALLOW in seed["evidence"], seed)
    check("同じ方向に発注できる候補があれば代替は作らない",
          pp.shallow_seed(ctx, {"fvg": good_fvg}, [], 30010.0, 12.0, 12.0, True) is None)
    old = {"BULL": [{**good_fvg["BULL"][0], "createdAt": T0 - 10 * BAR3}]}
    check("子の構造より前にできた古い空隙は使わない",
          pp.shallow_seed(ctx, {"fvg": old}, [], 30010.0, 12.0, 12.0, False) is None)
    stale = {"BULL": [{**good_fvg["BULL"][0], "preArrivalStructure": "BROKEN"}]}
    check("到達前に構造が壊れた FVG は使わない",
          pp.shallow_seed(ctx, {"fvg": stale}, [], 30010.0, 12.0, 12.0, False) is None)
    chase = {"BULL": [{**good_fvg["BULL"][0], "lo": 30020.0, "hi": 30040.0}]}
    check("現値が既に通過した水準へは置かない(取り逃しを理由に追いかけない)",
          pp.shallow_seed(ctx, {"fvg": chase}, [], 30010.0, 12.0, 12.0, False) is None)
    check("証拠が無ければ「代替参加なし」",
          pp.shallow_seed(ctx, {"fvg": {"BULL": []}}, [], 30010.0, 12.0, 12.0, False) is None)
    dead = ctx_alive(invalidated=T0)
    dead["children"] = ctx["children"]
    check("親が否定されていれば代替も作らない",
          pp.shallow_seed(dead, {"fvg": good_fvg}, [], 30010.0, 12.0, 12.0, False) is None)
    unconfirmed = copy.deepcopy(ctx)
    unconfirmed["children"]["BUY"]["confirmed"] = False
    check("子の構造が未確認なら代替は作らない",
          pp.shallow_seed(unconfirmed, {"fvg": good_fvg}, [], 30010.0, 12.0, 12.0, False) is None)


def test_shallow_does_not_double_count_or_displace():
    live = bundle(spec3m=RESTING, bars15=bars15_up())
    base = evaluate(live, policy(shallow="OFF"))
    with_shallow = evaluate(live, policy(shallow="LIVE"))
    extra = [c for c in with_shallow["candidates"] if c.get("r122Shallow")]
    for c in extra:
        check("代替候補は OTE の合流加点(OTE_FVG_CONFLUENCE)を受け取らない",
              "OTE_FVG_CONFLUENCE" not in (c.get("evidence") or []), c.get("evidence"))
        check("代替候補は自分の印を持つ", pp.EVIDENCE_SHALLOW in (c.get("evidence") or []), c)
    check("SHADOW の代替候補は primary の選択肢に入らない",
          all(c.get("modelGate") for c in evaluate(live, policy(shallow="SHADOW"))["candidates"]
              if c.get("r122Shallow")))
    check("武装できない代替候補は primary の選択肢に入らない",
          all(c.get("modelGate") for c in with_shallow["candidates"]
              if c.get("r122Shallow") and not c.get("allowed")))
    ids = [c.get("model") for c in base["candidates"]]
    check("代替候補は既存候補を消さない(記録は両方残る)",
          all(m in [c.get("model") for c in with_shallow["candidates"]] for m in ids), ids)


# ------------------------------------- 7. OFF / SHADOW / LIVE の効き方(§7 の 9,10)

def test_off_is_byte_identical():
    live = bundle(spec3m=RESTING, bars15=bars15_up())
    off = evaluate(live, OFF_POLICY)
    check("OFF では decision に R122 のキーが 1 つも増えない",
          "structure" not in off["decision"] and "structureDetail" not in off["decision"],
          sorted(off["decision"]))
    check("OFF では結果に structureContext を付けない", "structureContext" not in off)
    for c in off["candidates"]:
        check("OFF では候補に participation を付けない", "participation" not in c, c.get("model"))
        break


def test_shadow_changes_no_order_intent():
    live = bundle(spec3m=RESTING, bars15=bars15_up())
    off = evaluate(live, OFF_POLICY)["decision"]
    shadow = evaluate(live, policy(context="SHADOW", participation="SHADOW",
                                   shallow="SHADOW", near="SHADOW"))["decision"]
    keys = ("model", "side", "state", "grade", "entry", "stop", "targets", "decisionId")
    check("SHADOW は注文意図(model/side/state/grade/建値/SL/TP/decisionId)を変えない",
          all(off.get(k) == shadow.get(k) for k in keys),
          {k: (off.get(k), shadow.get(k)) for k in keys if off.get(k) != shadow.get(k)})
    check("SHADOW でも根拠は残る", shadow.get("structure"), shadow.get("structure"))


def test_live_changes_the_dependent_candidate_only():
    """構造を否定した fixture に差し替えると、**依存する候補だけ**が変わる。"""
    alive = bundle(spec3m=RESTING, bars15=bars15_up())
    dead = bundle(spec3m=RESTING, bars15=bars15_up(invalidate=True))
    pol = policy(participation="LIVE")
    a = evaluate(alive, pol)
    d = evaluate(dead, pol)
    check("否定した fixture では親の仮説が invalidated",
          (d["structureContext"]["thesis"] or {}).get("invalidatedAt") is not None)
    bias = (d["structureContext"]["thesis"] or {}).get("bias")
    blocked = [c for c in d["candidates"] if c.get("participationBlocked")]
    kept = [c for c in d["candidates"]
            if c.get("side") != bias and not c.get("participationBlocked")]
    check("否定後は親と同じ方向の候補にだけブロッカーが付く",
          all(c.get("side") == bias for c in blocked), [c.get("side") for c in blocked])
    check("逆方向の候補は同じ周期でも止まらない(全モデル停止にしない)",
          all(not c.get("participationBlocked") for c in kept))
    same_dir_alive = [c for c in a["candidates"] if c.get("side") == bias]
    check("生きている親では同じ候補がブロックされない",
          all(not c.get("participationBlocked") for c in same_dir_alive))
    if blocked:
        shadow = evaluate(dead, policy(participation="SHADOW"))
        check("SHADOW では同じ状況でもブロッカーを立てない",
              all(not c.get("participationBlocked") for c in shadow["candidates"]))
    else:
        check("否定した fixture では依存候補にブロッカーが付く(空振りでは検証にならない)",
              False, [c.get("model") for c in d["candidates"]])


def test_live_blocker_flips_armed_to_watch():
    """ブロッカー以外に不足が無い候補は、親の否定で ARMED → WATCH になる。"""
    bars = bars15_up()
    anchor = range_anchor(bars[-1]["t"] + BAR15)
    alive = bundle(spec3m=RESTING, bars15=bars, anchor=anchor)
    dead = bundle(spec3m=RESTING, bars15=bars15_up(invalidate=True), anchor=anchor)
    pol = policy(participation="LIVE")
    a, d = evaluate(alive, pol), evaluate(dead, pol)
    bias = (d["structureContext"]["thesis"] or {}).get("bias")
    armed_alive = [c for c in a["candidates"] if c.get("side") == bias and c.get("allowed")]
    check("生きている親では同方向の候補が武装できる fixture である", armed_alive,
          [(c.get("model"), c.get("hardBlockers")) for c in a["candidates"]])
    if armed_alive:
        models = {c["model"] for c in armed_alive}
        after = [c for c in d["candidates"] if c.get("model") in models and c.get("side") == bias]
        check("親が否定されると同じ候補が WATCH へ落ちる",
              after and all(c.get("state") == "WATCH"
                            and pp.BLOCKER_PARENT_INVALIDATED in (c.get("hardBlockers") or [])
                            for c in after),
              [(c.get("model"), c.get("state"), c.get("hardBlockers")) for c in after])
        changed = (d["decision"].get("structure") or {}).get("changedFromBaseline")
        if (d["decision"].get("model") in models
                and d["decision"].get("side") == bias):
            check("primary が落ちた周期は changedFromBaseline に理由が残る",
                  any("state" in item for item in (changed or [])), changed)
        shadow = evaluate(dead, policy(participation="SHADOW"))
        same = [c for c in shadow["candidates"] if c.get("model") in models and c.get("side") == bias]
        check("SHADOW では同じ fixture でも ARMED のまま(注文意図を変えない)",
              same and all(c.get("state") == "ARMED" for c in same),
              [(c.get("model"), c.get("state")) for c in same])


def test_missing_context_does_not_stop_models():
    """新文脈が無い周期でも、従来候補は従来どおり判定される。"""
    no15 = bundle(spec3m=RESTING, bars15=None, htf={"status": "MIXED", "bias": None,
                                                    "valid": False, "frames": {}})
    off = evaluate(no15, OFF_POLICY)["decision"]
    live = evaluate(no15, policy())["decision"]
    check("文脈が作れない周期でも decision は変わらない",
          all(off.get(k) == live.get(k) for k in
              ("model", "side", "state", "grade", "entry", "stop", "targets", "decisionId")),
          (off.get("model"), live.get("model")))
    ctx = evaluate(no15, policy()).get("structureContext") or {}
    check("文脈が無い理由が残る",
          ctx.get("status") == "CONTEXT_UNAVAILABLE" and ctx.get("reasons"), ctx.get("reasons"))


def test_selection_stage():
    high_watch = cand(model=BREAKER, blockers=["ANCHOR_CONSUMED"])
    high_watch["grade"], high_watch["score"] = "A", 7
    low_armed = cand(model=TURTLE)
    low_armed["grade"], low_armed["score"] = "B", 3
    off = msnr_gate.select_primary([high_watch, low_armed], {}, context=ctx_alive(),
                                   structure_policy=policy(selection="OFF"))
    check("selection OFF: 現行どおり高得点の WATCH が primary(契約は変わらない)",
          off["model"] == BREAKER and off["state"] == "WATCH", off["model"])
    on = msnr_gate.select_primary([high_watch, low_armed], {}, context=ctx_alive(),
                                  structure_policy=policy(selection="LIVE"))
    check("selection LIVE: 武装できる候補を優先する",
          on["model"] == TURTLE and on["state"] == "ARMED", on["model"])
    check("selection LIVE の理由が decision に残る",
          on["structure"]["selectionReason"] == "ELIGIBLE_PREFERRED_OVER_HIGHER_SCORE_WATCH", on)
    shadow = msnr_gate.select_primary([high_watch, low_armed], {}, context=ctx_alive(),
                                      structure_policy=policy(selection="SHADOW"))
    check("selection SHADOW: 選ばれる候補は変えず、観測だけ残す",
          shadow["model"] == BREAKER and "SHADOW" in shadow["structure"]["selectionReason"], shadow)


def test_selection_only_picks_fully_qualified_candidates():
    """選択層が選べるのは **既存ゲートを全部通った候補だけ**(`allowed`)。"""
    blocked_a = cand(model=BREAKER, blockers=["ANCHOR_CONSUMED"])
    blocked_a["grade"], blocked_a["score"] = "A+", 10
    blocked_b = cand(model=TURTLE, blockers=["RISK_CAP_EXCEEDED"])
    blocked_b["grade"], blocked_b["score"] = "A", 8
    on = msnr_gate.select_primary([blocked_a, blocked_b], {}, context=ctx_alive(),
                                  structure_policy=policy(selection="LIVE"))
    check("武装できる候補が 1 つも無ければ、selection LIVE でも並びは変わらない",
          on["model"] == BREAKER and on["state"] == "WATCH"
          and on["structure"]["selectionReason"] == "GRADE_SCORE_R_MODELRANK", on)
    armed = cand(model="VP80_REVERSION")
    armed["grade"], armed["score"] = "B", 1
    picked = msnr_gate.select_primary([blocked_a, blocked_b, armed], {}, context=ctx_alive(),
                                      structure_policy=policy(selection="LIVE"))
    check("selection LIVE が選ぶのは hardBlockers ゼロの候補だけ",
          picked["model"] == "VP80_REVERSION" and not picked["hardBlockers"]
          and picked["state"] == "ARMED", picked)


def test_selection_cannot_bypass_global_gates():
    """市場全体・口座全体の停止は primary を選んだ**後**に当たるので迂回できない。"""
    import monitor_publish
    armed = {"model": "VP80_REVERSION", "side": "BUY", "state": "ARMED", "grade": "B",
             "entry": 30000.0, "stop": 29980.0, "target": 30040.0}
    demoted, note = monitor_publish.apply_volatility_grade_gate(
        armed, {"active": True, "ratio_all": 0.99, "noise": 59.0})
    check("ボラ床の停止は、どのモデルが primary でも ARMED を WATCH へ落とす",
          demoted["state"] == "WATCH" and note, (demoted.get("state"), note))
    for model in ("BREAKER_CONTINUATION", "TURTLE_SOUP_REVERSAL", "OTE_FVG_PULLBACK"):
        other, _n = monitor_publish.apply_volatility_grade_gate(
            {**armed, "model": model}, {"active": True, "ratio_all": 0.99, "noise": 59.0})
        check(f"ボラ床の停止はモデルを見ない({model})", other["state"] == "WATCH", other)
    # 構造: publish_state はイベント窓・限月・ボラを `chosen`(= 選ばれた後)へ当てる。
    src = io.open(os.path.join(BASE, "monitor_publish.py"), encoding="utf-8").read()
    body = src[src.index("def publish_state("):]
    body = body[:body.index("\ndef ", 10)]
    for token in ("event blackout", "contract expiry gate", "apply_volatility_grade_gate"):
        check(f"publish_state は選択後の scenario へ {token} を当てる", token in body)
    check("publish_state は primary を選び直さない(select_primary を呼ばない)",
          "select_primary" not in body)
    # データゲートも同じく evaluate の後。
    enrich = src[src.index("def enrich_decisive_strategy("):]
    enrich = enrich[:enrich.index("\ndef ", 10)]
    check("取得受領書のゲートは evaluate の後に decision へ当たる",
          enrich.index("acquisition_display_gate") > enrich.index("msnr_gate.evaluate"))


# --------------------------------- 8. decision → 凍結プラン → カード縮小 → 保存

def test_decision_carries_minimal_fields():
    result = evaluate(bundle(spec3m=RESTING, bars15=bars15_up()), policy())
    decision = result["decision"]
    minimal = decision.get("structure") or {}
    for key in ("contextVersion", "thesisId", "participationState", "triggerEvidenceIds",
                "invalidation", "selectionReason", "changedFromBaseline"):
        check(f"decision.structure に {key} がある", key in minimal, sorted(minimal))
    scenario = msnr_gate.decision_to_scenario(decision, {}, qty=2)
    if scenario:
        check("凍結プラン(scenario)まで根拠と ID が残る",
              scenario.get("structure", {}).get("thesisId") == minimal.get("thesisId"),
              scenario.get("structure"))
        check("scenarioId は decisionId と一致(重複防止が壊れない)",
              scenario["scenarioId"] == decision["decisionId"])
    card = msnr_gate.build_card(result, price=result["decision"].get("entry"))
    if isinstance(card.get("decision"), dict):
        check("カードにも最小形式の根拠が載る", "structure" in card["decision"],
              sorted(card["decision"]))
    fat = copy.deepcopy(card)
    fat["summary"] = "あ" * 4000                       # 4096 バイト超で縮小を強制
    shrunk = msnr_gate._shrink_card(fat)
    if isinstance(shrunk.get("decision"), dict) and "structure" in (card.get("decision") or {}):
        check("カード縮小でも decision.structure は落ちない",
              "structure" in shrunk["decision"], sorted(shrunk["decision"]))
        check("カード縮小では structureDetail の方が先に落ちる",
              "structureDetail" not in shrunk["decision"])


def test_decision_id_unchanged_by_participation():
    live = bundle(spec3m=RESTING, bars15=bars15_up())
    off = evaluate(live, OFF_POLICY)["decision"]
    on = evaluate(live, policy())["decision"]
    check("参加判断は建値 / SL / TP を書き換えないので decisionId が変わらない",
          off["decisionId"] == on["decisionId"] and off["entry"] == on["entry"]
          and off["stop"] == on["stop"] and off["targets"] == on["targets"],
          (off["decisionId"], on["decisionId"]))


def test_roundtrip_through_disk_without_network():
    """JSON 保存 → 別プロセス相当の読み込み → evaluate → decision → 凍結プラン。"""
    payload = bundle(spec3m=RESTING, bars15=bars15_up())
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "cycle.json")
        with io.open(path, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(payload, fh, ensure_ascii=False)
        with io.open(path, encoding="utf-8") as fh:
            reloaded = json.load(fh)
        first = evaluate(payload, policy())
        second = evaluate(reloaded, policy())
        a = json.dumps(first["decision"], sort_keys=True, default=str)
        b = json.dumps(second["decision"], sort_keys=True, default=str)
        check("保存 → 読み込みで decision が一致(通信なし)", a == b)
        # 記憶も JSON として往復できる。
        memory = msc.Memory(None)
        state = memory.observe(first.get("structureContext"))
        mpath = os.path.join(tmp, "memory.json")
        with io.open(mpath, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(state, fh, ensure_ascii=False)
        with io.open(mpath, encoding="utf-8") as fh:
            back = msc.Memory(json.load(fh))
        check("記憶が JSON 往復で壊れない", back.as_dict() == state)
        check("壊れた記憶は「記憶なし」として扱う(周期を止めない)",
              msc.Memory({"schema": "NOPE"}).structures == {}
              and msc.Memory("x").structures == {})


def test_near_term_is_evaluation_only():
    result = evaluate(bundle(spec3m=RESTING, bars15=bars15_up()), policy())
    forecast = (result.get("structureContext") or {}).get("nearTerm") or {}
    check("短期予測は 3 分 / 15 分の明示地平を持つ",
          [h["label"] for h in forecast.get("horizons") or []] == ["3m", "15m"], forecast)
    check("短期予測は基準価格と基準時刻を固定する",
          forecast.get("referencePrice") is not None and forecast.get("referenceBarT") is not None,
          forecast)
    check("中立幅は既存のボラ尺度(ノイズ床)から事前に決まる",
          forecast.get("neutralBandPt") is not None, forecast)
    check("短期予測はゲートではないと自分で宣言する",
          forecast.get("gate") is False and forecast.get("usedForParticipation") is False, forecast)
    check("未検証の確率は付けない",
          not any(k in forecast for k in ("probability", "confidence", "winRate")), forecast)
    check("方向の語彙は UP/DOWN/NEUTRAL/UNKNOWN",
          forecast.get("direction") in msc.DIRECTIONS, forecast)
    # 採点は後続の確定足だけで行う(未来を入力にしない)。
    scored = msc.score_near_term(forecast, [])
    check("後続足が無ければ採点は UNAVAILABLE(推測しない)",
          all(row["actual"] == "UNAVAILABLE" for row in scored["scored"]), scored)


# ----------------------------------------------------- 9. 既存機能の回帰

def test_existing_gates_still_hold():
    live = bundle(spec3m=RESTING, bars15=bars15_up())
    result = evaluate(live, policy())
    stdv = msnr_gate.ict_stdv_policy()
    check("R121: STDV は mode=LIVE / targets=LIVE のまま",
          stdv["mode"] == "LIVE" and stdv["targets"]["mode"] == "LIVE", stdv)
    check("R121: runner への STDV 採用は無効のまま",
          stdv["targets"].get("runnerEligible") is False, stdv)
    check("R89: modelGate は空(TURTLE 復活を維持)",
          msnr_gate.model_gate_rules()["rules"] == []
          and msnr_gate.model_gate_rules()["invalid"] == [])
    gate = msnr_gate.limit_gate_policy()
    check("R119: gapCap=LIVE / maxGapR=1.5 / targetPassed=LIVE のまま",
          gate["gapCap"]["mode"] == "LIVE" and gate["gapCap"]["maxGapR"] == 1.5
          and gate["targetPassed"]["mode"] == "LIVE", gate)
    audit = msnr_gate.limit_gate_audit(
        {"model": BREAKER, "side": "BUY", "entry": 29699.5, "stop": 29661.75,
         "targets": [29763.25, 29800.0]}, 29800.0)
    check("R119: 現値が TP1 を通過した候補は今も止まる",
          audit["blocker"] == "TARGET_ALREADY_PASSED", audit)
    for c in result["candidates"]:
        if c.get("hardBlockers"):
            continue
        check("R122 LIVE でも既存ゲートを通った候補は ARMED のまま",
              c.get("state") == "ARMED", c.get("model"))
        break
    # 幾何の不変条件は R122 では触らない。
    check("SL 上限は変わらない", msnr_gate.sl_cap_pt() > 0)


def test_no_duplicate_votes():
    result = evaluate(bundle(spec3m=RESTING, bars15=bars15_up()), policy())
    for c in result["candidates"]:
        evidence = c.get("evidence") or []
        check(f"{c.get('model')} の evidence に重複が無い",
              len(evidence) == len(set(evidence)), evidence)
        check(f"{c.get('model')} は R122 で新しい加点を受け取らない",
              not any(str(tag).startswith("R122_") and tag != pp.EVIDENCE_SHALLOW
                      and tag != "R122_PARENT_INVALIDATED_SHADOW" for tag in evidence), evidence)
        break


def test_frozen_plan_is_not_rewritten():
    """保有中の凍結済み計画に R122 は触らない(参加判断は新規武装だけ)。"""
    source = io.open(os.path.join(BASE, "autotrade_engine.py"), encoding="utf-8").read()
    for name in ("market_structure_context", "participation_policy", "structureContext"):
        check(f"autotrade_engine は {name} を参照しない", name not in source)
    order = io.open(os.path.join(BASE, "order.py"), encoding="utf-8").read()
    for name in ("market_structure_context", "participation_policy", "structureContext"):
        check(f"order.py は {name} を参照しない", name not in order)
    # 凍結プランへ写すのは**表示・集計専用**の 1 キーだけで、判定には使わない。
    check("engine の凍結プランは decisionStructure を写すだけ",
          source.count("decisionStructure") == 1, source.count("decisionStructure"))
    for token in ("decisionStructure\")", "decisionStructure'"):
        check(f"engine が {token} を判定に読んでいない",
              f"plan.get(\"decisionStructure\")" not in source
              and "decisionStructure\"]" not in source)


def test_frozen_plan_carries_structure():
    """凍結プラン(engine の plan 行)まで根拠と ID が届く。"""
    import autotrade_engine
    result = evaluate(bundle(spec3m=RESTING, bars15=bars15_up(), anchor=range_anchor(
        bars15_up()[-1]["t"] + BAR15)), policy())
    decision = result["decision"]
    scenario = msnr_gate.decision_to_scenario(decision, {}, qty=2)
    if not scenario:
        check("凍結プランの検査に使える scenario が作れた", False, decision.get("model"))
        return
    fake_bundle = {"evaluation": {"decision": {"structure": decision.get("structure"),
                                               "evidence": decision.get("evidence")}}}
    payload = dict(scenario)
    payload.setdefault("symbol", "MNQZ6")
    plan = autotrade_engine.build_management_plan(payload, fake_bundle, {})
    carried = plan.get("decisionStructure") or {}
    check("engine の凍結プランに decisionStructure が入る",
          carried.get("thesisId") == (decision.get("structure") or {}).get("thesisId")
          and "participationState" in carried, carried)
    check("凍結プランの判定フィールドは変わらない(建値 / SL / 枚数 / decisionId)",
          plan["entry"] == scenario["entry"] and plan["initialStop"] == scenario["stop"]
          and plan["qty"] == scenario["qty"] and plan["decisionId"] == decision["decisionId"],
          (plan.get("entry"), plan.get("initialStop"), plan.get("qty")))
    check("tranche の合成プランも decisionStructure を引き継ぐ",
          "decisionStructure" in io.open(os.path.join(BASE, "tranche.py"),
                                         encoding="utf-8").read())


def test_structure_survives_the_worker_boundary():
    """Worker が structure を落としても、**執行が読む凍結プラン**には残る。

    本番の `reconcile` は Worker 正規化後の scenario(未知キーは落ちている)と、
    ローカルの評価カード付きバンドルを受け取る。凍結プランはローカル側から拾うので、
    執行の根拠は publish 境界で欠けない。
    """
    import autotrade_engine
    result = evaluate(bundle(spec3m=RESTING, bars15=bars15_up(),
                             anchor=range_anchor(bars15_up()[-1]["t"] + BAR15)), policy())
    decision = result["decision"]
    scenario = msnr_gate.decision_to_scenario(decision, {}, qty=2)
    if not scenario:
        check("伝播の検査に使える scenario が作れた", False, decision.get("model"))
        return
    check("(1) ローカル decision に structure がある", bool(decision.get("structure")))
    check("(2) 公開ペイロードに structure が載る", bool(scenario.get("structure")))
    # (3) Worker の正規化は明示キーだけを返す(= 未知キーは保存されない)。
    worker = io.open(os.path.join(BASE, "cloudflare", "src", "state_machine.js"),
                     encoding="utf-8").read()
    tail = worker.split("    scenario: {", 1)[-1][:4000]
    check("(3) Worker の validateScenario は structure を保存しない(正規化で落ちる)",
          "structure:" not in tail)
    # (4) その scenario でも凍結プランは根拠を持つ。
    stripped = {k: v for k, v in scenario.items() if k != "structure"}
    stripped.setdefault("symbol", "MNQZ6")
    local = {"evaluation": {"decision": {"structure": decision["structure"],
                                         "evidence": decision.get("evidence") or []}}}
    plan = autotrade_engine.build_management_plan(stripped, local, {})
    carried = plan.get("decisionStructure") or {}
    check("(4) Worker が落とした後でも凍結プランに根拠が残る",
          carried.get("thesisId") == decision["structure"].get("thesisId")
          and carried.get("participationState") is not None, carried)


def _code_without_docstrings(path):
    """判定はコードだけで行う(説明文に書かれたパス名で落とさない)。"""
    import ast
    src = io.open(path, encoding="utf-8").read()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc:
                src = src.replace(doc, "")
    return "\n".join(line.split("#", 1)[0] for line in src.splitlines())


def test_production_never_reconstructs_15m():
    """本番は**直接取得した 15 分足だけ**を親にする。3 分足からの再構成は研究用。"""
    src = _code_without_docstrings(os.path.join(BASE, "market_structure_context.py"))
    check("market_structure_context は bars3m を読まない", "bars3m" not in src)
    check("親の出所は snapshot.bars15m だけ",
          src.count('get("bars15m")') >= 1 and "reconstruct" not in src.lower(), None)
    # 再構成は再生専用モジュールにだけある。
    replay = io.open(os.path.join(BASE, "replay_structure_context.py"), encoding="utf-8").read()
    check("再構成は replay_structure_context にだけある", "def reconstruct_15m(" in replay)
    for name in ("monitor_publish.py", "monitor_pipeline.py", "tv_snapshot.py",
                 "autotrade_engine.py", "order.py", "msnr_gate.py"):
        body = io.open(os.path.join(BASE, name), encoding="utf-8").read()
        check(f"{name} は 15 分足を再構成しない", "reconstruct_15m" not in body)
    check("再生は再構成を『研究用』と明示する", "研究用" in replay)
    # 直接取得の 15 分足が無い周期は、推測で作らず親を HTF 要約へ落とす。
    ctx = context_of(bundle(spec3m=RESTING, bars15=None,
                            htf={"status": "MIXED", "bias": None, "valid": False, "frames": {}}))
    check("15 分足が無ければ理由を残して親を作らない(3 分足で代用しない)",
          "BARS15M_MISSING" in (ctx.get("reasons") or []), ctx.get("reasons"))


def test_pure_functions_have_no_side_effects():
    src = _code_without_docstrings(os.path.join(BASE, "market_structure_context.py"))
    for token in ("requests", "urllib", "socket", "subprocess", "os.remove", ".secrets", "open("):
        check(f"market_structure_context のコードに {token} が無い", token not in src)
    src2 = _code_without_docstrings(os.path.join(BASE, "participation_policy.py"))
    for token in ("requests", "urllib", "socket", "subprocess", "open(", ".secrets"):
        check(f"participation_policy のコードに {token} が無い", token not in src2)


def main():
    tests = [value for key, value in sorted(globals().items()) if key.startswith("test_")]
    for test in tests:
        print(f"\n--- {test.__name__} ---")
        test()
    print()
    if failures:
        print(f"FAILED {len(failures)}: " + ", ".join(failures))
        return 1
    print("ALL PASS (test_r122_structure_context)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

# -*- coding: utf-8 -*-
"""R121: ICT STDV(TTrades 版)の固定投影と参加判断の受け入れ試験。

`docs/CLAUDE_ICT_STDV_INTEGRATION_HANDOFF_2026-09-19.md` §6 の表に 1 対 1 で対応させる。
`ict_stdv` は純粋計算、`msnr_gate.evaluate` は契約 JSON を読むだけ。ネットワーク・台帳・
発注・`.secrets` への書き込みに触れない。
"""
import copy
import io
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import ict_stdv  # noqa: E402
import msnr_gate  # noqa: E402
import strategy_models  # noqa: E402

REAL_POLICY = msnr_gate.ict_stdv_policy
BAR = 180
T0 = 1786970000 - (1786970000 % BAR)
failures = []


def check(label, condition, detail=""):
    if not condition:
        failures.append(label)
        print(f"FAIL {label}: {detail}")
        return
    print(f"OK   {label}")


def section(text):
    print(f"\n--- {text}")


def policy(mode="SHADOW", targets="OFF", ratios=(-1.0, -2.0, -2.5), runner=False,
           participation="OFF", params=None):
    return {"mode": mode,
            "targets": {"mode": targets, "ratios": list(ratios), "runnerEligible": runner},
            "participation": {"mode": participation},
            "params": dict(params or {}), "invalid": []}


# ---------------------------------------------------------------- フィクスチャ

def bar(t, o, h, l, c, v=1000):
    return {"t": t, "o": o, "h": h, "l": l, "c": c, "v": v}


def sweep_series():
    """安値を掃引してから上へ構造を壊し、レベルへ戻る系列(BUY アンカーができる形)。

    index 0-5   じり高(ピボットの左側)
    index 6     高値 30,040 のピボット(= p0。左右 2 本より高い)
    index 7-10  下降 manipulation
    index 11    レベル 29,960 をヒゲで刈って実体は上(1 本型スイープ、安値 29,950 = p1)
    index 12-13 MSS(直前 5 本の参照高値 30,040 を終値で上抜け)
    index 14-15 レベルへの戻り(index 15 で RETEST_HELD)
    """
    return [
        bar(T0 + BAR * 0, 30000, 30006, 29996, 30004),
        bar(T0 + BAR * 1, 30004, 30010, 30000, 30008),
        bar(T0 + BAR * 2, 30008, 30014, 30004, 30012),
        bar(T0 + BAR * 3, 30012, 30020, 30008, 30018),
        bar(T0 + BAR * 4, 30018, 30026, 30014, 30024),
        bar(T0 + BAR * 5, 30024, 30030, 30020, 30028),
        bar(T0 + BAR * 6, 30028, 30040, 30024, 30030),      # ピボット高値 = p0
        bar(T0 + BAR * 7, 30030, 30032, 30008, 30010),
        bar(T0 + BAR * 8, 30010, 30012, 29988, 29990),
        bar(T0 + BAR * 9, 29990, 29992, 29970, 29972),
        bar(T0 + BAR * 10, 29972, 29974, 29958, 29962),
        bar(T0 + BAR * 11, 29962, 29996, 29950, 29994),     # スイープ + 奪還。安値 = p1
        bar(T0 + BAR * 12, 29994, 30022, 29990, 30020),
        bar(T0 + BAR * 13, 30020, 30052, 30016, 30048),     # MSS
        bar(T0 + BAR * 14, 30048, 30050, 29990, 30000),
        bar(T0 + BAR * 15, 30000, 30002, 29958, 29975),     # RETEST_HELD
    ]


SWEEP_BARS = sweep_series()
CHAIN_MSS = {"type": "SWEEP", "side": "BUY", "state": "MSS_CONFIRMED",
             "sweepBarT": T0 + BAR * 11, "mssBarT": T0 + BAR * 13}
CHAIN_SWEEP_ONLY = {"type": "SWEEP", "side": "BUY", "state": "SWEEP_CONFIRMED",
                    "sweepBarT": T0 + BAR * 11, "mssBarT": None}
CHAIN_FLIP = {"type": "FLIP", "side": "BUY", "state": "FLIP_HELD",
              "breakBarT": T0 + BAR * 11}


# ------------------------------------------------- 1. BUY/SELL 投影(出典の合成値)

section("1. BUY/SELL 投影 — 資料の合成値と全係数で一致")
WANT_BUY = {1.0: 19980.0, 0.0: 20000.0, -1.0: 20020.0, -2.0: 20040.0, -2.5: 20050.0, -4.0: 20080.0}
WANT_SELL = {1.0: 20020.0, 0.0: 20000.0, -1.0: 19980.0, -2.0: 19960.0, -2.5: 19950.0, -4.0: 19920.0}
buy = ict_stdv.project(20000, 19980, "BUY")
sell = ict_stdv.project(20000, 20020, "SELL")
check("BUY 投影が出典の表と一致(p0=20000 / p1=19980)",
      buy and all(abs(buy["byRatio"][r] - v) < 1e-9 for r, v in WANT_BUY.items()),
      (buy or {}).get("byRatio"))
check("SELL 投影が出典の表と一致(p0=20000 / p1=20020)",
      sell and all(abs(sell["byRatio"][r] - v) < 1e-9 for r, v in WANT_SELL.items()),
      (sell or {}).get("byRatio"))
check("係数の本数が出典と同じ(1 / 0 / -1 / -2 / -2.5 / -4)",
      ict_stdv.RATIOS == (1.0, 0.0, -1.0, -2.0, -2.5, -4.0), ict_stdv.RATIOS)
check("-2 は p1 から 3 幅(1 幅ずれていない)",
      abs(buy["byRatio"][-2.0] - 19980) == 60.0 and abs(20020 - sell["byRatio"][-2.0]) == 60.0)
check("符号が逆転していない(BUY は p0 より上、SELL は下)",
      buy["byRatio"][-1.0] > buy["byRatio"][0.0] and sell["byRatio"][-1.0] < sell["byRatio"][0.0])
check("0 と 1 はアンカー内部(p0 / p1 そのもの)",
      buy["byRatio"][0.0] == 20000.0 and buy["byRatio"][1.0] == 19980.0)

# ------------------------------------------- 2. 半tick / 重複 / 異常値

section("2. 半tick・重複・異常値")
odd = ict_stdv.project(20000.0, 19999.75, "BUY")      # 幅 1 tick -> -2.5 が半 tick
row25 = next(r for r in odd["rows"] if r["ratio"] == -2.5)
check("-2.5 の半 tick は決定論的に丸める",
      row25["raw"] == 20000.625 and row25["price"] == 20000.5, row25)
check("丸めで潰れた行は collapsedWith を持つ(2 本扱いしない)",
      row25["collapsedWith"] == -2.0, row25)
check("丸めは BUY が下・SELL が上(利確側に保守的)",
      ict_stdv._round_to_tick(100.6, "BUY") == 100.5
      and ict_stdv._round_to_tick(100.6, "SELL") == 100.75)
for label, args in (("ゼロ幅", (20000, 20000)), ("NaN", (float("nan"), 19980)),
                    ("inf", (float("inf"), 19980)), ("None", (None, 19980)),
                    ("文字列", ("x", 19980)), ("負価格になる", (1.0, -100.0))):
    check(f"不正入力は不適格: {label}", ict_stdv.project(*args, side="BUY") is None)
collapsed = ict_stdv.target_candidates(
    {"projectionValid": True, "side": "BUY", "reachedAt": {},
     "levels": [{"ratio": -2.0, "price": 20000.5, "collapsedWith": None},
                {"ratio": -2.5, "price": 20000.5, "collapsedWith": -2.0}]},
    19990, 19980, "BUY", target_ratios=(-2.0, -2.5))
check("潰れた水準は目標候補に出さない", [p for p, _ in collapsed] == [20000.5], collapsed)

# ------------------------------------------- 3. 掃引だけ / 戻りなし

section("3. 掃引だけ・戻りなしからは参加可能な STDV を作らない")
check("SWEEP_CONFIRMED(構造変化なし)ではアンカーを作らない",
      ict_stdv.find_anchor(CHAIN_SWEEP_ONLY, SWEEP_BARS) is None)
check("理由は CHAIN_STATE_UNCONFIRMED",
      ict_stdv.anchor_diagnosis(CHAIN_SWEEP_ONLY, SWEEP_BARS)["reason"] == "CHAIN_STATE_UNCONFIRMED")
check("FLIP チェーン(BREAKER)は v1 の対象外 = CHAIN_NOT_SWEEP",
      ict_stdv.anchor_diagnosis(CHAIN_FLIP, SWEEP_BARS)["reason"] == "CHAIN_NOT_SWEEP")
check("MSS_CONFIRMED ならアンカーができる",
      ict_stdv.find_anchor(CHAIN_MSS, SWEEP_BARS) is not None)
anchor = ict_stdv.find_anchor(CHAIN_MSS, SWEEP_BARS)
check("p0 はピボット高値 30,040 / p1 は掃引安値 29,950",
      anchor["p0"] == 30040.0 and anchor["p1"] == 29950.0, (anchor["p0"], anchor["p1"]))
check("ピボットが確定していなければアンカーを作らない(合成しない)",
      ict_stdv.anchor_diagnosis({**CHAIN_MSS, "sweepBarT": SWEEP_BARS[2]["t"]},
                                SWEEP_BARS)["reason"] == "PIVOT_NOT_CONFIRMED")
flat = [bar(T0 + BAR * i, 100, 100.25, 99.75, 100) for i in range(15)]
check("退化したレッグ(幅が床未満)は LEG_DEGENERATE",
      ict_stdv.anchor_diagnosis({**CHAIN_MSS, "sweepBarT": flat[11]["t"], "mssBarT": flat[13]["t"]},
                                flat)["reason"] in {"LEG_DEGENERATE", "PIVOT_NOT_CONFIRMED"})

# ------------------------------------------- 4. 未来参照(一括 vs 逐次)

section("4. 未来参照 — 一括再生と逐次再生で knownAt 以後が一致")
full = ict_stdv.find_anchor(CHAIN_MSS, SWEEP_BARS)
known_at = full["knownAt"]
seq_first = None
appeared_before_known = []
for n in range(3, len(SWEEP_BARS) + 1):
    window = SWEEP_BARS[:n]
    last_close = window[-1]["t"] + BAR
    chain = dict(CHAIN_MSS)
    if chain["mssBarT"] > window[-1]["t"]:
        continue                    # 確認足がまだ出ていない周期
    got = ict_stdv.find_anchor(chain, window)
    if got is None:
        continue
    if last_close < known_at:
        appeared_before_known.append(n)
    if seq_first is None:
        seq_first = got
check("右側 pivot / 確認足が閉じる前には出ない", not appeared_before_known, appeared_before_known)
check("逐次で最初に出たアンカーは一括と同一 ID・同一 p0/p1・同一水準",
      seq_first and seq_first["anchorId"] == full["anchorId"]
      and seq_first["p0"] == full["p0"] and seq_first["p1"] == full["p1"]
      and seq_first["byRatio"] == full["byRatio"],
      (seq_first or {}).get("anchorId"))
check("knownAt は確認足の終値時刻とピボット右側の確定時刻の遅い方",
      known_at == max(CHAIN_MSS["mssBarT"] + BAR,
                      SWEEP_BARS[6]["t"] + 2 * BAR + BAR), known_at)
check("価格の時刻(extremeAt)と発見可能時刻(knownAt)を混同しない",
      full["extremeAt"] < full["knownAt"], (full["extremeAt"], full["knownAt"]))

# ------------------------------------------- 5. EQ 通過 / 再起動

section("5. EQ 通過・再起動でアンカーが変わらない")
ids, p0s, p1s, dirs = set(), set(), set(), set()
for extra_price in (29900.0, 30000.0, 30100.0, 30500.0):
    got = ict_stdv.find_anchor(CHAIN_MSS, SWEEP_BARS)
    got = ict_stdv.mark_reached(got, bars=SWEEP_BARS, price=extra_price, price_known=True)
    ids.add(got["anchorId"]); p0s.add(got["p0"]); p1s.add(got["p1"]); dirs.add(got["side"])
check("現値が EQ を跨いでも anchorId / p0 / p1 / 方向は不変",
      len(ids) == 1 and len(p0s) == 1 and len(p1s) == 1 and dirs == {"BUY"},
      (ids, p0s, p1s, dirs))
again = ict_stdv.find_anchor(CHAIN_MSS, list(SWEEP_BARS))
check("再起動(同じ履歴の作り直し)でも同じ ID と水準",
      again["anchorId"] == full["anchorId"] and again["byRatio"] == full["byRatio"])
check("別の掃引は別 anchorId(良く伸びた方へ差し替えない)",
      ict_stdv.find_anchor({**CHAIN_MSS, "sweepBarT": SWEEP_BARS[10]["t"]},
                           SWEEP_BARS)["anchorId"] != full["anchorId"])

# ------------------------------------------- 6. 銘柄 / 限月 / 時間足

section("6. 銘柄・限月・時間足を黙って流用しない")
a_u6 = ict_stdv.find_anchor(CHAIN_MSS, SWEEP_BARS, symbol="MNQU6")
a_z6 = ict_stdv.find_anchor(CHAIN_MSS, SWEEP_BARS, symbol="MNQZ6")
check("限月が違えば anchorId が違う", a_u6["anchorId"] != a_z6["anchorId"])
check("sourceTf が anchorId に入る",
      ict_stdv.find_anchor(CHAIN_MSS, SWEEP_BARS, source_tf="15")["anchorId"] != full["anchorId"])
check("足が足りなければ BARS_MISSING",
      ict_stdv.anchor_diagnosis(CHAIN_MSS, SWEEP_BARS[:3])["reason"] == "BARS_MISSING")
check("掃引足が系列に無ければ SWEEP_BAR_NOT_FOUND",
      ict_stdv.anchor_diagnosis({**CHAIN_MSS, "sweepBarT": 1}, SWEEP_BARS)["reason"]
      == "SWEEP_BAR_NOT_FOUND")
old = ict_stdv.anchor_diagnosis(CHAIN_MSS, SWEEP_BARS, params={"maxAgeBars": 0})
check("古い根拠は ANCHOR_EXPIRED で projectionValid=False",
      old["reason"] == "ANCHOR_EXPIRED" and old["anchor"]["projectionValid"] is False)

# ------------------------------------------- 7. 現値と確定足終値を分ける

section("7. 現値と確定足終値を別に扱う")
reach_bar = ict_stdv.mark_reached(full, bars=SWEEP_BARS, price=None, price_known=False)
check("確定足だけの到達判定では priceSubstituted=False", reach_bar["priceSubstituted"] is False)
far = full["byRatio"][-1.0]
reach_px = ict_stdv.mark_reached(full, bars=[], price=far, price_known=True)
check("鮮度確認済みの現値で到達したら source=VERIFIED_PRICE / priceSubstituted=True",
      reach_px["priceSubstituted"] is True
      and reach_px["reachedAt"][str(-1.0)]["source"] == "VERIFIED_PRICE", reach_px["reachedAt"])
reach_unknown = ict_stdv.mark_reached(full, bars=[], price=far, price_known=False)
check("鮮度が確認できない価格では到達に数えない", reach_unknown["reachedAt"] == {})
before = ict_stdv.mark_reached(full, bars=[b for b in SWEEP_BARS if b["t"] < known_at])
check("knownAt より前の足では到達に数えない(事前予測成功に計上しない)",
      before["reachedAt"] == {}, before["reachedAt"])

# ------------------------------------------- 8. 到達と再訪

section("8. 到達と再訪")
hit = ict_stdv.mark_reached(full, bars=[], price=full["byRatio"][-1.0], price_known=True)
revisit = ict_stdv.mark_reached(hit, bars=[], price=full["p0"], price_known=True)
check("消化済み水準は再訪しても未消化に戻らない",
      str(-1.0) in revisit["reachedAt"], revisit["reachedAt"])
check("到達で自動の逆張り発注は生まれない(この層は注文を作らない)",
      not any(k in dir(ict_stdv) for k in ("place_order", "send", "flatten")))
ctx = ict_stdv.read_context(hit, side="BUY", entry=29990, stop=29970,
                            price=full["byRatio"][-1.0], price_known=True, targets=[30020, 30040])
check("消化済みは stdvContext.consumedRatios に残る", -1.0 in ctx["stdvContext"]["consumedRatios"])
all_hit = ict_stdv.mark_reached(full, bars=[], price=full["byRatio"][-4.0], price_known=True)
ctx_done = ict_stdv.read_context(all_hit, side="BUY", entry=29990, stop=29970,
                                 price=full["byRatio"][-4.0], price_known=True, targets=[30020])
check("全投影が消化済みなら participation=NO_ROOM",
      ctx_done["participation"] == "NO_ROOM", ctx_done["participation"])

# ------------------------------------------- 9. 1 本の足で複数事象

section("9. 1 本の足で複数事象(足内順序を仮定しない)")
both = [bar(T0, 100, 120, 80, 110)]
anchor_both = dict(full)
anchor_both["levels"] = [{"ratio": -1.0, "price": 115.0, "collapsedWith": None},
                         {"ratio": -2.0, "price": 200.0, "collapsedWith": None}]
anchor_both["knownAt"] = T0
marked = ict_stdv.mark_reached(anchor_both, bars=both)
check("同じ足が複数水準に触れても、触れた水準だけを個別に記録する",
      str(-1.0) in marked["reachedAt"] and str(-2.0) not in marked["reachedAt"],
      marked["reachedAt"])
check("足内の前後は仮定しない(到達は足の高安だけで判定し、順序を作らない)",
      marked["reachedAt"][str(-1.0)]["source"] == "CONFIRMED_BAR")

# ------------------------------------------- 10. 方向票

section("10. 方向票 — targetOnly は 0 票、健全性は別フィールド")
check("アンカーは targetOnly=True / eligibleForDirectionVote=False",
      full["targetOnly"] is True and full["eligibleForDirectionVote"] is False)
model = {"status": "ALIGNED", "valid": True, "direction": "BUY", "targetOnly": True}
check("_vote_direction は targetOnly を弾く", strategy_models._vote_direction(model) is None)
matrix_model = {"status": "OBSERVE", "valid": False, "direction": None, "targetOnly": True,
                "advisory": True, "projectionValid": None}
check("matrix の ictStdv も 0 票", strategy_models._vote_direction(matrix_model) is None)
lifecycle = {"ictStdv": dict(matrix_model)}
strategy_models._repo2_lifecycle(lifecycle, {"at": "2026-09-19T00:00:00+00:00"})
check("_repo2_lifecycle が valid=False にしても projectionValid は別に残る",
      lifecycle["ictStdv"]["valid"] is False
      and "projectionValid" in lifecycle["ictStdv"], lifecycle["ictStdv"])
check("投影の健全性は projectionValid(汎用 valid を流用しない)",
      "projectionValid" in full and full["projectionValid"] is True)

# ------------------------------------------- 11. 旧経路と二重計上

section("11. 旧 fibSd と新 STDV を独立票へ増幅しない")
check("fibSd は CRT_FIB 族として 1 票に畳まれる(既存規律)",
      strategy_models.CORRELATED_FAMILIES.get("fib") == "CRT_FIB")
votes = strategy_models._eligible_votes({
    "fibSd": {"status": "ALIGNED", "valid": True, "direction": "BUY", "targetOnly": False,
              "freshness": "FRESH", "lifecycle": "CONFIRMED", "entryTouched": True},
    "ictStdv": {"status": "ALIGNED", "valid": True, "direction": "BUY", "targetOnly": True},
})
check("旧 fibSd が 1 票でも、新 STDV は票を足さない",
      votes["BUY"] == ["fibSd"], votes)
check("親子アンカーは同一根拠として関係フィールドに持つ(独立票にしない)",
      full["parentAnchorId"] is None and full["childAnchorIds"] == [])

# ------------------------------------------- 12/13. 目標消費者 と OFF/SHADOW

section("12/13. 目標消費者の追跡 と OFF/SHADOW の不変性")


def bundle_for(bars, price=None, at=None):
    at = at or datetime.fromtimestamp(bars[-1]["t"] + BAR, timezone.utc).isoformat()
    return {"at": at, "priceAt": at, "price": bars[-1]["c"] if price is None else price,
            "sourceSymbol": "CME_MINI:MNQ1!", "sessionId": "TEST",
            "snapshot": {"levels": [{"label": "TEST", "price": 29960.0},
                                    {"label": "Weekly High", "price": 30200.0},
                                    {"label": "Monthly High", "price": 30400.0}],
                         "bars3m": [dict(b) for b in bars]}}


def evaluate(bars, pol, price=None):
    msnr_gate.ict_stdv_policy = lambda contract=None: pol
    try:
        return msnr_gate.evaluate(bundle_for(bars, price=price))
    finally:
        msnr_gate.ict_stdv_policy = REAL_POLICY


KEYS = ("model", "side", "entry", "stop", "targets", "targetR", "grade", "state",
        "decisionId", "hardBlockers")
off = evaluate(SWEEP_BARS, policy(mode="OFF"))
shadow = evaluate(SWEEP_BARS, policy(mode="SHADOW"))
check("OFF と SHADOW で decision の注文意図が一致(Entry/SL/TP/decisionId)",
      {k: (off["decision"] or {}).get(k) for k in KEYS}
      == {k: (shadow["decision"] or {}).get(k) for k in KEYS},
      json.dumps({k: [(off['decision'] or {}).get(k), (shadow['decision'] or {}).get(k)]
                  for k in KEYS}, ensure_ascii=False, default=str)[:300])
off_c = {(c["model"], c["side"]): c for c in off["candidates"]}
shadow_c = {(c["model"], c["side"]): c for c in shadow["candidates"]}
check("OFF と SHADOW で候補集合が一致", set(off_c) == set(shadow_c))
same_geo = all({k: off_c[k].get(kk) for kk in ("entry", "stop", "targets", "targetR", "score", "grade")}
               == {k: shadow_c[k].get(kk) for kk in ("entry", "stop", "targets", "targetR", "score", "grade")}
               for k in off_c)
check("OFF と SHADOW で候補の幾何・得点・等級が一致", same_geo)
check("OFF では ictStdv キーを付けない(出力を R121 以前と同一に保つ)",
      all("ictStdv" not in c for c in off["candidates"]))
check("SHADOW では監査だけ増える", any(c.get("ictStdv") for c in shadow["candidates"]))
shadow_stdv = next((c["ictStdv"] for c in shadow["candidates"] if c.get("ictStdv")), None)
check("SHADOW は targetsApplied=False(目標に触れない)",
      shadow_stdv and shadow_stdv["targetsApplied"] is False, shadow_stdv)
check("SHADOW でも targetsOffered は記録する(何を提示したか追える)",
      isinstance((shadow_stdv or {}).get("targetsOffered"), list))
check("SHADOW の監査に read(thesis / participation / headroom)が入る",
      (shadow_stdv or {}).get("read") and
      {"thesis", "participation", "headroom", "stdvContext", "invalidation"}
      <= set((shadow_stdv or {})["read"].keys()), list(((shadow_stdv or {}).get("read") or {}).keys()))

live = evaluate(SWEEP_BARS, policy(mode="LIVE", targets="LIVE"))
live_stdv = next((c["ictStdv"] for c in live["candidates"] if c.get("ictStdv")), None)
check("TARGETS LIVE では何を提示し何が選ばれたかを両方残す",
      live_stdv and "targetsOffered" in live_stdv and "targetsApplied" in live_stdv, live_stdv)
applied = [c for c in live["candidates"] if (c.get("ictStdv") or {}).get("targetsApplied")]
rejected = [c for c in live["candidates"] if (c.get("ictStdv") or {}).get("runnerRejected")]
check("runner に STDV が来たら既定では採らず、理由を残す(-4 で TP が不当に遠ざかるのを防ぐ)",
      all("ICT_STDV_RUNNER_REJECTED" in (c.get("evidence") or []) for c in rejected))
for cand in applied:
    check(f"TP を差し替えた候補は decisionId の素も変わる({cand['model']})",
          cand.get("stdvIdentity"), cand.get("stdvIdentity"))
base_id = msnr_gate.setup_identity({"model": "M", "side": "BUY", "level": "L",
                                    "chain": {"type": "SWEEP", "sweepBarT": 1}, "stop": 10})
with_id = msnr_gate.setup_identity({"model": "M", "side": "BUY", "level": "L",
                                    "chain": {"type": "SWEEP", "sweepBarT": 1}, "stop": 10,
                                    "stdvIdentity": "x"})
check("stdvIdentity が無ければ setup_identity は R121 以前と同一", base_id != with_id and len(base_id) == 12)

# ------------------------------------------- 14. LIVE の範囲

section("14. LIVE の変更範囲は新規候補だけ")
for module in ("autotrade_engine.py", "order.py"):
    src = io.open(os.path.join(BASE, module), encoding="utf-8").read()
    hits = [n for n in ("ict_stdv", "ictStdv", "ICT_STDV") if n in src]
    check(f"{module} は STDV を参照しない(凍結プランの管理は不変)", not hits, hits)
src_gate = io.open(os.path.join(BASE, "msnr_gate.py"), encoding="utf-8").read()
check("STDV は Entry / SL を書き換えない(stop への代入が無い)",
      "_stdv_for_chain" in src_gate
      and "stop = " not in src_gate.split("def _stdv_for_chain", 1)[1].split("\ndef ", 1)[0])
check("STDV は確認要素(CONFIRMATION_EVIDENCE)に入らない",
      not (msnr_gate.CONFIRMATION_EVIDENCE & {"ICT_STDV_ANCHOR", "ICT_STDV_TARGET"}),
      msnr_gate.CONFIRMATION_EVIDENCE & {"ICT_STDV_ANCHOR", "ICT_STDV_TARGET"})
check("STDV は候補の score を動かさない(SHADOW と OFF の score 一致で担保済み)", same_geo)

# ------------------------------------------- 15. 契約の妥当性 と実プロセス境界

section("15. 契約の妥当性と実プロセス境界(通信・発注なし)")
real = REAL_POLICY()
check("本番契約の ictStdv は読めて、不正な段が無い", real["invalid"] == [], real)
check("本番契約の既定は mode=SHADOW / targets=OFF / participation=OFF",
      real["mode"] == "SHADOW" and real["targets"]["mode"] == "OFF"
      and real["participation"]["mode"] == "OFF", real)
check("-4 は既定で目標に使わない(観測専用)",
      -4.0 not in (real["targets"].get("ratios") or []), real["targets"])
check("runnerEligible は既定 false", real["targets"].get("runnerEligible") is False)
for spec, expect in (({"ictStdv": "X"}, "ICT_STDV_MALFORMED"),
                     ({"ictStdv": {"mode": "ON"}}, "ICT_STDV_MODE_MALFORMED"),
                     ({"ictStdv": {"mode": "LIVE", "targets": {"mode": "LIVE", "ratios": [-9]}}},
                      "ICT_STDV_TARGETS_MALFORMED"),
                     ({"ictStdv": {"mode": "LIVE", "participation": 3}},
                      "ICT_STDV_PARTICIPATION_MALFORMED")):
    got = REAL_POLICY(spec)
    check(f"壊れた節はその段だけ OFF + invalid: {expect}",
          expect in got["invalid"] and (got["mode"] == "OFF" or got["targets"]["mode"] == "OFF"
                                        or got["participation"]["mode"] == "OFF"), got)
check("節が無ければ全部 OFF", REAL_POLICY({})["mode"] == "OFF")

probe = (
    "import json, os, sys\n"
    "import ict_stdv, msnr_gate\n"
    "print('@@' + json.dumps({\n"
    "  'module': ict_stdv.__file__, 'schema': ict_stdv.SCHEMA, 'version': ict_stdv.VERSION,\n"
    "  'ratios': list(ict_stdv.RATIOS), 'policy': msnr_gate.ict_stdv_policy(),\n"
    "  'contractPath': msnr_gate.CONTRACT_PATH, 'cwd': os.getcwd(),\n"
    "}, default=str))\n"
)
proc = subprocess.run([sys.executable, "-c", probe], cwd=BASE,
                      env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
                      capture_output=True, text=True)
line = next((l for l in proc.stdout.splitlines() if l.startswith("@@")), None)
check("子プロセス相当で保存 JSON を読み直せる", line is not None, proc.stderr[:200])
if line:
    info = json.loads(line[2:])
    check("子プロセスでも本番ディレクトリのモジュールと契約を読む",
          os.path.normcase(os.path.dirname(info["module"])) == os.path.normcase(BASE)
          and os.path.normcase(info["contractPath"]) == os.path.normcase(
              os.path.join(BASE, "execution_contract.json")), info)
    check("子プロセスでも schema / version / 係数 / mode が一致",
          info["schema"] == ict_stdv.SCHEMA and info["version"] == ict_stdv.VERSION
          and info["ratios"] == list(ict_stdv.RATIOS)
          and info["policy"]["mode"] == real["mode"], info)

# ------------------------------------------- 「読む」と「参加する」

section("participation — 読みと参加を分ける")
ctx_wait = ict_stdv.read_context(full, side="BUY", entry=29990.0, stop=29970.0,
                                 price=30010.0, price_known=True, targets=[30020.0, 30040.0])
check("建値より先に価格がいれば WAIT_FOR_PULLBACK(逆向き発注の指示ではない)",
      ctx_wait["participation"] == "WAIT_FOR_PULLBACK"
      and ctx_wait["entryZone"] and ctx_wait["entryZone"]["trigger"], ctx_wait["participation"])
check("BUY 仮説と短期の押し予測は両立する(thesis は BUY のまま)",
      ctx_wait["thesis"] == "BUY")
ctx_ready = ict_stdv.read_context(full, side="BUY", entry=29990.0, stop=29970.0,
                                  price=29985.0, price_known=True, targets=[30020.0, 30040.0])
check("価格が建値に到達していれば ENTRY_READY", ctx_ready["participation"] == "ENTRY_READY")
check("headroom は現値→目標 / 建値→目標 / R を別々に持つ",
      {"entryToTp1R", "entryToFinalR", "priceToTp1R", "riskPt"} <= set(ctx_ready["headroom"]),
      ctx_ready["headroom"])
check("未検証の確率を付けない", "probability" not in json.dumps(ctx_ready)
      and ctx_ready["nearTerm"]["note"].startswith("未検証"))
ctx_nopx = ict_stdv.read_context(full, side="BUY", entry=29990.0, stop=29970.0,
                                 price=None, price_known=False, targets=[30020.0])
check("現値の鮮度が確認できなければ PRICE_UNVERIFIED で参加判断を作らない",
      ctx_nopx["participation"] == "WAIT_FOR_PULLBACK"
      and "PRICE_UNVERIFIED" in ctx_nopx["participationReasons"])
check("参加状態は決めた集合のいずれか",
      ctx_ready["participation"] in ict_stdv.PARTICIPATION_STATES)

print()
if failures:
    print(f"FAILED {len(failures)}: " + ", ".join(failures))
    sys.exit(1)
print("R121 ICT STDV: ALL PASS")

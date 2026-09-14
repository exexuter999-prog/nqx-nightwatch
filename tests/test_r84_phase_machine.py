#!/usr/bin/env python3
"""R84: 管理位相機械(§5)。

ACCUMULATION で `cancelandbracket` を出さないこと(TP1 を消さない)、governing stop と
flattenBeyond の FLATTEN、全 TP1 解決 → 統合 MODIFY 1 回、統合後のトレールが既存の
runner 管理と一致することを固定する。

    python tests/test_r84_phase_machine.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _r84_fixtures as fx  # noqa: E402

sys.path.insert(0, fx.BASE)
import autotrade_engine as ae  # noqa: E402
import tranche  # noqa: E402

PASS = [0]
FAIL = [0]


def check(label, condition, detail=""):
    if condition:
        PASS[0] += 1
        print(f"  OK   {label}")
    else:
        FAIL[0] += 1
        print(f"  NG   {label}  {detail}")


PLAN = fx.composite()
ROWS = fx.live_orders_full()
T1, T2 = fx.tranche_one(), fx.tranche_two()
TP1_IDS = T1["legs"][0]["bracketOrderIds"] + T2["legs"][0]["bracketOrderIds"]


def states(rows):
    return tranche.leg_states(PLAN, rows, account=fx.ACC, symbol=fx.SYM, action=fx.SIDE)


def act(rows, price, qty, **over):
    live = states(rows)
    plan = {**PLAN, "pyramid": {**PLAN["pyramid"],
                                "flattenBeyond": tranche.flatten_beyond(PLAN, live)}}
    kwargs = dict(price=price, states=live, stop_buffer=4.0, breakeven_offset=1.0)
    kwargs.update(over)
    return tranche.management_action_composite(plan, fx.position(qty=qty), **kwargs)


print("--- ACCUMULATION: MODIFY 禁止 ---")
# SELL。価格が大きく利益方向へ動いても、TP1 脚が生きている間は張り替えない。
check("TP1 が生きている間は MODIFY を出さない(cancelandbracket は TP1 を消す)",
      act(ROWS, 29420.0, 8) is None)
check("価格が押し戻しても同じ(見送りであって HALT ではない)",
      act(ROWS, 29490.0, 8) is None)
check("位相は ACCUMULATION", tranche.phase(PLAN, states(ROWS)) == tranche.ACCUMULATION)

print()
print("--- ACCUMULATION でも FLATTEN は生きる ---")
flat = act(ROWS, 29510.0, 8)
check("governing stop(最新トランシェの構造 SL)を突破したら FLATTEN",
      flat and flat["action"] == "FLATTEN" and "governing" in flat["reason"], str(flat))
beyond = act(ROWS, 29295.0, 8)
check("最遠 live runner 目標を越えて建玉が残れば FLATTEN",
      beyond and beyond["action"] == "FLATTEN" and "farthest" in beyond["reason"], str(beyond))
check("forceFlatten は位相に関係なく効く",
      (act(ROWS, 29450.0, 8, force_flatten=True) or {}).get("action") == "FLATTEN")
check("KILL は位相に関係なく効く",
      (act(ROWS, 29450.0, 8, kill=True) or {}).get("action") == "FLATTEN")
check("セッション締めも同様",
      (act(ROWS, 29450.0, 8, session_flatten=True) or {}).get("action") == "FLATTEN")

print()
print("--- RUNNERS: 統合 MODIFY 1 回 ---")
runners = [row for row in ROWS if row["orderId"] not in TP1_IDS]
check("位相は RUNNERS", tranche.phase(PLAN, states(runners)) == tranche.RUNNERS)
modify = act(runners, 29420.0, 4)
check("全 TP1 解決後の最初の MODIFY が統合",
      modify and modify["action"] == "MODIFY" and modify.get("consolidate") is True,
      str(modify))
check("統合は残る runner ブラケット全部を 1 組へ畳む(openLegs=2)",
      modify and modify.get("openLegs") == 2, str(modify))
check("qty は建玉全量", modify and modify["qty"] == 4, str(modify))
check("TP は方針値(最新トランシェの runner 目標)",
      modify and abs(modify["tp"] - 29300.0) < 1e-9, str(modify))
# SELL の建値は 29466.375 → 床は建値 −1pt = 29465.375
check("SL は建値±1pt の床以上(建値で刈られて負けにしない)",
      modify and modify["sl"] <= 29465.5, str(modify))

print()
print("--- 単調性・protective は既存 runner 管理と同じ ---")
check("既に同じ SL なら出さない(単調性)",
      act(runners, 29420.0, 4, current_stop=modify["sl"]) is None)
# 建値 29466.375 → 床は建値 −1pt = 29465.375(tick へ丸める)。
FLOOR = tranche._tick(29466.375 - 1.0)
fallback = act(runners, 29450.0, 4, current_stop=29507.5, best=29390.0)
check("トレール目標(極値+距離)が現在値を追い越しても床だけは寄せる(R70)",
      fallback and abs(fallback["sl"] - FLOOR) < 1e-9, str(fallback))
check("床すら現在値の正しい側(+buffer)でなければ見送る(R37/R78 の逆側 SL を作らない)",
      act(runners, 29464.0, 4, current_stop=29507.5, best=29390.0) is None,
      str(act(runners, 29464.0, 4, current_stop=29507.5, best=29390.0)))
trailed = act(runners, 29360.0, 4, current_stop=29465.5, best=29350.0)
check("極値から距離を足したトレールが進む(SELL は 安値+距離)",
      trailed and abs(trailed["sl"] - (29350.0 + PLAN["trailDistance"])) < 1e-9,
      str(trailed))

print()
print("--- 遷移回廊は FLATTEN 判定のみ ---")
check("回廊中は MODIFY を出さない",
      act(runners, 29420.0, 4, transit=True) is None)
check("回廊中でも FLATTEN は出る",
      (act(runners, 29510.0, 4, transit=True) or {}).get("action") == "FLATTEN")

print()
print("--- 異常系 ---")
check("方向が違えば HALT",
      (tranche.management_action_composite(PLAN, fx.position(qty=8, side="LONG"),
                                           price=29450.0, states=states(ROWS)) or {}
       ).get("action") == "HALT")
check("価格が取れなければ HALT",
      (act(ROWS, None, 8) or {}).get("action") == "HALT")
check("建玉 0 なら何もしない", act(ROWS, 29450.0, 0) is None)
check("生きている脚が無いのに建玉が残るのは HALT",
      (act([], 29450.0, 4) or {}).get("action") == "HALT", str(act([], 29450.0, 4)))

print()
print("--- 統合後は既存 runner 管理と同形 ---")
pair = fx.oco_rows(["C-A", "C-B"])
merged = {"trancheId": "C2", "entryKey": PLAN["entryKey"], "initialStop": 29465.5,
          "entry": 29466.375, "orderType": "MARKET",
          "legs": [{"id": "RUNNER", "qty": 4, "target": 29300.0}],
          "consolidationPair": {"orderIds": ["C-A", "C-B"], "receipts": ["R:C-A", "R:C-B"],
                                "qty": 4, "sl": 29465.5, "tp": 29300.0}}
after = {**PLAN, "tranches": [merged], "qty": 4, "initialStop": 29465.5,
         "legs": [{"id": "RUNNER", "qty": 4, "target": 29300.0, "trancheId": "C2"}],
         "consolidation": merged["consolidationPair"],
         "pyramid": {**PLAN["pyramid"], "flattenBeyond": 29300.0,
                     "governingStop": 29465.5}}
after_states = tranche.leg_states(after, pair, account=fx.ACC, symbol=fx.SYM, action=fx.SIDE)
POS4 = fx.position(qty=4)
BUNDLE = {"price": 29360.0, "at": "2026-09-12T03:00:00Z",
          "snapshot": {"bars3m": [
              {"t": 1789099200, "o": 29400, "h": 29400, "l": 29350.0, "c": 29360}]}}
# 既存経路が使う極値と同じものを合成側へも渡す(入力を揃えて初めて比較になる)。
BEST = ae._extreme(BUNDLE, "SELL", ae._parse_at(POS4.get("filledAt")))
plain = tranche.management_action_composite(
    after, POS4, price=29360.0, states=after_states,
    current_stop=29465.5, best=BEST, stop_buffer=4.0, breakeven_offset=1.0)
single = ae.management_action(
    {"side": "SELL", "symbol": fx.SYM, "qty": 8, "entry": 29466.375,
     "initialStop": 29465.5, "tp1": 29440.0, "finalTarget": 29300.0,
     "trailDistance": PLAN["trailDistance"],
     "legs": [{"id": "TP1", "qty": 4, "target": 29440.0},
              {"id": "RUNNER", "qty": 4, "target": 29300.0}]},
    POS4, BUNDLE, {"stop": 29465.5}, {"NQX_AUTOTRADE_KILL": "0"})
check("統合後のトレール SL は既存 management_action と一致",
      plain and single and abs(plain["sl"] - single["sl"]) < 1e-9,
      f"{plain} vs {single}")
check("統合後の TP も一致(finalTarget)",
      plain and single and abs(plain["tp"] - single["tp"]) < 1e-9)

print()
print(f"合計: PASS {PASS[0]} / FAIL {FAIL[0]}")
sys.exit(1 if FAIL[0] else 0)

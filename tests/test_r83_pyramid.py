#!/usr/bin/env python3
"""R83: 追撃(pyramiding)の判定。

2026-09-12 ユーザー決定のとおり (a) トリガーは同方向の新規シグナル、(b) 枚数は
ULTRA で合計を引き直して差分を足す、(c) リスク上限は**追撃後の合計建玉**へ掛ける。

実例(2026-09-12 02:12): SHORT 2 @29,484.25 を保有中に `VP80_REVERSION SELL A+`
が ARMED。人は 6 枚乗せて SHORT 8 @29,457.50 とし、+$769 を取った。

    python tests/test_r83_pyramid.py
"""
import copy
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
import pyramid  # noqa: E402

PASS = [0]
FAIL = [0]


def check(label, condition, detail=""):
    if condition:
        PASS[0] += 1
        print(f"  OK   {label}")
    else:
        FAIL[0] += 1
        print(f"  NG   {label}  {detail}")


ON = {"pyramid": {"enabled": True, "dryRun": True, "trigger": "SAME_SIDE_SIGNAL",
                  "minGrade": "A", "maxAdds": 2}}
PLAN = {"side": "SELL", "qty": 2, "symbol": "MNQU6"}
SIGNAL = {"side": "SELL", "grade": "A+", "state": "ARMED", "model": "VP80_REVERSION"}
POS = {"side": "SHORT", "qty": 2, "avgEntry": 29484.25}
PV = 2.0


def call(**over):
    kwargs = dict(owned_plan=PLAN, scenario=SIGNAL, position=POS, adds_done=0,
                  add_price=29448.5, stop=29507.5, target_total_qty=8,
                  risk_cap_dollars=2268.0, point_value=PV, contract=ON)
    kwargs.update(over)
    return pyramid.evaluate(**kwargs)


print("--- スイッチ ---")
# R84 M0(docs/R84_PYRAMID_TRANCHE_MANAGEMENT.md §11): 実装完了で契約は
# enabled=true / **dryRun=true**(影運転)へ進んだ。dryRun が True である限り
# 判定は注記と台帳に出るだけで、一枚も送らない。dryRun=false へ進めてよいのは
# M3(Worker デプロイ + conformance)が終わってからで、そこは人の手順である。
check("契約は影運転(enabled=true / dryRun=true)",
      pyramid.enabled() is True and pyramid.dry_run() is True)
check("enabled=false なら評価せず PYRAMID_DISABLED",
      call(contract={"pyramid": {"enabled": False}})["reason"] == "PYRAMID_DISABLED")
check("pyramid 節が無い契約でも落ちずに無効",
      pyramid.evaluate(owned_plan=PLAN, scenario=SIGNAL, position=POS, adds_done=0,
                       add_price=1.0, stop=2.0, target_total_qty=4,
                       risk_cap_dollars=100.0, point_value=PV,
                       contract={})["reason"] == "PYRAMID_DISABLED")

print()
print("--- トリガー(同方向の新規シグナル) ---")
res = call()
check("同方向 A+ ARMED なら追撃する", res["add"] is True and res["reason"] == "PYRAMID_ADD",
      str(res))
check("差分だけ足す(目標 8 − 保有 2 = 6)",
      res["qty"] == 6 and res["totalQty"] == 8, str(res))
check("dryRun をそのまま返す(呼び出し側が送信可否に使う)", res["dryRun"] is True)
check("逆方向のシグナルでは追撃しない",
      call(scenario={**SIGNAL, "side": "BUY"})["reason"] == "SIDE_MISMATCH")
check("建玉の向きがプランと違えば追撃しない",
      call(position={**POS, "side": "LONG"})["reason"] == "SIDE_MISMATCH")
check("WATCH のシグナルでは追撃しない",
      call(scenario={**SIGNAL, "state": "WATCH"})["reason"] == "SCENARIO_NOT_ARMED")
check("建玉が無ければ追撃しない",
      call(position={**POS, "qty": 0})["reason"] == "NO_POSITION")
check("所有プランが無ければ追撃しない",
      call(owned_plan={})["reason"] == "NO_OWNED_PLAN")

print()
print("--- 等級の下限 ---")
check("minGrade=A なら B は弾く",
      call(scenario={**SIGNAL, "grade": "B"})["reason"] == "GRADE_BELOW_MIN")
check("minGrade=A なら A は通る", call(scenario={**SIGNAL, "grade": "A"})["add"] is True)
strict = copy.deepcopy(ON)
strict["pyramid"]["minGrade"] = "A+"
check("minGrade=A+ にすれば A も弾く",
      call(scenario={**SIGNAL, "grade": "A"}, contract=strict)["reason"] == "GRADE_BELOW_MIN")

print()
print("--- 回数の上限 ---")
check("maxAdds=2 なら 2 回済みで止まる",
      call(adds_done=2)["reason"] == "MAX_ADDS_REACHED")
check("1 回済みならまだ足せる", call(adds_done=1)["add"] is True)

print()
print("--- 枚数と合計リスク ---")
check("目標が保有以下なら足さない",
      call(target_total_qty=2)["reason"] == "NO_ROOM")
check("平均建値は枚数加重(2@29484.25 + 6@29448.5 = 29457.4375)",
      abs(res["combinedEntry"] - 29457.4375) < 1e-9, str(res.get("combinedEntry")))
check("合計リスクは合計建玉 × SL までの距離 × 点価値",
      abs(res["combinedRisk"] - abs(29507.5 - 29457.4375) * 8 * PV) < 1e-6,
      str(res.get("combinedRisk")))
tight = call(risk_cap_dollars=400.0)
check("上限に入らなければ枚数を刻んで落とす",
      tight["add"] is True and tight["qty"] < 6 and tight["clamped"] is True, str(tight))
check("刻んだ後も合計リスクは上限以内",
      tight["combinedRisk"] <= 400.0 + 1e-9, str(tight.get("combinedRisk")))
check("1 枚も入らなければ追撃しない",
      call(risk_cap_dollars=1.0)["reason"] == "RISK_CAP_EXCEEDED")
check("上限が読めなければ追撃しない",
      call(risk_cap_dollars=None)["reason"] == "RISK_CAP_UNAVAILABLE")
check("建玉の平均建値が取れなければ追撃しない",
      call(position={**POS, "avgEntry": None})["reason"] == "POSITION_ENTRY_UNAVAILABLE")
check("目標枚数が出せなければ追撃しない",
      call(target_total_qty=None)["reason"] == "TARGET_QTY_NOT_COMPUTABLE")

print()
print("--- 幾何の正気チェック ---")
check("SELL で SL が現在値以下なら追撃しない(入った瞬間に負けている)",
      call(stop=29400.0)["reason"] == "STOP_ON_WRONG_SIDE")
buy_plan = {"side": "BUY", "qty": 2}
buy_pos = {"side": "LONG", "qty": 2, "avgEntry": 29400.0}
check("BUY で SL が現在値以上なら追撃しない",
      call(owned_plan=buy_plan, position=buy_pos,
           scenario={**SIGNAL, "side": "BUY"}, add_price=29420.0,
           stop=29450.0)["reason"] == "STOP_ON_WRONG_SIDE")
check("BUY 側も同じ式で追撃できる",
      call(owned_plan=buy_plan, position=buy_pos,
           scenario={**SIGNAL, "side": "BUY"}, add_price=29420.0,
           stop=29380.0)["add"] is True)

print()
print("--- 発注しない(純関数) ---")
check("evaluate は dict を返すだけで副作用が無い",
      isinstance(res, dict) and set(res) >= {"add", "qty", "reason"})

print()
print(f"合計: PASS {PASS[0]} / FAIL {FAIL[0]}")
sys.exit(1 if FAIL[0] else 0)

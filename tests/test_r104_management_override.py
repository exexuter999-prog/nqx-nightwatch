# -*- coding: utf-8 -*-
"""R104: 決定 ID 単位の管理上書き(finalTarget / trailMode=BREAKEVEN_ONLY)。

2026-09-16 01:34 ユーザー指示(SHORT 6 @29,284.5、TP1 29,231.25、凍結 runner TP 29,215.75、
trailDistance 22.5): 「TP2 を 29,137 に変更、TP1 後はトレールに掛かりたくないので BE のみ」。
凍結プランは台帳の行なので書き換えない。`.secrets/management_override.json` を
`_frozen_plan_record()` の出口で乗せる。

固定する線引き:
  * finalTarget は MODIFY の take_profit と「最終 TP 到達で FLATTEN」の両方を差し替える
  * TP1 を越えていない finalTarget は無視(trailMode だけ乗る)
  * BREAKEVEN_ONLY は床(建値±1pt)にだけ寄せ、極値−距離のトレールを出さない
  * 床へ寄せた後は improved が立たず見送り(トレールが二度と出ない)
  * 上書きが無い / decisionId 不一致なら同じオブジェクトを返す(台帳の行は不変)
  * ファイルが無い・壊れている・schema 違いは「上書きなし」(周期を止めない)
"""
import json
import os
import sys
import tempfile
from datetime import datetime, timezone

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import autotrade_engine as ae  # noqa: E402


def check(label, condition, detail=""):
    if not condition:
        raise AssertionError(f"{label}: {detail}")
    print(f"OK   {label}")


FILLED_AT = datetime(2026, 9, 15, 15, 17, 46, tzinfo=timezone.utc)
CFG = {"NQX_AUTOTRADE_KILL": "0"}


def plan(**extra):
    base = {"decisionId": "4dba96c101c19fb9", "scenarioId": "4dba96c101c19fb9", "symbol": "MNQZ6",
            "side": "SELL", "qty": 6, "entry": 29284.5, "initialStop": 29314.5,
            "tp1": 29231.25, "finalTarget": 29215.75, "targets": [29231.25, 29215.75],
            "legs": [{"id": "TP1", "qty": 3, "target": 29231.25},
                     {"id": "RUNNER", "qty": 3, "target": 29215.75}],
            "trailDistance": 22.5, "legIdentityKnown": True}
    base.update(extra)
    return base


def position(qty=3):
    return {"side": "SHORT", "qty": qty, "avgEntry": 29284.5, "accountId": "LFF00000000000006",
            "symbol": "MNQZ6", "filledAt": FILLED_AT.isoformat()}


def bundle(price, low):
    t0 = int(FILLED_AT.timestamp()) + 60
    return {"price": price, "snapshot": {"bars3m": [
        {"t": t0, "o": price, "h": price + 5, "l": low, "c": price},
        {"t": t0 + 180, "o": price, "h": price + 2, "l": price - 2, "c": price},
    ]}}


OVERRIDE = {"4dba96c101c19fb9": {"decisionId": "4dba96c101c19fb9", "finalTarget": 29137.0,
                                 "trailMode": "BREAKEVEN_ONLY"}}

# 1. apply_management_override: finalTarget が finalTarget / targets[-1] / RUNNER 脚に乗る
original = plan()
snapshot = json.dumps(original, sort_keys=True)
updated = ae.apply_management_override(original, OVERRIDE)
check("上書きはコピーへ乗り、元のプランは不変", updated is not original and json.dumps(original, sort_keys=True) == snapshot)
check("finalTarget が 29,137 へ", updated["finalTarget"] == 29137.0, updated.get("finalTarget"))
check("targets[-1] も 29,137(TP1 は不変)", updated["targets"] == [29231.25, 29137.0], updated.get("targets"))
check("RUNNER 脚の target も 29,137(TP1 脚は不変)",
      updated["legs"][1]["target"] == 29137.0 and updated["legs"][0]["target"] == 29231.25, updated.get("legs"))
check("trailMode=BREAKEVEN_ONLY が乗る", updated.get("trailMode") == "BREAKEVEN_ONLY", updated.get("trailMode"))
check("乗せた内容が managementOverride に残る",
      updated.get("managementOverride") == {"finalTarget": 29137.0, "trailMode": "BREAKEVEN_ONLY"},
      updated.get("managementOverride"))

# 2. decisionId 不一致 / 上書き無し → 同じオブジェクト
check("decisionId 不一致は同じオブジェクト", ae.apply_management_override(plan(decisionId="other"), OVERRIDE) is original or
      ae.apply_management_override(plan(decisionId="other"), OVERRIDE)["decisionId"] == "other")
same = plan()
check("上書き表が空なら同じオブジェクト", ae.apply_management_override(same, {}) is same)

# 3. TP1 を越えていない finalTarget は無視(SELL: tp1 29,231.25 より上は不可)
bad = ae.apply_management_override(plan(), {"4dba96c101c19fb9": {"decisionId": "4dba96c101c19fb9",
                                                                  "finalTarget": 29240.0,
                                                                  "trailMode": "BREAKEVEN_ONLY"}})
check("TP1 を越えない finalTarget は無視され、凍結値 29,215.75 のまま",
      bad["finalTarget"] == 29215.75 and bad["targets"][-1] == 29215.75 and bad["legs"][1]["target"] == 29215.75, bad)
check("無視した値は finalTargetIgnored として残り、trailMode だけ乗る",
      bad["managementOverride"] == {"finalTargetIgnored": 29240.0, "trailMode": "BREAKEVEN_ONLY"}, bad.get("managementOverride"))
buy_ok = ae.apply_management_override(plan(side="BUY", tp1=29300.0, finalTarget=29350.0, targets=[29300.0, 29350.0]),
                                      {"4dba96c101c19fb9": {"decisionId": "4dba96c101c19fb9", "finalTarget": 29400.0}})
check("BUY は TP1 より上なら採用", buy_ok["finalTarget"] == 29400.0 and "trailMode" not in buy_ok, buy_ok)

# 4. management_action: TP1 後(runner 3 枚)、安値 29,225 / 現在値 29,240
default_act = ae.management_action(plan(), position(), bundle(29240.0, 29225.0), {}, CFG)
check("既定はトレール: min(床 29,283.5, 29,225+22.5=29,247.5) → 29,247.5 / tp 29,215.75",
      default_act and default_act["action"] == "MODIFY" and default_act["sl"] == 29247.5 and default_act["tp"] == 29215.75,
      default_act)
be_act = ae.management_action(updated, position(), bundle(29240.0, 29225.0), {}, CFG)
check("BREAKEVEN_ONLY は床 29,283.5 へ、tp は上書きの 29,137",
      be_act and be_act["action"] == "MODIFY" and be_act["sl"] == 29283.5 and be_act["tp"] == 29137.0 and be_act["qty"] == 3,
      be_act)
check("理由に override が残る", "override" in be_act["reason"], be_act["reason"])

# 5. 床へ寄せた後は二度と出ない(既定なら 29,220+22.5=29,242.5 へトレールする局面。
#    価格は凍結 finalTarget 29,215.75 より上に置き、既定側が FLATTEN にならないようにする)
after = ae.management_action(updated, position(), bundle(29225.0, 29220.0), {"stop": 29283.5}, CFG)
check("BREAKEVEN_ONLY: 床へ寄せ済みなら見送り(トレールしない)", after is None, after)
trail = ae.management_action(plan(), position(), bundle(29225.0, 29220.0), {"stop": 29283.5}, CFG)
check("既定はその局面で 29,242.5 へトレールする(対照)",
      trail and trail.get("action") == "MODIFY" and trail.get("sl") == 29242.5, trail)
deeper = ae.management_action(updated, position(), bundle(29180.0, 29170.0), {"stop": 29283.5}, CFG)
check("BREAKEVEN_ONLY: 29,215.75 を割っても FLATTEN もトレールも出ない", deeper is None, deeper)

# 6. 床が現在値の逆側(価格が建値−1pt より上へ戻した)なら見送り。逆側 SL は作らない
check("床が逆側なら見送り", ae.management_action(updated, position(), bundle(29290.0, 29225.0), {}, CFG) is None)

# 7. 最終 TP 到達 FLATTEN も上書き値を見る
check("凍結値 29,215.75 到達なら既定は FLATTEN",
      ae.management_action(plan(), position(), bundle(29210.0, 29205.0), {"stop": 29283.5}, CFG)["action"] == "FLATTEN")
not_yet = ae.management_action(updated, position(), bundle(29210.0, 29205.0), {"stop": 29283.5}, CFG)
check("上書き後は 29,215.75 では FLATTEN しない", not (not_yet and not_yet["action"] == "FLATTEN"), not_yet)
check("29,137 到達で FLATTEN",
      ae.management_action(updated, position(), bundle(29136.0, 29130.0), {"stop": 29283.5}, CFG)["action"] == "FLATTEN")

# 8. 全量(6 枚)が残る間は触らない(従来どおり)
check("TP1 前(6 枚)は OCO を触らない", ae.management_action(updated, position(qty=6), bundle(29240.0, 29225.0), {}, CFG) is None)

# 9. _frozen_plan_record の出口で乗る(台帳の行は不変)。ファイル欠落・壊れ・schema 違いは上書きなし
records = [{"status": "ENTRY_SENT", "action": "ENTRY_OWNERSHIP_BOUND", "plan": plan()}]
tmp = tempfile.mkdtemp(prefix="nqx-r103-")
path = os.path.join(tmp, "management_override.json")
saved = ae.MANAGEMENT_OVERRIDE_FILE
try:
    ae.MANAGEMENT_OVERRIDE_FILE = path
    check("ファイル無しは上書きなし", ae._frozen_plan(records, "MNQZ6")["finalTarget"] == 29215.75)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("{ broken")
    check("壊れたファイルは上書きなし", ae._frozen_plan(records, "MNQZ6")["finalTarget"] == 29215.75)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({"schema": "OTHER/1", "overrides": [OVERRIDE["4dba96c101c19fb9"]]}, handle)
    check("schema 違いは上書きなし", ae._frozen_plan(records, "MNQZ6")["finalTarget"] == 29215.75)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({"schema": "NQX_MANAGEMENT_OVERRIDE/1", "overrides": [OVERRIDE["4dba96c101c19fb9"]]}, handle)
    frozen = ae._frozen_plan(records, "MNQZ6")
    check("凍結プランに上書きが乗る", frozen["finalTarget"] == 29137.0 and frozen["trailMode"] == "BREAKEVEN_ONLY", frozen)
    check("台帳の行(元のプラン)は不変", records[0]["plan"]["finalTarget"] == 29215.75 and "trailMode" not in records[0]["plan"])
    rec = ae._frozen_plan_record(records, "MNQZ6")
    check("行の他のキーは保たれる", rec["status"] == "ENTRY_SENT" and rec["action"] == "ENTRY_OWNERSHIP_BOUND", rec)
finally:
    ae.MANAGEMENT_OVERRIDE_FILE = saved

print("ALL PASS (test_r104_management_override; external sends=0)")

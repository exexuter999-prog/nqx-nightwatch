# -*- coding: utf-8 -*-
"""R45: 口座別リスク上限は経路全体ではなく、その口座だけを落とす。

払えない口座があっても、払える口座の取引は生かす。全口座が払えないときだけ
経路ごと止める。外部送信: ゼロ（設定は全てインメモリの cfg）。
"""
import os
import sys
from datetime import datetime, timezone

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import execution_contract  # noqa: E402
import monitor_publish  # noqa: E402


def check(label, condition, detail=""):
    if not condition:
        raise AssertionError(f"{label}: {detail}")
    print(f"OK   {label}")


NOW = datetime.now(timezone.utc)
# LTA 2口座は $240、LFF は $100。2枚固定・$2/pt なので SL 上限は 60pt / 60pt / 25pt。
ENV = {"CROSSTRADE_ACCOUNTS": "LTA-A,LTA-B,LFF-C",
       "RISK_LTA-A": "240", "RISK_LTA-B": "240", "RISK_LFF-C": "100"}


def scenario(entry, stop, state="ARMED"):
    return {"grade": "A", "state": state, "side": "BUY", "qty": 2,
            "entry": entry, "stop": stop,
            "targets": [entry + 40, entry + 80],
            "legs": [{"id": "TP1", "qty": 1, "target": entry + 40},
                     {"id": "RUNNER", "qty": 1, "target": entry + 80}],
            "planVersion": "R12", "at": NOW.isoformat()}


# --- affordable_accounts の分割そのもの -------------------------------------
affordable, excluded = execution_contract.affordable_accounts(ENV, 111.0)
check("狭い口座だけが除外される",
      affordable == ["LTA-A", "LTA-B"] and [row["account"] for row in excluded] == ["LFF-C"],
      f"{affordable} / {excluded}")

affordable, excluded = execution_contract.affordable_accounts(ENV, 100.0)
check("全口座が払えるなら誰も外さない",
      affordable == ["LTA-A", "LTA-B", "LFF-C"] and not excluded,
      f"{affordable} / {excluded}")

affordable, excluded = execution_contract.affordable_accounts(ENV, 260.0)
check("全口座が払えないなら affordable は空",
      not affordable and len(excluded) == 3, f"{affordable} / {excluded}")

affordable, excluded = execution_contract.affordable_accounts({}, 111.0)
check("口座未設定は「全口座が払えない」ではない",
      not affordable and not excluded, f"{affordable} / {excluded}")

# --- evaluate() の accountScope 絞り込み ------------------------------------
market = {"at": NOW.isoformat(), "price": 20000, "cvdAt": NOW.isoformat()}

# 27.75pt x 2 x $2 = $111 -> LFF だけ払えない
narrowed = execution_contract.evaluate(scenario(20000, 19972.25), market, cfg=ENV, now=NOW)
check("払えない口座は accountScope から外れる",
      narrowed["accountScope"] == ["LTA-A", "LTA-B"], narrowed["accountScope"])
check("外した口座は excludedAccounts に残る",
      [row["account"] for row in narrowed["excludedAccounts"]] == ["LFF-C"],
      narrowed["excludedAccounts"])
check("上限は残った口座の最も厳しい値へ張り替わる",
      narrowed["riskCapDollars"] == 240.0, narrowed["riskCapDollars"])
check("1口座が払えないだけでは RISK_CAP_EXCEEDED にしない",
      "RISK_CAP_EXCEEDED" not in narrowed["blockers"], narrowed["blockers"])

# 25pt x 2 x $2 = $100 -> 全口座が払える
full = execution_contract.evaluate(scenario(20000, 19975), market, cfg=ENV, now=NOW)
check("全口座が払えるなら scope は変わらない",
      full["accountScope"] == ["LFF-C", "LTA-A", "LTA-B"] and not full["excludedAccounts"],
      f"{full['accountScope']} / {full['excludedAccounts']}")
check("その場合の上限は最も厳しい口座のまま",
      full["riskCapDollars"] == 100.0, full["riskCapDollars"])

# 65pt x 2 x $2 = $260 -> どの口座も払えない
blocked = execution_contract.evaluate(scenario(20000, 19935), market, cfg=ENV, now=NOW)
check("全口座が払えないときだけ RISK_CAP_EXCEEDED",
      "RISK_CAP_EXCEEDED" in blocked["blockers"], blocked["blockers"])

# 口座設定が無い経路は従来どおり既定上限との単純比較へ落ちる。
# ここを取り違えると、口座を持たないテスト/縮退経路が丸ごと停止する。
no_accounts = execution_contract.evaluate(scenario(20000, 19980), market, cfg={}, now=NOW)
check("口座未設定でも既定上限内なら止めない",
      "RISK_CAP_EXCEEDED" not in no_accounts["blockers"], no_accounts["blockers"])

# 凍結上限は絞り込みの後でも「狭い側だけ」効く。
frozen = dict(scenario(20000, 19972.25))
frozen["executionContract"] = {"riskCapDollars": 105.0, "riskCapSource": "frozen"}
frozen_out = execution_contract.evaluate(frozen, market, cfg=ENV, now=NOW)
check("凍結上限は絞り込み後も上限として効く",
      frozen_out["riskCapDollars"] == 105.0
      and "RISK_CAP_EXCEEDED" in frozen_out["blockers"],
      f"{frozen_out['riskCapDollars']} / {frozen_out['blockers']}")

# --- monitor_publish 側の表示/降格 -----------------------------------------
out, notes = monitor_publish.normalize_qty(scenario(20000, 19972.25), env=ENV)
check("1口座だけ超過なら ARMED のまま",
      out["state"] == "ARMED", out["state"])
check("除外はサイクル注記に残る",
      any("dropped from the route" in note and "LFF-C" in note for note in notes), notes)

out, notes = monitor_publish.normalize_qty(scenario(20000, 19975), env=ENV)
check("全口座が払えるなら注記なしで ARMED",
      out["state"] == "ARMED" and not any("cap" in note for note in notes),
      f"{out['state']} / {notes}")

out, notes = monitor_publish.normalize_qty(scenario(20000, 19935), env=ENV)
check("全口座が払えないときだけ WATCH へ降格",
      out["state"] == "WATCH"
      and any("demoted to WATCH" in note for note in notes),
      f"{out['state']} / {notes}")

print("ALL PASS (test_r45_account_exclusion; external sends=0)")

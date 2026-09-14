# -*- coding: utf-8 -*-
"""R84: 追撃 HALT の回復と遷移回廊(§9 / §4.3)。

既存の ENTRY 回復は **建玉ゼロ** を前提にしているので、保有中に起きる追撃の HALT に
は原理的に通らない。ここでは追撃専用の不在の証明と、証明が作れない間も
**FLATTEN と KILL は生きている** ことを固定する。

    python tests/test_r84_recovery.py
"""
import copy
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _r84_fixtures as fx  # noqa: E402

sys.path.insert(0, fx.BASE)
import autotrade_engine as ae  # noqa: E402

ae._read_env_file = lambda path=None: {}
import execution_contract  # noqa: E402
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


BASE_T1 = fx.tranche_one()
BASE_ROWS = []
for item in BASE_T1["legs"]:
    BASE_ROWS += fx.bracket_rows(item["orderId"], item["bracketOrderIds"])
PRE_SEND_IDS = sorted(row["orderId"] for row in BASE_ROWS)
BASE_POSITION = fx.position(qty=4, avg_entry=29484.25)
CLAIMED = {"key": "ENTRY:" + "9" * 64, "entryKey": "ENTRY:" + "9" * 64,
           "status": ae.PYRAMID_CLAIMED, "action": "PYRAMID_ENTRY",
           "accountId": fx.ACC, "symbol": fx.SYM,
           "baseQty": 4, "baseAvgEntry": 29484.25, "addQty": 4, "addPrice": 29448.5,
           "preSendOrderIds": PRE_SEND_IDS}

print("--- 不在の証明(§9) ---")
ok, detail = ae._pyramid_absence_proof(CLAIMED, BASE_POSITION, fx.orders(BASE_ROWS))
check("建玉が増えず、送信前の集合の外に行が無ければ成立", ok is True, detail)

grown = fx.position(qty=8, avg_entry=29466.375)
ok2, detail2 = ae._pyramid_absence_proof(CLAIMED, grown, fx.orders(BASE_ROWS))
check("建玉が増えていれば成立しない(部分的に入っている可能性)",
      ok2 is False and "POSITION_CHANGED" in detail2, detail2)

new_rows = BASE_ROWS + fx.bracket_rows("T2-TP1", ["T2-TP1-A", "T2-TP1-B"])
ok3, detail3 = ae._pyramid_absence_proof(CLAIMED, BASE_POSITION, fx.orders(new_rows))
check("知らない未終端の行があれば成立しない",
      ok3 is False and "NEW_ORDER_ROWS" in detail3, detail3)

terminal_rows = [dict(row, status="CANCELED") for row in new_rows if row["orderId"] not in PRE_SEND_IDS]
ok4, _ = ae._pyramid_absence_proof(CLAIMED, BASE_POSITION,
                                   fx.orders(BASE_ROWS + terminal_rows))
check("終端の行は不在の証明を妨げない", ok4 is True)

ok5, detail5 = ae._pyramid_absence_proof(CLAIMED, BASE_POSITION, {"verified": False})
check("注文照会が UNVERIFIED なら成立しない",
      ok5 is False and "ORDERS_UNVERIFIED" in detail5, detail5)
ok6, detail6 = ae._pyramid_absence_proof(CLAIMED, fx.position(qty=4, verified=False),
                                         fx.orders(BASE_ROWS))
check("建玉照会が UNVERIFIED なら成立しない",
      ok6 is False and "POSITION_UNVERIFIED" in detail6, detail6)
ok7, detail7 = ae._pyramid_absence_proof({**CLAIMED, "preSendOrderIds": []},
                                         BASE_POSITION, fx.orders(BASE_ROWS))
check("送信前スナップショットが無ければ成立しない(推測で解かない)",
      ok7 is False and "PRESEND_SNAPSHOT_MISSING" in detail7, detail7)

print()
print("--- HALT 中でも回廊と撤退は生きる ---")
BASE_PLAN = {
    "planKind": tranche.PLAN_KIND, "planVersion": "R19-ICT-SPLIT-1",
    "entryKey": "ENTRY:" + "1" * 64, "scenarioId": "S-T1", "symbol": fx.SYM,
    "accountScope": [fx.ACC], "side": "SELL", "qty": 4, "entry": 29484.25,
    "entryOrderType": "MARKET", "entryReference": 29484.25, "initialStop": 29520.0,
    "tp1": 29440.0, "finalTarget": 29380.0, "targets": [29440.0, 29380.0],
    "legs": [{"id": "TP1", "qty": 2, "target": 29440.0, "trancheId": "T1"},
             {"id": "RUNNER", "qty": 2, "target": 29380.0, "trancheId": "T1"}],
    "trailDistance": 18.75, "mode": "SPLIT_BRACKETS_TP1_RUNNER", "ultra": True,
    "ultraDrawdown": 3100.0, "riskCapDollars": 3100.0,
    "riskCapSource": "ACCOUNT_DRAWDOWN_BUFFER",
    "pyramid": {"addsDone": 0, "maxAdds": 2, "combinedEntry": 29484.25,
                "combinedRisk": None, "governingStop": 29520.0,
                "flattenBeyond": 29380.0},
    "tranches": [BASE_T1], "consolidation": None,
}
partial = fx.position(qty=6, avg_entry=29472.5)
corridor = tranche.bind_composite(BASE_PLAN, partial, fx.orders(BASE_ROWS),
                                  position_generation=fx.generation(partial),
                                  corridor_add_qty=4)
check("回廊中(6 枚)は所有される", corridor["owned"] is True
      and corridor["state"] == "PYRAMID_TRANSIT", str(corridor.get("reason")))
check("回廊中の MODIFY は出ない",
      tranche.management_action_composite(BASE_PLAN, partial, price=29450.0,
                                          states=corridor["legStates"],
                                          transit=True) is None)
killed = tranche.management_action_composite(BASE_PLAN, partial, price=29450.0,
                                             states=corridor["legStates"],
                                             transit=True, kill=True)
check("KILL は回廊中でも FLATTEN を出す", (killed or {}).get("action") == "FLATTEN")
breached = tranche.management_action_composite(BASE_PLAN, partial, price=29525.0,
                                               states=corridor["legStates"],
                                               transit=True)
check("構造 SL を割れば回廊中でも FLATTEN",
      (breached or {}).get("action") == "FLATTEN", str(breached))

print()
print("--- 次サイクル: 不在が証明できれば WAL を閉じて基礎プランへ戻る ---")
tmp = tempfile.mkdtemp(prefix="nqx-r84-rec-")
try:
    path = os.path.join(tmp, "r84.jsonl")
    ae._append_ledger(copy.deepcopy(CLAIMED) | {"prePlan": BASE_PLAN,
                                                "postPlanDraft": BASE_PLAN}, path)
    ae._append_ledger({"key": CLAIMED["key"], "status": "HALT",
                       "action": {"action": "PYRAMID_ENTRY", "qty": 4},
                       "reason": "pyramid add route UNKNOWN"}, path)
    records, error = ae._read_ledger(path)
    check("台帳は読める", error is None, str(error))
    wal = ae._pyramid_wal(records, fx.SYM, fx.ACC)
    check("WAL は開いている", wal is not None and wal["halt"] is not None)
    ctx = {"records": records, "cfg": {}, "symbol": fx.SYM, "account": fx.ACC,
           "ledger_path": path, "live": True, "execute": None,
           "query": None, "order_query": None, "generation": fx.generation(BASE_POSITION),
           "bundle": {"price": 29450.0}, "now": None, "livePrice": 29450.0}
    notes, resume = ae._pyramid_settle(ctx, wal, BASE_POSITION, fx.orders(BASE_ROWS))
    check("不在が証明できれば通常フローへ戻る", resume is True, str(notes))
    check("注記に回復が出る", any("recovered" in note for note in notes), str(notes))
    rows = [json.loads(line) for line in open(path, encoding="utf-8") if line.strip()]
    check("ENTRY_RECOVERED が同じキーで追記される",
          rows[-1]["status"] == "ENTRY_RECOVERED"
          and rows[-1]["key"] == CLAIMED["key"]
          and rows[-1]["action"] == ae.PYRAMID_RECOVERY, str(rows[-1]))
    check("HALT は解除される(台帳の行は消さない・書き換えない)",
          ae._has_halt(rows) is None)
    check("WAL も閉じる", ae._pyramid_wal(rows, fx.SYM, fx.ACC) is None)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print()
print("--- 証明できない間は自動で解かない ---")
tmp = tempfile.mkdtemp(prefix="nqx-r84-rec2-")
try:
    path = os.path.join(tmp, "r84.jsonl")
    ae._append_ledger(copy.deepcopy(CLAIMED) | {"prePlan": BASE_PLAN,
                                                "postPlanDraft": BASE_PLAN}, path)
    ae._append_ledger({"key": CLAIMED["key"], "status": "HALT",
                       "action": {"action": "PYRAMID_ENTRY", "qty": 4},
                       "reason": "pyramid add route UNKNOWN"}, path)
    records, _ = ae._read_ledger(path)
    wal = ae._pyramid_wal(records, fx.SYM, fx.ACC)
    ctx = {"records": records, "cfg": {}, "symbol": fx.SYM, "account": fx.ACC,
           "ledger_path": path, "live": True, "execute": None,
           "query": None, "order_query": None, "generation": fx.generation(partial),
           "bundle": {"price": 29450.0}, "now": None, "livePrice": 29450.0}
    notes, resume = ae._pyramid_settle(ctx, wal, partial, fx.orders(BASE_ROWS))
    check("枚数が増えていれば回復せず回廊のまま", resume is False, str(notes))
    rows = [json.loads(line) for line in open(path, encoding="utf-8") if line.strip()]
    check("ENTRY_RECOVERED は書かれない",
          not any(row["status"] == "ENTRY_RECOVERED" for row in rows))
    check("HALT は残る(人が建玉を照会して --clear-halt)",
          ae._has_halt(rows) is not None)
    check("注記は回廊にいることを示す",
          any("corridor" in note for note in notes), str(notes))
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print()
print("--- 送信 UNKNOWN の HALT は、コミットが成立したら解ける ---")
# 実害: 解けないと、回復して建玉を正しく管理していても以後の新規 FLAT ENTRY が
# 全部塞がる(_has_halt は FLAT 経路で効く)。R76 の rebound と同じ理屈で、
# コミット行が「送信は起きて構造で束縛できた」証明になる。
halt_row = {"key": CLAIMED["key"], "status": "HALT",
            "action": {"action": "PYRAMID_ENTRY", "qty": 4},
            "reason": "pyramid add route UNKNOWN"}
commit_row = {"key": CLAIMED["key"], "entryKey": CLAIMED["key"], "status": "ENTRY_SENT",
              "action": ae.PYRAMID_COMMIT, "accountId": fx.ACC, "symbol": fx.SYM,
              "plan": BASE_PLAN, "routeState": "UNKNOWN",
              "haltResolution": ae.HALT_RESOLUTION_PYRAMID_COMMIT}
check("HALT だけなら止まる", ae._has_halt([CLAIMED, halt_row]) is not None)
check("コミット行が後に来れば解ける",
      ae._has_halt([CLAIMED, halt_row, commit_row]) is None)
check("印の無いコミット行では解けない(退行検出)",
      ae._has_halt([CLAIMED, halt_row,
                    {k: v for k, v in commit_row.items() if k != "haltResolution"}])
      is not None)
check("engine のコミット行は実際にその印を書く",
      "HALT_RESOLUTION_PYRAMID_COMMIT" in open(
          os.path.join(fx.BASE, "autotrade_engine.py"), encoding="utf-8").read().split(
              "def _pyramid_commit")[1][:6000])

print()
print("--- WAL は口座で絞る(多口座で他口座の WAL を拾わない) ---")
other = dict(CLAIMED, key="ENTRY:" + "8" * 64, accountId="OTHER-ACC",
             prePlan=BASE_PLAN, postPlanDraft=BASE_PLAN)
mine = dict(CLAIMED, prePlan=BASE_PLAN, postPlanDraft=BASE_PLAN)
check("自分の口座の WAL は拾う",
      (ae._pyramid_wal([mine], fx.SYM, fx.ACC) or {}).get("entryKey") == CLAIMED["key"])
check("他口座の WAL は拾わない",
      ae._pyramid_wal([other], fx.SYM, fx.ACC) is None)
check("並んでいても自分のだけを拾う",
      (ae._pyramid_wal([mine, other], fx.SYM, fx.ACC) or {}).get("entryKey")
      == CLAIMED["key"])
check("別銘柄の WAL も拾わない",
      ae._pyramid_wal([dict(mine, prePlan=dict(BASE_PLAN, symbol="MNQZ6"))],
                      fx.SYM, fx.ACC) is None)

print()
print("--- 追撃の注記は R46 の手動建玉判定を壊さない ---")
manual = "autotrade hold: broker position ownership UNKNOWN " \
         "(broker position account is outside frozen scope)"
check("従来どおり手動と判定される", ae._is_manual_position_account([manual]) is True)
check("追撃の注記が混ざっても判定は変わらない",
      ae._is_manual_position_account([manual, "pyramid: SIDE_MISMATCH"]) is True)
check("ガードの注記でも変わらない",
      ae._is_manual_position_account(
          [manual, "pyramid guard: wal-scan failed (RuntimeError: x)"]) is True)
check("追撃以外の理由が混ざれば従来どおり False",
      ae._is_manual_position_account([manual, "AUTOTRADE HALT: something"]) is False)
check("追撃の注記だけでは手動と断定しない",
      ae._is_manual_position_account(["pyramid: SIDE_MISMATCH"]) is False)

print()
print(f"合計: PASS {PASS[0]} / FAIL {FAIL[0]}")
sys.exit(1 if FAIL[0] else 0)

# -*- coding: utf-8 -*-
"""R52: 台帳 HALT の明示解除(HALT_CLEARED)。

KILL/FLATTEN の HALT(送信結果不明)は同じ key の ENTRY_RECOVERED が原理的に書かれず、
人が建玉を確認するまで新規を止め続ける。2026-09-04 に口座を入れ替えた日、09-01 の
KILL HALT 3 件が新口座の新規 ENTRY を塞いでいた。解除は台帳を編集せず、理由と根拠を
持つ 1 行を追記する。

    python tests/test_r52_halt_clear.py
"""
import os
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
import autotrade_engine as ae  # noqa: E402

PASS = [0]
FAIL = [0]


def check(label, condition, detail=""):
    if condition:
        PASS[0] += 1
        print(f"  OK   {label}")
    else:
        FAIL[0] += 1
        print(f"  NG   {label}  {detail}")


with tempfile.TemporaryDirectory() as tmp:
    ledger = os.path.join(tmp, "ledger.jsonl")
    K1 = "KILL:PG:1:POS:aaaa:FLATTEN"
    K2 = "KILL:FLAT:MNQU6:PENDING:FLATTEN"
    ae._append_ledger({"key": K1, "status": "HALT", "action": "FLATTEN",
                       "reason": "live send failed/unknown: A"}, ledger)
    ae._append_ledger({"key": K2, "status": "HALT", "action": "FLATTEN",
                       "reason": "live send failed/unknown: B"}, ledger)

    records, _ = ae._read_ledger(ledger)
    check("最新の HALT が新規を塞ぐ", (ae._has_halt(records) or {}).get("key") == K2)

    ok, detail = ae.clear_halt(K2, "", ledger_path=ledger)
    check("理由なしは拒否", not ok and detail["reason"] == "HALT_CLEAR_REASON_REQUIRED")
    ok, detail = ae.clear_halt("KILL:nope", "x", ledger_path=ledger)
    check("存在しない key は拒否", not ok and detail["reason"] == "HALT_NOT_FOUND")

    ok, detail = ae.clear_halt(K2, "broker verified FLAT", evidence="qty=0",
                               operator="test", ledger_path=ledger)
    check("解除は HALT_CLEARED を追記", ok and detail["status"] == "HALT_CLEARED"
          and detail["evidence"] == "qty=0" and detail["haltAction"] == "FLATTEN")
    records, _ = ae._read_ledger(ledger)
    check("解除後は次に古い HALT が塞ぐ", (ae._has_halt(records) or {}).get("key") == K1)
    check("台帳の行は消えていない(追記のみ)",
          sum(1 for r in records if r["status"] == "HALT") == 2)

    ok, detail = ae.clear_halt(K2, "again", ledger_path=ledger)
    check("二重解除は拒否", not ok and detail["reason"] == "HALT_ALREADY_CLEARED")

    ok, _ = ae.clear_halt(K1, "broker verified FLAT", ledger_path=ledger)
    records, _ = ae._read_ledger(ledger)
    check("全部解けば HALT なし", ok and ae._has_halt(records) is None)

    ae._append_ledger({"key": K1, "status": "HALT", "action": "FLATTEN", "reason": "again"}, ledger)
    records, _ = ae._read_ledger(ledger)
    check("解除後に同じ key で再 HALT すれば再び塞ぐ",
          (ae._has_halt(records) or {}).get("key") == K1)

    rows = ae.list_halts(ledger)
    check("list_halts は新しい順・解除済みフラグ付き",
          [r["cleared"] for r in rows] == [False, True, True], str(rows))

    # ENTRY_RECOVERED(engine の自動回復)は従来どおり同じ key の HALT だけを解く
    ae._append_ledger({"key": "ENTRY:x", "status": "HALT", "action": "ENTRY", "reason": "crash"}, ledger)
    ae._append_ledger({"key": "ENTRY:x", "status": "ENTRY_RECOVERED", "action": "ENTRY_RECOVERY"}, ledger)
    records, _ = ae._read_ledger(ledger)
    check("ENTRY_RECOVERED は自分の key だけ解く(K1 の再 HALT は残る)",
          (ae._has_halt(records) or {}).get("key") == K1)

    # CLI: 解除は理由必須、二重解除は非0
    rc = ae._cli(["--clear-halt", K1, "--ledger", ledger])
    check("CLI: 理由なしは非0", rc == 1)
    rc = ae._cli(["--clear-halt", K1, "--reason", "verified", "--ledger", ledger])
    check("CLI: 解除は 0", rc == 0)
    rc = ae._cli(["--list-halts", "--ledger", ledger])
    check("CLI: 一覧は 0", rc == 0)

# 管理系(MODIFY/FLATTEN の action dict)HALT は、建玉が閉じた後は新規を塞がない
with tempfile.TemporaryDirectory() as tmp:
    ledger = os.path.join(tmp, "mgmt.jsonl")
    MK = "ENTRY:" + "f" * 64 + ":ACC:MODIFY:29500.25:6"
    ae._append_ledger({"key": MK, "status": "HALT", "action": {"action": "MODIFY", "qty": 6, "sl": 29500.25},
                       "reason": "live send failed/unknown: MODIFY_ROUTE_UNKNOWN"}, ledger)
    records, _ = ae._read_ledger(ledger)
    check("建玉が生きている間は管理系 HALT も新規を塞ぐ", (ae._has_halt(records) or {}).get("key") == MK)
    ae._append_ledger({"key": "POSITION_GENERATION:3:FLAT", "status": "POSITION_GENERATION",
                       "generation": 3, "open": False, "accountId": "ACC"}, ledger)
    records, _ = ae._read_ledger(ledger)
    check("建玉が閉じた後は管理系 HALT は失効", ae._has_halt(records) is None)
    check("list_halts でも cleared 扱い", ae.list_halts(ledger)[0]["cleared"] is True)
    ae._append_ledger({"key": MK + ":2", "status": "HALT", "action": {"action": "FLATTEN", "qty": 6},
                       "reason": "flatten unverified"}, ledger)
    records, _ = ae._read_ledger(ledger)
    check("FLAT 記録より新しい管理系 HALT は塞ぐ", (ae._has_halt(records) or {}).get("key") == MK + ":2")
    ae._append_ledger({"key": "KILL:PG:9:POS:x:FLATTEN", "status": "HALT", "action": "FLATTEN",
                       "reason": "kill unverified"}, ledger)
    ae._append_ledger({"key": "POSITION_GENERATION:4:FLAT", "status": "POSITION_GENERATION",
                       "generation": 4, "open": False, "accountId": "ACC"}, ledger)
    records, _ = ae._read_ledger(ledger)
    check("KILL の FLATTEN HALT(action が文字列)は FLAT 後も塞ぐ(従来どおり人が解く)",
          (ae._has_halt(records) or {}).get("key") == "KILL:PG:9:POS:x:FLATTEN")

print()
print(f"合計: PASS {PASS[0]} / FAIL {FAIL[0]}")
sys.exit(1 if FAIL[0] else 0)

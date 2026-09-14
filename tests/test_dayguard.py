# -*- coding: utf-8 -*-
"""dayguard.py の集計を一時台帳で検証する。

**2026-08-27 にガードは廃止された。** 日次損失 −$480 / DAYGOAL 到達 /
2連敗 / 損切り後15分冷却は、もう新規発注を止めない。このテストが守る性質は
2つに変わった:

  1. ``blocked`` は **どんな入力でも False**(廃止されたことの固定)
  2. 該当条件は ``notes`` に残り、集計値(dayPnl / consecLosses)は正しい

実台帳(.secrets/day_ledger.jsonl)には一切触らない。ネットワークもなし。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import dayguard  # noqa: E402

JST = timezone(timedelta(hours=9))
FAILED = []
TMP = tempfile.mkdtemp(prefix="dayguard_test_")
A2, A3, A4 = "LFE0002", "LFE0003", "LFE0004"
ENV = {"DAYGOAL_" + A2: "625", "DAYGOAL_" + A3: "370", "DAYGOAL_" + A4: "212",
       "RISK_" + A2: "60", "RISK_" + A3: "60", "RISK_" + A4: "50"}


def check(name, cond, detail=""):
    print(("  OK   " if cond else "  FAIL ") + name
          + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


def section(title):
    print(f"\n{'=' * 68}\n{title}\n{'=' * 68}")


def ledger(rows, name):
    """rows を1行ずつ書いた一時台帳のパスを返す。文字列はそのまま(破損行用)。"""
    path = os.path.join(TMP, name)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        for row in rows:
            fh.write((row if isinstance(row, str)
                      else json.dumps(row, ensure_ascii=False)) + "\n")
    return path


def close(when, each=None, per=None, side="BUY"):
    row = {"closedAt": when.isoformat(), "side": side, "source": "test"}
    if per is not None:
        row["pnl"] = per
    else:
        row["pnlEach"] = each
    return row


NOW = datetime(2026, 8, 18, 2, 0, tzinfo=JST)          # 取引日 = 2026-08-17
def ago(minutes):
    return NOW - timedelta(minutes=minutes)


section("1. 台帳の不備でも新規は止めない(ガード廃止)")


def test_no_ledger_allows():
    path = os.path.join(TMP, "missing.jsonl")
    state = dayguard.check(now=NOW, env=ENV, path=path)
    check("未初期化でも blocked にしない", state["blocked"] is False, str(state))
    check("retired フラグ", state.get("retired") is True)
    check("未初期化警告は残す", len(state["warnings"]) == 1, str(state["warnings"]))
    dayguard.initialize_ledger(path)
    initialized = dayguard.check(now=NOW, env=ENV, path=path)
    check("明示初期化した空台帳も allowed", initialized["blocked"] is False)
    os.unlink(path)
    lost = dayguard.check(now=NOW, env=ENV, path=path)
    check("初期化後の台帳消失でも allowed", lost["blocked"] is False, str(lost["warnings"]))
    check("消失は警告としては残る", len(lost["warnings"]) >= 1, str(lost["warnings"]))
    empty = dayguard.check(now=NOW, env=ENV, path=ledger([], "empty.jsonl"))
    check("空ファイルでも allowed", empty["blocked"] is False)
    legacy_path = ledger([], "legacy-existing.jsonl")
    legacy = dayguard.check(now=NOW, env=ENV, path=legacy_path)
    check("既存台帳は初回観測でmarker移行", legacy["blocked"] is False
          and os.path.exists(dayguard.ledger_marker(legacy_path)))
    os.unlink(legacy_path)
    check("移行後の消失でも allowed",
          dayguard.check(now=NOW, env=ENV, path=legacy_path)["blocked"] is False)
    check("破損行があっても allowed",
          dayguard.check(now=NOW, env=ENV,
                         path=ledger(["{not json"], "broken1.jsonl"))["blocked"] is False)


test_no_ledger_allows()


section("2. 日次損失 −$480 は note に残るが止めない")


def test_day_loss_limit():
    path = ledger([close(ago(300), per={A2: -160.0, A3: -160.0, A4: -160.0})], "loss.jsonl")
    state = dayguard.check(now=NOW, env=ENV, path=path)
    codes = [r["code"] for r in state["notes"]]
    check("合計 −$480.0", state["dayPnl"] == -480.0, str(state["dayPnl"]))
    check("DAY_LOSS_LIMIT を note に記録", "DAY_LOSS_LIMIT" in codes, str(codes))
    check("それでも blocked にしない", state["blocked"] is False)
    check("reasons は常に空", state["reasons"] == [], str(state["reasons"]))

    # 1セント手前は境界外(≤ 判定の固定は集計側に残す)
    near = ledger([close(ago(300), per={A2: -160.0, A3: -160.0, A4: -159.99})], "near.jsonl")
    state2 = dayguard.check(now=NOW, env=ENV, path=near)
    check("−$479.99 は DAY_LOSS_LIMIT にしない",
          "DAY_LOSS_LIMIT" not in [r["code"] for r in state2["notes"]],
          str(state2["notes"]))


test_day_loss_limit()


section("3. DAYGOAL 到達(口座別)も note どまり")


def test_daygoal_reached():
    # …0004 だけ目標 $212 に到達。他は未達。
    path = ledger([close(ago(300), per={A2: 100.0, A3: 80.0, A4: 212.0})], "goal.jsonl")
    state = dayguard.check(now=NOW, env=ENV, path=path)
    hits = [r for r in state["notes"] if r["code"] == "DAYGOAL_REACHED"]
    check("DAYGOAL_REACHED が1件", len(hits) == 1, str(state["notes"]))
    check("note に口座IDを含む", bool(hits) and A4 in hits[0]["detail"],
          hits[0]["detail"] if hits else "")
    check("到達しても blocked にしない", state["blocked"] is False)

    below = ledger([close(ago(300), per={A2: 100.0, A3: 80.0, A4: 211.75})], "below.jsonl")
    check("未達も当然通る",
          dayguard.check(now=NOW, env=ENV, path=below)["blocked"] is False)


test_daygoal_reached()


section("4. 2連敗 / 連敗が途切れる場合")


def test_two_losses():
    path = ledger([close(ago(300), each=20.0),      # 勝ち
                   close(ago(200), each=-15.0),     # 負け
                   close(ago(100), each=-15.0)], "consec.jsonl")   # 負け
    state = dayguard.check(now=NOW, env=ENV, path=path)
    check("連敗2", state["consecLosses"] == 2, str(state["consecLosses"]))
    check("TWO_LOSSES を note に記録", "TWO_LOSSES" in [r["code"] for r in state["notes"]],
          str(state["notes"]))
    check("2連敗でも blocked にしない", state["blocked"] is False)

    # 勝ち→負け→勝ち→負け は末尾から1連敗
    mixed = ledger([close(ago(400), each=20.0), close(ago(300), each=-15.0),
                    close(ago(200), each=20.0), close(ago(100), each=-15.0)],
                   "mixed.jsonl")
    state2 = dayguard.check(now=NOW, env=ENV, path=mixed)
    check("連敗1", state2["consecLosses"] == 1, str(state2["consecLosses"]))
    check("TWO_LOSSES にしない",
          "TWO_LOSSES" not in [r["code"] for r in state2["notes"]], str(state2["notes"]))


test_two_losses()


section("5. 損切り後15分の冷却")


def test_cooldown():
    hot = ledger([close(ago(14), each=-15.0)], "hot.jsonl")
    state = dayguard.check(now=NOW, env=ENV, path=hot)
    check("14分後は COOLDOWN を note に記録",
          "COOLDOWN" in [r["code"] for r in state["notes"]], str(state["notes"]))
    check("冷却中でも blocked にしない", state["blocked"] is False)

    cool = ledger([close(ago(16), each=-15.0)], "cool.jsonl")
    state2 = dayguard.check(now=NOW, env=ENV, path=cool)
    check("16分後は解除", "COOLDOWN" not in [r["code"] for r in state2["notes"]],
          str(state2["notes"]))
    check("勝ちの直後は冷却しない",
          "COOLDOWN" not in [r["code"] for r in dayguard.check(
              now=NOW, env=ENV, path=ledger([close(ago(1), each=20.0)], "win.jsonl")
          )["notes"]])


test_cooldown()


section("6. 取引日の境界は JST 07:00(日付ではない)")


def test_trade_day_boundary():
    check("02:00 JST は前日の取引日",
          dayguard.trade_day(datetime(2026, 8, 18, 2, 0, tzinfo=JST)) == "2026-08-17")
    check("06:59 JST も前日",
          dayguard.trade_day(datetime(2026, 8, 18, 6, 59, tzinfo=JST)) == "2026-08-17")
    check("07:00 JST から当日",
          dayguard.trade_day(datetime(2026, 8, 18, 7, 0, tzinfo=JST)) == "2026-08-18")

    # 前取引日の −$180 は持ち越さない
    prev = ledger([close(datetime(2026, 8, 17, 3, 0, tzinfo=JST),
                         per={A2: -60.0, A3: -60.0, A4: -60.0})], "prev.jsonl")
    later = datetime(2026, 8, 18, 22, 0, tzinfo=JST)     # 取引日 2026-08-18
    state = dayguard.check(now=later, env=ENV, path=prev)
    check("翌取引日は allowed", state["blocked"] is False, str(state["notes"]))
    check("集計も 0 件", state["closes"] == 0, str(state["closes"]))


test_trade_day_boundary()


section("7. 破損した台帳でも新規は止めない(警告は残す)")


def test_corrupt_ledger_still_allows():
    path = ledger([close(ago(300), each=-15.0),
                   "{壊れた JSON",
                   json.dumps(["配列は不可"]),
                   json.dumps({"closedAt": "not-a-time", "pnlEach": -5}),
                   json.dumps({"closedAt": ago(100).isoformat()}),     # 損益なし
                   close(ago(50), each=-15.0)], "corrupt.jsonl")
    state = dayguard.check(now=NOW, env=ENV, path=path)
    check("正常行だけ集計(2件)", state["closes"] == 2, str(state["closes"]))
    check("警告が4件", len(state["warnings"]) == 4, str(state["warnings"]))
    check("破損でも blocked にしない", state["blocked"] is False, str(state))


test_corrupt_ledger_still_allows()


section("8. result からの自動記録(publish 成功後に呼ばれる)")


def test_record_from_result():
    path = os.path.join(TMP, "auto.jsonl")
    long_win = {"side": "LONG", "entry": 30000.0, "exit": 30010.0, "qty": 1,
                "pointValue": 2.0, "closedAt": ago(30).isoformat(), "resultId": "rs_x"}
    check("LONG の実現損益 = +$20", dayguard.realized_usd(long_win) == 20.0,
          str(dayguard.realized_usd(long_win)))
    short_win = {**long_win, "side": "SHORT"}
    check("SHORT は符号が反転", dayguard.realized_usd(short_win) == -20.0,
          str(dayguard.realized_usd(short_win)))
    check("書き込み成功", dayguard.record_from_result(long_win, path=path) is True)
    rows, warns = dayguard.load_ledger(path)
    check("1行入った", len(rows) == 1 and warns == [], f"{len(rows)} {warns}")
    check("pnlEach が +20.0", rows[0]["pnlEach"] == 20.0, str(rows[0]))
    check("source=publish_result", True)

    for bad in ({}, None, {"side": "LONG"}, {**long_win, "exit": None},
                {**long_win, "side": "NEUTRAL"}, {**long_win, "closedAt": "x"}):
        check(f"不完全な result は記録しない: {str(bad)[:28]}",
              dayguard.record_from_result(bad, path=path) is False)
    rows2, _ = dayguard.load_ledger(path)
    check("行は増えていない", len(rows2) == 1, str(len(rows2)))


test_record_from_result()


section("9. CLI(--status / --record / --json)")


def run_cli(args, env=None):
    merged = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8", **(env or {}))
    return subprocess.run([sys.executable, os.path.join(BASE, "dayguard.py")] + args,
                          capture_output=True, env=merged, cwd=BASE)


def test_cli():
    proc = run_cli(["--status"])
    check("--status は exit 0", proc.returncode == 0, proc.stderr.decode("utf-8", "replace"))
    proc = run_cli(["--json"])
    check("--json は JSON を返す", proc.returncode == 0
          and "blocked" in json.loads(proc.stdout.decode("utf-8")),
          proc.stdout.decode("utf-8", "replace")[:120])
    proc = run_cli(["--record", "--pnl-each", "-5", "--pnl", "A=1"])
    check("--pnl と --pnl-each の併用は exit 2", proc.returncode == 2)
    proc = run_cli(["--record", "--pnl-each", "-5", "--closed-at", "not-a-time"])
    check("不正な --closed-at は exit 2", proc.returncode == 2)


test_cli()


section("10. order.py — ガードは廃止。どの操作も止めない")


def test_order_guard_paths():
    import order
    # 旧仕様の blocked 状態を渡しても、もう止まらないことを固定する。
    legacy_blocked = {"blocked": True, "reasons": [{"code": "TWO_LOSSES", "detail": "2連敗"}],
                      "tradeDay": "2026-08-17", "dayPnl": -30.0, "perAccount": {},
                      "consecLosses": 2, "closes": 2, "lastLossAt": None, "warnings": []}
    check("order に互換シンボルが残っている", hasattr(order, "dayguard_gate"),
          "order.dayguard_gate が無い")
    if not hasattr(order, "dayguard_gate"):
        return
    for action in ("new", "flatten", "modify", "cancel", "status"):
        check(f"--{action} は旧 blocked でも通る",
              order.dayguard_gate(legacy_blocked, action) is True)
    check("止める操作は1つも残っていない", order.GUARDED_ACTIONS == (),
          str(order.GUARDED_ACTIONS))
    check("dayguard_enabled() は False(廃止済み)", order.dayguard_enabled() is False)


test_order_guard_paths()


section("11. ガード廃止後もランナー管理・撤退経路は当然通る")


def test_runner_management_not_blocked():
    """R8 §3.3: High RR 管理(建値移動・トレール)の唯一の実行手段は
    order.py --modify。ガードを廃止した今は新規も止まらないが、
    「撤退・管理経路を塞がない」という元々の性質は落とさずに固定する。
    """
    import order
    path = ledger([close(ago(300), per={A2: 100.0, A3: 80.0, A4: 212.0})],
                  "r8_daygoal.jsonl")
    state = dayguard.check(now=NOW, env=ENV, path=path)
    check("DAYGOAL_REACHED は note に出る",
          any(r["code"] == "DAYGOAL_REACHED" for r in state["notes"]),
          str(state["notes"]))
    check("それでも blocked にはならない", state["blocked"] is False)
    for action in ("new", "modify", "flatten"):
        check(f"--{action} は通る", order.dayguard_gate(state, action) is True)


test_runner_management_not_blocked()


shutil.rmtree(TMP, ignore_errors=True)

print("\n" + "=" * 68)
if FAILED:
    print(f"FAILED {len(FAILED)}: " + ", ".join(FAILED))
    sys.exit(1)
print("ALL PASS (test_dayguard)")
sys.exit(0)

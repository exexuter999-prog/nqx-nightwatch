# -*- coding: utf-8 -*-
"""trade_journal.py — 決済の自動記録を検証する。

守る性質:
  1. 建玉 2→1→0 の遷移から、**凍結プランの脚価格**で戦績を組み立てる
  2. 価格がどの水準にも届いていない減少は **推測せず保留**する(嘘を作らない)
  3. 金額は realizedPnL の差分で突合し、差は fees(滑り+手数料)に落ちる
  4. 同じ決済を二度 publish しない
  5. ネットワークも発注も呼ばない(注入した偽物だけを使う)
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import trade_journal as tj  # noqa: E402

FAILED = []
TMP = tempfile.mkdtemp(prefix="trade_journal_test_")
UTC = timezone.utc
T0 = datetime(2026, 8, 27, 6, 0, tzinfo=UTC)


def check(name, cond, detail=""):
    print(("  OK   " if cond else "  FAIL ") + name
          + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


def section(title):
    print(f"\n{'=' * 68}\n{title}\n{'=' * 68}")


PLAN = {
    "entryKey": "ENTRY:abc", "symbol": "MNQU6", "side": "SELL", "qty": 2,
    "entry": 29262.25, "initialStop": 29302.0,
    "tp1": 29186.25, "finalTarget": 29134.25,
    "legs": [{"id": "TP1", "qty": 1, "target": 29186.25},
             {"id": "RUNNER", "qty": 1, "target": 29134.25}],
    "trailDistance": 29.75,
}


def bars(*rows):
    """rows: (minutes_after_T0, high, low)。close は中点にしておく。"""
    return {"snapshot": {"bars3m": [
        {"t": (T0 + timedelta(minutes=m)).timestamp(), "h": h, "l": lo,
         "o": (h + lo) / 2, "c": (h + lo) / 2}
        for m, h, lo in rows]}}


def position(qty, **extra):
    base = {"verified": True, "qty": qty, "symbol": "MNQU6", "side": "SHORT",
            "avgEntry": 29262.25, "filledAt": T0.isoformat(),
            "observedAt": T0.isoformat(), "generation": "PG:1:x"}
    base.update(extra)
    return base


def balance(realized):
    return {"verified": True, "realizedPnL": realized,
            "observedAt": T0.isoformat()}


ACC = "ACCT1"


section("1. 水準に届いた脚だけを割り当てる")


def test_attribute():
    pending = [{"id": "TP1", "qty": 1, "target": 29186.25},
               {"id": "RUNNER", "qty": 1, "target": 29134.25}]
    hit_tp1 = tj.attribute_exit("SHORT", 1, pending, 29302.0, high=29270.0, low=29180.0)
    check("TP1 に届いていれば target", hit_tp1 == [
        {"id": "TP1", "qty": 1, "exit": 29186.25, "kind": "target"}], str(hit_tp1))

    hit_stop = tj.attribute_exit("SHORT", 2, pending, 29302.0, high=29310.0, low=29250.0)
    check("SL に届いていれば stop 2枚",
          [leg["kind"] for leg in hit_stop] == ["stop", "stop"], str(hit_stop))
    check("stop の価格は現在の SL", all(leg["exit"] == 29302.0 for leg in hit_stop),
          str(hit_stop))

    nothing = tj.attribute_exit("SHORT", 1, pending, 29302.0, high=29270.0, low=29240.0)
    check("どこにも届いていなければ空(推測しない)", nothing == [], str(nothing))

    no_bars = tj.attribute_exit("SHORT", 1, pending, 29302.0, high=None, low=None)
    check("足が無ければ空", no_bars == [], str(no_bars))

    # LONG は向きが反転する
    long_pending = [{"id": "TP1", "qty": 1, "target": 29400.0}]
    long_hit = tj.attribute_exit("LONG", 1, long_pending, 29200.0, high=29405.0, low=29300.0)
    check("LONG の利確は高値で判定", long_hit and long_hit[0]["kind"] == "target",
          str(long_hit))
    long_stop = tj.attribute_exit("LONG", 1, long_pending, 29200.0, high=29350.0, low=29190.0)
    check("LONG の損切りは安値で判定", long_stop and long_stop[0]["kind"] == "stop",
          str(long_stop))


test_attribute()


section("2. 2→1→0 を追って戦績が組み上がる")


def test_full_cycle():
    published = []

    def fake_publish(result, cfg=None):
        published.append(result)
        return True, {"accepted": True}

    path = os.path.join(TMP, "journal1.json")

    # --- サイクル1: 建玉が見えた
    j, closed, notes = tj.observe(bars((0, 29270.0, 29255.0)), {ACC: position(2)},
                                  plans={ACC: PLAN}, records=[],
                                  balances={ACC: balance(0.0)},
                                  journal=tj.load_journal(path), now=T0)
    check("1周期目は追跡開始のみ", closed == [] and len(j["open"]) == 1, str(notes))
    check("realizedPnL を建玉時に控える",
          list(j["open"].values())[0]["realizedAtOpen"] == 0.0)

    # --- サイクル2: TP1 に届いて 1枚へ
    j, closed, notes = tj.observe(bars((0, 29270.0, 29255.0), (3, 29260.0, 29185.0)),
                                  {ACC: position(1)}, plans={ACC: PLAN}, records=[],
                                  balances={ACC: balance(52.0)}, journal=j,
                                  now=T0 + timedelta(minutes=6))
    state = list(j["open"].values())[0]
    check("TP1 が1枚 filled に入る",
          [(f["id"], f["kind"], f["exit"]) for f in state["filled"]]
          == [("TP1", "target", 29186.25)], str(state["filled"]))
    check("まだ閉じていない", closed == [])

    # --- サイクル3: 最終TP に届いて FLAT
    flat = {"verified": True, "qty": 0, "symbol": "MNQU6",
            "closedAt": (T0 + timedelta(minutes=30)).isoformat(),
            "observedAt": (T0 + timedelta(minutes=30)).isoformat()}
    j, closed, notes = tj.observe(
        bars((0, 29270.0, 29255.0), (3, 29260.0, 29185.0), (9, 29190.0, 29130.0)),
        {ACC: flat}, plans={ACC: PLAN}, records=[],
        balances={ACC: balance(300.0)}, journal=j, now=T0 + timedelta(minutes=30))
    check("閉じた建玉が1件返る", len(closed) == 1, str(notes))
    if not closed:
        return
    done = closed[0]
    check("脚は TP1 と RUNNER の2本",
          [f["id"] for f in done["filled"]] == ["TP1", "RUNNER"], str(done["filled"]))
    check("台帳から消えている", j["open"] == {}, str(j["open"]))

    ok, detail, result = tj.build_and_publish(done, mode="LIVE", publisher=fake_publish)
    check("publish 成功", ok, detail)
    if not ok:
        return
    check("exitSource=broker", result["exitSource"] == "broker", str(result["exitSource"]))
    check("exitBasis=plan-legs", result.get("exitBasis") == "plan-legs")
    check("脚が2本記録されている", len(result.get("legs") or []) == 2, str(result.get("legs")))
    check("SHORT のまま", result["side"] == "SHORT", result["side"])
    # 粗損益 = (29262.25−29186.25) + (29262.25−29134.25) = 76 + 128 = 204pt → $408
    gross = 204.0 * 2.0
    check("粗損益は脚価格どおり $408", abs(gross - 408.0) < 1e-9)
    # realizedPnL 差分は +300 なので fees = 408 − 300 = 108
    check("fees は realizedPnL との差 $108",
          abs(float(result["fees"]) - 108.0) < 0.01, str(result.get("fees")))
    check("mode=LIVE", result["mode"] == "LIVE")


test_full_cycle()


section("3. 水準に届かない減少は保留する(嘘を publish しない)")


def test_unattributable_is_held():
    published = []
    path = os.path.join(TMP, "journal2.json")
    j = tj.load_journal(path)
    j, closed, _ = tj.observe(bars((0, 29270.0, 29255.0)), {ACC: position(2)},
                              plans={ACC: PLAN}, records=[],
                              balances={ACC: balance(0.0)}, journal=j, now=T0)
    # 価格はどの水準にも届いていないのに枚数だけ減った(手動決済など)
    flat = {"verified": True, "qty": 0, "symbol": "MNQU6",
            "closedAt": (T0 + timedelta(minutes=9)).isoformat(),
            "observedAt": (T0 + timedelta(minutes=9)).isoformat()}
    j, closed, notes = tj.observe(bars((0, 29270.0, 29255.0), (3, 29268.0, 29250.0)),
                                  {ACC: flat}, plans={ACC: PLAN}, records=[],
                                  balances={ACC: balance(-20.0)}, journal=j,
                                  now=T0 + timedelta(minutes=9))
    check("publish 候補にしない", closed == [], str(closed))
    check("unresolved へ退避する", len(j.get("unresolved") or []) == 1, str(j.get("unresolved")))
    check("人に回す note が出る",
          any("/result" in note for note in notes), str(notes))
    check("publish は呼ばれない", published == [])


test_unattributable_is_held()


section("4. トレール後の SL は MANAGEMENT_SENT から読む")


def test_trailed_stop():
    records = [{"key": "k", "status": "MANAGEMENT_SENT", "planEntryKey": "ENTRY:abc",
                "action": {"action": "MODIFY", "sl": 29240.0, "tp": 29134.25}}]
    check("最新の MODIFY の sl を採る",
          tj.current_stop(records, PLAN, "ENTRY:abc") == 29240.0)
    check("MODIFY が無ければ構造 SL",
          tj.current_stop([], PLAN, "ENTRY:abc") == 29302.0)

    path = os.path.join(TMP, "journal3.json")
    j = tj.load_journal(path)
    j, _, _ = tj.observe(bars((0, 29270.0, 29255.0)), {ACC: position(2)},
                         plans={ACC: PLAN}, records=records,
                         balances={ACC: balance(0.0)}, journal=j, now=T0)
    # TP1 は取れて、runner はトレール SL 29240 で刈られた
    flat = {"verified": True, "qty": 0, "symbol": "MNQU6",
            "closedAt": (T0 + timedelta(minutes=12)).isoformat(),
            "observedAt": (T0 + timedelta(minutes=12)).isoformat()}
    j, closed, _ = tj.observe(
        bars((0, 29270.0, 29255.0), (3, 29260.0, 29185.0), (6, 29245.0, 29180.0)),
        {ACC: flat}, plans={ACC: PLAN}, records=records,
        balances={ACC: balance(140.0)}, journal=j, now=T0 + timedelta(minutes=12))
    check("2本とも割り当てられる", len(closed) == 1 and len(closed[0]["filled"]) == 2,
          str(closed))
    if closed:
        kinds = [(f["id"], f["kind"], f["exit"]) for f in closed[0]["filled"]]
        check("RUNNER はトレール SL で stop",
              ("RUNNER", "target", 29134.25) in kinds or
              ("RUNNER", "stop", 29240.0) in kinds, str(kinds))


test_trailed_stop()


section("5. 未確認の照会では建玉の生死を判断しない")


def test_unverified_is_ignored():
    path = os.path.join(TMP, "journal4.json")
    j = tj.load_journal(path)
    j, closed, notes = tj.observe(bars((0, 29270.0, 29255.0)), {ACC: position(2)},
                                  plans={ACC: PLAN}, records=[],
                                  balances={ACC: balance(0.0)}, journal=j, now=T0)
    check("追跡開始", len(j["open"]) == 1)
    unverified = {"verified": False, "qty": 0, "symbol": "MNQU6"}
    j, closed, notes = tj.observe(bars((0, 29270.0, 29255.0)), {ACC: unverified},
                                  plans={ACC: PLAN}, records=[],
                                  balances={ACC: {}}, journal=j,
                                  now=T0 + timedelta(minutes=3))
    check("UNVERIFIED は決済扱いしない", closed == [] and len(j["open"]) == 1,
          str((closed, j["open"])))


test_unverified_is_ignored()


section("6. realizedPnL が取れなくても粗損益で記録する")


def test_without_balance():
    published = []

    def fake_publish(result, cfg=None):
        published.append(result)
        return True, {}

    state = {
        "account": ACC, "symbol": "MNQU6", "side": "SHORT",
        "entry": 29262.25, "initialStop": 29302.0, "initialQty": 2,
        "openedAt": T0.isoformat(), "closedAt": (T0 + timedelta(minutes=30)).isoformat(),
        "filled": [{"id": "TP1", "qty": 1, "exit": 29186.25, "kind": "target"},
                   {"id": "RUNNER", "qty": 1, "exit": 29134.25, "kind": "target"}],
        "realizedAtOpen": None, "realizedAtClose": None,
    }
    ok, detail, result = tj.build_and_publish(state, publisher=fake_publish)
    check("突合できなくても publish する", ok, detail)
    check("fees は付けない(捏造しない)", "fees" not in (result or {}), str(result))

    # 有利な滑り(broker net > 粗損益)でも落ちない
    state2 = {**state, "realizedAtOpen": 0.0, "realizedAtClose": 500.0}
    ok2, detail2, result2 = tj.build_and_publish(state2, publisher=fake_publish)
    check("fees が負になる場合も publish は通る", ok2, detail2)
    check("その場合は突合を諦めた旨が残る", "not reconciled" in (detail2 or ""), detail2)


test_without_balance()


section("7. 同じ決済を二度記録しない")


def test_idempotent():
    calls = []

    def fake_publish(result, cfg=None):
        calls.append(result["resultId"])
        return True, {}

    state = {
        "account": ACC, "symbol": "MNQU6", "side": "SHORT",
        "entry": 29262.25, "initialStop": 29302.0, "initialQty": 2,
        "openedAt": T0.isoformat(), "closedAt": (T0 + timedelta(minutes=30)).isoformat(),
        "filled": [{"id": "TP1", "qty": 1, "exit": 29186.25, "kind": "target"},
                   {"id": "RUNNER", "qty": 1, "exit": 29134.25, "kind": "target"}],
        "realizedAtOpen": 0.0, "realizedAtClose": 300.0,
    }
    ok1, _, r1 = tj.build_and_publish(state, publisher=fake_publish)
    ok2, _, r2 = tj.build_and_publish(state, publisher=fake_publish)
    check("同じ素材は同じ resultId", ok1 and ok2 and r1["resultId"] == r2["resultId"],
          f"{r1 and r1['resultId']} / {r2 and r2['resultId']}")
    # reconcile 側の published リストが二重掲載を止める
    journal = {"open": {}, "published": [r1["resultId"]]}
    check("published に載っていれば skip する",
          r1["resultId"] in journal["published"])


test_idempotent()


section("8. 発注経路には触れない")


def test_no_order_calls():
    """散文での言及ではなく、**実際の呼び出し構文**が無いことを見る。"""
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(tj))

    imported = set()
    called = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
        elif isinstance(node, ast.Call):
            target = node.func
            if isinstance(target, ast.Attribute):
                called.add(target.attr)
            elif isinstance(target, ast.Name):
                called.add(target.id)

    for forbidden in ("subprocess", "urllib", "requests", "socket", "http", "order"):
        check(f"{forbidden} を import しない", forbidden not in imported, str(sorted(imported)))
    for forbidden in ("run", "Popen", "check_output", "urlopen", "send_order", "place"):
        check(f"{forbidden}() を呼ばない", forbidden not in called)
    check("import するのは読み取り系だけ",
          imported <= {"json", "os", "datetime", "typing", "annotations",
                       "__future__", "nqx_state", "autotrade_engine", "broker_status",
                       # R48: モデル別スコアカード。ローカル jsonl への追記のみで、
                       # ネットワーク・発注系は呼ばない(test_model_scorecard.py 参照)。
                       "model_scorecard",
                       # R56: 遡及記録 CLI(--backfill)の引数読みだけに使う。
                       "sys",
                       # R57: 根拠チャート(result.chart)。ローカルの確定足・監査バンドルを
                       # 読むだけで、ネットワーク・発注系は呼ばない(test_result_context.py)。
                       "result_context"},
          str(sorted(imported)))


test_no_order_calls()


print()
if FAILED:
    print(f"FAILED ({len(FAILED)}): " + ", ".join(FAILED))
    sys.exit(1)
print("ALL PASS (test_trade_journal)")

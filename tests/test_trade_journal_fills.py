# -*- coding: utf-8 -*-
"""trade_journal.py R56 — 約定(fills)からの往復再構成と記録を検証する。

守る性質:
  1. 約定列から往復(0 → 非0 → 0)を組み立て、建値は数量加重平均、決済は約定価格そのもの
  2. 凍結プランは orderId で引き、脚は TP1 / SL / STOP / EXIT と名付ける(価格は変えない)
  3. プランが無い往復も stop 無しで記録される(R は出さないが損益は残る)
  4. 約定列の建玉とブローカー照会が食い違うサイクルは状態を進めず、何も publish しない
  5. 同じ約定を二度数えない / 同じ result を二度 publish しない
  6. realizedPnL の差分が取れれば fees に落ちる
  7. ネットワークも発注も呼ばない(注入した偽物だけを使う)

固定データは 2026-09-04 の実口座の約定列(価格・枚数・ID は実測どおり)。
"""
import json
import os
import sys
import tempfile
from datetime import datetime, timezone

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import trade_journal as tj  # noqa: E402

FAILED = []
TMP = tempfile.mkdtemp(prefix="trade_journal_fills_test_")
UTC = timezone.utc


def check(name, cond, detail=""):
    print(("  OK   " if cond else "  FAIL ") + name
          + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


def section(title):
    print(f"\n{'=' * 68}\n{title}\n{'=' * 68}")


ACC = "LFE05062316710024"
D = "2026-09-04T"


def fill(fid, oid, at, action, qty, price):
    return {"id": fid, "orderId": oid, "timestamp": D + at + "Z", "action": action,
            "qty": qty, "price": price, "instrument": "MNQU6"}


# 09-04 の実約定(27 件)。往復は 5 つ閉じ、最後に SHORT 14 が開いたまま。
FILLS = [
    fill("649589730079", "649589730038", "16:07:13.434", "Sell", 18, 29526.5),
    fill("649589730093", "649589730040", "16:13:05.691", "Buy", 12, 29549.0),
    fill("649589730102", "649589730040", "16:13:05.970", "Buy", 6, 29549.0),
    fill("649589730164", "649589730150", "17:05:12.243", "Sell", 6, 29556.75),
    fill("649589730167", "649589730157", "17:05:12.243", "Sell", 6, 29556.75),
    fill("649589730186", "649589730151", "17:34:36.630", "Buy", 6, 29487.0),
    fill("649589730235", "649589730232", "18:52:00.523", "Buy", 6, 29523.5),
    fill("649589730260", "649589730253", "19:24:30.161", "Sell", 6, 29523.5),
    fill("649589730292", "649589730289", "19:26:58.738", "Buy", 6, 29521.75),
    fill("649589730304", "649589730297", "19:27:34.574", "Sell", 14, 29529.25),
    fill("649589730322", "649589730315", "19:27:35.167", "Sell", 14, 29529.75),
    fill("649589730333", "649589730299", "19:34:01.729", "Buy", 4, 29538.75),
    fill("649589730338", "649589730317", "19:34:01.729", "Buy", 4, 29538.75),
    fill("649589730351", "649589730299", "19:34:02.292", "Buy", 5, 29538.75),
    fill("649589730356", "649589730317", "19:34:02.292", "Buy", 5, 29538.75),
    fill("649589730371", "649589730299", "19:34:03.270", "Buy", 1, 29538.75),
    fill("649589730376", "649589730317", "19:34:03.270", "Buy", 1, 29538.75),
    fill("649589730389", "649589730299", "19:34:03.411", "Buy", 2, 29538.75),
    fill("649589730394", "649589730317", "19:34:03.411", "Buy", 2, 29538.75),
    fill("649589730407", "649589730299", "19:34:04.530", "Buy", 2, 29539.0),
    fill("649589730412", "649589730317", "19:34:04.530", "Buy", 2, 29539.0),
    fill("649589730430", "649589730423", "19:51:30.994", "Sell", 10, 29570.25),
    fill("649589730448", "649589730441", "19:51:32.139", "Sell", 10, 29570.5),
    fill("649589730459", "649589730425", "19:55:00.112", "Buy", 10, 29582.75),
    fill("649589730464", "649589730443", "19:55:00.112", "Buy", 10, 29582.75),
    fill("649589730482", "649589730475", "19:56:28.400", "Sell", 7, 29574.25),
    fill("649589730500", "649589730493", "19:56:34.667", "Sell", 7, 29575.0),
]

# 台帳に残る凍結プラン(17:05 の 12 枚。TP1 29487 / RUNNER 29350.75 / SL 29599.5)。
LEDGER_ROWS = [
    {"time": D + "17:05:03+00:00", "status": "ENTRY_RESTING", "key": "ENTRY:ab68", "plan": {
        "symbol": "MNQU6", "side": "SELL", "qty": 12, "entry": 29556.75, "initialStop": 29599.5,
        "tp1": 29487.0, "finalTarget": 29350.75, "targets": [29487.0, 29350.75],
        "legs": [{"id": "TP1", "qty": 6, "target": 29487.0}, {"id": "RUNNER", "qty": 6, "target": 29350.75}],
        "scenarioId": "321a145acc335fc3", "model": "BREAKER_CONTINUATION", "grade": "B",
        "accountScope": [ACC],
        "pendingOrderOwnership": {"accountScope": [ACC], "orderIds": ["649589730150", "649589730157"]},
    }},
    # 16:07 の 18 枚。プランは 8 枚だったが約定は 18 枚(ULTRA 枚数)。orderId で引ける。
    {"time": D + "16:07:20+00:00", "status": "ENTRY_SENT", "key": "ENTRY:7432", "plan": {
        "symbol": "MNQU6", "side": "SELL", "qty": 8, "entry": 29526.5, "initialStop": 29549.0,
        "tp1": 29483.25, "finalTarget": 29350.75, "targets": [29483.25, 29350.75],
        "legs": [{"id": "TP1", "qty": 4, "target": 29483.25}, {"id": "RUNNER", "qty": 4, "target": 29350.75}],
        "scenarioId": "171c26f5b84d175b", "model": "TURTLE_SOUP_REVERSAL", "grade": "A",
        "accountScope": [ACC]},
     "brokerOrder": {"filledOrderIds": ["649589730038"], "orders": [{"orderId": "649589730040"}]}},
]


def write_ledger(path, rows):
    with open(path, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


section("1. 約定列 → 往復")


def test_round_trips():
    fills = tj.normalize_fills(FILLS, "MNQU6")
    check("27 件が時系列に並ぶ", len(fills) == 27 and fills[0]["id"] == "649589730079")
    closed, state = tj.round_trips(fills)
    check("往復は 5 つ閉じる", len(closed) == 5, str(len(closed)))
    check("最後は SHORT 14 が開いたまま", state["position"] == -14
          and state["trade"] and state["trade"]["side"] == "SHORT", str(state))

    t1, t2, t3, t4, t5 = closed
    check("1: SHORT 18 @29526.50", t1["side"] == "SHORT" and t1["qty"] == 18 and t1["entry"] == 29526.5)
    check("1: 同一注文・同一価格の 2 約定は 1 脚", len(t1["legs"]) == 1
          and t1["legs"][0]["qty"] == 18 and t1["legs"][0]["price"] == 29549.0, str(t1["legs"]))
    check("2: SHORT 12 の建値は 2 脚の加重平均", t2["qty"] == 12 and t2["entry"] == 29556.75)
    check("2: 決済は 29487 と 29523.5 の 2 脚",
          [(leg["qty"], leg["price"]) for leg in t2["legs"]] == [(6, 29487.0), (6, 29523.5)], str(t2["legs"]))
    check("3: 2 分の往復も 1 件", t3["qty"] == 6 and t3["legs"][0]["price"] == 29521.75)
    check("4: 14+14 の建値は 29529.50", t4["qty"] == 28 and t4["entry"] == 29529.5, str(t4["entry"]))
    check("4: 10 約定の SL は価格で 2 脚に束ねる(上限 8)",
          len(t4["legs"]) == 2 and sum(leg["qty"] for leg in t4["legs"]) == 28
          and [leg["price"] for leg in t4["legs"]] == [29538.75, 29539.0], str(t4["legs"]))
    check("5: 建値 29570.375 は tick へ丸めて 29570.50", t5["entry"] == 29570.5 and abs(t5["entryRaw"] - 29570.375) < 1e-9)
    check("開始・終了時刻は最初の建玉約定と最後の決済約定",
          t5["openedAt"].startswith("2026-09-04T19:51:30") and t5["closedAt"].startswith("2026-09-04T19:55:00"))

    # ドテン: LONG 2 を SELL 5 で突き抜けると LONG が閉じ、SHORT 3 が開く
    flip = tj.normalize_fills([
        fill("a", "o1", "10:00:00.000", "Buy", 2, 100.0),
        fill("b", "o2", "10:01:00.000", "Sell", 5, 101.0),
    ])
    closed_flip, state_flip = tj.round_trips(flip)
    check("ドテン: LONG 2 が閉じる", len(closed_flip) == 1 and closed_flip[0]["qty"] == 2
          and closed_flip[0]["legs"][0]["price"] == 101.0)
    check("ドテン: 余り 3 枚で SHORT が開く", state_flip["position"] == -3
          and state_flip["trade"]["opens"][0]["qty"] == 3)

    # 続きから: 前回の state を渡すと同じ往復が組み上がる
    first, mid = tj.round_trips(fills[:22])
    second, end = tj.round_trips(fills[22:], mid)
    check("state を跨いでも往復数は同じ", len(first) + len(second) == 5 and end["position"] == -14)


test_round_trips()


section("2. 凍結プランへの帰属と脚の名前")


def test_attribution():
    fills = tj.normalize_fills(FILLS, "MNQU6")
    closed, _ = tj.round_trips(fills)
    by_order, timeline = tj.plan_lookup(LEDGER_ROWS, ACC, "MNQU6")
    check("pendingOrderOwnership の orderId で引ける", "649589730157" in by_order)
    check("brokerOrder.filledOrderIds でも引ける", "649589730038" in by_order)

    plan2, how2 = tj.attribute_plan(closed[1], by_order, timeline)
    check("17:05 の 12 枚は orderId で帰属", how2 == "order-id" and plan2["scenarioId"] == "321a145acc335fc3")
    legs2 = tj.label_legs(closed[1], plan2)
    check("29487 は TP1(target 付き)", legs2[0]["id"] == "TP1" and legs2[0]["target"] == 29487.0, str(legs2))
    check("29523.5 は建値より有利な手動決済 → EXIT", legs2[1]["id"] == "EXIT" and "target" not in legs2[1], str(legs2))

    plan1, how1 = tj.attribute_plan(closed[0], by_order, timeline)
    legs1 = tj.label_legs(closed[0], plan1)
    check("16:07 の 18 枚はプラン 8 枚でも orderId で帰属", how1 == "order-id" and plan1["grade"] == "A")
    check("29549 は初期 SL に届いた → SL", legs1[0]["id"] == "SL", str(legs1))

    plan4, how4 = tj.attribute_plan(closed[3], by_order, timeline)
    check("19:27 の 28 枚はプランが無い → 無帰属", plan4 is None and how4 is None)
    legs4 = tj.label_legs(closed[3], None)
    check("無帰属でも不利な決済は STOP と名付ける", all(leg["id"] == "STOP" for leg in legs4), str(legs4))

    # 近傍照合: orderId が台帳に無くても、同方向・建値 2pt 以内・30 分以内なら引く
    near_rows = [{"time": D + "19:26:00+00:00", "status": "ENTRY_SENT", "key": "ENTRY:near", "plan": {
        "symbol": "MNQU6", "side": "SELL", "qty": 28, "entry": 29530.0, "initialStop": 29545.0,
        "targets": [29500.0, 29350.75], "scenarioId": "near", "grade": "B", "accountScope": [ACC]}}]
    by_order_n, timeline_n = tj.plan_lookup(near_rows, ACC, "MNQU6")
    plan_n, how_n = tj.attribute_plan(closed[3], by_order_n, timeline_n)
    check("近傍照合で帰属できる", how_n == "proximity" and plan_n["scenarioId"] == "near")
    far_rows = [{**near_rows[0], "time": D + "18:00:00+00:00"}]
    check("30 分より前のプランには帰属しない",
          tj.attribute_plan(closed[3], *tj.plan_lookup(far_rows, ACC, "MNQU6"))[0] is None)
    other_rows = [{**near_rows[0], "plan": {**near_rows[0]["plan"], "accountScope": ["OTHER"]}}]
    check("他口座のプランには帰属しない",
          tj.attribute_plan(closed[3], *tj.plan_lookup(other_rows, ACC, "MNQU6"))[0] is None)


test_attribution()


section("3. result の組み立て(プラン有り / 無し)")


def test_build():
    fills = tj.normalize_fills(FILLS, "MNQU6")
    closed, _ = tj.round_trips(fills)
    by_order, timeline = tj.plan_lookup(LEDGER_ROWS, ACC, "MNQU6")
    for trade in closed:
        trade["symbol"] = "MNQU6"

    plan2, _ = tj.attribute_plan(closed[1], by_order, timeline)
    result, detail = tj.build_trade_result(closed[1], plan2, account=ACC, mode="LIVE")
    check("プラン有り: result ができる", result is not None, detail)
    check("stop は凍結プランの初期 SL", result and result["stop"] == 29599.5)
    check("exit は脚の加重平均", result and abs(result["exit"] - 29505.25) < 1e-9, str(result and result["exit"]))
    check("model / grade / scenarioId / accountId が乗る",
          result and result.get("model") == "BREAKER_CONTINUATION" and result.get("grade") == "B"
          and result.get("scenarioId") == "321a145acc335fc3" and result.get("accountId") == ACC)
    check("exitSource は broker、出所は broker-fills",
          result and result["exitSource"] == "broker" and result.get("exitBasis") == "broker-fills")
    kinds = [leg["kind"] for leg in result["legs"]] if result else []
    check("脚の kind は価格から出る(TP1=target / 手動=manual)", kinds == ["target", "manual"], str(kinds))

    result4, detail4 = tj.build_trade_result(closed[3], None, account=ACC, mode="LIVE")
    check("プラン無し: それでも result ができる", result4 is not None, detail4)
    check("プラン無し: stop は None(R を出さない)", result4 and result4["stop"] is None)
    # exit(表示用の加重平均)は tick へ丸まるが、脚の価格は約定そのもの。
    # 損益は脚から出すと 24 枚 × 9.25pt + 4 枚 × 9.5pt = $520 ちょうど。
    legs_gross = sum((float(leg["exit"]) - result4["entry"]) * -1.0 * 2 * int(leg["qty"])
                     for leg in result4["legs"]) if result4 else None
    check("プラン無し: 損益は約定どおり(28 枚 · 脚から −$520)",
          legs_gross is not None and abs(legs_gross - (-520.0)) < 1e-9, str(legs_gross))
    check("プラン無し: 表示用 exit は tick へ丸めた加重平均", result4 and result4["exit"] == 29538.75,
          str(result4 and result4["exit"]))
    check("プラン無し: model は付かない", result4 and not result4.get("model"))

    # 手入力(manual)は stop 必須のまま
    import nqx_state
    try:
        nqx_state.result_from_report("SHORT", 29529.5, None, 28, D + "19:27:34Z", D + "19:34:04Z",
                                     exit_price=29538.75, exit_source="manual", symbol="MNQU6")
        check("manual は stop 無しを拒む", False)
    except ValueError:
        check("manual は stop 無しを拒む", True)

    # realizedPnL の差分は fees に落ちる(粗 −$810 に対し純 −$828 → fees 18)
    plan1, _ = tj.attribute_plan(closed[0], by_order, timeline)
    result1, _ = tj.build_trade_result(closed[0], plan1, account=ACC, mode="LIVE", reported_net=-828.0)
    check("fees = 粗損益 − 実現損益", result1 and abs(result1.get("fees", 0) - 18.0) < 1e-9, str(result1 and result1.get("fees")))


test_build()


section("4. reconcile_fills: 記録・保留・二重防止")


class Fake:
    def __init__(self, fills, position, realized=None):
        self.fills = fills
        self.position = position
        self.realized = realized
        self.published = []
        self.fills_calls = 0

    def fills_query(self, account=None):
        self.fills_calls += 1
        return {"verified": True, "account": account, "fills": self.fills, "source": "fake"}

    def position_query(self, symbol, account=None):
        return self.position

    def balance_query(self, account=None):
        if self.realized is None:
            return {"verified": False}
        return {"verified": True, "realizedPnL": self.realized, "observedAt": "2026-09-05T04:00:00+09:00"}

    def publisher(self, result):
        self.published.append(result)
        return True, {"ok": True}


def bundle():
    return {"symbol": "MNQU6", "snapshot": {"bars3m": [
        {"t": datetime(2026, 9, 4, 19, 30, tzinfo=UTC).timestamp(), "h": 29540.0, "l": 29520.0,
         "o": 29530.0, "c": 29535.0}]}}


def test_reconcile():
    fake = Fake(FILLS, {"verified": True, "qty": 14, "side": "SHORT", "symbol": "MNQU6"})
    journal = tj.load_journal(os.path.join(TMP, "none.json"))
    notes = tj.reconcile_fills(bundle(), accounts=[ACC], fills_query=fake.fills_query,
                               position_query=fake.position_query, balance_query=fake.balance_query,
                               records=LEDGER_ROWS, journal=journal, mode="LIVE",
                               publisher=fake.publisher)
    check("閉じた 5 往復が publish される", len(fake.published) == 5, "\n".join(notes))
    check("published に resultId が残る", len(journal["published"]) == 5)
    state = journal["fills"][ACC]
    check("約定 27 件が seen に入る", len(state["seen"]) == 27)
    check("進行中の SHORT 14 が state に残る", state["position"] == -14 and state["trade"]["side"] == "SHORT")
    check("建玉が残っている周期に手数料の基準は置かない(R54)", "flat" not in state)
    ids = {r["resultId"] for r in fake.published}
    check("resultId は 5 つとも異なる", len(ids) == 5)
    attributed = [r for r in fake.published if r.get("model")]
    check("プラン付きは 2 件(16:07 と 17:05)、残りは無帰属", len(attributed) == 2
          and all(r["stop"] is None for r in fake.published if not r.get("model")))

    # 同じ約定列でもう一度 → 何も増えない
    notes2 = tj.reconcile_fills(bundle(), accounts=[ACC], fills_query=fake.fills_query,
                                position_query=fake.position_query, balance_query=fake.balance_query,
                                records=LEDGER_ROWS, journal=journal, mode="LIVE",
                                publisher=fake.publisher)
    check("二度目は publish しない", len(fake.published) == 5, "\n".join(notes2))

    # 建玉照会と食い違う(ブローカーは FLAT と言う) → 状態を進めず、何も送らない
    fake_bad = Fake(FILLS, {"verified": True, "qty": 0, "symbol": "MNQU6"})
    journal_bad = tj.load_journal(os.path.join(TMP, "none2.json"))
    notes_bad = tj.reconcile_fills(bundle(), accounts=[ACC], fills_query=fake_bad.fills_query,
                                   position_query=fake_bad.position_query,
                                   balance_query=fake_bad.balance_query,
                                   records=LEDGER_ROWS, journal=journal_bad, mode="LIVE",
                                   publisher=fake_bad.publisher)
    check("食い違いは保留(publish 0)", not fake_bad.published and any("holding" in n for n in notes_bad),
          "\n".join(notes_bad))
    check("食い違いでは seen も進まない", ACC not in journal_bad.get("fills", {}))

    # 建玉未確認 → 保留
    fake_unv = Fake(FILLS, {"verified": False})
    journal_unv = tj.load_journal(os.path.join(TMP, "none3.json"))
    tj.reconcile_fills(bundle(), accounts=[ACC], fills_query=fake_unv.fills_query,
                       position_query=fake_unv.position_query, balance_query=fake_unv.balance_query,
                       records=LEDGER_ROWS, journal=journal_unv, mode="LIVE", publisher=fake_unv.publisher)
    check("建玉未確認なら publish しない", not fake_unv.published)

    # publish 失敗は pending に残り、次周期で再送される
    class Flaky(Fake):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.fail = True

        def publisher(self, result):
            if self.fail:
                return False, "HTTP 502"
            return super().publisher(result)

    flaky = Flaky(FILLS[:3], {"verified": True, "qty": 0, "symbol": "MNQU6"})
    journal_f = tj.load_journal(os.path.join(TMP, "none4.json"))
    tj.reconcile_fills(bundle(), accounts=[ACC], fills_query=flaky.fills_query,
                       position_query=flaky.position_query, balance_query=flaky.balance_query,
                       records=LEDGER_ROWS, journal=journal_f, mode="LIVE", publisher=flaky.publisher)
    check("送信失敗は pending に残る", len(journal_f["fills"][ACC]["pending"]) == 1 and not flaky.published)
    flaky.fail = False
    tj.reconcile_fills(bundle(), accounts=[ACC], fills_query=flaky.fills_query,
                       position_query=flaky.position_query, balance_query=flaky.balance_query,
                       records=LEDGER_ROWS, journal=journal_f, mode="LIVE", publisher=flaky.publisher)
    check("次周期で再送され pending が空になる", len(flaky.published) == 1
          and not journal_f["fills"][ACC]["pending"])


test_reconcile()


section("5. realizedPnL の差分 → fees(R54: 基準はフラット時)")


def test_realized_delta():
    """基準を **建玉ゼロのとき**に取る。開いた後に取るとエントリー側の手数料が
    基準に含まれ、差分が決済側だけになる(2026-09-07 に手数料が半分になった原因)。"""
    open_fills = FILLS[:23]
    close_fills = FILLS[23:25]

    # 周期0: まだ何も建っていない。realized -137 が手数料の基準になる。
    fake = Fake([], {"verified": True, "qty": 0, "symbol": "MNQU6"}, realized=-137.0)
    journal = tj.load_journal(os.path.join(TMP, "none5.json"))
    tj.reconcile_fills(bundle(), accounts=[ACC], fills_query=fake.fills_query,
                       position_query=fake.position_query, balance_query=fake.balance_query,
                       records=LEDGER_ROWS, journal=journal, mode="LIVE", publisher=fake.publisher)
    flat = journal["fills"][ACC].get("flat")
    check("フラットの周期に手数料の基準が置かれる", flat and flat["realized"] == -137.0, str(flat))

    # 周期1: SHORT 20 が開く。エントリー手数料は既に realized に入っているので
    # ここで基準を取り直すと決済側しか差分に残らない。動かさないことを確かめる。
    fake.fills = open_fills
    fake.position = {"verified": True, "qty": 20, "side": "SHORT", "symbol": "MNQU6"}
    fake.realized = -157.0
    tj.reconcile_fills(bundle(), accounts=[ACC], fills_query=fake.fills_query,
                       position_query=fake.position_query, balance_query=fake.balance_query,
                       records=LEDGER_ROWS, journal=journal, mode="LIVE", publisher=fake.publisher)
    check("建玉が開いても基準は動かさない",
          journal["fills"][ACC]["flat"]["realized"] == -137.0,
          str(journal["fills"][ACC].get("flat")))

    # 周期2: SL で閉じた。realized -655 → 差分 -518。
    before = len(fake.published)
    fake.fills = open_fills + close_fills
    fake.position = {"verified": True, "qty": 0, "symbol": "MNQU6"}
    fake.realized = -655.0
    notes = tj.reconcile_fills(bundle(), accounts=[ACC], fills_query=fake.fills_query,
                               position_query=fake.position_query, balance_query=fake.balance_query,
                               records=LEDGER_ROWS, journal=journal, mode="LIVE", publisher=fake.publisher)
    last = fake.published[-1]
    check("閉じた周期に 1 件増える", len(fake.published) == before + 1, "/".join(notes))
    # 粗損益: 20 枚 · (29570.5 - 29582.75) × $2 = -$490。純 -518 → fees 28
    check("fees は粗損益と実現損益の差(エントリー側も含む)",
          abs(last.get("fees", 0) - 28.0) < 1e-9, str(last.get("fees")))
    check("閉じたら基準は現在値へ更新される",
          journal["fills"][ACC]["flat"]["realized"] == -655.0,
          str(journal["fills"][ACC].get("flat")))


test_realized_delta()


section("5b. 1 周期に 2 件閉じても手数料が落ちない(R54)")


def test_multi_close_fees():
    """2026-09-07 の実データ。1 周期で 9 枚 × 2 件が閉じ、旧実装は
    ``len(closed) == 1`` の条件から外れて **どちらにも手数料が付かなかった**。"""
    closed = [
        {"side": "LONG", "qty": 9, "entry": 29582.25,
         "legs": [{"qty": 9, "price": 29582.25}]},
        {"side": "SHORT", "qty": 9, "entry": 29585.75,
         "legs": [{"qty": 9, "price": 29581.5}]},
    ]
    # 粗損益 0 + 76.50 = 76.50。ブローカー差分 58.50 → 手数料 18.00 を枚数で折半。
    shares, total = tj.allocate_fees(closed, {"realized": 1440.0, "day": "2026-09-07"},
                                     1498.5, "2026-09-07")
    check("2 件とも手数料が付く", shares == [9.0, 9.0], str(shares))
    check("総額はブローカー差分から出る", abs(total - 18.0) < 1e-9, str(total))

    one = [{"side": "LONG", "qty": 18, "entry": 29542.0,
            "legs": [{"qty": 18, "price": 29582.5}]}]
    shares, total = tj.allocate_fees(one, {"realized": 0.0, "day": "2026-09-07"},
                                     1440.0, "2026-09-07")
    check("18 枚の往復は $18(半分にならない)", shares == [18.0], str(shares))

    check("基準が無ければ手数料を作らない",
          tj.allocate_fees(one, None, 1440.0, "2026-09-07") == (None, None))
    check("残高が取れなければ手数料を作らない",
          tj.allocate_fees(one, {"realized": 0.0, "day": "2026-09-07"}, None,
                           "2026-09-07") == (None, None))
    check("日を跨いだ基準は使わない(realizedPnL は日次リセット)",
          tj.allocate_fees(one, {"realized": 0.0, "day": "2026-09-06"}, 1440.0,
                           "2026-09-07") == (None, None))
    check("純額が粗損益を上回るなら分解しない(有利な滑り)",
          tj.allocate_fees(one, {"realized": 0.0, "day": "2026-09-07"}, 1500.0,
                           "2026-09-07") == (None, None))

    odd = [{"side": "LONG", "qty": 1, "entry": 100.0, "legs": [{"qty": 1, "price": 105.0}]},
           {"side": "LONG", "qty": 1, "entry": 100.0, "legs": [{"qty": 1, "price": 105.0}]},
           {"side": "LONG", "qty": 1, "entry": 100.0, "legs": [{"qty": 1, "price": 105.0}]}]
    shares, total = tj.allocate_fees(odd, {"realized": 0.0, "day": "2026-09-07"},
                                     29.99, "2026-09-07")
    check("端数を落とさない", abs(sum(shares) - total) < 1e-9, f"{shares} vs {total}")


test_multi_close_fees()


section("5c. 残高差分が壊れたときは手数料を作らない(R85)")


def test_realized_guards():
    """2026-09-10 01:26 の実例。01:10:07 に閉じた往復の 63 秒後に次が開き、その間の
    サイクルが「前の損失がまだ反映されていない実現損益」を基準に取った。4 枚の往復
    (粗 -$234・実手数料 $4)に **$173** が載り、LEDGER が口座より $169 少なくなった。"""
    trade = [{"side": "LONG", "qty": 4, "entry": 29421.5,
              "opens": [{"qty": 2, "price": 29421.75}, {"qty": 2, "price": 29421.5}],
              "closes": [{"qty": 4, "price": 29392.25}],
              "legs": [{"qty": 4, "price": 29392.25}]}]
    stale_base = {"realized": 1000.0, "day": "2026-09-10", "sod": 51973.0}
    # 実際の実現損益の動き: 前の往復 -$168 と今回 -$238 が両方ここで反映される。
    shares, total = tj.allocate_fees(trade, stale_base, 1000.0 - 168.0 - 238.0, "2026-09-10",
                                     sod_now=51973.0)
    check("1 枚片道 $21.5 の手数料は作らない(反映遅れの印)", (shares, total) == (None, None),
          f"{shares} {total}")

    healthy = {"realized": 832.0, "day": "2026-09-10", "sod": 51973.0}
    shares, total = tj.allocate_fees(trade, healthy, 832.0 - 238.0, "2026-09-10",
                                     sod_now=51973.0)
    check("基準が正しければ従来どおり配分する($4)", shares == [4.0] and abs(total - 4.0) < 1e-9,
          f"{shares} {total}")

    # JST 日付は同じでも、07:00 前後の取引日リセットを跨ぐと realizedPnL は 0 から数え直す。
    crossed = {"realized": 500.0, "day": "2026-09-10", "sod": 51000.0}
    check("日初純資産が変わったら(取引日を跨いだら)差分を使わない",
          tj.allocate_fees(trade, crossed, -238.0, "2026-09-10", sod_now=51500.0) == (None, None))
    check("sod を持たない古い基準は従来の判定のまま",
          tj.allocate_fees(trade, {"realized": 832.0, "day": "2026-09-10"}, 594.0,
                           "2026-09-10", sod_now=51973.0)[0] == [4.0])


test_realized_guards()


section("7. 手数料 = 約定枚数 × 単価(R85)")


def test_contract_fees():
    rates = tj.fee_rates_from_env({f"FEE_PER_SIDE_{ACC}": "0.50"}, [ACC])
    check("口座別の単価を読む", rates == {ACC: 0.5}, str(rates))
    check("全口座の既定も読む",
          tj.fee_rates_from_env({"FEE_PER_SIDE": "0.35"}, [ACC, "OTHER"]) == {ACC: 0.35, "OTHER": 0.35})
    check("口座別が既定より優先",
          tj.fee_rates_from_env({"FEE_PER_SIDE": "0.35", f"FEE_PER_SIDE_{ACC}": "0.5"}, [ACC])
          == {ACC: 0.5})
    for label, raw in (("数値でない", "abc"), ("負", "-1"), ("あり得ない額", "9")):
        check(f"{label}単価は使わない(R54 へ倒れる)",
              tj.fee_rates_from_env({f"FEE_PER_SIDE_{ACC}": raw}, [ACC]) == {})
    check("未設定なら空", tj.fee_rates_from_env({}, [ACC]) == {})

    # 09-04 の実約定。閉じた 5 往復の約定枚数は 36 / 24 / 12 / 56 / 40。
    fake = Fake(FILLS, {"verified": True, "qty": 14, "side": "SHORT", "symbol": "MNQU6"},
                realized=123456.0)   # 残高差分は壊れた値でも使われないことを見る
    journal = tj.load_journal(os.path.join(TMP, "none_rate.json"))
    notes = tj.reconcile_fills(bundle(), accounts=[ACC], fills_query=fake.fills_query,
                               position_query=fake.position_query,
                               balance_query=fake.balance_query,
                               records=LEDGER_ROWS, journal=journal, mode="LIVE",
                               publisher=fake.publisher, fee_rates={ACC: 0.5})
    fees = [row.get("fees") for row in fake.published]
    check("各往復の手数料が約定枚数 × $0.50", fees == [18.0, 12.0, 6.0, 28.0, 20.0],
          f"{fees} / " + "\n".join(notes))
    check("導出が注記に残る", any("約定 36 枚 × $0.50" in note for note in notes),
          "\n".join(notes))
    check("単価がある口座では「手数料を確定できず」を出さない",
          not any("手数料を確定できず" in note for note in notes))

    # 単価が無い口座で差分も使えない → 手数料なし + 設定を促す注記(黙って gross にしない)
    fake2 = Fake(FILLS[:3], {"verified": True, "qty": 0, "symbol": "MNQU6"})
    journal2 = tj.load_journal(os.path.join(TMP, "none_rate2.json"))
    notes2 = tj.reconcile_fills(bundle(), accounts=[ACC], fills_query=fake2.fills_query,
                                position_query=fake2.position_query,
                                balance_query=fake2.balance_query,
                                records=LEDGER_ROWS, journal=journal2, mode="LIVE",
                                publisher=fake2.publisher)
    check("単価なし・差分なしは手数料なしで記録し、理由を注記に出す",
          len(fake2.published) == 1 and fake2.published[0].get("fees") is None
          and any("手数料を確定できず" in note for note in notes2), "\n".join(notes2))
    check("注記に口座 ID を出さない(通知本文へ流れる)",
          not any(ACC in note for note in notes2), "\n".join(notes2))


test_contract_fees()


section("6. reconcile() の経路選択")


def test_reconcile_entry():
    ledger = os.path.join(TMP, "ledger.jsonl")
    write_ledger(ledger, LEDGER_ROWS)
    journal_path = os.path.join(TMP, "journal.json")
    fake = Fake(FILLS, {"verified": True, "qty": 14, "side": "SHORT", "symbol": "MNQU6"})
    notes = tj.reconcile(bundle(), accounts=[ACC], position_query=fake.position_query,
                         balance_query=fake.balance_query, fills_query=fake.fills_query,
                         journal_path=journal_path, ledger_path=ledger, mode="LIVE",
                         publisher=fake.publisher)
    check("fills_query を注入すれば約定経路で記録される", len(fake.published) == 5, "\n".join(notes))
    saved = tj.load_journal(journal_path)
    check("台帳に fills 状態が保存される", ACC in saved.get("fills", {}))

    # position_query だけ注入(旧テストの形)なら、約定を勝手に取りに行かない
    fake2 = Fake([], {"verified": True, "qty": 0, "symbol": "MNQU6"})
    notes2 = tj.reconcile(bundle(), accounts=[ACC], position_query=fake2.position_query,
                          balance_query=fake2.balance_query,
                          journal_path=os.path.join(TMP, "journal2.json"), ledger_path=ledger,
                          mode="LIVE", publisher=fake2.publisher)
    check("注入無しの fills は呼ばれない(旧経路)", fake2.fills_calls == 0 and not fake2.published, "\n".join(notes2))


test_reconcile_entry()


print()
if FAILED:
    print(f"FAILED ({len(FAILED)}): " + ", ".join(FAILED))
    sys.exit(1)
print("ALL PASS (test_trade_journal_fills)")

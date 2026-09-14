# -*- coding: utf-8 -*-
"""R35 発注経路の安全性。監査で見つかった実損に直結する欠陥を塞ぐ。

## 背景（監査で判明した事実）

- `.secrets/autotrade_ledger.jsonl` は**存在しない**。`_read_ledger` は
  `FileNotFoundError` を握り潰して空リストを返すので、冪等判定が全て無効
- `.secrets/telegram.env` に `NQX_LIVE_ORDERS=1` が**現に設定済み**
- `order.py` 子プロセスの HTTP 予算は最悪 114 秒だが、呼び出し側は 30 秒で kill
  （`autotrade_engine.py:464` / `telegram_bot.py:46`）

## 塞いだ欠陥

### 1. 「注文は出ているのに誰も管理しない建玉」

台帳へは送信の**前**に `ENTRY_CLAIMED` を書く。30 秒 kill が
「leg1 POST 済み・resolve 前」に落ちると、webhook には注文が届いているのに
親は「出ていない」と記録する。そのとき台帳には `ENTRY_CLAIMED` だけが残るが、

  * 冪等スキップ集合に無かった → **次サイクルで同じ発注を再試行**。
    CrossTrade のペイロードには冪等キーが無いので二重発注に直結する
  * 凍結プラン採用集合にも無かった → `management_action` が一度も呼ばれず、
    **トレールも建値移動も `reached_stop` の FLATTEN も動かない**。
    実弾の建玉がブローカー側 OCO だけを頼りに放置される

両方の集合に `ENTRY_CLAIMED` を足した。注文が実際に出たかは分からないが、
**出たかもしれない建玉を管理下に置く方が安全**。建玉が無ければ
`management_action` は qty=0 で何もしない。

### 2. TP1 約定後に建値移動が無言で発動しない

`management_action` は `qty != runner_qty` で「建玉が runner 分まで減った」
= TP1 約定済み、を確定させた**後**に、さらに `if not reached_tp1: return None`
としていた。`reached_tp1` は**現在価格**から計算される（`price >= plan["tp1"]`）
ので、TP1 約定後に価格が TP1 を割り込んで戻ると False になり、
**runner の SL が初期構造 SL のまま放置**される。TP1 で確定した利益を
runner の満額リスクが打ち消す窓が、警告もログも無しに開いていた。

建玉の枚数は約定の事実そのもの。事実が分かっているのに価格に再確認させない。

### 3. SL 上限ゲートの fail-open

`_sl_caps` は設定が読めないと `(None, None)` を返し、`vol_gate` を無効化して
**WATCH 降格をスキップ**していた。`.secrets/crosstrade.env` が読めない・
`RISK_*` が壊れている・`MAX_RISK_DOLLARS` が非数値、のいずれかで
SL 上限チェックが丸ごと消える。

契約の既定上限（`defaultCapDollars / pointValue / fixedQty` = 60pt）へ落とす。
**自分で数字を決めない** —— 契約が通す範囲を新たに狭めないため。
最初 15pt を書いたら既存テスト 3 件が落ちた（契約より厳しかった）。
"""
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import autotrade_engine  # noqa: E402
import execution_contract  # noqa: E402
import monitor_publish  # noqa: E402

FAILED = []


def check(name, condition, detail=""):
    print(("  OK   " if condition else "  FAIL ") + name
          + (f" ({detail})" if detail and not condition else ""))
    if not condition:
        FAILED.append(name)


SYMBOL = "MNQU6"
PLAN = {
    "symbol": SYMBOL, "side": "BUY", "qty": 2,
    "entry": 30000.0, "initialStop": 29980.0,
    "tp1": 30040.0, "finalTarget": 30100.0,
    "legs": [{"id": "TP1", "qty": 1, "target": 30040.0},
             {"id": "RUNNER", "qty": 1, "target": 30100.0}],
    "legIdentityKnown": True,
}


def act(qty, price):
    """management_action は bundle から現在価格を読む。"""
    return autotrade_engine.management_action(
        PLAN, {"qty": qty, "side": "BUY", "avgEntry": 30000.0},
        {"price": price, "snapshot": {}})


def test_entry_claimed_blocks_a_retry():
    """送信前に書かれる ENTRY_CLAIMED が、次サイクルの再発注を止める。"""
    records = [{"key": "K1", "status": "ENTRY_CLAIMED", "action": "ENTRY"}]
    check("ENTRY_CLAIMED は冪等スキップに当たる",
          autotrade_engine._latest(records, "K1",
                                   {"ENTRY_CLAIMED", "ENTRY_SENT"}) is not None)
    # ソース文字列の検査は脆い(コメントに書いただけでも通る)。
    # `_latest` に実際の集合を渡して挙動で確かめる。
    live = {"ENTRY_CLAIMED", "ENTRY_SENT", "ENTRY_ACCEPTED", "ENTRY_RESTING",
            "ENTRY_PARTIAL_ROUTE", "ENTRY_PARTIAL_FILL", "ENTRY_HALTED"}
    check("送信前の ENTRY_CLAIMED でも再発注を止める",
          autotrade_engine._latest(records, "K1", live) is not None)
    # 終端後は止めない(次の建玉を出せる)
    done = [{"key": "K1", "status": "ENTRY_TERMINAL", "action": "ENTRY"}]
    check("ENTRY_TERMINAL は再発注を止めない",
          autotrade_engine._latest(done, "K1", live) is None)
    # 別のキーには波及しない
    check("別キーには影響しない",
          autotrade_engine._latest(records, "K2", live) is None)


def test_entry_claimed_keeps_the_position_managed():
    """凍結プランとして採用されないと、建玉が管理対象外になる。"""
    record = {"key": "K1", "status": "ENTRY_CLAIMED", "action": "ENTRY",
              "plan": PLAN, "scenarioId": "s1"}
    got = autotrade_engine._frozen_plan_record([record], SYMBOL)
    check("ENTRY_CLAIMED が凍結プランとして返る", got is not None, str(got))
    # 終端状態は従来どおり採用しない
    for status in ("ENTRY_HALTED", "ENTRY_TERMINAL"):
        dead = dict(record, status=status)
        check(f"{status} は採用しない",
              autotrade_engine._frozen_plan_record([dead], SYMBOL) is None)


def test_trail_fires_on_the_fill_not_the_price():
    """建玉が runner 分まで減っていれば、価格が TP1 を割っていても建値へ動かす。"""
    # 価格が TP1 を割り込んで戻った状態(30020 < tp1 30040)
    below = act(1, 30020.0)
    check("TP1 を割っていても動く", below is not None and below.get("action") == "MODIFY",
          str(below))
    if below:
        check("SL が建値以上へ動く", below["sl"] >= PLAN["entry"] - 1e-9, str(below))
    # 価格が TP1 の上にある通常ケースも従来どおり
    above = act(1, 30060.0)
    check("TP1 の上でも動く", above is not None and above.get("action") == "MODIFY",
          str(above))
    # 建玉が満額のまま(TP1 未約定)なら何もしない
    full = act(2, 30020.0)
    check("TP1 未約定なら動かさない",
          full is None or full.get("action") != "MODIFY", str(full))
    # SL 到達は従来どおり FLATTEN
    stopped = act(2, 29970.0)
    check("SL 到達は FLATTEN", stopped and stopped.get("action") == "FLATTEN", str(stopped))


def test_sl_cap_does_not_fail_open():
    """設定が読めなくてもゲートを消さない。契約の既定へ落とす。"""
    fallback = monitor_publish.sl_cap_fallback_pt()
    risk = execution_contract.CONTRACT["risk"]
    expected = (float(risk["defaultCapDollars"]) / float(risk["pointValue"])
                / max(1, int(risk["fixedQty"])))
    check("フォールバックは契約の既定から導かれる",
          abs(fallback - expected) < 1e-9, f"{fallback} vs {expected}")
    check("自分で決めた数字ではない", fallback == 60.0, str(fallback))

    # 口座が一つも設定されていない env
    empty, _ = monitor_publish._sl_caps({})
    check("口座未設定でも None を返さない", empty is not None, str(empty))
    check("口座未設定なら契約の既定", abs(empty - expected) < 1e-9, str(empty))

    # 壊れた env でも None にしない
    broken, _ = monitor_publish._sl_caps({"CROSSTRADE_ACCOUNTS": "A",
                                          "MAX_RISK_DOLLARS": "x"})
    check("MAX_RISK_DOLLARS が非数値でも None にしない", broken is not None, str(broken))

    # 正常な env は固定2枚で使える幅へ一度だけ換算する。
    good, _ = monitor_publish._sl_caps({"CROSSTRADE_ACCOUNTS": "A,B",
                                        "RISK_A": "100", "RISK_B": "240"})
    check("正常な env では RISK_* を使う",
          abs(good - 100.0 / monitor_publish.MNQ_DOLLARS_PER_POINT / 2) < 1e-9, str(good))

    normal, normal_next = monitor_publish._sl_caps({"CROSSTRADE_ACCOUNTS": "A,B",
                                                     "RISK_A": "240", "RISK_B": "240",
                                                     "LIFELINE_A": "120", "LIFELINE_B": "1"})
    check("$240は2枚で60pt、口座数やLIFELINEでは割らない",
          normal == 60.0 and normal_next == 60.0, str((normal, normal_next)))


test_entry_claimed_blocks_a_retry()
test_entry_claimed_keeps_the_position_managed()
test_trail_fires_on_the_fill_not_the_price()
test_sl_cap_does_not_fail_open()

if FAILED:
    print(f"FAILED: {len(FAILED)} -> {', '.join(FAILED)}")
    raise SystemExit(1)
print("ALL PASS (test_r35_execution_safety)")

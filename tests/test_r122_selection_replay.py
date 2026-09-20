# -*- coding: utf-8 -*-
"""R122 選択層の逐次再生(`replay_selection.sequential`)の回帰。

`docs/reports/R122_SELECTION_REVIEW_2026-09-20.md` が再現した 3 件の欠陥を、
**本番モジュールをそのまま import して**固定する。再生は読むだけなので、この試験も
ネットワーク・`.secrets`・本番の記録に触れない。

  1. 占有は**実際に武装した周期**で判定する。WATCH で最初に現れた時刻は使わない。
     早い周期が塞がっていても、同じ decisionId が後の周期でまだ武装していれば採る。
  2. 指値を出して待っている間も枠を持つ(未約定のまま TP1 先着 / rest 期限切れ)。
  3. 凍結するのは**その周期の** Entry / SL / TP。WATCH 周期の目標を流用しない。

あわせて `entry_depth.simulate` が非約定の結果にも `attempted` / `cancelT` を返すこと
(既存キーは変えていないこと)を見る。
"""
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import entry_depth  # noqa: E402
import replay_selection as rs  # noqa: E402

BAR = 180
failures = []


def check(label, condition, detail=""):
    if not condition:
        failures.append(label)
        print(f"FAIL {label}: {detail}")
        return
    print(f"OK   {label}")


def bar(t, o, h, l, c):
    return {"t": t, "o": o, "h": h, "l": l, "c": c}


def row(t, model, entry, stop, targets, state="ARMED", price=None, decision_id=None):
    return {"t": t, "at": f"2026-01-01T00:{t // 60:02d}:00+00:00",
            "price": entry if price is None else price, "noise": 1.0,
            "variants": {"V": {"model": model, "side": "BUY", "entry": entry, "stop": stop,
                               "targets": targets, "state": state, "grade": "A",
                               "hardBlockers": [],
                               "decisionId": decision_id or (model + str(t))}}}


def run(rows, bars, guard_n=None, rest_sec=1800, cost_pt=0.0):
    return rs.sequential(rows, "V", bars, [b["t"] for b in bars], guard_n, rest_sec, cost_pt)


# ------------------------------------------------ 1. 武装時刻で占有を判定する

def test_armed_time_not_first_seen():
    """WATCH で先に現れた時刻ではなく、**武装した周期**で枠を取る(レビュー §1)。"""
    rows = [row(0, "OLD", 100, 95, [105, 110]),
            row(180, "NEW", 105, 100, [115, 120], "WATCH"),
            row(600, "NEW", 105, 100, [115, 120])]
    bars = [bar(0, 100, 101, 99, 100), bar(180, 100, 106, 99, 104), bar(360, 104, 111, 101, 110),
            bar(540, 110, 111, 104, 105), bar(600, 105, 108, 103, 107),
            bar(780, 107, 116, 106, 115), bar(960, 115, 121, 110, 120)]
    out = run(rows, bars)
    check("WATCH の周期は発注の機会として数えない",
          out["attempts"] == 2, [a["cycles"][0]["t"] for a in rs.order_attempts(rows, "V")])
    check("先行が決済したあとに武装した候補を取りこぼさない",
          out["taken"] == 2 and out["busySkipped"] == 0, out)
    new = [t for t in out["trades"] if t["model"] == "NEW"]
    check("NEW は武装した 600 で発注される(WATCH の 180 ではない)",
          len(new) == 1 and new[0]["t"] == 600, out["trades"])
    record = out["table"].get("NEW600")
    check("NEW の約定は 600 以降の足",
          record and record["res"].get("fillT") == 600, record and record["res"])


def test_busy_blocks_only_while_occupied():
    """枠が塞がっている間だけ見送り、空いた周期で採る。"""
    rows = [row(0, "OLD", 100, 95, [105, 110]),
            row(180, "NEW", 105, 100, [115, 120]),      # 先行の保有中
            row(600, "NEW", 105, 100, [115, 120], decision_id="NEW180")]
    bars = [bar(0, 100, 101, 99, 100), bar(180, 100, 106, 99, 104), bar(360, 104, 111, 101, 110),
            bar(540, 110, 111, 104, 105), bar(600, 105, 108, 103, 107),
            bar(780, 107, 116, 106, 115), bar(960, 115, 121, 110, 120)]
    out = run(rows, bars)
    new = [t for t in out["trades"] if t["model"] == "NEW"]
    check("同じ decisionId は塞がっていた周期を飛ばし、空いた周期で 1 度だけ発注する",
          out["taken"] == 2 and len(new) == 1 and new[0]["t"] == 600, out["trades"])


# ----------------------------------- 2. 注文を出して待っている間も枠を持つ

def test_resting_order_holds_the_slot():
    """未約定のまま TP1 へ先着した指値は、取消まで枠を持つ(レビュー §2)。"""
    rows = [row(0, "PENDING", 100, 95, [110, 120], price=105),
            row(180, "OVERLAP", 105, 100, [107, 109])]
    bars = [bar(0, 105, 106, 104, 105), bar(180, 105, 108, 102, 105),
            bar(360, 105, 109, 102, 107), bar(540, 107, 110, 102, 110)]
    out = run(rows, bars)
    pending = out["table"].get("PENDING0")
    check("先行の指値は TP1 先着で取消(TP1_FIRST)",
          pending and pending["res"]["outcome"] == "TP1_FIRST", pending and pending["res"])
    check("取消までの間に別の候補を発注しない",
          out["taken"] == 1 and out["filled"] == 0 and out["resting"] == 1, out)
    check("枠不足として数える", out["busySkipped"] == 1, out)


def test_rest_expiry_holds_the_slot():
    """rest 期限切れ(NO_FILL)も、期限まで枠を持つ。"""
    # TP1 は平坦な足では届かない位置に置く(NO_FILL を作るため)。
    rows = [row(0, "PENDING", 90, 85, [120, 130], price=100),
            row(180, "OVERLAP", 100, 95, [105, 110])]
    bars = [bar(t, 100, 100.5, 99.5, 100) for t in range(0, 1800, 180)]
    out = run(rows, bars, rest_sec=900)
    pending = out["table"].get("PENDING0")
    check("届かない指値は rest 期限で取消(NO_FILL)",
          pending and pending["res"]["outcome"] == "NO_FILL", pending and pending["res"])
    check("rest 期限までは別の候補を発注しない", out["taken"] == 1, out)


def test_simulate_returns_cancel_timing():
    """`entry_depth.simulate` が非約定の結果にも注文の有無と取消時刻を返す。"""
    bars = [bar(0, 105, 106, 104, 105), bar(180, 105, 108, 102, 105),
            bar(360, 105, 109, 102, 107), bar(540, 107, 110, 102, 110)]
    tp1 = entry_depth.simulate("BUY", 100.0, 95.0, [110.0, 120.0], bars, 0, 1800,
                               times=[b["t"] for b in bars])
    check("TP1 先着は attempted=True と cancelT を返す",
          tp1["outcome"] == "TP1_FIRST" and tp1["attempted"] is True
          and tp1["cancelT"] == 540 + BAR, tp1)
    flat = [bar(t, 100, 100.5, 99.5, 100) for t in range(0, 1800, 180)]
    nofill = entry_depth.simulate("BUY", 90.0, 85.0, [120.0, 130.0], flat, 0, 900,
                                  times=[b["t"] for b in flat])
    check("rest 期限切れは attempted=True と cancelT=start+rest",
          nofill["outcome"] == "NO_FILL" and nofill["attempted"] is True
          and nofill["cancelT"] == 900, nofill)
    nobars = entry_depth.simulate("BUY", 100.0, 95.0, [110.0, 120.0], flat, 10_000_000, 900,
                                  times=[b["t"] for b in flat])
    check("判定材料が無い周期は attempted=False(注文を出していない)",
          nobars["outcome"] == "NO_BARS" and nobars["attempted"] is False, nobars)
    filled = entry_depth.simulate("BUY", 97.25, 90.0, [110.0, 120.0],
                                  [bar(0, 100, 101, 99, 100), bar(180, 100, 100.5, 97.0, 98),
                                   bar(360, 98, 112, 97.5, 111), bar(540, 111, 125, 110, 124)],
                                  1, 1800)
    check("既存キー(outcome / r / tp1 / fillT / exitT / clearancePt)は変わらない",
          filled["outcome"] == "FILLED" and filled["tp1"] is True
          and all(k in filled for k in ("r", "points", "clearancePt", "fillT", "exitT")), filled)


# --------------------------------------- 3. 武装した周期の TP だけを凍結する

def test_frozen_targets_come_from_the_armed_cycle():
    """WATCH 周期の目標を、後から武装した計画へ流用しない(レビュー §3)。"""
    rows = [row(0, "SAME", 100, 95, [110, 120], "WATCH", decision_id="D1"),
            row(180, "SAME", 100, 95, [105, 115], decision_id="D1")]
    attempts = rs.order_attempts(rows, "V")
    check("WATCH の周期は発注の機会に入らない",
          len(attempts) == 1 and len(attempts[0]["cycles"]) == 1, attempts)
    check("凍結する目標は武装した周期のもの",
          attempts[0]["cycles"][0]["targets"] == [105.0, 115.0], attempts[0]["cycles"][0])
    bars = [bar(0, 100, 101, 99, 100), bar(180, 100, 101, 99, 100),
            bar(360, 100, 100.5, 96.0, 99), bar(540, 99, 106, 98, 105),
            bar(720, 105, 116, 104, 115)]
    out = run(rows, bars)
    trade = out["trades"][0] if out["trades"] else None
    check("再生も武装した周期の TP を使う",
          trade and trade["targets"] == [105.0, 115.0], trade)


def test_decision_id_is_the_dedupe_key():
    """同じ decisionId は 1 度だけ発注する(本番の重複防止と同じ)。"""
    rows = [row(0, "SAME", 100, 95, [105, 110], decision_id="D1"),
            row(180, "SAME", 100, 95, [105, 110], decision_id="D1"),
            row(360, "SAME", 100, 95, [105, 110], decision_id="D1")]
    attempts = rs.order_attempts(rows, "V")
    check("3 周期武装していても発注の機会は 1 件",
          len(attempts) == 1 and len(attempts[0]["cycles"]) == 3, attempts)
    bars = [bar(t, 100, 106, 99, 105) for t in range(0, 1800, 180)]
    out = run(rows, bars)
    check("発注は 1 回だけ", out["taken"] == 1, out)


def test_market_stop_guard_skips_only_that_cycle():
    """R90 穴 2 は**その周期だけ**見送る(計画ごと捨てない)。"""
    rows = [row(0, "G", 100, 99.5, [105, 110], price=100),      # SL まで 0.5pt < 1.0N
            row(180, "G", 100, 90.0, [105, 110], price=100, decision_id="G0")]
    bars = [bar(t, 100, 106, 99, 105) for t in range(0, 1800, 180)]
    out = run(rows, bars, guard_n=1.0)
    check("SL が近すぎる周期は見送るが、同じ計画の次の周期で発注する",
          out["taken"] == 1 and out["trades"] and out["trades"][0]["t"] == 180, out["trades"])


def main():
    tests = [value for key, value in sorted(globals().items()) if key.startswith("test_")]
    for test in tests:
        print(f"\n--- {test.__name__} ---")
        test()
    print()
    if failures:
        print(f"FAILED {len(failures)}: " + ", ".join(failures))
        return 1
    print("ALL PASS (test_r122_selection_replay)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

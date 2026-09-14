# -*- coding: utf-8 -*-
"""paper_trades.py — 出たが発注しなかったシナリオの架空評価(R55)。

守る性質:
  1. 取引日は JST 07:00 区切り(dayguard と同じ)
  2. 連続する周期は (model, side, entry) で 1 セットアップにまとめ、最初のプランを凍結する
  3. 約定を待つのは「最後にその形が出た周期 + TTL(15 分)」まで。それ以降は NO_FILL
  4. 同じ足が SL と TP に触れたら SL を先に取り、`ambiguous` を立てる
  5. TP1 到達後は runner の SL が建値へ寄る
  6. 実弾になった scenarioId は架空にしない
  7. 相関する撃ち直しは塊としても数えられる
  8. 保存は paperId で置き換え、**実弾の台帳には触れない**

    python tests/test_paper_trades.py
"""
import io
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
import paper_trades as pt  # noqa: E402

PASS = [0]
FAIL = [0]
TMP = tempfile.mkdtemp(prefix="paper_trades_test_")
UTC = timezone.utc


def check(label, condition, detail=""):
    if condition:
        PASS[0] += 1
        print(f"  OK   {label}")
    else:
        FAIL[0] += 1
        print(f"  NG   {label}  {detail}")


def at(hh, mm, day=7):
    return datetime(2026, 9, day, hh, mm, tzinfo=pt.JST)


def bar(moment, high, low, close=None):
    return {"t": int(moment.timestamp()), "h": high, "l": low,
            "c": close if close is not None else (high + low) / 2}


def plan(model="VP80_REVERSION", side="SELL", entry=29600.0, stop=29620.0,
         targets=(29570.0, 29500.0), state="ARMED", grade="A", sid="s1"):
    return {"model": model, "side": side, "entry": entry, "stop": stop,
            "targets": list(targets), "state": state, "grade": grade,
            "scenarioId": sid, "qty": 2}


def setup(**kwargs):
    cycles = kwargs.pop("cycles", [(at(20, 0), plan(**kwargs))])
    return pt.group_setups([(moment, {"scenarios": {"primary": p}}) for moment, p in cycles])[0]


# ----------------------------------------------------------------- 1. 取引日
print("\n1. 取引日は JST 07:00 区切り")
check("06:59 は前日", pt.trading_day(at(6, 59, day=8)) == "2026-09-07")
check("07:00 は当日", pt.trading_day(at(7, 0, day=8)) == "2026-09-08")
# 21:00 UTC = 翌 06:00 JST → まだ 09-07 の取引日。22:00 UTC = 翌 07:00 JST → 09-08 へ切り替わる。
check("UTC でも JST へ寄せる(06:00 JST は前日のまま)",
      pt.trading_day(datetime(2026, 9, 7, 21, 0, tzinfo=UTC)) == "2026-09-07")
check("UTC でも JST へ寄せる(07:00 JST で切り替わる)",
      pt.trading_day(datetime(2026, 9, 7, 22, 0, tzinfo=UTC)) == "2026-09-08")


# --------------------------------------------------------------- 2. まとめ方
print("\n2. 連続する周期は 1 セットアップ")
cycles = [(at(20, 0), plan(sid="a", stop=29620.0)),
          (at(20, 3), plan(sid="b", stop=29621.0)),      # SL だけ動く = 同じ形
          (at(20, 6), plan(sid="c", entry=29610.0))]     # 建値が変わる = 別の形
grouped = pt.group_setups([(m, {"scenarios": {"primary": p}}) for m, p in cycles])
check("SL だけ動く再掲は 1 件にまとまる", len(grouped) == 2, str(len(grouped)))
check("プランは最初の周期を凍結する", grouped[0]["stop"] == 29620.0, str(grouped[0]["stop"]))
check("周期数を数えている", grouped[0]["cycles"] == 2)
check("scenarioId を全部持つ", grouped[0]["scenarioIds"] == ["a", "b"])
check("最後に出た時刻を持つ", grouped[0]["lastAt"] == at(20, 3))

states = pt.group_setups([(at(20, 0), {"scenarios": {"primary": plan(state="WATCH")}})])[0]
check("WATCH 止まりは armed=False", states["armed"] is False)
best = pt.group_setups([(at(20, 0), {"scenarios": {"primary": plan(grade="B", sid="x")}}),
                        (at(20, 3), {"scenarios": {"primary": plan(grade="A+", sid="y")}})])[0]
check("等級は一番高いものを採る", best["bestGrade"] == "A+", str(best["bestGrade"]))


# ------------------------------------------------------------- 3. 約定の期限
print("\n3. 約定を待つのは TTL まで")
s = setup()
late = [bar(at(20, 3), 29590.0, 29580.0),                       # 建値に触れない
        bar(at(20, 30), 29605.0, 29595.0)]                      # TTL(20:15)より後
check("TTL を過ぎた足では約定しない",
      pt.simulate(s, late)["outcome"] == "NO_FILL", str(pt.simulate(s, late)))
early = [bar(at(20, 3), 29601.0, 29595.0), bar(at(20, 6), 29575.0, 29569.0)]
result = pt.simulate(s, early)
check("TTL 内に建値へ触れれば約定する", result["outcome"] == "WIN", str(result))
# 公開より前の足しか無ければ、そもそも使える足が無い(NO_BARS)。過去の足で約定させない。
check("公開時刻より前の足は使わない",
      pt.simulate(s, [bar(at(19, 57), 29650.0, 29500.0)])["outcome"] == "NO_BARS",
      str(pt.simulate(s, [bar(at(19, 57), 29650.0, 29500.0)])))


# --------------------------------------------------------- 4. 同じ足の優先順位
print("\n4. 同じ足で SL と TP に触れたら SL")
both = [bar(at(20, 3), 29625.0, 29560.0)]                       # 建値・SL・TP1 全部を含む足
result = pt.simulate(setup(), both)
check("不利側(SL)を採る", result["outcome"] == "LOSS", str(result))
check("R は −1.00", abs(result["rMultiple"] + 1.0) < 1e-9, str(result["rMultiple"]))
check("同足だった印が立つ", result["ambiguous"] is True)


# ------------------------------------------------------- 5. TP1 後は建値保護
print("\n5. TP1 到達後は runner の SL が建値へ")
bars = [bar(at(20, 3), 29601.0, 29598.0),                       # 約定
        bar(at(20, 6), 29602.0, 29569.0),                       # TP1 到達
        bar(at(20, 9), 29601.0, 29595.0)]                       # 建値へ戻る = runner は建値撤退
result = pt.simulate(setup(), bars)
ids = [leg["id"] for leg in result["legs"]]
check("TP1 と RUNNER の 2 脚になる", ids == ["TP1", "RUNNER"], str(ids))
check("runner は建値で落ちる", result["legs"][1]["exit"] == 29600.0, str(result["legs"][1]))
check("元の SL(29620)では落ちない", result["outcome"] == "WIN", str(result))
check("tp1Reached が立つ", result["tp1Reached"] is True)

runner_bars = bars[:2] + [bar(at(20, 9), 29560.0, 29495.0)]     # 最終ターゲットへ
result = pt.simulate(setup(), runner_bars)
check("最終ターゲットまで届けば TP2 脚",
      [leg["id"] for leg in result["legs"]] == ["TP1", "TP2"], str(result["legs"]))


# --------------------------------------------------------------- 6. 期限切れ
print("\n6. 期限内に決着しなければ OPEN")
flat = [bar(at(20, 3), 29601.0, 29598.0)] + [
    bar(at(20, 3) + timedelta(minutes=3 * i), 29605.0, 29595.0, 29599.0) for i in range(1, 90)]
result = pt.simulate(setup(), flat, horizon_min=30)
check("OPEN として最終足の終値で評価", result["outcome"] == "OPEN", str(result["outcome"]))
check("OPEN 脚が入る", result["legs"][-1]["id"] == "OPEN")


# ------------------------------------------------------------- 7. 実弾を外す
print("\n7. 実弾になった形は架空にしない")
card = os.path.join(TMP, "scorecard.jsonl")
io.open(card, "w", encoding="utf-8").write(
    json.dumps({"resultId": "rs_x", "scenarioId": "traded"}) + "\n")
check("scorecard の scenarioId を拾う", pt.traded_scenarios(card) == {"traded"})
check("scorecard が無ければ空", pt.traded_scenarios(os.path.join(TMP, "none.jsonl")) == set())


# --------------------------------------------------------------- 8. 塊の判定
print("\n8. 相関する撃ち直しは塊にまとまる")
rows = [{"paperId": "p1", "model": "VP80_REVERSION", "side": "SELL",
         "publishedAt": at(20, 0).isoformat()},
        {"paperId": "p2", "model": "VP80_REVERSION", "side": "SELL",
         "publishedAt": at(20, 6).isoformat()},
        {"paperId": "p3", "model": "VP80_REVERSION", "side": "SELL",
         "publishedAt": at(21, 0).isoformat()},          # 20 分以上あいた = 別の塊
        {"paperId": "p4", "model": "VP80_REVERSION", "side": "BUY",
         "publishedAt": at(20, 6).isoformat()}]          # 方向が違う = 別の塊
pt.assign_clusters(rows)
check("6 分後の再掲は同じ塊", rows[1]["clusterId"] == "p1" and rows[1]["clusterFirst"] is False)
check("1 時間後は別の塊", rows[2]["clusterId"] == "p3" and rows[2]["clusterFirst"] is True)
check("方向が違えば別の塊", rows[3]["clusterId"] == "p4")

summary_rows = [{"armed": True, "clusterFirst": True, "outcome": "WIN", "rMultiple": 2.0},
                {"armed": True, "clusterFirst": False, "outcome": "LOSS", "rMultiple": -1.0},
                {"armed": False, "clusterFirst": True, "outcome": "WIN", "rMultiple": 5.0}]
check("ARMED 以外は集計に入らない",
      pt.summarize(summary_rows)["setups"] == 2)
check("塊の代表だけなら 1 件", pt.summarize(summary_rows, cluster_only=True)["setups"] == 1)
check("損失が無ければ PF は None(∞ を作らない)",
      pt.summarize(summary_rows, cluster_only=True)["pf"] is None)
check("PF は R で計算する", pt.summarize(summary_rows)["pf"] == 2.0)


# ----------------------------------------------------------------- 9. 保存
print("\n9. 保存は paperId で置き換え")
path = os.path.join(TMP, "paper.jsonl")
pt.write_rows([{"paperId": "a", "rMultiple": 1.0}, {"paperId": "b", "rMultiple": 2.0}], path)
pt.write_rows([{"paperId": "a", "rMultiple": 9.0}], path)
saved = [json.loads(line) for line in io.open(path, encoding="utf-8").read().splitlines() if line]
check("同じ paperId は 1 行", len(saved) == 2, str(len(saved)))
check("後から書いた方が残る",
      next(r for r in saved if r["paperId"] == "a")["rMultiple"] == 9.0)
check("実弾の台帳とは別ファイル",
      os.path.basename(pt.PAPER_FILE) == "paper_trades.jsonl"
      and pt.PAPER_FILE != pt.SCORECARD_FILE)

# ------------------------------------------------- 10. 時系列で 1 本ずつ
print("\n10. 時系列は建玉が 1 つずつという制約を入れる")


def seq_row(pid, hh, mm, outcome="WIN", r=1.0, closed=None, tp1=True, model="M", side="SELL"):
    row = {"paperId": pid, "model": model, "side": side, "armed": True,
           "publishedAt": at(hh, mm).isoformat(), "lastAt": at(hh, mm).isoformat(),
           "outcome": outcome, "tp1Reached": tp1}
    if outcome not in {"NO_FILL", "NO_BARS"}:
        row["rMultiple"] = r
        row["closedAt"] = (closed or at(hh, mm) + timedelta(minutes=30)).isoformat()
    return row


rows = [seq_row("s1", 20, 0, closed=at(20, 30)),      # 20:00-20:30 建玉
        seq_row("s2", 20, 15),                        # 建玉中 → 取れない
        seq_row("s3", 20, 45, outcome="LOSS", r=-1.0, closed=at(21, 0), tp1=False),
        seq_row("s4", 20, 50)]                        # また建玉中 → 取れない
sequenced = pt.sequence_day(rows)
taken = [row["paperId"] for row in sequenced if row["sequence"] == "TAKEN"]
check("建玉中に出た形は取れない", taken == ["s1", "s3"], str(taken))
check("取れなかった側に印が付く",
      [r["paperId"] for r in sequenced if r["sequence"] == "SKIPPED_BUSY"] == ["s2", "s4"])

summary = pt.sequence_summary(sequenced)
check("取れた件数", summary["taken"] == 2, str(summary))
check("取れなかった件数", summary["skippedBusy"] == 2)
check("累積 R は取れた分だけ", abs(summary["rSum"] - 0.0) < 1e-9, str(summary["rSum"]))
check("谷も記録する", abs(summary["rTrough"] + 0.0) < 1e-9 or summary["rTrough"] <= 0.0)
check("最初に TP1 を取った順番", summary["firstTp1Index"] == 1, str(summary["firstTp1Index"]))

# 約定しなかった形も、指値が生きている間(最後に出た周期 + TTL)は次を塞ぐ
nofill = [seq_row("n1", 20, 0, outcome="NO_FILL"), seq_row("n2", 20, 10),
          seq_row("n3", 20, 20)]
seq2 = pt.sequence_day(nofill)
check("不成立でも TTL の間は次を取れない",
      [r["paperId"] for r in seq2 if r["sequence"] == "TAKEN"] == ["n1", "n3"],
      str([(r["paperId"], r["sequence"]) for r in seq2]))

check("WATCH 止まりは時系列に入らない",
      pt.sequence_day([{**seq_row("w", 20, 0), "armed": False}]) == [])

check("TP1 に一度も届かなければ None",
      pt.sequence_summary(pt.sequence_day(
          [seq_row("x", 20, 0, outcome="LOSS", r=-1.0, tp1=False)]))["firstTp1Index"] is None)


# --------------------------------------------------------- 11. 時間帯の区分
print("\n11. 時間帯は JST で切る")
check("07:00 はアジア", pt.session_of(at(7, 0)) == "アジア")
check("15:59 はアジア", pt.session_of(at(15, 59)) == "アジア")
check("16:00 はロンドン", pt.session_of(at(16, 0)) == "ロンドン")
check("22:00 は NY", pt.session_of(at(22, 0)) == "NY")
check("深夜 02:00 も NY", pt.session_of(at(2, 0, day=8)) == "NY")
check("UTC 入力でも JST で切る(09:00 UTC = 18:00 JST)",
      pt.session_of(datetime(2026, 9, 7, 9, 0, tzinfo=UTC)) == "ロンドン")
check("UTC 入力でも JST で切る(13:00 UTC = 22:00 JST = NY)",
      pt.session_of(datetime(2026, 9, 7, 13, 0, tzinfo=UTC)) == "NY")

buckets = pt.by_session(pt.sequence_day(
    [seq_row("a", 17, 0, closed=at(17, 10)),
     seq_row("b", 23, 0, outcome="LOSS", r=-1.0, closed=at(23, 10), tp1=False)]))
check("時間帯ごとに分かれる", [b["session"] for b in buckets] == ["ロンドン", "NY"], str(buckets))
check("時間帯ごとの R", buckets[0]["rSum"] == 1.0 and buckets[1]["rSum"] == -1.0)

print()
print(f"合計: PASS {PASS[0]} / FAIL {FAIL[0]}")
sys.exit(1 if FAIL[0] else 0)

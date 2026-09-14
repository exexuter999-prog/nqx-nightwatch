# -*- coding: utf-8 -*-
"""R52: ULTRA のドライラン表示は送信行と同じ脚枚数で出る。

2026-09-04 の机上検算(LFE 50K Flex・ULTRA 34枚)で、order.py の dry-run が
`qty=1;` を2行印字し、リワードも1枚ぶんで出ていた。live 経路だけが
ultra_legs(17/17)を使っていたので送信内容は正しかったが、engine が
「dry-run 成功」を live 送信の前提にする以上、人が最後に見る表示が送信内容と
食い違ってはならない。同時に ULTRA エンベロープの検査(残DD 超過・枚数上限)が
`--confirm` の後ろにあり、dry-run では一切効いていなかった。

すべてドライラン。--confirm は一度も付けない。

    python tests/test_r52_ultra_dryrun_display.py
"""
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _hermetic import make_sandbox, dry_run

SANDBOX = make_sandbox(BASE)
PASS = 0
FAIL = 0

ENV = ("CROSSTRADE_URL=http://127.0.0.1:9/blocked\n"
       "CROSSTRADE_KEY=TEST\n"
       "CROSSTRADE_DEST=TEST\n"
       "CROSSTRADE_ACCOUNTS=ACC-ULTRA-01\n"
       "RISK_ACC-ULTRA-01=200\n"
       "MAX_RISK_DOLLARS=200\n")

with open(os.path.join(SANDBOX, ".secrets", "crosstrade.env"), "w", encoding="utf-8") as fh:
    fh.write(ENV)


def check(title, ok_expected, args, must=(), must_not=(), count=()):
    global PASS, FAIL
    ok, out = dry_run(SANDBOX, args)
    problems = []
    if ok != ok_expected:
        problems.append(f"終了コード: 期待={'0' if ok_expected else '非0'} 実際={'0' if ok else '非0'}")
    for s in must:
        if s not in out:
            problems.append(f"出力に無い: {s!r}")
    for s in must_not:
        if s in out:
            problems.append(f"出力に有る: {s!r}")
    for s, n in count:
        actual = out.count(s)
        if actual != n:
            problems.append(f"出現回数: {s!r} 期待={n} 実際={actual}")
    if problems:
        FAIL += 1
        print(f"NG   {title}")
        for p in problems:
            print(f"       {p}")
        for line in out.splitlines():
            print("       | " + line)
    else:
        PASS += 1
        print(f"OK   {title}")


# 幾何: SELL 29255.5 / SL 20pt / TP1 36pt / runner 56pt。34枚 → 17/17。
# 想定損失 34×20×$2 = $1,360。リワード 17×36×2 + 17×56×2 = $3,128 → R:R 1:2.30
ULTRA = ["--side", "sell", "--qty", "34", "--entry", "29255.5", "--sl", "29275.5",
         "--split-tp", "29219.5,29199.5", "--ultra"]

check("ULTRA 34枚: 注文行は qty=17 が2行、qty=1 は出ない",
      True, ULTRA + ["--ultra-drawdown", "2000"],
      must=["ULTRA: 34枚 → TP1 17枚 / RUNNER 17枚", "リスク: $1360.00", "リワード: $3128.00",
            "※ ULTRA 34枚は TP1 17枚 / RUNNER 17枚", "[ドライラン]"],
      must_not=["qty=1;"],
      count=[("qty=17;", 2)])

check("ULTRA 残DD 不足はドライランでも止まる(以前は --confirm 後だけ)",
      False, ULTRA + ["--ultra-drawdown", "1000"],
      must=["ULTRA_DRAWDOWN_EXCEEDED"], must_not=["[ドライラン]"])

check("ULTRA --ultra-drawdown なしはドライランでも止まる",
      False, ULTRA, must=["ULTRA_DRAWDOWN_REQUIRED"], must_not=["[ドライラン]"])

# 奇数枚は runnerRemainder=true で端数を runner へ寄せる(33 → 16/17)。表示も同じ。
check("ULTRA 奇数枚 33 は TP1 16 / RUNNER 17 で表示も一致",
      True, ["--side", "sell", "--qty", "33", "--entry", "29255.5", "--sl", "29275.5",
             "--split-tp", "29219.5,29199.5", "--ultra", "--ultra-drawdown", "2000"],
      must=["ULTRA: 33枚 → TP1 16枚 / RUNNER 17枚", "※ ULTRA 33枚は TP1 16枚 / RUNNER 17枚"],
      must_not=["qty=1;"], count=[("qty=16;", 1), ("qty=17;", 1)])

check("ULTRA 成行(--last)でも想定損失は滑り緩衝込みで出て落ちない",
      True, ["--market", "--side", "sell", "--qty", "34", "--sl", "29275.5", "--last", "29255.5",
             "--split-tp", "29219.5,29199.5", "--ultra", "--ultra-drawdown", "2000"],
      must=["ULTRA: 34枚 → TP1 17枚 / RUNNER 17枚", "order_type=MARKET"],
      must_not=["Traceback"], count=[("qty=17;", 2)])

check("通常経路 2枚は従来どおり各脚 qty=1",
      True, ["--side", "sell", "--qty", "2", "--entry", "29255.5", "--sl", "29275.5",
             "--split-tp", "29219.5,29199.5"],
      must=["リスク: $80.00", "リワード: $184.00", "※ 2枚は各1枚の独立OCOブラケット"],
      must_not=["ULTRA:"], count=[("qty=1;", 2)])

print()
print(f"合計: PASS {PASS} / FAIL {FAIL}")
sys.exit(1 if FAIL else 0)

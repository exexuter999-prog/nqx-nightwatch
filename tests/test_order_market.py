# -*- coding: utf-8 -*-
"""成行のリスク概算(--last)と多口座判定を隔離環境で検証する。

2026-08-16 の再設計:
- 成行は --last(発注直前に観測した現在値)が必須。リスクは
  (|現在値 − SL| + 滑り緩衝 MARKET_SLIPPAGE_PT) × 枚数 × $2.00 で概算し、
  指値と同じ口座別上限(RISK_<口座ID>)で発注先を振り分ける
- 指値に --last を添えると、即約定側の指値(実質成行)を拒否する

すべてドライラン。--confirm は一度も付けない。
"""
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _hermetic import make_sandbox, dry_run

SANDBOX = make_sandbox(BASE)
PASS = 0
FAIL = 0

COMMON = ("CROSSTRADE_URL=http://127.0.0.1:9/blocked\n"
          "CROSSTRADE_KEY=TEST\n"
          "CROSSTRADE_DEST=TEST\n")
# 本番と同じ構図の3口座(IDはテスト用)。0004 だけ上限 $50。
MULTI = COMMON + ("CROSSTRADE_ACCOUNTS=ACC0002,ACC0003,ACC0004\n"
                  "RISK_ACC0002=60\nRISK_ACC0003=60\nRISK_ACC0004=50\n"
                  "MAX_RISK_DOLLARS=60\n")
SINGLE = COMMON + "CROSSTRADE_ACCOUNT=TEST\nMAX_RISK_DOLLARS=200\n"


def write_env(text):
    with open(os.path.join(SANDBOX, ".secrets", "crosstrade.env"), "w",
              encoding="utf-8") as fh:
        fh.write(text)


def check(title, ok_expected, args, must=(), must_not=()):
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
    if problems:
        FAIL += 1
        print(f"NG   {title}")
        for p in problems:
            print(f"       {p}")
        print("       ---- 出力 ----")
        for line in out.splitlines():
            print("       | " + line)
    else:
        PASS += 1
        print(f"OK   {title}")


print("#" * 68)
print("# 1. --last の必須化(口座数に関係なく)")
print("#" * 68)
write_env(MULTI)
check("多口座 + 成行 + --last なし → 拒否",
      False, ["--market", "--side", "buy", "--qty", "2", "--sl", "29700",
              "--split-tp", "29720,29730"],
      must=["--last"], must_not=["order_type=MARKET"])
write_env(SINGLE)
check("単一口座でも成行 + --last なし → 拒否",
      False, ["--market", "--side", "buy", "--qty", "2", "--sl", "29700",
              "--split-tp", "29720,29730"],
      must=["--last"], must_not=["order_type=MARKET"])
write_env(MULTI)
check("--last が 0 以下 → 拒否",
      False, ["--market", "--side", "buy", "--qty", "2",
              "--sl", "29700", "--last", "0", "--split-tp", "29720,29730"],
      must=["--last"])

print()
print("#" * 68)
print("# 2. 概算リスク = (|現在値 − SL| + 滑り緩衝) × 枚数 × $2.00")
print("#" * 68)
# 9.5pt + 緩衝2pt = 11.5pt × 2枚 × $2 = $46。全口座の上限内。
check("固定2枚・SL 9.5pt → 概算 $46、3口座すべてに発注",
      True, ["--market", "--side", "buy", "--qty", "2",
             "--sl", "29700", "--last", "29709.5", "--split-tp", "29720,29730"],
      must=["リスク概算: $46.00", "滑り緩衝 2pt",
            "発注先 ACC0002", "発注先 ACC0003", "発注先 ACC0004",
            "order_type=MARKET;", "[ドライラン]"],
      must_not=["除外", "limit_price"])
# 10.5pt + 2pt × 2枚 × $2 = $50。上限 $50 は inclusive。
check("概算がちょうど $50 → ACC0004 も参加(inclusive)",
      True, ["--market", "--side", "buy", "--qty", "2",
             "--sl", "29700", "--last", "29710.5", "--split-tp", "29720,29730"],
      must=["リスク概算: $50.00", "発注先 ACC0004"], must_not=["除外"])
# 12.5pt + 2pt × 2枚 × $2 = $58。ACC0004($50)だけ外れる。
check("概算 $58 → ACC0004 だけ除外、残り2口座に発注",
      True, ["--market", "--side", "sell", "--qty", "2",
             "--sl", "29750", "--last", "29737.5", "--split-tp", "29720,29710"],
      must=["リスク概算: $58.00", "除外 ACC0004",
            "発注先 ACC0002", "発注先 ACC0003"])
# 2枚で $116 → 全口座超過 → 見送り。
check("全口座超過 → エラーで見送り",
      False, ["--market", "--side", "sell", "--qty", "2",
              "--sl", "29750", "--last", "29723", "--split-tp", "29710,29700"],
      must=["除外 ACC0002", "除外 ACC0003", "除外 ACC0004", "見送って"],
      must_not=["order_type=MARKET"])

print()
print("#" * 68)
print("# 3. SL/TP の向きは現在値を基準に判定")
print("#" * 68)
check("買いなのに SL が現在値以上 → 拒否(R102 STOP_WRONG_SIDE_OF_MARKET)",
      False, ["--market", "--side", "buy", "--qty", "2",
              "--sl", "29730", "--last", "29721", "--split-tp", "29740,29750"],
      must=["STOP_WRONG_SIDE_OF_MARKET"])
check("売りなのに TP が現在値以上 → 拒否",
      False, ["--market", "--side", "sell", "--qty", "2",
              "--sl", "29750", "--last", "29723", "--split-tp", "29760,29740"],
      must=["売りのTP", "現在値"])

print()
print("#" * 68)
print("# 4. 滑り緩衝は MARKET_SLIPPAGE_PT で変更できる")
print("#" * 68)
write_env(MULTI + "MARKET_SLIPPAGE_PT=5\n")
check("緩衝 5pt → 8pt が固定2枚で概算 $52 になる",
      True, ["--market", "--side", "buy", "--qty", "2",
             "--sl", "29700", "--last", "29708", "--split-tp", "29720,29730"],
      must=["リスク概算: $52.00", "滑り緩衝 5pt"])
write_env(MULTI + "MARKET_SLIPPAGE_PT=abc\n")
check("緩衝が数値でない → 拒否",
      False, ["--market", "--side", "buy", "--qty", "2",
              "--sl", "29700", "--last", "29721", "--split-tp", "29730,29740"],
      must=["MARKET_SLIPPAGE_PT"])
write_env(MULTI)

print()
print("#" * 68)
print("# 5. 指値 + --last: 即約定側の指値(実質成行)を検知")
print("#" * 68)
check("買い指値が現在値以上 → 拒否(2026-08-12 の再発防止)",
      False, ["--side", "buy", "--qty", "2", "--entry", "29760",
              "--sl", "29730", "--last", "29750", "--split-tp", "29780,29800"],
      must=["即約定"], must_not=["order_type=LIMIT"])
check("売り指値が現在値以下 → 拒否",
      False, ["--side", "sell", "--qty", "2", "--entry", "29740",
              "--sl", "29770", "--last", "29750", "--split-tp", "29720,29700"],
      must=["即約定"])
check("正しい側の指値 + --last → 従来どおり通る(緩衝なし)",
      True, ["--side", "buy", "--qty", "2", "--entry", "29740",
             "--sl", "29725", "--last", "29750", "--split-tp", "29760,29780"],
      must=["リスク: $60.00", "除外 ACC0004", "発注先 ACC0002",
            "order_type=LIMIT;", "[ドライラン]"])
check("--last も quote も無い指値は QUOTE_UNAVAILABLE で送らない(R102 fail-closed)",
      False, ["--side", "buy", "--qty", "2", "--entry", "29740",
             "--sl", "29725", "--split-tp", "29760,29780"],
      must=["QUOTE_UNAVAILABLE"], must_not=["order_type=LIMIT;", "[ドライラン]"])

print()
print("#" * 68)
print("# 6. R18 — 単一TP/単枚ランナー経路の廃止")
print("#" * 68)
write_env(SINGLE)


def test_runner_order_sl_only():
    """Claimless single-runner orders are not part of the R18 entry contract."""
    check("SLのみ・単枚の新規ドライランも拒否",
          False, ["--side", "buy", "--qty", "1", "--entry", "29740", "--sl", "29710", "--last", "29750"],
          must=["SPLIT_PLAN_REQUIRED"], must_not=["order_type=LIMIT;", "[ドライラン]"])


test_runner_order_sl_only()


def test_runner_order_no_sl_rejected():
    """SL 省略は TP 省略とは違い、従来どおり拒否される(退行防止)。"""
    check("SL省略は従来どおり拒否(TP省略とは非対称)",
          False, ["--side", "buy", "--qty", "1", "--entry", "29740"],
          must=["--sl"], must_not=["order_type=LIMIT;", "[ドライラン]"])


test_runner_order_no_sl_rejected()

print()
print("#" * 68)
print("# 7. R40 — 多口座の管理は --account で必ず単一口座へ限定")
print("#" * 68)
write_env(MULTI)
check("多口座 --modify は口座指定なしを拒否", False,
      ["--modify", "--side", "buy", "--qty", "1", "--sl", "95", "--tp", "120"],
      must=["ACCOUNT_REQUIRED"])
check("許可リスト内の --account ならmodifyドライラン生成", True,
      ["--modify", "--account", "ACC0002", "--side", "buy", "--qty", "1",
       "--sl", "95", "--tp", "120"],
      must=["account=ACC0002", "[ドライラン]"],
      must_not=["account=ACC0003", "account=ACC0004"])

print()
print("=" * 68)
print(f"合計: PASS {PASS} / FAIL {FAIL}")
print("実際に CrossTrade へ送信した回数: 0(--confirm は一度も付けていない)")
print("=" * 68)
sys.exit(0 if FAIL == 0 else 1)

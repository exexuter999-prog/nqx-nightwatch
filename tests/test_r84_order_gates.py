# -*- coding: utf-8 -*-
"""R84: order.py の `--pyramid` 門(§6)。

差し替えるのは (1) FLAT 要求 → 基礎建玉の一致、(2) リスク上限 → 合成リスク、
(3) 枚数上限 → 追撃後の合計。それ以外の既存ゲートは全部そのまま通ることと、
**新しい門がドライランの return より前にある**(R52 の教訓)ことを固定する。

    python tests/test_r84_order_gates.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _hermetic  # noqa: E402

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
import execution_contract  # noqa: E402
import pyramid  # noqa: E402

PASS = [0]
FAIL = [0]


def check(label, condition, detail=""):
    if condition:
        PASS[0] += 1
        print(f"  OK   {label}")
    else:
        FAIL[0] += 1
        print(f"  NG   {label}  {detail}")


SANDBOX = _hermetic.make_sandbox(BASE)
ADD = ["--side", "sell", "--qty", "6", "--market", "--last", "29448.5",
       "--sl", "29507.5", "--split-tp", "29400,29300", "--ultra",
       "--ultra-drawdown", "2268", "--accounts", "TEST"]
BASE_DECL = "--pyramid-base=2@29484.25"


def run(extra, args=None):
    return _hermetic.dry_run(SANDBOX, (args if args is not None else ADD) + list(extra))


print("--- 追撃の必須フラグ ---")
ok, out = run(["--pyramid", BASE_DECL])
check("正しい組み合わせはドライランを通る", ok, out[-400:])
# SELL は不利側 = 安く売れる。29448.5 - 2.0 = 29446.5 を基準に合成し、
# (2*29484.25 + 6*29446.5)/8 = 29455.9375 → |29507.5 - 29455.9375| * 8 * $2 = $825.00。
check("合成建値と合計リスクがドライランに出る(人が最後に見る表示)",
      "PYRAMID: 基礎 2枚" in out and "合計想定損失 $825.00" in out, out[-500:])
check("滑りを当てた基準価格も表示に出る(数字の出所が追える)",
      "滑り 2pt 込み" in out and "29446.50 基準" in out, out[-500:])

ok, out = run(["--pyramid"])
check("--pyramid-base が無ければ落ちる",
      not ok and "PYRAMID_BASE_REQUIRED" in out, out[-300:])
ok, out = run(["--pyramid", "--pyramid-base=abc"])
check("読めない --pyramid-base は落ちる",
      not ok and "PYRAMID_BASE_REQUIRED" in out, out[-300:])
ok, out = run([BASE_DECL])
check("--pyramid 無しの --pyramid-base は落ちる",
      not ok and "PYRAMID_BASE_WITHOUT_MODE" in out, out[-300:])

# 通常経路(固定 2 枚)でも既存の口座別リスク上限を通る幾何にして、
# 「--ultra が無い」こと**だけ**で落ちることを見る。
no_ultra = ["--side", "sell", "--qty", "2", "--market", "--last", "29448.5",
            "--sl", "29470.0", "--split-tp", "29400,29300", "--accounts", "TEST"]
ok, out = run(["--pyramid", BASE_DECL], args=no_ultra)
check("--ultra が無ければ落ちる",
      not ok and "PYRAMID_REQUIRES_ULTRA" in out, out[-300:])

limit_args = ["--side", "sell", "--qty", "6", "--entry", "29500", "--sl", "29507.5",
              "--split-tp", "29400,29300", "--ultra", "--ultra-drawdown", "2268",
              "--accounts", "TEST"]
ok, out = run(["--pyramid", BASE_DECL], args=limit_args)
check("指値の追撃は落ちる(v1 は成行のみ)",
      not ok and "PYRAMID_MARKET_ONLY" in out, out[-300:])

ok, out = run(["--pyramid", BASE_DECL, "--modify"])
check("--modify との併用は落ちる",
      not ok and "PYRAMID_MODE_CONFLICT" in out, out[-300:])
ok, out = run(["--pyramid", BASE_DECL, "--flatten"])
check("--flatten との併用は落ちる(撤退経路を汚さない)",
      not ok and "PYRAMID_MODE_CONFLICT" in out, out[-300:])
ok, out = run(["--pyramid", BASE_DECL, "--manual-claim"])
check("手動 claim の追撃は落ちる",
      not ok and "PYRAMID_MANUAL_CLAIM_UNSUPPORTED" in out, out[-300:])
ok, out = run(["--pyramid-consolidate"])
check("--pyramid-consolidate は --modify 専用",
      not ok and "PYRAMID_CONSOLIDATE_REQUIRES_MODIFY" in out, out[-300:])

print()
print("--- 合成リスクと合計枚数 ---")
# 追撃分だけのリスク($732)は残ドローダウン $780 に収まるが、**合計建玉**の
# リスク($825)は超える —— 従来の ULTRA ゲートでは通ってしまう幾何。
tight = [value if value != "2268" else "780" for value in ADD]
ok, out = _hermetic.dry_run(SANDBOX, tight + ["--pyramid", BASE_DECL])
check("追撃分だけなら通る幾何でも、合計建玉のリスクで落ちる",
      not ok and "PYRAMID_DRAWDOWN_EXCEEDED" in out, out[-400:])
RISK_PRICE = pyramid.adverse_add_price("sell", 29448.5)
check("その額は pyramid.combined_risk と同じ($825.00)",
      abs(pyramid.combined_risk(2, 29484.25, 6, RISK_PRICE, 29507.5, 2.0) - 825.0) < 1e-9,
      str(pyramid.combined_risk(2, 29484.25, 6, RISK_PRICE, 29507.5, 2.0)))
check("追撃分だけのリスクは残ドローダウン内(従来ゲートでは素通り)",
      (abs(29448.5 - 29507.5) + 2.0) * 6 * 2.0 <= 780.0)

# **滑りの分だけで枠を割る幾何。** 現在値ぴったりの合成は $801 で残DD $810 に収まるが、
# 成行は不利側へ滑るので実際の合成は $825 になる。ここを見ていないと
# 「概算では枠内・約定したら枠超え」が通る(単一脚の経路は昔から緩衝を足していた)。
check("現在値基準なら通る($801)", abs(pyramid.combined_risk(
    2, 29484.25, 6, 29448.5, 29507.5, 2.0) - 801.0) < 1e-9)
slip = [value if value != "2268" else "810" for value in ADD]
ok, out = _hermetic.dry_run(SANDBOX, slip + ["--pyramid", BASE_DECL])
check("滑りを当てた合成リスクで落ちる(緩衝が抜けていた穴)",
      not ok and "PYRAMID_DRAWDOWN_EXCEEDED" in out, out[-400:])

# 合計 95 + 6 = 101 枚。契約の口座あたり上限 100 を超えるが、合計リスクは
# 上限内に収まるようにして **枚数のゲートだけ** を見る。
qty_args = [value if value != "29507.5" else "29500.0" for value in ADD]
qty_args = [value if value != "2268" else "4000" for value in qty_args]
ok, out = _hermetic.dry_run(SANDBOX, qty_args + ["--pyramid", "--pyramid-base=95@29484.25"])
check("追撃後の合計枚数が口座上限を超えたら落ちる",
      not ok and "PYRAMID_TOTAL_QTY_EXCEEDS_ACCOUNT_MAX" in out, out[-400:])

min_add = ["--side", "sell", "--qty", "1", "--market", "--last", "29448.5",
           "--sl", "29507.5", "--split-tp", "29400,29300", "--ultra",
           "--ultra-drawdown", "2268", "--accounts", "TEST"]
ok, out = run(["--pyramid", BASE_DECL], args=min_add)
check("分割できない枚数(1)は落ちる(既存 ULTRA の分割ゲートが先に効く)",
      not ok and ("PYRAMID_ADD_QTY_BELOW_SPLIT_MIN" in out
                  or "ULTRA_QTY_BELOW_MIN" in out
                  or "ULTRA_SPLIT_NOT_REPRESENTABLE" in out), out[-300:])
check("契約の minAddQty は ULTRA の最小枚数より下がらない",
      __import__("tranche").min_add_qty()
      >= int(execution_contract.CONTRACT["ultra"]["minQtyPerAccount"]))

print()
print("--- 既存ゲートは全部そのまま ---")
wrong_sl = [value if value != "29507.5" else "29400.0" for value in ADD]
ok, out = _hermetic.dry_run(SANDBOX, wrong_sl + ["--pyramid", BASE_DECL])
check("SL の向きは従来どおり検査される", not ok and "売りのSL" in out, out[-300:])
one_tp = [value if value != "29400,29300" else "29400" for value in ADD]
ok, out = _hermetic.dry_run(SANDBOX, one_tp + ["--pyramid", BASE_DECL])
check("分割 TP は 2 本必須(ULTRA と同じ)",
      not ok and "ULTRA_SPLIT_TARGET_COUNT" in out, out[-300:])
ok, out = run(["--pyramid", BASE_DECL, "--accounts", "TEST,TEST2"])
check("許可リスト外の口座は従来どおり落ちる",
      not ok and "allowlist" in out, out[-300:])

print()
print("--- 門はドライランの return より前にある(R52) ---")
ok, out = _hermetic.dry_run(SANDBOX, tight + ["--pyramid", BASE_DECL])
check("ドライランでも合成リスクで落ちる(engine は dry-run の成功を live の前提にする)",
      not ok and "ドライラン" not in out.split("PYRAMID_DRAWDOWN_EXCEEDED")[-1],
      out[-300:])

print()
print("--- --pyramid-base の解釈 ---")
sys.path.insert(0, SANDBOX)
import importlib  # noqa: E402
order = importlib.import_module("order")
check("<qty>@<avgEntry> を読む", order.parse_pyramid_base("4@29446.625") == (4, 29446.625))
check("整数の平均建値も読む", order.parse_pyramid_base("4@29446") == (4, 29446.0))
check("枚数 0 は読まない", order.parse_pyramid_base("0@29446.6") is None)
check("形が違えば読まない", order.parse_pyramid_base("4x29446.6") is None)
check("空は読まない", order.parse_pyramid_base("") is None and order.parse_pyramid_base(None) is None)

print()
print("--- require_verified_pyramid_base(FLAT 要求の差し替え) ---")
import broker_status  # noqa: E402


def stub(result):
    def query(symbol, account=None):
        return result
    return query


VERIFIED = {"verified": True, "qty": 4, "side": "SHORT", "symbol": "MNQU6",
            "accountId": "TEST", "account": "TEST", "avgEntry": 29446.625}
original = order.broker_status.query_position
try:
    order.broker_status.query_position = stub(VERIFIED)
    result = order.require_verified_pyramid_base("MNQU6", "TEST", "sell", 4, 29446.625)
    check("宣言どおりの基礎建玉なら通る", result["qty"] == 4)

    for label, patch, marker in (
            ("枚数違い", {"qty": 5}, "PYRAMID_BASE_QTY_MISMATCH"),
            ("方向違い", {"side": "LONG"}, "PYRAMID_BASE_SIDE_MISMATCH"),
            ("銘柄違い", {"symbol": "MNQZ6"}, "PYRAMID_BASE_SYMBOL_MISMATCH"),
            ("口座違い", {"accountId": "OTHER", "account": "OTHER"},
             "PYRAMID_BASE_ACCOUNT_MISMATCH"),
            ("平均建値なし", {"avgEntry": None}, "PYRAMID_BASE_ENTRY_UNAVAILABLE"),
            ("平均建値の乖離", {"avgEntry": 29400.0}, "PYRAMID_BASE_ENTRY_DEVIATION"),
            ("UNVERIFIED", {"verified": False}, "UNVERIFIED")):
        order.broker_status.query_position = stub({**VERIFIED, **patch})
        try:
            order.require_verified_pyramid_base("MNQU6", "TEST", "sell", 4, 29446.625)
            check(f"{label} は落ちる", False, "no SystemExit")
        except SystemExit as exc:
            check(f"{label} は落ちる", marker in str(exc), str(exc))
finally:
    order.broker_status.query_position = original

print()
print(f"合計: PASS {PASS[0]} / FAIL {FAIL[0]}")
sys.exit(1 if FAIL[0] else 0)

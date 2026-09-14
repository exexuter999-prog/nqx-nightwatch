# -*- coding: utf-8 -*-
"""R30 ULTRA: チャレンジ通過を第一優先にした枚数計算と SL 上限。

## 何が壊れていたか

ULTRA の枚数は `qty = ceil(profitTarget / (TP1_pt * $2))` で、**全量が TP1 で
決済される**前提だった。しかし実行計画は qty を半分ずつ TP1 と runner へ割る
2 レグ分割。前提と計画が矛盾していた。

見本シグナル(SL 21pt / TP1 50pt / runner 150pt)での実測:

| | 残DD使用 | 試行回数 | TP1のみ当たり | 両レグ当たり |
|---|---|---|---|---|
| 旧式 | **100%**(全3口座) | **1.0 回** | 目標の 50% | 200% |
| 新式 | 50〜53% | **2.0 回** | 20% | 100〜107% |

旧式は**一敗で口座が死ぬ**サイズだった。しかも TP1 だけ当たると目標の半分
しか取れず、両方当たると 200% で過大。

## なぜ試行回数が通過率を決めるか

ULTRA は枚数を TP から逆算するので、1 トレードの損失は概ね
`利益目標 ÷ R` になる。残ドローダウン B に対する試行回数は

    N = B / (target / R) = B * R / target

つまり **R が試行回数を決める**。武装候補 101 本の runner R 中央値は 10.55 で、
TP1 の 1.87 より 5 倍以上大きい。分割の両レグを勘定に入れるだけで
実効 R が上がり、同じ残 DD で試行回数が倍になる。

## SL 上限について

依頼どおり ULTRA では点数上限を外した(60pt → 400pt)。ただし実測では
60pt 上限が落としていたのは 348 候補中 **10 本**(上限だけが理由なのは 5 本)で、
それらの runner R 中央値 8.58 は通過組の 10.55 より**低い**。撤廃しても
武装数は 101 のまま変わらなかった。**火力の主因は枚数計算の是正**。

点数を無限にはしない。400pt は「構造として説明できない SL」を弾く最後の枠で、
ノイズフロア下限(MIN_RISK_NF_MULT)と対になる上側の枠。ULTRA のリスクの正本は
残ドローダウンに対するドル額(`ultra_mode._contract_blockers`)。

ここで固定する不変条件:
  * 枚数は 2 レグ分割の**実際の決済**から決まる
  * 枚数は必ず偶数(奇数は分割で表現できない)
  * TP1 だけ当たった場合の利益を**隠さず**出す(目標には届かない)
  * runner が無ければ TP1 と同値扱い。捏造しない
  * SL 点数上限は ULTRA のときだけ緩む。通常経路は 60pt のまま
  * 残ドローダウン超過は依然としてブロックする
"""
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import ultra_mode  # noqa: E402

FAILED = []


def check(name, condition, detail=""):
    print(("  OK   " if condition else "  FAIL ") + name
          + (f" ({detail})" if detail and not condition else ""))
    if not condition:
        FAILED.append(name)


PV = 2.0
SIGNAL = {"side": "SELL", "entry": 30126.0, "stop": 30147.0,
          "target": 30076.0, "targets": [30076.0, 29976.0]}


def test_geometry_carries_the_runner():
    g = ultra_mode.signal_geometry(SIGNAL)
    check("SL/TP1 は従来どおり", g["slPoints"] == 21.0 and g["tpPoints"] == 50.0, str(g))
    check("runner が入る", g["runnerPoints"] == 150.0, str(g))
    check("runner の R も出る", g["runnerRr"] == round(150.0 / 21.0, 2), str(g))
    # targets が無ければ TP1 と同値。捏造しない。
    bare = ultra_mode.signal_geometry({k: v for k, v in SIGNAL.items() if k != "targets"})
    check("targets 無しなら runner = TP1", bare["runnerPoints"] == bare["tpPoints"], str(bare))
    # 方向に反する runner は使わない
    wrong = dict(SIGNAL, targets=[30076.0, 30200.0])   # SELL なのに上
    check("方向に反する runner は無視", ultra_mode.signal_geometry(wrong)["runnerPoints"] == 50.0)


def test_qty_comes_from_both_legs():
    # target 3000, TP1 50pt, runner 150pt, PV 2 → 2*3000/((50+150)*2) = 15 → 偶数へ 16
    q = ultra_mode.required_qty_split(3000, 50, 150, PV)
    check("両レグから枚数が出る", q == 16, str(q))
    check("偶数に切り上がる", q % 2 == 0)
    # 旧式は残しつつ、値が違うことを明示的に固定する
    old = ultra_mode.required_qty(3000, 50, PV)
    check("旧式は全量TP1前提のまま", old == 30, str(old))
    check("新式は旧式より小さい(過大が是正される)", q < old, f"{q} vs {old}")


def test_qty_edge_cases():
    check("runner 欠損は TP1 と同値扱い",
          ultra_mode.required_qty_split(1000, 50, None, PV)
          == ultra_mode.required_qty_split(1000, 50, 50, PV))
    check("最低 2 枚", ultra_mode.required_qty_split(1, 1000, 1000, PV) == 2)
    for bad in (0, -1, None, "x"):
        check(f"不正な目標 {bad!r} は None",
              ultra_mode.required_qty_split(bad, 50, 150, PV) is None)
    check("TP1 が 0 なら None", ultra_mode.required_qty_split(1000, 0, 150, PV) is None)
    check("pointValue 0 なら None", ultra_mode.required_qty_split(1000, 50, 150, 0) is None)


def test_plan_halves_the_drawdown_and_doubles_the_attempts():
    accounts = [{"id": "APEX-01", "profitTarget": 3000, "buffer": 1260},
                {"id": "APEX-02", "profitTarget": 6000, "buffer": 2520},
                {"id": "APEX-03", "profitTarget": 9000, "buffer": 3780}]
    plan = ultra_mode.build_plan(SIGNAL, accounts, signal_qty=2)
    g = plan["geometry"]
    for row in plan["accounts"]:
        used = row["drawdownUsedPct"]
        attempts = row["buffer"] / row["projectedLoss"]
        check(f"{row['id']} 残DDを使い切らない", used < 70, f"{used}%")
        check(f"{row['id']} 2 回以上試せる", attempts >= 1.8, f"{attempts:.2f}")
        # 両レグ完走で目標に届く
        check(f"{row['id']} 両レグで目標到達",
              row["projectedProfit"] >= row["profitTarget"],
              f"{row['projectedProfit']} < {row['profitTarget']}")
        # TP1 だけでは届かないことを隠さない
        check(f"{row['id']} TP1のみの利益が出ている",
              row["profitIfTp1Only"] is not None
              and row["profitIfTp1Only"] < row["profitTarget"],
              str(row.get("profitIfTp1Only")))
        # 旧式は残DDを 100% 使い切っていた
        old = ultra_mode.required_qty(row["profitTarget"], g["tpPoints"], PV)
        old_loss = old * g["slPoints"] * PV
        check(f"{row['id']} 旧式は残DDを使い切っていた",
              old_loss >= row["buffer"] - 1e-9, f"{old_loss} vs {row['buffer']}")


def test_drawdown_still_blocks():
    """枚数が減っても、残 DD を超える計画は依然として止まる。"""
    tiny = [{"id": "APEX-09", "profitTarget": 9000, "buffer": 100}]
    plan = ultra_mode.build_plan(SIGNAL, tiny, signal_qty=2)
    row = plan["accounts"][0]
    check("残DD超過は INELIGIBLE",
          row["verdict"] == "INELIGIBLE" and "DRAWDOWN_EXCEEDED" in row["reasons"],
          str(row["reasons"]))
    check("routable でない", row["routable"] is False)
    blown = ultra_mode.build_plan(SIGNAL, [{"id": "X", "profitTarget": 3000, "buffer": 0}],
                                   signal_qty=2)
    check("残DD 0 は ACCOUNT_BLOWN", "ACCOUNT_BLOWN" in blown["accounts"][0]["reasons"])


def test_ultra_sl_cap_is_scoped_to_ultra_mode():
    import importlib
    saved = os.environ.get("NQX_ULTRA_MODE")
    try:
        os.environ.pop("NQX_ULTRA_MODE", None)
        import msnr_gate
        importlib.reload(msnr_gate)
        normal = msnr_gate.sl_cap_pt()
        check("通常経路は 60pt のまま", normal == msnr_gate.CARD_SL_CAP, str(normal))
        check("ULTRA 判定は既定で off", msnr_gate.ultra_mode_enabled() is False)
        os.environ["NQX_ULTRA_MODE"] = "1"
        importlib.reload(msnr_gate)
        ultra = msnr_gate.sl_cap_pt()
        check("ULTRA では点数上限が緩む", ultra == msnr_gate.ULTRA_SL_CAP_PT and ultra > normal,
              str(ultra))
        check("それでも無限ではない", ultra < 1000)
    finally:
        if saved is None:
            os.environ.pop("NQX_ULTRA_MODE", None)
        else:
            os.environ["NQX_ULTRA_MODE"] = saved
        import msnr_gate
        importlib.reload(msnr_gate)


test_geometry_carries_the_runner()
test_qty_comes_from_both_legs()
test_qty_edge_cases()
test_plan_halves_the_drawdown_and_doubles_the_attempts()
test_drawdown_still_blocks()
test_ultra_sl_cap_is_scoped_to_ultra_mode()

if FAILED:
    print(f"FAILED: {len(FAILED)} -> {', '.join(FAILED)}")
    raise SystemExit(1)
print("ALL PASS (test_r30_ultra_challenge)")

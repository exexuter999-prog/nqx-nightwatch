# -*- coding: utf-8 -*-
"""R34 壊れた入力に対する耐性。

新規モジュール(tv_snapshot / quarterly_theory / gann / _score_gann)へ
異常入力を投げたところ、**4 件**の欠陥が出た:

  1. `quarterly_theory._phase_behaviour` — 高安キーが欠けた足で **KeyError 落ち**。
     t だけ見て通し、後段の max/min で落ちていた
  2. `msnr_gate._score_gann` — `strategy_matrix.models.gann` が dict でないと
     **AttributeError 落ち**。下流から来る外部データの型を信用していた
  3. `tv_snapshot._num` — **NaN と inf を通していた**(`"1e400"` → `inf`)。
     価格や epoch に inf が混じると比較が全部通り、静かに壊れる
  4. `ultra_mode.required_qty_split` — 極小 TP で 5000 億枚。ただしこれは
     `_contract_blockers` の `ULTRA_QTY_EXCEEDS_ACCOUNT_MAX` が既に止めており、
     関数側で None にすると `QTY_NOT_COMPUTABLE` に化けて**どの上限で落ちたか
     が消える**。設計どおり呼び出し側に任せるのが正しかった(修正を撤回した)

ここで固定するのは「壊れた入力で**落ちない**」ことと、
「取れなかった」を「0 だった」にしないこと。
"""
import math
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import gann              # noqa: E402
import msnr_gate         # noqa: E402
import quarterly_theory  # noqa: E402
import tv_snapshot       # noqa: E402
import ultra_mode        # noqa: E402

FAILED = []


def check(name, condition, detail=""):
    print(("  OK   " if condition else "  FAIL ") + name
          + (f" ({detail})" if detail and not condition else ""))
    if not condition:
        FAILED.append(name)


BAD_BARS = [
    ("空", []),
    ("None", None),
    ("辞書でない要素", [1, 2, 3]),
    ("キー欠損", [{"t": i} for i in range(40)]),
    ("高安だけ欠損", [{"t": i, "o": 1, "c": 1} for i in range(40)]),
    ("全部同値", [{"t": i, "o": 1, "h": 1, "l": 1, "c": 1} for i in range(40)]),
    ("NaN", [{"t": i, "o": float("nan"), "h": float("nan"),
              "l": float("nan"), "c": float("nan")} for i in range(40)]),
    ("inf", [{"t": i, "o": float("inf"), "h": float("inf"), "l": 0, "c": 0}
             for i in range(40)]),
    ("負の価格", [{"t": i, "o": -100, "h": -90, "l": -110, "c": -100} for i in range(40)]),
    ("t が文字列", [{"t": "x", "o": 1, "h": 2, "l": 0, "c": 1} for i in range(40)]),
    ("巨大 epoch", [{"t": 10 ** 18, "o": 1, "h": 2, "l": 0, "c": 1} for i in range(40)]),
    ("逆順", [{"t": 40 - i, "o": 1, "h": 2, "l": 0, "c": 1} for i in range(40)]),
]


def test_nothing_raises_on_broken_bars():
    for label, bars in BAD_BARS:
        for name, fn in (("gann.observe", lambda b=bars: gann.observe(b, 100.0)),
                         ("gann.anchor", lambda b=bars: gann.anchor(b)),
                         ("QT.observe", lambda b=bars: quarterly_theory.observe(
                             {"at": "2026-08-24T21:00:00+09:00"}, b or [], 100.0))):
            try:
                fn()
                ok = True
                detail = ""
            except Exception as exc:                    # noqa: BLE001
                ok = False
                detail = f"{type(exc).__name__}: {exc}"
            check(f"{name} / {label} で落ちない", ok, detail)


def test_num_rejects_non_finite():
    """NaN と inf を通すと、価格や epoch の比較が全部通って静かに壊れる。"""
    check("1e400 は inf ではなく None", tv_snapshot._num("1e400") is None)
    check("NaN は None", tv_snapshot._num(float("nan")) is None)
    check("inf は None", tv_snapshot._num(float("inf")) is None)
    check("-inf は None", tv_snapshot._num(float("-inf")) is None)
    check("bool は数値にしない", tv_snapshot._num(True) is None)
    # 正常系は壊さない
    check("カンマは通る", tv_snapshot._num("59,999") == 59999.0)
    check("U+2212 は通る", tv_snapshot._num("−1.00") == -1.0)
    check("epoch も有限だけ", tv_snapshot._epoch_sec("1e400") is None)


def test_score_gann_does_not_trust_the_matrix_shape():
    """strategy_matrix は下流から来る外部データ。型を信用しない。"""
    shapes = [
        ("None", None),
        ("空", {}),
        ("models が文字列", {"models": "x"}),
        ("gann が文字列", {"models": {"gann": "x"}}),
        ("gann が None", {"models": {"gann": None}}),
        ("anchor が文字列", {"models": {"gann": {"status": "COMPUTED",
                                                 "anchor": "x", "angles": []}}}),
        ("angles が None", {"models": {"gann": {"status": "COMPUTED",
                                                "anchor": {"priceUnit": 10},
                                                "angles": None}}}),
        ("angles に非dict", {"models": {"gann": {"status": "COMPUTED",
                                                 "anchor": {"priceUnit": 10},
                                                 "angles": [1, 2]}}}),
        ("unit が 0", {"models": {"gann": {"status": "COMPUTED",
                                           "anchor": {"priceUnit": 0},
                                           "angles": [{"price": 1, "primary": True}]}}}),
    ]
    for label, matrix in shapes:
        cand = {"entry": 100.0, "targets": [110.0], "score": 0,
                "evidence": [], "penalties": []}
        try:
            msnr_gate._score_gann(cand, matrix)
            ok, detail = True, ""
        except Exception as exc:                        # noqa: BLE001
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        check(f"_score_gann / {label} で落ちない", ok, detail)
        if ok:
            check(f"_score_gann / {label} は採点を動かさない",
                  cand["score"] == 0 and not cand["penalties"], str(cand))


def test_quarterly_distinguishes_missing_from_zero():
    """「取れなかった」を「0 だった」にしない。"""
    holes = [{"t": i * 180, "o": 1, "c": 1} for i in range(40)]     # 高安が無い
    out = quarterly_theory._phase_behaviour(
        holes, "2026-08-24T18:00:00-04:00", "2026-08-24T19:30:00-04:00", 100.0)
    check("読めない足は 0 本として返る", out["bars"] == 0, str(out))
    check("高安は None であって 0 ではない",
          out["high"] is None and out["low"] is None, str(out))
    check("range も None", out["range"] is None, str(out))
    nan_price = quarterly_theory._phase_behaviour(
        [{"t": 0, "o": 1, "h": 2, "l": 0, "c": 1}],
        "1970-01-01T00:00:00+00:00", "1970-01-01T01:00:00+00:00", float("nan"))
    check("NaN の現在値では position を出さない", nan_price["position"] is None,
          str(nan_price))


def test_ultra_qty_reports_which_limit_rejected_it():
    """上限超過を None に潰さない。どの上限で落ちたかが消える。"""
    absurd = ultra_mode.required_qty_split(1000, 1e-9, 1e-9, 2.0)
    check("極小 TP でも枚数は返る(None にしない)", isinstance(absurd, int), str(absurd))
    plan = ultra_mode.build_plan(
        {"side": "SELL", "entry": 30126.0, "stop": 30147.0,
         "target": 30126.0 - 1e-9, "targets": [30126.0 - 1e-9, 30126.0 - 2e-9]},
        [{"id": "APEX-01", "profitTarget": 3000, "buffer": 1260}], signal_qty=2)
    row = plan["accounts"][0]
    blockers = " ".join(row.get("contractBlockers") or [])
    check("契約側が上限で止める",
          "QTY_NOT_COMPUTABLE" not in (row.get("reasons") or [])
          and ("EXCEEDS" in blockers or "SIGNAL" in " ".join(row.get("reasons") or [])),
          f"reasons={row.get('reasons')} blockers={row.get('contractBlockers')}")
    check("routable ではない", row.get("routable") is False)


test_nothing_raises_on_broken_bars()
test_num_rejects_non_finite()
test_score_gann_does_not_trust_the_matrix_shape()
test_quarterly_distinguishes_missing_from_zero()
test_ultra_qty_reports_which_limit_rejected_it()

if FAILED:
    print(f"FAILED: {len(FAILED)} -> {', '.join(FAILED)}")
    raise SystemExit(1)
print("ALL PASS (test_r34_robustness)")

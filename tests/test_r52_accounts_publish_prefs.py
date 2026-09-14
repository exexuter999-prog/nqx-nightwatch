# -*- coding: utf-8 -*-
"""R52: 残機 publish の口座行に Mini App の accountPrefs.profitTarget を当てる。

2026-09-04: ユーザーが Mini App で利益目標を $1,500 に設定したのに、毎サイクルの
`publish_accounts` は env の TARGET_*($3,000)で口座行を組んでいた。アプリの ULTRA
パネルは `accounts.list[].profitTarget` から枚数を出すので 10枚と表示され、engine
(`_apply_ultra_prefs` は prefs を渡していた)は 6枚 —— 設定の正本が2つに割れていた。

ネットワークを使わない(publish と prefs 取得は差し替える)。

    python tests/test_r52_accounts_publish_prefs.py
"""
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
import nqx_state  # noqa: E402

PASS = [0]
FAIL = [0]


def check(label, condition, detail=""):
    if condition:
        PASS[0] += 1
        print(f"  OK   {label}")
    else:
        FAIL[0] += 1
        print(f"  NG   {label}  {detail}")


# TRAILING_DD/FLOOR を置かない → ブローカー照会に行かず LIFELINE_* へ倒れる(hermetic)
ENV = {"CROSSTRADE_ACCOUNTS": "ACC-ULTRA-01", "RISK_ACC-ULTRA-01": "200",
       "LIFELINE_ACC-ULTRA-01": "2000", "TARGET_ACC-ULTRA-01": "3000"}

captured = []
_orig_publish = nqx_state.publish
_orig_fetch = nqx_state.fetch_account_prefs
nqx_state.publish = lambda stream, payload, cfg=None: (captured.append((stream, payload)) or (True, {"ok": True}))


def _row():
    return captured[-1][1]["accounts"]["list"][0]


try:
    # 1. アプリ設定が env の TARGET_* より優先される
    nqx_state.fetch_account_prefs = lambda cfg=None, view=None: {
        "ACC-ULTRA-01": {"ultra": True, "profitTarget": 1500, "maxDrawdown": None}}
    ok, _ = nqx_state.publish_accounts(cfg={}, env=ENV)
    check("publish は account stream へ出る", ok and captured[-1][0] == "account")
    check("口座行の profitTarget はアプリ設定 $1,500", _row().get("profitTarget") == 1500.0,
          str(_row()))
    check("残機は LIFELINE_* から", _row().get("buffer") == 2000.0)

    # 2. 設定が無い/取れないときは env の TARGET_* へ倒れる
    nqx_state.fetch_account_prefs = lambda cfg=None, view=None: {}
    nqx_state.publish_accounts(cfg={}, env=ENV)
    check("設定なしは env $3,000", _row().get("profitTarget") == 3000.0, str(_row()))

    def _boom(cfg=None, view=None):
        raise RuntimeError("worker down")
    nqx_state.fetch_account_prefs = _boom
    ok, _ = nqx_state.publish_accounts(cfg={}, env=ENV)
    check("設定取得の例外でも publish は止まらず env へ倒れる",
          ok and _row().get("profitTarget") == 3000.0)

    # 3. 明示の prefs 引数は取得より優先(_apply_ultra_prefs と同じ値を共有できる)
    nqx_state.fetch_account_prefs = lambda cfg=None, view=None: {
        "ACC-ULTRA-01": {"ultra": True, "profitTarget": 9999, "maxDrawdown": None}}
    nqx_state.publish_accounts(cfg={}, env=ENV, prefs={
        "ACC-ULTRA-01": {"ultra": True, "profitTarget": 1200, "maxDrawdown": None}})
    check("明示 prefs が優先", _row().get("profitTarget") == 1200.0, str(_row()))

    # 4. 不正な設定値(0 以下)は無視して env へ倒れる
    nqx_state.fetch_account_prefs = lambda cfg=None, view=None: {
        "ACC-ULTRA-01": {"ultra": True, "profitTarget": 0, "maxDrawdown": None}}
    nqx_state.publish_accounts(cfg={}, env=ENV)
    check("0 以下の設定は無視", _row().get("profitTarget") == 3000.0)
finally:
    nqx_state.publish = _orig_publish
    nqx_state.fetch_account_prefs = _orig_fetch

print()
print(f"合計: PASS {PASS[0]} / FAIL {FAIL[0]}")
sys.exit(1 if FAIL[0] else 0)

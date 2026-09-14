# -*- coding: utf-8 -*-
"""R37 実行経路の安全性。監査で二者独立に成立を確認した欠陥を塞ぐ。

## 1. トレール SL が現在値の反対側に置かれる

`management_action` は `current_stop` との単調性（BUY は上げのみ）は検査するが、
算出した `desired` と**現在価格**を一度も比較していなかった。`best` は終値では
なく高値/安値の極値なので、価格が走ってから普通に押しただけで `desired` が
現在値を追い越す:

    BUY / entry 29630 / risk 25pt(distance 18.75) で 29695 まで走り 29660 へ押すと
    desired = 29676.25 —— LONG の損切りを現在値の 16.25pt 上へ置こうとする

下流に止める層は無い（`management_intent` は tick と正数のみ、`order.py --modify`
に side 判定は無く、Worker も stop をそのまま採用）。しかも送信は
`cancelandbracket` なので、拒否されると runner が保護注文ゼロで残りうる。

## 2. 子プロセスの制限時間が HTTP 予算より短い

`order.py --confirm` は逐次 HTTP を最大 6 本使い、最悪 114 秒（同ファイルの
既存コメントが自認）。30 秒で kill すると webhook には注文が届いているのに
経路 envelope ごと捨て、次サイクルで恒久 hold に落ちる。

## 3. 分割建玉の全量 modify

2 枚が生存中の全量 `cancelandbracket` は TP1 と runner 最終 TP を 1 本に潰す。
自律経路にしか無かった不変条件を `order.py` へ降ろす。
"""
import os
import shutil
import subprocess
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import autotrade_engine  # noqa: E402
from _hermetic import make_sandbox  # noqa: E402

FAILED = []


def check(name, condition, detail=""):
    print(("  OK   " if condition else "  FAIL ") + name
          + (f" ({detail})" if detail and not condition else ""))
    if not condition:
        FAILED.append(name)


SYMBOL = "MNQU6"
PLAN = {
    "symbol": SYMBOL, "side": "BUY", "qty": 2,
    "entry": 29630.0, "initialStop": 29605.0,
    "tp1": 29645.0, "finalTarget": 29705.0,
    "trailDistance": 18.75,
    "legs": [{"id": "TP1", "qty": 1, "target": 29645.0},
             {"id": "RUNNER", "qty": 1, "target": 29705.0}],
    "legIdentityKnown": True,
}


def act(price, highs):
    """runner 1 枚が残っている状態で次の一手を求める。"""
    bars = [{"t": i * 180, "o": 29630.0, "h": h, "l": 29625.0, "c": 29630.0}
            for i, h in enumerate(highs)]
    return autotrade_engine.management_action(
        PLAN, {"qty": 1, "side": "BUY", "avgEntry": 29630.0},
        {"price": price, "snapshot": {"bars3m": bars, "bars": bars}})


def test_trail_never_places_the_stop_on_the_wrong_side():
    # 価格が 29695 まで走ってから 29660 へ押した局面。
    # desired = max(29630, 29695 - 18.75) = 29676.25 > 現在値 29660。
    wrong_side = act(29660.0, [29650.0, 29670.0, 29695.0])
    if wrong_side is not None and wrong_side.get("action") == "MODIFY":
        check("押し目で現在値より上に LONG の SL を出さない",
              wrong_side["sl"] <= 29660.0 - 0.25,
              f"sl={wrong_side['sl']} price=29660")
    else:
        check("押し目で現在値より上に LONG の SL を出さない", True)

    # 正常なトレール: 価格が高値圏にあり desired は現在値より下。
    ok = act(29700.0, [29650.0, 29680.0, 29700.0])
    if ok is not None and ok.get("action") == "MODIFY":
        check("正常なトレールは従来どおり出る", ok["sl"] <= 29700.0 - 0.25, str(ok))
        check("SL は建値以上へ動く", ok["sl"] >= PLAN["entry"] - 1e-9, str(ok))
    else:
        check("正常なトレールは従来どおり出る", False, str(ok))


def test_subprocess_timeout_exceeds_the_http_budget():
    # order.py の逐次 HTTP は最大 6 本 × 15 秒。既存コメントの自認値は 114 秒。
    check("子プロセスの上限が HTTP 予算を上回る",
          autotrade_engine.ORDER_SUBPROCESS_TIMEOUT_SEC >= 120,
          str(autotrade_engine.ORDER_SUBPROCESS_TIMEOUT_SEC))
    # 監視周期(3 分)を超えると次サイクルへ食い込むので上も抑える。
    check("監視周期を超えない",
          autotrade_engine.ORDER_SUBPROCESS_TIMEOUT_SEC <= 180,
          str(autotrade_engine.ORDER_SUBPROCESS_TIMEOUT_SEC))


STUB_BROKER = '''# -*- coding: utf-8 -*-
# R80: route_identity は import 時に broker_status の状態集合を参照する。スタブも同じ表面を持つ。
BROKER_ACTIVE_STATES = {"WORKING", "ACCEPTED", "SUBMITTED", "PENDING", "PENDING_SUBMIT",
                        "SUSPENDED", "PARTIALLY_FILLED", "PARTIAL_FILL"}
BROKER_TERMINAL_STATES = {"FILLED", "CANCELED", "REJECTED", "EXPIRED"}
BROKER_LIVE_PROTECTIVE_STATES = {"WORKING", "ACCEPTED", "SUBMITTED", "PARTIALLY_FILLED", "PARTIAL_FILL"}
"""テスト用スタブ。2 枚の建玉が確認できた状態を返す。ネットワークへは出ない。"""


def query_position(symbol="MNQU6", account=None):
    return {"verified": True, "qty": 2, "side": "LONG", "symbol": symbol,
            "accountId": "TEST", "avgEntry": 29630.0}


def position_identity(position):
    return "gen-1"


def query_orders(symbol="MNQU6", known_order_ids=None, account=None):
    return {"verified": True, "orders": []}


def derived_receipt(*args, **kwargs):
    return None
'''


def test_modify_refuses_to_collapse_a_split_position():
    sandbox = make_sandbox(BASE)
    try:
        with open(os.path.join(sandbox, "broker_status.py"), "w", encoding="utf-8") as fh:
            fh.write(STUB_BROKER)
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
        args = ["--modify", "--side", "buy", "--qty", "2",
                "--sl", "29615", "--tp", "29645",
                "--position-generation", "gen-1",
                "--management-key", "k", "--management-token", "t",
                "--management-intent-hash", "h", "--confirm"]
        proc = subprocess.run(
            [sys.executable, os.path.join(sandbox, "order.py")] + args,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            cwd=sandbox, env=env, timeout=60)
        out = (proc.stdout or "") + (proc.stderr or "")
        check("2 枚建玉の全量 modify を拒否する",
              proc.returncode != 0 and "MODIFY_SPLIT_PLAN_PROTECTED" in out,
              out.strip()[:400])
        check("拒否時に外部へ送信しない", "SNAPSHOT" not in out and "FINAL" not in out,
              out.strip()[:200])
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)


test_trail_never_places_the_stop_on_the_wrong_side()
test_subprocess_timeout_exceeds_the_http_budget()
test_modify_refuses_to_collapse_a_split_position()

if FAILED:
    print(f"FAILED: {len(FAILED)} -> {', '.join(FAILED)}")
    raise SystemExit(1)
print("ALL PASS (test_r37_execution_safety)")

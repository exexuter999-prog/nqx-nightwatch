# -*- coding: utf-8 -*-
"""order.py のドライランを本番 .secrets から隔離するための共通部品。

テストが検証したいのは order.py のロジックであって、本番
`.secrets/crosstrade.env` の内容ではない。実ファイルを読ませると
リスク上限(RISK_* / MAX_RISK_DOLLARS)を運用で変えるたびにテストが
壊れる(2026-08-15: 既定値 200→60 への変更で test_bot / test_buttons /
test_oneclick の計10件超が実際に壊れた)。

ここでは order.py / broker_status.py を一時ディレクトリへコピーし、
テスト専用の crosstrade.env を書き込んで実行する。送信先は破棄ポート
なので、万一 --confirm が漏れても外部には到達しない。
"""
import os
import shutil
import subprocess
import sys
import tempfile

# テストの歴史的前提(旧既定値)。本番のリスク設定変更から切り離すために固定する。
# テスト内の「リスク超過」ケース($400)はこの値でも超過のまま成立する。
TEST_MAX_RISK = 200


def make_sandbox(base):
    """order.py 一式を隔離した一時ディレクトリを作って返す。"""
    sandbox = tempfile.mkdtemp(prefix="nqx-hermetic-")
    shutil.copy2(os.path.join(base, "order.py"), os.path.join(sandbox, "order.py"))
    # order.py の実送信直前フェイルクローズ照会も同じ隔離環境で解決する。
    shutil.copy2(os.path.join(base, "broker_status.py"),
                 os.path.join(sandbox, "broker_status.py"))
    # order.py imports the pure execution contract.  Copying both immutable
    # inputs keeps the hermetic runner isolated while exercising the same gate
    # as production code.
    shutil.copy2(os.path.join(base, "execution_contract.py"),
                 os.path.join(sandbox, "execution_contract.py"))
    shutil.copy2(os.path.join(base, "execution_intent.py"),
                 os.path.join(sandbox, "execution_intent.py"))
    shutil.copy2(os.path.join(base, "management_intent.py"),
                 os.path.join(sandbox, "management_intent.py"))
    shutil.copy2(os.path.join(base, "nqx_state.py"),
                 os.path.join(sandbox, "nqx_state.py"))
    shutil.copy2(os.path.join(base, "strategy_evidence.py"),
                 os.path.join(sandbox, "strategy_evidence.py"))
    # Broker-truth route identity acquisition is imported by order.py.
    shutil.copy2(os.path.join(base, "route_identity.py"),
                 os.path.join(sandbox, "route_identity.py"))
    # R84: order.py は追撃の合計リスクを pyramid.combined_risk で見る(engine /
    # order.py / Worker を同一算術にするため)。コピー漏れは order.py の import 失敗
    # = 全ドライランが落ちる形で出る。
    shutil.copy2(os.path.join(base, "pyramid.py"),
                 os.path.join(sandbox, "pyramid.py"))
    shutil.copy2(os.path.join(base, "execution_contract.json"),
                 os.path.join(sandbox, "execution_contract.json"))
    shutil.copy2(os.path.join(base, "dayguard.py"),
                 os.path.join(sandbox, "dayguard.py"))
    os.makedirs(os.path.join(sandbox, ".secrets"), exist_ok=True)
    # Dayguard's first-run contract requires an explicit initialized marker.
    # This is a disposable test ledger, never the operator's production file.
    open(os.path.join(sandbox, ".secrets", "day_ledger.jsonl"), "x", encoding="utf-8").close()
    with open(os.path.join(sandbox, ".secrets", "day_ledger.jsonl.initialized"), "x",
              encoding="utf-8") as fh:
        fh.write("NQX_DAY_LEDGER_INITIALIZED_V1\n")
    with open(os.path.join(sandbox, ".secrets", "crosstrade.env"), "w",
              encoding="utf-8") as fh:
        # 破棄ポート。--confirm が漏れても誰にも届かない。
        fh.write("CROSSTRADE_URL=http://127.0.0.1:9/blocked\n"
                 "CROSSTRADE_KEY=TEST\n"
                 "CROSSTRADE_DEST=TEST\n"
                 "CROSSTRADE_ACCOUNT=TEST\n"
                 f"MAX_RISK_DOLLARS={TEST_MAX_RISK}\n")
    return sandbox


def dry_run(sandbox, args, timeout=30):
    """隔離環境の order.py を実行する(実ログ・実設定に触れない)。"""
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    proc = subprocess.run(
        [sys.executable, os.path.join(sandbox, "order.py")] + list(args),
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=sandbox, env=env, timeout=timeout)
    out = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode == 0, out.strip() or "(出力なし)"

# -*- coding: utf-8 -*-
"""全テストを 1ファイル = 1プロセス で実行する。

このディレクトリのテストは自走式スクリプトで、モジュール読み込み時に
実行されて sys.exit() する。unittest discover / pytest のように1プロセスへ
全部 import する走らせ方では、(1) SystemExit が import エラー扱いになり、
(2) telegram_bot / broker_status へのスタブ差し替えがファイル間で漏れて
結果が信用できない(2026-08-15 に実際に誤検知が起きた)。必ずこれを使う:

    python tests/run_all.py
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    names = sorted(n for n in os.listdir(HERE)
                   if n.startswith("test_") and n.endswith(".py"))
    failures = []
    for name in names:
        print(f"\n{'=' * 68}\n>>> {name}\n{'=' * 68}", flush=True)
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
        proc = subprocess.run([sys.executable, os.path.join(HERE, name)], env=env)
        if proc.returncode != 0:
            failures.append(name)
    print("\n" + "=" * 68)
    if failures:
        print(f"FAILED: {len(failures)} / {len(names)} -> {', '.join(failures)}")
        return 1
    print(f"ALL PASS ({len(names)} files)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

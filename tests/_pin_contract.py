# -*- coding: utf-8 -*-
"""テストを本番の運用スイッチから切り離す(R102, 2026-09-15)。

engine 系のテストは ``execution_contract.CONTRACT`` を本番 JSON からそのまま読む。
2026-09-15 に ``manualHalt.autotrade=true`` を本番で立てたところ、reconcile が
「MANUAL HALT」で早期 return し、18 ファイルが一斉に落ちた(テストの結合の問題で、
本番の停止は正しい)。ここで **プロセス内の辞書だけ** を既定へ戻す。JSON は触らない。

同時に取引限月も固定する(ロール後に fixture の CME_MINI:MNQU2026 が壊れないように)。

使い方: ``sys.path.insert(0, BASE)`` の後、engine を使う前に ``import _pin_contract``。
"""
import copy

import execution_contract

# 隔離サンドボックス(tests/_hermetic.py)にも同じブロックを書く。時計は 2026-09-10 に固定し、
# fixture の MNQU6(満期 2026-09-18)が本番のロール後・満期後も「有効な限月」として振る舞う。
PIN = {
    "symbol": "MNQU6", "tvSymbol": "CME_MINI:MNQU2026", "expiry": "2026-09-18",
    "lastEntryDaysBeforeExpiry": 3,
    "next": {"symbol": "MNQZ6", "tvSymbol": "CME_MINI:MNQZ2026", "expiry": "2026-12-18"},
    # 既存の engine fixture は OCO 構造を持たない(全部「裸」に見える)ので既定 OFF。
    # R102 の裸修復テストだけが LIVE を明示する。
    "nakedRepair": {"mode": "OFF", "graceSec": 0},
    "clockOverride": "2026-09-10T00:00:00+00:00",
}

execution_contract.CONTRACT.setdefault("manualHalt", {})["autotrade"] = False
execution_contract.CONTRACT["contract"] = copy.deepcopy(PIN)

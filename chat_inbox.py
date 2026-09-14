#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""停止済み Mini App 受信箱の互換 CLI。

受信箱機能は 2026-08-25 から無期限停止。監視ループに残った旧呼び出しを
ブロッカーにしないため、どの操作もネットワークや既読位置へ触れず正常終了する。
市場データ取得・分析・自動発注の各経路とは完全に分離されている。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

import nqx_state  # noqa: E402  (旧呼び出しとの import 互換性だけを維持)

CURSOR_PATH = BASE / ".secrets" / "chat_cursor.json"
MAX_CHARS = 4000
INBOX_ENABLED = False
INBOX_STATUS = "INBOX_DISABLED_INDEFINITELY"


def _cursor() -> int:
    try:
        return int(json.loads(CURSOR_PATH.read_text(encoding="utf-8")).get("seq") or 0)
    except (OSError, ValueError, AttributeError, json.JSONDecodeError):
        return 0


def _save_cursor(seq: int) -> None:
    try:
        CURSOR_PATH.parent.mkdir(parents=True, exist_ok=True)
        CURSOR_PATH.write_text(json.dumps({"seq": int(seq)}), encoding="utf-8")
    except OSError as exc:
        # 既読位置が保存できなくても会話自体は成立する。黙らせず、止めもしない。
        print(f"warning: could not save cursor: {exc}", file=sys.stderr)


def _local(at_ms) -> str:
    try:
        return datetime.fromtimestamp(float(at_ms) / 1000, timezone.utc)\
            .astimezone().strftime("%m/%d %H:%M")
    except (TypeError, ValueError, OSError):
        return "??/?? ??:??"


def fetch(after: int = 0, cfg=None):
    """停止中。ネットワークへ到達せず空の受信結果を返す。"""
    del after, cfg
    return True, []


def send(text: str, reply_to=None, cfg=None):
    """停止中。送信せず、明示的な停止理由を返す。"""
    del text, reply_to, cfg
    return False, {"reason": INBOX_STATUS, "enabled": INBOX_ENABLED}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Mini App チャットの受信箱")
    parser.add_argument("--poll", action="store_true", help="未読を表示する")
    parser.add_argument("--peek", action="store_true", help="既読位置を進めない")
    parser.add_argument("--reply", metavar="TEXT", help="Claude として返信する")
    parser.add_argument("--reply-to", type=int, default=None, help="返信先の seq")
    parser.add_argument("--history", type=int, metavar="N", help="直近 N 件を表示")
    parser.add_argument("--json", action="store_true", help="JSON で出す")
    args = parser.parse_args(argv)

    # 旧ループが --poll / --reply を残していても周期を失敗させない。
    # 受信・送信・cursor 更新は一切行わず、停止状態だけを返して exit 0 とする。
    status = {
        "ok": True,
        "enabled": INBOX_ENABLED,
        "status": INBOX_STATUS,
        "blocking": False,
    }
    if args.json:
        print(json.dumps(status, ensure_ascii=False))
    else:
        print("受信箱は無期限停止中（監視・分析・発注は継続）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

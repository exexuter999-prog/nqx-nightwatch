# -*- coding: utf-8 -*-
"""Telegram 429 の retry_after を跨いで再送しない(2026-09-08 実測の回帰)。

3 分ループの通知が 429 を受けた後も毎サイクル送信を試み、flood 制御の
retry_after が 549s → 338s → 484s と延び続けた。制限中は送信系メソッドを
呼ばず None を返し(呼び出し側は従来どおり送信失敗として扱う)、読み取り系
(getUpdates 等)は止めない。
"""
import json
import os
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import telegram_bot as tb  # noqa: E402

FAILED = []


def check(name, condition, detail=""):
    print(("  OK   " if condition else "  FAIL ") + name
          + (f" ({detail})" if detail and not condition else ""))
    if not condition:
        FAILED.append(name)


BODY_429 = json.dumps({"ok": False, "error_code": 429,
                       "description": "Too Many Requests: retry after 549",
                       "parameters": {"retry_after": 549}})

with tempfile.TemporaryDirectory() as tmp:
    path = os.path.join(tmp, "telegram_backoff.json")
    now = 1_800_000_000.0

    check("backoff ファイルが無ければ送信は止まらない",
          tb._blocked_by_backoff("sendMessage", path, now) is False)
    until = tb._record_backoff("sendMessage", BODY_429, path, now)
    check("429 の retry_after を保存し、余裕を足した解除時刻を返す",
          until == now + 549 + tb.BACKOFF_MARGIN_SEC and os.path.exists(path), str(until))
    saved = json.load(open(path, encoding="utf-8"))
    check("保存内容に retryAfter / method / until が入る",
          saved["retryAfter"] == 549 and saved["method"] == "sendMessage" and saved["until"] == until)
    check("制限中は sendMessage / sendPhoto を呼ばない",
          tb._blocked_by_backoff("sendMessage", path, now + 100) is True and
          tb._blocked_by_backoff("sendPhoto", path, now + 500) is True)
    check("制限中でも getUpdates / getMe は止めない(制限は chat への送信に掛かる)",
          tb._blocked_by_backoff("getUpdates", path, now + 100) is False and
          tb._blocked_by_backoff("getMe", path, now + 100) is False)
    check("解除時刻を過ぎたら送信を再開する",
          tb._blocked_by_backoff("sendMessage", path, until + 1) is False)
    check("retry_after が無い応答では backoff を作らない",
          tb._record_backoff("sendMessage", json.dumps({"ok": False, "error_code": 400}), path + ".x", now) is None
          and not os.path.exists(path + ".x"))
    check("壊れた JSON でも例外にしない",
          tb._record_backoff("sendMessage", "not json", path + ".y", now) is None)

    # api() は制限中にネットワークへ出ない(ダミー token でも URL を組む前に返る)
    original = tb.BACKOFF_FILE
    tb.BACKOFF_FILE = path
    try:
        check("api() は制限中に None を返し、リクエストを組まない",
              tb.api({"TELEGRAM_TOKEN": "0:dummy", "TELEGRAM_CHAT_ID": "1"}, "sendMessage", text="x") is None)
        check("send_photo() も制限中は None",
              tb.send_photo({"TELEGRAM_TOKEN": "0:dummy", "TELEGRAM_CHAT_ID": "1"}, b"png") is None)
    finally:
        tb.BACKOFF_FILE = original

if FAILED:
    print(f"\nFAILED ({len(FAILED)}): " + ", ".join(FAILED))
    sys.exit(1)
print("\nALL PASS (test_telegram_backoff)")

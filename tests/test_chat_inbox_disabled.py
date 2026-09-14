#!/usr/bin/env python3
"""無期限停止した受信箱が監視周期を止めないことを固定する。"""
from __future__ import annotations

import contextlib
import io
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import chat_inbox


class DisabledInboxTests(unittest.TestCase):
    def test_fetch_does_not_touch_network(self):
        with mock.patch.object(chat_inbox.nqx_state, "chat_fetch", side_effect=AssertionError("network")):
            self.assertEqual(chat_inbox.fetch(99), (True, []))

    def test_send_does_not_touch_network(self):
        with mock.patch.object(chat_inbox.nqx_state, "chat_send", side_effect=AssertionError("network")):
            ok, detail = chat_inbox.send("reply")
        self.assertFalse(ok)
        self.assertEqual(detail["reason"], "INBOX_DISABLED_INDEFINITELY")

    def test_shared_state_helpers_are_also_offline(self):
        with mock.patch.object(chat_inbox.nqx_state.urllib.request, "urlopen", side_effect=AssertionError("network")):
            self.assertEqual(chat_inbox.nqx_state.chat_fetch(7), (True, []))
            ok, detail = chat_inbox.nqx_state.chat_send("reply")
        self.assertFalse(ok)
        self.assertEqual(detail["reason"], "INBOX_DISABLED_INDEFINITELY")

    def test_all_legacy_cli_operations_are_nonblocking(self):
        for argv in (["--poll"], ["--history", "20"], ["--reply", "reply"]):
            with self.subTest(argv=argv), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(chat_inbox.main(argv), 0)

    def test_json_exposes_disabled_nonblocking_contract(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(chat_inbox.main(["--poll", "--json"]), 0)
        self.assertIn('\"status\": \"INBOX_DISABLED_INDEFINITELY\"', out.getvalue())
        self.assertIn('\"blocking\": false', out.getvalue())


if __name__ == "__main__":
    unittest.main()

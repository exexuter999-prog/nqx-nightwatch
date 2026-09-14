# -*- coding: utf-8 -*-
"""R52: nqx_cycle.stage_fetch は CLI の間欠クラッシュ(0xC0000409)を短く待って取り直す。

2026-09-04〜05 の夜、30 サイクル中 3 回が `tv pane list / tv state / tv pane focus 0 失敗
(exit 3221226505)` で BLOCKED になり、毎回次の tick で復帰していた。取得は冪等で 1 回
4 秒なので、BLOCKED にする前にリトライする。

    python tests/test_r52_fetch_retry.py
"""
import os
import sys
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
import nqx_cycle  # noqa: E402

CRASH = (3221226505, "", "FETCH BLOCKED: tv pane focus 0 失敗 (exit 3221226505):")
OK = (0, '{"htfFetched": [], "schema": "NQX_TV_FETCH/1"}', "")


class FetchRetryTests(unittest.TestCase):
    def _run(self, sequence, attempts="3"):
        calls, slept = [], []
        original_run, original_sleep = nqx_cycle._run, nqx_cycle.time.sleep
        original_env = os.environ.get("NQX_FETCH_ATTEMPTS")
        os.environ["NQX_FETCH_ATTEMPTS"] = attempts
        os.environ["NQX_FETCH_RETRY_SEC"] = "0.1"
        nqx_cycle._run = lambda args, stdin=None, timeout=180: (calls.append(list(args)) or sequence.pop(0))
        nqx_cycle.time.sleep = lambda s: slept.append(s)
        try:
            result = nqx_cycle.stage_fetch()
        finally:
            nqx_cycle._run, nqx_cycle.time.sleep = original_run, original_sleep
            if original_env is None:
                os.environ.pop("NQX_FETCH_ATTEMPTS", None)
            else:
                os.environ["NQX_FETCH_ATTEMPTS"] = original_env
            os.environ.pop("NQX_FETCH_RETRY_SEC", None)
        return result, calls, slept

    def test_first_attempt_success_does_not_retry(self):
        (ok, detail), calls, slept = self._run([OK])
        self.assertTrue(ok)
        self.assertEqual(len(calls), 1)
        self.assertEqual(slept, [])
        self.assertNotIn("attempt", detail)

    def test_transient_crash_is_retried_and_reported(self):
        (ok, detail), calls, slept = self._run([CRASH, OK])
        self.assertTrue(ok)
        self.assertEqual(len(calls), 2)
        self.assertEqual(slept, [0.1])
        self.assertIn("succeeded on attempt 2/3", detail)

    def test_persistent_crash_blocks_after_the_budget(self):
        (ok, detail), calls, slept = self._run([CRASH, CRASH, CRASH])
        self.assertFalse(ok)
        self.assertEqual(len(calls), 3)
        self.assertEqual(len(slept), 2)
        self.assertIn("3221226505", detail)
        self.assertIn("(attempt 3/3)", detail)

    def test_cvd_only_flag_is_preserved_on_retry(self):
        calls = []
        original = nqx_cycle._run
        seq = [CRASH, OK]
        nqx_cycle._run = lambda args, stdin=None, timeout=180: (calls.append(list(args)) or seq.pop(0))
        original_sleep = nqx_cycle.time.sleep
        nqx_cycle.time.sleep = lambda s: None
        try:
            ok, _ = nqx_cycle.stage_fetch(cvd_only=True)
        finally:
            nqx_cycle._run, nqx_cycle.time.sleep = original, original_sleep
        self.assertTrue(ok)
        self.assertTrue(all("--cvd-only" in c for c in calls))


if __name__ == "__main__":
    unittest.main(verbosity=1)

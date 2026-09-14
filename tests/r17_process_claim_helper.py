# -*- coding: utf-8 -*-
"""Subprocess contender for the hermetic R17 producer-race test.

The filesystem O_EXCL record models one atomic Durable Object commit without
network access. The production final authority remains the Worker Durable
Object transaction; this helper only proves independent Python processes use
the same canonical claim identity and fail closed after the first commit.
"""
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import nqx_state  # noqa: E402
import execution_intent  # noqa: E402


def main():
    scenario = json.loads(os.environ["R17_SCENARIO_JSON"])
    claim_file = os.environ["R17_PROCESS_CLAIM_FILE"]

    def atomic_publish(_stream, payload, _cfg=None):
        try:
            fd = os.open(claim_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            return False, {"reason": "ENTRY_CLAIM_ALREADY_HELD"}
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(str(payload.get("entryKey") or ""))
            handle.flush()
        intent = execution_intent.from_scenario(
            scenario, order_type=payload["orderType"],
            account_scope=scenario["executionContract"]["accountScope"])
        return True, {"view": {"entryClaim": {
            "state": "CLAIMED", "entryKey": payload["entryKey"],
            "executionIntent": intent,
            "executionIntentHash": execution_intent.intent_hash(intent),
        }}}

    ok, detail = nqx_state.claim_entry(scenario, cfg={}, publish_fn=atomic_publish)
    print(json.dumps({"ok": ok, "entryKey": detail.get("entryKey"),
                      "reason": detail.get("reason")}, separators=(",", ":")))


if __name__ == "__main__":
    main()

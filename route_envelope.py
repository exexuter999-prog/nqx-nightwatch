#!/usr/bin/env python3
"""Strict R22 parser for the resolved split-entry route envelope.

The parser is shared by Telegram and autotrade.  It never infers acceptance
from HTTP text: one canonical SNAPSHOT plus one terminal FINAL is required.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterable, Optional

SNAPSHOT_PREFIX = "NQX_ROUTE_SNAPSHOT "
FINAL_PREFIX = "NQX_ROUTE_FINAL "
RESERVED_PREFIX = "NQX_ROUTE_"
FINAL_RE = re.compile(
    r"^NQX_ROUTE_FINAL STATE=(SENT|PARTIAL|UNKNOWN|REJECTED) "
    r"ACCEPTED=(\d+) REJECTED=(\d+) UNKNOWN=(\d+) TOTAL=(\d+) RESOLVED=([01])$")
LEGS = ("TP1", "RUNNER")
MAX_OUTPUT_BYTES = 2 * 1024 * 1024


def _accounts(values: Optional[Iterable[Any]]) -> Optional[list[str]]:
    if values is None:
        return None
    rows = sorted(str(value).strip() for value in values if str(value).strip())
    return rows if rows and len(rows) == len(set(rows)) else None


#: R76: 束縛の出所と、ブラケット子 2 行の id / receipt。routeSnapshot 行の任意項目で、
#: あれば形を検査して透過する(Worker は既知の項目だけ取り出すので害は無い)。
IDENTITY_EXTRA_LIST_KEYS = ("bracketOrderIds", "bracketReceipts")


def _identity_extras(raw: Dict[str, Any]) -> tuple[Dict[str, Any], bool]:
    extras: Dict[str, Any] = {}
    source = raw.get("identitySource")
    if source not in (None, ""):
        if not isinstance(source, str) or len(source) > 32:
            return {}, False
        extras["identitySource"] = source
    for key in IDENTITY_EXTRA_LIST_KEYS:
        values = raw.get(key)
        if values in (None, "", []):
            continue
        if not isinstance(values, list) or len(values) != 2:
            return {}, False
        items = [str(value).strip() for value in values]
        if any(not item or len(item) > 160 for item in items) or items[0] == items[1]:
            return {}, False
        extras[key] = items
    if ("bracketOrderIds" in extras) != ("bracketReceipts" in extras):
        return {}, False
    return extras, True


def parse(output: Any, *, expected_accounts: Optional[Iterable[Any]] = None,
          require_resolved: bool = True, diagnostics: Any = None) -> Dict[str, Any]:
    machine = str(output or "")
    diagnostic_text = str(diagnostics or "")
    if len(machine.encode("utf-8")) > MAX_OUTPUT_BYTES:
        return {"ok": False, "reason": "route stdout exceeds 2 MiB"}
    if len(diagnostic_text.encode("utf-8")) > MAX_OUTPUT_BYTES:
        return {"ok": False, "reason": "route diagnostics exceed 2 MiB"}
    if diagnostic_text.strip():
        return {"ok": False, "reason": "route diagnostics must be empty"}
    lines = [line.strip() for line in machine.splitlines() if line.strip()]
    snapshot_lines = [line for line in lines if line.startswith(SNAPSHOT_PREFIX)]
    final_lines = [line for line in lines if line.startswith(FINAL_PREFIX)]
    malformed_reserved = [line for line in lines if line.startswith(RESERVED_PREFIX)
                          and not line.startswith(SNAPSHOT_PREFIX)
                          and not line.startswith(FINAL_PREFIX)]
    if malformed_reserved or len(snapshot_lines) != 1 or len(final_lines) != 1:
        return {"ok": False, "reason": "route envelope requires exactly one SNAPSHOT and FINAL"}
    if lines[-1] != final_lines[0]:
        return {"ok": False, "reason": "route FINAL is not the final non-empty line"}
    failure_marker = re.compile(
        r"(?:^|\b)(ERROR|FATAL|EXCEPTION|FAILED|INTERRUPTED|TRACEBACK)(?:\b|:)",
        flags=re.IGNORECASE)
    if any(failure_marker.search(line) for line in lines[:-1]):
        return {"ok": False, "reason": "route output contains an error/interruption marker"}
    match = FINAL_RE.fullmatch(final_lines[0])
    if not match:
        return {"ok": False, "reason": "route FINAL is malformed"}
    state = match.group(1)
    accepted, rejected, unknown, total, resolved = map(int, match.groups()[1:])
    if total < 1 or accepted + rejected + unknown != total:
        return {"ok": False, "reason": "route FINAL totals are inconsistent"}
    if require_resolved and resolved != 1:
        return {"ok": False, "reason": "route FINAL is not durably resolved"}
    try:
        raw_snapshot = json.loads(snapshot_lines[0][len(SNAPSHOT_PREFIX):])
    except (TypeError, json.JSONDecodeError):
        return {"ok": False, "reason": "route SNAPSHOT is not JSON"}
    if not isinstance(raw_snapshot, list) or len(raw_snapshot) != total:
        return {"ok": False, "reason": "route SNAPSHOT length differs from FINAL"}

    accounts = _accounts(expected_accounts)
    expected_keys = ({(account, leg) for account in accounts for leg in LEGS}
                     if accounts is not None else None)
    rows, keys, order_ids, receipts = [], set(), set(), set()
    for raw in raw_snapshot:
        if not isinstance(raw, dict):
            return {"ok": False, "reason": "route SNAPSHOT row is not an object"}
        account = str(raw.get("accountId") or "").strip()
        leg = str(raw.get("legId") or "").strip().upper()
        row_state = str(raw.get("state") or "").strip().upper()
        order_id = str(raw.get("orderId") or "").strip()
        receipt = str(raw.get("receipt") or "").strip()
        key = (account, leg)
        if (not account or len(account) > 64 or leg not in LEGS
                or row_state not in {"ACCEPTED", "REJECTED", "UNKNOWN"}
                or key in keys):
            return {"ok": False, "reason": "route SNAPSHOT account/leg/state is invalid"}
        if row_state == "ACCEPTED":
            if not order_id or not receipt or order_id in order_ids or receipt in receipts:
                return {"ok": False, "reason": "accepted route leg lacks unique broker identity"}
            order_ids.add(order_id)
            receipts.add(receipt)
        keys.add(key)
        extras, extras_ok = _identity_extras(raw)
        if not extras_ok:
            return {"ok": False, "reason": "route SNAPSHOT identity extras are invalid"}
        rows.append({"accountId": account, "legId": leg, "state": row_state,
                     "orderId": order_id or None, "receipt": receipt or None,
                     "filledAt": raw.get("filledAt"), **extras})
    if expected_keys is None:
        inferred_accounts = {account for account, _leg in keys}
        expected_keys = {(account, leg) for account in inferred_accounts for leg in LEGS}
    if keys != expected_keys:
        return {"ok": False, "reason": "route SNAPSHOT is not the complete account/leg product"}
    counts = {name: sum(row["state"] == name for row in rows)
              for name in ("ACCEPTED", "REJECTED", "UNKNOWN")}
    if (counts["ACCEPTED"] != accepted or counts["REJECTED"] != rejected
            or counts["UNKNOWN"] != unknown):
        return {"ok": False, "reason": "route SNAPSHOT counts differ from FINAL"}
    structurally_valid = (
        (state == "SENT" and accepted == total and rejected == unknown == 0)
        or (state == "REJECTED" and rejected == total and accepted == unknown == 0)
        or (state == "PARTIAL" and 0 < accepted < total and rejected + unknown > 0)
        or (state == "UNKNOWN" and unknown > 0 and accepted == 0)
    )
    if not structurally_valid:
        return {"ok": False, "reason": "route state disagrees with leg outcomes"}
    return {"ok": True, "state": state, "acceptedCount": accepted,
            "explicitRejectCount": rejected, "unknownCount": unknown,
            "totalAttempts": total, "resolved": resolved,
            "snapshot": rows, "final": final_lines[0]}


def format_envelope(snapshot: list[dict], state: str, *, resolved: int = 1) -> str:
    """Test/stub helper producing the same two-line canonical envelope."""
    counts = {name: sum(str(row.get("state") or "").upper() == name for row in snapshot)
              for name in ("ACCEPTED", "REJECTED", "UNKNOWN")}
    snap = SNAPSHOT_PREFIX + json.dumps(snapshot, ensure_ascii=True, sort_keys=True,
                                        separators=(",", ":"))
    final = (f"{FINAL_PREFIX}STATE={str(state).upper()} ACCEPTED={counts['ACCEPTED']} "
             f"REJECTED={counts['REJECTED']} UNKNOWN={counts['UNKNOWN']} "
             f"TOTAL={len(snapshot)} RESOLVED={int(resolved)}")
    return snap + "\n" + final

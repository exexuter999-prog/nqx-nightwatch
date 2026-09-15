# -*- coding: utf-8 -*-
"""R46: 手動で進行中の建玉がある口座だけを自動発注から外す。

「凍結プランで所有権を束縛できない建玉＝手動」とみなし、その口座だけを経路から
落として残りの口座は通す。所有権を*証明できないだけ*の状態は従来どおり経路全体を
止める。外部送信: ゼロ（runner はスタブ、broker 照会もスタブ）。
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import autotrade_engine as ae  # noqa: E402
import _pin_contract  # noqa: E402  (R102: 本番の manualHalt と限月をテストから切り離す)
import strategy_evidence  # noqa: E402


def check(label, condition, detail=""):
    if not condition:
        raise AssertionError(f"{label}: {detail}")
    print(f"OK   {label}")


NOW = datetime.now(timezone.utc)
MANUAL = "ACC-MANUAL"
AUTO_A = "ACC-AUTO-A"
AUTO_B = "ACC-AUTO-B"
EVIDENCE = strategy_evidence.canonicalize({
    "version": "R14-STRATEGY-EVIDENCE-1", "asOf": NOW.isoformat(),
    "sessionId": "NY-R46", "source": "fixture", "provenance": "test",
    "models": {"ifvg": {"valid": False}},
})

SCENARIO = {
    "scenarioId": "sc-r46", "decisionId": "sc-r46", "fingerprint": "fp-r46",
    "model": "VP80_REVERSION",
    "marketCycleId": "cycle-r46", "symbol": "MNQU6", "side": "BUY", "qty": 2,
    "entry": 20000, "stop": 19980, "target": 20040, "targets": [20040, 20080],
    "targetR": [1.0, 3.0],
    "legs": [{"id": "TP1", "qty": 1, "target": 20040},
             {"id": "RUNNER", "qty": 1, "target": 20080}],
    "planVersion": "R17-SPLIT-1", "grade": "A", "state": "ARMED",
    "issuedAt": NOW.isoformat(), "observedAt": NOW.isoformat(),
    "expiresAt": (NOW + timedelta(minutes=5)).isoformat(),
    "evidenceHash": EVIDENCE["evidenceHash"], "setupVersion": "R14-SETUP",
    "catalogVersion": "R14-CATALOG", "detectorVersion": "R14-DETECTOR",
    "executionContractVersion": "R22-EXECUTION-CONTRACT-1",
    "executionContract": {"accountScope": [AUTO_A, AUTO_B, MANUAL],
                          "riskCapDollars": 240.0},
}
BUNDLE = {"_published_scenario": dict(SCENARIO), "price": 19999,
          "at": NOW.isoformat(), "cvdAt": NOW.isoformat(),
          "snapshot": {"bars3m": [{"h": 20001, "l": 19998, "c": 19999}] * 12}}
ENV = {"NQX_SYMBOL": "MNQU6", "NQX_AUTOTRADE": "1", "NQX_LIVE_ORDERS": "0",
       "CROSSTRADE_ACCOUNTS": ",".join([AUTO_A, AUTO_B, MANUAL])}


def authoritative_view():
    """R15 committed hash-only state so the ENTRY path is reachable offline."""
    server_scenario = {key: value for key, value in SCENARIO.items()
                       if key not in {"decisionId", "model"}}
    server_scenario["cycleCommitted"] = True
    return {
        "market": {"at": NOW.isoformat(), "observedAt": NOW.isoformat(),
                   "cvdAt": NOW.isoformat(), "cycleId": SCENARIO["marketCycleId"],
                   "cycleCommitted": True, "verified": True, "source": "fixture",
                   "sourceSymbol": "CME_MINI:MNQU6", "strategyEvidence": EVIDENCE},
        "scenario": server_scenario,
        "position": {"state": "FLAT"}, "order": {"state": "NONE", "verified": True},
        "display": {"orderable": True, "cyclePaired": True},
    }


def positions(open_account, qty=4):
    def query(symbol, account=None):
        if account == open_account:
            return {"verified": True, "symbol": symbol, "qty": qty,
                    "accountId": account, "side": "BUY", "avgPrice": 19000.0,
                    "rawIdentity": f"{account}:{symbol}:BUY:{qty}",
                    "generation": "gen-manual-1"}
        return {"verified": True, "symbol": symbol, "qty": 0, "accountId": account}
    return query


def orders_flat(symbol, known_order_ids=None, account=None):
    return {"verified": True, "state": "NONE", "accountId": account, "rows": []}


def run(bundle, env, broker_query, ledger_rows=None):
    tmp = tempfile.mkdtemp()
    ledger = os.path.join(tmp, "ledger.jsonl")
    if ledger_rows:
        with open(ledger, "w", encoding="utf-8") as handle:
            for row in ledger_rows:
                handle.write(row + "\n")
    sent = []

    def runner(args, live):
        sent.append((list(args), live))
        return 0, "stub dry-run ok"

    notes = ae.reconcile(bundle, True, env, broker_query=broker_query, runner=runner,
                         ledger_path=ledger, broker_order_query=orders_flat,
                         state_query=authoritative_view)
    return notes, sent


# --- 1) 台帳に無い建玉 = 手動。その口座だけ外して残り2口座は評価が続く -------
notes, sent = run(BUNDLE, ENV, positions(MANUAL))
check("手動建玉の口座は除外注記が残る",
      any("autotrade excluded" in note and MANUAL in note for note in notes), notes)
check("残った口座は経路に生きている(提案まで到達する)",
      any("autotrade proposal ENTRY" in note for note in notes), notes)
check("確定送信(--confirm)は一度も起きていない",
      all(live is False for _args, live in sent), sent)
entry_args = [args for args, _live in sent if "--split-tp" in args]
check("dry-run の宛先から手動口座が消えている",
      entry_args and MANUAL not in entry_args[0][entry_args[0].index("--accounts") + 1],
      entry_args)
check("dry-run の宛先に残り2口座が入っている",
      entry_args[0][entry_args[0].index("--accounts") + 1] == f"{AUTO_A},{AUTO_B}",
      entry_args)

# --- 2) 除外後の宛先は凍結スコープから手動口座が消えている -------------------
live_env = {**ENV, "NQX_LIVE_ORDERS": "1"}
narrowed = ae._narrow_bundle_scope(BUNDLE, [AUTO_A, AUTO_B])
scope = narrowed["_published_scenario"]["executionContract"]["accountScope"]
check("narrow は凍結スコープを狭めるだけ", scope == [AUTO_A, AUTO_B], scope)
excluded = narrowed["_published_scenario"]["executionContract"]["excludedAccounts"]
check("外した口座は excludedAccounts に理由付きで残る",
      excluded == [{"account": MANUAL, "reason": "MANUAL_POSITION"}], excluded)
check("元の bundle は書き換えない",
      BUNDLE["_published_scenario"]["executionContract"]["accountScope"]
      == [AUTO_A, AUTO_B, MANUAL],
      BUNDLE["_published_scenario"]["executionContract"]["accountScope"])
check("凍結スコープに無い口座は narrow で増えない",
      ae._narrow_bundle_scope(BUNDLE, [AUTO_A, "ACC-NOT-FROZEN"])
      ["_published_scenario"]["executionContract"]["accountScope"] == [AUTO_A])

# --- 3) order.py へ渡す宛先が凍結スコープに一致する --------------------------
plan = {"side": "BUY", "qty": 2, "initialStop": 19980, "symbol": "MNQU6",
        "entry": 20000, "legs": [{"target": 20040}, {"target": 20080}],
        "accountScope": [AUTO_A, AUTO_B]}
args = ae._command_for_entry(plan, {"price": 19999})
check("ENTRY コマンドに --accounts が付く", "--accounts" in args, args)
check("--accounts の値は凍結スコープそのもの",
      args[args.index("--accounts") + 1] == f"{AUTO_A},{AUTO_B}", args)
check("scope が空なら --accounts を付けない",
      "--accounts" not in ae._command_for_entry({**plan, "accountScope": []},
                                                {"price": 19999}))

# --- 4) 手動と断定できない理由では従来どおり経路全体が止まる -----------------
check("識別不能な理由は手動扱いにしない",
      not ae._is_manual_position_account(
          ["autotrade hold: broker position strong identity is unavailable"]))
check("理由が混ざっていれば手動扱いにしない",
      not ae._is_manual_position_account([
          ae.MANUAL_POSITION_HOLDS[0],
          "autotrade hold: broker position strong identity is unavailable"]))
check("空の note は手動扱いにしない", not ae._is_manual_position_account([]))
check("凍結スコープ外の建玉は手動",
      ae._is_manual_position_account([ae.MANUAL_POSITION_HOLDS[1]]))
check("凍結プランが無い建玉は手動",
      ae._is_manual_position_account([ae.MANUAL_POSITION_HOLDS[0]]))

# --- 5) KILL は除外しない。台帳外の建玉も落とす明示経路のままにする ---------
kill_env = {**ENV, "NQX_AUTOTRADE_KILL": "1"}
notes, sent = run(BUNDLE, kill_env, positions(MANUAL))
check("KILL 中は除外注記を出さない",
      not any("autotrade excluded" in note for note in notes), notes)
check("KILL 中は新規提案へ落とさない",
      not any("autotrade proposal ENTRY" in note for note in notes), notes)

# --- 6) 全口座が手動なら新規は出さない ---------------------------------------
def all_open(symbol, account=None):
    return {"verified": True, "symbol": symbol, "qty": 4, "accountId": account,
            "side": "BUY", "avgPrice": 19000.0,
            "rawIdentity": f"{account}:{symbol}:BUY:4", "generation": "gen-manual-1"}


notes, sent = run(BUNDLE, ENV, all_open)
check("全口座が手動なら ENTRY へ落とさない",
      not any("autotrade proposal ENTRY" in note for note in notes), notes)
check("その場合も送信はゼロ", not sent, sent)

print("ALL PASS (test_r46_manual_account_exclusion; external sends=0)")

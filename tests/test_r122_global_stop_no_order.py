# -*- coding: utf-8 -*-
"""R122 選択層: **全体停止のときは、別の適格候補があっても注文が 0 件**。

`docs/reports/R122_SELECTION_REVIEW_2026-09-20.md` の指摘に応える統合試験。
選択層(`structureContext.selection`)を LIVE にしても、市場全体・口座全体の停止条件が
立っている周期では `order.py` の呼び出しが **1 件も出ない**ことを、
`autotrade_engine.reconcile` を実際に通して数える。

検査する停止条件:

  1. ボラ床スタンドダウン(`monitor_publish.apply_volatility_grade_gate`)
  2. High イベント窓(`publish_state` が chosen を WATCH へ降格)
  3. 限月の満期ガード(同上)
  4. 取得受領書のゲート(`enrich_decisive_strategy` が decision を WATCH へ)
  5. 設定側の手動 HALT(`execution_contract.json` の `manualHalt.autotrade`)
  6. AUTO OFF(`NQX_AUTOTRADE=0`)
  7. 建玉照会が UNVERIFIED

**空振り防止**: 同じ入力で全部の停止条件を外した「陽性対照」を先に走らせ、そこでは
実際に `--confirm` 付きの送信が起きることを確認する。陽性対照が 0 件なら試験は無効。

隔離: `runner`(order.py の起動点)・ブローカー照会・claim / state はすべて注入スタブ。
台帳は一時ディレクトリ。`socket` / `subprocess` は audit hook で遮断し、本番の
`.secrets` / CrossTrade / Worker / TradingView へは到達しない。

    python tests/test_r122_global_stop_no_order.py
"""
import contextlib
import copy
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.dirname(HERE)
sys.path.insert(0, BASE)
sys.path.insert(0, HERE)

# --- 通信と外部プロセスを止める(到達したら試験を落とす)-------------------
BLOCKED = []


def _audit(event, args):
    if event in ("socket.connect", "socket.getaddrinfo", "subprocess.Popen", "os.exec"):
        BLOCKED.append(event)
        raise AssertionError(f"外部到達 {event} はこの試験では禁止")


sys.addaudithook(_audit)

import autotrade_engine as ae  # noqa: E402
import _pin_contract  # noqa: E402  (manualHalt OFF / 限月固定)
import execution_contract  # noqa: E402
import execution_intent  # noqa: E402
import monitor_publish  # noqa: E402
import msnr_gate  # noqa: E402
import nqx_state  # noqa: E402
import route_envelope  # noqa: E402
import strategy_evidence  # noqa: E402

ae._read_env_file = lambda path=None: {}
for _key in ("NQX_AUTOTRADE", "NQX_LIVE_ORDERS", "NQX_AUTOTRADE_KILL", ae.ENTRY_DISARMED_KEY):
    os.environ.pop(_key, None)

NOW = datetime.now(timezone.utc)
ACC = "ACC-R122"
SYM = "MNQU6"
failures = []


def check(label, condition, detail=""):
    if not condition:
        failures.append(label)
        print(f"FAIL {label}: {detail}")
        return
    print(f"OK   {label}")


# ------------------------------------------------- 1. 別の適格候補がある入力

def policy(selection):
    return {"context": {"mode": "LIVE"}, "participation": {"mode": "LIVE"},
            "shallowCandidate": {"mode": "SHADOW"}, "selection": {"mode": selection},
            "nearTerm": {"mode": "SHADOW"}, "params": {}, "invalid": []}


def candidate(model, side, entry, stop, tp1, final, grade, score, blockers=()):
    """`select_primary` が読む形の候補。ブロッカーが空なら『武装できる候補』。"""
    return {"model": model, "side": side, "entry": entry, "stop": stop,
            "targets": [tp1, final], "targetR": [2.0, 4.0], "targetLabels": ["TP1", "RUNNER"],
            "grade": grade, "score": score, "evidence": [], "penalties": [],
            "hardBlockers": list(blockers), "allowed": not blockers,
            "state": "WATCH" if blockers else "ARMED",
            "chain": {"type": "FLIP", "state": "FLIP_HELD", "side": side,
                      "breakBarT": 1786970000, "barsLeft": 8}}


#: 高得点だが候補固有の理由で成立していない primary と、完全に適格な代替候補。
HIGH_WATCH = candidate("BREAKER_CONTINUATION", "SELL", 100.0, 110.0, 80.0, 60.0,
                       "A", 7, ["ANCHOR_CONSUMED"])
LOW_ARMED = candidate("VP80_REVERSION", "BUY", 100.0, 90.0, 110.0, 130.0, "B", 3)


def select(mode):
    return msnr_gate.select_primary([HIGH_WATCH, LOW_ARMED], {},
                                    context={"status": "OK", "childRelation": "CONTINUATION",
                                             "thesis": {"structureId": "T", "bias": "BUY"},
                                             "child": {}, "children": {}},
                                    structure_policy=policy(mode))


def test_eligible_alternative_exists():
    """現行(OFF)は高得点 WATCH を、selection LIVE は**武装できる候補**を primary にする。"""
    off, live = select("OFF"), select("LIVE")
    check("現行の primary は高得点だが WATCH",
          off["model"] == "BREAKER_CONTINUATION" and off["state"] == "WATCH", off["state"])
    check("同じ周期に武装できる代替候補がある", LOW_ARMED["allowed"] is True)
    check("selection LIVE はその代替候補を primary にする",
          live["model"] == "VP80_REVERSION" and live["state"] == "ARMED"
          and not live["hardBlockers"], (live["model"], live["state"]))
    check("選んだ理由が残る",
          live["structure"]["selectionReason"] == "ELIGIBLE_PREFERRED_OVER_HIGHER_SCORE_WATCH",
          live.get("structure"))
    return live


def selected_scenario():
    """selection LIVE が選んだ候補から、publish される scenario を実際に作る。"""
    live = select("LIVE")
    scenario = msnr_gate.decision_to_scenario(live, {}, qty=2)
    assert scenario, "decision_to_scenario が scenario を作れない"
    return {**scenario,
            "fingerprint": "fp-r122", "symbol": SYM, "marketCycleId": "cycle-r122",
            "issuedAt": NOW.isoformat(), "observedAt": NOW.isoformat(),
            "expiresAt": (NOW + timedelta(minutes=5)).isoformat(),
            "evidenceHash": EVIDENCE["evidenceHash"], "setupVersion": "R14-SETUP",
            "catalogVersion": "R14-CATALOG", "detectorVersion": "R14-DETECTOR",
            "executionContractVersion": execution_contract.VERSION,
            "executionContract": {"accountScope": [ACC]}}


# ------------------------------------------------------ 2. reconcile の harness

EVIDENCE = strategy_evidence.canonicalize({
    "version": "R14-STRATEGY-EVIDENCE-1", "asOf": NOW.isoformat(),
    "sessionId": "NY-R122", "source": "fixture", "provenance": "test",
    "models": {"ifvg": {"valid": False}},
})
#: **selection LIVE が実際に選んだ代替候補**から作った、publish される scenario。
SCENARIO = selected_scenario()
BUNDLE = {"_published_scenario": SCENARIO, "price": 99,
          "at": NOW.isoformat(), "cvdAt": NOW.isoformat(),
          "snapshot": {"bars3m": [{"h": 101, "l": 98, "c": 99}] * 12}}


def _claim_stub(scenario, **kwargs):
    order_type = str(kwargs.get("order_type") or "LIMIT").upper()
    market = {"verified": True, "price": BUNDLE["price"]} if order_type == "MARKET" else None
    intent = execution_intent.from_scenario(scenario, market, order_type=order_type,
                                            account_scope=[ACC])
    return True, {"entryKey": ae._entry_key(scenario), "claimToken": "T" * 43,
                  "executionIntent": intent,
                  "executionIntentHash": execution_intent.intent_hash(intent)}


nqx_state.claim_entry = _claim_stub


def authoritative_view(scenario):
    market = {"at": NOW.isoformat(), "observedAt": NOW.isoformat(), "cvdAt": NOW.isoformat(),
              "cycleId": scenario["marketCycleId"], "cycleCommitted": True, "verified": True,
              "source": "fixture", "sourceSymbol": "CME_MINI:MNQU6",
              "strategyEvidence": EVIDENCE}
    # 本番の publish は scenario を hash-only で送る(engine の
    # "scenario must be hash-only")。同じ形にする。
    server = {k: v for k, v in scenario.items()
              if k not in {"decisionId", "model", "structure", "structureDetail",
                           "strategyEvidence", "eligibleVotes"}}
    server["cycleCommitted"] = True
    return {"market": market, "scenario": server,
            "position": {"state": "FLAT"}, "order": {"state": "NONE", "verified": True},
            "display": {"orderable": True, "cyclePaired": True}}


FILLED_ORDERS = {"verified": True, "state": "FILLED", "activeOrders": [],
                 "orders": [{"accountId": ACC, "symbol": SYM, "action": "BUY", "qty": 1,
                             "orderType": "MARKET", "filledPrice": 99,
                             "orderId": f"ENTRY-{leg}", "receipt": f"RECEIPT-{leg}",
                             "status": "FILLED"} for leg in ("TP1", "RUNNER")]}


@contextlib.contextmanager
def manual_halt(on):
    cfg = execution_contract.CONTRACT.setdefault("manualHalt", {})
    before = cfg.get("autotrade")
    cfg["autotrade"] = on
    try:
        yield
    finally:
        if before is None:
            cfg.pop("autotrade", None)
        else:
            cfg["autotrade"] = before


def run_reconcile(scenario, *, auto="1", live="1", verified=True, ledger=None):
    """1 周期ぶん `reconcile` を通し、order.py の呼び出し(runner)を数える。"""
    calls = []

    def runner(args, confirm):
        calls.append((list(args), bool(confirm)))
        if not confirm:
            return 0, "stub dry-run ok"
        rows = [{"accountId": ACC, "legId": leg, "state": "ACCEPTED",
                 "orderId": f"ENTRY-{leg}", "receipt": f"RECEIPT-{leg}"}
                for leg in ("TP1", "RUNNER")]
        return 0, route_envelope.format_envelope(rows, "SENT")

    def position(_symbol, account=None):
        if not verified:
            return {"verified": False, "detail": "UNVERIFIED (fixture)"}
        return {"verified": True, "symbol": SYM, "accountId": ACC, "qty": 0,
                "observedAt": NOW.isoformat()}

    def orders(_symbol, known_order_ids=None, account=None):
        if not verified:
            return {"verified": False, "state": "UNKNOWN", "detail": "UNVERIFIED (fixture)"}
        return copy.deepcopy(FILLED_ORDERS)

    bundle = {**BUNDLE, "_published_scenario": scenario}
    cfg = {"NQX_SYMBOL": SYM, "CROSSTRADE_ACCOUNTS": ACC,
           "NQX_AUTOTRADE": auto, "NQX_LIVE_ORDERS": live,
           "NQX_AUTOTRADE_KILL": "0", "NQX_STALE_ENTRY_CANCEL": "0"}
    notes = ae.reconcile(bundle, True, cfg, position, runner, ledger, NOW,
                         broker_order_query=orders,
                         state_query=lambda: authoritative_view(scenario),
                         broker_fills_query=lambda _a: {"verified": True, "fills": []},
                         fresh_price_query=lambda _s: 99.0)
    return calls, notes


# --------------------------------------------------- 3. 陽性対照(空振り防止)

def test_positive_control_sends():
    """停止条件が無ければ、この harness は実際に `--confirm` 付きの送信を出す。"""
    with tempfile.TemporaryDirectory() as tmp, manual_halt(False):
        calls, notes = run_reconcile(SCENARIO, ledger=os.path.join(tmp, "a.jsonl"))
    confirmed = [args for args, confirm in calls if confirm]
    check("陽性対照: 停止条件が無ければ order.py が呼ばれる",
          len(calls) >= 1 and confirmed, (len(calls), notes[:2]))
    check("陽性対照: 送信は分割ブラケット(--split-tp)",
          any("--split-tp" in args for args in confirmed), confirmed[:1])


# ------------------------------------------------ 4. 全体停止 → 注文 0 件

def demoted(reason):
    """publish 経路が全体停止で WATCH へ落とした後のシナリオ。"""
    return {**SCENARIO, "state": "WATCH", "_demotedBy": reason}


def test_volatility_standdown_sends_nothing():
    vg = {"active": True, "ratio_all": 0.99, "noise": 59.0}
    chosen, note = monitor_publish.apply_volatility_grade_gate(dict(SCENARIO), vg)
    check("ボラ床は実際に ARMED を WATCH へ落とす",
          chosen["state"] == "WATCH" and note, (chosen.get("state"), note))
    with tempfile.TemporaryDirectory() as tmp, manual_halt(False):
        calls, notes = run_reconcile(chosen, ledger=os.path.join(tmp, "vol.jsonl"))
    check("ボラ床スタンドダウン → 注文 0 件", len(calls) == 0, (len(calls), notes[:2]))


def test_event_blackout_sends_nothing():
    with tempfile.TemporaryDirectory() as tmp, manual_halt(False):
        calls, notes = run_reconcile(demoted("EVENT_BLACKOUT"),
                                     ledger=os.path.join(tmp, "ev.jsonl"))
    check("High イベント窓 → 注文 0 件", len(calls) == 0, (len(calls), notes[:2]))


def test_contract_expiry_sends_nothing():
    with tempfile.TemporaryDirectory() as tmp, manual_halt(False):
        calls, notes = run_reconcile(demoted("CONTRACT_EXPIRY_NEAR"),
                                     ledger=os.path.join(tmp, "ct.jsonl"))
    check("限月の満期ガード → 注文 0 件", len(calls) == 0, (len(calls), notes[:2]))


def test_acquisition_gate_sends_nothing():
    with tempfile.TemporaryDirectory() as tmp, manual_halt(False):
        calls, notes = run_reconcile(demoted("ACQUISITION_DATA_STALE"),
                                     ledger=os.path.join(tmp, "aq.jsonl"))
    check("取得受領書のゲート → 注文 0 件", len(calls) == 0, (len(calls), notes[:2]))


def test_manual_halt_sends_nothing():
    with tempfile.TemporaryDirectory() as tmp, manual_halt(True):
        calls, notes = run_reconcile(SCENARIO, ledger=os.path.join(tmp, "halt.jsonl"))
    check("手動 HALT → 注文 0 件", len(calls) == 0, (len(calls), notes[:2]))
    check("手動 HALT の理由が注記に残る",
          any("MANUAL HALT" in str(n) for n in notes), notes[:2])


def test_auto_off_sends_nothing():
    with tempfile.TemporaryDirectory() as tmp, manual_halt(False):
        calls, notes = run_reconcile(SCENARIO, auto="0", live="0",
                                     ledger=os.path.join(tmp, "auto.jsonl"))
    check("AUTO OFF → 注文 0 件", len(calls) == 0, (len(calls), notes[:2]))


def test_broker_unverified_sends_nothing():
    with tempfile.TemporaryDirectory() as tmp, manual_halt(False):
        calls, notes = run_reconcile(SCENARIO, verified=False,
                                     ledger=os.path.join(tmp, "bk.jsonl"))
    check("建玉照会 UNVERIFIED → 注文 0 件", len(calls) == 0, (len(calls), notes[:2]))


def test_selection_live_does_not_change_the_gates():
    """選択層の mode を変えても、全体停止の判定は 1 つも変わらない。"""
    demote = demoted("EVENT_BLACKOUT")
    for mode in ("OFF", "SHADOW", "LIVE"):
        saved = msnr_gate.structure_context_policy
        try:
            msnr_gate.structure_context_policy = lambda contract=None, m=mode: policy(m)
            with tempfile.TemporaryDirectory() as tmp, manual_halt(False):
                calls, _notes = run_reconcile(demote, ledger=os.path.join(tmp, f"{mode}.jsonl"))
        finally:
            msnr_gate.structure_context_policy = saved
        check(f"selection={mode} でも全体停止中は注文 0 件", len(calls) == 0, len(calls))


def main():
    print("--- 入力に別の適格候補があること ---")
    test_eligible_alternative_exists()
    tests = [value for key, value in sorted(globals().items())
             if key.startswith("test_") and key != "test_eligible_alternative_exists"]
    for test in tests:
        print(f"\n--- {test.__name__} ---")
        test()
    print()
    check("外部到達は 0 件(socket / subprocess)", not BLOCKED, BLOCKED)
    if failures:
        print(f"FAILED {len(failures)}: " + ", ".join(failures))
        return 1
    print("ALL PASS (test_r122_global_stop_no_order)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

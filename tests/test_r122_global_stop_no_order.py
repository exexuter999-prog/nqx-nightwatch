# -*- coding: utf-8 -*-
"""R122 選択層: **全体停止のときは、別の適格候補があっても注文が 0 件**。

`docs/reports/R122_SELECTION_REVIEW_2026-09-20.md` の指摘に応える統合試験。
停止条件を**入力として与え**、本番の判定処理(降格)から `autotrade_engine.reconcile` の
注文呼び出しまでを通して数える。**手作業で `state: WATCH` を書かない。**

経路(スタブは外部 I/O だけ):

    fixture(実サイクルの確定 3 分足・レベル・価格)
      → monitor_publish.enrich_decisive_strategy   (msnr_gate の候補生成 → primary 選択)
      → monitor_publish.publish_state              (イベント窓 / 限月 / ボラ の降格はここ)
      → bundle["_published_scenario"]
      → autotrade_engine.reconcile                 (order.py の呼び出しを数える)

スタブしたのは **外部 I/O だけ**:

  * `nqx_state.load_cloud_env / _read_kv_env / publish_cycle / publish_accounts`(Cloudflare)
  * ブローカー照会と `runner`(= order.py の起動点)、claim(Worker)
  * `events.CACHE` / `events.MANUAL` の**パス**(イベント表そのものが入力。判定は本物の
    `events.current_gate` / `active_blackout` が行う)
  * 台帳とロックは一時ディレクトリ

停止条件は**設定・入力**として与える:

  1. ボラ床    … 実測の比率(≈0.29)に対して停止しきい値(`NQX_VOL_GATE_STANDDOWN`)を
                  下げ、**本物の `vol_gate` / `apply_volatility_grade_gate`** に降格させる
  2. イベント窓 … 手動イベント表に High を 1 件入れ、**本物の `active_blackout`** に当てる
  3. 限月満期  … 契約の時計を満期の 1 日前にし、**本物の `contract.entry_allowed`** に当てる
  4. 取得受領書 … bundle から `acquisitionReceipt` を落とし、**本物の
                  `acquisition_display_gate`** に降格させる
  5. 手動 HALT / 6. AUTO OFF / 7. 建玉照会 UNVERIFIED … engine 側の設定・照会結果

OFF / SHADOW / LIVE の 3 モードとも、**候補選択からやり直して**同じ経路を通す。
陽性対照(停止条件なし)では LIVE が実際に `--confirm` 付きの送信を出す。

    python tests/test_r122_global_stop_no_order.py
"""
import contextlib
import io
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.dirname(HERE)
sys.path.insert(0, BASE)
sys.path.insert(0, HERE)

# fixture は過去の実サイクルなので、価格の鮮度窓だけ広げる(本番と同じ環境変数の経路)。
os.environ.setdefault("NQX_MARKET_MAX_AGE_SEC", "999999999")

# --- 通信と外部プロセスを止める(到達したら試験を落とす)-------------------
BLOCKED = []


def _audit(event, args):
    if event in ("socket.connect", "socket.getaddrinfo", "subprocess.Popen", "os.exec"):
        BLOCKED.append(event)
        raise AssertionError(f"外部到達 {event} はこの試験では禁止")


sys.addaudithook(_audit)

import _pin_contract  # noqa: E402  (manualHalt OFF / 限月固定 / 時計の固定)
import execution_contract as _ec  # noqa: E402

# fixture は 2026-09-18 の実サイクルで、当時の発注先限月は MNQZ6(12 月限)。
# R102 の限月ガードを**本物のまま**通すため、契約ブロックをその日の実態へ合わせる。
# 時計も fixture の日付に置き、満期まで十分な日数がある状態にする。
_ec.CONTRACT["contract"] = {
    "symbol": "MNQZ6", "tvSymbol": "CME_MINI:MNQZ2026", "expiry": "2026-12-18",
    "lastEntryDaysBeforeExpiry": 3,
    "next": {"symbol": "MNQH7", "tvSymbol": "CME_MINI:MNQH2027", "expiry": "2027-03-19"},
    "nakedRepair": {"mode": "OFF", "graceSec": 0},
    "clockOverride": "2026-09-18T15:26:00+00:00",
}
import autotrade_engine as ae  # noqa: E402
import events as econ_events  # noqa: E402
import execution_contract  # noqa: E402
import execution_intent  # noqa: E402
import monitor_publish  # noqa: E402
import msnr_gate  # noqa: E402
import nqx_state  # noqa: E402
import route_envelope  # noqa: E402

ae._read_env_file = lambda path=None: {}
for _key in ("NQX_AUTOTRADE", "NQX_LIVE_ORDERS", "NQX_AUTOTRADE_KILL", ae.ENTRY_DISARMED_KEY):
    os.environ.pop(_key, None)

ACC = "ACC-R122"
SYM = "MNQZ6"
FIXTURE = os.path.join(HERE, "fixtures_r122_cycle.json")
MODES = ("OFF", "SHADOW", "LIVE")
failures = []


def check(label, condition, detail=""):
    if not condition:
        failures.append(label)
        print(f"FAIL {label}: {detail}")
        return
    print(f"OK   {label}")


# ------------------------------------------------ fixture(実サイクルの入力)

def load_fixture():
    """実サイクル(2026-09-18 15:26Z)の入力を**そのまま**返す。

    時刻をずらすと ET の時間帯が変わり、killzone の加点・確認要素が動いて候補集合が
    変わってしまう(実測: 03:31 ET へ移すと TURTLE SELL が NO_CONFIRMATION で
    武装しなくなった)。だから**ずらさない**。代わりに
      * 価格の鮮度窓は `NQX_MARKET_MAX_AGE_SEC`(本番と同じ環境変数)で広げ、
      * engine には `now = fixture の at` を渡す(`reconcile` の引数)
    ことで、判定はすべて fixture の時刻で行われる。
    """
    return json.load(io.open(FIXTURE, encoding="utf-8"))


def policy(selection):
    return {"context": {"mode": "LIVE"}, "participation": {"mode": "LIVE"},
            "shallowCandidate": {"mode": "SHADOW"}, "selection": {"mode": selection},
            "nearTerm": {"mode": "SHADOW"}, "params": {}, "invalid": []}


# ----------------------------------------------- 外部 I/O のスタブ(それだけ)

nqx_state.load_cloud_env = lambda required=True: {"NQX_SYMBOL": SYM, "NQX_STATE_URL": "stub"}
nqx_state._read_kv_env = lambda path=None: {"CROSSTRADE_ACCOUNTS": ACC, f"RISK_{ACC}": "240"}
nqx_state.publish_cycle = lambda cid, market, scenario, cfg: (True, {"ok": True})
nqx_state.publish_accounts = lambda cfg: (True, {"ok": True})

_EVENTS_DIR = tempfile.mkdtemp(prefix="r122-events-")
econ_events.CACHE = os.path.join(_EVENTS_DIR, "events_cache.json")
econ_events.MANUAL = os.path.join(_EVENTS_DIR, "events_manual.json")


def _claim_stub(scenario, **kwargs):
    order_type = str(kwargs.get("order_type") or "LIMIT").upper()
    market = {"verified": True, "price": scenario.get("entry")} if order_type == "MARKET" else None
    intent = execution_intent.from_scenario(scenario, market, order_type=order_type,
                                            account_scope=[ACC])
    return True, {"entryKey": ae._entry_key(scenario), "claimToken": "T" * 43,
                  "executionIntent": intent,
                  "executionIntentHash": execution_intent.intent_hash(intent)}


nqx_state.claim_entry = _claim_stub


# ------------------------------------------------ 停止条件(入力として与える)

@contextlib.contextmanager
def event_blackout(_bundle=None):
    """手動イベント表に High を 1 件置く。判定は本物の `events.active_blackout`。

    `events.current_gate()` は**実時計**で窓を判定するので、イベントは「今」に置く。
    """
    rows = [{"title": "R122 fixture High", "country": "USD", "impact": "High",
             "at": datetime.now(timezone.utc).isoformat(), "source": "manual"}]
    io.open(econ_events.MANUAL, "w", encoding="utf-8", newline="\n").write(
        json.dumps(rows, ensure_ascii=False))
    try:
        yield
    finally:
        os.remove(econ_events.MANUAL)


@contextlib.contextmanager
def contract_expiry_near():
    """契約の時計を満期の 1 日前へ。判定は本物の `contract.entry_allowed`。"""
    block = execution_contract.CONTRACT["contract"]
    before = block.get("clockOverride")
    expiry = datetime.fromisoformat(str(block["expiry"])).replace(tzinfo=timezone.utc)
    block["clockOverride"] = (expiry - timedelta(days=1)).isoformat()
    try:
        yield
    finally:
        block["clockOverride"] = before


@contextlib.contextmanager
def vol_standdown():
    """実測のボラ比率で止まるよう、停止しきい値を下げる。判定は本物のゲート。"""
    before = monitor_publish.VOL_GATE_STANDDOWN
    os.environ["NQX_VOL_GATE_STANDDOWN"] = "0.20"
    monitor_publish.VOL_GATE_STANDDOWN = float(os.environ["NQX_VOL_GATE_STANDDOWN"])
    try:
        yield
    finally:
        monitor_publish.VOL_GATE_STANDDOWN = before
        os.environ.pop("NQX_VOL_GATE_STANDDOWN", None)


@contextlib.contextmanager
def manual_halt(on=True):
    cfg = execution_contract.CONTRACT.setdefault("manualHalt", {})
    before = cfg.get("autotrade")
    cfg["autotrade"] = on
    try:
        yield
    finally:
        cfg["autotrade"] = before


@contextlib.contextmanager
def nothing():
    yield


# --------------------------------------------------------------- 1 周期を通す

def authoritative_view(scenario, bundle):
    """Worker が返す正本の形(publish した scenario をそのまま映す)。"""
    if not scenario:
        return {}
    at = bundle["at"]
    market = {"at": at, "observedAt": at, "cvdAt": at,
              "cycleId": scenario.get("marketCycleId"), "cycleCommitted": True,
              "verified": True, "source": "fixture",
              "sourceSymbol": bundle.get("sourceSymbol"),
              "strategyEvidence": bundle.get("strategyEvidence")}
    server = {k: v for k, v in scenario.items()
              if k not in {"decisionId", "model", "structure", "structureDetail",
                           "strategyEvidence", "eligibleVotes"}}
    server["cycleCommitted"] = True
    return {"market": market, "scenario": server,
            "position": {"state": "FLAT"}, "order": {"state": "NONE", "verified": True},
            "display": {"orderable": True, "cyclePaired": True}}


def run_cycle(mode, bundle, *, auto="1", live="1", verified=True, ledger_dir="."):
    """fixture → enrich → publish_state → reconcile。order.py の呼び出しを数える。"""
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
                "observedAt": bundle["at"]}

    def orders(_symbol, known_order_ids=None, account=None):
        if not verified:
            return {"verified": False, "state": "UNKNOWN", "detail": "UNVERIFIED (fixture)"}
        return {"verified": True, "symbol": SYM, "state": "NONE", "orders": [],
                "activeOrders": [], "accountScope": [ACC]}

    saved = msnr_gate.structure_context_policy
    try:
        msnr_gate.structure_context_policy = lambda c=None, m=mode: policy(m)
        enriched, _notes = monitor_publish.enrich_decisive_strategy(bundle)
        decision = (enriched.get("evaluation") or {}).get("decision") or {}
        ok, publish_notes = monitor_publish.publish_state(enriched)
        scenario = enriched.get("_published_scenario")
        now = datetime.fromisoformat(str(enriched["at"]).replace("Z", "+00:00"))
        cfg = {"NQX_SYMBOL": SYM, "CROSSTRADE_ACCOUNTS": ACC,
               "NQX_AUTOTRADE": auto, "NQX_LIVE_ORDERS": live,
               "NQX_AUTOTRADE_KILL": "0", "NQX_STALE_ENTRY_CANCEL": "0"}
        notes = ae.reconcile(enriched, True, cfg, position, runner,
                             os.path.join(ledger_dir, f"{mode}.jsonl"), now,
                             broker_order_query=orders,
                             state_query=lambda: authoritative_view(scenario, enriched),
                             broker_fills_query=lambda _a: {"verified": True, "fills": []},
                             fresh_price_query=lambda _s: float(enriched["price"]))
    finally:
        msnr_gate.structure_context_policy = saved
    return {"decision": decision, "scenario": scenario, "publishOk": ok,
            "publishNotes": publish_notes, "calls": calls, "notes": notes}


# ------------------------------------------ 1. 入力に別の適格候補があること

def test_input_has_a_hidden_eligible_candidate():
    """OFF は高得点 WATCH を、LIVE は**武装できる候補**を primary にする実入力。"""
    with tempfile.TemporaryDirectory() as tmp, manual_halt(False):
        results = {m: run_cycle(m, load_fixture(), ledger_dir=tmp) for m in MODES}
    off, live = results["OFF"]["decision"], results["LIVE"]["decision"]
    check("OFF の primary は高得点だが WATCH",
          off.get("state") == "WATCH" and off.get("grade") in ("A", "A+"),
          (off.get("model"), off.get("grade"), off.get("state"), off.get("hardBlockers")))
    check("OFF の WATCH 理由は候補固有(ANCHOR_CONSUMED)",
          "ANCHOR_CONSUMED" in (off.get("hardBlockers") or []), off.get("hardBlockers"))
    check("LIVE は武装できる候補を primary にする",
          live.get("state") == "ARMED" and not live.get("hardBlockers"),
          (live.get("model"), live.get("state"), live.get("hardBlockers")))
    check("LIVE は別のモデル・別の向きを選んでいる",
          live.get("model") != off.get("model") and live.get("side") != off.get("side"),
          (off.get("model"), off.get("side"), live.get("model"), live.get("side")))
    check("選んだ理由が残る",
          (live.get("structure") or {}).get("selectionReason")
          == "ELIGIBLE_PREFERRED_OVER_HIGHER_SCORE_WATCH", live.get("structure"))
    shadow = results["SHADOW"]["decision"]
    check("SHADOW は選ばず、観測だけ残す",
          shadow.get("model") == off.get("model")
          and "SHADOW" in str((shadow.get("structure") or {}).get("selectionReason")),
          shadow.get("structure"))


# ----------------------------------------------- 2. 陽性対照(空振り防止)

def test_positive_control_sends_when_nothing_stops_it():
    """停止条件が無ければ、LIVE は実際に `--confirm` 付きの送信を出す。"""
    with tempfile.TemporaryDirectory() as tmp, manual_halt(False):
        live = run_cycle("LIVE", load_fixture(), ledger_dir=tmp)
        off = run_cycle("OFF", load_fixture(), ledger_dir=tmp)
    confirmed = [args for args, confirm in live["calls"] if confirm]
    check("陽性対照(LIVE): order.py が呼ばれる",
          live["calls"] and confirmed, (len(live["calls"]), live["notes"][:2]))
    check("陽性対照(LIVE): 分割ブラケット(--split-tp)で送る",
          any("--split-tp" in args for args in confirmed), confirmed[:1])
    check("陽性対照(LIVE): 公開された scenario は ARMED",
          (live["scenario"] or {}).get("state") == "ARMED",
          (live["scenario"] or {}).get("state"))
    check("OFF は武装していないので 0 件(現行の振る舞い)",
          len(off["calls"]) == 0, (len(off["calls"]), off["notes"][:2]))


# --------------------------- 3. 全体停止 × 3 モード → 本番の降格 → 注文 0 件

def _run_stop(label, *, condition=None, per_bundle=None, drop_receipt=False,
              auto="1", live="1", verified=True, halt=False,
              expect_watch=True, expect_note=None):
    with tempfile.TemporaryDirectory() as tmp:
        for mode in MODES:
            bundle = load_fixture()
            if drop_receipt:
                bundle.pop("acquisitionReceipt", None)
            outer = manual_halt(True) if halt else manual_halt(False)
            inner = per_bundle(bundle) if per_bundle else (condition() if condition else nothing())
            with outer, inner:
                out = run_cycle(mode, bundle, auto=auto, live=live, verified=verified,
                                ledger_dir=tmp)
            check(f"{label} / selection={mode}: 注文 0 件",
                  len(out["calls"]) == 0, (len(out["calls"]), out["notes"][:2]))
            if expect_watch:
                state = (out["scenario"] or {}).get("state")
                check(f"{label} / selection={mode}: 本番の判定が scenario を WATCH へ降格",
                      state == "WATCH", state)
            if expect_note:
                text = " ".join(str(n) for n in list(out["publishNotes"]) + list(out["notes"]))
                armed_before = str(out["decision"].get("state") or "").upper() in ("ARMED", "ACTIVE")
                if armed_before or not expect_watch:
                    # 降格の対象があったモードだけ、本番の判定が理由を残したことを見る。
                    check(f"{label} / selection={mode}: 本番の判定が降格の理由を残す",
                          expect_note in text, text[:220])
                else:
                    print(f"INFO {label} / selection={mode}: primary が既に WATCH "
                          f"({out['decision'].get('hardBlockers')})なので降格の対象なし")


def test_volatility_standdown():
    _run_stop("ボラ床スタンドダウン", condition=vol_standdown,
              expect_note="vol gate stand-down")


def test_event_blackout():
    _run_stop("High イベント窓", per_bundle=event_blackout, expect_note="event blackout")


def test_contract_expiry_near():
    _run_stop("限月の満期ガード", condition=contract_expiry_near,
              expect_note="contract expiry gate")


def test_acquisition_receipt_missing():
    _run_stop("取得受領書なし", drop_receipt=True)


def test_manual_halt():
    _run_stop("手動 HALT", halt=True, expect_watch=False, expect_note="MANUAL HALT")


def test_auto_off():
    # AUTO OFF は新規 ENTRY を評価そのものから外す(注記も出ない = 何もしない)。
    _run_stop("AUTO OFF", auto="0", live="0", expect_watch=False)


def test_broker_unverified():
    _run_stop("建玉照会 UNVERIFIED", verified=False, expect_watch=False,
              expect_note="broker position is UNVERIFIED")


def main():
    ordered = ["test_input_has_a_hidden_eligible_candidate",
               "test_positive_control_sends_when_nothing_stops_it"]
    rest = sorted(k for k in globals() if k.startswith("test_") and k not in ordered)
    for name in ordered + rest:
        print(f"\n--- {name} ---")
        globals()[name]()
    print()
    check("外部到達は 0 件(socket / subprocess)", not BLOCKED, BLOCKED)
    if failures:
        print(f"FAILED {len(failures)}: " + ", ".join(failures))
        return 1
    print("ALL PASS (test_r122_global_stop_no_order)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

# -*- coding: utf-8 -*-
"""Mini App 一回押し発注(scenario_order_confirmed)の検証。

## 実注文は絶対に発生しない

  - `--confirm` 付きの order.py は **必ずスタブ**。本物は呼ばない
  - ドライランは order.py の**一時コピー**に対して実行する。実ログ
    (.secrets/order_log.json)には触れず、当日の発注回数も汚さない
  - 一時コピーの CrossTrade URL は破棄ポート(127.0.0.1:9)なので、
    万一 --confirm が漏れても外部には到達しない
  - Telegram API / Cloudflare API も全てスタブ

    python tests/test_oneclick.py
"""
import html
import copy
import json
import os
import re
import shutil
import sys
from datetime import datetime, timedelta, timezone

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

for _stream in ("stdout", "stderr"):
    _file = getattr(sys, _stream, None)
    if _file is not None and hasattr(_file, "reconfigure"):
        try:
            _file.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

import broker_status  # noqa: E402
import execution_contract  # noqa: E402
import execution_intent  # noqa: E402
import nqx_state      # noqa: E402
import route_envelope  # noqa: E402
import telegram_bot as tb  # noqa: E402
import strategy_evidence  # noqa: E402

CFG_LOCKED = {"TELEGRAM_TOKEN": "T", "TELEGRAM_CHAT_ID": "999"}
CFG_LIVE = {"TELEGRAM_TOKEN": "T", "TELEGRAM_CHAT_ID": "999", "NQX_LIVE_ORDERS": "1"}

# ---------------------------------------------------------------- 隔離環境

from _hermetic import make_sandbox, dry_run  # noqa: E402

SANDBOX = make_sandbox(BASE)

# ---------------------------------------------------------------- スタブ

tb.api = lambda *a, **k: {"ok": True, "result": {}}
tb.ORDER_KEY_FILE = os.path.join(SANDBOX, "order_keys.json")

CALLS = []
def sent_envelope():
    return route_envelope.format_envelope([
        {"accountId": "TEST", "legId": "TP1", "state": "ACCEPTED",
         "orderId": "ENTRY-TP1", "receipt": "RECEIPT-TP1"},
        {"accountId": "TEST", "legId": "RUNNER", "state": "ACCEPTED",
         "orderId": "ENTRY-RUNNER", "receipt": "RECEIPT-RUNNER"},
    ], "SENT")


SEND_RESULT = [True, sent_envelope()]
DAILY = [0]                                   # --status が返す当日回数
# 通常ケースは「ブローカーが verified=true で FLAT を返した」状態にする。
# 未確認ケースは専用テストで明示し、旧実装の fail-open を再び許さない。
BROKER = [{"verified": True, "source": "tradovate-rest", "symbol": "MNQU6",
           "qty": 0, "observedAt": datetime.now(timezone.utc).isoformat()}]
BROKER_ORDERS = [{"verified": True, "state": "CANCELED", "orders": [
    {"orderId": "REST-TP1", "status": "CANCELED"},
    {"orderId": "REST-RUNNER", "status": "REJECTED"}], "activeOrders": []}]
PUBLISHED = []


def hermetic_dry_run(args):
    """一時コピーの order.py に対してドライランを実行する(実ログに触れない)。"""
    return dry_run(SANDBOX, args)


def fake_run_order(args):
    CALLS.append(list(args))
    if "--confirm" in args:
        return SEND_RESULT[0], SEND_RESULT[1]     # 本物は絶対に呼ばない
    if "--status" in args:
        return True, f"取引日 2026-08-13: 発注 {DAILY[0]}/3 回"
    return hermetic_dry_run(args)


tb.run_order = fake_run_order
broker_status.query_position = lambda symbol=None: dict(BROKER[0])
broker_status.query_orders = lambda symbol=None, known_order_ids=None: copy.deepcopy(BROKER_ORDERS[0])
CURRENT_SERVER = [None]
def _default_server_state(cfg=None):
    return CURRENT_SERVER[0]
DEFAULT_SERVER_FETCH = _default_server_state
nqx_state.fetch_state_quiet = DEFAULT_SERVER_FETCH
DEFAULT_PUBLISH_ORDER = lambda order, cfg=None: (PUBLISHED.append(order) or (True, {}))
nqx_state.publish_order = DEFAULT_PUBLISH_ORDER
def claim_stub(scenario, **kwargs):
    order_type = str(kwargs.get("order_type") or "LIMIT").upper()
    intent = execution_intent.from_scenario(
        scenario, order_type=order_type,
        account_scope=scenario["executionContract"]["accountScope"])
    return True, {
        "entryKey": tb._server_order_key(scenario), "claimToken": "T" * 43,
        "executionIntent": intent, "executionIntentHash": execution_intent.intent_hash(intent),
    }


nqx_state.claim_entry = claim_stub
nqx_state.sync_position = lambda cfg=None, symbol=None: (True, {})

# ---------------------------------------------------------------- ヘルパ

PASS = [0]
FAIL = [0]


def check(label, condition, detail=""):
    if condition:
        PASS[0] += 1
        print(f"  OK   {label}")
    else:
        FAIL[0] += 1
        print(f"  NG   {label}  {detail}")


def reset():
    CALLS.clear()
    PUBLISHED.clear()
    SEND_RESULT[0], SEND_RESULT[1] = True, sent_envelope()
    DAILY[0] = 0
    BROKER[0] = {"verified": True, "source": "tradovate-rest", "symbol": "MNQU6",
                 "qty": 0, "observedAt": datetime.now(timezone.utc).isoformat()}
    BROKER_ORDERS[0] = {"verified": True, "state": "CANCELED", "orders": [
        {"orderId": "REST-TP1", "status": "CANCELED"},
        {"orderId": "REST-RUNNER", "status": "REJECTED"}], "activeOrders": []}
    CURRENT_SERVER[0] = None
    nqx_state.fetch_state_quiet = DEFAULT_SERVER_FETCH
    nqx_state.publish_order = DEFAULT_PUBLISH_ORDER
    for path in (tb.ORDER_KEY_FILE, tb._server_lock_path()):
        try:
            os.unlink(path)
        except OSError:
            pass


_nonce_seq = [0]


def nonce():
    _nonce_seq[0] += 1
    return f"nonce-oneclick-test-{_nonce_seq[0]:04d}"


def payload(**overrides):
    now = datetime.now(timezone.utc)
    scenario = {
        "scenarioId": "sc-test-1",
        "fingerprint": "fp-test-1",
        "marketCycleId": "cycle-oneclick-test",
        "symbol": "MNQU6",
        "side": "BUY",
        "qty": 2,
        "entry": 29760.00,
        "stop": 29726.00,
        "target": 29796.00,
        "issuedAt": now.isoformat(),
        "expiresAt": (now + timedelta(minutes=15)).isoformat(),
    }
    scenario.update(overrides.pop("scenario", {}))
    evidence = strategy_evidence.canonicalize({
        "version": "R14-STRATEGY-EVIDENCE-1", "asOf": scenario.get("issuedAt") or now.isoformat(),
        "sessionId": "NY-TEST", "source": "fixture", "provenance": "test",
        "models": {"ifvg": {"valid": False}},
    })
    scenario.setdefault("evidenceHash", evidence["evidenceHash"])
    # The Mini App now echoes the complete currently displayed server scenario.
    # Keep fixture packets structurally authoritative while individual tests can
    # still alter a field to exercise downstream validation.
    if "targets" not in scenario:
        if int(scenario.get("qty", 1)) == 2:
            try:
                distance = abs(float(scenario["target"]) - float(scenario["entry"])) or 1
                runner = (float(scenario["target"]) + distance if scenario["side"] == "BUY"
                          else float(scenario["target"]) - distance)
            except (TypeError, ValueError):
                runner = 29832.0
            scenario["targets"] = [scenario["target"], runner]
        else:
            scenario["targets"] = [scenario["target"]]
    if int(scenario.get("qty", 1)) == 2 and isinstance(scenario.get("targets"), list) and scenario["targets"]:
        scenario.setdefault("legs", [{"id": "TP1", "qty": 1, "target": scenario["targets"][0]},
                                     {"id": "RUNNER", "qty": 1, "target": scenario["targets"][-1]}])
        scenario.setdefault("planVersion", "R12-ICT-SPLIT-1")
    scenario.setdefault("state", "ARMED")
    scenario.setdefault("grade", "A")
    scenario.setdefault("orderable", True)
    scenario.setdefault("executionContractVersion", execution_contract.VERSION)
    scenario.setdefault("executionContract", {"accountScope": ["TEST"]})
    body = {"type": "scenario_order_confirmed", "scenario": scenario,
            "clientNonce": overrides.pop("clientNonce", nonce()),
            "source": "nqx-nightwatch-mini-app"}
    body.update(overrides)
    return json.dumps(body)


def send(raw, cfg=CFG_LIVE):
    parsed = json.loads(raw)
    if (nqx_state.fetch_state_quiet is DEFAULT_SERVER_FETCH and
            parsed.get("type") == "scenario_order_confirmed" and isinstance(parsed.get("scenario"), dict)):
        CURRENT_SERVER[0] = authoritative_view(parsed["scenario"])
    text, keyboard, note = tb.handle_web_app_data(cfg, raw)
    # タグを外し、HTML エンティティも戻してから内容を検査する。
    return html.unescape(re.sub(r"<[^>]+>", "", str(text))), note


def authoritative_view(scenario, *, orderable=True, grade=None):
    """Make a current server view for authoritative-gate tests.

    Execution R14 requires an observed market timestamp even in hermetic bot
    tests.  The helper keeps manual and AUTO fixtures on the same state shape.
    """
    observed_at = datetime.now(timezone.utc).isoformat()
    active_grade = grade if grade is not None else scenario.get("grade")
    evidence = strategy_evidence.canonicalize({
        "version": "R14-STRATEGY-EVIDENCE-1", "asOf": scenario.get("issuedAt") or observed_at,
        "sessionId": "NY-TEST", "source": "fixture", "provenance": "test",
        "models": {"ifvg": {"valid": False}},
    })
    scenario = dict(scenario)
    scenario.setdefault("marketCycleId", "cycle-oneclick-test")
    scenario.setdefault("evidenceHash", evidence["evidenceHash"])
    # Preserve a supplied wrong hash so the shared seal rejects the packet.
    if scenario["evidenceHash"] != evidence["evidenceHash"]:
        evidence = {**evidence, "evidenceHash": scenario["evidenceHash"]}
    return {
        "scenario": {**scenario, "cycleCommitted": True},
        "display": {"orderable": orderable, "cyclePaired": orderable, "blockReason": None},
        "market": {
            "at": observed_at,
            "observedAt": observed_at,
            "cvdAt": observed_at,
            "cycleId": scenario["marketCycleId"], "cycleCommitted": True,
            "strategyEvidence": evidence,
            "evaluation": {"decision": {"grade": active_grade}},
        },
        "order": {"state": "NONE"},
    }


def confirms():
    return [c for c in CALLS if "--confirm" in c]


# ================================================================ テスト

print("=" * 68)
print("1. 受動的な経路では発注されない")
print("=" * 68)

reset()
text, note = send(json.dumps({"type": "scenario_preview", "scenario": {
    "side": "BUY", "entry": 29760, "stop": 29726, "target": 29796, "qty": 1}}))
check("シナリオ受信(scenario_preview)だけでは --confirm を呼ばない", len(confirms()) == 0, str(CALLS))

reset()
text, note = send(json.dumps({"type": "market_snapshot"}))
check("未知の payload は拒否され、発注経路に入らない",
      len(confirms()) == 0 and "rejected" in note.lower(), note)

reset()
with open(os.path.join(BASE, "monitor_publish.py"), encoding="utf-8") as fh:
    monitor_source = fh.read()
# 「order.py」という文字列の有無ではなく「実行能力」を検査する。
# monitor_publish は発注回数の表示のために order.py の読み取り専用ヘルパーを
# import してよい(2026-08-14 の設計変更)が、--confirm・子プロセス実行・
# 読み取り専用以外の import はあってはならない。
_readonly_allowed = {"MAX_ORDERS_PER_DAY", "count_today", "load_log", "trade_date"}
_order_imports = {
    name.strip()
    for line in re.findall(r"^\s*from order import ([^\n]+)", monitor_source, re.M)
    for name in line.split(",")
}
check("monitor_publish.py に発注能力が無い(--confirm/子プロセス/書込系importなし)",
      "--confirm" not in monitor_source
      and "subprocess" not in monitor_source
      and "os.system" not in monitor_source
      and re.search(r"^\s*import order\b", monitor_source, re.M) is None
      and _order_imports <= _readonly_allowed,
      f"order imports = {sorted(_order_imports)}")

cf_dir = os.path.join(BASE, "cloudflare", "src")
cf_source = ""
for name in sorted(os.listdir(cf_dir)):
    if name.endswith(".js"):
        with open(os.path.join(cf_dir, name), encoding="utf-8") as fh:
            cf_source += fh.read()
# コメント中の注意書きは許容し、実際に外部へ発注しうるコードだけを見る。
cf_code = "\n".join(line for line in cf_source.splitlines()
                    if not line.strip().startswith(("*", "//", "/*")))
check("Cloudflare 側に --confirm を呼ぶコードが無い", "--confirm" not in cf_code)
check("Cloudflare 側にブローカーへの発信先が無い",
      not re.search(r"https?://[^\s\"']*(crosstrade|tradovate)", cf_code, re.I),
      "broker endpoint found")

print()
print("=" * 68)
print("2. 期限とシナリオ同一性")
print("=" * 68)

reset()
past = datetime.now(timezone.utc) - timedelta(minutes=5)
text, note = send(payload(scenario={"issuedAt": (past - timedelta(minutes=30)).isoformat(),
                                    "expiresAt": past.isoformat()}))
check("期限切れ payload は契約 blocker で拒否される", "ORDER BLOCKED" in text and "SCENARIO_EXPIRED" in text, text[:120])
check("期限切れでは order.py を一切呼ばない", len(CALLS) == 0, str(CALLS))

reset()
old = datetime.now(timezone.utc) - timedelta(minutes=40)
text, note = send(payload(scenario={"issuedAt": old.isoformat(),
                                    "expiresAt": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()}))
check("古すぎるシナリオは契約 blocker で拒否される", "SCENARIO_STALE" in text, text[:120])

reset()
text, note = send(payload(scenario={"fingerprint": ""}))
check("fingerprint 欠落は拒否される", "fingerprint" in text, text[:120])

reset()
text, note = send(payload(scenario={"symbol": "MESU6"}))
check("symbol 不一致は拒否される", "ORDER BLOCKED" in text and "MESU6" in text, text[:120])

print()
print("=" * 68)
print("3. 価格・方向・数量・リスク")
print("=" * 68)

reset()
text, note = send(payload(scenario={"stop": 29800.0}))       # BUY なのに SL が上
check("逆向き SL は拒否される", "SL < Entry < TP" in text, text[:160])

reset()
text, note = send(payload(scenario={"side": "SELL", "entry": 29760, "stop": 29726, "target": 29796}))
check("SELL の逆向き構成は拒否される",
      "TP < Entry < SL" in text or "SPLIT_TARGETS_INVALID" in text, text[:160])

reset()
text, note = send(payload(scenario={"entry": "not-a-number"}))
check("数値でない価格は契約 blocker で拒否される", "RISK_GEOMETRY_INVALID" in text, text[:120])

reset()
text, note = send(payload(scenario={"qty": 5}))
check("過大 qty は契約 blocker で拒否される", "QTY_EXCEEDS_CONTRACT" in text, text[:120])

reset()
text, note = send(payload(scenario={"qty": 2, "stop": 29660.0}))   # 100pt × 2枚 = $400
check("過大リスクは契約 blocker で拒否される", "RISK_CAP_EXCEEDED" in text, text[:160])
check("リスク超過では order.py を呼ばない", len(CALLS) == 0, str(CALLS))

reset()
text, note = send(payload(clientNonce="short"))
check("clientNonce が短すぎる場合は拒否される", "clientNonce" in text, text[:120])

reset()
text, note = send(payload(scenario={"entry": 29760.13, "stop": 29726.06, "target": 29796.19}))
sent = confirms()
check("off-tick split plan is rejected before broker routing",
      not sent and "分割TP" in text, text[:160])

print()
print("=" * 68)
print("4. 建玉と日次回数")
print("=" * 68)

reset()
BROKER[0] = {"verified": False, "source": "unavailable", "symbol": "MNQU6",
             "observedAt": datetime.now(timezone.utc).isoformat(), "detail": "no adapter"}
text, note = send(payload())
check("建玉照会が未確認ならライブ発注を止める", "POSITION UNVERIFIED" in text, text[:180])
check("未確認では --confirm を呼ばない", len(confirms()) == 0, str(CALLS))

reset()
BROKER[0] = {"verified": True, "source": "tradovate-rest", "symbol": "MNQU6",
             "side": "LONG", "qty": 2, "avgEntry": 29750.0,
             "observedAt": datetime.now(timezone.utc).isoformat()}
text, note = send(payload())
check("建玉があるときは新規発注しない", "POSITION OPEN" in text, text[:160])
check("建玉があるときは order.py を呼ばない", len(CALLS) == 0, str(CALLS))

reset()
BROKER[0] = {"verified": True, "source": "tradovate-rest", "symbol": "MNQU6",
             "qty": 0, "observedAt": datetime.now(timezone.utc).isoformat()}
text, note = send(payload())
check("FLAT が確認できていれば発注できる", "ORDER SENT" in text, text[:160])

reset()
DAILY[0] = 3
text, note = send(payload())
check("日次上限に達していたら発注しない", "発注上限" in text, text[:160])
check("日次上限では --confirm を呼ばない", len(confirms()) == 0, str(CALLS))

print()
print("=" * 68)
print("5. 二重送信の防止")
print("=" * 68)

reset()
key = nonce()
first_text, _ = send(payload(clientNonce=key))
first_confirms = len(confirms())
second_text, _ = send(payload(clientNonce=key))
total_confirms = len(confirms())
check("1 回目は送信される", first_confirms == 1, str(CALLS))
check("同じ nonce の 2 回目は拒否される", "既に処理済み" in second_text, second_text[:120])
check("二重タップでも --confirm は 1 回だけ", total_confirms == 1, f"{total_confirms} 回")

reset()
key = nonce()
send(payload(clientNonce=key))
# Bot 再起動を模擬(モジュール状態を捨てても鍵はファイルに残る)
tb._load_order_keys.__globals__["ORDER_KEY_FILE"] = tb.ORDER_KEY_FILE
text, _ = send(payload(clientNonce=key))
check("Bot 再起動後(ファイル永続)でも同じ nonce は拒否される", "既に処理済み" in text, text[:120])

print()
print("=" * 68)
print("6. ドライラン → ライブゲート → 送信の順序")
print("=" * 68)

reset()
text, note = send(payload(), cfg=CFG_LOCKED)
check("NQX_LIVE_ORDERS が無ければ送信しない", "LIVE ROUTE LOCKED" in text, text[:160])
check("ロック時は --confirm を呼ばない", len(confirms()) == 0, str(CALLS))
check("ロック時でもドライランは実行される", any("--confirm" not in c and "--side" in c for c in CALLS), str(CALLS))

reset()
# order.py 本体が拒否する構成(SL 未指定相当を作るため直接 argv を壊す)
original = tb.run_order
def rejecting_run_order(args):
    CALLS.append(list(args))
    if "--confirm" in args:
        return SEND_RESULT[0], SEND_RESULT[1]
    if "--status" in args:
        return True, "取引日 2026-08-13: 発注 0/3 回"
    return False, "ERROR: order.py がこの注文を拒否しました"
tb.run_order = rejecting_run_order
text, note = send(payload())
check("ドライラン失敗時は --confirm を呼ばない", len(confirms()) == 0, str(CALLS))
check("ドライラン失敗は ORDER BLOCKED として返る", "ORDER BLOCKED" in text, text[:160])
tb.run_order = original

print()
print("=" * 68)
print("7. 送信結果の表示(ドライラン成功を約定にしない)")
print("=" * 68)

reset()
text, note = send(payload())
check("成功時だけ ORDER SENT", "ORDER SENT" in text, text[:160])
check("final route receipt が表示される", "NQX_ROUTE_FINAL" in text, text[:260])
check("約定は別物であることを明示している", "約定はブローカー照会で確認" in text, text[:400])

reset()
SEND_RESULT[0], SEND_RESULT[1] = False, "HTTP ERROR 500\nupstream refused"
text, note = send(payload())
check("下流が失敗したら ORDER SENT と表示しない", "ORDER SENT" not in text, text[:160])
check("構造化finalが無い下流失敗は UNKNOWN として返る",
      "UNKNOWN — VERIFY BROKER" in text, text[:160])

reset()
SEND_RESULT[0], SEND_RESULT[1] = False, "order.py がタイムアウトしました(30秒)"
text, note = send(payload())
check("判定不能なら UNKNOWN — VERIFY BROKER", "UNKNOWN — VERIFY BROKER" in text, text[:160])
check("判定不能では成功と表示しない", "ORDER SENT" not in text, text[:160])

print()
print("=" * 68)
print("8. state への通知")
print("=" * 68)

reset()
send(payload())
states = [entry["state"] for entry in PUBLISHED]
check("PENDING → SENT の順で state が通知される", states == ["PENDING", "SENT"], str(states))
check("注文通知に建玉は含まれない", all("position" not in entry for entry in PUBLISHED), str(PUBLISHED))
published_key = PUBLISHED[0].get("idempotencyKey") if PUBLISHED else None
check("durable order key is the server-authoritative tuple, not client nonce",
      isinstance(published_key, str) and published_key.startswith("ENTRY:")
      and published_key != "nonce-oneclick-test", str(PUBLISHED))

# PENDING must be a confirmed durable commit before the sole --confirm call.
for mode in ("MANUAL_SLIDE", "AUTO_ONE_PASS"):
    reset()
    raw = payload(executionMode=mode,
                  **({"onePass": {"status": "PASS", "grade": "A"}}
                     if mode == "AUTO_ONE_PASS" else {}))
    nqx_state.publish_order = lambda order, cfg=None: (PUBLISHED.append(order) or (False, {"error": "stub"}))
    text, note = send(raw)
    locks = tb._load_server_order_locks()
    check(f"{mode} durable PENDING false keeps confirm at zero",
          len(confirms()) == 0 and "PENDING" in text and any(
              item.get("state") == "UNKNOWN" for item in locks.values()),
          f"calls={CALLS} locks={locks} text={text[:160]}")

reset()
raw = payload()
def raising_publish(_order, cfg=None):
    raise RuntimeError("publish unavailable stub")
nqx_state.publish_order = raising_publish
text, note = send(raw)
check("durable PENDING exception keeps confirm at zero and local UNKNOWN",
      len(confirms()) == 0 and any(
          item.get("state") == "UNKNOWN" for item in tb._load_server_order_locks().values()),
      f"calls={CALLS} locks={tb._load_server_order_locks()}")

# If SENT cannot be saved after the single broker call, retain UNKNOWN under
# the same server tuple. Changing nonce or MANUAL/AUTO mode cannot resend.
reset()
raw_obj = json.loads(payload())
publish_attempts = [0]
def sent_save_fails(order, cfg=None):
    publish_attempts[0] += 1
    PUBLISHED.append(order)
    return (publish_attempts[0] == 1), {}
nqx_state.publish_order = sent_save_fails
text, note = send(json.dumps(raw_obj))
server_key = tb._server_order_key(raw_obj["scenario"])
raw_obj["clientNonce"] = nonce()
raw_obj["executionMode"] = "AUTO_ONE_PASS"
raw_obj["onePass"] = {"status": "PASS", "grade": "A"}
text2, note2 = send(json.dumps(raw_obj))
check("SENT publish failure fixes local UNKNOWN and blocks different nonce/AUTO retry",
      len(confirms()) == 1 and tb._load_server_order_locks().get(server_key, {}).get("state") == "UNKNOWN"
      and "local PENDING/SENT/PARTIAL/UNKNOWN lock" in text2,
      f"confirms={confirms()} locks={tb._load_server_order_locks()} text={text2[:180]}")

# A matching terminal broker/server observation is the only automatic unlock.
terminal_view = authoritative_view(raw_obj["scenario"])
terminal_view["order"] = {"state": "REJECTED", "idempotencyKey": server_key,
                          "scenarioId": raw_obj["scenario"]["scenarioId"]}
CURRENT_SERVER[0] = terminal_view
nqx_state.fetch_state_quiet = lambda cfg=None: terminal_view
nqx_state.publish_order = DEFAULT_PUBLISH_ORDER
raw_obj["clientNonce"] = nonce()
text3, note3 = send(json.dumps(raw_obj))
check("matching broker terminal observation releases local tuple lock",
      len(confirms()) == 2 and "ORDER SENT" in text3,
      f"confirms={confirms()} locks={tb._load_server_order_locks()} text={text3[:180]}")

print()
print("=" * 68)
print("9. 既存経路の互換")
print("=" * 68)

reset()
text, note = send(json.dumps({"type": "scenario_order_request", "scenario": {
    "side": "BUY", "entry": 29760, "stop": 29726, "target": 29796, "qty": 1}}))
check("従来の scenario_order_request は二段階経路のまま", len(confirms()) == 0, str(CALLS))

reset()
body, keyboard = tb.handle(CFG_LIVE, "/status")
check("/status は従来どおり動く", "STATUS" in str(body), str(body)[:80])
body, keyboard = tb.handle(CFG_LIVE, "/help")
check("/help は従来どおり動く", "COMMAND DECK" in str(body), str(body)[:80])
# All execution modes use the same current-server gate before dry-run or
# confirm.  These cases intentionally never reach order.py.
reset()
body = json.loads(payload())
server = dict(body["scenario"])
nqx_state.fetch_state_quiet = lambda cfg=None: {"scenario": server, "display": {"orderable": True}, "market": {}}
body["scenario"]["entry"] = 29760.25
text, note = send(json.dumps(body))
check("manual tampered price is blocked by authoritative gate", "ORDER BLOCKED" in text and not CALLS, text[:160])

reset()
body = json.loads(payload())
sealed = authoritative_view(body["scenario"])
original_hash = sealed["market"]["strategyEvidence"]["evidenceHash"]
flipped_hash = f"{original_hash[:-1]}{'1' if original_hash.endswith('0') else '0'}"
sealed["market"] = {**sealed["market"], "strategyEvidence": {
    **sealed["market"]["strategyEvidence"],
    "evidenceHash": flipped_hash,
}}
nqx_state.fetch_state_quiet = lambda cfg=None: sealed
text, note = send(json.dumps(body))
check("manual one-bit evidence tamper is CYCLE_MISMATCH with runner 0",
      "CYCLE_MISMATCH" in text and not CALLS, text[:160])

reset()
auto_seal = json.loads(payload(executionMode="AUTO_ONE_PASS", onePass={"status": "PASS", "grade": "A"}))
partial = authoritative_view(auto_seal["scenario"])
partial["scenario"] = {**partial["scenario"], "cycleCommitted": False}
nqx_state.fetch_state_quiet = lambda cfg=None: partial
text, note = send(json.dumps(auto_seal))
check("AUTO_ONE_PASS partial cycle is blocked before runner",
      "CYCLE_MISMATCH" in text and not CALLS, text[:160])

reset()
body = json.loads(payload(scenario={"state": "WATCH"}))
nqx_state.fetch_state_quiet = lambda cfg=None: authoritative_view(body["scenario"])
text, note = send(json.dumps(body))
check("manual WATCH server scenario is blocked", "ORDER BLOCKED" in text and not CALLS, text[:160])

# 2026-09-04 ユーザー決定: B も発注可能。等級ゲートは契約外の等級(C)で確認する。
reset()
body = json.loads(payload(scenario={"grade": "B"}))
nqx_state.fetch_state_quiet = lambda cfg=None: authoritative_view(body["scenario"])
text, note = send(json.dumps(body))
check("manual B-grade server scenario is sent (2026-09-04)", "ORDER SENT" in text and bool(CALLS), text[:160])

reset()
body = json.loads(payload(scenario={"grade": "C"}))
nqx_state.fetch_state_quiet = lambda cfg=None: authoritative_view(body["scenario"])
text, note = send(json.dumps(body))
check("manual unknown-grade (C) server scenario is blocked", "ORDER BLOCKED" in text and not CALLS, text[:160])

for pending_state in ("PENDING", "SENT", "UNKNOWN"):
    reset()
    body = json.loads(payload())
    pending_view = authoritative_view(body["scenario"])
    pending_view["order"] = {"state": pending_state}
    nqx_state.fetch_state_quiet = lambda cfg=None, view=pending_view: view
    text, note = send(json.dumps(body))
    check(f"manual {pending_state} server order is blocked", "ORDER BLOCKED" in text and not CALLS, text[:160])

reset()
body = json.loads(payload())
nqx_state.fetch_state_quiet = lambda cfg=None: None
text, note = send(json.dumps(body))
check("manual state lookup failure is blocked", "ORDER BLOCKED" in text and not CALLS, text[:160])

reset()
split = json.loads(payload(scenario={"qty": 2, "targets": [29796.0, 29832.0],
                                     "legs": [{"id": "TP1", "qty": 1, "target": 29796.0},
                                              {"id": "RUNNER", "qty": 1, "target": 29832.0}],
                                     "planVersion": "R12-ICT-SPLIT-1"}))
nqx_state.fetch_state_quiet = lambda cfg=None: authoritative_view(split["scenario"])
text, note = send(json.dumps(split))
split_argv = next((call for call in CALLS if "--split-tp" in call), [])
check("Mini App -> Bot uses server split TP argv", split_argv and split_argv[split_argv.index("--split-tp") + 1] == "29796.00,29832.00", str(CALLS))
check("split TP never falls back to client --tp", split_argv and "--tp" not in split_argv, str(split_argv))

for label, targets in (("missing", None), ("same", [29796.0, 29796.0]),
                       ("reversed", [29832.0, 29796.0]), ("wrong-direction", [29796.0, 29700.0])):
    reset()
    invalid = json.loads(payload(scenario={"qty": 2, "targets": targets,
                                           "legs": ([] if targets is None else [{"id": "TP1", "qty": 1, "target": targets[0]},
                                                                                   {"id": "RUNNER", "qty": 1, "target": targets[-1]}]),
                                           "planVersion": "R12-ICT-SPLIT-1"}))
    nqx_state.fetch_state_quiet = lambda cfg=None, invalid=invalid: authoritative_view(invalid["scenario"])
    text, note = send(json.dumps(invalid))
    check(f"qty=2 {label} split plan is blocked", "ORDER BLOCKED" in text and not CALLS, text[:160])

body, keyboard = tb.handle(CFG_LIVE, "/cancel")
check("/cancel は従来どおり動く", body is not None)


print()
print("=" * 68)
print("10. AUTO_ONE_PASS のサーバー再検証")
print("=" * 68)

reset()
auto_body = json.loads(payload(executionMode="AUTO_ONE_PASS",
                                onePass={"status": "PASS", "grade": "A"}))
authoritative = dict(auto_body["scenario"], state="ARMED", grade="A")
auto_fetches = [0]
def fetch_auto_once(cfg=None):
    auto_fetches[0] += 1
    return authoritative_view(authoritative, grade="A")
nqx_state.fetch_state_quiet = fetch_auto_once
text, note = send(json.dumps(auto_body))
check("AUTO_ONE_PASS は最新状態が A かつ発注可なら通過する",
      "ORDER SENT" in text and len(confirms()) == 1 and auto_fetches[0] == 1, text[:180])

reset()
auto_body = json.loads(payload(executionMode="AUTO_ONE_PASS",
                                onePass={"status": "PASS", "grade": "A"}))
authoritative = dict(auto_body["scenario"], state="ARMED", grade="B")
nqx_state.fetch_state_quiet = lambda cfg=None: authoritative_view(authoritative, grade="B")
text, note = send(json.dumps(auto_body))
check("AUTO_ONE_PASS は A 未満をサーバー側で拒否する",
      "ORDER BLOCKED" in text and len(CALLS) == 0, text[:180])

reset()
auto_body = json.loads(payload(executionMode="AUTO_ONE_PASS",
                                onePass={"status": "PASS", "grade": "A"}))
nqx_state.fetch_state_quiet = lambda cfg=None: None
text, note = send(json.dumps(auto_body))
check("AUTO_ONE_PASS は最新状態を取得できなければ拒否する",
      "ORDER BLOCKED" in text and len(CALLS) == 0, text[:180])
nqx_state.fetch_state_quiet = lambda cfg=None: None


print()
print("=" * 68)
print("11. 決済結果(/result)")
print("=" * 68)

PUBLISHED_RESULTS = []
nqx_state.publish_result = lambda result, cfg=None: (PUBLISHED_RESULTS.append(result) or (True, {}))

BAR0 = 1786550000
CLOSED_VIEW = {
    "closedPosition": {
        "side": "LONG", "symbol": "MNQU6", "avgEntry": 29760.0, "stop": 29726.0,
        "target": 29796.0, "closedQty": 2, "initialQty": 2,
        "filledAt": datetime.fromtimestamp(BAR0 + 3 * 180, timezone.utc).isoformat(),
        "closedAt": datetime.fromtimestamp(BAR0 + 9 * 180, timezone.utc).isoformat(),
        "receipt": "HTTP 200",
    },
    "market": {"bars": [{"t": BAR0 + i * 180, "c": 29752 + i} for i in range(20)]},
}

def with_state(view):
    nqx_state.fetch_state_quiet = lambda cfg=None: view

reset(); PUBLISHED_RESULTS.clear()
with_state(None)
body, _ = tb.handle(CFG_LIVE, "/result")
check("state を取れないときは結果を作らない", "NOT PUBLISHED" in str(body), str(body)[:120])
check("結果を publish しない", len(PUBLISHED_RESULTS) == 0)

reset(); PUBLISHED_RESULTS.clear()
with_state({"closedPosition": None, "market": {}})
body, _ = tb.handle(CFG_LIVE, "/result")
check("決済済み建玉が無ければ作らない", "NOT PUBLISHED" in str(body), str(body)[:120])

reset(); PUBLISHED_RESULTS.clear()
with_state(CLOSED_VIEW)
body, _ = tb.handle(CFG_LIVE, "/result")
check("決済価格が無ければ publish せず指示を返す",
      "NOT PUBLISHED" in str(body) and "/result 29796.25" in html.unescape(str(body)), str(body)[:200])
check("推定した決済価格で結果を作らない", len(PUBLISHED_RESULTS) == 0)

reset(); PUBLISHED_RESULTS.clear()
with_state(CLOSED_VIEW)
body, _ = tb.handle(CFG_LIVE, "/result 29796.13")
published = PUBLISHED_RESULTS[0] if PUBLISHED_RESULTS else {}
check("明示された決済価格なら publish する", "RESULT PUBLISHED" in str(body), str(body)[:160])
check("exitSource は manual になる", published.get("exitSource") == "manual", str(published.get("exitSource")))
check("決済価格も 0.25 tick に正規化される", published.get("exit") == 29796.25, str(published.get("exit")))
check("path は実バー由来", published.get("pathSource") == "observed-bars", str(published.get("pathSource")))
check("導出値を payload に載せない",
      not any(k in published for k in ("state", "pnl", "realisedR", "held", "usd")), str(list(published.keys())))
check("mode は NQX_LIVE_ORDERS を反映する", published.get("mode") == "LIVE", str(published.get("mode")))

reset(); PUBLISHED_RESULTS.clear()
with_state(CLOSED_VIEW)
body, _ = tb.handle(CFG_LOCKED, "/result 29796.25")
check("ライブ未解除なら SIMULATION として記録する",
      PUBLISHED_RESULTS and PUBLISHED_RESULTS[0].get("mode") == "SIMULATION",
      str(PUBLISHED_RESULTS[0].get("mode") if PUBLISHED_RESULTS else None))

reset(); PUBLISHED_RESULTS.clear()
with_state(dict(CLOSED_VIEW, market={"bars": []}))
body, _ = tb.handle(CFG_LIVE, "/result 29796.25")
published = PUBLISHED_RESULTS[0] if PUBLISHED_RESULTS else {}
check("観測が無ければ両端のみとして記録する", published.get("pathSource") == "endpoints-only",
      str(published.get("pathSource")))
check("値動きを作らない", published.get("path") == [29760.0, 29796.25], str(published.get("path")))

reset(); PUBLISHED_RESULTS.clear()
with_state(CLOSED_VIEW)
body, _ = tb.handle(CFG_LIVE, "/result abc")
check("数値でない決済価格は拒否される", "使い方" in str(body), str(body)[:120])

print()
print("=" * 68)
print("12. 決済結果の1タップ記録(result: ボタン)")
print("=" * 68)

reset(); PUBLISHED_RESULTS.clear()
with_state(CLOSED_VIEW)
body, _, note_cb = tb.handle_callback(CFG_LIVE, "result:29796.25")
published = PUBLISHED_RESULTS[0] if PUBLISHED_RESULTS else {}
check("記録ボタンで publish される", "RESULT PUBLISHED" in str(body), str(body)[:160])
check("ボタン記録も exitSource=manual", published.get("exitSource") == "manual",
      str(published.get("exitSource")))
check("ボタンの価格が 0.25 tick で記録される", published.get("exit") == 29796.25,
      str(published.get("exit")))

reset(); PUBLISHED_RESULTS.clear()
with_state(CLOSED_VIEW)
body, _, note_cb = tb.handle_callback(CFG_LIVE, "result:abc")
check("壊れた記録ボタンは拒否される", "無効" in str(body), str(body)[:120])
check("拒否時は publish しない", len(PUBLISHED_RESULTS) == 0)

# 候補価格は検証済み・非STALEの現在値だけ
with_state({"market": {"verified": True, "stale": False, "price": 30092.13}})
check("候補価格は tick 正規化される", tb._suggest_exit_price() == 30092.25,
      str(tb._suggest_exit_price()))
with_state({"market": {"verified": True, "stale": True, "price": 30092.25}})
check("STALE な価格は候補にしない", tb._suggest_exit_price() is None)
with_state(None)
check("state が無ければ候補を出さない", tb._suggest_exit_price() is None)
check("拒否時は publish しない", len(PUBLISHED_RESULTS) == 0)

check("/result は order.py を呼ばない", len(CALLS) == 0, str(CALLS))
nqx_state.fetch_state_quiet = lambda cfg=None: None

print()
print("=" * 68)
print("13. position 遷移が order ストリームを終端化する(HANDOFF §11 ②)")
print("=" * 68)

# 送信後 SENT のまま残った注文は、決済して position が消えると ORDER_PENDING
# として再浮上し発注を永久ブロックする。_sync_position_quiet が position の
# 遷移を検知したときに終端化することを検証する。
SENT_ORDER_VIEW = {"order": {
    "idempotencyKey": "nonce-oneclick-test-sent-0001", "scenarioId": "sc-test-1",
    "state": "SENT", "side": "BUY", "qty": 1, "entry": 29760.0,
    "stop": 29726.0, "target": 29796.0, "receipt": "HTTP 200",
    "detail": "SENT", "at": datetime.now(timezone.utc).isoformat()}}


def with_transitions(transitions):
    nqx_state.sync_position = lambda cfg=None, symbol=None: (
        True, {"transitions": transitions, "realised": {}})


reset()
with_state(SENT_ORDER_VIEW)
with_transitions([{"kind": "position", "from": None, "to": "OPEN"}])
closed, note = tb._sync_position_quiet(CFG_LIVE)
check("建玉 OPEN 検知で SENT を FILLED にする",
      PUBLISHED and PUBLISHED[-1].get("state") == "FILLED", str(PUBLISHED))
check("idempotencyKey は元の注文を引き継ぐ",
      PUBLISHED and PUBLISHED[-1].get("idempotencyKey") == "nonce-oneclick-test-sent-0001",
      str(PUBLISHED))
check("OPEN 検知は決済としては報告しない", closed is False)

reset()
with_state(SENT_ORDER_VIEW)
with_transitions([{"kind": "position", "from": "OPEN", "to": "CLOSED"}])
closed, note = tb._sync_position_quiet(CFG_LIVE)
check("決済検知で残存 SENT を CANCELED にする",
      PUBLISHED and PUBLISHED[-1].get("state") == "CANCELED", str(PUBLISHED))
check("決済は検知として報告される", closed is True)

reset()
with_state({"order": dict(SENT_ORDER_VIEW["order"], state="FILLED")})
with_transitions([{"kind": "position", "from": "OPEN", "to": "CLOSED"}])
tb._sync_position_quiet(CFG_LIVE)
check("既に終端の注文には何も publish しない", len(PUBLISHED) == 0, str(PUBLISHED))

reset()
with_state(SENT_ORDER_VIEW)
with_transitions([])
tb._sync_position_quiet(CFG_LIVE)
check("position の遷移が無ければ order を触らない", len(PUBLISHED) == 0, str(PUBLISHED))

nqx_state.sync_position = lambda cfg=None, symbol=None: (True, {})
nqx_state.fetch_state_quiet = lambda cfg=None: None

print()
print("=" * 68)
print("14. 観測漏れ注文の手動解除(stuck_order_cancel_confirmed)")
print("=" * 68)

# Mini App の解除ボタン。発注はせず、ブローカー verified FLAT を再確認
# できたときだけ order=CANCELED を publish する(HANDOFF §11 ②)。
OLD_AT = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
FRESH_AT = datetime.now(timezone.utc).isoformat()
STUCK_VIEW = {"order": {"idempotencyKey": "stuck-key-0001", "state": "SENT",
                        "side": "BUY", "qty": 1, "entry": 29760.0, "stop": 29726.0,
                        "target": 29796.0, "at": OLD_AT},
              "entryClaim": {"entryKey": "stuck-key-0001", "routeSnapshot": [
                  {"accountId": "ACC-TEST", "legId": "TP1", "state": "ACCEPTED",
                   "orderId": "REST-TP1"},
                  {"accountId": "ACC-TEST", "legId": "RUNNER", "state": "ACCEPTED",
                   "orderId": "REST-RUNNER"}]}}


def unstick_payload(**overrides):
    body = {"type": "stuck_order_cancel_confirmed", "orderKey": "stuck-key-0001",
            "clientNonce": overrides.pop("clientNonce", nonce()),
            "source": "nqx-nightwatch-mini-app"}
    body.update(overrides)
    return json.dumps(body)


reset()
with_state(STUCK_VIEW)
text, note = send(unstick_payload())
check("verified FLAT なら CANCELED を publish する",
      PUBLISHED and PUBLISHED[-1].get("state") == "CANCELED", str(PUBLISHED))
check("対象の idempotencyKey に対して記録する",
      PUBLISHED and PUBLISHED[-1].get("idempotencyKey") == "stuck-key-0001", str(PUBLISHED))
check("完了を報告する", "CLEARED" in text, text[:160])
check("order.py は呼ばない(発注経路に入らない)", len(CALLS) == 0, str(CALLS))

reset()
with_state(STUCK_VIEW)
BROKER_ORDERS[0]["orders"][1]["status"] = "WORKING"
BROKER_ORDERS[0]["activeOrders"] = [BROKER_ORDERS[0]["orders"][1]]
text, note = send(unstick_payload())
check("hidden resting orderが生存中ならFLATでも解除しない",
      "BLOCKED" in text and len(PUBLISHED) == 0, text[:160])

reset()
with_state(STUCK_VIEW)
BROKER[0] = {"verified": False, "source": "unavailable", "detail": "HTTP 401"}
text, note = send(unstick_payload())
check("ブローカー未確認なら解除しない", "BLOCKED" in text and len(PUBLISHED) == 0, text[:160])

reset()
with_state(STUCK_VIEW)
BROKER[0] = {"verified": True, "source": "tradovate-rest", "symbol": "MNQU6",
             "side": "LONG", "qty": 2, "observedAt": datetime.now(timezone.utc).isoformat()}
text, note = send(unstick_payload())
check("建玉があれば解除しない(観測漏れではない)",
      "BLOCKED" in text and len(PUBLISHED) == 0, text[:160])

reset()
with_state({"order": dict(STUCK_VIEW["order"], at=FRESH_AT)})
text, note = send(unstick_payload())
check("送信直後の注文は解除しない(約定待ちの可能性)",
      "BLOCKED" in text and len(PUBLISHED) == 0, text[:160])

reset()
with_state(STUCK_VIEW)
text, note = send(unstick_payload(orderKey="stuck-key-9999"))
check("キー不一致(注文の入れ替わり)は解除しない",
      "BLOCKED" in text and len(PUBLISHED) == 0, text[:160])

reset()
with_state({"order": dict(STUCK_VIEW["order"], state="FILLED")})
text, note = send(unstick_payload())
check("終端済みの注文は解除不要として拒否する",
      "BLOCKED" in text and len(PUBLISHED) == 0, text[:160])

reset()
with_state({"order": None})
text, note = send(unstick_payload())
check("注文が無ければ何もしない", "BLOCKED" in text and len(PUBLISHED) == 0, text[:160])

reset()
with_state(STUCK_VIEW)
reused = nonce()
text, note = send(unstick_payload(clientNonce=reused))
text2, note2 = send(unstick_payload(clientNonce=reused))
check("同じ nonce の再送は一度しか実行しない",
      len(PUBLISHED) == 1 and "処理済み" in text2, f"{len(PUBLISHED)} / {text2[:120]}")

nqx_state.fetch_state_quiet = lambda cfg=None: None

# ---------------------------------------------------------------- 後始末

shutil.rmtree(SANDBOX, ignore_errors=True)

print()
print("=" * 68)
print(f"合計: PASS {PASS[0]} / FAIL {FAIL[0]}")
print(f"実際に CrossTrade へ送信した回数: 0(--confirm は全てスタブ)")
print("=" * 68)
sys.exit(1 if FAIL[0] else 0)

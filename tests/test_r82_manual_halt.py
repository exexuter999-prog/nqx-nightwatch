#!/usr/bin/env python3
"""R82: 設定ファイル側の手動HALT(緊急停止)。

``execution_contract.json`` の ``manualHalt.autotrade`` が true の間は、自律経路の
新規 ENTRY・追撃・建玉管理がすべて止まる。環境変数や武装台帳より強い(緊急停止が
`NQX_AUTOTRADE=1` で上書きできては意味がない)。撤退だけは塞がない。

2026-09-14: 撤退の検査が FLAT のブローカーでしか行われておらず、**建玉がある口座では
KILL の全決済が一度も送られていなかった**。``reconcile`` は KILL を通すが、その先の
管理専用注入(R68 の 1 口座経路 / 多口座ループ)が呼ぶ ``_reconcile_one`` の冒頭で
``autotrade_enabled`` が halt に倒されて空を返していた(届いても ``live_enabled`` が
False で dry-run 止まり)。建玉あり / 未約定 ENTRY あり × 1 口座 / 多口座 で、通常の
KILL と同じ送信・再照会・台帳になることを固定する。

照会・送信・claim はすべて注入スタブ、台帳とロックは一時ディレクトリ。本番の
.secrets / CrossTrade / Worker / 武装台帳へは到達しない(到達したら tripwire が数える)。

    python tests/test_r82_manual_halt.py
"""
import contextlib
import copy
import inspect
import json
import os
import sys
import tempfile
from datetime import datetime, timezone

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
import autotrade_arm  # noqa: E402
import autotrade_engine as ae  # noqa: E402
import _pin_contract  # noqa: E402  (R102: 本番の manualHalt と限月をテストから切り離す)
import broker_status  # noqa: E402
import execution_contract  # noqa: E402
import nqx_state  # noqa: E402

ae._read_env_file = lambda path=None: {}   # 本番 .secrets を読まない(hermetic)
# 環境変数は cfg より強い。シェルに残った値でスイッチが決まらないよう、このプロセスでは消す。
for _key in ("NQX_AUTOTRADE", "NQX_LIVE_ORDERS", "NQX_AUTOTRADE_KILL", ae.ENTRY_DISARMED_KEY):
    os.environ.pop(_key, None)

PASS = [0]
FAIL = [0]

#: 注入漏れで本番経路へ落ちたら、ここに名前が積まれる(最後に 0 件を検査する)。
PRODUCTION_REACH = []


def _tripwire(name, result):
    def stub(*_args, **_kwargs):
        PRODUCTION_REACH.append(name)
        return copy.deepcopy(result)
    return stub


broker_status.query_position = _tripwire(
    "broker_status.query_position", {"verified": False, "detail": "tripwire"})
broker_status.query_orders = _tripwire(
    "broker_status.query_orders", {"verified": False, "state": "UNKNOWN", "detail": "tripwire"})
broker_status.query_fills = _tripwire(
    "broker_status.query_fills", {"verified": False, "fills": []})
autotrade_arm.state = _tripwire("autotrade_arm.state", {"valid": False, "reason": "tripwire"})
nqx_state.fetch_state_quiet = _tripwire("nqx_state.fetch_state_quiet", {})
nqx_state.claim_entry = _tripwire("nqx_state.claim_entry", (False, {"reason": "tripwire"}))
nqx_state.claim_management = _tripwire("nqx_state.claim_management",
                                       (False, {"reason": "tripwire"}))


def check(label, condition, detail=""):
    if condition:
        PASS[0] += 1
        print(f"  OK   {label}")
    else:
        FAIL[0] += 1
        print(f"  NG   {label}  {detail}")


@contextlib.contextmanager
def halt(on):
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


@contextlib.contextmanager
def env(**pairs):
    before = {k: os.environ.get(k) for k in pairs}
    os.environ.update({k: v for k, v in pairs.items() if v is not None})
    try:
        yield
    finally:
        for k, v in before.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


NOW = datetime.now(timezone.utc)
SYM = "MNQU6"
SOLO = "ACC-R82"
ACC_A, ACC_B = "ACC-R82-A", "ACC-R82-B"
KILL_NOTE = "autotrade flatten sent: NQX_AUTOTRADE_KILL"


def open_position(account, qty):
    return {"verified": True, "symbol": SYM, "accountId": account, "side": "SHORT",
            "qty": qty, "avgEntry": 29500.0, "orderId": f"{account}-ENTRY",
            "receipt": f"R-{account}-ENTRY", "filledAt": NOW.isoformat(),
            "observedAt": NOW.isoformat()}


def flat_position(account):
    return {"verified": True, "symbol": SYM, "accountId": account, "qty": 0,
            "observedAt": NOW.isoformat()}


def no_orders(account):
    return {"verified": True, "symbol": SYM, "state": "NONE", "orders": [],
            "activeOrders": [], "accountScope": [account]}


def resting_entry(account):
    # 契約の blockingOrderStates(PENDING / SENT / UNKNOWN)に入る未約定 ENTRY。
    rows = [{"orderId": f"{account}-LIMIT", "action": "SELL", "status": "SENT",
             "accountId": account}]
    return {"verified": True, "symbol": SYM, "state": "SENT", "orders": rows,
            "activeOrders": rows, "accountScope": [account]}


class Broker:
    """口座ごとの建玉・注文を持つスタブ。live の ``--flatten`` だけが状態を変える。"""

    def __init__(self, positions, orders, *, solo=None, flatten_takes_effect=True):
        self.positions = dict(positions)
        self.orders = dict(orders)
        self.solo = solo
        self.flatten_takes_effect = flatten_takes_effect
        self.sends = []   # (argv, confirm)
        self.reads = []   # ("position" | "orders", account)

    def position(self, symbol, account=None):
        account = account or self.solo   # 1 口座経路は account 無しで照会する
        self.reads.append(("position", account))
        return copy.deepcopy(self.positions[account])

    def order(self, symbol, known_order_ids=None, account=None):
        account = account or self.solo
        self.reads.append(("orders", account))
        return copy.deepcopy(self.orders[account])

    def runner(self, argv, confirm):
        self.sends.append((list(argv), bool(confirm)))
        if confirm and argv[:1] == ["--flatten"] and self.flatten_takes_effect:
            account = argv[argv.index("--account") + 1]
            self.positions[account] = flat_position(account)
            self.orders[account] = no_orders(account)
        return 0, "stub ok"


#: KILL の撤退経路で呼ばれてはならない注入口(claim / state / recover / fills / quote)。
FORBIDDEN = []


def forbidden(name, result=None):
    def stub(*_args, **_kwargs):
        FORBIDDEN.append(name)
        return result if result is not None else (False, {"reason": f"{name} reached"})
    return stub


def bundle(scope):
    return {"_published_scenario": {"symbol": SYM,
                                    "executionContract": {"accountScope": list(scope)}},
            "price": 29480.0, "at": NOW.isoformat(),
            "snapshot": {"bars3m": [{"h": 29490.0, "l": 29470.0, "c": 29480.0}] * 12}}


def ledger_rows(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def run(broker, scope, tmp, *, kill, auto="0", live="0"):
    """``reconcile()`` を通す —— 管理専用の cfg 注入(R68 / 多口座ループ)はその中にある。"""
    ledger = os.path.join(tmp, "ledger.jsonl")
    cfg = {"NQX_SYMBOL": SYM, "CROSSTRADE_ACCOUNTS": ",".join(scope),
           "NQX_AUTOTRADE": auto, "NQX_LIVE_ORDERS": live,
           "NQX_AUTOTRADE_KILL": "1" if kill else "0", "NQX_STALE_ENTRY_CANCEL": "0"}
    notes = ae.reconcile(
        bundle(scope), True, cfg, broker.position, broker.runner, ledger, NOW,
        broker_order_query=broker.order,
        state_query=forbidden("state_query", {}),
        claim_entry=forbidden("claim_entry"), claim_management=forbidden("claim_management"),
        recover_entry=forbidden("recover_entry"),
        recover_management=forbidden("recover_management"),
        broker_fills_query=forbidden("broker_fills_query", {"verified": False}),
        fresh_price_query=forbidden("fresh_price_query", 29480.0))
    return notes, ledger_rows(ledger), ledger


def entry_or_management(sends):
    """ENTRY / 追撃 / MODIFY を示す引数を 1 つでも含む送信。"""
    flags = ("--modify", "--split-tp", "--pyramid", "--entry", "--market", "--side")
    return [argv for argv, _confirm in sends
            if any(str(item).startswith(flag) for item in argv for flag in flags)]


def flatten(account):
    return ["--flatten", "--account", account]


print("--- 設定の読み取り ---")
with halt(False):
    check("既定(false)では halt しない", ae.manual_halt({}) is False)
with halt(True):
    check("true で halt する", ae.manual_halt({}) is True)
section = execution_contract.CONTRACT.get("manualHalt")
check("契約に manualHalt.autotrade が存在し、既定は false",
      isinstance(section, dict) and section.get("autotrade") is False, str(section))

print()
print("--- 環境変数より強い ---")
with halt(True), env(NQX_AUTOTRADE="1", NQX_LIVE_ORDERS="1"):
    check("NQX_AUTOTRADE=1 でも autotrade_enabled は False",
          ae.autotrade_enabled({}) is False)
    check("NQX_LIVE_ORDERS=1 でも live_enabled は False",
          ae.live_enabled({}) is False)
with halt(True):
    check("cfg 側で渡しても False", ae.autotrade_enabled({"NQX_AUTOTRADE": "1"}) is False)
with halt(False), env(NQX_AUTOTRADE="1"):
    check("halt を降ろせば従来どおり有効", ae.autotrade_enabled({}) is True)

print()
print("--- reconcile は何も触らない ---")
with halt(True), env(NQX_AUTOTRADE="1", NQX_LIVE_ORDERS="1"), \
        tempfile.TemporaryDirectory() as tmp:
    touched = []

    def broker(symbol, account=None):
        touched.append(account)
        return {"verified": True, "qty": 2, "side": "SHORT", "accountId": account}

    def orders(symbol, known_order_ids=None, account=None):
        touched.append(("ORDERS", account))
        return no_orders(account)

    def runner(args, confirm):
        touched.append(("SEND", args))
        return 0, "should not be called"

    ledger = os.path.join(tmp, "ledger.jsonl")
    notes = ae.reconcile({"scenarios": {"primary": {}}},
                         cfg={"CROSSTRADE_ACCOUNTS": "ACC-1", "NQX_AUTOTRADE_KILL": "0"},
                         broker_query=broker, runner=runner, ledger_path=ledger,
                         broker_order_query=orders)
    check("MANUAL HALT の注記だけを返す",
          len(notes) == 1 and "MANUAL HALT" in notes[0], str(notes))
    check("ブローカー照会も送信も一切しない", touched == [], str(touched))
    check("台帳もロックも作らない",
          not os.path.exists(ledger) and not os.path.exists(ledger + ".lock"))

print()
print("--- 撤退は塞がない(FLAT) ---")
with halt(True), env(NQX_AUTOTRADE_KILL="1"), tempfile.TemporaryDirectory() as tmp:
    check("KILL が立っていれば halt は reconcile を止めない",
          ae.manual_halt({}) is True and ae.kill_enabled({}) is True)
    notes = ae.reconcile({"scenarios": {"primary": {}}},
                         cfg={"CROSSTRADE_ACCOUNTS": "ACC-1"},
                         broker_query=lambda s, account=None: flat_position("ACC-1"),
                         runner=lambda args, confirm: (0, "ok"),
                         ledger_path=os.path.join(tmp, "ledger.jsonl"),
                         broker_order_query=lambda s, known_order_ids=None, account=None:
                         no_orders("ACC-1"))
    check("KILL 経路では MANUAL HALT で早期 return しない",
          not (len(notes) == 1 and "MANUAL HALT" in str(notes[0])), str(notes))
    check("FLAT + 注文終端なら送らずに KILL 完了を報告する",
          notes == ["autotrade kill: broker verified FLAT and broker order terminal"], str(notes))

print()
print("--- 撤退は塞がない: 1 口座・建玉あり(SHORT 4) ---")
with halt(True), tempfile.TemporaryDirectory() as tmp:
    b = Broker({SOLO: open_position(SOLO, 4)}, {SOLO: no_orders(SOLO)}, solo=SOLO)
    notes, rows, _ = run(b, [SOLO], tmp, kill=True)
    check("dry-run → live の順で --flatten --account を 1 回ずつ送る",
          b.sends == [(flatten(SOLO), False), (flatten(SOLO), True)], str(b.sends))
    check("注記は通常の KILL と同じ", notes == [KILL_NOTE], str(notes))
    sent = [row for row in rows if row.get("status") == "FLATTEN_SENT"]
    check("台帳に FLATTEN_SENT(KILL:<建玉世代>:FLATTEN)が 1 行",
          len(sent) == 1 and sent[0]["key"].startswith("KILL:PG:1:POS:")
          and sent[0]["key"].endswith(":FLATTEN") and sent[0].get("action") == "FLATTEN"
          and sent[0].get("reason") == "NQX_AUTOTRADE_KILL", str(rows))
    check("事前照会 2 回(注入経路 + _reconcile_one)の後、送信後に建玉 → 注文を再照会する",
          b.reads == [("position", SOLO), ("orders", SOLO)] * 3, str(b.reads))
    check("ENTRY / 追撃 / MODIFY は 1 本も組み立てない",
          entry_or_management(b.sends) == [], str(b.sends))

print()
print("--- 撤退は塞がない: 1 口座・FLAT に未約定 ENTRY(SENT) ---")
with halt(True), tempfile.TemporaryDirectory() as tmp:
    b = Broker({SOLO: flat_position(SOLO)}, {SOLO: resting_entry(SOLO)}, solo=SOLO)
    notes, rows, _ = run(b, [SOLO], tmp, kill=True)
    check("未約定 ENTRY は --flatten(取消)で落とす",
          b.sends == [(flatten(SOLO), False), (flatten(SOLO), True)], str(b.sends))
    check("注記は通常の KILL と同じ", notes == [KILL_NOTE], str(notes))
    check("台帳に FLATTEN_SENT(KILL:FLAT:<銘柄>:<注文状態>:FLATTEN)",
          [(row["key"], row.get("action")) for row in rows
           if row.get("status") == "FLATTEN_SENT"]
          == [(f"KILL:FLAT:{SYM}:SENT:FLATTEN", "FLATTEN")], str(rows))

print()
print("--- 撤退は塞がない: 送信後も建玉が残る → HALT(自動再送しない) ---")
with halt(True), tempfile.TemporaryDirectory() as tmp:
    b = Broker({SOLO: open_position(SOLO, 4)}, {SOLO: no_orders(SOLO)}, solo=SOLO,
               flatten_takes_effect=False)
    notes, rows, _ = run(b, [SOLO], tmp, kill=True)
    check("live 送信は 1 回だけ", [confirm for _argv, confirm in b.sends] == [False, True],
          str(b.sends))
    halts = [row for row in rows if row.get("status") == "HALT"]
    check("台帳に FLATTEN の HALT が 1 行",
          len(halts) == 1 and halts[0].get("action") == "FLATTEN"
          and halts[0]["key"].startswith("KILL:PG:1:POS:")
          and "flatten not confirmed" in str(halts[0].get("reason")), str(rows))
    check("FLATTEN_SENT は書かない", not any(row.get("status") == "FLATTEN_SENT" for row in rows),
          str(rows))
    check("注記は AUTOTRADE HALT",
          notes == ["AUTOTRADE HALT: flatten not confirmed; position qty=4"], str(notes))

print()
print("--- halt のみ(KILL なし)・1 口座・建玉あり: AUTO/LIVE が立っていても何も触らない ---")
with halt(True), tempfile.TemporaryDirectory() as tmp:
    b = Broker({SOLO: open_position(SOLO, 4)}, {SOLO: no_orders(SOLO)}, solo=SOLO)
    notes, rows, ledger = run(b, [SOLO], tmp, kill=False, auto="1", live="1")
    check("MANUAL HALT の注記だけを返す",
          len(notes) == 1 and "MANUAL HALT" in notes[0], str(notes))
    check("照会も送信もしない(建値移動・トレール・追撃・新規なし)",
          b.reads == [] and b.sends == [], f"reads={b.reads} sends={b.sends}")
    check("台帳もロックも作らない",
          not os.path.exists(ledger) and not os.path.exists(ledger + ".lock"))

print()
print("--- 撤退は塞がない: 多口座・A(SHORT 4)/ B(SHORT 2)とも建玉あり ---")
with halt(True), tempfile.TemporaryDirectory() as tmp:
    b = Broker({ACC_A: open_position(ACC_A, 4), ACC_B: open_position(ACC_B, 2)},
               {ACC_A: no_orders(ACC_A), ACC_B: no_orders(ACC_B)})
    notes, rows, _ = run(b, [ACC_A, ACC_B], tmp, kill=True)
    check("A / B を別々に、各 live の前に同じ引数の dry-run で --flatten --account",
          b.sends == [(flatten(ACC_A), False), (flatten(ACC_A), True),
                      (flatten(ACC_B), False), (flatten(ACC_B), True)], str(b.sends))
    check("口座ごとに結果を報告",
          notes == [f"[{ACC_A}] {KILL_NOTE}", f"[{ACC_B}] {KILL_NOTE}"], str(notes))
    sent = [row["key"] for row in rows if row.get("status") == "FLATTEN_SENT"]
    check("FLATTEN_SENT は口座ごとに 1 行(建玉世代の key が別)",
          len(sent) == 2 and len(set(sent)) == 2, str(rows))
    check("各口座を事前 2 回 + 送信後 1 回、別々に照会(先頭口座を流用しない)",
          b.reads.count(("position", ACC_A)) == 3 and b.reads.count(("position", ACC_B)) == 3
          and b.reads.count(("orders", ACC_A)) == 3 and b.reads.count(("orders", ACC_B)) == 3,
          str(b.reads))
    check("ENTRY / 追撃 / MODIFY は 1 本も組み立てない",
          entry_or_management(b.sends) == [], str(b.sends))

print()
print("--- 撤退は塞がない: 多口座・A だけ建玉(B は FLAT) ---")
with halt(True), tempfile.TemporaryDirectory() as tmp:
    b = Broker({ACC_A: open_position(ACC_A, 4), ACC_B: flat_position(ACC_B)},
               {ACC_A: no_orders(ACC_A), ACC_B: no_orders(ACC_B)})
    notes, rows, _ = run(b, [ACC_A, ACC_B], tmp, kill=True)
    check("建玉のある A だけを落とし、FLAT の B には送らない",
          b.sends == [(flatten(ACC_A), False), (flatten(ACC_A), True)], str(b.sends))
    check("A の結果だけを報告", notes == [f"[{ACC_A}] {KILL_NOTE}"], str(notes))

print()
print("--- halt のみ(KILL なし)・多口座・両口座建玉: AUTO/LIVE が立っていても何も触らない ---")
with halt(True), tempfile.TemporaryDirectory() as tmp:
    b = Broker({ACC_A: open_position(ACC_A, 4), ACC_B: open_position(ACC_B, 2)},
               {ACC_A: no_orders(ACC_A), ACC_B: no_orders(ACC_B)})
    notes, rows, ledger = run(b, [ACC_A, ACC_B], tmp, kill=False, auto="1", live="1")
    check("MANUAL HALT の注記だけを返す",
          len(notes) == 1 and "MANUAL HALT" in notes[0], str(notes))
    check("どの口座にも照会も送信もしない", b.reads == [] and b.sends == [],
          f"reads={b.reads} sends={b.sends}")
    check("台帳もロックも作らない",
          not os.path.exists(ledger) and not os.path.exists(ledger + ".lock"))

print()
print("--- 通常の KILL と同一(送信・注記・台帳) ---")


def outcome(scope, positions, orders, *, halted, auto, live, solo=None):
    with halt(halted), tempfile.TemporaryDirectory() as tmp:
        b = Broker(positions, orders, solo=solo)
        notes, rows, _ = run(b, scope, tmp, kill=True, auto=auto, live=live)
        shape = [(row.get("key"), row.get("status"), row.get("action"), row.get("reason"))
                 for row in rows]
        return notes, b.sends, shape


FIXTURES = (
    ("1 口座・建玉", [SOLO], {SOLO: open_position(SOLO, 4)}, {SOLO: no_orders(SOLO)}, SOLO),
    ("1 口座・未約定 ENTRY", [SOLO], {SOLO: flat_position(SOLO)}, {SOLO: resting_entry(SOLO)},
     SOLO),
    ("多口座・両建玉", [ACC_A, ACC_B],
     {ACC_A: open_position(ACC_A, 4), ACC_B: open_position(ACC_B, 2)},
     {ACC_A: no_orders(ACC_A), ACC_B: no_orders(ACC_B)}, None),
)
for label, scope, positions, orders, solo in FIXTURES:
    halted = outcome(scope, positions, orders, halted=True, auto="0", live="0", solo=solo)
    for auto in ("0", "1"):
        normal = outcome(scope, positions, orders, halted=False, auto=auto, live=auto,
                         solo=solo)
        check(f"{label}: manualHalt 中の KILL = halt なしの KILL(AUTO={auto})",
              halted == normal and bool(halted[1]), f"\n    halt={halted}\n    norm={normal}")

print()
print("--- KILL が周期の途中で降りても、halt 中は建玉管理へ落ちない ---")
with halt(True), tempfile.TemporaryDirectory() as tmp:
    readings = iter([{"valid": True, "kill": True}])
    seen = []

    def flipping_arm_state(path=None, cfg=None, now=None):
        seen.append("state")
        return next(readings, {"valid": True, "kill": False})

    tripwire_state = autotrade_arm.state
    autotrade_arm.state = flipping_arm_state
    try:
        b = Broker({SOLO: open_position(SOLO, 4)}, {SOLO: no_orders(SOLO)}, solo=SOLO)
        # KILL だけを武装台帳から読ませる(AUTO/LIVE は cfg で明示)。
        notes = ae._reconcile_one(
            bundle([SOLO]), True,
            {"NQX_SYMBOL": SYM, "CROSSTRADE_ACCOUNTS": SOLO,
             "NQX_AUTOTRADE": "1", "NQX_LIVE_ORDERS": "1"},
            broker_query=b.position, runner=b.runner,
            ledger_path=os.path.join(tmp, "ledger.jsonl"), now=NOW,
            broker_order_query=b.order, state_query=forbidden("state_query", {}),
            claim_entry=forbidden("claim_entry"), claim_management=forbidden("claim_management"),
            recover_entry=forbidden("recover_entry"),
            recover_management=forbidden("recover_management"),
            broker_fills_query=forbidden("broker_fills_query", {"verified": False}),
            fresh_price_query=forbidden("fresh_price_query", 29480.0))
    finally:
        autotrade_arm.state = tripwire_state
    check("KILL を読み直している(撤退判定 → KILL 分岐)", len(seen) >= 2, str(seen))
    check("MANUAL HALT で止まり、管理(hold / MODIFY)の注記を出さない",
          len(notes) == 1 and "MANUAL HALT" in notes[0], str(notes))
    check("何も送らない", b.sends == [], str(b.sends))

print()
print("--- 門の位置(halt を緩めていない) ---")
source = open(os.path.join(BASE, "autotrade_engine.py"), encoding="utf-8").read()
check("halt を除いた二重キー(_kill_exit_switches)は _reconcile_one の KILL 撤退判定だけが使う",
      source.count("_kill_exit_switches(") == 3
      and inspect.getsource(ae._reconcile_one).count("_kill_exit_switches(") == 2,
      f"total={source.count('_kill_exit_switches(')}")
with halt(True):
    check("KILL が立っていても autotrade_enabled / live_enabled は False のまま",
          ae.autotrade_enabled({"NQX_AUTOTRADE": "1", "NQX_AUTOTRADE_KILL": "1"}) is False
          and ae.live_enabled({"NQX_AUTOTRADE": "1", "NQX_LIVE_ORDERS": "1",
                               "NQX_AUTOTRADE_KILL": "1"}) is False)

print()
print("--- 注入の外へ出ていない ---")
check("本番の照会・武装台帳・Worker へは一度も到達しない", PRODUCTION_REACH == [],
      str(PRODUCTION_REACH))
check("claim / state / recover / fills / quote は KILL の撤退経路で呼ばれない",
      FORBIDDEN == [], str(FORBIDDEN))

print()
print(f"合計: PASS {PASS[0]} / FAIL {FAIL[0]}")
sys.exit(1 if FAIL[0] else 0)

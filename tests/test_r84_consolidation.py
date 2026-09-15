# -*- coding: utf-8 -*-
"""R84: 統合(§5.3)を engine の送信経路ごと固定する。

**これを落とすと次の周期に所有権が落ちる** —— `cancelandbracket` は古いブラケットを
全部取り消すので、凍結した bracket id はもう存在しない。新しい 1 組を凍結し直せない
限り、構造導出枚数は 0 になって「ブローカー上に構造が無い」と読まれる。

    python tests/test_r84_consolidation.py
"""
import copy
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _r84_fixtures as fx  # noqa: E402

sys.path.insert(0, fx.BASE)
import autotrade_engine as ae  # noqa: E402
import _pin_contract  # noqa: E402  (R102: 本番の manualHalt と限月をテストから切り離す)

ae._read_env_file = lambda path=None: {}
import broker_status  # noqa: E402
import management_intent  # noqa: E402
import tranche  # noqa: E402

PASS = [0]
FAIL = [0]


def check(label, condition, detail=""):
    if condition:
        PASS[0] += 1
        print(f"  OK   {label}")
    else:
        FAIL[0] += 1
        print(f"  NG   {label}  {detail}")


T1, T2 = fx.tranche_one(), fx.tranche_two()
RUNNER_ROWS = (fx.bracket_rows(T1["legs"][1]["orderId"], T1["legs"][1]["bracketOrderIds"])
               + fx.bracket_rows(T2["legs"][1]["orderId"], T2["legs"][1]["bracketOrderIds"]))
PAIR_IDS = ["C-STOP", "C-TARGET"]
PAIR_ROWS = fx.oco_rows(PAIR_IDS)
POSITION = fx.position(qty=4, avg_entry=29466.375)
IDENTITY = broker_status.position_identity(POSITION)
GENERATION = "PG:1:" + str(IDENTITY)
PLAN = fx.composite(qty=4)
PLAN["legs"] = [{"id": "RUNNER", "qty": 2, "target": 29380.0, "trancheId": "T1"},
                {"id": "RUNNER", "qty": 2, "target": 29300.0, "trancheId": "T2"}]
PLAN["positionOwnership"] = {"generation": GENERATION, "rawIdentity": IDENTITY,
                             "accountId": fx.ACC, "symbol": fx.SYM, "side": "SELL",
                             "orderId": POSITION["orderId"], "receipt": None,
                             "filledAt": POSITION["filledAt"],
                             "avgEntry": 29466.375, "initialQty": 4}
BUNDLE = {"_published_scenario": {}, "price": 29380.0,
          "at": "2026-09-12T03:00:00Z",
          "snapshot": {"bars3m": [{"h": 29400.0, "l": 29350.0, "c": 29380.0}] * 12}}
CFG = {"NQX_SYMBOL": fx.SYM, "NQX_AUTOTRADE": "1", "NQX_LIVE_ORDERS": "1",
       "CROSSTRADE_ACCOUNTS": fx.ACC, "NQX_AUTOTRADE_KILL": "0",
       "NQX_STALE_ENTRY_CANCEL": "0"}


def claim_stub(intent):
    return True, {"managementKey": management_intent.management_key(intent),
                  "claimToken": "T" * 43,
                  "managementIntentHash": management_intent.intent_hash(intent)}


class World:
    """張り替えが成功したら、古い 2 組が消えて新しい 1 組だけになる世界。"""

    def __init__(self, emit_marker=True, bind_after=True):
        self.modified = False
        self.emit_marker = emit_marker
        self.bind_after = bind_after
        self.calls = []

    def orders(self):
        if not self.modified:
            return fx.orders(RUNNER_ROWS)
        return fx.orders(PAIR_ROWS if self.bind_after else [])

    def runner(self, args, confirm):
        self.calls.append((list(args), bool(confirm)))
        if not confirm:
            return 0, "stub dry-run ok"
        self.modified = True
        out = "MODIFY VERIFIED: structure"
        if self.emit_marker:
            out += "\n" + ae.MODIFY_BRACKET_MARKER + json.dumps({
                "account": fx.ACC, "symbol": fx.SYM, "action": "BUY",
                "qty": 4, "sl": 29465.5, "tp": 29300.0, "consolidated": True,
                "accounts": [{"accountId": fx.ACC, "stopOrderId": PAIR_IDS[0],
                              "targetOrderId": PAIR_IDS[1],
                              "stopReceipt": "R:" + PAIR_IDS[0],
                              "targetReceipt": "R:" + PAIR_IDS[1],
                              "ocoGroupId": "G"}]}, sort_keys=True)
        return 0, out


def make_ledger(tmp, plan=None):
    path = os.path.join(tmp, "r84c.jsonl")
    ae._append_ledger({"key": "POSITION_GENERATION:" + fx.ACC + ":1:" + str(IDENTITY),
                       "status": "POSITION_GENERATION", "generation": 1,
                       "identity": IDENTITY, "open": True, "accountId": fx.ACC,
                       "symbol": fx.SYM, "side": "SHORT"}, path)
    ae._append_ledger({"key": PLAN["entryKey"], "entryKey": PLAN["entryKey"],
                       "status": "ENTRY_SENT", "action": ae.PYRAMID_COMMIT,
                       "plan": plan or PLAN, "routeState": "SENT"}, path)
    return path


def run(path, world):
    def query(_symbol, account=None):
        return copy.deepcopy(POSITION)

    def order_query(_symbol, known_order_ids=None, account=None):
        return copy.deepcopy(world.orders())

    return ae._reconcile_one(
        BUNDLE, True, dict(CFG), query, world.runner, path, None, order_query,
        lambda: {}, None, claim_stub, fresh_price_query=lambda _symbol: 29380.0)


def rows_of(path):
    with open(path, encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


print("--- 統合 MODIFY と再凍結 ---")
tmp = tempfile.mkdtemp(prefix="nqx-r84-con-")
try:
    path = make_ledger(tmp)
    world = World()
    notes = run(path, world)
    sent = [args for args, confirm in world.calls if confirm]
    check("MODIFY が 1 回だけ送られる", len(sent) == 1, str(world.calls))
    check("--pyramid-consolidate が付く(order.py の >2 本ガードを統合として通す)",
          "--pyramid-consolidate" in sent[0], str(sent[0]))
    check("--ultra も付く(保護注文の組数で判定させる)", "--ultra" in sent[0])
    check("qty は建玉全量", sent[0][sent[0].index("--qty") + 1] == "4", str(sent[0]))
    rows = [row for row in rows_of(path) if row["status"] != "POSITION_GENERATION"]
    statuses = [row["status"] for row in rows]
    check("MANAGEMENT_CLAIMED → MANAGEMENT_SENT → ENTRY_SENT(再凍結)",
          statuses == ["ENTRY_SENT", "MANAGEMENT_CLAIMED", "MANAGEMENT_SENT", "ENTRY_SENT"],
          str(statuses))
    frozen = rows[-1]
    check("再凍結は PYRAMID_CONSOLIDATED",
          frozen["action"] == ae.PYRAMID_CONSOLIDATED, str(frozen.get("action")))
    collapsed = frozen["plan"]
    check("トランシェは 1 本へ畳まれる", len(collapsed["tranches"]) == 1,
          str(len(collapsed["tranches"])))
    check("その脚は RUNNER 1 本・統合枚数",
          collapsed["tranches"][0]["legs"] == [{"id": "RUNNER", "qty": 4, "target": 29300.0}],
          str(collapsed["tranches"][0]["legs"]))
    check("新しい 1 組の身元が焼かれる",
          collapsed["consolidation"]["orderIds"] == sorted(PAIR_IDS),
          str(collapsed["consolidation"]))
    # **構造 SL は動かさない。** `trade_journal.build_trade_result` は
    # `plan["initialStop"]` を R の分母にするので、統合のたびにトレール後の SL で
    # 上書きすると「リスクが縮んだこと」にされ R が実際より大きく記録される。
    # 単一プランも initialStop は動かない(トレールは MANAGEMENT_SENT の action.sl)。
    check("構造 SL(R の分母)は張り替えで動かない",
          collapsed["initialStop"] == PLAN["initialStop"], str(collapsed["initialStop"]))
    check("張り替えた保護水準は consolidation.sl に持つ",
          collapsed["consolidation"]["sl"] == 29465.5,
          str(collapsed["consolidation"].get("sl")))
    check("pyramid.protectiveStop にも同じ値が載る",
          collapsed["pyramid"]["protectiveStop"] == 29465.5,
          str(collapsed["pyramid"].get("protectiveStop")))
    # 統合トランシェの scenarioId は 1 つしか持てない。畳んだ決定を落とすと、基礎を
    # 作ったシグナルが再掲されたときに「新規シグナル」と取り違えて建て増す。
    check("畳んだトランシェの決定を全部残す(sourceScenarioIds)",
          {"S-T1", "S-T2"} <= set(collapsed["tranches"][0].get("sourceScenarioIds") or []),
          str(collapsed["tranches"][0].get("sourceScenarioIds")))
    check("台帳が無くても、統合後のプランだけで消費済みの決定を数えられる",
          {"S-T1", "S-T2"} <= ae._pyramid_consumed_decisions([], fx.ACC, collapsed),
          str(ae._pyramid_consumed_decisions([], fx.ACC, collapsed)))

    # 次の周期: 畳んだプランで所有権が続く(**ここが落ちると実弾が管理外になる**)
    binding = tranche.bind_composite(collapsed, POSITION, fx.orders(PAIR_ROWS),
                                     position_generation=GENERATION)
    check("畳んだ後も所有権が続く",
          binding["owned"] is True and binding["expectedQty"] == 4,
          str(binding.get("reason")))
    check("位相は RUNNERS のまま", binding["phase"] == tranche.RUNNERS)
    check("注記に統合が出る", any("consolidated" in note for note in notes), str(notes))
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print()
print("--- order.py の行が読めなくてもブローカーへ聞き直す ---")
tmp = tempfile.mkdtemp(prefix="nqx-r84-con2-")
try:
    path = make_ledger(tmp)
    world = World(emit_marker=False)
    run(path, world)
    rows = [row for row in rows_of(path) if row["status"] != "POSITION_GENERATION"]
    collapsed = rows[-1]["plan"]
    check("R80 の OCO 兄弟判定で 1 組を束縛し直す",
          collapsed["consolidation"]["orderIds"] == sorted(PAIR_IDS),
          str(collapsed["consolidation"]))
    check("pending にはならない",
          collapsed["tranches"][0]["consolidationPair"].get("pending") is not True)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print()
print("--- どちらでも束縛できなければ pending で凍結し、FLATTEN だけ残す ---")
tmp = tempfile.mkdtemp(prefix="nqx-r84-con3-")
try:
    path = make_ledger(tmp)
    world = World(emit_marker=False, bind_after=False)
    notes = run(path, world)
    rows = [row for row in rows_of(path) if row["status"] != "POSITION_GENERATION"]
    collapsed = rows[-1]["plan"]
    check("pending として凍結される",
          collapsed["tranches"][0]["consolidationPair"].get("pending") is True,
          str(collapsed["tranches"][0].get("consolidationPair")))
    check("枚数は保たれる(CLOSED と読んで所有権を落とさない)",
          collapsed["qty"] == 4, str(collapsed.get("qty")))
    binding = tranche.bind_composite(collapsed, POSITION, fx.orders([]),
                                     position_generation=GENERATION)
    check("所有は続くが遷移中",
          binding["owned"] is True and binding["state"] == "PYRAMID_TRANSIT",
          str(binding.get("reason")))
    check("遷移中は MODIFY を出さない",
          tranche.management_action_composite(collapsed, POSITION, price=29380.0,
                                              states=binding["legStates"],
                                              transit=True) is None)
    check("遷移中でも KILL の FLATTEN は出る",
          (tranche.management_action_composite(collapsed, POSITION, price=29380.0,
                                               states=binding["legStates"],
                                               transit=True, kill=True) or {}
           ).get("action") == "FLATTEN")
    check("注記が pending を知らせる",
          any("pending" in note for note in notes), str(notes))

    # 後の周期でブローカーから 1 組を束縛できれば畳み直す
    world2 = World()
    world2.modified = True

    def query2(_symbol, account=None):
        return copy.deepcopy(POSITION)

    def order_query2(_symbol, known_order_ids=None, account=None):
        return copy.deepcopy(fx.orders(PAIR_ROWS))

    ae._reconcile_one(BUNDLE, True, dict(CFG), query2, world2.runner, path, None,
                      order_query2, lambda: {}, None, claim_stub,
                      fresh_price_query=lambda _symbol: 29380.0)
    rows = [row for row in rows_of(path) if row["status"] != "POSITION_GENERATION"]
    # 最後の行は追撃評価の注記になりうる。凍結プランの正本は走査で引く。
    frozen = ae._frozen_plan_record(rows, fx.SYM, account=fx.ACC)
    resolved = (frozen or {}).get("plan")
    check("次の周期で pending が解消される",
          isinstance(resolved, dict)
          and resolved["consolidation"]["orderIds"] == sorted(PAIR_IDS)
          and resolved["tranches"][0]["consolidationPair"].get("pending") is not True,
          str((frozen or {}).get("action")))
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print()
print("--- 張り替えが拒否されたとき(R78 修復のトランシェ対応) ---")


class RejectingWorld:
    """cancelandbracket の取消だけが成立し、新しい stop が拒否された世界。

    残る保護行の本数を指定できる。合成では「守られている本数」は 2 本ではなく
    **生きている脚の数 × 2** 本なので、4 本残っていれば裸ではない(= 修復しない)。
    """

    def __init__(self, remaining_rows):
        self.remaining = remaining_rows
        self.modified = False
        self.repaired = False
        self.calls = []

    def orders(self):
        if self.repaired:
            # 修復も cancelandbracket。**新しい 1 組**が張られた世界。
            return fx.orders(PAIR_ROWS)
        if not self.modified:
            return fx.orders(RUNNER_ROWS)
        return fx.orders(RUNNER_ROWS[:self.remaining])

    def runner(self, args, confirm):
        self.calls.append((list(args), bool(confirm)))
        if not confirm:
            return 0, "stub dry-run ok"
        if "--modify" in args and not self.modified:
            self.modified = True
            return 1, "ERROR: broker rejected the replacement stop"
        if "--modify" in args:
            self.repaired = True
            return 0, "MODIFY VERIFIED: repaired"
        return 0, "stub"


for remaining, repairs in ((4, False), (0, True)):
    tmp = tempfile.mkdtemp(prefix="nqx-r84-rep-")
    try:
        path = make_ledger(tmp)
        world = RejectingWorld(remaining)
        notes = run(path, world)
        rows = [row for row in rows_of(path) if row["status"] != "POSITION_GENERATION"]
        repaired = any(row["status"] == "MODIFY_FAILED_REPAIRED" for row in rows)
        halted = any(row["status"] == "HALT" for row in rows)
        label = f"保護行が {remaining} 本残る"
        if repairs:
            check(f"{label}: 裸として同じ周期で修復する",
                  repaired and not halted, str([r["status"] for r in rows]))
            check(f"{label}: 修復の MODIFY にも --pyramid-consolidate が付く",
                  any("--pyramid-consolidate" in args
                      for args, confirm in world.calls if confirm and "--modify" in args),
                  str(world.calls))
            # **修復も cancelandbracket なので凍結した対はもう無い。** 再凍結しないと
            # 次の周期に全脚 CLOSED と読まれ、修復したのに管理外になる。
            frozen = ae._frozen_plan_record(rows, fx.SYM, account=fx.ACC)
            plan = (frozen or {}).get("plan") or {}
            check(f"{label}: 修復後の 1 組が凍結し直される",
                  (plan.get("consolidation") or {}).get("orderIds") == sorted(PAIR_IDS),
                  str(plan.get("consolidation")))
            binding = tranche.bind_composite(plan, POSITION, fx.orders(PAIR_ROWS),
                                             position_generation=GENERATION)
            check(f"{label}: 修復の次の周期も所有が続く",
                  binding["owned"] is True and binding["expectedQty"] == 4,
                  str(binding.get("reason")))
        else:
            check(f"{label}: 2 組とも生きているので修復せず HALT(人が確認)",
                  halted and not repaired, str([r["status"] for r in rows]))
            check(f"{label}: 注記は HALT", any("HALT" in note for note in notes), str(notes))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

print()
print(f"合計: PASS {PASS[0]} / FAIL {FAIL[0]}")
sys.exit(1 if FAIL[0] else 0)

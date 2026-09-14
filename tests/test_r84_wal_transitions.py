# -*- coding: utf-8 -*-
"""R84: 追撃 WAL の遷移表(§3.4)と、下書きが凍結プランに化けないこと(§1.3)。

`_frozen_plan_record()` は `plan` キーを持つ行しか採らない。PYRAMID_CLAIMED /
PYRAMID_SENT は `prePlan` / `postPlanDraft` に下書きを入れるので、**コミット前の
下書きが凍結プランとして選ばれる事故が型の上で起きない**。ここではその型と、
送信・claim・dry-run が失敗したときの各遷移を実際に `_reconcile_one` で回す。

    python tests/test_r84_wal_transitions.py
"""
import copy
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _r84_fixtures as fx  # noqa: E402

sys.path.insert(0, fx.BASE)
import autotrade_engine as ae  # noqa: E402

ae._read_env_file = lambda path=None: {}
import execution_contract  # noqa: E402
import nqx_state  # noqa: E402
import route_envelope  # noqa: E402
import strategy_evidence  # noqa: E402
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


NOW = datetime.now(timezone.utc)
EVIDENCE = strategy_evidence.canonicalize({
    "version": "R14-STRATEGY-EVIDENCE-1", "asOf": NOW.isoformat(),
    "sessionId": "NY-R84", "source": "fixture", "provenance": "test",
    "models": {"ifvg": {"valid": False}},
})
ADD_PRICE = 29448.5
SCENARIO = {
    "decisionId": "r84-add", "scenarioId": "r84-add", "model": "VP80_REVERSION",
    "grade": "A+", "state": "ARMED", "fingerprint": "fp-r84-add", "symbol": fx.SYM,
    "marketCycleId": "cycle-r84-1", "side": "SELL", "qty": 8,
    "entry": ADD_PRICE, "stop": 29507.5, "target": 29400.0,
    "targets": [29400.0, 29300.0], "targetR": [1.0, 2.5],
    "legs": [{"id": "TP1", "qty": 4, "target": 29400.0},
             {"id": "RUNNER", "qty": 4, "target": 29300.0}],
    "planVersion": "R19-ICT-SPLIT-1",
    "issuedAt": NOW.isoformat(), "observedAt": NOW.isoformat(),
    "expiresAt": (NOW + timedelta(minutes=5)).isoformat(),
    "evidenceHash": EVIDENCE["evidenceHash"], "setupVersion": "R14-SETUP",
    "catalogVersion": "R14-CATALOG", "detectorVersion": "R14-DETECTOR",
    "executionContractVersion": execution_contract.VERSION,
    "executionContract": {"accountScope": [fx.ACC], "riskCapDollars": 3100.0,
                          "riskCapSource": "ACCOUNT_DRAWDOWN_BUFFER"},
}
BUNDLE = {
    "_published_scenario": SCENARIO, "price": ADD_PRICE,
    "at": NOW.isoformat(), "cvdAt": NOW.isoformat(),
    "snapshot": {"bars3m": [{"h": 29460.0, "l": 29440.0, "c": ADD_PRICE}] * 12},
}
CFG = {"NQX_SYMBOL": fx.SYM, "NQX_AUTOTRADE": "1", "NQX_LIVE_ORDERS": "1",
       "CROSSTRADE_ACCOUNTS": fx.ACC, "NQX_AUTOTRADE_KILL": "0",
       "NQX_STALE_ENTRY_CANCEL": "0"}


def view():
    market = {"at": NOW.isoformat(), "observedAt": NOW.isoformat(), "cvdAt": NOW.isoformat(),
              "cycleId": SCENARIO["marketCycleId"], "cycleCommitted": True, "verified": True,
              "source": "fixture", "sourceSymbol": "CME_MINI:MNQU6",
              "strategyEvidence": EVIDENCE}
    server = {key: value for key, value in SCENARIO.items() if key not in {"decisionId", "model"}}
    server["cycleCommitted"] = True
    return {"market": market, "scenario": server,
            # 建玉が開いている間、正本の display は必ず orderable=false になる。
            # 追撃はそこを通れなければならない(§7 の POSITION_OPEN 免除と対)。
            "position": {"state": "OPEN", "qty": 4, "side": "SHORT"},
            "order": {"state": "PENDING", "verified": True},
            "display": {"orderable": False, "cyclePaired": True,
                        "blockReason": "POSITION OPEN — MANAGEMENT ONLY"}}


BASE_T1 = fx.tranche_one()
BASE_ROUTE = [{"accountId": fx.ACC, "legId": item["id"], "state": "ACCEPTED",
               "orderId": item["orderId"], "receipt": item["receipt"],
               "bracketOrderIds": item["bracketOrderIds"],
               "bracketReceipts": item["bracketReceipts"]}
              for item in BASE_T1["legs"]]
BASE_PLAN = {
    "planVersion": "R19-ICT-SPLIT-1", "entryKey": "ENTRY:" + "1" * 64,
    "scenarioId": "S-T1", "fingerprint": "fp-t1", "evidenceHash": "se-t1",
    "marketCycleId": "cycle-t1", "decisionId": "S-T1",
    "accountScope": [fx.ACC], "symbol": fx.SYM, "model": "VP80_REVERSION",
    "grade": "A+", "side": "SELL", "qty": 4,
    "entry": 29484.25, "entryOrderType": "LIMIT", "entryReference": 29484.25,
    "initialStop": 29520.0, "tp1": 29440.0, "finalTarget": 29380.0,
    "targets": [29440.0, 29380.0],
    "legs": [{"id": "TP1", "qty": 2, "target": 29440.0},
             {"id": "RUNNER", "qty": 2, "target": 29380.0}],
    "trailDistance": 18.75, "mode": "SPLIT_BRACKETS_TP1_RUNNER",
    "ultra": True, "ultraDrawdown": 3100.0, "riskCapDollars": 3100.0,
    "riskCapSource": "ACCOUNT_DRAWDOWN_BUFFER", "routeSnapshot": BASE_ROUTE,
}
BASE_ROWS = []
for item in BASE_T1["legs"]:
    BASE_ROWS += fx.bracket_rows(item["orderId"], item["bracketOrderIds"])
BASE_POSITION = fx.position(qty=4, avg_entry=29484.25)
import broker_status  # noqa: E402
IDENTITY = broker_status.position_identity(BASE_POSITION)
GENERATION = "PG:1:" + str(IDENTITY)
# 建玉 identity は枚数を含まない(account/symbol/side/orderId/receipt/filledAt/initialQty)。
# 追撃で枚数だけが変わるこのフィクスチャでは世代が進まないので、束縛の再凍結が
# 割り込まず WAL の遷移そのものを観測できる(世代交代は bind_composite 側で見る)。
BASE_PLAN_OWNERSHIP = {"generation": GENERATION, "rawIdentity": IDENTITY,
                       "accountId": fx.ACC, "symbol": fx.SYM, "side": "SELL",
                       "orderId": BASE_POSITION["orderId"], "receipt": None,
                       "filledAt": BASE_POSITION["filledAt"],
                       "avgEntry": 29484.25, "initialQty": 4}
ADD_ROUTE = [{"accountId": fx.ACC, "legId": "TP1", "state": "ACCEPTED",
              "orderId": "T2-TP1", "receipt": "R:T2-TP1",
              "bracketOrderIds": ["T2-TP1-A", "T2-TP1-B"],
              "bracketReceipts": ["R:T2-TP1-A", "R:T2-TP1-B"]},
             {"accountId": fx.ACC, "legId": "RUNNER", "state": "ACCEPTED",
              "orderId": "T2-RUN", "receipt": "R:T2-RUN",
              "bracketOrderIds": ["T2-RUN-A", "T2-RUN-B"],
              "bracketReceipts": ["R:T2-RUN-A", "R:T2-RUN-B"]}]
ADD_ROWS = (fx.bracket_rows("T2-TP1", ["T2-TP1-A", "T2-TP1-B"])
            + fx.bracket_rows("T2-RUN", ["T2-RUN-A", "T2-RUN-B"]))
COMBINED = (4 * 29484.25 + 4 * ADD_PRICE) / 8


def claim_stub(scenario, **kwargs):
    import execution_intent
    py = kwargs.get("pyramid")
    add_qty = int((py or {}).get("addQty") or scenario["qty"])
    split = execution_contract.ultra_split(add_qty)
    intent = execution_intent.build(
        symbol=scenario["symbol"], side=scenario["side"], qty=add_qty,
        order_type="MARKET", entry=None, last=BUNDLE["price"], stop=scenario["stop"],
        targets=scenario["targets"],
        legs=[{"id": "TP1", "qty": split[0], "target": scenario["targets"][0]},
              {"id": "RUNNER", "qty": split[1], "target": scenario["targets"][1]}],
        plan_version=scenario["planVersion"],
        execution_contract_version=execution_contract.VERSION,
        account_scope=[fx.ACC],
        pyramid=({"baseQty": int(py["baseQty"]), "addQty": add_qty} if py else None))
    return True, {"entryKey": ae._entry_key(scenario), "claimToken": "T" * 43,
                  "executionIntent": intent,
                  "executionIntentHash": execution_intent.intent_hash(intent)}


def make_ledger(tmp):
    path = os.path.join(tmp, "r84.jsonl")
    ae._append_ledger({"key": "POSITION_GENERATION:" + fx.ACC + ":1:" + str(IDENTITY),
                       "status": "POSITION_GENERATION", "generation": 1,
                       "identity": IDENTITY, "open": True, "accountId": fx.ACC,
                       "symbol": fx.SYM, "side": "SHORT"}, path)
    ae._append_ledger({"key": BASE_PLAN["entryKey"], "entryKey": BASE_PLAN["entryKey"],
                       "status": "ENTRY_SENT", "action": "ENTRY",
                       "plan": dict(BASE_PLAN, positionOwnership=BASE_PLAN_OWNERSHIP),
                       "routeState": "SENT", "routeSnapshot": BASE_ROUTE}, path)
    return path


def rows_of(path):
    with open(path, encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def run(path, *, runner, position_fn, order_fn, cfg=None, claim=claim_stub,
        bundle=None):
    """観測は **呼ばれるたびに現在の状態** を返す。1 周期の中で再照会が何度起きても
    (束縛 → 同周期管理、コミットの settle ループ)一貫した世界を見せるため、
    固定列ではなく関数で渡す。"""
    def query(_symbol, account=None):
        return copy.deepcopy(position_fn())

    def order_query(_symbol, known_order_ids=None, account=None):
        return copy.deepcopy(order_fn())

    return ae._reconcile_one(
        bundle if bundle is not None else BUNDLE, True, dict(CFG, **(cfg or {})),
        query, runner, path, None, order_query, view, claim, None,
        fresh_price_query=lambda _symbol: ADD_PRICE)


class World:
    """送信が成立したら建玉と注文が追撃後の姿に変わる、最小の世界。"""

    def __init__(self, filled=True):
        self.sent = False
        self.filled = filled

    def position(self):
        if self.sent and self.filled:
            return fx.position(qty=8, avg_entry=COMBINED)
        return BASE_POSITION

    def orders(self):
        if self.sent and self.filled:
            return fx.orders(BASE_ROWS + ADD_ROWS)
        return fx.orders(BASE_ROWS)

    def runner(self, calls, live_ok=True, dry_ok=True):
        def run_order(args, confirm):
            calls.append((list(args), bool(confirm)))
            if not confirm:
                return (0, "stub dry-run ok") if dry_ok else (1, "ERROR: PYRAMID_DRAWDOWN_EXCEEDED")
            if not live_ok:
                return 1, "ERROR: broker rejected"
            self.sent = True
            return 0, route_envelope.format_envelope(ADD_ROUTE, "SENT")
        return run_order


def contract(**over):
    """`execution_contract.CONTRACT["pyramid"]` を一時的に差し替える。"""
    class _Ctx:
        def __enter__(self):
            self.before = copy.deepcopy(execution_contract.CONTRACT["pyramid"])
            execution_contract.CONTRACT["pyramid"].update(over)
            return self
        def __exit__(self, *_exc):
            execution_contract.CONTRACT["pyramid"].clear()
            execution_contract.CONTRACT["pyramid"].update(self.before)
    return _Ctx()


print("--- dryRun: 判定は出るが一枚も送らない ---")
tmp = tempfile.mkdtemp(prefix="nqx-r84-")
try:
    path = make_ledger(tmp)
    calls = []
    world = World()
    notes = run(path, runner=world.runner(calls),
                position_fn=world.position, order_fn=world.orders)
    rows = rows_of(path)
    dry = [row for row in rows if row["status"] == "PYRAMID_DRYRUN"]
    check("PYRAMID_DRYRUN が 1 行だけ出る", len(dry) == 1, str([r["status"] for r in rows]))
    check("dryRun 行は plan キーを持たない", "plan" not in dry[0])
    check("注記に判定が載る", any("pyramid: ADD" in note for note in notes), str(notes))
    check("order.py は一度も呼ばれない", calls == [], str(calls))
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print()
print("--- 送信 → コミット(正常系) ---")
tmp = tempfile.mkdtemp(prefix="nqx-r84-")
try:
    path = make_ledger(tmp)
    calls = []
    world = World()
    with contract(dryRun=False):
        notes = run(path, runner=world.runner(calls),
                    position_fn=world.position, order_fn=world.orders)
    rows = [row for row in rows_of(path) if row["status"] != "POSITION_GENERATION"]
    statuses = [row["status"] for row in rows]
    check("WAL は CLAIMED → SENT → ENTRY_SENT(PYRAMID_COMMIT)の順",
          statuses == ["ENTRY_SENT", "PYRAMID_CLAIMED", "PYRAMID_SENT", "ENTRY_SENT"],
          str(statuses))
    claimed = rows[1]
    sent = rows[2]
    commit = rows[3]
    check("PYRAMID_CLAIMED は plan キーを持たない(下書きは prePlan/postPlanDraft)",
          "plan" not in claimed and "prePlan" in claimed and "postPlanDraft" in claimed)
    # **claimJournal を必ず持つ。** 無いと、order.py の門で落ちた周期の claim が
    # ローカルに残らず、建玉が決済されて FLAT 経路へ落ちた瞬間に
    # 「authoritative ENTRY lock has no durable local claim journal」で恒久停止する。
    check("PYRAMID_CLAIMED は claimJournal を持つ(恒久ロックの防止)",
          isinstance(claimed.get("claimJournal"), dict)
          and claimed["claimJournal"].get("entryKey") == claimed["entryKey"]
          and claimed["claimJournal"].get("claimToken"),
          str(claimed.get("claimJournal")))
    check("PYRAMID_SENT も plan キーを持たない", "plan" not in sent)
    check("コミット行だけが plan を持つ",
          commit.get("action") == "PYRAMID_COMMIT"
          and commit["plan"]["planKind"] == tranche.PLAN_KIND)
    check("コミット行の合成プランは 2 トランシェ・期待枚数 8",
          len(commit["plan"]["tranches"]) == 2 and commit["plan"]["qty"] == 8,
          str(commit["plan"].get("qty")))
    check("合成建値は加重平均",
          abs(commit["plan"]["entry"] - tranche._tick(COMBINED)) < 1e-9,
          str(commit["plan"]["entry"]))
    check("governing stop は最新トランシェの構造 SL",
          commit["plan"]["initialStop"] == 29507.5, str(commit["plan"]["initialStop"]))
    frozen = ae._frozen_plan_record(rows, fx.SYM, account=fx.ACC)
    check("_frozen_plan_record は下書きではなく合成プランを選ぶ",
          frozen["plan"]["planKind"] == tranche.PLAN_KIND
          and frozen["action"] == "PYRAMID_COMMIT")
    check("WAL は閉じている(コミット後は transit にならない)",
          ae._pyramid_wal(rows, fx.SYM, fx.ACC) is None)
    sent_args = [args for args, confirm in calls if confirm]
    check("送信は成行・分割・ULTRA・単一口座・--pyramid",
          len(sent_args) == 1
          and "--pyramid" in sent_args[0] and "--market" in sent_args[0]
          and "--ultra" in sent_args[0] and "--split-tp" in sent_args[0]
          and f"--pyramid-base=4@{29484.25:.4f}" in sent_args[0],
          str(sent_args))
    check("送信枚数は差分(8 − 4 = 4)",
          sent_args[0][sent_args[0].index("--qty") + 1] == "4", str(sent_args))
    check("`--last` は publish された正本価格(intent hash が割れない)",
          sent_args[0][sent_args[0].index("--last") + 1] == str(ADD_PRICE))

    # 同じシグナルの二度目は何もしない(§5 の重複防止)
    calls2 = []
    with contract(dryRun=False):
        notes2 = run(path, runner=world.runner(calls2),
                     position_fn=world.position, order_fn=world.orders)
    check("同じ entryKey の追撃は二度送らない",
          all(not confirm for _args, confirm in calls2), str(calls2))
    check("合成プランの周期は composite 管理へ入る",
          any("composite" in note for note in notes2), str(notes2))
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print()
print("--- claim 拒否は HALT ではなく SKIPPED(何も送っていない) ---")
tmp = tempfile.mkdtemp(prefix="nqx-r84-")
try:
    path = make_ledger(tmp)
    calls = []
    world = World()
    with contract(dryRun=False):
        notes = run(path, runner=world.runner(calls),
                    position_fn=world.position, order_fn=world.orders,
                    claim=lambda scenario, **kwargs: (False, {"reason": "ENTRY_CLAIM_ALREADY_HELD"}))
    rows = [row for row in rows_of(path) if row["status"] != "POSITION_GENERATION"]
    statuses = [row["status"] for row in rows]
    # claim が取れていないので WAL(PYRAMID_CLAIMED)は **書かない**。解くべきものが
    # 無いのに WAL を残すと、次周期が回廊へ入ってしまう。
    check("WAL を書かずに SKIPPED だけが残り HALT も無い",
          statuses == ["ENTRY_SENT", "PYRAMID_SKIPPED"], str(statuses))
    check("理由は CLAIM_REFUSED", rows[-1]["reason"] == "CLAIM_REFUSED")
    check("WAL は閉じる(次周期に回廊へ入らない)",
          ae._pyramid_wal(rows, fx.SYM, fx.ACC) is None)
    check("order.py は一度も呼ばれない", calls == [], str(calls))
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print()
print("--- dry-run 拒否も SKIPPED(門で落ちた=何も送っていない) ---")
tmp = tempfile.mkdtemp(prefix="nqx-r84-")
try:
    path = make_ledger(tmp)
    calls = []
    world = World()
    with contract(dryRun=False):
        run(path, runner=world.runner(calls, dry_ok=False),
            position_fn=world.position, order_fn=world.orders)
    rows = [row for row in rows_of(path) if row["status"] != "POSITION_GENERATION"]
    check("PYRAMID_SKIPPED(DRY_RUN_REJECTED)で終わる",
          rows[-1]["status"] == "PYRAMID_SKIPPED"
          and rows[-1]["reason"] == "DRY_RUN_REJECTED", str(rows[-1]))
    check("live 送信は行われない",
          all(not confirm for _args, confirm in calls), str(calls))
    check("WAL は閉じる", ae._pyramid_wal(rows, fx.SYM, fx.ACC) is None)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print()
print("--- live 送信が失敗/不明なら HALT + PYRAMID_SENT(回廊が開く) ---")
tmp = tempfile.mkdtemp(prefix="nqx-r84-")
try:
    path = make_ledger(tmp)
    calls = []
    world = World()
    with contract(dryRun=False):
        notes = run(path, runner=world.runner(calls, live_ok=False),
                    position_fn=world.position, order_fn=world.orders)
    rows = [row for row in rows_of(path) if row["status"] != "POSITION_GENERATION"]
    statuses = [row["status"] for row in rows]
    check("CLAIMED → SENT → HALT",
          statuses == ["ENTRY_SENT", "PYRAMID_CLAIMED", "PYRAMID_SENT", "HALT"],
          str(statuses))
    check("HALT 行の action は dict(建玉管理の HALT と区別できる)",
          isinstance(rows[-1]["action"], dict))
    check("注記は自動再送を促さない",
          any("do not resend" in note for note in notes), str(notes))
    wal = ae._pyramid_wal(rows, fx.SYM, fx.ACC)
    check("WAL は開いたまま(次周期は回廊 or 不在の証明へ)", wal is not None)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print()
print("--- enabled=false の間は評価すらしない ---")
tmp = tempfile.mkdtemp(prefix="nqx-r84-")
try:
    path = make_ledger(tmp)
    calls = []
    world = World()
    with contract(enabled=False):
        notes = run(path, runner=world.runner(calls),
                    position_fn=world.position, order_fn=world.orders)
    rows = [row for row in rows_of(path) if row["status"] != "POSITION_GENERATION"]
    check("PYRAMID_* の行が一つも増えない",
          [row["status"] for row in rows] == ["ENTRY_SENT"], str([r["status"] for r in rows]))
    check("注記にも pyramid が出ない",
          not any("pyramid" in note for note in notes), str(notes))
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print()
print("--- 基礎が合成として読めないなら足さない(恒久回廊を作らない) ---")
# R72 の /fills 束縛で身元を取った脚は bracket id を持たない。**単一プラン経路は親行で
# 照合できるので所有は成立する**が、合成は対の身元しか見ない。そこへ足すとコミットが
# INCONSISTENT になり、不在の証明も「建玉が増えた」で通らず、恒久的に遷移回廊
# (FLATTEN のみ)へ落ちる。送る前に確かめて見送るのが正しい。


def parent_rows():
    """成行の親行が per-order 詳細で見える世界(R52 の fieldsIncomplete 行)。"""
    rows = []
    for item in BASE_T1["legs"]:
        rows.append({"orderId": item["orderId"], "accountId": fx.ACC, "symbol": fx.SYM,
                     "action": "SELL", "status": "FILLED", "receipt": item["receipt"],
                     "qty": None, "orderType": "", "limitPrice": None,
                     "parentId": None, "brokerParentId": None, "fieldsComplete": False})
    return rows


FILLS_ROUTE = [{k: v for k, v in row.items()
                if k not in ("bracketOrderIds", "bracketReceipts")}
               for row in BASE_ROUTE]
FILLS_PLAN = dict(BASE_PLAN, routeSnapshot=FILLS_ROUTE,
                  positionOwnership=BASE_PLAN_OWNERSHIP)


def fills_ledger(tmp):
    path = os.path.join(tmp, "r84f.jsonl")
    ae._append_ledger({"key": "POSITION_GENERATION:" + fx.ACC + ":1:" + str(IDENTITY),
                       "status": "POSITION_GENERATION", "generation": 1,
                       "identity": IDENTITY, "open": True, "accountId": fx.ACC,
                       "symbol": fx.SYM, "side": "SHORT"}, path)
    ae._append_ledger({"key": FILLS_PLAN["entryKey"], "entryKey": FILLS_PLAN["entryKey"],
                       "status": "ENTRY_SENT", "action": "ENTRY", "plan": FILLS_PLAN,
                       "routeState": "SENT", "routeSnapshot": FILLS_ROUTE}, path)
    return path


tmp = tempfile.mkdtemp(prefix="nqx-r84-base-")
try:
    path = fills_ledger(tmp)
    calls = []
    world = World()
    # 親行だけが見える = 対の身元が取れない。
    world.orders = lambda: fx.orders(parent_rows())
    with contract(dryRun=False):
        notes = run(path, runner=world.runner(calls),
                    position_fn=world.position, order_fn=world.orders)
    rows = [row for row in rows_of(path) if row["status"] != "POSITION_GENERATION"]
    reasons = [row.get("reason") for row in rows if row["status"] == ae.PYRAMID_SKIPPED]
    check("基礎そのものは従来どおり所有されている(単一プラン経路は無傷)",
          not any("ownership UNKNOWN" in note for note in notes), str(notes))
    check("対の身元が取れない基礎へは足さない",
          all(not confirm for _args, confirm in calls), str(calls))
    check("理由は BASE_STRUCTURE_UNVERIFIABLE",
          "BASE_STRUCTURE_UNVERIFIABLE" in reasons, str(reasons))
    check("WAL を開かない(恒久回廊にならない)",
          ae._pyramid_wal(rows, fx.SYM, fx.ACC) is None)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

tmp = tempfile.mkdtemp(prefix="nqx-r84-base2-")
try:
    path = fills_ledger(tmp)
    calls = []
    world = World()
    # 親行 + 子行が両方見える = `_attach_bracket_ids` が身元を補える。
    world.orders = lambda: fx.orders(parent_rows() + BASE_ROWS)
    with contract(dryRun=False):
        notes = run(path, runner=world.runner(calls),
                    position_fn=world.position, order_fn=world.orders)
    rows = [row for row in rows_of(path) if row["status"] != "POSITION_GENERATION"]
    check("子行から身元を補えるなら追撃は通る(門が効きすぎていない)",
          any(confirm and "--pyramid" in args for args, confirm in calls), str(notes))
    check("凍結した合成プランは補った bracket id を持つ",
          all(len(leg.get("bracketOrderIds") or []) == 2
              for row in rows if row.get("action") == ae.PYRAMID_COMMIT
              for tr in row["plan"]["tranches"] for leg in tr["legs"]),
          str([row.get("action") for row in rows]))
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print()
print("--- 追撃が例外で落ちても、建玉管理は止まらない ---")
tmp = tempfile.mkdtemp(prefix="nqx-r84-")
try:
    path = make_ledger(tmp)
    calls = []
    world = World()
    boom = ae._pyramid_consider

    def explode(*_args, **_kwargs):
        raise RuntimeError("synthetic pyramid bug")

    ae._pyramid_consider = explode
    try:
        with contract(dryRun=False):
            notes = run(path, runner=world.runner(calls),
                        position_fn=world.position, order_fn=world.orders)
    finally:
        ae._pyramid_consider = boom
    check("周期は例外で落ちず注記を返す", isinstance(notes, list) and notes, str(notes))
    check("建玉管理の注記は出たまま",
          any("autotrade hold: managed" in note for note in notes), str(notes))
    check("握った例外は黙らせず注記に出す",
          any("pyramid guard" in note and "synthetic pyramid bug" in note
              for note in notes), str(notes))
    check("何も送っていない", all(not confirm for _args, confirm in calls), str(calls))
finally:
    shutil.rmtree(tmp, ignore_errors=True)

# ---------------------------------------------------------------------------
# 同じ決定(scenarioId)の再掲では建て増さない。
#
# `decisionId`(= scenarioId)は「同じセットアップなら 3 分ごとに変わらない」ハッシュ
# (msnr_gate.decision)で、実サイクルでは同じ構造が最長 14 連続で再武装していた。
# 一方 entryKey は marketCycleId を含むので **毎周期変わる**。entryKey だけで重複を
# 見ていると、同じシグナルが再掲されるたびに建て増す —— 基礎を作ったシグナル自身でも、
# ULTRA 枚数が焼き直されて今の建玉との差が出れば足してしまう。R83 の定義は
# 「同方向の **新規** シグナル」。
# ---------------------------------------------------------------------------
ORIGINAL_SCENARIO = SCENARIO


def rearmed(**over):
    """同じ正本の別サイクル(marketCycleId だけでなく指紋も変わりうる)。"""
    return dict(ORIGINAL_SCENARIO, **over)


def run_with(scenario, path, **kwargs):
    global SCENARIO
    SCENARIO = scenario
    try:
        return run(path, bundle=dict(BUNDLE, _published_scenario=scenario), **kwargs)
    finally:
        SCENARIO = ORIGINAL_SCENARIO


print()
print("--- 基礎を作った決定そのものでは建て増さない ---")
tmp = tempfile.mkdtemp(prefix="nqx-r84-")
try:
    path = make_ledger(tmp)
    calls = []
    world = World()
    same_as_base = rearmed(scenarioId="S-T1", decisionId="S-T1",
                           marketCycleId="cycle-t1-rearmed", fingerprint="fp-t1-rearmed")
    with contract(dryRun=False):
        notes = run_with(same_as_base, path, runner=world.runner(calls),
                         position_fn=world.position, order_fn=world.orders)
    statuses = [row["status"] for row in rows_of(path)]
    check("order.py を呼ばない(同じ決定の再掲は新規シグナルではない)", calls == [], str(calls))
    check("claim も WAL も作らない", "PYRAMID_CLAIMED" not in statuses, str(statuses))
    check("建玉管理は続く", any("autotrade hold: managed" in note for note in notes), str(notes))
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print()
print("--- 一度足した決定は、次の周期に再掲されても二度目を足さない ---")
tmp = tempfile.mkdtemp(prefix="nqx-r84-")
try:
    path = make_ledger(tmp)
    calls = []
    world = World()
    with contract(dryRun=False):
        run_with(ORIGINAL_SCENARIO, path, runner=world.runner(calls),
                 position_fn=world.position, order_fn=world.orders)
    first_sends = [args for args, confirm in calls if confirm]
    check("1 周期目は足す(前提)", len(first_sends) == 1, str(calls))
    commits = [row for row in rows_of(path) if row.get("action") == ae.PYRAMID_COMMIT]
    check("1 周期目はコミットまで進む(前提)", len(commits) == 1,
          str([row["status"] for row in rows_of(path)]))

    # 2 周期目: 同じ決定が別サイクルで再掲され、ULTRA の焼き直しで目標枚数が増えた。
    calls.clear()
    again = rearmed(marketCycleId="cycle-r84-2", fingerprint="fp-r84-add-2", qty=10,
                    legs=[{"id": "TP1", "qty": 5, "target": 29400.0},
                          {"id": "RUNNER", "qty": 5, "target": 29300.0}])
    with contract(dryRun=False):
        run_with(again, path, runner=world.runner(calls),
                 position_fn=world.position, order_fn=world.orders)
    check("同じ決定では二度目を送らない", calls == [], str(calls))
    claimed = [row for row in rows_of(path) if row["status"] == "PYRAMID_CLAIMED"]
    check("WAL は 1 本のまま", len(claimed) == 1, str(len(claimed)))

    # 対照: **別の決定**なら同じ条件で足しにいく(門が常に閉じているわけではない)。
    calls.clear()
    fresh = rearmed(scenarioId="r84-add-2", decisionId="r84-add-2",
                    marketCycleId="cycle-r84-3", fingerprint="fp-r84-add-3", qty=10,
                    legs=[{"id": "TP1", "qty": 5, "target": 29400.0},
                          {"id": "RUNNER", "qty": 5, "target": 29300.0}])
    with contract(dryRun=False):
        run_with(fresh, path, runner=world.runner(calls),
                 position_fn=world.position, order_fn=world.orders)
    check("別の決定なら追撃の送信まで進む", any(confirm for _args, confirm in calls), str(calls))
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print()
print("--- dryRun の影運転も同じ規律(観測が本番の挙動を映す) ---")
tmp = tempfile.mkdtemp(prefix="nqx-r84-")
try:
    path = make_ledger(tmp)
    calls = []
    world = World()
    notes_1 = run_with(ORIGINAL_SCENARIO, path, runner=world.runner(calls),
                       position_fn=world.position, order_fn=world.orders)
    notes_2 = run_with(rearmed(marketCycleId="cycle-r84-2"), path, runner=world.runner(calls),
                       position_fn=world.position, order_fn=world.orders)
    check("1 周期目は dryRun の判定を出す", any("pyramid: ADD" in note for note in notes_1),
          str(notes_1))
    check("同じ決定の再掲では判定を出し直さない",
          not any("pyramid: ADD" in note for note in notes_2), str(notes_2))
    dry = [row for row in rows_of(path) if row["status"] == "PYRAMID_DRYRUN"]
    check("dryRun 行は決定を記録する", len(dry) == 1 and dry[0].get("scenarioId") == "r84-add",
          str([(row.get("scenarioId"), row.get("reason")) for row in dry]))

    # 別の決定の dryRun は、直前と同じ理由(PYRAMID_ADD)でも台帳から落とさない。
    notes_3 = run_with(rearmed(scenarioId="r84-add-2", decisionId="r84-add-2",
                               marketCycleId="cycle-r84-3", fingerprint="fp-r84-add-3"),
                       path, runner=world.runner(calls),
                       position_fn=world.position, order_fn=world.orders)
    dry = [row for row in rows_of(path) if row["status"] == "PYRAMID_DRYRUN"]
    check("別の決定は判定を出す", any("pyramid: ADD" in note for note in notes_3), str(notes_3))
    check("別の決定の dryRun 行は重複排除で消されない",
          [row.get("scenarioId") for row in dry] == ["r84-add", "r84-add-2"],
          str([row.get("scenarioId") for row in dry]))
    check("dryRun では一枚も送らない", all(not confirm for _args, confirm in calls), str(calls))
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print()
print(f"合計: PASS {PASS[0]} / FAIL {FAIL[0]}")
sys.exit(1 if FAIL[0] else 0)

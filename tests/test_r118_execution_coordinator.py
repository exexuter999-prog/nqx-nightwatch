# -*- coding: utf-8 -*-
"""R118: 実行 Coordinator と、模擬ブローカーによる送信側の故障注入。

**外部到達 0 件**(HTTP は 127.0.0.1 の模擬サーバだけ)。**本番ファイル書き込み 0 件**
(予算・レーン・結果ログは全部一時ディレクトリ)。

見ているもの:

1. 通信予算(プロセス横断のトークンバケツ)の算術・進行保証・共有
2. 口座レーンの直列化(同じ口座へ同時に 2 つ入らない)
3. Coordinator: 口座間の並行、口座・脚別の結果、**二重送信しない**、
   **結果不明を再送しない**、併合した envelope が再解析できる
4. 模擬ブローカーで 1 / 7 / 20 口座の ENTRY、同時 TP1、部分約定、片脚拒否、
   取消競合、応答不明、送信前後のクラッシュ、再起動
5. 口座数を変えたときの送信側の所要と HTTP 本数(実測)

    python tests/test_r118_execution_coordinator.py
"""
import io
import json
import os
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

for _stream in ("stdout", "stderr"):
    _file = getattr(sys, _stream, None)
    if _file is not None and hasattr(_file, "reconfigure"):
        try:
            _file.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

import execution_coordinator as ec   # noqa: E402
import mock_broker as mb             # noqa: E402
import route_envelope                # noqa: E402

PASS = [0]
FAIL = [0]
TMP = tempfile.mkdtemp(prefix="r118-coord-")
MEASUREMENTS = []
ORIGINS_SEEN = []


def check(label, condition, detail=""):
    if condition:
        PASS[0] += 1
        print(f"  ok   {label}")
    else:
        FAIL[0] += 1
        print(f"  FAIL {label}" + (f" — {detail}" if detail else ""))


def section(title):
    print(f"\n--- {title} ---")


class VirtualClock:
    """仮想時計。眠らずに待ちの算術だけ進める。"""

    def __init__(self):
        self.t = 0.0
        self.lock = threading.Lock()

    def __call__(self):
        return self.t

    def sleep(self, d):
        with self.lock:
            self.t += d


def budget(**kwargs):
    kwargs.setdefault("path", os.path.join(TMP, "budget.json"))
    return ec.CommsBudget(**kwargs)


# ============================================================ 1. 通信予算

section("1. 通信予算(3 req/s・溜め 20・観測へ 2 本残す)")

clock = VirtualClock()
b = budget(rate=3.0, burst=20, reserve=2, clock=clock, sleep=clock.sleep, shared=False)
check("使える溜めは burst − reserve", b.capacity == 18)

for count, expected in ((18, 0.0), (40, (40 - 18) / 3), (120, (120 - 18) / 3)):
    clock = VirtualClock()
    b = budget(rate=3.0, burst=20, reserve=2, clock=clock, sleep=clock.sleep, shared=False)
    waited = sum(b.take(1) for _ in range(count))
    check(f"{count:>3} 本の合計待ち {waited:.2f} 秒(期待 {expected:.2f})",
          abs(waited - expected) < 0.05, f"waited={waited}")

# 浮動小数で止まらないこと(丸めで「あと 1e-16 足りない」が起きる)
clock = VirtualClock()
b = budget(rate=3.0, burst=20, reserve=2, clock=clock, sleep=clock.sleep, shared=False)
started = time.monotonic()
for _ in range(500):
    b.take(1)
check("500 本を回しても収束する(丸めで生きたループにならない)",
      time.monotonic() - started < 5.0, f"{time.monotonic() - started:.1f}s")

clock = VirtualClock()
b = budget(rate=3.0, burst=20, reserve=2, clock=clock, sleep=clock.sleep, shared=False)
waited = b.take(40)
check("溜めより大きい要求は刻んで取る(全体の待ちは同じ)",
      abs(waited - (40 - 18) / 3) < 0.05, f"waited={waited}")

check("floor_sec は補充待ちの下限そのもの",
      abs(b.floor_sec(40) - (40 - 18) / 3) < 1e-6)
check("溜めの中なら待ち 0", b.floor_sec(10) == 0.0)

# プロセス横断(= 同じファイルを見る別インスタンスが枠を食い合う)
shared_path = os.path.join(TMP, "shared_budget.json")
if os.path.exists(shared_path):
    os.unlink(shared_path)
# 補充を **遅く**して曖昧さを消す(毎秒 2 本 = 1 本あたり 0.5 秒)。速い補充だと
# 6 本目と 7 本目の間のファイル I/O だけで補充が済み、待ちが観測できない。
one = ec.CommsBudget(rate=2.0, burst=4, reserve=0, path=shared_path, shared=True)
two = ec.CommsBudget(rate=2.0, burst=4, reserve=0, path=shared_path, shared=True)
for _ in range(4):
    one.take(1)
started = time.monotonic()
waited = two.take(1)
elapsed = time.monotonic() - started
check("別インスタンスは同じ枠を食う(1 本目が使い切れば 2 本目は待つ)",
      elapsed > 0.2 and waited > 0.0,
      f"実待ち {elapsed * 1000:.0f}ms / 申告 {waited * 1000:.0f}ms")
check("予算ファイルは一時ディレクトリの中(本番へ書かない)",
      os.path.dirname(shared_path) == TMP)


# ============================================================ 2. 口座レーン

section("2. 口座レーンの直列化")

lane_dir = os.path.join(TMP, "lanes")
order_seen = []


def lane_worker(account, tag):
    lane = ec.AccountLane(account, directory=lane_dir)
    with lane.hold(timeout=10.0):
        order_seen.append(("in", tag))
        time.sleep(0.05)
        order_seen.append(("out", tag))


threads = [threading.Thread(target=lane_worker, args=("SAME", i)) for i in range(4)]
for thread in threads:
    thread.start()
for thread in threads:
    thread.join()
interleaved = any(order_seen[i][0] == "in" and order_seen[i + 1][0] == "in"
                  for i in range(len(order_seen) - 1))
check("同じ口座の操作は重ならない", not interleaved, repr(order_seen))

parallel_marks = []


def other_lane(account):
    lane = ec.AccountLane(account, directory=lane_dir)
    with lane.hold(timeout=10.0):
        parallel_marks.append(("in", account, time.monotonic()))
        time.sleep(0.08)
        parallel_marks.append(("out", account, time.monotonic()))


threads = [threading.Thread(target=other_lane, args=(f"ACC{i}",)) for i in range(4)]
start = time.monotonic()
for thread in threads:
    thread.start()
for thread in threads:
    thread.join()
check("別々の口座は同時に走る",
      time.monotonic() - start < 0.25, f"{time.monotonic() - start:.2f}s")

# 持ち主が死んで残ったロックは時間で引き剥がす
stale = os.path.join(lane_dir, "STALE.lane")
os.makedirs(lane_dir, exist_ok=True)
io.open(stale, "w").write("dead")
os.utime(stale, (time.time() - ec.LANE_STALE_SEC - 10, time.time() - ec.LANE_STALE_SEC - 10))
took = [False]
with ec.AccountLane("STALE", directory=lane_dir).hold(timeout=2.0):
    took[0] = True
check("死んだ持ち主のロックは時間で引き剥がす", took[0])


# ============================================================ 3. Coordinator

section("3. Coordinator の並行・結果・二重送信の禁止")


def envelope_for(account, states=("ACCEPTED", "ACCEPTED"), seed=0):
    rows = []
    for index, (leg, state) in enumerate(zip(route_envelope.LEGS, states)):
        row = {"accountId": account, "legId": leg, "state": state}
        if state == "ACCEPTED":
            row["orderId"] = f"{account}-{seed}-{index}"
            row["receipt"] = f"r-{account}-{seed}-{index}"
        rows.append(row)
    accepted = states.count("ACCEPTED")
    rejected = states.count("REJECTED")
    unknown = states.count("UNKNOWN")
    if accepted == len(states):
        state = "SENT"
    elif rejected == len(states):
        state = "REJECTED"
    elif unknown and accepted == 0:
        state = "UNKNOWN"
    else:
        state = "PARTIAL"
    return route_envelope.format_envelope(rows, state, resolved=1)


def coordinator(runner, **kwargs):
    kwargs.setdefault("budget", budget(rate=1000.0, burst=1000, reserve=0, shared=False))
    kwargs.setdefault("lane_factory",
                      lambda account: ec.AccountLane(account, directory=lane_dir))
    kwargs.setdefault("record", lambda row: None)
    return ec.Coordinator(runner=runner, **kwargs)


calls = []
lock = threading.Lock()


def ok_runner(args, confirm):
    account = args[args.index("--accounts") + 1]
    with lock:
        calls.append((account, confirm, time.monotonic()))
    time.sleep(0.06)
    return 0, envelope_for(account)


ACCTS = [f"ACC{i}" for i in range(6)]
coord = coordinator(ok_runner, parallel=6)
started = time.monotonic()
result = coord.run(op_key="op-1", operation="ENTRY", accounts=ACCTS,
                   build_args=lambda a: ["--accounts", a], confirm=True)
elapsed = time.monotonic() - started
check("6 口座が並行に走る(逐次なら 0.36s 以上)", elapsed < 0.25, f"{elapsed:.2f}s")
check("全口座 SENT", result["state"] == "SENT", json.dumps(result["state"]))
check("口座ごとの結果が入力順で返る",
      [row["account"] for row in result["outcomes"]] == ACCTS)
check("脚ごとの結果が残る(口座 × 2 脚)",
      all(len(row["legs"]) == 2 for row in result["outcomes"]))
check("併合した envelope が再解析できる",
      route_envelope.parse(result.merged_detail, expected_accounts=ACCTS)["ok"] is True)
reparsed = route_envelope.parse(result.merged_detail, expected_accounts=ACCTS)
check("併合後の合計が 12 脚 ACCEPTED",
      reparsed["acceptedCount"] == 12 and reparsed["totalAttempts"] == 12,
      json.dumps({k: reparsed[k] for k in ("acceptedCount", "totalAttempts")}))

# 同じ opKey で二度目は送らない
before = len(calls)
again = coord.run(op_key="op-1", operation="ENTRY", accounts=ACCTS,
                  build_args=lambda a: ["--accounts", a], confirm=True)
check("同じ opKey の再実行は 1 本も送らない", len(calls) == before,
      f"{len(calls) - before} 本送った")
check("再実行は全部 SKIPPED",
      all(row["status"] == ec.SKIPPED for row in again["outcomes"]))

# 結果不明は終端(再送しない)
unknown_calls = []


def unknown_runner(args, confirm):
    account = args[args.index("--accounts") + 1]
    unknown_calls.append(account)
    if account == "ACC2":
        return 1, "ERROR: connection reset"
    return 0, envelope_for(account)


coord2 = coordinator(unknown_runner, parallel=4)
result2 = coord2.run(op_key="op-2", operation="ENTRY", accounts=ACCTS[:4],
                     build_args=lambda a: ["--accounts", a], confirm=True)
check("envelope が読めない応答は UNKNOWN", result2["unknown"] == ["ACC2"],
      repr(result2["unknown"]))
check("不明が混ざると全体は PARTIAL", result2["state"] == ec.PARTIAL, result2["state"])
check("不明の口座も 2 脚ぶん UNKNOWN として残る(落とさない)",
      route_envelope.parse(result2.merged_detail,
                           expected_accounts=ACCTS[:4])["unknownCount"] == 2)
before = len(unknown_calls)
coord2.run(op_key="op-2", operation="ENTRY", accounts=ACCTS[:4],
           build_args=lambda a: ["--accounts", a], confirm=True)
check("不明だった口座を再送しない", len(unknown_calls) == before)

# rc=0 でも envelope が SENT を名乗れなければ信じない
check("rc≠0 の SENT envelope は UNKNOWN へ倒す",
      ec._classify(1, envelope_for("X"), "X")[0] == ec.UNKNOWN)
check("envelope が無い rc=0 は UNKNOWN(送信済みと言わない)",
      ec._classify(0, "done", "X")[0] == ec.UNKNOWN)
check("REJECTED は REJECTED のまま",
      ec._classify(1, envelope_for("X", ("REJECTED", "REJECTED")), "X")[0] == ec.REJECTED)

# 予算を共有する: レーンが増えても合計は上限を超えない
clock = VirtualClock()
paced = ec.Coordinator(
    runner=lambda args, confirm: (0, envelope_for(args[args.index("--accounts") + 1])),
    budget=budget(rate=3.0, burst=20, reserve=2, clock=clock, sleep=clock.sleep,
                  shared=False),
    parallel=1,
    lane_factory=lambda account: ec.AccountLane(account, directory=lane_dir, shared=False),
    clock=clock, record=lambda row: None)
twenty = [f"P{i}" for i in range(20)]
paced_result = paced.run(op_key="op-3", operation="ENTRY", accounts=twenty,
                         build_args=lambda a: ["--accounts", a], confirm=True)
expected_wait = (20 * ec.budget_units("ENTRY") - 18) / 3.0
check(f"20 口座 × {ec.budget_units('ENTRY')} 枠の補充待ちは "
      f"{paced_result['budgetWaitedSec']:.2f} 秒(算術 {expected_wait:.2f})",
      abs(paced_result["budgetWaitedSec"] - expected_wait) < 0.2,
      json.dumps(paced_result["budgetWaitedSec"]))

# 上限の適用範囲は **読みごとに分けて**出す(推測を 1 つの数に潰さない)
table = ec.floor_table(20)
check("送信が予算内かは未確認のまま(推測で埋めない)",
      ec.send_counts_against_budget() is None)
check("Coordinator は未確認なら安全側(送信も枠を確保する)",
      ec.budget_units("ENTRY") == ec.per_account_requests("ENTRY", "all"),
      f"{ec.budget_units('ENTRY')} vs {ec.per_account_requests('ENTRY', 'all')}")
check(f"N=20 の下限: REST {table['restSendCounted']:.1f}s / "
      f"Gateway {table['gatewaySendCounted']:.1f}s(送信も予算内という読み)",
      table["gatewaySendCounted"] < table["restSendCounted"],
      json.dumps(table))
check(f"N=20 の下限: 送信が枠外なら Gateway で {table['gatewaySendFree']:.2f}s(5 秒を切る)",
      table["gatewaySendFree"] < 5.0, json.dumps(table))
check("Gateway でも送信が予算内なら 5 秒目標に届かない",
      table["gatewaySendCounted"] > 5.0, json.dumps(table))
check("Gateway 併用でも api は 0 にならない(約定履歴の窓が REST に残る)",
      ec.per_account_requests("ENTRY", "gatewayApi") == 1,
      repr(ec.per_account_requests("ENTRY", "gatewayApi")))

# 事前検査で止めた口座は「送っていない」
blocked = coordinator(ok_runner, parallel=2, pre_check=lambda a: "not flat" if a == "ACC1" else None)
result3 = blocked.run(op_key="op-4", operation="ENTRY", accounts=["ACC0", "ACC1"],
                      build_args=lambda a: ["--accounts", a], confirm=True)
statuses = {row["account"]: row["status"] for row in result3["outcomes"]}
check("事前検査で落ちた口座は SKIPPED(送信していない)",
      statuses["ACC1"] == ec.SKIPPED and statuses["ACC0"] == ec.SENT, json.dumps(statuses))


# ============================================================ 3b. 管理経路の並行化

section("3b. engine の口座別管理をレーンで回す(既定は逐次)")

import autotrade_engine as ae  # noqa: E402

MANAGE = ["M0", "M1", "M2", "M3"]


def slow_manage(account):
    time.sleep(0.05)
    return [f"managed {account}"]


saved_env = os.environ.get("NQX_COORDINATOR")
# **本番の .secrets へ書かせない。** 予算とレーンを一時ディレクトリへ向ける。
# 予算は潤沢にしておく(ここで見たいのは並行して走るかどうかで、絞りの算術は §1)。
ae.MANAGE_BUDGET = budget(rate=1000.0, burst=1000, reserve=0, shared=False)
ae.MANAGE_LANE_DIR = lane_dir
try:
    os.environ["NQX_COORDINATOR"] = "0"
    started = time.monotonic()
    out = ae._manage_accounts_parallel(MANAGE, slow_manage)
    serial_sec = time.monotonic() - started
    check("既定(coordinator 無効)は逐次で従来どおり",
          serial_sec >= 0.18 and [out[a][0] for a in MANAGE]
          == [f"managed {a}" for a in MANAGE], f"{serial_sec:.2f}s")

    os.environ["NQX_COORDINATOR"] = "1"
    os.environ["NQX_COORDINATOR_PARALLEL"] = "4"
    started = time.monotonic()
    out = ae._manage_accounts_parallel(MANAGE, slow_manage)
    parallel_sec = time.monotonic() - started
    check(f"有効にすると並行に走る({serial_sec:.2f}s → {parallel_sec:.2f}s)",
          parallel_sec < serial_sec * 0.7, f"{parallel_sec:.2f}s")
    check("戻り値は口座ごとに揃う(入力順は呼び出し側が保つ)",
          set(out) == set(MANAGE) and all(out[a] == [f"managed {a}"] for a in MANAGE))

    # 1 口座の事故で他の口座の管理を止めない
    def boom(account):
        if account == "M1":
            raise RuntimeError("broker exploded")
        return [f"managed {account}"]

    out = ae._manage_accounts_parallel(MANAGE, boom)
    check("1 口座が落ちても他の口座は回る",
          out["M0"] == ["managed M0"] and out["M3"] == ["managed M3"])
    check("落ちた口座は HALT として注記に残る(黙って消さない)",
          out["M1"] and "AUTOTRADE HALT" in out["M1"][0] and "broker exploded" in out["M1"][0],
          repr(out["M1"]))

    # 同じ口座へ同時に 2 つ入らない(レーンは engine と Coordinator で共通)
    concurrent = []
    lock = threading.Lock()

    def watcher(account):
        with lock:
            concurrent.append(("in", account))
        time.sleep(0.03)
        with lock:
            concurrent.append(("out", account))
        return [account]

    ae._manage_accounts_parallel(["SAME", "SAME"], watcher)
    same_overlap = any(concurrent[i][0] == "in" and concurrent[i + 1][0] == "in"
                       and concurrent[i][1] == concurrent[i + 1][1]
                       for i in range(len(concurrent) - 1))
    check("同じ口座は重ならない", not same_overlap, repr(concurrent))
finally:
    if saved_env is None:
        os.environ.pop("NQX_COORDINATOR", None)
    else:
        os.environ["NQX_COORDINATOR"] = saved_env
    os.environ.pop("NQX_COORDINATOR_PARALLEL", None)
    ae.MANAGE_BUDGET = None
    ae.MANAGE_LANE_DIR = None

# 予算は **効く**。20 口座 × MODIFY 5 枠 = 100 枠は溜め 18 を超えるので必ず待つ。
_clock = VirtualClock()
ae.MANAGE_BUDGET = budget(rate=3.0, burst=20, reserve=2, clock=_clock,
                          sleep=_clock.sleep, shared=False)
ae.MANAGE_LANE_DIR = lane_dir
os.environ["NQX_COORDINATOR"] = "1"
try:
    ae._manage_accounts_parallel([f"B{i}" for i in range(20)], lambda a: [a])
finally:
    ae.MANAGE_BUDGET = None
    ae.MANAGE_LANE_DIR = None
    os.environ.pop("NQX_COORDINATOR", None)
expected = (20 * ec.budget_units("MODIFY") - 18) / 3.0
check(f"管理も同じ予算を食う(20 口座で仮想 {_clock.t:.1f} 秒待つ・算術 {expected:.1f})",
      abs(_clock.t - expected) < 0.5, f"t={_clock.t}")

check("台帳の追記に錠がある(並行で行が混ざらない)",
      isinstance(getattr(ae, "_LEDGER_WRITE_LOCK", None), type(threading.Lock())))


# ============================================================ 4. 模擬ブローカー

section("4. 模擬ブローカーでの ENTRY(実 HTTP・subprocess 境界なし)")

import broker_status as bs  # noqa: E402
import order                # noqa: E402


class Harness:
    """模擬ブローカー + `order.py` の送信ループを実際に回す足場。"""

    def __init__(self, count, *, lanes=1, symbol="MNQZ6"):
        self.accounts = [f"MOCK{i:02d}" for i in range(count)]
        self.broker = mb.MockBroker(self.accounts, symbol=symbol)
        self.server = mb.MockBrokerServer(self.broker)
        self.symbol = symbol
        self.lanes = lanes

    def __enter__(self):
        self.server.__enter__()
        ORIGINS_SEEN.append(self.server.origin)
        env = mb.env_for(self.server, self.accounts, symbol=self.symbol)
        self.env_path = os.path.join(TMP, f"env-{len(self.accounts)}-{self.lanes}.env")
        with io.open(self.env_path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(f"{k}={v}" for k, v in env.items()))
        self.saved = (bs.CROSSTRADE_ENV, bs.DEFAULT_SYMBOL,
                      os.environ.get("NQX_COORDINATOR_LANES"),
                      order.IDENTITY_SETTLE_ATTEMPTS, order.IDENTITY_SETTLE_DELAY_SEC)
        bs.CROSSTRADE_ENV = self.env_path
        bs.DEFAULT_SYMBOL = self.symbol
        os.environ["NQX_COORDINATOR_LANES"] = str(self.lanes)
        # 試験では identity が即座に見えるので、本番の待ちは詰める
        order.IDENTITY_SETTLE_ATTEMPTS = 2
        order.IDENTITY_SETTLE_DELAY_SEC = 0.01
        self.cfg = dict(env)
        return self

    def __exit__(self, *_exc):
        (bs.CROSSTRADE_ENV, bs.DEFAULT_SYMBOL, lanes,
         order.IDENTITY_SETTLE_ATTEMPTS, order.IDENTITY_SETTLE_DELAY_SEC) = self.saved
        if lanes is None:
            os.environ.pop("NQX_COORDINATOR_LANES", None)
        else:
            os.environ["NQX_COORDINATOR_LANES"] = lanes
        self.server.__exit__()
        bs.invalidate_read_cache()

    def split_lines(self, side="SELL", qty=1, sl=29900.0, targets=(29700.0, 29650.0)):
        def build(account):
            rows = []
            for leg, target in zip(("TP1", "RUNNER"), targets):
                rows.append((leg, [
                    f"key={self.cfg['CROSSTRADE_KEY']};",
                    f"destination={self.cfg['CROSSTRADE_DEST']};",
                    "command=PLACE;",
                    f"account={account};",
                    f"instrument={self.symbol};",
                    f"action={side};",
                    f"qty={qty};",
                    "order_type=MARKET;",
                    "tif=DAY;",
                    f"stop_loss={sl:.2f};",
                    f"take_profit={target:.2f};",
                ]))
            return rows
        return build

    def send(self, **kwargs):
        """`order.py` の送信ループをそのまま回す。

        identity の束縛は本番と同じ **窓方式** —— 送信の前後で口座の注文一覧を
        比べ、その一回で現れた行だけを束縛する。拒否された脚では新しい行が
        出ないので束縛できず、HTTP の結果で分類される(これが正しい挙動)。
        """
        identities = {}
        side = kwargs.get("side", "SELL")
        windows = {account: {row["orderId"]
                             for row in ((order._orders_snapshot(self.symbol, account=account)
                                          or {}).get("activeOrders") or [])}
                   for account in self.accounts}
        window_lock = threading.Lock()

        def probe(account, leg=None):
            view = order._orders_snapshot(self.symbol, account=account)
            if not view:
                return None
            rows = {row["orderId"]: row for row in view.get("activeOrders") or []}
            with window_lock:
                fresh = [rows[oid] for oid in rows if oid not in windows[account]]
                windows[account] |= set(rows)
            # 親(我々が出した向きの行)は 1 本だけのはず。2 本以上なら推測しない。
            parents = [row for row in fresh if str(row.get("action") or "").upper() == side]
            if len(parents) != 1:
                return None
            return {"orderId": parents[0]["orderId"], "receipt": parents[0]["receipt"]}

        started = time.monotonic()
        quiet = io.StringIO()
        saved_stdout = sys.stdout
        sys.stdout = quiet
        try:
            ok, results = order.post_split_to_accounts(
                self.cfg, self.accounts, self.split_lines(**kwargs), "split-place",
                identity_probe=probe, identities=identities)
        finally:
            sys.stdout = saved_stdout
        elapsed = time.monotonic() - started
        route = order.classify_route_results(results, split=True, identities=identities)
        snapshot = order.build_route_snapshot(results, split=True, identities=identities)
        return {"ok": ok, "route": route, "snapshot": snapshot,
                "elapsed": elapsed, "results": results}


with Harness(3) as h:
    outcome = h.send()
    check("3 口座 × 2 脚が全部 ACCEPTED",
          outcome["route"]["state"] == "SENT" and outcome["route"]["acceptedCount"] == 6,
          json.dumps(outcome["route"]))
    check("送信は 6 本(口座 × 脚)", h.broker.count("^/$") == 6 or len(h.broker.sends) == 6,
          f"sends={len(h.broker.sends)}")
    check("脚ごとに別の注文 ID と receipt が付く",
          len({row["orderId"] for row in outcome["snapshot"]}) == 6,
          repr([row["orderId"] for row in outcome["snapshot"]]))
    check("模擬ブローカー側にも 6 本の ENTRY 行が残る",
          sum(1 for account in h.accounts
              for row in h.broker.state[account]["orders"].values()
              if row["purpose"] == "ENTRY") == 6)


section("4b. 片脚拒否 / 応答不明 / 送信前後のクラッシュ")

with Harness(3) as h:
    h.broker.add_fault(mb.Fault(kind="reject", account="MOCK01", operation="PLACE", nth=2))
    outcome = h.send()
    states = {(row["accountId"], row["legId"]): row["state"] for row in outcome["snapshot"]}
    check("片脚拒否は PARTIAL(全体成功にしない)",
          outcome["route"]["state"] == "PARTIAL", json.dumps(outcome["route"]))
    check("拒否された脚だけが ACCEPTED 以外",
          sum(1 for value in states.values() if value != "ACCEPTED") == 1,
          repr(states))
    check("post_split_to_accounts は False を返す(呼び出し側は HALT へ)",
          outcome["ok"] is False)

with Harness(2) as h:
    h.broker.add_fault(mb.Fault(kind="unknown", account="MOCK01", operation="PLACE", nth=1))
    outcome = h.send()
    # R23/R72 の規律: **ブローカーの行が応答より強い**。返事が来なくても、その一回で
    # 現れた注文行を束縛できたなら ACCEPTED として所有する —— 所有しないと建玉が
    # 管理外(裸)になる。REJECTED と呼ばないことが要点で、ACCEPTED は誤りではない。
    states = [row["state"] for row in outcome["snapshot"]]
    check("応答不明でも、行が出た脚は所有する(裸にしない)",
          "REJECTED" not in states, repr(states))
    check("応答が返らなくてもブローカー側では成立している(= 再送してはいけない)",
          len(h.broker.sends) == 4
          and sum(1 for row in h.broker.state["MOCK01"]["orders"].values()
                  if row["purpose"] == "ENTRY") == 2,
          f"sends={len(h.broker.sends)}")
    check("送信は 1 脚 1 回きり(不明でも二度打たない)",
          len([row for row in h.broker.sends if row["account"] == "MOCK01"]) == 2)

with Harness(2) as h:
    h.broker.add_fault(mb.Fault(kind="crash_before", account="MOCK01",
                                operation="PLACE", nth=1))
    outcome = h.send()
    check("送信前のクラッシュはブローカー側に何も作らない",
          sum(1 for row in h.broker.state["MOCK01"]["orders"].values()
              if row["purpose"] == "ENTRY") == 1,
          repr([row["purpose"] for row in h.broker.state["MOCK01"]["orders"].values()]))
    check("送信前クラッシュも全体成功にしない", outcome["ok"] is False)

with Harness(2) as h:
    h.broker.add_fault(mb.Fault(kind="crash_after", account="MOCK00",
                                operation="PLACE", nth=1))
    outcome = h.send()
    check("送信後のクラッシュでも注文は **ブローカー側に残る**",
          sum(1 for row in h.broker.state["MOCK00"]["orders"].values()
              if row["purpose"] == "ENTRY") == 2)
    check("残った注文は所有される(REJECTED と呼ばない)",
          "REJECTED" not in [row["state"] for row in outcome["snapshot"]],
          repr([row["state"] for row in outcome["snapshot"]]))
    check("送信後クラッシュでも二度打たない",
          len([row for row in h.broker.sends if row["account"] == "MOCK00"]) == 2)

with Harness(2) as h:
    h.broker.add_fault(mb.Fault(kind="http", status=429, account="MOCK01",
                                operation="PLACE", nth=1))
    outcome = h.send()
    check("429 は REJECTED(不明ではない。明示の応答があった)",
          any(row["state"] == "REJECTED" for row in outcome["snapshot"]),
          json.dumps(outcome["route"]))


section("4c. 部分約定・同時 TP1・取消競合・再起動")

with Harness(3) as h:
    h.send()
    # 部分約定: 口座 0 は TP1 脚だけ約定
    h.broker.fill_entry("MOCK00", "SELL", 1, 29850.0)
    position = bs.query_position(h.symbol, account="MOCK00")
    check("部分約定は 1 枚として読める",
          position["verified"] is True and position["qty"] == 1, json.dumps(position))
    others = bs.query_position(h.symbol, account="MOCK01")
    check("他の口座は FLAT のまま(混ざらない)", others["qty"] == 0)

with Harness(7) as h:
    h.send()
    for account in h.accounts:
        h.broker.fill_entry(account, "SELL", 2, 29850.0)
    # 同時 TP1: 全口座で利確側が同時に約定する
    fired = []
    for account in h.accounts:
        live = [(oid, row) for oid, row in h.broker.state[account]["orders"].items()
                if row["purpose"] == "PROTECTIVE" and row["orderType"] == "LIMIT"
                and row["ordStatus"] == mb.ACTIVE]
        h.broker.fill_protective(account, live[0][0], 29700.0)
        fired.append(account)
    check("7 口座で同時 TP1 が成立する", len(fired) == 7)
    remaining = {account: bs.query_position(h.symbol, account=account)["qty"]
                 for account in h.accounts}
    check("TP1 後は全口座が runner 1 枚",
          set(remaining.values()) == {1}, json.dumps(remaining))
    siblings = {account: len(h.broker.protective_ids(account)) for account in h.accounts}
    check("TP1 が落ちた組の SL は取り消され、runner 側の組は生き残る(4 本 → 2 本)",
          set(siblings.values()) == {2}, json.dumps(siblings))

with Harness(2) as h:
    h.send()
    h.broker.fill_entry("MOCK00", "SELL", 2, 29850.0)
    protective = h.broker.protective_ids("MOCK00")
    # 取消競合: 張り替えの最中に TP1 が約定する
    h.broker.fill_protective("MOCK00", [oid for oid in protective
                                        if h.broker.state["MOCK00"]["orders"][oid]["orderType"]
                                        == "LIMIT"][0], 29700.0)
    h.broker.place({"account": "MOCK00", "command": "cancelandbracket", "action": "SELL",
                    "qty": "1", "stop_loss": "29850", "take_profit": "29650"})
    view = bs.query_orders(h.symbol, account="MOCK00")
    check("取消競合の後も保護は 1 組(2 本)だけ",
          view["openCount"] == 2, json.dumps({"open": view["openCount"]}))
    check("建玉は runner 1 枚のまま",
          bs.query_position(h.symbol, account="MOCK00")["qty"] == 1)

with Harness(2) as h:
    h.send()
    h.broker.fill_entry("MOCK00", "SELL", 2, 29850.0)
    before = bs.query_orders(h.symbol, account="MOCK00")
    # 再起動: 読む側のプロセスが落ちて、共有も全部捨てた状態から読み直す
    bs.invalidate_read_cache()
    after = bs.query_orders(h.symbol, account="MOCK00")
    check("再起動しても同じ注文 ID が読める(状態はブローカーが持つ)",
          {row["orderId"] for row in before["activeOrders"]}
          == {row["orderId"] for row in after["activeOrders"]})
    check("再起動後も建玉は verified",
          bs.query_position(h.symbol, account="MOCK00")["verified"] is True)


# ============================================================ 4d. 20 口座の通し

section("4d. 20 口座: ENTRY → 同時 TP1 → 保護変更(runner の建値寄せ)まで")

with Harness(20) as h:
    outcome = h.send()
    check("20 口座 × 2 脚 = 40 脚が全部 ACCEPTED",
          outcome["route"]["state"] == "SENT" and outcome["route"]["acceptedCount"] == 40,
          json.dumps(outcome["route"]))

    for account in h.accounts:
        h.broker.fill_entry(account, "SELL", 2, 29850.0)
    positions = {a: bs.query_position(h.symbol, account=a) for a in h.accounts}
    check("20 口座すべてが SHORT 2 枚",
          all(p["verified"] and p["qty"] == 2 and p["side"] == "SHORT"
              for p in positions.values()),
          json.dumps({a: p.get("qty") for a, p in list(positions.items())[:3]}))

    # 同時 TP1: 全口座で利確側が同時に約定
    tp1_at = time.monotonic()
    for account in h.accounts:
        limits = [oid for oid, row in h.broker.state[account]["orders"].items()
                  if row["purpose"] == "PROTECTIVE" and row["orderType"] == "LIMIT"
                  and row["ordStatus"] == mb.ACTIVE]
        h.broker.fill_protective(account, limits[0], 29700.0)
    check("20 口座で同時 TP1 が成立し、全口座 runner 1 枚",
          all(bs.query_position(h.symbol, account=a)["qty"] == 1 for a in h.accounts))

    # TP1 後の保護変更: runner の SL を建値へ寄せる(order.py と同じ命令)
    changed = []
    for account in h.accounts:
        before = set(h.broker.protective_ids(account))
        h.broker.place({"account": account, "command": "cancelandbracket",
                        "action": "SELL", "qty": "1",
                        "stop_loss": "29850.00", "take_profit": "29650.00"})
        after = set(h.broker.protective_ids(account))
        changed.append((account, before, after))
    check("20 口座すべてで保護が張り替わった(古い組は消え、新しい組が 2 本)",
          all(len(after) == 2 and not (before & after) for _a, before, after in changed),
          json.dumps([[len(b), len(a)] for _x, b, a in changed[:3]]))

    views = {a: bs.query_orders(h.symbol, account=a) for a in h.accounts}
    check("張り替え後、どの口座も生きた保護は 1 組(2 本)だけ",
          all(v["openCount"] == 2 for v in views.values()),
          json.dumps({a: v["openCount"] for a, v in list(views.items())[:5]}))
    check("R80 の OCO 兄弟判定が 20 口座すべてで成立する",
          all(bs.oco_sibling_pair(views[a]["activeOrders"], account=a,
                                  expected_action="BUY", symbol=h.symbol) is not None
              for a in h.accounts),
          repr([a for a in h.accounts
                if bs.oco_sibling_pair(views[a]["activeOrders"], account=a,
                                       expected_action="BUY", symbol=h.symbol) is None]))
    check("建玉は runner 1 枚のまま(張り替えで枚数が変わらない)",
          all(bs.query_position(h.symbol, account=a)["qty"] == 1 for a in h.accounts))

    # 1 口座だけ張り替えが片脚拒否された場合、他の 19 口座は無事
    h.broker.add_fault(mb.Fault(kind="reject", account=h.accounts[3],
                                operation="CANCELANDBRACKET", nth=1))
    result = h.broker.place({"account": h.accounts[3], "command": "cancelandbracket",
                             "action": "SELL", "qty": "1",
                             "stop_loss": "29840.00", "take_profit": "29640.00"})
    check("拒否された口座の保護はそのまま(裸にならない)",
          len(h.broker.protective_ids(h.accounts[3])) == 2,
          repr(len(h.broker.protective_ids(h.accounts[3]))))
    check("他の 19 口座は影響を受けない",
          all(len(h.broker.protective_ids(a)) == 2
              for a in h.accounts if a != h.accounts[3]))


# ============================================================ 5. 口座数の実測

section("5. 口座数を変えたときの送信側(実測・模擬ブローカー)")

# **往復時間を明示して測る。** 0ms は構造(本数と直列性)だけを見る値で、
# 並行化の効きは往復時間に支配される。本番の CrossTrade は 1 本 0.6 秒前後
# (2026-09-19 実測)なので、150ms は **控えめな**代用値である。
for latency in (0.0, 0.15):
    print()
    print(f"  1 リクエストの往復 {latency * 1000:.0f}ms")
    print(f"  {'N':>3}  {'レーン':>5}  {'所要(秒)':>9}  {'HTTP':>6}  {'内 送信':>7}  {'1 口座':>7}")
    for count in (1, 7, 20):
        for lanes in (1, 6):
            with Harness(count, lanes=lanes) as h:
                h.broker.latency_sec = latency
                h.broker.reset_counts()
                outcome = h.send()
                total = len(h.broker.requests)
                MEASUREMENTS.append({
                    "accounts": count, "lanes": lanes, "latencyMs": int(latency * 1000),
                    "seconds": round(outcome["elapsed"], 3), "http": total,
                    "sends": len(h.broker.sends), "perAccount": round(total / count, 2),
                    "state": outcome["route"]["state"]})
                print(f"  {count:>3}  {lanes:>5}  {outcome['elapsed']:>9.3f}  "
                      f"{total:>6}  {len(h.broker.sends):>7}  {total / count:>7.2f}")
                check(f"往復 {int(latency * 1000)}ms / N={count:<2} lanes={lanes} は全脚 ACCEPTED",
                      outcome["route"]["state"] == "SENT", json.dumps(outcome["route"]))


def measurement(accounts, lanes, latency_ms):
    return next(r for r in MEASUREMENTS if r["accounts"] == accounts
                and r["lanes"] == lanes and r["latencyMs"] == latency_ms)


check("1 口座あたりの HTTP は口座数によらず一定(5 本)",
      {r["perAccount"] for r in MEASUREMENTS} == {5.0},
      repr(sorted({r["perAccount"] for r in MEASUREMENTS})))
check("送信本数は必ず 口座 × 2 脚(並行でもレーン数でも増えない)",
      all(r["sends"] == r["accounts"] * 2 for r in MEASUREMENTS))

serial = measurement(20, 1, 150)
parallel = measurement(20, 6, 150)
check(f"往復 150ms・20 口座: 逐次 {serial['seconds']:.2f}s → 6 レーン "
      f"{parallel['seconds']:.2f}s({serial['seconds'] / parallel['seconds']:.1f} 倍)",
      parallel["seconds"] < serial["seconds"] * 0.5,
      json.dumps({"serial": serial["seconds"], "parallel": parallel["seconds"]}))

zero_serial = measurement(20, 1, 0)
zero_parallel = measurement(20, 6, 0)
zero_gain = zero_serial["seconds"] / max(zero_parallel["seconds"], 1e-6)
latency_gain = serial["seconds"] / max(parallel["seconds"], 1e-6)
# **往復 0ms の値は検査にしない。報告だけする。**
#
# 待ち時間が無い測定はスケジューリングの雑音に支配される —— 同じ機械で他の試験や
# 持続負荷が動いていると、0ms でも「待ち」が生まれて並行化が効いてしまう。実際、
# 「0ms では得が無い」も「得は往復時間とともに伸びる」も、**どちらも負荷次第で
# 崩れた**(2026-09-19、3 回中 1 回)。断定できるのは往復 150ms 側の比だけで、
# そこは上の検査が押さえている。0ms の数字は「構造(本数と直列性)だけを見た値」
# として表に残す。
print(f"  参考(検査にしない): 往復 0ms の並行化 {zero_gain:.1f} 倍 / "
      f"150ms の並行化 {latency_gain:.1f} 倍")

with io.open(os.path.join(TMP, "measurements.json"), "w", encoding="utf-8") as fh:
    json.dump(MEASUREMENTS, fh, ensure_ascii=False, indent=1)
print()
print("  実測値: " + os.path.join(TMP, "measurements.json"))


# ============================================================ 6. 送信検証の Gateway 化

section("6. 送信前後の検証を Gateway から取ると照会が何本になるか")

import broker_gateway as gw       # noqa: E402
import broker_source as bsrc      # noqa: E402
import execution_contract as ec_contract  # noqa: E402


def gateway_snapshot_from(broker, path, accounts):
    """模擬ブローカーの現状から Gateway のスナップショットを作る。

    本番では押し込み(`orderUpdate` / `positionUpdate`)がこれを更新する。
    ここでは「押し込みが届いた直後」を作って同じ状態にする。
    """
    state = gw.GatewayState(expected_accounts=accounts, contract_ids=[mb.CONTRACT_ID])
    now = time.monotonic()
    state.begin_resync("test", now)
    state.apply_full_positions(broker.ws_positions(), now, epoch=int(time.time() * 1000))
    state.apply_full_orders(broker.ws_orders(), now, epoch=int(time.time() * 1000))
    gw.write_snapshot(state, time.time(), path)
    bsrc.invalidate_snapshot()
    return state


def count_entry(count, *, gateway):
    """ENTRY 1 回の HTTP を段階ごとに数える。`gateway=True` で押し込みから検証する。"""
    with Harness(count) as h:
        snap = os.path.join(TMP, f"gw-{count}-{int(gateway)}.json")
        saved_path, saved_mode = gw.snapshot_path, os.environ.get("NQX_GATEWAY_MODE")
        saved_contract = ec_contract.CONTRACT.get("gateway", {}).get("sendVerification")
        gw.snapshot_path = lambda: snap
        try:
            if gateway:
                os.environ["NQX_GATEWAY_MODE"] = "LIVE"
                ec_contract.CONTRACT.setdefault("gateway", {})["sendVerification"] = True
                gateway_snapshot_from(h.broker, snap, h.accounts)
            else:
                os.environ["NQX_GATEWAY_MODE"] = "OFF"
                ec_contract.CONTRACT.setdefault("gateway", {})["sendVerification"] = False

            h.broker.reset_counts()
            order.require_verified_flat(h.symbol, accounts=h.accounts)
            verify = len(h.broker.requests)

            h.broker.reset_counts()
            windows = {a: order._orders_snapshot(h.symbol, account=a) for a in h.accounts}
            window = len(h.broker.requests)

            identities = {}
            seen = {a: {r["orderId"] for r in ((windows[a] or {}).get("activeOrders") or [])}
                    for a in h.accounts}

            def probe(account, leg=None):
                if gateway:
                    # 押し込みが届いた = スナップショットが更新された、を作る
                    gateway_snapshot_from(h.broker, snap, h.accounts)
                view = order._orders_snapshot(h.symbol, account=account)
                if not view:
                    return None
                rows = {r["orderId"]: r for r in view.get("activeOrders") or []}
                fresh = [rows[o] for o in rows if o not in seen[account]]
                seen[account] |= set(rows)
                parents = [r for r in fresh if str(r.get("action") or "").upper() == "SELL"]
                if len(parents) != 1:
                    return None
                return {"orderId": parents[0]["orderId"], "receipt": parents[0]["receipt"]}

            h.broker.reset_counts()
            quiet, stdout = io.StringIO(), sys.stdout
            sys.stdout = quiet
            try:
                ok, results = order.post_split_to_accounts(
                    h.cfg, h.accounts, h.split_lines(), "split-place",
                    identity_probe=probe, identities=identities)
            finally:
                sys.stdout = stdout
            loop = len(h.broker.requests)
            sends = len(h.broker.sends)
            route = order.classify_route_results(results, split=True, identities=identities)
            return {"verify": verify, "window": window, "loop": loop, "sends": sends,
                    "api": verify + window + loop - sends, "state": route["state"],
                    "accepted": route["acceptedCount"]}
        finally:
            gw.snapshot_path = saved_path
            if saved_mode is None:
                os.environ.pop("NQX_GATEWAY_MODE", None)
            else:
                os.environ["NQX_GATEWAY_MODE"] = saved_mode
            ec_contract.CONTRACT.setdefault("gateway", {})["sendVerification"] = saved_contract
            bsrc.invalidate_snapshot()


# **本番契約の値そのものを縛らない。** ここで見たいのは「試験が契約を書き換えっぱなしに
# していないこと」で、`sendVerification` が false かどうかではない(2026-09-20 に本番で
# true へ切り替えたので、値を固定すると運用の決定で試験が落ちる)。
BASELINE_SEND_VERIFICATION = ec_contract.CONTRACT.get("gateway", {}).get("sendVerification")

rest3 = count_entry(3, gateway=False)
gw3 = count_entry(3, gateway=True)
print(f"  {'経路':<10}{'FLAT検証':>9}{'送信前の窓':>11}{'送信ループ':>11}{'送信':>7}"
      f"{'照会 api':>9}{'1口座api':>9}")
for label, row in (("REST", rest3), ("Gateway", gw3)):
    print(f"  {label:<10}{row['verify']:>9}{row['window']:>11}{row['loop']:>11}"
          f"{row['sends']:>7}{row['api']:>9}{row['api'] / 3:>9.2f}")

check("REST 経路は全脚 ACCEPTED", rest3["state"] == "SENT" and rest3["accepted"] == 6)
check("Gateway 経路でも全脚 ACCEPTED(identity が押し込みから束縛できる)",
      gw3["state"] == "SENT" and gw3["accepted"] == 6, json.dumps(gw3))
check("送信本数は同じ(検証の出所を変えても送信は変わらない)",
      rest3["sends"] == gw3["sends"] == 6)
check(f"照会が {rest3['api']} 本 → {gw3['api']} 本へ減る",
      gw3["api"] < rest3["api"], json.dumps({"rest": rest3["api"], "gateway": gw3["api"]}))
check("試験が本番契約の sendVerification を元へ戻している(書き換えっぱなしにしない)",
      ec_contract.CONTRACT.get("gateway", {}).get("sendVerification")
      is BASELINE_SEND_VERIFICATION,
      repr(ec_contract.CONTRACT.get("gateway", {}).get("sendVerification")))

per_account_gateway = gw3["api"] / 3
floor20 = (20 * (per_account_gateway + 2) - 18) / 3.0
print()
print(f"  1 口座 api {rest3['api'] / 3:.2f} → {per_account_gateway:.2f} 本")
print(f"  N=20 の補充待ちの下限(送信も予算内という読み): "
      f"{(20 * (rest3['api'] / 3 + 2) - 18) / 3.0:.1f} 秒 → {floor20:.1f} 秒")


# ============================================================ 7. 本番へ書いていないか

section("7. 本番ファイルへ 1 バイトも書いていない")

PRODUCTION = [ec.BUDGET_FILE, ec.BUDGET_FILE + ".lock", ec.LANE_DIR, ec.RESULT_LOG]
leaked = [path for path in PRODUCTION if os.path.exists(path)]
check("予算・レーン・結果ログを .secrets へ作っていない", not leaked, repr(leaked))
check("模擬サーバは 127.0.0.1 だけ(外部到達 0 件)",
      bool(ORIGINS_SEEN) and all(o.startswith("http://127.0.0.1:") for o in ORIGINS_SEEN),
      repr(ORIGINS_SEEN[:3]))


# ============================================================ 締め

print(f"\n{'ALL PASS' if not FAIL[0] else 'FAILED'} ({PASS[0]} checks, {FAIL[0]} failures)")
sys.exit(1 if FAIL[0] else 0)

# -*- coding: utf-8 -*-
"""R117: Broker Gateway の状態機械 —— 故障注入と性能。

`GatewayState` は純粋(I/O なし・時計は注入)なので、順序逆転・重複・seq 欠落・
resync・再接続・口座混入・限月違いを **ネットワーク無しで** 何千件でも試験できる。

ここで固定する不変条件:

1. **VERIFIED でなければ建玉ゼロを名乗らない。** 整合性が証明できない期間は
   `UNVERIFIED` / `RESYNCING` を出し、呼び出し側が FLAT と読むことを許さない。
2. **他口座の行は状態に入れない。** identity の混入は数えて捨てる。
3. **古いフレームで新しい状態を上書きしない。** seq が戻ったら無視する。
4. **初期取得の最中に来たイベントを捨てない。** 溜めて、取得結果の上へ重ねる。
5. **ファイルを書いた時刻で鮮度を更新しない。** 鮮度はブローカーの epoch と受信時刻。

    python tests/test_r117_broker_gateway.py
"""
import os
import statistics
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

for _stream in ("stdout", "stderr"):
    _file = getattr(sys, _stream, None)
    if _file is not None and hasattr(_file, "reconfigure"):
        try:
            _file.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

import broker_gateway as gw  # noqa: E402

PASS = [0]
FAIL = [0]


def check(label, condition, detail=""):
    if condition:
        PASS[0] += 1
        print(f"  OK   {label}")
    else:
        FAIL[0] += 1
        print(f"  NG   {label}  {detail}")


ACCOUNTS = ["66248423", "66248504", "66248536"]
CONTRACT = "4470324"
OTHER_CONTRACT = "4399654"
FOREIGN = "99999999"


def fresh(accounts=None, contracts=(CONTRACT,)):
    state = gw.GatewayState(expected_accounts=accounts or ACCOUNTS, contract_ids=contracts)
    state.connected("sess-1", now=0.0)
    return state


def full_groups(rows_by_account, key="id"):
    """`Tv_ListPositions` / `Tv_ListOrders` の返す形(接続グループの入れ子)。"""
    return [{"environment": "demo", "userId": "6231671", "name": "GRP",
             "data": [row for rows in rows_by_account.values() for row in rows]}]


def order_row(account, order_id, status="Working", contract=CONTRACT):
    return {"id": order_id, "accountId": account, "contractId": contract,
            "ordStatus": status, "action": "Sell", "timestamp": "2026-09-19T00:00:00Z"}


def position_row(account, net, contract=CONTRACT):
    return {"accountId": account, "contractId": contract, "netPos": net,
            "netPrice": 29700.0}


def sync(state, *, positions=None, orders=None, now=1.0, epoch=1789758998913):
    state.begin_resync("test", now)
    state.apply_full_positions(full_groups(positions or {}), now, epoch)
    state.apply_full_orders(full_groups(orders or {}), now, epoch)
    return state


def frame(kind, seq, account, **extra):
    base = {"type": kind, "origin": "tradovate", "seq": seq, "epoch": 1789758998913,
            "account": account, "environment": "demo", "contractId": CONTRACT}
    base.update(extra)
    return base


# ================================================================
print("=" * 70)
print("1. 初期取得 → VERIFIED。それまでは FLAT を名乗らない")
print("=" * 70)

state = fresh()
check("接続直後は UNVERIFIED", state.integrity == gw.UNVERIFIED, state.integrity_reason)
view = state.account_view(ACCOUNTS[0], CONTRACT)
check("未観測の口座は integrity=UNVERIFIED", view["integrity"] == gw.UNVERIFIED, str(view))

sync(state, positions={}, orders={})
check("全体取得の後は VERIFIED", state.integrity == gw.VERIFIED, state.integrity_reason)
view = state.account_view(ACCOUNTS[0], CONTRACT)
check("行が無い口座は netPos=0 かつ VERIFIED",
      view["integrity"] == gw.VERIFIED and view["netPos"] == 0, str(view))
check("generation が進む", state.generation == 1, state.generation)

state2 = fresh()
sync(state2, positions={ACCOUNTS[0]: [position_row(ACCOUNTS[0], -2)]}, orders={})
v = state2.account_view(ACCOUNTS[0], CONTRACT)
check("建玉のある口座は netPos が入る", v["netPos"] == -2, str(v))
check("他の口座は 0", state2.account_view(ACCOUNTS[1], CONTRACT)["netPos"] == 0)

# ================================================================
print("=" * 70)
print("2. 初期取得とイベントの競合 —— 取りこぼさない")
print("=" * 70)

state = fresh()
state.begin_resync("boot", 1.0)
# 取得の応答が返る前にイベントが届く
verdict = state.apply_frame(frame("positionUpdate", 1, ACCOUNTS[0],
                                  position={"netPos": 4, "netPrice": 29700}), 1.1)
check("取得前のイベントは溜める(捨てない)", verdict == "buffered", verdict)
state.apply_full_positions(full_groups({}), 1.2, 1)
state.apply_full_orders(full_groups({}), 1.3, 1)
v = state.account_view(ACCOUNTS[0], CONTRACT)
check("溜めたイベントが取得結果の上に重なる", v["netPos"] == 4, str(v))
check("重ねた後も VERIFIED", state.integrity == gw.VERIFIED)

# ================================================================
print("=" * 70)
print("3. seq の欠落・重複・逆転")
print("=" * 70)

state = sync(fresh(), positions={}, orders={})
state.apply_frame(frame("positionUpdate", 10, ACCOUNTS[0], position={"netPos": 1}), 2.0)
check("連番を追う", state.last_seq == 10, state.last_seq)

verdict = state.apply_frame(frame("positionUpdate", 10, ACCOUNTS[0], position={"netPos": 9}), 2.1)
check("同じ seq は重複として無視", verdict == "duplicate", verdict)
check("重複で状態が壊れない",
      state.account_view(ACCOUNTS[0], CONTRACT)["netPos"] == 1)

verdict = state.apply_frame(frame("positionUpdate", 5, ACCOUNTS[0], position={"netPos": 9}), 2.2)
check("古い seq(逆転)も無視", verdict == "duplicate", verdict)
check("逆転で状態が戻らない",
      state.account_view(ACCOUNTS[0], CONTRACT)["netPos"] == 1)

verdict = state.apply_frame(frame("positionUpdate", 20, ACCOUNTS[0], position={"netPos": 3}), 2.3)
check("seq が飛んだら gap", verdict == "gap", verdict)
check("gap の後は VERIFIED でない", state.integrity == gw.RESYNCING, state.integrity)
check("gap を数える", state.seq_gaps == 1, state.seq_gaps)
check("**gap 中は FLAT を名乗らない**",
      state.account_view(ACCOUNTS[1], CONTRACT)["integrity"] != gw.VERIFIED)

state = sync(fresh(), positions={}, orders={})
state.apply_frame(frame("positionUpdate", 1, ACCOUNTS[0], position={"netPos": 1}), 3.0)
state.apply_frame(frame("positionUpdate", 2, ACCOUNTS[0], dropped=3,
                        position={"netPos": 2}), 3.1)
check("dropped>0 で resync へ", state.integrity == gw.RESYNCING, state.integrity)
check("dropped を数える", state.dropped == 3, state.dropped)

# ================================================================
print("=" * 70)
print("4. resync / status / 切断")
print("=" * 70)

state = sync(fresh(), positions={}, orders={})
state.apply_frame({"type": "resync", "seq": 51, "accounts": [ACCOUNTS[0]]}, 4.0)
check("resync フレームで RESYNCING", state.integrity == gw.RESYNCING, state.integrity)
state.apply_full_positions(full_groups({}), 4.1, 1)
state.apply_full_orders(full_groups({}), 4.2, 1)
check("再取得で VERIFIED へ戻る", state.integrity == gw.VERIFIED)
check("generation が進む(世代で古い判断を弾ける)", state.generation == 2, state.generation)

for bad in ("stalled", "stopped", "disconnected"):
    s = sync(fresh(), positions={}, orders={})
    s.apply_frame({"type": "status", "seq": 99, "state": bad}, 5.0)
    check(f"status={bad} は UNVERIFIED", s.integrity == gw.UNVERIFIED, s.integrity_reason)

s = sync(fresh(), positions={}, orders={})
s.disconnected("socket closed", 6.0)
check("切断で UNVERIFIED", s.integrity == gw.UNVERIFIED)
check("切断後は口座ごとも UNVERIFIED",
      s.account_view(ACCOUNTS[0], CONTRACT)["integrity"] == gw.UNVERIFIED)
s.connected("sess-2", 6.1)
check("再接続で連番をリセット(前の接続の seq を引き継がない)", s.last_seq is None)

# ================================================================
print("=" * 70)
print("5. identity —— 他口座・別限月を混ぜない")
print("=" * 70)

state = fresh()
sync(state, positions={FOREIGN: [position_row(FOREIGN, 5)]}, orders={})
check("設定外の口座は状態に入れない", FOREIGN not in state.accounts, sorted(state.accounts))
check("混入を数える", state.foreign_rows >= 1, state.foreign_rows)

verdict = state.apply_frame(frame("positionUpdate", 1, FOREIGN, position={"netPos": 5}), 7.0)
check("設定外の口座のイベントも弾く", verdict == "foreign", verdict)

verdict = state.apply_frame(frame("positionUpdate", 2, ACCOUNTS[0],
                                  contractId=OTHER_CONTRACT, position={"netPos": 7}), 7.1)
check("別限月のイベントは監視対象へ入れない", verdict == "other-contract", verdict)
check("別限月で本限月の netPos が動かない",
      state.account_view(ACCOUNTS[0], CONTRACT)["netPos"] == 0)

state = sync(fresh(), positions={},
             orders={ACCOUNTS[0]: [order_row(ACCOUNTS[0], 1, contract=OTHER_CONTRACT)]})
check("別限月の注文は本限月の orders に出ない",
      state.account_view(ACCOUNTS[0], CONTRACT)["orders"] == [])

# ================================================================
print("=" * 70)
print("6. 注文と約定")
print("=" * 70)

state = sync(fresh(), positions={},
             orders={ACCOUNTS[0]: [order_row(ACCOUNTS[0], 664994700004),
                                   order_row(ACCOUNTS[0], 664994700005, "Suspended")]})
v = state.account_view(ACCOUNTS[0], CONTRACT)
check("注文が口座・限月で引ける", len(v["orders"]) == 2, str(v["orders"]))
state.apply_frame(frame("orderUpdate", 1, ACCOUNTS[0],
                        order={"id": 664994700004, "ordStatus": "Canceled"}), 8.0)
v = state.account_view(ACCOUNTS[0], CONTRACT)
statuses = {str(o.get("id")): o.get("ordStatus") for o in v["orders"]}
check("orderUpdate が既存の注文を更新する",
      statuses.get("664994700004") == "Canceled", str(statuses))
check("他の注文は残る", statuses.get("664994700005") == "Suspended", str(statuses))

state.apply_frame(frame("executionUpdate", 2, ACCOUNTS[0]), 8.1)
check("約定イベントを数える", state.accounts[ACCOUNTS[0]]["executions"] == 1)

v = state.account_view(ACCOUNTS[0], CONTRACT)
check("終端した注文は liveOrders に出ない",
      [str(o.get("id")) for o in v["liveOrders"]] == ["664994700005"],
      str([(o.get("id"), o.get("ordStatus")) for o in v["liveOrders"]]))
check("orders には全部残る(監査用)", len(v["orders"]) == 2)

# 一覧取得の epoch が口座別の鮮度に入る
s = sync(fresh(), positions={}, orders={}, now=20.0, epoch=1789758998913)
sv = s.account_view(ACCOUNTS[0], CONTRACT)
check("一覧取得でも source 時刻が入る(ローカル時刻で埋めない)",
      sv["sourceObservedAt"] and sv["sourceObservedAt"].startswith("2026-"),
      str(sv["sourceObservedAt"]))
s2 = fresh()
s2.begin_resync("no epoch", 21.0)
s2.apply_full_positions(full_groups({}), 21.0, None)
s2.apply_full_orders(full_groups({}), 21.0, None)
check("epoch が無ければ source 時刻は None のまま",
      s2.account_view(ACCOUNTS[0], CONTRACT)["sourceObservedAt"] is None)

# ================================================================
print("=" * 70)
print("7. 鮮度はブローカー時刻で。書いた時刻で更新しない")
print("=" * 70)

state = sync(fresh(), positions={}, orders={}, now=10.0, epoch=1789758998913)
state.apply_frame(frame("positionUpdate", 1, ACCOUNTS[0], epoch=1789758999000,
                        position={"netPos": 1}), 10.5)
v = state.account_view(ACCOUNTS[0], CONTRACT)
check("source 時刻はブローカーの epoch 由来",
      v["sourceObservedAt"] and v["sourceObservedAt"].startswith("2026-"),
      str(v["sourceObservedAt"]))
check("受信時刻は別に持つ", v["receivedAt"] == 10.5, v["receivedAt"])

snap1 = state.snapshot(now=10.6)
snap2 = state.snapshot(now=999.0)
check("書いた時刻(writtenAt)は変わる", snap1["writtenAt"] != snap2["writtenAt"])
check("**データの鮮度は書き直しで変わらない**",
      snap1["accounts"][ACCOUNTS[0]]["receivedAt"]
      == snap2["accounts"][ACCOUNTS[0]]["receivedAt"])

with tempfile.TemporaryDirectory() as tmp:
    path = os.path.join(tmp, "snap.json")
    gw.write_snapshot(state, 11.0, path)
    loaded = gw.read_snapshot(path)
    check("原子的に書いて読み戻せる", loaded and loaded["schema"] == gw.SCHEMA)
    check("読み戻しても整合性が残る", loaded["integrity"] == gw.VERIFIED)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("{ broken")
    check("壊れたファイルは None(推測しない)", gw.read_snapshot(path) is None)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write('{"schema": "OTHER/9"}')
    check("schema 違いも None", gw.read_snapshot(path) is None)
check("ファイルが無ければ None",
      gw.read_snapshot(os.path.join(tempfile.gettempdir(), "nope-xyz.json")) is None)

# ================================================================
print("=" * 70)
print("8. 通信予算(3 req/s・バースト 20・利用者単位)")
print("=" * 70)

bucket = gw.TokenBucket(now=0.0)
waits = [bucket.take(0.0) for _ in range(gw.RPC_BURST)]
check("バースト 20 本までは待たない", all(w == 0 for w in waits), str(set(waits)))
check("21 本目は待つ", bucket.take(0.0) > 0)
check("時間が経てば補充される", gw.TokenBucket(now=0.0).take(10.0) == 0.0)

floors = {n: gw.order_placement_floor_sec(n) for n in (1, 7, 20)}
check("N=1(2 本)は補充待ち 0", floors[1] == 0.0, str(floors))
check("N=7(14 本)も補充待ち 0", floors[7] == 0.0, str(floors))
check("N=20(40 本)は下限 6.67 秒 = 5 秒目標に届かない",
      abs(floors[20] - 20 / 3.0) < 1e-6 and floors[20] > 5.0, str(floors))

# ================================================================
print("=" * 70)
print("9. 性能 —— 1000 件以上のフレームで p50/p95/p99")
print("=" * 70)


def bench(account_count, frames=2000):
    accounts = [str(66248000 + i) for i in range(account_count)]
    state = gw.GatewayState(expected_accounts=accounts, contract_ids=(CONTRACT,))
    state.connected("bench", 0.0)
    rows = {a: [position_row(a, 0)] for a in accounts}
    state.begin_resync("bench", 0.0)
    state.apply_full_positions(full_groups(rows), 0.0, 1)
    state.apply_full_orders(full_groups({}), 0.0, 1)
    samples = []
    for i in range(frames):
        account = accounts[i % account_count]
        payload = frame("positionUpdate", i + 1, account,
                        position={"netPos": (i % 5) - 2, "netPrice": 29700 + i})
        t0 = time.perf_counter()
        state.apply_frame(payload, float(i))
        samples.append((time.perf_counter() - t0) * 1000.0)
    samples.sort()
    def pct(p):
        return samples[min(len(samples) - 1, int(len(samples) * p))]
    return {"n": len(samples), "p50": pct(0.50), "p95": pct(0.95),
            "p99": pct(0.99), "max": samples[-1], "gaps": state.seq_gaps,
            "integrity": state.integrity}


print("  イベント受信 → ローカル状態反映(状態機械のみ。ネットワーク・IO を含まない)")
results = {}
for n in (1, 7, 20):
    r = bench(n)
    results[n] = r
    print("    N=%-3d 件=%d  p50=%.3fms p95=%.3fms p99=%.3fms max=%.3fms  %s"
          % (n, r["n"], r["p50"], r["p95"], r["p99"], r["max"], r["integrity"]))

check("N=1/7/20 とも 1000 件以上を測った", all(r["n"] >= 1000 for r in results.values()))
check("全件 VERIFIED を保つ(欠落 0)",
      all(r["integrity"] == gw.VERIFIED and r["gaps"] == 0 for r in results.values()),
      str({n: (r["integrity"], r["gaps"]) for n, r in results.items()}))
check("p95 ≤ 100ms(目標)", all(r["p95"] <= 100.0 for r in results.values()),
      str({n: r["p95"] for n, r in results.items()}))
check("p99 ≤ 250ms(目標)", all(r["p99"] <= 250.0 for r in results.values()),
      str({n: r["p99"] for n, r in results.items()}))
check("口座数で反映時間が劣化しない(N=20 の p95 ≤ N=1 の p95 × 3 + 0.05ms)",
      results[20]["p95"] <= results[1]["p95"] * 3 + 0.05,
      "N=1 %.4f / N=20 %.4f" % (results[1]["p95"], results[20]["p95"]))

# 故障を混ぜた持続試験(欠落・重複・逆転・resync を織り交ぜる)
def chaos(account_count, frames=3000):
    accounts = [str(66248000 + i) for i in range(account_count)]
    state = gw.GatewayState(expected_accounts=accounts, contract_ids=(CONTRACT,))
    state.connected("chaos", 0.0)
    state.begin_resync("boot", 0.0)
    state.apply_full_positions(full_groups({}), 0.0, 1)
    state.apply_full_orders(full_groups({}), 0.0, 1)
    seq = 0
    verdicts = {}
    for i in range(frames):
        account = accounts[i % account_count]
        if i % 97 == 96:                      # seq を飛ばす
            seq += 5
        elif i % 53 == 52:                    # 重複
            pass
        else:
            seq += 1
        payload = frame("positionUpdate", seq, account, position={"netPos": i % 3})
        verdict = state.apply_frame(payload, float(i))
        verdicts[verdict] = verdicts.get(verdict, 0) + 1
        if state.integrity != gw.VERIFIED:    # 欠落したら再取得して復帰
            state.apply_full_positions(full_groups({}), float(i), 1)
            state.apply_full_orders(full_groups({}), float(i), 1)
    return state, verdicts


state, verdicts = chaos(20)
check("故障を混ぜても最後は VERIFIED へ復帰する", state.integrity == gw.VERIFIED,
      state.integrity_reason)
check("欠落を検出している", state.seq_gaps > 0, state.seq_gaps)
check("重複を無視している", verdicts.get("duplicate", 0) > 0, str(verdicts))
check("混入は 0(全部設定内の口座)", state.foreign_rows == 0, state.foreign_rows)


# ================================================================
print("=" * 70)
print("10. 常駐 runner(偽 transport。ネットワークを使わない)")
print("=" * 70)

import asyncio  # noqa: E402


class FakeTransport:
    """台本どおりにフレームを返す transport。送った payload も記録する。"""

    def __init__(self, script, disconnect_at_end=False):
        self.script = list(script)
        # 台本を使い切ったあとの扱い。既定は「無害な status を返し続ける」——
        # None を返すと runner は正しく切断扱い(UNVERIFIED)にするので、
        # 整合性を見たいケースでは max_frames で止める。
        self.disconnect_at_end = disconnect_at_end
        self.filler = 0
        self.sent = []
        self.connects = 0
        self.closed = False

    async def connect(self):
        self.connects += 1

    async def send(self, payload):
        self.sent.append(payload)
        # RPC には全体取得の応答を返す(台本の先頭へ差し込む)。
        if payload.get("action") == "rpc":
            kind = "positions" if payload["api"] == "Tv_ListPositions" else "orders"
            self.script.insert(0, {"id": payload["id"], "epoch": 1789758998913,
                                   "data": {"success": True, "data": [
                                       {"environment": "demo", "userId": "u", "name": "g",
                                        "data": []}]}, "_kind": kind})

    async def recv(self):
        if self.script:
            return self.script.pop(0)
        if self.disconnect_at_end:
            return None
        self.filler += 1
        return {"type": "status", "origin": "tradovate", "state": "ready",
                "epoch": 1789758998913}

    async def close(self):
        self.closed = True


def run_gateway(script, accounts=ACCOUNTS, max_frames=None, disconnect_at_end=False):
    # 台本のフレーム + RPC 応答 2 本 + 余白 2 本で止める(切断させない)。
    if max_frames is None:
        max_frames = len(script) + 4
    state = gw.GatewayState(expected_accounts=accounts, contract_ids=(CONTRACT,))
    clock = [0.0]

    def tick():
        clock[0] += 0.001
        return clock[0]

    transport = FakeTransport(script, disconnect_at_end=disconnect_at_end)
    gateway = gw.BrokerGateway(transport=transport, state=state, clock=tick,
                               snapshot_file=None)
    summary = asyncio.run(gateway.run_once(max_frames=max_frames))
    return state, transport, gateway, summary


state, transport, gateway, summary = run_gateway([
    frame("positionUpdate", 1, ACCOUNTS[0], position={"netPos": 2}),
    frame("positionUpdate", 2, ACCOUNTS[1], position={"netPos": -1}),
])
check("接続は 1 回だけ", transport.connects == 1, transport.connects)
check("購読を 1 回送る", summary["subscribes"] == 1, summary["subscribes"])
check("起動時の RPC は 2 本(全口座の建玉+注文)", summary["rpcCalls"] == 2, summary["rpcCalls"])
check("購読メッセージが仕様どおり",
      any(p.get("action") == "subscribe" and p.get("origin") == "tradovate"
          and list(p.get("events")) == list(gw.SUBSCRIBE_EVENTS) for p in transport.sent),
      str(transport.sent[:1]))
check("**注文を送っていない**",
      all(p.get("api") in (gw.BrokerGateway.LIST_POSITIONS, gw.BrokerGateway.LIST_ORDERS)
          for p in transport.sent if p.get("action") == "rpc"),
      str([p.get("api") for p in transport.sent if p.get("action") == "rpc"]))
check("イベントが状態へ入る",
      state.account_view(ACCOUNTS[0], CONTRACT)["netPos"] == 2
      and state.account_view(ACCOUNTS[1], CONTRACT)["netPos"] == -1)
check("最後は VERIFIED", summary["integrity"] == gw.VERIFIED, summary["integrity"])
check("止めたら close する", transport.closed)

# 台本を使い切って切断したら、**整合性は落ちる**(FLAT を名乗らせない)
_, closed_transport, _, closed_summary = run_gateway(
    [frame("positionUpdate", 1, ACCOUNTS[0], position={"netPos": 1})],
    disconnect_at_end=True, max_frames=50)
check("切断したら UNVERIFIED へ落ちる", closed_summary["integrity"] == gw.UNVERIFIED,
      closed_summary["integrity"])
check("切断でも close する", closed_transport.closed)

# 欠落したら自動で再取得(RPC が 2 本増える)
state, transport, gateway, summary = run_gateway([
    frame("positionUpdate", 1, ACCOUNTS[0], position={"netPos": 1}),
    frame("positionUpdate", 9, ACCOUNTS[0], position={"netPos": 2}),   # gap
])
check("欠落を検出したら全体を取り直す(RPC 2 + 2)", summary["rpcCalls"] == 4,
      summary["rpcCalls"])
check("取り直しの後は VERIFIED", summary["integrity"] == gw.VERIFIED, summary["integrity"])
check("欠落を数える", summary["seqGaps"] == 1, summary["seqGaps"])

# resync フレームでも取り直す
state, transport, gateway, summary = run_gateway([
    {"type": "resync", "seq": 1, "accounts": [ACCOUNTS[0]], "epoch": 1789758998913},
])
check("resync でも取り直す", summary["rpcCalls"] == 4, summary["rpcCalls"])

# 定常(状態変化なし)では口座別ポーリングが 0 本
state, transport, gateway, summary = run_gateway(
    [{"type": "status", "seq": i + 1, "state": "ready", "epoch": 1789758998913}
     for i in range(50)], max_frames=54)
check("**定常のポーリングは 0 本**(起動時の 2 本以外に RPC を出さない)",
      summary["rpcCalls"] == 2, summary["rpcCalls"])
check("status ready は整合性を落とさない", summary["integrity"] == gw.VERIFIED,
      summary["integrity"])

# 口座数を 20 にしても起動 RPC は 2 本のまま = O(1)
for n in (1, 7, 20):
    accounts = [str(66248000 + i) for i in range(n)]
    _, _, _, s = run_gateway([frame("positionUpdate", 1, accounts[0],
                                    position={"netPos": 1})], accounts=accounts)
    check(f"N={n} でも起動 RPC は 2 本(O(1))", s["rpcCalls"] == 2, s["rpcCalls"])

# 予算: 起動の 2 本はバースト内なので待たない
check("起動の RPC は補充待ちなし", summary["budgetWaited"] == 0.0, summary["budgetWaited"])

print()
print("=" * 70)
if FAIL[0]:
    print(f"FAILED {FAIL[0]} / {PASS[0] + FAIL[0]}")
    sys.exit(1)
print(f"ALL PASS ({PASS[0]})")

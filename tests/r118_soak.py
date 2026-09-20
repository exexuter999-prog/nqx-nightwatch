# -*- coding: utf-8 -*-
"""R118: 20 口座の持続負荷試験(既定 2 時間)。**外部到達 0 件・本番ファイル書き込み 0 件。**

`test_` で始めていないのは `tests/run_all.py` に拾わせないため —— 2 時間かかる。

回すもの(全部同時):

* Gateway の状態機械へ 20 口座ぶんのイベントを流し続ける(約定・注文・建玉)
* `broker_source` 経由の観測を毎秒(= `fill_watch` の見回りと同じ頻度)
* 一定間隔で resync / 切断 / seq 欠落 / dropped を混ぜる
* 一定間隔で ENTRY → TP1 → FLATTEN を模擬ブローカーへ流す

見るもの:

* 整合性が **VERIFIED へ戻り続ける**か(戻らなくなったら故障)
* 偽 FLAT・他口座混入が 0 件のままか
* メモリと、状態機械が抱える行数(**単調増加していないか**)
* 観測 1 回の所要の分位(劣化していないか)

    python tests/r118_soak.py            # 2 時間
    python tests/r118_soak.py --minutes 5
"""
import argparse
import gc
import io
import json
import os
import statistics
import sys
import tempfile
import time
import tracemalloc

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

for _stream in ("stdout", "stderr"):
    _file = getattr(sys, _stream, None)
    if _file is not None and hasattr(_file, "reconfigure"):
        try:
            _file.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

import broker_gateway as gw      # noqa: E402
import broker_source as src      # noqa: E402
import fill_watch                # noqa: E402
import mock_broker as mb         # noqa: E402

CONTRACT = mb.CONTRACT_ID
SYMBOL = mb.SYMBOL
TMP = tempfile.mkdtemp(prefix="r118-soak-")
SNAPSHOT_FILE = os.path.join(TMP, "gateway_snapshot.json")
gw.snapshot_path = lambda: SNAPSHOT_FILE

ACCOUNTS = [f"SOAK{i:02d}" for i in range(20)]


def gateway_heap_mb():
    """**Gateway 自身が確保している量**(MB)。

    総ヒープは試験の足場(観測時間の配列・模擬ブローカーの履歴)も含むので、
    「増えた」の原因が Gateway なのか足場なのかを分けられない。`tracemalloc` の
    フィルタで `broker_gateway.py` 由来の確保だけを数える。
    """
    try:
        snapshot = tracemalloc.take_snapshot().filter_traces(
            [tracemalloc.Filter(True, "*broker_gateway.py"),
             tracemalloc.Filter(True, "*broker_source.py")])
        return sum(stat.size for stat in snapshot.statistics("filename")) / 1e6
    except Exception:  # noqa: BLE001
        return 0.0


#: 1 回の「落ちた」申告に載せる件数。集計で **回数と件数を混ぜない**ための定数。
DROPPED_PER_EVENT = 2


def rss_mb():
    """常駐メモリ(MB)。psutil が無ければ None。"""
    try:
        import psutil
        return psutil.Process().memory_info().rss / (1024 * 1024)
    except Exception:  # noqa: BLE001
        return None


class Feeder:
    """Gateway の状態機械へイベントを流す。`seq` と故障を自分で管理する。"""

    def __init__(self, state):
        self.state = state
        self.seq = 0
        self.epoch = int(time.time() * 1000)
        self.sent = 0
        self.gaps_injected = 0
        self.dropped_injected = 0
        self.resyncs = 0
        self.disconnects = 0

    def _frame(self, kind, payload, *, skip=0, dropped=None):
        self.seq += 1 + skip
        self.epoch += 250
        frame = {"type": kind, "seq": self.seq, "epoch": self.epoch, **payload}
        if dropped:
            frame["dropped"] = dropped
        return frame

    def full_state(self, broker, now):
        self.state.begin_resync("periodic", now)
        self.state.apply_full_positions(broker.ws_positions(), now, epoch=self.epoch)
        self.state.apply_full_orders(broker.ws_orders(), now, epoch=self.epoch)

    def position_event(self, account, net, now, *, wrapped=False):
        """建玉の押し込み。`wrapped` で **本体を data で包んだ形**も流す。

        押し込みの正確な形は実地未確認なので、両方の形を流して同じ結果になることを
        持続負荷の中でも確かめる。
        """
        self.sent += 1
        body = {"netPos": net, "netPrice": 29850.0}
        if wrapped:
            payload = {"data": {**body, "accountId": account, "contractId": CONTRACT}}
        else:
            payload = {"accountId": account, "contractId": CONTRACT, "position": body}
        self.state.apply_frame(self._frame(gw.POSITION_FRAME, payload), now)

    def order_event(self, account, order_id, status, now, *, skip=0, dropped=None,
                    wrapped=False):
        self.sent += 1
        if skip:
            self.gaps_injected += 1
        if dropped:
            self.dropped_injected += 1
        body = {"id": order_id, "ordStatus": status, "action": "BUY",
                "orderQty": 1, "orderType": "STOP"}
        if wrapped:
            payload = {"data": {**body, "accountId": account, "contractId": CONTRACT}}
        else:
            payload = {"accountId": account, "contractId": CONTRACT, "order": body}
        self.state.apply_frame(
            self._frame(gw.ORDER_FRAME, payload, skip=skip, dropped=dropped), now)


def run(minutes: float, report_every: float) -> int:
    deadline = time.monotonic() + minutes * 60.0
    state = gw.GatewayState(expected_accounts=ACCOUNTS, contract_ids=[CONTRACT])
    broker = mb.MockBroker(ACCOUNTS)
    feeder = Feeder(state)

    os.environ["NQX_GATEWAY_MODE"] = "LIVE"
    saved_contract_ids = src._contract_ids
    src._contract_ids = lambda cfg=None, symbol=None: [CONTRACT]

    tracemalloc.start()
    started = time.monotonic()
    base_rss = rss_mb()
    gc.collect()
    base_heap = tracemalloc.get_traced_memory()[0]

    observe_ms = []
    samples = []
    tick = 0
    false_flat = 0
    unverified_observations = 0
    recovered = 0
    last_report = time.monotonic()

    feeder.full_state(broker, time.monotonic())
    gw.write_snapshot(state, time.time(), SNAPSHOT_FILE)

    print(f"開始: 口座 {len(ACCOUNTS)} / {minutes:g} 分 / スナップショット {SNAPSHOT_FILE}")
    print(f"{'経過':>6} {'周回':>7} {'整合性':>10} {'観測 p95':>9} {'行数':>7} "
          f"{'全ヒープ':>8} {'GWヒープ':>8} {'偽FLAT':>7}")

    while time.monotonic() < deadline:
        tick += 1
        now = time.monotonic()

        # --- 相場の動き: 3 分ごとに ENTRY → TP1 → FLATTEN を一巡 -------------
        phase = tick % 180
        if phase == 1:
            for account in ACCOUNTS:
                broker.place({"account": account, "command": "PLACE", "action": "SELL",
                              "qty": "1", "order_type": "MARKET",
                              "stop_loss": "29900", "take_profit": "29700"})
                broker.fill_entry(account, "SELL", 2, 29850.0)
                feeder.position_event(account, -2, now, wrapped=(tick % 2 == 0))
        elif phase == 60:
            for account in ACCOUNTS:                      # 同時 TP1
                live = broker.protective_ids(account)
                if live:
                    broker.fill_protective(account, live[0], 29700.0)
                feeder.position_event(account, -1, now, wrapped=(tick % 2 == 0))
        elif phase == 120:
            for account in ACCOUNTS:
                broker._do_flatten(account)
                feeder.position_event(account, 0, now, wrapped=(tick % 2 == 0))

        # --- 注文イベント(毎周回) -------------------------------------------
        account = ACCOUNTS[tick % len(ACCOUNTS)]
        feeder.order_event(account, f"o-{tick}", "Working", now,
                           wrapped=(tick % 3 == 0))

        # --- 故障を混ぜる -----------------------------------------------------
        if tick % 97 == 0:                                 # seq 欠落
            feeder.order_event(account, f"gap-{tick}", "Working", now, skip=3)
        if tick % 151 == 0:                                # dropped の申告
            feeder.order_event(account, f"drop-{tick}", "Working", now,
                               dropped=DROPPED_PER_EVENT)
        if tick % 211 == 0:                                # 切断 → 再取得
            state.disconnected("soak: injected disconnect", now)
            feeder.disconnects += 1
        if tick % 211 == 5 or state.integrity != gw.VERIFIED:
            feeder.full_state(broker, time.monotonic())
            feeder.resyncs += 1
            if state.integrity == gw.VERIFIED:
                recovered += 1

        gw.write_snapshot(state, time.time(), SNAPSHOT_FILE)
        src.invalidate_snapshot()

        # --- 観測(fill_watch と同じ見回り) ----------------------------------
        t0 = time.perf_counter()
        observation = fill_watch.observe(
            SYMBOL, ACCOUNTS,
            lambda _s, account=None: src.position(
                SYMBOL, account,
                rest_call=lambda: {"verified": False, "source": "rest-disabled",
                                   "detail": "soak: REST は使わない"}))
        observe_ms.append((time.perf_counter() - t0) * 1000.0)

        for name, row in observation.items():
            if not row.get("verified"):
                unverified_observations += 1
                continue
            truth = broker.state[name]["netPos"]
            if row.get("qty") == 0 and truth != 0:
                false_flat += 1                 # **これが 1 件でも出たら失格**

        delay, _mode = fill_watch.next_delay(
            {"fastIntervalSec": 1.5, "idleIntervalSec": 45.0,
             "maxRequestsPerSec": 1.0, "rateLimitBackoffSec": 15.0},
            observation)

        if time.monotonic() - last_report >= report_every:
            last_report = time.monotonic()
            gc.collect()
            heap = tracemalloc.get_traced_memory()[0]
            rows = sum(len(entry["positions"]) + len(entry["orders"])
                       for entry in state.accounts.values())
            ordered = sorted(observe_ms[-2000:])
            p95 = ordered[int(len(ordered) * 0.95)] if ordered else 0.0
            sample = {
                "elapsedSec": round(time.monotonic() - started, 1), "tick": tick,
                "integrity": state.integrity, "observeP95Ms": round(p95, 3),
                "rows": rows, "heapMB": round(heap / 1e6, 2),
                "gatewayHeapMB": round(gateway_heap_mb(), 2),
                "rssMB": round(rss_mb() or 0.0, 1),
                "falseFlat": false_flat, "foreignRows": state.foreign_rows,
                "seqGaps": state.seq_gaps, "dropped": state.dropped,
                "nextDelaySec": delay,
            }
            samples.append(sample)
            print(f"{sample['elapsedSec']:>6.0f} {tick:>7} {state.integrity:>10} "
                  f"{p95:>9.3f} {rows:>7} {sample['heapMB']:>8.2f} "
                  f"{sample['gatewayHeapMB']:>8.2f} {false_flat:>7}", flush=True)

        time.sleep(0.05)

    tracemalloc.stop()
    src._contract_ids = saved_contract_ids

    ordered = sorted(observe_ms)
    first_half = ordered[: len(ordered) // 2]
    result = {
        "accounts": len(ACCOUNTS),
        "minutes": minutes,
        "ticks": tick,
        "framesFed": feeder.sent,
        "resyncs": feeder.resyncs,
        "disconnects": feeder.disconnects,
        "recovered": recovered,
        "seqGapsSeen": state.seq_gaps,
        "gapsInjected": feeder.gaps_injected,
        # **単位が違う。** `droppedSeen` はブローカーが申告した「落ちた件数」の
        # 合計、`droppedInjectedEvents` は申告を混ぜた**回数**(1 回につき 2 件と
        # 申告する)。前の報告は 1,292 と 646 を並べて「全部検出」と書いていて、
        # 数が合わないように読めた。期待値を先に出す。
        "droppedSeen": state.dropped,
        "droppedInjectedEvents": feeder.dropped_injected,
        "droppedInjectedCount": feeder.dropped_injected * DROPPED_PER_EVENT,
        "falseFlat": false_flat,
        "foreignRows": state.foreign_rows,
        "unreadableEvents": state.unreadable_events,
        "unverifiedObservations": unverified_observations,
        "finalIntegrity": state.integrity,
        "observeMs": {
            # 指示 §6 は p50/p95/**p99**/max を求めている。p99 が無いと
            # 「たまに 358ms」が平らな p95 に隠れる。
            "count": len(ordered),
            "p50": round(statistics.median(ordered), 3) if ordered else None,
            "p95": round(ordered[int(len(ordered) * 0.95)], 3) if ordered else None,
            "p99": round(ordered[int(len(ordered) * 0.99)], 3) if ordered else None,
            "max": round(ordered[-1], 3) if ordered else None,
        },
        "heapMB": {"start": round(base_heap / 1e6, 2),
                   "end": samples[-1]["heapMB"] if samples else None,
                   "series": [s["heapMB"] for s in samples],
                   "gatewaySeries": [s.get("gatewayHeapMB") for s in samples]},
        # **RSS は psutil が無ければ測れない。** 0.0 を「増えていない」と読ませない
        # ために、可用性を並べて出す(この機械には psutil が入っていない)。
        "rssMB": {"available": rss_mb() is not None,
                  "start": round(base_rss, 1) if base_rss else None,
                  "end": samples[-1]["rssMB"] if samples else None},
        "rows": {"first": samples[0]["rows"] if samples else None,
                 "last": samples[-1]["rows"] if samples else None,
                 "peak": max((s["rows"] for s in samples), default=None),
                 "ceiling": len(ACCOUNTS) * (gw.MAX_TERMINAL_ORDERS + 10)},
        "samples": samples,
    }
    out = os.path.join(TMP, "soak_result.json")
    with io.open(out, "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=1)

    print("\n=== 結果 ===")
    print(f"  周回 {tick} / フレーム {feeder.sent} / 再取得 {feeder.resyncs} / "
          f"切断 {feeder.disconnects}")
    print(f"  観測 1 回: p50 {result['observeMs']['p50']}ms "
          f"p95 {result['observeMs']['p95']}ms p99 {result['observeMs']['p99']}ms "
          f"max {result['observeMs']['max']}ms(標本 {result['observeMs']['count']})")
    if not result["rssMB"]["available"]:
        print("  RSS: **測れていない**(psutil が無い)。メモリの判定は"
              "tracemalloc の Gateway ヒープだけで行っている")
    series = result["heapMB"].get("gatewaySeries") or []
    print(f"  全ヒープ {result['heapMB']['start']} → {result['heapMB']['end']} MB"
          f"(試験の足場を含む)")
    print(f"  **Gateway のヒープ** {series[0] if series else '-'} → "
          f"{series[-1] if series else '-'} MB(これが判定の対象)")
    print(f"  行数 {result['rows']['first']} → {result['rows']['last']} "
          f"(最大 {result['rows']['peak']} / 上限 {result['rows']['ceiling']})")
    print(f"  偽 FLAT {false_flat} / 他口座混入 {state.foreign_rows} / "
          f"seq 欠落 {state.seq_gaps}(注入 {feeder.gaps_injected}) / "
          f"dropped {state.dropped} 件"
          f"(申告 {feeder.dropped_injected} 回 × {DROPPED_PER_EVENT} = "
          f"{feeder.dropped_injected * DROPPED_PER_EVENT} 件)")
    print(f"  結果: {out}")

    failures = []
    if false_flat:
        failures.append(f"偽 FLAT が {false_flat} 件")
    if state.foreign_rows:
        failures.append(f"他口座の行が {state.foreign_rows} 件混ざった")
    if state.unreadable_events:
        failures.append(f"読めない押し込みが {state.unreadable_events} 件")
    if state.integrity != gw.VERIFIED:
        failures.append(f"最後まで VERIFIED へ戻らなかった({state.integrity})")
    # **注入した欠落を取りこぼしたら失格。** これまで数を並べるだけで、
    # 合っているかどうかは人が読んで判断していた。
    if state.seq_gaps < feeder.gaps_injected:
        failures.append(f"seq 欠落の検出漏れ({state.seq_gaps} < {feeder.gaps_injected})")
    expected_dropped = feeder.dropped_injected * DROPPED_PER_EVENT
    if state.dropped < expected_dropped:
        failures.append(f"dropped の検出漏れ({state.dropped} < {expected_dropped})")
    # 抱える行数は **上限**で見る。前後の 2 点で比べると、再取得の周期
    # (全体取得のたびにブローカーの生きた行へ揃う)と噛み合ったときに
    # 「増え続けた」と誤判定する —— 実際は増減している(2026-09-19)。
    row_ceiling = len(ACCOUNTS) * (gw.MAX_TERMINAL_ORDERS + 10)
    peak_rows = max((s["rows"] for s in samples), default=0)
    if peak_rows > row_ceiling:
        failures.append(f"抱える行数が上限を超えた(最大 {peak_rows} > {row_ceiling})")
    # ヒープは **後半どうし**(第 3 四半分 vs 第 4 四半分)で比べる。
    #
    # 最初と最後で比べると、**上限まで埋まる立ち上がり**を漏れと誤判定する ——
    # 状態機械は口座ごとに終端注文を MAX_TERMINAL_ORDERS まで抱えるので、
    # 走り始めは必ず増える(そこは設計どおりで、上限で止まる)。漏れは
    # 「平らになったはずの後半でまだ増えている」ことなので、そこを見る。
    if len(samples) >= 4:
        quarter = max(1, len(samples) // 4)
        third = statistics.median(s["gatewayHeapMB"] for s in samples[-2 * quarter:-quarter])
        fourth = statistics.median(s["gatewayHeapMB"] for s in samples[-quarter:])
        if third and fourth > third * 1.5:
            failures.append(
                f"Gateway のヒープが後半でも増え続けた"
                f"(第3四半分 {third:.2f} → 第4四半分 {fourth:.2f} MB)")
    if len(ordered) > 200:
        late = sorted(observe_ms[-len(observe_ms) // 4:])
        late_p95 = late[int(len(late) * 0.95)]
        early = sorted(first_half)
        early_p95 = early[int(len(early) * 0.95)]
        if late_p95 > max(1.0, early_p95 * 3):
            failures.append(f"観測が遅くなった(p95 {early_p95:.2f} → {late_p95:.2f} ms)")

    if failures:
        print("\nFAILED: " + " / ".join(failures))
        return 1
    print("\nALL PASS(偽 FLAT 0・混入 0・整合性は VERIFIED・増加なし)")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="R118 持続負荷試験(模擬・外部到達なし)")
    parser.add_argument("--minutes", type=float, default=120.0)
    parser.add_argument("--report-every", type=float, default=300.0)
    args = parser.parse_args()
    sys.exit(run(args.minutes, args.report_every))

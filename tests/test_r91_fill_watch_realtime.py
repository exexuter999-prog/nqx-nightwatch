# -*- coding: utf-8 -*-
"""R91: fill_watch の常駐保証(自動起動・単一インスタンス)と、上限の中での高速照会。

ブローカー・Worker・TradingView には触れない(照会・reconcile・起動はすべて注入)。
spawn_detached の検査だけは、何もしないダミースクリプトを実際に切り離して起動する。

守ること:
  1. 契約 fillWatch が壊れていたら既定(autostart は False)。autostart は JSON の true だけ。
  2. 建玉がある間は fastIntervalSec、FLAT は idleIntervalSec。見回りの本数(建玉あり 1 / FLAT・未検証 2)を
     maxRequestsPerSec で割った間隔より詰めない。429 を見たら rateLimitBackoffSec 以上空ける。
  3. ループの reconcile ロックが握られている間はブローカーを照会しない(待たずに覗くだけ)。
  4. 単一インスタンス: ロックを握っている間は 2 つ目が取れない。
  5. supervise は autostart のときだけ、止まっていれば 1 回起動する。生きていれば起動しない。
     ロックを握ったまま応答が無いプロセスがあれば起動しない。3 回起動して heartbeat が出なければ 1 時間止める。
     起動の失敗で例外を投げない。
  6. settings を渡さない run() は R91 以前の固定間隔のまま(R78 の注入テストと互換)。
"""
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import autotrade_engine as ae  # noqa: E402
ae._read_env_file = lambda path=None: {}          # 本番 .secrets から隔離
import fill_watch  # noqa: E402

ACC = "LFF-TEST"
SYM = "MNQU6"
TMP = Path(tempfile.mkdtemp(prefix="nqx-r91-"))


def check(label, condition, detail=""):
    if not condition:
        raise AssertionError(f"{label}: {detail}")
    print(f"OK   {label}")


def settings(**over):
    return fill_watch.load_settings({"fillWatch": {"version": "R91-FILL-WATCH-1", "autostart": False,
                                                   "fastIntervalSec": 1.5, "idleIntervalSec": 5.0,
                                                   "maxRequestsPerSec": 1.0, "rateLimitBackoffSec": 15.0,
                                                   **over}})


def test_settings_loader():
    default = fill_watch.load_settings({})
    check("節が無ければ既定(autostart False / 1.5s / 5s / 1rps / 15s)",
          default["autostart"] is False and default["fastIntervalSec"] == 1.5
          and default["idleIntervalSec"] == 5.0 and default["maxRequestsPerSec"] == 1.0
          and default["rateLimitBackoffSec"] == 15.0 and default["invalid"] == [], default)
    check("autostart は JSON の true だけ", settings(autostart=True)["autostart"] is True)
    text_true = settings(autostart="true")
    check("文字列 \"true\" では起動しない(invalid に残る)",
          text_true["autostart"] is False and "autostart" in text_true["invalid"], text_true)
    wild = settings(fastIntervalSec=0.1, maxRequestsPerSec=5, idleIntervalSec="x")
    check("範囲外・型違いはその項目だけ既定へ", wild["fastIntervalSec"] == 1.5 and wild["maxRequestsPerSec"] == 1.0
          and wild["idleIntervalSec"] == 5.0
          and set(wild["invalid"]) == {"fastIntervalSec", "maxRequestsPerSec", "idleIntervalSec"}, wild)
    check("CrossTrade の毎秒 3 回を超える上限は書けない", fill_watch.SETTING_BOUNDS["maxRequestsPerSec"][1] < 3.0)
    broken = fill_watch.load_settings({"fillWatch": ["autostart"]})
    check("壊れた節は既定", broken["autostart"] is False and broken["invalid"] == ["FILL_WATCH_MALFORMED"], broken)
    real = fill_watch.load_settings()
    check("本番契約の fillWatch は読めて不正な項目が無い", real["invalid"] == [], real)


def test_pacing():
    open_one = {ACC: {"verified": True, "qty": 2, "side": "SHORT"}}
    flat_one = {ACC: {"verified": True, "qty": 0, "side": ""}}
    cfg = settings()
    check("建玉ありは FAST 1.5s", fill_watch.next_delay(cfg, open_one) == (1.5, "FAST"))
    check("FLAT は IDLE 5s", fill_watch.next_delay(cfg, flat_one) == (5.0, "IDLE"))
    check("見回りの本数: 建玉あり 1 / FLAT 2 / 未検証 2",
          fill_watch.sweep_cost(open_one) == 1 and fill_watch.sweep_cost(flat_one) == 2
          and fill_watch.sweep_cost({ACC: {"verified": False}}) == 2)
    three_open = {f"A{i}": {"verified": True, "qty": 2, "side": "LONG"} for i in range(3)}
    check("3 口座に建玉 → 1rps の予算で 3s より詰めない", fill_watch.next_delay(cfg, three_open) == (3.0, "FAST"))
    three_flat = {f"A{i}": {"verified": True, "qty": 0} for i in range(3)}
    check("3 口座 FLAT → 6 本 / 1rps で 6s", fill_watch.next_delay(cfg, three_flat) == (6.0, "IDLE"))
    check("全口座未検証の連続は指数で空ける", fill_watch.next_delay(cfg, flat_one, errors=2) == (20.0, "BACKOFF"))
    check("429 は rateLimitBackoffSec 以上", fill_watch.next_delay(cfg, open_one, rate_limited=True) == (15.0, "BACKOFF"))
    obs = fill_watch.observe(SYM, [ACC], lambda _s, account=None: {
        "verified": False, "detail": "crosstrade position query returned HTTP 429; tradovate: not configured"})
    check("照会の失敗理由を残し、429 を見分ける", fill_watch.rate_limited(obs) and "429" in obs[ACC]["error"], obs)
    check("429 以外の失敗は 429 扱いしない", not fill_watch.rate_limited(
        fill_watch.observe(SYM, [ACC], lambda _s, account=None: {"verified": False, "detail": "HTTP 503"})))


def run_capture(position_query, cfg, lock_busy=None, iterations=2, interval=5.0):
    sleeps, hb = [], TMP / f"hb-{time.monotonic_ns()}.json"
    fill_watch.run(symbol=SYM, accounts=[ACC], interval=interval, position_query=position_query,
                   reconcile=lambda bundle, state_ok=True, fresh_price_query=None: ["autotrade hold: managed"],
                   price_query=None, sync_position=None, heartbeat_path=hb, bundle_candidates=(),
                   resume=False, max_iterations=iterations, sleep=sleeps.append, log=lambda _l: None,
                   settings=cfg, lock_busy=lock_busy)
    return sleeps, fill_watch.load_heartbeat(hb)


def test_run_adaptive_and_rate_limited():
    calls = []
    sleeps, hb = run_capture(lambda _s, account=None: calls.append(1) or {"verified": True, "qty": 2, "side": "SHORT"},
                             settings())
    check("建玉ありの間は 1.5s で回す", sleeps == [1.5] and hb["mode"] == "FAST" and hb["nextDelaySec"] == 1.5, (sleeps, hb))
    check("heartbeat に版・起動者・照会時間", hb["version"] == "R91-FILL-WATCH-1" and hb["startedBy"] == "manual"
          and isinstance(hb["pollMs"], int), hb)
    sleeps, hb = run_capture(lambda _s, account=None: {"verified": False,
                                                       "detail": "crosstrade position query returned HTTP 429"},
                             settings())
    check("429 で 15s 以上空けて数える", sleeps and sleeps[0] >= 15.0 and hb["rateLimitedCount"] == 2
          and hb["mode"] == "BACKOFF", (sleeps, hb))
    busy = iter([True, False])
    calls.clear()
    sleeps, hb = run_capture(lambda _s, account=None: calls.append(1) or {"verified": True, "qty": 0},
                             settings(), lock_busy=lambda: next(busy))
    check("ループの reconcile 中(1 回目)は照会せず 1s 後に覗き直す", sleeps == [1.0] and len(calls) == 1, (sleeps, calls))
    check("heartbeat は止めない(PAUSED の回数を数える)", hb["pausedCount"] == 1, hb)
    legacy, _ = run_capture(lambda _s, account=None: {"verified": True, "qty": 2, "side": "SHORT"}, None, interval=5.0)
    check("settings 無しは R91 以前と同じ固定間隔", legacy == [5.0], legacy)


def test_status_line_modes():
    hb = TMP / "status.json"
    now = datetime.now(timezone.utc)
    fill_watch.write_heartbeat({"at": now.isoformat(), "intervalSec": 5.0, "nextDelaySec": 1.5, "mode": "FAST",
                                "rateLimitedCount": 2}, hb)
    line = fill_watch.status_line(now=now + timedelta(seconds=3), path=hb)
    check("alive にモードと 429 回数", "alive" in line and "FAST 1.5s" in line and "429×2" in line, line)
    check("30 秒を超えたら STALE", "STALE" in fill_watch.status_line(now=now + timedelta(seconds=45), path=hb))
    fill_watch.write_heartbeat({"at": now.isoformat(), "intervalSec": 5.0, "nextDelaySec": 60.0, "mode": "BACKOFF"}, hb)
    check("BACKOFF 60s の間は 6 倍まで STALE にしない",
          "alive" in fill_watch.status_line(now=now + timedelta(seconds=300), path=hb))


def test_locks():
    path = TMP / "instance.lock"
    first, second = fill_watch.FileLock(path), fill_watch.FileLock(path)
    check("1 つ目は取れる", first.acquire() is True)
    check("握っている間は 2 つ目が取れない(単一インスタンス)", second.acquire() is False)
    check("lock_held は握られていると True", fill_watch.lock_held(path) is True)
    first.release()
    check("解放後は lock_held False", fill_watch.lock_held(path) is False)
    ledger_lock = str(TMP / "ledger.jsonl.lock")
    with ae._ReconcileLock(ledger_lock, 0) as got:
        check("engine の reconcile ロックを握っている間は覗くと busy", got and fill_watch.lock_held(Path(ledger_lock)))
    check("覗くだけでロックを奪わない(解放後に engine が取れる)", fill_watch.lock_held(Path(ledger_lock)) is False)
    with ae._ReconcileLock(ledger_lock, 0) as got_again:
        check("覗いた後も engine はロックを取れる", got_again is True)


def test_supervise():
    hb, lock, state = TMP / "sup-hb.json", TMP / "sup.lock", TMP / "sup-state.json"
    spawned = []

    def spawn():
        spawned.append(1)
        return 4242

    kwargs = dict(heartbeat_path=hb, lock_path=lock, spawn_state_path=state, spawn=spawn)
    line = fill_watch.supervise(settings=settings(autostart=False), **kwargs)
    check("autostart false は表示だけ(起動しない)", spawned == [] and "未起動" in line, line)
    line = fill_watch.supervise(settings=settings(autostart=True), **kwargs)
    check("autostart true で止まっていれば 1 回起動", spawned == [1] and "起動 pid=4242" in line, line)
    check("起動の記録(pending 1)", json.loads(state.read_text(encoding="utf-8"))["pending"] == 1)
    now = datetime.now(timezone.utc)
    fill_watch.write_heartbeat({"at": now.isoformat(), "intervalSec": 5.0, "mode": "IDLE", "nextDelaySec": 5.0}, hb)
    line = fill_watch.supervise(now=now + timedelta(seconds=2), settings=settings(autostart=True), **kwargs)
    check("生きていれば起動しない", spawned == [1] and "alive" in line, line)
    check("生きているのを見たら pending を戻す", json.loads(state.read_text(encoding="utf-8"))["pending"] == 0)
    later = now + timedelta(minutes=10)
    holder = fill_watch.FileLock(lock)
    holder.acquire()
    try:
        line = fill_watch.supervise(now=later, settings=settings(autostart=True), **kwargs)
    finally:
        holder.release()
    check("応答の無いプロセスがロックを握っていれば起動しない", spawned == [1] and "ロックを保持" in line, line)
    fill_watch.write_heartbeat({"pending": 3, "lastSpawnAt": (later - timedelta(minutes=5)).isoformat()}, state)
    line = fill_watch.supervise(now=later, settings=settings(autostart=True), **kwargs)
    check("3 回起動しても heartbeat が出なければ止める", spawned == [1] and "停止中" in line, line)
    line = fill_watch.supervise(now=later + timedelta(hours=2), settings=settings(autostart=True), **kwargs)
    check("1 時間たてば再試行する", spawned == [1, 1] and "起動 pid=" in line, line)

    def boom():
        raise OSError("no python")
    hb.unlink()
    state.unlink()
    line = fill_watch.supervise(settings=settings(autostart=True), heartbeat_path=hb, lock_path=lock,
                                spawn_state_path=state, spawn=boom)
    check("起動に失敗しても例外を投げない", "autostart failed" in line, line)


def test_spawn_detached_with_dummy_script():
    marker = TMP / "dummy-ran.txt"
    script = TMP / "dummy_fill_watch.py"
    script.write_text("import sys, pathlib\n"
                      f"pathlib.Path({str(marker)!r}).write_text(' '.join(sys.argv[1:]), encoding='utf-8')\n",
                      encoding="utf-8")
    log = TMP / "fill_watch.log"
    pid = fill_watch.spawn_detached(log_path=log, script=script)
    deadline = time.monotonic() + 15
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.2)
    check("切り離したプロセスが起動して pid を返す", isinstance(pid, int) and pid > 0 and marker.exists(), pid)
    check("起動者の印を渡す", marker.read_text(encoding="utf-8").strip() == "--started-by nqx_cycle",
          marker.read_text(encoding="utf-8") if marker.exists() else None)
    check("ログに起動の見出しを追記", "autostart by nqx_cycle" in log.read_text(encoding="utf-8"))


def test_cycle_uses_supervise():
    source = Path(BASE, "nqx_cycle.py").read_text(encoding="utf-8")
    check("nqx_cycle は毎周期 supervise() を 1 行出す", "print(fill_watch.supervise())" in source)


test_settings_loader()
test_pacing()
test_run_adaptive_and_rate_limited()
test_status_line_modes()
test_locks()
test_supervise()
test_spawn_detached_with_dummy_script()
test_cycle_uses_supervise()
print("ALL PASS test_r91_fill_watch_realtime")

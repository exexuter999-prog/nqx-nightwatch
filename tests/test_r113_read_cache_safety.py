# -*- coding: utf-8 -*-
"""R113: 表示・会計用の読み取り共有と、その安全条件。

7 口座で 1 サイクルの照会が 70〜160 本になり `monitor_publish.py` が 300 秒で
タイムアウトするようになった。呼び出し元を記録したところ、**同じデータを別々の
呼び出し元が取り直している**のが正体だった(残高 7 口座 × 2 回、建玉が表示・
engine・戦績記録で 3 回)。

ここで固定するのは**安全条件**である。共有してよいのは表示・会計だけで、
送信の前後で建玉/注文を確かめる経路は必ず生の照会を使う ——
CLAUDE.md §4「送信後は必ずブローカーを再照会する」はキャッシュより強い。

    python tests/test_r113_read_cache_safety.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

for _stream in ("stdout", "stderr"):
    _file = getattr(sys, _stream, None)
    if _file is not None and hasattr(_file, "reconfigure"):
        try:
            _file.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

import broker_status as bs        # noqa: E402
import autotrade_engine as ae     # noqa: E402
import trade_journal              # noqa: E402

PASS = [0]
FAIL = [0]


def check(label, condition, detail=""):
    if condition:
        PASS[0] += 1
        print(f"  OK   {label}")
    else:
        FAIL[0] += 1
        print(f"  NG   {label}  {detail}")


def counter():
    box = {"n": 0}

    def produce():
        box["n"] += 1
        return {"call": box["n"]}
    return box, produce


# ================================================================
print("=" * 68)
print("1. 同じ読み取りは 1 回で済む")
print("=" * 68)

bs.invalidate_read_cache()
box, produce = counter()
first = bs.cached_read(("k",), produce)
second = bs.cached_read(("k",), produce)
check("2 回目は照会しない", box["n"] == 1 and first == second, f"n={box['n']}")

other = bs.cached_read(("other",), produce)
check("鍵が違えば別に取る", box["n"] == 2 and other != first, f"n={box['n']}")

box2, produce2 = counter()
bs.cached_read(("z",), produce2, ttl=0)
bs.cached_read(("z",), produce2, ttl=0)
check("ttl=0 なら共有しない", box2["n"] == 2, f"n={box2['n']}")

# ================================================================
print("=" * 68)
print("2. 送ったら必ず捨てる(ここが安全条件の要)")
print("=" * 68)

bs.invalidate_read_cache()
box, produce = counter()
bs.cached_read(("k",), produce)
bs.invalidate_read_cache()
bs.cached_read(("k",), produce)
check("invalidate 後は取り直す", box["n"] == 2, f"n={box['n']}")

# engine が order.py を起動するとき、**実送信でもドライランでも**捨てること。
for confirm in (True, False):
    bs.invalidate_read_cache()
    bs.cached_read(("sentinel",), lambda: {"stale": True})
    spawned = {}

    def fake_run(command, **kwargs):
        # 起動の時点でキャッシュが空でなければ、送信後の再照会が古い値に当たる。
        spawned["cacheEmptyAtSpawn"] = not bs._READ_CACHE
        raise RuntimeError("送信はしない(テスト)")

    original = ae.subprocess.run
    try:
        ae.subprocess.run = fake_run
        ae._run_order(["--status"], confirm)
    finally:
        ae.subprocess.run = original
    check(f"order.py 起動時に共有は空 (confirm={confirm})",
          spawned.get("cacheEmptyAtSpawn") is True, str(spawned))

# ================================================================
print("=" * 68)
print("3. 共有してよい経路 / いけない経路")
print("=" * 68)

check("残高は共有版がある", hasattr(bs, "query_balance_cached"))
check("建玉も共有版がある", hasattr(bs, "query_position_cached"))
check("注文も共有版がある(R114: 観測用)", hasattr(bs, "query_orders_cached"))
check("生の query_position は残っている(送信の検証用)",
      hasattr(bs, "query_position") and bs.query_position is not bs.query_position_cached)

# R114: engine の **観測** は共有版でよい。生でなければならないのは
#   (a) 送信後(= _run_order が捨てるので自動的に生)
#   (b) 証明用の二度読み(= live_reads() の中)
# の 2 つで、その 2 つが守られていることを test_r114_cycle_snapshot.py で固定する。
import inspect  # noqa: E402
source = inspect.getsource(ae._scoped_position_query) + inspect.getsource(ae._scoped_order_query)
check("engine の観測は共有版を既定にする(R114)",
      "query_position_cached" in source and "query_orders_cached" in source, source[:160])
stable = inspect.getsource(ae._stable_broker_snapshot)
check("証明用の三点読みは生読み文脈の中(R114)", "_live_broker_reads" in stable)

# 戦績記録(会計)と表示は共有版でよい。
journal = inspect.getsource(trade_journal.reconcile)
check("戦績記録は共有版を既定にする",
      "query_position_cached" in journal and "query_balance_cached" in journal)

import monitor_publish  # noqa: E402
display = inspect.getsource(monitor_publish.live_position_line)
check("表示は共有版を使う", "query_position_cached" in display)

print("=" * 68)
if FAIL[0]:
    print(f"FAILED {FAIL[0]} / {PASS[0] + FAIL[0]}")
    sys.exit(1)
print(f"ALL PASS ({PASS[0]})")

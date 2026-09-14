# -*- coding: utf-8 -*-
"""R90 の再生(読むだけ)。監査バンドルを現行コードで再評価し、方針の有無で何が変わるかを出す。

    python replay_stop_logic.py                # 全バンドル(数分)。結果は --cache へ保存
    python replay_stop_logic.py --limit 200    # 直近 200 本だけ
    python replay_stop_logic.py --cache PATH   # 再評価の結果を JSON へ(2 回目以降は読むだけ)

変種: BASE(全部 OFF)/ VWAP(vwapClearance LIVE)/ FLIP(flipOrigin LIVE)/ BOTH。
成績は entry_depth.simulate と同じ保守的規則(確定 3 分足・指値は 1tick 突き抜け・約定前 TP1 で
取消・同じ足は SL 優先・指値待ち 30 分・6 時間)。R は**計画の** SL 幅で割る。
穴 2(成行の SL 距離ゲート)は各変種の ARMED セットアップに後掛けで当て、ゲートが通る最初の
周期から建てる(全周期で塞がれば NO_TRADE)。

ネットワーク・台帳・発注に触れない。msnr_gate.stop_logic_policy を差し替えて方針を注入する。
"""
from __future__ import annotations

import argparse
import copy
import glob
import io
import json
import os
import random
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

BASE = os.path.dirname(os.path.abspath(__file__))
SECRETS = os.path.join(BASE, ".secrets")
JST = timezone(timedelta(hours=9))

VARIANTS = {
    "BASE": {"vwapClearance": {"mode": "OFF"}, "marketStopGuard": {"mode": "OFF"}, "flipOrigin": {"mode": "OFF"}},
    "VWAP": {"vwapClearance": {"mode": "LIVE"}, "marketStopGuard": {"mode": "OFF"}, "flipOrigin": {"mode": "OFF"}},
    "FLIP": {"vwapClearance": {"mode": "OFF"}, "marketStopGuard": {"mode": "OFF"}, "flipOrigin": {"mode": "LIVE"}},
    "BOTH": {"vwapClearance": {"mode": "LIVE"}, "marketStopGuard": {"mode": "OFF"}, "flipOrigin": {"mode": "LIVE"}},
}


def _instant(value: Any) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _finite(value: Any) -> Optional[float]:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed == parsed and abs(parsed) != float("inf") else None


def evaluate_all(paths: List[str]) -> List[Dict[str, Any]]:
    import msnr_gate
    import stop_logic
    policies = {name: stop_logic.load_policy({"stopLogic": spec}) for name, spec in VARIANTS.items()}
    saved = msnr_gate.stop_logic_policy
    rows = []
    started = time.time()
    try:
        for index, path in enumerate(paths, 1):
            try:
                bundle = json.load(io.open(path, encoding="utf-8"))
            except (OSError, ValueError):
                continue
            moment = _instant(bundle.get("at"))
            if moment is None:
                continue
            row = {"path": os.path.basename(path), "at": moment.isoformat(), "t": int(moment.timestamp()),
                   "price": _finite(bundle.get("price")), "vwap": _finite(bundle.get("vwap")),
                   "noise": None, "recorded": None, "variants": {}}
            recorded = (bundle.get("scenarios") or {}).get("primary") or {}
            if recorded.get("model"):
                row["recorded"] = {k: recorded.get(k) for k in ("model", "side", "state", "grade", "entry", "stop",
                                                                 "targets", "decisionId")}
            for name, policy in policies.items():
                msnr_gate.stop_logic_policy = (lambda p=policy: p)
                try:
                    result = msnr_gate.evaluate(copy.deepcopy(bundle))
                except Exception as exc:  # noqa: BLE001 - 再生は落とさず記録する
                    row["variants"][name] = {"error": f"{type(exc).__name__}: {exc}"}
                    continue
                if row["noise"] is None:
                    row["noise"] = result.get("noiseFloor")
                decision = result.get("decision") or {}
                row["variants"][name] = {
                    k: decision.get(k) for k in ("model", "side", "state", "grade", "entry", "stop", "targets",
                                                 "targetR", "decisionId", "hardBlockers", "evidence", "vwapStop")}
            rows.append(row)
            if index % 100 == 0:
                print(f"  {index}/{len(paths)} ({time.time() - started:.0f}s)", flush=True)
    finally:
        msnr_gate.stop_logic_policy = saved
    return rows


# ---------------------------------------------------------------- 成績

def setups_for(rows: List[Dict[str, Any]], variant: str) -> List[Dict[str, Any]]:
    """連続周期の同じ (model, side, entry, stop) を 1 セットアップ(最初の周期の幾何)にまとめる。"""
    out: List[Dict[str, Any]] = []
    for row in rows:
        dec = row["variants"].get(variant) or {}
        if not dec.get("model") or dec.get("model") == "FLAT":
            continue
        entry, stop = _finite(dec.get("entry")), _finite(dec.get("stop"))
        targets = [_finite(t) for t in dec.get("targets") or []]
        if entry is None or stop is None or len(targets) < 2 or None in targets:
            continue
        key = (dec["model"], dec["side"], entry, stop)
        cycle = {"t": row["t"], "price": row["price"], "noise": row["noise"], "state": dec.get("state"),
                 "grade": dec.get("grade"), "blockers": list(dec.get("hardBlockers") or []),
                 "evidence": list(dec.get("evidence") or []), "decisionId": dec.get("decisionId"),
                 "vwapStop": dec.get("vwapStop") or {}, "at": row["at"]}
        if out and out[-1]["key"] == key and row["t"] - out[-1]["last"] <= 20 * 60:
            out[-1]["last"] = row["t"]
            out[-1]["cycles"].append(cycle)
            continue
        out.append({"key": key, "model": dec["model"], "side": dec["side"], "entry": entry, "stop": stop,
                    "targets": targets, "first": row["t"], "last": row["t"], "cycles": [cycle],
                    "signal": (dec["model"], dec["side"], entry, row["at"])})
    return out


def _bootstrap(base: List[float], alt: List[float], n: int = 2000, seed: int = 90) -> Tuple[float, float, float, float]:
    rnd = random.Random(seed)
    m = len(base)
    if not m:
        return 0.0, 0.0, 0.0, 0.0
    diffs = sorted(sum(alt[i] - base[i] for i in idx) / m
                   for idx in ([rnd.randrange(m) for _ in range(m)] for _ in range(n)))
    return (sum(a - b for a, b in zip(alt, base)) / m, diffs[int(0.05 * n)], diffs[int(0.95 * n)],
            sum(1 for d in diffs if d > 0) / n)


def simulate_setup(setup: Dict[str, Any], bars, times, guard_n: Optional[float], rest_sec: int) -> Dict[str, Any]:
    """ARMED の最初の周期から建てる。guard_n があれば穴 2 のゲートを通る最初の周期から。"""
    import entry_depth
    armed = [c for c in setup["cycles"] if str(c.get("state") or "").upper() in {"ARMED", "ACTIVE"}]
    if not armed:
        return {"outcome": "NOT_ARMED"}
    side, entry, stop = setup["side"], setup["entry"], setup["stop"]
    skipped = 0
    for cycle in armed:
        price = cycle.get("price")
        market = entry_depth.executable(side, entry, price)
        if market and guard_n is not None:
            nf = cycle.get("noise")
            dist = (price - stop) if side == "BUY" else (stop - price)
            if nf is None or nf <= 0 or dist < guard_n * nf - 1e-9:
                skipped += 1
                continue
        result = entry_depth.simulate(side, entry, stop, setup["targets"], bars, cycle["t"], rest_sec,
                                      market_price=price if market else None, times=times)
        result["guardSkippedCycles"] = skipped
        result["market"] = bool(market)
        result["startAt"] = cycle["at"]
        return result
    return {"outcome": "GUARD_BLOCKED_ALL", "guardSkippedCycles": skipped}


def report(rows: List[Dict[str, Any]], rest_min: int = 30) -> None:
    import entry_depth
    bars = entry_depth.load_bars(SECRETS)
    times = [b["t"] for b in bars]
    print(f"\nbundles {len(rows)} ({rows[0]['at'][:16]} → {rows[-1]['at'][:16]} UTC) / bars {len(bars)} / "
          f"limit rest {rest_min}min / same-bar SL first / R = planned risk")

    # 1. 判定の一致(BASE vs 記録)と、変種ごとの変化
    agree = sum(1 for r in rows if r["recorded"] and (r["variants"].get("BASE") or {}).get("decisionId") == r["recorded"].get("decisionId"))
    rec = sum(1 for r in rows if r["recorded"])
    print(f"BASE vs recorded primary decisionId: {agree}/{rec} agree")
    for name in ("VWAP", "FLIP", "BOTH"):
        changed = stop_moved = armed_to_watch = watch_to_armed = 0
        cap = 0
        for r in rows:
            b, v = r["variants"].get("BASE") or {}, r["variants"].get(name) or {}
            if not b.get("model") and not v.get("model"):
                continue
            if b.get("decisionId") != v.get("decisionId"):
                changed += 1
            if b.get("model") == v.get("model") and b.get("side") == v.get("side") and b.get("stop") != v.get("stop"):
                stop_moved += 1
            bs, vs = str(b.get("state") or ""), str(v.get("state") or "")
            if bs in {"ARMED", "ACTIVE"} and vs not in {"ARMED", "ACTIVE"}:
                armed_to_watch += 1
            if vs in {"ARMED", "ACTIVE"} and bs not in {"ARMED", "ACTIVE"}:
                watch_to_armed += 1
            if "RISK_CAP_EXCEEDED" in (v.get("hardBlockers") or []) and "RISK_CAP_EXCEEDED" not in (b.get("hardBlockers") or []):
                cap += 1
        print(f"{name:5s}: cycles with primary decisionId changed {changed}, primary stop moved {stop_moved}, "
              f"ARMED→WATCH {armed_to_watch}, WATCH→ARMED {watch_to_armed}, new RISK_CAP_EXCEEDED {cap}")

    # 2. 成績(セットアップ単位、シグナル = (model, side, entry, 最初の周期) で対応付け)
    def collect(name: str, guard_n: Optional[float]) -> Dict[Tuple, Dict[str, Any]]:
        table = {}
        for setup in setups_for(rows, name):
            res = simulate_setup(setup, bars, times, guard_n, rest_min * 60)
            table[setup["signal"]] = {"setup": setup, "res": res}
        return table

    print("\n--- setup results (ARMED first cycle; R per setup, 0 = no trade) ---")
    base = collect("BASE", None)
    base_guard = collect("BASE", 1.0)
    tables = {"BASE": base, "BASE+GUARD": base_guard}
    for name in ("VWAP", "FLIP", "BOTH"):
        tables[name] = collect(name, None)
        tables[name + "+GUARD"] = collect(name, 1.0)

    def r_of(item: Optional[Dict[str, Any]]) -> float:
        if not item:
            return 0.0
        res = item["res"]
        return float(res.get("r") or 0.0) if res.get("outcome") == "FILLED" else 0.0

    keys = sorted(set(base) | set().union(*[set(t) for t in tables.values()]))
    armed_keys = [k for k in keys if any(k in t and t[k]["res"].get("outcome") not in {"NOT_ARMED"} for t in tables.values())]
    print(f"signals with ARMED in any variant: {len(armed_keys)}")
    base_r = [r_of(base.get(k)) for k in armed_keys]
    for name, table in tables.items():
        vals = [r_of(table.get(k)) for k in armed_keys]
        fills = sum(1 for k in armed_keys if (table.get(k) or {}).get("res", {}).get("outcome") == "FILLED")
        tp1 = sum(1 for k in armed_keys if (table.get(k) or {}).get("res", {}).get("tp1"))
        losses = sum(1 for k in armed_keys if (table.get(k) or {}).get("res", {}).get("outcome") == "FILLED"
                     and not (table.get(k) or {}).get("res", {}).get("tp1") and r_of(table.get(k)) < 0)
        blocked = sum(1 for k in armed_keys if (table.get(k) or {}).get("res", {}).get("outcome") == "GUARD_BLOCKED_ALL")
        mean, lo, hi, prob = _bootstrap(base_r, vals)
        print(f"{name:10s} ΣR={sum(vals):+7.2f} fills={fills:3d} tp1={tp1:3d} losses={losses:3d} guardBlocked={blocked:3d}"
              f" | vs BASE {mean:+.3f}R/signal [{lo:+.2f},{hi:+.2f}] P(improve)={prob:.2f}")

    # 2b. 塊の先頭だけ(同じ (model, side) が 20 分以内に撃ち直された塊は 1 件)と、
    #     1 ポジションずつの逐次再生(建玉がある間・指値が休んでいる間は新規を取らない)。
    #     セットアップ単位の合計は同じシグナルを周期ごとに数え直す(09-14 23:15/19/22/25 の
    #     BREAKER は 4 件)ので、こちらの 2 つが「実際に取れた数」に近い。
    print("\n--- cluster view (first of same (model, side) within 20 min) / sequential (one position at a time) ---")
    for name, table in tables.items():
        items = sorted(table.values(), key=lambda it: it["setup"]["first"])
        last_by_side: Dict[Tuple[str, str], int] = {}
        cluster_r, cluster_n = 0.0, 0
        busy_until, seq_r, seq_n, seq_fills, seq_tp1 = 0, 0.0, 0, 0, 0
        for item in items:
            setup, res = item["setup"], item["res"]
            if res.get("outcome") in {"NOT_ARMED"}:
                continue
            key = (setup["model"], setup["side"])
            if key not in last_by_side or setup["first"] - last_by_side[key] > 20 * 60:
                cluster_n += 1
                cluster_r += r_of(item)
            last_by_side[key] = setup["first"]
            # 逐次: 建玉(約定〜決済)と休んでいる指値(rest_min)の間は次を取らない
            start = setup["first"]
            if start < busy_until:
                continue
            seq_n += 1
            if res.get("outcome") == "FILLED":
                seq_fills += 1
                seq_tp1 += 1 if res.get("tp1") else 0
                seq_r += float(res.get("r") or 0.0)
                busy_until = int(res.get("exitT") or start) + 180
            elif res.get("outcome") == "NO_FILL":
                busy_until = start + rest_min * 60
        print(f"{name:10s} cluster: n={cluster_n:3d} ΣR={cluster_r:+7.2f} | sequential: taken={seq_n:3d} "
              f"fills={seq_fills:3d} tp1={seq_tp1:3d} ΣR={seq_r:+7.2f}")

    # 3. どのシグナルが変わったか(VWAP / FLIP で結果が変わったものだけ)
    print("\n--- signals whose outcome changed (BASE → variant) ---")
    for name in ("VWAP", "FLIP", "BASE+GUARD"):
        table = tables[name]
        print(f"[{name}]")
        shown = 0
        for k in armed_keys:
            b, v = base.get(k), table.get(k)
            rb, rv = r_of(b), r_of(v)
            ob = (b or {}).get("res", {}).get("outcome")
            ov = (v or {}).get("res", {}).get("outcome")
            if abs(rb - rv) < 1e-9 and ob == ov:
                continue
            setup = (v or b)["setup"]
            at = datetime.fromisoformat(k[3]).astimezone(JST)
            sb = (b or {}).get("setup", {}).get("stop")
            sv = (v or {}).get("setup", {}).get("stop")
            print(f"  {at:%m-%d %H:%M} {k[0][:18]:18s} {k[1]:4s} E {k[2]:,.2f} SL {sb if sb is not None else '-'} → {sv if sv is not None else '-'}"
                  f" | {ob} {rb:+.2f}R → {ov} {rv:+.2f}R")
            shown += 1
        if not shown:
            print("  (none)")

    # 4. 09-14 23:26 の当該周期
    print("\n--- 2026-09-14 14:25 UTC (23:25 JST) cycle ---")
    for r in rows:
        if r["at"].startswith("2026-09-14T14:25"):
            for name, dec in r["variants"].items():
                print(f"  {name:5s} {dec.get('model')} {dec.get('side')} {dec.get('state')} {dec.get('grade')} "
                      f"E {dec.get('entry')} SL {dec.get('stop')} T {dec.get('targets')} R {dec.get('targetR')} "
                      f"blockers {dec.get('hardBlockers')} vwapStop {dec.get('vwapStop')}")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="R90 replay (read-only)")
    parser.add_argument("--limit", type=int, default=0, help="直近 N 本だけ")
    parser.add_argument("--cache", default=os.path.join(BASE, ".secrets", "r90_replay_cache.json"),
                        help="再評価結果の保存先(存在すれば読むだけ)")
    parser.add_argument("--rest", type=int, default=30, help="指値を待つ分数")
    parser.add_argument("--reevaluate", action="store_true", help="キャッシュを無視して再評価")
    args = parser.parse_args(argv)
    rows = None
    if not args.reevaluate and os.path.exists(args.cache):
        rows = json.load(io.open(args.cache, encoding="utf-8"))
        print(f"loaded cache {args.cache} ({len(rows)} rows)")
    if rows is None:
        paths = sorted(glob.glob(os.path.join(SECRETS, "monitor_cycle_*.json")),
                       key=lambda p: (json.load(io.open(p, encoding="utf-8")).get("at") or ""))
        if args.limit:
            paths = paths[-args.limit:]
        print(f"evaluating {len(paths)} bundles × {len(VARIANTS)} variants …", flush=True)
        rows = evaluate_all(paths)
        rows.sort(key=lambda r: r["t"])
        with io.open(args.cache, "w", encoding="utf-8") as fh:
            json.dump(rows, fh, ensure_ascii=False)
        print(f"cached → {args.cache}")
    report(rows, rest_min=args.rest)
    return 0


if __name__ == "__main__":
    sys.exit(main())

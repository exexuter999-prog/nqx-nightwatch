# -*- coding: utf-8 -*-
"""R103 の再生(読むだけ)。監査バンドルを現行コードで再評価し、契約スイッチの変種ごとの成績を
R90 と同じ規則(replay_stop_logic / entry_depth)で比べる。

    python replay_r103.py                 # 全バンドル(数十分)。結果は --cache へ保存
    python replay_r103.py --limit 300     # 直近 300 本だけ
    python replay_r103.py --cache PATH    # 再評価の結果を JSON へ(2 回目以降は読むだけ)

変種(すべて vwapClearance LIVE・marketStopGuard は後掛け 1.0N = 本番と同じ):
  BASE     poolClearance OFF / sweepGate OFF(= R103 以前の判定)
  POOL     poolClearance LIVE(SL をプールの向こう +0.25N へ)
  SWEEP    sweepGate LIVE(プールが SL の外側 1N 以内なら掃引→奪還を待ち、SL は掃引極値 +0.25N)
  SWEEP1N  sweepGate LIVE で sweepStopN=1.0
  BOTH     POOL + SWEEP

成績は entry_depth.simulate の保守的規則(確定 3 分足・指値は 1tick 突き抜け・約定前 TP1 で取消・
同じ足は SL 優先・指値待ち 30 分・6 時間)。R は**計画の** SL 幅で割る。結論は「逐次」で語る。
ネットワーク・台帳・発注に触れない。msnr_gate.stop_logic_policy を差し替えて方針を注入する。
"""
from __future__ import annotations

import argparse
import copy
import glob
import io
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
SECRETS = os.path.join(BASE, ".secrets")
JST = timezone(timedelta(hours=9))

_COMMON = {"vwapClearance": {"mode": "LIVE"}, "marketStopGuard": {"mode": "OFF"}, "flipOrigin": {"mode": "OFF"}}
VARIANTS = {
    "BASE": {**_COMMON, "poolClearance": {"mode": "OFF"}, "sweepGate": {"mode": "OFF"}},
    "POOL": {**_COMMON, "poolClearance": {"mode": "LIVE"}, "sweepGate": {"mode": "OFF"}},
    "SWEEP": {**_COMMON, "poolClearance": {"mode": "OFF"}, "sweepGate": {"mode": "LIVE"}},
    "SWEEP1N": {**_COMMON, "poolClearance": {"mode": "OFF"}, "sweepGate": {"mode": "LIVE", "sweepStopN": 1.0}},
    "BOTH": {**_COMMON, "poolClearance": {"mode": "LIVE"}, "sweepGate": {"mode": "LIVE"}},
}
DECISION_KEYS = ("model", "side", "state", "grade", "entry", "stop", "targets", "targetR", "decisionId",
                 "hardBlockers", "evidence", "poolStop", "sweepGate")


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


def evaluate_all(paths: List[str], variants: Optional[Dict[str, Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    import msnr_gate
    import stop_logic
    variants = variants or VARIANTS
    policies = {name: stop_logic.load_policy({"stopLogic": spec}) for name, spec in variants.items()}
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
                   "price": _finite(bundle.get("price")), "noise": None, "recorded": None, "variants": {}}
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
                row["variants"][name] = {k: decision.get(k) for k in DECISION_KEYS}
            rows.append(row)
            if index % 100 == 0:
                print(f"  {index}/{len(paths)} ({time.time() - started:.0f}s)", flush=True)
    finally:
        msnr_gate.stop_logic_policy = saved
    return rows


def r_of(item: Optional[Dict[str, Any]]) -> float:
    if not item:
        return 0.0
    res = item["res"]
    return float(res.get("r") or 0.0) if res.get("outcome") == "FILLED" else 0.0


def report(rows: List[Dict[str, Any]], rest_min: int = 30, guard_n: Optional[float] = 1.0,
           secrets: str = SECRETS) -> None:
    import entry_depth
    import replay_stop_logic as r90
    bars = entry_depth.load_bars(secrets)
    times = [b["t"] for b in bars]
    names = list(VARIANTS)
    print(f"\nbundles {len(rows)} ({rows[0]['at'][:16]} → {rows[-1]['at'][:16]} UTC) / bars {len(bars)} / "
          f"limit rest {rest_min}min / market stop guard {guard_n}N / same-bar SL first / R = planned risk")
    agree = sum(1 for r in rows if r["recorded"] and (r["variants"].get("BASE") or {}).get("decisionId") == r["recorded"].get("decisionId"))
    rec = sum(1 for r in rows if r["recorded"])
    print(f"BASE vs recorded primary decisionId: {agree}/{rec} agree")

    # 1. 周期単位の変化
    for name in names[1:]:
        changed = stop_moved = armed_to_watch = watch_to_armed = pending = passed = 0
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
            blockers = v.get("hardBlockers") or []
            if "SWEEP_PENDING" in blockers or "SWEEP_STALE" in blockers:
                pending += 1
            if "SWEEP_GATE_PASSED" in (v.get("evidence") or []):
                passed += 1
        print(f"{name:8s}: decisionId changed {changed:4d} | stop moved {stop_moved:4d} | ARMED→WATCH {armed_to_watch:3d} | "
              f"WATCH→ARMED {watch_to_armed:3d} | sweep pending {pending:3d} | sweep passed {passed:3d}")

    # 2. セットアップ単位 → 塊 → 逐次
    tables: Dict[str, Dict[Tuple, Dict[str, Any]]] = {}
    for name in names:
        table = {}
        for setup in r90.setups_for(rows, name):
            res = r90.simulate_setup(setup, bars, times, guard_n, rest_min * 60)
            table[setup["signal"]] = {"setup": setup, "res": res}
        tables[name] = table
    base = tables["BASE"]
    keys = sorted(set().union(*[set(t) for t in tables.values()]))
    armed_keys = [k for k in keys if any(k in t and t[k]["res"].get("outcome") not in {"NOT_ARMED"} for t in tables.values())]
    print(f"\n--- setups (ARMED in any variant): {len(armed_keys)} ---")
    base_r = [r_of(base.get(k)) for k in armed_keys]
    for name, table in tables.items():
        vals = [r_of(table.get(k)) for k in armed_keys]
        fills = sum(1 for k in armed_keys if (table.get(k) or {}).get("res", {}).get("outcome") == "FILLED")
        tp1 = sum(1 for k in armed_keys if (table.get(k) or {}).get("res", {}).get("tp1"))
        losses = sum(1 for k in armed_keys if (table.get(k) or {}).get("res", {}).get("outcome") == "FILLED"
                     and not (table.get(k) or {}).get("res", {}).get("tp1") and r_of(table.get(k)) < 0)
        mean, lo, hi, prob = r90._bootstrap(base_r, vals)
        print(f"{name:8s} ΣR={sum(vals):+7.2f} fills={fills:3d} tp1={tp1:3d} losses={losses:3d}"
              f" | vs BASE {mean:+.3f}R/setup [{lo:+.2f},{hi:+.2f}] P(improve)={prob:.2f}")

    print("\n--- cluster (first of same (model, side) within 20 min) / sequential (one position at a time) ---")
    seq_rows = {}
    for name, table in tables.items():
        items = sorted(table.values(), key=lambda it: it["setup"]["first"])
        last_by_side: Dict[Tuple[str, str], int] = {}
        cluster_r, cluster_n = 0.0, 0
        busy_until, seq_r, seq_n, seq_fills, seq_tp1, seq_loss = 0, 0.0, 0, 0, 0, 0
        taken = []
        for item in items:
            setup, res = item["setup"], item["res"]
            if res.get("outcome") in {"NOT_ARMED"}:
                continue
            key = (setup["model"], setup["side"])
            if key not in last_by_side or setup["first"] - last_by_side[key] > 20 * 60:
                cluster_n += 1
                cluster_r += r_of(item)
            last_by_side[key] = setup["first"]
            start = setup["first"]
            if start < busy_until:
                continue
            seq_n += 1
            if res.get("outcome") == "FILLED":
                seq_fills += 1
                rr = float(res.get("r") or 0.0)
                seq_tp1 += 1 if res.get("tp1") else 0
                seq_loss += 1 if (rr < 0 and not res.get("tp1")) else 0
                seq_r += rr
                busy_until = int(res.get("exitT") or start) + 180
                taken.append((start, setup["model"], setup["side"], setup["entry"], setup["stop"], rr, bool(res.get("tp1"))))
            elif res.get("outcome") == "NO_FILL":
                busy_until = start + rest_min * 60
        seq_rows[name] = taken
        print(f"{name:8s} cluster: n={cluster_n:3d} ΣR={cluster_r:+7.2f} | sequential: taken={seq_n:3d} "
              f"fills={seq_fills:3d} tp1={seq_tp1:3d} losses={seq_loss:3d} ΣR={seq_r:+7.2f}")

    # 2b. 前半 70% / 後半 30%(逐次)
    print("\n--- sequential split: first 70% / last 30% of time ---")
    t0, t1 = rows[0]["t"], rows[-1]["t"]
    cut = t0 + 0.7 * (t1 - t0)
    for name, taken in seq_rows.items():
        a = [x[5] for x in taken if x[0] < cut]
        b = [x[5] for x in taken if x[0] >= cut]
        print(f"{name:8s} first70: n={len(a):3d} ΣR={sum(a):+7.2f} | last30: n={len(b):3d} ΣR={sum(b):+7.2f}")

    # 3. どのシグナルが変わったか
    print("\n--- setups whose outcome changed (BASE → variant) ---")
    for name in names[1:]:
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
            at = datetime.fromisoformat(k[3]).astimezone(JST)
            sb = (b or {}).get("setup", {}).get("stop")
            sv = (v or {}).get("setup", {}).get("stop")
            print(f"  {at:%m-%d %H:%M} {k[0][:18]:18s} {k[1]:4s} E {k[2]:,.2f} SL {sb if sb is not None else '-'} → {sv if sv is not None else '-'}"
                  f" | {ob} {rb:+.2f}R → {ov} {rv:+.2f}R")
            shown += 1
            if shown >= 40:
                print("  …")
                break
        if not shown:
            print("  (none)")

    # 4. 09-15 の 4 周期
    print("\n--- 2026-09-15 armed cycles (T1 07:48Z / T2 12:19Z / T3 12:31Z / T4 13:31Z) ---")
    for r in rows:
        if any(r["at"].startswith(p) for p in ("2026-09-15T07:48", "2026-09-15T12:19", "2026-09-15T12:31", "2026-09-15T13:31")):
            for name, dec in r["variants"].items():
                sg = dec.get("sweepGate") or {}
                print(f"  {r['at'][:16]} {name:8s} {dec.get('model')} {dec.get('side')} {dec.get('state')} {dec.get('grade')} "
                      f"E {dec.get('entry')} SL {dec.get('stop')} blockers {dec.get('hardBlockers')} "
                      f"gate {sg.get('state')} pool {(sg.get('pool') or {}).get('price')} req {sg.get('required')}")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="R103 replay (read-only)")
    parser.add_argument("--limit", type=int, default=0, help="直近 N 本だけ")
    parser.add_argument("--cache", default=os.path.join(BASE, ".secrets", "r103_replay_cache.json"),
                        help="再評価結果の保存先(存在すれば読むだけ)")
    parser.add_argument("--secrets", default=SECRETS, help="監査コピーのディレクトリ")
    parser.add_argument("--rest", type=int, default=30, help="指値を待つ分数")
    parser.add_argument("--reevaluate", action="store_true", help="キャッシュを無視して再評価")
    args = parser.parse_args(argv)
    secrets = args.secrets
    rows = None
    if not args.reevaluate and os.path.exists(args.cache):
        rows = json.load(io.open(args.cache, encoding="utf-8"))
        print(f"loaded cache {args.cache} ({len(rows)} rows)")
    if rows is None:
        paths = sorted(glob.glob(os.path.join(secrets, "monitor_cycle_*.json")),
                       key=lambda p: (json.load(io.open(p, encoding="utf-8")).get("at") or ""))
        if args.limit:
            paths = paths[-args.limit:]
        print(f"evaluating {len(paths)} bundles × {len(VARIANTS)} variants …", flush=True)
        rows = evaluate_all(paths)
        rows.sort(key=lambda r: r["t"])
        os.makedirs(os.path.dirname(args.cache) or ".", exist_ok=True)
        with io.open(args.cache, "w", encoding="utf-8") as fh:
            json.dump(rows, fh, ensure_ascii=False)
        print(f"cached → {args.cache}")
    report(rows, rest_min=args.rest, secrets=secrets)
    return 0


if __name__ == "__main__":
    sys.exit(main())

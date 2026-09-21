# -*- coding: utf-8 -*-
"""上位足の水準を**利確目標の候補にだけ**足したら結果が変わるか(読むだけ・通信なし)。

2026-09-21 16:31 の BREAKER BUY A+ は、上に London High(+0.8R)しか無く
`TARGET_HEADROOM_INSUFFICIENT` で WATCH になった。水準マップはセッション系 9 本だけで、
前日高安や上位足のスイングは目標候補に入っていない。これを足したら武装と損益が
どう変わるかを、監査バンドルの逐次再生で確かめる。

本体の実装(`htf_targets` + `msnr_gate.model_targets` の穴埋め)をそのまま使い、
`htf_targets.policy` だけを条件ごとに差し替える。契約も本体も書き換えない。

  A_NOW   穴埋めなし(R124 以前)
  B_FILL  前日高安・前週高安だけで穴埋め
  C_FILL  全ソース(+ 未回収の日足スイング・1h/4h の 20 本レンジ高安)で穴埋め = 本番

「常に足す」形は 2026-09-21 の初回比較で悪化したので本体に実装していない
(docs/R124_HTF_TARGET_FILL.md)。

    python replay_htf_targets.py            # 再評価(キャッシュがあれば使う)
    python replay_htf_targets.py --reevaluate
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
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
SECRETS = os.path.join(BASE, ".secrets")
DAY = 86400
#: 条件 -> htfTargets の節(本体の policy にそのまま渡す)
SPECS = {"A_NOW": {"mode": "OFF"},
         "B_FILL": {"mode": "LIVE", "sources": ["PREV_DAY", "PREV_WEEK"]},
         "C_FILL": {"mode": "LIVE"}}
VARIANTS = tuple(SPECS)
KEYS = ("model", "side", "state", "grade", "score", "entry", "stop", "targets", "targetR",
        "decisionId", "hardBlockers", "entryMode")


def _f(value: Any) -> Optional[float]:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out else None


def _ts(value: Any) -> Optional[float]:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def evaluate_all(paths: List[str]) -> List[Dict[str, Any]]:
    import htf_targets
    import msnr_gate
    original = htf_targets.policy

    rows: List[Dict[str, Any]] = []
    started = time.time()
    try:
        for index, path in enumerate(paths, 1):
            try:
                bundle = json.load(io.open(path, encoding="utf-8"))
            except (OSError, ValueError):
                continue
            t = _ts(bundle.get("at"))
            if t is None:
                continue
            # 15 分足は渡さない(本番の親は直接取得のみ。監査コーパスには無い)。
            (bundle.get("snapshot") or {}).pop("bars15m", None)
            row = {"path": os.path.basename(path), "at": bundle.get("at"), "t": int(t),
                   "price": _f(bundle.get("price")), "noise": None, "variants": {}}
            for name in VARIANTS:
                htf_targets.policy = (lambda contract=None, spec=SPECS[name]:
                                      original({"htfTargets": spec}))
                try:
                    result = msnr_gate.evaluate(copy.deepcopy(bundle))
                except Exception as exc:  # noqa: BLE001 — 再生は落とさず記録する
                    row["variants"][name] = {"error": f"{type(exc).__name__}: {exc}"}
                    continue
                if row["noise"] is None:
                    row["noise"] = result.get("noiseFloor")
                decision = result.get("decision") or {}
                row["variants"][name] = {k: decision.get(k) for k in KEYS}
                row["variants"][name]["restingLimit"] = bool(decision.get("restingLimit"))
                row["variants"][name]["headroomCandidates"] = sum(
                    1 for c in (result.get("candidates") or [])
                    if "TARGET_HEADROOM_INSUFFICIENT" in (c.get("hardBlockers") or []))
            rows.append(row)
            if index % 200 == 0:
                print(f"  {index}/{len(paths)} ({time.time() - started:.0f}s)", flush=True)
    finally:
        htf_targets.policy = original
    return rows


def report(rows: List[Dict[str, Any]], rest_min: int, guard_n: float) -> None:
    import entry_depth
    import replay_selection as rs
    bars = entry_depth.load_bars(SECRETS)
    times = [b["t"] for b in bars]
    fee = rs.fee_per_side()
    print(f"対象 {len(rows)} 周期({rows[0]['at'][:16]} → {rows[-1]['at'][:16]} UTC)")

    print("\n--- 周期単位(primary) ---")
    print(f"{'条件':<7} {'ARMED':>6} {'primaryが目標不足':>16} {'目標不足の候補(延べ)':>18}")
    for name in VARIANTS:
        dec = [r["variants"].get(name) or {} for r in rows]
        armed = sum(1 for d in dec if str(d.get("state")) in ("ARMED", "ACTIVE"))
        head = sum(1 for d in dec if "TARGET_HEADROOM_INSUFFICIENT" in (d.get("hardBlockers") or []))
        cand = sum(int(d.get("headroomCandidates") or 0) for d in dec)
        print(f"{name:<7} {armed:>6} {head:>16} {cand:>18}")

    print("\n--- 逐次(同時に 1 建玉 / 1 注文。手数料込み、滑りは片道 tick) ---")
    print(f"{'滑り':>4} {'条件':<7} {'発注':>4} {'約定':>4} {'TP1':>4} {'負け':>4} {'ΣR':>7} {'最大DD':>7} {'最良除くΣR':>10}")
    results = {}
    for slip in (0, 1, 2):
        cost = rs.cost_points(fee, slip)
        for name in VARIANTS:
            out = rs.sequential(rows, name, bars, times, guard_n, rest_min * 60, cost)
            results[(slip, name)] = out
            rs_ = [t["r"] for t in out["trades"]]
            ex_best = sum(rs_) - max(rs_) if rs_ else 0.0
            print(f"{slip:>4} {name:<7} {out['taken']:>4} {out['filled']:>4} {out['tp1']:>4} "
                  f"{out['loss']:>4} {out['sumR']:>+7.2f} {out['maxDD']:>+7.2f} {ex_best:>+10.2f}")

    print("\n--- A_NOW と違うトレード(滑り 1 tick) ---")
    base = {t["decisionId"]: t for t in results[(1, "A_NOW")]["trades"]}
    for name in VARIANTS[1:]:
        trades = results[(1, name)]["trades"]
        ids = {t["decisionId"] for t in trades}
        added = [t for t in trades if t["decisionId"] not in base]
        changed = [t for t in trades if t["decisionId"] in base
                   and (t["targets"] != base[t["decisionId"]]["targets"]
                        or abs(t["r"] - base[t["decisionId"]]["r"]) > 1e-9)]
        lost = [t for d, t in base.items() if d not in ids]
        print(f"[{name}] 新規 {len(added)} 件 ΣR {sum(t['r'] for t in added):+.2f} / "
              f"目標が変わった {len(changed)} 件 ΔR "
              f"{sum(t['r'] - base[t['decisionId']]['r'] for t in changed):+.2f} / "
              f"消えた {len(lost)} 件 ΣR {sum(t['r'] for t in lost):+.2f}")
        for t in sorted(added + changed, key=lambda x: x["t"]):
            tag = "新規" if t in added else "変更"
            prior = base.get(t["decisionId"])
            print(f"   {tag} {t['at'][:16]} {t['model']} {t['side']} E={t['entry']} SL={t['stop']} "
                  f"TP={t['targets']} R={t['r']:+.2f}"
                  + (f"(A: TP={prior['targets']} R={prior['r']:+.2f})" if prior else ""))


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--reevaluate", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--rest-min", type=int, default=30)
    parser.add_argument("--guard-n", type=float, default=1.0)
    parser.add_argument("--cache", default=os.path.join(SECRETS, "replay_htf_targets.json"))
    args = parser.parse_args(argv)
    paths = sorted(glob.glob(os.path.join(SECRETS, "monitor_cycle_*.json")))
    if args.limit:
        paths = paths[:args.limit]
    rows: List[Dict[str, Any]] = []
    if not args.reevaluate and os.path.exists(args.cache):
        try:
            payload = json.load(io.open(args.cache, encoding="utf-8"))
            if payload.get("count") == len(paths) and payload.get("schema") == "HTF-TARGETS/3":
                rows = payload["rows"]
        except (OSError, ValueError, KeyError):
            rows = []
    if not rows:
        print(f"再評価 {len(paths)} バンドル × {len(VARIANTS)} 条件 …", flush=True)
        rows = evaluate_all(paths)
        try:
            json.dump({"schema": "HTF-TARGETS/3", "count": len(paths), "rows": rows},
                      io.open(args.cache, "w", encoding="utf-8"), ensure_ascii=False, default=str)
        except OSError:
            pass
    rows = [r for r in rows if r.get("variants")]
    rows.sort(key=lambda r: r["t"])
    if not rows:
        print("対象バンドルなし")
        return 2
    report(rows, args.rest_min, args.guard_n)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

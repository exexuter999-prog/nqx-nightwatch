# -*- coding: utf-8 -*-
"""R121 の逐次再生(読むだけ)。ICT STDV の 3 条件を**同条件**で比べる。

    python replay_ict_stdv.py --replay                  # 全バンドル
    python replay_ict_stdv.py --replay --limit 400
    python replay_ict_stdv.py --replay --reevaluate      # キャッシュを無視

条件(差し替えるのは `msnr_gate.ict_stdv_policy` **だけ**):

  BASE      ictStdv OFF(= R121 以前の判定)
  SHADOW    mode=SHADOW(アンカーと投影を記録するだけ。注文意図は BASE と同一のはず)
  TARGETS   mode=LIVE / targets=LIVE(投影を既存 model_targets の目標候補へ渡す)
  TGT_RUN   TARGETS + runnerEligible=true(参考。`-4` 相当の遠い runner を許す)

同条件の意味: 同じ監査バンドル、**実際に効く** riskCap / stopLogic / limitGate(契約から
そのまま読む)、`entry_depth.simulate` の同じ約定規則、同じ逐次 1 建玉制約、R は計画の
SL 幅。`replay_r103.py` / `replay_stop_logic.py` / `replay_limit_gate.py` と同じ規則。

**手数料とスリッページ**: `entry_depth.simulate` は指値の 1tick 突き抜けと同じ足 SL 優先で
保守側に寄せるだけで、手数料も滑りも入れていない。片道 1 枚の手数料は口座ごとの
`FEE_PER_SIDE_*`(R85)で、再生では枚数が決まらないので R では表現しない。**この再生の
ΣR に費用は含まれていない**ことを結論に添える。

ネットワーク・台帳への書き込み・発注には触れない。
"""
from __future__ import annotations

import argparse
import collections
import copy
import glob
import io
import json
import os
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
SECRETS = os.path.join(BASE, ".secrets")
JST = timezone(timedelta(hours=9))

RATIOS = [-1.0, -2.0, -2.5]
VARIANTS: Dict[str, Dict[str, Any]] = {
    "BASE": {"mode": "OFF", "targets": {"mode": "OFF"}, "participation": {"mode": "OFF"}},
    "SHADOW": {"mode": "SHADOW", "targets": {"mode": "OFF"}, "participation": {"mode": "OFF"}},
    "TARGETS": {"mode": "LIVE", "targets": {"mode": "LIVE", "ratios": RATIOS,
                                            "runnerEligible": False},
                "participation": {"mode": "OFF"}},
    "TGT_RUN": {"mode": "LIVE", "targets": {"mode": "LIVE", "ratios": RATIOS + [-4.0],
                                            "runnerEligible": True},
                "participation": {"mode": "OFF"}},
}
DECISION_KEYS = ("model", "side", "state", "grade", "entry", "stop", "targets", "targetR",
                 "decisionId", "hardBlockers", "evidence", "ictStdv")


def _finite(value: Any) -> Optional[float]:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out and abs(out) != float("inf") else None


def _instant(value: Any) -> Optional[datetime]:
    try:
        out = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return out if out.tzinfo else out.replace(tzinfo=timezone.utc)


def _compact(audit: Any) -> Optional[Dict[str, Any]]:
    """キャッシュ用に STDV 監査を縮める。アンカー全体は持たない。"""
    if not isinstance(audit, dict):
        return None
    anchor = audit.get("anchor") or {}
    read = audit.get("read") or {}
    return {
        "reason": audit.get("reason"),
        "anchorId": anchor.get("anchorId"),
        "p0": anchor.get("p0"), "p1": anchor.get("p1"), "widthPt": anchor.get("widthPt"),
        "knownAt": anchor.get("knownAt"), "confirmedAt": anchor.get("confirmedAt"),
        "projectionValid": anchor.get("projectionValid"),
        "consumedRatios": anchor.get("consumedRatios"),
        "priceSubstituted": anchor.get("priceSubstituted"),
        "byRatio": {str(k): v for k, v in (anchor.get("byRatio") or {}).items()},
        "targetsOffered": [row.get("price") for row in audit.get("targetsOffered") or []],
        "targetsApplied": bool(audit.get("targetsApplied")),
        "runnerRejected": bool(audit.get("runnerRejected")),
        "baseTargets": audit.get("baseTargets"),
        "participation": read.get("participation"),
        "participationReasons": read.get("participationReasons"),
        "thesis": read.get("thesis"),
        "headroom": read.get("headroom"),
    }


def evaluate_all(paths: List[str]) -> List[Dict[str, Any]]:
    import msnr_gate
    policies = {name: msnr_gate.ict_stdv_policy({"ictStdv": spec})
                for name, spec in VARIANTS.items()}
    bad = {n: p["invalid"] for n, p in policies.items() if p["invalid"]}
    if bad:
        raise SystemExit(f"変種の指定が不正: {bad}")
    saved = msnr_gate.ict_stdv_policy
    rows: List[Dict[str, Any]] = []
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
            row = {"path": os.path.basename(path), "at": moment.isoformat(),
                   "t": int(moment.timestamp()), "price": _finite(bundle.get("price")),
                   "noise": None, "variants": {}}
            for name, policy in policies.items():
                msnr_gate.ict_stdv_policy = (lambda p=policy: p)
                try:
                    result = msnr_gate.evaluate(copy.deepcopy(bundle))
                except Exception as exc:  # noqa: BLE001 - 再生は落とさず記録する
                    row["variants"][name] = {"error": f"{type(exc).__name__}: {exc}"}
                    continue
                if row["noise"] is None:
                    row["noise"] = result.get("noiseFloor")
                decision = result.get("decision") or {}
                item = {k: decision.get(k) for k in DECISION_KEYS if k != "ictStdv"}
                item["ictStdv"] = _compact(decision.get("ictStdv"))
                # 候補側の STDV も数える(primary にならなかった分の根拠欠損を見るため)
                item["candidateStdv"] = [_compact(c.get("ictStdv"))
                                         for c in (result.get("candidates") or [])
                                         if c.get("ictStdv")]
                row["variants"][name] = item
            rows.append(row)
            if index % 100 == 0:
                print(f"  {index}/{len(paths)} ({time.time() - started:.0f}s)", flush=True)
    finally:
        msnr_gate.ict_stdv_policy = saved
    return rows


def _r_of(item: Optional[Dict[str, Any]]) -> float:
    if not item:
        return 0.0
    res = item["res"]
    return float(res.get("r") or 0.0) if res.get("outcome") == "FILLED" else 0.0


def report(rows: List[Dict[str, Any]], rest_min: int = 30, guard_n: float = 1.0,
           secrets: str = SECRETS) -> None:
    import entry_depth
    import replay_stop_logic as r90
    bars = entry_depth.load_bars(secrets)
    times = [b["t"] for b in bars]
    names = list(VARIANTS)
    print(f"\nbundles {len(rows)} ({rows[0]['at'][:16]} → {rows[-1]['at'][:16]} UTC) / bars {len(bars)}")
    print(f"同条件: riskCap/stopLogic/limitGate は契約の実値 / limit rest {rest_min}min / "
          f"market stop guard {guard_n}N / same-bar SL first / R = planned risk / "
          f"**手数料・スリッページは含まない**")

    # 1. アンカーの数え上げ(SHADOW で観測。BASE は STDV を作らないので 0)
    print("\n--- 1. アンカー(SHADOW の観測) ---")
    eligible = collections.Counter()
    missing = collections.Counter()
    delays: List[int] = []
    consumed_at_confirm = 0
    subst = 0
    anchor_ids = set()
    for row in rows:
        for audit in (row["variants"].get("SHADOW") or {}).get("candidateStdv") or []:
            if not audit:
                continue
            if audit.get("projectionValid") is True:
                eligible["ok"] += 1
                anchor_ids.add(audit.get("anchorId"))
                known, confirmed = audit.get("knownAt"), audit.get("confirmedAt")
                if known and confirmed:
                    delays.append(int(known) - int(confirmed))
                if audit.get("consumedRatios"):
                    consumed_at_confirm += 1
                if audit.get("priceSubstituted"):
                    subst += 1
            else:
                missing[audit.get("reason") or "NONE"] += 1
    print(f"適格アンカー(候補ごと) {eligible['ok']}  / 別アンカー {len(anchor_ids)} 本")
    print(f"根拠欠損 {sum(missing.values())}: {dict(missing)}")
    if delays:
        print(f"確認 → 可知(knownAt - confirmedAt) 中央 {statistics.median(delays):.0f} 秒 / "
              f"最大 {max(delays)} 秒")
    print(f"観測時点で既に消化済みの水準を持つアンカー {consumed_at_confirm} / "
          f"現値を代用したアンカー {subst}")

    # 2. 目標として実際に使われたか + 無効果だった理由
    print("\n--- 2. 目標投影が候補/最終 decision に使われた件数 ---")
    for name in ("TARGETS", "TGT_RUN"):
        offered = applied = rejected = primary_applied = 0
        for row in rows:
            var = row["variants"].get(name) or {}
            for audit in var.get("candidateStdv") or []:
                if not audit:
                    continue
                offered += 1 if audit.get("targetsOffered") else 0
                applied += 1 if audit.get("targetsApplied") else 0
                rejected += 1 if audit.get("runnerRejected") else 0
            if (var.get("ictStdv") or {}).get("targetsApplied"):
                primary_applied += 1
        print(f"{name:8s} 提示された候補 {offered:4d} / 目標が差し替わった候補 {applied:4d} / "
              f"runner 却下 {rejected:4d} / **最終 decision で差し替わった周期 {primary_applied:4d}**")
    print("\n  無効果だった理由(STDV アンカーを持つ候補の hardBlockers。SHADOW で数える):")
    reasons: collections.Counter = collections.Counter()
    bearer_models: collections.Counter = collections.Counter()
    for row in rows:
        var = row["variants"].get("SHADOW") or {}
        bearers = [a for a in (var.get("candidateStdv") or [])
                   if a and a.get("projectionValid") is True]
        if not bearers:
            continue
        # decision 側に STDV が載っているかで「primary に届いたか」を数える
        if (var.get("ictStdv") or {}).get("anchorId"):
            bearer_models[str(var.get("model"))] += 1
    print(f"    primary に届いた周期のモデル: {dict(bearer_models) or '(なし)'}")
    print("    ※ v1 のアンカー源は確定済み SWEEP チェーンだけなので、担い手はほぼ")
    print("      TURTLE_SOUP_REVERSAL になる。そのモデルは modelGate で ALL 停止中")
    print("      (2026-09-14 ユーザー決定)なので、候補としては出るが primary にならない。")
    print("      = **この標本では TARGETS を LIVE にしても最終判断は変わらない。**")

    # 3. 周期単位の変化(OFF/SHADOW の不変性の検算を含む)
    print("\n--- 3. 周期単位(BASE との差) ---")
    for name in names[1:]:
        changed_id = changed_tp = armed_to_watch = watch_to_armed = 0
        for row in rows:
            b, v = row["variants"].get("BASE") or {}, row["variants"].get(name) or {}
            if not b.get("model") and not v.get("model"):
                continue
            if b.get("decisionId") != v.get("decisionId"):
                changed_id += 1
            if b.get("targets") != v.get("targets"):
                changed_tp += 1
            bs, vs = str(b.get("state") or ""), str(v.get("state") or "")
            if bs in {"ARMED", "ACTIVE"} and vs not in {"ARMED", "ACTIVE"}:
                armed_to_watch += 1
            if vs in {"ARMED", "ACTIVE"} and bs not in {"ARMED", "ACTIVE"}:
                watch_to_armed += 1
        flag = "  <= SHADOW は 0 でなければならない" if name == "SHADOW" else ""
        print(f"{name:8s} decisionId 変化 {changed_id:4d} / TP 変化 {changed_tp:4d} / "
              f"ARMED→WATCH {armed_to_watch:3d} / WATCH→ARMED {watch_to_armed:3d}{flag}")

    # 4. セットアップ単位 → 逐次
    tables: Dict[str, Dict[Tuple, Dict[str, Any]]] = {}
    for name in names:
        table = {}
        for setup in r90.setups_for(rows, name):
            table[setup["signal"]] = {"setup": setup,
                                      "res": r90.simulate_setup(setup, bars, times, guard_n,
                                                                rest_min * 60)}
        tables[name] = table
    keys = sorted(set().union(*[set(t) for t in tables.values()]))
    armed = [k for k in keys
             if any(k in t and t[k]["res"].get("outcome") != "NOT_ARMED" for t in tables.values())]
    print(f"\n--- 4. setups (どれかの変種で ARMED): {len(armed)} ---")
    base_r = [_r_of(tables["BASE"].get(k)) for k in armed]
    for name, table in tables.items():
        vals = [_r_of(table.get(k)) for k in armed]
        fills = sum(1 for k in armed if (table.get(k) or {}).get("res", {}).get("outcome") == "FILLED")
        tp1_first = sum(1 for k in armed
                        if (table.get(k) or {}).get("res", {}).get("outcome") == "TP1_FIRST")
        no_fill = sum(1 for k in armed
                      if (table.get(k) or {}).get("res", {}).get("outcome") == "NO_FILL")
        mean, lo, hi, prob = r90._bootstrap(base_r, vals)
        print(f"{name:8s} ΣR={sum(vals):+7.2f} 約定={fills:3d} 未約定のまま TP1 到達={tp1_first:3d} "
              f"未約定={no_fill:3d} | vs BASE {mean:+.3f}R/setup [{lo:+.2f},{hi:+.2f}] "
              f"P(improve)={prob:.2f}")

    # 5. 逐次(同時に 1 建玉だけ)。**結論はここで語る。**
    print("\n--- 5. 逐次(同時に 1 建玉だけ。経路占有を考慮した実運用の姿) ---")
    seq: Dict[str, List[Tuple]] = {}
    for name, table in tables.items():
        items = sorted(table.values(), key=lambda it: it["setup"]["first"])
        busy_until = 0
        taken: List[Tuple] = []
        n = fills = tp1 = losses = 0
        total = 0.0
        equity = 0.0
        peak = 0.0
        max_dd = 0.0
        mfe: List[float] = []
        mae: List[float] = []
        for item in items:
            setup, res = item["setup"], item["res"]
            if res.get("outcome") == "NOT_ARMED":
                continue
            start = setup["first"]
            if start < busy_until:
                continue
            n += 1
            if res.get("outcome") == "FILLED":
                fills += 1
                r = float(res.get("r") or 0.0)
                tp1 += 1 if res.get("tp1") else 0
                losses += 1 if (r < 0 and not res.get("tp1")) else 0
                total += r
                equity += r
                peak = max(peak, equity)
                max_dd = min(max_dd, equity - peak)
                for key, sink in (("mfeR", mfe), ("maeR", mae)):
                    value = _finite(res.get(key))
                    if value is not None:
                        sink.append(value)
                busy_until = int(res.get("exitT") or start) + 180
                taken.append((start, setup["model"], setup["side"], r, bool(res.get("tp1"))))
            elif res.get("outcome") == "NO_FILL":
                busy_until = start + rest_min * 60
        seq[name] = taken
        mfe_s = f"{statistics.median(mfe):+.2f}" if mfe else "n/a"
        mae_s = f"{statistics.median(mae):+.2f}" if mae else "n/a"
        print(f"{name:8s} 取った={n:3d} 約定={fills:3d} TP1到達={tp1:3d} 損切り={losses:3d} "
              f"ΣR={total:+7.2f} 最大DD={max_dd:+6.2f}R MFE中央={mfe_s} MAE中央={mae_s}")
    print("  注: MFE/MAE は `entry_depth.simulate` が返さない(戻りは outcome/r/points/"
          "tp1/fillT/exitT/clearancePt)。**この再生では未取得**で、実トレードの MAE/MFE は "
          "`python model_scorecard.py --excursions` 側にある。費用も含まれていない。")

    # 6. 学習に使っていない後続期間(時間で 70/30)
    print("\n--- 6. 期間分割(前 70% / 後 30%。規則選定に後半を使っていない) ---")
    t0, t1 = rows[0]["t"], rows[-1]["t"]
    cut = t0 + 0.7 * (t1 - t0)
    for name, taken in seq.items():
        a = [x[3] for x in taken if x[0] < cut]
        b = [x[3] for x in taken if x[0] >= cut]
        print(f"{name:8s} 前70%: n={len(a):3d} ΣR={sum(a):+7.2f} | 後30%: n={len(b):3d} ΣR={sum(b):+7.2f}"
              + ("   <= n が小さい区間は不足と読む" if len(b) < 30 else ""))

    # 7. 1 件依存性(符号が 1 トレードで変わるか)
    print("\n--- 7. 1 件依存性 ---")
    for name, taken in seq.items():
        vals = sorted(x[3] for x in taken)
        if not vals:
            continue
        total = sum(vals)
        without_best = total - vals[-1]
        without_worst = total - vals[0]
        flip = (total > 0) != (without_best > 0) or (total > 0) != (without_worst > 0)
        print(f"{name:8s} ΣR={total:+7.2f} 最大勝ち除外={without_best:+7.2f} "
              f"最大負け除外={without_worst:+7.2f} 符号反転={'する' if flip else 'しない'}")

    # 8. 参加判断(方向の評価と参加後の結果を別に数える)
    print("\n--- 8. 参加判断(SHADOW の read。方向精度と参加後の損益を別集計) ---")
    part = collections.Counter()
    thesis = collections.Counter()
    for row in rows:
        for audit in (row["variants"].get("SHADOW") or {}).get("candidateStdv") or []:
            if not audit or audit.get("projectionValid") is not True:
                continue
            part[audit.get("participation") or "NONE"] += 1
            thesis[audit.get("thesis") or "NONE"] += 1
    print(f"participation: {dict(part)}")
    print(f"thesis: {dict(thesis)}")
    print("  注: STDV 水準の到達率はエントリー勝率ではない。参加後の損益は §5 の逐次で数える。")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="R121 replay (read-only)")
    parser.add_argument("--replay", action="store_true", help="監査バンドルを変種ごとに再評価")
    parser.add_argument("--limit", type=int, default=0, help="直近 N 本のバンドルだけ")
    parser.add_argument("--cache", default=os.path.join(SECRETS, "r121_replay_cache.json"))
    parser.add_argument("--reevaluate", action="store_true", help="キャッシュを無視して再評価")
    parser.add_argument("--secrets", default=SECRETS)
    parser.add_argument("--rest", type=int, default=30, help="指値を待つ分数")
    args = parser.parse_args(argv)
    if not args.replay:
        parser.error("--replay を指定する")
    rows = None
    if os.path.exists(args.cache) and not args.reevaluate:
        try:
            cached = json.load(io.open(args.cache, encoding="utf-8"))
            if cached.get("variants") == sorted(VARIANTS):
                rows = cached.get("rows")
                print(f"cache: {args.cache} ({len(rows)} bundles)")
        except (OSError, ValueError):
            rows = None
    if rows is None:
        paths = sorted(glob.glob(os.path.join(args.secrets, "monitor_cycle_*.json")))
        if args.limit:
            paths = paths[-args.limit:]
        print(f"re-evaluating {len(paths)} bundles x {len(VARIANTS)} variants ...")
        rows = evaluate_all(paths)
        try:
            json.dump({"variants": sorted(VARIANTS), "rows": rows},
                      io.open(args.cache, "w", encoding="utf-8"))
            print(f"cache -> {args.cache}")
        except OSError as exc:
            print(f"cache write failed: {exc}")
    rows = [r for r in rows if r.get("variants")]
    rows.sort(key=lambda r: r["t"])
    if rows:
        report(rows, rest_min=args.rest, secrets=args.secrets)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

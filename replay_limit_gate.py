# -*- coding: utf-8 -*-
"""R119 の再生と再集計(読むだけ)。

    python replay_limit_gate.py --ledger      # 台帳の実指値 40 件の再集計(速い)
    python replay_limit_gate.py --replay      # 監査バンドルを変種ごとに再評価(数十分)
    python replay_limit_gate.py --replay --limit 300 --cache PATH

--replay は `replay_r103.py` / `replay_stop_logic.py` と**同じ規則**で比べる ——
セットアップの束ね(setups_for)・約定判定(entry_depth.simulate: 確定 3 分足・指値は 1tick
突き抜け・約定前 TP1 で取消・同じ足は SL 優先・指値待ち 30 分・6 時間)・R90 穴 2 の成行
ゲート 1.0N・R は計画の SL 幅。差し替えるのは `msnr_gate.limit_gate_policy` だけ。

--ledger は `.secrets/autotrade_ledger.jsonl` の**実際に送った**指値を数える。成行で入って
いたらどうだったかは、SL 上限の条件を 3 通り(プラン自身の riskCapPoints / 60pt / 50pt)に
分けて出す —— 上限はモデル層(msnr_gate.sl_cap_pt = 60)と通常経路のドル上限
($200 ÷ $2 ÷ 2 枚 = 50pt、TRADING_CONTEXT.md)と ULTRA(残 DD から引く riskCapPoints)で
別物なので、1 つの数字にまとめない。

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

#: 変種。gapCap / targetPassed だけを動かす。GAP10 は 2026-09-19 時点で**保留**(参考値)。
MODELS = ["VP80_REVERSION", "TURTLE_SOUP_REVERSAL", "BREAKER_CONTINUATION", "OTE_FVG_PULLBACK"]
VARIANTS: Dict[str, Dict[str, Any]] = {
    "BASE": {"gapCap": {"mode": "OFF"}, "targetPassed": {"mode": "OFF"}},
    "GAP15": {"gapCap": {"mode": "LIVE", "maxGapR": 1.5, "models": MODELS},
              "targetPassed": {"mode": "OFF"}},
    "PASSED": {"gapCap": {"mode": "OFF"}, "targetPassed": {"mode": "LIVE"}},
    "BOTH": {"gapCap": {"mode": "LIVE", "maxGapR": 1.5, "models": MODELS},
             "targetPassed": {"mode": "LIVE"}},
    "GAP10": {"gapCap": {"mode": "LIVE", "maxGapR": 1.0, "models": MODELS},
              "targetPassed": {"mode": "LIVE"}},
}
DECISION_KEYS = ("model", "side", "state", "grade", "entry", "stop", "targets", "targetR",
                 "decisionId", "hardBlockers", "evidence", "limitGate")
GAP = "LIMIT_GAP_EXCEEDED"
PASSED = "TARGET_ALREADY_PASSED"


def _finite(value: Any) -> Optional[float]:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed == parsed and abs(parsed) != float("inf") else None


def _instant(value: Any) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


# ------------------------------------------------------------------ 再生

def evaluate_all(paths: List[str]) -> List[Dict[str, Any]]:
    import msnr_gate
    policies = {name: msnr_gate.limit_gate_policy({"limitGate": spec})
                for name, spec in VARIANTS.items()}
    bad = {n: p["invalid"] for n, p in policies.items() if p["invalid"]}
    if bad:
        raise SystemExit(f"変種の指定が不正: {bad}")
    saved = msnr_gate.limit_gate_policy
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
                msnr_gate.limit_gate_policy = (lambda p=policy: p)
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
        msnr_gate.limit_gate_policy = saved
    return rows


def _r_of(item: Optional[Dict[str, Any]]) -> float:
    if not item:
        return 0.0
    res = item["res"]
    return float(res.get("r") or 0.0) if res.get("outcome") == "FILLED" else 0.0


def report_replay(rows: List[Dict[str, Any]], rest_min: int = 30, guard_n: float = 1.0,
                  secrets: str = SECRETS) -> None:
    import entry_depth
    import replay_stop_logic as r90
    bars = entry_depth.load_bars(secrets)
    times = [b["t"] for b in bars]
    print(f"\nbundles {len(rows)} ({rows[0]['at'][:16]} → {rows[-1]['at'][:16]} UTC) / bars {len(bars)} / "
          f"limit rest {rest_min}min / market stop guard {guard_n}N / same-bar SL first / R = planned risk")

    # 1. 周期単位: 門が当たった回数と ARMED→WATCH
    print("\n--- 周期単位(BASE との差) ---")
    for name in list(VARIANTS)[1:]:
        gap = passed = armed_to_watch = 0
        for row in rows:
            b, v = row["variants"].get("BASE") or {}, row["variants"].get(name) or {}
            blockers = v.get("hardBlockers") or []
            gap += 1 if GAP in blockers else 0
            passed += 1 if PASSED in blockers else 0
            bs, vs = str(b.get("state") or ""), str(v.get("state") or "")
            if bs in {"ARMED", "ACTIVE"} and vs not in {"ARMED", "ACTIVE"}:
                armed_to_watch += 1
        print(f"{name:7s}: {GAP} {gap:4d} | {PASSED} {passed:4d} | ARMED→WATCH {armed_to_watch:4d}")

    # 2. セットアップ単位 → 逐次(既存 replay と同じ規則)
    tables: Dict[str, Dict[Tuple, Dict[str, Any]]] = {}
    for name in VARIANTS:
        table = {}
        for setup in r90.setups_for(rows, name):
            table[setup["signal"]] = {"setup": setup,
                                      "res": r90.simulate_setup(setup, bars, times, guard_n, rest_min * 60)}
        tables[name] = table
    keys = sorted(set().union(*[set(t) for t in tables.values()]))
    armed = [k for k in keys
             if any(k in t and t[k]["res"].get("outcome") != "NOT_ARMED" for t in tables.values())]
    print(f"\n--- setups (どれかの変種で ARMED): {len(armed)} ---")
    base_r = [_r_of(tables["BASE"].get(k)) for k in armed]
    for name, table in tables.items():
        vals = [_r_of(table.get(k)) for k in armed]
        fills = sum(1 for k in armed if (table.get(k) or {}).get("res", {}).get("outcome") == "FILLED")
        tp1_first = sum(1 for k in armed if (table.get(k) or {}).get("res", {}).get("outcome") == "TP1_FIRST")
        no_fill = sum(1 for k in armed if (table.get(k) or {}).get("res", {}).get("outcome") == "NO_FILL")
        excluded = sum(1 for k in armed
                       if k not in table or table[k]["res"].get("outcome") == "NOT_ARMED")
        mean, lo, hi, prob = r90._bootstrap(base_r, vals)
        print(f"{name:7s} ΣR={sum(vals):+7.2f} 残る約定={fills:3d} 取りこぼし(TP1先着)={tp1_first:3d} "
              f"未約定={no_fill:3d} 除外={excluded:3d} | vs BASE {mean:+.3f}R/setup [{lo:+.2f},{hi:+.2f}] "
              f"P(improve)={prob:.2f}")

    # 3. 逐次(同時に 1 建玉だけ)。**結論はここで語る。**
    # セットアップ単位は、実運用では持てない同時並行のトレードまで数える(R90 で 4 倍に
    # 数えた罠)。経路は 1 本しか無いので、約定したら決済まで次を取らない。
    print("\n--- 逐次(同時に 1 建玉だけ。経路占有を考慮した実運用の姿) ---")
    seq_taken: Dict[str, List[Tuple]] = {}
    for name, table in tables.items():
        items = sorted(table.values(), key=lambda it: it["setup"]["first"])
        busy_until = 0
        taken: List[Tuple] = []
        n = fills = tp1 = losses = 0
        total = 0.0
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
                busy_until = int(res.get("exitT") or start) + 180
                taken.append((start, setup["model"], setup["side"], r, bool(res.get("tp1"))))
            elif res.get("outcome") == "NO_FILL":
                busy_until = start + rest_min * 60
        seq_taken[name] = taken
        print(f"{name:7s} 取った={n:3d} 約定={fills:3d} TP1到達={tp1:3d} 損切り={losses:3d} ΣR={total:+7.2f}")

    # 4. 門が外したセットアップの、BASE での結末(取り逃しを数える)
    print("\n--- 各変種が外したセットアップの BASE での結末(セットアップ単位) ---")
    base_table = tables["BASE"]
    for name in list(VARIANTS)[1:]:
        table = tables[name]
        dropped = [k for k in armed
                   if base_table.get(k) and base_table[k]["res"].get("outcome") != "NOT_ARMED"
                   and (k not in table or table[k]["res"].get("outcome") == "NOT_ARMED")]
        outcomes = collections.Counter((base_table[k]["res"].get("outcome") or "?") for k in dropped)
        lost_r = sum(_r_of(base_table[k]) for k in dropped)
        print(f"{name:7s} 外した {len(dropped):3d} 件 → BASE では {dict(outcomes)} / 失う ΣR {lost_r:+.2f}")
        for key in dropped:
            setup = base_table[key]["setup"]
            res = base_table[key]["res"]
            at = datetime.fromisoformat(key[3]).astimezone(JST)
            cycles = [c for c in setup["cycles"]
                      if str(c.get("state") or "").upper() in {"ARMED", "ACTIVE"}] or setup["cycles"]
            price = cycles[0].get("price")
            risk = abs(setup["entry"] - setup["stop"])
            gap = abs((price or 0) - setup["entry"]) / risk if risk else float("nan")
            print(f"        {at:%m-%d %H:%M} {setup['model'][:20]:20s} {setup['side']:4s} "
                  f"E {setup['entry']:,.2f} SL {setup['stop']:,.2f} price {price} gapR {gap:.2f} "
                  f"→ BASE {res.get('outcome')} {_r_of(base_table[key]):+.2f}R")


# ------------------------------------------------------------- 台帳の再集計

def _ledger_signals(path: str) -> List[Dict[str, Any]]:
    """台帳の ENTRY を「同じ (model, side, entry, tp1) が 90 分以内」で 1 シグナルへ畳む。"""
    rows = []
    with io.open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    by_key: Dict[str, List[Dict[str, Any]]] = collections.OrderedDict()
    for row in rows:
        key = row.get("entryKey") or row.get("key")
        if isinstance(key, str) and key.startswith("ENTRY:"):
            by_key.setdefault(key, []).append(row)

    def fate(statuses, actions) -> str:
        s, a = set(statuses), set(actions)
        if "ENTRY_PARTIAL_FILL" in s or "ENTRY_OWNERSHIP_BOUND" in a or "MANAGEMENT_SENT" in s:
            return "FILLED"
        if "ENTRY_STALE_CANCEL" in a:
            return "MISS"
        if "ENTRY_TERMINAL" in a:
            return "CXL"
        if "ENTRY_RESTING" in s:
            return "RESTING"
        if "HALT" in s:
            return "HALT"
        return "OTHER"

    claims = []
    for key, group in by_key.items():
        plan = next((r["plan"] for r in group
                     if isinstance(r.get("plan"), dict) and r["plan"].get("decisionId")), None)
        first = min((r["time"] for r in group if isinstance(r.get("time"), str)), default=None)
        if not plan or not first:
            continue
        rest = [r for r in group if r.get("status") == "ENTRY_RESTING"]
        term = [r for r in group
                if r.get("action") in ("ENTRY_STALE_CANCEL", "ENTRY_TERMINAL", "ENTRY_OWNERSHIP_BOUND")
                or r.get("status") in ("ENTRY_PARTIAL_FILL", "MANAGEMENT_SENT")]
        held = None
        if rest and term:
            t0 = min(_instant(r["time"]) for r in rest)
            ends = [_instant(r["time"]) for r in term if _instant(r["time"]) and _instant(r["time"]) >= t0]
            if ends:
                held = (min(ends) - t0).total_seconds() / 60.0
        claims.append({"t": _instant(first), "plan": plan, "held": held,
                       "fate": fate([r.get("status") for r in group],
                                    [r.get("action") for r in group if isinstance(r.get("action"), str)])})
    claims.sort(key=lambda c: c["t"])
    signals: List[Dict[str, Any]] = []
    for claim in claims:
        plan = claim["plan"]
        key = (plan.get("model"), plan.get("side"), plan.get("entry"), plan.get("tp1"))
        hit = next((s for s in signals
                    if s["key"] == key and (claim["t"] - s["last"]).total_seconds() <= 5400), None)
        if hit:
            hit["last"] = claim["t"]
            hit["fates"].add(claim["fate"])
            hit["claims"] += 1
            if claim["held"] is not None:
                hit["held"] = (hit["held"] or 0.0) + claim["held"]
            continue
        signals.append({"key": key, "t": claim["t"], "last": claim["t"], "plan": plan,
                        "fates": {claim["fate"]}, "claims": 1, "held": claim["held"]})
    order = ("FILLED", "MISS", "CXL", "RESTING", "HALT", "OTHER")
    for sig in signals:
        sig["fate"] = next((f for f in order if f in sig["fates"]), "OTHER")
    return signals


def _market_series(secrets: str) -> List[Tuple[int, float, float, float]]:
    """(t, high, low, close)。Databento 補正済み corpus + tv_raw の和集合。"""
    out: Dict[int, Tuple[int, float, float, float]] = {}
    corpus = os.path.join(secrets, "research", "corpus_db_3m.json")
    if os.path.exists(corpus):
        data = json.load(io.open(corpus, encoding="utf-8"))
        off = float(data.get("offset") or 0.0)
        for bar in data.get("bars") or []:
            adj = off if str(bar.get("basis", "")).startswith("U6") else 0.0
            out[int(bar["t"])] = (int(bar["t"]), bar["h"] - adj, bar["l"] - adj, bar["c"] - adj)
    raw = os.path.join(secrets, "tv_raw", "bars3m.json")
    if os.path.exists(raw):
        for bar in (json.load(io.open(raw, encoding="utf-8")).get("bars") or []):
            t = int(bar["time"])
            out.setdefault(t, (t, bar["high"], bar["low"], bar["close"]))
    return [out[t] for t in sorted(out)]


def _audit_prices(secrets: str) -> List[Tuple[float, float]]:
    """監査バンドルの (at の epoch, price)。**武装周期の実価格**の唯一の記録。

    `monitor_cycle_HHMM.json` は HH:MM ごとに上書きされるので全周期は残っていない。
    残っている周期はこれを使う —— 直前確定足の終値は代用にならない(2026-09-15 13:31 の
    実価格 29,388.75 に対し直前の確定足終値は 29,420.75 で、gapR が 2.01 → 0.28 になった)。
    """
    out = []
    for path in glob.glob(os.path.join(secrets, "monitor_cycle_*.json")):
        try:
            bundle = json.load(io.open(path, encoding="utf-8"))
        except (OSError, ValueError):
            continue
        moment = _instant(bundle.get("at"))
        price = _finite(bundle.get("price"))
        if moment and price:
            out.append((moment.timestamp(), price))
    out.sort()
    return out


def report_ledger(secrets: str = SECRETS) -> None:
    signals = _ledger_signals(os.path.join(secrets, "autotrade_ledger.jsonl"))
    series = _market_series(secrets)
    times = [row[0] for row in series]
    audit = _audit_prices(secrets)
    audit_times = [row[0] for row in audit]
    import bisect
    stats = {"audit": 0, "proxy": 0}

    def close_before(ts: float) -> Optional[float]:
        """武装周期の現値。監査バンドルがあればその実価格、無ければ直前確定足の終値(代用)。"""
        i = bisect.bisect_right(audit_times, ts) - 1
        if i >= 0 and ts - audit_times[i] <= 600:
            stats["audit"] += 1
            return audit[i][1]
        j = bisect.bisect_right(times, ts - 180) - 1
        if j < 0:
            return None
        stats["proxy"] += 1
        return series[j][3]

    limits = [s for s in signals if s["plan"].get("entryOrderType") == "LIMIT"]
    print(f"台帳のシグナル {len(signals)} 件(うち指値 {len(limits)} 件) / "
          f"{signals[0]['t'].astimezone(JST):%Y-%m-%d} → {signals[-1]['t'].astimezone(JST):%Y-%m-%d} JST")

    # 1. 指値の遠さ(モデル別)
    print("\n--- 指値の gapR = |現値-建値| / リスク幅 ---")
    table: Dict[str, Dict[str, List[float]]] = collections.defaultdict(lambda: collections.defaultdict(list))
    tp1r: Dict[str, Dict[str, List[float]]] = collections.defaultdict(lambda: collections.defaultdict(list))
    for sig in limits:
        plan = sig["plan"]
        mkt = close_before(sig["t"].timestamp())
        entry, stop, tp1 = (_finite(plan.get("entry")), _finite(plan.get("initialStop")),
                            _finite(plan.get("tp1")))
        if mkt is None or None in (entry, stop, tp1) or abs(entry - stop) <= 0:
            continue
        risk = abs(entry - stop)
        sign = 1.0 if plan.get("side") == "BUY" else -1.0
        table[str(plan.get("model"))][sig["fate"]].append(abs(mkt - entry) / risk)
        tp1r[str(plan.get("model"))][sig["fate"]].append(sign * (tp1 - mkt) / risk)
    for model in sorted(table):
        print(f"\n## {model}")
        for fate in ("FILLED", "MISS", "CXL", "RESTING", "HALT"):
            xs = table[model].get(fate) or []
            if not xs:
                continue
            ys = tp1r[model][fate]
            print(f"   {fate:<8} n={len(xs):<3} gapR median {statistics.median(xs):.2f} "
                  f"[{min(xs):.2f}..{max(xs):.2f}] | 現値→TP1 の R median {statistics.median(ys):+.2f} "
                  f"(0 以下 {sum(1 for y in ys if y <= 0)})")

    # 2. gapCap を掛けたときの除外/残存
    print("\n--- BREAKER_CONTINUATION に gapCap を掛けた場合(標本内) ---")
    breaker = table.get("BREAKER_CONTINUATION") or {}
    for cap in (2.0, 1.5, 1.25, 1.0, 0.75):
        parts = []
        for fate in ("FILLED", "MISS", "CXL", "RESTING"):
            xs = breaker.get(fate) or []
            if xs:
                parts.append(f"{fate} {sum(1 for g in xs if g <= cap)}/{len(xs)}")
        print(f"  cap {cap:<5} 残す: {' / '.join(parts)}")

    # 3. 成行で入っていたら(SL 上限の 3 条件)
    print("\n--- 指値の代わりに成行で入っていたら(SL 上限の条件別) ---")
    print("    上限の出所: plan = 各プランの riskCapPoints(ULTRA は残 DD 由来) / "
          "60pt = msnr_gate.sl_cap_pt() / 50pt = $200÷$2÷2枚(TRADING_CONTEXT.md 通常経路)")
    for cap_name in ("plan", "60", "50"):
        counts: collections.Counter = collections.Counter()
        by_fate: Dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
        for sig in limits:
            plan = sig["plan"]
            if plan.get("model") != "BREAKER_CONTINUATION":
                continue
            mkt = close_before(sig["t"].timestamp())
            entry, stop = _finite(plan.get("entry")), _finite(plan.get("initialStop"))
            tp1, final = _finite(plan.get("tp1")), _finite(plan.get("finalTarget"))
            if mkt is None or None in (entry, stop, tp1, final):
                continue
            side = str(plan.get("side"))
            cap = ({"plan": _finite(plan.get("riskCapPoints")), "60": 60.0, "50": 50.0}[cap_name])
            risk_market = abs(mkt - stop)
            wrong_side = (stop > mkt) if side == "BUY" else (stop < mkt)
            if wrong_side:
                label = "SL_WRONG_SIDE"
            elif cap is not None and risk_market > cap + 1e-9:
                label = "RISK_CAP"
            else:
                label = _walk(series, times, sig["t"].timestamp(), side, mkt, stop, tp1, final)
            counts[label] += 1
            by_fate[sig["fate"]][label] += 1
        total = sum(counts.values())
        print(f"\n  [{cap_name}] n={total}  {dict(counts)}  (合計 {sum(counts.values())})")
        for fate in ("FILLED", "MISS", "CXL", "RESTING"):
            if by_fate.get(fate):
                print(f"      {fate:<8} n={sum(by_fate[fate].values()):<3} {dict(by_fate[fate])}")

    print(f"\n  現値の出所: 監査バンドルの実価格 {stats['audit']} 件 / "
          f"直前確定足の終値(代用) {stats['proxy']} 件 —— 代用の分は gapR を**過小に**出す")

    # 4. 注文経路の占有時間
    print("\n--- 指値が新規 ENTRY の経路を押さえていた時間(台帳の実測) ---")
    occ: Dict[Tuple[str, str], List[float]] = collections.defaultdict(list)
    for sig in signals:
        if sig["held"] is None:
            continue
        occ[(str(sig["plan"].get("model")), sig["fate"])].append(sig["held"])
    for (model, fate), vals in sorted(occ.items()):
        print(f"  {model:<22}{fate:<8} n={len(vals):<3} 合計 {sum(vals):6.0f} 分 / "
              f"中央 {statistics.median(vals):5.0f} 分 / 最長 {max(vals):5.0f} 分")


def _walk(series, times, start: float, side: str, entry: float, stop: float,
          tp1: float, final: float, horizon_h: int = 8) -> str:
    import bisect
    end = start + horizon_h * 3600
    first = bisect.bisect_right(times, start) - 1
    if first < 0:
        return "NO_BARS"
    tp1_done = False
    for t, high, low, _c in series[max(first, 0):]:
        if t > end:
            break
        if side == "BUY":
            if low <= stop:
                return "TP1_THEN_SL" if tp1_done else "SL_FIRST"
            if high >= final:
                return "FINAL"
            if high >= tp1:
                tp1_done = True
        else:
            if high >= stop:
                return "TP1_THEN_SL" if tp1_done else "SL_FIRST"
            if low <= final:
                return "FINAL"
            if low <= tp1:
                tp1_done = True
    return "TP1_THEN_OPEN" if tp1_done else "NEITHER"


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="R119 replay / re-aggregation (read-only)")
    parser.add_argument("--ledger", action="store_true", help="台帳の実指値を再集計する")
    parser.add_argument("--replay", action="store_true", help="監査バンドルを変種ごとに再評価する")
    parser.add_argument("--limit", type=int, default=0, help="直近 N 本のバンドルだけ")
    parser.add_argument("--cache", default=os.path.join(SECRETS, "r119_replay_cache.json"),
                        help="再評価の結果を保存/再利用する JSON")
    parser.add_argument("--reevaluate", action="store_true", help="キャッシュを無視して再評価")
    parser.add_argument("--secrets", default=SECRETS, help="監査コピーのディレクトリ")
    parser.add_argument("--rest", type=int, default=30, help="指値を待つ分数")
    args = parser.parse_args(argv)
    if not args.ledger and not args.replay:
        parser.error("--ledger か --replay のどちらかを指定する")
    if args.ledger:
        report_ledger(args.secrets)
    if args.replay:
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
            report_replay(rows, rest_min=args.rest, secrets=args.secrets)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

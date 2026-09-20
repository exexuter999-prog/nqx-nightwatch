# -*- coding: utf-8 -*-
"""R122 選択層だけの検証(読むだけ)。**他の段は一切動かさない。**

    python replay_selection.py --audit      # 1. 高得点 WATCH が隠した ARMED 候補の一覧
    python replay_selection.py --blockers   # 2. WATCH 理由の分類(全体停止 / 候補固有)
    python replay_selection.py --compare    # 3. 選択層だけの費用込み比較

比べるのは **2 条件だけ**で、違いは `structureContext.selection` の 1 語:

  S0_NOW      本番契約そのまま(selection OFF)= 現行の primary 選び
  S1_SELECT   同じ契約の `selection` だけ LIVE(= 既存ゲートを全部通った候補から最有力を選ぶ)

`shallowCandidate` は両条件とも SHADOW のまま、`nearTerm` も SHADOW のまま、新しい加点は
入れない。**契約ファイルは書き換えない** —— `msnr_gate.structure_context_policy` をこの
プロセスの中だけで差し替える。

入力は監査バンドル(`.secrets/monitor_cycle_*.json`)。**15 分足は渡さない** —— 本番の
親の出所は直接取得した `snapshot.bars15m` だけで、監査コーパスにはそれが無いため
(再構成は研究用で `replay_structure_context.py --source recon` 側にある)。選択層は
15 分足に依存しないので、この比較は本番構成に忠実。

費用: `.secrets/crosstrade.env` の `FEE_PER_SIDE_<口座>`(R85)。MNQ は 1pt = $2.00 なので
片道 $0.50/枚 = 往復 0.5pt 相当。滑りは `--slip-ticks` で明示的に足す(成行の建値と SL 決済に
1tick = 0.25pt ずつ)。**どちらも「同じ条件を両方に当てる」ためのもので、実績の再現ではない。**

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
import re
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
SECRETS = os.path.join(BASE, ".secrets")
JST = timezone(timedelta(hours=9))
BAR3_SEC = 180
TICK = 0.25
POINT_VALUE = 2.0          # MNQ: 1pt = $2.00

S0, S1 = "S0_NOW", "S1_SELECT"

#: 市場全体・口座全体の停止理由。**代替候補で迂回してはいけない。**
#: これらは primary を選んだ**後**(monitor_publish / engine / order.py)か、
#: 周期そのものを止める側(pipeline)で効くので、選択層では迂回できない。
GLOBAL_BLOCKERS = {
    "ACQUISITION_RECEIPT_MISSING": "取得受領書が無い(周期ごと停止)",
    "ACQUISITION_DATA_STALE": "取得データが古い(周期ごと停止)",
    "VOL_STANDDOWN": "ボラ床が高すぎる(全モデル停止)",
    "EVENT_BLACKOUT": "High イベント窓(全候補を WATCH へ降格)",
    "CONTRACT_EXPIRY_NEAR": "限月の満期が近い(新規だけ停止)",
    "CONTRACT_SYMBOL_DISAGREE": "限月・銘柄の不一致(相場データ前に停止)",
    "MANUAL_HALT": "手動 HALT(自律経路すべて停止)",
    "BROKER_UNVERIFIED": "建玉照会が未確認(新規・変更を停止)",
}
#: 契約で外したモデル / 型、および SHADOW の追加候補。R89 / R122 の設計どおり
#: **別モデルが primary になってよい**。この層に来る前に `strategy_evaluation` が
#: `modelGate` 印で primary の候補集合から外している。
POLICY_BLOCKERS = {"MODEL_DISABLED", "RESTING_LIMIT_DISABLED", "SHALLOW_CANDIDATE_SHADOW"}
#: その候補だけの不成立理由。**他の適格候補を止める理由にはならない。**
CANDIDATE_BLOCKERS = {
    "ANCHOR_CONSUMED": "この水準は既に消費済み(この候補の錨が無効)",
    "NO_CONFIRMATION": "独立した確認要素がこの候補に無い",
    "RISK_CAP_EXCEEDED": "この候補の SL 幅が上限超過",
    "RISK_BELOW_NOISE": "この候補の SL 幅がノイズ床未満",
    "TARGET_HEADROOM_INSUFFICIENT": "この候補の建値から目標までの余地不足",
    "TARGET_ALREADY_PASSED": "現値がこの候補の TP1 を通過済み",
    "GEOMETRY_INVALID": "この候補の建値/SL/TP の並びが不整合",
    "LIMIT_GAP_EXCEEDED": "この候補の指値が現値から遠すぎる",
    "SWEEP_GATE_PENDING": "この候補のプール掃引待ち",
    "SWEEP_GATE_STALE": "この候補の掃引が古い",
    "PARENT_THESIS_INVALIDATED": "この候補が依存する親の仮説が否定された",
    "NO_A_OR_A_PLUS_MODEL": "候補そのものが無い",
}


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


def classify(blocker: str) -> str:
    if blocker in GLOBAL_BLOCKERS:
        return "GLOBAL"
    if blocker in POLICY_BLOCKERS:
        return "POLICY"
    if blocker in CANDIDATE_BLOCKERS:
        return "CANDIDATE"
    return "UNKNOWN"


def fee_per_side(secrets: str = SECRETS) -> Optional[float]:
    """`FEE_PER_SIDE_<口座>` の中央値(1 枚・片道 $)。口座 ID は出さない。"""
    try:
        text = io.open(os.path.join(secrets, "crosstrade.env"), encoding="utf-8").read()
    except OSError:
        return None
    values = [float(v) for _k, v in re.findall(r"^\s*FEE_PER_SIDE_(\w+)\s*=\s*([0-9.]+)",
                                               text, re.M)]
    return statistics.median(values) if values else None


def cost_points(fee: Optional[float], slip_ticks: float) -> float:
    """1 往復・1 枚あたりの費用を**ポイント**で返す(R へは SL 幅で割って渡す)。"""
    fee_pt = (2.0 * float(fee)) / POINT_VALUE if fee else 0.0
    slip_pt = 2.0 * float(slip_ticks) * TICK
    return fee_pt + slip_pt


# --------------------------------------------------------------------- 評価

def policies() -> Dict[str, Dict[str, Any]]:
    """本番契約を読み、`selection` の 1 語だけを変えた 2 条件を返す。"""
    import msnr_gate
    live = msnr_gate.structure_context_policy()
    if live["invalid"]:
        raise SystemExit(f"本番契約の structureContext が不正: {live['invalid']}")
    print(f"本番契約の selection = {live['selection']['mode']}"
          "(この比較では両条件とも明示的に上書きするので、契約の値に依らない)")
    base = copy.deepcopy(live)
    base["selection"] = {"mode": "OFF"}       # S0: 現行(R122 以前)の並べ替え
    alt = copy.deepcopy(live)
    alt["selection"] = {"mode": "LIVE"}       # S1: 武装できる候補を優先
    for name, policy in ((S0, base), (S1, alt)):
        other = {k: policy[k]["mode"] for k in msnr_gate.STRUCTURE_CONTEXT_STAGES
                 if k != "selection"}
        print(f"  {name}: selection={policy['selection']['mode']} / 他の段 {other}")
    return {S0: base, S1: alt}


def evaluate_all(paths: List[str]) -> List[Dict[str, Any]]:
    import msnr_gate
    table = policies()
    saved = msnr_gate.structure_context_policy
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
            # 15 分足は渡さない(本番の親は直接取得のみ。監査コーパスには無い)。
            bundle.get("snapshot", {}).pop("bars15m", None)
            row = {"path": os.path.basename(path), "at": moment.isoformat(),
                   "t": int(moment.timestamp()), "price": _finite(bundle.get("price")),
                   "noise": None, "variants": {}, "candidates": []}
            for name, policy in table.items():
                msnr_gate.structure_context_policy = (lambda contract=None, p=policy: p)
                try:
                    result = msnr_gate.evaluate(copy.deepcopy(bundle))
                except Exception as exc:  # noqa: BLE001
                    row["variants"][name] = {"error": f"{type(exc).__name__}: {exc}"}
                    continue
                if row["noise"] is None:
                    row["noise"] = result.get("noiseFloor")
                decision = result.get("decision") or {}
                row["variants"][name] = {
                    k: decision.get(k) for k in
                    ("model", "side", "state", "grade", "score", "entry", "stop", "targets",
                     "targetR", "decisionId", "hardBlockers", "entryMode")}
                row["variants"][name]["restingLimit"] = bool(decision.get("restingLimit"))
                row["variants"][name]["selectionReason"] = (
                    (decision.get("structure") or {}).get("selectionReason"))
                if name == S0:
                    # 候補集合は条件で変わらない(選択層は並べ替えだけ)。S0 側で保存する。
                    row["candidates"] = [{
                        "model": c.get("model"), "side": c.get("side"), "grade": c.get("grade"),
                        "score": c.get("score"), "state": c.get("state"),
                        "allowed": bool(c.get("allowed")), "entry": _finite(c.get("entry")),
                        "stop": _finite(c.get("stop")),
                        "targets": [_finite(x) for x in (c.get("targets") or [])],
                        "targetR": [_finite(x) for x in (c.get("targetR") or [])],
                        "hardBlockers": list(c.get("hardBlockers") or []),
                        "modelGate": c.get("modelGate"),
                        "restingLimit": bool(c.get("restingLimit")),
                        "participation": (c.get("participation") or {}).get("state"),
                    } for c in (result.get("candidates") or [])]
            rows.append(row)
            if index % 200 == 0:
                print(f"  {index}/{len(paths)} ({time.time() - started:.0f}s)", flush=True)
    finally:
        msnr_gate.structure_context_policy = saved
    return rows


def ranked(candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """`msnr_gate.select_primary` と同じ並び(等級, 点数, TP1 の R, モデル順位)。"""
    import msnr_gate
    rank = {"A+": 3, "A": 2, "B": 1}
    pool = [c for c in candidates if not c.get("modelGate")]
    return sorted(pool, key=lambda c: (rank.get(c.get("grade"), 0), c.get("score", -999),
                                       (c.get("targetR") or [0])[0] if c.get("targetR") else 0,
                                       msnr_gate.MODEL_RANK.get(c.get("model"), 0)), reverse=True)


def hidden_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """primary が WATCH なのに、同じ周期に武装できる候補がある周期。"""
    out = []
    for row in rows:
        order = ranked(row["candidates"])
        if not order:
            continue
        top = order[0]
        if top.get("allowed"):
            continue
        alt = next((c for c in order if c.get("allowed")), None)
        if alt is None:
            continue
        out.append({"row": row, "order": order, "top": top, "alt": alt})
    return out


def _key(candidate: Dict[str, Any]) -> Tuple:
    return (candidate.get("model"), candidate.get("side"),
            candidate.get("entry"), candidate.get("stop"))


def audit(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """同じセットアップの重複をまとめた一覧。"""
    groups: List[Dict[str, Any]] = []
    for item in hidden_rows(rows):
        key = (_key(item["top"]), _key(item["alt"]))
        if groups and groups[-1]["key"] == key and item["row"]["t"] - groups[-1]["last"] <= 20 * 60:
            groups[-1]["last"] = item["row"]["t"]
            groups[-1]["cycles"] += 1
            groups[-1]["members"].append(item)
            continue
        groups.append({"key": key, "first": item["row"]["t"], "last": item["row"]["t"],
                       "cycles": 1, "members": [item]})
    return groups


def print_audit(rows: List[Dict[str, Any]]) -> None:
    groups = audit(rows)
    hidden = hidden_rows(rows)
    print(f"\n=== 1. 高得点 WATCH が ARMED 候補を隠した周期 ===")
    print(f"対象 {len(rows)} 周期 / 該当 {len(hidden)} 周期 / "
          f"同じセットアップの重複をまとめて **{len(groups)} 件**")
    for index, group in enumerate(groups, 1):
        head = group["members"][0]
        top, alt, row = head["top"], head["alt"], head["row"]
        span = (datetime.fromtimestamp(group["first"], timezone.utc).strftime("%m-%d %H:%M"),
                datetime.fromtimestamp(group["last"], timezone.utc).strftime("%H:%M"))
        print(f"\n[{index}] {span[0]}–{span[1]}Z  ({group['cycles']} 周期)  "
              f"現値 {row.get('price')}  ノイズ床 {row.get('noise')}")
        print(f"    現 primary : {top['model']} {top['side']} {top['grade']} "
              f"score={top['score']} {top['state']}")
        print(f"                 E={top['entry']} SL={top['stop']} TP={top['targets']} "
              f"R={top['targetR']}")
        blockers = [(b, classify(b)) for b in top["hardBlockers"]]
        print(f"                 ブロッカー: "
              + ", ".join(f"{b}[{k}]" for b, k in blockers))
        print(f"    代替候補   : {alt['model']} {alt['side']} {alt['grade']} "
              f"score={alt['score']} {alt['state']}")
        print(f"                 E={alt['entry']} SL={alt['stop']} TP={alt['targets']} "
              f"R={alt['targetR']}"
              + ("  [先回り指値]" if alt.get("restingLimit") else ""))
        flip = "**方向が変わる**" if alt["side"] != top["side"] else "同じ方向"
        print(f"    選ばれなかった理由: 現行の select_primary は (等級, 点数, TP1 の R, "
              f"モデル順位) だけで並べ、`allowed` を見ない。"
              f"等級 {top['grade']}>{alt['grade']} / 点数 {top['score']}>{alt['score']} "
              f"で WATCH の側が先頭になる。→ {flip}")
        others = [c for c in head["order"] if c is not top and c is not alt]
        if others:
            print("    その周期の他候補: " + " | ".join(
                f"{c['model']}/{c['side']}/{c['grade']}/{c['score']}/"
                f"{'ARMED' if c['allowed'] else 'WATCH'}" for c in others[:3]))


def print_blockers(rows: List[Dict[str, Any]]) -> None:
    print("\n=== 2. WATCH 理由の分類 ===")
    print("GLOBAL   = 市場全体・口座全体の停止理由。**代替候補で迂回してはいけない。**")
    print("CANDIDATE= その候補固有の不成立理由。他の適格候補を止める理由にはならない。")
    print("POLICY   = 契約で外したモデル/型(R89)。設計上、別モデルが primary になってよい。")

    all_b: collections.Counter = collections.Counter()
    for row in rows:
        for c in row["candidates"]:
            for b in c["hardBlockers"]:
                all_b[b] += 1
    print("\n--- 候補に付いた hardBlockers(全周期) ---")
    for blocker, count in all_b.most_common():
        kind = classify(blocker)
        note = (GLOBAL_BLOCKERS.get(blocker) or CANDIDATE_BLOCKERS.get(blocker)
                or ("契約で停止" if kind == "POLICY" else "分類なし"))
        print(f"  {blocker:32s} {count:5d}  [{kind}]  {note}")

    hidden = hidden_rows(rows)
    print(f"\n--- 隠した primary({len(hidden)} 周期)のブロッカー内訳 ---")
    kinds: collections.Counter = collections.Counter()
    per: collections.Counter = collections.Counter()
    for item in hidden:
        for b in item["top"]["hardBlockers"]:
            per[b] += 1
            kinds[classify(b)] += 1
    for blocker, count in per.most_common():
        print(f"  {blocker:32s} {count:5d}  [{classify(blocker)}]")
    has_global = [i for i in hidden if any(classify(b) == "GLOBAL" for b in i["top"]["hardBlockers"])]
    print(f"\n  GLOBAL を含む周期: {len(has_global)} / {len(hidden)}")
    if not has_global:
        print("  → **隠した理由はすべて候補固有**。市場全体・口座全体の停止を代替候補で"
              "迂回する形にはならない。")
    else:
        for item in has_global[:5]:
            print(f"    {item['row']['at'][:19]} {item['top']['model']} "
                  f"{item['top']['hardBlockers']}")

    print("\n--- 候補固有の理由が『他の適格候補まで』止めているか ---")
    print(f"  止められている適格候補がある周期: {len(hidden)}")
    by_alt: collections.Counter = collections.Counter()
    for item in hidden:
        by_alt[f"{item['alt']['model']}/{item['alt']['side']}/{item['alt']['grade']}"] += 1
    for key, count in by_alt.most_common():
        print(f"    {key:44s} {count}")
    flips = sum(1 for i in hidden if i["alt"]["side"] != i["top"]["side"])
    print(f"  うち**方向が変わる**周期: {flips} / {len(hidden)}")

    print("\n--- GLOBAL が選択層で迂回できないことの根拠(コード上の位置) ---")
    print("  ボラ床・イベント窓・取得受領書は `monitor_publish` が **primary を選んだ後**の")
    print("  decision / scenario に当てる(`apply_volatility_grade_gate` / `event gate` /")
    print("  `acquisition_display_gate`)。限月・手動 HALT・AUTO・建玉照会は `nqx_cycle` /")
    print("  `autotrade_engine` が**周期ごと**に止める。どれも『どの候補を primary にするか』")
    print("  より下流なので、選択層の並べ替えでは通らない。")


# ----------------------------------------------------------------- 3. 比較

def _r_after_cost(res: Dict[str, Any], risk_pt: float, cost_pt: float) -> float:
    if res.get("outcome") != "FILLED":
        return 0.0
    r = float(res.get("r") or 0.0)
    if risk_pt and risk_pt > 0:
        r -= cost_pt / risk_pt
    return r


def sequential(rows: List[Dict[str, Any]], variant: str, bars, times, guard_n: float,
               rest_sec: int, cost_pt: float) -> Dict[str, Any]:
    import replay_stop_logic as r90
    table = {}
    for setup in r90.setups_for(rows, variant):
        table[setup["signal"]] = {"setup": setup,
                                  "res": r90.simulate_setup(setup, bars, times, guard_n, rest_sec)}
    items = sorted(table.values(), key=lambda it: it["setup"]["first"])
    busy = 0
    out = {"taken": 0, "filled": 0, "tp1": 0, "loss": 0, "busySkipped": 0, "sumR": 0.0,
           "maxDD": 0.0, "best": 0.0, "trades": [], "table": table}
    equity = peak = 0.0
    for item in items:
        setup, res = item["setup"], item["res"]
        if res.get("outcome") == "NOT_ARMED":
            continue
        start = setup["first"]
        if start < busy:
            out["busySkipped"] += 1
            continue
        out["taken"] += 1
        if res.get("outcome") == "FILLED":
            risk = abs(float(setup["entry"]) - float(setup["stop"]))
            r = _r_after_cost(res, risk, cost_pt)
            out["filled"] += 1
            out["tp1"] += 1 if res.get("tp1") else 0
            out["loss"] += 1 if r < 0 else 0
            out["sumR"] += r
            equity += r
            peak = max(peak, equity)
            out["maxDD"] = min(out["maxDD"], equity - peak)
            out["best"] = max(out["best"], r)
            out["trades"].append({"t": start, "model": setup["model"], "side": setup["side"],
                                  "entry": setup["entry"], "stop": setup["stop"],
                                  "riskPt": risk, "r": r, "tp1": bool(res.get("tp1"))})
            busy = int(res.get("exitT") or start) + BAR3_SEC
        elif res.get("outcome") == "NO_FILL":
            busy = start + rest_sec
    return out


def print_compare(rows: List[Dict[str, Any]], rest_min: int, guard_n: float) -> None:
    import entry_depth
    import replay_stop_logic as r90
    bars = entry_depth.load_bars(SECRETS)
    times = [b["t"] for b in bars]
    fee = fee_per_side()
    print("\n=== 3. 選択層だけの比較(S0 現行 vs S1 selection LIVE) ===")
    print(f"対象 {len(rows)} 周期({rows[0]['at'][:16]} → {rows[-1]['at'][:16]} UTC)")
    print("違うのは `structureContext.selection` の 1 語だけ。shallowCandidate は両方 SHADOW、")
    print("nearTerm も両方 SHADOW、新しい加点は無し。**契約ファイルは書き換えていない。**")
    print(f"費用: FEE_PER_SIDE 中央値 ${fee if fee is not None else '不明'}/枚/片道"
          f"(MNQ 1pt=$2.00 → 往復 {2*(fee or 0)/POINT_VALUE:.2f}pt 相当)。"
          " 滑りは下の表で 0/1/2 tick/片道を当てる。")

    changed = [r for r in rows
               if (r["variants"][S0] or {}).get("decisionId") != (r["variants"][S1] or {}).get("decisionId")]
    flips = [r for r in changed
             if (r["variants"][S0] or {}).get("side") != (r["variants"][S1] or {}).get("side")]
    armed_gain = [r for r in changed
                  if str((r["variants"][S1] or {}).get("state")) in ("ARMED", "ACTIVE")
                  and str((r["variants"][S0] or {}).get("state")) not in ("ARMED", "ACTIVE")]
    print(f"\n--- 周期単位 ---")
    print(f"  primary が変わった周期        {len(changed)}")
    print(f"  うち WATCH → ARMED            {len(armed_gain)}")
    print(f"  うち**建てる方向が変わる**     {len(flips)}")

    print("\n--- 方向が変わる例(先頭 5 件) ---")
    for row in flips[:5]:
        a, b = row["variants"][S0], row["variants"][S1]
        print(f"  {row['at'][:19]}Z 現値 {row.get('price')}")
        print(f"    S0: {a['model']} {a['side']} {a['grade']} {a['state']} "
              f"E={a['entry']} SL={a['stop']} TP={a['targets']} blk={a['hardBlockers']}")
        print(f"    S1: {b['model']} {b['side']} {b['grade']} {b['state']} "
              f"E={b['entry']} SL={b['stop']} TP={b['targets']} 理由={b['selectionReason']}")

    print("\n--- 逐次(同時に 1 建玉だけ。費用込み) ---")
    print("  滑りは片道あたりの tick。どの行も手数料(往復 0.5pt 相当)は入っている。")
    print(f"{'滑り/片道':>16} {'条件':<11} {'取った':>6} {'約定':>5} {'TP1':>4} {'損切':>5} "
          f"{'見送り':>6} {'ΣR':>8} {'最大DD':>8} {'最大勝ち':>9}")
    totals: Dict[Tuple[int, str], Dict[str, Any]] = {}
    for slip in (0, 1, 2):
        cost_pt = cost_points(fee, slip)
        for name in (S0, S1):
            seq = sequential(rows, name, bars, times, guard_n, rest_min * 60, cost_pt)
            totals[(slip, name)] = seq
            label = f"{slip} tick" + ("(手数料のみ)" if slip == 0 else "")
            print(f"{label:>16} {name:<11} {seq['taken']:>6} {seq['filled']:>5} "
                  f"{seq['tp1']:>4} {seq['loss']:>5} {seq['busySkipped']:>6} "
                  f"{seq['sumR']:>+8.2f} {seq['maxDD']:>+8.2f} {seq['best']:>+9.2f}")
        delta = totals[(slip, S1)]["sumR"] - totals[(slip, S0)]["sumR"]
        per = cost_points(fee, slip)
        print(f"{'':>16} 差 S1-S0 = {delta:+.2f}R  "
              f"(1 往復の費用 {per:.2f}pt = SL 20pt なら {per/20:.3f}R)")

    # セットアップ単位(費用込み)
    print("\n--- setups(どちらかで ARMED。費用は 1 tick/片道) ---")
    cost_pt = cost_points(fee, 1)
    tables = {name: sequential(rows, name, bars, times, guard_n, rest_min * 60, cost_pt)["table"]
              for name in (S0, S1)}
    keys = sorted(set(tables[S0]) | set(tables[S1]))
    armed = [k for k in keys
             if any(k in t and t[k]["res"].get("outcome") != "NOT_ARMED" for t in tables.values())]

    def value(table, key):
        item = table.get(key)
        if not item:
            return 0.0
        setup = item["setup"]
        return _r_after_cost(item["res"], abs(float(setup["entry"]) - float(setup["stop"])), cost_pt)

    base = [value(tables[S0], k) for k in armed]
    alt = [value(tables[S1], k) for k in armed]
    mean, lo, hi, prob = r90._bootstrap(base, alt)
    print(f"  setups {len(armed)}  S0 ΣR={sum(base):+.2f}  S1 ΣR={sum(alt):+.2f}  "
          f"差 {mean:+.3f}R/setup [{lo:+.2f},{hi:+.2f}] P(improve)={prob:.2f}")

    # 追加されたトレードだけの成績
    print("\n--- S1 でだけ取れたトレード(費用込み・1 tick/片道) ---")
    s0_seq = sequential(rows, S0, bars, times, guard_n, rest_min * 60, cost_pt)
    s1_seq = sequential(rows, S1, bars, times, guard_n, rest_min * 60, cost_pt)
    s0_keys = {(t["t"], t["model"], t["side"], t["entry"], t["stop"]) for t in s0_seq["trades"]}
    extra = [t for t in s1_seq["trades"]
             if (t["t"], t["model"], t["side"], t["entry"], t["stop"]) not in s0_keys]
    lost = [t for t in s0_seq["trades"]
            if (t["t"], t["model"], t["side"], t["entry"], t["stop"])
            not in {(x["t"], x["model"], x["side"], x["entry"], x["stop"]) for x in s1_seq["trades"]}]
    print(f"  追加された {len(extra)} 件 ΣR={sum(t['r'] for t in extra):+.2f}  "
          f"(勝ち {sum(1 for t in extra if t['r'] > 0)} / 負け {sum(1 for t in extra if t['r'] <= 0)})")
    for t in extra[:8]:
        stamp = datetime.fromtimestamp(t["t"], timezone.utc).strftime("%m-%d %H:%M")
        print(f"    + {stamp}Z {t['model']:22s} {t['side']:4s} E={t['entry']:>9} "
              f"SL={t['stop']:>9} risk={t['riskPt']:>5.2f}pt r={t['r']:+.2f}")
    print(f"  取れなくなった {len(lost)} 件 ΣR={sum(t['r'] for t in lost):+.2f}")
    for t in lost[:8]:
        stamp = datetime.fromtimestamp(t["t"], timezone.utc).strftime("%m-%d %H:%M")
        print(f"    - {stamp}Z {t['model']:22s} {t['side']:4s} E={t['entry']:>9} "
              f"SL={t['stop']:>9} risk={t['riskPt']:>5.2f}pt r={t['r']:+.2f}")
    print("\n  注: R は計画の SL 幅で割った値。費用は FEE_PER_SIDE と明示した滑りだけで、")
    print("  約定行列・部分約定・イベント時のギャップは含まない。標本はこの期間のものだけ。")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="R122 selection stage only (read-only)")
    parser.add_argument("--audit", action="store_true")
    parser.add_argument("--blockers", action="store_true")
    parser.add_argument("--compare", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--rest-min", type=int, default=30)
    parser.add_argument("--guard-n", type=float, default=1.0)
    parser.add_argument("--cache", default=os.path.join(SECRETS, "replay_selection.json"))
    parser.add_argument("--reevaluate", action="store_true")
    args = parser.parse_args(argv)
    if not (args.audit or args.blockers or args.compare):
        parser.print_help()
        return 0
    paths = sorted(glob.glob(os.path.join(SECRETS, "monitor_cycle_*.json")))
    if args.limit:
        paths = paths[:args.limit]
    rows: List[Dict[str, Any]] = []
    if not args.reevaluate and os.path.exists(args.cache):
        try:
            payload = json.load(io.open(args.cache, encoding="utf-8"))
            if payload.get("count") == len(paths) and payload.get("schema") == "R122-SELECTION/1":
                rows = payload["rows"]
        except (OSError, ValueError, KeyError):
            rows = []
    if not rows:
        print(f"再評価 {len(paths)} バンドル × 2 条件 …")
        rows = evaluate_all(paths)
        try:
            json.dump({"schema": "R122-SELECTION/1", "count": len(paths), "rows": rows},
                      io.open(args.cache, "w", encoding="utf-8"), ensure_ascii=False, default=str)
        except OSError:
            pass
    rows = [r for r in rows if r.get("variants")]
    rows.sort(key=lambda r: r["t"])
    if not rows:
        print("対象バンドルなし")
        return 2
    if args.audit:
        print_audit(rows)
    if args.blockers:
        print_blockers(rows)
    if args.compare:
        print_compare(rows, args.rest_min, args.guard_n)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""モデル別スコアカード(R48)。

決済記録(result)を decision の model / grade へ紐付け、モデル別の実測成績
(N・勝率・粗PF・平均R・合計$)を集計する。**表示と台帳のみ** — 発注可否・
採点・実行契約には一切影響しない。

背景(EDGE_LEDGER.md R48): ICT/Alchemist/RTM には公開されたエッジ証拠が無い。
「PFが良い」と宣言できる唯一の道は自前計測であり、R47 で result に
scenarioId が付いたことで decision → 発注 → 決済の紐付けが機械化できる。
このモジュールがその紐付けの正本になる。

紐付けの優先順位:
  1. result.model / result.grade(R48 以降、凍結プランから焼き込み)
  2. result.scenarioId → `.secrets/autotrade_ledger.jsonl` の凍結プラン
  3. どちらも無ければ UNATTRIBUTED(推測で埋めない)

累積台帳: `.secrets/model_scorecard.jsonl`(resultId ごとに1行、追記のみ)。
Durable Object の resultLog は直近50件しか持たないため、長期の正本はここ。

R103-0: 行に `excursion`(MAE/MFE・SL 抜け幅・決済後の TP1 到達・huntClass)を足した。
計算は `excursion_metrics.classify`(純関数)。確定 3 分足は呼び出し側(trade_journal)が
注入し、無ければ result.chart の足を使う。足が無ければ `excursion` は null(推測しない)。

    python model_scorecard.py            # 集計テーブルを表示
    python model_scorecard.py --json     # JSON
    python model_scorecard.py --sync     # DO の recentResults を取り込んでから表示
    python model_scorecard.py --excursions                    # 刈られ方の内訳
    python model_scorecard.py --backfill-excursions <bars_dir># 既存行を再計算して追記
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

import excursion_metrics  # noqa: E402

SCORECARD_FILE = os.path.join(BASE, ".secrets", "model_scorecard.jsonl")
AUTOTRADE_LEDGER = os.path.join(BASE, ".secrets", "autotrade_ledger.jsonl")

#: 勝敗判定のコスト床(pt)。nqx_state.COST_FLOOR_PT / 表示層と同値に保つこと。
COST_FLOOR_PT = 2.0


def _num(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed == parsed and abs(parsed) != float("inf") else None


# ---------------------------------------------------------------- 紐付け

def plan_index(ledger_path: str = AUTOTRADE_LEDGER) -> Dict[str, Dict[str, Any]]:
    """autotrade台帳から scenarioId → {model, grade} を引けるようにする。

    台帳が壊れていても例外を出さない(スコアカードは監視を止めない)。
    後の記録が同じ scenarioId を持つ場合は後勝ち(グレード再評価を反映)。
    """
    index: Dict[str, Dict[str, Any]] = {}
    try:
        with open(ledger_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                plan = record.get("plan")
                if not isinstance(plan, dict):
                    continue
                scenario_id = str(plan.get("scenarioId") or "")
                if not scenario_id:
                    continue
                index[scenario_id] = {
                    "model": plan.get("model") or None,
                    "grade": plan.get("grade") or None,
                    "evidence": [str(x) for x in (plan.get("decisionEvidence") or [])][:16],
                }
    except OSError:
        pass
    return index


def excursion_of(row: Dict[str, Any], bars: Any = None,
                 tp1: Any = None, noise: Any = None) -> Optional[Dict[str, Any]]:
    """行 1 件の刈られ方(R103-0)。足が無ければ None(推測しない)。

    ``tp1`` と ``noise`` は再計算(--backfill-excursions)で使えるように結果へも残す。
    """
    rows = excursion_metrics.normalize_bars(bars)
    if not rows:
        return None
    metrics = excursion_metrics.classify(row, rows, tp1=tp1, noise=noise)
    metrics["tp1"] = _num(tp1)
    metrics["noise"] = _num(noise)
    metrics["bars"] = len(rows)
    return metrics


def _chart_inputs(result: Dict[str, Any]) -> Tuple[Any, Any, Any]:
    """result.chart(R57 の根拠チャート。ローカルに既にある足)から (bars, tp1, noise)。"""
    chart = result.get("chart") if isinstance(result, dict) else None
    if not isinstance(chart, dict):
        return None, None, None
    return chart.get("bars"), chart.get("tp1"), chart.get("noise")


def classify(result: Dict[str, Any],
             plans: Optional[Dict[str, Dict[str, Any]]] = None,
             *, bars: Any = None, tp1: Any = None,
             noise: Any = None) -> Optional[Dict[str, Any]]:
    """result 1件をスコアカード行へ変換する。数値が揃わなければ None。

    R103-0: ``bars`` / ``tp1`` / ``noise`` を渡すと行に ``excursion`` が載る。
    省略時は result.chart の足を使い、それも無ければ ``excursion`` は None。
    """
    if not isinstance(result, dict):
        return None
    result_id = str(result.get("resultId") or "")
    entry, exit_price = _num(result.get("entry")), _num(result.get("exit"))
    stop = _num(result.get("stop"))
    point_value = _num(result.get("pointValue")) or 2.0
    qty = result.get("qty")
    side = str(result.get("side") or "").upper()
    if not result_id or entry is None or exit_price is None or side not in ("LONG", "SHORT"):
        return None
    try:
        qty = int(qty)
    except (TypeError, ValueError):
        return None
    if qty < 1:
        return None

    model = result.get("model") or None
    grade = result.get("grade") or None
    scenario_id = str(result.get("scenarioId") or "") or None
    attributed = (plans or {}).get(scenario_id) or {} if scenario_id else {}
    if not model:
        model = attributed.get("model") or None
        grade = grade or attributed.get("grade") or None
    evidence_tags = attributed.get("evidence") or []
    direction = 1.0 if side == "LONG" else -1.0
    gross = (exit_price - entry) * direction * point_value * qty
    fees = _num(result.get("fees"))
    net = gross - fees if fees is not None else gross
    risk_pt = abs(entry - stop) if stop is not None else None
    r_multiple = (net / (risk_pt * point_value * qty)
                  if risk_pt and risk_pt > 0 else None)
    move_pt = abs(exit_price - entry)
    outcome = ("FLAT" if move_pt < COST_FLOOR_PT
               else ("WIN" if net > 0 else "LOSS"))
    row = {
        "resultId": result_id,
        "closedAt": result.get("closedAt"),
        # R57: 建玉時刻も残す(日誌の保有時間・MAE/MFE の区間に要る。無ければ None)。
        "openedAt": result.get("openedAt") or None,
        "model": str(model) if model else "UNATTRIBUTED",
        "grade": str(grade) if grade else None,
        "scenarioId": scenario_id,
        "accountId": result.get("accountId") or None,
        "mode": result.get("mode") or None,
        "side": side,
        "qty": qty,
        "entry": entry,
        "exit": exit_price,
        "stop": stop,
        "pointValue": point_value,
        "fees": fees,
        "pnlGross": round(gross, 2),
        "pnlNet": round(net, 2),
        "rMultiple": round(r_multiple, 3) if r_multiple is not None else None,
        "outcome": outcome,
        # R48: 凍結時の decision.evidence。特徴の有無で成績が分かれるかを
        # 後から実測するための分離キー(重み付けは N が貯まるまでしない)。
        "evidenceTags": evidence_tags,
    }
    # R103-0: 刈られ方の計測。足が無ければ None。
    if bars is None and tp1 is None and noise is None:
        bars, tp1, noise = _chart_inputs(result)
    row["excursion"] = excursion_of(row, bars=bars, tp1=tp1, noise=noise)
    return row


# ---------------------------------------------------------------- 台帳

def load_rows(path: str = SCORECARD_FILE) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict) and row.get("resultId"):
                    rows.append(row)
    except OSError:
        pass
    # R54: 訂正は追記で行う(行を書き換えない)。同じ resultId が複数あれば
    # **最後の 1 行だけ**を採る —— 手数料の訂正が PF に反映され、かつ台帳には
    # 訂正前の行がそのまま証跡として残る。
    latest: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        latest[str(row["resultId"])] = row
    return list(latest.values())


def record(result: Dict[str, Any], path: str = SCORECARD_FILE,
           plans: Optional[Dict[str, Dict[str, Any]]] = None,
           *, bars: Any = None, tp1: Any = None,
           noise: Any = None) -> Optional[Dict[str, Any]]:
    """publish 済み result を1件、台帳へ追記する(重複は resultId で排除)。

    どこで失敗しても例外を出さない — 記録の失敗で決済経路を巻き込まない。
    ``bars`` / ``tp1`` / ``noise`` は R103-0 の計測用(注入。無ければ result.chart)。
    """
    try:
        row = classify(result, plans=plans if plans is not None else plan_index(),
                       bars=bars, tp1=tp1, noise=noise)
        if row is None:
            return None
        # R54: 同じ resultId でも金額が変われば訂正として追記する。2026-09-07 に
        # 手数料を realizedPnL から取り直したとき、旧行が残ると PF が古い純額の
        # ままになった。金額が同じなら従来どおり何もしない(二重計上を防ぐ)。
        MONEY = ("exit", "qty", "fees", "pnlGross", "pnlNet", "outcome", "rMultiple")
        previous = {r["resultId"]: r for r in load_rows(path)}.get(row["resultId"])
        if previous is not None:
            if all(previous.get(key) == row.get(key) for key in MONEY):
                return None
            row["supersedes"] = str(previous.get("recordedAt") or "")
        row["recordedAt"] = datetime.now(timezone.utc).isoformat()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        return row
    except Exception:  # noqa: BLE001
        return None


def ingest(results: Iterable[Dict[str, Any]], path: str = SCORECARD_FILE) -> List[Dict[str, Any]]:
    """result の列(例: DO の recentResults)をまとめて取り込む。"""
    plans = plan_index()
    appended = []
    for result in results or []:
        row = record(result, path=path, plans=plans)
        if row:
            appended.append(row)
    return appended


# ---------------------------------------------------------------- 集計

def summarize(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """モデル別・グレード別の集計。PF は損失が無い間は None(∞を発明しない)。"""
    def _bucket() -> Dict[str, Any]:
        return {"n": 0, "wins": 0, "losses": 0, "flats": 0,
                "grossProfit": 0.0, "grossLoss": 0.0, "netTotal": 0.0,
                "rSum": 0.0, "rCount": 0}

    def _add(bucket: Dict[str, Any], row: Dict[str, Any]) -> None:
        bucket["n"] += 1
        bucket["netTotal"] += row["pnlNet"]
        if row["outcome"] == "WIN":
            bucket["wins"] += 1
            bucket["grossProfit"] += max(0.0, row["pnlNet"])
        elif row["outcome"] == "LOSS":
            bucket["losses"] += 1
            bucket["grossLoss"] += abs(min(0.0, row["pnlNet"]))
        else:
            bucket["flats"] += 1
            # FLAT でも損益は netTotal に載っている。PF の分子分母には含めない。
        if row.get("rMultiple") is not None:
            bucket["rSum"] += row["rMultiple"]
            bucket["rCount"] += 1

    def _final(bucket: Dict[str, Any]) -> Dict[str, Any]:
        decided = bucket["wins"] + bucket["losses"]
        return {
            "n": bucket["n"], "wins": bucket["wins"], "losses": bucket["losses"],
            "flats": bucket["flats"],
            "winRate": round(bucket["wins"] / decided, 3) if decided else None,
            "grossProfit": round(bucket["grossProfit"], 2),
            "grossLoss": round(bucket["grossLoss"], 2),
            "pf": (round(bucket["grossProfit"] / bucket["grossLoss"], 3)
                   if bucket["grossLoss"] > 1e-9 else None),
            "netTotal": round(bucket["netTotal"], 2),
            "avgR": (round(bucket["rSum"] / bucket["rCount"], 3)
                     if bucket["rCount"] else None),
        }

    models: Dict[str, Dict[str, Any]] = {}
    grades: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for row in rows:
        model = row.get("model") or "UNATTRIBUTED"
        models.setdefault(model, _bucket())
        _add(models[model], row)
        grade = row.get("grade") or "?"
        grades.setdefault((model, grade), _bucket())
        _add(grades[(model, grade)], row)
    return {
        "totalResults": len(rows),
        "models": {model: _final(bucket) for model, bucket in models.items()},
        "byGrade": {f"{model}|{grade}": _final(bucket)
                    for (model, grade), bucket in grades.items()},
        # 憲法(BLUEPRINT §0)の宣言条件への距離。PF そのものを宣言しない。
        "declarationNote": ("実測は過去実績であり将来の保証ではない。"
                            "PF≥1.5 / N≥200 / OOS PF≥1.3 を満たすまで『PFが良い』とは宣言しない"),
    }


def summary_line(summary: Dict[str, Any]) -> str:
    parts = []
    for model, stats in sorted((summary.get("models") or {}).items()):
        pf = stats.get("pf")
        parts.append(f"{model.replace('_REVERSION', '').replace('_REVERSAL', '')}"
                     f" N={stats['n']}"
                     f" PF={'—' if pf is None else pf}"
                     f" ${stats['netTotal']:+,.0f}")
    return "scorecard: " + (" | ".join(parts) if parts else "no attributed results yet")


# ---------------------------------------------------------------- 刈られ方の集計(R103-0)

def _median(values: List[float]) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return round(ordered[mid], 2)
    return round((ordered[mid - 1] + ordered[mid]) / 2.0, 2)


def excursion_summary(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """モデル×等級ごとの huntClass 内訳と、beyondStopPt / tp1AfterExitMin の中央値。

    計測がまだ無い行(足が無い・古い行)は ``missing`` に数える。推測で埋めない。
    """
    def _bucket() -> Dict[str, Any]:
        return {"n": 0, "missing": 0, "classes": {name: 0 for name in excursion_metrics.HUNT_CLASSES},
                "unclassified": 0, "_beyond": [], "_tp1": []}

    def _add(bucket: Dict[str, Any], row: Dict[str, Any]) -> None:
        bucket["n"] += 1
        metrics = row.get("excursion")
        if not isinstance(metrics, dict):
            bucket["missing"] += 1
            return
        hunt = metrics.get("huntClass")
        if hunt in bucket["classes"]:
            bucket["classes"][hunt] += 1
        else:
            bucket["unclassified"] += 1
        beyond = _num(metrics.get("beyondStopPt"))
        if beyond is not None:
            bucket["_beyond"].append(beyond)
        tp1 = _num(metrics.get("tp1AfterExitMin"))
        if tp1 is not None:
            bucket["_tp1"].append(tp1)

    def _final(bucket: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "n": bucket["n"], "missing": bucket["missing"],
            "classes": dict(bucket["classes"]), "unclassified": bucket["unclassified"],
            "beyondStopPtMedian": _median(bucket["_beyond"]),
            "beyondStopN": len(bucket["_beyond"]),
            "tp1AfterExitMinMedian": _median(bucket["_tp1"]),
            "tp1AfterExitN": len(bucket["_tp1"]),
        }

    models: Dict[str, Dict[str, Any]] = {}
    grades: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for row in rows:
        model = row.get("model") or "UNATTRIBUTED"
        models.setdefault(model, _bucket())
        _add(models[model], row)
        grade = row.get("grade") or "?"
        grades.setdefault((model, grade), _bucket())
        _add(grades[(model, grade)], row)
    return {
        "totalResults": len(rows),
        "models": {model: _final(bucket) for model, bucket in models.items()},
        "byGrade": {f"{model}|{grade}": _final(bucket)
                    for (model, grade), bucket in grades.items()},
        "thresholds": {
            "stopWindowMin": excursion_metrics.STOP_WINDOW_MIN,
            "tp1WindowMin": excursion_metrics.TP1_WINDOW_MIN,
            "favWindowMin": excursion_metrics.FAV_WINDOW_MIN,
            "huntNoiseN": excursion_metrics.HUNT_NOISE_N,
            "huntFavSlMult": excursion_metrics.HUNT_FAV_SL_MULT,
            "wrongWayFavSlMult": excursion_metrics.WRONG_WAY_FAV_SL_MULT,
        },
    }


def load_bars_dir(bars_dir: str) -> List[Dict[str, float]]:
    """``<bars_dir>/*.json`` の確定 3 分足を 1 本の列にまとめる(時刻で重複排除)。

    受ける形: ``{"bars": [...]}`` / ``{"bars3m": [...]}`` / 素の配列。
    ``{t,o,h,l,c}`` でも ``[t,o,h,l,c]`` でもよい。読めないファイルは黙って飛ばす。
    """
    merged: Dict[float, Dict[str, float]] = {}
    try:
        names = sorted(n for n in os.listdir(bars_dir) if n.endswith(".json"))
    except OSError:
        return []
    for name in names:
        try:
            with open(os.path.join(bars_dir, name), encoding="utf-8") as fh:
                payload = json.load(fh)
        except (OSError, ValueError):
            continue
        raw = payload
        if isinstance(payload, dict):
            raw = payload.get("bars") or payload.get("bars3m") or []
        for bar in excursion_metrics.normalize_bars(raw):
            merged[bar["t"]] = bar
    return [merged[t] for t in sorted(merged)]


def backfill_excursions(bars_dir: str, path: str = SCORECARD_FILE) -> Dict[str, Any]:
    """既存行を再計算し、変わった行だけ supersedes 付きで**追記**する。

    既存行は消さない・書き換えない(R54 と同じ訂正の作法)。
    tp1 / noise は行に残っている前回の入力を使い、無ければ None のまま。
    """
    bars = load_bars_dir(bars_dir)
    rows = load_rows(path)
    appended: List[Dict[str, Any]] = []
    if not bars:
        return {"bars": 0, "rows": len(rows), "appended": 0, "rowsAppended": appended}
    for row in rows:
        previous = row.get("excursion") if isinstance(row.get("excursion"), dict) else {}
        metrics = excursion_of(row, bars=bars,
                               tp1=previous.get("tp1"), noise=previous.get("noise"))
        if metrics is None or metrics == row.get("excursion"):
            continue
        new_row = dict(row)
        new_row["excursion"] = metrics
        new_row["supersedes"] = str(row.get("recordedAt") or "")
        new_row["recordedAt"] = datetime.now(timezone.utc).isoformat()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(new_row, ensure_ascii=False) + "\n")
        appended.append(new_row)
    return {"bars": len(bars), "rows": len(rows), "appended": len(appended),
            "rowsAppended": appended}


# ---------------------------------------------------------------- CLI

def _utf8_stdout() -> None:
    """cp932 のコンソールでも「—」で落ちないようにする(既存 main() の UnicodeEncodeError)。"""
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass


def _print_excursions(rows: List[Dict[str, Any]], as_json: bool) -> None:
    summary = excursion_summary(rows)
    if as_json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return
    names = list(excursion_metrics.HUNT_CLASSES)
    header = (f"{'MODEL':<24} {'GRADE':<6} {'N':>4} "
              + " ".join(f"{name[:9]:>9}" for name in names)
              + f" {'beyond':>8} {'tp1min':>7} {'足なし':>6}")
    print(header)
    print("-" * len(header))

    def _line(label: str, grade: str, stats: Dict[str, Any]) -> None:
        counts = " ".join(f"{stats['classes'][name]:>9}" for name in names)
        beyond = "—" if stats["beyondStopPtMedian"] is None else f"{stats['beyondStopPtMedian']:.2f}"
        tp1 = "—" if stats["tp1AfterExitMinMedian"] is None else f"{stats['tp1AfterExitMinMedian']:.0f}"
        print(f"{label:<24} {grade:<6} {stats['n']:>4} {counts} {beyond:>8} {tp1:>7} "
              f"{stats['missing']:>6}")

    for model, stats in sorted((summary.get("models") or {}).items()):
        _line(model, "ALL", stats)
        for key, gstats in sorted((summary.get("byGrade") or {}).items()):
            gmodel, grade = key.split("|", 1)
            if gmodel == model:
                _line("", grade, gstats)
    print("\nbeyond = 決済後 15 分の SL 抜け幅の中央値(pt)・"
          "tp1min = 決済後に TP1 へ届くまでの中央値(分。届いた件だけ)")
    print("STOP_HUNT/WRONG_WAY/DEEP は損切りの分類。しきい値は excursion_metrics の docstring。")


def main(argv: Optional[List[str]] = None) -> int:
    import argparse

    _utf8_stdout()
    parser = argparse.ArgumentParser(description="モデル別スコアカード(表示専用)")
    parser.add_argument("--json", action="store_true", help="JSON で出力")
    parser.add_argument("--sync", action="store_true",
                        help="Durable Object の recentResults を取り込んでから集計")
    parser.add_argument("--excursions", action="store_true",
                        help="R103: モデル×等級ごとの huntClass 内訳と中央値を表示")
    parser.add_argument("--backfill-excursions", metavar="BARS_DIR",
                        help="R103: <BARS_DIR>/*.json の確定 3 分足で既存行を再計算し、"
                             "supersedes 付きの新行を追記する(既存行は消さない)")
    args = parser.parse_args(argv)

    if args.sync:
        import nqx_state
        view = nqx_state.fetch_state_quiet()
        results = (view or {}).get("recentResults") or []
        appended = ingest(results)
        print(f"sync: {len(appended)} new result(s) ingested from server "
              f"({len(results)} visible)")

    if args.backfill_excursions:
        stats = backfill_excursions(args.backfill_excursions)
        print(f"backfill: {stats['appended']} row(s) appended "
              f"({stats['rows']} existing row(s), {stats['bars']} bar(s) loaded)")
        if not stats["bars"]:
            print("足が 1 本も読めなかった — 追記していない(推測で埋めない)")

    rows = load_rows()
    if args.excursions:
        _print_excursions(rows, args.json)
        return 0
    summary = summarize(rows)
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0
    print(f"results recorded: {summary['totalResults']}")
    header = f"{'MODEL':<24} {'GRADE':<6} {'N':>4} {'WIN%':>6} {'PF':>7} {'NET$':>10} {'avgR':>6}"
    print(header)
    print("-" * len(header))
    for model, stats in sorted((summary.get("models") or {}).items()):
        win = "—" if stats["winRate"] is None else f"{stats['winRate'] * 100:.0f}%"
        pf = "—" if stats["pf"] is None else f"{stats['pf']:.2f}"
        avg_r = "—" if stats["avgR"] is None else f"{stats['avgR']:+.2f}"
        print(f"{model:<24} {'ALL':<6} {stats['n']:>4} {win:>6} {pf:>7} "
              f"{stats['netTotal']:>+10,.0f} {avg_r:>6}")
        for key, gstats in sorted((summary.get("byGrade") or {}).items()):
            gmodel, grade = key.split("|", 1)
            if gmodel != model:
                continue
            gwin = "—" if gstats["winRate"] is None else f"{gstats['winRate'] * 100:.0f}%"
            gpf = "—" if gstats["pf"] is None else f"{gstats['pf']:.2f}"
            gavg = "—" if gstats["avgR"] is None else f"{gstats['avgR']:+.2f}"
            print(f"{'':<24} {grade:<6} {gstats['n']:>4} {gwin:>6} {gpf:>7} "
                  f"{gstats['netTotal']:>+10,.0f} {gavg:>6}")
    print(f"\n{summary['declarationNote']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

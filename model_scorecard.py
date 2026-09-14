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

    python model_scorecard.py            # 集計テーブルを表示
    python model_scorecard.py --json     # JSON
    python model_scorecard.py --sync     # DO の recentResults を取り込んでから表示
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

BASE = os.path.dirname(os.path.abspath(__file__))
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


def classify(result: Dict[str, Any],
             plans: Optional[Dict[str, Dict[str, Any]]] = None) -> Optional[Dict[str, Any]]:
    """result 1件をスコアカード行へ変換する。数値が揃わなければ None。"""
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
    return {
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
           plans: Optional[Dict[str, Dict[str, Any]]] = None) -> Optional[Dict[str, Any]]:
    """publish 済み result を1件、台帳へ追記する(重複は resultId で排除)。

    どこで失敗しても例外を出さない — 記録の失敗で決済経路を巻き込まない。
    """
    try:
        row = classify(result, plans=plans if plans is not None else plan_index())
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


# ---------------------------------------------------------------- CLI

def main(argv: Optional[List[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="モデル別スコアカード(表示専用)")
    parser.add_argument("--json", action="store_true", help="JSON で出力")
    parser.add_argument("--sync", action="store_true",
                        help="Durable Object の recentResults を取り込んでから集計")
    args = parser.parse_args(argv)

    if args.sync:
        import nqx_state
        view = nqx_state.fetch_state_quiet()
        results = (view or {}).get("recentResults") or []
        appended = ingest(results)
        print(f"sync: {len(appended)} new result(s) ingested from server "
              f"({len(results)} visible)")

    rows = load_rows()
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

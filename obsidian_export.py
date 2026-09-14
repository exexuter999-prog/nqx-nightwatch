#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Obsidian 保管庫へのトレードデータ書き出し(R57)。

`.secrets/model_scorecard.jsonl`(決済 1 件 = 1 行)と autotrade 台帳の凍結プランを突合し、
保管庫の `Trades/` に **1 トレード 1 ノート** を生成する。frontmatter は Obsidian Bases /
Dataview で集計できる正規化キー、本文の `<!-- nqx:auto-end -->` より下は人のメモとして
再生成しても保持する。`Models/<MODEL>.md` が無ければ観察ノートの雛形を作る。

    python obsidian_export.py                # 既定の保管庫へ書き出し
    python obsidian_export.py --dry-run      # 書かずに差分だけ表示
    python obsidian_export.py --vault PATH   # 保管庫を指定(既定: NQX_OBSIDIAN_VAULT か C:\\Users\\exexu\\FLEX)

取引経路には一切触れない(読み取り専用)。失敗しても監視ループの終了コードに影響しない
よう、監視ループからは呼ばず、セッション終了時に人が 1 回叩く。
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

import model_scorecard  # noqa: E402
import obsidian_charts  # noqa: E402
import obsidian_metrics  # noqa: E402

DEFAULT_VAULT = os.environ.get("NQX_OBSIDIAN_VAULT") or r"C:\Users\exexu\FLEX"
SCORECARD_FILE = os.path.join(BASE, ".secrets", "model_scorecard.jsonl")
LEDGER_FILE = os.path.join(BASE, ".secrets", "autotrade_ledger.jsonl")
AUDIT_GLOB = os.path.join(BASE, ".secrets", "monitor_cycle_*.json")
AUTO_END = "<!-- nqx:auto-end -->"
JST = timezone(timedelta(hours=9))
MODELS = ("VP80_REVERSION", "TURTLE_SOUP_REVERSAL", "BREAKER_CONTINUATION", "OTE_FVG_PULLBACK")
POINT_VALUE = 2.0


# ---------------------------------------------------------------- 読み込み

def _jsonl(path: str) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    if not os.path.exists(path):
        return rows
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


def _num(value: Any) -> Optional[float]:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out else None


def mask_account(value: Any) -> str:
    """口座 ID は先頭 3 + … + 末尾 4 だけ残す(CLAUDE.md §7: 口座情報を出力に露出しない)。"""
    text = str(value or "")
    return f"{text[:3]}…{text[-4:]}" if len(text) > 8 else text


def plan_index(ledger_path: str = LEDGER_FILE) -> Dict[str, Dict[str, Any]]:
    """scenarioId → 凍結プランの要約(後勝ち)。model/grade/経路/ULTRA/リスクを持つ。"""
    index: Dict[str, Dict[str, Any]] = {}
    for record in _jsonl(ledger_path):
        plan = record.get("plan")
        if not isinstance(plan, dict):
            continue
        scenario_id = str(plan.get("scenarioId") or "")
        if not scenario_id:
            continue
        legs = plan.get("legs") if isinstance(plan.get("legs"), list) else []
        # claim 時刻(=建玉時刻の近似)は **最初の ENTRY_CLAIMED** のもの。後続の MANAGEMENT や
        # 所有権再束縛の記録も同じプランを運ぶので、後勝ちにすると IN が MODIFY の時刻になる
        # (2026-09-05 02:05 のトレードが 02:49 と描かれた)。
        first_claim = (index.get(scenario_id) or {}).get("claimedAt")
        if record.get("status") == "ENTRY_CLAIMED" and not first_claim:
            first_claim = record.get("time")
        # 約定時刻は経路の routeSnapshot(ACCEPTED 行の filledAt)から。最初の約定を採る。
        filled_at = (index.get(scenario_id) or {}).get("filledAt")
        for leg in (plan.get("routeSnapshot") or []):
            if isinstance(leg, dict) and leg.get("filledAt"):
                stamp = str(leg["filledAt"])
                filled_at = stamp if (filled_at is None or stamp < filled_at) else filled_at
        index[scenario_id] = {
            "filledAt": filled_at,
            "scenarioId": scenario_id,
            "side": str(plan.get("side") or "").upper(),
            "model": plan.get("model") or None,
            "grade": plan.get("grade") or None,
            "entryKey": plan.get("entryKey"),
            "entryOrderType": plan.get("entryOrderType"),
            "entryReference": _num(plan.get("entryReference")),
            "plannedEntry": _num(plan.get("plannedEntry") or plan.get("entry")),
            "initialStop": _num(plan.get("initialStop")),
            "tp1": _num(plan.get("tp1")),
            "finalTarget": _num(plan.get("finalTarget")),
            "qty": plan.get("qty"),
            "legs": [{"id": leg.get("id"), "qty": leg.get("qty"), "target": leg.get("target")}
                     for leg in legs if isinstance(leg, dict)],
            "riskDollars": _num(plan.get("riskDollars")),
            "riskCapSource": plan.get("riskCapSource"),
            "ultra": bool(plan.get("ultra")) or plan.get("riskCapSource") == "ACCOUNT_DRAWDOWN_BUFFER",
            "evidence": [str(x) for x in (plan.get("decisionEvidence") or [])][:16],
            "claimedAt": first_claim or record.get("time"),
            "accountScope": [mask_account(a) for a in (plan.get("accountScope") or [])],
        }
    return index


def audit_model_index(pattern: str = AUDIT_GLOB) -> Dict[str, Dict[str, Any]]:
    """当日の監査バンドル(`monitor_cycle_HHMM.json`)から scenarioId → model/grade を引く。

    凍結プランに model が無い記録(R56 以前)の後追い用。監査ファイルは HHMM 名で
    日ごとに上書きされるので、当日分しか埋まらない。
    """
    index: Dict[str, Dict[str, Any]] = {}
    for path in sorted(glob.glob(pattern)):
        try:
            with open(path, encoding="utf-8") as fh:
                bundle = json.load(fh)
        except (OSError, json.JSONDecodeError):
            continue
        primary = ((bundle.get("scenarios") or {}).get("primary")
                   if isinstance(bundle.get("scenarios"), dict) else None)
        if not isinstance(primary, dict):
            continue
        scenario_id = str(primary.get("scenarioId") or "")
        model = primary.get("model")
        if scenario_id and isinstance(model, str) and model and model != "FLAT":
            index[scenario_id] = {"model": model, "grade": primary.get("grade")}
    return index


# ---------------------------------------------------------------- 変換

#: scenarioId の無い決済を凍結プランへ寄せる時間窓(分)。claim → 決済がこの内側で、
#: 方向・枚数が一致するプランが **1 つだけ** のときに限る。
MATCH_WINDOW_MIN = 60


def match_plan_by_time(row: Dict[str, Any], plans: Dict[str, Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    closed = _to_jst(row.get("closedAt"))
    side = {"SHORT": "SELL", "LONG": "BUY"}.get(str(row.get("side") or "").upper())
    try:
        qty = int(row.get("qty") or 0)
    except (TypeError, ValueError):
        return None
    if closed is None or not side or qty <= 0:
        return None
    hits = []
    for plan in plans.values():
        claimed = _to_jst(plan.get("claimedAt"))
        if claimed is None or plan.get("side") != side:
            continue
        try:
            plan_qty = int(plan.get("qty") or 0)
        except (TypeError, ValueError):
            continue
        if plan_qty != qty:
            continue
        delta = (closed - claimed).total_seconds() / 60.0
        if 0 <= delta <= MATCH_WINDOW_MIN:
            hits.append((delta, plan))
    if len(hits) != 1:
        return None
    return hits[0][1]


def _to_jst(value: Any) -> Optional[datetime]:
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(JST)


def build_trade(row: Dict[str, Any], plans: Dict[str, Dict[str, Any]],
                audit: Dict[str, Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """スコアカード 1 行 → ノート用の正規化レコード。"""
    result_id = str(row.get("resultId") or "")
    closed = _to_jst(row.get("closedAt"))
    if not result_id or closed is None:
        return None
    scenario_id = str(row.get("scenarioId") or "") or None
    plan = plans.get(scenario_id or "", {})
    backfill = audit.get(scenario_id or "", {})
    matched_by_time = False
    if not plan and scenario_id is None:
        # scenarioId を持たない決済(fills 経由で戦績に載った建玉)。直前 MATCH_WINDOW_MIN 分
        # 以内に claim された同方向・同枚数の凍結プランが 1 つだけあれば、それを属性に使う。
        # 手動建玉は枚数が一致しないか窓の外なので付かない(付かなければ UNATTRIBUTED のまま)。
        candidate = match_plan_by_time(row, plans)
        if candidate:
            plan, matched_by_time = candidate, True
            backfill = audit.get(str(candidate.get("scenarioId") or ""), {})
    model = (row.get("model") if row.get("model") not in (None, "", "UNATTRIBUTED") else None) \
        or plan.get("model") or backfill.get("model") or "UNATTRIBUTED"
    grade = row.get("grade") or plan.get("grade") or backfill.get("grade") or None
    entry, exit_price = _num(row.get("entry")), _num(row.get("exit"))
    stop = _num(row.get("stop")) if row.get("stop") is not None else plan.get("initialStop")
    qty = int(row.get("qty") or 0)
    side = str(row.get("side") or "").upper()
    pnl_net = _num(row.get("pnlNet"))
    pnl_gross = _num(row.get("pnlGross"))
    r_multiple = _num(row.get("rMultiple"))
    if r_multiple is None and pnl_net is not None and stop is not None and entry is not None and qty:
        risk_pt = abs(entry - stop)
        if risk_pt > 0:
            r_multiple = round(pnl_net / (risk_pt * POINT_VALUE * qty), 3)
    route = "auto" if plan else "manual"
    slip = None
    if plan.get("entryOrderType") == "MARKET" and plan.get("entryReference") is not None and entry is not None:
        slip = round(abs(entry - float(plan["entryReference"])), 2)
    return {
        "resultId": result_id,
        "closedAt": closed,
        "date": closed.strftime("%Y-%m-%d"),
        "account": mask_account(row.get("accountId")),
        "mode": row.get("mode") or None,
        "model": model,
        "grade": grade,
        "scenarioId": scenario_id or (plan.get("scenarioId") if matched_by_time else None),
        "side": side,
        "qty": qty,
        "entry": entry,
        "exit": exit_price,
        "stop": stop,
        "tp1": plan.get("tp1"),
        "finalTarget": plan.get("finalTarget"),
        "entryType": plan.get("entryOrderType"),
        "ultra": bool(plan.get("ultra")) if plan else False,
        "riskDollars": plan.get("riskDollars"),
        "slippage": slip,
        "pnlGross": pnl_gross,
        "pnlNet": pnl_net,
        "fees": _num(row.get("fees")),
        "rMultiple": r_multiple,
        "outcome": row.get("outcome") or None,
        "evidence": list(row.get("evidenceTags") or plan.get("evidence") or []),
        "route": route,
        "modelSource": ("result" if row.get("model") not in (None, "", "UNATTRIBUTED")
                        else ("ledger-time" if matched_by_time and (plan.get("model") or backfill.get("model"))
                              else "plan" if plan.get("model")
                              else "audit" if backfill.get("model") else "none")),
        # 建玉時刻の優先順: 戦績の openedAt(約定履歴) → 経路の約定時刻(routeSnapshot.filledAt)
        # → 凍結プランの claim 時刻(送信直前)。どれも無ければチャートは決済前 90 分を描く。
        "entryAt": (_to_jst(row.get("openedAt"))
                    or (_to_jst(plan.get("filledAt")) if plan else None)
                    or (_to_jst(plan.get("claimedAt")) if plan else None)),
        "chart": None,
    }


def note_name(trade: Dict[str, Any]) -> str:
    """安定したファイル名: 日時 + 方向 + 枚数 + resultId 末尾(model は後で埋まるので名前に入れない)。"""
    stamp = trade["closedAt"].strftime("%Y-%m-%d %H%M")
    return f"{stamp} {trade['side']} {trade['qty']} {trade['resultId'][-6:]}.md"


def _yaml_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value) if isinstance(value, float) else str(value)
    text = str(value)
    if re.search(r"[:#\[\]{}&*!|>'\"%@`,]|^\s|\s$", text) or text == "":
        return json.dumps(text, ensure_ascii=False)
    return text


def _metric_rows(trade: Dict[str, Any]) -> List[Tuple[str, str]]:
    """自動ブロックの「計測」表。無い数字は出さない(推測しない)。"""
    m = trade.get("metrics") or {}
    rows: List[Tuple[str, str]] = []
    if m.get("hold_min") is not None:
        rows.append(("保有", f"{m['hold_min']:.0f} 分" + (f"({m['bars_held']} 本)" if m.get("bars_held") else "")))
    if m.get("mae_pt") is not None:
        mae = f"−{m['mae_pt']} pt"
        mfe = f"+{m['mfe_pt']} pt"
        if m.get("mae_r") is not None:
            mae += f"(−{m['mae_r']:.2f}R)"
            mfe += f"(+{m['mfe_r']:.2f}R)"
        rows.append(("MAE / MFE", f"{mae} / {mfe}"))
    if m.get("efficiency") is not None:
        rows.append(("効率(損益 ÷ MFE)", f"{m['efficiency']:.2f}"))
    kind = trade.get("exitKind")
    if kind:
        slip = trade.get("exitSlippage")
        rows.append(("決済の種類", kind + (f"(滑り {slip} pt)" if slip is not None else "")))
    if trade.get("plannedRTp1") is not None:
        rows.append(("計画 R(TP1 / TP2)", f"{trade['plannedRTp1']} / {trade.get('plannedRTp2') if trade.get('plannedRTp2') is not None else '—'}"))
    if m.get("post60_fav_pt") is not None:
        post = f"有利 +{m['post60_fav_pt']} pt / 不利 −{m['post60_adv_pt']} pt"
        if m.get("tp1_after_exit_min") is not None:
            post += f" · TP1 に {m['tp1_after_exit_min']:.0f} 分後到達"
        elif trade.get("tp1") is not None:
            post += " · TP1 未到達"
        rows.append(("決済後 60 分", post))
    if trade.get("reentryAfterLossMin") is not None:
        rows.append(("損切り後の再エントリー", f"前回損切りから {trade['reentryAfterLossMin']:.0f} 分 / 連敗 {trade.get('lossStreakBefore', 0)}"))
    elif trade.get("lossStreakBefore"):
        rows.append(("直前の連敗", str(trade["lossStreakBefore"])))
    if trade.get("sessionBucket"):
        rows.append(("時間帯", trade["sessionBucket"] + (f"({trade['entryAt'].strftime('%H:%M')} JST)" if trade.get("entryAt") else "")))
    ctx = trade.get("context") or {}
    if ctx.get("penalties"):
        rows.append(("減点", ", ".join(ctx["penalties"])))
    if ctx.get("vol_ratio") is not None:
        rows.append(("ボラ比 / noise", f"{ctx['vol_ratio']} / {ctx.get('noise_floor')} pt"))
    if ctx.get("htf"):
        rows.append(("HTF 構造", " · ".join(f"{k} {v}" for k, v in sorted(ctx["htf"].items()) if v)))
    if ctx.get("levels_rel"):
        rows.append(("建値 − 水準", ctx["levels_rel"]))
    return rows


REFLECTION_TEMPLATE = """## 振り返り

- **前提**(なぜこの形で入ったか):
- **執行**(計画との差: 枚数・建値・滑り・タイミング):
- **管理**(SL 移動・分割・裁量介入とその根拠):
- **結果の解釈**(勝ち負けと判断の良し悪しを分ける。数字は上の「計測」を使う):
- **教訓 / 次に同じ形が来たら**:
- **プロセス評価**: A / B / C / D(計画どおりに動けたか。損益は見ない)
- **ミスタグ**: なし(例: #mistake/再エントリー #mistake/枚数 #mistake/SL手動 #mistake/追いかけ)
"""


def render_note(trade: Dict[str, Any], manual_body: str = "") -> str:
    """frontmatter + 自動ブロック + (保持された)人のメモ。"""
    m = trade.get("metrics") or {}
    ctx = trade.get("context") or {}
    fm: List[Tuple[str, Any]] = [
        ("type", "trade"),
        ("result_id", trade["resultId"]),
        ("date", trade["date"]),
        ("closed_at", trade["closedAt"].strftime("%Y-%m-%dT%H:%M:%S+09:00")),
        ("entry_at", trade["entryAt"].strftime("%Y-%m-%dT%H:%M:%S+09:00") if trade.get("entryAt") else None),
        ("account", trade["account"]),
        ("mode", trade["mode"]),
        ("route", trade["route"]),
        ("model", trade["model"]),
        ("grade", trade["grade"]),
        ("scenario_id", trade["scenarioId"]),
        ("side", trade["side"]),
        ("qty", trade["qty"]),
        ("entry", trade["entry"]),
        ("exit", trade["exit"]),
        ("stop", trade["stop"]),
        ("tp1", trade["tp1"]),
        ("final_target", trade["finalTarget"]),
        ("entry_type", trade["entryType"]),
        ("ultra", trade["ultra"]),
        ("risk_dollars", trade["riskDollars"]),
        ("slippage_pt", trade["slippage"]),
        ("pnl_gross", trade["pnlGross"]),
        ("pnl_net", trade["pnlNet"]),
        ("fees", trade["fees"]),
        ("r_multiple", trade["rMultiple"]),
        ("outcome", trade["outcome"]),
        ("exit_kind", trade.get("exitKind")),
        ("exit_slippage_pt", trade.get("exitSlippage")),
        ("hold_min", m.get("hold_min")),
        ("mae_pt", m.get("mae_pt")),
        ("mfe_pt", m.get("mfe_pt")),
        ("mae_r", m.get("mae_r")),
        ("mfe_r", m.get("mfe_r")),
        ("efficiency", m.get("efficiency")),
        ("post60_fav_pt", m.get("post60_fav_pt")),
        ("post60_adv_pt", m.get("post60_adv_pt")),
        ("tp1_after_exit_min", m.get("tp1_after_exit_min")),
        ("loss_streak_before", trade.get("lossStreakBefore")),
        ("reentry_after_loss_min", trade.get("reentryAfterLossMin")),
        ("session_bucket", trade.get("sessionBucket")),
        ("entry_hour", trade["entryAt"].hour if trade.get("entryAt") else None),
        ("planned_r_tp1", trade.get("plannedRTp1")),
        ("planned_r_tp2", trade.get("plannedRTp2")),
        ("vol_ratio", ctx.get("vol_ratio")),
        ("htf_1h", (ctx.get("htf") or {}).get("1h")),
        ("htf_4h", (ctx.get("htf") or {}).get("4h")),
        ("htf_1d", (ctx.get("htf") or {}).get("1d")),
        ("levels_rel", ctx.get("levels_rel")),
        ("model_source", trade["modelSource"]),
    ]
    lines = ["---"]
    for key, value in fm:
        lines.append(f"{key}: {_yaml_scalar(value)}")
    # 人の評価キーは既存ノートの値をそのまま引き継ぐ(自動側は null で置く)
    human = trade.get("human") or {}
    for key in ("process_grade", "mistakes"):
        lines.append(f"{key}: {human.get(key, 'null')}")
    for key in HUMAN_KEYS:
        if key in human and key not in ("process_grade", "mistakes"):
            lines.append(f"{key}: {human[key]}")
    evidence = trade.get("evidence") or []
    lines.append("evidence: [" + ", ".join(_yaml_scalar(x) for x in evidence) + "]")
    lines.append("penalties: [" + ", ".join(_yaml_scalar(x) for x in (ctx.get("penalties") or [])) + "]")
    tags = ["trade", f"model/{trade['model']}", f"outcome/{trade['outcome'] or 'UNKNOWN'}",
            f"route/{trade['route']}"]
    if trade.get("grade"):
        tags.append("grade/" + str(trade["grade"]).replace("+", "plus"))
    if trade.get("exitKind"):
        tags.append("exit/" + str(trade["exitKind"]))
    if trade.get("reentryAfterLossMin") is not None and trade["reentryAfterLossMin"] <= 30:
        tags.append("flag/損切り後30分内の再エントリー")
    lines.append("tags: [" + ", ".join(_yaml_scalar(x) for x in tags) + "]")
    lines.append("---")
    lines.append("")
    pnl = trade["pnlNet"]
    pnl_text = f"{pnl:+,.0f}" if pnl is not None else "n/a"
    r_text = f"{trade['rMultiple']:+.2f}R" if trade["rMultiple"] is not None else "R n/a"
    lines.append(f"# {trade['date']} {trade['closedAt'].strftime('%H:%M')} {trade['side']} {trade['qty']} "
                 f"{trade['model']} — {pnl_text} USD ({r_text})")
    lines.append("")
    lines.append(f"- モデル / 等級: [[Models/{trade['model']}|{trade['model']}]] / {trade['grade'] or '—'}"
                 f"(model の出所: {trade['modelSource']})")
    lines.append(f"- 経路: {trade['route']}" + (f" · {trade['entryType']}" if trade['entryType'] else "")
                 + (" · ULTRA" if trade["ultra"] else ""))
    lines.append(f"- 建値 → 決済: {trade['entry']} → {trade['exit']}"
                 + (f" · SL {trade['stop']}" if trade["stop"] is not None else "")
                 + (f" · TP1 {trade['tp1']}" if trade["tp1"] is not None else "")
                 + (f" · 最終 {trade['finalTarget']}" if trade["finalTarget"] is not None else ""))
    if trade["slippage"] is not None:
        lines.append(f"- 成行の滑り: {trade['slippage']} pt")
    if evidence:
        lines.append("- 根拠タグ: " + " ".join(f"#{x}" for x in evidence))
    lines.append(f"- 記録: [[Journal/{trade['date']}]] · resultId `{trade['resultId']}`")
    metric_rows = _metric_rows(trade)
    if metric_rows:
        lines.append("")
        lines.append("## 計測(自動)")
        lines.append("")
        lines.append("| 指標 | 値 |")
        lines.append("| --- | --- |")
        for label, value in metric_rows:
            lines.append(f"| {label} | {value} |")
    if trade.get("chart"):
        lines.append("")
        lines.append(f"![[{trade['chart']}]]")
        lines.append("")
        lines.append("> チャートはシステムが取得した確定 3 分足と VP 水準からの復元(TradingView の画面そのものではない)。")
    lines.append("")
    lines.append(AUTO_END)
    lines.append("")
    if manual_body.strip():
        lines.append(manual_body.rstrip("\n"))
    else:
        lines.append(REFLECTION_TEMPLATE.rstrip("\n"))
    lines.append("")
    return "\n".join(lines)


def split_manual(existing: str) -> str:
    """既存ノートの `AUTO_END` より下(人のメモ)を返す。無ければ空。"""
    if AUTO_END not in existing:
        return ""
    return existing.split(AUTO_END, 1)[1].lstrip("\n")


#: 人が frontmatter に書く評価キー。再生成しても値を引き継ぐ(自動側は上書きしない)。
HUMAN_KEYS = ("process_grade", "mistakes", "setup_quality", "reviewed")


def preserved_frontmatter(existing: str) -> Dict[str, str]:
    """既存ノートの frontmatter から HUMAN_KEYS の生の値(文字列)を拾う。"""
    out: Dict[str, str] = {}
    if not existing.startswith("---"):
        return out
    end = existing.find("\n---", 3)
    if end < 0:
        return out
    for line in existing[3:end].splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key, value = key.strip(), value.strip()
        if key in HUMAN_KEYS and value not in ("", "null"):
            out[key] = value
    return out


MODEL_TEMPLATE = """---
type: model
model: {model}
tags: [model]
---

# {model}

## 仮説(この形が効く理由)

-

## 観察ログ

| 日付 | 所見 | 参照 |
| --- | --- | --- |
|  |  |  |

## 実測(Bases)

![[Bases/Trades.base#{model}]]

> PF ≥ 1.5 / N ≥ 200 / OOS PF ≥ 1.3(BLUEPRINT §0)を満たすまで「効く」と宣言しない。
"""


# ---------------------------------------------------------------- 書き出し

CHART_DIR = os.path.join("attachments", "trades")
SESSION_DIR = os.path.join("attachments", "sessions")
AUTO_JOURNAL_DIR = os.path.join("Journal", "auto")


def _enrich(trade: Dict[str, Any], loaded: Tuple[List[Dict[str, float]], List[Dict[str, Any]], str],
            bundles: List[Tuple[datetime, str]]) -> None:
    """計測・決済種別・時間帯・武装文脈をトレードへ書き込む(in place)。"""
    bars = loaded[0]
    trade["metrics"] = obsidian_metrics.excursions(trade, bars, trade.get("entryAt"), trade["closedAt"])
    kind, slip = obsidian_metrics.exit_kind(trade)
    trade["exitKind"], trade["exitSlippage"] = kind, slip
    trade["sessionBucket"] = obsidian_metrics.session_bucket(trade.get("entryAt") or trade["closedAt"])
    ctx = obsidian_metrics.entry_context(trade.get("entryAt"), trade, bundles)
    trade["context"] = ctx
    trade["plannedRTp1"] = ctx.get("planned_r_tp1")
    trade["plannedRTp2"] = ctx.get("planned_r_tp2")


def render_session_note(date: str, trades: List[Dict[str, Any]], equity_rel: Optional[str]) -> str:
    stats = obsidian_metrics.session_stats(trades)
    win_rate_text = f"{stats['win_rate'] * 100:.0f}%" if stats["win_rate"] is not None else "—"
    lines = ["---", "type: session-auto", f"date: {date}", f"trades: {stats['n']}",
             f"net: {_yaml_scalar(stats['net'])}", f"win_rate: {_yaml_scalar(stats['win_rate'])}",
             f"pf: {_yaml_scalar(stats['pf'])}", f"expectancy: {_yaml_scalar(stats['expectancy'])}",
             f"max_dd: {_yaml_scalar(stats['max_dd'])}", f"reentries_after_loss: {stats['reentries']}",
             "tags: [session-auto]", "---", "",
             f"# {date} 自動集計", "",
             "> `python obsidian_export.py` が毎回描き直す。手で編集しない(Journal 本文に書く)。", "",
             "| 指標 | 値 |", "| --- | --- |",
             f"| 取引 | {stats['n']}(勝 {stats['wins']} / 負 {stats['losses']} / 引分 {stats['flats']}) |",
             f"| 勝率 | {win_rate_text} |",
             f"| 純損益 | {stats['net']:+,.0f} USD |",
             f"| PF | {stats['pf'] if stats['pf'] is not None else '—'}(総利益 {stats['gross_win']:,.0f} / 総損失 {stats['gross_loss']:,.0f}) |",
             f"| 期待値 / 取引 | {stats['expectancy']:+,.0f} USD |" if stats['expectancy'] is not None else "| 期待値 / 取引 | — |",
             f"| 平均勝ち / 平均負け | {stats['avg_win'] if stats['avg_win'] is not None else '—'} / {stats['avg_loss'] if stats['avg_loss'] is not None else '—'} |",
             f"| 平均 R | {stats['avg_r'] if stats['avg_r'] is not None else '—'} |",
             f"| 最大ドローダウン(決済ベース) | −{stats['max_dd']:,.0f} USD |",
             f"| 最大 / 最小 | {stats['best']:+,.0f} / {stats['worst']:+,.0f} |" if stats['best'] is not None else "| 最大 / 最小 | — |",
             f"| 損切り後 30 分内の再エントリー | {stats['reentries']} 件 |",
             ""]
    if equity_rel:
        lines += [f"![[{equity_rel}]]", ""]
    lines += ["## モデル別", "", "| モデル | N | 勝 / 負 | 純損益 |", "| --- | --- | --- | --- |"]
    for model, b in sorted(stats["by_model"].items(), key=lambda kv: -kv[1]["net"]):
        lines.append(f"| [[Models/{model}\\|{model}]] | {b['n']} | {b['wins']} / {b['losses']} | {b['net']:+,.0f} |")
    lines += ["", "## トレード", "", "| 時刻 | ノート | モデル | 方向・枚数 | 決済 | 損益 | MAE / MFE | 備考 |",
              "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for tr in sorted(trades, key=lambda x: x["closedAt"]):
        m = tr.get("metrics") or {}
        stem = os.path.splitext(note_name(tr))[0]
        mae_mfe = (f"−{m['mae_pt']} / +{m['mfe_pt']}" if m.get("mae_pt") is not None else "—")
        flags = []
        if tr.get("reentryAfterLossMin") is not None and tr["reentryAfterLossMin"] <= 30:
            flags.append(f"損切り後 {tr['reentryAfterLossMin']:.0f} 分で再エントリー")
        if m.get("tp1_after_exit_min") is not None and tr.get("exitKind") == "SL":
            flags.append(f"SL 後 {m['tp1_after_exit_min']:.0f} 分で TP1 到達")
        pnl = tr.get("pnlNet")
        lines.append(f"| {(tr.get('entryAt') or tr['closedAt']).strftime('%H:%M')}→{tr['closedAt'].strftime('%H:%M')} "
                     f"| [[Trades/{stem}\\|{stem[-6:]}]] | {tr['model']} {tr.get('grade') or ''} | {tr['side']} {tr['qty']} "
                     f"| {tr.get('exitKind') or '—'} | {pnl:+,.0f} | {mae_mfe} | {' · '.join(flags)} |")
    lines.append("")
    return "\n".join(lines)


def export(vault: str = DEFAULT_VAULT, scorecard_path: str = SCORECARD_FILE,
           ledger_path: str = LEDGER_FILE, audit_pattern: str = AUDIT_GLOB,
           dry_run: bool = False, charts: bool = True, force_charts: bool = False,
           raw_bars_path: str = obsidian_charts.RAW_BARS) -> Dict[str, Any]:
    plans = plan_index(ledger_path)
    audit = audit_model_index(audit_pattern)
    bundles = obsidian_charts.audit_bundles(audit_pattern)
    trades_dir = os.path.join(vault, "Trades")
    models_dir = os.path.join(vault, "Models")
    created, updated, unchanged, skipped, charted, sessions = [], [], [], [], [], []
    seen_models = set()
    trades: List[Dict[str, Any]] = []
    # R54: 戦績は「追記のまま最後の 1 行を採る」台帳。訂正 publish で同じ resultId が
    # 2 行になるので、素で読むと二重計上する(2026-09-07 の自動集計が 3 件 $1,498.50 →
    # 6 件 $3,024.00 になった)。重複排除の規則は model_scorecard 側の 1 箇所に置く。
    for row in model_scorecard.load_rows(scorecard_path):
        trade = build_trade(row, plans, audit)
        if trade is None:
            skipped.append(str(row.get("resultId") or "?"))
            continue
        loaded = obsidian_charts.bars_and_levels(
            trade.get("entryAt"), trade["closedAt"], raw_path=raw_bars_path,
            audit_pattern=audit_pattern, bundles=bundles, after_min=obsidian_metrics.POST_EXIT_MIN)
        _enrich(trade, loaded, bundles)
        trade["_loaded"] = loaded
        trades.append(trade)
    obsidian_metrics.sequence_context(trades)
    for trade in trades:
        trade["lossStreakBefore"] = trade.get("loss_streak_before")
        trade["reentryAfterLossMin"] = trade.get("reentry_after_loss_min")
        seen_models.add(trade["model"])
        if charts:
            # チャート復元: 1 トレード 1 SVG。既存は force でなければ触らない。
            stem = os.path.splitext(note_name(trade))[0]
            rel = f"{CHART_DIR}/{stem}.svg".replace("\\", "/")
            chart_path = os.path.join(vault, CHART_DIR, f"{stem}.svg")
            if not dry_run:
                source = obsidian_charts.render_trade_chart(
                    trade, trade.get("entryAt"), trade["closedAt"], chart_path,
                    raw_path=raw_bars_path, audit_pattern=audit_pattern, force=force_charts,
                    bundles=bundles, loaded=trade["_loaded"])
                if source:
                    charted.append(f"{stem}.svg ({source})")
            if os.path.exists(chart_path) or dry_run:
                trade["chart"] = rel
        path = os.path.join(trades_dir, note_name(trade))
        existing = ""
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                existing = fh.read()
        trade["human"] = preserved_frontmatter(existing)
        content = render_note(trade, split_manual(existing))
        if existing == content:
            unchanged.append(os.path.basename(path))
            continue
        (updated if existing else created).append(os.path.basename(path))
        if not dry_run:
            os.makedirs(trades_dir, exist_ok=True)
            with open(path, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(content)
    # セッション自動ノート(日付ごと): 統計 + 累積損益 + トレード一覧。毎回描き直す。
    by_date: Dict[str, List[Dict[str, Any]]] = {}
    for trade in trades:
        by_date.setdefault(trade["date"], []).append(trade)
    for date, day_trades in sorted(by_date.items()):
        stats = obsidian_metrics.session_stats(day_trades)
        equity_rel = f"{SESSION_DIR}/{date} equity.svg".replace("\\", "/")
        note = render_session_note(date, day_trades, equity_rel)
        note_path = os.path.join(vault, AUTO_JOURNAL_DIR, f"{date}.md")
        equity_path = os.path.join(vault, SESSION_DIR, f"{date} equity.svg")
        old = ""
        if os.path.exists(note_path):
            with open(note_path, encoding="utf-8") as fh:
                old = fh.read()
        if old != note:
            sessions.append(f"{date}.md")
            if not dry_run:
                os.makedirs(os.path.dirname(note_path), exist_ok=True)
                os.makedirs(os.path.dirname(equity_path), exist_ok=True)
                with open(note_path, "w", encoding="utf-8", newline="\n") as fh:
                    fh.write(note)
                with open(equity_path, "w", encoding="utf-8", newline="\n") as fh:
                    fh.write(obsidian_metrics.equity_svg(stats["curve"]))
    model_notes = []
    for model in sorted(set(MODELS) | seen_models):
        path = os.path.join(models_dir, f"{model}.md")
        if os.path.exists(path):
            continue
        model_notes.append(f"{model}.md")
        if not dry_run:
            os.makedirs(models_dir, exist_ok=True)
            with open(path, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(MODEL_TEMPLATE.format(model=model))
    return {"vault": vault, "created": created, "updated": updated, "unchanged": unchanged,
            "skipped": skipped, "modelNotes": model_notes, "charts": charted,
            "sessions": sessions, "dryRun": dry_run}


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Obsidian 保管庫へトレードノートを書き出す")
    parser.add_argument("--vault", default=DEFAULT_VAULT)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-charts", action="store_true", help="チャート SVG を作らない")
    parser.add_argument("--force-charts", action="store_true", help="既存のチャート SVG も描き直す")
    args = parser.parse_args(argv)
    if not os.path.isdir(args.vault):
        print(f"vault not found: {args.vault}")
        return 1
    summary = export(args.vault, dry_run=args.dry_run, charts=not args.no_charts,
                     force_charts=args.force_charts)
    prefix = "[dry-run] " if args.dry_run else ""
    print(f"{prefix}vault={summary['vault']}")
    print(f"{prefix}created={len(summary['created'])} updated={len(summary['updated'])} "
          f"unchanged={len(summary['unchanged'])} skipped={len(summary['skipped'])} "
          f"modelNotes={len(summary['modelNotes'])} charts={len(summary['charts'])} "
          f"sessions={len(summary['sessions'])}")
    for name in summary["created"]:
        print(f"  + {name}")
    for name in summary["updated"]:
        print(f"  ~ {name}")
    for name in summary["modelNotes"]:
        print(f"  + Models/{name}")
    for name in summary["charts"]:
        print(f"  # {name}")
    for name in summary["sessions"]:
        print(f"  = Journal/auto/{name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

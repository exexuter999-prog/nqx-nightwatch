#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""決済 result に載せる「根拠チャート」の文脈(R57)。

Mini App のリザルト画面が、建玉前〜決済後の確定 3 分足・VP 水準・凍結ターゲット・
根拠タグ・減点・HTF 構造をそのまま描けるように、result へ `chart` を付ける。

  chart = {
    "version": "NQX-RESULT-CHART/1",
    "tf": 180,
    "bars": [[t, o, h, l, c], ...],     # 建玉 45 分前〜決済 15 分後の確定 3 分足(最大 MAX_BARS)
    "levels": [{"label": "C: VAH", "price": 29566.7}, ...],
    "tp1": 29535.0, "tp2": 29350.75,    # 凍結プランのターゲット(無ければ null)
    "evidence": [...], "penalties": [...],
    "htf": {"1h": "UP", ...}, "volRatio": 0.18, "noise": 11.0, "session": "NY PM",
    "source": "raw" | "audit:<file>" | "none",
  }

足は **システムが取得した実データだけ**(`.secrets/tv_raw/bars3m.json`、無ければ監査バンドル)。
合成しない。無ければ `bars` は空で、画面は従来の path 描画に落ちる。
Worker(validateResult)は同じ形を検証し、上限を超えたものは拒否する。
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

import obsidian_charts  # noqa: E402
import obsidian_metrics  # noqa: E402

VERSION = "NQX-RESULT-CHART/1"
MAX_BARS = 80
MAX_LEVELS = 12
MAX_TAGS = 16
LEVEL_KEYS = ("C: VAH", "C: VAL", "C: POC", "P: VAH", "P: VAL", "P: POC")


def _parse(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _num(value: Any) -> Optional[float]:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out else None


#: 監査バンドルの一覧(923 ファイルの glob)はプロセス内で 60 秒だけ使い回す。
_BUNDLE_CACHE: Dict[str, Tuple[float, List[Tuple[datetime, str]]]] = {}
_BUNDLE_TTL_SEC = 60.0


def _bundles_cached(pattern: str) -> List[Tuple[datetime, str]]:
    import time
    now = time.time()
    hit = _BUNDLE_CACHE.get(pattern)
    if hit and now - hit[0] < _BUNDLE_TTL_SEC:
        return hit[1]
    bundles = obsidian_charts.audit_bundles(pattern)
    _BUNDLE_CACHE[pattern] = (now, bundles)
    return bundles


def _levels_of_bundle(basename: Optional[str], bundles: List[Tuple[datetime, str]]) -> List[Dict[str, Any]]:
    """監査バンドル(basename)の snapshot.levels。無ければ空。"""
    if not basename:
        return []
    path = next((p for _, p in bundles if os.path.basename(p) == basename), None)
    if not path:
        return []
    try:
        import json
        with open(path, encoding="utf-8") as fh:
            bundle = json.load(fh)
    except (OSError, ValueError):
        return []
    return [l for l in ((bundle.get("snapshot") or {}).get("levels") or []) if isinstance(l, dict)]


def _trim_bars(bars: List[Dict[str, float]], opened: datetime, closed: datetime) -> List[Dict[str, float]]:
    """上限を超えたら、建玉前と決済後から均等に削る(保有中は残す)。"""
    if len(bars) <= MAX_BARS:
        return bars
    o, c = opened.timestamp(), closed.timestamp()
    pre = [b for b in bars if b["t"] + obsidian_charts.BAR_SEC <= o]
    held = [b for b in bars if b["t"] + obsidian_charts.BAR_SEC > o and b["t"] < c]
    post = [b for b in bars if b["t"] >= c]
    held = held[-MAX_BARS:]
    room = MAX_BARS - len(held)
    keep_pre = min(len(pre), max(0, (room * 3) // 4))
    keep_post = min(len(post), room - keep_pre)
    keep_pre = min(len(pre), room - keep_post)
    return pre[len(pre) - keep_pre:] + held + post[:keep_post]


def build_chart(side: str, entry: Any, exit_price: Any, opened_at: Any, closed_at: Any,
                plan: Optional[Dict[str, Any]] = None, *,
                raw_bars_path: str = obsidian_charts.RAW_BARS,
                audit_pattern: str = obsidian_charts.AUDIT_GLOB,
                bundles: Optional[List[Tuple[datetime, str]]] = None) -> Optional[Dict[str, Any]]:
    """result.chart を組む。時刻が読めなければ None。足が無くても文脈だけは返す。"""
    opened, closed = _parse(opened_at), _parse(closed_at)
    if opened is None or closed is None or closed < opened:
        return None
    plan = plan if isinstance(plan, dict) else {}
    if bundles is None:
        bundles = _bundles_cached(audit_pattern)
    bars, levels, source = obsidian_charts.bars_and_levels(
        opened, closed, raw_path=raw_bars_path, audit_pattern=audit_pattern,
        bundles=bundles, after_min=obsidian_charts.AFTER_MIN)
    bars = _trim_bars(bars, opened, closed)
    trade = {"side": str(side or "").upper(), "entry": _num(entry)}
    ctx = obsidian_metrics.entry_context(opened, trade, bundles)
    # 水準は **建玉直前の周期**(その形を根拠にした時点)のものを優先し、無ければ決済側の周期。
    entry_levels = _levels_of_bundle(ctx.get("context_bundle"), bundles)
    picked_levels = []
    for level in (entry_levels or levels):
        label = str(level.get("label") or "")
        price = _num(level.get("price"))
        if label in LEVEL_KEYS and price is not None:
            picked_levels.append({"label": label, "price": round(price, 2)})
    evidence = [str(x)[:32] for x in (plan.get("decisionEvidence") or ctx.get("evidence") or [])][:MAX_TAGS]
    penalties = [str(x)[:32] for x in (ctx.get("penalties") or [])][:MAX_TAGS]
    tp1 = _num(plan.get("tp1"))
    tp2 = _num(plan.get("finalTarget"))
    return {
        "version": VERSION,
        "tf": obsidian_charts.BAR_SEC,
        "bars": [[int(b["t"]), round(b["o"], 2), round(b["h"], 2), round(b["l"], 2), round(b["c"], 2)]
                 for b in bars],
        "levels": picked_levels[:MAX_LEVELS],
        "tp1": tp1, "tp2": tp2,
        "evidence": evidence,
        "penalties": penalties,
        "htf": {str(k)[:8]: str(v)[:12] for k, v in (ctx.get("htf") or {}).items() if v},
        "volRatio": _num(ctx.get("vol_ratio")),
        "noise": _num(ctx.get("noise_floor")),
        "session": obsidian_metrics.session_bucket(opened.astimezone(obsidian_charts.JST)),
        "source": source,
    }


def attach(result: Dict[str, Any], plan: Optional[Dict[str, Any]] = None, **kwargs: Any) -> Dict[str, Any]:
    """result(nqx_state.build_result の戻り)に chart を付けて返す。失敗しても result は壊さない。"""
    if not isinstance(result, dict):
        return result
    try:
        chart = build_chart(result.get("side"), result.get("entry"), result.get("exit"),
                            result.get("openedAt"), result.get("closedAt"), plan, **kwargs)
    except Exception:  # noqa: BLE001 - 文脈は付加情報。決済記録を止めない
        chart = None
    if chart is not None:
        result["chart"] = chart
    return result

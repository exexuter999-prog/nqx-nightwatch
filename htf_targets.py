# -*- coding: utf-8 -*-
"""R124: 上位足の水準で**利確目標の不足だけ**を埋める(2026-09-21 ユーザー決定で LIVE)。

水準マップはセッション系(POC/VAH/VAL・Asia/London 高安・6pm/週足始値)だけで、
高値更新の局面では上に目標が 1 本も無く、候補が `TARGET_HEADROOM_INSUFFICIENT` で
WATCH に落ちていた(2026-09-21 16:31 の BREAKER BUY A+)。

`msnr_gate.model_targets` が**目標 2 本を揃えられないときだけ**、ここの水準を追加の目標
候補として足して引き直す。揃っている候補の目標には一切触れない —— 常に足すと runner が
遠い上位足へ動き、再生で悪化した(既存 18 件で −2.25R。docs/R124_HTF_TARGET_FILL.md)。
水準マップ(セットアップ検出・SL の流動性逃がし)には入れないので、変わるのは目標と、
それに連動する採点・`TARGET_HEADROOM_INSUFFICIENT` だけ。Entry / SL / 枚数は変えない。

水準は**その時点で確定している**上位足だけから作る(形成中の足は使わない):

  PREV_DAY     直近の確定日足の高安
  PREV_WEEK    前週(確定日足を週で集計)の高安
  DAILY_SWING  未回収の日足スイング高安(3 本フラクタル。以後の確定足と現値が越えていない)
  RANGE20_1H   1h の 20 本レンジ高安(htfContext。valid な足だけ)
  RANGE20_4H   4h の同上
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

CONTRACT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.environ.get("NQX_EXECUTION_CONTRACT", "execution_contract.json"))  # 🩹 Nerf Edition: NQX_EXECUTION_CONTRACT で差し替え可
MODES = ("OFF", "LIVE")
SOURCES = ("PREV_DAY", "PREV_WEEK", "DAILY_SWING", "RANGE20_1H", "RANGE20_4H")
#: 目標ラベルの接頭辞。level_tier は正規表現の search なので、階層(Weekly=1 / Prev Day=2)は
#: 接頭辞があっても変わらない。監査で「上位足で埋めた目標」を見分けるための印。
LABEL_PREFIX = "HTF "
DAY = 86400


def policy(contract: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """``htfTargets`` を ``{"mode", "sources", "invalid"}`` にする。

    節が無い・読めない・壊れているときは OFF(= R124 以前と同じ判定)。
    """
    off = {"mode": "OFF", "sources": [], "invalid": []}
    if contract is None:
        try:
            with open(CONTRACT_PATH, encoding="utf-8") as fh:
                contract = json.load(fh)
        except (OSError, ValueError):
            return {**off, "invalid": ["CONTRACT_UNREADABLE"]}
    raw = contract.get("htfTargets") if isinstance(contract, dict) else None
    if raw is None:
        return off
    if not isinstance(raw, dict):
        return {**off, "invalid": ["HTF_TARGETS_MALFORMED"]}
    mode = str(raw.get("mode") or "OFF").strip().upper()
    if mode not in MODES:
        return {**off, "invalid": ["HTF_TARGETS_MODE_MALFORMED"]}
    sources = raw.get("sources", list(SOURCES))
    if not isinstance(sources, list) or not sources:
        return {**off, "invalid": ["HTF_TARGETS_SOURCES_MALFORMED"]}
    unknown = [s for s in sources if s not in SOURCES]
    if unknown:
        return {**off, "invalid": [f"HTF_TARGETS_SOURCE_UNKNOWN:{s}" for s in unknown]}
    return {"mode": mode, "sources": [s for s in SOURCES if s in sources], "invalid": []}


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


def _week(t: float) -> Tuple[int, int]:
    # 日足の t は前日 22:00Z(ET 18:00)開始。+2h で取引日の暦日に寄せてから週を取る。
    d = datetime.fromtimestamp(t + 7200, timezone.utc).isocalendar()
    return (d[0], d[1])


def levels(bundle: Dict[str, Any], sources: Optional[List[str]] = None) -> List[Tuple[float, str]]:
    """``model_targets(extra=…)`` へ渡す ``(price, label)`` の列。材料が無ければ空。"""
    wanted = set(sources if sources is not None else SOURCES)
    if not isinstance(bundle, dict):
        return []
    snap = bundle.get("snapshot") if isinstance(bundle.get("snapshot"), dict) else {}
    now = _ts(bundle.get("at"))
    price = _f(bundle.get("price"))
    if now is None:
        return []
    days = [b for b in (snap.get("bars1d") or [])
            if isinstance(b, dict) and _f(b.get("t")) is not None
            and _f(b.get("h")) is not None and _f(b.get("l")) is not None
            and float(b["t"]) + DAY <= now]
    days.sort(key=lambda b: float(b["t"]))
    out: List[Tuple[float, str]] = []
    if days and "PREV_DAY" in wanted:
        out += [(float(days[-1]["h"]), "Prev Day High"), (float(days[-1]["l"]), "Prev Day Low")]
    if days and "PREV_WEEK" in wanted:
        this_week = _week(now - 3600)
        prior = [b for b in days if _week(float(b["t"])) != this_week]
        if prior:
            last = _week(float(prior[-1]["t"]))
            week = [b for b in prior if _week(float(b["t"])) == last]
            out += [(max(float(b["h"]) for b in week), "Weekly High"),
                    (min(float(b["l"]) for b in week), "Weekly Low")]
    if "DAILY_SWING" in wanted:
        for i in range(1, len(days) - 1):
            h, l = float(days[i]["h"]), float(days[i]["l"])
            later = days[i + 1:]
            if (h > float(days[i - 1]["h"]) and h > float(days[i + 1]["h"])
                    and all(float(b["h"]) <= h for b in later) and (price is None or price < h)):
                out.append((h, "Daily Swing High"))
            if (l < float(days[i - 1]["l"]) and l < float(days[i + 1]["l"])
                    and all(float(b["l"]) >= l for b in later) and (price is None or price > l)):
                out.append((l, "Daily Swing Low"))
    frames = ((snap.get("htfContext") or {}).get("frames") or {}) if isinstance(snap, dict) else {}
    for tf, source in (("1h", "RANGE20_1H"), ("4h", "RANGE20_4H")):
        frame = frames.get(tf) if isinstance(frames, dict) else None
        if source in wanted and isinstance(frame, dict) and frame.get("valid") is True:
            for key, name in (("range20High", "Range High"), ("range20Low", "Range Low")):
                value = _f(frame.get(key))
                if value is not None:
                    out.append((value, f"{tf.upper()} {name}"))
    return [(value, LABEL_PREFIX + label) for value, label in out]


def fill_for(bundle: Dict[str, Any], contract: Optional[Dict[str, Any]] = None) -> List[Tuple[float, str]]:
    """LIVE のときだけ穴埋め用の水準を返す。OFF / 不正な節では空(= R124 以前)。"""
    rule = policy(contract)
    if rule["mode"] != "LIVE":
        return []
    return levels(bundle, rule["sources"])

# -*- coding: utf-8 -*-
"""R86: 押し目を深く取り、SL にも同じだけ余裕を持たせる(ブラケットの平行移動)。

なぜ要るか
----------
2026-09-08 19:54 JST の VP80_REVERSION A+ BUY は SL 29,533.50 に対して安値 29,534.00
(**0.5pt 残し**)から TP1 へ届いた。逆に 09-09 07:06 と 09-11 09:04 の VP80 BUY は SL を
0.75〜1.0pt だけ抜かれて損切りになり、その後 108 / 144 分で TP1 に届いている。
「SL ぎりぎりを祈る」形をやめて、**入る場所そのものを深くし、SL も同じ幅だけ外へ
逃がす**。建値と SL を同じ幅だけ動かすので **SL 幅(= リスク額・枚数)は変わらない**。
変わるのは (1) 約定したトレードの SL までの余白と建値の良さ、(2) 押しが浅く約定しない
取り逃し、の 2 つだけである。

実測(docs/R86_ENTRY_DEPTH_AND_STOP_ROOM.md。監査バンドルの公開シナリオ 221 件、
確定 3 分足、1tick 突き抜けで約定、同じ足の SL/TP は SL 優先)
---------------------------------------------------------------------------------
- **SL だけ広げる**(建値そのまま)はどの切り方でも期待値が下がるか横ばい。
  広げた分だけ枚数が減り、救える損切りは少ない(損切りの 70% は SL を 1N 以上抜ける)。
- **VP80_REVERSION**(建値 = 直近の確定足終値。構造に錨が無い)は平行移動で改善:
  0.25N で +0.12〜+0.17R/件(ブートストラップ P(改善) 0.88〜1.00)。
  勝ちトレードの 82% は 0.25N 以上押してから伸びている。
- **BREAKER_CONTINUATION / TURTLE_SOUP_REVERSAL**(建値 = レベルそのもの)は深くすると
  悪化(0.5N で −0.05〜−0.08R/件)。レベルより深い約定は「レベルが崩れた時だけ約定する」
  逆選択になる。**深くしない。**
- OTE_FVG_PULLBACK は建値が既に 0.705 押し。件数不足で据え置き。

だから既定のルールは「VP80 だけ 0.25N 平行移動」1 本で、残りは触らない。

モード(``execution_contract.json`` の ``entryDepth.mode``)
----------------------------------------------------------
- ``OFF``    — 何もしない(バンドルにも書かない)。
- ``SHADOW`` — 発注幾何は変えず、動かした場合の建値/SL をバンドル ``entryDepth`` に記録する。
- ``LIVE``   — 公開シナリオの entry / stop を平行移動した値へ置き換える。
  判定(model / grade / state / decisionId)は **元の幾何のまま**。等級や武装可否を
  この層で動かさない(TP までの R が伸びたことで等級が上がる、を起こさないため)。
  ``evaluation.decision.evidence`` に記録専用タグ ``ENTRY_DEPTH_025N`` を足し、凍結プラン
  → スコアカードへ流す(R48 と同じ「N が貯まるまで重みにしない」タグ)。

契約が読めない・形が壊れている・値が変なときは **OFF に倒す**(何も変えない側が安全)。

    python entry_depth.py --report           # 公開シナリオ全件で「今のまま」vs「深くした場合」(読むだけ)
    python entry_depth.py --report --rest 60 # 指値の待ち時間を変えて感度を見る
    python entry_depth.py --trades           # 実トレードごとの SL 余白と、平行移動した場合の結果
"""
from __future__ import annotations

import argparse
import bisect
import glob
import io
import json
import math
import os
import random
import statistics
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

BASE = os.path.dirname(os.path.abspath(__file__))
CONTRACT_PATH = os.path.join(BASE, "execution_contract.json")
SECRETS = os.path.join(BASE, ".secrets")
VERSION = "R86-ENTRY-DEPTH-1"
TICK = 0.25
MODES = ("OFF", "SHADOW", "LIVE")
#: 契約に節が無い / 壊れているときの方針。何も動かさない。
DEFAULT_POLICY: Dict[str, Any] = {"version": VERSION, "mode": "OFF", "rules": [],
                                  "maxDepthPt": 10.0, "stopShift": "SAME_AS_DEPTH"}
JST = timezone(timedelta(hours=9))


# ---------------------------------------------------------------- 方針

def load_policy(contract: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """``execution_contract.json`` の ``entryDepth`` を検証して返す。不正は OFF。"""
    if contract is None:
        try:
            with io.open(CONTRACT_PATH, encoding="utf-8") as fh:
                contract = json.load(fh)
        except (OSError, ValueError):
            return dict(DEFAULT_POLICY)
    raw = (contract or {}).get("entryDepth")
    if not isinstance(raw, dict):
        return dict(DEFAULT_POLICY)
    mode = str(raw.get("mode") or "OFF").upper()
    if mode not in MODES:
        return dict(DEFAULT_POLICY)
    # 平行移動以外(SL だけ広げる等)は実測で負けたので受け付けない。
    if str(raw.get("stopShift") or "SAME_AS_DEPTH").upper() != "SAME_AS_DEPTH":
        return dict(DEFAULT_POLICY)
    max_depth = _finite(raw.get("maxDepthPt"))
    if max_depth is None or max_depth < TICK:
        return dict(DEFAULT_POLICY)
    rules = []
    for rule in raw.get("rules") or []:
        if not isinstance(rule, dict):
            return dict(DEFAULT_POLICY)
        depth_n = _finite(rule.get("depthN"))
        models = rule.get("models")
        if (depth_n is None or not 0 < depth_n <= 1.0 or not isinstance(models, list)
                or not models or not all(isinstance(m, str) and m for m in models)):
            return dict(DEFAULT_POLICY)
        rules.append({"case": str(rule.get("case") or "_".join(models)), "models": list(models),
                      "depthN": depth_n})
    return {"version": str(raw.get("version") or VERSION), "mode": mode, "rules": rules,
            "maxDepthPt": max_depth, "stopShift": "SAME_AS_DEPTH"}


def _finite(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _on_tick(value: float) -> bool:
    return abs(value * 4 - round(value * 4)) <= 1e-8


def _tick_half_up(value: float) -> float:
    return round(math.floor(value / TICK + 0.5) * TICK, 2)


# ---------------------------------------------------------------- 純粋計算

def plan_shift(scenario: Dict[str, Any], noise: Any,
               policy: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """平行移動の計画を返す。シナリオは変えない(純粋関数)。

    戻り値の ``eligible`` が True のときだけ ``shifted`` が意味を持つ。対象外は
    ``reason`` に理由を残す(「評価して動かさなかった」と「入力が無い」を区別する)。
    """
    policy = policy or DEFAULT_POLICY
    model = str((scenario or {}).get("model") or "")
    audit: Dict[str, Any] = {"version": policy.get("version", VERSION), "mode": policy.get("mode", "OFF"),
                             "model": model, "eligible": False, "case": None, "reason": None}
    rule = next((r for r in policy.get("rules") or [] if model in r["models"]), None)
    if rule is None:
        audit["reason"] = "MODEL_NOT_IN_POLICY"
        return audit
    audit["case"] = rule["case"]
    side = str(scenario.get("side") or "").upper()
    entry = _finite(scenario.get("entry"))
    stop = _finite(scenario.get("stop"))
    nf = _finite(noise)
    if side not in {"BUY", "SELL"} or entry is None or stop is None:
        audit["reason"] = "GEOMETRY_MISSING"
        return audit
    if not (_on_tick(entry) and _on_tick(stop)):
        audit["reason"] = "GEOMETRY_OFF_TICK"
        return audit
    sgn = 1.0 if side == "BUY" else -1.0
    risk = (entry - stop) * sgn
    if risk <= 0:
        audit["reason"] = "STOP_ON_WRONG_SIDE"
        return audit
    if nf is None or nf <= 0:
        audit["reason"] = "NOISE_FLOOR_MISSING"
        return audit
    depth = min(_tick_half_up(rule["depthN"] * nf), float(policy.get("maxDepthPt") or 0))
    depth = round(math.floor(depth / TICK + 1e-9) * TICK, 2)     # 上限で切った値も tick へ
    if depth < TICK:
        audit["reason"] = "DEPTH_BELOW_TICK"
        return audit
    new_entry = round(entry - sgn * depth, 2)
    new_stop = round(stop - sgn * depth, 2)
    targets = [_finite(t) for t in (scenario.get("targets") or [])]
    if any(t is None for t in targets) or any((t - new_entry) * sgn <= 0 for t in targets):
        audit["reason"] = "TARGET_NOT_BEYOND_SHIFTED_ENTRY"
        return audit
    audit.update({
        "eligible": True, "reason": None, "side": side,
        "depthN": rule["depthN"], "depthPt": depth, "noise": round(nf, 2),
        "riskPt": round(risk, 2), "riskN": round(risk / nf, 2),
        "original": {"entry": entry, "stop": stop},
        "shifted": {"entry": new_entry, "stop": new_stop},
        "targetR": [round((t - new_entry) * sgn / risk, 2) for t in targets],
    })
    return audit


def apply_live(scenario: Dict[str, Any], audit: Dict[str, Any]) -> Dict[str, Any]:
    """LIVE で対象なら entry / stop / targetR だけを置き換えた**写し**を返す。"""
    if not (audit.get("eligible") and audit.get("mode") == "LIVE"):
        return scenario
    shifted = dict(scenario)
    shifted["entry"] = audit["shifted"]["entry"]
    shifted["stop"] = audit["shifted"]["stop"]
    if audit.get("targetR"):
        shifted["targetR"] = list(audit["targetR"])
    return shifted


def evidence_tag(audit: Dict[str, Any]) -> Optional[str]:
    """記録専用タグ。0.25N → ``ENTRY_DEPTH_025N``。"""
    if not audit.get("eligible"):
        return None
    return "ENTRY_DEPTH_%03dN" % int(round(float(audit["depthN"]) * 100))


def _fmt(value: float) -> str:
    return f"{value:,.2f}"


def annotate(out: Dict[str, Any], scenario: Dict[str, Any], noise: Any,
             policy: Optional[Dict[str, Any]] = None) -> Tuple[Dict[str, Any], Optional[str]]:
    """``monitor_publish.enrich_decisive_strategy`` から呼ぶ唯一の入口。

    ``out`` に監査記録 ``entryDepth`` を書き(OFF では消す)、LIVE で対象なら平行移動した
    シナリオを返す。2 回呼ばれても二重に動かない —— 呼び出し側は毎回 decision から
    作り直した**元の幾何**のシナリオを渡す(pipeline と publish の 2 回評価で同じ結果)。
    """
    policy = policy or load_policy()
    if policy.get("mode") == "OFF":
        out.pop("entryDepth", None)
        return scenario, None
    audit = plan_shift(scenario, noise, policy)
    out["entryDepth"] = audit
    if not audit.get("eligible"):
        return scenario, None
    head = (f"R86 entry depth {audit['case']} {audit['side']} "
            f"E {_fmt(audit['original']['entry'])}→{_fmt(audit['shifted']['entry'])} "
            f"SL {_fmt(audit['original']['stop'])}→{_fmt(audit['shifted']['stop'])} "
            f"({audit['depthN']:g}N={audit['depthPt']:g}pt, risk {audit['riskPt']:g}pt unchanged)")
    if audit["mode"] == "SHADOW":
        return scenario, head + " [SHADOW: not applied]"
    shifted = apply_live(scenario, audit)
    evaluation = out.get("evaluation")
    decision = evaluation.get("decision") if isinstance(evaluation, dict) else None
    if isinstance(decision, dict):
        decision = dict(decision)
        decision["entry"] = shifted["entry"]
        decision["stop"] = shifted["stop"]
        tag = evidence_tag(audit)
        decision["evidence"] = list(dict.fromkeys([*(decision.get("evidence") or []), tag]))
        out["evaluation"] = {**evaluation, "decision": decision}
    return shifted, head + " [LIVE]"


# ---------------------------------------------------------------- 検証(読むだけ)

def _instant(value: Any) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def load_bars(secrets: str = SECRETS) -> List[Dict[str, float]]:
    """確定 3 分足の和集合。**各配列の最終行は形成中の可能性があるので捨てる。**

    出所: 監査バンドル ``snapshot.bars3m`` / ``tv_raw/bars3m.json`` / 台帳の
    ``result.view.market.bars`` と R57 決済チャート。確定値は出所間で一致する。
    """
    by_time: Dict[int, Dict[str, float]] = {}

    def add(rows: Iterable[Tuple[Any, Any, Any, Any, Any]]) -> None:
        clean = []
        for row in rows:
            try:
                clean.append((int(float(row[0])), float(row[1]), float(row[2]), float(row[3]), float(row[4])))
            except (TypeError, ValueError, IndexError):
                continue
        clean.sort()
        for t, o, h, l, c in clean[:-1]:
            by_time.setdefault(t, {"t": t, "o": o, "h": h, "l": l, "c": c})

    def snap(rows: Any) -> List[Tuple[Any, ...]]:
        return [(r.get("t"), r.get("o"), r.get("h"), r.get("l"), r.get("c"))
                for r in rows or [] if isinstance(r, dict)]

    for path in glob.glob(os.path.join(secrets, "monitor_cycle_*.json")):
        try:
            bundle = json.load(io.open(path, encoding="utf-8"))
        except (OSError, ValueError):
            continue
        add(snap((bundle.get("snapshot") or {}).get("bars3m")))
    try:
        raw = json.load(io.open(os.path.join(secrets, "tv_raw", "bars3m.json"), encoding="utf-8"))
        add([(r.get("time"), r.get("open"), r.get("high"), r.get("low"), r.get("close"))
             for r in raw.get("bars") or [] if isinstance(r, dict)])
    except (OSError, ValueError, AttributeError):
        pass
    try:
        with io.open(os.path.join(secrets, "autotrade_ledger.jsonl"), encoding="utf-8") as fh:
            for line in fh:
                if '"bars"' not in line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                view = ((row.get("result") or {}) if isinstance(row.get("result"), dict) else {}).get("view") or {}
                add(snap((view.get("market") or {}).get("bars")))
                items = list(view.get("recentResults") or [])
                if isinstance(view.get("result"), dict):
                    items.append(view["result"])
                for item in items:
                    if isinstance(item, dict):
                        add([tuple(b[:5]) for b in ((item.get("chart") or {}).get("bars") or [])
                             if isinstance(b, list) and len(b) >= 5])
    except OSError:
        pass
    return [by_time[t] for t in sorted(by_time)]


def executable(side: str, entry: float, price: Optional[float]) -> bool:
    """engine の ``_command_for_entry`` と同じ判定。建値に価格が届いていれば成行。"""
    return price is not None and ((side == "BUY" and entry >= price) or (side == "SELL" and entry <= price))


def simulate(side: str, entry: float, stop: float, targets: List[float], bars: List[Dict[str, float]],
             start: int, rest_sec: int, horizon_sec: int = 6 * 3600,
             trade_through: float = TICK, market_price: Optional[float] = None,
             times: Optional[List[int]] = None) -> Dict[str, Any]:
    """凍結幾何を確定足に当てる。足の中の順序は分からないので**不利側**を採る。

    - 成行(``market_price``): ``start`` を含む足で約定。その足の逆行極値も数える
    - 指値: ``start`` より後に開いた足が建値を ``trade_through`` 突き抜けたとき約定
      (建値ちょうどのタッチは待ち行列の後ろで約定しないことがある)
    - 約定前に TP1 へ触れたら取消(R52 と同じ)。``rest_sec`` を過ぎても取消
    - 約定足の TP1 は数えない。同じ足で SL と TP の両方に触れたら SL
    - TP1 後は runner の SL を建値へ(CLAUDE.md §4.2)
    - R は**計画の** SL 幅で割る(ULTRA は計画の幅から枚数を出す)
    """
    long_side = side == "BUY"
    tp1, final = targets[0], targets[-1]
    risk = abs(entry - stop)
    times = times if times is not None else [bar["t"] for bar in bars]
    fill = None
    fill_px = entry
    if market_price is not None:
        index = bisect.bisect_right(times, start) - 1
        if index < 0 or start - bars[index]["t"] >= 180:
            return {"outcome": "NO_BARS"}
        fill, fill_px = index, float(market_price)
    else:
        first = bisect.bisect_right(times, start)
        if first >= len(bars) or bars[first]["t"] - start > 600:
            return {"outcome": "NO_BARS"}             # 公開直後の足が無い = 判定材料が無い
        for index in range(first, len(bars)):
            bar = bars[index]
            if bar["t"] - start > rest_sec:
                return {"outcome": "NO_FILL"}
            if index > first and bar["t"] - bars[index - 1]["t"] > 65 * 60:
                return {"outcome": "NO_BARS"}         # 取得の欠落(CME の 1 時間休場は除く)
            if (bar["l"] <= entry - trade_through) if long_side else (bar["h"] >= entry + trade_through):
                fill = index
                break
            if (bar["h"] >= tp1) if long_side else (bar["l"] <= tp1):
                return {"outcome": "TP1_FIRST"}
        if fill is None:
            return {"outcome": "NO_BARS"}
    t0 = bars[fill]["t"]
    runner_stop, tp1_done, legs, worst, last = stop, False, [], None, bars[fill]
    for index in range(fill, len(bars)):
        bar = bars[index]
        if bar["t"] - t0 > horizon_sec or (index > fill and bar["t"] - bars[index - 1]["t"] > 65 * 60):
            break
        last = bar
        adverse = bar["l"] if long_side else bar["h"]
        if not tp1_done:
            worst = adverse if worst is None else (min(worst, adverse) if long_side else max(worst, adverse))
        hit_stop = (bar["l"] <= runner_stop) if long_side else (bar["h"] >= runner_stop)
        hit_tp1 = (not tp1_done) and index != fill and ((bar["h"] >= tp1) if long_side else (bar["l"] <= tp1))
        hit_final = tp1_done and ((bar["h"] >= final) if long_side else (bar["l"] <= final))
        if hit_stop:
            legs.append((1 if tp1_done else 2, runner_stop))
            break
        if hit_tp1:
            legs.append((1, tp1))
            tp1_done, runner_stop = True, fill_px
            continue
        if hit_final:
            legs.append((1, final))
            break
    done = sum(q for q, _ in legs)
    if done < 2:
        legs.append((2 - done, last["c"]))
    points = sum(((px - fill_px) if long_side else (fill_px - px)) * q for q, px in legs) / 2.0
    clearance = None if worst is None else ((worst - stop) if long_side else (stop - worst))
    # fillT / exitT は R90 の逐次再生(1 ポジションずつ)が使う。約定足と最後に見た足の時刻。
    return {"outcome": "FILLED", "r": points / risk, "points": points, "tp1": tp1_done,
            "clearancePt": clearance, "fillT": bars[fill]["t"], "exitT": last["t"]}


def load_setups(secrets: str = SECRETS) -> List[Dict[str, Any]]:
    """監査バンドルの公開シナリオを 1 セットアップ = 最初の周期の幾何、にまとめる。"""
    cycles = []
    for path in glob.glob(os.path.join(secrets, "monitor_cycle_*.json")):
        try:
            bundle = json.load(io.open(path, encoding="utf-8"))
        except (OSError, ValueError):
            continue
        plan = (bundle.get("scenarios") or {}).get("primary")
        moment = _instant(bundle.get("at"))
        noise = _finite(((bundle.get("evaluation") or {}).get("volGate") or {}).get("noise"))
        if not isinstance(plan, dict) or moment is None or noise is None:
            continue
        entry, stop = _finite(plan.get("entry")), _finite(plan.get("stop"))
        targets = [_finite(t) for t in plan.get("targets") or []]
        if entry is None or stop is None or len(targets) < 2 or None in targets:
            continue
        cycles.append({"t": int(moment.timestamp()), "model": str(plan.get("model") or ""),
                       "side": str(plan.get("side") or "").upper(), "state": str(plan.get("state") or ""),
                       "entry": entry, "stop": stop, "targets": targets, "noise": noise,
                       "price": _finite(bundle.get("price"))})
    cycles.sort(key=lambda c: c["t"])
    setups: List[Dict[str, Any]] = []
    for cycle in cycles:
        key = (cycle["model"], cycle["side"], cycle["entry"])
        if setups and setups[-1]["key"] == key and cycle["t"] - setups[-1]["last"] <= 20 * 60:
            setups[-1]["last"] = cycle["t"]
            continue
        setups.append({"key": key, "last": cycle["t"], **cycle})
    return setups


def _first_of_cluster(setups: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """同じ (model, side) が 20 分以内に撃ち直された塊は先頭 1 件だけ(相関を数え直さない)。"""
    out, last = [], {}
    for setup in setups:
        key = (setup["model"], setup["side"])
        if key in last and setup["t"] - last[key] <= 20 * 60:
            last[key] = setup["t"]
            continue
        last[key] = setup["t"]
        out.append(setup)
    return out


def _bootstrap(base: List[float], alt: List[float], n: int = 2000, seed: int = 86) -> Tuple[float, float, float, float]:
    rnd = random.Random(seed)
    m = len(base)
    if not m:
        return 0.0, 0.0, 0.0, 0.0
    diffs = sorted(sum(alt[i] - base[i] for i in idx) / m
                   for idx in ([rnd.randrange(m) for _ in range(m)] for _ in range(n)))
    return (sum(a - b for a, b in zip(alt, base)) / m, diffs[int(0.05 * n)], diffs[int(0.95 * n)],
            sum(1 for d in diffs if d > 0) / n)


CASES = (
    ("VP80(close-anchored)", {"VP80_REVERSION"}),
    ("LEVEL(BREAKER+TURTLE)", {"BREAKER_CONTINUATION", "TURTLE_SOUP_REVERSAL"}),
    ("OTE(0.705 midpoint)", {"OTE_FVG_PULLBACK"}),
)


def report(rest_min: int = 30, depths: Tuple[float, ...] = (0.25, 0.5, 1.0), secrets: str = SECRETS) -> int:
    bars = load_bars(secrets)
    setups = load_setups(secrets)
    if not bars or not setups:
        print("no bars or no published scenarios found")
        return 1
    times = [bar["t"] for bar in bars]
    print(f"bars {len(bars)} ({datetime.fromtimestamp(bars[0]['t'], JST):%m-%d %H:%M} → "
          f"{datetime.fromtimestamp(bars[-1]['t'], JST):%m-%d %H:%M} JST) / setups {len(setups)} / "
          f"limit rest {rest_min}min / limit fill = 1tick through / market when price reached entry / "
          f"same-bar SL first")

    def r_values(sub: List[Dict[str, Any]], k: float, widen_only: bool = False) -> Tuple[List[float], int]:
        values, fills = [], 0
        for s in sub:
            sgn = 1.0 if s["side"] == "BUY" else -1.0
            d = _tick_half_up(k * s["noise"])
            entry = s["entry"] if widen_only else round(s["entry"] - sgn * d, 2)
            stop = round(s["stop"] - sgn * d, 2)
            market = s["price"] if executable(s["side"], entry, s.get("price")) else None
            result = simulate(s["side"], entry, stop, s["targets"], bars, s["t"], rest_min * 60,
                              market_price=market, times=times)
            if result["outcome"] == "FILLED":
                fills += 1
                values.append(result["r"])
            else:
                values.append(0.0)
        return values, fills

    for label, models in CASES:
        sub = [s for s in setups if s["model"] in models]
        for view, items in (("all", sub), ("cluster", _first_of_cluster(sub))):
            if not items:
                continue
            base, base_fills = r_values(items, 0.0)
            line = f"{label:22s} {view:7s} n={len(items):3d} fill={base_fills:3d} base={sum(base) / len(items):+.3f}R"
            for k in depths:
                alt, fills = r_values(items, k)
                mean, lo, hi, prob = _bootstrap(base, alt)
                line += f" | {k:g}N {mean:+.3f}[{lo:+.2f},{hi:+.2f}] P{prob:.2f} f{fills}"
            alt, _ = r_values(items, 0.5, widen_only=True)
            mean, lo, hi, prob = _bootstrap(base, alt)
            line += f" | SLonly0.5N {mean:+.3f} P{prob:.2f}"
            print(line)
    return 0


def trades(secrets: str = SECRETS, policy: Optional[Dict[str, Any]] = None) -> int:
    """実トレード(台帳の凍結プラン + スコアカード)ごとに SL 余白と平行移動の結果を出す。"""
    policy = policy or load_policy()
    if not policy.get("rules"):
        policy = {**policy, "rules": [{"case": "VP80", "models": ["VP80_REVERSION"], "depthN": 0.25}]}
    bars = load_bars(secrets)
    scores: Dict[str, Dict[str, Any]] = {}
    try:
        with io.open(os.path.join(secrets, "model_scorecard.jsonl"), encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    row = json.loads(line)
                    if row.get("scenarioId"):
                        scores[row["scenarioId"]] = row
    except (OSError, ValueError):
        pass
    plans: Dict[str, Dict[str, Any]] = {}
    try:
        with io.open(os.path.join(secrets, "autotrade_ledger.jsonl"), encoding="utf-8") as fh:
            for line in fh:
                if '"ENTRY_CLAIMED"' not in line:
                    continue
                row = json.loads(line)
                plan = row.get("plan")
                if isinstance(plan, dict) and plan.get("scenarioId") in scores and plan["scenarioId"] not in plans:
                    plans[plan["scenarioId"]] = {**plan, "_claimAt": row.get("time")}
    except (OSError, ValueError):
        pass
    times = [bar["t"] for bar in bars]
    print("JST          model                 side  type    E          SL         clear  actual | shifted E / SL        clear  sim")
    for sid, plan in sorted(plans.items(), key=lambda kv: str(kv[1].get("_claimAt"))):
        moment = _instant(plan.get("_claimAt"))
        entry, stop = _finite(plan.get("entry")), _finite(plan.get("initialStop"))
        targets = [_finite(t) for t in plan.get("targets") or []]
        side = str(plan.get("side") or "")
        score = scores.get(sid) or {}
        model = str(plan.get("model") or score.get("model") or "")
        if moment is None or entry is None or stop is None or None in targets or len(targets) < 2:
            continue
        start = int(moment.timestamp())
        # msnr_gate.noise_floor と同じ定義: 直前の確定 3 分足 12 本のレンジ中央値。
        prior = [b for b in bars if b["t"] + 180 <= start][-12:]
        nf = statistics.median(b["h"] - b["l"] for b in prior) if len(prior) == 12 else None
        order_type = str(plan.get("entryOrderType") or "LIMIT").upper()
        reference = _finite(plan.get("entryReference"))
        fill_price = _finite(plan.get("actualFillPrice")) or reference
        # 実際に約定したトレードなので、指値は建値ちょうど・成行は実約定(無ければ参照価格)で建てる。
        # シフト後の指値は 1tick 突き抜けを要求する(比較はシフト側に不利に倒してある)。
        base = simulate(side, entry, stop, targets, bars, start, 6 * 3600, trade_through=0.0,
                        market_price=fill_price if order_type == "MARKET" else None, times=times)
        audit = plan_shift({"model": model, "side": side, "entry": entry, "stop": stop,
                            "targets": targets}, nf, {**policy, "mode": "LIVE"})
        clear = base.get("clearancePt")
        line = (f"{moment.astimezone(JST):%m-%d %H:%M}  {model[:20]:20s}  {side:4s}  {order_type:6s}  "
                f"{entry:<10,.2f} {stop:<10,.2f} {('%6.2f' % clear) if clear is not None else '     -'}  "
                f"{str(score.get('outcome')):6s} |")
        if audit.get("eligible"):
            new_entry, new_stop = audit["shifted"]["entry"], audit["shifted"]["stop"]
            market = (fill_price if order_type == "MARKET" and executable(side, new_entry, reference)
                      else None)
            shifted = simulate(side, new_entry, new_stop, targets, bars, start, 6 * 3600,
                               market_price=market, times=times)
            sclear = shifted.get("clearancePt")
            outcome = shifted["outcome"]
            if outcome == "FILLED":
                outcome = ("TP1" if shifted.get("tp1") else "SL/open") + (" mkt" if market is not None else "")
            line += (f" {new_entry:,.2f} / {new_stop:,.2f}  "
                     f"{('%6.2f' % sclear) if sclear is not None else '     -'}  {outcome}")
        else:
            line += f" (unchanged: {audit.get('reason')})"
        print(line)
    print("clear = TP1 到達(または SL 到達)までの SL への最小余白 pt。負 = SL 到達。確定 3 分足。")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="R86 entry depth — read-only research report")
    parser.add_argument("--report", action="store_true", help="公開シナリオ全件で今のまま vs 深くした場合")
    parser.add_argument("--trades", action="store_true", help="実トレードごとの SL 余白と平行移動の結果")
    parser.add_argument("--rest", type=int, default=30, help="指値を待つ分数(既定 30)")
    parser.add_argument("--policy", action="store_true", help="現在の方針を表示")
    args = parser.parse_args(argv)
    if args.policy or not (args.report or args.trades):
        print(json.dumps(load_policy(), ensure_ascii=False, indent=2))
    code = 0
    if args.report:
        code = report(rest_min=args.rest) or code
    if args.trades:
        code = trades() or code
    return code


if __name__ == "__main__":
    sys.exit(main())

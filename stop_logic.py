# -*- coding: utf-8 -*-
"""R90: 初期 SL の 3 つの穴(VWAP 手前の SL / 成行切替で縮む SL 距離 / BREAKER の錨)。

きっかけ(2026-09-14 23:26 JST、BREAKER_CONTINUATION B BUY、docs/R90_STOP_LOGIC_HOLES.md)
-------------------------------------------------------------------------------------
予定建値 29,002.00(ブレイクしたレベル)/ SL 28,968.50 / VWAP 28,958.15 / ノイズ床 29.88pt。
発注時点でレベルを割っていたので成行に切り替わり、28,985.38 で約定。押しの安値 28,955.75
(VWAP を 4.6pt 割っただけ)で SL が刺さり、その後 +337pt 上昇して TP1・runner の両方に届いた。

穴 1 — **SL の計算が VWAP を見ていない。** SL は「構造の極値 − 1N」だけで決まるので、
  VWAP の 10pt 手前(0.35N)に置かれた。価格が VWAP を試しに来ると VWAP に届く前に刺さる位置。
  → ``vwap_clearance``: VWAP が SL の近く(既定 ±1.0N)にあれば、SL を VWAP の外側 0.25N へ逃がす。
    それで 60pt 上限・R:R が壊れれば候補は WATCH(SL を内側へ縮めて R:R を作らない)。
穴 2 — **成行に切り替わると実際の SL 距離がノイズ床より短くなる。** ``RISK_BELOW_NOISE`` は
  予定建値(SL まで 33.5pt = 1.12N)にしか効かず、実際の約定から SL までは 16.9pt(0.57N)だった。
  → ``market_stop_guard``: 発注時点の価格から SL まで 1.0N 未満なら成行を出さない(見送り)。
    engine は claim の前に公開価格で、order.py は送信直前に quote で、同じ算術を当てる。
穴 3 — **BREAKER の SL の錨がブレイク足で、上昇の起点ではない。** ブレイクが確定した 22:48 の
  足(安値 28,998.50)を錨にしたが、上昇は 22:45 の足(安値 28,950.75)から始まっていた。
  → ``flip_origin_index``: ブレイク足から遡って「安値が切り下がる限り」の起点を錨にする
    (R88 の TURTLE と同じ形。等級・decisionId が変わるので契約のスイッチで切り替える)。

このモジュールは純粋計算と契約の読み取りだけを持つ。ネットワーク・台帳・発注に触れない。
msnr_gate(穴 1・穴 3)と autotrade_engine(穴 2)が呼ぶ。order.py は隔離テストの都合で
このモジュールを import せず、engine から渡された ``--min-stop-pt`` を自前の 3 行で検査する。

設定は ``execution_contract.json`` の ``stopLogic``(節が無い・壊れている → 全部 OFF)。

    python stop_logic.py --policy        # 現在の方針
    python stop_logic.py --report        # 監査バンドルの再生(読むだけ。数分かかる)
"""
from __future__ import annotations

import argparse
import io
import json
import math
import os
import re
import statistics
import sys
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

BASE = os.path.dirname(os.path.abspath(__file__))
CONTRACT_PATH = os.path.join(BASE, "execution_contract.json")
VERSION = "R90-STOP-LOGIC-1"
TICK = 0.25
MODES3 = ("OFF", "SHADOW", "LIVE")
MODES2 = ("OFF", "LIVE")
ALL_MODELS = ("VP80_REVERSION", "TURTLE_SOUP_REVERSAL", "BREAKER_CONTINUATION", "OTE_FVG_PULLBACK")
#: msnr_gate.noise_floor / monitor_publish.vol_gate と同じ母数(直前の確定 3 分足 12 本)。
NOISE_BARS = 12

DEFAULT_VWAP = {"mode": "OFF", "withinN": 1.0, "clearN": 0.25, "models": list(ALL_MODELS)}
DEFAULT_MARKET = {"mode": "OFF", "minN": 1.0}
DEFAULT_FLIP = {"mode": "OFF", "maxBarsBack": 5}

# ---- R103-1: SL 側の流動性プール(liquidity_pools.py が使う語彙と既定値)
VERSION_R103 = "R103-POOL-CLEARANCE-1"
BAR_SEC = 180
POOL_KINDS = ("SWING_HIGH", "SWING_LOW", "SESSION", "VA_EDGE", "PD_EXTREME")
DEFAULT_POOL = {"mode": "OFF", "withinN": 1.0, "clearN": 0.25,
                "kinds": list(POOL_KINDS), "models": list(ALL_MODELS)}
DEFAULT_RESTING = {"mode": "OFF", "minN": 1.0,
                   "sessionOpen": {"minutes": 30, "opensEt": ["09:30", "03:00"]}}


def default_policy() -> Dict[str, Any]:
    return {"version": VERSION, "vwapClearance": dict(DEFAULT_VWAP),
            "marketStopGuard": dict(DEFAULT_MARKET), "flipOrigin": dict(DEFAULT_FLIP),
            "poolClearance": dict(DEFAULT_POOL),
            "restingStopRecheck": {"mode": "OFF", "minN": 1.0,
                                   "sessionOpen": dict(DEFAULT_RESTING["sessionOpen"])}}


# ---------------------------------------------------------------- 数値ユーティリティ

def finite(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def on_tick(value: float) -> bool:
    return abs(value * 4 - round(value * 4)) <= 1e-8


def outward_tick(value: float, side: str) -> float:
    """SL を **不利側**の tick へ丸める(BUY は切り下げ、SELL は切り上げ)。

    msnr_gate._tick_price は最近接なので SL が最大 0.125pt 内側へ寄ることがある。
    「VWAP の外側」を約束する値がその丸めで内側へ戻ってはいけない。
    """
    scaled = value / TICK
    if side == "BUY":
        return round(math.floor(scaled + 1e-9) * TICK, 2)
    return round(math.ceil(scaled - 1e-9) * TICK, 2)


# ---------------------------------------------------------------- 方針

def _mode(raw: Any, allowed) -> Optional[str]:
    mode = str(raw or "OFF").strip().upper()
    return mode if mode in allowed else None


def _hhmm(raw: Any) -> Optional[tuple]:
    """``"09:30"`` → ``(9, 30)``。形式が違えば None(推測で補わない)。"""
    text = str(raw or "").strip()
    if not re.fullmatch(r"\d{1,2}:\d{2}", text):
        return None
    hour, minute = (int(part) for part in text.split(":"))
    return (hour, minute) if 0 <= hour <= 23 and 0 <= minute <= 59 else None


def load_policy(contract: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """``stopLogic`` を検証して返す。壊れた節は **その節だけ** OFF に倒す。

    3 つの穴は独立した機能なので、1 つの設定ミスで残り 2 つまで止めない。
    """
    if contract is None:
        try:
            with io.open(CONTRACT_PATH, encoding="utf-8") as fh:
                contract = json.load(fh)
        except (OSError, ValueError):
            return default_policy()
    raw = (contract or {}).get("stopLogic") if isinstance(contract, dict) else None
    policy = default_policy()
    if not isinstance(raw, dict):
        return policy
    policy["version"] = str(raw.get("version") or VERSION)

    vwap = raw.get("vwapClearance")
    if isinstance(vwap, dict):
        mode = _mode(vwap.get("mode"), MODES3)
        within = finite(vwap.get("withinN", DEFAULT_VWAP["withinN"]))
        clear = finite(vwap.get("clearN", DEFAULT_VWAP["clearN"]))
        models = vwap.get("models", list(ALL_MODELS))
        ok = (mode is not None and within is not None and clear is not None
              and 0 < within <= 3.0 and 0 < clear <= 1.0
              and isinstance(models, list) and models
              and all(isinstance(m, str) and m in ALL_MODELS for m in models))
        if ok:
            policy["vwapClearance"] = {"mode": mode, "withinN": within, "clearN": clear,
                                       "models": list(dict.fromkeys(models))}

    market = raw.get("marketStopGuard")
    if isinstance(market, dict):
        mode = _mode(market.get("mode"), MODES2)
        min_n = finite(market.get("minN", DEFAULT_MARKET["minN"]))
        if mode is not None and min_n is not None and 0 < min_n <= 3.0:
            policy["marketStopGuard"] = {"mode": mode, "minN": min_n}

    flip = raw.get("flipOrigin")
    if isinstance(flip, dict):
        mode = _mode(flip.get("mode"), MODES2)
        back = flip.get("maxBarsBack", DEFAULT_FLIP["maxBarsBack"])
        if (mode is not None and isinstance(back, int) and not isinstance(back, bool)
                and 1 <= back <= 20):
            policy["flipOrigin"] = {"mode": mode, "maxBarsBack": back}

    # R103-1: 節ごとに独立して検証する。ここが壊れても上の 3 節は動かさない。
    pool = raw.get("poolClearance")
    if isinstance(pool, dict):
        mode = _mode(pool.get("mode"), MODES3)
        within = finite(pool.get("withinN", DEFAULT_POOL["withinN"]))
        clear = finite(pool.get("clearN", DEFAULT_POOL["clearN"]))
        kinds = pool.get("kinds", list(POOL_KINDS))
        models = pool.get("models", list(ALL_MODELS))
        ok = (mode is not None and within is not None and clear is not None
              and 0 < within <= 3.0 and 0 < clear <= 1.0
              and isinstance(kinds, list) and kinds
              and all(isinstance(k, str) and k in POOL_KINDS for k in kinds)
              and isinstance(models, list) and models
              and all(isinstance(m, str) and m in ALL_MODELS for m in models))
        if ok:
            policy["poolClearance"] = {"mode": mode, "withinN": within, "clearN": clear,
                                       "kinds": list(dict.fromkeys(kinds)),
                                       "models": list(dict.fromkeys(models))}

    resting = raw.get("restingStopRecheck")
    if isinstance(resting, dict):
        mode = _mode(resting.get("mode"), MODES3)
        min_n = finite(resting.get("minN", DEFAULT_RESTING["minN"]))
        session = resting.get("sessionOpen", DEFAULT_RESTING["sessionOpen"])
        minutes = (session or {}).get("minutes") if isinstance(session, dict) else None
        opens = (session or {}).get("opensEt") if isinstance(session, dict) else None
        ok = (mode is not None and min_n is not None and 0 < min_n <= 3.0
              and isinstance(minutes, int) and not isinstance(minutes, bool)
              and 1 <= minutes <= 240
              and isinstance(opens, list) and opens
              and all(isinstance(t, str) and _hhmm(t) is not None for t in opens))
        if ok:
            policy["restingStopRecheck"] = {
                "mode": mode, "minN": min_n,
                "sessionOpen": {"minutes": int(minutes), "opensEt": list(dict.fromkeys(opens))}}
    return policy


# ---------------------------------------------------------------- 穴 1: VWAP の外側へ

def vwap_clearance(side: str, entry: Any, stop: Any, vwap: Any, noise: Any,
                   rule: Optional[Dict[str, Any]] = None, model: Optional[str] = None) -> Dict[str, Any]:
    """VWAP が SL の近くにあれば、SL を VWAP の外側 ``clearN``×N へ置いた値を返す(純粋関数)。

    対象(BUY): 今の SL が ``required = tick_down(vwap − clearN·N)`` より上で、かつ
    ``stop − vwap ≤ withinN·N``。つまり SL が VWAP の手前(上)〜VWAP ちょうど〜VWAP の
    わずか下(clearN 未満)にあるとき。VWAP が SL の内側(建値寄り)に clearN 以上入って
    いれば SL は既に VWAP の外側にあり、VWAP が SL の外側 withinN より遠ければ触らない
    (構造 SL を VWAP のために大きく広げない)。SELL は鏡像。

    戻り値の ``eligible`` が True のときだけ ``stop`` が新しい SL。``reason`` は
    「評価して動かさなかった」と「入力が無い」を区別する。
    """
    rule = rule or DEFAULT_VWAP
    audit: Dict[str, Any] = {"version": VERSION, "mode": rule.get("mode", "OFF"), "model": model,
                             "eligible": False, "reason": None, "vwap": None,
                             "original": None, "stop": None}
    side = str(side or "").upper()
    entry_f, stop_f, vwap_f, nf = finite(entry), finite(stop), finite(vwap), finite(noise)
    if model is not None and model not in (rule.get("models") or ALL_MODELS):
        audit["reason"] = "MODEL_NOT_IN_POLICY"
        return audit
    if side not in {"BUY", "SELL"} or entry_f is None or stop_f is None:
        audit["reason"] = "GEOMETRY_MISSING"
        return audit
    audit["original"] = stop_f
    if vwap_f is None:
        audit["reason"] = "VWAP_MISSING"
        return audit
    audit["vwap"] = round(vwap_f, 2)
    if nf is None or nf <= 0:
        audit["reason"] = "NOISE_FLOOR_MISSING"
        return audit
    sgn = 1.0 if side == "BUY" else -1.0
    if (entry_f - stop_f) * sgn <= 0:
        audit["reason"] = "STOP_ON_WRONG_SIDE"
        return audit
    within_pt = float(rule.get("withinN", 1.0)) * nf
    clear_pt = float(rule.get("clearN", 0.25)) * nf
    gap = (stop_f - vwap_f) * sgn            # 正 = VWAP は SL の外側(BUY なら下)
    required = outward_tick(vwap_f - sgn * clear_pt, side)
    audit.update({"gapPt": round(gap, 2), "gapN": round(gap / nf, 2),
                  "withinPt": round(within_pt, 2), "clearPt": round(clear_pt, 2),
                  "required": required})
    if (stop_f - required) * sgn <= 0:
        audit["reason"] = "STOP_ALREADY_BEYOND_VWAP"
        return audit
    if gap > within_pt + 1e-9:
        audit["reason"] = "VWAP_FAR_FROM_STOP"
        return audit
    # ここまで来れば required < stop < entry(BUY)なので、新しい SL が建値を跨ぐことはない。
    audit.update({"eligible": True, "reason": None, "stop": required,
                  "shiftPt": round((stop_f - required) * sgn, 2),
                  "riskPt": round((entry_f - required) * sgn, 2),
                  "riskN": round((entry_f - required) * sgn / nf, 2)})
    return audit


VWAP_EVIDENCE_TAG = "VWAP_STOP_CLEARED"


# ---------------------------------------------------------------- 穴 2: 成行の SL 距離

def market_stop_guard(side: str, stop: Any, price: Any, noise: Any,
                      rule: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """発注時点の価格から SL までの距離が ``minN``×N 以上か(純粋関数)。

    ``block`` が True なら成行を出さない。距離が測れない(価格・N が無い)ときも
    True —— 「証明できたら許可」(fail-closed)。SL が価格の向こう側にあるときも True
    (order.py の向きチェックと同じ)。
    """
    rule = rule or DEFAULT_MARKET
    side = str(side or "").upper()
    stop_f, price_f, nf = finite(stop), finite(price), finite(noise)
    verdict: Dict[str, Any] = {"version": VERSION, "mode": rule.get("mode", "OFF"), "block": False,
                               "reason": None, "price": price_f, "stop": stop_f, "noise": nf}
    if side not in {"BUY", "SELL"} or stop_f is None or price_f is None:
        verdict.update({"block": True, "reason": "PRICE_OR_STOP_MISSING"})
        return verdict
    if nf is None or nf <= 0:
        verdict.update({"block": True, "reason": "NOISE_FLOOR_MISSING"})
        return verdict
    dist = (price_f - stop_f) if side == "BUY" else (stop_f - price_f)
    min_pt = round(float(rule.get("minN", 1.0)) * nf, 2)
    verdict.update({"distPt": round(dist, 2), "distN": round(dist / nf, 2), "minPt": min_pt})
    if dist <= 0:
        verdict.update({"block": True, "reason": "STOP_ON_WRONG_SIDE_OF_PRICE"})
    elif dist < min_pt - 1e-9:
        verdict.update({"block": True, "reason": "MARKET_STOP_TOO_CLOSE"})
    else:
        verdict["reason"] = "OK"
    return verdict


# ---------------------------------------------------------------- R103-1: 指値の SL 再検査

ET_ZONE = ZoneInfo("America/New_York")
RESTING_STALE_REASON = "RESTING_STOP_BELOW_MIN_N"


def _range_median(rows: List[Dict[str, Any]]) -> Optional[float]:
    ranges = []
    for bar in rows:
        high, low = finite(bar.get("h", bar.get("high"))), finite(bar.get("l", bar.get("low")))
        if high is None or low is None or high < low:
            continue
        ranges.append(high - low)
    if not ranges:
        return None
    value = statistics.median(ranges)
    return value if value > 0 else None


def resting_stop_recheck(side: str, entry: Any, stop: Any, bars_now: Any, at_iso: Any,
                         rule: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """指値を置いたままの SL が、**今のノイズ**に対して短くなっていないか(純粋関数)。

    指値は数十分置かれる。武装時点で 1N あった SL 距離は、寄付きでレンジが倍になれば
    そのまま「刈られに行く距離」へ変わる(2026-09-15 22:38 の SELL は 18.5pt = 武装時 1.20N、
    09:30 ET 以降のレンジ中央値 48.25pt では 0.38N)。

    ``noiseSessionOpen`` は寄付き(``sessionOpen.opensEt``)から ``minutes`` 以内なら
    ``max(直前 12 本の中央値, 寄付き以降の確定足のレンジ中央値)``、それ以外は ``noiseNow``。
    入力が足りなければ推測せず ``stale=False`` と理由だけを返す(記録専用)。
    """
    rule = rule or DEFAULT_RESTING
    out: Dict[str, Any] = {"version": VERSION_R103, "mode": rule.get("mode", "OFF"),
                           "noiseNow": None, "noiseSessionOpen": None, "distPt": None,
                           "stale": False, "reason": None}
    side = str(side or "").upper()
    entry_f, stop_f = finite(entry), finite(stop)
    if side not in {"BUY", "SELL"} or entry_f is None or stop_f is None:
        out["reason"] = "GEOMETRY_MISSING"
        return out
    sgn = 1.0 if side == "BUY" else -1.0
    if (entry_f - stop_f) * sgn <= 0:
        out["reason"] = "STOP_ON_WRONG_SIDE"
        return out
    out["distPt"] = round(abs(entry_f - stop_f), 2)
    try:
        at = datetime.fromisoformat(str(at_iso))
    except (TypeError, ValueError):
        at = None
    if at is None or at.tzinfo is None:
        out["reason"] = "AT_TIME_MISSING"
        return out
    rows = [bar for bar in (bars_now or []) if isinstance(bar, dict)]
    closed = []
    for bar in rows:
        t = finite(bar.get("t", bar.get("time")))
        if t is None or t + BAR_SEC > at.timestamp() + 1e-9:
            continue
        closed.append(dict(bar, t=t))
    closed.sort(key=lambda bar: bar["t"])
    if len(closed) < NOISE_BARS:
        out["reason"] = "NOISE_FLOOR_MISSING"
        return out
    noise_now = _range_median(closed[-NOISE_BARS:])
    if noise_now is None:
        out["reason"] = "NOISE_FLOOR_MISSING"
        return out
    out["noiseNow"] = round(noise_now, 2)

    session = rule.get("sessionOpen") if isinstance(rule.get("sessionOpen"), dict) else {}
    minutes = session.get("minutes", DEFAULT_RESTING["sessionOpen"]["minutes"])
    minutes = minutes if isinstance(minutes, int) and not isinstance(minutes, bool) else 30
    at_et = at.astimezone(ET_ZONE)
    noise = noise_now
    for text in (session.get("opensEt") or DEFAULT_RESTING["sessionOpen"]["opensEt"]):
        hhmm = _hhmm(text)
        if hhmm is None:
            continue
        open_et = at_et.replace(hour=hhmm[0], minute=hhmm[1], second=0, microsecond=0)
        if not (timedelta(0) <= at_et - open_et < timedelta(minutes=minutes)):
            continue
        since = [bar for bar in closed if bar["t"] >= open_et.timestamp()]
        opened = _range_median(since)
        if opened is not None and opened > noise:
            noise = opened
        out["sessionOpenEt"] = text
        out["barsSinceOpen"] = len(since)
        break
    out["noiseSessionOpen"] = round(noise, 2)
    min_pt = round(float(rule.get("minN", DEFAULT_RESTING["minN"])) * noise, 2)
    out["minPt"] = min_pt
    out["distN"] = round(out["distPt"] / noise, 2)
    out["stale"] = bool(out["distPt"] < min_pt - 1e-9)
    out["reason"] = RESTING_STALE_REASON if out["stale"] else "OK"
    return out


def noise_from_bundle(bundle: Dict[str, Any]) -> Optional[float]:
    """公開バンドルのノイズ床。``evaluation.volGate.noise`` があればそれ、無ければ
    確定 3 分足 12 本のレンジ中央値(msnr_gate.noise_floor と同じ定義)。12 本無ければ None。"""
    evaluation = (bundle or {}).get("evaluation")
    if isinstance(evaluation, dict):
        value = finite((evaluation.get("volGate") or {}).get("noise"))
        if value is not None and value > 0:
            return value
    snapshot = (bundle or {}).get("snapshot") or {}
    bars = snapshot.get("bars3m") or snapshot.get("bars") or []
    ranges = []
    for bar in bars[-NOISE_BARS:] if isinstance(bars, list) else []:
        if not isinstance(bar, dict):
            continue
        high = finite(bar.get("h", bar.get("high")))
        low = finite(bar.get("l", bar.get("low")))
        if high is not None and low is not None and high >= low:
            ranges.append(high - low)
    if len(ranges) < NOISE_BARS:
        return None
    value = statistics.median(ranges)
    return value if value > 0 else None


# ---------------------------------------------------------------- 穴 3: BREAKER の錨

def flip_origin_index(bars: List[Dict[str, Any]], break_idx: int, side: str, max_back: int = 5) -> int:
    """ブレイク足 ``break_idx`` から遡り、上昇(BUY)の起点の足の添字を返す。

    遡り方: 直前の足の安値が今の起点の安値より低い限り起点を 1 本前へ動かす。
    最初に「切り下がらない」足が出たらそこで止める(手前のスイングは含めない)。
    遡る本数は ``max_back`` まで。SELL は高値の切り上がりで鏡像。
    起点が動かなければ ``break_idx`` 自身(= R90 以前と同じ錨)。
    """
    origin = break_idx
    key = "l" if str(side).upper() == "BUY" else "h"
    for k in range(break_idx - 1, max(-1, break_idx - 1 - int(max_back)), -1):
        try:
            prev, cur = float(bars[k][key]), float(bars[origin][key])
        except (KeyError, TypeError, ValueError, IndexError):
            break
        deeper = prev < cur if key == "l" else prev > cur
        if not deeper:
            break
        origin = k
    return origin


FLIP_EVIDENCE_TAG = "FLIP_STOP_ORIGIN"


# ---------------------------------------------------------------- 表示

def describe_vwap(audit: Dict[str, Any]) -> str:
    if not audit.get("eligible"):
        return f"R90 vwap clearance: not applied ({audit.get('reason')})"
    return (f"R90 vwap clearance {audit.get('model') or ''} SL {audit['original']:,.2f}→{audit['stop']:,.2f} "
            f"(VWAP {audit['vwap']:,.2f} gap {audit['gapN']:+.2f}N → +{audit['shiftPt']:g}pt, "
            f"risk {audit['riskPt']:g}pt={audit['riskN']:g}N) [{audit.get('mode')}]")


def describe_guard(verdict: Dict[str, Any]) -> str:
    if verdict.get("distPt") is None:
        return f"R90 market stop guard: {verdict.get('reason')}"
    return (f"R90 market stop guard: |price {verdict['price']:,.2f} − SL {verdict['stop']:,.2f}| = "
            f"{verdict['distPt']:g}pt ({verdict['distN']:g}N) vs min {verdict['minPt']:g}pt → "
            f"{'BLOCK' if verdict.get('block') else 'OK'} ({verdict.get('reason')})")


# ---------------------------------------------------------------- 再生(読むだけ)

def report(argv_rest: Optional[List[str]] = None) -> int:
    """監査バンドルを現行コードで再評価し、方針の有無で公開シナリオがどう変わるかを出す。

    実装は ``replay_stop_logic.py``(docs/R90 §2)と同じ規則。ここでは要約だけを印字する。
    """
    import replay_stop_logic  # noqa: WPS433 - 重い再生は別モジュール(読むだけ)
    return replay_stop_logic.main(argv_rest or [])


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="R90 stop logic — policy / read-only replay")
    parser.add_argument("--policy", action="store_true", help="現在の方針を表示")
    parser.add_argument("--report", action="store_true", help="監査バンドルの再生(読むだけ)")
    args, rest = parser.parse_known_args(argv)
    if args.report:
        return report(rest)
    print(json.dumps(load_policy(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

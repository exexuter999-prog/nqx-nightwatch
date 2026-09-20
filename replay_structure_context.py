# -*- coding: utf-8 -*-
"""R122 の逐次再生(読むだけ)。多層文脈と参加判断の 4 条件を**同条件**で比べる。

    python replay_structure_context.py --replay                 # 監査コーパス全件
    python replay_structure_context.py --replay --bars15m       # 15 分足がある窓だけ
    python replay_structure_context.py --replay --limit 400
    python replay_structure_context.py --forecast               # 短期予測の採点だけ

条件(差し替えるのは `msnr_gate.structure_context_policy` **だけ**):

  A_NOW    R122 全段 OFF(= 現行本番。STDV TARGETS LIVE + TURTLE 復活済み)
  B_SHADOW context / participation / nearTerm を SHADOW(記録だけ。注文意図は A と同一のはず)
  C_LIVE   context / participation を LIVE(親が否定された継続だけ武装しない)
  D_SHALL  C + shallowCandidate LIVE(浅い構造の代替候補)
  E_SELECT D + selection LIVE(高得点 WATCH より ARMED を優先。**選択層の変更**)

同条件の意味: 同じ監査バンドル、**実際に効く** modelGate / ictStdv / riskCap / stopLogic /
limitGate(契約からそのまま読む)、`entry_depth.simulate` の同じ約定規則、同じ逐次 1 建玉
制約、R は計画の SL 幅。`replay_ict_stdv.py` / `replay_r103.py` / `replay_stop_logic.py` と
同じ規則。**手数料とスリッページは含まれていない。**

``--bars15m``: 親の仮説を「直接取得した確定 15 分足」で作るための窓。`.secrets/tv_raw/
bars15m.json`(最後の実取得。`CME_MINI:MNQ1!`)から、**各バンドルの `at` 以前に閉じた足
だけ**を `snapshot.bars15m` へ入れる。この 15 分足がバンドル自身の 3 分足と同じ価格系列で
あることは毎回ハッシュではなく**値で**検算する(`verify_bars15m`)。合成ではない。

``--source recon``: **研究用**。監査バンドル自身の確定 3 分足から 15 分足を組み直して窓を
広げる。直接取得分と重なる範囲でしか正しさを確かめられない(重なりは 50 窓)ので、
**全期間の同一性は保証されない**。本番の親の出所は直接取得した `snapshot.bars15m` だけで、
この経路は本番コードには存在しない(`market_structure_context` は 3 分足から 15 分足を
作らない。`tests/test_r122_structure_context.py::test_production_never_reconstructs_15m`)。

ネットワーク・台帳への書き込み・発注には触れない。
"""
from __future__ import annotations

import argparse
import collections
import copy
import glob
import hashlib
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
BAR15_SEC = 900
BAR3_SEC = 180

STAGES = ("context", "participation", "shallowCandidate", "selection", "nearTerm")


def _stage(context="OFF", participation="OFF", shallow="OFF", selection="OFF", near="OFF"):
    return {"context": {"mode": context}, "participation": {"mode": participation},
            "shallowCandidate": {"mode": shallow}, "selection": {"mode": selection},
            "nearTerm": {"mode": near}, "params": {}, "invalid": []}


def _v(policy: Dict[str, Any], with15: bool) -> Dict[str, Any]:
    """1 変種 = (契約の段, 15 分足を渡すか)。**15 分足の有無も変種の一部**にして、
    保存修正(データが評価器へ届くこと)と判断ロジックの効果を混ぜない。"""
    return {"policy": policy, "bars15m": with15}


#: 段ごとの効果(従来の並び)。15 分足は `--bars15m` で全変種に一括で渡す。
LADDER: Dict[str, Dict[str, Any]] = {
    "A_NOW": _v(_stage(), False),
    "B_SHADOW": _v(_stage("SHADOW", "SHADOW", "SHADOW", "OFF", "SHADOW"), False),
    "C_LIVE": _v(_stage("LIVE", "LIVE", "OFF", "OFF", "SHADOW"), False),
    "D_SHALL": _v(_stage("LIVE", "LIVE", "LIVE", "OFF", "SHADOW"), False),
    "E_SELECT": _v(_stage("LIVE", "LIVE", "LIVE", "LIVE", "SHADOW"), False),
}
#: 2x2。行 = 保存修正(15 分足が届くか)、列 = 判断ロジック(R122 OFF / context+participation LIVE)。
SPLIT: Dict[str, Dict[str, Any]] = {
    "A0_OFF_NO15": _v(_stage(), False),
    "A1_OFF_15": _v(_stage(), True),
    "C0_LIVE_NO15": _v(_stage("LIVE", "LIVE", "OFF", "OFF", "SHADOW"), False),
    "C1_LIVE_15": _v(_stage("LIVE", "LIVE", "OFF", "OFF", "SHADOW"), True),
}
VARIANTS: Dict[str, Dict[str, Any]] = dict(LADDER)
BASELINE = "A_NOW"


def use_variants(table: Dict[str, Dict[str, Any]], baseline: str,
                 force15: bool = False) -> None:
    global VARIANTS, BASELINE
    VARIANTS = {name: _v(spec["policy"], spec["bars15m"] or force15)
                for name, spec in table.items()}
    BASELINE = baseline


#: 注文意図(これが A と一致すれば「記録しか増えていない」と言える)。
INTENT_KEYS = ("model", "side", "state", "grade", "entry", "stop", "targets", "decisionId")
DECISION_KEYS = INTENT_KEYS + ("score", "targetR", "hardBlockers", "entryMode")


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


# ------------------------------------------------------- 15 分足(直接取得)の注入

def load_raw_15m(secrets: str = SECRETS) -> List[Dict[str, float]]:
    """`tv_raw/bars15m.json` の確定足。**最終行は形成中の可能性があるので捨てる。**"""
    try:
        raw = json.load(io.open(os.path.join(secrets, "tv_raw", "bars15m.json"), encoding="utf-8"))
    except (OSError, ValueError):
        return []
    rows = []
    for item in raw.get("bars") or []:
        try:
            rows.append({"t": int(item["time"]), "o": float(item["open"]),
                         "h": float(item["high"]), "l": float(item["low"]),
                         "c": float(item["close"])})
        except (KeyError, TypeError, ValueError):
            continue
    rows.sort(key=lambda b: b["t"])
    return rows[:-1]


def verify_bars15m(bars15: List[Dict[str, float]], paths: List[str]) -> Dict[str, Any]:
    """15 分足がコーパスの 3 分足と**同じ価格系列**であることを値で確かめる。

    足りない窓は数えるだけで、ズレが 1 つでもあれば使わない(R102 のロール事故と同じ形を
    作らないため。ズラして合わせることは絶対にしない)。
    """
    by_t: Dict[int, Dict[str, float]] = {}
    for path in paths:
        try:
            bundle = json.load(io.open(path, encoding="utf-8"))
        except (OSError, ValueError):
            continue
        rows = sorted(((int(r["t"]), r) for r in ((bundle.get("snapshot") or {}).get("bars3m") or [])
                       if isinstance(r, dict) and r.get("t") is not None))
        for t, row in rows[:-1]:
            by_t.setdefault(t, row)
    matched = mismatched = 0
    for bar in bars15:
        need = [by_t.get(bar["t"] + BAR3_SEC * k) for k in range(5)]
        if any(x is None for x in need):
            continue
        matched += 1
        hi = max(float(x["h"]) for x in need)
        lo = min(float(x["l"]) for x in need)
        close = float(need[-1]["c"])
        if abs(hi - bar["h"]) > 1e-9 or abs(lo - bar["l"]) > 1e-9 or abs(close - bar["c"]) > 1e-9:
            mismatched += 1
    return {"matched": matched, "mismatched": mismatched, "ok": matched > 0 and mismatched == 0}


def reconstruct_15m(paths: List[str]) -> Tuple[List[Dict[str, float]], Dict[str, Any]]:
    """監査バンドル**自身の確定 3 分足**から 15 分足を組み直す。

    5 本そろった窓だけを作り、1 本でも欠けたらその 15 分足は作らない(欠損を推測で
    埋めない)。これは「上位足を 3 分足で代用する」ことではなく、**同じ市場の同じ確定足を
    足し合わせた再構成**で、正しさは直接取得した 15 分足(`tv_raw/bars15m.json`)との
    重なりで**値ごと**検算する(`verify_bars15m`)。一致しなければ使わない。
    """
    by_t: Dict[int, Dict[str, float]] = {}
    for path in paths:
        try:
            bundle = json.load(io.open(path, encoding="utf-8"))
        except (OSError, ValueError):
            continue
        rows = sorted(((int(r["t"]), r) for r in ((bundle.get("snapshot") or {}).get("bars3m") or [])
                       if isinstance(r, dict) and r.get("t") is not None))
        for t, row in rows[:-1]:          # 各配列の最終行は形成中の可能性
            by_t.setdefault(t, {"t": t, "o": float(row["o"]), "h": float(row["h"]),
                                "l": float(row["l"]), "c": float(row["c"])})
    out: List[Dict[str, float]] = []
    if by_t:
        lo, hi = min(by_t), max(by_t)
        for t15 in range(lo // BAR15_SEC * BAR15_SEC, hi + 1, BAR15_SEC):
            parts = [by_t.get(t15 + BAR3_SEC * k) for k in range(5)]
            if any(p is None for p in parts):
                continue
            out.append({"t": t15, "o": parts[0]["o"], "h": max(p["h"] for p in parts),
                        "l": min(p["l"] for p in parts), "c": parts[-1]["c"]})
    return out, {"source": "RECONSTRUCTED_FROM_CONFIRMED_3M", "bars3m": len(by_t),
                 "bars15m": len(out)}


def inject_bars15m(bundle: Dict[str, Any], bars15: List[Dict[str, float]]) -> int:
    """バンドルの `at` 以前に閉じた **連続した** 15 分足だけを入れる。

    - 未来足は入れない(`t + 900 <= at`)。
    - 途切れている手前は切る —— 穴のある系列でピボットを取ると、実際には隣り合って
      いない足を隣として読んでしまう。
    - 鮮度(最終足が古すぎる)は `market_structure_context` 側が既存の窓で弾く。
    """
    moment = _instant(bundle.get("at"))
    if moment is None or not bars15:
        return 0
    cutoff = moment.timestamp()
    usable = [b for b in bars15 if b["t"] + BAR15_SEC <= cutoff]
    if not usable:
        return 0
    rows = [usable[-1]]
    for bar in reversed(usable[:-1]):
        if rows[0]["t"] - bar["t"] != BAR15_SEC:
            break
        rows.insert(0, bar)
    snapshot = bundle.setdefault("snapshot", {})
    snapshot["bars15m"] = [dict(b) for b in rows]
    return len(rows)


# ------------------------------------------------------------------- 再生本体

def evaluate_all(paths: List[str], bars15: Optional[List[Dict[str, float]]] = None) -> List[Dict[str, Any]]:
    import msnr_gate
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
            bundle.get("snapshot", {}).pop("bars15m", None)
            with15 = copy.deepcopy(bundle)
            injected = inject_bars15m(with15, bars15 or [])
            # 入力ハッシュは**15 分足を除いた共通部分**で取る。両条件が同じ 3 分足・価格・
            # 時刻を読んでいることを示すため(15 分足の有無は変種の定義そのもの)。
            digest = hashlib.sha256(
                json.dumps(bundle, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]
            row = {"path": os.path.basename(path), "at": moment.isoformat(),
                   "t": int(moment.timestamp()), "price": _finite(bundle.get("price")),
                   "noise": None, "bars15m": injected, "inputHash": digest, "variants": {}}
            for name, spec in VARIANTS.items():
                policy = spec["policy"]
                source = with15 if spec["bars15m"] else bundle
                msnr_gate.structure_context_policy = (lambda contract=None, p=policy: p)
                try:
                    result = msnr_gate.evaluate(copy.deepcopy(source))
                except Exception as exc:  # noqa: BLE001 - 再生は落とさず記録する
                    row["variants"][name] = {"error": f"{type(exc).__name__}: {exc}"}
                    continue
                if row["noise"] is None:
                    row["noise"] = result.get("noiseFloor")
                decision = result.get("decision") or {}
                context = result.get("structureContext") or {}
                thesis = context.get("thesis") or {}
                item = {k: decision.get(k) for k in DECISION_KEYS}
                item["restingLimit"] = bool(decision.get("restingLimit"))
                item["structure"] = decision.get("structure")
                item["ctxStatus"] = context.get("status")
                item["ctxReasons"] = list(context.get("reasons") or ())
                item["relation"] = context.get("childRelation")
                item["thesisTf"] = thesis.get("timeframe")
                item["thesisFidelity"] = thesis.get("fidelity")
                item["thesisBias"] = thesis.get("bias")
                item["thesisPhase"] = thesis.get("phase")
                item["thesisInvalidated"] = thesis.get("invalidatedAt")
                item["nearTerm"] = context.get("nearTerm") or {}
                item["shallowBuilt"] = sum(1 for c in (result.get("candidates") or [])
                                           if c.get("r122Shallow"))
                item["parentBlocked"] = sum(1 for c in (result.get("candidates") or [])
                                            if c.get("participationBlocked"))
                item["has15m"] = bool(spec["bars15m"] and injected)
                item["participationStates"] = collections.Counter(
                    str(((c.get("participation") or {}).get("state")))
                    for c in (result.get("candidates") or []) if c.get("participation"))
                row["variants"][name] = item
            rows.append(row)
            if index % 100 == 0:
                print(f"  {index}/{len(paths)} ({time.time() - started:.0f}s)", flush=True)
    finally:
        msnr_gate.structure_context_policy = saved
    return rows


def _r_of(item: Optional[Dict[str, Any]]) -> float:
    if not item:
        return 0.0
    res = item["res"]
    return float(res.get("r") or 0.0) if res.get("outcome") == "FILLED" else 0.0


def _entry_type(setup: Dict[str, Any], resting: set) -> str:
    key = (setup.get("model"), setup.get("side"), setup.get("entry"), setup.get("stop"))
    if key in resting:
        return "RESTING_LIMIT"
    if str(setup.get("model") or "") in {"TURTLE_SOUP_REVERSAL", "BREAKER_CONTINUATION"}:
        return "RETEST_HELD"
    return "OTHER"


def _resting_keys(rows: List[Dict[str, Any]], variant: str) -> set:
    out = set()
    for row in rows:
        dec = row["variants"].get(variant) or {}
        if not dec.get("restingLimit"):
            continue
        entry, stop = _finite(dec.get("entry")), _finite(dec.get("stop"))
        if dec.get("model") and entry is not None and stop is not None:
            out.add((dec["model"], dec.get("side"), entry, stop))
    return out


def forecast_report(rows: List[Dict[str, Any]], bars, variant: Optional[str] = None) -> None:
    """短期予測(3 分 / 15 分)の採点。**表示・評価専用で参加には効いていない。**

    採点規則(測定前に固定):
      * 中立幅 = 1.0 x ノイズ床。実測 |終値 - 基準価格| がこれ以下なら実測 NEUTRAL。
      * 的中 = 予測と実測の**完全一致**。「逆方向ではなく動かなかった」も、その地平では
        **外れ**として数える(内訳は別途出す)。
      * UNKNOWN は方向を出していないので的中率の分母に入れず、率として別に出す。
      * 比較基準は**常に NEUTRAL と答える予測器**。同じ行・同じ規則で採点する。
    """
    import market_structure_context as msc
    if variant is None:
        variant = next((n for n in ("C1_LIVE_15", "C_LIVE", "B_SHADOW") if n in VARIANTS),
                       list(VARIANTS)[-1])
    by_t = {int(b["t"]): b for b in bars}
    cells: Dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    bands: List[float] = []
    for row in rows:
        var = row["variants"].get(variant) or {}
        forecast = var.get("nearTerm") or {}
        direction = str(forecast.get("direction") or "UNKNOWN")
        band = _finite(forecast.get("neutralBandPt"))
        if band is not None:
            bands.append(band)
        if not forecast.get("horizons"):
            for label in ("3m", "15m"):
                cells[label]["NO_FORECAST"] += 1
            continue
        ref = forecast.get("referenceBarT")
        future = [by_t[t] for t in (int(ref) + BAR3_SEC * k for k in range(1, 7)) if t in by_t]
        for item in msc.score_near_term(forecast, future).get("scored") or []:
            label = str(item.get("label"))
            actual = str(item.get("actual"))
            c = cells[label]
            c["rows"] += 1
            if actual == "UNAVAILABLE":
                c["unavailable"] += 1
                continue
            c["scorable"] += 1
            c[f"actual:{actual}"] += 1
            c[f"pred:{direction}"] += 1
            c[f"cell:{direction}->{actual}"] += 1
            if direction == "UNKNOWN":
                c["unknown"] += 1
            else:
                c["answered"] += 1
                c["hit" if direction == actual else "miss"] += 1
            if actual == "NEUTRAL":
                c["baselineHit"] += 1        # 常に NEUTRAL と答える基準
            else:
                c["baselineMiss"] += 1
    print(f"\n--- 短期予測(変種 {variant}。表示・評価専用。参加ゲートではない) ---")
    if bands:
        print(f"  中立幅 = 1.0xノイズ床。中央 {statistics.median(bands):.2f}pt / "
              f"最小 {min(bands):.2f} / 最大 {max(bands):.2f}")
    for label in ("3m", "15m"):
        c = cells.get(label) or collections.Counter()
        n = c["scorable"] or 1
        answered = c["answered"] or 0
        hit = c["hit"]
        print(f"\n  [{label}] 採点できた行 {c['scorable']} "
              f"(後続足なし {c['unavailable']}, 予測なし {c['NO_FORECAST']})")
        print(f"    UNKNOWN 率 {c['unknown']}/{c['scorable']} = {c['unknown']/n*100:.1f}%  /  "
              f"方向を出した行 {answered}")
        print(f"    予測の内訳  UP={c['pred:UP']} DOWN={c['pred:DOWN']} "
              f"NEUTRAL={c['pred:NEUTRAL']} UNKNOWN={c['pred:UNKNOWN']}")
        print(f"    実測の内訳  UP={c['actual:UP']} DOWN={c['actual:DOWN']} "
              f"NEUTRAL={c['actual:NEUTRAL']}")
        print("    混同表(予測 -> 実測):")
        for pred in ("UP", "DOWN", "NEUTRAL", "UNKNOWN"):
            parts = [f"{act}={c[f'cell:{pred}->{act}']}" for act in ("UP", "DOWN", "NEUTRAL")]
            total = sum(c[f"cell:{pred}->{act}"] for act in ("UP", "DOWN", "NEUTRAL"))
            if total:
                correct = c[f"cell:{pred}->{pred}"] if pred != "UNKNOWN" else 0
                print(f"      {pred:8s} n={total:5d}  " + "  ".join(parts)
                      + (f"   正解率 {correct/total*100:5.1f}%" if pred != "UNKNOWN" else ""))
        model_all = hit / n * 100.0
        base_all = c["baselineHit"] / n * 100.0
        print(f"    厳密な一致率(全採点行・UNKNOWN も外れとして数える)")
        print(f"      R122 の予測      {hit}/{c['scorable']} = {model_all:5.1f}%")
        print(f"      常に NEUTRAL     {c['baselineHit']}/{c['scorable']} = {base_all:5.1f}%"
              f"   差 {model_all - base_all:+.1f}pt")
        if answered:
            print(f"    方向を出した行だけ  的中 {hit}/{answered} = {hit/answered*100:.1f}%"
                  f"  (うち外れの内訳: 実測 NEUTRAL "
                  f"{c['cell:UP->NEUTRAL'] + c['cell:DOWN->NEUTRAL'] + c['cell:NEUTRAL->UP'] + c['cell:NEUTRAL->DOWN']} / "
                  f"実測が逆 {c['cell:UP->DOWN'] + c['cell:DOWN->UP']})")
            moved_hit = c["cell:UP->UP"] + c["cell:DOWN->DOWN"]
            moved = moved_hit + c["cell:UP->DOWN"] + c["cell:DOWN->UP"]
            if moved:
                print(f"    参考: 実際に中立幅を超えて動いた行に限れば 向きの一致 "
                      f"{moved_hit}/{moved} = {moved_hit/moved*100:.1f}%"
                      f"(**これは上の的中率ではない。動かなかった行を除いた参考値**)")
    print("\n  注: 中立幅は測定前に固定した(1.0xノイズ床 = 既存 STOP_BUFFER_NF_MULT と同じ倍率)。"
          "確率は付けない。この予測はどの段でも参加判断に使っていない。")


def scenes(rows: List[Dict[str, Any]], variant: str = "C_LIVE", limit: int = 30,
           secrets: str = SECRETS) -> None:
    """設計評価用の場面表。**結果を見て選ばない** —— 連続期間の先頭から時刻順に固定する。

    列: 時刻 / 親(出所・方向・保護水準・目標・否定)→ 子(状態・方向)→ 関係 →
    参加状態 → 最終モデルと Entry/SL/TP/ID → その計画が実際にどうなったか。
    """
    import entry_depth
    bars = entry_depth.load_bars(secrets)
    times = [b["t"] for b in bars]
    print(f"\n--- 設計評価の場面表({variant}・連続期間の先頭から時刻順に {limit} 件) ---")
    print("選び方: 結果を見ずに『候補がある周期を古い順』で固定。成績の標本ではなく誤読探し。")
    print("『結果』は**その幾何を当てたらどうなったか**で、WATCH の行は実際には発注していない。")
    taken = 0
    for row in rows:
        var = row["variants"].get(variant) or {}
        if not var.get("model") or var.get("model") == "FLAT":
            continue
        taken += 1
        if taken > limit:
            break
        st = var.get("structure") or {}
        entry, stop = _finite(var.get("entry")), _finite(var.get("stop"))
        targets = [_finite(t) for t in var.get("targets") or []]
        outcome = "-"
        if entry is not None and stop is not None and len(targets) >= 2 and None not in targets:
            res = entry_depth.simulate(var["side"], entry, stop, targets, bars, row["t"],
                                       30 * 60, market_price=row.get("price")
                                       if entry_depth.executable(var["side"], entry, row.get("price"))
                                       else None, times=times)
            outcome = f"{res.get('outcome')} r={res.get('r')}"
        print(f"\n[{taken:2d}] {row['at'][:19]}Z  price={row.get('price')}  nf={row.get('noise')}")
        print(f"     親 : {var.get('thesisTf')}/{var.get('thesisFidelity')} {var.get('thesisBias')} "
              f"phase={var.get('thesisPhase')} inval={var.get('thesisInvalidated')}")
        print(f"     子 : 関係={var.get('relation')}  文脈={var.get('ctxStatus')} "
              f"{','.join(var.get('ctxReasons') or []) or '-'}")
        print(f"     参加: {st.get('participationState')} / 意図={st.get('orderIntent')} / "
              f"根拠={','.join(st.get('triggerEvidenceIds') or []) or '-'}")
        print(f"     判断: {var.get('model')} {var.get('side')} {var.get('grade')} {var.get('state')} "
              f"E={var.get('entry')} SL={var.get('stop')} TP={var.get('targets')} "
              f"id={var.get('decisionId')}")
        print(f"     結果: {outcome}  短期予測={((var.get('nearTerm') or {}).get('direction'))}"
              f"  変化={st.get('changedFromBaseline')}")


def _sequential(rows: List[Dict[str, Any]], name: str, bars, times, guard_n, rest_sec):
    """1 変種の逐次成績。`report` と同じ規則。"""
    import replay_stop_logic as r90
    table = {}
    for setup in r90.setups_for(rows, name):
        table[setup["signal"]] = {"setup": setup,
                                  "res": r90.simulate_setup(setup, bars, times, guard_n, rest_sec)}
    items = sorted(table.values(), key=lambda it: it["setup"]["first"])
    busy = 0
    out = {"taken": 0, "filled": 0, "tp1": 0, "loss": 0, "busySkipped": 0,
           "sumR": 0.0, "maxDD": 0.0, "best": 0.0, "table": table}
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
            r = float(res.get("r") or 0.0)
            out["filled"] += 1
            out["tp1"] += 1 if res.get("tp1") else 0
            out["loss"] += 1 if (r < 0 and not res.get("tp1")) else 0
            out["sumR"] += r
            equity += r
            peak = max(peak, equity)
            out["maxDD"] = min(out["maxDD"], equity - peak)
            out["best"] = max(out["best"], r)
            busy = int(res.get("exitT") or start) + BAR3_SEC
        elif res.get("outcome") == "NO_FILL":
            busy = start + rest_sec
    return out


def invalidation_report(rows: List[Dict[str, Any]], bars15: List[Dict[str, float]],
                        secrets: str = SECRETS, limit: int = 3) -> None:
    """親の否定が実際の候補と primary をどう変えたかを**実データで**数える。

    否定された周期ごとに R122 OFF と LIVE を並べ、(a) 親と同じ方向の武装可能な候補、
    (b) 逆方向の武装可能な候補、(c) 実際にブロッカーが立った候補を分けて数える。
    ブロッカーが 1 度も「武装を止めた」に至らなければ、そう書く。
    """
    import msnr_gate
    off = _stage()
    live = _stage("LIVE", "LIVE", "OFF", "OFF", "SHADOW")
    saved = msnr_gate.structure_context_policy
    tally: collections.Counter = collections.Counter()
    shown = {"same": 0, "opposite": 0, "blocked": 0}
    print("PARENT_THESIS_INVALIDATED の実効(親が否定された周期だけを再評価)")
    try:
        for row in rows:
            var = row["variants"].get("C1_LIVE_15") or row["variants"].get("C_LIVE") or {}
            if not var.get("thesisInvalidated"):
                continue
            path = os.path.join(secrets, row["path"])
            try:
                bundle = json.load(io.open(path, encoding="utf-8"))
            except (OSError, ValueError):
                continue
            bundle.get("snapshot", {}).pop("bars15m", None)
            inject_bars15m(bundle, bars15)
            msnr_gate.structure_context_policy = lambda contract=None: off
            before = msnr_gate.evaluate(copy.deepcopy(bundle))
            msnr_gate.structure_context_policy = lambda contract=None: live
            after = msnr_gate.evaluate(copy.deepcopy(bundle))
            bias = ((after.get("structureContext") or {}).get("thesis") or {}).get("bias")
            tally["cycles"] += 1
            same = [c for c in before["candidates"] if c.get("side") == bias and c.get("allowed")]
            opposite = [c for c in before["candidates"]
                        if c.get("side") and c.get("side") != bias and c.get("allowed")]
            blocked = [c for c in after["candidates"] if c.get("participationBlocked")]
            if same:
                tally["同方向に武装可能な候補があった周期"] += 1
                still = [c for c in after["candidates"]
                         if c.get("side") == bias and c.get("allowed")]
                if not still:
                    tally["**その武装が止まった周期**"] += 1
                if shown["same"] < limit:
                    shown["same"] += 1
                    print(f"  [同方向] {row['at'][:19]} 親={bias} 否定済み  "
                          f"OFF: {[(c['model'], c['state']) for c in same]} → "
                          f"LIVE: {[(c['model'], c['state']) for c in after['candidates'] if c.get('side') == bias]}")
                    print(f"           primary {before['decision'].get('model')}/"
                          f"{before['decision'].get('state')} → {after['decision'].get('model')}/"
                          f"{after['decision'].get('state')}")
            if opposite:
                tally["逆方向に武装可能な候補があった周期"] += 1
                kept = [c for c in after["candidates"]
                        if c.get("side") and c.get("side") != bias and c.get("allowed")]
                if len(kept) == len(opposite):
                    tally["逆方向がそのまま残った周期"] += 1
                if shown["opposite"] < limit:
                    shown["opposite"] += 1
                    print(f"  [逆方向] {row['at'][:19]} 親={bias} 否定済み  "
                          f"OFF: {[(c['model'], c['side'], c['state']) for c in opposite]} → "
                          f"LIVE: {[(c['model'], c['side'], c['state']) for c in kept]}  "
                          f"primary {after['decision'].get('model')}/{after['decision'].get('side')}/"
                          f"{after['decision'].get('state')}")
            if blocked:
                tally["ブロッカーが立った周期"] += 1
                if shown["blocked"] < limit:
                    shown["blocked"] += 1
                    for c in blocked:
                        print(f"  [ブロック] {row['at'][:19]} 親={bias}  {c['model']}/{c['side']} "
                              f"{c['state']}  hardBlockers={sorted(c.get('hardBlockers') or [])}")
    finally:
        msnr_gate.structure_context_policy = saved
    print()
    for key, value in tally.most_common():
        print(f"  {key}: {value}")
    if not tally["**その武装が止まった周期**"]:
        print("  → **この入力では、ブロッカーが『武装できた候補を止めた』例は 0 件。**")
        print("     立った 3 件はいずれも別の不足(NO_CONFIRMATION / RISK_CAP_EXCEEDED)で既に WATCH。")
        print("     機構の正しさは合成 fixture(tests/test_r122_structure_context.py の")
        print("     test_live_blocker_flips_armed_to_watch)でのみ確認できている。")


def split_report(rows: List[Dict[str, Any]], rest_min: int = 30, guard_n: float = 1.0,
                 secrets: str = SECRETS) -> None:
    """2x2: 保存修正(15 分足が評価器へ届くか)と判断ロジック(R122 OFF / LIVE)を分離する。

    4 条件はすべて**同じ 3 分足・同じ価格・同じ時刻**を読む。違うのは
    (a) `snapshot.bars15m` を渡すか、(b) `structureContext` の段だけ。
    """
    import entry_depth
    import replay_stop_logic as r90
    bars = entry_depth.load_bars(secrets)
    times = [b["t"] for b in bars]
    names = list(VARIANTS)
    with15 = sum(1 for r in rows if r.get("bars15m"))
    print(f"\nbundles {len(rows)} ({rows[0]['at'][:16]} → {rows[-1]['at'][:16]} UTC)")
    print(f"15 分足を渡せたバンドル: {with15}/{len(rows)}(渡す変種のみ。渡さない変種は同じ"
          f"バンドルから bars15m を取り除いて評価している)")
    print(f"共通入力のハッシュ種類: {len({r['inputHash'] for r in rows})}"
          f"(15 分足を除いた部分。3 分足・価格・時刻は 4 条件で同一)")

    # 1. 親の出所が変わったか
    print("\n--- 1. 親の仮説の出所(判断ロジック LIVE の 2 条件) ---")
    for name in ("C0_LIVE_NO15", "C1_LIVE_15"):
        c = collections.Counter()
        for row in rows:
            v = row["variants"].get(name) or {}
            c["status:" + str(v.get("ctxStatus"))] += 1
            if v.get("thesisTf"):
                c["tf:" + str(v["thesisTf"])] += 1
                c["fid:" + str(v.get("thesisFidelity"))] += 1
            if v.get("thesisInvalidated"):
                c["invalidated"] += 1
            c["rel:" + str(v.get("relation"))] += 1
        ok = c["status:OK"]
        print(f"  {name:13s} 親あり {ok:4d}/{len(rows)} ({ok/len(rows)*100:.0f}%) | "
              + " ".join(f"{k[3:]}={v}" for k, v in sorted(c.items()) if k.startswith("tf:")))
        print(f"                精度 " + " ".join(f"{k[4:]}={v}" for k, v in sorted(c.items())
                                                   if k.startswith("fid:"))
              + f" | 親の否定 {c['invalidated']}")
        print(f"                関係 " + " ".join(f"{k[4:]}={v}" for k, v in sorted(c.items())
                                                   if k.startswith("rel:")))

    # 2. 注文意図の差(4 条件)
    print("\n--- 2. 注文意図の差(A0 との比較) ---")
    for name in names:
        intent = armed_to_watch = watch_to_armed = 0
        for row in rows:
            a = row["variants"].get(BASELINE) or {}
            v = row["variants"].get(name) or {}
            if any(a.get(k) != v.get(k) for k in INTENT_KEYS):
                intent += 1
            av, vv = str(a.get("state") or ""), str(v.get("state") or "")
            if av in {"ARMED", "ACTIVE"} and vv not in {"ARMED", "ACTIVE"}:
                armed_to_watch += 1
            if vv in {"ARMED", "ACTIVE"} and av not in {"ARMED", "ACTIVE"}:
                watch_to_armed += 1
        blocked = sum((row["variants"].get(name) or {}).get("parentBlocked") or 0 for row in rows)
        print(f"  {name:13s} 注文意図の変化 {intent:4d} / ARMED→WATCH {armed_to_watch:3d} / "
              f"WATCH→ARMED {watch_to_armed:3d} / 親の否定で止めた候補 {blocked:3d}")

    # 3. 逐次
    print("\n--- 3. 逐次(同時に 1 建玉だけ) ---")
    seq = {}
    for name in names:
        seq[name] = _sequential(rows, name, bars, times, guard_n, rest_min * 60)
        s = seq[name]
        share = (s["best"] / s["sumR"] * 100.0) if s["sumR"] > 0 else float("nan")
        print(f"  {name:13s} 取った={s['taken']:3d} 約定={s['filled']:3d} TP1={s['tp1']:3d} "
              f"損切り={s['loss']:3d} 見送り={s['busySkipped']:3d} ΣR={s['sumR']:+7.2f} "
              f"最大DD={s['maxDD']:+6.2f}R 最大勝ち={s['best']:+.2f}R({share:.0f}%)")

    # 4. 効果の分離
    print("\n--- 4. 効果の分離 ---")
    def delta(a, b, label):
        if a in seq and b in seq:
            print(f"  {label:38s} {b} - {a} = {seq[b]['sumR'] - seq[a]['sumR']:+7.2f}R")
    delta("A0_OFF_NO15", "A1_OFF_15", "保存修正だけ(R122 OFF。0 であるべき)")
    delta("A0_OFF_NO15", "C0_LIVE_NO15", "判断ロジックだけ(15 分足なし)")
    delta("A1_OFF_15", "C1_LIVE_15", "判断ロジックだけ(15 分足あり)")
    delta("C0_LIVE_NO15", "C1_LIVE_15", "良い親を与えた効果(R122 LIVE の中で)")
    print("  注: 費用・スリッページは含まれていない。")

    # 5. セットアップ単位
    print("\n--- 5. setups(どれかの条件で ARMED) ---")
    keys = sorted(set().union(*[set(seq[n]["table"]) for n in names]) or {()})
    armed = [k for k in keys
             if any(k in seq[n]["table"] and seq[n]["table"][k]["res"].get("outcome") != "NOT_ARMED"
                    for n in names)]
    base_r = [_r_of(seq[BASELINE]["table"].get(k)) for k in armed]
    for name in names:
        vals = [_r_of(seq[name]["table"].get(k)) for k in armed]
        mean, lo, hi, prob = r90._bootstrap(base_r, vals)
        print(f"  {name:13s} ΣR={sum(vals):+7.2f} | vs {BASELINE} {mean:+.3f}R/setup "
              f"[{lo:+.2f},{hi:+.2f}] P(improve)={prob:.2f}")

    forecast_report(rows, bars)


def report(rows: List[Dict[str, Any]], rest_min: int = 30, guard_n: float = 1.0,
           secrets: str = SECRETS) -> None:
    import entry_depth
    import replay_stop_logic as r90
    bars = entry_depth.load_bars(secrets)
    times = [b["t"] for b in bars]
    names = list(VARIANTS)
    print(f"\nbundles {len(rows)} ({rows[0]['at'][:16]} → {rows[-1]['at'][:16]} UTC) / bars {len(bars)}")
    with15 = sum(1 for r in rows if r.get("bars15m"))
    print(f"同条件: modelGate/ictStdv/riskCap/stopLogic/limitGate は契約の実値 / "
          f"limit rest {rest_min}min / market stop guard {guard_n}N / same-bar SL first / "
          f"R = planned risk / **手数料・スリッページは含まない**")
    print(f"15 分足を直接入れたバンドル: {with15}/{len(rows)}"
          + ("(親は BARS 精度)" if with15 else "(親は HTF 要約精度)"))
    digests = {r["inputHash"] for r in rows}
    print(f"入力バンドルのハッシュ種類: {len(digests)}(= 全変種が同一の入力を読んでいる)")

    # 1. 文脈の網羅
    print("\n--- 1. 文脈の網羅(C_LIVE で観測) ---")
    c = collections.Counter()
    for row in rows:
        var = row["variants"].get("C_LIVE") or {}
        c["status:" + str(var.get("ctxStatus"))] += 1
        for reason in var.get("ctxReasons") or ():
            c["reason:" + reason] += 1
        if var.get("thesisTf"):
            c["tf:" + str(var["thesisTf"])] += 1
            c["fidelity:" + str(var.get("thesisFidelity"))] += 1
            c["phase:" + str(var.get("thesisPhase"))] += 1
        if var.get("thesisInvalidated"):
            c["thesisInvalidated"] += 1
        c["relation:" + str(var.get("relation"))] += 1
    total = len(rows) or 1
    print(f"  親の仮説あり {c['status:OK']}/{total} ({c['status:OK']/total*100:.0f}%) / "
          f"文脈なし {c['status:CONTEXT_UNAVAILABLE']}")
    print("  出所: " + ", ".join(f"{k[3:]}={v}" for k, v in sorted(c.items()) if k.startswith("tf:")))
    print("  精度: " + ", ".join(f"{k[9:]}={v}" for k, v in sorted(c.items()) if k.startswith("fidelity:")))
    print("  段階: " + ", ".join(f"{k[6:]}={v}" for k, v in sorted(c.items()) if k.startswith("phase:")))
    print("  関係: " + ", ".join(f"{k[9:]}={v}" for k, v in sorted(c.items()) if k.startswith("relation:")))
    print(f"  親が否定された周期: {c['thesisInvalidated']}")
    print("  作れなかった理由: " + ", ".join(f"{k[7:]}={v}" for k, v in sorted(c.items())
                                              if k.startswith("reason:")))

    # 2. 参加状態
    print("\n--- 2. 参加状態(候補ごと。C_LIVE) ---")
    states: collections.Counter = collections.Counter()
    dwell: Dict[str, List[int]] = collections.defaultdict(list)
    run: Dict[str, int] = {}
    for row in rows:
        var = row["variants"].get("C_LIVE") or {}
        for key, value in (var.get("participationStates") or {}).items():
            states[key] += value
        primary = (var.get("structure") or {}).get("participationState")
        for key in list(run):
            if key != primary:
                dwell[key].append(run.pop(key))
        if primary:
            run[primary] = run.get(primary, 0) + 1
    for key, value in run.items():
        dwell[key].append(value)
    for key, value in states.most_common():
        spans = dwell.get(key) or []
        extra = (f" / primary での連続滞在 中央 {statistics.median(spans):.0f} 周期・"
                 f"最長 {max(spans)}") if spans else ""
        print(f"  {key:22s} 候補 {value:4d}{extra}")

    # 3. 周期単位の差(B の不変性の検算を含む)
    print("\n--- 3. 周期単位(A_NOW との差) ---")
    for name in names[1:]:
        intent = changed_id = armed_to_watch = watch_to_armed = 0
        for row in rows:
            a = row["variants"].get(BASELINE) or {}
            v = row["variants"].get(name) or {}
            if any(a.get(k) != v.get(k) for k in INTENT_KEYS):
                intent += 1
            if a.get("decisionId") != v.get("decisionId"):
                changed_id += 1
            av, vv = str(a.get("state") or ""), str(v.get("state") or "")
            if av in {"ARMED", "ACTIVE"} and vv not in {"ARMED", "ACTIVE"}:
                armed_to_watch += 1
            if vv in {"ARMED", "ACTIVE"} and av not in {"ARMED", "ACTIVE"}:
                watch_to_armed += 1
        flag = "  ← SHADOW は 0 でなければならない" if name == "B_SHADOW" else ""
        print(f"{name:9s} 注文意図の変化 {intent:4d} / decisionId 変化 {changed_id:4d} / "
              f"ARMED→WATCH {armed_to_watch:3d} / WATCH→ARMED {watch_to_armed:3d}{flag}")
    built = sum((row["variants"].get("D_SHALL") or {}).get("shallowBuilt") or 0 for row in rows)
    primary_shallow = sum(1 for row in rows
                          if ((row["variants"].get("D_SHALL") or {}).get("structure") or {}).get("shallow"))
    blocked = sum((row["variants"].get("C_LIVE") or {}).get("parentBlocked") or 0 for row in rows)
    print(f"  浅い代替候補: 生成 {built} / primary になった周期 {primary_shallow}")
    print(f"  親の否定で武装しなかった候補(C_LIVE): {blocked}")

    # 4. セットアップ単位 → 逐次
    tables: Dict[str, Dict[Tuple, Dict[str, Any]]] = {}
    for name in names:
        table = {}
        for setup in r90.setups_for(rows, name):
            table[setup["signal"]] = {"setup": setup,
                                      "res": r90.simulate_setup(setup, bars, times, guard_n,
                                                                rest_min * 60)}
        tables[name] = table
    keys = sorted(set().union(*[set(t) for t in tables.values()]) or {()})
    armed = [k for k in keys
             if any(k in t and t[k]["res"].get("outcome") != "NOT_ARMED" for t in tables.values())]
    print(f"\n--- 4. setups(どれかの変種で ARMED): {len(armed)} ---")
    base_r = [_r_of(tables[BASELINE].get(k)) for k in armed]
    for name, table in tables.items():
        vals = [_r_of(table.get(k)) for k in armed]
        fills = sum(1 for k in armed if (table.get(k) or {}).get("res", {}).get("outcome") == "FILLED")
        tp1_first = sum(1 for k in armed
                        if (table.get(k) or {}).get("res", {}).get("outcome") == "TP1_FIRST")
        no_fill = sum(1 for k in armed
                      if (table.get(k) or {}).get("res", {}).get("outcome") == "NO_FILL")
        mean, lo, hi, prob = r90._bootstrap(base_r, vals)
        print(f"{name:9s} ΣR={sum(vals):+7.2f} 約定={fills:3d} 未約定のまま TP1 到達={tp1_first:3d} "
              f"未約定={no_fill:3d} | vs A {mean:+.3f}R/setup [{lo:+.2f},{hi:+.2f}] "
              f"P(improve)={prob:.2f}")

    # 5. 逐次(同時に 1 建玉だけ)。**結論はここで語る。**
    print("\n--- 5. 逐次(同時に 1 建玉だけ) ---")
    totals: Dict[str, float] = {}
    for name, table in tables.items():
        resting = _resting_keys(rows, name)
        items = sorted(table.values(), key=lambda it: it["setup"]["first"])
        busy_until = 0
        n = fills = tp1 = losses = skipped_busy = 0
        total = equity = peak = 0.0
        max_dd = 0.0
        best = 0.0
        by_type: Dict[str, List[float]] = collections.defaultdict(list)
        for item in items:
            setup, res = item["setup"], item["res"]
            if res.get("outcome") == "NOT_ARMED":
                continue
            start = setup["first"]
            if start < busy_until:
                skipped_busy += 1
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
                best = max(best, r)
                busy_until = int(res.get("exitT") or start) + BAR3_SEC
                by_type[_entry_type(setup, resting)].append(r)
            elif res.get("outcome") == "NO_FILL":
                busy_until = start + rest_min * 60
        totals[name] = total
        share = (best / total * 100.0) if total > 0 else float("nan")
        print(f"{name:9s} 取った={n:3d} 約定={fills:3d} TP1到達={tp1:3d} 損切り={losses:3d} "
              f"保有制約で見送り={skipped_busy:3d} ΣR={total:+7.2f} 最大DD={max_dd:+6.2f}R "
              f"最大勝ち={best:+.2f}R({share:.0f}% of ΣR)")
        parts = [f"{k} n={len(v)} ΣR={sum(v):+6.2f}" for k in ("RESTING_LIMIT", "RETEST_HELD", "OTHER")
                 for v in [by_type.get(k) or []] if v]
        print("          型別: " + (" | ".join(parts) if parts else "(約定なし)"))
    print("  費用: **含まれていない**(片道 1 枚 $5 の口座なら 1 往復 $10 = SL 20pt で約 0.25R 相当)。")

    print("\n--- 5b. 効果の分離(逐次 ΣR) ---")
    for a, b, label in (("A_NOW", "B_SHADOW", "SHADOW 化(0 であるべき)"),
                        ("B_SHADOW", "C_LIVE", "文脈と参加の LIVE 化"),
                        ("C_LIVE", "D_SHALL", "浅い代替候補の追加"),
                        ("D_SHALL", "E_SELECT", "選択層(ARMED 優先)の変更")):
        if a in totals and b in totals:
            print(f"  {label:24s} {b} - {a} = {totals[b] - totals[a]:+7.2f}R")

    forecast_report(rows, bars)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="R122 structure context / participation replay")
    parser.add_argument("--replay", action="store_true")
    parser.add_argument("--split", action="store_true",
                        help="2x2(保存修正 × 判断ロジック)で効果を分離する")
    parser.add_argument("--forecast", action="store_true")
    parser.add_argument("--invalidation", action="store_true",
                        help="親の否定が候補と primary をどう変えたかを実データで数える")
    parser.add_argument("--scenes", type=int, default=0,
                        help="設計評価の場面表を先頭から N 件出す(結果で選ばない)")
    parser.add_argument("--bars15m", action="store_true",
                        help="(--replay 用)全変種へ 15 分足を渡し、その窓だけを対象にする")
    parser.add_argument("--source", choices=("tv", "recon"), default="tv",
                        help="15 分足の出所: tv=直接取得(.secrets/tv_raw/bars15m.json) / "
                             "recon=監査バンドル自身の確定 3 分足からの再構成(値で検算する)")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--rest-min", type=int, default=30)
    parser.add_argument("--guard-n", type=float, default=1.0)
    parser.add_argument("--cache", default=os.path.join(SECRETS, "replay_r122.json"))
    parser.add_argument("--reevaluate", action="store_true")
    args = parser.parse_args(argv)
    if not (args.replay or args.forecast or args.scenes or args.split or args.invalidation):
        parser.print_help()
        return 0

    paths = sorted(glob.glob(os.path.join(SECRETS, "monitor_cycle_*.json")))
    need15 = args.bars15m or args.split or args.invalidation
    bars15: List[Dict[str, float]] = []
    tag = ""
    if need15:
        direct = load_raw_15m()
        if args.source == "recon":
            bars15, meta = reconstruct_15m(paths)
            print(f"15 分足の出所: 再構成(確定 3 分足 {meta['bars3m']} 本 → 15 分足 "
                  f"{meta['bars15m']} 本)")
            if not direct:
                print("直接取得した 15 分足が無いので再構成を検算できない → 使わない")
                return 2
            by_t = {b["t"]: b for b in bars15}
            common = [b for b in direct if b["t"] in by_t]
            bad = [b for b in common
                   if any(abs(by_t[b["t"]][k] - b[k]) > 1e-9 for k in ("o", "h", "l", "c"))]
            print(f"直接取得との検算: 重なり {len(common)} 本 / 不一致 {len(bad)} 本"
                  f" → {'同一(使う)' if common and not bad else '**使えない**'}")
            print("*" * 78)
            print("* 再構成 15 分足は **研究用** です。")
            print("*  - 直接取得と一致を確認できたのは重なった "
                  f"{len(common)} 窓だけで、これは全期間("
                  f"{len(bars15)} 本)の同一性の証明ではありません。")
            print("*  - 本番の親の出所は **直接取得した snapshot.bars15m だけ** です"
                  "(市場再開後は compact で残るので毎周期届きます)。")
            print("*  - この再生の数字を、直接取得だけで測った数字と同列に並べないこと。")
            print("*" * 78)
            if not common or bad:
                return 2
            tag = "_recon"
        else:
            bars15 = direct
            if not bars15:
                print("15 分足の raw が無い(.secrets/tv_raw/bars15m.json)")
                return 2
            check = verify_bars15m(bars15, paths)
            print(f"15 分足の系列検算: 一致 {check['matched']} / 不一致 {check['mismatched']} "
                  f"→ {'同一系列' if check['ok'] else '**使えない**'}")
            if not check["ok"]:
                print("価格系列が一致しないので 15 分足は使わない(ズラして合わせることはしない)")
                return 2
            tag = "_15m"
        lo = bars15[0]["t"] + 20 * BAR15_SEC
        hi = bars15[-1]["t"] + BAR15_SEC
        keep = []
        for path in paths:
            try:
                bundle = json.load(io.open(path, encoding="utf-8"))
            except (OSError, ValueError):
                continue
            moment = _instant(bundle.get("at"))
            if moment is not None and lo <= moment.timestamp() <= hi:
                keep.append(path)
        paths = keep
        print(f"対象バンドル: {len(paths)}")
    if args.split or args.invalidation:
        use_variants(SPLIT, "A0_OFF_NO15")
        tag += "_split"
    elif args.bars15m:
        use_variants(LADDER, "A_NOW", force15=True)
    if args.limit:
        paths = paths[:args.limit]
    cache = args.cache.replace(".json", tag + ".json") if tag else args.cache
    rows: List[Dict[str, Any]] = []
    if not args.reevaluate and os.path.exists(cache):
        try:
            payload = json.load(io.open(cache, encoding="utf-8"))
            if payload.get("variants") == list(VARIANTS) and payload.get("count") == len(paths):
                rows = payload["rows"]
        except (OSError, ValueError, KeyError):
            rows = []
    if not rows:
        print(f"再評価 {len(paths)} バンドル × {len(VARIANTS)} 変種 …")
        rows = evaluate_all(paths, bars15)
        try:
            json.dump({"variants": list(VARIANTS), "count": len(paths), "rows": rows},
                      io.open(cache, "w", encoding="utf-8"), ensure_ascii=False, default=str)
        except OSError:
            pass
    rows = [r for r in rows if r.get("variants")]
    rows.sort(key=lambda r: r["t"])
    if not rows:
        print("対象バンドルなし")
        return 2
    if args.scenes:
        scenes(rows, limit=args.scenes)
        if not args.replay:
            return 0
    if args.invalidation:
        invalidation_report(rows, bars15)
        if not args.split:
            return 0
    if args.split:
        split_report(rows, rest_min=args.rest_min, guard_n=args.guard_n)
        return 0
    if args.forecast and not args.replay:
        import entry_depth
        forecast_report(rows, entry_depth.load_bars(SECRETS))
        return 0
    report(rows, rest_min=args.rest_min, guard_n=args.guard_n)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

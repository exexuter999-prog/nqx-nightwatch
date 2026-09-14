# -*- coding: utf-8 -*-
"""公開されたが発注しなかったシナリオを「架空トレード」として結果まで記録する(R55)。

なぜ要るか
----------
2026-09-07 は日次上限($1,500 = 一貫性ルール 50% × 利益目標 $3,000)に当たった時点で
AUTO を切った。その後も監視ループはシナリオを出し続けていたが、1 本も送っていない。
「出たけど取らなかった形が、取っていたらどうなったか」を残さないと、見送りの機会費用も
モデルの実力も測れない。

**この台帳は実弾とは完全に分ける。** `.secrets/paper_trades.jsonl` に書き、
`model_scorecard.jsonl`(R48 が「モデル別 PF の唯一の正本」と定めた実測)には一切混ぜない。
架空の勝ちを PF に混ぜたら、その正本の意味が消える。

入力
----
- 監査バンドル `.secrets/monitor_cycle_HHMM.json` の `scenarios.primary`
  (ファイル名は日を跨いで衝突するので `at` で取引日を選ぶ)
- 確定 3 分足 `.secrets/tv_raw/bars3m.json`(240 本 = 12 時間)+ 各バンドルの `snapshot.bars3m`

同じ形の数え方
--------------
`scenarioId` は**毎サイクル変わる**(2026-09-07 は 67 周期で 52 個)。SL がボラ床に合わせて
毎回動くためで、これを 1 件ずつ数えると同じ形を 10 回数えることになる。
連続する周期を `(model, side, entry)` でまとめて **1 セットアップ = 1 架空トレード**とし、
プランは**最初に出た周期のもの**を凍結する(その時点で入っていたら、という仮定)。

約定と決済の仮定(推測を混ぜないための規則)
------------------------------------------
- 約定: 公開時刻より **後**の確定足で、BUY は安値 ≤ 建値 / SELL は高値 ≥ 建値。指値で建値ちょうど。
- 同じ足が SL と TP1 の両方に触れたら **SL を先**に取る(足の中の順序は分からない)。
  その件は `ambiguous: true` を立てる。
- TP1 到達後は runner の SL を**建値**へ寄せる(CLAUDE.md §4.2 と同じ)。
- 期限は約定から 4 時間。届かなければ最終足の終値で評価し `OPEN` と記録する。
- 建玉が付かなければ `NO_FILL`。**その場合も 1 行残す**(出たのに触れなかった、が事実)。

損益は **pt と R を正本**にする。USD はプランの基準枚数(通常 2 枚)で参考値として出すだけで、
ULTRA の枚数は口座状態に依存するのでここでは復元しない。

    python paper_trades.py                 # 直近の取引日を集計して表示(書き込みなし)
    python paper_trades.py --write         # .secrets/paper_trades.jsonl へ追記(重複は上書き)
    python paper_trades.py --date 2026-09-07
"""
from __future__ import annotations

import argparse
import glob
import io
import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

BASE = os.path.dirname(os.path.abspath(__file__))
AUDIT_GLOB = os.path.join(BASE, ".secrets", "monitor_cycle_*.json")
RAW_BARS = os.path.join(BASE, ".secrets", "tv_raw", "bars3m.json")
PAPER_FILE = os.path.join(BASE, ".secrets", "paper_trades.jsonl")
SCORECARD_FILE = os.path.join(BASE, ".secrets", "model_scorecard.jsonl")
DEFAULT_VAULT = os.path.join(os.path.expanduser("~"), "FLEX")

JST = timezone(timedelta(hours=9))
#: 取引日の境界(JST 07:00)。dayguard と同じ区切り。
DAY_START_HOUR = 7
#: 架空トレードを追う上限。これを越えたら OPEN として終える。
HORIZON_MIN = 240
#: シナリオの有効期限(`nqx_state.build_scenario(ttl_minutes=15)` と同じ)。
#: 建玉が付くのを待つのは「最後にそのシナリオが出た周期 + TTL」まで。ここを長く取ると
#: 2 時間前の形が今の足で約定したことになる(2026-09-07 の 22:09 TURTLE_SOUP がそうなった)。
SCENARIO_TTL_MIN = 15
TICK = 0.25
POINT_VALUE = 2.0
ARMED_STATES = {"ARMED", "ACTIVE"}


# ---------------------------------------------------------------- 読み込み

def _num(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def trading_day(moment: datetime) -> str:
    """JST 07:00 区切りの取引日(dayguard と同じ)。"""
    local = moment.astimezone(JST)
    if local.hour < DAY_START_HOUR:
        local -= timedelta(days=1)
    return local.strftime("%Y-%m-%d")


def _instant(value: Any) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def load_bars(raw_path: str = RAW_BARS, audit_pattern: str = AUDIT_GLOB) -> List[Dict[str, float]]:
    """確定 3 分足を時刻で重複排除して集める。raw と監査バンドルの和を取る。"""
    by_time: Dict[int, Dict[str, float]] = {}

    def add(row: Any) -> None:
        if not isinstance(row, dict):
            return
        stamp = _num(row.get("t") if row.get("t") is not None else row.get("time"))
        high = _num(row.get("h") if row.get("h") is not None else row.get("high"))
        low = _num(row.get("l") if row.get("l") is not None else row.get("low"))
        close = _num(row.get("c") if row.get("c") is not None else row.get("close"))
        if stamp is None or high is None or low is None or close is None:
            return
        by_time[int(stamp)] = {"t": int(stamp), "h": high, "l": low, "c": close}

    try:
        raw = json.load(io.open(raw_path, encoding="utf-8"))
        for row in (raw.get("bars") if isinstance(raw, dict) else raw) or []:
            add(row)
    except (OSError, ValueError):
        pass
    for path in glob.glob(audit_pattern):
        try:
            bundle = json.load(io.open(path, encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for row in ((bundle.get("snapshot") or {}).get("bars3m") or []):
            add(row)
    return [by_time[key] for key in sorted(by_time)]


def load_cycles(day: str, audit_pattern: str = AUDIT_GLOB) -> List[Tuple[datetime, Dict[str, Any]]]:
    """その取引日の監査バンドルを時刻順に返す(ファイル名は日を跨いで衝突する)。"""
    rows: List[Tuple[datetime, Dict[str, Any]]] = []
    for path in glob.glob(audit_pattern):
        try:
            bundle = json.load(io.open(path, encoding="utf-8"))
        except (OSError, ValueError):
            continue
        moment = _instant(bundle.get("at"))
        if moment is None or trading_day(moment) != day:
            continue
        rows.append((moment, bundle))
    rows.sort(key=lambda row: row[0])
    return rows


def traded_scenarios(scorecard_path: str = SCORECARD_FILE) -> set:
    """実弾になった scenarioId。架空側から必ず外す。"""
    ids = set()
    try:
        with io.open(scorecard_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict) and row.get("scenarioId"):
                    ids.add(str(row["scenarioId"]))
    except OSError:
        pass
    return ids


# ---------------------------------------------------------------- まとめ方

def group_setups(cycles: List[Tuple[datetime, Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """連続する周期を (model, side, entry) でまとめて 1 セットアップにする。

    `scenarioId` で数えると同じ形が周期の数だけ増える(SL がボラ床に合わせて毎回動くため)。
    まとめたうえで **最初の周期のプランを凍結**する —— 「出た時点で入っていたら」の仮定。
    """
    setups: List[Dict[str, Any]] = []
    for moment, bundle in cycles:
        plan = (bundle.get("scenarios") or {}).get("primary")
        if not isinstance(plan, dict):
            continue
        entry = _num(plan.get("entry"))
        side = str(plan.get("side") or "").upper()
        model = str(plan.get("model") or "")
        if entry is None or side not in {"BUY", "SELL"} or not model:
            continue
        key = (model, side, entry)
        if setups and setups[-1]["key"] == key:
            setups[-1]["cycles"].append((moment, plan))
            continue
        setups.append({"key": key, "cycles": [(moment, plan)]})
    out: List[Dict[str, Any]] = []
    for setup in setups:
        first_at, first = setup["cycles"][0]
        states = {str(c[1].get("state") or "").upper() for c in setup["cycles"]}
        grades = [str(c[1].get("grade") or "") for c in setup["cycles"] if c[1].get("grade")]
        out.append({
            "publishedAt": first_at,
            "lastAt": setup["cycles"][-1][0],
            "cycles": len(setup["cycles"]),
            "model": setup["key"][0],
            "side": setup["key"][1],
            "entry": setup["key"][2],
            "stop": _num(first.get("stop")),
            "targets": [value for value in
                        (_num(t) for t in (first.get("targets") or [])) if value is not None],
            "qty": int(_num(first.get("qty")) or 2),
            "legs": first.get("legs") if isinstance(first.get("legs"), list) else None,
            "grade": grades[0] if grades else None,
            "bestGrade": _best_grade(grades),
            "armed": bool(states & ARMED_STATES),
            "states": sorted(states),
            "scenarioIds": sorted({str(c[1].get("scenarioId") or c[1].get("decisionId") or "")
                                   for c in setup["cycles"] if c[1].get("scenarioId")
                                   or c[1].get("decisionId")}),
        })
    return out


_GRADE_RANK = {"A+": 3, "A": 2, "B": 1}


def _best_grade(grades: List[str]) -> Optional[str]:
    ranked = [g for g in grades if g in _GRADE_RANK]
    if not ranked:
        return None
    return max(ranked, key=lambda g: _GRADE_RANK[g])


# ---------------------------------------------------------------- 建てて追う

def simulate(setup: Dict[str, Any], bars: List[Dict[str, float]],
             horizon_min: int = HORIZON_MIN,
             ttl_min: int = SCENARIO_TTL_MIN) -> Dict[str, Any]:
    """凍結プランを確定足に当てて結果を出す。推測は入れず、届かなければ届かないと言う。"""
    entry, stop = setup["entry"], setup["stop"]
    if entry is None or stop is None:
        return {"outcome": "NO_PLAN"}
    long_side = setup["side"] == "BUY"
    risk = abs(entry - stop)
    if risk < TICK:
        return {"outcome": "NO_PLAN"}
    published = setup["publishedAt"].timestamp()
    # シナリオが生きている間だけ約定を待つ。最後に出た周期 + TTL が期限。
    expires = setup.get("lastAt", setup["publishedAt"]).timestamp() + ttl_min * 60
    after = [bar for bar in bars if bar["t"] > published]
    if not after:
        return {"outcome": "NO_BARS"}

    fill_index = None
    for index, bar in enumerate(after):
        if bar["t"] > expires:
            break
        if (long_side and bar["l"] <= entry) or (not long_side and bar["h"] >= entry):
            fill_index = index
            break
    if fill_index is None:
        return {"outcome": "NO_FILL", "risk": round(risk, 2)}

    targets = setup["targets"] or []
    tp1 = targets[0] if targets else None
    final = targets[-1] if targets else None
    filled_at = after[fill_index]["t"]
    legs: List[Dict[str, Any]] = []
    ambiguous = False
    runner_stop = stop
    tp1_done = False
    mfe = mae = 0.0

    def favourable(price: float) -> float:
        return (price - entry) if long_side else (entry - price)

    for bar in after[fill_index:]:
        if (bar["t"] - filled_at) > horizon_min * 60:
            break
        mfe = max(mfe, favourable(bar["h"] if long_side else bar["l"]))
        mae = min(mae, favourable(bar["l"] if long_side else bar["h"]))
        hit_stop = (bar["l"] <= runner_stop) if long_side else (bar["h"] >= runner_stop)
        hit_tp1 = tp1 is not None and not tp1_done and (
            (bar["h"] >= tp1) if long_side else (bar["l"] <= tp1))
        hit_final = final is not None and tp1_done and (
            (bar["h"] >= final) if long_side else (bar["l"] <= final))
        if hit_stop and (hit_tp1 or hit_final):
            # 同じ足で両方に触れた。足の中の順序は取れないので不利側を採る。
            ambiguous = True
            hit_tp1 = hit_final = False
        if hit_stop:
            legs.append({"id": "RUNNER" if tp1_done else "SL", "qty": 1 if tp1_done else 2,
                         "exit": runner_stop, "at": bar["t"]})
            break
        if hit_tp1:
            legs.append({"id": "TP1", "qty": 1, "exit": tp1, "at": bar["t"]})
            tp1_done = True
            runner_stop = entry            # 建値保護(CLAUDE.md §4.2)
            if final is None:
                break
            continue
        if hit_final:
            legs.append({"id": "TP2", "qty": 1, "exit": final, "at": bar["t"]})
            break
    else:
        bar = after[-1]
        legs.append({"id": "OPEN", "qty": 1 if tp1_done else 2, "exit": bar["c"], "at": bar["t"]})

    if not legs:
        bar = after[min(len(after) - 1, fill_index)]
        legs.append({"id": "OPEN", "qty": 1 if tp1_done else 2, "exit": bar["c"], "at": bar["t"]})

    total_qty = sum(int(leg["qty"]) for leg in legs)
    points = sum(favourable(float(leg["exit"])) * int(leg["qty"]) for leg in legs) / total_qty
    ids = {leg["id"] for leg in legs}
    if "SL" in ids:
        outcome = "LOSS"
    elif "OPEN" in ids and not tp1_done:
        outcome = "OPEN"
    elif points > 0:
        outcome = "WIN"
    elif points < 0:
        outcome = "LOSS"
    else:
        outcome = "FLAT"
    closed_at = max(int(leg["at"]) for leg in legs)
    return {
        "outcome": outcome,
        "filledAt": datetime.fromtimestamp(filled_at, timezone.utc).isoformat(),
        "closedAt": datetime.fromtimestamp(closed_at, timezone.utc).isoformat(),
        "holdMin": round((closed_at - filled_at) / 60.0, 1),
        "legs": [{**leg, "at": datetime.fromtimestamp(int(leg["at"]),
                                                      timezone.utc).isoformat()} for leg in legs],
        "points": round(points, 2),
        "risk": round(risk, 2),
        "rMultiple": round(points / risk, 3),
        "usdBaseQty": round(points * POINT_VALUE * int(setup["qty"]), 2),
        "mfePt": round(mfe, 2),
        "maePt": round(mae, 2),
        "tp1Reached": tp1_done,
        "ambiguous": ambiguous,
    }


def build(day: str, audit_pattern: str = AUDIT_GLOB, raw_path: str = RAW_BARS,
          scorecard_path: str = SCORECARD_FILE) -> List[Dict[str, Any]]:
    """その取引日の「出たが取らなかった形」を架空トレードとして組み立てる。"""
    cycles = load_cycles(day, audit_pattern)
    bars = load_bars(raw_path, audit_pattern)
    traded = traded_scenarios(scorecard_path)
    rows: List[Dict[str, Any]] = []
    for setup in group_setups(cycles):
        if traded & set(setup["scenarioIds"]):
            continue                        # 実弾になった形は架空にしない
        result = simulate(setup, bars)
        rows.append({
            "paperId": "pp_" + f"{day}_{setup['publishedAt'].astimezone(JST):%H%M}_"
                               f"{setup['model']}_{setup['side']}_{setup['entry']:.2f}",
            "day": day,
            "publishedAt": setup["publishedAt"].isoformat(),
            "publishedJst": setup["publishedAt"].astimezone(JST).strftime("%H:%M"),
            "lastAt": setup["lastAt"].isoformat(),
            "lastSeenJst": setup["lastAt"].astimezone(JST).strftime("%H:%M"),
            "cycles": setup["cycles"],
            "model": setup["model"],
            "grade": setup["bestGrade"],
            "armed": setup["armed"],
            "states": setup["states"],
            "side": setup["side"],
            "qty": setup["qty"],
            "entry": setup["entry"],
            "stop": setup["stop"],
            "targets": setup["targets"],
            "scenarioIds": setup["scenarioIds"],
            **result,
        })
    assign_clusters(rows)
    return rows


# ---------------------------------------------------------------- 集計・保存

#: 同じ形が撃ち直された、と見なす間隔。これ以内に同じ (model, side) が再掲されたら 1 つの塊。
CLUSTER_GAP_MIN = 20


def assign_clusters(rows: List[Dict[str, Any]], gap_min: int = CLUSTER_GAP_MIN) -> None:
    """相関する再掲をまとめて数えられるようにする(`clusterId` を破壊的に付ける)。

    同じ形が SL だけ動かして毎周期出るので、素で数えると 7 分間の VP80 SELL が 3 件になる。
    R の合計を「独立した N 件の実験」と読むのは嘘になるので、**塊の代表 1 件**でも数えられる
    ようにしておく。どちらの見方も残し、片方へ丸めない。
    """
    ordered = sorted(rows, key=lambda row: row["publishedAt"])
    last: Dict[Tuple[str, str], Tuple[str, datetime]] = {}
    for row in ordered:
        key = (row["model"], row["side"])
        moment = _instant(row["publishedAt"])
        previous = last.get(key)
        if previous and moment and (moment - previous[1]).total_seconds() <= gap_min * 60:
            row["clusterId"], row["clusterFirst"] = previous[0], False
        else:
            row["clusterId"], row["clusterFirst"] = row["paperId"], True
        if moment:
            last[key] = (row["clusterId"], moment)


def summarize(rows: List[Dict[str, Any]], armed_only: bool = True,
              cluster_only: bool = False) -> Dict[str, Any]:
    """架空トレードの集計。PF は損失が無い間は None(∞ を発明しない)。"""
    used = [row for row in rows if row.get("armed")] if armed_only else list(rows)
    if cluster_only:
        used = [row for row in used if row.get("clusterFirst", True)]
    filled = [row for row in used if row.get("outcome") in {"WIN", "LOSS", "FLAT", "OPEN"}]
    wins = [row for row in filled if row["outcome"] == "WIN"]
    losses = [row for row in filled if row["outcome"] == "LOSS"]
    gross_win = sum(row["rMultiple"] for row in wins)
    gross_loss = abs(sum(row["rMultiple"] for row in losses))
    return {
        "setups": len(used),
        "noFill": sum(1 for row in used if row.get("outcome") == "NO_FILL"),
        "filled": len(filled),
        "wins": len(wins),
        "losses": len(losses),
        "open": sum(1 for row in filled if row["outcome"] == "OPEN"),
        "ambiguous": sum(1 for row in filled if row.get("ambiguous")),
        "rSum": round(sum(row["rMultiple"] for row in filled), 2),
        "rAvg": round(sum(row["rMultiple"] for row in filled) / len(filled), 3) if filled else None,
        "winRate": round(len(wins) / len(filled), 3) if filled else None,
        "pf": round(gross_win / gross_loss, 2) if gross_loss > 1e-9 else None,
    }


def write_rows(rows: List[Dict[str, Any]], path: str = PAPER_FILE) -> int:
    """`paperId` で置き換えて保存する。**実弾の台帳とは別ファイル**。"""
    existing: Dict[str, Dict[str, Any]] = {}
    try:
        with io.open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict) and row.get("paperId"):
                    existing[str(row["paperId"])] = row
    except OSError:
        pass
    for row in rows:
        existing[str(row["paperId"])] = row
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with io.open(path, "w", encoding="utf-8", newline="\n") as fh:
        for key in sorted(existing):
            fh.write(json.dumps(existing[key], ensure_ascii=False) + "\n")
    return len(rows)


#: セッション区分(JST)。境界は開始時刻で、次の区分の開始まで。
SESSIONS = ((7, "アジア"), (16, "ロンドン"), (22, "NY"), (5, "深夜"))


def session_of(moment: datetime) -> str:
    hour = moment.astimezone(JST).hour
    if 7 <= hour < 16:
        return "アジア"
    if 16 <= hour < 22:
        return "ロンドン"
    if hour >= 22 or hour < 5:
        return "NY"
    return "深夜"


def sequence_day(rows: List[Dict[str, Any]],
                 ttl_min: int = SCENARIO_TTL_MIN) -> List[Dict[str, Any]]:
    """時系列で 1 本ずつ回す。**同時に 2 つ持たない**という現実の制約を入れる。

    再掲を全部数えると相関した実験を独立に数えることになり、塊の代表だけだと今度は
    「その塊の 1 件目が一番いい」という保証が無い。どちらも実際の 1 日ではない。

    実際は **建玉は 1 つずつ**で、前の建玉が閉じるまで次は取れない。時系列に前から
    舐めて、空いているときに出た形だけを取る —— これがその日を 1 回やり直した姿。
    出ている間に出た形は `SKIPPED_BUSY`(取れなかった、が事実)。

    約定しなかった形も、**指値が板にある間(最後に出た周期 + TTL)は次を取れない**
    ものとして塞ぐ(engine は claim を 1 つしか持てない)。
    """
    ordered = sorted((row for row in rows if row.get("armed")),
                     key=lambda row: row["publishedAt"])
    busy_until: Optional[datetime] = None
    out: List[Dict[str, Any]] = []
    for row in ordered:
        published = _instant(row["publishedAt"])
        if published is None:
            continue
        if busy_until is not None and published < busy_until:
            out.append({**row, "sequence": "SKIPPED_BUSY"})
            continue
        closed = _instant(row.get("closedAt"))
        if closed is not None:
            busy_until = closed
        else:
            last = _instant(row.get("lastAt")) or published
            busy_until = last + timedelta(minutes=ttl_min)
        out.append({**row, "sequence": "TAKEN"})
    return out


def sequence_summary(sequenced: List[Dict[str, Any]]) -> Dict[str, Any]:
    """時系列で回した 1 日の姿。累積 R と「最初に TP1 を取るまで」を出す。"""
    taken = [row for row in sequenced if row["sequence"] == "TAKEN"]
    filled = [row for row in taken if row.get("rMultiple") is not None]
    cumulative = 0.0
    first_tp1 = None
    peak = trough = 0.0
    for index, row in enumerate(filled):
        cumulative += row["rMultiple"]
        peak = max(peak, cumulative)
        trough = min(trough, cumulative)
        if first_tp1 is None and row.get("tp1Reached"):
            first_tp1 = index + 1
    wins = [row for row in filled if row["outcome"] == "WIN"]
    losses = [row for row in filled if row["outcome"] == "LOSS"]
    gross_loss = abs(sum(row["rMultiple"] for row in losses))
    return {
        "taken": len(taken),
        "skippedBusy": sum(1 for row in sequenced if row["sequence"] == "SKIPPED_BUSY"),
        "filled": len(filled),
        "noFill": len(taken) - len(filled),
        "wins": len(wins),
        "losses": len(losses),
        "rSum": round(cumulative, 2),
        "rPeak": round(peak, 2),
        "rTrough": round(trough, 2),
        "firstTp1Index": first_tp1,
        "pf": (round(sum(row["rMultiple"] for row in wins) / gross_loss, 2)
               if gross_loss > 1e-9 else None),
    }


def by_session(sequenced: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """時間帯ごとの内訳(取れた形だけ)。N が小さいので順位付けはしない。"""
    buckets: Dict[str, Dict[str, Any]] = {}
    for row in sequenced:
        if row["sequence"] != "TAKEN" or row.get("rMultiple") is None:
            continue
        moment = _instant(row["publishedAt"])
        if moment is None:
            continue
        bucket = buckets.setdefault(session_of(moment),
                                    {"session": session_of(moment), "n": 0, "wins": 0,
                                     "losses": 0, "rSum": 0.0})
        bucket["n"] += 1
        bucket["rSum"] += row["rMultiple"]
        if row["outcome"] == "WIN":
            bucket["wins"] += 1
        elif row["outcome"] == "LOSS":
            bucket["losses"] += 1
    for bucket in buckets.values():
        bucket["rSum"] = round(bucket["rSum"], 2)
    order = {"アジア": 0, "ロンドン": 1, "NY": 2, "深夜": 3}
    return sorted(buckets.values(), key=lambda b: order.get(b["session"], 9))


def render_note(day: str, rows: List[Dict[str, Any]]) -> str:
    """Obsidian の `Paper/<date>.md` を組み立てる。**Trades/ とは別の場所**に置く。"""
    all_view = summarize(rows, armed_only=True, cluster_only=False)
    cluster_view = summarize(rows, armed_only=True, cluster_only=True)
    armed = [row for row in rows if row.get("armed")]
    lines = [
        "---",
        "type: paper-session",
        f"date: {day}",
        f"setups: {all_view['setups']}",
        f"clusters: {cluster_view['setups']}",
        f"r_sum_all: {all_view['rSum']}",
        f"r_sum_clusters: {cluster_view['rSum']}",
        "tags: [paper, session-auto]",
        "---",
        "",
        f"# {day} 架空トレード(出たが発注しなかった形)",
        "",
        "> `python paper_trades.py --date " + day + " --obsidian` が描き直す。手で編集しない。",
        "> **実弾ではない。** `model_scorecard.jsonl`(モデル別 PF の正本)には一切入れていない。",
        "",
        "## 前提と仮定",
        "",
        "- 対象は等級 A+/A/B かつ **ARMED / ACTIVE** になった形だけ(WATCH 止まりは下の表に `-` 印で載せるが集計外)。",
        "- 約定は公開時刻より後の確定 3 分足で建値に触れたら指値成立。待つのは"
        f"**最後にその形が出た周期 + {SCENARIO_TTL_MIN} 分**(シナリオの TTL)まで。",
        "- 同じ足が SL と TP に触れたら **SL を先**に取る(足の中の順序は取れない)。",
        "- TP1 到達後は runner の SL を建値へ寄せる(実運用と同じ)。",
        f"- 期限は約定から {HORIZON_MIN} 分。届かなければ最終足の終値で `OPEN` として評価。",
        "- 損益は **pt と R が正本**。ULTRA の枚数は口座状態に依存するので復元していない。",
        "- **実弾になった形はこの母集団に入っていない。** つまりここが答えるのは"
        "「取らなかった形だけを、空いているときに前から取っていたらどうなったか」。",
        "",
        "## 集計(2 通りの数え方)",
        "",
        "| 数え方 | 件数 | 建った | 不成立 | 勝 / 負 | 合計 R | 平均 R | PF |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for label, view in (("再掲を全部数える", all_view), ("**塊の代表だけ**", cluster_view)):
        r_avg = "—" if view["rAvg"] is None else f"{view['rAvg']:+.3f}"
        pf = "—" if view["pf"] is None else f"{view['pf']}"
        lines.append(f"| {label} | {view['setups']} | {view['filled']} | {view['noFill']} | "
                     f"{view['wins']} / {view['losses']} | {view['rSum']:+.2f} | {r_avg} | {pf} |")
    lines += [
        "",
        "> 同じ形が SL だけ動かして毎周期出るので、素で数えると相関した再掲を独立試行として"
        "数えてしまう。**塊**は同じ (model, side) が "
        f"{CLUSTER_GAP_MIN} 分以内に再掲されたものをひとまとめにした見方。",
        "> **どちらも N が小さすぎて結論にならない。** 数え方でここまで絵が変わる、という事実の方が今は重要。",
        "",
    ]
    sequenced = sequence_day(rows)
    seq = sequence_summary(sequenced)
    seq_pf = "—" if seq["pf"] is None else f"{seq['pf']}"
    first_tp1 = ("届かなかった" if seq["firstTp1Index"] is None
                 else f"{seq['firstTp1Index']} 本目")
    cap_line = ("TP1 に届く形が無いので上限には届かなかった" if seq["firstTp1Index"] is None
                else f"**{seq['firstTp1Index']} 本目で上限に届いてその日は終わり**")
    lines += [
        "",
        "## 時系列で 1 本ずつ回したら(現実の制約入り)",
        "",
        "上の 2 通りはどちらも「実際の 1 日」ではない。**建玉は 1 つずつ**しか持てないので、"
        "前が閉じるまで次は取れない。時系列に前から舐めて、空いているときに出た形だけを取った姿:",
        "",
        "| 項目 | 値 |",
        "| --- | --- |",
        f"| 取れた | **{seq['taken']} 件**(うち約定 {seq['filled']} · 不成立 {seq['noFill']}) |",
        f"| 建玉・指値が生きていて取れなかった | {seq['skippedBusy']} 件 |",
        f"| 勝 / 負 | {seq['wins']} / {seq['losses']} |",
        f"| 累積 R | **{seq['rSum']:+.2f}R**(最大 {seq['rPeak']:+.2f} / 最小 {seq['rTrough']:+.2f}) |",
        f"| PF | {seq_pf} |",
        f"| 最初に TP1 を取るまで | {first_tp1} |",
        "",
        f"> **ULTRA は「TP1 到達で日次上限に届く枚数」を出す。** つまりこの日を自動で回していたら "
        f"{cap_line}。累積 R の合計ではなく、**最初の TP1 までに何敗するか**が"
        "この口座の実質的な指標になる。",
        "",
        "## 時間帯別(取れた形だけ)",
        "",
        "| 時間帯 | N | 勝 / 負 | 合計 R |",
        "| --- | --- | --- | --- |",
    ]
    for bucket in by_session(sequenced):
        lines.append(f"| {bucket['session']} | {bucket['n']} | "
                     f"{bucket['wins']} / {bucket['losses']} | {bucket['rSum']:+.2f} |")
    sequence_by_id = {row["paperId"]: row["sequence"] for row in sequenced}
    lines += [
        "",
        "> 区分は JST でアジア 07-16 / ロンドン 16-22 / NY 22-05。N が小さいので順位付けはしない。",
        "",
        "## 一覧(JST)",
        "",
        "| 時刻 | 順 | 塊 | モデル | 等 | 方向 | 建値 | SL | 結果 | R | 保有 | MFE/MAE |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in sorted(rows, key=lambda r: r["publishedAt"]):
        mark = "●" if row.get("clusterFirst") else "↳"
        if not row.get("armed"):
            mark = "-"
        r_text = "" if row.get("rMultiple") is None else f"{row['rMultiple']:+.2f}"
        hold = "" if row.get("holdMin") is None else f"{row['holdMin']:.0f}分"
        mfe = "" if row.get("mfePt") is None else f"{row['mfePt']:+.1f}/{row['maePt']:+.1f}"
        note = " (同足)" if row.get("ambiguous") else ""
        order = {"TAKEN": "取", "SKIPPED_BUSY": "×"}.get(
            sequence_by_id.get(row["paperId"], ""), "")
        lines.append(
            f"| {row['publishedJst']} | {order} | {mark} | {row['model']} | "
            f"{row.get('grade') or '-'} | "
            f"{row['side']} | {row['entry']:,.2f} | {row['stop']:,.2f} | "
            f"{row['outcome']}{note} | {r_text} | {hold} | {mfe} |")
    lines += [
        "",
        f"取 = 時系列で実際に取れた · × = 建玉/指値が生きていて取れなかった · "
        f"● = その塊の 1 件目 · ↳ = 同じ形の撃ち直し · "
        f"- = WATCH 止まり(集計外、{len(rows) - len(armed)} 件)",
        "",
        "## 読み方のメモ",
        "",
        "この日は日次上限に当たった後も監視が回り続けていたので、**取らなかったのは正しい**。",
        "ここで測っているのは「取らなかった判断の是非」ではなく、**シナリオそのものの形の質**。",
    ]
    return "\n".join(lines) + "\n"


def write_note(day: str, rows: List[Dict[str, Any]], vault: str) -> str:
    path = os.path.join(vault, "Paper", f"{day}.md")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with io.open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(render_note(day, rows))
    return path


def _latest_day(audit_pattern: str = AUDIT_GLOB) -> Optional[str]:
    best = None
    for path in glob.glob(audit_pattern):
        try:
            bundle = json.load(io.open(path, encoding="utf-8"))
        except (OSError, ValueError):
            continue
        moment = _instant(bundle.get("at"))
        if moment and (best is None or moment > best):
            best = moment
    return trading_day(best) if best else None


def _cli(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="出たが発注しなかったシナリオを架空トレードとして評価する(読み取り専用)")
    parser.add_argument("--date", help="取引日 YYYY-MM-DD(JST 07:00 区切り)")
    parser.add_argument("--write", action="store_true", help=".secrets/paper_trades.jsonl へ保存")
    parser.add_argument("--all", action="store_true", help="WATCH 止まりの形も集計に含める")
    parser.add_argument("--obsidian", metavar="VAULT", nargs="?", const=DEFAULT_VAULT,
                        help="Obsidian の Paper/<date>.md を書き直す")
    args = parser.parse_args(argv)

    day = args.date or _latest_day()
    if not day:
        print("監査バンドルが見つかりません")
        return 1
    rows = build(day)
    print(f"{day}: 架空トレード {len(rows)} 件(うち ARMED {sum(1 for r in rows if r['armed'])})")
    for row in rows:
        if not args.all and not row["armed"]:
            continue
        mark = "*" if row["armed"] else " "
        r = row.get("rMultiple")
        print(f"  {mark}{row['publishedJst']} {row['model'][:22]:22} {row['side']:4} "
              f"E={row['entry']:>9,.2f} SL={row['stop']:>9,.2f} "
              f"{str(row.get('grade') or '-'):2} {row['outcome']:8} "
              f"{'' if r is None else f'{r:+.2f}R'}"
              f"{' (同足)' if row.get('ambiguous') else ''}")
    scope = "ARMED のみ" if not args.all else "全件"
    for label, cluster_only in (("再掲を全部数える", False), ("塊の代表だけ", True)):
        summary = summarize(rows, armed_only=not args.all, cluster_only=cluster_only)
        r_avg = "—" if summary["rAvg"] is None else f"{summary['rAvg']:+.3f}R"
        pf = "—" if summary["pf"] is None else f"{summary['pf']}"
        print(f"\n  {scope} / {label}: {summary['setups']} 件 / 建った {summary['filled']} · "
              f"不成立 {summary['noFill']} · 勝 {summary['wins']} / 負 {summary['losses']} · "
              f"合計 {summary['rSum']:+.2f}R · 平均 {r_avg} · PF {pf}")
    if args.write:
        print(f"  -> {write_rows(rows)} 件を {PAPER_FILE} へ保存")
    if args.obsidian:
        print(f"  -> {write_note(day, rows, args.obsidian)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())

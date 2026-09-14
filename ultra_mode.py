#!/usr/bin/env python3
"""ULTRA mode: per-account position sizing that overrides the signal quantity.

ULTRA OFF
    通常シグナルの枚数をそのまま使う。

ULTRA ON
    通常の枚数は使わない。口座ごとの利益目標と残ドローダウンから必要枚数を
    再計算し、その枚数へ強制的に上書きして口座別に発注する。

必要枚数は「1トレードで利益目標に届く枚数」:

    qty = ceil(profitTarget / (tpPoints * pointValue))

想定損失は同じ枚数を SL 幅に当てたもの:

    projectedLoss = qty * slPoints * pointValue

判定 (``verdict``) は利用者が定義したとおり **利益目標と残ドローダウンだけ**
で決める。1トレードあたりのリスク上限 (``cap``) は ULTRA では意図的に判定に
使わない — ULTRA は定義上その上限を超える枚数を出すモードだから。

このモジュールは純粋計算のみで、発注も外部I/Oも一切行わない。計算結果が
現行の実行契約 (execution_contract.json) を超える場合は ``contractBlockers``
に理由を積む。UI と Bot はそれを見て「口座条件としては ELIGIBLE だが、
実行契約が通さない」状態を利用者に正直に見せる。
"""
from __future__ import annotations

import json
import math
import os
from typing import Any, Dict, Iterable, List, Optional

import execution_contract

ULTRA_VERSION = "ULTRA-MODE/1"

#: MNQ は 1pt = $2.00。契約側と必ず同じ値を使う。
DEFAULT_POINT_VALUE = float(execution_contract.CONTRACT["risk"]["pointValue"])


def _number(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def signal_geometry(signal: Any) -> Dict[str, Any]:
    """Return the SL/TP distances of one signal, or the reason it is unusable.

    方向と価格の整合はここで一度だけ確かめる。捏造も補完もしない。
    """
    if not isinstance(signal, dict):
        return {"valid": False, "reason": "SIGNAL_MISSING"}
    side = str(signal.get("side") or "").upper()
    if side in {"LONG", "BUY"}:
        side = "BUY"
    elif side in {"SHORT", "SELL"}:
        side = "SELL"
    else:
        return {"valid": False, "reason": "SIGNAL_SIDE_INVALID"}
    entry = _number(signal.get("entry"))
    stop = _number(signal.get("stop") if signal.get("stop") is not None else signal.get("sl"))
    target = _number(signal.get("target") if signal.get("target") is not None else signal.get("tp"))
    if entry is None or stop is None or target is None:
        return {"valid": False, "reason": "SIGNAL_PRICES_INCOMPLETE"}
    if side == "BUY" and not (stop < entry < target):
        return {"valid": False, "reason": "SIGNAL_DIRECTION_INVALID"}
    if side == "SELL" and not (target < entry < stop):
        return {"valid": False, "reason": "SIGNAL_DIRECTION_INVALID"}
    sl_points = round(abs(entry - stop), 4)
    tp_points = round(abs(target - entry), 4)
    if sl_points <= 0 or tp_points <= 0:
        return {"valid": False, "reason": "SIGNAL_DISTANCE_INVALID"}
    # runner(2 本目の目標)。分割計画の実際の決済に必要(R30)。
    # 無ければ TP1 と同値とみなす —— 捏造せず、単に分割の恩恵が無いだけ。
    runner_points = tp_points
    targets = signal.get("targets")
    if isinstance(targets, (list, tuple)) and len(targets) >= 2:
        runner_price = _number(targets[1])
        if runner_price is not None:
            span = round(abs(runner_price - entry), 4)
            ok = (runner_price > entry) if side == "BUY" else (runner_price < entry)
            if ok and span > 0:
                runner_points = span
    return {"valid": True, "side": side, "entry": entry, "stop": stop,
            "target": target, "slPoints": sl_points, "tpPoints": tp_points,
            "runnerPoints": runner_points,
            "rr": round(tp_points / sl_points, 2),
            "runnerRr": round(runner_points / sl_points, 2)}


def required_qty(profit_target: Any, tp_points: Any, point_value: float) -> Optional[int]:
    """1トレードで利益目標に届く最小枚数。届かない入力では None。

    **全量が 1 つの目標で決済される**前提の式。実行計画が 2 レグ分割のときは
    前提が崩れるので ``required_qty_split()`` を使う(R30)。
    """
    target = _number(profit_target)
    points = _number(tp_points)
    if target is None or points is None or target <= 0 or points <= 0 or point_value <= 0:
        return None
    per_contract = points * point_value
    if per_contract <= 0:
        return None
    return int(math.ceil(target / per_contract))


def required_qty_split(profit_target: Any, tp1_points: Any, runner_points: Any,
                       point_value: float) -> Optional[int]:
    """2 レグ分割の**実際の決済**で利益目標に届く最小枚数(R30)。

    実行計画は qty を半分ずつ TP1 と runner へ割る。したがって両レグが
    当たったときの利益は

        (qty/2) * TP1_pt * PV  +  (qty/2) * runner_pt * PV

    従来の ``required_qty`` は「全量が TP1 で決済される」前提だったため、
    **計画と矛盾していた**。実測(武装候補 101 本、LUCIDFLEX 25K):

        現行  : 枚数 16、1トレード損失 $688、TP1 だけ当たると目標の **52%**、
                両方当たると **332%**(過大)、残 DD $1,000 に対し試行 **1.5 回**
        本式  : 枚数  6、1トレード損失 $249、両方当たると **129%**(到達)、
                試行 **4.0 回**

    枚数が減るのは過大だったものが是正されるからで、火力を落としているのでは
    ない。**同じ残ドローダウンで試行回数が 2.7 倍**になる。

    偶数へ切り上げるのは、分割が奇数枚を表現できないため
    (``execution_contract.ultra_split``)。
    """
    target = _number(profit_target)
    tp1 = _number(tp1_points)
    runner = _number(runner_points)
    if target is None or target <= 0 or point_value <= 0:
        return None
    if tp1 is None or tp1 <= 0:
        return None
    if runner is None or runner <= 0:
        runner = tp1                      # runner が無ければ両レグ同値とみなす
    per_pair = (tp1 + runner) * point_value      # 2 枚(各レグ 1 枚)ぶんの利益
    if per_pair <= 0:
        return None
    qty = int(math.ceil(2.0 * target / per_pair))
    if qty < 2:
        qty = 2
    if qty % 2:
        qty += 1
    # ここでは上限を当てない。極小 TP のような異常入力では 5000 億枚のような
    # 値になるが、それを止めるのは `_contract_blockers` の
    # `ULTRA_QTY_EXCEEDS_ACCOUNT_MAX` の役目。ここで None にすると
    # `QTY_NOT_COMPUTABLE` に化けて **どの上限で落ちたのかが消える** ——
    # 「口座条件としては ELIGIBLE だが実行契約が通さない」を正直に見せる
    # という設計( モジュール冒頭の docstring )が崩れる。
    return qty


def _contract_blockers(qty: int, projected_loss: float, buffer_value: Optional[float]) -> List[str]:
    """実行契約がこの枚数を通さない理由を列挙する。

    ULTRA が宣言された経路は **ULTRA エンベロープ**で判定する。通常経路の
    2枚固定・$240 はここでは当てない — ULTRA は定義上それを超えるモードで、
    その上限を当てると必ず全口座が routing 不可になる。

    ULTRA が契約で無効化されている場合だけ、通常経路の上限に戻る。
    """
    contract = execution_contract.CONTRACT
    envelope = contract.get("ultra") or {}
    blockers: List[str] = []

    if not envelope.get("enabled"):
        max_qty = int(contract["risk"]["maxQty"])
        if qty > max_qty:
            blockers.append(f"QTY_EXCEEDS_CONTRACT(max={max_qty})")
        fixed_qty = int(contract["risk"]["fixedQty"])
        if qty != fixed_qty:
            blockers.append(f"FIXED_QTY_REQUIRED(fixed={fixed_qty})")
        profile_cap = float(contract["risk"]["defaultCapDollars"])
        if projected_loss > profile_cap + 1e-9:
            blockers.append(f"RISK_CAP_EXCEEDED(cap=${profile_cap:,.2f})")
        blockers.append("ULTRA_DISABLED")
        return blockers

    if qty < int(envelope["minQtyPerAccount"]):
        blockers.append(f"ULTRA_QTY_BELOW_MIN(min={envelope['minQtyPerAccount']})")
    if qty > int(envelope["maxQtyPerAccount"]):
        blockers.append(f"ULTRA_QTY_EXCEEDS_ACCOUNT_MAX(max={envelope['maxQtyPerAccount']})")
    if execution_contract.ultra_split(qty) is None:
        blockers.append("ULTRA_SPLIT_NOT_REPRESENTABLE")
    account_max = float(envelope["maxRiskDollarsPerAccount"])
    if projected_loss > account_max + 1e-9:
        blockers.append(f"ULTRA_ACCOUNT_RISK_EXCEEDS_CONTRACT(max=${account_max:,.2f})")
    # ULTRA のリスク上限の正本は残ドローダウン。1トレード上限 (RISK_*) ではない。
    drawdown = _number(buffer_value)
    if drawdown is None or drawdown <= 0:
        blockers.append("ULTRA_DRAWDOWN_UNAVAILABLE")
    elif projected_loss > drawdown + 1e-9:
        blockers.append(f"ULTRA_DRAWDOWN_EXCEEDED(buffer=${drawdown:,.2f})")
    return blockers


def account_plan(account: Any, geometry: Dict[str, Any], point_value: float) -> Dict[str, Any]:
    """1口座ぶんの ULTRA 枚数と判定。"""
    account = account if isinstance(account, dict) else {}
    account_id = str(account.get("id") or "").strip()
    row: Dict[str, Any] = {
        "id": account_id,
        "label": str(account.get("label") or (f"…{account_id[-4:]}" if account_id else "")),
        "profitTarget": _number(account.get("profitTarget")),
        "buffer": _number(account.get("buffer")),
        "cap": _number(account.get("cap")),
        "qty": None, "projectedProfit": None, "projectedLoss": None,
        "drawdownUsedPct": None, "verdict": "INELIGIBLE", "reasons": [],
        "contractBlockers": [],
        # 早期 return する経路でもキーを欠かさない。ultra.js は初期値に
        # routable:false を持っており、こちらだけ欠けると行の形が食い違う。
        "routable": False,
    }
    if not account_id:
        row["reasons"].append("ACCOUNT_ID_MISSING")
        return row
    if not geometry.get("valid"):
        row["reasons"].append(geometry.get("reason") or "SIGNAL_INVALID")
        return row
    if row["profitTarget"] is None or row["profitTarget"] <= 0:
        row["reasons"].append("PROFIT_TARGET_MISSING")
        return row
    # R30: 分割計画(TP1 + runner を半分ずつ)の実際の決済で目標に届く枚数。
    # 従来は「全量が TP1 で決済」前提で、計画と矛盾していた。
    qty = required_qty_split(row["profitTarget"], geometry["tpPoints"],
                             geometry.get("runnerPoints"), point_value)
    if qty is None or qty < 1:
        row["reasons"].append("QTY_NOT_COMPUTABLE")
        return row
    row["qty"] = qty
    half = qty / 2.0
    runner_points = geometry.get("runnerPoints") or geometry["tpPoints"]
    # 両レグ当たり = 分割計画が完走したときの利益。これが目標に届く枚数を出す。
    row["projectedProfit"] = round(half * (geometry["tpPoints"] + runner_points) * point_value, 2)
    # TP1 だけ当たって runner が建値撤退した場合。**目標には届かない**ことを
    # 隠さず出す(現行式はここが目標の 52% だった)。
    row["profitIfTp1Only"] = round(half * geometry["tpPoints"] * point_value, 2)
    row["projectedLoss"] = round(qty * geometry["slPoints"] * point_value, 2)
    buffer_value = row["buffer"]
    if buffer_value is None:
        row["reasons"].append("DRAWDOWN_UNKNOWN")
    else:
        row["drawdownUsedPct"] = (round(row["projectedLoss"] / buffer_value * 100, 1)
                                  if buffer_value > 0 else None)
        if buffer_value <= 0:
            row["reasons"].append("ACCOUNT_BLOWN")
        elif row["projectedLoss"] > buffer_value + 1e-9:
            row["reasons"].append("DRAWDOWN_EXCEEDED")
    row["contractBlockers"] = _contract_blockers(qty, row["projectedLoss"], row["buffer"])
    row["legs"] = execution_contract.ultra_split(qty)
    # 判定は利用者定義どおり「利益目標 × 残ドローダウン」だけで決める。
    # 実行契約の可否は routable で別に返す。
    row["verdict"] = "ELIGIBLE" if not row["reasons"] else "INELIGIBLE"
    row["routable"] = row["verdict"] == "ELIGIBLE" and not row["contractBlockers"]
    return row


def build_plan(signal: Any, accounts: Any, *, point_value: Optional[float] = None,
               enabled: bool = True, signal_qty: Any = None) -> Dict[str, Any]:
    """ULTRA の発注計画を組む。発注は行わない。

    ``enabled=False`` (ULTRA OFF) では通常シグナルの枚数をそのまま返し、
    口座別の再計算は行わない。
    """
    value = _number(point_value)
    value = value if value and value > 0 else DEFAULT_POINT_VALUE
    geometry = signal_geometry(signal)
    rows: List[Dict[str, Any]] = []
    account_list = [item for item in (accounts or []) if isinstance(item, dict)] \
        if isinstance(accounts, Iterable) and not isinstance(accounts, (str, bytes)) else []

    if not enabled:
        normal = None
        try:
            normal = int(signal_qty) if signal_qty is not None else None
        except (TypeError, ValueError):
            normal = None
        return {"version": ULTRA_VERSION, "enabled": False, "pointValue": value,
                "geometry": geometry, "signalQty": normal, "accounts": [],
                "totalQty": normal, "eligibleCount": 0, "routable": False,
                "note": "ULTRA OFF — 通常シグナルの枚数をそのまま使用する"}

    for account in account_list:
        rows.append(account_plan(account, geometry, value))
    eligible = [row for row in rows if row["verdict"] == "ELIGIBLE"]
    return {
        "version": ULTRA_VERSION,
        "enabled": True,
        "pointValue": value,
        "geometry": geometry,
        "signalQty": _number(signal_qty),
        "accounts": rows,
        "totalQty": sum(int(row["qty"] or 0) for row in eligible),
        "totalProjectedProfit": round(sum(row["projectedProfit"] or 0 for row in eligible), 2),
        "totalProjectedLoss": round(sum(row["projectedLoss"] or 0 for row in eligible), 2),
        "eligibleCount": len(eligible),
        "accountCount": len(rows),
        "routable": bool(eligible) and all(row["routable"] for row in eligible),
        "note": "ULTRA ON — 通常枚数を無視し口座別の計算枚数へ強制上書きする",
    }


def accounts_from_state(state: Any) -> List[Dict[str, Any]]:
    """Durable Object の account stream から ULTRA 用の口座行を取り出す。"""
    if not isinstance(state, dict):
        return []
    accounts = state.get("accounts")
    rows = accounts.get("list") if isinstance(accounts, dict) else None
    return [row for row in (rows or []) if isinstance(row, dict)]


def signal_from_scenario(scenario: Any) -> Dict[str, Any]:
    """公開済み scenario を ULTRA の signal 形へ写す。価格は作らない。"""
    scenario = scenario if isinstance(scenario, dict) else {}
    targets = scenario.get("targets")
    target = scenario.get("target")
    if target is None and isinstance(targets, list) and targets:
        target = targets[0]
    return {"side": scenario.get("side"), "entry": scenario.get("entry"),
            "stop": scenario.get("stop"), "target": target}


def _cli(argv=None):
    import argparse

    parser = argparse.ArgumentParser(description="ULTRA mode sizing (calculation only)")
    parser.add_argument("--side", help="LONG/SHORT or BUY/SELL")
    parser.add_argument("--entry", type=float)
    parser.add_argument("--stop", type=float)
    parser.add_argument("--target", type=float)
    parser.add_argument("--off", action="store_true", help="ULTRA OFF (通常枚数のまま)")
    parser.add_argument("--signal-qty", type=int, default=None)
    parser.add_argument("--json", action="store_true", help="機械可読出力")
    args = parser.parse_args(argv)

    import nqx_state
    payload = nqx_state.build_accounts_payload()
    accounts = payload.get("list") or []
    signal = {"side": args.side, "entry": args.entry, "stop": args.stop, "target": args.target}
    plan = build_plan(signal, accounts, enabled=not args.off, signal_qty=args.signal_qty)
    if args.json:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 0
    geometry = plan["geometry"]
    if not geometry.get("valid"):
        print(f"SIGNAL INVALID: {geometry.get('reason')}")
        return 1
    print(f"ULTRA {'ON' if plan['enabled'] else 'OFF'} — {geometry['side']} "
          f"E={geometry['entry']:,.2f} SL={geometry['stop']:,.2f} TP={geometry['target']:,.2f} "
          f"(SL {geometry['slPoints']}pt / TP {geometry['tpPoints']}pt / RR {geometry['rr']})")
    if not plan["enabled"]:
        print(f"通常枚数 {plan['signalQty']} をそのまま使用")
        return 0
    for row in plan["accounts"]:
        detail = (f"{row['qty']}枚 利益 ${row['projectedProfit']:,.0f} "
                  f"損失 ${row['projectedLoss']:,.0f}") if row["qty"] else "—"
        note = "" if row["verdict"] == "ELIGIBLE" else f" [{', '.join(row['reasons'])}]"
        blocked = f"  routing不可: {', '.join(row['contractBlockers'])}" if row["contractBlockers"] else ""
        print(f"  {row['id']:<24} {detail:<44} {row['verdict']}{note}{blocked}")
    print(f"合計 {plan['totalQty']}枚 / 想定利益 ${plan['totalProjectedProfit']:,.0f} "
          f"/ 想定損失 ${plan['totalProjectedLoss']:,.0f} / routable={plan['routable']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())

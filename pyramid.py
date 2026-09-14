#!/usr/bin/env python3
"""R83 追撃(pyramiding)の判定。**純関数で、発注もネットワークも触らない。**

2026-09-12 ユーザー決定:

* **トリガーは同方向の新規シグナル** —— 保有中に同じ向きの候補が ARMED になったら
  追撃する。2026-09-12 02:12 の `VP80_REVERSION SELL A+` に人が手で 6 枚乗せて
  +$769 を取った動きを、経路として持つ。
* **枚数は ULTRA で全体を再計算** —— 追撃分だけを別枠で見ない。新しいシグナルの
  幾何で「利益目標に届く合計枚数」を引き直し、**今持っている枚数との差**を足す。
* **口座別リスク上限は追撃後の合計建玉に掛ける** —— 各エントリー個別ではない。
  CrossTrade/Tradovate は同一口座の同方向建玉を 1 本にネッティングするので
  (2026-09-12 の 4+6 が SHORT 10 に統合された)、リスクも 1 本で数えるのが実態。

追撃は平均建値を動かす。足すほど建値が現在値へ寄り、**同じ構造 SL までの距離が
変わる**ので、合計リスクは枚数に線形ではない。ここでは実際の合成建値から引き直し、
上限に収まる最大枚数まで刻んで落とす(`_fit_to_cap`)。

呼び出し側(engine)の責務はこのモジュールの外に置く:
  * ULTRA の合計目標枚数を出す(``ultra_mode.required_qty_split``)
  * 手動HALT(R82)・AUTO の武装・建玉の所有権を先に確認する
  * 実際の送信と、追撃後のプラン再凍結
"""
from __future__ import annotations

import math
from typing import Any, Dict, Optional

import execution_contract

#: 等級の強さ。追撃の下限等級はここで比較する。
GRADE_RANK = {"B": 1, "A": 2, "A+": 3}


def _number(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _int(value: Any) -> Optional[int]:
    number = _number(value)
    if number is None:
        return None
    return int(number)


def _text(value: Any) -> str:
    return str(value or "").strip()


def settings(contract: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """``execution_contract.json`` の ``pyramid`` 節。無ければ全部無効で返す。"""
    source = contract if contract is not None else execution_contract.CONTRACT
    section = source.get("pyramid") if isinstance(source, dict) else None
    if not isinstance(section, dict):
        return {"enabled": False, "dryRun": True, "maxAdds": 0, "minGrade": "A+"}
    return section


def enabled(contract: Optional[Dict[str, Any]] = None) -> bool:
    return settings(contract).get("enabled") is True


def dry_run(contract: Optional[Dict[str, Any]] = None) -> bool:
    """``dryRun`` の間は判定だけして送らない(影運転)。既定は安全側の True。"""
    return settings(contract).get("dryRun") is not False


def combined_entry(position_qty: int, position_entry: float,
                   add_qty: int, add_price: float) -> Optional[float]:
    """追撃後の平均建値。枚数加重。"""
    total = position_qty + add_qty
    if total <= 0:
        return None
    return (position_qty * position_entry + add_qty * add_price) / total


def combined_risk(position_qty: int, position_entry: float, add_qty: int,
                  add_price: float, stop: float, point_value: float) -> Optional[float]:
    """追撃後の合計建玉が構造 SL まで走ったときの損失($)。"""
    average = combined_entry(position_qty, position_entry, add_qty, add_price)
    if average is None or point_value <= 0:
        return None
    return abs(stop - average) * (position_qty + add_qty) * point_value


def slippage_points(contract: Optional[Dict[str, Any]] = None) -> float:
    """追撃の成行が不利側へ滑る上限(pt)。

    契約の ``marketOrder.maxDeviationPoints`` を使う —— 「供給した last と正本価格の
    乖離として認める上限」であり、成行の滑り上限として意味が合う。engine /
    order.py / Worker が同じ値を読めるので三重検証の数値が割れない
    (``.secrets`` の ``MARKET_SLIPPAGE_PT`` は order.py しか読めず、Worker では
    再現できないのでここでは使わない)。
    """
    source = contract if contract is not None else execution_contract.CONTRACT
    try:
        return float((source.get("marketOrder") or {})["maxDeviationPoints"])
    except (KeyError, TypeError, ValueError):
        return 0.0


def adverse_add_price(side: Any, add_price: Any,
                      contract: Optional[Dict[str, Any]] = None) -> Optional[float]:
    """追撃が **不利側へ滑った** ときの約定価格。

    SELL は安く売れ、BUY は高く買わされる。どちらも構造 SL までの距離が伸びる方向で、
    合計リスクは増える。上限判定はこの価格で行う —— 現在値ぴったりで見積もると、
    実際の約定は常に不利側へずれるので「概算では枠内・約定したら枠超え」になる。

    基礎はもう約定しているので滑るのは追撃脚だけである。合計距離へ一律に緩衝を
    足す単一脚の旧式より、この当て方の方が実際の合成建値に近い。
    """
    price = _number(add_price)
    if price is None:
        return None
    points = slippage_points(contract)
    return price - points if _text(side).upper() == "SELL" else price + points


def _fit_to_cap(position_qty: int, position_entry: float, add_qty: int,
                add_price: float, stop: float, point_value: float,
                cap_dollars: float) -> int:
    """上限に収まる最大の追撃枚数。収まらなければ 0。

    平均建値が枚数で動くので単純な割り算では出ない。枚数は小さい(ULTRA の
    口座別上限は契約で 100)ので、上から 1 枚ずつ落として最初に収まる値を採る。
    """
    for candidate in range(add_qty, 0, -1):
        risk = combined_risk(position_qty, position_entry, candidate,
                             add_price, stop, point_value)
        if risk is not None and risk <= cap_dollars + 1e-9:
            return candidate
    return 0


def _verdict(reason: str, **extra: Any) -> Dict[str, Any]:
    return {"add": False, "qty": 0, "reason": reason, **extra}


def evaluate(*, owned_plan: Any, scenario: Any, position: Any,
             adds_done: int, add_price: Any, stop: Any,
             target_total_qty: Any, risk_cap_dollars: Any,
             point_value: Any, contract: Optional[Dict[str, Any]] = None
             ) -> Dict[str, Any]:
    """追撃するか、するなら何枚か。

    ``add=False`` のときも ``reason`` に必ず機械可読な理由を入れる。「評価したが
    効かなかった」と「入力が無くて評価できなかった」を混ぜない(§2 と同じ規律)。
    """
    cfg = settings(contract)
    if cfg.get("enabled") is not True:
        return _verdict("PYRAMID_DISABLED")

    if not isinstance(owned_plan, dict) or not owned_plan:
        return _verdict("NO_OWNED_PLAN")
    if not isinstance(scenario, dict) or not scenario:
        return _verdict("NO_SCENARIO")
    if not isinstance(position, dict):
        return _verdict("NO_POSITION")

    position_qty = _int(position.get("qty")) or 0
    if position_qty <= 0:
        return _verdict("NO_POSITION")

    plan_side = _text(owned_plan.get("side")).upper()
    signal_side = _text(scenario.get("side")).upper()
    holding = {"LONG": "BUY", "SHORT": "SELL"}.get(
        _text(position.get("side")).upper(), _text(position.get("side")).upper())
    if not plan_side or not signal_side:
        return _verdict("SIDE_UNKNOWN")
    if signal_side != plan_side or (holding and holding != plan_side):
        return _verdict("SIDE_MISMATCH", signalSide=signal_side, planSide=plan_side)

    allowed_states = set(execution_contract.CONTRACT.get("scenario", {})
                         .get("allowedStates") or ("ARMED", "ACTIVE"))
    if _text(scenario.get("state")).upper() not in allowed_states:
        return _verdict("SCENARIO_NOT_ARMED", state=_text(scenario.get("state")))

    grade = _text(scenario.get("grade")).upper()
    floor = _text(cfg.get("minGrade") or "A+").upper()
    if GRADE_RANK.get(grade, 0) < GRADE_RANK.get(floor, 99):
        return _verdict("GRADE_BELOW_MIN", grade=grade, minGrade=floor)

    max_adds = _int(cfg.get("maxAdds")) or 0
    done = _int(adds_done) or 0
    if done >= max_adds:
        return _verdict("MAX_ADDS_REACHED", addsDone=done, maxAdds=max_adds)

    target_total = _int(target_total_qty)
    if target_total is None or target_total <= 0:
        return _verdict("TARGET_QTY_NOT_COMPUTABLE")
    wanted = target_total - position_qty
    if wanted <= 0:
        return _verdict("NO_ROOM", positionQty=position_qty, targetQty=target_total)

    entry = _number(position.get("avgEntry"))
    price = _number(add_price)
    stop_price = _number(stop)
    cap = _number(risk_cap_dollars)
    value = _number(point_value)
    if entry is None or entry <= 0:
        return _verdict("POSITION_ENTRY_UNAVAILABLE")
    if price is None or price <= 0 or stop_price is None or stop_price <= 0:
        return _verdict("ADD_GEOMETRY_UNAVAILABLE")
    if cap is None or cap <= 0 or value is None or value <= 0:
        return _verdict("RISK_CAP_UNAVAILABLE")

    # 構造 SL が既に現在値の反対側にある = この追撃は入った瞬間に負けている。
    if (plan_side == "SELL" and stop_price <= price) or \
       (plan_side == "BUY" and stop_price >= price):
        return _verdict("STOP_ON_WRONG_SIDE", stop=stop_price, price=price)

    # 枚数の決定も、報告する合計リスクも **不利側へ滑った価格** で行う。
    # `combinedEntry` だけは観測値のまま —— これは合成プランの `entry` /
    # `entryReference` になり、identity の照合基準として実際の約定に近い方がよい。
    # そのぶん `riskDollars` は `|entry - SL| x 枚数` と滑りの分だけ食い違うので、
    # `slippagePoints` を注記として一緒に残す。
    risk_price = adverse_add_price(plan_side, price, contract)
    if risk_price is None or risk_price <= 0:
        return _verdict("ADD_GEOMETRY_UNAVAILABLE")
    qty = _fit_to_cap(position_qty, entry, wanted, risk_price, stop_price, value, cap)
    if qty <= 0:
        return _verdict("RISK_CAP_EXCEEDED", wantedQty=wanted,
                        riskDollars=combined_risk(position_qty, entry, wanted,
                                                  risk_price, stop_price, value),
                        capDollars=cap, riskAddPrice=risk_price)

    average = combined_entry(position_qty, entry, qty, price)
    risk = combined_risk(position_qty, entry, qty, risk_price, stop_price, value)
    return {"add": True, "qty": qty, "reason": "PYRAMID_ADD",
            "positionQty": position_qty, "totalQty": position_qty + qty,
            "targetQty": target_total, "clamped": qty < wanted,
            "addPrice": price, "riskAddPrice": risk_price,
            "slippagePoints": slippage_points(contract),
            "combinedEntry": average, "combinedRisk": risk, "capDollars": cap,
            "grade": grade, "side": plan_side, "addsDone": done,
            "maxAdds": max_adds, "dryRun": dry_run(contract)}

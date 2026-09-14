#!/usr/bin/env python3
"""One deterministic R22 execution contract shared by every Python route.

This module has no network, broker, environment, or time side effects.  The
JSON next to it is the authoritative parameter source; callers supply all
observations explicitly and receive stable machine-readable blocker codes.
"""
from __future__ import annotations

import json
import math
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Optional, Tuple

BASE = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(BASE, "execution_contract.json"), encoding="utf-8") as _fh:
    CONTRACT: Dict[str, Any] = json.load(_fh)

VERSION = str(CONTRACT["version"])
TICK = 0.25


def _instant(value: Any) -> Optional[datetime]:
    if not value:
        return None
    raw = str(value).strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _number(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _accounts(cfg: Optional[Dict[str, Any]]) -> Iterable[str]:
    raw = str((cfg or {}).get("CROSSTRADE_ACCOUNTS") or
              (cfg or {}).get("CROSSTRADE_ACCOUNT") or "")
    return tuple(item.strip() for item in re.split(r"[,\n]+", raw) if item.strip())


def ultra_split(qty: Any) -> Optional[list]:
    """ULTRA の枚数を TP1 / RUNNER の2脚へ比率配分する。

    2枚固定時代の「各脚1枚」を一般化したもの。端数は runner 側へ寄せる。
    TP1 を先に外して残りを伸ばす運用なので、半端な1枚は runner が持つ。
    """
    try:
        total = int(qty)
    except (TypeError, ValueError):
        return None
    plan = CONTRACT["ultra"]["splitPlan"]
    min_leg = int(plan["minLegQty"])
    if total < min_leg * int(plan["legCount"]):
        return None
    tp1 = total // 2
    runner = total - tp1
    if tp1 < min_leg or runner < min_leg:
        return None
    return [tp1, runner]


def ultra_evaluate(plan: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """ULTRA の発注計画を ULTRA エンベロープだけで判定する。

    通常経路の固定枚数・$240 上限はここでは使わない。代わりに ULTRA 専用の
    上限(口座あたり枚数・合計枚数・口座数・口座あたり損失・合計損失)へ
    突き合わせる。1つでも超えたらその口座ではなく **計画全体** を止める。
    部分的に出すと、どの口座が入ったのか分からないまま建玉が残る。
    """
    envelope = CONTRACT["ultra"]
    blockers: list = []
    if not envelope.get("enabled"):
        return {"ok": False, "blockers": ["ULTRA_DISABLED"], "accounts": [],
                "totalQty": 0, "totalRiskDollars": 0.0}
    rows = (plan or {}).get("accounts") if isinstance(plan, dict) else None
    eligible = [row for row in (rows or [])
                if isinstance(row, dict) and row.get("verdict") == "ELIGIBLE"]
    if not eligible:
        return {"ok": False, "blockers": ["ULTRA_NO_ELIGIBLE_ACCOUNT"], "accounts": [],
                "totalQty": 0, "totalRiskDollars": 0.0}
    if len(eligible) > int(envelope["maxAccounts"]):
        blockers.append("ULTRA_ACCOUNTS_EXCEED_CONTRACT")

    total_qty, total_risk, checked = 0, 0.0, []
    for row in eligible:
        account_id = str(row.get("id") or "")
        qty = row.get("qty")
        loss = _number(row.get("projectedLoss"))
        buffer_value = _number(row.get("buffer"))
        legs = ultra_split(qty)
        row_blockers = []
        try:
            qty_int = int(qty)
        except (TypeError, ValueError):
            qty_int = 0
        if qty_int < int(envelope["minQtyPerAccount"]):
            row_blockers.append("ULTRA_QTY_BELOW_MIN")
        if qty_int > int(envelope["maxQtyPerAccount"]):
            row_blockers.append("ULTRA_QTY_EXCEEDS_ACCOUNT_MAX")
        if legs is None:
            row_blockers.append("ULTRA_SPLIT_NOT_REPRESENTABLE")
        if loss is None:
            row_blockers.append("ULTRA_RISK_UNKNOWN")
        else:
            if loss > float(envelope["maxRiskDollarsPerAccount"]) + 1e-9:
                row_blockers.append("ULTRA_ACCOUNT_RISK_EXCEEDS_CONTRACT")
            # 残ドローダウンが ULTRA のリスク上限の正本。設定漏れは通さない。
            if buffer_value is None or buffer_value <= 0:
                row_blockers.append("ULTRA_DRAWDOWN_UNAVAILABLE")
            elif loss > buffer_value + 1e-9:
                row_blockers.append("ULTRA_DRAWDOWN_EXCEEDED")
            total_risk += loss
        total_qty += qty_int
        checked.append({"id": account_id, "qty": qty_int, "legs": legs,
                        "riskDollars": loss, "drawdownBuffer": buffer_value,
                        "blockers": sorted(set(row_blockers))})
        blockers.extend(row_blockers)

    if total_qty > int(envelope["maxTotalQty"]):
        blockers.append("ULTRA_TOTAL_QTY_EXCEEDS_CONTRACT")
    if total_risk > float(envelope["maxRiskDollarsTotal"]) + 1e-9:
        blockers.append("ULTRA_TOTAL_RISK_EXCEEDS_CONTRACT")
    return {"ok": not blockers, "blockers": sorted(set(blockers)),
            "accounts": checked, "totalQty": total_qty,
            "totalRiskDollars": round(total_risk, 2),
            "version": str(envelope["version"])}


def risk_cap(cfg: Optional[Dict[str, Any]] = None) -> Tuple[Optional[float], str]:
    """Return the tightest configured account cap and its auditable source.

    An absent account mapping is deliberately not silently replaced with an
    unknown account-specific value: the contract reports the explicit common
    source or the static $240 profile default.
    """
    cfg = cfg or {}
    # The profile limit is an upper bound, not a fallback that a local
    # environment variable may widen.  An account configuration can tighten
    # it, but neither MAX_RISK_DOLLARS nor RISK_<account> can turn $240 into
    # $300 by changing a downstream process environment.
    profile_cap = float(CONTRACT["risk"]["defaultCapDollars"])
    configured_default = _number(cfg.get("MAX_RISK_DOLLARS"))
    if configured_default is not None and 0 < configured_default < profile_cap:
        default = configured_default
        default_source = "MAX_RISK_DOLLARS"
    else:
        default = profile_cap
        default_source = "execution_contract.defaultCapDollars"
    values = []
    for account in _accounts(cfg):
        key = f"{CONTRACT['risk']['accountPrefix']}{account}"
        value = _number(cfg.get(key))
        if value is None or value >= profile_cap:
            value = default
            source = default_source
        else:
            source = key
        if value > 0:
            values.append((value, source))
    if not values:
        return default, default_source
    return min(values, key=lambda item: item[0])


def account_caps(cfg: Optional[Dict[str, Any]] = None) -> list:
    """Return ``[(account, capDollars, capSource)]`` for every configured account.

    ``risk_cap`` collapses this to the single tightest cap, which is correct for
    a route that must satisfy every account at once.  A route that is allowed to
    drop the accounts it cannot afford needs the per-account values instead.
    """
    cfg = cfg or {}
    profile_cap = float(CONTRACT["risk"]["defaultCapDollars"])
    configured_default = _number(cfg.get("MAX_RISK_DOLLARS"))
    if configured_default is not None and 0 < configured_default < profile_cap:
        default, default_source = configured_default, "MAX_RISK_DOLLARS"
    else:
        default, default_source = profile_cap, "execution_contract.defaultCapDollars"
    rows = []
    for account in _accounts(cfg):
        key = f"{CONTRACT['risk']['accountPrefix']}{account}"
        value = _number(cfg.get(key))
        if value is None or value >= profile_cap:
            value, source = default, default_source
        else:
            source = key
        if value > 0:
            rows.append((account, float(value), source))
    return rows


def affordable_accounts(cfg: Optional[Dict[str, Any]],
                        risk_dollars: Optional[float]) -> Tuple[list, list]:
    """Split the configured accounts into (affordable, excluded) for one risk.

    A per-account cap is a property of that account, not of the route.  An
    account that cannot afford the trade is dropped from the route; it is not a
    reason to cancel the trade on the accounts that can.  Both halves are
    returned so the exclusion stays auditable instead of the route silently
    shrinking.
    """
    rows = account_caps(cfg)
    if risk_dollars is None or not rows:
        return [account for account, _, _ in rows], []
    affordable, excluded = [], []
    for account, cap, source in rows:
        if risk_dollars <= cap + 1e-9:
            affordable.append(account)
        else:
            excluded.append({"account": account, "capDollars": cap, "capSource": source})
    return affordable, excluded


def _cvd_at(market: Dict[str, Any]) -> Optional[datetime]:
    for source in (market, market.get("cvdMeta") if isinstance(market.get("cvdMeta"), dict) else {},
                   market.get("ictEvidence") if isinstance(market.get("ictEvidence"), dict) else {}):
        if not isinstance(source, dict):
            continue
        direct = _instant(source.get("cvdAt") or source.get("at")) if source is not market else _instant(source.get("cvdAt"))
        if direct:
            return direct
        nested = source.get("cvd")
        if isinstance(nested, dict):
            nested_at = _instant(nested.get("at") or nested.get("observedAt") or nested.get("cvdAt"))
            if nested_at:
                return nested_at
    return None


def evaluate(scenario: Optional[Dict[str, Any]], market: Optional[Dict[str, Any]],
             position: Optional[Dict[str, Any]] = None,
             order: Optional[Dict[str, Any]] = None,
             *, now: Optional[datetime] = None,
             cfg: Optional[Dict[str, Any]] = None,
             event_blackout: bool = False,
             ultra: bool = False,
             ultra_buffer: Optional[float] = None) -> Dict[str, Any]:
    """Evaluate the complete execution gate without executing anything.

    ``blockers`` are hard denials. ``caps`` preserve an A+ -> A downgrade and
    retry requirement without falsely treating a valid A scenario as blocked.

    ULTRA には2つの入口がある。producer(monitor_publish)は ``ultra=True`` と
    ``ultra_buffer``(その口座の残ドローダウン $)を明示して評価し、その結果が
    ``riskCapSource=ACCOUNT_DRAWDOWN_BUFFER`` としてシナリオへ凍結される。
    下流(engine / bot / Worker)は凍結された riskCapSource からULTRAを検出する。
    どちらの入口でも枚数は ULTRA エンベロープ、リスク上限は残ドローダウンと
    ``maxRiskDollarsPerAccount`` の狭い側で判定し、RISK_* は使わない。
    """
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    scenario = scenario if isinstance(scenario, dict) else {}
    market = market if isinstance(market, dict) else {}
    position = position if isinstance(position, dict) else {}
    order = order if isinstance(order, dict) else {}
    blockers, caps, acquisition = [], [], []

    observed = _instant(market.get("observedAt") or market.get("at"))
    market_age = None if observed is None else (now - observed).total_seconds()
    if observed is None:
        blockers.append("MARKET_TIMESTAMP_MISSING")
    elif market_age < -float(CONTRACT["market"]["futureToleranceSec"]):
        blockers.append("MARKET_IN_FUTURE")
    elif market_age > float(CONTRACT["market"]["maxAgeSec"]):
        blockers.append("MARKET_STALE")

    issued, expires = _instant(scenario.get("issuedAt")), _instant(scenario.get("expiresAt"))
    scenario_age = None if issued is None else (now - issued).total_seconds()
    if not scenario:
        blockers.append("SCENARIO_MISSING")
    elif issued is None or expires is None:
        blockers.append("SCENARIO_TIMESTAMP_MISSING")
    elif expires <= now:
        blockers.append("SCENARIO_EXPIRED")
    elif scenario_age > float(CONTRACT["scenario"]["maxAgeSec"]):
        blockers.append("SCENARIO_STALE")

    grade = str(scenario.get("grade") or "").upper()
    effective_grade = grade
    cvd_at = _cvd_at(market)
    cvd_unusable = False
    if CONTRACT["cvd"]["timestampRequiredForAPlus"] and cvd_at is None:
        caps.append("CVD_TIMESTAMP_MISSING")
        cvd_unusable = True
    elif cvd_at is not None and (now - cvd_at).total_seconds() > float(CONTRACT["cvd"]["maxAgeSec"]):
        caps.append("CVD_STALE")
        cvd_unusable = True
    if cvd_unusable:
        acquisition.append("CVD_REACQUIRE_REQUIRED")
        if grade == "A+":
            effective_grade = str(CONTRACT["cvd"]["missingTimestampCap"])
            caps.append("CVD_A_PLUS_CAPPED")
            blockers.append("CVD_A_PLUS_PROHIBITED")
    if effective_grade not in set(CONTRACT["scenario"]["allowedGrades"]):
        blockers.append("GRADE_NOT_ORDERABLE")
    if str(scenario.get("state") or "").upper() not in set(CONTRACT["scenario"]["allowedStates"]):
        blockers.append("SCENARIO_STATE_NOT_ORDERABLE")
    scenario_contract_version = str(scenario.get("executionContractVersion") or VERSION)
    if scenario_contract_version != VERSION:
        blockers.append("EXECUTION_CONTRACT_VERSION_MISMATCH")

    if event_blackout or market.get("eventBlackout") is True or market.get("eventGate") == "BLACKOUT":
        blockers.append("EVENT_BLACKOUT")
    # 日次ガードは 2026-08-27 に廃止した。ここは**デプロイ済み Worker の鏡**
    # なので、Worker を再デプロイするまで判定は消さない —— 消すとローカル
    # dry-run が通ってサーバーだけが拒否する、という最悪のズレが生まれる。
    # 実質的には無効: monitor_publish は available:true / blocked:false / 新鮮な
    # at しか出さないので、下の3つはもう成立しない。
    guard = market.get("dayguard") if isinstance(market.get("dayguard"), dict) else None
    if guard is not None:
        guard_at = _instant(guard.get("at"))
        if guard.get("available") is not True:
            blockers.append("DAYGUARD_UNAVAILABLE")
        elif (guard_at is None or (now - guard_at).total_seconds() < 0
              or (now - guard_at).total_seconds() > float(CONTRACT["dayguard"]["maxAgeSec"])):
            blockers.append("DAYGUARD_STALE")
        elif guard.get("blocked") is True:
            blockers.append("DAYGUARD_BLOCKED")
    session_end = _instant(scenario.get(CONTRACT["session"]["endField"]) or
                           market.get(CONTRACT["session"]["endField"]))
    if session_end is not None and now >= session_end:
        blockers.append("SESSION_ENDED")

    entry, stop, qty = _number(scenario.get("entry")), _number(scenario.get("stop")), _number(scenario.get("qty"))
    fixed_qty = int(CONTRACT["risk"]["fixedQty"])

    # ULTRA 検出。producer は ultra=True を明示、下流は凍結された
    # riskCapSource から検出する。エンベロープ無効なら常に通常経路。
    frozen_contract = scenario.get("executionContract") if isinstance(scenario.get("executionContract"), dict) else {}
    ultra_env = CONTRACT.get("ultra") or {}
    ultra_mode = bool(ultra_env.get("enabled")) and (
        ultra or str(frozen_contract.get("riskCapSource") or "") == str(ultra_env.get("riskCapSource")))
    ultra_leg_split = None
    if ultra_mode:
        try:
            qty_int = int(qty) if qty is not None and qty == int(qty) else None
        except (TypeError, ValueError):
            qty_int = None
        ultra_leg_split = ultra_split(qty_int) if qty_int is not None else None
        if (qty_int is None or qty_int < int(ultra_env["minQtyPerAccount"])
                or qty_int > int(ultra_env["maxQtyPerAccount"]) or ultra_leg_split is None):
            blockers.append("ULTRA_QTY_OUT_OF_ENVELOPE")
    elif qty is None or qty != fixed_qty:
        blockers.append("FIXED_QTY_REQUIRED")
    split = CONTRACT["splitPlan"]
    targets = scenario.get("targets")
    legs = scenario.get("legs")
    normalized_targets = []
    if not isinstance(targets, list) or len(targets) != int(split["targetCount"]):
        blockers.append("SPLIT_PLAN_REQUIRED")
    else:
        normalized_targets = [_number(value) for value in targets]
        if any(value is None for value in normalized_targets) or len(set(normalized_targets)) != len(normalized_targets):
            blockers.append("SPLIT_TARGETS_INVALID")
        elif entry is not None:
            side = str(scenario.get("side") or "").upper()
            if ((side == "BUY" and not (entry < normalized_targets[0] < normalized_targets[1]))
                    or (side == "SELL" and not (normalized_targets[1] < normalized_targets[0] < entry))
                    or side not in {"BUY", "SELL"}):
                blockers.append("SPLIT_TARGETS_INVALID")
    if ultra_mode and ultra_leg_split is not None:
        expected_legs = ([{"id": "TP1", "qty": ultra_leg_split[0], "target": normalized_targets[0]},
                          {"id": "RUNNER", "qty": ultra_leg_split[1], "target": normalized_targets[1]}]
                         if len(normalized_targets) == 2 and all(value is not None for value in normalized_targets) else None)
    else:
        expected_legs = ([{"id": "TP1", "qty": int(split["legQty"]), "target": normalized_targets[0]},
                          {"id": "RUNNER", "qty": int(split["legQty"]), "target": normalized_targets[1]}]
                         if len(normalized_targets) == 2 and all(value is not None for value in normalized_targets) else None)
    if (not isinstance(legs, list) or len(legs) != int(split["legCount"])
            or expected_legs is None or legs != expected_legs
            or (split.get("planVersionRequired") and not scenario.get("planVersion"))):
        blockers.append("SPLIT_LEGS_INVALID")
    cap, source = risk_cap(cfg)
    account_scope = sorted(set(_accounts(cfg)))
    excluded_accounts: list = []
    frozen_cap = _number(frozen_contract.get("riskCapDollars"))
    frozen_scope = frozen_contract.get("accountScope")
    risk = None
    if ultra_mode:
        # ULTRA: リスク上限の正本は残ドローダウン(producer は ultra_buffer、
        # 下流は凍結された riskCapDollars)。RISK_* と $240 既定は使わない。
        # 口座は同時に1つだけ — ENTRY claim key が口座次元を持たないため。
        if isinstance(frozen_scope, list) and frozen_scope:
            account_scope = sorted({str(value) for value in frozen_scope if str(value)})
        if len(account_scope) != 1:
            blockers.append("ULTRA_SCOPE_NOT_SINGLE")
        buffer_cap = _number(ultra_buffer) if ultra else frozen_cap
        max_account_risk = float(ultra_env["maxRiskDollarsPerAccount"])
        if buffer_cap is None or buffer_cap <= 0:
            cap, source = None, str(ultra_env.get("riskCapSource") or "ACCOUNT_DRAWDOWN_BUFFER")
            blockers.append("RISK_CAP_UNAVAILABLE")
        else:
            cap = min(float(buffer_cap), max_account_risk)
            source = str(ultra_env.get("riskCapSource") or "ACCOUNT_DRAWDOWN_BUFFER")
        if entry is None or stop is None or qty is None or qty <= 0:
            blockers.append("RISK_GEOMETRY_INVALID")
        else:
            risk = abs(entry - stop) * qty * float(CONTRACT["risk"]["pointValue"])
            if cap is not None and risk > cap + 1e-9:
                blockers.append("RISK_CAP_EXCEEDED")
    elif entry is None or stop is None or qty is None or qty <= 0:
        blockers.append("RISK_GEOMETRY_INVALID")
    elif qty > float(CONTRACT["risk"]["maxQty"]):
        blockers.append("QTY_EXCEEDS_CONTRACT")
    else:
        risk = abs(entry - stop) * qty * float(CONTRACT["risk"]["pointValue"])
        # 口座別上限はその口座の性質であって経路の性質ではない。払えない口座は
        # 経路から外すだけで、払える口座の取引まで消さない。全口座が払えない
        # ときだけ RISK_CAP_EXCEEDED になる。
        affordable, excluded_accounts = affordable_accounts(cfg, risk)
        if affordable and len(affordable) < len(account_scope):
            account_scope = sorted(set(affordable))
            # 残った口座で最も厳しい上限へ張り替える。外した口座の上限を
            # 残った口座の判定に使い続けない。
            remaining = [(value, src) for account, value, src in account_caps(cfg)
                         if account in set(affordable)]
            if remaining:
                cap, source = min(remaining, key=lambda item: item[0])
        # The monitor freezes the account cap/source into the server scenario.
        # A downstream route may only become stricter; it may not silently expand
        # that frozen cap because its local config omitted an account mapping.
        # 除外で cap を張り替えた後に適用するので、凍結値は常に上限として効く。
        if frozen_cap is not None and frozen_cap > 0 and (cap is None or frozen_cap < cap):
            cap = frozen_cap
            source = str(frozen_contract.get("riskCapSource") or "scenario.executionContract.riskCapDollars")
        # 口座が1つも設定されていない経路では affordable も excluded も空になる。
        # それは「全口座が払えない」ではなく「口座別の判定材料が無い」なので、
        # 従来どおり既定上限との単純比較へ落とす。ここを取り違えると、口座設定を
        # 持たないテスト/縮退経路が丸ごと RISK_CAP_EXCEEDED になる。
        no_account_affords = bool(excluded_accounts) and not affordable
        if cap is None or cap <= 0:
            blockers.append("RISK_CAP_UNAVAILABLE")
        elif no_account_affords or risk > cap + 1e-9:
            blockers.append("RISK_CAP_EXCEEDED")
    if not ultra_mode and risk is None and frozen_cap is not None and frozen_cap > 0 and (cap is None or frozen_cap < cap):
        # リスクを計算できなかった経路でも、凍結上限の「狭い側だけ」は維持する。
        cap = frozen_cap
        source = str(frozen_contract.get("riskCapSource") or "scenario.executionContract.riskCapDollars")

    if str(position.get("state") or "").upper() in set(CONTRACT["openPositionStates"]):
        blockers.append("POSITION_OPEN")
    if str(order.get("state") or "").upper() in set(CONTRACT["blockingOrderStates"]):
        blockers.append("ORDER_PENDING")

    return {
        "version": VERSION,
        "orderable": not blockers,
        "blockers": sorted(set(blockers)),
        "caps": sorted(set(caps)),
        "acquisitionRequired": sorted(set(acquisition)),
        "effectiveGrade": effective_grade or None,
        "marketAgeSec": market_age,
        "scenarioAgeSec": scenario_age,
        "riskDollars": risk,
        "riskCapDollars": cap,
        "riskCapSource": source,
        "accountScope": account_scope,
        "excludedAccounts": excluded_accounts,
        "cvdAt": cvd_at.isoformat() if cvd_at else None,
        "sessionEndAt": session_end.isoformat() if session_end else None,
    }

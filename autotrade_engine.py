# -*- coding: utf-8 -*-
"""R11-D の自律執行・建玉管理エンジン。

このモジュールは分析と CrossTrade の送信を分離し、同じ ``order.py`` の
安全ゲートを新規・変更・決済の全てで通す。通常の新規ENTRY権限は
Telegram Mini App AUTOがWorkerへ保存する期限付きLIVE武装である。
`NQX_AUTOTRADE` / `NQX_LIVE_ORDERS` は緊急上書きとテスト注入用に残す。

テストと監視の既定値は dry-run であり、実口座へ接続しない。ライブ送信は
``order.py`` の subprocess だけが行い、同じ decisionId / 管理ステップを
二度送らない。応答が失敗・部分成功・照会不能なら自動再送せず HALT とする。

管理方式は 2 枚を独立した 1 枚ずつの OCO ブラケットとして発注する分割型で
ある。1枚はTP1で確定し、もう1枚だけを最終ターゲットまで残す。TP1約定後は
runnerだけを建値→一方向トレールへ変更する。片脚の送信・照会が不明なら
再送せず HALT する。
"""

from __future__ import annotations

import hashlib
import inspect
import json
import math
import os
import statistics
import subprocess
import sys
import time
from datetime import datetime, time as dt_time, timezone, timedelta
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

import autotrade_arm
import contract as contract_month  # R102: 取引限月の正本
import execution_contract
import management_intent
import ownership_binder
import pyramid
import route_envelope
import route_identity
import strategy_evidence
import tranche

BASE = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = os.path.join(BASE, ".secrets", "crosstrade.env")
LEDGER_FILE = os.path.join(BASE, ".secrets", "autotrade_ledger.jsonl")
TICK = 0.25
#: TP1 約定後に runner の SL を寄せる下限を、建値ちょうどではなく
#: **建値 +1pt(SELL は −1pt)** にする。建値ぴったりの SL は、往復の手数料と
#: 1tick の滑りを吸収できずに実質マイナスで終わる。1pt = $2/枚 あれば
#: 「TP1 を取った後の runner は最悪でも損にならない」が金額で成立する。
#: 0.25 の倍数なので tick 丸めで潰れない。
BREAKEVEN_OFFSET_POINTS = 1.0
#: R104(2026-09-16 ユーザー決定。R103 は docs/DEVIN_TASKS.md §3-K の流動性狩り対策に先着): 人が置く **決定 ID 単位**の管理上書き。台帳は書き換えない。
#: `.secrets/management_override.json` = {"schema": "NQX_MANAGEMENT_OVERRIDE/1",
#:   "overrides": [{"decisionId": "...", "finalTarget": 29137.0, "trailMode": "BREAKEVEN_ONLY"}]}
#: `finalTarget` は runner 最終 TP(MODIFY の take_profit と最終 TP 到達 FLATTEN の両方)を
#: 差し替える。TP1 を越えていない値は無視する。`trailMode=BREAKEVEN_ONLY` は TP1 後の
#: SL を建値±1pt の床にだけ寄せ、極値からのトレールを出さない。凍結プランを読む
#: `_frozen_plan_record()` の出口で乗せるので、管理・修復・FLATTEN 判定が同じ値を見る。
MANAGEMENT_OVERRIDE_FILE = os.path.join(BASE, ".secrets", "management_override.json")
MANAGEMENT_OVERRIDE_SCHEMA = "NQX_MANAGEMENT_OVERRIDE/1"
TRAIL_MODE_BREAKEVEN_ONLY = "BREAKEVEN_ONLY"
#: R78: 保護 stop を現在値からどれだけ離して置くか(pt)。`cancelandbracket` は
#: 取消→新規の 2 段なので、新しい stop が「価格がすでに通過した側」に来ると
#: ブローカーが拒否し、**取消だけが成立して runner が保護注文ゼロ**で残る
#: (2026-09-11 01:40:48 実測: 安値 29,146.25 + トレール 28pt = 29,174.25 を、判断時の
#: 3 分前の価格 29,171 に対して 1 tick の余裕で「protective」と判定 → 送信時には
#: 価格が上にあり BUY STOP 拒否 → SHORT 2 枚が SL 無し)。1 tick では足りない。
#: `NQX_STOP_MARKET_BUFFER_PT` で可逆。order.py も同じ既定値で送信直前に再検査する。
STOP_MARKET_BUFFER_POINTS_DEFAULT = 4.0
# 通常経路の枚数・ポイント価値は実行契約だけを正本にする。LIFELINE は
# 口座残機の表示値であり、通常ENTRYの1口座あたりリスク枠を縮小しない。
POINT_VALUE = float(execution_contract.CONTRACT["risk"]["pointValue"])
MAX_QTY = int(execution_contract.CONTRACT["risk"]["maxQty"])


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "enable", "enabled"}


def _read_env_file(path: str = ENV_FILE) -> Dict[str, str]:
    out: Dict[str, str] = {}
    try:
        with open(path, encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    out[key.strip()] = value.strip()
    except OSError:
        pass
    return out


def _setting(name: str, cfg: Optional[Dict[str, str]] = None, default: Any = None) -> Any:
    if name in os.environ:
        return os.environ[name]
    if cfg and name in cfg:
        return cfg[name]
    return default


def _query_orders_with_ids(query, symbol: str, known_order_ids=None):
    """Use the richer broker query when supported without masking TypeErrors."""
    try:
        params = inspect.signature(query).parameters.values()
        supports = (any(param.kind == inspect.Parameter.VAR_KEYWORD for param in params)
                    or "known_order_ids" in inspect.signature(query).parameters)
    except (TypeError, ValueError):
        supports = False
    return query(symbol, known_order_ids=known_order_ids) if supports else query(symbol)


def _switch(name: str, arm_key: str, cfg: Optional[Dict[str, str]] = None) -> bool:
    """解決順。プロセス環境変数 > 明示 cfg/.env > app/台帳武装 > 既定 0。

    R39: 監視ループはサイクルごとに新しいプロセスを起動するので、環境変数
    だけでは二重キーが実運用の経路で一度も揃わなかった。武装台帳
    (``autotrade_arm``) を正規の受け渡し経路として追加した。R41では同モジュールが
    Mini App/Workerの期限付き武装を優先して読む。

    台帳は「どこにも書かれていないときだけ効く」位置に置く。明示的に書かれた
    キーは、それが ``0`` であっても台帳より強い —— 環境変数からも ``.env``
    からも緊急停止を掛けられる性質を壊さないため。逆に、期限切れ・口座変更・
    銘柄変更で台帳が無効になったときは、どこにも指定が無ければ ``False``
    へ落ちる（fail-closed）。
    """
    if name in os.environ:
        return _truthy(os.environ[name])
    if cfg and name in cfg:
        return _truthy(cfg[name])
    try:
        armed = autotrade_arm.state(cfg=cfg)
    except Exception:  # noqa: BLE001 — 台帳の異常は必ず武装解除側へ倒す
        return False
    if armed.get("valid"):
        return bool(armed.get(arm_key))
    return False


def manual_halt(cfg: Optional[Dict[str, str]] = None) -> bool:
    """R82 / R87: 緊急停止。設定ファイル(``manualHalt.autotrade``)か設定ページのスイッチ。

    立っている間は新規 ENTRY・追撃・建玉管理がすべて止まる。**環境変数や武装台帳
    より強い** —— `NQX_AUTOTRADE=1` で上書きできる緊急停止は緊急停止ではない。
    撤退は塞がない(`NQX_AUTOTRADE_KILL` の全決済と人の `--flatten` は通る)。

    設定ページ側(R87)は ``autotrade_arm.read_manual_halt`` の最後の既知値を見る。ここでは
    通信しない —— Worker から写すのは ``reconcile`` の本番経路の ``sync_manual_halt`` だけ。
    """
    return manual_halt_source(cfg) is not None


def manual_halt_source(cfg: Optional[Dict[str, str]] = None) -> Optional[str]:
    """立っている HALT の出所。立っていなければ None。"""
    section = execution_contract.CONTRACT.get("manualHalt")
    if isinstance(section, dict) and section.get("autotrade") is True:
        return "execution_contract.manualHalt.autotrade"
    try:
        remote = autotrade_arm.read_manual_halt()
    except Exception:  # noqa: BLE001 — 写しが読めないことを HALT と推測しない
        return None
    if remote.get("enabled") is True:
        since = str(remote.get("engagedAt") or remote.get("updatedAt") or "")
        return "Mini App 設定ページ" + (f" (since {since})" if since else "")
    return None


def autotrade_enabled(cfg: Optional[Dict[str, str]] = None) -> bool:
    """分析ループが自律経路を走らせるか。既定は無効。"""
    if manual_halt(cfg):
        return False
    return _switch("NQX_AUTOTRADE", "autotrade", cfg)


def live_enabled(cfg: Optional[Dict[str, str]] = None) -> bool:
    """実送信を許可する二重キー。片方だけでは dry-run のまま。"""
    return autotrade_enabled(cfg) and _switch("NQX_LIVE_ORDERS", "live", cfg)


def kill_enabled(cfg: Optional[Dict[str, str]] = None) -> bool:
    """緊急全決済フラグ。武装台帳からも環境変数からも立てられる。"""
    return _switch("NQX_AUTOTRADE_KILL", "kill", cfg)


def _kill_exit_switches(cfg: Optional[Dict[str, str]] = None) -> Tuple[bool, bool]:
    """R82: manualHalt 中の KILL 撤退にだけ使う ``(autotrade, live)``。halt を見ない二重キー。

    ``autotrade_enabled`` / ``live_enabled`` は manualHalt で必ず False に倒れる。そのままでは
    ``_reconcile_one`` の冒頭で空が返り、建玉がある口座の KILL 全決済が一度も送られなかった
    (届いても dry-run で終わる)。ここで返すのは halt が無いときと同じスイッチなので、撤退の
    可否と live / dry-run の区別は通常の KILL と一致する。緊急停止そのものを緩めないよう、
    呼び出しは ``_reconcile_one`` の撤退判定の 2 か所に限る(tests/test_r82 が本数を固定)。
    """
    autotrade = _switch("NQX_AUTOTRADE", "autotrade", cfg)
    return autotrade, autotrade and _switch("NQX_LIVE_ORDERS", "live", cfg)


def _stop_market_buffer(cfg: Optional[Dict[str, str]] = None) -> float:
    """R78: stop と現在値の最小距離(pt)。不正値は既定へ落とす。"""
    raw = _setting("NQX_STOP_MARKET_BUFFER_PT", cfg, STOP_MARKET_BUFFER_POINTS_DEFAULT)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = STOP_MARKET_BUFFER_POINTS_DEFAULT
    if not math.isfinite(value) or value < 0:
        value = STOP_MARKET_BUFFER_POINTS_DEFAULT
    return max(value, TICK)


def _num(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _tick(value: float) -> float:
    return round(round(float(value) / TICK) * TICK, 2)


def _iso_now(now: Optional[datetime] = None) -> str:
    value = now or datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _parse_at(value: Any) -> Optional[datetime]:
    if not value:
        return None
    raw = str(value).strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _price_from_bundle(bundle: Dict[str, Any]) -> Optional[float]:
    value = _num(bundle.get("price"))
    if value is not None:
        return value
    snapshot = bundle.get("snapshot") or {}
    bars = snapshot.get("bars3m") or snapshot.get("bars") or []
    if bars:
        last = bars[-1] or {}
        return _num(last.get("c", last.get("close")))
    return None


def _bars(bundle: Dict[str, Any]) -> List[Dict[str, Any]]:
    snapshot = bundle.get("snapshot") or {}
    raw = snapshot.get("bars3m") or snapshot.get("bars") or []
    return [bar for bar in raw if isinstance(bar, dict)]


def _noise_floor(bundle: Dict[str, Any]) -> Optional[float]:
    ranges = []
    for bar in _bars(bundle)[-12:]:
        high = _num(bar.get("h", bar.get("high")))
        low = _num(bar.get("l", bar.get("low")))
        if high is not None and low is not None and high >= low:
            ranges.append(high - low)
    return statistics.median(ranges) if len(ranges) >= 3 else None


def _epoch(value: Any) -> Optional[float]:
    number = _num(value)
    if number is not None:
        return number / 1000.0 if number > 10_000_000_000 else number
    parsed = _parse_at(value)
    return parsed.timestamp() if parsed else None


def _extreme(bundle: Dict[str, Any], side: str,
             since: Optional[datetime] = None) -> Optional[float]:
    values = []
    for bar in _bars(bundle):
        if since is not None:
            bar_time = _epoch(bar.get("t", bar.get("time")))
            if bar_time is not None and bar_time < since.timestamp():
                continue
        key = "h" if side == "BUY" else "l"
        value = _num(bar.get(key, bar.get("high" if side == "BUY" else "low")))
        if value is not None:
            values.append(value)
    # R78: 現在値も極値の候補に含める。足は周期開始時のもの(最大 3 分古い)で、
    # 約定監視(fill_watch)から呼ばれるときは quote だけが最新の観測だから。
    price = _price_from_bundle(bundle)
    if price is not None:
        values.append(price)
    if not values:
        return None
    return max(values) if side == "BUY" else min(values)


def _frozen_account_scope(scenario: Dict[str, Any],
                          cfg: Optional[Dict[str, str]]) -> List[str]:
    """凍結スコープ ∩ 現在の許可リスト。片方が空なら他方をそのまま使う。

    許可リストを読めない経路(cfg 未指定のテスト/純関数利用)で勝手に空へ
    潰さないため、交差が空になる場合は凍結スコープを残す。
    """
    frozen = sorted(str(value) for value in
                    ((scenario.get("executionContract") or {}).get("accountScope") or [])
                    if str(value))
    allowed = set(_multi_scope(cfg or {}))
    if not frozen or not allowed:
        return frozen
    narrowed = [account for account in frozen if account in allowed]
    return narrowed or frozen


def build_management_plan(scenario: Dict[str, Any], bundle: Optional[Dict[str, Any]] = None,
                          cfg: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """シナリオを凍結した管理計画へ変換する純粋関数。

    ``targets`` は R11-D の複数候補を受け、最初をTP1、最後をrunner最終TPと
    する。2枚固定ではこの二つが別価格であることを要求し、入口から二本の
    1枚OCOブラケットとして渡す。初期SLは分析が指定した構造SLから動かさない。
    """
    if not isinstance(scenario, dict):
        raise ValueError("scenario must be an object")
    side = str(scenario.get("side") or "").upper()
    if side not in {"BUY", "SELL"}:
        raise ValueError("scenario side must be BUY or SELL")
    entry, stop = _num(scenario.get("entry")), _num(scenario.get("stop"))
    if entry is None or stop is None or entry == stop:
        raise ValueError("scenario entry/stop are required")
    if side == "BUY" and stop >= entry or side == "SELL" and stop <= entry:
        raise ValueError("scenario stop has the wrong side")
    raw_targets = scenario.get("targets")
    if not isinstance(raw_targets, list) or not raw_targets:
        raw_targets = [scenario.get("target")]
    targets = [_tick(value) for value in (_num(item) for item in raw_targets) if value is not None]
    targets = [target for target in targets
               if (target > entry if side == "BUY" else target < entry)]
    if not targets:
        raise ValueError("scenario needs a directional target")
    try:
        qty = int(scenario.get("qty") or 2)
    except (TypeError, ValueError):
        raise ValueError("scenario qty must be an integer")
    frozen_contract = (scenario.get("executionContract")
                       if isinstance(scenario.get("executionContract"), dict) else {})
    frozen_cap = _num(frozen_contract.get("riskCapDollars"))
    # R47: ULTRA シナリオの検出。monitor_publish が凍結した riskCapSource が
    # ULTRA の正本値と一致したときだけ、枚数を ULTRA エンベロープで受ける。
    ultra_env = execution_contract.CONTRACT.get("ultra") or {}
    ultra_mode = bool(ultra_env.get("enabled")) and (
        str(frozen_contract.get("riskCapSource") or "") == str(ultra_env.get("riskCapSource")))
    leg_split = [1, 1]
    if ultra_mode:
        leg_split = execution_contract.ultra_split(qty)
        if (leg_split is None or qty < int(ultra_env["minQtyPerAccount"])
                or qty > int(ultra_env["maxQtyPerAccount"])):
            raise ValueError(f"ULTRA_QTY_OUT_OF_ENVELOPE: qty {qty}")
    else:
        if qty < 1 or qty > MAX_QTY:
            raise ValueError(f"scenario qty must be 1..{MAX_QTY}")
        if qty != 2:
            raise ValueError("R12 split management requires fixed qty=2")
    if len(targets) < 2 or targets[0] == targets[-1]:
        raise ValueError("R12 split management requires distinct TP1 and runner target")
    risk = abs(entry - stop)
    risk_dollars = risk * qty * POINT_VALUE
    if ultra_mode:
        # ULTRA: リスク上限の正本は凍結された残ドローダウンと、契約の
        # 口座あたり上限の狭い側。RISK_* と $240 既定は使わない。
        if frozen_cap is None or frozen_cap <= 0:
            raise ValueError("ULTRA drawdown cap is not frozen in the scenario")
        cap_dollars = min(float(frozen_cap), float(ultra_env["maxRiskDollarsPerAccount"]))
        cap_source = str(ultra_env.get("riskCapSource") or "ACCOUNT_DRAWDOWN_BUFFER")
        cap_points = cap_dollars / POINT_VALUE / qty
        if risk_dollars > cap_dollars + 1e-9:
            raise ValueError(
                f"ULTRA risk {risk:.2f}pt x {qty} = ${risk_dollars:.2f} exceeds "
                f"drawdown cap ${cap_dollars:.2f}")
    else:
        cap_dollars, cap_source = execution_contract.risk_cap(cfg)
        # 公開済みシナリオが明示的に狭い上限を凍結している場合だけ尊重する。
        # 口座数や LIFELINE で共通 $240 を割らない。2口座とも「各口座 $240」。
        if frozen_cap is not None and frozen_cap > 0 and (cap_dollars is None or frozen_cap < cap_dollars):
            cap_dollars = frozen_cap
            cap_source = str(frozen_contract.get("riskCapSource") or
                             "scenario.executionContract.riskCapDollars")
        if cap_dollars is None or cap_dollars <= 0:
            raise ValueError("normal account risk cap is unavailable")
        cap_points = cap_dollars / POINT_VALUE / qty
        if risk_dollars > cap_dollars + 1e-9:
            raise ValueError(
                f"scenario risk {risk:.2f}pt x {qty} = ${risk_dollars:.2f} exceeds "
                f"normal per-account cap {cap_points:.2f}pt x {qty} = ${cap_dollars:.2f}")
    noise = _noise_floor(bundle or {})
    try:
        trail_mult = float(_setting("NQX_AUTOTRADE_TRAIL_R", cfg, "0.75"))
    except (TypeError, ValueError):
        trail_mult = 0.75
    if not math.isfinite(trail_mult) or trail_mult <= 0:
        trail_mult = 0.75
    trail_distance = max(risk * trail_mult, noise or 0.0, TICK)
    return {
        "planVersion": str(scenario.get("planVersion") or ""),
        # The durable server tuple controls all ledger and broker decisions.
        # decisionId remains presentation-only for older operator displays.
        "entryKey": _entry_key(scenario),
        # R46: 正本の凍結スコープと、この周期で実際に武装している許可リストの
        # **積集合**。下流は狭める方向にしか動けないので、ここで増えることはない。
        # 手動建玉のある口座を外した周期は、凍結スコープではなく cfg 側が狭い。
        "accountScope": _frozen_account_scope(scenario, cfg),
        "scenarioId": str(scenario.get("scenarioId") or ""),
        "fingerprint": str(scenario.get("fingerprint") or ""),
        "evidenceHash": str(scenario.get("evidenceHash") or ""),
        "marketCycleId": str(scenario.get("marketCycleId") or ""),
        "decisionId": str(scenario.get("decisionId") or scenario.get("scenarioId") or ""),
        "symbol": str(scenario.get("symbol") or _setting("NQX_SYMBOL", cfg, contract_month.symbol())),
        "model": scenario.get("_displayModel") or scenario.get("model"),
        "grade": scenario.get("grade"),
        "side": side,
        "qty": qty,
        "entry": _tick(entry),
        "initialStop": _tick(stop),
        "riskPoints": _tick(risk),
        "riskDollars": round(risk_dollars, 2),
        "riskCapPoints": round(cap_points, 2),
        "riskCapDollars": round(cap_dollars, 2),
        "riskCapSource": cap_source,
        "tp1": targets[0],
        "finalTarget": targets[-1],
        "targets": targets,
        "legs": [{"id": "TP1", "qty": leg_split[0], "target": targets[0]},
                 {"id": "RUNNER", "qty": leg_split[1], "target": targets[-1]}],
        "targetR": scenario.get("targetR") or [],
        "trailDistance": _tick(trail_distance),
        "mode": "SPLIT_BRACKETS_TP1_RUNNER",
        # R47: ULTRA の目印と、order.py --ultra-drawdown へ渡す上限。
        "ultra": ultra_mode,
        "ultraDrawdown": cap_dollars if ultra_mode else None,
        # R48: モデル別スコアカードの特徴量分離用。凍結時点の decision.evidence
        # (CLASSIC_TS / LONDON_RANGE_SWEEP / DISP_RESEARCH_1_5X 等)を写す。
        # 表示・集計専用 — 発注判定はこのフィールドを読まない。
        "decisionEvidence": [str(x) for x in (
            (((bundle or {}).get("evaluation") or {}).get("decision") or {})
            .get("evidence") or [])][:16],
    }


def _read_ledger(path: str = LEDGER_FILE) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    records: List[Dict[str, Any]] = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line_no, raw in enumerate(fh, 1):
                if not raw.strip():
                    continue
                try:
                    record = json.loads(raw)
                except json.JSONDecodeError as exc:
                    return records, f"ledger line {line_no} is invalid JSON: {exc}"
                if not isinstance(record, dict) or not record.get("key") or not record.get("status"):
                    return records, f"ledger line {line_no} has no key/status"
                records.append(record)
    except FileNotFoundError:
        pass
    except OSError as exc:
        return records, f"ledger unavailable: {exc}"
    return records, None


def _append_ledger(record: Dict[str, Any], path: str = LEDGER_FILE) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    payload = dict(record)
    payload.setdefault("time", _iso_now())
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def _latest(records: Iterable[Dict[str, Any]], key: str, statuses: Iterable[str]) -> Optional[Dict[str, Any]]:
    wanted = set(statuses)
    for record in reversed(list(records)):
        if record.get("key") == key and record.get("status") in wanted:
            return record
    return None


#: HALT を後から解く記録。ENTRY_RECOVERED は engine 自身の回復(ブローカー不在証明)、
#: HALT_CLEARED は **人が** `--clear-halt` で理由と根拠を残して解いたもの(R52)。
HALT_CLEARING_STATUSES = ("ENTRY_RECOVERED", "HALT_CLEARED")
#: R76: 送信が「失敗/不明」だった ENTRY の HALT は、後周期にブローカーのブラケット構造から
#: 経路を凍結し直せたとき(送信が **起きていた** 証明)にも解ける。その行は status が
#: ENTRY_SENT / ENTRY_PARTIAL_FILL(凍結プランとして採用される必要がある)なので、解除の
#: 印は status ではなくこのフィールドで持つ。
HALT_RESOLUTION_REBOUND = "ROUTE_REBOUND_FROM_BROKER"
#: R84: 追撃の送信が UNKNOWN で HALT を書いた後、**コミットが成立した**ときの解除印。
#: コミットは「注文が実際に出て、ブローカーの構造で束縛できた」証明そのもの
#: (R76 の rebound と同じ理屈)。これが無いと、回復したのに HALT が残って以後の
#: 新規 FLAT ENTRY が全部塞がる —— `_clears_halt` は status か haltResolution しか見ない。
HALT_RESOLUTION_PYRAMID_COMMIT = "PYRAMID_COMMITTED_FROM_BROKER"


def _clears_halt(record: Dict[str, Any]) -> bool:
    return (record.get("status") in HALT_CLEARING_STATUSES
            or bool(str(record.get("haltResolution") or "").strip()))


MANAGEMENT_HALT_ACTIONS = {"MODIFY", "FLATTEN"}


def _is_management_halt(record: Dict[str, Any]) -> bool:
    """建玉管理(MODIFY / FLATTEN の action dict)で記録された HALT か。"""
    action = record.get("action")
    return (isinstance(action, dict)
            and str(action.get("action") or "").upper() in MANAGEMENT_HALT_ACTIONS)


def _has_halt(records: Iterable[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    recovered = set()
    position_closed_later = False   # 走査は新→旧。FLAT 記録より古い管理系 HALT は失効
    for record in reversed(list(records)):
        status = record.get("status")
        if _clears_halt(record):
            recovered.add(str(record.get("key") or ""))
        if status == "POSITION_GENERATION" and record.get("open") is False:
            position_closed_later = True
        if status == "HALT":
            if str(record.get("key") or "") in recovered:
                continue
            # R52: MODIFY/FLATTEN の HALT は「その建玉が生きている間」だけ新規を塞ぐ。
            # key が `<entryKey>:<口座>:MODIFY:<SL>:<qty>` なので ENTRY_RECOVERED では
            # 消えず、建玉が決済され FLAT になった後も新規判定を止め続けた
            # (2026-09-05 03:54 実測)。建玉が閉じた記録(POSITION_GENERATION open=False)
            # より古い管理系 HALT は無視する。KILL の FLATTEN(action が文字列)は従来どおり。
            if position_closed_later and _is_management_halt(record):
                continue
            return record
    return None


def list_halts(ledger_path: str = LEDGER_FILE) -> List[Dict[str, Any]]:
    """台帳の HALT を新しい順に、解除済みかどうか付きで返す(表示・監査用)。"""
    records, error = _read_ledger(ledger_path)
    if error:
        raise RuntimeError(error)
    cleared_keys = set()
    position_closed_later = False
    rows: List[Dict[str, Any]] = []
    for index in range(len(records) - 1, -1, -1):
        record = records[index]
        status = record.get("status")
        if _clears_halt(record):
            cleared_keys.add(str(record.get("key") or ""))
        if status == "POSITION_GENERATION" and record.get("open") is False:
            position_closed_later = True
        if status == "HALT":
            key = str(record.get("key") or "")
            action = record.get("action")
            rows.append({"index": index, "key": key, "time": record.get("time"),
                         "action": action.get("action") if isinstance(action, dict) else action,
                         "reason": str(record.get("reason") or "").splitlines()[0][:120],
                         "cleared": (key in cleared_keys
                                     or (position_closed_later and _is_management_halt(record)))})
    return rows


def clear_halt(key: str, reason: str, *, evidence: Optional[str] = None,
               operator: Optional[str] = None,
               ledger_path: str = LEDGER_FILE) -> Tuple[bool, Dict[str, Any]]:
    """人が HALT を解く(R52)。台帳の行は消さず、`HALT_CLEARED` を追記する。

    CLAUDE.md §6.6「原因を解消してから手動で再開する」の「手動」を、口頭や
    ファイル編集ではなく **監査可能な1行**にする。ENTRY の HALT は engine が
    ブローカー不在証明で自動回復するが、KILL/FLATTEN の HALT(送信結果不明)は
    同じ key の回復記録が原理的に書かれず、人が建玉を確認するまで新規を止め
    続ける —— それが設計であり、解くときも理由と根拠を残す。
    """
    key = str(key or "").strip()
    reason = str(reason or "").strip()
    if not key:
        return False, {"reason": "HALT_KEY_REQUIRED"}
    if not reason:
        return False, {"reason": "HALT_CLEAR_REASON_REQUIRED"}
    records, error = _read_ledger(ledger_path)
    if error:
        return False, {"reason": f"LEDGER_UNREADABLE: {error}"}
    halt_index = next((i for i in range(len(records) - 1, -1, -1)
                       if records[i].get("status") == "HALT"
                       and str(records[i].get("key") or "") == key), None)
    if halt_index is None:
        return False, {"reason": "HALT_NOT_FOUND"}
    if any(_clears_halt(r) and str(r.get("key") or "") == key
           for r in records[halt_index + 1:]):
        return False, {"reason": "HALT_ALREADY_CLEARED"}
    record = {"key": key, "status": "HALT_CLEARED", "action": "HALT_CLEARED",
              "reason": reason, "evidence": evidence, "operator": operator,
              "haltTime": records[halt_index].get("time"),
              "haltAction": records[halt_index].get("action")}
    _append_ledger(record, ledger_path)
    return True, record


def _stable_broker_snapshot(position_query, order_query, symbol, frozen_ids,
                            attempts: int = 3):
    """R53: 契約の component skew に収まる三点読み(before/orders/after)を取る。

    CrossTrade の応答時間は同じ口座・同じ照会でも 0.6〜6.2 秒とばらつく
    (2026-09-07 実測)。遅い方を引くと orders と after の観測時刻が
    ``maxComponentSkewSec``(2秒)を超え、``build_broker_observation`` が
    ``BROKER_OBSERVATION_COMPONENT_SKEW`` を投げて回復が落ちる。ブローカーの
    レイテンシは観測の質ではなく取得の運なので、**契約は緩めず取り直す**。

    許容内に収まらないまま attempts を使い切ったら最後の観測をそのまま返す。
    判定は従来どおり ``build_broker_observation`` 側で行い、ここでは何も
    自己申告しない。
    """
    import nqx_state
    max_skew = float(execution_contract.CONTRACT["brokerObservation"]["maxComponentSkewSec"])
    triple = None
    for _ in range(max(1, attempts)):
        before = position_query(symbol)
        orders = _query_orders_with_ids(order_query, symbol, frozen_ids)
        after = position_query(symbol)
        triple = (before, orders, after)
        orders_at = nqx_state._iso_ms(str((orders or {}).get("observedAt") or ""))
        after_at = nqx_state._iso_ms(str((after or {}).get("observedAt") or ""))
        if not orders_at or not after_at:
            continue
        if abs((after_at - orders_at).total_seconds()) <= max_skew:
            break
    return triple


def _claim_recovery_symbol(claim_view: Any, current_symbol: str) -> str:
    """R102c: claim の executionIntent.symbol(無ければ今の限月)。"""
    intent = claim_view.get("executionIntent") if isinstance(claim_view, dict) else None
    if isinstance(intent, dict) and str(intent.get("symbol") or "").strip():
        return str(intent.get("symbol")).strip()
    return str(current_symbol)


def _recover_entry_from_current_broker(claim, journal, position_query,
                                       order_query, symbol):
    """Production R22 startup recovery using a fresh stable broker snapshot."""
    import nqx_state
    frozen = claim.get("routeSnapshot") if isinstance(claim.get("routeSnapshot"), list) else []
    frozen_ids = sorted(str(row.get("orderId")) for row in frozen
                        if isinstance(row, dict) and row.get("state") == "ACCEPTED"
                        and row.get("orderId"))
    # R40: 経路 identity を **一度も取れなかった** 送信は、ここで恒久的に詰む。
    # CrossTrade は本文に注文IDを返さないので、identity は発注前後の注文
    # スナップショットの差分でしか束縛できない。その照会が落ちた周期
    # (例: 行の contractId が未設定の allowlist に弾かれる) では routeSnapshot
    # が全行 UNKNOWN / orderId=null になり、frozen_ids が必ず空になる。
    # 空のまま return すると **以後どの周期でも同じ空**になるため、claim は
    # 永久に宙吊りのままで新規発注が全部止まる(2026-08-25 に実際に発生)。
    #
    # CLAUDE.md §6 は解放条件を identity ではなく **不在の証明** で定義している:
    # 「新鮮な観測・qty=0・未終端の注文が1本も無い」。identity が無い経路は
    # まさにこの条件でしか終端できないので、その一本道を実装する。
    # ACCEPTED 行が1つでもあるなら従来どおり ID ごとの終端証明を要求する。
    identity_bound = any(isinstance(row, dict) and row.get("state") == "ACCEPTED"
                         for row in frozen)
    if not frozen_ids and identity_bound:
        # ACCEPTED なのに ID が無い = 束縛の取りこぼし。推測で解放しない。
        return False, {"reason": "ENTRY_RECOVERY_FROZEN_IDS_MISSING"}
    before, orders, after = _stable_broker_snapshot(
        position_query, order_query, symbol, frozen_ids)
    terminal = set(execution_contract.CONTRACT["brokerObservation"]["terminalStates"])
    rows = [row for row in (orders or {}).get("orders") or [] if isinstance(row, dict)]
    if (not isinstance(before, dict) or before.get("verified") is not True
            or not isinstance(after, dict) or after.get("verified") is not True
            or int(before.get("qty") or 0) != 0 or int(after.get("qty") or 0) != 0
            or not isinstance(orders, dict) or orders.get("verified") is not True):
        return False, {"reason": "ENTRY_RECOVERY_CURRENT_BROKER_PROOF_INVALID"}
    # R53: ブローカーが解決できなくなった凍結ID(古い注文が口座から消えた場合)は、
    # ID による終端証明を **永久に** 組めない。identity を取れなかった経路と同じで、
    # 残るのは不在の証明だけなので、そちらへ落とす。ここを塞いだままにすると
    # claim が恒久的に宙吊りになり新規が全部止まる(2026-09-07 に実際に発生)。
    unresolved = {str(value) for value in (orders.get("unresolvedOrderIds") or [])}
    provable_ids = [order_id for order_id in frozen_ids if order_id not in unresolved]
    if provable_ids:
        if not all(len([row for row in rows
                        if str(row.get("orderId") or "") == order_id
                        and str(row.get("status") or "").upper() in terminal]) == 1
                   for order_id in provable_ids):
            return False, {"reason": "ENTRY_RECOVERY_CURRENT_BROKER_PROOF_INVALID"}
    if len(provable_ids) != len(frozen_ids) or not frozen_ids:
        # identity 不明・解決不能の経路。ブローカーが「空である」ことだけが終端の
        # 根拠になるので、建玉ゼロ(上で確認済み)に加えて **未終端の注文が1本も無い**
        # ことを要求する。1本でも生きていれば従来どおり解放しない。
        active = [row for row in rows
                  if str(row.get("status") or "").upper() not in terminal]
        if active or (orders.get("activeOrders") or []) or int(orders.get("openCount") or 0) != 0:
            return False, {"reason": "ENTRY_RECOVERY_BROKER_NOT_EMPTY"}
    published, detail = nqx_state.publish_broker_observation(
        after, orders, claim.get("executionIntentHash"),
        before_position=before, after_position=after)
    if not published:
        return False, detail
    snapshot = detail.get("observation") if isinstance(detail, dict) else None
    recovered, detail = nqx_state.recover_entry_claim(
        claim.get("entryKey"), journal.get("claimToken"), snapshot)
    # R53: 凍結IDのどれかをブローカーが二度と解決できないなら、DO 側の
    # `entryRecoveryProof`(ACCEPTED 行ごとの終端照合)は原理的に永久に通らない。
    # 呼び出し側がその一件を DO の不在ベース解放(`staleEntryClaimReleasable`)へ
    # 委ねられるよう、事実だけを detail に載せる。ここでは何も自己申告しない。
    if not recovered and isinstance(detail, dict) and unresolved & set(frozen_ids):
        detail = {**detail, "identityUnresolvable": True}
    return recovered, detail


def _recover_management_from_current_broker(claim, journal, position_query,
                                            order_query, _symbol):
    import nqx_state
    return nqx_state.recover_management_from_broker(
        claim.get("managementKey"), journal.get("claimToken"), claim,
        position_query=position_query, orders_query=order_query)


def _position_side(position: Dict[str, Any]) -> Optional[str]:
    raw = str(position.get("side") or "").upper()
    return {"LONG": "BUY", "BUY": "BUY", "SHORT": "SELL", "SELL": "SELL"}.get(raw)


def management_action(plan: Dict[str, Any], position: Dict[str, Any], bundle: Dict[str, Any],
                      state: Optional[Dict[str, Any]] = None,
                      cfg: Optional[Dict[str, str]] = None,
                      now: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
    """現在の建玉に対する次の一手を返す。外部状態を変更しない。"""
    state = state or {}
    side = plan["side"]
    if _position_side(position) != side:
        return {"action": "HALT", "reason": "broker position side differs from frozen plan"}
    qty = int(position.get("qty") or 0)
    if qty <= 0:
        return None
    price = _price_from_bundle(bundle)
    if price is None:
        return {"action": "HALT", "reason": "current price unavailable"}
    entry = _num(position.get("avgEntry")) or plan["entry"]
    initial_stop = plan["initialStop"]
    if side == "BUY":
        reached_stop = price <= initial_stop
        reached_tp1 = price >= plan["tp1"]
        reached_final = price >= plan["finalTarget"]
    else:
        reached_stop = price >= initial_stop
        reached_tp1 = price <= plan["tp1"]
        reached_final = price <= plan["finalTarget"]

    flatten_reason = None
    if reached_final:
        flatten_reason = "final target reached while position remains open"
    elif reached_stop:
        flatten_reason = "structure stop breached while position remains open"
    elif bundle.get("forceFlatten") is True:
        flatten_reason = "forceFlatten flag"
    elif kill_enabled(cfg):
        flatten_reason = "NQX_AUTOTRADE_KILL"
    elif _session_flatten_due(now, cfg):
        flatten_reason = "configured session cutoff"
    if flatten_reason:
        return {"action": "FLATTEN", "qty": qty, "reason": flatten_reason}

    # 全量が残っている間は、最初から別々のTPを持つ broker-side OCO を
    # 触らない。ここで全量modifyを出すとTP1を消してしまう。
    if qty >= plan["qty"]:
        return None
    # TP1 が外れた後に残るのは runner 脚ちょうど。2枚固定なら1枚、ULTRA の
    # 比率分割ならその脚の枚数。半端な枚数は誰が持っているのか分からないので
    # 推測せず HALT する。
    runner_qty = 1
    for leg in plan.get("legs") or []:
        if isinstance(leg, dict) and str(leg.get("id") or "").upper() == "RUNNER":
            try:
                runner_qty = int(leg.get("qty") or 1)
            except (TypeError, ValueError):
                runner_qty = 1
            break
    if qty != runner_qty:
        return {"action": "HALT", "reason": "unexpected split position quantity"}
    if plan.get("legIdentityKnown") is False:
        return {"action": "HALT",
                "reason": "runner-only leg identity is unknown; runner inference prohibited"}
    # R35: ここへ来た時点で **TP1 は約定済み**。直上の `qty != runner_qty` で
    # 建玉が runner 分まで減っていることを確定させているからである。
    #
    # 以前はここで `if not reached_tp1: return None` としていた。reached_tp1 は
    # **現在価格**から計算される(price >= plan["tp1"])ので、TP1 約定後に価格が
    # TP1 を割り込んで戻ると False になり、**runner の SL が初期構造 SL のまま
    # 放置される**。TP1 で確定した利益を runner の満額リスクが打ち消す窓が、
    # 警告もログも無しに開いていた。
    #
    # 建玉の枚数は約定の事実そのもので、価格のような可変値ではない。
    # 事実が分かっているのに、その後で価格に再確認させる理由が無い。

    current_stop = _num(state.get("stop"))
    if current_stop is None:
        current_stop = initial_stop
    filled_at = _parse_at(position.get("filledAt"))
    best = _extreme(bundle, side, filled_at)
    distance = max(_num(plan.get("trailDistance")) or TICK, TICK)
    # BE の床は建値そのものではなく建値 +1pt(SELL は −1pt)。
    # 建値ちょうどで刈られると手数料と滑りの分だけ負けで終わるため、
    # runner が「最悪でも損にならない」状態を金額で成立させる。
    # 床を上げても単調性(`improved`)と現在値との位置関係(`protective`)は
    # 従来どおり効くので、価格が建値+1pt へ届いていない周期は単に見送られ、
    # 既存の構造 SL がそのまま残る。
    floor = entry + BREAKEVEN_OFFSET_POINTS if side == "BUY" else entry - BREAKEVEN_OFFSET_POINTS
    # R104: BREAKEVEN_ONLY は極値からのトレールを出さず、床(建値±1pt)にだけ寄せる。
    # 床へ寄せた後は `improved` が二度と立たないので、以後の周期は自然に見送りになる。
    breakeven_only = str(plan.get("trailMode") or "").upper() == TRAIL_MODE_BREAKEVEN_ONLY
    # R78: 「現在値の正しい側」は 1 tick ではなく市場側バッファで判定する。
    # 送信は判定の数秒〜数分後で、その間に価格が stop を跨げば cancelandbracket は
    # 取消だけ成立して runner が裸になる(モジュール先頭 STOP_MARKET_BUFFER_POINTS_DEFAULT)。
    buffer = _stop_market_buffer(cfg)
    if side == "BUY":
        trailed = (best - distance) if best is not None else floor
        desired = floor if breakeven_only else max(floor, trailed)
        improved = desired > current_stop + TICK / 2
        protective = desired <= price - buffer
    else:
        trailed = (best + distance) if best is not None else floor
        desired = floor if breakeven_only else min(floor, trailed)
        improved = desired < current_stop - TICK / 2
        protective = desired >= price + buffer
    if not improved:
        return None
    # R37: 単調性(`current_stop` との比較)は見ていたが、**現在価格との位置関係**を
    # 一度も見ていなかった。`best` は終値ではなく高値/安値の極値なので、価格が
    # 走ってから普通に押しただけで `desired` が現在値を追い越す。
    #
    #   BUY / entry 29630 / risk 25pt(distance 18.75) で 29695 まで走り 29660 へ押すと
    #   desired = 29676.25 —— **LONG の損切りを現在値の 16.25pt 上へ置こうとする**。
    #   MNQ で 19pt の押しは日常なので、特殊局面ではない。
    #
    # 下流にこれを止める層は無い: `management_intent.build()` は tick と正数しか見ず、
    # `order.py --modify` に `--sl` の side 判定は無く(新規経路にはある)、
    # Worker の `authoritativeManagementIntent` も stop はクライアント値を採用する。
    # しかも送信は `cancelandbracket`(既存の保護注文を取消して張り直す)なので、
    # 逆側 SL が broker に拒否されると「取消成功・新規拒否」で **runner が保護注文
    # ゼロ**のまま残りうる。逆側になる周期は単に見送る —— 既存の SL は現在値の
    # 正しい側にあり、そのまま有効である。
    if not protective:
        # R70: トレール目標(極値 − 距離)が現在値を追い越した周期でも、建値+1pt の床が
        # 現在値の正しい側にあり、かつ既存 SL より改善なら **床だけ**は寄せる。
        # 2026-09-08 19:24 実測: TP1 後の押しで desired 29,632.25 > price 29,608 → 見送り →
        # runner の SL が初期構造 SL 29,568.5(建値 −22pt)のまま残った。TP1 直後の押しは
        # 日常なので、この分岐で毎回見送ると §4「qty=1 確認後に建値以上へ一度寄せる」が
        # 一度も実行されず、R35 が塞いだはずの窓(TP1 の利益を runner の満額リスクが
        # 打ち消す)が開いたままになる。床は現在値より下(SELL は上)にしか置かないので
        # R37 の逆側 SL は生じない。トレールは次に protective になった周期で従来どおり進む。
        if side == "BUY":
            floor_ok = floor <= price - buffer and floor > current_stop + TICK / 2
        else:
            floor_ok = floor >= price + buffer and floor < current_stop - TICK / 2
        if not floor_ok:
            return None
        desired = floor
    return {
        "action": "MODIFY", "qty": qty, "sl": _tick(desired),
        "tp": plan["finalTarget"],
        "reason": ("TP1 reached: breakeven only (management override, no trail)" if breakeven_only
                   else "TP1 reached: breakeven/trailing protection improved"),
    }


def _session_flatten_due(now: Optional[datetime], cfg: Optional[Dict[str, str]]) -> bool:
    if not _truthy(_setting("NQX_AUTOTRADE_SESSION_FLATTEN", cfg, "0")):
        return False
    raw = str(_setting("NQX_AUTOTRADE_SESSION_END_ET", cfg, "15:55"))
    try:
        hour, minute = (int(part) for part in raw.split(":", 1))
        cutoff = dt_time(hour, minute)
    except (ValueError, TypeError):
        return False
    current = now or datetime.now(timezone.utc)
    # ET offset is sufficient for the current August deployment. The explicit
    # switch keeps this behavior off unless the operator opts in.
    et = (current if current.tzinfo else current.replace(tzinfo=timezone.utc)) - timedelta(hours=4)
    return et.time() >= cutoff


#: R78: reconcile() が現在値を取るための既定の照会。`reconcile()` が呼び出しの間だけ
#: 差し込み、`_reconcile_one` を直接呼ぶテストは None(= 周期の価格)のまま。
_FRESH_PRICE_QUERY: Optional[Callable[[str], Optional[float]]] = None


def _fresh_price(query: Optional[Callable[[str], Optional[float]]], symbol: str) -> Optional[float]:
    """照会の失敗・非数値は None(周期の価格へ戻る)。例外で周期を止めない。"""
    if query is None:
        return None
    try:
        value = query(symbol)
    except Exception:  # noqa: BLE001 - 取れなければ従来どおり周期の価格
        return None
    number = _num(value)
    return number if number is not None and number > 0 else None


def _default_fresh_price_query(symbol: str) -> Optional[float]:
    """R78: TradingView CLI の quote(1〜2 秒)。`NQX_FRESH_QUOTE=0` で無効。

    3 分ループでも約定監視でも、送信直前の現在値はこれで取る。quote は前面ペインの
    銘柄を返すので、MNQ 以外なら使わない。
    """
    if not _truthy(_setting("NQX_FRESH_QUOTE", None, "1")):
        return None
    try:
        import tv_fetch
        _raw, parsed = tv_fetch._cli_json(["quote"], timeout=20)
    except Exception:  # noqa: BLE001
        return None
    # R102: quote は前面チャートの銘柄。発注先の限月そのものでなければ使わない(連続足
    # MNQ1! が別限月へロールしていた 2026-09-15、建玉管理の参照価格まで別限月になっていた)。
    try:
        wanted = symbol if contract_month.normalize_code(str(symbol or "")) else contract_month.symbol()
    except contract_month.ContractError:
        return None
    quote_symbol = str(parsed.get("symbol") or "")
    front = None
    if contract_month.is_continuous(quote_symbol):
        front = str(tv_fetch.chart_symbol_info().get("front_contract") or "") or None
    resolved = contract_month.resolved_contract(quote_symbol, front)
    if not resolved or not contract_month.same_contract(resolved, wanted):
        return None
    value = parsed.get("last")
    if value is None:
        value = parsed.get("close")
    return _num(value)


def _protective_rows(order_view: Any, side: str, account: Optional[str] = None) -> List[Dict[str, Any]]:
    """建玉と逆方向の **live** な保護注文行(口座が分かる行はその口座に絞る)。

    R80: SUSPENDED(OSO の子が親の約定を待つ状態)は建玉を守っていないので数えない。
    TP + Suspended 子 の状態は「保護 1 本」= 裸として R78 の修復対象になる。
    """
    import broker_status
    opposite = "SELL" if str(side).upper() == "BUY" else "BUY"
    rows: List[Dict[str, Any]] = []
    source = (order_view.get("activeOrders") or []) if isinstance(order_view, dict) else []
    for row in source:
        if not isinstance(row, dict):
            continue
        if str(row.get("action") or "").upper() != opposite:
            continue
        if str(row.get("status") or "").upper() not in broker_status.BROKER_LIVE_PROTECTIVE_STATES:
            continue
        row_account = str(row.get("accountId") or row.get("account") or "")
        if account and row_account and row_account != str(account):
            continue
        rows.append(row)
    return rows


NAKED_GRACE_SEC_DEFAULT = 30.0


def _naked_repair_policy(cfg: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """R102: 契約 ``contract.nakedRepair``。壊れていれば OFF(この節だけ止まる)。"""
    section = (execution_contract.CONTRACT.get("contract") or {}).get("nakedRepair")
    if not isinstance(section, dict):
        return {"mode": "OFF", "graceSec": NAKED_GRACE_SEC_DEFAULT}
    mode = str(section.get("mode") or "OFF").upper()
    if mode not in ("OFF", "SHADOW", "LIVE"):
        mode = "OFF"
    try:
        grace = float(section.get("graceSec", NAKED_GRACE_SEC_DEFAULT))
    except (TypeError, ValueError):
        grace = NAKED_GRACE_SEC_DEFAULT
    override = _setting("NQX_NAKED_REPAIR", cfg, None)
    if override is not None and str(override).upper() in ("OFF", "SHADOW", "LIVE"):
        mode = str(override).upper()
    return {"mode": mode, "graceSec": max(grace, 0.0)}


def _fill_age_seconds(position: Dict[str, Any], now: Optional[datetime]) -> Optional[float]:
    now = now or datetime.now(timezone.utc)
    raw = position.get("filledAt") if isinstance(position, dict) else None
    if not raw:
        return None
    try:
        moment = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return max((now - moment).total_seconds(), 0.0)


def _guard_naked_position(*, plan: Dict[str, Any], position: Dict[str, Any], order: Dict[str, Any],
                          records: List[Dict[str, Any]], plan_key: str, state: Dict[str, Any],
                          price: Optional[float], noise: Optional[float],
                          cfg: Optional[Dict[str, str]], symbol: str, account: Optional[str],
                          open_qty: int, query: Callable[[str], Dict[str, Any]],
                          broker_order_query: Callable[..., Dict[str, Any]],
                          claimer: Optional[Callable[[Dict[str, Any]], Tuple[bool, Dict[str, Any]]]],
                          execute: Callable[[List[str], bool], Tuple[int, str]],
                          ledger_path: str, live: bool, now: datetime) -> Tuple[bool, List[str]]:
    """R102: 所有した建玉に保護注文の OCO 組が 1 つも無ければ、同じ周期で SL/TP を張るか撤退する。

    2026-09-15 15:10、限月ずれの通過指値で約定した LONG 2 は SL を置けず(市場の逆側)、TP 2 本
    だけが残った。engine は所有権 UNKNOWN で 4 周期沈黙し、人が手で SL を置いた。ここでは:

      1. 周期の注文一覧で OCO 組(``broker_status.oco_pairs``)が 0 なら、約定からの経過が
         ``graceSec`` を超えているときだけ再照会して確かめる(送信直後は子行が見えない)。
      2. 0 組が確定したら、直前の stop(最後に受理された SL、無ければ初期構造 SL)が現在値の
         守れる側(市場側バッファ付き)ならそれを、そうでなければ現在値から max(1.0N, buffer)
         離した価格を SL にし、TP1(無ければ最終 TP)と 1 組で ``--modify --repair-naked`` を送る。
         TP が現在値の逆側なら FLATTEN。価格が無ければ FLATTEN。
      3. 送信後はブローカーを再照会し、確認できなければ HALT(無条件リトライしない)。

    戻り値 ``(stop_here, notes)``。stop_here=False なら通常の管理へ進む。
    mode: OFF=何もしない / SHADOW=判定を注記に出すだけ / LIVE=送る。
    """
    import broker_status

    now = now or datetime.now(timezone.utc)          # 本番の reconcile は now を渡さない
    policy = _naked_repair_policy(cfg)
    if policy["mode"] == "OFF" or open_qty <= 0:
        return False, []
    side = str(plan.get("side") or "").upper()
    if side not in ("BUY", "SELL") or not isinstance(order, dict) or order.get("verified") is not True:
        return False, []
    opposite = "SELL" if side == "BUY" else "BUY"
    account_label = str(account or position.get("accountId") or position.get("account") or "ACCOUNT_UNKNOWN")
    pairs, singles = broker_status.oco_pairs(order.get("orders") or [], account=account_label,
                                             expected_action=opposite, symbol=symbol)
    if pairs:
        return False, []
    age = _fill_age_seconds(position, now)
    if age is not None and age < policy["graceSec"]:
        return False, [f"naked check deferred: fill age {age:.0f}s < {policy['graceSec']:g}s grace "
                       f"(OCO pairs not visible yet)"]
    try:
        fresh = _query_orders_with_ids(broker_order_query, symbol, [])
    except Exception as exc:  # noqa: BLE001
        return True, [f"autotrade hold: naked check deferred (order query failed: {type(exc).__name__})"]
    if not isinstance(fresh, dict) or fresh.get("verified") is not True:
        return True, ["autotrade hold: naked check deferred (protective orders UNVERIFIED)"]
    pairs, singles = broker_status.oco_pairs(fresh.get("orders") or [], account=account_label,
                                             expected_action=opposite, symbol=symbol)
    if pairs:
        return False, []
    orphan = len(singles)
    key = f"{plan_key}:{account_label}:NAKED_REPAIR:{open_qty}"
    if _latest(records, key, {"MANAGEMENT_SENT", "FLATTEN_SENT", "HALT"}):
        return True, [f"autotrade hold: naked repair already attempted ({key}); broker OCO only"]
    buffer = _stop_market_buffer(cfg)
    previous = _num(state.get("stop")) if _num(state.get("stop")) is not None else _num(plan.get("initialStop"))
    structural = _tick(previous) if previous is not None else None
    stop = None
    if price is not None:
        protective = structural is not None and (
            structural >= price + buffer if side == "SELL" else structural <= price - buffer)
        if protective:
            stop = structural
        else:
            room = max(float(noise or 0.0), buffer)
            stop = _tick(price - room) if side == "BUY" else _tick(price + room)
    legs = plan.get("legs") if isinstance(plan.get("legs"), list) else []
    target = _num((legs[0] or {}).get("target")) if legs else None
    if target is None:
        target = _num(plan.get("finalTarget"))
    target_ok = (target is not None and price is not None
                 and (target >= price + buffer if side == "BUY" else target <= price - buffer))
    head = f"NAKED position {side} {open_qty} @{account_label}: 0 OCO pairs, {orphan} orphan protective row(s)"
    if policy["mode"] == "SHADOW":
        plan_text = (f"would MODIFY sl={stop} tp={target}" if stop is not None and target_ok
                     else "would FLATTEN (no protective stop/target can be placed)")
        return False, [f"naked (shadow): {head}; {plan_text} [not sent]"]
    if not live:
        return True, [f"autotrade proposal NAKED_REPAIR: {head}; "
                      + (f"MODIFY sl={stop} tp={target}" if stop is not None and target_ok else "FLATTEN")]
    if stop is None or not target_ok:
        flatten_action = {"action": "FLATTEN", "qty": open_qty, "repair": True, "naked": True,
                          "reason": f"R102 naked position: no protective stop/target can be placed ({head})"}
        ok, detail = _execute_action(_command_for_flatten(account), True, execute)
        if ok:
            verified, verify_detail = _verify_after_action(query, symbol, "FLATTEN")
            if verified:
                _append_ledger({"key": key, "status": "FLATTEN_SENT", "action": flatten_action,
                                "planEntryKey": plan_key, "plan": plan, "result": detail,
                                "naked": {"orphanRows": orphan, "price": price}}, ledger_path)
                return True, [f"autotrade repair NAKED flatten sent: {head}"]
            detail = verify_detail
        _append_ledger({"key": key, "status": "HALT", "action": flatten_action,
                        "planEntryKey": plan_key, "plan": plan,
                        "reason": f"R102 naked flatten failed: {detail}"}, ledger_path)
        return True, [f"AUTOTRADE HALT: R102 naked flatten failed: {detail}"]
    repair_action = {"action": "MODIFY", "qty": open_qty, "sl": stop, "tp": target, "repair": True,
                     "naked": True, "reason": f"R102 naked position: placing SL/TP ({head})"}
    generation = _position_generation(position)
    try:
        intent = management_intent.build(
            account_id=position.get("accountId") or position.get("account"), symbol=plan["symbol"],
            position_generation=generation, side=plan["side"], qty=open_qty,
            stop=repair_action["sl"], target=repair_action["tp"])
    except ValueError as exc:
        return True, [f"AUTOTRADE HALT: R102 naked repair intent invalid ({exc})"]
    if claimer is None:
        import nqx_state
        claimer = nqx_state.claim_management
    try:
        claim_ok, claim = claimer(intent)
    except Exception as exc:  # noqa: BLE001
        claim_ok, claim = False, {"reason": f"{type(exc).__name__}: {exc}"}
    if (not claim_ok or not isinstance(claim, dict)
            or claim.get("managementKey") != management_intent.management_key(intent)
            or claim.get("managementIntentHash") != management_intent.intent_hash(intent)
            or not claim.get("claimToken")):
        return True, [f"AUTOTRADE HALT: R102 naked repair claim unavailable ({claim})"]
    journal = {"managementKey": claim["managementKey"], "claimToken": str(claim["claimToken"]),
               "managementIntentHash": str(claim["managementIntentHash"]), "managementIntent": intent}
    try:
        _append_ledger({"key": key, "status": "MANAGEMENT_CLAIMED", "action": repair_action,
                        "planEntryKey": plan_key, "plan": plan,
                        "managementClaimJournal": journal}, ledger_path)
    except OSError as exc:
        return True, [f"AUTOTRADE HALT: R102 naked repair journal failed ({exc})"]
    args = _command_for_modify(plan, repair_action, generation, claim, account=account, last=price)
    args.append("--repair-naked")
    ok, detail = _execute_action(args, True, execute)
    if ok:
        verified, verify_detail = _verify_after_action(query, symbol, "MODIFY", plan.get("side"))
        if verified:
            _append_ledger({"key": key, "status": "MANAGEMENT_SENT", "action": repair_action,
                            "planEntryKey": plan_key, "plan": plan, "result": detail,
                            "managementClaimJournal": journal,
                            "naked": {"orphanRows": orphan, "price": price}}, ledger_path)
            return True, [f"autotrade repair NAKED management_sent: SL {stop} / TP {target} placed "
                          f"for {side} {open_qty} ({orphan} orphan row(s) replaced)"]
        detail = verify_detail
    _append_ledger({"key": key, "status": "HALT", "action": repair_action,
                    "planEntryKey": plan_key, "plan": plan,
                    "reason": f"R102 naked repair failed: {detail}",
                    "managementClaimJournal": journal}, ledger_path)
    return True, [f"AUTOTRADE HALT: R102 naked repair failed: {detail}"]


def _repair_unprotected_runner(*, plan: Dict[str, Any], position: Dict[str, Any],
                               action: Dict[str, Any], failure: str, failed_key: str,
                               plan_key: str, failed_journal: Optional[Dict[str, Any]],
                               previous_stop: Optional[float], price: Optional[float],
                               cfg: Optional[Dict[str, str]], symbol: str,
                               account: Optional[str], open_qty: int,
                               query: Callable[[str], Dict[str, Any]],
                               broker_order_query: Callable[..., Dict[str, Any]],
                               claimer: Optional[Callable[[Dict[str, Any]], Tuple[bool, Dict[str, Any]]]],
                               execute: Callable[[List[str], bool], Tuple[int, str]],
                               ledger_path: str,
                               expected_pairs: int = 1,
                               modify_extra_args: Optional[List[str]] = None,
                               outcome: Optional[Dict[str, Any]] = None,
                               ) -> Tuple[bool, List[str], str]:
    """R78: 張り替え(cancelandbracket)が片脚拒否で終わり runner が裸/片脚のとき、同じ周期で直す。

    2026-09-11 01:40:48 実測: 旧 SL 29,301 の取消は成立し、新 BUY STOP 29,174.25 は
    「価格がすでに上」で拒否。TP 指値だけが残り、SHORT 2 枚が SL 無しのまま HALT で
    止まった(order.py の ULTRA ガードも「逆方向 2 本」を要求するため、次周期の修復
    modify 自体を拒否する構造だった)。ここでは:

      1. ブローカーの保護注文を数える。逆方向 active 行が 2 本以上なら対象外
         (ブラケットは存在する。新旧どちらかは不明なので従来どおり HALT で人が確認)。
      2. 1 本以下なら **直前の stop**(最後に受理された SL、無ければ初期構造 SL)を
         送信直前の現在値に対して市場側バッファ付きで検査し、守れる側なら同じ claim
         経路で MODIFY を 1 回だけ送る。守れない側(構造 SL を割った価格で裸)なら
         FLATTEN で撤退する。
      3. 修復に成功したら元の失敗行は HALT ではなく `MODIFY_FAILED_REPAIRED`
         (監査用・新規を塞がない)。失敗したら呼び出し側が従来どおり HALT を書く。

    戻り値 ``(handled, notes, halt_prefix)``。handled=False のとき呼び出し側は
    ``halt_prefix + failure`` を理由に HALT する。
    """
    try:
        view = _query_orders_with_ids(broker_order_query, symbol, [])
    except Exception as exc:  # noqa: BLE001
        return False, [], f"R78 repair skipped (order query failed: {type(exc).__name__}): "
    if not isinstance(view, dict) or view.get("verified") is not True:
        return False, [], "R78 repair skipped (protective orders UNVERIFIED): "
    live_rows = _protective_rows(view, plan["side"], account)
    # R84: 合成建玉では「守られている本数」は 2 本ではなく **生きている脚の数 × 2** 本。
    # 1 に固定したままだと、2 トランシェのうち片方が裸になっても「ブラケットはある」と
    # 読んで修復せずに HALT する。単一プランは expected_pairs=1 で従来どおり。
    expected_rows = 2 * max(1, int(expected_pairs or 1))
    if len(live_rows) >= expected_rows:
        return False, [], ""
    unprotected = f"RUNNER UNPROTECTED ({len(live_rows)} protective row(s)): "
    side = str(plan["side"]).upper()
    qty = int(action.get("qty") or open_qty or 0)
    if qty <= 0 or price is None:
        return False, [], unprotected + "R78 repair impossible (qty/price unknown): "
    buffer = _stop_market_buffer(cfg)
    fallback = _tick(previous_stop) if _num(previous_stop) is not None else None
    protective = fallback is not None and (
        fallback >= price + buffer if side == "SELL" else fallback <= price - buffer)
    account_label = account or "ACCOUNT_UNKNOWN"
    if not protective:
        # 直前の stop すら現在値の逆側 = 構造 SL を割った価格で裸。撤退する。
        flatten_action = {"action": "FLATTEN", "qty": qty,
                          "reason": "R78 repair: runner unprotected and previous stop is not protective",
                          "repair": True}
        flatten_key = f"{plan_key}:{account_label}:FLATTEN:{qty}"
        ok, detail = _execute_action(_command_for_flatten(account), True, execute)
        if ok:
            verified, verify_detail = _verify_after_action(query, symbol, "FLATTEN")
            if verified:
                _append_ledger({"key": failed_key, "status": "MODIFY_FAILED_REPAIRED", "action": action,
                                "planEntryKey": plan_key, "plan": plan, "reason": failure,
                                "repair": {"action": "FLATTEN", "key": flatten_key},
                                "managementClaimJournal": failed_journal}, ledger_path)
                _append_ledger({"key": flatten_key, "status": "FLATTEN_SENT", "action": flatten_action,
                                "planEntryKey": plan_key, "plan": plan, "result": detail}, ledger_path)
                if outcome is not None:
                    outcome.update({"action": "FLATTEN", "qty": qty})
                return True, [f"autotrade repair flatten sent: {flatten_action['reason']}"], ""
            detail = verify_detail
        _append_ledger({"key": flatten_key, "status": "HALT", "action": flatten_action,
                        "planEntryKey": plan_key, "plan": plan,
                        "reason": f"R78 repair flatten failed: {detail}"}, ledger_path)
        return False, [], unprotected + "R78 repair flatten failed: "
    repair_action = {"action": "MODIFY", "qty": qty, "sl": fallback, "tp": plan["finalTarget"],
                     "reason": "R78 repair: replacement bracket rejected; restoring previous stop",
                     "repair": True}
    repair_key = f"{plan_key}:{account_label}:MODIFY:{repair_action['sl']}:{qty}"
    generation = _position_generation(position)
    try:
        intent = management_intent.build(
            account_id=position.get("accountId") or position.get("account"), symbol=plan["symbol"],
            position_generation=generation, side=plan["side"], qty=qty,
            stop=repair_action["sl"], target=repair_action["tp"])
    except ValueError as exc:
        return False, [], unprotected + f"R78 repair intent invalid ({exc}): "
    if claimer is None:
        import nqx_state
        claimer = nqx_state.claim_management
    try:
        claim_ok, claim = claimer(intent)
    except Exception as exc:  # noqa: BLE001
        claim_ok, claim = False, {"reason": f"{type(exc).__name__}: {exc}"}
    if (not claim_ok or not isinstance(claim, dict)
            or claim.get("managementKey") != management_intent.management_key(intent)
            or claim.get("managementIntentHash") != management_intent.intent_hash(intent)
            or not claim.get("claimToken")):
        return False, [], unprotected + f"R78 repair claim unavailable ({claim}): "
    journal = {"managementKey": claim["managementKey"], "claimToken": str(claim["claimToken"]),
               "managementIntentHash": str(claim["managementIntentHash"]), "managementIntent": intent}
    try:
        _append_ledger({"key": repair_key, "status": "MANAGEMENT_CLAIMED", "action": repair_action,
                        "planEntryKey": plan_key, "plan": plan,
                        "managementClaimJournal": journal}, ledger_path)
    except OSError as exc:
        return False, [], unprotected + f"R78 repair journal failed ({exc}): "
    args = _command_for_modify(plan, repair_action, generation, claim, account=account, last=price)
    args += list(modify_extra_args or [])
    ok, detail = _execute_action(args, True, execute)
    if ok:
        verified, verify_detail = _verify_after_action(query, symbol, "MODIFY", plan.get("side"))
        if verified:
            _append_ledger({"key": failed_key, "status": "MODIFY_FAILED_REPAIRED", "action": action,
                            "planEntryKey": plan_key, "plan": plan, "reason": failure,
                            "repair": {"action": "MODIFY", "key": repair_key, "sl": repair_action["sl"]},
                            "managementClaimJournal": failed_journal}, ledger_path)
            _append_ledger({"key": repair_key, "status": "MANAGEMENT_SENT", "action": repair_action,
                            "planEntryKey": plan_key, "plan": plan, "result": detail,
                            "managementClaimJournal": journal}, ledger_path)
            if outcome is not None:
                # R84: 合成プランの呼び出し側は、この対を凍結し直さないと次の周期に
                # 所有権を失う。何を張り替えたのかをそのまま返す。
                outcome.update({"action": "MODIFY", "qty": qty, "sl": repair_action["sl"],
                                "tp": repair_action["tp"], "result": detail})
            return True, [f"autotrade repair management_sent: previous stop {repair_action['sl']} "
                          "restored after replacement rejection"], ""
        detail = verify_detail
    _append_ledger({"key": repair_key, "status": "HALT", "action": repair_action,
                    "planEntryKey": plan_key, "plan": plan,
                    "reason": f"R78 repair failed: {detail}",
                    "managementClaimJournal": journal}, ledger_path)
    return False, [], unprotected + "R78 repair failed; "


class _ReconcileLock:
    """R78: reconcile のプロセス間ロック(3 分ループと fill_watch の排他)。

    台帳は 1 行 append だが、同じ建玉に対する判断が 2 プロセスで同時に走ると
    claim / 送信が重なりうる。ロックファイルは台帳と同じ場所(`<ledger>.lock`)。
    取れなければ **待ってから諦める**(呼び出し側は何も送らずに注記だけ返す)。
    """

    def __init__(self, path: str, wait_sec: float):
        self.path = path
        self.wait_sec = max(0.0, float(wait_sec))
        self.handle = None
        self.acquired = False

    def __enter__(self) -> bool:
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        deadline = time.monotonic() + self.wait_sec
        while True:
            try:
                self.handle = open(self.path, "a+b")
                if os.name == "nt":
                    import msvcrt
                    self.handle.seek(0)
                    msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.acquired = True
                return True
            except OSError:
                if self.handle is not None:
                    try:
                        self.handle.close()
                    except OSError:
                        pass
                    self.handle = None
                if time.monotonic() >= deadline:
                    return False
                time.sleep(0.5)

    def __exit__(self, *_exc: Any) -> None:
        if self.handle is None:
            return
        try:
            if self.acquired:
                if os.name == "nt":
                    import msvcrt
                    self.handle.seek(0)
                    msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            try:
                self.handle.close()
            except OSError:
                pass
            self.handle = None
            self.acquired = False


#: order.py の子プロセスに与える上限。**HTTP 予算より必ず長く取ること。**
#:
#: R37: ここは 30 秒だった。しかし order.py の --confirm 経路は逐次 HTTP を
#: 最大 6 本使い(各 15 秒、nqx_state のみ 12 秒)、adapter フォールバックが挟まると
#: 更に伸びる —— 同ファイルの他所のコメントが自認するとおり最悪 114 秒である。
#: 30 秒で kill すると、webhook には注文が届いているのに親が経路 envelope ごと
#: 捨て、次サイクルで ownership_binder の accepted=0 → 恒久 hold に落ちる
#: (実弾建玉が broker 側 OCO だけを頼りにエンジンの管理外へ出る)。
#: 監視周期 3 分を超えない範囲で、予算に余裕を足した値にする。
ORDER_SUBPROCESS_TIMEOUT_SEC = 150


def _run_order(args: List[str], confirm: bool) -> Tuple[int, str]:
    command = [sys.executable, os.path.join(BASE, "order.py")] + list(args)
    if confirm:
        command.append("--confirm")
    # R41: **エンコーディングを固定する。** `text=True` は Windows のロケール
    # (この運用機は cp932)で子の出力をデコードする。order.py は日本語を UTF-8 で
    # 出すので、チャンク境界次第で reader スレッドが UnicodeDecodeError を投げ、
    # **stdout も stderr も丸ごと空**のまま rc=1 が返る。エンジンはそれを
    # 「送信失敗/不明」と読んで HALT する —— 実際には送信ロジックは正常で、
    # 親が子の返事を読めていないだけ(2026-08-25 22:00 / 22:11 に発生。
    # 21:12 は偶然デコードできたので通っていた)。
    #
    # 経路 envelope も同じ経路で読むので、これは「理由が見えない」だけでなく
    # **送信済みの注文を見失う**事故になりうる。errors="replace" にして、
    # 壊れた1バイトのために全出力を失う経路そのものを塞ぐ。
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    try:
        proc = subprocess.run(command, capture_output=True,
                              encoding="utf-8", errors="replace",
                              timeout=ORDER_SUBPROCESS_TIMEOUT_SEC,
                              cwd=BASE, env=env, check=False)
    except subprocess.TimeoutExpired as exc:
        # R37: kill した時点で子は**既に送信済みかもしれない**。stdout に経路
        # envelope が出ていれば捨てずに残す —— 捨てると所有権を束縛できず、
        # 建玉が管理外のまま恒久 hold になる。制御は従来どおり HALT(再送しない)。
        partial = exc.stdout or ""
        if isinstance(partial, bytes):
            partial = partial.decode("utf-8", "replace")
        return 1, (f"order.py timed out after {ORDER_SUBPROCESS_TIMEOUT_SEC}s; "
                   f"partial stdout preserved:\n{partial}")
    except Exception as exc:  # noqa: BLE001 — an unknown send must HALT, never retry
        return 1, f"order.py invocation failed: {type(exc).__name__}: {exc}"
    stdout = proc.stdout or ""
    stderr = proc.stderr or ""
    if stderr:
        print(stderr.rstrip(), file=sys.stderr)
    # Confirmed routes reserve stdout for the canonical SNAPSHOT/FINAL
    # envelope. Any stderr diagnostic makes the outcome UNKNOWN even when the
    # child accidentally exits zero.
    #
    # R41: しかし stderr を **戻り値から落としてはいけない**。order.py の
    # ライブ経路は `sys.exit("ERROR: ENTRY_CLAIM_INVALID: ...")` のように
    # 失敗理由を stderr にだけ書く。捨てると台帳にも報告にも
    # `live send failed/unknown:` の後に **空文字**しか残らず、なぜ止まったか
    # 誰にも分からなくなる(2026-08-25 22:00 に発生。原因の切り分けに
    # 正本 state の照会が必要だった)。
    #
    # envelope の解析は `NQX_ROUTE_SNAPSHOT` / `NQX_ROUTE_FINAL` 行を探すので、
    # 末尾に区切り付きで足しても誤検出しない。
    if stderr:
        detail = f"{stdout}\n--- order.py stderr ---\n{stderr.rstrip()}" if stdout else stderr
    else:
        detail = stdout
    if confirm and stderr:
        return proc.returncode or 1, detail
    return proc.returncode, detail


R102_REJECT_CODES = ("ORDER_SYMBOL_NOT_CONTRACT", "CONTRACT_EXPIRY_NEAR", "CONTRACT_EXPIRED",
                     "CONTRACT_INVALID", "LAST_SYMBOL_MISMATCH", "LAST_PRICE_DIVERGENT",
                     "QUOTE_UNAVAILABLE", "ENTRY_LIMIT_THROUGH_MARKET", "STOP_WRONG_SIDE_OF_MARKET",
                     "CHART_SYMBOL_CONTINUOUS", "CHART_SYMBOL_MISMATCH", "CHART_SYMBOL_MISSING")


def _r102_reject_code(detail: Any) -> Optional[str]:
    """order.py の拒否出力に R102 の拒否コードが含まれていればそれを返す。"""
    text = str(detail or "")
    for code in R102_REJECT_CODES:
        if code in text:
            return code
    return None


def _contract_entry_guard(plan: Dict[str, Any], bundle: Dict[str, Any]) -> Dict[str, Any]:
    """R102: 限月の門(claim の**前**)。見送りは HALT でも claim でもない。

    (1) 満期の手前は新規を出さない、(2) プランの symbol が正本の限月である、(3) 価格の出所
    (bundle.sourceSymbol)が同じ限月である、(4) 指値のとき SL が公開価格の守れる側にある。
    成行の SL 距離は R90 の _market_stop_guard が見る。契約ブロックが壊れていれば fail-closed。
    """
    try:
        ok, reason = contract_month.entry_allowed()
        if not ok:
            return {"block": True, "reason": reason.split(":", 1)[0], "text": reason}
        if not contract_month.same_contract(str(plan.get("symbol") or ""), contract_month.symbol()):
            return {"block": True, "reason": "ORDER_SYMBOL_NOT_CONTRACT",
                    "text": f"plan symbol {plan.get('symbol')} != contract {contract_month.symbol()}"}
        source = str(bundle.get("sourceSymbol") or (bundle.get("snapshot") or {}).get("symbol") or "")
        if source:
            sym_ok, sym_reason = contract_month.chart_symbol_matches(source, bundle.get("sourceFrontContract"))
            if not sym_ok:
                return {"block": True, "reason": sym_reason.split(":", 1)[0], "text": sym_reason}
    except contract_month.ContractError as exc:
        return {"block": True, "reason": "CONTRACT_INVALID", "text": str(exc)}
    price = _price_from_bundle(bundle)
    stop = _num(plan.get("initialStop"))
    if (str(plan.get("entryOrderType") or "").upper() == "LIMIT" and price is not None
            and stop is not None):
        side = str(plan.get("side") or "").upper()
        wrong = (side == "BUY" and stop >= price) or (side == "SELL" and stop <= price)
        if wrong:
            return {"block": True, "reason": "STOP_WRONG_SIDE_OF_MARKET",
                    "text": f"{side} initialStop {stop} vs price {price} (broker would reject the stop)"}
    return {"block": False, "reason": None, "text": ""}


def _command_for_entry(plan: Dict[str, Any], bundle: Dict[str, Any],
                       entry_key: Optional[str] = None,
                       claim_token: Optional[str] = None,
                       intent_hash: Optional[str] = None) -> List[str]:
    args = ["--side", plan["side"].lower(), "--qty", str(plan["qty"]),
            "--sl", str(plan["initialStop"]),
            "--split-tp", ",".join(str(leg["target"]) for leg in plan["legs"]),
            "--symbol", plan["symbol"]]
    # R47: ULTRA プランは order.py の ULTRA エンベロープで判定させる。
    # --ultra-drawdown は凍結された残ドローダウン上限(min(残DD, 契約上限))。
    if plan.get("ultra"):
        args += ["--ultra", "--ultra-drawdown", str(plan["ultraDrawdown"])]
    # R46: 宛先は凍結スコープそのもの。order.py は許可リストの部分集合しか
    # 受け付けないので、これは「狭める」だけの指定になる。手動建玉のある口座を
    # 外した周期で、子プロセスが env の全口座へ送ってしまう経路を塞ぐ。
    scope = [str(value) for value in plan.get("accountScope") or [] if str(value)]
    if scope:
        args += ["--accounts", ",".join(scope)]
    price = _price_from_bundle(bundle)
    entry = plan["entry"]
    # Limit if it is still resting on the requested side; otherwise use the
    # observed quote as a market order and let order.py recheck slippage/risk.
    executable = price is not None and ((plan["side"] == "BUY" and entry >= price) or
                                        (plan["side"] == "SELL" and entry <= price))
    if executable:
        args += ["--market", "--last", str(price)]
        # R90 穴 2: 送信直前の order.py にも同じ下限(= 1.0N)を渡す。engine は公開価格で、
        # order.py は quote(取れれば)で「価格 → SL」の距離を検査する。
        if plan.get("marketStopMinPt") is not None:
            args += ["--min-stop-pt", str(plan["marketStopMinPt"])]
    else:
        args += ["--entry", str(entry)]
        if price is not None:
            # R102: 指値でも参照価格を渡す。order.py は通過指値(ENTRY_LIMIT_THROUGH_MARKET)と
            # SL の側(STOP_WRONG_SIDE_OF_MARKET)を、この価格か送信直前の quote で検査する。
            args += ["--last", str(price)]
    source_symbol = str(bundle.get("sourceContract") or bundle.get("sourceSymbol")
                        or (bundle.get("snapshot") or {}).get("symbol") or "")
    if source_symbol and not contract_month.is_continuous(source_symbol):
        # R102: 価格の出所(限月そのもの)。発注先と別の限月なら order.py が LAST_SYMBOL_MISMATCH で止める。
        # 連続足のまま解決できていない周期は添えない(order.py の quote 側の門は掛かる)。
        args += [f"--price-symbol={source_symbol}"]
    if entry_key and claim_token and intent_hash:
        # R52: token/key/hash は `--opt=value` の 1 要素で渡す。`secrets.token_urlsafe`
        # は先頭が `-` になり得て、別要素で渡すと argparse がオプションと誤認して
        # 「--claim-token: expected one argument」で dry-run が落ち HALT する
        # (2026-09-05 01:54 実測。A+ 候補を 1 本落とし、claim は CLAIMED のまま
        # 900 秒の stale 解放を待つことになった)。
        args += [f"--entry-key={entry_key}", f"--claim-token={claim_token}",
                 f"--intent-hash={intent_hash}", f"--plan-version={plan['planVersion']}"]
    return args


def _entry_order_type(plan: Dict[str, Any], bundle: Dict[str, Any]) -> str:
    price = _price_from_bundle(bundle)
    executable = price is not None and ((plan["side"] == "BUY" and plan["entry"] >= price)
                                        or (plan["side"] == "SELL" and plan["entry"] <= price))
    return "MARKET" if executable else "LIMIT"


ENTRY_GUARD_SKIPPED = "ENTRY_GUARD_SKIPPED"


def _market_stop_guard(plan: Dict[str, Any], bundle: Dict[str, Any]) -> Dict[str, Any]:
    """R90 穴 2: 成行に切り替わる周期の「発注時点の価格 → SL」距離(docs/R90 §1.2)。

    2026-09-14 23:26 の BREAKER BUY は予定建値 29,002 / SL 28,968.50(33.5pt = 1.12N)で
    ``RISK_BELOW_NOISE`` を通ったが、発注時点の価格 28,996.75 からは 28.25pt(0.95N)、
    実約定 28,985.38 からは 16.9pt(0.57N)しか無かった。予定建値のノイズ床検査は成行に
    切り替わった瞬間に意味を失う。ここは **claim の前**に置く —— claim 後に見送ると
    CLAIMED が 900 秒残って次の新規を塞ぐ(R52)。

    方針が読めない・ノイズ床が測れないときは出さない(fail-closed。証明できたら許可)。
    ``block`` が False で ``minPt`` があれば、その値を order.py の ``--min-stop-pt`` へ渡す。
    """
    try:
        import stop_logic
        policy = stop_logic.load_policy()
    except Exception as exc:  # noqa: BLE001 - 方針が読めない = 証明できない = 出さない
        return {"block": True, "reason": "STOP_LOGIC_UNAVAILABLE",
                "text": f"R90 market stop guard unavailable: {type(exc).__name__}: {exc}"}
    rule = policy.get("marketStopGuard") if isinstance(policy, dict) else None
    if not isinstance(rule, dict) or str(rule.get("mode") or "OFF") != "LIVE":
        return {"block": False, "reason": "GUARD_OFF", "mode": (rule or {}).get("mode", "OFF")}
    verdict = stop_logic.market_stop_guard(
        plan.get("side"), plan.get("initialStop"), _price_from_bundle(bundle),
        stop_logic.noise_from_bundle(bundle), rule)
    verdict["text"] = stop_logic.describe_guard(verdict)
    return verdict


def _note_entry_guard(records: List[Dict[str, Any]], entry_key: str, scenario: Dict[str, Any],
                      guard: Dict[str, Any], ledger_path: str) -> None:
    """見送りを台帳に **1 回だけ**(同じ key・同じ理由の連続周期は書かない)。

    行は ``plan`` を持たない(凍結プランとして拾われない)。HALT でも claim でもない —— 次周期は
    価格が変われば普通に再評価される。
    """
    previous = _latest(records, entry_key, {ENTRY_GUARD_SKIPPED})
    if previous and (previous.get("guard") or {}).get("reason") == guard.get("reason"):
        return
    try:
        _append_ledger({"key": entry_key, "entryKey": entry_key, "status": ENTRY_GUARD_SKIPPED,
                        "action": "ENTRY_GUARD", "scenarioId": scenario.get("scenarioId"),
                        "decisionId": scenario.get("decisionId"),
                        "guard": {k: v for k, v in guard.items() if k != "text"},
                        "reason": guard.get("text") or guard.get("reason")}, ledger_path)
    except OSError:
        pass                                     # 記録できなくても見送り自体は成立している


RESTING_STOP_STALE = "RESTING_STOP_STALE"


def _resting_stop_recheck(plan: Dict[str, Any], bundle: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """R103-1: 指値を保持している周期の SL 再検査(純粋計算を呼ぶだけ)。

    OFF または入力不足なら None。ここでは何も送らない —— 取消しの配線は K-2 の結果を
    見てから別 PR(契約は ``stopLogic.restingStopRecheck``)。
    """
    try:
        import stop_logic
        rule = (stop_logic.load_policy() or {}).get("restingStopRecheck")
    except Exception:  # noqa: BLE001 - 方針が読めなければ見ない(記録専用の節)
        return None
    if not isinstance(rule, dict) or str(rule.get("mode") or "OFF") == "OFF":
        return None
    snapshot = (bundle or {}).get("snapshot") if isinstance(bundle, dict) else {}
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    bars = snapshot.get("bars3m") or snapshot.get("bars") or []
    at = (bundle or {}).get("at") or snapshot.get("at")
    try:
        return stop_logic.resting_stop_recheck(plan.get("side"), plan.get("entry"),
                                               plan.get("initialStop"), bars, at, rule)
    except Exception:  # noqa: BLE001
        return None


def _note_resting_stop_stale(records: List[Dict[str, Any]], plan: Dict[str, Any],
                             verdict: Dict[str, Any], ledger_path: str) -> None:
    """stale な指値の SL を台帳へ **1 回だけ**(同じ key・同じ理由は書かない)。

    行は ``plan`` を持たない(凍結プランとして拾われない)。HALT でも取消しでもない —— 監査だけ。
    """
    entry_key = _plan_entry_key(plan)
    if not entry_key:
        return
    previous = _latest(records, entry_key, {RESTING_STOP_STALE})
    if previous and (previous.get("recheck") or {}).get("reason") == verdict.get("reason"):
        return
    try:
        _append_ledger({"key": entry_key, "entryKey": entry_key, "status": RESTING_STOP_STALE,
                        "action": "RESTING_RECHECK", "decisionId": plan.get("decisionId"),
                        "recheck": verdict, "reason": verdict.get("reason")}, ledger_path)
    except OSError:
        pass                                     # 記録できなくても指値の扱いは変わらない


RESTING_STOP_CANCEL = "RESTING_STOP_CANCEL"


def _resting_cancel_eligible(plan: Dict[str, Any], order: Optional[Dict[str, Any]]) -> Optional[str]:
    """R103-3: 指値取消の身元検査(R52 と同じ)。取消してよければ None、駄目なら理由。

    取消は flatten(口座の未約定を全部消す)なので、**生きている親行が全部自分の注文 ID** の
    ときだけ。identity の無い残骸や手動注文が同居している状態では触らない。
    """
    ours = {str(row.get("orderId")) for row in (plan.get("routeSnapshot") or [])
            if isinstance(row, dict) and str(row.get("state") or "").upper() == "ACCEPTED"
            and row.get("orderId")}
    if not ours:
        return "NO_BOUND_ORDER_IDS"
    active_rows = [row for row in ((order or {}).get("activeOrders") or []) if isinstance(row, dict)]
    parents = [row for row in active_rows if not str(row.get("parentId") or "").strip()]
    if not parents:
        return "NO_LIVE_PARENT_ORDERS"
    if any(str(row.get("orderId")) not in ours for row in parents):
        return "FOREIGN_PARENT_ORDER"
    return None


def _command_for_modify(plan: Dict[str, Any], action: Dict[str, Any],
                        generation: str, claim: Optional[Dict[str, Any]] = None,
                        account: Optional[str] = None,
                        last: Optional[float] = None) -> List[str]:
    args = ["--modify", "--side", plan["side"].lower(), "--qty", str(action["qty"]),
            "--sl", str(action["sl"]), "--tp", str(action["tp"]), "--symbol", plan["symbol"],
            "--position-generation", generation]
    # R78: 判断に使った現在値を order.py へ渡す。order.py は quote が取れればそちらを
    # 優先し、取れなければこの値で「stop が守れる側か」を送信直前に再検査する。
    if last is not None:
        args += ["--last", str(last)]
    # R52: ULTRA プランは order.py に --ultra を渡す。修正側の「全脚生存中」判定を
    # 枚数(>=2)ではなく保護注文の組数で行わせるため。
    if plan.get("ultra"):
        args.append("--ultra")
    if account:
        args += ["--account", str(account)]
    if claim:
        # R52: `--opt=value` 形式(token が `-` で始まっても argparse が値と読む)。
        args += [f"--management-key={claim.get('managementKey') or ''}",
                 f"--management-token={claim.get('claimToken') or ''}",
                 f"--management-intent-hash={claim.get('managementIntentHash') or ''}"]
    return args


def _command_for_flatten(account: Optional[str] = None) -> List[str]:
    args = ["--flatten"]
    if account:
        args += ["--account", str(account)]
    return args


def _execute_action(args: List[str], live: bool, runner: Callable[[List[str], bool], Tuple[int, str]]) -> Tuple[bool, str]:
    dry_code, dry_output = runner(args, False)
    if dry_code != 0:
        return False, f"dry-run rejected: {dry_output[-500:]}"
    if not live:
        return True, f"dry-run only: {dry_output[-500:]}"
    code, output = runner(args, True)
    if code != 0:
        return False, f"live send failed/unknown:\n{output}"
    return True, output


def _entry_route_state(output: str, expected_accounts=None) -> Optional[str]:
    parsed = route_envelope.parse(output, expected_accounts=expected_accounts)
    return parsed.get("state") if parsed.get("ok") else None


def _entry_route_snapshot(output: str, expected_accounts=None) -> List[Dict[str, Any]]:
    parsed = route_envelope.parse(output, expected_accounts=expected_accounts)
    return parsed.get("snapshot") if parsed.get("ok") else []


def _bind_partial_leg(plan: Dict[str, Any], position: Dict[str, Any],
                      route_snapshot: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Bind a partial fill to exactly one accepted split leg.

    2枚固定では qty=1、ULTRA の比率分割ではその脚の枚数と一致すること。
    どの脚かは qty ではなく orderId / receipt の一致で決める(qty はふるい)。
    """
    try:
        qty = int(position.get("qty") or 0)
    except (TypeError, ValueError):
        return None
    leg_qtys = set()
    for leg in plan.get("legs") or []:
        try:
            leg_qtys.add(int(leg.get("qty")))
        except (TypeError, ValueError, AttributeError):
            pass
    if qty not in (leg_qtys or {1}):
        return None
    order_id = str(position.get("orderId") or "")
    receipt = str(position.get("receipt") or "")
    account = str(position.get("accountId") or position.get("account") or "")
    if not account or not order_id or not receipt:
        return None
    matches = [row for row in route_snapshot if row.get("state") == "ACCEPTED"
               and str(row.get("accountId") or "") == account
               and str(row.get("orderId") or "") == order_id
               and str(row.get("receipt") or "") == receipt]
    if len(matches) != 1:
        return None
    leg_id = matches[0]["legId"]
    legs = [dict(row) for row in plan.get("legs") or []
            if str(row.get("id") or "").upper() == leg_id]
    if len(legs) != 1:
        return None
    target = float(legs[0]["target"])
    management_tp1 = float(plan["tp1"]) if leg_id == "RUNNER" else target
    return {**plan, "legs": legs, "targets": [target],
            "tp1": management_tp1, "finalTarget": target, "partialLegId": leg_id,
            "legIdentityKnown": True, "routeSnapshot": route_snapshot,
            "fullPlan": dict(plan)}


def _bind_sent_partial(plan: Dict[str, Any], position: Dict[str, Any],
                       order: Dict[str, Any], route_snapshot: List[Dict[str, Any]],
                       generation: Optional[str]) -> Optional[Dict[str, Any]]:
    """Bind qty=1 after a fully accepted split route without guessing a leg."""
    owned = _bind_position_ownership(plan, position, generation)
    identified = _bind_partial_leg(owned or {}, position, route_snapshot) if owned else None
    if not identified or not isinstance(order, dict) or order.get("verified") is not True:
        return None
    filled_id = str(position.get("orderId") or "")
    accepted = [row for row in route_snapshot if row.get("state") == "ACCEPTED"]
    remaining = [row for row in accepted if str(row.get("orderId") or "") != filled_id]
    if len(accepted) != 2 or len(remaining) != 1:
        return None
    row = remaining[0]
    active = [item for item in order.get("activeOrders") or [] if isinstance(item, dict)
              and str(item.get("accountId") or item.get("account") or "") == str(row["accountId"])
              and str(item.get("orderId") or "") == str(row["orderId"])]
    if len(active) != 1:
        return None
    return {**identified, "remainingRestingLeg": dict(row),
            "remainingOrderOwnership": {
                "accountId": row["accountId"], "legId": row["legId"],
                "orderId": row["orderId"], "receipt": row["receipt"],
            }}


def _partial_completion_matches(plan: Dict[str, Any], position: Dict[str, Any],
                                order: Dict[str, Any]) -> bool:
    """Require both frozen split entry IDs to be broker-confirmed filled."""
    snapshot = plan.get("routeSnapshot") if isinstance(plan.get("routeSnapshot"), list) else []
    full_plan = plan.get("fullPlan") if isinstance(plan.get("fullPlan"), dict) else None
    if not full_plan or len(snapshot) != 2 or int(position.get("qty") or 0) != 2:
        return False
    account = str(position.get("accountId") or position.get("account") or "")
    if account not in set(full_plan.get("accountScope") or []):
        return False
    rows = [row for row in (order or {}).get("orders") or [] if isinstance(row, dict)]
    for frozen in snapshot:
        matches = [row for row in rows
                   if str(row.get("accountId") or row.get("account") or "") == account
                   and str(row.get("orderId") or "") == str(frozen.get("orderId") or "")
                   and str(row.get("status") or "").upper() == "FILLED"]
        if len(matches) != 1:
            return False
    return True


def _entry_key(scenario: Dict[str, Any]) -> Optional[str]:
    """Return the immutable, server-owned ENTRY identity.

    ``decisionId`` and Mini App nonces are deliberately absent: they are UI
    presentation/retry values and must never decide whether a broker command
    can be sent.  A missing member is a hard failure rather than a lossy key.
    """
    if not isinstance(scenario, dict):
        return None
    fields = ("scenarioId", "fingerprint", "evidenceHash", "marketCycleId")
    payload = {field: str(scenario.get(field) or "").strip() for field in fields}
    if any(not value for value in payload.values()):
        return None
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "ENTRY:" + hashlib.sha256(canonical).hexdigest()


def _position_generation(position: Any) -> Optional[str]:
    """Strong broker identity hash; weak side/avgEntry similarity is never enough."""
    import broker_status
    return broker_status.position_identity(position)


def _latest_generation_open(records: Iterable[Dict[str, Any]], account: Any) -> bool:
    """直近の POSITION_GENERATION 記録が open=True か(口座で絞る)。"""
    account = str(account or "")
    history = [row for row in records if row.get("status") == "POSITION_GENERATION"
               and (not account or str(row.get("accountId") or "") in {"", account})]
    return bool(history) and history[-1].get("open") is True


def _owned_brackets_active(order: Any, frozen_plan: Any) -> bool:
    """凍結経路の ACCEPTED 親注文に繋がる保護注文(子)がまだ active か(R52)。

    CrossTrade/Tradovate の子行は `brokerParentId` に真の親(ENTRY 注文)を持つ。
    親が約定して建玉があるあいだ子は WORKING で、建玉が決済されれば片方が約定し
    もう片方は OCO で取消される。したがって「建玉 0 なのに所有ブラケットが active」
    は建玉照会の一時的な欠落と矛盾する観測であり、FLAT の証拠にならない。
    """
    import broker_status
    accepted = {str(row.get("orderId")) for row in ((frozen_plan or {}).get("routeSnapshot") or [])
                if isinstance(row, dict) and row.get("state") == "ACCEPTED" and row.get("orderId")}
    # R84: 合成プランは top-level の routeSnapshot を持たない(脚ごとの identity は
    # トランシェの中)。ここを拡げておかないと、合成建玉のときだけ「一過性の FLAT」
    # 保護(R52)が効かず、生きている建玉に FLAT 行を書いてしまう。
    # R84: 統合後の 1 組は **親を持たない** OCO 兄弟なので、親リンクでは辿れない。
    # その id 自体が「建玉を守っている注文」の証拠になる。ここを拾わないと、
    # 統合済みの合成建玉だけ R52 の一過性 FLAT 保護が効かず、生きている建玉に
    # FLAT 行を書いてしまう。
    protective_ids = set()
    for tranche_row in (frozen_plan or {}).get("tranches") or []:
        if not isinstance(tranche_row, dict):
            continue
        for leg in tranche_row.get("legs") or []:
            if isinstance(leg, dict) and str(leg.get("orderId") or ""):
                accepted.add(str(leg["orderId"]))
        pair = tranche_row.get("consolidationPair")
        if isinstance(pair, dict):
            protective_ids.update(str(value) for value in pair.get("orderIds") or []
                                  if str(value))
    consolidation = (frozen_plan or {}).get("consolidation")
    if isinstance(consolidation, dict):
        protective_ids.update(str(value) for value in consolidation.get("orderIds") or []
                              if str(value))
    if (not accepted and not protective_ids) or not isinstance(order, dict):
        return False
    for row in order.get("orders") or []:
        if not isinstance(row, dict):
            continue
        if str(row.get("status") or "").upper() not in broker_status.BROKER_ACTIVE_STATES:
            continue
        if str(row.get("brokerParentId") or "") in accepted:
            return True
        if str(row.get("orderId") or "") in protective_ids:
            return True
    return False


def _observe_position_generation(records: Iterable[Dict[str, Any]], position: Dict[str, Any],
                                 ledger_path: str) -> Optional[str]:
    """Persist a monotonically increasing flat→open generation for strong identities."""
    account = str(position.get("accountId") or position.get("account") or "")
    history = [row for row in records if row.get("status") == "POSITION_GENERATION"
               and (not account or str(row.get("accountId") or "") in {"", account})]
    latest = history[-1] if history else None
    try:
        qty = int(position.get("qty") or 0)
    except (TypeError, ValueError):
        return None
    if qty <= 0:
        if latest and latest.get("open") is True:
            _append_ledger({"key": f"POSITION_GENERATION:{latest.get('generation')}:FLAT",
                            "status": "POSITION_GENERATION", "generation": latest.get("generation"),
                            "identity": latest.get("identity"), "open": False,
                            "accountId": account or latest.get("accountId")}, ledger_path)
        return None
    identity = _position_generation(position)
    if not identity:
        return None
    if latest and latest.get("open") is True and latest.get("identity") == identity:
        return f"PG:{int(latest.get('generation') or 0)}:{identity}"
    generation = int(latest.get("generation") or 0) + 1 if latest else 1
    _append_ledger({"key": f"POSITION_GENERATION:{account}:{generation}:{identity}",
                    "status": "POSITION_GENERATION", "generation": generation,
                    "identity": identity, "open": True,
                    "accountId": position.get("accountId") or position.get("account"),
                    "symbol": position.get("symbol"), "side": position.get("side")}, ledger_path)
    return f"PG:{generation}:{identity}"


def _bind_position_ownership(plan: Dict[str, Any], position: Dict[str, Any],
                             generation: Optional[str] = None) -> Optional[Dict[str, Any]]:
    generation = generation or _position_generation(position)
    if not generation:
        return None
    return {**plan, "positionOwnership": {
        "generation": generation,
        "rawIdentity": _position_generation(position),
        "account": position.get("account"), "accountId": position.get("accountId"),
        "symbol": position.get("symbol") or plan.get("symbol"),
        "side": str(position.get("side") or "").upper(),
        "orderId": position.get("orderId"), "receipt": position.get("receipt"),
        "filledAt": position.get("filledAt"), "avgEntry": position.get("avgEntry"),
        "initialQty": position.get("initialQty") or position.get("qty"),
    }}


def _plan_entry_key(plan: Any) -> Optional[str]:
    if not isinstance(plan, dict):
        return None
    stored = str(plan.get("entryKey") or "").strip()
    if stored:
        return stored
    return _entry_key(plan)


def _frozen_plan(records: Iterable[Dict[str, Any]], symbol: str,
                 account: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Select the latest durable ENTRY plan, never the current proposal."""
    record = _frozen_plan_record(records, symbol, account=account)
    return record.get("plan") if record else None


def _management_overrides(path: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
    """R104: 管理上書きファイルを decisionId → 上書き の辞書で返す。読めなければ空(=上書きなし)。"""
    path = path or MANAGEMENT_OVERRIDE_FILE
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return {}
    if not isinstance(payload, dict) or payload.get("schema") != MANAGEMENT_OVERRIDE_SCHEMA:
        return {}
    result: Dict[str, Dict[str, Any]] = {}
    for item in payload.get("overrides") or []:
        if isinstance(item, dict) and item.get("decisionId") not in (None, ""):
            result[str(item["decisionId"])] = item
    return result


def apply_management_override(plan: Any, overrides: Optional[Dict[str, Dict[str, Any]]] = None) -> Any:
    """R104: 凍結プランへ人の上書き(finalTarget / trailMode)を乗せた**コピー**を返す。

    台帳の行は書き換えない。上書きが無い、または decisionId が一致しなければ元の
    オブジェクトをそのまま返す(呼び出し側は `is` で「乗ったか」を判定できる)。
    `finalTarget` は TP1 より利益側でなければ無視する(TP1 を越えない runner 最終 TP は
    分割ブラケットとして成立しない)。乗せた内容は `plan["managementOverride"]` に残す。
    """
    if not isinstance(plan, dict):
        return plan
    table = _management_overrides() if overrides is None else overrides
    item = table.get(str(plan.get("decisionId") or ""))
    if not isinstance(item, dict):
        return plan
    updated = json.loads(json.dumps(plan))
    applied: Dict[str, Any] = {}
    side = str(plan.get("side") or "").upper()
    tp1 = _num(plan.get("tp1"))
    final = _num(item.get("finalTarget"))
    if final is not None and math.isfinite(final):
        final = _tick(final)
        beyond_tp1 = (tp1 is None or (final > tp1 if side == "BUY" else final < tp1))
        if beyond_tp1:
            updated["finalTarget"] = final
            targets = list(updated.get("targets") or [])
            if targets:
                targets[-1] = final
                updated["targets"] = targets
            for leg in updated.get("legs") or []:
                if isinstance(leg, dict) and str(leg.get("id") or "").upper() == "RUNNER":
                    leg["target"] = final
            applied["finalTarget"] = final
        else:
            applied["finalTargetIgnored"] = final
    mode = str(item.get("trailMode") or "").upper()
    if mode == TRAIL_MODE_BREAKEVEN_ONLY:
        updated["trailMode"] = mode
        applied["trailMode"] = mode
    if not applied:
        return plan
    updated["managementOverride"] = applied
    return updated


def _with_management_override(record: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """R104: 凍結プラン行の出口。plan に上書きが乗るときだけ行を浅くコピーして差し替える。"""
    if not record or not isinstance(record.get("plan"), dict):
        return record
    plan = apply_management_override(record["plan"])
    if plan is record["plan"]:
        return record
    copied = dict(record)
    copied["plan"] = plan
    return copied


def _frozen_plan_record(records: Iterable[Dict[str, Any]], symbol: str,
                        account: Optional[str] = None) -> Optional[Dict[str, Any]]:
    return _with_management_override(_frozen_plan_record_raw(records, symbol, account=account))


def _frozen_plan_record_raw(records: Iterable[Dict[str, Any]], symbol: str,
                            account: Optional[str] = None) -> Optional[Dict[str, Any]]:
    fallback = None
    # R71: 走査は新→旧。建玉が閉じた記録(POSITION_GENERATION open=False)より古い
    # **所有済み**プランは、その建玉がもう無いので凍結プランにしない。
    # 2026-09-08 19:37 実測: 同一口座で trade 1(2 枚、所有済み)が FLAT になった直後に
    # trade 2(ULTRA 8 枚)を CLAIMED したが、ここが trade 1 の所有プランを返し
    # (R35 の「所有プラン優先・未所有の新プランは fallback」)、binder が 8 枚を
    # 「2 枚プランの split lifecycle 外」と判定して管理を保留した。同一口座で連続
    # トレードするたびに 2 件目以降の管理が外れる。
    position_closed_later = False
    for record in reversed(list(records)):
        if (record.get("status") == "POSITION_GENERATION" and record.get("open") is False
                and (not account or not record.get("accountId")
                     or str(record.get("accountId")) == str(account))):
            position_closed_later = True
        if not isinstance(record.get("plan"), dict):
            continue
        # FLAT より古いプランは、所有済みでも未所有(同じ entry の CLAIMED 行)でも終わった
        # トレードのもの。建玉が開いたまま FLAT 行が書かれることは無い(FLAT 行は開→閉の
        # 遷移でだけ書かれる)ので、この境界より古い行を凍結プランにする理由が無い。
        if position_closed_later:
            continue
        plan = record["plan"]
        if str(plan.get("symbol") or "") != symbol:
            continue
        if account:
            ownership = plan.get("positionOwnership") if isinstance(
                plan.get("positionOwnership"), dict) else {}
            owner = str(ownership.get("accountId") or ownership.get("account") or "")
            pending = plan.get("pendingOrderOwnership") if isinstance(
                plan.get("pendingOrderOwnership"), dict) else {}
            pending_scope = {str(value) for value in pending.get("accountScope") or []}
            if owner and owner != str(account):
                continue
            if pending_scope and str(account) not in pending_scope:
                continue
        if record.get("status") in {"ENTRY_HALTED", "ENTRY_TERMINAL"}:
            # R74: 終端した古いプランに当たったら走査を止めるが、それより **新しい未所有
            # プラン**(fallback)があればそれを返す。2026-09-09 07:09 実測: 04:52 に R52 で
            # 取消した SELL の ENTRY_HALTED 行が残ったまま、07:07 の BUY 4 枚が送信直後の
            # 部分約定で未所有(fallback)になり、ここが None を返して「open position has no
            # frozen management plan」→ 実弾 4 枚がブローカー OCO だけで放置された。
            # 取消済みの行は FLAT 行を挟まない(未約定のまま消えるので建玉遷移が無い)。
            return fallback
        # R35: ENTRY_CLAIMED も凍結プランとして採用する。
        #
        # 送信直前に書かれる状態で、子プロセスが 30 秒 kill されるとここで
        # 止まる。以前は採用集合に無かったため凍結プランが None になり、
        # `management_action` が一度も呼ばれなかった —— トレールも建値移動も
        # `reached_stop` による FLATTEN も動かず、**実弾の建玉がブローカー側
        # OCO だけを頼りに放置**されていた。
        #
        # 注文が実際に出たかどうかは分からないが、**出たかもしれない建玉を
        # 管理下に置く方が安全**。建玉が無ければ management_action は
        # qty=0 で何もしない。
        if record.get("status") in {"ENTRY_CLAIMED", "ENTRY_SENT", "ENTRY_ACCEPTED",
                                    "ENTRY_RESTING", "ENTRY_PARTIAL_ROUTE",
                                    "ENTRY_PARTIAL_FILL"}:
            if account and not owner and not pending_scope:
                fallback = fallback or record
                continue
            return record
    return fallback


def _pending_order_ownership(order: Dict[str, Any], plan: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not isinstance(order, dict) or order.get("verified") is not True:
        return None
    if str(order.get("state") or "").upper() != "PENDING":
        return None
    order_ids = sorted({str(value) for value in order.get("orderIds") or [] if value not in (None, "")})
    if not order_ids:
        return None
    accounts = sorted({str(value) for value in order.get("accountScope") or [] if value not in (None, "")})
    if not accounts:
        accounts = sorted({str(value) for value in plan.get("accountScope") or [] if value not in (None, "")})
    return {"orderIds": order_ids, "receipts": sorted({str(value) for value in order.get("receipts") or []
                                                        if value not in (None, "")}),
            "accountScope": accounts, "symbol": plan.get("symbol"), "side": plan.get("side")}


def _pending_fill_matches(plan: Dict[str, Any], position: Dict[str, Any], order: Dict[str, Any]) -> bool:
    pending = plan.get("pendingOrderOwnership") if isinstance(plan.get("pendingOrderOwnership"), dict) else None
    if not pending or not _position_generation(position):
        return False
    if str(position.get("symbol") or "") != str(plan.get("symbol") or ""):
        return False
    if _position_side(position) != str(plan.get("side") or ""):
        return False
    account = str(position.get("accountId") or position.get("account") or "")
    if account not in set(pending.get("accountScope") or []):
        return False
    frozen_ids = set(pending.get("orderIds") or [])
    direct = str(position.get("orderId") or "")
    if direct and direct in frozen_ids:
        return True
    receipt = str(position.get("receipt") or "")
    if receipt and receipt in set(pending.get("receipts") or []):
        return True
    filled_ids = {str(value) for value in (order or {}).get("filledOrderIds") or []}
    return bool(frozen_ids & filled_ids)


def _legacy_entry_matches(record: Dict[str, Any], scenario: Dict[str, Any]) -> bool:
    """One-way migration guard for pre-R16 ledger rows.

    Old rows keyed only by ``decisionId`` remain consumed if their frozen plan
    still names the same server scenario.  New records always carry entryKey;
    no new execution decision is ever made from a decisionId.
    """
    if record.get("status") not in {"ENTRY_SENT", "ENTRY_HALTED"}:
        return False
    plan = record.get("plan") if isinstance(record.get("plan"), dict) else {}
    if _plan_entry_key(plan):
        return False
    scenario_id = str(scenario.get("scenarioId") or "")
    legacy = str(plan.get("scenarioId") or plan.get("decisionId") or record.get("key") or "")
    if not scenario_id or legacy != scenario_id:
        return False
    # Avoid migrating an unrelated same-label decision when the old plan did
    # preserve immutable fields.  Very old decisionId-only rows are accepted
    # once as a conservative duplicate block.
    for field in ("fingerprint", "evidenceHash", "marketCycleId"):
        if plan.get(field) is not None and str(plan.get(field)) != str(scenario.get(field) or ""):
            return False
    return True


def _authoritative_cycle_seal(view: Any, frozen: Any, *, cfg: Optional[Dict[str, str]] = None,
                              now: Optional[datetime] = None,
                              pyramid_mode: bool = False) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
    """Require a current, committed, hash-bound scenario before any runner.

    R84: ``pyramid_mode`` は **建玉が開いていること由来のゲートだけ**を外す。
    正本 display の ``orderable`` は建玉が開いている間ずっと False なので
    (Worker の projectState が「POSITION OPEN — MANAGEMENT ONLY」で落とす)、
    追撃では ``cyclePaired`` を必須のまま残し、代わりに執行契約を
    ``position=None / order=None`` で評価し直す —— これは monitor_publish が
    publish 時に通したのと**同じ門**で、建玉と注文に関する判定だけが抜ける。
    建玉側の安全はここではなく所有権束縛(tranche.bind_composite)と
    order.py の ``require_verified_pyramid_base`` が持つ。
    """
    if not isinstance(view, dict) or not isinstance(frozen, dict):
        return False, "CYCLE_MISMATCH authoritative view/frozen scenario unavailable", None
    display = view.get("display")
    market, scenario = view.get("market"), view.get("scenario")
    if not isinstance(display, dict) or display.get("cyclePaired") is not True:
        return False, "CYCLE_MISMATCH authoritative cycle is not orderable", None
    if not pyramid_mode and display.get("orderable") is not True:
        return False, "CYCLE_MISMATCH authoritative cycle is not orderable", None
    if not isinstance(market, dict) or not isinstance(scenario, dict):
        return False, "CYCLE_MISMATCH market/scenario missing", None
    if market.get("cycleCommitted") is not True or scenario.get("cycleCommitted") is not True:
        return False, "CYCLE_MISMATCH cycle commits missing", None
    if not market.get("cycleId") or str(market.get("cycleId")) != str(scenario.get("marketCycleId") or ""):
        return False, "CYCLE_MISMATCH cycle ids differ", None
    try:
        evidence = strategy_evidence.canonicalize(market.get("strategyEvidence"))
    except (TypeError, ValueError):
        evidence = None
    if not evidence or market.get("strategyEvidenceError"):
        return False, "CYCLE_MISMATCH canonical market evidence unavailable", None
    if "strategyEvidence" in scenario:
        return False, "CYCLE_MISMATCH scenario must be hash-only", None
    if str(scenario.get("evidenceHash") or "") != str(evidence.get("evidenceHash") or ""):
        return False, "CYCLE_MISMATCH scenario evidence hash differs", None
    for field in ("scenarioId", "fingerprint", "evidenceHash", "symbol", "side", "qty"):
        if str(frozen.get(field) or "") != str(scenario.get(field) or ""):
            return False, f"CYCLE_MISMATCH frozen {field} differs", None
    try:
        for field in ("entry", "stop", "target"):
            if abs(float(frozen.get(field)) - float(scenario.get(field))) > 1e-9:
                return False, f"CYCLE_MISMATCH frozen {field} differs", None
        if list(frozen.get("targets") or []) != list(scenario.get("targets") or []):
            return False, "CYCLE_MISMATCH frozen targets differ", None
    except (TypeError, ValueError):
        return False, "CYCLE_MISMATCH frozen price/targets invalid", None
    contract = execution_contract.evaluate(
        scenario, market,
        None if pyramid_mode else view.get("position"),
        None if pyramid_mode else view.get("order"),
        now=now, cfg=cfg,
    )
    if not contract.get("orderable"):
        return False, "execution contract " + ", ".join(contract.get("blockers") or []), None
    return True, "", scenario


def _verify_after_action(query: Callable[[str], Dict[str, Any]], symbol: str,
                        action: str, expected_side: Optional[str] = None,
                        expected_qty: Optional[int] = None) -> Tuple[bool, str]:
    """送信後に最低限の建玉状態を再確認する。照会不能は成功扱いにしない。"""
    try:
        observed = query(symbol)
    except Exception as exc:  # noqa: BLE001
        return False, f"post-send broker query failed ({type(exc).__name__}: {exc})"
    if not isinstance(observed, dict) or not observed.get("verified"):
        return False, "post-send broker position is UNVERIFIED"
    qty = int(observed.get("qty") or 0)
    if action == "FLATTEN":
        return (True, "flat verified") if qty == 0 else (False, f"flatten not confirmed; position qty={qty}")
    if action == "ENTRY":
        # A resting limit can be accepted while the broker remains FLAT.  The
        # order was sent, but it is not yet a fill; keep the frozen plan so a
        # later cycle can adopt it if the position appears, without resending.
        if qty > 0 and expected_side and _position_side(observed) != expected_side:
            return False, "post-send broker side differs from plan"
        if qty > 0 and expected_qty is not None and qty != expected_qty:
            return False, "post-send entry quantity differs from frozen split plan"
        return True, "broker verified (fill may be pending)"
    if qty <= 0:
        return False, "entry/modify post-check found no open position"
    if expected_side and _position_side(observed) != expected_side:
        return False, "post-send broker side differs from plan"
    return True, "position verified"


def _reconcile_r15_legacy(*_args, **_kwargs) -> List[str]:
    """Removed R15 route: retained only as a fail-closed compatibility symbol."""
    return ["autotrade blocked: R15 legacy reconcile is disabled"]

def _stale_entry_to_cancel(records: Iterable[Dict[str, Any]], symbol: str,
                           account: Optional[str], bundle: Dict[str, Any],
                           orders: Optional[Dict[str, Any]] = None
                           ) -> Optional[Tuple[Dict[str, Any], str]]:
    """未約定の指値エントリーが「価格が建値より先に TP1 に届いた」状態か(R52)。

    2026-09-05 ユーザー指示。SELL 指値 29,585 が残ったまま価格が TP1 29,498.5 に先に
    届いた(=シナリオは建玉なしで完走した)。指値が残る限り startup recovery は
    BROKER_NOT_EMPTY で止まり、新規が再開しない。人が手で消していた。

    条件: 凍結 ENTRY プランが未終端(後続に ENTRY_RECOVERED / ENTRY_HALTED が無い)、
    送信時刻以降のバーの極値が TP1 に到達。戻り値は ``(記録, 理由)``。
    建値到達なら約定して建玉が立つので、この経路(FLAT)には来ない。
    """
    rows = list(records)
    record = _frozen_plan_record(rows, symbol, account=account)
    if not record:
        return None
    plan = record.get("plan") or {}
    key = _plan_entry_key(plan) or str(record.get("key") or "")
    if not key:
        return None
    try:
        index = next(i for i, row in enumerate(rows) if row is record)
    except StopIteration:
        return None
    # 送信後に回復/終端した記録があれば、いま残っている注文はこのプランの物ではない。
    for row in rows[index + 1:]:
        if (str(row.get("key") or "") == key
                and row.get("status") in {"ENTRY_RECOVERED", "ENTRY_HALTED", "ENTRY_TERMINAL"}):
            return None
    # 取消は flatten(口座の未約定を全部消す)なので、**生きている親行が全部自分の
    # 注文 ID** のときだけ発火する。identity が無い残骸や、手動注文が同居している
    # 状態では触らない(CLAUDE.md §4「台帳にない手動建玉を勝手に管理しない」)。
    # 2026-09-05 01:4x、ユーザーが手動で 18 枚を同じ口座に入れた直後に顕在化した。
    ours = {str(row.get("orderId")) for row in
            (plan.get("routeSnapshot") or record.get("routeSnapshot") or [])
            if isinstance(row, dict) and str(row.get("state") or "").upper() == "ACCEPTED"
            and row.get("orderId")}
    if not ours:
        return None
    active_rows = [row for row in ((orders or {}).get("activeOrders") or [])
                   if isinstance(row, dict)]
    parents = [row for row in active_rows if not str(row.get("parentId") or "").strip()]
    if not parents or any(str(row.get("orderId")) not in ours for row in parents):
        return None
    side = str(plan.get("side") or "").upper()
    tp1 = _num(plan.get("tp1"))
    entry = _num(plan.get("entry"))
    if side not in {"BUY", "SELL"} or tp1 is None:
        return None
    since = _parse_at(record.get("time"))
    if since is None:
        return None
    extreme = _extreme(bundle, side, since)
    if extreme is None:
        return None
    reached = (extreme <= tp1) if side == "SELL" else (extreme >= tp1)
    if not reached:
        return None
    label = "low" if side == "SELL" else "high"
    reason = (f"TP1 reached before entry ({side} entry {entry} / tp1 {tp1}; "
              f"{label} since {record.get('time')} = {extreme})")
    return record, reason


#: R76: 台帳の送信窓(ENTRY_CLAIMED → HALT / 経路行)の外側に取る余裕(秒)。ブローカーの
#: 時計とこの PC の時計のずれ、CLAIMED 行の書き込みから POST までの遅れを吸収する。
ROUTE_REBIND_SKEW_SEC = 5.0
#: R76: 経路行(HALT 等)が無く ENTRY_CLAIMED だけが残ったときの窓の長さ(秒)。
#: 子プロセスの上限(送信はその中で終わる)と同じにする。
ROUTE_REBIND_FALLBACK_SEC = 150.0
ROUTE_END_STATUSES = {"HALT", "ENTRY_PARTIAL_ROUTE", "ENTRY_SENT", "ENTRY_ACCEPTED",
                      "ENTRY_RESTING", "ENTRY_PARTIAL_FILL", "ENTRY_HALTED"}


def _route_send_window(records: Iterable[Dict[str, Any]], entry_key: str
                       ) -> Optional[Tuple[datetime, datetime]]:
    """凍結プランの送信が起きた時間窓 ``[start, end]``(R76)。

    start = その key の(最後の)ENTRY_CLAIMED 行の時刻。送信は必ずこの後に起きる。
    end = それ以降で最初に書かれた同 key の経路行(HALT / ENTRY_*)の時刻。送信はこの前に
    終わっている(子プロセスの終了後に書かれるため)。経路行が無い(CLAIMED だけ残った)
    ときは start + 子プロセスの上限。CLAIMED 行が無ければ None —— 送ったかどうかの記録が
    無い経路は再束縛しない。両端に ``ROUTE_REBIND_SKEW_SEC`` の余裕を付ける。
    """
    claimed_at: Optional[datetime] = None
    end_at: Optional[datetime] = None
    for record in records:
        if str(record.get("key") or "") != str(entry_key):
            continue
        status = record.get("status")
        at = _parse_at(record.get("time"))
        if status == "ENTRY_CLAIMED":
            claimed_at, end_at = at, None
            continue
        if claimed_at is not None and end_at is None and at is not None and status in ROUTE_END_STATUSES:
            end_at = at
    if claimed_at is None:
        return None
    if end_at is None:
        end_at = claimed_at + timedelta(seconds=ROUTE_REBIND_FALLBACK_SEC)
    return (claimed_at - timedelta(seconds=ROUTE_REBIND_SKEW_SEC),
            end_at + timedelta(seconds=ROUTE_REBIND_SKEW_SEC))


def _default_fills_query(account: str) -> Optional[Dict[str, Any]]:
    """本番の約定履歴(R76 の枚数証拠)。注入が無く、ブローカー照会も注入されていない
    ときだけ ``reconcile`` が選ぶ。"""
    import broker_status
    return broker_status.query_fills(account=account)


def _scoped_fills_query(query, account: str) -> Optional[Dict[str, Any]]:
    """口座の約定履歴。失敗・未注入は None(= 枚数の証拠なし。束縛は作成順に倒れる)。"""
    if query is None or not account:
        return None
    try:
        view = query(account)
    except Exception:  # noqa: BLE001 - 証拠の欠けは「無い」に倒す。所有の判定は binder が行う
        return None
    return view if isinstance(view, dict) and view.get("verified") is True else None


def _rebind_unknown_route(records: Iterable[Dict[str, Any]], plan: Dict[str, Any],
                          position: Dict[str, Any], order: Dict[str, Any], *, symbol: str,
                          account: str, order_query, fills_query=None
                          ) -> Tuple[Optional[List[Dict[str, Any]]], str, Dict[str, Any]]:
    """R76: ACCEPTED 行の無い凍結経路を、ブローカーの **ブラケット構造** から凍結し直す。

    2026-09-08 10:34 実測: ULTRA 8 枚(4/4)の成行が HTTP 200 で約定したのに、送信直後の
    identity 取得が UNKNOWN → `HALT ENTRY live send failed/unknown`。1 秒後には LONG 8 と
    SELL の子 4 本(2 対)が見えていたが、以後の周期は binder が「accepted=0 has no ownership」
    を返し続け、TP1 後の建値移動もトレールも構造 SL 決済も走らなかった。

    条件は全部 ``route_identity.rebind_split_legs_from_brackets`` が判定する(同方向の
    active 行なし・逆方向 active 行が全部 live な対・対の数 = 脚の数・全対が送信窓の中・
    真の親 id あり)。ここでは台帳から送信窓を作り、建玉の枚数から束縛する脚を決め、
    約定履歴があれば枚数の証拠を渡す。戻り値 ``(routeSnapshot | None, 理由, 注文一覧)``。
    注文一覧は再束縛できた親 id を ``known_order_ids`` で読み足したもの(読めなければ元)。
    最終的な所有の判定は従来どおり ``ownership_binder.bind`` が行う。
    """
    import broker_status
    records = list(records)
    entry_key = _plan_entry_key(plan)
    if not entry_key:
        return None, "frozen plan has no entry key", order
    window = _route_send_window(records, entry_key)
    if window is None:
        return None, "ledger has no ENTRY_CLAIMED send window for this plan", order
    side = str(plan.get("side") or "").upper()
    if _position_side(position) != side:
        return None, "broker position side differs from frozen plan", order
    leg_qty: Dict[str, int] = {}
    for row in plan.get("legs") or []:
        if not isinstance(row, dict):
            continue
        try:
            value = int(row.get("qty"))
        except (TypeError, ValueError):
            continue
        leg_id = str(row.get("id") or "").upper()
        if leg_id in ownership_binder.LEGS and value > 0:
            leg_qty[leg_id] = value
    if not leg_qty:
        leg_qty = {leg: 1 for leg in ownership_binder.LEGS}
    if set(leg_qty) != set(ownership_binder.LEGS):
        return None, "frozen split legs are incomplete", order
    try:
        qty = int(position.get("qty") or 0)
    except (TypeError, ValueError):
        return None, "broker position qty is invalid", order
    tp1_qty, runner_qty = leg_qty["TP1"], leg_qty["RUNNER"]
    if qty == tp1_qty + runner_qty:
        legs = [("TP1", tp1_qty), ("RUNNER", runner_qty)]
    elif qty == runner_qty:
        # TP1 の目標は近いので先に外れる。残るのは RUNNER 脚以外にない(R52 と同じ前提)。
        legs = [("RUNNER", runner_qty)]
    else:
        return None, f"broker position qty {qty} is outside the rebindable split lifecycle", order
    window_start, window_end = window
    parent_qty = None
    fills = _scoped_fills_query(fills_query, account)
    if fills is not None:
        parent_qty = route_identity.fill_qty_by_parent(
            None, fills, symbol=symbol, action=side, since=window_start, until=window_end)
    platform = (order or {}).get("platform")
    snapshot, detail = route_identity.rebind_split_legs_from_brackets(
        account=account, symbol=symbol, action=side, legs=legs, broker_orders=order,
        window_start=window_start, window_end=window_end, platform=platform,
        parent_qty=parent_qty)
    if not snapshot:
        return None, detail, order
    bound_legs = {row["legId"] for row in snapshot}
    if "TP1" not in bound_legs and parent_qty:
        # TP1 のブラケットは消費済みだが、約定履歴に「送信窓の中・同方向・TP1 の枚数
        # ちょうど・RUNNER の親とは別」の親が 1 件だけ(窓の中の親が計 2 件)残っていれば、
        # それが TP1 の親。無ければ TP1 は UNKNOWN のまま(PARTIAL 経路で管理する)。
        runner_parent = next((row["orderId"] for row in snapshot if row["legId"] == "RUNNER"), None)
        others = [order_id for order_id, value in parent_qty.items()
                  if order_id != runner_parent and value == tp1_qty]
        if len(others) == 1 and len(parent_qty) == 2:
            receipt = broker_status.derived_receipt(platform, account, others[0])
            if receipt:
                snapshot.insert(0, {"accountId": account, "legId": "TP1", "state": "ACCEPTED",
                                    "orderId": others[0], "receipt": receipt, "status": "FILLED",
                                    "filledAt": None, "identitySource": "broker-fills"})
                bound_legs.add("TP1")
    for leg in ownership_binder.LEGS:
        if leg not in bound_legs:
            snapshot.append({"accountId": account, "legId": leg, "state": "UNKNOWN",
                             "orderId": None, "receipt": None, "filledAt": None})
    snapshot.sort(key=lambda row: ownership_binder.LEGS.index(row["legId"]))
    parent_ids = [row["orderId"] for row in snapshot if row.get("state") == "ACCEPTED"]
    try:
        refreshed = _query_orders_with_ids(order_query, symbol, parent_ids)
    except Exception:  # noqa: BLE001 - 読み足しは best-effort。構造照合が残る
        refreshed = None
    if isinstance(refreshed, dict) and refreshed.get("verified") is True:
        order = refreshed
    return snapshot, detail, order


# =====================================================================
# R84 追撃(pyramid)— 構造トランシェ台帳の配線
# ---------------------------------------------------------------------
# 判定と合成は tranche.py / pyramid.py(純関数)にあり、ここは **配線だけ**を持つ。
# docs/R84_PYRAMID_TRANCHE_MANAGEMENT.md の §3(実行順序)/§9(HALT と回復)。
# =====================================================================

#: WAL の段(§1.3)。`plan` キーを持つのはコミット行だけ ——
#: `_frozen_plan_record()` は `plan` のある行しか採らないので、下書きが凍結プランとして
#: 選ばれる事故が **型の上で** 起きない。
PYRAMID_CLAIMED = "PYRAMID_CLAIMED"
PYRAMID_SENT = "PYRAMID_SENT"
PYRAMID_SKIPPED = "PYRAMID_SKIPPED"
PYRAMID_DRYRUN = "PYRAMID_DRYRUN"
PYRAMID_COMMIT = "PYRAMID_COMMIT"
PYRAMID_CONSOLIDATED = "PYRAMID_CONSOLIDATED"
PYRAMID_OWNERSHIP_BOUND = "PYRAMID_OWNERSHIP_BOUND"
PYRAMID_RECOVERY = "PYRAMID_RECOVERY"
#: 台帳の重複防止集合。追撃は 1 シグナルにつき一度だけ送る(§5 の既存規律と同じ)。
PYRAMID_ENTRY_GUARD = {"ENTRY_CLAIMED", "ENTRY_SENT", "ENTRY_ACCEPTED", "ENTRY_RESTING",
                       "ENTRY_PARTIAL_ROUTE", "ENTRY_PARTIAL_FILL", "ENTRY_HALTED",
                       PYRAMID_CLAIMED, PYRAMID_SENT}
MODIFY_BRACKET_MARKER = "NQX_MODIFY_BRACKET "
#: 送信済みの追撃をコミットできないまま回廊に居続けてよい時間。これを越えたら
#: 「経路 identity を束縛できない送信」として HALT を 1 度だけ記録する —— 黙って
#: FLATTEN 専用のまま一日を終えるより、人が建玉を照会できる方がよい。
#: (回廊そのものは残るので、撤退と KILL は引き続き動く。)
PYRAMID_COMMIT_ESCALATE_SEC = 600.0


#: AUTO OFF の「管理専用」注入を区別する印(R84)。
#:
#: `_disarmed_single_account_management`(R68)と多口座ループは、AUTO が切れていても
#: 建玉管理と未約定取消だけは通すために `NQX_AUTOTRADE=1` / `NQX_LIVE_ORDERS=1` を
#: 差し込んで `_reconcile_one` を呼ぶ。その安全性は「建玉があるとき ENTRY へ進まない」
#: という不変条件に乗っていたが、R84 が open_qty>0 の枝へ送信経路(追撃)を足した以上、
#: もう暗黙には成立しない。**追撃は新規 ENTRY** なので、この印がある周期では送らない。
ENTRY_DISARMED_KEY = "NQX_ENTRY_DISARMED"


def _entry_disarmed(cfg: Optional[Dict[str, str]] = None) -> bool:
    """この周期の AUTO/LIVE が「管理専用」として差し込まれたものか。"""
    return _truthy(_setting(ENTRY_DISARMED_KEY, cfg, "0"))


def _pyramid_guard(label: str, action: Callable[[], Any],
                   fallback: Any) -> Tuple[Any, List[str]]:
    """追撃の新経路を **例外でサイクルを落とさない** 形に包む。

    `reconcile()` にも `_reconcile_locked()` にも例外の受け皿は無い。つまり追撃の
    実装バグが 1 つあるだけで、その周期の建玉管理・撤退・他口座の処理まで丸ごと
    止まる —— 実弾がブローカー側 OCO だけで放置される、R74/R78 と同じ形の事故に
    なる。追撃は「やらなくてよい仕事」なので、ここで落ちても管理は続ける。

    握った例外は必ず注記へ出す(黙って無かったことにしない)。
    """
    try:
        return action(), []
    except Exception as exc:  # noqa: BLE001 - 追撃の失敗で管理を止めない
        return fallback, [f"pyramid guard: {label} failed ({type(exc).__name__}: {exc})"]


def _account_matches(record: Dict[str, Any], account: Any) -> bool:
    if not account:
        return True
    stored = str(record.get("accountId") or "")
    return not stored or stored == str(account)


def _since_flat_boundary(records: List[Dict[str, Any]], account: Any) -> List[Dict[str, Any]]:
    """直近の FLAT 境界(POSITION_GENERATION open=False)より後の行だけ返す。

    境界の述語は `_frozen_plan_record`(R71)と同じものを使う。別の定義を作ると、
    どちらか片方だけが「終わったトレードの記録」を拾い続ける。
    """
    rows = list(records)
    boundary = -1
    for index, record in enumerate(rows):
        if (record.get("status") == "POSITION_GENERATION" and record.get("open") is False
                and _account_matches(record, account)):
            boundary = index
    return rows[boundary + 1:]


def _pyramid_consumed_decisions(records: List[Dict[str, Any]], account: Any,
                                plan: Any) -> set:
    """このトレード(直近 FLAT 境界以降)で既に建玉を作った・足した決定の集合。

    決定 = ``scenarioId``(msnr_gate の ``decisionId``)。これは「同じセットアップなら
    3 分ごとに変わらない」ように作られたハッシュで、実サイクルでは同じ構造が最長
    14 連続で再武装していた。一方 ``entryKey`` は ``marketCycleId`` を含むので
    **毎周期変わる** —— entryKey だけで重複を見ると、同じシグナルが再掲されるたびに
    建て増す(基礎を作ったシグナル自身でも、ULTRA 枚数の焼き直しで差が出れば足す)。
    R83 の定義は「同方向の **新規** シグナル」なので、決定単位で 1 トレード 1 回に絞る。

    数えるもの: 所有プラン(基礎と各トランシェ、統合で畳んだ分は ``sourceScenarioIds``)、
    境界以降の ``plan`` を持つ行(基礎 ENTRY・コミット・統合)、**送信まで進んだ**
    追撃(``PYRAMID_SENT`` がある WAL の下書き)、影運転の ``PYRAMID_DRYRUN``
    (本番なら送っていた = 影でも消費扱いにしないと、観測が本番の挙動を映さない)。
    送信前に落ちた claim(``CLAIM_REFUSED`` / ``DRY_RUN_REJECTED``)は何も出していない
    ので消費しない —— 次の周期に同じ決定で再挑戦できる。
    """
    decisions = set()

    def take(source: Any) -> None:
        if not isinstance(source, dict):
            return
        for value in [source.get("scenarioId")] + list(source.get("sourceScenarioIds") or []):
            text = str(value or "").strip()
            if text:
                decisions.add(text)
        for row in source.get("tranches") or []:
            if isinstance(row, dict):
                take(row)

    take(plan)
    scoped = [row for row in _since_flat_boundary(records, account)
              if _account_matches(row, account)]
    sent_keys = {str(row.get("entryKey") or "") for row in scoped
                 if row.get("status") == PYRAMID_SENT and row.get("entryKey")}
    for row in scoped:
        take(row.get("plan"))
        if row.get("status") == PYRAMID_CLAIMED and str(row.get("entryKey") or "") in sent_keys:
            draft = row.get("postPlanDraft")
            tranches = draft.get("tranches") if isinstance(draft, dict) else None
            if isinstance(tranches, list) and tranches:
                take(tranches[-1])
        if row.get("status") == PYRAMID_DRYRUN:
            text = str(row.get("scenarioId") or "").strip()
            if text:
                decisions.add(text)
    return decisions


def _pyramid_adds_done(records: List[Dict[str, Any]], account: Any) -> int:
    """直近 FLAT 境界以降にコミットされた追撃の回数(§3.1-5)。"""
    return sum(1 for row in _since_flat_boundary(records, account)
               if row.get("action") == PYRAMID_COMMIT)


def _pyramid_wal(records: List[Dict[str, Any]], symbol: str,
                 account: Any) -> Optional[Dict[str, Any]]:
    """未コミットの追撃 WAL(§1.3)。コミット済み・回復済みなら ``None``。

    戻り値 ``{"claimed", "sent", "halt", "entryKey"}``。どの段まで進んだかは
    台帳の最後の PYRAMID_* 行が一意に示す —— 推測で埋める余地を残さない。
    """
    scoped = _since_flat_boundary(records, account)
    claimed_index = None
    for index in range(len(scoped) - 1, -1, -1):
        row = scoped[index]
        if row.get("status") != PYRAMID_CLAIMED:
            continue
        # 口座で必ず絞る。絞らないと多口座で A 口座の WAL を B 口座の周期が拾い、
        # B の建玉へ A のプランを束縛しようとして「scope 外」の hold を返す。それが
        # R46 の手動建玉除外(`_is_manual_position_account`)を壊し、経路全体が止まる。
        if not _account_matches(row, account):
            continue
        plan = row.get("prePlan") if isinstance(row.get("prePlan"), dict) else {}
        if str(plan.get("symbol") or "") == str(symbol):
            claimed_index = index
            break
    if claimed_index is None:
        return None
    claimed = scoped[claimed_index]
    key = str(claimed.get("key") or "")
    sent = halt = None
    for row in scoped[claimed_index + 1:]:
        if str(row.get("key") or "") != key:
            continue
        status = row.get("status")
        if row.get("action") == PYRAMID_COMMIT or _clears_halt(row):
            return None
        if status == PYRAMID_SENT:
            sent = row
        elif status == "HALT":
            halt = row
    return {"claimed": claimed, "sent": sent, "halt": halt, "entryKey": key}


def _non_terminal_order_ids(order_view: Any) -> set:
    terminal = set(execution_contract.CONTRACT["brokerObservation"]["terminalStates"])
    rows = (order_view or {}).get("orders") if isinstance(order_view, dict) else None
    return {str(row.get("orderId")) for row in rows or []
            if isinstance(row, dict) and row.get("orderId") not in (None, "")
            and str(row.get("status") or "").upper() not in terminal}


def _all_order_ids(order_view: Any) -> List[str]:
    rows = (order_view or {}).get("orders") if isinstance(order_view, dict) else None
    return sorted({str(row.get("orderId")) for row in rows or []
                   if isinstance(row, dict) and row.get("orderId") not in (None, "")})


def _pyramid_absence_proof(claimed: Dict[str, Any], position: Dict[str, Any],
                           order_view: Any) -> Tuple[bool, str]:
    """§9 の追撃専用「不在の証明」。

    既存の ENTRY 回復は **建玉ゼロ** を前提にしているので、保有中に起きる追撃の
    HALT には原理的に通らない。ここでは

      * 建玉が claim 時点の baseQty から **増えていない**
      * 現注文照会の非終端行の orderId が **送信前の集合の部分集合**(新しい行が無い)

    の両方が揃ったときだけ「追撃は届いていない」と読む。どちらか欠ければ実際に
    部分的に入っている可能性があるので自動では解かない(人が `--clear-halt`)。
    """
    if not isinstance(position, dict) or position.get("verified") is not True:
        return False, "PYRAMID_RECOVERY_POSITION_UNVERIFIED"
    if not isinstance(order_view, dict) or order_view.get("verified") is not True:
        return False, "PYRAMID_RECOVERY_ORDERS_UNVERIFIED"
    try:
        base_qty = int(claimed.get("baseQty"))
        observed = int(position.get("qty") or 0)
    except (TypeError, ValueError):
        return False, "PYRAMID_RECOVERY_QTY_INVALID"
    if observed != base_qty:
        return False, "PYRAMID_RECOVERY_POSITION_CHANGED: " + f"{observed} != base {base_qty}"
    pre_send = {str(value) for value in claimed.get("preSendOrderIds") or []}
    if not pre_send:
        return False, "PYRAMID_RECOVERY_PRESEND_SNAPSHOT_MISSING"
    unknown = sorted(_non_terminal_order_ids(order_view) - pre_send)
    if unknown:
        return False, "PYRAMID_RECOVERY_NEW_ORDER_ROWS: " + ",".join(unknown[:4])
    return True, "pyramid add never reached the broker (position unchanged, no new order rows)"


def _parse_modify_bracket(output: str) -> Optional[Dict[str, Any]]:
    """order.py が張り替え後に出す ``NQX_MODIFY_BRACKET`` 行(R84)。

    読めなければ ``None``。ここで推測して身元を作らない —— 呼び出し側は
    ブローカーへ聞き直す(``oco_sibling_pair``)経路へ落ちる。
    """
    for line in reversed(str(output or "").splitlines()):
        line = line.strip()
        if not line.startswith(MODIFY_BRACKET_MARKER):
            continue
        try:
            parsed = json.loads(line[len(MODIFY_BRACKET_MARKER):])
        except (json.JSONDecodeError, ValueError):
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _consolidation_from_bracket(bracket: Any, *, account: str, qty: int,
                                stop: float, target: Any) -> Optional[Dict[str, Any]]:
    rows = (bracket or {}).get("accounts") if isinstance(bracket, dict) else None
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        if str(row.get("accountId") or "") != str(account):
            continue
        ids = [str(row.get("stopOrderId") or ""), str(row.get("targetOrderId") or "")]
        receipts = [str(row.get("stopReceipt") or row.get("receipt") or ""),
                    str(row.get("targetReceipt") or row.get("receipt") or "")]
        if len(set(ids)) != 2 or not all(ids):
            return None
        return {"orderIds": sorted(ids),
                "receipts": sorted(value for value in receipts if value),
                "qty": int(qty), "sl": float(stop),
                "tp": (float(target) if target is not None else None)}
    return None


def _consolidation_from_broker(order_view: Any, *, account: str, symbol: str, side: str,
                               qty: int, stop: float, target: Any) -> Optional[Dict[str, Any]]:
    """R80 の判定器で「建玉に対する OCO 兄弟 1 組」を読み直す(order.py の行が無い時)。"""
    import broker_status
    rows = (order_view or {}).get("orders") if isinstance(order_view, dict) else None
    if not isinstance(rows, list):
        return None
    opposite = "SELL" if str(side).upper() == "BUY" else "BUY"
    pair, _detail = broker_status.oco_sibling_pair(
        rows, account=account, expected_action=opposite, symbol=symbol, all_rows=rows)
    if not pair:
        return None
    ids = sorted(str(row.get("orderId") or "") for row in pair)
    receipts = sorted(str(row.get("receipt") or "") for row in pair)
    if len(set(ids)) != 2 or not all(ids):
        return None
    return {"orderIds": ids, "receipts": [value for value in receipts if value],
            "qty": int(qty), "sl": float(stop),
            "tp": (float(target) if target is not None else None)}


def _pyramid_entry_ids(plan: Any) -> List[str]:
    """合成プランの **エントリー親** id だけ(ブラケット子は渡さない)。

    子 id を ``known_order_ids`` に混ぜると、ブローカーが per-order 詳細で終端行を
    返した瞬間に ``_bracket_structure`` が「active でない」= INCONSISTENT と読む。
    対の生死は「一覧に出るかどうか」で決めるので、子は解決させない。
    """
    ids = []
    for row in (plan or {}).get("tranches") or []:
        if not isinstance(row, dict):
            continue
        for leg in row.get("legs") or []:
            if isinstance(leg, dict) and str(leg.get("orderId") or ""):
                ids.append(str(leg["orderId"]))
    return ids


def _attach_bracket_ids(tranche_row: Dict[str, Any], order_view: Any, *,
                        account: str, symbol: str, action: str) -> Dict[str, Any]:
    """脚の凍結 identity に ``bracketOrderIds`` が無い場合だけ、真の親リンクから補う。

    R72 の /fills 経路で束縛された脚は ``bracketOrderIds`` を持たない。ブローカーの
    子行は ``brokerParentId`` に **真の親**(ENTRY 注文)を持つので、親 id ちょうど
    2 本の子が居るときだけ採る。1 本・3 本以上は曖昧なので何もしない。
    """
    import broker_status
    rows = (order_view or {}).get("orders") if isinstance(order_view, dict) else None
    if not isinstance(rows, list):
        return tranche_row
    opposite = "SELL" if str(action).upper() == "BUY" else "BUY"
    legs = []
    for leg in tranche_row.get("legs") or []:
        row = dict(leg) if isinstance(leg, dict) else {}
        parent = str(row.get("orderId") or "")
        existing = [str(value) for value in row.get("bracketOrderIds") or [] if str(value)]
        if parent and len(existing) != 2:
            children = [child for child in rows if isinstance(child, dict)
                        and str(child.get("brokerParentId") or "") == parent
                        and str(child.get("accountId") or child.get("account") or "") == str(account)
                        and str(child.get("symbol") or "") == str(symbol)
                        and str(child.get("action") or "").upper() == opposite
                        and str(child.get("status") or "").upper()
                        in broker_status.BROKER_ACTIVE_STATES]
            if len(children) == 2:
                row["bracketOrderIds"] = [str(child.get("orderId")) for child in children]
                row["bracketReceipts"] = [str(child.get("receipt") or "") for child in children]
        legs.append(row)
    return {**tranche_row, "legs": legs}


def _command_for_pyramid_entry(plan: Dict[str, Any], add_tranche: Dict[str, Any], *,
                               account: str, price: float, base_qty: int,
                               base_avg_entry: float, ultra_drawdown: float,
                               entry_key: Optional[str] = None,
                               claim_token: Optional[str] = None,
                               intent_hash: Optional[str] = None,
                               plan_version: Optional[str] = None) -> List[str]:
    """追撃の送信引数。**それ自体が完結した分割ブラケット付きエントリー**(§1.1)。

    既存トランシェの OCO には触れないので ``cancelandbracket`` は出てこない ——
    取消→新規の 2 段が存在しない = R78 の「取消だけ成立して裸」が構造的に起きない。
    """
    qty = sum(int(leg.get("qty") or 0) for leg in add_tranche.get("legs") or [])
    args = ["--side", str(plan["side"]).lower(), "--qty", str(qty),
            "--sl", str(add_tranche["initialStop"]),
            "--split-tp", ",".join(str(leg["target"]) for leg in add_tranche["legs"]),
            "--symbol", str(plan["symbol"]),
            "--ultra", "--ultra-drawdown", str(ultra_drawdown),
            "--accounts", str(account),
            "--market", "--last", str(price),
            "--pyramid",
            # R52: `--opt=value` の 1 要素。値が `-` で始まっても argparse が値と読む。
            "--pyramid-base=" + str(int(base_qty)) + "@" + format(float(base_avg_entry), ".4f")]
    if entry_key and claim_token and intent_hash:
        args += ["--entry-key=" + str(entry_key), "--claim-token=" + str(claim_token),
                 "--intent-hash=" + str(intent_hash),
                 "--plan-version=" + str(plan_version or "")]
    return args


def _pyramid_settle_orders(broker_order_query, symbol: str, known_ids: List[str],
                           attempts: int, delay: float):
    """送信直後は行がまだ出ない(R52 実測)。短く待って取り直す。"""
    view = None
    for attempt in range(max(1, attempts)):
        try:
            view = _query_orders_with_ids(broker_order_query, symbol, known_ids)
        except Exception:  # noqa: BLE001 - 一過性の照会失敗は次の試行へ
            view = None
        if isinstance(view, dict) and view.get("verified") is True:
            return view
        if delay > 0 and attempt + 1 < max(1, attempts):
            time.sleep(delay)
    return view


def _pyramid_ledger_key(ctx: Dict[str, Any]) -> str:
    return "PYRAMID:" + str(ctx.get("account") or "ACCOUNT_UNKNOWN") + ":" + str(ctx.get("symbol") or "")


def _pyramid_note_row(ctx: Dict[str, Any], status: str, reason: str,
                      entry_key: Optional[str], payload: Optional[Dict[str, Any]] = None,
                      *, terminal: bool = False) -> None:
    """判定注記を台帳へ 1 行だけ残す。

    §3.1-6 は「1 サイクル 1 行」。**連続する同じ理由**は積まない —— 建玉を数時間
    持つ間ずっと同じ理由が出るので、積むと台帳が理由の繰り返しで埋まる
    (注記そのものは毎周期 Telegram / report_line に出るので観測は落ちない)。
    """
    # 終端印(WAL を閉じる行)は WAL と同じ entryKey でなければならない。それ以外の
    # 見送りは **口座+銘柄の安定キー**にする —— entryKey はサイクルごとに変わるので、
    # キー込みで重複判定すると同じ理由を毎周期 1 行ずつ積んでしまう。
    row = {"key": (entry_key if (terminal and entry_key) else _pyramid_ledger_key(ctx)),
           "status": status, "action": "PYRAMID_EVALUATE", "reason": reason,
           "accountId": ctx.get("account"), "symbol": ctx.get("symbol")}
    if entry_key:
        row["entryKey"] = entry_key
    if payload:
        row.update(payload)
    if terminal:
        # WAL を閉じる印。`_clears_halt` が拾うので `_pyramid_wal` は None を返す。
        row["haltResolution"] = "PYRAMID_NOT_SENT"
    if not terminal:
        for previous in reversed(_since_flat_boundary(ctx.get("records") or [], ctx.get("account"))):
            if previous.get("status") not in (PYRAMID_SKIPPED, PYRAMID_DRYRUN):
                continue
            # 決定が違えば別の観測。同じ理由(例: PYRAMID_ADD)でも潰さない ——
            # 潰すと影運転の台帳から「どの決定で足していたか」が消える。
            if (previous.get("status") == status
                    and str(previous.get("reason") or "") == str(reason)
                    and str(previous.get("key") or "") == str(row["key"])
                    and str(previous.get("scenarioId") or "") == str(row.get("scenarioId") or "")):
                return
            break
    try:
        _append_ledger(row, ctx["ledger_path"])
    except OSError:
        pass


def _pyramid_skip(ctx: Dict[str, Any], reason: str, *, entry_key: Optional[str] = None,
                  detail: Optional[str] = None, payload: Optional[Dict[str, Any]] = None,
                  terminal: bool = False) -> List[str]:
    _pyramid_note_row(ctx, PYRAMID_SKIPPED, reason, entry_key, payload, terminal=terminal)
    note = "pyramid: " + str(reason)
    if detail:
        note += " (" + str(detail)[:200] + ")"
    return [note]


def _pyramid_base_tranches(plan: Dict[str, Any], account: str) -> List[Dict[str, Any]]:
    if str(plan.get("planKind") or "") == tranche.PLAN_KIND:
        return [dict(row) for row in plan.get("tranches") or [] if isinstance(row, dict)]
    return [tranche.tranche_from_plan(plan, tranche_id="T1", account=account)]


def _pyramid_consider(ctx: Dict[str, Any], plan: Dict[str, Any], position: Dict[str, Any],
                      order: Dict[str, Any], *, binding_state: str) -> List[str]:
    """§3.1: 建玉の管理が「何もしなくてよい」周期にだけ追撃を評価する。

    管理と撤退は常に追撃より優先する —— 呼び出し側は `management_action` が
    None を返した後でしかここへ来ない。
    """
    if not pyramid.enabled():
        return []
    standing_halt = _has_halt(ctx.get("records") or [])
    if standing_halt:
        # 追撃は新規 ENTRY。送信結果が不明なまま残る HALT の下で新しい成行を出さない
        # (FLAT 経路の `_has_halt` ゲートと同じ規律。管理と撤退は従来どおり動く)。
        return _pyramid_skip(ctx, "LEDGER_HALT",
                             detail=str(standing_halt.get("reason"))[:200])
    if _entry_disarmed(ctx.get("cfg")):
        # AUTO OFF。建玉管理と撤退だけを続ける周期で、追撃は **新規 ENTRY** なので出さない
        # (CLAUDE.md §3「AUTO OFF は新規 ENTRY を止める」。R82 が追撃を新規と同列に
        # 並べているのと同じ扱い)。この経路は AUTO が切れていても live フラグが立って
        # いるので、ここで止めなければ実際に送ってしまう。
        return _pyramid_skip(ctx, "ENTRY_DISARMED")
    account = str(ctx.get("account") or "")
    if not account:
        return _pyramid_skip(ctx, "ACCOUNT_UNKNOWN")
    if binding_state not in {"OWNED_FULL", "OWNED_COMPOSITE"}:
        # 部分約定(未約定の脚が残っている)状態へ建て増すと、どの脚がどの建玉かを
        # 構造で追えなくなる。届いていない脚が片付くまで待つ。
        return _pyramid_skip(ctx, "BASE_NOT_FULLY_FILLED", detail=binding_state)
    proposal = ctx.get("proposal")
    if not isinstance(proposal, dict) or not str(proposal.get("side") or ""):
        # そもそも候補が出ていない周期。追撃の「見送り」ではなく **評価対象が無い**
        # ので、注記も台帳行も増やさない(建玉を数時間持つ間の大半がこれ)。
        return []
    cfg = ctx.get("cfg")
    try:
        view = ctx["state_query"]() if ctx.get("state_query") is not None else None
        if view is None:
            import nqx_state
            view = nqx_state.fetch_state_quiet()
    except Exception as exc:  # noqa: BLE001
        return _pyramid_skip(ctx, "STATE_UNAVAILABLE", detail=f"{type(exc).__name__}: {exc}")
    sealed, seal_reason, authoritative = _authoritative_cycle_seal(
        view, ctx.get("proposal"), cfg=cfg, now=ctx.get("now"), pyramid_mode=True)
    if not sealed or not isinstance(authoritative, dict):
        return _pyramid_skip(ctx, "CYCLE_NOT_SEALED", detail=seal_reason)
    scenario = dict(authoritative)
    entry_key = _entry_key(scenario)
    if not entry_key:
        return _pyramid_skip(ctx, "ENTRY_IDENTITY_INCOMPLETE")
    records = ctx.get("records") or []
    if _latest(records, entry_key, PYRAMID_ENTRY_GUARD):
        # このシグナルは既に一度処理した。注記も台帳も増やさない。
        return []
    decision = str(scenario.get("scenarioId") or "").strip()
    if decision in _pyramid_consumed_decisions(records, account, plan):
        # **同じ決定の再掲は新規シグナルではない。** 基礎を作った決定・既に足した決定が
        # 次の周期にも ARMED のまま出てくるのは普通(実測で最長 14 連続)。追撃の
        # 「見送り」ではなく評価対象が無いので、注記も台帳行も増やさない。
        return []

    frozen_contract = (scenario.get("executionContract")
                       if isinstance(scenario.get("executionContract"), dict) else {})
    scope = [str(value) for value in frozen_contract.get("accountScope") or [] if str(value)]
    if scope and scope != [account]:
        return _pyramid_skip(ctx, "ACCOUNT_MISMATCH", entry_key=entry_key,
                             detail=",".join(scope))
    ultra_env = execution_contract.CONTRACT.get("ultra") or {}
    if str(frozen_contract.get("riskCapSource") or "") != str(ultra_env.get("riskCapSource") or ""):
        # ULTRA で焼き直されていないシナリオは「合計建玉を引き直した枚数」を持たない。
        return _pyramid_skip(ctx, "NOT_ULTRA_SCENARIO", entry_key=entry_key)
    frozen_cap = _num(frozen_contract.get("riskCapDollars"))
    if frozen_cap is None or frozen_cap <= 0:
        return _pyramid_skip(ctx, "RISK_CAP_UNAVAILABLE", entry_key=entry_key)
    cap_dollars = min(float(frozen_cap), float(ultra_env["maxRiskDollarsPerAccount"]))

    bundle = ctx.get("bundle") or {}
    add_price = _price_from_bundle(bundle)
    if add_price is None:
        return _pyramid_skip(ctx, "ADD_PRICE_UNAVAILABLE", entry_key=entry_key)
    # R84: 送信の `--last` は **publish された正本価格**でなければならない。Worker は
    # claim 時にその値で executionIntent を組み、CONSUME で byte 比較するため、別の
    # 値(送信直前の quote)を渡すと `ENTRY_CLAIM_INTENT_MISMATCH` で claim が宙吊る。
    # 代わりに、正本価格が今の気配から離れすぎている周期は見送る(Worker の CONSUME も
    # 同じ乖離で落とすので、ここで落としておく方が claim を汚さない)。
    limit = float(execution_contract.CONTRACT["marketOrder"]["maxDeviationPoints"])
    live_price = _live_price(ctx)
    if live_price is not None and abs(live_price - add_price) > limit + 1e-9:
        return _pyramid_skip(ctx, "ADD_PRICE_DEVIATION", entry_key=entry_key,
                             detail=f"published {add_price} vs quote {live_price}")

    adds_done = _pyramid_adds_done(records, account)
    verdict = pyramid.evaluate(
        owned_plan=plan, scenario=scenario, position=position, adds_done=adds_done,
        add_price=add_price, stop=scenario.get("stop"),
        target_total_qty=scenario.get("qty"), risk_cap_dollars=cap_dollars,
        point_value=POINT_VALUE)
    if not verdict.get("add"):
        return _pyramid_skip(ctx, str(verdict.get("reason") or "PYRAMID_NO_ADD"),
                             entry_key=entry_key, payload={"verdict": verdict})
    add_qty = int(verdict.get("qty") or 0)
    floor_qty = tranche.min_add_qty()
    if add_qty < floor_qty:
        return _pyramid_skip(ctx, "ADD_QTY_BELOW_SPLIT_MIN", entry_key=entry_key,
                             detail=f"{add_qty} < {floor_qty}", payload={"verdict": verdict})

    base_qty = int(position.get("qty") or 0)
    base_avg_entry = _num(position.get("avgEntry"))
    if base_avg_entry is None or base_avg_entry <= 0:
        return _pyramid_skip(ctx, "POSITION_ENTRY_UNAVAILABLE", entry_key=entry_key)
    base_tranches = _pyramid_base_tranches(plan, account)
    all_orders = order.get("orders") if isinstance(order, dict) else []
    all_orders = all_orders if isinstance(all_orders, list) else []
    side = str(plan.get("side") or "").upper()
    symbol = str(ctx.get("symbol") or "")
    # 身元の穴埋めはコミットと同じ手順で先にやる。R72 の /fills 束縛で bracket id を
    # 持たない脚は、ブローカーの子行(真の親リンク)から補える場合がある。
    base_tranches = [_attach_bracket_ids(row, order, account=account, symbol=symbol,
                                         action=side) for row in base_tranches]
    base_states = tranche.leg_states({"tranches": base_tranches}, all_orders,
                                     account=account, symbol=symbol, action=side)
    # **送る前に** 基礎が合成として読めることを要求する。読めないまま足すと、コミットで
    # bind_composite が INCONSISTENT になり、不在の証明も「建玉が増えた」で通らず、
    # 恒久的に遷移回廊(FLATTEN のみ)へ落ちる。見送れば基礎はこれまでどおり単一プラン
    # 経路(親行で照合できる)で管理され続ける。
    broken = next((row for row in base_states
                   if row.get("state") == tranche.INCONSISTENT), None)
    if broken is not None:
        return _pyramid_skip(ctx, "BASE_STRUCTURE_UNVERIFIABLE", entry_key=entry_key,
                             detail=f"{broken.get('trancheId')}/{broken.get('legId')}: "
                                    f"{broken.get('reason')}")
    base_expected = tranche.expected_qty(base_states)
    if base_expected != base_qty:
        return _pyramid_skip(ctx, "BASE_STRUCTURE_QTY_MISMATCH", entry_key=entry_key,
                             detail=f"structure {base_expected} != broker {base_qty}")
    tranche_id = "T" + str(len(base_tranches) + 1)
    try:
        add_tranche = tranche.build_add_tranche(
            scenario, tranche_id=tranche_id, add_qty=add_qty, add_price=add_price,
            entry_key=entry_key)
    except ValueError as exc:
        return _pyramid_skip(ctx, str(exc), entry_key=entry_key)

    summary = (f"pyramid: ADD {add_qty} -> {base_qty + add_qty} @ "
               f"{verdict.get('combinedEntry')} risk ${verdict.get('combinedRisk')} "
               f"(grade {verdict.get('grade')}, adds {adds_done + 1}/{verdict.get('maxAdds')})")
    if pyramid.dry_run():
        _pyramid_note_row(ctx, PYRAMID_DRYRUN, "PYRAMID_ADD", entry_key,
                          {"verdict": verdict, "baseQty": base_qty,
                           "baseAvgEntry": base_avg_entry, "scenarioId": decision})
        return [summary + " [dryRun: not sent]"]

    draft_states = list(base_states) + [
        {"trancheId": tranche_id, "legId": str(leg.get("id")).upper(),
         "qty": int(leg.get("qty") or 0), "target": leg.get("target"),
         "state": tranche.OPEN} for leg in add_tranche.get("legs") or []]
    try:
        pre_plan = tranche.build_composite_plan(
            plan, base_tranches, entry_key=str(_plan_entry_key(plan) or entry_key),
            account=account, states=base_states, adds_done=adds_done,
            combined_entry=base_avg_entry, trail_distance=_num(plan.get("trailDistance")))
        post_plan_draft = tranche.build_composite_plan(
            plan, base_tranches + [add_tranche], entry_key=entry_key, account=account,
            states=draft_states, adds_done=adds_done + 1,
            combined_entry=_num(verdict.get("combinedEntry")),
            combined_risk=_num(verdict.get("combinedRisk")),
            trail_distance=_num(plan.get("trailDistance")))
    except ValueError as exc:
        return _pyramid_skip(ctx, "COMPOSITE_PLAN_INVALID", entry_key=entry_key,
                             detail=str(exc))
    post_plan_draft["ultraDrawdown"] = cap_dollars
    post_plan_draft["riskCapDollars"] = cap_dollars
    post_plan_draft["riskCapSource"] = str(ultra_env.get("riskCapSource") or "")
    post_plan_draft["planVersion"] = str(scenario.get("planVersion") or plan.get("planVersion") or "")
    # R48: 特徴量タグは **記録専用**。採点の重み付けはしない(N が貯まるまで)。
    evidence = [str(value) for value in (post_plan_draft.get("decisionEvidence") or [])]
    if "PYRAMID_ADD" not in evidence:
        evidence.append("PYRAMID_ADD")
    post_plan_draft["decisionEvidence"] = evidence[:16]

    execute = ctx["execute"]
    if not ctx.get("live"):
        args = _command_for_pyramid_entry(
            plan, add_tranche, account=account, price=add_price, base_qty=base_qty,
            base_avg_entry=base_avg_entry, ultra_drawdown=cap_dollars)
        ok, detail = _execute_action(args, False, execute)
        if not ok:
            return _pyramid_skip(ctx, "DRY_RUN_REJECTED", entry_key=entry_key,
                                 detail=detail[-300:])
        return [summary + " [proposal: AUTO is not LIVE]"]

    ledger_path = ctx["ledger_path"]
    pre_send_ids = _all_order_ids(order)

    claimer = ctx.get("claim_entry")
    if claimer is None:
        import nqx_state
        claimer = nqx_state.claim_entry
    claim_payload = {"baseQty": base_qty, "baseAvgEntry": base_avg_entry,
                     "addQty": add_qty, "addsDone": adds_done,
                     "positionGeneration": ctx.get("generation"),
                     "preSendOrderIds": pre_send_ids}
    try:
        claim_ok, claim = claimer(scenario, order_type="MARKET", pyramid=claim_payload)
    except TypeError as exc:
        claim_ok, claim = False, {"reason": f"PYRAMID_CLAIM_UNSUPPORTED: {exc}"}
    except Exception as exc:  # noqa: BLE001
        claim_ok, claim = False, {"reason": f"{type(exc).__name__}: {exc}"}
    if (not claim_ok or not isinstance(claim, dict) or claim.get("entryKey") != entry_key
            or not claim.get("claimToken") or not claim.get("executionIntentHash")):
        # 送信前の拒否は HALT にしない(何も出ていない)。台帳にも WAL を残さない ——
        # claim が取れていない以上、解くべきものが無い。
        return _pyramid_skip(ctx, "CLAIM_REFUSED", entry_key=entry_key,
                             detail=str(claim)[:300])
    claim_journal = {"entryKey": entry_key, "claimToken": str(claim["claimToken"]),
                     "executionIntentHash": str(claim["executionIntentHash"]),
                     "executionIntent": claim.get("executionIntent")}
    # **claim を取った後に、claimJournal ごと WAL を書く。** 順序を逆にすると、
    # order.py の門で落ちた周期の claim がローカルに残らず、建玉が決済されて FLAT 経路へ
    # 落ちた瞬間に「authoritative ENTRY lock has no durable local claim journal」で
    # 毎周期止まる(CLAIM を送らないので DO の stale 解放も動かない = 恒久ロック)。
    # 単一プラン経路も同じ順序(claim → claimJournal 付きで ENTRY_CLAIMED)。
    claim_row = {"key": entry_key, "entryKey": entry_key, "status": PYRAMID_CLAIMED,
                 "action": "PYRAMID_ENTRY", "accountId": account,
                 "symbol": ctx.get("symbol"),
                 "prePlan": pre_plan, "postPlanDraft": post_plan_draft,
                 "baseQty": base_qty, "baseAvgEntry": base_avg_entry,
                 "addQty": add_qty, "addPrice": add_price,
                 "preSendOrderIds": pre_send_ids, "verdict": verdict,
                 "positionGeneration": ctx.get("generation"),
                 "claimJournal": claim_journal}
    try:
        _append_ledger(claim_row, ledger_path)
    except OSError as exc:
        return [f"AUTOTRADE HALT: pyramid durable journal failed ({exc})"]
    ctx["records"] = list(records) + [claim_row]
    args = _command_for_pyramid_entry(
        plan, add_tranche, account=account, price=add_price, base_qty=base_qty,
        base_avg_entry=base_avg_entry, ultra_drawdown=cap_dollars,
        entry_key=entry_key, claim_token=str(claim["claimToken"]),
        intent_hash=str(claim["executionIntentHash"]),
        plan_version=post_plan_draft.get("planVersion"))
    ok, detail = _execute_action(args, True, execute)
    if not ok and str(detail).startswith("dry-run rejected:"):
        # order.py の門で落ちた = 何も送っていない。WAL を閉じて次のシグナルへ。
        return _pyramid_skip(ctx, "DRY_RUN_REJECTED", entry_key=entry_key,
                             detail=str(detail)[-300:], terminal=True)
    envelope = route_envelope.parse(detail, expected_accounts=[account])
    route_state = str(envelope.get("state") or "UNKNOWN") if envelope.get("ok") else "UNKNOWN"
    route_snapshot = (envelope.get("snapshot") or []) if envelope.get("ok") else []
    sent_row = {"key": entry_key, "entryKey": entry_key, "status": PYRAMID_SENT,
                "action": "PYRAMID_ENTRY", "accountId": account,
                "symbol": ctx.get("symbol"), "routeState": route_state,
                "routeSnapshot": route_snapshot, "claimJournal": claim_journal,
                "result": str(detail)[-2000:]}
    try:
        _append_ledger(sent_row, ledger_path)
    except OSError as exc:
        return [f"AUTOTRADE HALT: pyramid durable journal failed after send ({exc})"]
    ctx["records"] = list(ctx["records"]) + [sent_row]
    notes = [summary + f" [sent: route {route_state}]"]
    if not ok or route_state != "SENT":
        _append_ledger({"key": entry_key, "entryKey": entry_key, "status": "HALT",
                        "action": {"action": "PYRAMID_ENTRY", "qty": add_qty},
                        "accountId": account, "symbol": ctx.get("symbol"),
                        "reason": f"pyramid add route {route_state}: {str(detail)[-500:]}"},
                       ledger_path)
        return notes + [f"AUTOTRADE HALT: pyramid add route {route_state}; "
                        "do not resend, verify the broker position"]
    wal = {"claimed": claim_row, "sent": sent_row, "halt": None, "entryKey": entry_key}
    committed, commit_notes = _pyramid_commit(ctx, wal)
    notes += commit_notes
    if committed is None:
        notes.append("pyramid: commit pending (bracket identity not settled yet); "
                     "transition corridor protects the position")
    return notes


def _pyramid_commit(ctx: Dict[str, Any], wal: Dict[str, Any]
                    ) -> Tuple[Optional[Dict[str, Any]], List[str]]:
    """§3.3: 送信済みの追撃へ route identity を焼き、合成プランを正本へ切り替える。

    束縛が数秒で揃わないのは正常(送信直後は行が未出現。R52 実測)。揃わなければ
    そのサイクルは PYRAMID_SENT のまま終わり、次サイクルと fill_watch が再試行する。
    その間の所有権は §4.3 の遷移回廊が守る。
    """
    claimed = wal.get("claimed") or {}
    sent = wal.get("sent") or {}
    draft = claimed.get("postPlanDraft")
    if not isinstance(draft, dict):
        return None, ["pyramid: commit skipped (no frozen draft plan)"]
    account = str(ctx.get("account") or "")
    symbol = str(ctx.get("symbol") or "")
    side = str(draft.get("side") or "").upper()
    tranches = [dict(row) for row in draft.get("tranches") or [] if isinstance(row, dict)]
    if not tranches:
        return None, ["pyramid: commit skipped (draft has no tranches)"]
    route = sent.get("routeSnapshot") if isinstance(sent.get("routeSnapshot"), list) else []
    tranches[-1] = tranche.bind_tranche_identity(tranches[-1], route, account=account)
    attempts, delay = tranche.commit_settle()
    query = ctx["query"]
    order_query = ctx["order_query"]
    ledger_path = ctx["ledger_path"]
    base_identity = str(ctx.get("rawIdentity") or "")
    generation = ctx.get("generation")
    last_reason = "unknown"
    for attempt in range(max(1, attempts)):
        try:
            position = query(symbol)
        except Exception as exc:  # noqa: BLE001
            last_reason = f"position query failed ({type(exc).__name__})"
            position = None
        if not isinstance(position, dict) or position.get("verified") is not True:
            last_reason = "broker position is UNVERIFIED"
        else:
            identity = _position_generation(position)
            if identity and identity != base_identity:
                fresh_records, ledger_error = _read_ledger(ledger_path)
                if ledger_error:
                    return None, [f"AUTOTRADE HALT: {ledger_error}"]
                generation = _observe_position_generation(fresh_records, position, ledger_path)
                base_identity = identity
                ctx["records"] = fresh_records
                ctx["generation"] = generation
                ctx["rawIdentity"] = identity
            orders = _pyramid_settle_orders(
                order_query, symbol, _pyramid_entry_ids({"tranches": tranches}), 1, 0.0)
            if isinstance(orders, dict) and orders.get("verified") is True:
                tranches = [_attach_bracket_ids(row, orders, account=account, symbol=symbol,
                                                action=side) for row in tranches]
                states = tranche.leg_states({"tranches": tranches},
                                            orders.get("orders") or [], account=account,
                                            symbol=symbol, action=side)
                try:
                    composite = tranche.build_composite_plan(
                        draft, tranches, entry_key=str(draft.get("entryKey") or ""),
                        account=account, states=states,
                        adds_done=int((draft.get("pyramid") or {}).get("addsDone") or 0),
                        combined_entry=_num(position.get("avgEntry")),
                        combined_risk=_num((draft.get("pyramid") or {}).get("combinedRisk")),
                        trail_distance=_num(draft.get("trailDistance")))
                except ValueError as exc:
                    return None, [f"pyramid: commit blocked ({exc})"]
                composite["ultraDrawdown"] = draft.get("ultraDrawdown")
                composite["riskCapDollars"] = draft.get("riskCapDollars")
                composite["riskCapSource"] = draft.get("riskCapSource")
                composite["planVersion"] = draft.get("planVersion")
                composite["decisionEvidence"] = draft.get("decisionEvidence")
                binding = tranche.bind_composite(composite, position, orders,
                                                 position_generation=generation)
                if binding.get("owned") and binding.get("state") == "OWNED_COMPOSITE":
                    owned = binding["ownedPlan"]
                    row = {"key": str(draft.get("entryKey") or ""),
                           "entryKey": str(draft.get("entryKey") or ""),
                           "status": "ENTRY_SENT", "action": PYRAMID_COMMIT,
                           "accountId": account, "symbol": symbol,
                           "routeState": str(sent.get("routeState") or "SENT"),
                           "plan": owned,
                           "claimJournal": sent.get("claimJournal"),
                           # 送信が UNKNOWN で HALT を書いていた場合、この行が解除になる。
                           "haltResolution": HALT_RESOLUTION_PYRAMID_COMMIT,
                           "result": "pyramid composite frozen: expected qty "
                                     + str(binding.get("expectedQty"))}
                    _append_ledger(row, ledger_path)
                    ctx["records"] = list(ctx.get("records") or []) + [row]
                    return owned, ["autotrade pyramid committed: composite plan frozen "
                                   f"({len(tranches)} tranches, expected qty "
                                   f"{binding.get('expectedQty')})"]
                last_reason = str(binding.get("reason") or "composite binding refused")
            else:
                last_reason = "broker orders are UNVERIFIED"
        if delay > 0 and attempt + 1 < max(1, attempts):
            time.sleep(delay)
    return None, ["pyramid: commit not settled (" + last_reason[:200] + ")"]


def _pyramid_settle(ctx: Dict[str, Any], wal: Dict[str, Any], position: Dict[str, Any],
                    order: Dict[str, Any]) -> Tuple[List[str], bool]:
    """未コミットの WAL を片付ける。戻り値 ``(notes, resume)``。

    ``resume=True`` は「この周期の通常フローへ戻ってよい」。回廊にいる間は
    **FLATTEN 判定だけ**を行い、MODIFY も追撃の再評価もしない(§4.3)。
    """
    claimed = wal.get("claimed") or {}
    entry_key = str(wal.get("entryKey") or "")
    ledger_path = ctx["ledger_path"]
    proven, detail = _pyramid_absence_proof(claimed, position, order)
    if proven:
        row = {"key": entry_key, "entryKey": entry_key, "status": "ENTRY_RECOVERED",
               "action": PYRAMID_RECOVERY, "accountId": ctx.get("account"),
               "symbol": ctx.get("symbol"), "haltResolution": "PYRAMID_ADD_ABSENT",
               "result": detail}
        _append_ledger(row, ledger_path)
        ctx["records"] = list(ctx.get("records") or []) + [row]
        return ["autotrade pyramid recovered: " + detail], True
    if wal.get("sent") is not None:
        committed, notes = _pyramid_commit(ctx, wal)
        if committed is not None:
            return notes, True
    else:
        notes = []
    # ---- 遷移回廊(§4.3) ----
    # コミットできないまま長く居座るのは「送ったが経路 identity を束縛できない」状態。
    # 黙って FLATTEN 専用のまま続けると、建値移動もトレールも掛からない建玉が一日
    # 残る(R74/R78 と同じ形)。1 度だけ HALT を記録して人へ見せる —— 回廊は残るので
    # 撤退と KILL は引き続き動く。
    if wal.get("halt") is None:
        opened = _parse_at(claimed.get("time"))
        reference = (ctx.get("now") or datetime.now(timezone.utc))
        if opened is not None and (reference - opened).total_seconds() > PYRAMID_COMMIT_ESCALATE_SEC:
            _append_ledger({"key": entry_key, "entryKey": entry_key, "status": "HALT",
                            "action": {"action": "PYRAMID_ENTRY",
                                       "qty": claimed.get("addQty")},
                            "accountId": ctx.get("account"), "symbol": ctx.get("symbol"),
                            "reason": "pyramid add could not be committed within "
                                      f"{int(PYRAMID_COMMIT_ESCALATE_SEC)}s; route identity "
                                      "is unbound. Verify the broker position by hand."},
                           ledger_path)
            notes = notes + ["AUTOTRADE HALT: pyramid commit unbound; verify the broker "
                             "position (corridor keeps FLATTEN/KILL alive)"]
            wal = dict(wal, halt=True)
    pre_plan = claimed.get("prePlan")
    add_qty = claimed.get("addQty")
    if not isinstance(pre_plan, dict):
        return notes + ["autotrade hold: pyramid WAL has no frozen pre-plan"], False
    binding = tranche.bind_composite(pre_plan, position, order,
                                     position_generation=ctx.get("generation"),
                                     corridor_add_qty=add_qty)
    if not binding.get("owned"):
        return notes + ["autotrade hold: pyramid transition corridor refused ownership "
                        f"({binding.get('reason')})"], False
    plan = binding["ownedPlan"]
    plan_key = _plan_entry_key(plan) or entry_key
    action = tranche.management_action_composite(
        plan, position, price=_composite_reference_price(ctx),
        states=binding.get("legStates"), transit=True,
        stop_buffer=_stop_market_buffer(ctx.get("cfg")),
        force_flatten=(ctx.get("bundle") or {}).get("forceFlatten") is True,
        kill=kill_enabled(ctx.get("cfg")),
        session_flatten=_session_flatten_due(ctx.get("now"), ctx.get("cfg")))
    if not action:
        return notes + ["autotrade hold: pyramid transition corridor "
                        f"(qty {position.get('qty')} within [{binding.get('expectedQty')}, "
                        f"{int(binding.get('expectedQty') or 0) + int(add_qty or 0)}])"], False
    if action.get("action") == "HALT":
        return notes + [f"AUTOTRADE HALT: {action.get('reason')}"], False
    return notes + _composite_execute(ctx, plan, plan_key, action, position, states=None), False


def _live_price(ctx: Dict[str, Any]) -> Optional[float]:
    """送信直前の現在値。**1 周期に 1 回だけ**取る(quote は子プロセス 1-2 秒)。

    単一プラン経路は `management_action` の直前に既に取っているので、その値を
    そのまま ctx へ載せて使い回す。二度目の取得は reconcile ロックの中で
    まるごと待ち時間になる。
    """
    if "livePrice" in ctx:
        return ctx["livePrice"]
    fresh = _fresh_price(ctx.get("fresh_price_query") or _FRESH_PRICE_QUERY,
                         str(ctx.get("symbol") or ""))
    ctx["livePrice"] = fresh
    return fresh


def _composite_reference_price(ctx: Dict[str, Any]) -> Optional[float]:
    fresh = _live_price(ctx)
    return fresh if fresh is not None else _price_from_bundle(ctx.get("bundle") or {})


def _composite_execute(ctx: Dict[str, Any], plan: Dict[str, Any], plan_key: str,
                       action: Dict[str, Any], position: Dict[str, Any],
                       states: Optional[List[Dict[str, Any]]],
                       previous_stop: Optional[float] = None) -> List[str]:
    """合成プランの MODIFY / FLATTEN を送る。既存経路の作法をそのまま踏む。"""
    cfg = ctx.get("cfg")
    account = str(ctx.get("account") or "")
    ledger_path = ctx["ledger_path"]
    execute = ctx["execute"]
    query = ctx["query"]
    symbol = str(ctx.get("symbol") or plan.get("symbol") or "")
    records = ctx.get("records") or []
    open_qty = int(position.get("qty") or 0)
    key = (f"{plan_key}:{account or 'ACCOUNT_UNKNOWN'}:{action['action']}:"
           f"{action.get('sl', '')}:{action.get('qty', open_qty)}")
    if _latest(records, key, {"MANAGEMENT_SENT", "FLATTEN_SENT"}):
        return [f"autotrade idempotent skip: {key}"]
    if not ctx.get("live"):
        args = (_command_for_modify(plan, action, _position_generation(position), None,
                                    account=account or None,
                                    last=_composite_reference_price(ctx))
                if action["action"] == "MODIFY" else _command_for_flatten(account or None))
        if action.get("consolidate"):
            args = args + ["--pyramid-consolidate"]
        ok, detail = _execute_action(args, False, execute)
        if not ok:
            return [f"autotrade dry-run rejected: {detail[-300:]}"]
        return [f"autotrade proposal {action['action']}: {detail[-200:]}"]

    claim = None
    claim_journal = None
    if action["action"] == "MODIFY":
        try:
            intent = management_intent.build(
                account_id=position.get("accountId") or position.get("account"),
                symbol=plan["symbol"], position_generation=_position_generation(position),
                side=plan["side"], qty=action["qty"], stop=action["sl"], target=action["tp"])
        except ValueError as exc:
            return [f"AUTOTRADE HALT: management intent invalid ({exc})"]
        claimer = ctx.get("claim_management")
        if claimer is None:
            import nqx_state
            claimer = nqx_state.claim_management
        try:
            claim_ok, claim = claimer(intent)
        except Exception as exc:  # noqa: BLE001
            claim_ok, claim = False, {"reason": f"{type(exc).__name__}: {exc}"}
        if (not claim_ok or not isinstance(claim, dict)
                or claim.get("managementKey") != management_intent.management_key(intent)
                or claim.get("managementIntentHash") != management_intent.intent_hash(intent)
                or not claim.get("claimToken")):
            return [f"AUTOTRADE HALT: MANAGEMENT claim unavailable ({claim})"]
        claim_journal = {"managementKey": claim["managementKey"],
                         "claimToken": str(claim["claimToken"]),
                         "managementIntentHash": str(claim["managementIntentHash"]),
                         "managementIntent": intent}
        try:
            _append_ledger({"key": key, "status": "MANAGEMENT_CLAIMED", "action": action,
                            "planEntryKey": plan_key, "plan": plan,
                            "managementClaimJournal": claim_journal}, ledger_path)
        except OSError as exc:
            return [f"AUTOTRADE HALT: MANAGEMENT durable journal failed ({exc})"]
    reference_price = _composite_reference_price(ctx)
    args = (_command_for_modify(plan, action, _position_generation(position), claim,
                                account=account or None, last=reference_price)
            if action["action"] == "MODIFY" else _command_for_flatten(account or None))
    if action.get("consolidate"):
        args = args + ["--pyramid-consolidate"]
    ok, detail = _execute_action(args, True, execute)
    if not ok:
        if action["action"] == "MODIFY" and "MODIFY_STOP_NOT_PROTECTIVE" in detail:
            return ["autotrade hold: modify skipped, stop not protective at send time "
                    f"(bracket untouched): {detail[-300:]}"]
        if action["action"] == "MODIFY":
            live_legs = len(tranche.open_legs(states or plan.get("legStates") or []))
            outcome: Dict[str, Any] = {}
            handled, repair_notes, halt_prefix = _repair_unprotected_runner(
                plan=plan, position=position, action=action, failure=detail,
                failed_key=key, plan_key=plan_key, failed_journal=claim_journal,
                previous_stop=(previous_stop if _num(previous_stop) is not None
                               else _num((plan.get("consolidation") or {}).get("sl"))
                               if _num((plan.get("consolidation") or {}).get("sl")) is not None
                               else _num(plan.get("initialStop"))),
                price=reference_price,
                cfg=cfg, symbol=symbol, account=account or None, open_qty=open_qty,
                query=query, broker_order_query=ctx["order_query"],
                claimer=ctx.get("claim_management"), execute=execute,
                ledger_path=ledger_path, expected_pairs=max(1, live_legs),
                modify_extra_args=["--pyramid-consolidate"], outcome=outcome)
            if handled:
                if outcome.get("action") == "MODIFY":
                    # 修復も `cancelandbracket` なので、凍結していた対はもう無い。
                    # 張り替えた 1 組を凍結し直さないと、次の周期に全脚 CLOSED と
                    # 読まれて所有権を失う(修復したのに管理外になる)。
                    repair_notes = repair_notes + _freeze_consolidation(
                        ctx, plan, plan_key,
                        {"qty": int(outcome.get("qty") or open_qty),
                         "sl": outcome.get("sl"), "tp": outcome.get("tp")},
                        str(outcome.get("result") or ""))
                return repair_notes
            detail = halt_prefix + detail
        _append_ledger({"key": key, "status": "HALT", "action": action,
                        "planEntryKey": plan_key, "plan": plan, "reason": detail,
                        "managementClaimJournal": claim_journal}, ledger_path)
        return [f"AUTOTRADE HALT: {detail}"]
    verified, verify_detail = _verify_after_action(query, plan["symbol"], action["action"],
                                                   plan.get("side"))
    if not verified:
        notes = []
        if action["action"] == "MODIFY":
            # 送信は通っている = `cancelandbracket` は走り、凍結していた対はもう無い。
            # 検証が失敗したからといって再凍結を飛ばすと、次の周期に全脚 CLOSED と
            # 読まれて **HALT の上に所有権喪失が重なる**(実弾が完全に管理外になる)。
            notes = _freeze_consolidation(ctx, plan, plan_key, action, detail)
        _append_ledger({"key": key, "status": "HALT", "action": action,
                        "planEntryKey": plan_key, "plan": plan, "reason": verify_detail,
                        "managementClaimJournal": claim_journal}, ledger_path)
        return notes + [f"AUTOTRADE HALT: {verify_detail}"]
    status = "MANAGEMENT_SENT" if action["action"] == "MODIFY" else "FLATTEN_SENT"
    _append_ledger({"key": key, "status": status, "action": action,
                    "planEntryKey": plan_key, "plan": plan, "result": detail[-2000:],
                    "managementClaimJournal": claim_journal}, ledger_path)
    notes = [f"autotrade {status.lower()}: {action['reason']}"]
    if action["action"] != "MODIFY":
        return notes
    notes += _freeze_consolidation(ctx, plan, plan_key, action, detail)
    return notes


def _freeze_consolidation(ctx: Dict[str, Any], plan: Dict[str, Any], plan_key: str,
                          action: Dict[str, Any], output: str) -> List[str]:
    """張り替えで新しくなった保護注文 1 組を凍結し、トランシェ群を畳む(§5.3)。

    **これを落とすと次の周期に所有権が落ちる** —— 古い bracket id はもう存在せず、
    構造導出枚数が 0 になるからである。身元は (1) order.py の
    ``NQX_MODIFY_BRACKET`` 行、(2) ブローカーへの聞き直し(R80 の OCO 兄弟)の順に取り、
    どちらも取れなければ **pending** として凍結する(枚数は一致させ、管理は
    FLATTEN だけに絞る。次周期が身元を取り直して畳み直す)。
    """
    account = str(ctx.get("account") or "")
    symbol = str(ctx.get("symbol") or plan.get("symbol") or "")
    qty = int(action.get("qty") or plan.get("qty") or 0)
    stop = float(action["sl"])
    target = action.get("tp")
    consolidation = _consolidation_from_bracket(
        _parse_modify_bracket(output), account=account, qty=qty, stop=stop, target=target)
    if consolidation is None:
        attempts, delay = tranche.commit_settle()
        for attempt in range(max(1, attempts)):
            view = _pyramid_settle_orders(ctx["order_query"], symbol, [], 1, 0.0)
            consolidation = _consolidation_from_broker(
                view, account=account, symbol=symbol, side=str(plan.get("side") or ""),
                qty=qty, stop=stop, target=target)
            if consolidation is not None:
                break
            if delay > 0 and attempt + 1 < max(1, attempts):
                time.sleep(delay)
    pending = consolidation is None
    if pending:
        consolidation = {"orderIds": [], "receipts": [], "qty": qty, "sl": stop,
                         "tp": (float(target) if target is not None else None),
                         "pending": True}
    try:
        collapsed = tranche.collapse_to_consolidated(plan, consolidation=consolidation)
    except ValueError as exc:
        return [f"AUTOTRADE HALT: pyramid consolidation freeze failed ({exc})"]
    row = {"key": plan_key, "entryKey": plan_key, "status": "ENTRY_SENT",
           "action": PYRAMID_CONSOLIDATED, "accountId": account, "symbol": symbol,
           "plan": collapsed, "routeState": "SENT",
           "result": ("replacement OCO pair bound" if not pending
                      else "replacement OCO pair identity pending")}
    _append_ledger(row, ctx["ledger_path"])
    ctx["records"] = list(ctx.get("records") or []) + [row]
    if pending:
        return ["pyramid: consolidated plan frozen with a pending protective pair; "
                "management is limited to FLATTEN until the pair is bound"]
    return ["pyramid: consolidated into one protected position "
            f"({consolidation['orderIds'][0]}+{consolidation['orderIds'][1]})"]


def _resolve_pending_consolidation(ctx: Dict[str, Any], plan: Dict[str, Any],
                                   order: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """pending の統合対を、あとから `oco_sibling_pair` で束縛して畳み直す。"""
    pending = None
    for row in plan.get("tranches") or []:
        pair = row.get("consolidationPair") if isinstance(row, dict) else None
        if isinstance(pair, dict) and pair.get("pending") is True:
            pending = pair
            break
    if pending is None:
        return None
    account = str(ctx.get("account") or "")
    symbol = str(ctx.get("symbol") or plan.get("symbol") or "")
    consolidation = _consolidation_from_broker(
        order, account=account, symbol=symbol, side=str(plan.get("side") or ""),
        qty=int(pending.get("qty") or 0), stop=float(pending.get("sl") or 0.0),
        target=pending.get("tp"))
    if consolidation is None:
        return None
    try:
        collapsed = tranche.collapse_to_consolidated(plan, consolidation=consolidation)
    except ValueError:
        return None
    plan_key = _plan_entry_key(plan) or ""
    row = {"key": plan_key, "entryKey": plan_key, "status": "ENTRY_SENT",
           "action": PYRAMID_CONSOLIDATED, "accountId": account, "symbol": symbol,
           "plan": collapsed, "routeState": "SENT",
           "result": "pending replacement OCO pair bound from broker truth"}
    _append_ledger(row, ctx["ledger_path"])
    ctx["records"] = list(ctx.get("records") or []) + [row]
    return collapsed


def _composite_management(ctx: Dict[str, Any], plan_record: Dict[str, Any],
                          position: Dict[str, Any], order: Dict[str, Any]) -> List[str]:
    """合成プラン(planKind=PYRAMID_COMPOSITE)の 1 周期。

    `ownership_binder.bind()` は呼ばない —— 単一プランの実戦検証済み経路を
    追撃実装の巻き添えにしない(§4.1)。
    """
    plan = plan_record.get("plan")
    generation = ctx.get("generation")
    if not generation:
        return ["autotrade hold: broker position strong identity is unavailable"]
    resolved = _resolve_pending_consolidation(ctx, plan, order)
    if resolved is not None:
        plan = resolved
    binding = tranche.bind_composite(plan, position, order, position_generation=generation)
    if not binding.get("owned"):
        return [f"autotrade hold: composite position ownership UNKNOWN ({binding.get('reason')})"]
    owned = binding["ownedPlan"]
    previous = (plan.get("positionOwnership")
                if isinstance(plan.get("positionOwnership"), dict) else {})
    if str(previous.get("generation") or "") != str(generation):
        row = {"key": _plan_entry_key(plan), "entryKey": _plan_entry_key(plan),
               "status": "ENTRY_SENT", "action": PYRAMID_OWNERSHIP_BOUND,
               "accountId": ctx.get("account"), "symbol": ctx.get("symbol"),
               "plan": owned, "routeState": plan_record.get("routeState") or "SENT"}
        _append_ledger(row, ctx["ledger_path"])
        ctx["records"] = list(ctx.get("records") or []) + [row]
        return ctx["continue_management"](
            "autotrade adopted composite broker route/position ownership")
    plan = owned
    plan_key = _plan_entry_key(plan) or f"LEGACY:{plan.get('scenarioId') or 'UNKNOWN'}"
    records = ctx.get("records") or []
    current_stop = None
    for record in records:
        if (record.get("status") == "MANAGEMENT_SENT"
                and str(record.get("planEntryKey") or _plan_entry_key(record.get("plan")))
                == plan_key):
            current_stop = _num((record.get("action") or {}).get("sl"))
    reference_price = _composite_reference_price(ctx)
    bundle = ctx.get("bundle") or {}
    management_bundle = bundle
    if reference_price is not None:
        management_bundle = {**bundle, "price": reference_price, "priceSource": "FRESH_QUOTE"}
    best = _extreme(management_bundle, plan["side"], _parse_at(position.get("filledAt")))
    action = tranche.management_action_composite(
        plan, position, price=reference_price, states=binding.get("legStates"),
        current_stop=current_stop, best=best,
        stop_buffer=_stop_market_buffer(ctx.get("cfg")),
        breakeven_offset=BREAKEVEN_OFFSET_POINTS,
        force_flatten=bundle.get("forceFlatten") is True,
        kill=kill_enabled(ctx.get("cfg")),
        session_flatten=_session_flatten_due(ctx.get("now"), ctx.get("cfg")),
        transit=binding.get("state") == "PYRAMID_TRANSIT")
    if action and action.get("action") == "HALT":
        return [f"AUTOTRADE HALT: {action['reason']}"]
    if action:
        # R78 修復が戻す「直前の stop」は **最後に受理された SL**。F で initialStop を
        # 構造 SL(R の分母)へ固定したので、両者はもう別物である。
        return _composite_execute(ctx, plan, plan_key, action, position,
                                  binding.get("legStates"), previous_stop=current_stop)
    phase = binding.get("phase")
    notes = [f"autotrade hold: managed composite {plan_key} ({phase}, expected qty "
             f"{binding.get('expectedQty')})"]
    if binding.get("state") == "PYRAMID_TRANSIT":
        notes.append("pyramid: protective pair identity pending; FLATTEN only")
        return notes
    if phase == tranche.ACCUMULATION:
        notes.append("pyramid: BE deferred (TP1 legs live)")
    considered, guard_notes = _pyramid_guard(
        "consider", lambda: _pyramid_consider(ctx, plan, position, order,
                                              binding_state="OWNED_COMPOSITE"), [])
    return notes + list(considered) + guard_notes


def _reconcile_one(bundle: Dict[str, Any], state_ok: bool = True,
              cfg: Optional[Dict[str, str]] = None,
              broker_query: Optional[Callable[[str], Dict[str, Any]]] = None,
              runner: Optional[Callable[[List[str], bool], Tuple[int, str]]] = None,
              ledger_path: str = LEDGER_FILE,
              now: Optional[datetime] = None,
              broker_order_query: Optional[Callable[[str], Dict[str, Any]]] = None,
              state_query: Optional[Callable[[], Dict[str, Any]]] = None,
              claim_entry: Optional[Callable[[Dict[str, Any]], Tuple[bool, Dict[str, Any]]]] = None,
              claim_management: Optional[Callable[[Dict[str, Any]], Tuple[bool, Dict[str, Any]]]] = None,
              recover_entry: Optional[Callable[..., Tuple[bool, Dict[str, Any]]]] = None,
              recover_management: Optional[Callable[..., Tuple[bool, Dict[str, Any]]]] = None,
              broker_fills_query: Optional[Callable[[str], Dict[str, Any]]] = None,
              fresh_price_query: Optional[Callable[[str], Optional[float]]] = None,
              chain_management: bool = True) -> List[str]:
    """R16 reconcile: broker truth first, ENTRY cycle seal only when FLAT.

    Existing-position management and emergency exit are derived solely from a
    verified broker position plus the immutable plan in the local ledger.  A
    current cycle is required only to create a new position.  All external
    effects remain behind the injected/order.py runner and its explicit live
    switch.
    """
    cfg = dict(_read_env_file(), **(cfg or {}))
    # R82: manualHalt 中は KILL の撤退だけを通す。halt を見ない同じ二重キーで判定し、
    # 下の KILL 分岐から先(建玉管理・追撃・新規 ENTRY)へは進ませない。以前はここで
    # 空が返り、reconcile() が KILL を通しても建玉の全決済が一度も送られなかった。
    exit_only = manual_halt(cfg) and kill_enabled(cfg)
    if not (_kill_exit_switches(cfg)[0] if exit_only else autotrade_enabled(cfg)):
        return []
    if not isinstance(bundle, dict):
        return ["autotrade blocked: bundle is not an object"]
    records, ledger_error = _read_ledger(ledger_path)
    if ledger_error:
        return [f"AUTOTRADE HALT: {ledger_error}"]

    kill_requested = kill_enabled(cfg)
    halt = _has_halt(records)

    proposal = (bundle.get("_published_scenario") or {}) if "_published_scenario" in bundle else (
        (bundle.get("scenarios") or {}).get("primary") or {})
    symbol = str(proposal.get("symbol") or cfg.get("NQX_SYMBOL") or contract_month.symbol())
    query = broker_query
    if query is None:
        import broker_status
        query = broker_status.query_position
    try:
        position = query(symbol)
    except Exception as exc:  # noqa: BLE001
        return [f"autotrade blocked: broker query failed ({type(exc).__name__}: {exc})"]
    if not isinstance(position, dict) or position.get("verified") is not True:
        return ["autotrade blocked: broker position is UNVERIFIED"]
    try:
        open_qty = int(position.get("qty") or 0)
    except (TypeError, ValueError):
        return ["autotrade blocked: broker position qty is invalid"]
    if open_qty < 0:
        return ["autotrade blocked: broker position qty is invalid"]
    target_account = str(position.get("accountId") or position.get("account") or "").strip()
    if not target_account:
        configured_scope = [value.strip() for value in str(
            cfg.get("CROSSTRADE_ACCOUNTS") or cfg.get("CROSSTRADE_ACCOUNT") or ""
        ).replace("\n", ",").split(",") if value.strip()]
        if len(configured_scope) == 1:
            target_account = configured_scope[0]

    if broker_order_query is None:
        import broker_status
        broker_order_query = broker_status.query_orders
    frozen_for_query = _frozen_plan(records, symbol, account=target_account or None)
    known_order_ids = [row.get("orderId") for row in
                       ((frozen_for_query or {}).get("routeSnapshot") or [])
                       if isinstance(row, dict) and row.get("orderId")]
    try:
        order: Optional[Dict[str, Any]] = _query_orders_with_ids(
            broker_order_query, symbol, known_order_ids)
    except Exception as exc:  # noqa: BLE001
        return [f"autotrade blocked: broker order query failed ({type(exc).__name__}: {exc})"]
    if not isinstance(order, dict) or order.get("verified") is not True:
        return ["autotrade blocked: broker order is UNVERIFIED"]
    order_state = str((order or {}).get("state") or "NONE").upper()
    blocking_order = order_state in set(execution_contract.CONTRACT["blockingOrderStates"])
    # R52: 一度の qty=0 観測を FLAT と確定しない。2026-09-05 05:24:41、建玉 14 枚が
    # 生きている(fills に決済なし・所有ブラケット 4 本 WORKING)のに position 照会が
    # 一瞬 qty=0 を返し、POSITION_GENERATION FLAT を書いて startup recovery(失敗)へ
    # 落ち、管理が外れた。直近世代が open のときだけ: (1) 建玉を一度だけ再照会し、
    # 建玉が見えればそれを採る、(2) なお 0 でも所有ブラケットが active なら矛盾する
    # 観測として今サイクルを止める(FLAT を記録しない)。どちらでもなければ従来どおり。
    if open_qty == 0 and _latest_generation_open(records, target_account):
        try:
            recheck = query(symbol)
        except Exception:  # noqa: BLE001 - 再照会の失敗は裏付け無しとして元の観測へ戻る
            recheck = None
        if isinstance(recheck, dict) and recheck.get("verified") is True:
            try:
                recheck_qty = int(recheck.get("qty") or 0)
            except (TypeError, ValueError):
                recheck_qty = 0
            if recheck_qty > 0:
                position, open_qty = recheck, recheck_qty
                target_account = (str(position.get("accountId") or position.get("account") or "").strip()
                                  or target_account)
        if open_qty == 0 and _owned_brackets_active(order, frozen_for_query):
            return ["autotrade blocked: broker position reads FLAT while owned brackets are still "
                    "active (transient observation; FLAT not recorded)"]
    # Observe FLAT too: it closes the last generation durably, so a later
    # same-looking open position receives a new monotonic generation.
    current_generation = _observe_position_generation(records, position, ledger_path)
    live = _kill_exit_switches(cfg)[1] if exit_only else live_enabled(cfg)
    execute = runner or _run_order

    # Emergency exit is independent from market/scenario/state publication.
    # order.py --flatten is the account-scoped CrossTrade flatteneverything
    # command: it cancels active orders and flattens the position in one route.
    if kill_requested:
        if broker_order_query is None or not isinstance(order, dict) or order.get("verified") is not True:
            return ["autotrade blocked: broker order is UNVERIFIED"]
        kill_identity = current_generation or _position_generation(position) or (
            f"FLAT:{symbol}:{(order or {}).get('idempotencyKey') or order_state}")
        if open_qty <= 0 and not blocking_order:
            return ["autotrade kill: broker verified FLAT and broker order terminal"]
        flatten_key = f"KILL:{kill_identity}:FLATTEN"
        ok, detail = _execute_action(_command_for_flatten(target_account or None), live, execute)
        if not ok:
            if live:
                _append_ledger({"key": flatten_key, "status": "HALT", "action": "FLATTEN",
                                "reason": detail}, ledger_path)
            return [f"AUTOTRADE HALT: {detail}"]
        if not live:
            return [f"autotrade proposal FLATTEN: {detail}"]
        verified, verify_detail = _verify_after_action(query, symbol, "FLATTEN")
        if not verified:
            _append_ledger({"key": flatten_key, "status": "HALT", "action": "FLATTEN",
                            "reason": verify_detail}, ledger_path)
            return [f"AUTOTRADE HALT: {verify_detail}"]
        try:
            checked_order = broker_order_query(symbol)
        except Exception as exc:  # noqa: BLE001
            checked_order = {"verified": False, "detail": f"{type(exc).__name__}: {exc}"}
        checked_state = str((checked_order or {}).get("state") or "UNKNOWN").upper()
        if (not isinstance(checked_order, dict) or checked_order.get("verified") is not True
                or checked_state in set(execution_contract.CONTRACT["blockingOrderStates"])):
            _append_ledger({"key": flatten_key, "status": "HALT", "action": "FLATTEN",
                            "reason": f"post-flatten broker order unverified/blocking ({checked_state})"}, ledger_path)
            return [f"AUTOTRADE HALT: post-flatten broker order unverified/blocking ({checked_state})"]
        _append_ledger({"key": flatten_key, "status": "FLATTEN_SENT", "action": "FLATTEN",
                        "reason": "NQX_AUTOTRADE_KILL", "result": detail}, ledger_path)
        return ["autotrade flatten sent: NQX_AUTOTRADE_KILL"]
    if exit_only:
        # 撤退判定の後で KILL が降りた(武装台帳の読み直し)周期。halt 中なので管理へは落とさない。
        return ["autotrade MANUAL HALT: KILL withdrawn mid-cycle; position management stays stopped"]

    def _continue_management(note: str) -> List[str]:
        """R78: 所有権を束縛した周期は、台帳を読み直して **同じ周期で** 管理まで進める。

        以前は「management armed next cycle」で返していたため、TP1 約定 → 束縛 → 次周期で
        建値移動、と 3 分ループでは最大 6 分かかっていた(2026-09-11 01:38 束縛 → 01:40:48
        送信。その間に価格が戻って stop が拒否された)。再帰は 1 段だけ(chain_management=False)。
        """
        if not chain_management:
            return [note + "; management armed next cycle"]
        return [note + "; managing now"] + _reconcile_one(
            bundle, state_ok, cfg, query, runner, ledger_path, now,
            broker_order_query, state_query, claim_entry, claim_management,
            recover_entry, recover_management, broker_fills_query=broker_fills_query,
            fresh_price_query=fresh_price_query, chain_management=False)

    # A verified open position is managed from the frozen ledger plan even if
    # the proposal expired, its cycle changed, or state publication failed.
    if open_qty > 0:
        management_journal_row = None
        seen_management_keys = set()
        for row in reversed(records):
            record_key = str(row.get("key") or "")
            if record_key in seen_management_keys:
                continue
            seen_management_keys.add(record_key)
            if (row.get("status") in {"MANAGEMENT_CLAIMED", "HALT"}
                    and isinstance(row.get("managementClaimJournal"), dict)):
                management_journal_row = row
                break
        if management_journal_row is not None:
            try:
                management_view = state_query() if state_query is not None else None
                if management_view is None:
                    import nqx_state
                    management_view = nqx_state.fetch_state_quiet()
            except Exception as exc:  # noqa: BLE001
                return [f"autotrade hold: MANAGEMENT recovery state unavailable ({exc})"]
            current_claim = (management_view.get("managementClaim")
                             if isinstance(management_view, dict) else None)
            journal = management_journal_row["managementClaimJournal"]
            # R52: claim が指す建玉世代が今の建玉と違えば、それは **閉じた建玉の記録**で
            # あってロックではない。回復証明(nqx_state / DO とも)は「その世代が同枚数で
            # 生きていて凍結 ID が終端」を要求するので、閉じた世代に対しては原理的に
            # 通らず、毎サイクル `MANAGEMENT startup recovery failed` で新しい建玉の管理が
            # 永久に hold になった(2026-09-05 04:59 実測: 02:49 の MODIFY claim が
            # RESOLVED/UNKNOWN のまま残り、04:56 の新規建玉を管理できなかった)。DO 側も
            # 同じ managementKey の再 claim しか拒まないので、別世代の新規 claim は通る。
            claim_intent = (current_claim.get("managementIntent")
                            if isinstance(current_claim, dict)
                            and isinstance(current_claim.get("managementIntent"), dict) else {})
            claim_generation = str(claim_intent.get("positionGeneration") or "")
            stale_generation = bool(current_generation and claim_generation
                                    and claim_generation != current_generation)
            locking = (isinstance(current_claim, dict)
                       and not stale_generation
                       and current_claim.get("managementKey") == journal.get("managementKey")
                       and current_claim.get("managementIntentHash") == journal.get("managementIntentHash")
                       and (current_claim.get("state") == "CONSUMED"
                            or (current_claim.get("state") == "RESOLVED"
                                and current_claim.get("routeState") == "UNKNOWN")))
            if locking:
                recovery = recover_management or _recover_management_from_current_broker
                recovered, detail = recovery(
                    current_claim, journal, query, broker_order_query, symbol)
                if not recovered:
                    return [f"autotrade hold: MANAGEMENT startup recovery failed ({detail})"]
                _append_ledger({"key": management_journal_row.get("key"),
                                "status": "MANAGEMENT_RECOVERED",
                                "action": "MANAGEMENT_RECOVERY",
                                "managementClaimJournal": journal,
                                "result": detail}, ledger_path)
                return ["autotrade management recovery completed; no duplicate route sent"]
        # ---- R84: 追撃の配線 ----
        # 判定・合成・位相は tranche.py / pyramid.py の純関数が持ち、ここは材料を
        # 束ねて渡すだけにする。管理と撤退は常に追撃より優先で、追撃の評価は
        # 「この周期に打つ手が無い」と分かってからしか走らない(§3.1-1)。
        pyramid_ctx: Dict[str, Any] = {
            "records": records, "cfg": cfg, "symbol": symbol,
            "account": target_account or "", "ledger_path": ledger_path,
            "live": live, "execute": execute, "query": query,
            "order_query": broker_order_query, "state_query": state_query,
            "claim_entry": claim_entry, "claim_management": claim_management,
            "bundle": bundle, "now": now, "proposal": proposal,
            "generation": current_generation,
            "rawIdentity": _position_generation(position),
            "fresh_price_query": fresh_price_query,
            "continue_management": _continue_management,
        }
        wal, pyramid_notes = _pyramid_guard(
            "wal-scan",
            lambda: (_pyramid_wal(records, symbol, target_account)
                     if pyramid.enabled() else None),
            None)
        if wal is not None:
            settled, guard_notes = _pyramid_guard(
                "settle", lambda: _pyramid_settle(pyramid_ctx, wal, position, order),
                ([], True))
            settle_notes, resume = settled
            pyramid_notes = pyramid_notes + list(settle_notes) + guard_notes
            records = pyramid_ctx.get("records") or records
            current_generation = pyramid_ctx.get("generation") or current_generation
            if not resume:
                return pyramid_notes

        plan_record = _frozen_plan_record(records, symbol, account=target_account or None)
        plan = plan_record.get("plan") if plan_record else None
        if not isinstance(plan, dict):
            return pyramid_notes + ["autotrade hold: open position has no frozen management plan"]
        if not current_generation:
            return pyramid_notes + ["autotrade hold: broker position strong identity is unavailable"]
        if str(plan.get("planKind") or "") == tranche.PLAN_KIND:
            # 合成プランは `ownership_binder.bind()` を通らない(§4.1)。単一プランの
            # 実戦検証済み経路を追撃実装の巻き添えにしない。
            pyramid_ctx["records"] = records
            pyramid_ctx["generation"] = current_generation
            managed, guard_notes = _pyramid_guard(
                "composite-management",
                lambda: _composite_management(pyramid_ctx, plan_record, position, order),
                None)
            if managed is None:
                return pyramid_notes + guard_notes + [
                    "autotrade hold: composite management unavailable this cycle"]
            return pyramid_notes + managed + guard_notes
        full_plan = plan.get("fullPlan") if isinstance(plan.get("fullPlan"), dict) else plan
        if isinstance(plan.get("positionOwnership"), dict):
            full_plan = {**full_plan, "positionOwnership": plan["positionOwnership"]}
        route_snapshot = plan.get("routeSnapshot") if isinstance(plan.get("routeSnapshot"), list) else []
        binding = ownership_binder.bind(
            full_plan, route_snapshot, position, order,
            route_state=str((plan_record or {}).get("routeState") or "SENT"),
            position_generation=current_generation)
        rebind_note = None
        frozen_scope = {str(value) for value in full_plan.get("accountScope") or [] if str(value)}
        identity_missing = not any(
            isinstance(row, dict) and str(row.get("state") or "").upper() == "ACCEPTED"
            and str(row.get("accountId") or "") == target_account for row in route_snapshot)
        if (not binding.get("owned") and identity_missing and target_account
                and target_account in frozen_scope):
            # R76: 送信直後に identity を束縛できなかった経路(HALT `live send failed/unknown`
            # または routeState=UNKNOWN)。ブローカーのブラケット構造(送信窓の中に作られた
            # live な OCO 対・同方向の親行なし・枚数の整合)から凍結し直し、所有の判定は
            # 従来どおり binder に委ねる。曖昧なら何も書かず hold のまま。
            rebound_snapshot, rebind_detail, rebound_order = _rebind_unknown_route(
                records, full_plan, position, order, symbol=symbol, account=target_account,
                order_query=broker_order_query, fills_query=broker_fills_query)
            if rebound_snapshot:
                accepted_count = sum(1 for row in rebound_snapshot if row.get("state") == "ACCEPTED")
                rebound_state = "SENT" if accepted_count == len(rebound_snapshot) else "PARTIAL"
                rebound = ownership_binder.bind(
                    full_plan, rebound_snapshot, position, rebound_order,
                    route_state=rebound_state, position_generation=current_generation)
                if rebound.get("owned") and rebound.get("state") in {
                        "PARTIAL_FILL", "LIMITED_PARTIAL", "OWNED_FULL"}:
                    owned_plan = rebound["ownedPlan"]
                    status = ("ENTRY_SENT" if rebound.get("state") == "OWNED_FULL"
                              else "ENTRY_PARTIAL_FILL")
                    rebound_key = _plan_entry_key(full_plan)
                    _append_ledger({"key": rebound_key, "entryKey": rebound_key, "status": status,
                                    "action": "ENTRY_OWNERSHIP_BOUND", "plan": owned_plan,
                                    "routeState": rebound_state, "routeSnapshot": rebound_snapshot,
                                    "identitySource": route_identity.IDENTITY_SOURCE_BRACKETS,
                                    "haltResolution": HALT_RESOLUTION_REBOUND,
                                    "reason": rebind_detail, "brokerOrder": rebound_order},
                                   ledger_path)
                    return _continue_management(
                        "autotrade rebound UNKNOWN route from broker bracket structure "
                        f"({rebound_state}: {rebind_detail})")
                rebind_note = f"rebind: {rebound.get('reason')}"
            else:
                rebind_note = f"rebind: {rebind_detail}"
        if not binding.get("owned") or binding.get("state") not in {
                "PARTIAL_FILL", "LIMITED_PARTIAL", "OWNED_FULL"}:
            reason = str(binding.get("reason"))
            if rebind_note:
                reason = f"{reason}; {rebind_note}"
            return [f"autotrade hold: broker position ownership UNKNOWN ({reason})"]
        rebound = binding.get("ownedPlan")
        if not isinstance(rebound, dict):
            return ["autotrade hold: ownership binder produced no management plan"]
        previous_ownership = plan.get("positionOwnership")
        plan = rebound
        previous_generation = (str(previous_ownership.get("generation") or "")
                               if isinstance(previous_ownership, dict) else "")
        needs_ownership_persist = (previous_generation != current_generation
                                   or (binding.get("state") == "OWNED_FULL"
                                       and bool((plan_record or {}).get("plan", {}).get("partialLegId"))))
        if needs_ownership_persist:
            rebound_status = "ENTRY_SENT" if binding.get("state") == "OWNED_FULL" else "ENTRY_PARTIAL_FILL"
            _append_ledger({"key": _plan_entry_key(full_plan), "entryKey": _plan_entry_key(full_plan),
                            "status": rebound_status, "action": "ENTRY_OWNERSHIP_BOUND",
                            "plan": plan, "routeState": (plan_record or {}).get("routeState")}, ledger_path)
            return _continue_management("autotrade adopted exact broker route/position ownership")
        ownership = plan.get("positionOwnership")
        if not isinstance(ownership, dict) or str(ownership.get("generation")) != current_generation:
            return ["autotrade hold: broker position is not owned by the frozen route"]
        plan_key = _plan_entry_key(plan) or f"LEGACY:{plan.get('scenarioId') or plan.get('decisionId') or 'UNKNOWN'}"
        state: Dict[str, Any] = {}
        for record in records:
            if (record.get("status") == "MANAGEMENT_SENT"
                    and str(record.get("planEntryKey") or _plan_entry_key(record.get("plan"))) == plan_key):
                action_record = record.get("action") if isinstance(record.get("action"), dict) else {}
                state["stop"] = action_record.get("sl")
        # R78: 管理判断は周期開始時の価格ではなく、取れるなら送信直前の現在値で行う。
        # 3 分前の価格で「守れる側」と判定した stop が、送信時には価格に跨がれていた
        # (2026-09-11 01:40:48)。quote が取れない周期は従来どおり周期の価格。
        fresh_price = _fresh_price(
            fresh_price_query if fresh_price_query is not None else _FRESH_PRICE_QUERY, symbol)
        management_bundle = bundle
        if fresh_price is not None:
            management_bundle = {**bundle, "price": fresh_price, "priceSource": "FRESH_QUOTE"}
        reference_price = fresh_price if fresh_price is not None else _price_from_bundle(bundle)
        # R102: 裸の建玉(保護注文の OCO 組が 0)は、管理判断の前に同じ周期で直す。
        naked_stop, naked_notes = _guard_naked_position(
            plan=plan, position=position, order=order, records=records, plan_key=plan_key,
            state=state, price=reference_price, noise=_noise_floor(bundle), cfg=cfg, symbol=symbol,
            account=target_account or None, open_qty=open_qty, query=query,
            broker_order_query=broker_order_query, claimer=claim_management, execute=execute,
            ledger_path=ledger_path, live=live, now=now)
        pyramid_notes = pyramid_notes + list(naked_notes)
        if naked_stop:
            return pyramid_notes
        action = management_action(plan, position, management_bundle, state, cfg, now)
        if not action:
            pyramid_ctx["records"] = records
            pyramid_ctx["generation"] = current_generation
            pyramid_ctx["livePrice"] = fresh_price
            considered, guard_notes = _pyramid_guard(
                "consider",
                lambda: _pyramid_consider(pyramid_ctx, plan, position, order,
                                          binding_state=str(binding.get("state") or "")),
                [])
            override_notes = ([f"management override: {json.dumps(plan.get('managementOverride'), sort_keys=True)}"]
                              if isinstance(plan.get("managementOverride"), dict) else [])
            return pyramid_notes + [
                f"autotrade hold: managed {plan.get('scenarioId') or plan.get('decisionId') or plan_key}"
            ] + override_notes + list(considered) + guard_notes
        if action["action"] == "HALT":
            return [f"AUTOTRADE HALT: {action['reason']}"]
        key = (f"{plan_key}:{target_account or 'ACCOUNT_UNKNOWN'}:{action['action']}:"
               f"{action.get('sl', '')}:{action.get('qty', open_qty)}")
        if _latest(records, key, {"MANAGEMENT_SENT", "FLATTEN_SENT"}):
            return [f"autotrade idempotent skip: {key}"]
        claim = None
        if live and action["action"] == "MODIFY":
            raw_generation = _position_generation(position)
            account = position.get("accountId") or position.get("account")
            try:
                intent = management_intent.build(
                    account_id=account, symbol=plan["symbol"],
                    position_generation=raw_generation, side=plan["side"],
                    qty=action["qty"], stop=action["sl"], target=action["tp"])
            except ValueError as exc:
                return [f"AUTOTRADE HALT: management intent invalid ({exc})"]
            claimer = claim_management
            if claimer is None:
                import nqx_state
                claimer = nqx_state.claim_management
            try:
                claim_ok, claim = claimer(intent)
            except Exception as exc:  # noqa: BLE001
                claim_ok, claim = False, {"reason": f"{type(exc).__name__}: {exc}"}
            if (not claim_ok or not isinstance(claim, dict)
                    or claim.get("managementKey") != management_intent.management_key(intent)
                    or claim.get("managementIntentHash") != management_intent.intent_hash(intent)
                    or not claim.get("claimToken")):
                return [f"AUTOTRADE HALT: MANAGEMENT claim unavailable ({claim})"]
            management_claim_journal = {
                "managementKey": claim["managementKey"],
                "claimToken": str(claim["claimToken"]),
                "managementIntentHash": str(claim["managementIntentHash"]),
                "managementIntent": intent,
            }
            try:
                _append_ledger({"key": key, "status": "MANAGEMENT_CLAIMED",
                                "action": action, "planEntryKey": plan_key,
                                "plan": plan,
                                "managementClaimJournal": management_claim_journal}, ledger_path)
            except OSError as exc:
                return [f"AUTOTRADE HALT: MANAGEMENT durable journal failed ({exc})"]
        args = (_command_for_modify(plan, action, _position_generation(position), claim,
                                    account=target_account or None, last=reference_price)
                if action["action"] == "MODIFY" else _command_for_flatten(target_account or None))
        ok, detail = _execute_action(args, live, execute)
        if not ok:
            if action["action"] == "MODIFY" and "MODIFY_STOP_NOT_PROTECTIVE" in detail:
                # R78: order.py が送信直前の現在値で「この stop は守れない側」と判定した。
                # 何も取消していない(既存ブラケットは無傷)ので HALT ではなく見送り。
                # 次周期は新しい現在値で判断し直す。
                return ["autotrade hold: modify skipped, stop not protective at send time "
                        f"(bracket untouched): {detail[-300:]}"]
            if live and action["action"] == "MODIFY":
                handled, repair_notes, halt_prefix = _repair_unprotected_runner(
                    plan=plan, position=position, action=action, failure=detail,
                    failed_key=key, plan_key=plan_key,
                    failed_journal=management_claim_journal,
                    previous_stop=(_num(state.get("stop")) if _num(state.get("stop")) is not None
                                   else plan["initialStop"]),
                    price=reference_price, cfg=cfg, symbol=symbol,
                    account=target_account or None, open_qty=open_qty, query=query,
                    broker_order_query=broker_order_query, claimer=claim_management,
                    execute=execute, ledger_path=ledger_path)
                if handled:
                    return repair_notes
                detail = halt_prefix + detail
            if live:
                _append_ledger({"key": key, "status": "HALT", "action": action,
                                "planEntryKey": plan_key, "plan": plan, "reason": detail,
                                "managementClaimJournal": management_claim_journal
                                if action["action"] == "MODIFY" else None}, ledger_path)
            return [f"AUTOTRADE HALT: {detail}"]
        if not live:
            return [f"autotrade proposal {action['action']}: {detail}"]
        verified, verify_detail = _verify_after_action(query, plan["symbol"], action["action"], plan.get("side"))
        if not verified:
            _append_ledger({"key": key, "status": "HALT", "action": action,
                            "planEntryKey": plan_key, "plan": plan, "reason": verify_detail,
                            "managementClaimJournal": management_claim_journal
                            if action["action"] == "MODIFY" else None}, ledger_path)
            return [f"AUTOTRADE HALT: {verify_detail}"]
        status = "MANAGEMENT_SENT" if action["action"] == "MODIFY" else "FLATTEN_SENT"
        _append_ledger({"key": key, "status": status, "action": action,
                        "planEntryKey": plan_key, "plan": plan, "result": detail,
                        "managementClaimJournal": management_claim_journal
                        if action["action"] == "MODIFY" else None}, ledger_path)
        return [f"autotrade {status.lower()}: {action['reason']}"]

    # ---- R52: 価格が建値より先に TP1 へ届いた未約定エントリーの自動取消 ----
    # 取消は既存の撤退経路(--flatten --account: 口座の未約定注文を全て消す)で、
    # 送信後に FLAT + 注文非 blocking を再照会してから ENTRY_HALTED を記録し、
    # **同じサイクル**で下の startup recovery(不在証明 → RECOVER → ENTRY_RECOVERED)
    # へ進んで HALT と claim を解く。KILL 中は KILL 経路が先に処理している。
    # 停止は `NQX_STALE_ENTRY_CANCEL=0`。
    if (open_qty <= 0 and blocking_order and not kill_requested
            and _truthy(_setting("NQX_STALE_ENTRY_CANCEL", cfg, "1"))):
        stale = _stale_entry_to_cancel(records, symbol, target_account or None, bundle, order)
        if stale is not None:
            stale_record, stale_reason = stale
            stale_plan = stale_record.get("plan") or {}
            stale_key = _plan_entry_key(stale_plan) or str(stale_record.get("key") or "")
            ok, detail = _execute_action(_command_for_flatten(target_account or None), live, execute)
            if not ok:
                if live:
                    _append_ledger({"key": stale_key, "entryKey": stale_key, "status": "HALT",
                                    "action": "ENTRY_STALE_CANCEL", "reason": detail,
                                    "plan": stale_plan}, ledger_path)
                return [f"AUTOTRADE HALT: stale entry cancel failed ({stale_reason}): {detail}"]
            if not live:
                return [f"autotrade proposal ENTRY_STALE_CANCEL ({stale_reason}): {detail}"]
            verified, verify_detail = _verify_after_action(query, symbol, "FLATTEN")
            if verified:
                try:
                    checked_order = broker_order_query(symbol)
                except Exception as exc:  # noqa: BLE001
                    checked_order = {"verified": False, "detail": f"{type(exc).__name__}: {exc}"}
                checked_state = str((checked_order or {}).get("state") or "UNKNOWN").upper()
                if (not isinstance(checked_order, dict) or checked_order.get("verified") is not True
                        or checked_state in set(execution_contract.CONTRACT["blockingOrderStates"])):
                    verified = False
                    verify_detail = f"post-cancel broker order unverified/blocking ({checked_state})"
            if not verified:
                _append_ledger({"key": stale_key, "entryKey": stale_key, "status": "HALT",
                                "action": "ENTRY_STALE_CANCEL", "reason": verify_detail,
                                "plan": stale_plan}, ledger_path)
                return [f"AUTOTRADE HALT: {verify_detail}"]
            _append_ledger({"key": stale_key, "entryKey": stale_key, "status": "ENTRY_HALTED",
                            "action": "ENTRY_STALE_CANCEL", "plan": stale_plan,
                            "reason": stale_reason, "result": detail}, ledger_path)
            # 取り消せた。同じサイクルで startup recovery → 新規判定まで進める
            # (ENTRY_HALTED が記録済みなので、この分岐には二度と入らない)。
            # 2026-09-05 01:49 実測: 取消直後は per-order 詳細がまだ CANCELED を返さず、
            # 回復が PROOF_INVALID で 1 サイクル遅れた。ブローカー側の反映を短く待つ。
            try:
                settle = float(_setting("NQX_STALE_CANCEL_SETTLE_SEC", cfg, "3"))
            except (TypeError, ValueError):
                settle = 3.0
            if settle > 0:
                time.sleep(settle)
            return [f"autotrade stale entry canceled: {stale_reason}"] + _reconcile_one(
                bundle, state_ok, cfg, broker_query, runner, ledger_path, now,
                broker_order_query, state_query, claim_entry, claim_management,
                recover_entry, recover_management)

    # FLAT startup recovery is a production path, not merely a helper.  The
    # authoritative claim journal and durable local token must both match;
    # recovery re-queries broker truth and commits by snapshot CAS.
    view = None
    journal_candidates = [row for row in records if isinstance(row.get("claimJournal"), dict)]
    if journal_candidates:
        try:
            view = state_query() if state_query is not None else None
            if view is None:
                import nqx_state
                view = nqx_state.fetch_state_quiet()
        except Exception as exc:  # noqa: BLE001
            return [f"autotrade blocked: authoritative state lookup failed ({type(exc).__name__}: {exc})"]
    claim_view = view.get("entryClaim") if isinstance(view, dict) else None
    # R41: `CLAIMED` も回復対象に含める。
    #
    # CONSUME へ到達せずに落ちた送信は claim を `CLAIMED` のまま置き去りにする
    # (routeState は None、totalAttempts は 0 —— **一度も送っていない**)。
    # ここを CONSUMED だけに限っていたため回復経路が一度も動かず、同じキーの
    # `ENTRY_RECOVERED` が永久に書かれず、`_has_halt` が古い HALT を返し続けて
    # **以後すべての新規が止まった**(2026-08-25 22:00 の HALT が 22:11 / 22:18 で
    # そのまま再表示され、送信は一度も試行されなかった)。
    #
    # 送っていない claim を解くのは緩和ではない: 回復は従来どおりブローカーへ
    # 照会し、空だと証明できたときだけ通る。むしろ「送ったかもしれない」
    # CONSUMED より **安全側**の材料しかない状態である。
    #
    # R52(2026-09-04): **RECOVERED はロックではない。** DO は RECOVER 受理時に
    # entryRelease と tombstone を記録して claim を終端しており、CLAIM 側の
    # ENTRY_CLAIM_ACTIVE = {CLAIMED, CONSUMED} にも入っていない。routeState=UNKNOWN が
    # 残るのは送信当時に経路 identity を束縛できなかった「記録」であって現在の状態
    # ではない。ここを routeState だけで見ていたため、口座入替後(観測口座が claim の
    # accountScope 外)に毎サイクル回復を試み、DO の 409
    # 「broker observation cannot overwrite another scope/intent」で新規が全部止まった
    # (23:36 実測)。RESOLVED/CONSUMED + UNKNOWN の回復経路は従来どおり残す。
    claim_state = str(claim_view.get("state") or "") if isinstance(claim_view, dict) else ""
    locking_claim = (isinstance(claim_view, dict)
                     and claim_state != "RECOVERED"
                     and (claim_state in {"CLAIMED", "CONSUMED"}
                          or str(claim_view.get("routeState") or "") in {"SENT", "PARTIAL", "UNKNOWN"}))
    if locking_claim:
        journal_record = next((row for row in reversed(records)
                               if isinstance(row.get("claimJournal"), dict)
                               and row["claimJournal"].get("entryKey") == claim_view.get("entryKey")), None)
        if journal_record is None:
            return ["autotrade blocked: authoritative ENTRY lock has no durable local claim journal"]
        journal = journal_record["claimJournal"]
        recovery = recover_entry or _recover_entry_from_current_broker
        # R102c(2026-09-15 16:31): 不在証明は **claim が置かれた限月**で観測する。ロール直後に今の
        # 限月(MNQZ6)で観測すると Worker の entryRecoveryProof(observation.symbol == intent.symbol)が
        # 通らず、旧限月の claim が毎周期 ENTRY_CLAIM_RECOVERY_UNVERIFIED で新規を全部止めた。
        # MANAGEMENT の回復(recover_management_from_broker)は元から intent の銘柄で観測している。
        recovered, detail = recovery(claim_view, journal, query, broker_order_query,
                                     _claim_recovery_symbol(claim_view, symbol))
        if not recovered:
            # R40: identity を一度も束縛できなかった claim は、DO 側の
            # `entryRecoveryProof` も ACCEPTED 行を必須にするため RECOVER では
            # 絶対に通らない。しかし DO には R36 の stale release
            # (`staleEntryClaimReleasable`) があり、CLAIM 到達時に
            # 「claim が staleReleaseSec 超 + 建玉0 + 未終端注文ゼロ」を
            # **DO 自身が再検証して**解放する経路が既にある。
            #
            # ここで一律 blocked を返すと CLAIM へ到達できず、その正規の解放
            # 経路が永久に動かない(2026-08-25 に全周期が停止)。
            # identity 不明かつ RECOVER が上の不在証明を通った場合に限り、
            # 判定を DO へ委ねて先へ進む。エンジンは何も自己申告しない。
            frozen_rows = (claim_view.get("routeSnapshot")
                           if isinstance(claim_view.get("routeSnapshot"), list) else [])
            identity_bound = any(isinstance(row, dict) and row.get("state") == "ACCEPTED"
                                 for row in frozen_rows)
            # R53: identity は束縛できていたが、その注文IDをブローカーが解決できなく
            # なった場合(古い注文が口座から消える)も ID ごとの終端証明は永久に作れない。
            # 上と同じ理由で判定を DO へ委ねる。DO は不在の証明(建玉0・未終端注文
            # ゼロ・口座/銘柄一致・claim の年齢)を自分で再検証するので、エンジンが
            # 解放を宣言することにはならない。
            deferrable = (isinstance(detail, dict)
                          and str(detail.get("reason") or "") == "ENTRY_CLAIM_RECOVERY_UNVERIFIED"
                          and (not identity_bound
                               or bool(detail.get("identityUnresolvable"))))
            if not deferrable:
                return [f"autotrade blocked: ENTRY startup recovery failed ({detail})"]
        recovered_row = {"key": claim_view.get("entryKey"), "entryKey": claim_view.get("entryKey"),
                         "status": "ENTRY_RECOVERED", "action": "ENTRY_RECOVERY",
                         "claimJournal": journal, "result": detail}
        _append_ledger(recovered_row, ledger_path)
        records = [*records, recovered_row]
        halt = _has_halt(records)

    # Historical HALT records prevent only a fresh flat ENTRY.  Broker-verified
    # kill and owned-position management above must never be shadowed by them.
    if halt:
        return [f"AUTOTRADE HALT: {halt.get('reason', 'previous action failed')}"]

    accepted_record = _frozen_plan_record(records, symbol, account=target_account or None)
    if accepted_record and accepted_record.get("status") in {
            "ENTRY_ACCEPTED", "ENTRY_RESTING", "ENTRY_PARTIAL_ROUTE"}:
        accepted_plan = accepted_record["plan"]
        # R52: 集約 state だけでなく、**自分の注文 ID が全部終端**して建玉が FLAT なら
        # 終端する。FILLED → ブラケットで決済 → FLAT を engine が観測できなかった場合
        # (2026-09-05、ユーザーが engine の注文を手で数量変更して手動トレードにした)、
        # 集約 state は FILLED で上の集合に無く、「awaiting verified fill (FILLED)」で
        # 永久に止まる。生きている脚が 1 本でもあれば従来どおり待つ。
        cancel_states = {"CANCELED", "CANCELLED", "REJECTED", "EXPIRED"}
        frozen_ids = {str(row.get("orderId")) for row in (accepted_plan.get("routeSnapshot") or [])
                      if isinstance(row, dict) and str(row.get("state") or "").upper() == "ACCEPTED"
                      and row.get("orderId")}
        terminal_states = set(execution_contract.CONTRACT["brokerObservation"]["terminalStates"])
        observed_status = {str(row.get("orderId")): str(row.get("status") or "").upper()
                           for row in ((order or {}).get("orders") or []) if isinstance(row, dict)}
        all_terminal = bool(frozen_ids) and all(
            observed_status.get(order_id) in terminal_states for order_id in frozen_ids)
        if order_state in cancel_states or (open_qty <= 0 and all_terminal):
            detail = (order_state if order_state in cancel_states
                      else "all accepted legs terminal while FLAT: " + ", ".join(
                          f"{order_id}={observed_status.get(order_id)}" for order_id in sorted(frozen_ids)))
            _append_ledger({"key": _plan_entry_key(accepted_plan), "entryKey": _plan_entry_key(accepted_plan),
                            "status": "ENTRY_HALTED", "action": "ENTRY_TERMINAL",
                            "plan": accepted_plan, "reason": f"broker order {detail}"}, ledger_path)
            return [f"autotrade pending entry terminalized: {detail}"]
        if accepted_record.get("status") == "ENTRY_RESTING":
            # R103-1(SHADOW): SL が今のノイズに対して短くなっていれば台帳に 1 行だけ残す。
            verdict = _resting_stop_recheck(accepted_plan, bundle)
            if isinstance(verdict, dict) and verdict.get("stale"):
                _note_resting_stop_stale(records, accepted_plan, verdict, ledger_path)
                # R103-3(LIVE): 指値を取り消す。R52 の ENTRY_STALE_CANCEL と同じ経路(flatten →
                # 再照会で FLAT + 注文非 blocking → ENTRY_HALTED → 同じ周期で startup recovery)。
                # 身元検査(親行が全部自分の注文 ID)を通らなければ触らない。KILL 中は KILL が先。
                if (str(verdict.get("mode") or "") == "LIVE" and not kill_requested
                        and open_qty <= 0
                        and _truthy(_setting("NQX_RESTING_STOP_CANCEL", cfg, "1"))):
                    ineligible = _resting_cancel_eligible(accepted_plan, order)
                    stale_key = _plan_entry_key(accepted_plan) or str(accepted_record.get("key") or "")
                    stale_reason = (f"resting stop stale ({verdict.get('reason')}: SL {verdict.get('distPt')}pt"
                                    f" < {verdict.get('minPt')}pt = minN x noise {verdict.get('noiseSessionOpen')})")
                    if ineligible:
                        return [f"autotrade hold: {stale_reason}; cancel skipped ({ineligible})"]
                    ok, detail = _execute_action(_command_for_flatten(target_account or None), live, execute)
                    if not ok:
                        if live:
                            _append_ledger({"key": stale_key, "entryKey": stale_key, "status": "HALT",
                                            "action": RESTING_STOP_CANCEL, "reason": detail,
                                            "plan": accepted_plan}, ledger_path)
                        return [f"AUTOTRADE HALT: resting stop cancel failed ({stale_reason}): {detail}"]
                    if not live:
                        return [f"autotrade proposal {RESTING_STOP_CANCEL} ({stale_reason}): {detail}"]
                    verified, verify_detail = _verify_after_action(query, symbol, "FLATTEN")
                    if verified:
                        try:
                            checked_order = broker_order_query(symbol)
                        except Exception as exc:  # noqa: BLE001
                            checked_order = {"verified": False, "detail": f"{type(exc).__name__}: {exc}"}
                        checked_state = str((checked_order or {}).get("state") or "UNKNOWN").upper()
                        if (not isinstance(checked_order, dict) or checked_order.get("verified") is not True
                                or checked_state in set(execution_contract.CONTRACT["blockingOrderStates"])):
                            verified = False
                            verify_detail = f"post-cancel broker order unverified/blocking ({checked_state})"
                    if not verified:
                        _append_ledger({"key": stale_key, "entryKey": stale_key, "status": "HALT",
                                        "action": RESTING_STOP_CANCEL, "reason": verify_detail,
                                        "plan": accepted_plan}, ledger_path)
                        return [f"AUTOTRADE HALT: {verify_detail}"]
                    _append_ledger({"key": stale_key, "entryKey": stale_key, "status": "ENTRY_HALTED",
                                    "action": RESTING_STOP_CANCEL, "plan": accepted_plan,
                                    "reason": stale_reason, "result": detail, "recheck": verdict}, ledger_path)
                    try:
                        settle = float(_setting("NQX_STALE_CANCEL_SETTLE_SEC", cfg, "3"))
                    except (TypeError, ValueError):
                        settle = 3.0
                    if settle > 0:
                        time.sleep(settle)
                    return [f"autotrade resting entry canceled: {stale_reason}"] + _reconcile_one(
                        bundle, state_ok, cfg, broker_query, runner, ledger_path, now,
                        broker_order_query, state_query, claim_entry, claim_management,
                        recover_entry, recover_management)
        return [f"autotrade hold: {accepted_record.get('status')} awaiting verified fill ({order_state})"]

    # Only FLAT new ENTRY depends on a published and presently sealed cycle.
    if not state_ok:
        return ["autotrade blocked: frozen state was not published"]
    if view is None:
        try:
            view = state_query() if state_query is not None else None
            if view is None:
                import nqx_state
                view = nqx_state.fetch_state_quiet()
        except Exception as exc:  # noqa: BLE001
            return [f"autotrade blocked: authoritative state lookup failed ({type(exc).__name__}: {exc})"]
    if not isinstance(view, dict):
        return ["autotrade blocked: CYCLE_MISMATCH authoritative state unavailable"]
    if order is None:
        order = view.get("order") or {"state": "NONE", "verified": True}
        if not isinstance(order, dict):
            order = {"state": "UNKNOWN", "verified": False}
    if order.get("verified") is not True:
        return ["autotrade blocked: broker order is UNVERIFIED"]
    order_state = str(order.get("state") or "NONE").upper()
    if order_state in set(execution_contract.CONTRACT["blockingOrderStates"]):
        return [f"autotrade blocked: execution contract ORDER_PENDING ({order_state})"]

    sealed, seal_reason, authoritative = _authoritative_cycle_seal(view, proposal, cfg=cfg, now=now)
    if not sealed or not isinstance(authoritative, dict):
        return [f"autotrade blocked: {seal_reason}"]
    # Do not merge bundle order-control fields into the server scenario.
    scenario = dict(authoritative)
    if isinstance(proposal.get("model"), str):
        scenario["_displayModel"] = proposal["model"][:96]
    entry_key = _entry_key(scenario)
    if not entry_key:
        return ["autotrade blocked: authoritative ENTRY identity is incomplete"]

    # R35: ENTRY_CLAIMED を含める。
    #
    # 台帳へは送信の **前** に ENTRY_CLAIMED を書く。order.py の子プロセスが
    # 30 秒で kill されると(HTTP 予算は最悪 114 秒で構造的に超過する)、
    # webhook には注文が届いているのに親は「出ていない」と記録する。
    # そのとき台帳には ENTRY_CLAIMED だけが残るが、この集合に無かったため
    # **次サイクルで同じ発注を再試行**していた。CrossTrade のペイロードには
    # 冪等キーが無いので、二重発注に直結する。
    # R84: 追撃で消費したシグナルは、その後 FLAT になっても新規 ENTRY を作らない
    # (PYRAMID_CLAIMED / PYRAMID_SENT を重複防止集合に入れる。§2.2)。
    if _latest(records, entry_key, PYRAMID_ENTRY_GUARD):
        return [f"autotrade idempotent skip: entry {entry_key}"]
    legacy = next((record for record in records if _legacy_entry_matches(record, scenario)), None)
    if legacy:
        _append_ledger({"key": entry_key, "status": "ENTRY_MIGRATED", "action": "ENTRY",
                        "legacyKey": legacy.get("key"), "scenarioId": scenario.get("scenarioId")}, ledger_path)
        return [f"autotrade idempotent skip: legacy entry migrated to {entry_key}"]

    try:
        plan = build_management_plan(scenario, bundle, cfg)
    except ValueError as exc:
        return [f"autotrade blocked: invalid management plan ({exc})"]
    if plan.get("entryKey") != entry_key:
        return ["autotrade blocked: management plan ENTRY identity mismatch"]
    entry_order_type = _entry_order_type(plan, bundle)
    plan = {**plan, "entryOrderType": entry_order_type,
            "entryReference": (_price_from_bundle(bundle)
                               if entry_order_type == "MARKET" else plan["entry"])}
    if entry_order_type == "MARKET":
        # R90 穴 2: 成行の SL 距離ゲート。claim の**前**(見送りは HALT でも claim でもない)。
        guard = _market_stop_guard(plan, bundle)
        if guard.get("block"):
            _note_entry_guard(records, entry_key, scenario, guard, ledger_path)
            return [f"autotrade skip: R90 {guard.get('reason')} — {guard.get('text') or ''}".rstrip(" —")]
        if guard.get("minPt") is not None:
            plan = {**plan, "marketStopMinPt": guard["minPt"],
                    "marketStopGuard": {k: v for k, v in guard.items() if k != "text"}}
    # R102: 限月の門。claim の前(見送りは HALT でも claim でもない)。
    contract_guard = _contract_entry_guard(plan, bundle)
    if contract_guard.get("block"):
        _note_entry_guard(records, entry_key, scenario, contract_guard, ledger_path)
        return [f"autotrade skip: R102 {contract_guard.get('reason')} — "
                f"{contract_guard.get('text') or ''}".rstrip(" —")]
    if live:
        claimer = claim_entry
        if claimer is None:
            import nqx_state
            claimer = nqx_state.claim_entry
        try:
            claim_ok, claim = claimer(
                scenario,
                release_entry_key=((view.get("entryClaim") or {}).get("entryKey")
                                   if isinstance(view.get("entryClaim"), dict) else None),
                broker_position_verified_flat=True,
                broker_order_verified_terminal=(order.get("verified") is True
                    and order_state not in set(execution_contract.CONTRACT["blockingOrderStates"])),
                order_type=_entry_order_type(plan, bundle),
            )
        except Exception as exc:  # noqa: BLE001
            claim_ok, claim = False, {"reason": f"{type(exc).__name__}: {exc}"}
        if not claim_ok or not isinstance(claim, dict):
            return [f"autotrade blocked: ENTRY claim unavailable ({claim})"]
        if (claim.get("entryKey") != entry_key or not claim.get("claimToken")
                or not claim.get("executionIntentHash")):
            return ["autotrade blocked: ENTRY claim identity invalid"]
        claim_journal = {"entryKey": entry_key, "claimToken": str(claim["claimToken"]),
                         "executionIntentHash": str(claim["executionIntentHash"]),
                         "executionIntent": claim.get("executionIntent")}
        try:
            _append_ledger({"key": entry_key, "entryKey": entry_key,
                            "status": "ENTRY_CLAIMED", "action": "ENTRY_CLAIM",
                            "plan": plan, "claimJournal": claim_journal}, ledger_path)
        except OSError as exc:
            return [f"AUTOTRADE HALT: ENTRY durable journal failed ({exc})"]
        args = _command_for_entry(plan, bundle, entry_key, str(claim["claimToken"]),
                                  str(claim["executionIntentHash"]))
    else:
        args = _command_for_entry(plan, bundle)
    ok, detail = _execute_action(args, live, execute)
    envelope = (route_envelope.parse(detail, expected_accounts=plan.get("accountScope"))
                if live else {"ok": True})
    if live and (not envelope.get("ok") or (ok and envelope.get("state") != "SENT")):
        ok = False
        detail = f"live send failed/unknown:\n{detail}"
    if not ok:
        r102_code = _r102_reject_code(detail) if str(detail).startswith("dry-run rejected:") else None
        if r102_code:
            # R102: order.py の限月/価格の門で落ちた = 何も送っていない。HALT にせず見送り(1 回だけ記録)。
            _note_entry_guard(records, entry_key, scenario,
                              {"block": True, "reason": r102_code, "text": str(detail)[-300:]},
                              ledger_path)
            return [f"autotrade skip: R102 {r102_code} (order.py dry-run) — {str(detail)[-200:]}"]
        if live:
            route_state = _entry_route_state(detail, plan.get("accountScope"))
            if route_state in {"PARTIAL", "UNKNOWN"}:
                route_snapshot = _entry_route_snapshot(detail, plan.get("accountScope"))
                try:
                    observed_after = query(plan["symbol"])
                    orders_after = broker_order_query(plan["symbol"])
                except Exception as exc:  # noqa: BLE001
                    observed_after = {"verified": False, "detail": str(exc)}
                    orders_after = {"verified": False, "detail": str(exc)}
                try:
                    partial_qty = int((observed_after or {}).get("qty") or 0)
                except (TypeError, ValueError):
                    partial_qty = -1
                partial_plan = {**plan, "routeSnapshot": route_snapshot}
                status = "ENTRY_PARTIAL_ROUTE"
                generation = (_observe_position_generation(records, observed_after, ledger_path)
                              if partial_qty > 0 else None)
                binding = ownership_binder.bind(
                    plan, route_snapshot, observed_after, orders_after,
                    route_state=route_state, position_generation=generation)
                if binding.get("owned"):
                    partial_plan = binding["ownedPlan"]
                    status = ("ENTRY_RESTING" if binding["state"] == "RESTING"
                              else "ENTRY_PARTIAL_FILL" if binding["state"] in {
                                  "PARTIAL_FILL", "LIMITED_PARTIAL"}
                              else "ENTRY_SENT")
                else:
                    partial_plan["ownershipReason"] = binding.get("reason")
                _append_ledger({"key": entry_key, "entryKey": entry_key, "status": status,
                                "action": "ENTRY", "plan": partial_plan,
                                "routeState": route_state, "result": detail,
                                "routeSnapshot": route_snapshot,
                                "brokerOrder": orders_after,
                                "claimJournal": claim_journal}, ledger_path)
                return [f"AUTOTRADE HALT: {status} frozen; broker management/kill remains active"]
            _append_ledger({"key": entry_key, "entryKey": entry_key, "status": "HALT",
                            "action": "ENTRY", "plan": plan, "reason": detail}, ledger_path)
            return [f"AUTOTRADE HALT: {detail}"]
        return [f"autotrade dry-run rejected: {detail}"]
    if not live:
        return [f"autotrade proposal ENTRY {plan['model']} {plan['side']} {plan['entry']}->{plan['finalTarget']}"]
    route_snapshot = envelope.get("snapshot") or []
    try:
        opened_position = query(plan["symbol"])
    except Exception as exc:  # noqa: BLE001
        opened_position = {"verified": False, "detail": f"{type(exc).__name__}: {exc}"}
    try:
        opened_qty = int((opened_position or {}).get("qty") or 0)
    except (TypeError, ValueError):
        opened_qty = -1
    if not isinstance(opened_position, dict) or opened_position.get("verified") is not True:
        verify_detail = "post-send broker position is UNVERIFIED"
        _append_ledger({"key": entry_key, "entryKey": entry_key, "status": "HALT",
                        "action": "ENTRY", "plan": plan, "reason": verify_detail}, ledger_path)
        return [f"AUTOTRADE HALT: {verify_detail}"]
    try:
        post_entry_orders = _query_orders_with_ids(
            broker_order_query, plan["symbol"],
            [row.get("orderId") for row in route_snapshot if row.get("orderId")])
    except Exception as exc:  # noqa: BLE001
        post_entry_orders = {"verified": False, "detail": f"{type(exc).__name__}: {exc}"}
    opened_generation = (_observe_position_generation(records, opened_position, ledger_path)
                         if opened_qty > 0 else None)
    binding = ownership_binder.bind(
        plan, route_snapshot, opened_position, post_entry_orders,
        route_state="SENT", position_generation=opened_generation)
    if not binding.get("owned"):
        unknown_plan = {**plan, "routeSnapshot": route_snapshot,
                        "legIdentityKnown": False, "ownershipReason": binding.get("reason")}
        _append_ledger({"key": entry_key, "entryKey": entry_key,
                        "status": "ENTRY_PARTIAL_FILL" if opened_qty > 0 else "ENTRY_RESTING",
                        "action": "ENTRY", "plan": unknown_plan, "result": detail,
                        "brokerOrder": post_entry_orders, "reason": binding.get("reason"),
                        "claimJournal": claim_journal}, ledger_path)
        return [f"AUTOTRADE HALT: accepted entry ownership UNKNOWN ({binding.get('reason')}); resend blocked"]
    owned_plan = binding["ownedPlan"]
    status = {"RESTING": "ENTRY_RESTING", "PARTIAL_FILL": "ENTRY_PARTIAL_FILL",
              "LIMITED_PARTIAL": "ENTRY_PARTIAL_FILL",
              "OWNED_FULL": "ENTRY_SENT"}[binding["state"]]
    _append_ledger({"key": entry_key, "entryKey": entry_key, "status": status,
                    "routeState": "SENT", "action": "ENTRY", "plan": owned_plan,
                    "result": detail, "brokerOrder": post_entry_orders,
                    "claimJournal": claim_journal}, ledger_path)
    if status == "ENTRY_RESTING":
        return [f"autotrade entry accepted/pending: {plan['side']} {plan['entry']}"]
    if status == "ENTRY_PARTIAL_FILL":
        return ["autotrade entry partial fill frozen: exact filled/resting ownership bound"]
    return [f"autotrade entry sent: {plan['model']} {plan['side']} {plan['entry']}->{plan['finalTarget']}"]


def _multi_scope(cfg: Dict[str, str]) -> List[str]:
    raw = str(cfg.get("CROSSTRADE_ACCOUNTS") or cfg.get("CROSSTRADE_ACCOUNT") or "")
    return list(dict.fromkeys(value.strip() for value in raw.replace("\n", ",").split(",")
                              if value.strip()))


# R46: 手動建玉の証拠になるのはこの2つだけ。
#   1) その symbol/口座に凍結プランが1件も無い
#   2) 凍結プランはあるが、その accountScope にこの口座が入っていない
# どちらも「こちらの経路では作れない建玉」を意味する。identity 不明・照会失敗
# のような *証明できない* 状態はここに入れない。入れると、自分の建玉を放置した
# まま他口座へ新規を送る事故になる。
MANUAL_POSITION_HOLDS = (
    "autotrade hold: open position has no frozen management plan",
    "autotrade hold: broker position ownership UNKNOWN "
    "(broker position account is outside frozen scope)",
)


#: R84: 追撃は「やらなくてよい仕事」なので、その注記が手動建玉の判定を左右しない。
#: 判定は `all(note in MANUAL_POSITION_HOLDS)` なので、注記が 1 行混ざるだけで
#: R46 の除外が外れ、手動で触っている口座のせいで経路全体が止まる。
PYRAMID_NOTE_PREFIX = "pyramid"


def _is_manual_position_account(notes: List[str]) -> bool:
    """1口座分の note が「手動建玉」だけを述べているか。

    1つでも別の理由が混ざっていれば False。手動と断定できない口座は従来どおり
    経路全体を止める側に倒す。追撃(R84)の注記だけは判定材料から外す —— 追撃は
    建玉の所有とは無関係な付随情報で、これを混ぜると R46 の除外が壊れる。
    """
    material = [str(note) for note in notes or []
                if not str(note).startswith(PYRAMID_NOTE_PREFIX)]
    return bool(material) and all(note in MANUAL_POSITION_HOLDS for note in material)


def _narrow_bundle_scope(bundle: Dict[str, Any], accounts: List[str]) -> Dict[str, Any]:
    """公開済みシナリオの accountScope を ``accounts`` へ狭めた複製を返す。

    下流は凍結値を **狭める方向にしか** 動かせない(R45と同じ不変条件)。凍結スコープ
    に無い口座をここで足すことはない。
    """
    if not isinstance(bundle, dict):
        return bundle
    key = "_published_scenario" if "_published_scenario" in bundle else None
    scenario = bundle.get(key) if key else ((bundle.get("scenarios") or {}).get("primary"))
    if not isinstance(scenario, dict):
        return bundle
    contract = scenario.get("executionContract")
    if not isinstance(contract, dict):
        return bundle
    frozen = [str(value) for value in contract.get("accountScope") or [] if str(value)]
    keep = set(accounts)
    kept = [account for account in frozen if account in keep]
    if not kept or kept == frozen:
        return bundle
    dropped = [{"account": account, "reason": "MANUAL_POSITION"}
               for account in frozen if account not in keep]
    narrowed_scenario = {**scenario, "executionContract": {
        **contract, "accountScope": kept,
        "excludedAccounts": [*(contract.get("excludedAccounts") or []), *dropped]}}
    if key:
        return {**bundle, key: narrowed_scenario}
    scenarios = bundle.get("scenarios") if isinstance(bundle.get("scenarios"), dict) else {}
    return {**bundle, "scenarios": {**scenarios, "primary": narrowed_scenario}}


def _scoped_position_query(query, symbol: str, account: str) -> Dict[str, Any]:
    if query is None:
        import broker_status
        return broker_status.query_position(symbol, account=account)
    try:
        parameters = inspect.signature(query).parameters
    except (TypeError, ValueError):
        parameters = {}
    if "account" not in parameters and not any(
            item.kind == inspect.Parameter.VAR_KEYWORD for item in parameters.values()):
        return {"verified": False, "symbol": symbol, "accountId": account,
                "detail": "injected broker query has no account parameter"}
    return query(symbol, account=account)


def _scoped_order_query(query, symbol: str, account: str, known_order_ids=None) -> Dict[str, Any]:
    if query is None:
        import broker_status
        return broker_status.query_orders(
            symbol, known_order_ids=known_order_ids, account=account)
    try:
        parameters = inspect.signature(query).parameters
    except (TypeError, ValueError):
        parameters = {}
    if "account" not in parameters and not any(
            item.kind == inspect.Parameter.VAR_KEYWORD for item in parameters.values()):
        return {"verified": False, "symbol": symbol, "accountScope": [account],
                "state": "UNKNOWN", "detail": "injected broker order query has no account parameter"}
    kwargs = {"account": account}
    if "known_order_ids" in parameters or any(
            item.kind == inspect.Parameter.VAR_KEYWORD for item in parameters.values()):
        kwargs["known_order_ids"] = known_order_ids
    return query(symbol, **kwargs)


def _disarmed_single_account_management(
        bundle: Dict[str, Any], state_ok: bool, merged: Dict[str, str], proposal: Dict[str, Any],
        account: str, broker_query, runner, ledger_path: str, now: Optional[datetime],
        broker_order_query, state_query, claim_entry, claim_management,
        recover_entry, recover_management, broker_fills_query=None) -> Optional[List[str]]:
    """R68: 口座が 1 つでも AUTO OFF は「新規を止める」だけ(CLAUDE.md §3)。

    2 口座時代は ``reconcile`` の多口座分岐が、建玉・未終端注文のある口座にだけ
    ``NQX_AUTOTRADE=1``(管理専用)を差し込んで ``_reconcile_one`` を通していた。
    2026-09-04 に 1 口座へ戻してからは ``len(accounts) <= 1`` の近道で ``_reconcile_one``
    を直接呼ぶため、その冒頭の ``if not autotrade_enabled(cfg): return []`` で
    **AUTO 失効後の所有済み建玉が完全に放置**されていた(2026-09-08 16:05 に約定した
    SHORT 14 枚を 16:26 / 16:29 の reconcile が認識せず、台帳は ENTRY_RESTING のまま。
    TP1 後の建値移動・トレール・構造 SL 決済・未約定 ENTRY の取消も走らない)。

    ここでは多口座分岐と同じ規則を 1 口座に適用する:
      * 建玉が開いている / 未終端(blocking)の注文がある / KILL → 管理専用フラグで
        ``_reconcile_one`` へ。FLAT の口座に残る未約定 ENTRY は KILL 経路で取り消す。
      * 何も無ければ None を返し、従来どおり(空)に落ちる。新規 ENTRY はここから
        絶対に出ない(``_reconcile_one`` は建玉が無いときだけ ENTRY へ進み、その場合は
        この関数が None を返している)。
    """
    symbol = str(proposal.get("symbol") or merged.get("NQX_SYMBOL") or contract_month.symbol())
    query = broker_query
    order_query = broker_order_query
    if query is None:
        import broker_status
        query = lambda requested_symbol, account=account: broker_status.query_position(  # noqa: E731
            requested_symbol, account=account)
    if order_query is None:
        import broker_status
        order_query = lambda requested_symbol, known_order_ids=None, account=account: (  # noqa: E731
            broker_status.query_orders(requested_symbol, known_order_ids=known_order_ids,
                                       account=account))
    try:
        position = query(symbol)
    except Exception as exc:  # noqa: BLE001
        return [f"autotrade blocked: broker query failed ({type(exc).__name__}: {exc})"]
    if not isinstance(position, dict) or position.get("verified") is not True:
        return ["autotrade blocked: broker position is UNVERIFIED"]
    try:
        order = order_query(symbol)
    except Exception as exc:  # noqa: BLE001
        return [f"autotrade blocked: broker order query failed ({type(exc).__name__}: {exc})"]
    if not isinstance(order, dict) or order.get("verified") is not True:
        return ["autotrade blocked: broker order is UNVERIFIED"]
    try:
        open_qty = int(position.get("qty") or 0)
    except (TypeError, ValueError):
        open_qty = 0
    blocking = str(order.get("state") or "NONE").upper() in set(
        execution_contract.CONTRACT["blockingOrderStates"])
    kill = kill_enabled(merged)
    if open_qty <= 0 and not blocking:
        if kill:
            return ["autotrade kill: broker verified FLAT and broker order terminal"]
        return None
    cancel_resting = blocking and open_qty <= 0
    scoped_cfg = {**merged, "CROSSTRADE_ACCOUNTS": account,
                  # 管理/取消だけを許す。FLAT の枝は建玉があるとき通らないが、R84 の
                  # 追撃は open_qty>0 の枝に居るので、印を立てて明示的に止める。
                  "NQX_AUTOTRADE": "1",
                  "NQX_LIVE_ORDERS": "1",
                  ENTRY_DISARMED_KEY: "1",
                  "NQX_AUTOTRADE_KILL": "1" if (kill or cancel_resting) else "0"}
    notes = _reconcile_one(
        bundle, state_ok, scoped_cfg, query, runner, ledger_path, now,
        order_query, state_query, claim_entry, claim_management,
        recover_entry, recover_management, broker_fills_query=broker_fills_query)
    return notes or ["autotrade hold: managing owned position while AUTO is off"]


def reconcile(bundle: Dict[str, Any], state_ok: bool = True,
              cfg: Optional[Dict[str, str]] = None,
              broker_query: Optional[Callable[[str], Dict[str, Any]]] = None,
              runner: Optional[Callable[[List[str], bool], Tuple[int, str]]] = None,
              ledger_path: str = LEDGER_FILE,
              now: Optional[datetime] = None,
              broker_order_query: Optional[Callable[[str], Dict[str, Any]]] = None,
              state_query: Optional[Callable[[], Dict[str, Any]]] = None,
              claim_entry: Optional[Callable[[Dict[str, Any]], Tuple[bool, Dict[str, Any]]]] = None,
              claim_management: Optional[Callable[[Dict[str, Any]], Tuple[bool, Dict[str, Any]]]] = None,
              recover_entry: Optional[Callable[..., Tuple[bool, Dict[str, Any]]]] = None,
              recover_management: Optional[Callable[..., Tuple[bool, Dict[str, Any]]]] = None,
              broker_fills_query: Optional[Callable[[str], Dict[str, Any]]] = None,
              fresh_price_query: Optional[Callable[[str], Optional[float]]] = None) -> List[str]:
    """Reconcile one mirrored signal across every configured account.

    ENTRY remains one authoritative claim and one order.py route. Broker truth,
    ownership generations, MODIFY and FLATTEN are then reconciled independently
    per account so one account can never stand in for the other.

    ``broker_fills_query(account)`` (R76) は再束縛の枚数証拠。注入が無く、ブローカー照会も
    注入されていない(= 本番)ときだけ ``broker_status.query_fills`` を使う。テストの注入
    経路から本番の約定履歴へ到達することは無い。
    """
    merged = dict(_read_env_file(), **(cfg or {}))
    # R87: 設定ページの手動HALT を発注判断の直前に Worker から写す。本番経路(照会の注入が
    # 無い)だけで行い、注入テストからは Worker へ到達しない。読めなければ直前の既知値の
    # まま(通信障害で HALT が外れない)。3 分ループと fill_watch の両方がここを通る。
    if broker_query is None:
        try:
            autotrade_arm.sync_manual_halt()
        except Exception:  # noqa: BLE001 — 写しの失敗で監視を止めない(既知値で判断する)
            pass
    # R82: 設定側の緊急停止。管理も含めて何も触らない。撤退経路(KILL)だけは通す ——
    # 出口を消す緊急停止は緊急停止ではない。
    halt_source = manual_halt_source(merged)
    if halt_source is not None and not kill_enabled(merged):
        return [f"autotrade MANUAL HALT: {halt_source} "
                "(新規・追撃・建玉管理を停止中。撤退は --flatten / KILL で可能)"]
    if broker_fills_query is None and broker_query is None:
        broker_fills_query = _default_fills_query
    # R78: 送信直前の現在値(quote)。テストの注入経路(broker_query あり)からは本番の
    # TradingView へ到達しない。3 分ループと fill_watch の排他ロックもここで取る。
    if fresh_price_query is None and broker_query is None:
        fresh_price_query = _default_fresh_price_query
    lock_wait = _num(_setting("NQX_RECONCILE_LOCK_WAIT_SEC", merged, 120)) or 120.0
    with _ReconcileLock(ledger_path + ".lock", lock_wait) as acquired:
        if not acquired:
            return ["autotrade skip: reconcile lock is held by another process (loop/fill_watch)"]
        global _FRESH_PRICE_QUERY
        previous_query = _FRESH_PRICE_QUERY
        _FRESH_PRICE_QUERY = fresh_price_query
        try:
            return _reconcile_locked(
                bundle, state_ok, merged, broker_query, runner, ledger_path, now,
                broker_order_query, state_query, claim_entry, claim_management,
                recover_entry, recover_management, broker_fills_query, cfg)
        finally:
            _FRESH_PRICE_QUERY = previous_query


def _reconcile_locked(bundle, state_ok, merged, broker_query, runner, ledger_path, now,
                      broker_order_query, state_query, claim_entry, claim_management,
                      recover_entry, recover_management, broker_fills_query, cfg) -> List[str]:
    proposal = (bundle.get("_published_scenario") or {}) if isinstance(bundle, dict) and "_published_scenario" in bundle else (
        ((bundle.get("scenarios") or {}).get("primary") or {}) if isinstance(bundle, dict) else {})
    accounts = _multi_scope(merged)
    if broker_query is not None:
        injected_scope = [str(value) for value in
                          ((proposal.get("executionContract") or {}).get("accountScope") or [])
                          if str(value)]
        accounts = list(dict.fromkeys(injected_scope))
    entry_enabled = autotrade_enabled(merged)
    if len(accounts) <= 1:
        # 注入 query(テスト)で提案に accountScope が無いときは設定の口座へ戻す。
        solo = accounts[0] if accounts else next(iter(_multi_scope(merged)), None)
        if solo and not entry_enabled and isinstance(bundle, dict):
            managed = _disarmed_single_account_management(
                bundle, state_ok, merged, proposal, solo, broker_query, runner, ledger_path, now,
                broker_order_query, state_query, claim_entry, claim_management,
                recover_entry, recover_management, broker_fills_query=broker_fills_query)
            if managed is not None:
                return managed
        return _reconcile_one(
            bundle, state_ok, cfg, broker_query, runner, ledger_path, now,
            broker_order_query, state_query, claim_entry, claim_management,
            recover_entry, recover_management, broker_fills_query=broker_fills_query)
    if not isinstance(bundle, dict):
        return ["autotrade blocked: bundle is not an object"]

    symbol = str(proposal.get("symbol") or merged.get("NQX_SYMBOL") or contract_month.symbol())
    positions = {account: _scoped_position_query(broker_query, symbol, account)
                 for account in accounts}
    unverified_positions = [account for account, value in positions.items()
                            if not isinstance(value, dict) or value.get("verified") is not True]
    if unverified_positions:
        return ["autotrade blocked: broker position is UNVERIFIED for "
                + ",".join(unverified_positions)]
    orders = {account: _scoped_order_query(broker_order_query, symbol, account)
              for account in accounts}
    unverified_orders = [account for account, value in orders.items()
                         if not isinstance(value, dict) or value.get("verified") is not True]
    if unverified_orders:
        return ["autotrade blocked: broker order is UNVERIFIED for "
                + ",".join(unverified_orders)]

    blocking_states = set(execution_contract.CONTRACT["blockingOrderStates"])
    open_accounts = [account for account in accounts
                     if int(positions[account].get("qty") or 0) > 0]
    blocking_accounts = [account for account in accounts
                         if str(orders[account].get("state") or "NONE").upper() in blocking_states]
    kill_requested = kill_enabled(merged)
    excluded_notes: List[str] = []
    if open_accounts or blocking_accounts or kill_requested:
        notes: List[str] = []
        manual_accounts: List[str] = []
        targets = [account for account in accounts
                   if account in open_accounts or account in blocking_accounts]
        if not targets and kill_requested:
            return ["autotrade kill: all configured accounts verified FLAT and broker orders terminal"]
        # AUTO OFF/expiry stops new entries immediately, but never abandons an
        # already-owned position.  Frozen-plan MODIFY remains live because it
        # can only tighten/manage the broker OCO.  A still-resting entry on an
        # otherwise FLAT account is canceled through the existing FLATTEN path.
        live = live_enabled(merged)
        kill = kill_enabled(merged)
        for account in targets:
            cancel_resting = (not entry_enabled and account in blocking_accounts
                              and account not in open_accounts)
            scoped_cfg = {**merged, "CROSSTRADE_ACCOUNTS": account,
                          # _reconcile_one still re-verifies exact ownership;
                          # this enables only management/cancel for nonterminal broker truth.
                          "NQX_AUTOTRADE": "1",
                          "NQX_LIVE_ORDERS": "1" if (live or not entry_enabled) else "0",
                          # R84: AUTO が切れている周期は追撃も出さない(追撃は新規 ENTRY)。
                          ENTRY_DISARMED_KEY: "0" if entry_enabled else "1",
                          "NQX_AUTOTRADE_KILL": "1" if (kill or cancel_resting) else "0"}
            position_query = lambda requested_symbol, account=account: _scoped_position_query(
                broker_query, requested_symbol, account)
            order_query = lambda requested_symbol, known_order_ids=None, account=account: _scoped_order_query(
                broker_order_query, requested_symbol, account, known_order_ids)
            account_notes = _reconcile_one(
                bundle, state_ok, scoped_cfg, position_query, runner, ledger_path, now,
                order_query, state_query, claim_entry, claim_management,
                recover_entry, recover_management, broker_fills_query=broker_fills_query)
            # R46: 手動で進めている建玉は「その口座だけ」経路から外す。KILL は
            # 台帳外の建玉も落とす明示経路なので、除外判定を一切しない。
            if (not kill_requested and account in open_accounts
                    and _is_manual_position_account(account_notes)):
                manual_accounts.append(account)
            notes.extend(f"[{account}] {note}" for note in account_notes)
        remaining = [account for account in accounts if account not in set(manual_accounts)]
        still_busy = [account for account in remaining
                      if account in open_accounts or account in blocking_accounts]
        for account in manual_accounts:
            notes.append(
                f"[{account}] autotrade excluded: manual position outside the frozen "
                f"ledger; {len(remaining)} account(s) stay armed")
        if not manual_accounts or not remaining or still_busy or not entry_enabled:
            return notes or ["autotrade hold: multi-account portfolio is nonterminal"]
        # 残った口座は独立に FLAT/terminal を検証済み。凍結スコープを狭めて
        # そのまま通常の ENTRY 経路へ落とす。台帳外の建玉には一切触れない。
        excluded_notes = notes
        accounts = remaining
        merged = {**merged, "CROSSTRADE_ACCOUNTS": ",".join(remaining)}
        bundle = _narrow_bundle_scope(bundle, remaining)

    # All accounts were independently verified FLAT/terminal. One shared ENTRY
    # claim and one order.py invocation then routes both accounts atomically at
    # the application layer; the route envelope must contain both accounts.
    if not entry_enabled:
        return excluded_notes
    first = accounts[0]
    position_query = lambda requested_symbol: _scoped_position_query(
        broker_query, requested_symbol, first)
    order_query = lambda requested_symbol, known_order_ids=None: _scoped_order_query(
        broker_order_query, requested_symbol, first, known_order_ids)
    return excluded_notes + _reconcile_one(
        bundle, state_ok, merged, position_query, runner, ledger_path, now,
        order_query, state_query, claim_entry, claim_management,
        recover_entry, recover_management, broker_fills_query=broker_fills_query)


def _cli(argv: Optional[List[str]] = None) -> int:
    """台帳の HALT を見る/解く(R52)。発注・照会・publish は一切しない。"""
    import argparse

    parser = argparse.ArgumentParser(description="autotrade ledger HALT 管理(送信なし)")
    parser.add_argument("--list-halts", action="store_true", help="HALT を新しい順に表示")
    parser.add_argument("--clear-halt", metavar="KEY", help="この key の HALT を HALT_CLEARED で解く")
    parser.add_argument("--reason", help="--clear-halt の理由(必須)")
    parser.add_argument("--evidence", help="根拠(ブローカー照会の結果など)")
    parser.add_argument("--operator", help="解いた人")
    parser.add_argument("--ledger", default=LEDGER_FILE)
    args = parser.parse_args(argv)
    if args.clear_halt:
        ok, detail = clear_halt(args.clear_halt, args.reason or "", evidence=args.evidence,
                                operator=args.operator, ledger_path=args.ledger)
        print(("HALT_CLEARED " if ok else "REFUSED ") + json.dumps(detail, ensure_ascii=False))
        return 0 if ok else 1
    rows = list_halts(args.ledger)
    if not rows:
        print("HALT なし")
        return 0
    blocking_seen = False
    for row in rows:
        if row["cleared"]:
            mark = "cleared"
        elif not blocking_seen:
            mark = "BLOCKING"
            blocking_seen = True
        else:
            mark = "open"
        print(f"[{mark:8}] #{row['index']} {row['time']} {row['action']} {row['key']}")
        print(f"           {row['reason']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())

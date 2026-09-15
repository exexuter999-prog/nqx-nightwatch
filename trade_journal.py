#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""戦績の自動記録 — 決済を検知して LEDGER(Mini App)へ result を publish する。

## なぜ要るか

2026-08-27 の点検時点で、Durable Object の ``recentResults`` は **4件・全て
``exitSource=manual``・最新 08-25** だった。一方 ``autotrade_ledger.jsonl`` には
08-26〜27 に建玉世代が8つ開いている。**自動記録は一度も成立していなかった。**

原因は3つ重なっていた:

1. 決済を検知して result を作るのは ``telegram_bot.publish_trade_result`` だけ。
   3分ループ(``nqx_cycle`` → ``monitor_publish`` → ``autotrade_engine.reconcile``)
   はこの経路を**一度も呼ばない**。しかも bot プロセスは常駐していない。
2. 仮に呼ばれても、CrossTrade アダプタの FLAT 応答は ``avgExit`` を返さない
   (``broker_status.realised_prices()`` は Tradovate 分岐にしか繋がっていない)。
   決済価格が無いので publish されず「/result で手打ちしろ」に落ちる。
3. 結果として ``day_ledger.jsonl`` も止まり、日次集計まで死んでいた。

本モジュールは 1 と 2 を埋める。**常駐している唯一のプロセス**である3分ループ
から呼ばれ、建玉が閉じた瞬間を自分で観測して記録する。

## 決済価格をどこから取るか(重要)

CrossTrade は約定価格を返さない(``docs`` / 実測)。そこで:

  - **価格は凍結プランの脚**を使う。TP1 / RUNNER の最終TP / 構造SL /
    トレール後のSL —— いずれも**こちらが実際にブローカーへ置いた価格**で、
    相場から推定した値ではない。
  - **どの脚が約定したか**は、ブローカーの建玉枚数の遷移(2→1→0)と、
    確定足がその水準に触れたかどうかで決める。触れていない水準は選ばない。
  - どちらの水準にも触れていない減少は **推測しない**。その建玉は
    ``needsManualExit`` として保留し、人が ``/result`` で入れるまで publish
    しない。嘘の約定価格を作るくらいなら記録しないほうがよい。
  - **金額はブローカーが正本。** 口座の当日 ``realizedPnL`` の差分を
    ``reported_net`` として渡すと、価格由来の粗損益との差が ``fees``
    (滑り+手数料)としてカードに出る。脚価格の見た目を保ったまま、
    合計額はブローカーと一致する。

``exitSource`` は ``broker``。**決め手になった事実(どの脚が落ちたか・いくら
動いたか)はすべてブローカー由来**であり、価格はその脚に紐づく既知の指値だから。
価格そのものがブローカー報告でないことは ``exitBasis="plan-legs"`` として
ローカル台帳へ残す(Worker はホワイトリスト外の項目を落とすので画面には出ない)。

## R56 (2026-09-05): 約定(fills)を正本にする

上の「脚 + 到達」方式は **一度も成立しなかった**。2026-08-27〜09-04 の実トレード
18 件は全部 ``needsManualExit`` で保留され、``published`` は 0 件のままだった。
理由は単純で、実際の建玉は凍結プランと一致しない —— 手動建玉(SELL プランの
口座に LONG)、ULTRA 枚数(プラン 8 枚に対し約定 18 枚)、トレール後 SL、
同一 orderId の再利用。到達判定はプランが正しい前提でしか動かない。

ブローカーは ``GET /accounts/{name}/fills`` で約定そのもの(価格・枚数・
orderId・時刻)を返す(2026-09-05 実測)。以後は約定列から往復を組み立て、
価格を**推定しない**:

  - 建玉が 0 → 非0 で開き、0 に戻った瞬間を1往復とする。途中の同方向約定は
    建値(数量加重平均)へ、逆方向約定は決済脚へ入る。
  - 凍結プランは **orderId** で引く(``pendingOrderOwnership.orderIds`` /
    ``brokerOrder.filledOrderIds`` など台帳に残る ID)。引けなければ同方向・
    建値 2pt 以内・30 分以内のプランを近傍照合し、それも無ければ**無帰属**
    (手動建玉)として stop 無しで記録する —— R は出さないが損益は残る。
  - 約定列から出る建玉と、ブローカーの建玉照会が一致しないサイクルは
    **状態を進めず保留**する(約定の取りこぼしを記録にしない)。

旧経路(``observe`` / ``attribute_exit``)は約定を取れない口座のためだけに残す。

## 発注はしない

読むだけ。``order.py`` も CrossTrade の書き込み系も呼ばない。
publish 先は Durable Object の ``result`` ストリームだけ。
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

BASE = os.path.dirname(os.path.abspath(__file__))
JOURNAL_FILE = os.path.join(BASE, ".secrets", "trade_journal.json")
LEDGER_FILE = os.path.join(BASE, ".secrets", "autotrade_ledger.jsonl")

TICK = 0.25
# 水準に「触れた」と認める許容。ブローカーは指値ちょうどで約定するので、
# 確定足の高安がその価格に達していれば十分。浮動小数の誤差だけを吸収する。
TOUCH_EPS = 1e-9


# ---------------------------------------------------------------- 小道具

def _num(value: Any) -> Optional[float]:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result and abs(result) != float("inf") else None


def _iso(now: Optional[datetime] = None) -> str:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()


def _parse_at(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _side_word(raw: Any) -> Optional[str]:
    word = str(raw or "").strip().upper()
    if word in ("BUY", "LONG"):
        return "LONG"
    if word in ("SELL", "SHORT"):
        return "SHORT"
    return None


def load_journal(path: str = JOURNAL_FILE) -> Dict[str, Any]:
    """台帳を読む。壊れていたら**空で始める**(記録の欠落 < 誤った記録)。"""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {"open": {}, "published": []}
    if not isinstance(data, dict):
        return {"open": {}, "published": []}
    data.setdefault("open", {})
    data.setdefault("published", [])
    if not isinstance(data["open"], dict):
        data["open"] = {}
    if not isinstance(data["published"], list):
        data["published"] = []
    return data


def save_journal(journal: Dict[str, Any], path: str = JOURNAL_FILE) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(journal, fh, ensure_ascii=False, indent=1)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


# ---------------------------------------------------------------- 足の観測

def confirmed_bars(bundle: Dict[str, Any]) -> List[Dict[str, float]]:
    """bundle の確定3分足を ``{t,h,l,c}`` へ正規化する。形成中足は入れない。"""
    snapshot = bundle.get("snapshot") if isinstance(bundle.get("snapshot"), dict) else {}
    raw = snapshot.get("bars3m") or bundle.get("bars3m") or []
    rows: List[Dict[str, float]] = []
    for bar in raw if isinstance(raw, list) else []:
        if not isinstance(bar, dict):
            continue
        t = _num(bar.get("t", bar.get("time")))
        h = _num(bar.get("h", bar.get("high")))
        low = _num(bar.get("l", bar.get("low")))
        c = _num(bar.get("c", bar.get("close")))
        if None in (t, h, low, c) or h < low:
            continue
        rows.append({"t": t, "h": h, "l": low, "c": c})
    rows.sort(key=lambda row: row["t"])
    return rows


def extremes_since(bars: List[Dict[str, float]], since: Optional[float]) -> Tuple[Optional[float], Optional[float]]:
    """``since``(epoch秒)以降の確定足の高値・安値。無ければ ``(None, None)``。"""
    picked = [b for b in bars if since is None or b["t"] >= since - 1]
    if not picked:
        return None, None
    return max(b["h"] for b in picked), min(b["l"] for b in picked)


def _touched(side: str, level: float, high: Optional[float], low: Optional[float],
             favourable: bool) -> bool:
    """その水準に価格が届いたか。**届いていないものを届いたことにしない。**"""
    if high is None or low is None:
        return False
    # LONG の利確は上、損切りは下。SHORT はその逆。
    upward = (side == "LONG") == favourable
    return (high >= level - TOUCH_EPS) if upward else (low <= level + TOUCH_EPS)


# ---------------------------------------------------------------- プランの脚

def plan_legs(plan: Dict[str, Any]) -> List[Dict[str, Any]]:
    """凍結プランの脚を ``[{id, qty, target}]`` で返す(順序 = 決済される順)。"""
    legs = []
    for leg in plan.get("legs") or []:
        if not isinstance(leg, dict):
            continue
        target = _num(leg.get("target"))
        try:
            qty = int(leg.get("qty") or 0)
        except (TypeError, ValueError):
            qty = 0
        if qty < 1 or target is None:
            continue
        legs.append({"id": str(leg.get("id") or "LEG").upper(), "qty": qty, "target": target})
    if legs:
        return legs
    # 旧プラン(legs 無し)。tp1 / finalTarget から組み立てる。
    tp1, final = _num(plan.get("tp1")), _num(plan.get("finalTarget"))
    total = int(_num(plan.get("qty")) or 0)
    if tp1 is not None and final is not None and total >= 2:
        return [{"id": "TP1", "qty": total // 2, "target": tp1},
                {"id": "RUNNER", "qty": total - total // 2, "target": final}]
    if final is not None and total >= 1:
        return [{"id": "POSITION", "qty": total, "target": final}]
    return []


def current_stop(records: List[Dict[str, Any]], plan: Dict[str, Any],
                 entry_key: str) -> Optional[float]:
    """runner の現在の SL。トレールで動いていれば最新の MANAGEMENT_SENT を採る。

    ``management_action`` の MODIFY は ``{"action":"MODIFY","sl":<価格>,...}``。
    ``autotrade_engine`` はこれを ``MANAGEMENT_SENT`` として台帳へ残すので、
    「今その建玉を守っている価格」はそこから読める(推測しない)。
    """
    latest = None
    for record in records:
        if record.get("status") != "MANAGEMENT_SENT":
            continue
        if entry_key and str(record.get("planEntryKey") or "") not in ("", entry_key):
            continue
        action = record.get("action")
        if isinstance(action, dict) and _num(action.get("sl")) is not None:
            latest = _num(action["sl"])
    return latest if latest is not None else _num(plan.get("initialStop"))


# ---------------------------------------------------------------- 脚の割り当て

def attribute_exit(side: str, closed_qty: int, pending: List[Dict[str, Any]],
                   stop: Optional[float], high: Optional[float],
                   low: Optional[float]) -> List[Dict[str, Any]]:
    """減った枚数を、実際に価格が届いた水準へ割り当てる。

    返すのは ``[{id, qty, exit, kind}]``。割り当てられない枚数が1枚でも
    残ったら **空リストを返す**(部分的に嘘を混ぜるより、保留して人に回す)。
    """
    if closed_qty < 1 or not pending:
        return []
    assigned: List[Dict[str, Any]] = []
    remaining = closed_qty
    for leg in list(pending):
        if remaining < 1:
            break
        take = min(remaining, int(leg["qty"]))
        if _touched(side, leg["target"], high, low, favourable=True):
            assigned.append({"id": leg["id"], "qty": take,
                             "exit": leg["target"], "kind": "target"})
        elif stop is not None and _touched(side, stop, high, low, favourable=False):
            assigned.append({"id": leg["id"], "qty": take,
                             "exit": stop, "kind": "stop"})
        else:
            return []                    # どの水準にも届いていない → 推測しない
        remaining -= take
    return assigned if remaining < 1 else []


# ---------------------------------------------------------------- 観測 → 記録

def _open_key(account: str, generation: str) -> str:
    return f"{account}|{generation}"


def observe(bundle: Dict[str, Any], positions: Dict[str, Dict[str, Any]],
            *, plans: Dict[str, Dict[str, Any]], records: List[Dict[str, Any]],
            balances: Optional[Dict[str, Dict[str, Any]]] = None,
            journal: Optional[Dict[str, Any]] = None,
            now: Optional[datetime] = None) -> Tuple[Dict[str, Any], List[Dict[str, Any]], List[str]]:
    """1サイクル分の観測を台帳へ反映し、**閉じた建玉**を返す。

    引数はすべて呼び出し側が用意する(この関数はネットワークを触らない):
      ``positions`` / ``plans`` / ``balances`` は口座IDをキーにした辞書。

    戻り値 ``(journal, closed, notes)``。``closed`` の各要素が publish 候補。
    """
    journal = journal if journal is not None else load_journal()
    balances = balances or {}
    bars = confirmed_bars(bundle)
    stamp = _iso(now)
    closed: List[Dict[str, Any]] = []
    notes: List[str] = []

    for account, position in (positions or {}).items():
        if not isinstance(position, dict) or position.get("verified") is not True:
            continue                      # 未確認の照会で建玉の生死を判断しない
        qty = int(_num(position.get("qty")) or 0)
        plan = plans.get(account) if isinstance(plans.get(account), dict) else None
        balance = balances.get(account) or {}
        realized_now = _num(balance.get("realizedPnL")) if balance.get("verified") is True else None

        # --- 建玉が見えている: 開いた記録が無ければ作る / 極値を更新する
        if qty > 0:
            generation = str(position.get("generation") or position.get("orderId")
                             or position.get("filledAt") or "live")
            key = _open_key(account, generation)
            entry = _num(position.get("avgEntry"))
            side = _side_word(position.get("side"))
            if key not in journal["open"]:
                if plan is None or entry is None or side is None:
                    notes.append(f"trade journal: open position on one account has no frozen "
                                 f"plan/avgEntry yet — not tracking this cycle")
                    continue
                opened_at = position.get("filledAt") or position.get("observedAt") or stamp
                journal["open"][key] = {
                    "account": account,
                    "generation": generation,
                    "entryKey": str(plan.get("entryKey") or ""),
                    "symbol": str(position.get("symbol") or plan.get("symbol") or "MNQU6"),
                    "side": side,
                    "entry": entry,
                    "initialStop": _num(plan.get("initialStop")),
                    "initialQty": qty,
                    "remaining": qty,
                    "openedAt": str(opened_at),
                    "openedEpoch": (_parse_at(opened_at).timestamp()
                                    if _parse_at(opened_at) else None),
                    "pending": plan_legs(plan),
                    "filled": [],
                    "realizedAtOpen": realized_now,
                    "realizedDayAtOpen": str(balance.get("observedAt") or "")[:10] or None,
                    "plan": plan,
                    "lastSeenAt": stamp,
                }
                notes.append(f"trade journal: tracking new position "
                             f"{side} {qty} @{entry:,.2f}")
                continue

            state = journal["open"][key]
            state["lastSeenAt"] = stamp
            previous = int(state.get("remaining") or 0)
            if qty < previous:
                _record_partial(state, previous - qty, bars, records, stamp, notes)
            state["remaining"] = qty
            continue

        # --- FLAT: この口座で開いていた記録があれば閉じる
        for key in [k for k in journal["open"] if k.startswith(f"{account}|")]:
            state = journal["open"][key]
            previous = int(state.get("remaining") or 0)
            if previous > 0:
                _record_partial(state, previous, bars, records, stamp, notes)
            state["remaining"] = 0
            state["closedAt"] = str(position.get("closedAt") or position.get("observedAt") or stamp)
            state["realizedAtClose"] = realized_now
            journal["open"].pop(key)
            if state.get("needsManualExit"):
                notes.append("trade journal: position closed but the fill level could not be "
                             "identified from confirmed bars — record it with /result "
                             "(no invented price was published)")
                journal.setdefault("unresolved", []).append(state)
                continue
            closed.append(state)

    return journal, closed, notes


def _record_partial(state: Dict[str, Any], closed_qty: int, bars: List[Dict[str, float]],
                    records: List[Dict[str, Any]], stamp: str, notes: List[str]) -> None:
    """減った枚数を脚へ割り当てて ``state['filled']`` に足す。"""
    high, low = extremes_since(bars, state.get("openedEpoch"))
    stop = current_stop(records, state.get("plan") or {}, str(state.get("entryKey") or ""))
    assigned = attribute_exit(str(state["side"]), closed_qty,
                              state.get("pending") or [], stop, high, low)
    if not assigned:
        state["needsManualExit"] = True
        return
    for leg in assigned:
        leg["at"] = stamp
        state.setdefault("filled", []).append(leg)
        for candidate in list(state.get("pending") or []):
            if candidate["id"] == leg["id"]:
                candidate["qty"] = int(candidate["qty"]) - int(leg["qty"])
                if candidate["qty"] < 1:
                    state["pending"].remove(candidate)
                break


# ---------------------------------------------------------------- publish

def build_and_publish(state: Dict[str, Any], *, mode: str = "SIMULATION",
                      bars: Optional[List[Dict[str, Any]]] = None,
                      publisher: Optional[Callable[..., Tuple[bool, Any]]] = None,
                      builder: Optional[Callable[..., Tuple[Dict[str, Any], Any]]] = None,
                      ) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
    """閉じた建玉から result を作って publish する。

    金額はブローカーの ``realizedPnL`` 差分を ``reported_net`` として渡し、
    脚価格から出る粗損益との差を ``fees`` に落とす —— カードの合計は必ず
    ブローカーと一致する。差が取れないときは粗損益だけで出す(捏造しない)。
    """
    import nqx_state

    builder = builder or nqx_state.result_from_report
    publisher = publisher or nqx_state.publish_result

    legs = [{"id": leg["id"], "qty": int(leg["qty"]), "exit": float(leg["exit"]),
             "kind": leg["kind"]} for leg in state.get("filled") or []]
    if not legs:
        return False, "no attributed exit legs", None

    opened = _num(state.get("realizedAtOpen"))
    closing = _num(state.get("realizedAtClose"))
    reported_net = round(closing - opened, 2) if (opened is not None and closing is not None) else None

    plan = state.get("plan") if isinstance(state.get("plan"), dict) else {}
    kwargs = dict(
        side=state["side"], entry=float(state["entry"]),
        stop=float(state["initialStop"]), qty=int(state.get("initialQty") or 0),
        opened_at=state["openedAt"], closed_at=state["closedAt"],
        symbol=state.get("symbol") or "MNQU6", bars=bars, mode=mode,
        legs=legs, exit_source="broker",
        # ライフサイクル突合キー。凍結プランのシナリオと決済元の口座を result に
        # 残し、Mini App がエントリー→決済のカードをキーで繋げられるようにする。
        scenario_id=plan.get("scenarioId") or None,
        account_id=state.get("account") or None,
        # R48: モデル別スコアカード。凍結プランの model/grade を成績へ紐付ける。
        model=plan.get("model") or None,
        grade=plan.get("grade") or None,
    )
    try:
        result, derivation = builder(**kwargs, reported_net=reported_net)
    except (ValueError, TypeError) as exc:
        # reported_net が粗損益を上回る(有利な滑り)ときは fees が負になり
        # 拒否される。金額の突合は諦め、脚価格どおりの粗損益で出す。
        if reported_net is None:
            return False, f"could not build result: {exc}", None
        try:
            result, derivation = builder(**kwargs)
        except (ValueError, TypeError) as exc2:
            return False, f"could not build result: {exc2}", None
        derivation = f"{derivation or ''} / broker net not reconciled: {exc}".strip(" /")

    # 価格の出所をローカル台帳に残す。Worker は白リスト外の項目を落とすので
    # 画面には出ないが、「この価格がどこから来たか」を後から辿れるようにする。
    result["exitBasis"] = "plan-legs"
    # R57: 根拠チャート(表示専用)。付けられなくても決済記録は止めない。
    import result_context
    result_context.attach(result, plan)
    ok, detail = publisher(result)
    if not ok:
        return False, f"publish failed: {str(detail)[:160]}", result
    return True, (derivation or "recorded from plan legs"), result


# ---------------------------------------------------------------- 約定(fills)からの再構成(R56)

FILL_SEEN_LIMIT = 4000
LEG_LIMIT = 8                       # Worker の RESULT_LEG_LIMIT と同じ
ATTRIBUTION_WINDOW_SEC = 30 * 60    # 凍結プランへの近傍照合(記録時刻との差)
ATTRIBUTION_PRICE_PT = 2.0          # 同(建値の差)
_BROKER = object()                  # reconcile(fills_query=…) の既定 = broker_status


def _tick(value: float) -> float:
    """0.25 tick へ丸める(Worker の normalizeTick と同じ)。"""
    return round(float(value) / TICK) * TICK


def normalize_fills(rows: Any, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
    """``broker_status.query_fills`` の行を時系列の ``{id, orderId, action, qty, price, at, epoch}`` へ。

    銘柄が分かる行は ``symbol`` と突き合わせる(MNQU6 の約定に他限月を混ぜない)。
    価格・枚数・時刻のどれかが無い行は落とす(推測で埋めない)。
    """
    out: List[Dict[str, Any]] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        action = str(row.get("action") or row.get("side") or "").strip().upper()
        if action in ("BUY", "B", "LONG"):
            action = "BUY"
        elif action in ("SELL", "S", "SHORT"):
            action = "SELL"
        else:
            continue
        qty = int(_num(row.get("qty")) or 0)
        price = _num(row.get("price"))
        at = _parse_at(row.get("at") or row.get("timestamp"))
        if qty < 1 or price is None or at is None:
            continue
        instrument = str(row.get("instrument") or "").strip()
        if symbol and instrument and instrument.upper() not in str(symbol).upper() \
                and str(symbol).upper() not in instrument.upper():
            continue
        out.append({"id": str(row.get("id") or ""), "orderId": str(row.get("orderId") or ""),
                    "action": action, "qty": qty, "price": float(price),
                    "at": at.isoformat(), "epoch": at.timestamp(), "instrument": instrument})
    out.sort(key=lambda item: (item["epoch"], item["id"]))
    return out


def _fill_leg(fill: Dict[str, Any], qty: int) -> Dict[str, Any]:
    return {"qty": int(qty), "price": float(fill["price"]), "at": fill["at"],
            "orderId": str(fill.get("orderId") or ""), "fillId": str(fill.get("id") or "")}


def merge_legs(closes: List[Dict[str, Any]], limit: int = LEG_LIMIT) -> List[Dict[str, Any]]:
    """連続する同一注文・同一価格の約定を 1 脚にまとめる。

    Tradovate は 1 注文を複数約定に割る(09-04 は 28 枚の SL が 10 約定・2 価格)。
    脚数が上限を超えれば価格だけで束ね、それでも超えれば隣同士を数量加重で
    足し合わせる(合計の加重平均は変わらない)。
    """
    merged: List[Dict[str, Any]] = []
    for leg in closes:
        last = merged[-1] if merged else None
        if last and last["orderId"] == leg["orderId"] and abs(last["price"] - leg["price"]) < TOUCH_EPS:
            last["qty"] += int(leg["qty"])
            last["at"] = leg["at"]
        else:
            merged.append(dict(leg))
    if len(merged) > limit:
        by_price: List[Dict[str, Any]] = []
        for leg in merged:
            last = by_price[-1] if by_price else None
            if last and abs(last["price"] - leg["price"]) < TOUCH_EPS:
                last["qty"] += int(leg["qty"])
                last["at"] = leg["at"]
            else:
                by_price.append(dict(leg))
        merged = by_price
    while len(merged) > limit:
        first, second = merged[0], merged[1]
        total = int(first["qty"]) + int(second["qty"])
        blended = (first["price"] * first["qty"] + second["price"] * second["qty"]) / total
        merged[0:2] = [{**second, "qty": total, "price": _tick(blended)}]
    return merged


def _finish_trade(trade: Dict[str, Any]) -> Dict[str, Any]:
    opens = trade["opens"]
    qty = sum(int(leg["qty"]) for leg in opens)
    entry = sum(float(leg["price"]) * int(leg["qty"]) for leg in opens) / qty
    # 建値は tick に丸める(Worker が同じ丸めをするので、ここで揃えておく)。
    # 丸め差(最大 0.125pt)は realizedPnL の突合が fees として吸収する。
    return {**trade, "qty": qty, "entry": _tick(entry), "entryRaw": entry,
            "legs": merge_legs(trade["closes"])}


#: MNQ の 1pt あたりの金額。nqx_state.result_from_report の既定と揃える。
POINT_VALUE = 2.0


def _gross_usd(trade: Dict[str, Any], point_value: float = POINT_VALUE) -> Optional[float]:
    """価格由来の粗損益($)。nqx_state が result で使う式と同じものを再現する。"""
    legs = trade.get("legs") or []
    qty = sum(int(leg["qty"]) for leg in legs)
    if qty <= 0 or not trade.get("entry"):
        return None
    exit_price = sum(float(leg["price"]) * int(leg["qty"]) for leg in legs) / qty
    direction = 1.0 if str(trade.get("side") or "").upper() == "LONG" else -1.0
    return (exit_price - float(trade["entry"])) * direction * float(point_value) * int(trade["qty"])


#: R85: 手数料単価の設定キー(`.secrets/crosstrade.env`)。`FEE_PER_SIDE_<口座ID>` が
#: 口座別、`FEE_PER_SIDE` が全口座の既定。単位は **1 枚・片道あたり $**(MNQ)。
FEE_RATE_KEY = "FEE_PER_SIDE"
#: R85: 手数料として認める上限(1 枚・片道あたり $)。MNQ の手数料は業者を問わず片道
#: $0.25〜$1.50 程度で、これを大きく超える値は「残高差分の取り方が壊れた」印である
#: (残高照会の反映遅れ・日次リセット跨ぎ)。2026-09-10 01:26 の 4 枚の往復に $173
#: (= 片道 1 枚 $21.6)が載ったのがこれ。
FEE_SANITY_MAX_PER_SIDE = 5.0


def fee_rates_from_env(env: Any, accounts: List[str]) -> Dict[str, float]:
    """R85: 口座ごとの手数料単価($/枚/片道)。設定が無い・不正な口座は含めない。

    含めなかった口座は従来どおり realizedPnL 差分(R54)へ倒れる。
    """
    env = env if isinstance(env, dict) else {}
    default = _num(env.get(FEE_RATE_KEY))
    rates: Dict[str, float] = {}
    for account in accounts or []:
        value = _num(env.get(f"{FEE_RATE_KEY}_{account}"))
        if value is None:
            value = default
        if value is not None and 0.0 <= value <= FEE_SANITY_MAX_PER_SIDE:
            rates[str(account)] = float(value)
    return rates


def contracts_traded(trade: Dict[str, Any]) -> int:
    """往復で約定した枚数(建て + 決済)。手数料は約定 1 枚ごとに掛かる。"""
    return (sum(int((leg or {}).get("qty") or 0) for leg in trade.get("opens") or [])
            + sum(int((leg or {}).get("qty") or 0) for leg in trade.get("closes") or []))


def contract_fees(trade: Dict[str, Any], rate: Any) -> Optional[float]:
    """R85: 手数料 = 約定枚数 × 単価。

    R54 の「フラット時の realizedPnL からの差分」は、**残高照会がブローカーの約定より
    遅れて更新される**と基準ごと壊れる。2026-09-10 01:10:07 に閉じた往復の 63 秒後に
    次の往復が開き、その間のサイクルが「前の損失がまだ反映されていない実現損益」を
    基準に取ったため、次の往復(4 枚)に $173 が載った。単価は口座の性質で、約定の
    枚数はブローカーの事実なので、この積はタイミングに依存しない。
    """
    rate_value = _num(rate)
    contracts = contracts_traded(trade)
    if rate_value is None or rate_value < 0 or contracts <= 0:
        return None
    return round(contracts * rate_value, 2)


def allocate_fees(closed: List[Dict[str, Any]], flat: Optional[Dict[str, Any]],
                  realized_now: Optional[float], day_now: Optional[str],
                  point_value: float = POINT_VALUE, *, sod_now: Optional[float] = None,
                  ) -> Tuple[Optional[List[float]], Optional[float]]:
    """R54: 手数料をブローカーの realizedPnL 差分から出し、枚数で配分する。

    2026-09-07 の実測で、旧実装は 1 日 $36.00 の手数料のうち **$9.00 しか**
    記録できていなかった(LEDGER 合計 $1,525.50 に対しブローカー実現損益
    $1,498.50)。原因は 2 つとも「差分の取り方」にある。

    1. 目印(``mark``)を **建玉が開いた後**の周期で取っていたので、その時点の
       realizedPnL には既にエントリー側の手数料が入っており、差分には決済側
       しか残らない —— 18 枚の往復で $18 のところ $9 になった(ちょうど半分)。
    2. 1 周期に 2 件以上決済されると ``len(closed) == 1`` の条件から外れ、
       **どちらにも手数料が付かなかった**(9 枚 × 2 件で $18 が丸ごと欠落)。

    そこで基準を「建玉ゼロを観測したときの realizedPnL」(``flat``)に変える。
    フラットの瞬間はエントリー手数料がまだ発生していないので、差分は
    **その後に閉じた往復すべての純額**になる。総額は
    ``粗損益の合計 − 差分`` で、これはブローカーの事実から出た 1 つの数字。
    往復ごとの内訳はブローカーが返さないので、**手数料の性質どおり枚数で
    按分**する(端数は最後の 1 件が吸収)。分解できないときは None を返し、
    従来どおり手数料なしで記録する —— 推測で埋めない。
    """
    if not closed or not isinstance(flat, dict) or realized_now is None:
        return None, None
    base = _num(flat.get("realized"))
    if base is None:
        return None, None
    if flat.get("day") and day_now and str(flat["day"]) != str(day_now):
        # realizedPnL は日次でリセットされるので、日を跨いだ差分は意味を持たない。
        return None, None
    base_sod = _num(flat.get("sod"))
    if base_sod is not None and sod_now is not None and abs(base_sod - float(sod_now)) > 1e-9:
        # R85: `day` は照会時刻の **JST 日付**だが、ブローカーの realizedPnL は取引日
        # (JST 07:00 前後)でリセットされる。同じ JST 日付のまま取引日を跨ぐと差分が
        # 丸ごと手数料に化ける。日初純資産(netLiqSOD)が変わっていれば取引日が変わった。
        return None, None
    gross = [_gross_usd(trade, point_value) for trade in closed]
    if any(value is None for value in gross):
        return None, None
    total_qty = sum(int(trade["qty"]) for trade in closed)
    if total_qty <= 0:
        return None, None
    total_fees = round(sum(gross) - (float(realized_now) - float(base)), 2)
    if total_fees < 0:
        # 純額が粗損益を上回る(有利な滑り)。手数料としては分解できない。
        return None, None
    contracts = sum(contracts_traded(trade) or 2 * int(trade["qty"]) for trade in closed)
    if contracts > 0 and total_fees / contracts > FEE_SANITY_MAX_PER_SIDE + 1e-9:
        # R85: 手数料としてあり得ない額。残高の反映遅れで基準が古かった(2026-09-10 の
        # $173)。推測で配らず「手数料なし」で記録する —— 嘘の純額を LEDGER に残さない。
        return None, None
    shares: List[float] = []
    allocated = 0.0
    for index, trade in enumerate(closed):
        if index == len(closed) - 1:
            share = round(total_fees - allocated, 2)
        else:
            share = round(total_fees * int(trade["qty"]) / total_qty, 2)
            allocated += share
        shares.append(max(0.0, share))
    return shares, total_fees


def round_trips(fills: List[Dict[str, Any]],
                state: Optional[Dict[str, Any]] = None) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """約定列から往復(開いて閉じた建玉)を組み立てる。

    ``state`` は前回の続き ``{"position": 符号付き枚数, "trade": 進行中の往復}``。
    建玉が 0 のときの約定で開き、同方向は建値へ、逆方向は決済脚へ。逆方向の
    約定が建玉を突き抜ければ(ドテン)、余りで反対向きの往復を開く。
    """
    state = dict(state or {})
    pos = int(state.get("position") or 0)
    trade = state.get("trade") if isinstance(state.get("trade"), dict) else None
    closed: List[Dict[str, Any]] = []
    for fill in fills:
        remaining = int(fill["qty"]) if fill["action"] == "BUY" else -int(fill["qty"])
        while remaining:
            if pos == 0 or trade is None:
                trade = {"side": "LONG" if remaining > 0 else "SHORT",
                         "opens": [_fill_leg(fill, abs(remaining))], "closes": [],
                         "openedAt": fill["at"], "openedEpoch": fill["epoch"]}
                pos, remaining = remaining, 0
            elif (remaining > 0) == (pos > 0):
                trade["opens"].append(_fill_leg(fill, abs(remaining)))
                pos, remaining = pos + remaining, 0
            else:
                take = min(abs(remaining), abs(pos))
                trade["closes"].append(_fill_leg(fill, take))
                if remaining > 0:
                    pos += take
                    remaining -= take
                else:
                    pos -= take
                    remaining += take
                if pos == 0:
                    trade["closedAt"] = fill["at"]
                    trade["closedEpoch"] = fill["epoch"]
                    closed.append(_finish_trade(trade))
                    trade = None
    state["position"] = pos
    state["trade"] = trade
    return closed, state


def _plan_scope_matches(plan: Dict[str, Any], account: str) -> bool:
    scopes = {str(value) for value in (plan.get("accountScope") or []) if value not in (None, "")}
    ownership = plan.get("positionOwnership") if isinstance(plan.get("positionOwnership"), dict) else {}
    owner = str(ownership.get("accountId") or ownership.get("account") or "")
    pending = plan.get("pendingOrderOwnership") if isinstance(plan.get("pendingOrderOwnership"), dict) else {}
    scopes.update(str(value) for value in (pending.get("accountScope") or []) if value not in (None, ""))
    if owner:
        scopes.add(owner)
    return not scopes or str(account) in scopes


def plan_lookup(records: List[Dict[str, Any]], account: str,
                symbol: str) -> Tuple[Dict[str, Dict[str, Any]], List[Tuple[float, Dict[str, Any]]]]:
    """凍結プランを **orderId** と記録時刻で引けるようにする。

    台帳には送信した注文の ID が残る(``pendingOrderOwnership.orderIds`` =
    脚ごとのエントリー注文、``brokerOrder.filledOrderIds`` / ``orders[]`` =
    送信直後に見えた注文)。約定の orderId がどれかに当たれば、その往復は
    そのプランのものと確定する。
    """
    by_order: Dict[str, Dict[str, Any]] = {}
    timeline: List[Tuple[float, Dict[str, Any]]] = []
    for record in records or []:
        plan = record.get("plan") if isinstance(record, dict) else None
        if not isinstance(plan, dict) or str(plan.get("symbol") or "") != symbol:
            continue
        if not _plan_scope_matches(plan, account):
            continue
        ids = set()
        # R84: 合成プラン(planKind=PYRAMID_COMPOSITE)の脚 identity は
        # `tranches[].legs[]` にある。ここを歩かないと、追撃の約定は orderId で
        # 引けず ±2pt/30 分の近傍照合へ落ちる —— 追撃は **価格が動いた後に足す**
        # ものなので、その照合はまず当たらず、往復まるごと UNATTRIBUTED
        # (stop なし・R なし・model/grade なし)として記録され続ける。
        for tranche_row in plan.get("tranches") or []:
            if not isinstance(tranche_row, dict):
                continue
            for leg in tranche_row.get("legs") or []:
                if not isinstance(leg, dict):
                    continue
                if leg.get("orderId") not in (None, ""):
                    ids.add(str(leg["orderId"]))
                ids.update(str(value) for value in (leg.get("bracketOrderIds") or [])
                           if value not in (None, ""))
            pair = tranche_row.get("consolidationPair")
            if isinstance(pair, dict):
                ids.update(str(value) for value in (pair.get("orderIds") or [])
                           if value not in (None, ""))
            for row in tranche_row.get("routeSnapshot") or []:
                if isinstance(row, dict) and row.get("orderId") not in (None, ""):
                    ids.add(str(row["orderId"]))
        consolidation = plan.get("consolidation")
        if isinstance(consolidation, dict):
            ids.update(str(value) for value in (consolidation.get("orderIds") or [])
                       if value not in (None, ""))
        pending = plan.get("pendingOrderOwnership") if isinstance(plan.get("pendingOrderOwnership"), dict) else {}
        ids.update(str(value) for value in (pending.get("orderIds") or []) if value not in (None, ""))
        ownership = plan.get("positionOwnership") if isinstance(plan.get("positionOwnership"), dict) else {}
        if ownership.get("orderId") not in (None, ""):
            ids.add(str(ownership["orderId"]))
        broker_order = record.get("brokerOrder") if isinstance(record.get("brokerOrder"), dict) else {}
        ids.update(str(value) for value in (broker_order.get("filledOrderIds") or []) if value not in (None, ""))
        for row in broker_order.get("orders") or []:
            if isinstance(row, dict) and row.get("orderId") not in (None, ""):
                ids.add(str(row["orderId"]))
        for order_id in ids:
            by_order[order_id] = plan
        at = _parse_at(record.get("time"))
        if at is not None:
            timeline.append((at.timestamp(), plan))
    return by_order, timeline


def attribute_plan(trade: Dict[str, Any], by_order: Dict[str, Dict[str, Any]],
                   timeline: List[Tuple[float, Dict[str, Any]]]) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """往復を凍結プランへ帰属させる。orderId → 近傍照合 → 無帰属の順。"""
    for leg in trade.get("opens") or []:
        plan = by_order.get(str(leg.get("orderId") or ""))
        if plan:
            return plan, "order-id"
    want = "SELL" if trade["side"] == "SHORT" else "BUY"
    best: Optional[Tuple[float, Dict[str, Any]]] = None
    for at, plan in timeline:
        if str(plan.get("side") or "").upper() != want:
            continue
        entry = _num(plan.get("entry"))
        if entry is None or abs(entry - float(trade.get("entryRaw", trade["entry"]))) > ATTRIBUTION_PRICE_PT:
            continue
        gap = abs(at - float(trade["openedEpoch"]))
        if gap > ATTRIBUTION_WINDOW_SEC:
            continue
        if best is None or gap < best[0]:
            best = (gap, plan)
    return (best[1], "proximity") if best else (None, None)


def _plan_targets(plan: Dict[str, Any]) -> List[Tuple[str, float]]:
    """凍結プランの目標 ``[(id, price)]``。TP1 約定後の台帳は legs が RUNNER だけに
    なるので、``tp1`` / ``targets`` から TP1 を復元する。"""
    out: List[Tuple[str, float]] = []
    for leg in plan.get("legs") or []:
        if isinstance(leg, dict) and _num(leg.get("target")) is not None:
            out.append((str(leg.get("id") or "LEG").upper(), float(_num(leg["target"]))))
    # R84: 合成プランは **全トランシェの脚**が決済価格の候補。トップレベルの legs は
    # 凍結時点で生きていた脚しか持たないので、それだけでは古いトランシェの TP1 に
    # 名前が付かない(価格は合っているのに "EXIT" と呼ばれる)。
    for tranche_row in plan.get("tranches") or []:
        if not isinstance(tranche_row, dict):
            continue
        tranche_id = str(tranche_row.get("trancheId") or "T")
        for leg in tranche_row.get("legs") or []:
            value = _num((leg or {}).get("target")) if isinstance(leg, dict) else None
            if value is None:
                continue
            label = f"{str(leg.get('id') or 'LEG').upper()}@{tranche_id}"
            if all(abs(value - price) > TOUCH_EPS for _, price in out):
                out.append((label, float(value)))
        pair = tranche_row.get("consolidationPair")
        if isinstance(pair, dict) and _num(pair.get("tp")) is not None:
            value = float(_num(pair["tp"]))
            if all(abs(value - price) > TOUCH_EPS for _, price in out):
                out.append((f"RUNNER@{tranche_id}", value))
    consolidation = plan.get("consolidation")
    if isinstance(consolidation, dict):
        for field, label in (("tp", "RUNNER"), ("sl", "SL")):
            value = _num(consolidation.get(field))
            if value is None:
                continue
            if all(abs(value - price) > TOUCH_EPS for _, price in out):
                out.append((label, float(value)))
    targets = [_num(value) for value in (plan.get("targets") or [])]
    targets = [value for value in targets if value is not None]
    tp1 = _num(plan.get("tp1"))
    final = _num(plan.get("finalTarget"))
    if tp1 is None and targets:
        tp1 = targets[0]
    if final is None and targets:
        final = targets[-1]
    if tp1 is not None and all(abs(tp1 - price) > TOUCH_EPS for _, price in out):
        out.insert(0, ("TP1", float(tp1)))
    if final is not None and all(abs(final - price) > TOUCH_EPS for _, price in out):
        out.append(("RUNNER", float(final)))
    return out


def label_legs(trade: Dict[str, Any], plan: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """決済脚に名前を付ける。価格は約定のまま —— 名前だけをプランで説明する。

    目標に 1tick 以内 → その脚(TP1 / RUNNER)。初期 SL に届いている → SL。
    それ以外は建値に対して不利なら STOP(トレール後 SL・手動損切り)、
    有利なら EXIT(手動利確・建値撤退)。``kind`` は nqx_state が価格から出す。
    """
    direction = 1.0 if trade["side"] == "LONG" else -1.0
    targets = _plan_targets(plan) if plan else []
    stop = _num(plan.get("initialStop")) if plan else None
    # R84: 合成プランの initialStop は **governing**(最新トランシェ)の SL。古い
    # トランシェは自分の、より広い構造 SL を OCO に残したままなので、そちらで
    # 切られた脚も「SL」と読めるように候補へ足す。金額は約定そのものなので変わらず、
    # 名前だけが正しくなる。
    stops = [value for value in [stop] if value is not None]
    if plan:
        for tranche_row in plan.get("tranches") or []:
            value = _num((tranche_row or {}).get("initialStop")) if isinstance(tranche_row, dict) else None
            if value is not None and all(abs(value - known) > TOUCH_EPS for known in stops):
                stops.append(float(value))
        consolidation = plan.get("consolidation")
        if isinstance(consolidation, dict) and _num(consolidation.get("sl")) is not None:
            value = float(_num(consolidation["sl"]))
            if all(abs(value - known) > TOUCH_EPS for known in stops):
                stops.append(value)
    out: List[Dict[str, Any]] = []
    for leg in trade.get("legs") or []:
        price = float(leg["price"])
        hit = next(((leg_id, target) for leg_id, target in targets
                    if abs(price - target) <= TICK + TOUCH_EPS), None)
        if hit:
            out.append({"id": hit[0], "qty": int(leg["qty"]), "exit": price, "target": hit[1]})
            continue
        if any((price - value) * direction <= TICK + TOUCH_EPS for value in stops):
            out.append({"id": "SL", "qty": int(leg["qty"]), "exit": price})
            continue
        adverse = (price - float(trade["entry"])) * direction < 0
        out.append({"id": "STOP" if adverse else "EXIT", "qty": int(leg["qty"]), "exit": price})
    return out


def build_trade_result(trade: Dict[str, Any], plan: Optional[Dict[str, Any]], *,
                       account: str, mode: str = "SIMULATION",
                       bars: Optional[List[Dict[str, Any]]] = None,
                       reported_net: Optional[float] = None,
                       fees: Optional[float] = None,
                       fees_detail: Optional[str] = None,
                       builder: Optional[Callable[..., Tuple[Dict[str, Any], Any]]] = None,
                       ) -> Tuple[Optional[Dict[str, Any]], str]:
    """往復 1 件を result にする。stop は凍結プランがあるときだけ(無帰属は R 無し)。"""
    import nqx_state

    builder = builder or nqx_state.result_from_report
    legs = label_legs(trade, plan)
    if not legs:
        return None, "no closing fills"
    stop = _num(plan.get("initialStop")) if plan else None
    kwargs = dict(
        side=trade["side"], entry=float(trade["entry"]), stop=stop, qty=int(trade["qty"]),
        opened_at=trade["openedAt"], closed_at=trade["closedAt"],
        symbol=trade.get("symbol") or "MNQU6", bars=bars, mode=mode,
        legs=legs, exit_source="broker",
        scenario_id=(plan or {}).get("scenarioId") or None,
        account_id=account,
        model=(plan or {}).get("model") or None,
        grade=(plan or {}).get("grade") or None,
    )
    # R54: 手数料は「配分済みの額(fees)」か「純額(reported_net)」のどちらか一方。
    # 両方渡すと nqx_state が拒む。
    money = {"fees": fees} if fees is not None else {"reported_net": reported_net}
    try:
        result, derivation = builder(**kwargs, **money)
        if fees is not None and fees_detail:
            derivation = f"{derivation or ''} / {fees_detail}".strip(" /")
    except (ValueError, TypeError) as exc:
        if fees is None and reported_net is None:
            return None, f"could not build result: {exc}"
        try:
            result, derivation = builder(**kwargs)
        except (ValueError, TypeError) as exc2:
            return None, f"could not build result: {exc2}"
        derivation = f"{derivation or ''} / broker net not reconciled: {exc}".strip(" /")
    # 価格の出所。Worker は白リスト外の項目を落とすので画面には出ないが、
    # ローカル台帳で「約定由来か脚推定か」を後から辿れる。
    result["exitBasis"] = "broker-fills"
    # R84 §8: 追撃の内訳。**記録専用**で、採点の重み付けには使わない
    # (PYRAMID_ADD タグも R48 と同じ扱い。N が貯まるまでエッジを語らない)。
    if isinstance(plan, dict) and str(plan.get("planKind") or "") == "PYRAMID_COMPOSITE":
        result["pyramid"] = {
            "addsDone": int(((plan.get("pyramid") or {}).get("addsDone")) or 0),
            "tranches": [
                {"trancheId": str(row.get("trancheId") or ""),
                 "entry": _num(row.get("entry")),
                 "qty": sum(int((leg or {}).get("qty") or 0)
                            for leg in row.get("legs") or [] if isinstance(leg, dict))}
                for row in plan.get("tranches") or [] if isinstance(row, dict)],
        }
    # R57: 根拠チャート(建玉前後の確定 3 分足・VP 水準・凍結ターゲット・根拠タグ)。
    # 表示専用。付けられなければ result はそのまま(画面は path 描画に落ちる)。
    import result_context
    result_context.attach(result, plan)
    return result, (derivation or "recorded from broker fills")


#: 約定列の未決済を「fills では二度と埋まらない」と見なすまでの経過時間(秒)。
#: CrossTrade の `/fills` は当取引日しか返さないので、1 日跨げば取り込めない。
FILLS_WINDOW_SEC = 24 * 3600


def _instant(value: Any) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _fills_window_closed(next_state: Dict[str, Any], broker_signed: int,
                         observed_at: Any) -> Optional[Dict[str, Any]]:
    """R53: 進行中の往復が **過去の取引日** に閉じられ、fills では埋まらないか。

    CrossTrade の `/fills` は当取引日しか返さない。決済の約定を取り込む前に日付が
    変わると約定列は永久に「まだ建っている」ままになり、
    ``fills say -7 but broker says +0`` が毎サイクル出て **以後どの往復も記録され
    ない**(2026-09-04 の runner 決済で発生。決済自体は LEDGER に載っていたのに、
    ローカルの計数だけが取り残されて記録経路ごと止まっていた)。

    ブローカーが verified FLAT で、進行中の往復が 24 時間以上前に開いているときだけ
    その往復を返す。**決済価格は推測しない** —— 呼び出し側は記録を作らず、
    未突合として台帳へ退避するだけにすること。
    """
    if int(broker_signed) != 0 or int(next_state.get("position") or 0) == 0:
        return None
    trade = next_state.get("trade")
    if not isinstance(trade, dict):
        return None
    opened, observed = _instant(trade.get("openedAt")), _instant(observed_at)
    if opened is None or observed is None:
        return None
    if (observed - opened).total_seconds() < FILLS_WINDOW_SEC:
        return None
    return trade


def _signed_position(position: Dict[str, Any]) -> Optional[int]:
    """建玉照会を符号付き枚数へ。向きが読めない非ゼロ建玉は None(突合不能)。"""
    qty = int(_num(position.get("qty")) or 0)
    if qty <= 0:
        return 0
    side = _side_word(position.get("side"))
    if side == "LONG":
        return qty
    if side == "SHORT":
        return -qty
    return None


def reconcile_fills(bundle: Dict[str, Any], *, accounts: List[str],
                    fills_query: Callable[..., Dict[str, Any]],
                    position_query: Callable[..., Dict[str, Any]],
                    balance_query: Callable[..., Dict[str, Any]],
                    records: List[Dict[str, Any]], journal: Dict[str, Any],
                    mode: str = "SIMULATION", now: Optional[datetime] = None,
                    publisher: Optional[Callable[..., Tuple[bool, Any]]] = None,
                    builder: Optional[Callable[..., Tuple[Dict[str, Any], Any]]] = None,
                    symbol: str = "MNQU6",
                    fee_rates: Optional[Dict[str, float]] = None) -> List[str]:
    """約定列を口座ごとに読み、閉じた往復を LEDGER へ publish する。

    口座ごとの状態は ``journal["fills"][account]``:
      ``seen``     処理済み約定 ID(取り直しても二度数えない)
      ``position`` 約定列から出た現在の符号付き枚数
      ``trade``    進行中の往復(建値の材料)
      ``mark``     往復が開いたときの realizedPnL(決済時の差分 = ブローカーの純額)
      ``pending``  publish に失敗した result(次周期で再送)
    約定列の建玉とブローカーの建玉照会が一致しないサイクルは、状態を進めない。
    """
    notes: List[str] = []
    stamp = _iso(now)
    bars = confirmed_bars(bundle)
    path_bars = [{"t": bar["t"], "c": bar["c"]} for bar in bars]
    fills_state = journal.setdefault("fills", {})
    if not isinstance(fills_state, dict):
        fills_state = journal["fills"] = {}

    for account in accounts:
        try:
            snapshot = fills_query(account=account) or {}
        except Exception as exc:                       # noqa: BLE001 — 記録は監視を止めない
            notes.append(f"trade journal: fills query failed ({type(exc).__name__}) — not recording")
            continue
        if snapshot.get("verified") is not True:
            notes.append(f"trade journal: fills unavailable — {str(snapshot.get('detail') or 'unverified')[:120]}")
            continue
        try:
            position = position_query(symbol, account=account) or {}
        except Exception as exc:                       # noqa: BLE001
            notes.append(f"trade journal: position query failed ({type(exc).__name__}) — fills held")
            continue
        if position.get("verified") is not True:
            notes.append("trade journal: position unverified — fills held this cycle")
            continue
        broker_signed = _signed_position(position)
        if broker_signed is None:
            notes.append("trade journal: position side unreadable — fills held this cycle")
            continue
        try:
            balance = balance_query(account=account) or {}
        except Exception:                              # noqa: BLE001 — 金額突合は任意
            balance = {}
        realized_now = _num(balance.get("realizedPnL")) if balance.get("verified") is True else None
        day_now = str(balance.get("observedAt") or "")[:10] or None
        sod_now = _num(balance.get("netLiqSOD")) if balance.get("verified") is True else None

        state = fills_state.get(account) if isinstance(fills_state.get(account), dict) else {}
        seen = {str(value) for value in (state.get("seen") or [])}
        fresh = [fill for fill in normalize_fills(snapshot.get("fills"), symbol) if fill["id"] not in seen]
        closed, next_state = round_trips(
            fresh, {"position": int(state.get("position") or 0), "trade": state.get("trade")})

        unreconciled = [row for row in (state.get("unreconciled") or []) if isinstance(row, dict)]
        if int(next_state["position"]) != int(broker_signed):
            stranded = _fills_window_closed(next_state, broker_signed, position.get("observedAt"))
            if stranded is None:
                notes.append(f"trade journal: fills say {int(next_state['position']):+d} but broker says "
                             f"{int(broker_signed):+d} — holding this cycle (no record written)")
                continue
            # R53: 決済の約定が取引日を跨いで取り込めなかった往復。ここで待ち続けると
            # 以後の往復も一件も記録されないので、**推測で決済価格を作らずに**退避して
            # 計数だけブローカーの真実へ戻す。人が `/result` で入れる余地は残る。
            unreconciled = (unreconciled + [{
                "trade": stranded, "account": account, "at": stamp,
                "reason": "FILLS_WINDOW_CLOSED",
                "fillsPosition": int(next_state["position"]),
                "brokerPosition": int(broker_signed)}])[-20:]
            notes.append(f"trade journal: {stranded.get('side')} "
                         f"{abs(int(next_state['position']))} 枚の決済約定を取引日跨ぎで取り込めず "
                         f"未突合へ退避 — 記録は作らない(`/result 価格` で入れられます)")
            next_state = {"position": int(broker_signed), "trade": None}

        by_order, timeline = plan_lookup(records, account, symbol)
        flat = state.get("flat") if isinstance(state.get("flat"), dict) else None
        pending: List[Dict[str, Any]] = [row for row in (state.get("pending") or []) if isinstance(row, dict)]
        # R85: 単価が設定されている口座は「約定枚数 × 単価」。残高照会のタイミングに
        # 依存しないので、これが最優先。無い口座だけ R54(realizedPnL 差分)へ倒れる。
        rate = (fee_rates or {}).get(account)
        if rate is not None:
            fee_shares = [contract_fees(trade, rate) for trade in closed]
            fee_total = round(sum(value for value in fee_shares if value is not None), 2)
        else:
            # R54: フラット基準からの realizedPnL 差分で手数料の総額を出し、枚数で配分する。
            fee_shares, fee_total = allocate_fees(closed, flat, realized_now, day_now,
                                                  sod_now=sod_now)
            if closed and fee_shares is None:
                notes.append("trade journal: 手数料を確定できず手数料なしで記録 "
                             f"({FEE_RATE_KEY}_<口座ID> を設定すると約定枚数 × 単価で入ります)")
        total_qty = sum(int(trade["qty"]) for trade in closed) or 1
        for index, trade in enumerate(closed):
            trade["symbol"] = symbol
            trade["account"] = account
            plan, how = attribute_plan(trade, by_order, timeline)
            fees = fee_shares[index] if fee_shares else None
            fees_detail = None
            if fees is not None and rate is not None:
                fees_detail = (f"fees = 約定 {contracts_traded(trade)} 枚 × ${float(rate):.2f}"
                               f"({FEE_RATE_KEY}) = {fees:.2f}")
            elif fees is not None:
                fees_detail = (f"fees をブローカー実現損益から配分: 合計 {fee_total:.2f} × "
                               f"{int(trade['qty'])}/{total_qty} = {fees:.2f}")
            result, detail = build_trade_result(trade, plan, account=account, mode=mode,
                                                bars=path_bars, fees=fees,
                                                fees_detail=fees_detail, builder=builder)
            if result is None:
                journal.setdefault("unresolved", []).append(
                    {"account": account, "trade": trade, "reason": detail, "at": stamp})
                notes.append(f"trade journal: NOT recorded — {detail}")
                continue
            pending.append({"result": result, "detail": detail, "how": how or "unattributed",
                            "side": trade["side"], "qty": trade["qty"], "entry": trade["entry"]})

        still_pending: List[Dict[str, Any]] = []
        for row in pending:
            result = row.get("result") or {}
            result_id = result.get("resultId")
            if result_id and result_id in journal["published"]:
                continue
            ok, why = _publish(result, publisher)
            if not ok:
                row["lastError"] = str(why)[:160]
                still_pending.append(row)
                notes.append(f"trade journal: publish failed — {str(why)[:120]} (will retry)")
                continue
            journal["published"] = (journal["published"] + [result_id])[-200:]
            notes.append(f"trade journal: recorded {row.get('side')} {row.get('qty')} "
                         f"@{float(row.get('entry') or 0):,.2f} → LEDGER "
                         f"({row.get('how')} · {row.get('detail')})")
            _scorecard(result, notes, bars=bars)

        # R54: 手数料の基準は「建玉ゼロを観測したときの realizedPnL」。建玉が
        # 残っている間は動かさない(次の決済までの差分がその往復の純額になる)。
        # 建玉が開いた後に取り直すと、エントリー側の手数料が基準に含まれてしまい
        # 差分が決済側だけになる —— 2026-09-07 に手数料が半分しか載らなかった原因。
        if int(next_state.get("position") or 0) != 0 or isinstance(next_state.get("trade"), dict):
            if flat:
                next_state["flat"] = flat
        elif realized_now is not None:
            next_state["flat"] = {"realized": realized_now, "day": day_now, "sod": sod_now,
                                  "at": stamp}
        elif flat:
            next_state["flat"] = flat
        next_state["pending"] = still_pending[-20:]
        if unreconciled:
            next_state["unreconciled"] = unreconciled
        next_state["seen"] = (list(state.get("seen") or []) + [fill["id"] for fill in fresh])[-FILL_SEEN_LIMIT:]
        next_state["lastSyncAt"] = stamp
        next_state["source"] = str(snapshot.get("source") or "")
        fills_state[account] = next_state
    return notes


def _publish(result: Dict[str, Any],
             publisher: Optional[Callable[..., Tuple[bool, Any]]]) -> Tuple[bool, Any]:
    if publisher is None:
        import nqx_state
        publisher = nqx_state.publish_result
    try:
        return publisher(result)
    except Exception as exc:                           # noqa: BLE001 — 送信失敗は保留へ
        return False, f"{type(exc).__name__}: {exc}"


def _scorecard(result: Dict[str, Any], notes: List[str],
               bars: Optional[List[Dict[str, float]]] = None) -> None:
    """R48: モデル別スコアカードへも追記する(表示・台帳のみ)。失敗しても巻き込まない。

    R103-0: 刈られ方の計測に使う確定 3 分足を渡す(ここに既にあるもの。取り直さない)。
    """
    try:
        import model_scorecard
        if model_scorecard.record(result, bars=bars):
            notes.append(model_scorecard.summary_line(
                model_scorecard.summarize(model_scorecard.load_rows())))
    except Exception:                                  # noqa: BLE001
        pass


# ---------------------------------------------------------------- 入口

def reconcile(bundle: Dict[str, Any], *, accounts: List[str],
              position_query: Optional[Callable[..., Dict[str, Any]]] = None,
              balance_query: Optional[Callable[..., Dict[str, Any]]] = None,
              fills_query: Any = _BROKER,
              journal_path: str = JOURNAL_FILE, ledger_path: str = LEDGER_FILE,
              mode: str = "SIMULATION", now: Optional[datetime] = None,
              publisher: Optional[Callable[..., Tuple[bool, Any]]] = None,
              fee_rates: Optional[Dict[str, float]] = None,
              ) -> List[str]:
    """3分ループから呼ばれる入口。**読むだけ / 発注しない。**

    口座ごとに約定・建玉・残高を照会し、閉じた往復を LEDGER へ publish する。
    約定が取れる口座は約定経路(R56)、取れない口座だけ旧経路(脚 + 到達)。
    どこで失敗しても監視サイクルは止めない(記録の失敗で発注経路を巻き込まない)。

    ``fills_query`` を省略すると broker_status を使う。ただし ``position_query`` が
    注入されている(テスト・オフライン)ときは、約定だけを勝手にブローカーへ
    取りに行かない —— 注入されていなければ旧経路で動く。
    """
    import autotrade_engine
    import broker_status

    if not accounts:
        return []
    injected = position_query is not None
    position_query = position_query or broker_status.query_position
    balance_query = balance_query or broker_status.query_balance
    if fills_query is _BROKER:
        fills_query = None if injected else broker_status.query_fills
    symbol = str(bundle.get("sourceSymbol") or bundle.get("symbol") or "MNQU6")
    if "MNQ" in symbol and not symbol.startswith("MNQ"):
        symbol = "MNQU6"

    records, ledger_error = autotrade_engine._read_ledger(ledger_path)
    if ledger_error:
        return [f"trade journal: ledger unreadable ({ledger_error}) — not recording"]

    journal = load_journal(journal_path)
    notes: List[str] = []
    if fills_query is not None:
        notes += reconcile_fills(bundle, accounts=accounts, fills_query=fills_query,
                                 position_query=position_query, balance_query=balance_query,
                                 records=records, journal=journal, mode=mode, now=now,
                                 publisher=publisher, symbol=symbol, fee_rates=fee_rates)
        # 約定経路が一度でも成立した口座は旧経路を使わない(二重記録を防ぐ)。
        fills_state = journal.get("fills") if isinstance(journal.get("fills"), dict) else {}
        legacy_accounts = [account for account in accounts
                           if not isinstance(fills_state.get(account), dict)]
    else:
        legacy_accounts = list(accounts)

    if not legacy_accounts:
        try:
            save_journal(journal, journal_path)
        except OSError as exc:
            notes.append(f"trade journal: could not save state ({exc})")
        return notes

    positions: Dict[str, Dict[str, Any]] = {}
    balances: Dict[str, Dict[str, Any]] = {}
    plans: Dict[str, Dict[str, Any]] = {}
    for account in legacy_accounts:
        try:
            positions[account] = position_query(symbol, account=account) or {}
        except Exception as exc:                       # noqa: BLE001 — 記録は止めても止まらない
            return notes + [f"trade journal: position query failed ({type(exc).__name__}) — not recording"]
        try:
            balances[account] = balance_query(account=account) or {}
        except Exception:                              # noqa: BLE001 — 金額突合は任意
            balances[account] = {}
        plan = autotrade_engine._frozen_plan(records, symbol, account=account)
        if isinstance(plan, dict):
            plans[account] = plan

    journal, closed, legacy_notes = observe(bundle, positions, plans=plans, records=records,
                                            balances=balances, journal=journal, now=now)
    notes += legacy_notes

    bars = confirmed_bars(bundle)
    for state in closed:
        ok, detail, result = build_and_publish(
            state, mode=mode,
            bars=[{"t": b["t"], "c": b["c"]} for b in bars],
            publisher=publisher)
        result_id = (result or {}).get("resultId")
        if ok and result_id and result_id in journal["published"]:
            notes.append(f"trade journal: {result_id} already recorded — skipped")
            continue
        if ok:
            journal["published"] = (journal["published"] + [result_id])[-200:]
            notes.append(f"trade journal: recorded {state['side']} "
                         f"{state.get('initialQty')} → LEDGER ({detail})")
            # R48: モデル別スコアカードへも追記する(表示・台帳のみ)。
            # 失敗しても決済記録は成立している — 巻き込まない。
            _scorecard(result, notes, bars=bars)
        else:
            journal.setdefault("unresolved", []).append(state)
            notes.append(f"trade journal: NOT recorded — {detail}")

    try:
        save_journal(journal, journal_path)
    except OSError as exc:
        notes.append(f"trade journal: could not save state ({exc})")
    return notes


# ---------------------------------------------------------------- CLI(遡及記録)

USAGE = """usage: python trade_journal.py --backfill --account <ID> [--publish] [--unscoped] [--mode LIVE|SIMULATION]

約定履歴(GET /accounts/{ID}/fills)から往復を組み立てて一覧を出す。既定は dry-run。
  --publish   LEDGER(Durable Object の result ストリーム)へ送る。送った resultId は台帳へ残す
  --republish 送信済み(already)も送り直す。resultId が同じなので LEDGER 側は置き換え(R57 の根拠チャート付与用)
  --unscoped  CROSSTRADE_ACCOUNTS の外の(封印済み)口座を読む。読むだけ / 発注しない
  --mode      記録の mode。省略時は NQX_LIVE_ORDERS の設定から決める"""


def main(argv: Optional[List[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if "--backfill" not in args or "--account" not in args:
        print(USAGE)
        return 2
    index = args.index("--account") + 1
    account = args[index] if index < len(args) else ""
    if not account or account.startswith("--"):
        print(USAGE)
        return 2
    publish = "--publish" in args
    republish = "--republish" in args
    unscoped = "--unscoped" in args
    mode = None
    if "--mode" in args and args.index("--mode") + 1 < len(args):
        mode = str(args[args.index("--mode") + 1]).upper()

    import autotrade_engine
    import broker_status
    import nqx_state

    if mode not in ("LIVE", "SIMULATION"):
        try:
            cfg = {**nqx_state._read_kv_env(nqx_state.CROSSTRADE_ENV), **os.environ}
            mode = "LIVE" if autotrade_engine.live_enabled(cfg) else "SIMULATION"
        except Exception:                              # noqa: BLE001 — 表示上のモードだけ
            mode = "SIMULATION"

    snapshot = broker_status.query_fills(account=account, scoped=not unscoped)
    if snapshot.get("verified") is not True:
        print(f"fills unavailable: {snapshot.get('detail')}")
        return 1
    fills = normalize_fills(snapshot.get("fills"), "MNQU6")
    closed, state = round_trips(fills)
    try:
        rate = fee_rates_from_env(nqx_state._read_kv_env(nqx_state.CROSSTRADE_ENV),
                                  [account]).get(account)
    except Exception:                              # noqa: BLE001 — 手数料は任意
        rate = None
    records, error = autotrade_engine._read_ledger(LEDGER_FILE)
    by_order, timeline = plan_lookup(records or [], account, "MNQU6")
    journal = load_journal()
    print(f"{account}: {len(fills)} fills -> {len(closed)} closed round-trips ({mode})"
          + (f" · ledger unreadable ({error})" if error else ""))

    sent = 0
    for trade in closed:
        trade["symbol"] = "MNQU6"
        trade["account"] = account
        plan, how = attribute_plan(trade, by_order, timeline)
        # R85: 遡及でも手数料は約定枚数 × 単価(未設定なら手数料なし = 従来どおり)。
        fees = contract_fees(trade, rate) if rate is not None else None
        result, detail = build_trade_result(
            trade, plan, account=account, mode=mode, fees=fees,
            fees_detail=(f"fees = 約定 {contracts_traded(trade)} 枚 × ${float(rate):.2f}"
                         if fees is not None else None))
        direction = 1.0 if trade["side"] == "LONG" else -1.0
        gross = sum((float(leg["price"]) - float(trade["entry"])) * direction * 2.0 * int(leg["qty"])
                    for leg in trade["legs"])
        result_id = (result or {}).get("resultId") or "-"
        status = ("already" if result_id in journal["published"]
                  else ("build-failed" if result is None else "new"))
        print(f"  {trade['openedAt'][:19]} -> {trade['closedAt'][:19]}  {trade['side']:5} "
              f"{trade['qty']:>3} @{trade['entry']:,.2f}  legs {len(trade['legs'])}  "
              f"gross {gross:+,.2f}  fees {'-' if fees is None else f'{fees:.2f}'}  "
              f"{how or 'unattributed':12} {result_id} {status}"
              + (f"  ({detail})" if result is None else "")
              + (f"  chart={len((result.get('chart') or {}).get('bars') or [])} bars" if result else ""))
        # R57: --republish は「already」も送り直す。resultId が同じなので DO の resultLog は
        # 置き換え(二重掲載にならない)。根拠チャートを後から付けるために使う。
        if publish and result is not None and (status == "new" or (republish and status == "already")):
            ok, why = nqx_state.publish_result(result)
            if ok:
                journal["published"] = (journal["published"] + [result_id])[-200:]
                sent += 1
                _scorecard(result, [])
                print("     published")
            else:
                print(f"     publish failed: {str(why)[:160]}")
    open_trade = state.get("trade")
    if isinstance(open_trade, dict):
        qty = sum(int(leg["qty"]) for leg in open_trade["opens"])
        print(f"  open: {open_trade['side']} {qty} since {open_trade['openedAt'][:19]} "
              "(recorded when it closes)")
    if publish:
        save_journal(journal)
        print(f"published {sent}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

# -*- coding: utf-8 -*-
"""R118: ブローカー観測の **出所** を一本化する層。

R117 で Gateway(WebSocket)は単体で動くところまで作ったが、誰も使っていなかった。
既存の消費者(engine / 表示 / 会計 / ULTRA サイジング)はそれぞれ
`broker_status.query_position_cached` などを呼んでおり、その先は必ず REST だった。

ここはその **1 段下** に入り、同じ戻り値の形のまま出所だけを切り替える。
消費者側のコードは 1 行も変わらない。

三つの状態:

``OFF``
    従来どおり REST だけ。**既定。**
``SHADOW``
    答えは REST。裏で Gateway も読み、食い違いを `.secrets/gateway_divergence.jsonl`
    へ記録する。**判断は一切変わらない**ので、本番で安全に回して差分だけ集められる。
``LIVE``
    答えは Gateway。口座ごとに `VERIFIED` かつ鮮度内でなければ **その口座だけ**
    REST へ落ちる(全体を落とさない)。落ちた回数は数える。

この層が **触らない** もの(意図的に REST のまま):

* `broker_status.query_position` / `query_orders` の生呼び出し —— `order.py` の
  送信前後の検証がこれを使う。押し込みのストリームを「送った証拠」に使わない。
* `broker_status.live_reads()` の中 —— 二度読みが証明になる経路(不在証明の三点読み、
  R52 の一瞬 FLAT 再確認)。同じスナップショットを二度読んでも証明にならない。
* 残高 —— WebSocket に残高は流れてこない。REST のまま(1 口座 1 本/サイクル)。
* `known_order_ids` 付きの注文照会 —— 特定 ID の終端状態は一覧では証明できない。
  「一覧に無い」と「ブローカーが解決できない」は意味が違うので混ぜない。

根拠と実測は `docs/R118_GATEWAY_INTEGRATION.md`。
"""
from __future__ import annotations

import io
import json
import os
import re
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

import broker_gateway as gw

BASE = os.path.dirname(os.path.abspath(__file__))
SECRETS = os.path.join(BASE, ".secrets")
DIVERGENCE_LOG = os.path.join(SECRETS, "gateway_divergence.jsonl")

OFF = "OFF"
SHADOW = "SHADOW"
LIVE = "LIVE"
MODES = (OFF, SHADOW, LIVE)

#: Gateway スナップショットの鮮度。契約 `brokerObservation.maxAgeSec` に合わせる。
#: **性能対策としてここを緩めない**(指示 §0)。
DEFAULT_MAX_AGE_SEC = 30.0


# --------------------------------------------------------------------- 設定

#: 契約ファイルの写し。**mtime が変われば読み直す**ので、運用中に契約を書き換えても
#: 次の読みで効く(プロセスを落とさなくてよい)。写しが要るのは、`mode()` が
#: 口座ごと・照会ごとに呼ばれるため —— 20 口座なら 1 回の見回りで 40 回以上になり、
#: そのたびに 20KB の JSON を parse していた(観測 1 回 60〜107ms の一因)。
_CONTRACT_CACHE: Dict[str, Any] = {"mtime": None, "size": None, "value": None}
_CONTRACT_LOCK = threading.Lock()
CONTRACT_PATH = os.path.join(BASE, os.environ.get("NQX_EXECUTION_CONTRACT", "execution_contract.json"))  # 🩹 Nerf Edition: NQX_EXECUTION_CONTRACT で差し替え可


def _contract_file() -> Dict[str, Any]:
    try:
        stat = os.stat(CONTRACT_PATH)
        key = (stat.st_mtime_ns, stat.st_size)
    except OSError:
        return {}
    with _CONTRACT_LOCK:
        if _CONTRACT_CACHE["mtime"] == key[0] and _CONTRACT_CACHE["size"] == key[1]:
            cached = _CONTRACT_CACHE["value"]
            if cached is not None:
                return cached
    try:
        with io.open(CONTRACT_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:  # noqa: BLE001 — 契約が読めないなら OFF に倒す
        return {}
    if not isinstance(data, dict):
        return {}
    with _CONTRACT_LOCK:
        _CONTRACT_CACHE.update({"mtime": key[0], "size": key[1], "value": data})
    return data


def _contract() -> Dict[str, Any]:
    section = _contract_file().get("gateway")
    return section if isinstance(section, dict) else {}


def mode() -> str:
    """出所の状態。環境変数が契約より強い(緊急で止められるように)。"""
    override = str(os.environ.get("NQX_GATEWAY_MODE") or "").strip().upper()
    if override in MODES:
        return override
    configured = str(_contract().get("mode") or OFF).strip().upper()
    return configured if configured in MODES else OFF


def max_age_sec() -> float:
    try:
        value = float((_contract_file().get("brokerObservation") or {}).get("maxAgeSec"))
        return value if value > 0 else DEFAULT_MAX_AGE_SEC
    except (TypeError, ValueError):
        return DEFAULT_MAX_AGE_SEC


# ------------------------------------------------------------------ 統計

_STATS_LOCK = threading.Lock()
#: `shadowChecked` = 影運転で比較を試みた回数 / `shadowCompared` = **両方の答えが
#: 揃って実際に比べられた**回数 / `shadowUnavailable` = Gateway 側が無くて比べられ
#: なかった回数。この 3 つを分けないと、**Gateway が止まっているだけの日**も差分ログが
#: 空になり、導入手順 §8 段 2 の「差分が出ないこと」を通してしまう(R118 の穴)。
#: `positionsSeen` = その周期に **枚数のある建玉**を観測した回数。FLAT どうしの
#: 一致(qty=0 vs qty=0)は影運転の証拠として弱い —— 建玉があった周期を 1 つも
#: 含まないまま段 3 へ進めないように数える。
_STATS = {"gateway": 0, "rest": 0, "fallback": 0,
          "shadowChecked": 0, "shadowCompared": 0, "shadowDiverged": 0,
          "shadowUnavailable": 0, "positionsSeen": 0,
          "fallbackReasons": {}, "shadowUnavailableReasons": {}}


def stats() -> Dict[str, Any]:
    with _STATS_LOCK:
        return {**_STATS,
                "fallbackReasons": dict(_STATS["fallbackReasons"]),
                "shadowUnavailableReasons": dict(_STATS["shadowUnavailableReasons"]),
                "mode": mode()}


def reset_stats() -> None:
    with _STATS_LOCK:
        _STATS.update({"gateway": 0, "rest": 0, "fallback": 0,
                       "shadowChecked": 0, "shadowCompared": 0, "shadowDiverged": 0,
                       "shadowUnavailable": 0, "positionsSeen": 0})
        _STATS["fallbackReasons"] = {}
        _STATS["shadowUnavailableReasons"] = {}


def _count(key: str, reason: Optional[str] = None,
           bucket: str = "fallbackReasons") -> None:
    with _STATS_LOCK:
        _STATS[key] = _STATS.get(key, 0) + 1
        if reason:
            target = _STATS.setdefault(bucket, {})
            target[reason] = target.get(reason, 0) + 1


# ------------------------------------------------------- スナップショットの読み

_SNAPSHOT_CACHE: Dict[str, Any] = {"at": 0.0, "value": None, "path": None}
_SNAPSHOT_LOCK = threading.Lock()

#: スナップショットファイルを読み直す間隔。ファイル I/O を毎回やらないためだけの値で、
#: **鮮度の判定には使わない**(鮮度は必ずスナップショット自身の `writtenAt` で見る)。
_SNAPSHOT_REREAD_SEC = 0.25


def _snapshot(path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    target = path or gw.snapshot_path()
    now = time.monotonic()
    with _SNAPSHOT_LOCK:
        if (_SNAPSHOT_CACHE["path"] == target
                and _SNAPSHOT_CACHE["value"] is not None
                and now - _SNAPSHOT_CACHE["at"] < _SNAPSHOT_REREAD_SEC):
            return _SNAPSHOT_CACHE["value"]
    value = gw.read_snapshot(target)
    with _SNAPSHOT_LOCK:
        _SNAPSHOT_CACHE.update({"at": now, "value": value, "path": target})
    return value


def invalidate_snapshot() -> None:
    """次の読みでファイルを取り直す。送信の前後で必ず呼ぶ。"""
    with _SNAPSHOT_LOCK:
        _SNAPSHOT_CACHE.update({"at": 0.0, "value": None})


def _fresh_account(snapshot: Optional[Dict[str, Any]], account: str,
                   now_wall: Optional[float] = None) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """使ってよい口座行を返す。使えないときは (None, 理由)。"""
    if not isinstance(snapshot, dict):
        return None, "NO_SNAPSHOT"
    if snapshot.get("schema") != gw.SCHEMA:
        return None, "SCHEMA"
    if snapshot.get("integrity") != gw.VERIFIED:
        return None, "INTEGRITY_" + str(snapshot.get("integrity") or "UNKNOWN")
    written = snapshot.get("writtenAt")
    try:
        age = float(now_wall if now_wall is not None else time.time()) - float(written)
    except (TypeError, ValueError):
        return None, "NO_WRITTEN_AT"
    if age < 0 or age > max_age_sec():
        return None, "STALE"
    rows = snapshot.get("accounts") or {}
    row = None
    for alias in account_aliases(account):
        candidate = rows.get(str(alias))
        if isinstance(candidate, dict):
            row = candidate
            break
    if not isinstance(row, dict):
        return None, "ACCOUNT_NOT_OBSERVED"
    if row.get("integrity") != gw.VERIFIED:
        return None, "ACCOUNT_" + str(row.get("integrity") or "UNKNOWN")
    return row, None


# ------------------------------------------------------------- 形の変換

_ENV_CACHE: Dict[str, Any] = {"mtime": None, "size": None, "value": None}


def _env() -> Dict[str, str]:
    """`.secrets/crosstrade.env` の写し。`_contract_file` と同じ理由(mtime で更新)。"""
    import broker_status as bs
    try:
        stat = os.stat(bs.CROSSTRADE_ENV)
        key = (stat.st_mtime_ns, stat.st_size)
    except OSError:
        return bs.read_env(bs.CROSSTRADE_ENV)
    with _CONTRACT_LOCK:
        if (_ENV_CACHE["mtime"] == key[0] and _ENV_CACHE["size"] == key[1]
                and _ENV_CACHE["value"] is not None):
            return _ENV_CACHE["value"]
    value = bs.read_env(bs.CROSSTRADE_ENV)
    with _CONTRACT_LOCK:
        _ENV_CACHE.update({"mtime": key[0], "size": key[1], "value": value})
    return value


def _contract_ids(cfg: Optional[Dict[str, str]] = None, symbol: Optional[str] = None) -> List[str]:
    import broker_status as bs
    cfg = cfg if cfg is not None else _env()
    import re
    key = "CROSSTRADE_CONTRACT_ID_" + re.sub(r"[^A-Za-z0-9]", "_", str(symbol or bs.DEFAULT_SYMBOL))
    return [str(value) for value in (cfg.get(key), cfg.get("CROSSTRADE_CONTRACT_ID"))
            if value not in (None, "")]


def account_aliases(account: str, cfg: Optional[Dict[str, str]] = None) -> List[str]:
    """口座の別名。**設定の口座名と、ブローカー側の accountId は別物。**

    REST は URL に **口座名**を使い(`/accounts/<名前>/position`)、行の identity 照合に
    `CROSSTRADE_ACCOUNT_ID_<名前>` を別名として使う。一方 WebSocket の行は
    **accountId しか持たない**ので、Gateway の状態は accountId で鍵付けされる。

    ここを繋がないと、消費者が口座名で引いた瞬間に `ACCOUNT_NOT_OBSERVED` になって
    全部 REST へ落ちる(2026-09-19、実プロセスで発覚。名前と ID が同じだった
    模擬では出なかった)。
    """
    import broker_status as bs
    cfg = cfg if cfg is not None else _env()
    name = str(account)
    scoped = cfg.get("CROSSTRADE_ACCOUNT_ID_" + re.sub(r"[^A-Za-z0-9]", "_", name))
    out = [name]
    for value in (scoped, cfg.get("CROSSTRADE_ACCOUNT_ID")):
        if value not in (None, "") and str(value) not in out:
            out.append(str(value))
    return out


def position_from_snapshot(account: str, symbol: str,
                           snapshot: Optional[Dict[str, Any]] = None,
                           contract_ids: Optional[List[str]] = None,
                           now_wall: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """Gateway の行を `broker_status.query_position` と同じ形にする。

    **FLAT を名乗る条件は REST より厳しくない。** R41/R42 と同じ規律で、
    その口座に **別 contractId の非ゼロ建玉**が 1 行でもあれば FLAT と言わない。
    Gateway が持っているのは `Tv_ListPositions`(一覧)由来 —— 2026-08-25 に
    単数エンドポイントが取りこぼした側ではなく、**正本だった側**である。
    """
    snapshot = snapshot if snapshot is not None else _snapshot()
    row, reason = _fresh_account(snapshot, account, now_wall=now_wall)
    if row is None:
        return None
    wanted = [str(value) for value in (contract_ids or _contract_ids(symbol=symbol))]
    if not wanted:
        # 限月 ID が設定に無ければ Gateway 経路は使わない。銘柄名は一覧の行に無い。
        return None
    positions = row.get("positions") or {}
    if not isinstance(positions, dict):
        return None

    net = 0
    matched: Dict[str, Any] = {}
    other_nonzero = None
    for contract_id, item in positions.items():
        if not isinstance(item, dict):
            return None
        try:
            value = int(item.get("netPos") or 0)
        except (TypeError, ValueError):
            return None
        if str(contract_id) in wanted:
            net += value
            if value:
                matched = item
        elif value:
            other_nonzero = (str(contract_id), value)

    if net == 0 and other_nonzero is not None:
        # R41/R42 の規律: FLAT の誤報は建玉の上へ新規を武装させる。名乗らない。
        return None

    observed = row.get("sourceObservedAt")
    # **口座欄は 3 つとも「設定の口座名」。** REST がそう返すからで、揃えないと
    # `broker_status.position_identity` の hash(`account` / `accountId` を含む)が
    # 経路で変わり、**切替の瞬間に同じ建玉が別の建玉に見える**。
    #
    # 2026-09-19 に実ブローカーで確認: `/accounts/<名前>/position` の応答は
    # `account` / `accountId` / `brokerAccountId` のすべてに **口座名**を返す
    # (URL が名前で絞られていて、API がそれをそのまま返す)。数値の accountId が
    # 出てくるのは `Tv_ListPositions` の行と `CROSSTRADE_ACCOUNT_ID_*` の設定だけ。
    # 一致は `tests/test_r118_gateway_integration.py` が固定する。
    name = str(account)
    base = {"verified": True, "source": "gateway-ws", "platform": "TRADOVATE",
            "account": name, "accountId": name,
            "brokerAccountId": name, "symbol": str(symbol),
            "observedAt": observed or gw._iso(int(float(snapshot.get("writtenAt") or 0) * 1000)),
            "sourceObservedAt": observed,
            "gatewayGeneration": row.get("generation")}
    if net == 0:
        return {**base, "qty": 0, "closedAt": observed}
    return {**base, "side": "LONG" if net > 0 else "SHORT", "qty": abs(net),
            "avgEntry": matched.get("netPrice"),
            "filledAt": matched.get("timestamp"),
            "orderId": matched.get("orderId") or matched.get("id"),
            "receipt": matched.get("receipt")}


def orders_from_snapshot(account: str, symbol: str,
                         snapshot: Optional[Dict[str, Any]] = None,
                         contract_ids: Optional[List[str]] = None,
                         now_wall: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """Gateway の注文行を `broker_status.query_orders` と同じ形にする。

    **正規化と identity 検査は REST と同じ関数**(`normalize_crosstrade_orders`)へ
    通す。行の形が違えば `Unavailable` が飛ぶので、その場合は REST へ落ちる。
    別経路で緩い検査を書かない。
    """
    import broker_status as bs
    snapshot = snapshot if snapshot is not None else _snapshot()
    row, reason = _fresh_account(snapshot, account, now_wall=now_wall)
    if row is None:
        return None
    wanted = {str(value) for value in (contract_ids or _contract_ids(symbol=symbol))}
    if not wanted:
        return None
    rows = row.get("orders") or {}
    if not isinstance(rows, dict):
        return None
    selected = []
    for item in rows.values():
        if not isinstance(item, dict):
            return None
        if str(item.get("contractId") or "") not in wanted:
            continue
        clone = dict(item)
        # 一覧の行は銘柄名を持たない(contractId だけ)。正規化器は「名前があれば
        # 一致しなければならない」規約なので、無いまま渡して contractId で束縛させる。
        clone.pop("symbol", None)
        clone.pop("instrument", None)
        clone.pop("contractName", None)
        selected.append(clone)
    try:
        view = bs.normalize_crosstrade_orders(
            {"success": True, "data": selected},
            platform="TRADOVATE", account=str(account), symbol=str(symbol),
            # REST と同じく「口座名 + 設定上の accountId」を別名として渡す。
            # 行が持つのは accountId だけなので、これが無いと identity で弾かれる。
            account_aliases=account_aliases(account), contract_ids=sorted(wanted),
            unresolved_order_ids=None)
    except Exception:  # noqa: BLE001 — 形が違えば Gateway では答えない
        return None
    view["source"] = "gateway-ws"
    view["sourceObservedAt"] = row.get("sourceObservedAt")
    view["gatewayGeneration"] = row.get("generation")
    return view


# --------------------------------------------------------------- 出所の選択

def _record_divergence(kind: str, account: str, rest: Any, gateway: Any,
                       fields: List[str]) -> None:
    try:
        os.makedirs(SECRETS, exist_ok=True)
        with io.open(DIVERGENCE_LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "at": time.time(), "kind": kind, "account": str(account),
                "fields": fields,
                "rest": {key: (rest or {}).get(key) for key in fields},
                "gateway": {key: (gateway or {}).get(key) for key in fields},
            }, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001 — 記録できなくても運用は止めない
        pass


#: 影運転で比べる欄。**判断に効く欄だけ**。時刻や生の receipt は比べない
#: (出所が違えば必ず違う。差分ログがそれで埋まると本当の食い違いが埋もれる)。
POSITION_COMPARE = ("verified", "qty", "side")
ORDERS_COMPARE = ("verified", "state", "openCount")


def _compare(kind: str, account: str, rest: Any, gateway: Any,
             fields: Tuple[str, ...], reason: Optional[str] = None) -> None:
    _count("shadowChecked")
    if not isinstance(gateway, dict):
        # **比較が成立していない。** ここを黙って返すと、Gateway が止まっている日も
        # 差分ログが空になり「一致していた」と読めてしまう。理由ごとに数える。
        _count("shadowUnavailable", reason or "SHAPE",
               bucket="shadowUnavailableReasons")
        return
    _count("shadowCompared")
    differing = [key for key in fields
                 if (rest or {}).get(key) != gateway.get(key)]
    if differing:
        _count("shadowDiverged")
        _record_divergence(kind, account, rest, gateway, differing)


def _unavailable_reason(snapshot: Optional[Dict[str, Any]], account: str,
                        symbol: str, now_wall: Optional[float] = None) -> str:
    """Gateway で答えられない理由。**`SHAPE` に埋めない。**

    `NO_CONTRACT_ID` を分けているのは、`CROSSTRADE_CONTRACT_ID_<限月>` が
    **限月ロールのたびに空になる**ため(09-15 のロール以降ずっと空で、R117 の
    実接続まで誰も気づかなかった)。設定が欠けた瞬間に Gateway は全口座 REST へ
    静かに退避する —— 安全側ではあるが、**速さが黙って元に戻る**。
    """
    _, reason = _fresh_account(snapshot, account, now_wall=now_wall)
    if reason:
        return reason
    if not _contract_ids(symbol=symbol):
        return "NO_CONTRACT_ID"
    return "SHAPE"


def _note_position(answer: Any) -> Any:
    """枚数のある建玉を観測した回数を数える(影運転の証拠の質を見るため)。"""
    try:
        if isinstance(answer, dict) and answer.get("verified") and int(answer.get("qty") or 0):
            _count("positionsSeen")
    except (TypeError, ValueError):
        pass
    return answer


def position(symbol: str, account: str, *, rest_call, snapshot=None,
             now_wall: Optional[float] = None) -> Dict[str, Any]:
    """建玉の観測。`rest_call()` は引数なしで REST の結果を返す callable。"""
    current = mode()
    if current == OFF:
        _count("rest")
        return rest_call()
    view = position_from_snapshot(account, symbol, snapshot=snapshot, now_wall=now_wall)
    if current == SHADOW:
        answer = rest_call()
        _count("rest")
        reason = None if isinstance(view, dict) else _unavailable_reason(
            snapshot if snapshot is not None else _snapshot(), account, symbol, now_wall)
        _compare("position", account, answer, view, POSITION_COMPARE, reason)
        return _note_position(answer)
    if view is None:
        snap = snapshot if snapshot is not None else _snapshot()
        _count("fallback", _unavailable_reason(snap, account, symbol, now_wall))
        _count("rest")
        return _note_position(rest_call())
    _count("gateway")
    return _note_position(view)


def orders(symbol: str, account: str, *, rest_call, known_order_ids=None,
           snapshot=None, now_wall: Optional[float] = None) -> Dict[str, Any]:
    """注文の観測。ID 指定つきは **必ず REST**(一覧では ID の終端を証明できない)。"""
    current = mode()
    if current == OFF:
        _count("rest")
        return rest_call()
    if known_order_ids:
        _count("fallback", "KNOWN_ORDER_IDS")
        _count("rest")
        return rest_call()
    view = orders_from_snapshot(account, symbol, snapshot=snapshot, now_wall=now_wall)
    if current == SHADOW:
        answer = rest_call()
        _count("rest")
        reason = None if isinstance(view, dict) else _unavailable_reason(
            snapshot if snapshot is not None else _snapshot(), account, symbol, now_wall)
        _compare("orders", account, answer, view, ORDERS_COMPARE, reason)
        return answer
    if view is None:
        snap = snapshot if snapshot is not None else _snapshot()
        _count("fallback", _unavailable_reason(snap, account, symbol, now_wall))
        _count("rest")
        return rest_call()
    _count("gateway")
    return view


def observation_position(symbol: str, account: str = None, **_ignored) -> Dict[str, Any]:
    """**観測専用**の建玉照会(`fill_watch` 用)。`query_position` と同じ形を返す。

    `fill_watch` は 1.5 秒ごとに全口座を見回る。REST では 1 口座 1〜2 本の HTTP に
    なり、`next_delay` が `sweep_cost / maxRequestsPerSec` で伸びるので、口座が
    増えるほど TP1 の検知が遅れる(20 口座で 20 秒。指示 §1 の指摘どおり)。
    Gateway から読めば **0 本**なので、口座数に関係なく `fastIntervalSec` で回る。

    Gateway が使えない口座はそのまま REST。**穴は空けない。**
    """
    import broker_status as bs
    return position(symbol, str(account),
                    rest_call=lambda: bs.query_position(symbol, account=account))


def gateway_serving() -> bool:
    """いま Gateway が実際に答えを出せる状態か(表示・報告用)。"""
    if mode() != LIVE:
        return False
    snapshot = _snapshot()
    return isinstance(snapshot, dict) and snapshot.get("integrity") == gw.VERIFIED


# ------------------------------------------------- 影運転の記録(周期をまたぐ)

#: 1 周期 1 行。**`mode=OFF` の間は 1 バイトも書かない。**
CYCLE_LOG = os.path.join(SECRETS, "gateway_shadow.jsonl")


def record_cycle(path: Optional[str] = None, **extra) -> Optional[str]:
    """その周期の出所の内訳を追記する。**判断しない・失敗しても周期を止めない。**

    導入手順 §8 段 2 の確認(「差分が出ないこと」)は、差分ログが空なだけでは
    通せない —— Gateway が止まっていれば比較は 1 件も成立せず、同じく空になる。
    `shadowCompared` を周期ごとに残して、**比べた上で差が無かった**ことを言えるようにする。
    """
    current = mode()
    if current == OFF:
        return None
    snapshot = stats()
    touched = (snapshot.get("shadowChecked", 0) or snapshot.get("gateway", 0)
               or snapshot.get("fallback", 0))
    if not touched:
        return None
    if path is None:
        try:
            import cycle_timing
            if cycle_timing.writing_disabled():
                return None
        except Exception:  # noqa: BLE001 — 記録できなくても運用は止めない
            pass
    target = path or CYCLE_LOG
    row = {"at": time.time(), "mode": current,
           "gateway": snapshot.get("gateway", 0), "rest": snapshot.get("rest", 0),
           "fallback": snapshot.get("fallback", 0),
           "shadowChecked": snapshot.get("shadowChecked", 0),
           "shadowCompared": snapshot.get("shadowCompared", 0),
           "shadowDiverged": snapshot.get("shadowDiverged", 0),
           "shadowUnavailable": snapshot.get("shadowUnavailable", 0),
           "positionsSeen": snapshot.get("positionsSeen", 0),
           "fallbackReasons": snapshot.get("fallbackReasons") or {},
           "shadowUnavailableReasons": snapshot.get("shadowUnavailableReasons") or {},
           **{key: value for key, value in extra.items() if value is not None}}
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with io.open(target, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        return target
    except OSError:
        return None


def _read_jsonl(path: str) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    try:
        with io.open(path, encoding="utf-8") as fh:
            for raw in fh:
                try:
                    value = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(value, dict):
                    rows.append(value)
    except OSError:
        return []
    return rows


def shadow_summary(cycle_path: Optional[str] = None,
                   divergence_path: Optional[str] = None,
                   since: Optional[float] = None) -> Dict[str, Any]:
    """影運転の集計。**読むだけ。**"""
    rows = _read_jsonl(cycle_path or CYCLE_LOG)
    diffs = _read_jsonl(divergence_path or DIVERGENCE_LOG)
    if since is not None:
        rows = [row for row in rows if float(row.get("at") or 0) >= since]
        diffs = [row for row in diffs if float(row.get("at") or 0) >= since]
    totals = {"cycles": len(rows), "shadowChecked": 0, "shadowCompared": 0,
              "shadowDiverged": 0, "shadowUnavailable": 0, "positionsSeen": 0,
              "cyclesWithPosition": 0, "gateway": 0, "rest": 0, "fallback": 0}
    reasons: Dict[str, int] = {}
    modes: Dict[str, int] = {}
    for row in rows:
        for key in ("shadowChecked", "shadowCompared", "shadowDiverged",
                    "shadowUnavailable", "positionsSeen", "gateway", "rest", "fallback"):
            try:
                totals[key] += int(row.get(key) or 0)
            except (TypeError, ValueError):
                continue
        try:
            if int(row.get("positionsSeen") or 0):
                totals["cyclesWithPosition"] += 1
        except (TypeError, ValueError):
            pass
        modes[str(row.get("mode") or "?")] = modes.get(str(row.get("mode") or "?"), 0) + 1
        for bucket in ("shadowUnavailableReasons", "fallbackReasons"):
            for name, count in (row.get(bucket) or {}).items():
                try:
                    reasons[str(name)] = reasons.get(str(name), 0) + int(count)
                except (TypeError, ValueError):
                    continue
    fields: Dict[str, int] = {}
    for row in diffs:
        for name in row.get("fields") or []:
            fields[str(name)] = fields.get(str(name), 0) + 1
    first = min((float(row.get("at") or 0) for row in rows), default=0.0)
    last = max((float(row.get("at") or 0) for row in rows), default=0.0)
    return {"totals": totals, "reasons": reasons, "modes": modes,
            "divergences": len(diffs), "divergenceFields": fields,
            "firstAt": first, "lastAt": last}


def shadow_report(cycle_path: Optional[str] = None,
                  divergence_path: Optional[str] = None) -> str:
    """人が読む影運転の報告。**判定はしない**(段 3 へ進む条件は運用者が読む)。"""
    summary = shadow_summary(cycle_path, divergence_path)
    totals = summary["totals"]
    if not totals["cycles"]:
        return ("影運転の記録がまだ無い(`gateway.mode` が OFF か、周期が 1 度も"
                "回っていない)。")
    span = ""
    if summary["firstAt"] and summary["lastAt"]:
        span = (" · " + time.strftime("%m-%d %H:%M", time.localtime(summary["firstAt"]))
                + " 〜 " + time.strftime("%m-%d %H:%M", time.localtime(summary["lastAt"])))
    lines = [f"影運転 {totals['cycles']} 周期{span}",
             "  モード: " + ", ".join(f"{k}×{v}" for k, v in sorted(summary["modes"].items())),
             f"  比較を試みた {totals['shadowChecked']} 件 / "
             f"**成立 {totals['shadowCompared']}** / 不成立 {totals['shadowUnavailable']}",
             f"  食い違い {totals['shadowDiverged']} 件(差分ログ {summary['divergences']} 行)",
             f"  出所: gateway {totals['gateway']} / rest {totals['rest']} / "
             f"fallback {totals['fallback']}",
             f"  建玉を観測した周期 {totals['cyclesWithPosition']}"
             f"(FLAT どうしの一致は証拠として弱い)"]
    if summary["reasons"]:
        lines.append("  理由: " + ", ".join(
            f"{name}×{count}" for name, count in
            sorted(summary["reasons"].items(), key=lambda item: -item[1])))
    if summary["divergenceFields"]:
        lines.append("  食い違った欄: " + ", ".join(
            f"{name}×{count}" for name, count in
            sorted(summary["divergenceFields"].items(), key=lambda item: -item[1])))
    if totals["shadowCompared"] == 0:
        lines.append("  **比較が 1 件も成立していない。差分ゼロを『一致』と読まない。**")
    return "\n".join(lines)


def last_cycle(path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """直前の周期の 1 行(表示用)。"""
    rows = _read_jsonl(path or CYCLE_LOG)
    return rows[-1] if rows else None


def _main(argv: Optional[List[str]] = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(
        description="観測の出所(R118)。**読むだけ・注文も通信もしない。**")
    parser.add_argument("--shadow-report", action="store_true",
                        help="影運転の集計を出す")
    parser.add_argument("--status", action="store_true",
                        help="いまの mode と鮮度を 1 行で出す")
    args = parser.parse_args(argv)
    if args.shadow_report:
        print(shadow_report())
        return 0
    current = mode()
    snapshot = _snapshot()
    age = None
    if isinstance(snapshot, dict):
        try:
            age = time.time() - float(snapshot.get("writtenAt") or 0)
        except (TypeError, ValueError):
            age = None
    print(f"mode={current} · 限月 ID {_contract_ids() or '(無い → 全口座 REST)'} · "
          f"スナップショット "
          + ("無い" if not isinstance(snapshot, dict)
             else f"{snapshot.get('integrity')} / {age:.0f}s 前"
             if age is not None else str(snapshot.get("integrity"))))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())

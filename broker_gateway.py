# -*- coding: utf-8 -*-
"""R117: ブローカーと話すプロセスを 1 つにする(Broker Gateway)。

既定は **OFF**。`NQX_GATEWAY=1` のときだけ常駐プロセスとして動く。既存経路は何も変わらない。

## なぜ要るか

発注先が 7 口座になり、1 サイクルのブローカー照会が 70〜160 本・111〜284 秒になった。
R114 の一括照会で 3N 本(7 口座で 21 本)まで落ちたが、**口座数に比例する構造は残る**。
20 口座なら 60 本で、成行の門(`marketOrder.maxPriceAgeSec=60` 秒)に対して余裕が無い。

2026-09-19 に CrossTrade の WebSocket を実地検証して、前提が変わった:

* `wss://app.crosstrade.io/ws/stream` は **Tradovate 口座もプッシュする**
  (`orderUpdate` / `executionUpdate` / `positionUpdate`)。`fill_watch.py` の
  「WebSocket は NT8 だけ」は古い(2026-09-19 実測で否定)。
* **プッシュはレート枠を消費しない**(公式: "Incoming frames never count against any budget")。
* `Tv_ListPositions` / `Tv_ListOrders` は ``args:{}`` で **全リンク口座を 1 リクエスト**で返す
  (実測: 1 本で 5 接続グループ・42 注文行。各行が `accountId` を持ち、我々の
  `CROSSTRADE_ACCOUNT_ID_*` と一致する)。

つまり **観測は O(N) → O(1)** にできる。定常状態(状態変化なし)ではポーリング 0 本。

## この Gateway がやらないこと

* **注文を送らない。** 送信は従来どおり `order.py` の全ゲートを通る。Gateway から直接
  送って既存の検証を迂回する設計は禁止(指示 §3)。
* **鮮度だけで発注を許可しない。** ここが出すのは「いつ・どの証拠で・どこまで確かか」で、
  発注可否は従来どおり呼び出し側の契約が決める。
* **建玉ゼロを推測しない。** 整合性が証明できない期間は `UNVERIFIED` を出す
  (CLAUDE.md §6.1「建玉ゼロと推測しない」)。

## 構造

``GatewayState`` は **純粋な状態機械**で I/O を持たない。フレームを入れると状態が進む。
``BrokerGateway`` が接続・購読・resync・スナップショット書き出しを担う(transport と
clock は注入できる)。この分離のおかげで、順序逆転・重複・seq 欠落・resync・再接続・
口座消失を**ネットワーク無しで**何千件でも試験できる。
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

SCHEMA = "NQX_BROKER_GATEWAY/1"
WS_URL = os.environ.get("NQX_GATEWAY_WS_URL") or "wss://app.crosstrade.io/ws/stream"

#: 整合性の三状態。**FLAT を名乗れるのは VERIFIED のときだけ。**
VERIFIED = "VERIFIED"
UNVERIFIED = "UNVERIFIED"
RESYNCING = "RESYNCING"

#: 購読するイベント(2026-09-19 実測で通った名前)。
SUBSCRIBE_EVENTS = ("orders", "executions", "positions")
ORIGIN = "tradovate"

#: 状態を進めるフレーム種別。
POSITION_FRAME = "positionUpdate"
ORDER_FRAME = "orderUpdate"
EXECUTION_FRAME = "executionUpdate"
STATUS_FRAME = "status"
RESYNC_FRAME = "resync"

#: 終端した注文の状態(Tradovate の表記)。`execution_contract.json` の
#: `brokerObservation.terminalStates` と同じ意味。**大文字で比べる。**
TERMINAL_ORDER_STATES = frozenset({"FILLED", "CANCELED", "CANCELLED", "REJECTED", "EXPIRED"})

#: CrossTrade の上限(公式・利用者単位・REST と RPC 共通)。
RPC_PER_SEC = 3.0
RPC_BURST = 20

#: R118: スナップショットへ生存の印を書き直す間隔(秒)。契約 `brokerObservation.maxAgeSec`
#: (30 秒)より **十分短く**。ここを鮮度の緩和に使わない —— 書くのは「この時刻に
#: Gateway は生きていて VERIFIED だった」という事実だけで、ブローカー側の観測時刻
#: (`sourceObservedAt`)には触らない。
SNAPSHOT_REFRESH_SEC = float(os.environ.get("NQX_GATEWAY_REFRESH_SEC") or 5.0)

#: R118: 口座あたりに残す「終端した注文」の行数。押し込みだけで長時間回すと
#: 終端した行を誰も消さないので溜まり続ける。生きている行は 1 本も落とさない。
MAX_TERMINAL_ORDERS = int(os.environ.get("NQX_GATEWAY_MAX_TERMINAL_ORDERS") or 200)


def _iso(epoch_ms: Optional[float]) -> Optional[str]:
    """ブローカーの epoch(ミリ秒)を ISO へ。無ければ None(推測しない)。"""
    if epoch_ms in (None, ""):
        return None
    try:
        seconds = float(epoch_ms) / 1000.0
    except (TypeError, ValueError):
        return None
    from datetime import datetime, timezone
    return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat()


def _text(value: Any) -> str:
    return "" if value is None else str(value)


class GatewayState:
    """口座別の検証済み状態。**純粋** —— 時計もネットワークも持たない。

    呼び出し側が ``now`` (単調秒) を渡す。テストは仮想時計でそのまま動く。

    状態鍵は **(environment, accountId)**、建玉と注文は **contractId** で分ける
    (指示 §3A: 銘柄名の欠落を FLAT と解釈しない)。CrossTrade の行は `symbol` を
    持たず `contractId` しか持たないことがあるので、鍵は contractId 側に置く。
    """

    def __init__(self, *, expected_accounts: Iterable[str] = (),
                 contract_ids: Iterable[str] = ()):
        #: 設定上あるべき口座。ここに居ない口座の行は **混入**として弾く。
        self.expected_accounts = {(_text(a)) for a in expected_accounts if _text(a)}
        #: 監視対象の contractId。空なら全部受ける(照合は呼び出し側)。
        self.contract_ids = {(_text(c)) for c in contract_ids if _text(c)}
        self.updated_at: float = 0.0
        self.session_id: Optional[str] = None
        self.session_state = "disconnected"
        self.last_seq: Optional[int] = None
        self.seq_gaps = 0
        self.dropped = 0
        self.foreign_rows = 0
        #: R118: 知っている種類なのに読めなかったイベントの数。
        #: 0 でないなら押し込みの形がこちらの想定と違う。
        self.unreadable_events = 0
        self.integrity = UNVERIFIED
        self.integrity_reason = "not connected"
        self.snapshot_epoch: Optional[int] = None
        self.accounts: Dict[str, Dict[str, Any]] = {}
        #: 初期取得が完了するまでに届いたイベントは捨てずに溜める(競合対策)。
        self._pending: List[Dict[str, Any]] = []
        self._awaiting: Dict[str, bool] = {"positions": False, "orders": False}
        self.generation = 0

    # ---------------------------------------------------------------- 接続

    def connected(self, session_id: str, now: float) -> None:
        """新しい接続。**前の接続の連番は引き継がない。**"""
        self.session_id = _text(session_id)
        self.session_state = "connecting"
        self.last_seq = None
        self._pending.clear()
        self._mark_unverified("connection established; awaiting full state", now)

    def disconnected(self, reason: str, now: float) -> None:
        self.session_state = "disconnected"
        self._mark_unverified("disconnected: %s" % reason, now)

    def begin_resync(self, reason: str, now: float) -> None:
        """全体再取得を始める。ここから先のイベントは溜める。

        **口座ごとの整合性も同時に下げる。** 全体だけ下げて口座行を VERIFIED の
        ままにすると、`account_view` を読む側が「検証済みの FLAT」と誤読できてしまう
        (2026-09-19、テストで捕まえた)。FLAT を名乗れるのは VERIFIED のときだけ。
        """
        self.session_state = "syncing"
        self.integrity = RESYNCING
        self.integrity_reason = reason
        for row in self.accounts.values():
            row["integrity"] = RESYNCING
        self._awaiting = {"positions": True, "orders": True}
        self._pending.clear()
        self._touch(now)

    def _mark_unverified(self, reason: str, now: float) -> None:
        self.integrity = UNVERIFIED
        self.integrity_reason = reason
        for row in self.accounts.values():
            row["integrity"] = UNVERIFIED
        self._touch(now)

    def _touch(self, now: float) -> None:
        self.updated_at = now

    # ---------------------------------------------------------------- 全体取得

    def apply_full_positions(self, groups: Any, now: float, epoch: Optional[int] = None) -> int:
        """`Tv_ListPositions` の結果(全口座)を入れる。戻り値は採用した行数。"""
        return self._apply_full("positions", groups, now, epoch)

    def apply_full_orders(self, groups: Any, now: float, epoch: Optional[int] = None) -> int:
        """`Tv_ListOrders` の結果(全口座)を入れる。戻り値は採用した行数。"""
        return self._apply_full("orders", groups, now, epoch)

    def _apply_full(self, kind: str, groups: Any, now: float,
                    epoch: Optional[int]) -> int:
        """全体取得を **置き換え**として適用する。

        **足し込みではない。** `Tv_ListOrders` / `Tv_ListPositions` が返すのは
        「いま生きている全部」なので、切断・resync のあいだに取り消された注文や
        決済された建玉は **一覧に出てこない**。古い行を残すと:

        * 取り消し済みの注文が `ordStatus: "Working"` のまま生き続け、
          `liveOrders` に出る → **新規が blocking order で塞がる**、さらに悪いことに
          `verify_protective_orders` / `oco_sibling_pair` が**幻の保護脚**を数えて
          「runner は守られている」と誤答しうる(R78 の裸 runner の逆パターン)。
        * 決済済みの建玉が残り、**存在しない建玉**を報告する。

        2026-09-19 に 20 口座の持続負荷試験で発覚した(注文行が再取得のたびに
        増え続けていた)。`seen_accounts` に居る口座は消していなかったのが原因で、
        一覧に **1 行でも** ある口座ほど古い行が溜まるという最悪の形だった。
        """
        taken = 0
        seen_accounts = set()
        # 取得を適用する前に、知っている全口座のその種別を空にする。
        # **未観測(UNVERIFIED)とは別物** —— ここは「観測した結果、無かった」を作る。
        scope = set(self.expected_accounts) | set(self.accounts)
        for account in scope:
            self._account(account, "", now)[
                "positions" if kind == "positions" else "orders"] = {}
        for group in (groups if isinstance(groups, list) else []):
            if not isinstance(group, dict):
                continue
            environment = _text(group.get("environment"))
            for row in (group.get("data") or []):
                if not isinstance(row, dict):
                    continue
                account = _text(row.get("accountId") or row.get("account"))
                if not account:
                    continue
                if self.expected_accounts and account not in self.expected_accounts:
                    # 他口座の行が混ざるのは identity の事故。数えて捨てる。
                    self.foreign_rows += 1
                    continue
                entry = self._account(account, environment, now)
                seen_accounts.add(account)
                contract = _text(row.get("contractId"))
                if kind == "positions":
                    entry["positions"][contract] = dict(row)
                else:
                    entry["orders"][_text(row.get("id"))] = dict(row)
                taken += 1
        # 一覧に出なかった設定口座も「観測した結果 行が無い」= 空のまま。
        # **未観測とは違う。** 空にするのは上の置き換えで済んでいるので、ここは
        # 取得の印(`fullAt` / 鮮度)を付けるだけ。
        for account in (self.expected_accounts or set()) | seen_accounts:
            entry = self._account(account, "", now)
            entry["fullAt"][kind] = now
            entry["receivedAt"] = now
            # 一覧の行は個別の時刻を持たないので、**その取得の epoch** を鮮度にする。
            # 持っていなければ None のまま(ローカル時刻で埋めない)。
            listed = _iso(epoch)
            if listed:
                entry["sourceObservedAt"] = listed
        self._awaiting[kind] = False
        if epoch is not None:
            self.snapshot_epoch = int(epoch)
        self._finish_full_if_ready(now)
        return taken

    def _finish_full_if_ready(self, now: float) -> None:
        if any(self._awaiting.values()):
            return
        # 初期取得の最中に届いたイベントを、取得結果の上へ順に重ねる。
        pending, self._pending = self._pending, []
        self.generation += 1
        self.integrity = VERIFIED
        self.integrity_reason = "full state applied"
        for account in self.accounts.values():
            account["integrity"] = VERIFIED
            account["generation"] = self.generation
        for frame in pending:
            self._apply_event(frame, now)
        self.session_state = "ready"
        self._touch(now)

    # ---------------------------------------------------------------- イベント

    def apply_frame(self, frame: Any, now: float) -> str:
        """1 フレームを適用し、何をしたかを短い語で返す(監査・テスト用)。"""
        if not isinstance(frame, dict):
            return "ignored"
        kind = _text(frame.get("type"))
        seq_verdict = self._track_seq(frame, now)
        if seq_verdict == "duplicate":
            return "duplicate"

        if kind == STATUS_FRAME:
            state = _text(frame.get("state")) or "unknown"
            self.session_state = state
            if state in ("stalled", "stopped", "disconnected"):
                self._mark_unverified("status=%s" % state, now)
            self._touch(now)
            return "status:%s" % state
        if kind == RESYNC_FRAME:
            self.begin_resync("broker asked for resync", now)
            return "resync"
        if kind in (POSITION_FRAME, ORDER_FRAME, EXECUTION_FRAME):
            if self.integrity != VERIFIED:
                # 初期取得の途中。捨てずに溜めて、取得後に重ねる。
                self._pending.append(frame)
                # 欠落を検出したフレームは、溜めたことより「欠落」を返す方が監査に役立つ。
                return "gap" if seq_verdict == "gap" else "buffered"
            return self._apply_event(frame, now)
        return "ignored"

    def _track_seq(self, frame: Dict[str, Any], now: float) -> str:
        raw = frame.get("seq")
        if raw is None:
            return "ok"
        try:
            seq = int(raw)
        except (TypeError, ValueError):
            return "ok"
        dropped = frame.get("dropped")
        if dropped not in (None, "", 0):
            try:
                self.dropped += int(dropped)
            except (TypeError, ValueError):
                pass
            self.begin_resync("broker reported dropped frames", now)
        if self.last_seq is None:
            self.last_seq = seq
            return "ok"
        if seq <= self.last_seq:
            # 重複・順序逆転。状態は進めない(古い値で上書きしない)。
            return "duplicate"
        if seq > self.last_seq + 1:
            previous = self.last_seq
            self.seq_gaps += 1
            self.last_seq = seq
            self.begin_resync("seq gap %d -> %d" % (previous, seq), now)
            return "gap"
        self.last_seq = seq
        return "ok"

    def _event_payload(self, frame: Dict[str, Any], key: str) -> Dict[str, Any]:
        """イベント本体を取り出す。

        R118: **押し込みフレームの正確な形は実地で未確認**(2026-09-19 の観測窓では
        建玉・注文の変化が 1 件も起きず、`status` と RPC 応答しか届かなかった)。
        文書の形は `{type, accountId, contractId, position:{...}}` だが、RPC の応答は
        本体を `data` で包む。どちらでも読めるようにして、**読めなかったら
        整合性を落とす**(下の `_apply_event`)。黙って捨てると、押し込みが全部
        読めないまま「検証済み」を名乗り続けることになる。
        """
        for candidate in (frame.get(key), frame.get("data")):
            if isinstance(candidate, dict):
                return candidate
        return {}

    def _apply_event(self, frame: Dict[str, Any], now: float) -> str:
        kind = _text(frame.get("type"))
        payload = self._event_payload(
            frame, {POSITION_FRAME: "position", ORDER_FRAME: "order"}.get(kind, "data"))
        account = _text(frame.get("account") or frame.get("accountId")
                        or payload.get("accountId") or payload.get("account"))
        environment = _text(frame.get("environment") or payload.get("environment"))
        contract = _text(frame.get("contractId") or payload.get("contractId"))
        if self.expected_accounts and account and account not in self.expected_accounts:
            self.foreign_rows += 1
            return "foreign"
        if self.contract_ids and contract and contract not in self.contract_ids:
            # 別限月のイベント。状態は持つが、監視対象の判定には使わない。
            return "other-contract"
        if not account:
            return self._unreadable(kind, "no account id in frame", now)
        entry = self._account(account, environment, now)
        entry["sourceObservedAt"] = _iso(frame.get("epoch")) or entry.get("sourceObservedAt")
        entry["receivedAt"] = now
        entry["generation"] = self.generation
        if kind == POSITION_FRAME:
            entry["positions"][contract] = {**payload, "contractId": contract,
                                            "accountId": account}
            return "position"
        if kind == ORDER_FRAME:
            order_id = _text(payload.get("id") or payload.get("orderId")
                             or frame.get("orderId"))
            if not order_id:
                return self._unreadable(kind, "no order id in frame", now)
            entry["orders"][order_id] = {**payload, "contractId": contract,
                                         "accountId": account}
            self._prune_terminal_orders(entry)
            return "order"
        if kind == EXECUTION_FRAME:
            entry["executions"] = (entry.get("executions") or 0) + 1
            entry["lastExecutionAt"] = _iso(frame.get("epoch"))
            return "execution"
        return "ignored"

    def _prune_terminal_orders(self, entry: Dict[str, Any]) -> None:
        """終端した注文の行を口座あたり `MAX_TERMINAL_ORDERS` 本までにする。

        押し込みだけで回している間、終端した注文は誰も消さないので溜まり続ける
        (全体再取得が入るまで)。**生きている注文は 1 本も落とさない** ——
        落とすのは終端した古い行だけ。順序は挿入順(Python の dict は保持する)。
        """
        rows = entry["orders"]
        terminal = [key for key, row in rows.items()
                    if _text(row.get("ordStatus")).upper() in TERMINAL_ORDER_STATES]
        excess = len(terminal) - MAX_TERMINAL_ORDERS
        for key in terminal[:excess] if excess > 0 else ():
            rows.pop(key, None)

    def _unreadable(self, kind: str, why: str, now: float) -> str:
        """**知っている種類のイベントなのに読めなかった。**

        これを黙って捨てると、押し込みが全部読めないまま「検証済み」を名乗り続ける
        —— 全口座が FLAT に見えたまま固まるのと同じ事故になる。整合性を落として
        全体再取得へ回す(再取得は RPC なので、形が変わっていても状態は取れる)。
        """
        self.unreadable_events += 1
        if kind in (POSITION_FRAME, ORDER_FRAME, EXECUTION_FRAME):
            self.begin_resync(f"unreadable {kind}: {why}", now)
            return "unreadable"
        return "ignored"

    def _account(self, account: str, environment: str, now: float) -> Dict[str, Any]:
        entry = self.accounts.get(account)
        if entry is None:
            entry = {
                "accountId": account,
                "environment": environment,
                "positions": {},
                "orders": {},
                "executions": 0,
                "generation": self.generation,
                "integrity": self.integrity,
                "sourceObservedAt": None,
                "receivedAt": now,
                "fullAt": {},
            }
            self.accounts[account] = entry
        elif environment and not entry.get("environment"):
            entry["environment"] = environment
        return entry

    # ---------------------------------------------------------------- 読み出し

    def account_view(self, account: str, contract_id: str) -> Dict[str, Any]:
        """1 口座・1 限月の見え方。**判断はしない**(材料だけ返す)。

        `integrity` が `VERIFIED` でなければ、呼び出し側は建玉ゼロと読んではならない。
        """
        account = _text(account)
        contract_id = _text(contract_id)
        entry = self.accounts.get(account)
        if entry is None:
            return {"accountId": account, "integrity": UNVERIFIED,
                    "reason": "account not observed", "contractId": contract_id}
        position = entry["positions"].get(contract_id) or {}
        net = position.get("netPos")
        try:
            net = int(net) if net is not None else 0
        except (TypeError, ValueError):
            net = None
        orders = [row for row in entry["orders"].values()
                  if _text(row.get("contractId")) == contract_id]
        # 終端した注文は「残っている注文」ではない。両方返して、判断は呼び出し側へ。
        live = [row for row in orders
                if _text(row.get("ordStatus")).upper() not in TERMINAL_ORDER_STATES]
        return {
            "accountId": account,
            "environment": entry.get("environment"),
            "contractId": contract_id,
            "integrity": entry.get("integrity", UNVERIFIED),
            "generation": entry.get("generation"),
            "netPos": net,
            "avgPrice": position.get("netPrice"),
            "orders": orders,
            "liveOrders": live,
            "sourceObservedAt": entry.get("sourceObservedAt"),
            "receivedAt": entry.get("receivedAt"),
        }

    def snapshot(self, now: float) -> Dict[str, Any]:
        """書き出す形。**ファイルを書いた時刻で鮮度を更新しない**(指示 §3A)。

        `now` は **壁時計**(`time.time()`)。単調時計を入れてはいけない ——
        このファイルは **別のプロセス**が読み、`time.time()` と引き算して鮮度を
        判定する。単調時計は起動時刻が違えばプロセス間で比較できないので、
        読む側からは常に「古すぎる」に見える(2026-09-19、実プロセスで発覚。
        手で組んだスナップショットの試験では出なかった)。

        `sourceObservedAt`(ブローカー側の観測時刻)はここでは触らない。
        """
        return {
            "schema": SCHEMA,
            "session": {"id": self.session_id, "state": self.session_state},
            "integrity": self.integrity,
            "integrityReason": self.integrity_reason,
            "generation": self.generation,
            "seq": {"last": self.last_seq, "gaps": self.seq_gaps, "dropped": self.dropped},
            "foreignRows": self.foreign_rows,
            "unreadableEvents": self.unreadable_events,
            "brokerEpoch": self.snapshot_epoch,
            "writtenAt": now,
            "accounts": {
                account: {
                    "environment": entry.get("environment"),
                    "integrity": entry.get("integrity", UNVERIFIED),
                    "generation": entry.get("generation"),
                    "positions": entry.get("positions"),
                    "orders": entry.get("orders"),
                    "executions": entry.get("executions"),
                    "sourceObservedAt": entry.get("sourceObservedAt"),
                    "receivedAt": entry.get("receivedAt"),
                }
                for account, entry in self.accounts.items()
            },
        }


class TokenBucket:
    """CrossTrade の通信予算(3 req/s・バースト 20・利用者単位)。

    **プッシュは消費しない。** 消費するのは RPC と REST だけ(公式)。
    購読は別枠(約 20/分)なので数だけ別に持つ。
    """

    def __init__(self, rate: float = RPC_PER_SEC, burst: int = RPC_BURST,
                 now: float = 0.0):
        self.rate = float(rate)
        self.burst = int(burst)
        self.tokens = float(burst)
        self.updated = float(now)
        self.spent = 0
        self.waited = 0.0

    def take(self, now: float, count: int = 1) -> float:
        """``count`` 本ぶんの枠を取る。戻り値は**待つべき秒数**(0 なら即時)。"""
        elapsed = max(0.0, now - self.updated)
        self.tokens = min(float(self.burst), self.tokens + elapsed * self.rate)
        self.updated = now
        self.spent += count
        if self.tokens >= count:
            self.tokens -= count
            return 0.0
        deficit = count - self.tokens
        self.tokens = 0.0
        wait = deficit / self.rate if self.rate > 0 else float("inf")
        self.waited += wait
        return wait


# ---------------------------------------------------------------- 常駐 runner


class AiohttpTransport:
    """本番の WebSocket。**注入点**なので、テストはこれを差し替えて使わない。"""

    def __init__(self, url: str, token: str):
        self.url = url
        self.token = token
        self._session = None
        self._ws = None

    async def connect(self) -> None:
        import aiohttp
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=20, sock_read=None)
        self._session = aiohttp.ClientSession(timeout=timeout)
        self._ws = await self._session.ws_connect(
            self.url, headers={"Authorization": "Bearer %s" % self.token})

    async def send(self, payload: Dict[str, Any]) -> None:
        await self._ws.send_str(json.dumps(payload))

    async def recv(self) -> Optional[Dict[str, Any]]:
        """次のフレーム。切断なら None。**JSON でないフレームは読み飛ばす。**"""
        import aiohttp
        while True:
            message = await self._ws.receive()
            if message.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING,
                                aiohttp.WSMsgType.ERROR):
                return None
            if message.type is aiohttp.WSMsgType.TEXT:
                try:
                    return json.loads(message.data)
                except ValueError:
                    continue

    async def close(self) -> None:
        try:
            if self._ws is not None:
                await self._ws.close()
        finally:
            if self._session is not None:
                await self._session.close()


class BrokerGateway:
    """接続を 1 本だけ持ち、検証済み状態をファイルへ書き続ける常駐プロセス。

    **注文は送らない。** RPC は `Tv_ListPositions` / `Tv_ListOrders` の 2 つだけを使う。

    `transport` と `clock` は注入できる。テストは偽 transport で、再接続・欠落・
    resync・切断をネットワーク無しで再現する。
    """

    LIST_POSITIONS = "Tv_ListPositions"
    LIST_ORDERS = "Tv_ListOrders"

    #: 何も届かない時間がこれを超えたら「無言」とみなす(秒)。接続が生きていても
    #: フレームが止まることはある(add-on の停止・上流の詰まり)。届かないこと自体を
    #: FLAT の証明にしないため、ここで整合性を落として再取得へ回す。
    SILENCE_SEC = float(os.environ.get("NQX_GATEWAY_SILENCE_SEC") or 30.0)

    def __init__(self, *, transport, state: GatewayState,
                 clock=time.monotonic, snapshot_file: Optional[str] = None,
                 budget: Optional[TokenBucket] = None, writer=None,
                 wall_clock=time.time):
        self.transport = transport
        self.state = state
        self.clock = clock
        #: 書き出す時刻。**壁時計**(別プロセスが読むので単調時計は使えない)。
        self.wall_clock = wall_clock
        self.snapshot_file = snapshot_file
        self.budget = budget or TokenBucket(now=clock())
        self.writer = writer or (lambda st, _now: write_snapshot(
            st, self.wall_clock(), self.snapshot_file))
        self.rpc_calls = 0
        self.subscribes = 0
        self.reconnects = 0
        self.frames = 0
        self.silences = 0
        #: 受信のたびの「ブローカー epoch → ローカル受信」の差(秒)。
        self.latency_samples: List[float] = []

    async def _rpc(self, call_id: str, api: str) -> None:
        """RPC を 1 本送る。**枠を消費する**ので数える。"""
        wait = self.budget.take(self.clock(), 1)
        if wait > 0:
            import asyncio
            await asyncio.sleep(wait)
        self.rpc_calls += 1
        await self.transport.send({"action": "rpc", "id": call_id,
                                   "origin": ORIGIN, "api": api, "args": {}})

    async def _subscribe(self) -> None:
        self.subscribes += 1
        await self.transport.send({"action": "subscribe", "origin": ORIGIN,
                                   "events": list(SUBSCRIBE_EVENTS)})

    async def _request_full_state(self) -> None:
        self.state.begin_resync("requesting full state", self.clock())
        await self._rpc("gw-pos", self.LIST_POSITIONS)
        await self._rpc("gw-ord", self.LIST_ORDERS)

    def _note_latency(self, frame: Dict[str, Any], now_wall: float) -> None:
        epoch = frame.get("epoch")
        if epoch in (None, ""):
            return
        try:
            delta = now_wall - float(epoch) / 1000.0
        except (TypeError, ValueError):
            return
        # 時計ずれで負になる場合がある。**捨てずに記録**して、同期精度として報告する。
        self.latency_samples.append(delta)

    def _handle(self, frame: Dict[str, Any], now: float, now_wall: float) -> str:
        self.frames += 1
        self._note_latency(frame, now_wall)
        rid = _text(frame.get("id"))
        if rid in ("gw-pos", "gw-ord"):
            payload = frame.get("data") if isinstance(frame.get("data"), dict) else {}
            groups = payload.get("data")
            if rid == "gw-pos":
                self.state.apply_full_positions(groups, now, frame.get("epoch"))
            else:
                self.state.apply_full_orders(groups, now, frame.get("epoch"))
            return "full:%s" % rid
        verdict = self.state.apply_frame(frame, now)
        return verdict

    async def run_once(self, *, max_frames: Optional[int] = None,
                       max_seconds: Optional[float] = None) -> Dict[str, Any]:
        """1 回の接続ぶんを回す。戻り値は監査用の要約。"""
        import asyncio  # noqa: F401 — 下の wait_for で使う
        started = self.clock()
        try:
            return await self._run_once_inner(started, max_frames, max_seconds)
        finally:
            # **例外で抜けるときも必ず閉じる。** 閉じないと再接続のたびに
            # aiohttp のセッションが残る(2026-09-20 の 60 分常駐で
            # "Unclosed client session" を実測。切断 1 回につき 1 個)。
            try:
                await self.transport.close()
            except Exception:  # noqa: BLE001 — 後始末で落とさない
                pass

    async def _run_once_inner(self, started: float, max_frames: Optional[int],
                              max_seconds: Optional[float]) -> Dict[str, Any]:
        import asyncio
        await self.transport.connect()
        self.state.connected("sess-%d" % int(started * 1000), self.clock())
        await self._subscribe()
        await self._request_full_state()
        needs_resync = False
        seen = 0
        last_frame_at = started
        while True:
            if max_frames is not None and seen >= max_frames:
                break
            if max_seconds is not None and self.clock() - started >= max_seconds:
                break
            # **recv は必ず期限付きで待つ。** 無期限に待つと max_seconds も
            # 無言検知も効かない(2026-09-19、実測で 600 秒ぶら下がった)。
            budget_left = None
            if max_seconds is not None:
                budget_left = max(0.0, max_seconds - (self.clock() - started))
            silence_left = max(0.0, self.SILENCE_SEC - (self.clock() - last_frame_at))
            # R118: 生存の印を定期的に書く。イベントが起きない = 変化が無いだけで、
            # 観測が古いわけではない。ここを書かないと健全な待機中に `writtenAt` が
            # 古くなり、読む側(broker_source)が丸ごと REST へ落ちる。
            # **無言検知は別に走り続ける**ので、接続が死んでいれば整合性が落ちる。
            wait = min(SNAPSHOT_REFRESH_SEC, silence_left or self.SILENCE_SEC)
            if budget_left is not None:
                wait = min(wait, budget_left)
            if wait <= 0:
                break
            try:
                frame = await asyncio.wait_for(self.transport.recv(), timeout=wait)
            except asyncio.TimeoutError:
                now = self.clock()
                if budget_left is not None and budget_left <= wait:
                    break            # 観測窓の終わり。無言ではない。
                if now - last_frame_at >= self.SILENCE_SEC:
                    self.silences += 1
                    last_frame_at = now
                    self.state.begin_resync("no frames for %.0fs" % self.SILENCE_SEC, now)
                    await self._rpc("gw-pos", self.LIST_POSITIONS)
                    await self._rpc("gw-ord", self.LIST_ORDERS)
                elif self.snapshot_file is not None:
                    self.writer(self.state, now)
                continue
            last_frame_at = self.clock()
            if frame is None:
                self.state.disconnected("transport closed", self.clock())
                break
            seen += 1
            verdict = self._handle(frame, self.clock(), time.time())
            if verdict in ("gap", "resync") or self.state.integrity == RESYNCING:
                needs_resync = needs_resync or verdict in ("gap", "resync")
            if needs_resync and self.state.integrity == RESYNCING:
                needs_resync = False
                await self._rpc("gw-pos", self.LIST_POSITIONS)
                await self._rpc("gw-ord", self.LIST_ORDERS)
            if self.snapshot_file is not None:
                self.writer(self.state, self.clock())
        return {
            "frames": self.frames, "rpcCalls": self.rpc_calls,
            "subscribes": self.subscribes, "integrity": self.state.integrity,
            "seqGaps": self.state.seq_gaps, "dropped": self.state.dropped,
            "foreignRows": self.state.foreign_rows,
            "budgetSpent": self.budget.spent, "budgetWaited": self.budget.waited,
            "latencySamples": len(self.latency_samples), "silences": self.silences,
        }


def order_placement_floor_sec(accounts: int, legs_per_account: int = 2,
                              rate: float = RPC_PER_SEC, burst: int = RPC_BURST) -> float:
    """満杯のバケツから ``accounts × legs`` 本を送り切るのに要する下限秒数。

    指示 §4 の算術をコードにしたもの。応答時間・事前照会・ネットワークは含まない
    **純粋な補充待ち**なので、実測はこれより必ず大きい。
    """
    requests = int(accounts) * int(legs_per_account)
    if requests <= burst:
        return 0.0
    return (requests - burst) / float(rate)


def gateway_enabled(env: Optional[Dict[str, str]] = None) -> bool:
    """既定 OFF。`NQX_GATEWAY=1` のときだけ常駐を許す。"""
    source = env if env is not None else os.environ
    return str(source.get("NQX_GATEWAY") or "").strip() in ("1", "true", "TRUE", "yes")


def snapshot_path() -> str:
    base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, ".secrets", "broker_gateway_snapshot.json")


def write_snapshot(state: GatewayState, now: float, path: Optional[str] = None) -> str:
    """原子的に書き出す。読む側が半端な JSON を掴まないように一時ファイル経由。"""
    target = path or snapshot_path()
    tmp = target + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(state.snapshot(now), fh, ensure_ascii=False)
    os.replace(tmp, target)
    return target


def read_snapshot(path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """スナップショットを読む。無い・壊れている・schema 違いは None(推測しない)。"""
    target = path or snapshot_path()
    try:
        with open(target, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        return None
    return data



# ---------------------------------------------------------------- 常駐(R118)

def heartbeat_path() -> str:
    base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, ".secrets", "broker_gateway_heartbeat.json")


def lock_path() -> str:
    base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, ".secrets", "broker_gateway.lock")


#: 再接続の待ち。**接続は利用者あたり 1 本**なので、慌てて繋ぎ直すと自分の接続を
#: 自分で切ることになる。指数で空けて上限で止める。
RECONNECT_BACKOFF_SEC = (1.0, 2.0, 5.0, 10.0, 20.0, 30.0)


def write_heartbeat(payload: Dict[str, Any], path: Optional[str] = None) -> None:
    target = path or heartbeat_path()
    tmp = target + ".tmp"
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(payload, fh, ensure_ascii=False)
        os.replace(tmp, target)
    except OSError:
        pass


def read_heartbeat(path: Optional[str] = None) -> Dict[str, Any]:
    try:
        with open(path or heartbeat_path(), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def alive(max_age_sec: float = 60.0) -> bool:
    """常駐が生きているか(表示・監視用)。"""
    beat = read_heartbeat()
    try:
        return (time.time() - float(beat.get("at") or 0)) <= max_age_sec
    except (TypeError, ValueError):
        return False


async def serve(*, accounts: List[str], contract_ids: List[str], token: str,
                url: Optional[str] = None, max_seconds: Optional[float] = None,
                transport_factory=None, snapshot_file: Optional[str] = None,
                heartbeat_file: Optional[str] = None,
                sleep=None, clock=time.monotonic,
                on_cycle=None) -> Dict[str, Any]:
    """接続が切れても繋ぎ直しながら回り続ける。**注文は送らない。**

    `transport_factory` を差し替えるとネットワーク無しで試験できる。
    """
    import asyncio
    sleep = sleep or asyncio.sleep
    started = clock()
    state = GatewayState(expected_accounts=accounts, contract_ids=contract_ids)
    attempts = 0
    cycles = 0
    totals = {"frames": 0, "rpcCalls": 0, "reconnects": 0, "silences": 0, "errors": 0}
    while True:
        if max_seconds is not None and clock() - started >= max_seconds:
            break
        factory = transport_factory or (lambda: AiohttpTransport(url or WS_URL, token))
        target = snapshot_file or snapshot_path()

        def write_both(st, _now):
            """スナップショットと heartbeat を同時に。

            heartbeat を周期の終わりにしか書かないと、繋がっている間ずっと
            `--status` が「常駐していない」と言う(2026-09-19 実測)。
            """
            write_snapshot(st, time.time(), target)
            write_heartbeat({"schema": "NQX_BROKER_GATEWAY_HEARTBEAT/1",
                             "at": time.time(), "pid": os.getpid(),
                             "integrity": st.integrity,
                             "integrityReason": st.integrity_reason,
                             "accounts": len(accounts), "cycles": cycles,
                             "state": st.session_state}, heartbeat_file)

        gateway = BrokerGateway(transport=factory(), state=state, clock=clock,
                                snapshot_file=target, writer=write_both)
        window = None
        if max_seconds is not None:
            window = max(0.0, max_seconds - (clock() - started))
            if window <= 0:
                break
        try:
            summary = await gateway.run_once(max_seconds=window)
            totals["frames"] += summary["frames"]
            totals["rpcCalls"] += summary["rpcCalls"]
            totals["silences"] += summary["silences"]
            attempts = 0
        except Exception as exc:  # noqa: BLE001 — 切断・上流の事故で落ちても止めない
            totals["errors"] += 1
            # **整合性を落としてから**待つ。落とさずに待つと、読む側が古い
            # スナップショットを「検証済み」として読み続ける。
            state.disconnected(f"{type(exc).__name__}: {exc}", clock())
            try:
                write_snapshot(state, time.time(), snapshot_file or snapshot_path())
            except OSError:
                pass
        cycles += 1
        if on_cycle is not None:
            on_cycle(state, totals)
        write_heartbeat({"schema": "NQX_BROKER_GATEWAY_HEARTBEAT/1", "at": time.time(),
                         "pid": os.getpid(), "integrity": state.integrity,
                         "integrityReason": state.integrity_reason,
                         "accounts": len(accounts), "cycles": cycles, **totals},
                        heartbeat_file)
        if max_seconds is not None and clock() - started >= max_seconds:
            break
        totals["reconnects"] += 1
        delay = RECONNECT_BACKOFF_SEC[min(attempts, len(RECONNECT_BACKOFF_SEC) - 1)]
        attempts += 1
        await sleep(delay)
    return {"cycles": cycles, "integrity": state.integrity, **totals}


def _serve_cli(seconds: Optional[float]) -> int:
    import asyncio
    import broker_status

    if not gateway_enabled():
        print("NQX_GATEWAY=1 が無い。既定 OFF のため常駐しない。")
        return 0
    cfg = broker_status.read_env(broker_status.CROSSTRADE_ENV)
    token = cfg.get("CROSSTRADE_API_TOKEN") or cfg.get("CROSSTRADE_KEY")
    if not token:
        print("CROSSTRADE のトークンが無い")
        return 2
    accounts = [cfg.get("CROSSTRADE_ACCOUNT_ID_" + value.strip())
                for value in str(cfg.get("CROSSTRADE_ACCOUNTS") or "").split(",")
                if value.strip()]
    accounts = [str(value) for value in accounts if value]
    contract = cfg.get("CROSSTRADE_CONTRACT_ID_" + broker_status.DEFAULT_SYMBOL)
    # **接続は利用者あたり 1 本。** 二重起動は自分の接続を自分で切る。
    try:
        handle = os.open(lock_path(), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except OSError:
        beat = read_heartbeat()
        age = time.time() - float(beat.get("at") or 0)
        if age < 120:
            print("すでに常駐している(%.0f 秒前の heartbeat)。二重起動しない。" % age)
            return 0
        print("古いロックを引き剥がす(heartbeat %.0f 秒前)" % age)
        try:
            os.unlink(lock_path())
            handle = os.open(lock_path(), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except OSError as exc:
            print("ロックを取れない: %s" % exc)
            return 1
    try:
        os.write(handle, str(os.getpid()).encode("ascii"))
        os.close(handle)
        print("Gateway 常駐開始: 口座 %d / 限月 %s" % (len(accounts), contract))
        result = asyncio.run(serve(accounts=accounts,
                                   contract_ids=[contract] if contract else [],
                                   token=token, max_seconds=seconds))
        print("終了: %s" % json.dumps(result, ensure_ascii=False))
        return 0
    finally:
        try:
            os.unlink(lock_path())
        except OSError:
            pass



def _start_cli() -> int:
    """切り離して常駐させる。**注文は送らない。**

    `--serve` は前面で回るので、端末を閉じると止まる。運用ではこちらを使う
    (`nqx_cycle` の autostart と同じ `spawn_detached`)。
    """
    if not gateway_enabled():
        print("NQX_GATEWAY=1 が無い。既定 OFF のため起動しない。")
        return 0
    if alive():
        beat = read_heartbeat()
        print("すでに常駐している(pid=%s)。二重起動しない。" % beat.get("pid"))
        return 0
    pid = spawn_detached()
    print("常駐を起動した pid=%s(出力 %s)" % (pid, log_path()))
    print("止めるときは python broker_gateway.py --stop")
    return 0


def _stop_cli() -> int:
    """常駐を止める。**pid の使い回しで別プロセスを殺さない**ように鮮度を見る。"""
    import signal
    beat = read_heartbeat()
    pid = beat.get("pid")
    try:
        age = time.time() - float(beat.get("at") or 0)
    except (TypeError, ValueError):
        age = None
    if not pid:
        print("heartbeat に pid が無い(常駐していない)")
        return 0
    if age is None or age > 300:
        print("heartbeat が古い(%s)。pid=%s は別のプロセスかもしれないので止めない。"
              % ("読めない" if age is None else "%.0f 秒前" % age, pid))
        print("本当に止めるなら手で: taskkill /PID %s" % pid)
        return 1
    try:
        os.kill(int(pid), signal.SIGTERM)
    except (OSError, ValueError, TypeError) as exc:
        print("止められなかった(pid=%s): %s" % (pid, exc))
        return 1
    try:
        os.unlink(lock_path())
    except OSError:
        pass
    print("常駐を止めた pid=%s" % pid)
    return 0


def status_line() -> str:
    """毎周期 1 行で出す生死。**表示専用**(ここで何も判断しない)。"""
    try:
        import broker_source
        mode = broker_source.mode()
    except Exception:  # noqa: BLE001
        mode = "?"
    if mode == "OFF":
        return "gateway: OFF(観測は従来どおり REST)"
    beat = read_heartbeat()
    if not beat:
        return f"gateway: {mode} — 常駐していない(全口座 REST へ退避)"
    try:
        age = time.time() - float(beat.get("at") or 0)
    except (TypeError, ValueError):
        return f"gateway: {mode} — heartbeat が読めない"
    snapshot = read_snapshot()
    integrity = (snapshot or {}).get("integrity") or beat.get("integrity") or "?"
    try:
        snapshot_age = time.time() - float((snapshot or {}).get("writtenAt") or 0)
    except (TypeError, ValueError):
        snapshot_age = None
    alive_text = "ALIVE" if age <= 60 else "STALE"
    parts = [f"gateway: {mode} {alive_text} integrity={integrity}",
             f"口座 {beat.get('accounts')}",
             f"heartbeat {age:.0f}s 前"]
    if snapshot_age is not None:
        parts.append(f"観測 {snapshot_age:.0f}s 前")
    parts.extend(_source_parts())
    return " · ".join(parts)


#: 起動を諦める回数と、諦めてから次に試すまで。fill_watch(R91)と同じ考え方。
SPAWN_SUSPEND_AFTER = 3
SPAWN_RETRY_AFTER_SEC = 3600
LOG_ROTATE_BYTES = 5 * 1024 * 1024


def log_path() -> str:
    base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, ".secrets", "broker_gateway.log")


def spawn_state_path() -> str:
    base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, ".secrets", "broker_gateway_spawn.json")


def autostart_enabled(contract: Optional[Dict[str, Any]] = None) -> bool:
    """契約 `gateway.autostart`。**既定 false。**

    自動で起動し直すかどうかを分けてあるのは、**接続が利用者あたり 1 本**だから。
    人が手で張った接続を周期が勝手に切ると、切った側は気づかない。
    `NQX_GATEWAY_AUTOSTART=0` は契約より強い(緊急で止められる)。
    """
    override = str(os.environ.get("NQX_GATEWAY_AUTOSTART") or "").strip().lower()
    if override in ("0", "off", "false", "no"):
        return False
    if override in ("1", "on", "true", "yes"):
        return True
    section = contract
    if section is None:
        try:
            import broker_source
            section = broker_source._contract()
        except Exception:  # noqa: BLE001 — 読めないなら起動しない側へ倒す
            return False
    return bool((section or {}).get("autostart"))


def spawn_detached(log: Optional[str] = None, script: Optional[str] = None) -> int:
    """常駐を親(周期のプロセス)から切り離して起動し、pid を返す。

    `NQX_GATEWAY=1` を子へ渡す —— 既定 OFF の門は `_serve_cli` にあり、
    ここで渡さなければ子は即座に「既定 OFF」と言って終わる。
    """
    import subprocess
    import sys
    target = log or log_path()
    os.makedirs(os.path.dirname(target), exist_ok=True)
    try:
        if os.path.exists(target) and os.path.getsize(target) > LOG_ROTATE_BYTES:
            os.replace(target, target + ".1")
    except OSError:
        pass
    base = os.path.dirname(os.path.abspath(__file__))
    command = [sys.executable, script or os.path.join(base, "broker_gateway.py"), "--serve"]
    env = dict(os.environ, NQX_GATEWAY="1", PYTHONIOENCODING="utf-8",
               PYTHONUTF8="1", PYTHONUNBUFFERED="1")
    with open(target, "ab") as out:
        out.write(f"\n=== {_iso(int(time.time() * 1000))} autostart by nqx_cycle ===\n"
                  .encode("utf-8"))
        out.flush()
        kwargs: Dict[str, Any] = dict(cwd=base, env=env, stdin=subprocess.DEVNULL,
                                      stdout=out, stderr=subprocess.STDOUT, close_fds=True)
        if os.name == "nt":
            detached, new_group, breakaway = 0x00000008, 0x00000200, 0x01000000
            try:
                return subprocess.Popen(
                    command, creationflags=detached | new_group | breakaway, **kwargs).pid
            except OSError:
                return subprocess.Popen(
                    command, creationflags=detached | new_group, **kwargs).pid
        return subprocess.Popen(command, start_new_session=True, **kwargs).pid


def _read_json(path: str) -> Dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def supervise(*, now: Optional[float] = None, settings: Optional[Dict[str, Any]] = None,
              spawn=None, state_path: Optional[str] = None) -> str:
    """`nqx_cycle` の 1 行。生死を出し、`autostart` なら止まった常駐を起動し直す。

    **例外は投げない**(表示と起動の失敗で監視周期を止めない)。止まっていても
    観測は全口座 REST へ退避するだけなので、起動しないこと自体は安全側。
    """
    line = status_line()
    try:
        import broker_source
        if broker_source.mode() == broker_source.OFF:
            return line
        if not autostart_enabled(settings):
            return line
        current = float(now if now is not None else time.time())
        target = state_path or spawn_state_path()
        state = _read_json(target)
        if alive():
            if state.get("pending"):
                write_heartbeat({"pending": 0, "aliveAt": current}, target)
            return line
        if os.path.exists(lock_path()):
            beat_age = current - float(read_heartbeat().get("at") or 0)
            if beat_age < 120:
                return line + " · autostart: 既存プロセスがロックを保持している"
        pending = int(state.get("pending") or 0)
        last = float(state.get("lastSpawnAt") or 0)
        if pending >= SPAWN_SUSPEND_AFTER and (current - last) < SPAWN_RETRY_AFTER_SEC:
            return (line + f" · autostart 停止中: 起動 {pending} 回で heartbeat が出ない"
                           f"(.secrets/broker_gateway.log を確認)")
        pid = (spawn or spawn_detached)()
        write_heartbeat({"pending": pending + 1, "lastSpawnAt": current, "pid": pid}, target)
        return line + f" · autostart: 起動 pid={pid}"
    except Exception as exc:  # noqa: BLE001 — 表示専用・周期を止めない
        return line + f" · autostart failed ({type(exc).__name__})"


def _source_parts() -> List[str]:
    """前周期の出所の内訳(表示専用)。

    影運転で見たいのは「差分が無い」ではなく **「比べた上で差が無い」**。
    比較が成立していない周期は、差分ログが空でも一致の証拠にならない。
    """
    try:
        import broker_source
        row = broker_source.last_cycle()
    except Exception:  # noqa: BLE001 — 表示専用
        return []
    if not isinstance(row, dict):
        return []
    checked = int(row.get("shadowChecked") or 0)
    compared = int(row.get("shadowCompared") or 0)
    if checked:
        text = f"前周期 比較 {compared}/{checked} 差分 {int(row.get('shadowDiverged') or 0)}"
        reasons = row.get("shadowUnavailableReasons") or {}
        if reasons:
            text += "(不成立 " + ",".join(
                f"{name}×{count}" for name, count in sorted(reasons.items())) + ")"
        return [text]
    gateway_count = int(row.get("gateway") or 0)
    fallback = int(row.get("fallback") or 0)
    if gateway_count or fallback:
        text = f"前周期 gateway {gateway_count} / 退避 {fallback}"
        reasons = row.get("fallbackReasons") or {}
        if reasons:
            text += "(" + ",".join(f"{name}×{count}"
                                   for name, count in sorted(reasons.items())) + ")"
        return [text]
    return []

# ---------------------------------------------------------------- CLI(読み取り専用)


def _probe(seconds: float) -> int:
    """実環境の読み取り検証。**注文は送らない。**

        python broker_gateway.py --probe 45
    """
    import asyncio
    import broker_status

    cfg = broker_status.read_env(broker_status.CROSSTRADE_ENV)
    token = cfg.get("CROSSTRADE_API_TOKEN") or cfg.get("CROSSTRADE_KEY")
    if not token:
        print("CROSSTRADE_KEY が無い")
        return 2
    names = [a.strip() for a in str(cfg.get("CROSSTRADE_ACCOUNTS", "")).split(",") if a.strip()]
    accounts = [cfg.get("CROSSTRADE_ACCOUNT_ID_" + name) for name in names]
    accounts = [a for a in accounts if a]
    contract = cfg.get("CROSSTRADE_CONTRACT_ID_" + broker_status.DEFAULT_SYMBOL)
    state = GatewayState(expected_accounts=accounts,
                         contract_ids=[contract] if contract else [])
    gateway = BrokerGateway(transport=AiohttpTransport(WS_URL, token), state=state)

    started = time.monotonic()
    ready = [None]
    inner = gateway._handle

    def traced(frame, now, now_wall):
        verdict = inner(frame, now, now_wall)
        if ready[0] is None and state.integrity == VERIFIED:
            ready[0] = time.monotonic() - started
        return verdict

    gateway._handle = traced
    summary = asyncio.run(gateway.run_once(max_seconds=seconds))

    print("口座 %d / 限月 contractId=%s" % (len(accounts), contract))
    print("検証済み状態が揃うまで: %s"
          % ("%.2f 秒" % ready[0] if ready[0] is not None else "揃わなかった"))
    print("RPC %d 本 / 受信 %d フレーム / 無言 %d 回"
          % (summary["rpcCalls"], summary["frames"], summary["silences"]))
    print("整合性 %s  欠落 %d / dropped %d / 混入 %d / 補充待ち %.2f 秒"
          % (summary["integrity"], summary["seqGaps"], summary["dropped"],
             summary["foreignRows"], summary["budgetWaited"]))
    samples = sorted(gateway.latency_samples)
    if samples:
        def pct(p):
            return samples[min(len(samples) - 1, int(len(samples) * p))]
        print("ブローカー epoch → 受信(秒・時計ずれ込み): 件数 %d p50=%.3f p95=%.3f max=%.3f"
              % (len(samples), pct(0.5), pct(0.95), samples[-1]))
    else:
        print("ブローカー epoch → 受信: **サンプル 0**(この窓でイベントが起きなかった)")
    for account in accounts[:3]:
        view = state.account_view(account, contract or "")
        print("  …%s integrity=%-9s netPos=%-5s 生きた注文 %d 本 src=%s"
              % (str(account)[-4:], view["integrity"], view["netPos"],
                 len(view["liveOrders"]), view["sourceObservedAt"]))
    return 0


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Broker Gateway(既定 OFF・読み取り専用の検証)")
    parser.add_argument("--probe", type=float, metavar="SECONDS",
                        help="実環境へ接続して読み取りだけ検証する(注文は送らない)")
    parser.add_argument("--serve", nargs="?", type=float, const=-1.0, metavar="SECONDS",
                        help="常駐する(NQX_GATEWAY=1 が要る)。秒数を省くと無期限")
    parser.add_argument("--status", action="store_true", help="常駐の生死を 1 行で出す")
    parser.add_argument("--start", action="store_true",
                        help="常駐を切り離したプロセスとして起動する(注文は送らない)")
    parser.add_argument("--stop", action="store_true",
                        help="常駐を止める(heartbeat の pid。誤爆しないよう鮮度を見る)")
    args = parser.parse_args()
    if args.start:
        raise SystemExit(_start_cli())
    if args.stop:
        raise SystemExit(_stop_cli())
    if args.status:
        beat = read_heartbeat()
        if not beat:
            print("broker_gateway: heartbeat が無い(常駐していない)")
        else:
            age = time.time() - float(beat.get("at") or 0)
            print("broker_gateway: %s integrity=%s 口座 %s (heartbeat %.0fs 前 pid=%s)"
                  % ("ALIVE" if age <= 60 else "STALE", beat.get("integrity"),
                     beat.get("accounts"), age, beat.get("pid")))
        raise SystemExit(0)
    if args.serve is not None:
        raise SystemExit(_serve_cli(None if args.serve is not None and args.serve < 0
                                    else args.serve))
    if args.probe is None:
        parser.print_help()
        raise SystemExit(0)
    raise SystemExit(_probe(args.probe))


__all__ = [
    "SCHEMA", "VERIFIED", "UNVERIFIED", "RESYNCING", "WS_URL",
    "AiohttpTransport", "BrokerGateway",
    "SUBSCRIBE_EVENTS", "ORIGIN", "RPC_PER_SEC", "RPC_BURST",
    "GatewayState", "TokenBucket", "order_placement_floor_sec",
    "gateway_enabled", "snapshot_path", "write_snapshot", "read_snapshot",
]

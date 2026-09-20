# -*- coding: utf-8 -*-
"""R118: 模擬ブローカー —— CrossTrade(Tradovate 経路)の応答を返す実 HTTP サーバ。

**なぜ実サーバなのか。** `order.py` は subprocess で動く。プロセス内で関数を
差し替えても子には届かない。`CROSSTRADE_API_BASE` / `CROSSTRADE_URL` を
`127.0.0.1` のこのサーバへ向けると、**送信・照会・送信後の再照会まで本物と同じ
経路**で回せる。外部へは 1 件も出ない。

実装している面(本物と同じ URL 形):

    GET  /accounts                              口座名簿
    GET  /accounts/{id}                         残高
    GET  /accounts/{id}/position?instrument=X   建玉(単数・銘柄で絞る)
    GET  /accounts/{id}/positions               建玉(一覧・R41 の正本だった側)
    GET  /accounts/{id}/orders                  注文(生きている行)
    GET  /accounts/{id}/orders/{orderId}        注文(ID 指定・終端も返す)
    GET  /accounts/{id}/fills                   約定
    POST /                                      PLACE / cancelandbracket / flatteneverything

故障注入(`Fault`)は「どの口座の」「何番目の」「どの操作で」壊すかを指定する。
本物の壊れ方に合わせてある —— 片脚だけ拒否、応答を返さない(不明)、送信を
受け取ってから落ちる(送信済み・不明)、429、遅延。
"""
from __future__ import annotations

import json
import re
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional

CONTRACT_ID = "4470324"
SYMBOL = "MNQZ6"

ACTIVE = "Working"
FILLED = "Filled"
CANCELED = "Canceled"
REJECTED = "Rejected"


class Fault:
    """1 回だけ効く故障。`matches` が真になった呼び出しで発火する。"""

    def __init__(self, *, kind: str, account: Optional[str] = None,
                 operation: Optional[str] = None, leg: Optional[int] = None,
                 nth: int = 1, status: int = 500, delay: float = 0.0,
                 repeat: int = 1):
        self.kind = kind          # reject / unknown / crash_before / crash_after / http / delay
        self.account = account
        self.operation = operation
        self.leg = leg
        self.nth = max(1, int(nth))
        self.status = status
        self.delay = delay
        self.remaining = max(1, int(repeat))
        self.seen = 0
        self.fired = 0

    def matches(self, *, account: str, operation: str, leg: Optional[int]) -> bool:
        if self.remaining <= 0:
            return False
        if self.account is not None and str(self.account) != str(account):
            return False
        if self.operation is not None and self.operation != operation:
            return False
        if self.leg is not None and self.leg != leg:
            return False
        self.seen += 1
        if self.seen < self.nth:
            return False
        self.remaining -= 1
        self.fired += 1
        return True


class MockBroker:
    """口座ごとの建玉・注文の状態。**スレッド安全**(サーバが並行で叩く)。"""

    def __init__(self, accounts: List[str], *, symbol: str = SYMBOL,
                 contract_id: str = CONTRACT_ID, balance: float = 50000.0):
        self.symbol = symbol
        self.contract_id = contract_id
        self.accounts = [str(value) for value in accounts]
        self.lock = threading.RLock()
        self.state: Dict[str, Dict[str, Any]] = {
            account: {"netPos": 0, "netPrice": None, "orders": {}, "fills": [],
                      "balance": balance, "realizedPnL": 0.0}
            for account in self.accounts
        }
        self.faults: List[Fault] = []
        self.requests: List[Dict[str, Any]] = []
        self.sends: List[Dict[str, Any]] = []
        self._next_id = 1000
        #: 故障で「受け取ったが返事をしない」ときに使う待ち時間。
        self.unknown_hang_sec = 0.2
        #: 履歴の上限。**試験の足場が自分で太らないように。** 本物のブローカーは
        #: 古い注文と約定を返し続けないし、ここが無いと持続負荷試験のメモリ計測が
        #: 「模擬の蓄積」を測ってしまう(2026-09-19、2 時間試験の 20 分時点で判明)。
        self.max_history = 400
        #: 1 リクエストあたりの往復時間(秒)。**既定 0**。
        #: 本番の CrossTrade は 1 本 0.6 秒前後(2026-09-19 実測)。並行化の効きは
        #: この値に支配されるので、測るときは必ず値を明示して報告する。
        self.latency_sec = 0.0

    # -- 補助 --------------------------------------------------------------
    def _new_id(self) -> str:
        with self.lock:
            self._next_id += 1
            return str(self._next_id)

    def add_fault(self, fault: Fault) -> Fault:
        self.faults.append(fault)
        return fault

    def _fire(self, *, account: str, operation: str, leg: Optional[int] = None) -> Optional[Fault]:
        for fault in self.faults:
            if fault.matches(account=account, operation=operation, leg=leg):
                return fault
        return None

    def count(self, pattern: str = "") -> int:
        """`pattern`(正規表現)に当たったリクエストの本数。"""
        if not pattern:
            return len(self.requests)
        matcher = re.compile(pattern)
        return sum(1 for row in self.requests if matcher.search(row["path"]))

    def counts_by_kind(self) -> Dict[str, int]:
        buckets: Dict[str, int] = {}
        for row in self.requests:
            buckets[row["kind"]] = buckets.get(row["kind"], 0) + 1
        return buckets

    def reset_counts(self) -> None:
        with self.lock:
            self.requests = []

    # -- 状態の操作(試験から呼ぶ) ------------------------------------------
    def fill_entry(self, account: str, side: str, qty: int, price: float) -> None:
        """ENTRY の約定。保護注文の 2 脚を生きた行として置く。"""
        with self.lock:
            row = self.state[str(account)]
            signed = qty if side.upper() in {"BUY", "LONG"} else -qty
            row["netPos"] += signed
            row["netPrice"] = price
            for order_id, order in row["orders"].items():
                if order["purpose"] == "ENTRY" and order["ordStatus"] == ACTIVE:
                    order["ordStatus"] = FILLED
                    order["filledPrice"] = price
                    row["fills"].append({"orderId": order_id, "qty": order["orderQty"],
                                         "price": price, "timestamp": _now_iso()})

    def fill_protective(self, account: str, order_id: str, price: float) -> None:
        """保護注文の 1 本が約定した(TP1 など)。OCO の相方は取り消される。"""
        with self.lock:
            row = self.state[str(account)]
            order = row["orders"].get(str(order_id))
            if order is None:
                raise KeyError(order_id)
            order["ordStatus"] = FILLED
            order["filledPrice"] = price
            qty = int(order.get("orderQty") or 1)
            direction = 1 if str(order.get("action")).upper() == "BUY" else -1
            row["netPos"] += direction * qty
            row["fills"].append({"orderId": str(order_id), "qty": qty, "price": price,
                                 "timestamp": _now_iso()})
            sibling = order.get("ocoId")
            if sibling and sibling in row["orders"]:
                if row["orders"][sibling]["ordStatus"] == ACTIVE:
                    row["orders"][sibling]["ordStatus"] = CANCELED

    def protective_ids(self, account: str) -> List[str]:
        with self.lock:
            return [order_id for order_id, order in self.state[str(account)]["orders"].items()
                    if order["purpose"] == "PROTECTIVE" and order["ordStatus"] == ACTIVE]

    def live_order_rows(self, account: str) -> List[Dict[str, Any]]:
        with self.lock:
            return [dict(order) for order in self.state[str(account)]["orders"].values()
                    if order["ordStatus"] == ACTIVE]

    # -- 送信 --------------------------------------------------------------
    def place(self, fields: Dict[str, str]) -> Dict[str, Any]:
        account = str(fields.get("account") or "")
        command = str(fields.get("command") or "").upper()
        if account not in self.state:
            return {"status": 400, "body": {"success": False, "error": "unknown account"}}

        leg = None
        with self.lock:
            self.sends.append({"at": time.time(), "account": account,
                               "command": command, "fields": dict(fields)})
            # 送信履歴も上限を付ける。持続負荷試験で **足場自身が太る**のを防ぐ
            # (2 時間で 2 万件・約 9MB になり、メモリ計測が Gateway ではなく
            # 足場を測ってしまっていた)。試験が数えるのは最大 40 本。
            if len(self.sends) > self.max_history * 10:
                del self.sends[:len(self.sends) - self.max_history * 10]
            leg = sum(1 for row in self.sends
                      if row["account"] == account and row["command"] == command)

        fault = self._fire(account=account, operation=command, leg=leg)
        if fault is not None:
            if fault.kind == "crash_before":
                # 受け取る前に落ちる = **送っていない**。接続を切る。
                return {"status": None, "body": None, "hangup": True, "applied": False}
            if fault.kind == "delay":
                time.sleep(fault.delay)
            if fault.kind == "http":
                return {"status": fault.status,
                        "body": {"success": False, "error": "injected"}}
            if fault.kind == "reject":
                return {"status": 200,
                        "body": {"success": False, "error": "order rejected"}}

        if command == "PLACE":
            result = self._do_place(account, fields)
        elif command == "CANCELANDBRACKET":
            result = self._do_rebracket(account, fields)
        elif command == "FLATTENEVERYTHING":
            result = self._do_flatten(account)
        else:
            return {"status": 400, "body": {"success": False, "error": "unknown command"}}

        if fault is not None and fault.kind in {"unknown", "crash_after"}:
            # **受け取って適用した後**に返事をしない。送信側からは不明に見える。
            time.sleep(self.unknown_hang_sec)
            return {"status": None, "body": None, "hangup": True, "applied": True}
        return result

    def _do_place(self, account: str, fields: Dict[str, str]) -> Dict[str, Any]:
        qty = int(float(fields.get("qty") or 1))
        side = str(fields.get("action") or "BUY").upper()
        order_type = str(fields.get("order_type") or "MARKET").upper()
        entry_id = self._new_id()
        with self.lock:
            row = self.state[account]
            row["orders"][entry_id] = _order_row(
                entry_id, account, self.contract_id, side, qty, order_type,
                purpose="ENTRY", price=_float(fields.get("limit_price")))
            stop_id, target_id = self._new_id(), self._new_id()
            opposite = "SELL" if side == "BUY" else "BUY"
            row["orders"][stop_id] = _order_row(
                stop_id, account, self.contract_id, opposite, qty, "STOP",
                purpose="PROTECTIVE", price=_float(fields.get("stop_loss")),
                parent=entry_id, oco=target_id)
            row["orders"][target_id] = _order_row(
                target_id, account, self.contract_id, opposite, qty, "LIMIT",
                purpose="PROTECTIVE", price=_float(fields.get("take_profit")),
                parent=entry_id, oco=stop_id)
        return {"status": 200, "body": {"success": True,
                                        "data": {"orderId": entry_id,
                                                 "bracket": [stop_id, target_id]}}}

    def _do_rebracket(self, account: str, fields: Dict[str, str]) -> Dict[str, Any]:
        with self.lock:
            row = self.state[account]
            for order in row["orders"].values():
                if order["purpose"] == "PROTECTIVE" and order["ordStatus"] == ACTIVE:
                    order["ordStatus"] = CANCELED
            qty = int(float(fields.get("qty") or 1))
            side = str(fields.get("action") or "BUY").upper()
            opposite = "SELL" if side == "BUY" else "BUY"
            stop_id, target_id = self._new_id(), self._new_id()
            row["orders"][stop_id] = _order_row(
                stop_id, account, self.contract_id, opposite, qty, "STOP",
                purpose="PROTECTIVE", price=_float(fields.get("stop_loss")), oco=target_id)
            row["orders"][target_id] = _order_row(
                target_id, account, self.contract_id, opposite, qty, "LIMIT",
                purpose="PROTECTIVE", price=_float(fields.get("take_profit")), oco=stop_id)
        return {"status": 200, "body": {"success": True,
                                        "data": {"orderId": stop_id,
                                                 "bracket": [stop_id, target_id]}}}

    def _prune(self, row: Dict[str, Any]) -> None:
        """終端した注文と古い約定を捨てる。**生きている注文は落とさない。**"""
        terminal = [key for key, order in row["orders"].items()
                    if order["ordStatus"] != ACTIVE]
        for key in terminal[:max(0, len(terminal) - self.max_history)]:
            row["orders"].pop(key, None)
        if len(row["fills"]) > self.max_history:
            del row["fills"][:len(row["fills"]) - self.max_history]

    def _do_flatten(self, account: str) -> Dict[str, Any]:
        with self.lock:
            row = self.state[account]
            for order in row["orders"].values():
                if order["ordStatus"] == ACTIVE:
                    order["ordStatus"] = CANCELED
            row["netPos"] = 0
            row["netPrice"] = None
            self._prune(row)
        return {"status": 200, "body": {"success": True, "data": {"flattened": True}}}

    # -- 照会 --------------------------------------------------------------
    def position_singular(self, account: str, instrument: str) -> Dict[str, Any]:
        with self.lock:
            row = self.state[str(account)]
            return {"success": True, "data": {
                "account": account, "accountId": account, "symbol": instrument,
                "contractId": self.contract_id, "netPos": row["netPos"],
                "position": {"netPrice": row["netPrice"], "timestamp": _now_iso()}}}

    def positions_plural(self, account: str) -> Dict[str, Any]:
        with self.lock:
            row = self.state[str(account)]
            rows = []
            if row["netPos"]:
                rows.append({"accountId": account, "account": account,
                             "contractId": self.contract_id, "symbol": None,
                             "netPos": row["netPos"], "netPrice": row["netPrice"],
                             "timestamp": _now_iso()})
            return {"success": True, "data": rows}

    def orders_collection(self, account: str) -> Dict[str, Any]:
        with self.lock:
            rows = [_public_order(order) for order in self.state[str(account)]["orders"].values()
                    if order["ordStatus"] == ACTIVE]
            return {"success": True, "data": rows}

    def order_detail(self, account: str, order_id: str) -> Dict[str, Any]:
        with self.lock:
            order = self.state[str(account)]["orders"].get(str(order_id))
            if order is None:
                return {"__status": 400, "success": False, "error": "tradovate_rejected"}
            return {"success": True, "data": _public_order(order)}

    def balance(self, account: str) -> Dict[str, Any]:
        with self.lock:
            row = self.state[str(account)]
            return {"success": True, "data": {"balance": {
                "netLiq": row["balance"], "netLiqSOD": row["balance"],
                "realizedPnL": row["realizedPnL"]}}}

    def fills(self, account: str) -> Dict[str, Any]:
        with self.lock:
            return {"success": True, "data": list(self.state[str(account)]["fills"])}

    def accounts_list(self) -> Dict[str, Any]:
        return {"success": True,
                "data": [{"id": account, "name": account, "accountId": account,
                          "status": "ACTIVE", "active": True}
                         for account in self.accounts]}

    # -- WebSocket 用の材料 ------------------------------------------------
    def ws_positions(self) -> List[Dict[str, Any]]:
        """`Tv_ListPositions` の形(全口座を 1 本で)。"""
        with self.lock:
            return [{"environment": "live", "name": "mock", "userId": 1,
                     "data": [{"accountId": account, "contractId": self.contract_id,
                               "netPos": row["netPos"], "netPrice": row["netPrice"],
                               "timestamp": _now_iso()}
                              for account, row in self.state.items() if row["netPos"]]}]

    def ws_orders(self) -> List[Dict[str, Any]]:
        with self.lock:
            return [{"environment": "live", "name": "mock", "userId": 1,
                     "data": [_public_order(order)
                              for row in self.state.values()
                              for order in row["orders"].values()
                              if order["ordStatus"] == ACTIVE]}]


# ------------------------------------------------------------------ 行の形

def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _order_row(order_id: str, account: str, contract_id: str, action: str,
               qty: int, order_type: str, *, purpose: str,
               price: Optional[float] = None, parent: Optional[str] = None,
               oco: Optional[str] = None) -> Dict[str, Any]:
    return {"id": order_id, "accountId": account, "contractId": contract_id,
            "ordStatus": ACTIVE, "action": action, "orderQty": qty,
            "orderType": order_type, "price": price, "purpose": purpose,
            "parentId": parent, "ocoId": oco, "linkedId": None,
            "filledPrice": None, "timestamp": _now_iso()}


def _public_order(order: Dict[str, Any]) -> Dict[str, Any]:
    """`purpose` は模擬側の都合。ブローカーは返さないので外へは出さない。"""
    public = {key: value for key, value in order.items() if key != "purpose"}
    if order.get("orderType") == "STOP":
        public["stopPrice"] = order.get("price")
    else:
        public["limitPrice"] = order.get("price")
    return public


# ------------------------------------------------------------------ サーバ

class _Handler(BaseHTTPRequestHandler):
    broker: MockBroker = None  # type: ignore[assignment]
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):  # noqa: D102 — 標準出力を汚さない
        return

    # -- 記録 --------------------------------------------------------------
    def _note(self, kind: str) -> None:
        with self.broker.lock:
            self.broker.requests.append({"at": time.time(), "path": self.path,
                                         "kind": kind, "method": self.command})
        if self.broker.latency_sec:
            time.sleep(self.broker.latency_sec)

    def _send(self, status: int, payload: Any) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # -- GET ---------------------------------------------------------------
    def do_GET(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        parts = [value for value in parsed.path.strip("/").split("/") if value]
        # 先頭の /v1/api/tv は base に含まれる。落として揃える。
        while parts and parts[0] in {"v1", "api", "tv"}:
            parts.pop(0)
        query = urllib.parse.parse_qs(parsed.query)

        if parts == ["accounts"]:
            self._note("accounts")
            return self._send(200, self.broker.accounts_list())
        if len(parts) >= 2 and parts[0] == "accounts":
            account = urllib.parse.unquote(parts[1])
            if account not in self.broker.state:
                self._note("unknown")
                return self._send(400, {"success": False, "error": "unknown account"})
            if len(parts) == 2:
                self._note("balance")
                return self._send(200, self.broker.balance(account))
            tail = parts[2]
            if tail == "position":
                self._note("position")
                instrument = (query.get("instrument") or [self.broker.symbol])[0]
                return self._send(200, self.broker.position_singular(account, instrument))
            if tail == "positions":
                self._note("positions")
                return self._send(200, self.broker.positions_plural(account))
            if tail == "orders" and len(parts) == 3:
                self._note("orders")
                return self._send(200, self.broker.orders_collection(account))
            if tail == "orders" and len(parts) == 4:
                self._note("order_detail")
                payload = self.broker.order_detail(account, urllib.parse.unquote(parts[3]))
                status = payload.pop("__status", 200)
                return self._send(status, payload)
            if tail == "fills":
                self._note("fills")
                return self._send(200, self.broker.fills(account))
        self._note("notfound")
        return self._send(404, {"success": False, "error": "not found"})

    # -- POST --------------------------------------------------------------
    def do_POST(self):  # noqa: N802
        self._note("send")
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8", "replace") if length else ""
        fields: Dict[str, str] = {}
        for chunk in raw.replace("\n", ";").split(";"):
            if "=" in chunk:
                key, _, value = chunk.partition("=")
                fields[key.strip()] = value.strip()
        result = self.broker.place(fields)
        if result.get("hangup"):
            # 返事を書かずに接続を閉じる。呼び出し側からは **送ったか分からない**。
            self.close_connection = True
            return
        return self._send(int(result["status"]), result["body"])


class MockBrokerServer:
    """`with MockBrokerServer(broker) as server:` で使う。`server.base` が API の根。"""

    def __init__(self, broker: MockBroker, host: str = "127.0.0.1", port: int = 0):
        self.broker = broker
        handler = type("_BoundHandler", (_Handler,), {"broker": broker})
        self.httpd = ThreadingHTTPServer((host, port), handler)
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       kwargs={"poll_interval": 0.05}, daemon=True)

    @property
    def origin(self) -> str:
        host, port = self.httpd.server_address[:2]
        return f"http://{host}:{port}"

    @property
    def base(self) -> str:
        return self.origin + "/v1/api/tv"

    def __enter__(self) -> "MockBrokerServer":
        self.thread.start()
        return self

    def __exit__(self, *_exc) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


def env_for(server: MockBrokerServer, accounts: List[str], *,
            symbol: str = SYMBOL, contract_id: str = CONTRACT_ID) -> Dict[str, str]:
    """`.secrets/crosstrade.env` の代わりに使う設定。**本番の値は 1 つも含まない。**"""
    env = {
        "CROSSTRADE_KEY": "mock-key",
        "CROSSTRADE_API_TOKEN": "mock-token",
        "CROSSTRADE_API_BASE": server.base,
        "CROSSTRADE_URL": server.origin + "/",
        "CROSSTRADE_DEST": "mock-dest",
        "CROSSTRADE_PLATFORM": "TRADOVATE",
        "CROSSTRADE_ACCOUNTS": ",".join(accounts),
        "CROSSTRADE_CONTRACT_ID_" + re.sub(r"[^A-Za-z0-9]", "_", symbol): contract_id,
        "MARKET_SLIPPAGE_PT": "10",
    }
    for account in accounts:
        env[f"RISK_{account}"] = "5000"
        env[f"CROSSTRADE_ACCOUNT_ID_{account}"] = account
        env[f"TRAILING_DD_{account}"] = "2000"
        env[f"LIFELINE_{account}"] = "2000"
    return env

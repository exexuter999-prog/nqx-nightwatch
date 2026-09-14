# -*- coding: utf-8 -*-
"""CrossTrade 建玉照会アダプタの検証。

ローカルにモックサーバーを立て、CrossTrade のドキュメントに載っている
レスポンス形をそのまま返させる。**本物の CrossTrade には一切接続しない。**
発注もしない(このファイルは order.py を import すらしない)。

    python tests/test_broker_crosstrade.py
"""
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

for _stream in ("stdout", "stderr"):
    _file = getattr(sys, _stream, None)
    if _file is not None and hasattr(_file, "reconfigure"):
        try:
            _file.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

import broker_status  # noqa: E402

PASS = [0]
FAIL = [0]


def check(label, condition, detail=""):
    if condition:
        PASS[0] += 1
        print(f"  OK   {label}")
    else:
        FAIL[0] += 1
        print(f"  NG   {label}  {detail}")


# ---------------------------------------------------------------- モックサーバー

RESPONSE = {"status": 200, "body": {}}
REQUESTS = []


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        REQUESTS.append({
            "path": parsed.path,
            "query": parse_qs(parsed.query),
            "auth": self.headers.get("Authorization"),
        })
        payload = json.dumps(RESPONSE["body"]).encode("utf-8")
        self.send_response(RESPONSE["status"])
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


server = HTTPServer(("127.0.0.1", 0), Handler)
PORT = server.server_address[1]
threading.Thread(target=server.serve_forever, daemon=True).start()

CFG = {
    "CROSSTRADE_API_TOKEN": "mock-bearer-token",
    "CROSSTRADE_ACCOUNT": "LFE0256231671000X",
    "CROSSTRADE_API_BASE": f"http://127.0.0.1:{PORT}/v1/api/tv",
}


def query(body, status=200, cfg=None):
    RESPONSE["status"], RESPONSE["body"] = status, body
    REQUESTS.clear()
    adapter = broker_status.CrossTradeAdapter(cfg or CFG)
    try:
        return adapter.query("MNQU6"), None
    except broker_status.Unavailable as exc:
        return None, str(exc)


# ================================================================
print("=" * 68)
print("1. リクエストの組み立て")
print("=" * 68)

result, err = query({"success": True, "data": {"account": "LFE0256231671000X",
                                               "symbol": "MNQU6", "netPos": 0, "position": {}}})
req = REQUESTS[0]
check("パスは /accounts/{account}/position",
      req["path"] == "/v1/api/tv/accounts/LFE0256231671000X/position", req["path"])
check("instrument をクエリで渡す", req["query"].get("instrument") == ["MNQU6"], str(req["query"]))
check("Bearer トークンを付ける", req["auth"] == "Bearer mock-bearer-token", str(req["auth"]))

print()
print("=" * 68)
print("2. FLAT の判定")
print("=" * 68)

check("netPos=0 は verified な FLAT", result and result["verified"] and result["qty"] == 0, str(result))
check("closedAt を返す(CLOSED 遷移に必要)", result and result.get("closedAt"), str(result))
check("source が記録される", result and result["source"] == "crosstrade-rest", str(result))

print()
print("=" * 68)
print("3. 建玉あり")
print("=" * 68)

result, err = query({"success": True, "data": {
    "account": "LFE0256231671000X", "symbol": "MNQU6", "netPos": 2,
    "position": {"netPrice": 30190.5, "timestamp": "2026-08-14T01:00:00Z"}}})
check("netPos>0 は LONG", result and result["side"] == "LONG" and result["qty"] == 2, str(result))
check("平均建値を拾う", result and result["avgEntry"] == 30190.5, str(result))
check("約定時刻を拾う", result and result["filledAt"] == "2026-08-14T01:00:00Z", str(result))

result, err = query({"success": True, "data": {
    "account": "LFE0256231671000X", "symbol": "MNQU6", "netPos": -3, "position": {}}})
check("netPos<0 は SHORT", result and result["side"] == "SHORT" and result["qty"] == 3, str(result))
check("平均建値が無ければ None(捏造しない)", result and result["avgEntry"] is None, str(result))
check("それでも verified=True(建玉の存在は確認できている)", result and result["verified"], str(result))

print()
print("=" * 68)
print("4. 異常系は全て Unavailable(FLAT と誤解しない)")
print("=" * 68)

cases = [
    ("success=false", {"success": False, "error": "bad token"}, 200),
    ("data が配列", {"success": True, "data": []}, 200),
    ("data が無い", {"success": True}, 200),
    ("netPos が無い", {"success": True, "data": {"symbol": "MNQU6"}}, 200),
    ("netPos が数値でない", {"success": True, "data": {"symbol": "MNQU6", "netPos": "x"}}, 200),
    ("別シンボルが返る", {"success": True, "data": {"symbol": "MESU6", "netPos": 0}}, 200),
    ("HTTP 401", {"error": "unauthorized"}, 401),
    ("HTTP 500", {"error": "boom"}, 500),
]
for label, body, status in cases:
    result, err = query(body, status)
    check(f"{label} → 照会不能として扱う", result is None and err, f"result={result}")

print()
print("=" * 68)
print("5. 未設定と全体の解決")
print("=" * 68)

adapter = broker_status.CrossTradeAdapter({})
check("トークンが無ければ configured=False", adapter.configured is False)

adapter = broker_status.CrossTradeAdapter({"CROSSTRADE_API_TOKEN": "t"})
check("account が無ければ configured=False", adapter.configured is False)

names = [a.name for a in broker_status.adapters()]
check("CrossTrade が Tradovate より先に試される",
      names.index("crosstrade-rest") < names.index("tradovate-rest"), str(names))

# アダプタが1つも設定されていなければ verified=False で、qty は返さない。
# 以前はここで実際の .secrets を読む query_position を呼んでいたが、資格情報が
# 入っていない環境でしか通らず、入れた途端に落ちる。しかもこの suite で唯一
# ネットワークに出ていた。不変条件だけを空 cfg で検証する。
_real_adapters = broker_status.adapters
broker_status.adapters = lambda: [
    broker_status.CrossTradeAdapter({}), broker_status.TradovateAdapter({})
]
try:
    live = broker_status.query_position("MNQU6")
finally:
    broker_status.adapters = _real_adapters
check("アダプタ未設定なら verified=False で qty を返さない",
      live["verified"] is False and "qty" not in live, str(live))

# 逆側: 資格情報が揃っていれば configured になる(CROSSTRADE_KEY を流用する)
check("CROSSTRADE_KEY だけでも照会は configured になる",
      broker_status.CrossTradeAdapter(
          {"CROSSTRADE_KEY": "k", "CROSSTRADE_ACCOUNT": "A1"}).configured is True)
check("CROSSTRADE_ACCOUNTS の先頭が照会先になる",
      broker_status.CrossTradeAdapter(
          {"CROSSTRADE_KEY": "k", "CROSSTRADE_ACCOUNTS": "A9, A8",
           "CROSSTRADE_ACCOUNT": "A1"}).account == "A9")


# ================================================================
print()
print("=" * 68)
print("R38. 口座一覧の照会")
print("=" * 68)


def list_accounts(body, status=200, cfg=None):
    RESPONSE["status"], RESPONSE["body"] = status, body
    REQUESTS.clear()
    adapter = broker_status.CrossTradeAdapter(cfg or CFG)
    try:
        return adapter.list_accounts(), None
    except broker_status.Unavailable as exc:
        return None, str(exc)


LIVE_ROWS = [
    {"accountId": "90000001", "environment": "demo", "name": "LTA_A", "active": True},
    {"accountId": "90000002", "environment": "demo", "name": "LTA_B", "active": True},
    {"name": "GONE", "status": "unknown_account"},
]

rows, err = list_accounts({"success": True, "data": LIVE_ROWS})
req = REQUESTS[0]
check("パスは /accounts", req["path"] == "/v1/api/tv/accounts", req["path"])
check("Bearer トークンを付ける", req["auth"] == "Bearer mock-bearer-token", str(req["auth"]))
check("3 行を正規化して返す", rows is not None and len(rows) == 3, str(err))
check("発注先ラベルを name から取る",
      rows and rows[0]["id"] == "LTA_A" and rows[0]["accountId"] == "90000001", str(rows))
check("unknown_account は usable=False",
      rows and rows[2]["usable"] is False, str(rows and rows[2]))
check("生の行を必ず残す", rows and rows[0]["raw"] == LIVE_ROWS[0], str(rows and rows[0]))

# 口座IDがまだ分からないときに使う照会なので、口座未設定でも通ること
rows, err = list_accounts({"success": True, "data": LIVE_ROWS},
                          cfg={"CROSSTRADE_API_TOKEN": "mock-bearer-token",
                               "CROSSTRADE_API_BASE": f"http://127.0.0.1:{PORT}/v1/api/tv"})
check("口座未設定でも一覧は取れる(configured に依存しない)",
      rows is not None and len(rows) == 3, str(err))

# fail-closed: 形が違う・success が true でない・HTTP エラーは「口座なし」にしない
_, err = list_accounts({"success": True, "data": {"nope": 1}})
check("口座リストが無ければ Unavailable", err is not None and "account list" in err, str(err))
_, err = list_accounts({"success": False, "error": "bad token"})
check("success が true でなければ Unavailable", err is not None, str(err))
_, err = list_accounts({}, status=500)
check("HTTP エラーは Unavailable", err is not None and "HTTP 500" in err, str(err))

# {"data": {"accounts": [...]}} で包まれた形も受ける
rows, err = list_accounts({"success": True, "data": {"accounts": LIVE_ROWS}})
check("accounts で包まれた形も受ける", rows is not None and len(rows) == 3, str(err))

# query_accounts は失敗を「口座が無い」と誤解させない
_real_adapters = broker_status.adapters
try:
    broker_status.adapters = lambda: []
    out = broker_status.query_accounts()
finally:
    broker_status.adapters = _real_adapters
check("アダプタが無ければ verified=False で accounts は空",
      out["verified"] is False and out["accounts"] == [] and out["usable"] == [], str(out))

server.shutdown()

print()
print("=" * 68)
print(f"合計: PASS {PASS[0]} / FAIL {FAIL[0]}")
print("本物の CrossTrade への接続: 0 回(全てローカルモック)")
print("=" * 68)
sys.exit(1 if FAIL[0] else 0)

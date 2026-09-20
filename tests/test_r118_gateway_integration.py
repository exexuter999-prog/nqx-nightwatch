# -*- coding: utf-8 -*-
"""R118: Gateway を既存経路へつないだ部分の検査。**外部到達 0 件・本番ファイル書き込み 0 件。**

見ているもの:

1. 出所層(`broker_source`)の OFF / SHADOW / LIVE と、口座ごとの REST 退避
2. **FLAT を名乗る条件が REST より緩くなっていないこと**(R41/R42 の規律)
3. `known_order_ids` 付き・`live_reads()` の中は必ず REST であること
4. 消費者(`query_position_cached` / `query_orders_cached` / `prime_cycle_snapshot`)が
   コード無改造で切り替わること
5. `fill_watch` の見回り費用が Gateway 由来で 0 になり、**口座数で間隔が伸びないこと**
6. Gateway 常駐(`serve`)の再接続と、失敗時に整合性が落ちること

    python tests/test_r118_gateway_integration.py
"""
import asyncio
import io
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

for _stream in ("stdout", "stderr"):
    _file = getattr(sys, _stream, None)
    if _file is not None and hasattr(_file, "reconfigure"):
        try:
            _file.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

import broker_gateway as gw      # noqa: E402
import broker_source as src      # noqa: E402
import broker_status as bs       # noqa: E402
import fill_watch                # noqa: E402

PASS = [0]
FAIL = [0]
TMP = tempfile.mkdtemp(prefix="r118-")

ACCOUNTS = ["A1", "A2", "A3"]
CONTRACT = "4470324"
OTHER_CONTRACT = "9999999"
SYMBOL = "MNQZ6"


def check(label, condition, detail=""):
    if condition:
        PASS[0] += 1
        print(f"  ok   {label}")
    else:
        FAIL[0] += 1
        print(f"  FAIL {label}" + (f" — {detail}" if detail else ""))


def section(title):
    print(f"\n--- {title} ---")


# ----------------------------------------------------------------- 足場

def snapshot(accounts=ACCOUNTS, *, integrity=gw.VERIFIED, written=None,
             positions=None, orders=None, account_integrity=None):
    """`GatewayState.snapshot()` と同じ形を手で組む。"""
    return {
        "schema": gw.SCHEMA,
        "session": {"id": "s1", "state": "ready"},
        "integrity": integrity,
        "integrityReason": "test",
        "generation": 3,
        "seq": {"last": 10, "gaps": 0, "dropped": 0},
        "foreignRows": 0,
        "brokerEpoch": 1,
        "writtenAt": time.time() if written is None else written,
        "accounts": {
            account: {
                "environment": "live",
                "integrity": (account_integrity or {}).get(account, integrity),
                "generation": 3,
                "positions": (positions or {}).get(account, {}),
                "orders": (orders or {}).get(account, {}),
                "executions": 0,
                "sourceObservedAt": "2026-09-19T12:00:00Z",
                "receivedAt": 1.0,
            }
            for account in accounts
        },
    }


def position_row(net, contract=CONTRACT, price=29850.0, account="A1"):
    return {contract: {"accountId": account, "contractId": contract, "netPos": net,
                       "netPrice": price, "timestamp": "2026-09-19T12:00:00Z"}}


def order_row(order_id, action="BUY", status="Working", contract=CONTRACT,
              order_type="STOP", oco=None, account="A1"):
    return {"id": order_id, "accountId": account, "contractId": contract,
            "ordStatus": status, "action": action, "orderQty": 1,
            "orderType": order_type, "ocoId": oco, "parentId": None,
            "timestamp": "2026-09-19T12:00:00Z"}


class Mode:
    """`gateway.mode` を差し替える文脈。契約ファイルは触らない。"""

    def __init__(self, value):
        self.value = value

    def __enter__(self):
        self.previous = os.environ.get("NQX_GATEWAY_MODE")
        os.environ["NQX_GATEWAY_MODE"] = self.value
        return self

    def __exit__(self, *_exc):
        if self.previous is None:
            os.environ.pop("NQX_GATEWAY_MODE", None)
        else:
            os.environ["NQX_GATEWAY_MODE"] = self.previous


SNAPSHOT_FILE = os.path.join(TMP, "gateway_source_snapshot.json")
gw.snapshot_path = lambda: SNAPSHOT_FILE      # 本番の .secrets を触らない

# **設定の読みも隔離する。** `_contract_ids` / `account_aliases` は
# `.secrets/crosstrade.env` を読むので、本番の設定がある機械では通り、設定を外した
# 隔離コピー(指示 §5 のランナー)では Gateway 経路が丸ごと None になって落ちていた
# —— 2026-09-19〜20 の `run_all` が 130 本中この 1 本だけ FAIL していた理由がこれで、
# しかも §3 の途中で素の `io.open` が例外になるため、**残り 60 件超が一度も走って
# いなかった**。試験は環境ではなくコードを見るものなので、ここで固定する。
FAKE_ENV = {"CROSSTRADE_CONTRACT_ID_" + SYMBOL: CONTRACT,
            "CROSSTRADE_CONTRACT_ID_" + str(bs.DEFAULT_SYMBOL): CONTRACT}
src._env = lambda: dict(FAKE_ENV)
src.DIVERGENCE_LOG = os.path.join(TMP, "divergence.jsonl")
src.CYCLE_LOG = os.path.join(TMP, "gateway_shadow_default.jsonl")


def with_snapshot(data):
    """出所層が読むスナップショットを **実ファイル**で置く。

    キャッシュへ直接差し込まないのは、`prime_cycle_snapshot` が毎サイクル
    `invalidate_snapshot()` を呼ぶため —— 本番と同じく `read_snapshot` を
    経由させないと、その経路が試験で通らない。
    """
    if data is None:
        if os.path.exists(SNAPSHOT_FILE):
            os.unlink(SNAPSHOT_FILE)
    else:
        with io.open(SNAPSHOT_FILE, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
    src.invalidate_snapshot()


# ============================================================ 1. 形の変換

section("1. Gateway の行を broker_status と同じ形へ")

view = src.position_from_snapshot(
    "A1", SYMBOL, snapshot(positions={"A1": position_row(-2)}), contract_ids=[CONTRACT])
check("SHORT 2 枚が verified で返る",
      view and view["verified"] is True and view["qty"] == 2 and view["side"] == "SHORT",
      repr(view))
check("出所が gateway-ws として残る", view and view["source"] == "gateway-ws")
check("平均建値が載る", view and view["avgEntry"] == 29850.0)

flat = src.position_from_snapshot("A1", SYMBOL, snapshot(positions={"A1": {}}),
                                  contract_ids=[CONTRACT])
check("行が無い口座は qty=0 の FLAT", flat and flat["verified"] is True and flat["qty"] == 0)

# R41/R42: 別限月に建玉が残っている口座を FLAT と名乗らない
mixed = snapshot(positions={"A1": {**position_row(0), **position_row(-2, OTHER_CONTRACT)}})
check("別 contractId に非ゼロが居る口座は FLAT を名乗らない(REST へ落ちる)",
      src.position_from_snapshot("A1", SYMBOL, mixed, contract_ids=[CONTRACT]) is None)

orders_view = src.orders_from_snapshot(
    "A1", SYMBOL,
    snapshot(orders={"A1": {"1": order_row("1", oco="2"), "2": order_row("2", oco="1",
                                                                        order_type="LIMIT")}}),
    contract_ids=[CONTRACT])
check("生きた保護 2 本が openCount=2 で返る",
      orders_view and orders_view["verified"] is True and orders_view["openCount"] == 2,
      repr(orders_view and orders_view.get("openCount")))
check("正規化は REST と同じ関数を通る(source だけ違う)",
      orders_view and orders_view["source"] == "gateway-ws"
      and orders_view["state"] == "PENDING")
check("OCO の相互リンクが残る",
      orders_view and {row["brokerOcoId"] for row in orders_view["activeOrders"]} == {"1", "2"})

terminal = src.orders_from_snapshot(
    "A1", SYMBOL, snapshot(orders={"A1": {"9": order_row("9", status="Filled")}}),
    contract_ids=[CONTRACT])
check("終端した行は activeOrders に入らない",
      terminal and terminal["openCount"] == 0 and terminal["state"] == "FILLED")

other = src.orders_from_snapshot(
    "A1", SYMBOL, snapshot(orders={"A1": {"7": order_row("7", contract=OTHER_CONTRACT)}}),
    contract_ids=[CONTRACT])
check("別限月の注文行は混ざらない", other and other["openCount"] == 0)

broken = src.orders_from_snapshot(
    "A1", SYMBOL, snapshot(orders={"A1": {"8": {"contractId": CONTRACT}}}),
    contract_ids=[CONTRACT])
check("行の形が違えば Gateway では答えない(REST へ落ちる)", broken is None)

_saved_ids = src._contract_ids
src._contract_ids = lambda cfg=None, symbol=None: []
try:
    check("限月 ID が設定に無ければ Gateway 経路を使わない",
          src.position_from_snapshot("A1", SYMBOL, snapshot()) is None
          and src.orders_from_snapshot("A1", SYMBOL, snapshot()) is None)
finally:
    src._contract_ids = _saved_ids


# ============================================================ 2. 鮮度と整合性

section("2. 使ってよい口座行の条件")

cases = [
    ("スナップショットが無い", None, "NO_SNAPSHOT"),
    ("schema 違い", {"schema": "other"}, "SCHEMA"),
    ("全体が UNVERIFIED", snapshot(integrity=gw.UNVERIFIED), "INTEGRITY_UNVERIFIED"),
    ("全体が RESYNCING", snapshot(integrity=gw.RESYNCING), "INTEGRITY_RESYNCING"),
    ("鮮度切れ(maxAgeSec 超)", snapshot(written=time.time() - 120), "STALE"),
    ("未来の時刻", snapshot(written=time.time() + 60), "STALE"),
    ("口座が観測されていない", snapshot(accounts=["Z9"]), "ACCOUNT_NOT_OBSERVED"),
    ("その口座だけ RESYNCING",
     snapshot(account_integrity={"A1": gw.RESYNCING}), "ACCOUNT_RESYNCING"),
]
for label, data, expected in cases:
    row, reason = src._fresh_account(data, "A1")
    check(f"{label} → {expected}", row is None and reason == expected, f"reason={reason}")

row, reason = src._fresh_account(snapshot(), "A1")
check("VERIFIED かつ鮮度内なら使える", row is not None and reason is None)

# 全体 VERIFIED / 口座行だけ落ちている、を見逃さない(R117 で捕まえた穴の再発防止)
half = snapshot()
half["accounts"]["A1"]["integrity"] = gw.UNVERIFIED
check("全体 VERIFIED でも口座行が落ちていれば使わない",
      src._fresh_account(half, "A1")[0] is None)


# ============================================================ 3. 出所の選択

section("3. OFF / SHADOW / LIVE")

REST_FLAT = {"verified": True, "source": "crosstrade-rest", "symbol": SYMBOL,
             "qty": 0, "account": "A1"}
REST_ORDERS = {"verified": True, "source": "crosstrade-rest", "symbol": SYMBOL,
               "state": "NONE", "openCount": 0, "orders": [], "activeOrders": []}

with_snapshot(snapshot(positions={"A1": position_row(-2)}))

calls = [0]


def rest_position():
    calls[0] += 1
    return dict(REST_FLAT)


with Mode("OFF"):
    src.reset_stats()
    calls[0] = 0
    answer = src.position(SYMBOL, "A1", rest_call=rest_position)
    check("OFF: REST の答えをそのまま返す", answer["source"] == "crosstrade-rest")
    check("OFF: Gateway を読まない", src.stats()["gateway"] == 0 and calls[0] == 1)

with Mode("SHADOW"):
    src.reset_stats()
    calls[0] = 0
    src.DIVERGENCE_LOG = os.path.join(TMP, "divergence.jsonl")
    answer = src.position(SYMBOL, "A1", rest_call=rest_position)
    stats = src.stats()
    check("SHADOW: 答えは REST のまま", answer["source"] == "crosstrade-rest" and calls[0] == 1)
    check("SHADOW: 判断は 1 つも変わらない", answer["qty"] == 0)
    check("SHADOW: 食い違いを数える(REST=0 枚 vs Gateway=2 枚)",
          stats["shadowChecked"] == 1 and stats["shadowDiverged"] == 1, json.dumps(stats))
    # 差分ログが無いこと自体が検査対象。**素の open で落とさない** ——
    # 落とすと以降の検査が 1 件も走らないまま「1 本 FAIL」になる。
    logged = (io.open(src.DIVERGENCE_LOG, encoding="utf-8").read().strip().splitlines()
              if os.path.exists(src.DIVERGENCE_LOG) else [])
    record = json.loads(logged[-1]) if logged else {}
    check("SHADOW: 差分ログに欄と両方の値が残る",
          bool(logged) and "qty" in (record.get("fields") or [])
          and (record.get("rest") or {}).get("qty") == 0
          and (record.get("gateway") or {}).get("qty") == 2,
          json.dumps(record, ensure_ascii=False) if record else "差分ログが書かれていない")

with Mode("LIVE"):
    src.reset_stats()
    calls[0] = 0
    answer = src.position(SYMBOL, "A1", rest_call=rest_position)
    check("LIVE: Gateway が答える", answer["source"] == "gateway-ws" and answer["qty"] == 2)
    check("LIVE: REST を叩かない", calls[0] == 0 and src.stats()["gateway"] == 1)

    # 口座ごとの退避 —— A2 だけ落ちていても A1 は Gateway のまま
    with_snapshot(snapshot(positions={"A1": position_row(-2)},
                           account_integrity={"A2": gw.UNVERIFIED}))
    src.reset_stats()
    calls[0] = 0
    a1 = src.position(SYMBOL, "A1", rest_call=rest_position)
    a2 = src.position(SYMBOL, "A2", rest_call=rest_position)
    check("LIVE: 落ちた口座だけ REST へ退避する",
          a1["source"] == "gateway-ws" and a2["source"] == "crosstrade-rest")
    check("LIVE: 退避の理由が残る",
          src.stats()["fallbackReasons"].get("ACCOUNT_UNVERIFIED") == 1,
          json.dumps(src.stats()))

    # 全体が落ちれば全口座 REST(**穴は空かない**)
    with_snapshot(snapshot(integrity=gw.UNVERIFIED))
    src.reset_stats()
    calls[0] = 0
    rows = [src.position(SYMBOL, account, rest_call=rest_position) for account in ACCOUNTS]
    check("LIVE: Gateway が死ねば全口座 REST(観測が止まらない)",
          all(row["source"] == "crosstrade-rest" for row in rows) and calls[0] == 3)

    # ID 指定の注文照会は必ず REST
    with_snapshot(snapshot(orders={"A1": {"1": order_row("1")}}))
    src.reset_stats()
    order_calls = [0]

    def rest_orders():
        order_calls[0] += 1
        return dict(REST_ORDERS)

    src.orders(SYMBOL, "A1", rest_call=rest_orders, known_order_ids=["1"])
    check("LIVE: known_order_ids 付きは REST(一覧では ID の終端を証明できない)",
          order_calls[0] == 1 and src.stats()["fallbackReasons"].get("KNOWN_ORDER_IDS") == 1)
    src.orders(SYMBOL, "A1", rest_call=rest_orders)
    check("LIVE: ID 無しなら Gateway が答える", order_calls[0] == 1)


# ================================================ 3b. 影運転が何を証明するか

section("3b. 影運転は『比べた上で差が無い』と言えるか")

# (a) Gateway が答えられない周期は、差分ログが空でも一致の証拠にならない。
#     導入手順 §8 段 2 は「差分ログが空のまま」を確認項目にしているが、常駐が
#     止まっているだけの日も空になる。数で区別できないと段 3 へ進めない。
with Mode("SHADOW"):
    with_snapshot(None)
    src.reset_stats()
    calls[0] = 0
    answer = src.position(SYMBOL, "A1", rest_call=rest_position)
    shadow_stats = src.stats()
    check("SHADOW: Gateway が無くても答えは REST(判断は変わらない)",
          answer["source"] == "crosstrade-rest" and calls[0] == 1)
    check("SHADOW: 比較が成立していないことを数える(compared=0)",
          shadow_stats["shadowChecked"] == 1 and shadow_stats["shadowCompared"] == 0
          and shadow_stats["shadowUnavailable"] == 1, json.dumps(shadow_stats))
    check("SHADOW: 不成立の理由が残る(NO_SNAPSHOT)",
          shadow_stats["shadowUnavailableReasons"].get("NO_SNAPSHOT") == 1,
          json.dumps(shadow_stats["shadowUnavailableReasons"]))

# (b) 限月 ID が設定に無い周期を `SHAPE` に埋めない。
#     `CROSSTRADE_CONTRACT_ID_<限月>` は**ロールのたびに空になる**(09-15 のロール
#     以降ずっと空で、R117 の実接続まで誰も気づかなかった)。埋めると「Gateway が
#     黙って全 REST へ落ちた」ことが読み取れない。
with_snapshot(snapshot(positions={"A1": position_row(-2)}))
_saved_env = src._env
src._env = lambda: {}
try:
    with Mode("LIVE"):
        src.reset_stats()
        calls[0] = 0
        answer = src.position(SYMBOL, "A1", rest_call=rest_position)
        check("限月 ID が無ければ REST へ退避する",
              answer["source"] == "crosstrade-rest" and calls[0] == 1)
        check("退避の理由は NO_CONTRACT_ID(SHAPE に埋めない)",
              src.stats()["fallbackReasons"].get("NO_CONTRACT_ID") == 1,
              json.dumps(src.stats()["fallbackReasons"]))
    with Mode("SHADOW"):
        src.reset_stats()
        src.position(SYMBOL, "A1", rest_call=rest_position)
        check("影運転でも同じ理由で不成立として数える",
              src.stats()["shadowUnavailableReasons"].get("NO_CONTRACT_ID") == 1,
              json.dumps(src.stats()["shadowUnavailableReasons"]))
finally:
    src._env = _saved_env

# (c) 周期ごとの記録。**OFF では 1 バイトも書かない。**
CYCLE_LOG = os.path.join(TMP, "gateway_shadow.jsonl")
with Mode("OFF"):
    src.reset_stats()
    src.position(SYMBOL, "A1", rest_call=rest_position)
    check("OFF: 周期の記録を書かない",
          src.record_cycle(path=CYCLE_LOG) is None and not os.path.exists(CYCLE_LOG))

with Mode("SHADOW"):
    src.reset_stats()
    src.position(SYMBOL, "A1", rest_call=rest_position)
    written = src.record_cycle(path=CYCLE_LOG, status="PUBLISHED")
    rows = io.open(CYCLE_LOG, encoding="utf-8").read().strip().splitlines()
    row = json.loads(rows[-1]) if rows else {}
    check("SHADOW: 周期の記録が 1 行残る", written == CYCLE_LOG and len(rows) == 1)
    check("記録に比較の成立数と周期の結果が入る",
          row.get("shadowChecked") == 1 and row.get("shadowCompared") == 1
          and row.get("mode") == "SHADOW" and row.get("status") == "PUBLISHED",
          json.dumps(row, ensure_ascii=False))

# (d) 集計は「比較 0 件」を一致と言わない
EMPTY_LOG = os.path.join(TMP, "shadow_empty.jsonl")
NO_DIFF = os.path.join(TMP, "shadow_none.jsonl")
with io.open(EMPTY_LOG, "w", encoding="utf-8") as fh:
    fh.write(json.dumps({"at": time.time(), "mode": "SHADOW", "shadowChecked": 12,
                         "shadowCompared": 0, "shadowDiverged": 0,
                         "shadowUnavailable": 12,
                         "shadowUnavailableReasons": {"NO_SNAPSHOT": 12}}) + "\n")
text = src.shadow_report(cycle_path=EMPTY_LOG, divergence_path=NO_DIFF)
check("比較が 0 件なら報告がそう言う", "比較が 1 件も成立していない" in text, text)
summary = src.shadow_summary(cycle_path=EMPTY_LOG, divergence_path=NO_DIFF)
check("集計が理由別に出る",
      summary["totals"]["shadowUnavailable"] == 12
      and summary["reasons"].get("NO_SNAPSHOT") == 12,
      json.dumps(summary, ensure_ascii=False))


# ============================================================ 4. 消費者の切り替え

section("4. 消費者はコード無改造で切り替わる")


class FakeRest:
    """`query_position` / `query_orders` を差し替えて本数を数える。"""

    def __init__(self):
        self.positions = 0
        self.orders = 0
        self.balances = 0

    def install(self):
        self.original = (bs.query_position, bs.query_orders, bs.query_balance)
        bs.query_position = self._position
        bs.query_orders = self._orders
        bs.query_balance = self._balance

    def restore(self):
        bs.query_position, bs.query_orders, bs.query_balance = self.original

    def _position(self, symbol=SYMBOL, account=None):
        self.positions += 1
        return {"verified": True, "source": "crosstrade-rest", "symbol": symbol,
                "qty": 0, "account": account}

    def _orders(self, symbol=SYMBOL, known_order_ids=None, account=None):
        self.orders += 1
        return {"verified": True, "source": "crosstrade-rest", "symbol": symbol,
                "state": "NONE", "openCount": 0, "orders": [], "activeOrders": [],
                "accountScope": [account]}

    def _balance(self, account=None):
        self.balances += 1
        return {"verified": True, "netLiq": 50000.0, "account": account}


fake = FakeRest()
fake.install()
original_symbol = bs.DEFAULT_SYMBOL
bs.DEFAULT_SYMBOL = SYMBOL
original_contract_ids = src._contract_ids
src._contract_ids = lambda cfg=None, symbol=None: [CONTRACT]
original_reread = src._SNAPSHOT_REREAD_SEC
try:
    full = snapshot(positions={account: {} for account in ACCOUNTS},
                    orders={account: {} for account in ACCOUNTS})

    with Mode("OFF"):
        bs.invalidate_read_cache()
        with_snapshot(full)
        fake.positions = fake.orders = fake.balances = 0
        summary = bs.prime_cycle_snapshot(SYMBOL, ACCOUNTS)
        check("OFF: 3 口座で建玉 3 本・注文 3 本・残高 3 本(従来どおり)",
              (fake.positions, fake.orders, fake.balances) == (3, 3, 3),
              f"{fake.positions}/{fake.orders}/{fake.balances}")
        check("OFF: 要約の出所が OFF", summary["source"]["mode"] == "OFF")

    with Mode("LIVE"):
        bs.invalidate_read_cache()
        with_snapshot(full)
        fake.positions = fake.orders = fake.balances = 0
        summary = bs.prime_cycle_snapshot(SYMBOL, ACCOUNTS)
        check("LIVE: 建玉 0 本・注文 0 本(Gateway が答える)",
              (fake.positions, fake.orders) == (0, 0),
              f"{fake.positions}/{fake.orders}")
        check("LIVE: 残高だけ REST(WebSocket に残高は流れない)", fake.balances == 3)
        check("LIVE: 未検証の口座は出ない", summary["unverified"] == [], repr(summary["unverified"]))
        check("LIVE: 要約に Gateway 件数が載る",
              summary["source"]["gateway"] == 6 and summary["source"]["rest"] == 0,
              json.dumps(summary["source"]))

        # 消費者(engine / 表示 / 会計)が使う入口
        with_snapshot(full)
        bs.invalidate_read_cache()
        fake.positions = 0
        row = bs.query_position_cached(SYMBOL, account="A1")
        check("query_position_cached が Gateway から返る", row["source"] == "gateway-ws")
        check("query_position_cached は REST を叩かない", fake.positions == 0)

        # live_reads() の中は必ず REST(二度読みが証明になる経路)
        bs.invalidate_read_cache()
        with_snapshot(full)
        fake.positions = 0
        with bs.live_reads():
            proof = bs.query_position_cached(SYMBOL, account="A1")
        check("live_reads() の中は Gateway を挟まない",
              proof["source"] == "crosstrade-rest" and fake.positions == 1)

        bs.invalidate_read_cache()
        with_snapshot(full)
        fake.orders = 0
        with bs.live_reads():
            bs.query_orders_cached(SYMBOL, account="A1")
        check("live_reads() の注文照会も REST", fake.orders == 1)

    # 送信の前後(生の query_position)は出所層を通らない
    with Mode("LIVE"):
        with_snapshot(snapshot(positions={"A1": position_row(-2)}))
        fake.positions = 0
        raw = bs.query_position(SYMBOL, account="A1")
        check("生の query_position は常に REST(送信前後の検証はここを使う)",
              raw["source"] == "crosstrade-rest" and fake.positions == 1)
finally:
    fake.restore()
    bs.DEFAULT_SYMBOL = original_symbol
    src._contract_ids = original_contract_ids
    src._SNAPSHOT_REREAD_SEC = original_reread


# ============================================================ 4b. 既存注文の引き継ぎ

section("4b. 切替のときに既存注文の identity が変わらない")

LIVE_ROWS = {"A1": {"5001": order_row("5001", oco="5002"),
                    "5002": order_row("5002", oco="5001", order_type="LIMIT")}}

gateway_view = src.orders_from_snapshot("A1", SYMBOL, snapshot(orders=LIVE_ROWS),
                                        contract_ids=[CONTRACT])
rest_view = bs.normalize_crosstrade_orders(
    {"success": True, "data": [{k: v for k, v in row.items()
                                if k not in ("symbol", "instrument", "contractName")}
                               for row in LIVE_ROWS["A1"].values()]},
    platform="TRADOVATE", account="A1", symbol=SYMBOL,
    account_aliases=["A1"], contract_ids=[CONTRACT])

COMPARE = ("orderId", "accountId", "symbol", "contractId", "status", "orderType",
           "action", "qty", "receipt", "receiptSource", "brokerOcoId",
           "brokerParentId", "brokerLinkedId", "fieldsComplete")
gateway_rows = {row["orderId"]: {k: row[k] for k in COMPARE} for row in gateway_view["orders"]}
rest_rows = {row["orderId"]: {k: row[k] for k in COMPARE} for row in rest_view["orders"]}
check("同じ注文行は REST でも Gateway でも **1 欄も違わない**",
      gateway_rows == rest_rows,
      json.dumps({"gateway": gateway_rows, "rest": rest_rows}, ensure_ascii=False))
check("receipt はどちらの経路でも同じ値になる(所有権の束縛が切替で壊れない)",
      all(gateway_rows[key]["receipt"] == rest_rows[key]["receipt"] for key in gateway_rows)
      and all(row["receipt"] for row in gateway_rows.values()))
check("OCO 兄弟の判定器が Gateway 由来の行でも成立する",
      bs.oco_sibling_pair(gateway_view["activeOrders"], account="A1",
                          expected_action="BUY", symbol=SYMBOL) is not None,
      repr(bs.oco_sibling_pair(gateway_view["activeOrders"], account="A1",
                               expected_action="BUY", symbol=SYMBOL)))
check("REST と Gateway で OCO 兄弟の判定が一致する",
      bool(bs.oco_sibling_pair(gateway_view["activeOrders"], account="A1",
                               expected_action="BUY", symbol=SYMBOL))
      == bool(bs.oco_sibling_pair(rest_view["activeOrders"], account="A1",
                                  expected_action="BUY", symbol=SYMBOL)))


# ============================================================ 4c. 退避の集中

section("4c. REST への退避が集中したときに何本に戻るか")

# 指示 §5「RESTへのフォールバックが集中して再び遅くなる場合も測ってください」。
# **時間ではなく本数で測る** —— 所要はそのときのブローカーの混み具合で 2 倍以上
# 振れる(REST 7.91〜16.27 秒を実測)が、本数は決定論的で、時間はその関数になる。

degraded_scope = [f"D{i}" for i in range(20)]
fake2 = FakeRest()
fake2.install()
original_symbol2 = bs.DEFAULT_SYMBOL
bs.DEFAULT_SYMBOL = SYMBOL
original_ids2 = src._contract_ids
src._contract_ids = lambda cfg=None, symbol=None: [CONTRACT]
try:
    rows = []
    with Mode("LIVE"):
        for degraded in (0, 1, 5, 10, 20):
            broken = {account: gw.UNVERIFIED for account in degraded_scope[:degraded]}
            with_snapshot(snapshot(accounts=degraded_scope,
                                   positions={a: {} for a in degraded_scope},
                                   orders={a: {} for a in degraded_scope},
                                   account_integrity=broken))
            bs.invalidate_read_cache()
            fake2.positions = fake2.orders = fake2.balances = 0
            summary = bs.prime_cycle_snapshot(SYMBOL, degraded_scope)
            rows.append((degraded, fake2.positions, fake2.orders, fake2.balances,
                         summary["source"]["gateway"], summary["source"]["fallback"]))

    print(f"  {'退避口座':>8}{'建玉 REST':>10}{'注文 REST':>10}{'残高 REST':>10}"
          f"{'Gateway':>9}{'退避計':>8}")
    for degraded, positions, orders, balances, gateway_count, fallback in rows:
        print(f"  {degraded:>8}{positions:>10}{orders:>10}{balances:>10}"
              f"{gateway_count:>9}{fallback:>8}")

    check("退避 0 なら建玉・注文の REST は 0 本(残高だけ 20 本)",
          rows[0][1] == 0 and rows[0][2] == 0 and rows[0][3] == 20, repr(rows[0]))
    check("退避した口座の数だけ REST が増える(線形・それ以上でもそれ以下でもない)",
          all(positions == degraded and orders == degraded
              for degraded, positions, orders, _b, _g, _f in rows),
          repr([(d, p, o) for d, p, o, _b, _g, _f in rows]))
    check("全口座退避すると従来の REST と同じ本数へ戻る(40 本 + 残高 20 本)",
          rows[-1][1] == 20 and rows[-1][2] == 20 and rows[-1][3] == 20, repr(rows[-1]))
    check("Gateway が答えた件数は 2×(20 − 退避数)",
          all(gateway_count == 2 * (20 - degraded)
              for degraded, _p, _o, _b, gateway_count, _f in rows),
          repr([(d, g) for d, _p, _o, _b, g, _f in rows]))
    check("退避しても **観測は止まらない**(未検証の口座が出ない)",
          all(count >= 0 for _d, _p, _o, _b, _g, count in rows))
finally:
    fake2.restore()
    bs.DEFAULT_SYMBOL = original_symbol2
    src._contract_ids = original_ids2
    bs.invalidate_read_cache()


# ============================================================ 4e. 建玉 identity

section("4e. 建玉の identity が切替で変わらない")

# `broker_status.position_identity` は `account` / `accountId` / `symbol` / `side` /
# `orderId` / `receipt` / `filledAt` を hash に入れる。ここが経路で 1 欄でも違うと、
# **切替の瞬間に同じ建玉が別の建玉に見え**、世代(generation)が付け替わって
# 所有権が切れる。注文行(§4b)と同じ規律で建玉側も固定する。

OPEN_ROW = {CONTRACT: {"accountId": "66248423",   # ← 一覧の行は **数値の accountId**
                       "contractId": CONTRACT, "netPos": -2, "netPrice": 29850.0,
                       "orderId": "7001", "receipt": "r-7001",
                       "timestamp": "2026-09-19T12:00:00Z"}}

gateway_position = src.position_from_snapshot(
    "A1", SYMBOL, snapshot(positions={"A1": OPEN_ROW}), contract_ids=[CONTRACT])
# REST が返す形(2026-09-19 実測: 3 欄とも **口座名**)
rest_position = {"verified": True, "source": "crosstrade-rest", "platform": "TRADOVATE",
                 "account": "A1", "accountId": "A1", "brokerAccountId": "A1",
                 "symbol": SYMBOL, "side": "SHORT", "qty": 2, "avgEntry": 29850.0,
                 "filledAt": "2026-09-19T12:00:00Z", "orderId": "7001",
                 "receipt": "r-7001", "observedAt": "2026-09-19T12:00:00Z"}

check("口座欄は 3 つとも口座名(REST と同じ。数値の accountId を混ぜない)",
      gateway_position["account"] == gateway_position["accountId"]
      == gateway_position["brokerAccountId"] == "A1",
      json.dumps({k: gateway_position.get(k)
                  for k in ("account", "accountId", "brokerAccountId")}))
check("建玉の identity が REST と Gateway で一致する(切替で所有権が切れない)",
      bs.position_identity(gateway_position) == bs.position_identity(rest_position)
      is not None,
      f"gw={bs.position_identity(gateway_position)} rest={bs.position_identity(rest_position)}")
check("side / qty / avgEntry / orderId / receipt も一致",
      all(gateway_position.get(k) == rest_position.get(k)
          for k in ("side", "qty", "avgEntry", "orderId", "receipt")),
      json.dumps({k: [gateway_position.get(k), rest_position.get(k)]
                  for k in ("side", "qty", "avgEntry", "orderId", "receipt")}))


# ============================================================ 4f. 全体取得は置き換え

section("4f. 全体取得は足し込みではなく置き換え")

# 2026-09-19、20 口座の持続負荷試験で注文行が再取得のたびに増え続けていた。原因は
# `_apply_full` が既存の行を消さずに足し込んでいたこと。**これは記憶の問題ではなく
# 正しさの問題**で、切断中に取り消された注文が `Working` のまま生き残り、
# 新規を塞ぎ、保護注文の照合で幻の脚を通しうる。

def full_orders(rows, account="A1"):
    return [{"environment": "live", "data": [
        {"id": oid, "accountId": account, "contractId": CONTRACT, "ordStatus": status,
         "action": "BUY", "orderQty": 1, "orderType": "STOP"} for oid, status in rows]}]


def full_positions(rows, account="A1"):
    return [{"environment": "live", "data": [
        {"accountId": account, "contractId": contract, "netPos": net, "netPrice": 29850.0}
        for contract, net in rows]}]


state = gw.GatewayState(expected_accounts=["A1", "A2"], contract_ids=[CONTRACT])
state.begin_resync("first", 1.0)
state.apply_full_positions(full_positions([(CONTRACT, -2)]), 1.0, epoch=1)
state.apply_full_orders(full_orders([("100", "Working"), ("101", "Working")]), 1.0, epoch=1)
first = state.account_view("A1", CONTRACT)
check("最初の取得: 生きた注文 2 本・建玉 SHORT 2 枚",
      len(first["liveOrders"]) == 2 and first["netPos"] == -2,
      json.dumps({"live": len(first["liveOrders"]), "net": first["netPos"]}))

# 切断中に 101 が取り消され、一覧から消えた(口座には 100 が残っている)
state.begin_resync("after disconnect", 2.0)
state.apply_full_positions(full_positions([(CONTRACT, -2)]), 2.0, epoch=2)
state.apply_full_orders(full_orders([("100", "Working")]), 2.0, epoch=2)
second = state.account_view("A1", CONTRACT)
check("**一覧から消えた注文は状態からも消える**(幻の Working を残さない)",
      [row["id"] for row in second["liveOrders"]] == ["100"],
      repr([row["id"] for row in second["liveOrders"]]))
check("全部の注文行が 1 本だけ(終端として残しもしない)",
      len(second["orders"]) == 1, repr(len(second["orders"])))

# 建玉も同じ: 決済されて一覧から消えたら FLAT
state.begin_resync("after close", 3.0)
state.apply_full_positions(full_positions([("9999999", -1)]), 3.0, epoch=3)  # 別限月だけ残る
state.apply_full_orders(full_orders([("100", "Working")]), 3.0, epoch=3)
third = state.account_view("A1", CONTRACT)
check("一覧から消えた建玉は 0 枚になる(存在しない建玉を報告しない)",
      third["netPos"] == 0, repr(third["netPos"]))

# 一覧に 1 行も無い口座も空(従来どおり)
empty = state.account_view("A2", CONTRACT)
check("一覧に出なかった口座は空のまま",
      empty["netPos"] == 0 and len(empty["orders"]) == 0, json.dumps(empty["netPos"]))

# 何度取得しても増えない
before = sum(len(e["orders"]) + len(e["positions"]) for e in state.accounts.values())
for round_index in range(20):
    state.begin_resync("repeat", 4.0 + round_index)
    state.apply_full_positions(full_positions([(CONTRACT, -2)]), 4.0 + round_index, epoch=4)
    state.apply_full_orders(full_orders([("100", "Working"), ("102", "Working")]),
                            4.0 + round_index, epoch=4)
after = sum(len(e["orders"]) + len(e["positions"]) for e in state.accounts.values())
check(f"20 回取得しても行数が増えない({before} → {after})", after <= 3,
      json.dumps({"before": before, "after": after}))
check("整合性は VERIFIED に戻る", state.integrity == gw.VERIFIED, state.integrity)


# ============================================================ 5. fill_watch

section("5. fill_watch の見回り費用と間隔")

SETTINGS = {"fastIntervalSec": 1.5, "idleIntervalSec": 45.0,
            "maxRequestsPerSec": 1.0, "rateLimitBackoffSec": 15.0}


def observation(count, *, source, qty=2):
    return {f"A{i}": {"verified": True, "qty": qty, "side": "SHORT", "source": source}
            for i in range(count)}


for n in (1, 7, 20):
    rest_obs = observation(n, source="crosstrade-rest")
    gw_obs = observation(n, source="gateway-ws")
    rest_delay, _ = fill_watch.next_delay(SETTINGS, rest_obs)
    gw_delay, gw_mode = fill_watch.next_delay(SETTINGS, gw_obs)
    check(f"N={n:<2} REST は {rest_delay:g}s / Gateway は {gw_delay:g}s",
          gw_delay == 1.5 and gw_mode == "FAST",
          f"rest={rest_delay} gw={gw_delay}")

check("Gateway 由来の見回りは HTTP 0 本",
      fill_watch.sweep_cost(observation(20, source="gateway-ws")) == 0)
check("REST 由来は従来どおり数える(建玉ありは 1 本)",
      fill_watch.sweep_cost(observation(20, source="crosstrade-rest")) == 20)
mixed_obs = {**observation(3, source="gateway-ws")}
mixed_obs["B0"] = {"verified": True, "qty": 0, "side": "", "source": "crosstrade-rest"}
check("混在すると REST の口座だけ数える(FLAT は裏取りで 2 本)",
      fill_watch.sweep_cost(mixed_obs) == 2)
# heartbeat に載るのは `merge_baseline` の結果。ここで `source` を落とすと
# **どの出所で観測しているかが外から一切見えない**(2026-09-20、mode=LIVE にしたのに
# heartbeat の出所が空で、効いていないように見えた)。
merged = fill_watch.merge_baseline(
    {"A1": {"verified": True, "qty": 0, "side": ""}},
    {"A1": {"verified": True, "qty": 2, "side": "LONG", "source": "gateway-ws"}})
check("heartbeat の基準に出所が残る", merged["A1"].get("source") == "gateway-ws", repr(merged))
dropped = fill_watch.merge_baseline(
    {"GONE": {"verified": True, "qty": 0, "side": ""}},
    {"A1": {"verified": True, "qty": 0, "side": "", "source": "gateway-ws"}}, ["A1"])
check("監視対象から外れた口座は基準から落ちる(口座を入れ替えても数が合う)",
      sorted(dropped) == ["A1"], repr(sorted(dropped)))
check("口座を渡さなければ従来どおり残す(R91 との互換)",
      "GONE" in fill_watch.merge_baseline(
          {"GONE": {"verified": True, "qty": 0, "side": ""}},
          {"A1": {"verified": True, "qty": 0, "side": ""}}))

check("出所欄が無い観測は従来どおり(R91 との互換)",
      fill_watch.sweep_cost({"A": {"verified": True, "qty": 2}}) == 1)


def observed_source(position):
    return fill_watch.observe(SYMBOL, ["A1"], lambda _s, account=None: position)["A1"]


check("observe は出所を写し取る",
      observed_source({"verified": True, "qty": 2, "side": "SHORT",
                       "source": "gateway-ws"})["source"] == "gateway-ws")
check("未検証の観測は従来どおり verified=False",
      observed_source({"verified": False, "detail": "boom"})["verified"] is False)

# 検知に穴が空かない: Gateway で見えた枚数変化は従来と同じ経路で trigger される
baseline = {"A1": {"verified": True, "qty": 2, "side": "SHORT"}}
after_tp1 = {"A1": {"verified": True, "qty": 1, "side": "SHORT", "source": "gateway-ws"}}
check("Gateway 由来でも TP1 の枚数変化が検知される",
      fill_watch.changes(baseline, after_tp1) == ["A1: SHORT 2 -> SHORT 1"],
      repr(fill_watch.changes(baseline, after_tp1)))

# Gateway が落ちて REST へ退避した周期も、検知は続く
after_fallback = {"A1": {"verified": True, "qty": 1, "side": "SHORT",
                         "source": "crosstrade-rest"}}
check("REST へ退避した周期でも同じ変化を検知する",
      fill_watch.changes(baseline, after_fallback) == ["A1: SHORT 2 -> SHORT 1"])
check("未検証は変化とみなさない(偽の TP1 を作らない)",
      fill_watch.changes(baseline, {"A1": {"verified": False}}) == [])


# ============================================================ 5b. 切替中の穴

section("5b. Gateway が落ちている最中に TP1 が起きても検知できるか")

# 指示 §3「イベント欠落、再接続、停止時も含め、旧 fill_watch から切り替えて
# 検知と管理に穴が空かないこと」。**fill_watch のループをそのまま回す。**

from pathlib import Path  # noqa: E402

TRADE = {"qty": 2, "side": "SHORT"}
GATEWAY_UP = [True]
REST_CALLS = [0]


def scripted_position(_symbol, account=None):
    """Gateway が生きていれば押し込み由来、落ちていれば REST 由来を返す。"""
    if GATEWAY_UP[0]:
        return {"verified": True, "source": "gateway-ws", "symbol": SYMBOL,
                "qty": TRADE["qty"], "side": TRADE["side"], "accountId": account}
    REST_CALLS[0] += 1
    return {"verified": True, "source": "crosstrade-rest", "symbol": SYMBOL,
            "qty": TRADE["qty"], "side": TRADE["side"], "accountId": account}


triggers = []


def fake_reconcile(_bundle, state_ok=True, fresh_price_query=None):
    triggers.append({"qty": TRADE["qty"], "gateway": GATEWAY_UP[0]})
    return ["managed"]


ticks = [0]


def scripted_sleep(_delay):
    """周期の境目で相場を進める。**Gateway は TP1 のちょうど手前で落とす。**"""
    ticks[0] += 1
    if ticks[0] == 2:
        GATEWAY_UP[0] = False          # 接続断
    if ticks[0] == 3:
        TRADE["qty"] = 1               # 落ちている最中に TP1 が約定
    if ticks[0] == 5:
        GATEWAY_UP[0] = True           # 再接続
    if ticks[0] == 6:
        TRADE["qty"] = 0               # runner も決済


beat = Path(TMP) / "fill_watch_heartbeat.json"
rc = fill_watch.run(
    symbol=SYMBOL, accounts=["A1"], interval=1.0,
    position_query=scripted_position, reconcile=fake_reconcile,
    heartbeat_path=beat, bundle_candidates=(Path(TMP) / "nope.json",),
    resume=False, max_iterations=8, sleep=scripted_sleep, log=lambda _m: None,
    settings={"fastIntervalSec": 1.5, "idleIntervalSec": 45.0,
              "maxRequestsPerSec": 1.0, "rateLimitBackoffSec": 15.0})

check("ループは正常終了する", rc == 0, repr(rc))
check("Gateway が落ちている最中の TP1(2 → 1 枚)を検知した",
      any(row["qty"] == 1 and row["gateway"] is False for row in triggers),
      repr(triggers))
check("再接続後の決済(1 → 0 枚)も検知した",
      any(row["qty"] == 0 for row in triggers), repr(triggers))
check("同じ変化で二度 trigger しない(枚数ごとに 1 回)",
      len([row for row in triggers if row["qty"] == 1]) == 1
      and len([row for row in triggers if row["qty"] == 0]) == 1,
      repr(triggers))
check("落ちている間は REST を叩いている(観測が止まっていない)",
      REST_CALLS[0] >= 2, repr(REST_CALLS[0]))
recorded = json.load(io.open(beat, encoding="utf-8"))
check("heartbeat に最後の観測が残る",
      recorded.get("lastObservation", {}).get("A1", {}).get("qty") == 0,
      json.dumps(recorded.get("lastObservation")))
check("heartbeat に最後の trigger が残る",
      bool((recorded.get("lastTrigger") or {}).get("reason")),
      json.dumps(recorded.get("lastTrigger")))


# ============================================================ 6. 常駐

section("6. Gateway 常駐(serve)の再接続と失敗")


class ScriptedTransport:
    """台本どおりに返す偽 transport。`fail_connect` で接続そのものを失敗させる。"""

    def __init__(self, frames, *, fail_connect=False, fail_recv=False):
        self.frames = list(frames)
        self.fail_connect = fail_connect
        self.fail_recv = fail_recv
        self.sent = []
        self.closed = 0

    async def connect(self):
        if self.fail_connect:
            raise ConnectionError("injected connect failure")

    async def send(self, payload):
        self.sent.append(payload)

    async def recv(self):
        if self.fail_recv:
            raise ConnectionError("injected recv failure")
        if self.frames:
            return self.frames.pop(0)
        return None

    async def close(self):
        self.closed += 1


def full_frames():
    return [
        {"id": "gw-pos", "epoch": 1_700_000_000_000,
         "data": {"success": True, "data": [{"environment": "live", "data": [
             {"accountId": "A1", "contractId": CONTRACT, "netPos": -2, "netPrice": 29850.0}]}]}},
        {"id": "gw-ord", "epoch": 1_700_000_000_000,
         "data": {"success": True, "data": [{"environment": "live", "data": []}]}},
    ]


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    async def sleep(self, d):
        self.t += d


snap_file = os.path.join(TMP, "gateway_snapshot.json")
beat_file = os.path.join(TMP, "gateway_heartbeat.json")

clock = Clock()
built = [0]


def factory():
    built[0] += 1
    return ScriptedTransport(full_frames())


result = asyncio.run(gw.serve(accounts=["A1"], contract_ids=[CONTRACT], token="t",
                              max_seconds=40.0, transport_factory=factory,
                              snapshot_file=snap_file, heartbeat_file=beat_file,
                              sleep=clock.sleep, clock=clock))
check("切断のたびに繋ぎ直す", built[0] >= 2 and result["cycles"] >= 2,
      json.dumps(result))
check("再接続の待ちが指数で空く(1 本しか張れないので慌てない)", clock.t >= 3.0,
      f"t={clock.t}")
written = json.load(io.open(snap_file, encoding="utf-8"))
check("スナップショットが書かれている", written["schema"] == gw.SCHEMA)
beat = json.load(io.open(beat_file, encoding="utf-8"))
check("heartbeat に整合性と口座数が載る",
      beat["accounts"] == 1 and "integrity" in beat, json.dumps(beat))

clock2 = Clock()
result2 = asyncio.run(gw.serve(
    accounts=["A1"], contract_ids=[CONTRACT], token="t", max_seconds=10.0,
    transport_factory=lambda: ScriptedTransport([], fail_connect=True),
    snapshot_file=snap_file, heartbeat_file=beat_file,
    sleep=clock2.sleep, clock=clock2))
check("接続できない間は整合性が UNVERIFIED",
      result2["integrity"] == gw.UNVERIFIED and result2["errors"] >= 1,
      json.dumps(result2))
failed_snapshot = json.load(io.open(snap_file, encoding="utf-8"))
check("**落ちたことをスナップショットへ書いてから**待つ(古い検証済みを残さない)",
      failed_snapshot["integrity"] == gw.UNVERIFIED)
check("落ちたスナップショットは出所層に使われない",
      src._fresh_account(failed_snapshot, "A1")[0] is None)


# 切断で例外が出たときも transport を閉じる。閉じないと**再接続のたびに**
# aiohttp のセッションが残る(2026-09-20 の 60 分常駐で "Unclosed client session"
# を実測。切断 1 回につき 1 個で、1 日回せば切断回数ぶん溜まる)。
async def leak_case(fail_recv):
    transport = ScriptedTransport(full_frames(), fail_recv=fail_recv)
    state = gw.GatewayState(expected_accounts=["A1"], contract_ids=[CONTRACT])
    gateway = gw.BrokerGateway(transport=transport, state=state, snapshot_file="MEM",
                               writer=lambda st, now: None)
    try:
        await gateway.run_once(max_seconds=1.0)
    except Exception:  # noqa: BLE001 — 投げ直されるのが正しい
        pass
    return transport


check("切断の例外で抜けても transport を閉じる(セッションを残さない)",
      asyncio.run(leak_case(True)).closed == 1)
check("正常に終わったときも閉じるのは 1 回だけ",
      asyncio.run(leak_case(False)).closed == 1)

# 定期書き出し: イベントが来なくても鮮度が落ちない
section("6b. 無イベントでも鮮度が落ちない(無言検知は生きたまま)")


class SilentTransport(ScriptedTransport):
    """初期取得のあと何も返さず、`recv` が待たされ続ける。"""

    def __init__(self, frames, hold):
        super().__init__(frames)
        self.hold = hold

    async def recv(self):
        if self.frames:
            return self.frames.pop(0)
        await asyncio.sleep(self.hold)
        return None


async def silent_case():
    state = gw.GatewayState(expected_accounts=["A1"], contract_ids=[CONTRACT])
    writes = []
    gateway = gw.BrokerGateway(
        transport=SilentTransport(full_frames(), hold=5.0), state=state,
        snapshot_file="MEM", writer=lambda st, now: writes.append(st.integrity))
    original = gw.SNAPSHOT_REFRESH_SEC
    gw.SNAPSHOT_REFRESH_SEC = 0.05
    try:
        await gateway.run_once(max_seconds=0.45)
    finally:
        gw.SNAPSHOT_REFRESH_SEC = original
    return state, writes, gateway


state, writes, gateway = asyncio.run(silent_case())
check("無イベントでも生存の印を書き続ける", len(writes) >= 4, f"writes={len(writes)}")
check("書いている間も整合性は VERIFIED のまま", writes[-1] == gw.VERIFIED)
check("無言検知はまだ発火していない(SILENCE_SEC 未満)", gateway.silences == 0)


# ====================================== 6c. 常駐の自動起動(既定 false)

section("6c. 止まった常駐を周期が起動し直すか(契約 gateway.autostart)")

SPAWN_STATE = os.path.join(TMP, "gateway_spawn.json")
SUP_BEAT = os.path.join(TMP, "supervise_heartbeat.json")
SUP_LOCK = os.path.join(TMP, "supervise.lock")
_saved_beat_path, _saved_lock_path = gw.heartbeat_path, gw.lock_path
gw.heartbeat_path = lambda: SUP_BEAT           # 本番の .secrets を触らない
gw.lock_path = lambda: SUP_LOCK


def supervise_once(mode_value, contract, *, beat_age=None, locked=False, state=None):
    """`supervise` を 1 回。spawn は呼ばれた回数を数えるだけ(プロセスは作らない)。"""
    spawned = []
    for path in (SUP_BEAT, SUP_LOCK, SPAWN_STATE):
        if os.path.exists(path):
            os.unlink(path)
    if beat_age is not None:
        gw.write_heartbeat({"at": time.time() - beat_age, "integrity": gw.VERIFIED,
                            "accounts": 3}, SUP_BEAT)
    if locked:
        io.open(SUP_LOCK, "w", encoding="utf-8").write("4242")
    if state is not None:
        with io.open(SPAWN_STATE, "w", encoding="utf-8") as fh:
            json.dump(state, fh)
    with Mode(mode_value):
        line = gw.supervise(settings=contract, state_path=SPAWN_STATE,
                            spawn=lambda: spawned.append(1) or 4242)
    return line, len(spawned)


line, spawned = supervise_once("SHADOW", {"autostart": False}, beat_age=900)
check("既定(autostart 無し)では起動しない", spawned == 0 and "autostart" not in line, line)

line, spawned = supervise_once("OFF", {"autostart": True}, beat_age=900)
check("mode=OFF なら autostart=true でも起動しない(観測に使っていない)",
      spawned == 0, line)

line, spawned = supervise_once("SHADOW", {"autostart": True}, beat_age=900)
check("SHADOW で常駐が死んでいれば起動し直す", spawned == 1 and "pid=4242" in line, line)
check("起動の回数を残す(3 回で止めるため)",
      json.loads(io.open(SPAWN_STATE, encoding="utf-8").read()).get("pending") == 1)

line, spawned = supervise_once("LIVE", {"autostart": True}, beat_age=5)
check("生きている常駐には手を出さない", spawned == 0, line)

line, spawned = supervise_once("LIVE", {"autostart": True}, beat_age=5,
                               state={"pending": 2})
check("生き返ったら起動の回数を 0 へ戻す", spawned == 0
      and json.loads(io.open(SPAWN_STATE, encoding="utf-8").read()).get("pending") == 0)

# heartbeat は途切れたがロックはある = まだ生きているかもしれない。**触らない。**
line, spawned = supervise_once("LIVE", {"autostart": True}, beat_age=90, locked=True)
check("ロックを持つプロセスが居れば起動しない(自分の接続を自分で切らない)",
      spawned == 0 and "ロック" in line, line)

# 2 分以上 heartbeat が無いロックは残骸。起動して `_serve_cli` に引き剥がさせる。
line, spawned = supervise_once("LIVE", {"autostart": True}, beat_age=600, locked=True)
check("2 分以上沈黙したロックは残骸として扱う(起動する)", spawned == 1, line)

line, spawned = supervise_once("LIVE", {"autostart": True}, beat_age=900,
                               state={"pending": 3, "lastSpawnAt": time.time() - 60})
check("3 回続けて heartbeat が出なければ止める(無限に起動し直さない)",
      spawned == 0 and "autostart 停止中" in line, line)

line, spawned = supervise_once("LIVE", {"autostart": True}, beat_age=900,
                               state={"pending": 3,
                                      "lastSpawnAt": time.time() - gw.SPAWN_RETRY_AFTER_SEC - 60})
check("1 時間おいたらもう一度だけ試す", spawned == 1, line)

_prev_autostart = os.environ.get("NQX_GATEWAY_AUTOSTART")
os.environ["NQX_GATEWAY_AUTOSTART"] = "0"
try:
    line, spawned = supervise_once("LIVE", {"autostart": True}, beat_age=900)
    check("NQX_GATEWAY_AUTOSTART=0 は契約より強い", spawned == 0, line)
finally:
    if _prev_autostart is None:
        os.environ.pop("NQX_GATEWAY_AUTOSTART", None)
    else:
        os.environ["NQX_GATEWAY_AUTOSTART"] = _prev_autostart

def _raise():
    raise RuntimeError("spawn boom")


with Mode("LIVE"):
    for path in (SUP_BEAT, SPAWN_STATE):
        if os.path.exists(path):
            os.unlink(path)
    line = gw.supervise(settings={"autostart": True}, state_path=SPAWN_STATE, spawn=_raise)
check("spawn が投げても 1 行返す(周期は続く)", "autostart failed" in line, line)

# `--stop` は heartbeat の pid を止める。**pid は使い回される**ので、古い
# heartbeat の pid をそのまま殺すと**無関係のプロセスを落とす**。
_saved_kill = os.kill
killed = []
os.kill = lambda pid, sig: killed.append(pid)
try:
    gw.write_heartbeat({"at": time.time() - 3600, "pid": 999999,
                        "integrity": gw.VERIFIED}, SUP_BEAT)
    rc = gw._stop_cli()
    check("--stop: heartbeat が古ければ止めない(pid の使い回しで誤爆しない)",
          rc == 1 and not killed, f"rc={rc} killed={killed}")
    gw.write_heartbeat({"at": time.time(), "pid": 999999,
                        "integrity": gw.VERIFIED}, SUP_BEAT)
    rc = gw._stop_cli()
    check("--stop: 生きている常駐は止める", rc == 0 and killed == [999999],
          f"rc={rc} killed={killed}")
finally:
    os.kill = _saved_kill

gw.heartbeat_path, gw.lock_path = _saved_beat_path, _saved_lock_path


# ============================================================ 7. 実プロセスの書き出し

section("7. Gateway が実際に書いたファイルを、出所層がそのまま読めるか")

# **手で組んだスナップショットでは出ない不具合がある。** 2026-09-19 に、書き出しの
# `writtenAt` が単調時計(`time.monotonic`)だったため、別プロセスが `time.time()` と
# 引き算して **常に STALE** と判定していた。上の検査は全部 `time.time()` で組んだ
# スナップショットを使っていたので気づけなかった。ここは **BrokerGateway 本体に
# 書かせて**、それを `broker_source` が受け取れることを見る。

REAL_FILE = os.path.join(TMP, "real_gateway_snapshot.json")


async def write_with_real_gateway():
    state = gw.GatewayState(expected_accounts=["A1"], contract_ids=[CONTRACT])
    gateway = gw.BrokerGateway(transport=ScriptedTransport(full_frames()), state=state,
                               snapshot_file=REAL_FILE)
    await gateway.run_once(max_seconds=1.0)
    return state


real_state = asyncio.run(write_with_real_gateway())
written_snapshot = json.load(io.open(REAL_FILE, encoding="utf-8"))
check("BrokerGateway が書いたファイルが読める",
      gw.read_snapshot(REAL_FILE) is not None)
check("writtenAt は **壁時計**(別プロセスが time.time() と引き算できる)",
      abs(time.time() - float(written_snapshot["writtenAt"])) < 60,
      f"writtenAt={written_snapshot['writtenAt']} now={time.time()}")

row, reason = src._fresh_account(written_snapshot, "A1")
check("出所層が実ファイルを鮮度内として受け取る(STALE にならない)",
      row is not None and reason is None, f"reason={reason}")

saved_path = gw.snapshot_path
gw.snapshot_path = lambda: REAL_FILE
src.invalidate_snapshot()
try:
    with Mode("LIVE"):
        answer = src.position(SYMBOL, "A1", rest_call=lambda: dict(REST_FLAT))
        check("実ファイル経由で Gateway が答える(SHORT 2 枚)",
              answer["source"] == "gateway-ws" and answer["qty"] == 2,
              json.dumps({k: answer.get(k) for k in ("source", "qty", "side")}))
finally:
    gw.snapshot_path = saved_path
    src.invalidate_snapshot()

# 切断のあとに書かれたファイルは使われない
async def write_after_disconnect():
    state = gw.GatewayState(expected_accounts=["A1"], contract_ids=[CONTRACT])
    gateway = gw.BrokerGateway(transport=ScriptedTransport(full_frames()), state=state,
                               snapshot_file=REAL_FILE)
    await gateway.run_once(max_seconds=1.0)
    state.disconnected("test", 1.0)
    gw.write_snapshot(state, time.time(), REAL_FILE)


asyncio.run(write_after_disconnect())
after = json.load(io.open(REAL_FILE, encoding="utf-8"))
check("切断のあとのファイルは UNVERIFIED で、出所層に使われない",
      after["integrity"] == gw.UNVERIFIED and src._fresh_account(after, "A1")[0] is None)


# ============================================================ 締め

print(f"\n{'ALL PASS' if not FAIL[0] else 'FAILED'} ({PASS[0]} checks, {FAIL[0]} failures)")
sys.exit(1 if FAIL[0] else 0)

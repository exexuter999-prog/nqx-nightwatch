#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CrossTrade → Tradovate 発注スクリプト

使い方:
  python order.py --side buy  --qty 2 --entry 29630 --sl 29580 --tp 29730
  python order.py --side sell --qty 2 --entry 29585 --sl 29625 --tp 29548
  python order.py --market --side buy --qty 1 --sl 29700 --last 29726.5   # 成行
  python order.py --flatten                            # 全決済
  python order.py --status                             # 当日の発注状況

安全装置(config で変更):
  - 1日の発注回数上限
  - 最大建玉
  - --confirm を付けないと送信しない(ドライラン)
"""
import argparse, json, os, re, sys, time, urllib.request, urllib.parse
from datetime import datetime, timezone, timedelta

BASE = os.path.dirname(os.path.abspath(__file__))
ENV = os.path.join(BASE, ".secrets", "crosstrade.env")
LOG = os.path.join(BASE, ".secrets", "order_log.json")

# ---- リスク制限 ----
# 上限の趣旨は「1日に何回リスクを取るか」。未約定でキャンセルした指値は
# 建玉になっていないので、--cancel で取り消せばカウントから外れる。
# 回数上限は撤廃済み。0 は無制限を表し、必要な場合だけ env で一時的に
# `NQX_MAX_ORDERS_PER_DAY` を正数へ設定する。停止条件は dayguard の日次損失・
# 連敗・冷却であり、固定回数で機会を捨てない。
try:
    MAX_ORDERS_PER_DAY = max(0, int(os.environ.get("NQX_MAX_ORDERS_PER_DAY", "0")))
except ValueError:
    MAX_ORDERS_PER_DAY = 0
MAX_QTY            = 2      # rebound from execution_contract after imports
# 1トレードのリスク上限。TRADING_CONTEXT の原則は「その日の実効枠の 10%」。
# 実効枠は口座ごとに違う(LUCIDFLEX 25K は日次上限 $600 → $60)。口座を替えた
# ときに変え忘れないよう、.secrets/crosstrade.env の MAX_RISK_DOLLARS で上書きできる。
MAX_RISK_DOLLARS   = 240.0
SYMBOL             = "MNQU6"   # R102: import 後に contract.py(取引限月の正本)で上書きする

# 建玉照会ができない状態での新規ライブ発注は、未確認を FLAT と
# 誤解するため禁止する。dry-run は照会せず生成できるが、--confirm は
# broker_status の verified=true を必須にする。
import broker_status
import execution_contract
import execution_intent
import management_intent
import pyramid
import route_identity
import contract as contract_month  # R102: 取引限月の正本

MAX_QTY = int(execution_contract.CONTRACT["risk"]["maxQty"])
MAX_RISK_DOLLARS = float(execution_contract.CONTRACT["risk"]["defaultCapDollars"])
SYMBOL = contract_month.symbol()                    # R102: 発注先の限月は正本から(MNQU6 の直書きを廃止)


def last_divergence_pt(cfg=None):
    """R102: 判断に使った現在値(--last)と送信直前の quote の許容乖離(pt)。既定 40pt。"""
    raw = os.environ.get("NQX_LAST_DIVERGENCE_PT") or (cfg or {}).get("NQX_LAST_DIVERGENCE_PT") or "40"
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = 40.0
    return max(value, 0.25)


def require_verified_flat(symbol, accounts=None, purpose="live order"):
    scope = [str(value) for value in (accounts or []) if str(value)]
    if not scope:
        scope = [None]
    for account in scope:
        result = broker_status.query_position(symbol, account=account)
        label = str(account or result.get("accountId") or "configured account")
        if not result.get("verified"):
            detail = result.get("detail") or "broker position query unavailable"
            sys.exit(f"ERROR: {purpose} blocked — {label} position is UNVERIFIED ({detail})")
        qty = int(result.get("qty") or 0)
        if qty > 0:
            side = result.get("side") or "UNKNOWN"
            sys.exit(f"ERROR: {purpose} blocked — {label} POSITION OPEN {side} {qty} "
                     f"({result.get('source')})")

#: R84 `--pyramid-base=<qty>@<avgEntry>`。engine が観測した基礎建玉の宣言。
PYRAMID_BASE_RE = re.compile(r"^(\d{1,4})@(\d+(?:\.\d+)?)$")


def parse_pyramid_base(raw):
    """``<qty>@<avgEntry>`` を ``(qty, avgEntry)`` へ。読めなければ ``None``。"""
    match = PYRAMID_BASE_RE.match(str(raw or "").strip())
    if not match:
        return None
    qty = int(match.group(1))
    average = float(match.group(2))
    if qty <= 0 or not (average > 0):
        return None
    return qty, average


def require_verified_pyramid_base(symbol, account, side, base_qty, base_avg_entry,
                                  purpose="live pyramid add"):
    """R84 §6-1: 追撃は FLAT ではなく **宣言どおりの基礎建玉** を要求する。

    `require_verified_flat` の代わりに置く唯一のゲート差し替え。engine の観測と
    送信瞬間の実態がズレた周期は、ここで落ちて**見送り**になるだけである
    (何も取り消さず、何も送らない)。照会不能を「建玉あり」とも「FLAT」とも
    読み替えない。
    """
    result = broker_status.query_position(symbol, account=account)
    label = str(account or result.get("accountId") or "configured account")
    if not result.get("verified"):
        detail = result.get("detail") or "broker position query unavailable"
        sys.exit(f"ERROR: {purpose} blocked — {label} position is UNVERIFIED ({detail})")
    qty = int(result.get("qty") or 0)
    if qty != int(base_qty):
        sys.exit(f"ERROR: PYRAMID_BASE_QTY_MISMATCH: {label} の建玉は {qty} 枚で、"
                 f"宣言された基礎建玉 {base_qty} 枚と違います")
    expected_side = "LONG" if str(side).lower() == "buy" else "SHORT"
    if str(result.get("side") or "").upper() != expected_side:
        sys.exit(f"ERROR: PYRAMID_BASE_SIDE_MISMATCH: {label} の建玉は "
                 f"{result.get('side')} で、追撃の向き {expected_side} と違います")
    if str(result.get("symbol") or "") != str(symbol):
        sys.exit("ERROR: PYRAMID_BASE_SYMBOL_MISMATCH")
    position_account = result.get("accountId") or result.get("account")
    if position_account in (None, "") or str(position_account) != str(account):
        sys.exit("ERROR: PYRAMID_BASE_ACCOUNT_MISMATCH")
    average = result.get("avgEntry")
    try:
        average = float(average)
    except (TypeError, ValueError):
        average = None
    if average is None or average <= 0:
        sys.exit("ERROR: PYRAMID_BASE_ENTRY_UNAVAILABLE: broker did not return an "
                 "average entry for the base position; combined risk cannot be verified")
    limit = float(execution_contract.CONTRACT["marketOrder"]["maxDeviationPoints"])
    if abs(average - float(base_avg_entry)) > limit + 1e-9:
        sys.exit(f"ERROR: PYRAMID_BASE_ENTRY_DEVIATION: 建玉の平均建値 {average} が"
                 f"宣言 {base_avg_entry} から {limit:g}pt 以上離れています")
    print(f"pyramid base verified: {label} {expected_side} {qty}枚 @ {fmt_price(average)}")
    return result


def load_env():
    cfg = {}
    if not os.path.exists(ENV):
        sys.exit(f"ERROR: {ENV} が見つかりません")
    with open(ENV, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip()
    return cfg


def configured_accounts(cfg):
    """Return the explicit CrossTrade account allowlist for today's route.

    ``CROSSTRADE_ACCOUNTS`` is a comma/newline separated allowlist.  The
    legacy single-account variable remains the fallback so existing setups
    keep behaving exactly as before.  Account names are validated here rather
    than accepting a value from a Telegram/Mini App payload.
    """
    raw = cfg.get("CROSSTRADE_ACCOUNTS", "").strip()
    if raw:
        candidates = re.split(r"[,\n]+", raw)
    else:
        candidates = [cfg.get("CROSSTRADE_ACCOUNT", "")]

    accounts = []
    for value in candidates:
        account = value.strip()
        if not account:
            continue
        if not re.fullmatch(r"[A-Za-z0-9_-]{3,64}", account):
            sys.exit("ERROR: CROSSTRADE account allowlist contains an invalid account id")
        if account not in accounts:
            accounts.append(account)

    if not accounts:
        sys.exit("ERROR: CROSSTRADE_ACCOUNT(S) is not configured")
    return accounts


def account_risk_cap(cfg, account):
    """口座ごとのリスク上限。未設定なら共通の MAX_RISK_DOLLARS にフォールバックする。

    `.secrets/crosstrade.env` に
        RISK_LFE00000000000002=60
    のように口座IDを付けて書く。多口座に同時発注するとき、口座ごとに
    残り枠が違うので**共通の1つの上限では守れない**(枠 $249 の口座に
    とって $60 は 24%)。ここで口座別に判定する。
    """
    # Share the same account-scoped source resolution used by bot/monitor.
    cap, _source = execution_contract.risk_cap({**cfg, "CROSSTRADE_ACCOUNTS": str(account)})
    if cap is None or cap <= 0:
        sys.exit(f"ERROR: execution contract risk cap unavailable for {account}")
    return cap

    raw = cfg.get(f"RISK_{account}")
    if raw is None or str(raw).strip() == "":
        raw = cfg.get("MAX_RISK_DOLLARS", MAX_RISK_DOLLARS)
    try:
        cap = float(raw)
    except (TypeError, ValueError):
        sys.exit(f"ERROR: RISK_{account} が数値ではありません: {raw!r}")
    if cap <= 0:
        sys.exit(f"ERROR: RISK_{account} は正の数にしてください: {cap}")
    return cap


def market_slippage_pt(cfg):
    """成行の滑り緩衝(pt)。概算リスク = (|現在値 − SL| + これ) × 枚数 × $2.00。

    成行は約定価格が事前に確定しないため、現在値ぴったりでリスクを見積もると
    実際の約定が常に不利側へずれる。緩衝を上乗せした概算で口座別上限を
    判定することで、「概算では枠内・約定したら枠超え」を防ぐ。
    `.secrets/crosstrade.env` の MARKET_SLIPPAGE_PT で変更できる。
    """
    raw = cfg.get("MARKET_SLIPPAGE_PT", 2.0)
    try:
        slip = float(raw)
    except (TypeError, ValueError):
        sys.exit(f"ERROR: MARKET_SLIPPAGE_PT が数値ではありません: {raw!r}")
    if slip < 0:
        sys.exit(f"ERROR: MARKET_SLIPPAGE_PT は 0 以上にしてください: {slip}")
    return slip


STOP_MARKET_BUFFER_PT_DEFAULT = 4.0


def stop_market_buffer_pt(cfg):
    """R78: 保護 stop と現在値の最小距離(pt)。autotrade_engine と同じ既定値・同じ環境変数。

    `cancelandbracket` は取消→新規の 2 段。新しい stop が価格に跨がれていると
    ブローカーが拒否し、取消だけが成立して runner が保護注文ゼロで残る
    (2026-09-11 01:40:48 実測: BUY STOP 29,174.25 を価格 29,174.25 以上で送って拒否)。
    """
    raw = os.environ.get("NQX_STOP_MARKET_BUFFER_PT", cfg.get("NQX_STOP_MARKET_BUFFER_PT")
                         if isinstance(cfg, dict) else None)
    try:
        value = float(raw) if raw not in (None, "") else STOP_MARKET_BUFFER_PT_DEFAULT
    except (TypeError, ValueError):
        value = STOP_MARKET_BUFFER_PT_DEFAULT
    if value != value or value < 0:      # NaN / 負値
        value = STOP_MARKET_BUFFER_PT_DEFAULT
    return max(value, 0.25)              # 1 tick 未満にはしない


def modify_reference_price(last):
    """R78: 張り替え直前の参照価格 ``(price, source)``。

    TradingView CLI の quote(数秒以内)を最優先し、取れなければ engine が判断に使った
    ``--last``。どちらも無ければ ``(None, None)``(ガードは省略され、その旨を印字する)。
    quote は `NQX_MODIFY_FRESH_QUOTE=0` で止められる。隔離テスト環境には tv_fetch が
    無いので、テストは ``--last`` だけでガードを検証する。
    """
    flag = str(os.environ.get("NQX_MODIFY_FRESH_QUOTE", "1")).strip().lower()
    if flag not in ("0", "false", "no", "off"):
        try:
            import tv_fetch
            _raw, parsed = tv_fetch._cli_json(["quote"], timeout=20)
            value = parsed.get("last")
            if value is None:
                value = parsed.get("close")
            value = float(value)
            # R102: quote は前面チャートの銘柄。発注先の限月そのものでなければ使わない
            # (連続足 MNQ1! が別限月へロールしていた 2026-09-15 の再発防止)。
            quote_symbol = str(parsed.get("symbol") or "")
            front = None
            if contract_month.is_continuous(quote_symbol):
                # R102b: 連続足は front_contract で「今どの限月か」を解決してから採用する。
                front = str(tv_fetch.chart_symbol_info().get("front_contract") or "") or None
            resolved = contract_month.resolved_contract(quote_symbol, front)
            if value > 0 and resolved and contract_month.same_contract(resolved, contract_month.symbol()):
                return value, "quote"
        except Exception:  # noqa: BLE001 - quote が無ければ --last へ
            pass
    try:
        if last is not None and float(last) > 0:
            return float(last), "--last"
    except (TypeError, ValueError):
        pass
    return None, None


def stop_side_guard(side, stop, reference, buffer):
    """R78: stop が現在値から buffer 以上「守れる側」にあれば True。

    SELL 建玉の stop(BUY STOP)は現在値より上、BUY 建玉の stop(SELL STOP)は下。
    """
    if reference is None:
        return True
    if str(side).lower() == "sell":
        return float(stop) >= float(reference) + float(buffer)
    return float(stop) <= float(reference) - float(buffer)


def new_order_accounts(cfg, accounts):
    """新規発注(PLACE)だけに適用する口座フィルタ。

    ``BLOCK_NEW_ACCOUNTS`` は新規建玉を止めたい口座を一時的に外すための
    allowlist の対になる deny-list。--flatten / --modify / --status / --cancel
    はこの関数を通らない(呼び出し元は main() の新規発注パスのみ)ので、
    ブロック中の口座でも既存ポジションの決済・張り替えは塞がれない
    (撤退経路を絶対に塞がない、という dayguard と同じ設計原則)。
    """
    raw = cfg.get("BLOCK_NEW_ACCOUNTS", "").strip()
    if not raw:
        return accounts
    blocked = {v.strip() for v in re.split(r"[,\n]+", raw) if v.strip()}
    filtered = [a for a in accounts if a not in blocked]
    for account in accounts:
        if account in blocked:
            print(f"  新規発注ブロック中 {account}(BLOCK_NEW_ACCOUNTS)")
    if not filtered:
        sys.exit("ERROR: BLOCK_NEW_ACCOUNTS が発注先を全て除外しました。"
                  "\n新規発注できる口座がありません。")
    return filtered


def split_accounts_by_risk(cfg, accounts, risk):
    """リスク額で発注先を振り分ける。

    上限を超える口座は**その口座だけ外す**。1枚が最小なので枚数では
    調整できず、超える口座は「今回は見送る」以外に選択肢がない。
    全体を止めると、枠が足りている口座まで巻き添えになる。
    """
    allowed, skipped = [], []
    for account in accounts:
        cap = account_risk_cap(cfg, account)
        (allowed if risk <= cap else skipped).append((account, cap))
    return allowed, skipped


def post_to_accounts(cfg, accounts, lines_for_account, label,
                     identity_probe=None, identities=None, identity_settle=None):
    """Send one independently auditable webhook per configured account.

    All accounts are attempted so a transient failure on one route does not
    silently leave the other route unattempted.  The aggregate succeeds only
    when every account returns a 2xx response; partial delivery is reported as
    a failure so the caller cannot display a false all-clear.

    ``identity_settle(results)`` (R76) runs once after every attempt and may
    fill identities the per-attempt probe missed, before acceptance is judged.
    """
    results = []
    resolved = identities if identities is not None else {}
    for account in accounts:
        lines = lines_for_account(account)
        print(f"--- {label.upper()} account={account} ---")
        print("\n".join(lines).replace(cfg["CROSSTRADE_KEY"], "***KEY***"))
        status, body = post(cfg, lines, f"{label}:{account}")
        results.append((account, status, body))
        if identity_probe is not None:
            identity = identity_probe(account, None)
            if identity:
                resolved[(account, None)] = identity
    if identity_settle is not None:
        identity_settle(results)

    failed = [(account, status) for account, status, body in results
              if route_attempt_state(status, body, resolved.get((account, None))) != "ACCEPTED"]
    if failed:
        # stdout carries the machine envelope and route_envelope.parse treats
        # ERROR/FAILED/... anywhere in it as a poisoned route.  This wording
        # must stay outside that marker set, otherwise every PARTIAL route is
        # downgraded to UNKNOWN and the frozen-leg path becomes unreachable.
        print(f"ROUTE NOT_ALL_ACCEPTED: {failed}")
        return False, results
    return True, results


def post_split_to_accounts(cfg, accounts, lines_for_account, label,
                           identity_probe=None, identities=None, identity_settle=None):
    """Send independently bracketed split legs and report every account/leg.

    A partial response is deliberately a failure: the caller must halt and
    reconcile the broker, never resend the missing leg blindly.

    ``identity_settle(results)`` (R76) runs once after every leg was posted and
    may bind legs the per-leg window missed, before acceptance is judged.
    """
    results = []
    resolved = identities if identities is not None else {}
    for account in accounts:
        for leg, lines in lines_for_account(account):
            print(f"--- {label.upper()} account={account} leg={leg} ---")
            print("\n".join(lines).replace(cfg["CROSSTRADE_KEY"], "***KEY***"))
            status, body = post(cfg, lines, f"{label}:{account}:{leg}")
            results.append((account, leg, status, body))
            # One snapshot per leg: TP1 and RUNNER carry identical entry
            # economics and can only be told apart by the window in which
            # their broker row appeared.
            if identity_probe is not None:
                identity = identity_probe(account, leg)
                if identity:
                    resolved[(account, leg)] = identity
    if identity_settle is not None:
        identity_settle(results)
    failed = [(account, leg, status) for account, leg, status, body in results
              if route_attempt_state(status, body, resolved.get((account, leg))) != "ACCEPTED"]
    if failed:
        # Same marker constraint as post_to_accounts.
        print(f"ROUTE NOT_ALL_ACCEPTED: {failed}")
        return False, results
    return True, results


def _orders_snapshot(symbol, account=None):
    """Read-only broker order view used to bracket exactly one route attempt.

    Identity acquisition is best-effort by design: it may never turn a send
    into a crash, and an unavailable/unverified view simply leaves the attempt
    classified by its HTTP result.
    """
    try:
        view = broker_status.query_orders(symbol, account=account)
    except Exception:  # noqa: BLE001 - acquisition must never break the route
        return None
    return view if isinstance(view, dict) and view.get("verified") is True else None


#: R52: POST 直後の一覧はブローカー側に行が現れる前に読めてしまう。
#: 2026-09-04 23:57 実測: HTTP 200 の応答から 0.5〜1.3 秒後に注文行が出た。
IDENTITY_SETTLE_ATTEMPTS = max(1, int(os.environ.get("NQX_IDENTITY_SETTLE_ATTEMPTS", "8")))
IDENTITY_SETTLE_DELAY_SEC = max(0.0, float(os.environ.get("NQX_IDENTITY_SETTLE_DELAY_SEC", "0.5")))


def _orders_snapshot_settled(symbol, before, account=None, attempts=None, delay=None,
                             sleep=None):
    """送信直後の注文一覧を、新しい行が見えるまで短く待って読み直す(R52)。

    `before` が無い(送信前の一覧が取れていない)ときは比較できないので従来どおり
    1 回だけ読む。一覧が照会不能(None)になったらそこで止める。行の比較が
    できない形(orderId 欠落など)も待たずに返す —— 待って直る種類の欠落ではない。
    上限は attempts × delay 秒(既定 8 × 0.5 = 3.5 秒)。claim の maxAgeSec 180 秒の
    内側に十分収まる。
    """
    attempts = IDENTITY_SETTLE_ATTEMPTS if attempts is None else max(1, int(attempts))
    delay = IDENTITY_SETTLE_DELAY_SEC if delay is None else max(0.0, float(delay))
    sleep = sleep or time.sleep
    after = _orders_snapshot(symbol, account=account)
    if before is None:
        return after
    tries = 1
    while after is not None and tries < attempts:
        fresh = route_identity.new_rows(before, after)
        if fresh is None or fresh:
            break
        sleep(delay)
        after = _orders_snapshot(symbol, account=account)
        tries += 1
    return after


#: R76: 成行の即時約定では親行が working 一覧に出ず、子(ブラケット)が応答の 0.5〜数秒後に
#: 現れる。送信直後の一覧は一過性の状態(親 PendingNew / 子 Suspended→Working の切替)で
#: Unavailable にもなる(2026-09-08 10:34 実測)。その None を跨いで読み続ける窓
#: (既定 10 × 1.0 秒)。束縛できた時点で止まるので、通常は 1〜2 回で抜ける。
IDENTITY_BRACKET_ATTEMPTS = max(1, int(os.environ.get("NQX_IDENTITY_BRACKET_ATTEMPTS", "10")))
IDENTITY_BRACKET_DELAY_SEC = max(0.0, float(os.environ.get("NQX_IDENTITY_BRACKET_DELAY_SEC", "1.0")))
#: R76: 1 脚あたりの壁時計の予算(秒)。CrossTrade の応答は同じ照会でも 0.6〜6.2 秒ばらつく
#: (2026-09-07 実測)ので、回数だけで縛ると 2 脚 + 約定履歴で engine の子プロセス上限
#: (150 秒)に迫る。予算を使い切ったら次の読みには入らない。
IDENTITY_BRACKET_BUDGET_SEC = max(1.0, float(os.environ.get("NQX_IDENTITY_BRACKET_BUDGET_SEC", "15")))


def _entry_orders_identity(symbol, before, *, account, action, qty, order_type, entry_price,
                           exclude_order_ids=None, parent_qty=None, attempts=None, delay=None,
                           sleep=None, snapshot=None, augment=None, budget=None, clock=None):
    """送信直後の注文一覧を短く読み続け、ENTRY 脚の identity を束縛する(R76)。

    戻り値 ``(identity, after_view)``。読みごとに 2 つの束縛を試す:

    1. ``route_identity.bind_entry_leg`` —— 窓に現れた **親行**(指値の WORKING、または
       per-order 詳細で読み足した成行の FILLED 親)
    2. ``route_identity.bind_entry_leg_from_brackets`` —— 窓に現れた **子 2 行**(逆方向・
       active・相互 parentId)。成行の親が一覧に一度も出ないときの証拠

    ``before`` が無ければ 1 回だけ読んで比較しない(従来どおり)。行の比較ができない形
    (orderId 欠落など)は待たずに返す —— 待って直る種類の欠落ではない。一覧が一過性に
    None(Unavailable)でも上限まで読み直す。上限は attempts × delay 秒と、壁時計の
    ``budget`` 秒の狭い方。
    """
    snapshot = snapshot or _orders_snapshot
    augment = augment or _augment_market_parents
    attempts = IDENTITY_BRACKET_ATTEMPTS if attempts is None else max(1, int(attempts))
    delay = IDENTITY_BRACKET_DELAY_SEC if delay is None else max(0.0, float(delay))
    budget = IDENTITY_BRACKET_BUDGET_SEC if budget is None else max(0.0, float(budget))
    sleep = sleep or time.sleep
    clock = clock or time.monotonic
    wanted_type = str(order_type or "").upper()
    last = None
    started = clock()
    for attempt in range(attempts):
        if attempt and clock() - started + delay > budget:
            # 予算を使い切った。次の読み(応答が数秒かかりうる)には入らない。
            break
        after = snapshot(symbol, account=account)
        if after is not None:
            if before is None:
                return None, after
            if wanted_type == "MARKET":
                # R52: 成行の親は一覧に出ない。子行の真の親 id から詳細を読み足す。
                after = augment(symbol, before, after, account=account)
            last = after
            if route_identity.new_rows(before, after) is None:
                return None, after
            identity = route_identity.bind_entry_leg(
                before, after, account=account, symbol=symbol, action=action, qty=qty,
                order_type=order_type, entry_price=entry_price)
            if identity is not None:
                # 親行で束縛できた脚にも、同じ窓のその親の子 2 行を添える(親 id が後で
                # 解決できなくなっても構造で照合できるように)。束縛の可否は変えない。
                identity = route_identity.attach_bracket_ids(
                    identity, before, after, account=account, symbol=symbol, action=action)
            else:
                identity = route_identity.bind_entry_leg_from_brackets(
                    before, after, account=account, symbol=symbol, action=action, qty=qty,
                    exclude_order_ids=exclude_order_ids, parent_qty=parent_qty)
            if identity is not None:
                return identity, after
        if attempt < attempts - 1:
            sleep(delay)
    return None, last


def _bound_identity_ids(identities):
    """束縛済み identity の注文 id(親)と子 id の集合。次の脚の候補から外すために使う。"""
    bound = set()
    for value in (identities or {}).values():
        if isinstance(value, dict):
            bound |= route_identity._identity_ids(value)
    return bound


def _late_bind_entry_legs(symbol, account, origin_view, *, action, order_type, entry_price,
                          legs, bound, parent_qty=None, market=False, attempts=None,
                          delay=None, sleep=None, snapshot=None, augment=None):
    """全脚の送信後、未束縛の脚を送信前の窓に対してまとめて束縛する最終パス(R76)。

    脚ごとの窓が取りこぼした(TP1 の窓では子がまだ無く、RUNNER の窓に 2 対が現れた等)
    ケースの救済。規則は ``route_identity.late_bind_entry_legs``(候補数 = 未束縛の脚数
    のときだけ、枚数の証拠か作成順で割り当てる)。origin が無ければ何もしない。
    """
    if origin_view is None or not legs:
        return {}
    snapshot = snapshot or _orders_snapshot
    augment = augment or _augment_market_parents
    attempts = IDENTITY_SETTLE_ATTEMPTS if attempts is None else max(1, int(attempts))
    delay = IDENTITY_SETTLE_DELAY_SEC if delay is None else max(0.0, float(delay))
    sleep = sleep or time.sleep
    for attempt in range(attempts):
        after = snapshot(symbol, account=account)
        if after is not None:
            if market:
                after = augment(symbol, origin_view, after, account=account)
            late = route_identity.late_bind_entry_legs(
                origin_view, after, account=account, symbol=symbol, action=action,
                order_type=order_type, entry_price=entry_price, legs=legs, bound=bound,
                parent_qty=parent_qty)
            if late:
                return late
        if attempt < attempts - 1:
            sleep(delay)
    return {}


def _fills_snapshot(account=None):
    """R72: 約定履歴の読み取り専用スナップショット(identity 用)。照会不能は None。"""
    try:
        view = broker_status.query_fills(account=account)
    except Exception:  # noqa: BLE001 - identity は best-effort。送信を壊さない
        return None
    return view if isinstance(view, dict) and view.get("verified") is True else None


def _fills_snapshot_settled(account, before, action=None, attempts=None, delay=None,
                            sleep=None, snapshot=None):
    """送信直後の約定履歴を、この脚の方向の新しい約定が見えるまで短く待って読み直す(R72)。

    注文窓(`_orders_snapshot_settled`)と違い、照会が None(一過性の Unavailable)でも
    上限まで読み直す —— 約定履歴には注文状態が無いので、待てば直る失敗しか無い。
    上限は attempts × delay 秒(既定 8 × 0.5 = 3.5 秒)。
    """
    snapshot = snapshot or _fills_snapshot
    attempts = IDENTITY_SETTLE_ATTEMPTS if attempts is None else max(1, int(attempts))
    delay = IDENTITY_SETTLE_DELAY_SEC if delay is None else max(0.0, float(delay))
    sleep = sleep or time.sleep
    wanted = str(action or "").upper()
    after = snapshot(account)
    if before is None:
        return after
    tries = 1
    while tries < attempts:
        fresh = route_identity.new_fills(before, after) if after is not None else None
        if fresh and (not wanted or any(str(row.get("action") or "").upper() == wanted
                                          for row in fresh if isinstance(row, dict))):
            break
        sleep(delay)
        after = snapshot(account)
        tries += 1
    return after


def _entry_fills_identity(account, before, *, symbol, action, qty, exclude_order_ids=None,
                          settle=True, snapshot=None, sleep=None):
    """R72: 約定履歴から ENTRY 脚の identity を束縛する。戻り値 ``(identity, after_view)``。

    なぜ要るか: 成行 ENTRY は POST 直後に親 PendingNew → Filled、子 Suspended → Working と
    状態が連続で変わり、注文一覧の窓はその一瞬の状態が許可リスト外だと照会ごと
    Unavailable になって脚の identity が None に確定する(2026-09-08 10:34 / 21:16 実測:
    成行 2 脚とも UNKNOWN → HALT、建玉は管理外で TP1 後の建値移動も走らなかった)。
    約定履歴は状態を持たず ``orderId``(= 親 ENTRY 注文)・数量・価格・時刻を返すので、
    注文窓が落ちた脚をここで拾う。束縛の規則は ``route_identity.bind_entry_leg_from_fills``。

    ``settle=False`` は注文窓で既に束縛できた脚: 待たずに 1 回だけ読み、次の脚の窓を
    進めるためだけに使う。``before`` が無ければ比較できないので束縛しない(推測しない)。
    """
    if settle:
        after = _fills_snapshot_settled(account, before, action=action, snapshot=snapshot,
                                        sleep=sleep)
    else:
        after = (snapshot or _fills_snapshot)(account)
    if before is None or after is None:
        return None, after
    identity = route_identity.bind_entry_leg_from_fills(
        before, after, account=account, symbol=symbol, action=action, qty=qty,
        exclude_order_ids=exclude_order_ids)
    return identity, after


def _augment_market_parents(symbol, before, after, account=None, query_orders=None):
    """成行 ENTRY の **親行** を per-order 詳細で読み足した一覧を返す(R52)。

    CrossTrade/Tradovate の一覧は working な注文だけを返す。成行の親は POST の
    直後に FILLED になり **一覧に一度も現れない** ので、窓には子(SL/TP の OCO 対)
    しか出ず `bind_entry_leg` が候補ゼロ → UNKNOWN → HALT になった
    (2026-09-05 04:27 実測: 28 枚成行が約定していたのに束縛できなかった)。

    窓に新しく現れた子行が持つ `brokerParentId`(raw の parentId=真の親)を集め、
    その id を `known_order_ids` で per-order 詳細から読み、`after` に無ければ足す。
    足した親は FILLED / 親 id なしの通常の行なので、束縛の判定自体は変えない。
    親 id が取れない・照会できない・既知の id しか無いときは `after` をそのまま返す
    (推測で行を作らない)。
    """
    if not isinstance(after, dict) or after.get("verified") is not True:
        return after
    fresh = route_identity.new_rows(before, after) if before is not None else None
    if not fresh:
        return after
    present = {str(row.get("orderId") or "") for row in (after.get("orders") or [])
               if isinstance(row, dict)}
    parent_ids = sorted({str(row.get("brokerParentId") or "") for row in fresh
                         if isinstance(row, dict) and row.get("brokerParentId") not in (None, "")}
                        - present - {""})
    if not parent_ids:
        return after
    query_orders = query_orders or broker_status.query_orders
    try:
        detail = query_orders(symbol, known_order_ids=parent_ids, account=account)
    except Exception:  # noqa: BLE001 - identity は best-effort。送信を壊さない
        return after
    if not isinstance(detail, dict) or detail.get("verified") is not True:
        return after
    parents = [row for row in (detail.get("orders") or [])
               if isinstance(row, dict) and str(row.get("orderId") or "") in parent_ids]
    if not parents:
        return after
    augmented = dict(after)
    augmented["orders"] = list(after.get("orders") or []) + parents
    augmented["marketParentIds"] = [str(row.get("orderId")) for row in parents]
    return augmented


#: R52: flatten 送信後、ブローカー側で注文が消えるまでの待ち(identity と同じ実測値)。
FLATTEN_SETTLE_ATTEMPTS = max(1, int(os.environ.get("NQX_FLATTEN_SETTLE_ATTEMPTS", "8")))
FLATTEN_SETTLE_DELAY_SEC = max(0.0, float(os.environ.get("NQX_FLATTEN_SETTLE_DELAY_SEC", "0.5")))


def _flatten_verified(accounts, symbol, query_position=None, query_orders=None,
                      attempts=None, delay=None, sleep=None):
    """送信後、各口座が FLAT かつ注文が非 blocking になるまで短く待って再照会する(R52)。

    戻り値 ``(verified_accounts, failed_accounts)``。照会不能は「未確認」であり、
    成功扱いにしない。上限は attempts × delay 秒(既定 8 × 0.5 = 3.5 秒)。
    """
    query_position = query_position or broker_status.query_position
    query_orders = query_orders or broker_status.query_orders
    attempts = FLATTEN_SETTLE_ATTEMPTS if attempts is None else max(1, int(attempts))
    delay = FLATTEN_SETTLE_DELAY_SEC if delay is None else max(0.0, float(delay))
    sleep = sleep or time.sleep
    blocking = set(execution_contract.CONTRACT["blockingOrderStates"])
    pending = [str(account) for account in accounts]
    verified = []
    for attempt in range(attempts):
        still = []
        for account in pending:
            try:
                position_after = query_position(symbol, account=account)
                orders_after = query_orders(symbol, account=account)
            except Exception:  # noqa: BLE001 — 照会不能は未確認(成功扱いしない)
                still.append(account)
                continue
            order_state = str((orders_after or {}).get("state") or "UNKNOWN").upper()
            if (isinstance(position_after, dict) and position_after.get("verified") is True
                    and int(position_after.get("qty") or 0) == 0
                    and isinstance(orders_after, dict) and orders_after.get("verified") is True
                    and order_state not in blocking):
                verified.append(account)
            else:
                still.append(account)
        pending = still
        if not pending or attempt == attempts - 1:
            break
        sleep(delay)
    return verified, pending


#: R52: cancelandbracket は取消→新規の 2 段。2026-09-05 02:49 実測で新しい対の出現が
#: 応答の 3〜4 秒後だったため、既定 8×0.5 秒では取りこぼした。張り替えは長めに待つ。
MODIFY_SETTLE_ATTEMPTS = max(1, int(os.environ.get("NQX_MODIFY_SETTLE_ATTEMPTS", "24")))


def _late_bind_replacement(symbol, account, origin_view, *, action, qty, stop, target,
                           attempts=None, sleep=None):
    """送信前スナップショット(origin)に対して、張り替え後の対をもう一度だけ束縛する(R52)。

    probe が窓を取り違えた/待ち切れなかった場合の救済。origin が無ければ何もしない。
    """
    if origin_view is None:
        return None
    late = _orders_snapshot_settled(symbol, origin_view, account=account,
                                    attempts=MODIFY_SETTLE_ATTEMPTS if attempts is None else attempts,
                                    sleep=sleep)
    if late is None:
        return None
    return route_identity.bind_replacement_bracket(
        origin_view, late, account=account, symbol=symbol, action=action,
        qty=qty, stop=stop, target=target)


def _protective_settled(symbol, account, known_order_ids, expected_action,
                        attempts=None, delay=None, sleep=None):
    """張り替え後、口座の逆方向行が「建玉に対する OCO 兄弟 1 組」に落ち着くまで短く待つ(R52/R80)。

    R80: 「逆方向 active 行が 2 本」では、OSO の子(SUSPENDED、親の約定まで起動しない)や
    `parentId` だけで結ばれた行も 2 本に数えてしまう(2026-09-12 01:53 の疑義)。判定は
    broker_status.oco_sibling_pair() で、成立しないまま attempts を使い切ったら最後の理由を
    印字して呼び出し側の照合(verify_protective_orders → MODIFY_POSTVERIFY_UNKNOWN)に落とす。
    """
    attempts = FLATTEN_SETTLE_ATTEMPTS if attempts is None else max(1, int(attempts))
    delay = FLATTEN_SETTLE_DELAY_SEC if delay is None else max(0.0, float(delay))
    sleep = sleep or time.sleep
    view, reason = None, None
    for attempt in range(attempts):
        view = broker_status.query_orders(symbol, known_order_ids=known_order_ids, account=account)
        rows = view.get("orders") if isinstance(view.get("orders"), list) else (view.get("activeOrders") or [])
        pair, reason = broker_status.oco_sibling_pair(
            rows, account=account, expected_action=expected_action, symbol=symbol, all_rows=rows)
        if pair is not None or attempt == attempts - 1:
            break
        sleep(delay)
    if reason:
        print(f"protective settle: {reason}")
    return view


def route_attempt_state(status, body, identity=None):
    """Classify one webhook attempt without treating HTTP 2xx as broker success.

    CrossTrade can acknowledge the HTTP request while returning an ERROR body.
    Network/no-status results remain UNKNOWN; only an explicit response with no
    accepted leg is safe to call REJECTED.

    ``identity`` is the broker row that provably appeared during this single
    attempt (see ``route_identity``).  Broker truth outranks the response echo
    in both directions: a bare ``{"success": true}`` becomes ACCEPTED once the
    row exists, and a rejected/timed-out attempt that nevertheless created an
    order is owned rather than left orphaned.  No identity means the HTTP
    result decides, exactly as before.
    """
    broker_order_id = str((identity or {}).get("orderId") or "").strip()
    broker_receipt = str((identity or {}).get("receipt") or "").strip()
    if broker_order_id and broker_receipt:
        return "ACCEPTED"
    raw = str(body or "").strip()
    text = raw.upper()
    if int(status or 0) == 0:
        return "UNKNOWN"
    if "ERROR" in text or "REJECT" in text:
        return "REJECTED"
    if not 200 <= int(status) < 300:
        return "REJECTED"
    # HTTP acceptance is not broker acceptance.  A successful leg must carry
    # a durable broker identity that can later be reconciled and terminalized.
    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return "UNKNOWN"
    data = parsed.get("data") if isinstance(parsed, dict) else None
    if not isinstance(data, dict):
        data = parsed if isinstance(parsed, dict) else {}
    order_id = data.get("orderId") or data.get("id")
    receipt = data.get("receipt") or data.get("requestId")
    return "ACCEPTED" if order_id and receipt else "UNKNOWN"


def classify_route_results(results, split=False, identities=None):
    states = []
    for row in results:
        status, body = (row[2], row[3]) if split else (row[1], row[2])
        key = (row[0], row[1]) if split else (row[0], None)
        states.append(route_attempt_state(status, body, (identities or {}).get(key)))
    accepted = states.count("ACCEPTED")
    rejected = states.count("REJECTED")
    unknown = states.count("UNKNOWN")
    total = len(states)
    if total and accepted == total:
        state = "SENT"
    elif accepted > 0:
        state = "PARTIAL"
    elif unknown > 0:
        state = "UNKNOWN"
    else:
        state = "REJECTED"
    return {"state": state, "acceptedCount": accepted,
            "explicitRejectCount": rejected, "unknownCount": unknown,
            "totalAttempts": total}


def build_route_snapshot(results, split=False, identities=None):
    """Freeze account/leg outcome and broker identity without guessing IDs."""
    snapshot = []
    for row in results:
        if split:
            account, leg, status, body = row
        else:
            account, status, body = row
            leg = None
        identity = (identities or {}).get((account, leg)) or {}
        parsed = None
        try:
            parsed = json.loads(body) if isinstance(body, str) else body
        except json.JSONDecodeError:
            parsed = None
        data = parsed.get("data") if isinstance(parsed, dict) and isinstance(parsed.get("data"), dict) else parsed
        data = data if isinstance(data, dict) else {}
        row = {
            "accountId": str(account), "legId": str(leg) if leg else None,
            "state": route_attempt_state(status, body, identity),
            "orderId": data.get("orderId") or data.get("id") or identity.get("orderId"),
            "receipt": (data.get("receipt") or data.get("requestId")
                        or identity.get("receipt")),
            "filledAt": data.get("filledAt") or identity.get("filledAt"),
        }
        # R76: 束縛の出所と、子(ブラケット)の id / receipt を凍結する。後周期は親行が
        # 読めなくても、この子 2 行の構造で所有権を照合できる(ownership_binder)。
        for extra in ("identitySource", "bracketOrderIds", "bracketReceipts"):
            if identity.get(extra) not in (None, "", []):
                row[extra] = identity[extra]
        snapshot.append(row)
    return snapshot

def trade_date():
    """CME営業日: ET 18:00 で日付が変わる。JST基準で朝7時区切り相当。"""
    now = datetime.now(timezone.utc)
    et = now - timedelta(hours=4)          # 夏時間 EDT
    if et.hour >= 18:
        et += timedelta(days=1)
    return et.strftime("%Y-%m-%d")

def load_log():
    if os.path.exists(LOG):
        with open(LOG, encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_log(log):
    with open(LOG, "w", encoding="utf-8") as f:
        json.dump(log, f, ensure_ascii=False, indent=2)


def fmt_price(value):
    """Keep CME quarter-tick precision in CrossTrade payloads."""
    return f"{float(value):.2f}"

def count_today(log):
    return len(log.get(trade_date(), []))


def count_label(log):
    """発注回数の表示。0=無制限を分母にしない。"""
    count = count_today(log)
    return f"{count}/無制限" if MAX_ORDERS_PER_DAY == 0 else f"{count}/{MAX_ORDERS_PER_DAY}"

def post(cfg, payload_lines, label):
    body = "\n".join(payload_lines)
    data = body.encode("utf-8")
    req = urllib.request.Request(
        cfg["CROSSTRADE_URL"], data=data,
        headers={"Content-Type": "text/plain"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            out = r.read().decode("utf-8", "replace")
            print(f"HTTP {r.status}\n{out}")
            return r.status, out
    except urllib.error.HTTPError as e:
        print(f"HTTP ERROR {e.code}\n{e.read().decode('utf-8','replace')}")
        return e.code, "error"
    except Exception as e:
        print(f"ERROR: {e}")
        return 0, "error"


def exit_code_for(status):
    """送信結果を終了コードに反映する。

    以前は下流が 500 を返しても exit 0 だったため、呼び出し側が
    「送信成功」と誤表示できてしまった。ドライランの成功と約定は別物なので、
    2xx 以外は必ず失敗として返す。
    """
    return 0 if 200 <= status < 300 else 1

# --- 日次ガード(R7)-------------------------------------------------------
# CLAUDE.md §3「提案を止める条件」を機械が強制する。**決済は絶対に止めない。**
# --flatten / --modify / --cancel / --status を塞ぐと、撤退経路を失って
# ガードが守るどころか危険になる。

GUARDED_ACTIONS = ()                # 日次ガード廃止(2026-08-27)。止める操作は無い


def dayguard_gate(state, action, enabled=True):
    """常に True。日次ガードは 2026-08-27 に廃止した。

    呼び出し側と既存テストのために形だけ残してある。``state['blocked']`` は
    ``dayguard.check()`` がもう True にしないので、引数によらず通す。
    """
    return True


def dayguard_state(cfg):
    """当日の集計を返す。取れなくても**発注は止めない**(表示だけ諦める)。"""
    try:
        import dayguard
        env = dict(os.environ)
        env.update(cfg or {})
        return dayguard.check(env=env), None
    except Exception as exc:                     # noqa: BLE001 — 表示専用。止めない
        return None, f"dayguard を評価できません({type(exc).__name__}: {exc})"


def dayguard_enabled():
    """互換のために残す。ガードは廃止済みなので常に False。"""
    return False


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--side", choices=["buy", "sell"])
    p.add_argument("--qty", type=int, default=int(execution_contract.CONTRACT["risk"]["fixedQty"]))
    p.add_argument("--entry", type=float, help="指値価格(省略or--marketで成行)")
    p.add_argument("--sl", type=float, help="損切り価格")
    p.add_argument("--tp", type=float, help="利確価格")
    p.add_argument("--split-tp",
                   help="2枚分割用のTP価格を低い順/高い順にカンマ区切りで指定する。"
                        "qtyと同数の独立OCOブラケットを作る。--tpとは併用不可")
    p.add_argument("--market", action="store_true", help="成行で入る(--last が必須)")
    p.add_argument("--last", type=float,
                   help="発注直前に観測した現在値。成行のリスク概算と口座別判定に必須。"
                        "指値に添えると即約定側の指値(実質成行)を検知して拒否する")
    p.add_argument("--repair-naked", action="store_true",
                   help="R102: --modify 専用。保護注文の OCO 組が 0(裸)の建玉に SL/TP を 1 組で張る修復。"
                        "全脚生存の保護(MODIFY_SPLIT_PLAN_PROTECTED)は、裸がブローカー照会で確認できた"
                        "ときだけ外す")
    p.add_argument("--price-symbol", default=None,
                   help="R102: --last を観測したチャートの銘柄(例 CME_MINI:MNQU2026)。発注先と別の"
                        "限月なら LAST_SYMBOL_MISMATCH で拒否する")
    p.add_argument("--min-stop-pt", type=float,
                   help="R90: 成行のとき、参照価格(quote が取れればそれ、無ければ --last)から SL "
                        "までがこの距離(pt)未満なら MARKET_STOP_TOO_CLOSE で拒否する。engine が "
                        "1.0×ノイズ床を渡す。指値には掛からない")
    p.add_argument("--symbol", default=SYMBOL)
    p.add_argument("--account",
                   help="口座別の管理/全決済対象。CROSSTRADE_ACCOUNTS の許可リスト内のみ")
    p.add_argument("--accounts",
                   help="新規発注の宛先をこの部分集合へ**狭める**(R46)。"
                        "CROSSTRADE_ACCOUNTS の許可リスト内のみ。手動で進行中の"
                        "建玉がある口座を1件だけ外すために使う。増やす用途には使えない")
    p.add_argument("--flatten", action="store_true", help="全決済")
    p.add_argument("--status", action="store_true", help="当日の発注状況")
    p.add_argument("--cancel", type=int, metavar="N",
                   help="当日N番目の発注を記録から取り消す(未約定の指値用)。"
                        "Tradovate側の注文取消は別途手動で行うこと")
    p.add_argument("--modify", action="store_true",
                   help="保有中ポジションのSL/TPを張り替える(cancelandbracket)。"
                        "--side --qty --sl --tp が必要。order_id 不要")
    p.add_argument("--no-tp", action="store_true",
                   help="--modify で TP を意図的に外す(TPが消える点を承知の上で)")
    p.add_argument("--confirm", action="store_true", help="実際に送信(無しはドライラン)")
    p.add_argument("--entry-key", help="Durable Object が発行した ENTRY claim key")
    p.add_argument("--claim-token", help="ENTRY claim の一回限り bearer token")
    p.add_argument("--intent-hash", help="Durable Object が凍結した executionIntent hash")
    p.add_argument("--plan-version", help="正本シナリオのsplit plan version")
    p.add_argument("--ultra", action="store_true",
                   help="ULTRA mode。固定枚数ではなく ULTRA エンベロープで枚数と"
                        "リスクを判定し、TP1/RUNNER を比率分割する。"
                        "--ultra-drawdown が必須(その口座の残ドローダウンが上限の正本)")
    p.add_argument("--ultra-drawdown", type=float,
                   help="ULTRA の口座別リスク上限 $(残ドローダウン)。--ultra と併用必須")
    p.add_argument("--position-generation",
                   help="MODIFY対象として凍結したbroker position generation")
    p.add_argument("--management-key", help="Durable Object MANAGEMENT claim key")
    p.add_argument("--management-token", help="MANAGEMENT claim bearer token")
    p.add_argument("--management-intent-hash", help="frozen MANAGEMENT intent hash")
    p.add_argument("--pyramid", action="store_true",
                   help="R84 追撃(保有中の同方向建て増し)。--split-tp / --market / --last / "
                        "--ultra / --ultra-drawdown / 単一の --accounts / --pyramid-base が必須")
    p.add_argument("--pyramid-base",
                   help="R84 engine が観測した基礎建玉 <qty>@<avgEntry>(例 4@29446.625)")
    p.add_argument("--pyramid-consolidate", action="store_true",
                   help="R84 §5.3 統合: 全 TP1 解決後の runner ブラケット群を 1 組へ畳む --modify")
    p.add_argument("--manual-claim", action="store_true",
                   help="Durable Object が落ちている間の人による一回送信。claim の代わりに "
                        "DO の 5xx 実測と送信直前の verified FLAT で担保する")
    a = p.parse_args()

    # R84: 追撃フラグの相互排他。撤退経路(--flatten)と取消(--cancel)には掛けない。
    if a.pyramid and (a.modify or a.flatten or a.cancel is not None or a.status):
        sys.exit("ERROR: PYRAMID_MODE_CONFLICT: --pyramid は新規の追撃送信専用です")
    if a.pyramid_base and not a.pyramid:
        sys.exit("ERROR: PYRAMID_BASE_WITHOUT_MODE: --pyramid-base は --pyramid と一緒に使います")
    if a.pyramid_consolidate and not a.modify:
        sys.exit("ERROR: PYRAMID_CONSOLIDATE_REQUIRES_MODIFY: "
                 "--pyramid-consolidate は --modify 専用です")
    if a.pyramid and a.manual_claim:
        sys.exit("ERROR: PYRAMID_MANUAL_CLAIM_UNSUPPORTED: "
                 "追撃は必ず Durable Object の claim 経路で送ります")

    cfg = load_env()
    log = load_log()
    today = trade_date()
    accounts = configured_accounts(cfg)
    if a.account:
        if a.account not in accounts:
            sys.exit("ERROR: --account is outside CROSSTRADE_ACCOUNTS allowlist")
        if not (a.modify or a.flatten):
            sys.exit("ERROR: --account is supported only with --modify or --flatten")
        accounts = [a.account]
    if a.accounts:
        # R46: 許可リストを**狭める**だけ。1件でも外にあれば送らない。撤退経路
        # (--flatten / --modify / --cancel)には掛けない。
        if a.modify or a.flatten:
            sys.exit("ERROR: --accounts is supported only for new orders")
        requested = [value.strip() for value in re.split(r"[,\n]+", a.accounts) if value.strip()]
        outside = [value for value in requested if value not in accounts]
        if outside:
            sys.exit("ERROR: --accounts is outside CROSSTRADE_ACCOUNTS allowlist: "
                     + ",".join(outside))
        if not requested:
            sys.exit("ERROR: --accounts is empty")
        accounts = [account for account in accounts if account in set(requested)]

    if a.status:
        orders = log.get(today, [])
        print(f"取引日 {today}: 発注 {count_label(log)} 回")
        for i, o in enumerate(orders, 1):
            print(f"  [{i}] {o['time']}  {o['side']} {o['qty']}枚 entry={o.get('entry','MKT')} "
                  f"sl={o.get('sl')} tp={o.get('tp')}")
        if orders:
            print("\n未約定でキャンセルした指値は --cancel N で記録から外せます")
        return

    if a.cancel is not None:
        orders = log.get(today, [])
        if not 1 <= a.cancel <= len(orders):
            sys.exit(f"ERROR: 1〜{len(orders)} の範囲で指定してください")
        removed = orders.pop(a.cancel - 1)
        save_log(log)
        print(f"記録から削除: {removed['time']} {removed['side']} {removed['qty']}枚 "
              f"entry={removed.get('entry','MKT')}")
        print(f"本日 {count_label(log)} 回")
        print("\n※ Tradovate側の注文取消は別途手動で行ってください")
        return

    if a.modify:
        # cancelandbracket: 既存の保護注文を取り消し、新しいTP/SLを張り直す。
        # flatten_first は付けない(付けるとポジションが決済される)。
        if not a.side:
            sys.exit("ERROR: --side buy|sell が必要です(保有中ポジションの方向)")
        if a.sl is None:
            sys.exit("ERROR: --sl は必須です")
        if a.qty < 1:
            sys.exit(f"ERROR: qty は 1 以上です(指定 {a.qty})")
        modify_ceiling = (int(execution_contract.CONTRACT["ultra"]["maxQtyPerAccount"])
                          if a.ultra else MAX_QTY)
        if a.qty > modify_ceiling:
            sys.exit(f"ERROR: 最大 {modify_ceiling} 枚です(指定 {a.qty})")
        # cancelandbracket は保護注文を丸ごと張り替える。take_profit を送らないと
        # TP が消えた状態のブラケットになる(未実証だが、消える前提で防ぐ)。
        # TP を意図的に外したい場合だけ --no-tp を明示させる。
        if a.tp is None and not a.no_tp:
            sys.exit("ERROR: --modify では --tp も指定してください。\n"
                     "  cancelandbracket は保護注文を張り替えるため、TP を省くと\n"
                     "  既存の TP が消える可能性があります(未検証)。\n"
                     "  TP を外すことが目的なら --no-tp を付けてください。")
        # cancelandbracket は必ず1口座へ限定する。2口座の建玉は同じ時刻でも
        # 約定状態・position generation が別なので、1回の claim でまとめない。
        if len(accounts) != 1:
            sys.exit("ERROR: ACCOUNT_REQUIRED: --modify requires --account when multiple accounts are configured")
        account = accounts[0]

        # R78: 送信直前の現在値で stop が「守れる側」にあるかを、**何も取り消す前に**検査する。
        # engine の判断は数秒〜数分前の価格で、その間に価格が stop を跨ぐと cancelandbracket は
        # 取消だけ成立して runner が裸になる(2026-09-11 01:40:48)。ドライランでも同じ検査を
        # 通すので、engine はドライランの段階で見送りにできる(HALT にしない)。
        reference, reference_source = modify_reference_price(a.last)
        stop_buffer = stop_market_buffer_pt(cfg)
        if reference is None:
            print("stop-side guard: 参照価格なし(quote 不可・--last なし) — 検査を省略")
        elif not stop_side_guard(a.side, a.sl, reference, stop_buffer):
            sys.exit("ERROR: MODIFY_STOP_NOT_PROTECTIVE: "
                     f"stop {fmt_price(a.sl)} は現在値 {fmt_price(reference)}({reference_source})から "
                     f"{stop_buffer:g}pt 以上{'上' if a.side == 'sell' else '下'}にありません。"
                     "cancelandbracket は取消→新規の 2 段で、この stop は拒否されて runner が裸になります。"
                     "既存ブラケットには触れていません。")
        else:
            print(f"stop-side guard: stop {fmt_price(a.sl)} vs {fmt_price(reference)} "
                  f"({reference_source}), buffer {stop_buffer:g}pt OK")

        def lines_for_account(account):
            lines = [f"key={cfg['CROSSTRADE_KEY']};",
                     f"destination={cfg['CROSSTRADE_DEST']};",
                     "command=cancelandbracket;",
                     f"account={account};",
                     f"instrument={a.symbol};",
                     f"action={a.side.upper()};",
                     f"qty={a.qty};",
                     f"stop_loss={fmt_price(a.sl)};"]
            if a.tp:
                lines.append(f"take_profit={fmt_price(a.tp)};")
            return lines

        print("--- MODIFY BRACKET ---")
        print("\n".join(lines_for_account(accounts[0])).replace(
            cfg["CROSSTRADE_KEY"], "***KEY***"))
        for account in accounts[1:]:
            print(f"--- MODIFY BRACKET account={account} ---")
            print("\n".join(lines_for_account(account)).replace(
                cfg["CROSSTRADE_KEY"], "***KEY***"))
        print(f"\n保有 {a.side} {a.qty}枚 の SL を {fmt_price(a.sl)}"
              + (f" / TP を {fmt_price(a.tp)}" if a.tp else "") + " に張り替えます")
        print("※ flatten_first は付けていないのでポジションは維持されます")

        if not a.confirm:
            print("\n[ドライラン] 送信するには --confirm を付けてください")
            return
        if (not a.position_generation or not a.management_key or not a.management_token
                or not a.management_intent_hash):
            sys.exit("ERROR: MANAGEMENT_CLAIM_REQUIRED")
        # 保有中ポジションの保護注文変更も、対象建玉を確認できないまま
        # 実行しない。照会不能時は --confirm 経路を止める。
        verified = broker_status.query_position(a.symbol, account=account)
        if not verified.get("verified"):
            sys.exit("ERROR: modify blocked — position is UNVERIFIED (broker query unavailable)")
        if int(verified.get("qty") or 0) <= 0:
            sys.exit("ERROR: modify blocked — no verified open position")
        # R37: 分割建玉の全量張り替えを拒否する(CLAUDE.md §4.1 の明示的な禁止事項)。
        #
        # `cancelandbracket` は保護注文を**丸ごと**取り消して張り直すコマンドなので、
        # TP1 と runner 最終 TP の 2 本が生きている間に単一の take_profit を送ると
        # **2 本が 1 本に潰れる**。しかも Worker は qty を position.qty で上書きする
        # ため、2 枚建玉中は runner だけの部分 modify を発行する術が無い ——
        # 選べるのは全量 modify だけで、それは必ず TP1 を消す。
        #
        # 自律経路は autotrade_engine 側("qty >= plan の qty なら None")で守られて
        # いたが、その不変条件が order.py に無く、telegram の `/modify buy 2 ...`
        # (HELP の用例そのもの)から素通りで到達できた。新規側には
        # `FIXED_QTY_REQUIRED` と分割必須があるのに、modify 側だけ対応物が無かった。
        #
        # 建玉の性質そのものに対する不変条件なので、claim 検証より前で落とす。
        fixed_qty = int(execution_contract.CONTRACT["risk"]["fixedQty"])
        held_qty = int(verified.get("qty") or 0)
        if a.ultra:
            # R52: ULTRA の脚は 1 枚ではない(例 6/6)ので枚数では「全脚生存中」を
            # 判定できない。保護注文の **組数**で判定する: 建玉と逆方向の active 行が
            # 2 本(= 1 組)なら TP1 脚のブラケットは消えている。4 本なら両脚生存 →
            # 全量張り替えは TP1 を潰すので拒否。呼び出し側は残る脚の枚数を --qty で
            # 宣言し、建玉と一致しなければ拒否する。
            if held_qty != a.qty:
                sys.exit(f"ERROR: MODIFY_POSITION_MISMATCH: 建玉 {held_qty} 枚 ≠ 指定 {a.qty} 枚")
            guard_view = broker_status.query_orders(a.symbol, account=account)
            if not isinstance(guard_view, dict) or guard_view.get("verified") is not True:
                sys.exit("ERROR: modify blocked — protective orders are UNVERIFIED")
            opposite = "SELL" if a.side == "buy" else "BUY"
            live_protective = [row for row in (guard_view.get("activeOrders") or [])
                               if isinstance(row, dict)
                               and str(row.get("action") or "").upper() == opposite]
            naked_pairs, naked_singles = ([], [])
            if a.repair_naked:
                naked_pairs, naked_singles = broker_status.oco_pairs(
                    guard_view.get("orders") or [], account=account, expected_action=opposite, symbol=a.symbol)
            if a.repair_naked and naked_pairs:
                sys.exit(f"ERROR: REPAIR_NOT_NAKED: 保護注文の OCO 組が {len(naked_pairs)} 組あります。"
                         "--repair-naked は組が 0 のときだけ使えます")
            if a.pyramid_consolidate:
                # R84 §5.3: 合成建玉(複数トランシェ)の統合。**全 TP1 脚が解決済み**で
                # あることは engine が構造(脚の三状態)で確定させており、ここではその
                # 帰結だけを確かめる —— 残っているのは runner の OCO 組だけなので
                # 保護注文は必ず「2 本 × 組数」になる。半端な本数は組になっていない
                # (片脚拒否や TP1 が生きている)ので拒否する。
                if len(live_protective) >= 2 and len(live_protective) % 2 != 0:
                    sys.exit(
                        "ERROR: PYRAMID_CONSOLIDATE_UNPAIRED: "
                        f"保護注文が {len(live_protective)} 本で OCO の組になっていません。"
                        "張り替えると守られない枚数が残ります。")
                if len(live_protective) < 2:
                    print(f"※ 保護注文が {len(live_protective)} 本しかありません(片脚拒否/裸)。"
                          "runner の保護を張り直す修復として続行します(R78)")
                else:
                    print(f"※ R84 統合: 保護注文 {len(live_protective)} 本"
                          f"({len(live_protective) // 2} 組)を 1 組へ畳みます")
            elif a.repair_naked:
                print(f"※ R102 裸修復(ULTRA): OCO 組 0(孤立行 {len(naked_singles)} 本)。"
                      f"全量 {held_qty} 枚に SL/TP を 1 組で張ります")
            elif len(live_protective) > 2:
                sys.exit(
                    "ERROR: MODIFY_SPLIT_PLAN_PROTECTED: "
                    f"ULTRA 建玉 {held_qty} 枚に対し保護注文が {len(live_protective)} 本あります。"
                    "1 組(2 本)= runner だけが残った状態でのみ張り替えます。")
            elif len(live_protective) < 2:
                # R78: 片脚拒否/裸(0〜1 本)は「守る」方向の張り替えなので拒否しない。
                # 以前は `!= 2` で修復 modify 自体を止めていた(2026-09-11 01:41 の HALT 後、
                # 手動 SL で 2 本に戻していなければ engine の修復も通らなかった)。
                print(f"※ 保護注文が {len(live_protective)} 本しかありません(片脚拒否/裸)。"
                      "runner の保護を張り直す修復として続行します(R78)")
        elif held_qty >= fixed_qty and a.repair_naked:
            # R102: 裸(OCO 組 0)の全量建玉に SL/TP を張る修復。組が 1 つでもあれば従来どおり拒否。
            guard_view = broker_status.query_orders(a.symbol, account=account)
            if not isinstance(guard_view, dict) or guard_view.get("verified") is not True:
                sys.exit("ERROR: modify blocked — protective orders are UNVERIFIED")
            opposite = "SELL" if a.side == "buy" else "BUY"
            naked_pairs, naked_singles = broker_status.oco_pairs(
                guard_view.get("orders") or [], account=account, expected_action=opposite, symbol=a.symbol)
            if naked_pairs:
                sys.exit(f"ERROR: REPAIR_NOT_NAKED: 保護注文の OCO 組が {len(naked_pairs)} 組あります。"
                         "--repair-naked は組が 0 のときだけ使えます")
            print(f"※ R102 裸修復: OCO 組 0(孤立行 {len(naked_singles)} 本)。"
                  f"全量 {held_qty} 枚に SL/TP を 1 組で張ります(TP1 と runner は 1 本に畳まれる)")
        elif held_qty >= fixed_qty:
            sys.exit(
                "ERROR: MODIFY_SPLIT_PLAN_PROTECTED: "
                f"建玉 {verified.get('qty')} 枚は分割の全脚が生存中です。"
                "全量のブラケット張り替えは TP1 と runner 最終 TP を 1 本に潰すため拒否します。"
                "TP1 約定後(qty=1)に modify するか、撤退なら --flatten を使ってください。")
        before_identity = broker_status.position_identity(verified)
        if not before_identity:
            sys.exit("ERROR: MODIFY_OWNERSHIP_UNKNOWN: broker position has no strong identity")
        if a.position_generation != before_identity:
            sys.exit("ERROR: MODIFY_POSITION_GENERATION_MISMATCH")
        expected_side = "LONG" if a.side == "buy" else "SHORT"
        if (str(verified.get("symbol") or "") != a.symbol
                or str(verified.get("side") or "").upper() != expected_side
                or int(verified.get("qty") or 0) != a.qty):
            sys.exit("ERROR: MODIFY_POSITION_MISMATCH: symbol/side/qty changed")
        position_account = verified.get("accountId") or verified.get("account")
        if position_account in (None, "") or str(position_account) not in {str(x) for x in accounts}:
            sys.exit("ERROR: MODIFY_ACCOUNT_SCOPE_MISMATCH")
        if a.tp is None:
            sys.exit("ERROR: MODIFY_POSTVERIFY_REQUIRED: live --no-tp is unsupported")
        actual_management_intent = management_intent.build(
            account_id=position_account, symbol=a.symbol,
            position_generation=before_identity, side=a.side, qty=a.qty,
            stop=a.sl, target=a.tp)
        actual_management_hash = management_intent.intent_hash(actual_management_intent)
        if (management_intent.management_key(actual_management_intent) != a.management_key
                or actual_management_hash != a.management_intent_hash):
            sys.exit("ERROR: MANAGEMENT_CLAIM_INTENT_MISMATCH")
        import nqx_state
        consumed, consume_detail = nqx_state.consume_management_claim(
            a.management_key, a.management_token,
            actual_management_intent, actual_management_hash)
        if not consumed:
            sys.exit(f"ERROR: MANAGEMENT_CLAIM_INVALID: {consume_detail}")
        modify_window = {"before": _orders_snapshot(a.symbol, account=account)}
        modify_window["origin"] = modify_window["before"]
        modify_brackets = {}

        def modify_identity_probe(account, leg=None):
            before = modify_window.get("before")
            # R52: cancelandbracket 直後は新しい行がまだ見えない。長めに待って取り直す。
            after = _orders_snapshot_settled(a.symbol, before, account=account,
                                             attempts=MODIFY_SETTLE_ATTEMPTS)
            if after is not None:
                modify_window["before"] = after
            if before is None or after is None:
                return None
            bracket = route_identity.bind_replacement_bracket(
                before, after, account=account, symbol=a.symbol,
                action="SELL" if str(a.side).upper() == "BUY" else "BUY",
                qty=a.qty, stop=a.sl, target=a.tp)
            if not bracket:
                return None
            modify_brackets[str(account)] = bracket
            return {"orderId": bracket["stopOrderId"], "receipt": bracket["stopReceipt"]}

        ok, route_results = post_to_accounts(cfg, accounts, lines_for_account, "modify",
                                             identity_probe=modify_identity_probe)
        if not ok:
            # R52: 応答は 200 だが probe が新しい対を取りこぼした場合の救済。送信前の
            # スナップショットに対してもう一度だけ束縛を試す(2026-09-05 02:49 実測:
            # 張り替えは成功していたのに MODIFY_ROUTE_UNKNOWN で HALT した)。
            http_ok = all(int(status or 0) in (200, 201, 202) for _account, status, _body in route_results)
            late = _late_bind_replacement(
                a.symbol, account, modify_window.get("origin"),
                action="SELL" if str(a.side).upper() == "BUY" else "BUY",
                qty=a.qty, stop=a.sl, target=a.tp) if http_ok else None
            if late:
                modify_brackets[str(account)] = late
                print("replacement pair bound late from broker order truth")
                ok = True
        if not ok:
            nqx_state.resolve_management_claim(
                a.management_key, a.management_token, "UNKNOWN",
                receipt={"route": "not fully accepted"})
            sys.exit("ERROR: MODIFY_ROUTE_UNKNOWN: bracket replacement was not fully accepted")
        route_receipt, receipt_detail = broker_status.extract_replacement_receipt(route_results)
        if not route_receipt:
            # CrossTrade answers cancelandbracket with a bare success body, so
            # the replacement identity is read back from the orders the request
            # actually created.  Requiring the pair to be *new* is stricter than
            # the response-body route: an unchanged bracket cannot pass.
            acquired = [modify_brackets.get(str(account)) for account in accounts]
            if all(acquired):
                route_receipt = {"accounts": acquired}
                receipt_detail = "replacement identity acquired from broker order truth"
        if not route_receipt:
            nqx_state.resolve_management_claim(
                a.management_key, a.management_token, "UNKNOWN",
                receipt={"error": receipt_detail})
            sys.exit(f"ERROR: MODIFY_ROUTE_UNKNOWN: {receipt_detail}")
        middle = broker_status.query_position(a.symbol, account=account)
        middle_identity = broker_status.position_identity(middle)
        if (not isinstance(middle, dict) or middle.get("verified") is not True
                or middle_identity != before_identity
                or int(middle.get("qty") or 0) != a.qty
                or str(middle.get("side") or "").upper() != expected_side):
            nqx_state.resolve_management_claim(
                a.management_key, a.management_token, "UNKNOWN", receipt=route_receipt)
            sys.exit("ERROR: MODIFY_POSTVERIFY_UNKNOWN: position identity/qty/side changed")
        replacement_ids = [value for row in route_receipt.get("accounts") or []
                           for value in (row.get("stopOrderId"), row.get("targetOrderId"))
                           if value not in (None, "")]
        # R52: 古い保護注文が消え、新しい 1 組(逆方向 2 行)だけになるまで短く待つ。
        protective = _protective_settled(a.symbol, account, replacement_ids,
                                         "SELL" if a.side == "buy" else "BUY")
        after = broker_status.query_position(a.symbol, account=account)
        protective_ok, protective_detail = broker_status.verify_protective_orders(
            protective, accounts, a.qty, a.sl, a.tp, side=a.side,
            position_generation=before_identity, position_before=middle,
            current_position=after,
            route_receipt=route_receipt)
        if not protective_ok:
            nqx_state.resolve_management_claim(
                a.management_key, a.management_token, "UNKNOWN", receipt=route_receipt)
            sys.exit(f"ERROR: MODIFY_POSTVERIFY_UNKNOWN: {protective_detail}")
        resolved, resolve_detail = nqx_state.resolve_management_claim(
            a.management_key, a.management_token, "SENT", receipt=route_receipt)
        if not resolved:
            sys.exit(f"ERROR: MODIFY_RESOLVE_UNKNOWN: {resolve_detail}")
        print(f"MODIFY VERIFIED: {protective_detail}")
        # R84: 張り替えで **新しくなった** 保護注文の身元を機械可読で 1 行出す。
        # engine は合成プランの凍結 bracket id をこれで差し替える —— 出さないと
        # 次の周期は消えた古い id を探して「脚が閉じた」と読み、構造導出枚数が
        # 崩れて所有権ごと落ちる。行の語彙は route_envelope の ERROR/FAILED 検出に
        # 掛からないものだけを使う。
        print("NQX_MODIFY_BRACKET " + json.dumps({
            "account": str(accounts[0]),
            "symbol": str(a.symbol),
            "action": "SELL" if str(a.side).lower() == "buy" else "BUY",
            "qty": int(a.qty), "sl": float(a.sl),
            "tp": float(a.tp) if a.tp is not None else None,
            "consolidated": bool(a.pyramid_consolidate),
            "accounts": route_receipt.get("accounts") or [],
        }, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
        sys.exit(0)

    if a.flatten:
        def lines_for_account(account):
            return [f"key={cfg['CROSSTRADE_KEY']};",
                    f"destination={cfg['CROSSTRADE_DEST']};",
                    "command=flatteneverything;",
                    f"account={account};"]

        print("--- FLATTEN ---")
        print("\n".join(lines_for_account(accounts[0])).replace(
            cfg["CROSSTRADE_KEY"], "***KEY***"))
        for account in accounts[1:]:
            print(f"--- FLATTEN account={account} ---")
            print("\n".join(lines_for_account(account)).replace(
                cfg["CROSSTRADE_KEY"], "***KEY***"))
        if a.confirm:
            # R52: flatteneverything は注文行を作らないので経路 identity が無く、
            # HTTP 200 {"success":true} でも分類は NOT_ALL_ACCEPTED になる
            # (09-01 の KILL 3 件は全部この理由で HALT した)。撤退の真実は応答の
            # 分類ではなく **送信後のブローカー再照会**(FLAT + 注文が非 blocking)。
            # 分類は監査用に印字だけし、成否は再照会で決める。行が消えるまでの
            # 遅延は identity と同じく短く待つ。
            ok, _ = post_to_accounts(cfg, accounts, lines_for_account, "flatten")
            if not ok:
                print("flatten route classification is not ACCEPTED; verifying broker truth instead")
            _, failures = _flatten_verified(accounts, a.symbol)
            if failures:
                sys.exit("ERROR: FLATTEN_UNVERIFIED: command sent, but broker flat/order terminal "
                         f"post-verification failed for {','.join(failures)}. Do not resend blindly.")
            print("FLATTEN VERIFIED: all targeted broker accounts FLAT and orders nonblocking")
            sys.exit(0)
        print("\n[ドライラン] 送信するには --confirm を付けてください")
        return

    # ---- バリデーション ----
    # BLOCK_NEW_ACCOUNTS はここ(新規発注パス)でのみ適用する。status/cancel/
    # modify/flatten は上で既に return/exit 済みなので影響を受けない。
    accounts = new_order_accounts(cfg, accounts)
    if not a.side:
        sys.exit("ERROR: --side buy|sell が必要です")
    # qty=0 はリスク $0 で全口座の cap を通過し、PLACE が送信された上に
    # 日次回数だけ消費する。下限も上限と同じ場所で止める。
    # ULTRA は明示宣言されたときだけ別エンベロープの上限を使う。
    qty_ceiling = (int(execution_contract.CONTRACT["ultra"]["maxQtyPerAccount"])
                   if a.ultra else MAX_QTY)
    if a.qty < 1 or a.qty > qty_ceiling:
        sys.exit(f"ERROR: 新規 qty は 1..{qty_ceiling} の範囲です(指定 {a.qty})")
    if MAX_ORDERS_PER_DAY > 0 and count_today(log) >= MAX_ORDERS_PER_DAY:
        sys.exit(f"ERROR: 本日の発注上限 {MAX_ORDERS_PER_DAY} 回に到達しています。"
                 f"\n本日は発注できません。")

    # ---- 日次サマリ(旧 R7 日次ガード)----
    # 2026-08-27 に**停止条件としては廃止**。日次損失 −$480 / DAYGOAL 到達 /
    # 2連敗 / 損切り後15分冷却は、もう新規発注を止めない。台帳が読めなくても
    # 止めない —— 記録の不備で発注を止めるのは、ガードを外した以上筋が通らない。
    # 数字だけは発注前に必ず目に入る場所へ出す(判断は人がする)。
    guard, guard_error = dayguard_state(cfg)
    if guard_error:
        print(f"⚠ 日次集計を出せません: {guard_error}(発注は止めません)")
    elif guard:
        # 表示専用なので、欠けた項目があっても**絶対に落とさない**。
        # ここで KeyError を出すと、集計の不備が発注そのものを殺す —— それは
        # ガードを廃止した意図の正反対になる。
        for warning in guard.get("warnings") or []:
            print(f"⚠ 台帳: {warning}")
        try:
            print(f"日次 (取引日 {guard.get('tradeDay', '—')}): "
                  f"${float(guard.get('dayPnl') or 0.0):.2f} / "
                  f"連敗 {guard.get('consecLosses', 0)} / 決済 {guard.get('closes', 0)}件")
            for note in guard.get("notes") or []:
                print(f"   · {note.get('code')}: {note.get('detail')}"
                      "(ガードは廃止済み・停止しません)")
        except (TypeError, ValueError):
            pass
    if not a.market and a.entry is None:
        sys.exit("ERROR: --entry を指定するか --market を付けてください")
    if a.sl is None:
        sys.exit("ERROR: --sl(損切り価格)は必須です")
    split_targets = None
    if a.split_tp:
        if a.tp is not None:
            sys.exit("ERROR: --split-tp と --tp は併用できません")
        try:
            split_targets = [float(value.strip()) for value in a.split_tp.split(",") if value.strip()]
        except ValueError:
            sys.exit("ERROR: --split-tp は数値をカンマ区切りで指定してください")
        # R40: ULTRA はここを免除する。下流の 928 行には既に
        # 「ULTRA なら固定枚数を課さない」分岐があるのに、この early check だけが
        # 取り残されていて **ULTRA の qty>2 が必ずここで exit していた**
        # (ULTRA エンベロープの判定へ到達すらしない)。
        #
        # TP の本数は「枚数」ではなく ULTRA の splitPlan.legCount(=2) が正本。
        # 通常経路は従来どおり「固定2枚 / TP2本」を強制する。
        leg_count = int(execution_contract.CONTRACT["ultra"]["splitPlan"]["legCount"])
        if a.ultra:
            if len(split_targets) != leg_count:
                sys.exit(f"ERROR: ULTRA_SPLIT_TARGET_COUNT: ULTRA は TP を "
                         f"{leg_count} つ指定してください(指定 {len(split_targets)})")
        else:
            if a.qty != int(execution_contract.CONTRACT["risk"]["fixedQty"]):
                sys.exit(f"ERROR: FIXED_QTY_REQUIRED: 新規は固定 2 枚です(指定 {a.qty})")
            if len(split_targets) != a.qty or a.qty != 2:
                sys.exit("ERROR: 分割型は固定2枚に対して2つのTPを指定してください")
        if len(set(split_targets)) != len(split_targets):
            sys.exit("ERROR: 分割型のTP1とrunner最終TPは異なる価格にしてください")


    # ---- R102: 発注先の限月と価格の出所(ドライランでも --confirm でも同じ順で検査) ----
    # 2026-09-15 15:10、チャート(連続足 MNQ1! = 12 月限)の値で作った買い指値 29,363.75 が
    # 9 月限 MNQU6(市場 29,078)へ 285pt 上の通過指値として飛び、SL 29,329.75 は市場より上で
    # 置けず裸になった。ここで止める:
    #   (a) --symbol が正本の限月(contract.py)そのものである
    #   (b) 満期の手前(contract.lastEntryDaysBeforeExpiry 日未満)ではない
    #   (c) --last の出所(--price-symbol)が発注先と同じ限月である
    #   (d) 送信直前の quote(同じ限月のときだけ採用)と --last が乖離していない
    #   (e) 参照価格が取れない新規は送らない(fail-closed)
    #   (f) 指値が参照価格を通過していない / SL が参照価格の守れる側にある
    if not contract_month.same_contract(a.symbol, contract_month.symbol()):
        sys.exit(f"ERROR: ORDER_SYMBOL_NOT_CONTRACT: --symbol {a.symbol} は正本の限月 "
                 f"{contract_month.symbol()} ではありません(python contract.py --status)")
    entry_ok, entry_reason = contract_month.entry_allowed()
    if not entry_ok:
        sys.exit(f"ERROR: {entry_reason}(新規は満期の手前で止める。撤退は --flatten)")
    if a.price_symbol and not contract_month.same_contract(a.price_symbol, a.symbol):
        sys.exit(f"ERROR: LAST_SYMBOL_MISMATCH: --last の出所 {a.price_symbol} は発注先 {a.symbol} "
                 "と別の限月です(チャートが連続足/別限月のまま)")
    quote_ref, quote_src = modify_reference_price(None)
    if a.last is not None and quote_ref is not None \
            and abs(float(a.last) - quote_ref) > last_divergence_pt():
        sys.exit(f"ERROR: LAST_PRICE_DIVERGENT: --last {fmt_price(a.last)} と送信直前の quote "
                 f"{fmt_price(quote_ref)} が {abs(float(a.last) - quote_ref):.2f}pt 離れています"
                 f"(> {last_divergence_pt():g}pt。判断の価格と発注先の価格が別物)")
    if quote_ref is not None:
        market_ref, market_src = quote_ref, quote_src
    elif a.last is not None:
        market_ref, market_src = float(a.last), "--last"
    else:
        sys.exit("ERROR: QUOTE_UNAVAILABLE: 発注先限月の参照価格が取れません(quote 不可・--last なし)。"
                 "新規は参照価格なしで送りません(--last を添えてください)")
    if not a.market:
        if a.side == "buy" and a.entry >= market_ref:
            sys.exit(f"ERROR: ENTRY_LIMIT_THROUGH_MARKET: 買い指値 {fmt_price(a.entry)} が参照価格 "
                     f"{fmt_price(market_ref)}({market_src})以上 — 即約定します。"
                     "今すぐ入るのが意図なら --market --last で発注してください")
        if a.side == "sell" and a.entry <= market_ref:
            sys.exit(f"ERROR: ENTRY_LIMIT_THROUGH_MARKET: 売り指値 {fmt_price(a.entry)} が参照価格 "
                     f"{fmt_price(market_ref)}({market_src})以下 — 即約定します。"
                     "今すぐ入るのが意図なら --market --last で発注してください")
    if a.side == "buy" and a.sl >= market_ref:
        sys.exit(f"ERROR: STOP_WRONG_SIDE_OF_MARKET: 買いの SL {fmt_price(a.sl)} が参照価格 "
                 f"{fmt_price(market_ref)}({market_src})以上 — ブローカーが置けず裸になります")
    if a.side == "sell" and a.sl <= market_ref:
        sys.exit(f"ERROR: STOP_WRONG_SIDE_OF_MARKET: 売りの SL {fmt_price(a.sl)} が参照価格 "
                 f"{fmt_price(market_ref)}({market_src})以下 — ブローカーが置けず裸になります")
    print(f"R102 contract gate: {a.symbol} (expiry {contract_month.expiry().isoformat()}, "
          f"{contract_month.days_to_expiry()}d) reference {fmt_price(market_ref)}({market_src}) OK")

    # ---- リスク判定の基準価格 ----
    # 指値はエントリー価格が基準。成行は約定価格が事前に確定しないため、
    # 観測した現在値(--last)+ 滑り緩衝でリスクを概算する。
    # --last の無い成行はリスクを検証できないので、口座数に関係なく拒否する。
    if a.market:
        if a.last is None:
            sys.exit("ERROR: 成行には --last(発注直前に観測した現在値)が必須です。\n"
                     "  リスクは (|現在値 − SL| + 滑り緩衝) × 枚数 × $2.00 で概算し、\n"
                     "  指値と同じ口座別上限で発注先を判定します。\n"
                     "  例: python order.py --market --side buy --qty 1 --sl 29700 --last 29726.50")
        if a.last <= 0:
            sys.exit(f"ERROR: --last は正の価格にしてください: {a.last}")
        ref = a.last
        slippage = market_slippage_pt(cfg)
        # R90 穴 2: 発注時点の価格から SL までの距離。engine が --min-stop-pt(= 1.0×ノイズ床)を
        # 渡したときだけ検査する。予定建値のノイズ床検査(msnr_gate RISK_BELOW_NOISE)は成行に
        # 切り替わった瞬間に意味を失う(2026-09-14 23:26: 予定 33.5pt → 実約定から 16.9pt)。
        # quote が取れれば送信直前の値で、取れなければ --last で。ドライランも同じ門を通す。
        if a.min_stop_pt is not None:
            if a.min_stop_pt <= 0:
                sys.exit(f"ERROR: --min-stop-pt は正の距離にしてください: {a.min_stop_pt}")
            guard_ref, guard_src = modify_reference_price(a.last)
            guard_dist = (guard_ref - a.sl) if a.side == "buy" else (a.sl - guard_ref)
            if guard_dist < a.min_stop_pt - 1e-9:
                sys.exit("ERROR: MARKET_STOP_TOO_CLOSE: "
                         f"参照価格 {fmt_price(guard_ref)}({guard_src})から SL {fmt_price(a.sl)} まで "
                         f"{guard_dist:.2f}pt < 下限 {a.min_stop_pt:g}pt。成行の SL 距離がノイズ床を"
                         "割るので出しません(見送り。SL を近づけて通さない)。")
            print(f"stop-distance guard: {fmt_price(guard_ref)}({guard_src}) → SL {fmt_price(a.sl)} = "
                  f"{guard_dist:.2f}pt ≥ {a.min_stop_pt:g}pt OK")
    else:
        ref = a.entry
        slippage = 0.0
        # 2026-08-12 の実害(現在値より不利な側の指値は即約定)は、上の R102 の門が
        # 参照価格(quote / --last)に対して ENTRY_LIMIT_THROUGH_MARKET で止める。

    # SL/TP の向きチェック(成行は現在値を基準に判定する)
    basis = "現在値" if a.market else "エントリー"
    if a.side == "buy" and a.sl >= ref:
        sys.exit(f"ERROR: 買いのSL({a.sl})が{basis}({ref})以上です")
    if a.side == "sell" and a.sl <= ref:
        sys.exit(f"ERROR: 売りのSL({a.sl})が{basis}({ref})以下です")
    targets = split_targets if split_targets is not None else ([a.tp] if a.tp is not None else [])
    if split_targets is None:
        sys.exit("ERROR: SPLIT_PLAN_REQUIRED: 新規は異なるTP1/runnerを --split-tp で指定してください")
    for target in targets:
        if a.side == "buy" and target <= ref:
            sys.exit(f"ERROR: 買いのTP({target})が{basis}({ref})以下です")
        if a.side == "sell" and target >= ref:
            sys.exit(f"ERROR: 売りのTP({target})が{basis}({ref})以上です")

    risk = (abs(ref - a.sl) + slippage) * a.qty * 2.00   # MNQ 1pt = $2.00
    # R52: 脚の枚数。通常経路は各脚1枚(固定2枚)。ULTRA は比率分割(例 34枚 → 17/17)。
    # ドライランの注文行・R:R も **送信行と同じ枚数**で組む。2026-09-04 の机上検算で
    # ULTRA 34枚の dry-run が `qty=1;` を2行印字し、リワードも1枚ぶんで出ていた
    # (live 経路だけが ultra_legs を使っていた)。人が最後に見る表示が送信内容と
    # 違うのは、ゲートとしてのドライランの意味を失わせる。
    if a.ultra:
        leg_qty = execution_contract.ultra_split(a.qty)
        if leg_qty is None:
            sys.exit(f"ERROR: ULTRA_SPLIT_NOT_REPRESENTABLE: {a.qty} 枚を2脚へ分割できません")
    else:
        leg_qty = (1, 1)
    rew = (sum(abs(target - ref) * 2.00 * leg_qty[index]
               for index, target in enumerate(targets))
           if targets else None)
    if a.market:
        print(f"リスク概算: ${risk:.2f}(現在値 {ref:.2f} 基準・滑り緩衝 {slippage:g}pt 込み)"
              + (f"  リワード概算: ${rew:.2f}" if rew else ""))
    else:
        print(f"リスク: ${risk:.2f}" + (f"  リワード: ${rew:.2f}  R:R = 1:{rew/risk:.2f}" if rew else ""))

    # 口座ごとに残り枠が違う。共通の1上限では小さい口座を守れないので、
    # 超える口座はここで発注先から外す(全体は止めない)。
    #
    # ULTRA のリスク上限の正本は RISK_* ではなく、その口座の残ドローダウン
    # (--ultra-drawdown)。ULTRA は定義上 RISK_* を超える枚数を出すモード
    # なので、ここで RISK_* を当てると必ず全口座が除外されてしまう。
    if a.ultra:
        allowed = [(account, float(a.ultra_drawdown or 0.0)) for account in accounts]
        skipped = []
    else:
        allowed, skipped = split_accounts_by_risk(cfg, accounts, risk)
    for account, cap in skipped:
        print(f"  除外 {account}: リスク ${risk:.2f} が上限 ${cap:.2f} を超過")
    if not allowed:
        sys.exit("ERROR: リスク上限を満たす発注先がありません。"
                 "\nSL を近づけるのではなく、このトレードを見送ってください。")
    for account, cap in allowed:
        print(f"  発注先 {account}: 上限 ${cap:.2f}")
    accounts = [account for account, _ in allowed]

    # ---- ULTRA エンベロープ(R52: dry-run も同じ門を通す) ----
    # 以前はこの検査が `--confirm` の後ろにあり、ドライランは ULTRA の残DD超過・
    # 枚数上限を一切見ずに「通った」と出していた。engine は dry-run 成功を
    # live 送信の前提にするので、門は両方で同じでなければならない。
    # 通常経路は固定枚数のまま。ULTRA だけが別エンベロープで判定される。
    # モードは明示宣言が要る — 既定で緩む経路を作らない。
    if a.ultra:
        ultra_envelope = execution_contract.CONTRACT["ultra"]
        if not ultra_envelope.get("enabled"):
            sys.exit("ERROR: ULTRA_DISABLED: 実行契約が ULTRA を許可していません")
        if a.ultra_drawdown is None or a.ultra_drawdown <= 0:
            sys.exit("ERROR: ULTRA_DRAWDOWN_REQUIRED: --ultra-drawdown にその口座の残ドローダウン $ が必要です")
        if a.qty < int(ultra_envelope["minQtyPerAccount"]):
            sys.exit(f"ERROR: ULTRA_QTY_BELOW_MIN: 最小 {ultra_envelope['minQtyPerAccount']} 枚です(指定 {a.qty})")
        if a.qty > int(ultra_envelope["maxQtyPerAccount"]):
            sys.exit(f"ERROR: ULTRA_QTY_EXCEEDS_ACCOUNT_MAX: 上限 {ultra_envelope['maxQtyPerAccount']} 枚です(指定 {a.qty})")
        ultra_legs = leg_qty
        # 想定損失は上の `risk` と同じ式(成行は --last + 滑り緩衝)。以前の
        # `abs(a.entry - a.sl)` は成行(entry=None)で TypeError を投げていた。
        ultra_risk = risk
        # R84 §6-2: 追撃では口座上限の判定を **合成リスクへ差し替える**。ここで
        # 追撃脚だけのリスクを当てると、基礎が利益側にある(= 新しい構造 SL が基礎建値を
        # 越えている)普通のケースで落ちる —— そのとき合成建値は SL のすぐ近くにあり、
        # 実際の合計リスクは追撃単体よりずっと小さい。判定は下の --pyramid ブロックが
        # `pyramid.combined_risk` で行う(engine / Worker と同一算術)。枚数のエンベロープは
        # 追撃でもそのまま上で効いている。
        if not a.pyramid:
            if ultra_risk > float(ultra_envelope["maxRiskDollarsPerAccount"]) + 1e-9:
                sys.exit(f"ERROR: ULTRA_ACCOUNT_RISK_EXCEEDS_CONTRACT: "
                         f"${ultra_risk:,.2f} > ${float(ultra_envelope['maxRiskDollarsPerAccount']):,.2f}")
            if ultra_risk > float(a.ultra_drawdown) + 1e-9:
                sys.exit(f"ERROR: ULTRA_DRAWDOWN_EXCEEDED: "
                         f"想定損失 ${ultra_risk:,.2f} が残ドローダウン ${float(a.ultra_drawdown):,.2f} を超えます")
        if len(accounts) != 1:
            sys.exit("ERROR: ULTRA_SINGLE_ACCOUNT_PER_ROUTE: ULTRA は口座ごとに1回ずつ呼ぶ")
        print(f"ULTRA: {a.qty}枚 → TP1 {ultra_legs[0]}枚 / RUNNER {ultra_legs[1]}枚 "
              f"· 想定損失 ${ultra_risk:,.2f} / 残DD ${float(a.ultra_drawdown):,.2f}")
    else:
        ultra_legs = None
        fixed_qty = int(execution_contract.CONTRACT["risk"]["fixedQty"])
        if a.qty != fixed_qty:
            sys.exit(f"ERROR: FIXED_QTY_REQUIRED: 新規は固定 {fixed_qty} 枚です(指定 {a.qty})")

    # ---- R84 追撃(--pyramid)の門 ----
    # ここは ULTRA エンベロープと同じ位置 = **ドライランの return より前**(R52 の教訓)。
    # 差し替えるのは (1) FLAT 要求 → 基礎建玉の一致、(2) リスク上限 → 合成リスク、
    # (3) 枚数上限 → 追撃後の合計。それ以外の既存ゲート(SL/TP の向き・滑り・tick・
    # claim consume)は全部そのまま通す。
    pyramid_base = None
    if a.pyramid:
        pyramid_cfg = execution_contract.CONTRACT.get("pyramid") or {}
        if pyramid_cfg.get("enabled") is not True:
            sys.exit("ERROR: PYRAMID_DISABLED: 実行契約が追撃を許可していません")
        if not a.ultra:
            sys.exit("ERROR: PYRAMID_REQUIRES_ULTRA: 追撃は ULTRA エンベロープで送ります")
        if split_targets is None:
            sys.exit("ERROR: PYRAMID_SPLIT_REQUIRED: 追撃も TP1 / runner の分割ブラケットです")
        if str(pyramid_cfg.get("orderType") or "MARKET_ONLY").upper() == "MARKET_ONLY" and not a.market:
            sys.exit("ERROR: PYRAMID_MARKET_ONLY: 追撃は成行のみです。"
                     "未約定の追撃を取り消す手段が --flatten(建玉ごと消える)しかないため。")
        if a.last is None:
            sys.exit("ERROR: PYRAMID_LAST_REQUIRED: 追撃には観測した現在値 --last が必要です")
        if len(accounts) != 1:
            sys.exit("ERROR: PYRAMID_SINGLE_ACCOUNT_PER_ROUTE: 追撃は建玉のある 1 口座へ送ります")
        pyramid_base = parse_pyramid_base(a.pyramid_base)
        if pyramid_base is None:
            sys.exit("ERROR: PYRAMID_BASE_REQUIRED: --pyramid-base=<qty>@<avgEntry> が必要です")
        base_qty, base_avg_entry = pyramid_base
        envelope = execution_contract.CONTRACT["ultra"]
        floor_qty = max(int(envelope["minQtyPerAccount"]),
                        int(pyramid_cfg.get("minAddQty") or 2))
        if a.qty < floor_qty:
            sys.exit(f"ERROR: PYRAMID_ADD_QTY_BELOW_SPLIT_MIN: 追撃は最小 {floor_qty} 枚です"
                     f"(指定 {a.qty})")
        total_qty = base_qty + a.qty
        if total_qty > int(envelope["maxQtyPerAccount"]):
            sys.exit(f"ERROR: PYRAMID_TOTAL_QTY_EXCEEDS_ACCOUNT_MAX: 追撃後の合計 {total_qty} 枚が"
                     f"口座あたり上限 {envelope['maxQtyPerAccount']} 枚を超えます")
        # 合計リスクは pyramid.py の **同じ関数**で見る。engine / order.py / Worker の
        # 三重検証を同一算術にするため、ここで別の式を書かない。滑りも
        # `pyramid.adverse_add_price`(契約の maxDeviationPoints)で当てる —— 上の単一脚が
        # 使う `MARKET_SLIPPAGE_PT` は `.secrets` にあって Worker が読めないので、三者で
        # 再現できる契約の定数に寄せる。現在値ぴったりで見積もると、成行の約定は常に
        # 不利側へずれるので「概算では枠内・約定したら枠超え」になる。
        point_value = float(execution_contract.CONTRACT["risk"]["pointValue"])
        risk_price = pyramid.adverse_add_price(a.side, a.last)
        if risk_price is None:
            sys.exit("ERROR: PYRAMID_COMBINED_RISK_UNAVAILABLE")
        combined = pyramid.combined_risk(base_qty, base_avg_entry, a.qty, risk_price,
                                         a.sl, point_value)
        if combined is None:
            sys.exit("ERROR: PYRAMID_COMBINED_RISK_UNAVAILABLE")
        combined_entry = pyramid.combined_entry(base_qty, base_avg_entry, a.qty, a.last)
        account_ceiling = float(envelope["maxRiskDollarsPerAccount"])
        if combined > account_ceiling + 1e-9:
            sys.exit(f"ERROR: PYRAMID_ACCOUNT_RISK_EXCEEDS_CONTRACT: "
                     f"合計 ${combined:,.2f} > ${account_ceiling:,.2f}")
        if combined > float(a.ultra_drawdown) + 1e-9:
            sys.exit(f"ERROR: PYRAMID_DRAWDOWN_EXCEEDED: 追撃後の想定損失 ${combined:,.2f} が"
                     f"残ドローダウン ${float(a.ultra_drawdown):,.2f} を超えます")
        print(f"PYRAMID: 基礎 {base_qty}枚 @ {fmt_price(base_avg_entry)} + 追撃 {a.qty}枚 "
              f"@ {fmt_price(a.last)} = {total_qty}枚 @ {fmt_price(combined_entry)} · "
              f"合計想定損失 ${combined:,.2f}"
              f"(滑り {pyramid.slippage_points():g}pt 込み・{fmt_price(risk_price)} 基準)"
              f" / 残DD ${float(a.ultra_drawdown):,.2f}")

    # CrossTrade 仕様: order_type/action は大文字、TP/SL は絶対価格
    # limit エントリー時、ブラケットは約定後に発動(orphan 防止)
    def lines_for_account(account, qty=None, target=None):
        lines = [f"key={cfg['CROSSTRADE_KEY']};",
                 f"destination={cfg['CROSSTRADE_DEST']};",
                 "command=PLACE;",
                 f"account={account};",
                 f"instrument={a.symbol};",
                 f"action={a.side.upper()};",
                 f"qty={a.qty if qty is None else qty};"]
        if a.market:
            lines.append("order_type=MARKET;")
        else:
            lines.append("order_type=LIMIT;")
            lines.append(f"limit_price={fmt_price(a.entry)};")
        lines.append("tif=DAY;")
        lines.append(f"stop_loss={fmt_price(a.sl)};")
        effective_target = a.tp if target is None else target
        if effective_target is not None:
            lines.append(f"take_profit={fmt_price(effective_target)};")
        return lines

    if split_targets is not None:
        print("--- SPLIT ORDER (TP1 + RUNNER) ---")
        for account in accounts:
            for leg, target in enumerate(split_targets, 1):
                print(f"--- ORDER account={account} leg={leg} ---")
                print("\n".join(lines_for_account(account, qty=leg_qty[leg - 1], target=target)).replace(
                    cfg["CROSSTRADE_KEY"], "***KEY***"))
        if a.ultra:
            print(f"※ ULTRA {a.qty}枚は TP1 {leg_qty[0]}枚 / RUNNER {leg_qty[1]}枚 の独立OCOブラケット。"
                  "片脚が不明なら再送せず照会してHALTします")
        else:
            print("※ 2枚は各1枚の独立OCOブラケット。片脚が不明なら再送せず照会してHALTします")
    else:
        print("--- ORDER ---")
        print("\n".join(lines_for_account(accounts[0])).replace(
            cfg["CROSSTRADE_KEY"], "***KEY***"))
        for account in accounts[1:]:
            print(f"--- ORDER account={account} ---")
            print("\n".join(lines_for_account(account)).replace(
                cfg["CROSSTRADE_KEY"], "***KEY***"))

    if not a.confirm:
        print("\n[ドライラン] 送信するには --confirm を付けてください")
        return

    # 実送信直前にも同じ建玉照会を再実行する。Mini App/Bot を経由しない
    # order.py 直接実行でも、照会不能を FLAT として扱わない。
    manual_claim = bool(getattr(a, "manual_claim", False))
    if manual_claim:
        # Durable Object が落ちている間の、人による一回送信。claim は DO が発行する
        # ので取りようがない。担保は (a) DO が本当に落ちていること、(b) 送信直前の
        # 建玉が verified FLAT であることの二点。DO が生きているならここは通さない。
        import urllib.error
        import urllib.request
        import nqx_state as _ns
        try:
            _cfg = _ns.load_cloud_env()
            _tok = _ns.issue_launch_token(_ns._reader_user_id(_cfg), ttl_seconds=120, cfg=_cfg)
            _req = urllib.request.Request(
                _cfg["NQX_API_BASE"].rstrip("/") + "/api/state",
                headers={"X-NQX-Launch": _tok, "User-Agent": "NQX-Nightwatch-State/1.0"},
                method="GET")
            with urllib.request.urlopen(_req, timeout=15) as _resp:
                _resp.read()
            _down = (False, "durable object responded 200")
        except urllib.error.HTTPError as _exc:
            _down = (_exc.code >= 500, f"durable object HTTP {_exc.code}")
        except Exception as _exc:
            _down = (True, f"durable object unreachable: {type(_exc).__name__}")
        if not _down[0]:
            sys.exit(f"ERROR: MANUAL_CLAIM_REFUSED: {_down[1]}; use the claim path")
        print(f"[MANUAL] {_down[1]} — claim を人の一回送信で代替する")
    else:
        if not a.entry_key or not a.claim_token or not a.intent_hash:
            sys.exit("ERROR: ENTRY_CLAIM_REQUIRED: live new order requires --entry-key, --claim-token and --intent-hash")
        if not a.plan_version:
            sys.exit("ERROR: ENTRY_INTENT_REQUIRED: live new order requires --plan-version")
    if a.pyramid:
        require_verified_pyramid_base(a.symbol, accounts[0], a.side,
                                      pyramid_base[0], pyramid_base[1])
    else:
        require_verified_flat(a.symbol, accounts=accounts)
    import nqx_state
    try:
        actual_intent = execution_intent.build(
            symbol=a.symbol, side=a.side, qty=a.qty,
            order_type="MARKET" if a.market else "LIMIT",
            entry=None if a.market else a.entry, last=a.last if a.market else None,
            stop=a.sl, targets=split_targets,
            legs=[{"id": "TP1", "qty": (ultra_legs[0] if ultra_legs else 1),
                   "target": split_targets[0]},
                  {"id": "RUNNER", "qty": (ultra_legs[1] if ultra_legs else 1),
                   "target": split_targets[1]}],
            plan_version=a.plan_version or "MANUAL",
            execution_contract_version=execution_contract.VERSION,
            account_scope=accounts,
            # R84 §7-4: 追撃であることを intent hash に固定する。Worker は同じ値を
            # DO 自身の建玉から再構成して突き合わせる(クライアント申告は信じない)。
            pyramid=({"baseQty": pyramid_base[0], "addQty": a.qty} if a.pyramid else None),
        )
    except (TypeError, ValueError) as exc:
        sys.exit(f"ERROR: ENTRY_INTENT_INVALID: {exc}")
    actual_intent_hash = execution_intent.intent_hash(actual_intent)
    if not manual_claim:
        if actual_intent_hash != a.intent_hash:
            sys.exit("ERROR: ENTRY_CLAIM_INTENT_MISMATCH: CLI/config execution intent differs from claim")
        claim_ok, claim_detail = nqx_state.consume_entry_claim(
            a.entry_key, a.claim_token, actual_intent, actual_intent_hash)
        if not claim_ok:
            sys.exit(f"ERROR: ENTRY_CLAIM_INVALID: {claim_detail}")
    # The webhook body carries no broker identity, so each attempt is bracketed
    # by an order-view snapshot and bound to the single row it created.
    # R52: 窓は **口座ごと**。以前は既定口座 1 つの一覧で全口座の脚を照合していた
    # (2口座ミラーでは 2 口座目の脚が原理的に束縛できない)。
    entry_window = {account: _orders_snapshot(a.symbol, account=account) for account in accounts}
    # R72: 約定履歴の窓。注文一覧の窓が送信直後の一過性の状態で落ちた脚を、
    # 約定(orderId・数量・価格・時刻)から束縛する第 2 経路。送信前に 1 回読む。
    fills_window = {account: _fills_snapshot(account) for account in accounts}
    # R76: 最終パス用に **送信前** の窓を別に残す(脚ごとの probe は窓を進める)。
    entry_origin = dict(entry_window)
    fills_origin = dict(fills_window)
    entry_identities = {}
    entry_order_type = "MARKET" if a.market else "LIMIT"
    entry_reference = None if a.market else a.entry

    def leg_quantity(leg):
        # R40: 分割送信の脚の枚数は「常に1」ではない。ULTRA は比率分割
        # (例 52枚 → TP1 26 / RUNNER 26)なので、1 で照合すると **どの行にも
        # 一致せず identity を束縛できない**。束縛できない経路は routeSnapshot
        # が UNKNOWN になり、ENTRY claim が恒久的に宙吊りになる。
        if split_targets is None:
            return a.qty
        if ultra_legs:
            leg_index = {"TP1": 0, "RUNNER": 1}.get(str(leg or "").upper())
            return ultra_legs[leg_index] if leg_index is not None else None
        return 1

    def entry_identity_probe(account, leg=None):
        probe_qty = leg_quantity(leg)
        if probe_qty is None:
            # 脚が分からないまま推測で照合しない。
            return None
        before = entry_window.get(account)
        # R72/R76: 既に束縛済みの注文 id(親と子)は候補から外す —— 同じ窓に 2 脚分が
        # 入ったときの切り分けを、推測ではなく「束縛済み」という事実で行う。
        bound_ids = _bound_identity_ids(entry_identities)
        # R76: 親行(指値/読み足した成行の親)か、子 2 行(ブラケット)が見えるまで短く
        # 読み続ける。一過性の Unavailable は跨ぐ。
        identity, after = _entry_orders_identity(
            a.symbol, before, account=account, action=a.side.upper(), qty=probe_qty,
            order_type=entry_order_type, entry_price=entry_reference,
            exclude_order_ids=bound_ids)
        if after is not None:
            entry_window[account] = after
        # R72: 注文窓で束縛できなければ約定履歴で束縛する。束縛できた脚でも窓は
        # 進める(次の脚の窓に前の脚の約定を残さない)。
        fills_identity, fills_after = _entry_fills_identity(
            account, fills_window.get(account), symbol=a.symbol, action=a.side.upper(),
            qty=probe_qty, exclude_order_ids=bound_ids, settle=identity is None)
        if fills_after is not None:
            fills_window[account] = fills_after
        return identity if identity is not None else fills_identity

    def entry_identity_settle(results):
        # R76: 脚ごとの窓が取りこぼした脚を、送信前の窓に対してまとめて束縛する。
        # 候補の数が未束縛の脚の数と一致するときだけ、枚数の証拠(約定履歴)か作成順
        # (TP1 → RUNNER の送信順)で割り当てる。合わなければ UNKNOWN のまま。
        keys = [((row[0], row[1]) if split_targets is not None else (row[0], None))
                for row in results]
        for account in accounts:
            missing = [leg for acc, leg in keys if acc == account
                       and (acc, leg) not in entry_identities]
            if not missing:
                continue
            legs = [(leg or "ENTRY", leg_quantity(leg)) for leg in missing]
            if any(qty is None for _leg, qty in legs):
                continue
            bound = {(leg or "ENTRY"): identity for (acc, leg), identity in entry_identities.items()
                     if acc == account}
            fills_after = _fills_snapshot(account)
            parent_qty = (route_identity.fill_qty_by_parent(
                fills_origin.get(account), fills_after, symbol=a.symbol, action=a.side.upper())
                if fills_after is not None else None)
            late = _late_bind_entry_legs(
                a.symbol, account, entry_origin.get(account), action=a.side.upper(),
                order_type=entry_order_type, entry_price=entry_reference, legs=legs,
                bound=bound, parent_qty=parent_qty, market=bool(a.market))
            for leg in missing:
                identity = late.get(leg or "ENTRY")
                if identity:
                    entry_identities[(account, leg)] = identity
                    print(f"identity bound late for account={account} leg={leg or 'ENTRY'} "
                          f"from {identity.get('identitySource')}")

    if split_targets is not None:
        def split_lines_for_account(account):
            # 2枚時代の「各脚1枚」を一般化する。ULTRA は比率分割の枚数を使い、
            # 通常経路は従来どおり各脚1枚のまま。
            leg_ids = ("TP1", "RUNNER")
            leg_qty = ultra_legs if ultra_legs else (1, 1)
            return [(leg_ids[index],
                     lines_for_account(account, qty=leg_qty[index], target=target))
                    for index, target in enumerate(split_targets)]
        ok, results = post_split_to_accounts(
            cfg, accounts, split_lines_for_account, "split-place",
            identity_probe=entry_identity_probe, identities=entry_identities,
            identity_settle=entry_identity_settle)
    else:
        ok, results = post_to_accounts(
            cfg, accounts, lines_for_account, "place",
            identity_probe=entry_identity_probe, identities=entry_identities,
            identity_settle=entry_identity_settle)
    route = classify_route_results(results, split=split_targets is not None,
                                   identities=entry_identities)
    route_snapshot = build_route_snapshot(results, split=split_targets is not None,
                                          identities=entry_identities)
    resolved, resolve_detail = nqx_state.resolve_entry_claim(
        a.entry_key, a.claim_token, route["state"], route["acceptedCount"],
        route["totalAttempts"], route["explicitRejectCount"],
        route_snapshot=route_snapshot)
    if not resolved:
        print(f"ENTRY CLAIM RESOLUTION UNKNOWN: {resolve_detail}")
        sys.exit(1)
    snapshot_line = "NQX_ROUTE_SNAPSHOT " + json.dumps(
        route_snapshot, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    final_line = ("NQX_ROUTE_FINAL STATE={state} ACCEPTED={acceptedCount} "
                  "REJECTED={explicitRejectCount} UNKNOWN={unknownCount} "
                  "TOTAL={totalAttempts} RESOLVED=1").format(**route)
    record = {
        "time": datetime.now().strftime("%H:%M:%S"),
        "side": a.side, "qty": a.qty,
        "entry": a.entry, "sl": a.sl, "tp": a.tp,
        "accounts": accounts,
    }
    if split_targets is not None:
        record["splitTp"] = split_targets
    if a.market:
        # 成行のリスク概算に使った現在値。除外判定の根拠を後から追える。
        record["last"] = a.last
    if ok and route["state"] == "SENT":
        record["route"] = "ALL_CONFIRMED"
        log.setdefault(today, []).append(record)
        save_log(log)
        print(f"\n記録しました。本日 {count_label(log)} 回")
    else:
        print("\n送信は成功していません。ブローカー側の建玉を必ず確認してください。")
        if split_targets is not None:
            record["succeededLegs"] = [f"{account}:{leg}" for account, leg, status, _ in results
                                       if route_attempt_state(status, _, entry_identities.get((account, leg))) == "ACCEPTED"]
        else:
            record["succeededAccounts"] = [account for account, status, _ in results
                                           if route_attempt_state(status, _, entry_identities.get((account, None))) == "ACCEPTED"]
        record["route"] = route["state"]
        log.setdefault(today, []).append(record)
        save_log(log)
        # This line precedes the machine envelope on stdout, so its wording
        # must stay outside route_envelope's ERROR/FAILED marker set.
        print("\nROUTE NOT_ALL_ACCEPTED: verify every account before retrying.")
    # Machine receipt is emitted exactly once, only after durable RESOLVE, and
    # is deliberately the final non-empty output line.
    print(snapshot_line)
    print(final_line)
    sys.exit(0 if ok and route["state"] == "SENT" else 1)

if __name__ == "__main__":
    main()

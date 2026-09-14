#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""日次集計 — かつての「日次ガード」。**2026-08-27 に停止条件としては廃止**。

日次損失 −$480 / DAYGOAL 到達 / 2連敗 / 損切り後15分冷却の4条件は、
2026-08-18 から ``order.py`` の発注ゲートと ``monitor_publish.py`` の武装降格
として機械が強制していた。運用者の指示により**この4条件で新規を止めるのを
やめた**。台帳と集計はそのまま残す —— 数字は表示・監査に要るし、いつでも
ゲートを戻せる状態にしておくため。

現在の役割:
  - 決済記録を ``.secrets/day_ledger.jsonl`` に追記する(戦績の元データ)
  - 当日の実現損益・連敗数を集計して返す(``check()``)
  - **``blocked`` は常に False。** 発注を止める判断はもうしない

**決済を止めることは元から絶対にしない。** ``--flatten`` / ``--modify`` /
``--cancel`` は order.py 側でガードの外に置いてある。撤退経路を塞ぐガードは
守るどころか危険を増やす。

台帳: ``.secrets/day_ledger.jsonl``(追記専用・1行1決済)

    {"closedAt": "2026-08-18T00:45:00+09:00", "side": "BUY",
     "pnl": {"LFE...0002": -38.5, "LFE...0003": -37.0},
     "source": "publish_result"}

口座別が分からない記録は ``"pnlEach": -38.5``(全口座同額とみなす)。

    python dayguard.py --status
    python dayguard.py --initialize
    python dayguard.py --record --pnl-each -38.5 --closed-at 2026-08-18T00:45+09:00
    python dayguard.py --record --pnl 0002=-38.5,0003=-37 --closed-at ...

終了コード: 0(判定の可否によらず) / 2(入力の構造異常のみ)。
**残機(LIFELINE)は管理しない** — 正本を2つにしないため。
"""
import json
import math
import os
import sys
from datetime import datetime, timedelta, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
LEDGER = os.path.join(BASE, ".secrets", "day_ledger.jsonl")
JST = timezone(timedelta(hours=9))

# 取引日の境界。CME メンテナンス(17:00 CT)= JST 07:00。order.py の
# trade_date() と同じ区切りで、00:45 の決済は「前日の取引日」に属する。
DAY_BOUNDARY_H = 7
DAY_LOSS_LIMIT = -480.0
COOLDOWN_MIN = 15.0
CONSEC_LOSS_LIMIT = 2


def _env_float(env, name, default):
    try:
        value = float((env or os.environ).get(name, default))
        return value if math.isfinite(value) else float(default)
    except (TypeError, ValueError):
        return float(default)


def _env_int(env, name, default):
    try:
        return int(float((env or os.environ).get(name, default)))
    except (TypeError, ValueError):
        return int(default)


def _num(value):
    """有限な数値だけ返す。bool は数値として扱わない。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def parse_instant(text):
    """タイムゾーン付き ISO を datetime に。読めなければ None(捏造しない)。"""
    if not isinstance(text, str) or not text.strip():
        return None
    raw = text.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=JST)


def trade_day(moment, boundary_h=DAY_BOUNDARY_H):
    """JST 07:00 区切りの取引日を 'YYYY-MM-DD' で返す。"""
    local = moment.astimezone(JST)
    if local.hour < boundary_h:
        local -= timedelta(days=1)
    return local.strftime("%Y-%m-%d")


# --- 台帳 -----------------------------------------------------------------

def ledger_marker(path=LEDGER):
    return path + ".initialized"


def _create_marker(path, label="NQX_DAY_LEDGER_INITIALIZED_V1"):
    marker = ledger_marker(path)
    try:
        fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return marker
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(label + "\n")
    return marker


def initialize_ledger(path=LEDGER):
    """Explicitly establish an empty ledger without masking later data loss."""
    marker = ledger_marker(path)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    if os.path.exists(marker) and not os.path.exists(path):
        raise OSError("初期化済み台帳が消失しています。復元または監査が必要です")
    if not os.path.exists(path):
        with open(path, "x", encoding="utf-8", newline="\n"):
            pass
    _create_marker(path)
    return marker

def load_ledger(path=LEDGER):
    """Return rows and integrity warnings; callers fail closed for new ENTRY."""
    rows, warnings = [], []
    if not os.path.exists(path):
        if os.path.exists(ledger_marker(path)):
            return rows, ["初期化済み台帳が消失しています"]
        return rows, ["台帳が未初期化です(dayguard.py --initialize が必要)"]
    if not os.path.exists(ledger_marker(path)):
        try:
            _create_marker(path, "NQX_DAY_LEDGER_LEGACY_OBSERVED_V1")
        except OSError as exc:
            warnings.append(f"台帳初期化markerを書けません({exc})")
    try:
        with open(path, encoding="utf-8-sig") as fh:
            lines = fh.readlines()
    except OSError as exc:
        return rows, [f"台帳を読めません({exc})"]

    for number, line in enumerate(lines, 1):
        text = line.strip()
        if not text:
            continue
        try:
            row = json.loads(text)
        except json.JSONDecodeError:
            warnings.append(f"{number}行目: JSON として読めない行をスキップ")
            continue
        if not isinstance(row, dict):
            warnings.append(f"{number}行目: オブジェクトでない行をスキップ")
            continue
        closed = parse_instant(row.get("closedAt"))
        if closed is None:
            warnings.append(f"{number}行目: closedAt が読めない行をスキップ")
            continue
        pnl = {}
        raw_pnl = row.get("pnl")
        if isinstance(raw_pnl, dict):
            for account, value in raw_pnl.items():
                amount = _num(value)
                if amount is not None:
                    pnl[str(account)] = amount
        each = _num(row.get("pnlEach"))
        if not pnl and each is None:
            warnings.append(f"{number}行目: 損益が読めない行をスキップ")
            continue
        rows.append({"closedAt": closed, "pnl": pnl, "pnlEach": each,
                     "side": row.get("side"), "source": row.get("source"),
                     "resultId": str(row["resultId"]) if row.get("resultId") else None})
    # R54: 訂正 publish は台帳へ追記される(行は書き換えない)。同じ resultId が
    # 複数あれば **ファイル順で最後の 1 行**だけを数える —— 2026-09-07 に手数料を
    # 直して 3 件を publish し直したとき、日次集計が $1,498.50 → $3,024.00 と
    # 二重計上された。resultId を持たない行(手入力)は従来どおり全部数える。
    latest = {}
    for index, row in enumerate(rows):
        if row["resultId"]:
            latest[row["resultId"]] = index
    superseded = {index for row_id, keep in latest.items()
                  for index, row in enumerate(rows)
                  if row["resultId"] == row_id and index != keep}
    rows = [row for index, row in enumerate(rows) if index not in superseded]
    rows.sort(key=lambda r: r["closedAt"])
    return rows, warnings


def append_row(row, path=LEDGER):
    """1行追記する。ディレクトリが無ければ作る。"""
    marker = ledger_marker(path)
    if os.path.exists(marker) and not os.path.exists(path):
        raise OSError("初期化済み台帳が消失しています。追記で再作成しません")
    initialize_ledger(path)
    with open(path, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def _row_total(row, accounts):
    """1決済の合計損益。pnl があればその和、無ければ pnlEach × 口座数。"""
    if row["pnl"]:
        return sum(row["pnl"].values())
    return (row["pnlEach"] or 0.0) * max(len(accounts), 1)


def _row_by_account(row, accounts):
    """口座別に割り当てた損益。pnlEach は全口座同額とみなす。"""
    if row["pnl"]:
        return dict(row["pnl"])
    return {account: (row["pnlEach"] or 0.0) for account in accounts}


def configured_accounts(env):
    """DAYGOAL_* / RISK_* に現れる口座 ID を集める(順序は安定させる)。"""
    found = []
    for key in sorted((env or {}).keys()):
        for prefix in ("DAYGOAL_", "RISK_"):
            if key.startswith(prefix):
                account = key[len(prefix):]
                if account and account not in found:
                    found.append(account)
    return found


# --- 判定 -----------------------------------------------------------------

def check(now=None, env=None, path=LEDGER):
    """当日の集計を返す。**新規発注は止めない**(2026-08-27 にガードを廃止)。

    日次損失 −$480 / DAYGOAL 到達 / 2連敗 / 損切り後15分冷却の4条件は、
    運用者の指示で **判定材料としては残し、停止条件からは外した**。
    ``blocked`` は常に False、``reasons`` は常に空で返る。数字(``dayPnl`` /
    ``perAccount`` / ``consecLosses``)は表示・監査用にそのまま計算する。

    なぜ削除ではなく「常に通す」なのか: ``dayguard`` の観測は Worker 側の
    ``ENTRY_CLAIM_DAYGUARD_INVALID`` が **フィールドの存在を要求している**。
    bundle から落とすと、既にデプロイ済みの Worker が全 ENTRY claim を
    拒否する。観測の形は保ったまま、判定だけを無効化する。

    ``retired`` が True であることが、この関数がもうゲートではない証拠。
    """
    env = env if env is not None else os.environ
    now = now or datetime.now(JST)
    rows, warnings = load_ledger(path)

    boundary = _env_int(env, "NQX_DAY_BOUNDARY_H", DAY_BOUNDARY_H)
    loss_limit = _env_float(env, "NQX_DAY_LOSS_LIMIT", DAY_LOSS_LIMIT)
    cooldown = _env_float(env, "NQX_COOLDOWN_MIN", COOLDOWN_MIN)
    consec_limit = _env_int(env, "NQX_CONSEC_LOSS_LIMIT", CONSEC_LOSS_LIMIT)
    accounts = configured_accounts(env)

    today = trade_day(now, boundary)
    todays = [r for r in rows if trade_day(r["closedAt"], boundary) == today]

    per_account = {}
    day_pnl = 0.0
    for row in todays:
        day_pnl += _row_total(row, accounts)
        for account, amount in _row_by_account(row, accounts).items():
            per_account[account] = per_account.get(account, 0.0) + amount

    # 末尾から連続する負け(1決済=1行を単位にする)
    consec = 0
    for row in reversed(todays):
        if _row_total(row, accounts) < 0:
            consec += 1
        else:
            break

    losses = [r for r in todays if _row_total(r, accounts) < 0]
    last_loss = losses[-1]["closedAt"] if losses else None

    # 廃止済みのガードが「何に触れていたか」は観測として残す。停止はしない。
    # 台帳が壊れていても新規は止めない —— 記録の不備で発注を止めるのは、
    # ガードを廃止した以上もう筋が通らない(warnings は下でそのまま返す)。
    notes = []
    if day_pnl <= loss_limit:
        notes.append({"code": "DAY_LOSS_LIMIT",
                      "detail": f"日次損益 ${day_pnl:.2f} が旧上限 ${loss_limit:.2f} に到達"})
    for account, total in sorted(per_account.items()):
        goal = _num((env or {}).get(f"DAYGOAL_{account}"))
        if goal is not None and goal > 0 and total >= goal:
            notes.append({"code": "DAYGOAL_REACHED",
                          "detail": f"{account} が日次目標 ${goal:.2f} に到達(${total:.2f})"})
    if consec >= consec_limit:
        notes.append({"code": "TWO_LOSSES",
                      "detail": f"{consec}連敗(旧上限 {consec_limit})"})
    if last_loss is not None:
        elapsed = (now - last_loss).total_seconds() / 60.0
        if 0 <= elapsed < cooldown:
            notes.append({"code": "COOLDOWN",
                          "detail": f"直近の損切りから {elapsed:.1f}分"
                                    f"(旧冷却 {cooldown:.0f}分)"})

    return {
        "tradeDay": today,
        # ガードは廃止。ここが True になる経路はもう無い。
        "blocked": False,
        "retired": True,
        "reasons": [],
        # 旧ガードなら止めていた条件。表示・監査のためだけに残す。
        "notes": notes,
        "dayPnl": round(day_pnl, 2),
        "perAccount": {a: round(v, 2) for a, v in sorted(per_account.items())},
        "consecLosses": consec,
        "closes": len(todays),
        "lastLossAt": last_loss.isoformat() if last_loss else None,
        "warnings": warnings,
    }


def realized_usd(result):
    """result(nqx_state.build_result の形)から1口座分の実現損益を出す。

    result は価格しか持たないので here で計算する:
    LONG は ``(exit − entry)``、SHORT は ``(entry − exit)`` に qty と
    pointValue を掛ける。1項目でも欠けたら None(0 で埋めない)。

    ``fees`` があれば差し引く。実約定価格を取得できない以上、価格から出る
    のは粗損益でしかなく、日次ガードは**実際に口座から引かれた額**で判定
    しなければ甘くなる(TRADING_CONTEXT §2「発注水準から損益を計算しない」)。
    """
    if not isinstance(result, dict):
        return None
    entry = _num(result.get("entry"))
    exit_price = _num(result.get("exit"))
    qty = _num(result.get("qty"))
    point = _num(result.get("pointValue"))
    side = str(result.get("side") or "").upper()
    if None in (entry, exit_price, qty, point) or qty <= 0 or point <= 0:
        return None
    if side in ("LONG", "BUY"):
        move = exit_price - entry
    elif side in ("SHORT", "SELL"):
        move = entry - exit_price
    else:
        return None
    fees = _num(result.get("fees")) or 0.0
    return move * qty * point - abs(fees)


def record_from_result(result, env=None, path=LEDGER):
    """publish 成功後の result を台帳に写す。**publish 前に呼ばない。**

    書けなかった場合は False を返すだけで例外を投げない(記録の失敗で
    決済経路そのものを壊さない)。
    """
    if not isinstance(result, dict):
        return False
    closed = parse_instant(result.get("closedAt"))
    amount = realized_usd(result)
    if closed is None or amount is None:
        return False
    row = {"closedAt": closed.isoformat(), "pnlEach": round(amount, 2),
           "side": result.get("side"), "source": "publish_result"}
    if result.get("resultId"):
        row["resultId"] = str(result["resultId"])
    try:
        append_row(row, path)
    except OSError:
        return False
    return True


def format_status(state):
    lines = [f"取引日 {state['tradeDay']}(JST 07:00 区切り)",
             f"決済 {state['closes']}件 / 日次 ${state['dayPnl']:.2f} / "
             f"連敗 {state['consecLosses']}"]
    for account, total in state["perAccount"].items():
        lines.append(f"  {account}: ${total:.2f}")
    if state["blocked"]:
        lines.append("判定: ⛔ 新規発注を止めます")
        lines.extend(f"  - {r['code']}: {r['detail']}" for r in state["reasons"])
    else:
        lines.append("判定: ✅ 新規発注できます")
    lines.extend(f"⚠ {w}" for w in state["warnings"])
    return "\n".join(lines)


# --- CLI ------------------------------------------------------------------

def _parse_pnl_map(text):
    out = {}
    for part in str(text).split(","):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            raise ValueError(f"'口座=金額' の形式で書いてください: {part!r}")
        account, raw = part.split("=", 1)
        amount = _num(raw)
        if amount is None:
            raise ValueError(f"金額が数値ではありません: {part!r}")
        out[account.strip()] = amount
    if not out:
        raise ValueError("--pnl が空です")
    return out


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description="日次ガード(CLAUDE.md §3 の機械強制)")
    parser.add_argument("--status", action="store_true", help="判定と根拠を表示")
    parser.add_argument("--json", action="store_true", help="判定を JSON で出す")
    parser.add_argument("--record", action="store_true", help="決済を台帳に追記")
    parser.add_argument("--initialize", action="store_true",
                        help="初回だけ空台帳と初期化markerを明示作成")
    parser.add_argument("--pnl", help="口座別: 0002=-38.5,0003=-37")
    parser.add_argument("--pnl-each", type=float, help="全口座同額のとき")
    parser.add_argument("--closed-at", help="決済時刻(タイムゾーン付き ISO)")
    parser.add_argument("--side", help="BUY / SELL(任意)")
    args = parser.parse_args(argv)

    try:
        env = dict(os.environ)
        try:
            import nqx_state
            env.update(nqx_state.load_cloud_env(required=False) or {})
        except Exception:
            pass                                  # env が無くても判定は成立する

        if args.initialize:
            initialize_ledger()
            print(f"初期化しました: {LEDGER}")

        if args.record:
            if (args.pnl is None) == (args.pnl_each is None):
                raise ValueError("--pnl と --pnl-each はどちらか一方を指定してください")
            closed = parse_instant(args.closed_at) if args.closed_at else datetime.now(JST)
            if closed is None:
                raise ValueError("--closed-at がタイムゾーン付き ISO ではありません")
            row = {"closedAt": closed.isoformat(), "source": "manual"}
            if args.side:
                row["side"] = str(args.side).upper()
            if args.pnl is not None:
                row["pnl"] = _parse_pnl_map(args.pnl)
            else:
                row["pnlEach"] = float(args.pnl_each)
            append_row(row)
            print(f"記録しました: {json.dumps(row, ensure_ascii=False, sort_keys=True)}")

        state = check(env=env)
        if args.json:
            sys.stdout.write(json.dumps(state, ensure_ascii=False, indent=1) + "\n")
        else:
            print(format_status(state))
    except (ValueError, OSError) as exc:
        sys.stderr.write(json.dumps({"error": f"{type(exc).__name__}: {exc}"},
                                    ensure_ascii=False) + "\n")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())

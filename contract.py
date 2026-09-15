#!/usr/bin/env python3
"""R102: 取引限月(contract month)の正本。

2026-09-15、TradingView の連続足 ``CME_MINI:MNQ1!`` が 12 月限へロールしたのに発注先は
9 月限 ``MNQU6`` のままで、12 月限の値で作った指値が 9 月限へ 285pt 上の通過指値として
飛び、SL は市場より上で置けず裸建玉になった(docs/R102_CONTRACT_ROLL_GUARD.md §0)。
銘柄の検査は「MNQ を含むか」だけで、限月の一致をどこも見ていなかった。

このモジュールは ``execution_contract.json`` の ``contract`` ブロックを唯一の正本として、

* 発注先の限月コード(``MNQU6``)と TradingView 側の銘柄(``CME_MINI:MNQU2026``)
* 満期(最終取引日)と、その手前で新規を止める日数
* 次の限月(ロール先)

を返し、チャート銘柄・環境変数・設定ファイルが正本と一致しているかを機械で検査する
(``python contract.py --status``)。ネットワーク・ブローカー・チャートには触れない。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import execution_contract

BASE = os.path.dirname(os.path.abspath(__file__))

# CME の限月コード。MNQU6 / MNQU26 / MNQU2026 を同じ契約として扱う。
_CODE_RE = re.compile(r"^([A-Z]{1,4})([FGHJKMNQUVXZ])(\d{1,4})$")
# TradingView の連続足(MNQ1! / NQ2! など)。発注先の限月とは一致し得ないので常に不一致。
_CONTINUOUS_RE = re.compile(r"^[A-Z]{1,6}\d!$")
_MONTH_CODES = {"F": 1, "G": 2, "H": 3, "J": 4, "K": 5, "M": 6,
                "N": 7, "Q": 8, "U": 9, "V": 10, "X": 11, "Z": 12}


class ContractError(ValueError):
    """契約ブロックが壊れている(起動時に落とす)。"""


# --------------------------------------------------------------------------- 正本
def section() -> Dict[str, Any]:
    block = execution_contract.CONTRACT.get("contract")
    if not isinstance(block, dict):
        raise ContractError("execution_contract.json に contract ブロックがありません(R102)")
    return block


def _validated(block: Dict[str, Any], *, label: str) -> Dict[str, Any]:
    symbol_code = str(block.get("symbol") or "").strip().upper()
    tv = str(block.get("tvSymbol") or "").strip()
    expiry_text = str(block.get("expiry") or "").strip()
    if not _CODE_RE.match(symbol_code):
        raise ContractError(f"{label}.symbol が限月コードではありません: {symbol_code!r}")
    if not tv or not same_contract(symbol_code, tv):
        raise ContractError(f"{label}.tvSymbol {tv!r} が {label}.symbol {symbol_code!r} と別の限月です")
    try:
        expiry_value = date.fromisoformat(expiry_text)
    except ValueError as exc:
        raise ContractError(f"{label}.expiry が日付ではありません: {expiry_text!r}") from exc
    root, month, year = _split_code(symbol_code)
    if (expiry_value.year, expiry_value.month) != (year, month):
        raise ContractError(f"{label}.expiry {expiry_text} が限月 {symbol_code} の年月と違います")
    return {"symbol": symbol_code, "tvSymbol": tv, "expiry": expiry_value, "root": root}


def current() -> Dict[str, Any]:
    """発注先の限月。``{"symbol", "tvSymbol", "expiry"(date), "root"}``。"""
    return _validated(section(), label="contract")


def next_contract() -> Optional[Dict[str, Any]]:
    block = section().get("next")
    if not isinstance(block, dict) or not block.get("symbol"):
        return None
    return _validated(block, label="contract.next")


def symbol() -> str:
    return current()["symbol"]


def tv_symbol() -> str:
    return current()["tvSymbol"]


def expiry() -> date:
    return current()["expiry"]


def last_entry_days() -> int:
    value = section().get("lastEntryDaysBeforeExpiry", 3)
    try:
        days = int(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"contract.lastEntryDaysBeforeExpiry が整数ではありません: {value!r}") from exc
    return max(days, 0)


# --------------------------------------------------------------------------- 限月コード
def _split_code(code: str) -> Tuple[str, int, int]:
    match = _CODE_RE.match(code.strip().upper())
    if not match:
        raise ContractError(f"限月コードではありません: {code!r}")
    root, month_code, year_text = match.groups()
    year = int(year_text)
    if len(year_text) == 1:            # MNQU6 → 2026(2020 年代を前提。2030 年以降は 2 桁以上で書く)
        year = 2020 + year
    elif len(year_text) == 2:          # MNQU26
        year = 2000 + year
    return root, _MONTH_CODES[month_code], year


def normalize_code(value: str) -> Optional[str]:
    """``MNQU6`` / ``CME_MINI:MNQU2026`` / ``MNQU26`` → ``MNQU2026``。連続足や無関係は None。"""
    text = str(value or "").strip().upper()
    if not text:
        return None
    if ":" in text:
        text = text.split(":", 1)[1]
    if _CONTINUOUS_RE.match(text) or not _CODE_RE.match(text):
        return None
    root, month, year = _split_code(text)
    month_code = next(k for k, v in _MONTH_CODES.items() if v == month)
    return f"{root}{month_code}{year}"


def is_continuous(value: str) -> bool:
    text = str(value or "").strip().upper()
    if ":" in text:
        text = text.split(":", 1)[1]
    return bool(_CONTINUOUS_RE.match(text))


def same_contract(a: str, b: str) -> bool:
    na, nb = normalize_code(a), normalize_code(b)
    return na is not None and na == nb


def continuous_root(value: Any) -> Optional[str]:
    """``CME_MINI:MNQ1!`` → ``MNQ``。連続足でなければ None。"""
    text = str(value or "").strip().upper()
    if ":" in text:
        text = text.split(":", 1)[1]
    match = re.match(r"^([A-Z]{1,6})\d!$", text)
    return match.group(1) if match else None


def chart_symbol_plausible(chart_symbol: Any) -> Tuple[bool, str]:
    """pane 一覧など front_contract がまだ無い段階の緩い検査。

    限月そのものなら厳密一致、連続足なら root(MNQ)の一致だけを見る。厳密な判定は
    front_contract を添えた ``chart_symbol_matches`` が state ごとに行う。
    """
    text = str(chart_symbol or "").strip()
    if is_continuous(text):
        expected_root = current()["root"]
        if continuous_root(text) == expected_root:
            return True, "ok (continuous, front contract resolved later)"
        return False, f"CHART_SYMBOL_MISMATCH: chart={text} (root {continuous_root(text)}) contract={tv_symbol()}"
    return chart_symbol_matches(text)


def chart_symbol_matches(chart_symbol: Any, front_contract: Any = None) -> Tuple[bool, str]:
    """チャートの銘柄が発注先の限月と同じか。``(ok, reason)``。

    * 限月そのもの(``CME_MINI:MNQU2026``): 正本と同じ限月なら ok、違えば ``CHART_SYMBOL_MISMATCH``。
    * 連続足(``CME_MINI:MNQ1!``): TradingView が今どの限月を指しているか(``front_contract``、
      ``mainSeries().symbolInfo()`` から毎周期取る)を添えて判定する。一致すれば ok、
      別限月(TradingView がロールした)なら ``CHART_SYMBOL_MISMATCH``、解決できていなければ
      ``CHART_SYMBOL_CONTINUOUS_UNRESOLVED``(fail-closed)。2026-09-15 の事故は front=MNQZ2026 vs
      contract=MNQU2026 の MISMATCH としてここで止まる。
    どれもサイクルを BLOCK する(自動で張り替えない)。
    """
    text = str(chart_symbol or "").strip()
    expected = tv_symbol()
    if not text:
        return False, f"CHART_SYMBOL_MISSING: contract={expected}"
    if is_continuous(text):
        if continuous_root(text) != current()["root"]:
            return False, f"CHART_SYMBOL_MISMATCH: chart={text} (root {continuous_root(text)}) contract={expected}"
        front = str(front_contract or "").strip()
        if not front:
            return False, (f"CHART_SYMBOL_CONTINUOUS_UNRESOLVED: chart={text} contract={expected} "
                           "(front contract unknown; TradingView symbolInfo not acquired)")
        if same_contract(front, expected):
            return True, f"ok: {text} -> {normalize_code(front)}"
        return False, (f"CHART_SYMBOL_MISMATCH: chart={text} front={normalize_code(front) or front} "
                       f"contract={expected} (TradingView rolled; roll the order contract)")
    if same_contract(text, expected):
        return True, "ok"
    return False, f"CHART_SYMBOL_MISMATCH: chart={text} contract={expected}"


def resolved_contract(chart_symbol: Any, front_contract: Any = None) -> Optional[str]:
    """価格の出所としての限月そのもの(取引所付き)。``CME_MINI:MNQ1!`` + ``MNQZ2026`` → ``CME_MINI:MNQZ2026``。"""
    text = str(chart_symbol or "").strip()
    if not text:
        return None
    exchange = text.split(":", 1)[0] if ":" in text else "CME_MINI"
    code = normalize_code(front_contract) if is_continuous(text) else normalize_code(text)
    return f"{exchange}:{code}" if code else None


# --------------------------------------------------------------------------- 満期
def third_friday(year: int, month: int) -> date:
    first = date(year, month, 1)
    offset = (4 - first.weekday()) % 7          # weekday(): Mon=0 … Fri=4
    return first + timedelta(days=offset + 14)


def clock_override() -> Optional[datetime]:
    """``contract.clockOverride``(ISO 日時)。テストの固定契約(tests/_pin_contract.py と隔離サンドボックス)が
    fixture の限月(MNQU6)を満期後も使えるようにする明示の設定。本番 JSON には書かない(--status が警告する)。"""
    raw = section().get("clockOverride") if isinstance(execution_contract.CONTRACT.get("contract"), dict) else None
    if not raw:
        return None
    try:
        moment = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _now_date(now: Optional[datetime]) -> date:
    moment = now or clock_override() or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    # 満期は ET 09:30 だが、日付判定は UTC の日付で十分(JST の朝には UTC も同日)。
    return moment.astimezone(timezone.utc).date()


def days_to_expiry(now: Optional[datetime] = None) -> int:
    return (expiry() - _now_date(now)).days


def entry_allowed(now: Optional[datetime] = None) -> Tuple[bool, str]:
    """満期の手前では新規 ENTRY を出さない。管理と撤退はこの判定を見ない。"""
    left = days_to_expiry(now)
    limit = last_entry_days()
    if left < 0:
        return False, f"CONTRACT_EXPIRED: {symbol()} expired {expiry().isoformat()} ({-left}d ago)"
    if left < limit:
        return False, (f"CONTRACT_EXPIRY_NEAR: {symbol()} expires {expiry().isoformat()} "
                       f"in {left}d < {limit}d")
    return True, f"ok: {symbol()} expires {expiry().isoformat()} in {left}d"


def summary_line(now: Optional[datetime] = None) -> str:
    """nqx_cycle の行頭に出す 1 行。例: ``contract: MNQU6 (CME_MINI:MNQU2026) exp 2026-09-18 (3d) entry=ok``。"""
    try:
        ok, reason = entry_allowed(now)
        tag = "ok" if ok else reason.split(":", 1)[0]
        return (f"contract: {symbol()} ({tv_symbol()}) exp {expiry().isoformat()} "
                f"({days_to_expiry(now)}d) entry={tag}")
    except ContractError as exc:
        return f"contract: INVALID ({exc})"


# --------------------------------------------------------------------------- 参照サイトの照合
def _read_text(path: str) -> Optional[str]:
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return None


def _env_value(path: str, key: str) -> Optional[str]:
    text = _read_text(path)
    if text is None:
        return None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def _regex_value(path: str, pattern: str) -> Optional[str]:
    text = _read_text(path)
    if text is None:
        return None
    match = re.search(pattern, text, re.MULTILINE)
    return match.group(1).strip() if match else None


def _json_value(path: str, key: str) -> Optional[str]:
    text = _read_text(path)
    if text is None:
        return None
    try:
        value = json.loads(text).get(key)
    except (ValueError, AttributeError):
        return None
    return None if value is None else str(value)


def reference_sites(base: str = BASE) -> List[Dict[str, Any]]:
    """発注先の限月を別々に持っている場所。``required=False`` は無くても警告だけ。"""
    return [
        {"label": "env NQX_SYMBOL (process)", "kind": "env", "required": False},
        {"label": ".secrets/nqx_cloud.env NQX_SYMBOL", "kind": "envfile",
         "path": os.path.join(base, ".secrets", "nqx_cloud.env"), "key": "NQX_SYMBOL", "required": False},
        {"label": ".secrets/crosstrade.env NQX_SYMBOL", "kind": "envfile",
         "path": os.path.join(base, ".secrets", "crosstrade.env"), "key": "NQX_SYMBOL", "required": False},
        {"label": "monitor_config.json symbol", "kind": "json",
         "path": os.path.join(base, "monitor_config.json"), "key": "symbol", "required": True},
        {"label": "cloudflare/wrangler.toml NQX_SYMBOL", "kind": "regex",
         "path": os.path.join(base, "cloudflare", "wrangler.toml"),
         "pattern": r'^\s*NQX_SYMBOL\s*=\s*"([^"]+)"', "required": True},
        {"label": "TRADING_CONTEXT.md §2 シンボル", "kind": "regex",
         "path": os.path.join(base, "TRADING_CONTEXT.md"),
         "pattern": r"^\|\s*シンボル\s*\|\s*\*\*([A-Z0-9]+)\*\*", "required": False},
        {"label": "last chart symbol (.secrets/tv_raw/chart_state.json)", "kind": "chart",
         "path": os.path.join(base, ".secrets", "tv_raw", "chart_state.json"), "required": False},
    ]


def check_sites(base: str = BASE, environ: Optional[Dict[str, str]] = None) -> List[Dict[str, Any]]:
    """各サイトの値と正本を比べる。``ok`` が False の行が 1 つでもあれば起動を止めてよい。"""
    env = os.environ if environ is None else environ
    expected = symbol()
    rows: List[Dict[str, Any]] = []
    for site in reference_sites(base):
        kind = site["kind"]
        found: Optional[str]
        if kind == "env":
            found = env.get("NQX_SYMBOL")
        elif kind == "envfile":
            found = _env_value(site["path"], site["key"])
        elif kind == "json":
            found = _json_value(site["path"], site["key"])
        elif kind == "regex":
            found = _regex_value(site["path"], site["pattern"])
        elif kind == "chart":
            found = _json_value(site["path"], "symbol")
        else:  # pragma: no cover - defensive
            found = None
        if found is None:
            ok = not site.get("required", False)
            note = "missing"
        elif kind == "chart":
            raw_dir = os.path.join(base, ".secrets", "tv_raw")
            front = (_json_value(os.path.join(raw_dir, "symbol_info.json"), "front_contract")
                     or _json_value(os.path.join(raw_dir, "symbol_info_15m.json"), "front_contract"))
            ok, note = chart_symbol_matches(found, front)
            if front:
                found = f"{found} (front {front})"
        else:
            ok = same_contract(found, expected)
            note = "ok" if ok else "MISMATCH"
        rows.append({"label": site["label"], "found": found, "expected": expected,
                     "ok": bool(ok), "note": note, "required": bool(site.get("required", False))})
    return rows


def enforce_sites(base: str = BASE, environ: Optional[Dict[str, str]] = None) -> Optional[str]:
    """正本と食い違う**設定**(チャートの観測は除く)があれば理由文字列を返す。無ければ None。"""
    bad = [row for row in check_sites(base, environ)
           if not row["ok"] and row["note"] != "missing" and not row["label"].startswith("last chart")]
    if not bad:
        return None
    parts = [f"{row['label']}={row['found']}" for row in bad]
    return f"CONTRACT_SYMBOL_DISAGREE: contract={symbol()} vs " + ", ".join(parts)


# --------------------------------------------------------------------------- CLI
def status_lines(now: Optional[datetime] = None, base: str = BASE) -> Tuple[List[str], bool]:
    lines: List[str] = []
    healthy = True
    try:
        cur = current()
    except ContractError as exc:
        return [f"contract: INVALID - {exc}"], False
    if clock_override() is not None:
        lines.append(f"WARNING  : contract.clockOverride={clock_override().isoformat()} (test pin; must not be set in production)")
        healthy = False
    lines.append(f"contract : {cur['symbol']}  tv={cur['tvSymbol']}  expiry={cur['expiry'].isoformat()}"
                 f"  ({days_to_expiry(now)}d left, third Friday "
                 f"{'ok' if cur['expiry'] == third_friday(cur['expiry'].year, cur['expiry'].month) else 'MISMATCH'})")
    ok, reason = entry_allowed(now)
    lines.append(f"entry    : {'allowed' if ok else 'BLOCKED'} - {reason}")
    nxt = next_contract()
    if nxt is None:
        lines.append("next     : (未設定) - ロール前に contract.next を書く")
    else:
        lines.append(f"next     : {nxt['symbol']}  tv={nxt['tvSymbol']}  expiry={nxt['expiry'].isoformat()}")
    lines.append("sites    :")
    for row in check_sites(base):
        mark = "OK " if row["ok"] else "NG "
        if row["found"] is None:
            mark = "-- " if not row["required"] else "NG "
        lines.append(f"  {mark} {row['label']}: {row['found'] if row['found'] is not None else '(missing)'}"
                     + ("" if row["ok"] else f"  <- {row['note']}"))
        if not row["ok"]:
            healthy = False
    return lines, healthy


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="R102: 取引限月の正本と参照サイトの照合(読むだけ)")
    parser.add_argument("--status", action="store_true", help="限月・満期・参照サイトの一致を表示する")
    parser.add_argument("--json", action="store_true", help="JSON で出力する")
    args = parser.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # cp932 コンソール対策
    except (AttributeError, ValueError):
        pass
    if args.json:
        try:
            cur = current()
            payload = {"symbol": cur["symbol"], "tvSymbol": cur["tvSymbol"],
                       "expiry": cur["expiry"].isoformat(), "daysToExpiry": days_to_expiry(),
                       "entryAllowed": entry_allowed()[0], "entryReason": entry_allowed()[1],
                       "next": None, "sites": check_sites()}
            nxt = next_contract()
            if nxt is not None:
                payload["next"] = {"symbol": nxt["symbol"], "tvSymbol": nxt["tvSymbol"],
                                   "expiry": nxt["expiry"].isoformat()}
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0 if all(row["ok"] for row in payload["sites"]) else 1
        except ContractError as exc:
            print(json.dumps({"error": str(exc)}, ensure_ascii=False))
            return 2
    lines, healthy = status_lines()
    print("\n".join(lines))
    return 0 if healthy else 1


if __name__ == "__main__":
    sys.exit(main())

# -*- coding: utf-8 -*-
"""R102: 取引限月の正本(contract.py)と満期ガード。

2026-09-15 15:10、連続足 MNQ1! が 12 月限へロールしたのに発注先は 9 月限 MNQU6 のままで、
12 月限の値の指値が 9 月限へ 285pt 上の通過指値として飛び、SL は市場より上で置けず裸になった。
銘柄検査は「MNQ を含むか」だけだった(docs/R102_CONTRACT_ROLL_GUARD.md)。

守ること:
  1. MNQU6 / MNQU26 / CME_MINI:MNQU2026 は同じ限月。MNQ1! / MNQ2!(連続足)はどの限月とも一致しない。
  2. チャート銘柄は発注先の限月そのものでなければ CHART_SYMBOL_CONTINUOUS / _MISMATCH。
  3. 満期(第 3 金曜)の手前 lastEntryDaysBeforeExpiry 日未満は新規不可、満期後は CONTRACT_EXPIRED。
  4. 参照サイト(monitor_config.json / wrangler.toml / env)が正本と食い違えば CONTRACT_SYMBOL_DISAGREE。
  5. 契約ブロックの tvSymbol / expiry が symbol と別の限月なら ContractError(起動時に落ちる)。

純粋関数だけ。ネットワーク・ブローカー・チャートに触れない。

    python tests/test_r102_contract.py
"""
import copy
import json
import os
import sys
import tempfile
from datetime import date, datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.dirname(HERE)
sys.path.insert(0, BASE)

import execution_contract  # noqa: E402
import contract  # noqa: E402

PASS = [0]
FAIL = [0]


def check(label, condition, detail=""):
    if condition:
        PASS[0] += 1
        print(f"  OK   {label}")
    else:
        FAIL[0] += 1
        print(f"  NG   {label}  {detail}")


def section(title):
    print(f"\n--- {title}")


BLOCK = {
    "symbol": "MNQU6", "tvSymbol": "CME_MINI:MNQU2026", "expiry": "2026-09-18",
    "lastEntryDaysBeforeExpiry": 3,
    "next": {"symbol": "MNQZ6", "tvSymbol": "CME_MINI:MNQZ2026", "expiry": "2026-12-18"},
}
execution_contract.CONTRACT["contract"] = copy.deepcopy(BLOCK)


def _utc(y, m, d, hh=0):
    return datetime(y, m, d, hh, tzinfo=timezone.utc)


# ---------------------------------------------------------------- 1. 限月コード
section("1. 限月コードの正規化")
check("MNQU6 -> MNQU2026", contract.normalize_code("MNQU6") == "MNQU2026")
check("MNQU26 -> MNQU2026", contract.normalize_code("MNQU26") == "MNQU2026")
check("CME_MINI:MNQU2026 -> MNQU2026", contract.normalize_code("CME_MINI:MNQU2026") == "MNQU2026")
check("MNQ1! は限月ではない", contract.normalize_code("CME_MINI:MNQ1!") is None)
check("空は None", contract.normalize_code("") is None)
check("same_contract(MNQU6, CME_MINI:MNQU2026)", contract.same_contract("MNQU6", "CME_MINI:MNQU2026"))
check("same_contract(MNQU6, MNQZ6) は偽", not contract.same_contract("MNQU6", "MNQZ6"))
check("same_contract(MNQ1!, MNQU6) は偽", not contract.same_contract("CME_MINI:MNQ1!", "MNQU6"))
check("is_continuous(MNQ1!)", contract.is_continuous("CME_MINI:MNQ1!"))
check("is_continuous(NQ2!)", contract.is_continuous("NQ2!"))
check("is_continuous(MNQU2026) は偽", not contract.is_continuous("CME_MINI:MNQU2026"))

# ---------------------------------------------------------------- 2. 正本の読み出し
section("2. 正本")
check("symbol()", contract.symbol() == "MNQU6")
check("tv_symbol()", contract.tv_symbol() == "CME_MINI:MNQU2026")
check("expiry()", contract.expiry() == date(2026, 9, 18))
nxt = contract.next_contract()
check("next_contract()", nxt is not None and nxt["symbol"] == "MNQZ6" and nxt["expiry"] == date(2026, 12, 18))
check("last_entry_days()", contract.last_entry_days() == 3)

# ---------------------------------------------------------------- 3. チャート銘柄
section("3. チャート銘柄の一致")
ok, reason = contract.chart_symbol_matches("CME_MINI:MNQ1!")
check("MNQ1! で front 未解決は CHART_SYMBOL_CONTINUOUS_UNRESOLVED", not ok and reason.startswith("CHART_SYMBOL_CONTINUOUS_UNRESOLVED"), reason)
ok, reason = contract.chart_symbol_matches("CME_MINI:MNQ1!", "MNQU2026")
check("MNQ1! + front MNQU2026 は一致", ok, reason)
ok, reason = contract.chart_symbol_matches("CME_MINI:MNQ1!", "MNQZ2026")
check("MNQ1! + front MNQZ2026 は CHART_SYMBOL_MISMATCH(ロール検知)", not ok and reason.startswith("CHART_SYMBOL_MISMATCH"), reason)
ok, reason = contract.chart_symbol_matches("CME_MINI:NQ1!", "MNQU2026")
check("NQ1!(別 root)は front が合っても CHART_SYMBOL_MISMATCH", not ok and reason.startswith("CHART_SYMBOL_MISMATCH"), reason)
check("chart_symbol_plausible: MNQ1! は root 一致で通る", contract.chart_symbol_plausible("CME_MINI:MNQ1!")[0])
check("chart_symbol_plausible: NQ1! は通らない", not contract.chart_symbol_plausible("CME_MINI:NQ1!")[0])
check("resolved_contract(MNQ1!, MNQZ2026) = CME_MINI:MNQZ2026", contract.resolved_contract("CME_MINI:MNQ1!", "MNQZ2026") == "CME_MINI:MNQZ2026")
check("resolved_contract(MNQU2026) = CME_MINI:MNQU2026", contract.resolved_contract("CME_MINI:MNQU2026") == "CME_MINI:MNQU2026")
check("resolved_contract(MNQ1!, None) = None", contract.resolved_contract("CME_MINI:MNQ1!") is None)
ok, reason = contract.chart_symbol_matches("CME_MINI:MNQU2026")
check("MNQU2026 は一致", ok, reason)
ok, reason = contract.chart_symbol_matches("MNQU6")
check("MNQU6(取引所なし)も一致", ok, reason)
ok, reason = contract.chart_symbol_matches("CME_MINI:MNQZ2026")
check("MNQZ2026 は CHART_SYMBOL_MISMATCH", not ok and reason.startswith("CHART_SYMBOL_MISMATCH"), reason)
ok, reason = contract.chart_symbol_matches("CME_MINI:NQU2026")
check("NQ(E-mini)は CHART_SYMBOL_MISMATCH", not ok and reason.startswith("CHART_SYMBOL_MISMATCH"), reason)
ok, reason = contract.chart_symbol_matches("")
check("空は CHART_SYMBOL_MISSING", not ok and reason.startswith("CHART_SYMBOL_MISSING"), reason)

# ---------------------------------------------------------------- 4. 満期
section("4. 満期と新規可否")
check("第 3 金曜 2026-09", contract.third_friday(2026, 9) == date(2026, 9, 18))
check("第 3 金曜 2026-12", contract.third_friday(2026, 12) == date(2026, 12, 18))
check("第 3 金曜 2027-03", contract.third_friday(2027, 3) == date(2027, 3, 19))
check("days_to_expiry 09-14 = 4", contract.days_to_expiry(_utc(2026, 9, 14)) == 4)
check("days_to_expiry 09-15 = 3", contract.days_to_expiry(_utc(2026, 9, 15, 6)) == 3)
ok, reason = contract.entry_allowed(_utc(2026, 9, 14))
check("4 日前は新規可", ok, reason)
ok, reason = contract.entry_allowed(_utc(2026, 9, 15))
check("3 日前(= 境界)は新規可", ok, reason)
ok, reason = contract.entry_allowed(_utc(2026, 9, 16))
check("2 日前は CONTRACT_EXPIRY_NEAR", not ok and reason.startswith("CONTRACT_EXPIRY_NEAR"), reason)
ok, reason = contract.entry_allowed(_utc(2026, 9, 19))
check("満期翌日は CONTRACT_EXPIRED", not ok and reason.startswith("CONTRACT_EXPIRED"), reason)
line = contract.summary_line(_utc(2026, 9, 16))
check("summary_line は限月・満期・entry を持つ",
      line.startswith("contract: MNQU6 (CME_MINI:MNQU2026) exp 2026-09-18 (2d) entry=CONTRACT_EXPIRY_NEAR"), line)

# ---------------------------------------------------------------- 5. 参照サイト
section("5. 参照サイトの照合")
tmp = tempfile.mkdtemp(prefix="r102_")
os.makedirs(os.path.join(tmp, "cloudflare"))
os.makedirs(os.path.join(tmp, ".secrets", "tv_raw"))
with open(os.path.join(tmp, "monitor_config.json"), "w", encoding="utf-8") as fh:
    json.dump({"schemaVersion": "NQX_MONITOR_PIPELINE/1", "symbol": "MNQU6"}, fh)
with open(os.path.join(tmp, "cloudflare", "wrangler.toml"), "w", encoding="utf-8") as fh:
    fh.write('[vars]\nNQX_ACCOUNT_ID = "x"\nNQX_SYMBOL = "MNQU6"\n')
with open(os.path.join(tmp, ".secrets", "nqx_cloud.env"), "w", encoding="utf-8") as fh:
    fh.write("NQX_API_BASE=http://127.0.0.1:9/blocked\nNQX_SYMBOL=MNQU6\n")
with open(os.path.join(tmp, ".secrets", "tv_raw", "chart_state.json"), "w", encoding="utf-8") as fh:
    json.dump({"symbol": "CME_MINI:MNQU2026"}, fh)
rows = contract.check_sites(tmp, environ={})
check("全サイト一致(チャート含む)", all(r["ok"] for r in rows), json.dumps(rows, ensure_ascii=False))
check("enforce_sites は None", contract.enforce_sites(tmp, environ={}) is None)

with open(os.path.join(tmp, "cloudflare", "wrangler.toml"), "w", encoding="utf-8") as fh:
    fh.write('[vars]\nNQX_SYMBOL = "MNQZ6"\n')
rows = contract.check_sites(tmp, environ={})
bad = [r for r in rows if not r["ok"]]
check("wrangler の別限月を検出", len(bad) == 1 and "wrangler" in bad[0]["label"], json.dumps(bad, ensure_ascii=False))
reason = contract.enforce_sites(tmp, environ={})
check("enforce_sites は CONTRACT_SYMBOL_DISAGREE", reason is not None and reason.startswith("CONTRACT_SYMBOL_DISAGREE"), reason)

with open(os.path.join(tmp, "cloudflare", "wrangler.toml"), "w", encoding="utf-8") as fh:
    fh.write('[vars]\nNQX_SYMBOL = "MNQU6"\n')
reason = contract.enforce_sites(tmp, environ={"NQX_SYMBOL": "MNQZ6"})
check("環境変数の別限月も検出", reason is not None and "env NQX_SYMBOL" in reason, reason)

with open(os.path.join(tmp, ".secrets", "tv_raw", "chart_state.json"), "w", encoding="utf-8") as fh:
    json.dump({"symbol": "CME_MINI:MNQ1!"}, fh)
rows = contract.check_sites(tmp, environ={})
chart_row = [r for r in rows if r["label"].startswith("last chart")][0]
check("チャート観測は連続足を NG で示す", not chart_row["ok"] and "CONTINUOUS" in chart_row["note"], chart_row["note"])
check("チャート観測は enforce_sites では止めない(設定ではなく観測)", contract.enforce_sites(tmp, environ={}) is None)

os.remove(os.path.join(tmp, "monitor_config.json"))
rows = contract.check_sites(tmp, environ={})
mc = [r for r in rows if r["label"].startswith("monitor_config")][0]
check("必須サイトの欠落は NG", not mc["ok"] and mc["note"] == "missing", json.dumps(mc, ensure_ascii=False))

# ---------------------------------------------------------------- 6. 壊れた契約
section("6. 壊れた契約ブロックは起動で落ちる")
broken = copy.deepcopy(BLOCK)
broken["tvSymbol"] = "CME_MINI:MNQZ2026"
execution_contract.CONTRACT["contract"] = broken
try:
    contract.current()
    check("tvSymbol が別限月なら ContractError", False)
except contract.ContractError as exc:
    check("tvSymbol が別限月なら ContractError", "別の限月" in str(exc), str(exc))

broken = copy.deepcopy(BLOCK)
broken["expiry"] = "2026-12-18"
execution_contract.CONTRACT["contract"] = broken
try:
    contract.current()
    check("expiry が別の年月なら ContractError", False)
except contract.ContractError as exc:
    check("expiry が別の年月なら ContractError", "年月" in str(exc), str(exc))

broken = copy.deepcopy(BLOCK)
broken["symbol"] = "MNQ1!"
execution_contract.CONTRACT["contract"] = broken
try:
    contract.current()
    check("symbol が連続足なら ContractError", False)
except contract.ContractError as exc:
    check("symbol が連続足なら ContractError", True)

execution_contract.CONTRACT.pop("contract")
try:
    contract.current()
    check("contract ブロック無しは ContractError", False)
except contract.ContractError:
    check("contract ブロック無しは ContractError", True)
line = contract.summary_line()
check("summary_line は INVALID を返す(例外にしない)", line.startswith("contract: INVALID"), line)
execution_contract.CONTRACT["contract"] = copy.deepcopy(BLOCK)

# ---------------------------------------------------------------- 7. 本番 JSON
section("7. 本番 execution_contract.json の contract ブロック")
with open(os.path.join(BASE, "execution_contract.json"), encoding="utf-8") as fh:
    live = json.load(fh)
execution_contract.CONTRACT["contract"] = live.get("contract")
try:
    cur = contract.current()
    check("本番ブロックが読める", True)
    check("本番 expiry は第 3 金曜", cur["expiry"] == contract.third_friday(cur["expiry"].year, cur["expiry"].month),
          cur["expiry"].isoformat())
    check("本番 tvSymbol は連続足ではない", not contract.is_continuous(cur["tvSymbol"]), cur["tvSymbol"])
    nxt = contract.next_contract()
    check("本番 next は current より後の限月", nxt is None or nxt["expiry"] > cur["expiry"])
except contract.ContractError as exc:
    check("本番ブロックが読める", False, str(exc))

print(f"\nR102 contract: {PASS[0]} passed, {FAIL[0]} failed")
sys.exit(1 if FAIL[0] else 0)

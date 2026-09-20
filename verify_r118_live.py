# -*- coding: utf-8 -*-
"""R118(Gateway)の本番導入を、**発注も通信もせずに**確認する(読むだけ)。

    python verify_r118_live.py

やること: いまの契約・設定・常駐の状態・影運転の実績を読んで、

1. **設定の前提**が揃っているか(限月 ID / 口座 ID / トークン)
2. 段(常駐 → SHADOW → LIVE → 送信検証 → Coordinator)の**組み合わせに矛盾が無いか**
3. **次に進んでよい段**と、そのために足りないものは何か

を出す。判断はしない —— 段を進めるのは運用者で、ここは条件が揃ったかどうかだけを言う。

やらないこと: CrossTrade への接続、注文、publish、AUTO の変更、ファイルの書き込み。

終了コード: 0 = いまの構成に矛盾が無い / 1 = 矛盾がある(理由を出す)。
"""
from __future__ import annotations

import io
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
JST = timezone(timedelta(hours=9))

problems: list[str] = []
notes: list[str] = []

# --------------------------------------------------------------- 段 2 → 3 の条件
#
# docs/R118_GATEWAY_INTEGRATION.md §8 は「2 と 3 の間は最低 1 取引日空ける」とだけ
# 書いてある。1 取引日は 3 分周期で約 440 周期。ここはその半分を下限にし、
# **比較が実際に成立していること**と**建玉があった周期を含むこと**を足す
# (FLAT どうしの qty=0 比較は必ず一致するので、それだけでは一致の証拠にならない)。
SHADOW_MIN_CYCLES = 200
SHADOW_MIN_COMPARED = 500
SHADOW_MAX_UNAVAILABLE_RATIO = 0.05
SHADOW_MIN_POSITION_CYCLES = 1


def ok(text: str) -> None:
    print(f"  ok   {text}")


def fail(text: str) -> None:
    problems.append(text)
    print(f"  NG   {text}")


def note(text: str) -> None:
    notes.append(text)
    print(f"  --   {text}")


def section(title: str) -> None:
    print(f"\n--- {title} ---")


def stamp(value: float) -> str:
    try:
        return datetime.fromtimestamp(float(value), timezone.utc).astimezone(JST) \
            .strftime("%m-%d %H:%M:%S JST")
    except (TypeError, ValueError, OSError):
        return "-"


import broker_gateway as gw          # noqa: E402
import broker_source as src          # noqa: E402
import broker_status as bs           # noqa: E402

print("=" * 68)
print("R118 Gateway — 導入状態の確認(読むだけ)")
print("=" * 68)

# ============================================================ 0. 解決先と契約

section("0. どのファイルを読んでいるか")

print(f"  契約     {src.CONTRACT_PATH}")
print(f"  設定     {bs.CROSSTRADE_ENV}")
print(f"  観測     {gw.snapshot_path()}")
print(f"  生存     {gw.heartbeat_path()}")
print(f"  影運転   {src.CYCLE_LOG}")
print(f"  食い違い {src.DIVERGENCE_LOG}")

contract = src._contract()
if not contract:
    fail("契約に gateway 節が無い(全部 OFF として動く)")
mode = src.mode()
env_mode = str(os.environ.get("NQX_GATEWAY_MODE") or "").strip().upper()
autostart = gw.autostart_enabled(contract)
send_verification = bool(contract.get("sendVerification"))
coordinator = contract.get("coordinator") or {}
coordinator_on = bool(coordinator.get("enabled"))
lanes = coordinator.get("maxParallel")

print(f"\n  mode={mode}" + (f"(環境変数 NQX_GATEWAY_MODE={env_mode} が契約より優先)"
                            if env_mode in src.MODES else "")
      + f" · autostart={autostart} · sendVerification={send_verification}"
      + f" · coordinator={coordinator_on}(lanes={lanes})")

# ============================================================ 1. 設定の前提

section("1. 設定の前提(ここが欠けると Gateway は黙って全 REST に落ちる)")

cfg = bs.read_env(bs.CROSSTRADE_ENV)
symbol = str(bs.DEFAULT_SYMBOL)
contract_ids = src._contract_ids()
if contract_ids:
    ok(f"限月 ID が設定にある({symbol})")
else:
    fail(f"CROSSTRADE_CONTRACT_ID_{symbol} が設定に無い —— "
         f"Gateway 経路は 1 件も使われず、理由は NO_CONTRACT_ID になる"
         f"(限月ロールのたびに起きる)")

accounts = [value.strip() for value in
            str(cfg.get("CROSSTRADE_ACCOUNTS") or "").split(",") if value.strip()]
if not accounts:
    fail("CROSSTRADE_ACCOUNTS が空(口座が読めない)")
else:
    missing = [name for name in accounts
               if not cfg.get("CROSSTRADE_ACCOUNT_ID_" + name)]
    if missing:
        fail(f"accountId が設定に無い口座 {len(missing)} 件 —— WebSocket の行は "
             f"accountId しか持たないので ACCOUNT_NOT_OBSERVED で REST へ落ちる")
    else:
        ok(f"{len(accounts)} 口座すべてに accountId がある")

if cfg.get("CROSSTRADE_API_TOKEN") or cfg.get("CROSSTRADE_KEY"):
    ok("CrossTrade のトークンがある(常駐に必要)")
else:
    fail("CrossTrade のトークンが無い(常駐できない)")

try:
    import contract as contract_month
    frozen = contract_month.symbol()
    if str(frozen) == symbol:
        ok(f"契約の限月と broker_status.DEFAULT_SYMBOL が一致({symbol})")
    else:
        fail(f"限月が食い違う: 契約 {frozen} / broker_status {symbol}")
except Exception as exc:  # noqa: BLE001
    note(f"限月の正本を読めなかった({type(exc).__name__})")

# ============================================================ 2. 段の組み合わせ

section("2. 段の組み合わせに矛盾が無いか")

if mode == src.OFF:
    ok("mode=OFF —— 観測は従来どおり REST(R117 以前と同じ経路)")
    if autostart:
        note("autostart=true だが mode=OFF なので常駐は起動しない(設計どおり)")
else:
    ok(f"mode={mode}")

if send_verification and mode != src.LIVE:
    fail(f"sendVerification=true なのに mode={mode} —— 送信前後の検証だけを"
         f"押し込みに任せることになる。観測を LIVE にしてから進める段(§8 の 3 → 4)")
elif send_verification:
    ok("sendVerification=true(mode=LIVE のうえで有効)")

if coordinator_on:
    try:
        if int(lanes) < 1:
            fail(f"coordinator.enabled=true だが maxParallel={lanes}")
        else:
            ok(f"coordinator=true(lanes={lanes})")
    except (TypeError, ValueError):
        fail(f"coordinator.maxParallel が数でない({lanes!r})")
    if mode == src.OFF:
        note("coordinator は観測の出所と独立(送信の並行化だけ)。mode=OFF でも効く")

budget = (coordinator.get("commsBudget") or {})
if budget.get("sendCountsAgainstApiBudget") is None:
    note("commsBudget.sendCountsAgainstApiBudget=null(未確認)—— 送信も枠を食う"
         "前提で確保している。CrossTrade の回答が来るまでここは推測で埋めない")

for switch, name in ((os.environ.get("NQX_GATEWAY_MODE"), "NQX_GATEWAY_MODE"),
                     (os.environ.get("NQX_GATEWAY_AUTOSTART"), "NQX_GATEWAY_AUTOSTART"),
                     (os.environ.get("NQX_COORDINATOR"), "NQX_COORDINATOR"),
                     (os.environ.get("NQX_COORDINATOR_LANES"), "NQX_COORDINATOR_LANES")):
    if switch:
        note(f"環境変数 {name}={switch} が効いている(契約より強い)")

# ============================================================ 3. 常駐の状態

section("3. 常駐(段 1)")

beat = gw.read_heartbeat()
snapshot = gw.read_snapshot(gw.snapshot_path())
alive = gw.alive()
if not beat:
    note("常駐したことが無い(heartbeat が無い)")
else:
    age = time.time() - float(beat.get("at") or 0)
    print(f"  heartbeat {stamp(beat.get('at'))}({age:.0f} 秒前)"
          f" · pid={beat.get('pid')} · integrity={beat.get('integrity')}"
          f" · 口座 {beat.get('accounts')}")
    if alive:
        ok("常駐は生きている")
        if accounts and int(beat.get("accounts") or 0) != len(accounts):
            fail(f"常駐が見ている口座 {beat.get('accounts')} と設定の {len(accounts)} が違う")
    else:
        note("常駐は止まっている(観測は全口座 REST へ退避する = 安全側)")

if isinstance(snapshot, dict):
    written = float(snapshot.get("writtenAt") or 0)
    age = time.time() - written
    fresh = 0 <= age <= src.max_age_sec()
    print(f"  観測      {stamp(written)}({age:.0f} 秒前 / 上限 {src.max_age_sec():.0f} 秒)"
          f" · integrity={snapshot.get('integrity')}")
    if alive and not fresh:
        fail("常駐は生きているのにスナップショットが鮮度外(出所層は使わない)")
elif alive:
    fail("常駐は生きているのにスナップショットが無い")

# ============================================================ 4. 影運転の実績

section("4. 影運転(段 2)の実績")

summary = src.shadow_summary()
totals = summary["totals"]
print("  " + src.shadow_report().replace("\n", "\n  "))

shadow_ready = True
if mode == src.OFF:
    note("mode=OFF なので影運転は記録されない(段 2 に入ってから読む欄)")
    shadow_ready = False
elif mode == src.LIVE:
    # LIVE では比較しない(答えが Gateway なので REST と並べる相手が居ない)。
    # 段 2 の件数は **段 3 へ進む前の条件**なので、進んだあとに不足として出さない。
    print("  mode=LIVE —— 影運転は終わっている。以下は段 2 の記録が残っていれば参考値")
    if totals["shadowDiverged"]:
        fail(f"影運転で食い違いが {totals['shadowDiverged']} 件記録されている —— "
             f"{src.DIVERGENCE_LOG} を読む")
else:
    if totals["cycles"] < SHADOW_MIN_CYCLES:
        note(f"周期 {totals['cycles']} / {SHADOW_MIN_CYCLES} —— まだ足りない")
        shadow_ready = False
    if totals["shadowCompared"] < SHADOW_MIN_COMPARED:
        note(f"比較の成立 {totals['shadowCompared']} / {SHADOW_MIN_COMPARED} —— まだ足りない")
        shadow_ready = False
    if totals["shadowDiverged"]:
        fail(f"食い違いが {totals['shadowDiverged']} 件ある —— "
             f"{src.DIVERGENCE_LOG} を読んでから段 3 を判断する")
        shadow_ready = False
    checked = totals["shadowChecked"] or 1
    ratio = totals["shadowUnavailable"] / checked
    if ratio > SHADOW_MAX_UNAVAILABLE_RATIO:
        note(f"比較の不成立が {ratio:.0%}(上限 {SHADOW_MAX_UNAVAILABLE_RATIO:.0%})"
             f" —— 常駐が落ちているか、設定が欠けている")
        shadow_ready = False
    if totals["cyclesWithPosition"] < SHADOW_MIN_POSITION_CYCLES:
        note(f"建玉のあった周期が {totals['cyclesWithPosition']} —— "
             f"FLAT どうしの一致だけでは段 3 の根拠にならない")
        shadow_ready = False

# ============================================================ 5. 未実装の明示

section("5. まだ実装していないもの(LIVE と表示しない)")

gateway_source = io.open(os.path.join(BASE, "broker_gateway.py"), encoding="utf-8").read()
if "executionUpdate" in gateway_source and "_fills_snapshot" not in gateway_source:
    ok("約定行(executionUpdate)は件数だけ —— `_fills_snapshot` は REST のまま"
       "(N=20 の下限が 7.33 秒でなく 14.0 秒なのはこれが理由)")
else:
    note("約定行の持ち方が変わっている。docs §7 を更新すること")

per_account = (coordinator.get("perAccountRequests") or {}).get("ENTRY") or {}
if per_account.get("gatewayApi") == 1:
    ok("ENTRY の Gateway 側 api 本数は 1(約定履歴の窓)と契約に書いてある")
else:
    note(f"perAccountRequests.ENTRY.gatewayApi={per_account.get('gatewayApi')}")

# ============================================================ 6. 次の段

section("6. 次に進んでよい段")

if mode == src.OFF and not alive:
    print("  → 段 1: `NQX_GATEWAY=1 python broker_gateway.py --start` で常駐させる")
    print("     (読み取りだけ・注文は送らない。止めるのは "
          "`python broker_gateway.py --stop`)")
elif mode == src.OFF and alive:
    print("  → 段 2: 契約 gateway.mode を \"SHADOW\" にする(判断は 1 つも変わらない)")
elif mode == src.SHADOW and not shadow_ready:
    print("  → 段 2 を続ける。上の『まだ足りない』が全部埋まるまで段 3 へ進まない")
elif mode == src.SHADOW and shadow_ready:
    print("  → 段 3: 契約 gateway.mode を \"LIVE\" にしてよい条件が揃っている")
    print("     (建玉なし・未約定注文なしのときに。戻しは NQX_GATEWAY_MODE=OFF)")
elif mode == src.LIVE and not send_verification:
    print("  → 段 4: gateway.sendVerification を true にするかの判断。**FLAT のときに**")
    print("     (押し込みを『送った証拠』に使う判断。SHADOW で差分 0 を見てから)")
elif mode == src.LIVE and send_verification and not coordinator_on:
    print("  → 段 5: gateway.coordinator.enabled を true にするかの判断。**FLAT のときに**")
else:
    print("  → 全段が入っている。実測(判定 → 全口座受理 p95 / TP1 → 保護変更)を"
          "本番の 1 トレードで取る")

print("\n" + "=" * 68)
if problems:
    print(f"矛盾 {len(problems)} 件:")
    for text in problems:
        print(f"  - {text}")
    raise SystemExit(1)
print(f"いまの構成に矛盾は無い(mode={mode} · autostart={autostart} · "
      f"sendVerification={send_verification} · coordinator={coordinator_on})")
if notes:
    print(f"進めるために足りないもの {len(notes)} 件は上の `--` 行")
raise SystemExit(0)

# -*- coding: utf-8 -*-
"""R122 の本番反映を、**発注も監視も通信もせずに**確認する(読むだけ)。

    python verify_r122_live.py

やること: 本番の起動先(`python nqx_cycle.py` が使う BASE と子プロセスの cwd/env)から
`msnr_gate` / `market_structure_context` / `participation_policy` / `execution_contract.json`
が**どのファイルに解決されるか**を出し、`structureContext` の各段が意図どおりに読めることを
確かめ、保存済みの監査バンドルとフィクスチャを通して「文脈が実際に decision と凍結プランへ
届く」ことを見る。未実装・未配線の段が LIVE と表示されないことも検査する。

やらないこと: TradingView の取得、ブローカー照会、Cloudflare、Telegram、発注、publish、
AUTO の変更、台帳への書き込み。ネットワークを使う import もしない。

終了コード: 0 = 反映済み / 1 = 反映されていない(理由を出す)。
"""
from __future__ import annotations

import ast
import copy
import glob
import hashlib
import io
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
JST = timezone(timedelta(hours=9))
problems: list[str] = []

#: 期待する本番構成。ここを変えるときは docs/R122_… §8 の根拠も一緒に更新する。
EXPECT_MODES = {"context": "LIVE", "participation": "LIVE", "shallowCandidate": "SHADOW",
                "selection": "LIVE", "nearTerm": "SHADOW"}
#: **LIVE にしてはいけない段**(未校正の予測を武装ゲートにしない)。
NO_LIVE = ("nearTerm",)


def sha(path: str) -> str:
    try:
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()[:16]
    except OSError as exc:
        return f"<{exc.strerror}>"


def stamp(path: str) -> str:
    try:
        return datetime.fromtimestamp(os.path.getmtime(path), timezone.utc).astimezone(JST) \
            .strftime("%Y-%m-%d %H:%M:%S JST")
    except OSError:
        return "-"


def fail(text: str) -> None:
    problems.append(text)
    print(f"  NG   {text}")


def ok(text: str) -> None:
    print(f"  OK   {text}")


# ---------------------------------------------------------- 1. 起動先の同一性

print("1. 本番の起動先")
print(f"   BASE(nqx_cycle.py の Path(__file__).resolve().parent) = {BASE}")
for name in ("nqx_cycle.py", "monitor_publish.py", "msnr_gate.py",
             "market_structure_context.py", "participation_policy.py",
             "execution_contract.json"):
    path = os.path.join(BASE, name)
    print(f"   {name:<30} sha256[:16]={sha(path)}  mtime={stamp(path)}")

pub_path = os.path.join(BASE, "monitor_publish.py")
pub = io.open(pub_path, encoding="utf-8").read()
tree = ast.parse(pub)
imports_gate = any(isinstance(n, ast.Import) and any(a.name == "msnr_gate" for a in n.names)
                   for n in ast.walk(tree))
if imports_gate and "sys.path.insert" in pub:
    ok("monitor_publish は sys.path.insert(0, BASE) の後に msnr_gate を import する")
else:
    fail("monitor_publish の import 経路が確認できない")

strays = []
for root, _dirs, files in os.walk(os.path.join(BASE, ".claude")):
    if "msnr_gate.py" in files:
        strays.append(os.path.join(root, "msnr_gate.py"))
if strays:
    print(f"   注意: R122 を持たない複製が {len(strays)} 本ある(git worktree)。")
    for path in strays:
        has = "structure_context_policy" in io.open(path, encoding="utf-8", errors="replace").read()
        print(f"     {'あり' if has else 'なし'}  {path}")

# ------------------------------------------------------------- 2. 契約の読み

print("\n2. execution_contract.json の structureContext")
import market_structure_context as msc  # noqa: E402
import msnr_gate  # noqa: E402
import participation_policy as pp  # noqa: E402

policy = msnr_gate.structure_context_policy()
if policy["invalid"]:
    fail(f"契約に不正な段がある: {policy['invalid']}")
else:
    ok("不正な段は無い")
for stage, expect in EXPECT_MODES.items():
    got = policy[stage]["mode"]
    line = f"{stage:18s} = {got}"
    if got == expect:
        ok(line)
    else:
        fail(line + f"(期待 {expect})")
for stage in NO_LIVE:
    if policy[stage]["mode"] == "LIVE":
        fail(f"{stage} が LIVE になっている(未校正の予測を武装ゲートにしない)")
    else:
        ok(f"{stage} は LIVE ではない")

with io.open(os.path.join(BASE, "execution_contract.json"), encoding="utf-8") as fh:
    contract = json.load(fh)
node = contract.get("structureContext") or {}
if node.get("version") == msc.VERSION and node.get("schema") == msc.SCHEMA:
    ok(f"契約の version / schema が実装と一致({msc.VERSION} / {msc.SCHEMA})")
else:
    fail(f"契約の version / schema が実装と食い違う: {node.get('version')} / {node.get('schema')}")

stdv = msnr_gate.ict_stdv_policy()
if stdv["participation"]["mode"] == "OFF":
    ok("ictStdv.participation は OFF(参加判断の入口は R122 の 1 か所)")
else:
    fail("ictStdv.participation が OFF でない(参加判断の二重入口)")

# ------------------------------------------------- 3. 未実装が LIVE に見えないか

print("\n3. 未実装 / 未配線が LIVE と表示されていないこと")
gate_src = io.open(os.path.join(BASE, "msnr_gate.py"), encoding="utf-8").read()
wiring = {
    "context": "_structure_context_for(",
    "participation": "participation_policy.evaluate(",
    "shallowCandidate": "_shallow_candidate(",
    "selection": "ELIGIBLE_PREFERRED_OVER_HIGHER_SCORE_WATCH",
    "nearTerm": "near_term(",
}
msc_src = io.open(os.path.join(BASE, "market_structure_context.py"), encoding="utf-8").read()
for stage, token in wiring.items():
    wired = token in gate_src or token in msc_src
    if policy[stage]["mode"] != "OFF" and not wired:
        fail(f"{stage} は {policy[stage]['mode']} だがコードに配線が無い({token})")
    elif wired:
        ok(f"{stage} の配線がある({token})")
    else:
        ok(f"{stage} は OFF(配線なしでも整合)")

# -------------------------------------------- 4. 保存済みバンドルで実際に効くか

print("\n4. 保存済みの監査バンドルで文脈が decision まで届くか(読むだけ)")
paths = sorted(glob.glob(os.path.join(BASE, ".secrets", "monitor_cycle_*.json")))[-40:]
seen = {"ok": 0, "unavailable": 0, "withStructure": 0, "scenario": 0, "parents": {}}
for path in paths:
    try:
        bundle = json.load(io.open(path, encoding="utf-8"))
        result = msnr_gate.evaluate(copy.deepcopy(bundle))
    except (OSError, ValueError):
        continue
    context = result.get("structureContext")
    if not isinstance(context, dict):
        continue
    if context.get("status") == "OK":
        seen["ok"] += 1
        tf = (context.get("thesis") or {}).get("timeframe")
        seen["parents"][tf] = seen["parents"].get(tf, 0) + 1
    else:
        seen["unavailable"] += 1
    decision = result.get("decision") or {}
    if decision.get("structure"):
        seen["withStructure"] += 1
        scenario = msnr_gate.decision_to_scenario(decision, bundle, qty=2)
        if scenario and scenario.get("structure"):
            seen["scenario"] += 1
if seen["ok"] + seen["unavailable"] == 0:
    fail("監査バンドルを 1 本も評価できなかった")
else:
    ok(f"直近 {seen['ok'] + seen['unavailable']} 本: 親あり {seen['ok']} / 親なし {seen['unavailable']} "
       f"(出所 {seen['parents']})")
    ok(f"decision に最小形式が載った周期 {seen['withStructure']} / "
       f"うち凍結プランまで届いた {seen['scenario']}")

# ----------------------------------------- 5. 15 分足が publish 経路で残ること

print("\n5. 15 分足(親の第一の出所)が compact で落ちないこと")
if 'for secondary in ("bars45m", "bars1h", "bars4h")' in pub and 'snapshot["bars15mCount"]' in pub:
    ok("monitor_publish.compact は bars15m を残す(bars1d と同じ扱い)")
else:
    fail("monitor_publish.compact が bars15m を落としている(親が常に HTF 要約へ落ちる)")
try:
    import monitor_publish  # noqa: E402 - import 時にネットワークへ触れない
    sample = {"at": "2026-09-19T00:00:00+00:00", "price": 30000.0,
              "snapshot": {"bars": [{"t": 1, "o": 1, "h": 1, "l": 1, "c": 1}],
                           "bars15m": [{"t": 900 * i, "o": 1, "h": 1, "l": 1, "c": 1}
                                       for i in range(24)],
                           "levels": []}}
    compacted = monitor_publish.compact(sample)
    rows = (compacted.get("snapshot") or {}).get("bars15m")
    if isinstance(rows, list) and len(rows) == 24:
        ok("compact() の実行結果にも bars15m が 24 本残る")
    else:
        fail(f"compact() の実行結果に bars15m が残らない: {type(rows).__name__}")
    if (compacted.get("snapshot") or {}).get("bars15mCount") == 24:
        ok("bars15mCount も従来どおり載る(既存の表示互換)")
    else:
        fail("bars15mCount が落ちた")
except Exception as exc:  # noqa: BLE001
    fail(f"monitor_publish.compact を実行できない: {type(exc).__name__}: {exc}")

# ------------------------------------------- 5b. 根拠の伝播(4 段すべて)

print("\n5b. 根拠(structure)の伝播: ローカル decision → 公開ペイロード → Worker 保存後 → 凍結プラン")
sample = None
for path in reversed(paths):
    try:
        bundle = json.load(io.open(path, encoding="utf-8"))
        result = msnr_gate.evaluate(copy.deepcopy(bundle))
    except (OSError, ValueError):
        continue
    decision = result.get("decision") or {}
    scenario = msnr_gate.decision_to_scenario(decision, bundle, qty=2)
    if scenario and decision.get("structure"):
        sample = (path, bundle, decision, scenario)
        break

if sample is None:
    print("   (直近 40 本に scenario を作れる decision が無い。合成 fixture で確認する)")
    decision = {"model": "BREAKER_CONTINUATION", "side": "BUY", "state": "ARMED", "grade": "A",
                "decisionId": "verify122", "entry": 30000.0, "stop": 29980.0,
                "targets": [30040.0, 30080.0], "targetR": [2.0, 4.0], "evidence": [],
                "structure": {"contextVersion": msc.VERSION, "thesisId": "verify-thesis",
                              "participationState": "ENTRY_READY", "triggerEvidenceIds": [],
                              "invalidation": "fixture", "selectionReason": "fixture",
                              "changedFromBaseline": [], "relation": "CONTINUATION",
                              "orderIntent": "MARKET", "shallow": False}}
    bundle = {"evaluation": {"decision": decision}}
    scenario = msnr_gate.decision_to_scenario(decision, bundle, qty=2)
    sample = ("<fixture>", bundle, decision, scenario)

src_path, sample_bundle, sample_decision, sample_scenario = sample
print(f"   標本: {os.path.basename(src_path)}")
minimal = sample_decision.get("structure") or {}
required = ("contextVersion", "thesisId", "participationState", "triggerEvidenceIds",
            "invalidation", "selectionReason", "changedFromBaseline")
missing = [k for k in required if k not in minimal]
if missing:
    fail(f"(1) ローカル decision.structure に不足: {missing}")
else:
    ok("(1) ローカル decision.structure に最小形式が揃っている")

if (sample_scenario or {}).get("structure"):
    ok("(2) 公開ペイロード(decision_to_scenario の出力)に structure が載る")
else:
    fail("(2) 公開ペイロードに structure が載っていない")

# (3) Worker の正規化を**実際に走らせて**落ちることを示す(node が無ければ静的検査へ落とす)
worker_keeps = None
sm = os.path.join(BASE, "cloudflare", "src", "state_machine.js")
if os.path.exists(sm):
    # 本番の publish は scenario を hash-only で送る(engine の "scenario must be hash-only")。
    # 同じ形にしてから Worker の正規化を通す。
    payload = {k: v for k, v in (sample_scenario or {}).items()
               if k not in ("strategyEvidence", "eligibleVotes")}
    now = datetime.now(timezone.utc)
    payload.update({
        "symbol": "MNQZ6", "fingerprint": "verify122",
        "issuedAt": now.isoformat(), "observedAt": now.isoformat(),
        "expiresAt": (now + timedelta(minutes=10)).isoformat(),
    })
    lines = [
        "import { validateScenario } from " + json.dumps("file:///" + sm.replace(chr(92), "/")) + ";",
        "const raw = JSON.parse(process.argv[2]);",
        "const out = validateScenario(raw, { symbol: 'MNQZ6' });",
        "process.stdout.write(JSON.stringify({ok: out.ok, reason: out.reason || null, "
        "hasStructure: !!(out.scenario && out.scenario.structure)}));",
    ]
    script = chr(10).join(lines) + chr(10)
    tmp = os.path.join(BASE, ".secrets", "_verify_r122_worker.mjs")
    try:
        os.makedirs(os.path.dirname(tmp), exist_ok=True)
        io.open(tmp, "w", encoding="utf-8", newline=chr(10)).write(script)
        proc = subprocess.run(["node", tmp, json.dumps(payload, ensure_ascii=False)],
                              capture_output=True, timeout=60)
        if proc.returncode == 0:
            answer = json.loads(proc.stdout.decode("utf-8"))
            worker_keeps = bool(answer.get("hasStructure"))
            print(f"   Worker validateScenario: ok={answer.get('ok')} "
                  f"reason={answer.get('reason')} structure={'残る' if worker_keeps else '落ちる'}")
        else:
            print("   (node で Worker を実行できなかった: "
                  + proc.stderr.decode("utf-8", "replace").strip().splitlines()[-1][:120]
                  + ")。静的検査へ落とす")
    except (OSError, ValueError, subprocess.SubprocessError):
        print("   (node が無い / 実行できない。静的検査へ落とす)")
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
if worker_keeps is None:
    worker_src = io.open(sm, encoding="utf-8").read() if os.path.exists(sm) else ""
    worker_keeps = "structure:" in worker_src.split("scenario: {", 1)[-1][:4000]
if worker_keeps:
    ok("(3) Worker は structure を保存する")
else:
    print("   INFO (3) Worker は未知キーを落とす正規化なので structure は**保存されない**"
          "(表示したい場合だけ deploy が要る)")

# (4) 執行が実際に読む凍結プラン。Worker 正規化後の scenario でも、ローカル評価カードから拾う。
try:
    import autotrade_engine
    # 本番の `reconcile` は Worker 正規化後の scenario と**ローカルの評価カード付き**
    # バンドルを受け取る。ここでも同じ形に組んでから凍結プランを作る(読むだけ)。
    local_bundle = dict(sample_bundle)
    local_bundle["evaluation"] = {"decision": {"structure": minimal,
                                               "evidence": sample_decision.get("evidence") or []}}
    stripped = {k: v for k, v in (sample_scenario or {}).items() if k != "structure"}
    stripped.setdefault("symbol", "MNQZ6")
    plan = autotrade_engine.build_management_plan(stripped, local_bundle, {})
    carried = plan.get("decisionStructure") or {}
    if carried.get("thesisId") == minimal.get("thesisId") and carried.get("participationState") is not None:
        ok("(4) **Worker が structure を落とした scenario でも**、凍結プランは "
           "ローカル評価カードから decisionStructure を持つ(執行の根拠は欠けない)")
    else:
        fail(f"(4) 凍結プランに decisionStructure が届いていない: {carried}")
    for field, want in (("entry", stripped.get("entry")), ("initialStop", stripped.get("stop")),
                        ("decisionId", sample_decision.get("decisionId"))):
        if plan.get(field) != want:
            fail(f"(4) 凍結プランの {field} が変わった: {plan.get(field)} != {want}")
except Exception as exc:  # noqa: BLE001
    fail(f"(4) 凍結プランを組めない: {type(exc).__name__}: {exc}")


# ------------------------------------------------- 6. 記憶の置き場所と分離

print("\n6. 構造の記憶(発注台帳と分離)")
state_path = os.path.join(BASE, ".secrets", "structure_context_state.json")
print(f"   {state_path}  {'あり' if os.path.exists(state_path) else '(まだ無い。初回サイクルで作られる)'}")
if "autotrade_ledger" not in io.open(os.path.join(BASE, "market_structure_context.py"),
                                     encoding="utf-8").read():
    ok("記憶モジュールは発注台帳に触れない")
else:
    fail("記憶モジュールが発注台帳を参照している")
engine = io.open(os.path.join(BASE, "autotrade_engine.py"), encoding="utf-8").read()
order = io.open(os.path.join(BASE, "order.py"), encoding="utf-8").read()
leaked = [name for name in ("market_structure_context", "participation_policy", "structureContext")
          if name in engine or name in order]
if leaked:
    fail(f"保有建玉の経路(engine / order.py)が R122 を参照している: {leaked}")
else:
    ok("保有建玉の管理(engine / order.py)は R122 を一切参照しない")

# ----------------------------------------------------------------- まとめ

print("\n" + "=" * 68)
if problems:
    print(f"反映されていない点 {len(problems)} 件:")
    for text in problems:
        print(f"  - {text}")
    raise SystemExit(1)
print("R122 は本番構成へ反映済み(context=LIVE / participation=LIVE / "
      "shallowCandidate=SHADOW / selection=LIVE / nearTerm=SHADOW)")
raise SystemExit(0)

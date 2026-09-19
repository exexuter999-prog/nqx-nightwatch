# -*- coding: utf-8 -*-
"""R119 の本番反映を、**発注も監視も通信もせずに**確認する(読むだけ)。

    python verify_r119_live.py

やること: 本番の起動先(`python nqx_cycle.py` が使う BASE と子プロセスの cwd/env)から
`msnr_gate` と `execution_contract.json` が**どのファイルに解決されるか**を出し、
`limitGate` が gapCap=1.5 / targetPassed=LIVE で読めることを確かめ、最後に**保存済みの**
監査バンドルとフィクスチャを通して門が実際に効くことを見る。

やらないこと: TradingView の取得、ブローカー照会、Cloudflare、Telegram、発注、publish、
AUTO の変更、台帳への書き込み。ネットワークを使う import もしない
(`monitor_publish` は import 時の副作用が無いが、`telegram_bot` を連れてくるので触らない。
代わりに「あのファイルの `sys.path.insert(0, BASE)` + `import msnr_gate`」を静的に確認する)。

終了コード: 0 = 反映済み / 1 = 反映されていない(理由を出す)。
"""
from __future__ import annotations

import ast
import hashlib
import io
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
JST = timezone(timedelta(hours=9))
EXPECT_GAP_R = 1.5
EXPECT_MODES = {"gapCap": "LIVE", "targetPassed": "LIVE"}
problems: list[str] = []


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
entry = os.path.join(BASE, "nqx_cycle.py")
print(f"   BASE(nqx_cycle.py の Path(__file__).resolve().parent) = {BASE}")
for name in ("nqx_cycle.py", "monitor_publish.py", "msnr_gate.py", "autotrade_engine.py",
             "execution_contract.json", "monitor_config.json"):
    path = os.path.join(BASE, name)
    print(f"   {name:<26} sha256[:16]={sha(path)}  mtime={stamp(path)}")

# 子プロセスの起動条件が「この BASE を cwd に、sys.executable で」であること。
src = io.open(entry, encoding="utf-8").read()
if 'cwd=str(BASE)' in src and "[sys.executable, *args]" in src:
    ok("nqx_cycle は子プロセスを cwd=BASE / sys.executable で起動する(別ツリーを参照しない)")
else:
    fail("nqx_cycle の子プロセス起動条件が読めない(cwd=BASE / sys.executable)")

# monitor_publish が自分のディレクトリを先頭に入れて msnr_gate を import すること。
pub = io.open(os.path.join(BASE, "monitor_publish.py"), encoding="utf-8").read()
tree = ast.parse(pub)
inserts = [n for n in ast.walk(tree)
           if isinstance(n, ast.Call) and getattr(getattr(n.func, "value", None), "attr", "") == "path"
           and getattr(n.func, "attr", "") == "insert"]
imports_gate = any(isinstance(n, ast.Import) and any(a.name == "msnr_gate" for a in n.names)
                   for n in ast.walk(tree))
if inserts and imports_gate:
    ok("monitor_publish は sys.path.insert(0, BASE) の後に msnr_gate を import する")
else:
    fail("monitor_publish の import 経路が確認できない")

# 紛れ込みうる古い複製(git worktree)を明示する。
strays = []
for root, dirs, files in os.walk(os.path.join(BASE, ".claude")):
    if "msnr_gate.py" in files:
        strays.append(os.path.join(root, "msnr_gate.py"))
if strays:
    print(f"   注意: R119 を持たない複製が {len(strays)} 本ある(git worktree)。"
          "ループをそこから起動すると門は OFF になる:")
    for path in strays:
        has = "limit_gate_policy" in io.open(path, encoding="utf-8", errors="replace").read()
        print(f"     {'R119 有' if has else 'R119 無'}  {os.path.relpath(path, BASE)}")

# ------------------------------------------- 2. 子プロセスと同じ条件で解決を見る

print("\n2. 子プロセスと同じ条件(cwd=BASE / PYTHONUTF8=1)での解決と契約")
probe = (
    "import json, os, sys, hashlib\n"
    "import msnr_gate, execution_contract\n"
    "out = {\n"
    "  'msnr_gate_file': msnr_gate.__file__,\n"
    "  'contract_path': msnr_gate.CONTRACT_PATH,\n"
    "  'contract_file_used_by_execution_contract': getattr(execution_contract, 'CONTRACT_PATH', None),\n"
    "  'policy': msnr_gate.limit_gate_policy(),\n"
    "  'LIMIT_MAX_GAP_R': msnr_gate.LIMIT_MAX_GAP_R,\n"
    "  'has_gate_fn': [hasattr(msnr_gate, n) for n in "
    "      ('limit_gate_policy','limit_gate_audit','limit_entry_side','gate_price')],\n"
    "  'sys_path_head': sys.path[:3],\n"
    "  'cwd': os.getcwd(),\n"
    "  'env_overrides': {k: v for k, v in os.environ.items() if 'LIMIT' in k.upper() or 'GAP' in k.upper()},\n"
    "}\n"
    "print('@@' + json.dumps(out, default=str))\n"
)
child_env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
proc = subprocess.run([sys.executable, "-c", probe], cwd=BASE, env=child_env,
                      capture_output=True, text=True)
line = next((l for l in proc.stdout.splitlines() if l.startswith("@@")), None)
if not line:
    fail(f"子プロセスの検査が失敗した: {proc.stderr.strip()[:300]}")
    info = {}
else:
    info = json.loads(line[2:])
    print(f"   cwd            = {info['cwd']}")
    print(f"   msnr_gate      = {info['msnr_gate_file']}")
    print(f"   CONTRACT_PATH  = {info['contract_path']}")
    if os.path.normcase(os.path.dirname(info["msnr_gate_file"])) == os.path.normcase(BASE):
        ok("msnr_gate は本番ディレクトリのものが読まれている")
    else:
        fail(f"msnr_gate が別の場所から読まれている: {info['msnr_gate_file']}")
    if os.path.normcase(info["contract_path"]) == os.path.normcase(
            os.path.join(BASE, "execution_contract.json")):
        ok("契約は本番ディレクトリの execution_contract.json が読まれている")
    else:
        fail(f"契約が別の場所から読まれている: {info['contract_path']}")
    if all(info["has_gate_fn"]):
        ok("R119 の関数(limit_gate_policy / limit_gate_audit / limit_entry_side / gate_price)がある")
    else:
        fail("R119 の関数が無い = 変更前のコードが読まれている")
    policy = info["policy"]
    print(f"   limitGate      = {json.dumps(policy, ensure_ascii=False)}")
    if policy.get("invalid"):
        fail(f"契約の limitGate に不正な節がある: {policy['invalid']}")
    else:
        ok("limitGate に不正な節が無い(不正な節は黙って OFF になるので必ず見る)")
    gap = policy.get("gapCap") or {}
    if gap.get("mode") == EXPECT_MODES["gapCap"] and gap.get("maxGapR") == EXPECT_GAP_R:
        ok(f"gapCap = {gap['mode']} / maxGapR = {gap['maxGapR']}")
    else:
        fail(f"gapCap が期待と違う: {gap}")
    passed = policy.get("targetPassed") or {}
    if passed.get("mode") == EXPECT_MODES["targetPassed"]:
        ok(f"targetPassed = {passed['mode']}")
    else:
        fail(f"targetPassed が期待と違う: {passed}")
    if info["LIMIT_MAX_GAP_R"] == EXPECT_GAP_R:
        ok(f"msnr_gate.LIMIT_MAX_GAP_R = {info['LIMIT_MAX_GAP_R']}(契約と一致)")
    else:
        fail(f"LIMIT_MAX_GAP_R が契約とずれている: {info['LIMIT_MAX_GAP_R']}")
    if info["env_overrides"]:
        fail(f"環境変数で上書きされている疑い: {info['env_overrides']}")
    else:
        ok("limitGate を上書きする環境変数は無い(この節に env の抜け道は作っていない)")

# ------------------------- 3. 保存済みの入力で門が実際に効くか(通信なし)

print("\n3. 保存済みの入力で門が効くか(ファイルだけ。取得も照会もしない)")
check = (
    "import copy, json, io, os, sys\n"
    "from datetime import datetime\n"
    "import msnr_gate, stop_logic\n"
    "rows = []\n"
    "# (a) 2026-09-19 01:44 の実バンドル: BUY 29,699.5 / 現値 29,713.25 = gapR 0.34 → 通る\n"
    "p = os.path.join('.secrets', 'monitor_cycle_0146.json')\n"
    "if os.path.exists(p):\n"
    "    b = json.load(io.open(p, encoding='utf-8'))\n"
    "    r = msnr_gate.evaluate(copy.deepcopy(b))\n"
    "    for c in r.get('candidates') or []:\n"
    "        rows.append(('audit 0146', c.get('model'), c.get('side'), c.get('entry'),\n"
    "                     c.get('state'), (c.get('limitGate') or {}), c.get('hardBlockers')))\n"
    "# (b) R103 の T4 フィクスチャ: SELL 29,426 / 現値 29,388.75 = gapR 2.01 → 止まる\n"
    "cp = os.path.join('tests', 'fixtures', 'r103', 'cases_2026-09-15.json')\n"
    "bp = os.path.join('tests', 'fixtures', 'r103', 'bars3m_2026-09-15.json')\n"
    "if os.path.exists(cp) and os.path.exists(bp):\n"
    "    cases = {c['tag']: c for c in json.load(io.open(cp, encoding='utf-8'))['cases']}\n"
    "    bars = json.load(io.open(bp, encoding='utf-8'))\n"
    "    bars = bars['bars'] if isinstance(bars, dict) else bars\n"
    "    case = cases['T4']; ts = datetime.fromisoformat(case['armedAt']).timestamp()\n"
    "    rowsb = [x for x in bars if x['t'] + 180 <= ts][-int(case['closedBarsAtArm']):]\n"
    "    b = {'at': case['armedAt'], 'priceAt': case['armedAt'], 'price': case['priceAtArm'],\n"
    "         'snapshot': {'levels': copy.deepcopy(case['levels']), 'bars3m': copy.deepcopy(rowsb)}}\n"
    "    r = msnr_gate.evaluate(b)\n"
    "    for c in r.get('candidates') or []:\n"
    "        rows.append(('r103 T4', c.get('model'), c.get('side'), c.get('entry'),\n"
    "                     c.get('state'), (c.get('limitGate') or {}), c.get('hardBlockers')))\n"
    "print('@@' + json.dumps(rows, default=str))\n"
)
proc = subprocess.run([sys.executable, "-c", check], cwd=BASE, env=child_env,
                      capture_output=True, text=True)
line = next((l for l in proc.stdout.splitlines() if l.startswith("@@")), None)
if not line:
    fail(f"保存済み入力での検査が失敗した: {proc.stderr.strip()[:300]}")
else:
    rows = json.loads(line[2:])
    saw_pass = saw_block = False
    for src_name, model, side, entry, state, gate, blockers in rows:
        gap_r = gate.get("gapR")
        print(f"   [{src_name}] {str(model)[:22]:<22}{str(side):<5} E={entry} state={state} "
              f"type={gate.get('orderType')} gapR={gap_r} tp1R={gate.get('tp1R')} "
              f"blockers={gate.get('blockers')}")
        if gap_r is not None and gate.get("orderType") == "LIMIT":
            if float(gap_r) <= EXPECT_GAP_R and not (gate.get("blockers") or []):
                saw_pass = True
            if float(gap_r) > EXPECT_GAP_R and "LIMIT_GAP_EXCEEDED" in (gate.get("blockers") or []):
                saw_block = True and state == "WATCH"
    if saw_pass:
        ok("gapR <= 1.5 の指値候補はそのまま通っている")
    else:
        fail("gapR <= 1.5 の指値候補が確認できない(門が過剰に効いている可能性)")
    if saw_block:
        ok("gapR > 1.5 の指値候補は LIMIT_GAP_EXCEEDED + WATCH になっている")
    else:
        fail("gapR > 1.5 の指値候補が止まっていない(門が効いていない)")

# ------------------------------------------------------------------ まとめ

print("\n4. 触っていないもの(この検査の範囲)")
for text in ("TradingView の取得", "ブローカー照会", "Cloudflare / Worker", "Telegram 送信",
             "publish", "発注 / dry-run 送信", "AUTO の状態", "台帳への書き込み"):
    print(f"   -  {text}: 触らない")

print()
if problems:
    print(f"反映されていない可能性: {len(problems)} 件")
    for text in problems:
        print(f"  - {text}")
    raise SystemExit(1)
print("R119 は本番の起動先に反映済み(gapCap=LIVE/1.5, targetPassed=LIVE)。"
      "市場再開後の稼働確認は docs/R119_LIMIT_GATE.md §7 を見る。")

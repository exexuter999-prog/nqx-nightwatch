# -*- coding: utf-8 -*-
"""Python producer と JS 側スキーマ/署名の整合を検証する。

ネットワークも Cloudflare も使わない。node を子プロセスで呼び、
`cloudflare/src/` の本物の検証関数に Python の出力を通す。

    python tests/test_state_bridge.py
"""
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

for _stream in ("stdout", "stderr"):
    _file = getattr(sys, _stream, None)
    if _file is not None and hasattr(_file, "reconfigure"):
        try:
            _file.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

import monitor_publish  # noqa: E402
import nqx_state        # noqa: E402
import strategy_evidence  # noqa: E402

PASS = [0]
FAIL = [0]


def check(label, condition, detail=""):
    if condition:
        PASS[0] += 1
        print(f"  OK   {label}")
    else:
        FAIL[0] += 1
        print(f"  NG   {label}  {detail}")


def node_eval(script):
    """cloudflare/src の本物のモジュールを使って評価する。"""
    result = subprocess.run(
        [_node(), "--input-type=module", "-e", script],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=os.path.join(BASE, "cloudflare"), timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip()[:400])
    return json.loads(result.stdout.strip())


def _node():
    return "node"


# ================================================================
print("=" * 68)
print("1. Python が組んだ scenario を JS の検証器が受理するか")
print("=" * 68)

now = datetime.now(timezone.utc)
scenario = nqx_state.build_scenario(
    {"side": "BUY", "entry": 29906.5, "stop": 29871.5, "target": 29922.75,
     "targets": [29922.75, 29940.0],
     "legs": [{"id": "TP1", "qty": 1, "target": 29922.75},
              {"id": "RUNNER", "qty": 1, "target": 29940.0}],
     "planVersion": "R17-SPLIT-1", "qty": 2, "title": "C: VAH hold", "state": "ACTIVE"},
    observed_at=now.isoformat(), ttl_minutes=10, symbol="MNQU6",
)

for field in ("scenarioId", "fingerprint", "issuedAt", "observedAt", "expiresAt", "symbol", "state"):
    check(f"必須項目 {field} が入っている", scenario.get(field))

verdict = node_eval(
    "import {validateScenario} from './src/state_machine.js';"
    f"const raw = {json.dumps(scenario)};"
    "const r = validateScenario(raw, {symbol: 'MNQU6'});"
    "console.log(JSON.stringify({ok: r.ok, reason: r.reason ?? null, rr: r.scenario?.rr ?? null}));"
)
check("JS の validateScenario が受理する", verdict["ok"], str(verdict.get("reason")))

# 逆向きは Python 側で組めても JS で必ず落ちる(二重の防波堤)
bad = dict(scenario, stop=29950.0)
verdict = node_eval(
    "import {validateScenario} from './src/state_machine.js';"
    f"const r = validateScenario({json.dumps(bad)}, {{symbol: 'MNQU6'}});"
    "console.log(JSON.stringify({ok: r.ok, reason: r.reason ?? null}));"
)
check("逆向き SL は JS 側でも拒否される", not verdict["ok"], str(verdict.get("reason")))

verdict = node_eval(
    "import {validateScenario} from './src/state_machine.js';"
    f"const r = validateScenario({json.dumps(dict(scenario, symbol='MESU6'))}, {{symbol: 'MNQU6'}});"
    "console.log(JSON.stringify({ok: r.ok}));"
)
check("別 symbol は JS 側でも拒否される", not verdict["ok"])

print()
print("=" * 68)
print("2. 署名の Python ↔ JS 一致")
print("=" * 68)

# Python producer -> Cloudflare validator/state projection -> Mini App chart
# normalization.  These fields are display/decision observability, but must
# remain bounded and intact across the three local contracts.
matrix_bundle = {
    "at": now.isoformat(), "priceAt": now.isoformat(), "priceSource": "TradingView quote_get",
    "sourceSymbol": "CME_MINI:MNQ1!", "price": 29906.5,
    "snapshot": {"symbol": "CME_MINI:MNQ1!", "resolution": "3",
                 "bars3m": [{"t": 1786550000, "o": 29900, "h": 29908, "l": 29898, "c": 29904},
                            {"t": 1786550180, "o": 29904, "h": 29910, "l": 29902, "c": 29906.5}],
                 "levels": []},
    "strategyMatrix": {
        "version": "IMAGE_STRATEGY_CATALOG/1",
        "alignment": {"BUY": 2, "SELL": 0, "primary": "BUY"},
        "activeModels": ["ifvg", "blocks", "fibSd", "sessions"],
        "models": {
            "ifvg": {"status": "CONFIRMED", "valid": True, "direction": "BUY", "zones": [{"lo": 29904, "hi": 29907, "active": True}]},
            "blocks": {"status": "CONFIRMED", "valid": True, "activeKinds": ["OB", "BRK"]},
            "fibSd": {"status": "ALIGNED", "valid": True, "levels": [29904, 29908]},
            "sessions": {"status": "CONFIRMED", "valid": True, "session": "NY", "asianHigh": 29910},
        },
    },
}
matrix_bundle["strategyEvidence"] = strategy_evidence.from_matrix(
    matrix_bundle.pop("strategyMatrix"), as_of=now.isoformat(), session_id="NY-TEST",
    source="test-producer", provenance="fixture")
matrix_market = monitor_publish.build_market_payload(matrix_bundle, now=now)
mini_chart = "file:///" + os.path.join(BASE, "telegram_mini_app", "chart.js").replace("\\", "/")
matrix_view = node_eval(
    "import {emptyState, applyEvent, projectState, validateMarket} from './src/state_machine.js';"
    f"import {{normalizeMarketSnapshot}} from {json.dumps(mini_chart)};"
    f"const raw = {json.dumps(matrix_market)}; const now = Date.parse(raw.observedAt);"
    "const checked = validateMarket(raw, now); let s = emptyState('bridge', 'MNQU6');"
    "s = applyEvent(s, {stream: 'market', revision: 1, payload: {market: raw}}, now).state;"
    "const market = projectState(s, now).market; const mini = normalizeMarketSnapshot(market, now);"
    "console.log(JSON.stringify({ok: checked.ok, version: mini?.strategyMatrix?.version,"
    " primary: mini?.strategyMatrix?.alignment?.primary, active: mini?.strategyMatrix?.activeModels,"
    " ifvg: mini?.strategyMatrix?.models?.ifvg?.zones?.[0]?.hi,"
    " blocks: mini?.strategyMatrix?.models?.blocks?.activeKinds,"
    " fibSd: mini?.strategyMatrix?.models?.fibSd?.levels, session: mini?.strategyMatrix?.models?.sessions?.session}));"
)
check("Python -> Cloudflare -> Mini App strategy matrix stays intact", matrix_view["ok"] and
      matrix_view["version"] == "IMAGE_STRATEGY_CATALOG/1" and matrix_view["primary"] == "BUY" and
      matrix_view["active"] == ["ifvg", "blocks", "fibSd", "sessions"] and matrix_view["ifvg"] == 29907 and
      matrix_view["blocks"] == ["OB", "BRK"] and matrix_view["fibSd"] == [29904, 29908] and
      matrix_view["session"] == "NY", str(matrix_view))

versioned = nqx_state.build_scenario({
    "side": "BUY", "qty": 2, "entry": 29906.5, "stop": 29871.5, "target": 29922.75,
    "targets": [29922.75, 29940.0],
    "legs": [{"id": "TP1", "qty": 1, "target": 29922.75},
             {"id": "RUNNER", "qty": 1, "target": 29940.0}],
    "planVersion": "R17-SPLIT-1",
    "grade": "A", "strategyEvidence": matrix_bundle["strategyEvidence"],
    "eligibleVotes": {"BUY": ["ifvg", "blocks"], "SELL": []},
    "evidenceHash": matrix_bundle["strategyEvidence"]["evidenceHash"],
    "setupVersion": "R14-ICT-SETUP/1", "catalogVersion": "IMAGE_STRATEGY_CATALOG/1",
    "detectorVersion": "REPO2-DETECTOR/1", "executionContractVersion": "R22-EXECUTION-CONTRACT-1",
}, now.isoformat(), symbol="MNQU6", state="ARMED")
versioned_view = node_eval(
    "import {validateScenario} from './src/state_machine.js';"
    f"const r = validateScenario({json.dumps(versioned)}, {{symbol: 'MNQU6', pairedEvidence: {json.dumps(matrix_bundle['strategyEvidence'])}}});"
    "console.log(JSON.stringify({ok: r.ok, versions: r.scenario && [r.scenario.setupVersion, r.scenario.catalogVersion, r.scenario.detectorVersion, r.scenario.executionContractVersion],"
    " hash: r.scenario?.evidenceHash, votes: r.scenario?.eligibleVotes?.BUY}));"
)
check("versioned scenario is hash-only and validates against paired market evidence", "strategyEvidence" not in versioned and versioned_view["ok"] and
      versioned_view["versions"] == ["R14-ICT-SETUP/1", "IMAGE_STRATEGY_CATALOG/1", "REPO2-DETECTOR/1", "R22-EXECUTION-CONTRACT-1"] and
      versioned_view["hash"] == matrix_bundle["strategyEvidence"]["evidenceHash"] and
      versioned_view["votes"] == ["ifvg", "blocks"], str(versioned_view))

body = json.dumps({"stream": "market", "revision": 1}, separators=(",", ":"))
import hashlib  # noqa: E402
body_hash = hashlib.sha256(body.encode()).hexdigest()
py_sig = nqx_state._sign("bridge-secret", nqx_state.signing_string("1786500000", "n" * 16, "acct", body_hash))
js = node_eval(
    "import {hmacHex, sha256Hex, publishSigningString} from './src/auth.js';"
    f"const body = {json.dumps(body)};"
    "const h = await sha256Hex(body);"
    "console.log(JSON.stringify({hash: h, sig: await hmacHex('bridge-secret', publishSigningString('1786500000', 'n'.repeat(16), 'acct', h))}));"
)
check("body hash が一致する", js["hash"] == body_hash, f"{js['hash']} != {body_hash}")
check("publish 署名が一致する", js["sig"] == py_sig, f"{js['sig']} != {py_sig}")

token = nqx_state.issue_launch_token("4242", ttl_seconds=3600, cfg={"NQX_LAUNCH_SECRET": "bridge-launch"})
js = node_eval(
    "import {verifyLaunchToken} from './src/auth.js';"
    f"const r = await verifyLaunchToken({json.dumps(token)}, 'bridge-launch', {{nowMs: Date.now()}});"
    "console.log(JSON.stringify({ok: r.ok, userId: r.userId ?? null, reason: r.reason ?? null}));"
)
check("Python が発行した launch token を JS が検証できる", js["ok"] and js["userId"] == "4242", str(js))

js = node_eval(
    "import {verifyLaunchToken} from './src/auth.js';"
    f"const r = await verifyLaunchToken({json.dumps(token)}, 'wrong-secret', {{nowMs: Date.now()}});"
    "console.log(JSON.stringify({ok: r.ok}));"
)
check("別 secret では検証に通らない", not js["ok"])

print()
print("=" * 68)
print("3. revision の単調増加")
print("=" * 68)

original_revision_file = nqx_state.REVISION_FILE
original_lock = nqx_state.LOCK_FILE
tmp = tempfile.mkdtemp(prefix="nqx-rev-")
nqx_state.REVISION_FILE = os.path.join(tmp, "rev.json")
nqx_state.LOCK_FILE = nqx_state.REVISION_FILE + ".lock"
try:
    seq = [nqx_state.next_revision("scenario") for _ in range(50)]
    check("同一 stream の revision は厳密に増加する", all(b > a for a, b in zip(seq, seq[1:])), str(seq[:5]))

    scenario_rev = nqx_state.next_revision("scenario")
    position_rev = nqx_state.next_revision("position")
    scenario_rev2 = nqx_state.next_revision("scenario")
    check("scenario と position の revision は独立している",
          scenario_rev2 > scenario_rev and position_rev > 0)

    # 記録ファイルが消えても時刻由来の下限で巻き戻らない
    highest = max(seq)
    os.unlink(nqx_state.REVISION_FILE)
    check("記録が消えても revision は巻き戻らない", nqx_state.next_revision("scenario") > highest)
finally:
    nqx_state.REVISION_FILE = original_revision_file
    nqx_state.LOCK_FILE = original_lock

print()
print("=" * 68)
print("4. monitor_publish のシナリオ選択")
print("=" * 68)

key, chosen, dropped = monitor_publish.select_active_scenario({})
check("シナリオが無ければ None を返す", chosen is None and dropped == [])

key, chosen, dropped = monitor_publish.select_active_scenario({
    "long_watch": {"state": "WATCH", "side": "BUY", "entry": 1, "stop": 0, "target": 2},
    "short_armed": {"state": "ACTIVE", "side": "SELL", "entry": 1, "stop": 2, "target": 0},
})
check("ACTIVE が WATCH より優先される", key == "short_armed", str(key))
check("表示しなかったシナリオを報告する", dropped == ["long_watch"], str(dropped))

# This test covers the explicit no-Cloudflare path.  Keep it hermetic even
# after a developer has configured a real Durable Object locally; otherwise a
# unit test would publish its fixture price (29,900) into production state.
_real_load_cloud_env = nqx_state.load_cloud_env
try:
    def _unconfigured(*_args, **_kwargs):
        raise nqx_state.NotConfigured("test fixture: Cloudflare disabled")

    nqx_state.load_cloud_env = _unconfigured
    ok, notes = monitor_publish.publish_state({"at": now.isoformat(), "price": 29900, "scenarios": {}})
finally:
    nqx_state.load_cloud_env = _real_load_cloud_env

check("Cloudflare 未設定なら publish は False を返す(成功と偽らない)", ok is False, str(notes))
check("未設定であることが理由として残る", any("not configured" in note for note in notes), str(notes))

print()
print("=" * 68)
print("5. ブローカー照会の fail-closed")
print("=" * 68)

import broker_status  # noqa: E402

# このテストは「アダプタ未設定」の fail-closed 経路を検証する。§4 の
# Cloudflare と同様に、env ファイルの参照先ごと空の一時領域へ差し替える。
# 本物の .secrets を読ませると実 REST へ照会が飛び、テストが本番の
# 口座状態と疎通に依存してしまう(2026-08-15 に実際に発生)。
_no_adapter_dir = tempfile.mkdtemp(prefix="nqx-noadapter-")
_real_env_paths = (broker_status.CROSSTRADE_ENV, broker_status.TRADOVATE_ENV)
try:
    broker_status.CROSSTRADE_ENV = os.path.join(_no_adapter_dir, "crosstrade.env")
    broker_status.TRADOVATE_ENV = os.path.join(_no_adapter_dir, "tradovate.env")
    result = broker_status.query_position("MNQU6")
finally:
    broker_status.CROSSTRADE_ENV, broker_status.TRADOVATE_ENV = _real_env_paths

check("アダプタ未設定なら verified=False", result["verified"] is False, str(result))
check("建玉が無いとは言わない(qty を返さない)", "qty" not in result, str(result))
check("理由が残る", bool(result.get("detail")), str(result))

hint = broker_status.order_log_hint()
check("order_log は正本ではないと明示している", hint["authoritative"] is False)


print()
print("=" * 68)
print("6. 決済結果 — path と exit の出所")
print("=" * 68)

BAR_START = 1786550000
bars = [{"t": BAR_START + i * 180, "o": 29750 + i, "h": 29755 + i,
         "l": 29745 + i, "c": 29752 + i, "v": 1000} for i in range(20)]
opened = datetime.fromtimestamp(BAR_START + 3 * 180, timezone.utc).isoformat()
closed = datetime.fromtimestamp(BAR_START + 9 * 180, timezone.utc).isoformat()

path_values, source = nqx_state.build_path(bars, opened, closed, 29760.0, 29796.0)
check("保有区間の実バーだけを使う", source == "observed-bars", source)
check("区間外のバーを含めない", len(path_values) == 2 + 7, f"{len(path_values)} 点")
check("両端は entry / exit", path_values[0] == 29760.0 and path_values[-1] == 29796.0, str(path_values[:2]))
check("中身は実バーの終値", path_values[1:-1] == [29752 + i for i in range(3, 10)], str(path_values[1:-1]))

empty_path, empty_source = nqx_state.build_path([], opened, closed, 29760.0, 29796.0)
check("観測が無ければ両端 2 点だけ", empty_path == [29760.0, 29796.0], str(empty_path))
check("その事実を pathSource で明示する", empty_source == "endpoints-only", empty_source)

outside, outside_source = nqx_state.build_path(
    [{"t": BAR_START - 9999, "c": 1}, {"t": BAR_START + 99999, "c": 2}],
    opened, closed, 29760.0, 29796.0)
check("区間外しか無い場合も値動きを作らない", outside == [29760.0, 29796.0] and outside_source == "endpoints-only",
      str(outside))

print()
print("=" * 68)
print("7. 決済結果 — 導出と検証")
print("=" * 68)

check("コスト帯の中は勝ちにしない", nqx_state.derive_state("LONG", 29760, 29761.5) == "flat")
check("コスト帯を超えたら勝ち", nqx_state.derive_state("LONG", 29760, 29764) == "win")
check("SHORT は方向が反転する", nqx_state.derive_state("SHORT", 29760, 29740) == "win")
check("SHORT の逆行は負け", nqx_state.derive_state("SHORT", 29760, 29790) == "loss")

check("TP 到達を判別する", nqx_state.classify_exit("LONG", 29760, 29796, 29726, 29796) == "target")
check("SL 到達を判別する", nqx_state.classify_exit("LONG", 29760, 29726, 29726, 29796) == "stop")
check("どちらでもなければ manual", nqx_state.classify_exit("LONG", 29760, 29775, 29726, 29796) == "manual")

v1 = nqx_state.pick_verdict("rs-abc", "win", "target")
check("同じ結果には同じ文言", v1 == nqx_state.pick_verdict("rs-abc", "win", "target"))
variants = {nqx_state.pick_verdict(f"rs-{i}", "win", "target") for i in range(30)}
check("結果が変われば文言も変わりうる", len(variants) > 1, str(variants))

closed_position = {
    "side": "LONG", "symbol": "MNQU6", "avgEntry": 29760.0, "stop": 29726.0, "target": 29796.0,
    "closedQty": 2, "initialQty": 2, "filledAt": opened, "closedAt": closed, "receipt": "HTTP 200",
}
result = nqx_state.build_result(closed_position, 29796.0, "broker", bars=bars, symbol="MNQU6")
check("resultId は同じトレードで安定する",
      result["resultId"] == nqx_state.build_result(closed_position, 29796.0, "broker", bars=bars,
                                                   symbol="MNQU6")["resultId"])
check("導出値を payload に載せない",
      not any(k in result for k in ("state", "pnl", "realisedR", "held", "usd")), str(result.keys()))

try:
    nqx_state.build_result(closed_position, 29796.0, "estimated", bars=bars, symbol="MNQU6")
    check("推定した決済価格は拒否される", False, "例外が出なかった")
except ValueError:
    check("推定した決済価格は拒否される", True)

for missing in ("avgEntry", "closedAt", "side"):
    broken = dict(closed_position)
    broken.pop(missing)
    try:
        nqx_state.build_result(broken, 29796.0, "broker", bars=bars, symbol="MNQU6")
        check(f"{missing} が無ければ結果を作らない", False, "例外が出なかった")
    except (ValueError, TypeError):
        check(f"{missing} が無ければ結果を作らない", True)

# R56: stop は broker 由来(約定から組んだ記録・凍結プランの無い手動建玉)だけ省略できる。
# R を出せないだけで損益は事実。manual(手入力)は従来どおり必須。
no_stop = dict(closed_position)
no_stop.pop("stop")
broker_no_stop = nqx_state.build_result(no_stop, 29796.0, "broker", bars=bars, symbol="MNQU6")
check("broker 由来は stop 無しでも結果を作り、stop は None", broker_no_stop["stop"] is None)
try:
    nqx_state.build_result(no_stop, 29796.0, "manual", bars=bars, symbol="MNQU6")
    check("manual は stop が無ければ結果を作らない", False, "例外が出なかった")
except (ValueError, TypeError):
    check("manual は stop が無ければ結果を作らない", True)

verdict = node_eval(
    "import {validateResult} from './src/state_machine.js';"
    f"const r = validateResult({json.dumps(result)}, {{symbol: 'MNQU6'}});"
    "console.log(JSON.stringify({ok: r.ok, reason: r.reason ?? null, points: r.result?.pathPoints ?? null}));"
)
check("Python が組んだ result を JS の検証器が受理する", verdict["ok"], str(verdict.get("reason")))
check("path の点数がサーバー側にも残る", verdict["points"] == len(result["path"]), str(verdict))

print()
print("=" * 68)
print("8. ブローカーの往復から決済価格を出す")
print("=" * 68)

realised = broker_status.realised_prices(
    {"bought": 2, "boughtValue": 59520.0, "sold": 2, "soldValue": 59592.0, "prevPos": 2})
check("LONG の決済は売り平均", realised.get("avgExit") == 29796.0, str(realised))
check("LONG の建値は買い平均", realised.get("avgEntry") == 29760.0, str(realised))

realised_short = broker_status.realised_prices(
    {"bought": 2, "boughtValue": 59452.0, "sold": 2, "soldValue": 59520.0, "prevPos": -2})
check("SHORT は建値と決済が入れ替わる",
      realised_short.get("avgEntry") == 29760.0 and realised_short.get("avgExit") == 29726.0,
      str(realised_short))

check("往復が揃っていなければ何も返さない",
      broker_status.realised_prices({"bought": 2, "boughtValue": 59520.0}) == {})
check("方向が分からなければ avgExit を作らない",
      "avgExit" not in broker_status.realised_prices(
          {"bought": 2, "boughtValue": 59520.0, "sold": 2, "soldValue": 59592.0}))

print()
print("=" * 68)
print("9. 口座残機(LIFELINE)の Python ↔ JS 整合")
print("=" * 68)

# env から組んだ payload を JS の検証器がそのまま受理するか。
_lifeline_env = {
    "CROSSTRADE_ACCOUNTS": "LFE02562316710002,LFE02562316710003,LFE02562316710004",
    "MAX_RISK_DOLLARS": "60",
    "RISK_LFE02562316710002": "60",
    "RISK_LFE02562316710003": "60",
    "RISK_LFE02562316710004": "50",
    "LIFELINE_LFE02562316710002": "816",
    "LIFELINE_LFE02562316710003": "407",
    "LIFELINE_LFE02562316710004": "249",
}
accounts_payload = nqx_state.build_accounts_payload(env=_lifeline_env)
check("env から accounts payload を組める", accounts_payload is not None)
check("口座数が CROSSTRADE_ACCOUNTS と一致する", len(accounts_payload["list"]) == 3)
check("RISK_* が cap に反映される", accounts_payload["list"][2]["cap"] == 50.0)

js = node_eval(
    "import {validateAccounts} from './src/state_machine.js';"
    f"const r = validateAccounts({json.dumps(accounts_payload)});"
    "console.log(JSON.stringify({ok: r.ok, reason: r.reason ?? null,"
    " total: r.accounts?.totalBuffer ?? null, label: r.accounts?.list?.[0]?.label ?? null}));"
)
check("JS の validateAccounts が受理する", js["ok"], str(js.get("reason")))
check("合計残機を JS 側が導出する", js.get("total") == 1472, str(js.get("total")))
check("口座ラベルは末尾4桁でマスクされる", js.get("label") == "…0002", str(js.get("label")))

check("LIFELINE_* が無ければ payload を作らない(機能未設定)",
      nqx_state.build_accounts_payload(env={"CROSSTRADE_ACCOUNTS": "A,B"}) is None)

# STREAMS の回帰: result / account の採番が ValueError で落ちない
# (2026-08-15 まで "result" が漏れており、publish_result が本番で必ず失敗していた)。
for _stream in ("result", "account"):
    try:
        nqx_state.next_revision(_stream)
        check(f"next_revision('{_stream}') が採番できる", True)
    except ValueError as exc:
        check(f"next_revision('{_stream}') が採番できる", False, str(exc))

print()
print("=" * 68)
print("10. 残存注文の終端化(resolve_stuck_order・HANDOFF §11 ②)")
print("=" * 68)

# 送信後の注文を FILLED/CANCELED に進める唯一の経路。ネットワークは使わず、
# state の読み取りと publish をスタブして純粋なロジックだけを検証する。
check("BLOCKING_ORDER_STATES が JS 側と一致する",
      node_eval(
          "import {BLOCKING_ORDER_STATES} from './src/state_machine.js';"
          "console.log(JSON.stringify([...BLOCKING_ORDER_STATES].sort()));"
      ) == sorted(nqx_state.BLOCKING_ORDER_STATES))

_published_orders = []
_real_fetch = nqx_state.fetch_state_quiet
_real_publish_order = nqx_state.publish_order
try:
    stuck_view = {"order": {
        "idempotencyKey": "nonce-bridge-test-sent-0001", "scenarioId": "sc-1",
        "state": "SENT", "side": "BUY", "qty": 1, "entry": 29760.0,
        "stop": 29726.0, "target": 29796.0, "receipt": "HTTP 200",
        "detail": "sent", "at": now.isoformat(), "revision": 3}}
    nqx_state.fetch_state_quiet = lambda cfg=None: stuck_view
    nqx_state.publish_order = lambda order, cfg=None: (
        _published_orders.append(order) or (True, {"stored": True}))

    ok, detail = nqx_state.resolve_stuck_order("FILLED", detail="position open confirmed")
    check("SENT を FILLED に進める",
          ok and _published_orders and _published_orders[-1]["state"] == "FILLED", str(detail))
    check("idempotencyKey を引き継ぐ",
          _published_orders and _published_orders[-1]["idempotencyKey"] == "nonce-bridge-test-sent-0001")

    verdict = node_eval(
        "import {validateOrder} from './src/state_machine.js';"
        f"const r = validateOrder({json.dumps(_published_orders[-1])});"
        "console.log(JSON.stringify({ok: r.ok, reason: r.reason ?? null, state: r.order?.state ?? null}));"
    )
    check("終端化 payload を JS の validateOrder が受理する", verdict["ok"], str(verdict.get("reason")))
    check("JS 側でも FILLED として解釈される", verdict.get("state") == "FILLED")

    _published_orders.clear()
    nqx_state.fetch_state_quiet = lambda cfg=None: {"order": dict(stuck_view["order"], state="FILLED")}
    ok, detail = nqx_state.resolve_stuck_order("CANCELED")
    check("終端済みの注文には publish しない",
          not ok and not _published_orders and "skipped" in detail, str(detail))

    nqx_state.fetch_state_quiet = lambda cfg=None: None
    ok, detail = nqx_state.resolve_stuck_order("CANCELED")
    check("state を読めなければ publish しない", not ok and not _published_orders, str(detail))

    try:
        nqx_state.resolve_stuck_order("SENT")
        check("非終端状態への遷移は拒否される", False, "例外が出なかった")
    except ValueError:
        check("非終端状態への遷移は拒否される", True)
finally:
    nqx_state.fetch_state_quiet = _real_fetch
    nqx_state.publish_order = _real_publish_order

# バグの生涯そのものを JS 側で再現する: SENT が position CLOSED 後に
# ORDER_PENDING として再浮上し、終端化 publish で解除される。
lifecycle = node_eval(
    "import {emptyState, applyEvent, projectState} from './src/state_machine.js';"
    "const now = Date.parse('2026-08-16T12:00:00Z');"
    "let s = emptyState('acct', 'MNQU6');"
    "const order = {idempotencyKey: 'k'.repeat(20), state: 'SENT', side: 'BUY', qty: 1,"
    "  entry: 29760, stop: 29726, target: 29796, at: new Date(now).toISOString()};"
    "s = applyEvent(s, {stream: 'order', revision: 1, payload: {order}}, now).state;"
    "const open = {verified: true, source: 't', symbol: 'MNQU6', side: 'LONG', qty: 1,"
    "  avgEntry: 29760, observedAt: new Date(now).toISOString()};"
    "s = applyEvent(s, {stream: 'position', revision: 1, payload: {position: open}}, now).state;"
    "const flat = {verified: true, source: 't', symbol: 'MNQU6', qty: 0,"
    "  observedAt: new Date(now + 60000).toISOString()};"
    "s = applyEvent(s, {stream: 'position', revision: 2,"
    "  payload: {position: flat, closedAt: new Date(now + 60000).toISOString()}}, now + 60000).state;"
    "const blocked = projectState(s, now + 120000).display;"
    "s = applyEvent(s, {stream: 'order', revision: 2,"
    "  payload: {order: {...order, state: 'FILLED', at: new Date(now + 120000).toISOString()}}}, now + 120000).state;"
    "const released = projectState(s, now + 180000).display;"
    "console.log(JSON.stringify({blockedPriority: blocked.priority, blockedOrderable: blocked.orderable,"
    "  releasedPriority: released.priority}));"
)
check("決済後に SENT が ORDER_PENDING として再浮上する(バグの再現)",
      lifecycle["blockedPriority"] == "ORDER_PENDING" and lifecycle["blockedOrderable"] is False,
      str(lifecycle))
check("終端化 publish で ORDER_PENDING が解除される",
      lifecycle["releasedPriority"] != "ORDER_PENDING", str(lifecycle))

print()
print("=" * 68)
print(f"合計: PASS {PASS[0]} / FAIL {FAIL[0]}")
print("=" * 68)
sys.exit(1 if FAIL[0] else 0)

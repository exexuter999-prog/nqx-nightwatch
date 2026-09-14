# -*- coding: utf-8 -*-
"""R57: Obsidian 保管庫への 1 トレード 1 ノート書き出し。

スコアカード行 + 凍結プランから frontmatter を正規化し、冪等に再生成しても
`<!-- nqx:auto-end -->` より下の人のメモを保持する。口座 ID はマスクする。

    python tests/test_obsidian_export.py
"""
import json
import os
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
import obsidian_export as ox  # noqa: E402

PASS = [0]
FAIL = [0]


def check(label, condition, detail=""):
    if condition:
        PASS[0] += 1
        print(f"  OK   {label}")
    else:
        FAIL[0] += 1
        print(f"  NG   {label}  {detail}")


def write_jsonl(path, rows):
    with open(path, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


SCORE = [
    {"resultId": "rs_aaaaaa111111", "closedAt": "2026-09-04T19:55:00.112000+00:00",
     "model": "UNATTRIBUTED", "grade": "A+", "scenarioId": "scn1", "accountId": "LFE00000000000024",
     "mode": "LIVE", "side": "SHORT", "qty": 20, "entry": 29570.5, "exit": 29582.75,
     "stop": 29580.25, "pointValue": 2.0, "fees": None, "pnlGross": -490.0, "pnlNet": -490.0,
     "rMultiple": -1.256, "outcome": "LOSS", "evidenceTags": ["CVD_ALIGNED", "VP_ACCEPTED"]},
    {"resultId": "rs_bbbbbb222222", "closedAt": "2026-09-04T19:26:58.738000+00:00",
     "model": "UNATTRIBUTED", "grade": None, "scenarioId": None, "accountId": "LFE00000000000024",
     "mode": "LIVE", "side": "SHORT", "qty": 6, "entry": 29523.5, "exit": 29521.75, "stop": None,
     "pointValue": 2.0, "fees": None, "pnlGross": 21.0, "pnlNet": 21.0, "rMultiple": None,
     "outcome": "FLAT", "evidenceTags": []},
]
LEDGER = [
    {"time": "2026-09-04T19:51:27+00:00", "status": "ENTRY_CLAIMED", "plan": {
        "scenarioId": "scn1", "model": None, "grade": "A+", "entryKey": "ENTRY:x",
        "entryOrderType": "MARKET", "entryReference": 29569.25, "initialStop": 29580.25,
        "tp1": 29535.0, "finalTarget": 29350.75, "qty": 20, "riskDollars": 440.0,
        "riskCapSource": "ACCOUNT_DRAWDOWN_BUFFER", "decisionEvidence": ["CVD_ALIGNED"],
        "accountScope": ["LFE00000000000024"],
        "legs": [{"id": "TP1", "qty": 10, "target": 29535.0}, {"id": "RUNNER", "qty": 10, "target": 29350.75}]}},
]

with tempfile.TemporaryDirectory() as tmp:
    vault = os.path.join(tmp, "vault")
    os.makedirs(vault)
    score = os.path.join(tmp, "score.jsonl")
    ledger = os.path.join(tmp, "ledger.jsonl")
    write_jsonl(score, SCORE)
    write_jsonl(ledger, LEDGER)
    audit_dir = os.path.join(tmp, "audit")
    os.makedirs(audit_dir)
    with open(os.path.join(audit_dir, "monitor_cycle_0451.json"), "w", encoding="utf-8") as fh:
        json.dump({"at": "2026-09-04T20:00:00+00:00",
                   "scenarios": {"primary": {"scenarioId": "scn1", "model": "VP80_REVERSION", "grade": "A+"}},
                   "snapshot": {"levels": [{"label": "C: VAH", "price": 29566.7}, {"label": "P: POC", "price": 29526.43}]}}, fh)
    audit = os.path.join(audit_dir, "monitor_cycle_*.json")

    summary = ox.export(vault, scorecard_path=score, ledger_path=ledger, audit_pattern=audit, charts=False)
    check("2 件作成・5 モデル雛形", len(summary["created"]) == 2 and len(summary["modelNotes"]) == 5, str(summary))
    names = sorted(os.listdir(os.path.join(vault, "Trades")))
    check("ファイル名は JST 日時 + 方向 + 枚数 + resultId 末尾",
          names == ["2026-09-05 0426 SHORT 6 222222.md", "2026-09-05 0455 SHORT 20 111111.md"], str(names))

    auto_path = os.path.join(vault, "Trades", "2026-09-05 0455 SHORT 20 111111.md")
    text = open(auto_path, encoding="utf-8").read()
    check("model は監査バンドルから後追い(UNATTRIBUTED を上書き)",
          "model: VP80_REVERSION" in text and "model_source: audit" in text)
    check("経路 auto / MARKET / ULTRA / 滑り",
          "route: auto" in text and "entry_type: MARKET" in text and "ultra: true" in text
          and "slippage_pt: 1.25" in text, text[:600])
    check("口座 ID はマスク", "LFE…0024" in text and "LFE00000000000024" not in text)
    check("損益・R・結果", "pnl_net: -490.0" in text and "r_multiple: -1.256" in text and "outcome: LOSS" in text)
    tags_line = next((l for l in text.splitlines() if l.startswith("tags: ")), "")
    check("tags に model/outcome/route/grade/exit", all(x in tags_line for x in ("model/VP80_REVERSION", "outcome/LOSS", "route/auto", "grade/Aplus", "exit/SL")), tags_line)
    check("決済の種類と滑りを frontmatter に", "exit_kind: SL" in text and "exit_slippage_pt: 2.5" in text, text[:900])
    check("振り返り雛形が本文に入る", "## 振り返り" in text and "プロセス評価" in text)
    check("evidence 配列", "evidence: [CVD_ALIGNED, VP_ACCEPTED]" in text)

    manual_path = os.path.join(vault, "Trades", "2026-09-05 0426 SHORT 6 222222.md")
    mtext = open(manual_path, encoding="utf-8").read()
    check("プランの無い決済は manual / UNATTRIBUTED", "route: manual" in mtext and "model: UNATTRIBUTED" in mtext)

    # 人のメモを書き足してから再生成 → メモ保持・冪等
    with open(auto_path, "a", encoding="utf-8") as fh:
        fh.write("\n- 前セッション VAH の 5pt 下で狩られた\n")
    again = ox.export(vault, scorecard_path=score, ledger_path=ledger, audit_pattern=audit, charts=False)
    text2 = open(auto_path, encoding="utf-8").read()
    check("再生成でメモを保持", "前セッション VAH の 5pt 下" in text2 and text2.count(ox.AUTO_END) == 1)
    check("内容が同じなら unchanged", len(again["created"]) == 0, str(again))
    third = ox.export(vault, scorecard_path=score, ledger_path=ledger, audit_pattern=audit, charts=False)
    check("3 回目は完全に unchanged", len(third["updated"]) == 0 and len(third["unchanged"]) == 2, str(third))

    # 人の評価キー(process_grade / mistakes)は再生成で引き継ぐ
    graded = open(auto_path, encoding="utf-8").read().replace("process_grade: null", "process_grade: B", 1)
    graded = graded.replace("mistakes: null", "mistakes: [mistake/再エントリー]", 1)
    with open(auto_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(graded)
    ox.export(vault, scorecard_path=score, ledger_path=ledger, audit_pattern=audit, charts=False)
    regraded = open(auto_path, encoding="utf-8").read()
    check("process_grade / mistakes を再生成後も保持",
          "process_grade: B" in regraded and "mistakes: [mistake/再エントリー]" in regraded, regraded[:1200])

    model_note = open(os.path.join(vault, "Models", "VP80_REVERSION.md"), encoding="utf-8").read()
    check("モデル雛形に Bases 埋め込み", "![[Bases/Trades.base#VP80_REVERSION]]" in model_note)
    dry = ox.export(os.path.join(tmp, "empty"), scorecard_path=score, ledger_path=ledger,
                    audit_pattern=audit, dry_run=True, charts=False)

    # チャート復元: 生の 3 分足(建玉 45 分前〜決済 15 分後)から SVG を作り、ノートに埋め込む
    raw = os.path.join(tmp, "bars3m.json")
    base_t = 1788551700 - 60 * 60   # 04:55 JST の 60 分前から 3 分刻みで 30 本
    bars = [{"time": base_t + i * 180, "open": 29560 + i, "high": 29565 + i, "low": 29555 + i,
             "close": 29562 + i, "volume": 100} for i in range(30)]
    with open(raw, "w", encoding="utf-8") as fh:
        json.dump({"success": True, "bars": bars}, fh)
    chart_vault = os.path.join(tmp, "vault2")
    os.makedirs(chart_vault)
    res = ox.export(chart_vault, scorecard_path=score, ledger_path=ledger, audit_pattern=audit,
                    raw_bars_path=raw)
    svg_path = os.path.join(chart_vault, "attachments", "trades", "2026-09-05 0455 SHORT 20 111111.svg")
    check("チャート SVG が生成される", os.path.exists(svg_path) and any("111111.svg (raw" in c for c in res["charts"]), str(res["charts"]))
    svg = open(svg_path, encoding="utf-8").read()
    check("SVG に建値・SL・TP1・IN/OUT・水準", all(k in svg for k in ("ENTRY 29,570.5", "SL 29,580.25", "TP1 29,535", "IN 04:51", "OUT 04:55", "C: VAH")), svg[:300])
    note = open(os.path.join(chart_vault, "Trades", "2026-09-05 0455 SHORT 20 111111.md"), encoding="utf-8").read()
    check("ノートにチャートを埋め込む", "![[attachments/trades/2026-09-05 0455 SHORT 20 111111.svg]]" in note)
    again = ox.export(chart_vault, scorecard_path=score, ledger_path=ledger, audit_pattern=audit, raw_bars_path=raw)
    check("2 回目はチャートを描き直さず、ノートも unchanged", again["charts"] == [] and len(again["unchanged"]) == 2, str(again))
    forced = ox.export(chart_vault, scorecard_path=score, ledger_path=ledger, audit_pattern=audit,
                       raw_bars_path=raw, force_charts=True)
    check("--force-charts で描き直す", len(forced["charts"]) == 2, str(forced["charts"]))
    missing = os.path.join(tmp, "nobars.json")
    res_nb = ox.export(os.path.join(tmp, "vault3"), scorecard_path=score, ledger_path=ledger,
                       audit_pattern=os.path.join(tmp, "none_*.json"), raw_bars_path=missing)
    nb_svg = open(os.path.join(tmp, "vault3", "attachments", "trades", "2026-09-05 0455 SHORT 20 111111.svg"), encoding="utf-8").read()
    check("足が無ければ『chart unavailable』の SVG(落ちない)", "chart unavailable" in nb_svg and len(res_nb["created"]) == 2)
    check("dry-run は書かない", not os.path.exists(os.path.join(tmp, "empty", "Trades")) and len(dry["created"]) == 2)

check("mask_account", ox.mask_account("LFE00000000000024") == "LFE…0024" and ox.mask_account("ACC") == "ACC")

# scenarioId の無い決済(fills 経由)は、直前 60 分の同方向・同枚数プランが 1 つだけなら属性を引く
with tempfile.TemporaryDirectory() as tmp:
    ledger = os.path.join(tmp, "ledger.jsonl")
    write_jsonl(ledger, [
        {"time": "2026-09-04T19:27:30+00:00", "status": "ENTRY_CLAIMED", "plan": {
            "scenarioId": "scn28", "model": "BREAKER_CONTINUATION", "grade": "A+", "side": "SELL", "qty": 28,
            "entryOrderType": "MARKET", "entryReference": 29526.75, "accountScope": ["LFE00000000000024"]}},
        {"time": "2026-09-04T14:57:00+00:00", "status": "ENTRY_CLAIMED", "plan": {
            "scenarioId": "scn6", "model": "VP80_REVERSION", "grade": "A", "side": "SELL", "qty": 6,
            "entryOrderType": "LIMIT", "accountScope": ["LFE00000000000024"]}},
    ])
    index = ox.plan_index(ledger)
    row28 = {"resultId": "rs_28", "closedAt": "2026-09-04T19:34:04+00:00", "side": "SHORT", "qty": 28,
             "entry": 29529.5, "exit": 29538.75, "pnlNet": -518.0, "outcome": "LOSS", "model": "UNATTRIBUTED"}
    t28 = ox.build_trade(row28, index, {})
    check("60 分以内の同方向・同枚数プランへ寄せる", t28["model"] == "BREAKER_CONTINUATION"
          and t28["modelSource"] == "ledger-time" and t28["scenarioId"] == "scn28" and t28["route"] == "auto", str(t28))
    row6 = {"resultId": "rs_6", "closedAt": "2026-09-04T19:26:58+00:00", "side": "SHORT", "qty": 6,
            "entry": 29523.5, "exit": 29521.75, "pnlNet": 21.0, "outcome": "FLAT", "model": "UNATTRIBUTED"}
    t6 = ox.build_trade(row6, index, {})
    check("窓の外(4.5 時間前)のプランには寄せない → manual/UNATTRIBUTED",
          t6["model"] == "UNATTRIBUTED" and t6["route"] == "manual", str(t6))
    row_dup = dict(row28, resultId="rs_dup")
    index2 = dict(index)
    index2["scn28b"] = dict(index["scn28"], scenarioId="scn28b", claimedAt="2026-09-04T19:00:00+00:00")
    check("候補が 2 つなら曖昧として寄せない", ox.build_trade(row_dup, index2, {})["model"] == "UNATTRIBUTED")

print()
print(f"合計: PASS {PASS[0]} / FAIL {FAIL[0]}")
sys.exit(1 if FAIL[0] else 0)

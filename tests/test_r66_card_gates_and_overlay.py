# -*- coding: utf-8 -*-
"""R66: 評価カードの htf / entry 機械生成と、建玉ストリームへの凍結プラン重ね合わせ。

固定するのは:
  * build_card は bundle を渡されたときだけ htf / entry を出し、渡さない出力は不変
  * htf.aligned は decision の side と 15分 CT_TREND の符号一致だけで決まる
  * entry.pass は decision 自身の武装可否の写し(新しい判定を作らない)
  * position_plan_overlay は凍結プランの SL/TP を重ね、MANAGEMENT_SENT の sl を優先する
  * 向き不一致 / 建値乖離 / 台帳なし / FLAT では何も重ねない(推測で水準を作らない)

ネットワークもブローカーも使わない。
    python tests/test_r66_card_gates_and_overlay.py
"""
import json
import os
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

for _stream in ("stdout", "stderr"):
    _file = getattr(sys, _stream, None)
    if _file is not None and hasattr(_file, "reconfigure"):
        try:
            _file.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

import msnr_gate   # noqa: E402
import nqx_state   # noqa: E402

PASS = [0]
FAIL = [0]


def check(label, condition, detail=""):
    if condition:
        PASS[0] += 1
        print(f"  OK   {label}")
    else:
        FAIL[0] += 1
        print(f"  NG   {label}  {detail}")


# ---------------------------------------------------------------- htf / entry

BUNDLE = {"price": 29598.75,
          "snapshot": {"study15m": {"NQX_DATA_CT_TREND": -1.0, "NQX_DATA_CT_ATR": 26.77}}}
DECISION_BUY = {"model": "TURTLE_SOUP_REVERSAL", "side": "BUY", "state": "WATCH", "grade": "A",
                "entry": 29584.5, "stop": 29574.0, "hardBlockers": ["ANCHOR_CONSUMED"]}

print("\n[htf]")
h = msnr_gate.htf_gate_from_bundle(BUNDLE, DECISION_BUY)
check("15分 CT_TREND の符号と ATR を写す", h == {"ctTrend": -1, "aligned": False, "ctAtr": 26.77}, str(h))
h_sell = msnr_gate.htf_gate_from_bundle(BUNDLE, dict(DECISION_BUY, side="SELL"))
check("SELL と −1 は aligned", h_sell["aligned"] is True, str(h_sell))
h_flat = msnr_gate.htf_gate_from_bundle(BUNDLE, {"model": "FLAT", "side": "FLAT"})
check("FLAT は aligned=False(アプリ側が未判定として描く)", h_flat["aligned"] is False, str(h_flat))
check("15分 study が無ければ None(0 で埋めない)",
      msnr_gate.htf_gate_from_bundle({"snapshot": {}}, DECISION_BUY) is None)
h_atr = msnr_gate.htf_gate_from_bundle({"ctAtr15": 30.5, "snapshot": {"study15m": {}}}, DECISION_BUY)
check("ATR だけなら ctTrend=0 で ATR を出す", h_atr == {"ctTrend": 0, "aligned": False, "ctAtr": 30.5}, str(h_atr))

print("\n[entry]")
e = msnr_gate.entry_gate_from_decision(DECISION_BUY, 29598.75, {"price": 29586.78})
check("structure は MSNR レベル価格", e["structure"] == 29586.78, str(e))
check("airPt/airPct は建値と structure の隙間(2.28pt = リスク 10.5pt の 22%)",
      e["airPt"] == 2.28 and e["airPct"] == 22, str(e))
check("reachR は |現値−建値|/リスク", e["reachR"] == 1.36, str(e))
check("hardBlockers があれば pass=False", e["pass"] is False, str(e))
e_armed = msnr_gate.entry_gate_from_decision(dict(DECISION_BUY, state="ARMED", hardBlockers=[]), 29590.0, None)
check("ARMED かつ blockers 無しで pass=True、MSNR 無しなら structure=stop",
      e_armed["pass"] is True and e_armed["structure"] == 29574.0 and e_armed["airPct"] == 100, str(e_armed))
check("建値/SL の無い FLAT では None",
      msnr_gate.entry_gate_from_decision({"model": "FLAT", "side": "FLAT"}, 29590.0) is None)
check("現値が無ければ reachR を出さない",
      "reachR" not in msnr_gate.entry_gate_from_decision(DECISION_BUY, None))

print("\n[build_card]")
result = {"at": "2026-09-08T00:00:00+09:00", "noiseFloor": 8.0, "levels": [],
          "rotation": {"signals": 0, "negations": 0, "verdict": "OK"},
          "decision": DECISION_BUY, "summary": ""}
plain = msnr_gate.build_card(dict(result), price=29598.75)
with_bundle = msnr_gate.build_card(dict(result), price=29598.75, bundle=BUNDLE)
check("bundle 無しの出力に htf / entry は無い(既存呼び出し不変)",
      "htf" not in plain and "entry" not in plain, str(sorted(plain)))
check("bundle 有りで htf / entry が付く",
      with_bundle.get("htf") == {"ctTrend": -1, "aligned": False, "ctAtr": 26.77}
      and with_bundle.get("entry", {}).get("reachR") == 1.36, str(with_bundle.get("entry")))
check("4096 バイト以内", msnr_gate._card_bytes(with_bundle) <= msnr_gate.CARD_MAX_BYTES)

# ---------------------------------------------------------------- overlay

ENTRY = 29600.0
PLAN = {"symbol": "MNQU6", "side": "SELL", "qty": 2, "entry": ENTRY, "initialStop": ENTRY + 24.0,
        "tp1": ENTRY - 26.0, "finalTarget": ENTRY - 52.0, "targets": [ENTRY - 26.0, ENTRY - 52.0],
        "scenarioId": "s1", "fingerprint": "f1", "evidenceHash": "e1", "marketCycleId": "c1",
        "legs": [{"id": "TP1", "qty": 1, "target": ENTRY - 26.0}, {"id": "RUNNER", "qty": 1, "target": ENTRY - 52.0}]}
POSITION = {"verified": True, "symbol": "MNQU6", "side": "SHORT", "qty": 2, "avgEntry": ENTRY,
            "source": "test", "observedAt": "2026-09-08T00:00:00+00:00"}


def ledger(rows):
    handle = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8")
    for row in rows:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    handle.close()
    return handle.name


import autotrade_engine  # noqa: E402
PLAN_KEY = autotrade_engine._plan_entry_key(PLAN)

print("\n[position_plan_overlay]")
path = ledger([{"key": "k1", "status": "ENTRY_SENT", "plan": PLAN}])
ov = nqx_state.position_plan_overlay(POSITION, ledger_path=path)
check("凍結プランの initialStop / finalTarget を重ねる",
      ov == {"stop": ENTRY + 24.0, "target": ENTRY - 52.0}, str(ov))

path = ledger([{"key": "k1", "status": "ENTRY_SENT", "plan": PLAN},
               {"key": "k2", "status": "MANAGEMENT_SENT", "planEntryKey": PLAN_KEY,
                "action": {"action": "MODIFY", "qty": 1, "sl": ENTRY - 1.0}, "plan": PLAN}])
ov = nqx_state.position_plan_overlay(dict(POSITION, qty=1, initialQty=2), ledger_path=path)
check("TP1 後の MANAGEMENT_SENT の sl を優先する", ov == {"stop": ENTRY - 1.0, "target": ENTRY - 52.0}, str(ov))

path = ledger([{"key": "k1", "status": "ENTRY_SENT", "plan": PLAN},
               {"key": "k2", "status": "MANAGEMENT_SENT", "planEntryKey": "ENTRY:other",
                "action": {"action": "MODIFY", "qty": 1, "sl": ENTRY - 9.0}, "plan": PLAN}])
ov = nqx_state.position_plan_overlay(POSITION, ledger_path=path)
check("別プランの MANAGEMENT_SENT は無視する", ov["stop"] == ENTRY + 24.0, str(ov))

path = ledger([{"key": "k1", "status": "ENTRY_SENT", "plan": PLAN}])
check("向きがプランと違えば重ねない(手動建玉)",
      nqx_state.position_plan_overlay(dict(POSITION, side="LONG"), ledger_path=path) == {})
check("建値がプランから許容(max(5pt, リスク半分)=12pt)を超えて離れていれば重ねない",
      nqx_state.position_plan_overlay(dict(POSITION, avgEntry=ENTRY + 30.0), ledger_path=path) == {})
check("許容内の滑りは重ねる",
      nqx_state.position_plan_overlay(dict(POSITION, avgEntry=ENTRY + 3.25), ledger_path=path) != {})
check("シンボル違いは重ねない",
      nqx_state.position_plan_overlay(dict(POSITION, symbol="MESU6"), ledger_path=path) == {})
check("FLAT / UNVERIFIED は重ねない",
      nqx_state.position_plan_overlay(dict(POSITION, qty=0), ledger_path=path) == {}
      and nqx_state.position_plan_overlay(dict(POSITION, verified=False), ledger_path=path) == {})
check("ブローカー行に既に SL があれば上書きしない",
      nqx_state.position_plan_overlay(dict(POSITION, stop=ENTRY + 20.0), ledger_path=path) == {"target": ENTRY - 52.0})
path = ledger([{"key": "k1", "status": "ENTRY_SENT", "plan": PLAN},
               {"key": "k1", "status": "ENTRY_TERMINAL", "plan": PLAN}])
check("ENTRY_TERMINAL 後は凍結プラン無し → 重ねない",
      nqx_state.position_plan_overlay(POSITION, ledger_path=path) == {})
check("台帳が無ければ空", nqx_state.position_plan_overlay(POSITION, ledger_path=os.path.join(tempfile.gettempdir(), "nope.jsonl")) == {})

print(f"\n{'ALL PASS' if not FAIL[0] else 'FAILED'}: {PASS[0]} ok / {FAIL[0]} ng")
sys.exit(1 if FAIL[0] else 0)

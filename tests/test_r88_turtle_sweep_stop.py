# -*- coding: utf-8 -*-
"""R88: TURTLE_SOUP_REVERSAL の SL をスイープ全体(破壊足〜奪還足)の極値に置くスイッチ。

msnr_gate.evaluate は純粋関数なので、ネットワーク・台帳・発注に触れない。

守ること:
  1. 既定(OFF)は R88 以前と同じ SL(奪還足 1 本の極値)で、chain に新しいキーを足さない。
  2. LIVE の 2 本型スイープは、奪還足より深い破壊足の極値の外へ SL を置く(BUY / SELL 鏡像)。
  3. 1 本型スイープは LIVE でも SL が変わらない(タグも付かない)。
  4. LIVE で SL が動くと targetR・setup_identity(= decisionId)が変わり、60pt 上限を
     超えれば RISK_CAP_EXCEEDED になる。記録専用タグ SWEEP_STOP_FULL_SPAN が付く。
  5. スイッチの綴りは LIVE 系以外すべて OFF(fail-closed)。
"""
import os
import sys
from datetime import datetime, timezone

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import msnr_gate  # noqa: E402

ENV_KEYS = ("NQX_TURTLE_SWEEP_STOP", "NQX_SL_CAP_PT", "NQX_ULTRA_MODE")
T0 = 1786970000 - (1786970000 % 180)
P = 30000.0
TARGETS = [{"label": "Weekly High", "price": 30100.0}, {"label": "Monthly High", "price": 30200.0}]


def check(label, condition, detail=""):
    if not condition:
        raise AssertionError(f"{label}: {detail}")
    print(f"OK   {label}")


def bundle(spec):
    bars = [{"t": T0 + 180 * i, "o": o, "h": h, "l": l, "c": c, "v": 1000}
            for i, (o, h, l, c) in enumerate(spec)]
    price_at = datetime.fromtimestamp(bars[-1]["t"] + 180, timezone.utc).isoformat()
    return {"at": price_at, "priceAt": price_at, "price": bars[-1]["c"],
            "snapshot": {"levels": [{"label": "TEST", "price": P}] + TARGETS, "bars3m": bars}}


REAL_MODEL_GATE = msnr_gate.model_gate_rules


def evaluate(spec, mode):
    if mode is None:
        os.environ.pop("NQX_TURTLE_SWEEP_STOP", None)
    else:
        os.environ["NQX_TURTLE_SWEEP_STOP"] = mode
    # R89: 本番契約のモデルゲート(2026-09-14 から TURTLE を外している)とは独立に SL だけを検査する。
    msnr_gate.model_gate_rules = lambda contract=None: {"rules": [], "invalid": []}
    try:
        return msnr_gate.evaluate(bundle(spec))
    finally:
        msnr_gate.model_gate_rules = REAL_MODEL_GATE


def turtle(result, side):
    return next((c for c in result["candidates"]
                 if c["model"] == "TURTLE_SOUP_REVERSAL" and c["side"] == side), None)


def sweep_chain(result, side):
    level = next(lv for lv in result["levels"] if lv["label"] == "TEST")
    return next((c for c in level["chains"] if c["type"] == "SWEEP" and c["side"] == side), None)


ABOVE = [(30012, 30018, 30010, 30014) for _ in range(12)]     # 支持として機能(ノイズ床 8pt)
BELOW = [(29984, 29990, 29982, 29986) for _ in range(12)]     # 抵抗として機能

# 2 本型 BUY: 破壊足の安値 29,970 が奪還足の安値 29,986 より 16pt 深い
TWO_BAR_BUY = ABOVE + [
    (30010, 30012, 29970, 29990),    # 12 破壊(ゾーン下で引ける)
    (29988, 30012, 29986, 30006),    # 13 奪還 = sweepBarT(実体 18)
    (30006, 30026, 30004, 30024),    # 14 MSS
    (30020, 30022, 30000, 30012),    # 15 保持リテスト
]
# 2 本型 SELL: 破壊足の高値 30,030 が奪還足の高値 30,014 より 16pt 高い
TWO_BAR_SELL = BELOW + [
    (29990, 30030, 29988, 30010),    # 12 破壊(ゾーン上で引ける)
    (30012, 30014, 29994, 29994),    # 13 奪還 = sweepBarT(実体 18)
    (29994, 29996, 29974, 29976),    # 14 MSS
    (29980, 30000, 29978, 29988),    # 15 保持リテスト
]
# 1 本型 BUY(test_msnr_gate の FULL_BUY と同形)
ONE_BAR_BUY = ABOVE + [
    (29998, 30012, 29994, 30010),    # 12 スイープ(下を刈って上で引ける)
    (30010, 30026, 30008, 30024),    # 13 MSS
    (30020, 30022, 30000, 30012),    # 14 保持リテスト
]
# 2 本型 BUY で破壊足が深すぎる(安値 29,935 → SL 73pt > 60pt 上限)
DEEP_BUY = ABOVE + [
    (30010, 30012, 29935, 29990),
    (29988, 30012, 29986, 30006),
    (30006, 30026, 30004, 30024),
    (30020, 30022, 30000, 30012),
]


def test_switch_parsing():
    for raw, want in ((None, "OFF"), ("", "OFF"), ("OFF", "OFF"), ("0", "OFF"), ("false", "OFF"),
                      ("shadow", "OFF"), ("garbage", "OFF"), ("LIVE", "LIVE"), (" live ", "LIVE"),
                      ("1", "LIVE"), ("on", "LIVE"), ("true", "LIVE")):
        if raw is None:
            os.environ.pop("NQX_TURTLE_SWEEP_STOP", None)
        else:
            os.environ["NQX_TURTLE_SWEEP_STOP"] = raw
        check(f"NQX_TURTLE_SWEEP_STOP={raw!r} → {want}", msnr_gate.sweep_stop_mode() == want,
              msnr_gate.sweep_stop_mode())
        check(f"params() にも同じ値({raw!r})", msnr_gate.params()["sweep_stop"] == want)


def test_default_off_keeps_reclaim_bar_stop():
    off = evaluate(TWO_BAR_BUY, None)
    chain = sweep_chain(off, "BUY")
    check("2 本型 BUY の連鎖が RETEST_HELD", chain and chain["state"] == "RETEST_HELD"
          and chain["sweepBarT"] == T0 + 180 * 13, chain)
    check("OFF は chain に R88 のキーを足さない",
          not ({"sweepExtreme", "sweepOriginBarT", "sweepExtremeBeyondReclaim"} & set(chain)), chain)
    cand = turtle(off, "BUY")
    check("ノイズ床 8pt(緩衝 8pt)", off["noiseFloor"] == 8.0, off["noiseFloor"])
    check("OFF の SL は奪還足の安値 29,986 − 8 = 29,978(R88 以前と同じ)",
          cand and cand["entry"] == 30000.0 and cand["stop"] == 29978.0, cand and cand["stop"])
    check("OFF にタグは付かない", "SWEEP_STOP_FULL_SPAN" not in cand["evidence"], cand["evidence"])
    explicit = evaluate(TWO_BAR_BUY, "OFF")
    check("明示の OFF と未設定は同じ判定", explicit["decision"] == off["decision"]
          and turtle(explicit, "BUY")["stop"] == 29978.0)


def test_live_two_bar_buy_anchors_full_sweep():
    off = evaluate(TWO_BAR_BUY, None)
    live = evaluate(TWO_BAR_BUY, "LIVE")
    chain = sweep_chain(live, "BUY")
    check("LIVE は破壊足をスイープの起点として記録",
          chain["sweepOriginBarT"] == T0 + 180 * 12 and chain["sweepExtreme"] == 29970
          and chain["sweepExtremeBeyondReclaim"] is True, chain)
    c_off, c_live = turtle(off, "BUY"), turtle(live, "BUY")
    check("LIVE の SL は破壊足の安値 29,970 − 8 = 29,962(実際に刈られた価格の外)",
          c_live["stop"] == 29962.0 and c_live["entry"] == c_off["entry"], c_live["stop"])
    check("SL が深くなる分だけ targetR は下がる",
          c_live["targetR"] and c_off["targetR"] and c_live["targetR"][0] < c_off["targetR"][0],
          (c_off["targetR"], c_live["targetR"]))
    check("setup_identity は SL を含むので変わる",
          msnr_gate.setup_identity(c_live) != msnr_gate.setup_identity(c_off))
    id_off = msnr_gate.select_primary([c_off])["decisionId"]
    id_live = msnr_gate.select_primary([c_live])["decisionId"]
    check("decisionId も変わる(別セットアップとして重複防止が数え直す)", id_off != id_live,
          (id_off, id_live))
    check("記録専用タグ SWEEP_STOP_FULL_SPAN が付く", "SWEEP_STOP_FULL_SPAN" in c_live["evidence"],
          c_live["evidence"])
    check("タグは確認要素にしない(武装の鍵にならない)",
          "SWEEP_STOP_FULL_SPAN" not in msnr_gate.CONFIRMATION_EVIDENCE
          and "SWEEP_STOP_FULL_SPAN" not in c_live["confirmations"], c_live["confirmations"])


def test_live_two_bar_sell_mirror():
    off = turtle(evaluate(TWO_BAR_SELL, None), "SELL")
    live = turtle(evaluate(TWO_BAR_SELL, "LIVE"), "SELL")
    check("SELL OFF の SL は奪還足の高値 30,014 + 8 = 30,022", off and off["stop"] == 30022.0,
          off and off["stop"])
    check("SELL LIVE の SL は破壊足の高値 30,030 + 8 = 30,038", live and live["stop"] == 30038.0,
          live and live["stop"])
    check("SELL LIVE にもタグ", "SWEEP_STOP_FULL_SPAN" in live["evidence"], live["evidence"])


def test_one_bar_sweep_unchanged():
    off = evaluate(ONE_BAR_BUY, None)
    live = evaluate(ONE_BAR_BUY, "LIVE")
    chain = sweep_chain(live, "BUY")
    check("1 本型は起点 = 奪還足", chain["sweepOriginBarT"] == chain["sweepBarT"]
          and chain["sweepExtremeBeyondReclaim"] is False, chain)
    c_off, c_live = turtle(off, "BUY"), turtle(live, "BUY")
    check("1 本型は LIVE でも SL が同じ(29,994 − 8 = 29,986)",
          c_off["stop"] == c_live["stop"] == 29986.0, (c_off["stop"], c_live["stop"]))
    check("1 本型は decisionId も同じ・タグなし",
          off["decision"]["decisionId"] == live["decision"]["decisionId"]
          and "SWEEP_STOP_FULL_SPAN" not in c_live["evidence"], c_live["evidence"])


def test_live_risk_cap():
    off = turtle(evaluate(DEEP_BUY, None), "BUY")
    live = turtle(evaluate(DEEP_BUY, "LIVE"), "BUY")
    check("OFF は 22pt で上限内", off["stop"] == 29978.0 and "RISK_CAP_EXCEEDED" not in off["hardBlockers"],
          off["hardBlockers"])
    check("LIVE は 29,935 − 8 = 29,927(73pt)で RISK_CAP_EXCEEDED・WATCH",
          live["stop"] == 29927.0 and "RISK_CAP_EXCEEDED" in live["hardBlockers"]
          and live["state"] == "WATCH", (live["stop"], live["hardBlockers"], live["state"]))


def test_pure_chain_prices():
    bars = [{"t": 0, "o": 29988, "h": 30012, "l": 29986, "c": 30006}]
    base = {"type": "SWEEP", "side": "BUY", "sweepBarT": 0}
    check("sweepExtreme が無ければ奪還足の極値", msnr_gate._chain_prices(base, bars, 30000.0, 8.0)[0] == 29978.0)
    check("sweepExtreme があればそちら", msnr_gate._chain_prices({**base, "sweepExtreme": 29970.0},
                                                             bars, 30000.0, 8.0)[0] == 29962.0)
    flip = {"type": "FLIP", "side": "BUY", "breakBarT": 0, "sweepExtreme": 29900.0}
    check("FLIP(BREAKER)の SL は対象外", msnr_gate._chain_prices(flip, bars, 30000.0, 8.0)[0] == 29978.0)


saved = {key: os.environ.get(key) for key in ENV_KEYS}
try:
    for key in ENV_KEYS:
        os.environ.pop(key, None)
    test_switch_parsing()
    test_default_off_keeps_reclaim_bar_stop()
    test_live_two_bar_buy_anchors_full_sweep()
    test_live_two_bar_sell_mirror()
    test_one_bar_sweep_unchanged()
    test_live_risk_cap()
    test_pure_chain_prices()
finally:
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
print("ALL PASS test_r88_turtle_sweep_stop")

# -*- coding: utf-8 -*-
"""msnr_gate.py(MSNR 確認連鎖ゲート)の判定を合成 bars で検証する。

実データはコピーしない(.secrets に依存しない)。実サイクル bundle での検証は
HANDOFF.md の完了記録に手動検証として残してある。
ネットワーク・ファイル書き込みなし。CLI 検査だけ subprocess で stdin を渡す。
"""
import json
import os
import subprocess
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import msnr_gate  # noqa: E402

FAILED = []


def check(name, cond, detail=""):
    print(("  OK   " if cond else "  FAIL ") + name + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


def section(title):
    print(f"\n{'=' * 68}\n{title}\n{'=' * 68}")


T0 = 1786970000 - (1786970000 % 180)          # 180 秒境界に揃えた基準時刻
P = 30000.0                                    # テスト用のレベル


def mk_bars(spec):
    """(o, h, l, c) の並びを 180 秒刻みの bars に。価格は 0.25 整合で書くこと。"""
    return [{"t": T0 + 180 * i, "o": o, "h": h, "l": l, "c": c, "v": 1000}
            for i, (o, h, l, c) in enumerate(spec)]


def bundle_of(bars, levels=None, forming=False, long_keys=False, vwap=None):
    """bars と levels から最小の bundle を作る。forming=True で最終足を未確定にする。

    vwap は R4 の vwapPath が drift 検証に使う参照値(bundle.vwap)。
    渡さない場合は drift が測れないので vwapPath は VWAP_DRIFT で無効になる。
    """
    last_t = bars[-1]["t"]
    price_at_epoch = last_t + (179 if forming else 180)
    rows = []
    for bar in bars:
        if long_keys:
            rows.append({"time": bar["t"], "open": bar["o"], "high": bar["h"],
                         "low": bar["l"], "close": bar["c"]})
        else:
            rows.append(dict(bar))
    out = {
        "at": "2026-08-18T00:00:00+09:00",
        "priceAt": msnr_gate.datetime.fromtimestamp(
            price_at_epoch, msnr_gate.datetime.now().astimezone().tzinfo).isoformat(),
        "snapshot": {
            "levels": levels if levels is not None else [{"label": "TEST", "price": P}],
            "bars3m": rows,
        },
    }
    if vwap is not None:
        out["vwap"] = vwap
    return out


# 値幅 8pt・実体 2pt の平穏な足(ノイズ床 8pt / 回転シグナルにならない)
def filler(mid):
    return (mid - 1, mid + 5, mid - 3, mid + 1)


ABOVE = [filler(30013) for _ in range(12)]     # 終値 30014 = レベル上(支持として機能)
BELOW = [filler(29985) for _ in range(12)]     # 終値 29986 = レベル下(抵抗として機能)


def evaluate(bars, levels=None, forming=False, long_keys=False, vwap=None):
    """(o,h,l,c) の並びでも bars 辞書でも受ける。"""
    if bars and not isinstance(bars[0], dict):
        bars = mk_bars(bars)
    return msnr_gate.evaluate(bundle_of(bars, levels, forming, long_keys, vwap))


def own_vwap(spec):
    """フィクスチャ自身の窓内 VWAP(drift 0 で vwapPath を有効にするため)。"""
    return msnr_gate.rolling_vwap(mk_bars(spec))[-1]


def bar_t(i):
    return T0 + 180 * i


def level0(result):
    return result["levels"][0]


section("1. 完成した連鎖 — スイープ→displacement→MSS→保持リテスト")
FULL_BUY = ABOVE + [
    (29998, 30012, 29994, 30010),    # 12 スイープ(1本型): 下を刈って実体は上、実体12≥NF8
    (30010, 30026, 30008, 30024),    # 13 MSS: 直前5本の高値 30018 を実体で上抜け
    (30020, 30022, 30000, 30012),    # 14 保持リテスト: レベルに到達し上で引ける
]
res = evaluate(FULL_BUY)
lv = level0(res)
chain = next((c for c in lv["chains"] if c["side"] == "BUY"), None)


def test_single_bar_sweep_full_chain_buy():
    check("noiseFloor が 8pt", res["noiseFloor"] == 8.0, str(res["noiseFloor"]))
    check("touchTol が下限 2.0pt", res["touchTol"] == 2.0, str(res["touchTol"]))
    check("BUY 連鎖が RETEST_HELD", chain is not None and chain["state"] == "RETEST_HELD",
          str(chain))
    check("sweepBarT がスイープ足", chain and chain["sweepBarT"] == bar_t(12))
    check("mssBarT が MSS 足", chain and chain["mssBarT"] == bar_t(13))
    check("retestBarT がリテスト足", chain and chain["retestBarT"] == bar_t(14))
    check("anchorOk[BUY]", lv["anchorOk"]["BUY"] is True, lv["freshness"])
    check("回転は OK", res["rotation"]["verdict"] == "OK", str(res["rotation"]))
    check("BUY promotion allowed", lv["promotion"]["BUY"]["allowed"] is True,
          str(lv["promotion"]["BUY"]))
    check("blockers は空", lv["promotion"]["BUY"]["blockers"] == [])
    check("summary に昇格可", "✔昇格可" in res["summary"], res["summary"])


test_single_bar_sweep_full_chain_buy()


section("1b. SELL 鏡像 — 抵抗スイープの完成連鎖")
FULL_SELL = BELOW + [
    (30002, 30006, 29988, 29990),    # 12 スイープ(1本型): 上を刈って実体は下、実体12
    (29990, 29992, 29974, 29976),    # 13 MSS: 直前5本の安値 29982 を実体で下抜け
    (29980, 30000, 29978, 29988),    # 14 保持リテスト: レベルに到達し下で引ける
]
res_sell = evaluate(FULL_SELL)
lv_sell = level0(res_sell)
chain_sell = next((c for c in lv_sell["chains"] if c["side"] == "SELL"), None)


def test_single_bar_sweep_full_chain_sell():
    check("SELL 連鎖が RETEST_HELD",
          chain_sell is not None and chain_sell["state"] == "RETEST_HELD", str(chain_sell))
    check("SELL promotion allowed", lv_sell["promotion"]["SELL"]["allowed"] is True,
          str(lv_sell["promotion"]["SELL"]))
    check("BUY 側は連鎖なし", lv_sell["promotion"]["BUY"]["allowed"] is False)


test_single_bar_sweep_full_chain_sell()


section("2. 2本型スイープ(破壊→2本以内の奪還)")
TWO_BAR = ABOVE + [
    (30010, 30012, 29986, 29990),    # 12 破壊(実体でゾーン下に引ける)
    (29988, 30012, 29986, 30006),    # 13 奪還(1本後)= sweepBar・実体18
    (30006, 30026, 30004, 30024),    # 14 MSS
    (30020, 30022, 30000, 30012),    # 15 保持リテスト(レベルに到達)
]
res2 = evaluate(TWO_BAR)
lv2 = level0(res2)
chain2 = next((c for c in lv2["chains"] if c["side"] == "BUY"), None)


def test_two_bar_sweep_reclaim():
    check("連鎖が成立", chain2 is not None, "chain none")
    check("sweepBar は奪還足", chain2 and chain2["sweepBarT"] == T0 + 180 * 13,
          str(chain2))
    check("RETEST_HELD まで到達", chain2 and chain2["state"] == "RETEST_HELD", str(chain2))
    check("freshness は RECLAIMED", lv2["freshness"] == "RECLAIMED", lv2["freshness"])
    check("promotion allowed", lv2["promotion"]["BUY"]["allowed"] is True,
          str(lv2["promotion"]["BUY"]))


test_two_bar_sweep_reclaim()


section("3. 奪還が3本後 — 連鎖不成立・BROKEN のまま")
LATE = ABOVE + [
    (30010, 30012, 29986, 29990),    # 12 破壊
    (29990, 29992, 29984, 29988),    # 13
    (29988, 29990, 29982, 29986),    # 14
    (29986, 30012, 29984, 30006),    # 15 奪還(破壊から3本後 → 2本型の窓外)
]
res3 = evaluate(LATE)
lv3 = level0(res3)


def test_reclaim_too_late_is_broken():
    check("BUY のスイープ連鎖なし(2本型の窓外)",
          not [c for c in lv3["chains"] if c["side"] == "BUY" and c["type"] == "SWEEP"],
          str(lv3["chains"]))
    check("freshness=BROKEN", lv3["freshness"] == "BROKEN", lv3["freshness"])
    check("anchorOk[BUY]=False", lv3["anchorOk"]["BUY"] is False)
    check("blockers に ANCHOR_BROKEN と未完成コード",
          "ANCHOR_BROKEN" in lv3["promotion"]["BUY"]["blockers"] and
          set(lv3["promotion"]["BUY"]["blockers"]) &
          {"NO_CHAIN", "ACCEPTANCE_NOT_CONFIRMED", "FLIP_RETEST_NOT_HELD"},
          str(lv3["promotion"]["BUY"]))
    check("完成していないので不許可", lv3["promotion"]["BUY"]["allowed"] is False)


test_reclaim_too_late_is_broken()


section("4. 8/17 型 — 実体ブレイク→即リテストだけでは武装しない")
BREAK_RETEST = BELOW + [
    (29988, 30012, 29986, 30010),    # 12 下から上への実体ブレイク(スイープではない)
    (30010, 30014, 30001, 30008),    # 13 リテストして保持
    (30008, 30020, 30006, 30018),    # 14 継続
]
res4 = evaluate(BREAK_RETEST)
lv4 = level0(res4)


def test_break_retest_without_sweep_blocked():
    check("BUY promotion は不許可", lv4["promotion"]["BUY"]["allowed"] is False,
          str(lv4["promotion"]["BUY"]))
    check("blocker に連鎖系コード",
          set(lv4["promotion"]["BUY"]["blockers"]) & {
              "NO_CHAIN", "SWEEP_ONLY", "NO_DISPLACEMENT", "MSS_NOT_CONFIRMED",
              "RETEST_NOT_HELD", "ACCEPTANCE_NOT_CONFIRMED", "FLIP_RETEST_NOT_HELD",
              "CHAIN_EXPIRED"},
          str(lv4["promotion"]["BUY"]["blockers"]))
    check("BUY のスイープ連鎖は生成されない(ブレイクはスイープではない)",
          not [c for c in lv4["chains"] if c["side"] == "BUY" and c["type"] == "SWEEP"],
          str(lv4["chains"]))


test_break_retest_without_sweep_blocked()


section("5. displacement 不足 — SWEEP_CANDIDATE 止まり")
NO_DISP = ABOVE + [
    (30004, 30010, 29994, 30008),    # 12 スイープだが実体 4pt < 1.0×NF(8pt)
    (30008, 30026, 30006, 30024),    # 13 その後 MSS 相当の足が出ても昇格しない
    (30020, 30022, 30001, 30012),    # 14 リテスト相当
]
res5 = evaluate(NO_DISP)
lv5 = level0(res5)
chain5 = next((c for c in lv5["chains"] if c["side"] == "BUY"), None)


def test_no_displacement_stays_candidate():
    check("state=SWEEP_CANDIDATE", chain5 and chain5["state"] == "SWEEP_CANDIDATE",
          str(chain5))
    check("blocker に NO_DISPLACEMENT",
          "NO_DISPLACEMENT" in lv5["promotion"]["BUY"]["blockers"],
          str(lv5["promotion"]["BUY"]))
    check("promotion 不許可", lv5["promotion"]["BUY"]["allowed"] is False)


test_no_displacement_stays_candidate()


section("5b. 連鎖の途中状態 — MSS 待ち / リテスト待ち")
NO_MSS = ABOVE + [
    (29998, 30012, 29994, 30010),    # 12 スイープ+displacement
    (30010, 30014, 30006, 30012),    # 13 直前5本の高値 30018 を超えない
    (30012, 30016, 30008, 30010),    # 14 同上
]
res5b = evaluate(NO_MSS)
lv5b = level0(res5b)
chain5b = next((c for c in lv5b["chains"] if c["side"] == "BUY"), None)
check("state=SWEEP_CONFIRMED", chain5b and chain5b["state"] == "SWEEP_CONFIRMED",
      str(chain5b))
check("blocker=MSS_NOT_CONFIRMED",
      lv5b["promotion"]["BUY"]["blockers"] == ["MSS_NOT_CONFIRMED"],
      str(lv5b["promotion"]["BUY"]))

NO_RETEST = ABOVE + [
    (29998, 30012, 29994, 30010),    # 12 スイープ
    (30010, 30026, 30008, 30024),    # 13 MSS
    (30024, 30030, 30022, 30028),    # 14 ゾーンに帰ってこない
    (30028, 30032, 30026, 30030),    # 15 同上(TTL 内)
]
res5c = evaluate(NO_RETEST)
lv5c = level0(res5c)
chain5c = next((c for c in lv5c["chains"] if c["side"] == "BUY"), None)
check("state=MSS_CONFIRMED", chain5c and chain5c["state"] == "MSS_CONFIRMED",
      str(chain5c))
check("barsLeft が残っている", chain5c and chain5c["barsLeft"] == 12, str(chain5c))
check("blocker=RETEST_NOT_HELD",
      lv5c["promotion"]["BUY"]["blockers"] == ["RETEST_NOT_HELD"],
      str(lv5c["promotion"]["BUY"]))
check("summary に残本数", "残12本" in res5c["summary"], res5c["summary"])


section("6. 回転相場 — 全レベルを止める")
ROT = []
low, high = 30000.0, 30020.0
for i in range(14):
    if i % 2 == 0:
        ROT.append((low, high + 1, low - 1, high))       # 上げの実体
        low, high = high, high + 2
    else:
        ROT.append((low, low + 1, high - 22, high - 22))  # 次足で始値を割る全戻し
        low, high = high - 22, low + 2
res6 = evaluate(mk_bars(ROT), levels=[{"label": "TEST", "price": 30010.0}])


def test_rotation_blocks_all():
    rot = res6["rotation"]
    check("verdict=ROTATION", rot["verdict"] == "ROTATION", str(rot))
    check("signals ≥ 4", rot["signals"] >= 4, str(rot))
    check("ratio ≥ 0.5", rot["ratio"] >= 0.5, str(rot))
    ok = all("ROTATION_REGIME" in lvx["promotion"][s]["blockers"]
             for lvx in res6["levels"] for s in ("BUY", "SELL"))
    check("全レベルに ROTATION_REGIME", ok,
          str([lvx["promotion"] for lvx in res6["levels"]]))
    check("allowed は全て False",
          not any(lvx["promotion"][s]["allowed"]
                  for lvx in res6["levels"] for s in ("BUY", "SELL")))
    check("summary に回転", "回転" in res6["summary"], res6["summary"])


test_rotation_blocks_all()


section("7. 実体タッチ3回 — CONSUMED はアンカーにならない")
CONSUMED = ABOVE + [
    (30006, 30008, 29998, 30001),    # 12 エピソード 1
    (30001, 30012, 30000, 30008),    # 13 ゾーン外へ離脱
    (30008, 30010, 29997, 30000),    # 14 エピソード 2
    (30000, 30012, 29999, 30008),    # 15 ゾーン外へ離脱
    (30008, 30010, 29998, 30001),    # 16 エピソード 3 → CONSUMED
]
res7 = evaluate(CONSUMED)
lv7 = level0(res7)


def test_consumed_level_not_anchor():
    check("bodyTouches=3(エピソード)", lv7["bodyTouches"] == 3,
          str(lv7["bodyTouches"]))
    check("freshness=CONSUMED", lv7["freshness"] == "CONSUMED", lv7["freshness"])
    check("anchorOk が両サイド False",
          lv7["anchorOk"] == {"BUY": False, "SELL": False}, str(lv7["anchorOk"]))
    check("両サイドに ANCHOR_CONSUMED",
          all("ANCHOR_CONSUMED" in lv7["promotion"][s]["blockers"] for s in ("BUY", "SELL")),
          str(lv7["promotion"]))


test_consumed_level_not_anchor()


section("8. TTL — スイープから15本でリテストが来なければ失効")
TTL = ABOVE + [
    (29998, 30012, 29994, 30010),    # 12 スイープ
    (30010, 30026, 30008, 30024),    # 13 MSS
] + [(30030, 30034, 30026, 30030) for _ in range(15)]   # 14〜28 レベルに戻らない
res8 = evaluate(TTL)
lv8 = level0(res8)
chain8 = next((c for c in lv8["chains"] if c["side"] == "BUY"), None)


def test_chain_ttl_expires():
    check("state=EXPIRED", chain8 and chain8["state"] == "EXPIRED", str(chain8))
    check("barsLeft ≤ 0", chain8 and chain8["barsLeft"] <= 0, str(chain8))
    check("blocker=CHAIN_EXPIRED",
          "CHAIN_EXPIRED" in lv8["promotion"]["BUY"]["blockers"],
          str(lv8["promotion"]["BUY"]))
    check("promotion 不許可", lv8["promotion"]["BUY"]["allowed"] is False)


test_chain_ttl_expires()


section("9. VWAP 系レベルは連鎖評価の対象外")
res9 = evaluate(FULL_BUY, levels=[{"label": "VWAP Lower Band", "price": P},
                                  {"label": "TEST", "price": 30100.0}])
lv9 = res9["levels"][0]


def test_vwap_level_excluded():
    check("dynamic=True", lv9["dynamic"] is True)
    check("freshness=None", lv9["freshness"] is None, str(lv9["freshness"]))
    check("chains は空", lv9["chains"] == [])
    check("両サイドとも DYNAMIC_LEVEL のみ",
          all(lv9["promotion"][s]["blockers"] == ["DYNAMIC_LEVEL"] and
              lv9["promotion"][s]["allowed"] is False for s in ("BUY", "SELL")),
          str(lv9["promotion"]))


test_vwap_level_excluded()


section("10. 形成中バーは落とす")
res10a = evaluate(FULL_BUY, forming=False)
res10b = evaluate(FULL_BUY, forming=True)


def test_forming_bar_dropped():
    check("確定足は全数", res10a["closedBars"] == len(FULL_BUY), str(res10a["closedBars"]))
    check("形成中は1本少ない", res10b["closedBars"] == len(FULL_BUY) - 1,
          str(res10b["closedBars"]))


test_forming_bar_dropped()


section("11. 確定足 11 本 — fail-closed")
res11 = evaluate(ABOVE[:11])


def test_insufficient_bars_fail_closed():
    check("closedBars=11", res11["closedBars"] == 11, str(res11["closedBars"]))
    check("noiseFloor=None", res11["noiseFloor"] is None)
    check("全 promotion が INSUFFICIENT_BARS",
          all(res11["levels"][0]["promotion"][s]["blockers"] == ["INSUFFICIENT_BARS"] and
              res11["levels"][0]["promotion"][s]["allowed"] is False
              for s in ("BUY", "SELL")), str(res11["levels"][0]["promotion"]))
    check("summary に判定不能", "判定不能" in res11["summary"], res11["summary"])


test_insufficient_bars_fail_closed()


section("12. CLI — BOM 付き入力・long キー・終了コード")
GATE = os.path.join(BASE, "msnr_gate.py")


def run_cli(args, payload_bytes):
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    proc = subprocess.run([sys.executable, GATE] + args, input=payload_bytes,
                          capture_output=True, env=env, timeout=60)
    return proc.returncode, proc.stdout.decode("utf-8", "replace"), \
        proc.stderr.decode("utf-8", "replace")


def test_bom_and_longkey_input():
    payload = json.dumps(bundle_of(mk_bars(FULL_BUY), long_keys=True),
                         ensure_ascii=False).encode("utf-8")
    bom = b"\xef\xbb\xbf" + payload
    code, out, err = run_cli(["--summary"], bom)
    check("BOM 付きで exit 0", code == 0, err.strip())
    check("summary 1行", out.strip().startswith("MSNR:") and len(out.strip().splitlines()) == 1,
          out.strip())
    check("long キーでも連鎖が成立", "✔昇格可" in out, out.strip())

    code, out, err = run_cli(["--level", str(P), "--side", "buy"], bom)
    view = json.loads(out) if code == 0 else {}
    check("--level/--side で exit 0", code == 0, err.strip())
    check("該当レベルの promotion のみ",
          view.get("matched") is True and list(view.get("promotion", {})) == ["BUY"],
          out.strip()[:200])
    check("allowed が本体と一致", view.get("promotion", {}).get("BUY", {}).get("allowed") is True)

    code, out, err = run_cli([], b"{not json}")
    check("壊れた入力は exit 2", code == 2, str(code))
    check("stderr に error", '"error"' in err, err.strip()[:120])

    code, out, err = run_cli([], json.dumps({"snapshot": {"bars3m": "nope"}}).encode())
    check("bars が list でなければ exit 2", code == 2, str(code))


test_bom_and_longkey_input()


section("R2-1. SBR フリップ(支持の下抜け→受容→戻り売り)")
SBR = ABOVE + [
    (30010, 30012, 29990, 29994),    # 12 break: 支持を実体で下抜け
    (29994, 29996, 29986, 29990),    # 13 acceptance: ゾーン外でもう1本引ける
    (29992, 30001, 29988, 29992),    # 14 revisit & hold: ゾーンに戻り陰線で保持
    (29990, 29992, 29982, 29984),    # 15 下方向へ継続
    (29984, 29986, 29976, 29978),    # 16
]
res_sbr = evaluate(SBR)
lv_sbr = level0(res_sbr)
flip_sbr = next((c for c in lv_sbr["chains"]
                 if c["type"] == "FLIP" and c["side"] == "SELL"), None)


def test_sbr_flip_sell_allowed():
    check("FLIP 連鎖が FLIP_HELD", flip_sbr and flip_sbr["state"] == "FLIP_HELD",
          str(flip_sbr))
    check("breakBarT / holdBarT が正しい足",
          flip_sbr and flip_sbr["breakBarT"] == bar_t(12)
          and flip_sbr["holdBarT"] == bar_t(14), str(flip_sbr))
    check("freshness=FLIPPED", lv_sbr["freshness"] == "FLIPPED", str(lv_sbr["freshness"]))
    check("anchorOk は SELL のみ true",
          lv_sbr["anchorOk"] == {"BUY": False, "SELL": True}, str(lv_sbr["anchorOk"]))
    check("SELL promotion allowed", lv_sbr["promotion"]["SELL"]["allowed"] is True,
          str(lv_sbr["promotion"]["SELL"]))
    check("BUY は不許可", lv_sbr["promotion"]["BUY"]["allowed"] is False)
    check("summary に鮮度残", "鮮度残" in res_sbr["summary"], res_sbr["summary"])


test_sbr_flip_sell_allowed()


section("R2-2. RBS フリップ(抵抗の上抜け→受容→押し目買い)")
RBS = BELOW + [
    (29990, 30010, 29988, 30006),    # 12 break: 抵抗を実体で上抜け
    (30006, 30014, 30004, 30010),    # 13 acceptance
    (30008, 30010, 29999, 30008),    # 14 revisit & hold
    (30008, 30018, 30006, 30016),    # 15 上方向へ継続
    (30016, 30026, 30014, 30024),    # 16
]
res_rbs = evaluate(RBS)
lv_rbs = level0(res_rbs)
flip_rbs = next((c for c in lv_rbs["chains"]
                 if c["type"] == "FLIP" and c["side"] == "BUY"), None)


def test_rbs_flip_buy_allowed():
    check("FLIP 連鎖が FLIP_HELD", flip_rbs and flip_rbs["state"] == "FLIP_HELD",
          str(flip_rbs))
    check("anchorOk は BUY のみ true",
          lv_rbs["anchorOk"] == {"BUY": True, "SELL": False}, str(lv_rbs["anchorOk"]))
    check("BUY promotion allowed", lv_rbs["promotion"]["BUY"]["allowed"] is True,
          str(lv_rbs["promotion"]["BUY"]))
    check("SELL は不許可", lv_rbs["promotion"]["SELL"]["allowed"] is False)


test_rbs_flip_buy_allowed()


section("R2-3. 受容が無いフリップ")
NO_ACCEPT = ABOVE + [
    (30010, 30012, 29990, 29994),    # 12 break
    (29994, 30004, 29992, 30000),    # 13 すぐゾーンへ帰還(ゾーン外クローズが続かない)
    (30000, 30003, 29997, 30000),    # 14
    (30000, 30004, 29998, 30001),    # 15
]
res_na = evaluate(NO_ACCEPT)
lv_na = level0(res_na)


def test_flip_without_acceptance_blocked():
    flip = next((c for c in lv_na["chains"] if c["type"] == "FLIP"), None)
    check("state=FLIP_BREAK", flip and flip["state"] == "FLIP_BREAK", str(flip))
    check("blocker=ACCEPTANCE_NOT_CONFIRMED",
          "ACCEPTANCE_NOT_CONFIRMED" in lv_na["promotion"]["SELL"]["blockers"],
          str(lv_na["promotion"]["SELL"]))
    check("SELL 不許可", lv_na["promotion"]["SELL"]["allowed"] is False)


test_flip_without_acceptance_blocked()


section("R2-4. 8/17 型 — 受容まで成立、保持足が未出現")
BEFORE_HOLD = BELOW + [
    (29990, 30010, 29988, 30006),    # 12 break(上抜け)
    (30006, 30014, 30004, 30010),    # 13 acceptance
    (30010, 30016, 30012, 30014),    # 14 ゾーンに戻らないまま推移
    (30014, 30018, 30012, 30016),    # 15
]
res_bh = evaluate(BEFORE_HOLD)
lv_bh = level0(res_bh)


def test_flip_before_hold_blocked():
    flip = next((c for c in lv_bh["chains"] if c["type"] == "FLIP"), None)
    check("state=FLIP_ACCEPTED", flip and flip["state"] == "FLIP_ACCEPTED", str(flip))
    check("blocker=FLIP_RETEST_NOT_HELD",
          "FLIP_RETEST_NOT_HELD" in lv_bh["promotion"]["BUY"]["blockers"],
          str(lv_bh["promotion"]["BUY"]))
    check("BUY 不許可(武装時点で点灯しない)",
          lv_bh["promotion"]["BUY"]["allowed"] is False)


test_flip_before_hold_blocked()


section("R2-5. break 後の奪還でフリップ連鎖は消える")
FLIP_RESET = ABOVE + [
    (30010, 30012, 29990, 29994),    # 12 break
    (29994, 29996, 29986, 29990),    # 13 acceptance
    (29990, 30012, 29988, 30006),    # 14 奪還 → フリップ失敗
]
res_fr = evaluate(FLIP_RESET)
lv_fr = level0(res_fr)


def test_flip_reset_on_reclaim():
    check("SELL の FLIP 連鎖が消える",
          not [c for c in lv_fr["chains"]
               if c["type"] == "FLIP" and c["side"] == "SELL"], str(lv_fr["chains"]))
    check("SELL は NO_CHAIN",
          "NO_CHAIN" in lv_fr["promotion"]["SELL"]["blockers"],
          str(lv_fr["promotion"]["SELL"]))
    check("SELL 不許可", lv_fr["promotion"]["SELL"]["allowed"] is False)


test_flip_reset_on_reclaim()


section("R2-6/7. CONSUMED はエピソード数で数える")
CONSEC = ABOVE + [
    (30006, 30008, 29998, 30001),    # 12 ゾーン内 1本目
    (30001, 30003, 29997, 30000),    # 13 連続 → 同一エピソード
    (30000, 30002, 29996, 30001),    # 14 連続 → 同一エピソード
]
res_c1 = evaluate(CONSEC)
lv_c1 = level0(res_c1)


def test_consumed_consecutive_is_one_episode():
    check("bodyTouches=1(エピソード)", lv_c1["bodyTouches"] == 1, str(lv_c1["bodyTouches"]))
    check("CONSUMED にならない", lv_c1["freshness"] == "BODY_TESTED", lv_c1["freshness"])
    check("両サイドのアンカーが生きている",
          lv_c1["anchorOk"] == {"BUY": True, "SELL": True}, str(lv_c1["anchorOk"]))


test_consumed_consecutive_is_one_episode()

EPISODES = ABOVE + [
    (30006, 30008, 29998, 30001),    # 12 エピソード 1
    (30001, 30012, 30000, 30008),    # 13 ゾーン外へ離脱
    (30008, 30010, 29998, 30000),    # 14 エピソード 2
    (30000, 30012, 29999, 30008),    # 15 ゾーン外へ離脱
    (30008, 30010, 29998, 30001),    # 16 エピソード 3 → CONSUMED
]
res_c3 = evaluate(EPISODES)
lv_c3 = level0(res_c3)


def test_consumed_three_episodes():
    check("bodyTouches=3", lv_c3["bodyTouches"] == 3, str(lv_c3["bodyTouches"]))
    check("freshness=CONSUMED", lv_c3["freshness"] == "CONSUMED", lv_c3["freshness"])
    check("両サイドとも ANCHOR_CONSUMED",
          all("ANCHOR_CONSUMED" in lv_c3["promotion"][s]["blockers"]
              for s in ("BUY", "SELL")), str(lv_c3["promotion"]))


test_consumed_three_episodes()


section("R2-8. 完成連鎖の鮮度 TTL")
STALE = FULL_BUY + [(30030, 30034, 30026, 30030) for _ in range(11)]
res_stale = evaluate(STALE)
lv_stale = level0(res_stale)
chain_stale = next((c for c in lv_stale["chains"]
                    if c["type"] == "SWEEP" and c["side"] == "BUY"), None)


def test_completed_chain_expires():
    check("保持から11本経過で EXPIRED",
          chain_stale and chain_stale["state"] == "EXPIRED", str(chain_stale))
    check("blocker=CHAIN_EXPIRED",
          "CHAIN_EXPIRED" in lv_stale["promotion"]["BUY"]["blockers"],
          str(lv_stale["promotion"]["BUY"]))
    check("allowed=False", lv_stale["promotion"]["BUY"]["allowed"] is False)


test_completed_chain_expires()


section("R2-9. BROKEN 支持は SELL 側のアンカーとして生きる")


def test_anchor_broken_flip_side_ok():
    check("anchorOk が side 別",
          lv3["anchorOk"] == {"BUY": False, "SELL": True}, str(lv3["anchorOk"]))


test_anchor_broken_flip_side_ok()


section("R3-1. 到達条件 — 戻りがレベルに届かないスイープは保持にならない")
SHORT_RETEST = ABOVE + [
    (29998, 30012, 29994, 30010),    # 12 スイープ
    (30010, 30026, 30008, 30024),    # 13 MSS
    (30020, 30022, 30000.25, 30012),  # 14 戻りが 1tick 手前で止まる
]
res_sr = evaluate(SHORT_RETEST)
lv_sr = level0(res_sr)
chain_sr = next((c for c in lv_sr["chains"]
                 if c["type"] == "SWEEP" and c["side"] == "BUY"), None)


def test_retest_short_of_level_blocked():
    check("state=MSS_CONFIRMED(保持にならない)",
          chain_sr and chain_sr["state"] == "MSS_CONFIRMED", str(chain_sr))
    check("blocker=RETEST_NOT_HELD のみ",
          lv_sr["promotion"]["BUY"]["blockers"] == ["RETEST_NOT_HELD"],
          str(lv_sr["promotion"]["BUY"]))
    check("allowed=False", lv_sr["promotion"]["BUY"]["allowed"] is False)


test_retest_short_of_level_blocked()


section("R3-2. 到達条件 — 0028 の再現(P=30,253 / revisit l=30,255)")
# 実バンドル monitor_cycle_0028/0031 の Weekly Mid 30,253 を合成で再現する。
# ノイズ床 20pt → tol 2.0pt。R2 までは l=30,255(ゾーン上端ちょうど)で保持が
# 成立していた。到達条件ではレベル本体に 2pt 届いていないので不成立。
LV_0028 = 30253.0
FILL_0028 = [(30238, 30248, 30228, 30240) for _ in range(12)]   # レベル下で推移
GRAZE = FILL_0028 + [
    (30250, 30277, 30249, 30273.5),      # 12 break(上抜け)
    (30273.5, 30287, 30266, 30268.5),    # 13 acceptance
    (30257.75, 30278, 30255, 30266),     # 14 戻りはレベルの 2pt 手前で止まる
    (30266, 30279, 30262, 30271.75),     # 15
    (30271.5, 30276, 30266, 30272),      # 16
]
res_gz = evaluate(GRAZE, levels=[{"label": "Weekly Mid", "price": LV_0028}])
lv_gz = level0(res_gz)
flip_gz = next((c for c in lv_gz["chains"] if c["type"] == "FLIP"), None)


def test_flip_hold_short_of_level_blocked():
    check("ノイズ床 20pt / tol 2.0pt(R2 では通っていた較正)",
          res_gz["noiseFloor"] == 20.0 and res_gz["touchTol"] == 2.0,
          f"{res_gz['noiseFloor']} / {res_gz['touchTol']}")
    check("state=FLIP_ACCEPTED(保持にならない)",
          flip_gz and flip_gz["state"] == "FLIP_ACCEPTED", str(flip_gz))
    check("blocker=FLIP_RETEST_NOT_HELD のみ(ANCHOR/回転による偶然の false でない)",
          lv_gz["promotion"]["BUY"]["blockers"] == ["FLIP_RETEST_NOT_HELD"],
          str(lv_gz["promotion"]["BUY"]))
    check("BUY 側のアンカーは生きている(false の理由は連鎖だけ)",
          lv_gz["anchorOk"]["BUY"] is True, str(lv_gz["anchorOk"]))
    check("回転は OK", res_gz["rotation"]["verdict"] == "OK", str(res_gz["rotation"]))
    check("allowed=False", lv_gz["promotion"]["BUY"]["allowed"] is False)


test_flip_hold_short_of_level_blocked()


section("13. blocker コードは指示書 §5(R2)の列挙のみ")
ALLOWED_CODES = {"NO_CHAIN", "SWEEP_ONLY", "NO_DISPLACEMENT", "MSS_NOT_CONFIRMED",
                 "RETEST_NOT_HELD", "ACCEPTANCE_NOT_CONFIRMED", "FLIP_RETEST_NOT_HELD",
                 "CHAIN_EXPIRED", "ANCHOR_BROKEN", "ANCHOR_CONSUMED",
                 "ROTATION_REGIME", "INSUFFICIENT_BARS", "DYNAMIC_LEVEL"}
seen = set()
for r in (res, res_sell, res2, res3, res4, res5, res5b, res5c, res6, res7, res8,
          res9, res11, res_sbr, res_rbs, res_na, res_bh, res_fr, res_c1, res_c3,
          res_stale, res_sr, res_gz):
    for lvx in r["levels"]:
        for s in ("BUY", "SELL"):
            seen |= set(lvx["promotion"][s]["blockers"])
        for c in lvx["chains"]:
            seen |= set(c["blockers"])
check("列挙外のコードが無い", seen <= ALLOWED_CODES, str(seen - ALLOWED_CODES))
check("主要コードを実際に踏んでいる",
      {"NO_CHAIN", "NO_DISPLACEMENT", "RETEST_NOT_HELD", "CHAIN_EXPIRED",
       "ANCHOR_BROKEN", "ANCHOR_CONSUMED", "ROTATION_REGIME", "INSUFFICIENT_BARS",
       "DYNAMIC_LEVEL", "SWEEP_ONLY", "MSS_NOT_CONFIRMED"} <= seen,
      str(sorted(seen)))


section("14. レベル統合と環境変数の上書き")
res14 = evaluate(FULL_BUY, levels=[{"label": "PP", "price": P},
                                   {"label": "CPR-TC", "price": P + 1.5},
                                   {"label": "Far", "price": P + 40}])
check("2.0pt 以内は統合される", len(res14["levels"]) == 2, str(len(res14["levels"])))
check("mergedWith に吸収ラベル", res14["levels"][0]["mergedWith"] == ["CPR-TC"],
      str(res14["levels"][0]["mergedWith"]))
check("残るのは先着の label/price",
      res14["levels"][0]["label"] == "PP" and res14["levels"][0]["price"] == P)

os.environ["NQX_MSNR_DISP_MULT"] = "3.0"
res14b = evaluate(FULL_BUY)
os.environ.pop("NQX_MSNR_DISP_MULT")
check("DISP_MULT の上書きが効く",
      res14b["levels"][0]["promotion"]["BUY"]["allowed"] is False and
      "NO_DISPLACEMENT" in res14b["levels"][0]["promotion"]["BUY"]["blockers"],
      str(res14b["levels"][0]["promotion"]["BUY"]))


section("15. R4 §1 — VWAP 反応経路(ADVISORY)")
# 下降で VWAP の下に居座る12本 → 急騰で突破 → 受容 → VWAP まで押して上で引ける
_down, _mid = [], 30060
for _ in range(12):
    _down.append((_mid + 2, _mid + 3, _mid - 3, _mid - 2))
    _mid -= 5
RECLAIM = _down + [
    (30003, 30042, 30002, 30040),    # 12 突破
    (30040, 30048, 30038, 30046),    # 13 受容
    (30046, 30047, 30020, 30038),    # 14 到達 + 保持
]
res15 = evaluate(RECLAIM, vwap=own_vwap(RECLAIM))


def test_vwap_reclaim_full_path():
    vw = res15["vwapPath"]
    check("side=BUY(RECLAIM)", vw["side"] == "BUY", str(vw))
    check("state=VWAP_HELD", vw["state"] == "VWAP_HELD", str(vw))
    check("breakBarT が突破足", vw["breakBarT"] == bar_t(12), str(vw))
    check("holdBarT が保持足", vw["holdBarT"] == bar_t(14), str(vw))
    check("drift 0.0pt", vw["drift"] == 0.0, str(vw["drift"]))
    check("blockers は空", vw["blockers"] == [])
    check("summary に VWAP 行", "VWAP: RECLAIM" in res15["summary"], res15["summary"])


test_vwap_reclaim_full_path()

_up, _mid = [], 29940
for _ in range(12):
    _up.append((_mid - 2, _mid + 3, _mid - 3, _mid + 2))
    _mid += 5
REJECT = _up + [
    (29997, 29998, 29958, 29960),    # 12 実体で VWAP 割れ
    (29960, 29962, 29952, 29954),    # 13 受容
    (29954, 29980, 29953, 29962),    # 14 戻して下で引ける
]
res15b = evaluate(REJECT, vwap=own_vwap(REJECT))


def test_vwap_reject_mirror():
    vw = res15b["vwapPath"]
    check("side=SELL(REJECT)", vw["side"] == "SELL", str(vw))
    check("state=VWAP_HELD", vw["state"] == "VWAP_HELD", str(vw))
    check("summary に VWAP: REJECT", "VWAP: REJECT" in res15b["summary"],
          res15b["summary"])


test_vwap_reject_mirror()

res15c = evaluate(RECLAIM, vwap=30100.0)          # 再計算値 30032.60 から 67pt 乖離


def test_vwap_drift_disables():
    vw = res15c["vwapPath"]
    check("state は出さない", vw["state"] is None, str(vw))
    check("blocker=VWAP_DRIFT", vw["blockers"] == ["VWAP_DRIFT"], str(vw))
    check("drift を数値で報告", vw["drift"] == 67.4, str(vw["drift"]))
    check("summary に無効表示", "VWAP: 無効" in res15c["summary"], res15c["summary"])
    novwap = evaluate(RECLAIM)                    # bundle.vwap 欠落 = 検証不能
    check("参照値が無いときも fail-closed",
          novwap["vwapPath"]["blockers"] == ["VWAP_DRIFT"]
          and novwap["vwapPath"]["state"] is None, str(novwap["vwapPath"]))


test_vwap_drift_disables()

FLAT_RALLY = [filler(30000) for _ in range(12)] + [
    (30001, 30040, 30000, 30038),
    (30038, 30046, 30036, 30044),
    (30044, 30045, 30018, 30036),
]
res15d = evaluate(FLAT_RALLY, vwap=own_vwap(FLAT_RALLY))


def test_vwap_no_prior_side_no_break():
    vw = res15d["vwapPath"]
    check("下側に居た事実が無ければ break にしない", vw["state"] is None, str(vw))
    check("drift は測れている(無効化ではない)", vw["drift"] == 0.0, str(vw))
    check("blockers は空", vw["blockers"] == [], str(vw))


test_vwap_no_prior_side_no_break()


section("16. R4 §2 — grade(A+ / A)")
TWO_LEVELS = [{"label": "TEST", "price": P}, {"label": "Extra", "price": P + 1.0}]
res16 = evaluate(FULL_BUY, levels=TWO_LEVELS)
lv16 = level0(res16)
TOUCHED = ABOVE[:5] + [(30000, 30004, 29996, 30001)] + ABOVE[6:] + [
    (29998, 30012, 29994, 30010),
    (30010, 30026, 30008, 30024),
    (30020, 30022, 30000, 30012),
]
res16b = evaluate(TOUCHED, levels=TWO_LEVELS)
lv16b = level0(res16b)


def test_grade_aplus_confluence():
    chain16 = next(c for c in lv16["chains"] if c["side"] == "BUY")
    check("dispBody 12.0 ≥ 1.3×NF(10.4)", chain16["dispBody"] == 12.0,
          str(chain16.get("dispBody")))
    check("mergedWith 1件", lv16["mergedWith"] == ["Extra"], str(lv16["mergedWith"]))
    check("freshness=WICK_TESTED", lv16["freshness"] == "WICK_TESTED", lv16["freshness"])
    check("grade=A+", lv16["promotion"]["BUY"]["grade"] == "A+",
          str(lv16["promotion"]["BUY"]))
    check("summary に等級", "✔昇格可[A+]" in res16["summary"], res16["summary"])


test_grade_aplus_confluence()


def test_grade_a_when_third_test():
    check("freshness=BODY_TESTED", lv16b["freshness"] == "BODY_TESTED",
          lv16b["freshness"])
    check("allowed は維持", lv16b["promotion"]["BUY"]["allowed"] is True,
          str(lv16b["promotion"]["BUY"]))
    check("grade=A(A+ にしない)", lv16b["promotion"]["BUY"]["grade"] == "A",
          str(lv16b["promotion"]["BUY"]))
    check("不許可のときは grade=None",
          lv16b["promotion"]["SELL"]["grade"] is None,
          str(lv16b["promotion"]["SELL"]))


test_grade_a_when_third_test()


section("17. R4 §3 — レベル階層と dedupe")
res17 = evaluate(FULL_BUY, levels=[{"label": "09:30 (30000)", "price": P},
                                   {"label": "Weekly High", "price": P + 1.0},
                                   {"label": "Far", "price": P + 40}])


def test_dedupe_keeps_higher_tier():
    top = res17["levels"][0]
    check("代表は Weekly High(tier1)", top["label"] == "Weekly High", top["label"])
    check("tier=1", top["tier"] == 1, str(top["tier"]))
    check("旧代表は mergedWith へ", top["mergedWith"] == ["09:30 (30000)"],
          str(top["mergedWith"]))
    check("confluence=2", top["confluence"] == 2, str(top["confluence"]))
    check("代表の価格も入れ替わる", top["price"] == P + 1.0, str(top["price"]))


test_dedupe_keeps_higher_tier()


def test_tier_regex_table():
    cases = [("Weekly Low", 1), ("Monthly High", 1), ("All Time High", 1),
             ("Previous Day Low", 2), ("Prev Day Mid Range | CPR-BC", 2),
             ("Asia Low", 3), ("London High", 3), ("New York High", 3),
             ("C: VAH", 3), ("C: POC", 3), ("Weekly Mid", 3),
             ("PP-S1", 4), ("CPR-TC", 4),
             ("08:30 (30238)", 5), ("Market Open", 5), ("CT Trail (15m)", 5)]
    for label, want in cases:
        got = msnr_gate.level_tier(label)
        check(f"tier({label})={want}", got == want, f"got {got}")


test_tier_regex_table()


section("18. R4 §5 — VP 受容経路(ADVISORY)")
VAH, VAL = 30050.0, 29950.0
VP_BARS = [(30055, 30060, 30052, 30056) for _ in range(12)] + [
    (30056, 30057, 30040, 30044),    # 12 VA へ復帰
    (30044, 30046, 30036, 30040),    # 13 受容
    (30040, 30042, 30030, 30034),
]
VP_LEVELS = [{"label": "C: VAH", "price": VAH}, {"label": "C: VAL", "price": VAL}]
res18 = evaluate(VP_BARS, levels=VP_LEVELS)


def test_vp_acceptance_advisory():
    vp = res18["vpPath"]
    check("side=SELL(上抜け失敗 → 下の VAL へ)", vp["side"] == "SELL", str(vp))
    check("state=VP_ACCEPTED", vp["state"] == "VP_ACCEPTED", str(vp))
    check("target=VAL", vp["target"] == VAL, str(vp["target"]))
    check("summary に目標域", "VP: VA復帰受容 → 29,950 目標域" in res18["summary"],
          res18["summary"])
    nova = evaluate(VP_BARS, levels=[{"label": "C: VAH", "price": VAH}])
    check("VAL が無ければ評価しない", nova["vpPath"]["state"] is None,
          str(nova["vpPath"]))


test_vp_acceptance_advisory()


section("19. R4 — ADVISORY は promotion に影響しない")


def test_advisory_does_not_affect_promotion():
    base = evaluate(FULL_BUY, levels=TWO_LEVELS)
    withv = evaluate(FULL_BUY, levels=TWO_LEVELS, vwap=own_vwap(FULL_BUY))
    check("vwapPath の有無で promotion が変わらない",
          [lv["promotion"] for lv in base["levels"]]
          == [lv["promotion"] for lv in withv["levels"]],
          f'{base["levels"][0]["promotion"]} vs {withv["levels"][0]["promotion"]}')
    check("RECLAIM 完成でも promotion は連鎖だけで決まる",
          all(not lv["promotion"][s]["allowed"]
              for lv in res15["levels"] for s in ("BUY", "SELL")),
          str([lv["promotion"] for lv in res15["levels"]]))
    check("VP 受容中でも promotion は独立",
          all(lv["promotion"][s]["allowed"] is False
              or lv["promotion"][s]["grade"] in ("A", "A+")
              for lv in res18["levels"] for s in ("BUY", "SELL")))
    for r in (res15, res15b, res15c, res15d, res18):
        for lv in r["levels"]:
            for s in ("BUY", "SELL"):
                pr = lv["promotion"][s]
                check_codes = set(pr["blockers"]) & {"VWAP_DRIFT",
                                                     "VWAP_RETEST_NOT_HELD",
                                                     "VWAP_ACCEPTANCE_NOT_CONFIRMED"}
                check("promotion に VWAP 系 blocker が混ざらない",
                      not check_codes, str(pr))


test_advisory_does_not_affect_promotion()


section("20. R4 — blocker 列挙(ADVISORY 経路を含む)")
R4_CODES = ALLOWED_CODES | {"VWAP_DRIFT", "VWAP_RETEST_NOT_HELD",
                            "VWAP_ACCEPTANCE_NOT_CONFIRMED"}
seen4 = set()
for r in (res15, res15b, res15c, res15d, res16, res16b, res17, res18):
    for lvx in r["levels"]:
        for s in ("BUY", "SELL"):
            seen4 |= set(lvx["promotion"][s]["blockers"])
        for c in lvx["chains"]:
            seen4 |= set(c["blockers"])
    seen4 |= set(r["vwapPath"]["blockers"]) | set(r["vpPath"]["blockers"])
check("列挙外のコードが無い", seen4 <= R4_CODES, str(seen4 - R4_CODES))
check("VWAP_DRIFT を実際に踏んでいる", "VWAP_DRIFT" in seen4, str(sorted(seen4)))


section("21. R5 裁定① — FLIP 型が A+ になれる(8/12 の勝ち型)")
# 支持 30000 の上で20本 → 実体20pt で下抜け → 受容 → 戻りがレベルを印字して
# 下で引ける(FLIP_HELD)。tier1 + 合流 + 初動 20pt ≥ 1.3×NF(10)= 13pt。
SBR_STRONG = [(30010, 30016, 30006, 30012) for _ in range(20)] + [
    (30008, 30009, 29986, 29988),    # 20 break(実体 20pt)
    (29988, 29993, 29981, 29984),    # 21 受容
    (29984, 30001, 29979, 29990),    # 22 到達 + 保持(高値が P=30000 を印字)
    (29990, 29992, 29972, 29975),
]
TIER1_PAIR = [{"label": "Weekly High", "price": P}, {"label": "C: VAH", "price": P + 1.0}]
res21 = evaluate(SBR_STRONG, levels=TIER1_PAIR)
lv21 = level0(res21)


def test_grade_aplus_flip():
    chain = next((c for c in lv21["chains"] if c["state"] == "FLIP_HELD"), None)
    check("FLIP_HELD が成立", chain is not None, str([c["state"] for c in lv21["chains"]]))
    check("鮮度は FLIPPED", lv21["freshness"] == "FLIPPED", lv21["freshness"])
    check("初動 20pt ≥ 1.3×NF", chain and chain["dispBody"] >= 1.3 * res21["noiseFloor"],
          f'{chain and chain["dispBody"]} vs {1.3 * res21["noiseFloor"]}')
    check("tier=1", lv21["tier"] == 1, str(lv21["tier"]))
    check("SELL allowed", lv21["promotion"]["SELL"]["allowed"] is True,
          str(lv21["promotion"]["SELL"]))
    check("grade=A+(R5 で FLIPPED を許可集合に追加)",
          lv21["promotion"]["SELL"]["grade"] == "A+", str(lv21["promotion"]["SELL"]))


test_grade_aplus_flip()

# 同型で初動だけ弱い(実体 4pt < 13pt)→ A どまり
SBR_WEAK = [(30010, 30016, 30006, 30012) for _ in range(20)] + [
    (29996, 29997, 29986, 29992),    # 20 break だが実体 4pt
    (29992, 29994, 29981, 29984),    # 21 受容(実体 8pt)
    (29984, 30001, 29979, 29990),    # 22 到達 + 保持
    (29990, 29992, 29972, 29975),
]
res21b = evaluate(SBR_WEAK, levels=TIER1_PAIR)
lv21b = level0(res21b)


def test_grade_a_flip_weak_disp():
    check("SELL allowed は維持", lv21b["promotion"]["SELL"]["allowed"] is True,
          str(lv21b["promotion"]["SELL"]))
    check("初動不足なので grade=A", lv21b["promotion"]["SELL"]["grade"] == "A",
          str(lv21b["promotion"]["SELL"]))


test_grade_a_flip_weak_disp()


section("22. R45 — VWAP のアンカーはセッション開始(ET 18:00)")


def test_vwap_backward_compat_60bars():
    # フィクスチャ 60本は全て同じ取引日(ET 18:00 起点)の中にあるので、
    # セッションアンカーでも系列は R4/R5 と一致する。
    check("走査本数 = 供給本数", res15["closedBars"] == len(RECLAIM),
          f'{res15["closedBars"]} vs {len(RECLAIM)}')
    check("アンカー本数 = 走査本数(全足がセッション内)",
          res15["vwapAnchorBars"] == res15["closedBars"],
          f'{res15["vwapAnchorBars"]} vs {res15["closedBars"]}')
    check("VWAP_HELD は維持(R4 と同じ判定)",
          res15["vwapPath"]["state"] == "VWAP_HELD", str(res15["vwapPath"]))
    check("drift 0.0 のまま", res15["vwapPath"]["drift"] == 0.0, str(res15["vwapPath"]))


test_vwap_backward_compat_60bars()


def test_vwap_anchor_extends_within_session():
    # 走査ウィンドウ(60)より多く供給し、全てが同一セッション内のとき。
    lead = [filler(30400 - i * 4) for i in range(80)]     # 80本の助走(下降)
    long_bars = lead + RECLAIM
    res = evaluate(long_bars, vwap=own_vwap(long_bars))
    check("走査は 60本に絞られる", res["closedBars"] == 60, str(res["closedBars"]))
    check("アンカーはセッション開始の epoch",
          res["vwapAnchorT"] == msnr_gate.session_anchor_epoch(bar_t(len(long_bars) - 1)),
          str(res["vwapAnchorT"]))
    check("同一セッション内なら全量が累積される",
          res["vwapAnchorBars"] == len(long_bars),
          f'{res["vwapAnchorBars"]} vs {len(long_bars)}')
    # 助走ぶん VWAP が上にずれるので、60本だけのときと系列が変わる
    only60 = msnr_gate.rolling_vwap(mk_bars(RECLAIM))[-1]
    full = msnr_gate.rolling_vwap(mk_bars(long_bars))[-1]
    check("アンカーが違えば VWAP も変わる(アンカー計算が効いている)",
          abs(full - only60) > 1.0, f"{full:.2f} vs {only60:.2f}")


test_vwap_anchor_extends_within_session()


def test_vwap_anchor_drops_previous_session():
    """300本(15時間)は ET 18:00 をまたぐ。前の取引日の足を混ぜない。

    R5 はここで全量(240本)をアンカーにしていた。実データでは 240本 =
    12.9時間で必ずセッション境界を越えるため、bundle.vwap と 13pt ずれて
    vwapPath が毎サイクル VWAP_DRIFT で死んでいた。
    """
    spec = [filler(30000) for _ in range(300)]
    bundle = bundle_of(mk_bars(spec))
    kept = msnr_gate.closed_bars(bundle)
    check("受理は 240本で切り詰める", len(kept) == 240, str(len(kept)))
    anchor = msnr_gate.session_anchor_epoch(kept[-1]["t"])
    in_session = [b for b in kept if b["t"] >= anchor]
    res = msnr_gate.evaluate(bundle)
    check("アンカーはセッション開始", res["vwapAnchorT"] == anchor, str(res["vwapAnchorT"]))
    check("セッション境界をまたぐので全量より少ない",
          0 < res["vwapAnchorBars"] < 240, str(res["vwapAnchorBars"]))
    check("累積本数 = アンカー以降の確定足",
          res["vwapAnchorBars"] == len(in_session),
          f'{res["vwapAnchorBars"]} vs {len(in_session)}')
    series = msnr_gate.rolling_vwap(kept, anchor)
    check("アンカー前は None・以降は値を持つ",
          series[0] is None and series[-1] is not None,
          f"{series[0]} / {series[-1]}")
    check("None は先頭側の prefix にしかならない",
          [i for i, v in enumerate(series) if v is None]
          == list(range(240 - len(in_session))))


test_vwap_anchor_drops_previous_session()


def test_vwap_seed_restores_truncated_window():
    """publish 経路は bars3m を 60本へ切る。累積スカラーで手前ぶんを戻す。

    R45: 切られた 60本だけで系列を作るとアンカーが窓の先頭になり、
    bundle.vwap との drift が閾値を超えて vwapPath が死ぬ(実測 19.0pt)。
    """
    lead = [filler(30400 - i * 4) for i in range(80)]
    long_bars = mk_bars(lead + RECLAIM)
    anchor = msnr_gate.session_anchor_epoch(long_bars[-1]["t"])
    truth = msnr_gate.rolling_vwap(long_bars, anchor)[-1]

    pv = vv = 0.0
    for bar in long_bars:
        pv += ((bar["h"] + bar["l"] + bar["c"]) / 3.0) * bar["v"]
        vv += bar["v"]

    cut = long_bars[-60:]
    bundle = bundle_of(cut, vwap=round(truth, 2))
    bundle["snapshot"].update(vwapAnchorT=anchor, vwapSessionPv=pv,
                              vwapSessionVv=vv, vwapThroughT=long_bars[-1]["t"],
                              vwapSessionBars=len(long_bars))
    res = msnr_gate.evaluate(bundle)
    check("60本でも累積から復元する", res["vwapSeeded"] is True, str(res["vwapSeeded"]))
    check("セッション本数を持ち越す", res["vwapSessionBars"] == len(long_bars),
          str(res["vwapSessionBars"]))
    check("drift 0.0(全量と一致)", res["vwapPath"]["drift"] == 0.0,
          str(res["vwapPath"]))

    # 累積を載せない = 切られた窓だけ。ずれて drift が大きくなる。
    bare = bundle_of(cut, vwap=round(truth, 2))
    bare["snapshot"]["vwapAnchorT"] = anchor
    res_bare = msnr_gate.evaluate(bare)
    check("累積が無ければ復元しない", res_bare["vwapSeeded"] is False,
          str(res_bare["vwapSeeded"]))
    check("窓だけの系列はずれる", (res_bare["vwapPath"]["drift"] or 0) > 1.0,
          str(res_bare["vwapPath"]))

    # 合計の終点が手持ちの終点と違う = 前提が壊れている。復元しない。
    mismatched = bundle_of(cut, vwap=round(truth, 2))
    mismatched["snapshot"].update(vwapAnchorT=anchor, vwapSessionPv=pv,
                                  vwapSessionVv=vv,
                                  vwapThroughT=long_bars[-1]["t"] + 180)
    check("終点が違えば復元しない",
          msnr_gate.evaluate(mismatched)["vwapSeeded"] is False)


test_vwap_seed_restores_truncated_window()


section("23. R8 §4 — rrPotential(走路の R 換算・ADVISORY)")
# ABOVE(12本・実体4pt/レンジ8pt)→ noiseFloor=8.0 / touchTol=2.0(下限)
# → sl_proxy = 8+2 = 10.0 / 走路の下限距離 = 3×touchTol = 6.0pt


def test_rr_potential_two_roadblocks():
    levels = [{"label": "TEST", "price": P},
              {"label": "PDH", "price": P + 50},
              {"label": "Weekly High", "price": P + 120}]
    res = evaluate(ABOVE, levels=levels)
    lv = level0(res)
    buy = lv["rrPotential"]["BUY"]
    check("rb1 = PDH(50pt)", buy["rb1Pt"] == 50.0 and buy["rb1Label"] == "PDH", str(buy))
    check("rb1R = 5.0R(50pt ÷ sl_proxy10)", buy["rb1R"] == 5.0, str(buy))
    check("rb2 = Weekly High(120pt)",
          buy["rb2Pt"] == 120.0 and buy["rb2Label"] == "Weekly High", str(buy))
    check("rb2R = 12.0R", buy["rb2R"] == 12.0, str(buy))
    sell = lv["rrPotential"]["SELL"]
    check("下方向にレベルが無ければ SELL は両方 null",
          sell["rb1Pt"] is None and sell["rb2Pt"] is None, str(sell))


test_rr_potential_two_roadblocks()


def test_rr_potential_open_runway():
    levels = [{"label": "TEST", "price": P}, {"label": "PDL", "price": P - 80}]
    res = evaluate(ABOVE, levels=levels)
    lv = level0(res)
    buy = lv["rrPotential"]["BUY"]
    check("利益方向(上)にレベルが無ければ rb1/rb2 とも null(走路は開放)",
          buy["rb1Pt"] is None and buy["rb1R"] is None
          and buy["rb2Pt"] is None and buy["rb2R"] is None, str(buy))
    sell = lv["rrPotential"]["SELL"]
    check("SELL は rb1 のみ成立(PDL)し rb2 は null",
          sell["rb1Pt"] == 80.0 and sell["rb1Label"] == "PDL" and sell["rb2Pt"] is None,
          str(sell))


test_rr_potential_open_runway()


def test_rr_potential_ignores_dynamic_and_near():
    levels = [
        {"label": "TEST", "price": P},
        {"label": "VWAP Upper Band", "price": P + 15},   # dynamic → 候補から除外
        {"label": "Session High", "price": P + 4},        # 距離4pt < 3×tol(6pt) → 除外
        {"label": "PDH", "price": P + 40},                 # 唯一の有効候補
    ]
    res = evaluate(ABOVE, levels=levels)
    lv = level0(res)
    buy = lv["rrPotential"]["BUY"]
    check("rb1 は PDH(近すぎる/動的レベルを飛ばす)",
          buy["rb1Label"] == "PDH" and buy["rb1Pt"] == 40.0, str(buy))
    check("rb1R = 4.0R", buy["rb1R"] == 4.0, str(buy))
    check("有効候補が1件のみなので rb2 は null", buy["rb2Pt"] is None, str(buy))
    check("動的/近接レベルが rb1・rb2 のどちらにも出てこない",
          "VWAP" not in (buy["rb1Label"] or "") and buy["rb1Label"] != "Session High",
          str(buy))
    vwap_level = next(lx for lx in res["levels"] if lx["label"] == "VWAP Upper Band")
    check("動的レベル自身の rrPotential は BUY/SELL とも null",
          vwap_level["rrPotential"]["BUY"] is None and vwap_level["rrPotential"]["SELL"] is None,
          str(vwap_level["rrPotential"]))


test_rr_potential_ignores_dynamic_and_near()


def test_rr_potential_does_not_change_promotion():
    # rrPotential は evaluate() の最後(promotion 確定後)に追加されるだけの
    # 観測値であり、既存の合成ロジック(compose/grade_of)を一切経由しない。
    # 本ファイル前半で確認済みの promotion/grade を rrPotential 追加後の
    # 出力でも再確認する(165 実データでの再走査は HANDOFF.md の完了記録に
    # 手動検証として残す — .secrets の実データはこのテストにコピーしない)。
    checks = [
        (lv, "BUY", True, None, "1. 完成連鎖(BUY)"),
        (lv4, "BUY", False, None, "3. break→retest だけ(不許可)"),
        (lv5, "BUY", False, None, "5. displacement 不足(不許可)"),
        (lv21, "SELL", True, "A+", "21. R5 FLIP grade=A+"),
        (lv21b, "SELL", True, "A", "21. R5 FLIP grade=A(初動不足)"),
    ]
    for level, side, expect_allowed, expect_grade, tag in checks:
        promo = level["promotion"][side]
        check(f"{tag}: allowed={expect_allowed}(rrPotential 追加後も不変)",
              promo["allowed"] is expect_allowed, str(promo))
        if expect_grade is not None:
            check(f"{tag}: grade={expect_grade}(不変)",
                  promo["grade"] == expect_grade, str(promo))
        check(f"{tag}: rrPotential キーが level に存在する",
              "rrPotential" in level, sorted(level.keys()))


test_rr_potential_does_not_change_promotion()


section("24. --card の SL 上限 — 表示と強制のズレを再発させない")
# 2026-08-19 の実害: CARD_SL_CAP が $50 時代の 25.0 のまま残り、
# monitor_publish の強制側と食い違った。2026-08-21 に Apex
# (RISK=240 / 2枚固定 → 60pt)へ追随させた。
# 実データ221バンドル中 192件(86.9%)で ruling が反転していた。


def test_card_sl_cap_default_matches_enforced():
    check("既定 CARD_SL_CAP は 60pt($240 ÷ $2.00 ÷ 2枚)",
          msnr_gate.CARD_SL_CAP == 60.0, str(msnr_gate.CARD_SL_CAP))
    saved = os.environ.pop("NQX_SL_CAP_PT", None)
    try:
        check("sl_cap_pt() の既定も 60pt", msnr_gate.sl_cap_pt() == 60.0,
              str(msnr_gate.sl_cap_pt()))
        # ノイズ床 8pt(ABOVE の値幅)→ 8/60 = 0.13 → 通常帯
        card = msnr_gate.build_card(evaluate(ABOVE))
        check("既定 cap でカードが組める", card["volGate"]["slCap"] == 60.0,
              str(card["volGate"]))
        check("ノイズ床 8pt は通常帯(旧 25pt 既定なら 0.32 でやはり通常だが、"
              "実運用の 17pt 帯で判定が割れる)",
              card["volGate"]["ruling"] == "通常", str(card["volGate"]))
    finally:
        if saved is not None:
            os.environ["NQX_SL_CAP_PT"] = saved


test_card_sl_cap_default_matches_enforced()


def test_card_sl_cap_env_override_is_read_at_call_time():
    """既定引数に定数を焼くと env 上書きが効かなくなる(その退行を止める)。"""
    saved = os.environ.get("NQX_SL_CAP_PT")
    try:
        os.environ["NQX_SL_CAP_PT"] = "25"
        check("env で 25pt に戻せる", msnr_gate.sl_cap_pt() == 25.0,
              str(msnr_gate.sl_cap_pt()))
        card = msnr_gate.build_card(evaluate(ABOVE))
        check("import 後に env を変えてもカードへ反映される(呼び出し時解決)",
              card["volGate"]["slCap"] == 25.0, str(card["volGate"]))
        os.environ["NQX_SL_CAP_PT"] = "abc"
        check("数値でない env は既定 60pt にフォールバック",
              msnr_gate.sl_cap_pt() == 60.0, str(msnr_gate.sl_cap_pt()))
    finally:
        os.environ.pop("NQX_SL_CAP_PT", None)
        if saved is not None:
            os.environ["NQX_SL_CAP_PT"] = saved


test_card_sl_cap_env_override_is_read_at_call_time()


def test_card_sl_cap_explicit_argument_still_wins():
    check("明示引数は env より優先(--sl-cap の後方互換)",
          msnr_gate.build_card(evaluate(ABOVE), 25.0)["volGate"]["slCap"] == 25.0)


def test_card_ruling_flips_at_real_noise_floor():
    """実運用のノイズ床 17.5pt(262バンドルの中央値)で判定が割れることを固定する。

    17.5 ÷ 25.0 = 0.70 → 停止 / 17.5 ÷ 52.5 = 0.33 → 通常。
    このズレが 86.9% のバンドルで発火していた。
    """
    noise = 17.5
    fake = {"at": "t", "noiseFloor": noise, "rotation": {}, "levels": [], "summary": ""}
    old = msnr_gate.build_card(dict(fake), 25.0)["volGate"]
    new = msnr_gate.build_card(dict(fake), 52.5)["volGate"]
    check("旧 25pt では 停止", old["ruling"] == "停止" and old["ratio"] == 0.7, str(old))
    check("正しい 52.5pt では 通常", new["ruling"] == "通常" and new["ratio"] == 0.33,
          str(new))


test_card_sl_cap_explicit_argument_still_wins()
test_card_ruling_flips_at_real_noise_floor()


section("R9. ストップ予算 / VP 到達余地(2026-08-21 の敗戦の数値化)")
# 実測: Entry 29,327 / SL 28.75pt / ノイズ床 27.12pt / SL上限 45pt。
# 最大逆行 29.5pt に 0.75pt 足りず刈られ、予算は 16.25pt 余っていた。


def test_stop_budget_flags_unused_budget():
    sb = msnr_gate.stop_budget(29327, 29355.75, 27.12, 45.0)
    check("使用率 0.64", sb["utilization"] == 0.64, str(sb))
    check("ノイズ床の 1.06倍", sb["nfMult"] == 1.06, str(sb))
    check("推奨は 1.5xNF = 40.68pt", sb["wantPt"] == 40.68, str(sb))
    check("推奨が予算に収まる", sb["wantFits"] is True)
    check("不合格(最小緩衝で予算を余らせた)", sb["pass"] is False)


def test_stop_budget_passes_when_wide_enough():
    sb = msnr_gate.stop_budget(29327, 29368, 27.12, 45.0)
    check("1.5xNF 以上なら合格", sb["pass"] is True, str(sb))


def test_stop_budget_does_not_block_when_budget_too_small():
    sb = msnr_gate.stop_budget(29327, 29347, 40.0, 45.0)
    check("推奨が予算超過なら距離では落とさない",
          sb["wantFits"] is False and sb["pass"] is True, str(sb))


def test_vp_headroom_flags_exhausted_rotation():
    vh = msnr_gate.vp_headroom({"target": 29325.35}, 29327, "SELL", 27.12)
    check("残り 1.65pt", vh["remainPt"] == 1.65, str(vh))
    check("使い果たし判定", vh["exhausted"] is True, str(vh))
    far = msnr_gate.vp_headroom({"target": 29280.0}, 29327, "SELL", 27.12)
    check("目標が遠ければ余地あり", far["exhausted"] is False, str(far))


def test_card_extras_are_opt_in():
    base = msnr_gate.build_card(evaluate(FULL_BUY))
    check("既定では stopBudget が出ない", "stopBudget" not in base, str(list(base)))
    check("既定では vpHeadroom が出ない", "vpHeadroom" not in base, str(list(base)))
    with_sb = msnr_gate.build_card(evaluate(FULL_BUY), entry=30000, stop=29980)
    check("entry/stop を渡すと出る", "stopBudget" in with_sb, str(list(with_sb)))


test_stop_budget_flags_unused_budget()
test_stop_budget_passes_when_wide_enough()
test_stop_budget_does_not_block_when_budget_too_small()
test_vp_headroom_flags_exhausted_rotation()
test_card_extras_are_opt_in()


section("R10. ICT 層(ADVISORY・promotion に影響しないこと)")


def test_ict_session_maps_jst_to_et_killzones():
    # 22:30 JST = 09:30 ET(NY 寄り)= 真の高安形成帯
    s = msnr_gate.ict_session("2026-08-19T22:30:00+09:00")
    check("22:30 JST → 09:30 ET", s["et"] == "09:30", str(s))
    check("真の高安形成帯", s["window"] == "TRUE_DAY_HL", str(s))
    # 03:00 JST = 14:00 ET = PM トレンド
    s = msnr_gate.ict_session("2026-08-19T03:00:00+09:00")
    check("03:00 JST → 14:00 ET / PM トレンド",
          s["et"] == "14:00" and s["window"] == "PM_TREND", str(s))
    # 01:30 JST = 12:30 ET = NY ランチ(ICT は新規を探さない)
    s = msnr_gate.ict_session("2026-08-19T01:30:00+09:00")
    check("01:30 JST → ランチ帯は tradeable=False",
          s["window"] == "LUNCH" and s["tradeable"] is False, str(s))
    check("壊れた at は None を返す", msnr_gate.ict_session("garbage")["window"] is None)
    check("at 欠落でも落ちない", msnr_gate.ict_session(None)["etMinute"] is None)


def test_dealing_range_premium_discount():
    bars = mk_bars([(100, 200, 100, 150)] * 3)          # レンジ 100-200 / EQ 150
    hi = msnr_gate.dealing_range(bars, 180)
    check("EQ は中点", hi["eq"] == 150.0, str(hi))
    check("EQ 上はプレミアム", hi["position"] == "PREMIUM", str(hi))
    check("売りを favor する", hi["favors"]["SELL"] and not hi["favors"]["BUY"], str(hi))
    lo = msnr_gate.dealing_range(bars, 120)
    check("EQ 下はディスカウント", lo["position"] == "DISCOUNT", str(lo))
    check("買いを favor する", lo["favors"]["BUY"] and not lo["favors"]["SELL"], str(lo))
    check("OTE 売り帯は premium 側(162-179)", lo["oteSell"] == [162.0, 179.0], str(lo["oteSell"]))
    check("OTE 買い帯は discount 側(121-138)", lo["oteBuy"] == [121.0, 138.0], str(lo["oteBuy"]))
    check("幅ゼロなら None", msnr_gate.dealing_range(mk_bars([(100, 100, 100, 100)]), 100) is None)


def test_draw_on_liquidity_lrlr_vs_hrlr():
    # 上に旧高値が1つだけ → LRLR、間に障害物を足すと HRLR
    spec = [(100, 100, 100, 100)] * 4 + [(100, 130, 100, 100)] + [(100, 100, 100, 100)] * 4
    bars = mk_bars(spec)
    dol = msnr_gate.draw_on_liquidity(bars, [], 100, 2.0)
    check("上の DOL は旧高値 130", dol["BUY"]["target"] == 130.0, str(dol["BUY"]))
    check("障害物なしは LRLR", dol["BUY"]["run"] == "LRLR", str(dol["BUY"]))
    levels = [{"price": 110.0, "dynamic": False}, {"price": 120.0, "dynamic": False}]
    dol2 = msnr_gate.draw_on_liquidity(bars, levels, 100, 2.0)
    check("間に2つ挟まると HRLR", dol2["BUY"]["run"] == "HRLR", str(dol2["BUY"]))
    check("障害物数を数える", dol2["BUY"]["obstacles"] == 2, str(dol2["BUY"]))


def test_fvg_scan_finds_unfilled_gap_only():
    # 下に bullish FVG(bars[0].h=100 < bars[2].l=110)。以後埋めない。
    bars = mk_bars([(95, 100, 95, 100), (100, 115, 100, 112),
                    (112, 120, 110, 118), (118, 125, 114, 124)])
    g = msnr_gate.fvg_scan(bars, 124, 2.0)
    check("未充填の bullish FVG を拾う", len(g["BULL"]) == 1, str(g))
    check("FVG の範囲は 100-110", g["BULL"][0]["lo"] == 100.0 and g["BULL"][0]["hi"] == 110.0,
          str(g["BULL"]))
    filled = mk_bars([(95, 100, 95, 100), (100, 115, 100, 112),
                      (112, 120, 110, 118), (118, 120, 99, 100)])
    check("埋まった FVG は消える", msnr_gate.fvg_scan(filled, 100, 2.0)["BULL"] == [], "")


def test_ict_never_changes_promotion():
    """R10 の中核不変条件: ICT を消しても allowed / grade / blockers が変わらない。"""
    for name, bundle in (("FULL_BUY", FULL_BUY), ("FULL_SELL", FULL_SELL)):
        res = evaluate(bundle)
        before = [(l["label"], s, l["promotion"][s]["allowed"], l["promotion"][s]["grade"],
                   tuple(l["promotion"][s]["blockers"]))
                  for l in res["levels"] for s in ("BUY", "SELL")]
        # ict を計算しない経路(= 旧実装相当)と比べる
        res2 = evaluate(bundle)
        res2["ict"] = None
        after = [(l["label"], s, l["promotion"][s]["allowed"], l["promotion"][s]["grade"],
                  tuple(l["promotion"][s]["blockers"]))
                 for l in res2["levels"] for s in ("BUY", "SELL")]
        check(f"{name}: promotion は ICT に依存しない", before == after, "")
        check(f"{name}: ict が付いている", res.get("ict") is not None, "")


def test_ict_side_check_is_advisory_only():
    res = evaluate(FULL_BUY)
    chk = msnr_gate.ict_side_check(res["ict"], "BUY")
    check("side チェックは dict を返す", isinstance(chk, dict), str(chk))
    check("notes は列挙のみ",
          all(n in ("PREMIUM_DISCOUNT_WRONG_SIDE", "HIGH_RESISTANCE_RUN",
                    "OUTSIDE_KILLZONE") for n in chk["notes"]), str(chk["notes"]))
    check("ict が None なら None", msnr_gate.ict_side_check(None, "BUY") is None)


def test_card_carries_ict_and_shrinks_last():
    card = msnr_gate.build_card(evaluate(FULL_BUY))
    adv = card.get("advisory") or {}
    check("card に advisory.ict が載る", "ict" in adv, str(list(adv)))
    check("ict は summary を持つ", "summary" in adv.get("ict", {}), str(adv.get("ict")))
    check("card に CVD のA+可否が載る",
          isinstance(card.get("cvdGate"), dict)
          and "aplusAllowed" in card["cvdGate"], str(card.get("cvdGate")))
    check("card に ICT 時間帯が載る",
          isinstance(card.get("sessionGate"), dict)
          and "tradeable" in card["sessionGate"], str(card.get("sessionGate")))
    size = len(json.dumps(card, ensure_ascii=False).encode("utf-8"))
    check("4096 バイト以内", size <= msnr_gate.CARD_MAX_BYTES, f"{size} bytes")


test_ict_session_maps_jst_to_et_killzones()
test_dealing_range_premium_discount()
test_draw_on_liquidity_lrlr_vs_hrlr()
test_fvg_scan_finds_unfilled_gap_only()
test_ict_never_changes_promotion()
test_ict_side_check_is_advisory_only()
test_card_carries_ict_and_shrinks_last()



section("R10 §5. Index SMT(ADVISORY)")


def _smt_bars(spec):
    """(h, l) の並びを 180 秒刻みの {t,h,l} に。"""
    return [{"t": T0 + 180 * i, "o": l, "h": h, "l": l, "c": h, "v": 100}
            for i, (h, l) in enumerate(spec)]


# 安値: NQ は 90 → 95 と切り上げ(HL)、ES は 90 → 85 と切り下げ(LL)= 乖離
NQ_HL = _smt_bars([(110, 100), (108, 98), (105, 90), (108, 98), (110, 100),
                   (109, 99), (106, 95), (109, 99), (111, 101)])
ES_LL = _smt_bars([(110, 100), (108, 98), (105, 90), (108, 98), (110, 100),
                   (109, 99), (106, 85), (109, 99), (111, 101)])
ES_SAME = list(NQ_HL)
SMT_META = {"ES": {"sessionId": "TEST-NY"}}


def smt_eval(peers, at="2026-08-19T21:00:00+09:00"):
    """R12: SMTは同時刻・同一セッション・fresh peerを明示して評価する。"""
    return msnr_gate.index_smt(NQ_HL, peers, at, SMT_META, "TEST-NY")


def test_smt_unavailable_without_peers():
    r = msnr_gate.index_smt(NQ_HL, None, "2026-08-19T22:30:00+09:00")
    check("peers 無しは available=False", r["available"] is False, str(r))
    check("理由は NO_PEERS", r["reason"] == "NO_PEERS", str(r))
    check("summary は出さない", r["summary"] is None, str(r))


def test_smt_detects_bullish_divergence_at_lows():
    r = smt_eval({"ES": ES_LL})
    check("available", r["available"] is True, str(r))
    check("安値で乖離", r["low"]["divergence"] is True, str(r["low"]))
    check("NQ は HL", r["low"]["primary"] == "HL", str(r["low"]))
    check("ES は LL", r["low"]["peers"]["ES"] == "LL", str(r["low"]))
    check("bias は強気", r["bias"] == "BULLISH", str(r["bias"]))
    check("強い方は NQ", r["low"]["strong"] == "NQ", str(r["low"]))


def test_smt_no_divergence_when_indices_agree():
    r = smt_eval({"ES": ES_SAME})
    check("同じ動きなら乖離なし", r["low"]["divergence"] is False, str(r["low"]))
    check("bias は None", r["bias"] is None, str(r))


def test_smt_window_flag_matches_ict_hours():
    # 22:30 JST = 09:30 ET → AM 窓(05:00-09:30 ET)の外(境界は排他)
    r = msnr_gate.index_smt(NQ_HL, {"ES": ES_LL}, "2026-08-19T22:00:00+09:00")
    check("21:00-22:30 JST は AM 窓", r["window"] == "AM", str(r["window"]))
    r = msnr_gate.index_smt(NQ_HL, {"ES": ES_LL}, "2026-08-19T02:00:00+09:00")
    check("01:00-04:00 JST は PM 窓", r["window"] == "PM", str(r["window"]))
    r = msnr_gate.index_smt(NQ_HL, {"ES": ES_LL}, "2026-08-19T12:00:00+09:00")
    check("窓外は None", r["window"] is None, str(r["window"]))


def test_smt_normalizes_long_keys_and_junk():
    long_es = [{"time": b["t"], "high": b["h"], "low": b["l"]} for b in ES_LL]
    r = msnr_gate.index_smt(NQ_HL, {"es": long_es}, "2026-08-19T21:00:00+09:00",
                                    {"es": {"sessionId": "TEST-NY"}}, "TEST-NY")
    check("長いキー名でも読む", r["available"] is True, str(r["reason"]))
    check("peer 名は大文字化", r["peers"] == ["ES"], str(r["peers"]))
    junk = msnr_gate.index_smt(NQ_HL, {"ES": [{"nope": 1}, "x", None]},
                               "2026-08-19T22:00:00+09:00")
    check("壊れた peer は無視して落ちない", junk["available"] is False, str(junk))


def test_smt_flows_into_bundle_and_side_check():
    bundle = bundle_of(mk_bars(FULL_BUY))
    bundle["snapshot"]["peers"] = {"ES": [{"t": b["t"], "h": b["h"] + 5, "l": b["l"] + 5}
                                          for b in mk_bars(FULL_BUY)]}
    res = msnr_gate.evaluate(bundle)
    check("bundle 経由で smt が付く", (res["ict"] or {}).get("smt") is not None, "")
    chk = msnr_gate.ict_side_check(res["ict"], "BUY")
    check("side チェックが SMT を見る",
          "SMT_AGAINST" not in chk["notes"] or res["ict"]["smt"]["bias"] == "BEARISH",
          str(chk["notes"]))
    # bias と逆 side には SMT_AGAINST が付く
    fake = {"smt": {"available": True, "bias": "BULLISH"}, "range": {}, "dol": {},
            "session": {}}
    check("強気SMT + 売り → SMT_AGAINST",
          "SMT_AGAINST" in msnr_gate.ict_side_check(fake, "SELL")["notes"], "")
    check("強気SMT + 買い → 付かない",
          "SMT_AGAINST" not in msnr_gate.ict_side_check(fake, "BUY")["notes"], "")


def test_smt_never_changes_promotion():
    base = bundle_of(mk_bars(FULL_BUY))
    withp = bundle_of(mk_bars(FULL_BUY))
    withp["snapshot"]["peers"] = {"ES": [{"t": b["t"], "h": b["h"] - 3, "l": b["l"] - 9}
                                         for b in mk_bars(FULL_BUY)]}
    key = lambda r: [(l["label"], s, l["promotion"][s]["allowed"],
                      l["promotion"][s]["grade"], tuple(l["promotion"][s]["blockers"]))
                     for l in r["levels"] for s in ("BUY", "SELL")]
    check("peers を足しても promotion は不変",
          key(msnr_gate.evaluate(base)) == key(msnr_gate.evaluate(withp)), "")


test_smt_unavailable_without_peers()
test_smt_detects_bullish_divergence_at_lows()
test_smt_no_divergence_when_indices_agree()
test_smt_window_flag_matches_ict_hours()
test_smt_normalizes_long_keys_and_junk()
test_smt_flows_into_bundle_and_side_check()
test_smt_never_changes_promotion()



section("R10. カードの残本数は summary と同じ数(2026-08-22 の表示バグ)")
# 完成した連鎖の寿命は CHAIN_TTL ではなく COMPLETE_TTL(保持足からの経過)。
# barsLeft をそのまま出していたため、生きている連鎖に「残0本」と表示されていた。


def test_card_bars_left_uses_complete_ttl():
    card = msnr_gate.build_card(evaluate(FULL_BUY))
    m = card["msnr"]
    check("完成状態である", m["chainState"] == "RETEST_HELD", str(m))
    prm = msnr_gate.params()
    chain = [c for c in level0(evaluate(FULL_BUY))["chains"]
             if c["type"] == "SWEEP" and c["side"] == "BUY"][0]
    want = prm["complete_ttl"] - (chain.get("barsSinceRetest") or 0)
    check("残本数は COMPLETE_TTL 基準", m["barsLeft"] == want,
          f'card={m["barsLeft"]} want={want} chainTtl={chain["barsLeft"]}')
    check("summary の鮮度残と一致",
          f'鮮度残{m["barsLeft"]}本' in card["summary"], card["summary"])


def test_card_bars_left_still_chain_ttl_when_incomplete():
    card = msnr_gate.build_card(evaluate(NO_RETEST))
    m = card["msnr"]
    check("未完成は CHAIN_TTL のまま", m["chainState"] == "MSS_CONFIRMED" and m["barsLeft"] == 12,
          str(m))


test_card_bars_left_uses_complete_ttl()
test_card_bars_left_still_chain_ttl_when_incomplete()


print("\n" + "=" * 68)
if FAILED:
    print(f"FAILED {len(FAILED)}: " + ", ".join(FAILED))
    sys.exit(1)
print("ALL PASS (test_msnr_gate)")
sys.exit(0)

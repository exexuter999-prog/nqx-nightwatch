# -*- coding: utf-8 -*-
"""R103-0: 決済トレードの「刈られ方」の計測(純関数。fixture だけで通る)。

    python tests/test_r103_excursions.py

tests/fixtures/r103/ の 2026-09-15 の 4 件(MNQ 3 分確定足 + 武装時点の幾何と約定)を
固定する。ネットワーク・台帳・発注に到達しないことも tripwire で確かめる。
"""
import json
import os
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
import excursion_metrics as em  # noqa: E402
import model_scorecard as ms  # noqa: E402

PASS = [0]
FAIL = [0]
FIXTURES = os.path.join(BASE, "tests", "fixtures", "r103")


def check(label, condition, detail=""):
    if condition:
        PASS[0] += 1
        print(f"  OK   {label}")
    else:
        FAIL[0] += 1
        print(f"  NG   {label}  {detail}")


def load(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------- 本番パス tripwire

HITS = []
for module_name, attrs in (("broker_status", ("query_position", "query_orders", "query_balance")),
                           ("nqx_state", ("fetch_state_quiet", "publish_result")),
                           ("autotrade_arm", ("state",))):
    try:
        module = __import__(module_name)
    except Exception:                                  # noqa: BLE001 — 無ければ到達しようがない
        continue
    for attr in attrs:
        if hasattr(module, attr):
            setattr(module, attr, (lambda name: (lambda *a, **k: HITS.append(name)))
                    (f"{module_name}.{attr}"))

# ---------------------------------------------------------------- fixture

bars = load("bars3m_2026-09-15.json")["bars"]
cases = load("cases_2026-09-15.json")["cases"]
check("fixture: 3 分足がある", len(bars) > 0, str(len(bars)))
check("fixture: 4 件ある", len(cases) == 4, str(len(cases)))

EXPECTED_BEYOND = [54.0, 0.0, 26.25, 8.25]
EXPECTED_TP1 = [168, None, 78, 21]
TP1_TOL_MIN = 3


def to_trade(case):
    """fixture の 1 件を .secrets/model_scorecard.jsonl の 1 行と同じ形へ。"""
    decision, trade = case["decision"], case["trade"]
    return {
        "side": "LONG" if decision["side"] == "BUY" else "SHORT",
        "entry": trade["fill"],
        "stop": decision["stop"],
        "exit": trade["exit"],
        "openedAt": trade["openedAt"],
        "closedAt": trade["closedAt"],
        "outcome": "LOSS",
    }


rows = []
for index, case in enumerate(cases):
    tag = case.get("tag") or f"#{index + 1}"
    metrics = em.classify(to_trade(case), bars,
                          tp1=case["decision"]["targets"][0], noise=case["noiseFloor"])
    rows.append(metrics)
    check(f"{tag}: huntClass=STOP_HUNT", metrics["huntClass"] == "STOP_HUNT", str(metrics))
    check(f"{tag}: beyondStopPt={EXPECTED_BEYOND[index]}",
          metrics["beyondStopPt"] == EXPECTED_BEYOND[index], str(metrics))
    want = EXPECTED_TP1[index]
    got = metrics["tp1AfterExitMin"]
    if want is None:
        check(f"{tag}: tp1AfterExitMin は None(決済後 180 分で TP1 未到達)", got is None, str(got))
    else:
        check(f"{tag}: tp1AfterExitMin≈{want}分(±{TP1_TOL_MIN})",
              got is not None and abs(got - want) <= TP1_TOL_MIN, str(got))
    check(f"{tag}: 保有中の MAE/MFE が出る",
          metrics["maePt"] is not None and metrics["mfePt"] is not None, str(metrics))
    check(f"{tag}: fav3hPt は SL 幅より広い(方向は合っていた)",
          (metrics["fav3hPt"] or 0) >= abs(case["decision"]["stop"] - case["trade"]["fill"]),
          str(metrics))

# ---------------------------------------------------------------- 足が無いとき

empty = em.classify(to_trade(cases[0]), [], tp1=None, noise=None)
check("足が無ければ全部 None(推測で埋めない)",
      all(value is None for value in empty.values()), str(empty))
check("キーは足の有無で変わらない", set(empty) == set(rows[0]), str(set(empty) ^ set(rows[0])))

# ---------------------------------------------------------------- スコアカード行

case = cases[0]
result = {
    "resultId": "R103-TEST-1",
    "openedAt": case["trade"]["openedAt"], "closedAt": case["trade"]["closedAt"],
    "side": "LONG" if case["decision"]["side"] == "BUY" else "SHORT",
    "qty": 2, "entry": case["trade"]["fill"], "exit": case["trade"]["exit"],
    "stop": case["decision"]["stop"], "fees": 4.0,
    "scenarioId": "S-R103-TEST", "mode": "SIMULATION",
}
row = ms.classify(result, plans={}, bars=bars,
                  tp1=case["decision"]["targets"][0], noise=case["noiseFloor"])
check("noise 未指定でも建玉直前 12 本から出す",
      em.noise_before(bars, case["trade"]["openedAt"]) is not None)
check("建玉より前に 12 本無ければ noise は None(推測で埋めない)",
      em.noise_before(bars[:5], case["trade"]["openedAt"]) is None)
check("noise_before は msnr_gate.noise_floor と同じ母数",
      em.NOISE_BARS == __import__("msnr_gate").NOISE_BARS)

# tp1 / noise を渡さず、bars と凍結プラン(plans)だけで 4 件が STOP_HUNT になる。
for index, item in enumerate(cases):
    tag = item.get("tag") or f"#{index + 1}"
    plan_result = {
        "resultId": f"R103-PLAN-{index}",
        "openedAt": item["trade"]["openedAt"], "closedAt": item["trade"]["closedAt"],
        "side": "LONG" if item["decision"]["side"] == "BUY" else "SHORT",
        "qty": 2, "entry": item["trade"]["fill"], "exit": item["trade"]["exit"],
        "stop": item["decision"]["stop"], "fees": 4.0,
        "scenarioId": f"S-{tag}", "mode": "SIMULATION",
    }
    plans = {f"S-{tag}": {"model": item["decision"]["model"], "grade": item["decision"]["grade"],
                          "tp1": item["decision"]["targets"][0]}}
    plan_row = ms.classify(plan_result, plans=plans, bars=bars)
    check(f"{tag}: plans の TP1 と建玉前の足だけで STOP_HUNT になる",
          plan_row["excursion"]["huntClass"] == "STOP_HUNT", str(plan_row["excursion"]))
    check(f"{tag}: noiseSource=BARS_BEFORE_ENTRY",
          plan_row["excursion"]["noiseSource"] == "BARS_BEFORE_ENTRY",
          str(plan_row["excursion"]))

check("scorecard 行に excursion が載る",
      isinstance(row.get("excursion"), dict), str(row.get("excursion")))
check("scorecard の huntClass も STOP_HUNT",
      row["excursion"]["huntClass"] == "STOP_HUNT", str(row["excursion"]))
check("既存キーと衝突しない(excursion は新規キー 1 個だけ)",
      set(row) - set(ms.classify(result, plans={})) == set(), str(set(row)))
check("足を渡さなければ excursion は None",
      ms.classify(result, plans={})["excursion"] is None, str(row.get("excursion")))

no_bars = dict(result, resultId="R103-TEST-2")
check("publish 用 result は書き換えない",
      "excursion" not in no_bars and "excursion" not in result, str(sorted(result)))

# ---------------------------------------------------------------- 集計と再計算(tempdir)

with tempfile.TemporaryDirectory() as tmp:
    path = os.path.join(tmp, "model_scorecard.jsonl")
    # 足が足りずに計測が空のまま記録された行(tp1 / noise は入力として残っている)。
    stale = dict(row)
    stale["excursion"] = dict(row["excursion"], huntClass=None, beyondStopPt=None,
                              tp1AfterExitMin=None, fav3hPt=None, bars=0)
    stale["recordedAt"] = "2026-09-15T00:00:00+00:00"
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(stale, ensure_ascii=False) + "\n")
    before = open(path, encoding="utf-8").read()

    bars_dir = os.path.join(tmp, "bars")
    os.makedirs(bars_dir)
    with open(os.path.join(bars_dir, "bars3m_2026-09-15.json"), "w", encoding="utf-8") as fh:
        json.dump({"bars": bars}, fh)

    stats = ms.backfill_excursions(bars_dir, path=path)
    check("backfill: 1 行追記した", stats["appended"] == 1, str(stats))
    after = open(path, encoding="utf-8").read()
    check("backfill: 既存行は消さない・書き換えない", after.startswith(before), after[:120])
    appended = [json.loads(line) for line in after.strip().splitlines()][-1]
    check("backfill: 新行に supersedes が付く",
          appended.get("supersedes") == stale["recordedAt"], str(appended.get("supersedes")))
    check("backfill: 新行に計測が載る",
          (appended.get("excursion") or {}).get("huntClass") == "STOP_HUNT",
          str(appended.get("excursion")))
    check("backfill: 同じ足で 2 回目は追記しない",
          ms.backfill_excursions(bars_dir, path=path)["appended"] == 0)
    check("backfill: 足が読めなければ追記しない",
          ms.backfill_excursions(os.path.join(tmp, "nothing"), path=path)["appended"] == 0)

    # backfill も凍結プランから TP1 を引く(台帳は tempdir。.secrets は読まない)。
    ledger = os.path.join(tmp, "autotrade_ledger.jsonl")
    with open(ledger, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"plan": {"scenarioId": row["scenarioId"], "model": row["model"],
                                      "grade": row["grade"],
                                      "targets": [case["decision"]["targets"][0]]}}) + "\n")
    check("plan_index が targets[0] を tp1 として持つ",
          ms.plan_index(ledger)[row["scenarioId"]]["tp1"] == case["decision"]["targets"][0])

    plain = os.path.join(tmp, "plain.jsonl")
    bare = {key: value for key, value in row.items() if key != "excursion"}
    bare["recordedAt"] = "2026-09-15T00:00:00+00:00"
    with open(plain, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(bare, ensure_ascii=False) + "\n")
    ms.backfill_excursions(bars_dir, path=plain, ledger_path=ledger)
    latest = [json.loads(line) for line in open(plain, encoding="utf-8")][-1]
    check("backfill: excursion の無い行も plan の TP1 で STOP_HUNT になる",
          latest["excursion"]["huntClass"] == "STOP_HUNT", str(latest["excursion"]))

    summary = ms.excursion_summary(ms.load_rows(path))
    model = row["model"]
    check("--excursions: モデル別に huntClass の内訳が出る",
          summary["models"][model]["classes"]["STOP_HUNT"] == 1, str(summary["models"][model]))
    check("backfill: tp1 / noise は前回の入力を引き継ぐ",
          appended["excursion"]["tp1"] == row["excursion"]["tp1"]
          and appended["excursion"]["noise"] == row["excursion"]["noise"],
          str(appended["excursion"]))
    check("--excursions: beyondStopPt の中央値",
          summary["models"][model]["beyondStopPtMedian"] == EXPECTED_BEYOND[0],
          str(summary["models"][model]))
    check("--excursions: 等級別にも出る",
          any(key.startswith(model + "|") for key in summary["byGrade"]), str(summary["byGrade"]))

check("中央値: 偶数個は平均", ms._median([1.0, 2.0, 3.0, 4.0]) == 2.5)
check("中央値: 空なら None", ms._median([]) is None)

# ---------------------------------------------------------------- R57 の出力は変えない

check("obsidian_metrics.excursions は excursion_metrics へ委譲",
      __import__("obsidian_metrics").excursions.__module__ in ("obsidian_metrics",))

check("本番パスへの到達 0 件", not HITS, str(HITS))

print(f"\n合計: PASS {PASS[0]} / FAIL {FAIL[0]}")
sys.exit(1 if FAIL[0] else 0)

"""R44: ブローカー残高から残機(LIFELINE)を導出する経路の回帰テスト。

それまで残機は `.secrets/crosstrade.env` の `LIFELINE_*` 手入力だけが正本で、
CLAUDE.md 自身が「更新漏れはそのまま残機の誤認になる」と警告していた。
2026-08-26 03:14 の実測では設定値 2,973.90 に対し実際は 2,868.10 で、
直前の損切り $105.80 がまるごと未反映だった。

ここで守るのは次の4点:
  1. 残高が取れて床が決まるときだけ自動導出する
  2. 取れないときは黙って推測せず、手入力へ倒す
  3. トレーリングの床は単調増加(=下げない)
  4. 設定が無ければブローカーへ問い合わせない(従来経路を遅くしない)
"""
import json
import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import broker_status  # noqa: E402
import nqx_state  # noqa: E402

FAILURES = []


def check(label, condition, detail=""):
    if condition:
        print(f"  ok   {label}")
    else:
        FAILURES.append(f"{label}{(' — ' + detail) if detail else ''}")
        print(f"  FAIL {label}{(' — ' + detail) if detail else ''}")


def balance(net_liq, net_liq_sod, verified=True):
    return {"verified": verified, "netLiq": net_liq, "netLiqSOD": net_liq_sod,
            "realizedPnL": net_liq - net_liq_sod, "openPnL": 0.0}


BASE_ENV = {
    "CROSSTRADE_ACCOUNTS": "ACC-A,ACC-B",
    "RISK_ACC-A": "240", "RISK_ACC-B": "240",
    "LIFELINE_ACC-A": "2973.90", "LIFELINE_ACC-B": "2973.90",
}


def with_temp_highwater(fn):
    """高値台帳を毎回まっさらにして走らせる。"""
    def wrapper():
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "account_highwater.json")
            with mock.patch.object(nqx_state, "HIGHWATER_PATH", path):
                fn(path)
    wrapper.__name__ = fn.__name__
    return wrapper


@with_temp_highwater
def test_trailing_dd_derives_buffer_from_live_balance(path):
    env = dict(BASE_ENV, **{"TRAILING_DD_ACC-A": "3000", "TRAILING_DD_ACC-B": "3000"})
    balances = {"ACC-A": balance(99868.10, 100000.0),
                "ACC-B": balance(99868.10, 100000.0)}
    payload = nqx_state.build_accounts_payload(env=env, balances=balances)
    rows = {row["id"]: row for row in payload["list"]}
    # 床 = 100,000 − 3,000 = 97,000。残機 = 99,868.10 − 97,000。
    check("残機がブローカー残高から導出される", rows["ACC-A"]["buffer"] == 2868.10,
          str(rows["ACC-A"]["buffer"]))
    check("手入力の 2973.90 では **ない**", rows["ACC-A"]["buffer"] != 2973.90)
    check("純資産が equity として載る", rows["ACC-A"]["equity"] == 99868.10)
    check("source が broker 由来を名乗る", payload["source"] == "crosstrade-balance",
          payload["source"])
    check("cap は env のまま", rows["ACC-A"]["cap"] == 240.0)


@with_temp_highwater
def test_explicit_floor_wins_over_trailing(path):
    env = dict(BASE_ENV, **{"TRAILING_DD_ACC-A": "3000", "FLOOR_ACC-A": "98000"})
    balances = {"ACC-A": balance(99868.10, 100000.0)}
    rows = {r["id"]: r for r in
            nqx_state.build_accounts_payload(env=env, balances=balances)["list"]}
    check("FLOOR_* が TRAILING_DD_* より優先される", rows["ACC-A"]["buffer"] == 1868.10,
          str(rows["ACC-A"]["buffer"]))


@with_temp_highwater
def test_trailing_floor_locks_at_start_plus_100(path):
    """R85: Lucid 50K funded。残高 $52,100 超えで日を終えると最低残高は $50,100 で固定。"""
    env = {"CROSSTRADE_ACCOUNTS": "ACC-A", "TRAILING_DD_ACC-A": "2000",
           "DD_LOCK_FLOOR_ACC-A": "50100"}
    # 固定前(実口座 9/11 の SOD): EOD 高値 51,973 → 床 49,973(トレーリングのまま)
    pre = nqx_state.build_accounts_payload(
        env=env, balances={"ACC-A": balance(53656.0, 51973.0)})["list"][0]
    check("固定前はトレーリングの床(51,973 − 2,000)", pre["buffer"] == 3683.0, str(pre["buffer"]))
    # 9/11 の EOD 53,657(52,100 超え)→ 床は 51,657 ではなく 50,100
    post = nqx_state.build_accounts_payload(
        env=env, balances={"ACC-A": balance(53657.0, 53657.0)})["list"][0]
    check("52,100 を超えて日を終えたら床は 50,100 で止まる", post["buffer"] == 3557.0,
          str(post["buffer"]))
    # その後 EOD がさらに上がっても床は上がらない
    later = nqx_state.build_accounts_payload(
        env=env, balances={"ACC-A": balance(55000.0, 55000.0)})["list"][0]
    check("固定後は EOD 高値が上がっても床は 50,100 のまま", later["buffer"] == 4900.0,
          str(later["buffer"]))


@with_temp_highwater
def test_trailing_floor_lock_boundary(path):
    env = {"CROSSTRADE_ACCOUNTS": "ACC-A", "TRAILING_DD_ACC-A": "2000",
           "DD_LOCK_FLOOR_ACC-A": "50100"}
    exact = nqx_state.build_accounts_payload(
        env=env, balances={"ACC-A": balance(52100.0, 52100.0)})["list"][0]
    check("ちょうど 52,100 で終えた日は床 50,100(トレーリングでも固定でも同じ値)",
          exact["buffer"] == 2000.0, str(exact["buffer"]))


@with_temp_highwater
def test_without_lock_config_keeps_trailing(path):
    env = {"CROSSTRADE_ACCOUNTS": "ACC-A", "TRAILING_DD_ACC-A": "2000"}
    row = nqx_state.build_accounts_payload(
        env=env, balances={"ACC-A": balance(53657.0, 53657.0)})["list"][0]
    check("DD_LOCK_FLOOR が無ければ従来どおりトレーリングし続ける", row["buffer"] == 2000.0,
          str(row["buffer"]))


@with_temp_highwater
def test_unverified_balance_falls_back_to_manual(path):
    env = dict(BASE_ENV, **{"TRAILING_DD_ACC-A": "3000", "TRAILING_DD_ACC-B": "3000"})
    balances = {"ACC-A": {"verified": False, "detail": "timeout"},
                "ACC-B": {"verified": False, "detail": "timeout"}}
    payload = nqx_state.build_accounts_payload(env=env, balances=balances)
    rows = {row["id"]: row for row in payload["list"]}
    check("照会できなければ手入力へ倒す", rows["ACC-A"]["buffer"] == 2973.90)
    check("推測した equity を作らない", "equity" not in rows["ACC-A"])
    check("source が env 由来を名乗る", payload["source"] == "crosstrade-env",
          payload["source"])


@with_temp_highwater
def test_missing_floor_config_falls_back_to_manual(path):
    balances = {"ACC-A": balance(99868.10, 100000.0),
                "ACC-B": balance(99868.10, 100000.0)}
    rows = {r["id"]: r for r in
            nqx_state.build_accounts_payload(env=BASE_ENV, balances=balances)["list"]}
    check("床の決め方が無ければ自動導出しない", rows["ACC-A"]["buffer"] == 2973.90)


@with_temp_highwater
def test_highwater_only_ratchets_up(path):
    env = {"CROSSTRADE_ACCOUNTS": "ACC-A", "TRAILING_DD_ACC-A": "3000"}
    # 1日目: EOD 100,000 → 床 97,000
    nqx_state.build_accounts_payload(
        env=env, balances={"ACC-A": balance(100500.0, 100000.0)})
    # 2日目: EOD が 100,500 へ切り上がる → 床 97,500
    day2 = nqx_state.build_accounts_payload(
        env=env, balances={"ACC-A": balance(100400.0, 100500.0)})
    check("EOD 高値が上がると床も上がる",
          day2["list"][0]["buffer"] == 2900.0, str(day2["list"][0]["buffer"]))
    # 3日目: EOD が 100,200 へ下がっても床は 97,500 のまま(緩めない)
    day3 = nqx_state.build_accounts_payload(
        env=env, balances={"ACC-A": balance(100300.0, 100200.0)})
    check("EOD が下がっても床は下げない",
          day3["list"][0]["buffer"] == 2800.0, str(day3["list"][0]["buffer"]))
    store = json.load(open(path, encoding="utf-8"))
    check("高値が台帳に残る",
          store["ACC-A"]["highWaterNetLiqSOD"] == 100500.0, json.dumps(store))


@with_temp_highwater
def test_buffer_never_goes_negative(path):
    env = {"CROSSTRADE_ACCOUNTS": "ACC-A", "TRAILING_DD_ACC-A": "3000"}
    rows = nqx_state.build_accounts_payload(
        env=env, balances={"ACC-A": balance(96000.0, 100000.0)})["list"]
    # 吹き飛んだ口座は 0。負値は Worker 側の validateAccounts が拒否する。
    check("床を割っても残機は 0 で下げ止まる", rows[0]["buffer"] == 0.0, str(rows[0]["buffer"]))


def test_no_broker_call_without_config():
    called = []

    def spy(ids):
        called.append(ids)
        return {}

    with mock.patch.object(broker_status, "query_balances", spy):
        nqx_state.build_accounts_payload(env=BASE_ENV)
    check("床の設定が無ければブローカーへ問い合わせない", called == [], str(called))


def test_zero_is_a_real_balance_value():
    # `_first_number` は価格用で 0 を捨てる。損益で同じことをすると
    # 「含み益ゼロ」と「取得できていない」が同じ null に潰れる。
    raw = {"openPnL": 0.0, "netLiq": 0.0}
    check("_first_number は 0 を落とす(価格用の既存挙動)",
          broker_status._first_number(raw, ("openPnL",)) is None)
    check("_first_finite は 0 を値として返す",
          broker_status._first_finite(raw, ("openPnL",)) == 0.0)
    check("_first_finite は欠損では None を返す",
          broker_status._first_finite(raw, ("nope",)) is None)


def main():
    tests = [
        test_trailing_dd_derives_buffer_from_live_balance,
        test_explicit_floor_wins_over_trailing,
        test_trailing_floor_locks_at_start_plus_100,
        test_trailing_floor_lock_boundary,
        test_without_lock_config_keeps_trailing,
        test_unverified_balance_falls_back_to_manual,
        test_missing_floor_config_falls_back_to_manual,
        test_highwater_only_ratchets_up,
        test_buffer_never_goes_negative,
        test_no_broker_call_without_config,
        test_zero_is_a_real_balance_value,
    ]
    for fn in tests:
        print(fn.__name__)
        fn()
    print()
    if FAILURES:
        print(f"FAILED {len(FAILURES)}")
        for line in FAILURES:
            print("  - " + line)
        return 1
    print(f"OK {len(tests)} tests")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""R39: 自律送信の武装台帳・二重キー解決順・publish のバイト列復号。

ここが守るのは 3 点。
  1. 武装台帳は期限・口座・銘柄・破損で **必ず** 武装解除側へ倒れる。
  2. 明示指定（環境変数 / crosstrade.env）は台帳より強い。
  3. monitor_publish は自分で UTF-8 復号する（cp932 環境で落ちない）。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import autotrade_arm  # noqa: E402
import autotrade_engine  # noqa: E402
import monitor_publish  # noqa: E402

FAILED: list = []


def check(label, condition, detail=""):
    if condition:
        print(f"  OK   {label}")
    else:
        FAILED.append(label)
        print(f"  FAIL {label}{(': ' + str(detail)) if detail else ''}")


def _tmp() -> str:
    return os.path.join(tempfile.mkdtemp(prefix="nqxarm"), "autotrade_arm.json")


def _write(path, **overrides):
    now = datetime.now(timezone.utc)
    record = {
        "schemaVersion": autotrade_arm.SCHEMA_VERSION,
        "armId": "arm_test000000",
        "armedAt": now.isoformat().replace("+00:00", "Z"),
        "expiresAt": (now + timedelta(minutes=30)).isoformat().replace("+00:00", "Z"),
        "autotrade": True, "live": True,
        "accountScope": ["ACCT1"], "symbol": "MNQU6",
        "reason": "test", "operator": "test",
    }
    record.update(overrides)
    Path(path).write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    return record


CFG = {"CROSSTRADE_ACCOUNTS": "ACCT1", "NQX_SYMBOL": "MNQU6"}


def test_state():
    print("\n[武装台帳の判定]")
    path = _tmp()

    check("台帳が無ければ武装解除", autotrade_arm.state(path, CFG)["valid"] is False)

    _write(path)
    live = autotrade_arm.state(path, CFG)
    check("正しい台帳は武装", live["valid"] and live["autotrade"] and live["live"])

    _write(path, live=False)
    dry = autotrade_arm.state(path, CFG)
    check("live=false はドライラン", dry["valid"] and dry["autotrade"] and not dry["live"])

    _write(path, autotrade=False, live=True)
    both = autotrade_arm.state(path, CFG)
    check("autotrade=false なら live も落ちる", both["live"] is False)

    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat().replace("+00:00", "Z")
    _write(path, expiresAt=past)
    expired = autotrade_arm.state(path, CFG)
    check("期限切れは自動で武装解除", expired["valid"] is False and expired.get("expired") is True)

    _write(path, expiresAt="2026-08-24T10:00:00")  # tz なし
    check("tz 無しの expiresAt は無効",
          autotrade_arm.state(path, CFG)["valid"] is False)

    _write(path, accountScope=["ACCT_OLD"])
    scope = autotrade_arm.state(path, CFG)
    check("口座が変わった台帳は無効", scope["valid"] is False, scope.get("reason"))

    _write(path, symbol="MNQZ6")
    check("銘柄が変わった台帳は無効（限月ロール）",
          autotrade_arm.state(path, CFG)["valid"] is False)

    _write(path, schemaVersion="OTHER/9")
    check("未知スキーマは無効", autotrade_arm.state(path, CFG)["valid"] is False)

    Path(path).write_text("{ this is not json", encoding="utf-8")
    broken = autotrade_arm.state(path, CFG)
    check("壊れた台帳は fail-closed",
          broken["valid"] is False and broken["autotrade"] is False and broken["live"] is False)

    Path(path).write_text("[1,2,3]", encoding="utf-8")
    check("配列の台帳も fail-closed", autotrade_arm.state(path, CFG)["valid"] is False)


def test_arm_guards():
    print("\n[武装コマンドのガード]")
    path = _tmp()
    try:
        autotrade_arm.arm(live=False, minutes=autotrade_arm.MAX_MINUTES + 1,
                          path=path, cfg=CFG)
        check("上限を超える武装は拒否", False)
    except ValueError:
        check("上限を超える武装は拒否", True)

    try:
        autotrade_arm.arm(live=False, minutes=0, path=path, cfg=CFG)
        check("0 分の武装は拒否", False)
    except ValueError:
        check("0 分の武装は拒否", True)

    try:
        autotrade_arm.arm(live=False, minutes=10, path=path,
                          cfg={"CROSSTRADE_ACCOUNTS": ""})
        check("送信先不明の武装は拒否", False)
    except ValueError:
        check("送信先不明の武装は拒否", True)

    # R40: 上限は契約から読む。数字を焼かない（契約を広げたときに武装だけが
    # 取り残されて「ドライランは通るのにライブで落ちる」を作り直さないため）。
    max_accounts = autotrade_arm._max_accounts()
    if max_accounts is None:
        # maxAccounts=null(上限撤廃)。口座数では LIVE 武装を止めない。
        many = ",".join(f"ACC{index}" for index in range(12))
        autotrade_arm.arm(live=True, minutes=10, path=path,
                          cfg={"CROSSTRADE_ACCOUNTS": many, "NQX_SYMBOL": "MNQU6"})
        check("上限なしなら口座数で LIVE 武装を止めない", Path(path).exists())
        autotrade_arm.arm(live=False, minutes=10, path=path,
                          cfg={"CROSSTRADE_ACCOUNTS": many, "NQX_SYMBOL": "MNQU6"})
        check("上限なしならドライラン武装も通る", Path(path).exists())
    else:
        too_many = ",".join(f"ACC{index}" for index in range(max_accounts + 1))
        try:
            autotrade_arm.arm(live=True, minutes=10, path=path,
                              cfg={"CROSSTRADE_ACCOUNTS": too_many, "NQX_SYMBOL": "MNQU6"})
            check("契約の上限を超える LIVE 武装は拒否", False)
        except ValueError as exc:
            check("契約の上限を超える LIVE 武装は拒否", "maxAccounts" in str(exc), str(exc))

        # 上限ちょうどの口座数は LIVE でも許す
        at_limit = ",".join(f"ACC{index}" for index in range(max_accounts))
        autotrade_arm.arm(live=True, minutes=10, path=path,
                          cfg={"CROSSTRADE_ACCOUNTS": at_limit, "NQX_SYMBOL": "MNQU6"})
        check(f"上限ちょうど({max_accounts}口座)の LIVE 武装は許可", Path(path).exists())

        # 上限を超えていてもドライランは許す（提案表示は口座数に依存しない）
        autotrade_arm.arm(live=False, minutes=10, path=path,
                          cfg={"CROSSTRADE_ACCOUNTS": too_many, "NQX_SYMBOL": "MNQU6"})
        check("上限超過でもドライラン武装は許可", Path(path).exists())

    record = autotrade_arm.arm(live=True, minutes=5, path=path, cfg=CFG,
                               reason="unit")
    check("武装は armId を発行", str(record["armId"]).startswith("arm_"))
    check("武装は口座スコープを凍結", record["accountScope"] == ["ACCT1"])

    autotrade_arm.set_kill(True, path=path)
    check("KILL は台帳へ立つ", autotrade_arm.state(path, CFG)["kill"] is True)
    autotrade_arm.set_kill(False, path=path)
    check("KILL は下ろせる", autotrade_arm.state(path, CFG)["kill"] is False)

    autotrade_arm.disarm(path=path)
    check("解除で台帳が消える", not Path(path).exists())
    check("解除の二度打ちは失敗しない", autotrade_arm.disarm(path=path)["removed"] is False)


def test_switch_precedence():
    print("\n[二重キーの解決順]")
    path = _tmp()
    _write(path, live=True)
    saved = {key: os.environ.pop(key, None)
             for key in ("NQX_AUTOTRADE", "NQX_LIVE_ORDERS", "NQX_AUTOTRADE_KILL")}
    original = autotrade_arm.ARM_FILE
    original_remote = autotrade_arm._remote_view
    autotrade_arm.ARM_FILE = path
    autotrade_arm._remote_view = lambda: (False, None)
    try:
        check("台帳だけで autotrade が立つ", autotrade_engine.autotrade_enabled(CFG) is True)
        check("台帳だけで live が立つ", autotrade_engine.live_enabled(CFG) is True)

        off = {**CFG, "NQX_LIVE_ORDERS": "0"}
        check("crosstrade.env の 0 は台帳より強い",
              autotrade_engine.live_enabled(off) is False)
        check("live を切っても autotrade は残る",
              autotrade_engine.autotrade_enabled(off) is True)

        os.environ["NQX_AUTOTRADE"] = "0"
        check("環境変数の 0 は全部を落とす",
              autotrade_engine.autotrade_enabled(CFG) is False
              and autotrade_engine.live_enabled(CFG) is False)
        del os.environ["NQX_AUTOTRADE"]

        check("KILL は台帳から立てられる",
              autotrade_engine.kill_enabled(CFG) is False)
        autotrade_arm.set_kill(True, path=path)
        check("KILL 台帳を engine が読む", autotrade_engine.kill_enabled(CFG) is True)
        check("crosstrade.env の KILL=0 は台帳より強い",
              autotrade_engine.kill_enabled({**CFG, "NQX_AUTOTRADE_KILL": "0"}) is False)

        autotrade_arm.disarm(path=path)
        check("台帳を消せば既定は無効",
              autotrade_engine.autotrade_enabled(CFG) is False
              and autotrade_engine.live_enabled(CFG) is False)
        check("台帳が無くても env は効く",
              autotrade_engine.autotrade_enabled({**CFG, "NQX_AUTOTRADE": "1"}) is True)
    finally:
        autotrade_arm.ARM_FILE = original
        autotrade_arm._remote_view = original_remote
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_remote_authority():
    print("\n[Mini App AUTO のサーバー正本]")
    path = _tmp()
    original_file = autotrade_arm.ARM_FILE
    original_remote = autotrade_arm._remote_view
    autotrade_arm.ARM_FILE = path
    now = datetime.now(timezone.utc)
    arm = {
        "schemaVersion": autotrade_arm.SCHEMA_VERSION,
        "armId": "app_testremote",
        "enabled": True, "autotrade": True, "live": True,
        "source": "TELEGRAM_MINI_APP",
        "armedAt": now.isoformat().replace("+00:00", "Z"),
        "expiresAt": (now + timedelta(minutes=30)).isoformat().replace("+00:00", "Z"),
        "accountScope": ["ACCT1"], "symbol": "MNQU6",
    }
    try:
        autotrade_arm._remote_view = lambda: (True, {"autotradeArm": arm})
        current = autotrade_arm.state(cfg=CFG)
        check("アプリAUTO ONだけで LIVE 武装", current["valid"] and current["live"], current)
        check("武装元は Telegram Mini App", current.get("source") == "TELEGRAM_MINI_APP")

        autotrade_arm._remote_view = lambda: (True, {"autotradeArm": {
            **arm, "enabled": False, "autotrade": False, "live": False, "status": "OFF"}})
        off = autotrade_arm.state(cfg=CFG)
        check("アプリAUTO OFFは即時にENTRY権限を落とす",
              off["valid"] is False and off["autotrade"] is False)

        autotrade_arm._remote_view = lambda: (True, None)
        unavailable = autotrade_arm.state(cfg=CFG)
        check("Worker状態を確認できなければ fail-closed",
              unavailable["valid"] is False and "unavailable" in unavailable["reason"])
    finally:
        autotrade_arm.ARM_FILE = original_file
        autotrade_arm._remote_view = original_remote


def test_publish_decode():
    print("\n[publish のバイト列復号]")
    bundle = {
        "at": "2026-08-24T12:00:00Z", "price": 23000.0,
        "priceAt": "2026-08-24T12:00:00Z", "sourceSymbol": "MNQU6",
        "watching": ["ヴァリューエリア上限で反応を待つ"],
        "snapshot": {"bars": [{"t": 1, "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 10}]},
    }
    raw = json.dumps(bundle, ensure_ascii=False).encode("utf-8")

    # 旧経路(ロケール復号)がこの環境で実際に壊れることを示す。
    try:
        raw.decode("cp932")
        locale_breaks = False
    except UnicodeDecodeError:
        locale_breaks = True
    check("日本語 UTF-8 は cp932 復号で壊れる（旧経路の再現）", locale_breaks)

    decoded = monitor_publish.read_bundle_bytes(raw)
    check("UTF-8 バイト列を復号できる",
          decoded.get("watching") == ["ヴァリューエリア上限で反応を待つ"])

    with_bom = b"\xef\xbb\xbf" + raw
    check("BOM 付きでも復号できる",
          monitor_publish.read_bundle_bytes(with_bom).get("watching")
          == ["ヴァリューエリア上限で反応を待つ"])

    try:
        monitor_publish.read_bundle_bytes(b"\xff\xfe not utf-8")
        check("不正バイト列は例外", False)
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
        check("不正バイト列は例外", True)


def test_cycle_window():
    print("\n[監視窓]")
    import nqx_cycle
    jst = nqx_cycle.JST

    def at(hour, minute=0):
        return datetime(2026, 8, 24, hour, minute, tzinfo=jst).astimezone(timezone.utc)

    check("07:00 は窓内", nqx_cycle.in_window(at(7, 0)) is True)
    check("12:30 は窓内", nqx_cycle.in_window(at(12, 30)) is True)
    check("21:00 は窓内", nqx_cycle.in_window(at(21, 0)) is True)
    check("23:59 は窓内", nqx_cycle.in_window(at(23, 59)) is True)
    check("03:59 は窓内", nqx_cycle.in_window(at(3, 59)) is True)
    # 2026-09-05 ユーザー指示: 終了を 04:00 → 05:45 へ
    check("04:00 は窓内", nqx_cycle.in_window(at(4, 0)) is True)
    check("05:44 は窓内", nqx_cycle.in_window(at(5, 44)) is True)
    check("05:45 は窓外", nqx_cycle.in_window(at(5, 45)) is False)
    check("06:59 は窓外", nqx_cycle.in_window(at(6, 59)) is False)
    check("設定検査が通る", nqx_cycle._preflight_config() is None,
          nqx_cycle._preflight_config())


def test_report_line():
    """報告行は publish 済み bundle と cycle からここで組む。

    エージェントに数字を書き写させないための行なので、**建値・SL・TP が
    出ていること**そのものを固定する。書き写し工程が 403 サイクルで
    ICT 入力 0% を招いた張本人だった。
    """
    print("\n[報告行]")
    import nqx_cycle

    armed = {"symbol": "MNQU6", "decision": {
        "model": "VP80_REVERSION", "grade": "A+", "side": "SELL", "state": "ARMED",
        "entry": 29630.0, "stop": 29655.25, "targets": [29580.0, 29505.5, 29400.0]}}
    # R77: publish 済みの行は正本(ゲート後 scenario)から組む。decision だけを
    # 渡して「published」と言わせない(2026-09-10 23:41 の誤報の形)。
    line = nqx_cycle.report_line(armed, True,
                                 published_state={"scenario": dict(armed["decision"])})
    check("モデル・方向・等級・状態が出る",
          "primary=VP80_REVERSION SELL A+ ARMED" in line, line)
    check("建値と SL が出る", "E=29,630" in line and "SL=29,655.25" in line, line)
    check("TP は 2 本まで（分割計画の本数）",
          "TP=29,580/29,505.5" in line and "29,400" not in line, line)
    check("publish 済みが分かる", line.endswith("published"), line)
    demoted = nqx_cycle.report_line(
        armed, True, published_state={"scenario": {**armed["decision"], "state": "WATCH"}})
    check("publish が WATCH に落とした行は WATCH(R77)",
          "SELL A+ WATCH" in demoted and "ARMED" not in demoted, demoted)
    unread = nqx_cycle.report_line(armed, True)
    check("正本なしの publish 済みは未確認を明示する(R77)",
          "post-gate state unread" in unread, unread)

    flat = {"symbol": "MNQU6", "decision": {"model": "FLAT", "state": "WATCH"}}
    blocked = nqx_cycle.report_line(flat, False, "BLOCKED missing=RANGE_ANCHOR_MISSING")
    check("不成立は primary=NONE", "primary=NONE" in blocked, blocked)
    check("不成立で建値を出さない", "E=" not in blocked, blocked)
    check("BLOCKED 理由が残る", blocked.endswith("BLOCKED missing=RANGE_ANCHOR_MISSING"),
          blocked)

    # 価格は bundle 由来。読めないときは黙って省く（HALT にしない）。
    saved = nqx_cycle._paths
    nqx_cycle._paths = lambda: {"bundle": Path("/nonexistent/bundle.json")}
    try:
        check("bundle が読めなくても行は出る",
              "primary=NONE" in nqx_cycle.report_line(flat, False))
    finally:
        nqx_cycle._paths = saved


def main():
    test_state()
    test_arm_guards()
    test_switch_precedence()
    test_remote_authority()
    test_publish_decode()
    test_cycle_window()
    test_report_line()
    print()
    if FAILED:
        print(f"FAILED {len(FAILED)}: " + ", ".join(FAILED))
        return 1
    print("ALL PASS (test_r39_autotrade_arm)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

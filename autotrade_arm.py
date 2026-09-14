#!/usr/bin/env python3
"""自律送信の武装スイッチ（R39）。

`autotrade_engine` の二重キー `NQX_AUTOTRADE` / `NQX_LIVE_ORDERS` は
プロセス環境変数でしか渡せなかった。ところが監視ループは 3 分ごとに
**新しいシェルから新しい Python を起動する**ので、環境変数はサイクルを
またいで残らない。結果として、二重キーは実運用の経路では一度も揃わず、
`reconcile()` は毎サイクル即座に `[]` を返していた。これが「自律送信が
動かない」の実体である。

R39では二重キーをセッション期限付き台帳へ移した。R41では通常運用の正本を
Telegram Mini AppのAUTOスイッチ→Worker/Durable Objectへ移し、監視プロセスが
毎サイクル取得する。期限切れ・取得不能・口座変更・銘柄変更はすべて自動失効。
OS環境変数の明示値は緊急上書きとして最優先、ローカル台帳はCloudflare未設定時の
保守用フォールバックとして残す。KILLだけは常にローカル独立経路である。

    python autotrade_arm.py --status
    python autotrade_arm.py --arm --minutes 420 --reason "JST夜間監視" --confirm
    python autotrade_arm.py --arm --live --minutes 420 --reason "..." --confirm
    python autotrade_arm.py --disarm
    python autotrade_arm.py --kill --confirm      # 緊急全決済フラグ
    python autotrade_arm.py --unkill

ローカル台帳は `.secrets/autotrade_arm.json`、監査は `.secrets/autotrade_arm_log.jsonl`。
どちらも `.secrets` 配下なので通知本文にも画面出力にも口座キーを出さない。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

BASE = os.path.dirname(os.path.abspath(__file__))
ARM_FILE = os.path.join(BASE, ".secrets", "autotrade_arm.json")
ARM_LOG = os.path.join(BASE, ".secrets", "autotrade_arm_log.jsonl")
ENV_FILE = os.path.join(BASE, ".secrets", "crosstrade.env")

SCHEMA_VERSION = "NQX_AUTOTRADE_ARM/1"
# R39 当初は監視窓(JST 21:00〜翌04:00)ちょうどの 420 分だった。2026-08-25 に窓が
# 12:30 始まり(930 分)へ、2026-09-01 に 07:00 始まり(1260 分)へ広がったが、既定値は
# 420 分のまま据え置く。MAX_MINUTES=720 が上限なので窓全体を1回で覆うことはできない。
# 窓の後半も自律送信を有効にしたい場合は人が張り直す(エージェントは武装しない)。
DEFAULT_MINUTES = 420          # 窓全体(930分)より短い。必要なら人が張り直す
MAX_MINUTES = 720              # これ以上の連続武装は認めない
TRUTHY = {"1", "true", "yes", "on", "enable", "enabled"}
REMOTE_CACHE_SECONDS = 3.0
_REMOTE_CACHE: Dict[str, Any] = {"at": 0.0, "view": None, "configured": False}


# ------------------------------------------------------------------ 基礎

def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in TRUTHY


def _now(now: Optional[datetime] = None) -> datetime:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse(value: Any) -> Optional[datetime]:
    raw = str(value or "").strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def read_env_file(path: str = ENV_FILE) -> Dict[str, str]:
    out: Dict[str, str] = {}
    try:
        with open(path, encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    out[key.strip()] = value.strip()
    except OSError:
        pass
    return out


def account_scope(cfg: Optional[Dict[str, str]] = None) -> List[str]:
    """武装時点の送信先口座。ここが変わった台帳は無効にする。"""
    cfg = cfg if cfg is not None else read_env_file()
    raw = str(cfg.get("CROSSTRADE_ACCOUNTS") or cfg.get("CROSSTRADE_ACCOUNT") or "")
    return sorted({item.strip() for item in re.split(r"[,\n]+", raw) if item.strip()})


def _max_accounts() -> int:
    """ライブ武装で許す口座数の上限。**正本は実行契約**。

    ここに数字を焼くと、契約を広げたときに武装だけが取り残されて
    「ドライランは通るのにライブで落ちる」を別の場所に作り直すことになる。
    契約が読めないときは 1 に倒す(狭い側が安全)。
    maxAccounts が null なら「上限なし」で None を返す —— 「読めなかった」と
    「無制限と決めた」を同じ 1 に潰さないための区別。
    """
    try:
        import execution_contract
        limit = execution_contract.CONTRACT["accountMode"]["maxAccounts"]
    except Exception:  # noqa: BLE001 — 読めないなら最も狭い前提へ
        return 1
    return None if limit is None else int(limit)


def _symbol(cfg: Optional[Dict[str, str]] = None) -> str:
    cfg = cfg if cfg is not None else read_env_file()
    return str(os.environ.get("NQX_SYMBOL") or cfg.get("NQX_SYMBOL") or "MNQU6")


# ------------------------------------------------------------------ 読み取り

def read_arm(path: Optional[str] = None) -> Dict[str, Any]:
    """台帳を読み、**壊れていれば必ず武装解除として返す**（fail-closed）。"""
    path = path or ARM_FILE
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except FileNotFoundError:
        return {"present": False, "valid": False, "autotrade": False, "live": False,
                "kill": False, "reason": "no arm file"}
    except OSError as exc:
        return {"present": True, "valid": False, "autotrade": False, "live": False,
                "kill": False, "reason": f"arm file unreadable: {exc}"}
    try:
        record = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return {"present": True, "valid": False, "autotrade": False, "live": False,
                "kill": False, "reason": f"arm file is invalid JSON: {exc}"}
    if not isinstance(record, dict):
        return {"present": True, "valid": False, "autotrade": False, "live": False,
                "kill": False, "reason": "arm file is not an object"}
    return record


def _remote_view() -> tuple[bool, Optional[Dict[str, Any]]]:
    """Worker の AUTO 正本を短時間キャッシュして読む。

    戻り値は ``(remote_configured, view)``。remote が設定済みなのに取得できない
    場合は ``(True, None)`` とし、ローカル台帳へ勝手にフォールバックしない。
    """
    now_mono = time.monotonic()
    if now_mono - float(_REMOTE_CACHE.get("at") or 0.0) < REMOTE_CACHE_SECONDS:
        return bool(_REMOTE_CACHE.get("configured")), _REMOTE_CACHE.get("view")
    configured = False
    view = None
    try:
        import nqx_state
        cloud = nqx_state.load_cloud_env(required=False)
        configured = bool(cloud.get("NQX_API_BASE") and cloud.get("NQX_LAUNCH_SECRET"))
        if configured:
            view = nqx_state.fetch_state_quiet(cloud)
    except Exception:  # noqa: BLE001 — remote の異常は必ず OFF 側へ
        view = None
    _REMOTE_CACHE.update({"at": now_mono, "view": view, "configured": configured})
    return configured, view


def _validated_record(record: Dict[str, Any], cfg: Dict[str, str], now: datetime,
                      *, label: str) -> Dict[str, Any]:
    """ローカル/Workerの同一スキーマを、口座・銘柄・期限込みで検証する。"""
    disarmed = {"present": bool(record.get("present", True)), "valid": False,
                "autotrade": False, "live": False, "kill": False,
                "expiresAt": record.get("expiresAt"), "armId": record.get("armId"),
                "source": record.get("source") or label}
    if record.get("valid") is False and "reason" in record and "schemaVersion" not in record:
        return {**disarmed, "reason": record["reason"]}
    if str(record.get("schemaVersion") or "") != SCHEMA_VERSION:
        return {**disarmed, "reason": f"{label} schemaVersion is unsupported"}
    if not bool(record.get("autotrade")):
        return {**disarmed, "reason": "AUTO is OFF in Mini App" if label == "remote arm" else "autotrade is disabled"}

    expires = _parse(record.get("expiresAt"))
    if expires is None:
        return {**disarmed, "reason": f"{label} expiresAt is missing or not timezone-aware"}
    if expires <= now:
        return {**disarmed, "reason": f"{label} expired at {_iso(expires)}", "expired": True}

    current_scope = account_scope(cfg)
    armed_scope = sorted(str(item) for item in (record.get("accountScope") or []))
    if armed_scope != current_scope:
        return {**disarmed, "reason": "CROSSTRADE account scope changed since arming; re-arm in app"}
    if str(record.get("symbol") or "") != _symbol(cfg):
        return {**disarmed, "reason": "symbol changed since arming; re-arm in app"}

    return {
        "present": True, "valid": True, "autotrade": True,
        "live": bool(record.get("live")), "kill": bool(record.get("kill")),
        "armId": record.get("armId"), "armedAt": record.get("armedAt"),
        "expiresAt": _iso(expires),
        "remainingSec": max(0, int((expires - now).total_seconds())),
        "accountScope": armed_scope, "symbol": record.get("symbol"),
        "reason": record.get("reason") or "", "operator": record.get("operator") or "",
        "source": record.get("source") or label,
    }


def state(path: Optional[str] = None, cfg: Optional[Dict[str, str]] = None,
          now: Optional[datetime] = None) -> Dict[str, Any]:
    """現在の武装状態を判定する。無効理由は必ず ``reason`` に残す。

    無効化条件はすべて「推測せずに落とす」側へ倒す。
      * 台帳が無い / 壊れている / スキーマ違い
      * ``expiresAt`` を過ぎている（期限切れは自動で解除される）
      * 武装時と ``CROSSTRADE_ACCOUNTS`` が違う（死んだ口座の再現防止）
      * 武装時と銘柄が違う（限月ロールで武装が残らない）
    """
    explicit_path = path is not None
    path = path or ARM_FILE
    now = _now(now)
    cfg = cfg if cfg is not None else read_env_file()
    local_record = read_arm(path)
    local = _validated_record(local_record, cfg, now, label="arm file")
    if explicit_path:
        return local

    configured, view = _remote_view()
    if not configured:
        return local
    if not isinstance(view, dict):
        return {"present": False, "valid": False, "autotrade": False, "live": False,
                "kill": bool(local.get("kill")), "reason": "remote AUTO state unavailable"}
    remote_record = view.get("autotradeArm")
    if not isinstance(remote_record, dict):
        return {"present": False, "valid": False, "autotrade": False, "live": False,
                "kill": bool(local.get("kill")), "reason": "AUTO is OFF in Mini App",
                "source": "TELEGRAM_MINI_APP"}
    remote = _validated_record(remote_record, cfg, now, label="remote arm")
    # KILL はローカルの独立した緊急経路。アプリのENTRY権限に吸収しない。
    return {**remote, "kill": bool(local.get("kill")), "source": "TELEGRAM_MINI_APP"}


# ------------------------------------------------------------------ 手動HALT(R87)

#: 設定ページの手動HALT を監視PCに写した最後の既知値。Worker が読めない周期はこれを使う。
MANUAL_HALT_FILE = os.path.join(BASE, ".secrets", "manual_halt_remote.json")
MANUAL_HALT_SCHEMA = "NQX_MANUAL_HALT/1"
#: 同じプロセス内では、最後に Worker から読めた値を正本にする(ファイル書き込みに失敗しても
#: その周期の判断は Worker の値で行う)。
_MANUAL_HALT_MEMO: Dict[str, Any] = {}


def read_manual_halt(path: Optional[str] = None) -> Dict[str, Any]:
    """設定ページの手動HALT の最後の既知値。

    無い / 壊れている → ``enabled: False``(「押された」と推測しない)。HALT を立てるのは
    Worker で ON を**観測した**ときだけで、読めない周期に勝手に立ても外しもしない。
    """
    if path is None and _MANUAL_HALT_MEMO:
        return dict(_MANUAL_HALT_MEMO)
    target = path or MANUAL_HALT_FILE
    try:
        with open(target, "rb") as fh:
            record = json.loads(fh.read().decode("utf-8-sig"))
    except FileNotFoundError:
        return {"enabled": False, "known": False, "reason": "no manual halt mirror"}
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return {"enabled": False, "known": False, "reason": f"manual halt mirror unreadable: {exc}"}
    if not isinstance(record, dict) or record.get("schemaVersion") != MANUAL_HALT_SCHEMA:
        return {"enabled": False, "known": False, "reason": "manual halt mirror schema is unsupported"}
    return {**record, "enabled": record.get("enabled") is True, "known": True}


def sync_manual_halt(view: Optional[Dict[str, Any]] = None, path: Optional[str] = None,
                     now: Optional[datetime] = None) -> Dict[str, Any]:
    """Worker の ``manualHalt`` を監視PCへ写す。**読めなければ直前の既知値を保つ**(sticky)。

    通信障害で HALT が勝手に外れない / 勝手に立たない。Worker が ``manualHalt`` を
    持たない(未デプロイ・一度も押していない)ときは OFF を写す —— それは観測された OFF。
    ``view`` を渡さなければ ``_remote_view()``(AUTO と同じ 3 秒キャッシュ)を使うので、
    同じ周期の AUTO 判定と合わせても Worker への読み取りは 1 回で済む。
    """
    if view is None:
        configured, view = _remote_view()
        if not configured:
            return {**read_manual_halt(path), "synced": False, "reason": "cloud state is not configured"}
    if not isinstance(view, dict):
        return {**read_manual_halt(path), "synced": False,
                "reason": "remote state unavailable (last known manual halt kept)"}
    raw = view.get("manualHalt") if isinstance(view.get("manualHalt"), dict) else {}
    record = {
        "schemaVersion": MANUAL_HALT_SCHEMA,
        "enabled": raw.get("enabled") is True,
        "updatedAt": raw.get("updatedAt"),
        "engagedAt": raw.get("engagedAt"),
        "reason": raw.get("reason"),
        "source": "TELEGRAM_MINI_APP",
        "syncedAt": _iso(_now(now)),
    }
    previous = read_manual_halt(path)
    if path is None:
        _MANUAL_HALT_MEMO.clear()
        _MANUAL_HALT_MEMO.update({**record, "known": True})
    changed = (previous.get("enabled") is True) != record["enabled"] \
        or previous.get("updatedAt") != record["updatedAt"] or not previous.get("known")
    if changed:
        try:
            _write_atomic(record, path or MANUAL_HALT_FILE)
            if previous.get("known") and (previous.get("enabled") is True) != record["enabled"]:
                _audit({"event": "manual_halt_sync", "enabled": record["enabled"],
                        "updatedAt": record["updatedAt"], "source": "TELEGRAM_MINI_APP"})
        except OSError:
            pass    # この周期は _MANUAL_HALT_MEMO が正本。次の周期で書き直す
    return {**record, "known": True, "synced": True}


def summary(path: Optional[str] = None, cfg: Optional[Dict[str, str]] = None,
            now: Optional[datetime] = None) -> str:
    """1 行の運用表示。口座 ID は伏せる。"""
    current = state(path, cfg, now)
    if not current.get("valid"):
        return f"DISARMED ({current.get('reason')})"
    mode = "LIVE" if current["live"] else "DRY-RUN"
    minutes = current.get("remainingSec", 0) // 60
    return f"ARMED/{mode}残り{minutes}分 accounts={len(current.get('accountScope') or [])}"


# ------------------------------------------------------------------ 書き込み

def _audit(entry: Dict[str, Any], path: Optional[str] = None) -> None:
    path = path or ARM_LOG
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    payload = dict(entry)
    payload.setdefault("at", _iso(_now()))
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def _write_atomic(record: Dict[str, Any], path: Optional[str] = None) -> None:
    path = path or ARM_FILE
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(record, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def arm(*, live: bool, minutes: int = DEFAULT_MINUTES, reason: str = "",
        operator: str = "", path: Optional[str] = None, cfg: Optional[Dict[str, str]] = None,
        now: Optional[datetime] = None) -> Dict[str, Any]:
    """自律経路を武装する。**呼び出し側が --confirm を検証済みであること。**"""
    now = _now(now)
    if minutes < 1:
        raise ValueError("minutes must be >= 1")
    if minutes > MAX_MINUTES:
        raise ValueError(f"minutes must be <= {MAX_MINUTES}")
    cfg = cfg if cfg is not None else read_env_file()
    scope = account_scope(cfg)
    if not scope:
        raise ValueError("CROSSTRADE_ACCOUNTS is empty; refusing to arm an unknown destination")
    max_accounts = _max_accounts()
    if live and max_accounts is not None and len(scope) > max_accounts:
        # ドライランは通るのにライブで ENTRY_INTENT_INVALID になる組み合わせを、
        # 武装時点で止める。上限は契約(execution_contract.accountMode.maxAccounts)から
        # 読む —— ここに数字を焼くと、契約を広げたときに武装だけが取り残される。
        raise ValueError(
            f"live arming allows at most {max_accounts} CROSSTRADE account(s) "
            f"(found {len(scope)}); execution contract accountMode.maxAccounts "
            f"is {max_accounts}")
    previous = state(path, cfg, now)
    record = {
        "schemaVersion": SCHEMA_VERSION,
        "armId": "arm_" + secrets.token_hex(6),
        "armedAt": _iso(now),
        "expiresAt": _iso(now + timedelta(minutes=int(minutes))),
        "autotrade": True,
        "live": bool(live),
        "kill": bool(previous.get("kill")) if previous.get("valid") else False,
        "accountScope": scope,
        "symbol": _symbol(cfg),
        "reason": str(reason or "")[:200],
        "operator": str(operator or os.environ.get("USERNAME") or "operator")[:64],
    }
    _write_atomic(record, path)
    _audit({"event": "ARM", "armId": record["armId"], "live": record["live"],
            "minutes": int(minutes), "expiresAt": record["expiresAt"],
            "accounts": len(scope), "reason": record["reason"],
            "operator": record["operator"]})
    return record


def disarm(*, path: Optional[str] = None, reason: str = "manual disarm") -> Dict[str, Any]:
    path = path or ARM_FILE
    previous = read_arm(path)
    try:
        os.remove(path)
        removed = True
    except FileNotFoundError:
        removed = False
    except OSError as exc:
        raise RuntimeError(f"arm file could not be removed: {exc}") from exc
    _audit({"event": "DISARM", "armId": previous.get("armId"),
            "removed": removed, "reason": str(reason or "")[:200]})
    return {"removed": removed, "armId": previous.get("armId")}


def set_kill(enabled: bool, *, path: Optional[str] = None, reason: str = "") -> Dict[str, Any]:
    """緊急全決済フラグ。武装していなくても立てられる（KILL は独立経路）。"""
    record = read_arm(path)
    now = _now()
    if str(record.get("schemaVersion") or "") != SCHEMA_VERSION:
        cfg = read_env_file()
        record = {
            "schemaVersion": SCHEMA_VERSION,
            "armId": "arm_" + secrets.token_hex(6),
            "armedAt": _iso(now),
            # KILL 単独の台帳は新規武装ではない。autotrade は立てるが live は
            # 立てない —— reconcile の KILL 分岐は live=False ならドライランで
            # 止まるので、ここで勝手に実送信を許可しない。
            "expiresAt": _iso(now + timedelta(minutes=DEFAULT_MINUTES)),
            "autotrade": True, "live": False,
            "accountScope": account_scope(cfg), "symbol": _symbol(cfg),
            "reason": "kill switch only", "operator": str(os.environ.get("USERNAME") or "operator")[:64],
        }
    record["kill"] = bool(enabled)
    _write_atomic(record, path)
    _audit({"event": "KILL_ON" if enabled else "KILL_OFF",
            "armId": record.get("armId"), "reason": str(reason or "")[:200]})
    return record


# ------------------------------------------------------------------ CLI

def _print_status(path: Optional[str] = None) -> int:
    requested_path = path
    display_path = path or ARM_FILE
    cfg = read_env_file()
    current = state(requested_path, cfg)
    scope = account_scope(cfg)
    print(f"arm file : {display_path}")
    print(f"status   : {summary(requested_path, cfg)}")
    print(f"autotrade: {current.get('autotrade')}")
    print(f"live     : {current.get('live')}")
    print(f"kill     : {current.get('kill')}")
    print(f"symbol   : {_symbol(cfg)}")
    print(f"accounts : {len(scope)} configured")
    if current.get("valid"):
        print(f"armId    : {current.get('armId')}")
        print(f"expiresAt: {current.get('expiresAt')} (残り {current.get('remainingSec', 0)//60} 分)")
        if current.get("reason"):
            print(f"reason   : {current['reason']}")
    else:
        print(f"reason   : {current.get('reason')}")
    # 明示指定は台帳より強い。台帳が効かない状態を黙って作らせない。
    shadowed = []
    for key in ("NQX_AUTOTRADE", "NQX_LIVE_ORDERS", "NQX_AUTOTRADE_KILL"):
        if key in os.environ:
            shadowed.append(f"env {key}={os.environ[key]}")
        elif key in cfg:
            shadowed.append(f"crosstrade.env {key}={cfg[key]}")
    for line in shadowed:
        print(f"override : {line} — この指定が台帳より優先されます")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="自律送信の武装スイッチ")
    parser.add_argument("--status", action="store_true", help="現在の武装状態を表示")
    parser.add_argument("--arm", action="store_true", help="自律経路を武装する")
    parser.add_argument("--live", action="store_true",
                        help="--arm と併用。実送信まで許可する（無しはドライラン）")
    parser.add_argument("--minutes", type=int, default=DEFAULT_MINUTES,
                        help=f"武装の有効時間（既定 {DEFAULT_MINUTES} 分 / 上限 {MAX_MINUTES} 分）")
    parser.add_argument("--reason", default="", help="武装理由（監査ログに残る）")
    parser.add_argument("--disarm", action="store_true", help="武装を解除する")
    parser.add_argument("--kill", action="store_true", help="緊急全決済フラグを立てる")
    parser.add_argument("--unkill", action="store_true", help="緊急全決済フラグを下ろす")
    parser.add_argument("--confirm", action="store_true",
                        help="--arm / --kill に必須。無しは何も書き換えない")
    args = parser.parse_args(argv)

    if args.disarm:
        result = disarm()
        print(f"disarmed ({'removed' if result['removed'] else 'already absent'})")
        return _print_status()
    if args.unkill:
        set_kill(False, reason="cli --unkill")
        print("kill switch cleared")
        return _print_status()
    if args.kill:
        if not args.confirm:
            print("[ドライラン] --kill には --confirm が必要です")
            return 1
        set_kill(True, reason=args.reason or "cli --kill")
        print("KILL SWITCH ON — 次サイクルで order.py --flatten が送られます")
        return _print_status()
    if args.arm:
        if not args.confirm:
            print("[ドライラン] --arm には --confirm が必要です")
            return 1
        try:
            record = arm(live=bool(args.live), minutes=int(args.minutes),
                         reason=args.reason)
        except ValueError as exc:
            print(f"ERROR: {exc}")
            return 2
        mode = "LIVE（実送信）" if record["live"] else "DRY-RUN（提案のみ）"
        print(f"armed {mode} / expiresAt={record['expiresAt']} / armId={record['armId']}")
        return _print_status()
    return _print_status()


if __name__ == "__main__":
    sys.exit(main())

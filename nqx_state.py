#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PC → Cloudflare Worker の状態 publisher。

Mini App が読む「唯一の正本」は Durable Object の中にある。このモジュールは
そこへ状態を書き込む側(producer)で、次を担保する。

  - HMAC 署名(timestamp / nonce / body hash / account)
  - stream ごとの **単調増加 revision**(scenario と position は完全に独立)
  - 失敗を握り潰さない。publish 失敗は False を返し、呼び出し側が判断する

## やらないこと

  - 発注。このモジュールは order.py も CrossTrade も呼ばない
  - 秘密の露出。secret は署名にしか使わず、payload にも戻り値にも入れない

設定: `.secrets/nqx_cloud.env`
    NQX_API_BASE=https://nqx-nightwatch-api.<subdomain>.workers.dev
    NQX_PUBLISH_SECRET=<wrangler secret put NQX_PUBLISH_SECRET と同じ値>
    NQX_LAUNCH_SECRET=<wrangler secret put NQX_LAUNCH_SECRET と同じ値>
    NQX_ACCOUNT_ID=lucid-50k-daily
    NQX_SYMBOL=MNQU6
    NQX_WEB_APP_URL=https://nqx-nightwatch.pages.dev/
"""
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import execution_contract
import contract as contract_month  # R102: 取引限月の正本
import execution_intent
import management_intent
import strategy_evidence

BASE = os.path.dirname(os.path.abspath(__file__))
CLOUD_ENV = os.path.join(BASE, ".secrets", "nqx_cloud.env")
REVISION_FILE = os.path.join(BASE, ".secrets", "nqx_revisions.json")
LOCK_FILE = REVISION_FILE + ".lock"
AUDIT_LOG = os.path.join(BASE, ".secrets", "nqx_audit.jsonl")

HTTP_TIMEOUT = 12
PUBLISH_RETRIES = 3
# Worker 側 state_machine.js の STREAMS と一致させること。
# "result" が漏れていた間、publish_result は next_revision で必ず落ちていた
# (2026-08-15 に発見・修正。テストがスタブしていて気づけなかった)。
STREAMS = ("scenario", "position", "order", "market", "result", "account", "cycle",
           "entry_claim", "management_claim", "broker_observation", "cycle_health")

# Worker 側 state_machine.js の BLOCKING_ORDER_STATES と一致させること。
# この状態の order が残ったままだと、決済して position が消えた瞬間に
# ORDER_PENDING が再浮上し、display.orderable が恒久的に false になる
# (HANDOFF §11 ②)。
BLOCKING_ORDER_STATES = ("PENDING", "SENT", "UNKNOWN", "PARTIAL", "ENTRY_PARTIAL_ROUTE", "ENTRY_PARTIAL_FILL", "ENTRY_RESTING")
TERMINAL_ORDER_STATES = ("FILLED", "CANCELED", "REJECTED")


class NotConfigured(Exception):
    """Cloudflare 側の設定が無い。既存の Telegram 経路は動き続ける。"""


# ---------------------------------------------------------------- 設定

def load_cloud_env(required=True):
    cfg = {}
    if os.path.exists(CLOUD_ENV):
        with open(CLOUD_ENV, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    cfg[key.strip()] = value.strip()
    if required:
        for key in ("NQX_API_BASE", "NQX_PUBLISH_SECRET", "NQX_ACCOUNT_ID"):
            if not cfg.get(key):
                raise NotConfigured(f"{CLOUD_ENV} に {key} がありません")
    cfg.setdefault("NQX_SYMBOL", contract_month.symbol())
    cfg.setdefault("NQX_WEB_APP_URL", "https://nqx-nightwatch.pages.dev/")
    return cfg


def is_configured():
    try:
        load_cloud_env(required=True)
        return True
    except NotConfigured:
        return False


# ---------------------------------------------------------------- revision

class _FileLock:
    """monitor と bot の 2 プロセスが同じ revision を取らないようにする。"""

    def __init__(self, path, timeout=5.0):
        self.path = path
        self.timeout = timeout
        self.fd = None

    def __enter__(self):
        deadline = time.time() + self.timeout
        while True:
            try:
                self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                return self
            except FileExistsError:
                if time.time() > deadline:
                    # 取れなくても進む。revision は時刻由来なので衝突しても 409 で弾かれる。
                    try:
                        os.unlink(self.path)
                    except OSError:
                        pass
                    return self
                time.sleep(0.02)

    def __exit__(self, *exc):
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
        try:
            os.unlink(self.path)
        except OSError:
            pass
        return False


def _load_revisions():
    try:
        with open(REVISION_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def next_revision(stream, bump=1):
    """stream ごとの単調増加 revision を採番する。

    ミリ秒エポックを下限にすることで、記録ファイルが消えても巻き戻らない。
    """
    if stream not in STREAMS:
        raise ValueError(f"unknown stream {stream}")
    os.makedirs(os.path.dirname(REVISION_FILE), exist_ok=True)
    with _FileLock(LOCK_FILE):
        revisions = _load_revisions()
        previous = int(revisions.get(stream, 0))
        revision = max(int(time.time() * 1000), previous + bump)
        revisions[stream] = revision
        with open(REVISION_FILE, "w", encoding="utf-8") as fh:
            json.dump(revisions, fh, ensure_ascii=False, indent=2)
    return revision


# ---------------------------------------------------------------- 署名

def _sign(secret, message):
    return hmac.new(secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()


def signing_string(timestamp, nonce, account_id, body_hash):
    return f"v1:{timestamp}:{nonce}:{account_id}:{body_hash}"


def issue_launch_token(user_id, ttl_seconds=6 * 3600, cfg=None):
    """Mini App の URL に載せる短命トークン。

    Telegram の keyboard button 起動では initData が供給されないため、
    Worker 側の読み取り認証はこのトークンで行う。secret は URL に出ない。
    """
    cfg = cfg or load_cloud_env()
    secret = cfg.get("NQX_LAUNCH_SECRET")
    if not secret:
        raise NotConfigured("NQX_LAUNCH_SECRET がありません")
    exp = int(time.time()) + int(ttl_seconds)
    signature = _sign(secret, f"v1:{user_id}:{exp}")
    return f"v1.{user_id}.{exp}.{signature}"


DIST_INDEX = os.path.join(BASE, "telegram_mini_app", "dist", "index.html")


def app_version():
    """Mini App のビルド識別子。

    Telegram の WebView はページを強くキャッシュする。URL が変わらないと
    再デプロイしても古い画面が出続けるので、ビルドが変わったら必ず変わる
    トークンを URL に載せる。

    優先順位:
      1. `.secrets/nqx_cloud.env` の NQX_APP_VERSION(デプロイ時に明示する場合)
      2. ローカル dist/index.html の内容ハッシュ
         (Vite が資産名にハッシュを付けるので、中身が変われば必ず変わる)
      3. どちらも無ければ None
    """
    cfg = load_cloud_env(required=False)
    if cfg.get("NQX_APP_VERSION"):
        return cfg["NQX_APP_VERSION"]
    try:
        with open(DIST_INDEX, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()[:12]
    except OSError:
        return None


def web_app_url(user_id, cfg=None, ttl_seconds=12 * 3600):
    """Mini App の URL を組み立てる。

    - `v`: ビルド識別子。WebView のキャッシュを確実に外す
    - `t` / `api`: Worker の読み取り認証。Cloudflare 未設定なら付かない
    """
    cfg = cfg or load_cloud_env(required=False)
    base = cfg.get("NQX_WEB_APP_URL", "https://nqx-nightwatch.pages.dev/")

    params = {}
    version = app_version()
    if version:
        params["v"] = version

    api = cfg.get("NQX_API_BASE", "")
    if cfg.get("NQX_LAUNCH_SECRET") and api:
        params["t"] = issue_launch_token(user_id, ttl_seconds, cfg)
        params["api"] = api

    if not params:
        return base
    separator = "&" if "?" in base else "?"
    return f"{base}{separator}{urllib.parse.urlencode(params)}"


# ---------------------------------------------------------------- publish

def _audit(entry):
    try:
        os.makedirs(os.path.dirname(AUDIT_LOG), exist_ok=True)
        with open(AUDIT_LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass  # 監査ログが書けないことで発注経路を止めない


def chat_fetch(after=0, cfg=None):
    """受信箱は無期限停止。旧呼び出しを止めず、通信せず空を返す。"""
    del after, cfg
    return True, []


def chat_send(text, reply_to=None, cfg=None):
    """受信箱は無期限停止。送信せず停止理由を返す。"""
    del text, reply_to, cfg
    return False, {"reason": "INBOX_DISABLED_INDEFINITELY", "enabled": False}


def publish(stream, payload, cfg=None, revision=None):
    """1 イベントを Worker へ送る。

    戻り値 ``(ok: bool, detail: dict)``。ok=False を成功として扱わないこと。
    """
    cfg = cfg or load_cloud_env()
    account_id = cfg["NQX_ACCOUNT_ID"]
    secret = cfg["NQX_PUBLISH_SECRET"]
    url = cfg["NQX_API_BASE"].rstrip("/") + "/api/publish"

    last_detail = {}
    for attempt in range(PUBLISH_RETRIES):
        rev = revision if revision is not None and attempt == 0 else next_revision(stream)
        nonce = secrets.token_hex(16)
        body = json.dumps(
            {"stream": stream, "revision": rev, "nonce": nonce,
             "issuedAt": datetime.now(timezone.utc).isoformat(), "payload": payload},
            ensure_ascii=False, separators=(",", ":"),
        )
        timestamp = str(int(time.time()))
        body_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "NQX-Nightwatch-State/1.0",
            "X-NQX-Timestamp": timestamp,
            "X-NQX-Nonce": nonce,
            "X-NQX-Account": account_id,
            "X-NQX-Signature": _sign(secret, signing_string(timestamp, nonce, account_id, body_hash)),
        }
        request = urllib.request.Request(url, data=body.encode("utf-8"), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
                detail = json.loads(response.read().decode("utf-8", "replace"))
            # R27: HTTP 200 = 成功、ではない。
            #
            # tombstoneCycle() は scenario を null にしながら accepted:true を
            # 返すので、サーバがシナリオを破棄しても 200 が返る。ここで本文を
            # 読まずに True を返していたため、Telegram は「ARMED — 発注ボタン
            # 表示中」と表示しつつ Mini App には何も出ない、という状態になっていた。
            # 破棄の引き金は 9 種(market/scenario 検証失敗・evidence のティック
            # 不整合・evidenceHash 不一致・ingest 時点で失効 など)。
            reason = str(detail.get("reason") or "")
            if reason.startswith("CYCLE_TOMBSTONED"):
                last_detail = dict(detail)
                last_detail["tombstoned"] = True
                _audit({"at": datetime.now(timezone.utc).isoformat(), "kind": "publish",
                        "stream": stream, "revision": rev, "ok": False, "reason": reason})
                return False, last_detail
            _audit({"at": datetime.now(timezone.utc).isoformat(), "kind": "publish",
                    "stream": stream, "revision": rev, "ok": True})
            return True, detail
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            try:
                last_detail = json.loads(raw)
            except json.JSONDecodeError:
                last_detail = {"reason": raw[:200]}
            last_detail["status"] = exc.code
            # 409 = revision か nonce の衝突。採番し直して再試行する。
            if exc.code == 409 and attempt + 1 < PUBLISH_RETRIES:
                continue
            break
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
            last_detail = {"reason": f"{type(exc).__name__}: {exc}"}
            if attempt + 1 < PUBLISH_RETRIES:
                time.sleep(0.4)
                continue
            break

    _audit({"at": datetime.now(timezone.utc).isoformat(), "kind": "publish",
            "stream": stream, "ok": False, "detail": last_detail})
    return False, last_detail


# ---------------------------------------------------------------- 便利関数

def scenario_fingerprint(scenario):
    """同一シナリオを識別する安定キー。価格と方向と時間軸だけで決める。"""
    material = json.dumps({
        "symbol": scenario.get("symbol"),
        "side": str(scenario.get("side", "")).upper(),
        "entry": scenario.get("entry"),
        "stop": scenario.get("stop"),
        "target": scenario.get("target"),
        "targets": scenario.get("targets"),
        "targetR": scenario.get("targetR"),
        "legs": scenario.get("legs"),
        "planVersion": scenario.get("planVersion"),
        "setupVersion": scenario.get("setupVersion"),
        "catalogVersion": scenario.get("catalogVersion"),
        "detectorVersion": scenario.get("detectorVersion"),
        "executionContractVersion": scenario.get("executionContractVersion"),
        "evidenceHash": scenario.get("evidenceHash"),
        "marketCycleId": scenario.get("marketCycleId"),
        "title": scenario.get("title"),
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "fp_" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]


def build_scenario(raw, observed_at, ttl_minutes=15, symbol=None, state=None, ultra=False):
    """monitor の scenario dict を DO のスキーマへ整える。

    必須項目を欠いたまま送らない。欠けていれば ValueError で止める。

    ``ultra=True`` は monitor_publish の ULTRA サイジング専用。枚数を ULTRA
    エンベロープで検証し、脚を比率分割(端数は runner)にする。既定の
    ``ultra=False`` では従来どおり固定枚数だけを通す(既定で緩む経路を作らない)。
    """
    symbol = symbol or load_cloud_env(required=False).get("NQX_SYMBOL", contract_month.symbol())
    observed = datetime.fromisoformat(str(observed_at))
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=timezone.utc)
    expires = observed + timedelta(minutes=ttl_minutes)

    for field in ("side", "entry", "stop", "target"):
        if raw.get(field) is None:
            raise ValueError(f"scenario.{field} がありません")

    side = str(raw["side"]).upper()
    if side not in {"BUY", "SELL"}:
        raise ValueError("scenario.side must be BUY or SELL")
    try:
        qty = int(raw.get("qty"))
    except (TypeError, ValueError) as exc:
        raise ValueError("scenario.qty must be an integer") from exc
    ultra_leg_split = None
    if ultra:
        ultra_env = execution_contract.CONTRACT.get("ultra") or {}
        if not ultra_env.get("enabled"):
            raise ValueError("ULTRA_DISABLED")
        ultra_leg_split = execution_contract.ultra_split(qty)
        if (qty < int(ultra_env["minQtyPerAccount"]) or qty > int(ultra_env["maxQtyPerAccount"])
                or ultra_leg_split is None):
            raise ValueError("ULTRA_QTY_OUT_OF_ENVELOPE")
    elif qty != int(execution_contract.CONTRACT["risk"]["fixedQty"]):
        raise ValueError("FIXED_QTY_REQUIRED")

    def tick(value, field):
        if value is None or isinstance(value, bool):
            raise ValueError(f"scenario.{field} is required")
        try:
            parsed = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"scenario.{field} must be numeric") from exc
        if not math.isfinite(parsed) or abs(parsed * 4 - round(parsed * 4)) > 1e-8:
            raise ValueError(f"scenario.{field} must be a finite 0.25-tick value")
        return parsed

    entry = tick(raw["entry"], "entry")
    stop = tick(raw["stop"], "stop")
    target = tick(raw["target"], "target")
    raw_targets = raw.get("targets", [target])
    if not isinstance(raw_targets, list) or len(raw_targets) != int(execution_contract.CONTRACT["splitPlan"]["targetCount"]):
        raise ValueError("SPLIT_PLAN_REQUIRED")
    targets = [tick(value, "targets") for value in raw_targets]
    if targets[0] != target:
        raise ValueError("scenario.target must equal targets[0]")
    ordered = all(
        value > entry and (index == 0 or value > targets[index - 1])
        for index, value in enumerate(targets)
    ) if side == "BUY" else all(
        value < entry and (index == 0 or value < targets[index - 1])
        for index, value in enumerate(targets)
    )
    if not ordered:
        raise ValueError("scenario.targets must be directional and ordered")
    if len(targets) < 2 or targets[0] == targets[-1]:
        raise ValueError("scenario qty=2 requires distinct TP1 and runner targets")
    target_r = raw.get("targetR") or []
    if not isinstance(target_r, list):
        raise ValueError("scenario.targetR must be an array")
    clean_target_r = []
    for value in target_r[:3]:
        try:
            parsed = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("scenario.targetR must contain finite values") from exc
        if not math.isfinite(parsed):
            raise ValueError("scenario.targetR must contain finite values")
        clean_target_r.append(parsed)
    legs = ([{"id": "TP1", "qty": ultra_leg_split[0], "target": targets[0]},
             {"id": "RUNNER", "qty": ultra_leg_split[1], "target": targets[1]}]
            if ultra_leg_split is not None else
            [{"id": "TP1", "qty": 1, "target": targets[0]},
             {"id": "RUNNER", "qty": 1, "target": targets[1]}])
    if raw.get("legs") != legs:
        raise ValueError("SPLIT_LEGS_INVALID")
    plan_version = str(raw.get("planVersion") or "")[:64]
    if not plan_version:
        raise ValueError("SPLIT_LEGS_INVALID")
    evidence_hash = str(raw.get("evidenceHash") or "")[:128] or None
    frozen_strategy_evidence = raw.get("strategyEvidence") if isinstance(raw.get("strategyEvidence"), dict) else None
    eligible_votes = raw.get("eligibleVotes") if isinstance(raw.get("eligibleVotes"), dict) else {}
    # A packet is either completely versioned with frozen evidence or legacy.
    # Never emit a partial version tuple that downstream OOS cannot isolate.
    if evidence_hash or frozen_strategy_evidence:
        if not (evidence_hash and frozen_strategy_evidence):
            raise ValueError("versioned scenario requires strategyEvidence and evidenceHash together")
        frozen_evidence = strategy_evidence.canonicalize(frozen_strategy_evidence)
        if frozen_evidence is None or frozen_evidence["evidenceHash"] != evidence_hash:
            raise ValueError("scenario evidenceHash must match canonical strategyEvidence")
        required_versions = ("setupVersion", "catalogVersion", "detectorVersion", "executionContractVersion")
        if any(not str(raw.get(field) or "").strip() for field in required_versions):
            raise ValueError("versioned scenario immutable version set is incomplete")
        frozen_strategy_evidence = frozen_evidence
        setup_version = str(raw["setupVersion"])[:64]
        catalog_version = str(raw["catalogVersion"])[:64]
        detector_version = str(raw["detectorVersion"])[:64]
        execution_contract_version = str(raw["executionContractVersion"])
    else:
        setup_version = catalog_version = detector_version = execution_contract_version = None

    fingerprint = scenario_fingerprint({
        **raw, "symbol": symbol, "targets": targets, "targetR": clean_target_r,
        "legs": legs, "planVersion": plan_version,
        "setupVersion": setup_version, "catalogVersion": catalog_version,
        "detectorVersion": detector_version,
        "executionContractVersion": execution_contract_version,
        "evidenceHash": evidence_hash,
    })
    return {
        "scenarioId": raw.get("scenarioId") or f"{fingerprint}@{observed.isoformat()}",
        "fingerprint": fingerprint,
        "state": (state or raw.get("state") or "WATCH").upper(),
        "symbol": symbol,
        "side": side,
        "qty": qty,
        "entry": entry,
        "stop": stop,
        "target": target,
        "targets": targets,
        "targetR": clean_target_r,
        "legs": legs,
        "planVersion": plan_version,
        "setupVersion": setup_version,
        "catalogVersion": catalog_version,
        "detectorVersion": detector_version,
        "executionContractVersion": execution_contract_version,
        # The producer validates full frozen evidence above, but the scenario
        # wire is intentionally hash-only.  The paired market is the sole
        # full-evidence transport/storage path.
        "eligibleVotes": eligible_votes,
        "evidenceHash": evidence_hash,
        "marketCycleId": (str(raw.get("marketCycleId")).strip() if raw.get("marketCycleId") else None),
        "grade": str(raw.get("grade") or "").upper() or None,
        # R56 (2026-09-05): model を落とさない。ここに無かったため、engine の
        # _displayModel が一度も付かず、凍結プラン → result → スコアカードの model が
        # 全部 null(UNATTRIBUTED)になっていた(title には同じ名前が入っていた)。
        # 表示・集計専用。Worker は未知キーとして黙って落とすので発注判定は変わらない。
        "model": (str(raw.get("model"))[:32] if raw.get("model") else None),
        "title": raw.get("title"),
        "reason": raw.get("reason"),
        "trigger": raw.get("trigger"),
        "invalidation": raw.get("invalidation"),
        "issuedAt": observed.isoformat(),
        "observedAt": observed.isoformat(),
        "expiresAt": expires.isoformat(),
        "snapshotAt": observed.isoformat(),
    }


def _reader_user_id(cfg):
    """launch token に載せる Telegram user id を解決する。"""
    if cfg.get("NQX_USER_ID"):
        return cfg["NQX_USER_ID"]
    telegram_env = os.path.join(BASE, ".secrets", "telegram.env")
    if os.path.exists(telegram_env):
        with open(telegram_env, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("TELEGRAM_CHAT_ID="):
                    return line.split("=", 1)[1].strip()
    return None


def fetch_state_quiet(cfg=None):
    """Durable Object の現在状態を読む。取れなければ None。

    None は「状態が無い」ではなく「確認できていない」。呼び出し側はこれを
    根拠にポジションを否定しないこと。
    """
    try:
        cfg = cfg or load_cloud_env()
    except NotConfigured:
        return None
    user_id = _reader_user_id(cfg)
    if not user_id or not cfg.get("NQX_LAUNCH_SECRET"):
        return None
    try:
        token = issue_launch_token(user_id, ttl_seconds=120, cfg=cfg)
        request = urllib.request.Request(
            cfg["NQX_API_BASE"].rstrip("/") + "/api/state",
            headers={
                "X-NQX-Launch": token,
                "User-Agent": "NQX-Nightwatch-State/1.0",
            }, method="GET",
        )
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
            body = json.loads(response.read().decode("utf-8", "replace"))
        return body.get("view") if body.get("ok") else None
    except Exception:
        return None


def fetch_account_prefs(cfg=None, view=None):
    """Mini App が保存した口座別設定(ULTRA 対象・利益目標・DD上限)を読む。

    正本は Worker/Durable Object の ``view.accountPrefs``。取れなければ空 dict。
    空は「設定なし」と「確認できず」の両方を含む — 確認できないときに ULTRA を
    発火させない(fail-closed)ためにこの縮退で正しい。
    """
    view = view if isinstance(view, dict) else fetch_state_quiet(cfg)
    prefs = ((view or {}).get("accountPrefs") or {}).get("map")
    if not isinstance(prefs, dict):
        return {}
    cleaned = {}
    for account_id, row in prefs.items():
        if not isinstance(row, dict):
            continue
        cleaned[str(account_id)] = {
            "ultra": row.get("ultra") is True,
            "profitTarget": row.get("profitTarget"),
            "maxDrawdown": row.get("maxDrawdown"),
        }
    return cleaned


def publish_scenario(scenario, cfg=None):
    return publish("scenario", {"scenario": scenario}, cfg)


def clear_scenario(state="CANCELED", cfg=None):
    """表示中のシナリオを明示的に降ろす。理由を必ず添える。"""
    return publish("scenario", {"scenario": None, "state": state}, cfg)


# ---------------------------------------------------------------- 建玉に凍結プランの水準を重ねる

_POSITION_SIDE_TO_PLAN = {"LONG": "BUY", "BUY": "BUY", "SHORT": "SELL", "SELL": "SELL"}
#: 建値の突合許容。成行の avgEntry は複数約定の加重平均で tick に乗らず、
#: 滑りも乗る(R52 実測 29570.375)。リスク幅の半分か 5pt の広い方。
_OVERLAY_ENTRY_TOLERANCE_PT = 5.0


def _finite(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def position_plan_overlay(position, ledger_path=None):
    """ブローカー建玉行に、台帳の凍結プランから ``stop`` / ``target`` を重ねる。

    CrossTrade/Tradovate の建玉照会は SL/TP を返さない(注文行にも数量・種別・
    価格が無い — R52)。``sync_position`` はその生の行をそのまま publish して
    いたので、Worker の position は常に ``stop=null / target=null`` になり、
    Mini App の保有中カードは SL / TP を空(—)で描き続けていた(2026-09-08)。

    水準の正本は **凍結プラン**(``autotrade_ledger.jsonl`` の ENTRY プラン)。
    SL は TP1 後の建値移動/トレールで動くので、同じプランに対する最後の
    ``MANAGEMENT_SENT`` の ``action.sl`` を優先し、無ければ ``initialStop``。
    TP は runner の最終目標 ``finalTarget``(無ければ targets の末尾)。

    **推測で水準を作らない。** 次のどれかなら空の辞書を返し、行は素通しになる:
      - 建玉が無い / 照会が verified でない
      - 台帳が読めない、または凍結プランが無い(ENTRY_HALTED / TERMINAL 含む)
      - シンボルか向きがプランと食い違う
      - 建値が分かっていてプランの建値から許容を超えて離れている(手動建玉)
    表示専用で、発注・変更・照会には一切影響しない。失敗は握って空を返す。
    """
    if not isinstance(position, dict):
        return {}
    try:
        qty = int(position.get("qty") or 0)
    except (TypeError, ValueError):
        return {}
    if qty <= 0 or position.get("verified") is not True:
        return {}
    try:
        import autotrade_engine  # 遅延: engine 側は nqx_state を関数内でしか import しない
        records, error = autotrade_engine._read_ledger(
            ledger_path or autotrade_engine.LEDGER_FILE)
        if error:
            return {}
        symbol = str(position.get("symbol") or "")
        account = position.get("accountId") or position.get("account")
        plan = autotrade_engine._frozen_plan(records, symbol,
                                             account=str(account) if account else None)
        if not isinstance(plan, dict):
            return {}
        want = _POSITION_SIDE_TO_PLAN.get(str(position.get("side") or "").upper())
        if not want or str(plan.get("side") or "").upper() != want:
            return {}
        plan_entry = _finite(plan.get("entry"))
        avg_entry = _finite(position.get("avgEntry"))
        initial_stop = _finite(plan.get("initialStop"))
        if initial_stop is None:
            initial_stop = _finite(plan.get("stop"))
        if plan_entry is not None and avg_entry is not None:
            risk = abs(plan_entry - initial_stop) if initial_stop is not None else 0.0
            tolerance = max(_OVERLAY_ENTRY_TOLERANCE_PT, 0.5 * risk)
            if abs(avg_entry - plan_entry) > tolerance:
                return {}
        stop = initial_stop
        plan_key = autotrade_engine._plan_entry_key(plan)
        if plan_key:
            for record in records:
                if record.get("status") != "MANAGEMENT_SENT":
                    continue
                key = record.get("planEntryKey") or autotrade_engine._plan_entry_key(record.get("plan"))
                if str(key or "") != str(plan_key):
                    continue
                action = record.get("action") if isinstance(record.get("action"), dict) else {}
                moved = _finite(action.get("sl"))
                if moved is not None:
                    stop = moved
        target = _finite(plan.get("finalTarget"))
        if target is None:
            targets = [_finite(t) for t in (plan.get("targets") or [])]
            targets = [t for t in targets if t is not None]
            target = targets[-1] if targets else None
        out = {}
        if stop is not None and _finite(position.get("stop")) is None:
            out["stop"] = stop
        if target is not None and _finite(position.get("target")) is None:
            out["target"] = target
        return out
    except Exception:  # noqa: BLE001 - 表示のための重ね合わせで publish を止めない
        return {}


def publish_position(position, closed_at=None, cfg=None):
    if isinstance(position, dict) and int(position.get("qty") or 0) > 0:
        try:
            import broker_status
            generation = broker_status.position_identity(position)
        except Exception:  # noqa: BLE001 - never guess a broker identity
            generation = None
        position = dict(position)
        position["positionGeneration"] = generation
    payload = {"position": position}
    if closed_at:
        payload["closedAt"] = closed_at
    return publish("position", payload, cfg)


def publish_order(order, cfg=None):
    return publish("order", {"order": order}, cfg)


def resolve_stuck_order(to_state, detail=None, cfg=None, view=None):
    """order ストリームに残った PENDING/SENT/PARTIAL/UNKNOWN を終端状態へ進める。

    order.py は nqx_state を知らないため、送信後の注文を FILLED/CANCELED に
    進める経路がここ以外に無い。現在の order が blocking でなければ何もしない。

    戻り値 ``(published: bool, detail: dict)``。触らなかった場合は
    published=False で ``detail["skipped"]`` に理由が入る。
    """
    to_state = str(to_state).upper()
    if to_state not in TERMINAL_ORDER_STATES:
        raise ValueError(f"terminal order state expected, got {to_state}")
    if view is None:
        view = fetch_state_quiet(cfg)
    if not isinstance(view, dict):
        return False, {"reason": "state unavailable"}
    order = view.get("order")
    if not isinstance(order, dict):
        return False, {"skipped": "no order in state"}
    current = str(order.get("state") or "").upper()
    if current not in BLOCKING_ORDER_STATES:
        return False, {"skipped": f"order is already {current or 'terminal'}"}
    updated = {
        "idempotencyKey": order.get("idempotencyKey"),
        "scenarioId": order.get("scenarioId"),
        "state": to_state,
        "side": order.get("side"),
        "qty": order.get("qty"),
        "entry": order.get("entry"),
        "stop": order.get("stop"),
        "target": order.get("target"),
        "receipt": order.get("receipt"),
        "detail": detail or f"resolved from {current}",
        "at": datetime.now(timezone.utc).isoformat(),
    }
    ok, publish_detail = publish_order(updated, cfg)
    if isinstance(publish_detail, dict):
        publish_detail = dict(publish_detail, resolvedFrom=current, resolvedTo=to_state)
    return ok, publish_detail


def publish_market(market, cfg=None):
    return publish("market", {"market": market}, cfg)


def publish_cycle(cycle_id, market, scenario, cfg=None):
    """Atomically publish a single market/scenario pair to the Durable Object.

    The Worker validates both sides before committing either.  This prevents a
    fresh chart from being displayed with a previous armed scenario.
    """
    return publish("cycle", {"cycleId": str(cycle_id), "market": market,
                             "scenario": scenario}, cfg)


def publish_cycle_beacon(status, reason=None, kill=None, cfg=None):
    """監視ループのビーコン(「監視の監視」)。

    BLOCKED / HALT のサイクルは market・scenario を publish しないため、
    Mini App からは沈黙の長さしか見えない。この別便が生死と理由を届ける。
    表示専用 — Worker 側 applyCycleHealthEvent は armed 状態に一切触れない。

    呼び出し元(nqx_cycle)は失敗しても本流を止めないこと。ここでも例外は
    握って ``(False, detail)`` に落とす。
    """
    if status not in ("PUBLISHED", "BLOCKED", "HALT"):
        return False, {"reason": f"unknown beacon status {status}"}
    beacon = {
        "status": status,
        "at": datetime.now(timezone.utc).isoformat(),
        "reason": None if reason is None else str(reason)[:500],
        "kill": bool(kill),
    }
    try:
        return publish("cycle_health", {"beacon": beacon}, cfg)
    except NotConfigured as exc:
        return False, {"reason": str(exc)}
    except Exception as exc:  # noqa: BLE001 — ビーコンで本流を殺さない
        return False, {"reason": f"{type(exc).__name__}: {exc}"}


def entry_tuple(scenario):
    """Return the only tuple allowed to control ENTRY idempotency."""
    if not isinstance(scenario, dict):
        raise ValueError("scenario is required for entry claim")
    frozen = {}
    for field in ("scenarioId", "fingerprint", "evidenceHash", "marketCycleId"):
        value = str(scenario.get(field) or "").strip()
        if not value:
            raise ValueError(f"scenario.{field} is required for entry claim")
        frozen[field] = value
    return frozen


def entry_key(scenario):
    frozen = entry_tuple(scenario)
    material = json.dumps(frozen, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "ENTRY:" + hashlib.sha256(material.encode("utf-8")).hexdigest()


def _claim_token_hash(token):
    token = str(token or "")
    if len(token) < 32:
        raise ValueError("entry claim token is invalid")
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def claim_entry(scenario, cfg=None, publish_fn=None, release_entry_key=None,
                broker_position_verified_flat=False,
                broker_order_verified_terminal=False,
                order_type="LIMIT", pyramid=None):
    """Atomically reserve one ENTRY tuple in the Durable Object.

    The plaintext bearer token never enters Durable Object storage.  A network
    failure is a hard refusal; callers must not route an order without the
    returned token.
    """
    token = secrets.token_urlsafe(32)
    key = entry_key(scenario)
    sender = publish_fn or publish
    payload = {
        "action": "CLAIM", "entryKey": key, "tuple": entry_tuple(scenario),
        "claimTokenHash": _claim_token_hash(token),
        "orderType": str(order_type or "").upper(),
    }
    # R84: 追撃(pyramid)の申告。**DO は自分の state で全部再検証する** —— ここで
    # 送るのは「どの建玉へ、何枚足すつもりか」の宣言であって、許可の根拠ではない。
    # Worker が未対応の間はこのキーごと拒否されるので、engine は PYRAMID_SKIPPED へ
    # 落ちて安全に素通りする(順序依存が無い)。
    if isinstance(pyramid, dict) and pyramid:
        payload["pyramid"] = {
            "baseQty": int(pyramid.get("baseQty") or 0),
            "baseAvgEntry": (float(pyramid["baseAvgEntry"])
                             if pyramid.get("baseAvgEntry") is not None else None),
            "addQty": int(pyramid.get("addQty") or 0),
            "addsDone": int(pyramid.get("addsDone") or 0),
            "positionGeneration": str(pyramid.get("positionGeneration") or ""),
            "preSendOrderIds": sorted({str(value) for value in
                                       pyramid.get("preSendOrderIds") or []})[:64],
        }
    # R22: a caller cannot self-report flat/terminal to release an older
    # journal.  RECOVER succeeds only against the DO's fresh broker observation.
    ok, detail = sender("entry_claim", payload, cfg)
    if not ok:
        return False, detail
    claim = (((detail or {}).get("view") or {}).get("entryClaim")
             if isinstance(detail, dict) else None)
    if not isinstance(claim, dict) or claim.get("entryKey") != key:
        return False, {"reason": "ENTRY_CLAIM_RESPONSE_INVALID"}
    try:
        intent = execution_intent.normalize(claim.get("executionIntent"))
        intent_hash = execution_intent.intent_hash(intent)
    except ValueError as exc:
        return False, {"reason": f"ENTRY_CLAIM_INTENT_INVALID: {exc}"}
    if intent_hash != str(claim.get("executionIntentHash") or ""):
        return False, {"reason": "ENTRY_CLAIM_INTENT_HASH_MISMATCH"}
    return True, {"entryKey": key, "claimToken": token,
                  "executionIntent": intent, "executionIntentHash": intent_hash,
                  "detail": detail}


def consume_entry_claim(entry_key_value, claim_token, execution_intent_value,
                        execution_intent_hash, cfg=None, publish_fn=None):
    sender = publish_fn or publish
    normalized = execution_intent.normalize(execution_intent_value)
    computed_hash = execution_intent.intent_hash(normalized)
    if computed_hash != str(execution_intent_hash or ""):
        return False, {"reason": "ENTRY_CLAIM_INTENT_HASH_MISMATCH"}
    return sender("entry_claim", {
        "action": "CONSUME", "entryKey": str(entry_key_value),
        "claimTokenHash": _claim_token_hash(claim_token),
        "executionIntent": normalized, "executionIntentHash": computed_hash,
    }, cfg)


def resolve_entry_claim(entry_key_value, claim_token, route_state,
                        accepted_count, total_attempts, explicit_reject_count=0,
                        route_snapshot=None, cfg=None, publish_fn=None):
    sender = publish_fn or publish
    return sender("entry_claim", {
        "action": "RESOLVE", "entryKey": str(entry_key_value),
        "claimTokenHash": _claim_token_hash(claim_token),
        "routeState": str(route_state).upper(),
        "acceptedCount": int(accepted_count), "totalAttempts": int(total_attempts),
        "explicitRejectCount": int(explicit_reject_count),
        "routeSnapshot": route_snapshot if isinstance(route_snapshot, list) else [],
    }, cfg)


def _recovery_snapshot_fields(snapshot):
    if not isinstance(snapshot, dict):
        raise ValueError("broker recovery snapshot is required")
    snapshot_hash = str(snapshot.get("snapshotHash") or "")
    snapshot_id = str(snapshot.get("snapshotId") or "")
    cursor = str(snapshot.get("cursor") or "")
    if not re.fullmatch(r"bo_[a-f0-9]{64}", snapshot_hash) or not snapshot_id:
        raise ValueError("broker recovery snapshot identity is invalid")
    return {"brokerSnapshotHash": snapshot_hash, "brokerSnapshotId": snapshot_id,
            "brokerCursor": cursor}


def recover_entry_claim(entry_key_value, claim_token, broker_snapshot,
                        cfg=None, publish_fn=None):
    """Ask the DO to verify its own fresh broker observation journal."""
    sender = publish_fn or publish
    return sender("entry_claim", {
        "action": "RECOVER", "entryKey": str(entry_key_value),
        "claimTokenHash": _claim_token_hash(claim_token),
        **_recovery_snapshot_fields(broker_snapshot),
    }, cfg)


def claim_management(intent_value, cfg=None, publish_fn=None):
    """Acquire the account-scoped Durable Object CAS for one MODIFY intent."""
    intent = management_intent.normalize(intent_value)
    key = management_intent.management_key(intent)
    token = secrets.token_urlsafe(32)
    sender = publish_fn or publish
    ok, detail = sender("management_claim", {
        "action": "CLAIM", "managementKey": key,
        "claimTokenHash": _claim_token_hash(token), "managementIntent": intent,
        "managementIntentHash": management_intent.intent_hash(intent),
    }, cfg)
    if not ok:
        return False, detail
    claim = (((detail or {}).get("view") or {}).get("managementClaim")
             if isinstance(detail, dict) else None)
    if (not isinstance(claim, dict) or claim.get("managementKey") != key
            or claim.get("managementIntentHash") != management_intent.intent_hash(intent)):
        return False, {"reason": "MANAGEMENT_CLAIM_RESPONSE_INVALID"}
    return True, {"managementKey": key, "claimToken": token,
                  "managementIntent": intent,
                  "managementIntentHash": management_intent.intent_hash(intent),
                  "detail": detail}


def consume_management_claim(key, token, intent_value, intent_hash_value,
                             cfg=None, publish_fn=None):
    intent = management_intent.normalize(intent_value)
    computed = management_intent.intent_hash(intent)
    if computed != str(intent_hash_value or ""):
        return False, {"reason": "MANAGEMENT_INTENT_HASH_MISMATCH"}
    return (publish_fn or publish)("management_claim", {
        "action": "CONSUME", "managementKey": str(key),
        "claimTokenHash": _claim_token_hash(token),
        "managementIntent": intent, "managementIntentHash": computed,
    }, cfg)


def resolve_management_claim(key, token, route_state, *, receipt=None,
                             cfg=None, publish_fn=None):
    return (publish_fn or publish)("management_claim", {
        "action": "RESOLVE", "managementKey": str(key),
        "claimTokenHash": _claim_token_hash(token),
        "routeState": str(route_state or "UNKNOWN").upper(),
        "routeReceipt": receipt if isinstance(receipt, dict) else None,
    }, cfg)


def recover_management_claim(key, token, broker_snapshot, *, cfg=None, publish_fn=None):
    """Ask the DO to verify its own fresh broker observation journal."""
    return (publish_fn or publish)("management_claim", {
        "action": "RECOVER", "managementKey": str(key),
        "claimTokenHash": _claim_token_hash(token),
        **_recovery_snapshot_fields(broker_snapshot),
    }, cfg)


def _iso_ms(value):
    raw = str(value or "").strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw).astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None
    return parsed


def _position_snapshot_hash(position):
    # R40: このハッシュは「二重読みの **間に建玉が動いていない**」ことだけを
    # 見るためのもので、観測時刻を含めてはいけない。``closedAt`` は
    # broker_status がブローカーの ``timestamp`` を得られないとき観測時刻へ
    # フォールバックする(CrossTrade/Tradovate の建玉 payload に timestamp は
    # 無い)。結果 **フラットな口座では毎回値が変わり**、
    # BROKER_OBSERVATION_UNSTABLE_POSITION が必ず立つ = 二重読みが原理的に
    # 成立しない。回復が必要なのはまさにフラットな局面なので、ここが塞がると
    # ENTRY claim を終端できない(2026-08-25 に発生)。
    # qty が 0 のまま closedAt だけが動くのは建玉の変化ではない。
    #
    # 除外はこの 2 つだけに留める。建玉が実際に入れ替われば qty/side/orderId/
    # receipt/filledAt/initialQty のどれかが必ず動き、それらは material に残る。
    material = {key: position.get(key) for key in sorted(position)
                if key not in {"observedAt", "closedAt"}}
    raw = json.dumps(material, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _js_iso(value):
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def build_broker_observation(position, orders, current_intent_hash, *,
                             before_position=None, after_position=None,
                             cursor=None, snapshot_id=None):
    """Canonical R22 atomic broker observation; no I/O is performed."""
    """Publish read-only broker truth used by DO-side crash recovery."""
    if not isinstance(position, dict) or position.get("verified") is not True:
        raise ValueError("BROKER_OBSERVATION_POSITION_UNVERIFIED")
    if not isinstance(orders, dict) or orders.get("verified") is not True:
        raise ValueError("BROKER_OBSERVATION_ORDERS_UNVERIFIED")
    account = str(position.get("accountId") or position.get("account") or "")
    symbol = str(position.get("symbol") or orders.get("symbol") or "")
    position_at = str(position.get("observedAt") or "")
    orders_at = str(orders.get("observedAt") or "")
    position_dt, orders_dt = _iso_ms(position_at), _iso_ms(orders_at)
    if not account or not symbol or not position_dt or not orders_dt:
        raise ValueError("BROKER_OBSERVATION_IDENTITY_TIME_INVALID")
    max_skew = float(execution_contract.CONTRACT["brokerObservation"]["maxComponentSkewSec"])
    if abs((position_dt - orders_dt).total_seconds()) > max_skew:
        raise ValueError("BROKER_OBSERVATION_COMPONENT_SKEW")
    observed_dt = max(position_dt, orders_dt)
    rows = []
    for row in orders.get("orders") or []:
        if isinstance(row, dict):
            rows.append({"orderId": str(row.get("orderId") or ""),
                         "receipt": str(row.get("receipt") or ""),
                         "accountId": str(row.get("accountId") or row.get("account") or ""),
                         "symbol": str(row.get("symbol") or ""),
                         "status": str(row.get("status") or "").upper()})
    rows.sort(key=lambda row: (str(row.get("orderId") or ""), str(row.get("receipt") or "")))
    position_generation = position.get("positionGeneration")
    import broker_status
    raw_identity = broker_status.position_identity(position)
    if int(position.get("qty") or 0) > 0:
        position_generation = position_generation or raw_identity
        if not raw_identity:
            raise ValueError("BROKER_OBSERVATION_RAW_POSITION_IDENTITY_MISSING")
    platform = str(orders.get("platform") or position.get("platform") or "STUB").upper()
    if cursor:
        mode, before_hash, after_hash = "BROKER_CURSOR", None, None
    else:
        before = before_position if isinstance(before_position, dict) else position
        after = after_position if isinstance(after_position, dict) else position
        before_hash, after_hash = _position_snapshot_hash(before), _position_snapshot_hash(after)
        if before_hash != after_hash:
            raise ValueError("BROKER_OBSERVATION_UNSTABLE_POSITION")
        mode = "STABLE_DOUBLE_READ"
    seed = {"at": _js_iso(observed_dt), "account": account,
            "symbol": symbol, "intent": str(current_intent_hash or ""),
            "cursor": str(cursor or ""), "before": before_hash, "orders": rows}
    if not snapshot_id:
        snapshot_id = "bs_" + hashlib.sha256(json.dumps(
            seed, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()[:32]
    observation = {
        "observedAt": _js_iso(observed_dt),
        "positionObservedAt": _js_iso(position_dt),
        "ordersObservedAt": _js_iso(orders_dt),
        "snapshotId": str(snapshot_id), "cursor": str(cursor) if cursor else None,
        "snapshotMode": mode, "stableBeforeHash": before_hash,
        "stableAfterHash": after_hash, "platform": platform,
        "accountId": account, "symbol": symbol,
        "currentIntentHash": str(current_intent_hash or ""),
        "position": {"verified": True, "qty": int(position.get("qty") or 0),
                     "side": str(position.get("side") or "FLAT").upper(),
                     "positionGeneration": position_generation,
                     "rawPositionIdentity": raw_identity},
        "orders": rows,
    }
    canonical = json.dumps(observation, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":")).encode("utf-8")
    observation["snapshotHash"] = "bo_" + hashlib.sha256(canonical).hexdigest()
    return observation


def publish_broker_observation(position, orders, current_intent_hash,
                               cfg=None, publish_fn=None, **snapshot_kwargs):
    """Publish one canonical read-only broker snapshot for DO recovery CAS."""
    try:
        observation = build_broker_observation(
            position, orders, current_intent_hash, **snapshot_kwargs)
    except ValueError as exc:
        return False, {"reason": str(exc)}
    ok, detail = (publish_fn or publish)(
        "broker_observation", {"observation": observation}, cfg)
    if isinstance(detail, dict):
        detail = {**detail, "observation": observation}
    return ok, detail


def recover_management_from_broker(key, token, claim, position=None, orders=None,
                                   cfg=None, publish_fn=None, position_query=None,
                                   orders_query=None):
    """Re-query exact broker truth, journal it, then CAS-recover MANAGEMENT."""
    if not isinstance(claim, dict) or str(claim.get("managementKey") or "") != str(key):
        return False, {"reason": "MANAGEMENT_CLAIM_JOURNAL_MISMATCH"}
    intent_hash = str(claim.get("managementIntentHash") or "")
    intent = claim.get("managementIntent") if isinstance(claim.get("managementIntent"), dict) else {}
    symbol = str(intent.get("symbol") or "")
    receipt_rows = ((claim.get("routeReceipt") or {}).get("accounts")
                    if isinstance(claim.get("routeReceipt"), dict) else [])
    # R52: RESOLVE を UNKNOWN で閉じた claim は routeReceipt に accounts を持たない
    # (2026-09-05 02:54、None を反復して TypeError → reconcile ごと落ち publish 失敗)。
    receipt_rows = receipt_rows if isinstance(receipt_rows, list) else []
    frozen_ids = sorted({str(value) for row in receipt_rows if isinstance(row, dict)
                         for value in (row.get("stopOrderId"), row.get("targetOrderId"))
                         if value not in (None, "")})
    if not intent_hash or not symbol:
        return False, {"reason": "MANAGEMENT_CLAIM_INTENT_HASH_MISSING"}
    if not frozen_ids:
        # R52: UNKNOWN で RESOLVE した claim は routeReceipt に accounts を持たない。
        # 凍結 ID が無ければ終端証明は組めない(理由を intent hash と混同しない)。
        return False, {"reason": "MANAGEMENT_CLAIM_FROZEN_IDS_MISSING"}
    if position_query is None or orders_query is None:
        import broker_status
        position_query = position_query or broker_status.query_position
        orders_query = orders_query or broker_status.query_orders
    before = position_query(symbol)
    try:
        current_orders = orders_query(symbol, known_order_ids=frozen_ids)
    except TypeError:
        current_orders = orders_query(symbol)
    after = position_query(symbol)
    raw_identity = None
    try:
        import broker_status
        raw_identity = broker_status.position_identity(after)
    except (TypeError, ValueError):
        raw_identity = None
    terminal = set(execution_contract.CONTRACT["brokerObservation"]["terminalStates"])
    rows = [row for row in (current_orders or {}).get("orders") or [] if isinstance(row, dict)]
    # A broker-acquired receipt is per replacement order; the legacy
    # response-body route froze one shared request receipt for both.
    expected_receipts = {}
    for row in receipt_rows:
        if not isinstance(row, dict):
            continue
        shared = str(row.get("receipt") or "")
        if row.get("stopOrderId") not in (None, ""):
            expected_receipts[str(row.get("stopOrderId"))] = str(row.get("stopReceipt") or shared)
        if row.get("targetOrderId") not in (None, ""):
            expected_receipts[str(row.get("targetOrderId"))] = str(row.get("targetReceipt") or shared)
    if (not raw_identity or raw_identity != str(intent.get("positionGeneration") or "")
            or not isinstance(before, dict) or before.get("verified") is not True
            or not isinstance(after, dict) or after.get("verified") is not True
            or int(before.get("qty") or 0) != int(intent.get("qty") or -1)
            or int(after.get("qty") or 0) != int(intent.get("qty") or -1)
            or not isinstance(current_orders, dict) or current_orders.get("verified") is not True
            or not all(len([row for row in rows
                            if str(row.get("orderId") or "") == order_id
                            and str(row.get("receipt") or "") == expected_receipts[order_id]
                            and str(row.get("status") or "").upper() in terminal]) == 1
                       for order_id in frozen_ids)):
        return False, {"reason": "MANAGEMENT_CURRENT_BROKER_PROOF_INVALID"}
    observed, detail = publish_broker_observation(
        after, current_orders, intent_hash, cfg=cfg, publish_fn=publish_fn,
        before_position=before, after_position=after)
    if not observed:
        return False, detail
    return recover_management_claim(
        key, token, detail.get("observation") if isinstance(detail, dict) else None,
        cfg=cfg, publish_fn=publish_fn)


# ---------------------------------------------------------------- 口座残機(LIFELINE)

CROSSTRADE_ENV = os.path.join(BASE, ".secrets", "crosstrade.env")


def _read_kv_env(path):
    cfg = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    cfg[key.strip()] = value.strip()
    return cfg


HIGHWATER_PATH = os.path.join(BASE, ".secrets", "account_highwater.json")


def _read_highwater():
    """口座別の EOD 純資産の高値を読む。壊れていれば空(=推測しない)。"""
    try:
        with open(HIGHWATER_PATH, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _update_highwater(account_id, net_liq_sod, store):
    """EOD 高値を切り上げる。**下げない**。

    トレーリング DD の床は「一度上がったら戻らない」。ここで下げてしまうと
    床が緩んで残機を過大に見せるので、単調増加だけを許す。
    """
    if net_liq_sod is None:
        return store.get(account_id, {}).get("highWaterNetLiqSOD")
    row = store.get(account_id)
    prior = row.get("highWaterNetLiqSOD") if isinstance(row, dict) else None
    best = net_liq_sod if prior is None else max(float(prior), float(net_liq_sod))
    store[account_id] = {"highWaterNetLiqSOD": best,
                         "lastNetLiqSOD": float(net_liq_sod)}
    return best


def _write_highwater(store):
    try:
        os.makedirs(os.path.dirname(HIGHWATER_PATH), exist_ok=True)
        tmp = HIGHWATER_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(store, handle, ensure_ascii=False, indent=2, sort_keys=True)
        os.replace(tmp, HIGHWATER_PATH)
    except OSError:
        # 書けなくても残機の算出そのものは今サイクル分だけ成立する。
        # 次サイクルで床が緩まないよう、失敗は黙って無視せず False を返す。
        return False
    return True


def build_accounts_payload(env=None, now=None, balances=None, roster=None, prefs=None):
    """口座別の残機(LIFELINE)を組み立てる。

        CROSSTRADE_ACCOUNTS=ID1,ID2,ID3   発注対象(order.py と同じ正)
        RISK_<口座ID>=60                  1トレードのリスク上限 $
        LIFELINE_<口座ID>=816             撤退ラインまでの残り $(手入力・後方互換)
        TRAILING_DD_<口座ID>=3000         EOD トレーリング DD 幅 $(R44・自動化の鍵)
        FLOOR_<口座ID>=97000              撤退ラインの純資産(任意・明示指定)
        DD_LOCK_FLOOR_<口座ID>=50100      トレーリングの床が止まる純資産(任意・R85)
        TARGET_<口座ID>=3000              利益目標 $(ULTRA mode の枚数計算用)

    R44: 残機はブローカーから引けるようになった。`GET /accounts/{name}` の
    `balance.netLiq` と `balance.netLiqSOD` が正本で、

        床      = max(EOD純資産の高値, 初回観測の netLiqSOD) − TRAILING_DD
        残機    = max(0, netLiq − 床)

    `netLiqSOD` は当日始値 = 前営業日の EOD なので、これを単調増加で覚えるだけで
    トレーリングが再現できる。床の明示指定 `FLOOR_*` があればそちらが優先。

    R85(2026-09-14 ユーザー確認): Lucid 50K の funded は、残高が $52,100 を超えて日を
    終えると最低残高が **$50,100 で固定**される。幅 $2,000 の床は EOD 高値 $52,100 で
    ちょうど $50,100 に届くので、`DD_LOCK_FLOOR_*` を置くと
    `床 = min(EOD 高値 − TRAILING_DD, DD_LOCK_FLOOR)` になる(固定前はトレーリングのまま、
    届いた後は上がらない)。固定後もトレーリングを続けると余力を実際より小さく見積もる
    (2026-09-13: 表示 $2,000 / 実際 $3,557)。

    自動化できない場合(残高が照会できない / TRAILING_DD も FLOOR も未設定)は
    従来どおり `LIFELINE_*` の手入力へ倒す。**推測で数字を作らない。**

    それまでこの値は手入力だけが正本で、CLAUDE.md 自身が「更新漏れはそのまま
    残機の誤認になる」と警告していた。2026-08-26 03:14 の実測では設定値
    2,973.90 に対し実際は 2,868.10 で、直前の損切り $105.80 が未反映だった。

    TARGET_* は ULTRA mode 専用の任意項目。未設定の口座は ULTRA の枚数を
    計算できず INELIGIBLE になる(推測して枚数を作らない)。
    """
    env = env if env is not None else _read_kv_env(CROSSTRADE_ENV)
    raw_accounts = str(env.get("CROSSTRADE_ACCOUNTS") or env.get("CROSSTRADE_ACCOUNT") or "")
    ids = [a.strip() for a in re.split(r"[,\n]+", raw_accounts) if a.strip()]
    if not ids:
        return None

    def _env_float(key):
        raw = env.get(key)
        if raw is None:
            return None
        try:
            return float(raw)
        except ValueError:
            return None

    # 床の決め方が設定されていない口座しか無いなら、そもそもブローカーへ
    # 問い合わせない。従来経路(LIFELINE_* 手入力)を通るだけの呼び出しに
    # ネットワーク往復を足さないための門。
    wants_live = any(env.get(f"TRAILING_DD_{a}") or env.get(f"FLOOR_{a}") for a in ids)
    if balances is None and wants_live:
        try:
            import broker_status
            balances = broker_status.query_balances(ids)
        except Exception:
            balances = {}
    balances = balances if isinstance(balances, dict) else {}

    store = _read_highwater()
    store_dirty = False
    default_cap = float(env.get("MAX_RISK_DOLLARS", 60))
    entries = []
    live_used = 0
    for account_id in ids:
        balance = balances.get(account_id) or {}
        live = balance.get("verified") is True
        net_liq = balance.get("netLiq") if live else None

        buffer_value = None
        source_kind = "env"
        if live and net_liq is not None:
            high_water = _update_highwater(
                account_id, balance.get("netLiqSOD"), store)
            if balance.get("netLiqSOD") is not None:
                store_dirty = True
            floor = _env_float(f"FLOOR_{account_id}")
            if floor is None:
                trailing = _env_float(f"TRAILING_DD_{account_id}")
                if trailing is not None and trailing > 0 and high_water is not None:
                    floor = float(high_water) - trailing
                    lock = _env_float(f"DD_LOCK_FLOOR_{account_id}")
                    if lock is not None and lock > 0:
                        floor = min(floor, lock)
            if floor is not None:
                # セント単位へ丸める。99868.1 - 97000.0 が 2868.100000000006 に
                # なる程度の誤差だが、画面にそのまま出ると値が信用されなくなる。
                buffer_value = round(max(0.0, float(net_liq) - floor), 2)
                source_kind = "broker"

        if buffer_value is None:
            fallback = _env_float(f"LIFELINE_{account_id}")
            if fallback is None:
                continue
            buffer_value = max(0.0, fallback)

        cap_value = _env_float(f"RISK_{account_id}")
        if cap_value is None:
            cap_value = default_cap
        entry = {"id": account_id, "cap": cap_value, "buffer": buffer_value}
        if source_kind == "broker":
            live_used += 1
            entry["equity"] = round(float(net_liq), 2)
        target_value = _env_float(f"TARGET_{account_id}")
        # アプリ保存の利益目標(accountPrefs)は env の TARGET_* より優先する。
        # 入力の場は Mini App 側へ寄せる(数値の妥当性は Worker で検証済み)。
        pref_target = ((prefs or {}).get(account_id) or {}).get("profitTarget")
        try:
            if pref_target is not None and float(pref_target) > 0:
                target_value = float(pref_target)
        except (TypeError, ValueError):
            pass
        if target_value is not None and target_value > 0:
            entry["profitTarget"] = target_value
        entries.append(entry)

    if store_dirty:
        _write_highwater(store)

    if not entries:
        return None
    observed = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    source = ("crosstrade-balance" if live_used == len(entries)
              else "crosstrade-balance+env" if live_used else "crosstrade-env")
    payload = {"observedAt": observed.isoformat(), "source": source, "list": entries}

    # 口座名簿の突合(再同期)。CROSSTRADE の口座は黙って消える(2026-08-21 /
    # 08-24 の二度実測)。設定に居るのにブローカーから消えた口座(missing)、
    # ブローカー側に現れた未設定口座(unknown)、死んだ状態(dead)を毎サイクル
    # publish し、Mini App が警告を出せるようにする。表示専用 — accountScope
    # には一切影響しない(発注前の裏取りは従来どおり broker_status --accounts)。
    if roster is None and wants_live:
        try:
            import broker_status
            roster = broker_status.query_accounts()
        except Exception:  # noqa: BLE001 — 名簿が取れなくても残機 publish は続ける
            roster = None
    if isinstance(roster, dict):
        verified = roster.get("verified") is True
        rows = roster.get("accounts") or []
        broker_ids = {str(r.get("id")) for r in rows
                      if isinstance(r, dict) and r.get("id")}
        usable_ids = {str(r.get("id")) for r in rows
                      if isinstance(r, dict) and r.get("id") and r.get("usable")}
        payload["sync"] = {
            "verified": verified,
            "observedAt": str(roster.get("observedAt") or "") or None,
            # 照会が通ったときだけ差分を主張する。失敗時に「消えた」と言わない。
            "missing": sorted(a for a in ids if a not in broker_ids)[:16] if verified else [],
            "dead": sorted(a for a in ids
                           if a in broker_ids and a not in usable_ids)[:16] if verified else [],
            "unknown": sorted(a for a in usable_ids if a not in ids)[:16] if verified else [],
        }
    return payload


def publish_accounts(cfg=None, env=None, prefs=None):
    """残機を publish する。未設定なら何もせず (False, reason) を返す。

    R52(2026-09-04): Mini App が保存した口座別設定(``accountPrefs`` の
    ``profitTarget``)を **ここでも**当てる。``_apply_ultra_prefs`` は prefs を
    渡していたが、毎サイクルの残機 publish は env の ``TARGET_*`` だけで組んで
    いたため、アプリの ULTRA 表示(``accounts.list[].profitTarget`` から計算)が
    env の $3,000 で 10枚、engine は設定の $1,500 で 6枚、と食い違っていた。
    設定の正本は1か所(アプリ保存 > env)なので、publish する行も同じ値にする。
    prefs が取れないときは env へ倒れる(fetch_account_prefs の fail-closed と同じ)。
    """
    if prefs is None:
        try:
            prefs = fetch_account_prefs(cfg)
        except Exception:  # noqa: BLE001 — 設定が読めなくても残機 publish は止めない
            prefs = {}
    payload = build_accounts_payload(env=env, prefs=prefs)
    if payload is None:
        return False, {"reason": "LIFELINE_* / TRAILING_DD_* is not configured"}
    return publish("account", {"accounts": payload}, cfg)


# ---------------------------------------------------------------- 決済結果

TICK = 0.25
COST_FLOOR_PT = 2.0   # MNQ 往復コストの下限。この帯の中は FLAT 扱い(表示層と同値)


def _to_epoch(value):
    parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def build_path(bars, opened_at, closed_at, entry, exit_price):
    """保有区間の価格パスを **実観測だけ** から組む。

    使うのは publish 済みの実バー(TradingView 由来)の終値のみ。
    合成パスは本プロジェクトの統治ルールで禁止されているので生成しない。
    観測が無ければ両端 2 点だけを返し、その事実を pathSource で明示する。

    戻り値 ``(path, source)``。source は "observed-bars" か "endpoints-only"。
    """
    entry = float(entry)
    exit_price = float(exit_price)
    try:
        start = _to_epoch(opened_at)
        end = _to_epoch(closed_at)
    except (TypeError, ValueError):
        return [entry, exit_price], "endpoints-only"

    closes = []
    for bar in bars or []:
        if not isinstance(bar, dict):
            continue
        try:
            stamp = float(bar.get("t"))
            close = float(bar.get("c"))
        except (TypeError, ValueError):
            continue
        if not (start <= stamp <= end):
            continue
        closes.append((stamp, close))

    if not closes:
        # 観測が無い区間を埋めない。両端だけを返し、直線であることを隠さない。
        return [entry, exit_price], "endpoints-only"

    closes.sort()
    path = [entry] + [close for _, close in closes] + [exit_price]
    return path, "observed-bars"


# verdict は setup × 出方のテーブルから引く。固定文言だと 2 回目から効かないため。
# 判定材料は全て実データ(決済価格が TP/SL のどちら側に着いたか)であり、
# 相場の解釈はしていない。文言はここを編集すれば差し替えられる。
# 声のルール: 夜警室の口調。短く、乾いて、誇張しない。勝ちでも浮かれない。
_WIN_TARGET = [
    "They let this one through.",
    "The level paid.",
    "Filed as planned.",
    "It fell where we were looking.",
    "The trap was older than the prey.",
    "The map was right on time.",
    "Charted, waited, taken.",
    "The nova came to the lens.",
    "No chase. It came to us.",
    "The structure kept its promise.",
    "It broke where the map said.",
    "The iris opened in time.",
    "One clean shot, no witnesses.",
    "The plate does not argue.",
    "Patience, then the flare.",
    "The beam tipped with us aboard.",
    "Night work, paid in full.",
    "We asked once. It answered.",
    "Clean entry, cleaner exit.",
    "The market signed the receipt.",
    "Seen early, taken late.",
    "Right side of the silence.",
    "The line bent; we did not.",
    "One more plate for the archive.",
    "Weighed, and found profitable.",
    "Another night the plan survived.",
]
_WIN_MANUAL = [
    "Taken early. Still taken.",
    "Off the plan, on the right side.",
    "You closed it yourself.",
    "Greed declined, profit accepted.",
    "Left the table mid-feast.",
    "The exit rang early. You answered.",
    "Cash taken; questions later.",
    "You called last orders.",
    "Cut the night short, kept the coin.",
    "Not the target. Still the money.",
    "You outran your own plan.",
    "Paid out before the encore.",
    "The hand moved first; it was right.",
    "Enough is a position too.",
    "Booked it before the sky turned.",
    "You kept what the night offered.",
]
_LOSS_STOP = [
    "You were the liquidity.",
    "The stop did its job.",
    "Paid for the information.",
    "The night keeps its tax.",
    "Redacted by the tape.",
    "Wrong sky, right telescope.",
    "It was a decoy star.",
    "The level lied politely.",
    "Small wound, clean blade.",
    "The trap caught the trapper.",
    "A receipt for tuition.",
    "The beam tipped without us.",
    "Not every flare is a nova.",
    "First loss is the cheapest.",
    "The silence won this round.",
    "One plate spoiled by clouds.",
    "The structure broke its word.",
    "Bled by the plan, not by panic.",
    "The stop spoke. We obeyed.",
    "A false dawn, fully priced.",
    "The night audited us.",
    "It cost exactly what we agreed.",
    "The market kept the change.",
    "This one hunted back.",
    "Filed under: paid in full.",
    "Wrong about the turn, right about the size.",
]
_LOSS_MANUAL = [
    "Cut before the stop.",
    "You blinked first.",
    "Closed by hand, at a cost.",
    "Retreat, in good order.",
    "You left before the bill grew.",
    "Cut it while it was small.",
    "The exit was the victory.",
    "The lens fogged; we didn't linger.",
    "Closed the wound, kept the hand.",
    "You paid to stop watching.",
    "Called it early; the stop agreed later.",
    "A small no, before a large one.",
]
_FLAT = [
    "Nothing happened. Again.",
    "Inside the cost.",
    "A round trip, no distance.",
    "The night shrugged.",
    "Nothing moved but the clock.",
    "The scale refused to tip.",
    "Even. The rarest number.",
    "It breathed in; we stepped out.",
    "No blood, no trophy.",
    "The tape kept its secret.",
    "Costs covered, pride intact.",
    "A plate of empty sky.",
    "The market never showed its hand.",
    "Zero, earned honestly.",
    "The trap sprang on air.",
    "Some nights refuse to be read.",
    "Neither hunter nor prey tonight.",
    "The beam balanced. So be it.",
    "An honest scratch.",
    "Break-even: the night's polite refusal.",
]
VERDICTS = {
    ("win", "target"):  _WIN_TARGET,
    ("win", "manual"):  _WIN_MANUAL,
    ("loss", "stop"):   _LOSS_STOP,
    ("loss", "manual"): _LOSS_MANUAL,
    ("flat", "target"): _FLAT,
    ("flat", "stop"):   _FLAT,
    ("flat", "manual"): _FLAT,
}


def classify_exit(side, entry, exit_price, stop, target):
    """決済がどこで着いたかを実価格だけで分類する。"""
    direction = -1.0 if str(side).upper() in ("SHORT", "SELL") else 1.0
    if target is not None:
        try:
            if (float(exit_price) - float(target)) * direction >= -TICK:
                return "target"
        except (TypeError, ValueError):
            pass
    if stop is not None:
        try:
            if (float(exit_price) - float(stop)) * direction <= TICK:
                return "stop"
        except (TypeError, ValueError):
            pass
    return "manual"


def derive_state(side, entry, exit_price):
    """表示層と同じ判定。コスト帯の中は勝ちにしない。"""
    direction = -1.0 if str(side).upper() in ("SHORT", "SELL") else 1.0
    points = (float(exit_price) - float(entry)) * direction
    if abs(points) <= COST_FLOOR_PT:
        return "flat"
    return "win" if points > 0 else "loss"


def pick_verdict(result_id, state, exit_kind):
    """同じトレードなら常に同じ文言。連続するトレードでは変わる。

    先頭4バイトを整数にしてから剰余を取る(プール拡大後も偏りを出さない)。
    """
    options = VERDICTS.get((state, exit_kind)) or VERDICTS[(state, "manual")]
    digest = hashlib.sha256(result_id.encode("utf-8")).digest()
    return options[int.from_bytes(digest[:4], "big") % len(options)]


def normalize_legs(legs, side, entry, stop, qty, exit_price=None):
    """分割決済の片脚を正規化する。

    R12 の分割型は 1枚を TP1、1枚を runner として持つので、決済は
    **1つの価格では表せない**(TP1 と SL に半分ずつ当たる形が普通に起きる)。
    ``legs`` はその内訳で、各要素は ``{id, qty, exit, target?}``。

    ``kind``(target / stop / manual)は渡された値を信用せず、価格から
    ``classify_exit`` で導出する。数量の合計は ``qty`` に一致しなければ
    ならない。``exit_price`` を渡した場合は、数量加重平均が 1tick 以内で
    一致することも確かめる(表示の建値と内訳が食い違わないようにする)。

    戻り値は正規化済みの list。``legs`` が None なら None を返す。
    """
    if legs is None:
        return None
    if not isinstance(legs, (list, tuple)) or not legs:
        raise ValueError("legs は 1 件以上のリスト")

    out = []
    total_qty = 0
    weighted = 0.0
    for index, raw in enumerate(legs):
        if not isinstance(raw, dict):
            raise ValueError("legs の各要素は dict")
        leg_qty = int(raw.get("qty") or 0)
        if leg_qty < 1:
            raise ValueError("legs[].qty は 1 以上")
        if raw.get("exit") is None:
            raise ValueError("legs[].exit は必須(決済価格が無い脚は書けない)")
        leg_exit = float(raw["exit"])
        leg_target = float(raw["target"]) if raw.get("target") is not None else None
        out.append({
            "id": str(raw.get("id") or f"LEG{index + 1}"),
            "qty": leg_qty,
            "exit": leg_exit,
            "target": leg_target,
            "kind": classify_exit(side, entry, leg_exit, stop, leg_target),
        })
        total_qty += leg_qty
        weighted += leg_exit * leg_qty

    if total_qty != int(qty):
        raise ValueError(f"legs の数量合計 {total_qty} が qty {int(qty)} と一致しません")
    if exit_price is not None:
        blended = weighted / total_qty
        if abs(blended - float(exit_price)) > TICK:
            raise ValueError(
                f"legs の加重平均 {blended:.2f} が exit {float(exit_price):.2f} と一致しません")
    return out


def blended_exit(legs):
    """分割決済の数量加重平均。0.25 tick に丸める(発注系と同じ刻み)。"""
    total = sum(int(leg["qty"]) for leg in legs)
    weighted = sum(float(leg["exit"]) * int(leg["qty"]) for leg in legs)
    return round((weighted / total) / TICK) * TICK


def build_result(closed_position, exit_price, exit_source, bars=None,
                 symbol=None, point_value=2.0, mode="SIMULATION", receipt=None,
                 legs=None, fees=None, result_id=None,
                 scenario_id=None, account_id=None, model=None, grade=None):
    """決済結果を組み立てる。

    ``closed_position`` は Durable Object の ``closedPosition``。
    ``exit_price`` は **ブローカー由来かユーザーが明示した値のみ**。
    相場から推定した値を渡さないこと(exitSource がそれを拒む)。

    ``legs`` を渡すと分割決済の内訳を保存する(``normalize_legs`` 参照)。
    ``exit`` / ``pnl`` の正本はあくまで数量加重平均のままで、legs は
    「どこで半分ずつ当たったか」を失わないための追加情報。

    ``fees`` は **1口座分**の滑り+手数料($、正の値)。実約定価格を取得
    できないので、価格から出るのは粗損益でしかない。ブローカーの実現損益と
    の差はここに入れる —— 価格側へ按分すると tick に乗らない嘘の約定価格を
    作ることになる(TRADING_CONTEXT §2)。
    """
    if exit_source not in ("broker", "manual"):
        raise ValueError("exitSource must be 'broker' or 'manual'")

    entry = closed_position.get("avgEntry")
    if entry is None:
        raise ValueError("closedPosition.avgEntry がありません")
    stop = closed_position.get("stop")
    # R56: ブローカー約定から組んだ記録は、凍結プランの無い手動建玉だと stop を
    # 持たない。R を出せないだけで損益は事実なので、broker 由来に限り省略を許す。
    if stop is None and exit_source != "broker":
        raise ValueError("closedPosition.stop がありません(実現 R を出せません)")

    opened_at = closed_position.get("filledAt") or closed_position.get("observedAt")
    closed_at = closed_position.get("closedAt") or closed_position.get("observedAt")
    if not opened_at or not closed_at:
        raise ValueError("建玉時刻または決済時刻がありません")

    qty = int(closed_position.get("closedQty") or closed_position.get("initialQty") or 0)
    if qty < 1:
        raise ValueError("決済数量がありません")

    side = str(closed_position.get("side") or "").upper()
    if side not in ("LONG", "SHORT"):
        raise ValueError("closedPosition.side が不正です")

    path, path_source = build_path(bars, opened_at, closed_at, entry, exit_price)
    # resultId は素材のハッシュなので、価格や時刻を1つでも直すと別 ID になる。
    # DO の resultLog は resultId でしか重複排除しないため、訂正のつもりの
    # publish が **同じトレードの2行目**として積まれる(HANDOFF §2026-08-21 の
    # 「訂正 publish は二重掲載」)。訂正するときは元の ID を明示して置き換える。
    if result_id is not None:
        result_id = str(result_id)
        if not re.fullmatch(r"rs_[0-9a-f]{20}", result_id):
            raise ValueError("result_id は rs_ + 16進20桁")
    else:
        material = f"{opened_at}|{closed_at}|{entry}|{exit_price}|{side}|{qty}"
        result_id = "rs_" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:20]

    target = closed_position.get("target")
    state = derive_state(side, entry, exit_price)
    verdict = pick_verdict(result_id, state, classify_exit(side, entry, exit_price, stop, target))
    normalized_legs = normalize_legs(legs, side, float(entry),
                                     float(stop) if stop is not None else None, qty,
                                     exit_price=float(exit_price))

    payload = {
        "resultId": result_id,
        "side": side,
        "symbol": symbol or closed_position.get("symbol") or contract_month.symbol(),
        "qty": qty,
        "entry": float(entry),
        "exit": float(exit_price),
        "stop": float(stop) if stop is not None else None,
        "pointValue": float(point_value),
        "openedAt": str(opened_at),
        "closedAt": str(closed_at),
        "path": path,
        "pathSource": path_source,
        "exitSource": exit_source,
        "verdict": verdict,
        "mode": "LIVE" if str(mode).upper() == "LIVE" else "SIMULATION",
        "receipt": receipt or closed_position.get("receipt"),
    }
    if normalized_legs:
        payload["legs"] = normalized_legs
    if fees is not None:
        payload["fees"] = round(abs(float(fees)), 2)
    # ライフサイクル突合キー(任意)。どのシナリオ・どの口座の決済かを
    # 表示層が推定ではなくキーで辿れるようにする。表示専用 — entry claim や
    # 実行契約には使わない(Worker 側 validateResult も同じ扱い)。
    if scenario_id:
        payload["scenarioId"] = str(scenario_id)[:96]
    if account_id:
        payload["accountId"] = str(account_id)[:64]
    # R48: モデル別スコアカードの正本キー。凍結プランの model/grade を運ぶ。
    # 表示と集計専用 — Worker 側でも判定には使わない。
    if model:
        payload["model"] = str(model)[:32]
    if grade:
        payload["grade"] = str(grade)[:4]
    return payload


def publish_result(result, cfg=None):
    ok, detail = publish("result", {"result": result}, cfg)
    # R7: 日次ガードの台帳へ写す。**publish が成功したときだけ**書く
    # (失敗時に台帳だけ増えると二重記録になる)。CLI の --result と Bot の
    # /result は両方ここを通るので、この1箇所で両経路を捕捉できる。
    # dayguard が無い・書けない場合も決済経路は壊さない(fail-open)。
    if ok:
        try:
            import dayguard
            dayguard.record_from_result(result)
        except Exception:                        # noqa: BLE001
            pass
    return ok, detail


def result_from_report(side, entry, stop, qty, opened_at, closed_at,
                       exit_price=None, pnl=None, point_value=2.0,
                       target=None, symbol=None, bars=None, mode="SIMULATION",
                       legs=None, fees=None, reported_net=None, result_id=None,
                       exit_source="manual", scenario_id=None, account_id=None,
                       model=None, grade=None):
    """チャット/CLI で報告された勝敗を result に変換する。

    Bot の ``/result`` は Durable Object の ``closedPosition`` を前提にするが、
    建玉が DO に記録されないまま決済される経路(REST が FLAT を返し続ける、
    Mini App を経由しない手動決済など)では使えない。ここは報告された値から
    同じ payload を組み立てる経路で、既定の ``exitSource`` は ``manual``
    (人手申告)。

    ``exit_source="broker"`` を渡せるのは、決済の**事実**がブローカー由来の
    ときだけ —— ``trade_journal`` が建玉枚数の遷移と realizedPnL からこれを
    使う。相場から推定した価格にこの値を付けてはならない。

    ``exit_price`` と ``pnl`` はどちらか一方。``pnl`` を渡した場合は
    **実現損益から決済価格を逆算**する(相場からの推定ではなく、報告された
    確定値の換算)。逆算した事実は戻り値の第2要素に載せて隠さない。

    ``legs`` を渡した場合は、**そちらが正本**になる(分割決済は 1 つの
    決済価格では表せない)。``exit_price`` / ``pnl`` は省略でき、決済価格は
    脚の数量加重平均から出す。両方渡した場合は 1tick 以内で一致を検査する。
    """
    if legs is None and (exit_price is None) == (pnl is None):
        raise ValueError("exit_price か pnl のどちらか一方を指定してください")
    if legs is not None and pnl is not None:
        raise ValueError("legs と pnl は同時に指定できません(内訳が正本)")

    side = str(side).upper()
    if side in ("BUY", "LONG"):
        side = "LONG"
    elif side in ("SELL", "SHORT"):
        side = "SHORT"
    else:
        raise ValueError("side は buy/sell(long/short)のいずれか")

    entry = float(entry)
    if stop is None:
        # R56: 凍結プランの無い手動建玉(ブローカー約定由来)だけ stop を省略できる。
        if exit_source != "broker":
            raise ValueError("stop は必須(ブローカー約定由来の記録だけが省略できる)")
    else:
        stop = float(stop)
    qty = int(qty)
    if qty < 1:
        raise ValueError("qty は 1 以上")
    point_value = float(point_value)

    derivation = None
    if legs is not None:
        # 内訳が正本。決済価格は数量加重平均で、ここでは発明しない。
        checked = normalize_legs(legs, side, entry, stop, qty, exit_price=exit_price)
        exit_price = blended_exit(checked)
        legs = checked
        breakdown = " / ".join(
            f"{leg['id']} {leg['qty']}枚 @{leg['exit']:.2f}({leg['kind']})" for leg in checked)
        derivation = f"exit を分割決済の数量加重平均から算出: {breakdown} → {exit_price:.2f}"
    elif exit_price is None:
        # pnl は「その口座の実現損益($)」。符号はどちらでも受ける。
        pnl = float(pnl)
        points = abs(pnl) / (point_value * qty)
        direction = 1 if pnl >= 0 else -1
        if side == "LONG":
            exit_price = entry + direction * points
        else:
            exit_price = entry - direction * points
        # 0.25 tick に丸める(発注系と同じ刻み)。
        exit_price = round(exit_price / 0.25) * 0.25
        derivation = (f"exit を実現損益から逆算: {pnl:+.2f} USD ÷ "
                      f"({point_value:.2f}/pt × {qty}) = {points:.2f}pt → {exit_price:.2f}")
    else:
        exit_price = float(exit_price)

    closed = {
        "avgEntry": entry,
        "stop": stop,
        "target": float(target) if target is not None else None,
        "side": side,
        "closedQty": qty,
        "filledAt": str(opened_at),
        "closedAt": str(closed_at),
        "symbol": symbol,
    }
    if reported_net is not None:
        if fees is not None:
            raise ValueError("fees と reported_net は同時に指定できません")
        # 粗損益(価格由来)と実現損益の差をそのまま fees に入れる。差の内訳
        # (滑り / 手数料)は取得経路が無いので分解しない。
        direction = 1.0 if side == "LONG" else -1.0
        gross = (exit_price - entry) * direction * point_value * qty
        fees = gross - float(reported_net)
        if fees < -1e-9:
            raise ValueError(
                f"reported_net {float(reported_net):+.2f} が粗損益 {gross:+.2f} を上回っています")
        derivation = ((derivation + " / ") if derivation else "") + (
            f"fees を粗損益との差から算出: {gross:+.2f} − {float(reported_net):+.2f} = {fees:.2f}")

    result = build_result(closed, exit_price, exit_source, bars=bars,
                          symbol=symbol, point_value=point_value, mode=mode,
                          legs=legs, fees=fees, result_id=result_id,
                          scenario_id=scenario_id, account_id=account_id,
                          model=model, grade=grade)
    return result, derivation


def clear_result(cfg=None):
    return publish("result", {"result": None}, cfg)


def sync_position(cfg=None, symbol=None):
    """ブローカーに照会して結果をそのまま publish する。

    照会できなかった場合も **必ず publish する**。そうしないと Mini App が
    「最後に見えた状態」を verified のまま表示し続けてしまう。

    戻り値 ``(ok, detail)``。detail には DO が返した transitions が入るので、
    呼び出し側は建玉が CLOSED に落ちた瞬間を検知できる。
    """
    import broker_status  # 循環 import を避けるため遅延

    cfg = cfg or load_cloud_env()
    symbol = symbol or cfg.get("NQX_SYMBOL", contract_month.symbol())
    result = broker_status.query_position(symbol)
    # ブローカーは SL/TP を返さない。凍結プランの水準を重ねて publish する
    # (無ければ素通し。position_plan_overlay の docstring 参照)。
    overlay = position_plan_overlay(result)
    if overlay:
        result = dict(result, **overlay)
    closed_at = result.pop("closedAt", None)
    # 決済価格は position スキーマに載せない(建玉の話ではないため)。
    # 呼び出し側が result ストリームで使えるよう detail に添えて返す。
    realised = {key: result.pop(key) for key in ("avgExit", "avgBuy", "avgSell")
                if key in result}
    ok, detail = publish_position(result, closed_at=closed_at, cfg=cfg)
    if isinstance(detail, dict):
        detail = dict(detail, realised=realised)
    return ok, detail


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Cloudflare state publisher")
    parser.add_argument("--check", action="store_true", help="設定と疎通を確認する")
    parser.add_argument("--sync-position", action="store_true", help="ブローカー照会結果を publish する")
    parser.add_argument("--clear-scenario", metavar="STATE", nargs="?", const="CANCELED",
                        help="表示中のシナリオを降ろす(既定 CANCELED)")
    parser.add_argument("--cancel-order", action="store_true",
                        help="残存する PENDING/SENT/PARTIAL/UNKNOWN の注文を CANCELED にする"
                             "(決済後の ORDER_PENDING 恒久ブロックの手動解除)")
    parser.add_argument("--launch-token", metavar="USER_ID", help="launch token を発行して表示する")
    parser.add_argument("--publish-accounts", action="store_true",
                        help="crosstrade.env の LIFELINE_* から口座残機を publish する")
    parser.add_argument("--result", action="store_true",
                        help="報告された勝敗を LEDGER に記録する(人手申告・exitSource=manual)")
    parser.add_argument("--side", help="--result: buy/sell")
    parser.add_argument("--entry", type=float, help="--result: 約定価格")
    parser.add_argument("--stop", type=float, help="--result: 初期SL(実現Rの分母)")
    parser.add_argument("--target", type=float, help="--result: TP(任意)")
    parser.add_argument("--qty", type=int, default=1, help="--result: 枚数(既定1)")
    parser.add_argument("--exit", dest="exit_price", type=float,
                        help="--result: 決済価格。--pnl と排他")
    parser.add_argument("--pnl", type=float,
                        help="--result: 実現損益$(1口座分)。決済価格を逆算する。--exit と排他")
    parser.add_argument("--opened-at", help="--result: 建玉時刻 ISO(TZ付き)")
    parser.add_argument("--closed-at", help="--result: 決済時刻 ISO(TZ付き)")
    parser.add_argument("--dry-run", action="store_true", help="--result: publish せず payload を表示")
    args = parser.parse_args()

    if args.result:
        missing = [n for n, v in (("--side", args.side), ("--entry", args.entry),
                                  ("--stop", args.stop), ("--opened-at", args.opened_at),
                                  ("--closed-at", args.closed_at)) if v is None]
        if missing:
            print("不足: " + " ".join(missing))
            return 2
        try:
            result, derivation = result_from_report(
                args.side, args.entry, args.stop, args.qty,
                args.opened_at, args.closed_at,
                exit_price=args.exit_price, pnl=args.pnl,
                target=args.target)
        except ValueError as exc:
            print(f"ERROR {exc}")
            return 2
        if derivation:
            print(derivation)
        gross = (result["exit"] - result["entry"]) * result["qty"] * result["pointValue"]
        if result["side"] == "SHORT":
            gross = -gross
        print(f"{result['side']} {result['qty']}枚 {result['entry']:.2f} → "
              f"{result['exit']:.2f}  損益 {gross:+.2f} USD/口座  verdict: {result['verdict']}")
        if args.dry_run:
            print(json.dumps(result, ensure_ascii=False, indent=1))
            return 0
        ok, detail = publish_result(result)
        print(("OK " if ok else "FAILED ") + json.dumps(detail, ensure_ascii=False)[:300])
        return 0 if ok else 1

    if args.check:
        if not is_configured():
            print(f"未設定: {CLOUD_ENV}")
            print("Cloudflare 連携は無効です(既存の Telegram 経路は動きます)")
            return 1
        cfg = load_cloud_env()
        print(f"api      : {cfg['NQX_API_BASE']}")
        print(f"account  : {cfg['NQX_ACCOUNT_ID']}")
        print(f"symbol   : {cfg['NQX_SYMBOL']}")
        print(f"revisions: {_load_revisions()}")
        # User-Agent を必ず付ける。既定の "Python-urllib/3.x" は Cloudflare の
        # bot 判定に 403 で弾かれ、**API は正常なのに起動チェックだけが
        # 「到達できません」と出る**。publish / state 読みは最初から独自 UA を
        # 付けていたので、この1行が欠けている --check だけが嘘をついていた。
        try:
            request = urllib.request.Request(
                cfg["NQX_API_BASE"].rstrip("/") + "/api/health",
                headers={"User-Agent": "NQX-Nightwatch-State/1.0"}, method="GET")
            with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as r:
                print("health   : " + r.read().decode("utf-8", "replace"))
        except Exception as exc:
            print(f"health   : 到達できません({exc})")
            return 1
        return 0

    if args.launch_token:
        print(issue_launch_token(args.launch_token))
        return 0

    if args.sync_position:
        ok, detail = sync_position()
        print(("OK " if ok else "FAILED ") + json.dumps(detail, ensure_ascii=False)[:400])
        return 0 if ok else 1

    if args.publish_accounts:
        ok, detail = publish_accounts()
        print(("OK " if ok else "FAILED ") + json.dumps(detail, ensure_ascii=False)[:400])
        return 0 if ok else 1

    if args.cancel_order:
        ok, detail = resolve_stuck_order("CANCELED", detail="manual cancel via nqx_state CLI")
        if not ok and isinstance(detail, dict) and detail.get("skipped"):
            # 触るべき注文が無いのは失敗ではない(目的の状態は既に成立している)。
            print("NOOP " + json.dumps(detail, ensure_ascii=False)[:400])
            return 0
        print(("OK " if ok else "FAILED ") + json.dumps(detail, ensure_ascii=False)[:400])
        return 0 if ok else 1

    if args.clear_scenario:
        ok, detail = clear_scenario(args.clear_scenario)
        print(("OK " if ok else "FAILED ") + json.dumps(detail, ensure_ascii=False)[:400])
        return 0 if ok else 1

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())

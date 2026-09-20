# -*- coding: utf-8 -*-
"""R78: 約定監視(fill watcher)。3 分ループとは別プロセスで建玉の枚数変化を数秒で検知し、
建玉管理(``autotrade_engine.reconcile``)を即時に起動する。

なぜ要るか
----------
2026-09-11 01:34〜01:38 に TP1 が約定し、3 分ループが 01:40:48 に runner の stop を置いた
ときには価格が安値から 28pt 以上戻っていて、BUY STOP 29,174.25 は「価格がすでに上」で
拒否された。``cancelandbracket`` は取消→新規の 2 段なので runner は保護注文ゼロで残った。
検知が数秒なら stop は安値付近で置けた。3 分の検知遅延そのものが原因である。

このプロセスがすること / しないこと
----------------------------------
* 発注コマンドを直接叩かない。engine の ``reconcile`` を呼ぶだけ(全ゲートはその中)。
* 建玉の枚数/方向が変わった瞬間(4→2、2→0、0→4)だけ動く。定期的な再評価はしない
  (``--manage-every`` で明示したときだけ、建玉がある間の再評価を N 秒ごとに行う)。
* bundle に scenario を載せない。**新規 ENTRY はこのプロセスから絶対に出ない**
  (``_published_scenario`` を空にして渡す。engine は提案が無ければ ENTRY 経路で何もしない)。
* 価格は TradingView の quote を取り直して bundle に載せる(周期の古い価格を使わない)。
  足は最後の検証済み bundle(最大 3 分古い)を使い、現在値は engine が極値候補に含める。
* Worker の建玉 stream を reconcile の前に同期する(MANAGEMENT claim が建玉枚数を照合するため)。
* 3 分ループと同じ reconcile ロック(``autotrade_engine._ReconcileLock``)で排他する。
* heartbeat を ``.secrets/fill_watch_heartbeat.json`` に書き、``nqx_cycle`` が生死を 1 行出す。

R91: 常駐を保証し、上限の中で速く見る(2026-09-15)
--------------------------------------------------
R78 の導入後、このプロセスは一度も常駐していなかった(heartbeat は 09-11 02:20 の 1 回だけ)。
TP1 検知は実際には 3 分遅れのままだった。

* **本当のプッシュは使えない。** CrossTrade の WebSocket は NinjaTrader 8 口座だけで、
  Tradovate 口座は REST のみ・約定のプッシュ通知も無い。だから「ブローカー照会の間隔」を
  上限の中で詰めるのが、この経路で取れる最速である。
* **適応間隔。** 建玉がある間は ``fastIntervalSec``(既定 1.5 秒)、FLAT は
  ``idleIntervalSec``(既定 5 秒)。TP1 / SL の約定は建玉がある間にしか起きない。
* **自前の予算。** CrossTrade の上限は利用者ごと毎秒 3 回・溜め 20 回(REST と WebSocket で共有、
  超えると 429)。3 分ループの照会と送信後照会を 429 で落とさないため、このプロセスは
  ``maxRequestsPerSec``(既定 1.0)以下に抑える。FLAT の照会は裏取りで 2 本打つ
  (broker_status R41)ので、1 回の見回りは「建玉あり 1 本 / FLAT・未検証 2 本」で数える。
* **429 は待つ。** 照会結果に HTTP 429 が見えたら ``rateLimitBackoffSec``(既定 15 秒)以上空ける。
* **ループの reconcile 中は照会しない。** ロックを待たずに覗くだけ(取れたら即返す)。
  ループの送信と送信後照会が走っている間に予算を食わないため。
* **単一インスタンス。** ``.secrets/fill_watch.lock`` を生存中ずっと握る。2 つ目は起動しない。
* **自動起動。** 契約 ``fillWatch.autostart=true`` のとき、``nqx_cycle`` が毎周期
  ``supervise()`` で heartbeat を見て、止まっていれば切り離したプロセスとして起動し直す
  (出力は ``.secrets/fill_watch.log``)。起動しても heartbeat が出ない状態が 3 回続いたら
  1 時間は起動を止めてログを見るよう注記する。

起動
----
    python fill_watch.py            # 監視セッション中は常駐。Ctrl+C で停止
    python fill_watch.py --once     # 1 回だけ観測して heartbeat と比較(スケジューラ用)
"""
from __future__ import annotations

import argparse
import json
import math
import os
import contract as contract_month  # R102: 取引限月の正本
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

BASE = Path(__file__).resolve().parent
HEARTBEAT_PATH = BASE / ".secrets" / "fill_watch_heartbeat.json"
#: R91: 単一インスタンスのロック・自動起動の記録・切り離したプロセスの出力。
INSTANCE_LOCK_PATH = BASE / ".secrets" / "fill_watch.lock"
SPAWN_STATE_PATH = BASE / ".secrets" / "fill_watch_spawn.json"
LOG_PATH = BASE / ".secrets" / "fill_watch.log"
CONTRACT_PATH = BASE / "execution_contract.json"
#: ``autotrade_engine.LEDGER_FILE + ".lock"`` と同じ場所(ループの reconcile ロック)。
RECONCILE_LOCK_PATH = BASE / ".secrets" / "autotrade_ledger.jsonl.lock"
#: 足の出所。検証済み bundle を優先し、無ければ取得直後の bundle。
BUNDLE_CANDIDATES = (BASE / ".secrets" / "monitor_pipeline_bundle.json",
                     BASE / ".secrets" / "tv_bundle.json")
DEFAULT_INTERVAL_SEC = 5.0
MAX_BACKOFF_SEC = 60.0
VERSION = "R91-FILL-WATCH-1"
#: 契約 ``fillWatch`` の既定。節が無い・壊れているときもこの値(autostart は False)。
DEFAULT_SETTINGS: Dict[str, Any] = {"version": VERSION, "autostart": False, "fastIntervalSec": 1.5,
                                    "idleIntervalSec": DEFAULT_INTERVAL_SEC, "maxRequestsPerSec": 1.0,
                                    "rateLimitBackoffSec": 15.0}
#: 数値の許容範囲。範囲外はその項目だけ既定へ戻す。maxRequestsPerSec は CrossTrade の
#: 毎秒 3 回より必ず低く(ループの照会と送信後照会の分を残す)。
SETTING_BOUNDS = {"fastIntervalSec": (0.5, 60.0), "idleIntervalSec": (1.0, 300.0),
                  "maxRequestsPerSec": (0.1, 2.5), "rateLimitBackoffSec": (5.0, 300.0)}
#: ループの reconcile 中に照会を止めている間、ロックを覗き直す間隔。
PAUSE_POLL_SEC = 1.0
#: 起動しても heartbeat が出ない回数がこれに達したら、自動起動を一旦止める。
SPAWN_SUSPEND_AFTER = 3
SPAWN_RETRY_AFTER_SEC = 3600
LOG_ROTATE_BYTES = 5_000_000
#: reconcile の注記にこれが含まれたら、同じ変化に対してもう一度だけ reconcile を回す。
#: 約定直後の周期は所有権を束縛して「management armed next cycle」で終わるため、
#: 3 分ループでは管理(建値移動)まで **さらに 3 分** かかっていた。
REARM_MARKERS = ("management armed next cycle",)
RATE_LIMIT_RE = re.compile(r"\b429\b|rate[_ ]?limit", re.I)

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def configured_accounts(cfg: Optional[Dict[str, str]] = None) -> List[str]:
    import autotrade_engine as ae
    merged = dict(ae._read_env_file(), **(cfg or {}))
    return list(ae._multi_scope(merged))


# ---------------------------------------------------------------- R91: 設定

def load_settings(contract: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """契約 ``fillWatch`` を検証して返す。読めない・壊れている項目は既定値。

    ``autostart`` は JSON の ``true`` のときだけ有効(文字列 "true" や 1 では起動しない)。
    ``invalid`` に既定へ戻した項目名を残す(本番契約に無いことはテストが見る)。
    """
    out = dict(DEFAULT_SETTINGS)
    out["invalid"] = []
    if contract is None:
        try:
            contract = json.loads(Path(CONTRACT_PATH).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            out["invalid"].append("CONTRACT_UNREADABLE")
            return out
    raw = contract.get("fillWatch") if isinstance(contract, dict) else None
    if raw is None:
        return out
    if not isinstance(raw, dict):
        out["invalid"].append("FILL_WATCH_MALFORMED")
        return out
    if "autostart" in raw:
        if isinstance(raw["autostart"], bool):
            out["autostart"] = raw["autostart"]
        else:
            out["invalid"].append("autostart")
    for key, (low, high) in SETTING_BOUNDS.items():
        if key not in raw:
            continue
        value = raw[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) \
                or not math.isfinite(float(value)) or not low <= float(value) <= high:
            out["invalid"].append(key)
            continue
        out[key] = float(value)
    if raw.get("version"):
        out["version"] = str(raw["version"])
    return out


def legacy_settings(interval: float) -> Dict[str, Any]:
    """R91 以前の固定間隔と同じ振る舞い(注入テスト・明示の ``interval`` 用)。"""
    return {**DEFAULT_SETTINGS, "fastIntervalSec": float(interval), "idleIntervalSec": float(interval),
            "maxRequestsPerSec": 1e9, "invalid": []}


def sweep_cost(observation: Dict[str, Dict[str, Any]]) -> int:
    """1 回の見回りで打つ HTTP の本数の見積もり。建玉あり 1 本、FLAT・未検証は 2 本。

    FLAT は broker_status R41 の裏取り(``/positions`` 一覧)で 2 本目が走る。未検証は最悪側。

    R118: Gateway(押し込み)から来た行は **0 本**。押し込みは上限の枠を消費しない
    ので、口座が増えても見回りの間隔が伸びない。REST へ落ちた口座だけが従来どおり
    数えられる —— 混在しても正しく効く。**全口座が Gateway 由来なら床は 0** で、
    `next_delay` は `fastIntervalSec` そのものになる。
    """
    cost = 0
    for value in (observation or {}).values():
        if str(value.get("source") or "") in PUSH_SOURCES:
            continue
        is_open = value.get("verified") and int(value.get("qty") or 0) > 0
        cost += 1 if is_open else 2
    return cost


def next_delay(settings: Dict[str, Any], observation: Dict[str, Dict[str, Any]],
               errors: int = 0, rate_limited: bool = False) -> Tuple[float, str]:
    """次の照会までの秒数とモード(FAST / IDLE / BACKOFF)。純粋関数。"""
    open_now = any(v.get("verified") and int(v.get("qty") or 0) > 0 for v in (observation or {}).values())
    base = float(settings["fastIntervalSec"] if open_now else settings["idleIntervalSec"])
    delay = max(base, sweep_cost(observation) / float(settings["maxRequestsPerSec"]))
    mode = "FAST" if open_now else "IDLE"
    if errors:
        delay = min(MAX_BACKOFF_SEC, max(delay, float(settings["idleIntervalSec"])) * (2 ** min(errors, 4)))
        mode = "BACKOFF"
    if rate_limited:
        delay = max(delay, float(settings["rateLimitBackoffSec"]))
        mode = "BACKOFF"
    return round(delay, 3), mode


# ---------------------------------------------------------------- R91: ロック

class FileLock:
    """プロセス間の非ブロッキング排他(``autotrade_engine._ReconcileLock`` と同じ方式)。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.handle = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.handle = open(self.path, "a+b")
            if os.name == "nt":
                import msvcrt
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            if self.handle is not None:
                try:
                    self.handle.close()
                except OSError:
                    pass
            self.handle = None
            return False

    def release(self) -> None:
        if self.handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            try:
                self.handle.close()
            except OSError:
                pass
            self.handle = None


def lock_held(path: Path) -> bool:
    """他のプロセスがロックを握っているか。**待たない**(取れたら即返す)。"""
    probe = FileLock(path)
    if probe.acquire():
        probe.release()
        return False
    return True


# ---------------------------------------------------------------- 観測と起動

def observe(symbol: str, accounts: List[str],
            position_query: Callable[..., Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """口座ごとの ``{verified, qty, side}``。照会失敗は verified=False(変化とみなさない)。"""
    out: Dict[str, Dict[str, Any]] = {}
    for account in accounts:
        try:
            position = position_query(symbol, account=account)
        except Exception as exc:  # noqa: BLE001 - 照会失敗は観測なし
            out[account] = {"verified": False, "error": f"{type(exc).__name__}: {exc}"}
            continue
        if not isinstance(position, dict) or position.get("verified") is not True:
            detail = str((position or {}).get("detail") or "") if isinstance(position, dict) else ""
            out[account] = {"verified": False, **({"error": detail[:200]} if detail else {})}
            continue
        try:
            qty = int(position.get("qty") or 0)
        except (TypeError, ValueError):
            qty = 0
        side = str(position.get("side") or "").upper() if qty > 0 else ""
        # R118: どこから読んだか。Gateway 由来の行は HTTP を使っていないので、
        # 次の見回りの間隔(`sweep_cost`)に数えない。
        out[account] = {"verified": True, "qty": qty, "side": side,
                        "source": str(position.get("source") or "")}
    return out


#: R118: この出所で来た観測は HTTP を使っていない(WebSocket の押し込み)。
PUSH_SOURCES = ("gateway-ws",)


def rate_limited(observation: Dict[str, Dict[str, Any]]) -> bool:
    """照会の失敗理由に CrossTrade / Tradovate の 429 が見えるか。"""
    return any(not v.get("verified") and RATE_LIMIT_RE.search(str(v.get("error") or ""))
               for v in (observation or {}).values())


def changes(previous: Optional[Dict[str, Dict[str, Any]]],
            current: Dict[str, Dict[str, Any]]) -> List[str]:
    """verified 同士で qty/side が変わった口座の説明。未検証の観測は比較に使わない。"""
    notes: List[str] = []
    for account, cur in current.items():
        prev = (previous or {}).get(account)
        if not cur.get("verified") or not prev or not prev.get("verified"):
            continue
        if (prev.get("qty"), prev.get("side")) != (cur.get("qty"), cur.get("side")):
            notes.append(f"{account}: {prev.get('side') or 'FLAT'} {prev.get('qty')} -> "
                         f"{cur.get('side') or 'FLAT'} {cur.get('qty')}")
    return notes


def merge_baseline(previous: Optional[Dict[str, Dict[str, Any]]],
                   current: Dict[str, Dict[str, Any]],
                   accounts: Optional[List[str]] = None) -> Dict[str, Dict[str, Any]]:
    """次回比較の基準。未検証だった口座は前回の verified 観測を保つ。

    R118: `source` も持ち越す。heartbeat に載るのはこの基準なので、落とすと
    **どの出所で観測しているかが外から一切見えない**(2026-09-20、mode=LIVE に
    したのに heartbeat の出所が空のままで、効いていないように見えた)。
    `accounts` を渡すと、**監視対象から外れた口座の行を落とす** —— 口座を入れ替えた
    あと、消えた口座が基準に残り続けて heartbeat の口座数が合わなくなる。
    """
    baseline = dict(previous or {})
    for account, cur in current.items():
        if cur.get("verified"):
            baseline[account] = {"verified": True, "qty": cur.get("qty"),
                                 "side": cur.get("side"), "source": cur.get("source") or ""}
    if accounts is not None:
        keep = {str(value) for value in accounts}
        baseline = {name: row for name, row in baseline.items() if name in keep}
    return baseline


def load_bundle(price: Optional[float], symbol: str,
                candidates=BUNDLE_CANDIDATES) -> Dict[str, Any]:
    """最後の bundle の足を土台に、現在値だけを差し替えた管理専用 bundle。

    scenario は**必ず空**にする。engine の ENTRY 経路は提案が無ければ何も送らない。
    """
    bundle: Dict[str, Any] = {}
    for path in candidates:
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and (data.get("snapshot") or {}).get("bars3m"):
            bundle = {"snapshot": {"bars3m": data["snapshot"]["bars3m"]},
                      "sourceSymbol": data.get("sourceSymbol") or symbol,
                      "cvdAt": data.get("cvdAt"), "priceAt": data.get("priceAt")}
            break
    if not bundle:
        bundle = {"snapshot": {"bars3m": []}, "sourceSymbol": symbol}
    if price is not None:
        bundle["price"] = float(price)
        bundle["priceSource"] = "FRESH_QUOTE"
        bundle["priceAt"] = _iso_now()
    bundle["at"] = _iso_now()
    bundle["_published_scenario"] = {}
    bundle["scenarios"] = {}
    bundle["_fillWatch"] = True
    return bundle


def trigger(reason: str, symbol: str, *,
            price_query: Optional[Callable[[str], Optional[float]]],
            reconcile: Callable[..., List[str]],
            sync_position: Optional[Callable[[], Any]],
            candidates=BUNDLE_CANDIDATES) -> List[str]:
    """枚数変化 1 回ぶんの処理。reconcile は最大 2 回(束縛 → 管理)。"""
    notes: List[str] = [f"trigger: {reason}"]
    if sync_position is not None:
        try:
            ok, detail = sync_position()
            if not ok:
                notes.append(f"position sync failed: {detail}")
        except Exception as exc:  # noqa: BLE001
            notes.append(f"position sync failed ({type(exc).__name__}: {exc})")
    for attempt in range(2):
        price = None
        if price_query is not None:
            try:
                price = price_query(symbol)
            except Exception:  # noqa: BLE001 - quote が無ければ engine が自前で取る
                price = None
        bundle = load_bundle(price, symbol, candidates)
        fresh = (lambda _symbol, value=price: value) if price is not None else None
        result = list(reconcile(bundle, state_ok=True, fresh_price_query=fresh))
        notes.extend(result)
        if not any(marker in note for note in result for marker in REARM_MARKERS):
            break
        notes.append("rearm: ownership bound; running management now instead of next cycle")
    return notes


def load_heartbeat(path: Path = HEARTBEAT_PATH) -> Dict[str, Any]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_heartbeat(payload: Dict[str, Any], path: Path = HEARTBEAT_PATH) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def status_line(now: Optional[datetime] = None, path: Path = HEARTBEAT_PATH) -> str:
    """nqx_cycle が毎周期 1 行出す生死表示。"""
    hb = load_heartbeat(path)
    if not hb:
        return "fill_watch: 未起動（python fill_watch.py を別プロセスで常駐させる）"
    try:
        at = datetime.fromisoformat(str(hb.get("at")))
    except (TypeError, ValueError):
        return "fill_watch: heartbeat が読めない"
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    age = (current - at).total_seconds()
    interval = float(hb.get("intervalSec") or DEFAULT_INTERVAL_SEC)
    expected = float(hb.get("nextDelaySec") or interval)
    if age > max(30.0, interval * 6, expected * 6):
        return f"fill_watch: STALE（最終 heartbeat {age:.0f}s 前）"
    last = hb.get("lastTrigger") or {}
    tail = f" 最終起動 {last.get('at')} {last.get('reason')}" if last else ""
    mode = hb.get("mode")
    pace = f"{mode} {expected:g}s" if mode else f"{interval:g}s 間隔"
    extra = f" 429×{hb['rateLimitedCount']}" if hb.get("rateLimitedCount") else ""
    return f"fill_watch: alive（{age:.0f}s 前, {pace}{extra}）{tail}"


def run(*, symbol: str, accounts: List[str], interval: float,
        position_query: Callable[..., Dict[str, Any]],
        reconcile: Callable[..., List[str]],
        price_query: Optional[Callable[[str], Optional[float]]] = None,
        sync_position: Optional[Callable[[], Any]] = None,
        heartbeat_path: Path = HEARTBEAT_PATH,
        bundle_candidates=BUNDLE_CANDIDATES,
        manage_every: float = 0.0,
        once: bool = False,
        resume: bool = True,
        max_iterations: Optional[int] = None,
        sleep: Callable[[float], None] = time.sleep,
        log: Callable[[str], None] = print,
        settings: Optional[Dict[str, Any]] = None,
        lock_busy: Optional[Callable[[], bool]] = None,
        started_by: str = "manual") -> int:
    """監視ループ本体。注入可能にしてテストから外部接続なしで回す。

    ``settings`` を渡さなければ R91 以前と同じ固定間隔(``interval``)で回る。
    ``lock_busy`` が True を返す間(ループの reconcile 中)はブローカーを照会しない。
    """
    pace = settings if settings is not None else legacy_settings(interval)
    previous_hb = load_heartbeat(heartbeat_path) if resume else {}
    baseline: Optional[Dict[str, Dict[str, Any]]] = previous_hb.get("lastObservation") if resume else None
    last_trigger: Optional[Dict[str, Any]] = previous_hb.get("lastTrigger") if resume else None
    last_manage_at = time.monotonic()
    started_at = _iso_now()
    errors = 0
    iterations = 0
    limited_count = 0
    paused_count = 0
    while True:
        iterations += 1
        if lock_busy is not None:
            try:
                busy = bool(lock_busy())
            except Exception:  # noqa: BLE001 - 覗けなければ止めずに照会する
                busy = False
            if busy:
                paused_count += 1
                write_heartbeat({"schema": "NQX_FILL_WATCH/1", "version": VERSION, "at": _iso_now(),
                                 "pid": os.getpid(), "symbol": symbol, "intervalSec": pace["idleIntervalSec"],
                                 "nextDelaySec": PAUSE_POLL_SEC, "mode": "PAUSED_LOOP_RECONCILE",
                                 "startedAt": started_at, "startedBy": started_by,
                                 "lastObservation": baseline, "consecutiveErrors": errors,
                                 "rateLimitedCount": limited_count, "pausedCount": paused_count,
                                 "lastTrigger": last_trigger}, heartbeat_path)
                if once or (max_iterations is not None and iterations >= max_iterations):
                    return 0
                sleep(PAUSE_POLL_SEC)
                continue
        poll_started = time.monotonic()
        current = observe(symbol, accounts, position_query)
        poll_ms = round((time.monotonic() - poll_started) * 1000)
        unverified = [a for a, v in current.items() if not v.get("verified")]
        limited = rate_limited(current)
        limited_count += 1 if limited else 0
        diff = changes(baseline, current) if baseline is not None else []
        if baseline is None:
            log(f"[{datetime.now():%H:%M:%S}] baseline: " + ", ".join(
                f"{a}={v.get('side') or 'FLAT'} {v.get('qty')}" if v.get("verified") else f"{a}=UNVERIFIED"
                for a, v in current.items()))
        open_now = any(v.get("verified") and int(v.get("qty") or 0) > 0 for v in current.values())
        periodic = (manage_every > 0 and open_now
                    and time.monotonic() - last_manage_at >= manage_every)
        reason = "; ".join(diff) if diff else ("periodic management" if periodic else "")
        if reason:
            log(f"[{datetime.now():%H:%M:%S}] {reason}")
            notes = trigger(reason, symbol, price_query=price_query, reconcile=reconcile,
                            sync_position=sync_position, candidates=bundle_candidates)
            for note in notes:
                log(f"  - {note}")
            last_trigger = {"at": _iso_now(), "reason": reason, "notes": notes[-8:]}
            last_manage_at = time.monotonic()
        baseline = merge_baseline(baseline, current, accounts)
        errors = errors + 1 if unverified and len(unverified) == len(current) else 0
        # 次の見回りの本数は「今の」建玉で決まる(未検証の口座は最悪側の 2 本)。
        delay, mode = next_delay(pace, {a: current.get(a) or {} for a in accounts}, errors, limited)
        if limited:
            log(f"[{datetime.now():%H:%M:%S}] rate limited (429): waiting {delay:g}s")
        write_heartbeat({"schema": "NQX_FILL_WATCH/1", "version": VERSION, "at": _iso_now(),
                         "pid": os.getpid(), "symbol": symbol, "intervalSec": pace["idleIntervalSec"],
                         "nextDelaySec": delay, "mode": mode, "pollMs": poll_ms,
                         "startedAt": started_at, "startedBy": started_by,
                         "lastObservation": baseline, "unverified": unverified,
                         "consecutiveErrors": errors, "rateLimitedCount": limited_count,
                         "pausedCount": paused_count, "lastTrigger": last_trigger},
                        heartbeat_path)
        if once or (max_iterations is not None and iterations >= max_iterations):
            return 0
        sleep(delay)


# ---------------------------------------------------------------- R91: 自動起動(nqx_cycle から)

def _read_json(path: Path) -> Dict[str, Any]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def spawn_detached(log_path: Path = LOG_PATH, script: Path = BASE / "fill_watch.py") -> int:
    """fill_watch を親(周期のプロセス)から切り離して起動し、pid を返す。

    Windows は DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP(ジョブから抜けられるなら抜ける)。
    出力はログへ追記し、5MB を超えたら 1 世代だけ回す。
    """
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if log_path.exists() and log_path.stat().st_size > LOG_ROTATE_BYTES:
            os.replace(log_path, log_path.with_suffix(log_path.suffix + ".1"))
    except OSError:
        pass
    # ログを即時に書く(ブロックバッファのままだと約定時の注記が 8KB たまるまで見えない)。
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1", PYTHONUNBUFFERED="1")
    command = [sys.executable, str(script), "--started-by", "nqx_cycle"]
    with open(log_path, "ab") as out:
        out.write(f"\n=== {_iso_now()} autostart by nqx_cycle ===\n".encode("utf-8"))
        out.flush()
        kwargs: Dict[str, Any] = dict(cwd=str(Path(script).parent), env=env, stdin=subprocess.DEVNULL,
                                      stdout=out, stderr=subprocess.STDOUT, close_fds=True)
        if os.name == "nt":
            detached, new_group, breakaway = 0x00000008, 0x00000200, 0x01000000
            try:
                return subprocess.Popen(command, creationflags=detached | new_group | breakaway, **kwargs).pid
            except OSError:
                # ジョブが breakaway を許さない環境。切り離しだけで起動する。
                return subprocess.Popen(command, creationflags=detached | new_group, **kwargs).pid
        return subprocess.Popen(command, start_new_session=True, **kwargs).pid


def supervise(*, now: Optional[datetime] = None, heartbeat_path: Path = HEARTBEAT_PATH,
              lock_path: Path = INSTANCE_LOCK_PATH, spawn_state_path: Path = SPAWN_STATE_PATH,
              settings: Optional[Dict[str, Any]] = None,
              spawn: Optional[Callable[[], int]] = None) -> str:
    """nqx_cycle の 1 行。生死を出し、``autostart`` なら止まった fill_watch を起動し直す。

    例外は投げない(表示と起動の失敗で監視周期を止めない)。
    """
    line = status_line(now=now, path=heartbeat_path)
    try:
        cfg = settings if settings is not None else load_settings()
        if not cfg.get("autostart"):
            return line
        current = now or datetime.now(timezone.utc)
        state = _read_json(spawn_state_path)
        if line.startswith("fill_watch: alive"):
            if state.get("pending"):
                write_heartbeat({"pending": 0, "aliveAt": current.isoformat()}, spawn_state_path)
            return line
        if lock_held(lock_path):
            return line + " · autostart: 既存プロセスがロックを保持(応答なし?)のため起動しない"
        pending = int(state.get("pending") or 0)
        last_spawn = None
        try:
            last_spawn = datetime.fromisoformat(str(state.get("lastSpawnAt")))
        except (TypeError, ValueError):
            pass
        if pending >= SPAWN_SUSPEND_AFTER and last_spawn is not None \
                and (current - last_spawn).total_seconds() < SPAWN_RETRY_AFTER_SEC:
            return (line + f" · autostart 停止中: 起動 {pending} 回で heartbeat が出ない"
                    f"(.secrets/fill_watch.log を確認)")
        pid = (spawn or spawn_detached)()
        write_heartbeat({"pending": pending + 1, "lastSpawnAt": current.isoformat(), "pid": pid},
                        spawn_state_path)
        return line + f" · autostart: 起動 pid={pid}"
    except Exception as exc:  # noqa: BLE001 - 表示専用・周期を止めない
        return line + f" · autostart failed ({type(exc).__name__})"


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="R78/R91 約定監視: 建玉の枚数変化を数秒で検知して建玉管理を即時に起動する")
    parser.add_argument("--interval", type=float, default=None,
                        help="FLAT の間の照会間隔(秒)。既定は契約 fillWatch.idleIntervalSec(5)")
    parser.add_argument("--fast-interval", type=float, default=None,
                        help="建玉がある間の照会間隔(秒)。既定は契約 fillWatch.fastIntervalSec(1.5)")
    parser.add_argument("--max-rps", type=float, default=None,
                        help="このプロセスの毎秒リクエスト上限。既定は契約 fillWatch.maxRequestsPerSec(1.0)")
    parser.add_argument("--manage-every", type=float,
                        default=float(os.environ.get("NQX_FILL_WATCH_MANAGE_EVERY_SEC", "0")),
                        help="建玉がある間、変化が無くても N 秒ごとに管理を再評価する(既定 0 = 変化時のみ)")
    parser.add_argument("--symbol", default=None)
    parser.add_argument("--once", action="store_true", help="1 回だけ観測して終了")
    parser.add_argument("--no-resume", action="store_true",
                        help="前回 heartbeat の観測を基準にしない(起動時の観測を基準にする)")
    parser.add_argument("--started-by", default="manual", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    settings = load_settings()
    if settings["invalid"]:
        print(f"fill_watch: 契約 fillWatch の不正な項目を既定値にした: {settings['invalid']}")
    env_idle = os.environ.get("NQX_FILL_WATCH_INTERVAL_SEC")
    for key, value in (("idleIntervalSec", args.interval if args.interval is not None else env_idle),
                       ("fastIntervalSec", args.fast_interval), ("maxRequestsPerSec", args.max_rps)):
        if value is None:
            continue
        low, high = SETTING_BOUNDS[key]
        try:
            settings[key] = min(high, max(low, float(value)))
        except (TypeError, ValueError):
            print(f"fill_watch: {key} の指定 {value!r} を無視した")

    instance = FileLock(INSTANCE_LOCK_PATH)
    if not instance.acquire():
        print("fill_watch: 既に別のプロセスが常駐中(.secrets/fill_watch.lock)。起動しない")
        return 3

    import autotrade_engine as ae
    import broker_status
    import nqx_state

    cfg = ae._read_env_file()
    symbol = args.symbol or str(cfg.get("NQX_SYMBOL") or contract_month.symbol())
    accounts = configured_accounts(cfg)
    if not accounts:
        print("fill_watch: CROSSTRADE_ACCOUNTS が空なので監視対象がありません")
        instance.release()
        return 1
    reconcile_lock = Path(ae.LEDGER_FILE + ".lock")
    # R118: 観測の出所。OFF なら従来どおり REST を直接叩く(1 行も挙動が変わらない)。
    # SHADOW / LIVE では broker_source が口座ごとに Gateway か REST かを決める。
    # **Gateway が死んでいる口座は REST へ落ちる**ので、検知に穴は空かない。
    import broker_source
    source_mode = broker_source.mode()
    if source_mode == broker_source.OFF:
        position_query = broker_status.query_position
    else:
        position_query = broker_source.observation_position
    print(f"fill_watch: {symbol} accounts={len(accounts)} fast={settings['fastIntervalSec']:g}s "
          f"idle={settings['idleIntervalSec']:g}s max_rps={settings['maxRequestsPerSec']:g} "
          f"manage_every={args.manage_every:g}s started_by={args.started_by} "
          f"source={source_mode} heartbeat={HEARTBEAT_PATH}")
    try:
        return run(symbol=symbol, accounts=accounts, interval=settings["idleIntervalSec"],
                   position_query=position_query, reconcile=ae.reconcile,
                   price_query=ae._default_fresh_price_query,
                   sync_position=nqx_state.sync_position,
                   manage_every=max(0.0, args.manage_every), once=args.once,
                   resume=not args.no_resume, settings=settings,
                   lock_busy=lambda: lock_held(reconcile_lock), started_by=args.started_by)
    except KeyboardInterrupt:
        print("fill_watch: stopped")
        return 0
    finally:
        instance.release()


if __name__ == "__main__":
    sys.exit(main())

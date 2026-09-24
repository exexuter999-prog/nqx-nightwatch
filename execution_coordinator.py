# -*- coding: utf-8 -*-
"""R118: 実行 Coordinator —— 口座をまたぐ送信の調停。

いまの送信は `order.py --accounts a,b,c` を **1 プロセス**で呼び、その中で口座を
順番に回している。7 口座なら 7 口座ぶんの HTTP が直列に並ぶ。20 口座では
そのまま 3 倍になる。

ここは口座を **レーン**へ分け、口座間は並行・口座内は直列に走らせる。ただし
CrossTrade の上限(利用者あたり毎秒 3 本・溜め 20 本、REST と WebSocket で共有)は
**プロセスをまたいで**効くので、並行数を増やすだけでは速くならず、絞られて逆に
遅くなる(2026-09-19 実測: 6 並列で 1 本あたり 1.4 → 4.2 秒)。よって通信予算を
**ファイル越しに共有**し、全レーンの合計がその上限を超えないようにする。

**Gateway から直接送信しない。** 送信は従来どおり `order.py` の subprocess で、
安全ゲート・分割 OCO・claim・台帳はそのまま通る。ここがやるのは順序と本数の調停と、
口座・脚別の結果の取りまとめだけ。

**結果不明は終端。** `UNKNOWN` になったレーンを再送しない(CLAUDE.md §7)。
同じ `opKey` で同じ口座へ二度入らないよう、レーンは一度使ったら閉じる。

設定は `execution_contract.json` の `gateway.coordinator`。**既定 `enabled=false`。**
"""
from __future__ import annotations

import contextlib
import errno
import io
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import route_envelope

BASE = os.path.dirname(os.path.abspath(__file__))
SECRETS = os.path.join(BASE, ".secrets")
BUDGET_FILE = os.path.join(SECRETS, "comms_budget.json")
LANE_DIR = os.path.join(SECRETS, "lanes")
RESULT_LOG = os.path.join(SECRETS, "coordinator_results.jsonl")

SENT = "SENT"
REJECTED = "REJECTED"
UNKNOWN = "UNKNOWN"
PARTIAL = "PARTIAL"
SKIPPED = "SKIPPED"

#: レーンの持ち主が死んだとみなすまで。order.py の subprocess 上限より長くとる。
LANE_STALE_SEC = 600.0

#: トークン比較の丸め許容と、1 回の待ちの下限。**進行を保証するためのもの**で、
#: 上限を緩めるものではない(1e-9 本は 3 req/s に対して無視できる)。
_EPSILON = 1e-9
_MIN_SLEEP_SEC = 0.001


# ------------------------------------------------------------------ 設定

def config() -> Dict[str, Any]:
    try:
        with io.open(os.path.join(BASE, os.environ.get("NQX_EXECUTION_CONTRACT", "execution_contract.json")), encoding="utf-8") as fh:  # 🩹 Nerf Edition: NQX_EXECUTION_CONTRACT で差し替え可
            data = json.load(fh)
        section = ((data.get("gateway") or {}).get("coordinator") or {})
        return section if isinstance(section, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def enabled() -> bool:
    override = str(os.environ.get("NQX_COORDINATOR") or "").strip()
    if override in {"0", "false", "FALSE"}:
        return False
    if override in {"1", "true", "TRUE"}:
        return True
    return bool(config().get("enabled"))


def max_parallel() -> int:
    try:
        value = int(os.environ.get("NQX_COORDINATOR_PARALLEL")
                    or config().get("maxParallel") or 6)
    except (TypeError, ValueError):
        value = 6
    return max(1, min(32, value))


def _budget_config() -> Tuple[float, int, int]:
    section = config().get("commsBudget") or {}
    try:
        rate = float(section.get("requestsPerSec") or 3.0)
    except (TypeError, ValueError):
        rate = 3.0
    try:
        burst = int(section.get("burst") or 20)
    except (TypeError, ValueError):
        burst = 20
    try:
        reserve = int(section.get("reserveForObservation") or 0)
    except (TypeError, ValueError):
        reserve = 0
    return max(0.1, rate), max(1, burst), max(0, reserve)


#: 送信(`/v1/send/`)が REST と同じ予算を食うか。**未確認**。
#: 公式のレート制限ページ(2026-09-19 確認)が対象として挙げるのは
#: 「HTTP REST(`/v1/api/*`)と WebSocket RPC」で、`/v1/send/` は一度も出てこない。
#: 確かめるには注文を送るしかないので、ここは `None`(不明)のまま置く。
#: **不明を「食わない」と読み替えて計算しない。** 下限は両方の読みで出す。
def send_counts_against_budget() -> Optional[bool]:
    value = (config().get("commsBudget") or {}).get("sendCountsAgainstApiBudget")
    return value if isinstance(value, bool) else None


def per_account_requests(operation: str, kind: str = "api") -> int:
    """1 口座 1 操作あたりの本数。

    `kind` は "api"(上限の対象になる `/v1/api/*`)/ "send"(`/v1/send/`)/
    "all" / **"gatewayApi"**(送信検証を押し込みから取ったときに残る `/v1/api/*`)。
    """
    section = (config().get("perAccountRequests") or {}).get(str(operation).upper())
    defaults = {"ENTRY": {"api": 6, "send": 2, "gatewayApi": 1},
                "MODIFY": {"api": 4, "send": 1, "gatewayApi": 0},
                "FLATTEN": {"api": 3, "send": 1, "gatewayApi": 0}}.get(
                    str(operation).upper(), {"api": 6, "send": 1, "gatewayApi": 1})
    if isinstance(section, dict):
        values = {"api": section.get("api", defaults["api"]),
                  "send": section.get("send", defaults["send"]),
                  "gatewayApi": section.get("gatewayApi", defaults["gatewayApi"])}
    elif section is not None:
        # 旧い形(合計 1 つの数)との互換。全部 api 側として数える(安全側)。
        try:
            return max(1, int(section)) if kind in ("api", "all") else 0
        except (TypeError, ValueError):
            values = defaults
    else:
        values = defaults
    try:
        api = max(0, int(values["api"]))
        send = max(0, int(values["send"]))
        gateway_api = max(0, int(values.get("gatewayApi", defaults["gatewayApi"])))
    except (TypeError, ValueError):
        api, send = defaults["api"], defaults["send"]
        gateway_api = defaults["gatewayApi"]
    return {"api": api, "send": send, "all": api + send, "gatewayApi": gateway_api}[kind]


# ------------------------------------------------------- プロセス横断のロック

@contextlib.contextmanager
def _file_lock(path: str, timeout: float = 30.0, poll: float = 0.01):
    """`O_CREAT|O_EXCL` の場所取り。臨界区間は短く保つこと。

    持ち主が死んで残ったロックは `LANE_STALE_SEC` で引き剥がす。引き剥がしは
    **時刻だけ**で判断する —— pid を見ても Windows では別プロセスに再利用される。
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    deadline = time.monotonic() + timeout
    handle = None
    while True:
        try:
            handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except OSError as exc:
            if exc.errno not in (errno.EEXIST, errno.EACCES):
                raise
            try:
                age = time.time() - os.path.getmtime(path)
                if age > LANE_STALE_SEC:
                    os.unlink(path)
                    continue
            except OSError:
                pass
            if time.monotonic() > deadline:
                raise TimeoutError(f"could not take {os.path.basename(path)} within {timeout}s")
            time.sleep(poll)
    try:
        os.write(handle, str(time.time()).encode("ascii"))
        os.close(handle)
        handle = None
        yield
    finally:
        if handle is not None:
            with contextlib.suppress(OSError):
                os.close(handle)
        with contextlib.suppress(OSError):
            os.unlink(path)


# ------------------------------------------------------------- 通信予算

class CommsBudget:
    """プロセスをまたぐトークンバケツ。

    CrossTrade の上限は **利用者ごと**なので、`order.py` の子プロセスと
    `fill_watch` と監視ループが同じ枠を食い合う。プロセス内のバケツでは足りない。

    `reserve` は観測(Gateway の RPC・鮮度の取り直し)へ残す枠。送信で溜めを
    使い切ると、送った直後の確認がそのぶん待たされる。
    """

    def __init__(self, *, rate: Optional[float] = None, burst: Optional[int] = None,
                 reserve: Optional[int] = None, path: Optional[str] = None,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep,
                 shared: bool = True):
        cfg_rate, cfg_burst, cfg_reserve = _budget_config()
        self.rate = float(rate if rate is not None else cfg_rate)
        self.burst = int(burst if burst is not None else cfg_burst)
        self.reserve = int(reserve if reserve is not None else cfg_reserve)
        self.path = path or BUDGET_FILE
        self.clock = clock
        self.sleep = sleep
        self.shared = shared
        self.waited = 0.0
        self.granted = 0
        self._local = {"tokens": float(max(1, self.burst - self.reserve)), "at": clock()}
        self._lock = threading.Lock()

    @property
    def capacity(self) -> int:
        return max(1, self.burst - self.reserve)

    # -- 状態の読み書き ----------------------------------------------------
    def _read(self) -> Dict[str, float]:
        if not self.shared:
            return dict(self._local)
        try:
            with io.open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
            return {"tokens": float(data["tokens"]), "at": float(data["at"])}
        except Exception:  # noqa: BLE001 — 壊れていれば満杯から始める
            return {"tokens": float(self.capacity), "at": self.clock()}

    def _write(self, state: Dict[str, float]) -> None:
        if not self.shared:
            self._local = dict(state)
            return
        tmp = self.path + ".tmp"
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with io.open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"tokens": state["tokens"], "at": state["at"]}, fh)
        os.replace(tmp, self.path)

    # -- 取得 --------------------------------------------------------------
    def take(self, count: int = 1, *, timeout: float = 120.0) -> float:
        """`count` 本ぶんの枠を取る。戻り値は待った秒数。

        待ちは **ロックの外**で行う。ロックを握ったまま眠ると他のレーンが
        補充の計算すらできなくなる。
        """
        count = max(1, int(count))
        if count > self.capacity:
            # 1 回の操作が溜めより大きい。刻んで取る(全体の待ちは同じ)。
            waited = 0.0
            remaining = count
            while remaining > 0:
                step = min(self.capacity, remaining)
                waited += self.take(step, timeout=timeout)
                remaining -= step
            return waited

        deadline = self.clock() + timeout
        total_wait = 0.0
        while True:
            with self._lock:
                ctx = (_file_lock(self.path + ".lock", timeout=10.0)
                       if self.shared else contextlib.nullcontext())
                with ctx:
                    state = self._read()
                    now = self.clock()
                    elapsed = max(0.0, now - state["at"])
                    tokens = min(float(self.capacity), state["tokens"] + elapsed * self.rate)
                    # 丸めの許容。`elapsed * rate` は 1/3 のような値で必ず端数が出るので、
                    # 厳密比較にすると **あと 1e-16 足りない** で待ち続ける。仮想時計の
                    # 試験でここが生きたループになった(2026-09-19)。実時計でも同じことが
                    # 起きて、眠る時間が 0 に近づきながら CPU を焼く。
                    if tokens + _EPSILON >= count:
                        self._write({"tokens": max(0.0, tokens - count), "at": now})
                        self.granted += count
                        self.waited += total_wait
                        return total_wait
                    # 前へ進むことを保証する。`need` が 0 に潰れると上と同じ轍になる。
                    need = max((count - tokens) / self.rate, _MIN_SLEEP_SEC)
                    # 補充の起点を進めておく(他のレーンが同じ待ちを二重に数えない)。
                    self._write({"tokens": tokens, "at": now})
            if self.clock() + need > deadline:
                raise TimeoutError(
                    f"comms budget could not grant {count} within {timeout}s")
            self.sleep(need)
            total_wait += need

    def floor_sec(self, requests: int) -> float:
        """満杯から `requests` 本を流すときの、補充待ちだけの下限。"""
        extra = max(0, int(requests) - self.capacity)
        return extra / self.rate


# ------------------------------------------------------------------ レーン

class AccountLane:
    """1 口座の操作を直列化する。**同じ口座へ同時に 2 つ入れない。**

    プロセス内のロックだけでは足りない —— `fill_watch` と監視ループは別プロセスで、
    どちらも同じ口座へ MODIFY を出しうる。ファイルの場所取りで揃える。
    """

    def __init__(self, account: str, *, directory: Optional[str] = None,
                 shared: bool = True):
        self.account = str(account)
        self.directory = directory or LANE_DIR
        self.shared = shared
        self._local = threading.Lock()

    @property
    def path(self) -> str:
        safe = "".join(ch if ch.isalnum() else "_" for ch in self.account)
        return os.path.join(self.directory, f"{safe}.lane")

    @contextlib.contextmanager
    def hold(self, timeout: float = 60.0):
        acquired = self._local.acquire(timeout=timeout)
        if not acquired:
            raise TimeoutError(f"account lane {self.account} is busy in-process")
        try:
            if self.shared:
                with _file_lock(self.path, timeout=timeout):
                    yield
            else:
                yield
        finally:
            self._local.release()


# ------------------------------------------------------------------ 結果

class LegOutcome(dict):
    """1 口座 1 脚の結果。`route_envelope` の行と同じ形を保つ。"""


class AccountOutcome(dict):
    """1 口座の結果。`status` は SENT / REJECTED / UNKNOWN / PARTIAL / SKIPPED。"""


def _classify(rc: int, detail: str, account: str) -> Tuple[str, Dict[str, Any]]:
    """戻り値は (status, envelope)。**判定できないものは UNKNOWN に倒す。**"""
    envelope = route_envelope.parse(detail, expected_accounts=[account],
                                    require_resolved=True)
    if envelope.get("ok"):
        state = str(envelope.get("state") or UNKNOWN).upper()
        if state in {SENT, REJECTED, PARTIAL, UNKNOWN}:
            # rc とも突き合わせる。envelope が SENT でも rc≠0 なら信じない。
            if state == SENT and rc != 0:
                return UNKNOWN, envelope
            return state, envelope
        return UNKNOWN, envelope
    # envelope が読めない場合。**rc=0 でも送信済みと言わない**(§7 の規律)。
    return UNKNOWN, envelope


class CoordinatorResult(dict):
    """全レーンの結果と、併合した envelope。"""

    @property
    def merged_detail(self) -> str:
        return str(self.get("mergedDetail") or "")


def _merge_envelopes(outcomes: Sequence[AccountOutcome]) -> Tuple[str, str]:
    """各レーンの envelope を 1 本へ併合する。

    engine と台帳は従来どおり 1 本の envelope を読む。ここで形を変えると
    所有権の束縛(`route_identity`)が全部通らなくなるので、**行をそのまま並べ、
    合計だけ数え直す**。
    """
    rows: List[Dict[str, Any]] = []
    for outcome in outcomes:
        envelope = outcome.get("envelope") or {}
        snapshot = envelope.get("snapshot")
        if isinstance(snapshot, list) and snapshot:
            rows.extend(snapshot)
            continue
        # envelope を読めなかったレーンは、その口座の両脚を UNKNOWN として残す。
        # **落とさない** —— 落とすと「送っていない」と読めてしまう。
        for leg in route_envelope.LEGS:
            rows.append({"accountId": outcome.get("account"), "legId": leg,
                         "state": "UNKNOWN", "orderId": None, "receipt": None,
                         "filledAt": None})
    accepted = sum(1 for row in rows if row.get("state") == "ACCEPTED")
    rejected = sum(1 for row in rows if row.get("state") == "REJECTED")
    unknown = sum(1 for row in rows if row.get("state") == "UNKNOWN")
    total = len(rows)
    if total and accepted == total:
        state = SENT
    elif total and rejected == total:
        state = REJECTED
    elif unknown and accepted == 0:
        state = UNKNOWN
    elif accepted and (rejected or unknown):
        state = PARTIAL
    else:
        state = UNKNOWN
    return state, route_envelope.format_envelope(rows, state, resolved=1)


# ------------------------------------------------------------- Coordinator

class Coordinator:
    """口座間の並行送信。`runner(args, confirm) -> (rc, detail)` を注入して試験する。"""

    def __init__(self, *, runner: Callable[[List[str], bool], Tuple[int, str]],
                 budget: Optional[CommsBudget] = None,
                 parallel: Optional[int] = None,
                 lane_factory: Optional[Callable[[str], AccountLane]] = None,
                 clock: Callable[[], float] = time.monotonic,
                 record: Optional[Callable[[Dict[str, Any]], None]] = None,
                 pre_check: Optional[Callable[[str], Optional[str]]] = None,
                 post_check: Optional[Callable[[str, AccountOutcome], Optional[str]]] = None):
        self.runner = runner
        self.budget = budget if budget is not None else CommsBudget()
        self.parallel = parallel if parallel is not None else max_parallel()
        self.lane_factory = lane_factory or (lambda account: AccountLane(account))
        self.clock = clock
        self.record = record
        self.pre_check = pre_check
        self.post_check = post_check
        #: 同じ (opKey, 口座) で二度送らないための印。**プロセス内で終端**。
        self._used: Dict[Tuple[str, str], str] = {}
        self._used_lock = threading.Lock()

    # -- 1 レーン ----------------------------------------------------------
    def _one(self, *, op_key: str, operation: str, account: str,
             args: List[str], confirm: bool) -> AccountOutcome:
        started = self.clock()
        outcome = AccountOutcome({
            "account": account, "operation": operation, "opKey": op_key,
            "status": SKIPPED, "rc": None, "detail": "", "legs": [],
            "waitedSec": 0.0, "elapsedSec": 0.0, "sent": False,
        })
        with self._used_lock:
            previous = self._used.get((op_key, account))
            if previous is not None:
                outcome.update({"status": SKIPPED,
                                "detail": f"already attempted in this run ({previous})"})
                return outcome
            # 送る **前** に印を付ける。落ちた後に再入して二度送るのを防ぐ。
            self._used[(op_key, account)] = "IN_FLIGHT"

        try:
            lane = self.lane_factory(account)
            with lane.hold(timeout=90.0):
                if self.pre_check is not None:
                    blocked = self.pre_check(account)
                    if blocked:
                        outcome.update({"status": SKIPPED, "detail": f"pre-check: {blocked}"})
                        return outcome
                need = budget_units(operation)
                waited = self.budget.take(need)
                outcome["waitedSec"] = round(waited, 3)
                rc, detail = self.runner(args, confirm)
                status, envelope = _classify(rc, detail, account)
                outcome.update({
                    "status": status, "rc": rc, "detail": detail,
                    "envelope": envelope,
                    "legs": [LegOutcome(row) for row in (envelope.get("snapshot") or [])],
                    "sent": status in {SENT, PARTIAL} or status == UNKNOWN,
                })
                if self.post_check is not None:
                    note = self.post_check(account, outcome)
                    if note:
                        outcome["postCheck"] = note
        except TimeoutError as exc:
            # 予算もレーンも取れなかった = **送っていない**。ここだけは SKIPPED。
            outcome.update({"status": SKIPPED, "detail": f"lane/budget timeout: {exc}"})
        except Exception as exc:  # noqa: BLE001 — 送ったかどうか分からない
            outcome.update({"status": UNKNOWN,
                            "detail": f"coordinator failure: {type(exc).__name__}: {exc}"})
        finally:
            outcome["elapsedSec"] = round(self.clock() - started, 3)
            with self._used_lock:
                self._used[(op_key, account)] = outcome["status"]
            self._write_result(outcome)
        return outcome

    def _write_result(self, outcome: AccountOutcome) -> None:
        if self.record is not None:
            try:
                self.record(dict(outcome))
            except Exception:  # noqa: BLE001
                pass
            return
        try:
            os.makedirs(SECRETS, exist_ok=True)
            with io.open(RESULT_LOG, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({
                    "at": time.time(),
                    **{key: value for key, value in outcome.items()
                       if key not in {"detail", "envelope"}},
                }, ensure_ascii=False) + "\n")
        except Exception:  # noqa: BLE001
            pass

    # -- 全レーン ----------------------------------------------------------
    def run(self, *, op_key: str, operation: str, accounts: Iterable[str],
            build_args: Callable[[str], List[str]], confirm: bool) -> CoordinatorResult:
        scope = [str(value).strip() for value in accounts if str(value).strip()]
        if not scope:
            return CoordinatorResult({"state": SKIPPED, "outcomes": [], "mergedDetail": "",
                                      "accounts": 0, "budgetWaitedSec": 0.0})
        started = self.clock()
        workers = max(1, min(self.parallel, len(scope)))
        outcomes: List[AccountOutcome] = []
        if workers == 1:
            for account in scope:
                outcomes.append(self._one(op_key=op_key, operation=operation,
                                          account=account, args=build_args(account),
                                          confirm=confirm))
        else:
            with ThreadPoolExecutor(max_workers=workers,
                                    thread_name_prefix="coord") as pool:
                futures = [pool.submit(self._one, op_key=op_key, operation=operation,
                                       account=account, args=build_args(account),
                                       confirm=confirm)
                           for account in scope]
                outcomes = [future.result() for future in futures]
        # 並びを入力順へ戻す(台帳と報告の再現性のため)。
        order = {account: index for index, account in enumerate(scope)}
        outcomes.sort(key=lambda row: order.get(row.get("account"), 0))
        attempted = [row for row in outcomes if row.get("status") != SKIPPED]
        state, merged = _merge_envelopes(attempted) if attempted else (SKIPPED, "")
        return CoordinatorResult({
            "state": state,
            "accounts": len(scope),
            "attempted": len(attempted),
            "skipped": len(outcomes) - len(attempted),
            "outcomes": outcomes,
            "mergedDetail": merged,
            "elapsedSec": round(self.clock() - started, 3),
            "budgetWaitedSec": round(sum(row.get("waitedSec") or 0.0 for row in outcomes), 3),
            "unknown": [row["account"] for row in outcomes if row.get("status") == UNKNOWN],
        })


def order_floor_sec(accounts: int, operation: str = "ENTRY",
                    budget: Optional[CommsBudget] = None,
                    *, gateway: bool = False,
                    include_send: Optional[bool] = None) -> float:
    """`accounts` 口座へ `operation` を出すときの、補充待ちだけの下限(秒)。

    `gateway=True` は「送信前後の検証と identity 束縛を Gateway の押し込みで賄う」
    場合 —— 上限の対象になる `/v1/api/*` が **0 本**になる。

    `include_send` は送信(`/v1/send/`)を予算に数えるかどうか。既定は契約の
    `sendCountsAgainstApiBudget`(**未確認なら安全側の True**)。不明を「食わない」
    と読み替えないための既定値で、確認が取れたら契約に false を書く。
    """
    budget = budget if budget is not None else CommsBudget(shared=False)
    counted = send_counts_against_budget() if include_send is None else include_send
    accounts = max(0, int(accounts))
    # Gateway 経路でも 0 本にはならない —— ENTRY は約定履歴の窓が REST に残る。
    api = accounts * per_account_requests(operation, "gatewayApi" if gateway else "api")
    send = accounts * per_account_requests(operation, "send")
    return budget.floor_sec(api + (send if counted is not False else 0))


def budget_units(operation: str, *, gateway: bool = False) -> int:
    """Coordinator が 1 口座ぶんに確保する枠の数。

    送信が予算を食うかは未確認なので、**確認が取れるまでは食う前提**で確保する
    (少なく確保して 429 を踏むより、多めに確保して少し遅い方がまし)。
    """
    counted = send_counts_against_budget()
    api = per_account_requests(operation, "gatewayApi" if gateway else "api")
    send = per_account_requests(operation, "send") if counted is not False else 0
    return max(1, api + send)


def floor_table(accounts: int, operation: str = "ENTRY") -> Dict[str, float]:
    """下限を **読みごとに分けて**返す。推測を 1 つの数へ潰さないため。"""
    return {
        "restSendCounted": order_floor_sec(accounts, operation, include_send=True),
        "restSendFree": order_floor_sec(accounts, operation, include_send=False),
        "gatewaySendCounted": order_floor_sec(accounts, operation, gateway=True,
                                              include_send=True),
        "gatewaySendFree": order_floor_sec(accounts, operation, gateway=True,
                                           include_send=False),
    }

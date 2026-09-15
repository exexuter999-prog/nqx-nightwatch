#!/usr/bin/env python3
"""1 サイクルを 1 コマンドで回す（R39）。

CLAUDE.md §6.2a の順序 —— 取得 → ingest → pipeline → publish —— を、
その順序でしか実行できない形に固める。以前はこれが 4 つの手打ちコマンドと
1 つの `python -c` ワンライナーに分かれていて、次の 3 つが実際に起きていた。

  * `monitor_publish.py` を生スナップショットで直接叩いてしまう（§6.2a 違反）
  * `PYTHONUTF8` を付け忘れて publish が文字化けで落ちる
  * `monitor_pipeline` が BLOCKED でも publish まで進んでしまう

このスクリプトはそのどれも起こせない。段階を飛ばせず、UTF-8 を自前で固定し、
`status=READY` かつ `publishPreflight.ready=true` のときにしか publish を呼ばない。

R51 で **取得そのもの**もこの中へ入れた。以前はエージェントが MCP の応答を
読んで `.secrets/tv_raw/*.json` へ書き写しており、その転記に数分かかるせいで
先に書いた raw が 240 秒の鮮度窓を割り、`required raw acquisition is not fresh`
で落ちていた（2026-09-01 に 2 サイクル連続で発生）。`tv_fetch.py` が MCP
サーバ同梱 CLI から同じ JSON を直接落とすので、取得は 1 周 4 秒で終わる。

    python nqx_cycle.py                 # 1 サイクル（取得も含む）
    python nqx_cycle.py --no-fetch      # raw を取り直さず、今ある raw で回す
    python nqx_cycle.py --dry           # publish 直前まで（送信も通知もしない）
    python nqx_cycle.py --force-window  # 監視窓の外でも回す（配線確認用）
    $cvdRetryJson | python nqx_cycle.py --cvd-retry

終了コード: 0=publish 済み / 1=BLOCKED（このサイクルは見送り）/ 2=HALT。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

BASE = Path(__file__).resolve().parent
CONFIG = BASE / "monitor_config.json"
RAW_DIR = BASE / ".secrets" / "tv_raw"
TV_BUNDLE = BASE / ".secrets" / "tv_bundle.json"
AUDIT_DIR = BASE / ".secrets"
# publish 後の正本(R77)。`monitor_publish.main()` が Telegram 送信まで終えた bundle を
# ここへ書き、ゲート後の scenario を `_published_scenario` に残す(None = 武装なし)。
# `monitor_publish.STATE_FILE` と同じパス。重い import を避けて直書きし、乖離は
# tests/test_r77_report_line_published_state.py が止める。
PUBLISHED_STATE = BASE / ".secrets" / "monitor_last_sent.json"
# R81: 窓が閉じた後に一度だけ建玉の正本を画面へ反映した記録(JST の日付)。
WINDOW_SETTLE_STATE = BASE / ".secrets" / "window_settle.json"

JST = timezone(timedelta(hours=9))
WINDOW_OPEN = dt_time(7, 0)      # JST（2026-09-01 に 12:30 から前倒し）
WINDOW_CLOSE = dt_time(5, 45)    # JST（翌日。2026-09-05 ユーザー指示で 04:00 → 05:45）

# 子プロセスは必ず UTF-8 で動かす。日本語入りの bundle をロケールの
# コードページで復号させない（cp932 環境で publish が落ちていた原因）。
CHILD_ENV = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}

# 自分の標準出力も同じ理由で UTF-8 に固定する。子プロセスだけ直していたので、
# BLOCKED の理由行（em dash や日本語を含む）を出す print が cp932 で
# UnicodeEncodeError を投げ、**停止理由が画面に出ないまま落ちていた**。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass


def in_window(now: Optional[datetime] = None) -> bool:
    """JST 07:00〜翌 05:45 か。窓の外ではデータ取得も評価も発注もしない。"""
    local = (now or datetime.now(timezone.utc)).astimezone(JST).time()
    return local >= WINDOW_OPEN or local < WINDOW_CLOSE


def _run(args: List[str], stdin: Optional[bytes] = None,
         timeout: int = 180) -> Tuple[int, str, str]:
    proc = subprocess.run([sys.executable, *args], input=stdin, cwd=str(BASE),
                          env=CHILD_ENV, capture_output=True, timeout=timeout,
                          check=False)
    return (proc.returncode,
            (proc.stdout or b"").decode("utf-8", "replace"),
            (proc.stderr or b"").decode("utf-8", "replace"))


def _read_json(path: Path) -> Any:
    return json.loads(path.read_bytes().decode("utf-8-sig"))


def _config() -> Dict[str, Any]:
    return _read_json(CONFIG)


def _paths() -> Dict[str, Path]:
    cfg = _config()
    provider, output = cfg.get("provider") or {}, cfg.get("output") or {}
    return {
        "snapshot": BASE / str(provider.get("snapshotInputPath")),
        "cvdRetry": BASE / str(provider.get("cvdRetryInputPath")),
        "cycle": BASE / str(output.get("cyclePath")),
        "bundle": BASE / str(output.get("publishBundlePath")),
    }


def _preflight_config() -> Optional[str]:
    """設定外パス・LIVE 設定・未知 provider は、走らせる前に止める（§6.2a 1）。"""
    try:
        cfg = _config()
    except (OSError, ValueError) as exc:
        return f"monitor_config.json unreadable: {exc}"
    if str(cfg.get("schemaVersion") or "") != "NQX_MONITOR_PIPELINE/1":
        return "monitor_config.schemaVersion is unsupported"
    if str((cfg.get("provider") or {}).get("type") or "") != "file":
        return "monitor_config.provider.type must be file"
    if str((cfg.get("execution") or {}).get("mode") or "").upper() != "DRY_RUN_ONLY":
        return "monitor_config.execution.mode must be DRY_RUN_ONLY"
    return None


# ------------------------------------------------------------------ 段階

def stage_fetch(cvd_only: bool = False) -> Tuple[bool, str]:
    """TradingView raw を取り直す（R51）。

    **エージェントに MCP の出力を書き写させない。** `tv_fetch.py` が CLI 経由で
    逐語保存するので、必須 8 raw の mtime は数秒以内に揃う。ここが失敗した
    サイクルは、古い raw で評価させずに BLOCKED にする。
    """
    args = ["tv_fetch.py", "--raw-dir", str(RAW_DIR)]
    if cvd_only:
        args.append("--cvd-only")
    # R52: MCP 同梱 CLI(node)は間欠的に 0xC0000409 で落ちる(2026-09-04〜05 の夜に
    # 30 サイクル中 3 回、毎回次の呼び出しで復帰。常駐 MCP サーバとの CDP 競合と推定)。
    # 取得は冪等で 1 回 4 秒なので、BLOCKED にする前に短く待って取り直す。
    # 上限 attempts × (4 秒 + retry_sec) は raw 鮮度窓 240 秒の内側に収まる。
    attempts = max(1, int(os.environ.get("NQX_FETCH_ATTEMPTS", "3")))
    pause = max(0.0, float(os.environ.get("NQX_FETCH_RETRY_SEC", "2")))
    detail = ""
    for attempt in range(1, attempts + 1):
        code, out, err = _run(args, timeout=300)
        detail = (out + err).strip()
        if code == 0:
            if attempt > 1:
                detail = f"{detail}\n(fetch succeeded on attempt {attempt}/{attempts})"
            return True, detail
        if attempt < attempts:
            time.sleep(pause)
    return False, f"{detail or 'tv_fetch.py failed'} (attempt {attempts}/{attempts})"


def stage_acquire(replay: bool = False) -> Tuple[bool, str]:
    args = ["tv_snapshot.py", "--raw-dir", str(RAW_DIR), "--out", str(TV_BUNDLE)]
    if replay:
        args.append("--replay-now-from-bars")
    code, out, err = _run(args)
    detail = (out + err).strip()
    if code != 0:
        return False, detail or "tv_snapshot.py failed"
    return True, detail


def stage_ingest(kind: str, raw: bytes) -> Tuple[bool, str]:
    code, out, err = _run(["monitor_ingest.py", "--config", str(CONFIG), "--kind", kind],
                          stdin=raw)
    if code != 0:
        return False, (err or out).strip() or "monitor_ingest.py failed"
    try:
        receipt = json.loads(out.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return False, f"ingest receipt unreadable: {out.strip()[:200]}"
    # §6.2a 3: receipt に orderInvoked=false / networkInvoked=false が無ければ止める。
    for field in ("orderInvoked", "networkInvoked", "reconcileInvoked"):
        if receipt.get(field) is not False:
            return False, f"ingest receipt {field} is not false"
    return True, json.dumps(receipt, ensure_ascii=False, sort_keys=True)


def stage_pipeline() -> Tuple[str, Dict[str, Any], str]:
    """戻り値 (status, cycle, detail)。status は READY / BLOCKED / ERROR。"""
    code, out, err = _run(["monitor_pipeline.py", "--config", str(CONFIG)])
    if code == 2:
        return "ERROR", {}, (err or out).strip()
    try:
        cycle = _read_json(_paths()["cycle"])
    except (OSError, ValueError) as exc:
        return "ERROR", {}, f"cycle artifact unreadable: {exc}"
    status = str(cycle.get("status") or "BLOCKED").upper()
    ready = bool((cycle.get("publishPreflight") or {}).get("ready"))
    if status == "READY" and not ready:
        status = "BLOCKED"
    return status, cycle, (out or err).strip()


def stage_publish() -> Tuple[bool, str, Optional[Dict[str, Any]]]:
    """戻り値 (ok, detail, published_state)。

    published_state は publish が **実際に載せた** ゲート後の scenario
    (`read_published_state`)。publish が失敗した、または正本が読めない/別
    サイクルのものなら None で、報告行は「未確認」と明示する。
    """
    bundle_path = _paths()["bundle"]
    try:
        raw = bundle_path.read_bytes()
    except OSError as exc:
        return False, f"publish bundle unreadable: {exc}", None
    try:
        sent_at = (json.loads(raw.decode("utf-8-sig")) or {}).get("at")
    except (ValueError, AttributeError):
        sent_at = None
    code, out, err = _run(["monitor_publish.py"], stdin=raw, timeout=300)
    detail = (out + ("\n" + err if err.strip() else "")).strip()
    if code != 0:
        return False, detail, None
    return True, detail, read_published_state(sent_at)


def read_published_state(sent_at: Any) -> Optional[Dict[str, Any]]:
    """publish 後の正本から、実際に載ったゲート後の scenario を引く(R77)。

    pipeline の decision は publish **前** の提案で、`monitor_publish.py` は
    その後にイベント封鎖・ボラゲート・ULTRA・実行契約の各ゲートで ARMED を
    WATCH へ落とすことがある。2026-09-10 23:41 は publish が
    「vol gate stand-down … demoted to WATCH」と出したのに、報告行は decision
    から `A ARMED` を印字した。報告行は Telegram の先頭行であり、エージェントが
    逐語で返す行なので、載った状態を映さなければならない。

    `at` が送った bundle と一致する正本だけを採る。未書き込み・別サイクルの
    残骸・読めないときは None を返し、呼び出し側に「未確認」と言わせる。
    duplicate skip(同じ fingerprint)のサイクルでは前回の正本が同じ `at` を
    持つので、そのまま同じ状態が返る。
    """
    if not sent_at:
        return None
    try:
        sent = _read_json(PUBLISHED_STATE)
    except (OSError, ValueError):
        return None
    if not isinstance(sent, dict) or "_published_scenario" not in sent:
        return None
    if str(sent.get("at") or "") != str(sent_at):
        return None
    scenario = sent.get("_published_scenario")
    return {
        "at": sent.get("at"),
        "source": PUBLISHED_STATE.name,
        "scenario": dict(scenario) if isinstance(scenario, dict) else None,
    }


def save_audit(now: Optional[datetime] = None) -> Optional[Path]:
    """§6.3 の per-cycle 監査保存。pipeline は固定パスを上書きするだけなので、
    ここで時刻付きのコピーを残さないと履歴が一切残らない。"""
    stamp = (now or datetime.now(timezone.utc)).astimezone(JST).strftime("%H%M")
    try:
        bundle = _paths()["bundle"].read_bytes()
    except OSError:
        return None
    target = AUDIT_DIR / f"monitor_cycle_{stamp}.json"
    target.write_bytes(bundle)
    return target


# ------------------------------------------------------------------ 報告

def _price_from_bundle() -> Optional[float]:
    """報告行の現在値。bundle が無い/読めないサイクルでは黙って省く。"""
    try:
        value = float(_read_json(_paths()["bundle"]).get("price"))
    except (OSError, ValueError, TypeError, KeyError):
        return None
    return value


def _fmt(value: Any) -> Optional[str]:
    try:
        return f"{float(value):,.2f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        return None


def report_line(cycle: Dict[str, Any], published: bool, extra: str = "",
                published_state: Optional[Dict[str, Any]] = None) -> str:
    """1 サイクル 1 行。**エージェントはこの行をそのまま報告する。**

    値を手で組み直させない —— 403 サイクルで ICT 入力が 0% だった原因は
    まさに「エージェントが数字を書き写す」工程だった。価格・建値・SL・TP は
    publish した bundle と cycle 成果物からここで引く。

    R77: publish が成功したサイクルは pipeline の decision ではなく、
    `read_published_state()` が正本から引いた **ゲート後の scenario** で書く。
    decision は publish 前の提案で、封鎖・ボラ・ULTRA・契約のゲートで WATCH に
    落ちた後もそのまま残る。decision に戻るのは publish が走らなかった
    (BLOCKED / DRY / 失敗)サイクルだけで、publish は通ったのに正本が読めない
    ときは decision を出しつつ「未確認」を末尾に明示する。
    """
    unread = False
    if published and published_state is not None:
        source = published_state.get("scenario")
        source = source if isinstance(source, dict) else {}
    else:
        source = cycle.get("decision") or {}
        unread = bool(published)
    contract = source.get("executionContract")
    contract = contract if isinstance(contract, dict) else {}
    model = str(source.get("model") or ("SCENARIO" if source else "FLAT"))
    grade = str(source.get("grade") or contract.get("effectiveGrade") or "")
    side = str(source.get("side") or "")
    state = str(source.get("state") or "")
    stamp = datetime.now(JST).strftime("%H:%M")
    symbol = str(cycle.get("symbol") or "MNQ")
    price = _fmt(_price_from_bundle())
    head = f"[{stamp}] {symbol}" + (f" {price}" if price else "")

    parts: List[str] = [head]
    if model in ("FLAT", "") or not grade:
        parts.append("primary=NONE")
    else:
        parts.append(" ".join(x for x in (f"primary={model}", side, grade, state) if x))
        geometry = []
        entry, stop = _fmt(source.get("entry")), _fmt(source.get("stop"))
        targets = [t for t in (_fmt(x) for x in (source.get("targets") or [])) if t]
        if entry:
            geometry.append(f"E={entry}")
        if stop:
            geometry.append(f"SL={stop}")
        if targets:
            geometry.append("TP=" + "/".join(targets[:2]))
        if geometry:
            parts.append(" ".join(geometry))
    parts.append("published" if published else "no-publish")
    if unread:
        parts.append("post-gate state unread — showing pipeline decision")
    if extra:
        parts.append(extra)
    return " | ".join(parts)


_DEMOTION_SUFFIX = re.compile(r"\s*[—-]+\s*(?:scenario|grade \S+) demoted to WATCH.*$")


def publish_demotion_notes(detail: str) -> List[str]:
    """publish 出力から「武装を WATCH へ落とした」注記だけを抜く(R77)。

    ボラゲート・イベント封鎖・ULTRA 見送り・口座上限は `monitor_publish.py` が
    `… — scenario demoted to WATCH` の形で 1 行ずつ出す。報告行の末尾に理由を
    載せるための抽出で、状態そのものは `read_published_state()` が正本から引く。
    """
    notes: List[str] = []
    for line in str(detail or "").splitlines():
        text = line.strip().lstrip("-").strip()
        if "demoted to WATCH" not in text:
            continue
        notes.append(_DEMOTION_SUFFIX.sub("", text).strip() or text)
    return notes


#: 上位足の欠落・陳腐化コード → 取得に必要な timeframe と保存先。
HTF_FRAMES = (
    ("BARS45M", "45", "bars45m.json"),
    ("BARS1H", "60", "bars1h.json"),
    ("BARS4H", "240", "bars4h.json"),
    ("BARS1D", "1D", "bars1d.json"),
)


def htf_due(cycle: Dict[str, Any]) -> List[str]:
    """欠落・陳腐化している上位足を、そのまま実行できる指示に変えて返す。

    上位足はエージェントが MCP を叩かない限り絶対に埋まらない。ところが
    ループ指示文の取得リストに 45分/1h/4h が無く、pipeline は
    ``BARS45M_STALE_*`` を ``missing`` へ積むだけで **誰にも届いていなかった**
    (2026-08-25 実測: 45m 7.9h / 1h 7.7h / 4h 10.7h 陳腐化)。missing は
    非ブロッキングなので BLOCKED にもならず、HTF の加点だけが静かに死ぬ。

    「気付けるように書いておく」では足りない —— R39 で段階を指示文へ並べる
    のをやめたのと同じ理由で、**必要なときに名指しで出す**。
    """
    validation = cycle.get("validation") or {}
    codes = [str(item) for item in (validation.get("missing") or [])]
    due: List[str] = []
    for label, timeframe, filename in HTF_FRAMES:
        hit = next((c for c in codes
                    if c == f"{label}_MISSING" or c.startswith(f"{label}_STALE_")), None)
        if not hit:
            continue
        if hit.endswith("_MISSING"):
            why = "未取得"
        else:
            try:
                age = int(hit.rsplit("_", 1)[-1].rstrip("S"))
                why = f"{age // 3600}h{age % 3600 // 60:02d}m 経過"
            except ValueError:
                why = hit
        due.append(f"{timeframe:>3} → {filename}（{why}）")
    return due


def _blocked_reasons(cycle: Dict[str, Any]) -> List[str]:
    validation = cycle.get("validation") or {}
    reasons: List[str] = []
    reasons += [str(item) for item in (validation.get("blocking") or [])]
    if not reasons:
        preflight = cycle.get("publishPreflight") or {}
        if preflight.get("reason"):
            reasons.append(str(preflight["reason"]))
    missing = [str(item) for item in (validation.get("missing") or [])]
    if missing:
        reasons.append("missing=" + ",".join(missing[:6]))
    return reasons or ["unspecified"]


# ------------------------------------------------------------------ ビーコン

def send_beacon(status: str, reason: str = "") -> None:
    """ループ生死の別便(「監視の監視」)。

    BLOCKED / HALT のサイクルは market・scenario を publish しないため、
    Mini App には沈黙しか見えない。ここで理由込みの軽量ビーコンを送る。
    **失敗してもサイクルの結果(終了コード)は絶対に変えない。**
    """
    collapsed = " ".join(str(reason or "").split())[:300] or None
    kill = None
    try:
        import autotrade_arm
        kill = bool(autotrade_arm.state().get("kill"))
    except Exception:  # noqa: BLE001 — 表示専用。取れなければ省く
        pass
    try:
        import nqx_state
        ok, detail = nqx_state.publish_cycle_beacon(status, reason=collapsed, kill=kill)
        if not ok:
            why = (detail or {}).get("reason", "unknown") if isinstance(detail, dict) else detail
            print(f"  beacon: not sent ({str(why)[:120]})")
    except Exception as exc:  # noqa: BLE001
        print(f"  beacon: not sent ({type(exc).__name__})")


# ------------------------------------------------------------------ CLI

def _settle_journal() -> List[str]:
    """R85: 窓閉じ後に一度だけ戦績を記録する(最後に publish した bundle の足で)。"""
    try:
        bundle = json.loads(PUBLISHED_STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        bundle = {}
    import monitor_publish
    return list(monitor_publish._record_closed_trades(bundle if isinstance(bundle, dict) else {})
                or [])


def settle_after_window(now: Optional[datetime] = None, *,
                        state_path: Optional[Path] = None,
                        sync: Optional[Any] = None,
                        journal: Optional[Any] = None) -> Optional[str]:
    """窓が閉じた後、**一度だけ**建玉の正本を画面へ反映する(R81)。

    窓外サイクルは1行出して return するので、窓の終わり際に決済された建玉は
    `OPEN -> CLOSED` を publish されないまま翌 07:00 まで残る。2026-09-12 05:42:49 に
    決済した SHORT がこれで、05:45 の窓閉じをまたいで Mini App に存在しない建玉が
    出ていた(05:42 サイクルはまだ `hold: managed`、次の 05:45 は窓外で即 return)。

    **読み取りと状態 publish だけ**を行う。評価も発注もしない(§6.1 の「窓の外では
    データ取得・評価・発注を行わない」はそのまま守る —— ここで触るのはブローカーの
    建玉照会と画面状態の一致だけで、相場データも武装も見ない)。

    1 窓につき 1 回で足りるので JST の日付を印にする。失敗した周期は印を残さず、
    次の窓外サイクルが再試行する。

    R85: **戦績(trade_journal)も同じ一度で記録する。** CrossTrade の `/fills` は
    当取引日しか返さない(JST 06:00 前後で閉じる)ので、窓の終わり際に閉じた往復は
    ここで拾わないと、翌 07:00 には決済の約定が取れず「まだ建っている」まま取り残される。
    2026-09-12 05:42:49 の SHORT 10 がこれで、記録は手動の `--backfill` で入ったが
    ローカルの約定計数は `-2` のまま残った。読み取りと result の publish だけで、
    発注はしない。`sync` を注入したテストでは、注入されない限り走らせない。
    """
    run_journal = journal
    if run_journal is None and sync is None:
        run_journal = _settle_journal
    now = now or datetime.now(JST)
    state_path = state_path or WINDOW_SETTLE_STATE
    today = now.strftime("%Y-%m-%d")
    try:
        done = json.loads(state_path.read_text(encoding="utf-8")).get("date")
    except (OSError, ValueError):
        done = None
    if done == today:
        return None
    if sync is None:
        try:
            import nqx_state
        except ImportError as exc:                # noqa: BLE001 — 停止理由にしない
            return f"window settle unavailable: {exc}"
        sync = nqx_state.sync_position
    try:
        ok, detail = sync()
    except Exception as exc:                      # noqa: BLE001 — 監視を止めない
        return f"window settle failed: {type(exc).__name__}: {exc}"
    if not ok:
        return f"window settle failed: {str(detail)[:120]}"
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps({"date": today}), encoding="utf-8")
    except OSError as exc:
        return f"window settle wrote nothing: {exc}"
    moves = detail.get("transitions") if isinstance(detail, dict) else None
    closed = [row for row in (moves or [])
              if isinstance(row, dict) and row.get("to") == "CLOSED"]
    note = "window settle: position CLOSED" if closed else "window settle: position ok"
    if run_journal is not None:
        try:
            journal_notes = [str(value) for value in (run_journal() or [])]
        except Exception as exc:                  # noqa: BLE001 — 記録は監視を止めない
            journal_notes = [f"trade journal failed: {type(exc).__name__}: {exc}"]
        recorded = [value for value in journal_notes if "→ LEDGER" in value]
        if recorded:
            note += f" · journal recorded {len(recorded)}"
        elif journal_notes:
            note += " · " + journal_notes[0][:120]
    return note


def run_cycle(*, dry: bool, force_window: bool, replay: bool,
              cvd_retry: Optional[bytes] = None, fetch: bool = True) -> int:
    import autotrade_arm  # 遅延 import（表示のためだけに読む）

    if not force_window and not in_window():
        note = settle_after_window()
        line = f"[{datetime.now(JST):%H:%M}] 監視窓外（JST 07:00〜翌05:45）— 停止中"
        print(line + (f" | {note}" if note else ""))
        return 0

    problem = _preflight_config()
    if problem:
        print(f"HALT: {problem}")
        send_beacon("HALT", problem)
        return 2

    print(f"autotrade: {autotrade_arm.summary()}")
    # R102: 取引限月の正本(execution_contract.json.contract)。満期までの日数と新規可否を毎周期
    # 1 行で見せ、NQX_SYMBOL を持つ設定(env / wrangler.toml / monitor_config.json)が正本と
    # 食い違えば相場データを取る前に HALT する(チャートと発注先が別限月のまま走らせない)。
    import contract as contract_month
    print(contract_month.summary_line())
    try:
        disagree = contract_month.enforce_sites()
    except contract_month.ContractError as exc:
        disagree = f"CONTRACT_INVALID: {exc}"
    if disagree:
        print(f"HALT: {disagree}")
        send_beacon("HALT", disagree)
        return 2
    # R78: 約定監視(fill_watch.py)の生死。止まっていても周期は続けるが、TP1 検知が
    # 3 分遅れに戻っていることを毎周期 1 行で見せる。
    # R91: 契約 fillWatch.autostart=true なら、止まっている fill_watch をここで起動し直す
    # (単一インスタンス。起動の失敗で周期は止めない)。
    try:
        import fill_watch
        print(fill_watch.supervise())
    except Exception as exc:  # noqa: BLE001 - 表示専用
        print(f"fill_watch: status unavailable ({type(exc).__name__})")

    if cvd_retry is not None:
        # CVD 再取得だけを 1 度投入して、同じ pipeline をもう一度回す。
        # 価格・足・VP・range・SMT・event はこの経路で書き換えない。
        ok, detail = stage_ingest("cvd-retry", cvd_retry)
        if not ok:
            print(f"HALT: cvd-retry ingest failed — {detail}")
            send_beacon("HALT", f"cvd-retry ingest failed — {detail}")
            return 2
        print(f"  cvd-retry ingested: {detail}")
    else:
        if fetch:
            ok, detail = stage_fetch()
            if not ok:
                print(f"BLOCKED: fetch — {detail}")
                send_beacon("BLOCKED", f"fetch — {detail}")
                return 1
            if detail:
                print(f"  fetch: {detail.splitlines()[-1]}")
        ok, detail = stage_acquire(replay=replay)
        if not ok:
            print(f"BLOCKED: acquisition — {detail}")
            send_beacon("BLOCKED", f"acquisition — {detail}")
            return 1
        for line in detail.splitlines():
            print(f"  {line}")
        ok, detail = stage_ingest("snapshot", TV_BUNDLE.read_bytes())
        if not ok:
            print(f"HALT: snapshot ingest failed — {detail}")
            send_beacon("HALT", f"snapshot ingest failed — {detail}")
            return 2
        print(f"  ingest receipt: {detail}")

    status, cycle, detail = stage_pipeline()
    if status == "ERROR":
        print(f"HALT: pipeline — {detail}")
        send_beacon("HALT", f"pipeline — {detail}")
        return 2

    # 上位足は取りに行かない限り埋まらない。BLOCKED でも READY でも出す。
    due = htf_due(cycle)
    if due:
        print("  HTF 未取得/期限切れ — pane 0 を一時的に切り替えて取得し、必ず 15 へ戻す:")
        for line in due:
            print(f"    {line}")

    if status != "READY":
        reasons = "; ".join(_blocked_reasons(cycle))
        print(report_line(cycle, False, f"BLOCKED {reasons}"))
        send_beacon("BLOCKED", reasons)
        cvd = cycle.get("cvd") or {}
        if cvd.get("refreshRequired") and int(cvd.get("attempts") or 1) < 2:
            print("  next: CVD を1度だけ取り直して "
                  "`$cvdRetryJson | python nqx_cycle.py --cvd-retry`")
        return 1

    cvd = cycle.get("cvd") or {}
    if cvd.get("refreshRequired") and int(cvd.get("attempts") or 1) < 2:
        print("  note: CVD refreshRequired — A+ は A へ上限化される")

    if dry:
        print(report_line(cycle, False, "DRY（publish 未実行）"))
        return 0

    published, publish_detail, published_state = stage_publish()
    for line in publish_detail.splitlines():
        print(f"  {line}")
    audit = save_audit()
    if audit:
        print(f"  audit: {audit.name}")
    if not published:
        print(report_line(cycle, False, "HALT publish failed"))
        send_beacon("HALT", f"publish failed — {publish_detail.splitlines()[-1] if publish_detail else ''}")
        return 2
    halted = ("AUTOTRADE HALT" in publish_detail
              or "UNVERIFIED" in publish_detail
              or "server state: UNAVAILABLE" in publish_detail)
    # 成功時もビーコンを送り、前サイクルの BLOCKED/HALT 表示をアプリから消す。
    send_beacon("HALT" if halted else "PUBLISHED",
                "AUTOTRADE HALT or UNVERIFIED in publish output" if halted else None)
    # 報告行は載った状態(ゲート後)で書く。落とした理由は publish 出力から添える。
    demoted = publish_demotion_notes(publish_detail)
    tail = " ".join(x for x in ("HALT" if halted else "",
                                f"demoted: {demoted[0]}" if demoted else "") if x)
    print(report_line(cycle, True, tail, published_state=published_state))
    return 2 if halted else 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="R13 監視サイクルを1コマンドで回す")
    parser.add_argument("--dry", action="store_true",
                        help="publish 直前で止める（送信も通知もしない）")
    parser.add_argument("--force-window", action="store_true",
                        help="JST 監視窓の外でも実行する（配線確認用）")
    parser.add_argument("--replay-now-from-bars", action="store_true",
                        help="**配線検証専用。** 最終足の close を現在時刻とみなす")
    parser.add_argument("--cvd-retry", action="store_true",
                        help="標準入力の CVD 再取得 JSON だけを投入して再評価する")
    parser.add_argument("--no-fetch", action="store_true",
                        help="TradingView から raw を取り直さず、今ある raw で回す")
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.replay_now_from_bars and not args.dry:
        print("HALT: --replay-now-from-bars は --dry でしか使えません"
              "（古い価格で発注しうる）")
        return 2
    retry = sys.stdin.buffer.read() if args.cvd_retry else None
    try:
        return run_cycle(dry=args.dry, force_window=args.force_window,
                         replay=args.replay_now_from_bars, cvd_retry=retry,
                         fetch=not args.no_fetch)
    except subprocess.TimeoutExpired as exc:
        print(f"HALT: {exc.cmd} timed out after {exc.timeout}s — 再送しないこと")
        send_beacon("HALT", f"{exc.cmd} timed out after {exc.timeout}s")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

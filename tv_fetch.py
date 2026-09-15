#!/usr/bin/env python3
"""TradingView の raw 取得を、エージェントの転記から完全に外す（R51）。

R39 で「段階を手で並べない」を `nqx_cycle.py` に固めた後も、**取得だけは
エージェントの仕事のまま**だった。MCP の応答をエージェントが読み、同じ JSON を
`.secrets/tv_raw/*.json` へ書き戻す。この転記が 2 つの実害を出していた。

  * **速度** — bars3m 240本 + bars15m 60本の逐語出力だけで数分かかる。
    必須 8 raw の鮮度窓は 240 秒（`tv_snapshot.RAW_POLICY`）なので、
    書き終わる頃には先に書いた raw が期限切れになる。2026-09-01 の実測で
    `required raw acquisition is not fresh` が 2 回連続で出た。
    3分間隔の監視ループなのに 1 周が 20 分かかっていた。
  * **忠実性** — 転記は要約・省略・書き間違いの入口そのもので、実サイクル
    403 本で `rangeAnchor` / `peers` / `po3` / `cvdMeta` の取得率を 0% に
    していた張本人（`docs/TV_ACQUISITION_LOOP.md` 冒頭）。

MCP サーバ（`tradingview-mcp`）には **同じ core を呼ぶ CLI が同梱**されており、
`console.log(JSON.stringify(result, null, 2))` で MCP ツールと同一の JSON を
stdout に出す。つまり Python から CLI を叩いて **stdout をそのままファイルへ
落とせば**、転記工程は存在しなくなる。エージェントの担当は
`python nqx_cycle.py` を 1 回叩くことだけになる。

    python tv_fetch.py                  # 固定順序で 1 周ぶん取得する
    python tv_fetch.py --cvd-only       # CVD 関連 raw だけ取り直す
    python tv_fetch.py --raw-dir DIR    # 保存先を変える（既定 .secrets/tv_raw）

終了コード: 0=取得完了 / 1=取得不良（そのサイクルは BLOCKED にする）。

**この経路は注文・ネットワーク・reconcile を一切呼ばない。** 読み取りと、
pane 0 の時間足を HTF 取得のために一時変更して 15 へ戻す操作だけを行う。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import contract as contract_month  # R102: 取引限月の正本(連続足 MNQ1! を拒否する)

BASE = Path(__file__).resolve().parent
RAW_DIR_DEFAULT = BASE / ".secrets" / "tv_raw"

#: MCP サーバ同梱 CLI。環境で差し替えられるようにしておく（再インストールで
#: パスが変わる。見つからないまま黙って前サイクルの raw を再利用させない）。
CLI_JS = Path(os.environ.get(
    "NQX_TV_CLI", r"C:\Users\exexu\tradingview-mcp\src\cli\index.js"))

#: 固定画面の不変条件（`docs/R43_ANALYSIS_CONTEXT.md`「固定画面」）。
#: R102: 銘柄は「MNQ を含む」ではなく **発注先の限月そのもの**(contract.tv_symbol())で
#: 検査する。連続足 CME_MINI:MNQ1! は 2026-09-15 に 12 月限へロールし、9 月限へ 285pt 上の
#: 通過指値が飛んだ。EXPECTED_SYMBOL_PART は互換のため残すが検査には使わない。
EXPECTED_SYMBOL_PART = "MNQ"
PANE_CONTEXT, PANE_EXECUTION = 0, 1
STEP_15M, STEP_3M = 900, 180

#: HTF は期限駆動。`htf_context.py --raw-dir` の `due` に出た frame だけ取る。
HTF_FRAMES: Dict[str, Tuple[str, str]] = {
    "45m": ("45", "bars45m.json"),
    "1h": ("60", "bars1h.json"),
    "4h": ("240", "bars4h.json"),
    "1d": ("1D", "bars1d.json"),
}

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass


class AcquisitionError(RuntimeError):
    """取得が契約を満たさなかった。**部分的に書いたまま先へ進ませない。**"""


# ------------------------------------------------------------------ CLI 実行

def _node() -> str:
    exe = os.environ.get("NQX_NODE") or shutil.which("node")
    if not exe:
        raise AcquisitionError("node が PATH に無い（NQX_NODE で明示指定できる）")
    return exe


def _cli(args: Sequence[str], timeout: int = 90) -> bytes:
    """CLI を 1 回叩き、stdout を **バイト列のまま** 返す。

    decode してから encode し直すと Windows のコードページを経由して
    化ける（publish 経路で実際に起きた）。raw は逐語保存が要件なので、
    ここでは一切触らない。
    """
    if not CLI_JS.is_file():
        raise AcquisitionError(f"TradingView CLI が見つからない: {CLI_JS}")
    try:
        proc = subprocess.run([_node(), str(CLI_JS), *args],
                              capture_output=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise AcquisitionError(f"tv {' '.join(args)} が {exc.timeout}s で応答なし") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or b"").decode("utf-8", "replace").strip()
        raise AcquisitionError(f"tv {' '.join(args)} 失敗 (exit {proc.returncode}): "
                               f"{detail[:300]}")
    return proc.stdout or b""


def _cli_json(args: Sequence[str], timeout: int = 90) -> Tuple[bytes, Dict[str, Any]]:
    """stdout（逐語）と、検査用にパースした dict を返す。

    保存するのは **常に前者**。パースは「success を確認する」ためだけに使い、
    整形した結果を書き戻さない。
    """
    raw = _cli(args, timeout=timeout)
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise AcquisitionError(f"tv {' '.join(args)} の出力が JSON ではない: {exc}") from exc
    if not isinstance(parsed, dict) or parsed.get("success") is not True:
        raise AcquisitionError(f"tv {' '.join(args)} が success=true を返さなかった")
    return raw, parsed


# ------------------------------------------------------------------ 保存

def _write(raw_dir: Path, name: str, payload: bytes) -> None:
    """原子的に置き換える。書きかけの raw を受領書に拾わせない。"""
    raw_dir.mkdir(parents=True, exist_ok=True)
    tmp = raw_dir / f".{name}.tmp"
    tmp.write_bytes(payload)
    os.replace(tmp, raw_dir / name)


# ------------------------------------------------------------------ 検査

def _dominant_step(parsed: Dict[str, Any]) -> Optional[int]:
    """足の時間幅は **bar の time 差の最頻値**でしか確かめられない。

    `chart_get_state.resolution` も `pane_list` も嘘をつく（2026-08-25 実測。
    resolution が 3/15/3/45 と揺れる間、`ohlcv` は 900 秒足を返し続けた）。
    """
    bars = parsed.get("bars")
    if not isinstance(bars, list) or len(bars) < 3:
        return None
    times = [int(b["time"]) for b in bars if isinstance(b, dict) and "time" in b]
    deltas = [b - a for a, b in zip(times, times[1:]) if b > a]
    if not deltas:
        return None
    return Counter(deltas).most_common(1)[0][0]


#: R102b: 連続足(MNQ1!)が今どの限月を指しているか。TradingView の datafeed が返す
#: symbolInfo(mainSeries().symbolInfo())の front_contract を毎周期読む。symbolExt() には無い。
SYMBOL_INFO_EXPR = (
    "(function(){var w=window.TradingViewApi._activeChartWidgetWV.value();"
    "var m=(w.model&&w.model())||(w._chartWidget&&w._chartWidget.model());"
    "var si=(m.mainSeries().symbolInfo())||{};"
    "var keys=['name','full_name','pro_name','description','root','front_contract','expiration',"
    "'typespecs','type','exchange'];var out={};"
    "keys.forEach(function(k){if(si[k]!==undefined)out[k]=si[k];});return JSON.stringify(out);})()"
)


def chart_symbol_info(timeout: int = 30) -> Dict[str, Any]:
    """アクティブチャートの銘柄情報(``front_contract`` を含む)。取れなければ ``{}``。"""
    try:
        _raw, parsed = _cli_json(["ui", "eval", SYMBOL_INFO_EXPR], timeout=timeout)
    except AcquisitionError:
        return {}
    result = parsed.get("result")
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except ValueError:
            return {}
    return result if isinstance(result, dict) else {}


def _write_symbol_info(raw_dir: Path, filename: str) -> Dict[str, Any]:
    """銘柄情報を raw に逐語保存する。取れなかった周期は**古いファイルを消す**
    (前周期の front_contract をロール後に再利用させない)。"""
    info = chart_symbol_info()
    if info:
        payload = {"success": True, "fetchedAt": time.time(), **info}
        _write(raw_dir, filename, json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    else:
        try:
            (raw_dir / filename).unlink()
        except FileNotFoundError:
            pass
    return info


def _require_window(parsed: Dict[str, Any], expected_resolution: str, where: str,
                    front_contract: Optional[str] = None) -> None:
    symbol = str(parsed.get("symbol") or "")
    if contract_month.is_continuous(symbol) and not front_contract:
        # 連続足は「今どの限月か」を添えないと判定できない。呼び出し側が持っていなければここで読む。
        front_contract = str(chart_symbol_info().get("front_contract") or "") or None
    ok, reason = contract_month.chart_symbol_matches(symbol, front_contract)
    if not ok:
        raise AcquisitionError(f"{where}: {reason}")
    resolution = str(parsed.get("resolution") or "")
    if resolution != expected_resolution:
        raise AcquisitionError(
            f"{where}: resolution={resolution}（期待 {expected_resolution}）")


def _fetch_bars(raw_dir: Path, name: str, count: int, step: int,
                timeframe: str, *, repair: bool) -> None:
    """OHLCV を取り、**足の幅が一致したときだけ**保存する。

    一致しなければ `timeframe` を張り直して 1 度だけ取り直す。ここで
    fail-closed にしておかないと、間違った足の raw が保存され、
    `tv_snapshot.normalize_bars` が後段で落とすまで気付けない。
    """
    args = ["ohlcv", "-n", str(count)]
    raw, parsed = _cli_json(args)
    observed = _dominant_step(parsed)
    if observed != step and repair:
        _cli_json(["timeframe", timeframe])
        time.sleep(1.5)
        raw, parsed = _cli_json(args)
        observed = _dominant_step(parsed)
    if observed != step:
        raise AcquisitionError(
            f"{name}: 足の間隔が {observed} 秒（期待 {step} 秒）— 保存しない")
    _write(raw_dir, name, raw)


# ------------------------------------------------------------------ 段階

def _htf_due(raw_dir: Path) -> List[str]:
    """期限駆動。目視の時刻で全 frame を毎回取り直さない。"""
    proc = subprocess.run([sys.executable, str(BASE / "htf_context.py"),
                           "--raw-dir", str(raw_dir)],
                          cwd=str(BASE), capture_output=True, check=False, timeout=120)
    if proc.returncode != 0:
        return []
    try:
        payload = json.loads((proc.stdout or b"").decode("utf-8", "replace"))
    except ValueError:
        return []
    return [str(x) for x in (payload.get("due") or []) if str(x) in HTF_FRAMES]


def _fetch_context_15m(raw_dir: Path) -> None:
    """左 15m。構造・アンカーを先に固定する（3m の方向へ後付けしない）。"""
    _cli_json(["pane", "focus", str(PANE_CONTEXT)])
    raw, parsed = _cli_json(["state"])
    info = _write_symbol_info(raw_dir, "symbol_info_15m.json")
    _require_window(parsed, "15", "pane 0", str(info.get("front_contract") or "") or None)
    _write(raw_dir, "chart_state_15m.json", raw)

    _fetch_bars(raw_dir, "bars15m.json", 60, STEP_15M, "15", repair=True)

    raw, _ = _cli_json(["values"])
    _write(raw_dir, "study_15m.json", raw)

    # VP セッションラベルと SMT は **pane 0 の study**。pane 1 には
    # CVD Unified しか無いため、pane 1 で読むと study_count=0 が返り、
    # 必須の pine_labels.json が空になってサイクルごと BLOCKED になる
    # （2026-09-01 実測）。ここで取るのが正しい。
    raw, _ = _cli_json(["data", "labels", "-f", "Sessions", "-n", "100"])
    _write(raw_dir, "pine_labels.json", raw)

    raw, _ = _cli_json(["data", "lines", "-f", "SMT", "-v"])
    _write(raw_dir, "smt_lines.json", raw)

    raw, _ = _cli_json(["data", "labels", "-f", "SMT", "-n", "4"])
    _write(raw_dir, "smt_labels.json", raw)


def _fetch_htf(raw_dir: Path, due: Sequence[str]) -> List[str]:
    """due の frame だけ pane 0 で取り、**必ず 15 分へ戻す**。

    途中で失敗しても復元を先に行う。復元しないと次サイクルの pane 契約が
    壊れ、以後ずっと BLOCKED になる（2026-08-26 に実際にやった）。
    """
    done: List[str] = []
    try:
        for key in due:
            timeframe, filename = HTF_FRAMES[key]
            _cli_json(["timeframe", timeframe])
            time.sleep(1.5)
            _, state = _cli_json(["state"])
            _require_window(state, timeframe, f"HTF {key}")
            raw, parsed = _cli_json(["ohlcv", "-n", "80"])
            if _dominant_step(parsed) is None:
                raise AcquisitionError(f"HTF {key}: 足が取得できない")
            _write(raw_dir, filename, raw)
            done.append(key)
    finally:
        _cli_json(["timeframe", "15"])
        time.sleep(1.5)
        raw, state = _cli_json(["state"])
        _require_window(state, "15", "pane 0 復元")
        _write(raw_dir, "chart_state_15m.json", raw)
    return done


def _fetch_execution_3m(raw_dir: Path) -> None:
    """右 3m とその中の CVD study 領域。CVD は第三 pane ではない。"""
    _cli_json(["pane", "focus", str(PANE_EXECUTION)])
    raw, parsed = _cli_json(["state"])
    info = _write_symbol_info(raw_dir, "symbol_info.json")
    _require_window(parsed, "3", "pane 1", str(info.get("front_contract") or "") or None)
    names = [str((s or {}).get("name") or "") for s in (parsed.get("studies") or [])]
    if not any("CVD" in n for n in names):
        raise AcquisitionError("pane 1 に CVD Unified が見えない")
    _write(raw_dir, "chart_state.json", raw)

    _fetch_bars(raw_dir, "bars3m.json", 240, STEP_3M, "3", repair=True)

    _fetch_cvd(raw_dir)

    raw, _ = _cli_json(["quote"])
    _write(raw_dir, "quote.json", raw)


def _fetch_cvd(raw_dir: Path) -> None:
    """CVD 関連だけ。再取得経路（--cvd-only）からも同じ関数を使う。

    価格・足・VP・range・SMT・event はこの経路で書き換えない（R43）。
    """
    raw, _ = _cli_json(["values"])
    _write(raw_dir, "study_3m.json", raw)
    raw, _ = _cli_json(["data", "tables", "-f", "CVD"])
    _write(raw_dir, "cvd_table.json", raw)


# ------------------------------------------------------------------ 入口

def fetch_cycle(raw_dir: Path = RAW_DIR_DEFAULT, *,
                skip_htf: bool = False) -> Dict[str, Any]:
    """固定順序で 1 周ぶん取得する。

    画面確認 → 左15m構造 → due HTF → 左15m復元 → 右3m実行足 → CVD →
    左15mへ focus 復元。
    """
    raw, layout = _cli_json(["pane", "list"])
    panes = layout.get("panes") or []
    if len(panes) != 2:
        raise AcquisitionError(f"pane 数が 2 ではない（{len(panes)}）")
    for index, resolution in ((PANE_CONTEXT, "15"), (PANE_EXECUTION, "3")):
        pane = next((p for p in panes if int(p.get("index", -1)) == index), None)
        if pane is None:
            raise AcquisitionError(f"pane {index} が無い")
        ok, reason = contract_month.chart_symbol_plausible(pane.get("symbol"))
        if not ok:
            raise AcquisitionError(f"pane {index}: {reason}")
        if str(pane.get("resolution") or "") != resolution:
            raise AcquisitionError(
                f"pane {index} の時間足が {pane.get('resolution')}（期待 {resolution}）")
    _write(raw_dir, "pane_layout.json", raw)

    _fetch_context_15m(raw_dir)

    due = [] if skip_htf else _htf_due(raw_dir)
    fetched = _fetch_htf(raw_dir, due) if due else []

    _fetch_execution_3m(raw_dir)

    # 最後は必ず左 15m へ focus を戻す（次サイクルの起点を固定する）。
    _cli_json(["pane", "focus", str(PANE_CONTEXT)])
    return {"schema": "NQX_TV_FETCH/1", "htfFetched": fetched, "rawDir": str(raw_dir)}


def fetch_cvd_only(raw_dir: Path = RAW_DIR_DEFAULT) -> Dict[str, Any]:
    _cli_json(["pane", "focus", str(PANE_EXECUTION)])
    _, parsed = _cli_json(["state"])
    _require_window(parsed, "3", "pane 1")
    _fetch_cvd(raw_dir)
    _cli_json(["pane", "focus", str(PANE_CONTEXT)])
    return {"schema": "NQX_TV_FETCH/1", "cvdOnly": True, "rawDir": str(raw_dir)}


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="TradingView raw を CLI 経由で逐語取得する（転記を挟まない）")
    parser.add_argument("--raw-dir", default=str(RAW_DIR_DEFAULT))
    parser.add_argument("--cvd-only", action="store_true",
                        help="CVD 関連 raw だけ取り直す（他は書き換えない）")
    parser.add_argument("--skip-htf", action="store_true",
                        help="期限切れでも HTF を取りに行かない")
    args = parser.parse_args(list(argv) if argv is not None else None)
    raw_dir = Path(args.raw_dir)
    try:
        result = (fetch_cvd_only(raw_dir) if args.cvd_only
                  else fetch_cycle(raw_dir, skip_htf=args.skip_htf))
    except AcquisitionError as exc:
        print(f"FETCH BLOCKED: {exc}")
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

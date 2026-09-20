# -*- coding: utf-8 -*-
"""R118: 1 サイクルの段階別の所要を測る。**読むだけ・判断しない。**

「145 秒かかる」は分かっていたが、**どこに何秒**かは誰も測っていなかった。観測の
16 秒を引いた残りを「TradingView 取得・評価・publish」と一括で説明するのは推測で、
指示 §5 が禁じている。ここは事実を出すためだけにある。

使い方(`nqx_cycle` / `monitor_publish` が呼ぶ):

    import cycle_timing
    with cycle_timing.phase("fetch"):
        ...
    print(cycle_timing.line())
    cycle_timing.dump()

子プロセス(`monitor_publish`)の内訳は、親が拾えるように 1 行で標準出力へ出す
(`NQX_TIMING ` の印)。親はその行を自分の表へ畳む。
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import time
from typing import Any, Dict, List, Optional

BASE = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(BASE, ".secrets", "cycle_timing.jsonl")
MARKER = "NQX_TIMING "

_PHASES: List[Dict[str, Any]] = []
_STARTED = time.monotonic()


def reset() -> None:
    global _STARTED
    _PHASES.clear()
    _STARTED = time.monotonic()


@contextlib.contextmanager
def phase(name: str, **extra):
    """1 段階を測る。**例外が出ても記録する**(落ちた段階こそ知りたい)。"""
    started = time.monotonic()
    error = None
    try:
        yield
    except BaseException as exc:  # noqa: BLE001 — 記録してから投げ直す
        error = f"{type(exc).__name__}"
        raise
    finally:
        record(name, time.monotonic() - started, error=error, **extra)


def record(name: str, seconds: float, *, error: Optional[str] = None, **extra) -> None:
    row = {"name": str(name), "seconds": round(float(seconds), 3)}
    if error:
        row["error"] = error
    row.update({key: value for key, value in extra.items() if value is not None})
    _PHASES.append(row)


def absorb(text: str) -> int:
    """子プロセスの出力から `NQX_TIMING` 行を拾って自分の表へ足す。"""
    taken = 0
    for raw in str(text or "").splitlines():
        stripped = raw.strip()
        if not stripped.startswith(MARKER):
            continue
        try:
            rows = json.loads(stripped[len(MARKER):])
        except ValueError:
            continue
        if isinstance(rows, list):
            for row in rows:
                if isinstance(row, dict) and row.get("name"):
                    _PHASES.append(row)
                    taken += 1
    return taken


def phases() -> List[Dict[str, Any]]:
    return list(_PHASES)


def total() -> float:
    return time.monotonic() - _STARTED


def line(prefix: str = "timing") -> str:
    """人が読む 1 行。長い順ではなく **実行順**(どこで詰まったかが分かる)。"""
    if not _PHASES:
        return f"{prefix}: (no phases recorded)"
    parts = []
    for row in _PHASES:
        label = row["name"]
        if row.get("error"):
            label += f"!{row['error']}"
        parts.append(f"{label} {row['seconds']:.1f}s")
    return f"{prefix}: {total():.1f}s = " + " + ".join(parts)


def emit() -> str:
    """子プロセスが親へ渡す 1 行。"""
    return MARKER + json.dumps(_PHASES, ensure_ascii=False, separators=(",", ":"))


def _writing_disabled() -> bool:
    """本番の記録へ書かない場面か。

    テストは `nqx_cycle.run_cycle` を段階を差し替えて呼ぶので、そのままだと
    **本番の `.secrets/cycle_timing.jsonl` が試験の 0.1 秒の周期で埋まる**
    (2026-09-19、16 件溜まっていて集計が読めなくなった)。`tests/run_all.py` が
    環境変数で止め、個別実行も argv で見分ける。
    """
    switch = str(os.environ.get("NQX_CYCLE_TIMING") or "").strip().lower()
    if switch in ("0", "off", "false", "no"):
        return True
    import sys
    entry = os.path.abspath(str(sys.argv[0] or ""))
    return (os.sep + "tests" + os.sep) in entry


def writing_disabled() -> bool:
    """本番の記録へ書かない場面か(`broker_source` の影運転の記録も同じ判定を使う)。"""
    return _writing_disabled()


def dump(path: Optional[str] = None, **extra) -> Optional[str]:
    """追記する。失敗しても周期は止めない。"""
    if path is None and _writing_disabled():
        return None
    target = path or os.environ.get("NQX_CYCLE_TIMING_LOG") or LOG
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with io.open(target, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"at": time.time(), "totalSec": round(total(), 3),
                                 "phases": _PHASES, **extra},
                                ensure_ascii=False) + "\n")
        return target
    except OSError:
        return None


def report(path: Optional[str] = None, last: int = 20) -> str:
    """記録した周期をまとめて出す(読むだけ)。"""
    target = path or LOG
    rows = []
    try:
        with io.open(target, encoding="utf-8") as fh:
            for raw in fh:
                try:
                    rows.append(json.loads(raw))
                except ValueError:
                    continue
    except OSError:
        return f"{target} が読めない"
    rows = rows[-last:]
    if not rows:
        return "記録がまだ無い"
    names: List[str] = []
    for row in rows:
        for item in row.get("phases") or []:
            if item.get("name") not in names:
                names.append(item["name"])
    out = [f"直近 {len(rows)} 周期(秒)", ""]
    header = f"{'段階':<26}{'中央値':>9}{'最小':>9}{'最大':>9}{'割合':>9}"
    out.append(header)
    out.append("-" * 62)
    totals = sorted(row.get("totalSec") or 0.0 for row in rows)
    median_total = totals[len(totals) // 2] if totals else 0.0
    for name in names:
        values = sorted(item["seconds"] for row in rows
                        for item in (row.get("phases") or []) if item.get("name") == name)
        if not values:
            continue
        median = values[len(values) // 2]
        # 割合は「中央値どうしの比」なので、段階ごとに中央値を取る周期が違えば
        # 合計が 100% にならない。**丸めて辻褄を合わせない**が、桁は固定して
        # 表が崩れないようにする(以前 18584% が列を割っていた)。
        share = (median / median_total * 100.0) if median_total else 0.0
        share_text = f"{share:7.1f}%" if share < 1000 else f"{share:7.0f}%"
        out.append(f"{name:<26}{median:>9.1f}{values[0]:>9.1f}{values[-1]:>9.1f}{share_text:>9}")
    out.append("-" * 62)
    out.append(f"{'合計':<26}{median_total:>9.1f}{totals[0]:>9.1f}{totals[-1]:>9.1f}"
               f"{'100.0%':>9}")
    return "\n".join(out)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="R118 サイクルの段階別所要(読むだけ)")
    parser.add_argument("--last", type=int, default=20)
    parser.add_argument("--path", default=None)
    args = parser.parse_args()
    print(report(args.path, args.last))

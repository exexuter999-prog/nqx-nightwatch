# -*- coding: utf-8 -*-
"""nerf — 公開版(🩹 Nerf Edition / 補助輪版)の印。

このリポジトリに置いてある版は **意図的に弱体化** されています。`nqx_cycle.py` / `telegram_bot.py` を
プログラムとして起動すると `NQX_EXECUTION_CONTRACT=execution_contract.nerf.json` が立ち、その子
(pipeline / publish / engine / order.py / fill_watch)まで含めてナーフ契約で走ります。効くやつ(エッジ層)は
そこで OFF、フルパワー版は作者の PC の中だけで走っています。試験や道具として import した
モジュールは本来の `execution_contract.json` のままなので、公開版の試験は緑のままです。
このモジュールは「いまどれだけ弱いか」を数えて、周期の報告行と Telegram の先頭に印を付けるだけ。
**判定・発注・SL・枚数には一切触れません**(触っていたらナーフではなく改造です)。

    python nerf.py            # 出力: 🩹power 14% と、何が封印されているかの一覧
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Tuple

BASE = os.path.dirname(os.path.abspath(__file__))
NERF_CONTRACT = "execution_contract.nerf.json"

CONTACT = "https://github.com/exexuter999-prog"
TAG = "🩹"

#: (表示名, 契約のパス, 「効いている」とみなす値)。mode を持つ層は LIVE のときだけ効いているとみなす。
EDGE_LAYERS: List[Tuple[str, Tuple[str, ...]]] = [
    ("ICT STDV アンカー", ("ictStdv", "mode")),
    ("ICT STDV 目標", ("ictStdv", "targets", "mode")),
    ("構造文脈(親の仮説)", ("structureContext", "context", "mode")),
    ("参加判断", ("structureContext", "participation", "mode")),
    ("浅い代替候補", ("structureContext", "shallowCandidate", "mode")),
    ("選択層", ("structureContext", "selection", "mode")),
    ("近傍判断", ("structureContext", "nearTerm", "mode")),
    ("上位足の目標穴埋め", ("htfTargets", "mode")),
    ("押し目深度", ("entryDepth", "mode")),
    ("VWAP の SL 逃がし", ("stopLogic", "vwapClearance", "mode")),
    ("流動性プールの SL 逃がし", ("stopLogic", "poolClearance", "mode")),
    ("掃引ゲート", ("stopLogic", "sweepGate", "mode")),
    ("指値 SL 再検査", ("stopLogic", "restingStopRecheck", "mode")),
    ("BREAKER 起点 SL", ("stopLogic", "flipOrigin", "mode")),
]
MODELS = ("VP80_REVERSION", "TURTLE_SOUP_REVERSAL", "BREAKER_CONTINUATION", "OTE_FVG_PULLBACK")
GRADES = ("A+", "A", "B")


def _get(doc: Any, path: Tuple[str, ...]) -> Any:
    node = doc
    for key in path:
        node = node.get(key) if isinstance(node, dict) else None
    return node


def active() -> bool:
    """このプロセスがナーフ契約で走っているか(プログラムとして起動した nqx_cycle / telegram_bot と、その子)。"""
    return os.environ.get("NQX_EXECUTION_CONTRACT", "").endswith(NERF_CONTRACT)


def _contract() -> Dict[str, Any]:
    """走っている契約(ナーフ中はそれ)。走っていなければナーフ契約ファイルを読んで「なったときの姿」を出す。"""
    try:
        if active():
            import execution_contract
            doc = getattr(execution_contract, "CONTRACT", None)
            if isinstance(doc, dict):
                return doc
        with open(os.path.join(BASE, NERF_CONTRACT), encoding="utf-8") as fh:
            doc = json.load(fh)
        return doc if isinstance(doc, dict) else {}
    except Exception:  # noqa: BLE001 — 印のために周期を止めない
        return {}


def inventory(contract: Dict[str, Any] | None = None) -> List[Tuple[str, bool, str]]:
    """(名前, 効いているか, 状態の短い説明)の一覧。"""
    doc = contract if contract is not None else _contract()
    rows: List[Tuple[str, bool, str]] = []
    for label, path in EDGE_LAYERS:
        mode = str(_get(doc, path) or "OFF").upper()
        rows.append((label, mode == "LIVE", mode))
    disabled = {str(r.get("model")) for r in (_get(doc, ("modelGate", "disabled")) or []) if isinstance(r, dict)
                and str(r.get("variant") or "ALL").upper() == "ALL"}
    for model in MODELS:
        rows.append((f"モデル {model}", model not in disabled, "封印" if model in disabled else "稼働"))
    allowed = {str(g) for g in (_get(doc, ("scenario", "allowedGrades")) or [])}
    for grade in GRADES:
        rows.append((f"等級 {grade} の発注", grade in allowed, "可" if grade in allowed else "出禁"))
    pyramid = _get(doc, ("pyramid", "enabled")) is True and _get(doc, ("pyramid", "dryRun")) is not True
    rows.append(("追撃(ピラミッド)", pyramid, "可" if pyramid else "一発勝負"))
    return rows


def power_level(contract: Dict[str, Any] | None = None) -> Tuple[int, int, int]:
    """(効いている数, 全体, パーセント)。"""
    rows = inventory(contract)
    on = sum(1 for _label, active, _state in rows if active)
    total = max(1, len(rows))
    return on, total, int(round(100.0 * on / total))


def power_suffix(contract: Dict[str, Any] | None = None) -> str:
    """周期の報告行の末尾に付ける印。例: ``🩹power 14%``。"""
    _on, _total, pct = power_level(contract)
    return f"{TAG}power {pct}%"


def headline_tag() -> str:
    """Telegram 先頭行の頭に付ける印(1 文字 + 空白。バイト上限を圧迫しない)。"""
    return f"{TAG} "


def banner(contract: Dict[str, Any] | None = None) -> str:
    on, total, pct = power_level(contract)
    return (f"{TAG} NQ Nightwatch — Nerf Edition(補助輪版) power {pct}% ({on}/{total})\n"
            f"   効くやつは全部、作者の PC に置いてきました。フルパワー版が欲しい人は作者へ: {CONTACT}")


def main() -> int:
    print(banner())
    for label, on, state in inventory():
        print(f"   {'●' if on else '○'} {label}: {state}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

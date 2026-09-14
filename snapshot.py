#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
市況スナップショットの読み書き

Bot からは TradingView MCP が使えないため、PC側(Claude Code)が
分析のたびにここへ書き出し、Bot の /price はそれを読むだけにする。

書き込み(PC側 Claude Code が analyze のたびに実行):
  python snapshot.py --write --price 29584.75 --vwap 29597 \
      --vwap-lo 29560 --vwap-hi 29800 --cvd -4323 --note "下落継続"

読み出し(Bot の /price が使う):
  python snapshot.py
"""
import argparse, json, os, sys
from datetime import datetime

for _s in ("stdout", "stderr"):
    _f = getattr(sys, _s, None)
    if _f is not None and hasattr(_f, "reconfigure"):
        try:
            _f.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

BASE = os.path.dirname(os.path.abspath(__file__))
SNAP = os.path.join(BASE, ".secrets", "snapshot.json")


def _read_json_file(path, option):
    try:
        with open(path, encoding="utf-8") as f:
            value = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"ERROR: {option} could not be read: {exc}")
    if not isinstance(value, list):
        raise SystemExit(f"ERROR: {option} must point to a JSON array")
    return value


def write(a):
    data = {
        "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "price": a.price,
        "vwap": a.vwap,
        "vwap_lo": a.vwap_lo,
        "vwap_hi": a.vwap_hi,
        "cvd": a.cvd,
        "note": a.note,
    }
    if a.bars:
        data["bars"] = _read_json_file(a.bars, "--bars")
    if a.levels:
        data["levels"] = _read_json_file(a.levels, "--levels")
    with open(SNAP, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"記録: {data['at']} price={a.price}")


def read():
    if not os.path.exists(SNAP):
        return "市況データがありません(PC側でまだ分析していません)"

    with open(SNAP, encoding="utf-8") as f:
        d = json.load(f)

    # 古いデータを掴ませない。鮮度を必ず添える。
    age = ""
    age = ""
    try:
        t = datetime.strptime(d["at"], "%Y-%m-%d %H:%M:%S")
        mins = int((datetime.now() - t).total_seconds() / 60)
        if mins >= 10:
            age = f"  ※{mins}分前・古い可能性"
        else:
            age = f"  ({mins}分前)"
    except Exception:
        pass

    lines = [f"取得 {d['at']}{age}", ""]
    if d.get("price") is not None:
        lines.append(f"現在値   {d['price']:,.2f}")
    if d.get("vwap") is not None:
        lines.append(f"VWAP     {d['vwap']:,.2f}")
    if d.get("vwap_lo") is not None:
        lines.append(f"VWAP下限 {d['vwap_lo']:,.2f}")
    if d.get("vwap_hi") is not None:
        lines.append(f"VWAP上限 {d['vwap_hi']:,.2f}")
    if d.get("cvd") is not None:
        lines.append(f"CVD      {int(d['cvd']):,}")
    if d.get("note"):
        lines += ["", d["note"]]
    return "\n".join(lines)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--write", action="store_true")
    p.add_argument("--price", type=float)
    p.add_argument("--vwap", type=float)
    p.add_argument("--vwap-lo", type=float, dest="vwap_lo")
    p.add_argument("--vwap-hi", type=float, dest="vwap_hi")
    p.add_argument("--cvd", type=float)
    p.add_argument("--note", default="")
    p.add_argument("--bars", help="JSON file containing recent 3-minute bars")
    p.add_argument("--levels", help="JSON file containing chart levels")
    a = p.parse_args()

    if a.write:
        if a.price is None:
            sys.exit("ERROR: --price は必須です")
        write(a)
    else:
        print(read())


if __name__ == "__main__":
    main()

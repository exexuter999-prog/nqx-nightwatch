#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram へ通知を送る(監視側から呼ぶ用)

telegram_bot.py が常駐していなくても単体で動く。
Bot は「受け取って発注する」係、こちらは「送る」係。

使い方:
  python notify.py "本文"
  echo "本文" | python notify.py
  python notify.py --setup "SHORT" --entry 29800 --sl 29830 --tp 29750 --qty 2 \
                   --why "C:POC/VWAP 合流帯からの戻り売り"

--setup を使うと、Bot にそのまま貼れる /order コマンドを併記する。
"""
import argparse, os, sys

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

# 送信処理は telegram_bot と共有する(トークン管理を一本化)
from telegram_bot import load_env, send, esc, deck_head, scenario_keyboard


def build_setup(a):
    """エントリー提案を、価格が一目で読める形に整える。"""
    side = a.setup.upper()
    order_side = "sell" if side in ("SHORT", "SELL") else "buy"

    lines = [deck_head(f"{side} SETUP READY", "⚡"), ""]
    lines.append(f"Entry: <b>{a.entry:,.2f}</b>")
    lines.append(f"SL:    <b>{a.sl:,.2f}</b>")
    if a.tp:
        lines.append(f"TP:    <b>{a.tp:,.2f}</b>")
    lines.append("")

    # MNQ 1pt = $2.00
    risk_pt = abs(a.entry - a.sl)
    risk = risk_pt * a.qty * 2.00
    body = f"リスク ${risk:.2f}({risk_pt:g}pt × {a.qty}枚)"
    if a.tp:
        rew_pt = abs(a.tp - a.entry)
        rew = rew_pt * a.qty * 2.00
        body += f" / R:R 1:{rew/risk:.2f}"
    lines.append(body)

    if a.why:
        lines.append(f"根拠: {esc(a.why)}")

    lines += ["", "<b>⟦ NEXT ⟧</b>",
              "下の PREVIEW ボタンでドライランを開始できます。",
              "コマンドのコピー＆貼り付けは不要です。"]
    return "\n".join(lines)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("text", nargs="*", help="本文(省略時は標準入力)")
    p.add_argument("--setup", metavar="LONG|SHORT", help="エントリー提案として整形")
    p.add_argument("--entry", type=float)
    p.add_argument("--sl", type=float)
    p.add_argument("--tp", type=float)
    p.add_argument("--qty", type=int, default=2)
    p.add_argument("--why", default="", help="根拠(1行)")
    a = p.parse_args()

    cfg = load_env()

    if a.setup:
        if a.entry is None or a.sl is None:
            sys.exit("ERROR: --setup には --entry と --sl が必要です")
        text = build_setup(a)
        order_side = "sell" if a.setup.upper() in ("SHORT", "SELL") else "buy"
        keyboard = scenario_keyboard(order_side, a.qty, a.entry, a.sl, a.tp)
    else:
        raw = " ".join(a.text) if a.text else sys.stdin.read()
        raw = raw.strip()
        if not raw:
            sys.exit("ERROR: 本文が空です")
        text = esc(raw)
        keyboard = None

    r = send(cfg, text, keyboard)
    if r and r.get("ok"):
        print("送信しました")
    else:
        sys.exit(f"送信に失敗: {r}")


if __name__ == "__main__":
    main()

# -*- coding: utf-8 -*-
"""R57: トレード日誌の自動計測(MAE/MFE・決済種別・決済後 60 分・再エントリー・時間帯・統計)。

    python tests/test_obsidian_metrics.py
"""
import os
import sys
from datetime import datetime, timedelta, timezone

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
import obsidian_metrics as om  # noqa: E402

PASS = [0]
FAIL = [0]
JST = timezone(timedelta(hours=9))


def check(label, condition, detail=""):
    if condition:
        PASS[0] += 1
        print(f"  OK   {label}")
    else:
        FAIL[0] += 1
        print(f"  NG   {label}  {detail}")


def bar(t, o, h, l, c):
    return {"t": float(t), "o": o, "h": h, "l": l, "c": c, "v": 1.0}


entry_at = datetime(2026, 9, 5, 4, 51, tzinfo=JST)
closed_at = datetime(2026, 9, 5, 4, 55, tzinfo=JST)
e, x = entry_at.timestamp(), closed_at.timestamp()
# 建玉中 2 本(04:51〜04:54, 04:54〜04:57 の途中で決済)。決済後 60 分に TP1 到達。
bars = [bar(e - 540, 29560, 29566, 29555, 29562), bar(e - 180, 29562, 29575, 29560, 29572),
        bar(e, 29572, 29578, 29569, 29576), bar(e + 180, 29576, 29583, 29574, 29580),
        bar(x + 180, 29580, 29584, 29560, 29562), bar(x + 540, 29562, 29565, 29540, 29542),
        bar(x + 900, 29542, 29545, 29530, 29533), bar(x + 4000, 29533, 29536, 29520, 29525)]
short = {"side": "SHORT", "entry": 29570.5, "exit": 29582.75, "stop": 29580.25, "tp1": 29535.0,
         "finalTarget": 29350.75, "qty": 20, "pnlNet": -490.0}

m = om.excursions(short, bars, entry_at, closed_at)
check("MAE は建玉中の高値 − 建値(SHORT)", m["mae_pt"] == 12.5, str(m))
check("MFE は建値 − 建玉中の安値", m["mfe_pt"] == 1.5, str(m))
check("R 換算(SL 9.75pt)", m["mae_r"] == 1.28 and m["mfe_r"] == 0.15, str(m))
check("保有時間・本数", m["hold_min"] == 4.0 and m["bars_held"] == 2, str(m))
check("効率は損益 ÷ MFE(USD)", m["efficiency"] == round(-490.0 / (1.5 * 2 * 20), 2), str(m))
check("決済後 60 分の有利・不利", m["post60_fav_pt"] == 52.75 and m["post60_adv_pt"] == 1.25, str(m))
check("決済後 TP1 到達までの分数(足の終わり)", m["tp1_after_exit_min"] == 18, str(m))
check("足が無ければ空", om.excursions(short, [], entry_at, closed_at) == {})

check("決済の種類: SL(滑り 2.5)", om.exit_kind(short) == ("SL", 2.5))
check("TP1", om.exit_kind({**short, "exit": 29535.25}) == ("TP1", None))
check("分割の加重平均は PARTIAL", om.exit_kind({**short, "exit": 29552.0}) == ("PARTIAL", None))
check("裁量の不利側決済は MANUAL", om.exit_kind({**short, "exit": 29574.0, "stop": 29599.5}) == ("MANUAL", None))
long_trade = {"side": "LONG", "entry": 20000.0, "exit": 19989.5, "stop": 19990.0, "tp1": 20030.0}
check("LONG の SL(滑り 0.5)", om.exit_kind(long_trade) == ("SL", 0.5), str(om.exit_kind(long_trade)))
check("LONG の SL で有利側に約定すれば滑り 0", om.exit_kind({**long_trade, "exit": 19990.5}) == ("SL", 0.0))
# 建玉時刻が不明(手動建玉)なら保有中の MAE/MFE は出さず、決済後だけ出す
m_manual = om.excursions(short, bars, None, closed_at)
check("建玉時刻不明なら MAE/MFE を推測しない", "mae_pt" not in m_manual and m_manual.get("post60_fav_pt") == 52.75, str(m_manual))

check("時間帯: 04:51 JST は NY PM", om.session_bucket(entry_at) == "NY PM")
check("時間帯: 23:10 JST は NY AM", om.session_bucket(datetime(2026, 9, 4, 23, 10, tzinfo=JST)) == "NY AM")
check("時間帯: 16:00 JST はロンドン", om.session_bucket(datetime(2026, 9, 4, 16, 0, tzinfo=JST)) == "ロンドン")
check("時間帯: 05:30 JST は引け後", om.session_bucket(datetime(2026, 9, 5, 5, 30, tzinfo=JST)) == "引け後")

# 連敗・再エントリー: 04:34 に損切り → 04:51 建玉(17 分後)
t1 = {"closedAt": datetime(2026, 9, 5, 4, 34, tzinfo=JST), "entryAt": datetime(2026, 9, 5, 4, 27, tzinfo=JST), "outcome": "LOSS", "pnlNet": -518.0, "model": "A"}
t2 = {"closedAt": closed_at, "entryAt": entry_at, "outcome": "LOSS", "pnlNet": -490.0, "model": "B"}
t3 = {"closedAt": datetime(2026, 9, 5, 5, 30, tzinfo=JST), "entryAt": datetime(2026, 9, 5, 4, 56, tzinfo=JST), "outcome": "WIN", "pnlNet": 1281.0, "model": "B"}
t0 = {"closedAt": datetime(2026, 9, 5, 3, 52, tzinfo=JST), "entryAt": datetime(2026, 9, 5, 2, 5, tzinfo=JST), "outcome": "WIN", "pnlNet": 1236.0, "model": "B"}
seq = [t3, t1, t0, t2]
om.sequence_context(seq)
check("最初の損切りは連敗 0・再エントリーなし", t1["loss_streak_before"] == 0 and t1["reentry_after_loss_min"] is None)
check("損切り 17 分後の建玉は再エントリー", t2["reentry_after_loss_min"] == 17 and t2["loss_streak_before"] == 1, str(t2))
check("2 連敗の 1 分後", t3["loss_streak_before"] == 2 and t3["reentry_after_loss_min"] == 1, str(t3))

s = om.session_stats(seq)
check("統計: N/勝率/PF/純損益", s["n"] == 4 and s["wins"] == 2 and s["win_rate"] == 0.5
      and s["net"] == 1509 and s["pf"] == round(2517 / 1008, 2), str(s))
check("最大 DD は決済ベース", s["max_dd"] == 1008, str(s))
check("損切り後 30 分内の再エントリー数", s["reentries"] == 2, str(s))
svg = om.equity_svg(s["curve"])
check("累積損益 SVG", svg.startswith("<svg") and "最終 +1,509 USD" in svg)
check("空の曲線でも落ちない", "no closed trades" in om.equity_svg([]))

print()
print(f"合計: PASS {PASS[0]} / FAIL {FAIL[0]}")
sys.exit(1 if FAIL[0] else 0)

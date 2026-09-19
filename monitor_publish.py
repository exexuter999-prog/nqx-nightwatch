#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Publish a monitor bundle and optionally reconcile the R11-D execution loop.

The monitor loop supplies a compact JSON bundle on stdin.  The default remains
notification/preview-only.  When the two explicit environment flags
``NQX_AUTOTRADE=1`` and ``NQX_LIVE_ORDERS=1`` are present, the separate
``autotrade_engine`` module may submit, modify, or flatten through ``order.py``.

状態の正本は Cloudflare の Durable Object にある。このスクリプトは producer
として、scenarioId / fingerprint / issuedAt / observedAt / expiresAt / symbol /
state を付けて publish する。Mini App はそこから読むので、Telegram のメッセージや
URL に埋めた過去 payload が表示の根拠になることはない。

Cloudflare 未設定の場合だけ、従来どおり ``?monitor=`` 付きの URL を送る。
その場合 Mini App は「未検証プレビュー」として表示し、発注ボタンを出さない。
"""
import base64
import hashlib
import json
import math
import os
import re
import statistics
import sys
import urllib.parse
from datetime import datetime, timedelta, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
WEB_APP_URL = os.environ.get("TELEGRAM_WEB_APP_URL", "https://nqx-nightwatch.pages.dev/")
STATE_FILE = os.path.join(BASE, ".secrets", "monitor_last_sent.json")
LEGACY_STATE_FILE = os.path.join(BASE, ".secrets", "last_monitor_bundle.json")
# R122: 構造文脈の記憶(初出 / 否定 / 消化)。**発注台帳とは別ファイル**で、
# 読めなくても周期は止めない(記憶なし = 毎周期その場の観測だけで判断する)。
STRUCTURE_STATE_FILE = os.path.join(BASE, ".secrets", "structure_context_state.json")

# シナリオの有効期間。監視ループは 3 分間隔なので、3 サイクル分を上限にする。
# 監視が止まればサーバー時刻だけでシナリオが消える。
SCENARIO_TTL_MIN = float(os.environ.get("NQX_SCENARIO_TTL_MIN", "10"))
MARKET_TICK = 0.25
# MNQ は 1pt = $2.00。NQ(E-mini)の $20.00 と混同しないこと。
MNQ_POINT_VALUE = 2.0

# --- 通知の見た目に関する定数 ---
# 通知バナーに出るのは先頭 2 行だけなので、そこに状況を凝縮する。
SPARK_CHARS = "▁▂▃▄▅▆▇█"
VWAP_BAR_CELLS = 20
# 最近接レベルがこの距離以内なら「接触」として扱う。
ZONE_TOUCH_POINTS = 3.0
# 観測がこれ以上古ければ鮮度に警告を付ける(CLAUDE.md §6 の /price と同基準)。
STALE_WARN_MIN = 10.0
# 2 行目に載せる待機理由の最大長。長いと通知バナーで切られる。
HEADLINE_REASON_CHARS = 34
MARKET_MAX_AGE_SEC = float(os.environ.get("NQX_MARKET_MAX_AGE_SEC", "600"))
MARKET_FUTURE_TOLERANCE_SEC = 60.0
# Mini App のチャートに送る本数。60 はユーザー選定(2026-08-16、デモで比較)。
# Worker 側の受理上限は 240。監視ループが多めに取ってもここで 60 に揃う。
FEED_BARS_MAX = max(20, min(240, int(float(os.environ.get("NQX_FEED_BARS", "60")))))
#: 判定専用の 1 分足の保持上限。``FEED_BARS_MAX`` は **Mini App のチャートへ送る**
#: 本数であって、評価器の入力枠ではない。1 分足は market payload にも凍結プレビュー
#: URL にも載らない(``build_market_payload`` / ``_frozen_preview_url`` は
#: bars3m しか読まない)ので、ここを 60 に揃える理由が無い。60 に揃えていた間、
#: ``msnr_gate.SILVER_BULLET_LOOKBACK_BARS`` (90) は一度も届かず、Silver Bullet の
#: liquidity level 母集団だけが静かに 1/3 へ縮んでいた。上限は評価器側の
#: ``msnr_gate.MAX_INPUT_BARS`` と同じ 240 に合わせる。
FEED_BARS_1M_MAX = 240
#: 運ぶレベル数の上限。Worker 側(state_machine.js の validateMarket)と同値。
FEED_LEVELS_MAX = 30

#: RISK_* が読めないときの SL 上限(pt)。fail-open でゲートを消す代わりの既定値。
#: **自分で数字を決めない** —— 実行契約の既定上限
#: (defaultCapDollars / pointValue / fixedQty) をそのまま使う。これなら契約が
#: 通す範囲を新たに狭めず、「読めなかったからゲートが消える」だけを潰せる。
#: execution_contract の import はこの下にあるので、呼ばれた時に評価する。
def sl_cap_fallback_pt():
    try:
        risk = execution_contract.CONTRACT["risk"]
        return (float(risk["defaultCapDollars"]) / float(risk["pointValue"])
                / max(1, int(risk["fixedQty"])))
    except Exception:
        return 60.0
TELEGRAM_MAX_TEXT_BYTES = 4096


def telegram_utf8_preflight(text, limit=TELEGRAM_MAX_TEXT_BYTES):
    """Keep Telegram HTML messages within its UTF-8 byte budget.

    Notification rows are self-contained HTML fragments, so truncating only at
    row boundaries preserves tag balance.  This is a display guard; it never
    changes a market/scenario or an execution decision.
    """
    value = str(text or "")
    if len(value.encode("utf-8")) <= limit:
        return value
    suffix = "\n<i>… truncated for Telegram</i>"
    suffix_bytes = len(suffix.encode("utf-8"))
    if suffix_bytes >= limit:
        raise ValueError("Telegram byte limit is too small for the truncation marker")
    kept = []
    used = 0
    for line in value.splitlines():
        addition = ("\n" if kept else "") + line
        if used + len(addition.encode("utf-8")) + suffix_bytes > limit:
            break
        kept.append(line)
        used += len(addition.encode("utf-8"))
    # A generated monitor message always starts with a bounded header.  Keep a
    # defensive plain-text fallback for direct utility callers nevertheless.
    if not kept:
        plain = ""
        remaining = limit - suffix_bytes
        for char in value:
            if len((plain + char).encode("utf-8")) > remaining:
                break
            plain += char
        return plain + suffix
    return "\n".join(kept) + suffix

sys.path.insert(0, BASE)
import nqx_state  # noqa: E402
import msnr_gate  # noqa: E402  (R11-D pure strategy evaluator; never places orders)
import autotrade_arm  # noqa: E402  (session-scoped arming ledger)
import autotrade_engine  # noqa: E402  (explicitly armed execution lifecycle)
import execution_contract  # noqa: E402  (pure single execution contract)
import contract as contract_month  # R102: 取引限月の正本  # noqa: E402
import strategy_evidence  # noqa: E402  (canonical strategy matrix envelope)
import events as econ_events  # noqa: E402  (キャッシュ読取のみ。fetch はしない)
from telegram_bot import api, esc, load_env, send  # noqa: E402


def _event_gate():
    """経済イベントの現在状態。イベント機能の故障で通知経路を殺さない。"""
    try:
        return econ_events.current_gate()
    except Exception as exc:
        return {"blackout": None, "warning": None, "upcoming": [],
                "notes": [f"events unavailable: {type(exc).__name__}: {exc}"]}


def _event_until(blackout):
    """封鎖の解除時刻をローカル HH:MM で。読めなければ空文字(捏造しない)。"""
    try:
        return econ_events._parse_at(blackout.get("windowEnd")).astimezone().strftime("%H:%M")
    except (ValueError, TypeError, AttributeError):
        return ""


# ボラ予算ゲート(CLAUDE.md §3「★ ボラ予算ゲート」/ スキル operational-gates.md §8)。
# 緩衝(直近12本の確定足レンジ中央値)が SL 予算に占める割合で武装可否を決める。
# 8/17 の2敗は比率 0.77 / 0.82 でどちらも停止帯だった。
# 枚数は口座ルールで固定する(2026-08-22)。監視側の bundle が qty を書き忘れても
# アプリと発注が食い違わないよう、publish の直前でここが正規化する。
# 「定型文にだけ書いた規則は実行されない」— パスB と同じ失敗を繰り返さない。
FIXED_QTY_DEFAULT = int(execution_contract.CONTRACT["risk"]["fixedQty"])
VOL_GATE_BARS = 12
VOL_GATE_STANDDOWN = float(os.environ.get("NQX_VOL_GATE_STANDDOWN", "0.60"))
# 2026-09-01 ユーザー決定: 中間帯の「A+のみ」を撤廃し、A も武装させる。
# 既定を stand-down と同値にすると A+ 限定帯が空になる(>0.60 の全停止は不変)。
# 旧運用へ戻すのはコード変更ではなく `NQX_VOL_GATE_APLUS=0.40` の環境変数で行う。
VOL_GATE_APLUS = float(os.environ.get("NQX_VOL_GATE_APLUS", "0.60"))
MNQ_DOLLARS_PER_POINT = float(execution_contract.CONTRACT["risk"]["pointValue"])


def _sl_caps(env=None):
    """RISK_* から固定枚数時の SL 上限(pt)を2水準で返す。

    返すのは (全口座, 次点) の pt。ドル上限は口座数で割らず、各口座の
    ``RISK_<account>`` を ``pointValue * fixedQty`` で一度だけ割る。
    既定契約では $240 / ($2 * 2枚) = 60pt。LIFELINE は参照しない。

    R35: 設定が読めないときは (None, None) で**ゲートを無効化していた**
    (fail-open)。`.secrets/crosstrade.env` が読めない・`RISK_*` が壊れている・
    `MAX_RISK_DOLLARS` が非数値、のいずれかで SL 上限チェックが丸ごと消え、
    上限無しのシナリオが ARMED で公開されていた。

    お金に効くゲートを「設定が読めない」で無効化しない。読めなければ
    `sl_cap_fallback_pt()` へ落とす —— 保守的な既定値であって無効化ではない。
    """
    try:
        env = env if env is not None else nqx_state._read_kv_env(nqx_state.CROSSTRADE_ENV)
        raw = str(env.get("CROSSTRADE_ACCOUNTS") or env.get("CROSSTRADE_ACCOUNT") or "")
        ids = [a.strip() for a in re.split(r"[,\n]+", raw) if a.strip()]
        caps = []
        for account_id in ids:
            scoped = dict(env)
            scoped["CROSSTRADE_ACCOUNTS"] = account_id
            scoped.pop("CROSSTRADE_ACCOUNT", None)
            cap_dollars, _ = execution_contract.risk_cap(scoped)
            if cap_dollars is not None and cap_dollars > 0:
                caps.append(float(cap_dollars))
        if not caps:
            fallback = sl_cap_fallback_pt()
            return fallback, fallback
        caps.sort()
        risk = execution_contract.CONTRACT["risk"]
        fixed_qty = max(1, int(risk["fixedQty"]))
        point_value = float(risk["pointValue"])
        allc = caps[0] / point_value / fixed_qty
        nextc = (caps[1] if len(caps) > 1 else caps[0]) / point_value / fixed_qty
        return allc, nextc
    except Exception:
        # 例外でもゲートを消さない。読めなかったこと自体は下流の注記で見える。
        fallback = sl_cap_fallback_pt()
        return fallback, fallback


def _account_sl_caps(env=None):
    """RISK_* から**口座ごと**の SL 上限(pt)を ``[(account, cap_pt)]`` で返す。

    `_sl_caps` は経路全体で満たすべき「最も厳しい1本」を返す。こちらは口座別
    上限をそのまま返すので、払えない口座だけを経路から外す判定に使える。

    R35 と同じく、設定が読めないときもゲートを無効化しない。読めなければ
    `sl_cap_fallback_pt()` 1本へ落とす —— 保守的な既定値であって無効化ではない。
    """
    try:
        env = env if env is not None else nqx_state._read_kv_env(nqx_state.CROSSTRADE_ENV)
        rows = execution_contract.account_caps(env)
        if not rows:
            return [(None, sl_cap_fallback_pt())]
        risk = execution_contract.CONTRACT["risk"]
        fixed_qty = max(1, int(risk["fixedQty"]))
        point_value = float(risk["pointValue"])
        return [(account, float(cap) / point_value / fixed_qty)
                for account, cap, _ in rows]
    except Exception:
        # 例外でもゲートを消さない。読めなかったこと自体は下流の注記で見える。
        return [(None, sl_cap_fallback_pt())]


def _ruling(ratio):
    if ratio is None:
        return "不明"
    if ratio > VOL_GATE_STANDDOWN:
        return "停止"
    if ratio > VOL_GATE_APLUS:
        return "A+のみ"
    return "通常"


def vol_gate(bundle, env=None):
    """ボラ予算ゲートを評価する。

    ``noise`` は直近 ``VOL_GATE_BARS`` 本の確定足レンジ中央値(= §1 の緩衝の
    最低量)。``ratio`` がこれを SL 上限で割った値で、>0.60 なら緩衝だけで
    予算が尽きるため武装させない。

    fail-open: 本数不足・設定不備では ``active`` を False にして降格しない。
    その事実は ``reason`` に載せて必ず表示する(隠すと沈黙の禁止になる)。
    """
    snapshot = bundle.get("snapshot") or {}
    bars = snapshot.get("bars3m") or snapshot.get("bars") or []
    if not isinstance(bars, list) or len(bars) < VOL_GATE_BARS:
        return {"active": False, "noise": None, "ratio_all": None, "ratio_next": None,
                "ruling_all": "不明", "ruling_next": "不明",
                "reason": f"ボラゲート無効(確定足 {len(bars) if isinstance(bars, list) else 0}本 "
                          f"< {VOL_GATE_BARS}本)"}
    def _num(bar, *keys):
        """短縮キー(h/l)と長いキー(high/low)の両方を受ける。"""
        for key in keys:
            value = bar.get(key) if isinstance(bar, dict) else None
            if isinstance(value, bool) or value is None:
                continue
            try:
                value = float(value)
            except (TypeError, ValueError):
                continue
            if math.isfinite(value):
                return value
        return None

    ranges = []
    for bar in bars[-VOL_GATE_BARS:]:
        high, low = _num(bar, "h", "high"), _num(bar, "l", "low")
        if high is None or low is None or high < low:
            continue
        ranges.append(high - low)
    if len(ranges) < VOL_GATE_BARS:
        return {"active": False, "noise": None, "ratio_all": None, "ratio_next": None,
                "ruling_all": "不明", "ruling_next": "不明",
                "reason": "ボラゲート無効(確定足の高安が欠落)"}

    noise = statistics.median(ranges)
    cap_all, cap_next = _sl_caps(env)
    if not cap_all:
        return {"active": False, "noise": noise, "ratio_all": None, "ratio_next": None,
                "ruling_all": "不明", "ruling_next": "不明",
                "reason": "ボラゲート無効(RISK_* 未設定)"}

    ratio_all = noise / cap_all
    ratio_next = noise / cap_next if cap_next else None
    return {
        "active": True,
        "noise": noise,
        "cap_all": cap_all,
        "cap_next": cap_next,
        "ratio_all": ratio_all,
        "ratio_next": ratio_next,
        "ruling_all": _ruling(ratio_all),
        "ruling_next": _ruling(ratio_next),
        # 全口座で停止帯なら武装させない。次点(小口座を外した水準)が通っても
        # 発注は3口座同時なので、最も厳しい口座に合わせる。
        "standdown": ratio_all > VOL_GATE_STANDDOWN,
        "reason": "",
    }


def format_vol_gate(vg):
    """ゲート判定の1行。常時表示する(通ったことも記録に残す)。"""
    if not vg:
        return ""
    if not vg.get("active"):
        return vg.get("reason") or "ボラゲート無効"
    noise = vg["noise"]
    parts = [f"ボラ床 {noise:.1f}pt", f"比率 {vg['ratio_all']:.2f}={vg['ruling_all']}"]
    if vg.get("ratio_next") is not None and vg["ratio_next"] != vg["ratio_all"]:
        parts.append(f"({vg['ratio_next']:.2f}={vg['ruling_next']})")
    return " ".join(parts)


def apply_volatility_grade_gate(chosen, vg):
    """Enforce the documented volatility bands in code.

    The band used to be display-only, leaving a scenario orderable unless the
    monitoring agent happened to notice the card.  The engine is the authority
    now: every grade stands down above ``VOL_GATE_STANDDOWN``.

    2026-09-01: the A+-only middle band was opened by user decision, so
    ``VOL_GATE_APLUS`` defaults to the stand-down level and A arms wherever A+
    does.  The clause below stays live because the threshold is still
    configurable (``NQX_VOL_GATE_APLUS``) — set it lower to restore the band.
    """
    if not isinstance(chosen, dict) or not isinstance(vg, dict) or not vg.get("active"):
        return chosen, None
    if str(chosen.get("state") or "WATCH").upper() not in {"ACTIVE", "ARMED"}:
        return chosen, None
    ratio = vg.get("ratio_all")
    try:
        ratio = float(ratio)
    except (TypeError, ValueError):
        return chosen, None
    try:
        noise = float(vg.get("noise") or 0.0)
    except (TypeError, ValueError):
        noise = 0.0
    if ratio > VOL_GATE_STANDDOWN:
        return ({**chosen, "state": "WATCH"},
                f"vol gate stand-down (noise {noise:.2f}pt / ratio {ratio:.2f} "
                f"> {VOL_GATE_STANDDOWN:.2f}) — scenario demoted to WATCH")
    if ratio > VOL_GATE_APLUS and str(chosen.get("grade") or "").upper() != "A+":
        return ({**chosen, "state": "WATCH"},
                f"vol gate A+ only (noise {noise:.2f}pt / ratio {ratio:.2f} "
                f"> {VOL_GATE_APLUS:.2f}) — grade A demoted to WATCH")
    return chosen, None


def normalize_qty(chosen, env=None):
    """シナリオの枚数を口座ルール(既定2枚)に揃え、上限超過なら武装を落とす。

    返り値は (scenario, notes)。**表示と強制のズレを作らない**のが目的で、
    アプリが ARMED を出したのに order.py が上限で弾く状態を防ぐ。
    """
    notes = []
    if not isinstance(chosen, dict):
        return chosen, notes
    try:
        env = env if env is not None else nqx_state._read_kv_env(nqx_state.CROSSTRADE_ENV)
    except Exception:
        env = {}
    # 枚数も実行契約を正本にする。環境変数で1枚へ変えてしまうと、監視だけが
    # 別のpt上限を表示し、Worker/engine/order.pyは固定2枚として拒否する。
    fixed = FIXED_QTY_DEFAULT

    out = dict(chosen)
    try:
        current = int(out.get("qty") or 0)
    except (TypeError, ValueError):
        current = 0
    if current != fixed:
        out["qty"] = fixed
        notes.append(f"qty normalized {current or 'unset'} -> {fixed} (account rule)")

    # 固定枚数でのリスクが口座上限を超えるなら、発注できない案を武装表示しない。
    # ただし上限は**口座ごと**に判定する。1口座が払えないことを理由に、払える
    # 口座の取引まで消さない —— 払えない口座を経路から外して継続する。
    caps_pt = _account_sl_caps(env)
    try:
        risk_pt = abs(float(out["entry"]) - float(out["stop"]))
    except (KeyError, TypeError, ValueError):
        risk_pt = None
    if caps_pt and risk_pt is not None and str(out.get("state", "")).upper() in ("ACTIVE", "ARMED"):
        # _account_sl_caps は既に固定枚数で使える幅。ここで枚数をもう一度割ると
        # $240 / $2 / 2枚 / 2枚 = 30pt となる二重換算なので禁止する。
        affordable = [(account, cap) for account, cap in caps_pt if risk_pt <= cap + 1e-9]
        excluded = [(account, cap) for account, cap in caps_pt if risk_pt > cap + 1e-9]
        if not affordable:
            widest = max(cap for _, cap in caps_pt)
            notes.append(
                f"risk {risk_pt:.2f}pt x {fixed} exceeds account cap "
                f"({widest:.2f}pt at {fixed}) — scenario demoted to WATCH")
            out["state"] = "WATCH"
        elif excluded:
            dropped = ", ".join(f"{account or 'default'}({cap:.2f}pt)" for account, cap in excluded)
            notes.append(
                f"risk {risk_pt:.2f}pt x {fixed} exceeds per-account cap for {dropped} "
                f"— those accounts are dropped from the route; "
                f"{len(affordable)} account(s) still armed")
    return out, notes


def compact(bundle):
    if not isinstance(bundle, dict):
        raise ValueError("bundle must be an object")
    snapshot = bundle.get("snapshot") or {}
    bars = snapshot.get("bars") if isinstance(snapshot, dict) else None
    if not isinstance(bars, list) or not bars:
        raise ValueError("snapshot.bars is required")
    snapshot = dict(snapshot)
    bars_original_count = len(bars)
    snapshot["bars"] = bars[-FEED_BARS_MAX:]
    bars3m = snapshot.get("bars3m")
    if isinstance(bars3m, list):
        bars_original_count = len(bars3m)
        snapshot["bars3m"] = bars3m[-FEED_BARS_MAX:]
    # 1 分足はチャート配信にも凍結プレビューにも載らない(送信経路は bars3m
    # だけを読む)。ここで 60 本へ切ると、publish の帯域は 1 バイトも減らないまま
    # 評価器の lookback だけが縮む。R13 パイプラインは compact の**後**に
    # `enrich_decisive_strategy` を呼ぶので、この切り詰めは判定入力そのものだった。
    bars1m = snapshot.get("bars1m")
    if isinstance(bars1m, list):
        snapshot["bars1mOriginalCount"] = len(bars1m)
        snapshot["bars1m"] = bars1m[-FEED_BARS_1M_MAX:]
    if bars_original_count > FEED_BARS_MAX:
        snapshot["barsTruncatedFrom"] = bars_original_count
    # HTF raw bars have already been reduced to ``htfContext`` and frozen in
    # strategyEvidence before publish.  Carrying another 4x80 OHLC rows into
    # the Worker/Telegram URL can silently remove the preview button, while no
    # downstream consumer reads them.  Keep counts plus compact context; the
    # full rows remain in the local pipeline/audit artifact.
    #
    # ``bars1d`` だけは例外で、**評価器が生足のまま直接読む唯一の HTF 系列**
    # (`msnr_gate._daily_bars` → R48 の `CLASSIC_TS` と `decision_context.gap`)。
    # R13 も monitor_publish.main も compact の**後**に評価するので、ここで
    # pop すると `_daily_bars` は常に空になり、日足由来の記録タグは
    # 一度も立たない —— 実測でも監査 896 サイクルに `CLASSIC_TS` は 0 件、
    # `context.gap` も 0 件だった。HTF 生足は market payload
    # (`build_market_payload`)にも凍結プレビュー(`_frozen_preview_url`)にも
    # 載らないため、残しても送信サイズは変わらない。
    #
    # R122(2026-09-19): ``bars15m`` も同じ理由で**残す**。R122 の親の仮説は
    # 「直接取得した確定 15 分足の構造」を第一の出所にしており、compact が落とすと
    # 評価器へ一度も届かない(実測: 監査 1,359 本すべてで `BARS15M_MISSING`、親は
    # 常に粗い HTF 要約へ落ちていた)。bars1d と同様、market payload にも凍結
    # プレビューにも載らないので送信サイズは変わらない。60 本 × 5 数値。
    for secondary in ("bars45m", "bars1h", "bars4h"):
        rows = snapshot.pop(secondary, None)
        if isinstance(rows, list):
            snapshot[secondary + "Count"] = len(rows)
    bars15m = snapshot.get("bars15m")
    if isinstance(bars15m, list):
        snapshot["bars15mCount"] = len(bars15m)
    bars1d = snapshot.get("bars1d")
    if isinstance(bars1d, list):
        snapshot["bars1dCount"] = len(bars1d)
    levels = snapshot.get("levels")
    if isinstance(levels, list):
        # R31: 10 本では「意識している水準」を全部は運べない。実測で画面内の
        # レベルは中央値 16 本・最大 26 本。レベル一覧フィードが全部を描けるよう
        # 30 本まで載せる(Worker 側の上限も同じ値に揃えてある)。
        snapshot["levels"] = levels[:FEED_LEVELS_MAX]
    out = {
        "at": bundle.get("at") or snapshot.get("at"),
        "price": bundle.get("price", snapshot.get("price")),
        "change": bundle.get("change"),
        "changePct": bundle.get("changePct"),
        "vwap": bundle.get("vwap", snapshot.get("vwap")),
        "cvd": bundle.get("cvd", snapshot.get("cvd")),
        "regime": bundle.get("regime", "MX"),
        "scenarios": bundle.get("scenarios") or {},
        "strategyEvidence": bundle.get("strategyEvidence") if isinstance(bundle.get("strategyEvidence"), dict) else None,
        "snapshot": snapshot,
    }
    # Preserve pane-level telemetry for the Mini App/Telegram preview. These
    # are observational only: CVD must come from an explicit visible study
    # and VIX must remain a delayed context quote.
    # ``watching`` / ``cvdFast`` / ``cvdSlow`` / ``position`` はすべて任意。
    # 監視ループが供給しなかった場合、通知は該当ブロックを省略するだけで、
    # 欠損を推測値で埋めることはしない。
    for key in (
        "priceAt", "priceSource", "sourceSymbol", "sourceFrontContract", "sourceContract",
        "cvdSource", "cvdPane", "cvdTimeframe", "cvdAt",
        "cvdFast", "cvdSlow", "cvdMeta", "cvdAttempts",
        "vix", "vixAt", "vixSource", "vixDelayed",
        "watching", "position",
        # R11-D: preserve router inputs through compaction.
        "phase", "po3", "eventGate", "rangeAnchor", "peerMeta", "sessionId",
        # R6: シナリオ評価の表示用。ここでは検証せず透過する(正本の検証は
        # Worker 側。落ちても market を止めない設計 = APP_EVAL_DISPLAY_SPEC §5)
        "evaluation",
        "ictEvidence", "eventBlackout", "sessionEndAt",
        # Ingress/provider evidence is an audit trail only.  The evaluated
        # strategyEvidence above remains the one frozen execution/OOS input.
        "providerStrategyEvidenceAudit",
        "acquisitionReceipt",
    ):
        if key in bundle:
            out[key] = bundle[key]
    if not out["at"]:
        raise ValueError("bundle.at is required")
    return out


def acquisition_display_gate(receipt):
    """Collapse the raw acquisition receipt into a safe UI status.

    Hashes and local paths stay in the audit bundle.  The Mini App only needs
    enough truth to distinguish a coherent fresh acquisition from a stale or
    partial one.  This function never promotes a scenario; it is display-only.
    """
    if not isinstance(receipt, dict):
        return {
            "status": "MISSING", "requiredFresh": False,
            "freshCount": 0, "requiredCount": 0,
            "oldestAgeSec": None, "sourceSpanSec": None,
            "staleRequired": ["acquisitionReceipt"],
        }
    sources = receipt.get("sources")
    if not isinstance(sources, dict):
        return {
            "status": "MISSING", "requiredFresh": False,
            "freshCount": 0, "requiredCount": 0,
            "oldestAgeSec": None, "sourceSpanSec": None,
            "staleRequired": ["acquisitionReceipt.sources"],
        }
    required = [(str(name), row) for name, row in sources.items()
                if isinstance(row, dict) and row.get("required") is True]
    fresh = [(name, row) for name, row in required if row.get("status") == "FRESH"]
    stale = [name for name, row in required if row.get("status") != "FRESH"]
    ages = []
    stamps = []
    for _, row in required:
        try:
            age = float(row.get("ageSec"))
            if math.isfinite(age) and age >= 0:
                ages.append(age)
        except (TypeError, ValueError):
            pass
        try:
            stamp = datetime.fromisoformat(str(row.get("modifiedAt") or "").replace("Z", "+00:00"))
            stamps.append(stamp.timestamp())
        except (TypeError, ValueError, OverflowError):
            pass
    required_fresh = bool(required) and len(fresh) == len(required) \
        and receipt.get("requiredFresh") is True and not stale
    return {
        "status": "FRESH" if required_fresh else ("STALE" if required else "MISSING"),
        "requiredFresh": required_fresh,
        "freshCount": len(fresh),
        "requiredCount": len(required),
        "oldestAgeSec": round(max(ages), 3) if ages else None,
        "sourceSpanSec": round(max(stamps) - min(stamps), 3) if len(stamps) >= 2 else None,
        "staleRequired": stale[:8],
    }


def _apply_entry_depth(out, scenario, result):
    """R86: VP80 の建値と SL を同じ幅だけ深くする(``entry_depth.annotate``)。

    ``scenario`` は毎回 decision から作り直した**元の幾何**なので、pipeline と publish で
    2 回呼ばれても二重には動かない。この層の失敗で監視サイクルを落とさない —— 例外は
    「動かさない」に倒して注記だけ返す(発注幾何を変えない側が安全)。
    """
    try:
        import entry_depth
        return entry_depth.annotate(out, scenario, (result or {}).get("noiseFloor"))
    except Exception as exc:                     # noqa: BLE001 - 失敗時は元の幾何のまま
        out.pop("entryDepth", None)
        return scenario, f"R86 entry depth unavailable (not applied): {type(exc).__name__}: {exc}"


def _structure_memory_load(path=STRUCTURE_STATE_FILE):
    """R122 の記憶を読む。無い・壊れている・読めないは「記憶なし」。"""
    try:
        with open(path, encoding="utf-8") as handle:
            state = json.load(handle)
    except (OSError, ValueError):
        return None
    return state if isinstance(state, dict) else None


def _structure_memory_save(context, state, path=STRUCTURE_STATE_FILE):
    """観測を取り込んで書き戻す。**失敗しても周期を止めない**(注記だけ返す)。"""
    try:
        import market_structure_context
        memory = market_structure_context.Memory(state)
        payload = memory.observe(context)
        target = path + ".tmp"
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(target, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=1)
        os.replace(target, path)
        return None
    except Exception as exc:  # noqa: BLE001 - 記憶の失敗で publish を止めない
        return f"R122 structure memory not persisted: {type(exc).__name__}: {exc}"


def enrich_decisive_strategy(bundle):
    """R11-D: monitor bundleから評価カードとprimary scenarioを生成する。

    これは純粋なローカル計算で、publishやorder.pyを呼ばない。既存の
    外部シナリオはdecisionが価格を持つ場合だけprimaryへ置き換え、2位以下を
    ライブへ漏らさない。計算不能時は価格フィードを止めず、呼び出し側へ注記を返す。
    """
    if os.environ.get("NQX_DECISIVE_STRATEGY", "1").strip().lower() in {"0", "false", "off"}:
        return bundle, ["R11-D disabled by NQX_DECISIVE_STRATEGY"]
    out = dict(bundle)
    strategy_notes = []
    # R122: 構造文脈の記憶は evaluate の**入力**として渡し、評価後に取り除く
    # (msnr_gate.evaluate は純粋関数のままで、ファイルには触れない)。
    structure_state = _structure_memory_load()
    if isinstance(structure_state, dict):
        try:
            import market_structure_context
            out["_structureMemory"] = market_structure_context.Memory(structure_state).snapshot()
        except Exception:  # noqa: BLE001 - 記憶が読めなくても評価は続ける
            out.pop("_structureMemory", None)
    try:
        result = msnr_gate.evaluate(out)
        out.pop("_structureMemory", None)
        structure_context = result.get("structureContext")
        if structure_context is not None:
            note = _structure_memory_save(structure_context, structure_state)
            if note:
                strategy_notes.append(note)
            try:
                import market_structure_context
                # 監査コピー用の縮約。発注経路は decision.structure を読む。
                out["structureContext"] = market_structure_context.compact(structure_context)
            except Exception:  # noqa: BLE001
                pass
        decision = result.get("decision") or {}
        data_gate = acquisition_display_gate(out.get("acquisitionReceipt"))
        # A raw-data receipt is mandatory for an actionable decision.  Keep
        # the analytical model visible, but remove its orderable grade/state
        # when the acquisition cannot be proved coherent and fresh.
        if data_gate.get("requiredFresh") is not True:
            decision = dict(decision)
            blocker = ("ACQUISITION_RECEIPT_MISSING"
                       if data_gate.get("status") == "MISSING"
                       else "ACQUISITION_DATA_STALE")
            decision["hardBlockers"] = list(dict.fromkeys(
                [*(decision.get("hardBlockers") or []), blocker]))
            decision["state"] = "WATCH"
            decision["grade"] = None
            result = dict(result)
            result["decision"] = decision
        card = msnr_gate.build_card(
            result,
            price=out.get("price"),
            side=decision.get("side") if decision.get("side") in {"BUY", "SELL"} else None,
            entry=decision.get("entry"), stop=decision.get("stop"),
            # htf(15分 CT_TREND/ATR)と entry(air / reachR / pass)を bundle から
            # 機械生成させる。R51 で人手の追記が消えて以降、渡さない限り
            # 15M ALIGN / ENTRY 行は永久に N/A だった。
            bundle=out,
        )
        card["dataGate"] = data_gate
        out["evaluation"] = card
        # 証跡は表示カードと分けて渡す。後段はここを読めば、OTEがどの
        # range anchor / FVG / SMT / CVD状態で評価されたか追跡できる。
        out["ictEvidence"] = result.get("ict") or {}
        # Full model matrix is observational and feeds both the Telegram
        # scenario card and the TradingView-style chart overlays.
        raw_matrix = result.get("strategyMatrix") or {}
        try:
            evidence = strategy_evidence.from_matrix(
                raw_matrix, as_of=out.get("at"),
                session_id=out.get("sessionId") or (out.get("snapshot") or {}).get("sessionId") or "UNSPECIFIED",
                source="msnr_gate", provenance="monitor-evaluate")
            out["strategyEvidence"] = evidence
            eligible_votes = ((raw_matrix.get("alignment") or {}).get("evidence")
                              if isinstance(raw_matrix, dict) else {}) or {}
            provenance = {
                "setupVersion": "R42-STRUCTURAL-EDGE/1",
                "catalogVersion": str((raw_matrix or {}).get("version") or "IMAGE_STRATEGY_CATALOG/1"),
                "detectorVersion": "REPO2-DETECTOR/1",
                "executionContractVersion": execution_contract.VERSION,
                "strategyEvidence": evidence or {},
                "eligibleVotes": eligible_votes,
                "evidenceHash": (evidence or {}).get("evidenceHash"),
            }
            out.update(provenance)
            # The decision, scenario, published state and later OOS record
            # must all freeze exactly the same version/evidence identity.
            if isinstance(decision, dict):
                decision.update(provenance)
            if isinstance(card.get("decision"), dict):
                card["decision"].update({
                    key: value for key, value in provenance.items()
                    if key not in {"strategyEvidence", "eligibleVotes"}
                })
        except ValueError as exc:
            out["strategyEvidence"] = None
            strategy_notes.append(f"strategy evidence unavailable: {exc}")
        health = (result.get("ict") or {}).get("cvd") or {}
        if health.get("refreshRequired"):
            out["cvdRefresh"] = health.get("refresh")
        scenario = msnr_gate.decision_to_scenario(decision, out, qty=FIXED_QTY_DEFAULT)
        depth_note = None
        if scenario:
            scenario, depth_note = _apply_entry_depth(out, scenario, result)
            out["scenarios"] = {"primary": scenario}
        elif decision.get("model") == "FLAT":
            out["scenarios"] = {}
            out.pop("entryDepth", None)
        notes = ["R12 decision=%s/%s/%s" % (
            decision.get("model"), decision.get("side"), decision.get("grade") or "B")]
        if depth_note:
            notes.append(depth_note)
        notes.extend(strategy_notes)
        if health.get("refreshRequired"):
            notes.append("CVD refresh required before next cycle (A+ capped to A)")
        elif health.get("status") == "UNAVAILABLE_A_CAP":
            notes.append("CVD unavailable after retry (A+ capped to A)")
        return out, notes
    except (TypeError, ValueError, KeyError, ArithmeticError) as exc:
        out.pop("_structureMemory", None)
        return out, [f"R11-D evaluation unavailable: {type(exc).__name__}: {exc}"]


def _finite_number(value, field):
    """Return a finite float; never turn missing/invalid data into zero."""
    if value is None or isinstance(value, bool):
        raise ValueError(f"{field} is required")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a finite number") from exc
    if not math.isfinite(parsed):
        raise ValueError(f"{field} must be a finite number")
    return parsed


def _instant(value, field):
    if not value:
        raise ValueError(f"{field} is required")
    raw = str(value).strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _on_tick(value):
    return abs((value / MARKET_TICK) - round(value / MARKET_TICK)) < 1e-8


def normalize_market_bars(raw_bars, limit=None):
    """Normalize TradingView long/short OHLC keys to the Mini App contract.

    ``limit`` は保持する末尾本数。既定は Mini App のチャート枠 ``FEED_BARS_MAX``
    だが、チャートへ送らない足(1 分足)は評価器の枠で呼ぶこと。
    """
    if not isinstance(raw_bars, list) or not raw_bars:
        raise ValueError("snapshot.bars is required for the market feed")
    keep = FEED_BARS_MAX if limit is None else max(1, int(limit))
    normalized = []
    for index, raw in enumerate(raw_bars[-keep:]):
        if not isinstance(raw, dict):
            raise ValueError(f"snapshot.bars[{index}] must be an object")
        t = raw.get("t", raw.get("time"))
        o = _finite_number(raw.get("o", raw.get("open")), f"bars[{index}].open")
        h = _finite_number(raw.get("h", raw.get("high")), f"bars[{index}].high")
        l = _finite_number(raw.get("l", raw.get("low")), f"bars[{index}].low")
        c = _finite_number(raw.get("c", raw.get("close")), f"bars[{index}].close")
        try:
            t = float(t)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"bars[{index}].time must be epoch seconds") from exc
        if not math.isfinite(t) or t <= 0:
            raise ValueError(f"bars[{index}].time must be epoch seconds")
        if h < l or h < max(o, c) or l > min(o, c):
            raise ValueError(f"bars[{index}] has an impossible OHLC range")
        if not all(_on_tick(value) for value in (o, h, l, c)):
            raise ValueError(f"bars[{index}] is not aligned to the 0.25 MNQ tick")
        bar = {"t": t, "o": o, "h": h, "l": l, "c": c}
        volume = raw.get("v", raw.get("volume"))
        if volume is not None:
            bar["v"] = _finite_number(volume, f"bars[{index}].volume")
        normalized.append(bar)
    return normalized


# ---------------------------------------------------------------- R56 指標(表示専用)
# チャートに「読み取った追加情報」を載せるための小さな束。VWAP バンド・CVD の
# 生値と履歴・PO3・SMT 観測・15分 SwingArm(CT)の値・イベント状態。
# **表示専用**。判定は msnr_gate / execution_contract が bundle から直接読んでおり、
# ここを変えても発注経路は変わらない。欠けている値は載せない(0 で埋めない)。
INDICATOR_HISTORY_MAX = 24
_CT_KEYS = {
    "trend": "NQX_DATA_CT_TREND", "atr": "NQX_DATA_CT_ATR",
    "fib618": "NQX_DATA_CT_FIB618", "fib786": "NQX_DATA_CT_FIB786", "fib886": "NQX_DATA_CT_FIB886",
    "trail": "NQX_DATA_CT_TRAIL", "swing100": "CT SwingArm / 100",
    "ltfTrend": "NQX_DATA_LTF_TREND", "ltfAtr": "NQX_DATA_LTF_ATR",
    "ltfFib618": "NQX_DATA_LTF_FIB618", "ltfTrail": "NQX_DATA_LTF_TRAIL",
    "htfAtr": "NQX_DATA_HTF_ATR", "htfFib618": "NQX_DATA_HTF_FIB618", "htfTrail": "NQX_DATA_HTF_TRAIL",
    "hardStop": "NQX_DATA_RISK_HARD_STOP", "invalidation": "NQX_DATA_RISK_INVALIDATION",
}


def _cvd_scalar(value):
    """CVD を数値にする。tv_snapshot は ``{bias, fast, slow, value}`` の辞書で持つが、
    Worker は数値しか受けない(辞書のまま送ると market.cvd が常に null になり、
    画面の CVD が消えていた — 2026-09-05 実測)。"""
    if isinstance(value, dict):
        return _number(value.get("value"))
    return _number(value)


def build_indicators(bundle):
    """チャート用の指標束。値が一つも無ければ ``{}``(payload に載せない)。"""
    snapshot = bundle.get("snapshot") if isinstance(bundle.get("snapshot"), dict) else {}
    out = {}
    for key, source in (("vwapHi", "vwap_hi"), ("vwapLo", "vwap_lo"), ("vwapAnchorT", "vwapAnchorT")):
        parsed = _number(snapshot.get(source))
        if parsed is not None:
            out[key] = parsed

    raw_cvd = bundle.get("cvd", snapshot.get("cvd"))
    meta = bundle.get("cvdMeta") if isinstance(bundle.get("cvdMeta"), dict) else (
        snapshot.get("cvdMeta") if isinstance(snapshot.get("cvdMeta"), dict) else {})
    cvd = {}
    if isinstance(raw_cvd, dict):
        for key in ("value", "fast", "slow"):
            parsed = _number(raw_cvd.get(key))
            if parsed is not None:
                cvd[key] = parsed
        if raw_cvd.get("bias"):
            cvd["bias"] = str(raw_cvd["bias"])[:16]
    elif _number(raw_cvd) is not None:
        cvd["value"] = _number(raw_cvd)
    for key, source in (("fast", "cvdFast"), ("slow", "cvdSlow")):
        if key not in cvd and _number(snapshot.get(source)) is not None:
            cvd[key] = _number(snapshot.get(source))
    history = [_number(item) for item in (meta.get("history") or [])
               if _number(item) is not None]
    if history:
        cvd["history"] = history[-INDICATOR_HISTORY_MAX:]
    if meta.get("status"):
        cvd["status"] = str(meta["status"])[:16]
    # 値しか無い CVD は top-level の ``cvd`` と同じ情報なので載せない
    # (指標束が無い旧 bundle の payload を 1 バイトも変えないため)。
    if cvd and set(cvd.keys()) != {"value"}:
        out["cvd"] = cvd

    if snapshot.get("po3"):
        out["po3"] = str(snapshot["po3"])[:32]
    smt = snapshot.get("smtObservation") if isinstance(snapshot.get("smtObservation"), dict) else None
    if smt:
        row = {}
        if smt.get("peer"):
            row["peer"] = str(smt["peer"])[:16]
        if smt.get("bias"):
            row["bias"] = str(smt["bias"])[:16]
        if isinstance(smt.get("agree"), bool):
            row["agree"] = smt["agree"]
        if _number(smt.get("position")) is not None:
            row["position"] = _number(smt["position"])
        if row:
            out["smt"] = row
    study = snapshot.get("study15m") if isinstance(snapshot.get("study15m"), dict) else {}
    ct = {}
    for key, source in _CT_KEYS.items():
        parsed = _number(study.get(source))
        if parsed is not None:
            ct[key] = parsed
    if ct:
        out["ct"] = ct
    event = snapshot.get("eventGate") if isinstance(snapshot.get("eventGate"), dict) else {}
    if event.get("state"):
        out["event"] = str(event["state"])[:24]
    return out


def build_market_payload(bundle, now=None):
    """Build the signed market stream payload from explicit observations only."""
    snapshot = bundle.get("snapshot") or {}
    source_bars = snapshot.get("bars3m") or snapshot.get("bars")
    price = _finite_number(bundle.get("price"), "price")
    if not _on_tick(price):
        raise ValueError("price is not aligned to the 0.25 MNQ tick")

    source_symbol = (
        bundle.get("sourceSymbol") or snapshot.get("sourceSymbol") or snapshot.get("symbol")
    )
    if not source_symbol or "MNQ" not in str(source_symbol).upper():
        raise ValueError("sourceSymbol must identify an MNQ feed")
    source = bundle.get("priceSource") or snapshot.get("priceSource") or snapshot.get("source")
    if not source:
        raise ValueError("priceSource is required")

    observed_raw = (
        bundle.get("priceAt") or snapshot.get("priceAt") or snapshot.get("quoteAt")
    )
    observed = _instant(observed_raw, "priceAt")
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    age = (current - observed).total_seconds()
    if age < -MARKET_FUTURE_TOLERANCE_SEC:
        raise ValueError("priceAt is too far in the future")
    if age > MARKET_MAX_AGE_SEC:
        raise ValueError(f"price observation is stale ({age:.0f}s old)")

    payload = {
        "at": observed.isoformat(),
        "observedAt": observed.isoformat(),
        "price": price,
        "change": bundle.get("change"),
        "changePct": bundle.get("changePct"),
        "vwap": bundle.get("vwap"),
        "cvd": _cvd_scalar(bundle.get("cvd", snapshot.get("cvd"))),
        "regime": bundle.get("regime"),
        "source": str(source),
        "sourceSymbol": str(source_symbol),
        "resolution": str(snapshot.get("resolution") or ""),
        "verified": True,
        # The Mini App visual feed is explicitly 3m. Falling back to the
        # canonical bars keeps older producers readable, but the new monitor
        # always supplies bars3m and never interpolates it from 15m candles.
        "bars": normalize_market_bars(source_bars),
        "barsTruncatedFrom": snapshot.get("barsTruncatedFrom") or
            (len(source_bars) if isinstance(source_bars, list) and len(source_bars) > FEED_BARS_MAX else None),
        "barResolution": "3" if snapshot.get("bars3m") else str(snapshot.get("resolution") or ""),
        "levels": snapshot.get("levels"),
        "strategyEvidence": bundle.get("strategyEvidence"),
        # R52(2026-09-01): `tv_snapshot` は CVD の観測時刻を **`cvdMeta.at` にしか
        # 書かない**。ここが top-level `cvdAt` だけを見ていたため market payload の
        # `cvdAt` は常に null で、`execution_contract._cvd_at()` が
        # CVD_TIMESTAMP_MISSING → CVD_A_PLUS_PROHIBITED を毎サイクル立て、
        # **A+ が一度も武装できなかった**(取れている値を捨てていた = CLAUDE.md §1 違反)。
        # `msnr_gate` は既に `cvdMeta.at` を CVD 時刻として使っており、これで両者が揃う。
        "cvdAt": (bundle.get("cvdAt")
                  or (bundle.get("cvdMeta") or {}).get("at")
                  or snapshot.get("cvdAt")),
        "eventBlackout": bundle.get("eventBlackout") is True,
        "dayguard": bundle.get("dayguard"),
        "sessionEndAt": bundle.get("sessionEndAt", snapshot.get("sessionEndAt")),
        "executionContractVersion": execution_contract.VERSION,
    }
    # R6: 評価カードは任意。**キーが無ければ従来と完全に同一の payload** に
    # なるよう、存在するときだけ足す(既存テストの純度検査を壊さないため)。
    evaluation = bundle.get("evaluation")
    if isinstance(evaluation, dict) and evaluation:
        payload["evaluation"] = evaluation
    # R56: チャート用の指標束。値が無ければキーごと載せない(既存 payload と同一)。
    indicators = build_indicators(bundle)
    if indicators:
        payload["indicators"] = indicators
    return payload


def encode(bundle):
    raw = json.dumps(bundle, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


# Telegram の reply_markup は JSON 全体で 4096 バイト。ボタン 1 個ぶんの
# 定型部分を引いた実効上限をここに置く。
FROZEN_PREVIEW_URL_MAX = 3500


def _frozen_preview_url(bundle):
    """正本 publish 失敗時の退避 URL。収まらなければ ``(None, "")`` を返す。

    足を段階的に削って上限へ収める。削っても収まらないときはボタンを諦める
    —— 通知本文が届くことの方が、開けないボタンより価値が高い。
    """
    snapshot = bundle.get("snapshot") if isinstance(bundle.get("snapshot"), dict) else {}
    # 監査用の重い枝(strategyEvidence / ictEvidence / evaluation)は退避 URL に
    # 載せない。凍結プレビューが要るのは「今いくらで、何を見ているか」だけで、
    # 証跡は .secrets の監査コピーと台帳に残っている。
    keep_top = ("at", "price", "priceAt", "priceSource", "sourceSymbol", "sourceFrontContract",
                "sourceContract", "sessionId",
                "vwap", "regime", "scenarios", "watching", "cvd", "cvdMeta",
                "eventBlackout", "note")
    for bars_keep, levels_keep in ((30, 12), (12, 8), (0, 6), (0, 0)):
        trimmed = {key: bundle[key] for key in keep_top if key in bundle}
        if snapshot:
            shrunk = {key: snapshot[key] for key in
                      ("at", "sessionId", "po3", "eventGate", "cvd", "cvdMeta")
                      if key in snapshot}
            for field in ("bars", "bars3m"):
                rows = snapshot.get(field)
                if isinstance(rows, list):
                    shrunk[field] = rows[-bars_keep:] if bars_keep else []
            rows = snapshot.get("levels")
            if isinstance(rows, list):
                shrunk["levels"] = rows[:levels_keep] if levels_keep else []
            trimmed["snapshot"] = shrunk
        trimmed["frozenPreviewTrimmed"] = True
        token = encode(trimmed)
        url = f"{WEB_APP_URL.rstrip('/')}/?monitor={urllib.parse.quote(token, safe='-_')}"
        if len(url.encode("utf-8")) <= FROZEN_PREVIEW_URL_MAX:
            return url, "✦ OPEN FROZEN PREVIEW"
    return None, ""


def _side_label(side):
    """BUY/LONG・SELL/SHORT を表示名に写す。未知の値は None(黙って売りにしない)。

    NQX の d=N(NEUTRAL)や表記ミスが「売り」として表示・発注される事故を防ぐ。
    """
    text = str(side or "").strip().upper()
    if text in ("BUY", "LONG"):
        return "LONG", "買い"
    if text in ("SELL", "SHORT"):
        return "SHORT", "売り"
    return None, None


def format_scenario(key, scenario):
    """CLAUDE.md §2 の提示フォーマットに合わせて 1 件を整形する。

    価格は必ず絶対値で出す。pt 幅と金額は補足として括弧で添える。
    NQX の値をそのまま写した bundle(en レンジ・TP リスト・~価格・NEUTRAL)は
    ここで落とさず、劣化カードとして事実を表示する(通知サイクルを殺さない)。
    """
    side, label = _side_label(scenario.get("side"))
    entry = _number(scenario.get("entry"))
    stop = _number(scenario.get("stop"))
    target = _number(scenario.get("target"))
    if side is None or None in (entry, stop, target):
        return (
            f"<b>⚠ シナリオ {esc(str(key))} は表示できません</b>\n"
            "side は BUY/SELL、entry/stop/target は単一の数値が必要です。\n"
            "NQX のレンジ(low..high)・TPリスト・~価格・NEUTRAL は変換してから渡すこと\n"
            "(スキルの references/operational-gates.md §4 参照)。"
        )
    risk_points = abs(entry - stop)
    reward_points = abs(target - entry)
    rr = reward_points / risk_points if risk_points else 0.0
    title = esc(str(scenario.get("title", "")).strip())
    reason = esc(str(scenario.get("reason", "")).strip())
    trigger = esc(str(scenario.get("trigger", "")).strip() or "未設定")
    invalidation = esc(str(scenario.get("invalidation", "")).strip() or "未設定")
    try:
        qty = int(scenario.get("qty") or 0)
    except (TypeError, ValueError):
        qty = 0

    heading = f"<b>{label} {side}</b>"
    if title:
        heading += f" — {title}"

    if qty > 0:
        risk_line = (
            f"リスク ${risk_points * MNQ_POINT_VALUE * qty:,.2f}"
            f"({risk_points:,.2f}pt × {qty}枚)  ·  R:R 1:{rr:.2f}"
        )
    else:
        risk_line = f"リスク {risk_points:,.2f}pt  ·  R:R 1:{rr:.2f}"

    lines = [
        heading,
        (
            "<code>"
            f"Entry: {entry:,.2f}\n"
            f"SL:    {stop:,.2f}  ({risk_points:,.2f}pt)\n"
            f"TP:    {target:,.2f}  ({reward_points:,.2f}pt)"
            "</code>"
        ),
        f"<code>{risk_line}</code>",
        f"<b>発動条件</b> {trigger}",
        f"<b>無効化</b> {invalidation}",
    ]
    if reason:
        lines.append(f"<b>根拠</b> {reason}")
    strategy_names = scenario.get("strategyModels") or []
    if isinstance(strategy_names, list) and strategy_names:
        bias = scenario.get("strategyAlignment")
        lines.append(f"<b>IMAGE MODEL MATRIX</b> {esc(' / '.join(str(x) for x in strategy_names[:8]))}"
                     + (f" · bias={esc(str(bias))}" if bias is not None else ""))
    return telegram_utf8_preflight("\n".join(lines))


def _price(value, missing="n/a"):
    """Format a price without turning missing data into a fake zero."""
    try:
        if value is None or value == "":
            return missing
        return f"{float(value):,.2f}"
    except (TypeError, ValueError):
        return missing


def _integer(value, missing="n/a"):
    """Format count-like telemetry (for example CVD) without fake decimals."""
    try:
        if value is None or value == "":
            return missing
        return f"{int(float(value)):,}"
    except (TypeError, ValueError):
        return missing


def _number(value):
    """Return a finite float, or None. 欠損をゼロに化けさせない。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _clock(value, now=None):
    """観測時刻を (HH:MM, 経過分) に分解する。

    パースできない場合は原文をそのまま返す。時刻を捏造しない。
    """
    raw = str(value or "").strip()
    if not raw:
        return "時刻不明", None
    try:
        stamp = _instant(raw, "at")
    except ValueError:
        return raw, None
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return stamp.astimezone().strftime("%H:%M"), (current - stamp).total_seconds() / 60.0


def _freshness(age_min):
    if age_min is None:
        return ""
    if age_min >= STALE_WARN_MIN:
        return f"⚠{age_min:.0f}分前"
    return f"{max(0, int(age_min))}分前"


def nearest_levels(price, levels):
    """現在値の直上・直下のレベルを 1 本ずつ返す。全部並べても状況は伝わらない。"""
    above = below = None
    for item in levels or []:
        if not isinstance(item, dict):
            continue
        level = _number(item.get("price"))
        if level is None:
            continue
        label = str(item.get("label", "")).strip() or "レベル"
        if level >= price:
            if above is None or level < above[0]:
                above = (level, label)
        elif below is None or level > below[0]:
            below = (level, label)
    return above, below


def describe_zone(price, vwap, lo, hi, levels):
    """現在値が構造のどこにいるかを一句で返す。判断材料が無ければ None。"""
    candidates = [item for item in nearest_levels(price, levels) if item]
    if candidates:
        nearest = min(candidates, key=lambda item: abs(item[0] - price))
        if abs(nearest[0] - price) <= ZONE_TOUCH_POINTS:
            return f"{nearest[1]}に接触"
    lo_f, hi_f, vwap_f = _number(lo), _number(hi), _number(vwap)
    if lo_f is None or hi_f is None or vwap_f is None or hi_f <= lo_f:
        return None
    if price >= hi_f:
        return "VWAP上限に張り付き"
    if price <= lo_f:
        return "VWAP下限に張り付き"
    ratio = (price - lo_f) / (hi_f - lo_f)
    if ratio >= 0.75:
        return "上限寄り"
    if ratio <= 0.25:
        return "下限寄り"
    return "VWAP上" if price >= vwap_f else "VWAP下"


def format_sparkline(bars, resolution="3"):
    """直近バーの終値を 1 行の波形にする。動きの有無を数字より速く伝える。"""
    closes, highs, lows = [], [], []
    for bar in bars or []:
        if not isinstance(bar, dict):
            continue
        close = _number(bar.get("c", bar.get("close")))
        high = _number(bar.get("h", bar.get("high")))
        low = _number(bar.get("l", bar.get("low")))
        if close is None or high is None or low is None:
            continue
        closes.append(close)
        highs.append(high)
        lows.append(low)
    if not closes:
        return None
    floor, ceiling = min(closes), max(closes)
    span = ceiling - floor
    if span <= 0:
        # 全バー同値。中央の文字で埋める(ゼロ除算と嘘の起伏を避ける)。
        spark = SPARK_CHARS[len(SPARK_CHARS) // 2] * len(closes)
    else:
        spark = "".join(
            SPARK_CHARS[min(len(SPARK_CHARS) - 1, int((close - floor) / span * len(SPARK_CHARS)))]
            for close in closes
        )
    try:
        label = f"{int(float(resolution)) * len(closes)}分"
    except (TypeError, ValueError):
        label = f"{len(closes)}本"
    low, high = min(lows), max(highs)
    return f"{label:<6}{spark}   レンジ {low:,.2f}–{high:,.2f}({high - low:,.1f}pt)"


def format_vwap_bar(price, lo, vwap, hi):
    """VWAP バンド内の現在位置を横バーで示す。数字 3 つより位置が分かる。"""
    price_f, lo_f, vwap_f, hi_f = (_number(v) for v in (price, lo, vwap, hi))
    if None in (price_f, lo_f, vwap_f, hi_f) or hi_f <= lo_f:
        return []
    ratio = (price_f - lo_f) / (hi_f - lo_f)
    cells = ["─"] * VWAP_BAR_CELLS
    cells[min(VWAP_BAR_CELLS - 1, max(0, int(min(1.0, max(0.0, ratio)) * VWAP_BAR_CELLS)))] = "●"
    track = "".join(cells)
    left = "◀" if ratio < 0.0 else "├"
    right = "▶" if ratio > 1.0 else "┤"
    if price_f > hi_f:
        distance = f"上限を {price_f - hi_f:,.1f}pt 上抜け"
    elif price_f < lo_f:
        distance = f"下限を {lo_f - price_f:,.1f}pt 下抜け"
    else:
        distance = f"上限まで {hi_f - price_f:,.1f}pt · 下限まで {price_f - lo_f:,.1f}pt"
    # 先頭の "VWAP帯 " は表示幅 7(帯 が全角)。数値行を同じ 7 桁で字下げして揃える。
    # 桁数が変わっても数値同士がくっつかないよう、区切りは最低 2 文字を確保する。
    track_width = VWAP_BAR_CELLS + 2
    lo_s, vwap_s, hi_s = f"{lo_f:,.0f}", f"{vwap_f:,.0f}", f"{hi_f:,.0f}"
    scale = lo_s.ljust(max(len(lo_s) + 2, (track_width - len(vwap_s)) // 2)) + vwap_s
    scale += " " * max(2, track_width - len(scale) - len(hi_s)) + hi_s
    return [
        f"VWAP帯 {left}{track}{right}",
        f"       {scale}",
        f"       {distance} · VWAP {price_f - vwap_f:+,.1f}pt",
    ]


def format_cvd(bundle, snapshot):
    """CVD を値・直近の向き・地合いで返す。EMA が無ければ方向は書かない。

    向きは CVD と EMA Fast の位置関係で見る。Fast/Slow の比較だけだと
    遅れて反応するため、CVD が既に下を向いていても「上昇」と出てしまう。
    Fast/Slow は転換の途中を可視化するため「地合い」として別に添える。
    """
    cvd_label = _integer(bundle.get("cvd", snapshot.get("cvd")), missing="n/a")
    if cvd_label == "n/a":
        return None
    cvd = _number(bundle.get("cvd", snapshot.get("cvd")))
    fast = _number(bundle.get("cvdFast", snapshot.get("cvdFast")))
    slow = _number(bundle.get("cvdSlow", snapshot.get("cvdSlow")))
    if cvd is None or fast is None:
        return f"CVD {cvd_label}"
    if cvd > fast:
        direction = "▲上昇"
    elif cvd < fast:
        direction = "▼下降"
    else:
        direction = "→横ばい"
    if slow is None or fast == slow:
        return f"CVD {cvd_label} {direction}"
    return f"CVD {cvd_label} {direction} · 地合い {'買い優勢' if fast > slow else '売り優勢'}"


def orders_today():
    """本日の発注回数を読み取る。

    ここは表示用の読み取り専用ヘルパーだけを使う。自律送信は下流の
    ``autotrade_engine`` に限定し、この関数からは送信系へ触れない。
    ``trade_date()`` の CME 営業日境界(ET 18:00 ロールオーバー)を再実装せず
    再利用するために import している。読めなければ通知から省くだけで落とさない。
    """
    try:
        from order import MAX_ORDERS_PER_DAY, count_today, load_log

        return count_today(load_log()), MAX_ORDERS_PER_DAY
    except (ImportError, OSError, ValueError, KeyError, TypeError):
        return None


def format_order_count(orders):
    """回数表示。order.py の 0=無制限契約を通知にも反映する。"""
    if not orders:
        return ""
    return f"{orders[0]}/無制限" if orders[1] == 0 else f"{orders[0]}/{orders[1]}"


def _shorten(text, limit):
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def lifeline_total():
    """全口座の残機合計。未設定・読めないときは None(表示から省くだけ)。"""
    try:
        payload = nqx_state.build_accounts_payload()
    except Exception:
        return None
    if not payload:
        return None
    return sum(item["buffer"] for item in payload["list"])


def build_headline(bundle, chosen, orders, state_ok=True, gate=None):
    """通知バナーに出る先頭 2 行。ここだけで状況が分かることを最優先にする。

    1 行目は bundle の必須フィールドだけから自動生成する。監視ループが
    ``watching`` を書き忘れても、価格・構造上の位置・発注可否は必ず出る。

    ``state_ok`` が False(正本 publish 失敗 = 凍結プレビュー)のときは、
    Mini App に発注ボタンが出ないので ACTIVE でも「発注可」とは言わない。
    ``gate`` は経済イベントの現在状態(_event_gate)。封鎖中はその事実を、
    平時は直近イベントを 2 行目に出す(動きのない時間帯にこそ価値がある)。
    """
    snapshot = bundle.get("snapshot") or {}
    price = _number(bundle.get("price", snapshot.get("price")))
    at_label, age_min = _clock(bundle.get("at"))
    state = str((chosen or {}).get("state", "")).upper()
    blackout = (gate or {}).get("blackout")

    if chosen is not None and state in ("ACTIVE", "ARMED"):
        _, side_jp = _side_label(chosen.get("side"))
        side = side_jp or "⚠サイド不明"
        entry = _number(chosen.get("entry"))
        stop = _number(chosen.get("stop"))
        target = _number(chosen.get("target"))
        armed = "発注可" if state_ok else "⚠発注不可(正本未更新)"
        first = (
            f"⚡MNQ {side} {entry:,.2f} → {target:,.2f} · SL {stop:,.2f} · {armed}"
            if None not in (entry, stop, target)
            else f"⚡MNQ {side} シナリオ {state} · {armed}"
        )
    else:
        change = _number(bundle.get("change"))
        arrow = "・" if not change else ("▲" if change > 0 else "▼")
        move = f" {arrow}{change:+,.0f}" if change else ""
        head = f"MNQ {price:,.2f}{move}" if price is not None else "MNQ 価格取得不可"
        zone = describe_zone(
            price,
            bundle.get("vwap", snapshot.get("vwap")),
            snapshot.get("vwap_lo"),
            snapshot.get("vwap_hi"),
            snapshot.get("levels"),
        ) if price is not None else None
        if blackout:
            tail = "⚠イベント封鎖(発注不可)"
        else:
            tail = "監視中(発注不可)" if chosen is not None else "待機"
        first = " · ".join(part for part in (head, zone, tail) if part)

    tail_parts = []
    if chosen is not None:
        title = _shorten(chosen.get("title") or chosen.get("reason") or "", HEADLINE_REASON_CHARS)
        entry, stop, target = (
            _number(chosen.get("entry")), _number(chosen.get("stop")), _number(chosen.get("target"))
        )
        if title:
            tail_parts.append(title)
        if None not in (entry, stop, target) and abs(entry - stop) > 0:
            tail_parts.append(f"R:R 1:{abs(target - entry) / abs(entry - stop):.2f}")
    else:
        watching = bundle.get("watching") or []
        if isinstance(watching, str):
            watching = [watching]
        if isinstance(watching, list) and watching:
            tail_parts.append(_shorten(watching[0], HEADLINE_REASON_CHARS))
    if blackout:
        until = _event_until(blackout)
        tail_parts.append(
            f"☄{_shorten(blackout.get('title'), 12)}" + (f" 〜{until}" if until else " 封鎖中"))
    else:
        nearest = ((gate or {}).get("upcoming") or [None])[0]
        if nearest:
            try:
                delta_min = (econ_events._parse_at(nearest["at"])
                             - datetime.now(timezone.utc)).total_seconds() / 60.0
            except (ValueError, TypeError, KeyError):
                delta_min = None
            if delta_min is not None and delta_min <= 90:
                mark = "☄" if nearest.get("impact") == "High" else "◇"
                tail_parts.append(
                    f"{mark}{econ_events.fmt_local_hm(nearest)} "
                    f"{_shorten(nearest.get('title'), 12)}")
    if orders:
        tail_parts.append(f"発注 {format_order_count(orders)}")
    fresh = _freshness(age_min)
    tail_parts.append(f"{at_label}({fresh})" if fresh else at_label)

    return [f"<b>{esc(first)}</b>", esc(" · ".join(tail_parts))]


def build_monitor_message(bundle, state_ok, execution_notes=None):
    """Telegram に送る本文を組み立てる。

    先頭 2 行は通知バナーに出るサマリ(``build_headline``)。以降が本文で、
    価格の羅列ではなく波形・バンド位置・直近レベルという視覚要素で状況を出す。
    表示するシナリオは ``publish_state`` と同じ 1 件だけで、古い 2 件目が
    Telegram 側に漏れることはない。
    """
    snapshot = bundle.get("snapshot") or {}
    symbol = str(snapshot.get("symbol") or "CME_MINI:MNQ1!")
    resolution = str(snapshot.get("resolution") or "3")
    resolution_label = resolution if resolution.lower().endswith("m") else f"{resolution}分足"
    regime = str(bundle.get("regime", "MX"))
    price = _number(bundle.get("price", snapshot.get("price")))

    key, chosen, dropped = select_active_scenario(bundle.get("scenarios") or {})
    orders = orders_today()

    # 経済イベントゲート。封鎖中は表示上も ARMED/ACTIVE を WATCH に降格し、
    # publish_state 側の降格(同じ規則)と食い違わないようにする。
    gate = _event_gate()
    blackout = gate.get("blackout")
    if (chosen is not None and blackout
            and str(chosen.get("state", "WATCH")).upper() in ("ACTIVE", "ARMED")):
        chosen = {**chosen, "state": "WATCH", "_eventDemoted": blackout}

    # ボラ予算ゲートも publish_state と同じ規則で表示側に反映する。
    vg = vol_gate(bundle)
    if (chosen is not None and vg.get("standdown")
            and str(chosen.get("state", "WATCH")).upper() in ("ACTIVE", "ARMED")):
        chosen = {**chosen, "state": "WATCH", "_volDemoted": vg}

    lines = build_headline(bundle, chosen, orders, state_ok, gate)
    lines += [
        "",
        f"<b>⟦ ✦ NIGHTWATCH ⟧</b>  <code>{esc(symbol)} · {esc(resolution_label)} · {esc(regime)}</code>",
    ]

    # --- 市況の詳細は折りたたみ(expandable blockquote)に収める。
    # 既読画面は数行の静かなカードになり、開けば波形・帯・レベルが見える。
    # 等幅が要る行は <pre>、可変幅の日本語ラベルは行末に置く。
    visual = []
    # 通知の波形は直近12本に固定(バナー幅の制約。チャート本数とは独立)
    spark = format_sparkline((snapshot.get("bars3m") or snapshot.get("bars") or [])[-12:], resolution)
    if spark:
        visual.append(spark)
    if price is not None:
        bar = format_vwap_bar(
            price,
            snapshot.get("vwap_lo"),
            bundle.get("vwap", snapshot.get("vwap")),
            snapshot.get("vwap_hi"),
        )
        if bar:
            if visual:
                visual.append("")
            visual.extend(bar)
        above, below = nearest_levels(price, snapshot.get("levels"))
        level_rows = []
        for label, item in (("直上", above), ("直下", below)):
            if not item:
                continue
            level, name = item
            level_rows.append(f"{label} {level - price:>+6.1f}  {level:>10,.2f}  {esc(name)}")
        if level_rows:
            visual.append("")
            visual.extend(level_rows)

    facts = []
    cvd_line = format_cvd(bundle, snapshot)
    if cvd_line:
        facts.append(cvd_line)
    vix = _price(bundle.get("vix", snapshot.get("vix")), missing="n/a")
    if vix != "n/a":
        delayed = "遅延" if bundle.get("vixDelayed", snapshot.get("vixDelayed", True)) else "実測"
        vix_source = str(bundle.get("vixSource") or snapshot.get("vixSource") or "")
        suffix = f" · {vix_source}" if vix_source else ""
        facts.append(f"VIX {vix}({delayed}){suffix}")
    position = bundle.get("position")
    if position:
        facts.append(f"建玉 {position}")
    events_line = " / ".join(
        f"{econ_events.fmt_local_hm(item)} {_shorten(item.get('title'), 18)}"
        f"({'高' if item.get('impact') == 'High' else '中'})"
        for item in (gate.get("upcoming") or [])[:3])
    if events_line:
        facts.append(f"指標 {events_line}")
    if gate.get("notes"):
        # カレンダー未取得・陳腐化中は封鎖判定が効いていない。隠さず出す。
        facts.append("⚠指標カレンダー無効(python events.py --refresh)")
    vol_line = format_vol_gate(vg)
    if vol_line:
        # 通ったことも記録に残す。沈黙は「見ていない」と区別がつかない。
        facts.append(vol_line)

    detail = []
    if visual:
        detail.append("<pre>" + "\n".join(visual) + "</pre>")
    if facts:
        detail.append(f"<code>{esc('  ·  '.join(facts))}</code>")
    if cvd_line:
        # 免責は CVD を出した詳細ブロックの中に置く(フッターを汚さない)。
        detail.append("<i>※ CVD は観測値。注文フローには変換していません。</i>")
    if detail:
        lines += ["", "<blockquote expandable>" + "\n".join(detail) + "</blockquote>"]

    # --- シナリオ / 待機。ACTIVE か WATCH かは「発注ボタンが出るか」と同義なので、
    # 比喩ではなくその事実をそのまま書く(CLAUDE.md §7)。
    # グリフは Mini App と同じ言語: ◆=発注可 ◇=監視 ✧=なし(月表現は2026-08-16に全廃)。
    lines.append("")
    if chosen is not None:
        state = str(chosen.get("state", "WATCH")).upper()
        if state in ("ACTIVE", "ARMED"):
            if state_ok:
                lines.append(f"<b>◆ シナリオ {esc(state)} — 発注ボタン 表示中</b>")
            else:
                # 凍結プレビューでは Mini App にボタンが出ない。事実だけを書く。
                lines.append(
                    f"<b>◇ シナリオ {esc(state)} — ⚠ 正本更新失敗のため発注ボタンは出ません</b>"
                )
        else:
            demoted = chosen.get("_eventDemoted")
            vol_demoted = chosen.get("_volDemoted")
            if demoted:
                until = _event_until(demoted)
                lines.append(
                    "<b>◇ シナリオ WATCH — ☄ "
                    f"{esc(str(demoted.get('title', '')))} 封鎖中につき降格"
                    + (f"(〜{esc(until)})" if until else "")
                    + " — 発注ボタンは出ません</b>"
                )
            elif vol_demoted:
                ratio_txt = "{:.2f}".format(vol_demoted.get("ratio_all") or 0.0)
                limit_txt = "{:.2f}".format(VOL_GATE_STANDDOWN)
                lines.append(
                    "<b>◇ シナリオ WATCH — ボラ床が SL 予算を圧迫(比率 "
                    f"{esc(ratio_txt)} > {esc(limit_txt)})につき降格 — 発注ボタンは出ません</b>"
                )
            else:
                lines.append("<b>◇ シナリオ WATCH — 発注ボタンは出ません(NOT ARMED)</b>")
        lines.append(format_scenario(key, chosen))
        if dropped:
            lines.append(
                f"<i>他 {len(dropped)} 件は非表示(表示は1件のみ): {esc(', '.join(dropped))}</i>"
            )
    else:
        lines.append("<b>✧ シナリオなし — 発注ボタンは出ていません</b>")
        watching = bundle.get("watching") or []
        if isinstance(watching, str):
            watching = [watching]
        if isinstance(watching, list) and watching:
            lines.append("<b>待っているもの</b>")
            lines.extend(f" ・{esc(_shorten(item, 60))}" for item in watching[:3])
            lines.append("<i>どれも出ていません。</i>")
        else:
            lines.append("<i>条件未成立。</i>")

    # --- フッターは毎回同じ場所・同じ順(残機 → 回数 → 正本)。
    footer = []
    lifeline = lifeline_total()
    if lifeline is not None:
        footer.append(f"残機 ${lifeline:,.0f}")
    if orders:
        footer.append(f"本日 {format_order_count(orders)}")
    footer.append("正本 更新済み" if state_ok else "⚠ 正本 更新失敗 — 凍結プレビュー")
    # 武装状態は毎回出す。「出ているつもりで出ていない」を通知だけで気づける。
    try:
        footer.append("自律 " + autotrade_arm.summary())
    except Exception:  # noqa: BLE001 — 表示の失敗で通知を落とさない
        footer.append("自律 状態不明")
    lines += ["", f"<code>{esc(' · '.join(footer))}</code>"]
    if execution_notes:
        clean = [_shorten(str(note), 120) for note in execution_notes[:2]]
        lines += ["", "<code>自律執行 · " + esc(" / ".join(clean)) + "</code>"]
    return telegram_utf8_preflight("\n".join(lines))


def fingerprint(bundle):
    """Stable identity for a frozen market, scenario and evaluation card.

    旧実装は at/price/regime/scenarios だけを比較していたため、同じスナップ
    ショットを新しい判定ロジックで再評価しても、MSNR/CVD/DATAゲートの修正が
    duplicate として破棄された。評価カードは Mini App の正本表示なので、
    その変更も publish 対象に含める。
    """
    return json.dumps({
        "at": bundle.get("at"),
        "price": bundle.get("price"),
        "regime": bundle.get("regime"),
        "scenarios": bundle.get("scenarios") or {},
        "evaluation": bundle.get("evaluation") or {},
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def select_active_scenario(scenarios):
    """表示できるのは 1 件だけ。どれを採るかをここで決め、落としたものを返す。

    ARMED/ACTIVE を WATCH より優先する。同順位なら宣言順の先頭。
    落としたシナリオは黙って消さず、呼び出し側がログに出す。
    """
    ranked = []
    for key, scenario in (scenarios or {}).items():
        if not isinstance(scenario, dict):
            continue
        state = str(scenario.get("state", "WATCH")).upper()
        rank = {"ACTIVE": 0, "ARMED": 0}.get(state, 1)
        ranked.append((rank, len(ranked), key, scenario))
    if not ranked:
        return None, None, []
    ranked.sort()
    _, _, key, chosen = ranked[0]
    dropped = [entry[2] for entry in ranked[1:]]
    return key, chosen, dropped


def _send_scenario_card(cfg, bundle):
    """A/A+ の武装シナリオがあるときだけチャート画像を送る。失敗は握り潰して本文へ進む。"""
    try:
        primary = ((bundle.get("scenarios") or {}).get("primary") or {}) if isinstance(bundle, dict) else {}
        if not isinstance(primary, dict) or str(primary.get("state") or "").upper() not in {"ARMED", "ACTIVE"}:
            return None
        if str(primary.get("grade") or "") not in {"A", "A+", "B"}:
            return None
        import scenario_card
        from telegram_bot import send_photo
        png = scenario_card.render_png(bundle)
        if not png:
            return None
        return send_photo(cfg, png, caption=scenario_card.caption(bundle))
    except Exception as exc:  # noqa: BLE001 - 画像は補助。本文の通知を止めない。
        print(f"scenario card skipped: {type(exc).__name__}: {exc}")
        return None


POSITION_CARD_SENT = os.path.join(".secrets", "position_card_sent.json")


def _send_position_card(cfg, bundle):
    """保有中のカード(TP1/TP2/SL の到達時損益つき)を送る。

    **毎サイクル送らない。** 3分ごとに同じ絵が流れると通知が読めなくなるので、
    契約側が変わったときだけ送る —— 新規約定 / TP1 が抜けて runner になった /
    SL を建値へ寄せた、の3つ。含み損益は fingerprint に入れないので、
    値動きだけでは再送されない。

    失敗は握り潰す。画像は補助であって、本文と発注の経路を止めない。

    入口は `bundle["position"]`(= live_position_line が verified を確認して
    置いた1行)。これが無いサイクルでは描かない —— 建玉照会をもう一周する
    必要が無いし、大半のサイクルは FLAT なので口座数ぶんの REST を毎回
    無駄打ちすることになる。
    """
    if not (isinstance(bundle, dict) and bundle.get("position")):
        _forget_position_card()
        return None
    try:
        import position_card
        from telegram_bot import send_photo

        price = _number(bundle.get("price"))
        png, text, derived = position_card.render_open_card(last=price)
        fingerprint = position_card.card_fingerprint(derived)
    except Exception as exc:  # noqa: BLE001 - NoCard(建玉なし)もここに落ちる
        if type(exc).__name__ != "NoCard":
            print(f"position card skipped: {type(exc).__name__}: {exc}")
        _forget_position_card()
        return None

    try:
        with open(POSITION_CARD_SENT, "r", encoding="utf-8") as fh:
            previous = json.load(fh).get("fingerprint")
    except (FileNotFoundError, json.JSONDecodeError, OSError, AttributeError):
        previous = None
    if previous == fingerprint:
        return None

    sent = send_photo(cfg, png, caption=text)
    if sent:
        try:
            os.makedirs(os.path.dirname(POSITION_CARD_SENT), exist_ok=True)
            with open(POSITION_CARD_SENT, "w", encoding="utf-8") as fh:
                json.dump({"fingerprint": fingerprint,
                           "at": datetime.now(timezone.utc).isoformat()}, fh)
        except OSError as exc:
            # 記録できなくても送信自体は成功している。次サイクルで再送される
            # かもしれないが、送らないより誤解が少ない。
            print(f"position card fingerprint not saved: {exc}")
    return sent


def _forget_position_card():
    """建玉が無い/読めないときは記録を消す。次に持ったら必ず1枚送るため。"""
    try:
        os.remove(POSITION_CARD_SENT)
    except OSError:
        pass


def _apply_ultra_prefs(chosen, scenario, contract, market, execution_cfg, cfg,
                       cycle_id, event_blackout):
    """口座別 ULTRA 設定(Mini App 保存)を武装候補へ適用する(R47)。

    ユーザー決定(2026-08-30): **達成不能なら見送り**。ULTRA 対象口座が
    設定されている間、通常2枚への切り替えは行わない — 目標に届く枚数が
    出せないシナリオは WATCH に降格し、理由を注記に残す。

    返り値 ``(chosen, scenario, contract, notes)``。ULTRA 対象が無ければ無変更。
    ここは publish 前のサイジングであり、発注そのものは従来どおり
    engine / order.py の全ゲートを通る。
    """
    notes = []
    state = str(scenario.get("state") or "").upper()
    if state not in ("ARMED", "ACTIVE"):
        return chosen, scenario, contract, notes

    try:
        prefs = nqx_state.fetch_account_prefs(cfg)
    except Exception as exc:  # noqa: BLE001 — 確認できないときは ULTRA を発火させない
        notes.append(f"ultra prefs unavailable ({type(exc).__name__}) — normal sizing")
        return chosen, scenario, contract, notes
    ultra_ids = [key for key, value in prefs.items() if value.get("ultra") is True]
    if not ultra_ids:
        return chosen, scenario, contract, notes

    def _demote(reason):
        demoted = {**chosen, "state": "WATCH"}
        try:
            rebuilt = nqx_state.build_scenario(
                {**demoted, "scenarioId": demoted.get("scenarioId"), "marketCycleId": cycle_id},
                observed_at=scenario.get("observedAt"),
                ttl_minutes=SCENARIO_TTL_MIN, symbol=cfg.get("NQX_SYMBOL", contract_month.symbol()))
        except (ValueError, TypeError):
            rebuilt = {**scenario, "state": "WATCH"}
        if demoted.get("grade") in ("A+", "A", "B"):
            rebuilt = {**rebuilt, "grade": demoted["grade"]}
        rebuilt["executionContract"] = execution_contract.evaluate(
            rebuilt, market, None, None, cfg=execution_cfg, event_blackout=event_blackout)
        notes.append(f"ULTRA skip: {reason} — scenario demoted to WATCH (見送り)")
        return demoted, rebuilt, rebuilt["executionContract"], notes

    if len(ultra_ids) > 1:
        return _demote("multiple ULTRA accounts configured (claim scope allows one)")
    account_id = ultra_ids[0]
    scope = contract.get("accountScope") or []
    if account_id not in scope:
        return _demote(f"ULTRA account …{account_id[-6:]} is not in the executable scope")

    try:
        import ultra_mode
        accounts_payload = nqx_state.build_accounts_payload(env=execution_cfg, prefs=prefs)
    except Exception as exc:  # noqa: BLE001
        return _demote(f"account data unavailable ({type(exc).__name__})")
    rows = (accounts_payload or {}).get("list") or []
    row = next((item for item in rows if str(item.get("id")) == account_id), None)
    if row is None:
        return _demote(f"ULTRA account …{account_id[-6:]} has no lifeline data")

    pref = prefs.get(account_id) or {}
    effective = dict(row)
    try:
        pref_dd = pref.get("maxDrawdown")
        if pref_dd is not None and float(pref_dd) > 0:
            effective["buffer"] = min(float(row.get("buffer") or 0.0), float(pref_dd))
    except (TypeError, ValueError):
        pass

    targets = chosen.get("targets") or [chosen.get("target")]
    # R52(2026-09-05 ユーザー決定): ULTRA のサイジングは **TP1 基準**で3経路を揃える。
    # Mini App(ultraPanel)と Bot(_ultra_order_gate → signal_from_scenario)は runner を
    # 渡さず TP1 だけで枚数を出すのに、auto 経路だけが runner を渡して R30 の分割式
    # (両脚到達で目標)になっていた。同じ幾何で app 18枚 / auto 8枚に割れ、ユーザーは
    # app の枚数を「ULTRA 枚数」として手動発注した。runner の目標価格は脚(legs)に
    # そのまま残す —— 変えるのは枚数の根拠だけで、執行の幾何は変えない。
    signal = {"side": chosen.get("side"), "entry": chosen.get("entry"),
              "stop": chosen.get("stop"), "target": targets[0]}
    plan = ultra_mode.build_plan(signal, [effective])
    prow = (plan.get("accounts") or [{}])[0]
    envelope = execution_contract.ultra_evaluate(plan)
    if prow.get("verdict") != "ELIGIBLE" or prow.get("contractBlockers") or not envelope.get("ok"):
        reasons = list(prow.get("reasons") or []) + list(prow.get("contractBlockers") or [])
        reasons += [b for b in envelope.get("blockers") or [] if b not in reasons]
        return _demote(f"…{account_id[-6:]} target unreachable ({', '.join(reasons) or 'INELIGIBLE'})")

    qty = int(prow["qty"])
    leg_split = execution_contract.ultra_split(qty)
    resized = {**chosen, "qty": qty,
               "legs": [{"id": "TP1", "qty": leg_split[0], "target": targets[0]},
                        {"id": "RUNNER", "qty": leg_split[1], "target": targets[-1]}]}
    try:
        rebuilt = nqx_state.build_scenario(
            {**resized, "scenarioId": resized.get("scenarioId"), "marketCycleId": cycle_id},
            observed_at=scenario.get("observedAt"), ttl_minutes=SCENARIO_TTL_MIN,
            symbol=cfg.get("NQX_SYMBOL", contract_month.symbol()), ultra=True)
    except (ValueError, TypeError) as exc:
        return _demote(f"ultra scenario rebuild failed ({exc})")
    if resized.get("grade") in ("A+", "A", "B"):
        rebuilt = {**rebuilt, "grade": resized["grade"]}
    ultra_cfg = {**execution_cfg, "CROSSTRADE_ACCOUNTS": account_id}
    ultra_contract = execution_contract.evaluate(
        rebuilt, market, None, None, cfg=ultra_cfg, event_blackout=event_blackout,
        ultra=True, ultra_buffer=effective.get("buffer"))
    rebuilt["executionContract"] = ultra_contract
    notes.append(
        f"ULTRA sized: …{account_id[-6:]} qty {qty} (TP1 {leg_split[0]} / RUNNER {leg_split[1]}) "
        f"· projected profit ${prow.get('projectedProfit'):,.0f} / loss ${prow.get('projectedLoss'):,.0f}")
    return resized, rebuilt, ultra_contract, notes


def _sync_position_to_worker(state_ok):
    """ブローカー建玉を Worker の position stream へ同期する(R52)。

    戻り値は注記のリスト(成功時は空)。正本が更新できていない周期は触らない。
    照会不能は `sync_position` が STALE として publish するので、ここで握らない。
    失敗しても監視サイクルは止めない(建玉管理は engine が別途ブローカー照会で
    fail-closed に守る)。
    """
    if not state_ok:
        return []
    try:
        ok, detail = nqx_state.sync_position()
    except Exception as exc:  # noqa: BLE001
        return [f"position sync failed ({type(exc).__name__}: {exc})"]
    if not ok:
        return [f"position sync failed: {detail}"]
    return []


def publish_state(bundle):
    """Durable Object へ market と scenario を publish する。

    送信はここでは行わない。publish に失敗しても Telegram 通知は行う
    (通知が止まる方が危険なため)。ただし成功したふりはしない。
    """
    notes = []
    try:
        cfg = nqx_state.load_cloud_env()
    except nqx_state.NotConfigured as exc:
        return False, [f"cloudflare state is not configured ({exc})"]
    # Cloudflare の接続設定だけでは、実行契約が凍結すべき送信先口座を持たない。
    # 発注先の正本は order.py と同じ crosstrade.env なので、契約評価用だけを
    # 明示的に合成する。publish 用の cfg は外部接続情報のまま保持する。
    try:
        execution_env = nqx_state._read_kv_env(nqx_state.CROSSTRADE_ENV)
    except (AttributeError, OSError):
        # テスト用/縮退 nqx_state でも通知経路は落とさない。実運用では
        # accountScope が空になり execution_contract が新規を止める。
        execution_env = {}
    execution_cfg = {**execution_env, **cfg}

    # Event status is a contractual observation, not Telegram-only copy.
    pre_gate = _event_gate()
    bundle["eventBlackout"] = bool(pre_gate.get("blackout"))
    # dayguard は 2026-08-27 に**停止条件としては廃止**した。日次損失・DAYGOAL・
    # 2連敗・冷却で新規を止めるのはやめ、集計だけを残す。
    #
    # ただし観測フィールド自体は出し続ける。デプロイ済み Worker の
    # ENTRY_CLAIM_DAYGUARD_INVALID は `state.market.dayguard` の **存在** を
    # 要求しており、bundle から落とすと全 ENTRY claim が拒否される。
    # 集計に失敗しても available:true / blocked:false で通す —— 台帳の不備で
    # 発注を止めるのは、ガードを外した以上もう筋が通らない。
    guard_now = datetime.now(timezone.utc)
    try:
        import dayguard
        guard_env = dict(os.environ)
        guard_env.update(execution_cfg)
        guard = dayguard.check(now=guard_now, env=guard_env)
    except Exception as exc:                     # noqa: BLE001 - 集計は表示専用
        guard = None
        notes.append(f"dayguard summary unavailable: {type(exc).__name__}: {exc}")
    bundle["dayguard"] = {"at": guard_now.isoformat(), "available": True,
                          "blocked": False, "codes": []}
    try:
        market = build_market_payload(bundle)
    except (TypeError, ValueError) as exc:
        # An unverified/stale price must revoke the previous market/scenario
        # pair as a single cycle.  A legacy clear_scenario call here would
        # create an observable split between streams.
        notes.append(f"market rejected before publish: {exc}")
        disarm_material = json.dumps({
            "at": bundle.get("at"), "priceAt": bundle.get("priceAt"),
            "sourceSymbol": bundle.get("sourceSymbol"), "reason": str(exc),
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        cycle_id = "cy_disarm_" + hashlib.sha256(disarm_material).hexdigest()[:20]
        ok, detail = nqx_state.publish_cycle(cycle_id, None, None, cfg)
        notes.append("cycle disarmed" if ok else f"cycle disarm failed: {detail}")
        return False, notes

    # R15: market and its executable scenario are one atomic state pair in
    # the Durable Object.  The cycle id is content-addressed before either is
    # published; a partial network failure cannot produce a fresh market plus
    # an old armed scenario.
    cycle_material = json.dumps(market, ensure_ascii=False, sort_keys=True,
                                separators=(",", ":")).encode("utf-8")
    cycle_id = "cy_" + hashlib.sha256(cycle_material).hexdigest()[:24]
    market = {**market, "cycleId": cycle_id}

    # 口座残機(LIFELINE)。設定が無ければ黙って飛ばす。失敗しても
    # 監視は止めない(残機は市場データと違い、次のサイクルで自然に直る)。
    try:
        acc_ok, acc_detail = nqx_state.publish_accounts(cfg)
        if acc_ok:
            notes.append("accounts ok")
        elif "not configured" not in str(acc_detail.get("reason", "")):
            notes.append(f"accounts failed: {acc_detail}")
    except Exception as exc:  # 残機の publish 失敗で市況とシナリオを道連れにしない
        notes.append(f"accounts failed: {type(exc).__name__}: {exc}")

    key, chosen, dropped = select_active_scenario(bundle.get("scenarios"))
    if dropped:
        # 黙って切り捨てない。何を表示しなかったかを必ず残す。
        notes.append(f"dropped scenarios (only one can be active): {', '.join(dropped)}")

    if chosen is None:
        bundle["_published_scenario"] = None
        cycle_ok, cycle_detail = nqx_state.publish_cycle(cycle_id, market, None, cfg)
        notes.append("cycle disarmed" if cycle_ok else f"cycle disarm failed: {cycle_detail}")
        return cycle_ok, notes

    chosen, qty_notes = normalize_qty(chosen, execution_cfg)
    notes.extend(qty_notes)

    # NQX 値の写経事故を publish 前に止める。side が BUY/SELL に解決できない
    # (d=N の NEUTRAL など)ものは発注根拠にならないので publish しない。
    if _side_label(chosen.get("side"))[0] is None:
        bundle["_published_scenario"] = None
        notes.append(
            f"scenario rejected before publish: side {chosen.get('side')!r} is not BUY/SELL"
        )
        cycle_ok, cycle_detail = nqx_state.publish_cycle(cycle_id, market, None, cfg)
        notes.append("cycle disarmed" if cycle_ok else f"cycle disarm failed: {cycle_detail}")
        return False, notes

    # 未知の state は Worker が ingest ごと拒否し、typo 1 つで通知サイクル全体が
    # 凍結プレビューに落ちる。発注ボタンの出ない WATCH に降格して publish は生かす。
    state = str(chosen.get("state", "WATCH")).upper()
    if state not in ("WATCH", "ACTIVE", "ARMED"):
        notes.append(f"unknown scenario state {state!r} — demoted to WATCH (fail-safe)")
        chosen = {**chosen, "state": "WATCH"}

    # 重要指標ブラックアウト(High の前後)。武装したまま指標に突っ込ませない。
    # カレンダー未取得(notes あり)のときは降格しない(fail-open)が、必ず記録する。
    gate = _event_gate()
    for note in gate.get("notes") or []:
        notes.append(f"events: {note}")
    blackout = gate.get("blackout")
    if blackout and str(chosen.get("state", "WATCH")).upper() in ("ACTIVE", "ARMED"):
        notes.append(
            f"event blackout ({blackout.get('title')}) — scenario demoted to WATCH")
        chosen = {**chosen, "state": "WATCH"}

    # R102 満期ガード。発注先限月の満期まで contract.lastEntryDaysBeforeExpiry 日未満は
    # 新規を武装させない(管理・撤退は engine 側でこの判定を見ない)。契約ブロックが
    # 壊れているときは fail-closed で WATCH に落とす(限月不明のまま送らせない)。
    try:
        entry_ok, entry_reason = contract_month.entry_allowed()
    except contract_month.ContractError as exc:
        entry_ok, entry_reason = False, f"CONTRACT_INVALID: {exc}"
    if not entry_ok and str(chosen.get("state", "WATCH")).upper() in ("ACTIVE", "ARMED"):
        notes.append(f"contract expiry gate ({entry_reason}) — scenario demoted to WATCH")
        chosen = {**chosen, "state": "WATCH"}

    # ボラ予算ゲート。緩衝だけで SL 予算が尽きる相場では武装させない。
    # 封鎖と同じく fail-open(本数不足・設定不備では降格しない)。
    vg = vol_gate(bundle)
    if not vg.get("active"):
        notes.append(f"vol gate inactive: {vg.get('reason')}")
    else:
        chosen, vol_note = apply_volatility_grade_gate(chosen, vg)
        if vol_note:
            notes.append(vol_note)

    # 日次ガードによる WATCH 降格は廃止(2026-08-27)。日次損失・DAYGOAL・
    # 2連敗・冷却では武装を降ろさない。該当したことは注記だけ残す。
    if guard:
        for warning in guard.get("warnings") or []:
            notes.append(f"day ledger: {warning}")
        for note in guard.get("notes") or []:
            notes.append(f"day summary: {note['code']} — {note['detail']} (guard retired)")

    # Keep the post-gate scenario for the execution lifecycle.  The input
    # bundle may still contain the pre-gate ARMED proposal; using that value
    # here would allow a blackout/volatility/dayguard demotion to be bypassed.
    bundle["_published_scenario"] = dict(chosen)

    try:
        scenario = nqx_state.build_scenario(
            {**chosen, "scenarioId": chosen.get("scenarioId"), "marketCycleId": cycle_id},
            observed_at=bundle.get("at"),
            ttl_minutes=SCENARIO_TTL_MIN,
            symbol=cfg.get("NQX_SYMBOL", contract_month.symbol()),
        )
    except (ValueError, TypeError) as exc:
        bundle["_published_scenario"] = None
        notes.append(f"scenario rejected before publish: {exc}")
        cycle_ok, cycle_detail = nqx_state.publish_cycle(cycle_id, market, None, cfg)
        notes.append("cycle disarmed" if cycle_ok else f"cycle disarm failed: {cycle_detail}")
        return False, notes

    # R6: 発注ボタンの等級バッジ。nqx_state は無変更の制約があるので、
    # build_scenario の返り値へここで足す(A+ / A / B 以外は Worker が null に倒す)。
    # 2026-09-04: B も発注可能(ユーザー決定)。
    grade = chosen.get("grade")
    if grade in ("A+", "A", "B"):
        scenario = {**scenario, "grade": grade}

    contract = execution_contract.evaluate(
        scenario, market, None, None, cfg=execution_cfg,
        event_blackout=bundle["eventBlackout"])
    scenario["executionContract"] = contract
    # A CVD timestamp gap never permits an A+ packet to enter the shared
    # state. Rebuild so the revised grade participates in its fingerprint.
    if contract.get("effectiveGrade") and contract["effectiveGrade"] != scenario.get("grade"):
        chosen = {**chosen, "grade": contract["effectiveGrade"]}
        scenario = nqx_state.build_scenario(
            {**chosen, "scenarioId": chosen.get("scenarioId"), "marketCycleId": cycle_id},
            observed_at=bundle.get("at"), ttl_minutes=SCENARIO_TTL_MIN,
            symbol=cfg.get("NQX_SYMBOL", contract_month.symbol()))
        scenario["executionContract"] = execution_contract.evaluate(
            scenario, market, None, None, cfg=execution_cfg,
            event_blackout=bundle["eventBlackout"])
        notes.append("execution contract capped A+ to A pending CVD timestamp reacquisition")

    # R47: 口座別 ULTRA 設定(Mini App 保存)の適用。対象口座があるとき、
    # 目標に届く枚数へリサイズするか、届かなければ WATCH へ見送る。
    chosen, scenario, _ultra_contract, ultra_notes = _apply_ultra_prefs(
        chosen, scenario, scenario.get("executionContract") or {}, market,
        execution_cfg, cfg, cycle_id, bundle["eventBlackout"])
    notes.extend(ultra_notes)

    # autotrade_engine は publish 後の同じ bundle を受け取る。ここに pre-build の
    # chosen を残すと accountScope / fingerprint / marketCycleId が欠け、Worker は
    # 正しい2口座契約を持っていてもローカル handoff が空口座で停止する。
    bundle["_published_scenario"] = dict(scenario)
    cycle_ok, detail = nqx_state.publish_cycle(cycle_id, market, scenario, cfg)
    notes.append(f"cycle {cycle_id} ok" if cycle_ok else f"cycle failed: {detail}")
    return cycle_ok, notes


def _record_closed_trades(bundle):
    """決済済みの建玉を LEDGER(Mini App の戦績)へ記録する。

    ここが**自動記録の唯一の起動点**である。以前は決済の検知が
    ``telegram_bot`` 常駐プロセスの中にしか無く、3分ループからは一度も
    呼ばれていなかったため、2026-08-25 以降の実トレードが1件も戦績に
    載っていなかった(DO の recentResults は 4 件・全て手入力)。

    **読むだけ。** 建玉と残高を照会し、閉じたものを publish するだけで、
    ``order.py`` にも CrossTrade の書き込み系にも触れない。記録に失敗しても
    監視サイクルは止めない —— 記録の失敗で発注経路を巻き込まないため。
    """
    try:
        import autotrade_engine
        import trade_journal
    except ImportError as exc:
        return [f"trade journal unavailable: {exc}"]
    try:
        cfg = {**nqx_state._read_kv_env(nqx_state.CROSSTRADE_ENV), **os.environ}
    except (AttributeError, OSError):
        cfg = dict(os.environ)
    accounts = autotrade_engine._multi_scope(cfg)
    if not accounts:
        return []
    try:
        live = autotrade_engine.live_enabled(cfg)
    except Exception:                            # noqa: BLE001 — 表示上のモードだけ
        live = False
    try:
        # R85: 手数料は `FEE_PER_SIDE_<口座ID>` があれば約定枚数 × 単価(R54 の残高差分より優先)。
        fee_rates = trade_journal.fee_rates_from_env(cfg, accounts)
    except Exception:                            # noqa: BLE001 — 手数料は任意
        fee_rates = {}
    try:
        return trade_journal.reconcile(
            bundle, accounts=accounts,
            mode="LIVE" if live else "SIMULATION", fee_rates=fee_rates)
    except Exception as exc:                     # noqa: BLE001 — 記録は監視を止めない
        return [f"trade journal failed: {type(exc).__name__}: {exc}"]


def live_position_line(bundle):
    """保有中なら含み損益を1行にして返す。保有していない/照会できないなら None。

    **表示専用。** ここでは発注も変更もしない。照会が verified でなければ黙って
    None を返す —— 建玉の有無を推測して書かない(書けば誤報になる)。

    Mini App の監視フィードと Telegram 本文の両方が `bundle["position"]` を
    読むので、ライブ損益はここに1本だけ置けば両方に出る。
    """
    price = _number(bundle.get("price"))
    if price is None:
        return None
    try:
        import broker_status
    except ImportError:
        return None

    accounts = []
    for adapter in broker_status.adapters():
        accounts = list(getattr(adapter, "accounts", None) or [])
        if accounts:
            break
    if not accounts:
        return None

    held = []
    for account in accounts:
        try:
            row = broker_status.query_position(account=account)
        except Exception:  # noqa: BLE001  表示のために監視を止めない
            return None
        if not row.get("verified"):
            return None
        if int(row.get("qty") or 0) > 0:
            held.append(row)
    if not held:
        return None

    entries = {round(float(r["avgEntry"]), 4) for r in held
               if r.get("avgEntry") is not None}
    if len(entries) != 1:
        # 口座ごとに建値が違うなら1行にまとめない(平均を作れば嘘になる)。
        return None
    entry = entries.pop()
    side = str(held[0].get("side") or "LONG").upper()
    direction = -1.0 if side == "SHORT" else 1.0
    qty = int(held[0].get("qty") or 0)
    lots = qty * len(held)
    pts = (price - entry) * direction
    usd = pts * 2.0 * lots

    line = (f"{side} {qty}×{len(held)} @{entry:,.2f} · "
            f"{pts:+,.2f}pt · {'−' if usd < 0 else '+'}${abs(usd):,.2f}")

    # 到達時損益は凍結プランがあるときだけ足す。無ければ含み損益だけ出す。
    try:
        import position_card
        plan = position_card.load_plan()
    except Exception:  # noqa: BLE001
        return line
    stop = _number(plan.get("stop"))
    targets = [_number(t) for t in (plan.get("targets") or [])]
    targets = [t for t in targets if t is not None]
    tail = []
    if targets:
        tp1 = (targets[0] - entry) * direction * 2.0 * len(held)
        tail.append(f"TP1 {'−' if tp1 < 0 else '+'}${abs(tp1):,.0f}")
    if stop is not None:
        sl = (stop - entry) * direction * 2.0 * lots
        tail.append(f"SL {'−' if sl < 0 else '+'}${abs(sl):,.0f}")
    return line + (f" ({' / '.join(tail)})" if tail else "")


def read_bundle_bytes(raw: bytes):
    """bundle を **バイト列から** 読む。

    R39: ここは以前 ``json.load(sys.stdin)`` だった。``sys.stdin`` は
    ロケールのコードページ(この環境では cp932)で復号されるので、
    ``monitor_pipeline`` が ``ensure_ascii=False`` で書いた日本語入りの
    bundle を渡すと、文字化けするか ``UnicodeDecodeError`` で落ちていた。
    CLAUDE.md §6.2a / §6.3 が指示する「ファイルをバイト列のまま渡す」
    経路が、受け側で成立していなかった。復号はここで UTF-8 に固定する。
    """
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    return compact(json.loads(raw.decode("utf-8-sig")))


def main():
    try:
        bundle = read_bundle_bytes(sys.stdin.buffer.read())
    except (json.JSONDecodeError, UnicodeDecodeError, OSError, ValueError) as exc:
        raise SystemExit(f"ERROR: invalid monitor bundle: {exc}")

    bundle, strategy_notes = enrich_decisive_strategy(bundle)

    previous = None
    previous_path = STATE_FILE
    for candidate in (STATE_FILE, LEGACY_STATE_FILE):
        try:
            with open(candidate, "r", encoding="utf-8") as fh:
                previous = json.load(fh)
            previous_path = candidate
            break
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            continue
    if isinstance(previous, dict) and fingerprint(previous) == fingerprint(bundle):
        if previous_path != STATE_FILE:
            os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
            with open(STATE_FILE, "w", encoding="utf-8") as fh:
                json.dump(previous, fh, ensure_ascii=False, indent=2)
        print("skipped duplicate monitor bundle")
        return

    # ライブ含み損益は publish の **前** に入れる。Mini App が読む正本と
    # Telegram 本文の両方が同じ 1 行を使うので、画面ごとに数字がずれない。
    position_line = live_position_line(bundle)
    if position_line:
        bundle["position"] = position_line

    # 先に state を publish する。Mini App が読むのはこちらであって、
    # 下の Telegram メッセージや URL ではない。
    state_ok, state_notes = publish_state(bundle)
    # R52: Worker の建玉 stream を **reconcile の前**に同期する。MANAGEMENT claim は
    # Worker が保持する建玉(qty / side / positionGeneration)と intent を突き合わせる
    # ので、これが無いと TP1 後の建値移動が MANAGEMENT_CLAIM_POSITION_MISMATCH で
    # 永久に出せない(2026-09-05 02:46 実測)。建玉 publish は telegram_bot 常駐の中に
    # しか無く、09-01 以降一度も更新されていなかった。
    position_sync_notes = _sync_position_to_worker(state_ok)
    # Entry and position management are opt-in and live-gated inside the
    # separate engine.  Disabled/default runs only emit a short note; they do
    # not query the broker or call order.py.
    auto_notes = autotrade_engine.reconcile(bundle, state_ok=state_ok)
    # 戦績の自動記録。**reconcile の後**に置く —— 同じサイクルで engine が
    # 決済(FLATTEN)を送ったなら、その結果まで含めて観測できる。
    # 読むだけの経路なので、失敗しても監視サイクルは止めない。
    auto_notes = list(auto_notes) + position_sync_notes + _record_closed_trades(bundle)

    cfg = load_env()
    if state_ok:
        # 正本が更新できたので、Mini App は API から読む。過去 payload は URL に載せない。
        url = nqx_state.web_app_url(cfg["TELEGRAM_CHAT_ID"])
        button = "✦ OPEN NIGHTWATCH"
    else:
        # Cloudflare 未設定 or publish 失敗。従来の凍結プレビューに退避する。
        # Mini App 側はこのモードを「未検証プレビュー」として扱い、発注ボタンを出さない。
        #
        # R39: ここで bundle 全体を URL へ載せると、足とレベルが増えたときに
        # Telegram の reply_markup 上限を超え、"reply markup is too long" で
        # **通知そのものが SystemExit で落ちていた**。正本 publish が失敗した
        # サイクルほど通知が要るのに、その時だけ通知が消えるという最悪の挙動。
        # 足を削って収め、それでも収まらなければボタンを諦めて本文だけ送る。
        url, button = _frozen_preview_url(bundle)

    # ★ ReplyKeyboard で貼ること。インラインボタンから開いた Mini App では
    #   Telegram の WebApp.sendData() が無言で失敗し、発注が Bot に届かない。
    keyboard = {
        "keyboard": [[{"text": button, "web_app": {"url": url}}]],
        "resize_keyboard": True,
        "is_persistent": True,
        "one_time_keyboard": False,
    } if url else None
    # 武装シナリオがあるときは、本文の前にチャート画像を添える。画像は補助
    # なので、描けない・送れないときは本文だけを送る(通知自体は止めない)。
    # 発注可否の判断は本文と Mini App が担い、画像は見るためのもの。
    # R27: **本文を先に送る。** 画像は補助で、発注可否の判断は本文と Mini App が
    # 担う。sendPhoto のタイムアウトは 60 秒で本文の 45 秒より長く、しかも画像を
    # 送るのは A/A+ かつ ARMED のとき **だけ** —— 最も急ぐサイクルだけが余計に
    # 待たされていた。画像が遅くても・落ちても、本文は既に届いている。
    # R39: 診断は送信の **前** に出す。以前は send() の後に print していたので、
    # Telegram が落ちた瞬間 SystemExit で state_notes / strategy_notes /
    # auto_notes が全部消え、「なぜ正本が更新できなかったか」が残らなかった。
    print(f"  server state: {'ok' if state_ok else 'UNAVAILABLE (frozen preview fallback)'}")
    print(f"  autotrade   : {autotrade_arm.summary()}")
    for note in (*state_notes, *strategy_notes, *auto_notes):
        print(f"    - {note}")

    result = send(cfg, build_monitor_message(bundle, state_ok, auto_notes), reply_markup=keyboard)
    if not result or not result.get("ok"):
        raise SystemExit(f"ERROR: Telegram send failed: {result}")
    _send_scenario_card(cfg, bundle)
    # 保有中のカードは「契約が変わった瞬間」だけ。詳細は _send_position_card。
    _send_position_card(cfg, bundle)
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as fh:
        json.dump(bundle, fh, ensure_ascii=False, indent=2)
    with open(LEGACY_STATE_FILE, "w", encoding="utf-8") as fh:
        json.dump(bundle, fh, ensure_ascii=False, indent=2)
    print("published monitor bundle to Telegram")


if __name__ == "__main__":
    main()

#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""MSNR 確認連鎖ゲート — bundle JSON を読み、反応型シナリオの武装資格を機械判定する。

MSNR(Alchemist / Malaysian SNR)ドクトリンは
``スイープ → 奪還 → displacement → 実体クローズ MSS → 保持されたリテスト``
を要求し、「ブレイクだけなら SWEEP CANDIDATE 止まり」と明記している。
2026-08-17 の2敗はどちらも「確定足ブレイク→リテスト」だけで武装したもので、
この連鎖を通せばどちらも武装できなかった。本スクリプトはその連鎖を
**形容詞を使わない数値判定**として3分監視ループに接続する。

入力は毎サイクル既に書いている bundle JSON そのもの(stdin・BOM 許容)。
追加の MCP 呼び出しは 0 回。判定は純粋関数で、ファイル書き込み・
ネットワークは一切行わない。

    python msnr_gate.py                              # フル JSON
    python msnr_gate.py --summary                    # summary 1行(報告に貼る)
    python msnr_gate.py --level 30166.92 --side buy  # 該当レベルの promotion のみ
    python msnr_gate.py --card [--sl-cap 25]         # bundle.evaluation の骨格(R6)
    python msnr_gate.py --card --side sell --entry 29327 --stop 29355.75
                                                    # + stopBudget / vpHeadroom(R9)

終了コード: 0(promotion の可否によらず) / 2(入力の構造異常のみ)。

**このゲートはボラ予算ゲート・CT_TREND 整合・イベント封鎖を代替しない。**
既存ゲート群への AND 条件の追加である(CLAUDE.md §3 / スキル
operational-gates.md §9)。
"""
import json
import hashlib
import math
import os
import re
from zoneinfo import ZoneInfo
import statistics
import sys
from datetime import datetime, timedelta, timezone

import liquidity_pools
import strategy_models

TICK = 0.25
BAR_SEC = 180                 # 3分足
WINDOW_BARS = 60              # freshness / 連鎖 / 回転 / ノイズ床の走査ウィンドウ
# R45: VWAP はチャートの **Session VWAP と同じアンカー**(取引日開始 = ET 18:00)
# で再計算する。R5 は「供給された全量」をアンカーにしていたが、240本(12時間)の
# 窓は前の取引日に食い込む。実測 2026-08-26 04:15 ET のサイクルで
# 全量アンカー 29,217.22 に対し bundle の Session VWAP は 29,204.06 ——
# 13.16pt のずれは vwap_drift_max=3.0 を毎サイクル超え、vwapPath は
# `VWAP_DRIFT` で恒常的に死んでいた(= VWAP 反応経路が一度も出ていない)。
# 走査は従来どおり末尾 WINDOW_BARS 本のまま。受理上限は正本サーバーと同じ 240。
MAX_INPUT_BARS = 240
ET_ZONE = ZoneInfo("America/New_York")
SESSION_OPEN_HOUR_ET = 18     # CME 取引日の開始(= Session VWAP のアンカー)
NOISE_BARS = 12               # ノイズ床の母数(monitor_publish.VOL_GATE_BARS と同期必須)
DEDUPE_PT = 2.0               # レベル統合のしきい値(§3.3)
VWAP_RE = re.compile(r"vwap", re.I)
VAH_RE = re.compile(r"\bvah\b", re.I)
VAL_RE = re.compile(r"\bval\b", re.I)

# R4 §3.1 レベル階層。上から順に判定し、最初に当たった tier を採用する
# (= 複数マッチ時は最小 tier)。短いトークンは語境界を付けて誤爆を避ける
# (`pp` が "Supply" に当たるのを防ぐ。採用した仮定)。
TIER_PATTERNS = [
    (1, re.compile(r"monthly\s+(high|low)|weekly\s+(high|low)|all\s*time", re.I)),
    (2, re.compile(r"previous\s+day|prev\s+day", re.I)),
    # `\bmid\b` は仕様表の「Weekly/Monthly Mid」を拾うため(実データのラベルは
    # "Weekly Mid" で "Range" が付かないことがある)。Prev Day Mid は tier2 が先に当たる。
    (3, re.compile(r"\basia\b|\blondon\b|new\s+york|\bvah\b|\bval\b|\bpoc\b|\bmid\b", re.I)),
    (4, re.compile(r"\bpp\b|\br[12]\b|\bs[12]\b|\bcpr\b", re.I)),
    (5, re.compile(r"\d{2}:\d{2}|market\s+open", re.I)),
]
TIER_DEFAULT = 5

# R4 §2: A+ の初動しきい値(通常の displacement 1.0×NF より強いことを要求)
APLUS_DISP_MULT = 1.3

# 状態の進行度(「最も進んだ連鎖」を選ぶための序列)
CHAIN_RANK = {"EXPIRED": 0, "SWEEP_CANDIDATE": 1, "FLIP_BREAK": 1,
              "SWEEP_CONFIRMED": 2, "MSS_CONFIRMED": 3, "FLIP_ACCEPTED": 3,
              "RETEST_HELD": 4, "FLIP_HELD": 4}

# R4 §1/§5 の ADVISORY 経路。promotion には一切影響しない
PATH_RANK = {"EXPIRED": 0, "VWAP_BREAK": 1, "VWAP_ACCEPTED": 2, "VWAP_HELD": 3,
             "VP_RE_ENTRY": 1, "VP_ACCEPTED": 2}
VWAP_PATH_JP = {"VWAP_BREAK": "突破·受容待ち", "VWAP_ACCEPTED": "受容済·到達待ち",
                "VWAP_HELD": "到達保持", "EXPIRED": "期限切れ"}
VP_PATH_JP = {"VP_RE_ENTRY": "VA復帰·受容待ち", "VP_ACCEPTED": "VA復帰受容",
              "EXPIRED": "期限切れ"}
VWAP_BLOCKERS_BY_STATE = {
    "VWAP_BREAK": ["VWAP_ACCEPTANCE_NOT_CONFIRMED"],
    "VWAP_ACCEPTED": ["VWAP_RETEST_NOT_HELD"],
    "VWAP_HELD": [],
    "EXPIRED": ["CHAIN_EXPIRED"],
}

CHAIN_JP = {"SWEEP_CANDIDATE": "スイープのみ", "SWEEP_CONFIRMED": "スイープ確認·MSS待ち",
            "MSS_CONFIRMED": "MSS確認·リテスト待ち", "RETEST_HELD": "リテスト保持",
            "FLIP_BREAK": "フリップ受容待ち", "FLIP_ACCEPTED": "フリップ受容·戻り待ち",
            "FLIP_HELD": "フリップ保持", "EXPIRED": "期限切れ"}

# 終端 PASS。ここに入ってからも鮮度 TTL(COMPLETE_TTL)で失効する
COMPLETE_STATES = ("RETEST_HELD", "FLIP_HELD")
# R26: 掃引済み(SL の根拠が確定)かつリテスト未到達(建値が現値の先＝指値が
# 置ける)状態。実サイクル 372 本で MSS_CONFIRMED は 216 回現れ、そのうち
# **99% (213/216)** が建値を現値の先に持ち、リスク中央値 27.5pt・94% が
# 60pt 上限内だった。一方エンジンが候補化していた RETEST_HELD は 70 回しか
# 無く、待つことで機会の約 2/3 を捨てていた。
#
# 「未掃引の流動性プールに先回りして指値」は実測で棄却した: 掃引 100 件の
# うち 89% は 36 分以内に水準の内側へ戻るが、行き過ぎ幅が中央値 21.5pt・
# p90 82pt で、プール直上の SL が耐えるのは 58%。88% 耐えるには 68.5pt 要り
# 60pt 上限を超える。**掃引が起きてから**なら SL は推測ではなく実測になる。
LIMIT_ENTRY_STATES = ("MSS_CONFIRMED",)
LIMIT_MAX_GAP_R = 1.5      # 現値から建値まで。これ以上遠い指値は待ちが長すぎる


# 状態 → blocker(R2 §5 の列挙以外は出力しない)
BLOCKERS_BY_STATE = {
    "SWEEP_CANDIDATE": ["SWEEP_ONLY", "NO_DISPLACEMENT"],
    "SWEEP_CONFIRMED": ["MSS_NOT_CONFIRMED"],
    "MSS_CONFIRMED": ["RETEST_NOT_HELD"],
    "FLIP_BREAK": ["ACCEPTANCE_NOT_CONFIRMED"],
    "FLIP_ACCEPTED": ["FLIP_RETEST_NOT_HELD"],
    "EXPIRED": ["CHAIN_EXPIRED"],
    "RETEST_HELD": [],
    "FLIP_HELD": [],
}


def _env_float(name, default):
    try:
        value = float(os.environ.get(name, default))
        return value if math.isfinite(value) else float(default)
    except (TypeError, ValueError):
        return float(default)


def _env_int(name, default):
    try:
        return int(float(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return int(default)


def params():
    """§3 のパラメータ表。既定値は指示書が正で、環境変数で上書きできる。"""
    return {
        "touch_pt": _env_float("NQX_MSNR_TOUCH_PT", 2.0),
        "disp_mult": _env_float("NQX_MSNR_DISP_MULT", 1.0),
        "mss_lookback": _env_int("NQX_MSNR_MSS_LOOKBACK", 5),
        "mss_window": _env_int("NQX_MSNR_MSS_WINDOW", 6),
        "retest_window": _env_int("NQX_MSNR_RETEST_WINDOW", 10),
        "chain_ttl": _env_int("NQX_MSNR_CHAIN_TTL", 15),
        "consumed": _env_int("NQX_MSNR_CONSUMED", 3),
        "rot_window": _env_int("NQX_MSNR_ROT_WINDOW", 10),
        "flip_accept": _env_int("NQX_MSNR_FLIP_ACCEPT_BARS", 3),
        "complete_ttl": _env_int("NQX_MSNR_COMPLETE_TTL", 10),
        # R4
        "aplus_disp": _env_float("NQX_MSNR_APLUS_DISP", APLUS_DISP_MULT),
        "vwap_drift_max": _env_float("NQX_MSNR_VWAP_DRIFT_MAX", 3.0),
        "vwap_prior": _env_int("NQX_MSNR_VWAP_PRIOR_BARS", 2),
        "vp_outside": _env_int("NQX_MSNR_VP_OUTSIDE_BARS", 2),
        # R88: TURTLE の SL をスイープ全体の極値に置くか(既定 OFF)
        "sweep_stop": sweep_stop_mode(),
        # R90: BREAKER(FLIP)の SL の錨をブレイク足でなく上昇/下降の起点へ(契約 stopLogic.flipOrigin)
        "flip_origin": flip_origin_rule(),
    }


# --- 入力の正規化 ---------------------------------------------------------

def _num(obj, *keys):
    """短縮キー(h/l)と長いキー(high/low)の両対応。bool は数値として拒否する。"""
    for key in keys:
        value = obj.get(key) if isinstance(obj, dict) else None
        if isinstance(value, bool) or value is None:
            continue
        try:
            value = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(value):
            return value
    return None


def _epoch(text):
    """タイムゾーン付き ISO を epoch 秒に。読めなければ None(捏造しない)。"""
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        return datetime.fromisoformat(text.strip().replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def closed_bars(bundle):
    """確定足のみを時系列で返す(§3: t + 180 ≤ epoch(priceAt))。

    priceAt が無ければ at。どちらも読めない場合は形成中バーの判定ができないので
    **最終バーを落とす**(fail-closed。形成中足で方向を断定しない §9)。
    """
    snapshot = bundle.get("snapshot")
    if snapshot is None:
        snapshot = {}
    if not isinstance(snapshot, dict):
        raise ValueError("snapshot must be an object")
    raw = snapshot.get("bars3m")
    if raw is None:
        raw = snapshot.get("bars")
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        raise ValueError("snapshot.bars3m / snapshot.bars must be a list")

    bars = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        bar = {
            "t": _num(item, "t", "time"),
            "o": _num(item, "o", "open"),
            "h": _num(item, "h", "high"),
            "l": _num(item, "l", "low"),
            "c": _num(item, "c", "close"),
            # R4 §1: 窓内 VWAP の再計算に使う。欠落は None のまま残し、
            # rolling_vwap 側で「1本でも欠けたら無効」と判定する
            "v": _num(item, "v", "volume"),
        }
        if None in (bar["t"], bar["o"], bar["h"], bar["l"], bar["c"]):
            continue
        if bar["h"] < bar["l"]:
            continue
        bars.append(bar)
    bars.sort(key=lambda b: b["t"])

    # R5: ここでは**確定足を全量**返す(VWAP のアンカー用)。走査ウィンドウへの
    # 絞り込みは evaluate() が行う。
    ref = _epoch(bundle.get("priceAt")) or _epoch(bundle.get("at"))
    if ref is None:
        return bars[:-1][-MAX_INPUT_BARS:] if bars else []
    return [b for b in bars if b["t"] + BAR_SEC <= ref][-MAX_INPUT_BARS:]


def noise_floor(bars):
    """直近 12 本の確定足レンジの中央値(monitor_publish.vol_gate と同一定義)。

    monitor_publish は編集禁止のため意図的に複製している。
    **VOL_GATE_BARS を変更したらここも同期すること。**
    """
    if len(bars) < NOISE_BARS:
        return None
    ranges = [b["h"] - b["l"] for b in bars[-NOISE_BARS:]]
    return statistics.median(ranges)


def level_tier(label):
    """R4 §3.1: ラベルから階層を返す。該当なしは 5(最下位)。"""
    for tier, pattern in TIER_PATTERNS:
        if pattern.search(label):
            return tier
    return TIER_DEFAULT


def dedupe(levels):
    """価格差 2.0pt 以内のレベルを1つに統合する。

    R4 §3.2: 代表は「先に現れた方」ではなく **tier が上(数字が小さい)方**。
    同 tier なら先着。Weekly High が 09:30 オープンに吸収される事故を防ぐ。
    吸収されたラベルは(旧代表を含めて)mergedWith に積む。
    """
    if levels is None:
        levels = []
    if not isinstance(levels, list):
        raise ValueError("snapshot.levels must be a list")
    kept = []
    for item in levels:
        if not isinstance(item, dict):
            continue
        price = _num(item, "price")
        if price is None:
            continue
        label = str(item.get("label") or item.get("name") or "?")
        tier = level_tier(label)
        for existing in kept:
            if abs(existing["price"] - price) <= DEDUPE_PT:
                if tier < existing["tier"]:
                    existing["mergedWith"].append(existing["label"])
                    existing.update({"label": label, "price": price, "tier": tier})
                else:
                    existing["mergedWith"].append(label)
                break
        else:
            kept.append({"label": label, "price": price, "tier": tier,
                         "mergedWith": []})
    for level in kept:
        level["confluence"] = 1 + len(level["mergedWith"])
    return kept


# --- §3.4 freshness -------------------------------------------------------

def orientation(bars, price, tol):
    """レベルの役割を決める。ゾーンの外で最初に引けた足の側が価格の居場所。

    仕様は「下側レベル=支持の例、抵抗は上下鏡像」とのみ書くため、支持/抵抗の
    確定手順をここで機械化した(採用した仮定。ゾーン内クローズは方向情報を
    持たないので読み飛ばし、1本も外に無ければ支持として扱う)。
    """
    for bar in bars:
        if bar["c"] > price + tol:
            return "SUPPORT"
        if bar["c"] < price - tol:
            return "RESISTANCE"
    return "SUPPORT"


def freshness_scan(bars, price, tol, consumed_touches=3):
    """6状態の状態機械。返り値は (freshness, bodyTouchEpisodes)。

    R2 修正2: 消費は**エピソード数**で数える。ゾーン外クローズの状態から
    ゾーン内クローズに入った時に 1 回。連続したゾーン内クローズは同一
    エピソード(レベル上に3本滞留しただけで失格にしない)。
    """
    role = orientation(bars, price, tol)
    state, episodes, broke_at, prev_in_zone = "FRESH", 0, None, False
    for idx, bar in enumerate(bars):
        if state == "CONSUMED":
            break                                   # 終端
        close, high, low = bar["c"], bar["h"], bar["l"]
        if role == "SUPPORT":
            broke = close < price - tol
            reclaim = close > price + tol
            wick = low < price and close > price
        else:
            broke = close > price + tol
            reclaim = close < price - tol
            wick = high > price and close < price
        in_zone = price - tol <= close <= price + tol

        if broke:
            if state != "BROKEN":
                broke_at = idx                       # 破壊は「遷移」の1回。以降の下値足では更新しない
            state = "BROKEN"
        elif state == "BROKEN" and reclaim:
            # 奪還が破壊から2本を超えたら「破壊のまま」(§3.5 2本型の窓と同一)
            if broke_at is not None and idx - broke_at <= 2:
                state = "RECLAIMED"
        elif in_zone:
            if not prev_in_zone:
                episodes += 1                        # 新しい実体タッチのエピソード
            if episodes >= consumed_touches:
                state = "CONSUMED"
            elif state != "BROKEN":
                # 破壊済みのレベルは正規の奪還(ゾーン外クローズ)まで BROKEN のまま。
                # ゾーン内クローズで anchorOk を蘇らせない(fail-closed の採用仮定)。
                state = "BODY_TESTED"
        elif wick and state == "FRESH":
            state = "WICK_TESTED"
        prev_in_zone = in_zone
    return state, episodes


def anchor_ok(freshness, role):
    """アンカー資格を side 別に返す(R2 修正1.2)。

    破壊されたレベルは**元の方向**のアンカーを失うが、**反対方向**の
    アンカーになる(RBS/SBR)。CONSUMED だけが両方向を殺す。
    """
    origin = "BUY" if role == "SUPPORT" else "SELL"
    flip = "SELL" if origin == "BUY" else "BUY"
    if freshness == "CONSUMED":
        return {"BUY": False, "SELL": False}
    return {origin: freshness not in ("BROKEN", "FLIPPED"), flip: True}


# --- §3.5 確認連鎖 --------------------------------------------------------

def _origin_side(bars, idx, price, tol):
    """idx の直前で、ゾーンの外に引けた最も新しい足の側。ゾーン内クローズは方向を持たない。"""
    for m in range(idx - 1, -1, -1):
        if bars[m]["c"] > price + tol:
            return "ABOVE"
        if bars[m]["c"] < price - tol:
            return "BELOW"
    return None


def _is_reclaim_bar(bars, i, price, tol, side):
    """奪還足か。1本型スイープと2本型(破壊→2本以内の奪還)の両方を見る。

    BUY は「**支持**スイープ」なので、刈られる直前までレベルが支持だったこと
    (直前のゾーン外クローズが上側)を要求する。これが無いと、下から上への
    単なる実体ブレイクが1本型スイープに化けて 8/17 型が素通りする
    (指示書 §7 テスト4 が要求する挙動)。SELL は鏡像。
    """
    bar = bars[i]
    if side == "BUY":
        if bar["c"] <= price:
            return False
        # 1本型: 支持だったレベルの下をヒゲで刈り、実体は上で引ける
        if bar["l"] <= price - TICK and _origin_side(bars, i, price, tol) == "ABOVE":
            return True
        # 2本型: 支持の破壊 → 2本以内の奪還
        for m in (i - 1, i - 2):
            if m >= 0 and bars[m]["c"] < price - tol \
                    and _origin_side(bars, m, price, tol) == "ABOVE":
                return True
        return False
    if bar["c"] >= price:
        return False
    if bar["h"] >= price + TICK and _origin_side(bars, i, price, tol) == "BELOW":
        return True
    for m in (i - 1, i - 2):
        if m >= 0 and bars[m]["c"] > price + tol \
                and _origin_side(bars, m, price, tol) == "BELOW":
            return True
    return False


#: R88: TURTLE_SOUP_REVERSAL の構造 SL を「スイープ全体(2 本型の破壊足〜奪還足)の極値」に
#: 置くか。既定 OFF(= 奪還足 1 本の極値。R88 以前と同じ)。2 本型では破壊足のヒゲの方が
#: 深いことがあり、奪還足だけで置いた SL は実際に刈られた価格の内側に来うる
#: (docs/R88_TURTLE_SWEEP_STOP.md)。LIVE にすると SL・targets/targetR・等級・
#: decisionId(setup_identity は SL を含む)が変わるので、切り替えはユーザー判断。
SWEEP_STOP_LIVE_VALUES = {"LIVE", "ON", "1", "TRUE", "YES"}


def sweep_stop_mode():
    """``NQX_TURTLE_SWEEP_STOP``。LIVE の綴り以外はすべて OFF(fail-closed = 従来の SL)。"""
    raw = str(os.environ.get("NQX_TURTLE_SWEEP_STOP", "OFF")).strip().upper()
    return "LIVE" if raw in SWEEP_STOP_LIVE_VALUES else "OFF"


def _sweep_origin_index(bars, i, price, tol, side):
    """奪還足 i のスイープが始まった足。2 本型なら破壊足、1 本型なら i 自身。

    ``_is_reclaim_bar`` の 2 本型と同じ条件を読む。破壊足は高々 1 本
    (i-2 が外で引けていれば i-1 の直前の外側クローズは反対側になり、条件を満たさない)。
    """
    for m in (i - 1, i - 2):
        if m < 0:
            continue
        if side == "BUY":
            broke = bars[m]["c"] < price - tol and _origin_side(bars, m, price, tol) == "ABOVE"
        else:
            broke = bars[m]["c"] > price + tol and _origin_side(bars, m, price, tol) == "BELOW"
        if broke:
            return m
    return i


# --- R90: 初期 SL の穴(docs/R90_STOP_LOGIC_HOLES.md) --------------------------
#: 方針の正本は execution_contract.json の stopLogic(stop_logic.load_policy)。
#: 穴 1(VWAP 逃がし)は _candidate_for_chain、穴 3(FLIP の錨)は scan_flip / _chain_prices が読む。
#: テストと再生はこの関数を差し替えて方針を注入する(R89 の model_gate_rules と同じ作法)。

def stop_logic_policy():
    """R90 の方針。契約が読めない・壊れていれば全部 OFF(= R90 以前と同じ判定)。"""
    try:
        import stop_logic
        return stop_logic.load_policy()
    except Exception:  # noqa: BLE001 - 方針が読めなければ従来どおり
        return None


def flip_origin_rule():
    """``params()`` に載せる穴 3 の規則 ``{"mode", "maxBarsBack"}``。OFF なら None。"""
    policy = stop_logic_policy()
    rule = policy.get("flipOrigin") if isinstance(policy, dict) else None
    if not isinstance(rule, dict) or str(rule.get("mode") or "OFF") != "LIVE":
        return None
    return {"mode": "LIVE", "maxBarsBack": int(rule.get("maxBarsBack") or 5)}


def _apply_vwap_clearance(model, side, entry, stop, bundle, nf):
    """穴 1。``(stop, audit)``。OFF は ``(stop, None)``、SHADOW は記録だけ、LIVE は置換。

    この層の例外で評価を落とさない —— 失敗は「従来の SL のまま」に倒し、理由を audit に残す。
    """
    policy = stop_logic_policy()
    rule = policy.get("vwapClearance") if isinstance(policy, dict) else None
    if not isinstance(rule, dict) or str(rule.get("mode") or "OFF") == "OFF":
        return stop, None
    try:
        import stop_logic
        vwap = _num(bundle, "vwap") if isinstance(bundle, dict) else None
        snapshot_v = bundle.get("snapshot") if isinstance(bundle, dict) and isinstance(bundle.get("snapshot"), dict) else {}
        if vwap is None and snapshot_v:
            vwap = _num(snapshot_v, "vwap")
        # R105: セッション VWAP が欠損付き(窓がアンカーに届かない / ループ停止中の抜け)なら
        # 使わない。推測で埋めた VWAP に SL を寄せない。
        if snapshot_v.get("vwapComplete") is False or (isinstance(bundle, dict) and bundle.get("vwapComplete") is False):
            return stop, {"mode": rule.get("mode"), "model": model, "eligible": False, "applied": False,
                          "reason": "VWAP_PARTIAL", "vwap": vwap, "original": stop,
                          "gapBars": snapshot_v.get("vwapGapBars")}
        audit = stop_logic.vwap_clearance(side, entry, stop, vwap, nf, rule, model=model)
    except Exception as exc:  # noqa: BLE001
        return stop, {"mode": rule.get("mode"), "model": model, "eligible": False, "applied": False,
                      "reason": f"ERROR:{type(exc).__name__}"}
    audit["applied"] = bool(audit.get("eligible") and str(rule.get("mode")) == "LIVE")
    if audit["applied"]:
        return audit["stop"], audit
    return stop, audit


def _compact_vwap_audit(audit):
    """カード(4096 バイト上限)に載せる分だけ。"""
    if not isinstance(audit, dict):
        return {}
    return {key: audit.get(key) for key in ("mode", "applied", "reason", "vwap", "original", "stop", "shiftPt", "gapN")
            if audit.get(key) is not None}


def _apply_pool_clearance(model, side, entry, stop, bars, levels, bundle, nf):
    """R103-1。``(stop, audit)``。OFF は ``(stop, None)``、SHADOW は記録だけ、LIVE は置換。

    vwapClearance(穴 1)と同じ形。この層の例外で評価を落とさない —— 失敗は「従来の SL の
    まま」に倒し、理由を audit に残す。
    """
    policy = stop_logic_policy()
    rule = policy.get("poolClearance") if isinstance(policy, dict) else None
    if not isinstance(rule, dict) or str(rule.get("mode") or "OFF") == "OFF":
        return stop, None
    try:
        price = _num(bundle, "price") if isinstance(bundle, dict) else None
        if price is None and bars:
            price = bars[-1]["c"]
        tol = max(params()["touch_pt"], 0.10 * nf) if nf else None
        rows = (liquidity_pools.pools(bars, levels, price, tol, nf)
                if (bars or levels) else None)
        audit = liquidity_pools.stop_pool_audit(side, entry, stop, rows, nf, rule, model=model)
    except Exception as exc:  # noqa: BLE001
        return stop, {"mode": rule.get("mode"), "model": model, "applied": False,
                      "reason": f"ERROR:{type(exc).__name__}"}
    audit["applied"] = bool(audit.get("required") is not None
                            and str(rule.get("mode")) == "LIVE")
    if audit["applied"]:
        return audit["required"], audit
    return stop, audit


def _compact_stdv_audit(audit):
    """R121: カード(4096 バイト上限)に載せる分だけ。アンカー全体は載せない。

    載せるのは「どのアンカーで」「投影がどこで」「目標に効いたか」「参加判断」だけ。
    水準は消費者(目標選択)が実際に見る負の係数に絞る。
    """
    if not isinstance(audit, dict):
        return {}
    anchor = audit.get("anchor") if isinstance(audit.get("anchor"), dict) else {}
    read = audit.get("read") if isinstance(audit.get("read"), dict) else {}
    compact = {key: audit.get(key) for key in
               ("mode", "targetsMode", "reason", "targetsApplied", "runnerRejected")
               if audit.get(key) is not None}
    if anchor:
        compact["anchorId"] = anchor.get("anchorId")
        compact["side"] = anchor.get("side")
        compact["p0"] = anchor.get("p0")
        compact["p1"] = anchor.get("p1")
        compact["projectionValid"] = anchor.get("projectionValid")
        compact["consumedRatios"] = anchor.get("consumedRatios") or []
        compact["levels"] = {str(row.get("ratio")): row.get("price")
                             for row in (anchor.get("levels") or [])
                             if isinstance(row, dict) and (row.get("ratio") or 0) < 0}
    if audit.get("targetsApplied"):
        # 差し替えた周期は**必ず**差し替え前を残す。空リストでも落とさない ——
        # 「基準に梯子が無かった(= STDV が梯子を作った)」と「記録していない」を
        # 区別できなくなるため(実運用のログで `None` に見えていた)。
        compact["baseTargets"] = list(audit.get("baseTargets") or [])
    if audit.get("targetsOffered"):
        compact["offered"] = [row.get("price") for row in audit["targetsOffered"]
                              if isinstance(row, dict)]
    if read:
        compact["participation"] = read.get("participation")
        compact["thesis"] = read.get("thesis")
        compact["headroom"] = read.get("headroom") or {}
    return compact


def _compact_pool_audit(audit):
    """カード(4096 バイト上限)に載せる分だけ。プールは価格と種別だけ残す。"""
    if not isinstance(audit, dict):
        return {}
    def _rows(key):
        return [{"price": row.get("price"), "kind": row.get("kind")}
                for row in (audit.get(key) or []) if isinstance(row, dict)]
    compact = {key: audit.get(key) for key in ("mode", "applied", "reason", "original",
                                               "required", "shiftPt", "riskN")
               if audit.get(key) is not None}
    for key in ("between", "beyondWithinN", "inside025N"):
        rows = _rows(key)
        if rows:
            compact[key] = rows
    nearest = audit.get("nearestBeyond")
    if isinstance(nearest, dict):
        compact["nearestBeyond"] = {"price": nearest.get("price"), "kind": nearest.get("kind"),
                                    "gapPt": nearest.get("gapPt")}
    return compact


def _sweep_origin_t(chain):
    """掃引ゲートが「これより後の掃引だけを見る」起点の時刻。VP80 は再突入エピソードの起点。"""
    if not isinstance(chain, dict):
        return None
    origin = chain.get("originBarT")
    if origin is None:
        origin = chain.get("sweepBarT") if chain.get("type") == "SWEEP" else chain.get("breakBarT")
    try:
        return int(origin) if origin is not None else None
    except (TypeError, ValueError):
        return None


def _apply_sweep_gate(model, side, entry, original_stop, current_stop, bars, levels, chain, bundle, nf):
    """R103-3。``(stop, audit)``。OFF は ``(current_stop, None)``、SHADOW は記録だけ、LIVE は
    PASSED なら SL を掃引極値の向こうへ置換(PENDING / STALE は呼び出し側が WATCH にする)。

    プールは **元の SL**(VWAP 逃がし後・プール逃がし前)に対して見る。プール逃がしが SL を
    動かした後で見ると「もうプールは内側」になり、掃引を待つ判定が消えてしまうため。
    この層の例外で評価を落とさない —— 失敗は「今の SL のまま」に倒し、理由を audit に残す。
    """
    policy = stop_logic_policy()
    rule = policy.get("sweepGate") if isinstance(policy, dict) else None
    if not isinstance(rule, dict) or str(rule.get("mode") or "OFF") == "OFF":
        return current_stop, None
    try:
        import stop_logic
        price = _num(bundle, "price") if isinstance(bundle, dict) else None
        if price is None and bars:
            price = bars[-1]["c"]
        tol = max(params()["touch_pt"], 0.10 * nf) if nf else None
        rows = (liquidity_pools.pools(bars, levels, price, tol, nf)
                if (bars or levels) else None)
        pool_rule = {"mode": rule.get("mode"), "withinN": rule.get("poolWithinN", 1.0),
                     "clearN": 0.25, "kinds": rule.get("kinds") or list(stop_logic.POOL_KINDS),
                     "models": list(stop_logic.ALL_MODELS)}
        base = liquidity_pools.stop_pool_audit(side, entry, original_stop, rows, nf, pool_rule, model=None)
        audit = liquidity_pools.sweep_gate_audit(side, entry, original_stop, base, bars,
                                                 _sweep_origin_t(chain), nf, tol, rule, model=model)
    except Exception as exc:  # noqa: BLE001
        return current_stop, {"mode": rule.get("mode"), "model": model, "state": "ERROR",
                              "applied": False, "reason": f"ERROR:{type(exc).__name__}"}
    audit["applied"] = bool(str(rule.get("mode")) == "LIVE" and audit.get("state") == "PASSED"
                            and audit.get("required") is not None)
    if audit["applied"]:
        return audit["required"], audit
    return current_stop, audit


def _compact_sweep_audit(audit):
    """カードに載せる分だけ。"""
    if not isinstance(audit, dict):
        return {}
    compact = {key: audit.get(key) for key in ("mode", "state", "applied", "reason", "sweepState",
                                               "sweepBarT", "reclaimBarT", "extreme", "ageBars",
                                               "original", "required", "shiftPt", "riskN")
               if audit.get(key) is not None}
    pool = audit.get("pool")
    if isinstance(pool, dict):
        compact["pool"] = {"price": pool.get("price"), "kind": pool.get("kind"), "gapPt": pool.get("gapPt")}
    return compact


def _mss_reference(bars, sweep_idx, lookback, side):
    """MSS の参照窓 = sweepBar の直前 5 本。3 本未満なら判定不能で None。"""
    window = bars[max(0, sweep_idx - lookback):sweep_idx]
    if len(window) < 3:
        return None
    return max(b["h"] for b in window) if side == "BUY" else min(b["l"] for b in window)


def scan_chain(bars, sweep_idx, price, tol, nf, side, prm):
    """スイープ足1本から連鎖を追い、到達した状態を返す。"""
    last = len(bars) - 1
    sweep = bars[sweep_idx]
    chain = {
        "type": "SWEEP", "side": side, "state": "SWEEP_CANDIDATE",
        "sweepBarT": int(sweep["t"]), "mssBarT": None, "retestBarT": None,
        "barsSinceRetest": None,
        "dispBody": abs(sweep["c"] - sweep["o"]),   # R4 §2: A+ 判定の初動
        "barsLeft": prm["chain_ttl"] - (last - sweep_idx),
        "blockers": [],
    }
    if prm.get("sweep_stop") == "LIVE":
        # R88: SL の錨をスイープ全体へ。OFF ではキーを足さない(出力を R88 以前と同一に保つ)。
        origin = _sweep_origin_index(bars, sweep_idx, price, tol, side)
        span = bars[origin:sweep_idx + 1]
        extreme = min(b["l"] for b in span) if side == "BUY" else max(b["h"] for b in span)
        chain["sweepOriginBarT"] = int(bars[origin]["t"])
        chain["sweepExtreme"] = extreme
        chain["sweepExtremeBeyondReclaim"] = (extreme < sweep["l"]) if side == "BUY" \
            else (extreme > sweep["h"])

    body = abs(sweep["c"] - sweep["o"])
    directional = sweep["c"] > sweep["o"] if side == "BUY" else sweep["c"] < sweep["o"]
    if not (body >= prm["disp_mult"] * nf and directional):
        return _finalize_chain(chain, prm, last, sweep_idx)   # displacement 不足で頭打ち
    chain["state"] = "SWEEP_CONFIRMED"

    ref = _mss_reference(bars, sweep_idx, prm["mss_lookback"], side)
    if ref is None:
        return _finalize_chain(chain, prm, last, sweep_idx)   # 参照窓不足 = MSS 判定不能
    mss_idx = None
    for k in range(sweep_idx + 1, min(last, sweep_idx + prm["mss_window"]) + 1):
        broke_structure = bars[k]["c"] > ref if side == "BUY" else bars[k]["c"] < ref
        if broke_structure:
            mss_idx = k
            break
    if mss_idx is None:
        return _finalize_chain(chain, prm, last, sweep_idx)
    chain["state"] = "MSS_CONFIRMED"
    chain["mssBarT"] = int(bars[mss_idx]["t"])

    for j in range(mss_idx + 1, min(last, mss_idx + prm["retest_window"]) + 1):
        bar = bars[j]
        if side == "BUY":
            if bar["c"] < price - tol:               # 連鎖リセット(構造の再破壊)
                return None
            # 到達条件(R3): 戻りはレベル価格そのものに触れること。ゾーンの端では足りない
            if bar["l"] <= price and bar["c"] > price:
                chain["state"] = "RETEST_HELD"
                chain["retestBarT"] = int(bar["t"])
                chain["barsSinceRetest"] = last - j
                break
        else:
            if bar["c"] > price + tol:
                return None
            if bar["h"] >= price and bar["c"] < price:
                chain["state"] = "RETEST_HELD"
                chain["retestBarT"] = int(bar["t"])
                chain["barsSinceRetest"] = last - j
                break
    return _finalize_chain(chain, prm, last, sweep_idx)


def _finalize_chain(chain, prm, last, start_idx):
    """TTL と blocker を確定させる。

    未完成の連鎖は CHAIN_TTL(スイープ/break から15本)で失効。
    完成した連鎖も **COMPLETE_TTL(保持足から10本)** を超えたら失効する
    (R2 修正3: 90分前に保持した連鎖で武装させない)。
    """
    chain["barsLeft"] = prm["chain_ttl"] - (last - start_idx)
    since = chain.get("barsSinceRetest") if chain["type"] == "SWEEP" \
        else chain.get("barsSinceHold")
    if chain["state"] in COMPLETE_STATES:
        if since is not None and since > prm["complete_ttl"]:
            chain["state"] = "EXPIRED"
    elif chain["barsLeft"] <= 0:
        chain["state"] = "EXPIRED"
    chain["blockers"] = list(BLOCKERS_BY_STATE[chain["state"]])
    return chain


def _is_flip_break(bars, b, price, tol, side):
    """フリップの起点足か。SELL は支持の実体下抜け、BUY は抵抗の実体上抜け。"""
    if side == "SELL":
        return bars[b]["c"] < price - tol \
            and _origin_side(bars, b, price, tol) == "ABOVE"
    return bars[b]["c"] > price + tol \
        and _origin_side(bars, b, price, tol) == "BELOW"


def scan_flip(bars, break_idx, price, tol, side, prm):
    """FLIP 連鎖(RBS/SBR)を追う。break → 受容 → 戻り → 保持クローズ。

    8/12 の勝ち型(下抜け後の戻り売り)がこれ。8/17 の2敗も形は同じだが
    **保持クローズの前に武装した**ので FLIP_ACCEPTED 止まりになる。
    """
    last = len(bars) - 1
    chain = {
        "type": "FLIP", "side": side, "state": "FLIP_BREAK",
        "breakBarT": int(bars[break_idx]["t"]), "holdBarT": None,
        "barsSinceHold": None,
        # R4 §2: FLIP の初動は「受容を作った2本のうち大きい方の実体」。
        # 受容前は break 足だけで暫定値を持たせる
        "dispBody": abs(bars[break_idx]["c"] - bars[break_idx]["o"]),
        "barsLeft": prm["chain_ttl"] - (last - break_idx),
        "blockers": [],
    }
    flip_rule = prm.get("flip_origin") if isinstance(prm.get("flip_origin"), dict) else None
    if flip_rule and flip_rule.get("mode") == "LIVE":
        # R90: SL の錨をブレイク足から上昇/下降の起点へ。OFF ではキーを足さない
        # (出力を R90 以前と同一に保つ。R88 の sweepExtreme と同じ形)。
        import stop_logic
        origin = stop_logic.flip_origin_index(bars, break_idx, side, flip_rule.get("maxBarsBack", 5))
        span = bars[origin:break_idx + 1]
        extreme = min(b["l"] for b in span) if side == "BUY" else max(b["h"] for b in span)
        chain["flipOriginBarT"] = int(bars[origin]["t"])
        chain["flipExtreme"] = extreme
        chain["flipExtremeBeyondBreak"] = (extreme < bars[break_idx]["l"]) if side == "BUY" \
            else (extreme > bars[break_idx]["h"])
    # 到達条件(R3): 戻りの極値がレベル価格そのものに届くこと(tol に依存しない)。
    # ゾーンは破壊・受容・タッチ計数の分類にだけ使う。
    if side == "SELL":                                # 支持のフリップ = 戻り売り
        def reset(bar):    return bar["c"] > price + tol      # 奪還 = フリップ失敗
        def outside(bar):  return bar["c"] < price - tol      # 受容(ゾーン外クローズ)
        def held(bar):     return bar["h"] >= price and bar["c"] < price
    else:                                             # 抵抗のフリップ = 押し目買い
        def reset(bar):    return bar["c"] < price - tol
        def outside(bar):  return bar["c"] > price + tol
        def held(bar):     return bar["l"] <= price and bar["c"] > price

    accept_idx = hold_idx = None
    for k in range(break_idx + 1, last + 1):
        bar = bars[k]
        if reset(bar):                                # break 後はいつでも消滅する
            return None
        if accept_idx is None:
            if k - break_idx <= prm["flip_accept"] and outside(bar):
                accept_idx = k
                chain["state"] = "FLIP_ACCEPTED"
                chain["dispBody"] = max(chain["dispBody"],
                                        abs(bar["c"] - bar["o"]))
            continue                                  # 受容前に保持は読まない
        if hold_idx is None and k - accept_idx <= prm["retest_window"] and held(bar):
            hold_idx = k
            chain["state"] = "FLIP_HELD"
            chain["holdBarT"] = int(bar["t"])
            chain["barsSinceHold"] = last - k
    return _finalize_chain(chain, prm, last, break_idx)


def best_chain(bars, price, tol, nf, side, prm):
    """同一レベル・同一 side では最も進んだスイープ連鎖を1本だけ返す(同順位は直近)。"""
    best = None
    for i in range(len(bars)):
        if not _is_reclaim_bar(bars, i, price, tol, side):
            continue
        chain = scan_chain(bars, i, price, tol, nf, side, prm)
        if chain is None:
            continue
        if best is None or (CHAIN_RANK[chain["state"]], chain["sweepBarT"]) >= \
                (CHAIN_RANK[best["state"]], best["sweepBarT"]):
            best = chain
    return best


def best_flip(bars, price, tol, side, prm):
    """同一レベル・同一 side で最も進んだ FLIP 連鎖を1本だけ返す。"""
    best = None
    for b in range(len(bars)):
        if not _is_flip_break(bars, b, price, tol, side):
            continue
        chain = scan_flip(bars, b, price, tol, side, prm)
        if chain is None:
            continue
        if best is None or (CHAIN_RANK[chain["state"]], chain["breakBarT"]) >= \
                (CHAIN_RANK[best["state"]], best["breakBarT"]):
            best = chain
    return best


# --- R4 §1: VWAP 反応経路(ADVISORY・promotion に影響しない)----------------

def session_anchor_epoch(now_epoch):
    """取引日(ET 18:00 起点)の開始 epoch = Session VWAP のアンカー。

    ``tv_snapshot.session_bounds_et`` の ``day_open`` と**同一定義**。
    どちらか一方だけを変えると bundle.vwap と系列のアンカーがずれ、
    drift ゲートが恒常的に閉じる(R45 で実際に起きた)。
    """
    if now_epoch is None:
        return None
    now_et = datetime.fromtimestamp(now_epoch, ET_ZONE)
    day_open = now_et.replace(hour=SESSION_OPEN_HOUR_ET,
                              minute=0, second=0, microsecond=0)
    if now_et.hour < SESSION_OPEN_HOUR_ET:
        day_open -= timedelta(days=1)
    return int(day_open.timestamp())


def session_vwap_seed(bars, anchor, snapshot):
    """窓の手前(anchor → bars[0] の直前)の累積 ``(pv, vv)`` を復元する。

    R45: publish 経路は bars3m を FEED_BARS_MAX(既定60本)に切ってから
    評価に回すため、判定側はセッション開始まで遡る足を持っていない。
    取得側(tv_snapshot)が載せたセッション累積の合計から、**自分が持っている
    足ぶんの合計を引く**ことで手前ぶんを復元する。差が負になる/合計が
    最終足まで届いていない場合は復元せず ``None``(捏造しない)。
    """
    if not isinstance(snapshot, dict) or not bars or anchor is None:
        return None
    pv_total = _num(snapshot, "vwapSessionPv")
    vv_total = _num(snapshot, "vwapSessionVv")
    through = _num(snapshot, "vwapThroughT")
    if None in (pv_total, vv_total, through):
        return None
    if int(through) != int(bars[-1]["t"]):
        return None                       # 合計の終点と手持ちの終点が違う
    own_pv = own_vv = 0.0
    for bar in bars:
        if bar["t"] < anchor:
            continue
        vol = bar.get("v")
        if vol is None or vol < 0:
            return None
        own_pv += ((bar["h"] + bar["l"] + bar["c"]) / 3.0) * vol
        own_vv += vol
    pv_pre = pv_total - own_pv
    vv_pre = vv_total - own_vv
    if vv_pre < -1e-6 or pv_pre < -1e-6:
        return None                       # 合計より手持ちが多い = 前提が壊れている
    return max(0.0, pv_pre), max(0.0, vv_pre)


def rolling_vwap(bars, anchor=None, seed=None):
    """VWAP 系列を bars3m から再計算する(§1.1)。

    ``vwap_i = Σ(hlc3_k × v_k) ÷ Σ(v_k)``(anchor ≤ k ≤ i)。

    R45: ``anchor``(epoch)を渡すと、そのセッション開始以降の確定足だけを
    累積する = チャートの Session VWAP と同じアンカー。アンカーより前の足は
    ``None`` を置き、**系列の長さと並びは bars と一致させる**(呼び出し側が
    末尾スライスでインデックスを合わせているため)。``None`` は必ず先頭側の
    連続した prefix にしかならない。
    ``anchor=None`` は旧挙動(供給された最古のバーを起点)。

    ``seed=(pv, vv)`` は ``session_vwap_seed`` が復元した「窓の手前ぶん」。
    bars が 60本へ切られていても系列がセッション開始起点になる。

    **v が1本でも欠けたら None**(捏造せず、経路ごと無効にする)。
    """
    cum_pv, cum_v = (seed if seed else (0.0, 0.0))
    out = []
    for bar in bars:
        vol = bar.get("v")
        if vol is None or vol < 0:
            return None
        if anchor is not None and bar["t"] < anchor:
            out.append(None)
            continue
        hlc3 = (bar["h"] + bar["l"] + bar["c"]) / 3.0
        cum_pv += hlc3 * vol
        cum_v += vol
        if cum_v <= 0:
            return None
        out.append(cum_pv / cum_v)
    return out


def _finalize_path(path, prm, last, start_idx, blockers_map):
    """ADVISORY 経路の TTL と blocker を確定させる(連鎖と同じ寿命規則)。"""
    path["barsLeft"] = prm["chain_ttl"] - (last - start_idx)
    since = path.get("barsSinceHold")
    if path["state"] in ("VWAP_HELD",):
        if since is not None and since > prm["complete_ttl"]:
            path["state"] = "EXPIRED"
    elif path["barsLeft"] <= 0:
        path["state"] = "EXPIRED"
    path["blockers"] = list(blockers_map[path["state"]])
    return path


def _is_vwap_break(bars, vwaps, b, tolv, side, prior_need):
    """VWAP の突破足か。**反対側に居た事実**(確定足2本以上)を前提に要求する。

    前提を課さないと、VWAP 沿いを漂う値動きが毎足 break に化ける。
    「直近に」の範囲は break 足より前の窓全体とした(採用した仮定)。
    R45: アンカー(セッション開始)より前の足は ``vwaps[i] is None``。
    比較対象が無いので break にも前提の母数にも数えない。
    """
    if vwaps[b] is None:
        return False
    if side == "BUY":
        if not bars[b]["c"] > vwaps[b] + tolv:
            return False
        prior = sum(1 for m in range(b)
                    if vwaps[m] is not None and bars[m]["c"] < vwaps[m] - tolv)
    else:
        if not bars[b]["c"] < vwaps[b] - tolv:
            return False
        prior = sum(1 for m in range(b)
                    if vwaps[m] is not None and bars[m]["c"] > vwaps[m] + tolv)
    return prior >= prior_need


def scan_vwap_path(bars, vwaps, break_idx, tolv, side, prm):
    """突破 → 受容 → 到達保持。各判定は**そのバー時点の vwap_i** と比較する。

    ``vwaps`` の ``None`` は先頭側の prefix にしかならず(rolling_vwap の規約)、
    ``break_idx`` は必ず非 None の位置なので ``k > break_idx`` も非 None。
    """
    last = len(bars) - 1
    path = {"side": side, "state": "VWAP_BREAK",
            "breakBarT": int(bars[break_idx]["t"]), "holdBarT": None,
            "barsSinceHold": None,
            "barsLeft": prm["chain_ttl"] - (last - break_idx), "blockers": []}
    if side == "BUY":                                  # RECLAIM
        def outside(i): return bars[i]["c"] > vwaps[i] + tolv
        def reset(i):   return bars[i]["c"] < vwaps[i] - tolv
        def held(i):    return bars[i]["l"] <= vwaps[i] and bars[i]["c"] > vwaps[i]
    else:                                              # REJECT
        def outside(i): return bars[i]["c"] < vwaps[i] - tolv
        def reset(i):   return bars[i]["c"] > vwaps[i] + tolv
        def held(i):    return bars[i]["h"] >= vwaps[i] and bars[i]["c"] < vwaps[i]

    accept_idx = hold_idx = None
    for k in range(break_idx + 1, last + 1):
        if accept_idx is None:
            if k - break_idx <= prm["flip_accept"] and outside(k):
                accept_idx = k
                path["state"] = "VWAP_ACCEPTED"
            continue
        if reset(k):                                   # リセットは受容後に見る(§1.2 の記載どおり)
            return None
        if hold_idx is None and k - accept_idx <= prm["retest_window"] and held(k):
            hold_idx = k
            path["state"] = "VWAP_HELD"
            path["holdBarT"] = int(bars[k]["t"])
            path["barsSinceHold"] = last - k
    return _finalize_path(path, prm, last, break_idx, VWAP_BLOCKERS_BY_STATE)


def vwap_path(bars, bundle_vwap, tolv, prm, vwaps=None):
    """§1.3 の出力。最も進んだ経路を1本返す。drift 超過なら経路ごと無効。

    ``vwaps`` は bars と**同じ長さ・同じ並び**の VWAP 系列。R45 以降は
    evaluate() が**セッション開始(ET 18:00)をアンカー**に計算して末尾を
    切り出したものを渡す。省略時は bars だけから再計算する(旧挙動)。
    """
    empty = {"side": None, "state": None, "drift": None, "blockers": []}
    if vwaps is None:
        vwaps = rolling_vwap(bars)
    if vwaps is None or len(vwaps) != len(bars):
        return dict(empty, blockers=["INSUFFICIENT_BARS"])
    if not vwaps or vwaps[-1] is None:
        # セッション開始より後の確定足がまだ無い。近似で埋めず経路を出さない。
        return dict(empty, blockers=["INSUFFICIENT_BARS"])
    drift = None if bundle_vwap is None else abs(vwaps[-1] - bundle_vwap)
    # drift が測れない(bundle.vwap 欠落)場合も fail-closed で無効にする。
    # 近似がチャートの真値と一致する保証が無いまま経路を出さない(採用した仮定)。
    if drift is None or drift > prm["vwap_drift_max"]:
        return dict(empty, drift=None if drift is None else round(drift, 2),
                    blockers=["VWAP_DRIFT"])

    best = None
    for side in ("BUY", "SELL"):
        for b in range(len(bars)):
            if not _is_vwap_break(bars, vwaps, b, tolv, side, prm["vwap_prior"]):
                continue
            path = scan_vwap_path(bars, vwaps, b, tolv, side, prm)
            if path is None:
                continue
            key = (PATH_RANK[path["state"]], path["breakBarT"])
            if best is None or key >= (PATH_RANK[best["state"]], best["breakBarT"]):
                best = path
    if best is None:
        return dict(empty, drift=round(drift, 2))
    best["drift"] = round(drift, 2)
    return best


# --- R4 §5: VP 受容経路(80%ルールの3分適応・ADVISORY)---------------------

def _find_level_price(levels, pattern):
    """統合後のレベルから、label か mergedWith が pattern に当たる価格を返す。"""
    for level in levels:
        if pattern.search(level["label"]):
            return level["price"]
        for merged in level["mergedWith"]:
            if pattern.search(merged):
                return level["price"]
    return None


def _scan_vp(bars, edge, target, tol, side, prm):
    """バリューエリアの外で受容に失敗し、中へ戻って受容されたか。

    side は復帰後に向かう方向。上抜け失敗 → SELL(目標 VAL)、下抜け失敗 → BUY。
    """
    last = len(bars) - 1
    if side == "SELL":                                 # VAH の外(上)に居た
        def outside(i): return bars[i]["c"] > edge + tol
        def inside(i):  return bars[i]["c"] < edge - tol
    else:                                              # VAL の外(下)に居た
        def outside(i): return bars[i]["c"] < edge - tol
        def inside(i):  return bars[i]["c"] > edge + tol

    best = None
    for r in range(len(bars)):
        if not inside(r):
            continue
        if sum(1 for m in range(r) if outside(m)) < prm["vp_outside"]:
            continue
        path = {"side": side, "state": "VP_RE_ENTRY", "target": target,
                "reEntryBarT": int(bars[r]["t"]), "acceptBarT": None,
                "barsSinceHold": None,
                "barsLeft": prm["chain_ttl"] - (last - r), "blockers": []}
        for k in range(r + 1, last + 1):
            if outside(k):                             # 外へ戻ったら消滅
                path = None
                break
            if k - r <= prm["flip_accept"] and inside(k):
                path["state"] = "VP_ACCEPTED"
                path["acceptBarT"] = int(bars[k]["t"])
                break
        if path is None:
            continue
        path = _finalize_path(path, prm, last, r,
                              {"VP_RE_ENTRY": [], "VP_ACCEPTED": [], "EXPIRED": []})
        key = (PATH_RANK[path["state"]], path["reEntryBarT"])
        if best is None or key >= (PATH_RANK[best["state"]], best["reEntryBarT"]):
            best = path
    return best


def vp_path(bars, levels, tol, prm):
    """§5 の出力。VAH と VAL が揃っているときだけ評価する。"""
    empty = {"side": None, "state": None, "target": None, "blockers": []}
    vah = _find_level_price(levels, VAH_RE)
    val = _find_level_price(levels, VAL_RE)
    if vah is None or val is None or vah <= val:
        return empty
    best = None
    for edge, target, side in ((vah, val, "SELL"), (val, vah, "BUY")):
        path = _scan_vp(bars, edge, target, tol, side, prm)
        if path is None:
            continue
        if best is None or PATH_RANK[path["state"]] > PATH_RANK[best["state"]]:
            best = path
    return best or empty


# --- §3.6 rotation --------------------------------------------------------

def rotation(bars, nf, prm):
    """回転相場の機械判定。実体シグナルの半数以上が次の足で全戻しなら ROTATION。"""
    window = bars[-(prm["rot_window"] + 1):]
    signals = negations = 0
    for i in range(len(window) - 1):
        cur, nxt = window[i], window[i + 1]
        if abs(cur["c"] - cur["o"]) < 0.5 * nf:
            continue
        signals += 1
        if cur["c"] > cur["o"] and nxt["c"] < cur["o"]:
            negations += 1
        elif cur["c"] < cur["o"] and nxt["c"] > cur["o"]:
            negations += 1
    ratio = (negations / signals) if signals else 0.0
    verdict = "ROTATION" if (signals >= 4 and ratio >= 0.5) else "OK"
    return {"signals": signals, "negations": negations,
            "ratio": round(ratio, 2), "verdict": verdict}


# --- 合成 -----------------------------------------------------------------

def grade_of(level, chains, side, nf, prm):
    """R4 §2: ボラ比率 0.4〜0.6 帯で提案できる A+ を機械定義する。

    3条件すべて — 強い初動(≥1.3×NF)/ 合流または上位階層 / 消耗していない鮮度。
    allowed=false のときは None(等級は「通ったもの」にしか付かない)。
    """
    complete = [c for c in chains
                if c["side"] == side and c["state"] in COMPLETE_STATES]
    if not complete:
        return None
    strong = any((c.get("dispBody") or 0.0) >= prm["aplus_disp"] * nf
                 for c in complete)
    tiered = level["tier"] <= 2 or len(level["mergedWith"]) >= 1
    # R5 裁定①: FLIPPED を許可集合に追加する。完成した FLIP 連鎖は必ず
    # 破壊(BROKEN)を経るので鮮度が FLIPPED になり、これを外すと 8/12 の
    # 勝ち型が 0.4〜0.6 帯から恒久排除される。過剰テストは CONSUMED が防ぐ。
    fresh_ok = level["freshness"] in ("FRESH", "WICK_TESTED", "FLIPPED")
    return "A+" if (strong and tiered and fresh_ok) else "A"


def compose(level, chains, side, rot, nf=None, prm=None):
    """R2 §1.3 の合成式。SWEEP か FLIP のどちらかが**完成かつ鮮度内**なら通す。"""
    if level["dynamic"]:
        return {"allowed": False, "grade": None, "blockers": ["DYNAMIC_LEVEL"]}
    mine = [c for c in chains if c["side"] == side]
    complete = any(c["state"] in COMPLETE_STATES for c in mine)

    blockers = []
    if not mine:
        blockers.append("NO_CHAIN")
    elif not complete:
        for chain in mine:                            # SWEEP → FLIP の順で重複なく積む
            for code in chain["blockers"]:
                if code not in blockers:
                    blockers.append(code)
    anchor = level["anchorOk"][side]
    if not anchor:
        blockers.append("ANCHOR_CONSUMED" if level["freshness"] == "CONSUMED"
                        else "ANCHOR_BROKEN")
    if rot["verdict"] != "OK":
        blockers.append("ROTATION_REGIME")
    allowed = complete and anchor and rot["verdict"] == "OK"
    grade = grade_of(level, chains, side, nf, prm) if (allowed and nf) else None
    return {"allowed": allowed, "grade": grade,
            "blockers": [] if allowed else blockers}


# --- R8 §4: rrPotential(ADVISORY・promotion/allowed/grade には一切影響しない) ---

RR_ROADBLOCK_MULT = 3.0        # 直近すぎるレベルは走路として数えない(§4.1)
RR_HIGH_CANDIDATE = 2.8        # rb2R がこれ以上で「高RR候補」表示(表示のみ)


def rr_potential(level, levels, nf, tol):
    """R8 §4.1: 走路(roadblock)の R 換算。observe-only、判定には使わない。

    side ごとに利益方向(BUY=上/SELL=下)にある non-dynamic レベルのうち
    ``|rb − L| >= 3 × touchTol`` を満たすものを近い順に2つ拾い、SL距離の
    下限代理(noiseFloor + touchTol)で割って R 化する。無ければ null
    (走路は開放 = 反対方向の障害物が見えていないだけで、無制限という意味ではない)。
    """
    sl_proxy = nf + tol
    price = level["price"]
    candidates = [lv for lv in levels if not lv["dynamic"] and lv is not level]

    def _leg(rb):
        if rb is None or not sl_proxy:
            return None, None
        pt = abs(rb["price"] - price)
        return round(pt, 2), round(pt / sl_proxy, 2)

    out = {}
    for side in ("BUY", "SELL"):
        if side == "BUY":
            pool = sorted((lv for lv in candidates if lv["price"] > price),
                         key=lambda lv: lv["price"])
        else:
            pool = sorted((lv for lv in candidates if lv["price"] < price),
                         key=lambda lv: lv["price"], reverse=True)
        pool = [lv for lv in pool if abs(lv["price"] - price) >= RR_ROADBLOCK_MULT * tol]
        rb1 = pool[0] if len(pool) >= 1 else None
        rb2 = pool[1] if len(pool) >= 2 else None
        rb1_pt, rb1_r = _leg(rb1)
        rb2_pt, rb2_r = _leg(rb2)
        out[side] = {
            "rb1Pt": rb1_pt, "rb1R": rb1_r, "rb1Label": rb1["label"] if rb1 else None,
            "rb2Pt": rb2_pt, "rb2R": rb2_r, "rb2Label": rb2["label"] if rb2 else None,
        }
    return out


# --- R10: ICT 層(ADVISORY・promotion / allowed / grade には一切影響しない) ---
# 出典: ICT Core Content Months 1-12。既存ゲートと重複しない4点だけを実装する。
#   1. Premium/Discount  … ディーリングレンジのどちら側で売買しているか(Month 1 EP4/5)
#   2. Draw on Liquidity … 次の流動性プールと LRLR/HRLR(Month 1 EP7 / Month 4 EP11)
#   3. Killzone/Profile … 指数先物の時間帯(Month 10 EP11/12・Month 12 EP4 Step3)
#   4. FVG              … 直近の未充填フェアバリューギャップ(Month 4 EP12)
# MSNR 連鎖(sweep→MSS→retest / break→acceptance→hold)は ICT の
# Turtle Soup / Breaker とほぼ同義なので **再実装しない**(R10 の対応表を参照)。

# R35: ET は ZoneInfo で解釈する(_et_minutes 参照)。旧: 固定オフセット。
ICT_ET_ZONE = ET_ZONE
ICT_JST_OFFSET_H = 9           # 表示用に残す。時刻計算には使わない。
ICT_ET_OFFSET_H = -4           # 同上。DST の手動切替はもう不要。
OTE_LO, OTE_HI = 0.62, 0.79    # Month 1 EP4/5「爆発的な動きは OTE で起きる」

# (開始ET分, 終了ET分, キー, 表示, ICT がセットアップを探す時間帯か)
ICT_WINDOWS = [
    (2 * 60, 5 * 60, "LONDON_KZ", "ロンドンKZ", True),
    (7 * 60, 9 * 60 + 30, "NY_AM_KZ", "NY AM KZ", True),
    (9 * 60 + 30, 10 * 60 + 30, "TRUE_DAY_HL", "真の高安形成帯", True),
    (10 * 60 + 30, 12 * 60, "AM_TREND_LATE", "AMトレンド後半", True),
    (12 * 60, 13 * 60, "LUNCH", "NYランチ", False),
    (13 * 60, 15 * 60, "PM_TREND", "PMトレンド", True),
    (15 * 60, 16 * 60, "LAST_HOUR", "ラストアワー", True),
]

ICT_WINDOW_NOTE = {
    "TRUE_DAY_HL": "真の日の高値/安値がこの1時間で作られやすい",
    "LUNCH": "浅い戻りの持ち合い。ICT は新規を探さない時間帯",
    "PM_TREND": "14:00ET前後から動き出す。AM の継続か反転か",
    "OFF_HOURS": "キルゾーン外。ICT の型は成立しにくい",
}

# R48: Silver Bullet の3窓(EDGE_LEDGER 却下リスト「合算検証禁止」に従い
# 別ラベルで記録する)。表示・スコアカード分離用の注釈で、判定には使わない。
SILVER_BULLET_WINDOWS = [
    (3 * 60, 4 * 60, "LDN_OPEN_SB"),
    (10 * 60, 11 * 60, "AM_SB"),
    (14 * 60, 15 * 60, "PM_SB"),
]

# R48: HOD/LOD の累積形成率(TradingStats E3・NQ 3,034セッション・記述統計)。
# (この境界ET分以降のbucket名, その境界時点での HOD累積, LOD累積)。
# 「PM に居る=HOD の60%はもう出来ている」という日中成熟度の注釈にだけ使う。
# 方向シグナルではない(EDGE_LEDGER M9)。
DAY_MATURITY_CHECKPOINTS = [
    (9 * 60 + 30, "AM", 0.362, 0.416),
    (12 * 60, "PM", 0.604, 0.681),
    (15 * 60, "LATE", 0.770, 0.849),
    (16 * 60, "CLOSED", None, None),
]


def _et_minutes(at_iso):
    """bundle の ``at`` から ET の 0時起点の分を返す。失敗したら None。

    R35: **タイムゾーンを実際に解釈する。**

    以前は ISO 文字列の文字位置 [11:13]/[14:16] を切り出し、`+09:00` 決め打ちで
    `hh - 4 - 9` していた。オフセット表記を一切見ないので、同じ瞬間でも表記が
    違えば別の ET になる。実測(2026-08-24 の同一瞬間):

        2026-08-24T07:46:00+00:00 → 18:46 OFF_HOURS   ← tv_snapshot が出す形
        2026-08-24T16:46:00+09:00 → 03:46 LONDON_KZ   ← 旧バンドルの形(正解)
        2026-08-24T03:46:00-04:00 → 14:46 PM_TREND

    R28 で取得経路を tv_snapshot(UTC 出力)に差し替えた瞬間、キルゾーンと
    SMT 窓が 9 時間ずれるところだった。`quarterly_theory` は最初から
    ZoneInfo で正しく読んでいたので、**同じバンドルを両者が別の時刻として
    扱う**食い違いも既に存在していた。

    夏時間の手動切替(ICT_ET_OFFSET_H)も同時に不要になる。tz 付きの時刻なら
    ZoneInfo が DST を正しく扱う。tz が無い時刻は**推測せず None**にする ——
    どのゾーンのつもりかは書いた人しか知らない。
    """
    if not at_iso:
        return None
    text = str(at_iso).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except (ValueError, TypeError):
        return None
    if moment.tzinfo is None:
        return None
    et = moment.astimezone(ICT_ET_ZONE)
    return et.hour * 60 + et.minute


def ict_session(at_iso):
    """ICT のキルゾーン / 指数先物プロファイル上の現在位置(Month 10 EP11/12)。

    ``tradeable`` は「ICT がその時間帯にセットアップを探すか」であって
    **武装可否ではない**。ランチ帯でも既存ゲートが全部通れば武装は成立する。
    """
    etm = _et_minutes(at_iso)
    if etm is None:
        return {"etMinute": None, "et": None, "window": None, "label": None,
                "tradeable": None, "silverBullet": None, "note": None}
    silver = next((key for start, end, key in SILVER_BULLET_WINDOWS
                   if start <= etm < end), None)
    for start, end, key, label, ok in ICT_WINDOWS:
        if start <= etm < end:
            return {"etMinute": etm, "et": "%02d:%02d" % (etm // 60, etm % 60),
                    "window": key, "label": label, "tradeable": ok,
                    "silverBullet": silver,
                    "note": ICT_WINDOW_NOTE.get(key)}
    return {"etMinute": etm, "et": "%02d:%02d" % (etm // 60, etm % 60),
            "window": "OFF_HOURS", "label": "時間外", "tradeable": False,
            "silverBullet": silver,
            "note": ICT_WINDOW_NOTE["OFF_HOURS"]}


def _iso_epoch(value):
    """ISO-8601をUTC epochへ。未指定・曖昧時刻は推測しない。"""
    if not value:
        return None
    try:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            return None
        return int(parsed.astimezone(timezone.utc).timestamp())
    except (TypeError, ValueError):
        return None


def _range_from_prices(hi, lo, price, metadata=None):
    """明示アンカー／観測レンジ共通のPD/OTE値を返す。"""
    if price is None:
        return None
    try:
        hi, lo, price = float(hi), float(lo), float(price)
    except (TypeError, ValueError):
        return None
    span = hi - lo
    if span <= 0:
        return None
    eq = lo + span / 2.0
    if price > eq:
        pos = "PREMIUM"
    elif price < eq:
        pos = "DISCOUNT"
    else:
        pos = "EQ"
    out = {
        "high": round(hi, 2), "low": round(lo, 2), "eq": round(eq, 2),
        "spanPt": round(span, 2), "pct": round((price - lo) / span * 100, 1),
        "position": pos,
        # 売りは安値からの戻り(premium 側)、買いは高値からの押し(discount 側)
        "oteSell": [round(lo + span * OTE_LO, 2), round(lo + span * OTE_HI, 2)],
        "oteBuy": [round(hi - span * OTE_HI, 2), round(hi - span * OTE_LO, 2)],
        "favors": {"BUY": pos == "DISCOUNT", "SELL": pos == "PREMIUM"},
    }
    if metadata:
        out.update(metadata)
    return out


# R27: レベル集合から導出するディーリングレンジ。
#
# 実サイクル 403 本で明示 rangeAnchor は **0 本**(403/403 が RANGE_ANCHOR_REQUIRED)。
# そのせいで premium/discount も OTE も一度も働かず、392 候補すべてが
# NO_CONFIRMATION で止まっていた。一方でレベル集合には ICT が dealing range に
# 使う当のアンカーが入っている(London Low 347 / New York High 303 / PDH 175 …)。
# 外部(TradingView)由来・上位時間軸・明示的に命名されており、
# resolve_range_anchor が禁じる「ローリング 3M 高安の後付け」とは別物。
#
# 導出で ICT_LOCATION が付くようになると、実測で 0 → 86 本が A/A+ に届く
# (うち A+ 53 本。SL 中央値 21.4pt、TP1 の R 中央値 1.90)。
DERIVED_RANGE_PAIRS = (
    ("Previous Day High", "Previous Day Low", "PRIOR_DAY_DERIVED"),
    ("New York High", "New York Low", "SESSION_NY_DERIVED"),
    ("London High", "London Low", "SESSION_LONDON_DERIVED"),
    ("Asia High", "Asia Low", "SESSION_ASIA_DERIVED"),
)
DERIVED_RANGE_MIN_SPAN_PT = 40.0   # 開いた直後のセッションは範囲として使わない


def derive_range_anchor(snapshot, price):
    """レベル集合の高安ペアからディーリングレンジを導出する。

    条件をすべて満たしたときだけ返す:
      * 高安の両方が存在し high > low
      * lo <= price <= hi(現値がレンジ内。外なら別のレンジが働いている)
      * span >= DERIVED_RANGE_MIN_SPAN_PT(実測 min 16.2pt の退化レンジを弾く)

    出力には source="levels" を刻み、手入力アンカーと**絶対に混ぜない**。
    """
    if not isinstance(snapshot, dict) or price is None:
        return None
    try:
        price = float(price)
    except (TypeError, ValueError):
        return None
    table = {}
    aliases = {}
    for item in snapshot.get("levels") or []:
        if not isinstance(item, dict):
            continue
        # R36: **進行中のセッション高安をレンジの起点にしない。**
        #
        # 進行中の高安は毎サイクル広がるローリング極値で、R12 が OTE に
        # 使うことを禁じた「ローリング 3M 高安」と実質同じ性質を持つ。
        # 名前が付いているというだけで通すのは筋が通らない。
        #
        # `settled` を明示している水準(tv_snapshot が出す computed(session))は
        # 終わったものだけ通す。キーが無い水準 —— インジケータ由来の PDH/PDL
        # など —— は従来どおり通す。それらは元から確定値である。
        if item.get("settled") is False:
            continue
        label = str(item.get("label") or "").strip()
        try:
            level_price = float(item.get("price"))
        except (TypeError, ValueError):
            continue
        table[label] = level_price
        # R37: 複合ラベルを分解して別名でも引けるようにする。
        #
        # 実測 403 本で `Previous Day Low` の完全一致は **18 回**しかない一方、
        # `"Weekly Low | Previous Day Low"` が 155 回、
        # `"New York Low | Previous Day Low"` が 44 回あった。対は高安の**両方**が
        # 完全一致して初めて成立するので、最も強いはずの前日レンジが Low 側の
        # ラベル形だけを理由に **403 本中 2 回**しか成立せず、最も弱い進行中
        # セッションのレンジが勝ち続けていた。
        #
        # 完全一致(table)を常に優先し、分解した別名は補助として持つ。
        for part in re.split(r"[|/]", label):
            part = part.strip()
            if part and part != label:
                aliases[part] = level_price
    for hi_label, lo_label, anchor_type in DERIVED_RANGE_PAIRS:
        hi = table.get(hi_label, aliases.get(hi_label))
        lo = table.get(lo_label, aliases.get(lo_label))
        if hi is None or lo is None or hi <= lo:
            continue
        if not (lo <= price <= hi):
            continue
        if hi - lo < DERIVED_RANGE_MIN_SPAN_PT:
            continue
        out = _range_from_prices(hi, lo, price, {
            "anchorType": anchor_type, "freshness": "ACTIVE", "valid": True,
            "source": "levels", "rangeTf": "session",
            "derivedFrom": [hi_label, lo_label],
        })
        if out:
            return out
    return None


def resolve_range_anchor(bundle, snapshot, price):
    """R12: ICT OTE用の明示的なディーリングレンジを検証する。

    3分足のローリング高安からOTEを後付けしない。監視側は ``rangeAnchor``
    に rangeTf/rangeStart/rangeEnd/anchorType/freshness/high/low を保存する。
    15M・1H・セッション・日足など、実行足より上位又は明示セッションの
    アンカーだけを OTE 候補に使う。
    """
    raw = (snapshot.get("rangeAnchor") if isinstance(snapshot, dict) else None)
    if not isinstance(raw, dict):
        raw = bundle.get("rangeAnchor") if isinstance(bundle, dict) else None
    if not isinstance(raw, dict):
        # 明示アンカーが無ければレベル集合から導出する。ROLLING_3M_OTE_FORBIDDEN
        # をはじめ、以降の検証経路は手入力アンカー専用のまま変えていない。
        derived = derive_range_anchor(snapshot, price)
        if derived:
            return derived
        return {"valid": False, "reason": "RANGE_ANCHOR_REQUIRED"}
    tf = str(raw.get("rangeTf") or "").strip().lower()
    start, end = raw.get("rangeStart"), raw.get("rangeEnd")
    anchor_type = str(raw.get("anchorType") or "").strip().upper()
    freshness = str(raw.get("freshness") or "").strip().upper()
    required = {"rangeTf": tf, "rangeStart": start, "rangeEnd": end,
                "anchorType": anchor_type, "freshness": freshness}
    missing = [key for key, value in required.items() if not value]
    if missing:
        return {"valid": False, "reason": "RANGE_ANCHOR_FIELDS_MISSING",
                "missing": missing}
    if tf in {"3m", "3", "180s", "180"}:
        return {"valid": False, "reason": "ROLLING_3M_OTE_FORBIDDEN",
                "rangeTf": raw.get("rangeTf")}
    start_epoch, end_epoch = _iso_epoch(start), _iso_epoch(end)
    if start_epoch is None or end_epoch is None or end_epoch <= start_epoch:
        return {"valid": False, "reason": "RANGE_ANCHOR_TIME_INVALID"}
    if freshness not in {"FRESH", "ACTIVE"}:
        return {"valid": False, "reason": "RANGE_ANCHOR_NOT_FRESH",
                "freshness": freshness}
    out = _range_from_prices(raw.get("high"), raw.get("low"), price, {
        "rangeTf": raw.get("rangeTf"), "rangeStart": start, "rangeEnd": end,
        "anchorType": anchor_type, "freshness": freshness, "valid": True,
    })
    if not out:
        return {"valid": False, "reason": "RANGE_ANCHOR_PRICE_INVALID",
                "rangeTf": raw.get("rangeTf")}
    return out


def dealing_range(bars, price):
    """旧表示用のローリングレンジ。OTE発注候補には使わない。"""
    if not bars or price is None:
        return None
    return _range_from_prices(max(b["h"] for b in bars), min(b["l"] for b in bars),
                              price, {"source": "ROLLING_3M_DISPLAY", "valid": False})


def swing_liquidity(bars, price, tol, lookback=3):
    """未回収の旧高値(BSL)/旧安値(SSL)を近い順に返す(Month 4 EP11)。

    スイング点 = 前後 ``lookback`` 本を含む窓で最高/最安の確定足。
    その後の足が更新していれば「回収済み」として除外する。
    """
    n = len(bars)
    bsl, ssl = set(), set()
    for i in range(lookback, n - lookback):
        win = bars[i - lookback:i + lookback + 1]
        h, l = bars[i]["h"], bars[i]["l"]
        rest = bars[i + 1:]
        if h >= max(b["h"] for b in win) and h > price + tol:
            if not any(b["h"] > h for b in rest):
                bsl.add(round(h, 2))
        if l <= min(b["l"] for b in win) and l < price - tol:
            if not any(b["l"] < l for b in rest):
                ssl.add(round(l, 2))
    return sorted(bsl), sorted(ssl, reverse=True)


def draw_on_liquidity(bars, levels, price, tol):
    """Month 1 EP7: 次の流動性プール(DOL)と LRLR / HRLR の判定。

    LRLR(低抵抗)= 目標までに挟まる旧高安/レベルが1つ以下。
    HRLR(高抵抗)= 2つ以上。ICT は HRLR を高確率条件と見なさない。
    2026-08-19 の反省 S4「走路を見ていない」の ICT 版。
    """
    if price is None or not bars:
        return None
    bsl, ssl = swing_liquidity(bars, price, tol)
    statics = [lv["price"] for lv in (levels or []) if not lv.get("dynamic")]

    def _side(target, side):
        if target is None:
            return None
        lo, hi = (price, target) if side == "BUY" else (target, price)
        pool = bsl if side == "BUY" else ssl
        blockers = {round(p, 2) for p in statics + list(pool)
                    if lo + tol < p < hi - tol}
        n = len(blockers)
        return {"target": target, "distPt": round(abs(target - price), 2),
                "obstacles": n, "run": "LRLR" if n <= 1 else "HRLR"}

    return {"BUY": _side(bsl[0] if bsl else None, "BUY"),
            "SELL": _side(ssl[0] if ssl else None, "SELL")}


FVG_MAX_AGE_BARS = 20
FVG_MIN_DISPLACEMENT = 1.30


def fvg_scan(bars, price, tol, limit=2, timeframe="3m", noise=None):
    """未充填FVGを、実際に候補化できる証跡付きで返す。

    FVGそのものだけでなく、時間足・生成時刻・年齢・displacement・到達前に
    構造を保ったかを保存する。後者が崩れた空隙は表示できても OTE/FVG の
    エントリー根拠に使わない。
    """
    empty = {"BULL": [], "BEAR": []}
    if len(bars) < 3 or price is None:
        return empty
    out = {"BULL": [], "BEAR": []}
    for i in range(1, len(bars) - 1):
        a, impulse, c = bars[i - 1], bars[i], bars[i + 1]
        after = bars[i + 2:]
        created_at = c.get("t")
        age_bars = len(bars) - (i + 2)
        body = abs(impulse["c"] - impulse["o"])
        displacement_r = (body / noise) if noise and noise > 0 else None

        def _meta(side, lo, hi, dist, structure_intact, wick_touched):
            age_minutes = age_bars * (BAR_SEC / 60.0) if str(timeframe).lower() in {"3m", "3"} else None
            eligible = (age_bars <= FVG_MAX_AGE_BARS and structure_intact and
                        (displacement_r is None or displacement_r >= FVG_MIN_DISPLACEMENT))
            # R48: ヒゲ接触と実体破壊を分離して記録する(EDGE_LEDGER M6)。
            # WICK_TOUCHED はギャップ内へヒゲが入ったが完全充填には至っていない
            # 状態。eligible 判定は従来どおり変えない — 特徴量としてスコアカード
            # が「fresh と touched のどちらが効くか」を実測するための分離。
            return {"lo": round(lo, 2), "hi": round(hi, 2),
                    "mid": round((lo + hi) / 2, 2), "distPt": round(dist, 2),
                    "side": side, "timeframe": timeframe, "createdAt": created_at,
                    "ageBars": age_bars, "ageMinutes": age_minutes,
                    "displacementBody": round(body, 2),
                    "displacementR": round(displacement_r, 2) if displacement_r is not None else None,
                    "preArrivalStructure": "INTACT" if structure_intact else "BROKEN",
                    "arrivalState": "WICK_TOUCHED" if wick_touched else "UNTOUCHED",
                    "eligible": eligible}
        if a["h"] < c["l"] - TICK:
            lo, hi = a["h"], c["l"]
            # 現値がギャップより上にあり、到達前の足で origin を終値破壊
            # していない場合だけ「未到達の需要」と呼ぶ。
            intact = not any(b["c"] <= lo for b in after)
            if hi < price and not any(b["l"] <= lo for b in after):
                touched = any(b["l"] <= hi for b in after)
                out["BULL"].append(_meta("BULL", lo, hi, price - hi, intact, touched))
        if a["l"] > c["h"] + TICK:
            lo, hi = c["h"], a["l"]
            intact = not any(b["c"] >= hi for b in after)
            if lo > price and not any(b["h"] >= hi for b in after):
                touched = any(b["h"] >= lo for b in after)
                out["BEAR"].append(_meta("BEAR", lo, hi, lo - price, intact, touched))
    out["BULL"] = sorted(out["BULL"], key=lambda g: g["distPt"])[:limit]
    out["BEAR"] = sorted(out["BEAR"], key=lambda g: g["distPt"])[:limit]
    return out


# --- R10 §5: Index SMT(ADVISORY・ICT Month 10 EP11/12)---------------------
# NQ / ES / YM の相対的な高安を比べ、**1つだけが直近の高安を更新しない**状態
# (SMT ダイバージェンス)を検出する。ICT はこれで AM/PM トレンドの型を裏付ける。
#
# 本システムでの意義: **CVD が読めない時の代替確認**。2026-08-19 に
# `data_get_study_values` が27分キャッシュを返して CVD 裏付けが取れなくなったが、
# SMT は価格だけで計算できるので同じ事故では止まらない。
#
# データは bundle の ``snapshot.peers`` から読む(無ければ available=False)。
#   "peers": {"ES": [{"t":…,"h":…,"l":…}, …], "YM": [ … ]}
# 価格帯が違っても**方向しか見ない**のでスケール調整は不要。

SMT_SWING_LOOKBACK = 2         # fractal の左右本数
SMT_MIN_SEP_BARS = 3           # 比較する2つのスイング点の最小間隔
SMT_WINDOWS = {"AM": (5 * 60, 9 * 60 + 30), "PM": (12 * 60, 15 * 60)}  # ET 分


def _norm_peer_bars(rows):
    """peers の足を {t,h,l} に正規化する。長いキー名も受ける。"""
    out = []
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        t = r.get("t", r.get("time"))
        h = r.get("h", r.get("high"))
        l = r.get("l", r.get("low"))
        if t is None or h is None or l is None:
            continue
        try:
            out.append({"t": int(t), "h": float(h), "l": float(l)})
        except (TypeError, ValueError):
            continue
    return sorted(out, key=lambda b: b["t"])


def _fractal_points(bars, kind, lookback=SMT_SWING_LOOKBACK):
    """前後 ``lookback`` 本より高い/低い足を [(index, 価格)] で返す。"""
    key = "h" if kind == "high" else "l"
    pick = max if kind == "high" else min
    pts = []
    for i in range(lookback, len(bars) - lookback):
        win = bars[i - lookback:i + lookback + 1]
        if bars[i][key] == pick(b[key] for b in win):
            pts.append((i, bars[i][key]))
    return pts


def _last_two_swings(bars, kind):
    """直近2つのスイング点。``SMT_MIN_SEP_BARS`` 以上離れた組だけを返す。"""
    pts = _fractal_points(bars, kind)
    if len(pts) < 2:
        return None
    latest = pts[-1]
    for prev in reversed(pts[:-1]):
        if latest[0] - prev[0] >= SMT_MIN_SEP_BARS:
            return prev, latest
    return None


def _peer_extreme(peer_bars, t, kind):
    """同一タイムスタンプの peer 極値だけを使う。

    SMTは近いバーを代入すると別の流動性イベントを比較してしまうため、
    R12では t±N の近似を廃止する。
    """
    if not peer_bars:
        return None
    win = [b for b in peer_bars if b["t"] == t]
    if not win:
        return None
    return max(b["h"] for b in win) if kind == "high" else min(b["l"] for b in win)


def _direction(first, second, kind):
    """2点の関係を HH/LH(高値)・HL/LL(安値)で返す。同値は EQ。"""
    if first is None or second is None:
        return None
    if abs(second - first) < TICK:
        return "EQ"
    if kind == "high":
        return "HH" if second > first else "LH"
    return "HL" if second > first else "LL"


def _smt_leg(bars, peers, kind):
    """安値側 / 高値側それぞれの SMT 判定(1レッグ分)。"""
    pair = _last_two_swings(bars, kind)
    if not pair:
        return {"divergence": False, "reason": "INSUFFICIENT_SWINGS"}
    (i1, v1), (i2, v2) = pair
    t1, t2 = bars[i1]["t"], bars[i2]["t"]
    prim = _direction(v1, v2, kind)
    peer_dirs, diverging = {}, []
    for name, rows in peers.items():
        p1 = _peer_extreme(rows, t1, kind)
        p2 = _peer_extreme(rows, t2, kind)
        d = _direction(p1, p2, kind)
        peer_dirs[name] = d
        if d and prim and d != prim and "EQ" not in (d, prim):
            diverging.append(name)
    if not diverging or prim in (None, "EQ"):
        return {"divergence": False, "primary": prim, "peers": peer_dirs,
                "atBarT": t2, "reason": None if prim else "NO_PRIMARY_SWING"}
    # 安値の乖離は強気、高値の乖離は弱気(ICT: 更新しなかった側が強い)
    strong = "NQ" if prim in ("HL", "LH") else diverging[0]
    if kind == "high":
        strong = "NQ" if prim == "HH" else diverging[0]
    return {"divergence": True, "primary": prim, "peers": peer_dirs,
            "diverging": diverging, "strong": strong, "atBarT": t2,
            "bias": "BULLISH" if kind == "low" else "BEARISH", "reason": None}


def external_smt(observation, bars, at_iso=None):
    """チャート上の SMT インジケータが**既に計算した**乖離を取り込む(R29)。

    peers から自前で計算する index_smt() は、ES 足を同一時刻で揃える必要が
    あり、CDP がタブ 0 に固定束縛されている実環境では取得できていない
    (実サイクル 403 本で peers 0%)。一方チャート上の SMT インジケータは
    MNQ と MES の乖離を毎足計算して線とラベルを描いており、その出力は
    data_get_pine_lines / _labels でそのまま読める。

    こちらは**観測の取り込み**であって再計算ではない。判定を素通しさせない
    ため、index_smt と同じ関門(SMT 窓・鮮度)は変えずに通す。

    observation は tv_snapshot.smt_from_pine() が組む:
      {"bias": "BULLISH"|"BEARISH"|None, "peer": "MES1!", "agree": bool,
       "atBarT": int|None, "source": "pine"}
    """
    if not isinstance(observation, dict):
        return None
    bias = observation.get("bias")
    if bias not in {"BULLISH", "BEARISH"}:
        return None
    etm = _et_minutes(at_iso)
    window = None
    for key, (lo, hi) in SMT_WINDOWS.items():
        if etm is not None and lo <= etm < hi:
            window = key
    if not window:
        return {"available": False, "peers": [], "window": None, "bias": None,
                "summary": None, "reason": "OUTSIDE_SMT_WINDOW"}
    # 色と幾何が食い違っていたら方向を主張しない(CVD bias と同じ規律)。
    if not observation.get("agree", True):
        return {"available": False, "peers": [], "window": window, "bias": None,
                "summary": None, "reason": "SMT_SOURCES_DISAGREE"}
    latest = int(bars[-1]["t"]) if bars else None
    peer = str(observation.get("peer") or "PEER")
    source = str(observation.get("source") or "pine")
    source_label = "JSON alert" if source == "smt_alert_json" else "チャート観測"
    note = "安値で乖離 → 強気" if bias == "BULLISH" else "高値で乖離 → 弱気"
    return {"available": True, "peers": [peer], "window": window,
            "low": None, "high": None, "bias": bias, "note": note,
            "summary": "SMT %s: %s [%s窓·%s]" % (peer, note, window, source_label),
            "reason": None, "sessionId": observation.get("sessionId"),
            "freshness": "FRESH", "at": latest, "source": source}


def index_smt(bars, peers, at_iso=None, peer_meta=None, session_id=None):
    """ICT Index SMT。時刻・同一セッション・鮮度が揃う時だけ有効にする。"""
    norm = {}
    for name, rows in (peers or {}).items():
        rows = _norm_peer_bars(rows)
        if len(rows) >= SMT_SWING_LOOKBACK * 2 + 1:
            norm[str(name).upper()] = rows
    etm = _et_minutes(at_iso)
    window = None
    for key, (lo, hi) in SMT_WINDOWS.items():
        if etm is not None and lo <= etm < hi:
            window = key
    if not norm or len(bars) < SMT_SWING_LOOKBACK * 2 + 1:
        return {"available": False, "peers": sorted(norm), "window": window,
                "bias": None, "summary": None,
                "reason": "NO_PEERS" if not norm else "INSUFFICIENT_BARS"}
    if not window:
        return {"available": False, "peers": sorted(norm), "window": window,
                "bias": None, "summary": None, "reason": "OUTSIDE_SMT_WINDOW"}
    if not session_id or not isinstance(peer_meta, dict):
        return {"available": False, "peers": sorted(norm), "window": window,
                "bias": None, "summary": None, "reason": "SMT_SESSION_ID_REQUIRED"}
    latest = int(bars[-1]["t"])
    stale = []
    wrong_session = []
    for name, rows in norm.items():
        meta = peer_meta.get(name) or peer_meta.get(name.lower()) or {}
        if str(meta.get("sessionId") or "") != str(session_id):
            wrong_session.append(name)
        if not rows or int(rows[-1]["t"]) != latest:
            stale.append(name)
    if wrong_session or stale:
        reason = "SMT_SESSION_MISMATCH" if wrong_session else "SMT_STALE_PEER"
        return {"available": False, "peers": sorted(norm), "window": window,
                "bias": None, "summary": None, "reason": reason,
                "sessionId": session_id, "wrongSession": wrong_session,
                "stalePeers": stale, "freshness": "STALE"}

    low = _smt_leg(bars, norm, "low")
    high = _smt_leg(bars, norm, "high")
    biases = [leg.get("bias") for leg in (low, high) if leg.get("divergence")]
    bias = biases[0] if len(biases) == 1 else None
    if len(biases) == 2:
        note = "両側で乖離(相殺)"
    elif bias == "BULLISH":
        note = "安値で乖離 → 強気"
    elif bias == "BEARISH":
        note = "高値で乖離 → 弱気"
    else:
        note = "乖離なし"
    parts = []
    leg = low if bias == "BULLISH" else high if bias == "BEARISH" else None
    if leg and leg.get("divergence"):
        peers_txt = " / ".join("%s=%s" % (k, v) for k, v in sorted(leg["peers"].items()) if v)
        parts.append("NQ=%s %s" % (leg["primary"], peers_txt))
    summary = "SMT %s: %s" % (" ".join(sorted(norm)), note)
    if parts:
        summary += "(" + " ".join(parts) + ")"
    if window:
        summary += " [%s窓]" % window
    return {"available": True, "peers": sorted(norm), "window": window,
            "low": low, "high": high, "bias": bias, "note": note,
            "summary": summary, "reason": None, "sessionId": session_id,
            "freshness": "FRESH", "at": latest}


POS_JP = {"PREMIUM": "プレミアム", "DISCOUNT": "ディスカウント", "EQ": "EQ"}


def ict_context(bars, levels, price, tol, at_iso, peers=None, peer_meta=None,
                session_id=None, range_anchor=None, cvd=None, nf=None,
                smt_observation=None):
    """R12: ICT観測を実判定用の証跡付きコンテキストへまとめる。"""
    sess = ict_session(at_iso)
    # peers から自前計算するのが本筋。取れていないときだけ、チャートの SMT
    # インジケータが計算済みの観測を使う(R29)。自前計算が成立していれば
    # そちらが勝つ —— 観測の取り込みで自前の判定を上書きしない。
    smt = index_smt(bars, peers, at_iso, peer_meta, session_id)
    if not smt.get("available"):
        observed = external_smt(smt_observation, bars, at_iso)
        if observed and observed.get("available"):
            smt = observed
    rng = range_anchor if isinstance(range_anchor, dict) and range_anchor.get("valid") else None
    dol = draw_on_liquidity(bars, levels, price, tol)
    fvg = fvg_scan(bars, price, tol, timeframe="3m", noise=nf)
    notes = []
    if rng:
        notes.append("%s(%.0f%%)" % (POS_JP[rng["position"]], rng["pct"]))
    for side, jp in (("BUY", "上"), ("SELL", "下")):
        d = (dol or {}).get(side)
        if d:
            notes.append("DOL%s %s(%.0fpt·%s)"
                         % (jp, format(d["target"], ",.0f"), d["distPt"], d["run"]))
    if smt.get("available") and smt.get("bias"):
        notes.append("SMT %s" % ("強気" if smt["bias"] == "BULLISH" else "弱気"))
    if sess.get("label"):
        notes.append(sess["label"])
    return {"session": sess, "range": rng, "dol": dol, "fvg": fvg, "smt": smt,
            "cvd": cvd or {}, "rangeAnchor": range_anchor or {},
            "summary": " · ".join(notes) if notes else None}


ICT_COVERAGE_KEYS = ("rangeAnchor", "smt", "fvg", "dol", "killzone", "cvd", "po3")
REPO2_COVERAGE_MODELS = ("blocks", "ifvg", "quarterly", "sessions", "fibSd")


def ict_coverage(bundle, ict, matrix=None):
    """どの ICT 入力が実際に届いたかを decision へ残す。

    rangeAnchor / peers / po3 / Repo2 ゾーンが未取得でも候補は成立する。
    その場合 ICT 層は黙って無得点になるだけで、記録を後から見ても
    「ICT を評価した上で効かなかった」のか「そもそも入力が無かった」のか
    区別できない。ここで明示して静かな劣化を可視化する。
    """
    ict = ict if isinstance(ict, dict) else {}
    bundle = bundle if isinstance(bundle, dict) else {}
    snapshot = bundle.get("snapshot") if isinstance(bundle.get("snapshot"), dict) else {}
    fvg = ict.get("fvg") or {}
    dol = ict.get("dol") or {}
    available = {
        "rangeAnchor": bool((ict.get("range") or {}).get("valid")),
        "smt": bool((ict.get("smt") or {}).get("available")),
        "fvg": any(row.get("eligible") for side in ("BULL", "BEAR")
                   for row in (fvg.get(side) or [])),
        "dol": bool(dol.get("BUY") or dol.get("SELL")),
        "killzone": (ict.get("session") or {}).get("window") is not None,
        "cvd": bool((ict.get("cvd") or {}).get("available")),
        "po3": bool(bundle.get("po3") or snapshot.get("po3")),
    }
    models = (matrix or {}).get("models") if isinstance(matrix, dict) else None
    for name in REPO2_COVERAGE_MODELS:
        row = (models or {}).get(name) if isinstance(models, dict) else None
        available[name] = bool(isinstance(row, dict)
                               and str(row.get("status") or "").upper() != "MISSING")
    missing = sorted(key for key, ok in available.items() if not ok)
    # R37: レンジが「取得したもの」か「levels から導出したもの」かを記録に残す。
    # この関数の docstring は「入力が無かったのか、評価した上で効かなかったのかを
    # 区別する」ためのものだと明言しているのに、`rangeAnchor` は `range.valid` だけを
    # 見ており、導出でも true を返していた。実測 403 本すべてが導出(明示 rangeAnchor は
    # 0 本)なので、記録を後から見ても静かな劣化が判別できなかった。
    # 可否そのもの(available)は「アンカーが解決できたか」の指標として据え置き、
    # 出所を別キーで併記する。
    rng = ict.get("range") or {}
    anchor_type = str(rng.get("anchorType") or "")
    return {"available": available, "missing": missing,
            "ictInputsPresent": sum(1 for key in ICT_COVERAGE_KEYS if available[key]),
            "ictInputsTotal": len(ICT_COVERAGE_KEYS),
            "rangeAnchorType": anchor_type or None,
            "rangeAnchorSource": rng.get("source"),
            "rangeAnchorDerived": bool(available["rangeAnchor"]
                                       and anchor_type.upper().endswith("_DERIVED")),
            "degraded": bool(missing)}


def ict_side_check(ict, side):
    """ADVISORY の side 別まとめ。``ok`` は **参考値**(武装可否には使わない)。"""
    if not ict:
        return None
    rng = ict.get("range") or {}
    dol = (ict.get("dol") or {}).get(side) or {}
    sess = ict.get("session") or {}
    notes = []
    favors = rng.get("favors")
    if favors is not None and not favors.get(side, False):
        notes.append("PREMIUM_DISCOUNT_WRONG_SIDE")
    if dol.get("run") == "HRLR":
        notes.append("HIGH_RESISTANCE_RUN")
    if sess.get("tradeable") is False:
        notes.append("OUTSIDE_KILLZONE")
    smt = ict.get("smt") or {}
    want = {"BUY": "BULLISH", "SELL": "BEARISH"}[side]
    if smt.get("available") and smt.get("bias") and smt["bias"] != want:
        notes.append("SMT_AGAINST")
    return {"ok": not notes, "notes": notes, "position": rng.get("position"),
            "run": dol.get("run"), "window": sess.get("window")}


# --- R11-D: ICT / SMT / VP を実判定へ接続する戦略層 ------------------------
# 旧 R10 の計算値は残すが、ここから先は表示専用ではない。candidate は
# 「モデル固有チェーン → 構造価格 → 走路 → score」の順で作り、最後に
# select_primary() がライブへ出す1件を決める。発注はこのファイルから行わない。

MODEL_ORDER = (
    "VP80_REVERSION", "TURTLE_SOUP_REVERSAL",
    "BREAKER_CONTINUATION", "OTE_FVG_PULLBACK",
    # ICTBACK の検証レシピ。既存モデルを上書きせず、1分足が明示的に
    # 届いたときだけ候補として観測する。実運用の昇格は別の契約で行う。
    "SILVER_BULLET_SWEEP_FVG",
)
MODEL_RANK = {name: len(MODEL_ORDER) - i for i, name in enumerate(MODEL_ORDER)}
EVAL_PHASES = {"EVAL_STRIKE", "PA_HARVEST"}
MODEL_MIN_R = 1.5
MODEL_APLUS_R = 2.8
MODEL_APLUS_RUNNER_R = 4.0   # ヘッドルーム加点は runner の R で判定する

# ICTBACK.mp4 の as_traded レシピ。ここは「動画の主張をコードへ写した
# 定義」であり、当リポジトリで独立検証済みの勝率・PFを意味しない。
SILVER_BULLET_MODEL = "SILVER_BULLET_SWEEP_FVG"
SILVER_BULLET_VERSION = "ICTBACK-AS-TRADED/1"
SILVER_BULLET_BAR_SEC = 60
SILVER_BULLET_ATR_LENGTH = 14
SILVER_BULLET_DISPLACEMENT_MULT = 1.5
SILVER_BULLET_PENETRATION_TICKS = 1
SILVER_BULLET_SWING_LEFT = 2
SILVER_BULLET_SWING_RIGHT = 2
SILVER_BULLET_LEVEL_K = 3
SILVER_BULLET_LOOKBACK_BARS = 90
SILVER_BULLET_MAX_AGE_BARS = 20
SILVER_BULLET_MAX_STALENESS_SEC = 180
SILVER_BULLET_TARGET_R = 2.0


#: ``NQX_SB_MODE`` を OFF と解釈する綴り。``NQX_DECISIVE_STRATEGY`` と同じ規則。
#: `OFF` だけを見ていたため、このリポジトリで一般的な `0` / `false` を書いても
#: **無言で CANDIDATE のまま**だった —— 停止スイッチが fail-open していた。
SILVER_BULLET_OFF_VALUES = {"OFF", "0", "FALSE", "NO", "DISABLED"}


def silver_bullet_mode():
    """Silver Bullet の候補出力モード。

    既定は ``CANDIDATE`` だが、候補自身に live validation / split target の
    blocker を付ける。``OFF`` は表示・候補生成を止めるための明示的な診断用。
    """
    raw = str(os.environ.get("NQX_SB_MODE", "CANDIDATE")).strip().upper()
    return "OFF" if raw in SILVER_BULLET_OFF_VALUES else "CANDIDATE"
# R27: SL の下限。上限しか無かったため、退化した SL ほど R が膨らんで高得点に
# なる逆転が起きていた(_geometry_blockers の docstring 参照)。
MIN_RISK_PT = 4.0            # 絶対下限
# ノイズフロア倍率。model_stop_buffer と同じ 0.5 を使う ——「SL がバッファ 1 本分
# より狭い」= 構造ではなく退化、という判定にするため。1.0 にすると実測で正当な
# BREAKER/TURTLE まで 27 本落ちた(武装 98 → 71)。狙いは 2pt 級の退化を潰すこと。
# R42: use one full median 3-minute bar as the structural stop budget.  A
# half-bar floor let an 8-13pt stop score better merely because R used a smaller
# denominator.  Replay of 411 frozen cycles preserved 122/130 ARMED candidates
# while moving median SL 17.25 -> 30.00pt and minimum 8.00 -> 15.25pt.
STOP_BUFFER_NF_MULT = 1.0
MIN_RISK_NF_MULT = STOP_BUFFER_NF_MULT
# OTE は risk = 0.085*span + buffer。span の下限が無いと退化する。
MIN_OTE_SPAN_NF_MULT = 6.0
MIN_OTE_SPAN_PT = 40.0

# A 判定に最低 1 つ必要な「構造と独立したデータ源による確認」。
# displacement やチェーン完成は構造そのものなので含めない(構造だけで A に
# なる抜け道になる)。VP 受容も価格構造なので含めない。
# 独立源 = ICT のレンジ位置(HTF アンカー)、SMT(相関銘柄)、CVD / CBC(注文
# フロー)、PO3(セッション構造)、OTE+FVG の合流。
#
# R37: `IMAGE_MODEL_ALIGNMENT` をここから外した。理由は 2 つあり、どちらも
# 「画像カタログ層を弱める」ためではない。
#
#   1. 定義に合わない。ここは**独立したデータ源**の集合だが、画像カタログ層は
#      セットアップの構造を作ったのと**同じ 3 分足**から再計算した読みであって、
#      別の源ではない。含めていたのは分類の誤り。
#   2. カタログ自身の契約に反する。`build_strategy_matrix` は
#      `hardGateImpact: "RANKING_ONLY"` を宣言し、IMAGE_STRATEGY_CATALOG.md §8 も
#      「口座リスクや CVD 制約をすり抜けるハードゲートにはならない」と明記する。
#      ここに入れると `NO_CONFIRMATION` を単独で解除でき、宣言が嘘になる。
#
# 実測(403 サイクル): ここに残したまま層へ通電すると、新規武装が最大 111 件増え、
# その 110 件は画像合議が**唯一の**確認になる(grade A 80 / A+ 30)。
# 層は引き続き score へ ±2 で効き、順位と strategyBias を動かす —— 武装の
# 唯一の鍵にならないだけである。
CONFIRMATION_EVIDENCE = frozenset({
    "ICT_LOCATION", "SMT_ALIGNED", "CVD_ALIGNED", "CBC_ALIGNED", "PO3_ALIGNED",
    "OTE_FVG_CONFLUENCE",
})


def _tick_price(value):
    """MNQの0.25pt tickへ丸める。入力を捏造せず、有限値だけを返す。"""
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value):
        return None
    return round(round(value / TICK) * TICK, 2)


def _strategy_price(bundle, bars):
    snapshot = bundle.get("snapshot") if isinstance(bundle.get("snapshot"), dict) else {}
    price = _num(bundle, "price", "last")
    if price is None:
        price = _num(snapshot, "price", "last")
    if price is None and bars:
        price = bars[-1].get("c")
    return _tick_price(price)


def route_regime(bundle, result):
    """状態を停止器ではなく候補モデルのルーターへ変換する。"""
    raw = bundle.get("regime")
    snapshot = bundle.get("snapshot") if isinstance(bundle.get("snapshot"), dict) else {}
    raw = raw or snapshot.get("regime") or "MX"
    regime = str(raw).upper().strip()
    rotation = (result.get("rotation") or {}).get("verdict")
    if rotation == "ROTATION":
        return {"regime": regime, "rotation": True,
                "preferred": {"VP80_REVERSION", "TURTLE_SOUP_REVERSAL"},
                "suppressed": {"BREAKER_CONTINUATION", "OTE_FVG_PULLBACK"}}
    if regime == "BA":
        preferred = {"VP80_REVERSION", "TURTLE_SOUP_REVERSAL"}
        suppressed = {"BREAKER_CONTINUATION"}
    elif regime in {"TR", "EX", "ER"}:
        preferred = {"BREAKER_CONTINUATION", "OTE_FVG_PULLBACK"}
        suppressed = {"TURTLE_SOUP_REVERSAL"}
    elif regime == "TX":
        preferred = {"TURTLE_SOUP_REVERSAL", "BREAKER_CONTINUATION"}
        suppressed = set()
    else:
        # R37: ここは以前 `preferred = set(MODEL_ORDER)` で、**全4モデルに +2** を
        # 配っていた。`bundle["regime"]` を書き込むコードはリポジトリに存在せず
        # (全参照が `.get("regime", "MX")` の読み出し)、実データは 403/403 が "MX"。
        # つまり「入力が無い」が「最大加点」に化けており、チェーン完成 +3 と
        # 合わせて外部入力ゼロで素点 8 = grade A に届いていた。
        # ルーティングの意見が無いなら中立にする —— 下流の else 分岐が +1 を配る。
        preferred = set()
        suppressed = set()
    return {"regime": regime, "rotation": False,
            "regimeKnown": regime in {"BA", "TR", "EX", "ER", "TX"},
            "preferred": preferred, "suppressed": suppressed}


def _bar_for_time(bars, timestamp):
    if timestamp is None:
        return None
    try:
        target = int(timestamp)
    except (TypeError, ValueError):
        return None
    return min(bars, key=lambda bar: abs(int(bar["t"]) - target), default=None)


def model_stop_buffer(nf):
    return max(STOP_BUFFER_NF_MULT * float(nf or 0), 2.0)


def _chain_prices(chain, bars, level_price, nf):
    """連鎖の極値を構造SLの外側へ置くための純粋な価格計算。"""
    start = chain.get("sweepBarT") if chain.get("type") == "SWEEP" else chain.get("breakBarT")
    bar = _bar_for_time(bars, start)
    if bar is None:
        return None, None
    buffer = model_stop_buffer(nf)
    side = chain.get("side")
    # R88: scan_chain が LIVE でスイープ全体の極値を載せたときだけ使う(OFF では無い)。
    # R90: FLIP(BREAKER)も同じ形。scan_flip が LIVE で起点の極値を載せたときだけ使う。
    extreme = chain.get("sweepExtreme") if chain.get("type") == "SWEEP" else chain.get("flipExtreme")
    if side == "BUY":
        low = float(bar["l"]) if extreme is None else min(float(bar["l"]), float(extreme))
        stop = min(low, float(level_price)) - buffer
    else:
        high = float(bar["h"]) if extreme is None else max(float(bar["h"]), float(extreme))
        stop = max(high, float(level_price)) + buffer
    return _tick_price(stop), int(bar["t"])


def _directional_levels(levels, entry, side, exclude=None):
    exclude = set(exclude or ())
    if side == "BUY":
        return sorted((lv for lv in levels if not lv.get("dynamic")
                       and lv.get("label") not in exclude
                       and lv.get("price") > entry), key=lambda lv: lv["price"])
    return sorted((lv for lv in levels if not lv.get("dynamic")
                   and lv.get("label") not in exclude
                   and lv.get("price") < entry), key=lambda lv: lv["price"], reverse=True)


# ラダーの規律。発注契約は splitPlan.targetCount = 2 を 3 箇所で独立に強制
# している(nqx_state / state_machine.js / execution_intent)。実サイクル 403 本の
# 再生では、構築された 246 シナリオのうち **212 (86%)** が本数か順序の不整合で
# ブローカー到達前に拒絶されていた。DOL を無条件に先頭へ置いていたのが原因で、
# DOL は 64% の確率で **最も遠い** 目標だった(= TP1 が runner より遠い)。
LADDER_MIN_SEP_R = 0.5        # runner は TP1 より実質的に遠いこと
# runner の引き戻し先。operational-gates.md:656 は「tier <= 2」と書くが、
# tier<=2 は月足/週足/前日のみで、実サイクルでは最遠 rung の 30/213 しか
# 該当せず、文字通り適用すると runner の約 86% が消える。tier<=3 + preferred。
# 階層判定は R4 §3.1 の level_tier() を再利用する(dedupe と同じ規則)。
RUNNER_TIER_MAX = 3
PREFERRED_LABELS = frozenset({"DOL", "VA_TARGET"})
DEDUPE_PT = 2.0               # TICK では 0.25pt 差の 2 本が別 rung として残る


def target_pool(entry, stop, side, levels, ict, min_r=MODEL_MIN_R, extra=None):
    """方向・最小R・重複を満たす候補を **距離順** に返す。順位付けはしない。

    DOL も明示レベルも同じ min_r フィルタを通す。DOL を素通しする経路は
    ここに存在しない(それが 86% 拒絶の原因だった)。
    """
    risk = abs(float(entry) - float(stop))
    if risk <= 0:
        return []
    raw = list(extra or ())
    dol = (ict.get("dol") or {}).get(side) if isinstance(ict, dict) else None
    if dol and dol.get("target") is not None:
        raw.append((float(dol["target"]), "DOL"))
    for level in _directional_levels(levels, entry, side):
        raw.append((float(level["price"]), str(level["label"])))
    pool = []
    for target, label in raw:
        if side == "BUY" and target <= entry:
            continue
        if side == "SELL" and target >= entry:
            continue
        r = abs(target - entry) / risk
        if r < min_r:
            continue
        priced = _tick_price(target)
        if any(abs(priced - old[0]) < DEDUPE_PT for old in pool):
            continue
        pool.append((priced, label, round(r, 2), level_tier(label)))
    pool.sort(key=lambda item: abs(item[0] - entry))
    return pool


def model_targets(entry, stop, side, levels, ict, min_r=MODEL_MIN_R, preferred=None, extra=None):
    """TP1 = 最も近い到達可能目標 / RUNNER = 最も遠い「質のある」目標。2本か0本。

    DOL は「最優先で選ぶ」が「先頭に置く」ではない。先頭に置くと TP1 が
    runner より遠くなり、下流の方向チェックが弾く。
    """
    pool = target_pool(entry, stop, side, levels, ict, min_r, extra)
    if len(pool) < 2:
        return []
    risk = abs(float(entry) - float(stop))
    tp1 = pool[0]
    prefer = set(preferred or ()) | PREFERRED_LABELS
    tail = pool[1:]
    quality = [item for item in tail
               if item[1] in prefer or item[3] <= RUNNER_TIER_MAX]
    runner = (quality or tail)[-1]
    if abs(runner[0] - tp1[0]) / risk < LADDER_MIN_SEP_R:
        return []
    return [(tp1[0], tp1[1], tp1[2]), (runner[0], runner[1], runner[2])]


def _cbc_score(snapshot, side):
    data = snapshot.get("cbc") if isinstance(snapshot, dict) else None
    if not isinstance(data, dict):
        return 0, None
    want = "bullish" if side == "BUY" else "bearish"
    vals = [str(data.get(key, "inside")).lower() for key in
            ("market", "cbc", "openingRange", "emaCloud", "vwap", "ema200")]
    aligned = sum(value == want for value in vals)
    opposed = sum(value in {"bullish", "bearish"} and value != want for value in vals)
    return (1 if aligned >= 4 else -1 if opposed >= 4 else 0), aligned


#: CVD の方向を載せてくるキーの別名。プロバイダによって名前が違う。
CVD_BIAS_KEYS = ("bias", "side", "trend", "direction")


def cvd_bias(value):
    """CVD が**方向として使える形**なら BULLISH/BEARISH を返す。それ以外は None。

    R37: 以前は `_cvd_score` と `cvd_health` が別々に形を判定しており、
    素の int を前者は「無得点」、後者は「FRESH かつ A+ 許可」と扱っていた。
    健全性と採点が同じ事実を別々に判断していたのが根本原因なので、
    judgement をこの 1 関数に集約する。**片方だけ変えられない構造にする。**
    """
    if not isinstance(value, dict):
        return None
    for key in CVD_BIAS_KEYS:
        text = str(value.get(key) or "").upper()
        if text in {"BULLISH", "BEARISH"}:
            return text
    return None


def _cvd_score(bundle, snapshot, side):
    value = bundle.get("cvd") if isinstance(bundle, dict) else None
    value = value if value is not None else snapshot.get("cvd") if isinstance(snapshot, dict) else None
    raw = cvd_bias(value)
    if raw is None:
        return 0, None
    want = {"BUY": "BULLISH", "SELL": "BEARISH"}[side]
    return (1 if raw == want else -1), raw


CVD_MAX_FETCH_ATTEMPTS = 2
CVD_STALL_SAMPLES = 3


def cvd_health(bundle, snapshot, bars):
    """CVDの再取得状態とA+可否を一つの機械可読契約へまとめる。

    欠落／停止は「シグナルなし」ではなく、まず監視側に再取得を要求する。
    それでも無ければ価格構造による A は残すが、A+を許可しない。古い値を
    方向根拠に再利用しない。
    """
    source = bundle if isinstance(bundle, dict) else {}
    snap = snapshot if isinstance(snapshot, dict) else {}
    raw = source.get("cvd", snap.get("cvd"))
    meta = source.get("cvdMeta") if isinstance(source.get("cvdMeta"), dict) else {}
    if not meta and isinstance(snap.get("cvdMeta"), dict):
        meta = snap.get("cvdMeta")
    try:
        # attempts includes the initial study read.  A bundle that got as far
        # as this evaluator has already made that first read, so a missing
        # value without metadata still has exactly one retry remaining.
        attempts = int(meta.get("attempts", source.get("cvdAttempts", 1)))
    except (TypeError, ValueError):
        attempts = 0
    history = meta.get("history", snap.get("cvdHistory", []))
    values = []
    if isinstance(history, list):
        for item in history[-CVD_STALL_SAMPLES:]:
            value = item.get("value") if isinstance(item, dict) else item
            try:
                values.append(float(value))
            except (TypeError, ValueError):
                values = []
                break
    stalled = False
    if len(values) == CVD_STALL_SAMPLES and len(set(values)) == 1 and len(bars) >= CVD_STALL_SAMPLES:
        recent = bars[-CVD_STALL_SAMPLES:]
        moved = max(b["h"] for b in recent) - min(b["l"] for b in recent) >= TICK
        stalled = moved
    explicit = str(meta.get("status") or "").upper()
    cvd_at = meta.get("at") or source.get("cvdAt") or snap.get("cvdAt")
    sample_epoch, bundle_epoch = _iso_epoch(cvd_at), _iso_epoch(source.get("at"))
    timestamp_stale = bool(sample_epoch is not None and bundle_epoch is not None and
                           abs(bundle_epoch - sample_epoch) > 600)
    # R37: 健全性が「値が入っているか」しか見ていなかった。素の int でも
    # `status=FRESH / aplusAllowed=True` を返す一方、`_cvd_score` は dict の bias を
    # 要求するので加点は常に 0 —— **「A+ 上限化も効かず、確認にもならない」**という
    # 最悪の両立だった(実測 403 本中 401 本が素の int、A+ ARMED 43 本がこの状態で承認)。
    # `_cvd_score` が方向として使える形かどうかを健全性の条件に含める。判定条件は
    # `_cvd_score` と同じキー・同じ語彙にそろえてある(片方だけ変えないこと)。
    unusable = cvd_bias(raw) is None
    unavailable = (raw is None or explicit in {"MISSING", "UNAVAILABLE", "STALE", "STALLED"}
                   or stalled or timestamp_stale or unusable)
    retry_required = unavailable and attempts < CVD_MAX_FETCH_ATTEMPTS
    reason = ("CVD_STALLED" if stalled or explicit in {"STALE", "STALLED"} else
              "CVD_TIMESTAMP_STALE" if timestamp_stale else
              "CVD_BIAS_UNUSABLE" if raw is not None and unusable else
              "CVD_MISSING") if unavailable else None
    return {
        "status": "RETRY_REQUIRED" if retry_required else "UNAVAILABLE_A_CAP" if unavailable else "FRESH",
        "available": not unavailable,
        "freshness": "STALE" if unavailable else "FRESH",
        "aplusAllowed": not unavailable,
        "attempts": attempts,
        "maxAttempts": CVD_MAX_FETCH_ATTEMPTS,
        "refreshRequired": retry_required,
        "refresh": ({"provider": meta.get("provider") or source.get("cvdSource"),
                     "nextAttempt": attempts + 1, "reason": reason}
                    if retry_required else None),
        "reason": reason,
        "at": cvd_at,
    }


def _resting_limit_ok(candidate, price):
    """指値として成立するか。建値が現値の **先** にあり、遠すぎないこと。

    order.py:784-792 は即約定する指値を拒否する(買いは建値 < 現値、売りは
    建値 > 現値)。ここで同じ向きを要求しておかないと、発注段階で必ず落ちる。
    """
    try:
        entry = float(candidate["entry"]); stop = float(candidate["stop"]); last = float(price)
    except (TypeError, ValueError, KeyError):
        return False
    risk = abs(entry - stop)
    if risk <= 0:
        return False
    long = stop < entry
    # 建値が現値の先にあること = まだ到達していないこと。
    if long and not (entry < last):
        return False
    if not long and not (entry > last):
        return False
    return abs(last - entry) / risk <= LIMIT_MAX_GAP_R


#: Gann 1×1 に「乗っている」とみなす許容。価格単位に対する比。
#: 実測(348 候補)の建値→1×1 距離は中央値 0.222 単位。0.05 で 14% が発火する。
#: 角度・円を**全部**対象にすると 0.08 で 48% が当たり、選択的でなくなった
#: (9 本もあれば必ず何かが近い)。Gann の核である 1×1 だけを見る。
GANN_TOUCH_TOL_UNITS = 0.05
#: 経路(建値→TP1)を 1×1 が横切るときの減点。roadblock の扱いは HRLR と同じ。
GANN_ROADBLOCK_PENALTY = 1
GANN_NODE_BONUS = 1


def _score_gann(candidate, strategy_matrix):
    """Gann 1×1 を採点へ反映する(R33)。

    **実証的な裏付けは無い。** 利用者の明示的な指示で、裏付けが無くても
    トレードに影響させる。影響のさせ方は既存の HRLR / killzone と同じ
    「加点・減点」に留め、ハードゲートにも確認要素(CONFIRMATION_EVIDENCE)にも
    しない —— 確認要素にすると、裏付けの無いものが単独で武装根拠になってしまう。

    規則:
      建値が 1×1 に乗っている        → +1 `GANN_1X1_NODE`
      建値→TP1 の経路を 1×1 が横切る → −1 `GANN_1X1_ROADBLOCK`
    両方に当たることはない(乗っていれば経路の端であって内側ではない)。
    """
    models = (strategy_matrix or {}).get("models")
    gann = (models or {}).get("gann") if isinstance(models, dict) else None
    # dict でなければ触らない。`gann` が文字列でも AttributeError にしない
    # (strategy_matrix は下流から来る外部データなので型を信用しない)。
    if not isinstance(gann, dict) or gann.get("status") != "COMPUTED":
        return
    anchor_row = gann.get("anchor")
    unit = anchor_row.get("priceUnit") if isinstance(anchor_row, dict) else None
    angles = gann.get("angles")
    one = next((row for row in (angles if isinstance(angles, list) else [])
                if isinstance(row, dict) and row.get("primary")), None)
    if not unit or unit <= 0 or not one:
        return
    try:
        level = float(one["price"])
        entry = float(candidate["entry"])
    except (KeyError, TypeError, ValueError):
        return
    targets = candidate.get("targets") or []
    candidate["gann"] = {"level": level, "unit": unit,
                         "distanceUnits": round(abs(level - entry) / unit, 3)}
    if abs(level - entry) <= GANN_TOUCH_TOL_UNITS * unit:
        candidate["score"] += GANN_NODE_BONUS
        candidate["evidence"].append("GANN_1X1_NODE")
        return
    if not targets:
        return
    try:
        tp1 = float(targets[0])
    except (TypeError, ValueError):
        return
    lo, hi = min(entry, tp1), max(entry, tp1)
    if lo + 1e-9 < level < hi - 1e-9:
        candidate["score"] -= GANN_ROADBLOCK_PENALTY
        candidate["penalties"].append("GANN_1X1_ROADBLOCK")


def _geometry_blockers(candidate):
    """最終的な entry/stop/targets に対する不変条件。採点の経路に依らず必ず通す。

    - SL 距離は sl_cap_pt() 以下
    - SL 距離は **ノイズフロア以上**(R27。下記参照)
    - 先頭目標の R は MODEL_MIN_R 以上
    - 方向と価格の並びが整合(BUY: stop < entry < target / SELL: 逆)

    R27 で下限を足した理由: それまで全経路の risk チェックが**上限のみ**だった
    (msnr_gate / execution_contract / state_machine.js / monitor_publish /
    autotrade_engine のいずれも `risk > cap` しか見ない)。実効的な下限は
    model_stop_buffer の 2.0pt 定数だけで、OTE は risk = 0.085*span + buffer
    なので狭いレンジで簡単にそこへ落ちる。しかも risk は R の分母なので、
    **退化した SL ほど R が膨らんで高得点になる**という逆転が起きていた。
    ノイズフロア未満の SL は市場の平常の揺れで刈られる。
    """
    out = []
    entry, stop = candidate.get("entry"), candidate.get("stop")
    side = candidate.get("side")
    targets = candidate.get("targets") or []
    target_r = candidate.get("targetR") or []
    try:
        entry, stop = float(entry), float(stop)
    except (TypeError, ValueError):
        return ["GEOMETRY_INVALID"]
    risk = abs(entry - stop)
    if risk > sl_cap_pt() + 1e-9:
        out.append("RISK_CAP_EXCEEDED")
    nf = candidate.get("noiseFloor")
    floor = MIN_RISK_PT
    try:
        if nf is not None:
            floor = max(floor, MIN_RISK_NF_MULT * float(nf))
    except (TypeError, ValueError):
        pass
    if risk < floor - 1e-9:
        out.append("RISK_BELOW_NOISE")
    if not targets or not target_r or target_r[0] < MODEL_MIN_R - 1e-9:
        out.append("TARGET_HEADROOM_INSUFFICIENT")
    if targets:
        first = float(targets[0])
        ok = (stop < entry < first) if side == "BUY" else (first < entry < stop) if side == "SELL" else False
        if not ok:
            out.append("GEOMETRY_INVALID")
    return out


def _finalize_candidate(candidate, ict):
    """採点後の一意な等級・CVD上限・武装状態を確定する。

    幾何の不変条件はここで再検証する。採点の途中で entry/stop が差し替わる
    モデル(VP80 / OTE)があり、採点時の判定だけでは最終値を保証できない。
    """
    candidate["hardBlockers"] = list(candidate.get("hardBlockers") or []) + _geometry_blockers(candidate)
    # 独立した確認要素が一つも無いセットアップは A にしない。構造(チェーン)
    # とレジームと R:R だけで 7 点に届く設計だったため、実サイクル 403 本で
    # 61 の異なるセットアップが武装していた。確認要素 = ICT 位置・SMT・
    # CVD・CBC・VP 受容・画像カタログ合議・displacement のいずれか。
    confirmations = CONFIRMATION_EVIDENCE & set(candidate.get("evidence") or [])
    candidate["confirmations"] = sorted(confirmations)
    if not confirmations:
        candidate["hardBlockers"].append("NO_CONFIRMATION")
    cvd = (ict or {}).get("cvd") or {}
    # targetR だけ切って targets を切らなかったため、実サイクルで 35 候補が
    # len(targets)=4 / len(targetR)=3 の位置ずれを起こしていた。3 本まとめて切る。
    keep = len(candidate.get("targetR") or [])
    candidate["targets"] = (candidate.get("targets") or [])[:keep]
    candidate["targetLabels"] = (candidate.get("targetLabels") or [])[:keep]
    candidate["targetR"] = (candidate.get("targetR") or [])[:keep]
    grade = "A+" if candidate["score"] >= 9 else "A" if candidate["score"] >= 7 else "B"
    if grade == "A+" and not cvd.get("aplusAllowed", True):
        grade = "A"
        candidate["evidence"].append("CVD_A_PLUS_CAPPED")
        if cvd.get("refreshRequired"):
            candidate["penalties"].append("CVD_REFRESH_REQUIRED")
        else:
            candidate["penalties"].append("CVD_UNAVAILABLE_A_CAP")
    candidate["grade"] = grade
    candidate["cvdHealth"] = cvd
    candidate["hardBlockers"] = list(dict.fromkeys(candidate["hardBlockers"]))
    candidate["penalties"] = list(dict.fromkeys(candidate["penalties"]))
    candidate["evidence"] = list(dict.fromkeys(candidate["evidence"]))
    # 2026-09-04 ユーザー決定: B も発注可能にする。等級は順位付け(A+ > A > B)にだけ
    # 残し、ハードブロッカーが無ければ B も ARMED になる。
    candidate["allowed"] = not candidate["hardBlockers"] and grade in {"A", "A+", "B"}
    candidate["state"] = "ARMED" if candidate["allowed"] else "WATCH"
    return candidate


def _closed_bars_for(bundle, key, bar_sec, max_bars=MAX_INPUT_BARS):
    """Optional timeframe の確定足を返す。欠損時は空配列(推測しない)。"""
    snapshot = bundle.get("snapshot") if isinstance(bundle, dict) else {}
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    raw = snapshot.get(key)
    if not isinstance(raw, list):
        return []
    bars = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        t = _num(item, "t", "time")
        o = _num(item, "o", "open")
        h = _num(item, "h", "high")
        l = _num(item, "l", "low")
        c = _num(item, "c", "close")
        if None in (t, o, h, l, c) or h < l or h < max(o, c) or l > min(o, c):
            continue
        # Provider の epoch 単位が混ざっていても、価格や時刻を補間しない。
        if t > 10_000_000_000:
            t /= 1000.0
        bars.append({"t": t, "o": o, "h": h, "l": l, "c": c})
    bars.sort(key=lambda row: row["t"])
    ref = _epoch(bundle.get("priceAt")) or _epoch(bundle.get("at")) \
        if isinstance(bundle, dict) else None
    if ref is None:
        # 時刻が証明できない場合は最後の形成中候補を使わない。
        return bars[:-1][-max_bars:] if bars else []
    return [row for row in bars if row["t"] + bar_sec <= ref][-max_bars:]


def _silver_bullet_window(timestamp):
    """Sweep 時刻の窓を注釈する。窓は hard gate ではない。"""
    try:
        et = datetime.fromtimestamp(float(timestamp), tz=ET_ZONE)
        minute = et.hour * 60 + et.minute
    except (TypeError, ValueError, OverflowError, OSError):
        return None
    for start, end, label in ((3 * 60, 4 * 60, "LDN_OPEN_SB"),
                              (10 * 60, 11 * 60, "NY_AM_SB"),
                              (14 * 60, 15 * 60, "NY_PM_SB")):
        if start <= minute < end:
            return label
    return None


def _silver_bullet_atr(bars, index, length=SILVER_BULLET_ATR_LENGTH):
    """index より前だけで ATR を計算する(no look-ahead)。"""
    if index < length or index > len(bars):
        return None
    sample = bars[index - length:index]
    ranges = []
    for pos, bar in enumerate(sample, start=index - length):
        previous_close = bars[pos - 1]["c"] if pos > 0 else bar["o"]
        ranges.append(max(bar["h"] - bar["l"],
                          abs(bar["h"] - previous_close),
                          abs(bar["l"] - previous_close)))
    return sum(ranges) / len(ranges) if len(ranges) == length else None


def _silver_bullet_level_universe(bars, sweep_index, levels):
    """session refs + confirmed 1m swings のうち、各側の直近 K 本を返す。"""
    if not bars or not (0 <= sweep_index < len(bars)):
        return []
    reference = bars[sweep_index]["c"]
    rows = []

    for level in levels or []:
        if not isinstance(level, dict) or level.get("dynamic"):
            continue
        price = _num(level, "price")
        if price is None or not math.isfinite(price):
            continue
        freshness = str(level.get("freshness") or "").upper()
        if freshness in {"CONSUMED", "BROKEN"}:
            continue
        side = "BSL" if price > reference else "SSL" if price < reference else None
        if side:
            rows.append({"price": _tick_price(price), "side": side,
                         "kind": "SESSION_REF", "label": str(level.get("label") or "LEVEL")})

    left = SILVER_BULLET_SWING_LEFT
    right = SILVER_BULLET_SWING_RIGHT
    start = max(left, sweep_index - SILVER_BULLET_LOOKBACK_BARS)
    stop = max(start, sweep_index - right)
    for index in range(start, stop):
        window = bars[index - left:index + right + 1]
        bar = bars[index]
        if len(window) != left + right + 1:
            continue
        if bar["h"] >= max(row["h"] for row in window):
            price = _tick_price(bar["h"])
            if price > reference and not any(row["h"] > bar["h"]
                                             for row in bars[index + 1:sweep_index]):
                rows.append({"price": price, "side": "BSL", "kind": "SWING_HIGH",
                             "label": "1m swing high"})
        if bar["l"] <= min(row["l"] for row in window):
            price = _tick_price(bar["l"])
            if price < reference and not any(row["l"] < bar["l"]
                                             for row in bars[index + 1:sweep_index]):
                rows.append({"price": price, "side": "SSL", "kind": "SWING_LOW",
                             "label": "1m swing low"})

    unique = {}
    for row in rows:
        key = (row["side"], row["price"])
        # 同価格なら session reference を優先し、証跡を安定させる。
        current = unique.get(key)
        if current is None or (row["kind"] == "SESSION_REF" and
                               current["kind"] != "SESSION_REF"):
            unique[key] = row
    out = []
    for side in ("BSL", "SSL"):
        side_rows = sorted((row for row in unique.values() if row["side"] == side),
                           key=lambda row: abs(row["price"] - reference))
        out.extend(side_rows[:SILVER_BULLET_LEVEL_K])
    return out


def _silver_bullet_fvg_after(bars, sweep_index, side):
    """Sweep後の最初の1m displacement FVGを、到達状態込みで返す。"""
    last_impulse = min(len(bars) - 2,
                       sweep_index + 1 + SILVER_BULLET_MAX_AGE_BARS)
    for impulse_index in range(sweep_index + 1, last_impulse + 1):
        if impulse_index < SILVER_BULLET_ATR_LENGTH:
            continue
        a, impulse, c = (bars[impulse_index - 1], bars[impulse_index],
                         bars[impulse_index + 1])
        atr = _silver_bullet_atr(bars, impulse_index)
        body = abs(impulse["c"] - impulse["o"])
        if atr is None or body < SILVER_BULLET_DISPLACEMENT_MULT * atr:
            continue
        if side == "BUY":
            if impulse["c"] <= impulse["o"] or c["l"] - a["h"] < TICK:
                continue
            lo, hi = a["h"], c["l"]
        else:
            if impulse["c"] >= impulse["o"] or a["l"] - c["h"] < TICK:
                continue
            lo, hi = c["h"], a["l"]
        mid = _tick_price((lo + hi) / 2.0)
        fill_index = None
        invalid_reason = None
        touched = False
        for probe_index, probe in enumerate(bars[impulse_index + 2:],
                                             start=impulse_index + 2):
            touched = touched or (probe["l"] <= mid if side == "BUY"
                                  else probe["h"] >= mid)
            through = (probe["l"] <= mid - TICK if side == "BUY"
                       else probe["h"] >= mid + TICK)
            full = (probe["l"] <= lo if side == "BUY" else probe["h"] >= hi)
            body_break = (probe["c"] <= lo if side == "BUY" else probe["c"] >= hi)
            if full or body_break:
                invalid_reason = "SB_FVG_FILLED_BEFORE_TRADE_THROUGH" \
                    if fill_index is None else "SB_AMBIGUOUS_FILL_AND_FULL_FILL"
                break
            if through:
                fill_index = probe_index
                break
        if invalid_reason:
            continue
        age_bars = len(bars) - (impulse_index + 2)
        if age_bars > SILVER_BULLET_MAX_AGE_BARS:
            continue
        return {
            "lo": _tick_price(lo), "hi": _tick_price(hi), "mid": mid,
            "createdAt": c["t"], "impulseAt": impulse["t"],
            "impulseIndex": impulse_index, "ageBars": age_bars,
            "displacementBody": round(body, 2), "atr14": round(atr, 2),
            "displacementR": round(body / atr, 2), "timeframe": "1m",
            "arrivalState": ("TRADE_THROUGH" if fill_index is not None
                             else "TOUCHED_NOT_FILLED" if touched else "UNTOUCHED"),
            "entryFilled": fill_index is not None,
            "fillAt": bars[fill_index]["t"] if fill_index is not None else None,
            "eligible": True,
        }
    return None


def _silver_bullet_candidate(setup, bundle, nf):
    """動画レシピの幾何を既存候補スキーマへ写像する(発注はしない)。"""
    if nf is None or nf <= 0:
        return None
    side = setup["side"]
    bars = setup["bars"]
    sweep_index = setup["sweepIndex"]
    impulse_index = setup["fvg"]["impulseIndex"]
    entry = _tick_price(setup["fvg"]["mid"])
    buffer = model_stop_buffer(nf)
    scoped = bars[sweep_index:impulse_index + 1]
    swing = (min(row["l"] for row in scoped) if side == "BUY"
             else max(row["h"] for row in scoped))
    stop = _tick_price(swing - buffer if side == "BUY" else swing + buffer)
    if entry is None or stop is None or abs(entry - stop) <= 0:
        return None
    risk = abs(entry - stop)
    target = _tick_price(entry + SILVER_BULLET_TARGET_R * risk
                         if side == "BUY" else entry - SILVER_BULLET_TARGET_R * risk)
    if target is None:
        return None
    chain = {
        "type": "SWEEP", "state": "SB_SWEEP_FVG", "side": side,
        "sweepBarT": bars[sweep_index]["t"], "dispBarT": bars[impulse_index]["t"],
        "dispBody": setup["fvg"]["displacementBody"],
    }
    candidate = {
        "model": SILVER_BULLET_MODEL, "side": side, "entry": entry, "stop": stop,
        "targets": [target], "targetLabels": ["SB_FIXED_2R"],
        "targetR": [round(abs(target - entry) / risk, 2)],
        "level": setup["level"]["label"], "chain": chain,
        "noiseFloor": nf, "rangeAnchor": {}, "strategyModels": [SILVER_BULLET_MODEL],
        "strategyAlignment": 0, "evidence": [
            "SB_LIQUIDITY_SWEEP", "SB_DIRECTION_FROM_SWEEP", "SB_1M_FVG",
            "SB_DISPLACEMENT_1_5_ATR", "SB_50_PERCENT_ENTRY", "SB_NO_15M_BIAS",
            "SB_FIXED_2R",
        ],
        "hardBlockers": [
            # 動画自身が adverse selection は未価格付けと明記している。
            "SB_LIVE_FILL_UNVALIDATED",
            # 現行 Nightwatch は2枚をTP1/runnerへ分割するため、単一2Rを
            # 勝手に別Rへ変換して発注しない。
            "SB_FIXED_2R_SINGLE_TARGET",
        ],
        "penalties": [], "score": 4,
        "entryOrderType": "LIMIT", "restingLimit": True,
        "fillRule": "TRADE_THROUGH_1_TICK",
        "fvg": dict(setup["fvg"]),
        "silverBullet": {
            "version": SILVER_BULLET_VERSION,
            "window": _silver_bullet_window(bars[sweep_index]["t"]),
            "level": dict(setup["level"]),
            "sweepAt": bars[sweep_index]["t"],
            "displacementAt": bars[impulse_index]["t"],
            "displacementSwing": _tick_price(swing),
            "entryFilled": False,
            "fillRule": "TRADE_THROUGH_1_TICK",
            "targetR": SILVER_BULLET_TARGET_R,
        },
    }
    # 15m/HTF/SMT/CVDをこの動画レシピの条件へ混ぜない。独立確認が無い
    # ため _finalize_candidate は NO_CONFIRMATION も付ける。
    return _finalize_candidate(candidate, {})


def silver_bullet_evaluation(bundle, levels, nf):
    """ICTBACK の1m sweep→FVG候補を監査用に生成する。"""
    recipe = {
        "version": SILVER_BULLET_VERSION, "timeframe": "1m",
        "liquidity": "session_refs_plus_swings", "levelK": SILVER_BULLET_LEVEL_K,
        "penetrationTicks": SILVER_BULLET_PENETRATION_TICKS,
        "displacement": "body >= 1.5 * ATR(14)", "mssRequired": False,
        "entry": "50% FVG", "stop": "beyond displacement swing",
        "target": "fixed 2R", "windows": "annotation_only",
    }
    out = {"model": SILVER_BULLET_MODEL, "version": SILVER_BULLET_VERSION,
           "mode": silver_bullet_mode(), "status": "MISSING", "timeframe": "1m",
           "recipe": recipe, "bars": 0, "candidate": None, "reason": None}
    if silver_bullet_mode() == "OFF":
        out["status"], out["reason"] = "OFF", "SB_DISABLED"
        return out
    bars = _closed_bars_for(bundle, "bars1m", SILVER_BULLET_BAR_SEC)
    out["bars"] = len(bars)
    if len(bars) < SILVER_BULLET_ATR_LENGTH + 4:
        # 「取得していない」と「取得したが取得器が拒否した」を混ぜない。
        # tv_snapshot は間隔不一致の bars1m.json を落として理由をここへ残す。
        snapshot = bundle.get("snapshot") if isinstance(bundle, dict) else {}
        rejected_map = (snapshot or {}).get("barsRejected") if isinstance(snapshot, dict) else None
        rejected = rejected_map.get("bars1m") if isinstance(rejected_map, dict) else None
        if not bars and rejected:
            out["reason"], out["detail"] = "SB_1M_REJECTED", str(rejected)
        else:
            out["reason"] = "SB_1M_SOURCE_MISSING" if not bars else "SB_1M_INSUFFICIENT"
        return out
    ref = _epoch(bundle.get("priceAt")) or _epoch(bundle.get("at"))
    if ref is not None and ref - (bars[-1]["t"] + SILVER_BULLET_BAR_SEC) > SILVER_BULLET_MAX_STALENESS_SEC:
        out["reason"] = "SB_1M_STALE"
        return out
    deltas = [round(b["t"] - a["t"]) for a, b in zip(bars, bars[1:])]
    # 17:00–18:00 ET maintenance等の正当な時間飛びは許可するが、1分未満の
    # 列や1分の倍数でない列は1m OHLCとして扱わない。
    if deltas and any(delta < SILVER_BULLET_BAR_SEC or
                      delta % SILVER_BULLET_BAR_SEC != 0 for delta in deltas):
        out["reason"] = "SB_1M_CADENCE_INVALID"
        return out
    setups = []
    start = max(0, len(bars) - SILVER_BULLET_LOOKBACK_BARS)
    for sweep_index in range(start, len(bars) - 2):
        universe = _silver_bullet_level_universe(bars, sweep_index, levels)
        for level in universe:
            price = level["price"]
            bar = bars[sweep_index]
            swept_buy = (level["side"] == "SSL" and
                         bar["l"] <= price - SILVER_BULLET_PENETRATION_TICKS * TICK and
                         bar["c"] > price)
            swept_sell = (level["side"] == "BSL" and
                          bar["h"] >= price + SILVER_BULLET_PENETRATION_TICKS * TICK and
                          bar["c"] < price)
            side = "BUY" if swept_buy else "SELL" if swept_sell else None
            if side is None:
                continue
            fvg = _silver_bullet_fvg_after(bars, sweep_index, side)
            if not fvg:
                continue
            setup = {"side": side, "level": level, "sweepIndex": sweep_index,
                     "bars": bars, "fvg": fvg}
            setups.append(setup)
    out["setupsSeen"] = len(setups)
    # 同一方向で最も新しい(同率なら近いlevel)ものだけを primary 候補化する。
    latest = {}
    for setup in sorted(setups, key=lambda row: (row["fvg"]["createdAt"],
                                                 -abs(row["level"]["price"] -
                                                     row["bars"][row["sweepIndex"]]["c"])),
                        reverse=True):
        # 新しいFVGが既にfill/invalidなら、その方向の古い指値へ戻らない。
        latest.setdefault(setup["side"], setup)
    selected = {}
    for setup in latest.values():
        if setup["fvg"].get("entryFilled"):
            continue
        candidate = _silver_bullet_candidate(setup, bundle, nf)
        if candidate is None:
            continue
        last_price = _strategy_price(bundle, bars)
        if not _resting_limit_ok(candidate, last_price):
            continue
        selected[setup["side"]] = (setup, candidate)
    if selected:
        # 方向は既存 select_primary と同じく候補重複除去の責任範囲に置く。
        setup, candidate = next(iter(selected.values()))
        out.update({"status": "CANDIDATE", "candidate": candidate,
                    "window": _silver_bullet_window(setup["bars"][setup["sweepIndex"]]["t"]),
                    "selected": {
                        "side": setup["side"], "level": dict(setup["level"]),
                        "sweepAt": setup["bars"][setup["sweepIndex"]]["t"],
                        "fvgCreatedAt": setup["fvg"]["createdAt"],
                        "arrivalState": setup["fvg"]["arrivalState"],
                    }})
    else:
        out["status"], out["reason"] = "WATCH", "SB_NO_VALID_PENDING_SETUP"
    return out


def _po3_score(bundle, snapshot, side):
    value = bundle.get("po3") if isinstance(bundle, dict) else None
    value = value if value is not None else snapshot.get("po3") if isinstance(snapshot, dict) else None
    raw = str(value or "").upper()
    if raw in {"DISTRIBUTION_UP", "MANIPULATION_DOWN"}:
        return (1 if side == "BUY" else -1), raw
    if raw in {"DISTRIBUTION_DOWN", "MANIPULATION_UP"}:
        return (1 if side == "SELL" else -1), raw
    return 0, raw or None


def _candidate_for_chain(model, level, chain, bars, levels, ict, regime_info, bundle, nf,
                         strategy_matrix=None, entry=None, stop=None, targets_override=None,
                         extra_targets=None):
    """1候補を採点する。

    ``entry`` / ``stop`` を渡すモデル(OTE)は、その最終的な幾何で targets・
    R:R・SL上限まで評価する。ここを連鎖の幾何のまま採点すると、公開される
    targetR と加点根拠がずれ、SL上限も実際に出す注文とは別の距離で
    判定されてしまう。
    """
    side = chain.get("side")
    if entry is None:
        entry = _tick_price(level.get("price"))
        stop, _ = _chain_prices(chain, bars, entry, nf)
    else:
        entry, stop = _tick_price(entry), _tick_price(stop)
    if entry is None or stop is None:
        return None
    # R90 穴 1: VWAP が SL の近くにあれば SL を VWAP の外側へ逃がして**から**採点する。
    # targets・R・SL 上限・setup_identity(= decisionId)はすべて最終の SL で決まる。
    stop, vwap_audit = _apply_vwap_clearance(model, side, entry, stop, bundle, nf)
    # R103-1: SL の外側に未回収の流動性プールがあれば、その向こうへ逃がして**から**採点する
    # (SHADOW は記録だけ)。LIVE の場合も targets・R:R・SL 上限・decisionId は最終の SL で決まる。
    stop_before_pool = stop
    stop, pool_audit = _apply_pool_clearance(model, side, entry, stop, bars, levels, bundle, nf)
    # R103-3: 元の SL の外側 1N 以内にプールがある候補は、掃引→奪還を待つ(LIVE: 待つ間は WATCH、
    # 通れば SL は掃引極値の向こう。SHADOW: 記録だけ)。targets・R:R・上限・decisionId は最終の SL。
    stop, sweep_audit = _apply_sweep_gate(model, side, entry, stop_before_pool, stop, bars, levels,
                                          chain, bundle, nf)
    # R121: STDV アンカーは **最終の entry/stop が決まってから**作る(SL を動かす節の後)。
    # OFF / SHADOW では `stdv_extra` は必ず空で、targets はこの節の前と同一になる。
    stdv_policy = ict_stdv_policy()
    stdv_audit, stdv_extra = _stdv_for_chain(model, level, chain, bars, side, entry, stop,
                                             None, bundle, nf, stdv_policy)
    if targets_override is not None:
        targets = targets_override
    else:
        base_targets = model_targets(entry, stop, side, levels, ict, extra=extra_targets)
        targets = base_targets
        if stdv_extra:
            merged = list(extra_targets or ()) + list(stdv_extra)
            with_stdv = model_targets(entry, stop, side, levels, ict, extra=merged)
            runner_label = str(with_stdv[-1][1]) if len(with_stdv) == 2 else ""
            runner_is_stdv = runner_label.startswith("STDV_")
            if runner_is_stdv and not (stdv_policy.get("targets") or {}).get("runnerEligible"):
                # `-4` 等で runner が不当に遠くなるのを防ぐ。STDV 単独で遠い TP を
                # 正当化しない(docs/R121_ICT_STDV.md §4)。理由は監査に残す。
                stdv_audit["runnerRejected"] = True
            elif with_stdv and with_stdv != base_targets:
                targets = with_stdv
                stdv_audit["targetsApplied"] = True
                stdv_audit["baseTargets"] = [item[0] for item in base_targets]
    candidate = {
        "model": model, "side": side, "entry": entry, "stop": stop,
        "targets": [item[0] for item in targets],
        "targetLabels": [item[1] for item in targets],
        "targetR": [item[2] for item in targets],
        "level": level.get("label"), "chain": chain,
        # SL 下限の判定に使う。引数ではなく候補に持たせるのは、entry/stop を
        # 上書きしてから _finalize_candidate を再実行する経路(VP80 / OTE)でも
        # 落ちないようにするため(R25 の欠陥と同じ形を作らない)。
        "noiseFloor": nf,
        "rangeAnchor": (ict or {}).get("rangeAnchor") or {},
        "strategyModels": [], "strategyAlignment": 0,
        "evidence": [], "hardBlockers": [], "penalties": [], "score": 0,
    }
    if stdv_audit is not None:
        # R121: 投影と「読み」は記録専用。採点・等級・確認要素(CONFIRMATION_EVIDENCE)には
        # 入れない —— 現在地が投影目標に近いことだけで方向の確信を加算しないため。
        candidate["ictStdv"] = stdv_audit
        anchor = stdv_audit.get("anchor") or {}
        if anchor.get("projectionValid") is True:
            candidate["evidence"].append("ICT_STDV_ANCHOR")
        if stdv_audit.get("targetsApplied"):
            candidate["evidence"].append("ICT_STDV_TARGET")
            # TP が変わったので decisionId も変える(「価格を変えたが ID 不変」で
            # 過去 claim を流用しない)。setup_identity がこのキーを読む。
            candidate["stdvIdentity"] = "|".join(
                [str(anchor.get("anchorId"))] + [str(item[0]) for item in targets])
        if stdv_audit.get("runnerRejected"):
            candidate["evidence"].append("ICT_STDV_RUNNER_REJECTED")
        # 「読み」は最終の targets で引き直す(headroom が実際に出す注文と一致する形にする)。
        if anchor.get("anchorId"):
            try:
                import ict_stdv
                gate_last, _reason = gate_price(bundle)
                stdv_audit["read"] = ict_stdv.read_context(
                    anchor, side=side, entry=entry, stop=stop, price=gate_last,
                    price_known=gate_last is not None,
                    targets=[item[0] for item in targets])
            except Exception:  # noqa: BLE001 - 読みの失敗で候補を落とさない
                pass
    if vwap_audit is not None:
        candidate["vwapStop"] = vwap_audit
        if vwap_audit.get("applied"):
            # R90: SL を VWAP の外側へ逃がした印。記録専用(採点・確認要素にしない)。
            candidate["evidence"].append("VWAP_STOP_CLEARED")
    if pool_audit is not None:
        candidate["poolStop"] = pool_audit
        if pool_audit.get("applied"):
            # R103-1: SL をプールの向こうへ逃がした印。記録専用。
            candidate["evidence"].append("POOL_STOP_CLEARED")
        else:
            # SHADOW の観測。判定・採点・等級・decisionId には触れない。
            candidate["evidence"].extend(liquidity_pools.evidence_tags(pool_audit))
    if sweep_audit is not None:
        candidate["sweepGate"] = sweep_audit
        gate_state = sweep_audit.get("state")
        if sweep_audit.get("applied"):
            # R103-3: 掃引→奪還を確認してから建てた印。記録専用。
            candidate["evidence"].append(liquidity_pools.EVIDENCE_SWEEP_PASSED)
        elif str(sweep_audit.get("mode") or "") == "LIVE" and gate_state in ("PENDING", "STALE"):
            # LIVE: プールが SL の外側にある間は掃引を待つ(候補は WATCH)。
            candidate["hardBlockers"].append(
                liquidity_pools.BLOCKER_SWEEP_PENDING if gate_state == "PENDING"
                else liquidity_pools.BLOCKER_SWEEP_STALE)
        else:
            candidate["evidence"].extend(liquidity_pools.sweep_evidence_tags(sweep_audit))
    if not targets:
        candidate["hardBlockers"].append("TARGET_HEADROOM_INSUFFICIENT")
    if abs(entry - stop) > sl_cap_pt():
        candidate["hardBlockers"].append("RISK_CAP_EXCEEDED")
    if level.get("freshness") == "CONSUMED":
        candidate["hardBlockers"].append("ANCHOR_CONSUMED")
    candidate["score"] += 3  # モデル固有チェーン完成
    if model in regime_info["preferred"]:
        candidate["score"] += 2
        candidate["evidence"].append("REGIME_FIT")
    elif model in regime_info["suppressed"]:
        candidate["score"] -= 2
        candidate["penalties"].append("ROTATION_AGAINST_MODEL" if regime_info["rotation"]
                                      else "REGIME_MISMATCH")
    else:
        candidate["score"] += 1
    rng = (ict or {}).get("range") or {}
    favors = rng.get("favors") or {}
    if favors.get(side):
        candidate["score"] += 2
        candidate["evidence"].append("ICT_LOCATION")
    elif favors:
        # レンジが分かっていて逆側にいる: 減点する。以前は加点が付かないだけ
        # だったため、実サイクルで武装した 81 本すべてがこの状態だった。
        # SMT 逆行と同じ重さ(-2)。
        candidate["score"] -= 2
        candidate["penalties"].append("PREMIUM_DISCOUNT_WRONG_SIDE")
    else:
        # レンジ未取得(rangeAnchor 無し): 判定できないので増減なし。
        candidate["penalties"].append("RANGE_ANCHOR_MISSING")
    # ヘッドルーム加点は **runner** の R を見る。TP1 を「最も近い適格目標」に
    # 変えると TP1 の R 中央値が 2.42 -> 1.96 に落ち、旧閾値(2.8)の発火率が
    # 39% -> 8% になって等級が一斉に下がる。4.0R は新しい runner R の 61
    # パーセンタイル = 旧発火率 39% を再現する値。**リファクタを跨いで採点
    # 分布を一定に保つための較正であって、存在しない実績への当てはめではない。**
    if targets and candidate["targetR"][-1] >= MODEL_APLUS_RUNNER_R:
        candidate["score"] += 2
    elif targets and candidate["targetR"][0] >= MODEL_MIN_R:
        candidate["score"] += 1
    else:
        candidate["hardBlockers"].append("TARGET_HEADROOM_INSUFFICIENT")
    smt = (ict or {}).get("smt") or {}
    want = "BULLISH" if side == "BUY" else "BEARISH"
    if smt.get("available") and smt.get("freshness") == "FRESH" and smt.get("bias"):
        if smt["bias"] == want:
            candidate["score"] += 1
            candidate["evidence"].append("SMT_ALIGNED")
        else:
            candidate["score"] -= 2
            candidate["penalties"].append("SMT_AGAINST")
    snapshot = bundle.get("snapshot") if isinstance(bundle.get("snapshot"), dict) else {}
    htf = snapshot.get("htfContext") if isinstance(snapshot.get("htfContext"), dict) else {}
    htf_bias = htf.get("bias") if htf.get("status") == "ALIGNED" else None
    candidate["htfContext"] = {
        "status": htf.get("status"), "bias": htf_bias,
        "complete": htf.get("complete"),
    }
    if htf_bias == side:
        candidate["score"] += 1
        candidate["evidence"].append("HTF_ALIGNED")
    elif htf_bias in {"BUY", "SELL"}:
        candidate["score"] -= 1
        candidate["penalties"].append("HTF_CONFLICT")
    cbc, cbc_count = _cbc_score(snapshot, side)
    if cbc:
        candidate["score"] += cbc
        (candidate["evidence"] if cbc > 0 else candidate["penalties"]).append("CBC_ALIGNED" if cbc > 0 else "CBC_AGAINST")
    cvd, cvd_bias = _cvd_score(bundle, snapshot, side)
    if cvd and ((ict or {}).get("cvd") or {}).get("available"):
        candidate["score"] += 1 if cvd > 0 else -1
        (candidate["evidence"] if cvd > 0 else candidate["penalties"]).append("CVD_ALIGNED" if cvd > 0 else "CVD_AGAINST")
    po3, po3_state = _po3_score(bundle, snapshot, side)
    if po3:
        candidate["score"] += po3
        if po3 > 0:
            candidate["evidence"].append("PO3_ALIGNED")
    # The supplied image catalogue is now part of the candidate rank.  It is
    # directional confluence, not a replacement for MSNR, CVD, R:R, or risk.
    alignment = (strategy_matrix or {}).get("alignment") or {}
    try:
        own = int(alignment.get(side, 0))
        other = int(alignment.get("SELL" if side == "BUY" else "BUY", 0))
    except (TypeError, ValueError):
        own = other = 0
    delta = own - other
    candidate["strategyAlignment"] = delta
    candidate["strategyModels"] = list((strategy_matrix or {}).get("activeModels") or [])[:8]
    if delta >= 2:
        candidate["score"] += min(2, delta)
        candidate["evidence"].append("IMAGE_MODEL_ALIGNMENT")
    elif delta <= -2:
        candidate["score"] -= 1
        candidate["penalties"].append("IMAGE_MODEL_CONFLICT")
    dol = ((ict or {}).get("dol") or {}).get(side) or {}
    if dol.get("run") == "HRLR":
        candidate["score"] -= 1
        candidate["penalties"].append("HRLR")
    _score_gann(candidate, strategy_matrix)
    # CLAUDE.md §2 は killzone を加点・減点へ反映すると定めている。武装可否の
    # ハードゲートにはしない(ICT が探さない時間帯でも他条件が揃えば成立する
    # という ict_session() の設計はそのまま)。HRLR と同じ減点のみの扱いにし、
    # 既存のどのゲートも緩めない。
    session = (ict or {}).get("session") or {}
    if session.get("tradeable") is False:
        candidate["score"] -= 1
        candidate["penalties"].append("OUTSIDE_KILLZONE")
    elif session.get("tradeable") is True:
        candidate["evidence"].append("ICT_KILLZONE")
    if chain.get("dispBody", 0) >= 1.3 * nf:
        candidate["score"] += 1
        candidate["evidence"].append("DISPLACEMENT_FRESH")
    return _finalize_candidate(candidate, ict)


def candidate_vp80(result, bars, levels, ict, regime_info, bundle, nf, strategy_matrix=None):
    vp = result.get("vpPath") or {}
    if vp.get("state") not in {"VP_RE_ENTRY", "VP_ACCEPTED"} or vp.get("side") not in {"BUY", "SELL"}:
        return None
    side = vp["side"]
    edge = _find_level_price(levels, VAH_RE if side == "SELL" else VAL_RE)
    if edge is None:
        return None
    entry = _tick_price(bars[-1]["c"] if bars else edge)
    buffer = model_stop_buffer(nf)
    # R27: 走査を **今回のエピソード** に限る。
    #
    # それまでは窓 60 本(= 3 時間)すべてを走査していたため、3 時間前の無関係な
    # 突出まで SL に取り込んでいた。実測で SL 中央値 62pt(上限 60pt 超)、最大
    # 278pt、**101 候補中 53 本(52%)が RISK_CAP_EXCEEDED で破棄**されていた。
    # R25 は採点経路の統一だけを行い、SL の取得元は変えていなかった。
    # vpPath は今回のエピソードの起点 reEntryBarT を持っている。
    episode_t = vp.get("reEntryBarT")
    scoped = bars
    if episode_t is not None:
        try:
            scoped = [bar for bar in bars if int(bar["t"]) >= int(episode_t)] or bars
        except (TypeError, ValueError, KeyError):
            scoped = bars
    outside = [bar for bar in scoped if (bar["h"] > edge if side == "SELL" else bar["l"] < edge)]
    stop = _tick_price((max(bar["h"] for bar in outside) + buffer) if side == "SELL"
                       else (min(bar["l"] for bar in outside) - buffer)) if outside else None
    if entry is None or stop is None:
        return None
    # VA 復帰はエッジから反対側のエッジへ向かう型。建値が既に目標側へ抜けて
    # いれば「復帰は済んだ」状態で、追いかける根拠が無い(以前はこの状態を
    # 目標が建値の反対側にある候補として出し、GEOMETRY_INVALID になっていた)。
    if vp.get("target") is not None:
        vp_target = _tick_price(vp["target"])
        if vp_target is not None and ((side == "SELL" and entry <= vp_target)
                                      or (side == "BUY" and entry >= vp_target)):
            return None
    # VA target はモデル固有の目標だが、**min_r を素通りさせない**。
    # 以前は先頭に prepend していたため targetR[0] が MODEL_MIN_R 未満の
    # まま通り(実データに 1.4 が存在)、かつ targets が 4 本になって
    # 発注契約(ちょうど 2 本)から外れていた。pool へ渡して同じ関門を通す。
    extra = None
    if vp.get("target") is not None:
        extra = [(float(_tick_price(vp["target"])), "VA_TARGET")]
    # R90: targets は _candidate_for_chain が**最終の SL**(VWAP 逃がし後)で組む。
    # ここで先に組むと、SL が動いた候補の R・ラダーが古い SL の値のまま公開される。
    try:
        origin_t = int(episode_t) if episode_t is not None else None
    except (TypeError, ValueError):
        origin_t = None
    fake = {"type": "SWEEP", "side": side, "state": vp["state"],
            "sweepBarT": int(bars[-1]["t"]) if bars else None,
            # R103-3: 掃引ゲートは再突入エピソードの起点より後の掃引だけを見る。
            "originBarT": origin_t,
            "dispBody": max((abs(b["c"] - b["o"]) for b in bars[-3:]), default=0)}
    # 最終的な建値/SL をそのまま採点に渡す。以前は VA edge の幾何で採点して
    # から entry/stop を上書きしていたため、SL 上限も R 判定も別のトレードの
    # 値で通過していた(実サイクル 403 本中 199 本が武装し、A+ の SL が
    # 78〜116pt という異常の原因)。
    candidate = _candidate_for_chain("VP80_REVERSION", {"label": "VA edge", "price": edge,
                                                         "tier": 3, "mergedWith": [],
                                     "confluence": 1, "freshness": "FRESH"},
                                     fake, bars, levels, ict, regime_info, bundle, nf,
                                     strategy_matrix, entry=entry, stop=stop,
                                     extra_targets=extra)
    if candidate:
        candidate["evidence"].append("VP_ACCEPTED" if vp.get("state") == "VP_ACCEPTED" else "VP_RE_ENTRY")
        if vp.get("state") == "VP_ACCEPTED":
            candidate["score"] += 2
        _finalize_candidate(candidate, ict)
    return candidate


# --- R48: 特徴量タグ(EDGE_LEDGER M6/M7/M9) --------------------------------
# 採点・グレード・武装可否には一切影響しない。decision.evidence に載せて、
# モデル別スコアカードが「この特徴の有無で成績が分かれるか」を実測するための
# 分離キーである。重み付けは N が貯まるまで行わない(却下リスト参照)。

CLASSIC_TS_LOOKBACK_DAYS = 20     # Raschke原典: 新20日極値
CLASSIC_TS_MIN_AGE_SESSIONS = 4   # 旧極値が4セッション以上古いこと
CLASSIC_TS_LEVEL_TOL_PT = 2.0     # レベル統合(DEDUPE_PT)と同じ許容
DISP_RESEARCH_MULT = 1.5          # MPM研究の事前定義(直前20本レンジ中央値比)
DISP_RESEARCH_LOOKBACK = 20
LONDON_LEVEL_RE = re.compile(r"london", re.I)


def _daily_bars(bundle):
    snapshot = (bundle or {}).get("snapshot") or {}
    rows = snapshot.get("bars1d") or (bundle or {}).get("bars1d") or []
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            out.append({"t": row.get("t"), "h": float(row["h"]), "l": float(row["l"])})
        except (KeyError, TypeError, ValueError):
            continue
    return out


def classic_turtle_soup(level_price, side, daily_bars):
    """Raschke原典条件: 掃引水準が20日極値で、旧極値が4セッション以上古い。

    daily_bars は古→新の確定日足(最終行が直近確定日)。判定できない入力では
    False(推測で原典条件を名乗らない)。
    """
    try:
        price = float(level_price)
    except (TypeError, ValueError):
        return False
    rows = daily_bars[-(CLASSIC_TS_LOOKBACK_DAYS):]
    if len(rows) < CLASSIC_TS_LOOKBACK_DAYS:
        return False
    key = "h" if side == "SELL" else "l"
    values = [row[key] for row in rows]
    extreme = max(values) if side == "SELL" else min(values)
    if abs(price - extreme) > CLASSIC_TS_LEVEL_TOL_PT:
        return False
    extreme_index = (len(values) - 1) - (values[::-1].index(extreme))
    age_sessions = (len(values) - 1) - extreme_index
    return age_sessions >= CLASSIC_TS_MIN_AGE_SESSIONS


CHOP_LOOKBACK = 20            # R103-1: レベル横断の観測窓(直前 20 本の確定足)
CHOP_MIN_CROSSES = 3


def level_chopped(bars, price, lookback=CHOP_LOOKBACK, min_crosses=CHOP_MIN_CROSSES):
    """直前 ``lookback`` 本の終値がレベルを ``min_crosses`` 回以上横断したか(記録専用)。

    横断 = 隣り合う確定足の終値がレベルの反対側へ移ったこと。ちょうど同値の足は
    直前の側を引き継ぐ(同値だけで往復を数えない)。
    """
    value = _num({"p": price}, "p")
    if value is None:
        return False
    sides, crosses, last = [], 0, None
    for bar in (bars or [])[-lookback:]:
        close = _num(bar, "c", "close") if isinstance(bar, dict) else None
        if close is None or close == value:
            continue
        sides.append(1 if close > value else -1)
    for side in sides:
        if last is not None and side != last:
            crosses += 1
        last = side
    return crosses >= min_crosses


def feature_tags(chain, level, bars, bundle, strategy_matrix=None):
    """chain候補に付ける記録専用タグ。判定に影響しない。"""
    tags = []
    label = str((level or {}).get("label") or "")
    side = str((chain or {}).get("side") or "").upper()
    origin_t = (chain or {}).get("sweepBarT") or (chain or {}).get("breakBarT")
    idx = next((i for i, b in enumerate(bars or [])
                if b.get("t") == origin_t), None)

    # R49: Quarterly Theory の位相タグ。掃引が誘導位相(セッション=London の M、
    # または 90分クォーターの M)で起きたスイープは「Q2で誘導→分配」の教義型。
    # 時刻の事実だけを使う(方向は主張しない)。
    if (chain or {}).get("type") == "SWEEP" and origin_t is not None:
        try:
            import quarterly_theory
            q = quarterly_theory.quarters(int(origin_t))
        except Exception:  # noqa: BLE001 — タグは記録専用。判定を巻き込まない
            q = None
        if q and (q.get("sessionPhase") == "M" or q.get("quarterPhase") == "M"):
            tags.append("QT_M_PHASE_SWEEP")
    # R49: true open 奪還(Judas)の観測方向と候補方向の一致。方向は
    # quarterly_theory が価格から導いたもの(時刻からではない)。
    qt_model = ((strategy_matrix or {}).get("models") or {}).get("quarterly") or {}
    if isinstance(qt_model, dict) and qt_model.get("direction") \
            and qt_model.get("direction") == side:
        tags.append("QT_JUDAS_ALIGNED")

    # 研究互換 displacement(MPM/EDGE_LEDGER M6): 起点バーのレンジが
    # 直前20本レンジ中央値の1.5倍以上。既存の noiseFloor 倍率判定とは独立。
    if idx is not None and idx >= 5:
        prior = bars[max(0, idx - DISP_RESEARCH_LOOKBACK):idx]
        try:
            ranges = [float(b["h"]) - float(b["l"]) for b in prior]
            med = statistics.median(ranges) if ranges else None
            span = float(bars[idx]["h"]) - float(bars[idx]["l"])
            if med and med > 0 and span >= DISP_RESEARCH_MULT * med:
                tags.append("DISP_RESEARCH_1_5X")
        except (KeyError, TypeError, ValueError):
            pass

    # レベルの実体無傷(Malaysian SNR のヒゲ/実体分離)。起点以降に
    # レベルを実体クローズで破壊した足が無いこと。
    if idx is not None:
        try:
            price = float((level or {}).get("price"))
            later = bars[idx:]
            destroyed = (any(float(b["c"]) > price + TICK for b in later) if side == "SELL"
                         else any(float(b["c"]) < price - TICK for b in later))
            if not destroyed:
                tags.append("LEVEL_BODY_INTACT")
        except (KeyError, TypeError, ValueError):
            pass

    # R103-1: 直前 20 本の終値がレベルを 3 回以上横断している(揉み合いの中に置いた
    # アンカー)。刈られやすさの観測だけで、採点・等級・decisionId には影響しない。
    if level_chopped(bars, (level or {}).get("price")):
        tags.append("LEVEL_CHOPPED_3")

    if (chain or {}).get("type") == "SWEEP":
        # London range 境界のスイープ(EDGE_LEDGER M9: ブレイク後30分以内に
        # 83% がレンジ内へ回帰する E3 統計。タグのみ・加点しない)。
        if LONDON_LEVEL_RE.search(label):
            tags.append("LONDON_RANGE_SWEEP")
        # Raschke原典 Turtle Soup 条件(EDGE_LEDGER M7)。
        if side in ("BUY", "SELL") and classic_turtle_soup(
                (level or {}).get("price"), side, _daily_bars(bundle)):
            tags.append("CLASSIC_TS")
    return tags


# R48: ターゲット到達しやすさの注釈(EDGE_LEDGER M9 の E3 統計)。
# 「夜間高安は94%の日に破られ、PDH/PDLは57%/43%」という記述統計をラベルに
# 添えるだけで、targetR の採点には使わない。
TARGET_REACH_PATTERNS = [
    (re.compile(r"overnight|globex|\bon\s*(high|low)\b", re.I), "ON_EXTREME_BROKEN_94PCT"),
    (re.compile(r"prev(ious)?\s*day\s*high|\bpdh\b", re.I), "PDH_TOUCHED_57PCT"),
    (re.compile(r"prev(ious)?\s*day\s*low|\bpdl\b", re.I), "PDL_TOUCHED_43PCT"),
    (re.compile(r"weekly|monthly|all\s*time", re.I), "HTF_EXTREME_RARE"),
]


def target_reachability(target_labels):
    out = []
    for label in target_labels or []:
        text = str(label or "")
        out.append(next((tag for pattern, tag in TARGET_REACH_PATTERNS
                         if pattern.search(text)), None))
    return out


def decision_context(bundle, ict, target_labels=None):
    """decision へ添える文脈注釈(R48)。方向シグナルではない。"""
    context = {}
    if target_labels:
        reach = target_reachability(target_labels)
        if any(reach):
            context["targetReachability"] = reach
    session = (ict or {}).get("session") or {}
    etm = session.get("etMinute")
    if etm is not None:
        bucket, hod_cum, lod_cum = "OVERNIGHT", None, None
        for boundary, name, hod, lod in DAY_MATURITY_CHECKPOINTS:
            if etm >= boundary:
                bucket, hod_cum, lod_cum = name, hod, lod
        context["dayMaturity"] = {"bucket": bucket,
                                  "hodCum": hod_cum, "lodCum": lod_cum}
    if session.get("silverBullet"):
        context["silverBullet"] = session["silverBullet"]
    # R49: Quarterly Theory の位相グリッド(時刻の事実のみ。方向は含めない)。
    try:
        import quarterly_theory
        at = (bundle or {}).get("at") or (((bundle or {}).get("snapshot") or {}).get("at"))
        epoch = None
        if at:
            epoch = datetime.fromisoformat(
                str(at).replace("Z", "+00:00")).timestamp()
        q = quarterly_theory.quarters(epoch) if epoch else None
        if q:
            context["quarterly"] = {
                "session": q["session"], "sessionPhase": q["sessionPhase"],
                "quarter": q["quarterIndex"] + 1, "quarterPhase": q["quarterPhase"],
                "micro": q["microPhase"],
            }
    except Exception:  # noqa: BLE001 — 注釈は判定を巻き込まない
        pass
    daily = _daily_bars(bundle)
    snapshot_rows = ((bundle or {}).get("snapshot") or {}).get("bars1d") \
        or (bundle or {}).get("bars1d") or []
    if len(daily) >= 2 and len(snapshot_rows) >= 2:
        prev, today = snapshot_rows[-2], snapshot_rows[-1]
        try:
            gap_pt = float(today["o"]) - float(prev["c"])
            prev_range = max(float(prev["h"]) - float(prev["l"]), TICK)
            ratio = abs(gap_pt) / prev_range
            bucket = ("GE50" if ratio >= 0.50 else "GE25" if ratio >= 0.25
                      else "GE10" if ratio >= 0.10 else "LT10")
            context["gap"] = {"points": round(gap_pt, 2), "ratio": round(ratio, 3),
                              "bucket": bucket}
        except (KeyError, TypeError, ValueError):
            pass
    return context


#: R89: 契約(execution_contract.json の modelGate)で武装から外すモデル/型。既定は空 = 何も外さない。
#: ALL = そのモデルの候補すべて / RESTING_LIMIT = MSS 確認後にレベルへ先回りする指値(restingLimit)だけ。
MODEL_GATE_VARIANTS = ("ALL", "RESTING_LIMIT")
MODEL_GATE_BLOCKERS = {"ALL": "MODEL_DISABLED", "RESTING_LIMIT": "RESTING_LIMIT_DISABLED"}
CONTRACT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "execution_contract.json")


def model_gate_rules(contract=None):
    """``modelGate.disabled`` を ``{"rules": [(model, variant)], "invalid": [...]}`` にする。

    節が無い・読めない・形が壊れているときは何も外さない(R89 以前と同じ判定)。
    不正な行はその行だけ捨てて ``invalid`` に残す(本番契約に不正な行が無いことはテストが見る)。
    """
    if contract is None:
        try:
            with open(CONTRACT_PATH, encoding="utf-8") as fh:
                contract = json.load(fh)
        except (OSError, ValueError):
            return {"rules": [], "invalid": ["CONTRACT_UNREADABLE"]}
    raw = contract.get("modelGate") if isinstance(contract, dict) else None
    if raw is None:
        return {"rules": [], "invalid": []}
    rows = raw.get("disabled") if isinstance(raw, dict) else None
    if not isinstance(rows, list):
        return {"rules": [], "invalid": ["MODEL_GATE_MALFORMED"]}
    rules, invalid = [], []
    for row in rows:
        model = row.get("model") if isinstance(row, dict) else None
        variant = str(row.get("variant") or "ALL").strip().upper() if isinstance(row, dict) else None
        if model not in MODEL_ORDER or variant not in MODEL_GATE_VARIANTS:
            invalid.append(row)
            continue
        if (model, variant) not in rules:
            rules.append((model, variant))
    return {"rules": rules, "invalid": invalid}


def model_gate_blocker(candidate, rules):
    """外す対象ならブロッカー名、対象外なら None。"""
    for model, variant in rules or ():
        if candidate.get("model") != model:
            continue
        if variant == "ALL" or (variant == "RESTING_LIMIT" and candidate.get("restingLimit")):
            return MODEL_GATE_BLOCKERS[variant]
    return None


#: R121: ICT STDV(execution_contract.json の ictStdv)。段は 3 つとも独立。
ICT_STDV_MODES = ("OFF", "SHADOW", "LIVE")


def ict_stdv_policy(contract=None):
    """``ictStdv`` を ``{"mode", "targets", "participation", "params", "invalid"}`` にする。

    節が無い・読めない・形が壊れているときは全部 OFF(= R121 以前と同じ判定)。壊れた
    段はその段だけ OFF にして ``invalid`` に残す(本番契約に不正な段が無いことは試験が見る)。
    """
    off = {"mode": "OFF", "targets": {"mode": "OFF"}, "participation": {"mode": "OFF"},
           "params": {}, "invalid": []}
    if contract is None:
        try:
            with open(CONTRACT_PATH, encoding="utf-8") as fh:
                contract = json.load(fh)
        except (OSError, ValueError):
            return {**off, "invalid": ["CONTRACT_UNREADABLE"]}
    raw = contract.get("ictStdv") if isinstance(contract, dict) else None
    if raw is None:
        return off
    if not isinstance(raw, dict):
        return {**off, "invalid": ["ICT_STDV_MALFORMED"]}
    out = {"mode": "OFF", "targets": {"mode": "OFF"}, "participation": {"mode": "OFF"},
           "params": {}, "invalid": []}
    mode = str(raw.get("mode") or "OFF").strip().upper()
    if mode not in ICT_STDV_MODES:
        out["invalid"].append("ICT_STDV_MODE_MALFORMED")
    else:
        out["mode"] = mode
    targets = raw.get("targets")
    if isinstance(targets, dict):
        tmode = str(targets.get("mode") or "OFF").strip().upper()
        ratios = targets.get("ratios")
        try:
            import ict_stdv
            allowed = set(ict_stdv.RATIOS)
        except Exception:  # noqa: BLE001 - モジュールが無ければこの段は OFF
            allowed = set()
        ratios = [float(r) for r in ratios if _finite_num(r) is not None and float(r) in allowed] \
            if isinstance(ratios, list) else None
        if tmode not in ICT_STDV_MODES or (tmode != "OFF" and not ratios):
            out["invalid"].append("ICT_STDV_TARGETS_MALFORMED")
        else:
            out["targets"] = {"mode": tmode, "ratios": ratios or [],
                              "runnerEligible": targets.get("runnerEligible") is True}
    elif targets is not None:
        out["invalid"].append("ICT_STDV_TARGETS_MALFORMED")
    participation = raw.get("participation")
    if isinstance(participation, dict):
        pmode = str(participation.get("mode") or "OFF").strip().upper()
        if pmode not in ICT_STDV_MODES:
            out["invalid"].append("ICT_STDV_PARTICIPATION_MALFORMED")
        else:
            out["participation"] = {"mode": pmode}
    elif participation is not None:
        out["invalid"].append("ICT_STDV_PARTICIPATION_MALFORMED")
    params = raw.get("params")
    out["params"] = dict(params) if isinstance(params, dict) else {}
    return out


def _finite_num(value):
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out and abs(out) != float("inf") else None


def _stdv_for_chain(model, level, chain, bars, side, entry, stop, targets, bundle, nf, policy):
    """R121: 候補 1 件の STDV 監査。``(audit, extra_targets)``。

    ``extra_targets`` は ``targets.mode == "LIVE"`` のときだけ中身が入る。OFF / SHADOW は
    必ず空 —— 注文意図・Entry/SL/TP・decisionId を変えないため(受け入れ試験)。
    """
    if not isinstance(policy, dict) or policy.get("mode") == "OFF":
        return None, []
    try:
        import ict_stdv
    except Exception as exc:  # noqa: BLE001 - 読めなければ「STDV なし」に倒す
        return {"mode": policy.get("mode"), "reason": f"MODULE_UNAVAILABLE:{type(exc).__name__}"}, []
    snapshot = bundle.get("snapshot") if isinstance(bundle, dict) and isinstance(
        bundle.get("snapshot"), dict) else {}
    diag = ict_stdv.anchor_diagnosis(
        chain, bars, noise_floor=nf, params=policy.get("params"),
        symbol=(bundle or {}).get("sourceSymbol") or snapshot.get("sourceSymbol"),
        session_id=(bundle or {}).get("sessionId") or snapshot.get("sessionId"),
        level=level)
    anchor = diag.get("anchor")
    audit = {"mode": policy.get("mode"), "targetsMode": (policy.get("targets") or {}).get("mode"),
             "participationMode": (policy.get("participation") or {}).get("mode"),
             "reason": diag.get("reason"), "anchor": None, "read": None,
             "targetsOffered": [], "targetsApplied": False, "runnerRejected": False}
    if not isinstance(anchor, dict):
        return audit, []
    # 到達判定は **knownAt 以後の確定足**と、鮮度確認済みの現値だけで行う(R119 と同じ関数)。
    gate_last, _gate_reason = gate_price(bundle)
    anchor = ict_stdv.mark_reached(anchor, bars=bars, price=gate_last,
                                   price_known=gate_last is not None)
    audit["anchor"] = anchor
    audit["read"] = ict_stdv.read_context(
        anchor, side=side, entry=entry, stop=stop, price=gate_last,
        price_known=gate_last is not None, targets=targets)
    if diag.get("reason"):
        return audit, []
    trule = policy.get("targets") or {}
    offered = ict_stdv.target_candidates(anchor, entry, stop, side,
                                         target_ratios=trule.get("ratios") or ())
    audit["targetsOffered"] = [{"price": price, "label": label} for price, label in offered]
    if str(trule.get("mode") or "OFF").upper() != "LIVE":
        return audit, []
    return audit, list(offered)


#: R119: 新規武装だけに掛かる 2 つの門(execution_contract.json の limitGate)。
#: gapCap = 指値が現値から遠すぎる / targetPassed = 現値が既に TP1 を通過している。
LIMIT_GATE_BLOCKERS = {"gapCap": "LIMIT_GAP_EXCEEDED", "targetPassed": "TARGET_ALREADY_PASSED"}
LIMIT_GATE_MODES = ("OFF", "SHADOW", "LIVE")


def limit_gate_policy(contract=None):
    """``limitGate`` を ``{"gapCap": {...}, "targetPassed": {...}, "invalid": [...]}`` にする。

    節が無い・読めない・形が壊れているときは全部 OFF(= R119 以前と同じ判定)。壊れた節は
    その節だけ OFF にして ``invalid`` に残す(本番契約に不正な節が無いことはテストが見る)。
    """
    if contract is None:
        try:
            with open(CONTRACT_PATH, encoding="utf-8") as fh:
                contract = json.load(fh)
        except (OSError, ValueError):
            return {"gapCap": {"mode": "OFF"}, "targetPassed": {"mode": "OFF"},
                    "invalid": ["CONTRACT_UNREADABLE"]}
    raw = contract.get("limitGate") if isinstance(contract, dict) else None
    out = {"gapCap": {"mode": "OFF"}, "targetPassed": {"mode": "OFF"}, "invalid": []}
    if raw is None:
        return out
    if not isinstance(raw, dict):
        out["invalid"].append("LIMIT_GATE_MALFORMED")
        return out
    gap = raw.get("gapCap")
    if isinstance(gap, dict):
        mode = str(gap.get("mode") or "OFF").strip().upper()
        if mode not in LIMIT_GATE_MODES:
            out["invalid"].append("LIMIT_GATE_GAP_CAP_MALFORMED")
        elif mode == "OFF":
            # OFF は 1 語で戻せるようにする。maxGapR / models は見ない。
            out["gapCap"] = {"mode": "OFF"}
        else:
            try:
                max_gap = float(gap.get("maxGapR", LIMIT_MAX_GAP_R))
            except (TypeError, ValueError):
                max_gap = None
            models = gap.get("models")
            models = [m for m in models if m in MODEL_ORDER] if isinstance(models, list) else None
            if max_gap is None or max_gap <= 0 or not models:
                out["invalid"].append("LIMIT_GATE_GAP_CAP_MALFORMED")
            else:
                out["gapCap"] = {"mode": mode, "maxGapR": max_gap, "models": models}
    elif gap is not None:
        out["invalid"].append("LIMIT_GATE_GAP_CAP_MALFORMED")
    passed = raw.get("targetPassed")
    if isinstance(passed, dict):
        mode = str(passed.get("mode") or "OFF").strip().upper()
        if mode not in LIMIT_GATE_MODES:
            out["invalid"].append("LIMIT_GATE_TARGET_PASSED_MALFORMED")
        else:
            out["targetPassed"] = {"mode": mode}
    elif passed is not None:
        out["invalid"].append("LIMIT_GATE_TARGET_PASSED_MALFORMED")
    return out


def gate_price(bundle, max_age=None):
    """R119 の門が使う**鮮度確認済みの**現値。``(price, reason)``。

    ``price`` が取れないときは ``(None, 理由)`` で、呼び出し側は門を掛けない(相場データの
    古さは pipeline が先に BLOCK する。ここで新しい停止経路を作らない)。``bars[-1]["c"]``
    への落ち込みは**採らない** —— 確定足の終値は「今の値」ではないので、TP1 通過の判定に
    使うと過去の足で新規を止めたり通したりする。
    """
    if not isinstance(bundle, dict):
        return None, "NO_BUNDLE"
    price = _num(bundle, "price")
    if price is None or price <= 0:
        return None, "NO_PRICE"
    price_at = _epoch(bundle.get("priceAt"))
    if price_at is None:
        return None, "NO_PRICE_AT"
    # 基準は bundle 自身の `at`(その周期の「今」)。無い周期だけ実時刻へ落ちる。
    reference = _epoch(bundle.get("at"))
    if reference is None:
        reference = datetime.now(timezone.utc).timestamp()
    if max_age is None:
        try:
            import execution_contract
            max_age = float(execution_contract.CONTRACT["scenario"]["maxAgeSec"])
        except Exception:  # noqa: BLE001 - 契約が読めないときは §6.2 の既定窓で判定する
            max_age = 600.0
    age = reference - price_at
    if abs(age) > float(max_age):
        return None, f"PRICE_STALE_{age:.0f}S"
    return price, None


def limit_entry_side(candidate, price):
    """発注時に **LIMIT になるか**(建値が現値の先にあるか)。

    ``autotrade_engine._entry_order_type`` と同じ式にする。あちらは建値が現値を通過して
    いれば MARKET を出すので、指値専用の向き判定をそのまま全候補へ当てると、成行になる
    はずの候補を「指値として成立しない」と誤って落とす。
    """
    try:
        entry = float(candidate["entry"])
        last = float(price)
    except (TypeError, ValueError, KeyError):
        return False
    side = str(candidate.get("side") or "").upper()
    if side == "BUY":
        return entry < last
    if side == "SELL":
        return entry > last
    return False


def limit_gate_audit(candidate, price, price_reason=None, policy=None):
    """R119 の 2 門の観測。``{"blocker", "gapR", "tp1R", "orderType", ...}`` を必ず返す。

    ``blocker`` は LIVE で当たった門のブロッカー名だけ。SHADOW は観測を残して None を返す。
    採点・等級・建値・SL・targets には一切触れないので ``decisionId`` は変わらない。
    """
    policy = limit_gate_policy() if policy is None else policy
    gap_rule = (policy or {}).get("gapCap") or {"mode": "OFF"}
    passed_rule = (policy or {}).get("targetPassed") or {"mode": "OFF"}
    audit = {"gapCapMode": str(gap_rule.get("mode") or "OFF"),
             "targetPassedMode": str(passed_rule.get("mode") or "OFF"),
             "blocker": None, "gapR": None, "tp1R": None, "orderType": None, "reason": None}
    if audit["gapCapMode"] == "OFF" and audit["targetPassedMode"] == "OFF":
        audit["reason"] = "GATE_OFF"
        return audit
    if price is None:
        # 鮮度が確認できない周期は掛けない。理由を残して「評価した上で効かなかった」と
        # 「入力が無くて判定していない」を区別できるようにする(CLAUDE.md §2 と同じ規律)。
        audit["reason"] = price_reason or "PRICE_UNVERIFIED"
        return audit
    try:
        entry = float(candidate["entry"])
        stop = float(candidate["stop"])
        tp1 = float((candidate.get("targets") or [None])[0])
    except (TypeError, ValueError, KeyError, IndexError):
        audit["reason"] = "GEOMETRY_INCOMPLETE"
        return audit
    risk = abs(entry - stop)
    if risk <= 0:
        audit["reason"] = "GEOMETRY_INCOMPLETE"
        return audit
    side = str(candidate.get("side") or "").upper()
    if side not in {"BUY", "SELL"}:
        audit["reason"] = "SIDE_UNKNOWN"
        return audit
    is_limit = limit_entry_side(candidate, price)
    audit["orderType"] = "LIMIT" if is_limit else "MARKET"
    audit["gapR"] = round(abs(price - entry) / risk, 3)
    sign = 1.0 if side == "BUY" else -1.0
    audit["tp1R"] = round(sign * (tp1 - price) / risk, 3)
    blockers = []
    # 門 2(targetPassed): 現値が既に TP1 に到達/通過。指値でも成行でも払えない幾何なので
    # 注文種別で分けない。向きは区別する(BUY は上、SELL は下が「通過」)。
    if audit["targetPassedMode"] != "OFF" and audit["tp1R"] <= 0:
        audit.setdefault("observed", []).append(LIMIT_GATE_BLOCKERS["targetPassed"])
        if audit["targetPassedMode"] == "LIVE":
            blockers.append(LIMIT_GATE_BLOCKERS["targetPassed"])
    # 門 1(gapCap): **LIMIT になる候補だけ**。成行になる候補は待ちが発生しないので無関係。
    if audit["gapCapMode"] != "OFF" and is_limit:
        models = gap_rule.get("models") or ()
        if candidate.get("model") in models and audit["gapR"] > float(gap_rule["maxGapR"]):
            audit["maxGapR"] = float(gap_rule["maxGapR"])
            audit.setdefault("observed", []).append(LIMIT_GATE_BLOCKERS["gapCap"])
            if audit["gapCapMode"] == "LIVE":
                blockers.append(LIMIT_GATE_BLOCKERS["gapCap"])
    audit["blocker"] = blockers[0] if blockers else None
    audit["blockers"] = blockers
    return audit


def build_candidates(bundle, result, bars, levels, ict, nf, strategy_matrix=None, model_gate=None,
                     limit_gate=None):
    # 指値の向き(建値が現値の先にあるか)を判定するための現値。
    last_price = (bundle or {}).get("price")
    if last_price is None and bars:
        last_price = bars[-1].get("c")
    # R119 の 2 門は**鮮度確認済みの**現値だけで判定する(確定足の終値へは落ちない)。
    gate_last, gate_reason = gate_price(bundle)
    route = route_regime(bundle, result)
    candidates = []
    # ICTBACK の1m候補は、既存の3m MSNR連鎖を置換しない。欠損時に3mを
    # 1mと呼び替えることはしないため、ここでは明示的な1m証跡だけを読む。
    sb = result.get("silverBullet") if isinstance(result, dict) else None
    if (silver_bullet_mode() != "OFF" and isinstance(sb, dict)
            and isinstance(sb.get("candidate"), dict)):
        candidates.append(sb["candidate"])
    vp = candidate_vp80(result, bars, levels, ict, route, bundle, nf, strategy_matrix)
    if vp:
        candidates.append(vp)
    for level in levels:
        for chain in level.get("chains", []):
            state = chain.get("state")
            resting = False
            if state not in COMPLETE_STATES:
                # 反転を先回りする指値。掃引が済んでいるので SL は実測の
                # 掃引極値、建値はまだ来ていないリテスト水準になる。
                if state not in LIMIT_ENTRY_STATES or chain.get("type") != "SWEEP":
                    continue
                resting = True
            model = "TURTLE_SOUP_REVERSAL" if chain.get("type") == "SWEEP" else "BREAKER_CONTINUATION"
            candidate = _candidate_for_chain(model, level, chain, bars, levels, ict, route, bundle, nf,
                                             strategy_matrix)
            if candidate and resting and not _resting_limit_ok(candidate, last_price):
                candidate = None
            if candidate:
                if resting:
                    candidate["entryOrderType"] = "LIMIT"
                    candidate["restingLimit"] = True
                    candidate["evidence"].append("RESTING_LIMIT")
                if chain.get("sweepExtremeBeyondReclaim"):
                    # R88: SL を破壊足の極値まで下げた印。記録専用(採点・確認要素にしない)。
                    candidate["evidence"].append("SWEEP_STOP_FULL_SPAN")
                if chain.get("flipExtremeBeyondBreak"):
                    # R90: SL の錨をブレイク足から上昇/下降の起点まで下げた印。記録専用。
                    candidate["evidence"].append("FLIP_STOP_ORIGIN")
                # R48: 記録専用の特徴量タグ。score/grade には触れない。
                candidate["evidence"].extend(
                    feature_tags(chain, level, bars, bundle, strategy_matrix))
                candidates.append(candidate)
            rng = (ict or {}).get("range") or {}
            fvg = (ict or {}).get("fvg") or {}
            side = chain.get("side")
            fvg_side = "BULL" if side == "BUY" else "BEAR"
            eligible_fvg = next((row for row in (fvg.get(fvg_side) or [])
                                 if row.get("eligible") and row.get("preArrivalStructure") == "INTACT"), None)
            # OTEは明示的なHTF/セッション・アンカーがFRESHで、かつ到達前の
            # displacement FVG が残るときだけ候補化する。ローリング3M高安を
            # 見てから都合の良いOTEを作る経路はここには存在しない。
            # R27: レンジ幅の下限。OTE は risk = 0.085*span + buffer なので、
            # span が狭いと SL がノイズフロアを下回り、必ず刈られる建玉になる。
            # しかも R が膨らんで採点は最高になるため、退化を報酬してしまう。
            # spanPt は _range_from_prices の派生値。手で組んだレンジには無い
            # ことがあるので、高安から直接求められる方を優先する。
            span_pt = 0.0
            try:
                span_pt = abs(float(rng.get("high")) - float(rng.get("low")))
            except (TypeError, ValueError):
                try:
                    span_pt = float(rng.get("spanPt") or 0)
                except (TypeError, ValueError):
                    span_pt = 0.0
            span_ok = span_pt >= max(MIN_OTE_SPAN_PT, MIN_OTE_SPAN_NF_MULT * nf)
            if (rng.get("valid") and (rng.get("favors") or {}).get(side)
                    and eligible_fvg and span_ok):
                ote = rng.get("oteBuy" if side == "BUY" else "oteSell") or []
                if len(ote) == 2:
                    entry = _tick_price(sum(ote) / 2)
                    stop = _tick_price(min(ote) - model_stop_buffer(nf) if side == "BUY"
                                       else max(ote) + model_stop_buffer(nf))
                    # OTE の建値/SL をそのまま採点対象にする。連鎖の幾何で
                    # 採点してから上書きすると、R:R 加点も SL 上限判定も
                    # 実際に出す注文とは別物になる。
                    if model_targets(entry, stop, side, levels, ict):
                        ote_candidate = _candidate_for_chain("OTE_FVG_PULLBACK", level, chain,
                                                             bars, levels, ict, route, bundle, nf,
                                                             strategy_matrix,
                                                             entry=entry, stop=stop)
                        if ote_candidate:
                            ote_candidate.update({"fvg": eligible_fvg, "rangeAnchor": rng})
                            ote_candidate["evidence"].append("OTE_FVG_CONFLUENCE")
                            # R48: 同じ連鎖の特徴量タグは OTE 候補にも記録する。
                            ote_candidate["evidence"].extend(
                                feature_tags(chain, level, bars, bundle, strategy_matrix))
                            ote_candidate["score"] += 2
                            _finalize_candidate(ote_candidate, ict)
                            candidates.append(ote_candidate)
    # R89: 契約で外したモデル/型は重複除去の**前**に分ける。外した候補が同じ side の
    # 生きている候補(例: 指値型に負けたリテスト保持型)を押し出さないため。
    rules = model_gate_rules()["rules"] if model_gate is None else model_gate
    # R119: 指値の遠さと「現値が既に TP1 を通過」も同じ層で分ける。**新規武装だけ**に掛かり、
    # 凍結プランの管理(MODIFY / FLATTEN / 追撃)はこの関数を通らないので影響しない。
    gate_policy = limit_gate_policy() if limit_gate is None else limit_gate
    # side/modelごとの重複は最高scoreだけを残す。
    unique, gated = {}, {}
    for candidate in candidates:
        model_blocker = model_gate_blocker(candidate, rules)
        if model_blocker:
            candidate["modelGate"] = model_blocker
        gate = limit_gate_audit(candidate, gate_last, gate_reason, gate_policy)
        candidate["limitGate"] = gate
        for tag in (gate.get("observed") or ()):
            # SHADOW も LIVE も観測は証跡へ(記録専用。採点・等級には触れない)。
            candidate["evidence"] = list(dict.fromkeys([*(candidate.get("evidence") or []), tag]))
        blockers = [x for x in (model_blocker, *(gate.get("blockers") or ())) if x]
        if blockers:
            # 記録には残す(WATCH + ブロッカー)。primary の選択肢には入れない。
            candidate["hardBlockers"] = list(dict.fromkeys([*(candidate.get("hardBlockers") or []), *blockers]))
            candidate["allowed"] = False
            candidate["state"] = "WATCH"
        bucket = gated if blockers else unique
        key = (candidate.get("model"), candidate.get("side"))
        current = bucket.get(key)
        candidate_r = (candidate.get("targetR") or [0])[0]
        current_r = (current.get("targetR") or [0])[0] if current else 0
        if current is None or (candidate.get("score", -999), candidate_r) > \
                (current.get("score", -999), current_r):
            bucket[key] = candidate
    return list(unique.values()) + list(gated.values())


def setup_identity(candidate):
    """構造の同一性キー。同じ起点・同じ SL なら同じセットアップ。

    建値は現在値に追従して動く(VP80 は直近終値)ので含めない。含めると
    同じセットアップが足ごとに別物になる。
    """
    chain = candidate.get("chain") or {}
    origin = chain.get("sweepBarT") if chain.get("type") == "SWEEP" else chain.get("breakBarT")
    level = candidate.get("level")
    stop = candidate.get("stop")
    parts = [candidate.get("model"), candidate.get("side"), level, origin, stop]
    # R121: STDV が実際に TP を差し替えた候補だけ、目標も同一性へ入れる。OFF / SHADOW と
    # 差し替えの無い周期では `stdvIdentity` が無いので、ID は R121 以前と**バイト一致**する。
    if candidate.get("stdvIdentity"):
        parts.append(candidate["stdvIdentity"])
    material = "|".join(str(x) for x in parts)
    return hashlib.sha1(material.encode()).hexdigest()[:12]


def select_primary(candidates, bundle=None):
    """A/A+を最優先し、同点でも必ず1件へ決める。"""
    candidates = list(candidates or [])
    rank = {"A+": 3, "A": 2, "B": 1}
    candidates.sort(key=lambda c: (rank.get(c.get("grade"), 0), c.get("score", -999),
                                   c.get("targetR", [0])[0] if c.get("targetR") else 0,
                                   MODEL_RANK.get(c.get("model"), 0)), reverse=True)
    if not candidates:
        return {"phase": str((bundle or {}).get("phase") or os.environ.get("NQX_PHASE", "EVAL_STRIKE")).upper(),
                "model": "FLAT", "side": "FLAT", "state": "WATCH", "grade": None,
                "score": 0, "entry": None, "stop": None, "targets": [], "targetR": [],
                "hardBlockers": ["NO_A_OR_A_PLUS_MODEL"], "penalties": [], "evidence": []}
    chosen = candidates[0]
    phase = str((bundle or {}).get("phase") or os.environ.get("NQX_PHASE", "EVAL_STRIKE")).upper()
    if phase not in EVAL_PHASES:
        phase = "EVAL_STRIKE"
    # セットアップの同一性は「モデル・方向・構造の起点・SL」で決まる。時刻を
    # 混ぜると同じセットアップが 3 分ごとに別 ID になり、下流の重複防止が
    # 効かない(実サイクルで同一構造の再武装が最長 14 連続していた)。
    setup_id = setup_identity(chosen)
    decision_id = hashlib.sha1((setup_id + "|" + chosen["model"] + "|" + chosen["side"]).encode()).hexdigest()[:16]
    return {
        "decisionId": decision_id, "setupId": setup_id, "phase": phase, "model": chosen["model"],
        "side": chosen["side"], "state": chosen["state"], "grade": chosen["grade"],
        "score": chosen["score"], "entryMode": (
            "FVG_50_PERCENT_LIMIT" if chosen["model"] == SILVER_BULLET_MODEL
            else "AGGRESSIVE_ACCEPT_CLOSE" if chosen["model"] in {"VP80_REVERSION", "TURTLE_SOUP_REVERSAL"}
            else "STRUCTURE_RETEST"),
        "entry": chosen["entry"], "stop": chosen["stop"], "targets": chosen["targets"],
        "targetR": chosen["targetR"], "targetLabels": chosen.get("targetLabels") or [],
        "hardBlockers": chosen["hardBlockers"],
        "penalties": chosen["penalties"], "evidence": chosen["evidence"],
        "confirmations": chosen.get("confirmations") or [],
        "rangeAnchor": chosen.get("rangeAnchor") or {}, "fvg": chosen.get("fvg"),
        "cvdHealth": chosen.get("cvdHealth") or {},
        "strategyModels": chosen.get("strategyModels") or [],
        "strategyAlignment": chosen.get("strategyAlignment", 0),
        "htfContext": chosen.get("htfContext") or {},
        "silverBullet": chosen.get("silverBullet") or {},
        # R90: VWAP 逃がしの監査(評価して動かさなかった / 入力が無かった / 置き換えた)。
        "vwapStop": _compact_vwap_audit(chosen.get("vwapStop")),
        # R103-1: OFF のときはキーごと足さない(出力を R103 以前と同一に保つ)。
        **({"poolStop": _compact_pool_audit(chosen["poolStop"])} if chosen.get("poolStop") else {}),
        # R103-3: 掃引ゲートの監査。OFF ではキーごと無い。
        **({"sweepGate": _compact_sweep_audit(chosen["sweepGate"])} if chosen.get("sweepGate") else {}),
        # R121: 先回り指値型(restingLimit)と STDV 監査を decision まで運ぶ。前者は
        # 型別の集計、後者は「最終判断に効いたか」の追跡に要る。無いときはキーを
        # 足さない(出力を R121 以前と同一に保つ)。
        **({"restingLimit": True} if chosen.get("restingLimit") else {}),
        **({"ictStdv": _compact_stdv_audit(chosen["ictStdv"])} if chosen.get("ictStdv") else {}),
        "modelRank": [c["model"] + ":" + str(c.get("grade")) for c in candidates[:4]],
    }


def strategy_evaluation(bundle, result, bars, levels, nf, ict):
    """R11-Dの候補・decisionを一括生成する。外部状態を変更しない。"""
    matrix = result.get("strategyMatrix")
    if not isinstance(matrix, dict):
        matrix = strategy_models.build_strategy_matrix(bundle, bars, levels, ict or {})
        result["strategyMatrix"] = matrix
    result["silverBullet"] = silver_bullet_evaluation(bundle, levels, nf)
    candidates = build_candidates(bundle, result, bars, levels, ict or {}, nf, matrix)
    # R89: 契約で外した候補は primary に選ばない(別モデルが primary になれる)。
    decision = select_primary([c for c in candidates if not c.get("modelGate")], bundle)
    decision["smt"] = (ict or {}).get("smt") or {}
    decision["ictSession"] = (ict or {}).get("session") or {}
    decision["ictCoverage"] = ict_coverage(bundle, ict, matrix)
    # R48: 文脈注釈(日中成熟度・Silver Bullet窓・ギャップbucket・目標到達率)。
    # 監査バンドルとスコアカード用の記録で、判定・採点には使わない。
    decision["context"] = decision_context(bundle, ict or {},
                                           decision.get("targetLabels"))
    decision["strategyModels"] = list(matrix.get("activeModels") or [])[:8]
    decision["strategyBias"] = (matrix.get("alignment") or {}).get("primary")
    return candidates, decision


def decision_to_scenario(decision, bundle, qty=2):
    """evaluation.decisionを既存の単一target scenarioへ写像する。発注はしない。"""
    if not isinstance(decision, dict) or decision.get("model") == "FLAT":
        return None
    side = decision.get("side")
    targets = decision.get("targets") or []
    entry, stop = decision.get("entry"), decision.get("stop")
    if side not in {"BUY", "SELL"} or not targets or entry is None or stop is None:
        return None
    target = targets[0]
    state = decision.get("state", "WATCH")
    execution_blockers = []
    # R12の自律執行は常に2枚分割。単一ターゲットの分析は残すが、TP1と
    # runner最終TPを凍結できるまで武装表示にしない。
    if int(qty) == 2 and (len(targets) < 2 or targets[0] == targets[1]):
        state = "WATCH"
        execution_blockers.append("RUNNER_TARGET_REQUIRED")
    legs = ([{"id": "TP1", "qty": 1, "target": targets[0]},
             {"id": "RUNNER", "qty": 1, "target": targets[1]}]
            if int(qty) == 2 and len(targets) >= 2 and targets[0] != targets[1]
            else [])
    return {
        "scenarioId": decision.get("decisionId"),
        "state": state,
        "side": side, "qty": int(qty), "entry": entry, "stop": stop,
        "target": target, "grade": decision.get("grade"),
        "title": decision.get("model"),
        "reason": " / ".join(decision.get("evidence") or []) or "構造モデル完成",
        "trigger": decision.get("entryMode") or "STRUCTURE",
        "invalidation": "structure stop",
        "model": decision.get("model"), "phase": decision.get("phase"),
        "decisionId": decision.get("decisionId"), "targets": list(targets),
        "targetR": list(decision.get("targetR") or []),
        "legs": legs,
        "planVersion": "R19-ICT-SPLIT-1",
        "targetLabels": list(decision.get("targetLabels") or []),
        "rangeAnchor": decision.get("rangeAnchor") or {},
        "fvg": decision.get("fvg"), "cvdHealth": decision.get("cvdHealth") or {},
        "smt": decision.get("smt") or {}, "ictSession": decision.get("ictSession") or {},
        "strategyModels": list(decision.get("strategyModels") or []),
        "strategyAlignment": decision.get("strategyAlignment"),
        "strategyBias": decision.get("strategyBias"),
        "htfContext": decision.get("htfContext") or {},
        "silverBullet": decision.get("silverBullet") or {},
        # Immutable R14 decision provenance.  These fields are observational
        # metadata only, but make later OOS analysis reject mixed setups.
        "setupVersion": decision.get("setupVersion"),
        "catalogVersion": decision.get("catalogVersion"),
        "detectorVersion": decision.get("detectorVersion"),
        "executionContractVersion": decision.get("executionContractVersion"),
        "strategyEvidence": decision.get("strategyEvidence") or {},
        "eligibleVotes": decision.get("eligibleVotes") or {},
        "evidenceHash": decision.get("evidenceHash"),
        "executionBlockers": execution_blockers,
    }


def _chain_start(chain):
    return chain["sweepBarT"] if chain["type"] == "SWEEP" else chain["breakBarT"]


def summarize(result, prm):
    """§3.7 の1行。最も進んだ連鎖と回転判定だけを載せる。"""
    rot = result["rotation"]
    if rot.get("verdict") is None:
        tail = "回転 —"
    else:
        tail = f"回転 {rot['negations']}/{rot['signals']} " \
               f"{'OK' if rot['verdict'] == 'OK' else '回転'}"
    if result.get("insufficient"):
        return f"MSNR: 判定不能({result['insufficient']}) · {tail}" \
            + _advisory_tail(result)

    best = None
    for level in result["levels"]:
        for chain in level["chains"]:
            key = (CHAIN_RANK[chain["state"]],
                   1 if level["promotion"][chain["side"]]["allowed"] else 0,
                   _chain_start(chain))
            if best is None or key > best[0]:
                best = (key, level, chain)
    if best is None:
        return f"MSNR: 連鎖なし · {tail}" + _advisory_tail(result)

    _, level, chain = best
    head = f"{level['label']}{level['price']:.0f} {chain['side']}連鎖 " \
           f"{CHAIN_JP[chain['state']]}"
    if chain["state"] in COMPLETE_STATES:
        since = chain.get("barsSinceRetest") if chain["type"] == "SWEEP" \
            else chain.get("barsSinceHold")
        head += f"(鮮度残{max(prm['complete_ttl'] - (since or 0), 0)}本)"
    elif chain["state"] != "EXPIRED":
        head += f"(残{max(chain['barsLeft'], 0)}本)"
    line = f"MSNR: {head} · {tail}"
    promo = level["promotion"][chain["side"]]
    if promo["allowed"]:
        line += f" ✔昇格可[{promo['grade'] or 'A'}]"
    return line + _advisory_tail(result)


def _advisory_tail(result):
    """R4: ADVISORY 経路(VWAP / VP)を summary の末尾に足す。武装可否とは無関係。"""
    parts = []
    vwp = result.get("vwapPath") or {}
    if vwp.get("state"):
        kind = "RECLAIM" if vwp["side"] == "BUY" else "REJECT"
        drift = "" if vwp.get("drift") is None else f"(drift {vwp['drift']:.1f}pt)"
        parts.append(f"VWAP: {kind} {VWAP_PATH_JP[vwp['state']]}{drift}")
    elif "VWAP_DRIFT" in (vwp.get("blockers") or []):
        drift = vwp.get("drift")
        parts.append("VWAP: 無効(drift " +
                     (f"{drift:.1f}pt)" if drift is not None else "測定不能)"))
    vpp = result.get("vpPath") or {}
    if vpp.get("state"):
        parts.append(f"VP: {VP_PATH_JP[vpp['state']]} → {vpp['target']:,.0f} 目標域")
    ict = result.get("ict") or {}
    if ict.get("summary"):
        parts.append("ICT: " + ict["summary"])
    return ("" if not parts else " · " + " · ".join(parts))


def evaluate(bundle, prm=None):
    """bundle 1つを判定して出力辞書を返す(純粋関数・副作用なし)。"""
    prm = prm or params()
    # R45: VWAP のアンカーは**セッション開始(ET 18:00)**。走査は末尾
    # WINDOW_BARS 本。両者は同じ末尾で終わるのでスライスの長さを揃えれば
    # インデックスは一致する。
    all_bars = closed_bars(bundle)
    bars = all_bars[-WINDOW_BARS:]
    snapshot = bundle.get("snapshot") if isinstance(bundle.get("snapshot"), dict) else {}
    # アンカーは取得側(tv_snapshot)が明示した値を最優先。無ければ最終確定足の
    # 時刻から同じ規則で導く(手書き bundle・旧 bundle も同じアンカーになる)。
    vwap_anchor = _num(bundle, "vwapAnchorT")
    if vwap_anchor is None:
        vwap_anchor = _num(snapshot, "vwapAnchorT")
    if vwap_anchor is None and all_bars:
        vwap_anchor = session_anchor_epoch(all_bars[-1]["t"])
    # publish 経路で足が切られていても、取得側の累積から手前ぶんを復元する。
    vwap_seed = session_vwap_seed(all_bars, vwap_anchor, snapshot)
    vwaps_all = rolling_vwap(all_bars, vwap_anchor, vwap_seed)
    vwaps = vwaps_all[-len(bars):] if (vwaps_all and bars) else None
    levels = dedupe(snapshot.get("levels"))
    nf = noise_floor(bars)

    result = {
        "at": bundle.get("at"),
        "closedBars": len(bars),
        # R45: アンカー(セッション開始)以降で実際に累積した確定足の本数。
        # 旧 R5 は「供給された全量」だった。
        "vwapAnchorBars": sum(1 for v in (vwaps_all or []) if v is not None),
        "vwapAnchorT": int(vwap_anchor) if vwap_anchor is not None else None,
        # 系列が「窓の手前ぶん」を復元して作られたか。False なら手持ちの足だけ。
        "vwapSeeded": vwap_seed is not None,
        "vwapSessionBars": (lambda n: int(n) if n is not None else None)(
            _num(snapshot, "vwapSessionBars")),
        "noiseFloor": round(nf, 2) if nf is not None else None,
        "touchTol": None,
        "rotation": {"signals": 0, "negations": 0, "ratio": None, "verdict": None},
        # R4 ADVISORY(promotion には一切影響しない。表示と観測のみ)
        "vwapPath": {"side": None, "state": None, "drift": None, "blockers": []},
        "vpPath": {"side": None, "state": None, "target": None, "blockers": []},
        # R10 ADVISORY(ICT)。promotion 確定後に付ける観測値。
        "ict": None,
        # R11-D: ライブ判断の正本。旧 levels[].promotion は後方互換用に残す。
        "candidates": [],
        "decision": None,
        "levels": [],
        "summary": "",
    }

    if nf is None or nf <= 0:
        # fail-closed: 連鎖は「証明できたら許可」なので、証明不能 = 不許可。
        result["insufficient"] = f"確定足 {len(bars)}本<{NOISE_BARS}"
        result["vwapPath"]["blockers"] = ["INSUFFICIENT_BARS"]
        for level in levels:
            level.update({
                "dynamic": bool(VWAP_RE.search(level["label"])),
                "role": None, "freshness": None, "bodyTouches": 0,
                "anchorOk": {"BUY": False, "SELL": False}, "chains": [],
                "promotion": {side: {"allowed": False, "grade": None,
                                     "blockers": ["INSUFFICIENT_BARS"]}
                              for side in ("BUY", "SELL")},
                "rrPotential": {"BUY": None, "SELL": None},
            })
            result["levels"].append(level)
        result["candidates"], result["decision"] = strategy_evaluation(
            bundle, result, bars, levels, nf, None)
        result["summary"] = summarize(result, prm)
        return result

    tol = max(prm["touch_pt"], 0.10 * nf)
    result["touchTol"] = round(tol, 2)
    rot = rotation(bars, nf, prm)
    result["rotation"] = rot
    ref_vwap = _num(bundle, "vwap")
    if ref_vwap is None:
        ref_vwap = _num(snapshot, "vwap")
    result["vwapPath"] = vwap_path(bars, ref_vwap, tol, prm, vwaps)
    result["vpPath"] = vp_path(bars, levels, tol, prm)

    for level in levels:
        level["dynamic"] = bool(VWAP_RE.search(level["label"]))
        if level["dynamic"]:
            # VWAP は毎足動くので、静的スナップショット価格に対する連鎖評価が定義できない。
            level.update({"role": None, "freshness": None, "bodyTouches": 0,
                          "anchorOk": {"BUY": False, "SELL": False}, "chains": []})
        else:
            price = level["price"]
            role = orientation(bars, price, tol)
            fresh, touches = freshness_scan(bars, price, tol, prm["consumed"])
            chains = [c for c in
                      [best_chain(bars, price, tol, nf, "BUY", prm),
                       best_chain(bars, price, tol, nf, "SELL", prm),
                       best_flip(bars, price, tol, "BUY", prm),
                       best_flip(bars, price, tol, "SELL", prm)] if c]
            if fresh == "BROKEN" and any(c["type"] == "FLIP" and c["state"] == "FLIP_HELD"
                                         for c in chains):
                fresh = "FLIPPED"                     # 破壊が反対方向のアンカーとして完成した
            level.update({"role": role, "freshness": fresh, "bodyTouches": touches,
                          "anchorOk": anchor_ok(fresh, role), "chains": chains})
        level["promotion"] = {side: compose(level, level["chains"], side, rot, nf, prm)
                              for side in ("BUY", "SELL")}
        result["levels"].append(level)

    # R8 §4: rrPotential は promotion 確定後、全レベルの dynamic フラグが
    # 揃ってから計算する(他レベルを走路候補として参照するため)。
    # promotion / chains / grade は一切読まない — 触れても値は変わらない。
    for level in levels:
        level["rrPotential"] = ({"BUY": None, "SELL": None} if level["dynamic"]
                                else rr_potential(level, levels, nf, tol))

    # R12: ICT証拠は明示されたレンジ・CVD状態・同一時刻peerを含めて保存する。
    # ローリング3Mの表示レンジはOTE候補に渡さない。
    price = _num(bundle, "price")
    range_anchor = resolve_range_anchor(bundle, snapshot, price)
    health = cvd_health(bundle, snapshot, bars)
    result["ict"] = ict_context(
        bars, levels, price, tol, bundle.get("at"), snapshot.get("peers"),
        snapshot.get("peerMeta", bundle.get("peerMeta")),
        snapshot.get("sessionId", bundle.get("sessionId")), range_anchor, health, nf,
        snapshot.get("smtObservation", bundle.get("smtObservation")))

    result["candidates"], result["decision"] = strategy_evaluation(
        bundle, result, bars, levels, nf, result["ict"])

    result["summary"] = summarize(result, prm)
    decision = result.get("decision") or {}
    if decision.get("model") and decision.get("model") != "FLAT":
        result["summary"] += " · DECISION: %s %s [%s]" % (
            decision.get("model"), decision.get("side"), decision.get("grade") or "B")
    return result


# --- R6 §3: --card(Mini App の evaluation 骨格を機械生成する)--------------

CARD_MAX_BYTES = 4096          # R6 §2 のサイズ上限(Worker が超過を拒否する)
# 3口座同時の SL 上限(= 口座上限 $105 ÷ $2.00)。**強制側と一致させること。**
# 実際に降格を判定するのは monitor_publish.vol_gate で、あちらは .secrets の
# RISK_* から導出する。ここは表示用の既定値なので二重管理になっており、
# **ずれるとカードの ruling と実際の降格判定が食い違う。**
#
# 2026-08-19 の実害: $50 時代の 25.0 が残ったまま $60 → $105 と2度の
# 引き上げを見逃し、実データ221バンドル中 **192件(86.9%)で判定が食い違った**
# (カードは「停止」160件・実際の強制は「通常」155件でほぼ反転)。
# 口座上限を変えたら NQX_SL_CAP_PT かこの定数を必ず更新する。
CARD_SL_CAP = 60.0
# R30: ULTRA の SL 点数上限。ULTRA のリスクの正本は残ドローダウンに対する
# ドル額なので、点数では縛らない。それでも無限にはしない —— ここは
# 「構造として説明できない SL」を弾く最後の枠で、ノイズフロアの下限
# (MIN_RISK_NF_MULT)と対になる上側の枠。
ULTRA_SL_CAP_PT = 400.0
VOL_APLUS = 0.40
VOL_STANDDOWN = 0.60


def ultra_mode_enabled():
    """ULTRA 経路で回しているか。SL 上限の正本が切り替わる(R30)。"""
    return str(os.environ.get("NQX_ULTRA_MODE", "0")).strip() in {"1", "true", "TRUE", "yes"}


def sl_cap_pt():
    """SL 上限(pt)。env で上書きできる。

    既定値を正しく保つのが要点。``--sl-cap`` は任意入力なので、運用者が
    渡し忘れた瞬間に既定値がそのまま表示に出る(2026-08-19 の実害)。

    **ULTRA では点数の上限を外す(R30)。** ULTRA のリスクの正本は
    「枚数 × SL幅 × $2 が残ドローダウンに収まるか」であって、SL の絶対点数
    ではない(``ultra_mode._contract_blockers`` が残 DD で判定している)。
    枚数は TP から逆算されるので、SL と TP が比例して広がる限りドル損失は
    変わらない —— 実際 ULTRA の 1 トレード損失は概ね「利益目標 ÷ R」になる。

    実測では 60pt 上限が落としていたのは 348 候補中 **10 本**(上限だけが
    理由なのは 5 本)で、しかもそれらの runner R 中央値 8.58 は通過組の
    10.55 より**低い**。つまりこの上限撤廃は火力の主因ではない。主因は
    枚数計算の是正(``required_qty_split``)。それでも点数上限は ULTRA の
    リスク定義と噛み合わないので外す。
    """
    if ultra_mode_enabled():
        return _env_float("NQX_ULTRA_SL_CAP_PT", ULTRA_SL_CAP_PT)
    return _env_float("NQX_SL_CAP_PT", CARD_SL_CAP)


def _vol_ruling(ratio):
    """CLAUDE.md §3 のボラ予算ゲートと同じ閾値(表示用に再掲する)。"""
    if ratio is None:
        return "不明"
    if ratio > VOL_STANDDOWN:
        return "停止"
    return "A+のみ" if ratio > VOL_APLUS else "通常"


def _card_msnr(result, price, side):
    """card に載せる1レベル分。--level 指定があればそれ、無ければ最有望連鎖。"""
    levels = result["levels"]
    if not levels:
        # 「判定していない」と「レベルを受け取ったが連鎖なし」を表示層が
        # 区別できるよう、評価済みの否定結果も必ず配送する。
        return {
            "label": None, "price": None, "tier": None, "confluence": None,
            "freshness": None, "side": side,
            "chainType": None, "chainState": None, "barsLeft": None,
            "allowed": False, "grade": None, "blockers": ["NO_LEVELS"],
        }
    target = None
    if price is not None:
        for level in levels:
            if abs(level["price"] - price) <= DEDUPE_PT:
                if target is None or abs(level["price"] - price) < abs(target["price"] - price):
                    target = level
    chain = None
    if target is not None:
        cands = [c for c in target["chains"] if side is None or c["side"] == side]
        chain = max(cands, key=lambda c: CHAIN_RANK[c["state"]], default=None)
    else:                                        # summary と同じ「最も進んだ連鎖」
        best = None
        for level in levels:
            for cand in level["chains"]:
                key = (CHAIN_RANK[cand["state"]],
                       1 if level["promotion"][cand["side"]]["allowed"] else 0,
                       _chain_start(cand))
                if best is None or key > best[0]:
                    best = (key, level, cand)
        if best is None:
            # ここが旧実装の欠落点。全レベルを評価して NO_CHAIN になっても
            # None を返したため、カード/Worker/Mini App から MSNR 判定が消えた。
            # 現値に最も近い静的レベル（現値不明なら tier/confluence 順）を
            # 代表として配送する。これは表示選択だけで promotion は変えない。
            static = [level for level in levels if not level.get("dynamic")]
            pool = static or levels
            if price is not None:
                target = min(pool, key=lambda level: abs(level["price"] - price))
            else:
                target = min(pool, key=lambda level: (
                    level.get("tier") if level.get("tier") is not None else 99,
                    -(level.get("confluence") or 0), level.get("label") or ""))
        else:
            _, target, chain = best

    if side:
        use_side = side
    elif chain:
        use_side = chain["side"]
    elif target.get("role") == "RESISTANCE":
        use_side = "SELL"
    elif target.get("role") == "SUPPORT":
        use_side = "BUY"
    else:
        # 方向性のないレベルは、許可済みを優先し、その後 blocker が少ない側を
        # 表示代表にする。同率は決定論的に BUY。発注可否の変更には使わない。
        use_side = min(("BUY", "SELL"), key=lambda candidate_side: (
            not bool((target.get("promotion") or {}).get(candidate_side, {}).get("allowed")),
            len((target.get("promotion") or {}).get(candidate_side, {}).get("blockers") or []),
            candidate_side != "BUY"))
    promo = target["promotion"][use_side]
    # 完成した連鎖(RETEST_HELD / FLIP_HELD)の寿命を決めるのは CHAIN_TTL ではなく
    # **COMPLETE_TTL(保持足からの経過)**。barsLeft をそのまま出すと、生きている
    # 連鎖に「残0本」と表示される(2026-08-22 に実害)。summarize() と同じ数にする。
    bars_left = None
    if chain:
        bars_left = max(chain["barsLeft"], 0)
        if chain["state"] in COMPLETE_STATES:
            since = (chain.get("barsSinceRetest") if chain["type"] == "SWEEP"
                     else chain.get("barsSinceHold"))
            bars_left = max(params()["complete_ttl"] - (since or 0), 0)
    out = {
        "label": target["label"], "price": target["price"],
        "tier": target["tier"], "confluence": target["confluence"],
        "freshness": target["freshness"], "side": use_side,
        "chainType": chain["type"] if chain else None,
        "chainState": chain["state"] if chain else None,
        "barsLeft": bars_left,
        "allowed": promo["allowed"], "grade": promo["grade"],
        "blockers": list(promo["blockers"]),
    }
    return out


def _best_rr_potential(result):
    """R8 §4.1: 全レベル・全 side から rb2R 最大の1件を選ぶ(--card の advisory 用)。

    rb2(ランナーの走路)が無いレベルは候補にしない。promotion / allowed とは
    無関係に選ぶ(ADVISORY — 武装していないレベルの走路も表示され得る)。
    """
    best = None
    for level in result.get("levels", []):
        rrp = level.get("rrPotential") or {}
        for side in ("BUY", "SELL"):
            info = rrp.get(side)
            if not info or info.get("rb2R") is None:
                continue
            if best is None or info["rb2R"] > best[0]:
                best = (info["rb2R"], info)
    return best[1] if best else None


# --- R9 §1: ストップ予算の検算(2026-08-21 の敗戦から) -------------------
# 実測: Entry 29,327 / SL 28.75pt に対し **最大逆行 29.5pt** — 0.75pt(3tick)
# 足りずに刈られ、その後 TP 方向へ 17pt 走った。SL 上限は 45pt あり、
# **予算を 16.25pt(36%)残したまま最小緩衝を選んでいた**。
REACTION_BUFFER_MULT = 1.5     # 反応型(逆行方向に売買する型)が要求する緩衝の倍率


def stop_budget(entry, stop, nf, sl_cap=None):
    """SL 距離が「予算」に対してどれだけ使えているかを返す(純粋関数)。

    ``pass`` は「反応型として十分な緩衝か」。**予算内なのに最小緩衝で置いた**
    ストップを不合格にする。予算に収まらない場合は広げるのではなく見送る
    (TRADING_CONTEXT §6 初期ストップの手順5)。
    """
    if sl_cap is None:
        sl_cap = sl_cap_pt()
    if entry is None or stop is None or not nf or not sl_cap:
        return None
    dist = abs(float(entry) - float(stop))
    want = REACTION_BUFFER_MULT * nf
    fits = want <= sl_cap
    return {
        "distPt": round(dist, 2),
        "capPt": round(sl_cap, 2),
        "utilization": round(dist / sl_cap, 2),
        "nfMult": round(dist / nf, 2),
        "wantPt": round(want, 2),          # 反応型の推奨最小距離
        "wantFits": fits,                  # それが予算に収まるか
        "pass": dist >= want or not fits,  # 収まらないなら距離では落とさない
    }


def vp_headroom(vp, entry, side, nf):
    """VP 回転目標がエントリー時点で使い果たされていないかを返す。

    2026-08-21: `VP_ACCEPTED → target 29,325.35` に対し Entry 29,327 =
    **残り 1.65pt**。回転はほぼ完了しており、TP 29,265 は目標の 60pt 先だった。
    """
    if not vp or vp.get("target") is None or entry is None or not nf:
        return None
    target = float(vp["target"])
    remain = (float(entry) - target) if side == "SELL" else (target - float(entry))
    return {
        "target": target,
        "remainPt": round(remain, 2),
        "remainNf": round(remain / nf, 2),
        # 目標まで 0.25×NF 未満 = その回転はもう終わっている
        "exhausted": remain < 0.25 * nf,
    }


def build_card(result, sl_cap=None, price=None, side=None,
               entry=None, stop=None, bundle=None):
    """R6 §2 スキーマのうち at / volGate / rotation / msnr / advisory / summary、
    および ``bundle`` を渡したときだけ htf / entry。

    htf / entry はかつて「監視ループの Claude が追記する」前提で空にしていた。
    R51 で転記工程そのものを廃止した(エージェントは nqx_cycle.py を1回叩くだけ)
    ため、以後この2行は**誰も書かず**、Mini App の 15M ALIGN / ENTRY 行が常に
    N/A のまま残っていた(2026-09-08 実測: 直近 964 サイクル全てで欠落)。
    材料は bundle に揃っている(15分 study の CT_TREND / CT_ATR、decision の
    entry / stop、現値)ので、ここで機械的に導く。判定は増やさない —
    ``aligned`` は決定済み side と 15分トレンドの符号一致、``pass`` は
    decision 自身の武装可否(ARMED|ACTIVE かつ hardBlockers 無し)の写しである。

    ``sl_cap`` を省略すると `sl_cap_pt()`(既定 60pt / NQX_SL_CAP_PT)を使う。
    **import 時ではなく呼び出し時に解決する** — 既定引数に定数を置くと env の
    上書きが効かなくなるため。
    """
    if sl_cap is None:
        sl_cap = sl_cap_pt()
    noise = result.get("noiseFloor")
    ratio = None
    if noise is not None and sl_cap:
        ratio = round(noise / sl_cap, 2)
    rot = result.get("rotation") or {}
    card = {
        "at": result.get("at"),
        "volGate": {"noise": noise, "slCap": sl_cap, "ratio": ratio,
                    "ruling": _vol_ruling(ratio)},
        "rotation": {"signals": rot.get("signals") or 0,
                     "negations": rot.get("negations") or 0,
                     "verdict": rot.get("verdict") or "不明"},
        "summary": result.get("summary") or "",
    }
    # R43: VOL/MSNRだけでは「入力は健全なのか」「CVD欠落でA+が落ちたのか」
    # 「単にICT時間帯の減点なのか」をアプリから区別できない。判定を増やさず、
    # evaluate() が既に凍結したICT文脈を表示用の小さなゲートへ写す。
    ict = result.get("ict") if isinstance(result.get("ict"), dict) else {}
    cvd = ict.get("cvd") if isinstance(ict.get("cvd"), dict) else None
    if cvd:
        card["cvdGate"] = {
            key: cvd.get(key) for key in (
                "status", "available", "freshness", "aplusAllowed", "attempts",
                "maxAttempts", "refreshRequired", "reason",
            ) if key in cvd
        }
    session = ict.get("session") if isinstance(ict.get("session"), dict) else None
    if session:
        card["sessionGate"] = {
            key: session.get(key) for key in (
                "window", "label", "tradeable", "et", "note",
            ) if key in session
        }
    decision = result.get("decision")
    if isinstance(decision, dict):
        # R11-D: decision は evaluation の正本。候補全件をカードへ詰めず、
        # 最有力1件と順位だけを残して4096バイト制限を守る。
        card["decision"] = {
            key: decision.get(key) for key in (
                "decisionId", "phase", "model", "side", "state", "grade", "score",
                "entryMode", "entry", "stop", "targets", "targetR", "hardBlockers",
                "penalties", "evidence", "modelRank", "strategyModels", "strategyAlignment", "strategyBias",
                "silverBullet", "vwapStop", "poolStop", "sweepGate",
            ) if key in decision
        }
    msnr = _card_msnr(result, price, side)
    if msnr:
        card["msnr"] = msnr

    advisory = {}
    vwp = result.get("vwapPath") or {}
    if vwp.get("state"):
        advisory["vwap"] = {"side": vwp["side"], "state": vwp["state"],
                            "drift": vwp.get("drift")}
    elif "VWAP_DRIFT" in (vwp.get("blockers") or []):
        advisory["vwap"] = {"side": None, "state": "VWAP_DRIFT",
                            "drift": vwp.get("drift")}
    vpp = result.get("vpPath") or {}
    if vpp.get("state"):
        advisory["vp"] = {"side": vpp["side"], "state": vpp["state"],
                          "target": vpp.get("target")}
    # R10 §2: ICT は ADVISORY。side 指定があればその side の注意点も出す。
    ict = result.get("ict") or {}
    if ict.get("summary"):
        entry_ict = {"summary": ict["summary"]}
        chk = ict_side_check(ict, side) if side else None
        if chk and chk["notes"]:
            entry_ict["notes"] = chk["notes"]
        advisory["ict"] = entry_ict

    matrix = result.get("strategyMatrix") or {}
    if matrix:
        alignment = matrix.get("alignment") or {}
        advisory["strategies"] = {
            "version": matrix.get("version"),
            "active": list(matrix.get("activeModels") or [])[:8],
            "bias": alignment.get("primary"),
            "buy": alignment.get("BUY", 0),
            "sell": alignment.get("SELL", 0),
            "summary": matrix.get("summary"),
        }

    # Silver Bullet は1分足が無いときも「未取得」と明示する。候補の表示は
    # 監査用であり、ここから発注可否を緩めない。
    silver_bullet = result.get("silverBullet")
    if isinstance(silver_bullet, dict):
        advisory["silverBullet"] = {
            key: silver_bullet.get(key) for key in (
                "model", "version", "mode", "status", "timeframe", "bars",
                "window", "selected", "reason", "detail", "setupsSeen",
            ) if key in silver_bullet
        }

    # R8 §4.1: rrPotential は advisory 内の最後尾に置く(縮小時に最初に落ちる枠)
    rr = _best_rr_potential(result)
    if rr:
        prefix = "高RR候補 " if rr["rb2R"] >= RR_HIGH_CANDIDATE else ""
        advisory["rrPotential"] = (
            f"{prefix}走路 rb2 {rr['rb2R']:.1f}R"
            f"({rr['rb2Label']} まで {rr['rb2Pt']:.0f}pt)"
        )
    if advisory:
        card["advisory"] = advisory

    # R9: entry/stop を渡されたときだけ増える。既存の呼び出しは1バイトも変わらない。
    sb = stop_budget(entry, stop, noise, sl_cap)
    if sb:
        card["stopBudget"] = sb
    vh = vp_headroom(advisory.get("vp"), entry, side, noise)
    if vh:
        card["vpHeadroom"] = vh

    # bundle を渡されたときだけ増える(--card CLI と monitor_publish が渡す)。
    # 渡さない既存呼び出しの出力は1バイトも変わらない。
    if isinstance(bundle, dict):
        htf_gate = htf_gate_from_bundle(bundle, card.get("decision"))
        if htf_gate:
            card["htf"] = htf_gate
        entry_gate = entry_gate_from_decision(card.get("decision"), price,
                                              card.get("msnr"))
        if entry_gate:
            card["entry"] = entry_gate

    return _shrink_card(card)


def _ct15_value(bundle, key):
    """15分 study(``snapshot.study15m``)の1値。tv_snapshot が書いた生値だけを読む。"""
    snapshot = bundle.get("snapshot") if isinstance(bundle.get("snapshot"), dict) else {}
    study = snapshot.get("study15m") if isinstance(snapshot.get("study15m"), dict) else {}
    return _num(study, key)


def htf_gate_from_bundle(bundle, decision):
    """APP_EVAL_DISPLAY_SPEC §2 の ``htf`` を bundle から機械生成する。

    ``ctTrend`` は 15分 SwingArm の ``NQX_DATA_CT_TREND`` の符号(−1 / 0 / +1)、
    ``ctAtr`` は ``NQX_DATA_CT_ATR``(無ければ tv_snapshot の ``ctAtr15``)。
    ``aligned`` は decision の side と符号が一致するときだけ True。side が無い
    (FLAT / WATCH で方向未決)ときは False にしておき、アプリ側が「判定していない」
    と描く(Worker は boolean しか通さないため null を運べない)。
    トレンドも ATR も無ければ None(N/A のまま。0 で埋めない)。
    """
    if not isinstance(bundle, dict):
        return None
    trend = _ct15_value(bundle, "NQX_DATA_CT_TREND")
    atr = _ct15_value(bundle, "NQX_DATA_CT_ATR")
    if atr is None:
        atr = _num(bundle, "ctAtr15")
    if trend is None and atr is None:
        return None
    sign = 0 if trend is None else (1 if trend > 0 else -1 if trend < 0 else 0)
    side = decision.get("side") if isinstance(decision, dict) else None
    aligned = (side == "BUY" and sign > 0) or (side == "SELL" and sign < 0)
    out = {"ctTrend": sign, "aligned": bool(aligned)}
    if atr is not None:
        out["ctAtr"] = round(atr, 2)
    return out


def entry_gate_from_decision(decision, price=None, msnr=None):
    """APP_EVAL_DISPLAY_SPEC §2 の ``entry`` を decision から機械生成する。

    - ``structure``: 評価対象の MSNR レベル価格(無ければ構造否定価格 = stop)
    - ``airPt`` / ``airPct``: 建値と structure の隙間(pt と、リスク幅に対する %)
    - ``reachR``: 現値から建値までの距離をリスク幅で割った値
      (R26 の gapR と同じ量。``LIMIT_MAX_GAP_R`` を超える指値は待ちが長すぎる)
    - ``pass``: decision 自身の武装可否の写し(ARMED|ACTIVE かつ hardBlockers 無し)。
      ここで新しい判定は作らない。
    建値か SL が無い decision(FLAT)では None。
    """
    if not isinstance(decision, dict):
        return None
    entry = _num(decision, "entry")
    stop = _num(decision, "stop")
    if entry is None or stop is None:
        return None
    risk = abs(entry - stop)
    if risk <= 0:
        return None
    structure = _num(msnr, "price") if isinstance(msnr, dict) else None
    if structure is None:
        structure = stop
    air_pt = abs(entry - structure)
    out = {
        "structure": round(structure, 2),
        "airPt": round(air_pt, 2),
        "airPct": int(round(air_pt / risk * 100.0)),
    }
    last = None
    try:
        last = float(price) if price is not None else None
        if last is not None and not math.isfinite(last):
            last = None
    except (TypeError, ValueError):
        last = None
    if last is not None:
        out["reachR"] = round(abs(last - entry) / risk, 2)
    state = str(decision.get("state") or "WATCH").upper()
    blockers = decision.get("hardBlockers")
    blocked = bool(blockers) if isinstance(blockers, list) else False
    out["pass"] = state in {"ARMED", "ACTIVE"} and not blocked
    return out


def _card_bytes(card):
    return len(json.dumps(card, ensure_ascii=False).encode("utf-8"))


def _shrink_card(card):
    """4096 バイトに収める。落とす順は advisory.rrPotential → advisory.ict →
    advisory 全体 → decision.poolStop(R103-1)→ decision.modelRank/evidence/penalties →
    msnr.blockers(R6 §2 / R8 §4.1 / R10 §2)。"""
    if _card_bytes(card) <= CARD_MAX_BYTES:
        return card
    if "advisory" in card:
        card["advisory"].pop("rrPotential", None)
        if _card_bytes(card) <= CARD_MAX_BYTES and card["advisory"]:
            return card
        card["advisory"].pop("ict", None)
        if not card["advisory"]:
            card.pop("advisory", None)
    if _card_bytes(card) <= CARD_MAX_BYTES:
        return card
    card.pop("advisory", None)
    if _card_bytes(card) <= CARD_MAX_BYTES:
        return card
    if "decision" in card:
        decision = card["decision"]
        # R103-1: SHADOW の監査(poolStop)は evidence の記録タグより先に落とす。
        # タグはスコアカードの分離キーで、監査は再生で復元できる。
        decision.pop("poolStop", None)
        decision.pop("sweepGate", None)
        # R121: STDV の監査も SHADOW の記録なので evidence タグより先に落とす。
        decision.pop("ictStdv", None)
        if _card_bytes(card) <= CARD_MAX_BYTES:
            return card
        decision.pop("modelRank", None)
        decision.pop("evidence", None)
        decision.pop("penalties", None)
    if _card_bytes(card) <= CARD_MAX_BYTES:
        return card
    if "msnr" in card:
        card["msnr"]["blockers"] = card["msnr"]["blockers"][:1]
    if _card_bytes(card) <= CARD_MAX_BYTES:
        return card
    card["summary"] = card["summary"][:120]
    return card


# --- CLI ------------------------------------------------------------------

def _filtered(result, price, side):
    """--level / --side 指定時の縮約ビュー。"""
    match = None
    for level in result["levels"]:
        if abs(level["price"] - price) <= DEDUPE_PT:
            if match is None or abs(level["price"] - price) < abs(match["price"] - price):
                match = level
    if match is None:
        return {"at": result["at"], "requested": price, "matched": False,
                "note": f"levels に {price} ±{DEDUPE_PT}pt のレベルがない",
                "summary": result["summary"]}
    sides = [side] if side else ["BUY", "SELL"]
    return {
        "at": result["at"], "matched": True, "label": match["label"],
        "price": match["price"], "tier": match["tier"],
        "confluence": match["confluence"], "mergedWith": match["mergedWith"],
        "dynamic": match["dynamic"],
        "role": match["role"], "freshness": match["freshness"],
        "anchorOk": {s: match["anchorOk"][s] for s in sides},
        "rotation": result["rotation"],
        "vwapPath": result["vwapPath"], "vpPath": result["vpPath"],
        "decision": result.get("decision"),
        "candidates": [{"model": c.get("model"), "side": c.get("side"),
                        "grade": c.get("grade"), "score": c.get("score"),
                        "allowed": c.get("allowed")} for c in result.get("candidates", [])],
        "chains": [c for c in match["chains"] if c["side"] in sides],
        "promotion": {s: match["promotion"][s] for s in sides},
        "rrPotential": {s: match["rrPotential"][s] for s in sides},
        "summary": result["summary"],
    }


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    want_summary = "--summary" in argv
    want_card = "--card" in argv
    sl_cap = None                       # None = sl_cap_pt() を呼び出し時に解決
    if "--sl-cap" in argv:
        try:
            sl_cap = float(argv[argv.index("--sl-cap") + 1])
            if not (math.isfinite(sl_cap) and sl_cap > 0):
                raise ValueError
        except (IndexError, ValueError):
            sys.stderr.write(json.dumps({"error": "--sl-cap には正の pt を渡す"},
                                        ensure_ascii=False) + "\n")
            return 2
    price = side = None
    if "--level" in argv:
        try:
            price = float(argv[argv.index("--level") + 1])
        except (IndexError, ValueError):
            sys.stderr.write(json.dumps({"error": "--level には価格を渡す"},
                                        ensure_ascii=False) + "\n")
            return 2
    entry = stop = None
    for flag, name in (("--entry", "entry"), ("--stop", "stop")):
        if flag in argv:
            try:
                value = float(argv[argv.index(flag) + 1])
                if not math.isfinite(value):
                    raise ValueError
            except (IndexError, ValueError):
                sys.stderr.write(json.dumps({"error": f"{flag} には価格を渡す"},
                                            ensure_ascii=False) + "\n")
                return 2
            if name == "entry":
                entry = value
            else:
                stop = value
    if "--side" in argv:
        try:
            side = argv[argv.index("--side") + 1].strip().upper()
        except IndexError:
            side = ""
        side = {"BUY": "BUY", "LONG": "BUY", "SELL": "SELL", "SHORT": "SELL"}.get(side)
        if side is None:
            sys.stderr.write(json.dumps({"error": "--side は buy / sell"},
                                        ensure_ascii=False) + "\n")
            return 2

    try:
        raw = sys.stdin.buffer.read().decode("utf-8-sig")   # Write ツール製 JSON は BOM 付き
        bundle = json.loads(raw)
        if not isinstance(bundle, dict):
            raise ValueError("bundle must be an object")
        result = evaluate(bundle)
    except Exception as exc:
        sys.stderr.write(json.dumps({"error": f"{type(exc).__name__}: {exc}"},
                                    ensure_ascii=False) + "\n")
        return 2

    if want_card:
        out = json.dumps(build_card(result, sl_cap, price, side, entry, stop,
                                    bundle=bundle),
                         ensure_ascii=False, indent=1)
    elif want_summary:
        out = result["summary"]
    elif price is not None:
        out = json.dumps(_filtered(result, price, side), ensure_ascii=False, indent=2)
    else:
        out = json.dumps(result, ensure_ascii=False, indent=2)
    sys.stdout.write(out + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

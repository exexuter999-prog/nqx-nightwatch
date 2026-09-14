#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""経済イベント(重要指標・要人発言)の取得・キャッシュ・ブラックアウト判定

データ源は ForexFactory の公開カレンダー JSON(週次)。ネットワークに出るのは
``--refresh`` のときだけで、監視ループ(monitor_publish.py)はキャッシュを
読むだけにする。フィードが取れない・古い場合は「データなし」として扱い、
監視は止めない(ブラックアウトはデータがあるときだけ効く fail-open。
ここを fail-closed にすると、フィード側の障害で取引全体が止まる)。

使い方:
  python events.py --refresh          # 取得してキャッシュ(週初と毎朝)
  python events.py                    # 本日の対象イベント一覧(JST)と封鎖状態
  python events.py --next             # 直近イベントとブラックアウト状態
  python events.py --add "Powell Speaks" --at 2026-08-20T02:00+09:00 --impact High
                                      # 手動イベント(急な要人発言の追加など)

ブラックアウト規則(env で変更可):
  High   … 前 NQX_EVENT_PRE_MIN(既定15)分 〜 後 NQX_EVENT_POST_MIN(既定10)分
           → 監視側が ARMED/ACTIVE を WATCH に降格する(発注ボタンを出さない)
  Medium … 表示のみ(警告)。降格はしない

時刻の正本は UTC で保存し、表示だけローカル(JST)に変換する。
フィードの時刻は一次情報ではないため、NQX 的な検証水準では UNVERIFIED 扱い
(スキルの references/event-volatility.md)。ここでの用途は「その時間帯に
新規を武装しない」という運用ゲートであって、イベント分析の根拠ではない。
"""
import argparse
import json
import os
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

for _s in ("stdout", "stderr"):
    _f = getattr(sys, _s, None)
    if _f is not None and hasattr(_f, "reconfigure"):
        try:
            _f.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

BASE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(BASE, ".secrets", "events_cache.json")
MANUAL = os.path.join(BASE, ".secrets", "events_manual.json")

FEED_URL = os.environ.get(
    "NQX_EVENT_FEED", "https://nfs.faireconomy.media/ff_calendar_thisweek.json")
# 週次フィードなので、これより古いキャッシュは「データなし」として捨てる。
CACHE_MAX_AGE_DAYS = 8
PRE_MIN = float(os.environ.get("NQX_EVENT_PRE_MIN", "15"))
POST_MIN = float(os.environ.get("NQX_EVENT_POST_MIN", "10"))
COUNTRIES = [c.strip().upper() for c in
             os.environ.get("NQX_EVENT_COUNTRIES", "USD").split(",") if c.strip()]


def _now(now=None):
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc)


def _parse_at(value):
    """ISO 時刻を UTC aware datetime に。TZ の無い時刻は受け付けない。"""
    raw = str(value or "").strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        raise ValueError(f"event time must include a timezone: {value!r}")
    return parsed.astimezone(timezone.utc)


def _covers(rows, now):
    """収録されたイベントが「今日」を含んでいるか(R35)。

    フィードは週単位なので、fetchedAt が新しくても中身が前の週ということが
    起きる。取得時刻の年齢だけを見ていたため、**今週のイベントを 1 件も
    持たないキャッシュが「新鮮」と判定**され、封鎖が黙って無効になっていた。

    判定は「収録の最終時刻が今より後か」。1 件も読めなければ覆っていない扱い。
    """
    latest = None
    for item in rows:
        try:
            at = _parse_at(item.get("at") or item.get("date"))
        except (ValueError, TypeError):
            continue
        if latest is None or at > latest:
            latest = at
    return latest is not None and latest >= now


def normalize_feed(raw):
    """フィードの1週間分を正規化する。読めない行は黙って捨てず件数を返す。"""
    events, skipped = [], 0
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            skipped += 1
            continue
        title = str(item.get("title", "")).strip()
        impact = str(item.get("impact", "")).strip().capitalize()
        try:
            at = _parse_at(item.get("date"))
        except (ValueError, TypeError):
            skipped += 1
            continue
        if not title:
            skipped += 1
            continue
        events.append({
            "title": title,
            "country": str(item.get("country", "")).strip().upper(),
            "impact": impact if impact in ("High", "Medium", "Low") else "Low",
            "at": at.isoformat(),
            "source": "ff-calendar",
        })
    return events, skipped


def refresh():
    req = urllib.request.Request(FEED_URL, headers={"User-Agent": "nqx-nightwatch"})
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = json.load(r)
    events, skipped = normalize_feed(raw)
    if not events:
        raise SystemExit("ERROR: フィードから1件も読めませんでした(形式変更の可能性)")
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    payload = {
        "fetchedAt": _now().isoformat(),
        "source": FEED_URL,
        "events": events,
    }
    with open(CACHE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return events, skipped


def _load_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def load_manual():
    data = _load_json(MANUAL)
    return data if isinstance(data, list) else []


def add_manual(title, at, impact):
    at_utc = _parse_at(at)
    impact = str(impact or "High").capitalize()
    if impact not in ("High", "Medium"):
        raise SystemExit("ERROR: --impact は High か Medium を指定してください")
    items = load_manual()
    items.append({
        "title": str(title).strip(),
        "country": "USD",
        "impact": impact,
        "at": at_utc.isoformat(),
        "source": "manual",
        "addedAt": _now().isoformat(),
    })
    os.makedirs(os.path.dirname(MANUAL), exist_ok=True)
    with open(MANUAL, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)
    return items[-1]


def load_events(now=None):
    """キャッシュ+手動イベントを統合して返す。(events, notes)。

    欠損・陳腐化は notes で報告する。ここでは絶対に例外で落とさない
    (呼び出し側は監視ループ。イベント機能の故障で通知を殺さない)。
    """
    now = _now(now)
    events, notes = [], []
    cache = _load_json(CACHE)
    if not isinstance(cache, dict):
        notes.append("no calendar cache — run `python events.py --refresh`")
    else:
        try:
            fetched = _parse_at(cache.get("fetchedAt"))
            age_days = (now - fetched).total_seconds() / 86400.0
        except (ValueError, TypeError):
            fetched, age_days = None, None
        if age_days is None or age_days > CACHE_MAX_AGE_DAYS:
            notes.append(
                f"calendar cache is stale ({age_days:.1f}d old — refresh required)"
                if age_days is not None else "calendar cache has no valid fetchedAt")
        else:
            rows = [item for item in cache.get("events") or [] if isinstance(item, dict)]
            # R35: **取得時刻ではなく収録期間**で鮮度を見る。
            #
            # フィードは `ff_calendar_thisweek.json`(その週ぶん)なので、
            # fetchedAt が新しくても中身は前の週ということが起きる。実際
            # 2026-08-24 時点で 8/19 取得・8/16〜8/21 収録のキャッシュが
            # 「新鮮」と判定され、**今週のイベントを 1 件も持たないまま
            # notes も空**だった。封鎖が黙って無効になっていた。
            covered = _covers(rows, now)
            if not covered:
                notes.append("calendar cache does not cover today — "
                             "run `python events.py --refresh`")
            else:
                events.extend(rows)
    for item in load_manual():
        if isinstance(item, dict):
            events.append(item)
    return events, notes


def relevant(events):
    """監視対象国の Medium/High だけに絞り、時刻順に返す。"""
    out = []
    for item in events:
        if item.get("impact") not in ("High", "Medium"):
            continue
        if "ALL" not in COUNTRIES and item.get("country") not in COUNTRIES:
            continue
        try:
            at = _parse_at(item.get("at"))
        except (ValueError, TypeError):
            continue
        out.append({**item, "_at": at})
    out.sort(key=lambda item: item["_at"])
    return out


def _window(item):
    at = item["_at"]
    return at - timedelta(minutes=PRE_MIN), at + timedelta(minutes=POST_MIN)


def active_blackout(now=None, events=None):
    """今ブラックアウト中の High イベントを返す(無ければ None)。"""
    now = _now(now)
    if events is None:
        events, _ = load_events(now)
    for item in relevant(events):
        if item.get("impact") != "High":
            continue
        start, end = _window(item)
        if start <= now <= end:
            return {**item, "windowStart": start.isoformat(),
                    "windowEnd": end.isoformat()}
    return None


def active_warning(now=None, events=None):
    """今警告帯にいる Medium イベント(表示のみ・降格しない)。"""
    now = _now(now)
    if events is None:
        events, _ = load_events(now)
    for item in relevant(events):
        if item.get("impact") != "Medium":
            continue
        start, end = _window(item)
        if start <= now <= end:
            return item
    return None


def upcoming(now=None, events=None, horizon_min=720.0, limit=3):
    """これから horizon 分以内の対象イベント(表示用)。"""
    now = _now(now)
    if events is None:
        events, _ = load_events(now)
    out = []
    for item in relevant(events):
        delta_min = (item["_at"] - now).total_seconds() / 60.0
        if 0 <= delta_min <= horizon_min:
            out.append(item)
        if len(out) >= limit:
            break
    return out


def current_gate(now=None):
    """監視ループが読む現在状態。例外はここで飲み込み、必ず dict を返す。"""
    now = _now(now)
    try:
        events, notes = load_events(now)
        return {
            "blackout": active_blackout(now, events),
            "warning": active_warning(now, events),
            "upcoming": upcoming(now, events),
            "notes": notes,
        }
    except Exception as exc:  # イベント機能の故障で監視通知を殺さない
        return {"blackout": None, "warning": None, "upcoming": [],
                "notes": [f"events unavailable: {type(exc).__name__}: {exc}"]}


def fmt_local_hm(item):
    """イベント時刻をローカル(JST)HH:MM で返す。表示専用。"""
    try:
        return _parse_at(item.get("at")).astimezone().strftime("%H:%M")
    except (ValueError, TypeError):
        return "??:??"


IMPACT_JP = {"High": "高", "Medium": "中"}


def _describe(item, now):
    at = _parse_at(item["at"])
    delta_min = (at - _now(now)).total_seconds() / 60.0
    when = at.astimezone().strftime("%m/%d %H:%M")
    rel = f"{delta_min / 60:.1f}時間後" if delta_min > 90 else f"{delta_min:.0f}分後"
    if delta_min < 0:
        rel = f"{-delta_min:.0f}分前"
    mark = "☄" if item.get("impact") == "High" else "◇"
    src = "手動" if item.get("source") == "manual" else "FF"
    return f"  {mark} {when}  {item['title']}({IMPACT_JP.get(item['impact'], '?')}・{src})  {rel}"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--refresh", action="store_true", help="フィードを取得してキャッシュ")
    p.add_argument("--next", action="store_true", help="直近イベントと封鎖状態のみ")
    p.add_argument("--add", metavar="TITLE", help="手動イベントを追加")
    p.add_argument("--at", help="--add の時刻(TZ付きISO。例 2026-08-20T02:00+09:00)")
    p.add_argument("--impact", default="High", help="--add の重要度(High/Medium)")
    a = p.parse_args()

    if a.add:
        if not a.at:
            sys.exit("ERROR: --add には --at(TZ付きISO)が必要です")
        item = add_manual(a.add, a.at, a.impact)
        print(f"追加: {item['title']} {fmt_local_hm(item)}({IMPACT_JP[item['impact']]})")
        return

    if a.refresh:
        events, skipped = refresh()
        print(f"取得: {len(events)}件(読めない行 {skipped})→ {CACHE}")

    now = _now()
    events, notes = load_events(now)
    for note in notes:
        print(f"⚠ {note}")

    blackout = active_blackout(now, events)
    warning = active_warning(now, events)
    if blackout:
        until = _parse_at(blackout["windowEnd"]).astimezone().strftime("%H:%M")
        print(f"☄ ブラックアウト中: {blackout['title']}({until} まで)— 新規は武装しない")
    elif warning:
        print(f"◇ 警告帯: {warning['title']}(中)— 表示のみ")
    else:
        print("封鎖なし")

    horizon = 24 * 60.0 if not a.next else 720.0
    items = upcoming(now, events, horizon_min=horizon, limit=3 if a.next else 10)
    if items:
        print("\n直近の対象イベント(JST):")
        for item in items:
            print(_describe(item, now))
    else:
        print("直近に対象イベントなし" + ("" if notes else f"(対象: {','.join(COUNTRIES)} の中・高)"))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram Bot → order.py → CrossTrade → Tradovate

外出先からエントリー指示を出すための常駐 Bot。
発注ロジックは一切持たず、すべて order.py に委譲する。
= order.py の安全装置(2枚上限・SL必須・回数上限・向きチェック)が
  Bot 経由でもそのまま効く。

使い方:
  python telegram_bot.py            # 常駐(Ctrl-C で停止)
  python telegram_bot.py --check    # 設定と疎通の確認だけして終了

設定: .secrets/telegram.env
  TELEGRAM_TOKEN=<BotFather のトークン>
  TELEGRAM_CHAT_ID=<自分のチャットID>

依存: 標準ライブラリのみ(order.py と同じ方針)
"""
import argparse, hashlib, json, math, os, re, subprocess, sys, time
import urllib.request
import uuid, urllib.parse, urllib.error
from datetime import datetime, timezone

# Windows のコンソールは既定が cp932 で、日本語ログが化ける。
# 出力先を UTF-8 に付け替えて、環境変数を設定しなくても読めるようにする。
for _s in ("stdout", "stderr"):
    _f = getattr(sys, _s, None)
    if _f is not None and hasattr(_f, "reconfigure"):
        try:
            _f.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

BASE = os.path.dirname(os.path.abspath(__file__))
ENV = os.path.join(BASE, ".secrets", "telegram.env")
ORDER_PY = os.path.join(BASE, "order.py")

API = "https://api.telegram.org/bot{token}/{method}"
WEB_APP_URL = os.environ.get("TELEGRAM_WEB_APP_URL", "https://nqx-nightwatch.pages.dev/")
WEB_APP_MAX_QTY = 2
WEB_APP_DEFAULT_QTY = 2
MAX_ORDER_RISK_DOLLARS = 200.0
POLL_TIMEOUT = 30          # long polling の待ち秒数
SUBPROCESS_TIMEOUT = 30    # order.py の実行上限

# ---- Mini App 一回押し発注 ----
# NQX_SYMBOL は下の import 群の後で正本(contract.py)から取る(R102)。
TICK = 0.25                                             # CME MNQ の最小刻み
POINT_VALUE = 2.00                                      # MNQ 1pt = $2.00
ORDER_KEY_FILE = os.path.join(BASE, ".secrets", "nqx_order_keys.json")
ORDER_KEY_TTL_SEC = 48 * 3600                           # 消費済み鍵の保持期間
SCENARIO_MAX_AGE_SEC = 20 * 60                          # 発行から受理までの上限
POSITION_SYNC_INTERVAL_SEC = 60                         # ブローカー定期照会の間隔
STUCK_ORDER_MIN_AGE_SEC = 10 * 60                       # 観測漏れ解除を許すまでの経過時間
                                                        # (unstick.js の STUCK_ORDER_MIN_AGE_MS と一致)

# 同ディレクトリの補助モジュール。どちらも標準ライブラリのみで発注はしない。
sys.path.insert(0, BASE)
import broker_status                                    # noqa: E402
import nqx_state                                        # noqa: E402
import execution_contract                               # noqa: E402
import contract as contract_month  # R102: 取引限月の正本  # noqa: E402
NQX_SYMBOL = contract_month.symbol()                    # TRADING_CONTEXT.md §2 / R102 正本
import management_intent                                # noqa: E402
import route_envelope                                   # noqa: E402
import strategy_evidence                                # noqa: E402

# Rebind legacy presentation constants to the one execution-contract source.
MAX_ORDER_RISK_DOLLARS = execution_contract.CONTRACT["risk"]["defaultCapDollars"]
WEB_APP_DEFAULT_QTY = int(execution_contract.CONTRACT["risk"]["fixedQty"])
SCENARIO_MAX_AGE_SEC = execution_contract.CONTRACT["scenario"]["maxAgeSec"]

# 直前のドライランを保持する。/confirm でこれを --confirm 付きで再実行する。
# 期限なし(ユーザー選択)。プロセス再起動で消える。
#
# token: このドライラン固有のID。送信ボタンの callback_data に埋め込み、
#        押された時に現在の PENDING と一致するかを見る。
#        → 二重タップ・古いメッセージのボタンを押した場合は無視される。
PENDING = {"argv": None, "desc": None, "at": None, "token": None}
_SEQ = [0]


class BotResponse(str):
    """本文としても ``(本文, キーボード)`` としても扱える返信。

    常駐ループは従来どおりアンパックして使い、診断スクリプトや
    ログ表示側は文字列として扱えるようにする。発注状態そのものは
    ここに持たせず、キーボードだけを付加情報として保持する。
    """

    __slots__ = ("keyboard",)

    def __new__(cls, text="", keyboard=None):
        obj = super().__new__(cls, text or "")
        obj.keyboard = keyboard
        return obj

    def __iter__(self):
        yield str(self)
        yield self.keyboard


def response(text, keyboard=None):
    return BotResponse(text, keyboard)


def new_token():
    _SEQ[0] += 1
    return str(_SEQ[0])


def clear_pending():
    PENDING.clear()
    PENDING.update({"argv": None, "desc": None, "at": None, "token": None})


# ---------------------------------------------------------------- 設定

def load_env():
    if not os.path.exists(ENV):
        sys.exit(
            f"ERROR: {ENV} が見つかりません\n\n"
            "  1. Telegram で @BotFather → /newbot → トークン取得\n"
            "  2. @userinfobot に話しかけて自分の数値IDを取得\n"
            "  3. .secrets/telegram.env に以下を書く:\n\n"
            "     TELEGRAM_TOKEN=123456:ABC-DEF...\n"
            "     TELEGRAM_CHAT_ID=123456789\n")
    cfg = {}
    with open(ENV, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip()
    for key in ("TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID"):
        if not cfg.get(key):
            sys.exit(f"ERROR: {ENV} に {key} がありません")
    if not cfg["TELEGRAM_CHAT_ID"].lstrip("-").isdigit():
        sys.exit(f"ERROR: TELEGRAM_CHAT_ID は数値です(現在: {cfg['TELEGRAM_CHAT_ID']})")
    return cfg


# ---------------------------------------------------------------- Telegram API

#: 429(Too Many Requests)の retry_after を跨いで再送しない。Telegram の flood
#: 制御は制限中に叩くたびに延びる(2026-09-08 実測: 3 分ループの通知が
#: retry_after 549s → 338s → 484s と伸び続けた)。制限は chat への送信に掛かる
#: ので、getUpdates などの読み取りは対象外にする。
BACKOFF_FILE = os.path.join(BASE, ".secrets", "telegram_backoff.json")
BACKOFF_EXEMPT_METHODS = frozenset({
    "getUpdates", "getMe", "getWebhookInfo", "getChat", "getChatMenuButton",
    "getFile", "getMyCommands",
})
BACKOFF_MARGIN_SEC = 5.0


def _backoff_until(path=None):
    try:
        with open(path or BACKOFF_FILE, encoding="utf-8") as fh:
            return float(json.load(fh).get("until") or 0.0)
    except (OSError, ValueError, TypeError, AttributeError):
        return 0.0


def _record_backoff(method, body, path=None, now=None):
    """429 応答の retry_after を保存し、解除時刻(epoch)を返す。無ければ None。"""
    try:
        retry = float(((json.loads(body) or {}).get("parameters") or {}).get("retry_after") or 0)
    except (ValueError, TypeError, AttributeError):
        retry = 0.0
    if retry <= 0:
        return None
    clock = time.time() if now is None else float(now)
    until = clock + retry + BACKOFF_MARGIN_SEC
    payload = {
        "until": until,
        "untilLocal": datetime.fromtimestamp(until).strftime("%Y-%m-%d %H:%M:%S"),
        "retryAfter": retry,
        "method": method,
        "recordedAt": datetime.fromtimestamp(clock).strftime("%Y-%m-%d %H:%M:%S"),
    }
    target = path or BACKOFF_FILE
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        tmp = target + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
        os.replace(tmp, target)
    except OSError as exc:
        print(f"[{ts()}] backoff を保存できません: {exc}", file=sys.stderr)
    return until


def _blocked_by_backoff(method, path=None, now=None):
    """制限中なら理由を 1 行出して True。呼び出し側は送信失敗(None)として扱う。"""
    if method in BACKOFF_EXEMPT_METHODS:
        return False
    until = _backoff_until(path)
    clock = time.time() if now is None else float(now)
    if until and clock < until:
        print(f"[{ts()}] Telegram backoff: {method} skipped until "
              f"{datetime.fromtimestamp(until).strftime('%H:%M:%S')} (retry_after honoured)",
              file=sys.stderr)
        return True
    return False


def api(cfg, method, **params):
    if _blocked_by_backoff(method):
        return None
    url = API.format(token=cfg["TELEGRAM_TOKEN"], method=method)
    # urlencode は日本語を UTF-8 のパーセント表現にしてから送る。
    # ASCII に変換できる段階で bytes 化し、Windows のコードページに依存させない。
    data = urllib.parse.urlencode(
        params, encoding="utf-8", errors="strict"
    ).encode("ascii")
    req = urllib.request.Request(url, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=POLL_TIMEOUT + 15) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        # トークンが誤っている場合はここで気付けるようにする
        if e.code == 401:
            sys.exit("ERROR: Telegram トークンが無効です(401)。BotFather で確認してください")
        print(f"[{ts()}] HTTP {e.code}: {body}", file=sys.stderr)
        if e.code == 429:
            _record_backoff(method, body)
        return None
    except Exception as e:
        print(f"[{ts()}] API error: {e}", file=sys.stderr)
        return None


TELEGRAM_TEXT_LIMIT = 4096      # Bot API の sendMessage 上限(文字数)
SPLIT_MARGIN = 64               # <pre> の閉じ直しと続き表示のための余白


def split_html_message(text, limit=TELEGRAM_TEXT_LIMIT):
    """HTML を壊さずに 4096 文字以内へ分割する。

    order.py の原文をそのまま <pre> で流すため、長い出力は普通に上限を超える。
    超えたまま送ると Telegram は 400 を返し、**メッセージが丸ごと消える**。

    分割は行境界で行う。<pre> の途中で切れる場合は閉じてから次で開き直す
    (タグが閉じていないと parse_mode=HTML が解釈に失敗する)。
    """
    if len(text) <= limit:
        return [text]

    budget = limit - SPLIT_MARGIN
    chunks, current, in_pre = [], [], False

    def flush(open_pre_next):
        if not current:
            return
        body = "\n".join(current)
        if in_pre:
            body += "</pre>"          # 開いたまま切らない
        chunks.append(body)
        current.clear()
        if open_pre_next:
            current.append("<pre>")

    for line in text.split("\n"):
        # 1 行だけで budget を超える場合は行の途中で切る(最後の手段)
        pieces = [line[i:i + budget] for i in range(0, len(line), budget)] or [""]
        for piece in pieces:
            projected = sum(len(c) + 1 for c in current) + len(piece)
            if current and projected > budget:
                was_in_pre = in_pre
                flush(was_in_pre)
            current.append(piece)
            # タグ数の偶奇で <pre> の内外を追う。入れ子は使わない前提。
            in_pre = (in_pre + piece.count("<pre>") + piece.count("</pre>")) % 2 == 1
    flush(False)
    return [c for c in chunks if c.strip()]


def send(cfg, text, keyboard=None, reply_markup=None):
    """常に認証済みチャットIDにだけ返信する(送信先を引数で受けない)。

    4096 文字を超える本文は分割する。キーボードは最後の 1 通にだけ付ける
    (途中のメッセージに付けると、古いボタンが複数残ってしまうため)。

    ``keyboard`` はインラインボタン用。**Mini App の起動ボタンには使えない。**
    Telegram の ``WebApp.sendData()`` は ReplyKeyboard から開いた場合のみ動作し、
    インラインボタンから開くと無言で失敗する(発注が Bot に届かない)。
    Mini App を貼るときは ``reply_markup`` に ``webapp_keyboard()`` を渡すこと。
    """
    parts = split_html_message(text)
    result = None
    for index, part in enumerate(parts):
        params = dict(chat_id=cfg["TELEGRAM_CHAT_ID"],
                      text=part,
                      parse_mode="HTML",
                      disable_web_page_preview="true")
        if index == len(parts) - 1 and (reply_markup or keyboard):
            markup = reply_markup if reply_markup else {"inline_keyboard": keyboard}
            params["reply_markup"] = json.dumps(
                markup, ensure_ascii=False, separators=(",", ":")
            )
        result = api(cfg, "sendMessage", **params)
        if result is None:
            return None          # 途中で失敗したら残りを送らない
    return result


def send_photo(cfg, png_bytes, caption="", reply_markup=None):
    """PNG を sendPhoto で送る(multipart/form-data を手組みする)。

    失敗しても例外にせず None を返す。写真は本文の補助であって、写真が送れない
    ことで本文の通知まで止めてはいけない(呼び出し側でフォールバックする)。
    """
    if not png_bytes:
        return None
    if _blocked_by_backoff("sendPhoto"):
        return None
    boundary = "----nqx" + uuid.uuid4().hex
    crlf = b"\r\n"
    fields = [("chat_id", str(cfg["TELEGRAM_CHAT_ID"])), ("parse_mode", "HTML")]
    if caption:
        fields.append(("caption", caption[:1024]))
    if reply_markup:
        fields.append(("reply_markup", json.dumps(reply_markup, ensure_ascii=False, separators=(",", ":"))))
    body = bytearray()
    for name, value in fields:
        body += f"--{boundary}".encode("utf-8") + crlf
        body += f'Content-Disposition: form-data; name="{name}"'.encode("utf-8") + crlf + crlf
        body += str(value).encode("utf-8") + crlf
    body += f"--{boundary}".encode("utf-8") + crlf
    body += b'Content-Disposition: form-data; name="photo"; filename="scenario.png"' + crlf
    body += b"Content-Type: image/png" + crlf + crlf
    body += bytes(png_bytes) + crlf
    body += f"--{boundary}--".encode("utf-8") + crlf
    url = API.format(token=cfg["TELEGRAM_TOKEN"], method="sendPhoto")
    req = urllib.request.Request(url, data=bytes(body), method="POST",
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(req, timeout=POLL_TIMEOUT + 30) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        print(f"[{ts()}] sendPhoto HTTP {e.code}: {body[:200]}", file=sys.stderr)
        if e.code == 429:
            _record_backoff("sendPhoto", body)
        return None
    except Exception as e:  # noqa: BLE001
        print(f"[{ts()}] sendPhoto error: {e}", file=sys.stderr)
        return None


def mini_app_url(cfg):
    """Mini App の URL を組み立てる。

    keyboard button 起動では Telegram が initData を渡さないため、Worker 側の
    読み取り認証には Bot が発行する短命 launch token を使う。token には
    Telegram の user id と失効時刻しか入っておらず、secret は URL に出ない。
    Cloudflare 未設定なら従来どおり素の URL を返す(既存挙動を壊さない)。
    """
    try:
        return nqx_state.web_app_url(cfg["TELEGRAM_CHAT_ID"])
    except Exception as exc:
        print(f"[{ts()}] launch token を発行できません: {exc}", file=sys.stderr)
        return WEB_APP_URL


def webapp_keyboard(cfg):
    """Return the persistent ReplyKeyboard button for the Mini App."""
    return {
        "keyboard": [[{
            "text": "✦ OPEN NIGHTWATCH",
            "web_app": {"url": mini_app_url(cfg)},
        }]],
        "resize_keyboard": True,
        "is_persistent": True,
        "one_time_keyboard": False,
    }


def send_webapp(cfg, text):
    """Send a message with the Mini App launch button.

    token は短命なので、状態が動いたタイミングで貼り直す。古いキーボードから
    開いても Worker が 401 を返し、Mini App は空状態になる(推測表示はしない)。
    """
    params = dict(chat_id=cfg["TELEGRAM_CHAT_ID"],
                  text=text,
                  parse_mode="HTML",
                  disable_web_page_preview="true",
                  reply_markup=json.dumps(
                      webapp_keyboard(cfg), ensure_ascii=False, separators=(",", ":")
                  ))
    return api(cfg, "sendMessage", **params)


def kb(*rows):
    """[("表示", "callback_data"), ...] の行を inline_keyboard 形式にする。"""
    return [[{"text": t, "callback_data": d} for t, d in row] for row in rows]


# 画面下部に常に出るメニュー。ラベルは英語、意味説明は本文側で日本語にする。
# グリフは Mini App と同じ言語(◉ 市況 / ◆ 状態 / ◇ 監視 / ✕ 破壊的操作)。
MENU = kb(
    [("⟦ ◉ MARKET ⟧", "cmd:/price"), ("⟦ ◆ STATUS ⟧", "cmd:/status")],
    [("⟦ ◇ SCENARIOS ⟧", "cmd:/scenarios"), ("⟦ ✕ FLATTEN ⟧", "ask:flatten")],
    [("⟦ ⌫ DISCARD ⟧", "cmd:/cancel"), ("⟦ ⋯ HELP ⟧", "cmd:/help")],
)


def ts():
    return datetime.now().strftime("%H:%M:%S")


# ---------------------------------------------------------------- order.py 実行

def run_py(script, args):
    """同ディレクトリの python スクリプトを呼び、(成功bool, 出力str) を返す。"""
    cmd = [sys.executable, os.path.join(BASE, script)] + args
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace",
                           timeout=SUBPROCESS_TIMEOUT, cwd=BASE, env=env)
    except subprocess.TimeoutExpired:
        return False, f"{script} がタイムアウトしました({SUBPROCESS_TIMEOUT}秒)"
    out = (p.stdout or "") + (p.stderr or "")
    return p.returncode == 0, out.strip() or "(出力なし)"


def run_order(args):
    """order.py を呼び、(成功bool, 出力str) を返す。"""
    cmd = [sys.executable, ORDER_PY] + args
    # Windows では子プロセスの既定が cp932 になり、order.py の日本語が化ける。
    # 子側にも UTF-8 を明示する(order.py は変更しない)。
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace",
                           timeout=SUBPROCESS_TIMEOUT, cwd=BASE, env=env)
    except subprocess.TimeoutExpired:
        return False, f"order.py がタイムアウトしました({SUBPROCESS_TIMEOUT}秒)"
    stdout = p.stdout or ""
    stderr = p.stderr or ""
    if stderr:
        print(stderr.rstrip(), file=sys.stderr)
    confirmed = "--confirm" in args
    ok = p.returncode == 0 and not (confirmed and stderr)
    out = stdout if stdout else stderr
    return ok, out.strip() or "(出力なし)"


def esc(s):
    """HTML parse_mode 用。order.py の出力をそのまま流すのでエスケープ必須。"""
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def pre(s):
    return f"<pre>{esc(s)}</pre>"


def deck_head(label, marker="⚡"):
    """Telegramで確実に使える等幅ヘッダー。任意フォントはBot APIでは指定できない。"""
    return pre(f"⟦ {marker} NQX // {label} ⟧")


def scenario_callback(order_side, qty, entry, sl, tp=None):
    """シナリオ通知のボタン用データ。クリック後は必ず dry-run だけを行う。"""
    values = [order_side.lower(), str(int(qty)), fmt_price(entry), fmt_price(sl)]
    if tp is not None:
        values.append(fmt_price(tp))
    return "scenario:order:" + ":".join(values)


def scenario_keyboard(order_side, qty, entry, sl, tp=None):
    """通知に添えるシナリオ操作ボタン。送信は dry-run 後の別ボタンで行う。"""
    label = "LONG" if order_side.lower() == "buy" else "SHORT"
    return kb(
        [(f"⟦⚡ PREVIEW {label}⟧", scenario_callback(order_side, qty, entry, sl, tp))],
        [("⟦📡 MARKET⟧", "cmd:/price"), ("⟦🧭 STATUS⟧", "cmd:/status")],
    )


def order_summary(desc):
    """注文条件をスマホで読みやすい一行一項目にする。"""
    fields = [part.strip() for part in desc.split(" / ") if part.strip()]
    return pre("\n".join(fields))


def important_order_lines(out):
    """order.py の原文から、先に目に入れるべき行だけを拾う。

    原文全体は必ず別途表示するため、ここで拾えない情報は失われない。
    """
    lines = []
    for line in out.splitlines():
        stripped = line.strip()
        if (stripped.startswith("リスク:") or
                stripped.startswith("ERROR:") or
                stripped.startswith("[ドライラン]") or
                stripped.startswith("[送信") or
                stripped.startswith("[全決済") or
                stripped.startswith("HTTP ")):
            lines.append(stripped)
    return lines


def order_output(out, label="order.py 原文"):
    """結果を要点→原文の順にする。価格・拒否理由を欠落させない。"""
    parts = []
    important = important_order_lines(out)
    if important:
        parts.append("<b>⟦ HIGHLIGHTS ⟧</b>\n" + pre("\n".join(important)))
    parts.append("<b>⟦ order.py RAW ⟧</b>\n" + pre(out))
    return "\n".join(parts)


def dry_run_output(desc, out):
    return (deck_head("DRY RUN // NOT SENT", "⚡") + "\n"
            "<b>⟦ ORDER ⟧</b>\n" + order_summary(desc) + "\n" +
            order_output(out))


def execution_output(ok, desc, at, out, title="送信"):
    state = "SEND COMPLETE" if ok else "SEND FAILED"
    body = deck_head(state, "🚀" if ok else "🛑") + "\n<b>⟦ ORDER ⟧</b>\n" + order_summary(desc)
    body += f"\n<i>ドライラン: {esc(at)}</i>\n"
    return body + order_output(out)


# ---------------------------------------------------------------- コマンド

HELP = deck_head("COMMAND DECK", "⚡") + """

<b>⟦ INFO ⟧</b>
<code>/price</code> — 最新の市況（PC側の分析結果）
<code>/status</code> — 本日の発注回数（送信記録）
<code>/app</code> — Mini App のボタンを最新ビルドに貼り直す
<code>/sync</code> — ブローカーの実建玉を照会
<code>/position</code> — 保有中のカード（TP1/TP2/SL の到達時損益つき）
   <code>/position demo</code> で見た目だけ確認（作り物）
<code>/result</code> — 直近の決済結果を Mini App に出す
   <code>/result 29796.25</code> で決済価格を明示

<b>⟦ ORDER ⟧</b>
<code>/app</code> — 正本シナリオのTP1/runner分割注文を開く
<code>/order</code> / <code>/market</code> — 廃止済み。Mini Appへ誘導
<code>/confirm</code> — 保有建玉の変更ドライランだけを確定
<code>/cancel</code> — 保留中のドライランを破棄
<code>/modify buy 2 29760 29796</code> — SL/TP張り替え
   side qty sl tp（tp省略可）
<code>/flatten</code> — <b>FLATTEN</b>
<code>/log N</code> — 記録からN番目を削除

<b>⟦ SAFETY ⟧</b>
新規発注はMini Appの正本シナリオからのみ実行します。
固定2枚・TP1/runner・execution claimが揃わない新規経路は拒否されます。
数値は価格そのもの（pt幅ではありません）。"""

SCENARIOS = deck_head("SCENARIO SIGNAL", "⚡") + """

シナリオ通知が届いたら、本文の <code>PREVIEW LONG</code> または
<code>PREVIEW SHORT</code> ボタンを押してください。
コマンドをコピーして貼り付ける必要はありません。

PREVIEW は確認用のドライランだけを作成します。
実際の送信は次の <code>🚀 CONFIRM &amp; SEND</code> ボタンを押すまで行われません。"""


def parse_nums(parts, names):
    """['buy','2','29760'] → ('buy', 2, [29760.0]) 形式に。失敗時は例外。"""
    vals = []
    for i, name in enumerate(names):
        try:
            vals.append(float(parts[i]))
        except (IndexError, ValueError):
            raise ValueError(f"{name} が不正です")
    return vals


def cmd_order(parts, market=False):
    """/order side qty entry sl [tp]  |  /market side qty sl last"""
    if len(parts) < 2:
        raise ValueError("引数が足りません")
    side = parts[0].lower()
    if side not in ("buy", "sell"):
        raise ValueError(f"side は buy か sell です(指定: {side})")

    try:
        qty = int(parts[1])
    except ValueError:
        raise ValueError(f"qty が不正です: {parts[1]}")
    if qty < 1:
        raise ValueError(f"qty は 1 以上です(指定: {qty})")

    argv = ["--side", side, "--qty", str(qty)]

    if market:
        # last(今チャートで見えている現在値)を必ず打たせる。order.py が
        # (|現在値 − SL| + 滑り緩衝) で概算し、口座別上限を判定する。
        if len(parts) < 4:
            raise ValueError("使い方: /market side qty sl last(現在値)\n"
                             "例: /market buy 1 29700 29726.5\n"
                             "現在値は /price か Mini App のチャートで確認")
        sl, last = parse_nums(parts[2:4], ["sl", "last"])
        argv += ["--market", "--sl", fmt_price(sl), "--last", fmt_price(last)]
        desc = f"MARKET {side.upper()} {qty}枚 / SL {sl:,.2f} / 現在値 {last:,.2f}"
    else:
        if len(parts) < 4:
            raise ValueError("使い方: /order side qty entry sl [tp]")
        entry, sl = parse_nums(parts[2:4], ["entry", "sl"])
        argv += ["--entry", fmt_price(entry), "--sl", fmt_price(sl)]
        desc = f"{side.upper()} {qty}枚 / Entry {entry:,.2f} / SL {sl:,.2f}"
        if len(parts) >= 5:
            tp, = parse_nums(parts[4:5], ["tp"])
            argv += ["--tp", fmt_price(tp)]
            desc += f" / TP {tp:,.2f}"
    return argv, desc


def cmd_modify(parts):
    """/modify side qty sl [tp]"""
    if len(parts) < 3:
        raise ValueError("使い方: /modify side qty sl [tp]")
    side = parts[0].lower()
    if side not in ("buy", "sell"):
        raise ValueError(f"side は buy か sell です(指定: {side})")
    try:
        qty = int(parts[1])
    except ValueError:
        raise ValueError(f"qty が不正です: {parts[1]}")
    if qty < 1:
        raise ValueError(f"qty は 1 以上です(指定: {qty})")
    sl, = parse_nums(parts[2:3], ["sl"])
    argv = ["--modify", "--side", side, "--qty", str(qty), "--sl", fmt_price(sl)]
    desc = f"MODIFY {side.upper()} {qty}枚 / SL {sl:,.2f}"
    if len(parts) >= 4:
        tp, = parse_nums(parts[3:4], ["tp"])
        argv += ["--tp", fmt_price(tp)]
        desc += f" / TP {tp:,.2f}"
    return argv, desc


def live_orders_enabled(cfg):
    """Require an explicit local switch before any --confirm subprocess."""
    return str(cfg.get("NQX_LIVE_ORDERS", "0")).strip().lower() in {
        "1", "true", "yes", "on"
    }


def do_confirm(cfg):
    """Send the pending dry-run only after the explicit live-route gate."""
    if not PENDING["argv"]:
        return response("保留中の変更ドライランがありません。新規は /app から実行してください", MENU)
    if "--modify" not in PENDING["argv"]:
        clear_pending()
        return _order_blocked(
            "旧Telegram新規経路は廃止されました",
            "Mini Appのauthoritative one-clickを使用してください。注文は送信していません",
        )[0]
    if not live_orders_enabled(cfg):
        return response(
            deck_head("LIVE ROUTE LOCKED", "🛡️") + "\n"
            "<b>注文送信はロック中です。</b>\n"
            "`.secrets/telegram.env` に <code>NQX_LIVE_ORDERS=1</code> を設定し、"
            "Botを再起動してから、もう一度 <b>CONFIRM &amp; SEND</b> を押してください。\n"
            "この保留注文は送信していません。",
            MENU,
        )
    # The only remaining Telegram confirm path is MODIFY.  It must target an
    # open position, so the new-entry FLAT preflight is inapplicable here.
    # order.py performs the authoritative pre/post broker identity checks and
    # refuses the route if that truth is unavailable.
    argv, desc, at = list(PENDING["argv"]), PENDING["desc"], PENDING["at"]
    frozen = PENDING.get("managementIntent")
    if not isinstance(frozen, dict):
        clear_pending()
        return _order_blocked("MODIFY frozen broker identity is missing")[0]
    observed = broker_status.query_position(str(frozen.get("symbol") or NQX_SYMBOL))
    current_generation = broker_status.position_identity(observed)
    try:
        current = management_intent.build(
            account_id=observed.get("accountId") or observed.get("account"),
            symbol=observed.get("symbol"), position_generation=current_generation,
            side=frozen.get("side"), qty=observed.get("qty"),
            stop=frozen.get("stop"), target=frozen.get("target"))
    except (AttributeError, ValueError) as exc:
        clear_pending()
        return _order_blocked(f"MODIFY broker truth changed/unavailable: {exc}")[0]
    if current != frozen:
        clear_pending()
        return _order_blocked("MODIFY position generation/account/qty changed after dry-run")[0]
    try:
        claim_ok, claim = nqx_state.claim_management(frozen)
    except Exception as exc:  # noqa: BLE001
        claim_ok, claim = False, {"reason": f"{type(exc).__name__}: {exc}"}
    if (not claim_ok or not isinstance(claim, dict) or not claim.get("claimToken")
            or claim.get("managementKey") != management_intent.management_key(frozen)):
        clear_pending()
        return _order_blocked(f"MANAGEMENT claim unavailable: {claim}")[0]
    argv += ["--management-key", str(claim["managementKey"]),
             "--management-token", str(claim["claimToken"]),
             "--management-intent-hash", str(claim["managementIntentHash"])]
    management_lock_key = _set_management_server_lock({**claim, "managementIntent": frozen})
    clear_pending()                          # CAS取得後、送信前にローカル再押下を無効化
    ok, out = run_order(argv + ["--confirm"])
    if ok:
        _release_server_order_lock(management_lock_key)
    return response(execution_output(ok, desc, at, out), MENU)


def do_flatten():
    clear_pending()
    ok, out = run_order(["--flatten", "--confirm"])
    state = "[FLATTEN COMPLETE]" if ok else "[FLATTEN FAILED]"
    return response(deck_head(state, "💥" if ok else "🛑") + "\n" + order_output(out), MENU)


def do_status():
    ok, out = run_order(["--status"])
    if PENDING["argv"]:
        body = (deck_head("STATUS", "🧭") + "\n" + pre(out) +
                "\n<b>⟦ ⏳ PENDING ORDER / NOT SENT ⟧</b>\n" + order_summary(PENDING["desc"]))
        return response(body, kb(
            [("⟦🚀 CONFIRM &amp; SEND⟧", f"go:{PENDING['token']}"), ("⟦🧹 DISCARD⟧", "cmd:/cancel")],
            [("⟦🛠 HELP⟧", "cmd:/help")]))
    return response(deck_head("STATUS", "🧭") + "\n" + pre(out), MENU)


def handle(cfg, text):
    """1メッセージを処理して (返信文字列, キーボード) を返す。"""
    parts = text.strip().split()
    if not parts:
        return response("", None)
    cmd = parts[0].lower()
    if "@" in cmd:                      # /status@MyBot 形式
        cmd = cmd.split("@", 1)[0]
    args = parts[1:]

    # ---- 情報系 ----
    if cmd in ("/start", "/help"):
        return response(HELP, MENU)

    if cmd == "/scenarios":
        return response(SCENARIOS, MENU)

    if cmd == "/status":
        return do_status()

    if cmd == "/app":
        # 常設キーボードは「最後に reply_markup を送ったメッセージ」の内容で固定される。
        # ビルドを差し替えても、貼り直さない限りボタンは古い URL を指したままになる。
        url = mini_app_url(cfg)
        version = nqx_state.app_version()
        masked = re.sub(r"(t=)[^&]+", r"\1***", url) if "t=" in url else url
        lines = [deck_head("MINI APP RELOADED", "✦"),
                 "<b>下の ✦ OPEN NIGHTWATCH を貼り直しました。</b>",
                 f"build: <code>{esc(version or '不明(dist が未ビルド)')}</code>"]
        if "api=" in url:
            lines.append("state service: <b>接続あり</b>")
        else:
            lines.append("state service: <b>未設定</b> — アプリは "
                         "<code>NO STATE SERVICE</code> と表示します\n"
                         "<code>.secrets/nqx_cloud.env</code> を作成してください")
        lines.append(f"\n<code>{esc(masked)}</code>")
        send_webapp(cfg, "\n".join(lines))
        return response("", None)

    if cmd == "/result":
        # 決済結果を Mini App に出す。発注はしない。
        # 引数があればそれを実際の決済価格として使う(exitSource=manual)。
        manual = None
        if args:
            try:
                manual = normalize_tick(_webapp_number(args[0], "exit"))
            except ValueError:
                return response("使い方: <code>/result</code> または <code>/result 29796.25</code>", MENU)
        published, note = publish_trade_result(cfg, manual_exit=manual)
        title = deck_head("RESULT PUBLISHED", "🧾") if published else deck_head("RESULT NOT PUBLISHED", "🛑")
        return response(title + "\n" + note, MENU)

    if cmd == "/sync":
        # ブローカーへ照会し、その結果を state 側へ反映する。発注はしない。
        result = broker_status.query_position(NQX_SYMBOL)
        _sync_position_quiet(cfg)
        if result.get("verified"):
            if int(result.get("qty") or 0) == 0:
                summary = f"FLAT ({result['source']})"
            else:
                summary = f"{result['side']} {result['qty']}枚 @ {result.get('avgEntry')} ({result['source']})"
        else:
            summary = "UNVERIFIED — 建玉を確認できません(既存カードは STALE のまま残ります)\n" \
                      + str(result.get("detail", ""))
        return response(deck_head("BROKER POSITION", "🧾") + "\n" + pre(summary), MENU)

    if cmd in ("/position", "/card"):
        # 保有中のカードを今すぐ1枚出す。照会だけで、発注も変更もしない。
        # 引数 demo は建玉が無くても見た目を確認するための作り物(送信時に明示)。
        import position_card

        demo = bool(args) and args[0].lower() == "demo"
        try:
            if demo:
                parts = position_card.demo_parts()
                derived = position_card.derive_open(
                    parts["position"], parts["plan"], parts["last"],
                    accounts=parts["accounts"])
                png = position_card.render_png(
                    parts["position"], parts["plan"], parts["last"],
                    bars=parts["bars"], accounts=parts["accounts"],
                    note="DEMO · not a real position")
                text = "DEMO — 実建玉ではありません\n" + position_card.caption(derived)
            else:
                png, text, _ = position_card.render_open_card()
        except position_card.NoCard as exc:
            return response(deck_head("NO OPEN POSITION", "🫥") + "\n" + pre(str(exc)), MENU)
        except Exception as exc:  # noqa: BLE001 - 描けない理由をそのまま返す
            return response(deck_head("POSITION CARD FAILED", "🛑")
                            + "\n" + pre(f"{type(exc).__name__}: {exc}"), MENU)
        if not send_photo(cfg, png, caption=text):
            return response(deck_head("POSITION CARD NOT SENT", "🛑") + "\n" + pre(text), MENU)
        return response("", None)

    if cmd == "/price":
        ok, out = run_py("snapshot.py", [])
        title = deck_head("MARKET SNAPSHOT", "📡") if ok else deck_head("MARKET SNAPSHOT FAILED", "🛑")
        return response(title + "\n" + pre(out), MENU)

    if cmd == "/log":
        if not args or not args[0].isdigit():
            return response("使い方: <code>/log N</code>（N番目を記録から削除）", MENU)
        ok, out = run_order(["--cancel", args[0]])
        title = deck_head("LOG ENTRY REMOVED", "🧹") if ok else deck_head("LOG REMOVE FAILED", "🛑")
        return response(title + "\n" + order_output(out), MENU)

    # ---- 全決済(ユーザー選択により確認なし即実行)----
    if cmd == "/flatten":
        return do_flatten()

    # ---- 保留の破棄 ----
    if cmd == "/cancel":
        if not PENDING["argv"]:
            return response("保留中のドライランはありません", MENU)
        desc = PENDING["desc"]
        clear_pending()
        return response(deck_head("DRY RUN DISCARDED", "🧹") + "\n" + order_summary(desc), MENU)

    # ---- 送信 ----
    if cmd == "/confirm":
        return do_confirm(cfg)

    # ---- SL を建値へ(ボタン用ショートカット)----
    if cmd == "/be":
        return response(("使い方: <code>/be side qty 建値</code>\n"
                         "例: <code>/be buy 2 29760</code>\n"
                         "SL を建値に動かすドライランを出します"), MENU)

    # ---- ドライラン ----
    if cmd in ("/order", "/market"):
        clear_pending()
        return response(
            deck_head("AUTHORITATIVE ENTRY ONLY", "🛡️") + "\n"
            "<b>Telegramテキスト新規は廃止されました。</b>\n"
            "<code>/app</code> から最新の正本シナリオを開き、固定2枚・TP1/runnerの"
            " one-click を使用してください。\n<b>注文は送信していません</b>", MENU)

    if cmd == "/modify":
        try:
            argv, desc = cmd_modify(args)
        except ValueError as e:
            return response(f"<b>入力エラー</b>\n{esc(str(e))}\n\n{HELP}", MENU)

        observed = broker_status.query_position(NQX_SYMBOL)
        generation = broker_status.position_identity(observed)
        try:
            expected_side = "BUY" if str(observed.get("side") or "").upper() == "LONG" else "SELL"
            parsed_side = argv[argv.index("--side") + 1].upper()
            parsed_qty = int(argv[argv.index("--qty") + 1])
            parsed_sl = argv[argv.index("--sl") + 1]
            parsed_tp = argv[argv.index("--tp") + 1]
            if (observed.get("verified") is not True or expected_side != parsed_side
                    or int(observed.get("qty") or 0) != parsed_qty):
                raise ValueError("broker position is unavailable or side/qty changed")
            frozen = management_intent.build(
                account_id=observed.get("accountId") or observed.get("account"),
                symbol=observed.get("symbol") or NQX_SYMBOL,
                position_generation=generation, side=parsed_side, qty=parsed_qty,
                stop=parsed_sl, target=parsed_tp)
        except (ValueError, IndexError) as exc:
            clear_pending()
            return _order_blocked(f"MODIFY dry-run cannot freeze exact broker identity: {exc}")[0]
        argv += ["--position-generation", generation]
        ok, out = run_order(argv)              # --confirm なし = 送信されない
        if not ok:
            clear_pending()
            return response(deck_head("SEND FAILED", "🛑") +
                            "\n<b>order.py が拒否しました</b>\n" + order_output(out), MENU)

        token = new_token()
        PENDING.update({"argv": argv, "desc": desc, "at": ts(), "token": token,
                        "managementIntent": frozen})
        body = dry_run_output(desc, out)
        return response(body, kb([("⟦🚀 CONFIRM &amp; SEND⟧", f"go:{token}"), ("⟦🧹 DISCARD⟧", "cmd:/cancel")]))

    return response(f"<b>不明なコマンド</b>: {esc(cmd)}\n\n{HELP}", MENU)


def handle_callback(cfg, data):
    """ボタンが押されたときの処理。(返信, キーボード, 通知文) を返す。"""
    # 送信ボタン: token が現在の PENDING と一致する時だけ通す
    if data.startswith("go:"):
        token = data[3:]
        if not PENDING["argv"]:
            return "保留中のドライランはありません(送信済みか破棄済み)", MENU, "対象なし"
        if token != PENDING["token"]:
            # 古いメッセージのボタン or 二重タップ
            return None, None, "古いボタンです。無視しました"
        body, keyboard = do_confirm(cfg)
        return body, keyboard, "送信しました"

    # 旧callbackにはclaim/evidence hash/split planが無いので新規経路に入れない。
    if data.startswith("scenario:order:"):
        clear_pending()
        return (deck_head("AUTHORITATIVE ENTRY ONLY", "🛡️") + "\n"
                "旧PREVIEWボタンは失効しました。<code>/app</code> から最新シナリオを開いてください。",
                MENU, "旧新規経路は廃止済み")

    # 決済結果の1タップ記録。押された価格を manual として publish する。
    # ボタンに載る価格は _suggest_exit_price の検証済み現在値のみ。
    if data.startswith("result:"):
        try:
            manual = normalize_tick(float(data[len("result:"):].replace(",", "")))
            if manual is None or not math.isfinite(manual):
                raise ValueError
        except (TypeError, ValueError):
            return "無効な記録ボタンです", MENU, "無効な記録ボタン"
        published, note = publish_trade_result(cfg, manual_exit=manual)
        title = deck_head("RESULT PUBLISHED", "🧾") if published else deck_head("RESULT NOT PUBLISHED", "🛑")
        return title + "\n" + note, MENU, ("記録しました" if published else "記録できませんでした")

    # 全決済は確認を挟む(タップ1回で全部消えるのを防ぐ)
    if data == "ask:flatten":
        return (deck_head("FLATTEN POSITION?", "💥") +
                "\n保有ポジションをすべて成行で閉じます。",
                kb([("⟦💥 EXECUTE FLATTEN⟧", "do:flatten"), ("⟦↩ BACK⟧", "cmd:/status")]),
                None)

    if data == "do:flatten":
        body, keyboard = do_flatten()
        return body, keyboard, "全決済を送信"

    if data.startswith("cmd:"):
        body, keyboard = handle(cfg, data[4:])
        return body, keyboard, None

    return None, None, "不明なボタン"


# ---------------------------------------------------------------- メインループ

def _webapp_number(value, field):
    """Parse a finite price/quantity value from Mini App JSON."""
    try:
        number = float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        raise ValueError(f"{field} is invalid")
    if not math.isfinite(number):
        raise ValueError(f"{field} is invalid")
    return number


def fmt_price(value):
    """Preserve CME quarter-tick prices when building argv/commands."""
    return f"{float(value):.2f}"


# ---------------------------------------------------------------- Mini App 一回押し発注

def normalize_tick(value):
    """CME の 0.25 tick に正規化する。以後の判定は必ずこの戻り値だけで行う。"""
    return round(float(value) / TICK) * TICK


def _parse_iso(value, field):
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        raise ValueError(f"{field} is not a timestamp")
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _load_order_keys():
    try:
        with open(ORDER_KEY_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def consume_order_key(key, detail):
    """idempotency key を一回限りとして消費する。

    プロセス再起動後も効くようにファイルへ落とす。ボタンの二重タップ、
    Telegram の再送、古いメッセージからの再実行はすべてここで止まる。
    戻り値 True = 今回が初回。False = 既に使用済み。
    """
    now = time.time()
    keys = {k: v for k, v in _load_order_keys().items()
            if now - float(v.get("at", 0)) < ORDER_KEY_TTL_SEC}
    if key in keys:
        return False
    keys[key] = {"at": now, "detail": detail}
    os.makedirs(os.path.dirname(ORDER_KEY_FILE), exist_ok=True)
    with open(ORDER_KEY_FILE, "w", encoding="utf-8") as fh:
        json.dump(keys, fh, ensure_ascii=False, indent=2)
    return True


def _server_order_key(scenario):
    """Broker idempotency identity derived only from the server scenario."""
    if not isinstance(scenario, dict):
        return None
    fields = ("scenarioId", "fingerprint", "evidenceHash", "marketCycleId")
    payload = {field: str(scenario.get(field) or "").strip() for field in fields}
    if any(not value for value in payload.values()):
        return None
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "ENTRY:" + hashlib.sha256(canonical).hexdigest()


def _server_lock_path():
    # Follow ORDER_KEY_FILE dynamically so hermetic tests can redirect both.
    return ORDER_KEY_FILE + ".server-tuples"


def _load_server_order_locks():
    try:
        with open(_server_lock_path(), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_server_order_locks(locks):
    path = _server_lock_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(locks, fh, ensure_ascii=False, separators=(",", ":"))
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _set_server_order_lock(key, state, scenario, claim_token=None):
    locks = _load_server_order_locks()
    previous = locks.get(key) if isinstance(locks.get(key), dict) else {}
    locks[key] = {
        "state": str(state).upper(),
        "scenarioId": str((scenario or {}).get("scenarioId") or ""),
        "fingerprint": str((scenario or {}).get("fingerprint") or ""),
        "evidenceHash": str((scenario or {}).get("evidenceHash") or ""),
        "marketCycleId": str((scenario or {}).get("marketCycleId") or ""),
        "at": time.time(),
        "claimToken": claim_token or previous.get("claimToken"),
    }
    _save_server_order_locks(locks)


def _set_management_server_lock(claim):
    key = f"MGMTLOCK:{claim.get('managementKey')}"
    locks = _load_server_order_locks()
    locks[key] = {
        "kind": "MANAGEMENT", "state": "UNKNOWN",
        "managementKey": str(claim.get("managementKey") or ""),
        "claimToken": str(claim.get("claimToken") or ""),
        "managementIntentHash": str(claim.get("managementIntentHash") or ""),
        "managementIntent": claim.get("managementIntent"),
        "at": time.time(),
    }
    _save_server_order_locks(locks)
    return key


def reconcile_startup_claims(*, state_fetch=None, position_query=None,
                             orders_query=None, entry_recover=None,
                             management_recover=None):
    """Production startup reconciliation; all broker operations are read-only."""
    state_fetch = state_fetch or nqx_state.fetch_state_quiet
    position_query = position_query or broker_status.query_position
    orders_query = orders_query or broker_status.query_orders
    view = state_fetch()
    if not isinstance(view, dict):
        return ["startup recovery: authoritative state unavailable"]
    notes = []
    locks = _load_server_order_locks()
    for local_key, lock in list(locks.items()):
        if not isinstance(lock, dict) or str(lock.get("state") or "").upper() not in {
                "PREPARING", "UNKNOWN"}:
            continue
        if lock.get("kind") == "MANAGEMENT":
            claim = view.get("managementClaim")
            if (not isinstance(claim, dict)
                    or claim.get("managementKey") != lock.get("managementKey")
                    or claim.get("managementIntentHash") != lock.get("managementIntentHash")):
                notes.append("startup recovery: MANAGEMENT journal mismatch")
                continue
            recovery = management_recover or nqx_state.recover_management_from_broker
            ok, detail = recovery(
                lock["managementKey"], lock.get("claimToken"), claim,
                position_query=position_query, orders_query=orders_query)
            if ok:
                _release_server_order_lock(local_key)
                notes.append("startup recovery: MANAGEMENT recovered")
            else:
                notes.append(f"startup recovery: MANAGEMENT locked ({detail})")
            continue
        claim = view.get("entryClaim")
        if not isinstance(claim, dict) or claim.get("entryKey") != local_key:
            notes.append("startup recovery: ENTRY journal mismatch")
            continue
        symbol = str((claim.get("executionIntent") or {}).get("symbol") or NQX_SYMBOL)
        known_ids = [row.get("orderId") for row in claim.get("routeSnapshot") or []
                     if isinstance(row, dict) and row.get("orderId")]
        position = position_query(symbol)
        try:
            order_view = orders_query(symbol, known_order_ids=known_ids)
        except TypeError:
            order_view = orders_query(symbol)
        recovery = entry_recover or recover_unknown_server_lock
        ok, detail = recovery(
            local_key, position, order_view,
            position_query=position_query, orders_query=orders_query)
        notes.append("startup recovery: ENTRY recovered" if ok
                     else f"startup recovery: ENTRY locked ({detail})")
    return notes


def recover_unknown_server_lock(key, broker_position, broker_order, *,
                                position_query=None, orders_query=None):
    """Explicitly recover PREPARING/UNKNOWN only from verified broker truth.

    No TTL path exists.  A caller must supply verified FLAT and a verified
    nonblocking/terminal broker-order observation for the same local tuple.
    """
    lock = _load_server_order_locks().get(key)
    if not isinstance(lock, dict) or str(lock.get("state") or "").upper() not in {"PREPARING", "UNKNOWN"}:
        return False, "local claim is not recoverable"
    if (not isinstance(broker_position, dict) or broker_position.get("verified") is not True
            or int(broker_position.get("qty") or 0) != 0):
        return False, "broker position is not verified flat"
    state = str((broker_order or {}).get("state") or "UNKNOWN").upper()
    if (not isinstance(broker_order, dict) or broker_order.get("verified") is not True
            or state in set(execution_contract.CONTRACT["blockingOrderStates"])):
        return False, "broker order is not verified terminal/nonblocking"
    token = lock.get("claimToken")
    if not token:
        return False, "local claim token is unavailable"
    view = nqx_state.fetch_state_quiet()
    claim = view.get("entryClaim") if isinstance(view, dict) else None
    if (not isinstance(claim, dict) or str(claim.get("entryKey") or "") != key
            or not claim.get("executionIntentHash")):
        return False, "authoritative claim journal is unavailable"
    frozen = claim.get("routeSnapshot") if isinstance(claim.get("routeSnapshot"), list) else []
    frozen_ids = {str(row.get("orderId")) for row in frozen
                  if isinstance(row, dict) and row.get("state") == "ACCEPTED" and row.get("orderId")}
    broker_rows = [row for row in (broker_order.get("orders") or []) if isinstance(row, dict)]
    observed_ids = {str(row.get("orderId")) for row in broker_rows if row.get("orderId")}
    if not frozen_ids or not frozen_ids.issubset(observed_ids):
        return False, "broker terminal proof does not cover frozen route ids"
    # R22 recovery never commits a caller-supplied stale observation.  Query a
    # fresh position->orders->position transaction and bind its stable hash to
    # the Durable Object CAS.  Injected callables keep tests fully offline.
    position_query = position_query or broker_status.query_position
    orders_query = orders_query or broker_status.query_orders
    symbol = str((claim.get("executionIntent") or {}).get("symbol") or contract_month.symbol())
    current_before = position_query(symbol)
    current_orders = orders_query(symbol, known_order_ids=sorted(frozen_ids))
    current_after = position_query(symbol)
    if (not isinstance(current_before, dict) or current_before.get("verified") is not True
            or not isinstance(current_after, dict) or current_after.get("verified") is not True
            or int(current_before.get("qty") or 0) != 0
            or int(current_after.get("qty") or 0) != 0
            or not isinstance(current_orders, dict) or current_orders.get("verified") is not True):
        return False, "current broker recovery snapshot is not verified flat/terminal"
    current_rows = [row for row in current_orders.get("orders") or [] if isinstance(row, dict)]
    terminal = set(execution_contract.CONTRACT["brokerObservation"]["terminalStates"])
    if not all(len([row for row in current_rows
                    if str(row.get("orderId") or "") == order_id
                    and str(row.get("status") or "").upper() in terminal]) == 1
               for order_id in frozen_ids):
        return False, "current broker recovery snapshot lacks terminal frozen ids"
    observed_ok, observed_detail = nqx_state.publish_broker_observation(
        current_after, current_orders, claim["executionIntentHash"],
        before_position=current_before, after_position=current_after)
    if not observed_ok:
        return False, observed_detail
    snapshot = observed_detail.get("observation") if isinstance(observed_detail, dict) else None
    ok, detail = nqx_state.recover_entry_claim(key, token, snapshot)
    if not ok:
        return False, detail
    _release_server_order_lock(key)
    return True, detail


def _release_server_order_lock(key):
    locks = _load_server_order_locks()
    if key in locks:
        del locks[key]
        _save_server_order_locks(locks)


def _server_order_lock_active(key, view):
    """Keep PENDING/SENT/PARTIAL/UNKNOWN locked until a matching terminal server view."""
    lock = _load_server_order_locks().get(key)
    if not isinstance(lock, dict):
        return False
    order = view.get("order") if isinstance(view, dict) else None
    terminal = {"FILLED", "REJECTED", "CANCELED"}
    if (isinstance(order, dict)
            and str(order.get("state") or "").upper() in terminal
            and str(order.get("idempotencyKey") or "") == key
            and str(order.get("scenarioId") or "") == str(lock.get("scenarioId") or "")):
        _release_server_order_lock(key)
        return False
    return True


def classify_send_result(ok, out, expected_accounts=None):
    """order.py の出力から「本当に送れたのか」を判定する。

    ドライランの成功は約定でも送信成功でもない。下流が 2xx を返したことを
    確認できない限り SENT と呼ばない。判定できない場合は UNKNOWN にして、
    必ずブローカー確認を促す。
    """
    parsed = route_envelope.parse(out, expected_accounts=expected_accounts)
    if not parsed.get("ok"):
        return "UNKNOWN", None
    state = parsed["state"]
    receipt = parsed["final"]
    if state == "SENT" and ok:
        return "SENT", receipt
    if state == "REJECTED":
        return "REJECTED", receipt
    if state == "PARTIAL":
        return "PARTIAL", receipt
    return "UNKNOWN", receipt


def daily_order_count():
    """order.py --status から当日の発注回数を読む。読めなければ None。"""
    ok, out = run_order(["--status"])
    if not ok:
        return None, out
    match = re.search(r"発注\s*(\d+)\s*/\s*(\d+)\s*回", out)
    if not match:
        return None, out
    return (int(match.group(1)), int(match.group(2))), out


def preflight_position(cfg=None):
    """発注前に建玉の有無を確認する。

    優先順位はブローカー照会 → Durable Object の最終確認状態。
    どちらも取れないときは必ず止める。未確認を「建玉なし」と解釈しない。
    戻り値 (blocked: bool, reason: str|None, evidence: str)。
    """
    result = broker_status.query_position(NQX_SYMBOL)
    if result.get("verified"):
        qty = int(result.get("qty") or 0)
        if qty > 0:
            return True, f"POSITION OPEN — {result.get('side')} {qty} 枚 ({result['source']})", result["source"]
        return False, None, result["source"]

    view = nqx_state.fetch_state_quiet()
    if view is not None:
        position = view.get("position")
        if position:
            return True, (f"POSITION {position.get('state')} — ブローカー照会不能のため"
                          "既存建玉を否定できません"), "durable-object"
        return True, ("POSITION UNVERIFIED — ブローカー照会が未確認です。"
                      "建玉なしとは判断しません"), "durable-object"

    return True, ("POSITION UNVERIFIED — ブローカー照会も state 照会もできていません。"
                  "ライブ発注を停止しました"), "unavailable"


def _order_blocked(reason, note=None):
    body = (deck_head("ORDER BLOCKED", "🛑") + "\n<b>" + esc(reason) + "</b>\n")
    if note:
        body += esc(note) + "\n"
    body += "<b>注文は送信していません</b>"
    return response(body, MENU), reason


def _authoritative_cycle_seal(view):
    """Revalidate the Worker cycle seal for every manual/AUTO order request."""
    if not isinstance(view, dict):
        return False, "CYCLE_MISMATCH authoritative state unavailable"
    display = view.get("display")
    market, scenario = view.get("market"), view.get("scenario")
    if (not isinstance(display, dict) or display.get("orderable") is not True
            or display.get("cyclePaired") is not True):
        return False, "CYCLE_MISMATCH authoritative cycle is not orderable"
    if not isinstance(market, dict) or not isinstance(scenario, dict):
        return False, "CYCLE_MISMATCH market/scenario missing"
    if market.get("cycleCommitted") is not True or scenario.get("cycleCommitted") is not True:
        return False, "CYCLE_MISMATCH cycle commits missing"
    if not market.get("cycleId") or str(market.get("cycleId")) != str(scenario.get("marketCycleId") or ""):
        return False, "CYCLE_MISMATCH cycle ids differ"
    if "strategyEvidence" in scenario:
        return False, "CYCLE_MISMATCH scenario must be hash-only"
    try:
        evidence = strategy_evidence.canonicalize(market.get("strategyEvidence"))
    except (TypeError, ValueError):
        evidence = None
    if not evidence or market.get("strategyEvidenceError"):
        return False, "CYCLE_MISMATCH canonical market evidence unavailable"
    if str(scenario.get("evidenceHash") or "") != str(evidence.get("evidenceHash") or ""):
        return False, "CYCLE_MISMATCH scenario evidence hash differs"
    return True, None


def _authoritative_scenario_gate(submitted, cfg=None, now=None):
    """Resolve every order from the current Durable Object scenario, fail closed."""
    try:
        view = nqx_state.fetch_state_quiet()
    except Exception:
        view = None
    if not isinstance(view, dict):
        return False, None, "latest NQX state is unavailable"
    authoritative = view.get("scenario")
    if not isinstance(authoritative, dict):
        return False, None, "no active authoritative scenario"
    display = view.get("display")
    if not isinstance(display, dict) or display.get("orderable") is not True:
        return False, None, (display.get("blockReason") if isinstance(display, dict)
                             else "authoritative order gate is unavailable")
    sealed, seal_reason = _authoritative_cycle_seal(view)
    if not sealed:
        return False, None, seal_reason

    # All manual and AUTO paths evaluate this pure contract before extracting
    # executable prices.  It holds a same-request view for ONE-PASS so that
    # the marker verification cannot refetch a different state.
    contract = execution_contract.evaluate(
        authoritative, view.get("market"), view.get("position"), view.get("order"),
        now=now, cfg=cfg,
    )
    # Account routing scope is sealed by the Durable Object scenario.  The
    # Telegram env intentionally does not own CrossTrade account ids, so its
    # local contract evaluation must not erase or replace that server truth.
    frozen_execution = (authoritative.get("executionContract")
                        if isinstance(authoritative.get("executionContract"), dict) else {})
    frozen_scope = frozen_execution.get("accountScope")
    if isinstance(frozen_scope, list) and frozen_scope:
        contract["accountScope"] = list(frozen_scope)
    if not contract["orderable"]:
        return False, None, "execution contract: " + ", ".join(contract["blockers"])

    state = str(authoritative.get("state") or "").upper()
    if state not in {"ACTIVE", "ARMED"}:
        return False, None, f"authoritative scenario state {state or 'UNKNOWN'} is not orderable"
    grade = str(authoritative.get("grade") or "").upper()
    if grade not in {"A", "A+", "B"}:  # 2026-09-04: B も発注可能(ユーザー決定)
        return False, None, f"authoritative scenario grade {grade or 'UNKNOWN'} is not orderable"
    if contract.get("effectiveGrade") != grade:
        return False, None, "execution contract: CVD_A_PLUS_CAPPED"

    for field in ("scenarioId", "fingerprint", "evidenceHash", "symbol"):
        if str(submitted.get(field) or "") != str(authoritative.get(field) or ""):
            return False, None, f"authoritative {field} mismatch"
    if str(submitted.get("side") or "").upper() != str(authoritative.get("side") or "").upper():
        return False, None, "authoritative side mismatch"
    try:
        if int(submitted.get("qty")) != int(authoritative.get("qty")):
            return False, None, "authoritative qty mismatch"
        for field in ("entry", "stop", "target"):
            if normalize_tick(_webapp_number(submitted.get(field), field)) != normalize_tick(
                    _webapp_number(authoritative.get(field), field)):
                return False, None, f"authoritative {field} mismatch"
        for field in ("issuedAt", "expiresAt"):
            if _parse_iso(submitted.get(field), field) != _parse_iso(authoritative.get(field), field):
                return False, None, f"authoritative {field} mismatch"
    except (TypeError, ValueError):
        return False, None, "submitted scenario has invalid authoritative fields (価格が不正)"

    # State, grade, and orderability are echoed by the Mini App so a stale
    # card cannot silently ride a newer server scenario with a different gate.
    if str(submitted.get("state") or "").upper() != state:
        return False, None, "authoritative state mismatch"
    if str(submitted.get("grade") or "").upper() != grade:
        return False, None, "authoritative grade mismatch"
    if submitted.get("orderable") is not True:
        return False, None, "submitted scenario is not marked orderable"

    auth_targets = authoritative.get("targets")
    client_targets = submitted.get("targets")
    if not isinstance(auth_targets, list) or not isinstance(client_targets, list) or len(auth_targets) != len(client_targets):
        return False, None, "authoritative targets mismatch"
    try:
        if [normalize_tick(_webapp_number(value, "targets")) for value in client_targets] != [
                normalize_tick(_webapp_number(value, "targets")) for value in auth_targets]:
            return False, None, "authoritative targets mismatch"
    except ValueError:
        return False, None, "authoritative targets are invalid"

    if int(authoritative.get("qty") or 0) == 2:
        if str(submitted.get("planVersion") or "") != str(authoritative.get("planVersion") or ""):
            return False, None, "authoritative planVersion mismatch"
        if submitted.get("legs") != authoritative.get("legs"):
            return False, None, "authoritative legs mismatch"
    return True, {**authoritative, "executionContract": contract,
                  "_authoritativeView": view}, None


def _scenario_is_ultra(scenario):
    """凍結された riskCapSource が ULTRA の正本値なら ULTRA シナリオ(R47)。"""
    frozen = (scenario.get("executionContract")
              if isinstance(scenario.get("executionContract"), dict) else {})
    ultra_env = execution_contract.CONTRACT.get("ultra") or {}
    return bool(ultra_env.get("enabled")) and (
        str(frozen.get("riskCapSource") or "") == str(ultra_env.get("riskCapSource")))


def _authoritative_split_targets(scenario, qty, entry, side_name):
    """Return server-owned TP1/runner targets; client values are never routed."""
    leg_split = [1, 1]
    if qty != 2:
        # R47: ULTRA シナリオだけは比率分割の枚数で脚を検証する。
        leg_split = (execution_contract.ultra_split(qty)
                     if _scenario_is_ultra(scenario) else None)
        if leg_split is None:
            return normalize_tick(_webapp_number(scenario.get("target"), "target")), None
    targets = scenario.get("targets")
    legs = scenario.get("legs")
    if not isinstance(targets, list) or len(targets) < 2 or not isinstance(legs, list) or len(legs) != 2:
        raise ValueError("qty=2 needs authoritative TP1/runner targets and legs")
    tp1 = normalize_tick(_webapp_number(targets[0], "targets[0]"))
    runner = normalize_tick(_webapp_number(targets[-1], "targets[-1]"))
    expected = [{"id": "TP1", "qty": leg_split[0], "target": tp1},
                {"id": "RUNNER", "qty": leg_split[1], "target": runner}]
    if legs != expected or not scenario.get("planVersion"):
        raise ValueError("authoritative split plan is incomplete")
    if tp1 == runner:
        raise ValueError("TP1 and runner target must differ")
    if side_name == "BUY" and not (entry < tp1 < runner):
        raise ValueError("BUY split targets must be ordered above entry")
    if side_name == "SELL" and not (runner < tp1 < entry):
        raise ValueError("SELL split targets must be ordered below entry")
    return tp1, runner


def _one_pass_server_gate(payload, scenario):
    """AUTO_ONE_PASS のためのサーバー側再検証。

    Mini App の表示値や ``onePass`` マーカーは信用しない。最新の Durable
    Object view と照合し、同じシナリオ・発注可・A/A+・有効状態が揃わない
    限り、AUTO-EXECUTE は dry-run にも入れず fail-closed で止める。
    手動スライド経路には適用しない。
    """
    marker = payload.get("onePass")
    if not isinstance(marker, dict) or str(marker.get("status", "")).upper() != "PASS":
        return False, "ONE-PASS の PASS マーカーがありません"
    marker_grade = str(marker.get("grade") or "").strip().upper()
    if marker_grade not in {"A", "A+", "B"}:
        return False, "ONE-PASS は A+ / A / B のみ許可します"

    # Reuse the exact authoritative view used by the shared gate.  A second
    # fetch here would create a TOCTOU window and made AUTO differ from manual.
    view = scenario.get("_authoritativeView")
    if not isinstance(view, dict):
        return False, "latest NQX state was not retained for ONE-PASS"

    authoritative = view.get("scenario")
    if not isinstance(authoritative, dict):
        return False, "サーバーに有効なシナリオがありません"
    if str(authoritative.get("scenarioId") or "") != str(scenario.get("scenarioId") or ""):
        return False, "表示中シナリオとサーバーシナリオが一致しません"
    if str(authoritative.get("fingerprint") or "") != str(scenario.get("fingerprint") or ""):
        return False, "シナリオ fingerprint が一致しません"

    state = str(authoritative.get("state") or "").upper()
    if state not in {"ACTIVE", "ARMED"}:
        return False, f"サーバーシナリオが {state or '不明'} のため発注不可です"

    display = view.get("display")
    if not isinstance(display, dict) or display.get("orderable") is not True:
        return False, (display.get("blockReason") if isinstance(display, dict)
                       else "サーバー発注ゲートが閉じています")

    evaluation = (view.get("market") or {}).get("evaluation")
    decision = evaluation.get("decision") if isinstance(evaluation, dict) else None
    grade = str(authoritative.get("grade") or
                (decision.get("grade") if isinstance(decision, dict) else "") or
                "").strip().upper()
    if grade not in {"A", "A+", "B"}:
        return False, f"サーバー判定が {grade or '不明'} のため A+ / A / B 条件未達です"
    if grade != marker_grade:
        return False, "ONE-PASS 等級とサーバー等級が一致しません"

    # 価格・方向・数量を最新 view と突き合わせ、古い画面からの差替えを拒否。
    try:
        if str(authoritative.get("symbol") or "") != str(scenario.get("symbol") or ""):
            return False, "symbol がサーバーシナリオと一致しません"
        if str(authoritative.get("side") or "").upper() != str(scenario.get("side") or "").upper():
            return False, "side がサーバーシナリオと一致しません"
        if int(authoritative.get("qty")) != int(scenario.get("qty")):
            return False, "qty がサーバーシナリオと一致しません"
        for field in ("entry", "stop", "target"):
            if normalize_tick(_webapp_number(authoritative.get(field), field)) != normalize_tick(
                    _webapp_number(scenario.get(field), field)):
                return False, f"{field} がサーバーシナリオと一致しません"
    except (TypeError, ValueError):
        return False, "サーバーシナリオの数値を検証できません"

    return True, None


def _ultra_order_gate(scenario, side_name):
    """ULTRA ON の発注要求を正本の口座設定から組み直して判定する。

    返り値が ``(None, None)`` のときだけ通常経路へ進む。ULTRA が要求する
    枚数は定義上 execution_contract の固定枚数・リスク上限を超えるため、
    現状は必ずここで止まる。止める理由は口座別に全部見せる — 「なぜ出ない
    のか」が分からないまま握り潰すのが一番危ない。
    """
    import ultra_mode

    try:
        accounts_payload = nqx_state.build_accounts_payload()
    except Exception as exc:  # noqa: BLE001 - 設定不備で送信経路へ落ちない
        return _order_blocked(f"ULTRA: 口座設定を読めません ({exc})")[0], "ultra accounts unavailable"
    accounts = (accounts_payload or {}).get("list") or []
    if not accounts:
        return _order_blocked(
            "ULTRA: 口座設定 (LIFELINE_* / TARGET_*) がありません",
            "crosstrade.env に口座ごとの残ドローダウンと利益目標が必要です")[0], "ultra unconfigured"

    plan = ultra_mode.build_plan(
        ultra_mode.signal_from_scenario(scenario), accounts,
        signal_qty=scenario.get("qty"))
    geometry = plan.get("geometry") or {}
    if not geometry.get("valid"):
        return _order_blocked(f"ULTRA: シグナルが不正です ({geometry.get('reason')})")[0], "ultra signal invalid"

    lines = []
    for row in plan["accounts"]:
        if row["verdict"] == "ELIGIBLE":
            lines.append(f"{row['id']}: {row['qty']}枚 "
                         f"利益 ${row['projectedProfit']:,.0f} / 損失 ${row['projectedLoss']:,.0f} ELIGIBLE")
        else:
            lines.append(f"{row['id']}: INELIGIBLE ({', '.join(row['reasons'])})")
    detail = "\n".join(lines)

    if not plan["eligibleCount"]:
        return _order_blocked("ULTRA: 発注できる口座がありません", detail)[0], "ultra no eligible account"

    if not plan["routable"]:
        blockers = sorted({blocker for row in plan["accounts"] for blocker in row["contractBlockers"]})
        return _order_blocked(
            f"ULTRA_CONTRACT_BLOCKED: 計算枚数 合計{plan['totalQty']}枚 は現行の実行契約を超えます",
            detail + "\n\n拒否理由: " + ", ".join(blockers))[0], "ultra contract blocked"

    # 実行契約・エンベロープ・比率分割・所有権は ULTRA 枚数を通せる状態にある。
    # 残る唯一の関門が ENTRY claim の同一性。claim key は scenario tuple から
    # 導出されるので、同じシグナルで2口座目を CLAIM すると ALREADY_HELD で
    # 弾かれ、さらに RECOVER 後の tombstone が恒久化する。
    #
    # 口座次元を claim key へ入れる変更は「二重発注を防ぐ」中核不変条件そのもの
    # なので、専用の設計と試験を通してから開ける。ここで暫定的に回避しない。
    envelope = execution_contract.ultra_evaluate(plan)
    if not envelope["ok"]:
        return _order_blocked(
            "ULTRA_ENVELOPE_BLOCKED: " + ", ".join(envelope["blockers"]),
            detail)[0], "ultra envelope blocked"
    return _order_blocked(
        f"ULTRA_CLAIM_SCOPE_PENDING: 計算枚数 合計{envelope['totalQty']}枚 は実行契約を通ります",
        detail + "\n\nENTRY claim key が口座次元を持たないため、複数口座への同時"
        "ルーティングだけが未開通です。1口座ずつの claim 分離が入るまで送信しません。"
    )[0], "ultra claim scope pending"


def handle_scenario_order_confirmed(cfg, payload):
    """Mini App の CONFIRM & SEND ORDER 一回押しを処理する。

    実行順序は指示書どおり:
        認証済み chat(呼び出し元で確認済み)
          → nonce / 期限 / fingerprint
          → 数値・tick・方向・数量・リスク
          → 建玉と日次回数
          → order.py ドライラン(--confirm なし)
          → NQX_LIVE_ORDERS=1 かつドライラン成功のときだけ --confirm を一度
          → 成否と receipt を返す

    payload の値は一切信用しない。すべてここで作り直す。
    """
    scenario = payload.get("scenario")
    if not isinstance(scenario, dict):
        return _order_blocked("scenario がありません")[0], "scenario missing"

    execution_mode = str(payload.get("executionMode") or "MANUAL_SLIDE").upper()
    now = datetime.now(timezone.utc)
    allowed, authoritative, reason = _authoritative_scenario_gate(scenario, cfg=cfg, now=now)
    if not allowed:
        return _order_blocked(reason)[0], "authoritative scenario blocked"
    # From this point forward the server copy, not the Mini App payload, owns
    # all executable prices and targets.
    scenario = authoritative
    if execution_mode == "AUTO_ONE_PASS":
        allowed, reason = _one_pass_server_gate(payload, scenario)
        if not allowed:
            return _order_blocked(reason)[0], "auto one-pass blocked"

    key = str(payload.get("clientNonce") or payload.get("idempotencyKey") or "").strip()
    if len(key) < 16 or len(key) > 128:
        return _order_blocked("clientNonce が不正です")[0], "bad nonce"
    server_key = _server_order_key(scenario)
    if not server_key:
        return _order_blocked(
            "server-authoritative scenarioId/fingerprint/evidenceHash/marketCycleId identity is incomplete"
        )[0], "bad server identity"

    # ---- 期限 ----------------------------------------------------
    try:
        issued_at = _parse_iso(scenario.get("issuedAt"), "issuedAt")
        expires_at = _parse_iso(scenario.get("expiresAt"), "expiresAt")
    except ValueError as exc:
        return _order_blocked(f"シナリオ時刻が不正です: {exc}")[0], "bad timestamps"
    if expires_at <= now:
        return _order_blocked("シナリオは期限切れです",
                              f"失効 {expires_at.astimezone().strftime('%H:%M:%S')} / 現在 "
                              f"{now.astimezone().strftime('%H:%M:%S')}")[0], "expired"
    if (now - issued_at).total_seconds() > SCENARIO_MAX_AGE_SEC:
        return _order_blocked("シナリオが古すぎます")[0], "stale scenario"
    if not scenario.get("fingerprint") or not scenario.get("scenarioId"):
        return _order_blocked("fingerprint / scenarioId がありません")[0], "no fingerprint"

    # ---- symbol / side / 数値 ------------------------------------
    symbol = str(scenario.get("symbol") or "")
    if symbol != NQX_SYMBOL:
        return _order_blocked(f"symbol が {symbol} です(期待 {NQX_SYMBOL})")[0], "symbol mismatch"

    side_name = str(scenario.get("side", "")).upper()
    if side_name not in ("BUY", "SELL"):
        return _order_blocked("side が不正です")[0], "bad side"
    side = side_name.lower()

    try:
        entry = normalize_tick(_webapp_number(scenario.get("entry"), "entry"))
        sl = normalize_tick(_webapp_number(scenario.get("stop"), "stop"))
    except ValueError as exc:
        return _order_blocked(f"価格が不正です: {exc}")[0], "bad price"

    try:
        qty = int(scenario.get("qty", WEB_APP_DEFAULT_QTY))
    except (TypeError, ValueError):
        return _order_blocked("qty が不正です")[0], "bad qty"

    # ---- ULTRA mode ----------------------------------------------
    # R47: サーバー正本のシナリオ自体が ULTRA サイズ済み(riskCapSource が
    # ULTRA の正本値)のときは、そのシナリオの枚数を ULTRA エンベロープで
    # 検証して通す。旧経路(payload の ultraMode 意図だけ)は従来どおり
    # ここで再計算・遮断される。
    scenario_ultra = _scenario_is_ultra(scenario)
    ultra_env = execution_contract.CONTRACT.get("ultra") or {}
    if payload.get("ultraMode") is True and not scenario_ultra:
        ultra_body, ultra_note = _ultra_order_gate(scenario, side_name)
        if ultra_body is not None:
            return ultra_body, ultra_note

    if scenario_ultra:
        if (qty < int(ultra_env.get("minQtyPerAccount") or 2)
                or qty > int(ultra_env.get("maxQtyPerAccount") or 100)
                or execution_contract.ultra_split(qty) is None):
            return _order_blocked(
                f"ULTRA_QTY_OUT_OF_ENVELOPE: {qty}枚は ULTRA エンベロープ外です"
            )[0], "ultra qty invalid"
    elif qty != WEB_APP_DEFAULT_QTY:
        return _order_blocked(
            f"FIXED_QTY_REQUIRED: 新規は固定 {WEB_APP_DEFAULT_QTY} 枚です(指定 {qty})"
        )[0], "fixed qty"

    try:
        tp, runner_tp = _authoritative_split_targets(scenario, qty, entry, side_name)
    except ValueError as exc:
        return _order_blocked(f"分割TPの正本プランが無効です: {exc}")[0], "invalid split plan"

    # 正規化「後」の値で方向を再判定する。丸めで等値になった場合も弾く。
    if side == "buy" and not (sl < entry < tp and (runner_tp is None or tp < runner_tp)):
        return _order_blocked("BUY は SL < Entry < TP が必要です",
                              f"正規化後: SL {sl:,.2f} / Entry {entry:,.2f} / TP {tp:,.2f}")[0], "bad direction"
    if side == "sell" and not (tp < entry < sl and (runner_tp is None or runner_tp < tp)):
        return _order_blocked("SELL は TP < Entry < SL が必要です",
                              f"正規化後: TP {tp:,.2f} / Entry {entry:,.2f} / SL {sl:,.2f}")[0], "bad direction"

    risk = abs(entry - sl) * qty * POINT_VALUE
    if scenario_ultra:
        # ULTRA: 上限の正本は凍結された残ドローダウンと契約の口座あたり上限の
        # 狭い側。$240 既定はここでは使わない(order.py も同じ判定を再実行する)。
        frozen_contract = (scenario.get("executionContract")
                           if isinstance(scenario.get("executionContract"), dict) else {})
        frozen_cap = frozen_contract.get("riskCapDollars")
        try:
            ultra_cap = min(float(frozen_cap),
                            float(ultra_env.get("maxRiskDollarsPerAccount") or 0))
        except (TypeError, ValueError):
            ultra_cap = 0.0
        if ultra_cap <= 0 or risk > ultra_cap + 1e-9:
            return _order_blocked(
                f"ULTRA: 推定リスク ${risk:,.2f} が残ドローダウン上限 ${ultra_cap:,.2f} を超えています"
            )[0], "ultra risk cap"
    elif risk > MAX_ORDER_RISK_DOLLARS:
        return _order_blocked(f"推定リスク ${risk:.2f} が上限 ${MAX_ORDER_RISK_DOLLARS:.2f} を超えています")[0], "risk cap"

    # ---- 建玉と日次回数 ------------------------------------------
    blocked, reason, evidence = preflight_position(cfg)
    if blocked:
        return _order_blocked(reason, "保有中は新規発注を有効化しません")[0], "position open"

    counts, status_out = daily_order_count()
    if counts and counts[0] >= counts[1]:
        return _order_blocked(f"本日の発注上限 {counts[1]} 回に到達しています")[0], "daily cap"

    target_desc = (f"TP1 {tp:,.2f} / Runner {runner_tp:,.2f}" if runner_tp is not None
                   else f"TP {tp:,.2f}")
    desc = f"{side_name} {qty}枚 / Entry {entry:,.2f} / SL {sl:,.2f} / {target_desc}"

    # ---- idempotency ---------------------------------------------
    # ここから先は送信経路に入る。二重タップ・再送はこの一行で止まる。
    if not consume_order_key(key, desc):
        return _order_blocked("この確認は既に処理済みです",
                              "同じボタンからの再送は実行しません")[0], "duplicate nonce"

    argv = ["--side", side, "--qty", str(qty),
            "--entry", fmt_price(entry), "--sl", fmt_price(sl), "--symbol", symbol]
    # R102: 参照価格と出所はサーバー正本の market(同じ view)から。order.py は指値の通過と
    # SL の側を発注先限月の価格で検査し、価格が無ければ送信直前の quote、それも無ければ
    # QUOTE_UNAVAILABLE で送らない(2026-09-15 の 285pt 通過指値の再発防止)。
    view_for_ref = scenario.get("_authoritativeView") if isinstance(scenario.get("_authoritativeView"), dict) else {}
    market_ref = view_for_ref.get("market") if isinstance(view_for_ref.get("market"), dict) else {}
    try:
        market_price = float(market_ref.get("price")) if market_ref.get("price") is not None else None
    except (TypeError, ValueError):
        market_price = None
    if market_price is not None and math.isfinite(market_price) and market_price > 0:
        argv += ["--last", fmt_price(market_price)]
    price_origin = str(market_ref.get("sourceContract") or market_ref.get("sourceSymbol") or "")
    if price_origin and not contract_month.is_continuous(price_origin):
        argv += [f"--price-symbol={price_origin}"]
    if runner_tp is not None:
        argv += ["--split-tp", f"{fmt_price(tp)},{fmt_price(runner_tp)}"]
    else:
        argv += ["--tp", fmt_price(tp)]
    if scenario_ultra:
        # R47: ULTRA は口座ごとに1回(claim scope)。宛先を凍結スコープの
        # 1口座へ狭め、リスク上限は残ドローダウンを order.py へ渡す。
        frozen_contract = (scenario.get("executionContract")
                           if isinstance(scenario.get("executionContract"), dict) else {})
        ultra_scope = [str(a) for a in (frozen_contract.get("accountScope") or []) if str(a)]
        if len(ultra_scope) != 1:
            return _order_blocked(
                "ULTRA_SCOPE_NOT_SINGLE: ULTRA シナリオの凍結スコープが1口座ではありません"
            )[0], "ultra scope invalid"
        argv += ["--accounts", ultra_scope[0],
                 "--ultra", "--ultra-drawdown",
                 str(min(float(frozen_contract.get("riskCapDollars")),
                         float(ultra_env.get("maxRiskDollarsPerAccount") or 0)))]

    # ---- ドライラン ------------------------------------------------
    dry_ok, dry_out = run_order(argv)
    if not dry_ok:
        _publish_order_state(key, scenario, side, qty, entry, sl, tp, "REJECTED", None, "dry run rejected")
        body = (deck_head("ORDER BLOCKED", "🛑") + "\n<b>order.py が拒否しました</b>\n"
                + order_output(dry_out) + "\n<b>注文は送信していません</b>")
        return response(body, MENU), "dry run rejected"

    # ---- ライブ経路のロック -----------------------------------------
    if not live_orders_enabled(cfg):
        _publish_order_state(key, scenario, side, qty, entry, sl, tp, "CANCELED", None, "live route locked")
        body = (deck_head("LIVE ROUTE LOCKED", "🛡️") + "\n"
                "<b>注文送信はロック中です。</b>\n"
                "<code>.secrets/telegram.env</code> に <code>NQX_LIVE_ORDERS=1</code> を設定し、"
                "Bot を再起動してから、Mini App でもう一度確認してください。\n"
                "<b>注文は送信していません</b>\n" + dry_run_output(desc, dry_out))
        return response(body, MENU), "live route locked"

    # ---- 送信(ここだけが --confirm を付ける唯一の場所)-----------------
    # A nonce is only a UI retry token.  The durable/local broker lock is the
    # immutable server tuple, shared by MANUAL_SLIDE and AUTO_ONE_PASS.
    authoritative_view = scenario.get("_authoritativeView")
    if _server_order_lock_active(server_key, authoritative_view):
        return _order_blocked(
            "the current server scenario already has a local PENDING/SENT/PARTIAL/UNKNOWN lock",
            "A matching broker terminal state is required before retrying")[0], "server tuple locked"
    try:
        claim_ok, claim = nqx_state.claim_entry(scenario, order_type="LIMIT")
    except Exception as exc:  # noqa: BLE001 - claim failure must never route
        claim_ok, claim = False, {"reason": f"{type(exc).__name__}: {exc}"}
    if not claim_ok or not isinstance(claim, dict):
        return _order_blocked(
            "Durable Object ENTRY claim could not be acquired",
            f"Fail closed: {claim}")[0], "entry claim failed"
    if (str(claim.get("entryKey") or "") != server_key or not claim.get("claimToken")
            or not claim.get("executionIntentHash")):
        return _order_blocked("Durable Object returned an invalid ENTRY claim")[0], "entry claim invalid"
    try:
        _set_server_order_lock(server_key, "PREPARING", scenario, str(claim["claimToken"]))
    except OSError as exc:
        return _order_blocked(f"local order lock could not be persisted: {exc}")[0], "local lock failed"
    pending_ok = _publish_order_state(
        server_key, scenario, side, qty, entry, sl, tp, "PENDING", None, "sending")
    if not pending_ok:
        try:
            _set_server_order_lock(server_key, "UNKNOWN", scenario)
        except OSError:
            pass
        return _order_blocked(
            "durable PENDING commit was not confirmed",
            "The local state is UNKNOWN; order.py --confirm was not called")[0], "pending publish failed"
    try:
        _set_server_order_lock(server_key, "PENDING", scenario)
    except OSError:
        # The Durable Object is blocking, but losing the independent local
        # lock would weaken restart/race protection.  Do not confirm.
        return _order_blocked(
            "local PENDING lock could not be persisted",
            "order.py --confirm was not called")[0], "local pending lock failed"
    claimed_argv = argv + ["--entry-key", server_key, "--claim-token", str(claim["claimToken"]),
                           "--intent-hash", str(claim["executionIntentHash"]),
                           "--plan-version", str(scenario.get("planVersion") or "")]
    send_ok, send_out = run_order(claimed_argv + ["--confirm"])
    expected_accounts = ((scenario.get("executionContract") or {}).get("accountScope")
                         if isinstance(scenario.get("executionContract"), dict) else None)
    outcome, receipt = classify_send_result(send_ok, send_out, expected_accounts)
    terminal_state = {"SENT": "SENT", "PARTIAL": "PARTIAL",
                      "REJECTED": "REJECTED", "UNKNOWN": "UNKNOWN"}[outcome]
    terminal_saved = _publish_order_state(
        server_key, scenario, side, qty, entry, sl, tp, terminal_state, receipt, outcome)
    if not terminal_saved:
        try:
            _set_server_order_lock(server_key, "UNKNOWN", scenario)
        except OSError:
            pass
        body = (deck_head("UNKNOWN — VERIFY BROKER", "⚠️") + "\n"
                "<b>The send result could not be durably recorded.</b>\n"
                "The local server-tuple lock is UNKNOWN. A matching broker terminal state "
                "must be observed before retry.\n" + order_output(send_out))
        return response(body, MENU), "sent state publish failed"
    if outcome == "REJECTED":
        _release_server_order_lock(server_key)
    else:
        _set_server_order_lock(server_key, terminal_state, scenario)

    # 送信直後にブローカーへ照会し、約定を確認できたときだけ建玉として載せる。
    _sync_position_quiet(cfg)

    if outcome == "SENT":
        body = (deck_head("ORDER SENT", "🚀") + "\n"
                f"<b>{esc(symbol)} {side_name} {qty}</b>\n"
                + pre(f"Entry {entry:,.2f}\nSL    {sl:,.2f}\n{target_desc}\n"
                      f"リスク ${risk:.2f}")
                + f"\n<b>Receipt:</b> <code>{esc(receipt or '(なし)')}</code>\n"
                  "<i>送信は成功しました。約定はブローカー照会で確認されます。</i>\n"
                + order_output(send_out))
        return response(body, MENU), "order sent"

    if outcome == "REJECTED":
        body = (deck_head("ORDER BLOCKED", "🛑") + "\n<b>下流が送信を拒否しました</b>\n"
                + order_output(send_out) + "\n<b>建玉は作られていない見込みですが、必ず確認してください</b>")
        return response(body, MENU), "order rejected"

    body = (deck_head("UNKNOWN — VERIFY BROKER", "⚠️") + "\n"
            "<b>送信の成否を判定できませんでした。</b>\n"
            "Tradovate 側の建玉と未約定注文を今すぐ確認してください。\n"
            + order_output(send_out))
    return response(body, MENU), "order unknown"


def _publish_order_state(key, scenario, side, qty, entry, sl, tp, state, receipt, detail):
    """Publish an order state and report whether the durable commit succeeded."""
    try:
        result = nqx_state.publish_order({
            "idempotencyKey": key,
            "scenarioId": scenario.get("scenarioId"),
            "state": state,
            "side": side.upper(),
            "qty": qty,
            "entry": entry,
            "stop": sl,
            "target": tp,
            "receipt": receipt,
            "detail": detail,
            "at": datetime.now(timezone.utc).isoformat(),
        })
        if isinstance(result, tuple):
            return result[0] is True
        return result is True
    except Exception as exc:
        print(f"[{ts()}] order state publish failed: {exc}", file=sys.stderr)
        return False


def _sync_position_quiet(cfg=None):
    """ブローカー照会 → state 反映。建玉が閉じたら決済結果も publish する。

    戻り値 ``(closed: bool, note: str|None)``。closed が True のときだけ
    呼び出し側が結果を報告する。
    """
    try:
        ok, detail = nqx_state.sync_position()
    except nqx_state.NotConfigured:
        return False, None
    except Exception as exc:
        print(f"[{ts()}] position sync failed: {exc}", file=sys.stderr)
        return False, None

    if not ok or not isinstance(detail, dict):
        return False, None
    transitions = detail.get("transitions") or []
    _resolve_order_after_position(transitions)
    if not any(t.get("kind") == "position" and t.get("to") == "CLOSED" for t in transitions):
        return False, None

    published, note = publish_trade_result(cfg, realised=detail.get("realised") or {})
    return True, note


def _resolve_order_after_position(transitions):
    """position の遷移に合わせて order ストリームを終端化する(HANDOFF §11 ②)。

    order.py は送信後の注文状態を publish しないため、SENT のまま残った注文は
    決済して position が消えた瞬間に ORDER_PENDING として再浮上し、発注を
    永久ブロックする。ここが唯一の自動終端化経路:
      - 建玉がブローカーで確認できた → FILLED
      - 建玉が閉じたのに blocking のまま残っている → CANCELED(掃除)
    """
    to_states = {t.get("to") for t in transitions if t.get("kind") == "position"}
    if to_states & {"OPEN", "PARTIAL", "EXIT_PENDING"}:
        to_state, note = "FILLED", "position open confirmed by broker"
    elif "CLOSED" in to_states:
        to_state, note = "CANCELED", "position closed; residual order cleared"
    else:
        return
    try:
        ok, detail = nqx_state.resolve_stuck_order(to_state, detail=note)
        if not ok and not (isinstance(detail, dict) and detail.get("skipped")):
            print(f"[{ts()}] order resolve failed: {detail}", file=sys.stderr)
    except Exception as exc:
        print(f"[{ts()}] order resolve failed: {exc}", file=sys.stderr)


def _suggest_exit_price():
    """1タップ記録の候補価格。検証済み・非STALEの現在値だけを返す。

    ここで返した価格は**候補**であって記録ではない。ユーザーがボタンを
    押した時点で初めて exitSource=manual として publish される。
    """
    try:
        view = nqx_state.fetch_state_quiet()
    except Exception:
        return None
    market = (view or {}).get("market") or {}
    price_value = market.get("price")
    if market.get("verified") is True and not market.get("stale") and isinstance(price_value, (int, float)):
        return normalize_tick(price_value)
    return None


def publish_trade_result(cfg=None, realised=None, manual_exit=None):
    """決済結果を Durable Object へ publish する。

    決済価格はブローカー由来かユーザーが明示した値だけを使う。
    どちらも無ければ **publish しない**。相場から推定した決済価格で
    損益画面を出すことはしない。

    戻り値 ``(published: bool, note: str)``。
    """
    realised = realised or {}
    view = nqx_state.fetch_state_quiet()
    if view is None:
        return False, "state を取得できないため結果を作れません"
    closed = view.get("closedPosition")
    if not closed:
        return False, "決済済みの建玉が state にありません"

    exit_price = manual_exit if manual_exit is not None else realised.get("avgExit")
    if exit_price is None:
        return False, ("決済価格をブローカーから取得できません。"
                       "<code>/result 29796.25</code> のように実際の決済価格を指定してください")
    exit_source = "manual" if manual_exit is not None else "broker"

    bars = (view.get("market") or {}).get("bars")
    live = live_orders_enabled(cfg) if cfg else False
    try:
        result = nqx_state.build_result(
            closed, exit_price, exit_source, bars=bars,
            symbol=NQX_SYMBOL, point_value=POINT_VALUE,
            mode="LIVE" if live else "SIMULATION",
        )
    except (ValueError, TypeError) as exc:
        return False, f"結果を作れません: {exc}"

    ok, detail = nqx_state.publish_result(result)
    if not ok:
        return False, f"結果の publish に失敗しました: {str(detail)[:160]}"

    points = (result["exit"] - result["entry"]) * (-1 if result["side"] == "SHORT" else 1)
    usd = points * result["pointValue"] * result["qty"]
    return True, (f"{result['side']} {result['qty']}枚 / "
                  f"{result['entry']:,.2f} → {result['exit']:,.2f} / "
                  f"{points:+.2f} pt = ${usd:+,.2f}\n"
                  f"exit: {exit_source} / path: {result['pathSource']} "
                  f"({len(result['path'])} 点)")


def handle_stuck_order_cancel(cfg, payload):
    """Mini App の「観測漏れの注文表示を解除」を処理する(HANDOFF §11 ②)。

    order ストリームに残った blocking(PENDING/SENT/PARTIAL/UNKNOWN)を CANCELED に
    するだけで、ブローカーの注文・建玉には一切触れない。発注ではないため
    NQX_LIVE_ORDERS のロックは要求しない。

    誤操作より見逃しを許す。確認が一つでも欠けたら何もしない:
        認証済み chat(呼び出し元で確認済み)
          → nonce → 対象キーの一致 → 送信からの経過時間
          → ブローカー verified FLAT → 一回限りの消費 → CANCELED publish
    """
    def blocked(reason, note=None):
        body = deck_head("UNSTICK BLOCKED", "🛑") + "\n<b>" + esc(reason) + "</b>\n"
        if note:
            body += esc(note) + "\n"
        body += "<b>表示は変更していません</b>"
        return response(body, MENU), f"unstick blocked: {reason}"

    key = str(payload.get("clientNonce") or "").strip()
    if len(key) < 16 or len(key) > 128:
        return blocked("clientNonce が不正です")
    order_key = str(payload.get("orderKey") or "").strip()
    if not order_key:
        return blocked("orderKey がありません")

    view = nqx_state.fetch_state_quiet()
    if not isinstance(view, dict):
        return blocked("state を取得できません", "Durable Object に到達できないため解除しません")
    order = view.get("order")
    if not isinstance(order, dict):
        return blocked("解除対象の注文がありません")
    state = str(order.get("state") or "").upper()
    if state not in nqx_state.BLOCKING_ORDER_STATES:
        return blocked(f"注文は既に {state} です", "解除は不要です")
    if order.get("idempotencyKey") != order_key:
        return blocked("注文が入れ替わっています",
                       "画面の表示が古い可能性があります。Mini App を開き直してください")

    try:
        order_at = _parse_iso(order.get("at"), "order.at")
    except ValueError:
        return blocked("注文の送信時刻を確認できません")
    age_sec = (datetime.now(timezone.utc) - order_at).total_seconds()
    if age_sec < STUCK_ORDER_MIN_AGE_SEC:
        return blocked(f"送信から {age_sec / 60:.0f} 分しか経っていません",
                       f"約定待ちの可能性があるため {STUCK_ORDER_MIN_AGE_SEC // 60} 分間は解除しません")

    result = broker_status.query_position(NQX_SYMBOL)
    if not result.get("verified"):
        return blocked("ブローカー照会が未確認です", "建玉なしを確認できるまで解除しません")
    if int(result.get("qty") or 0) > 0:
        return blocked(f"建玉があります — {result.get('side')} {result.get('qty')} 枚",
                       "観測漏れではありません。定期照会が注文を FILLED に進めます(/sync でも可)")

    claim = view.get("entryClaim") if isinstance(view.get("entryClaim"), dict) else {}
    rows = claim.get("routeSnapshot") if isinstance(claim.get("routeSnapshot"), list) else []
    frozen_ids = {str(row.get("orderId")) for row in rows
                  if isinstance(row, dict) and row.get("state") == "ACCEPTED" and row.get("orderId")}
    if not frozen_ids:
        return blocked("凍結されたbroker order IDがありません",
                       "verified FLATだけではresting orderを解除しません")
    broker_orders = broker_status.query_orders(NQX_SYMBOL, known_order_ids=frozen_ids)
    if not isinstance(broker_orders, dict) or broker_orders.get("verified") is not True:
        return blocked("ブローカー注文照会が未確認です")
    all_rows = [row for row in broker_orders.get("orders") or [] if isinstance(row, dict)]
    indexed = {str(row.get("orderId")): row for row in all_rows if row.get("orderId") is not None}
    if not frozen_ids.issubset(indexed):
        return blocked("凍結注文IDの一部をブローカーで確認できません")
    terminal = {"FILLED", "CANCELED", "CANCELLED", "REJECTED", "EXPIRED"}
    nonterminal = [order_id for order_id in frozen_ids
                   if str(indexed[order_id].get("status") or "").upper() not in terminal]
    if nonterminal:
        return blocked("resting orderがまだ生存しています",
                       "対象: " + ", ".join(sorted(nonterminal)))

    if not consume_order_key(key, f"UNSTICK {order_key}"):
        return blocked("この解除要求は既に処理済みです", "同じボタンからの再送は実行しません")

    ok, detail = nqx_state.resolve_stuck_order("CANCELED", detail="manual cancel via Mini App", view=view)
    if not ok:
        return blocked("CANCELED の記録に失敗しました", str(detail)[:200])

    body = (deck_head("ORDER DISPLAY CLEARED", "🧹") + "\n"
            f"<b>{esc(state)} {esc(str(order.get('side') or ''))} "
            f"{esc(str(order.get('qty') or ''))}</b> を CANCELED として記録しました。\n"
            "ブローカーの注文・建玉には触れていません。\n"
            f"照会: verified FLAT + frozen orders terminal ({esc(str(result.get('source') or ''))})")
    return response(body, MENU), "stuck order cleared"


def handle_web_app_data(cfg, raw_data):
    """Convert a Mini App payload into the appropriate path.

    The browser is never trusted for authorization or execution.  The polling
    loop checks the exact allowed chat ID before calling this function.

    - ``scenario_order_confirmed`` : 一回押しの明示承認。全項目を再検証し、
      ドライラン成功かつ NQX_LIVE_ORDERS=1 のときだけ ``--confirm`` を一度実行する。
    - ``stuck_order_cancel_confirmed`` : 観測漏れで残った注文表示の解除。
      発注はしない。ブローカー verified FLAT を再確認できたときだけ
      order=CANCELED を publish する(HANDOFF §11 ②)。
    - ``scenario_preview`` / ``scenario_order_request`` : 従来の二段階経路。
      ドライランを作り、Telegram の CONFIRM ボタンを待つ(互換のため残す)。
    """
    try:
        payload = json.loads(raw_data)
        if not isinstance(payload, dict):
            raise ValueError("payload is not an object")

        if payload.get("type") == "scenario_order_confirmed":
            body, note = handle_scenario_order_confirmed(cfg, payload)
            text, keyboard = body
            return text, keyboard, note

        if payload.get("type") == "stuck_order_cancel_confirmed":
            body, note = handle_stuck_order_cancel(cfg, payload)
            text, keyboard = body
            return text, keyboard, note

        if payload.get("type") in {"scenario_preview", "scenario_order_request"}:
            clear_pending()
            return (deck_head("AUTHORITATIVE ENTRY ONLY", "🛡️") + "\n"
                    "旧Mini App preview/order requestは発注経路ではありません。"
                    "最新画面のauthoritative one-clickを使用してください。",
                    MENU, "legacy entry route disabled")
        if payload.get("type") not in {"scenario_preview", "scenario_order_request"}:
            raise ValueError("unsupported Mini App payload")
        scenario = payload.get("scenario")
        if not isinstance(scenario, dict):
            raise ValueError("scenario is missing")

        side_name = str(scenario.get("side", "")).upper()
        if side_name not in ("BUY", "SELL"):
            raise ValueError("side is invalid")
        side = side_name.lower()
        entry = _webapp_number(scenario.get("entry"), "entry")
        sl = _webapp_number(scenario.get("stop"), "stop")
        tp = _webapp_number(scenario.get("target"), "target")

        # The prototype's risk panel is calibrated for two MNQ contracts.  A
        # client may omit qty, but can never exceed the hard open-size limit.
        qty_value = scenario.get("qty", WEB_APP_DEFAULT_QTY)
        try:
            qty = int(qty_value)
        except (TypeError, ValueError):
            raise ValueError("qty is invalid")
        if qty < 1 or qty > WEB_APP_MAX_QTY:
            raise ValueError("qty is outside the safety limit")

        # Reject an obviously inverted setup before order.py is invoked.
        if side == "buy" and not (sl < entry < tp):
            raise ValueError("BUY requires stop < entry < target")
        if side == "sell" and not (tp < entry < sl):
            raise ValueError("SELL requires target < entry < stop")
        estimated_risk = abs(entry - sl) * qty * 2.0
        if estimated_risk > MAX_ORDER_RISK_DOLLARS:
            raise ValueError(
                f"estimated risk ${estimated_risk:.2f} exceeds ${MAX_ORDER_RISK_DOLLARS:.2f} cap"
            )

        command = f"/order {side} {qty} {fmt_price(entry)} {fmt_price(sl)} {fmt_price(tp)}"
        body, keyboard = handle(cfg, command)
        if "SEND FAILED" in str(body):
            return body, keyboard, "Order request rejected by safety gate"
        return body, keyboard, "Order request received; confirm button required"
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        return (f"<b>Mini App payload rejected</b>\n{esc(str(exc))}", MENU,
                "Mini App payload rejected")


def check(cfg):
    """設定と疎通の確認。"""
    print(f"env      : {ENV}")
    print(f"order.py : {ORDER_PY}", "OK" if os.path.exists(ORDER_PY) else "*** 見つかりません ***")
    print(f"chat_id  : {cfg['TELEGRAM_CHAT_ID']}")
    me = api(cfg, "getMe")
    if not me or not me.get("ok"):
        print("getMe 失敗。トークンを確認してください")
        return 1
    u = me["result"]
    print(f"bot      : @{u.get('username')} ({u.get('first_name')})")
    ok, out = run_order(["--status"])
    print(f"--- order.py --status ---\n{out}")
    r = send(cfg, "<b>疎通確認</b>\nBot は正常に接続されています。")
    if r and r.get("ok"):
        print("テストメッセージを送信しました。Telegram を確認してください")
        return 0
    print("送信に失敗。TELEGRAM_CHAT_ID が正しいか、Bot に一度話しかけたか確認してください")
    return 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="設定と疎通の確認のみ")
    a = ap.parse_args()

    cfg = load_env()
    if a.check:
        sys.exit(check(cfg))

    allowed = str(cfg["TELEGRAM_CHAT_ID"])
    print(f"[{ts()}] Bot 起動。許可チャットID: {allowed}  (Ctrl-C で停止)")

    me = api(cfg, "getMe")
    if me and me.get("ok"):
        print(f"[{ts()}] @{me['result'].get('username')} として接続")
    send(cfg, "<b>Bot 起動</b>\n/help でコマンド一覧", MENU)

    send_webapp(cfg, "<b>✦ NIGHTWATCH</b>\nOpen the scenario console from the button below.")

    # 口座残機(LIFELINE)を起動時に publish する。env(LIFELINE_*)が正本。
    # 未設定・失敗でも Bot は止めない。
    try:
        acc_ok, acc_detail = nqx_state.publish_accounts()
        if acc_ok:
            print(f"[{ts()}] LIFELINE を publish しました")
        elif "not configured" not in str(acc_detail.get("reason", "")):
            print(f"[{ts()}] LIFELINE publish 失敗: {acc_detail}", file=sys.stderr)
    except Exception as exc:
        print(f"[{ts()}] LIFELINE publish 失敗: {exc}", file=sys.stderr)

    # R22: process restart cannot silently unlock an UNKNOWN broker route.
    # Re-query current broker truth and let the DO release only by snapshot CAS.
    try:
        for note in reconcile_startup_claims():
            print(f"[{ts()}] {note}")
    except Exception as exc:
        print(f"[{ts()}] startup claim recovery failed closed: {exc}", file=sys.stderr)

    offset = None
    last_position_sync = 0.0
    while True:
        try:
            # ブローカー建玉を定期照会して state 側を更新する。
            # 照会できなくてもカードは消えず STALE になる(消去はしない)。
            if time.time() - last_position_sync > POSITION_SYNC_INTERVAL_SEC:
                last_position_sync = time.time()
                closed, note = _sync_position_quiet(cfg)
                if closed:
                    # 建玉が閉じた。結果画面が出せたかどうかを必ず伝える。
                    body = deck_head("POSITION CLOSED", "🧾") + "\n" + (note or "決済結果を作成できませんでした")
                    keyboard = MENU
                    # ブローカーが決済価格を返せなかった場合は、直近の検証済み
                    # 価格を「候補」としてボタンに載せる(自動草稿)。押されて
                    # 初めて manual として記録される — 推定値で勝手に publish しない。
                    if note and "決済価格" in note:
                        suggestion = _suggest_exit_price()
                        if suggestion is not None:
                            body += ("\n\n直近の検証済み価格を決済価格として1タップで記録できます。"
                                     "\n実際の約定が違う場合は <code>/result 価格</code> を打ってください。")
                            keyboard = kb([(f"⟦🧾 {fmt_price(suggestion)} で記録⟧",
                                            f"result:{fmt_price(suggestion)}")]) + MENU
                    send(cfg, body, keyboard)

            params = {"timeout": POLL_TIMEOUT}
            if offset is not None:
                params["offset"] = offset
            r = api(cfg, "getUpdates", **params)
            if not r or not r.get("ok"):
                time.sleep(3)
                continue

            for up in r["result"]:
                offset = up["update_id"] + 1

                # ---- ボタンが押された ----
                cq = up.get("callback_query")
                if cq:
                    chat_id = str(cq.get("message", {}).get("chat", {}).get("id"))
                    data = cq.get("data", "")

                    # 認証: ボタン経由でも同じチェックを通す
                    if chat_id != allowed:
                        frm = cq.get("from", {})
                        print(f"[{ts()}] 拒否(btn) chat_id={chat_id} "
                              f"user=@{frm.get('username')} data={data!r}",
                              file=sys.stderr)
                        api(cfg, "answerCallbackQuery", callback_query_id=cq["id"])
                        continue

                    print(f"[{ts()}] [btn] {data}")
                    try:
                        reply, keyboard, note = handle_callback(cfg, data)
                    except Exception as e:
                        reply, keyboard, note = f"内部エラー: {esc(str(e))}", MENU, None
                        print(f"[{ts()}] callback error: {e}", file=sys.stderr)

                    # ボタンのローディング表示を必ず止める
                    api(cfg, "answerCallbackQuery", callback_query_id=cq["id"],
                        text=note or "")
                    if reply:
                        send(cfg, reply, keyboard)
                        print(f"[{ts()}] > {reply.splitlines()[0][:60]}")
                    continue

                # ---- 通常メッセージ ----
                msg = up.get("message") or up.get("edited_message")

                # Telegram keyboard-launched Mini Apps arrive as web_app_data
                # messages rather than ordinary text.
                if msg and msg.get("web_app_data") is not None:
                    chat_id = str(msg.get("chat", {}).get("id"))
                    if chat_id != allowed:
                        print(f"[{ts()}] rejected Mini App chat_id={chat_id}", file=sys.stderr)
                        continue
                    raw_data = msg.get("web_app_data", {}).get("data", "")
                    print(f"[{ts()}] < Mini App payload ({len(raw_data)} bytes)")
                    try:
                        reply, keyboard, note = handle_web_app_data(cfg, raw_data)
                    except Exception as e:
                        reply, keyboard, note = f"<b>Mini App handler error</b>\n{esc(str(e))}", MENU, None
                        print(f"[{ts()}] Mini App handler error: {e}", file=sys.stderr)
                    if reply:
                        send(cfg, reply, keyboard)
                        print(f"[{ts()}] > {reply.splitlines()[0][:60]}")
                    continue

                if not msg or "text" not in msg:
                    continue

                chat_id = str(msg["chat"]["id"])
                text = msg["text"]

                # ---- 認証: 許可チャットID以外は一切応答しない ----
                if chat_id != allowed:
                    frm = msg.get("from", {})
                    print(f"[{ts()}] 拒否 chat_id={chat_id} "
                          f"user=@{frm.get('username')} text={text[:40]!r}",
                          file=sys.stderr)
                    continue

                print(f"[{ts()}] < {text}")
                try:
                    reply, keyboard = handle(cfg, text)
                except Exception as e:
                    reply, keyboard = f"内部エラー: {esc(str(e))}", MENU
                    print(f"[{ts()}] handler error: {e}", file=sys.stderr)
                if reply:
                    send(cfg, reply, keyboard)
                    print(f"[{ts()}] > {reply.splitlines()[0][:60]}")

        except KeyboardInterrupt:
            print(f"\n[{ts()}] 停止しました")
            send(cfg, "<b>Bot 停止</b>")
            break
        except Exception as e:
            print(f"[{ts()}] loop error: {e}", file=sys.stderr)
            time.sleep(3)


if __name__ == "__main__":
    main()

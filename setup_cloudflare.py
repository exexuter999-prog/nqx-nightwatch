#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Cloudflare への一括セットアップ(Worker → 設定 → Pages → Telegram)。

    python setup_cloudflare.py            # 1〜4 を通しで実行
    python setup_cloudflare.py --check    # 前提だけ確認して何もしない
    python setup_cloudflare.py --skip-keyboard   # Telegram への貼り直しをしない

## 前提

プロジェクト直下で(PowerShell でもそのまま動く):

    node cloudflare\\node_modules\\wrangler\\bin\\wrangler.js login

ブラウザで Cloudflare のダッシュボードにログインするだけでは足りない。
CLI 側の OAuth が別に要る。対話的なので事前に済ませておくこと。

## 何をするか

  1. Worker + Durable Object をデプロイし、URL を取得する
  2. secret を投入し、`.secrets/nqx_cloud.env` を作る
     (NQX_PUBLISH_SECRET / NQX_LAUNCH_SECRET はここで生成する)
  3. Mini App をビルドして Pages にデプロイする
  4. Telegram の常設キーボードを最新ビルドの URL で貼り直す

## やらないこと

  - 発注。このスクリプトは order.py にも CrossTrade にも触れない
  - 秘密の表示。生成した値も既存の値も標準出力に出さない
  - 既存 secret の上書き(既に設定済みなら再利用する)
"""
import argparse
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import urllib.error
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
CLOUDFLARE_DIR = os.path.join(BASE, "cloudflare")
MINI_APP_DIR = os.path.join(BASE, "telegram_mini_app")
WRANGLER = os.path.join(CLOUDFLARE_DIR, "node_modules", "wrangler", "bin", "wrangler.js")
CLOUD_ENV = os.path.join(BASE, ".secrets", "nqx_cloud.env")
TELEGRAM_ENV = os.path.join(BASE, ".secrets", "telegram.env")
PAGES_PROJECT = "nqx-nightwatch"

for _stream in ("stdout", "stderr"):
    _file = getattr(sys, _stream, None)
    if _file is not None and hasattr(_file, "reconfigure"):
        try:
            _file.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

sys.path.insert(0, BASE)


def say(step, message):
    print(f"[{step}] {message}", flush=True)


def run(cmd, cwd, stdin_text=None, timeout=900):
    """秘密を握った状態で子プロセスを回す。出力はそのまま返す。"""
    env = dict(os.environ, CI="1", WRANGLER_SEND_METRICS="false",
               PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    proc = subprocess.run(cmd, cwd=cwd, env=env, input=stdin_text,
                          capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def wrangler(args, stdin_text=None, cwd=CLOUDFLARE_DIR, timeout=900):
    # .cmd シムを避けて wrangler の JS を node で直接叩く(Windows の spawn 制約)。
    return run(["node", WRANGLER] + args, cwd, stdin_text, timeout)


def read_env(path):
    cfg = {}
    if not os.path.exists(path):
        return cfg
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                cfg[key.strip()] = value.strip()
    return cfg


def check_prerequisites():
    problems = []
    if not os.path.exists(WRANGLER):
        problems.append("cloudflare/node_modules/wrangler がありません → cd cloudflare && npm install")
    if not os.path.exists(TELEGRAM_ENV):
        problems.append(f"{TELEGRAM_ENV} がありません")
    else:
        telegram = read_env(TELEGRAM_ENV)
        for key in ("TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID"):
            if not telegram.get(key):
                problems.append(f"{TELEGRAM_ENV} に {key} がありません")

    toml_path = os.path.join(CLOUDFLARE_DIR, "wrangler.toml")
    toml = open(toml_path, encoding="utf-8").read() if os.path.exists(toml_path) else ""
    match = re.search(r'^NQX_ALLOWED_USER_ID\s*=\s*"([^"]*)"', toml, re.M)
    if not match or not match.group(1):
        problems.append("wrangler.toml の NQX_ALLOWED_USER_ID が空です")

    code, out = wrangler(["whoami"], timeout=180)
    authed = "You are not authenticated" not in out
    if not authed:
        # PowerShell では && が使えないので、1 行で完結する形を案内する。
        problems.append(
            "wrangler が未認証です。プロジェクト直下で次を実行してください:\n"
            "      node cloudflare\\node_modules\\wrangler\\bin\\wrangler.js login")
    return problems, authed


# ---------------------------------------------------------------- 1. Worker

WORKER_URL_RE = re.compile(r"https://[a-z0-9.-]+\.workers\.dev")


def deploy_worker():
    say(1, "Worker + Durable Object をデプロイします")
    code, out = wrangler(["deploy"])
    if code != 0:
        print(out[-2500:])
        raise SystemExit("ERROR: wrangler deploy が失敗しました")
    urls = WORKER_URL_RE.findall(out)
    if not urls:
        print(out[-2500:])
        raise SystemExit("ERROR: Worker の URL を出力から取得できませんでした")
    url = urls[-1]
    say(1, f"デプロイ完了: {url}")
    return url


# ---------------------------------------------------------------- 2. secret と設定

def existing_secret_names():
    code, out = wrangler(["secret", "list"], timeout=180)
    if code != 0:
        return set()
    try:
        return {item["name"] for item in json.loads(out[out.index("["):out.rindex("]") + 1])}
    except (ValueError, KeyError, TypeError):
        return set(re.findall(r'"name"\s*:\s*"([^"]+)"', out))


def put_secret(name, value):
    code, out = wrangler(["secret", "put", name], stdin_text=value + "\n", timeout=300)
    if code != 0:
        print(out[-1500:])
        raise SystemExit(f"ERROR: secret {name} の投入に失敗しました")


def configure(worker_url):
    say(2, "secret を投入し、.secrets/nqx_cloud.env を作ります")
    telegram = read_env(TELEGRAM_ENV)
    existing_cloud = read_env(CLOUD_ENV)
    already = existing_secret_names()

    # 既に設定済みなら値を変えない。変えると Worker と PC 側で食い違う。
    publish_secret = existing_cloud.get("NQX_PUBLISH_SECRET") or secrets.token_hex(32)
    launch_secret = existing_cloud.get("NQX_LAUNCH_SECRET") or secrets.token_hex(32)

    reused = bool(existing_cloud.get("NQX_PUBLISH_SECRET"))
    say(2, "既存の署名鍵を再利用します" if reused else "署名鍵を新規生成しました(値は表示しません)")

    put_secret("NQX_PUBLISH_SECRET", publish_secret)
    put_secret("NQX_LAUNCH_SECRET", launch_secret)
    put_secret("TELEGRAM_BOT_TOKEN", telegram["TELEGRAM_TOKEN"])
    say(2, f"secret を投入しました(既存: {sorted(already) or 'なし'})")

    os.makedirs(os.path.dirname(CLOUD_ENV), exist_ok=True)
    with open(CLOUD_ENV, "w", encoding="utf-8") as fh:
        fh.write(
            "# setup_cloudflare.py が生成。共有・コミット禁止。\n"
            "# Worker 側の secret と同じ値でなければ publish も launch token も通らない。\n"
            f"NQX_API_BASE={worker_url}\n"
            f"NQX_PUBLISH_SECRET={publish_secret}\n"
            f"NQX_LAUNCH_SECRET={launch_secret}\n"
            "NQX_ACCOUNT_ID=lucid-50k-daily\n"
            "NQX_SYMBOL=MNQU6\n"
            "NQX_WEB_APP_URL=https://nqx-nightwatch.pages.dev/\n"
            f"NQX_USER_ID={telegram['TELEGRAM_CHAT_ID']}\n"
        )
    try:
        os.chmod(CLOUD_ENV, 0o600)
    except OSError:
        pass
    say(2, f"{CLOUD_ENV} を書きました")

    say(2, "Worker の疎通を確認します")
    try:
        health_request = urllib.request.Request(
            worker_url.rstrip("/") + "/api/health",
            headers={"User-Agent": "NQX-Nightwatch-Deploy-Check/1.0"},
        )
        with urllib.request.urlopen(health_request, timeout=20) as response:
            health = json.loads(response.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"ERROR: /api/health に到達できません: {exc}")
    if not health.get("ok"):
        raise SystemExit(f"ERROR: /api/health が異常です: {health}")
    say(2, f"health OK (serverTime {health.get('serverTime')})")


def verify_state_api():
    """launch token で /api/state が読めるところまで確認する。"""
    import importlib
    import nqx_state
    importlib.reload(nqx_state)          # 生成直後の設定を読み直す

    view = nqx_state.fetch_state_quiet()
    if view is None:
        raise SystemExit("ERROR: /api/state を読めませんでした(launch token の検証に失敗)")
    say(2, f"/api/state OK — symbol={view.get('symbol')} "
           f"priority={view.get('display', {}).get('priority')}")
    return nqx_state


# ---------------------------------------------------------------- 3. Pages

def resolve_pages_project():
    """既存の Pages プロジェクト名を確認する。

    URL の nqx-nightwatch.pages.dev から名前を推測しているだけなので、
    実際の一覧と突き合わせる。取り違えて別サイトへ上書きしないため。
    """
    code, out = wrangler(["pages", "project", "list"], timeout=300)
    if code != 0:
        say(3, "プロジェクト一覧を取得できませんでした。既定名で進めます")
        return PAGES_PROJECT
    names = set(re.findall(r"([a-z0-9][a-z0-9-]*)\.pages\.dev", out))
    names |= set(re.findall(r"^\s*│?\s*([a-z0-9][a-z0-9-]*)\s*│", out, re.M))
    if PAGES_PROJECT in names:
        return PAGES_PROJECT
    candidates = sorted(n for n in names if "nqx" in n or "nightwatch" in n)
    if len(candidates) == 1:
        say(3, f"プロジェクト名を {candidates[0]} と判定しました")
        return candidates[0]
    if candidates:
        raise SystemExit(
            "ERROR: Pages プロジェクトを一意に決められません: " + ", ".join(candidates) +
            "\n  setup_cloudflare.py の PAGES_PROJECT を明示してください")
    say(3, f"一覧に見当たりません。{PAGES_PROJECT} として進めます")
    return PAGES_PROJECT


def deploy_pages():
    say(3, "Mini App をビルドします")
    # Windows の subprocess は PATHEXT 経由で npm.ps1 を解決できない。
    # npm.cmd を明示すれば shell を使わず、安全に Vite を起動できる。
    npm = "npm.cmd" if os.name == "nt" else "npm"
    code, out = run([npm, "run", "build"], MINI_APP_DIR, timeout=900)
    if code != 0:
        print(out[-2500:])
        raise SystemExit("ERROR: vite build が失敗しました")

    project = resolve_pages_project()
    say(3, f"Pages プロジェクト {project} にデプロイします")
    code, out = wrangler(
        ["pages", "deploy", "dist", "--project-name", project, "--commit-dirty=true"],
        cwd=MINI_APP_DIR, timeout=900)
    if code != 0:
        print(out[-2500:])
        raise SystemExit("ERROR: wrangler pages deploy が失敗しました")
    urls = re.findall(r"https://[a-z0-9.-]+\.pages\.dev\S*", out)
    say(3, f"デプロイ完了: {urls[-1] if urls else '(URL を取得できず)'}")
    return out


def verify_pages(nqx_state):
    """本番 URL がローカルで検証した最新ビルドを返しているか確認する。

    ``resultView`` / ``stateStack`` の存在だけでは、どちらも含む古いビルドを
    最新と誤認する。Telegram WebView のキャッシュ対策も兼ね、ローカル dist の
    index.html 本文ハッシュと Pages が返す本文ハッシュを一致させる。
    """
    version = nqx_state.app_version()
    url = "https://nqx-nightwatch.pages.dev/?v=" + urllib.parse.quote(version or "verify")
    try:
        with open(nqx_state.DIST_INDEX, "rb") as local_file:
            local_html = local_file.read()
    except OSError as exc:
        say(3, f"ローカル dist/index.html を読めませんでした({exc})")
        return False

    try:
        # Cloudflare may reject urllib's default user agent even though the
        # Pages site is publicly available.  Use a normal browser-like agent
        # so this post-deploy check measures the same response the Mini App
        # receives rather than a crawler policy.
        request = urllib.request.Request(url, headers={
            "Cache-Control": "no-cache",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 Chrome/140.0 Safari/537.36",
        })
        with urllib.request.urlopen(request, timeout=25) as response:
            remote_html = response.read()
    except (urllib.error.URLError, OSError) as exc:
        say(3, f"確認できませんでした({exc})。反映に少し時間がかかることがあります")
        return False

    html = remote_html.decode("utf-8", "replace")
    has_result_view = 'id="resultView"' in html
    has_state_stack = 'id="stateStack"' in html
    local_hash = hashlib.sha256(local_html).hexdigest()[:12]
    remote_hash = hashlib.sha256(remote_html).hexdigest()[:12]
    exact = local_html == remote_html
    say(3, "本番 HTML: "
        f"resultView={has_result_view} stateStack={has_state_stack} "
        f"local={local_hash} remote={remote_hash} build={version}")
    if not exact:
        say(3, "Pages がローカルの最新ビルドと一致しません。CDN の反映を待って再確認してください")
    return exact and has_result_view and has_state_stack


# ---------------------------------------------------------------- 4. Telegram

def refresh_keyboard():
    say(4, "Telegram の常設キーボードを最新 URL で貼り直します")
    import importlib
    import telegram_bot
    importlib.reload(telegram_bot)

    cfg = telegram_bot.load_env()
    url = telegram_bot.mini_app_url(cfg)
    masked = re.sub(r"(t=)[^&]+", r"\1***", url)
    result = telegram_bot.send_webapp(
        cfg,
        "<b>☾ NIGHTWATCH</b>\n"
        "Mini App を最新ビルドに更新しました。\n"
        "下の <b>☾ OPEN NIGHTWATCH</b> から開いてください。",
    )
    if not result or not result.get("ok"):
        raise SystemExit(f"ERROR: Telegram への送信に失敗しました: {result}")
    say(4, f"送信しました: {masked}")


# ---------------------------------------------------------------- main

def main():
    parser = argparse.ArgumentParser(description="Cloudflare 一括セットアップ")
    parser.add_argument("--check", action="store_true", help="前提の確認だけ行う")
    parser.add_argument("--skip-keyboard", action="store_true", help="Telegram への貼り直しをしない")
    args = parser.parse_args()

    problems, authed = check_prerequisites()
    if problems:
        print("前提が揃っていません:")
        for problem in problems:
            print(f"  - {problem}")
        if args.check:
            return 1
        return 1
    say(0, "前提 OK(wrangler 認証済み)")
    if args.check:
        return 0

    worker_url = deploy_worker()
    configure(worker_url)
    nqx_state = verify_state_api()
    deploy_pages()
    verify_pages(nqx_state)
    if not args.skip_keyboard:
        refresh_keyboard()

    print()
    print("=" * 60)
    print("完了しました。")
    print(f"  Worker : {worker_url}")
    print("  Pages  : https://nqx-nightwatch.pages.dev/")
    print("  Bot    : ☾ OPEN NIGHTWATCH を押すと最新版が開きます")
    print()
    print("シナリオを表示するには、監視ループから monitor_publish.py を流してください。")
    print("建玉と決済結果には .secrets/tradovate.env の設定が要ります。")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())

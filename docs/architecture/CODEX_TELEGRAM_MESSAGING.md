# Codex 向け — Telegram メッセージの正しい送り方

対象: このリポジトリから Telegram へ何かを送る全てのコード。
最終更新: 2026-08-14

---

## 0. 大原則(これだけは外さない)

1. **Telegram API を自分で叩かない。** `telegram_bot.send()` か
   `telegram_bot.send_webapp()` を使う。
2. **本文に入れる外部由来の文字列は必ず `esc()` を通す。** `parse_mode` は HTML。
3. **送信の成否を確認する。** 失敗は `None` か `{"ok": false}` で返る。握り潰さない。

現状、Telegram API を直接呼んでいるのは `telegram_bot.py` の `api()` **1 箇所だけ**。
`monitor_publish.py` も `notify.py` も `send()` 経由になっている。この形を崩さないこと。

---

## 1. 使う関数

| 関数 | 用途 | 場所 |
|---|---|---|
| `send(cfg, text, keyboard=None)` | 通常の送信。inline ボタンを付けられる | [telegram_bot.py](telegram_bot.py) |
| `send_webapp(cfg, text)` | Mini App の**常設キーボード**を貼り直す | 同上 |
| `esc(s)` | HTML エスケープ。`& < >` を変換 | 同上 |
| `pre(s)` | `<pre>` で囲む(内部で `esc` 済み) | 同上 |
| `kb(*rows)` | `[("表示","callback_data"), …]` → inline_keyboard | 同上 |
| `deck_head(label, marker)` | 等幅の見出し | 同上 |
| `load_env()` | `.secrets/telegram.env` を読む | 同上 |

```python
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from telegram_bot import esc, kb, load_env, pre, send

cfg = load_env()
result = send(cfg, f"<b>見出し</b>\n{esc(user_text)}")
if not result or not result.get("ok"):
    raise SystemExit(f"ERROR: Telegram send failed: {result}")
```

### なぜ `send()` を経由するのか

- **送信先が固定される。** `send()` は `chat_id` を引数で受け取らない。
  必ず `.secrets/telegram.env` の `TELEGRAM_CHAT_ID` にだけ飛ぶ。
  ここを引数化すると、バグ 1 つで他人のチャットに注文情報が出る。
- **エンコードが揃う。** `api()` が `urlencode(..., encoding="utf-8")` してから
  ASCII bytes 化する。Windows のコードページに依存しない。
- **401 を検知する。** トークンが無効なら即座に終了して気付ける。
- **4096 文字で分割する。**(§3)

---

## 2. HTML エスケープ

`parse_mode="HTML"` なので、`&` `<` `>` が生のまま入ると Telegram は
**400 Bad Request を返し、メッセージは届かない**。

```python
send(cfg, f"<b>{esc(title)}</b>\n{esc(body)}")     # 正しい
send(cfg, f"<b>{title}</b>\n{body}")               # 危険
```

特に危ないのは外部由来の文字列。

- `order.py` の標準出力(`R:R = 1:2.09` のような `<` `>` を含みうる)
- ブローカーのエラーメッセージ
- 例外の文字列 `esc(str(exc))`
- シナリオの `reason` / `title`(モデルが生成した日本語・英語)

### 使えるタグ

`b` `strong` `i` `em` `u` `ins` `s` `strike` `del` `a` `code` `pre` `blockquote` `span`(spoiler)。
**これ以外は 400 になる。** `<br>` も `<p>` も `<div>` も使えない。改行は `\n`。

### `esc()` は属性値には足りない

`esc()` が変換するのは `& < >` だけで、引用符はそのまま。
`<a href="...">` を組み立てるなら URL に `"` が入らないことを保証すること。
URL は `urllib.parse.quote` を通すのが安全。

```python
from urllib.parse import quote
send(cfg, f'<a href="{quote(url, safe=":/?&=-_.")}">開く</a>')
```

---

## 3. 4096 文字の上限

`sendMessage` の本文上限は **4096 文字**。超えると 400 で丸ごと落ちる。

`send()` が `split_html_message()` で自動分割するようになった(2026-08-14 に追加)。
分割は行境界で行い、`<pre>` の途中で切れる場合は閉じてから次で開き直す。
キーボードは**最後の 1 通にだけ**付く。

そのため通常は意識不要だが、次は守ること。

- **`send()` を経由すること。** 自前で API を叩くと分割されない
- **`<pre>` を入れ子にしない。** 分割器はタグ数の偶奇で内外を追っている
- **長い生出力は `pre()` に入れる。** 行境界が無いと切り口が汚くなる

---

## 4. キーボードは 2 種類ある。混同しない

| | inline_keyboard | ReplyKeyboard (`send_webapp`) |
|---|---|---|
| 出る場所 | メッセージの直下 | 入力欄の下(常設) |
| 作り方 | `kb([("表示","callback_data")])` | `webapp_keyboard(cfg)` |
| 押した時 | `callback_query` が来る | `web_app_data` が来る |
| Mini App | 起動できる。**initData あり / `sendData` 不可** | 起動できる。**initData なし / `sendData` 可** |
| 用途 | 操作ボタン(PREVIEW / CONFIRM / FLATTEN) | ☾ OPEN NIGHTWATCH |

### 常設キーボードは「貼り直し」でしか変わらない

Telegram は **最後に `reply_markup` を含んだメッセージ**の内容でキーボードを固定する。
Mini App の URL を変えても、`send_webapp()` を呼ぶまでボタンは古い URL を指したまま。

```python
send_webapp(cfg, "Mini App を更新しました")   # これでボタンが差し替わる
```

Bot 起動時と `/app` コマンドがこれを行う。**デプロイしただけでは反映されない。**

### inline ボタンを押されたら必ず `answerCallbackQuery`

返さないとクライアントのローディング表示が回り続ける。
`main()` のループが既に行っているので、`handle_callback()` に処理を足す形にすること。

### web_app ボタンの URL は HTTPS 必須

`http://` は Telegram が拒否する。ローカル確認は
[telegram_mini_app/dev-telegram.html](telegram_mini_app/dev-telegram.html) のハーネスを使う。

---

## 5. 送信結果の確認

`api()` は失敗時に `None` を返す(例外は投げない)。

```python
result = send(cfg, text)
if not result or not result.get("ok"):
    # 失敗。成功したことにしない
    print(f"[warn] Telegram send failed: {result}", file=sys.stderr)
```

`monitor_publish.py` はここで `SystemExit` を投げている。監視ループが
「送ったつもりで送れていない」状態になるのを防ぐため。**この扱いを弱めないこと。**

---

## 6. レート制限

- 同一チャットへは **1 秒あたり 1 通** が目安
- 超えると 429 と `retry_after` が返る

3 分間隔の監視ループなら問題にならないが、ループの中で複数通に分けて送る場合は
`time.sleep(1)` を挟むこと。`send()` の自動分割も連続送信になるので、
極端に長い本文を高頻度で送らない。

---

## 7. 秘密を本文に載せない

- **Bot トークン、`CROSSTRADE_KEY`、`NQX_PUBLISH_SECRET`、`NQX_LAUNCH_SECRET`** は
  絶対に本文へ入れない
- `order.py` の出力をそのまま流すときは、`order.py` 側が既に
  `***KEY***` へ置換していることを前提にしている。この置換を外さない
- Mini App の URL を表示するときは launch token をマスクする

```python
masked = re.sub(r"(t=)[^&]+", r"\1***", url)
```

スクリーンショットは簡単に共有されるので、「自分のチャットだから安全」とは考えない。

---

## 8. 通知経路と発注経路を混ぜない

**Telegram へ送るコードから `order.py` を呼ばない。**

- `monitor_publish.py` は通知とプレビュー専用。`--confirm` を呼ぶコードを足さない
- 定期通知・テスト送信・Bot 再起動から発注処理を呼ばない
- 発注は「Mini App の一回押し → `handle_scenario_order_confirmed()` → `order.py --confirm`」
  だけ。この経路以外を作らない

これは [tests/test_oneclick.py](tests/test_oneclick.py) が機械的に検査している。
`monitor_publish.py` に `--confirm` の文字列が入っただけでテストが落ちる。

### 8.1 ライブ発注の前提 — broker の verified が必須

`NQX_LIVE_ORDERS=1` は送信許可のフラグであって、建玉が FLAT だという証明ではない。
次の条件が満たされない場合、Mini App の確認ボタン・Bot の確認・`order.py --confirm` は
すべて停止する。

- `CROSSTRADE_ACCOUNTS` の全口座を `query_position(..., account=...)` で照会し、
  すべて `verified=true` を返す
- 全口座で `qty=0` が返り、対象シンボルの FLAT を確認できる
- 既存ポジションがある場合は新規注文ではなく管理/決済だけを行う

照会不能を FLAT として通す設定は設けない。`order_log.json` や Durable Object の
「最後に見えた状態」は建玉の正本ではない。照会経路を設定する前にボタンを有効化しない。
送信後も receipt とブローカー側の建玉を別々に確認し、receipt だけで約定済みと表示しない。

---

## 9. 重複送信を防ぐ

`monitor_publish.py` は `fingerprint()`(`at` / `price` / `regime` / `scenarios`)で
直前の送信と比較し、同一なら送らない。状態は
`.secrets/monitor_last_sent.json` に持つ。

新しい通知を足すときも、**同じ内容を繰り返し送らない仕組みを必ず付ける。**
3 分ループで同じカードが並ぶと、本当に変わったときに気付けなくなる。

---

## 10. よくある失敗と原因

| 症状 | 原因 |
|---|---|
| 400 `can't parse entities` | `esc()` 忘れ。`&` `<` `>` が生で入っている |
| 400 `Unsupported start tag` | `<br>` `<p>` `<div>` など使えないタグ |
| 400 `message is too long` | 4096 文字超。`send()` を経由していない |
| 400 `BUTTON_TYPE_INVALID` | `web_app` の URL が https でない |
| 401 | トークンが違う。BotFather で再確認 |
| 403 `bot was blocked` | ユーザーが Bot をブロックしている |
| 429 | レート制限。`retry_after` 秒待つ |
| 何も起きない | `api()` が `None` を返している。戻り値を見ていない |
| 日本語が化ける | 自前で API を叩き、`encoding="utf-8"` を指定していない |
| ボタンが古いまま | `send_webapp()` を呼んでいない(§4) |
| ローディングが回り続ける | `answerCallbackQuery` を返していない |

---

## 11. 動作確認

```powershell
python telegram_bot.py --check
```

設定・疎通・`order.py --status` を確認してテストメッセージを 1 通送る。

送らずにロジックだけ見たい場合はテストを使う。`api` がスタブされているので
Telegram にも CrossTrade にも到達しない。

```powershell
python tests/test_oneclick.py
python tests/test_buttons.py
```

分割の確認:

```powershell
python -c "import telegram_bot as t; p=t.split_html_message('<pre>'+'x'*12000+'</pre>'); print(len(p), [len(x) for x in p])"
```

---

## 12. 監視通知のテンプレート

`monitor_publish.py` の形をそのまま踏襲すること。

```python
lines = [
    "<b>☾ NQX // PSYCHOSICK CHANNEL</b>",
    "<i>静寂の中で、価格だけが笑っている。</i>",
    f"<code>銘柄 {esc(str((bundle.get('snapshot') or {}).get('symbol', 'CME_MINI:MNQ1!')))} · 観測 {esc(str(bundle['at']))}</code>",
    f"<b>現在値 {float(bundle['price']):,.2f}</b>  <code>相場状態 {esc(str(bundle.get('regime','MX')))}</code>",
]
for key, scenario in (bundle.get("scenarios") or {}).items():
    lines.append(format_scenario(key, scenario))     # 中で esc 済み
lines.append("<i>状態 // 正本サーバー照合済み。通知は扉、注文は別の鍵。</i>")
lines.append("<i>※ CVDは観測値。妄想で注文フローには変換しない。</i>")

keyboard = [[{"text": "OPEN LIVE SCENARIO", "web_app": {"url": url}}]]
result = send(cfg, "\n".join(lines), keyboard)
if not result or not result.get("ok"):
    raise SystemExit(f"ERROR: Telegram send failed: {result}")
```

押さえどころ:

- 価格は `<code>` に入れる。等幅になり、桁が読み違えにくい
- `,.2f` で桁区切りと小数 2 桁を固定する
- **「注文は作られていない」旨を必ず入れる。** 通知を注文と誤読させない
- ボタンは inline に置く。常設キーボードを毎回書き換えない

---

## 13. 変えてはいけない約束

1. `send()` は `chat_id` を引数で受け取らない。送信先を可変にしない
2. `parse_mode` は HTML で固定。Markdown へ切り替えない(既存の本文が全て壊れる)
3. 通知経路から `order.py --confirm` を呼ばない
4. 送信失敗を成功として扱わない
5. 秘密を本文に載せない

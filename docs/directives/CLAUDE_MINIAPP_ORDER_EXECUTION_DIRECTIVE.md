# Claude Code 実装指示書

## NQX Mini App: シナリオ表示から安全な発注完了までの一式改修

発行目的: `nqx-nightwatch` のライブシナリオを Telegram Mini App に表示し、ユーザーが Mini App 上で明示確認したときだけ、既存の `order.py` → CrossTrade → Tradovate 経由で発注を完了できるようにする。

この指示書は、現在のシナリオ生成・Telegram通知・発注安全装置を壊さずに、連携経路を完成させるためのもの。見た目だけのモック、ブラウザからの直接Webhook、秘密情報のフロントエンド埋め込みは禁止。

---

## 0. 最初に必ず読むファイル

作業開始前に、以下を読み、内容を実装判断の基準にすること。

1. `TRADING_CONTEXT.md`
2. `CLAUDE.md`
3. `telegram_mini_app/README.md`
4. `monitor_publish.py`
5. `telegram_bot.py`
6. `order.py`
7. `project/app/nq-nightwatch-nqx-final.html`
8. `project/claude-skills/mnq-nightwatch-nqx/SKILL.md`
9. `project/claude-skills/mnq-nightwatch-nqx/references/nqx1-current.md`
10. `project/claude-skills/mnq-nightwatch-nqx/references/regime-gamma-policy.md`

既存の `buildTruthChart`、NQXシナリオ構造、`monitor_publish.py` のURL契約を削除・置換しないこと。

---

## 1. 絶対条件（安全・認証・発注）

- 実注文は、ユーザーの明示的な最終確認が完了した場合だけ許可する。
- 実装・テスト中に `order.py --confirm`、`order.py --market ... --confirm`、`--flatten --confirm` を実行しない。テストはドライラン、モック、または実際のCrossTrade送信を伴わない検証だけにする。
- ブラウザ/Mini AppからCrossTrade、Tradovate、`order.py`を直接呼ばない。
- `.secrets/crosstrade.env`、`.secrets/telegram.env`、Telegram bot token、CrossTrade keyをフロントエンド・公開JSON・URLクエリ・ログ・consoleに出さない。
- 価格・数量・方向・SL・TP・symbol・有効期限は、クライアント値を信用せず、サーバー側で再検証する。
- Telegram `initData` / `initDataUnsafe` を利用する場合は、サーバー側でTelegram公式の署名検証を行う。`user.id`またはchat idを allowlist と照合し、許可対象以外は拒否する。
- リプレイ攻撃を防ぐため、サーバー発行のnonce/action tokenを単回使用にする。tokenには最低限、`chat_id`、`scenario_fingerprint`、`symbol`、`side`、`qty`、`entry`、`sl`、`tp`、`issued_at`、`expires_at`、`nonce`を含める。
- tokenの有効期限は短くする（推奨90秒）。期限切れ、二重送信、シナリオ差し替え、チャート時刻の古さをすべて拒否する。
- 発注前に必ず `order.py --status` 相当の当日状態、発注回数、数量上限、推定リスク、方向、SL/TP順序を確認する。
- 1注文の推定リスクは `TRADING_CONTEXT.md` の上限（現在の運用値は $200）を超えないこと。MNQの1pt=$2、1tick=0.25pt、価格は0.25刻みに正規化する。
- `BUY`: `SL < entry < TP`、`SELL`: `TP < entry < SL`。成立しないものは発注不可。
- TPまたはSLがないシナリオは発注不可。`WATCH`は発注提案であり、ユーザーが最終確認するまで絶対に送信しない。
- 実注文成功後も、CrossTrade/Tradovateの応答をそのまま成功扱いにせず、HTTP結果・注文識別子・受信時刻・発注パラメータを監査ログへ保存する。
- UIの文言は「安全装置を迂回できる」印象を与えないこと。「ARM」は予約、「CONFIRM & SEND」は最終確認後の送信と明確に区別する。

---

## 2. 現在の構成と維持すべき契約

現状の主経路は次の通り。これを壊さずに改修すること。

```text
TradingView / monitor JSON
  -> monitor_publish.py
  -> Telegram message + Mini App URL (?monitor=<encoded bundle>)
  -> telegram_mini_app/app.js
  -> Telegram WebApp sendData
  -> telegram_bot.py
  -> order.py (まずdry-run)
  -> ユーザーの最終確認
  -> order.py --confirm
  -> CrossTrade -> Tradovate
```

既存コード上、以下の挙動を確認してから変更すること。

- `monitor_publish.py` は通知専用であり、`order.py`を呼ばない。
- `telegram_mini_app/app.js` は `?monitor=` のbundleを復元し、シナリオ行を書き換えている。
- Mini Appの現在の `REQUEST LIVE ORDER` は `scenario_order_request` を `sendData` している。
- `telegram_bot.py` の `handle_web_app_data()` は現状、サーバー側で検証して `/order ...` のドライランを作り、確認ボタンを返す設計である。
- `order.py` は `--confirm` なしでは送信しない。これを安全の最後の防波堤として残す。

---

## 3. 目標UX（必ず二段階確認）

「Mini Appのボタンを押せば発注が完了する」という要件は、誤タップ防止のため二段階に分ける。最終確認を省略した一発発注にはしない。

### Phase A: ARM / dry-run

1. シナリオカードに `ARM LONG` / `ARM SHORT` を表示する。
2. 押下後、Mini App内に以下を固定表示する。
   - symbol: `MNQU6`（環境設定で変更可能だが、クライアントから任意symbolを受け付けない）
   - side
   - qty
   - entry / stop / target
   - risk dollars
   - R:R
   - scenario timestamp / validity deadline
   - trigger / invalidation
   - `regime`, `cvd`, `missingInputs`（CVDから方向性を推論しない）
3. `REQUEST ORDER CHECK` を押すとTelegramへ `scenario_order_request` を送信する。
4. Botはドライランのみを実行し、ユーザーへ発注内容を返す。ここではまだライブ送信しない。

### Phase B: CONFIRM & SEND

1. ドライランが安全装置を通過した場合だけ、Mini AppまたはTelegram内に最終確認UIを出す。
2. 最終確認画面では、Entry / SL / TP / qty / risk / sideを省略せず再表示する。
3. ボタン文言は `CONFIRM & SEND LIVE ORDER` とし、単なる `SEND` や `BUY` にしない。
4. ユーザーが最終確認を押したときだけ `scenario_confirm_request` を送信する。
5. Botはtoken、署名、期限、チャットID、シナリオfingerprintを照合し、同じ注文を一度しか受け付けない。
6. 検証成功後に限り、サーバー側で `order.py ... --confirm` を一回だけ実行する。
7. 成功・拒否・期限切れ・重複の各結果をMini App/Telegramに明示する。

Telegramの `sendData` だけではMini Appへ非同期応答を戻せない場合がある。その場合は、既存のTelegram inline confirmationをフォールバックとして維持すること。安全性を落として一発送信へ変更してはならない。

---

## 4. 実装タスク

### A. 共通のシナリオ契約を作る

`monitor_publish.py`、`telegram_bot.py`、Mini Appで個別に解釈しないよう、共通スキーマまたは共有ヘルパーを作る。

必須フィールド:

```json
{
  "scenario_id": "string",
  "scenario_fingerprint": "sha256 string",
  "at": "ISO-8601 JST",
  "symbol": "CME_MINI:MNQ1!",
  "execution_symbol": "MNQU6",
  "resolution": "3",
  "state": "WATCH",
  "side": "BUY | SELL",
  "qty": 1,
  "entry": 0.0,
  "stop": 0.0,
  "target": 0.0,
  "riskDollars": 0.0,
  "probability": null,
  "trigger": "string",
  "invalidation": "string",
  "valid_until": "ISO-8601 JST",
  "source": "string",
  "missingInputs": []
}
```

実行symbolと表示symbolを混同しない。`CME_MINI:MNQ1!` はTradingView表示用、CrossTrade送信用は `MNQU6` とする。環境変数で切り替える場合もallowlist方式にする。

fingerprintは、少なくとも `at,symbol,side,qty,entry,stop,target,scenario_id` の正規化JSONからSHA-256を作る。表示テキストだけをfingerprintにしない。

### B. `monitor_publish.py` を拡張する

- 既存のbundle公開・重複スキップを保持する。
- シナリオごとに `scenario_id` と `scenario_fingerprint` を付与する。
- `.secrets/monitor_last_sent.json` と別に、発注候補のサーバー側状態を保存する（例: `.secrets/order_intents.json`）。
- 保存するのは最新候補と短期token情報だけ。Telegram URLに秘密を載せない。
- 送信メッセージのMini App URLは現行の `?monitor=` 契約を維持し、URL値から発注権限を与えない。

### C. `telegram_mini_app/app.js` を改修する

- 初期表示は既存のライブscenario bundleを使い、テスト固定scenarioを混ぜない。
- `scenario_order_request` と `scenario_confirm_request` を別payloadにする。
- `REQUEST ORDER CHECK` はドライラン要求だけ。
- 最終確認前に、side/qty/entry/SL/TP/risk/R:Rを再掲し、誤クリック防止の明示チェック（例: `I reviewed Entry / SL / TP`）を置く。
- `CONFIRM & SEND LIVE ORDER` は、ドライラン応答と一致するtokenなしでは有効化しない。
- `sendData` が使えない通常ブラウザでは、絶対にライブ発注できない状態を表示する。
- `window.NightwatchChart?.arm()` / `disarm()` の既存挙動を壊さない。
- `Telegram.WebApp.HapticFeedback` は補助演出に留め、成功判定に使わない。
- XSS対策として、シナリオ由来文字列を `innerHTML` に直接連結しない。既存表示を必要ならtextContentベースへ置き換える。

### D. `telegram_bot.py` を改修する

- `handle_web_app_data()` でpayload schema、署名、許可chat、期限、fingerprint、symbol、価格刻み、数量、risk、side、SL/TPの向きを検証する。
- `scenario_order_request` は必ずdry-run。`--confirm`を付けない。
- dry-run通過時に、単回使用tokenを発行し、サーバー側に保存する。
- `scenario_confirm_request` は、tokenの所有chat、fingerprint、価格、数量、期限、nonce、現在のintent状態を再照合する。
- 既に使用済みtoken、期限切れtoken、別scenarioへの差し替え、二重送信を拒否する。
- 検証通過後だけ `run_order([... , "--confirm"])` を一度だけ呼ぶ。例外・タイムアウト・HTTP非2xxは失敗扱いにする。
- 成功時は注文識別子、HTTP status、時刻を監査ログへ追加する。失敗時も理由を保存する。
- Mini App由来のリクエストと通常Botコマンドを同じ安全関数へ集約し、経路ごとに検証を弱めない。
- `PENDING`をグローバル一件だけで管理する場合は、chat_idとtokenを必ず結び付け、別ユーザーの押下で確定できないようにする。

### E. `order.py` は安全装置を維持・必要最小限だけ補強する

- `--confirm`必須、SL必須、数量上限、日次発注回数、リスク上限、SL/TP方向チェックを維持する。
- 可能なら `--intent-id` / `--idempotency-key` を受け取り、同じintentの再送を拒否する。
- 価格は0.25刻みに正規化し、CrossTrade payloadへ送る直前に再検証する。
- 既存の `--status`、`--cancel`、`--modify`、`--flatten` の意味を変えない。
- 実注文送信の前後で秘密キーをログへ出さない。

### F. UI側に状態を明示する

最低限、以下の状態を区別する。

```text
LIVE SCENARIO
ARMED / DRY-RUN PENDING
READY FOR FINAL CONFIRM
SENDING LIVE ORDER
ORDER ACCEPTED
ORDER REJECTED
EXPIRED / STALE
DUPLICATE BLOCKED
TELEGRAM ONLY / NOT CONNECTED
```

「テスト」「シミュレーション」「LIVE」を同じ色や同じボタンで表現しない。ライブ発注完了を示すのは、サーバーの検証済み成功応答だけにする。

---

## 5. 受け入れテスト（実注文なし）

以下をすべて自動テストまたは再現可能なdry-runテストで通すこと。実CrossTradeへ送信してはならない。

### 正常系

- 有効なBUY: `SL < Entry < TP`、qty=1、risk <= $200。
- 有効なSELL: `TP < Entry < SL`、qty=1、risk <= $200。
- Mini Appでライブbundleが表示され、テスト固定scenarioが混ざらない。
- ARM → dry-run → final confirmation の順で状態が遷移する。
- 最終確認なしでは `order.py --confirm` が一度も呼ばれない。

### 拒否系

- 通常ブラウザ、Telegram外、署名不正、allowlist外chat。
- token期限切れ、nonce再利用、同一intent二重送信。
- scenario fingerprint不一致、bundle差し替え、`WATCH`の期限切れ。
- `BUY`でSL>=EntryまたはTP<=Entry。
- `SELL`でTP>=EntryまたはSL<=Entry。
- qty=0、qty>2、risk>$200、価格が0.25刻みでない値。
- symbolがallowlist外、SL/TP欠落、NaN/Infinity、未来・不正なJST時刻。
- 当日発注回数上限到達、`order.py --status`で拒否条件。
- CrossTrade HTTP非2xx、タイムアウト、空レスポンス。

### 回帰系

- `monitor_publish.py` の既存Telegram通知・重複スキップが従来どおり動く。
- 既存の通常Bot `/order`、`/market`、`/modify`、`/flatten`、`/status` の安全挙動が変わらない。
- `project/app/nq-nightwatch-nqx-final.html` のチャート、`buildTruthChart`、シナリオ表示が壊れない。
- Mini Appを再読み込みしても、古いintentが勝手に再発注されない。
- サーバー再起動後も使用済みtokenと監査ログが保持される。

---

## 6. 検証コマンド

作業終了時に、実注文を送らない範囲で以下を実行する。

```powershell
cd "C:\Users\exexu\Downloads\nq-nightwatch-claude-code-handoff"

python -m py_compile order.py telegram_bot.py monitor_publish.py
python telegram_bot.py --check
python order.py --status

cd telegram_mini_app
npm run build
cd ..

# 受け入れテストはmock CrossTrade / dry-runのみ。
# --confirmは実行しない。
```

ブラウザ検証では、localhostまたはステージングを使い、Networkタブで次を確認する。

- CrossTrade URLがブラウザから直接呼ばれていない。
- bot token / CrossTrade keyがレスポンス・URL・JS bundleに存在しない。
- dry-run前にライブ送信が発生していない。
- 最終確認ボタンを押さない限り、サーバー側のlive routeが実行されない。
- 失敗時にUIが `ORDER ACCEPTED` と誤表示しない。

---

## 7. 完了報告の形式

実装完了時は、次を日本語で報告すること。

1. 変更ファイル一覧
2. Mini App → Bot → order.py → CrossTrade → Tradovate の実際の経路
3. 最終確認の位置と、未確認時に送信されないこと
4. token/nonce/idempotencyの実装方法
5. risk/qty/symbol/SL/TPの検証結果
6. 実注文を送らずに実施したテスト結果
7. 未解決の制約
8. ユーザーが実運用前に行う操作（Bot再起動、Pages/Workerデプロイ、Telegramでdry-run確認など）

完了報告で「実注文成功」と書けるのは、ユーザーが明示的に承認した実運用テストが実際に成功した場合だけ。コード検証やdry-run成功を実注文成功と混同しないこと。

---

## 8. 禁止事項

- 一発クリックで確認を省略する。
- URLのbase64 bundleやブラウザpayloadを認証情報として扱う。
- フロントエンドにCrossTrade/Telegram秘密情報を埋め込む。
- CVD、ローソク足、見た目のスコアから確率や注文許可を捏造する。
- stale/missing scenarioを発注可能として表示する。
- `monitor_publish.py`から直接注文する。
- 既存の`order.py`安全装置を迂回する別のHTTP送信経路を作る。
- テスト目的で`--confirm`を実行する。
- ユーザーの明示確認なしに実注文、変更、全決済を行う。

この指示書の最優先事項は、シナリオ表示の利便性ではなく、意図しない発注を構造的に不可能にすることである。

# Claude Code 実装指示書

## Mini App の発注確認を「一回の明示的なボタン」に統合する

### 目的

現在のフローは次の二段階になっている。

```text
Mini App のシナリオ選択
  → Telegram に注文リクエスト送信
  → Bot がドライラン
  → Telegram の CONFIRM & SEND ボタン
  → order.py --confirm
```

これを、ユーザーが価格とリスクを確認したうえで Mini App の明示的な発注ボタンを一度押すだけのフローに変更する。

```text
Mini App に注文内容を固定表示
  → ユーザーが CONFIRM & SEND ORDER を一度押す
  → サーバー側で全検証・ドライラン
  → 検証成功時のみ order.py --confirm
```

ボタンの一回押下をユーザーの明示的な承認として扱う。ページ表示、シナリオ受信、テスト通知、Bot 起動だけでは絶対に発注しない。

---

## 1. 作業開始前に必ず読むファイル

以下を読み、既存の契約・挙動を壊さないこと。

1. `C:\Users\exexu\Downloads\nq-nightwatch-claude-code-handoff\TRADING_CONTEXT.md`
2. `C:\Users\exexu\Downloads\nq-nightwatch-claude-code-handoff\order.py`
3. `C:\Users\exexu\Downloads\nq-nightwatch-claude-code-handoff\telegram_bot.py`
4. `C:\Users\exexu\Downloads\nq-nightwatch-claude-code-handoff\telegram_mini_app\app.js`
5. `C:\Users\exexu\Downloads\nq-nightwatch-claude-code-handoff\telegram_mini_app\README.md`
6. `C:\Users\exexu\Downloads\nq-nightwatch-claude-code-handoff\monitor_publish.py`

既存の `buildTruthChart`、NQX/MNQ の検証ロジック、`monitor_publish.py` の Telegram 配信契約を削除・置換しないこと。

---

## 2. 絶対条件

### 2.1 残す安全ゲート

- `order.py` の `--confirm` 必須条件を削除しない。
- `--confirm` なしの実行は常にドライランとする。
- 実注文は必ずバックエンドから `order.py ... --confirm` を通す。
- ブラウザから CrossTrade、Tradovate、Webhook を直接呼ばない。
- `.secrets` の値を Mini App に送らない。秘密情報を HTML、JavaScript、Telegram payload に入れない。
- `NQX_LIVE_ORDERS=1` が設定されていない場合は、ボタンを押しても送信せず `LIVE ROUTE LOCKED` を返す。
- 注文内容を受信しただけ、ページを開いただけ、シナリオが表示されただけでは発注しない。
- 自動ループ、タイマー、テスト通知、Bot 再起動から発注処理を呼ばない。
- ユーザーに見えない自動承認、固定文字列による偽の承認、`/confirm` の裏側での無断実行を作らない。

### 2.2 ボタンが承認になる条件

発注ボタンを押す直前に、Mini App 内へ次を明確に表示する。

- Symbol: `MNQU6`（現在の運用コンテキストに従う）
- Side: BUY / SELL
- Quantity
- Entry
- Stop Loss
- Take Profit
- 推定リスク額、R:R
- シナリオ発行時刻、失効時刻
- `SIMULATION` / `LIVE` の状態

ボタンのラベルは曖昧な `REQUEST LIVE ORDER` や `ARM` にせず、次のようにする。

```text
CONFIRM & SEND ORDER
```

発注後のボタンは即時 disabled にし、`SENDING...` と表示する。二重タップで二重発注できないこと。

---

## 3. 変更対象と実装方針

### 3.1 `telegram_mini_app/app.js`

現在の `sendDemo()` に相当する処理を、明示的な承認送信として整理する。

#### 変更内容

- `REQUEST LIVE ORDER` を `CONFIRM & SEND ORDER` に変更。
- ボタン押下時に `clientNonce`（暗号学的に推測しにくい一回限りの値）を付ける。
- payload の型は既存互換を壊さない形で、少なくとも次を含める。

```json
{
  "type": "scenario_order_confirmed",
  "scenario": {
    "symbol": "MNQU6",
    "side": "BUY",
    "qty": 1,
    "entry": 29630.00,
    "stop": 29580.00,
    "target": 29730.00
  },
  "clientNonce": "one-time-value",
  "source": "nqx-nightwatch-mini-app"
}
```

- payload 内の値を信用して発注しない。payload は Bot 側で再検証する。
- `sendData()` が利用できない通常ブラウザでは、発注せず「Telegram 内で開いてください」と表示する。
- 送信後はボタンを disabled にし、同じ payload を再送しない。
- 発注確認用の長いコマンドを画面へ表示しない。
- `DISCARD` は維持し、選択解除だけを行う。

### 3.2 `telegram_bot.py`

`handle_web_app_data()` を中心に、`scenario_order_confirmed` の専用処理を追加する。

#### 必須検証

1. Telegram の許可済み chat/user からのデータであること。
2. payload の型、symbol、side、qty、entry、SL、TP が存在し、数値が有限であること。
3. 価格を MNQ の 0.25 tick に正規化し、正規化後の値で再計算すること。
4. BUY は `SL < Entry < TP`、SELL は `TP < Entry < SL`。
5. qty は既存の安全上限以内、かつ現在の運用ルールの最大建玉以内。
6. 推定損失が `TRADING_CONTEXT.md` のリスク上限以内。
7. 1 日の発注回数、現在の建玉、シンボル、SL 必須条件を `order.py --status` 等で再確認すること。
8. シナリオの発行時刻・失効時刻を検証し、期限切れなら拒否すること。
9. `clientNonce` またはサーバー発行の scenario fingerprint を一回限りとして扱うこと。
10. 送信直前にもう一度 `order.py` のドライラン結果を確認すること。

#### 実行順序

```text
payload受信
  → 認証済みchat確認
  → nonce / 期限 / fingerprint確認
  → 数値・tick・方向・数量・リスクを再検証
  → order.py を --confirm なしでドライラン
  → ドライラン成功かつ NQX_LIVE_ORDERS=1 の場合のみ
  → order.py に --confirm を付けて一度だけ実行
  → 成否と receipt/log を Telegram に返す
```

既存の `PENDING` が二段階確認専用になっている場合は、Mini App の一回押し経路だけ専用関数に分離する。既存の `/order` → Telegram 確認ボタンの経路は、互換性のため残してよい。

#### 二重送信防止

- nonce/fingerprint を送信前に消費済みとして保存する。
- ネットワーク再試行、ボタン二重タップ、古い Telegram メッセージから同じ注文を再送できないこと。
- 保存先は既存の安全な `.secrets` 管理方式に合わせる。秘密情報をログへ出さない。
- 成功・拒否・期限切れを区別した監査ログを残す。

#### 応答

成功時:

```text
ORDER SENT
MNQU6 BUY 1
Entry / SL / TP: <exact prices>
Receipt: <broker or CrossTrade receipt>
```

拒否時:

```text
ORDER BLOCKED
<具体的な理由>
注文は送信していません
```

成功と表示するのは `order.py --confirm` と下流レスポンスが成功した場合だけ。ドライラン成功を発注成功と表示しない。

### 3.3 `order.py`

- `--confirm` 必須ゲートを削除しない。
- qty、SL、TP方向、日次回数、リスク上限、OCO ブラケットの既存チェックを維持する。
- Mini App 対応のために変更が必要な場合も、承認ゲートを弱める変更は禁止。
- CLI の既存仕様と `/order`、`/status`、`/flatten` の挙動を壊さない。

### 3.4 `monitor_publish.py`

- 監視通知は表示・プレビュー用途のままにする。
- 監視通知の送信、テスト通知、3 分ループから `order.py --confirm` を呼ばない。
- シナリオ fingerprint、発行時刻、期限を Mini App が利用できる場合は付加する。
- 既存の重複送信防止を壊さない。

### 3.5 シナリオのライフサイクルと過去シナリオ混入防止

Mini App に表示してよいのは、サーバーが「現在も成立している」と判定したシナリオだけとする。過去の Telegram 通知、ブラウザのキャッシュ、前回の JSON、古い fingerprint をそのまま再表示しない。

#### シナリオ表示の必須条件

表示前にサーバー側で次をすべて確認する。

1. `scenarioId` または fingerprint が存在する。
2. symbol が現在の対象 (`MNQU6`) と一致する。
3. 発行時刻、最終観測時刻、失効時刻が存在する。
4. 失効時刻を過ぎていない。
5. 最新の市場スナップショットに紐づいている。
6. trigger、invalidation、Entry、SL、TP が現行データで成立している。
7. 既に `INVALIDATED`、`EXPIRED`、`CANCELED`、`REPLACED`、`FILLED`、`CLOSED` になっていない。
8. 現在のポジションと矛盾する新規シナリオではない。

条件を一つでも満たさない場合はシナリオを表示せず、`NO ACTIVE SCENARIO` と表示する。最後に見えていたシナリオを安全策として残したり、過去のシナリオをフォールバック表示したりしない。

#### 更新・無効化ルール

- 新しい monitor snapshot を受信したら、旧 snapshot にだけ存在するシナリオを再利用しない。
- 同じ fingerprint の更新は最新の `issuedAt` / `observedAt` が新しいものだけ採用する。
- trigger 到達前に invalidation、期限切れ、対象レベル消失、データ不足が起きたら即時 `INVALIDATED` または `EXPIRED` に遷移する。
- 新しいシナリオが旧シナリオを置き換える場合、旧シナリオを `REPLACED` と記録してから表示対象から外す。
- ブラウザ再読込、Telegram 再オープン、Bot 再起動時は、保存された過去 payload ではなくサーバーの active state を再取得する。
- active state が取得できない、署名・時刻・fingerprint の検証に失敗する場合は、空状態にする。推測で表示しない。
- Mini App の画面には `ACTIVE UNTIL <JST>`、`ISSUED <JST>`、`SOURCE SNAPSHOT <JST>` を表示して、古い情報かどうかを確認できるようにする。

#### シナリオとポジションの分離

- シナリオが `FILLED` になったら、シナリオカードを「保有ポジション」へ移し、発注用のボタンを無効化する。
- 保有中は同じ方向の新規エントリーシナリオを重複表示しない。
- 反対方向シナリオは自動で発注可能にせず、`POSITION OPEN — MANAGEMENT ONLY` と表示する。

### 3.6 保有ポジションの永続表示

一度でも約定が確認されたポジションは、完全決済 (`FLAT`) がサーバー側で確認されるまで Mini App に表示し続ける。シナリオの期限切れ、ページ再読込、Bot 再起動、Telegram の再接続では消さない。

#### ポジション表示項目

保有中は、サーバーで取得した最新状態を使って次を表示する。

- Symbol
- Side (LONG / SHORT)
- Quantity
- Average Entry
- Current Price（取得できない場合は `STALE` と明示）
- Stop Loss / Take Profit
- Unrealized P&L（取得できない場合は `N/A`）
- Position state: `OPEN`, `PARTIAL`, `EXIT PENDING`, `CLOSED`, `STALE`
- 約定確認時刻と最終更新時刻（JST）
- 情報源 (`order.py --status` / broker receipt / CrossTrade response)

#### ポジション状態の正本

- ブラウザの localStorage、画面上のボタン状態、古いシナリオ payload をポジションの正本にしない。
- `order.py --status` または接続済みブローカーのポジション照会をサーバー側で定期的に行い、その結果で状態を更新する。
- 通信断や照会失敗時はポジションを消さず、最後の確認状態を `STALE` として残す。
- `FLAT` を確認できた場合だけ保有カードを閉じ、決済時刻・決済数量・receipt を監査ログへ記録する。
- 部分決済時は残数量を表示し、数量が 0 になるまでカードを閉じない。
- 再起動時は監査ログと broker status を突き合わせ、保有中ならカードを復元する。
- 保有ポジションがある間は、新規注文ボタンを無条件に有効化しない。既存ポジションとの数量、方向、リスクを再検証してから表示する。

#### ポジションの表示優先順位

画面上の優先順位は次の通りとする。

```text
OPEN / PARTIAL POSITION
  → EXIT PENDING
  → ACTIVE SCENARIO
  → NO ACTIVE SCENARIO
```

ポジションカードはシナリオカードより上に置き、保有中であることを常に明示する。ポジションが `STALE` の場合は赤またはアンバーの警告を出し、状態不明のまま新規発注を進めない。

---

## 4. テスト要件

実注文を発生させないテストを先にすべて実行すること。

### 必須テスト

1. ページを開いただけでは注文されない。
2. シナリオ通知を受信しただけでは注文されない。
3. ボタン表示時に Entry/SL/TP/Qty/Risk が表示される。
4. ボタンが `CONFIRM & SEND ORDER` の一つだけである。
5. 通常ブラウザでは `sendData` が動かず、注文されない。
6. `NQX_LIVE_ORDERS=0` ではボタン押下後も送信されない。
7. 期限切れ payload は拒否される。
8. 不正な価格、逆向き SL/TP、過大 qty、過大リスクは拒否される。
9. 同じ nonce/fingerprint の二回目は拒否される。
10. ボタン二重タップで一回しか注文経路が実行されない。
11. ドライラン失敗時は `--confirm` を呼ばない。
12. 成功時だけ `ORDER SENT` と表示される。
13. 既存の monitor publish、`/status`、`/order`、`/cancel`、`/flatten` のテストが通る。
14. 期限切れ・無効化・置換済みの過去シナリオが再読込後にも表示されない。
15. 最新 snapshot が取得できない場合に、古いシナリオをフォールバック表示しない。
16. `FILLED` シナリオが保有ポジションカードへ移る。
17. 保有ポジションがある間、ページ再読込・Bot再起動・Telegram再接続後もポジションが表示される。
18. 通信断・status照会失敗時にポジションが消えず `STALE` 表示になる。
19. 部分決済では残数量が表示され、数量0の `FLAT` 確認までカードが残る。
20. `FLAT` と receipt を確認した後だけポジションカードが閉じる。

テスト中は実際の `order.py --confirm`、CrossTrade送信、Tradovate送信を実行しないこと。実ルートの確認は、コード変更とドライランテストが完了した後、ユーザーが別途明示的に承認した場合に限る。

---

## 5. 完了条件

- Mini App のユーザー体験が「内容確認 → `CONFIRM & SEND ORDER` 一回押し」になっている。
- 二回目の Telegram 確認ボタンや長いコマンド貼り付けが不要になっている。
- ただし、ユーザーのボタン押下がない限り実注文は発生しない。
- `order.py --confirm`、リスク制限、SL 必須、日次回数制限、`NQX_LIVE_ORDERS` ロックが残っている。
- 監視・テスト通知・ページ表示から発注されない。
- 変更ファイル一覧、テスト結果、未実施の実注文テストを報告する。
- デプロイや実口座送信は、別途指示がない限り行わない。

この指示書の目的は確認操作を一回に減らすことであり、安全ゲートを無効化することではない。

# ULTRA mode — 口座別の必要枚数へ強制上書きする発注モード

Machine-readable source: `ultra_mode.py`（正本の計算） /
`telegram_mini_app/ultra.js`（表示側の同一式）

## 何をするモードか

**ULTRA OFF** — 通常シグナルに設定された枚数をそのまま使う。

**ULTRA ON** — 通常の枚数は使わない。各口座の利益目標と残ドローダウンから
必要枚数を再計算し、その枚数へ強制的に上書きして口座別に発注する。

```
通常シグナルの枚数 → 無視 → 口座ごとに必要枚数を再計算
                            → 計算後の枚数へ強制上書き → 口座別に発注
```

## 計算式

```
qty            = ceil(profitTarget / (tpPoints * pointValue))
projectedProfit = qty * tpPoints * pointValue
projectedLoss   = qty * slPoints * pointValue
verdict         = ELIGIBLE  (projectedLoss <= 残ドローダウン)
                  INELIGIBLE(それ以外)
```

MNQ は `pointValue = $2.00`（`execution_contract.json` の `risk.pointValue`）。

判定は **利益目標と残ドローダウンだけ**で決める。1トレードのリスク上限
(`RISK_<口座>`) は ULTRA では判定に使わない — ULTRA は定義上その上限を
超える枚数を出すモードだから。ただし超過している事実は
`contractBlockers` に必ず出す。

## サンプル

方向 SHORT / Entry 30,126.00 / SL 30,147.00 / TP 30,076.00
（SL幅 21pt・TP幅 50pt・RR 2.38）

| 口座 | 利益目標 | 必要枚数 | 想定利益 | 想定損失 | 判定 |
|---|---|---|---|---|---|
| APEX-01 | $3,000 | 30枚 | $3,000 | $1,260 | ELIGIBLE |
| APEX-02 | $6,000 | 60枚 | $6,000 | $2,520 | ELIGIBLE |
| APEX-03 | $9,000 | 90枚 | $9,000 | $3,780 | ELIGIBLE |

`tests/test_ultra_mode.py` がこの表を1行ずつ固定している。

## 設定

`.secrets/crosstrade.env` に口座ごとに1行足すだけ。設定ファイルは増やさない。

```text
CROSSTRADE_ACCOUNTS=APEX-01,APEX-02,APEX-03
LIFELINE_APEX-01=2500      # 撤退ラインまでの残り $(既存)
RISK_APEX-01=180           # 1トレードのリスク上限 $(既存)
TARGET_APEX-01=3000        # 利益目標 $(ULTRA 用・新規)
```

`TARGET_*` は任意項目。未設定の口座は枚数を計算できないので
`PROFIT_TARGET_MISSING` で INELIGIBLE になる。**推測して枚数を作らない。**

値は `nqx_state.build_accounts_payload()` → Durable Object の `account`
stream → Mini App の順に流れる。口座条件の正本は1か所だけ。

**枚数の根拠は TP1 基準で 3 経路(Mini App / Bot / auto)を揃える(R52, 2026-09-05 ユーザー決定)。**
`ultra_mode.required_qty_split` は runner を渡せば R30 の分割式(両脚到達で目標)になるが、
Mini App(`ultraPanel` は `target` だけ渡す)と Bot(`signal_from_scenario` は TP1 だけ)は
runner を渡さないので、実効的には「全枚数が TP1 距離で決済されて目標に届く枚数」。auto
経路(`monitor_publish._apply_ultra_prefs`)だけが runner を渡していて、同じ幾何で app 18枚 /
auto 8枚に割れた。auto も `target` だけ渡すように変えた(runner の目標価格は脚に残る)。

**Mini App の口座別設定(`accountPrefs.profitTarget`)は env の `TARGET_*` より優先し、
毎サイクルの `publish_accounts` もその値で口座行を組む(R52, 2026-09-04)。** それまで
残機 publish だけが env の値で出ていたため、アプリの ULTRA パネル(`accounts.list[].profitTarget`
から計算)と engine(`_apply_ultra_prefs`)で枚数が食い違った(設定 $1,500 → engine 6枚 /
表示 10枚)。回帰: `tests/test_r52_accounts_publish_prefs.py`。

## どこで計算されるか

| 層 | 役割 |
|---|---|
| `ultra_mode.py` | **正本**。Bot が発注前に必ずここで再計算する |
| `telegram_mini_app/ultra.js` | **表示専用**。同じ式を画面で見せるだけ |
| Mini App の送信 | `ultraMode: true` という **意図だけ**を送る |

Mini App が送った枚数は一切使わない。`telegram_bot._ultra_order_gate()` が
正本の口座設定から組み直す。既存の「Mini App の値は信用しない」契約と同じ。

`tests/test_ultra_mode.py` の `test_python_and_mini_app_agree_exactly` が
両実装を実際に走らせて全フィールドを突き合わせる。`ultra.js` にハードコード
した契約定数も `execution_contract.json` との乖離を機械で止めている。

## ULTRA エンベロープ（実行契約）

通常経路（2枚固定・$240・単一口座）は**一切緩めていない**。ULTRA は
`execution_contract.json` の `ultra` セクションという**別のエンベロープ**で
判定され、明示宣言されたときだけ使われる。既定で緩む経路は作らない。

```json
"ultra": {
  "enabled": true,
  "requiresExplicitMode": true,
  "minQtyPerAccount": 2,
  "maxQtyPerAccount": 100,
  "maxTotalQty": 300,
  "maxAccounts": 8,
  "riskCapSource": "ACCOUNT_DRAWDOWN_BUFFER",
  "maxRiskDollarsPerAccount": 5000.0,
  "maxRiskDollarsTotal": 12000.0,
  "splitPlan": { "legCount": 2, "allocation": "PROPORTIONAL", "runnerRemainder": true }
}
```

リスク上限の正本は **その口座の残ドローダウン**。1トレード上限 (`RISK_*`) は
ULTRA では判定に使わない — ULTRA は定義上それを超えるモードなので、当てると
必ず全口座が不可になる。ただし残ドローダウンの設定漏れは通さない。

1口座でも上限を超えたら**計画全体**を止める。部分的に出すと、どの口座が
入ったのか分からないまま建玉が残る。

### 比率分割

```
30枚 → TP1 15 / RUNNER 15
 9枚 → TP1  4 / RUNNER  5   (端数は runner へ寄せる)
```

`execution_contract.ultra_split()` と Durable Object の `ultraSplit()` が同一
結果を返すことを試験で固定している。

## 開通済みの層と、残る唯一の関門

| 層 | 状態 |
|---|---|
| ULTRA エンベロープ（Python / Durable Object） | 開通・パリティ試験あり |
| 比率分割（`ultra_split` / `ultraSplit`） | 開通・パリティ試験あり |
| `execution_intent` の脚枚数（総枚数から一意に導出） | 開通・hash パリティ試験あり |
| `ownership_binder` の枚数ライフサイクル | 0 / TP1脚 / RUNNER脚 / 合計 へ一般化 |
| R22 broker observation の建玉枚数上限 | ULTRA 上限まで観測可能 |
| 経路スナップショット行数 | 口座数 × 2脚（16行）まで |
| `order.py` の ULTRA 経路 | `--ultra` / `--ultra-drawdown` で開通 |
| `order.py` のドライラン表示・門(R52, 2026-09-04) | 注文行・R:R が比率分割の枚数で出る。ULTRA エンベロープ検査(残DD / 枚数上限 / 分割)は `--confirm` の前で効く。成行は `--last`+滑り緩衝で想定損失を出す。回帰: `tests/test_r52_ultra_dryrun_display.py` |
| 自動管理（runner 脚の判定） | 比率分割の枚数に対応 |
| **複数口座への同時ルーティング** | **未開通** |

残る関門は ENTRY claim の同一性ひとつ。claim key は scenario tuple
(`scenarioId + fingerprint + evidenceHash + marketCycleId`) から導出されるので、
同じシグナルで2口座目を CLAIM すると `ALREADY_HELD` で弾かれ、さらに RECOVER
後の tombstone が恒久化する。

claim key へ口座次元を入れる変更は「二重発注を防ぐ」中核不変条件そのものなので、
専用の設計と試験を通してから開ける。それまで `_ultra_order_gate()` は
`ULTRA_CLAIM_SCOPE_PENDING` で止まり、口座別の内訳と理由を全部返す。

```text
ULTRA_CLAIM_SCOPE_PENDING: 計算枚数 合計180枚 は実行契約を通ります
APEX-01: 30枚 利益 $3,000 / 損失 $1,260 ELIGIBLE
APEX-02: 60枚 利益 $6,000 / 損失 $2,520 ELIGIBLE
APEX-03: 90枚 利益 $9,000 / 損失 $3,780 ELIGIBLE

ENTRY claim key が口座次元を持たないため、複数口座への同時ルーティングだけが
未開通です。1口座ずつの claim 分離が入るまで送信しません。
```

黙って握り潰さないことが要点。「なぜ出ないのか」が分からないまま止まるのが
一番危ない。

### 単一口座なら今の経路で出せる

`order.py --ultra --ultra-drawdown <残DD> --qty <枚数>` は1口座ぶんの ULTRA
発注として完成している。claim も intent も所有権も ULTRA 枚数で通る。
複数口座を同時に出す部分だけが残っている。

## デモで触る

発注経路を塞いだまま、ULTRA と自動発注の見え方を一通り試せる。

```bash
npm run dev --prefix telegram_mini_app
```

`http://localhost:5173/?demo=1`（従来の `#demo` と 5タップの隠しジェスチャも有効）。

デモのシグナルは仕様例そのもの（SELL 30,126 / SL 30,147 / TP 30,076）。口座は
5つ用意してあり、ULTRA を ON にすると全パターンが一画面で見える。

| 口座 | 残DD | 利益目標 | 枚数 | 想定損失 | 判定 |
|---|---|---|---|---|---|
| APEX-01 | $2,500 | $3,000 | 30枚 | $1,260 | ELIGIBLE |
| APEX-02 | $3,000 | $6,000 | 60枚 | $2,520 | ELIGIBLE |
| APEX-03 | $5,000 | $9,000 | 90枚 | $3,780 | ELIGIBLE |
| APEX-04 | $800 | $9,000 | 90枚 | $3,780 | INELIGIBLE（残DD超過） |
| **APEX-05** | **$4,000** | $9,000 | 90枚 | $3,780 | ELIGIBLE（残DD の 94.5%） |

APEX-05 は残ドローダウン $4,000 の架空口座。**ぎりぎり通る境界**を踏めるように
してある（あと $220 減れば INELIGIBLE に落ちる）。合計は 270枚 / 想定損失
$11,340 で、ULTRA エンベロープの合計上限（300枚 / $12,000）に迫る状態も同時に
確認できる。

| 操作 | 見えるもの |
|---|---|
| 設定タブの ULTRA トグル | 口座別の枚数・想定利益・想定損失・判定 |
| スライドして発注 | 本番なら何枚がどの口座へ行くか（脚の内訳付き） |
| 自動発注パネルを叩く | IDLE → CLAIMED → ROUTING → MANAGING → MODIFY を一巡 |
| SCENE セレクタ | 武装シナリオ ⇄ 残った注文（解除導線のリハーサル） |

デモ中の確定操作はこう出る。**送信は行われない。**

```text
デモ ULTRA — APEX-01 30枚(15/15) · APEX-02 60枚(30/30) · APEX-03 90枚(45/45)
           / 合計 180枚 · 想定損失 $7,560.00 · 除外 APEX-04
```

`confirmOrder()` はデモ判定で必ず `return` し、`tg.sendData` へ到達しない。
`telegram_mini_app/test/ultra.test.mjs` が「デモ遮断が送信より前にあること」
「送信の出口が2か所のままであること」をソース単位で固定している。

## アプリ側

### 設定タブ

実行モードの切り替えは **設定タブ**（ドックの3つ目）にまとめてある。

- `ONE-PASS` — A/A+ を一度だけ合否判定し、PASS のときだけ AUTO-EXECUTE を出す
- `ULTRA` — 通常枚数を無視し、口座別の計算枚数へ強制上書きする

選択は `localStorage` の `nqx.modes.v1` に保存され、**開き直しても維持される**。

保存するのはこの2つの真偽値だけ。建玉・注文・シナリオ・市況の正本は今までどおり
毎回サーバーから取り直す（「ブラウザに正本を持たせない」原則は変えていない）。
`telegram_mini_app/test/settings.test.mjs` が localStorage の使用箇所が2つのまま
であることと、取引状態を保存していないことをソース単位で固定している。

ULTRA を ON のまま開き直すと ON のままになる。桁が変わる設定が黙って復活すると
危ないので、次の3つで必ず見えるようにした。

1. 起動時に「ULTRA は ON のまま復元されました」と通知する
2. ヘッダーに `ULTRA` バッジを出し続ける（設定タブに隠れても監視画面から見える）
3. 設定画面に持ち越しの注意書きを常設する

### 監視画面

- ULTRA が ON の間、シナリオカードに口座別の枚数・想定利益・想定損失・判定を表示
- ELIGIBLE でも routing 不可なら理由を各行に出す
- `自動発注` パネルは autotrade の現在地（IDLE / CLAIMED / ROUTING /
  MANAGING / MODIFY）をサーバー状態から導出して表示する。
  **この画面から autotrade を武装することはできない** — 武装は運用機の
  `NQX_AUTOTRADE=1` + `NQX_LIVE_ORDERS=1` が正本で、遠隔から有効化できて
  はいけない

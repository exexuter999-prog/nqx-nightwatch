# Codex 向けタスク: 価格の上に波形チャート・VWAP・主要レベルを出す

対象: [telegram_mini_app/](telegram_mini_app/)

現状ミニアプリの市況は**ハードコードされた静止テキスト**しかない。
`29,841.00 / VWAP 29,702.78 / CVD -4,323` は index.html に直書きの飾りで、
どこからも供給されていない。

ここに**3分足の波形・VWAPバンド・主要レベル**を出し、
スマホで開いた瞬間に状況が掴めるようにしたい。

---

## ⚠️ 最優先: 壊してはいけないもの

### 1. 発注系に一切触らない

`app.js` の以下は**変更禁止**。

- `inTelegramRuntime()` / `setBridgeState()` — Telegram 判定
- `sendDemo()` の `tg.sendData()` 経路
- `scenarios` オブジェクトの価格
- 安全表示(`.safety-bar` / `NO LIVE ROUTE` / `SEND is simulated`)

**このアプリは実発注しない。** チャートを足したことで
「本番っぽく」見せてはいけない。`SIMULATION` / `LOCKED` の表示は残す。

許可する app.js の差分は**次の2箇所・各1行だけ**(詳細は後述):

```js
function renderPreview(key) {
  selected = scenarios[key];
  scene3d.setMode(key);
  window.NightwatchChart?.arm(selected);   // ← 追加してよい
```
```js
function discardPreview() {
  selected = null;
  scene3d.setMode("neutral");
  window.NightwatchChart?.disarm();        // ← 追加してよい
```

### 2. 古いデータを「今」に見せない

**これが本タスクで一番重要な安全要件。**

ユーザーは実口座で運用しており、[CLAUDE.md](CLAUDE.md) には
「`pane_list` は信用しない(実害あり)」「10分以上古いと警告」と
明記されている。**止まった指標を根拠に使って損失を出した実績がある。**

チャートは黙って古い線を描いてはいけない。

| データ鮮度 | 表示 |
|---|---|
| 2分未満 | 通常表示 |
| 2〜10分 | 右上に `3分前` を淡色で常時表示 |
| **10分以上** | **チャート全体を減光(opacity .35)+ `STALE 14分前` を rust で表示** |
| 取得失敗 / bars 無し | **チャートを描かない。** `NO FEED` とだけ出す |

**欠損を補間しない。** 足が飛んでいるなら飛んだまま描くか、描かない。
「それらしい線」を作るくらいなら何も出さないほうが安全。

### 3. 3D背景のレイアウト規約を壊さない

[CODEX_MINIAPP_SCENE_HANDOFF.md](CODEX_MINIAPP_SCENE_HANDOFF.md) を先に読むこと。

背景の天秤は `.app-shell` の下端を実測してその下のバンドに配置している。
**市況バンドが高くなると自動で追従する**(`ResizeObserver` 実装済み)ので
基本は何もしなくてよい。ただし:

- チャートの高さが**アニメーションで変化する実装にしない**
  (毎フレーム再レイアウトが走る)。出す/消すの2状態だけにする
- 1230×1517 で市況バンドが +110px 高くなると、背景バンドは
  555px → 約445px に縮む。ここは想定内
- **バンドが260px を切ると背景は「クロップ表示」に切り替わる。**
  チャートを高くしすぎるとウィンドウが低い環境でそこに落ちる

---

## 置き場所

`.market-band` の中、`.market-head` と `.price-line` の**間**に挿入する。

```
┌─ .market-band ────────────────────────────┐
│  INSTRUMENT  MNQ · CME        ● SIMULATION │  ← .market-head(既存)
│                                            │
│  ┌────────────────────────────────────┐   │
│  │  ここに新規 .tape-chart             │   │  ← 追加
│  └────────────────────────────────────┘   │
│                                            │
│  29,841.00  +12.25 (+0.04%)                │  ← .price-line(既存)
│  VWAP 29,702.78  CVD -4,323  REGIME TREND  │  ← .market-meta(既存)
└────────────────────────────────────────────┘
```

実測済みの寸法(1230×1517):

| 要素 | x | y | w | h |
|---|---|---|---|---|
| `.market-band` | 273 | 148 | 684 | 201 |
| `.market-head` | 290 | 166 | 650 | 43 |
| `.price-line` | 290 | 229 | 650 | 78 |

チャート描画領域: **幅650 × 高さ96**(デスクトップ) / **高さ76**(≤460px幅)。
うち**右56pxはラベル用のガター**として空ける。

---

## データ契約

### 供給元

[snapshot.py](snapshot.py) を拡張する。PC側の Claude Code が
分析のたびに TradingView MCP から取って書き出す運用になっている。

現在の `.secrets/snapshot.json`:

```json
{
  "at": "2026-08-12 02:17:22",
  "price": 29584.75,
  "vwap": 29702.78,
  "vwap_lo": 29597.6,
  "vwap_hi": 29807.96,
  "cvd": -4323.0,
  "note": "TP執行後も下落継続。本日安値更新中。"
}
```

### 拡張後(このタスクで定義する形)

```json
{
  "at": "2026-08-12 02:17:22",
  "symbol": "MNQ1!",
  "resolution": "3",
  "price": 29584.75,
  "vwap": 29702.78,
  "vwap_lo": 29597.60,
  "vwap_hi": 29807.96,
  "cvd": -4323.0,
  "note": "TP執行後も下落継続。本日安値更新中。",

  "bars": [
    { "t": 1786480200, "o": 29610.25, "h": 29618.00, "l": 29601.50, "c": 29604.75 },
    { "t": 1786480380, "o": 29604.75, "h": 29609.25, "l": 29588.00, "c": 29591.50 }
  ],

  "levels": [
    { "kind": "vwap",  "price": 29702.78, "label": "VWAP" },
    { "kind": "level", "price": 29560.00, "label": "PDL" },
    { "kind": "zone",  "price": 29636.00, "to": 29647.00, "label": "合流帯" }
  ]
}
```

- `bars`: **3分足・直近60本**(=3時間)。`v` は任意、チャートでは使わない
- `t`: UNIX秒
- `kind`: `"vwap"` / `"level"` / `"zone"` の3種。`zone` のみ `to` を持つ
- `vwap_lo` / `vwap_hi` は `levels` に入れず専用フィールドのまま
  (バンドとして塗るため)
- **すべてのフィールドが欠けうる。** 1つ欠けても描画が落ちないこと

### snapshot.py 側の追加

```bash
python snapshot.py --write --price 29584.75 --vwap 29702.78 \
    --vwap-lo 29597.60 --vwap-hi 29807.96 --cvd -4323 \
    --bars bars.json --levels levels.json --note "下落継続"
```

`--bars` / `--levels` はファイルパスを取る(コマンドラインに60本は乗らない)。
**既存の引数と `read()` の出力書式は変えないこと。** Bot の `/price` が使っている。

### 配信経路

`.secrets/` は**絶対に公開しない**([CODEX_BOT_TASK.md](CODEX_BOT_TASK.md) の規約)。
`.secrets/snapshot.json` を静的ホストに置く実装にしないこと。

このタスクの範囲では以下でよい:

1. ミニアプリは `./market.json` を `fetch()` する
2. 見つからない/壊れている → **フォールバックせず `NO FEED` 表示**
   (index.html の現在のハードコード値はそのまま残す。数字は消さない)
3. dev では `telegram_mini_app/public/market.json` にサンプルを置いて開発する

`.secrets/snapshot.json` → `public/market.json` への publish は**別タスク**。
経路を勝手に作らないこと。サンプルの `market.json` は**必ずダミー値**にし、
実際の口座データをコミットしない。

---

## 描画仕様

### ライブラリを入れない

**手書きの SVG で実装すること。** 理由:

- バンドが既に 641kB(three.js)。これ以上重くしたくない
- 必要なのは折れ線1本・水平線数本・矩形2〜3個だけ
- 既存の CSS 変数(`--acid` / `--rust` / `--violet` / `--bone` / `--muted` / `--line`)で
  テーマを揃えたい。Canvas だとこれができない
- index.html の importmap フォールバックを壊したくない

`<svg>` の `viewBox` は**実測ピクセルと一致**させ、`ResizeObserver` で
再描画する。`preserveAspectRatio="none"` で引き伸ばすと足の形が歪むので禁止。

### Y軸スケール

```
range = bars の high/low 全体 ∪ [vwap_lo, vwap_hi]
上下に 6% パディング
```

**`levels` はスケール決定に含めない。** 遠いレベルが1本あるだけで
値動きが潰れる。レンジ外のレベルは端に寄せて `↑` `↓` を付ける。

### X軸

**時刻ではなく本数で等間隔に置く。** 立会時間の切れ目で
不自然な空白が出るのを避けるため。

### 各要素

| 要素 | 描画 |
|---|---|
| 波形 | 終値の `polyline` / 1.5px / `--bone` |
| 直近値 | 3px の点。前足比で `--acid`(上) or `--rust`(下) |
| VWAP | 実線 1px / `--violet` / opacity .55 |
| VWAPバンド | `vwap_lo`〜`vwap_hi` を塗り / `--violet` / opacity .06 |
| level | 破線 1px / `--muted`、右ガターに 9px のラベル |
| zone | 矩形塗り / `--bone` / opacity .08 |

ラベルは既存の `.code-label` と同じ字送り(`font-size:9px; letter-spacing:.16em`)。

### シナリオを ARM したときのオーバーレイ ← ここが本題

`ARM LONG` / `ARM SHORT` を押したら、そのシナリオの3本をチャートに重ねる。
**背景の天秤が LONG/SHORT で傾くのと同時に、チャート上でも
Entry/SL/TP の位置関係が見える**のが狙い。

| | 描画 |
|---|---|
| ENTRY | 実線 1.5px / LONG は `--acid`、SHORT は `--rust` |
| STOP | 破線 1px / `--rust` + 右ラベル `SL` |
| TARGET | 破線 1px / `--acid` + 右ラベル `TP` |
| 損益帯 | Entry〜TP を薄く塗る(opacity .07) |

**ARM 時だけスケールを再計算してよい**(Entry/SL/TP が
レンジ外に出ると意味がないため)。切り替えは 240ms のイーズで。

`DISCARD` でオーバーレイを消す。

---

## 実装の形

新規 `telegram_mini_app/chart.js`(ESモジュール)。

```js
export function initTapeChart(mountEl);
// → { update(data), arm(scenario), disarm(), destroy() }
```

`app.js` の先頭で import し、`window.NightwatchChart` に載せる。
scene3d.js と同じく、**外から触るAPIは最小限に**。

ポーリング: `market.json` を **30秒間隔**で再取得。
`document.visibilityState !== "visible"` の間は止める(バッテリー)。

---

## 動作確認

```bash
cd telegram_mini_app
npm install
npm run dev -- --port 8771
```

`.claude/launch.json` に `nightwatch-mini-app`(port 8771)を定義済み。

### 必ず通すこと

1. **`npm run build` が通る**
2. **ブラウザコンソールエラー 0件**
3. **1230×1517 で背景の天秤がコンソールに食い込まない**

   ```js
   window.Nightwatch3D.screenBounds().layout.clearance   // 常に正であること
   ```

   neutral / ARM LONG / ARM SHORT / SEND DEMO / DISCARD の
   全状態で確認する(市況バンドが伸びると背景が再レイアウトされるため、
   ここが本タスクで最も壊れやすい)

4. **スマホ幅375** でチャートが潰れず、価格が読めること
5. **データ異常系**:
   - `market.json` を消す → `NO FEED`、既存の数字は残る
   - `bars` を空配列にする → チャート非表示、落ちない
   - `at` を30分前にする → **減光 + STALE 表示**
   - `levels` に極端な外れ値(例 25000)→ 端に寄って波形が潰れない
6. **`prefers-reduced-motion`** でオーバーレイのトランジションが無効

### 触っていないことの確認

```bash
cd tests
python test_buttons.py      # ★ 全チェック通過 が出ること
```

ミニアプリは Bot と独立だが、`snapshot.py` を変更するので
`/price` の出力が壊れていないことを確認する:

```bash
python snapshot.py          # 従来どおりの書式で読めること
```

---

## やらないこと

- **ローソク足の描画**(この幅では潰れる。終値ラインで十分)
- **十字カーソル・ツールチップ・ピンチズーム**
  スマホで誤タップの元。見るだけの図でよい
- **時間軸のラベル**(60本=3時間と決め打ちなので不要)
- **CVD のサブチャート**(数値は `.market-meta` に既にある)
- **WebSocket / リアルタイム配信**(30秒ポーリングで十分)
- **チャートからの発注**(絶対に作らない)

---

## 補足: なぜ鮮度表示にここまでこだわるのか

ユーザーは過去に口座を複数回破綻させており、
2026-08-07 には2時間で66約定・24枚まで膨らませて失格している。

また [CLAUDE.md](CLAUDE.md) §1 には、TradingView の `pane_list` が
嘘をついて15分足を3分足として読んでいた事故、
指標が同一値で停止していたのに気づかず使いかけた事故が記録されている。

**「動いていないのに動いているように見えるチャート」が一番危険。**
このミニアプリはスマホから見る=チャートを別途確認できない状況で使う。
迷ったら「描かない」を選ぶこと。

---

## 関連ファイル

| ファイル | 役割 |
|---|---|
| [telegram_mini_app/chart.js](telegram_mini_app/chart.js) | **新規作成** |
| [telegram_mini_app/app.js](telegram_mini_app/app.js) | 追加は許可した2行のみ |
| [telegram_mini_app/index.html](telegram_mini_app/index.html) | `.tape-chart` のマウント先を追加 |
| [telegram_mini_app/styles.css](telegram_mini_app/styles.css) | チャートの色・字送り |
| [telegram_mini_app/scene3d.js](telegram_mini_app/scene3d.js) | **触らない** |
| [snapshot.py](snapshot.py) | `--bars` / `--levels` を追加。既存出力は維持 |
| [CODEX_MINIAPP_SCENE_HANDOFF.md](CODEX_MINIAPP_SCENE_HANDOFF.md) | 背景3Dの規約。**先に読むこと** |
| [CLAUDE.md](CLAUDE.md) | 運用ルール全体 |
| [TRADING_CONTEXT.md](TRADING_CONTEXT.md) | 口座条件・リスク制限 |

# 実装指示書 — シナリオ内 SL/TP 金額 + 枚数表示 (MONEY PANEL)

対象: `project/app/nq-nightwatch-nqx-final.html`
発行: 2026-07-30
状態: 未実装 / 実装前に本書を全読すること

---

## 0. 一行要約

シナリオが生成されたとき (`G.n >= 1`)、そのシナリオカード内に **MNQ枚数** と
**SL/TP各段の損益金額(ドル)** を表示する。金額は「操作者が入力した現金リスク上限」と
「パケットが宣言した価格」からのみ算出し、**パケットの内容を一切書き換えない**。

---

## 1. なぜこれが「表示のみ」でなければならないか

Nightwatch は判断記録装置であり、発注端末ではない。金額表示は便利だが、次の一線を
越えると設計が壊れる。

**絶対禁止 (これを破る実装は差し戻し)**

1. 算出した金額・枚数を `M.sz` など NQX フィールドへ書き戻すこと。
   → `qm=H` のパケットに数値 `sz` が入ると validator が `HEURISTIC_NUMERIC` で
     弾く。スクリーンショット由来のパケットは枚数を較正できない、という既存の
     安全則をそのまま維持する。
2. 金額を R:R・信頼度・グレード・トリガー・SL構造距離の計算に流入させること。
   → 金額は価格geometryの**下流**。上流へ戻してはならない。
3. 枚数を自動で最大化すること (例: 上限いっぱいまで枚数を増やす提案)。
   → サイズ決定は操作者の領域。本機能は「入力された上限で何枚可能か」を
     示すだけ。

**要件**: 金額表示のON/OFFを切り替えても、`rangeModel()` の unit、`G` の判定文、
シナリオの `cf`/`rr`、validator の結果が **1ビットも変わらない** こと。実装後に
これを検証する (§7)。

---

## 2. 入力

### 2.1 現金リスク上限 (既存UIを再利用)

`#riskCapCash` が既にある (`WG SESSION RISK CAP`)。新規UIは作らない。

- 未入力/未ロック時は金額を表示せず、代わりに後述の `CAP REQUIRED` 状態を出す。
- 既存の `riskCapLocked` / `riskCapSessionKey` のセッション判定をそのまま使う。

### 2.2 契約仕様 (MNQ)

```
MNQ 1枚 = 1ポイントあたり $2.00
NQ  1枚 = 1ポイントあたり $20.00
tick = 0.25pt (MNQ 1tick = $0.50)
```

`M.sy` から判定する。`MNQ` を含めば $2、`NQ` なら $20。判定できない場合は
金額を表示せず `SYMBOL UNVERIFIED` とする。**勝手に $2 と仮定してはならない。**

### 2.3 価格 (パケットから)

`S.en` (エントリー帯), `S.sl`, `S.tp` の3段。すべて宣言済みの値のみ使用。
`~` 付き(推定値)が混ざる場合は §6 の劣化表示にする。

---

## 3. 計算

```
entryMid = (enLow + enHigh) / 2          // 既存のR:R計算と同一の基準を使う
riskPts  = |entryMid - sl|
dollarPerPoint = (MNQ ? 2 : 20)

riskPerContract = riskPts * dollarPerPoint

// ストレスプロファイル: 既存 WG-H1 の "R + 8pt" 規約を踏襲
stressPts = riskPts + 8
stressPerContract = stressPts * dollarPerPoint

qty = floor(capCash / stressPerContract)
```

**`qty` の扱い**

- `qty >= 1` → 通常表示
- `qty === 0` → **`OVER CAP` を赤字で表示し、金額は出さない**。
  「1枚でも上限を超える」は執行不可を意味する。ここで小数枚や
  「上限を上げれば可能」といった示唆を出してはならない。

**各段の金額**

```
riskDollar   = riskPerContract * qty          // SL到達時の損失
tpDollar[i]  = |tp[i] - entryMid| * dollarPerPoint * qty
```

`tp` が3つ揃わない場合は揃っている段のみ表示する (欠損を0で埋めない)。

---

## 4. 表示

### 4.1 挿入位置

シナリオカード内、既存 `.dsGrid.risk` (ENTRY/SL/TP を出している2列グリッド) の
**直下**に新ブロック `.moneyPanel` を追加する。既存のグリッドは変更しない。

### 4.2 構造

```html
<div class="moneyPanel" data-state="ok">
  <div class="mpHead">
    <small>POSITION SIZE</small>
    <strong>4 MNQ</strong>
    <em>CAP $500 · STRESS $118/CT</em>
  </div>
  <div class="mpRow risk">
    <small>SL 27,637.75</small><b>-$1,478</b><span>-369.75 pt</span>
  </div>
  <div class="mpRow">
    <small>TP1 28,075.25</small><b>+$542</b><span>+67.75 pt / 0.18R</span>
  </div>
  <div class="mpRow">
    <small>TP2 …</small><b>+$…</b><span>… pt / …R</span>
  </div>
  <div class="mpRow">
    <small>TP3 …</small><b>+$…</b><span>… pt / …R</span>
  </div>
  <div class="mpFoot">DISPLAY ONLY · NOT AN ORDER · SIZE IS THE OPERATOR'S DECISION</div>
</div>
```

### 4.3 見た目 (既存トークンのみ使用。新色を作らない)

- 損失側: `var(--short)` / 利益側: `var(--long)`
- 金額 `<b>` は `font-size:15px; font-weight:700;` タブular数字
  (`font-variant-numeric:tabular-nums` を必ず付ける。桁が揺れると読めない)
- 枠線 `1px solid var(--line-soft)`、背景 `var(--bg-2)`
- `.mpFoot` は `var(--ink-quiet)` の 8px。**この免責は常時表示、省略不可。**

**動きの演出** (任意、`body.fx-off` 時は無効化すること)

- 金額が更新されたら 180ms で数字を上下フェード (`translateY(4px)` → `0`)
- `.moneyPanel` 出現時に `opacity 0→1` + `translateY(6px)→0` を 220ms

`prefers-reduced-motion: reduce` と `body.fx-off` の両方で
アニメーションを止める。金額は静止状態でも完全に読めること。

### 4.4 状態別表示

| 状態 | 条件 | 表示 |
|---|---|---|
| `ok` | cap入力済 & qty>=1 & 価格が厳密 | 通常表示 |
| `cap-required` | cap未入力/未ロック | `SET SESSION RISK CAP TO SEE SIZING` (金額なし) |
| `over-cap` | qty === 0 | `OVER CAP — 1 CONTRACT EXCEEDS $<cap>` を `var(--danger)` で |
| `approx` | 価格に `~` を含む | 金額の前に `~` を付け、`APPROX PRICES` を併記 |
| `symbol-unverified` | `M.sy` から乗数が確定できない | `SYMBOL UNVERIFIED — NO SIZING` |
| なし | `G.n === 0` | **ブロック自体を描画しない** |

`G.n === 0` で金額を出さないのは重要。シナリオがないのに金額が見えると、
存在しない取引を想像させる。

---

## 5. 実装場所 (具体)

1. **CSS**: `obsidian-ops-v2` の style ブロック末尾に `.moneyPanel` 一式を追加。
   既存セレクタは書き換えない。
2. **計算関数**: `buildTruthChart` より前に純関数として追加。

```js
function scenarioMoney(sc, market, settings){
  // 戻り値: {state:'ok'|'cap-required'|'over-cap'|'approx'|'symbol-unverified',
  //          qty, dollarPerPoint, riskPts, riskDollar, legs:[{label,price,pts,dollar,r}]}
  // 副作用なし。sc / market / settings を変更しないこと。
}
```

3. **描画**: シナリオカードを組んでいる箇所 (`.dsGrid.risk` を出力している関数) の
   直後に `renderMoneyPanel(money)` の出力を差し込む。
4. **再描画**: `#riskCapCash` の `input` と `#riskCapLock` の `click` で
   シナリオカードを再描画する。**チャートは再描画しなくてよい** (金額はチャートに
   影響しないため)。

---

## 6. 精度の劣化を隠さない

- 価格に `~` があれば金額にも `~` を付ける。丸めて隠さない。
- 金額は**整数ドルに丸める** (`Math.round`)。セント表示は精度の幻想を与える。
- 枚数は必ず `floor`。切り上げは絶対にしない。
- `riskPts <= 0` (SLがエントリーの勝ち側) の場合は金額を出さず
  `INVALID GEOMETRY` とする。validator が別途エラーを出すが、
  ここでも金額を計算してはならない。

---

## 7. 実装後に必須の検証

```
[ ] G.n=0 のパケットで .moneyPanel が DOM に存在しないこと
[ ] cap 未入力で金額が出ず cap-required 表示になること
[ ] cap 入力/未入力を切り替えても rangeModel(state.market).unit が不変
[ ] cap 入力/未入力を切り替えても scenario の cf / rr 表示が不変
[ ] validate_nqx.py の全 evals/fixtures が改訂前と同一の終了コード
[ ] コピーした NQX に sz や金額が混入していないこと (COPY NQX で確認)
[ ] qty=0 のケース (cap を極小にする) で over-cap が出て金額が消えること
[ ] MNQ1! と NQ1! で乗数が $2 / $20 と切り替わること
[ ] 判定不能シンボルで symbol-unverified になること
[ ] body.fx-off でアニメーションが止まり金額は読めること
[ ] prefers-reduced-motion でも同様
[ ] .mpFoot の免責が常に見えること
```

**このチェックリストを全て通すまで完了報告をしないこと。**
特に3行目と4行目 (金額が上流へ流入していないこと) は本機能の存在条件である。

---

## 8. やらないこと

- 発注機能・ブローカー連携 (Nightwatch の役割外)
- 複数シナリオの合算リスク表示 (相互排他が前提なので合算は誤誘導)
- 過去の損益記録・勝率表示 (`evidence-ledger` の領域)
- 税・手数料の計算 (`cm` コストモデルが未較正のため)
- 金額に基づく「推奨枚数」の提示 (サイズは操作者の決定)

---

## 9. ロールバック

作業前に `nq-nightwatch-nqx-final.before-moneypanel-<YYYYMMDD>.html` を作成する。
`app/` 直下に別名の完成版を増やさず、正本へ段階適用すること。

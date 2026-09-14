# R11-D 指令 — DECISIVE ICT / SMT / VP 戦略統合

宛先: このリポジトリを変更する下位モデル。  
作業ディレクトリ: `C:\Users\exexu\Downloads\nq-nightwatch-claude-code-handoff`

この文書は**実装指示書**である。分析レポートや改善案を返して終えてはならない。
指定されたコード、テスト、運用文書を実際に変更し、検証結果まで返すこと。

---

## 0. 任務

現行 Nightwatch は ICT / SMT / VP / FVG / DOL を取得・計算しているが、その多くを
`ADVISORY` に隔離し、`allowed` / `grade` / `promotion` に一切使っていない。
これは今回で終了する。

本タスクの目的は次の4点である。

1. **ICT・SMT・VPを実際のモデル選択、Entry配置、等級、目標、武装可否へ接続する。**
2. **回転相場を全停止理由にせず、その環境で取るモデルへルーティングする。**
3. **評価フェーズと資金提供フェーズを分離し、同じ慎重運用を両方へ流用しない。**
4. **ライブ出力を「最有力の1モデル」に絞り、A以上ならきっちり取れる状態にする。**

慎重であること自体を品質とみなしてはならない。品質とは、
**観測された構造に対して、モデル、Entry、SL、目標、失効条件が一意に決まること**である。

ただし、次は慎重さではなく実行整合性なので削除しない。

- 現在値・時刻・銘柄・時間足の検証
- 構造否定に基づくSL
- 口座リスク上限
- OCO、注文方向、未約定注文、イベント封鎖の検査
- ユーザー承認なしの実発注禁止
- 損切り直後の衝動的な入り直し禁止

---

## 1. 指示の優先順位

矛盾がある場合は、次の順で従う。

1. この `CODEX_R11_DECISIVE_ICT_STRATEGY_DIRECTIVE.md`
2. `TRADING_CONTEXT.md` の最新の口座・発注安全条件
3. `CLAUDE.md` のうち、この指令と衝突しない運用条件
4. `docs/history/STRATEGY_EVOLUTION_R10.md`
5. 旧 `CODEX_ICT_STRATEGY_TASK.md`

旧 `CODEX_ICT_STRATEGY_TASK.md` の以下は**明示的に破棄**する。

- `ADVISORY` を足しても promotion 差分0を要求する条件
- ICT / SMT / VP の `ENFORCED` 化禁止
- OTE への Entry 置き直し禁止
- `monitor_publish.py` / Mini App / Worker を一律無変更とする制約
- 全てを3セッション観測後まで保留する条件
- `不明点は列挙して止める` という一般停止規則

不明点があっても、後述の既定値と優先順位で実装を続ける。停止してよいのは、
実データを破壊する恐れがある場合、認証情報が必要な場合、実発注が必要な場合だけである。

---

## 2. 作業前に読むファイル

次をこの順で全文読む。コード変更はその後に開始する。

1. `TRADING_CONTEXT.md`
2. `CLAUDE.md`
3. `CODEX_ICT_STRATEGY_TASK.md`
4. `docs/history/STRATEGY_EVOLUTION_R10.md`
5. `HANDOFF.md`
6. `msnr_gate.py`
7. `tests/test_msnr_gate.py`
8. `monitor_publish.py`
9. `nqx_state.py`
10. `telegram_mini_app/app.js`
11. `telegram_mini_app/test/evalcard.test.mjs`
12. `project/claude-skills/mnq-nightwatch-nqx/references/operational-gates.md`
13. `project/engine-contract/nqx1-spec.md`

追加で、利用可能なら次の研究・監査規則も読む。

- `C:\Users\exexu\.codex\skills\mnq-nightwatch-nqx\references\regime-gamma-policy.md`
- `C:\Users\exexu\.codex\skills\mnq-nightwatch-nqx\references\evidence-log.md`
- `C:\Users\exexu\.codex\skills\mnq-nightwatch-nqx\references\research-basis.md`

---

## 3. 先に直す事実不整合

### 3.1 口座サイズの不整合

ローカル文書は口座を「Apex 100K intraday」と呼びながら、利益目標 `$9,000`、
最大損失 `$4,000` を使っている。2026-08-22時点のApex公式表では、この組合せは
150K Intraday Evaluationであり、100Kは利益目標 `$6,000`、最大損失 `$3,000` である。

- Evaluation: <https://apextraderfunding.com/help-center/intraday-trailing-drawdown-accounts/intraday-trailing-drawdown-evaluations/>
- PA: <https://apextraderfunding.com/help-center/intraday-trailing-drawdown-accounts/intraday-trailing-drawdown-performance-accounts-pa/>

実装時は口座名から推測しない。`accountProfile` を1か所に集約し、少なくとも次を持たせる。

```json
{
  "phase": "EVAL_STRIKE",
  "planSize": 150000,
  "profitTarget": 9000,
  "maxDrawdown": 4000,
  "maxContracts": 12,
  "source": "dashboard-or-explicit-env",
  "verifiedAt": "ISO-8601"
}
```

管理画面または明示的な環境変数で確認できない値は、既存値を勝手に書き換えない。
ただし不整合は起動時・カード・実装報告の先頭に明示する。

### 3.2 評価フェーズへPA制約を混ぜない

現行の新Intraday Evaluationには最低取引日数、Consistency、DLLがなく、1日合格が可能である。
従って、評価フェーズで30%想定から作られた `DAYGOAL=$2,700` を合格速度の上限に使わない。

PAは別物であり、現在のIntraday PAには5利益日、口座別最低日次利益、Safety Net、
50% Consistencyがある。

- Payout requirements: <https://apextraderfunding.com/help-center/uncategorized/intraday-trailing-drawdown-payouts/>

この違いをコード上の `phase` で分岐する。コメントだけで分けてはならない。

---

## 4. 新しい全体構造

判定順序を次に固定する。

```text
市場データ検証
  → レベルマップ
  → レジーム
  → 全モデル候補生成(BUY/SELL両方)
  → ICT/SMT/VP/CBC/FVG/PO3/ADRで候補を採点
  → 構造SLと目標余地を検査
  → A以上だけ残す
  → 決定規則で最有力1件を選ぶ
  → WATCHまたはARMEDをpublish
```

**ゼロシナリオを優先してはならない。** ゼロが許されるのは、全候補がA未満、
構造SLが口座枠外、目標が消費済み、イベント封鎖中、または市場データが無効な場合だけである。

内部ではBUY/SELL/FLATを比較するが、ライブ画面へ出すのは最有力1件だけとする。

---

## 5. フェーズを分離する

新しい必須フィールド:

```text
NQX_PHASE=EVAL_STRIKE | PA_HARVEST
```

### 5.1 `EVAL_STRIKE`

目的は評価口座の最短通過である。

- AまたはA+以外は武装しない。
- 同時に武装できるのは最有力1モデルだけ。
- Entry前に、現在の残り利益目標を構造ターゲットで満たすための必要枚数を計算する。
- 現行2MNQでは `$9,000` に約2,250pt必要であり、一発通過設計にならない。この矛盾を隠さない。
- 必要枚数、構造SLでの損失、手数料、滑り緩衝、口座上限を数値で出す。
- 実際のリスク上限を変更するのは、明示的な `EVAL_RISK_DOLLARS` が設定された場合だけ。
- `EVAL_RISK_DOLLARS` が無ければ現行上限で判定を続け、必要な設定差分を報告する。
- トレーリング閾値をSLとして使わない。SLは必ず構造否定点に置く。
- SL後に同じ脚へ即再入場しない。そのセッションのStrikeは終了する。

必要枚数:

```text
requiredQty = ceil((remainingProfit + estimatedFees) / (targetDistancePt × $2.00))
```

予定損失:

```text
plannedLoss = (stopDistancePt + slippageBufferPt) × requiredQty × $2.00
              + estimatedFees
```

`requiredQty` が口座上限を超える、または `plannedLoss` が明示リスク枠を超える場合は
`STRIKE_NOT_FEASIBLE` とする。これは慎重な見送りではなく、算術上の不成立である。

### 5.2 `PA_HARVEST`

目的はSafety Netを越え、出金可能利益を積み、口座を維持することである。

- Aは基準リスク、A+は**同じリスクでランナーを許可**する。等級を理由に枚数を増やさない。
- 同時保有は1モデルだけ。
- 2連敗、日次損失上限、イベント封鎖は維持する。
- 日次利益上限は固定30%想定ではなく、最新のPA 50% Consistencyと出金要件から計算する。
- 5利益日の最低額を満たした日は、さらに取る理由がない限り新規を止める。
- PAのサイズはTierを超えない。評価フェーズの最大枚数を流用しない。

---

## 6. レジームは停止器ではなくモデルルーターにする

`rotation()` の結果を全 `promotion` の共通ブロッカーへ入れる現行実装を廃止する。

| レジーム/状態 | 優先モデル | 抑えるモデル |
|---|---|---|
| `BA` / 高回転 | `VP80_REVERSION`, `TURTLE_SOUP_REVERSAL` | `BREAKER_CONTINUATION` |
| `TR` | `BREAKER_CONTINUATION`, `OTE_FVG_PULLBACK` | 無確認の逆張り |
| `EX` / `ER` | `BREAKER_CONTINUATION`, `OTE_FVG_PULLBACK` | 1本だけの反転 |
| `TX` | `TURTLE_SOUP_REVERSAL`, `BREAKER_CONTINUATION` | 受容未確定の飛び乗り |
| `MX` | 完成チェーンを持つ全モデル | レジーム不明だけを理由に全停止しない |
| `EC` / イベント封鎖 | 新規執行なし | 全モデル |

回転比率が高い場合:

- VP80とTurtle Soupには `regimeFit=2`
- Breaker/Continuationには `regimeFit=0` と `ROTATION_AGAINST_MODEL=-2`
- 全候補を一律 `allowed=false` にしない

LUNCH帯も全停止にしない。

- VP80 / Range Reversionには中立
- Continuationには `-1`
- A+をAへ落とすことはあっても、LUNCHだけで完成した反転モデルを捨てない

---

## 7. 実装する執行モデル

### 7.1 `VP80_REVERSION`

用途: バリュー外の受容失敗から反対側バリュー端へ戻る回転。

必須:

1. VAHとVALが両方とも正確に存在する。
2. VA外側の確定終値が2本以上ある。
3. VA内へ確定終値で復帰する。
4. 続く3本以内にもう1本VA内を保持する、またはエッジ再テストを拒否する。
5. 反対側VA端まで最低1.5R、A+には2.8R以上の余地がある。

Entry:

- A+: 2本目のVA内保持終値で `AGGRESSIVE_ACCEPT_CLOSE` を許可する。
- A: 復帰したVAH/VALの最初の再テストで入る。
- Entry時点でPOCまたは反対側VA端が消費済みなら武装しない。

SL:

- 失敗オークションの外側極値 + `max(0.50 × U, 2pt)`。
- `U` は確定足TR14平均。口座枠へ合わせて内側へ縮めない。

Target:

- TP1: POC
- TP2: 反対側VA端
- POCがEntryの背後、または0.5R未満ならTP1から除外し、反対側VA端を最初の目標にする。

ICT/SMT:

- BUYはdiscount/VAL側、SELLはpremium/VAH側ならlocation +2。
- SMT整合は +1、逆行は -2。
- 回転レジームは +2。`ROTATION_REGIME` で止めない。

### 7.2 `TURTLE_SOUP_REVERSAL`

用途: PDH/PDL、Asia/London高安、VAH/VAL、明示的な流動性プールのSweep反転。

必須:

1. 名前付き流動性をヒゲまたは終値でSweep。
2. レベル内へ確定終値でReclaim。
3. displacementとMSS、または強いReclaim後の保持。
4. 次のDOLまで最低1.5R。

Entry:

- A+: Reclaim + displacement確定終値で入ってよい。
- A: 最初のリテストで入る。
- 追加の3本目・4本目の確認を要求しない。

SMT:

- 同じ流動性SweepでNQがESに対して更新失敗なら反転方向へ +1。
- SMTが無くても、displacementが強ければAに到達可能。
- `peers` 欠落は0点であり、ブロッカーにしない。

### 7.3 `BREAKER_CONTINUATION`

用途: Breaker / FLIP / 支持抵抗転換後の継続。

必須:

1. 名前付き境界のbreak。
2. 境界外の確定受容。
3. 境界またはBreakerへの戻りを保持。
4. HTFが同方向または中立。
5. 次のDOLまで最低1.5R。

Entry:

- standard: Breaker/旧境界への最初の戻り。
- A+かつLRLR: acceptance closeでの早期Entryを許可する。
- 回転比率が高い場合は -2。VP/Turtleへ候補順位を譲る。

### 7.4 `OTE_FVG_PULLBACK`

用途: トレンド脚の62–79%戻しとFVG重複を使ったContinuation。

必須:

1. HTF方向と一致するdisplacement leg。
2. OTE帯が正しく計算できる。
3. OTE帯とBreaker、FVG、セッションレベルのどれか1つが重なる。
4. DOLが同方向にあり、最低1.5R。

Entry:

- OTE/FVG重複帯へ指値。
- 現在値付近へEntryを寄せない。
- 到達しなければ未約定で終わる。追いかけない。

### 7.5 VWAPの扱い

VWAPは独立した第5モデルを作らない。

- VWAP break/accept/holdは`BREAKER_CONTINUATION`の動的境界として使う。
- VWAP外からのfailed acceptance/reclaimは`TURTLE_SOUP_REVERSAL`へ送る。
- `VWAP_DRIFT` のときだけVWAP証拠を0点にする。他モデルは止めない。

---

## 8. ICT / SMT / インジケータを判定へ直結する

### 8.1 Premium / Discount / OTE

表示だけで終わらせない。

- 正しい側ならlocation +2。
- wrong sideだがOTEへ置換可能なら、候補を捨てず `RELOCATE_TO_OTE` にする。
- OTEへ価格が到達し、元の構造が生存していれば、連鎖を最初から作り直さずARMEDへ進める。
- wrong sideかつ置換先が口座枠外、構造死、目標消費済みなら不合格。

固定 `Entry=S±2pt` は廃止する。Entryはモデルの構造帯で決め、許容幅は
`max(2pt, 0.25×noiseFloor)` とする。

### 8.2 Draw on Liquidity / LRLR / HRLR

- DOLをTP候補へ直接入れる。
- 最初のDOLまで1.5R未満なら `TARGET_HEADROOM_INSUFFICIENT`。
- 2.8R以上ならtarget +2。
- 1.5–2.79Rならtarget +1。
- HRLRは -1。全停止にはしない。
- DOL残距離 `<0.25×noiseFloor` は `TARGET_EXHAUSTED` のhard blocker。

### 8.3 Index SMT

SMTは単独トリガーにしないが、必ずgradeへ影響させる。

- sideと整合: +1
- sideと逆: -2
- 両方向乖離/不明: 0
- `snapshot.peers` 欠落: 0
- AM/PM比較窓外: 効果を半減せず0点とする。古いSMTを持ち越さない。

YMが取得できない現環境ではMNQ対ESだけで判定する。YM欠落をエラーにしない。

### 8.4 CBC

`snapshot.cbc` をoptionalで受ける。

```json
{
  "market": "bullish|bearish|inside",
  "cbc": "bullish|bearish|inside",
  "openingRange": "bullish|bearish|inside",
  "emaCloud": "bullish|bearish|inside",
  "vwap": "bullish|bearish|inside",
  "ema200": "bullish|bearish|inside"
}
```

- 4項目以上がsideと一致: +1
- 4項目以上がsideと逆: -1
- `inside` と欠落は0
- CBCの名称や内部ロジックを推測して展開しない。表の文字列をそのまま使う。

### 8.5 Flux FVG

`snapshot.fvgLines` をoptionalで受ける。

```json
[{"hi": 30010.0, "lo": 30002.0, "kind": "bullish", "at": "ISO-8601"}]
```

- 自前`fvg_scan`と50%以上価格重複: confirmed FVG、+1候補。
- 外部のみ: indicator claimとしてEntry帯に使えるが加点しない。
- 自前のみ: inferred FVGとして従来どおり使う。
- 不一致はブロッカーにしない。証拠ラベルを分ける。

### 8.6 CVD

CVD必須条件を削除する。

- fast/slow、傾き、sideが整合: +1候補。
- 逆行: -1。
- 停止・欠落: 0。Entry提案全体を止めない。
- CVD、CBC、SMT、FVGの確認点は合計最大2点まで。

### 8.7 Power of 3

単に日足始値より上/下だけでPO3を決めない。

最低限、次を状態として分ける。

```text
ACCUMULATION
MANIPULATION_UP
MANIPULATION_DOWN
DISTRIBUTION_UP
DISTRIBUTION_DOWN
UNKNOWN
```

Accumulation rangeの一方をSweepし、反対方向へdisplacementした場合だけDistributionを付ける。
モデルsideとDistribution方向が一致すれば +1候補。`UNKNOWN` は0。

### 8.8 CBDR / Asian Range STD / ADR

これらは単独Entryトリガーにしない。目標と脚の余地へ使う。

- CBDR/Asian projectionとDOLが重なる: target confluence。
- completed 5-day ADRから1.27 / 1.62投影を作る。
- 当日レンジが1.27 ADR以上で、さらに同方向Continuationを狙う: -1。
- 1.27 ADR以上でも、Sweep/Reclaim反転が完成していれば反転モデルを止めない。
- `dailyBars` 欠落は0。全判定を止めない。

---

## 9. A / A+ の新しい決定式

現行の「初動・tier・鮮度」の3条件だけでA+を決める実装を置き換える。
gradeは確率ではなく、**現在の執行準備度**である。

### 9.1 hard eligibility

1つでも失敗すれば候補から除外する。

- モデル固有チェーン完成
- Entry、構造否定、SL、TPが価格で存在
- SLが構造外側にある
- 構造SL + 緩衝が口座リスク枠内
- 最初の有効目標まで1.5R以上
- 目標未消費
- 市場データが現在・正しい銘柄・正しい時間足
- 重要指標ブラックアウト外
- 未約定注文との衝突なし

### 9.2 score

| 要素 | 点 |
|---|---:|
| モデル固有チェーン完成・鮮度内 | +3 |
| レジームがモデルに適合 | +2 |
| レジーム中立/`MX`だがチェーン完成 | +1 |
| ICT/VP上のEntry位置が正しい | +2 |
| OTEへ置換済みで位置が正しい | +2 |
| target headroom 2.8R以上 | +2 |
| target headroom 1.5–2.79R | +1 |
| SMT/CBC/CVD/FVG/PO3整合 | 各+1、合計最大+2 |
| displacement `>=1.3×noiseFloor` かつ鮮度良好 | +1 |
| SMT逆行 | -2 |
| CBCまたはCVDが強く逆行 | -1 |
| HRLR | -1 |
| ADR 1.27以上で同方向Continuation | -1 |
| LUNCH帯のContinuation | -1 |
| 回転相場のContinuation | -2 |

### 9.3 grade

```text
score >= 9  → A+
score 7–8   → A
score <= 6  → B以下・武装しない
```

Optionalデータ欠落は0点であり、hard blockerにしない。
完成した価格構造だけで7点へ届く設計を維持する。

---

## 10. Entry状態機械

状態を次に統一する。

```text
DETECTED
  → QUALIFIED
  → RELOCATE_TO_OTE または READY_AT_STRUCTURE
  → ARMED
  → ACTIVE
  → PARTIAL / BE
  → CLOSED / INVALIDATED / EXPIRED
```

規則:

- A+はモデルが許す場合、acceptance closeで早期Entryできる。
- Aは最初のretestを使う。
- `RELOCATE_TO_OTE` は拒否ではない。構造が生存したままEntry帯へ来たらARMEDにする。
- 固定10分TTLを廃止する。
- 有効期限は「次の15分足確定」「構造否定」「イベント封鎖開始」の最も早いものを基本とし、
  モデルが明示する場合でも最大30分とする。
- Entry前にTPまたはSL価格へ到達した注文は即EXPIRED。
- シナリオ無効化時は未約定注文も同時に取消対象とする。

---

## 11. 最有力1モデルの決定規則

候補を次の順で並べる。

1. grade: A+ > A
2. score
3. レジーム適合点
4. target R
5. チェーン鮮度
6. 直近トリガー時刻

同点でも曖昧として全停止しない。上記で必ず1件へ決める。
反対方向の同点候補がある場合、より近い構造無効化を持つ方を優先する。

ライブ画面へは最有力1件だけをpublishする。2位以下は監査ログへ残す。

---

## 12. 出力スキーマ

`msnr_gate.py --card` に、既存項目を壊さず次を追加する。

```json
{
  "decision": {
    "phase": "EVAL_STRIKE",
    "model": "VP80_REVERSION",
    "side": "SELL",
    "state": "ARMED",
    "grade": "A+",
    "score": 10,
    "entryMode": "AGGRESSIVE_ACCEPT_CLOSE",
    "entry": 30020.25,
    "stop": 30034.50,
    "targets": [30005.00, 29978.50],
    "targetR": [1.07, 2.93],
    "hardBlockers": [],
    "penalties": [],
    "evidence": ["VP_ACCEPTED", "ROTATION_FIT", "SMT_ALIGNED"]
  }
}
```

カード4096バイト制限は維持する。

縮小順:

1. 2位以下候補
2. 長い説明文
3. 生のICT内訳
4. evidenceの4件目以降

`decision` の model / side / state / grade / score / entry / stop / target / hardBlockers は
絶対に削らない。

`monitor_publish.py` は `evaluation.decision` を正本としてscenarioを組めるようにする。
手作業でgrade・Entry・HTFをJSONへ足す現行運用を廃止する。

Mini Appでは `ADVISORY / 武装根拠外` 表示を削除し、次へ置き換える。

```text
MODEL   VP80 REVERSAL
GRADE   A+ · 10/12
ROUTE   ROTATION → RANGE REVERSAL
ENTRY   VAH ACCEPT CLOSE
TARGET  POC 1.1R / VAL 2.9R
```

---

## 13. 実装タスク

### Task A — 判定順序を変更

`msnr_gate.evaluate()` を次の順へ変更する。

1. bars / levels / noise / regime
2. VWAP / VP path
3. MSNR chains
4. rrPotential
5. ICT / SMT / CBC / FVG / PO3 / CBDR / ADR
6. candidate generation
7. hard eligibility
8. score / grade
9. primary selection
10. summary / card

ICTをpromotion確定後に計算する現行順序を廃止する。

### Task B — 新しい純粋関数

最低限、次を実装する。

```text
route_regime(...)
build_candidates(...)
candidate_vp80(...)
candidate_turtle_soup(...)
candidate_breaker(...)
candidate_ote_fvg(...)
score_candidate(...)
select_primary(...)
model_stop_buffer(...)
model_targets(...)
power_of_three(...)
cbdr_projection(...)
adr_projection(...)
cbc_alignment(...)
fvg_confluence(...)
```

### Task C — 旧共通ブロッカーを削除

- `compose()` の全モデル共通 `ROTATION_REGIME`
- CVD欠落による全提案停止
- CT_TREND逆行による全モデル停止
- ボラ比率0.4/0.6だけによる全停止
- `Entry=S±2pt`
- 一律1.5×noiseFloorの反応型SL
- 固定10分TTL

CT_TREND、ボラ、CVDは削除するのではなく、モデル固有の点数・構造SLへ移す。

### Task D — publish / UI

変更を許可する。

- `monitor_publish.py`
- `nqx_state.py`
- `cloudflare/src/state_machine.js`（必要な場合）
- `telegram_mini_app/app.js`
- `telegram_mini_app/styles.css`
- 対応テスト

ただし、`order.py --confirm` を実行してはならない。実発注テストは禁止。

### Task E — 観測台帳

`ict_observe.py` を作る。単純なサイクル数ではなく、`decision_id` 単位で集計する。

必須列:

```text
decision_id,setup_version,phase,model,direction,regime,grade,score,
ict_location,smt,cbc,fvg,po3,dol_r,hrlr,entry,stop,targets,
filled,net_points,r_multiple,mfe_points,mae_points,outcome
```

未約定、取消、期限切れを勝敗へ数えない。同一オークションの連続シグナルを重複させない。

gradeは直ちに実運用へ使うが、勝率や確率とは呼ばない。
経験的確率を表示できるのは、同じ
`setup_version × regime × session × direction` でOOS 30件以上等の昇格条件を満たした場合だけ。

### Task F — Challenge calculator

`challenge_calc.py` は作るが、データ不足時に偽の「合格確率」を出さない。

2モードにする。

1. `--scenario`: 指定した勝率・平均勝ちR・平均負けRに対する条件付き感度表
2. `--empirical`: 凍結モデルの十分な実績がある場合だけ経験的推定

出力:

- risk dollars
- required qty
- target distance
- stop distance
- fees / slippage
- pass target到達に必要なR
- 条件付きpass/breach率
- 期待トレード数
- `COMPLIANT / NOT_FEASIBLE / UNVERIFIED_INPUT`

解析解とMonte Carloが同じ前提を使うようにする。Intraday trailingは含み益ピークで動くため、
単純gambler's ruinを実口座の正確な合格確率と呼ばない。

### Task G — 運用文書を縮約

`CLAUDE.md` に追記を重ねない。ライブ判断に必要な規則を150–250行程度へ縮約する。

移動先:

- 過去の敗戦説明 → `INCIDENT_ARCHIVE.md`
- Bot/JSON/BOM/デプロイ手順 → `OPERATIONS.md`
- 旧口座・旧週次計画 → `ARCHIVE/`

`CLAUDE.md` に残すのは、データ取得、モデルルーター、grade、Entry/SL/TP、phase、発注承認だけ。

---

## 14. テスト変更

意図的に挙動を変えるため、旧「promotion差分0」を合格条件にしてはならない。

### 削除・置換する旧テスト

- `test_rotation_blocks_all`
- `test_advisory_does_not_affect_promotion`
- `test_ict_never_changes_promotion`
- `test_ict_side_check_is_advisory_only`
- `test_smt_never_changes_promotion`

### 必須の新テスト

1. 回転相場でVP80がA以上へ昇格する。
2. 同じ回転相場でBreaker continuationは減点される。
3. `VP_ACCEPTED` かつ反対端2.8R以上でA+になる。
4. `vpHeadroom.exhausted` でhard blockになる。
5. aligned SMTでA→A+へ上がる境界ケース。
6. opposite SMTでA+→AまたはBへ落ちる。
7. peers欠落は0点で、完成構造を止めない。
8. wrong-side EntryがOTEへ再配置される。
9. OTE未到達はWATCH、到達後は構造生存ならARMED。
10. HRLRは減点するが単独では全停止しない。
11. CBC 4/6整合で加点、欠落は中立。
12. Flux FVGと自前FVG重複で加点。
13. CVD停止中でもVP80またはTurtle SoupがAなら武装可能。
14. LUNCHのVP80は停止されず、Continuationだけ減点される。
15. ADR 1.27超でContinuationを減点する。
16. `MX`でも完成チェーン+十分なscoreならAになれる。
17. 最有力候補が決定規則どおり1件になる。
18. カードが4096バイト以下。
19. stale price / symbol mismatch / event blackout / risk capは従来どおり武装を止める。
20. 実発注コマンドが一度も呼ばれない。

### 実データ・リプレイ

- 2026-08-21のSELL 29,327は、`TARGET_EXHAUSTED` とwrong-sideで旧Entryを武装しない。
  OTE帯へ`RELOCATE_TO_OTE`を出す。
- 2026-08-17の回転相場では、Breakerを止めるが、VP80/Turtle候補まで全停止しない。
- 2026-08-12の勝ち型はBreakerまたはTurtleとしてA以上を維持する。

全 `.secrets/monitor_cycle_*.json` に新旧比較をかけ、次を報告する。

- 旧allowed → 新grade別件数
- 新たにA/A+になったVP/ICTモデル件数
- 回転でContinuationからReversionへ切り替わった件数
- wrong-sideからOTEへ再配置された件数
- target exhaustedで止まった件数
- 例外件数

差分0は要求しない。**差分が発生することが今回の目的**である。

---

## 15. 受入基準

次を全て満たして完了とする。

- [ ] ICT / SMT / VPがgradeまたはEntry/targetを実際に変更する。
- [ ] VP80が独立した執行モデルとしてARMEDになれる。
- [ ] rotationがモデルルーターとして働く。
- [ ] CVD/SMT/CBC/FVG欠落が一律NO TRADEを作らない。
- [ ] A/A+が新score式と一致する。
- [ ] 最有力1モデルだけがpublishされる。
- [ ] EVAL_STRIKE / PA_HARVESTがコードと出力で分かれる。
- [ ] 旧30% DAYGOALがEvaluationを止めない。
- [ ] Entry/SL/TP/無効化/期限が全て価格で出る。
- [ ] リスク上限、イベント、データ鮮度、注文安全装置は維持される。
- [ ] カード最大4096バイト以下。
- [ ] テストスイートALL PASS。
- [ ] 400件リプレイの戦略差分を表で報告する。
- [ ] 実発注をしていない。

---

## R12 運用契約追補（R11-Dを置換しない）

R11-Dの「ICTを実判定へ入れる」意図を、実行・監査まで一貫させる。

- OTEは `rangeTf / rangeStart / rangeEnd / anchorType / freshness / high / low` を保存した
  `rangeAnchor`だけから生成する。ローリング3分足の高安だけでOTEを作らない。
- FVGは時間足、年齢、displacement、到達前構造を、SMTは同時刻・同一session・freshnessを
  保存し、成立していないものを加点・トリガーに使わない。
- CVD欠落／停止はstudy値を一度再取得する。再取得後もfreshでなければCVDを方向根拠にせず、
  構造モデルのAは残すがA+は禁止する。
- 2枚はTP1とrunnerの各1枚OCOとして送る。共通最終TPブラケットは廃止し、TP1約定後に
  qty=1を照会で確認したrunnerだけを建値・トレールへ変更する。
- OOSでは、固定済みのentry/SL/exit/costを変えず、
  `MSNR -> PD -> DOL -> FVG -> SMT -> Killzone -> CVD` の順でアブレーションする。
  実現R、勝率、MFE/MAE、約定率、コスト後期待値のいずれも実績outcomeが無い限り捏造しない。

正本は `docs/R12_ICT_EXECUTION_CONTRACT.md` と `CLAUDE.md` である。

---

## 16. 禁止する逃げ方

次の実装・報告は不合格である。

- ICT/SMT/VPを再び `ADVISORY` のままにする。
- 情報をカードへ表示しただけで「統合した」と報告する。
- optionalデータ欠落を理由に全停止する。
- 回転、LUNCH、HTF不一致の1項目だけで全モデルを止める。
- SMTを単独Entryトリガーにする。
- common single-bar signalだけでAを付ける。
- 構造より内側へSLを縮め、R:Rを製造する。
- 旧テストを守るため新戦略を無効化する。
- 実装せず、追加調査・提案・未裁定一覧だけを返す。
- ユーザーへ選択肢を投げ返し、実装を止める。
- 口座サイズの不整合を無視して数値をハードコードする。
- `--confirm` を使う。

---

## 17. 検証コマンド

PowerShell:

```powershell
$env:PYTHONUTF8='1'
$env:PYTHONIOENCODING='utf-8'
python tests/run_all.py
Get-Content -Raw -Encoding UTF8 .secrets\monitor_cycle_XXXX.json | python msnr_gate.py --summary
Get-Content -Raw -Encoding UTF8 .secrets\monitor_cycle_XXXX.json | python msnr_gate.py --card
python ict_observe.py
python challenge_calc.py --scenario --wr 0.50 --avg-win-r 2.0 --avg-loss-r 1.0
```

ファイルの読み書きはUTF-8、入力は`utf-8-sig`を許容する。

---

## 18. 完了報告

最終報告は次の順にする。

1. 何が実際の判定へ入ったか
2. 旧ADVISORYとの差
3. 4モデルのgrade件数
4. 400件リプレイ差分
5. 8/21・8/17・8/12リプレイ結果
6. UIでの最有力モデル表示
7. テスト結果
8. 変更ファイル
9. 無変更ファイル
10. 未実施事項と、その実行に必要な外部条件

「より慎重になった」ことを成果として書かない。
成果は、**取るべきモデルを早く選び、構造が揃ったA以上を武装し、情報を利益経路へ変えたこと**で示す。

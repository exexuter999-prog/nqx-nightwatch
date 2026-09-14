# GPT改良指示書 — Hard Stop 距離ゲート強化（TOO TIGHT の Risk Gate 接続）

- 宛先: GPT（実装担当）
- 発行: Fable（設計担当）/ 承認: ユーザー
- 対象ファイル（現行最終盤・ユーザー指定の3点）:
  1. `project/app/nq-nightwatch-nqx-final.html` — Nightwatch本体
  2. `project/engine-contract/validate_nqx.py` — NQX検証器
  3. `project/engine-contract/nqx1-spec.md` — NQX/1仕様
- 本指示書の目的: 2026-07-20に発生した実トレード事例（LONG Entry ~28,951-28,964・SL 28,927.75が直近安値28,928の直上に置かれ、Invalidationより先にHard SLが刈られた）で判明した、**「SLが構造的に近すぎる」ことを検出はできるが停止まではできない**既存ギャップを埋める。新機能の追加ではなく、**既にコードに存在する判定ロジックを、既存のBLOCKED経路へ正しく配線する**作業が中心である。

---

## 0. あなた（GPT）の役割と行動規範

### 0-1. 役割
あなたは実装担当である。設計判断は本指示書に従い、独自の設計変更を行わない。裁量が必要な箇所は本指示書が個別に許可範囲を定める。許可範囲外の判断が必要になったら、**作業を止めず**その項目を `BLOCKED` として報告書に記録し、次の独立タスクへ進め。

### 0-2. 絶対規範（違反は全成果物の無効化に相当する）
1. **数値を発明しない**。ATR係数・バッファ倍率・閾値は、(a) 本指示書が指定した値、(b) 既存コードに既にハードコードされている値、のいずれかからのみ使う。「調査でよく見た数値だから」という理由だけで新しい定数を書き込まない。
2. **既存の安全条件を弱めない**。`SL < Invalidation < Entry`（LONG）/ `Entry < Invalidation < SL`（SHORT）の不等式、Risk Gate、排他ルール、`SCORE IS NOT PROBABILITY` 相当の免責は一切変更しない。今回の作業は既存ゲートを**追加**するものであり、既存ゲートを緩めるものであってはならない。
3. **後付け検証の禁止**。実装後は必ず現行の`SAMPLE_NQX`（デモパケット）と、本指示書5章のテストケースの両方で回帰確認する。
4. **成果の粉飾禁止**。変更によって既存のデモパケットがどう挙動を変えたか（PASSからBLOCKEDに変わった、等）を隠さずそのまま報告する。
5. 質問で作業を止めない。不明点は「明示的な仮定」として記録して進み、仮定一覧を報告書に集約する。
6. すべての作業報告は日本語。コード・ファイル名・技術用語は原語のまま。

---

## 1. 前提となる調査結果（読了必須）

着手前に以下を理解してから始めること。実装対象コードは全てあなたが直接読める状態にある。

### 1-1. 既に実装されている判定ロジック（削除・再設計禁止、接続のみ変更する）

`project/app/nq-nightwatch-nqx-final.html` には、想定以上に緻密な既存ロジックが既にある：

- **`structuralStopPlan(sc, market)`**（関数定義: 約3375行目付近）: エントリー方向の逆側にある直近の構造レベル（`market.levels`）を「アンカー」とし、`buffer = max(rangeModel(market).unit * 0.32, 2)` だけアンカーの外側に置いた価格を `recommended` として計算する。実際のSL（`sc.sl`）がこの `recommended` より内側にある、または `risk < minimum`（`minimum = max(rangeModel.unit*0.72, |entry-invalidation|*1.12, 4)`）の場合、`status: 'TOO TIGHT'` を返す。
- **`rangeModel(market)`**（関数定義: 約3260行目付近）: OHLCが供給されていれば直近12本のTrue Range平均（実測ATR相当）を `unit` として使い、それが無ければ「マップされたレベル間隔の中央値×0.42」、それも無ければ「価格スケールの0.045%」の順にフォールバックする。出典ラベル（`SUPPLIED OHLC RANGE` / `MAPPED LEVEL SPACING` / `PRICE-SCALE FALLBACK`）を持つ。
- **`invalidationOrderAudit(sc)`**（関数定義: 約3423行目付近）: Invalidationの位置がSLとEntryの間にあるかを検証する、**別系統の**独立した監査。`minBuffer = max(2, risk*0.15)` という、`structuralStopPlan` とは異なる基準でバッファ不足を判定している。
- **`computeGrade(sc, confidence)`**（関数定義: 約3522行目付近）: `sc.stopPlan.status === 'TOO TIGHT'` の場合、グレードを**1段階格下げするだけ**（約3527-3530行目）。BLOCKED化の条件リスト（約3531-3535行目）には `invalidationAudit.FAIL` / `rrAudit.FAIL` / `eventAudit.FAIL` / `validity.EXPIRED` / `crossBlocked` の5つしかなく、**`stopPlan.status === 'TOO TIGHT'` は含まれていない**。
- **`validateAll()`**（関数定義: 約4832行目付近）: `sc.stopPlan.status === 'TOO TIGHT'` の場合、`sc.advisories`（助言）に追加するのみ（約4852行目）。`sc.warnings`（グレード等に影響しうる重大項目の集計先）には入れていない。

### 1-2. 今回のギャップの正体

上記の結果、実際のトレード画像で見えていた「AUTO: STOP SITS INSIDE STRUCTURAL PROTECTION — DOWNGRADED ONE LEVEL」という文言は、ロジックが**正しく発火していた**証拠である。しかし：

- グレードが1段階下がるだけで、シナリオは引き続き `ARMED` / `WATCH` として提示され続けた
- `invalidationOrderAudit` と `structuralStopPlan` が独立した基準で動いているため、「Invalidation順序的にはPASS」かつ「構造距離的にはTOO TIGHT」という状態が起こり得て、ユーザーはInvalidationが機能していると誤認しやすい
- `O|c=MISSING`（観測OHLCなし）の場合、`rangeModel` が `MAPPED LEVEL SPACING` にフォールバックし、実測ボラティリティより粗い推定値になる。この状態のまま `TOO TIGHT` 判定がBLOCKEDに至らないのは、二重の意味で危険（推定精度が低い状況ほど強制停止の必要性が高いのに、実際には停止しない）

### 1-3. リサーチで裏付けられた設計原則（本指示書が要求する変更の根拠）

- Institute and Faculty of Actuaries（Acar & Toffel, 2000）: 同じ名目ストップ距離でも年率ボラティリティが4倍になるとストップ到達確率が約40倍になる、という定量的知見。固定距離のSLはボラティリティに対して不適合になりやすい。
- ICT/SMC系の複数の独立情報源（収束性が高い、ただし個々はweak〜medium）: 「ストップは掃かれたウィックの外側に置く」「スイープ／ブレイク直後は50-79%戻すのが正常」であり、直近安値への戻り自体は失敗の証拠ではなく、SLがそもそも構造の内側にありすぎることが根本原因になりやすい。
- これらは「新しい計算式を発明する根拠」ではなく、「既存の `structuralStopPlan` が採用している設計（構造の外側＋レンジ観測ベースのバッファ）は方向性として正しい」ことを裏付けるものとして扱う。**新しいATR乗数や新しいバッファ係数を追加リサーチ結果から書き込むことは禁止**（絶対規範1）。既存の `0.32` `0.72` `1.12` 等の定数はそのまま使う。

---

## 2. 実装タスク

### 2-1. タスクA: `stopPlan.status === 'TOO TIGHT'` をBLOCKED条件に接続する

対象: `project/app/nq-nightwatch-nqx-final.html` の `computeGrade` 関数（約3522-3537行目）

現状:
```js
if(sc.stopPlan&&sc.stopPlan.status==='TOO TIGHT'){
  grade=order[Math.min(order.length-1,order.indexOf(grade)+1)];
  reasons.push('STOP SITS INSIDE STRUCTURAL PROTECTION — DOWNGRADED ONE LEVEL');
}
```

この1段階格下げの挙動自体は**維持する**（軽微なケースまで一律BLOCKEDにすると過剰反応になるため）。その上で、以下の条件を**新たにBLOCKED化条件として追加**する:

- `sc.stopPlan` が `TOO TIGHT` かつ、実際のリスク距離（`sc.stopPlan.risk`）が `sc.stopPlan.minimum` の**70%未満**（すなわち推奨最小距離を大きく下回る、単なるグレーゾーンではなく明確な違反）の場合、`grade = 'BLOCKED'` とし、`reasons` に `'HARD STOP CRITICALLY INSIDE STRUCTURE — <実測risk> PT vs REQUIRED <minimum> PT'`（実際の数値をテンプレートに埋め込む）を追加する。
- 閾値 `70%` は本指示書が指定する値である。他の割合を独自に採用しない。この値の根拠は「明確な違反」と「軽微な逸脱」を分けるための実務的な線引きであり、リサーチから逆算した数値ではない — 曖昧にせずコメントでそう明記すること。
- `sc.stopPlan.risk` が計算できない（`sc.sl == null` など）場合はこの追加条件を評価しない（既存の `MISSING SL` 系の扱いに委ねる）。

### 2-2. タスクB: `invalidationOrderAudit` と `structuralStopPlan` の整合性チェックを追加する

対象: 同ファイルの `validateAll()` 関数（約4832-4871行目）

現状、2つの監査は独立に計算され、互いを見ていない。以下を追加する:

- `sc.invalidationAudit.status === 'PASS'` かつ `sc.stopPlan && sc.stopPlan.status === 'TOO TIGHT'` の場合、`sc.warnings`（advisoriesではなくwarningsへ、既存のInvalidation FAIL/構造化された重大項目と同じ重みで扱う）に `'INVALIDATION ORDER PASSES BUT HARD STOP SITS INSIDE STRUCTURE — THE TWO AUDITS DISAGREE'` を追加する。
- この行は新しい判定を作るのではなく、**既存の2つの判定結果を突き合わせて矛盾を可視化するだけ**である。矛盾がある場合にどちらを正とするかの裁定はこのタスクの範囲外（タスクAが構造距離側を優先してBLOCKED化する設計のため、実質的にタスクAが解決する）。

### 2-3. タスクC: `computeWarnings` にSL距離の生の妥当性チェックを追加する（バックストップ）

対象: 同ファイルの `computeWarnings(sc)` 関数（約3210-3229行目）

`structuralStopPlan` は `market.levels` が空、または `rangeModel` が `PRICE-SCALE FALLBACK`（最弱の推定）にしか到達できない場合でも `TOO TIGHT` 判定自体は返すが、念のため以下を追加する:

- `sc.sl != null` かつ Entry帯が定義されている場合、Entry中央値からSLまでの距離が「Entry帯の幅そのもの」より狭い場合（すなわちEntry帯の中に収まってしまうほど近い、最低限の非常識さチェック）、`'SL DISTANCE IS NARROWER THAN THE ENTRY BAND ITSELF'` をwarningsに追加する。
- これは `structuralStopPlan` が何らかの理由で機能しなかった場合の最終防御線であり、`structuralStopPlan` を置き換えるものではない。両方を併存させる。

### 2-4. タスクD: `validate_nqx.py` へパケット単体でのSL距離チェックを追加する

対象: `project/engine-contract/validate_nqx.py` の `validate()` 関数、既存の `INVALIDATION_ORDER` チェック（約542-562行目）の直後

`validate_nqx.py` はHTML側と異なり `market.levels` のような構造化オブジェクトを持たないため、`structuralStopPlan` と同じ計算はできない。代わりに、パケットのテキストから読み取れる情報だけで実行可能な、より単純なチェックを追加する:

- 新しいエラーコード `SL_INVALIDATION_TOO_CLOSE` を追加する。
- 条件: `inv is not None` かつ `sl is not None` かつ Invalidation-SL間の距離 `abs(inv - sl)` が、Entry中央値からSLまでのリスク距離（`risk = abs(e_mid - sl)`）の **15%未満**（既存コード544行目付近の `sl_equals` 変数近傍、`minBuffer` 相当の考え方をPython側に移植する）の場合、エラーとして報告する。
- **この15%という値は、HTML側の `invalidationOrderAudit` に既に存在する `risk*.15` という定数をそのまま移植したものである**。新しい数値をリサーチから持ち込まない。
- メッセージ例: `f"{tag}: invalidation ({inv}) sits only {abs(inv-sl):.2f} pt from SL ({sl}), less than 15% of risk ({risk:.2f} pt) — invalidation may not protect before the hard stop is hit"`
- fix例: `"widen the gap between invalidation and SL, or accept that the hard stop is the effective invalidation and declare SL = Invalidation"`
- このチェックは新規追加であり、既存の `INVALIDATION_ORDER` エラー（不等式の順序チェック）とは独立した、**距離の妥当性**チェックである。既存チェックを置き換えない。

### 2-5. タスクE: `nqx1-spec.md` にタスクDの新規検証ルールを明文化する

対象: `project/engine-contract/nqx1-spec.md` の「Required safety invariants」節（約209-228行目）

既存の invariant 2（`LONG: SL < invalidation < entry low` 等）の直後に、新しい invariant として以下を追加する:

```
2a. Invalidation should sit meaningfully inside the stop, not immediately adjacent to
    it. The validator flags SL_INVALIDATION_TOO_CLOSE when the invalidation-to-SL gap
    is under 15% of the entry-to-SL risk distance, since a close-based invalidation
    may not confirm before the hard stop is physically touched.
```

番号は既存のinvariant群と衝突しないよう、実際のファイルの現状を確認してから振り直すこと（本指示書執筆時点では12個の invariant が存在するが、あなたの実装時点で変わっている可能性があるため、必ず現物を確認する）。

---

## 3. 変更してはならないもの

- 既存の `structuralStopPlan` の定数（`0.32`、`0.72`、`1.12`、最低2pt/4pt）は変更しない。今回の作業は「判定結果の配線」であり「判定基準の再設計」ではない。
- 既存の `invalidationOrderAudit` の `minBuffer = max(2, risk*.15)` の計算式自体は変更しない（タスクDでPython側に移植するのみ）。
- `computeConfidence` 内の信頼度スコア計算（約3568-3681行目）は、既存の `stopPlan.status==='TOO TIGHT'` 時の減点（`risk=Math.max(0,risk-7)`、約3639行目）と信頼度上限キャップ（`cap=49`、約3668行目）を変更しない。BLOCKED化はグレード（`computeGrade`）側でのみ行い、信頼度スコアの計算式には触れない。
- NQX/1パケットのフィールド定義・レコード構造（`nqx1-spec.md` の Records節）は変更しない。タスクEは safety invariants の追記のみで、新規フィールドの追加は行わない。
- 既存の `SAMPLE_NQX`（デモパケット、`nq-nightwatch-nqx-final.html` 内）は変更しない。ただし本作業によりデモパケットの表示グレードが変わる可能性はあり、それは正しい挙動として報告する（絶対規範4）。

---

## 4. 実装順序

```
タスクD（validate_nqx.py） → タスクE（nqx1-spec.md） → タスクA → タスクB → タスクC
```

タスクD/Eを先に行う理由: パケット単体での検証はNightwatch本体より影響範囲が狭く、既存の `INVALIDATION_ORDER` チェックのすぐ隣に追加するだけの独立した変更のため、先に完了させて回帰確認のサイクルを短く保てる。タスクA〜Cは相互に依存する（タスクBの矛盾検出はタスクAのBLOCKED化があって初めて意味を持つ）ため、この順で行う。

---

## 5. 検証手順（各タスク完了後に必ず実施）

### 5-1. `validate_nqx.py` の回帰確認（タスクD/E後）

```bash
python project/engine-contract/validate_nqx.py project/engine-contract/valid-sample.nqx
python project/engine-contract/validate_nqx.py project/engine-contract/invalid-sample.nqx
```

- `valid-sample.nqx` は引き続き `exit 0`（PASS）であること。もしSL_INVALIDATION_TOO_CLOSEで新たにFAILするようになった場合、それはサンプル自体が本来境界ケースだったことを意味する。数値を確認し、サンプル側を修正するかタスクD閾値の解釈を再確認する（閾値自体は変更しない）。
- 新規テストケース `project/engine-contract/test-sl-too-close.nqx` を作成する。内容は実トレード事例を模したミニマル構成（Entry ~28951-28964 LONG、SL 28927.75、Invalidation 28939.75）とし、`SL_INVALIDATION_TOO_CLOSE` エラーが出ることを確認する。

### 5-2. Nightwatch本体の回帰確認（タスクA〜C後）

ブラウザでNightwatchを開き、DEMOボタンでサンプルパケットを読み込んだ上で以下を確認する:

1. 既存デモパケットのシナリオが、変更前後でグレードがどう変わったか記録する。
2. 実トレード事例相当のテキスト（本指示書冒頭のInvalidation/SL数値）をINTELペインに貼り付けてコンパイルし、Mission Control（シナリオカード）で当該シナリオが `BLOCKED` として明示され、かつ `HARD STOP CRITICALLY INSIDE STRUCTURE` 相当の文言が表示されることを確認する。
3. Risk Gate行（`rxRiskGate`表示）・下部ステータスバーの `⚠ RISK GATE BLOCKED` 表示（約5408-5410行目のロジック経由）が正しく反応することを確認する。
4. コンソールエラーが出ていないこと（JS構文・実行時エラーの両方）を確認する。

---

## 6. 報告様式

`project/strategy/GPT_IMPLEMENTATION_REPORT.md` に追記専用で記録する。既存ファイルの記法（フェーズ完了ごとの見出し・完了条件との対照表・自己監査節）を踏襲する。今回のフェーズ名は `## SLゲート強化完了 (日付)` とする。

必須記載事項:
- タスクA〜Eそれぞれの変更行番号（実装後の実際の行番号）
- 5-1・5-2の検証結果（数値・スクリーンショットまたはコンソール出力の要約）
- 絶対規範との対照による自己監査
- 変更前後でのデモパケット表示の差分（もしあれば）

---

## 7. 用語集（本指示書固有）

| 用語 | 定義 |
|---|---|
| TOO TIGHT | `structuralStopPlan` が返す状態。SLが推奨構造距離の内側にある、またはリスク距離が最小値を下回る |
| Advisory | `sc.advisories` — UIに表示されるが、グレードのBLOCKED化には影響しない助言レベルの指摘 |
| Warning | `sc.warnings` — グレード計算・Risk Gate表示に影響しうる重大な指摘 |
| Invalidation | 「このシナリオの前提が崩れた」と判定する価格。Hard SLより内側（エントリーに近い側）に置かれ、SLより先に到達すべきもの |
| Hard Stop / SL | 実際の損切り注文が置かれる価格 |

---

*本指示書は既存コードに既に存在する判定ロジックを正しい配線先へ接続する作業であり、新しいSL計算モデルの発明ではない。数値・閾値は本指示書が明示した値、または既存コードに既にある定数のみを使用すること（絶対規範1）。*

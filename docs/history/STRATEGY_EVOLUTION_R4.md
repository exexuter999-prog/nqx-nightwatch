# 戦略進化指示書 R4 — 概念監査で見つかった抜けの補完(Opus 5 向け)

**発行**: 2026-08-18(Fable 5 による MSNR/VP/キーレベル/ATR/VWAP の概念監査に基づく)
**前提**: R1〜R3 実装済み・検収済み。本書は「ブレーキの増設」ではなく
**利益を取る経路の補完と観測の拡充**が主眼。

---

## 0. 監査結論 — 何が欠けているか

R1〜R3 は「負ける型を止める」方向の整備だった。監査の結論は、
**歴史的に勝ってきた型のうち2系統に、いまだ武装経路が無い/半死文がある**こと。

| # | 発見 | 重大度 | 根拠 |
|---|---|---|---|
| 1 | **VWAP 反応型に武装経路が無い。** VWAP 系レベルは恒久的に `DYNAMIC_LEVEL` で拒否される(§9.3)。しかし 8/6(+$1,500)は VWAP 下抜けの日であり、8/12 の勝ちシナリオの表題は「VWAP reclaim toward C: POC」だった。**R1 の FLIP 欠落と同型の構造欠陥** | **P0** | operational-gates §9.3 / CLAUDE.md §9 実績 |
| 2 | **「A+のみ」帯が半死文。** ボラ比率 0.4〜0.6 帯は「A+のみ提案可」だが、A+ の設定品質定義が存在しない(算術条件「構造幅 ≤ SL上限−ノイズ床」だけ)。実務ではこの帯は事実上の全停止になっている | **P0** | CLAUDE.md §3:318 |
| 3 | **レベルに階層が無い。** msnr_gate の dedupe は「先に現れた方を残す」ので、Weekly High が 09:30 オープンに吸収され得る。合流(mergedWith)は記録するだけで強度に使っていない | P1 | msnr_gate.py dedupe |
| 4 | **TP に到達可能性の概念が無い。** SL 側は4重のゲートがあるのに、TP は構造ロードブロックの位置だけで置かれ、「その距離を今の ATR で到達できるか」を誰も検算しない。TP 延長の議論も数字を持たない | P1 | §4 TP延長 / operational-gates §1 |
| 5 | **VP をレベル線としか使っていない。** VAH/VAL/POC は静的レベル扱いのみ。バリューエリア復帰の受容(80%ルール)・値域移動・未タッチ POC(磁石)という VP 本来の概念が不在 | P1〜P2 | snapshot.levels の用途 |
| 6 | 回転判定「次の1本」は 8/17 の体感より鈍い(108 サイクルで ROTATION 1件)。R2 で保留のまま | P2 | R2 完了報告 |
| 7 | QML/OCL/A/V レベルの生成、MITIGATED、HVN/LVN 判定 — ドクトリンにあるが**現行データ源から機械的に得られない**。捏造しないという原則に従い、明示的にスコープ外を継続 | 対象外 | §9.11 |

### R4 の設計原則(R1→R3 の3往復から学んだこと)

1. **二段階導入**: 新しい型・ゲートはまず **ADVISORY**(summary/watching に表示のみ、
   武装可否に影響しない)で最低 **3営業セッション**回し、実測を HANDOFF に記録
   してから **ENFORCED** に昇格する。R1 の FLIP 欠落も R2 の 0.05pt 保持も、
   実データが無ければ見つからなかった。校正コストを前払いする
2. **合格の定義は両面**: 8/17 の敗者が止まること **と** 8/12・8/6 の勝者が
   通ることを常にペアで検証する。片面だけの検証が R1 の欠陥を生んだ
3. データ源に無いものは実装しない(#7)

---

## 1. P0-1: VWAP 反応経路(vwapPath)— ADVISORY で導入

### 1.1 窓内 VWAP の再計算

bundle の `vwap` は現在値のスナップショットで、バーごとの当時値が無い。
そこで **bars3m から窓内 VWAP を再計算**する(バーは volume を持っている):

```
vwap_i = Σ_{k ≤ i} (hlc3_k × v_k) ÷ Σ_{k ≤ i} v_k     hlc3 = (h+l+c)/3
アンカー = 供給された最古のバー(= 窓内 VWAP。セッション VWAP の近似)
```

- 毎回 `|vwap_last − bundle.vwap|` を **drift** として出力する。
  **drift > 3.0pt なら vwapPath 全体を `VWAP_DRIFT` で無効**(近似が
  チャートの真値から外れている = この手法自体を使わない。fail-closed)
- `v` が欠けたバーが1本でもあれば `INSUFFICIENT_BARS` 扱い

### 1.2 連鎖(FLIP と同型の縮約状態機械)

RECLAIM(BUY)の定義。REJECT(SELL)は鏡像。`tolv = max(2.0, 0.10 × NF)`:

| 段階 | 定義 | 状態 |
|---|---|---|
| 前提 | 直近に `c < vwap_i − tolv` の確定足が2本以上ある(下側に居た事実) | — |
| break | 確定足: `c > vwap_i + tolv` | `VWAP_BREAK` |
| 受容 | 続く3本以内にもう1本 `c > vwap_i + tolv` | `VWAP_ACCEPTED` |
| 到達+保持 | 受容後10本以内の確定足 j: `l_j ≤ vwap_j` かつ `c_j > vwap_j` | `VWAP_HELD`(終端) |
| リセット | 受容後に `c < vwap_i − tolv` → 消滅 | — |
| TTL | break から 15本 / 完成後 10本(既存と共用) | `EXPIRED` |

各判定は**そのバー時点の vwap_i** と比較する(静的スナップショット比較の
未定義問題はこれで解消する)。

### 1.3 出力と導入段階

- 出力に `vwapPath: {side, state, drift, blockers}` を追加。summary 例:
  `VWAP: RECLAIM 受容済·到達待ち(drift 0.8pt)`
- blocker 追加: `VWAP_DRIFT` / `VWAP_RETEST_NOT_HELD` /
  `VWAP_ACCEPTANCE_NOT_CONFIRMED`(§5 の列挙を更新)
- **R4 では ADVISORY**。`promotion` には影響させない。3セッションの実測後、
  ユーザー承認を得て ENFORCED(VWAP 反応型の武装条件)へ昇格する

### 1.4 検証(必須)

- 8/12 の実バンドルが `.secrets` に残っていれば「VWAP reclaim toward C: POC」
  当時のサイクルで RECLAIM が完成することを確認。無ければ合成で代替し明記
- 8/17〜18 の 165 本で drift の分布を報告(3pt 閾値の妥当性確認)

---

## 2. P0-2: A+ の機械的定義 — grade 出力

ボラ比率 0.4〜0.6 帯を死文から復活させる。msnr_gate の各 promotion に
`grade` を追加:

```
grade = "A+"  iff  以下すべて:
  1. 完成連鎖の displacement 実体 ≥ 1.3 × NF(通常の 1.0 より強い初動)
  2. レベルの合流または上位階層: mergedWith ≥ 1 または tier ≤ 2(§3)
  3. freshness ∈ {FRESH, WICK_TESTED}(3回目以降のテストは A+ にしない)
それ以外の allowed=true は "A"
```

- FLIP 連鎖の displacement は「受容を作った2本のうち大きい方の実体」で測る
- CLAUDE.md §3 の 0.4〜0.6 帯の行を更新:
  「A+のみ = **msnr_gate の grade=="A+"** かつ 構造幅 ≤ SL上限−ノイズ床」
  (既存の算術条件は維持して AND)
- これは**新しい停止ではない**(≤0.4 帯の挙動は不変)。0.4〜0.6 帯で
  取れるトレードを定義する変更 — 即 ENFORCED でよい

---

## 3. P1-1: レベル階層と dedupe の修正

### 3.1 tier 表(label の正規表現で判定。大文字小文字無視)

| tier | 対象 | regex 目安 |
|---|---|---|
| 1 | Monthly High/Low・Weekly High/Low・All Time High | `monthly (high|low)|weekly (high|low)|all time` |
| 2 | Previous Day High/Low・Prev Day Mid・未タッチ前日 POC | `previous day|prev day` |
| 3 | セッション高安・VAH/VAL/POC・Weekly/Monthly Mid | `asia|london|new york|vah|val|poc|mid range` |
| 4 | ピボット・CPR | `pp|r[12]|s[12]|cpr` |
| 5 | 時刻オープン・その他 | `\d{2}:\d{2}|market open`・該当なし既定 |

複数マッチは**最小 tier(最上位)を採用**。

### 3.2 変更

- dedupe: 「先に現れた方」ではなく **tier が上の方を残す**(同 tier は先出)。
  吸収したラベルは従来どおり mergedWith へ
- 出力に `tier` と `confluence`(= 1 + mergedWith 数)を追加
- §2 の A+ 条件 2 がこれを参照する
- 挙動変更は「どのラベルが代表になるか」だけなので即 ENFORCED でよいが、
  **既存テストの期待ラベルが変わり得る**。フィクスチャを確認して更新する

---

## 4. P1-2: TP 到達性チェック(REPORT-ONLY・恒久)

```
tpBars15 = |TP − Entry| ÷ NQX_DATA_CT_ATR      (15分足換算の必要本数)
≤ 4.0  : 妥当(1時間以内に到達し得る距離)
>  4.0 : 提示に「⚠TP遠い(15分×N本分)」を必ず併記
```

- 実装場所は **msnr_gate ではなく提示フォーマット**(CLAUDE.md §2)。
  CT_ATR は bundle に無いのでパスBの取得値を使う — 手順として §2 の
  提示フォーマットに1行追加する(コード変更なし)
- **TP 延長を求められたときの §4 の反対理由をこの数字で言う**:
  「新 TP は CT_ATR の N 本分。4本を超える延長には構造根拠に加えて
  時間根拠(セッション残時間)が要る」
- 拒否には使わない。REPORT-ONLY を恒久とする(TP はユーザーの裁量領域)

---

## 5. P1-3: VP 受容チェーン(80%ルールの3分適応)— ADVISORY で導入

バリューエリアの外で受容に失敗し、中へ戻って受容されたら、
反対側のバリューエンドまで回転しやすい(VP の 80% ルール)。
現行の型リスト(戻り売り・反発買い)に無い**第3の利益源**。

```
前提: levels に VAH と VAL の両方が存在(欠けたら評価しない)
上側の例(下は鏡像)。tol は既存共用:
  OUTSIDE:   c > VAH + tol の確定足が2本以上
  RE_ENTRY:  c < VAH − tol の確定足
  ACCEPTED:  続く3本以内にもう1本 c < VAH − tol
             → 目標域 = VAL(反対端)。summary に
               「VP: VA復帰受容 → VAL 30,xxx 目標域」
  リセット:  c > VAH + tol で消滅 / TTL 15本
```

- 出力に `vpPath` を追加(vwapPath と同じ形)
- **R4 では ADVISORY**。武装経路にしない。3セッションの実測
  (何回発火し、目標域まで何 pt 走ったか)を HANDOFF に記録してから判断

---

## 6. P2: データ調査タスク(挙動変更なし)

1. **回転判定の感度**: 「否定 = 次の1本で全戻し」を「2本以内」に変えた場合の
   ROTATION 発火数を 165 本で比較する表を作る(変更は提案のみ。適用しない)
2. **未タッチ前日 POC(NPOC)**: 現行の Pine ラベルに前日 POC が出るか確認。
   出るなら tier 2 に含め、タッチ済みかを bars で判定して summary に磁石注記。
   **出ないなら BLOCKED-BY-DATA と記録して終了**(捏造しない)
3. **スコープ外の再確認**: QML/OCL/A/V の自動生成・MITIGATED・HVN/LVN は
   引き続き実装しない。理由(データ源に無い)を §9.11 に追記

---

## 7. 作業一覧と順序

| # | ファイル | 内容 |
|---|---|---|
| 1 | `msnr_gate.py` | §1 vwapPath / §2 grade / §3 tier+dedupe / §5 vpPath |
| 2 | `tests/test_msnr_gate.py` | §8 の新規テスト。既存は dedupe 変更の影響のみ更新 |
| 3 | `CLAUDE.md` | §3 A+ 行の更新 / §2 提示フォーマットに TP 到達性1行 / §7 summary の扱いは不変 |
| 4 | `operational-gates.md` §9 | vwapPath・vpPath(ADVISORY と明記)・grade・tier・blocker 列挙。両コピー SHA 一致 |
| 5 | `HANDOFF.md` | 完了記録 + **ADVISORY 観測の記録欄**(3セッション分の枠を作る) |
| 6 | §6 のデータ調査2件 | 結果を HANDOFF に表で残す |

**触ってはいけないもの**(不変): monitor_publish.py / order.py /
telegram_bot.py / nqx_state.py。チャート設定変更・発注禁止。

## 8. テスト(最低この10本)

| テスト名 | 期待 |
|---|---|
| `test_vwap_reclaim_full_path` | 下側2本→break→受容→到達保持 → VWAP_HELD |
| `test_vwap_reject_mirror` | 鏡像で VWAP_HELD(SELL) |
| `test_vwap_drift_disables` | 再計算 VWAP と bundle.vwap の乖離 >3pt → VWAP_DRIFT |
| `test_vwap_no_prior_side_no_break` | 下側に居た事実が無ければ break 不成立 |
| `test_grade_aplus_confluence` | displacement 1.4×NF + mergedWith 1 + FRESH → grade A+ |
| `test_grade_a_when_third_test` | BODY_TESTED(3回目)→ grade A どまり |
| `test_dedupe_keeps_higher_tier` | Weekly High と 09:30 が 2pt 内 → Weekly High が代表 |
| `test_tier_regex_table` | tier 表の代表ラベル各1つが正しい tier になる |
| `test_vp_acceptance_advisory` | OUTSIDE→RE_ENTRY→ACCEPTED で vpPath が目標域を出す |
| `test_advisory_does_not_affect_promotion` | vwapPath/vpPath がどの状態でも promotion 不変 |

## 9. 検収基準

```
[ ] run_all.py ALL PASS(10ファイル)
[ ] ADVISORY 経路が promotion に一切影響しないことをテストで証明
[ ] 実データ165本: 例外ゼロ / drift 分布・vwapPath / vpPath の発火数を報告
[ ] 8/17 の武装時点(0004/0028/0031)は false のまま(退行なし)
[ ] 8/12 型合成: FLIP_HELD 維持 + grade が付く
[ ] §9 両コピー SHA 一致 / blocker 列挙が新コードを含めて一致
[ ] 監視系4ファイル無変更
[ ] HANDOFF に完了記録 + ADVISORY 観測欄 + §6 調査結果の表
```

R1〜R3 と同じく、定義の解釈変更は禁止。迷ったら仮定を明記して報告する。
**ADVISORY → ENFORCED の昇格判断はユーザーの承認事項**であり、
実装者が独断で昇格させてはならない。

# 戦略進化指示書 R5 — R4 要判断2件への裁定(Opus 5 向け・小規模)

**発行**: 2026-08-18(Fable 5 の R4 検収に基づく)
**前提**: R4 実装済み。本書は R4 が報告した要判断①②への裁定のみ。
**ユーザーへの注記**: この指示書を実装に回すこと自体を §2(bars3m を
140本に増やす)の承認とみなす。60本表示の維持は §2 のとおり保証される。

---

## 0. 裁定の要約

| 要判断 | 裁定 |
|---|---|
| ① A+ 条件3が FLIP 型を構造的に排除 | **FLIPPED を鮮度の許可集合に追加**(条件3 = FRESH / WICK_TESTED / **FLIPPED**) |
| ② vwapPath が drift で8割無効 | **bars3m を 140本供給**し、VWAP はその全量で再計算。チェーン走査と Mini App 表示は従来どおり60本。閾値 3pt は**変えない** |

検収で確認した事実(Fable 5 の独立プローブ):
理想的な 8/12 型 FLIP(初動 20pt ≥ 必要13pt・tier1・合流あり)でも
条件3単独で grade=A に落ちる。条件1・2は FLIP でも成立し得る。
→ ①は R4 指示書(Fable 5 起草)の設計欠陥であり、実装は指示に忠実。

---

## 1. 裁定① — A+ 条件3に FLIPPED を追加

```
条件3(改): freshness ∈ {FRESH, WICK_TESTED, FLIPPED}
```

- **理由**: FLIPPED は「破壊された側から見た初回テストが保持された」状態で、
  フリップ側にとっての WICK_TESTED と同格。これを除外する条件3は
  「完成した FLIP は必ず鮮度 FLIPPED になる」(break が freshness の
  BROKEN 遷移を必ず先に起こすため)という構造と組み合わさって、
  8/12 の勝ち型を 0.4〜0.6 帯から恒久排除していた
- ペアリング条件(FLIP 連鎖のときだけ FLIPPED を許す等)は**付けない**。
  集合に1状態足すだけの最小変更とする。過剰テストは CONSUMED(エピソード
  ≥3 → allowed 自体が落ちる)が引き続き防ぐ
- 条件1(初動 ≥1.3×NF)・条件2(tier≤2 or 合流)は不変

### テスト

- `test_grade_aplus_flip`: 上の実証と同型(SBR・初動 ≥1.3×NF・合流あり)
  → **grade A+**
- `test_grade_a_flip_weak_disp`: 同型で初動 < 1.3×NF → grade A
- 既存 `test_grade_*` は不変のまま通ること

---

## 2. 裁定② — VWAP アンカー問題は「バーの供給量」で解く

drift 中央値 11pt の原因は近似式ではなく**アンカー位置**(窓の最古足 vs
セッション開始)。閾値を緩める修正は禁止。次で解く:

1. **CLAUDE.md §1 パスA step3 と §7**: `data_get_ohlcv count=60` → **`count=140`**。
   bundle の `bars3m` には**取得した全量(最大140本)**を入れる
2. **表示の60本は維持される**(ユーザー選定 2026-08-16): `monitor_publish.py`
   は publish 時に `NQX_FEED_BARS`(既定60)へ自動切詰めするので、
   Mini App と通知に流れる本数は変わらない。**monitor_publish は無変更**
3. **msnr_gate.py**: `rolling_vwap` を「**供給された確定足の全量**」で計算し、
   チェーン走査・freshness・rotation・noiseFloor は従来どおり
   **末尾 WINDOW_BARS(60)本**に限定する(インデックス整合に注意 —
   vwap 配列は全量、bars スライスは末尾60本なのでオフセットを合わせる)
4. **drift ≤ 3.0pt は不変**。アンカーが揃えば縮むはずで、揃わなければ
   経路は無効のまま — それがこの検査の役目
5. CLAUDE.md §7「bars3m は必ず 60本」の記述を
   「**140本入れる(publish 時に表示用60本へ自動切詰め)**」に更新

### 検証(必須・次のライブセッション)

- 過去の 60本バンドルでは検証**不能**(データが無い)。次の監視セッションで
  実測し、drift の新分布(中央値・≤3pt 率)を HANDOFF の ADVISORY 観測欄に
  記録する
- 60本バンドルを食わせたときの**後方互換**(挙動が R4 と同一)をテストで保証:
  `test_vwap_backward_compat_60bars`

---

## 3. 裁定外(現状維持を明示)

- **回転感度(1→3件)**: 変更しない。ゲートを2つ同時に動かさない。
  vwapPath / vpPath の観測3セッションが終わってから再評価
- **vpPath**: 実装は正しい。0発火は監視手順(VAH/VAL 未供給)の問題で、
  R4 で追記済みの「levels に VAH/VAL/POC を必ず入れる」の実行で解消する
- **tier 表の `\bmid\b` 修正**: 承認(仕様表の意図に合致)。
  Opus 5 の採用仮定5点すべて承認

## 4. 作業一覧

| # | ファイル | 内容 |
|---|---|---|
| 1 | `msnr_gate.py` | §1 条件3に FLIPPED / §2-3 全量 VWAP + 60本走査の分離 |
| 2 | `tests/test_msnr_gate.py` | §1・§2 の新規3本。既存は不変で通ること |
| 3 | `CLAUDE.md` | §1 step3・§7 の count=140 と「140本入れる」への更新 |
| 4 | `operational-gates.md` | §9.12 条件3・§9.13 の全量計算を反映。両コピー SHA 一致 |
| 5 | `HANDOFF.md` | R5 完了記録。drift 再計測は次セッションの宿題として明記 |

**不変**: monitor_publish.py / order.py / telegram_bot.py / nqx_state.py。
チャート設定変更・発注禁止。ADVISORY → ENFORCED の昇格判断は引き続き
ユーザー承認事項。

## 5. 検収基準

```
[ ] run_all.py ALL PASS(10ファイル)
[ ] 実証プローブ再現: 8/12 型 FLIP(初動≥1.3×NF・合流)→ grade A+
[ ] 60本バンドルの後方互換テスト PASS(R4 と同一挙動)
[ ] 実データ165本: 例外ゼロ / 0004・0028・0031 false 維持 /
    allowed=true 4件の grade 変化を新旧比較で報告(FLIPPED 追加で
    A+ に変わるものは無いはず — 4件とも条件1で落ちている)
[ ] §9 両コピー SHA 一致 / 監視系4ファイル無変更
[ ] HANDOFF に R5 完了記録 + 次セッションの drift 再計測タスク
```

# ICTBACK 動画の再評価と実装境界

対象動画: `C:\Users\exexu\Downloads\ictback.mp4`（19:33）

この文書は動画内の説明を「外部研究者の主張」として整理したもの。動画の
紙上バックテスト結果を、Nightwatch の実現損益・約定品質・OOS証明とは扱わない。

## 動画から抽出したルール

終盤のレシピ（約18:30）と設定スライドから、採用した定義は次の通り。

- 1分足で、session reference と swing の直近3本を流動性母集団にする。
- 流動性を1 tick以上貫通し、レベル内へ終値を戻す sweep を要求する。
- 方向は sweep から取る。MSS と15分バイアスは要求しない。
- sweep 後の最初の1分FVGを使い、displacement は body >= 1.5 x ATR(14)。
- エントリーはFVG 50%、ストップは displacement swing の外側、目標は固定2R。
- London / NY AM / NY PM の時刻は注釈として記録するが、hard gate にしない。
- limit fill は touch ではなく trade-through として扱う。

動画は、16年の1分データ、NQ+ES、25,003構成、現実コスト、曖昧足をloss扱い、
ES holdout等を説明している。一方、限界として order-block/OTE/breaker entry、
news/day-of-week、FX/gold、そして limit fill の adverse selection は未検証と明記している。

動画内のアブレーションでは、FVG only の net Sharpe 0.33 に sweep を加えると
0.69、displacement追加後は0.59、Silver Bullet時間帯を足すと0.56、15分バイアスを
足すと0.16まで下がると報告している。5分版のrandom-entry比較は p94、1分版と
as_traded は p100 とされる。これは動画の検証結果であり、MNQの独立再現結果ではない。

## 既存エンジンとの照合

既存の通常経路は3分足 MSNR 連鎖で、ICT / Silver Bullet の窓は注釈・スコアカード
用途だった。現行の固定2枚実行契約は TP1 と RUNNER の2つの異なる目標を要求するが、
動画レシピは単一の固定2Rである。この差を別のrunner Rへ勝手に変換しない。

## 実装

`msnr_gate.py` に `SILVER_BULLET_SWEEP_FVG` を追加した。1分足は
`snapshot.bars1m` からのみ読み、欠損時に `bars3m` を代用しない。候補には次の
hard blockerを必ず付ける。

- `SB_LIVE_FILL_UNVALIDATED`: 動画自身が紙上limit fillの adverse selectionを未価格付け。
- `SB_FIXED_2R_SINGLE_TARGET`: 現行の2枚TP1/runner契約と単一2Rが未整合。
- `NO_CONFIRMATION`: sweep/FVGだけは既存の独立確認源の代替にならない。

したがって、候補が表示されても `WATCH` であり、自動発注を武装しない。1分足が
無い場合は `SB_1M_SOURCE_MISSING` として記録する。`snapshot.bars1m` は任意入力として
pipeline / compact / `tv_snapshot` の境界だけを拡張し、既存3分監視の必須取得順は変更しない。

## 未検証として残すもの

この実装だけでは、動画のNQ/ES結果をMNQの実約定結果へ一般化できない。次の昇格には、
1分足の完全な履歴、trade-through基準の約定再現、曖昧足・手数料・slippageを含む
独立バックテスト、walk-forward / holdout、そして現行2枚splitへ適合する目標設計が必要。

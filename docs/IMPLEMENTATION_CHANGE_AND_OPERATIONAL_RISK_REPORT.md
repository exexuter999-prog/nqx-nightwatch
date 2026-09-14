# 実装変更・運用不具合リスク報告書

発行日: 2026-09-03  
対象: `C:\Users\exexu\Downloads\nq-nightwatch-claude-code-handoff`  
対象資料: `C:\Users\exexu\Downloads\ictback.mp4`、既存コード、既存テスト

## 0. 要旨

今回の変更は、ICTBACK 動画の Silver Bullet レシピを **1分足の監査用・候補表示用モデル** として追加したものである。現行の3分足 MSNR 経路を置き換えず、1分足が無い場合は明示的に `SB_1M_SOURCE_MISSING` とする。

ただし、現時点では動画の結果を MNQ の実運用成績へ昇格できない。候補には、紙上 limit fill の未検証、動画の単一固定2Rと現行の2枚 TP1/runner 契約の不整合、独立確認不足という hard blocker を付けているため、候補が検出されても `WATCH` に留まり、自動発注へは到達しない。

## 1. 依頼と参照資料の扱い

今回のユーザー依頼は「変更箇所と、それによって生じうる運用上の不具合をまとめた報告書の発行」である。貼付テキストおよび `ictback.mp4` は、実装内容を検討するための参照資料として扱い、そこに含まれる主張を自動発注の許可、リスク上限の変更、または口座運用指示とは扱っていない。

動画由来の定義・数値は、動画内の主張として別紙 [VIDEO_REEVALUATION_SILVER_BULLET.md](<C:/Users/exexu/Downloads/nq-nightwatch-claude-code-handoff/docs/VIDEO_REEVALUATION_SILVER_BULLET.md>) に記録している。動画の紙上バックテスト結果を、MNQ の独立検証結果や実約定品質とは扱わない。

## 2. 変更したファイルと変更内容

| ファイル | 加えた変更 | 直接の運用影響 |
|---|---|---|
| [msnr_gate.py](<C:/Users/exexu/Downloads/nq-nightwatch-claude-code-handoff/msnr_gate.py:1740>) | `SILVER_BULLET_SWEEP_FVG`、`ICTBACK-AS-TRADED/1`、1分足・ATR14・1.5倍 displacement・1 tick sweep・直近3水準・固定2Rの定義を追加。sweep→最初のFVG→50% entry→displacement swing 外側 stop を評価。 | `snapshot.bars1m` が存在する時だけ新しい候補評価が動く。3分足の代用はしない。 |
| [msnr_gate.py](<C:/Users/exexu/Downloads/nq-nightwatch-claude-code-handoff/msnr_gate.py:2434>) | 候補を既存 candidate schema に写像し、`LIMIT`、`TRADE_THROUGH_1_TICK`、単一 target `[2R]` を記録。hard blocker を付与。 | 検出時に primary、decision ID、entry mode、カード表示が変わる可能性はあるが、hard blocker により発注可能状態にはしない。 |
| [msnr_gate.py](<C:/Users/exexu/Downloads/nq-nightwatch-claude-code-handoff/msnr_gate.py:2499>) | `silver_bullet_evaluation()` を追加。欠損、少数、stale、cadence 不正、無効化済み FVG を明示的な理由へ変換。 | データ品質不良を黙って補完せず、モデル単位で `MISSING` として観測できる。 |
| [msnr_gate.py](<C:/Users/exexu/Downloads/nq-nightwatch-claude-code-handoff/msnr_gate.py:3000>) | Silver Bullet 候補を既存候補群へ追加。primary 選択、scenario、card に `silverBullet` を引き継ぐ。 | 1分足が供給されると、既存候補と並んで選択順位に参加し、表示上の判断主体が変わりうる。 |
| [monitor_pipeline.py](<C:/Users/exexu/Downloads/nq-nightwatch-claude-code-handoff/monitor_pipeline.py:241>) | provider bundle の任意 `bars1m` を normalize して snapshot へ受け渡し。 | 1分足は任意入力。欠損は既存3分足の必須失敗にしない。 |
| [monitor_pipeline.py](<C:/Users/exexu/Downloads/nq-nightwatch-claude-code-handoff/monitor_pipeline.py:503>) | 1分足 stale を `missing` に記録し、3分足 decision の blocking にはしない。 | 3分足判断は継続できる一方、Silver Bullet だけ利用不可になるため、運用画面の解釈ミスが起こりうる。 |
| [tv_snapshot.py](<C:/Users/exexu/Downloads/nq-nightwatch-claude-code-handoff/tv_snapshot.py:83>) | 任意 raw source `bars1m.json` を追加。確認済み1分足だけを snapshot に追加。**間隔不一致は `AcquireError` を捕まえて当該フレームだけ落とし、理由を `snapshot.barsRejected` に残す(2026-09-03 修正。1m / 15m / HTF 共通)**。 | 自動取得器は1分足取得へ変更していない。ファイルが無ければ従来経路のまま。壊れた任意ファイルで必須の3m取得が落ちることはない。 |
| [monitor_publish.py](<C:/Users/exexu/Downloads/nq-nightwatch-claude-code-handoff/monitor_publish.py:413>) | compact 後も `bars1m` を保持し、元件数を `bars1mOriginalCount` に記録。**保持上限は Mini App のチャート枠 `FEED_BARS_MAX`(60) ではなく評価器枠 `FEED_BARS_1M_MAX`(240)(2026-09-03 修正)**。 | 1分足は market payload にも凍結プレビュー URL にも載らないため publish サイズは変わらない。評価器が宣言どおり90本の lookback を使える。 |
| [monitor_config.json](<C:/Users/exexu/Downloads/nq-nightwatch-claude-code-handoff/monitor_config.json>)、[monitor_config.example.json](<C:/Users/exexu/Downloads/nq-nightwatch-claude-code-handoff/monitor_config.example.json>) | `maxAgeSec.bars1m = 180` を追加。 | 1分足の鮮度基準を明示。ただし任意ソースであり、欠損だけでは全体停止しない。 |
| [tests/test_silver_bullet.py](<C:/Users/exexu/Downloads/nq-nightwatch-claude-code-handoff/tests/test_silver_bullet.py>) | 3分足代用禁止、sweep/FVG geometry、50% entry、2R、touch 非約定、full fill 無効化、compact 維持を検証。 | 今後の変更で1分足の意味が3分足へ退化することを検知する。 |

## 3. 実装した評価契約

動画から採用したコード上の定義は次の通りである。

- 1分足のみを使用し、未確定バーを除外する。
- session reference と確認済み swing から、近い3水準を候補母集団にする。
- 1 tick 以上の sweep と、レベル内へ戻る終値を要求する。
- 方向は sweep から決め、MSS と15分バイアスは要求しない。
- sweep 後の最初の1分 FVG と、body が `1.5 × ATR(14)` 以上の displacement を要求する。
- FVG の50%を limit entry、displacement swing 外側を stop、target は固定2Rとする。
- touch は fill とせず、`TRADE_THROUGH_1_TICK` を記録する。
- London / NY AM / NY PM の時刻帯は注釈であり、hard gate ではない。

候補に常時付く blocker は以下である。

1. `SB_LIVE_FILL_UNVALIDATED`: 動画の紙上 limit fill に adverse selection が価格付けされておらず、ライブ約定を証明していない。
2. `SB_FIXED_2R_SINGLE_TARGET`: 動画の単一2Rと、現行の固定2枚 TP1/runner 契約が未整合。
3. `NO_CONFIRMATION`: sweep/FVG は同じ1分OHLC由来で、既存の独立確認源を代替しない。

## 4. 生じうる運用不具合と重大度

重大度は、P0=昇格・発注を禁止すべき契約リスク、P1=判断または監視結果を誤らせるリスク、P2=表示・保守上のリスクを表す。

| 重大度 | 発生条件 | 起こりうる症状 | 現在の防止策・対応 |
|---|---|---|---|
| P0 | 固定2R blocker を手動削除する、または単一targetをrunnerへ勝手に変換する | 現行の2枚 split 契約と異なる注文定義になる。scenario 側でも runner 要件が不一致となる。 | blocker を削除しない。target 契約と実約定・管理・台帳の全経路を別途適合検証するまで昇格不可。 |
| P0 | `SB_LIVE_FILL_UNVALIDATED` または `NO_CONFIRMATION` を確認済みと誤認する | paper fill をライブ fill と誤認、または独立確認なしで A/A+ と解釈する危険 | 候補は `WATCH`。1分OHLCの touch は broker fill の証拠ではない。曖昧な broker state の再送もしない。 |
| P1 | `bars1m` が stale、欠損、または無い | Silver Bullet が候補を出さず `SB_1M_SOURCE_MISSING` / `SB_1M_STALE` になる。オペレーターが「既存3分判断も止まった」と誤解する可能性 | 3分足は継続し、1分モデルだけ unavailable として記録。raw の mtime、最終bar時刻、cadenceを確認する。 |
| P1 | 手動投入した `bars1m.json` の間隔や形式が不正 | ~~`tv_snapshot` の取得処理ごと `AcquireError` で停止~~ → **2026-09-03 修正済み**。1分足だけを落とし、`snapshot.barsRejected` に理由を残す。評価は `SB_1M_REJECTED`(未取得の `SB_1M_SOURCE_MISSING` とは別)になる。pipeline 経由では従来どおり `snapshot.bars1m rejected`。 | 不正ファイルを残したまま運用しない。`bars1mRejected` が出ていたら任意ファイルを除去する。必須の3分経路は止まらない。 |
| P1 | 1分足が供給され、既存候補より高順位または同順位になる | 発注はされなくても `primary.model`、`decisionId`、`setupId`、`entryMode`、カード、監査ハッシュが変わる可能性 | 1分足導入前に、候補選択とUI下流の差分を記録する。`SILVER_BULLET_SWEEP_FVG` が primary でも state は WATCH であることを確認する。 |
| P1 | ~~compact の `FEED_BARS_MAX` により、1分足が最大60本へ切り詰められる~~ | **2026-09-03 修正済み。** 実際の切り詰めは compact だけでなく `monitor_pipeline._source_bundle` の取り込みでも起きており、R13 は compact の**後**に評価するため、これは publish の縮約ではなく**判定入力そのもの**の縮約だった。宣言した90本 lookback は一度も届かず、`bars1mOriginalCount` は切った後を数えていたため常に60を返し、切り詰めを検知できなかった。 | 1分足は market payload にも凍結プレビューにも載らない(送信経路は bars3m のみ)ため、保持上限を評価器枠 `FEED_BARS_1M_MAX=240`(= `msnr_gate.MAX_INPUT_BARS`)に分離した。Mini App の3分チャート枠 `FEED_BARS_MAX=60` は不変。回帰は `test_silver_bullet.py` が固定。 |
| P1 | displacement の「1.5 ATR」を、bodyではなくrangeとして再解釈したい | 動画定義とコード定義がずれ、バックテスト・候補頻度・損益が再現不能になる | 現実装は `body >= 1.5 × ATR(14)`。変更時は動画の該当時刻、定義、look-aheadなしの再テストをセットで更新する。 |
| P1 | session level、swingの左右幅、FVGの最小gap、stop bufferの解釈が動画と異なる | 同じ名称でも検出されるセットアップと stop距離が変わる。`RISK_CAP_EXCEEDED` や `RISK_BELOW_NOISE` の発生率も変わりうる | 現実装の具体値を version として固定。動画の曖昧な部分は「独立検証済み」と表示しない。 |
| P1 | 複数の有効setupを、動画の as-traded 頻度と同一視する | 現実装は方向ごとに最新setup 1件を primary 候補化する。動画側の複数回トレード頻度を再現しない | 約定頻度、同時保有、再エントリー、日次上限を含む別仕様として検証する。現在の実行契約へ未接続のままにする。 |
| P1 | 動画のNQ/ES紙上結果をMNQへ一般化する | MNQのslippage、limit adverse selection、約定拒否、口座リスクを含まないまま優位性を誤認する | MNQ 1分履歴、曖昧足、手数料、slippage、trade-through約定、walk-forward/holdoutを独立に検証する。 |
| P2 | `silverBullet` advisory が常時カードへ追加される | payloadが増加し、厳格な下流schemaやサイズ制限、古いUIクライアントとの互換性に影響する可能性 | 最新検証の card は2473 bytesで、既存の4096 bytes予算内。未知フィールドを拒否する下流がないか確認する。 |
| P2 | `NQX_SB_MODE` が未設定のまま1分足だけ投入される | デフォルト `CANDIDATE` のため、意図せず候補表示が始まる。ただし現実装では hard blocker が残る | 本番で候補表示自体を止める必要がある場合だけ `NQX_SB_MODE=OFF` を明示し、設定変更を監査記録する。 |
| P2 | 停止スイッチに `OFF` 以外の綴りを書く | ~~`0` / `false` / `no` は未知値として **`CANDIDATE` に fail-open** していた~~ → **2026-09-03 修正済み**。`NQX_DECISIVE_STRATEGY` と同じ規則で `OFF/0/FALSE/NO/DISABLED` を OFF と解釈する。 | 停止スイッチは fail-open させない。綴りは `test_silver_bullet.py` が固定。 |

## 5. 現在の運用状態

最新の実データ検証では、snapshot の `bars1m` は0本で、Silver Bullet 評価は次の状態だった。

```text
status: MISSING
reason: SB_1M_SOURCE_MISSING
primary: FLAT
state: WATCH
candidateModels: []
```

したがって、現在の運用では新モデルによる判断・発注への影響は発生していない。`bars3m`、既存の monitor pipeline、発注・自動武装経路、Worker 配置はこの変更で置き換えていない。今回の作業中に broker への注文送信も実施していない。

## 6. 検証結果

- `PYTHONUTF8=1 python tests/run_all.py` → `ALL PASS (60 files)`
- `python -m py_compile msnr_gate.py monitor_pipeline.py monitor_publish.py tv_snapshot.py` → 成功
- [test_silver_bullet.py](<C:/Users/exexu/Downloads/nq-nightwatch-claude-code-handoff/tests/test_silver_bullet.py>) → `Silver Bullet PASS`
- compact と card enrich の実データ検証 → `cardBytes=2473`、`bars1m=0`、`SB_1M_SOURCE_MISSING`
- テスト・検証では外部注文送信なし

これらはコード整合性と fail-closed 境界の検証であり、Silver Bullet のMNQ収益性を証明するものではない。

## 7. 運用上の結論と昇格条件

現時点の安全な扱いは、**Silver Bullet は観測・監査用候補に限定し、既存3分足運用の判断・発注契約を変更しない** である。

本番で1分足を供給する前に、最低限次を満たす必要がある。

1. mtime、bar close、1分 cadence、symbol を含む取得証跡を毎サイクル検証する。
2. ~~90本 lookback と publish後60本制限の差を解消または明示し~~(2026-09-03 に解消済み。取り込み・compact とも評価器枠240本)、完全履歴で再評価する。
3. body/range、level universe、FVG、stop buffer、trade-through の定義を固定して独立バックテストする。
4. MNQの実データで、手数料・slippage・曖昧足・limit adverse selection・holdoutを検証する。
5. 固定2R単一targetを現行の2枚 TP1/runner契約へ適合させる設計を承認する。勝手なR変換は禁止する。
6. broker fill、modify、close、ledger、account scopeを含む実行経路を別途検証し、確認なしに武装・再送しない。

以上を満たすまで、`SB_*` blocker を解除して `ARMED` へ進めてはならない。

## 8. 変更していない範囲

今回の変更対象外は、`order.py`、execution contract、`autotrade_arm.py`、`nqx_cycle.py`、Worker の本番配置、および CrossTrade への直接送信経路である。従って、この変更だけで口座数、固定数量、SL上限、発注権限、または自動武装条件が変更されたわけではない。

## 9. 発行後に発見・修正した実装不具合(2026-09-03)

本報告書の発行後、報告した risk 行のうち3件が「起こりうる」ではなく
**既に起きている実装不具合**であることを確認し、修正した。60ファイルの
既存テストは全て PASS のまま、各件の回帰を
[tests/test_silver_bullet.py](<C:/Users/exexu/Downloads/nq-nightwatch-claude-code-handoff/tests/test_silver_bullet.py>) へ固定した。

1. **1分足の評価入力が Mini App のチャート枠で切られていた。**
   `monitor_pipeline._source_bundle` と `monitor_publish.compact` の双方が
   `FEED_BARS_MAX`(60) を適用しており、R13 は compact の**後**に
   `enrich_decisive_strategy` を呼ぶため、これは publish の縮約ではなく判定入力の
   縮約だった。`SILVER_BULLET_LOOKBACK_BARS`(90) は到達不能で、liquidity level
   母集団だけが静かに縮んでいた。さらに `bars1mOriginalCount` は切った**後**を
   数えており、切り詰めを観測するための監査値が機能していなかった。
   1分足は market payload(`build_market_payload`)にも凍結プレビュー URL
   (`_frozen_preview_url`)にも載らないので、60に揃える帯域上の理由は無い。
   保持上限を `FEED_BARS_1M_MAX = 240`(= `msnr_gate.MAX_INPUT_BARS`)へ分離した。
   3分チャート枠 `FEED_BARS_MAX = 60` は変更していない。

2. **任意の研究入力1本で必須の3m取得ごと停止しうる状態だった。**
   `tv_snapshot.build_bundle` の `bars1m.json` 正規化は `normalize_bars` の
   間隔不一致 `AcquireError` を捕まえておらず、3m の応答を `bars1m.json` へ
   保存した・前サイクルの残骸が残った、といった理由で acquire 全体が毎サイクル
   落ちる経路になっていた。これは `monitor_pipeline.py:503` が明文化している契約
   (壊れた任意フィードが有効な3m判定を消してはならない)と逆である。1分足だけを
   落として理由を `snapshot.barsRejected` に残し、評価側は未取得
   (`SB_1M_SOURCE_MISSING`)と拒否(`SB_1M_REJECTED`)を区別するようにした。

3. **`compact` が評価前に `bars1d` を削除しており、R48 の日足タグが永久に不発だった。**
   `msnr_gate._daily_bars` が読む生の日足は、評価器が直接参照する唯一の HTF 系列で、
   `CLASSIC_TS`(Raschke原典 Turtle Soup)と `decision_context.gap`(ギャップbucket)の
   唯一の入力である。ところが `monitor_publish.compact` は payload 削減のため
   `bars1d` を pop しており、R13 も `monitor_publish.main` も **compact の後**に
   評価するため、`_daily_bars` は常に空を返していた。監査 896 サイクルに
   `CLASSIC_TS` は 0 件、`context.gap` も 0 件。EDGE_LEDGER M7 の特徴量は
   「N が貯まるまで重み付けしない」設計だが、**N が永久に貯まらない**状態だった
   (R25 / R37 と同じ「見えているのに最終判定へ届いていない」型)。
   HTF 生足は market payload にも凍結プレビューにも載らないので、`bars1d` だけ
   残しても送信サイズは変わらない(監査コピーが約4KB増えるのみ)。
   **判定は変わらない**: `feature_tags` は `_finalize_candidate` の**後**に
   `evidence` へ足され、`CLASSIC_TS` は `CONFIRMATION_EVIDENCE` に含まれず、
   `gap` は `select_primary` 後の注釈である。よって grade / state / 武装可否は不変。

4. **任意 HTF(15m / 45m / 1h / 4h / 1D)も 2. と同じ穴を持っていた。**
   `_optional_bars()` に集約し、間隔不一致は当該フレームだけ落として
   `snapshot.barsRejected` に理由を残す(1m の `bars1mRejected` もここへ統合)。
   CLAUDE.md §1 の「HTF欠落は3分足で補間せず `INSUFFICIENT` として無得点」と
   挙動を揃えた。必須の `bars3m.json` は従来どおり fail-closed のままである。

5. **停止スイッチが fail-open していた。**
   `silver_bullet_mode()` は `OFF` という綴りだけを見て、それ以外の値を全て
   `CANDIDATE` に落としていた。本リポジトリで一般的な `NQX_SB_MODE=0` /
   `=false` は無言で有効のままになる。`NQX_DECISIVE_STRATEGY` と同じ規則
   (`OFF/0/FALSE/NO/DISABLED`)に揃えた。

いずれも Silver Bullet の hard blocker、`WATCH` 固定、2枚 TP1/runner 契約、
発注経路、口座スコープには触れていない。§7 の昇格条件は据え置きである。

### 9.1 意図的に修正していない事項

- **`bars3m` の取り込み時60本切り詰め**(`monitor_pipeline._source_bundle` →
  `normalize_market_bars`)。R45 で既知として `vwapSession*` スカラーによる
  補償が入っている。60→240 に広げると `closed_bars` が走査する構造・レベル・
  チェーンの母数が変わり、**武装するシナリオそのものが変わる**。R42 が凍結
  411 サイクルの replay を根拠に窓を変えたのと同じ手順(replay での前後比較)
  を踏まずに触るべきではない。実施する場合は replay 結果とセットで行う。
- **`bars15m` / HTF の60本切り詰め**は実害なし。`htf_context` は
  `MIN_BARS = 20` かつ直近20本しか見ないため、60本で足りている。
- **`tv_fetch.py` が1分足を取得しないこと**。これは不具合ではなく §5 / §7 の
  意図した状態である。取得を足すと Silver Bullet が本番で通電するため、
  §7 の昇格条件を満たす前に足してはならない。
- **`SILVER_BULLET_SWEEP_FVG` が B グレードで `primary` を奪いうること**
  (§4 の P1)。A / A+ より必ず下位に並ぶため発注へは到達せず、表示・
  `decisionId` の変化に留まる。仕様どおりに残している。

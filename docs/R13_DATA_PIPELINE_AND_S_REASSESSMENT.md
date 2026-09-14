# R13 データ取得パイプラインと設計再評価

最終更新: 2026-08-22

## 目的と境界

この文書は、監視のデータ取得を「人が画面を見て判断する手順」から、設定で再現できるシステム経路へ置き換えるための契約である。外部の TradingView MCP は Python から直接呼ばない。取得器は正規化済み JSON スナップショットを書き、`monitor_pipeline.py` がそれを唯一の入力として扱う。これにより、画面操作、発注、秘密情報の管理を取得パイプラインから分離する。

取得パイプライン自体は `DRY_RUN_ONLY` のまま注文を持たない。実発注の通常権限は
Telegram Mini App AUTOがWorkerへ保存する期限付きLIVE武装で、既存の
`autotrade_engine.py` の安全ゲートと凍結済みpublish stateを迂回しない。

## 設定から執行候補までの固定順序

設定は `monitor_config.json`（雛形は `monitor_config.example.json`）に置く。秘密情報、任意のシェルコマンド、発注フラグはこの設定に置かない。

1. **設定検証** — `schemaVersion`、symbol、provider、入力パス、出力先、鮮度上限、CVD retry 入力を検証する。未知の provider、相対パス外参照、構成不足はここで停止する。
2. **入力の凍結** — 初回 JSON を一度だけ読み、UTF-8 バイト列の SHA-256、取得時刻、設定バージョンを記録する。実行中に入力を読み直して結果を変えない。
3. **ソース健全性** — symbol、取得タイムスタンプ、JST/ET セッション、価格、3分・15分足を確認する。古い又は空のソースは `NO_TRADE` とし、推測値で補完しない。
4. **アンカーを持つ ICT 構造** — `rangeTf`、`rangeStart`、`rangeEnd`、`anchorType`、`freshness`、high、low を検証する。ローリング3分足だけの OTE は不合格とする。
5. **VP / PD / DOL / FVG** — VP の値、PD/DOL、FVG の時間足・年齢・displacement・到達前構造を同一スナップショットへ正規化する。存在するが根拠不足の情報は評価へ寄与させない。
6. **CVD 初回取得** — 可視の CVD 値、時刻、履歴、方向を検証し、`cvdAttempts=1` とする。欠落、停滞、又は鮮度超過を明示する。
7. **SMT / peer とイベント** — peer は同時刻・同一セッション・鮮度を必須とする。イベントは検証済み時刻・状態・ルールを保持し、未検証イベントは執行根拠にしない。
8. **CVD だけを一回再取得** — 第6段階の CVD が不健康な時だけ、`cvdRetryInputPath` から CVD フィールドだけを取り込む。価格、足、VP、構造、イベントを再取得結果で上書きしない。2回目も不健康なら `cvdAttempts=2` を記録し、A+ を A に上限設定する。
9. **戦略評価** — 完全な統合スナップショットだけを `msnr_gate.evaluate` へ渡す。LONG、SHORT、FLAT の比較、アンカー、FVG、SMT、CVD、イベント、リスク、セッション終了を一つの決定に残す。
10. **publish 前検証** — `monitor_publish.compact` と状態生成を dry-run で通す。出力は検証済み評価、入力ハッシュ、フェーズログ、次周期の取得要求を含む単一の原子的 JSON とする。
11. **執行ハンドオフ** — 有効な A/A+・ARMED・二枚分割TP・有効リスクの候補だけを、既存の `autotrade_engine` の管理計画へ dry-run で渡す。ingest/pipelineは `reconcile`、注文送信、設定変更を呼ばない。READY後の`monitor_publish`だけがreconcileを呼び、Mini App AUTO LIVEと凍結済みpublish stateの両方を満たす場合だけ実送信へ進める。

## 入力契約

市場データとしての実行必須入力は、symbol、取得時刻、価格、3M確定足、価格ソース、セッションである。15M、range anchor、VP、PD/DOL、FVG、CVD、peer/SMT、イベント、risk は ICT 品質評価に必要な入力であり、欠落を「中立の数字」で埋めない。市場必須の欠落は `BLOCKED / NO_TRADE`、ICT品質入力の欠落は明示した減点又は等級上限として扱う。各不足は `missing[]` と次周期の `acquisitionRequest[]` に出す。

CVD は例外である。初回に欠落又は不鮮明なら再取得を要求し、再取得は一回だけ行う。CVD が依然として使えない候補は分析対象に残せるが、`A+` を返してはならない。再取得失敗は発注失敗ではなく、明示的な品質低下である。

## S 判定の再評価基準

以下は「設計・運用品質」の採点であり、利益率の主張ではない。S はすべて自動テスト又は生成物で再現できることを意味する。

| 領域 | S 判定条件 | R13施策 |
|---|---|---|
| 設定統合 | バージョン付き設定から、入力・出力・鮮度・再取得を一意に決定できる | allowlist 付き file provider と設定スキーマ |
| データ完全性 | 入力ハッシュ、必須欠落、時刻、鮮度、正規化結果が一つの成果物に残る | 凍結入力・フェーズログ・原子的出力 |
| CVD復旧 | 初回判定、CVD限定の一回再取得、二回失敗時のA上限を機械的に実行する | `cvdAttempts` と retry input 分離 |
| ICT整合性 | anchor/FVG/SMT/VP/PD/DOL が評価に渡り、表示だけで消えない | R12 evidence fields を統合スナップショットで保持 |
| 執行安全 | 取得器が注文を作らず、Mini App AUTOと凍結stateを迂回できない | dry-run handoff のみ、`reconcile` 不呼出し |
| 監査可能性 | 同じ設定と同じ入力から同じ評価・要求・ハッシュが再現できる | 決定論的出力と固定された取得順序 |
| 回帰防止 | 正常、欠落、鮮度超過、CVD成功/失敗、発注非実行をテストする | `tests/test_monitor_pipeline.py` |

上の7領域は、実装後にテストが通り、生成物の検査が通った場合のみ S と記録する。テストが未実行、又は一つでも失敗なら該当領域は S ではない。

## R13 再評価結果（2026-08-22）

| 領域 | 判定 | 検証根拠 |
|---|---|---|
| 設定統合 | **S** | `schemaVersion`、file provider、設定ディレクトリ境界、`DRY_RUN_ONLY` の拒否テスト |
| データ完全性 | **S** | 入力SHA-256、原子的入力・出力、時刻/シンボル/tick/市場鮮度の検査 |
| CVD復旧 | **S** | 初回+一回だけの再取得、構造上書き拒否、設定済み鮮度超過、A+→A上限のテスト |
| ICT整合性 | **S** | R12のrange anchor、FVG、同時刻SMT、CVD上限の契約テスト |
| 執行安全 | **S** | ingest/pipelineのnetwork・`reconcile`・`order.py` 不呼出し、Mini App AUTO期限/認証・broker gate回帰テスト |
| 監査可能性 | **S** | config/snapshot/retry hash、phase log、acquisition request、検証済みbundleの固定出力 |
| 回帰防止 | **S** | `python tests/run_all.py` が **ALL PASS (18 files)**。正常、CVD再取得、CVD鮮度超過、市場鮮度超過、LIVE拒否、パスescape、CVD構造上書きを含む |

このS判定は、テスト済みの設計・運用制御に限る。標準設定は入力未投入のまま `BLOCKED` で止まるため、データが無いのにREADY又は発注へ進む経路はない。

## 実証的な有効性は別評価

戦略の収益性をSと呼ぶには、凍結済みルールと実約定の OOS が必要である。少なくとも `oos_ablation.py` の MSNR → PD → DOL → FVG → SMT → Killzone → CVD 比較を、同一 entry/SL/exit/cost で行い、勝率、実現R、MFE/MAE、約定率、コスト後期待値を保存する。さらに NQX の evidence ledger promotion gate（独立OOS 30件以上、コスト後正、t統計量、複数年の安定性等）を満たすまでは、`qm=H` のままとする。

したがって、R13で目指すのは「運用設計の全項目をS」にすることであり、未観測の利益をSと偽装することではない。実証評価は `UNRATED` から開始し、実データによってのみ昇格又は失格が決まる。

# NQ Nightwatch / NQX 資料インデックス

ルートには実行時に直接参照される正本とランタイムスクリプトを残す。
それ以外の資料は用途別に分け、旧版は削除せず `history` / `archive` に保管する。

## 正本（ルート）

- `CLAUDE.md` — 下位エージェント向け運用契約
- `TRADING_CONTEXT.md` — MNQ/Apex/CrossTradeの口座・リスク正本
- `HANDOFF.md` — 現行引き継ぎ
- `SESSION_HANDOFF.md` — セッション起動情報
- `START_HERE_CLAUDE_CODE.md` — Claude Code開始手順
- `CODEX_R11_DECISIVE_ICT_STRATEGY_DIRECTIVE.md` — R11-D判定指示
- `CODEX_ICT_STRATEGY_TASK.md` — ICT/SMT統合タスク

## 資料フォルダー

- `architecture/` — UI・状態サービス・NQX契約の設計資料
- `directives/` — Mini App / Botなどの作業指示書
- `history/` — R4〜R10等の旧戦略・旧ハンドオフ
- `reports/` — 実装報告・残課題・改善記録

## 生成物・退避

- `..\artifacts\packages\` — 配布用ZIP
- `..\artifacts\reference-images\` — 設計参照画像
- `..\artifacts\logs\` — Bot標準出力・エラーログ
- `..\archive\backups\` — 旧版バックアップ

`project/`, `cloudflare/`, `telegram_mini_app/`, `animated-web/`, `tests/` は
実行・ビルド・検証のパスを保つため、今回の整理では移動しない。

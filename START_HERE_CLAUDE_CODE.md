# Claude Code: Start Here

次の順に全て読んでから実装を開始してください。

1. `docs/architecture/CLAUDE_NIGHTWATCH_DESIGN_BLUEPRINT.md`
2. `docs/history/CHART_INNOVATION_ADDENDUM.md`
3. `01-15m-level-rays-full.png`
4. `02-15m-level-rays-clean.png`
5. `03-level-ray-hierarchy-crop.png`
6. `project/00_MISSION.md` の指定順にプロジェクト資料
7. 正本 `project/app/nq-nightwatch-nqx-final.html`

目的は、勝者「Data-First Decision Column」を維持しつつ、15分足へTemporal Level Raysと監査付きScenario Candidate Queueを追加することです。NQX/1、安全監査、No Synthetic Future、無効生成物で既存分析を上書きしない規則は絶対に維持してください。

最初の回答ではコードを書かず、以下だけを返してください。

- 読み取った保護対象
- 変更予定の正確なセレクタ・関数
- 旧`buildChart`を再利用しない確認
- 実装フェーズと各回帰テスト
- 不明データを捏造しないための候補生成ゲート

計画確認後に実装してください。

実装対象は `project/app/nq-nightwatch-nqx-final.html`。完成版を別名で増殖させず、段階ごとのrollback pointを作って正本へ適用してください。

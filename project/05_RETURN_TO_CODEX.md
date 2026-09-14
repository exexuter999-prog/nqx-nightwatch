# Claude回答後

Claudeから `../docs/architecture/CLAUDE_NIGHTWATCH_DESIGN_BLUEPRINT.md` を受け取ったら、次をCodexへ渡す。

1. このZIP
2. ClaudeのBlueprint

送信文:

> Claudeの採用案だけを基準に現行HTMLへ実装してください。NQXエンジンと安全ゲートは維持し、CSSの追加上書きではなく統合を行ってください。フェーズごとに回帰テストし、最終HTML、変更点、テスト結果、未解決事項を返してください。Blueprintが曖昧な箇所は、Market Truth First、Levels Before Routes、04_SCORECARD.mdの順で判断してください。

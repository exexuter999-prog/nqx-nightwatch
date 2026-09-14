# NQ Nightwatch — Design Competition V2

## このパッケージの役割

Claudeに現行サイトの完成コードを書かせるパッケージではありません。

Claudeには次を行わせます。

1. 現行コードと実画面を容赦なく監査する
2. 明確に異なる3つの再設計案を作る
3. 同じ100点評価表で競わせる
4. 85点以上の1案へ決定する
5. Codexが実装できる精度まで設計を落とす

実装はCodexが行います。

## Claudeへ渡すもの

- このZIP
- `01_SEND_TO_CLAUDE.md` の本文

## Claudeから受け取るもの

- `../docs/architecture/CLAUDE_NIGHTWATCH_DESIGN_BLUEPRINT.md`

## 正本

`app/nq-nightwatch-nqx-final.html`

## 成功の定義

サイトを「派手な未来風デモ」から「一目で判断できるプロ用執行ターミナル」へ変える設計が得られること。

現在値、最寄りの上下レベル、現在位置、今の行動、シナリオ状態、Entry、Invalidation、Hard SL、TP1が、装飾に邪魔されず短時間で把握できなければ失敗です。

## 読む順番

1. `01_SEND_TO_CLAUDE.md`
2. `02_DESIGN_COMPETITION_RULES.md`
3. `03_DELIVERABLE_CONTRACT.md`
4. `04_SCORECARD.md`
5. `context/VISUAL_EVIDENCE_BRIEF.md`
6. `context/REFERENCE_PRINCIPLES.md`
7. `context/CURRENT_SOURCE_MAP.md`
8. `app/nq-nightwatch-nqx-final.html`
9. その他の設計・エンジン資料

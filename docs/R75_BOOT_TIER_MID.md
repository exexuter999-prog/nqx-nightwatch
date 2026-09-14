# R75 — 起動 tier「mid」(目玉 + 氷のみ)(2026-09-09)

ユーザー報告: 「氷の新チャートと目玉がページに現れない」。バグではなく R67 の boot guard が
Telegram Desktop を既定で **lite**(WebGL 全停止)にしていたため。ユーザー判断で、Desktop の既定を
「3D 背景だけ止め、目玉と氷は出す」中間 tier に変更した。

## tier 表(`telegram_mini_app/boot_guard.js`)

| tier | 3D 背景 scene3d | 3D の目 eye3d | 氷 ice | 鉄粉 | 音声 | 選ばれ方 |
| --- | --- | --- | --- | --- | --- | --- |
| full | ○ | ○ | ○ | ○ | ○ | 既定(スマホ・ブラウザ)/ `boot=full` |
| **mid** | × | ○ | ○ | ○ | ○ | **Telegram Desktop の既定** / `boot=mid` |
| lite | × | × | × | ○ | ○ | `boot=lite` のみ(R75 以降、既定にはならない) |
| safe | × | × | × | × | × | 前回の起動が完了していない痕跡(24h)/ `boot=safe` / `safe=1` |

`decideTier()` の返り値は `webgl`(目・氷のどちらかを動かすか)に加えて
`scene3d / eye3d / ice` を別々に持つ。app.js は 3D 背景を `boot.scene3d`、目を `boot.eye3d`、
氷のチャート 3 面を `boot.ice` で読み分け、`if (boot.webgl)` のまとめ判定は残さない。

## ピルの挙動

- MID: ピルに「MID」。タップで `boot=full`(3D 背景まで点ける、明示の段階上げ)。
- SAFE / LITE: タップで記録を消し、`boot` / `safe` パラメータを外して端末の既定
  (Desktop なら mid、他は full)で開き直す。R67 までは常に full へ戻していたが、Desktop で落ちた
  直後に full へ戻すのは再発の近道だった。
- どの tier でも、落ちれば次の起動は safe(24h)。

## 期待と観測

- Desktop の WebGL コンテキストは 3 → 2(目・氷)。落ちるかどうかは実機で見る。落ちるなら次は
  「氷だけ」「目だけ」に分けるか、DPR を下げる(webview-memory-budget 参照)。
- Chromium(Browser pane)で `?demo=1&boot=mid`: scene3d チャンクは要求されず、eye3d / icescene は
  読み込まれ、ピルに MID が出ることを確認。

## 回帰

`test/boot_guard.test.mjs`(10)。tdesktop → mid、lite は URL のみ、crash → safe → 24h 後 mid、
app.js の配線(scene3d / eye3d / ice の別判定、ピルの遷移先)。

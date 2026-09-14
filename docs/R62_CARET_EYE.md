# R62 — 最後のカーソルは目になる(タイプライター)

2026-09-06 ユーザー要望: 「キーボードアニメーションの最後のカーソルを奇抜な発想で超クールにして」。

## 何が変わったか

`typewriter.js` の打ち終わりの余韻(点滅する棒のカーソル 1.2 秒)を、**眼になって消える** 2.4 秒の
振り付けに置き換えた(余韻 `AFTERGLOW_MS` = 2.6 秒)。

| 時刻(%) | 眼 |
| --- | --- |
| 0 → 14 | 棒のカーソルが横に開いて眼になる(clip-path の楕円が 22%×50% → 50%×31%) |
| 14 → 30 | 虹彩が左へ寄る —— **打った文字を振り返る** |
| 50 → 62 | 虹彩が中央へ —— こちらを見る |
| 71 → 80 | 一度まばたき(楕円の高さ 2%) |
| 90 → 100 | 閉じながら消える |

色は Nightwatch の目: 骨色の強膜(#e9e4d6)、朱の虹彩(acid)、黒の瞳、朱のにじみ(box-shadow)。
上瞼の影は linear-gradient の層。

## 追補(同日): 7 つの型とデモの CARET

ユーザー「いろんなパターンを追加。ダイヤのマークでもいいし、デモで参照できるように」。
型は `html[data-caret="…"]` で切り替え(`typewriter.applyCaretVariant`)。デモの帯の **CARET** で選ぶと、
表示中の `[data-type]` を打ち直して(`replayAll`)その場で見比べられる。選択は端末に覚える
(`nqx.caret.v1`、表示設定なので保存してよい。取引状態は保存しない規律はそのまま)。

| 型 | 振り付け |
| --- | --- |
| eye | 眼になって振り返り、まばたきして閉じる(既定) |
| diamond | 棒が ◆ の紋章に開き、面を返しながら 2 回転(地金の光が走る)、白く光って一点に消える |
| ember | 火が点いて揺れ、火の粉(box-shadow の点)が上へ飛び、炭になって消える |
| glitch | 赤と青緑の残像に裂け、走査線で欠け、横一線に潰れて消える(steps で跳ぶ) |
| candle | ヒゲ 1 本の上で朱の実体が伸び縮みし、線になって消える |
| reticle | 照準環と 4 本の目盛が回りながら締まり、ロックして朱に光り、縮んで消える |
| classic | 従来の点滅 |

全て `::after` 1 枚(DOM は増やさない)、2.4 秒以内(`AFTERGLOW_MS` 2.6 秒の中)。テストが型ごとの
規則・keyframes・時間を検査する。

## 追補(同日): 状況で自動割り振り

ユーザー「状況に応じてカーソルを適当に割り振って」。`typewriter.assignCarets(root, rules, fallback)` が
打つ直前に各 `[data-type]` へ `data-caret` を付ける(最初に当たった規則。selector は要素自身か祖先)。

| 状況 | 型 |
| --- | --- |
| 武装して送れるシナリオ(`.state-card.scenario.armed`) | reticle |
| 監視中のシナリオ・GATES・待機(NO SCENARIO) | eye |
| 建玉(`.state-card.position`) | candle |
| 送信中の注文(`.state-card.order`)・トースト | glitch |
| AUTO の帯(`#onePassMessage`) | AUTO 稼働中なら reticle、それ以外 eye |
| 自動発注パネル | 管理中 candle、経路・claim 中 glitch、他 eye |
| SYSTEM の総合判定 | 全部 UP = diamond、WARN = ember、DOWN = glitch、他 eye |
| 設定の状態語 | classic |

CSS は各型に 2 系統の規則を持つ: **要素自身**の `[data-caret="v"]`(自動割り振り)と、**html** の
`html[data-caret="v"]`(デモの CARET で固定。特異度が高く個別より強い)。どの規則も基準の姿(RESET)から
書くので、別の型の宣言を引き継がない。デモの CARET は **AUTO** が既定(`html` の属性を外す)。
選択は `nqx.caret.v1` に覚える(既定 auto)。

## 作りの制約

typewriter.js は「テキストノードだけを触り、DOM 構造・属性を変えない」規律(テストが子ノード数を固定)。
そのため眼は **既存の `::after` 1 枚**で描く: 形は `clip-path: ellipse()`、虹彩と瞳は `radial-gradient` の層を
`background-position`(em 単位)で動かす。JS はクラスの付け外しと余韻時間だけ。対象は `[data-type]`・
`.toast`・`#onePassMessage`・`#settingsState`。`prefers-reduced-motion` では typewriter が走らないので眼も出ない。

## 検証

- `test/typewriter.test.mjs`: 余韻の長さ(2.6 秒の間 `is-typed` が残る)、CSS の振り付けが余韻内に収まる
  こと、まばたきと振り返りの keyframe があること、typewriter が DOM を増やさないこと。全 204 PASS。
- デモで SCENE を切り替えて再タイプさせ、1.25 秒後(振り返り)と 2.1 秒後(こちらを見る)を確認。

## 同日の追補(バグ狩り): 余韻の最中に打ち直すと型が出なかった

トースト・AUTO の帯・設定の状態語は**同じ要素**へ続けて打つ。直前の打ち終わりの `is-typed` が残った
まま `is-typing` を付けていたので、打っている間は型の振り付け(specificity と後勝ちで型の規則が勝つ)が
出て、打ち終わりに `is-typed` を付け直しても同じクラスの付け直しではアニメーションが再始動しなかった
(2 通目のトーストのカーソルは点滅も型も出ない。デモの CARET を続けて切り替えたときも同じ)。
打ち始めで `is-typed` を必ず外す。テスト「余韻の最中に同じ要素へ続けて打つ」を追加。

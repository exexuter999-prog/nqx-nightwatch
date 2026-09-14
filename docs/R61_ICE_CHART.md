# R61 — 氷のチャート(計画の投影足)の作り直し

2026-09-06 ユーザー要望: 「氷のチャートを作り直してほしい」(実機のスクリーンショットでは、
武装シナリオの投影足が灰色の四角の板に見えていた)。

## 何が変わったか

投影足そのもの(現値 → 建値 → 目標の足列。`icecandles.projectionCandles` / `iceBlocks`)と、
価格→px の配置は変えていない。変えたのは **3D の見せ方**(`telegram_mini_app/icescene.js`)。

| | 旧 | 新 |
| --- | --- | --- |
| 形 | 八角柱(flatShading) | 角を落とした箱(`RoundedBoxGeometry`、面取り 0.14) |
| 動き | Y 軸で **1 秒に一回転**(板が明滅して見えた) | 上面と右側面が見える固定角(tilt −0.22 / yaw 0.42)。6.5 秒周期の ±6° の揺れと 1px の浮き沈みだけ。reduced-motion では静止 |
| 材質 | MeshPhysical(clearcoat + 環境マップ、opacity .46) | 独自シェーダ: **縁が光る**(フレネル)・**内部の霜**(3D fbm、乳白)・**亀裂**(ノイズ等高線の白い筋)・**気泡**・**上面の霜**(上を向く面ほど白い)・**鋭いグリント**(Blinn-Phong 指数 90 / 40、光源はゆっくり首を振る) |
| 色 | 方向の色を塊全体に混ぜる | 氷はほぼ無色。方向の色(teal / red)は塊の**底に沈める**(色の付いた水の上の氷)。建値の塊は白く息をする |
| きらめき | 無し | 塊ごとに 3 点の `Points`(十字の光条、位相ずらしで瞬く) |
| 先の足 | opacity を段階で落とす | 確度で不透明度を連続に落とす |
| SVG 代替 | 1 秒回転 | 3D と同じ 6.5 秒のゆっくりした揺れ |

霜・亀裂の座標は**画面座標(px)**なので、塊が揺れても模様は塊の中に留まる(8〜14px の模様)。
屈折(transmission)は使えない(WebGL の透過はシーン内しか見ない)ので、下のチャートはアルファ
合成で透かし、氷らしさは上の 4 つで作る。カメラはピクセル空間の正射影のまま(SVG のグリッドと
1px もずれない。3D は価格を一切知らない)。

## 同日の追補(ヒゲと起動直後)

- **SL までのヒゲを廃止**(「ヒゲが SL まで延びているのを修正」)。建値の足で SL までヒゲを伸ばして
  いた処理を外し、代わりに**小さなヒゲ**(実体の 12〜24%、`WICK_PATTERN` の決まった並びで乱数なし)
  を全ての足に付ける(「少しも無いのは寂しい」)。建値の足だけ SL 側を少し長く(実体の 35% 以上)
  するが、SL との距離の 45% で止める。どのヒゲも計画のアンカー [lo, hi] の外へは出ない。
- **実体の中のヒゲを消す**: `wickSegments()` でヒゲを実体の上と下の 2 本に分ける(3D の氷は透けるので、
  1 本で貫くと中に線が見えた)。3D・SVG とも同じ関数を使う。
- **起動直後の旧 SVG 氷**: 3D が届くまで SVG の氷を描かない(`ice3dPending`)。WebGL 無し・読み込み失敗の
  ときだけ SVG に落ちる。

## 予算

`icescene.js` は後読みチャンク。`RoundedBoxGeometry`(examples/jsm)を足しても 9.9 kB。
lazy closure 631,579 bytes(警告 640,000 / 上限 665,600 の内)。

## 検証

- `npm test` 202 PASS(`icecandles.test.mjs` の投影の契約は不変)。`npm run build` / `verify:build` 通過。
- デモ(`?demo=1`、ARMED)のシナリオチャートと市況チャートに氷が出る。細部はコンソールから
  `import('/icescene.js')` して 360×300 の canvas に 5 塊を `update()` して確認した。
- 開発サーバで `examples/jsm` を初めて import すると Vite が依存を再最適化して一度 504(Outdated
  Optimize Dep)を返す。再読込で直る(ビルドには関係ない)。

## 同日の追補(バグ狩り): 見えない間は回さない・予算の赤

- **隠れたタブでも氷が回っていた**: WATCH 以外のタブでは watch の面が `hidden`(display:none)になるが、
  RAF は display:none では止まらない。氷の WebGL が見えない画面で 60fps 描き続けていた(電池・GPU・
  メモリ)。`IntersectionObserver` で見えている間だけ回す(eye3d と同じ規律)。見えたら回し直し、
  `destroy()` で observer を外す。
- **`verify:build` が R61 以降ずっと赤だった**: icescene.js が `wickSegments` を icecandles.js(main
  チャンク)から import していたため、Rollup が遅延チャンク icescene → 起動チャンク(main + CSS)の
  import を作り、遅延 closure が 957,365 bytes(上限 665,600)になっていた。実行上は main が既に
  読み込まれているので害は無いが、`npm run ci` は落ちる(デプロイ済みの manifest でも確認)。
  `wickSegments` を依存の無い `wicks.js` に移し(icecandles.js は再 export)、closure 632,211 bytes
  (余裕 33,389)に戻した。**遅延チャンクは main に住むモジュールを import してはならない**
  (1 つ import しただけで main 全体と CSS が予算に乗る)。
- **表示する view が無い状態(起動直後、またはデモを実状態が届く前に閉じた)で `render()` がシナリオの
  チャートを destroy していなかった**: 氷の RAF と WebGL が見えないまま回り続ける。武装の印
  (`body.is-armed`)と消えたカードの鉄粉も残っていた。空状態の分岐でも落とす。

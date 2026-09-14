# R58 — RADAR タブ(市場レーダー)

2026-09-05 ユーザー要望: チャート下の指標読み出し(R56 凡例 8 行)が「ごちゃっててややこしい」。
提案を経て「市場レーダー一覧画面として別ページに設けてもよい」→ 専用タブへ。

## 何が変わったか

- **チャート下の 8 行凡例(`chartlegend.js`)を撤去**し、ドックに **RADAR** タブ(WATCH の右)を足した。
  中身は `telegram_mini_app/radar.js`。WATCH のチャートは軸ラベルと `marketStatus` 一行だけになる。
- 旧凡例は指標の**出所**(VP / VWAP / HTF / CVD / ICT / LIQ / 15M / GATE)で行を切っていたので、
  一行に価格・向き・数量・状態が混ざり、`30,0xx.xx` が数十個並ぶ壁になっていた。RADAR は同じ値を
  **問いの種類**で組み直す。

| カード | 入るもの | 形 |
| --- | --- | --- |
| PRICE MAP | 価格を持つもの全部(VP・開始値・VWAP 帯・EMA・HTF EMA20・BSL/SSL・DOL・FVG/IFVG/CRT ゾーン・GANN・15 分 61.8/TRAIL/HARD SL・MSNR 水準・武装中の ENTRY/SL/TP) | 1 本の梯子。上端が最高値、LAST を挟んで下端が最安値(チャートの縦軸と同じ向き)。各行 = 名前・価格・**現在値からの距離(pt)**。距離の帯が行の背景に伸びる |
| BIAS | 向きを持つもの(1D/4H/1H/45M 構造と EMA 傾き・HTF bias・VWAP 側・CVD・SMT・MSS・IFVG・PO3・15M CT・LTF・REGIME) | チップ(ラベル / ▲▼ + 一語)。色は向きだけ |
| FLOW | 価格ではない計測(CVD + スパークライン・FAST/SLOW・Δ VWAP・ATR 3M/15M・NOISE ×比率) | タイル(ラベルの下に値) |
| CLOCK | キルゾーン(ET 時刻)・QT 位相・イベント | タイル |
| SETUP | MSNR 連鎖 + 最有力モデル・等級・状態、止めている理由 | 二行 + blocker チップ |

- 梯子の規則: 同じ tick の点水準は 1 行に合流(`VAH · BSL`、`WK OPEN · EMA20 45M`)、DOL / MSNR は
  タグ。ゾーンは近い縁までの距離、現在値を含めば `INSIDE`。距離の帯の尺度は日足・4 時間足の EMA20
  (数百 pt 先)を除いて決める(入れると近い水準の帯が全部 1〜2% に潰れる)。VWAP 系は水準リストに
  同名があればそちらを正とし、indicators 側を重ねない。上下それぞれ 24 本まで(近い順)。
- 識別子は `humanize()` で区切りを空けるだけ(`TARGET_HEADROOM_INSUFFICIENT` → `TARGET HEADROOM
  INSUFFICIENT`)。チップの語は短縮表(`ACCEPTED → ACCEPT`、`CONFIRMED → CONF`、`MANIPULATION → MANIP`)。
- **表示専用**。market を読むだけで判定も再計算もしない。距離は現在値との引き算、BIAS の
  「6 ▲ · 4 ▼」は表示したチップの数。値が verified でない(古い / stale)ときは「NO VERIFIED MARKET」。
- 動きは初回表示の `nw-rise` だけ。値の更新は静かに差し替える(3D 面が既に GPU を使う。
  ユーザー: アニメーションは重くなりそうだから任せる)。
- 720px 以上では梯子を左、他のカードを右の 2 列。

## 検証

- `telegram_mini_app/test/radar.test.mjs`(13)。`test/system.test.mjs` のタブ一覧の断言を更新。
  全 189 PASS。`npm run build` / `verify:build` 通過。
- デモ(`?demo=1`)の RADAR タブで 375px / 768px を確認。Browser pane のクリックはタイムアウト
  するので `button.dock-tab[data-view="radar"]` を JS で click() する。

## 既知の限界

- ゾーン(FVG / IFVG / CRT)の距離は縁までなので、同じ価格帯の点水準と並ぶと順序が直感と
  ずれることがある(並びは中点の価格)。
- HTF EMA20 の帯は 100% で止まる(尺度から除いた印)。

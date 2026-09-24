# 🩹 NQ Nightwatch — Nerf Edition(補助輪版)

このリポジトリに置いてある版は **意図的に弱体化してあります**。効くやつは全部、作者の PC の中で
元気に走っています。ここにあるのは「動くけど強くない」版です。走らせると周期の報告行の末尾に
`🩹power 14%` のような出力が付き、Telegram の先頭にも 🩹 が付きます(`python nerf.py` で内訳)。

判定・発注・SL・枚数のコードには手を入れていません。弱くしているのは設定だけです:
`nqx_cycle.py` / `telegram_bot.py` をプログラムとして起動すると環境変数
`NQX_EXECUTION_CONTRACT=execution_contract.nerf.json` が立ち、その子プロセス(pipeline / publish /
engine / order.py / fill_watch)まで含めて **ナーフ契約** `execution_contract.nerf.json` で走ります。
本来の `execution_contract.json` は試験と道具のために残してあり、何が封印されているかは
2 つのファイルの差分(と `python nerf.py`)で全部分かります。

## パッチノート v0.nerf(2026-09-24)

| 対象 | フルパワー版 | この版 |
|---|---|---|
| ICT STDV 投影(目標の差し替え) | LIVE | **封印**。目標は素の構造だけで引く |
| 多層構造文脈(親の仮説・参加判断・選択層) | LIVE | **記憶喪失**。親の仮説を覚えられない |
| 上位足の目標穴埋め | LIVE | **目を閉じている**(実装はある) |
| 押し目深度(建値と SL の平行移動) | LIVE | **浅瀬のみ** |
| VWAP / 流動性プールの SL 逃がし | LIVE | **SL はそのまま狩られる**(本人の希望) |
| `TURTLE_SOUP_REVERSAL` | 稼働 | **亀は冬眠中** |
| `OTE_FVG_PULLBACK` | 稼働 | **押し目待ちの押し目待ち** |
| 発注できる等級 | A+ / A / B | **A+ だけ**(A と B は出禁) |
| 追撃(ピラミッド) | 影運転 | **一発勝負** |
| 反転・指値の乗り換え・取り逃がし台帳・自律修復デーモン | あり | **この版には存在しない概念** |

残しているもの: 成行 SL 距離の門(`marketStopGuard`)・指値の門(`limitGate`)・限月ガード・
イベント封鎖・ボラ床・建玉照会の fail-closed。弱くはしても、危なくはしていません。

## フルパワー版が欲しい人へ

作者に連絡してください: <https://github.com/exexuter999-prog>

フルパワー版は作者の PC に機械ロックが掛かっていて、鍵(アクセスコード)を持っている人しか
別の PC で起動できません。この版を fork して設定を LIVE に戻すのは自由ですが、それはあなたの
責任で、あなたの口座で、あなたの結果です。

## 戻し方(作者用)

```powershell
git checkout master   # フルパワー版(この PC の中だけ。pre-push フックで origin へは押せない)
```

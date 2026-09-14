# 4社20口座：公式料金による再構成

確認日：2026-09-13 JST。対象期間：2026-09-15〜2026-12-12。
対象は評価費、追加費用、funded保有枠、資金繰りに関わる出金条件。コピー可否・自動化許可・ULTRA・運用コードの検証や変更は対象外。

## 結論

元案はFundedNextのプラン混同とMFFUのプラン別保有上限を修正する必要がある。
既存Lucid funded 1口座を保持する前提で、新規購入は19口座。評価の購入だけでfundedが確保されるわけではなく、以下は全口座の合格・有効化を条件とした到達構成。

- 元案に近いRapid Dailyを残し、MFFUをRapid EOD 3口座＋Pro 2口座へ修正：割引条件付き参考額 $2,684.50。
- 費用を優先してFundedNextをFlexへ変更：同条件で $2,151.00。
- この金額はLucid・FundedNext・MFFUの現在の公式割引と、Tradeifyの5%セット割引を使用。Tradeifyの期間限定コードは含めない。
- 全て無割引・個別定価なら、前者 $4,065.90、後者 $3,235.95。
- $1,800で19評価を一括購入できるとの断定は不可。期間限定割引を購入日に再確認する必要がある。

## 公式販売価格

| 会社・50Kプラン | 公開通常価格 | 今回観測した割引価格 | 新規予定数 | 根拠 |
|---|---:|---:|---:|---|
| Lucid Flex | $146 | $105.20、VAULTの公式表示 | 4 | [公式販売ページ](https://lucidtrading.com/#plans)、LucidFlexタブ |
| FundedNext Rapid Daily | $299.98 | 1個目$169.99、2〜4個目各$189.99、5個目$161.49 | 5 | [公式販売ページ](https://fundednext.com/ja/futures)、Rapid→50K→Daily、RAPID |
| FundedNext Flex（代替） | $133.99 | 1〜2個目各$69.99、3〜4個目各$79.99、5個目$67.99 | 5 | [公式販売ページ](https://fundednext.com/ja/futures)、Flex→50K、FNFLEX |
| MFFU Rapid EOD | $209 | $104.50、CLUB | 3 | [公式販売ページ](https://myfundedfutures.com/)、注文確認画面でTotalまで確認 |
| MFFU Pro（代替枠） | $265 | $132.50、CLUB、追加オプションなし | 2 | [公式Pro](https://myfundedfutures.com/plans/pro)、注文確認画面でTotalまで確認 |
| Tradeify Select | $165 | 5口座セット$783.75、期間限定コード別 | 5 | [公式料金表](https://help.tradeify.co/en/articles/14369021-tradeify-pricing-reference) |

Lucid/FundedNextは公開販売画面の表示価格。本人の購入履歴を反映した最終決済額は未確認。FundedNextの5個目15%割引は公開数量選択欄の表示を用いた。既存の購入履歴・キャンペーン変更・併用条件により変わり得る。LucidとTradeifyの決済詳細はログイン後であり、本人名義の注文は作成していない。

MFFUのホームでは$105/$133と丸めて表示されるが、注文確認画面では$104.50/$132.50。集計には後者を採用。

FundedNextのヘルプ料金表にはFlex「最初の5購入$69.99」とある一方、今回の販売画面は「最初の2購入$69.99」。ここでは現在の販売画面を採用した。旧Rapid/Bolt販売終了の注記と、新Rapid Pro/Dailyの販売を混同しない。

4社の対象評価は買い切りで、funded化のactivation feeは$0。表は初回評価費のみで、失格後の再購入・任意追加サービス・取引手数料・決済時の税や為替/カード費用は含まない。ドル円150円は元案の換算仮定であり、現在の為替レートとして検証していない。

## 20口座の構成

| 会社 | 到達構成 | 新規購入数 |
|---|---|---:|
| Lucid | Flex 50K×5、うち既存funded×1 | 4 |
| FundedNext | Flex 50K×5、またはRapid Daily 50K×5 | 5 |
| MFFU | Rapid EOD 50K×3＋Pro 50K×2 | 5 |
| Tradeify | Select 50K×5、合格後Flex選択 | 5 |
| 合計 | 20口座 | 19 |

- Lucid：標準funded枠は世帯5口座。[公式FAQ](https://lucidtrading.com/)
- FundedNext：標準funded枠は個人/世帯5口座。月間新規発行上限10口座。[公式保有上限](https://helpfutures.fundednext.com/en/articles/14261075-how-many-accounts-can-i-hold-with-fundednext-futures-and-what-is-the-maximum-allocation-available)
- MFFU：25K/50Kのみなら合計5口座。ただしRapid EOD 50Kはその内3口座まで。残り2枠にProを入れる案は、この一般枠とプラン別上限を組み合わせた構成。[公式保有上限](https://help.myfundedfutures.com/en/articles/16498635-moving-from-evaluation-to-sim-funded-account)
- Tradeify：標準sim funded枠は個人/世帯5口座。[公式保有上限](https://help.tradeify.co/en/articles/10468251-how-many-simulated-funded-accounts-can-i-have-at-once)

MFFU Proを選ぶ理由は、元案のEOD方式を維持するため。Proの分配は80/20、出金間隔は14日であり、Rapid EODの日次90/10とは異なる。[公式Proルール](https://help.myfundedfutures.com/en/articles/11802674-pro-plan-sim-funded-and-live-account-highlights)

価格と日次出金を優先するなら、MFFUの残り2枠を通常Rapid 50Kへ変更可能な構成になる。現行料金は各$104.50でProより合計$56安い。ただし通常Rapidはfunded段階がIntraday trailing DDであり、EODと同一商品ではない。[公式Rapid](https://myfundedfutures.com/plans/rapid)

限定販売の追加枠商品は、元案の標準50K構成の基礎予算には入れない。

## 費用集計

| 購入 | Rapid Dailyを残す案 | FundedNext Flexへ変更する案 |
|---|---:|---:|
| Lucid Flex×4 | $420.80 | $420.80 |
| FundedNext×5 | $901.45 | $367.95 |
| MFFU Rapid EOD×3 | $313.50 | $313.50 |
| MFFU Pro×2 | $265.00 | $265.00 |
| Tradeify Select×5、5%セット割のみ | $783.75 | $783.75 |
| 合計 | **$2,684.50** | **$2,151.00** |
| $1,800に対する不足 | $884.50 | $351.00 |

Tradeifyの公式サイトは確認時点でコードSEP「50% OFF 2 TIME USE, THEN 30% OFF」、9月14日23:59 EST終了と表示。FAQは5%セット割との併用を認めているが、本人の利用回数および5口座セットへの実適用額は決済画面で未確認。

下記は5口座セット全体にコードが適用できた場合の算術であり、確定見積ではない。販売画面の$83は丸め表示なので、単価$83×5では計算しない。

| Tradeifyセットへの追加コード | Tradeify5口座 | Rapid Daily案の総額 | Flex案の総額 |
|---|---:|---:|---:|
| なし | $783.75 | $2,684.50 | $2,151.00 |
| 30% | 約$548.63 | 約$2,449.38 | 約$1,915.88 |
| 50% | 約$391.88 | 約$2,292.63 | 約$1,759.13 |

Flex案でも$1,800内になるのは50%併用などの条件が揃ったケース。残額は約$40.87しかない。開始日9月15日や着金日まで同じ割引が続くとは置かない。

## 費用優先の再構成案

1. 9/15〜9/20：Lucidの出金を実際に受け取り、使えるドル予算を確定。$1,800は着金後の資金として扱う。
2. 9/21〜10/4：販売価格を再確認し、Lucid Flex×4、FundedNext Flex×5、Tradeify Select×5を購入対象とする。現在の参考額は$1,572.50。既存1＋新規評価14＝合格後15口座。予算残$227.50。
3. 10/5〜10/25：実際の追加着金が$351以上増えた時点で、MFFU Rapid EOD×3＋Pro×2を計$578.50で購入。合格後4社20口座。手数料等がある場合はその分も資金条件に加える。
4. 10/26〜12/12：評価合格、有効化、出金、終了、再評価、Live移行を別々に数える。20口座の同時稼働は目標であり、購入時点での確約ではない。

上記の日付は目標窓。合格日数・出金額・着金日を固定した予測ではなく、実績と現金残高で次へ進む。割引が変われば数量か予算を再計算する。追加着金が無ければ購入を進めない。

元案の日付は月曜始まりになっていない。2026年9月15日は火曜日。上記は初週以外を月曜始まりに修正。9月15日〜12月12日は両端を含め89日。

## 収益予測に直結する訂正

- FundedNext Rapid Daily 50Kは90%分配が標準で、$14.99の90%追加オプションを加える構成ではない。初回に$2,100のバッファが必要。上限は1回$1,200で、5回のPerformance Reward後に口座終了。毎日無制限に引き続ける前提は使えない。[公式Daily出金規定](https://helpfutures.fundednext.com/en/articles/15878210-what-are-the-performance-reward-eligibility-criteria-for-fundednext-futures-rapid-daily-fundednext-account)
- FundedNext FlexはDailyの安売り版ではない。50KのDDは$1,500、評価目標$2,500、一貫性40%は評価中のみ。出金は$200以上の利益日5日、利益の50%・上限$1,500、分配95%、5回で終了。通常上限での手取りは$1,425。[公式Flex出金規定](https://helpfutures.fundednext.com/en/articles/14878865-what-are-the-performance-reward-eligibility-criteria-for-flex-fundednext-account)
- MFFU Rapid EOD fundedの一貫性は無しと公式確認できた。$2,100バッファ、最低出金$500、条件達成後日次、90/10。[公式Rapid EOD規定](https://help.myfundedfutures.com/en/articles/16158363-rapid-eod-50k-a-comprehensive-look)
- Tradeify Select Flex 50Kは2026年9月1日以降の購入分について、5勝日、利益の50%・上限$2,500。元案の$3,000は新規購入へ使わない。[公式Select出金規定](https://help.tradeify.co/en/articles/12853966-select-flex-and-select-daily-payout-policies)
- Lucid Flex 50Kは5日各$150以上、利益の50%・上限$2,000、90/10。5出金後Liveへ移行する規定がある。[公式Lucid出金規定](https://support.lucidtrading.com/en/articles/12945796-lucidflex-payouts)

したがって、元案の「全口座が2週ごとに$1,800」「一巡$36,000」「90日$110,000/$60,000」は、この料金調査から支持できる収益予測ではない。口座別の出金上限、終了/移行条件、実際の利益と着金から再計算する。

失格時の再購入費を一律$2,600に固定することもできない。特にFundedNextは失格しなくても5回で終了するため、継続して5枠を維持するには再評価費と期間を予算に含める。Live移行に伴うSim口座の停止・終了も、失格とは別に追跡する。

今回は料金調査と展開案の再構成のみ実施。購入、口座接続、発注、運用設定の変更は行っていない。

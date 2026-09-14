# 将来のGammaデータ経路

現時点では認可済みproviderが無いため、Nightwatchのgamma状態は`U`のままにする。
Open Interestへ機械的にcall=dealer short、put=dealer long等の符号を付ける方法は採用しない。
OIとGreeksだけでは保有主体・売買方向が確定しないためである。

## 優先順位

1. **CME Group Options Analytics** — NQ options on futuresのcoverageを契約前に確認し、
   対象なら第一候補。5分snapshotのDelta/Gamma/IV等をREST JSONで取得でき、過去データは
   DataMineへ接続できる。
2. **CME futures/options market data + 自前chain計算** — option chain、OI、underlying、
   expiration、Greeksを同時点で揃えられる契約を結べた場合だけ使用する。
3. **Cboe LiveVol / Nasdaq Greeks and Vols** — QQQ/SPX等の米国上場option proxy用。
   NQ先物optionの直接データとして混ぜず、`underlierType=PROXY`で分離する。

## 受入schema

```json
{
  "schema": "NQX_GAMMA_CONTEXT/1",
  "status": "U",
  "source": "CME_OPTIONS_ANALYTICS",
  "provenance": "DIRECT_VENDOR",
  "underlier": "NQ",
  "underlierType": "FUTURES_OPTION",
  "asOf": "2026-08-25T00:00:00Z",
  "spot": 0,
  "expiryScope": [],
  "signConvention": "UNRESOLVED",
  "zeroGamma": null,
  "gammaState": "U",
  "strikes": []
}
```

`status=READY` にできるのは、source/asOf/underlier/expiry/spot/strike/gamma/OIが揃い、
ライセンス上の保存・再配信範囲を確認し、sign conventionが
`DIRECT_VENDOR`又は検証済み`CHAIN_CALC`として明示された場合だけ。欠落・遅延・proxyは
`U`又は`PROXY_ONLY`で、A/A+の昇格根拠にしない。

候補資料: [CME Options Analytics](https://www.cmegroup.com/market-data/greeks-and-implied-volatility-data.html)、
[CME Market Data APIs](https://www.cmegroup.com/market-data/market-data-api.html)、
[Cboe DataShop API](https://datashop.cboe.com/documentation)、
[Nasdaq Greeks and Vols specification](https://www.nasdaqtrader.com/content/technicalsupport/specifications/dataproducts/GreeksandVols_Specification.pdf)

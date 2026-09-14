# SMT Divergences V2 → Nightwatch JSON設定

対象: `SMT Divergences V2 [OutOfOptions]`。この設定は任意。現在のNightwatchは
`data_get_pine_lines / data_get_pine_labels` でもSMTを取得できるため、Webhook受信先が
無い段階ではJSON alertを作らなくてもよい。

## 推奨パラメータ

- `Detect FVG Based SMTs`: ON
- `Merge SMTs`: ON
- `Don't Consider Equal High/Low as Sweep`: ON（equalだけの偽sweepを除外）
- `Formation`: ON
- `Breakage`: ON
- `Alert Delay (candles)`: `1`（確定足。0は画面観測専用）
- `Alert Filter`: `なし`（Bullish/Bearish両方）

## Formation Alert Template

```json
{"schema":"NQX_SMT_ALERT/1","event":"FORMED","type":"{{type}}","timeframe":"{{timeframe}}","startPrice":"{{startPrice}}","startTime":"{{startTime}}","endPrice":"{{endPrice}}","endTime":"{{endTime}}","duration":"{{duration}}"}
```

## Breakage Alert Template

```json
{"schema":"NQX_SMT_ALERT/1","event":"BROKEN","type":"{{type}}","timeframe":"{{timeframe}}","smtTime":"{{smtTime}}"}
```

TradingViewでAlertを作るときは、このインジケータのalert conditionを選び、Webhook URLを
指定する。TradingViewはmessageがvalid JSONなら`application/json`でPOSTする。2FAが必要で、
受信先は80/443番port、3秒以内の応答が必要。認証情報はbodyへ入れない。

Nightwatch側の受領契約は、受信bodyを検証後に同一filesystem上の
`.secrets/tv_raw/smt_alert.json` へ一時ファイル→atomic replaceで保存すること。
`tv_snapshot.smt_from_alert()` は `FORMED` を方向付き観測へ、`BROKEN` を
`lifecycle=INVALIDATED` へ変換する。BROKEN時は古いPine lineへfallbackしない。

## 動作確認

1. TradingViewのAlert logでFormation/Breakageのbodyが1行のvalid JSONになっていること。
2. `smt_alert.json` の更新時刻が240秒以内であること。
3. `python tv_snapshot.py --out .secrets/tv_bundle.json` 後、
   `snapshot.smtObservation.source=smt_alert_json` を確認する。
4. SMT窓外ではデータが届いても採点されない。これは故障ではない。

参考: [TradingView公式Webhook設定](https://www.tradingview.com/support/solutions/43000529348-how-to-configure-webhook-alerts/)、
[SMT Divergences V2公式ページ](https://www.tradingview.com/script/q8awKabu-SMT-Divergences-V2-OutOfOptions/)

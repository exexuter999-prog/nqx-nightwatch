# NQ Nightwatch — Codex実装レポート

## Nightwatch本体

- 正本: `project/app/nq-nightwatch-nqx-final.html`
- V9〜V11のGlass下位cascadeを整理し、最終層をTokens / Layout / Components / Chart / States / Responsiveへ統合。
- chart/data content層のLiquid Glass、glass-on-glass、装飾FX DOMを撤去。
- `buildTruthChart`を唯一のライブ描画経路とし、synthetic future routeを描画しない。
- Temporal Level Rays、Primary / Secondary / Context、origin、cluster、画面外要約を追加。
- 観測OHLCだけを使うPattern Queueを追加。不完全候補を自動シナリオ登録しない。
- NQX/1 parser、validator、version gate、R:R、Invalidation、Event、排他、安全監査を維持。
- 無効packetは既存分析を上書きしない。推定価格の`~`を維持。
- 390pxではデスクトップ縮小ではなく、MISSION tabとsafe-area対応bottom barへ再構成。

## Nightwatch回帰結果

- JavaScript compile: PASS
- Pure rendering: 15 rays / 3 estimated origins / 2 observed candidates
- `NO SYNTHETIC FUTURE`: PASS
- live `routeflow`: 0
- FX DOM: 0
- `validate_nqx.py valid-sample.nqx`: exit 0
- `validate_nqx.py invalid-sample.nqx`: exit 1
- 1440 / 1024 / 768 / 390: horizontal overflowなし
- 390px touch targets: tabs 44px、structure 44px、bottom actions 56px
- 変更前退避: `project/app/nq-nightwatch-nqx-final.pre-codex.html`

## NQX ATR Pressure Map v1.0

- Clean-room Pine v6インジケータを追加。
- ATR trail、extremum、61.8 / 78.6 / 88.6 retracement、EMA 8 / 21 / 55、relative volume、zone lifecycle、risk geometryを実装。
- Pressure Scoreを説明可能な100点尺度に固定し、確率・勝率表示を禁止。
- Data Window plots、WATCH/confirmation alerts、`NQX_ATR_PRESSURE/1` JSONを追加。
- AI UplinkにFrame RolesとEvidence Protocolを追加。

## v1.1 — Chart-TF moving-cloud修正

実チャートで、Chart-TFのFib境界がextremumとATR trailへ毎バー追随し、意図しない階段cloudになる欠陥を確認した。

- Chart-TFは最初の確定retracementでFib、invalidation、ATR、extremumを固定。
- `CONSUMED`を既定非表示にし、新extremum更新後に再武装。
- NIGHTWATCH AUTOを追加: 3分→15分/45分、15分→45分/4時間、45分→4時間/8時間。
- 重複時間足を方向スコアへ二重加点しない。
- Structure / Context値をData WindowとJSONへ追加。

## v1.2 — MTF moving-cloud修正

v1.1の実チャートではChart-TFだけが固定され、Structure / Contextは確定HTFの動くATR境界を再計算していた。このため青・黄の帯が長い階段cloudとして残った。

v1.2で以下を修正した。

- Chart / Structure / Contextを同一の`f_zoneEngine`へ統一。
- 各時間足で最初の確定retracement時に61.8 / 78.6 / 88.6、invalidation、ATR、extremumを独立固定。
- Structure / Contextはtuple全要素の`[1]`と`barmerge.lookahead_on`で完成HTFだけを取得。
- 上位足zoneを`plot.style_linebr`の水平区間へ変更。step geometryを撤去。
- 非アクティブ・consumed上位足zoneを既定非表示。
- Truth Tableへ各roleの`ZONE / BUILD`を追加し、trendとzone存在を分離。
- 非アクティブHTF値はJSONで`null`とし、`NaN`を禁止。
- alert名を実態に合わせて`NQX zone touch`へ修正。

## v1.2検証範囲

- Pine delimiter / quote / contract静的検査
- Confirmed HTF pattern検査
- synthetic future API不使用検査
- Nightwatch JavaScript compileおよびNQX validator回帰
- ローカルPine compilerは存在しないため、TradingView上のPine v6 compileとチャート目視は最終受け入れ項目として残す。

## v1.3 — 履歴fill縦縞修正

v1.2では固定CTゾーンのfillへ現在のPressure Gate色を直接使っていた。スコアが閾値を跨ぐたびに同一ゾーン内の色がバー単位で切り替わり、赤／黄または緑／青の縦縞が発生した。

- zone geometry色をBull=緑、Bear=赤へ固定。
- high-pressure色を現在ラベル、Truth Table header、確定markerへ限定。
- ゾーン位置、lifecycle、score、NQX JSON、安全監査は変更しない。

## Fable独立診断

最新v1.3画像、現行Pine、元Blackflag FTSコードを別診断担当へ渡し、実装変更なしで監査した。診断結果は次のとおり。

- P0の未来参照・確率偽装はなし。HTF tuple全要素`[1] + lookahead_on`は維持対象。
- 元FTSのmodified true range / ATR 28 / factor 5と、v1.3のstandard ATR 21 / factor 3が異なり、白trailとFib幾何が期待から外れていた。
- zoneが初回接触時に生成されるため事前に見えず、接触場所から突然始まっていた。
- 3回目visitとrearmが同一バーで成立し、`CONSUMED`が観測不能になる競合があった。
- `TOUCH`は接触バー数ではなく再侵入回数であり、CT/Structure/Contextの区別が表示されていなかった。
- 三階層fill、trail、marker、label、tableの同時表示が長いHTF帯を過剰に見せていた。

## v1.4 — Fable診断引き継ぎ修正

- ATR kernelを`MODIFIED FTS`既定へ変更し、modified true range + Wilder smoothing、length 28、factor 5を採用。比較用`STANDARD ATR`を残した。
- 最初の確定counter-direction candleで、直前完成バーのFib / invalidation / ATRをsnapshotし、`ARMED V0`として表示する。
- zone外からの確定再侵入だけを`VISIT`として数え、連続滞在バーは加算しない。
- 3回目VISITでconsumed bar/extremumを保存。同一バーrearmを禁止し、その後の新extremum 1 frozen ATR更新でのみ再武装する。
- `TOUCH`を`VISITS`へ変更し、Truth Table、Data Window、JSONでCT / Structure / Contextを分離。
- `NIGHTWATCH AUTO / FULL MAP / CT FOCUS / OUTLINE CAPTURE`のlayer profileを追加。15分以上のAUTOではHTFをoutline中心にする。
- `RECENT`既定でzone/marker描画を直近180 chart barsへ限定。計算、Data Window、JSONは全履歴を維持。
- 白trailは依頼の主要情報なので既定ONを維持し、太さ1・透明度22へ抑制。
- NQX schema、validator、risk order、No Synthetic Future、score非確率契約は維持。

## v1.4.1 — ATR factor回帰の隔離修正

- 実チャートでのv1.4回帰を再診断し、`MODIFIED FTS` kernel、length 28、prior-bar snapshot、VISITS、CONSUMED/REARMは維持した。
- 既定の`ATR trail factor`だけを`5.0`から`3.0`へ戻した。可変trailから生成するFib zoneの過大化を隔離して確認するための最小変更である。
- `f_zoneEngine`、`f_confirmedZoneEngine`、confirmed HTFの`[1] + lookahead_on`、NQX JSON、安全監査には変更を加えていない。
- 次の受け入れ確認はTradingView 15分足で行い、zone幅、現在価格からの距離、`ARMED V0`と直近ローソク足の整合を比較する。
- Fable再監査により、modified Wilderを元FTS同様の0 seed（初回はTR/length）へ修正し、SMA未成立期間のHiLoも原式へ整合。
- JSONの`touch_count`互換別名を削除し、再侵入回数は`visit_count`だけを正本化。

## v1.5 — stale zone整合と機能追加

実チャート再診断で、factor 3.0へ下げてもv1.4の悪化が解消しない真因を再特定した。ARMEDトリガーが1本の逆行足で発火し、trend flipか3-VISIT+rearm以外にゾーンを畳む経路が無いため、強トレンドで「価格から数百pt乖離した凍結ゾーン」が残存し、そのゾーンがscore・INV/STOP・状態表示を汚染していた（実測: 現在値29,186でINV 29648、stale帯にS 78/DEEP MITIGATION表示）。

- stale判定を追加: 帯が現在closeからfrozen ATRの`staleZoneAtr`倍(既定6.0)超に乖離したゾーンを`STALE`とする。lifecycle(VISITS/CONSUMED/rearm)は不変更。
- score整合: staleゾーンをzoneComponent・freshnessPoints・high-pressure gateから除外。zoneClassへ`STALE BULL/BEAR`を追加。実測でS 78→S 50へ正常化。
- リスク整合: stale中のInvalidation/HardStopをライブATR trailへフォールバック。実測でINV 29648→29331(現行trail)へ正常化。リスク順序は維持。
- 表示: stale帯はfill・境界・ラベルを既定非表示とし、`Show stale-zone ghost outline`(既定ON)で点線ゴーストbox+`STALE Vn · x.x ATR AWAY`タグのみ現在バーまで表示。
- 新機能: approach halo(価格が`approachAtr`(既定1.0 ATR)以内でzone境界にzone色haloを重畳)、鮮度ladder fill(ARMED最明→DEEP MITIGATION最暗、zone寿命内で単調減光)、61.8→88.6深度グラデーション、CONSUMEDのrearm進捗%表示(Truth Table)、`NQX zone approach`/`NQX zone stale`アラート。
- Data Window: `NQX_DATA_CT_STALE / STRUCTURE_STALE / CONTEXT_STALE / CT_ZONE_DIST_ATR / CT_REARM_PROGRESS`を追加(計23 plots)。JSONへ`zone_stale`を追加(schema `NQX_ATR_PRESSURE/1`維持、additiveのみ)。
- 初回コンパイルでRE10140(出力series 71 > 上限64)が発生。halo 4本→2本統合と境界線色のconstant化で8スロット削減し解消。
- TradingView実機(MNQ1! 15分)でPine v6 compile PASS、ランタイムエラーなし、Truth Table実値でstale score/リスクフォールバックの動作を確認。confirmed HTF(`[1]`+lookahead_on)、No Synthetic Future、score非確率、リスク順序の安全条件は全て維持。

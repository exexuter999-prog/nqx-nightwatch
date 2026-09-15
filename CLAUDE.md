# CLAUDE.md — R43 structural-edge autonomous execution contract

> このファイルは**監視 PC 上の Claude Code セッション**向けの運用契約で、ここのコマンドは本番の発注・
> Cloudflare・Telegram に直結する。Devin など外部エージェントはこのファイルを自動で Knowledge として
> 取り込むが、**§0 / §6 の手順を実行してはならない**。外部エージェントの制約は `AGENTS.md` 冒頭と
> `docs/DEVIN_TASKS.md` §1(2026-09-15)。

## 0. セッション開始

1. `TRADING_CONTEXT.md` を読む。
2. `python broker_status.py` で建玉の正本を確認する。
3. `python order.py --status` は送信記録の補助確認にだけ使う。
4. 監視入力の `at`、価格、確定足、シンボルを確認する。

このファイルは運用契約、`TRADING_CONTEXT.md` は口座・金額・銘柄の正本、
`order.py` は送信直前の機械ゲートである。古い履歴や手順を推測で復活させない。
**口座 ID そのものの正本は `.secrets/crosstrade.env` の `CROSSTRADE_ACCOUNTS` /
`CROSSTRADE_ACCOUNT_ID_*`**(2026-09-15、リポジトリ公開に伴い文書・テスト内の口座 ID は
`LFF00000000000006` のような 0 埋めの伏せ字に置換した。文書の ID を発注先として使わない)。

## 1. データ取得

- 形成中バーを確定足として扱わない。`at` と価格が古い、欠損、シンボル不一致なら
  新規シナリオを作らない。
- **限月(R102, 2026-09-15)**: 取引限月の正本は `execution_contract.json` の `contract`(`python contract.py --status`)。
  チャートは連続足 `MNQ1!` のままでよい。`tv_fetch` が毎周期 TradingView の symbolInfo から MNQ1! が今指す限月
  (`front_contract`)を解決し、発注先の限月と一致すれば通す。TradingView がロールして食い違った周期は
  `CHART_SYMBOL_MISMATCH`、解決できない周期は `CHART_SYMBOL_CONTINUOUS_UNRESOLVED` で
  BLOCK し(限月そのものを表示していれば厳密一致)、`order.py` は発注先限月の参照価格で指値の通過(`ENTRY_LIMIT_THROUGH_MARKET`)と SL の側
  (`STOP_WRONG_SIDE_OF_MARKET`)を送信前に止める。満期の手前は新規だけ止まる(`CONTRACT_EXPIRY_NEAR`)。
  所有した建玉に保護注文の OCO 組が 0 なら engine が同じ周期で SL/TP を張るか撤退する(`contract.nakedRepair`)。
  ロールは FLAT のときに `docs/CONTRACT_ROLL_CHECKLIST.md` の順で。根拠は `docs/R102_CONTRACT_ROLL_GUARD.md`。
- `VP / ICT / SMT / FVG / DOL / PO3 / CVD` は背景ログではなく、
  `msnr_gate.evaluate()` の候補スコア・モデル選択・根拠として判定に使う。
- ICT OTE は `rangeTf / rangeStart / rangeEnd / anchorType / freshness / high / low` を持つ
  `rangeAnchor` だけから作る。ローリング3分足の高安は表示専用で、OTE根拠にしない。
- FVG は時間足・生成時刻・年齢・displacement・到達前構造を、SMT は同時刻・同一
  `sessionId`・fresh peerを保存する。欠けた証拠を推測で補わない。
- `bars45m / bars1h / bars4h / bars1d` は各時間足の確定OHLCを直接取得する。3分足から
  合成しない。`htfContext.frames` に構造、EMA20傾き、ATR14、最終確定時刻、鮮度を保存し、
  2時間足以上が同方向なら候補へ `HTF_ALIGNED`、反対なら `HTF_CONFLICT` を一度だけ反映する。
- SMTの通常経路は `smt_lines.json / smt_labels.json` でありJSON設定は必須ではない。
  webhookを使う場合だけ `NQX_SMT_ALERT/1` を `smt_alert.json` に保存する。`BROKEN` は
  残存ラインより優先して当該観測を無効化する。設定値は `docs/SMT_ALERT_JSON_SETUP.md`。
- CVDが欠落／停止なら、study値を一度だけ再取得して `cvdMeta.attempts/status/history` を
  保存する。再取得後もfreshでなければCVD方向を使わず、A+だけを禁止してAを上限にする。
- 情報が見えたのに最終シナリオへ影響していない実装は禁止する。根拠は
  `evaluation.decision` の `model / evidence / penalties / targetR / rangeAnchor / cvdHealth` に残す。
- `tv_snapshot.py` が作る `acquisitionReceipt` を監査正本とする。`chart_state.json`、
  `bars3m.json`、`study_3m.json`、`pine_labels.json` は240秒以内に実際に書き換えられ、
  SHA-256と更新時刻が保存されていなければそのサイクルをBLOCKする。ファイルが存在する
  だけでは取得成功とみなさず、前サイクルのrawを黙って再利用しない。

## 2. R11-D の決定

`msnr_gate` は次のモデルから最有力の1件だけを選ぶ。

- `VP80_REVERSION`（VP受容失敗→VA内回帰）
- `TURTLE_SOUP_REVERSAL`（流動性スイープ反転）
- `BREAKER_CONTINUATION`（ブレイク→受容→継続）
- `OTE_FVG_PULLBACK`（ICT OTE と FVG の押し戻り）

ICT premium/discount、killzone、DOL、SMT、CVD、PO3 はモデルの整合・加点・
減点へ反映する。`select_primary()` が A+ > A > B の順で必ず1件に決着させる。
killzone と DOL(HRLR)は減点のみで、武装可否のハードゲートにはしない。ICT が
探さない時間帯でも他条件が全て揃えば成立する。`decision.ictCoverage` に届いた
ICT 入力を記録し、「評価して効かなかった」と「入力が無く無得点」を区別する。
建値/SL を上書きするモデル(OTE)は、その最終的な幾何で R:R と SL 上限を判定する。

- `A+ / A / B` かつ `state=ARMED|ACTIVE` が新規候補。CVDがfreshでない場合は、他条件でA+相当でもAへ上限化する。
  **2026-09-04 ユーザー決定: B も発注可能にした。** 正本は `execution_contract.json` の
  `scenario.allowedGrades`(Worker はビルド時 import)。等級は `select_primary()` の
  順位付け(A+ > A > B)にだけ残り、ハードブロッカーが無ければ B も ARMED になる。
  B のエッジは未実測(R48 スコアカードで自前計測するまで PF を語らない)。
- `FLAT / WATCH`、および契約に無い等級は発注せず、理由だけをログする。
- 同時に複数モデルを武装しない。勝ち筋を逃すための無期限待機を作らない。
- SL は構造否定価格＋必要なバー緩衝で先に決め、MNQ 2枚固定・SL 60pt 以下で
  なければその一件を不合格とする。
- **押し目深度(R86, 2026-09-14)**: SL だけを広げる設計は採らない(実測で改善しない)。
  余裕が要るときは**建値と SL を同じ幅だけ深くする平行移動**で、SL 幅・枚数・ターゲット価格・
  判定(model/grade/state/decisionId)は変えない。対象は建値に構造の錨が無い
  `VP80_REVERSION`(建値 = 直近終値)だけで 0.25N。レベルそのものに建てる BREAKER/TURTLE と
  OTE は深くしない(逆選択で悪化 / 件数不足)。設定は `execution_contract.json` の
  `entryDepth.mode`(OFF / SHADOW=記録のみ / LIVE=置換。**2026-09-14 ユーザー決定で LIVE**)、入口は
  `monitor_publish._apply_entry_depth` → `entry_depth.annotate` の 1 か所。検証は
  `python entry_depth.py --report / --trades`(読むだけ)。根拠と撤退基準は
  `docs/R86_ENTRY_DEPTH_AND_STOP_ROOM.md`。
- **モデルゲート(R89, 2026-09-14)**: `execution_contract.json` の `modelGate.disabled` に
  `{model, variant}`(variant = `ALL` / `RESTING_LIMIT`)を書くと、その候補は評価・記録はするが
  primary に選ばれない(`MODEL_DISABLED` / `RESTING_LIMIT_DISABLED` で WATCH。別モデルが primary に
  なれる)。**2026-09-14 ユーザー決定で `TURTLE_SOUP_REVERSAL` を `ALL` で外した**(戻すのは
  `disabled` を `[]` にする 1 行)。書き換えたら `python -c "import msnr_gate; print(msnr_gate.model_gate_rules())"`
  で `invalid` が空であることを確かめる(不正な行は黙って無視される)。根拠は `docs/R89_MODEL_GATE.md`。
- **初期 SL の穴(R90, 2026-09-15)**: 設定は `execution_contract.json` の `stopLogic`、根拠は
  `docs/R90_STOP_LOGIC_HOLES.md`。(1) `vwapClearance`(**LIVE**): VWAP が SL の近く(±1.0N)に
  あれば SL を VWAP の外側 0.25N へ逃がす(`msnr_gate._candidate_for_chain`、採点の前。60pt 上限・
  R:R が壊れれば WATCH。SL が変わるので decisionId も変わる。記録タグ `VWAP_STOP_CLEARED`)。
  (2) `marketStopGuard`(**LIVE**): 成行に切り替わる周期に、発注時点の価格から SL まで 1.0N 未満なら
  出さない。engine は claim の**前**に公開価格で見送り(台帳 `ENTRY_GUARD_SKIPPED`、HALT でも
  claim でもない)、order.py は `--min-stop-pt` で quote を再検査し `MARKET_STOP_TOO_CLOSE`。
  (3) `flipOrigin`(**OFF**): BREAKER の SL 錨をブレイク足でなく上昇/下降の起点へ。実装済みだが
  逐次再生で改善せず(BREAKER を RISK_CAP_EXCEEDED の WATCH に変えるだけ)、ユーザー判断まで OFF。
  SL が変わる節は FLAT・primary なしのときに切り替える。検証は `python stop_logic.py --policy` /
  `python replay_stop_logic.py`(読むだけ)/ `python tests/test_r90_stop_logic.py`。
- **SL 狩り対策(R103, 2026-09-16)**: 設定は `execution_contract.json` の `stopLogic` の 3 節、根拠は
  `docs/reports/STOP_HUNT_EVIDENCE_2026-09-15.md` と `docs/R103_1_LIQUIDITY_POOL_STOP.md` /
  `docs/R103_3_SWEEP_GATE.md`。(1) `poolClearance`: 元の SL の外側 1N 以内に未回収の流動性プール
  (スイング高安 / セッション高安 / VAH・VAL / 前日高安)があれば SL をその向こう 0.25N へ逃がす(採点の前。
  60pt 上限・R:R が壊れれば WATCH。decisionId も変わる。記録タグ `POOL_STOP_CLEARED`)。逐次再生で
  ΣR +11.6 → +19.1、損切り 14 → 9。**2026-09-16 07:10 ユーザー決定で LIVE**(ループ停止・FLAT のときに切替)。(2) `sweepGate`: 同じ幾何の候補を「プールの掃引→奪還」まで待つ。
  再生では掃引後に通った周期が 0(候補が先に別の幾何になる)で、実効は見送り。**SHADOW で記録のみ**。
  (3) `restingStopRecheck`: 指値を保持している周期に SL 幅を今のノイズ床(寄付き 30 分は寄付き後の
  レンジ中央値との max)で再検査し、LIVE なら R52 と同じ経路で取り消す(`RESTING_STOP_CANCEL`)。
  決済の分類(`STOP_HUNT / WRONG_WAY / DEEP`)は `python model_scorecard.py --excursions`。
  切替は mode の 1 値ずつ、SL が変わる節は FLAT のときに。検証は `python tests/test_r103_sweep_gate.py` /
  `python replay_r103.py`(読むだけ)。
- **口座別リスク上限(`RISK_<口座ID>`)は経路ではなく口座の性質**として扱う(R45)。
  その上限を割る口座は `accountScope` から外して残りの口座で継続し、
  **全口座が払えないときだけ**シナリオを不合格(WATCH)にする。外した口座は
  `executionContract.excludedAccounts` とサイクル注記に必ず残す。
- **口座別 ULTRA(R47)**: Mini App の口座別設定(Worker の `accountPrefs`)で
  ULTRA 対象口座(同時に1口座のみ)と利益目標を指定できる。対象があるとき
  monitor_publish は publish 前に ultra_mode で枚数を再計算し、目標に届けば
  シナリオを ULTRA 枚数(`riskCapSource=ACCOUNT_DRAWDOWN_BUFFER`・比率分割脚・
  scope 1口座)へ焼き直す。**届かなければ通常2枚へ落とさず WATCH で見送る**
  (2026-08-30 ユーザー決定)。リスク上限の正本は残ドローダウンと契約
  `ultra.maxRiskDollarsPerAccount` の狭い側で、`RISK_*`・$240 は当てない。
  engine / order.py / Worker は凍結された riskCapSource から同じ規則を再検証する。

## 3. 自律エントリー

実送信の正本は、Telegram Mini App の **AUTO スイッチ**が Worker/Durable Objectへ
保存する期限付き `NQX_AUTOTRADE_ARM/1` である。認証済みユーザーがAUTOをONにすると
7時間（最大12時間）の `autotrade=true / live=true` が発行され、画面を閉じても期限内は
監視PCが毎サイクル取得する。AUTO OFF・期限切れ・Worker状態取得不能・口座スコープ変更・
限月変更のいずれかで新規ENTRYは即時停止する。ブラウザのlocalStorageはAUTOの正本にしない。

`NQX_AUTOTRADE` / `NQX_LIVE_ORDERS` のOS環境変数は緊急ロック/保守テスト用の上書きとして
残すが、通常運用でONを運ぶ経路にはしない。ローカル `autotrade_arm.py --arm` はCloudflare
未設定時の保守用フォールバックであり、Cloudflare設定済み環境ではアプリ状態が優先される。
状態確認と緊急KILLは次を使う。

```powershell
python autotrade_arm.py --status
python autotrade_arm.py --kill --confirm
python autotrade_arm.py --unkill
```

**設定側の手動HALT(R82, 2026-09-12 ユーザー決定)**: `execution_contract.json` の
`manualHalt.autotrade=true` で自律経路を全部止める。**新規 ENTRY・追撃・建玉管理
(建値移動/トレール/張り替え)のすべて**が止まり、`autotrade_enabled()` は False を返す。
**環境変数と武装台帳より強い** —— `NQX_AUTOTRADE=1` で上書きできる緊急停止は緊急停止
ではない。**撤退だけは塞がない**: `NQX_AUTOTRADE_KILL=1` の全決済と人が叩く
`order.py --flatten` は従来どおり通る。保有中に立てると管理も止まるので、建玉は
ブローカー側 OCO だけが守る状態になる。

**設定ページの手動HALT(R87, 2026-09-14)**: 同じ HALT を Mini App からも立てられる。Worker の
`POST /api/manualHalt`(認証 + CSRF、DO の `manualHalt`、期限なし)に保存し、監視PCは
`autotrade_engine.reconcile` の本番経路(3 分ループと fill_watch)で `autotrade_arm.sync_manual_halt`
が `.secrets/manual_halt_remote.json` へ写す。**Worker が読めない周期は直前の既知値を保つ**
(通信障害で HALT が外れも立ちもしない)。`manual_halt()` は JSON か写しのどちらかで True。
Worker は 2026-09-14 デプロイ済み(Version 063401e9)。**設定ページのスイッチ UI は未実装**。

**追撃(R83, 2026-09-12 ユーザー決定)**: 保有中に**同方向の新規シグナル**が ARMED に
なったら建て増す。枚数は **ULTRA で合計建玉を引き直し**、今の枚数との差だけ足す。
**口座別リスク上限は各エントリー個別ではなく追撃後の合計建玉へ掛ける**
(CrossTrade/Tradovate は同一口座の同方向建玉を 1 本にネッティングするため。
2026-09-12 の 4+6 が SHORT 10 に統合された実測)。判定は `pyramid.evaluate()` の純関数で、
設定は `execution_contract.json` の `pyramid`(`enabled` / `dryRun` / `minGrade` /
`maxAdds`)。**`enabled=false` の間は評価すらしない。`dryRun=true` の間は判定を注記に
出すだけで送らない。** 追撃は平均建値を動かすので、合計リスクは枚数に線形ではない ——
実際の合成建値から引き直し、上限に収まる最大枚数まで刻んで落とす。
**R84(2026-09-12 実装)**: 上の未了部分は `docs/R84_PYRAMID_TRANCHE_MANAGEMENT.md`
(構造トランシェ台帳)で実装した。枚数の集合照合(`lifecycle_quantities`)は合成プランでは
**使わない** —— 脚ごとの三状態(OPEN / CLOSED / INCONSISTENT / PENDING)から期待枚数を
導出する(`tranche.py`)。追撃は**それ自体が完結した分割ブラケット付き成行エントリー**として
送り、既存トランシェの OCO には触れない(`cancelandbracket` を使わない = R78 の裸窓が
構造的に起きない)。台帳は二相コミット: `PYRAMID_CLAIMED`(下書きは `prePlan` /
`postPlanDraft`。**`plan` キーを持たない**)→ `PYRAMID_SENT` → コミット行
(`status=ENTRY_SENT` / `action=PYRAMID_COMMIT`、ここだけが `plan` を持つ)。送信済み・
未コミットの区間は回廊 `base ≤ qty ≤ base+add` で所有権を保ち、**管理は FLATTEN 判定のみ**。
ACCUMULATION 位相(TP1 脚が 1 本でも生きている)では **MODIFY を出さない**。全 TP1 解決後の
最初の MODIFY が統合(`--pyramid-consolidate`)で、そこで 1 組へ畳んで再凍結する。
**現在は影運転**(`enabled=true` / `dryRun=true`)。`dryRun=false` にしてよいのは
Worker(`cd cloudflare && npm run deploy`。人が叩く)を入れた後。
**出口条件は日数でなく件数(2026-09-13 ユーザー決定)**: デプロイ後に追撃なしの普通の
トレードが 1 回完走 → 別々の決定の `PYRAMID_DRYRUN` を 3〜5 件手で確認 → `maxAdds=1` /
`minGrade="A+"` で人が見ている時間帯に初弾、台帳と注文行が全段揃ってから設定を戻す。

AUTOがLIVEの場合は `autotrade_engine.py` だけが `order.py` を呼び、次の順序を守る。

1. Cloudflare 正本が更新済みで、イベント・ボラのゲート後もシナリオが
   `A+/A/B ARMED|ACTIVE` であることを確認する(2026-09-04 に B を追加)。
2. `CROSSTRADE_ACCOUNTS` の**全口座**を `query_position(..., account=口座)` と
   `query_orders(..., account=口座)` で照会し、全て `verified=true, qty=0` かつ
   注文が非blockingであることを確認する。先頭口座の結果を全体へ流用しない。
3. シナリオの `decisionId / entry / stop / targets / side / qty / accountScope` を管理台帳へ凍結する。
4. `order.py` dry-run が通ったときだけ同じ引数へ `--confirm` を付けて一度送る。
5. 応答が失敗・部分成功・不明なら自動再送せず `HALT` とし、建玉を照会して通知する。

AUTO OFFは新規ENTRYを止め、FLAT口座に残る未約定ENTRYを取消経路へ送る。すでに所有権を
凍結済みの建玉は放棄せず、口座別OCO・runner管理・必要な決済だけを継続する。

リミットが現在値を通過している場合は観測値 `--last` 付き成行へ切り替えるが、
`order.py` の滑り・リスク・SL/TP向きゲートを省略しない。

## 4. 保有中の自動変更・決済

2枚は `order.py --split-tp TP1,TP2` により、同じ構造SLを持つ**各1枚の独立OCOブラケット**として送る。`targets[0]` をTP1、最後のターゲットをrunner最終TPとし、異なる2価格が無い案は発注しない。

1. broker quantity=2 の間は2本のTPを全量`--modify`で張り替えない。TP1を消さない。
2. TP1約定後に**その口座の** broker quantity=1 を確認したときだけ、
   `order.py --modify --account <口座>` で残るrunnerのSLを建値以上（SELLは建値以下）へ一度寄せる。
3. 以後はrunnerだけを高値/安値と「守る金額」から計算したトレールで利益方向にだけ更新し、SLを不利な方向へ戻さない。
4. 最終TP到達なのにrunnerが残る、構造SLを越えて建玉が残る、明示的な `forceFlatten` / `NQX_AUTOTRADE_KILL=1` がある場合は口座ごとに `order.py --flatten --account <口座> --confirm`。
5. 分割片脚の送信失敗・部分成功・送信後qtyが0/2以外・照会不能は `HALT`。不足片を自動再送しない。
   **R52(2026-09-05 ユーザー決定)**: CrossTrade/Tradovate は注文行に数量・種別・価格を、建玉行に
   receipt を返さない。この経路では (a) TP1 後の runner 継続所有は「両エントリー脚 FILLED + 建玉 =
   RUNNER 脚枚数 + 平均建値の整合」で認め、(b) `--modify --ultra` の「全脚生存中」判定は建玉枚数
   ではなく保護注文の組数(逆方向 active 行が 2 本 = 1 組)で行い、(c) 張り替え後の照合は
   **構造**で成功とみなす。**価格は裏取りしない**(ブローカーが返さないため)。MANAGEMENT intent の qty
   上限は ULTRA エンベロープの口座別最大枚数(Python / Worker 同一)。
   **R80(2026-09-12)**: (c) の「構造」は **建玉に対する OCO 兄弟** と定義する
   (`broker_status.oco_sibling_pair`、根拠は `docs/R80_OCO_SIBLING_VERIFICATION.md`)。
   新規 2 行・逆方向・**live**(`SUSPENDED` / `PENDING` は不可)・どちらも `parentId`(OSO の親)
   を持たない・生 `ocoId` が**相互**・対に子ブラケットが付いていない・注文 ID と per-order
   receipt が束縛時の対と一致、の全部。`parentId` に潰したリンクや「同じ親」では通さない。
   束縛(`route_identity.bind_replacement_bracket`)・照合(`verify_protective_orders`)・settle
   (`order._protective_settled`)・R78 修復の保護行カウント(`_protective_rows`)が同じ判定器を使い、
   成立しなければ `MODIFY_POSTVERIFY_UNKNOWN`(engine は R78 修復 → HALT)。SL の**価格**は
   従来どおり engine が送った値を信じる。送信ペイロード(`command=cancelandbracket;
   action=<建玉の側>; qty; stop_loss; take_profit`)は CrossTrade 公式の契約どおりで変えていない。
   2026-09-12 01:53 の張り替えは、API 上は相互 `ocoId`・`parentId` 無しの OCO 兄弟だったが、
   当時の `ordStatus` が Working か Suspended かは台帳に残っておらず、従来の照合は
   Suspended の子でも通していた。
6. セッション時刻での自動全決済は `NQX_AUTOTRADE_SESSION_FLATTEN=1` を明示した場合だけ。
7. **決定 ID 単位の管理上書き(R104, 2026-09-16 ユーザー決定。R103 は流動性狩り対策 docs/DEVIN_TASKS.md §3-K)**: 凍結プラン(台帳の行)は
   書き換えない。`.secrets/management_override.json`(`schema=NQX_MANAGEMENT_OVERRIDE/1`、
   `overrides[]` に `{decisionId, finalTarget, trailMode}`)を `autotrade_engine._frozen_plan_record()`
   の出口で乗せる(`apply_management_override`)。`finalTarget` は runner 最終 TP を差し替え
   (MODIFY の take_profit と「最終 TP 到達で FLATTEN」の両方。TP1 を越えない値は無視)、
   `trailMode=BREAKEVEN_ONLY` は TP1 後の SL を建値±1pt の床にだけ寄せ、極値からのトレールを
   出さない。乗った内容は hold 注記 `management override: {...}` と plan の `managementOverride`
   に残る。ファイル欠落・破損・schema 違いは「上書きなし」で周期を止めない。トレードが
   終わったら行を消す(別の決定には効かないので残っても実害は無い)。初出は 2026-09-16 01:34 の
   SHORT 6 @29,284.5(TP2 を P:VAL 29,137 へ、TP1 後は BE のみ)。検証は
   `python tests/test_r103_management_override.py`。

日次ガード(日次損失 −$480 / DAYGOAL / 2連敗 / 損切り後15分冷却)は 2026-08-27 に
**廃止**した。これらの条件で新規を止めることはもう無い。`dayguard.py` は集計と
台帳追記だけを担い、`blocked` は常に False。数字は発注前の表示と戦績に残る。
建玉照会が `UNVERIFIED` のときは新規・変更を止める。台帳にない手動建玉を勝手に
推測管理しない（緊急時は `NQX_AUTOTRADE_KILL=1` の全決済のみ許可）。

**モデル別スコアカード(R48)**: 決済 result には凍結プランの
`model / grade / scenarioId` が焼き込まれ、`trade_journal` が publish 成功時に
`.secrets/model_scorecard.jsonl` へ追記する(`python model_scorecard.py` で集計)。
これは ICT/Alchemist に公開エッジ証拠が無い(EDGE_LEDGER.md R48・2度の走査で
MISSING 確定)ことへの回答で、**モデル別 PF の唯一の正本は自前実測**である。
PF≥1.5 / N≥200 / OOS PF≥1.3(BLUEPRINT §0)を満たすまで「PFが良い」と宣言しない。
R48 の特徴量タグ(CLASSIC_TS / LONDON_RANGE_SWEEP / DISP_RESEARCH_1_5X /
LEVEL_BODY_INTACT / FVG arrivalState / silverBullet / dayMaturity / gap bucket)は
**記録専用**で、採点・武装への重み付けは N が貯まるまで行わない(却下リストは
EDGE_LEDGER.md 参照)。

**Quarterly Theory(R49)**: 時刻は位相(A/M/D/X: 日6h×4→90分×4)を決めるが
方向は決めない。方向は `quarterly_theory.judas_from_true_open` が **true open
(NY 00:00)への誘導→奪還を価格から**観測したときだけ付き(minExc=当日3分
レンジ中央値)、90分クォーターの D 位相でだけ既存カタログの1票になる
(±2票しきい値の中。単独で武装は動かない)。`QT_M_PHASE_SWEEP` /
`QT_JUDAS_ALIGNED` は記録タグで、重み昇格はスコアカード実測後(EDGE_LEDGER M11)。

**決済の記録(戦績)は `trade_journal.py` が毎サイクル自動で行う。** 3分ループが
`autotrade_engine.reconcile()` の直後にこれを呼び、口座ごとの建玉が閉じたことを
観測したら `result` を publish して Mini App の LEDGER へ載せる。決済価格は
**凍結プランの脚**（TP1 / runner最終TP / 構造SL / トレール後SL）だけを使い、
どの脚が落ちたかは建玉枚数の遷移と確定足が触れた水準から決める。どの水準にも
触れていない減少は**推測せず保留**し、人が `/result` で入れるまで publish しない。
金額はブローカーの `realizedPnL` 差分で突合し、差は `fees`(滑り+手数料)に落ちる。
2026-08-27 以前はこの起動点が `telegram_bot.py` 常駐プロセスの中にしか無く、
3分ループから一度も呼ばれていなかったため、実トレードが1件も戦績に載っていなかった。
**R85(2026-09-13)**: 手数料は `.secrets/crosstrade.env` の `FEE_PER_SIDE_<口座ID>`
(1 枚・片道 $、MNQ)があれば **約定枚数 × 単価**で出す。realizedPnL 差分は残高照会の
反映遅れで壊れる(4 枚の往復に $173 が載り、LEDGER が口座より $141 少なかった)ので、
単価が無い口座だけの予備にし、片道 1 枚 $5 を超える額は作らない。窓閉じ後の最初の
サイクル(R81 の settle)で戦績も一度記録する(`/fills` は取引日で閉じるため)。
Mini App の「Balance」は **ブローカーの純資産(equity)**、DD 残は `DD LEFT` として別に出し、
LEDGER の集計は設定中の口座の記録だけで行う(入れ替え前の口座を NET に混ぜない)。

## 5. 重複防止と失敗処理

`.secrets/autotrade_ledger.jsonl` が自律経路の監査台帳である。

- entry は `decisionId` ごとに一度だけ送る。
- modify は `decisionId + action + SL + qty` ごとに一度だけ送る。
- flatten と失敗応答は一度記録し、同じ理由で自動再送しない。
- 台帳が壊れている、書けない、未知の状態がある場合は `HALT`。
- `order.py` のログは送信記録であり、建玉の正本ではない。必ず broker 照会を優先する。
- **HALT の解除(R52)**: ENTRY の HALT は engine がブローカー不在証明で自動回復する
  (`ENTRY_RECOVERED`)。KILL/FLATTEN の HALT(送信結果不明)は自動では解けず、人が
  建玉を照会してから `python autotrade_engine.py --clear-halt <key> --reason "..."
  --evidence "..."` で `HALT_CLEARED` を**追記**する(`--list-halts` で確認)。台帳の
  行を消したり書き換えたりしない。2026-09-04 に 09-01 の KILL HALT 3 件が口座入替後の
  新口座の新規 ENTRY を塞いでいた。
- Worker で `RECOVERED` になった claim は終端であり、`routeState=UNKNOWN` が残っていても
  ロックではない(R52)。回復を試みると観測口座が旧 scope 外のとき DO の 409 で新規が全部
  止まる。

## 6. 監視ループ（具体手順）

### 6.1 時間窓と起動前チェック

- 監視窓は **JST 07:00〜翌05:45、3分間隔**（2026-09-01 に 12:30 から前倒し、2026-09-05 に終了を 04:00 → 05:45 へ延長）。窓の外ではデータ取得・評価・発注を行わず、停止中であることだけを1行で記録する。
  **例外(R81, 2026-09-12)**: 窓が閉じた後の最初の窓外サイクルだけ、`nqx_cycle.settle_after_window()`
  が `nqx_state.sync_position()` を一度呼び、建玉の正本を画面へ反映する。窓の終わり際に決済された
  建玉は `OPEN → CLOSED` を publish されないまま翌 07:00 まで Mini App に残るため（2026-09-12
  05:42:49 決済の SHORT が実例。05:42 サイクルはまだ `hold: managed`、次の 05:45 は窓外で即 return）。
  **読み取りと状態 publish だけで、相場データも武装も見ない。** 1 窓 1 回の印は
  `.secrets/window_settle.json`（JST の日付）で、失敗した周期は印を残さず次の窓外サイクルが再試行する。
- Claude Code と Codex、または監視ループを二重起動しない。1セッション・1ループだけを稼働させる。
- セッション開始時に、次を上から順に1回だけ実行する。Claude Code側の監視開始指示は次の1回だけ送る。

**ループ指示文の正本は `docs/MONITOR_LOOP_PROMPT.md`。** 本文をそのまま `/loop 3m` に貼る。
この節と食い違ったら指示文の側を直す（2つの手順書を並立させない）。

```powershell
cd "C:\Users\exexu\Downloads\nq-nightwatch-claude-code-handoff"
python telegram_bot.py --check
python nqx_state.py --check
python broker_status.py --accounts
python broker_status.py --account <口座ID> --json   # 口座ID は .secrets/crosstrade.env の CROSSTRADE_ACCOUNTS
python order.py --status
python events.py --refresh
python autotrade_arm.py --status
```

- **約定監視(R78/R91)**: `fill_watch.py` が建玉の枚数変化(TP1 約定・決済・約定)を検知し、その場で
  `autotrade_engine.reconcile` を呼ぶ(scenario 無しの bundle なので新規 ENTRY は出ない。3 分ループとは
  reconcile ロックで排他)。止まっていると TP1 検知が 3 分遅れに戻る。**R91(2026-09-15 ユーザー決定)で
  契約 `fillWatch.autostart=true`**: `nqx_cycle` が毎周期 `fill_watch:` 行で生死を出し、止まっていれば
  切り離したプロセスとして起動し直す(単一インスタンス `.secrets/fill_watch.lock`、出力 `.secrets/fill_watch.log`)。
  照会は建玉あり 1.5 秒・FLAT 5 秒、fill_watch 自身は毎秒 1 リクエスト以下、429 で 15 秒待つ、ループの
  reconcile 中は照会しない。CrossTrade の Tradovate 口座は REST のみ(WebSocket は NT8 だけ・約定プッシュ無し)
  なので、これが経路上の最速(実測: 照会 1 本 ≈0.6 秒)。止めるときは `autostart=false` にしてから heartbeat の
  pid を止める。R78 導入後 09-15 まで一度も常駐していなかった(2026-09-11 01:40 に 3 分遅れの建値移動で
  stop が拒否され runner が裸になった)。根拠は `docs/R91_FILL_WATCH_REALTIME.md`。
- **contract 行(R102)**: `nqx_cycle` は `autotrade:` の次に `contract: MNQU6 (CME_MINI:MNQU2026) exp 2026-09-18 (3d) entry=ok`
  を出す。`entry=CONTRACT_EXPIRY_NEAR` は新規停止(管理・撤退は続く)。env / wrangler.toml / monitor_config.json の
  NQX_SYMBOL が正本と食い違えば `HALT: CONTRACT_SYMBOL_DISAGREE` で相場データを取る前に止まる。
- `broker_status.py --accounts` に `CROSSTRADE_ACCOUNTS` の口座が**出てこない場合は発注しない**。2026-08-21 と 2026-08-24 の二度、設定に残った口座が CrossTrade 側から消えており、気付かずに送っていれば宛先不明で失敗していた。口座は黙って消える。
- **`LIFELINE_*` は EOD ドローダウンの切り上がりに合わせて毎日手で更新する。** ブローカーからは取得できず、コードにトレーリングの実装も無い。更新漏れは残機の誤認になり、可変枚数を入れた後はそのままサイズ誤りになる。
- 2回の `broker_status.py --account ... --json` のどちらか一方でも `verified=true` でない、Cloudflare 正本が確認できない、または台帳が壊れている場合は、建玉ゼロと推測しない。そのサイクルは新規・変更を停止し、`HALT` と口座別理由を通知する。
- Highイベントの前15分〜後10分は `monitor_publish.py` が `ARMED/ACTIVE` を `WATCH` に降格する。Mediumは背景情報として記録する。イベントキャッシュが古い場合は `events.py --refresh` を再実行し、更新できなければ通知に「指標カレンダー無効」を残す（既存仕様のfail-open）。その場合も急な要人発言を手動確認し、Highイベントの時刻が判明したら `events.py --add` で追加する。

### 6.2 毎3分サイクル（必ずこの順序）
### 6.2a R13 設定駆動パイプライン（この節を優先）

この節は、§6.2 の従来手順の評価・保存・publish部分、および §6.3 の生bundleを直接
`monitor_publish.py` に渡す手順を置き換える。収集元の画面確認は残すが、**生データから
直接publish又は発注へ進んではならない**。全サイクルは次の一本の順序で進める。

> **R39: この順序は `python nqx_cycle.py` が実装している。段階を手で叩かない。**
> 取得 → ingest（受領書検査）→ pipeline → `READY` かつ `publishPreflight.ready` の
> ときだけ publish → 監査コピー、までを1コマンドで、**この順序でしか実行できない形**に
> 固めてある。UTF-8 の固定も設定検査もこの中でやる。終了コードは
> `0=publish 済み / 1=BLOCKED / 2=HALT`。
>
> 段階を手で並べていた間、R13 パイプラインは**一度も走らないまま** 403 サイクルが
> 旧経路（生 bundle → `monitor_publish.py`）で流れた。順序は書くだけでは守られない。
>
> **R51: 取得もこの中に入った。エージェントの担当は「`python nqx_cycle.py` を1回叩く」
> だけである。** `tv_fetch.py` が MCP サーバ同梱 CLI（`tradingview-mcp/src/cli`）から
> MCP と同一の JSON を受け取り、`.secrets/tv_raw/` へ逐語で落とす。整形は
> `tv_snapshot.py` が決定論的に行う。
>
> **エージェントに MCP の出力を書き写させてはならない。** この転記工程が、実サイクル
> 403本で `rangeAnchor` / `peers` / `po3` / `cvdMeta` の取得率を **0%** にした張本人で
> あり、さらに 2026-09-01 の実測では必須8 raw の鮮度窓 240 秒を自分で割って
> `required raw acquisition is not fresh` を2サイクル連続で出し、1周に20分かけていた
> （3分間隔に間に合っていない）。R51 後は取得 3.5 秒 / 1周 36 秒。

1. `monitor_config.json` を読み、`schemaVersion=NQX_MONITOR_PIPELINE/1`、`type=file`、
   `execution.mode=DRY_RUN_ONLY`、入力・CVD再取得・出力パスを確認する。設定外パス、LIVE設定、
   又は未知providerは停止する。
2. **（R51: この観測は `tv_fetch.py` が実行する。手で叩かない。）**
   WINDOW_LAYOUT → CONTEXT_15M → BARS_HTF_DUE → RANGE_ANCHOR →
   EXECUTION_3M → VP_PD_DOL → CVD_INITIAL → SMT_PEERS → EVENT_CONTEXT の順に観測する。
   **VPセッションラベルとSMTは pane 0 の study** なので pane 0 focus 中に読む
   （pane 1 には CVD Unified しか無く、pane 1 で読むと `study_count: 0` が返り、
   必須の `pine_labels.json` が空になってサイクルごと BLOCKED になる）。
   左15分の構造を先に固定し、HTF取得後は左を15分へ戻す。右下CVDはpane 1内の
   study領域として取得し、最後はpane 0 MNQ/15へfocusを戻す。形成中足を確定足にしない。
   field仕様は `docs/R13_DATA_PIPELINE_AND_S_REASSESSMENT.md` に従う。
3. 収集した**同一時点の初回スナップショットJSONだけ**を、標準入力から原子的に投入する。

```powershell
$initialSnapshotJson | python monitor_ingest.py --config monitor_config.json --kind snapshot
```

   入力receiptに `orderInvoked=false` と `networkInvoked=false` が無ければ停止する。
4. 初回の検証・ICT評価・publish前検証を実行する。

```powershell
python monitor_pipeline.py --config monitor_config.json
```

   `status=BLOCKED` なら publishも発注もせず、`phaseLog` と `acquisitionRequest` の不足だけを
   次周期で取り直す。市場の古さ・銘柄不一致・価格異常は `BLOCKED / NO TRADE` である。
5. CVDが `refreshRequired=true` の時だけ、**CVD関連フィールドだけ**を一度投入し、同じ
   pipelineをもう一度実行する。価格、足、VP、range、SMT、eventをこの再取得で書き換えない。

```powershell
$cvdRetryJson | python monitor_ingest.py --config monitor_config.json --kind cvd-retry
python monitor_pipeline.py --config monitor_config.json
```

   `cvdAttempts=2` 後は再取得しない。なお不健康なら A+ は A に上限化する。CVD再取得の失敗を
   注文再送で補ってはならない。
6. `status=READY` と `publishPreflight.ready=true` の時だけ、pipelineが出力した検証済み
   `.secrets/monitor_pipeline_bundle.json` を publish正本へ渡す。**初回入力JSONを直接渡さない。**

**この呼び出しは `nqx_cycle.stage_publish()` が行う。手で叩かない（R39）。**

   `monitor_publish.py` の後段だけが既存の `autotrade_engine.reconcile()` を呼べる。実注文は
   Mini App AUTOがLIVE、凍結済みpublish state、broker verified、既存リスク
   ゲートの全てを満たす時に限る。R13 pipelineと`monitor_ingest.py`は注文・ネットワーク・
   `reconcile`を呼ばない。

この順序の保存物は `.secrets/monitor_pipeline_cycle.json`（入力ハッシュ、phase log、不足要求、
CVD状態、dry-run handoff）と `.secrets/monitor_pipeline_bundle.json`（検証済みbundle）である。
同じ設定と同じ入力は同じ評価に再現されなければならない。

1. TradingView MCP の `chart_get_state` で、銘柄が `MNQU6`（または同一MNQ契約）、現在値、タイムゾーン、3分足・15分足の状態を確認する。違う時間足なら `chart_set_timeframe` で復元してから取得し、形成中バーを確定足として使わない。
2. 3分足を直近240本取得する（`data_get_ohlcv count=240`）。形成中足の除去は `tv_snapshot.py` が行う。同時に `data_get_study_values` で VWAP、VWAPバンド、CVD fast/slow、SwingArm/NQX値、VPのVAH/VAL/POC、ICT/SMT関連の観測値を取得する。CVDが無い、または価格が動く中で直近3標本が同値なら、同じstudy値を**一度だけ再取得**し、`cvdMeta={provider,attempts,status,history}`をbundleへ入れる。再取得失敗は注文再送ではなくA+→A上限化として記録する。VIXを読む場合は取得後にNQペインへ戻す。
3. 15分足は毎サイクルの必須入力ではないが、`00/15/30/45`の確定直後とシナリオをA以上へ昇格させる直前には必ず更新する。15分の `CT_TREND / CT_FIB618 / CT_TRAIL / CT_ATR` を記録し、OTE候補ではその方向レグ又は名前付きセッションを `rangeAnchor={rangeTf,rangeStart,rangeEnd,anchorType,freshness,high,low}` として固定する。3分足のローリングレンジからOTEを作らない。SMT peerには同時刻の足、同じ`sessionId`、freshnessを保存する。
   45分・1時間・4時間・日足は、raw欠落時または `now >= 前回rawの最終行の close + 60秒`（最終行は取得時点の形成中足。旧規則の `最終確定足open + 2×step` は毎本1本遅れ、途中値を確定足として採点していた。2026-09-08 修正）の時だけpane 0を一時的に `45 / 60 / 240 / 1D` へ切り替え、各80本を `bars45m / bars1h / bars4h / bars1d.json` に逐語保存する。HTF raw の最終行は後続の行が現れるまで確定足にしない。各取得直後にsymbol/resolutionを再確認し、最後は必ずpane 0を15分へ戻す。HTF欠落は3分足で補間せず、`INSUFFICIENT` として無得点にする。
4. 入力を機械検査する。`priceAt` はpublish時点から600秒以内、価格とOHLCは0.25tick整合、`sourceSymbol` はMNQを含む、`snapshot.bars3m` は確定足（240本取得から形成中を除いたもの）、`snapshot.levels` は空でないことを確認する。1つでも欠けたら `scenarios` を空または `WATCH` にして理由を `watching` に書き、古い値で補完しない。
5. 直近12本の確定足レンジ中央値を `noiseFloor` とし、`ratio = noiseFloor / 60pt` を計算する。`ratio > 0.60` は全モデル停止、`ratio <= 0.60` はA/A+を許可する（**2026-09-01 ユーザー決定で「0.40〜0.60 はA+のみ」を撤廃**。閾値は `NQX_VOL_GATE_APLUS` で可逆）。これは `monitor_publish.apply_volatility_grade_gate()` が強制し、人の見落としに依存しない。初期SLは構造否定価格の外側へ最低 `1.0 × noiseFloor` の緩衝を置く。構造SLが60ptまたは口座別$240を超える場合は不合格とし、SLを内側へ縮めてR:Rを作らない。
6. `VP80_REVERSION / TURTLE_SOUP_REVERSAL / BREAKER_CONTINUATION / OTE_FVG_PULLBACK` を候補として、VP・ICT・SMT・FVG・IFVG・Fib SD・DOL・PO3・CVD・HTFを `msnr_gate.evaluate()` の `model / evidence / penalties / targetR / htfContext` に反映する。IFVG providerが無ければ3本FVG→body breach→inverse retest/hold→構造維持を確定3分OHLCから導く。Fib SD providerが無ければ**解決済みの明示HTF/セッションrange**だけから `-1/-2/-2.5/-3/-4/-5/0/1` を投影する。ローリング3分高安は使わない。情報を取得しただけで根拠に残らない候補は採用しない。
7. `select_primary()` で最有力の1件だけを選び、次の一発判定を行う。`grade=A+/A/B`(2026-09-04 に B を追加)、`state=ARMED/ACTIVE`、方向、`decisionId`、Entry/構造SL/TP1/runner最終TP、固定qty=2、CVD health、rangeAnchor、リスク上限、イベント・ボラ・セッションゲートがすべて同じbundleで通れば **PASS**。CVDが非freshならA+をAへ上限化した上で判定する。1つでも欠ければ **FAIL** とし、別モデルへ逃げない。FAIL時は `watching` に「何が変わればPASSになるか」を1文で残す。
8. Entry・SL・TPは必ず価格そのものでbundleへ入れる。`reason` には構造レベル、ICT/SMT等の実際に効いた根拠、否定条件、到達距離を含める。シナリオがないサイクルも `watching` を最低1件書く。

### 6.3 bundleの保存とpublish

毎サイクル、監査用に `.secrets/monitor_cycle_HHMM.json` を **UTF-8（BOMなし）** で保存する。必須トップレベルは `at`、`price`、`priceAt`、`priceSource`、`sourceSymbol`、`snapshot`、`cvdMeta`。`snapshot` には `bars3m`（確定足）、`levels`、取得時刻、study値、`rangeAnchor`、SMTの`sessionId / peers / peerMeta`、`bars45m / bars1h / bars4h / bars1d / htfContext`を入れる。OTE/FVG/SMT/CVD/HTFの証跡を次サイクルに推測で復元しない。

**R39: この保存と publish は `nqx_cycle.py` が行う。** 監査コピーは `save_audit()` が
毎サイクル残し、publish には検証済み bundle が**バイト列のまま**渡る。PowerShell の
文字列パイプで JSON を渡す経路は廃止した（ロケール依存で文字化けして落ちていた）。

`monitor_publish.py` は **評価 → Cloudflare Durable Objectへの正本publish → `autotrade_engine.reconcile()` → Telegram通知** の順で実行する。出力に `server state: ok` が無い、または `AUTOTRADE HALT` / `UNVERIFIED` が出た場合は、そのサイクルの再送を禁止し、建玉を再照会してから次の判断をする。

### 6.4 建玉がFLATのサイクル

- A/B**両口座**が `verified=true` かつ `qty=0`、注文非blockingのときだけ新規候補を評価する。
- PASSでMini App AUTOがLIVEなら `autotrade_engine` がdry-run検査後に一度だけ送信する。AUTO OFF・期限切れ・状態未確認なら提案表示だけで、`--confirm` は付けない。
- 成行へ切り替わる場合も、発注直前の観測値を `--last` として渡し、SL/TP向き・滑り・口座別リスクゲートを通す。
- `decisionId` ごとに送信は1回だけ。応答が失敗・部分成功・タイムアウト・不明なら再送せず、台帳へ `HALT` を記録する。
- **未約定エントリーの自動取消(R52, 2026-09-05 ユーザー決定)**: 建玉 FLAT のまま未終端の
  注文が残り、凍結 ENTRY プランの送信時刻以降の確定足の極値が TP1 に届いた(SELL は安値 ≤ TP1、
  BUY は高値 ≥ TP1。送信後の足が無ければ現在値)ときは、engine が `--flatten --account` で
  その口座の未約定注文を消し、再照会で FLAT + 注文非 blocking を確認してから `ENTRY_HALTED`
  (`ENTRY_STALE_CANCEL`)を記録し、**同じサイクル**で startup recovery(不在証明 → RECOVER →
  `ENTRY_RECOVERED`)まで進めて HALT と claim を解く。取消の確認が取れなければ HALT。
  停止は `NQX_STALE_ENTRY_CANCEL=0`。回復済みプランの後に残る注文は別物として触らない。

### 6.5 建玉がOPENのサイクル

- 毎サイクル、A/Bを `query_position(..., account=...)` で別々に照会する。どちらかに建玉・blocking注文がある間は新規シナリオを送らない。
- **例外(R46): 手動で進行中の建玉がある口座だけは経路から外す。** 判定は所有権で
  自動的に行う。`ownership_binder` が「その symbol/口座に凍結プランが無い」または
  「凍結プランの `accountScope` にこの口座が入っていない」と返した建玉は、こちらの
  経路では作れない＝手動である。その口座を `accountScope` から落とし、残りの口座が
  全て verified FLAT なら通常どおり新規を評価する。外した口座は
  `executionContract.excludedAccounts` とサイクル注記に必ず残し、建玉には触れない。
  所有権を**証明できないだけ**の状態（identity不明・照会UNVERIFIED など）は手動扱い
  にせず、従来どおり経路全体を止める。`NQX_AUTOTRADE_KILL=1` の間は除外を行わない
  （KILLは台帳外の建玉も落とす明示経路であるため）。
  新規の宛先は `order.py --accounts` で凍結スコープへ**狭める**。この引数は許可リスト
  の部分集合しか受け付けず、`--flatten` / `--modify` には掛からない（撤退経路を塞がない）。
- **台帳の注文は nightwatch の所有物（R79, 2026-09-12 ユーザー決定）**: 凍結 route
  snapshot の `orderId` / `receipt` で身元が取れた脚は、成行の平均建値が凍結参照価格から
  乖離していても所有する。乖離ゲート（R52/R76）が守っていたのは「他人の建玉かもしれない」
  ことだけで、そのリスクは identity 側で既に排除されている。緊急停止は
  `execution_contract.json` の `ownershipAttribution.manualHalt=true` で、ON の間は
  従来の厳格な乖離ゲートへ戻る。**身元**（orderId/receipt/口座/銘柄/方向/枚数/ブラケット
  構造）の検査はこのスイッチでは一切緩まない。2026-09-12 01:30 の SHORT 4（不利側
  6.25pt）が、SL 建値移動もトレールも掛からないままブローカー OCO だけで 80 分放置された
  のが直接の理由。
- 凍結済みR12分割計画があれば、口座ごとにposition generationと4脚中の該当2脚を束縛する。2枚が残る口座は各OCOを変更しない。TP1約定後にその口座のqty=1を確認した場合だけ `--modify --account` でrunnerを建値以上（SELLは建値以下）へ寄せ、高値/安値とnoiseFloorから計算したトレールを利益方向にだけ更新する。
- 最終TP到達、構造SL突破、`forceFlatten`、`NQX_AUTOTRADE_KILL=1` のいずれかで建玉が残っていれば、対象口座へ `--flatten --account` を一度だけ送信する。KILLは両口座を独立に再確認する。
- 変更・決済の送信後は必ずブローカーを再照会する。確認できなければ成功扱いにせず `HALT` とし、無条件リトライしない。台帳にない手動建玉は推測で管理しない。
- **裸の runner を作らない(R78, 2026-09-11)**: `cancelandbracket` は取消→新規の 2 段で、新 stop が
  価格に跨がれているとブローカーが拒否し、取消だけ成立して runner が保護注文ゼロになる。
  (a) engine と `order.py --modify` は stop を現在値から `NQX_STOP_MARKET_BUFFER_PT`(既定 4pt)以上
  離れた側にしか置かない。engine は送信直前の quote で判断し、`order.py` は何も取り消す前に
  再検査して `MODIFY_STOP_NOT_PROTECTIVE` で止まる(engine はこれを HALT ではなく見送りにする)。
  (b) それでも片脚拒否で逆方向 active 行が 2 本未満なら、**同じ周期で**直前の stop(最後に受理
  された SL か初期構造 SL)へ戻す MODIFY を送る。直前の stop すら守れない側なら FLATTEN。
  成功時、元の失敗は `MODIFY_FAILED_REPAIRED`(新規を塞がない)。(c) `order.py` の ULTRA
  ガードは逆方向行が 2 本**超**のときだけ拒否する(0〜1 本は修復として通す)。(d) 所有権を
  束縛した周期は「次周期」ではなく同じ周期で管理まで進む(`managing now`)。手動で入れた SL は
  engine の張り替えで取消される前提で扱う。

### 6.6 サイクル報告と停止条件

毎サイクルの標準出力・Telegram先頭行は1〜2行に圧縮し、少なくとも次を含める。

```text
[HH:MM] MNQU6 29,630.00 | primary=VP80_REVERSION SELL A+ ACTIVE | E=29,630 SL=29,605 TP=29,680 | broker=FLAT | action=ENTRY
```

PASSできない場合は `primary=NONE` とし、`watching=「条件→成立したら方向」` を示す。`price stale`、`market rejected`、`state unavailable`、`broker UNVERIFIED`、台帳エラー、送信結果不明のいずれかは即時HALT。次サイクルで自動的に再送せず、原因を解消してから手動で再開する。

**R77(2026-09-11)**: この行の state と E/SL/TP は **publish 後の正本**(`.secrets/monitor_last_sent.json` の `_published_scenario`)から `nqx_cycle.report_line()` が引く。pipeline の decision は publish 前の提案で、ボラ・封鎖・ULTRA・契約のゲートで WATCH に落ちても書き換わらない(2026-09-10 23:41 に publish が「demoted to WATCH」を出したのに行が `A ARMED` を印字した)。decision に戻るのは publish が走らなかったサイクルだけで、publish は通ったのに正本が読めなければ行末に `post-gate state unread` が付く。落とした理由は `demoted: …` として末尾に添える。監査コピー `monitor_cycle_HHMM.json` は publish 前の写しなので、ゲート後の状態は正本を見る。

**サイクル・ビーコン(R47)**: `nqx_cycle.py` は各終了地点で `cycle_health`
ストリームへ軽量ビーコン(PUBLISHED/BLOCKED/HALT・理由・KILL状態)を送る。
BLOCKED/HALT は market/scenario を publish しないため、Mini App の WATCHTOWER は
これで「沈黙」と「理由があって止まっている」を区別する。ビーコンは表示専用で、
送信失敗はサイクルの終了コードを変えない(`send_beacon` が握る)。残機 publish には
口座名簿の突合(`accounts.sync`: missing/unknown/dead)も同乗する — 設定口座の
消失(過去2回の事故)はアプリの LIFELINE 直下に警告として出る。

セッション終了時は、稼働中のloopを停止し、2口座をそれぞれ `broker_status.py --account ... --json` で確認する。`NQX_AUTOTRADE_SESSION_FLATTEN=1` を明示した場合だけ、設定済みET時刻に口座別で自動全決済する。

## 7. 禁止事項

- `order.py` の安全ゲートを迂回した直接 CrossTrade HTTP。
- 建玉未確認での新規、SLなし、固定2枚を増減して損失を取り返す操作。
- 契約内の等級(A+ / A / B)を「慎重に様子見」し続けること、または成立した候補を情報だけで捨てること。
- 部分成功・タイムアウト後の無条件リトライ。
- `.secrets` のキー・口座情報の通知本文、コミット、画面出力への露出。

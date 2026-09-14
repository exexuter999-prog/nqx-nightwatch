# AGENTS.md — R43 structural-edge autonomous execution contract

## 0. セッション開始

1. `TRADING_CONTEXT.md` を読む。
2. `python broker_status.py` で建玉の正本を確認する。
3. `python order.py --status` は送信記録の補助確認にだけ使う。
4. 監視入力の `at`、価格、確定足、シンボルを確認する。

このファイルは運用契約、`TRADING_CONTEXT.md` は口座・金額・銘柄の正本、
`order.py` は送信直前の機械ゲートである。古い履歴や手順を推測で復活させない。

## 1. データ取得

- 形成中バーを確定足として扱わない。`at` と価格が古い、欠損、シンボル不一致なら
  新規シナリオを作らない。
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

- `A+ / A` かつ `state=ARMED|ACTIVE` のみ新規候補。CVDがfreshでない場合は、他条件でA+相当でもAへ上限化する。
- `B / FLAT / WATCH` は発注せず、理由だけをログする。
- 同時に複数モデルを武装しない。勝ち筋を逃すための無期限待機を作らない。
- SL は構造否定価格＋必要なバー緩衝で先に決め、MNQ 2枚固定・SL 60pt 以下・
  口座別リスク上限内でなければその一件を不合格とする。

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

AUTOがLIVEの場合は `autotrade_engine.py` だけが `order.py` を呼び、次の順序を守る。

1. Cloudflare 正本が更新済みで、イベント・ボラのゲート後もシナリオが
   `A/A+ ARMED|ACTIVE` であることを確認する。
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
6. セッション時刻での自動全決済は `NQX_AUTOTRADE_SESSION_FLATTEN=1` を明示した場合だけ。

日次ガード(日次損失 −$480 / DAYGOAL / 2連敗 / 損切り後15分冷却)は 2026-08-27 に
**廃止**した。これらの条件で新規を止めることはもう無い。`dayguard.py` は集計と
台帳追記だけを担い、`blocked` は常に False。数字は発注前の表示と戦績に残る。
建玉照会が `UNVERIFIED` のときは新規・変更を止める。台帳にない手動建玉を勝手に
推測管理しない（緊急時は `NQX_AUTOTRADE_KILL=1` の全決済のみ許可）。

**決済の記録(戦績)は `trade_journal.py` が毎サイクル自動で行う。** 3分ループが
`autotrade_engine.reconcile()` の直後にこれを呼び、口座ごとの建玉が閉じたことを
観測したら `result` を publish して Mini App の LEDGER へ載せる。決済価格は
**凍結プランの脚**（TP1 / runner最終TP / 構造SL / トレール後SL）だけを使い、
どの脚が落ちたかは建玉枚数の遷移と確定足が触れた水準から決める。どの水準にも
触れていない減少は**推測せず保留**し、人が `/result` で入れるまで publish しない。
金額はブローカーの `realizedPnL` 差分で突合し、差は `fees`(滑り+手数料)に落ちる。
2026-08-27 以前はこの起動点が `telegram_bot.py` 常駐プロセスの中にしか無く、
3分ループから一度も呼ばれていなかったため、実トレードが1件も戦績に載っていなかった。

## 5. 重複防止と失敗処理

`.secrets/autotrade_ledger.jsonl` が自律経路の監査台帳である。

- entry は `decisionId` ごとに一度だけ送る。
- modify は `decisionId + action + SL + qty` ごとに一度だけ送る。
- flatten と失敗応答は一度記録し、同じ理由で自動再送しない。
- 台帳が壊れている、書けない、未知の状態がある場合は `HALT`。
- `order.py` のログは送信記録であり、建玉の正本ではない。必ず broker 照会を優先する。

## 6. 監視ループ（具体手順）

### 6.1 時間窓と起動前チェック

- 監視窓は **JST 07:00〜翌04:00、3分間隔**（2026-09-01 に 12:30 から前倒し）。窓の外ではデータ取得・評価・発注を行わず、停止中であることだけを1行で記録する。
- Codex と Codex、または監視ループを二重起動しない。1セッション・1ループだけを稼働させる。
- セッション開始時に、次を上から順に1回だけ実行する。Codex側の監視開始指示は次の1回だけ送る。

**ループ指示文の正本は `docs/MONITOR_LOOP_PROMPT.md`。** 本文をそのまま `/loop 3m` に貼る。
この節と食い違ったら指示文の側を直す（2つの手順書を並立させない）。

```powershell
cd "C:\Users\exexu\Downloads\nq-nightwatch-Codex-handoff"
python telegram_bot.py --check
python nqx_state.py --check
python broker_status.py --accounts
python broker_status.py --account LTATANOBA1001064330885 --json
python broker_status.py --account LTATANOBA1005923156221 --json
python order.py --status
python events.py --refresh
python autotrade_arm.py --status
```

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
> エージェントの担当は **2 の取得だけ**。MCP の出力を要約・整形・書き写しせず、
> `.secrets/tv_raw/` へ**逐語で**保存する（ファイル名は `docs/TV_ACQUISITION_LOOP.md`）。
> 整形は `tv_snapshot.py` が決定論的に行う —— この転記工程が、実サイクル403本で
> `rangeAnchor` / `peers` / `po3` / `cvdMeta` の取得率を **0%** にしていた張本人である。

1. `monitor_config.json` を読み、`schemaVersion=NQX_MONITOR_PIPELINE/1`、`type=file`、
   `execution.mode=DRY_RUN_ONLY`、入力・CVD再取得・出力パスを確認する。設定外パス、LIVE設定、
   又は未知providerは停止する。
2. TradingView MCPから、WINDOW_LAYOUT → CONTEXT_15M → BARS_HTF_DUE → RANGE_ANCHOR →
   EXECUTION_3M → VP_PD_DOL → CVD_INITIAL → SMT_PEERS → EVENT_CONTEXT の順に観測する。
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
   45分・1時間・4時間・日足は、raw欠落時または `now >= 前回の最終確定足open + 2×step` の時だけpane 0を一時的に `45 / 60 / 240 / 1D` へ切り替え、各80本を `bars45m / bars1h / bars4h / bars1d.json` に逐語保存する。各取得直後にsymbol/resolutionを再確認し、最後は必ずpane 0を15分へ戻す。HTF欠落は3分足で補間せず、`INSUFFICIENT` として無得点にする。
4. 入力を機械検査する。`priceAt` はpublish時点から600秒以内、価格とOHLCは0.25tick整合、`sourceSymbol` はMNQを含む、`snapshot.bars3m` は確定足（240本取得から形成中を除いたもの）、`snapshot.levels` は空でないことを確認する。1つでも欠けたら `scenarios` を空または `WATCH` にして理由を `watching` に書き、古い値で補完しない。
5. 直近12本の確定足レンジ中央値を `noiseFloor` とし、`ratio = noiseFloor / 60pt` を計算する。`ratio > 0.60` は全モデル停止、`ratio <= 0.60` はA/A+を許可する（**2026-09-01 ユーザー決定で「0.40〜0.60 はA+のみ」を撤廃**。閾値は `NQX_VOL_GATE_APLUS` で可逆）。これは `monitor_publish.apply_volatility_grade_gate()` が強制し、人の見落としに依存しない。初期SLは構造否定価格の外側へ最低 `1.0 × noiseFloor` の緩衝を置く。構造SLが60ptまたは口座別$240を超える場合は不合格とし、SLを内側へ縮めてR:Rを作らない。
6. `VP80_REVERSION / TURTLE_SOUP_REVERSAL / BREAKER_CONTINUATION / OTE_FVG_PULLBACK` を候補として、VP・ICT・SMT・FVG・IFVG・Fib SD・DOL・PO3・CVD・HTFを `msnr_gate.evaluate()` の `model / evidence / penalties / targetR / htfContext` に反映する。IFVG providerが無ければ3本FVG→body breach→inverse retest/hold→構造維持を確定3分OHLCから導く。Fib SD providerが無ければ**解決済みの明示HTF/セッションrange**だけから `-1/-2/-2.5/-3/-4/-5/0/1` を投影する。ローリング3分高安は使わない。情報を取得しただけで根拠に残らない候補は採用しない。
7. `select_primary()` で最有力の1件だけを選び、次の一発判定を行う。`grade=A/A+`、`state=ARMED/ACTIVE`、方向、`decisionId`、Entry/構造SL/TP1/runner最終TP、固定qty=2、CVD health、rangeAnchor、リスク上限、イベント・ボラ・セッションゲートがすべて同じbundleで通れば **PASS**。CVDが非freshならA+をAへ上限化した上で判定する。1つでも欠ければ **FAIL** とし、別モデルへ逃げない。FAIL時は `watching` に「何が変わればPASSになるか」を1文で残す。
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

### 6.5 建玉がOPENのサイクル

- 毎サイクル、A/Bを `query_position(..., account=...)` で別々に照会する。どちらかに建玉・blocking注文がある間は新規シナリオを送らない。
- 凍結済みR12分割計画があれば、口座ごとにposition generationと4脚中の該当2脚を束縛する。2枚が残る口座は各OCOを変更しない。TP1約定後にその口座のqty=1を確認した場合だけ `--modify --account` でrunnerを建値以上（SELLは建値以下）へ寄せ、高値/安値とnoiseFloorから計算したトレールを利益方向にだけ更新する。
- 最終TP到達、構造SL突破、`forceFlatten`、`NQX_AUTOTRADE_KILL=1` のいずれかで建玉が残っていれば、対象口座へ `--flatten --account` を一度だけ送信する。KILLは両口座を独立に再確認する。
- 変更・決済の送信後は必ずブローカーを再照会する。確認できなければ成功扱いにせず `HALT` とし、無条件リトライしない。台帳にない手動建玉は推測で管理しない。

### 6.6 サイクル報告と停止条件

毎サイクルの標準出力・Telegram先頭行は1〜2行に圧縮し、少なくとも次を含める。

```text
[HH:MM] MNQU6 29,630.00 | primary=VP80_REVERSION SELL A+ ACTIVE | E=29,630 SL=29,605 TP=29,680 | broker=FLAT | action=ENTRY
```

PASSできない場合は `primary=NONE` とし、`watching=「条件→成立したら方向」` を示す。`price stale`、`market rejected`、`state unavailable`、`broker UNVERIFIED`、台帳エラー、送信結果不明のいずれかは即時HALT。次サイクルで自動的に再送せず、原因を解消してから手動で再開する。

セッション終了時は、稼働中のloopを停止し、2口座をそれぞれ `broker_status.py --account ... --json` で確認する。`NQX_AUTOTRADE_SESSION_FLATTEN=1` を明示した場合だけ、設定済みET時刻に口座別で自動全決済する。

## 7. 禁止事項

- `order.py` の安全ゲートを迂回した直接 CrossTrade HTTP。
- 建玉未確認での新規、SLなし、固定2枚を増減して損失を取り返す操作。
- B以下を「慎重に様子見」し続けること、またはA以上を情報だけで捨てること。
- 部分成功・タイムアウト後の無条件リトライ。
- `.secrets` のキー・口座情報の通知本文、コミット、画面出力への露出。

# NQX SwingArm Pressure V2 — 実装設計書 (BLUEPRINT)

- 成果物予定ファイル: `project/indicator/nqx_swingarm_pressure_v2.pine`（新規・別ファイル）
- 現行 `nqx_atr_pressure_map_v1.pine`（v1.5）は比較用として無変更で残す
- 対象読者: 追加質問なしで実装する実装担当（Codex）
- 本書は設計のみ。完成Pineコードを含まない。疑似コードは幾何・状態遷移の厳密化のためだけに用いる

## 0. 拘束条件（全章に優先）

1. clean-room実装。保護された元ソース（SwingArm High Pressure V6.8）を推測で複製しない。観察可能な公開情報（ユーザー提供のBlackflag FTS参考コード、公開ユーザーガイド、設定画面、チャート画像）から挙動を定義し、観察不能な内部式は「不明」と明記のうえ監査可能な代替式を定義する。
2. No Synthetic Future。未来ローソク・未来経路・予測ターゲット線を描かない。現在確定値の右方向への**有界な**水平延長（レベルレール）は予測ではなく表示であり、上限バー数を明記して許可する。
3. confirmed HTFのみ。`request.security` は全tuple要素を `[1]` オフセット + `barmerge.lookahead_on` で取得する。未確定HTFバーからゾーン・状態・ラベルを生成しない。
4. State-Driven Rendering。描画はすべて状態機械（D章）の出力であり、描画側で状態を推測しない。
5. Risk Is Geometry。invalidation・hard stopは幾何（E章）から導出し、表示専用値を作らない。
6. naから価格を捏造しない。na要素はJSONで `null`、表示で非表示またはN/A。推定価格には `~` を付ける（Nightwatch側規約）。
7. scoreはprobabilityではない。元実装の "Probability" 系語彙（Long/Short Probability、Bounce Probability、PROBABILITY STATUS）はV2ではすべて **Score / Grade** に置換する。`SCORE IS NOT PROBABILITY` をテーブル・ラベル凡例・JSONに必ず伴わせる。実データ校正なしに%確率・勝率・bounce probabilityを表示しない。満点表示は「Score 100/100 · EXCELLENT」。
8. NQX/1 schema、version gate、validator、R:R、invalidation、event、排他、安全監査を維持。無効出力で既存分析を上書きしない。

---

## A. Evidence Audit

### A-1. 読了済み資料

| 資料 | 内容 | 採用した事実 |
|---|---|---|
| Blackflag FTS 参考コード（pasted-text.txt, Pine v4, 公開） | modified TR（HiLo/HRef/LRef）、Wilder MA、factor×ATRトレイル、ratchet、extremum、f1/f2/f3、fib間fill、fibクロスalert | E章の幾何の一次根拠。`fill(Fib1,Fib2)=不透明, (Fib2,Fib3)=70, (Fib3,L100)=60` の3段fill構造 |
| 目標チャート（134928.png, 15M MNQ1!, SwingArm High Pressure V6.8） | 3エンジン同時稼働の階段帯、4色分類、fib別スコアラベル、右端レベルレール | A-2表の全行 |
| 設定画面（134221/134247/134310.png） | Fresh Zone Age=50、Proximity ATR 28/2、Max Zones=20、Trading Type=CT/15m/1H、エンジン別表示トグル、Session 05:30–02:30、RSI 14/70/30（OB=白/OS=紫に変更済）、Trendline 5/5、Break Statistics=Top Right | H章の入力設計 |
| 誤った現行結果（codex-clipboard-….png, 45分足） | v1.5は薄い小矩形が散発するだけ。SwingArm原本の帯構造と情報量が皆無 | A-2「差異」列 |
| ユーザーガイド/リリースノート（010623〜010716.png, V6.7.3〜V6.8 Enhanced） | CT=entry/HTF1=confirmation+targets/HTF2=extended targets、preset 2種、BLUE/YELLOW=Optimal Entry・GREEN/RED=Fresh zone、fib意味付け、INTERNAL/EXTERNAL TARGETラベル、グレード閾値（🔥85+/⚡70+/✓55+/⚠40+/❌<40）、fibレベル別レーティング、5-EMA構造強度、volume tracking per fib、アラート一覧、1m/15m/1H/2H/4H最適化 | B/C/F/G/I章 |
| 現行v1.5（nqx_atr_pressure_map_v1.pine）+ SPEC + 実装レポート | 単一凍結ゾーン方式、prior-bar snapshot、VISITS/CONSUMED/REARM、stale機構 | K章の分類根拠 |

### A-2. 監査表

| # | 観察対象 | 元画像で観察できる挙動 | 現行v1.5の挙動 | 差異 | 根本原因 | V2での対応 | 観察だけでは確定できない事項 |
|---|---|---|---|---|---|---|---|
| 1 | 階段状に変化する帯 | 各TFのfib1→trail(100%)帯がトレイルのratchetに追随して階段状に更新され、履歴として塗り残る | 凍結矩形のみ。動的帯は「moving cloudの欠陥」としてv1.1/v1.2で排除済み | 帯の本体が存在しない | v1系は「固定ゾーンが正」という誤った要件解釈で動的帯を欠陥扱いした | DynamicArm（動的帯）を第一級オブジェクトとして復活。plot+fillで描き、履歴は塗り跡として自然に残す。凍結スナップショットは分析用FrozenZoneとして分離 | なし（参考コードのfill構造と一致） |
| 2 | 広いRegular zone | 帯はfib61.8%線からtrail(100%)まで。fib1–f2 / f2–f3 / f3–trailの3段で濃度が変わる | ゾーンは61.8–88.6間のみの薄い矩形 | 帯の縦幅が約半分以下、3段構造なし | v1がfib1–fib3のみをゾーン定義とした | Regular zone = [f1, trail]。3段サブバンド（f1–f2 / f2–f3 / f3–trail）を参考コードのfill比率で再現 | 各段の正確なtransparency値（G章で暫定値を定義し校正） |
| 3 | 狭いOptimal Entry zone | 高圧時に帯全体が青/黄へ変色し、deep側にOptimal Entryボックスが出る（設定に "Most resent Optimal Entry #n"） | Optimal Entry概念なし。high-pressureは色分けのみ | エントリー用サブゾーンが存在しない | v1はゾーン=1種類 | OptimalEntryZone = 高圧分類時の [f2, trail]（deep側）をbox化。帯の変色と分離管理 | OEボックスの正確な縦幅（f2–f3 か f2–trail か）。→ 仮定: [f2, trail]。校正項目#1 |
| 4 | 3時間足の独立性 | 15m/2H/4Hの帯・fib・ラベル・スコアが同時かつ独立表示（ラベルに `15m…28/5` `2H…28/5` `4H…28/6`） | 3TFを計算するが、描画はCT 1ゾーン+HTF細線2組。HTF帯なし | 3系統の同格エンジンではない | v1はCT中心+HTF参照の設計 | C章: 完全同格の3エンジン。状態・配列・描画・スコアをエンジン単位で分離 | 4Hのfactor 6がプリセット既定か使用者設定かは不明。→ エンジン別factor入力（既定 5/5/6）。校正項目#2 |
| 5 | 過去ゾーンの保持 | 反転後も旧帯の塗り跡・旧ラベルが残存。設定に Max Zones to Track=20 | 反転で即消滅（活性ゾーンは常に最大1） | 履歴ゼロ | v1は「1TF=1ゾーン」設計 | 塗り跡はplot履歴で自動保持。分析用FrozenZoneをエンジン毎に最大20件リング配列で追跡（proximity/target/統計に使用） | 「20」が描画上限か分析上限かは不明。→ 仮定: 分析上限（描画はplot履歴+ラベル予算で別管理）。校正項目#3 |
| 6 | Fibラベル | 61.8/78.6/88.6タッチ時に `TF fib% score GRADE` ラベルが履歴に残る。右端に現在レベルのレール（`15m 61.8% 28/5 🔥100% EXCELLENT` 等、TFごとに61.8/78.6/88.6/Swingarmの4枚） | fibはdata windowと白線のみ。ラベルなし | レベル別の意味づけ・スコア提示が皆無 | v1はゾーン一括の状態しか持たない | fibレベルを個別エンティティ化。タッチラベル（履歴）+右端レール（現在値）を実装。表記は `2H 78.6% 28/5 · 87/100 STRONG` 形式（%確率表記は禁止） | ラベル発行の正確な頻度（毎タッチか、Label Spacingで間引きか）。→ 仮定: 確定バーのクロス毎+同一レベルはN bar間引き。校正項目#4 |
| 7 | Fresh/High-pressure分類 | 4色: 緑=通常Bull、赤=通常Bear、青=高圧Bull Optimal、黄=高圧Bear Optimal。Fresh Zone Age=50 barsでfresh判定。高圧は「最大コンフルエンス+構造整合」 | 2色+ハイプレッシャー色はラベルのみ。freshは表示文言のみ | 帯自体の4色分類がない | v1.3で「fillの動的変色=縦縞バグ」として色固定した | 帯のfill色を分類で切替（現在バーのみ変色、塗り跡は当時の色のまま=履歴として正しい）。縦縞問題はhysteresis（F章: 分類変更は確定バー+2bar持続で確定）で防止 | 高圧判定の内部式は不明（"proprietary"）。→ F章のScore≥閾値70で代替。校正項目#5 |
| 8 | External Target | HTF帯が反対側の利確目標として「EXTERNAL TARGET - Take Profit」ラベル付きで機能。ガイド: 15Mでentry、2H/4H帯をexitに使う | ターゲット概念なし | 利確幾何が存在しない | v1は単一TFゾーンの侵入/棄却のみ | ExternalTarget: 進行方向にある最寄りのHTF1/HTF2帯境界（f1）をターゲット化。box+ラベル。無ければ出さない（捏造禁止） | ターゲット選択の優先規則（最寄り/最深/両方）。→ 仮定: HTF1とHTF2それぞれ最寄り1件、計最大2件。校正項目#6 |
| 9 | ラベルが生成されるタイミング | タッチラベルは価格がレベルへ到達した履歴位置に残る。右端レールは常時現在値。スコアはラベル生成時点の値で固定表示 | ラベルは現在バーの1枚のみ | 履歴の意思決定痕跡が残らない | v1はlive label 1枚設計 | タッチラベル=確定バーで生成し以後不変（スコア凍結）。レール=barstate.islastで毎回再描画 | タッチラベルのスコアが後から更新されるか不明。→ 仮定: 生成時凍結（repaint回避のため設計として固定） |
| 10 | 右端へ延長されるゾーン | 最終バーの右側の未来x領域に、各TFの現在レベル群が階段状ブロックとして表示される | 延長なし | 右側の視認レールがない | v1に該当機能なし | 有界レベルレール: 現在の各レベルを最終バーから+2〜+12barのlabel/lineで表示（上限+15bar、予測ではない旨を仕様に明記）。元画像の「未来領域の階段ブロック」の生成機構自体は不明のため模倣せず、機能等価（現在レベルの右端提示）で置換 | 未来x領域の階段状ブロックの描画実体（box列か、HTF plotのオフセットか）は確定不能。校正項目#7 |
| 11 | 反転時のゾーン切替 | trend flipでそのTFの帯だけが反対色の新帯に切替。他TFは不変。旧帯は塗り跡+ラベルとして残る | flipで全消去→BUILDING→pullback待ち→ARMED V0 | flip直後に帯が無い空白期間が生じる | v1.4のprior-bar snapshot設計（pullback開始まで武装しない） | flip確定バーから新DynamicArmを即時稼働（参考コード方式）。ARMED V0待機は廃止（D章）。旧armはFrozenZone化 | なし |
| 12 | SwingArmラベルの28/5表記 | 全レベルラベルに `ATR長/factor` が付記される（15m,2H=28/5、4H=28/6） | なし | パラメータの自己記述がない | — | ラベル書式に `len/factor` を含める（H章の入力値を反映） | 上記#4行参照（4H factor） |
| 13 | RSIキャンドル色 | RSI 14/70/30。OB/OS到達バーのローソクを着色（この環境ではOB=白/OS=紫に設定） | なし | — | — | barcolorで実装。既定OFF→ONは入力（H章）。既定色はガイド準拠のOB=青/OS=赤とし、白/紫はユーザー設定例として記載 | なし |
| 14 | テーブル | Probability Status／Swingarm Status（Bottom）、Break Statistics（Top Right） | Truth Table 1枚 | 3テーブル構成でない | — | G/H章: Score Status・SwingArm Status・Break Statisticsの3テーブル（それぞれ表示位置・サイズ入力付き） | Institutional Activityドロップダウンの意味は不明。→ 相対出来高スパイク感度プリセット（Most Active/Active/Quiet）として代替。校正項目#8 |

### A-3. 観察だけでは確定できない事項（集約）と扱い

| # | 事項 | V2での明示的仮定 | 校正方法（L章フェーズ10で実施） |
|---|---|---|---|
| 1 | Optimal Entryボックスの縦幅 | [f2, trail]（deep側） | 同時刻スクリーンショットA/B比較で [f2,f3] と切替検証 |
| 2 | 4Hエンジンのfactor既定 | 6.0（画像の28/6） | 元設定画面の追加確認 or 帯下端価格の逆算一致で確定 |
| 3 | Max Zones=20の適用範囲 | 分析オブジェクト上限（エンジン毎） | ラベル/ボックス残存数を目視比較 |
| 4 | タッチラベルの発行間引き | 確定クロス毎、同一レベル再発行はLabel Spacing×5 bar抑制 | ラベル密度を目標画像と比較して間引き幅を調整 |
| 5 | 高圧（4色）分類の内部式 | F章Score≥70で高圧。hysteresis 2bar | 同一相場の青/黄切替タイミングを比較 |
| 6 | External Targetの選択規則 | HTF1/HTF2それぞれ進行方向最寄り1件 | ターゲットラベル位置の比較 |
| 7 | 右端未来領域の階段ブロックの実体 | 模倣しない。有界レールで機能等価 | ユーザー承認（見た目の合意）で確定 |
| 8 | Institutional Activityの意味 | 相対出来高スパイク感度プリセット | 元表示の変化点と出来高の突合 |
| 9 | 構造強度（5-EMA stack）の重み | EMA 8/21/55/100/200の整列度を等重み加点 | Structure %表示の傾向一致で調整 |
| 10 | Fresh Age 50 barの基準TF | 各エンジンの自TF確定バー数 | fresh→通常色の切替時刻比較 |
| 11 | Break Statisticsの集計定義 | セッション内のf1/f2/f3/trail確定クロス回数（観測数のみ。勝率・成功率は算出しない） | 表数値の突合 |

---

## B. V2 Domain Model

型表記はPine v6。na条件・更新主体・寿命を明記する。「更新主体」は必ず1箇所（単一責務）。

### B-1. FrameRole（enum相当のconst文字列）
| フィールド | 型 | 説明 |
|---|---|---|
| role | string | "CT" / "HTF1" / "HTF2" |
| tf | string | 解決済みTF。CT=chart、HTF1/HTF2はプリセット/手動から解決 |
| isDuplicateOfCT | bool | `timeframe.in_seconds(tf)==chartSeconds`。true時はスコア重複加点禁止・描画は片方のみ |

na条件: なし（barstate.isfirstで確定）。更新主体: 入力解決部。寿命: スクリプト全期間。

### B-2. SwingArmEngineState（エンジン毎に1つ、simple変数群 or 単一UDT）
| フィールド | 型 | na条件 | 更新主体 | 寿命 |
|---|---|---|---|---|
| trend | int (+1/−1) | 初期化前のみna→1に正規化 | エンジンコア（毎確定バー） | 常駐 |
| trail | float | ATR未成立期間 | エンジンコア | 常駐 |
| extremum | float | 同上 | エンジンコア | 常駐 |
| atr | float | 冒頭length本未満 | ATRカーネル | 常駐 |
| f1, f2, f3 | float | trail/exがna | エンジンコア（導出） | 常駐 |
| flipBarTime | int | flip未経験 | エンジンコア（flip時のみ） | 次flipまで |
| armAgeBars | int | — (0開始) | エンジンコア | 次flipまで |
| classification | string | — | 分類器（F章、確定バーのみ） | 常駐 |
| classifyHoldCount | int | — | 分類器（hysteresis用） | 常駐 |

### B-3. DynamicArm（描画用ビュー。EngineStateから毎バー導出、保存しない）
| フィールド | 型 | 説明 |
|---|---|---|
| dir | int | trend |
| bandTop/bandBottom | float | bull: top=f1, bottom=trail ／ bear: top=trail, bottom=f1 |
| sub1Top..sub3Bottom | float | 3段サブバンド境界（f1/f2/f3/trail） |
| color4 | color | 4色分類の現在色 |
| isFresh | bool | armAgeBars ≤ freshAge |

na条件: EngineStateがnaなら全na（描画されない）。更新主体: 描画層のみ。寿命: 1バー（毎バー再導出）。

### B-4. FrozenZone（エンジン毎 array、最大 maxZones=20）
| フィールド | 型 | na条件 | 更新主体 | 寿命 |
|---|---|---|---|---|
| id | int | — | flip処理（採番） | expiry/FIFO削除まで |
| dir | int | — | flip処理 | 同上 |
| f1,f2,f3,trailAtFreeze | float | 生成時に全て有限であることを検証（1つでもnaなら生成しない） | flip処理（以後**不変**） | 同上 |
| extremumAtFreeze | float | 同上 | flip処理 | 同上 |
| birthTime/freezeTime | int | — | flip処理 | 同上 |
| visitCount | int | — (0開始) | Visit判定（確定バー） | 同上 |
| deepestLevel | int (0..3) | — | Visit判定 | 同上 |
| state | string | — | 状態機械（D章） | 同上 |
| scoreAtFreeze | int | — | 分類器 | 同上 |

### B-5. OptimalEntryZone（エンジン毎に最大1つの「最新」+履歴はFrozenZoneに内包）
| フィールド | 型 | na条件 | 更新主体 | 寿命 |
|---|---|---|---|---|
| active | bool | — | 分類器（classification=高圧の間true） | 分類解除/flipまで |
| top/bottom | float | activeでない時na | 分類器（[f2, trail]を確定バーで更新） | 同上 |
| boxId | box | 非表示時na | 描画層（barstate.islastで再描画） | 同上 |
| sinceBarTime | int | — | 分類器 | 同上 |

### B-6. ExternalTarget（最大2件: HTF1由来/HTF2由来）
| フィールド | 型 | na条件 | 更新主体 | 寿命 |
|---|---|---|---|---|
| srcRole | string | — | ターゲット解決器（確定バー） | ターゲット到達/構成変化まで |
| top/bottom | float | 進行方向に該当帯が無い場合na（**無ければ描かない**） | ターゲット解決器 | 同上 |
| reached | bool | — | ターゲット解決器（highs/lowsの確定タッチ） | 同上 |

### B-7. ZoneVisit（イベント。FrozenZone/DynamicArmのカウンタ更新に使う一時値）
| フィールド | 型 | 説明 |
|---|---|---|
| zoneId / engine | int/string | 対象 |
| level | int (1..3) | 61.8/78.6/88.6のどれへ到達したか |
| barTime | int | 確定バー時刻 |
| relVolume | float | 到達バーの相対出来高（volume tracking per fib） |
| rejected | bool | 参考コード同型の棄却パターン成立 |

na条件: 生成条件不成立なら生成しない。更新主体: Visit判定のみ。寿命: 即時消費（カウンタへ反映後破棄）。

### B-8. PressureEvidence（スコア入力のスナップショット。毎確定バー再計算）
| フィールド | 型 | na時の扱い |
|---|---|---|
| alignedFrames | int (0..3) | 重複TFは除外して集計 |
| structureStrength | int (0..100) | EMA未成立→0 |
| fibDepth | int (0..3) | 帯外=0 |
| freshness | int (0..2) | fresh=2/active=1/consumed·stale相当=0 |
| proximityHot | bool | proxATR na→false |
| relVolume | float | volume na（出来高無しシンボル）→0扱い |
| rsiState | int (−1/0/+1) | RSI na→0 |
| targetAvailable | bool | ExternalTarget無し→false |
| visitCount | int | — |

### B-9. RenderLayer（描画層定義。G章のz-orderと1:1）
`L1_CONTEXT_BAND … L11_TABLES` の11定数。各層は「描画プリミティブ種別・最大オブジェクト数・再描画契機」を持つ（G章の表が正）。

### B-10. AlertEvent
| フィールド | 型 | 説明 |
|---|---|---|
| kind | string | I章の9種 |
| engine/zoneId/level | — | 対象 |
| confirmed | bool | 常にtrue（確定バー以外で発火しない） |
| dedupeKey | string | `kind+engine+zoneId+level`。同一キーは状態が解除されるまで再発火禁止 |
| jsonPayload | string | I章 |

---

## C. Three Independent Engines

共通実装は関数 `f_swingArmEngine(...)` 1本とし、**状態はすべて引数と戻り値（tuple）で完結**させる。エンジン間で共有するvar/arrayを作らない。CTはチャートコンテキストで直接実行、HTF1/HTF2は `request.security(syminfo.tickerid, tf, f_confirmedEngine(...), gaps_off, lookahead_on)` で取得し、`f_confirmedEngine` は全tuple要素を `[1]` して返す（v1.5と同一の確定HTFパターンを踏襲）。

| 項目 | CT engine | HTF1 engine | HTF2 engine |
|---|---|---|---|
| 入力時間足 | チャートTF | preset解決（15m既定プリセットで "120"） | 同（"240"） |
| ATR kernel | modified TR + Wilder（E-1）。unmodified切替入力あり | 同左（自TFのOHLCで独立計算） | 同左 |
| ATR length / factor | 28 / 5.0 | 28 / 5.0 | 28 / 6.0 |
| trend flip | close が反対側trailを確定超え（E-3） | 同（自TF確定バー） | 同 |
| extremum更新 | flip時 high/low リセット→トレンド方向へ単調更新 | 同 | 同 |
| dynamic arm更新 | 毎確定バーで trail ratchet・f1/f2/f3再計算 | 自TF確定バー毎（チャート上は階段） | 同 |
| zone生成（凍結） | flip確定バーで旧armをFrozenZone化しarray push（FIFO 20） | 同 | 同 |
| Optimal Entry生成 | classification=高圧の確定バーで [f2,trail] を活性化 | 同 | 同 |
| target生成 | 対象外（CTはentry側） | 自帯がCTのExternalTarget候補 | 同（extended target） |
| invalidation | 現在trail（動的）。FrozenZoneはtrailAtFreeze | 同 | 同 |
| visit | 帯外→帯内の確定再侵入で+1（連続滞在は加算しない。v1.5準拠） | 同 | 同 |
| consumed | visitCount≥3（以後スコアのfreshness=0、OE禁止） | 同 | 同 |
| rearm | 廃止（動的armはflipで世代交代するため不要。FrozenZoneは不変履歴） | 同 | 同 |
| expiry | FrozenZone: array長>20でFIFO削除、または age>expiryBars(既定 20×freshAge) | 同 | 同 |
| confirmed HTF projection | 不要（自足） | 全要素[1]+lookahead_on | 同 |

分離の検証（M章のテストと対応）: 3エンジンのtuple戻り値を別変数群へ束縛し、いかなる式でも他エンジンの中間値を参照しない。唯一の合流点は (a) F章スコアのalignedFrames、(b) ExternalTarget解決、(c) 描画層。いずれも「読み取り専用の合流」であり状態は書き戻さない。

プリセット解決:
- `CT/15m/1H`: HTF1="15", HTF2="60"
- `CT/2H/4H`: HTF1="120", HTF2="240"（**既定**。15分チャートで15m/2H/4Hになる）
- `MANUAL`: 入力2値
- ガード: `chartSec > htf1Sec or htf1Sec > htf2Sec` → `runtime.error`。`htf==chart` は稼働させるが `isDuplicateOfCT=true`（スコア重複加点禁止・帯描画はCT側のみ）。

---

## D. State Machine

対象はエンジンの「現行arm」と「FrozenZone」。`ARMED V0`（pullback待ちの長期武装表示）は**廃止**する。flip確定バーから帯は即時FRESHで稼働する（参考コードの即時fib描画と目標画像の切替挙動に一致）。

| 状態 | 定義 | 入る条件 | 出る遷移 | 禁止遷移 | 描画 | アラート |
|---|---|---|---|---|---|---|
| BUILDING | flip当日barの未確定状態 | trend flip検知（barstate.isconfirmed前） | 確定でFRESHへ | BUILDING→OPTIMAL（未確定バーで高圧化禁止） | 描画なし（前armの塗り跡のみ） | なし |
| FRESH | armAge ≤ freshAge(50) | BUILDINGの確定 | ageでACTIVE / スコアでOPTIMAL / flipでINVALIDATED | FRESH→CONSUMED（visit経由なしの直行禁止） | 緑/赤帯（明るめ、G章） | Fresh zone alert |
| ACTIVE | age > freshAge の通常稼働 | FRESHのage超過 | OPTIMAL / TESTED / INVALIDATED | ACTIVE→FRESH（若返り禁止） | 緑/赤帯（基準透過） | なし |
| OPTIMAL | classification=高圧（Score≥70がhysteresis 2確定バー持続） | FRESH/ACTIVE/TESTEDから | スコア低下2bar持続で元の状態へ復帰 / INVALIDATED | OPTIMAL→CONSUMED直行禁止（visit判定を経る） | 青/黄帯 + OEボックス | Optimal Entry alert |
| TESTED | visitCount≥1 かつ deepestLevel≤1 | f1確定タッチ | DEEP_TESTED / CONSUMED / OPTIMAL / INVALIDATED | TESTED→FRESH | 帯維持+タッチラベル | 61.8 touch |
| DEEP_TESTED | deepestLevel≥2 | f2/f3確定タッチ | CONSUMED / OPTIMAL / INVALIDATED | DEEP_TESTED→TESTED（深度後退禁止） | 帯維持+タッチラベル | 78.6/88.6 touch |
| CONSUMED | visitCount≥3 | 3回目の独立visit確定 | INVALIDATED / EXPIRED のみ | CONSUMED→OPTIMAL/FRESH/ACTIVE | 帯は残すが減光（G章）。OE禁止 | なし（rejection/JSON発火禁止） |
| INVALIDATED | 自TF closeがtrailを確定突破（=flip） | flip確定バー | （現行armとしては終端）FrozenZone化して状態を引継ぎ、以後はEXPIREDのみ | INVALIDATED→他状態への復活禁止 | 新armに世代交代。旧帯は塗り跡 | Invalidation alert |
| EXPIRED | FrozenZoneがFIFO/age上限超過 | expiry条件 | なし（削除） | EXPIRED→任意 | オブジェクト削除（塗り跡は残る） | なし |
| TARGET | ExternalTargetとして参照されている（**役割フラグ**。FRESH/ACTIVE/TESTED等と併存） | ターゲット解決器が選択 | 到達（reached）or 構成変化で解除 | TARGET単独では遷移しない（本体状態に従属） | ターゲット枠+「EXTERNAL TARGET」ラベル | Target reached |

補足:
- v1.5の `STALE`（価格乖離ゾーンの除外）は、V2では「CTエンジンのCONSUMED減光＋スコアのproximity=false」に吸収され、独立状態としては持たない。乖離した帯はスコアに寄与せず、視覚上も減光で自然に後退する。
- 全状態遷移は `barstate.isconfirmed` でのみ確定する。未確定バーで状態・ラベル・アラートを変化させない（リペイント禁止）。

---

## E. Geometry

すべて疑似コード。`len`=ATR length、`k`=ATR factor（エンジン別）、`ex`=extremum、価格は最終段で `round_to_mintick` する。

### E-1. modified true range（参考コードで観察可能な公開式）
```
hiLo  = min(high - low, 1.5 * nz(sma(high - low, len)))
hRef  = low <= high[1] ? high - close[1] : (high - close[1]) - 0.5*(low - high[1])
lRef  = high >= low[1] ? close[1] - low  : (close[1] - low)  - 0.5*(low[1] - high)
mTR   = max(hiLo, hRef, lRef)
uTR   = max(high-low, |high-close[1]|, |low-close[1]|)   // unmodified切替用
```

### E-2. Wilder ATR（seed規約を明記）
```
wATR := nz(wATR[1]) + (TR - nz(wATR[1])) / len     // seed=0。初回値は TR/len から漸近
```
先頭len本は未成熟のためarm生成を抑止する（`bar_index < len*2` はエンジン出力を全naとする=ウォームアップガード）。

### E-3. trailing arm（ratchet）
```
up = close - k*wATR ; dn = close + k*wATR
Up := close[1] > Up[1] ? max(up, Up[1]) : up
Dn := close[1] < Dn[1] ? min(dn, Dn[1]) : dn
trend := close > Dn[1] ? +1 : close < Up[1] ? -1 : nz(trend[1], +1)
trail = trend==+1 ? Up : Dn
```

### E-4. extremum
```
ex := flipToBull ? high : flipToBear ? low
    : trend==+1 ? max(ex[1], high) : min(ex[1], low)
```

### E-5. Fibレベル（61.8/78.6/88.6は個別の意味を持つ）
```
f(p) = ex + (trail - ex) * p/100      // p ∈ {61.8, 78.6, 88.6} ; l100 = trail
```
意味: f1=Early entry、f2=Good entry、f3=Deep entry（excellent R/R）。ラベル・スコア・アラートはレベル別。

### E-6. Regular zone（動的帯）
```
band      = [min(f1,trail), max(f1,trail)]
sub-bands = [f1,f2], [f2,f3], [f3,trail]   // 濃度は深いほど濃く（G章）
```
**動的armとFrozen zoneの分離**: 帯は毎確定バーのtrail/exから再計算される「今の幾何」。FrozenZoneはflip時にf1/f2/f3/trailAtFreeze/exを1回だけ複写した不変スナップショットで、以後trailにもexにも追随しない。v1.4の「可変trail距離をそのまま固定ゾーン幅として凍結する」設計は、動的側=表示、凍結側=分析と役割分離することで解消する（凍結値は分析・proximity・target専用で、幅の妥当性はE-10ガードを通す）。

### E-7. Optimal Entry（仮定#1）
```
OE_active = (classification == HIGH_*)        // F章
OE_top/bottom = trend==+1 ? [trail, f2] : [f2, trail]   // deep側 [f2, trail]
```

### E-8. proximity distance（Zone Proximity設定 28/2 はここ**だけ**で使う）
```
proxATR  = wilderATR(proxLen=28)              // trail ATRとは別インスタンス
d(zone)  = close > zone.top ? close - zone.top : close < zone.bottom ? zone.bottom - close : 0
proximityHot(zone) = d(zone) <= proxFactor(=2.0) * proxATR
```
禁止事項: `proxFactor=2` をE-3のtrail factorに流用しない（v1.4回帰の再発防止としてM章でテスト化）。

### E-9. target zone / hard stop / invalidation
```
target(dir=+1) = 進行方向上方で最も近い HTFn の band（帯下端=zone.bottom を第一目標値）
hardStop(long)  = trail - stopBuffer(0.20)*wATR     // swingarm low の外側
hardStop(short) = trail + 0.20*wATR
invalidation    = trail（動的）／ FrozenZone参照時は trailAtFreeze
検証: long: hardStop < invalidation < close 側、short: 逆順。違反時はその値をnaとし表示しない。
```

### E-10. zone width / outlier / rounding ガード
```
minWidth = max(4 * syminfo.mintick, 0.05 * wATR)
maxWidth = 3.0 * k * wATR
凍結時: width∉[minWidth,maxWidth] or 任意値がna → FrozenZone生成をスキップ（塗り跡のみ残る）
outlier: |ex - trail| > 6 * k * wATR（ギャップ異常）→ 当該バーの凍結・OE生成を停止
表示・JSON直前に round_to_mintick
```

---

## F. Pressure Score（100点・確率ではない）

観測可能な証拠のみで構成。全項目「欠損=0点」でnaを伝播させない。二重加点防止規則を各行に明記。

| # | 項目 | 配点 | 判定（確定バー） | 欠損時 | 二重加点防止 |
|---|---|---:|---|---|---|
| 1 | 3時間足方向整合 | 25 | 対象方向に整合するエンジン数×(CT:9, HTF1:8, HTF2:8) | HTF未成立→当該0 | isDuplicateOfCTのTFは加点しない |
| 2 | Fib depth | 20 | 現在価格の帯内深度: f1到達8 / f2到達14 / f3到達20 | 帯外0 | 最深1段のみ（累積しない） |
| 3 | Freshness | 15 | FRESH:15 / ACTIVE:8 / TESTED·DEEP:8 / CONSUMED:0 | — | 状態は単一なので排他 |
| 4 | 構造強度（5-EMA stack） | 15 | EMA(8,21,55,100,200)の方向整列1本3点（closeとの位置+隣接順序） | EMA未成立分は0 | 同一EMAの位置と順序を重複加点しない（順序のみ数える） |
| 5 | Directional relative volume | 10 | relVol≥1.2かつ方向一致:10 / ≥1.0:5 | volume無し→0 | ローソク方向1回のみ判定 |
| 6 | Zone proximity | 10 | E-8のproximityHot:10（Enable Zone Proximity Bonuses=ONの時のみ） | proxATR na→0 | 複数ゾーンhotでも10固定 |
| 7 | RSI location | 5 | long候補でRSI≤30、short候補でRSI≥70:5 | RSI na→0 | — |
| 合計 | | **100** | | | |

- zone visit count: 直接加点しない（#3のCONSUMED=0で反映）。rejection: 加点せず、成立時にラベルへ `✓REJ` タグ（v1.5の教訓: 結果を入力に混ぜない）。structural target availability: 加点せずグレード条件（下記）。session: 加点せず、アラートフィルタ（I章）。
- 高圧分類（4色）: `Score(dir) ≥ pressureGate(70)` が2確定バー持続 → HIGH。解除も2バー持続（hysteresisで縦縞・点滅を防止）。
- グレード: `≥85 EXCELLENT / ≥70 STRONG / ≥55 GOOD / ≥40 WEAK / <40 POOR`（元ガイドの閾値を踏襲）。ただし `EXCELLENT` は `targetAvailable=true` を追加要件とし、満たさない場合はSTRONGへキャップ（利確幾何なき満点を禁止）。
- 表示書式: `87/100 · STRONG`。`%` 記号・"probability"・勝率・bounce率は全面禁止。全テーブル・レール・JSONに `SCORE IS NOT PROBABILITY` を付す。

---

## G. Visual Specification

描画順（z-order昇順=奥から）。transparencyはPine `color.new` の値（大きいほど透明）。

| z | レイヤー | プリミティブ | 色 | transp | width | style | label size | 最大数 | 右延長 | 履歴 | 重複時 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | Context(HTF2) Regular band | plot×4+fill×3 | bull #1B5E20 / bear #7F1D1D（高圧: #1565C0 / #C9A227） | sub1:88 sub2:80 sub3:72 | 1 | linebr | — | 1帯 | plot自然延長なし | plot履歴で永続 | 最奥固定 |
| 2 | HTF1 Regular band | 同上 | 同色系 | 84/76/68 | 1 | linebr | — | 1帯 | 同 | 同 | HTF2の上 |
| 3 | Chart Regular band | 同上 | 同色系 | 80/72/64 | 1 | linebr | — | 1帯 | 同 | 同 | HTF1の上 |
| 4 | Dynamic SwingArm trail線 | plot×3（各エンジンl100） | trend色 bull #26A69A / bear #EF5350 | 0 | CT:2 HTF:1 | linebr | — | 3 | なし | 永続 | 帯の上 |
| 5 | Optimal Entry box | box×3（最新のみ） | 青 #1565C0 / 黄 #C9A227 | 枠0 / 面75 | 枠2 | solid | — | 3 | 現在バー+5bar（上限15） | 消滅で削除 | 帯の上 |
| 6 | Fib lines | plot（帯のf1/f2/f3線を白系で強調） | white | f1:35 f2:20 f3:35 | 1 | linebr | — | 9 | なし | 永続 | — |
| 7 | Invalidation/hard stop | plot×2（CTのみ既定ON） | white / silver | 25/45 | 1 | linebr+cross | — | 2 | なし | 永続 | — |
| 8 | External Target | box×2+label | 反対色の枠のみ | 枠20/面92 | 1 | dashed | small | 2 | +10bar固定 | 到達で解除 | OEより下 |
| 9 | Candles(RSI色) | barcolor | OB #FFFFFF / OS #7E57C2（既定はガイド準拠 青/赤、入力で変更） | — | — | — | — | — | — | 永続 | — |
| 10 | Confirmed labels | label | 帯色ベース: bull青地 / bear赤地 / WEAK以下は灰地 | 地10 | — | — | small（入力Tiny..Huge） | タッチ履歴: エンジン毎40・全体120 / 右端レール: 4×3=12 | レールは最終バー+2..+12bar | タッチ履歴は残存（FIFO削除） | Label Spacing×5barで同レベル間引き、縦は自動段組 |
| 11 | Tables | table×3 | v1.5トーン踏襲 | — | — | — | normal | 3 | — | — | 最前面 |

- テキスト書式（タッチ/レール共通）: `"{TF} {level}% {len}/{factor} · {score}/100 {GRADE}"`＋グレード絵文字（🔥/⚡/✓/⚠/❌）。SwingArm行は `"{TF} Swingarm {len}/{factor} · {score}/100 {GRADE}"`。
- 密度改善（元画像のラベル過密への対策）: `Display density = FULL / REDUCED / CAPTURE`。REDUCED既定=タッチ履歴をGOOD以上のみ・レールはSwingarm+88.6のみ。CAPTURE=レールとテーブルのみ。
- 塗り跡（履歴fill）は「当時の分類色のまま」保持し、後から再着色しない（v1.3縦縞の再発防止と、履歴の正直さの両立）。
- 右延長はすべて上限+15barの有界とし、いかなる予測値も置かない。

---

## H. Inputs and Defaults

| group | input | 型 | 既定 | 備考 |
|---|---|---|---|---|
| 01 · Preset & Engines | Trading Type | options: `CT/15m/1H`, `CT/2H/4H`, `MANUAL` | **CT/2H/4H** | 15分チャートで15m/2H/4H（目標画像構成） |
| | Manual HTF1 / HTF2 | timeframe | "120" / "240" | MANUAL時のみ有効 |
| | Trail kernel | options: modified / unmodified | modified | E-1 |
| | CT ATR length / factor | int / float | 28 / 5.0 | SwingArm本体 |
| | HTF1 ATR length / factor | int / float | 28 / 5.0 | |
| | HTF2 ATR length / factor | int / float | 28 / 6.0 | 仮定#2（画像28/6） |
| 02 · Zone lifecycle | Fresh Zone Age Threshold (bars) | int | 50 | 各エンジン自TFバー（仮定#10） |
| | Max Zones to Track | int | 20 | エンジン毎FrozenZone上限 |
| | Consumed visits | int | 3 | |
| | Zone expiry (×freshAge) | int | 20 | EXPIRED条件 |
| 03 · Proximity & Pressure | ATR Length for Zone Detection | int | 28 | E-8専用 |
| | ATR Factor for Zone Proximity | float | 2.0 | E-8専用。**trail factorへ流用禁止** |
| | Enable Zone Proximity Bonuses | bool | true | F#6 |
| | Enable Fresh Extreme Zone Bonus | bool | true | F#3のFRESH:15を有効化（OFF時は8） |
| | High-pressure gate | int | 70 | F章 |
| 04 · Fib | Fib levels | float×3 | 61.8 / 78.6 / 88.6 | |
| | Hard-stop buffer (ATR) | float | 0.20 | E-9 |
| 05 · Session | SessionTime | session | 0530-0230 | チャートTZ（校正時にTZ入力追加可） |
| | Highlight Session | bool | false | 背景淡色 |
| | Session-filtered alerts | bool | false | I章 |
| 06 · Display engines | SwingArm Pressure #1/#2/#3 | bool×3 | true | 帯表示（#1=CT, #2=HTF1, #3=HTF2） |
| | Most recent Optimal Entry #1/#2/#3 | bool×3 | true | OEボックス |
| 07 · Labels | Fib 61.8/78.6/88.6 Break Labels | bool×3 | false | タッチ履歴ラベル（元設定はOFF） |
| | Swing Arm Break Labels | bool | false | trail折れラベル |
| | Rail labels | bool | true | 右端レール |
| | Label Spacing | int | 1 | 間引き係数（×5bar） |
| | Label size | options | Normal | Tiny..Huge |
| 08 · RSI candles | RSI length / OB / OS | int | 14 / 70 / 30 | |
| | Enable RSI Candle Color | bool | true | |
| | OB / OS color | color | 青 / 赤（ガイド既定。ユーザー例: 白/紫） | |
| 09 · Tables | Show Score Status | bool | true | 旧"PROBABILITY STATUS"を改名 |
| | Volume spike sensitivity | options: Most Active/Active/Quiet | Most Active | 仮定#8（relVol閾値 1.15/1.30/1.50） |
| | Show SwingArm Status | bool | true | 位置Bottom Left・サイズNormal入力付き |
| | Show Break Statistics | bool | true | 位置Top Right・サイズNormal入力付き |
| 10 · Trendlines | Length High / Low | int | 5 / 5 | pivot長 |
| | Show Trendline / Trend Pressure label | bool | true / true | 二次機能（フェーズ10） |
| 11 · Density | Display density | options | REDUCED | FULL/REDUCED/CAPTURE |
| | Reduced visual effects | bool | false | fillを枠線化（軽量端末向け） |
| 12 · Alerts/Export | Emit JSON on confirmed rejection / OE | bool | false | I章 |
| 13 · Info box | Watermark（Slot1..3, size, pos） | 各種 | timeframe/ticker/custom, huge, top-center | 純装飾。CAPTUREでは非表示 |

---

## I. Alerts and Export

全アラート共通: `barstate.isconfirmed` 必須、dedupeKey（B-10）で状態解除まで再発火禁止、`alert.freq_once_per_bar_close`。Session-filtered=ON時はセッション外を抑制。

| # | イベント | 確定条件 | 重複抑制 | JSONフィールド（追加分） | NQX連携 |
|---|---|---|---|---|---|
| 1 | Fresh zone | エンジンflip確定（FRESH開始） | flip毎に1回 | engine, dir, f1/f2/f3, trail | 情報のみ（packet不可） |
| 2 | 61.8 touch | closeがf1を帯方向へ確定クロス | zoneId+level | engine, level:1, score, grade | 情報のみ |
| 3 | 78.6 touch | 同f2 | 同 | level:2 | 情報のみ |
| 4 | 88.6 touch | 同f3 | 同 | level:3 | 情報のみ |
| 5 | Optimal Entry | OPTIMAL遷移確定 | OPTIMAL期間毎1回 | oe_top, oe_bottom, score, grade | **可**（要素完全時のみ） |
| 6 | Confirmed rejection | f2以深到達後、f1外側へ確定回帰（方向別。v1.5規則踏襲） | zoneId毎 | side, invalidation, hard_stop | **可**（NQX/1本命イベント） |
| 7 | Invalidation | trail確定突破=flip | flip毎 | engine, old_dir | 情報のみ |
| 8 | Target reached | ExternalTargetへ確定タッチ | target毎 | src_role, target_top/bottom | 情報のみ |
| 9 | Structure change | 5-EMA構造状態（Bull/Bear/Neutral）の確定遷移 | 状態毎 | structure_state, strength | 情報のみ |

JSON契約:
- schema idは `NQX_ATR_PRESSURE/1` を維持し、イベント#6（CONFIRMED_REJECTION）はv1.5の全フィールド互換＋ `engine`, `score`, `grade`, `oe_active` をadditive追加（既存validatorを壊さない）。
- イベント#5はevent種 `OPTIMAL_ENTRY` として送るが、既定OFF。Nightwatch validatorがevent種を拡張するまで有効化しない（無効packetで既存分析を上書きしない原則）。
- 全JSONに `score_is_probability:false`。na値は `null`。NaN禁止。
- Data Window: エンジン毎に `NQX_DATA_{ROLE}_TREND/TRAIL/F1/F2/F3/SCORE_L/SCORE_S/ZONES` を出す（constant color=1スロット、J章予算内）。

---

## J. Resource Budget（Pine v6）

| 資源 | 予算 | 内訳・規則 |
|---|---|---|
| 出力series上限64 | 目標≤58 | 帯plot 3エンジン×4=12（color=na定数）/ fill 3×3=9×series色2=18 / trail可視plot3×series色2=6 / fib強調はfill側plot再利用0 / invalidation+stop 2 / barcolor 1 / alertcondition 9 / plotshape(rejection) 2×2=4 / Data Window 8 → 合計 ≈58。**残6は予備。新規plot追加時は本表を必ず更新**（v1.5でRE10140=71/64超過の実績あり） |
| request.security | 2回 | HTF1/HTF2各1回のtuple一括取得。追加呼び出し禁止 |
| max_boxes_count | 50 | OE 3 + Target 2 + 予備。FrozenZone履歴はbox化しない（塗り跡=plot履歴で表現するため0個） |
| max_lines_count | 100 | トレンドライン2 + レール補助線≤36 + 予備 |
| max_labels_count | 500 | タッチ履歴120 + レール12 + ターゲット/OEタグ≤10 + 予備 |
| max_bars_back | 明示2000 | 4H×20ゾーン分の遡及を保証（15分チャートで4Hの50bar fresh＝800チャートバー） |
| 配列上限 | エンジン毎: FrozenZone20 / タッチラベルid 40 | 超過時は最古をarray.shift+オブジェクトdelete |
| 3エンジン合計 | ゾーン60・ラベル120・box≤10・line≤40 | barstate.islastの再描画対象はレール・OE・target・tableのみ（毎バー再生成禁止） |
| 削除規則 | FIFO一元化 | 削除は「配列からshift→対応する描画idをdelete」の順で、孤児オブジェクトを残さない |

---

## K. Implementation Blueprint（v1.5の資産分類）

V2は新規ファイル `nqx_swingarm_pressure_v2.pine`。v1.5は無変更で併存（比較・Nightwatch既存連携の後方互換のため）。

| v1.5の構成要素 | 分類 | 理由・V2での扱い |
|---|---|---|
| ATR kernel（E-1/E-2相当、modified TR+Wilder） | **KEEP** | 参考コードと一致済み。エンジン別len/factor化のみ |
| f_zoneEngine のtrail/extremum/fib算出部 | **KEEP**（関数分割） | E-3〜E-5と同一。ただし「pullback snapshot」以降を切除 |
| f_zoneEngine のARMED/pullback snapshot部 | **REMOVE** | D章でARMED V0廃止。flip即時FRESHに置換 |
| f_zoneEngine のVISITS/CONSUMED判定 | **KEEP**（移植） | 帯外→帯内の確定再侵入カウントはFrozenZone/armに共通適用 |
| REARM機構 | **REMOVE** | 世代交代（flip=INVALIDATED→新arm）で不要 |
| stale機構（距離除外・ghost・リスクフォールバック） | **REWRITE** | 独立状態を廃し、F#6 proximity=0点＋CONSUMED減光＋リスクは常に現行trail基準へ統合 |
| f_confirmedZoneEngine（[1]+lookahead_on投影） | **KEEP** | パターンそのまま。tuple構成のみ拡張 |
| Pressure Score | **REWRITE** | F章の7項目100点へ全面再設計（配点・hysteresis・グレード・EXCELLENTのtarget要件） |
| rendering（凍結矩形+fib線+gradient） | **REWRITE** | G章11層へ全面刷新（動的帯3系統が主役） |
| labels（live label 1枚+ghostタグ） | **REWRITE** | タッチ履歴+右端レール方式へ |
| Truth Table | **REWRITE** | Score Status / SwingArm Status / Break Statisticsの3表へ分割 |
| JSON（NQX/1 + zone_stale） | **KEEP**（拡張） | I章のadditiveフィールド追加。schema id不変 |
| alerts | **REWRITE** | I章9種へ再編（発火条件・dedupe込み） |
| Data Window plots | **REWRITE** | エンジン別8系列へ再編（J章予算準拠） |
| approach halo / freshness ladder / depth gradient | **KEEP**（意匠として） | G章の帯サブバンド濃度・OE枠強調に転用 |

---

## L. Phased Implementation

各フェーズ: 変更対象 / 完了条件 / 回帰テスト / ロールバック地点。フェーズ末ごとにTradingView実機コンパイル（v1.5でのRE10140教訓により出力series数をJ章表と照合）。

| Phase | 変更対象 | 完了条件 | 回帰テスト | ロールバック |
|---|---|---|---|---|
| 1. Data structures & three engines | エンジン関数・tuple・preset解決・ガード | 15分チャートでCT/2H/4Hの trend/trail/f1..f3 がData Windowに出る（描画なし） | 3エンジン値がv1.5のct系列と（CTのみ）一致。HTF値が[1]確定値 | ファイル新規のため常時可（v1.5無傷） |
| 2. Dynamic SwingArm geometry | 帯plot+fill（3段）、trail線 | 目標画像と同型の階段帯が3系統出る（単色でよい） | flip時の帯切替がエンジン単位で独立（M-3） | Phase1末コミット |
| 3. Frozen zone history | FrozenZone配列・visit/consumed・expiry | flipで凍結、20件FIFO、visitカウントがdata windowで検証可 | 凍結値が以後不変（M-5） | Phase2末 |
| 4. Optimal Entry & classification | F章スコア骨格（#1〜#4）+4色+hysteresis+OE box | 高圧時に帯が青/黄化しOE boxが出る。点滅なし | 色切替が2確定バーhysteresisを守る | Phase3末 |
| 5. Pressure Score完成 | F章#5〜#7、グレード、EXCELLENTのtarget要件 | スコアとグレードがラベル書式で出る。100超なし | 欠損入力（volume無しシンボル）で0点扱い | Phase4末 |
| 6. Rendering完成 | G章11層、レール、密度モード、RSI candle | 目標画像との構図一致（M-16） | 出力series≤58、CAPTUREモードで枠のみ | Phase5末 |
| 7. Alerts & JSON | I章9種+JSON | 全アラートが確定バーのみ・dedupe動作 | JSONにNaNなし、validator通過 | Phase6末 |
| 8. NQX integration | Data Window、Nightwatch取込手順書 | Nightwatchが新plotsを読める（手動確認） | 無効packetが既存分析を上書きしない | Phase7末 |
| 9. Performance hardening | J章予算の実測・削除規則・max_bars_back | 10,000バー履歴でエラーなし、オブジェクト数が予算内 | 60ゾーン到達時のFIFOが孤児を残さない | Phase8末 |
| 10. TradingView visual calibration | A-3の校正項目#1〜#11 | ユーザー承認（目標画像との並置比較） | 校正はすべて入力値変更で吸収（コード変更が要る場合は該当Phaseへ戻る） | 各校正前 |

---

## M. Acceptance Tests

| # | テスト | 合格条件 |
|---|---|---|
| 1 | エンジン独立性 | 同一時間足を3roleに指定しても状態変数が混ざらない（isDuplicateOfCTで描画/加点のみ抑制） |
| 2 | confirmed HTF | 未確定HTFバー中にHTF帯・fib・ラベル・スコアが一切動かない（リプレイで検証） |
| 3 | flip独立 | CTのflipでHTF1/HTF2の帯が不変。逆も同様 |
| 4 | 動的Arm | trail ratchetに沿って帯が階段状に更新され、履歴が塗り跡として残る |
| 5 | Frozen不変 | FrozenZoneの全価格が生成後に1tickも動かない |
| 6 | ゾーン上限 | エンジン毎20超過で最古がFIFO削除され、描画オブジェクトの孤児が残らない |
| 7 | レイヤー分離 | Regular帯とOptimal Entry boxが別レイヤーで、OEのみ独立にON/OFFできる |
| 8 | 4色の正しさ | 緑=通常Bull帯／赤=通常Bear帯／青=高圧Bull OE／黄=高圧Bear OE。高圧色をラベル地・OE以外の帯履歴再着色に使わない |
| 9 | fib順序 | bull帯: trail < f3 < f2 < f1 < ex、bear帯: 逆順が常に成立（違反バーは描画スキップ+ログ） |
| 10 | リスク順序 | long: hardStop < invalidation、short: hardStop > invalidation。違反時は非表示 |
| 11 | スコア上限 | いかなる合成でも100を超えない（min(100,·)ではなく配点合計=100で保証） |
| 12 | 非確率 | 全出力に%確率・probability・勝率・bounce率の語が存在しない。`SCORE IS NOT PROBABILITY` が3テーブル・レール凡例・JSONに存在する |
| 13 | JSON健全性 | NaN不在、na→null、schema NQX_ATR_PRESSURE/1でvalidator exit 0 |
| 14 | No Synthetic Future | 右延長は+15bar以内の現在値水平のみ。未来価格・経路・予測線ゼロ |
| 15 | 排他 | 無効packet・未確定バー・欠損価格が既存NQX分析を上書きしない |
| 16 | 目標構図 | 15m MNQ・CT/2H/4H・同一期間で、目標画像と (a)3系統階段帯 (b)4色分類 (c)fib別ラベル (d)右端レール (e)テーブル構成 が同型（画素一致は要求しない） |
| 17 | 計算とUIの分離 | 全ラベル/テーブル/帯をOFFにしてもData Window系列とアラートが完全動作 |
| 18 | proximity分離 | proxFactor=2をtrail factorに変えても帯幅が不変（誤用防止の逆テスト: trail factor入力のみが帯幅を変える) |

---

## N. 最終判断

### なぜv1.5の延命では不十分か
v1.5は「各TFにつき最大1枚の凍結Fib矩形」を中核とする設計で、(1) 動的階段帯が存在しない、(2) fib1–trail間の広い帯構造がない、(3) ゾーン履歴を持たない（最大1枚）、(4) 4色分類・Optimal Entry・External Target・レベル別スコアの概念がない、(5) ARMED V0待機が目標挙動（flip即時帯生成）と正反対。これらは入力値やfactor調整では埋まらない**データモデル欠落**であり、v1.4→v1.5で行った修正（stale等）はこの誤ったモデルの症状緩和に過ぎなかった。延命は不合格。

### V2で残すもの / 作り直すもの
残す: ATRカーネル（modified TR+Wilder）、trail/extremum/fib幾何、confirmed HTF投影パターン（[1]+lookahead_on）、VISITS再侵入カウント、NQX/1 JSON基盤、安全条件一式。
作り直す: エンジンを3系統同格へ、動的帯（描画の主役）と凍結ゾーン（分析）の分離、状態機械（ARMED V0廃止・FRESH/OPTIMAL/TARGET導入）、100点スコアとグレード、11層描画、3テーブル、9アラート。詳細はK章の関数別分類が正。

### 最初に実装すべき最小縦断スライス（Phase 1–2の中の最小片）
「CTエンジン単独＋動的帯＋trail線＋fib3線＋flip時のFrozenZone凍結1件」を15分チャートで動かし、参考コード（Blackflag FTS）を同チャートに並置して trail/ex/f1–f3 が全バー一致することを確認する。これが一致しない限り上位機能に進まない（幾何の同一性がすべての土台）。

### TradingViewで最初に比較すべき15分足の画面構成
MNQ1! 15分・表示範囲は目標画像と同じ約4営業日・`CT/2H/4H`・密度REDUCED・テーブル3枚ON。並置比較の観点順: ①4H帯の階段形状と上下端価格 → ②15m帯のflip位置 → ③高圧時の黄/青変色区間 → ④右端レールの4枚組×3TF → ⑤タッチラベルの位置と文言。①が合わない場合はfactor（28/5, 28/5, 28/6）の校正から着手。

### 画像だけでは確定できないパラメータと校正方法
A-3表の11項目が正本。校正はすべてPhase 10で「同一時刻・同一レンジのA/Bスクリーンショット比較」により入力値の変更のみで行い、幾何式そのものの変更が必要になった場合は該当フェーズへ戻る。特に優先度が高いのは #1 OE縦幅、#2 4H factor、#5 高圧閾値 の3点で、これらが目標画像の見た目の8割を決める。

---
*本設計書はclean-room原則に基づき、保護されたSwingArm High Pressure V6.8のソースコードを参照せずに、ユーザー提供の公開参考コード・公開ガイド・設定画面・チャート画像の観察のみから作成した。*

---

## O. Codex実装監査補遺（2026-07-17・本文との矛盾時はこちらを優先）

本補遺は、Pine Script v6の実行モデル、64 plot-count上限、確定HTF投影、NQX安全条件を満たすための拘束条件である。本文の数式・視覚目標・clean-room方針は維持し、以下の実装構造だけを補正する。

### O-1. エンジンと描画管理を分離する

1. `f_engineSeries()` はOHLCから `trend / trail / ex / f1 / f2 / f3 / ATR / flip / armId / age` のスカラー系列tupleだけを返す。box、line、label、table、array、UDTを生成・返却しない。
2. CTはチャート文脈で直接1回呼ぶ。HTF1/HTF2は同じ純系列エンジンを `request.security()` 内で呼び、確定した直前HTF値だけを `[1] + barmerge.lookahead_on` で投影する。
3. FrozenZone配列、描画オブジェクト、visit、expiry、FIFO削除はすべてチャート文脈のProjection Managerが管理する。`request.security()` 境界をオブジェクトや配列に越えさせない。
4. HTFのflipは、投影済み `armId` または `trend` とHTF確定時刻の変化からチャート文脈で一度だけ生成する。同一HTFイベントを下位足ごとに重複生成しない。

### O-2. DynamicArmとFrozenZoneの状態機械を分離する

- DynamicArmは現在進行中の階段帯であり、`REGULAR / HIGH_PRESSURE / INVALIDATED` の表示分類だけを持つ。帯そのものへvisit/CONSUMEDを適用しない。
- FrozenZoneは反転によって確定した旧アームの不変スナップショットであり、`FRESH → TESTED → DEEP → CONSUMED`、または `INVALIDATED / EXPIRED` を持つ。
- `BUILDING` は確定出力状態として使用しない。未確定足では旧状態を保持し、確定flipで一度に新DynamicArmへ遷移する。
- flip時に凍結する値は、リセット後の新アームではなく旧アーム最終確定バーの `trail[1] / ex[1] / f1[1] / f2[1] / f3[1] / direction[1]` である。生成後は1tickも移動させない。

### O-3. 非リペイント境界

- CTの状態遷移、extremum更新、flip、FrozenZone生成、visit、invalidation、alertは `barstate.isconfirmed` のときだけ行う。
- HTFは未確定値を表示・採点・JSON化しない。`lookahead_off` の現在HTF値と、`lookahead_on` の未シフト値は禁止する。
- リアルタイム足では最後の確定状態を保持する。確定前の色、ラベル、zone、scoreの先行変更を禁止する。

### O-4. 出力系列予算の修正

本文I章の「エンジン別8 Data Window系列」とJ章の単一「Data Window 8」は同時成立しないため、重複plotを作らない。

- `f1 / f2 / f3 / trail` の可視plotはData Window出力を兼ねる: 4×3エンジン = 12。
- 非可視plotは原則 `trend / ex / score` の3×3エンジン = 9まで。追加診断はDEBUGモード時だけ別ビルドで有効化する。
- 9種イベントは9本の `alertcondition()` に展開せず、確定バーで `alert()` を呼ぶ単一ディスパッチャを基本とする。UI用に必要なら汎用 `alertcondition()` は最大1本。
- trailは4境界の一つを兼ねるため別plotを増やさない。FrozenZoneはbox/lineオブジェクトで描画し、履歴ごとのplot系列を作らない。
- 各フェーズ終了時にTradingViewコンパイルでplot-countを実測する。フル版の内部目標は48以下、絶対上限は58。推測表だけで合格にしない。

### O-5. シナリオとRisk Geometryの凍結

- DynamicArmの外部ターゲットは画面上のライブ参考値として動いてよいが、NQXシナリオを生成した瞬間に `entry / stop / invalidation / target[] / sourceArmId / sourceTime` を固定する。
- FrozenZone由来の拒否・再侵入シナリオでは、hard stopとinvalidationの基準に現在の動的trailではなく `trailAtFreeze` と凍結境界を使う。
- longは `hardStop < invalidation < entry < target`、shortは逆順を満たすときだけemitする。順序不成立、欠損、STALE、未確定HTFでは無効packetとし、既存分析を上書きしない。

### O-6. 実装ゲート

最初のコミット対象はCT単独スライスだけとする。modified TR、Wilder、ratchet trail、trend、extremum、f1–f3、確定flip、旧アーム凍結1件を実装し、公開Blackflag FTSとの全バー一致をユーザーがTradingViewで確認するまで、HTF、Score、4色分類、ラベル履歴、table、alerts、JSON、Nightwatch連携へ進まない。係数調整で数式不一致を隠してはならない。

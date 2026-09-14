# R37 画像カタログ層の通電と、その前提の修正

2026-08-24。監査（実サイクル 403 本の再生 + 全所見の反証パス）で確定した欠陥を直した。

## 何が起きていたか

| 指標 | 修正前 | 修正後 |
|---|---|---|
| `alignment` が (0,0) のサイクル | **403 / 403** | 30 / 403 |
| 画像層が採点に効いた候補 | **0** | 129 |
| 投票できたモデル | **0 種** | 6 種 |
| `PRIOR_DAY_DERIVED` の成立 | **2 / 403** | 157 / 403 |
| CVD 健全性が実態と一致 | **否**（素の int を FRESH 扱い） | 是（`RETRY_REQUIRED` 243） |
| ARMED 候補 | 101 | 126（全て grade A） |
| 画像合議だけで武装した候補 | — | **0**（構造的に不可能） |

画像カタログ層は計算され、Telegram にも表示され、しかし **403 サイクルすべてで
一票も投じていなかった**。`IMAGE_STRATEGY_CATALOG.md` §8 が謳う「モデルの一致は
候補 score を押し上げ、対立は penalty を付ける」は、実測でどちらも起きていなかった。

## 順序がすべてだった

先に層へ通電すると危険だった。`IMAGE_MODEL_ALIGNMENT` は `CONFIRMATION_EVIDENCE` の
一員で、`NO_CONFIRMATION` はハードブロッカーである。つまり**画像合議だけで武装
ゲートを解除できた**。実測（cycle 1505）:

```
市場入力は同一のまま、画像合議だけを付与
  付与前  VP80_REVERSION BUY  score=10  grade=A+  state=WATCH  blockers=['NO_CONFIRMATION']
  付与後  VP80_REVERSION BUY  score=12  grade=A+  state=ARMED  blockers=[]
```

403 本での露出は**新規武装が最大 111 件増え、その 110 件は画像合議が唯一の確認**
（grade A 80 / A+ 30）。そして層の検出器 3 つが方向を誤っていた。順序を逆にすると、
**方向を誤る層が唯一の武装根拠になる**。したがって次の順で直した。

## 1. 方向欠陥を先に潰す

### CRT の Double Purge が評価順で SELL に潰れる

`direction = "SELL" if sweep_up else "BUY" if sweep_down else None` は、上下**両方**を
刈って内側へ戻った足で必ず SELL になる。出力が「上のみパージ」と完全に一致し、
下側の掃引情報が消えていた。

確定 OHLC からはどちらを先に刈ったか決められない。**方向を作らない**（カタログ §5 が
`Double Purge` を独立型として要求しているとおり `type=DOUBLE_PURGE_CRT` / `sweep=BOTH`
で記録し、`valid=False`）。価格や方向を補間しない、という既存の規律に合わせた。

### IFVG の方向が valid の根拠と別ゾーン由来

`valid` は確認済みゾーン（`eligible`）から、`direction` は別配列（`near or zones`）の
末尾から取っていた。突き合わせが無いので、**BUY の確認証跡で SELL 票が立ち、
配列順を入れ替えるだけで方向が反転**した。`freshness`/`provenance` は `selected` 由来
なので、方向を出したゾーンの鮮度は一度も検査されていなかった。

方向も `selected`（= `eligible[-1]`）から取る。`detect_blocks` が既にそうしており、
これで根拠・方向・鮮度の三者が同じゾーンを指す。

### rejectionWick が上下ヒゲを比較しない

`lower` を無条件に先に評価していたため、**上ヒゲが下ヒゲの 80 倍でも BUY** になった
（`o100 h120 l99.75 c100` で実測）。doji の閾値 0.015 は増幅要因にすぎず、
`body=0.25` の通常足でも発火する。

支配的なヒゲで方向を決める。あわせてレベル照合を**ヒゲ先端**基準にし
（掃除されるのは終値ではない）、最初の一致で `break` するのをやめた
（変数名は `nearest` だが実際には最近傍を選んでいなかった）。

## 2. 層へ通電する

### 欠落を「明示的な否定宣言」に変換していた

`_vote_direction` は *「欠落は観測扱いのまま／明示的な否定だけ fail-close」* と設計され、
コメントにもそう書いてある。ところが `_repo2_lifecycle` が先に走り、欠落した
`sessionId`/`freshness` を文字列 `"UNKNOWN"` で埋めていた。`value is None` の分岐へ
永遠に到達せず、**全モデルが無条件に失格**していた。`crt` は 212/403 本で
`valid=True / status=CONFIRMED / direction=BUY / freshness=FRESH` まで到達しながら、
212 本すべて棄却されていた。

欠落は埋めない。設計意図どおりに戻した。

### sessionId が SMT ピア取得に結合していた

`tv_snapshot` は `if peers:` の中でしか `sessionId` を書かない。ピアは CDP がタブ0に
固定束縛されるため実環境では取得できない。結果、**SMT 取得の失敗が無関係な CRT の
投票権まで巻き添えで消していた**。

取引セッション（ASIA/LONDON/NEWYORK）の ID を `derive_session_id()` で常に採番し、
bundle に載せる。`normalize_peers` が返す **SMT 窓**の ID（`ET-YYYYMMDD-AM/PM`）とは
別物で、`index_smt` の照合は従来どおり snapshot 側の ID を使うので干渉しない。

### 投票に必要なキーが無い

- `vwapReversion`（CONFIRMED 119 本）と `rejectionWick`（同 21 本）は `valid` を返さず、
  `_vote_direction` の最初のチェックで落ちていた
- `mmxm` と `amdWyckoff` は `status` を返さず、`_repo2_lifecycle` が `lifecycle=UNKNOWN` を
  付けて **算出済みの direction ごと `valid=False` へ上書き**していた。カタログ §3・§4 の
  2 章分がこれだけで無効化されていた

いずれも欠けていたキーを足した。あわせて `mmxm` の premium/discount 逆張り禁止が
「レンジ未取得なら制約なし」で素通りしていたのを fail-closed にした。

### 相関する検出器を 1 票に畳む

`detect_mmxm` は `classify_amd` の出力をそのまま入力に取る。両者を独立票にすると
同じ足の判定を二重計上し、同じ根拠だけで `alignment` の ±2 閾値をまたげてしまう。
既存の `crt/fib/fibCrt` 族と同じ扱いにした（`CORRELATED_FAMILIES`）。

## 3. 層をハードゲートの鍵から外す

`IMAGE_MODEL_ALIGNMENT` を `CONFIRMATION_EVIDENCE` から外した。**層を弱めるためでは
ない。**

1. **定義に合わない。** ここは「構造と独立した**データ源**」の集合だが、画像カタログ層は
   セットアップの構造を作ったのと同じ 3 分足から再計算した読みであって、別の源ではない
2. **カタログ自身の契約に反する。** `build_strategy_matrix` は
   `hardGateImpact: "RANKING_ONLY"` を宣言し、§8 も「口座リスクや CVD 制約をすり抜ける
   ハードゲートにはならない」と明記する。ここに入れると宣言が嘘になる

層は引き続き score へ ±2 で効き、順位と `strategyBias` を動かす —— 武装の唯一の鍵に
ならないだけである。**実測でも武装 126 件のうち画像合議が唯一の確認である候補は 0。**

## 4. 静かに間違っていたもの

### `REGIME_FIT` +2 が実質無条件

`bundle["regime"]` を書き込むコードはリポジトリに存在しない（全参照が
`.get("regime", "MX")` の読み出し）。実データは 403/403 が `"MX"` で、`route_regime` の
else 節が `preferred = set(MODEL_ORDER)` = **全4モデルに +2** を配っていた。

「入力が無い」が「最大加点」に化けており、チェーン完成 +3 と合わせて**外部入力ゼロで
素点 8 = grade A** に届いていた。ルーティングの意見が無いなら中立にする（else 分岐の
+1 が付く）。`regimeKnown` を記録に残す。

### CVD が「新鮮扱いなのに無得点」

`cvd_health` は「値が入っているか」しか見ず、素の int でも `status=FRESH /
aplusAllowed=True` を返す。一方 `_cvd_score` は dict の bias を要求するので加点は常に 0。
**A+ 上限化も効かず、確認にもならない**という最悪の両立だった（403 本中 401 本が
素の int、A+ ARMED 43 本がこの状態で承認されていた）。

健全性と採点が同じ事実を別々に判断していたのが根本原因なので、判定を `cvd_bias()` の
1 関数に集約した。**片方だけ変えられない構造にした。**

### freshness の語彙が割れ、OTE 合流 58 本を捨てていた

`derive_range_anchor` は導出レンジに必ず `ACTIVE` を刻み、`resolve_range_anchor` は
`{FRESH, ACTIVE}` を受理する。しかし fib 系 3 箇所の許可集合は
`{FRESH, WICK_TESTED, BODY_TESTED}` で **`ACTIVE` を欠いていた**。同じアンカーが
「OTE には使えるが Fib 整合には使えない」という不整合。レンジが取れた 236 本すべてが
`FIB_ANCHOR_OR_FRESHNESS_MISSING` で落ち、**うち 58 本は価格が実際に OTE 帯の中に
あった**。`USABLE_ANCHOR_FRESHNESS` に集約した。

### 前日レンジが Low 側のラベル形だけを理由に負けていた

対照表は完全一致で引く。`Previous Day Low` の完全一致は **18 回**しかない一方、
`"Weekly Low | Previous Day Low"` が 155 回、`"New York Low | Previous Day Low"` が 44 回。
対は高安の**両方**が一致して初めて成立するので、最も強いはずの前日レンジが
**403 本中 2 回**しか成立せず、最も弱い進行中セッションのレンジが勝ち続けていた。

複合ラベルを `|` と `/` で分解して別名としても引けるようにした（完全一致を常に優先）。
結果、`PRIOR_DAY_DERIVED` が **2 → 157 本**になった。

### `ict_coverage` が「導出で代替した」を「入力があった」と報告

この関数の docstring は「入力が無かったのか、評価した上で効かなかったのかを区別する」
ためのものだと明言しているのに、`rangeAnchor` は `range.valid` だけを見ており導出でも
`true` を返していた。可否そのものは据え置き、`rangeAnchorType` /
`rangeAnchorSource` / `rangeAnchorDerived` を併記して出所を追えるようにした。

## 5. 実行経路（実弾に関わるもの）

### トレール SL が現在値の反対側に置かれる

`current_stop` との単調性は検査するが、算出した `desired` と**現在価格**を一度も
比較していなかった。`best` は終値ではなく高値/安値の極値なので、価格が走ってから
普通に押しただけで `desired` が現在値を追い越す:

```
BUY / entry 29630 / risk 25pt(distance 18.75) で 29695 まで走り 29660 へ押すと
desired = 29676.25 —— LONG の損切りを現在値の 16.25pt 上へ置こうとする
```

MNQ で 19pt の押しは日常であり、特殊局面ではない。下流に止める層は無い
（`management_intent` は tick と正数のみ、`order.py --modify` に side 判定は無く、
Worker も stop をそのまま採用）。しかも送信は `cancelandbracket` なので、拒否されると
**runner が保護注文ゼロ**で残りうる。保護側に最低 1 ティック無ければ MODIFY を出さない。

### 子プロセスの制限時間が HTTP 予算より短い

`order.py --confirm` は逐次 HTTP を最大 6 本使い、最悪 114 秒（同ファイルの既存
コメントが自認）。**30 秒**で kill すると、webhook には注文が届いているのに
`TimeoutExpired.stdout` ごと経路 envelope を捨て、次サイクルで `ownership_binder` の
accepted=0 → 恒久 hold に落ちる。

`ORDER_SUBPROCESS_TIMEOUT_SEC = 150`（監視周期 3 分を超えない範囲で予算に余裕）へ。
あわせて timeout 時も stdout を捨てずに残す。制御は従来どおり HALT（再送しない）。

### 分割建玉の全量 modify

`cancelandbracket` は保護注文を丸ごと張り替えるので、TP1 と runner 最終 TP が生きて
いる間に単一の take_profit を送ると **2 本が 1 本に潰れる**。Worker は qty を
`position.qty` で上書きするため、2 枚建玉中は runner だけの部分 modify を発行する術が
無い —— 選べるのは全量 modify だけで、それは必ず TP1 を消す。

自律経路は `autotrade_engine` 側で守られていたが、その不変条件が `order.py` に無く、
telegram の `/modify buy 2 ...`（HELP の用例そのもの）から素通りで到達できた。新規側には
`FIXED_QTY_REQUIRED` があるのに modify 側だけ対応物が無かった。
`MODIFY_SPLIT_PLAN_PROTECTED` で拒否する。

## 6. CLAUDE.md を UTF-8 へ

`CLAUDE.md` は**リポジトリ内で唯一の非 UTF-8 ファイル**（106 ファイル走査中 1 件）で、
UTF-16LE だった。Claude Code がセッション開始時に自動読み込みする内容は最初から
文字化けしており、Read ツールでも同じ。**毎サイクルこの契約に従って動くはずの
エージェントが、契約文を読めていなかった。** 内容は無傷のまま UTF-8（BOM 無し・LF）へ
変換した。

## 検証

- `python tests/run_all.py` → **43 ファイル通過**（新規 2 ファイル・38 チェックを追加）
  - `tests/test_r37_strategy_activation.py` — 方向欠陥・投票キー・相関畳み込み・
    RANKING_ONLY・regime 中立・CVD 判定の共有
  - `tests/test_r37_execution_safety.py` — トレールの対市場価格ガード・timeout 予算・
    分割建玉の modify 拒否（隔離サンドボックスで実際に `order.py` を走らせ、
    外部送信ゼロを確認）
- 403 サイクル再生で、武装候補の SL は中央値 17.0pt / 最大 58.8pt（上限 60pt 内）、
  先頭目標の R は最小 1.51 / 中央値 1.86。幾何の不変条件は維持

## 触っていないもの

- **検出のしきい値**。`classify_amd` が 403 本中 283 本で方向を出すのは緩く見えるが、
  これは相場から測った値ではなく設計判断なので、defect 修正の範囲では変えない。
  1 票として `alignment` に入るだけで、加点は合計 +2 が上限
- 4 モデルの構造検出、実行契約、ULTRA、発注経路そのもの
- カタログ未実装のモデル（QML / OCL / Judas Swing / EQH-EQL / IRL-ERL /
  CRT 8 種のうち 5 種）。これは欠陥修正ではなく新規実装なので別件

## 残っている既知の問題

監査は他にも挙げている。重いものから:

- **MODIFY 後の HALT で現 SL 認識が復元されない** — `state["stop"]` の復元源が
  `MANAGEMENT_SENT` 行だけで、HALT 行は SL を記録しない。トレールが不利方向へ
  張り替わりうる（自動再送そのものは 3 層のガードで塞がれていることを反証パスで確認済み）
- **HALT レコードのキー空間が `ENTRY_RECOVERED` と一致しない** — 解除手段がコードに無い
- **Tradovate の「一覧に無い＝verified FLAT」と `accounts[0]` フォールバック** —
  現行 env では `configured=False` のため未到達だが時限式
- **ボラゲートの単位バグ** — `_sl_caps` の正常系だけ `fixedQty` で割らない。
  保存済みサイクルで停止判定 0 本＝事実上デッドコード
- **`0.40 < ratio <= 0.60` は A+ のみ** を強制するコードが存在しない
- **DST ハードコードが 3 系統** — `order.py` / `autotrade_engine.py` / `dayguard.py`
- **取得手順の二重化** — CLAUDE.md §6.2a（R13 パイプライン）と
  `docs/TV_ACQUISITION_LOOP.md`（R28 直渡し）が矛盾。実測で機能しているのは R28 経路で、
  §6.2a はそれを名指しで禁じている。どちらを正とするか決めて一本化すること

# シナリオ評価表示 設計書(Mini App)— R6

**発行**: 2026-08-18(Fable 5)
**目的**: `msnr_gate` が持つ評価の構造化データ(grade・blocker・連鎖状態・
回転・ボラ予算・ADVISORY)を、Mini App の1画面で見えるようにする。
**背景**: R1〜R5 で評価は機械化されたが、アプリに届くのは watching 末尾の
MSNR 1行だけ。ユーザーは**発注ボタンを押す瞬間に「なぜ武装できているか」を
見られない**。逆に提案が出ない夜は「何が止めているか」が1行に潰れている。

---

## 0. 設計原則

1. **評価は表示するもので、アプリで再計算しない。** 判定の正本は
   `msnr_gate` + CLAUDE.md のゲート群。アプリは受け取った evaluation を
   描画するだけ(アプリ側に判定ロジックを複製しない)
2. **evaluation が無くても全画面が従来どおり動く**(後方互換必須。
   旧バンドル・旧 Worker・旧クライアントのどの組合せでも壊れない)
3. **ADVISORY は武装根拠に見えない見た目にする**(「観測中」ラベル必須)
4. 通知バナー(先頭2行)は**変更しない**

## 1. 全体経路と変更点(3層)

```
監視ループ ──(bundle + evaluation)──> monitor_publish.py ──> Worker/DO ──> Mini App
   [1a] msnr_gate --card で生成        [2] whitelist 追加      [3] 検証+保存   [4] 描画
```

| # | ファイル | 変更 |
|---|---|---|
| 1 | `msnr_gate.py` | `--card` モード新設(§3。evaluation の骨格を機械生成) |
| 2 | `monitor_publish.py` | `normalize_bundle` の任意キー一覧(235-243行)に `"evaluation"` を追加し、market payload に載せる(§4) |
| 3 | `cloudflare/src/state_machine.js` | market 検証に `evaluation` の受理(型・サイズ検証)、scenario 検証に `grade` 1キー追加(§5) |
| 4 | `telegram_mini_app/app.js` + `styles.css` | シナリオカード増強 + ゲートボード + ADVISORY チップ(§6) |

**market ストリームに載せる理由**: シナリオが無い夜(=これまでの大半)こそ
「何が止めているか」を出したい。scenario ストリームはシナリオ存在時しか
流れない。scenario 側には `grade` だけ複製する(ボタン表示用)。

---

## 2. evaluation スキーマ(bundle トップレベル・任意)

```jsonc
"evaluation": {
  "at": "2026-08-18T02:49:30+09:00",       // 必須。bundle.at と同値
  "volGate": {                              // 必須
    "noise": 23.1, "slCap": 25.0,
    "ratio": 0.93, "ruling": "停止"         // "通常" | "A+のみ" | "停止"
  },
  "rotation": { "signals": 5, "negations": 2, "verdict": "OK" },   // 必須
  "msnr": {                                 // 任意(評価対象レベルがある時)
    "label": "Weekly Mid", "price": 30253.0, "tier": 3, "confluence": 2,
    "freshness": "FLIPPED", "side": "SELL",
    "chainType": "FLIP", "chainState": "FLIP_HELD", "barsLeft": 7,
    "allowed": true, "grade": "A+", "blockers": []
  },
  "htf": {                                  // 任意。R66(2026-09-08)以降は build_card が bundle から機械生成
    "ctTrend": -1, "ctAtr": 35.45, "aligned": true   // 15分 CT_TREND の符号 / CT_ATR / decision.side との一致
  },
  "entry": {                                // 任意。decision に entry/stop があるとき build_card が機械生成
    "structure": 30253.0, "airPt": 2.0, "airPct": 13,   // MSNR レベル(無ければ stop)と建値の隙間
    "reachR": 0.7, "pass": true                         // |現値−建値|/リスク(R26 gapR) / decision の武装可否の写し
  },
  "tp": { "bars15": 1.3, "pass": true },    // 任意(REPORT-ONLY の写し)
  "advisory": {                             // 任意
    "vwap": { "side": "BUY", "state": "VWAP_ACCEPTED", "drift": 0.8 },
    "vp":   { "side": "SELL", "state": "VP_ACCEPTED", "target": 29950.0 }
  },
  "summary": "MSNR: … 1行"                  // 必須(既存 summary と同一)
}
```

- **サイズ上限: JSON 直列化で 4096 バイト**。超過時は publisher 側で
  `advisory` → `msnr.blockers` の順に落として縮める(Worker は超過を拒否)
- 数値は有限値のみ。`ruling`/`verdict`/`chainState`/`freshness`/`grade` は
  既存の列挙以外を入れない(Worker が列挙検証する)

## 3. `msnr_gate.py --card`(生成の機械化)

```
python msnr_gate.py --card [--level S --side buy|sell] [--sl-cap 52.5] < bundle
```

- 出力 = §2 の骨格のうち **at / volGate / rotation / msnr / advisory / summary**
  に加え、**R66 以降は `htf` / `entry` も**(`build_card(..., bundle=)` が 15分 study と
  decision から機械生成する。`tp` は引き続き含めない)。
  旧仕様の「監視ループの Claude が追記する」は R51 で転記工程ごと廃止されたため、
  2026-09-08 まで 964 サイクル連続で両行が N/A のまま残っていた
- `volGate.ratio` = noiseFloor ÷ SL上限。**既定 52.5pt**($105 ÷ $2.00)。
  `NQX_SL_CAP_PT` で上書きでき、`--sl-cap` はさらにそれより優先される。
  msnr_gate は RISK_* env を読まない(依存を増やさない)ので、
  **口座上限を変えたら既定値と env の両方を確認すること**
- ★ **強制側との二重管理に注意。** 実際に ARMED を WATCH へ降格させるのは
  `monitor_publish.vol_gate` で、あちらは `.secrets` の `RISK_*` から
  SL 上限を導出する。**ここがずれるとカードの表示と実際の判定が食い違う。**
  2026-08-19 の実害: 既定が $50 時代の 25.0 のまま $60 → $105 の2度の
  引き上げを見逃し、実データ221バンドル中 **192件(86.9%)で ruling が反転**
  していた(カード「停止」160件 / 実際の強制「通常」155件)
- `ruling` の閾値は CLAUDE.md §3 と同一(≤0.40 通常 / ≤0.60 A+のみ / 超 停止)
- `--level` 指定時は該当レベル行を `msnr` に。省略時は「最も進んだ連鎖」の
  レベルを入れる(summary と同じ選択)
- 既存の出力モード(フル / --summary / --level)は**一切変更しない**

監視ループの組み込み(CLAUDE.md §7 に追記する1行):
bundle 書き出し → BOM 除去 → `--card` 実行 → 出力を bundle の
`evaluation` キーへ read-then-write ワンライナーで埋め込み(§7 既存の
watching 追記と同じ型)→ publish。

## 4. monitor_publish.py の変更(最小・2箇所)

1. `normalize_bundle` の任意キータプル(235-243行)に `"evaluation"` を追加
2. `build_market_payload` の返り値に `evaluation` を透過で載せる
   (検証はしない — 正本の検証は Worker。ここで落とすのは §2 のサイズ上限のみ)

**制約**: `tests/run_all.py` の9ファイル、特に `test_market_feed` /
`test_oneclick` の純度検査を壊さない。evaluation 無しバンドルの出力が
現行とバイト単位で同一であること(新規テストで保証)。

## 5. Worker(cloudflare/)の変更

`validateScenario`(state_machine.js 277-297行)は**ホワイトリスト複製**なので、
何もしなければ evaluation はサーバーで剥がれる。変更は2点:

1. **market ストリーム**: market payload 検証に `evaluation` を追加。
   型検証(§2 の必須キーと列挙・数値有限性・直列化 4096 バイト上限)。
   不正な evaluation は **market 全体を拒否せず evaluation だけ落とす**
   (評価の欠損で価格ストリームを止めない。理由を state の note に残す)
2. **scenario ストリーム**: ホワイトリストに `grade`(`"A+"|"A"|null`)を追加

- テスト: `cloudflare/test/state_machine.test.mjs` に受理/剥落/上限超過の
  3ケース、`integration.test.mjs` に E2E 1ケース
- デプロイ: `cd cloudflare && npx wrangler deploy`(ユーザー環境で実行)

## 6. Mini App の表示設計

### 6a. シナリオカード増強(app.js `scenarioCard` 272行〜)

既存カードの ENTRY/SL/TP 行の直下に**ゲートチェックリスト**(5行固定):

```
◇ ボラ予算   0.48 · A+のみ帯          ✓
◇ 回転       2/5 OK                    ✓
◇ MSNR       FLIP_HELD · 鮮度残7本     ✓
◇ 15分整合   CT −1 · 方向一致          ✓
◇ Entry配置  空気幅13% · 到達0.7R      ✓
```

- 各行 = ラベル + 実測値 + ✓/✗。✗ の行は `--halt` 色 + blocker コードを
  そのまま表示(翻訳しない — コードが正本)
- **ARMED ボタンに grade バッジ**: `発注 [A+]`(scenario.grade が無い旧
  データでは従来表示)。grade=A かつ ruling="A+のみ" の矛盾状態は
  ボタンを出さず警告(サーバー側は既に WATCH のはずだが二重防御)
- WATCH カードでは ✗ 行が「昇格に足りないもの」の一覧になる

### 6b. ゲートボード(シナリオ無し時。watching リストの上に配置)

`evaluation` があれば、シナリオカードの代わりに現況ボードを出す:

```
GATES ─ 02:49
ボラ予算  ██████████░ 0.93  停止
回転      1/6 OK
MSNR      CT Hard Stop 30115 · FLIP受容·戻り待ち(残14本)
ADVISORY  VWAP: 無効(drift 3.3pt) · VP: —
```

- 1行目のバーは ratio の視覚化(0.4 / 0.6 に目盛り)。ruling で色分け
  (通常=--pass / A+のみ=--signal / 停止=--halt)
- ADVISORY 行は**必ず「観測中 · 武装根拠外」のマイクロラベル**を添える

### 6c. 実装様式

- 新規コンポーネント関数 `evalChecklist(evaluation, scenario)` と
  `gateBoard(evaluation)` を app.js に追加(既存の `scenarioCard` /
  `state-card` の class 体系と `escapeHtml` を踏襲)
- styles.css: `.gate-row`, `.gate-pass`, `.gate-halt`, `.grade-badge`,
  `.advisory-chip`(既存のダーク端末調に合わせる。新色は導入しない —
  既存の accent/stop 系トークンを再利用)
- テスト: `telegram_mini_app/test/evalcard.test.mjs`
  (evaluation 有/無/不正の3系統でレンダリングが例外を出さないこと)
- デプロイ: `cd telegram_mini_app && npm run build &&
  npx wrangler pages deploy dist --project-name=nqx-nightwatch`

## 7. 段階導入(それぞれ独立にデプロイ可能な順)

| Phase | 内容 | 依存 |
|---|---|---|
| P1 | §3 `--card` + §4 publish 透過 + §5 Worker 受理 | なし(表示前でもデータが正本に貯まり始める) |
| P2 | §6a カード増強 + grade バッジ | P1 |
| P3 | §6b ゲートボード + ADVISORY チップ | P1 |

各 Phase の完了ごとに HANDOFF に記録。P2/P3 は同時でもよい。

## 8. 検収基準

```
[ ] evaluation 無しバンドル: publish 出力・アプリ表示とも現行と同一(後方互換)
[ ] --card 出力が §2 スキーマに適合(新テスト。列挙・サイズ・数値有限性)
[ ] tests/run_all.py ALL PASS(10ファイル+新規)/ cloudflare の node テスト PASS
[ ] 不正 evaluation は market を止めず evaluation だけ落ちる(Worker テスト)
[ ] 4096 バイト超の evaluation が publisher 側で縮められて通る
[ ] アプリ: ARMED カードに grade / WATCH カードに ✗ 行 / シナリオ無し時に
    ゲートボード、の3状態をスクリーンショットで確認
[ ] ADVISORY 表示に「観測中 · 武装根拠外」ラベルが常に付く
[ ] 通知バナー(先頭2行)のレイアウトが変わっていない
[ ] order.py / telegram_bot.py / nqx_state.py 無変更
```

## 9. 罠(このリポジトリで実在するものだけ)

1. **Worker はホワイトリスト複製** — クライアントだけ直しても何も出ない。
   §5 を先にやる(Phase P1 が最初なのはこのため)
2. **monitor_publish のテスト純度**(test_market_feed / test_oneclick)。
   evaluation 追加は「キーが無ければ完全に従来どおり」で書く
3. Write ツールの JSON は BOM 付き。`--card` の出力を bundle に埋める
   ワンライナーは**必ず read-then-write**(§7 の既存ワンライナーを流用)
4. Mini App のデプロイはユーザーの wrangler 認証が要る。ビルドまで済ませ、
   deploy コマンドは提示して実行可否をユーザーに確認する
5. スキル・CLAUDE.md を触った場合は project/ 複製との SHA 一致を確認する
6. DO の resultLog(LEDGER)には触れない — evaluation は market/scenario
   ストリームのみ

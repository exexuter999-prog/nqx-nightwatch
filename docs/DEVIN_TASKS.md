# DEVIN_TASKS.md — Devin に投げる nightwatch 強化タスク集

作成: 2026-09-15。R91(fill_watch 自動起動)までが実装済みの時点で書いた。
番号は **R92〜R101 を Devin 用に予約**する(別セッションが同じ番号を取ったら次の空きへずらす)。
R102 は限月ロール(2026-09-15)に使われたので、09-16 追加のタスク K は **R103**。

Devin は「GitHub 上のリポジトリで、テストを回しながら PR を作る」道具である。
nightwatch の弱点は **(1) 実測 N が小さい(決済 42 件、11 件は帰属不明)、(2) 事故は全部本番で
見つけている(R70〜R91 はほぼ全部が実弾の事故起点)、(3) テストが本番の台帳・CrossTrade・Worker に
落ちる** の 3 つで、どれも「本番に触らない場所で長時間しつこく回す」作業が効く。Devin に向いている。

逆に Devin に **できないこと**: TradingView Desktop・CrossTrade・Telegram・Cloudflare の deploy。
この 4 つに触る手順は全部【人】と印を付けた。

---

## 0. 先に人がやること(Devin はここから先しか見えない)

### 0.1 git 化する(2026-09-15 完了: private リポ `github.com/exexuter999-prog/nqx-nightwatch` へ push 済み)

Devin は GitHub リポジトリを前提にする。**`.secrets/` は絶対に入れない**(CrossTrade キー・bot token・
台帳・監査コピー 116MB)。ルートの `.gitignore` が正本で、`.secrets/`・`*.env`・`node_modules/`・
`dist/`・ルートのランタイム JSON・`animated-web/`(90MB)・`archive/`・`artifacts/`・`nqx-upload/`・
`preview/`・`*.bak*`・`cloudflare/.wrangler/`(miniflare のローカル DO 状態 44MB)・
`.claude/settings.local.json` を除外している。除外後の追跡対象は 703 ファイル・約 103MB
(うち `telegram_mini_app/public` のステッカー GIF が大半)。

リモートは `origin`(master)。本番ツリーから追加の変更を送るときは PowerShell 5.1(`&&` は使えない)で:

```powershell
cd "C:\Users\exexu\Downloads\nq-nightwatch-claude-code-handoff"
git add .
git status --short | Select-String "secrets"   # 何も出ないことを確認してから commit
git commit -m "<変更内容>"
git push
```

2026-09-15 ユーザー決定で push 前に次を済ませた:
- **口座 ID は全部伏せ字に置換済み**(CrossTrade 口座名は `LFF00000000000006` のように接頭辞と桁数を
  保った 0 埋め、Tradovate の数値 accountId は `9000xxxx`)。対応表は `TRADING_CONTEXT.md` 冒頭。
  実 ID の正本は `.secrets/crosstrade.env`。**Devin は伏せ字しか見ない**ので、tests の固定データも
  伏せ字のままで動く。R92 の gitleaks カスタムルールは「実 ID の形」= `LF[EF]0[1-9]\d{12}` /
  `LTATANOBA10[1-9]\d{11}` / `6\d{7}`(accountId)を検出対象にし、0 埋めの伏せ字は当たらないようにする。
- `cloudflare/wrangler.toml` の `NQX_ALLOWED_USER_ID`(Telegram 数値 user id)と `NQX_AUTOTRADE_ACCOUNTS`
  (口座 CSV)は `[vars]` から **wrangler secret へ移した**。Worker のコードは `env.<名前>` で読むだけなので
  無変更。投入は `python setup_cloudflare.py --sync-secrets`、ローカル dev 用は `cloudflare/.dev.vars`
  (git 管理外)。**deploy と secret 投入は運用者**(手順は §0.4)。
- `grep` 済み: API キー本体・bot token・`sk_` 系トークンは追跡対象に無い(2026-09-15 確認)。

### 0.4 Worker 側の secret 移行(【人】1 回だけ。2026-09-15 に実施済み。`wrangler secret list` で 2 つを確認、認証付き `/api/state` 正常)

同じ名前を var と secret の両方に置けないので、**deploy(var を外す)→ secret 投入**の順になる。
その間(数十秒)は Worker の認証が `NQX_ALLOWED_USER_ID is not configured` で 500 を返すので、
監視窓の外(JST 05:45〜07:00)か建玉が無いときに、次を続けて叩く(PowerShell 5.1):

```powershell
cd "C:\Users\exexu\Downloads\nq-nightwatch-claude-code-handoff\cloudflare"
npx wrangler deploy
cd ..
python setup_cloudflare.py --sync-secrets
python nqx_state.py --check
```

`--sync-secrets` は `.secrets/telegram.env` の `TELEGRAM_CHAT_ID` と `.secrets/crosstrade.env` の
`CROSSTRADE_ACCOUNTS` を投入する(値は表示しない)。以後、口座を入れ替えたときの Worker 側の作業は
`wrangler.toml` の編集 + deploy ではなく **`--sync-secrets` 1 回**になる。

### 0.2 Devin の Knowledge に §1 を貼る

§1 は全タスク共通の禁止事項。Devin のセッションごとに繰り返し書かなくてよいように
Knowledge(常時読み込まれる文)へ登録する。各タスクのプロンプトは §1 が読まれている前提で短くしてある。

登録場所(Devin 公式ドキュメント、2026-09-15 確認): Devin の Web アプリで **Settings → Resources →
Knowledge** を開き、右上の **Create knowledge**。項目は次のとおり。
- **Trigger Description**(必須。Devin はこれを見て思い出すかを決める): 「nqx-nightwatch リポジトリでの
  すべての作業。コマンド実行・テスト・PR 作成の前に必ず読む」のように「常に」を明示する。
- **Content**: §1 の ```text``` ブロックの中身をそのまま。
- **Pinned repos**: `exexuter999-prog/nqx-nightwatch` を選ぶ(そのリポのセッションで必ず読まれる)。
- Folder / Macro(`!name` で呼べる短縮名)は任意。

**注意: Devin はリポジトリの `CLAUDE.md` と `AGENTS.md` を自動で Knowledge に取り込む。** どちらも
監視 PC の運用契約で、本番の発注コマンドが手順として書かれている。そのため両ファイルの冒頭に
「外部エージェントはこの手順を実行しない」という前置きを入れた(2026-09-15)。Knowledge の自動生成分は
Settings → Resources → Knowledge に並ぶので、最初のセッションの前に一度眺めて、運用手順が
「やること」として取り込まれていたら無効化する。

### 0.3 監査コーパスの匿名化エクスポート(タスク B の前提)

`.secrets/monitor_cycle_*.json`(1,228 本)は Devin に見せられない場所にある。タスク B-0 で Devin に
エクスポータを書かせ、**人が本番 PC で 1 回叩いて**匿名化コーパスをコミットする。順序は §3 参照。

---

## 1. 全タスク共通ガードレール(Devin Knowledge に登録する文)

```text
このリポジトリは MNQ 先物の自律発注システム(nightwatch)で、本番はこのコードを 3 分ごとに
そのまま実行している。あなた(Devin)の作業は全部「本番に触らない場所」で行う。

■ 絶対に実行しないコマンド(引数を変えても不可)
  python order.py(--status 以外)/ python autotrade_engine.py / python nqx_cycle.py /
  python tv_fetch.py / python tv_snapshot.py / python telegram_bot.py / python fill_watch.py /
  python monitor_publish.py / python nqx_state.py(--check 含む)/ python autotrade_arm.py /
  npm run deploy / wrangler deploy / wrangler tail / curl や HTTP で crosstrade・workers.dev・
  api.telegram.org に触ること。
  理由: 実弾の発注・Cloudflare Durable Object の読み取り枠・Telegram 通知に直結する。
■ .secrets/ を作らない・読まない・コミットしない。crosstrade.env の形式を真似た値も書かない。
  秘密っぽい文字列(API key / token / 口座 ID の新規追加)が diff に入ったら PR を出さず止めて報告する。
■ テストの走らせ方は python tests/run_all.py だけ(1 ファイル 1 プロセス。pytest / unittest discover は
  スタブ漏れで誤検知するので使わない)。テストは自走式スクリプト(末尾で sys.exit)。
  環境変数 PYTHONUTF8=1 PYTHONIOENCODING=utf-8 を付ける。Worker は cd cloudflare; npm test、
  Mini App は cd telegram_mini_app; npm run ci(test + build + verify:build)。
■ 新しいテストは台帳・ロック・照会・Worker を全部 tempdir/注入スタブに置く。
  broker_status.query_* / autotrade_arm.state / nqx_state.claim_* / nqx_state.fetch_state_quiet に
  「到達 0 件」の tripwire を仕込む。本番パスに落ちるテストは不合格。
■ 振る舞いを変える変更は必ず execution_contract.json のスイッチの裏に置き、既定値は OFF または
  SHADOW(記録だけ)にする。LIVE に倒すのは人。スイッチには "_note"(何を・なぜ・戻し方)を付ける。
  既存の例: entryDepth.mode / stopLogic.*.mode / modelGate.disabled / pyramid.dryRun。
■ 変えてはいけないもの: order.py が CrossTrade へ送るペイロード(command=cancelandbracket 等)、
  Python と Worker で共有している契約値(execution_contract.json の既存キーの意味・
  brokerObservation.maxComponentSkewSec 等)、docs/MONITOR_LOOP_PROMPT.md、CLAUDE.md §6 の手順。
  Worker(cloudflare/src)を変えたときは PR に「deploy が必要」と明記(deploy は人が叩く)。
■ 欠けた情報を推測で埋めない。データが無ければ「無い」と出す(このリポの一貫した規律。
  例: 決済の脚が判定できなければ保留、口座の規約が不明なら空欄)。
■ 1 PR = 1 タスク。docs/R<番号>_<名前>.md に「目的 / 何が変わる / 戻し方 / 検証コマンド」を日本語で書く。
  PR 本文も日本語で、冒頭に「運用者に何が変わるか」を 3 行以内で。
■ ファイルは UTF-8(BOM なし)。Python の open() には必ず encoding="utf-8"。
  コード内コメント・docstring は既存にならい日本語でよい。
■ 触らないタスク: 多口座直接発注(R87 direct route)は別セッションの作業ツリーで進行中。
  TradingView 取得(tv_fetch / tv_snapshot / Pine)は実機が要る。これらは指示があっても着手しない。
```

---

## 2. タスク一覧(優先順)

| ID | 名前 | 狙い | 本番への危険 | Devin 単独で完結 |
|---|---|---|---|---|
| A | R92 隔離 CI を緑にする | 以後の全作業の土台。テストが本番へ落ちる穴を塞ぐ | なし | ○ |
| B | R93 Replay Lab(監査コーパス 1,228 本の再生) | N 不足の唯一の代替。B 等級・TURTLE 停止・killzone の実証 | なし(読むだけ) | B-0 のあと【人】1 回 |
| C | R94 nqx_doctor(発注が止まる疑いどころの一発診断) | 沈黙して止まる事故を数分で切る | なし(照会 1 回) | ○ |
| D | R87 後半 Mini App 手動 HALT スイッチ | Worker 側は deploy 済み・UI だけ未実装 | なし(deploy は人) | ○ |
| E | R95 契約スイッチ台帳と AGENTS.md の二重管理解消 | スイッチが増えすぎた。文書の食い違いを機械で検出 | なし | ○ |
| F | R96 撤退ポリシー(09-14 決定・未実装) | KILL/FLATTEN の HALT が新規を何日も塞ぐ問題の根治 | **高**(engine) | ○(既定 OFF。LIVE は人) |
| G | R97 状態機械の性質テスト(fuzz)+ Python/Worker 共通ゴールデン | 事故を本番で見つける前に見つける | なし | ○ |
| H | R98 裸 runner 脅威モデル(レッドチーム読解) | 建玉が保護ゼロ/管理外になる経路の全列挙 | なし | ○ |
| I | R99 20 口座 90 日プランのモンテカルロ | 面白い枠。相関 1 の 20 口座で何が起きるかを数字に | なし | 規約の数値は【人】 |
| J | R100 Worker の rows read 予算 | DO 無料枠 1101 で AUTO が丸一日死ぬ再発防止 | なし(deploy は人) | ○ |
| **K** | **R103 流動性狩り対策(計測 → プール検出 → 再生)** | 直近の損切りは方向が合っていて SL の位置が狩られている。SL 側の流動性判定がコードに無い | なし(K-0/K-1 は既定 SHADOW・記録だけ。LIVE は人) | K-0 / K-1 は fixture で完結、K-2 は B-0 のあと |

推奨順序: **K-0 → K-1(fixture だけで着手可。2026-09-16 ユーザー相談で最優先)→ A → B-0【人】→
K-2 / B-1 / C / D / E(並行可)→ G / H → F(A が緑で、H を読んだ後)→ I / J**。

---

## 3. 各タスク

### A. R92 — 隔離 CI を緑にする

**背景**: 2026-09-14 に `test_r82` が本番の `autotrade_ledger.jsonl.lock` を掴み、1 口座経路の
テストが CrossTrade へ HTTP を出した。engine テストは「外部ゼロ」を名乗っていても注入漏れで落ちる。
Devin の全作業はテストに依存するので、まず「クリーンな checkout(.secrets なし・ネットなし)で
3 系統のテストが緑」を作る。

**入口**: `tests/run_all.py`(1 ファイル 1 プロセスの理由が docstring にある)、`tests/_hermetic.py`
(order.py 隔離の既存部品)、`cloudflare/package.json`、`telegram_mini_app/package.json`。

**貼り付け用プロンプト**:

```text
タスク R92: このリポの 3 系統のテスト(python tests/run_all.py / cd cloudflare && npm test /
cd telegram_mini_app && npm run ci)を、.secrets/ が無く外部ネットワークも無いクリーンな環境で
全部緑にし、GitHub Actions(.github/workflows/ci.yml、ubuntu + windows の 2 ジョブ)で回す。

やること:
1. tests/_netguard.py を新設: sitecustomize として PYTHONPATH 経由で子プロセスにも効く形で、
   同一プロセス内で bind したループバック以外の socket.connect と外部 DNS 解決を拒否し、
   試みた呼び出し元(スタック先頭 3 フレーム)を tests/_netguard.log に記録する。
   run_all.py はこれを有効にして走らせ、ログに 1 件でも到達があれば FAIL にする。
2. .secrets/ が無いことで落ちるテストを直す。直し方は「テスト側で tempdir に台帳・env を作る/
   スタブを注入する」だけ。本番コード側に「テスト時は読み飛ばす」分岐を足してはいけない。
   本番コードに注入口(引数)が無くて直せない箇所は、引数を追加する最小変更を別コミットにし、
   PR 本文に列挙する。
3. 各テストが本番パス(.secrets/autotrade_ledger.jsonl、crosstrade.env、Worker URL)を参照しない
   ことを tripwire で検査する共通ヘルパを tests/_hermetic.py に足す。
4. gitleaks(または同等)で秘密検出を CI に入れる。口座 ID の形(LF[EF]\d{14}、LTA[A-Z0-9]+)も
   カスタムルールで検出し、既存ファイルの既知の出現は allowlist にする。
5. docs/R92_HERMETIC_CI.md に「なぜ 1 ファイル 1 プロセスか / netguard の仕組み / 落ちていた
   テストと直した理由の一覧 / ローカルで同じ隔離で回すコマンド」を書く。

完了条件: CI 緑、netguard ログ 0 件、本番コードの差分が「注入口の追加」以外に無いこと。
やらないこと: テストのスキップ・削除で緑にすること。既存テストの assert を弱めること。
```

### B. R93 — Replay Lab(監査コーパスの再生基盤)

**背景**: モデル別 PF の正本は自前実測だが、決済は 42 件(うち 11 件が UNATTRIBUTED)。
BLUEPRINT §0 の PF≥1.5 / N≥200 / OOS PF≥1.3 には何年もかかる。一方、監査コピー
`monitor_cycle_HHMM.json` は 1,228 本あり、`replay_stop_logic.py`(R90)と `entry_depth.py --report`
(R86)は既にこれを現行 `msnr_gate.evaluate()` で再評価して方針の差を出している。
ただし約定規則が 2 か所に別々に書かれ、R90 で「セットアップ単位だと 4 倍数える罠」も分かった。
これを 1 つの基盤にまとめ、**契約スイッチの ON/OFF を全部同じ規則で比較できる**ようにする。

答えたい問い(EDGE_LEDGER.md / CLAUDE.md にある未測定項目):
- B 等級(2026-09-04 に発注可へ)にエッジはあるか。A+/A だけと比べて逐次で何が変わるか
- TURTLE_SOUP_REVERSAL を ALL で外した(R89)判断は逐次で正しいか。RESTING_LIMIT だけ外すのはどうか
- killzone / DOL(HRLR) の減点は実際に効いているか(減点を 0 にした変種と比較)
- ボラゲート 0.60 の閾値感度(0.50 / 0.60 / 0.70)
- R86 entryDepth と R90 stopLogic を同時に LIVE にした合成の成績(個別には測ったが合成は未証明)

**B-0(Devin)→【人】**: エクスポータを書く → 人が本番で 1 回叩く → `corpus/` をコミット。

```text
タスク R93-0: 監査コピーの匿名化エクスポータ replay_corpus.py を書く。

入力: <dir>/monitor_cycle_*.json(既定 .secrets/ 。私の環境にはこのディレクトリは無いので、
tests/fixtures に形の同じ小さなサンプルを 3 本自作して開発する。形は
replay_stop_logic.py evaluate_all() と entry_depth.py load_setups() が読んでいるキーから逆算する:
トップレベル at / price / priceAt / sourceSymbol / snapshot(bars3m, levels, htfContext, vwap, cvd*,
po3, smtObservation, eventGate, sessionId) / evaluation(decision, volGate, sessionGate, cvdGate,
dataGate, stopBudget) / scenarios / cvdMeta / acquisitionReceipt)。

出力: corpus/<JST日付>.jsonl.gz(1 行 1 サイクル)+ corpus/INDEX.json(本数・期間・SHA-256)。
匿名化: accountId / accountScope / excludedAccounts / riskCapSource 周辺の口座名、および
正規表現 LF[EF]\d{14} と LTA[A-Z0-9]{10,} に当たる文字列をすべて "ACCT_<連番>" に置換する。
置換前後で JSON の構造(キーの集合・配列長)が変わらないことをテストで保証する。
サイズが 50MB を超えるなら Git LFS を使う指示を README に書く。

完了条件: python replay_corpus.py --export <in> <out> が冪等(2 回叩いて同じ SHA)。
--verify <out> で口座 ID 残存 0 件を検査して exit 0/1 を返す。
私は .secrets を持たないので、本番での実行は運用者が行う。PR 本文に運用者が叩く
1 行コマンドを書く。
```

【人】が叩く(監視窓の外か、ループを止めているときに):

```powershell
python replay_corpus.py --export .secrets corpus
python replay_corpus.py --verify corpus
git add corpus; git commit -m "R93 corpus export"
```

**B-1(Devin)**:

```text
タスク R93-1: Replay Lab。corpus/ の全サイクルを現行コードで再評価し、契約スイッチの変種ごとの
成績を同じ約定規則で比較する python replay_lab.py を作る。

土台: replay_stop_logic.py(evaluate_all / setups_for / simulate_setup / _bootstrap)と
entry_depth.py(simulate / load_setups / _first_of_cluster)に同じ約定規則が 2 回書かれている。
これを replay_rules.py に 1 つにまとめる(確定 3 分足のみ・指値は 1 tick 突き抜けで約定・
約定前に TP1 到達なら取消・同じ足で SL と TP が両方触れたら SL 優先・指値待ち 30 分・
最長 6 時間・R は計画の SL 幅で割る)。既存 2 ファイルはこれを import する形に置き換え、
既存の --report 出力が変わらないことをゴールデンテストで保証する。

集計は 2 つの見方を必ず両方出し、結論は「逐次」で語る:
  (a) 塊の先頭(同じ scenarioId/decisionId の連続周期は 1 件)= 従来の見方
  (b) 逐次(1 建玉ずつ。保有中に出た候補は建てない)= 実運用と同じ数え方
変種は execution_contract.json の差分として与える(--variant '{"scenario":{"allowedGrades":["A+","A"]}}'
のように)。msnr_gate は contract を関数で読むので、replay_stop_logic.py が
msnr_gate.stop_logic_policy を差し替えているのと同じ方法で注入し、本物の
execution_contract.json は絶対に書き換えない。

最初に回す変種(すべて BASE=現行契約との比較):
  1. allowedGrades ["A+","A"] vs 現行(B 含む)
  2. modelGate.disabled=[] vs 現行(TURTLE ALL)vs [{TURTLE, RESTING_LIMIT}]
  3. killzone / DOL 減点を 0 にしたもの(msnr_gate に減点係数の注入口が無ければ、
     関数差し替えで 0 にする。本番コードは変えない)
  4. NQX_VOL_GATE_APLUS 相当の閾値 0.50 / 0.60 / 0.70
  5. entryDepth.mode と stopLogic の全 LIVE / 全 OFF / 現行

出力: docs/reports/REPLAY_<日付>.md。各変種に N / 勝率 / 粗 PF / 平均 R / 合計 R /
ブートストラップ 95% 区間(既存 _bootstrap を再利用)/ 期間前半 70% と後半 30% の分割。
表の直後に「N≥200 未満の行では PF を結論にしない」と機械的に注記する。
時間軸の分割で結果が反転する変種は「不安定」と印を付ける。

やらないこと: 契約の値を変える PR を出すこと(結論は運用者の判断材料)。3 分足の中の順序を
仮定して楽観側に約定させること(EDGE_LEDGER.md M6 の罠)。
```

### C. R94 — nqx_doctor(「発注が止まる」の一発診断)

**背景**: 発注が沈黙して止まる事故が 3 系統ある。1 位 Worker に残った ENTRY claim
(2026-08-31、3 日気付かず)、2 位 台帳の KILL/FLATTEN HALT(09-01→09-04)、3 位 古い凍結注文 ID で
注文照会が恒久 UNVERIFIED(09-07、28 サイクル)。さらに DO 無料枠 1101(09-10、health は通るので
`--check` が当てにならない)、口座の消失(3 回)、manualHalt、AUTO 期限切れ、fill_watch 停止。
確認順は人の記憶にしかない。

```text
タスク R94: nqx_doctor.py を作る。「新規 ENTRY が出ない」ときに 1 回叩けば疑いどころを全部
上から順に判定し、1 画面と JSON(--json)で出す。読むだけで、状態は一切変えない。

判定項目(それぞれ OK / WARN / BLOCK と 1 行の理由、対処コマンドを出す):
 1. Worker の entryClaim: nqx_state.fetch_state_quiet() の entryClaim が CLAIMED/CONSUMED で
    残っていないか。routeState、acceptedCount、accountScope に今の口座が入っているか。
 2. 台帳の HALT: autotrade_engine._has_halt と同じ規則で未解除 HALT を列挙(--list-halts の
    ロジックを関数として再利用する。二重実装しない)。
 3. 凍結プランの注文 ID: 終端していない凍結プランの routeSnapshot にある orderId を
    broker_status.query_orders(symbol, known_order_ids=...) と引数なしの両方で照会し、
    片方だけ verified なら R53 型と判定。
 4. 口座名簿: CROSSTRADE_ACCOUNTS の口座が broker_status の一覧に全部あるか(missing / unknown)。
 5. manualHalt: execution_contract.json の manualHalt.autotrade と
    .secrets/manual_halt_remote.json の写し、どちらが True か。
 6. AUTO: autotrade_arm の状態と期限。環境変数 NQX_AUTOTRADE / NQX_AUTOTRADE_KILL の上書き有無。
 7. fill_watch: heartbeat の年齢と pid の生死。
 8. Worker 健全性: 認証付き GET /api/state を 1 回だけ叩き、500/1101 なら DO 枠切れの疑いと
    「wrangler tail で確認」を出す。/api/health は判定に使わない(通るので)。
    Durable Object の読み取り枠を消費するので、この 1 回以外に Worker を叩かない。
 9. 取得鮮度: acquisitionReceipt の各 raw の更新時刻が 240 秒以内か。
10. 契約の妥当性: msnr_gate.model_gate_rules()['invalid'] が空か、
    stop_logic.py --policy 相当の各節が有効か。
11. 監視窓: 今が JST 07:00〜05:45 の窓内か。

実装上の制約: 私の環境では 1〜9 の照会は実行できない。照会関数はすべて引数で注入できる形にし、
tests/test_r94_doctor.py で「3 系統の事故それぞれを再現した固定データ」を流して判定が出ることを
検証する(memory 相当の記録は docs/ と tests/test_r52_halt_clear.py、tests/test_r53_unresolvable_order.py、
cloudflare/test/r36_stale_claim_release.test.mjs にある)。
本番での実行は運用者が行う。標準出力に口座 ID・キーを出さない(末尾 4 桁だけ)。
docs/R94_DOCTOR.md に判定表と対処コマンドの対応を書く。
```

### D. R87 後半 — Mini App の手動 HALT スイッチ

**背景**: Worker の `POST /api/manualHalt`(認証 + CSRF、DO の `manualHalt`、期限なし)は
2026-09-14 に deploy 済みで、監視 PC 側の同期(`autotrade_arm.sync_manual_halt` →
`.secrets/manual_halt_remote.json`)も済み。**設定ページのスイッチ UI だけ未実装**。

```text
タスク R87-UI: telegram_mini_app の SYSTEM タブに「手動 HALT」スイッチを実装する。

参照: state_client.js の /api/autotrade 呼び出し(認証ヘッダと CSRF の付け方はこれと同じ)、
cloudflare/src/index.js の /api/manualHalt(POST、body 上限あり)、
cloudflare/src/nightwatch_do.js の buildManualHalt(応答 { ok, manualHalt, view })、
cloudflare/test/r87_manual_halt.test.mjs(受け付ける body の形)、system.js(SYSTEM タブは
純関数 deriveRoute / renderSystemView で描く規律。DOM を直接触る例外を増やさない)。

仕様:
- 現在値は /api/state の view.manualHalt から描く(WS の差分でも更新)。ON の間は SYSTEM タブの
  engine ノードを DOWN 相当の色にし、「新規・追撃・建玉管理が止まっている。建玉はブローカー OCO
  だけが守る」と 1 行出す。
- ON にする操作は 2 段(タップ → 3 秒以内にもう一度タップで確定。R57 の音の規律に合わせ、確定時
  だけ 1 音)。OFF にする操作も同じ 2 段。
- 失敗(401/403/413/5xx)は表示を変えず理由を 1 行出す。楽観更新はしない(サーバの応答で描く)。
- Telegram WebView のメモリ予算(docs/R75_BOOT_TIER_MID.md、boot_guard.js の tier)を壊さない:
  新しい依存・大きい canvas・遅延チャンクから main の import を足さない。
  npm run ci の verify:build(サイズ予算)が通ること。
- test/system.test.mjs と test/settings.test.mjs に、ON/OFF/失敗/2 段確認のタイムアウトの
  ケースを足す。

完了条件: npm run ci 緑。deploy と実機確認は運用者。PR 本文に「Worker 側の変更なし、
Mini App の再ビルドと配置だけ」と明記する。
```

### E. R95 — 契約スイッチ台帳と AGENTS.md の二重管理解消

**背景**: `execution_contract.json` に R82〜R91 でスイッチが 10 個以上増え、それぞれの根拠が
`_note` / docs / tests に散っている。`AGENTS.md`(Codex 用)は CLAUDE.md の古い写しで、
**2026-09-04 の「B も発注可」が反映されておらず**、R82〜R91 の節も無い。契約ファイル自身が
正本だと CLAUDE.md が言っているのだから、文書は契約から生成すればよい。

```text
タスク R95: 契約スイッチ台帳の自動生成と、AGENTS.md の二重管理を終わらせる。

1. contract_switchboard.py: execution_contract.json を歩き、「mode / enabled / dryRun /
   disabled / autotrade / allowedGrades / manualHalt」など振る舞いを切り替えるキーを全部拾い、
   1 行 1 スイッチの表(JSON パス、現在値、取り得る値、_note の要約、_decisionNote の日付、
   docs/R*.md への参照、そのキー名を参照しているテストファイル)を Markdown で
   docs/CONTRACT_SWITCHBOARD.md に出す。--check で「_note が無い」「docs 参照が無い」
   「参照するテストが 0 件」のスイッチを列挙して exit 1。
   tests/test_r95_switchboard.py で --check を回し、現状の未整備スイッチは allowlist に入れて
   PR 本文に列挙する(直すのは別 PR)。
2. AGENTS.md: 冒頭の「外部エージェントへの前置き」ブロック(2026-09-15 に追加。Devin が AGENTS.md を
   自動で Knowledge に取り込むための安全弁なので**必ず残す**)の直後に「運用契約の正本は CLAUDE.md。
   以下は CLAUDE.md と同一でなければならない」と宣言し、本文は CLAUDE.md(その冒頭の注記を除く)と
   byte 同一にする。tests/test_r95_agents_sync.py で宣言部以降の一致を検査する。
3. CLAUDE.md の中で契約値を直書きしている箇所(allowedGrades、pyramid の enabled/dryRun、
   entryDepth.mode、modelGate、stopLogic の各 mode、fillWatch.autostart)を拾い、
   契約ファイルの現在値と食い違っていれば --check で WARN に出す(CLAUDE.md は直さない。
   食い違いを PR 本文に列挙する)。

やらないこと: 契約の値を変える。CLAUDE.md の手順を書き換える。
```

### F. R96 — 撤退ポリシー(2026-09-14 ユーザー決定・未実装)

**背景**: KILL / FLATTEN の HALT は「送信結果不明」の fail-safe だが、flatten 経路は identity を
束縛できないので **HTTP 200 でも必ず HALT** になり、人の `--clear-halt` 待ちで新規を何日も塞いだ
(09-01→09-04)。ユーザーは 09-14 に次を決めたが、監視ループ再開のため実装は中断した。
memory では R86 と呼んでいたが番号は押し目深度に使われたので **R96** とする。

決定内容(変えない):
1. `manualHalt.autotrade=true` のときは **KILL の全決済も含めて engine は何も送らない**。人が叩く
   `order.py --flatten` は通す。09-14 に入れた `_kill_exit_switches`(halt 中 KILL を通す例外)は撤去。
2. 「異常」は**決済・取消を送ったのに確認できなかったときだけ**(KILL / 構造 SL・最終 TP の管理決済 /
   R78 修復決済 / 未約定 ENTRY 取消)。ENTRY 送信不明や SL 張り替え失敗の HALT では建玉に触らない。
3. 未確認の撤退は同じ建玉世代へ **3 分おきに再送**(fill_watch から数秒で重ねない)。
4. ブローカーで FLAT + 注文非 blocking を確認したら HALT を自動解除して監視とオートを続行。
5. KILL の全決済が確認できたら **KILL も engine が自動で降ろす**(一度きりの全決済ボタンになる。
   止め続けるのは manualHalt)。降ろせるのは武装台帳の KILL だけで、環境変数 / crosstrade.env は注記。

設計メモ(09-14 のもの): 解除行 `FLATTEN_CONFIRMED` を `HALT_CLEARING_STATUSES` に追加、HALT 行に
accountId、旧 KILL 行は key の PG 世代から口座を引く、FLAT 判定は R52 の一過性 FLAT 検査を通した後だけ。
別件発見: Worker 設定済みで Mini App AUTO が OFF の間、武装台帳の KILL が `_switch`(valid 必須)で
無視される。これも同じスイッチの裏で直す。

```text
タスク R96: 撤退ポリシーを autotrade_engine.py に実装する。決定 1〜5 と設計メモは
docs/DEVIN_TASKS.md §3-F に書いてあり、これを変えない。

進め方(この順で、各段が独立に安全であること):
 a. execution_contract.json に exitPolicy 節を足す(version、_note、mode: "OFF"、
    retryIntervalSec: 180、autoClearOnFlat、autoLowerKill、haltOnlyUnconfirmedExits)。
    mode が OFF の間は今の挙動と byte 同一(ゴールデンテストで固定)。
 b. 未使用ヘルパを先に入れる(HALT 行への accountId 付与、KILL key の PG 世代→口座解決、
    FLATTEN_CONFIRMED の判定、再送の間隔管理)。全部純関数で、tests/test_r96_exit_policy.py で覆う。
 c. 門(mode=LIVE のときだけ通る分岐)を入れる。_kill_exit_switches の撤去は LIVE のときだけ効く形で。
 d. 利用箇所をつなぐ。fill_watch 経由の reconcile(scenario 無し bundle)から数秒で重ねないこと、
    再送は「同じ建玉世代」に対してだけであることをテストで固定。
    R52 の一過性 FLAT 検査(_stable_broker_snapshot 相当)を通した FLAT だけを「確認」とする。
 e. AUTO OFF 中の武装台帳 KILL が _switch で無視される件を、同じ mode の裏で直す。

テストは reconcile() に ledger_path・broker_position_query・broker_order_query・
order_runner・arm_state をすべて注入し、本番パスへの到達 0 件を tripwire で検査する
(tests/test_r82_manual_halt.py と tests/test_r52_halt_clear.py の作り方に合わせる)。
CLAUDE.md §3 の「撤退だけは塞がない」は決定 1 で変わるので、docs/R96_EXIT_POLICY.md に
「CLAUDE.md §3 と §5 のこの文をこう書き換える必要がある」と差分案を書く(CLAUDE.md 自体は直さない)。

完了条件: mode=OFF で既存 test 全緑・挙動同一。mode=LIVE のケースが新テストで全部通る。
PR 本文の冒頭に「LIVE に倒すのは運用者。倒す前に --list-halts で残存 HALT を確認」と書く。
```

### G. R97 — 状態機械の性質テスト(fuzz)と Python/Worker 共通ゴールデン

**背景**: R70〜R91 の事故はほぼ全部「特定の順序・特定の枚数遷移」で出た(TP1 後の押し、成行の
即時約定、片脚拒否、閉じた世代の claim)。1 ケースずつ手で書いたテストでは次の順序は拾えない。
Python と Worker で同じ算術(ULTRA エンベロープ、合計リスク、claim 規則)を別々に持っているので、
食い違いは deploy 後にしか出ない。

```text
タスク R97: 性質テスト(property-based)と Python/Worker 共通のゴールデンケースを追加する。
本番コードは変えない。不変条件が破れたら「テストを通すために本番を直す」ではなく、
破れる入力を最小化して PR 本文に報告する(修正は別 PR)。

Python(hypothesis を devDependency として requirements-dev.txt に追加):
 1. tranche.py: leg_states → expected_qty。不変条件: 期待枚数は OPEN 脚の合計と一致、
    INCONSISTENT が 1 つでもあれば管理は FLATTEN 判定のみ、PENDING 中は MODIFY を出さない。
 2. pyramid.py: combined_entry / combined_risk / _fit_to_cap。不変条件: 合計リスクは追撃後の
    合成建値から計算され、cap を超える枚数は返さない、枚数は 0 以上の整数、tick 整合。
 3. broker_status.oco_sibling_pair: R80 の 8 条件のどれか 1 つを崩した行は必ず不成立
    (成立行を生成してから 1 条件ずつ壊す生成器)。
 4. stop_logic.py / msnr_gate._apply_vwap_clearance / entry_depth.plan_shift:
    SL は不利方向へ動かない(平行移動は幅不変)、60pt 上限、0.25 tick 整合、
    R:R が壊れたら WATCH。
 5. execution_contract.py の等級・状態ゲート: allowedGrades の外は必ず不合格。
Worker(fast-check を devDependency に):
 6. cloudflare/src/state_machine.js: position/order の遷移関数に任意の順序のイベント列を
    流し、OPEN_POSITION_STATES・BLOCKING_ORDER_STATES・TERMINAL の不変条件を検査。
共通ゴールデン:
 7. tests/golden/*.json に「入力 → 期待出力」のケースを置き、Python(tests/test_r97_golden.py)と
    Worker(cloudflare/test/r97_golden.test.mjs)が同じファイルを読んで同じ答えを出すことを検査
    (対象: ULTRA エンベロープの口座別枚数、combined_risk、claim の受理/拒否)。
    既存の test_r84_worker_conformance.py がやっている範囲を吸収して重複を消す。

完了条件: CI 緑。見つけた不変条件違反は docs/R97_PROPERTY_TESTS.md に表で残す
(入力の最小例・どの不変条件・本番で起き得るか)。
```

### H. R98 — 裸 runner 脅威モデル(レッドチーム読解)

**背景**: 「建玉が保護注文ゼロ、または管理外で放置される」経路が R70(建値移動見送り)、
R71(旧プラン選択)、R74/R75(HALTED 行で fallback 破棄・有利側乖離)、R78(片脚拒否)、
R79(乖離で未所有)、R80(SUSPENDED を保護扱い)と 7 回見つかっている。全部本番で見つけた。
コードを全部読んで経路を列挙する仕事は Devin に向く。

```text
タスク R98: autotrade_engine.py / order.py / fill_watch.py / route_identity.py /
ownership_binder.py / tranche.py / cloudflare/src/nightwatch_do.js を読み、
「建玉が保護注文ゼロになる」「建玉があるのに engine が管理しない」「決済したのに台帳が
終端しない」に至る経路を全部列挙する。本番コードは変えない。

出力 docs/R98_NAKED_RUNNER_THREAT_MODEL.md:
 - 表: 経路 ID / 起点(どの関数のどの分岐)/ 引き金(ブローカー応答・タイミング・枚数遷移)/
   既存の防御(R 番号と関数名)/ 残る隙間 / 隙間を固定するテスト案 / 本番で起きた例の有無。
 - 既知 7 件(R70/71/74/75/78/79/80)を先に埋め、それ以外を探す。特に:
   (a) cancelandbracket の取消と新規の間の窓、(b) fill_watch と 3 分ループの reconcile ロック
   の境界、(c) ACCUMULATION 位相(R84)で MODIFY を出さない間の SL の所在、(d) Worker の
   管理 claim が RESOLVED/UNKNOWN で残る場合、(e) 監視窓の外(05:45〜07:00 JST)で建玉が残る場合、
   (f) DO 1101 で Worker が丸ごと死んでいる間の管理、(g) manualHalt ON 中の建玉。
 - 「隙間あり」と判定した経路には tests/test_r98_*.py で再現テストを書く(赤のまま入れず、
   期待動作を assert して skip 理由に経路 ID を書く。修正は別 PR)。

完了条件: 表の全行に「既存の防御」か「隙間」のどちらかが埋まっていて、根拠の行番号がある。
推測で「たぶん守られている」と書かない。読んで確認できなかった箇所は「未確認」と書く。
```

### I. R99 — 20 口座 90 日プランのモンテカルロ(面白い枠)

**背景**: 2026-09-12 に「自動化対応 4 社 × 5 口座 = 20 口座(全部 50K)」を決めた。20 口座は独立でなく
同一シグナルの写し(相関 1)なので、期待値の議論は「1 口座の分布 × 相関 1」で決まる。
`challenge_calc.py` は 1 口座の算術感度しか出さない。**規約の数値は Devin に調べさせない**
(会社ごとに違い、変わる。推測で埋めると全部が嘘になる)。

```text
タスク R99: fleet_sim.py — 複数プロップ口座を同一シグナルで同時に運用したときの
モンテカルロ。読むだけの計算ツールで、発注や契約には一切つながない。

入力:
 1. firms.json(運用者が埋める。私は値を入れない): 会社ごとに
    { name, phase: "eval"|"funded", startBalance, maxDD, ddType: "EOD_TRAILING"|"INTRADAY_TRAILING"|
      "STATIC", ddLockRule(例: Lucid Flex 50K は残高 $52,100 超えで日を終えると床 $50,100 に固定),
      profitTarget, consistencyPct(1 日の利益が総利益の x% を超えたら目標に数えない等、規則の
      文章も文字列で持つ), minTradingDays, payoutRule(頻度・最小・利益の何 %・上限),
      maxContracts, feePerSide, sourceUrl }。
    全項目に "TODO" を許し、TODO のまま走らせたら結果に「未入力の規約あり」と印を付ける。
    リポ内に既にある数値(TRADING_CONTEXT.md、docs/reports/PROP_20_*.md、challenge_calc.py)は
    参照して初期値にしてよいが、出所を sourceUrl か sourceFile に書く。
 2. トレード分布: (a) corpus の Replay Lab 出力(R93 の逐次 R 列)か、(b) .secrets を持たない
    私のために tests/fixtures の合成分布。R 倍数の経験分布からブートストラップで日次の
    トレード列を作る。1 日のトレード数の分布も経験値から。
 3. 枚数方針: 評価は 2 枚固定、funded は ULTRA(口座別 min(残 DD, $5,000) から逆算。
    ultra_mode.py の関数を import して同じ算術にする)。

モデル: 全口座が同じトレード列を受ける(相関 1)。会社ごとの DD 規則・一貫性・出金規則を日次で適用。
出力(docs/reports/FLEET_SIM_<日付>.md + JSON):
 - 90 日後に funded になっている口座数の分布、初回出金までの日数の分布、全滅確率
 - 「相関 1」と「独立(参考)」の差(なぜ 20 口座で分散が減らないかを数字で)
 - 感度: 1 日のトレード数、勝率 ±5pt、平均 R ±0.2、枚数 2 → ULTRA、一貫性 30/40/50%
 - 一貫性ルールが「TP1 だけで日次上限に当てて止める」枚数設計と噛み合うかの検算
再現性: 乱数 seed を固定し、同じ入力で同じ出力(テストで固定)。
やらないこと: 会社の規約をウェブから拾って埋めること(運用者が入れる)。「合格確率 x%」を
未入力のまま断定すること。
```

### J. R100 — Worker の rows read 予算

**背景**: 2026-09-10 01:25 から Worker が全エンドポイントで 1101(DO 無料枠の rows read 超過)を返し、
AUTO が DISARMED/HALT で丸一日止まった。`#migrate` が毎リクエストでストレージを読み、`/ws` の
snapshot と cycle_health ストリームも読む。索引はその後張ったが、予算としては測っていない。

```text
タスク R100: cloudflare/src/nightwatch_do.js の SQLite 読み取り行数を測り、予算化する。
deploy は運用者。契約値(execution_contract.json の共有キー)は変えない。

 1. 計測: テスト用に sql.exec をラップして rows read を数える計測器を cloudflare/test に作り、
    /state・/ws の初回 snapshot・/publish(各ストリーム)・cycle_health・manualHalt・
    entry/management claim の各経路で「1 リクエストあたり何行読むか」を表にする
    (docs/R100_DO_ROWS_READ_BUDGET.md)。3 分ループ 1 周 + fill_watch + Mini App 1 台の
    1 日の合計を見積もり、無料枠(1 日の rows read 上限。数値は Cloudflare の現行ドキュメントを
    確認して出所 URL を書く)に対する比率を出す。
 2. 削減: #migrate はスキーマ version を 1 行読むだけにし、version が一致すれば他を読まない。
    /ws の snapshot と cycle_health は必要な列・件数だけ(RESULT_LOG_LIMIT / ACCOUNT_LIST_LIMIT /
    ROUTE_SNAPSHOT_LIMIT を守る)。索引が効いているかを EXPLAIN 相当のテストで検査。
 3. 予算テスト: 各経路の rows read が表の値を超えたら落ちるテストを追加(閾値は表の 1.2 倍)。
 4. 検知: 1101 を受けた Python 側(nqx_state.fetch_state_quiet 等)が「DO 枠切れの疑い」と
    区別できる例外種別を返す最小変更を提案(実装は別 PR。Python は変えない)。

完了条件: npm test 緑、表と削減前後の比較が docs にある。PR 本文に「deploy 必要」と
「deploy 後に運用者が確認する 1 コマンド」を書く。
```

---

### K. R103 — 流動性狩り対策(計測 → プール検出 → 再生)【2026-09-16 追加・最優先】

**背景**: 2026-09-15 の 4 連敗(VP80 B / BREAKER B / BREAKER A / BREAKER A+)は全部、損切りの後に
価格が狙った方向へ進んだ(2 件は決済後 24〜80 分で TP1、1 件は TP2 まで。合計 200pt 超)。
09-04〜09-15 の帰属済み損切り 19 件で見ても、決済後 3 時間以内に TP1 へ届いたものが 7 件、50pt 以上
順行したものが 11 件。**弁別したのは「SL の外側 1N 以内に未回収の流動性プールがあるか」で、
勝ち 0/7・負け 6/12**。一方コードは、`msnr_gate.swing_liquidity`(BSL/SSL)とセッション高安・VA 端を
ターゲット・DOL・HRLR にしか使わず、**SL の計算では一度も見ていない**(R90 が VWAP だけ直した穴と同じ形)。
数字と 6 つの欠落は `docs/reports/STOP_HUNT_EVIDENCE_2026-09-15.md`。09-15 の確定 3 分足と 4 件の
幾何は `tests/fixtures/r103/`(口座情報なし)に固定してあり、Devin はこれだけでテストを書ける。

**決めてあること(変えない)**:
- 「SL だけ広げる」は採らない(R86 で否定済み)。効かせるのは **SL の位置**(プールの向こう側)と
  **建値のタイミング**(掃引の後)。
- 契約スイッチの既定は SHADOW(記録だけ)。LIVE に倒すのは人。SL が変わる節は FLAT・primary なしのときに
  切り替える(R88 / R90 と同じ)。
- 新モデルは足さない(EDGE_LEDGER 却下リスト)。既存 4 モデルの SL と建値タイミングを変える。
- 再生は R86 / R90 と同じ**不利側**の規則。1 分足が無い(`tv_fetch` は 15 分と 3 分だけ)ので、
  「掃引後に入る」型の再生は楽観側に倒れうる。その行には必ず印を付ける。
- 計測(K-0)が先。効いたかどうかをスコアカードのタグで測れない変更は入れない(R48 の規律)。

**入口**: `stop_logic.py`(`vwap_clearance` の形と `load_policy` の節ごとの検証)、
`msnr_gate._candidate_for_chain` / `candidate_vp80` / `swing_liquidity` / `feature_tags` / `noise_floor`、
`obsidian_metrics.excursions`(MAE/MFE の既存実装)、`model_scorecard.record`、
`entry_depth.py` / `replay_stop_logic.py`(約定規則)、`autotrade_engine._market_stop_guard`(R90 穴 2)。

**K-0(Devin)**:

```text
タスク R103-0: 決済トレードの「刈られ方」を計測する excursion_metrics.py を書き、スコアカード行へ追記する。

背景と数字: docs/reports/STOP_HUNT_EVIDENCE_2026-09-15.md。固定データ: tests/fixtures/r103/
(bars3m_2026-09-15.json = MNQ 3 分確定足、cases_2026-09-15.json = 4 件の武装時点の幾何と約定)。

1. excursion_metrics.py(純関数。ネットワーク・台帳・発注に触れない):
   classify(trade, bars, tp1, noise) → dict。trade は .secrets/model_scorecard.jsonl の 1 行と同じ形
   (side LONG/SHORT、entry、stop、exit、openedAt、closedAt、outcome)。返すもの:
     maePt / mfePt / maeR / mfeR / holdMin(保有中。確定 3 分足の高安)
     beyondStopPt(決済後 15 分に SL をどこまで抜けたか。SL 到達でなければ null)
     tp1AfterExitMin(決済後 180 分以内に TP1 へ届くまでの分。届かなければ null)
     fav3hPt(決済後 3 時間の最大順行 pt)
     huntClass: 損切りのとき
       STOP_HUNT = tp1AfterExitMin が非 null、または(beyondStopPt ≤ 1.0×noise かつ fav3hPt ≥ 2×SL 幅)
       WRONG_WAY = fav3hPt < 1×SL 幅
       DEEP = それ以外
     勝ちは WIN、建値付近は FLAT。しきい値は定数にして docstring に書く。
   obsidian_metrics.excursions は同じ計算を持つ。中身をこのモジュールへ移し、obsidian_metrics は
   これを呼ぶ形にして、既存の出力が変わらないことをゴールデンテストで固定する。
2. model_scorecard.record が書く行に上の値を足す(既存キーと衝突しない名前。足が無ければ null)。
   Worker へ publish する result ペイロードは変えない(Worker 側の検証に触れない)。
   足は trade_journal が既に持つ確定 3 分足(result.view.market.bars と tv_raw)を注入引数で受ける。
3. model_scorecard.py に --excursions を足す: モデル×等級ごとに huntClass の内訳、beyondStopPt と
   tp1AfterExitMin の中央値を表にする。--backfill-excursions <bars_dir> で既存行を再計算し、
   supersedes 付きの新行を追記する(既存行は消さない・書き換えない)。出力は cp932 でも落ちないように
   sys.stdout を utf-8 に再設定する(既存の main() は「—」で UnicodeEncodeError を出す)。
4. tests/test_r103_excursions.py: fixture の 4 件が全部 STOP_HUNT に分類され、beyondStopPt が
   54.0 / 0.0 / 26.25 / 8.25、tp1AfterExitMin が 168 / null / 78 / 21(±3 分)になることを固定する。

完了条件: fixture だけで通る純関数。python tests/run_all.py 緑。本番パスへの到達 0 件。
やらないこと: 採点・武装・契約を変えること。publish ペイロードを変えること。
```

**K-1(Devin)**:

```text
タスク R103-1: SL 側の流動性プール検出と、SL をプールの向こうへ逃がす契約の節(既定 SHADOW)。

背景: docs/reports/STOP_HUNT_EVIDENCE_2026-09-15.md §3〜§4。R90 の vwapClearance と同じ形で入れる
(stop_logic.vwap_clearance と msnr_gate._apply_vwap_clearance を読んでから始める)。

1. liquidity_pools.py(純関数):
   pools(bars, levels, price, tol, noise) → [{price, kind, label, barT, ageBars, equalCount, swept}]
     kind: SWING_HIGH / SWING_LOW(msnr_gate.swing_liquidity をそのまま呼ぶ。再実装しない)、
           SESSION(levels のラベル Asia/London/New York High|Low)、VA_EDGE(C:/P: VAH|VAL)、
           PD_EXTREME(PDH / PDL / Previous Day High|Low)。ラベルの正規表現は msnr_gate の既存に合わせる。
     equalCount: 同じ側の極値が tol 以内に何本並ぶか(EQH / EQL の厚み)。
   stop_pool_audit(side, entry, stop, pools, noise, rule) →
     {between, beyondWithinN, nearestBeyond, inside025N, required, applied, reason}
     required = 最寄りの外側プールの向こう clearN×N(stop_logic.outward_tick で不利側へ丸める)。
     プールが無い / withinN×N より遠い / SL が既に向こう側なら required は null、reason は
     NO_POOL / POOL_FAR_FROM_STOP / STOP_ALREADY_BEYOND_POOL / POOLS_MISSING(levels も bars も無い)。
2. execution_contract.json に stopLogic.poolClearance を足す:
     {"mode": "SHADOW", "withinN": 1.0, "clearN": 0.25,
      "kinds": ["SWING_HIGH","SWING_LOW","SESSION","VA_EDGE","PD_EXTREME"], "models": [4 モデル], "_note": ...}
   stop_logic.load_policy に節の検証を足す(壊れていればこの節だけ OFF。他の節に影響しない)。
3. msnr_gate._candidate_for_chain で _apply_vwap_clearance の直後に同じ形で適用する:
   OFF    = 何もしない。出力は現行とバイト一致(ゴールデンテストで固定)。
   SHADOW = 候補に poolStop(compact な監査)を記録し、記録専用タグ STOP_POOL_WITHIN_1N /
            POOL_BETWEEN_ENTRY_STOP を evidence に足す。SL・score・grade・decisionId は動かさない。
   LIVE   = SL を required に置換し、タグ POOL_STOP_CLEARED。60pt 上限・R:R が壊れれば候補は WATCH
            (SL を内側へ縮めない)。decisionId は最終 SL で決まる(R90 と同じ)。
   select_primary の返り値に poolStop を載せる(vwapStop と同じ compact 形)。
4. 指値の SL 再検査(SHADOW のみ): stopLogic.restingStopRecheck
     {"mode": "SHADOW", "minN": 1.0, "sessionOpen": {"minutes": 30, "opensEt": ["09:30", "03:00"]}}
   純関数 stop_logic.resting_stop_recheck(side, entry, stop, bars_now, at_iso, rule) →
     {noiseNow, noiseSessionOpen, distPt, stale, reason}。noiseSessionOpen は寄付きから minutes 以内なら
     max(直前 12 本の中央値, 寄付き以降の確定足のレンジ中央値)、それ以外は noiseNow。
   autotrade_engine は ENTRY_RESTING を保持している周期にこれを評価し、stale なら台帳へ
   RESTING_STOP_STALE(action=RESTING_RECHECK、plan を持たない、同じ key・同じ理由につき 1 回)を
   書くだけ。取消は送らない(LIVE の配線は K-2 の結果を見てから別 PR)。
5. msnr_gate.feature_tags に記録専用タグ LEVEL_CHOPPED_3 を足す(直前 20 本の終値がレベルを 3 回以上横断)。
6. tests/test_r103_liquidity_pools.py: fixture の 4 件で
   22:38 → beyondWithinN に 29447.00(SWING_HIGH と VA_EDGE)、required = 29447 + 0.25N の不利側 tick
   21:20 → beyondWithinN に 29388.50、between に 29402.25
   16:49 / 21:34 → beyondWithinN は空、between は非空
   restingStopRecheck: 22:38 のケースを 22:36 JST 時点(22:30 / 22:33 の足を含む bars)で評価すると stale=true
   OFF のとき msnr_gate.evaluate の出力が現行とバイト一致(fixture から bundle 相当を組んで固定)。

完了条件: 既定 SHADOW で判定・SL・decisionId が現行と同一(増えるのはタグと監査キーだけ)。run_all.py 緑。
やらないこと: 既定を LIVE にすること。modelGate(TURTLE 停止)を触ること。新モデルを足すこと。
```

**K-2(Devin。B-0 の corpus のあと)**:

```text
タスク R103-2: R103-1 の節を再生で比べる。

前提: corpus/(R93-0 のエクスポート)。無ければ tests/fixtures/r103 だけで配線を完成させ、PR に
「corpus 待ち」と書く。約定規則は replay_rules.py(R93-1)があればそれ、無ければ
replay_stop_logic.py の simulate_setup をそのまま使う(規則を第 3 の場所に書かない)。

変種(全部 BASE=現行契約との比較。結論は逐次=1 建玉ずつの数え方で語る):
  1. poolClearance LIVE(withinN 1.0 / clearN 0.25)
  2. 1 の kinds を SWING だけ / SESSION+VA だけ に絞った版
  3. 「外側 1N 以内にプールがあるセットアップは見送る」(SL を動かさず WATCH)
  4. restingStopRecheck LIVE(stale で取消。取消後は同じ周期で再評価しない)
  5. 1 + 4 の合成
  6. 探索: BREAKER の「掃引後に入る」型。break 後に SL 側の最寄りプールを極値が抜け、レベル側へ
     引け戻した足があって初めて候補化。建値はレベル、SL は掃引極値 + 緩衝。3 分足では足の中の順序が
     見えないので、この行には必ず「楽観側」の印を付ける。

出力: docs/reports/REPLAY_R103_<日付>.md。各変種に N / 勝率 / 粗 PF / 平均 R / 合計 R /
ブートストラップ 95% 区間 / 前半 70% と後半 30% の分割 / 損切りの huntClass 内訳(R103-0)。
N<200 の行は PF を結論にしない旨を機械的に注記。時間分割で反転する変種は「不安定」の印。
別表: 09-04〜09-15 の実トレード(cases と corpus から再構成できる分)に変種 1・3・4 を当てた
1 件ずつの結果(entry_depth.py --trades と同じ形)。

やらないこと: 契約の値を変える PR。1 分足の順序を仮定して楽観側に約定させること。
```

**【人】がやること**(Devin の外):
- B-0 の corpus export(監視窓の外か、ループを止めているとき)。K-2 はこれが無いと fixture 止まり。
- **1 分足の取得**: `tv_fetch` に 1 分足(`data_get_ohlcv` を pane 1 で解像度 1・240 本)を足し、
  `tv_snapshot` の optional `bars1m.json`(`tv_snapshot.py:89`)を埋める。実機(TradingView Desktop)が
  要るので監視 PC の Claude Code セッションで行う。これが入るまで「掃引後に入る」型は再生できても信用しない。
- K-1 の節を LIVE に倒す判断(K-2 の逐次表を見てから。FLAT・primary なしのときに)。
- R86 `entryDepth.mode` の再判断(証拠資料 §5: 09-16 の再生で VP80 0.25N の差が負に転じた)。

---

## 4. Devin の運用メモ

- **1 セッション 1 タスク**。同じセッションで別タスクに進ませない(注入漏れのテストが混ざる)。
- PR は人がレビューしてから merge。F(engine)は merge 後も `mode=OFF` なので本番は変わらないが、
  **本番ツリーへ取り込んだ瞬間に 3 分ループが新コードを走らせる**。取り込みは監視窓の外
  (JST 05:45〜07:00)か、ループを止めているときに。
- 取り込みは git pull ではなく、本番ツリー(この Downloads 配下)へ差分を当てる。`.secrets/` と
  ルートのランタイム JSON は git 管理外なので上書きされないが、`execution_contract.json` は
  管理内。**F の PR が契約に節を足すので、取り込み前に本番の契約と diff を見る**。
- Devin が「本番で叩いてほしい 1 行」を PR に書く設計にしてある(B-0 の export、C の doctor、
  J の deploy 後確認)。それだけを人が叩く。
- Devin に見せるべき既存資料: `CLAUDE.md`(契約)、`docs/R84_*` `docs/R90_*` `docs/R91_*`
  (最近の設計の書き方)、`tests/run_all.py` の docstring、`tests/_hermetic.py`。
  `TRADING_CONTEXT.md` は口座条件の正本だが、Devin の作業に金額は要らない。

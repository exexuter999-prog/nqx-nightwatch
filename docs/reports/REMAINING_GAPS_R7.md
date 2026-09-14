# 残穴統合指示書 R7(Opus 5 向け)

**発行**: 2026-08-18(Fable 5)
**目的**: R1〜R6 を経て残っている穴を全て棚卸しし、埋められるものを埋める。
**凍結解除**: 本書に限り `order.py` / `nqx_state.py` / `monitor_publish.py` の
編集を**テスト必須で**許可する。`telegram_bot.py` は引き続き触らない
(調査の結果、触らずに済む設計を §2 で確定済み)。

---

## 0. 穴のインベントリ(これが全部)

| # | 穴 | 重大度 | 本書の対応 |
|---|---|---|---|
| 1 | **日次ガードが文書のみ**: 日次損失 −$180 / DAYGOAL / 2連敗 / 15分冷却を `order.py` は強制しない。8/7(−$2,816)の破綻パターンを機械は止められない | **最重大** | §2 dayguard |
| 2 | **R5 未実装**: A+ 条件3が FLIP 型を排除(8/12 の勝ち型が 0.4〜0.6 帯で永久に A 止まり)/ vwapPath が drift で8割無効 | 高 | §1(R5 を先に実装) |
| 3 | **R6 の本番実証が未了**: evaluation が本番 Worker を通ることはローカル E2E でしか証明されていない | 中 | §3 チェックリスト |
| 4 | evaluation に htf/entry/tp を手動追記した後のサイズ再検査が無い(4096 超で Worker が丸ごと null に落とす) | 低 | §3 |
| 5 | スキル operational-gates.md §9.10 に `--card` 手順が無い(CLAUDE.md §7 のみ) | 低 | §3 |
| 6 | セッション開始手順(CLAUDE.md §0)に指標健全性チェックが無い(8/18 に CVD Unified と VWAP Bands が死んだ既知事象) | 低 | §4 |
| 7 | ADVISORY(vwapPath/vpPath)の3セッション観測が 0/3 | — | 運用事項。実装なし(観測欄は HANDOFF に既存) |
| 8 | 8/17 のブローカー決済価格が未確認(逆算値で publish 済み。訂正は resultId ハッシュで二重掲載になる) | — | **対応不能と明記**(ブローカー履歴が出たら手動判断) |
| 9 | 回転感度(1本→2本で発火1→3件)の再評価 | — | ADVISORY 観測3セッション後に判断。実装なし |

§7〜§9 は「埋めない穴」として記録することが対応。黙って放置しない。

---

## 1. Phase 0 — R5 を先に実装する

[STRATEGY_EVOLUTION_R5.md](../history/STRATEGY_EVOLUTION_R5.md) を**そのまま**実装する
(A+ 条件3に FLIPPED / bars3m 140本供給 + 全量 VWAP + 60本走査の分離 /
後方互換テスト)。検収基準も R5 §5 のとおり。
R7 の他 Phase より**先に**やる(§2 以降と衝突するファイルは無いが、
テスト全緑を Phase 境界ごとに確認する運びにするため)。

---

## 2. Phase 1 — 日次ガード(dayguard): 文書ルールを機械にする

### 2.1 設計の前提(調査済み・変更不要な事実)

- **結果記録の単一チョークポイントは `nqx_state.publish_result`**
  (nqx_state.py:799)。CLI `--result`(:952)と Bot の `/result`
  (telegram_bot.py:1152)の両方がここを通る。**よって telegram_bot は
  無変更で両経路を捕捉できる**
- 口座別上限は `RISK_*`、日次利益目標は `DAYGOAL_*` が
  `.secrets/crosstrade.env` に既存
- 取引日境界は **07:00 JST**(CME メンテナンス 17:00 CT)。
  env `NQX_DAY_BOUNDARY_H`(既定 7)

### 2.2 新規 `dayguard.py`(stdlib のみ・~150行)

**台帳**: `.secrets/day_ledger.jsonl`(追記専用・1行1決済):

```jsonc
{"closedAt": "2026-08-18T00:45:00+09:00", "side": "BUY",
 "pnl": {"LFE...0002": -38.5, "LFE...0003": -37.0, "LFE...0004": -37.0},
 "source": "publish_result"}          // または "manual"
```

- 口座別が分からない記録は `"pnlEach": -38.5`(全口座同額とみなす)

**判定**(`check(now, env)` → dict):

```
day          = closedAt を 07:00 JST 境界で切った「今日」の行のみ
dayPnl       = 今日の全行・全口座の合計
lastLoss     = 今日の合計が負だった最新行の closedAt
consecLosses = 今日の行を時系列に見て、末尾から連続する負け行数
blocked(理由は全て列挙):
  DAY_LOSS_LIMIT  : dayPnl ≤ −180.0(env NQX_DAY_LOSS_LIMIT)
  DAYGOAL_REACHED : いずれかの口座の日次合計 ≥ その口座の DAYGOAL_*
  TWO_LOSSES      : consecLosses ≥ 2
  COOLDOWN        : now − lastLoss < 15分(env NQX_COOLDOWN_MIN)
```

**CLI**:

```
python dayguard.py --status                    # 判定と根拠を表示
python dayguard.py --record --pnl-each -38.5 --closed-at ISO   # 手動追記
python dayguard.py --record --pnl 0002=-38.5,0003=-37,0004=-37 --closed-at ISO
```

- 台帳が無い/空 = 今日まだ取引なし → **allowed**(正常系)
- 台帳が**壊れている** = 読めない行はスキップし、警告を必ず表示して
  **fail-open**(破損で恒久ブロックしない。ただし黙らない)
- 全て 0.25/金額の検証付き。exit 0/2 のみ

### 2.3 組み込み(2箇所+1箇所)

1. **`nqx_state.publish_result`**: publish 成功後に
   `dayguard.record_from_result(result)` を呼ぶ(result から closedAt と
   実現損益を写す。**publish が失敗したら台帳に書かない**)。
   import は関数内 lazy + try/except で、dayguard 不在でも publish が
   壊れないこと
2. **`order.py`**: 新規発注(通常・`--market`)の**ドライラン表示と
   `--confirm` 送信の両方**の前に `dayguard.check()` を評価。
   blocked なら理由を全て表示して拒否する。
   - **`--flatten` / `--modify` / `--cancel` / `--status` は絶対に
     ブロックしない**(決済・撤退はいつでもできること。これを塞ぐと
     ガードが逆に危険になる)
   - env `NQX_DAYGUARD=off` で無効化できる(表示に「⚠ガード無効」を必ず出す)
3. **`monitor_publish.py`**: blackout / vol gate と同じパターンで、
   dayguard が blocked のとき ARMED/ACTIVE を WATCH に降格 + note 追記
   (Mini App の発注ボタンが消える = 表示側の防御)。
   台帳なし・dayguard import 失敗は fail-open + note

### 2.4 テスト(`tests/test_dayguard.py` 新規 + 既存2本に追加)

| ケース | 期待 |
|---|---|
| 台帳なし | allowed |
| −$180 到達(3口座計) | DAY_LOSS_LIMIT |
| 1口座が DAYGOAL 到達 | DAYGOAL_REACHED(口座IDを理由に含む) |
| 2連敗(勝ち→負け→負け) | TWO_LOSSES。勝ち→負け→勝ち→負け は通る |
| 負けの 14分後 / 16分後 | COOLDOWN / allowed |
| 07:00 JST 境界: 前日の負けは翌日に持ち越さない | allowed |
| 破損行を含む台帳 | 警告付き fail-open、正常行は集計される |
| order.py: blocked で新規拒否・flatten は通る | 両方 |
| monitor_publish: blocked で ARMED→WATCH 降格 | note に理由 |
| **純度**: 台帳が無いとき market/scenario payload が従来と同一 | バイト一致 |

既存の `tests/run_all.py` 全ファイルが緑のまま。
`test_order_market.py` / `test_oneclick.py` の既存ケースを壊さない。

### 2.5 ドキュメント

- CLAUDE.md §3「提案を止める条件」の表に
  「**この表は dayguard.py が機械強制する**(order.py が発注前に判定・
  monitor_publish が武装降格)。手動記録は `dayguard.py --record`」を追記
- §6 の決済記録手順に「`--result` を通せば台帳は自動で付く。通さない
  決済だけ `dayguard.py --record`」を追記
- HANDOFF に「`LIFELINE_*` 手動更新は従来どおり必要(dayguard は
  残機を管理しない)」と明記

---

## 3. Phase 2 — R6 の仕上げ(本番実証+小穴2つ)

1. **本番実証チェックリスト**(次のライブサイクルで1回だけ実施):
   ```
   [ ] --card 付き bundle を publish → "market ok" が出る
   [ ] Mini App を開き、シナリオ無しなら GATES ボードが出る
   [ ] ADVISORY チップに「観測中 · 武装根拠外」が付いている
   [ ] 出なければ: Worker のデプロイ時刻を wrangler deployments list で確認し、
       state_machine.js の normalizeEvaluation が本番に入っているか疑う
   ```
   結果を HANDOFF に1行記録
2. **サイズ再検査**: CLAUDE.md §7 の「評価カードを bundle に入れる」
   ワンライナーの print 行を
   `print('evaluation:', ..., '/', len(json.dumps(d['evaluation'], ensure_ascii=False).encode()), 'bytes')`
   に差し替え、**4096 超なら WARNING を出す**1行を足す(手動追記した
   htf/entry/tp で超過すると Worker が丸ごと null に落とすため)
3. **operational-gates.md §9.10** に手順2行を追記:
   「summary を watching へ配線した後、`--card` の出力を `evaluation` に
   入れてから publish する(Mini App のゲート表示の供給元)」。
   **project/ 複製へコピーして SHA256 一致を確認**

---

## 4. Phase 3 — セッション開始チェックの1行

CLAUDE.md §0 の手順に1行追加:

```
2.5 mcp__tradingview__data_get_study_values で3分ペインの指標5つが
    全て値を返すか確認(CVD Unified 停止・VWAP Bands 無応答は 8/18 に実発生。
    CVD が返らない間は §3 の必須条件を満たせないので提案を出さない)
```

---

## 5. 検収基準(全部 YES で完了)

```
[ ] Phase 0: R5 §5 の検収基準が全て YES
[ ] Phase 1: tests/run_all.py ALL PASS(11ファイル: 既存10 + test_dayguard)
[ ] dayguard: §2.4 の10ケース全て PASS
[ ] order.py --flatten が blocked 状態でも通ることを実測
[ ] 台帳なしのとき market/scenario payload がバイト単位で従来と同一
[ ] Phase 2: 3点の追記が入り、スキル両コピー SHA 一致
[ ] Phase 3: CLAUDE.md §0 に1行
[ ] HANDOFF: 完了記録 + 穴 #7/#8/#9 が「埋めない穴」として残っていること
[ ] telegram_bot.py 無変更(SHA で確認)
```

## 6. 罠

1. **order.py は実発注経路。** 変更は §2.3 の挿入点のみに留め、既存の
   RISK 判定・--last 検証・TP 必須ガードに触れない。テスト先行で書く
2. `publish_result` へのフックは **publish 成功後**。失敗時に台帳だけ
   増えると二重記録になる(resultId 二重掲載と同型の罠)
3. 日次境界は**日付ではなく 07:00 JST**。00:45 の決済は「前日の取引日」
4. PowerShell 5.1 に `&&` は無い(ユーザー環境で実発生)。手順は `;` 区切りで書く
5. Write ツールの BOM / read-then-write / スキル2コピー同期(従来どおり)
6. dayguard は**残機(LIFELINE)を管理しない**。混ぜると正本が2つになる

# MSNR 連鎖ゲート 修正指示書 R2(Opus 5 向け)

**発行**: 2026-08-18(Fable 5 の R1 検収に基づく)
**前提**: [MSNR_HANDOFF_SPEC.md](../architecture/MSNR_HANDOFF_SPEC.md)(R1)と、その実装
`msnr_gate.py` / `tests/test_msnr_gate.py` が存在すること。R1 の定義は
本書で明示的に変更する箇所以外、すべて据え置き。

---

## 0. R1 検収の結論(実装者への申し送り)

R1 実装は検収の機械的項目を**全て通過した**(ALL PASS 10ファイル・スキル
2コピー SHA 一致・preview 2件・monitor_publish 等無変更・summary 行の
文字化けなし)。採用仮定②(スイープに「直前まで支持/抵抗だったこと」を
要求)は**正しい判断として承認**する。維持すること。

ただし合成プローブで**論理欠陥3件**が実証された。うち1件は致命的:
**8/12 の勝ちパターン(下抜け後の戻り売り)が構造的に一生通らない。**

| # | 欠陥 | 実証 |
|---|---|---|
| 1 | **FLIP(RBS/SBR)経路が無い**: 支持を実体で下抜け→受容→戻り→陰線保持、の教科書的 SBR で SELL が `['NO_CHAIN','ANCHOR_BROKEN']`。破壊されたレベルは反対方向のアンカーになる(ドクトリン 5586 行の RBS/SBR 確認)のに、anchorOk が side 無差別で false になる | 合成ケースで SELL false を確認 |
| 2 | **CONSUMED の較正誤り**: ゾーン内クローズが3**本**で CONSUMED になるため、レベル上に3本滞留しただけで失格。実データで完成した連鎖 10 件が全てこれで死んだ | 合成ケースで確認 |
| 3 | **完成連鎖に鮮度が無い**: リテスト保持から 30 本(90分)経過し価格が離れても RETEST_HELD のまま allowed=true | 合成ケースで確認 |
| 4 | (軽微・文書のみ)summary がチャット報告にしか載らず、**Telegram バナー(watching)に届かない** | 経路確認 |

Opus 5 の報告①「実データで allowed=true がゼロ、緩める候補は
NQX_MSNR_CONSUMED」への回答: **既定値 3 は変えない。数え方が誤っていた**
(本数→エピソード数へ。修正2)。報告②の追加前提は上記のとおり承認済み。

---

## 1. 修正1(致命): FLIP 連鎖の追加と anchorOk の side 別化

### 1.1 FLIP 連鎖の機械的定義(SELL = 支持のフリップの例。BUY は鏡像)

役割 SUPPORT のレベル P、ゾーン `[P−tol, P+tol]`(tol は R1 §3 と同じ)で:

| 段階 | 定義 | 状態 |
|---|---|---|
| **break** | 確定足 b: `c < P − tol` かつ `_origin_side(b)` = ABOVE | `FLIP_BREAK` |
| **acceptance** | break 足の後 **FLIP_ACCEPT_BARS(既定3)本以内**に、もう1本 `c < P − tol`(break 足と合わせゾーン外クローズ2本 = ドクトリンの "acceptance outside") | `FLIP_ACCEPTED` |
| **revisit & hold** | acceptance 成立後 **RETEST_WINDOW(既定10・共用)本以内**の確定足 j: `h ≥ P − tol`(ゾーン帰還)かつ `c < P`(保持) | `FLIP_HELD`(終端 PASS) |
| **リセット** | break 後いつでも `c > P + tol` が出たら連鎖消滅(フリップ失敗=奪還) | — |
| **TTL** | break 足から **CHAIN_TTL(既定15・共用)本**で未完成なら `EXPIRED` | — |

- 新パラメータ: `NQX_MSNR_FLIP_ACCEPT_BARS`(既定 3)
- BUY フリップ(抵抗の上抜け→受容→戻り→陽線保持)は完全鏡像
- chain オブジェクトに `"type": "FLIP"` を持たせる。既存のスイープ連鎖には
  `"type": "SWEEP"` を追加。FLIP のフィールドは
  `type / side / state / breakBarT / holdBarT / barsSinceHold / barsLeft / blockers`
- blocker 追加(列挙の完全版は §5): `FLIP_BREAK → ACCEPTANCE_NOT_CONFIRMED`、
  `FLIP_ACCEPTED → FLIP_RETEST_NOT_HELD`
- `best_chain` は SWEEP / FLIP を別々に走査し、**side ごとに最も進んだ連鎖を
  最大2本**(SWEEP 1 + FLIP 1)返してよい。promotion 判定はどちらか一方の
  完成で足りる

### 1.2 anchorOk の side 別化

出力の `anchorOk` を bool から **`{"BUY": bool, "SELL": bool}`** に変更する。

| freshness | 元役割 side(SUPPORT→BUY / RESISTANCE→SELL) | フリップ side |
|---|---|---|
| FRESH / WICK_TESTED / BODY_TESTED / RECLAIMED | **true** | true(実害なし: フリップ側は連鎖の origin 条件で自然に不成立) |
| BROKEN | **false** | **true**(破壊されたレベルは反対方向のアンカー) |
| FLIPPED(新設: BROKEN 後に FLIP_HELD を観測) | false | true |
| CONSUMED | false | false |

freshness に `FLIPPED` を追加する(表示・記録用。判定は上表が正)。

### 1.3 合成式(R1 §2 を置換)

```
promotion[side].allowed =
      ( SWEEP連鎖.state == RETEST_HELD かつ barsSinceRetest ≤ COMPLETE_TTL
        または FLIP連鎖.state == FLIP_HELD かつ barsSinceHold ≤ COMPLETE_TTL )
  AND anchorOk[side]
  AND rotation.verdict == "OK"
  AND NOT dynamic
```

### 1.4 ★ 8/17 を復活させないこと(この修正の検収の核心)

8/17 の2敗は「上抜け→リテスト買い」= まさに RBS フリップ BUY だが、
**保持クローズが出る前に武装した**。FLIP_HELD は保持足の確定後にしか
点灯しないので、武装時点のバンドルでは `FLIP_ACCEPTED` 止まりで
false のままになるはず。**回帰必須**: `.secrets` の `0004` / `0028` /
`0031` サイクルで該当 BUY が **false のまま**であることを再実測し、
blocker(`FLIP_RETEST_NOT_HELD` 等)とともに HANDOFF に記録する。
もし true になったら acceptance / hold の定義が甘い —
**定義を勝手に緩めて通すのではなく、実装を疑って報告すること。**

---

## 2. 修正2: CONSUMED をエピソード計数に

- **body-touch エピソード** = 「直前の確定足クローズがゾーン外」の状態から
  ゾーン内クローズに入った時に 1 回と数える。**連続するゾーン内クローズは
  同一エピソード**(ゾーン外クローズを1本挟むまで次のエピソードは始まらない)
- `CONSUMED` = エピソード数 ≥ `NQX_MSNR_CONSUMED`(**既定 3 のまま変更しない**)
- 出力キー `bodyTouches` は**エピソード数**を返す(意味変更。§9 文書も直す)
- 既存テスト7(3本連続タッチで CONSUMED)はフィクスチャを**分離型3回**
  (間にゾーン外クローズを挟む)に修正する

---

## 3. 修正3: 完成連鎖の鮮度 TTL

- 新パラメータ `NQX_MSNR_COMPLETE_TTL`(既定 **10** 本)
- RETEST_HELD / FLIP_HELD は保持足から COMPLETE_TTL 本を超えたら
  `EXPIRED`(blocker `CHAIN_EXPIRED`)に落とす。R1 実装が既に持つ
  `barsSinceRetest`(FLIP は `barsSinceHold`)をそのまま使う
- summary の完成表示にも残本数を出す:
  `リテスト保持(鮮度残N本)` の形式

---

## 4. 修正4(文書のみ): summary を Telegram バナーに配線する

CLAUDE.md §7 の「【毎サイクル】MSNR 連鎖ゲート」を以下の手順に置換する
(operational-gates.md §9 の使用手順も同一に):

```
- bundle を書いて BOM を除去した後、
  `PYTHONUTF8=1 python msnr_gate.py --summary < .secrets/monitor_cycle_HHMM.json`
  を実行し、出力1行を (a) 報告に載せ、(b) 下のワンライナーで bundle の
  watching 末尾に追記してから publish する(バナー詳細と Mini App に載せるため。
  watching の先頭は構造ナラティブのまま維持する)
- シナリオを A/X に昇格させる前に `--level <構造価格S> --side buy|sell` で
  該当 side の allowed=true を確認する(false なら blocker を watching に書いて W 止まり)
```

watching 追記のワンライナー(**read-then-write。1行版の自壊トラップ
(2026-08-14)を踏まないこと**):

```bash
PYTHONUTF8=1 python -c "
import io, json, sys
p, line = sys.argv[1], sys.argv[2]
d = json.loads(io.open(p, encoding='utf-8-sig').read())
w = d.get('watching') or []
w = [x for x in w if not str(x).startswith('MSNR:')] + [line]
d['watching'] = w
io.open(p, 'w', encoding='utf-8', newline='\n').write(json.dumps(d, ensure_ascii=False, indent=1))
" .secrets/monitor_cycle_HHMM.json "<summaryの1行>"
```

---

## 5. blocker 列挙の完全版(これ以外を出力しない。§9 両コピーも更新)

```
NO_CHAIN / SWEEP_ONLY / NO_DISPLACEMENT / MSS_NOT_CONFIRMED / RETEST_NOT_HELD /
ACCEPTANCE_NOT_CONFIRMED / FLIP_RETEST_NOT_HELD / CHAIN_EXPIRED /
ANCHOR_BROKEN / ANCHOR_CONSUMED / ROTATION_REGIME / INSUFFICIENT_BARS / DYNAMIC_LEVEL
```

---

## 6. ドキュメント編集

1. **CLAUDE.md §3** の MSNR 行を更新:
   `RETEST_HELD` → 「完成状態(`RETEST_HELD` / `FLIP_HELD`、鮮度 TTL 内)/
   該当 side の `anchorOk` / 回転 OK」
2. **CLAUDE.md §7** — §4 の置換
3. **operational-gates.md §9** — FLIP 連鎖・side 別 anchorOk・エピソード計数・
   COMPLETE_TTL・blocker 列挙・使用手順を反映。パラメータ表に
   `FLIP_ACCEPT_BARS` / `COMPLETE_TTL` を追加。
   **編集後 project/ 複製へコピーし SHA256 一致を確認**
4. **HANDOFF.md** — R2 完了記録(§8 の実測値を含む)

---

## 7. テスト追加・修正(`tests/test_msnr_gate.py`)

既存 74 チェックは、意味変更に該当する以下以外**全て維持**:
テスト7(CONSUMED)のフィクスチャ修正、blocker 列挙検査の更新、
anchorOk のスキーマ変更に伴うアサーション修正。

新規(名前はこのまま):

| # | テスト名 | 期待 |
|---|---|---|
| 1 | `test_sbr_flip_sell_allowed` | 支持で20本推移→実体下抜け→受容2本→戻り高値がゾーン侵入・陰線保持→数本下続 → SELL の FLIP_HELD / **allowed=true** |
| 2 | `test_rbs_flip_buy_allowed` | 1 の鏡像 |
| 3 | `test_flip_without_acceptance_blocked` | 下抜け1本→即ゾーン帰還(受容なし)→ `ACCEPTANCE_NOT_CONFIRMED` / false |
| 4 | `test_flip_before_hold_blocked` | **8/17 型**: 上抜け+受容まで成立、保持足が未出現 → `FLIP_RETEST_NOT_HELD` / false |
| 5 | `test_flip_reset_on_reclaim` | break 後に `c > P + tol` → FLIP 連鎖消滅 |
| 6 | `test_consumed_consecutive_is_one_episode` | ゾーン内クローズ3本連続 → BODY_TESTED(エピソード1)・CONSUMED でない |
| 7 | `test_consumed_three_episodes` | ゾーン外を挟む3エピソード → CONSUMED |
| 8 | `test_completed_chain_expires` | リテスト保持から COMPLETE_TTL+1 本経過 → EXPIRED / false |
| 9 | `test_anchor_broken_flip_side_ok` | BROKEN 支持: anchorOk `{"BUY": false, "SELL": true}` |

---

## 8. 検収基準(全部 YES で完了)

```
[ ] python tests/run_all.py → ALL PASS(10 ファイル)
[ ] §7 の新規9テストが存在し PASS
[ ] 実データ再走査(165 バンドル): 例外ゼロ
[ ] 0004 / 0028 / 0031 の該当 BUY が false のまま(blocker を記録)
[ ] 8/17 21時台に RETEST_HELD だった 10 件の再判定結果を報告
    (エピソード計数で true になるのは正常。ただし 1 件はバー列を目視して
     「その時点で武装して良かったか」の妥当性コメントを HANDOFF に書く)
[ ] anchorOk が side 別 / chains に type / blocker 列挙が §5 と一致
[ ] CLAUDE.md §3・§7 更新済み、operational-gates.md 両コピー SHA 一致
[ ] monitor_publish.py / order.py / telegram_bot.py / nqx_state.py 無変更
[ ] HANDOFF.md に R2 完了記録(実測値・迷った点と採った仮定)
```

R1 と同じく、**定義の解釈変更は禁止**。迷ったら仮定を明記して報告する。

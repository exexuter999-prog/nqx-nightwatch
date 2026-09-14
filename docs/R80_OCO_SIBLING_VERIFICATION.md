# R80 張り替え(cancelandbracket)後の保護注文は「建玉に対する OCO 兄弟」であることを構造で証明する

2026-09-12 01:53 JST の実弾(LFF…0006、SHORT 2 @ 29,484.25 の runner)で、TP1 後の
建値移動 `order.py --modify` が送った `cancelandbracket` の結果を、ユーザーがブローカー
UI で「Stop Loss 29,483.25 が Buy Limit 29,081.75(TP)の子ブラケットとして付いている」と
読んだ。子ブラケットは親が約定してから起動するので、その読みが正しければ runner は
実質 SL 無しで、TP 約定後に建玉が無いまま BE 水準の買い逆指値が置かれることになる。

この文書は (1) 何が事実として確定したか、(2) なぜ従来の照合がそれを見分けられなかったか、
(3) R80 で何を変えたかを残す。**送信ペイロードは変えていない**(理由は §1)。

## 1. 事実確定(推測で直さない)

### 1.1 CrossTrade 公式(`/docs/webhooks/commands/cancel-and-bracket`、2026-09-12 取得)

- CANCELANDBRACKET は「銘柄の working 注文を全て取消し、新しい take-profit / stop-loss
  ブラケット(**OCO 対**)へ 1 つの管理された操作として置き換える」。
- `ACTION` は **保護する建玉の側**(`BUY`=ロング保有 → 出口は Sell、`SELL`=ショート保有 →
  出口は Buy to Cover)。出口注文の方向ではない。
- `QTY` は建玉枚数へ自動クランプ。`STOP_LOSS` は常に StopMarket。
- Tradovate では「両脚が Tradovate サーバ上で 1 組の OCO として置かれる」。
  TP と SL の両方を渡すと「自動で OCO リンクされ、一方が約定すればもう一方は取消」。
- 取消フェーズの前後で建玉を再確認し、取消中に建玉が消えていればブラケットを置かない。

補足(`/docs/getting-started/tradovate-guides/tradovate-order-types-and-exits`):
PLACE の `take_profit`/`stop_loss` は Tradovate ネイティブの **OSO**(entry → 子 2 本、
子は `Suspended` で作られ entry の初回約定で起動)。CANCELANDBRACKET は「既存建玉の
周りにブラケットを置く」経路で、`action` は保護側を名指しし、一致する建玉を要求する。

→ 台帳 #1332 の送信(`command=cancelandbracket; action=SELL; qty=2; stop_loss=29483.25;
take_profit=29081.75`)は文書化された契約そのものである。

### 1.2 ブローカーの注文行(CrossTrade REST、読み取りのみ、2026-09-12 02:10 JST)

Tradovate の注文行が持つリンク項目は 3 種類で、意味が違う(同口座の当日行から実測):

| 行 | action | ordStatus | ocoId | parentId | linkedId | 意味 |
|---|---|---|---|---|---|---|
| …960(TP1 脚 ENTRY) | Sell | Filled | – | – | …961 | OSO の親。`linkedId` は最初の子 |
| …961(TP1 の SL/TP 子) | Buy | Filled | …962 | **…960** | – | OSO の **子**。`parentId` が真の親 |
| …962(TP1 のもう一方の子) | Buy | Canceled | …961 | – | – | 子同士の OCO 相方 |
| …978 / …979 / …980(RUNNER 脚) | 同上の形 | | | | | |
| **…014(張り替え)** | Buy | Canceled(※) | **…015** | **無し** | – | 相互 ocoId のみ |
| **…015(張り替え)** | Buy | Working | **…014** | **無し** | – | 相互 ocoId のみ |

- `parentId` = OSO(親が約定してから子が起動する関係)。`ocoId` = OCO(一方が約定すれば
  他方が取消される兄弟)。`linkedId` = 親側から見た最初の子。
- cancelandbracket が作った …014 / …015 は **相互 `ocoId` だけで結ばれ、どちらにも
  `parentId` が無い**。同時刻(16:52:56.051Z)に 2 本が作られている。API 上は
  「TP の子ブラケット」ではなく「同じ建玉を対象にした OCO 兄弟」の形である。
- ただし当時の `ordStatus` が `Working` だったか `Suspended` だったかは **台帳に残っていない**。
  従来の照合は `SUSPENDED` を active として受け入れていたので、仮に子ブラケット
  (Suspended)であっても照合は通っていた。ここが穴である(§2)。
- (※)…014 は照会時点で Canceled、…015 が Working、さらに 17:08:00Z に **台帳に無い**
  Buy …023(リンク無し)が現れている。02:27 JST の再照会では 17:14:05Z に Buy …042 / …044
  (リンク無し)が加わり、建玉は 17:16:40Z に **SHORT 8 @ 29,458.56** へ増えていた。
  nightwatch の台帳は 16:53:31Z の MANAGEMENT_SENT 以降、17:18:21Z の
  `POSITION_GENERATION`(観測の記録)しか書いておらず、ENTRY / MODIFY / FLATTEN は
  一切送っていない。取消・…023 / …042 / …044・6 枚の追加はすべて経路外(手動)の操作で
  ある。凍結プラン(runner SHORT 2)と枚数が合わないので engine はこの建玉を所有せず
  管理しない(hold)。**このセッションでは建玉にも注文にも触れていない。**

### 1.3 UI の読みと API の食い違いについて

Tradovate の UI が OCO 対をどう描くか(Limit 行の「Stop Loss」列に相方の価格を出す、
相方の Type を「Stop Loss」と表示する)は、公式資料を取得できず確定できなかった
(Tradovate ヘルプは 403、コミュニティは該当記述なし)。API の行は §1.2 のとおり
OSO ではない。**UI の見た目で判定せず、行の `parentId` / `ocoId` / `ordStatus` で
判定する**のが R80 の立場である。

## 2. なぜ従来の照合を素通りしたか

R52 の構造照合(`broker_status.verify_protective_orders` / `route_identity.bind_replacement_bracket`)
は次の 3 点で「OCO 兄弟」と「OSO の子」を区別できなかった。

1. **リンクの取り違え**: 正規化が `parentId = ocoId or ocoGroupId or parentId or osId` と
   1 項目に潰していた。OSO の子(`parentId`=TP)と OCO の相方(`ocoId`)が同じ欄に入る。
2. **Suspended を active 扱い**: `BROKER_ACTIVE_STATES` に `SUSPENDED` が入っている
   (ENTRY の OSO 子を照会で落とさないために必要)。張り替え後の 2 行がどちらも
   `Suspended` でも「active 2 行」を満たしてしまう。
3. **子の存在を見ない**: 張り替え注文に **さらに子ブラケットが付いている**行を数えていない。

`order.py` の `_protective_settled` と engine の `_protective_rows`(R78 修復)も同じく
「逆方向 active 行の本数」しか見ていない。

## 3. R80 で変えたこと(送信は変えない)

### 3.1 正規化(`broker_status.normalize_crosstrade_orders`)

行に `brokerOcoId`(生 `ocoId`)と `brokerLinkedId`(生 `linkedId`)を追加した。
`brokerParentId`(生 `parentId`)は R52 のまま。従来の `parentId`(潰したリンク)は
ENTRY 側の消費者(`bracket_pairs` / `ownership_binder`)が使うので残す。

### 3.2 `broker_status.oco_sibling_pair()`(新設・唯一の判定器)

逆方向の未終端行が「建玉に対する OCO 兄弟」であることを次の全条件で証明する。
1 つでも欠ければ `(None, 理由)`。

1. 口座・銘柄・方向が一致する未終端行が **ちょうど 2 本**
2. 2 本とも `BROKER_LIVE_PROTECTIVE_STATES`(`WORKING` 等。**`SUSPENDED` / `PENDING` は不可**)
3. 2 本とも `brokerParentId` が空(親を持つ行は注文の子ブラケット = OSO)
4. `brokerOcoId` が **相互**(a.ocoId == b.id かつ b.ocoId == a.id)。片方向・欠落・
   `parentId` だけの結びつきは不可
5. 口座の未終端行のどれも `brokerParentId` で 2 本のどちらかを指していない
   (張り替え注文に子ブラケットが付いていない)

### 3.3 消費者

- `route_identity.bind_replacement_bracket`(項目欠落行の分岐): 新規行の対を
  `oco_sibling_pair` で束縛する。receipt に `structure=OCO_SIBLINGS` /
  `pricesConfirmed=false` を焼く。
- `broker_status.verify_protective_orders`(構造モード): 同じ判定器 + 注文 ID /
  per-order receipt の一致(R52 のまま)。detail は
  `protective OCO sibling pair verified structurally (...; prices not confirmed)`。
- `order.py _protective_settled`: 「逆方向 2 本」ではなく `oco_sibling_pair` が成立する
  まで待つ。成立しなければ理由を印字し、照合が `MODIFY_POSTVERIFY_UNKNOWN` で止める
  (engine は R78 の修復経路へ)。
- `autotrade_engine._protective_rows`(R78 修復の「保護行の本数」): `SUSPENDED` を
  数えない。TP + Suspended 子 の状態は「保護 1 本」= 裸として修復対象になる。

### 3.4 変えなかったもの

- **送信ペイロード**(`command=cancelandbracket; action=<建玉の側>; qty; stop_loss;
  take_profit`)。文書化された契約と一致し、ブローカーの行も OCO 兄弟の形だった。
  ここを推測で変えると、文書どおりに動いている経路を壊す。
- 価格の裏取り。ブローカーは価格を返さないので、構造で証明できるのは
  「古い対が消え、建玉に対する OCO 兄弟が 1 組張られた」ことまでで、SL の価格は
  engine が送った値を信じる(R52 のユーザー決定のまま)。

## 4. 回帰テスト

`tests/test_r80_oco_sibling_verification.py`(正規化・判定器・束縛・照合・settle・
engine 修復・`order.py --modify --confirm` の in-process 往復)。既存の
`tests/test_r52_modify_incomplete.py` の fixture は実データの形(`brokerOcoId` /
`brokerParentId`)へ寄せた。

## 5. 未決(実装しない)

- トレール目標が現在値に追い越されると R70 の床だけが適用される件(92pt 乗った玉の
  保護が 1pt)。守る金額の定義はユーザー未決。

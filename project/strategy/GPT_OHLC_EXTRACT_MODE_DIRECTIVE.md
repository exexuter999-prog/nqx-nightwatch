# GPT改良指示書 — 15M単一画像からの `O.c` 抽出軽量モード追加

- 宛先: GPT（実装担当）
- 発行: Fable（設計担当）/ 承認: ユーザー
- 対象ファイル: `project/app/nq-nightwatch-nqx-final.html`（Nightwatch本体、AI INTELLIGENCE UPLINKパネルの拡張）
- 上位文書: `project/engine-contract/nqx1-spec.md`（`O`レコード仕様、SLゲート強化3回の追補で最新化済み）
- 本指示書の目的: 既存の「AI INTELLIGENCE UPLINK」（3M/15M/45M複数フレーム画像をAPIへ送りNQXパケット全体を生成させる機能）に、**15Mチャート画像1枚だけを送り、`O|c=...`（観測OHLC）行だけを軽量に生成・反映する第3のモード**を追加する。既存のFAST/DEEPモードは変更しない。

---

## 0. 背景（なぜこの機能が必要か）

`SLゲート強化`作業（`GPT_SL_GATE_UPGRADE_DIRECTIVE.md`と3回の追補）で、`structuralStopPlan()`のSL距離判定は`O.c`（観測OHLC）が供給されていれば直近12本のTrue Range平均（`SUPPLIED OHLC RANGE`）という精密なボラティリティ推定を使い、`O.c=MISSING`の場合はレベル間隔中央値（`MAPPED LEVEL SPACING`）という粗い推定にフォールバックすることが確定した。

ところが実運用では、フルのAI INTELLIGENCE UPLINK（3M/15M/45Mの3枚を要求し、NQXパケット全体——レベルマップ・シナリオ・リスク管理まで——を生成する重い処理）を使わないと`O.c`を埋める手段がなく、日常的には`O.c=MISSING`のまま運用されがちだった。

本機能は、**NQXパケット全体の生成とは切り離し、15Mチャート画像1枚だけから`O.c`行だけを軽量に生成し、既存の`#src`テキストへ差し込む**ことで、この運用上のギャップを埋める。

---

## 1. 絶対規範（既存追補と同一、再掲）

1. 数値を発明しない。画像から読み取れない値は`MISSING`として扱う。既存の`upEvidenceProtocol()`（約5637-5653行目）が定める「OBSERVED/INDICATOR_CLAIM/INFERRED/ASSUMED/UNREADABLE」の分類規律を、このモードでもそのまま適用する。
2. 既存の安全条件を弱めない。既存のFAST/DEEPモード、既存のプロンプト（`upPromptNQX`・`upPromptFast`）、既存の`validateNQX`、既存のRisk Gateには一切手を加えない。
3. 後付け検証の禁止。
4. 成果の粉飾禁止。
5. 質問で作業を止めない。
6. 日本語報告。

---

## 2. 機能要件

### 2-1. UIへの追加

対象: `project/app/nq-nightwatch-nqx-final.html` の `#upMode` セレクト（約2035-2038行目）

現状:
```html
<select id="upMode" aria-label="NQX generation mode">
  <option value="fast">MODE: FAST / EXECUTION NQX</option>
  <option value="deep">MODE: DEEP / FULL AUDIT</option>
</select>
```

新しい選択肢を追加する:
```html
<option value="ohlc">MODE: OHLC / 15M CANDLE EXTRACT ONLY</option>
```

`upModeState`の説明文切り替え（`upRefreshMode()`関数、約5616-5623行目）に、`ohlc`モード用の分岐を追加する:
```js
function upRefreshMode(){
  uplink.mode=$('#upMode')?$('#upMode').value:'fast';
  var box=$('#upModeState'),desc=$('#upModeDesc');
  if(box)box.setAttribute('data-state',uplink.mode);
  if(desc)desc.textContent=uplink.mode==='deep'
    ?'FULL QUANT / CAUSAL / MULTI-FRAME AUDIT'
    :(uplink.mode==='ohlc'
      ?'SINGLE 15M FRAME · O.c ONLY · NO SCENARIO GENERATION'
      :'LEVEL-FIRST · STRUCTURAL SL · SINGLE-PASS VALIDATION');
}
```

`upIsFast()`（約5609行目）は`ohlc`モードでは`false`を返すよう変更してよいが、後述のとおり`ohlc`モード時は専用の分岐（`upIsOhlc()`）で処理を切り替えるため、既存のFAST/DEEP判定ロジック自体を壊さないよう注意する。新しいヘルパー関数を追加する:

```js
function upIsOhlc(){return !!$('#upMode')&&$('#upMode').value==='ohlc';}
```

### 2-2. `ohlc`モード時のUI制約

`ohlc`モードが選択されている間、以下をJSで制御する（`upRefreshMode()`または新しい`upApplyModeConstraints()`関数内）:

- 画像アップロード上限を**1枚**に制限する。既存の`UP_MAX_IMAGES`定数（画像上限、現状の値をそのまま確認して使う）を`ohlc`モード専用に上書きするのではなく、`upAddFiles()`（約5905行目）の`slots`計算箇所で、`ohlc`モードなら`Math.max(0, 1-uplink.images.length)`を使うよう分岐する。2枚目以降が追加されようとした場合は既存の「上限に達した」旨のメッセージ（既存コードの挙動をそのまま流用）で弾く。
- `#upDrop`のプレースホルダテキスト（`DROP OR SELECT CHART IMAGES<br>MAXIMUM 6 // ORDER MUST MATCH FRAME ROLES`、約2062行目）を、`ohlc`モード選択時はJSで`DROP OR SELECT ONE 15M CHART IMAGE<br>OHLC EXTRACTION ONLY`に書き換える。モード変更時に元のテキストへ戻す。
- `#upPacket`内の`FRAME ROLES`・`SOURCE FRAMES`入力欄は、`ohlc`モードでは意味を持たない（15M固定のため）。無効化はしない（絶対規範2、既存要素を壊さない）が、視覚的に「このモードでは無視される」ことが分かるよう、`disabled`属性を付与し、モード変更で元に戻す。
- `#upWebWrap`（イベント検索チェックボックス）は`ohlc`モードでは非表示にする（`upWebVis()`関数、約5626-5628行目に`ohlc`分岐を追加）。O.c抽出はイベント文脈を必要としない。

### 2-3. 専用プロンプトの追加

新しい関数`upPromptOhlc()`を、既存の`upPromptFast()`（約5713行目）の直後に追加する。既存の`upPromptNQX()`・`upPromptFast()`とは独立した、単一目的の短いプロンプトとする:

```js
function upPromptOhlc(){
  var symbol=$('#upSymbol').value.trim()||'MNQ1!';
  return[
    'You are extracting exact observed OHLC bars from ONE 15-minute chart image for '+symbol+'.',
    'Return ONLY one line in this exact format, nothing else. No prose, no Markdown, no code fence, no explanation:',
    'O|c=<TIME,OPEN,HIGH,LOW,CLOSE[,VOLUME]>;<TIME,OPEN,HIGH,LOW,CLOSE[,VOLUME]>;... or O|c=MISSING',
    '',
    upEvidenceProtocol(),
    '',
    'OHLC EXTRACTION CONTRACT',
    '- Read only the trailing bars that are fully closed and clearly readable on this single 15M image. Do not guess a forming/incomplete bar.',
    '- Extract up to 12 of the most recent closed bars, in chronological order (oldest first, most recent last). Fewer is acceptable; never pad with invented bars.',
    '- Each bar is TIME,OPEN,HIGH,LOW,CLOSE with an optional ,VOLUME. TIME is the bar\'s printed or inferable clock time (HH:MM), not a guess at a date. If volume is not readable, omit it (do not write a placeholder).',
    '- A bar qualifies as OBSERVED only if open/high/low/close are all directly readable from candle geometry or an explicit price label/axis — never estimated from a colored fill, a covered candle, an indicator overlay, or a cloud/band.',
    '- If fewer than 3 bars are confidently readable, or the image is not identifiable as a 15-minute chart, or OHLC values cannot be distinguished from indicator overlays, output exactly: O|c=MISSING',
    '- Escape a literal comma inside a value as \\, and a literal pipe as \\|. Semicolons separate bars; do not use a semicolon inside a single bar\'s fields.',
    '- Never fabricate a bar, never interpolate between two visible candles, never reconstruct a bar from a moving average or trend line.',
    '',
    'FINAL AUDIT BEFORE OUTPUT: output is exactly one line starting with O|c=; every bar has 5 or 6 comma-separated fields; high >= max(open,close); low <= min(open,close); high >= low; bars are in chronological order; no bar was invented or interpolated.'
  ].filter(Boolean).join('\n');
}
```

**このプロンプトは既存の`upEvidenceProtocol()`をそのまま呼び出し、画像読み取りの安全規律（OBSERVED/INDICATOR_CLAIM等の分類、colored fillからの復元禁止等）を継承する。新しい安全規律を独自に発明しない。**

`upPrompt()`（約5756行目）の分岐に`ohlc`を追加する:
```js
function upPrompt(){
  if(upIsOhlc())return upPromptOhlc();
  return upIsFast()?upPromptFast():upPromptNQX();
  /* ...既存のデッドコード（到達しない旧実装）はそのまま残す、削除しない... */
}
```

### 2-4. API呼び出しの流用

既存の`upCallClaude()`・`upCallOpenAI()`（約5949-5985行目）は**そのまま流用する**。プロンプトが変わるだけで、画像添付・API呼び出し形式は共通である。変更不要。

`upTokenBudget()`（約5610行目）に`ohlc`モード用の分岐を追加する。1行のOHLC出力は既存のFAST(2200)より遥かに小さいトークン数で足りるため:
```js
function upTokenBudget(){return upIsOhlc()?600:(upIsFast()?2200:3400);}
```
（`600`という数値は目安であり、12バー×1行という出力サイズから見て既存の`2200`より明確に小さくてよいという判断に基づく。正確な最小値の実測はGPTの検証時に行い、不足していれば余裕を持たせて調整してよいが、既存モードの値は変更しない。）

`upTimeoutMs()`（約5611行目）も同様に、`ohlc`モードでは既存FASTの`75000`より短くてよい（例: `40000`）。この値も目安であり、検証時に実測して調整してよい。

### 2-5. レスポンス処理: 既存 `#src` への `O.c` 行の差し込み

これが本機能の核心であり、既存の`runUplink()`（約6032-6077行目）とは異なる処理が必要な箇所である。既存の`runUplink()`は「レスポンス全体を新しいNQXパケットとして`#src`を丸ごと置き換える」設計だが、`ohlc`モードでは「既存の`#src`の中身を保持したまま、`O|c=...`行だけを更新または挿入する」必要がある。

新しい関数`runUplinkOhlc()`を追加し、`runUplink()`の冒頭で`upIsOhlc()`なら分岐させる:

```js
function runUplink(){
  if(uplink.busy)return;
  if(upIsOhlc()){runUplinkOhlc();return;}
  /* ...既存のFAST/DEEP処理はそのまま... */
}

function runUplinkOhlc(){
  if(uplink.busy)return;
  uplink.provider=$('#upProv').value;
  uplink.key=$('#upKey').value.trim();
  uplink.model=$('#upModel').value.trim()||UP_DEFAULT_MODEL[uplink.provider];
  if(!uplink.key){say('API KEY REQUIRED','warn');$('#upKey').focus();return;}
  if(uplink.images.length!==1){say('OHLC MODE REQUIRES EXACTLY ONE 15M CHART IMAGE','warn');return;}
  uplink.busy=true;
  var started=(window.performance&&performance.now)?performance.now():Date.now();
  var timeoutMs=upTimeoutMs(),btn=$('#upRun'),btnText=btn.textContent;
  btn.disabled=true;btn.textContent='OHLC EXTRACTING…';upSetPerf('OHLC RUNNING');
  upSaveLS();
  showBusy(['> OHLC EXTRACT PATH // '+(uplink.provider==='claude'?'CLAUDE':'OPENAI')+'…','> single 15M frame → O.c line only…']);
  var ctl=(typeof AbortController!=='undefined')?new AbortController():null;
  var timer=setTimeout(function(){if(ctl)ctl.abort();},timeoutMs);
  var done=function(){clearTimeout(timer);uplink.busy=false;btn.disabled=false;btn.textContent=btnText;};
  var call=uplink.provider==='claude'?upCallClaude:upCallOpenAI;
  call(upPrompt(),ctl?ctl.signal:undefined).then(function(text){
    hideBusy();
    text=String(text||'').replace(/```[a-z]*\n?/gi,'').replace(/```/g,'').trim();
    var lines=text.split(/\r?\n/).map(function(l){return l.trim();}).filter(Boolean);
    var oLine=lines.find(function(l){return /^O\|c=/i.test(l);});
    if(!oLine){throw new Error('Response did not contain a single O|c= line: '+text.slice(0,120));}
    var elapsed=(((window.performance&&performance.now)?performance.now():Date.now())-started)/1000;
    uplink.lastMs=Math.round(elapsed*1000);
    var merged=upMergeOhlcLine($('#src').value,oLine);
    if(merged.error){upSetPerf('OHLC '+elapsed.toFixed(1)+'s · REJECTED');say('OHLC MERGE REJECTED — '+merged.error,'warn');return;}
    upSetPerf('OHLC '+elapsed.toFixed(1)+'s · '+(merged.replaced?'REPLACED':'INSERTED'));
    $('#src').value=merged.text;
    say('OHLC UPLINK '+elapsed.toFixed(1)+'s — O.c line '+(merged.replaced?'replaced':'inserted')+' ('+merged.barCount+' bars)','ok');
    runParse(false);
  }).catch(function(err){
    hideBusy();upSetPerf('OHLC ERROR');
    var msg=(err&&err.name==='AbortError')?'TIMEOUT ('+Math.round(timeoutMs/1000)+' SECONDS)':(err&&err.message)||'unknown';
    say('OHLC UPLINK FAILED — '+msg,'warn');
  }).then(done,done);
}
```

### 2-6. `O.c` 行の差し込みロジック（`upMergeOhlcLine`）

新しい純粋関数`upMergeOhlcLine(srcText, newOLine)`を追加する。この関数は:

1. `srcText`が空、または`!NQX/1`ヘッダーを含まない場合は、**新規パケットを作らない**。`{error: 'PASTE OR COMPILE AN NQX/1 PACKET BEFORE RUNNING OHLC EXTRACT'}`を返す。O.c抽出はあくまで既存パケットへの補完機能であり、パケット全体を新規生成する機能ではないため（絶対規範2、既存フローとの役割分担を明確にする）。
2. `srcText`の各行を走査し、`/^O\|c=/i`にマッチする既存行があれば、その行を`newOLine`で置換する（`replaced: true`）。
3. 既存の`O`行が見つからない場合、`nqx1-spec.md`のRECORD ORDER（`!NQX/1, D, M, C, Z, E, O, G, S1..`）に従い、`E`行の直後・`G`行の直前に`newOLine`を挿入する。`E`行が無ければ`Z`行の直後、`Z`行も無ければ`M`行の直後、`M`行すら無ければ`!NQX/1`ヘッダーの直後に挿入する（`replaced: false`）。
4. `newOLine`から`O|c=`以降のバー列を`parseCandleRow`/`parseCandleTape`相当（既存の`nqxResolveRaw`＋`nqxSplitEscaped`の変換、`project/engine-contract/validate_nqx.py:254-302`のPython移植と同じロジック——SLゲート強化第3追補で確定した「NQX輸送層はカンマ区切り、内部形式はパイプ区切り」という契約に従う）で解析し、受理されたバー数を`barCount`として返す。1本も受理されなかった場合は`{error: 'NO VALID OHLC BAR ACCEPTED FROM RESPONSE'}`を返す（既存の`#src`は変更しない）。

```js
function upMergeOhlcLine(srcText,newOLine){
  var text=String(srcText||'');
  if(!/^\s*!NQX\/1\s*$/m.test(text)){
    return{error:'PASTE OR COMPILE AN NQX/1 PACKET BEFORE RUNNING OHLC EXTRACT'};
  }
  var lines=text.split(/\r?\n/);
  var oIdx=lines.findIndex(function(l){return /^O\|c=/i.test(l.trim());});
  var replaced=oIdx>=0;
  if(replaced){
    lines[oIdx]=newOLine;
  }else{
    var insertAt=lines.findIndex(function(l){return /^G\|/i.test(l.trim());});
    if(insertAt<0)insertAt=lines.length;
    lines.splice(insertAt,0,newOLine);
  }
  var cVal=(newOLine.match(/^O\|c=(.*)$/i)||[])[1]||'';
  var tapeRaw=nqxResolveRaw(cVal,{});
  var barCount=0;
  if(tapeRaw&&!/^(?:MISSING|U|N\/A)$/i.test(tapeRaw)){
    var legacyRows=tapeRaw.split(/\s*;\s*/).map(function(row){return nqxSplitEscaped(row,',').join('|');}).join(';');
    var tape=parseCandleTape(legacyRows);
    barCount=tape.accepted.length;
    if(barCount===0)return{error:'NO VALID OHLC BAR ACCEPTED FROM RESPONSE'};
  }
  return{text:lines.join('\n'),replaced:replaced,barCount:barCount};
}
```

**この関数は`nqxResolveRaw`・`nqxSplitEscaped`・`parseCandleTape`という既存の関数をそのまま呼び出しているだけであり、新しいパース処理を発明していない。**

---

## 3. 検証手順

### 3-1. モード切替・UI制約の確認

ブラウザでNightwatchを開き、AI INTELLIGENCE UPLINKパネルで`MODE: OHLC / 15M CANDLE EXTRACT ONLY`を選択し、以下を確認する:

- 画像を2枚ドロップしようとした場合、1枚しか追加されず警告が出ること。
- `FRAME ROLES`・`SOURCE FRAMES`入力欄が無効化表示になること。
- イベント検索チェックボックスが非表示になること。
- モードをFAST/DEEPに戻すと、これらの制約が解除されること。

### 3-2. `upMergeOhlcLine` の単体確認（Node上で関数を直接呼び出す、既存追補と同じ手法）

以下のケースを検証する:

1. `srcText`が空文字列 → `{error: 'PASTE OR COMPILE...'}`が返ること。
2. `srcText`が`SAMPLE_NQX`相当（既存の`O|c=MISSING`を含むパケット）で、`newOLine='O|c=18:45,28900,28910,28895,28905;18:48,28905,28912,28898,28900'` → 既存の`O|c=MISSING`行が置換され、`replaced=true`、`barCount=2`となること。
3. `srcText`から`O`行を削除したもの（`M`と`Z`と`E`と`G`は残す）で同じ`newOLine` → `G`行の直前に新規挿入され、`replaced=false`となること。
4. `newOLine='O|c=MISSING'` → `barCount=0`が正しく返り、エラーにならないこと（`MISSING`は有効な値であり、バー0本のエラーとは区別する。現状の疑似コード4節の判定`tapeRaw&&!/^(?:MISSING...)/.test`はこのケースを正しくスキップするはずだが、実装時に確認すること）。
5. 不正なバー（高値<安値等）だけを含む`newOLine` → `{error: 'NO VALID OHLC BAR ACCEPTED...'}`が返り、`#src`が変更されないこと。

### 3-3. エンドツーエンド確認（API呼び出しを含む）

ユーザー自身のAPIキーが必要なため、GPTが実際にAPIを呼び出す検証は本指示書の範囲外としてよい。代わりに、`upCallClaude`/`upCallOpenAI`をモック（固定文字列を返すダミー関数に差し替え）した状態で、`runUplinkOhlc()`の一連の処理（プロンプト生成→レスポンス受信→マージ→`runParse`呼び出し）が例外なく完走することを確認する。

### 3-4. 既存回帰

FAST/DEEPモードの既存の動作（`SAMPLE`ボタンでのデモロード、既存プロンプトの内容、既存の`runUplink`のFAST/DEEP分岐）が本変更によって影響を受けていないことを確認する。既存のNode VM純粋関数実行手法（SLゲート強化の追補で使ったもの）を流用し、`upPromptFast()`・`upPromptNQX()`の出力が変更前と一致することを確認する。

---

## 4. 報告様式

`GPT_IMPLEMENTATION_REPORT.md`に追記専用で新しい節`## OHLC抽出軽量モード完了 (日付)`を追加する。既存の記法（完了条件対照表・検証結果・自己監査節）を踏襲する。加えて:

- `upTokenBudget`・`upTimeoutMs`の`ohlc`モード値をどう決定したか（本指示書の目安値をそのまま使ったか、実測して調整したか）
- 3-2節の5ケースの結果
- 3-4節の既存回帰結果
- 残る既知の限界（あれば。例: 画像が本当に15M表示かどうかをコード側で強制検証する手段はなく、プロンプト内の指示に依存する、等）

---

*本書は既存のAI INTELLIGENCE UPLINKにモードを1つ追加するものであり、既存のFAST/DEEPモード・既存のNQXパケット生成フロー・既存のRisk Gate/Validatorには一切変更を加えない。*

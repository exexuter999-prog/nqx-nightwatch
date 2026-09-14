# R74 — 音声キューの鳴るタイミング(2026-09-09)

ユーザー報告: 「起動時に全部再生される」「正しいタイミングでひとつずつ鳴らない」。
対象は `telegram_mini_app/sound.js`(再生器)と `app.js` の配線。判定・発注経路には触れていない。

## 実測(修正前・Chromium / dev サーバー / `?demo=1` / SOUND 保存 ON)

| 観測 | 意味 |
| --- | --- |
| タップ前に `scenario-armed.wav` の `currentTime` が 2.40s(= クリップの全長) | **起動時に「Armed. Stand by.」が最後まで再生済み** |
| 最初のタップで 8 本の `<audio>` が同時に `muted` 再生→停止 | 解錠の下準備。muted が効く端末では無音、効かない端末では 8 本が一斉に鳴る |
| SOUND スイッチを最初のタップで ON にすると、確認音の `<audio>` が解錠の `settle` に `pause()` される | 「ON にしたのに鳴らない」 |

## 根因

1. **基準が null だった。** `app.js` の起動シーケンスは状態クライアントを作る前に一度 `render()` を呼ぶ
   (view=null)。`observe(null)` がそれを「最初の view = 基準」として飲み込み、最初の snapshot が
   「空 → 全部現れた」の差分として読まれた。建玉があれば「Executed」、武装があれば「Armed」が
   **起動のたびに**鳴る。sound.js 単体のテストは最初から実在 view を渡していたので気付かなかった。
2. **眠った AudioContext に `start()` を溜めていた。** `playBuffer()` は `ctx.state` を見ずに
   `resume()`(非同期)→ `start(0)` していた。iOS / 割り込み後 / touch の pointerdown だけで作られた
   context は操作の外では起きないので、source は溜まり、次のタップで起きた瞬間に**全部が一斉に**鳴る。
3. **解錠の muted 再生が本番の一声と衝突していた。** 同じ `<audio>` 要素を解錠(muted 再生)と本番で
   共用し、解錠の `settle` が「今鳴っているのが本番か」を見ずに `pause()` していた。
4. **権限エラーでも Web Speech に落ちていた。** 音が許されない状況では読み上げも許されないか、
   キューに溜まって後で喋り出すだけ。
5. **解錠が `pointerdown` だけだった。** Chromium の touch は pointerdown を「利用者の操作」と
   認めない(pointerup / touchend が要る)。WebKit は touchend / click。

## 対処(表示層のみ)

- `observe()`: **null は基準にしない。** 最初の実在 view を基準として飲み込む。`rebase(view)` を追加し、
  `app.js` の `onState` は `snapshot` かつ `reason ∈ {initial, visibility}` のとき render より前に
  rebase する(起動時と画面復帰時は「新しく見た」であって「変わった」ではない)。WS の delta と
  reconnect / interval の snapshot は従来どおり差分。デモの出入りも rebase。
- **一声は「今」の出来事。** 直列キュー(前の一声の `ended` まで次を始めない)。眠った context には
  `start()` せず、`RESUME_GRACE_MS`(1.5s)だけ起こすのを待って、起きなければ `<audio>` に落ちる。
  `CUE_TTL_MS`(6s)より待たされた一声は捨てる(遅れて鳴る「Executed」は誤報)。試聴は TTL 無し。
- **解錠は無音で。** 共有の `<audio>` 1 本を無音 WAV(data URI、2ms)で解錠し、本番はその 1 本の
  `src` を差し替えて鳴らす。解錠の settle は `PRIMING` 印を見て、本番が奪っていれば手を引く。
  AudioContext は `resume()` + 無音 1 サンプルの `start()`(iOS の版差対策)。
- Web Speech は**音源の読み込み失敗だけ**。`NotAllowedError` / `AbortError` は静かに捨てる。
- 解錠のリスナーは `pointerdown / pointerup / touchend / click / keydown` に掛け、**外さない**
  (`unlock()` は冪等。iOS の割り込み後も次のタップで起きる)。

## 実測(修正後・同条件)

- タップ前: `voice/` の取得ゼロ(起動時の読み上げ無し)。
- 最初のタップ: `data:` の無音 WAV が 1 本だけ muted/volume 0 で再生→停止。実音源は鳴らない。
  同時に 8 本を `fetch` → `decodeAudioData`。
- デモの SCENE 切替 ARMED→RUNNER→GATES: `buffer.start`(running, 0.88s)→ `buffer.start`(running, 1.05s)
  が切替の瞬間に 1 本ずつ。Web Speech 呼び出しゼロ。

## 回帰テスト

`telegram_mini_app/test/sound_timing.test.mjs`(9)。既存 `sound.test.mjs` / `sound_claim.test.mjs` の
解錠テストは新契約(共有 1 本・無音 WAV)へ書き換え。`npm run ci` 全 270 PASS、予算内。

## 端末側の前提(変わらず)

- SOUND スイッチ ON。画面ロック中・バックグラウンドの WebView では鳴らない(仕様)。
- iOS で一度も画面をタップしていない起動直後は、操作の外の一声は鳴らない(解錠前)。鳴らせない
  一声は捨てるので、後でまとめて鳴ることはもう無い。

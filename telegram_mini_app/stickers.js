/**
 * リザルト背景の GIF ステッカー台帳(2026-08-16: 宇宙プレート版の後継)。
 *
 * 選択は seed(openedAt+entry+symbol の FNV — result.js の seedOf と同一)で
 * 決定論。同じトレードには常に同じステッカーが出る(VERDICTS と同じ原則)。
 * ファイルの実体は public/stickers/<name>.gif。名前は URL にそのまま入るので
 * 英小文字・数字・ハイフンのみ(test/stickers.test.mjs が強制する)。
 *
 * 差し替え方: public/stickers/ に GIF を置き、この台帳に名前を足すだけ。
 */
export const STICKERS = {
  win: [
    // ── 初代7本(2026-08-16): メタル×マッスル系統
    'win-terminator',      // T2 溶鉱炉サムズアップ(金属×火)
    'win-gigachad',        // GigaChad 白黒マッスル
    'win-keyboard',        // マッスルアーム×レトロPC
    'win-cavill',          // Henry Cavill 装填ポーズ×PC
    'win-mafia-skull',     // マフィア髑髏×$スモーク
    'win-hackerman',       // Kung Fury ハッカーマン
    'win-scorpion',        // Mortal Kombat スコーピオン
    // ── 増強22本(同日): かわいい×イキリ+マッチョ追加
    'win-ai-cat',          // AI ダンス猫
    'win-arnold-flex',     // Arnold フレックス
    'win-banana-cat-heart',// バナナ猫×ハート
    'win-cat-sunglasses',  // サングラス猫
    'win-catjam',          // キャップ猫バイブス
    'win-cats-3d-dance',   // 3D 猫ダンス
    'win-chipi-cat',       // chipi chipi(BOOM BOOM)
    'win-chipi-cat-2',     // chipi chipi(DUBI DUBI)
    'win-happy-cat',       // ハッピー猫
    'win-happy-cat-chill', // ごろ寝満足猫
    'win-loaded-money',    // 札束猫
    'win-maxwell-cat',     // Maxwell 回転猫
    'win-maxwell-cat-2',   // Maxwell(手描き)
    'win-oiia-cat',        // OIIA 飛行猫
    'win-popcat',          // POP CAT
    'win-popcat-deal',     // POP CAT(deal with it)
    'win-sigma-cat',       // シグマ猫×ライター
    'win-skeleton-dance',  // 白黒骸骨ダンス(Silly Symphony)
    'win-skeleton-guitar', // メタル骸骨×ギター
    'win-swag-cat',        // スワッグ猫(フーディ×丸眼鏡)
    'win-thug-cat',        // ドヤ顔猫
    'win-uia-cat',         // UIA 宇宙猫
    // ── 第3弾 53本(2026-08-17): 祝勝・マッスル・金・著名ミーム
    'win-bodybuilder', 'win-borat-thumbs', 'win-cat-cool', 'win-cat-driving',
    'win-cat-driving2', 'win-cat-fist-pump', 'win-cat-nod', 'win-cats-jump',
    'win-chad-gigachad', 'win-cool-sunglasses', 'win-cool-thumbs',
    'win-dance-hamster2', 'win-dance-rat', 'win-doge', 'win-flex-check',
    'win-get-money', 'win-goal-celebration', 'win-great-success',
    'win-gym-gains', 'win-hamster-dance', 'win-happy-dance', 'win-keyboard-cat',
    'win-lightweight', 'win-lightweight2', 'win-money-mood', 'win-money-rain',
    'win-old-man-dance', 'win-party-time', 'win-peachcat-yay',
    'win-potemkin-victory', 'win-raccoon-rave', 'win-rain-6m',
    'win-rainbow-money', 'win-ronnie', 'win-shake-it', 'win-shh-cat',
    'win-skeleton-dance2', 'win-skeleton-dance3', 'win-skeletons-dancing',
    'win-vamos-nippon', 'win-gatsby-cheers', 'win-cheers-fireworks',
    'win-fist-pump-yes', 'win-saul-fingers', 'win-bale-laugh', 'win-model-walk',
    'win-dog-dance', 'win-durag-dog', 'win-husky-dance', 'win-cat-ok',
    'win-spongebob-rich', 'win-leo-pointing', 'win-thats-it',
  ],
  loss: [
    // ── 初代2本
    'loss-collapse',       // 骸骨が崩れ落ちる
    'loss-crumble',        // 崩壊しながらサムズアップ
    // ── 増強22本(同日): ズッコケ系統
    'loss-banana-cat-cry', // 泣きバナナ猫
    'loss-bowling-fail',   // ボウリング大失敗
    'loss-cat-dies',       // *dies* 猫
    'loss-cat-fall',       // 黒猫が落ちる
    'loss-cat-fall-funny', // 猫転倒(白黒)
    'loss-cat-stairs',     // 階段から落ちた猫
    'loss-chair-break',    // 椅子が壊れる
    'loss-chair-fail',     // オフィス椅子ズッコケ
    'loss-chair-fall',     // 椅子ごと転倒
    'loss-crying-cat',     // 泣き猫
    'loss-dog-fall',       // 犬ズッコケ
    'loss-dog-fall-2',     // 犬×階段
    'loss-goat-faint',     // 気絶ヤギ(I cant...)
    'loss-goat-fall',      // 気絶ヤギ(硬直)
    'loss-ouch-fall',      // ウォータースライダー事故
    'loss-sad-cat',        // 哀愁の立ち猫
    'loss-skeleton-dead',  // 窓際骸骨 AHH!
    'loss-skeleton-fall',  // 倒れゆく骸骨
    'loss-skeleton-fall-2',// 倒れゆく骸骨(Hey stoopid)
    'loss-skeleton-stan',  // 立ち尽くして倒れる骸骨
    'loss-tom-iron',       // Tom×アイロン
    'loss-walter-fall',    // Walter White 転倒
    // ── 第3弾 39本(2026-08-17): 転倒・悲嘆・著名ミーム
    'loss-angy-cat', 'loss-bike-ice', 'loss-boy-tumbling', 'loss-bro-sad',
    'loss-cat-crazy', 'loss-cat-scared', 'loss-cat-scream', 'loss-crying-dog',
    'loss-dies-cringe', 'loss-drake-no', 'loss-dropping-cake',
    'loss-facepalm-panda', 'loss-facepalm', 'loss-facepalm2',
    'loss-football-fail', 'loss-gta-wasted', 'loss-ok-cat', 'loss-penguin-fall',
    'loss-penguin-oops', 'loss-penguin-slip', 'loss-sad-crying', 'loss-sad-dog',
    'loss-sad-dogge', 'loss-sad-hamster', 'loss-sad-thumbs-cat',
    'loss-skull-deathstorm', 'loss-skull2', 'loss-table-flip',
    'loss-this-is-fine', 'loss-this-is-fine2', 'loss-titanic', 'loss-titanic2',
    'loss-wasted-fall', 'loss-wasted', 'loss-no-god-please', 'loss-carell-no',
    'loss-sad-pablo', 'loss-esteban-wait', 'loss-squidward',
  ],
  flat: [
    // ── 初代2本
    'flat-loading',        // 骸骨×PC「LOADING」待機
    'flat-shrug',          // 骸骨シュラッグ
    // ── 増強6本(同日)
    'flat-cat-judge',      // ジト目猫
    'flat-cat-take-time',  // I AM WAITING....... 猫
    'flat-crypt-chill',    // Just Crypt and Chill 骸骨
    'flat-lazy-cat',       // WAITING FOR MY TRAIN 猫
    'flat-monkey-look',    // 目をそらすパペット猿
    'flat-skeleton-chill', // ハンモック骸骨
    // ── 第3弾 20本(2026-08-17): 待機・脱力・凪
    'flat-bingus', 'flat-bored-pepe', 'flat-capybara-pool', 'flat-capybara',
    'flat-cat-side-eye', 'flat-cat-stare', 'flat-chess-thought',
    'flat-clock-time', 'flat-clocks-ticking', 'flat-gorilla-think',
    'flat-sleep-at-work', 'flat-sleepy-bored', 'flat-sleepy-cat',
    'flat-sloth-slow', 'flat-staring-off', 'flat-waiting-star-trek',
    'flat-zoned-out', 'flat-patiently-waiting', 'flat-coffee',
    'flat-patrick-spongebob',
  ],
};

export function stickerOf(seed, state) {
  const pool = STICKERS[state] || STICKERS.flat;
  return pool[(seed >>> 0) % pool.length];
}

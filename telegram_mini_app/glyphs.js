/**
 * Nightwatch のオリジナル絵文字(グリフ)。
 *
 * 端末の絵文字フォントに依存しないよう、全部インライン SVG。色は
 * currentColor を継承するので、置かれた場所の文字色・点灯状態に追従する。
 * 1em 四方で文字の高さに揃え、ベースラインを少し下げて行の中央に座らせる。
 *
 * 意味の対応(画面上で一貫させる):
 *   signal   ◆ 武装シナリオ(ひし形の的)
 *   position ◈ 建玉(二重ひし形 = 保持)
 *   order    ⇢ 注文(ブローカーへ飛ぶ矢)
 *   closed   ▤ 決済(帳簿の一行)
 *   watch    👁 監視(夜警の目)
 *   auto     ⟲ AUTO(自走する歯車)
 *   ultra    ◌ ULTRA(虹の円環 — CSS の .ultra-icon をそのまま使う)
 *   robot    ⌬ 自動発注エンジン
 *   alert    ⚠ 警告
 *   demo     ✦ デモ・遊び
 *   omen     ☄ 御神籤
 *   up/down  建玉の方向
 */

const SVG = (body, extra = "") =>
  `<svg class="glyph ${extra}" viewBox="0 0 24 24" aria-hidden="true" focusable="false">${body}</svg>`;

export const GLYPH = {
  // 的: ひし形の外枠 + 中心の点。武装したシナリオ。
  signal: SVG(
    `<path d="M12 2.5 21.5 12 12 21.5 2.5 12z" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/>` +
    `<path d="M12 7.5 16.5 12 12 16.5 7.5 12z" fill="currentColor" opacity=".9"/>`),
  // 保持: 二重ひし形。外は細く、内は塗り。
  position: SVG(
    `<path d="M12 2.5 21.5 12 12 21.5 2.5 12z" fill="none" stroke="currentColor" stroke-width="1.4"/>` +
    `<path d="M12 6 18 12 12 18 6 12z" fill="none" stroke="currentColor" stroke-width="1.4"/>` +
    `<circle cx="12" cy="12" r="2" fill="currentColor"/>`),
  // 注文: 右へ飛ぶ矢。軸が点線 = まだ届いていない。
  order: SVG(
    `<path d="M3 12h10" stroke="currentColor" stroke-width="1.8" stroke-dasharray="2.5 2.5" stroke-linecap="round"/>` +
    `<path d="M13 12h7M16 8l4 4-4 4" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>`),
  // 決済: 帳簿の一行が閉じる。
  closed: SVG(
    `<rect x="3.5" y="5" width="17" height="14" rx="2" fill="none" stroke="currentColor" stroke-width="1.5"/>` +
    `<path d="M7 10h10M7 14h6" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/>` +
    `<path d="M15.5 14.5l1.6 1.6 3-3.2" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>`),
  // 監視: 夜警の目。
  watch: SVG(
    `<path d="M2 12s3.6-6.5 10-6.5S22 12 22 12s-3.6 6.5-10 6.5S2 12 2 12z" fill="none" stroke="currentColor" stroke-width="1.6"/>` +
    `<circle cx="12" cy="12" r="3" fill="currentColor"/>` +
    `<circle cx="13" cy="11" r=".9" fill="#000" opacity=".55"/>`),
  // AUTO: 自走する歯車 + 回る矢。
  auto: SVG(
    `<circle cx="12" cy="12" r="3.2" fill="none" stroke="currentColor" stroke-width="1.6"/>` +
    `<path d="M12 2.8v2.6M12 18.6v2.6M2.8 12h2.6M18.6 12h2.6M5.5 5.5l1.8 1.8M16.7 16.7l1.8 1.8M18.5 5.5l-1.8 1.8M7.3 16.7l-1.8 1.8" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/>` +
    `<path d="M16.8 9.2a5.4 5.4 0 0 0-9.2-1.4" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" opacity=".55"/>`),
  // 自動発注エンジン: 六角のコア。
  robot: SVG(
    `<path d="M12 2.8 20 7.4v9.2L12 21.2 4 16.6V7.4z" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/>` +
    `<path d="M9 11h6M9 14h6" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/>` +
    `<circle cx="12" cy="8.3" r="1.1" fill="currentColor"/>`),
  // 警告: 三角 + 感嘆。
  alert: SVG(
    `<path d="M12 3.5 21.5 20H2.5z" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/>` +
    `<path d="M12 9.5v4.5" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>` +
    `<circle cx="12" cy="16.8" r="1.1" fill="currentColor"/>`),
  // デモ: 四芒星。
  demo: SVG(
    `<path d="M12 2.5c.6 5.2 4.3 8.9 9.5 9.5-5.2.6-8.9 4.3-9.5 9.5-.6-5.2-4.3-8.9-9.5-9.5 5.2-.6 8.9-4.3 9.5-9.5z" fill="currentColor"/>`),
  // 御神籤: 流星。
  omen: SVG(
    `<path d="M3 21l8.5-8.5" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>` +
    `<path d="M6 21l4-4M3 18l4-4" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" opacity=".5"/>` +
    `<circle cx="15.5" cy="8.5" r="5" fill="currentColor"/>` +
    `<circle cx="14" cy="7" r="1.4" fill="#000" opacity=".35"/>`),
  // 方向
  up: SVG(`<path d="M12 4.5 19.5 17h-15z" fill="currentColor"/>`),
  down: SVG(`<path d="M12 19.5 4.5 7h15z" fill="currentColor"/>`),
  // 閉じる(▴ の置き換え)
  collapse: SVG(`<path d="M12 7.5 18.5 15h-13z" fill="currentColor"/>`),
};

/** 名前からグリフ HTML を返す。未知の名前は空文字(壊れた記号を出さない)。 */
export function glyph(name, extra = "") {
  const svg = GLYPH[name];
  if (!svg) return "";
  return extra ? svg.replace('class="glyph ', `class="glyph ${extra} `) : svg;
}

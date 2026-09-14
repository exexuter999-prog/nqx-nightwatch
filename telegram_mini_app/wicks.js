/**
 * ヒゲの分割(R61)。icecandles.js(SVG、main チャンク)と icescene.js(3D、遅延チャンク)の両方が
 * 使う純関数を、**依存の無い単独モジュール**に置く。
 *
 * icescene.js が icecandles.js から直接 import していた間、Rollup は遅延チャンク icescene から
 * 起動チャンク(main)への import を作り、`verify:build` の遅延予算(650 kB)が main の 200 kB と
 * CSS を抱えて 957 kB で赤のままだった(2026-09-06 に発見。R61 以降ずっと)。
 */

/**
 * ヒゲを実体の**外側だけ**の 2 本に分ける(2026-09-06 ユーザー: ろうそく足の中のヒゲを消して)。
 * 氷の実体は透けるので、1 本で貫くと中に線が見える。上 = 実体の上端からヒゲの先まで、
 * 下 = 実体の下端からヒゲの先まで。長さ 0 なら描かない。px 座標(下が正)。
 *
 * @param {{cy:number,h:number,wickCy:number,wickH:number}} block
 * @returns {{up:{cy:number,len:number}, down:{cy:number,len:number}}}
 */
export function wickSegments(block) {
  const bodyTop = block.cy - block.h / 2;
  const bodyBottom = block.cy + block.h / 2;
  const wickTop = block.wickCy - block.wickH / 2;
  const wickBottom = block.wickCy + block.wickH / 2;
  const upLen = Math.max(0, bodyTop - wickTop);
  const downLen = Math.max(0, wickBottom - bodyBottom);
  return {
    up: { cy: bodyTop - upLen / 2, len: upLen },
    down: { cy: bodyBottom + downLen / 2, len: downLen },
  };
}

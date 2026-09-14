/**
 * Nightwatch のロゴ・アニメーションアート。
 *
 * 構成: 夜警の目(エンブレム)+ ワードマーク。
 *
 *   1. 起動時(一度だけ): 目の輪郭が筆で描かれ、虹彩が開き、文字が左から
 *      現れる。約 1.8 秒。
 *   2. 待機(常時): 虹彩がゆっくり呼吸し、8〜14 秒に一度まばたきする。
 *      まばたきの間隔は決定論(時刻の秒から導出)で、乱数は使わない。
 *   3. 武装(armed): 虹彩が朱赤に変わり、外周のリングが脈動する。
 *   4. 警戒(alert): 瞼が細くなり、虹彩が収縮する(STALE / HALT の合図)。
 *
 * 描画は SVG と CSS アニメーションだけ。JS は状態のクラス付けと、まばたきの
 * タイマーだけを持つ。prefers-reduced-motion では描画アニメを省き、まばたき
 * も止める(静止した目)。
 */

const EMBLEM = `
<svg class="logo-emblem" viewBox="0 0 64 64" aria-hidden="true" focusable="false">
  <defs>
    <radialGradient id="nw-iris" cx="50%" cy="45%" r="55%">
      <stop offset="0" stop-color="#fff" stop-opacity=".95"/>
      <stop offset=".35" stop-color="currentColor"/>
      <stop offset="1" stop-color="currentColor" stop-opacity=".35"/>
    </radialGradient>
    <clipPath id="nw-lid">
      <path class="logo-lid-clip" d="M4 32s8.5-17 28-17 28 17 28 17-8.5 17-28 17S4 32 4 32z"/>
    </clipPath>
  </defs>
  <!-- 外周のリング(武装時に脈動) -->
  <circle class="logo-ring" cx="32" cy="32" r="29" fill="none" stroke="currentColor" stroke-width="1" opacity=".0"/>
  <!-- 目の輪郭(起動時に筆で描かれる) -->
  <path class="logo-outline" d="M4 32s8.5-17 28-17 28 17 28 17-8.5 17-28 17S4 32 4 32z"
        fill="none" stroke="currentColor" stroke-width="2.2" stroke-linejoin="round" stroke-linecap="round"/>
  <!-- 瞼の内側(まばたきで閉じる) -->
  <g clip-path="url(#nw-lid)">
    <g class="logo-eyeball">
      <circle class="logo-iris" cx="32" cy="32" r="10.5" fill="url(#nw-iris)"/>
      <circle class="logo-pupil" cx="32" cy="32" r="4.6" fill="#070708"/>
      <circle class="logo-glint" cx="35.5" cy="28.5" r="1.7" fill="#fff" opacity=".9"/>
    </g>
    <rect class="logo-blink" x="0" y="0" width="64" height="64" fill="#09090a" transform="scale(1 0)" transform-origin="32 32"/>
  </g>
</svg>`;

const REDUCED = typeof window !== "undefined"
  && window.matchMedia?.("(prefers-reduced-motion: reduce)")?.matches;

/**
 * ロゴを mount に組み立てる。既存のワードマークのテキストはそのまま使う。
 * @returns {{setState(name: string): void, destroy(): void}}
 */
export function initLogo(mount) {
  if (!mount) return { setState() {}, destroy() {} };
  const text = (mount.textContent || "Nightwatch").trim();
  mount.classList.add("logo");
  mount.innerHTML = `${EMBLEM}<span class="logo-word" aria-label="${text}">${
    [...text].map((ch, i) => `<i style="--i:${i}">${ch}</i>`).join("")
  }</span>`;
  if (!REDUCED) mount.classList.add("logo-intro");

  let blinkTimer = 0;
  let destroyed = false;
  const blinkEl = mount.querySelector(".logo-blink");

  const blink = () => {
    if (destroyed || REDUCED || !blinkEl) return;
    blinkEl.classList.remove("is-blinking");
    // 再起動のためにリフロー
    void blinkEl.getBoundingClientRect();
    blinkEl.classList.add("is-blinking");
    scheduleBlink();
  };
  const scheduleBlink = () => {
    // 8〜14 秒。時刻の秒から決定論で決め、乱数は使わない。
    const sec = new Date().getSeconds();
    const wait = 8000 + (sec % 7) * 1000;
    blinkTimer = window.setTimeout(blink, wait);
  };
  if (!REDUCED) scheduleBlink();

  return {
    /** "idle" | "armed" | "alert" */
    setState(name) {
      mount.classList.toggle("is-armed", name === "armed");
      mount.classList.toggle("is-alert", name === "alert");
    },
    destroy() {
      destroyed = true;
      window.clearTimeout(blinkTimer);
    },
  };
}

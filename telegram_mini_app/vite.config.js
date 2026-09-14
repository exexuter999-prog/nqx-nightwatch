import { defineConfig } from "vite";

// R65: GLSL のコメントをビルド時に落とす。
//
// シェーダはテンプレート文字列なので minify に削られず、中の行コメント(日本語 = 1 文字 3 バイト)
// がそのまま出荷される。eye3d.js だけで 5.9 kB あり、瞼の配管を足した時点で遅延チャンクの予算
// (665.6 kB)を 1.3 kB 超えた。上限を緩めるのではなく、「glsl の印(ブロックコメント)+ バッククォート」
// で始まる文字列の中の行コメントだけをここで取り除く(GLSL に文字列リテラルは無いので、
// 二重スラッシュは必ずコメント)。ソースのコメントはそのまま残る —— 削るのは dist だけ。
// ※ この説明をブロックコメントで書くと、印の並び(アスタリスク+スラッシュ)がコメントを閉じて
//   構文エラーになる(2026-09-07 実測)。行コメントで書くこと。
const GLSL_LITERAL = /\/\* glsl \*\/\s*`([\s\S]*?)`/g;
function stripGlslComments() {
  return {
    name: "nqx-strip-glsl-comments",
    enforce: "pre",
    transform(code, id) {
      if (!/\.js$/.test(id) || !code.includes("/* glsl */")) return null;
      const out = code.replace(GLSL_LITERAL, (whole, body) => {
        const stripped = body
          .replace(/\/\/[^\n]*/g, "")          // 行コメント
          .replace(/[ \t]+\n/g, "\n")          // 行末の空白
          .replace(/\n{3,}/g, "\n\n");         // 空行の連打
        return whole.slice(0, whole.indexOf("`")) + "`" + stripped + "`";
      });
      return { code: out, map: null };
    },
  };
}

export default defineConfig({
  base: "./",
  plugins: [stripGlslComments()],
  build: {
    target: "es2020",
    // Public Pages deployment does not need source maps.  Keeping them out of
    // dist avoids publishing implementation source and a multi-megabyte asset.
    sourcemap: false,
    // The initial application shell has a 500 kB operating budget; the
    // separately lazy 3D renderer has an explicit 650 kB budget.
    chunkSizeWarningLimit: 650,
    // CI reads Rollup's actual import graph; warnings alone are not a budget.
    manifest: true,
    rollupOptions: {
      // R31: レベル一覧フィードは **別エントリ**。本体の起動チャンクに
      // 混ぜないことで、発注画面が一切重くならない。
      input: {
        main: "index.html",
        levels: "levels.html",
        // R38: Claude との対話面。発注画面の起動チャンクには混ぜない。
        chat: "chat.html",
      },
      output: {
        manualChunks: {
          "three-renderer": ["three", "gsap", "postprocessing"],
        },
      },
    },
  }
});

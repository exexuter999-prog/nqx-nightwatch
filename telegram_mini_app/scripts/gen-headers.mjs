/**
 * `public/_headers` を生成する。Cloudflare Pages はこのファイルを読み、
 * 配信するすべての応答にヘッダを足す。
 *
 * ここが守るもの:
 *   - **CSP**: 起動 URL に載る launch token と Telegram の initData を、
 *     ページ内で動くコードから外へ持ち出させない。script-src を自分と
 *     telegram.org に限れば、第三者の JS はそもそも実行されない。
 *     connect-src を自分の Worker に限れば、万一実行されても送り先が無い。
 *   - **Referrer-Policy**: 外部(フォント CDN 等)への要求に、token を含む
 *     URL を載せない。
 *
 * connect-src だけは環境ごとに変わるので、`NQX_API_BASE`(環境変数)か
 * `../.secrets/nqx_cloud.env` から解決してここで焼き込む。解決できない
 * ときはビルドを止める — 繋がらない CSP や、緩い CSP を配るより良い。
 */
import fs from "node:fs";
import path from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const secretsEnv = path.resolve(root, "..", ".secrets", "nqx_cloud.env");
const outFile = path.join(root, "public", "_headers");

function apiBaseFromEnvFile(file) {
  if (!fs.existsSync(file)) return "";
  for (const line of fs.readFileSync(file, "utf8").split(/\r?\n/)) {
    const text = line.trim();
    if (!text || text.startsWith("#") || !text.includes("=")) continue;
    const [key, ...rest] = text.split("=");
    if (key.trim() === "NQX_API_BASE") return rest.join("=").trim();
  }
  return "";
}

const apiBase = (process.env.NQX_API_BASE || apiBaseFromEnvFile(secretsEnv) || "").trim();
if (!apiBase) {
  console.error(
    "ERROR: NQX_API_BASE を解決できません。CSP の connect-src を確定できないので中止します。\n" +
    `  ${secretsEnv} に NQX_API_BASE を書くか、環境変数で渡してください。`,
  );
  process.exit(1);
}

let origin;
try {
  origin = new URL(apiBase).origin;
} catch {
  console.error(`ERROR: NQX_API_BASE が URL として読めません: ${apiBase}`);
  process.exit(1);
}
const wsOrigin = origin.replace(/^https:/, "wss:").replace(/^http:/, "ws:");

const csp = [
  "default-src 'self'",
  "base-uri 'none'",
  "object-src 'none'",
  "form-action 'none'",
  // Telegram Web は Mini App を iframe で開く。ネイティブ WebView は影響を受けない。
  "frame-ancestors 'self' https://web.telegram.org",
  // 第三者の JS を実行させない。3D も含め、資産はすべて自分のオリジンから来る。
  "script-src 'self' https://telegram.org",
  // インライン style は既存の HTML と DOM 生成が使う。XSS を止めるのは script-src。
  "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
  "font-src 'self' https://fonts.gstatic.com data:",
  "img-src 'self' data: blob:",
  "media-src 'self' data: blob:",
  // 送り先を自分の Worker だけに固定する(資格情報の持ち出し防止)。
  `connect-src 'self' ${origin} ${wsOrigin}`,
  "worker-src 'self' blob:",
].join("; ");

const body = `# 自動生成 — 直接編集しない。scripts/gen-headers.mjs が npm run build のたびに書き直す。
# connect-src の宛先は .secrets/nqx_cloud.env の NQX_API_BASE から来る。
/*
  Content-Security-Policy: ${csp}
  Referrer-Policy: no-referrer
  X-Content-Type-Options: nosniff
  Cross-Origin-Opener-Policy: same-origin
  Permissions-Policy: accelerometer=(), camera=(), display-capture=(), geolocation=(), gyroscope=(), magnetometer=(), microphone=(), midi=(), payment=(), usb=()
`;

fs.mkdirSync(path.dirname(outFile), { recursive: true });
fs.writeFileSync(outFile, body, "utf8");
console.log(`_headers written (connect-src ${origin} ${wsOrigin})`);

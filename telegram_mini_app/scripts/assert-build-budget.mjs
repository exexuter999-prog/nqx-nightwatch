import fs from "node:fs";
import path from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const dist = path.join(root, "dist");
const manifestPath = path.join(dist, ".vite", "manifest.json");
const manifest = JSON.parse(fs.readFileSync(manifestPath, "utf8"));
// R38: エントリが複数ある(index / levels / chat)。予算を測るのは
// **発注画面**の index.html であって、たまたま先頭に来たものではない。
// `find` のままだと chat.html を拾い、「lazy import が無い」で落ちていた。
const MAIN_ENTRY = "index.html";
const entryKey = Object.keys(manifest)
  .find((key) => manifest[key].isEntry && key === MAIN_ENTRY);
if (!entryKey) {
  const entries = Object.keys(manifest).filter((key) => manifest[key].isEntry);
  throw new Error(`build manifest has no ${MAIN_ENTRY} entry (found: ${entries.join(", ")})`);
}

const bytes = (file) => fs.statSync(path.join(dist, file)).size;
const closure = (seed) => {
  const seen = new Set();
  const visit = (key) => {
    if (seen.has(key)) return;
    const item = manifest[key];
    if (!item) throw new Error(`manifest import missing: ${key}`);
    seen.add(key);
    for (const imported of item.imports || []) visit(imported);
  };
  visit(seed);
  const assets = new Set();
  for (const key of seen) {
    const item = manifest[key];
    assets.add(item.file);
    for (const css of item.css || []) assets.add(css);
  }
  return { keys: [...seen], assets: [...assets], total: [...assets].reduce((sum, file) => sum + bytes(file), 0) };
};

const initial = closure(entryKey);
const initialBudget = 500 * 1024;
if (initial.total > initialBudget) {
  throw new Error(`initial dependency budget exceeded: ${initial.total} > ${initialBudget} bytes`);
}
const dynamic = manifest[entryKey].dynamicImports || [];
if (!dynamic.length) throw new Error("expected a lazy renderer dynamic import");
const lazyBudget = 650 * 1024;
const lazyWarningThreshold = 625 * 1024;
const lazy = dynamic.map((key) => ({ key, ...closure(key) }));
for (const item of lazy) {
  if (item.total > lazyBudget) {
    throw new Error(`lazy dependency budget exceeded (${item.key}): ${item.total} > ${lazyBudget} bytes`);
  }
  if (item.total > lazyWarningThreshold) {
    console.warn(`lazy dependency warning (${item.key}): ${item.total} > ${lazyWarningThreshold} bytes`);
  }
}
console.log(JSON.stringify({
  initial: { bytes: initial.total, budget: initialBudget, assets: initial.assets },
  lazy: lazy.map((item) => ({ key: item.key, bytes: item.total,
    warningThreshold: lazyWarningThreshold, budget: lazyBudget,
    headroom: lazyBudget - item.total, assets: item.assets })),
}, null, 2));

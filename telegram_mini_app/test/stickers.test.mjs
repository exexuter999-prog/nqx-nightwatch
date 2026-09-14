// リザルト背景 GIF ステッカーの台帳とファイル実体の整合、決定論選択。
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { STICKERS, stickerOf } from '../stickers.js';

const here = path.dirname(fileURLToPath(import.meta.url));
const dir = path.join(here, '..', 'public', 'stickers');

test('ステッカー台帳は空プールなし・実ファイルと1対1', () => {
  assert.ok(STICKERS.win.length > 0 && STICKERS.loss.length > 0 && STICKERS.flat.length > 0);
  const listed = Object.values(STICKERS).flat();
  for (const name of listed) {
    assert.match(name, /^[a-z0-9-]+$/, `URL に入れない名前: ${name}`);
    assert.ok(fs.existsSync(path.join(dir, `${name}.gif`)), `実体が無い: ${name}.gif`);
  }
  const files = fs.readdirSync(dir).filter(f => f.endsWith('.gif')).map(f => f.slice(0, -4));
  for (const f of files) assert.ok(listed.includes(f), `台帳に無い孤児: ${f}.gif`);
});

test('選択は決定論で、必ず該当プールの中から出る', () => {
  assert.equal(stickerOf(12345, 'win'), stickerOf(12345, 'win'));
  for (const state of ['win', 'loss', 'flat'])
    for (const seed of [0, 1, 7, 1755300000, 0xffffffff])
      assert.ok(STICKERS[state].includes(stickerOf(seed, state)));
  // 未知の state は flat に落ちる(派手に間違えない)
  assert.ok(STICKERS.flat.includes(stickerOf(3, 'unknown')));
});

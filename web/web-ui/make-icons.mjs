#!/usr/bin/env node
import fs from 'node:fs';
import path from 'node:path';
import zlib from 'node:zlib';
import { fileURLToPath } from 'node:url';

const T = (() => { const t = new Int32Array(256); for (let n = 0; n < 256; n++) { let c = n; for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1; t[n] = c; } return t; })();
const crc = (b) => { let c = ~0; for (const x of b) c = T[(c ^ x) & 255] ^ (c >>> 8); return ~c >>> 0; };
function chunk(type, data) {
  const len = Buffer.alloc(4); len.writeUInt32BE(data.length);
  const td = Buffer.concat([Buffer.from(type, 'latin1'), data]);
  const cr = Buffer.alloc(4); cr.writeUInt32BE(crc(td));
  return Buffer.concat([len, td, cr]);
}
function png(w, h, rgba) {
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(w, 0); ihdr.writeUInt32BE(h, 4); ihdr[8] = 8; ihdr[9] = 6;
  const raw = Buffer.alloc((w * 4 + 1) * h);
  for (let y = 0; y < h; y++) { raw[y * (w * 4 + 1)] = 0; rgba.copy(raw, y * (w * 4 + 1) + 1, y * w * 4, (y + 1) * w * 4); }
  return Buffer.concat([
    Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]),
    chunk('IHDR', ihdr), chunk('IDAT', zlib.deflateSync(raw, { level: 9 })), chunk('IEND', Buffer.alloc(0)),
  ]);
}
function icon(s) {
  const b = Buffer.alloc(s * s * 4);
  const cx = s / 2, cy = s / 2, hw = 0.40 * s, hh = 0.28 * s, r = 0.07 * s, st = 0.065 * s;
  const kx = 0.058 * s, ky = 0.088 * s;
  for (let y = 0; y < s; y++) for (let x = 0; x < s; x++) {
    const i = (y * s + x) * 4; let R = 255, G = 255, B = 255;
    const px = x + 0.5, py = y + 0.5;
    const dx = Math.max(Math.abs(px - cx) - (hw - r), 0), dy = Math.max(Math.abs(py - cy) - (hh - r), 0);
    const d = Math.hypot(dx, dy) - r;
    const cov = Math.max(0, Math.min(1, st / 2 - Math.abs(d) + 0.5));
    if (cov > 0) { R = Math.round(255 * (1 - cov) + 10 * cov); G = R; B = R; }
    if (Math.abs(px - cx) < kx && Math.abs(py - cy) < ky) { R = 0x25; G = 0x63; B = 0xeb; }
    b[i] = R; b[i + 1] = G; b[i + 2] = B; b[i + 3] = 255;
  }
  return png(s, s, b);
}
const dir = path.join(path.dirname(fileURLToPath(import.meta.url)), 'icons');
fs.mkdirSync(dir, { recursive: true });
for (const [name, size] of [['icon-192', 192], ['icon-512', 512], ['apple-touch-icon', 180]]) {
  fs.writeFileSync(path.join(dir, name + '.png'), icon(size));
  console.log('wrote icons/' + name + '.png');
}
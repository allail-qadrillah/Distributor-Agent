import test from 'node:test';
import assert from 'node:assert/strict';
import QRCode from 'qrcode';
import { encodeQR, renderTerminal } from '../src/qr.js';

// Oracle: the dev-only `qrcode` reference package, forced to BYTE mode
// (single segment) with a pinned version/level/mask, must produce the
// exact same module matrix as our in-repo encoder.
function referenceMatrix(text, version, level, mask) {
  const qr = QRCode.create([{ data: text, mode: 'byte' }], {
    version,
    errorCorrectionLevel: level,
    maskPattern: mask,
  });
  const { size, data } = qr.modules;
  const modules = [];
  for (let r = 0; r < size; r++) {
    modules.push(Array.from(data.slice(r * size, (r + 1) * size)));
  }
  return { size, modules };
}

function assertMatchesReference(text, version, level, mask) {
  const mine = encodeQR(text, { version, level, mask });
  const ref = referenceMatrix(text, version, level, mask);
  assert.equal(mine.size, ref.size, 'size mismatch');
  const mineRows = mine.modules.map((row) => Array.from(row));
  assert.deepEqual(mineRows, ref.modules, `matrix mismatch v${version}/${level}/m${mask}`);
}

test('qr matrix matches reference for a short payload (v1-L)', () => {
  assertMatchesReference('halo', 1, 'L', 0);
});

test('qr matrix matches reference across every mask (v2-M)', () => {
  for (let mask = 0; mask < 8; mask++) {
    assertMatchesReference('distributor bridge', 2, 'M', mask);
  }
});

test('qr matrix matches reference at version 6-L', () => {
  assertMatchesReference('x'.repeat(100), 6, 'L', 3);
});

test('qr matrix matches reference at a 16-bit char-count version (v10-M)', () => {
  assertMatchesReference('y'.repeat(200), 10, 'M', 5);
});

test('qr matrix matches reference with multibyte utf-8 payload (v4-M)', () => {
  assertMatchesReference('mõõduka pikkusega tekst ühest testist ✓', 4, 'M', 2);
});

test('qr auto-selects the smallest version that fits', () => {
  const qr = encodeQR('2@pairing-payload-example', { level: 'L' });
  assert.equal(qr.version, 2); // 24 bytes needs v2-L (34 cw data capacity)
  assert.ok(qr.mask >= 0 && qr.mask <= 7);
});

test('qr auto mode yields a matrix whose format bits decode back', () => {
  const qr = encodeQR('auto mask selection payload', { level: 'M' });
  // read the 15 format bits (MSB first) from the copy around the
  // top-left finder and verify BCH + level/mask round-trip
  const { modules, level, mask } = qr;
  const cells = [
    [8, 0], [8, 1], [8, 2], [8, 3], [8, 4], [8, 5], [8, 7],
    [8, 8], [7, 8], [5, 8], [4, 8], [3, 8], [2, 8], [1, 8], [0, 8],
  ];
  let value = 0;
  for (const [r, c] of cells) value = (value << 1) | modules[r][c];
  const unmasked = value ^ 0x5412;
  const data = unmasked >> 10;
  // recompute the BCH remainder of the 5-bit data over G15 (0x537)
  let d = data << 10;
  while (bchDigit(d) >= 11) d ^= 0x537 << (bchDigit(d) - 11);
  assert.equal((data << 10) | d, unmasked, 'BCH remainder must round-trip');
  const levelBits = { M: 0, L: 1 };
  assert.equal(data, (levelBits[level] << 3) | mask, 'level+mask must round-trip');
});

function bchDigit(value) {
  let digit = 0;
  while (value !== 0) { digit++; value >>>= 1; }
  return digit;
}

test('renderTerminal produces half-block lines with quiet zone', () => {
  const qr = encodeQR('terminal', { level: 'L', version: 2, mask: 1 });
  const text = renderTerminal(qr, { quietZone: 4 });
  const lines = text.split('\n');
  assert.equal(lines.length, Math.ceil((qr.size + 8) / 2));
  assert.ok(lines.every((line) => line.length === qr.size + 8));
  assert.ok(/█/.test(lines[2]) || /▀/.test(lines[2]), 'finder pattern visible');
});

test('renderTerminal quiet zone is all light modules', () => {
  const qr = encodeQR('terminal', { level: 'L', version: 2, mask: 1 });
  const lines = renderTerminal(qr, { quietZone: 4 }).split('\n');
  assert.ok(!/\S/.test(lines[0]), 'top quiet zone is blank');
  assert.ok(!/\S/.test(lines[lines.length - 1]), 'bottom quiet zone is blank');
  assert.ok(lines.every((line) => !/\S/.test(line.slice(0, 4))), 'left quiet zone is blank');
});

test('large payloads choose higher versions without error', () => {
  const qr = encodeQR('z'.repeat(400), { level: 'M' });
  assert.ok(qr.version >= 13);
  assert.equal(qr.size, 17 + 4 * qr.version);
});

// Byte-mode QR encoder (ISO/IEC 18004), levels L and M, versions 1-40,
// with auto version + mask selection. Implemented in-repo because the
// approved dependency list allows no QR runtime dependency (Section 3)
// while Section 8 requires terminal QR display. Correctness is proven in
// tests/qr.test.js against the dev-only `qrcode` reference package.
//
// Algorithm follows the standard structure (Arase's public reference
// implementation of the spec): data encoding -> Reed-Solomon blocks ->
// interleaving -> matrix function patterns -> zigzag placement -> mask.

const EC_BLOCKS = {
  L: [1, 1, 1, 1, 1, 2, 2, 2, 2, 4, 4, 4, 4, 4, 6, 6, 6, 6, 7, 8, 8, 9, 9, 10, 12, 12, 12, 13, 14, 15, 16, 17, 18, 19, 19, 20, 21, 22, 24, 25],
  M: [1, 1, 1, 2, 2, 4, 4, 4, 5, 5, 5, 8, 9, 9, 10, 10, 11, 13, 14, 16, 17, 17, 18, 20, 21, 23, 25, 26, 28, 29, 31, 33, 35, 37, 38, 40, 43, 45, 47, 49],
};
const EC_CODEWORDS = {
  L: [7, 10, 15, 20, 26, 36, 40, 48, 60, 72, 80, 96, 104, 120, 132, 144, 168, 180, 196, 224, 224, 252, 270, 300, 312, 336, 360, 390, 420, 450, 480, 510, 540, 570, 570, 600, 630, 660, 720, 750],
  M: [10, 16, 26, 36, 48, 64, 72, 88, 110, 130, 150, 176, 198, 216, 240, 280, 308, 338, 364, 416, 442, 476, 504, 560, 588, 644, 700, 728, 784, 812, 868, 924, 980, 1036, 1064, 1120, 1204, 1260, 1316, 1372],
};
// format info level indicator bits (format info, not version info)
const LEVEL_BITS = { L: 1, M: 0 };

const EXP = new Uint8Array(512);
const LOG = new Uint8Array(256);
{
  let x = 1;
  for (let i = 0; i < 255; i++) {
    EXP[i] = x;
    LOG[x] = i;
    x <<= 1;
    if (x & 0x100) x ^= 0x11d; // GF(256) primitive polynomial
  }
  for (let i = 255; i < 512; i++) EXP[i] = EXP[i - 255];
}
const gfMul = (a, b) => (a === 0 || b === 0 ? 0 : EXP[LOG[a] + LOG[b]]);

function polyMul(p, q) {
  const out = new Uint8Array(p.length + q.length - 1);
  for (let i = 0; i < p.length; i++) {
    for (let j = 0; j < q.length; j++) out[i + j] ^= gfMul(p[i], q[j]);
  }
  return out;
}
function polyMod(dividend, divisor) {
  let rem = new Uint8Array(dividend);
  while (rem.length - divisor.length >= 0) {
    const factor = rem[0];
    for (let i = 0; i < divisor.length; i++) rem[i] ^= gfMul(divisor[i], factor);
    let offset = 0;
    while (offset < rem.length && rem[offset] === 0) offset++;
    rem = rem.slice(offset);
  }
  return rem;
}
function generatorPoly(degree) {
  let poly = new Uint8Array([1]);
  for (let i = 0; i < degree; i++) poly = polyMul(poly, new Uint8Array([1, EXP[i]]));
  return poly;
}
function rsEncode(data, ecCount) {
  const gen = generatorPoly(ecCount);
  const padded = new Uint8Array(data.length + ecCount);
  padded.set(data);
  const remainder = polyMod(padded, gen);
  const out = new Uint8Array(ecCount);
  const start = ecCount - remainder.length;
  out.set(remainder, start > 0 ? start : 0);
  return out;
}

const symbolSize = (v) => 17 + 4 * v;
// total data+EC codewords per version (ISO/IEC 18004 table)
const TOTAL_CODEWORDS = [
  26, 44, 70, 100, 134, 172, 196, 242, 292, 346,
  404, 466, 532, 581, 655, 733, 815, 901, 991, 1085,
  1156, 1258, 1364, 1474, 1588, 1706, 1828, 1921, 2051, 2185,
  2323, 2465, 2611, 2761, 2876, 3034, 3196, 3362, 3532, 3706,
];
const totalCodewords = (v) => TOTAL_CODEWORDS[v - 1];

class BitBuffer {
  constructor() { this.buffer = []; this.length = 0; }
  put(value, bits) {
    for (let i = bits - 1; i >= 0; i--) this.putBit((value >>> i) & 1);
  }
  putBit(bit) {
    const byteIndex = this.length >> 3;
    if (this.buffer.length <= byteIndex) this.buffer.push(0);
    if (bit) this.buffer[byteIndex] |= 0x80 >>> (this.length & 7);
    this.length++;
  }
  get bytes() { return Uint8Array.from(this.buffer); }
}

function charCountBits(version) {
  return version < 10 ? 8 : 16; // byte mode
}

function encodeData(bytes, version, level) {
  const total = totalCodewords(version);
  const ecTotal = EC_CODEWORDS[level][version - 1];
  const dataTotal = total - ecTotal;

  const buffer = new BitBuffer();
  buffer.put(4, 4); // byte mode indicator
  buffer.put(bytes.length, charCountBits(version));
  for (const byte of bytes) buffer.put(byte, 8);
  if (buffer.length + 4 <= dataTotal * 8) buffer.put(0, 4);
  while (buffer.length % 8 !== 0) buffer.putBit(0);
  let padToggle = 0;
  while (buffer.length < dataTotal * 8) {
    buffer.put(padToggle ? 0x11 : 0xec, 8);
    padToggle ^= 1;
  }

  // Reed-Solomon blocks (group 1 blocks hold floor, group 2 hold +1)
  const blocks = EC_BLOCKS[level][version - 1];
  const blocksInGroup2 = total % blocks;
  const blocksInGroup1 = blocks - blocksInGroup2;
  const totalInGroup1 = Math.floor(total / blocks);
  const dataInGroup1 = Math.floor(dataTotal / blocks);
  const ecCount = totalInGroup1 - dataInGroup1;

  const data = buffer.bytes;
  const dc = [];
  const ec = [];
  let offset = 0;
  for (let b = 0; b < blocks; b++) {
    const size = b < blocksInGroup1 ? dataInGroup1 : dataInGroup1 + 1;
    dc.push(data.slice(offset, offset + size));
    ec.push(rsEncode(dc[b], ecCount));
    offset += size;
  }
  const out = new Uint8Array(total);
  let index = 0;
  const maxData = Math.max(...dc.map((b) => b.length));
  for (let i = 0; i < maxData; i++) {
    for (const block of dc) if (i < block.length) out[index++] = block[i];
  }
  for (let i = 0; i < ecCount; i++) {
    for (const block of ec) out[index++] = block[i];
  }
  return out;
}

function makeMatrix(version) {
  const size = symbolSize(version);
  const modules = Array.from({ length: size }, () => new Uint8Array(size));
  const reserved = Array.from({ length: size }, () => new Uint8Array(size));
  const set = (row, col, dark, isReserved) => {
    modules[row][col] = dark ? 1 : 0;
    if (isReserved) reserved[row][col] = 1;
  };
  return { size, modules, reserved, set };
}

function drawFinder({ size, set }, row, col) {
  for (let r = -1; r <= 7; r++) {
    for (let c = -1; c <= 7; c++) {
      if (row + r < 0 || size <= row + r || col + c < 0 || size <= col + c) continue;
      const dark = (r >= 0 && r <= 6 && (c === 0 || c === 6))
        || (c >= 0 && c <= 6 && (r === 0 || r === 6))
        || (r >= 2 && r <= 4 && c >= 2 && c <= 4);
      set(row + r, col + c, dark, true);
    }
  }
}

function alignmentPositions(version) {
  if (version === 1) return [];
  const size = symbolSize(version);
  const posCount = Math.floor(version / 7) + 2;
  const intervals = size === 145
    ? 26
    : Math.ceil((size - 13) / (2 * posCount - 2)) * 2;
  const coords = [size - 7];
  for (let i = 1; i < posCount - 1; i++) coords[i] = coords[i - 1] - intervals;
  coords.push(6);
  coords.reverse();
  const positions = [];
  const n = coords.length;
  for (let i = 0; i < n; i++) {
    for (let j = 0; j < n; j++) {
      if ((i === 0 && j === 0) || (i === 0 && j === n - 1) || (i === n - 1 && j === 0)) continue;
      positions.push([coords[i], coords[j]]);
    }
  }
  return positions;
}

function drawAlignment(matrix, version) {
  for (const [row, col] of alignmentPositions(version)) {
    for (let r = -2; r <= 2; r++) {
      for (let c = -2; c <= 2; c++) {
        const dark = r === -2 || r === 2 || c === -2 || c === 2 || (r === 0 && c === 0);
        matrix.set(row + r, col + c, dark, true);
      }
    }
  }
}

function drawTiming(matrix) {
  const { size, set } = matrix;
  for (let i = 8; i < size - 8; i++) {
    const value = i % 2 === 0;
    set(i, 6, value, true);
    set(6, i, value, true);
  }
}

const G15 = (1 << 10) | (1 << 8) | (1 << 5) | (1 << 4) | (1 << 2) | (1 << 1) | 1;
const G15_MASK = (1 << 14) | (1 << 12) | (1 << 10) | (1 << 4) | (1 << 1);
const G18 = (1 << 12) | (1 << 11) | (1 << 10) | (1 << 9) | (1 << 8) | (1 << 5) | (1 << 2) | 1;
const bchDigit = (value) => {
  let digit = 0;
  while (value !== 0) { digit++; value >>>= 1; }
  return digit;
};
const G15_BCH = bchDigit(G15);
const G18_BCH = bchDigit(G18);

function formatBits(level, mask) {
  const data = (LEVEL_BITS[level] << 3) | mask;
  let d = data << 10;
  while (bchDigit(d) - G15_BCH >= 0) d ^= G15 << (bchDigit(d) - G15_BCH);
  return ((data << 10) | d) ^ G15_MASK;
}

function versionBits(version) {
  let d = version << 12;
  while (bchDigit(d) - G18_BCH >= 0) d ^= G18 << (bchDigit(d) - G18_BCH);
  return (version << 12) | d;
}

function drawFormat(matrix, level, mask) {
  const { size, set } = matrix;
  const bits = formatBits(level, mask);
  for (let i = 0; i < 15; i++) {
    const mod = ((bits >> i) & 1) === 1;
    if (i < 6) set(i, 8, mod, true);
    else if (i < 8) set(i + 1, 8, mod, true);
    else set(size - 15 + i, 8, mod, true);

    if (i < 8) set(8, size - i - 1, mod, true);
    else if (i < 9) set(8, 15 - i, mod, true);
    else set(8, 14 - i, mod, true);
  }
  set(size - 8, 8, true, true); // dark module
}

function drawVersion(matrix, version) {
  if (version < 7) return;
  const { size, set } = matrix;
  const bits = versionBits(version);
  for (let i = 0; i < 18; i++) {
    const mod = ((bits >> i) & 1) === 1;
    const row = Math.floor(i / 3);
    const col = i % 3 + size - 8 - 3;
    set(row, col, mod, true);
    set(col, row, mod, true);
  }
}

function placeData(matrix, codewords) {
  const { size, modules, reserved, set } = matrix;
  let inc = -1;
  let row = size - 1;
  let bitIndex = 7;
  let byteIndex = 0;
  for (let col = size - 1; col > 0; col -= 2) {
    if (col === 6) col--;
    for (;;) {
      for (let c = 0; c < 2; c++) {
        if (!reserved[row][col - c]) {
          let dark = false;
          if (byteIndex < codewords.length) {
            dark = ((codewords[byteIndex] >>> bitIndex) & 1) === 1;
          }
          set(row, col - c, dark, false);
          bitIndex--;
          if (bitIndex === -1) { byteIndex++; bitIndex = 7; }
        }
      }
      row += inc;
      if (row < 0 || size <= row) { row -= inc; inc = -inc; break; }
    }
  }
}

const MASKS = [
  (r, c) => (r + c) % 2 === 0,
  (r, c) => r % 2 === 0,
  (r, c) => c % 3 === 0,
  (r, c) => (r + c) % 3 === 0,
  (r, c) => (Math.floor(r / 2) + Math.floor(c / 3)) % 2 === 0,
  (r, c) => ((r * c) % 2) + ((r * c) % 3) === 0,
  (r, c) => (((r * c) % 2) + ((r * c) % 3)) % 2 === 0,
  (r, c) => (((r + c) % 2) + ((r * c) % 3)) % 2 === 0,
];

function applyMask(matrix, mask) {
  const { size, modules, reserved } = matrix;
  const fn = MASKS[mask];
  for (let row = 0; row < size; row++) {
    for (let col = 0; col < size; col++) {
      if (!reserved[row][col] && fn(row, col)) modules[row][col] ^= 1;
    }
  }
}

function penaltyScore(matrix, level) {
  const { size, modules } = matrix;
  // N1: runs of same color >= 5
  let score = 0;
  for (let row = 0; row < size; row++) {
    let runColor = -1;
    let runLength = 1;
    for (let col = 0; col < size; col++) {
      const color = modules[row][col];
      if (color === runColor) {
        runLength++;
        if (runLength === 5) score += 3;
        else if (runLength > 5) score += 1;
      } else {
        runColor = color;
        runLength = 1;
      }
    }
  }
  for (let col = 0; col < size; col++) {
    let runColor = -1;
    let runLength = 1;
    for (let row = 0; row < size; row++) {
      const color = modules[row][col];
      if (color === runColor) {
        runLength++;
        if (runLength === 5) score += 3;
        else if (runLength > 5) score += 1;
      } else {
        runColor = color;
        runLength = 1;
      }
    }
  }
  // N2: 2x2 blocks
  for (let row = 0; row < size - 1; row++) {
    for (let col = 0; col < size - 1; col++) {
      const c = modules[row][col];
      if (c === modules[row][col + 1] && c === modules[row + 1][col] && c === modules[row + 1][col + 1]) {
        score += 3;
      }
    }
  }
  // N3: finder-like pattern 1011101 with 4 light modules on one side
  const finderLike = [1, 0, 1, 1, 1, 0, 1, 0, 0, 0, 0];
  const inRange = (row, col) => row >= 0 && row < size && col >= 0 && col < size;
  for (let row = 0; row < size; row++) {
    for (let col = 0; col < size; col++) {
      for (const [dr, dc] of [[0, 1], [1, 0]]) {
        let matches = true;
        for (let i = 0; i < finderLike.length; i++) {
          const r = row + dr * i;
          const c = col + dc * i;
          if (!inRange(r, c) || modules[r][c] !== finderLike[i]) { matches = false; break; }
        }
        if (matches) score += 40;
      }
    }
  }
  // N4: dark module ratio
  let dark = 0;
  for (let row = 0; row < size; row++) {
    for (let col = 0; col < size; col++) dark += modules[row][col];
  }
  const ratio = (dark / (size * size)) * 100;
  score += Math.floor(Math.abs(ratio - 50) / 5) * 10;
  void level;
  return score;
}

function dataCapacityBytes(version, level) {
  return totalCodewords(version) - EC_CODEWORDS[level][version - 1];
}

export function encodeQR(text, { level = 'L', version = null, mask = null } = {}) {
  const bytes = Buffer.from(String(text), 'utf8');
  let v = version;
  if (v == null) {
    for (let candidate = 1; candidate <= 40; candidate++) {
      const needed = 4 + charCountBits(candidate) + bytes.length * 8;
      if (needed <= dataCapacityBytes(candidate, level) * 8) { v = candidate; break; }
    }
    if (v == null) throw new Error('QR payload too large');
  }
  const codewords = encodeData(bytes, v, level);
  const matrix = makeMatrix(v);
  drawFinder(matrix, 0, 0);
  drawFinder(matrix, 0, matrix.size - 7);
  drawFinder(matrix, matrix.size - 7, 0);
  drawTiming(matrix);
  drawAlignment(matrix, v);
  drawFormat(matrix, level, 0); // reserve format areas before data
  drawVersion(matrix, v);
  placeData(matrix, codewords);
  let chosenMask = mask;
  if (chosenMask == null) {
    let bestScore = Infinity;
    for (let candidate = 0; candidate < 8; candidate++) {
      applyMask(matrix, candidate);
      drawFormat(matrix, level, candidate);
      const score = penaltyScore(matrix, level);
      if (score < bestScore) { bestScore = score; chosenMask = candidate; }
      applyMask(matrix, candidate); // undo (mask is an involution)
      drawFormat(matrix, level, 0);
    }
  }
  applyMask(matrix, chosenMask);
  drawFormat(matrix, level, chosenMask);
  return { version: v, level, mask: chosenMask, size: matrix.size, modules: matrix.modules };
}

export function renderTerminal(matrix, { quietZone = 4 } = {}) {
  const { size, modules } = matrix;
  const lines = [];
  const total = size + quietZone * 2;
  const at = (row, col) => {
    const r = row - quietZone;
    const c = col - quietZone;
    if (r < 0 || c < 0 || r >= size || c >= size) return 0;
    return modules[r][c];
  };
  for (let row = 0; row < total; row += 2) {
    let line = '';
    for (let col = 0; col < total; col++) {
      const top = at(row, col);
      const bottom = at(row + 1, col);
      line += top && bottom ? '█' : top ? '▀' : bottom ? '▄' : ' ';
    }
    lines.push(line);
  }
  return lines.join('\n');
}

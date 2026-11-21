import {
  closeSync,
  existsSync,
  fsyncSync,
  mkdirSync,
  openSync,
  readdirSync,
  renameSync,
  statSync,
  writeSync,
} from 'node:fs';
import { dirname, join } from 'node:path';

const FSYNC_DIR_UNSUPPORTED = new Set(['EINVAL', 'ENOTSUP', 'EPERM', 'EACCES', 'EISDIR']);

// Section 10: the Baileys event listener boundary persists events with a
// synchronous write sequence — temp file, flush, fsync, atomic rename,
// fsync of the containing directory — so a normal process crash cannot
// lose an event between delivery and persistence. This does not protect
// against physical disk failure.
export function atomicWriteDurable(filePath, data) {
  const payload = Buffer.isBuffer(data) ? data : Buffer.from(String(data), 'utf8');
  const dir = dirname(filePath);
  const tmp = `${filePath}.tmp`;
  const fd = openSync(tmp, 'w', 0o600);
  try {
    let offset = 0;
    while (offset < payload.length) {
      offset += writeSync(fd, payload, offset, payload.length - offset);
    }
    fsyncSync(fd);
  } finally {
    closeSync(fd);
  }
  renameSync(tmp, filePath);
  fsyncDir(dir);
}

export function ensureDir(path) {
  if (existsSync(path)) {
    return;
  }
  mkdirSync(path, { recursive: true, mode: 0o700 });
}

export function fsyncDir(dir) {
  let fd;
  try {
    fd = openSync(dir, 'r');
  } catch {
    return; // directory not usable for fsync (exotic platforms)
  }
  try {
    fsyncSync(fd);
  } catch (err) {
    // some filesystems (drvfs, overlayfs) refuse directory fsync; the
    // rename is still atomic and the file itself was fsynced
    if (!FSYNC_DIR_UNSUPPORTED.has(err?.code)) {
      throw err;
    }
  } finally {
    closeSync(fd);
  }
}

export function dirSize(path) {
  let total = 0;
  let entries;
  try {
    entries = readdirSync(path);
  } catch {
    return 0;
  }
  for (const entry of entries) {
    const full = join(path, entry);
    const stats = statSync(full);
    if (stats.isDirectory()) {
      total += dirSize(full);
    } else {
      total += stats.size;
    }
  }
  return total;
}

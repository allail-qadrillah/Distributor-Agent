// Session persistence (Section 6). The ConnectionManager depends only on
// the SessionStore INTERFACE:
//
//   load()                    -> { creds, keys } | null
//   saveCreds(creds)          -> void   (Baileys 'creds.update')
//   saveKeys(data)            -> void   (SignalKeyStore set payload)
//   clear()                   -> void   (logged out: wipe for re-pairing)
//
// `keys` must implement Baileys' SignalKeyStore: get(type, ids) and
// set(data). A future adapter (DB, cloud) implements the same interface
// without touching the ConnectionManager.
import { existsSync, readFileSync, unlinkSync } from 'node:fs';
import { join } from 'node:path';
import { BufferJSON } from '@whiskeysockets/baileys';
import { atomicWriteDurable, ensureDir } from './fsutil.js';

export class LocalFileSessionStore {
  constructor({ authDir }) {
    this.authDir = authDir;
    this.credsPath = join(authDir, 'creds.json');
    this.keysPath = join(authDir, 'keys.json');
    this._keys = new Map();
    this._creds = null;
  }

  load() {
    ensureDir(this.authDir);
    this._creds = this._readJson(this.credsPath);
    const rawKeys = this._readJson(this.keysPath);
    this._keys = new Map();
    if (rawKeys) {
      for (const [type, ids] of Object.entries(rawKeys)) {
        const inner = new Map();
        for (const [id, value] of Object.entries(ids)) inner.set(id, value);
        this._keys.set(type, inner);
      }
    }
    if (this._creds == null && this._keys.size === 0) {
      return null; // no session: the manager starts with initAuthCreds
    }
    return { creds: this._creds, keys: this.keyStore() };
  }

  saveCreds(creds) {
    this._creds = creds;
    ensureDir(this.authDir);
    atomicWriteDurable(this.credsPath, JSON.stringify(creds, BufferJSON.replacer));
  }

  async saveKeys(data) {
    for (const [type, ids] of Object.entries(data)) {
      let inner = this._keys.get(type);
      if (!inner) {
        inner = new Map();
        this._keys.set(type, inner);
      }
      for (const [id, value] of Object.entries(ids)) {
        if (value == null) inner.delete(id);
        else inner.set(id, value);
      }
    }
    ensureDir(this.authDir);
    const serializable = {};
    for (const [type, inner] of this._keys) {
      serializable[type] = Object.fromEntries(inner);
    }
    atomicWriteDurable(this.keysPath, JSON.stringify(serializable, BufferJSON.replacer));
  }

  clear() {
    for (const path of [this.credsPath, this.keysPath]) {
      if (existsSync(path)) {
        try { unlinkSync(path); } catch { /* already gone */ }
      }
    }
    this._creds = null;
    this._keys = new Map();
  }

  keyStore() {
    const store = this;
    return {
      async get(type, ids) {
        const inner = store._keys.get(type);
        const out = {};
        if (!inner) return out;
        for (const id of ids) {
          if (inner.has(id)) out[id] = inner.get(id);
        }
        return out;
      },
      async set(data) {
        await store.saveKeys(data);
      },
    };
  }

  _readJson(path) {
    if (!existsSync(path)) return null;
    try {
      return JSON.parse(readFileSync(path, 'utf8'), BufferJSON.reviver);
    } catch {
      return null; // corrupt session file: treat as no session
    }
  }
}

import { createHash } from 'node:crypto';

// keys that must never reach a log line (Section 17). Values become
// '[redacted]'; anything else that serializes to a string containing a
// phone-number-like pattern is masked too.
const REDACTED_KEYS = new Set([
  'to',
  'text',
  'body',
  'phone',
  'token',
  'secret',
  'qr',
  'creds',
  'password',
]);

const PHONE_LIKE = /(\+?62\d{6,15}|@s\.whatsapp\.net)/;

function scrub(value) {
  if (value instanceof Error) {
    return { name: value.name, message: scrubString(value.message) };
  }
  if (typeof value === 'string') {
    return scrubString(value);
  }
  if (value && typeof value === 'object') {
    const out = {};
    for (const [key, inner] of Object.entries(value)) {
      out[key] = REDACTED_KEYS.has(key)
        ? '[redacted]'
        : scrub(inner);
    }
    return out;
  }
  return value;
}

function scrubString(text) {
  return text.replace(PHONE_LIKE, '[masked]');
}

export function jidHash(jid) {
  return createHash('sha256').update(String(jid)).digest('hex').slice(0, 12);
}

export function createLogger(write = (line) => process.stdout.write(line)) {
  const emit = (level, event, fields = {}) => {
    const entry = { ts: new Date().toISOString(), level, event };
    for (const [key, value] of Object.entries(fields)) {
      entry[key] = REDACTED_KEYS.has(key) ? '[redacted]' : scrub(value);
    }
    write(`${JSON.stringify(entry)}\n`);
  };
  return {
    info: (event, fields) => emit('info', event, fields),
    warn: (event, fields) => emit('warn', event, fields),
    error: (event, fields) => emit('error', event, fields),
    child(bound) {
      const base = bound;
      return {
        info: (event, fields) => emit('info', event, { ...base, ...fields }),
        warn: (event, fields) => emit('warn', event, { ...base, ...fields }),
        error: (event, fields) => emit('error', event, { ...base, ...fields }),
      };
    },
  };
}

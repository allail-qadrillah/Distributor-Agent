import { createHash } from 'node:crypto';
import pino from 'pino';

// Structured logging on pino. Event-first call style:
//   logger.info('inbound_delivered', { jid, attempts })
// and pino-style object calls (used by the stores):
//   logger.warn({ event: 'inbound_record_corrupt', file })
// both produce one JSON line: { ts, level, service, event, ...fields }.
//
// Redaction (Section 17) is a hard property, so it wraps the destination
// itself: every line is re-scrubbed after pino serialized it, no matter
// which call path produced it.
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

// same shape as the Python side's mask_phone: any 9–15 digit run (with
// optional +) is a phone number, plus any full WhatsApp JID
const PHONE_LIKE = /(\+?\d{9,15}|@s\.whatsapp\.net)/g;

const LOG_LEVELS = new Set(['fatal', 'error', 'warn', 'info', 'debug', 'trace']);

function scrubString(text) {
  return String(text).replace(PHONE_LIKE, '[masked]');
}

function scrub(value) {
  if (value instanceof Error) {
    return { name: value.name, message: scrubString(value.message) };
  }
  if (typeof value === 'string') {
    return scrubString(value);
  }
  if (Array.isArray(value)) {
    return value.map(scrub);
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

export function jidHash(jid) {
  return createHash('sha256').update(String(jid)).digest('hex').slice(0, 12);
}

// pino-pretty is a devDependency: only loaded for an interactive dev
// terminal, never in production (where it is not installed).
let prettyFactory = null;
if (process.stdout.isTTY && process.env.NODE_ENV !== 'production') {
  try {
    const mod = await import('pino-pretty');
    prettyFactory = mod.default;
  } catch {
    prettyFactory = null;
  }
}

// Everything pino writes passes through here: parse, scrub, re-serialize.
// A line that cannot be parsed passes through raw — pino owns it.
function redactingStream(dest) {
  const writeLine = typeof dest === 'function' ? dest : (line) => dest.write(line);
  return {
    write(line) {
      let out = line;
      try {
        out = `${JSON.stringify(scrub(JSON.parse(line)))}\n`;
      } catch { /* unparseable: pass through */ }
      writeLine(out);
    },
  };
}

const serializeError = (value) =>
  value instanceof Error ? { name: value.name, message: scrubString(value.message) } : value;

function toApi(instance) {
  const emit = (method, event, fields) =>
    method(event && typeof event === 'object' ? event : { ...fields, event });
  return {
    info: (event, fields) => emit(instance.info.bind(instance), event, fields),
    warn: (event, fields) => emit(instance.warn.bind(instance), event, fields),
    error: (event, fields) => emit(instance.error.bind(instance), event, fields),
    debug: (event, fields) => emit(instance.debug.bind(instance), event, fields),
    child: (bound) => toApi(instance.child(bound)),
  };
}

export function createLogger({
  write,
  level = 'info',
  // colored console on a development TTY, JSON otherwise (tests and
  // production always pass an explicit destination or run headless)
  pretty = process.stdout.isTTY && process.env.NODE_ENV !== 'production',
} = {}) {
  if (!LOG_LEVELS.has(level)) {
    throw new Error(`invalid log level: ${level}`);
  }
  const dest = write ?? (pretty && prettyFactory
    ? prettyFactory({ colorize: true, messageKey: 'event', ignore: 'pid,hostname' })
    : process.stdout);
  const instance = pino(
    {
      level,
      base: { service: 'whatsapp-bridge' },
      messageKey: 'event',
      formatters: { level: (label) => ({ level: label }) },
      timestamp: () => `,"ts":"${new Date().toISOString()}"`,
      serializers: { error: serializeError, err: serializeError },
    },
    redactingStream(dest),
  );
  return toApi(instance);
}

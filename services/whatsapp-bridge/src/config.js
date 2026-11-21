import { resolve } from 'node:path';

export class ConfigError extends Error {}

const QR_MODES = new Set(['local', 'docker']);
const LOG_LEVELS = new Set(['fatal', 'error', 'warn', 'info', 'debug', 'trace']);

function parsePort(raw) {
  const port = Number(raw ?? 3001);
  if (!Number.isInteger(port) || port < 1 || port > 65535) {
    throw new ConfigError(`WHATSAPP_BRIDGE_PORT invalid: ${raw}`);
  }
  return port;
}

function parseLogLevel(raw) {
  const level = String(raw ?? 'info').trim().toLowerCase();
  if (!LOG_LEVELS.has(level)) {
    throw new ConfigError(`LOG_LEVEL invalid: ${raw} (fatal|error|warn|info|debug|trace)`);
  }
  return level;
}

export function loadConfig(env = process.env) {
  const required = (name) => {
    const value = env[name];
    if (typeof value !== 'string' || value.trim() === '') {
      throw new ConfigError(`${name} is required (fail closed)`);
    }
    return value.trim();
  };

  const qrMode = env.WHATSAPP_BRIDGE_QR_MODE ?? 'local';
  if (!QR_MODES.has(qrMode)) {
    throw new ConfigError(`WHATSAPP_BRIDGE_QR_MODE invalid: ${qrMode}`);
  }

  return {
    dataDir: resolve(env.WHATSAPP_BRIDGE_DATA_DIR ?? 'data/whatsapp-bridge'),
    bridgeToken: required('WHATSAPP_BRIDGE_TOKEN'),
    webhookSecret: required('WHATSAPP_WEBHOOK_SECRET'),
    fastapiUrl: required('WHATSAPP_BRIDGE_URL').replace(/\/+$/, ''),
    host: env.WHATSAPP_BRIDGE_HOST ?? '127.0.0.1',
    port: parsePort(env.WHATSAPP_BRIDGE_PORT),
    qrMode,
    logLevel: parseLogLevel(env.LOG_LEVEL),
    // spec-fixed constants (Section 11/12/14/15) — not configurable
    fastapiTimeoutMs: 150_000,
    maxActiveJids: 5,
    outboundWaitLimit: 10,
    queueMaxBytes: 1024 ** 3,
    queueHighRatio: 0.8,
    queueLowRatio: 0.6,
  };
}

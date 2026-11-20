import { join } from 'node:path';
import { pathToFileURL } from 'node:url';
import { makeWASocket } from '@whiskeysockets/baileys';
import { loadConfig } from './config.js';
import { createLogger } from './logger.js';
import { LocalFileSessionStore } from './session-store.js';
import { ConnectionManager } from './connection-manager.js';
import { InboundStore } from './inbound-store.js';
import { InboundForwarder } from './inbound-forwarder.js';
import { DeliveryStore } from './delivery-store.js';
import { DeliveryReporter } from './delivery-reporter.js';
import { OutboundSender } from './outbound.js';
import { FastApiClient } from './fastapi-client.js';
import { createHttpServer } from './httpserver.js';
import { CapacityGuard } from './capacity.js';
import { displayQr } from './qr-display.js';

export function makeRealSocket(authState) {
  return makeWASocket({
    auth: { creds: authState.creds, keys: authState.keys },
    syncFullHistory: false,
  });
}

export async function startBridge({
  config,
  makeSocket = makeRealSocket,
  fetchImpl,
  displayQrFn = displayQr,
  exit = (code) => process.exit(code),
  capacityIntervalMs = 10_000,
}) {
  const logger = createLogger();
  const sessionStore = new LocalFileSessionStore({ authDir: join(config.dataDir, 'auth') });
  const inboundStore = new InboundStore({ dataDir: config.dataDir, logger });
  const deliveryStore = new DeliveryStore({ dataDir: config.dataDir, logger });
  const client = new FastApiClient({
    baseUrl: config.fastapiUrl,
    webhookSecret: config.webhookSecret,
    timeoutMs: config.fastapiTimeoutMs,
    fetchImpl,
  });

  const recovered = inboundStore.recoverProcessing();
  const resplit = inboundStore.resplitAll();
  if (recovered || resplit) {
    logger.info('inbound_recovered', { in_flight: recovered, resplit: resplit });
  }

  let stopped = false;

  const manager = new ConnectionManager({
    makeSocket,
    store: sessionStore,
    logger,
    onQr: (qr) => {
      const action = displayQrFn(qr, { mode: config.qrMode });
      if (action === 'stop-container') {
        logger.error('qr_pairing_required', {
          mode: 'docker',
          hint: 'no terminal in docker: stop detected so the operator pairs locally, then restarts',
        });
        stop().then(() => exit(0));
      }
    },
    onMessages: (upsert) => {
      inboundStore.ingest(upsert);
    },
    onMessagesUpdate: (updates) => {
      reporter.observe(updates);
    },
    onOpen: () => logger.info('bridge_socket_open'),
  });

  const forwarder = new InboundForwarder({
    store: inboundStore,
    client,
    logger,
    maxJids: config.maxActiveJids,
    markRead: async (key) => {
      const socket = manager.socket;
      if (!socket || typeof socket.readMessages !== 'function') {
        throw new Error('socket not connected');
      }
      await socket.readMessages([key]);
    },
  });

  const reporter = new DeliveryReporter({ store: deliveryStore, client, logger });

  const outbound = new OutboundSender({
    store: deliveryStore,
    logger,
    waitLimit: config.outboundWaitLimit,
    send: async (jid, text, messageId) => {
      const socket = manager.socket;
      if (!socket || typeof socket.sendMessage !== 'function') {
        throw new Error('socket not connected');
      }
      await socket.sendMessage(jid, { text }, { messageId });
    },
  });

  const capacity = new CapacityGuard({
    dirs: [
      inboundStore.incomingDir,
      inboundStore.processingDir,
      inboundStore.retryDir,
      inboundStore.quarantineDir,
      deliveryStore.dir,
    ],
    maxBytes: config.queueMaxBytes,
    highRatio: config.queueHighRatio,
    lowRatio: config.queueLowRatio,
    logger,
  });
  const capacityTimer = setInterval(() => {
    const { action } = capacity.check();
    if (action === 'disconnect') {
      logger.error('bridge_capacity_disconnect');
      manager.suspend();
    } else if (action === 'reconnect') {
      logger.info('bridge_capacity_reconnect');
      manager.resume();
    }
  }, capacityIntervalMs);
  capacityTimer.unref?.();

  const isReady = () => manager.connected && !forwarder.paused && !outbound.shuttingDown;
  const httpServer = createHttpServer({ token: config.bridgeToken, outbound, isReady, logger });
  await new Promise((resolve) => {
    httpServer.listen(config.port, config.host, resolve);
  });
  const boundPort = httpServer.address().port;

  forwarder.start();
  reporter.start();
  manager.start();
  logger.info('bridge_started', { host: config.host, port: boundPort, data_dir: config.dataDir, qr_mode: config.qrMode });

  const stop = async () => {
    if (stopped) return;
    stopped = true;
    outbound.beginShutdown();
    clearInterval(capacityTimer);
    httpServer.closeAllConnections?.();
    await new Promise((resolve) => httpServer.close(resolve));
    await forwarder.stop();
    await reporter.stop();
    manager.stop();
    logger.info('bridge_stopped');
  };

  return {
    stop,
    manager,
    forwarder,
    reporter,
    outbound,
    httpServer,
    inboundStore,
    deliveryStore,
    sessionStore,
    port: boundPort,
  };
}

export function main() {
  let config;
  try {
    config = loadConfig(process.env);
  } catch (error) {
    process.stdout.write(`${JSON.stringify({ level: 'error', event: 'config_invalid', error: String(error) })}\n`);
    process.exit(1);
  }
  startBridge({ config }).then((bridge) => {
    const shutdown = (signal) => {
      // cap the graceful window well under the compose stop grace period
      const force = setTimeout(() => process.exit(0), 20_000);
      force.unref();
      bridge.stop().then(() => process.exit(0));
      void signal;
    };
    process.on('SIGTERM', () => shutdown('SIGTERM'));
    process.on('SIGINT', () => shutdown('SIGINT'));
    process.on('uncaughtException', (error) => {
      process.stdout.write(`${JSON.stringify({ level: 'error', event: 'uncaught_exception', error: String(error) })}\n`);
      process.exit(1);
    });
  }).catch((error) => {
    process.stdout.write(`${JSON.stringify({ level: 'error', event: 'bridge_start_failed', error: String(error) })}\n`);
    process.exit(1);
  });
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main();
}

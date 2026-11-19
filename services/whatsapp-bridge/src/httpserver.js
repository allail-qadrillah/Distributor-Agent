import { createServer } from 'node:http';
import { createHash, timingSafeEqual } from 'node:crypto';

const MAX_BODY_BYTES = 1024 * 1024;

export function tokenMatches(presented, expected) {
  if (typeof presented !== 'string' || typeof expected !== 'string' || expected.length === 0) {
    return false;
  }
  const a = createHash('sha256').update(presented).digest();
  const b = createHash('sha256').update(expected).digest();
  return timingSafeEqual(a, b);
}

function bearerToken(req) {
  const header = req.headers.authorization;
  if (typeof header !== 'string') return null;
  const match = /^Bearer (.+)$/.exec(header);
  return match ? match[1] : null;
}

function sendJson(res, status, body) {
  const payload = JSON.stringify(body);
  res.writeHead(status, { 'content-type': 'application/json', 'content-length': Buffer.byteLength(payload) });
  res.end(payload);
}

async function readBody(req) {
  const chunks = [];
  let size = 0;
  for await (const chunk of req) {
    size += chunk.length;
    if (size > MAX_BODY_BYTES) throw new Error('body_too_large');
    chunks.push(chunk);
  }
  return Buffer.concat(chunks).toString('utf8');
}

export function createHttpServer({ token, outbound, isReady, logger }) {
  const server = createServer((req, res) => {
    const done = (status, body) => {
      logger.info('http_request', { method: req.method, path: req.url, status });
      sendJson(res, status, body);
    };
    if (req.method === 'GET' && req.url === '/health') {
      return done(200, { status: 'ok' });
    }
    if (req.method === 'GET' && req.url === '/ready') {
      return done(isReady() ? 200 : 503, isReady() ? { status: 'ready' } : { status: 'not_ready' });
    }
    if (req.method === 'POST' && req.url === '/messages') {
      if (!tokenMatches(bearerToken(req), token)) {
        return done(401, { status: 'failed', error: 'unauthorized' });
      }
      readBody(req).then((raw) => {
        let payload;
        try {
          payload = JSON.parse(raw);
        } catch {
          throw new Error('invalid_json');
        }
        return outbound.handle(payload).then((result) => done(result.status, result.body));
      }).catch((error) => {
        if (error?.message === 'body_too_large') return done(413, { status: 'failed', error: 'body_too_large' });
        if (error?.message === 'invalid_json') return done(400, { status: 'failed', error: 'invalid_json' });
        return done(500, { status: 'failed', error: 'internal' });
      });
      return undefined;
    }
    return done(404, { status: 'failed', error: 'not_found' });
  });
  return server;
}

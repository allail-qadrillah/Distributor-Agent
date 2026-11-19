export class FastApiClient {
  constructor({ baseUrl, webhookSecret, timeoutMs = 150_000, fetchImpl } = {}) {
    if (!baseUrl) throw new Error('baseUrl required');
    this.baseUrl = baseUrl.replace(/\/+$/, '');
    this.webhookSecret = webhookSecret;
    this.timeoutMs = timeoutMs;
    this.fetch = fetchImpl ?? globalThis.fetch;
  }

  postEvents(payload) {
    return this._post('/webhooks/whatsapp/events', payload);
  }

  postDeliveryReport(report) {
    return this._post('/webhooks/whatsapp/delivery', report);
  }

  async _post(path, payload) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.timeoutMs);
    try {
      const response = await this.fetch(this.baseUrl + path, {
        method: 'POST',
        headers: {
          'content-type': 'application/json',
          'x-webhook-secret': this.webhookSecret,
        },
        body: JSON.stringify(payload),
        signal: controller.signal,
      });
      let body = null;
      try {
        body = await response.json();
      } catch {
        body = null;
      }
      const retryAfterRaw = Number(response.headers?.get?.('retry-after'));
      return {
        status: response.status,
        ok: response.status >= 200 && response.status < 300,
        body,
        retryAfterMs: Number.isFinite(retryAfterRaw) && retryAfterRaw > 0
          ? retryAfterRaw * 1000
          : null,
      };
    } catch (error) {
      if (error?.name === 'AbortError' || controller.signal.aborted) {
        return { status: null, ok: false, body: null, retryAfterMs: null, error: 'timeout' };
      }
      return { status: null, ok: false, body: null, retryAfterMs: null, error: 'network' };
    } finally {
      clearTimeout(timer);
    }
  }
}

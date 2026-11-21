// QR pairing display (Section 8). The QR string itself is a pairing secret:
// it is rendered to the terminal only, never passed to the logger.
// In docker mode there is no interactive terminal, so the caller is told
// to stop the container (pairing must happen from a local run).
import { encodeQR, renderTerminal } from './qr.js';

export function displayQr(qrText, { mode = 'local', write = (s) => process.stdout.write(s) } = {}) {
  if (mode === 'docker') {
    return 'stop-container';
  }
  const qr = encodeQR(qrText, { level: 'L' });
  const art = renderTerminal(qr, { quietZone: 4 });
  write(`\nWhatsApp pairing QR — scan with terminal background set to light:\n${art}\n`);
  return 'displayed';
}

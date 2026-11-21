// Receipt classification (Section 10). Baileys emits `messages.update`
// entries of the shape { key, update: { status } } where status is a
// WAMessageStatus number:
//   0 ERROR, 1 PENDING, 2 SERVER_ACK, 3 DELIVERY_ACK, 4 READ, 5 PLAYED
//
// Only our own outbound messages (key.fromMe === true) carry delivery
// receipts. PENDING and SERVER_ACK are transport noise; the mapping is
// kept and nothing is reported. Unrecognized values also hold the
// mapping — an ambiguous receipt must never be reported as failed.

export function classifyReceipt(entry) {
  if (!entry || typeof entry !== 'object') return null;
  const key = entry.key;
  if (!key || key.fromMe !== true) return null;
  const status = entry.update?.status;
  if (typeof status !== 'number') return null;
  if (status === 0) return 'failed'; // ERROR: WhatsApp rejected the send
  if (status === 3 || status === 4 || status === 5) return 'delivered';
  return null;
}

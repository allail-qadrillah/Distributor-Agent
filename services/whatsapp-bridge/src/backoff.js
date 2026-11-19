export function computeBackoffMs(attempt, { maxMs = 60_000, baseMs = 1000 } = {}, random = Math.random) {
  const exponent = Math.max(0, Math.floor(attempt));
  let raw;
  if (exponent >= 31) {
    raw = maxMs; // avoid 2**31 overflow territory
  } else {
    raw = Math.min(baseMs * 2 ** exponent, maxMs);
  }
  const factor = 0.5 + random() * 0.5;
  return Math.min(Math.round(raw * factor), maxMs);
}

# Catatan Engineering, P3: WhatsApp Bridge Contract

Tanggal: 2026-11-18 · Status: kontrak diimplementasi sisi Python; pengiriman WhatsApp nyata BELUM diuji (bridge Node belum ada di lingkungan ini)

## 1. Tujuan

Mendefinisikan kontrak kawat (wire contract) antara aplikasi Python dan bridge WhatsApp eksternal (layanan Node.js berbasis Baileys). Sisi Python mengimplementasi kontrak ini penuh (normalisasi inbound, adapter outbound, webhook); sisi bridge tinggal mengikuti. Dokumen ini dirujuk oleh `.env.example`.

## 2. Fakta Baileys yang diverifikasi

Diverifikasi dari sumber WhiskeySockets/Baileys (2026-11-18):

- Event `messages.upsert` membawa `{"messages": WAMessage[], "type": "notify" | "append"}`.
  `"notify"` = pesan hidup (diproses); `"append"` = backfill riwayat sync — DILEWATI,
  tidak boleh direplay ke agen.
- `WAMessage.key = {remoteJid, id, fromMe, participant?}`. Chat 1:1 memakai
  `<digits>@s.whatsapp.net`; grup `@g.us`; broadcast `status@broadcast`; newsletter
  `@newsletter`; LID `@lid` (tidak bisa dipetakan ke E.164).
- Teks pelanggan ada di `message.conversation` ATAU
  `message.extendedTextMessage.text`. Jenis konten lain (gambar, dokumen) tidak
  membawa teks yang bisa diproses pada milestone ini.
- Read receipt TIDAK otomatis: Baileys hanya mengirimnya lewat pemanggilan
  eksplisit `sock.readMessages([key])`. Aplikasi memanfaatkan ini — lihat §3.

## 3. Inbound: `POST /webhooks/whatsapp/events`

Bridge meneruskan payload `messages.upsert` MENTAH (tidak dinormalisasi).
Aplikasi memproses dan menjawab per pesan:

```json
{
  "results": [
    {
      "external_message_id": "3A",
      "status": "processed",
      "mark_read": true,
      "replied": true
    },
    { "external_message_id": null, "status": "skipped:history_backfill", "mark_read": false, "replied": false }
  ]
}
```

- `status` : `processed` | `duplicate` | `unknown_sender` |
  `identity_mismatch` (nomor kini milik pelanggan lain: berhenti + handoff,
  transkrip milik pelanggan tertaut, link tidak pernah ditimpa) |
  `human_handling` | `handoff` | `deferred` (persetujuan pesanan masih
  `pending_delivery`: pesan menunggu, diproses ulang setelah aktivasi) |
  `agent_error` | `skipped:<alasan>` (backfill, echo keluar
  sendiri, grup, broadcast, newsletter, LID, tanpa teks, bentuk invalid).
- `mark_read: false` = aplikasi MEMERINTAHKAN bridge menahan read receipt:
  jangan panggil `sock.readMessages` untuk pesan ini. Aplikasi hanya
  mengizinkan receipt setelah agen benar-benar memproses pesan. Pesan saat
  handoff/unknown sender tetap terlihat belum dibaca oleh pelanggan.
- `replied: true` hanya bila balasan dikirim (atau dikirim dengan hasil
  `unknown`).

Idempotensi inbound ditangani aplikasi lewat `UNIQUE (messages.external_message_id)`
— bridge boleh mengirim ulang event yang sama dengan aman.

## 4. Outbound: `POST {WHATSAPP_BRIDGE_URL}/messages`

Aplikasi → bridge, sekali per pesan keluar:

```json
POST /messages
Authorization: Bearer {WHATSAPP_BRIDGE_TOKEN}
{ "outbox_id": "<uuid>", "to": "+6281234567890", "text": "..." }
```

`outbox_id` adalah kunci rekonsiliasi: bridge HARUS memakainya pada laporan
delivery (§5). WhatsApp sendiri tidak punya idempotency key sisi penerima.

Respons 2xx dari bridge:

```json
{ "status": "sent", "error": null }
```

- `"sent"` HANYA boleh dijawab setelah `sock.sendMessage` resolve (pesan
  diterima dan dienkripsi untuk pengiriman). Level perangkat (DELIVERY_ACK,
  READ) datang BELAKANGAN lewat webhook delivery.
- `"failed"` + `error` = bridge/WhatsApp menolak; boleh dicoba ulang aplikasi
  dalam budget attempt.

Pemetaan hasil sisi adapter (aturan keamanan inti outbox):

| Kondisi | Status aplikasi | Dicoba ulang otomatis? |
|---|---|---|
| Respons `"sent"` | `sent` | tidak perlu |
| Respons `"failed"` | `failed` | ya, ≤ 3 attempt |
| HTTP 4xx (bridge menolak permintaannya sendiri: auth, validasi) | `failed` | ya |
| HTTP 5xx (bridge gagal saat memproses: pesan MUNGKIN sudah ke WhatsApp) | `unknown` | TIDAK PERNAH |
| Koneksi tak pernah terbangun (ConnectError) | `failed` | ya |
| Respons hilang setelah permintaan terbang (timeout baca, reset, badan 2xx tak terparse) | `unknown` | TIDAK PERNAH |

`unknown` tidak pernah di-resend otomatis: pesan mungkin sudah sampai;
resend buta bisa menduplikasi pesan ke pelanggan. Penyelesaiannya hanya
rekonsiliasi (§5).

Pemetaan 4xx/5xx ini kontrak: hanya jaminan eksplisit bridge "tidak
terkirim sebelum 5xx" yang boleh memindahkan 5xx ke `failed`. Kontrak ini
tidak membawa jaminan itu.

## 5. Delivery report: `POST /webhooks/whatsapp/delivery`

Bridge melaporkan hasil final per pesan keluar, kapan pun itu tiba:

```json
X-Webhook-Secret: {WHATSAPP_WEBHOOK_SECRET}
{ "outbox_id": "<uuid>", "status": "delivered" | "failed", "error": null }
```

- `delivered` merekonsiliasi baris `pending`/`sending`/`unknown` menjadi
  `sent`, dan MENGAKTIFKAN persetujuan pesanan yang terikat pada pesan
  ringkasan itu (`order_approvals.pending_delivery → active`) — dalam
  transaksi yang SAMA dengan transisi status (tidak bisa terpisah oleh
  crash). Aktivasi juga memicu pemrosesan ulang pesan yang sebelumnya
  DITUNDA karena persetujuan masih `pending_delivery` (konfirmasi cepat
  pelanggan diproses tepat satu kali, hanya setelah persetujuannya ada).
- `failed` merekonsiliasi menjadi `failed` (terminal: adapter tidak
  mengirim ulang; operator yang memutuskan).
- Baris yang sudah terminal (`sent`/`failed`) menjawab `already_final` —
  laporan duplikat/terlambat adalah no-op.

### 5a. Klaim `sending` basi (lease `claimed_at`)

Setiap dispatch mengklaim baris `sending` + stempel waktu `claimed_at`
(lease). Baris `sending` yang lease-nya kedaluwarsa
(`OUTBOX_STALE_SENDING_SECONDS`, default 60 dtk) = dispatcher yang mati
di tengah pengiriman: hasilnya TIDAK bisa ditentukan, jadi sweep
rekonsiliasi menjadikannya `unknown` — tidak pernah resend otomatis,
hanya delivery report (atau operator) yang menyelesaikannya. Sweep ini
berjalan otomatis di awal setiap dispatch.

## 6. Keamanan (diimplementasi, remediasi Phase 3)

- Token Bearer aplikasi→bridge: `WHATSAPP_BRIDGE_TOKEN` (bridge
  memverifikasi). Produksi dengan `WHATSAPP_BRIDGE_URL` tanpa token
  ditolak saat startup.
- Webhook bridge→aplikasi (`/webhooks/whatsapp/*`): shared secret
  `WHATSAPP_WEBHOOK_SECRET` di header `X-Webhook-Secret`, diperbandingkan
  constant-time (`hmac.compare_digest`).
- Rute operator (`/conversations/*`, `/outbox/dispatch`): shared token
  `OPERATOR_API_TOKEN` di header `X-Operator-Token`, sama constant-time.
- Fail-closed: secret kosong di `APP_ENVIRONMENT=production` (default)
  = SEMUA request ditolak 401. `APP_ENVIRONMENT=development` = bypass
  terdokumentasi (hanya untuk dev).
- Ini batas autentikasi, bukan sistem identitas: belum ada akun
  operator per-orang, proteksi replay, atau protokol rotasi. Jalur
  upgrade: mTLS/HMAC-per-bridge dengan timestamp, akun operator nyata.
- Rute operator menunggu lock per-telepon dengan BUDGET terbatas (10
  dtk): bila alur agen sedang berjalan, rute menjawab 503 `conversation
  busy` (tidak ada yang termutasi) alih-alih menggantung hingga timeout
  provider 120 dtk.

## 7. Konfigurasi

```
APP_ENVIRONMENT=production            # atau development (bypass terdokumentasi)
WHATSAPP_BRIDGE_URL=                  # kosong = hanya inbound; balasan menunggu di outbox
WHATSAPP_BRIDGE_TOKEN=                # WAJIB di produksi bila URL terisi
WHATSAPP_BRIDGE_TIMEOUT_SECONDS=15.0
WHATSAPP_WEBHOOK_SECRET=              # X-Webhook-Secret; kosong di produksi = 401 semua
OPERATOR_API_TOKEN=                   # X-Operator-Token; kosong di produksi = 401 semua
OUTBOX_STALE_SENDING_SECONDS=60.0     # lease klaim 'sending' basi
```

URL kosong berarti `WhatsAppAdapter` tidak dibangun: `dispatch_pending`
menjawab 0, tidak ada yang dikirim — mode inbound-only yang aman.

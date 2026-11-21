# Distributor Agent — Local Run Guide (WSL Ubuntu)

Panduan ini menjalankan **FastAPI** dan **WhatsApp Baileys Bridge** secara langsung di WSL Ubuntu, tanpa Docker.

## TLDR; Quick start ringkas

**Terminal 1 — FastAPI**

```bash
cd "./Distributor Agent"
uv sync
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

**Terminal 2 — WhatsApp Bridge**

```bash
cd "./Distributor Agent/services/whatsapp-bridge"
nvm use 26
npm ci
set -a
source "../../.env"
set +a
export WHATSAPP_BRIDGE_DATA_DIR="../../data/whatsapp-bridge"
export WHATSAPP_BRIDGE_FASTAPI_URL="http://127.0.0.1:8000"
export WHATSAPP_BRIDGE_HOST="127.0.0.1"
export WHATSAPP_BRIDGE_PORT="3001"
export WHATSAPP_BRIDGE_QR_MODE="local"
npm start
```

**Terminal 3 — Health checks**

```bash
curl -i http://127.0.0.1:8000/health
curl -i http://127.0.0.1:3001/health
curl -i http://127.0.0.1:3001/ready
```

Setelah semua proses siap, lakukan tes pesan dari akun WhatsApp lain dan verifikasi inbound, Agent processing, outbound, serta delivery receipt secara terpisah.

---

## 1. Arsitektur proses lokal

Jalankan dua proses di dua terminal WSL yang berbeda:

```text
WhatsApp
   ↕
Baileys Bridge :3001
   ↕ HTTP webhook / outbound API
FastAPI :8000
   ↕
Agent + business services
   ↕
PostgreSQL / Supabase
```

- FastAPI menangani webhook, Agent, business logic, database, dan outbox.
- Baileys Bridge menangani koneksi WhatsApp, inbound events, outbound sending, serta delivery receipts.

## 2. Prasyarat

Pastikan tersedia:

- WSL Ubuntu.
- Python 3.12.
- `uv`.
- Node.js 26 LTS melalui NVM.
- npm.
- Environment variables yang valid untuk aplikasi dan koneksi Supabase/PostgreSQL serta provider AI yang digunakan.

Periksa versi:

```bash
python3 --version
uv --version
node --version
npm --version
```

Node harus berada pada major version 26:

```bash
nvm install 26
nvm use 26
nvm alias default 26
node --version
```

Jika `nvm` belum tersedia, pasang dan konfigurasi NVM sesuai dokumentasi resmi NVM terlebih dahulu. Jangan lanjut memakai Node 22 untuk Bridge karena dependency project mensyaratkan Node >=26.

## 3. Siapkan environment variables

Gunakan file `.env` di root repository sesuai konfigurasi project. 

Jika `.env` berasal dari Windows dan memiliki CRLF, ubah line endings:

```bash
sudo apt update
sudo apt install dos2unix
dos2unix .env
```

Pastikan `.env` memiliki konfigurasi yang dibutuhkan FastAPI. Nama variabel yang penting untuk koneksi lokal:

| Variable | Nilai / tujuan |
|---|---|
| `WHATSAPP_BRIDGE_URL` | `http://127.0.0.1:3001` — alamat Bridge yang dipanggil FastAPI |
| `WHATSAPP_BRIDGE_TOKEN` | Shared bearer token; nilainya harus sama pada FastAPI dan Bridge |
| `WHATSAPP_WEBHOOK_SECRET` | Shared webhook secret; nilainya harus sama pada FastAPI dan Bridge |
| `WHATSAPP_BRIDGE_FASTAPI_URL` | `http://127.0.0.1:8000` — alamat FastAPI yang dipanggil Bridge |
| `OPERATOR_API_TOKEN` | Token operator terpisah; bukan pengganti Bridge token |
| Database / Supabase variables | Credential koneksi database yang diwajibkan konfigurasi FastAPI |
| OpenAI / LLM variables | Credential dan konfigurasi provider AI yang diwajibkan Agent |

Gunakan nilai secret yang sudah disediakan untuk environment milik sendiri. Jangan menyalin secret ke terminal output atau membagikannya.

## 4. Install dependency dan jalankan FastAPI

Buka **Terminal 1**:

```bash
cd "./Distributor Agent"
uv sync
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

FastAPI akan tersedia pada:

- API: `http://127.0.0.1:8000`
- Health: `http://127.0.0.1:8000/health`
- OpenAPI: `http://127.0.0.1:8000/openapi.json`

Periksa health dari terminal lain:

```bash
curl -i http://127.0.0.1:8000/health
```

Health endpoint yang mengembalikan HTTP 200 membuktikan proses API hidup. Itu belum membuktikan koneksi database, Agent, atau WhatsApp end-to-end bekerja.

## 6. Install dependency dan jalankan WhatsApp Bridge

Buka **Terminal 2**:

```bash
cd "./Distributor Agent/services/whatsapp-bridge"
npm ci
```

Pastikan Node 26 aktif pada terminal ini:

```bash
node --version
```

Load variable dari `.env` root ke shell Bridge:

```bash
set -a
source "../../.env"
set +a
```

Jika `source` menghasilkan error seperti `command not found` pada baris `.env`, periksa line endings dan jalankan `dos2unix`. Pastikan `.env` menggunakan format shell-compatible `KEY=value`; jangan memakai format yang tidak dapat di-source.

Set variable khusus Bridge:

```bash
export WHATSAPP_BRIDGE_DATA_DIR="../../data/whatsapp-bridge"
export WHATSAPP_BRIDGE_FASTAPI_URL="http://127.0.0.1:8000"
export WHATSAPP_BRIDGE_HOST="127.0.0.1"
export WHATSAPP_BRIDGE_PORT="3001"
export WHATSAPP_BRIDGE_QR_MODE="local"
```

Pastikan `WHATSAPP_BRIDGE_TOKEN` dan `WHATSAPP_WEBHOOK_SECRET` sudah ter-load dari `.env` dan nilainya sama dengan yang digunakan FastAPI.

Jalankan Bridge:

```bash
npm start
```

Bridge lokal akan listen pada `127.0.0.1:3001`. Mode `local` dapat menampilkan QR pairing di terminal.

### Pairing WhatsApp

1. Tunggu QR muncul di Terminal 2.
2. Buka WhatsApp pada ponsel yang akan dipasangkan.
3. Buka menu **Linked devices**.
4. Pilih **Link a device**, lalu scan QR dari terminal.
5. Tunggu proses pairing dan reconnect selesai.
6. Cari log yang menunjukkan socket terbuka, misalnya `wa_connected` dan `bridge_socket_open`.

Baileys dapat melakukan restart koneksi setelah pairing. Kode restart seperti 515 pada tahap pairing dapat menjadi bagian dari handshake; nilai itu sendiri tidak membuktikan pairing gagal.

**Penting:** session WhatsApp tersimpan di `data/whatsapp-bridge`. Jangan menghapus folder ini atau subfolder `auth/`, Penghapusan dapat menghilangkan session dan memaksa pairing ulang.

## 7. Verifikasi Bridge

Dengan kedua proses berjalan, buka terminal ketiga:

```bash
curl -i http://127.0.0.1:3001/health
curl -i http://127.0.0.1:3001/ready
```

Interpretasi:

- `/health` HTTP 200: proses Bridge hidup.
- `/ready` HTTP 200: Bridge melaporkan kondisi siap, termasuk koneksi dan guard operasional yang relevan.
- `/ready` HTTP 503: Bridge belum siap. Periksa koneksi WhatsApp, shutdown state, inbound pause, dan capacity guard di log Bridge.

Health endpoint Bridge dan FastAPI hanya memverifikasi status proses/komponen sesuai kontrak endpoint. 
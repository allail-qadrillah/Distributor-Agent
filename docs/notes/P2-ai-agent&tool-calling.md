# P2: AI Agent & Tool Calling

Tanggal: 2026-11-17 · Status: selesai dan terverifikasi (unit + regresi + integrasi DB + smoke live provider dan model).

## Apa yang dibangun

Fondasi AI agent lengkap untuk asisten penjualan FMCG via WhatsApp. Aplikasi mengirim pesan pelanggan + instruksi agen ke model LLM, model bisa meminta tool, tool dieksekusi ke layanan bisnis yang sudah ada, hasil dikirim balik ke model, loop berulang sampai jawaban teks akhir. Milestone berurutan: deteksi respons → definisi tool + registry → validasi argumen + konteks terpercaya → eksekusi tool → loop multi-putaran → kendali eksekusi + retry → verifikasi alur pesanan → verifikasi live.

## Arsitektur

```
lapisan aplikasi (WhatsApp / API)
        │  AgentInput(messages) + ToolContext(customer_id, ...)
        ▼
AgentOrchestrator ──── instruksi system + riwayat ────► SurplussProvider (adapter)
        │                                                    │ HTTP POST /chat/completions
        │                                                    │ (OpenAI-compatible)
        │  AgentResponse(kind="text" | "tool_calls")
        │
        ▼
   ToolExecutor.execute(tool_call_id, name, arguments, context)
        │  registry → parse_tool_arguments → handler → service
        ▼
   ToolResult(ok, data | error) ──► pesan tool ──► provider lagi (loop)
```

Arah dependensi ketat: `api → tools → services → repositories → database`. Orkestrator di `app.agents` pemanggil teratas; `app.tools` tidak pernah mengimpor `app.agents`.

Pemisahan tanggung jawab: orkestrator = alur Python murni, tanpa SQL/repo/transaksi/parsing provider. Adapter = satu-satunya modul yang tahu format kabel provider. Handler = penerjemah murni (args tervalidasi + konteks → permintaan service). Instruksi = data murni (`instructions.py`, Bahasa Indonesia).

## Komponen

| File | Tanggung jawab |
|---|---|
| `src/app/agents/schemas.py` | `AgentMessage` (4 peran: system/user/assistant/tool, aturan bentuk per peran), `AgentInput`, `AgentResponse` (XOR teks/tool_calls), `AgentRunResult` (final_text + response_id + transcript + events), `AgentRunEvent`. |
| `src/app/agents/orchestrator.py` | `AgentOrchestrator.run(input, context)`: loop provider→executor→provider sampai teks akhir, batas putaran 5, limit panggilan per giliran (8) dan total (16), rekam event observabilitas. |
| `src/app/agents/instructions.py` | `AGENT_INSTRUCTIONS`: asisten penjualan FMCG Indonesia, backend otoritatif, dilarang mengarang harga/stok, dilarang klaim berhasil tanpa hasil backend. |
| `src/app/agents/exceptions.py` | Error domain agen: `AgentAuthenticationError`, `AgentRateLimitError`, `AgentProviderUnavailableError`, `AgentInvalidResponseError`, `AgentRoundLimitError`, `AgentToolCallLimitError`, `AgentError` (dengan `partial_transcript`/`partial_events`). |
| `src/app/agents/providers/surpluss.py` | `SurplussProvider`: bangun permintaan OpenAI-compatible, kirim via httpx, normalisasi respons, retry transien, `_wire_message` serialisasi transkrip. |
| `src/app/tools/definitions.py` | `ToolDefinition` frozen + tiga definisi: `search_products`, `create_draft_order`, `confirm_order` (JSON Schema inline, deskripsi Bahasa). |
| `src/app/tools/registry.py` | `ToolRegistry`: peta nama→definisi+handler, tolak duplikat/nama tak dikenal, `openai_tools()` emisi wire. |
| `src/app/tools/arguments.py` | `SearchProductsArgs`/`CreateDraftOrderArgs`/`ConfirmOrderArgs` (Pydantic strict `extra="forbid"`) + `parse_tool_arguments`. |
| `src/app/tools/context.py` | `ToolContext` frozen: `customer_id`, `conversation_id`, `approved_order_id` (persetujuan satu pesanan), `with_confirm_order_approval(order_id)`. |
| `src/app/tools/results.py` | `ToolResult` (ok + tepat satu payload), `ToolResultError` (kategori + pesan + detail). |
| `src/app/tools/handlers.py` | Tiga handler + `ToolServices` (injeksi service) + `map_service_error` (14 eksepsi service → kategori). |
| `src/app/tools/executor.py` | `ToolExecutor.execute` selalu mengembalikan `ToolResult`, tidak pernah exception lewat batas; `build_tool_executor(services)` titik wiring. |

## Tiga tool

- `search_products` (`search_term`) → `ProductInquiryService.search_products` (read-only, semua kandidat, tanpa reservasi stok).
- `create_draft_order` (`items[]`) → `OrderService.create_draft_order`, `customer_id` dari konteks, harga dari backend.
- `confirm_order` (`order_id`) → `OrderService.confirm_order` setelah dua gerbang: persetujuan eksplisit (`approved_order_id` == `order_id`) + kepemilikan draft (`get_order` lalu bandingkan `customer_id`).

## Batas kepercayaan

Model menghasilkan teks bebas. Output-nya bukan tipe. Empat konsep terpisah, tidak digabung: argumen mentah (string JSON) → argumen tervalidasi (Pydantic) → konteks terpercaya (`ToolContext`) → permintaan service internal.

Invariant keamanan dibakar ke data: tidak ada definisi tool yang menerima `customer_id`, harga, atau argumen persetujuan. `customer_id` hanya dari konteks terpercaya. Harga satuan hanya dari daftar harga backend. Persetujuan konfirmasi hanya dari `with_confirm_order_approval()` yang dipanggil logika aplikasi. Model diberi hak bicara, bukan hak tanda tangan.

## Kategori kegagalan

`unknown_tool`, `malformed_arguments`, `invalid_arguments`, `unauthorized`, `not_found`, `inactive`, `missing_data`, `business_rule`, `execution_error`. Kegagalan selalu jadi data (`ToolResult(ok=False, error={code, message, detail})`) yang dibaca model, bukan exception mentah. Kegagalan tak terduga disanitasi (hanya nama kelas exception). `MissingToolHandlerError` raise keras: definisi tanpa handler = bug wiring, bukan kegagalan model.

## Kendali eksekusi

Limit dicek sebelum eksekusi, batch tidak pernah parsial. Batch melebihi limit ditolak utuh (`AgentToolCallLimitError`). Batas putaran tercapai tanpa teks akhir → `AgentRoundLimitError` (raise, tidak pernah mengarang jawaban).

Retry hanya di batas provider, hanya untuk kegagalan transien: `AgentRateLimitError` (hormati `Retry-After` ≤ 8s), `AgentProviderUnavailableError` (backoff eksponensial 0.5s/1.0s, maks 2 retry). Timeout, auth, dan eksekusi tool tidak pernah di-retry. Eksekusi tool punya efek samping bisnis; `create_draft_order` dua kali = dua draft.

## Keputusan kunci

- httpx mentah, bukan OpenAI SDK: provider terdokumentasi kirim HTTP 200 walau gagal (error di body). Keputusan sukses dilihat dari body (`error`/`choices`), bukan status.
- Chat Completions OpenAI-compatible, format standar diteruskan apa adanya, tanpa transformasi pintar. Pelajaran P2.8: patuh standar secara membosankan = nol masalah interop.
- JSON Schema tool ditulis tangan, bukan `model_json_schema()` Pydantic (Pydantic memancarkan `$defs`/`format: uuid` yang tidak terverifikasi aman lintas provider). Sinkronisasi wire↔Pydantic dijaga test.
- Tanpa `providers/base.py`: antarmuka satu implementasi = abstraksi tanpa pemakai kedua. Protocol diekstrak saat provider kedua ada.
- Tanpa retry otomatis di adapter (P2.1); retry transien-saja ditambah P2.6 setelah batas dipahami.
- `ToolExecutor.execute` berprimitif (id, name, arguments, context) supaya `app.tools` bebas impor `app.agents`.
- `AgentRunResult.transcript` tanpa instruksi sistem; dipersist dan direplay sebagai `AgentInput` berikutnya.
- Observabilitas = data murni (`AgentRunEvent`), bukan infra logging: identitas + waktu saja, tanpa argumen mentah/isi pesan/kredensial.
- Konfigurasi limit/retry via parameter konstruktor (default), env wiring menyusul bersama lapisan aplikasi.

## Hasil verifikasi

- Unit penuh (`--ignore=tests/integration`): 331 passed.
- Integrasi DB (`RUN_DB_SMOKE=1`): 30 passed, 3 skipped (PostgreSQL nyata).
- Smoke live (`RUN_AGENT_SMOKE=1`): 3 passed (teks, tool_calls nyata, loop penuh konvergen; model `deepseek-v4.1-flash-1017`).
- `ruff check` + `format --check`: bersih.

## Pembelajaran inti

- **Keberhasilan HTTP ≠ keberhasilan bisnis.** Provider kirim 200 untuk permintaan gagal. Kebenaran ada di body.
- **Batas kepercayaan paling murah di skema, bukan kode pemeriksa.** Argumen yang tidak pernah diminta tidak akan pernah dikirim.
- **Jangan memvalidasi gabungan objek terpercaya + tak terpercaya.** Pemisahan tipe = pemisahan kepercayaan.
- **Kegagalan juga data.** Model membaca `ok=false` dan menjawab jujur, bukan percakapan mati.
- **Retry adalah keputusan keamanan, bukan kenyamanan.** Pertanyaan penentu: apakah percobaan ulang mengubah keadaan bisnis?
- **Verifikasi berlapis menang** karena tiap lapisan punya sumber ketidakpastian berbeda: unit (logika), DB (SQL+transaksi), live (kontrak provider + perilaku model).

## Risiko terbuka (fase 3)

Agen → DB dalam satu percakapan hidup belum diuji (butuh lapisan aplikasi + data seeded). Kepatuhan isi jawaban model (Bahasa, kejujuran angka) terarah instruksi tapi tidak diuji otomatis. Deteksi persetujuan eksplisit pelanggan (`ya, konfirmasi` → approval) milik lapisan kanal, bukan agent. Konfigurasi limit/retry belum dari env.

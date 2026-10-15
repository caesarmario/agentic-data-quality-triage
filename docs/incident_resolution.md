# Resolve Dengan Bukti

**Status: implementasi tersedia; acceptance lokal dan live dicatat secara terpisah di bawah.**

Dokumen ini menjelaskan cara operator menggunakan Resolve, bukti pengujiannya, dan batas yang belum dibuktikan. Fitur ini bukan klaim production readiness atau jaminan bahwa seluruh warehouse sehat.

Hasil acceptance pada 11 Oktober 2026:

- Incident DQ `DQ-20261011-3E3C14` diperiksa lagi: **24 segmen ditemukan, 25 segmen diharapkan**.
- DAG `90_03_dag_dq_incident_revalidation` selesai dengan status `success`, tetapi hasil pemeriksaannya **`blocked`**.
- Pengguna telah melihat hasil blocked melalui panel ephemeral Discord. Incident tidak boleh ditutup dengan bukti tersebut.
- Pemeriksaan DQ positif berikutnya menghasilkan **25 dari 25 segmen**, tanpa menutup incident otomatis.
- Fixture schema terisolasi menghasilkan `blocked` saat ada kolom tambahan, kemudian `pass` setelah fixture disiapkan ulang melalui script admin terpisah.
- Mario menutup fixture schema melalui Streamlit dengan alasan `cocok dengan kontrak`. Backend dan ClickHouse menyimpan status resolved; Discord memperbarui message ID yang sama.
- Penutupan final DQ melalui Discord, pengujian visual mobile, dan fault-injection outbox live tidak boleh diasumsikan dari keberhasilan schema tersebut. Lihat ledger untuk status masing-masing.

Seluruh pemeriksaan Resolve menggunakan tools deterministik, bukan Gemini. Pengaturan Gemini untuk pengujian Copilot personal tetap aktif; workflow Resolve tidak menambah panggilan provider.

## Isi

- [Panduan operator](#panduan-operator)
- [Cara menggunakan Discord](#cara-menggunakan-discord)
- [Cara menggunakan Streamlit](#cara-menggunakan-streamlit)
- [Memahami hasil pemeriksaan](#memahami-hasil-pemeriksaan)
- [Mengapa penutupan bisa ditolak](#mengapa-penutupan-bisa-ditolak)
- [Jika ada masalah](#jika-ada-masalah)
- [Developer Reference](#developer-reference)
- [Acceptance Ledger](#acceptance-ledger)

## Panduan Operator

### Resolve Artinya Apa?

Resolve berarti:

> Masalah pada incident ini sudah diperiksa lagi. Hasil pemeriksaan lolos, dan operator setuju menutup incident ini.

Resolve **tidak memperbaiki data**. Tombol ini tidak menjalankan backfill, ingestion, dbt, perubahan schema, atau penghapusan data.

Contoh sederhana: sebuah pemeriksaan menemukan 24 dari 25 segmen negara/channel. Operator menyelidiki dan memperbaiki penyebabnya melalui workflow lain. Setelah itu, Resolve memeriksa apakah masalah pada incident tersebut masih ada. Jika hasil terbaru menunjukkan semua segmen yang diwajibkan tersedia, operator dapat meminta penutupan.

Penutupan satu incident tidak berarti seluruh tabel, seluruh dashboard, atau seluruh warehouse sudah sehat.

### Urutannya

```text
Pilih incident
  -> Buka Resolve
  -> Periksa Lagi
  -> Airflow memeriksa data atau schema
  -> Baca hasil dan bukti
  -> Jika lolos, isi alasan penutupan
  -> Konfirmasi Penutupan
  -> Backend memeriksa syarat sekali lagi
  -> Incident menjadi RESOLVED jika semua syarat masih terpenuhi
```

Pemeriksaan tidak langsung menutup incident. Hasil PASS pun masih memerlukan alasan dan konfirmasi operator.

### Apa yang Diperiksa?

| Jenis incident | Pemeriksaan penutupan |
|---|---|
| Data Quality | Rule penyebab incident, pada tabel, tanggal data, dan dimension yang sama, beserta prasyaratnya. |
| Schema drift | Schema aktual tabel dibandingkan dengan kontrak schema aktif yang sudah disetujui. |

Untuk DQ, PASS dari tanggal lain atau rule lain tidak boleh dipakai. Tabel atau denominator kosong tidak dianggap pulih hanya karena suatu rate terlihat `0`.

Untuk schema drift, tanggal incident adalah sejarah deteksi. Pemeriksaan baru membaca schema tabel saat ini, bukan schema sebuah partisi tanggal. Finding `warn` maupun `fail` masih memblokir penutupan. Workflow tidak mengubah kontrak agar schema yang bermasalah terlihat benar.

### Ada Dua Batas Waktu

| Batas | Artinya |
|---|---|
| Request: 15 menit sejak dibuat | Waktu ini termasuk antrean Airflow. Request yang melewati deadline tidak menjadi bukti penutupan yang sah. |
| Bukti PASS: maksimal 10 menit setelah pemeriksaan selesai | Konfirmasi harus menggunakan bukti yang masih baru. Jika sudah kedaluwarsa, periksa lagi. |

Bukti bisa menjadi tidak berlaku sebelum 10 menit jika incident, kontrak, data yang tercatat, atau schema berubah. Jangan menunggu sampai menit terakhir untuk mengonfirmasi.

## Cara Menggunakan Discord

### 1. Buka Incident

Cari incident card DataSentry di `dq-alerts`, atau hasil triage yang terkait dengan incident tersebut. Klik **Resolve**.

Panel yang muncul bersifat **ephemeral**: hanya terlihat oleh pengguna yang membukanya. Ini tidak menjadikan incident atau channel privat. Incident card tetap mengikuti akses channel Discord.

Periksa judul masalah, tabel, dan tanggal yang ditampilkan. Pastikan incident yang dipilih memang incident yang ingin ditutup.

### 2. Klik Periksa Lagi

DataSentry membuat atau menggunakan kembali satu request pemeriksaan melalui shared API. Bot segera merespons; handler Discord tidak menunggu sampai seluruh DAG selesai.

Saat masih menunggu atau berjalan:

```text
Pemeriksaan sedang berjalan.

Incident belum ditutup.
Kami hanya memeriksa kondisi data atau schema.

[Periksa Status]
```

Klik **Periksa Status** untuk membaca request yang sama. Jangan membuat banyak request hanya karena hasil belum muncul.

Hasil pemeriksaan juga dikirim sebagai card ke `dq-ops-private`. Jika delivery belum berhasil, status pemeriksaan tetap dapat dibaca melalui shared API atau Streamlit. Pengiriman notifikasi dan hasil pemeriksaan adalah dua hal berbeda.

**Open Workspace** hanya muncul jika URL operator UI sudah dikonfigurasi.

### 3. Baca Hasil

Hasil negatif yang sudah diketahui untuk `DQ-20261011-3E3C14`:

```text
Incident belum bisa ditutup.

Segmen yang diharapkan: 25
Segmen yang ditemukan: 24

Masih ada segmen yang belum lengkap.
Periksa penyebabnya atau lanjutkan proses perbaikan.
```

Tidak ada tombol untuk memaksa Resolve. Melihat warna hijau atau `success` pada DagRun tidak mengubah hasil bisnis ini menjadi PASS.

Contoh panel untuk hasil DQ positif 25/25; PASS masih memerlukan konfirmasi manusia:

```text
Pemeriksaan lolos.

Segmen yang diharapkan: 25
Segmen yang ditemukan: 25

Masalah pada incident ini tidak ditemukan dalam pemeriksaan terbaru.

[Tutup Incident] [Lihat Bukti]
```

### 4. Tulis Alasan dan Konfirmasi

Jika bukti PASS masih berlaku dan tidak ada penghalang, klik **Tutup Incident**. Isi alasan sepanjang **5 sampai 1.000 karakter**, setelah spasi di awal dan akhir dibuang.

Contoh alasan, hanya digunakan jika memang sesuai dengan kejadian sebenarnya:

```text
Segmen yang sebelumnya hilang sudah dimuat melalui workflow perbaikan terpisah.
Pemeriksaan penutupan terbaru menemukan 25 dari 25 segmen yang diwajibkan.
```

Bot menampilkan langkah konfirmasi. Baca scope dan alasan sekali lagi, lalu klik **Konfirmasi Penutupan**.

Keputusan akhir memerlukan akses pengguna ke `dq-ops-private`. Jika `DISCORD_APPROVER_ROLE_IDS` dikonfigurasi, pengguna juga harus memiliki role yang diizinkan. Konfirmasi terikat pada operator yang membuka panel; operator lain harus membuka panelnya sendiri.

Panel konfirmasi Discord saat ini memiliki timeout 120 detik. Jika panel kedaluwarsa, buka Resolve lagi dan periksa status bukti. Timeout panel tidak berarti incident telah ditutup.

**Batal** menutup panel/konfirmasi. Itu tidak membatalkan pekerjaan remediation maupun request Airflow yang sudah dibuat.

### 5. Setelah Berhasil Ditutup

Incident card yang sama diperbarui menjadi `RESOLVED`. Jika pesan lama hilang, existing replacement policy dapat membuat pesan pengganti; itu bukan incident baru.

Card penutupan menunjukkan hasil pemeriksaan, operator, dan alasan. Footer menggunakan provenance pemeriksaan sistem, bukan Gemini. Riwayat triage dan jawaban AI tetap tersedia sebagai riwayat; penjelasan kegagalan lama tidak boleh dibaca sebagai kondisi data terbaru.

Resolve tidak memberi izin kepada Gemini untuk menentukan status. Tidak ada panggilan LLM dalam workflow penutupan ini.

## Cara Menggunakan Streamlit

1. Buka tab **Incidents**, pilih incident, lalu buka **Overview**.
2. Klik **Resolve** untuk membuka panel. Membuka panel tidak membuat request pemeriksaan.
3. Isi **Nama operator**. Nama ini wajib untuk memulai pemeriksaan dan membuat keputusan.
4. Klik **Periksa Lagi**, atau gunakan **Periksa Status** jika request sudah berjalan.
5. Baca hasil, nilai yang diharapkan, nilai yang ditemukan, serta penghalangnya.
6. Jika lolos, isi **Alasan penutupan**, lalu klik **Lanjut ke konfirmasi**.
7. Baca ringkasan, centang persetujuan, lalu klik **Konfirmasi Penutupan**.

Nama operator di Streamlit adalah identitas yang diketik pada **trusted local console**. Nama itu dicatat, tetapi belum diverifikasi melalui SSO atau akun individual. Jangan menyebutnya sebagai verified identity.

Panel terbuka memperbarui status setiap lima detik hanya ketika Overview incident tersebut aktif. Berpindah tab, menutup panel, atau memilih incident lain tidak boleh membuat request baru. Rerun UI bukan persetujuan penutupan.

Setelah penutupan tersimpan dan lifecycle tersinkronisasi, incident tidak lagi muncul pada filter `active`. Gunakan filter resolved/history untuk membacanya kembali. Jika daftar masih tertinggal, gunakan **Perbarui daftar incident** dan baca status sinkronisasi.

Request ID, Airflow run ID, error code, dan evidence references berada di **Audit & Recovery**, dalam expander referensi teknis penutupan. Detail tersebut tidak menjadi isi utama panel operator.

### Jika Sinkronisasi Belum Selesai

Pesan seperti berikut bukan berarti perbaikan data sedang berjalan:

```text
Sinkronisasi belum selesai.
Keputusan tersimpan, tetapi status di ClickHouse atau Discord mungkin belum sama.
```

Keputusan di control store dan delivery ke tampilan lain memiliki status terpisah. Jangan membuat keputusan penutupan kedua untuk memperbaiki tampilan. Pengelola platform perlu memeriksa pending outbox dan mengirim ulang event yang sama.

## Memahami Hasil Pemeriksaan

| Status request | Bahasa sederhananya | Boleh ditutup? |
|---|---|---|
| `queued` | Menunggu giliran pemeriksaan. | Tidak. |
| `running` | Pemeriksaan sedang berjalan. | Tidak. |
| `pass` | Pemeriksaan lolos. | Hanya jika bukti masih berlaku, tidak ada penghalang, dan operator mengonfirmasi. |
| `blocked` | Pemeriksaan selesai, tetapi masalah masih ada atau prasyarat belum lolos. | Tidak. |
| `error` | Pemeriksaan mengalami error; bukti belum cukup. | Tidak. |
| `expired` | Request atau bukti tidak lagi berlaku untuk penutupan. | Tidak. Periksa status lalu buat pemeriksaan baru secara eksplisit jika diizinkan. |

Backend dapat tetap menyimpan status request `pass` sementara UI menjelaskan bahwa bukti PASS sudah kedaluwarsa. Waktu berlaku diperiksa lagi saat konfirmasi; label PASS historis tidak cukup.

**DagRun `success` berarti pekerjaan DAG selesai. Hasil bisnis `pass` berarti pemeriksaan incident lolos. Keduanya tidak sama.**

## Mengapa Penutupan Bisa Ditolak?

- Rule incident masih gagal, menghasilkan warning, atau buktinya tidak lengkap.
- Bukti berasal dari request, incident, revision, tanggal, tabel, atau scope lain.
- Incident berubah sejak pemeriksaan dibuat.
- Kontrak aktif berubah, atau ada schema/data change yang terdeteksi setelah pemeriksaan.
- Ada hasil pemeriksaan relevan yang lebih baru dan belum lolos.
- Bukti sudah kedaluwarsa.
- Remediation terkait masih pending, approved tetapi belum dieksekusi, sedang berjalan, atau hasil execution-nya belum jelas.
- Pemeriksaan dilakukan sebelum remediation terkait selesai.
- API, control store, warehouse, atau koneksi Airflow belum tersedia.
- Operator tidak memiliki akses penutupan yang diperlukan.

Resolve tidak menyetujui, mengeksekusi, atau membatalkan remediation. Selesaikan atau batalkan request remediation terkait melalui workflow yang sesuai, lalu periksa lagi. Jangan meminta Gemini untuk mengabaikan penghalang ini.

## Jika Ada Masalah

| Yang terlihat | Tindakan operator |
|---|---|
| Masih ditemukan 24 dari 25 segmen. | Lanjutkan investigasi/perbaikan terpisah. Jangan menutup incident. |
| Pemeriksaan belum tersedia. | Hubungi pengelola platform untuk memeriksa API, control store, dan autentikasi Airflow. |
| Tidak ada respons atau timeout setelah klik. | Hasil request belum tentu gagal. Periksa status request yang sama terlebih dahulu. Jangan membuat request baru secara membabi buta. |
| Data berubah atau bukti kedaluwarsa. | Jalankan pemeriksaan baru setelah keadaan stabil. |
| Alasan terlalu pendek. | Tulis alasan 5 sampai 1.000 karakter yang menjelaskan bukti dan keputusan. |
| Bot offline atau panel konfirmasi kedaluwarsa. | Buka kembali Resolve saat bot tersedia, atau periksa request melalui Streamlit. Tidak ada auto-close. |
| Discord belum berubah, tetapi keputusan tersimpan. | Periksa `synchronization_pending`; pengelola menindaklanjuti outbox. |
| Kegagalan baru muncul setelah incident ditutup. | Monitoring dapat membuka episode kegagalan baru sesuai identity policy. Itu bukan manual Reopen. |

Data bisa berubah setelah pemeriksaan atau setelah incident ditutup. Pemeriksaan ini membuktikan kondisi pada waktu tertentu, bukan jaminan permanen dan bukan pengganti monitoring.

---

## Developer Reference

This section describes the current code paths, not a completed production-readiness claim. Operational, human, concurrency, and visual acceptance remain bounded by the ledger below.

### Architecture and Ownership

```text
Discord Resolve / Streamlit Overview
                 |
          Shared control-plane API
                 |
       PostgreSQL lifecycle/request store
                 |
       Airflow Public API v2 + JWT
                 |
  90_03_dag_dq_incident_revalidation
                 |
      dq_runner scoped deterministic checks
                 |
  ClickHouse evidence + PostgreSQL request result
                 |
       Human reason and confirmation
                 |
  Transactional lifecycle decision + durable outbox
                 |
      ClickHouse projection / audit / Discord
```

The API server does not receive a Docker socket and does not insert DagRuns into Airflow metadata tables. The existing Airflow runner helper executes fixed request stages in `dq_runner`. PostgreSQL is the transactional control store, not the analytical warehouse.

| Responsibility | Current code |
|---|---|
| Public request and result models | [agent/resolution_contracts.py](../agent/resolution_contracts.py) |
| Lifecycle transitions, closure gate, dispatch, synchronization | [agent/tools/incident_resolution.py](../agent/tools/incident_resolution.py) |
| PostgreSQL transactions and record access | [agent/tools/incident_resolution_store.py](../agent/tools/incident_resolution_store.py) |
| Idempotent control-store schema migration | [infra/init/postgres/03_incident_resolution.sql](../infra/init/postgres/03_incident_resolution.sql) |
| Scoped DQ/schema evidence capture | [pipelines/dq/incident_revalidation.py](../pipelines/dq/incident_revalidation.py) |
| Airflow v2/JWT client | [apps/common/airflow_api.py](../apps/common/airflow_api.py) |
| HTTP endpoints | [apps/api/main.py](../apps/api/main.py) |
| Shared UI/bot API client | [apps/common/control_plane.py](../apps/common/control_plane.py) |
| Discord panel, modal, confirmation handlers | [apps/discord_bot/resolution.py](../apps/discord_bot/resolution.py) |
| Discord rendering | [apps/discord_bot/formatters.py](../apps/discord_bot/formatters.py) |
| Streamlit UI and helper gates | [apps/streamlit/resolution.py](../apps/streamlit/resolution.py) |
| Operator workspace integration | [apps/streamlit/app.py](../apps/streamlit/app.py) |

### Shared API

| Method and endpoint | Purpose | Normal response |
|---|---|---|
| `GET /api/v1/alerts/{alert_ref}/resolution-preview` | Authoritative scope, revision, blockers, availability, latest request, resolution, and synchronization status. | `200` |
| `POST /api/v1/alerts/{alert_ref}/revalidations` | Create or reuse a durable request; dispatch only the allowlisted DAG. | `202` |
| `GET /api/v1/incident-revalidations/{request_id}` | Read durable request progress, result, deadline, and evidence. | `200` |
| `POST /api/v1/alerts/{alert_ref}/resolve` | Confirm closure against a valid server-owned PASS request. | `200` |

Mutation endpoints use the existing `CONTROL_PLANE_APPROVAL_TOKEN` boundary, carried by the shared client in `X-Control-Plane-Token`. Never expose that token in client-visible messages, documentation examples, or audit payloads. The two read endpoints follow the existing local control-plane read boundary; this feature does not add public multi-tenant authentication.

Revalidation creation accepts only:

```json
{
  "actor": "Operator name",
  "idempotency_key": "stable-key-for-this-explicit-check"
}
```

Final confirmation accepts:

```json
{
  "resolution_request_id": "00000000-0000-4000-8000-000000000001",
  "expected_incident_revision": 1,
  "idempotency_key": "stable-key-for-this-explicit-decision",
  "actor": "Operator name",
  "reason": "Fresh scoped verification passed; closing this incident only."
}
```

These are contract illustrations, not executable acceptance evidence. Request identity must come from the API, and revision must come from the authoritative preview. The public models forbid extra request fields. Clients cannot submit PASS, observed values, SQL, table scope, provider choice, or permission overrides.

Current API limits are an actor of 1-200 characters, an idempotency key of 1-100 characters, a positive incident revision, a UUID request ID, and a trimmed reason of 5-1,000 characters. Streamlit further limits operator names to 120 characters and rejects control characters in the name.

Error mapping: `404` for missing incident/request, `409` for conflicts or blocked closure, `422` for invalid input, `401/403` at the applicable authorization boundary, and `503` when required dependencies are unavailable. HTTP `202` is not PASS, and HTTP `200` on a status read is not closure.

### Airflow Execution Contract

The manual DAG is [90_03_dag_dq_incident_revalidation.py](../dags/90_03_dag_dq_incident_revalidation.py):

```text
t00_start
  -> t10_load_request
  -> t20_run_scoped_checks
  -> t30_persist_validation_result
  -> t40_notify_validation_result
  -> t90_finish
```

It uses `schedule=None`, `catchup=False`, `max_active_runs=1`, the shared `Asia/Bangkok` timezone configuration, and zero task retries. The request deadline is 15 minutes from creation, including queue time; the DAG also has a 15-minute execution timeout. These clocks are distinct.

The only DAG parameter is a UUID `resolution_request_id`. The deterministic run ID is:

```text
manual__incident_revalidation_<request_uuid>
```

The Airflow client obtains a JWT at `/auth/token` and uses `/api/v2/dags/.../dagRuns`. Server-side configuration consists of `AIRFLOW_API_BASE_URL`, `AIRFLOW_API_USERNAME`, and `AIRFLOW_API_PASSWORD`. PostgreSQL access uses `DQ_CONTROL_DATABASE_URL`. Credentials belong in private runtime environment configuration, not in this document or user request bodies.

Dispatch first checks for the deterministic run ID. After an uncertain POST, subsequent reconciliation checks that same run instead of blindly sending another POST. An unknown trigger is not converted into PASS. Explicit retries must retain their idempotency identity until the previous outcome is known.

The check stage writes fresh DQ/schema observability evidence. The persist stage records the business result; the notify stage synchronizes through the outbox. Infrastructure errors are recorded as errors, never disguised as successful validation. Notification failure may leave synchronization pending without changing the business verdict.

### Evidence Gate

DQ scope is loaded from authoritative incident state with `kind=dq_failure`, table, metric, dimension, and business date. Current adapters are contract-bound to the project's supported Orders rules: positive row count, freshness, configured not-null/accepted-values checks, segment coverage, row-count anomaly, duplicate rate, late-arriving rate, and configured non-negative revenue. This is not an arbitrary SQL validation API.

Prerequisites include table/required-column existence, a supported date column, positive scoped rows, finite observations, and valid positive denominators for rate checks. Segment coverage verifies contracted identities rather than accepting an arbitrary equal count as sufficient recovery.

Schema scope uses `kind=schema_drift`, current table metadata, and the active approved repository contract. A clean comparison is required; severity-gate success alone is insufficient. The existing isolated schema acceptance alias is test infrastructure, not permission for clients to select contracts or modify production schemas.

Before closure, the backend reloads the incident/request and checks latest request identity, exact scope, revision, PASS result, evidence references, timestamp validity, maximum TTL, active contract hash, current schema hash, data watermark, newer non-PASS results, and linked remediation state. Completed remediation must precede the evidence observation, not merely the final confirmation.

No external LLM participates in evidence capture or closure. Existing Gemini configuration for personal Copilot testing is unaffected. Resolve does not add a mutation tool to MCP or to the agent tool registry.

### Persistence, Replay, and Outbox

The migration adds these non-destructive records under `dq_control`:

| Table | Role |
|---|---|
| `incident_controls` | Authoritative status, revision, episode, stable identity, and incident snapshot. |
| `incident_revalidations` | Durable scoped request, timestamps, business result, and dispatch progress. |
| `incident_revalidation_keys` | Bind idempotency identities to durable requests and actors. |
| `incident_resolution_decisions` | Durable final human decision and its idempotency identity. |
| `incident_outbox` | Pending projection, audit, and notification events with per-sink completion flags. |

Current transactions take a lifecycle-wide advisory lock and incident row `FOR UPDATE` locks. This serializes competing changes and is a conservative local-demo design, not a high-throughput locking benchmark. Confirmation rechecks revision inside the transaction. Client-side button disabling is only a usability safeguard.

The decision retains `resolved_by`, `resolved_at`, `resolution_reason`, `resolution_request_id`, `validation_dag_run_id`, evidence references, contract hash, revision, and result summary. Existing Alert Ref, system key, original creation time, severity history, and report references remain intact.

Outbox flags distinguish ClickHouse projection, audit, and Discord delivery. Pending sinks do not roll back an already committed decision. Synchronization replays the existing event and latest authoritative projection; it must not create another closure decision. The preview's `synchronization_pending` is the current operator-facing synchronization signal.

Do not assume an independent always-on outbox retry daemon or exactly-once network delivery. A Discord send may have an ambiguous acknowledgement. Result-notification delivery and final incident-card continuity need explicit acceptance evidence, including recovery cases. The existing incident message registry and replacement policy remain responsible for durable card identity.

Alert production uses newest check results before selecting failures, and shared lifecycle transitions preserve active triage state. Late triage must not overwrite a resolved incident. A genuinely newer failure may open a new episode under existing identity policy; old FAIL history must not reopen an incident. Schema fingerprints and dates retain the existing producer identity rules.

### Technical Bounds and Known Limitations

- Freshness protection is point-in-time. The warehouse is not transactionally locked across evidence capture and closure.
- The data watermark fingerprints bounded active ClickHouse part metadata and recorded pipeline activity. It is not a complete row-by-row checksum or proof against every unlogged direct warehouse write.
- Part merges can conservatively invalidate otherwise healthy evidence; the safe response is a new check, not a force-close override.
- Watermark capture is bounded to 10,000 parts and segment verification to 1,000 configured segments. Exceeding a bound must not be represented as complete PASS evidence.
- Current contracts and supported engines/rules constrain scope. Unsupported incidents need an explicit implementation/contract change, not a client-supplied query.
- `max_active_runs=1` and the request deadline can cause queued requests to expire. There is no automatic paid retry, pipeline repair, or automatic incident closure.
- A worker interrupted after claiming a check is not silently re-executed. Reconcile durable request and Airflow state before an operator initiates another request.
- Linked remediation detection is conservative and relies on recorded approval/execution linkage. Work performed outside platform recording is not an end-to-end guaranteed signal.
- Discord final authorization validates guild, channel access, message/bot correlation, current state, requester, and optional approver roles. Streamlit identity remains trusted local input, not SSO.
- Both interfaces depend on the shared API for writes. A failed API call has no local mutation fallback.
- Persistent Discord actions require the bot online; an ephemeral modal/confirmation session is not durable. The validation request and result are durable.
- Resolved incident-card rendering, message continuity, simultaneous operators, outbox recovery, and desktop/mobile/light/dark readability must not be reported accepted until demonstrated.

## Acceptance Ledger

Retained Airflow task logs and current API/Discord read-back were inspected. Pending entries are not inferred from code or mocked UI tests.

| Scenario | Current evidence | Acceptance status |
|---|---|---|
| Negative DQ scoped revalidation | Incident `DQ-20261011-3E3C14`: observed 24, expected 25; new DAG `success`, business result `blocked`. | Reported verified negative case. |
| Discord blocked result | User verified the blocked result in an ephemeral panel. | Verified human observation for this negative case only. |
| Positive DQ PASS | Request `021417a4-5374-41c2-89fe-05d1b92c1bb5`: 25/25, PASS; fresh human-triggered request `205ab903-b11c-4519-9fe1-ad519ff8a1ae` repeated PASS. | Operational check verified; final Discord decision recorded separately. |
| Schema negative and positive checks | Isolated `dq_resolution_acceptance.schema_fixture`; negative request `efdff26d-f72e-4652-add1-b1308d784b56` blocked with one finding; positive `3dae80af-1584-4ed8-8b2c-f15982eb31d2` passed with zero findings. | Both operational checks verified. |
| Delegated live closure from Discord | Owner explicitly delegated the remaining demo closure. The coding agent used the authenticated browser, a fresh PASS, factual reason, and final confirmation for `DQ-20261011-3E3C14`. | Verified delegated UI acceptance; not a claim that the owner clicked the final confirmation. |
| Human final closure from Streamlit | `DQ-20261011-5C580A`, Mario, 2026-10-11 16:14:38.825181 UTC, reason `cocok dengan kontrak`, revision 2. | Human confirmation verified; post-fix queue success notice requires separate visual evidence. |
| Concurrent Discord/Streamlit decision | Real PostgreSQL concurrent confirmation regression admits one decision and rejects the conflicting one. | Regression accepted through DAG 91; simultaneous live human clicks not performed. |
| Expired/stale evidence and interrupted request recovery | User saw expired historical PASS; backend retained active incident. Fresh explicit check produced a new request. Contract, timestamp, timeout and replay regression tests included. | Expiry safety observed live; infrastructure interruption recovery only regression-tested. |
| Outbox and same-message resolved update | Original schema and DQ messages remained unchanged across closure. New fixture `DQ-20261013-1C69EA` retained message `1559344212544069734` after a deliberate pre-HTTP application fault and replay of event `994ffd4f-751c-4821-9ab1-4efc09d1e9a3`. | Normal and scoped application-fault recovery verified; not an actual Discord outage. |
| DAG 91 regression | Discord: `manual__resolve_discord_20261011T155327` (300 passed); API: `manual__resolve_api_20261011T155706` (218); UI: `manual__resolve_ui_20261011T160842` (222). Hardened all: `manual__resolve_hardened_20261011T161628` (1,559 passed, 12 skipped, one dependency warning). All five tasks succeeded and readiness was 27/27 in each run. | Post-final-change run is recorded below when complete. |
| Visual acceptance | Discord desktop/mobile and Streamlit wide/narrow/light/dark not accepted in this draft. | Pending. |

### Evidence to Add for Each Accepted Case

Record the exact DAG ID and run ID, final DagRun state, every task state, retained log location, incident reference, request ID, scope, business verdict, expected/observed values, and evidence references. For regression, include test count, skips/warnings, and platform readiness result.

For final closure, add the operator interaction evidence, decision revision/time/reason, audit correlation, outbox synchronization state, and Discord message IDs before and after the transition. Redact credentials and unnecessary personal identifiers. Never manufacture IDs or substitute an example response for an actual result.

Use DAG `91_dag_dq_platform_validation` for code acceptance and `90_03_dag_dq_incident_revalidation` for the scoped operational checks. Positive data/schema cases must use approved synthetic or isolated acceptance scope, not destructive changes to working project tables. Mocks or REST inspection alone do not establish live button/modal acceptance. Record whether the person clicked directly or explicitly delegated an authenticated browser interaction.

### Retained Operational Runs

For every revalidation below, the DAG ID is `90_03_dag_dq_incident_revalidation` and the run ID is `manual__incident_revalidation_<request_id>`. All six tasks (`t00_start`, `t10_load_request`, `t20_run_scoped_checks`, `t30_persist_validation_result`, `t40_notify_validation_result`, `t90_finish`) reached `success`. Business verdicts remain separate:

| Request ID | Incident | Business result |
|---|---|---|
| `c0e73abd-3875-4f30-8da9-0805bc004f02` | DQ `DQ-20261011-3E3C14` | Blocked: 24/25 segments. |
| `021417a4-5374-41c2-89fe-05d1b92c1bb5` | Same DQ incident | PASS: 25/25 segments. |
| `205ab903-b11c-4519-9fe1-ad519ff8a1ae` | Same DQ incident | Fresh PASS requested by the human through Discord. |
| `efdff26d-f72e-4652-add1-b1308d784b56` | Schema `DQ-20261011-5C580A` | Blocked: unexpected fixture column. |
| `3dae80af-1584-4ed8-8b2c-f15982eb31d2` | Same schema incident | PASS: zero findings; used for the human closure. |
| `ae768d40-b5d1-45d0-9a17-186f435330cd` | DQ `DQ-20261011-3E3C14` | PASS: 25/25; expired before the earlier confirmation completed. No closure decision. |
| `cf7c92ba-f3c9-4b1e-ac1c-b8f0c04c1d26` | Same DQ incident | Fresh PASS: 25/25; used for the delegated browser closure. |

Read retained logs without changing the request:

```text
docker exec dq_airflow_api_server python /opt/airflow/project/scripts/read_airflow_validation_logs.py --dag-id 90_03_dag_dq_incident_revalidation --run-id manual__incident_revalidation_YOUR_REQUEST_ID
```

The synthetic DQ partition was prepared and corrected through daily orchestrator runs `manual__resolve_missing_segment_20261011` and `manual__resolve_repaired_baseline_20261011`. Those are separate pipeline operations, not actions performed by Resolve. Schema setup used `scripts/prepare_resolution_schema_fixture.py`, which refuses a table without the explicit fixture ownership marker. Only the isolated fixture column was changed; Resolve itself performs no DDL.

Review found seven missing fail-closed cases in the initial all-suite run `manual__resolve_all_20261011T161213`. They were fixed before the successful hardened run; the failed run is retained rather than presented as accepted evidence.

The schema decision initially captured pending synchronization while its outbox was being delivered. Current preview state is authoritative for the pending warning; the historical decision snapshot is not. A formatter regression now prevents a stale warning after synchronization finishes.

### DQ Closure on 2026-10-12

The owner explicitly delegated the final demo closure after an expired confirmation session had left the incident active. The coding agent requested the fresh read-only check through Discord, submitted the factual reason below, and clicked Confirm Close within the new evidence window.

```text
Delegated demo closure: the fresh read-only Airflow check confirmed all 25 required segments. No data repair was executed by this closure.
```

The shared API recorded `resolved` at `2026-10-12T14:09:35.993648Z`, incident revision 3, request `cf7c92ba-f3c9-4b1e-ac1c-b8f0c04c1d26`. PostgreSQL read-back confirmed exactly one decision. The outbox's projection, audit, and delivery flags are complete and current preview `synchronization_pending=false`.

The exact-rule evidence is `clickhouse://dq.dq_check_results/ddd565d3-87c9-4b82-a86b-7c14cc2e01cf`; the positive-row prerequisite is `clickhouse://dq.dq_check_results/022c008e-d26d-4c3a-b2cb-dc6bc3194b06`. The operational DagRun `manual__incident_revalidation_cf7c92ba-f3c9-4b1e-ac1c-b8f0c04c1d26` and all six tasks succeeded, with retained scoped-check logs inspected.

ClickHouse interaction audit `aad0da0f-2d68-4ce7-9b03-878920ce4768` and lifecycle audit `6f5d1f80-1c6b-4b04-9015-81768c4e7b9e` recorded success. Discord REST and the visible client confirmed message `1558869741165215817` now renders RESOLVED, 25 expected/25 observed, the original issue, factual reason, and system-check provenance. There was no LLM call or data repair from this workflow.

This establishes the normal delegated UI closure and same-message synchronization path, not simultaneous two-human confirmation or injected live outbox-failure recovery.

### Final Regression for the Workspace and Closure Changes

DAG `91_dag_dq_platform_validation`, run `manual__workspace_native_scheme_all_20261012T224015`, completed successfully after the latest code patches. All five tasks succeeded. Retained logs reported **1,923 passed, 12 skipped, 10 warnings**, with platform readiness **27/27**. The preceding named Discord/API/UI suites and the final UI-only run are enumerated in [the workspace ledger](discord_workspace_acceptance.md). Direct pytest was developer feedback, not substituted acceptance.

The desktop English/Indonesian and Light/Dark views were inspected, including immediate native theme switching. The owner deferred mobile/narrow rendering on 2026-10-13 and confirmed that a second Discord user is not currently available; two-human live decisions remain pending. Fixture-scoped application fault/replay was subsequently verified through DAG `90_04`; see [its acceptance ledger](resolution_outbox_acceptance.md) for retained failures, exact task resumption, and recovery proof. Final DAG 91 run `manual__desktop_docs_final_all_20261013T000807` passed all five tasks with 1,975 passed, 12 skipped, 10 warnings, and 27/27 readiness checks; retained logs were inspected.

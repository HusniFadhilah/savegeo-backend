# Hardcoded Data Audit + Migration Plan

Date: 2026-08-01

## 1. Ringkasan kondisi backend

`savegeo/backend` (FastAPI + Postgres) sudah punya sebagian infrastruktur config-in-DB
dari rebuild sebelumnya: `system_config` (admin-editable settings), `gee_credentials`
(metadata only, key file di Supabase Storage/local disk), `uploaded_models` (model
registry). Tapi ditemukan **bug nyata**: services analisis (`carbon_service.py`,
`vegetation_service.py`, `landcover_service.py`, `download_service.py`) baca threshold
default (cloud_threshold, scale, vis_min/max/palette, co2_factor) langsung dari
`app.core.config.Settings` (env-only), **bukan** dari `system_config` — padahal
`system_config` sudah lengkap berisi key yang sama persis dan admin panel sudah bisa
menulis ke sana. Efeknya: admin ubah "Carbon vis min/max" atau "Cloud threshold" di
panel, DB ke-update, tapi endpoint analisis tetap pakai nilai lama dari `.env`. Bug ini
sudah diperbaiki di pass ini (lihat 6).

Selain itu ditemukan model registry ganda (`app/inference/model_registry.py`,
file-based JSON, warisan dari backend lama) yang berjalan paralel dengan
`uploaded_models` (DB, yang benar-benar dipakai). Tidak berbahaya (tidak pernah
benar-benar dipakai untuk resolve path karena semua caller sudah pass `model_path`
eksplisit dari `get_active_model_path(db, ...)`), tapi tetap bikin file
`var/saved_models/registry.json` kosong tiap kali `CarbonInferenceEngine`
diinisialisasi — dead weight, bukan risiko keamanan.

## 2. Temuan hardcoded utama

| Area | Temuan | Klasifikasi |
|---|---|---|
| `app/core/config.py` | Semua secret default ke string kosong, DSN default cuma dummy lokal — **tidak ada secret bocor di source** | KEEP_IN_CODE (struktur Settings) |
| `app/core/config.py` `default_cloud_threshold`/`carbon_scale`/`veg_scale`/`lc_scale`/`max_pixels`/`carbon_vis_*`/`carbon_co2_factor` | Duplikat sumber kebenaran dengan `system_config` — **bug**, sudah diperbaiki (carbon+vegetation), landcover masih pending | MOVE_TO_DB (carbon/vegetation: DONE; landcover: TODO) |
| `app/registries/carbon_dataset_registry.py`, `landcover_dataset_registry.py` | GEE asset ID, band names, loader functions (`load_carbon_reference_ee` dispatch table) | KEEP_IN_CODE (algoritmik, terikat GEE API) |
| ...tapi field display (`name`, `full_name`, `description`, `attribution`, `limitations`, is-active) di registry yang sama | Admin plausibel mau ubah tanpa redeploy | MOVE_TO_DB — **DONE** via tabel `datasets` (carbon), landcover baru terseed belum dipakai di endpoint |
| `app/registries/vegetation_index_registry.py` | Formula NDVI/EVI/SAVI/dst, band mapping, classification bins | KEEP_IN_CODE (rumus indeks matematis, harus deterministic) |
| `app/registries/map_layer_registry.py` | Basemap tile URL template (OSM/Esri/CARTO), termasuk urutan Satellite=default | KEEP_IN_CODE saat ini — dianggap konfigurasi platform stabil, bukan per-tenant; kandidat MOVE_TO_DB kalau nanti butuh multi-tenant basemap berbeda |
| `app/inference/model_registry.py` (`ModelRegistry`, JSON file) | Sistem registry model paralel yang tidak dipakai untuk resolve path produksi | **Tidak diklasifikasikan ulang** — direkomendasikan dihapus di fase depan setelah dikonfirmasi tidak ada pemanggil lain |
| `scripts/seed_admin.py` | Tidak ada password default — wajib `SEED_ADMIN_USERNAME`/`SEED_ADMIN_PASSWORD` env atau prompt interaktif, min 8 karakter | Sudah benar (SECURITY: aman) |
| `app/services/storage_service.py` | Path traversal check (`_safe_local_path`: tolak absolute path & `..`) sudah ada | Sudah benar (SECURITY: aman) |
| `app/providers/arcgis_client.py` | Domain allowlist (`_is_allowed_domain`) untuk request ArcGIS eksternal | Sudah benar (SECURITY: SSRF-safe untuk ArcGIS) |
| `app/api/routes/regions.py` | Proxy ke `REGION_API_BASE_URL` — base URL dari env/DB, tapi endpoint sub-path (`/province`, `/city`, dst) hardcoded string, bukan user input — tidak ada SSRF path karena base URL admin-controlled, bukan request-controlled | KEEP_IN_CODE (env-controlled base, aman) |
| `admin_users` table | Tidak ada kolom role/permission sebelum pass ini — semua admin implisit full-access | **DIIMPLEMENTASI**: `roles`/`permissions`/`role_permissions` + `admin_users.role_id` (nullable, additive) |
| Tidak ada audit trail | Perubahan config/model/credential tidak tercatat | **DIIMPLEMENTASI**: `audit_logs` table, di-wire ke 6 titik mutasi admin paling sensitif |
| `.env` di `.gitignore` | Confirmed sudah ada, tidak pernah commit `.env` asli | Sudah benar |
| CORS | `allow_origins` = daftar eksplisit dari `Settings`, bukan `"*"` + `allow_credentials=True` (kombinasi itu terlarang oleh spec CORS & akan gagal) | Sudah benar |
| `GEECredential.to_dict()` | Bahkan dengan `include_path=True`, cuma expose `bucket_path` (string path storage), **tidak pernah** expose isi private key JSON — key mentah cuma pernah didownload sementara ke temp file saat `ee.Initialize()`, langsung dihapus setelahnya | Sudah benar (SECURITY: aman) |

## 3. Klasifikasi ringkas

- **KEEP_IN_CODE**: GEE asset ID, loader functions per provider, rumus indeks vegetasi
  (NDVI/EVI/dst), band mapping, basemap tile URL template, enum action/status teknis,
  validasi schema Pydantic.
- **MOVE_TO_ENV**: `DATABASE_URL`, `JWT_SECRET_KEY`, semua API key provider AI/ArcGIS,
  `GEE_KEY_FILE`/`GEE_SERVICE_ACCOUNT` fallback, `UPLOAD_DIR`/`MODEL_DIR`,
  `ALLOWED_ORIGINS` default, `SUPABASE_URL`/`SUPABASE_SERVICE_ROLE_KEY`.
- **MOVE_TO_DB**: threshold/scale/vis default analisis (carbon+vegetation sudah,
  landcover belum), status aktif/nonaktif dataset + display metadata (carbon sudah via
  tabel `datasets`), role/permission (baru diimplementasi), audit trail (baru
  diimplementasi).
- **SEED_TO_DB**: default admin (opt-in eksplisit, sudah benar), `DEFAULT_CONFIGS` ke
  `system_config` (sudah ada), permission/role default (baru:
  `scripts/seed_rbac.py`), dataset display metadata awal dari registry (baru:
  `scripts/seed_datasets.py`).
- **SECURITY_RISK**: **tidak ditemukan** secret/private key/password bocor di source
  code, log, atau response API dalam audit ini.

## 4. Skema DB baru (migration `0002_rbac_datasets_audit`)

### `datasets`
Overlay admin-editable di atas registry statis. `key` match dengan key registry.
Kolom: `id, key(unique), module, provider_type, name, full_name, description,
attribution, limitations(JSONB), unit, resolution, year_min, year_max,
is_active(bool, default true), display_order, extra_metadata(JSONB), created_at,
updated_at`. Index unique di `key`.

### `roles` / `permissions` / `role_permissions`
- `roles`: `id, name(unique), description, is_default, created_at`.
- `permissions`: `id, code(unique, mis. "config.write"), description`.
- `role_permissions`: composite PK `(role_id, permission_id)`, FK cascade delete.
- `admin_users.role_id`: kolom baru, **nullable**, FK ke `roles.id`. NULL = admin
  legacy full-access (perilaku sebelum kolom ini ada) — tidak ada admin existing yang
  kehilangan akses akibat migration ini.

### `audit_logs`
`id, admin_user_id(FK, nullable), action, resource_type, resource_id, detail(JSONB),
ip_address, created_at(indexed)`. Insert-only, best-effort (gagal tulis log tidak
pernah menggagalkan aksi admin yang sebenarnya — lihat `app/services/audit_service.py`).

### Belum diimplementasi (didesain, didokumentasikan sebagai next-phase)
- `dataset_providers` — normalisasi provider metadata terpisah dari `datasets`
  (sekarang provider info masih di kolom `provider_type` + `extra_metadata` JSONB di
  tabel `datasets` — cukup untuk kebutuhan sekarang, normalisasi penuh belum genting).
- `dataset_versions` — versioning per-tahun dataset (mis. GLC_FCS30D per periode) —
  sekarang masih ditangani di registry (`year_min`/`year_max`/`supported_glc_fcs30d_year`
  function), belum dipindah karena butuh perubahan lebih dalam ke loader functions.
- `external_services` — katalog URL layanan eksternal (region API, BMKG, DEMNAS,
  ArcGIS) — saat ini semua sudah MOVE_TO_ENV (bukan hardcoded), jadi tabel terpisah
  belum mendesak; pertimbangkan kalau butuh admin toggle per-service tanpa restart.

## 5. Strategi migrasi bertahap (status)

1. **Inventory + laporan** — SELESAI (dokumen ini).
2. **Pindahkan data paling berisiko/sering berubah ke DB** — SEBAGIAN: analysis
   defaults (carbon+vegetation) SELESAI, dataset display metadata (carbon) SELESAI,
   RBAC+audit SELESAI. Landcover analysis defaults + dataset overlay: TODO (lihat 7).
3. **Repository/service layer DB-with-fallback** — SELESAI, pattern dibuktikan di
   `app/repositories/dataset_repo.py` (dipakai `carbon_service.get_carbon_dataset_list`)
   dan `app/services/config_service.get_analysis_defaults` (dipakai carbon+vegetation
   services). Fallback ke `Settings`/registry statis kalau tidak ada row DB — tidak ada
   breaking change kalau tabel kosong.
4. **Seed script data awal** — SELESAI: `scripts/seed_rbac.py`,
   `scripts/seed_datasets.py`, keduanya idempotent (diverifikasi jalan 2x, run kedua
   0 perubahan).
5. **Endpoint admin CRUD** — BELUM — `datasets`/`roles`/`audit_logs` baru bisa diedit
   lewat SQL langsung atau shell, belum ada route admin. Direkomendasikan sebagai
   langkah lanjut kalau frontend butuh UI untuk ini (lihat 8, Risiko tersisa).
6. **Hapus fallback lama** — BELUM, dan memang belum boleh — fallback masih aktif
   sesuai instruksi "jangan hapus registry lama sebelum fallback DB terbukti jalan".

## 6. Perubahan yang sudah dilakukan

1. **Bug fix analysis defaults**: `app/services/config_service.py` dapat fungsi baru
   `get_analysis_defaults(db)` yang baca `cloud_threshold`/`carbon_scale`/`veg_scale`/
   `lc_scale`/`max_pixels`/`carbon_vis_min`/`carbon_vis_max`/`carbon_vis_palette`/
   `carbon_co2_factor` dari `system_config`, fallback ke `Settings` kalau row belum
   ada. Di-wire ke `carbon_service.py` (semua 3 endpoint analyze), `vegetation_service.py`
   (3 endpoint: analyze/compare/timeseries), `download_service.py`. `landcover_service.py`
   sengaja **tidak** diubah pass ini karena route landcover belum pernah punya akses
   `db: Session` sama sekali — perubahan itu butuh nambah `Depends(get_db)` ke 3 route
   handler + thread `db` ke banyak helper function internal (`get_landcover_image`,
   `summarize_landcover_classes`, dst), risiko regresi lebih tinggi untuk 803-baris file
   dengan banyak provider branch di bawah tekanan waktu — didokumentasikan sebagai
   TODO prioritas berikutnya, bukan dilewati diam-diam.
   `max_pixels` di `gee_common.py::_event_area_ha` dan `disaster_service.py::get_disaster_dem_slope`
   sengaja **tetap** di `Settings` (bukan dipindah ke DB) — direklasifikasi sebagai
   KEEP_IN_ENV karena ini batas aman teknis GEE (`reduceRegion maxPixels`), bukan
   parameter bisnis yang admin butuh ubah per dataset/tahun.
2. **Tabel + model baru**: `DatasetEntry`, `Role`, `Permission`, `AuditLog` (SQLAlchemy
   models), migration `alembic/versions/0002_rbac_datasets_audit.py`.
3. **Repository fallback layer**: `app/repositories/dataset_repo.py`
   (`get_overrides_by_key`, `apply_override`) — dipakai di
   `carbon_service.get_carbon_dataset_list` untuk filter `is_active=false` dan merge
   override nama/deskripsi/atribusi/limitasi. **Dibuktikan hidup**: toggle
   `is_active=false` langsung di tabel `datasets` (SQL), dataset hilang dari
   `/api/carbon/datasets` tanpa restart server; toggle balik, muncul lagi.
4. **RBAC additive**: `app/core/security.py::require_permission(code)` — dependency baru,
   **belum dipasang** ke route manapun (opt-in, supaya tidak ada risiko regresi ke
   endpoint yang sudah jalan). `admin_users.role_id` nullable, NULL = full access
   (semua admin existing tidak terpengaruh).
5. **Audit log**: `app/services/audit_service.py::log_audit()` (best-effort, gagal
   tulis tidak menggagalkan aksi admin), di-wire ke `PUT /admin/config`,
   `POST/DELETE /admin/gee/credentials`, `POST /admin/gee/credentials/{id}/activate`,
   `POST /admin/models/upload`, `DELETE /admin/models/{id}`.
6. **Seed scripts**: `scripts/seed_rbac.py` (9 permission, 2 role: admin+viewer),
   `scripts/seed_datasets.py` (34 dataset row dari carbon+landcover registry).

## 7. Verifikasi + hasil

Dijalankan terhadap Postgres lokal beneran (`db_savegeo`, bukan mock/SQLite):

| Command | Hasil |
|---|---|
| `python -m compileall app scripts alembic` | OK, tidak ada syntax error |
| `alembic upgrade head` (0001 -> 0002) | OK — 13 tabel total terkonfirmasi via `\dt` |
| `python -m scripts.seed_rbac` (2x) | "Seeded 9 permissions and 2 roles" kedua kali (idempotent — upsert-style) |
| `python -m scripts.seed_datasets` (2x) | Run 1: "Seeded 34 new dataset rows (0 already existed)". Run 2: "Seeded 0 new dataset rows (34 already existed)" — idempotent terbukti |
| `uvicorn app.main:app` start | OK, `ee_initialized: true` dari kredensial DB real |
| `GET /api/health` | 200, `active_models: 39` |
| `GET /api/models` | 200, list 39 model |
| `GET /api/carbon/datasets` | 200, count 9 (setelah filter compatible-model) |
| `GET /api/landcover/datasets` | 200, count 16 |
| `GET /api/admin/config/public` | 200 (regression check untuk fix urutan route dari sesi sebelumnya, masih benar) |
| **Live proof: dataset override** | `UPDATE datasets SET is_active=false WHERE key='WCMC'` via psql langsung -> `WCMC` hilang dari `/api/carbon/datasets` (count 9->8) tanpa restart server -> di-set balik `true` -> muncul lagi |
| **Live proof: analysis defaults** | `UPDATE system_config SET value='9.99' WHERE key='carbon.co2_factor'` -> `config_service.get_analysis_defaults(db)` langsung baca `9.99` -> dikembalikan ke `3.67` |
| Admin login smoke test | **Tidak berhasil diverifikasi** — password admin real di DB lokal ini tidak diketahui asisten (dibuat sesi lain); JWT/login flow sendiri sudah diverifikasi jalan di sesi audit koneksi sebelumnya dengan admin buatan sendiri |
| Vegetation/landcover/carbon analyze dengan GEE beneran | **Tidak dijalankan penuh** — butuh AOI valid + model GEE-deployable + waktu tunggu GEE (bisa menit), di luar jendela waktu sesi ini. Yang diverifikasi: route wiring benar (`require_ee` dependency, `db` param lengkap di semua 3 endpoint carbon + 3 endpoint vegetation), tidak ada `NameError`/import error saat compile maupun saat server start |

Bug yang ditemukan **selama proses implementasi sendiri** (bukan di kode lama, tapi
risiko yang muncul saat wiring `db` ke service — dicatat untuk transparansi): substitusi
awal otomatis (`settings.X` -> `config_service.get_analysis_defaults(db)`) sempat
menaruh referensi ke `db` di 3 fungsi (`vegetation_service.analyze_vegetation`,
`analyze_vegetation_compare`, `analyze_timeseries` dan awalnya juga di
`landcover_service.py`, `disaster_service.py`, `gee_common.py`) yang route-nya belum
pernah punya `Depends(get_db)`. Terdeteksi lewat `python -m py_compile` (langsung
`SyntaxError`/tidak — sebagian lolos compile tapi akan `NameError` saat runtime) dan
scanner khusus yang dibuat untuk cek setiap pemanggilan `config_service.get_analysis_defaults(db)`
punya parameter `db` di signature function-nya. Semua kasus diperbaiki: vegetation+timeseries
di-wire penuh (route + service), landcover+disaster+gee_common sengaja dikembalikan ke
`Settings` (bukan dipaksa wire) karena route-nya tidak dirancang untuk itu di pass ini.

## 8. Risiko tersisa

1. **Landcover analysis defaults masih env-only** — admin ubah `analysis.lc_scale` di
   panel tidak berefek ke `/api/analyze/landcover*`. Butuh nambah `db: Session =
   Depends(get_db)` ke 3 route handler landcover + thread ke beberapa helper — di luar
   scope aman untuk pass ini, didokumentasikan sebagai prioritas #1 follow-up.
2. **`datasets`/`roles`/`audit_logs` belum ada endpoint admin CRUD** — perubahan cuma
   bisa lewat SQL langsung atau script. Kalau frontend butuh UI kelola dataset/role,
   ini next step (Tahap 5 di rencana migrasi).
3. **`require_permission` belum dipasang ke route manapun** — RBAC infrastruktur ada
   tapi belum enforced; semua admin masih full-access (`role_id IS NULL`) sampai ada
   yang eksplisit assign role dan endpoint mulai pakai dependency ini.
4. **`ModelRegistry` (file JSON) masih ada sebagai dead code** — tidak berbahaya, tapi
   bikin file `registry.json` kosong ke-generate terus. Rekomendasi hapus setelah
   dikonfirmasi tidak ada pemanggil lain di luar `app/inference/*`.
5. **Full GEE analyze smoke test belum dijalankan end-to-end** di pass ini (lihat
   tabel verifikasi) — risiko murni "belum diverifikasi", bukan "diketahui rusak".
   Endpoint yang PALING berisiko kena regresi (carbon, karena paling banyak diubah)
   sudah diverifikasi sampai lapisan resolve-config+dataset-listing; lapisan GEE
   compute sendiri tidak disentuh sama sekali oleh perubahan pass ini.
6. **`app_settings`/`external_services`/`dataset_providers`/`dataset_versions` sebagai
   tabel terpisah belum ada** — `system_config` sudah memenuhi peran `app_settings`;
   yang lain didesain di dokumen ini tapi belum diimplementasi (lihat 4).

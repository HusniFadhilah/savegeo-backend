# PROMPT: Ingest "Banjir Sumatera 2025" ke Disaster Intelligence Dashboard SaveGeo

Tempel prompt ini utuh ke Claude Code (atau agent lain) yang bekerja di repo
`savegeo/backend` + `savegeo/frontend` untuk mengeksekusi tugas ini end-to-end.

## Tujuan

Data citra tegak (orthophoto) resolusi sangat tinggi hasil tasking banjir
Sumatera 2025 ada di lokal:

```
C:\Users\ACER\Downloads\Banjir Sumatera 2025\
├── 30-11-2025\
│   ├── Aceh Tenggara BSG-104-20251129-084819-407954813-ortho.zip
│   ├── Batuphat Barat-ortho.zip
│   ├── Cot Kuta-ortho.zip
│   ├── Sipirok BSG-113-20251129-072829-407836324-ortho.zip
│   ├── Takengon BSG-104-20251129-084859-407957340-ortho.zip
│   └── Sumatera Barat 1-ortho\           (sudah ter-extract)
│       ├── BSG-119-..._browse.png
│       ├── BSG-119-..._metadata.json
│       └── BSG-119-..._ortho.tif
└── 01-12-2025\
    ├── 0e2d4c62-...-Tiff.tif             (1.9GB, TANPA metadata.json/zip — mosaik lepas)
    ├── Aceh - BSG-105-20251130-080649-408120110-ortho.zip
    ├── Sumut - BSG-104-20251130-080129-408120097-ortho.zip
    └── Sumut BSG-104-20251130-080154-408120098-non-ortho.zip
```

Setiap `*-ortho.zip` (kecuali yang sudah ter-extract) berisi 3 file:
`{scene_id}_browse.png` (preview kecil), `{scene_id}_metadata.json`
(acquisitionDate, sensorName, gsd, cloudCoverPercent, waterPercent,
geometry footprint GeoJSON Polygon, dst — schema sudah diverifikasi, lihat
contoh di bawah), `{scene_id}_ortho.tif` (raster GeoTIFF terorthorektifikasi,
~150-195MB, GSD ~1m — kemungkinan besar sensor BlackSky "Global-14" via
tasking BSG/BNPB).

Contoh isi metadata.json (real, dari `Cot Kuta-ortho.zip`):
```json
{
  "id": "BSG-114-20251129-024929-407836328",
  "acquisitionDate": "2025-11-29T02:49:29.039",
  "sensorName": "Global-14",
  "gsd": 1.0616324,
  "cloudCoverPercent": 0.1,
  "waterPercent": 8.2,
  "geometry": {"type": "Polygon", "coordinates": [[...]]},
  "width": 8584, "height": 7986
}
```

Target: event ini harus muncul di modul **Disaster Intelligence Dashboard**
SaveGeo (`savegeo/backend` FastAPI + Supabase Postgres, `savegeo/frontend`
Vite/React), **bisa dilihat scene-nya** (viewer peta, bukan cuma thumbnail)
dan **dianalisis secara lengkap** (flood change, water extent, forest change
— 3 model yang sudah live: `flood_change_v1`, `water_segmentation_v1`,
`forest_change_v1`, lihat `app/registries/disaster_model_registry.py`).

## Konteks arsitektur yang WAJIB dibaca dulu

1. `backend/docs/disaster-redesign-contract.md` — kontrak API lengkap modul
   ini (model, endpoint admin `/admin/disasters/...`, endpoint user
   `/disasters/...`, alur publish).
2. `backend/app/repositories/disaster_repo.py` — semua helper CRUD
   (`create_event`, `create_aoi`, `add_imagery`, `set_primary_imagery`,
   `create_run`, `upsert_result`, `publish_result`).
3. `backend/app/services/disaster_analysis_service.py` — cara 3 model
   dijalankan. **PENTING**: ketiganya TIDAK pernah membaca file raster dari
   `SatelliteImagery` row — mereka hanya memakai `imagery.acquisition_date`
   untuk query Sentinel-1/2 dari Google Earth Engine langsung (`±5 hari`
   window, lihat `_window()`). Jadi hasil analisis kuantitatif
   (flooded_area_ha, dst) akan selalu berbasis Sentinel, BUKAN dari raster
   lokal BlackSky — raster lokal cuma untuk **visual viewer** resolusi
   tinggi, bukan input model.
4. `backend/app/providers/local_raster_provider.py` — sudah ada primitive
   rasterio (`GridSpec`, `load_raster_to_grid`, `WarpedVRT`) yang dipakai
   modul carbon/landcover untuk baca GeoTIFF lokal/COG. **Reuse pattern ini**
   untuk membangun tile server lokal, jangan bikin dari nol.
5. `backend/app/services/gee_common.get_tile_url()` — tile URL yang dipakai
   3 model GEE selalu berbentuk template Leaflet standar
   `.../tiles/{z}/{x}/{y}` (dari `image.getMapId()['tile_fetcher'].url_format`).
   Endpoint tile lokal yang akan dibuat **harus menghasilkan bentuk URL yang
   sama** (`.../{z}/{x}/{y}.png`) supaya komponen frontend yang sudah ada
   (`ResultTileLayer.tsx`, `SwipeCompareMap.tsx`, `SatelliteViewer.tsx`,
   `MapView.tsx`) bisa langsung memakainya tanpa modifikasi — mereka semua
   cuma menerima string URL template ke Leaflet `TileLayer`.
6. `backend/app/core/config.py` — `settings.upload_dir` (`./var/uploads`)
   sudah ada sebagai lokasi penyimpanan file upload. Pakai subfolder di sini
   untuk raster lokal, JANGAN commit file GeoTIFF (100MB-1.9GB) ke git.
   Cek `.gitignore` root/backend — kalau `var/uploads/` belum di-ignore,
   tambahkan.

## Gap yang harus dibangun (belum ada di codebase)

`SatelliteImagery` model (`app/db/models/satellite_imagery.py`) punya kolom
`preview_tile_url` tapi **tidak ada kolom untuk path file lokal**, dan
**tidak ada endpoint** yang men-serve GeoTIFF lokal sebagai XYZ tile
(semua tile selama ini datang dari GEE). Kamu harus membangun:

### A. Migration kecil
Tambah migration baru di `alembic/versions/` (ikuti pola
`0006_disaster_intelligence.py`) menambah 2 kolom nullable ke
`satellite_imagery`:
- `source_kind` (String, default `"gee"`, nilai lain `"local_upload"`)
- `local_file_path` (String, path relatif ke `settings.upload_dir`)

Update `SatelliteImagery.to_dict()` + constructor + `disaster_repo.add_imagery()`
untuk meneruskan 2 field baru ini (`data.get("source_kind", "gee")`,
`data.get("local_file_path")`).

### B. Local COG tile service
File baru `app/services/local_imagery_tile_service.py`:
- Fungsi `render_tile(local_file_path: str, z: int, x: int, y: int) -> bytes | None`
  — hitung bounding box tile XYZ (Web Mercator) standar, buka raster via
  `rasterio` + `WarpedVRT` (reuse pattern dari `local_raster_provider.py`,
  reproject ke EPSG:3857, resolusi native 256x256), render band 1-3 sebagai
  RGB PNG (cek dulu jumlah band aktual tiap `_ortho.tif` via `gdalinfo` —
  kemungkinan RGB 3-band atau RGBA 4-band, JANGAN asumsikan tanpa cek).
  Return `None` kalau tile di luar cakupan raster (biar endpoint balas 404,
  bukan crash) — sama seperti pola `load_raster_to_grid` yang sudah ada.
- Cache dataset handle rasterio per file (mis. `functools.lru_cache` kecil
  atau dict module-level) supaya tidak buka ulang file 150-1900MB tiap
  request tile.

### C. Endpoint tile
Tambah route di `app/api/routes/disaster_events.py` (user-facing, karena
User juga harus bisa lihat viewer-nya, bukan cuma Admin):
`GET /disasters/imagery-tiles/{imagery_id}/{z}/{x}/{y}.png`
— `Depends(get_current_user)`, load `SatelliteImagery` by id, pastikan
`source_kind == "local_upload"` dan event induknya `status == "published"`
(404 kalau tidak, konsisten dengan aturan "404 bukan 403" di kontrak),
panggil `render_tile`, balas `Response(content=png_bytes, media_type="image/png")`,
404 kalau `render_tile` balas `None`.
Tambah juga versi admin (`Depends(get_current_admin)`, tanpa filter
published) di `admin_disaster.py` supaya Admin bisa preview sebelum publish:
`GET /admin/disasters/imagery-tiles/{imagery_id}/{z}/{x}/{y}.png`.

### D. Pra-proses raster jadi Cloud-Optimized GeoTIFF
Sebelum di-serve, cek tiap `*_ortho.tif` dan file `0e2d4c62-...-Tiff.tif`
(1.9GB) dengan `gdalinfo` apakah sudah punya internal tiling + overview
pyramids. Kalau belum, convert dulu (`gdal_translate -of COG -co
COMPRESS=DEFLATE -co BLOCKSIZE=512` atau `gdalwarp` + `gdaladdo`) — tanpa
ini, render tile zoom rendah akan sangat lambat/berat memori karena baca
seluruh raster tiap request.

## Langkah eksekusi data

### 1. Script persiapan data (`scripts/prepare_banjir_sumatera_2025.py`)
- Extract semua `*-ortho.zip` (skip yang sudah ter-extract di folder
  `Sumatera Barat 1-ortho\`) ke staging dir.
- Untuk tiap scene: baca `{id}_metadata.json` → ambil `acquisitionDate`,
  `sensorName`, `gsd`, `cloudCoverPercent`, `geometry` (footprint Polygon
  GeoJSON), `width`/`height`.
- Convert `{id}_ortho.tif` ke COG (langkah D di atas), pindah ke
  `var/uploads/disaster_imagery/banjir-sumatera-2025/{slug-lokasi}/`
  (slug dari nama file: `aceh-tenggara`, `batuphat-barat`, `cot-kuta`,
  `sipirok`, `takengon`, `sumatera-barat-1`, `aceh-bsg-105`,
  `sumut-bsg-104-ortho`, `sumut-bsg-104-non-ortho`).
- File lepas `0e2d4c62-...-Tiff.tif` (1.9GB, **tanpa metadata**): jalankan
  `gdalinfo` untuk dapatkan bounding box/CRS asli, tapi **tanggal akuisisi
  dan lokasi persisnya tidak diketahui dari file ini** — tandai sebagai
  perlu verifikasi manual (kemungkinan mosaik gabungan tanggal 01-12-2025
  berdasarkan nama folder). Jangan menebak tanggal, laporkan ke user untuk
  konfirmasi sebelum dipakai sebagai imagery row `acquisition_date`.
- Tulis `manifest.json` ringkasan semua scene: `{slug, location_label,
  province, local_file_path, acquisition_date, sensor, gsd_m,
  cloud_coverage_pct, footprint_geojson, bbox, centroid}` (`bbox`/`centroid`
  bisa dihitung dari `footprint_geojson` pakai `app/services/geo_utils.py`
  `bbox_and_centroid()` yang sudah ada — reuse, jangan tulis ulang).

### 2. Keputusan struktur data — VERIFIKASI DULU sebelum eksekusi
Ada 8-9 lokasi terpisah (Aceh Tenggara, Batuphat Barat, Cot Kuta, Sipirok,
Takengon, Sumatera Barat 1, Aceh/BSG-105, Sumut/BSG-104 ortho+non-ortho),
tersebar di 3 provinsi (Aceh, Sumatera Utara, Sumatera Barat), jaraknya
ratusan km satu sama lain. `DisasterEvent.province`/`district` memang sudah
`list[str]` (mendukung 1 event banyak provinsi), dan `AnalysisRun.aoi_id`
mendukung banyak AOI per event (satu run = satu AOI). Tapi sebelum
implementasi, **baca `frontend/src/features/admin/components/disaster/AoiManager.tsx`
lebih dulu** untuk cek apakah UI Admin saat ini hanya mengelola SATU AOI
aktif per event (pola `get_active_aoi` = AOI terbaru saja) atau sudah
mendukung banyak AOI/lokasi per event dengan baik di list-nya:
- **Kalau AoiManager hanya menampilkan/mengedit AOI tunggal**: buat
  **1 DisasterEvent per lokasi** (8-9 event, nama misal
  "Banjir Cot Kuta, Aceh Utara — 29 Nov 2025"), supaya tiap event punya 1
  AOI konsisten dengan UI. Semua tetap bisa dikelompokkan lewat filter
  `disaster_type=flood&year=2025` di halaman list User.
- **Kalau backend API + Admin frontend memang sudah oke banyak AOI/run per
  event** (verifikasi lewat `GET /admin/disasters/{id}` yang mengembalikan
  `aoi` — cek apakah field ini singular atau bisa banyak): buat **1
  DisasterEvent "Banjir Sumatera 2025"** dengan banyak AOI (satu per
  lokasi) + satu set 3 run per AOI.
Pilih salah satu, sebutkan alasannya di commit message, JANGAN merge semua
footprint jadi satu AOI raksasa (akan mencakup ratusan km area tidak
relevan antar provinsi, memboroskan compute GEE dan salah secara analitik).

### 3. Script ingestion (`scripts/ingest_banjir_sumatera_2025.py`)
Login admin (`POST /admin/auth/login`) lalu, per event/lokasi:
1. `POST /admin/disasters` — buat event: `disaster_type="flood"`,
   `province`/`district` dari manifest, `event_date`/`start_date` dari
   `acquisitionDate` scene (tanggal 2025-11-29 atau 2025-11-30 sesuai
   folder), `severity` (isi manual sesuai tingkat dampak per lokasi, jangan
   asal — tanyakan ke user kalau tidak jelas), `description` ringkas Bahasa
   Indonesia, `source="Citra tasking very-high-resolution (BSG) + BNPB"`,
   `status="draft"`.
2. `POST /admin/disasters/{id}/aoi` — `geojson` = footprint dari manifest
   (atau AOI yang digambar admin manual kalau footprint scene dianggap
   kurang presisi mewakili area terdampak banjir sebenarnya — footprint
   citra ≠ luas genangan), `source="upload_geojson"`.
3. `POST /admin/disasters/{id}/imagery` — **dua baris minimal per lokasi**:
   - Baris "post" lokal resolusi tinggi: `phase="post"`,
     `satellite=metadata.sensorName` (mis. `"BlackSky Global-14"`),
     `acquisition_date`, `sensor=metadata.sensorName`,
     `resolution_m=metadata.gsd`, `cloud_coverage_pct=metadata.cloudCoverPercent`,
     `data_source="BSG tasking"`, `is_primary=true`, dan (via kolom baru dari
     langkah A) `source_kind="local_upload"`, `local_file_path=...` —
     `preview_tile_url` diisi setelah dapat `imagery_id` hasil response,
     format `f"/api/disasters/imagery-tiles/{imagery_id}/{{z}}/{{x}}/{{y}}.png"`
     (perlu PATCH lanjutan atau endpoint create langsung terima
     `preview_tile_url`, cek urutan create vs update field ini di
     `disaster_repo.add_imagery` — kemungkinan perlu 2 langkah: create dulu
     baru PATCH `preview_tile_url` kalau endpoint update imagery ada, atau
     tambahkan endpoint tersebut kalau belum ada di kontrak A).
   - Baris "pre" via Sentinel: `phase="pre"`, `satellite="Sentinel-2 Optical"`
     atau `"Sentinel-1 SAR GRD"`, `acquisition_date` ~10-14 hari sebelum
     tanggal event (mis. `2025-11-15`), `data_source="Copernicus (GEE)"`,
     `source_kind="gee"` (tidak perlu `local_file_path`) — ini yang dipakai
     sebagai `pre_imagery_id` di run supaya `disaster_analysis_service`
     query Sentinel window yang benar-benar sebelum banjir.
   - Baris "post" via Sentinel (kalau tanggal capture lokal terlalu dekat
     dengan tanggal event sehingga Sentinel-1 SAR window ±5 hari tetap
     dapat data) — boleh pakai baris post lokal yang sama sebagai
     `post_imagery_id` (compute function cuma baca `.acquisition_date`,
     jadi baris manapun dengan tanggal post yang benar sah dipakai; tidak
     wajib baris terpisah, cukup pastikan tanggalnya akurat).
4. `POST /admin/disasters/{id}/analyses` × 3 (satu per
   `flood_change_v1`/`water_segmentation_v1`/`forest_change_v1`) dengan
   `aoi_id` dari langkah 2, `pre_imagery_id`/`post_imagery_id` dari langkah 3.
5. `POST /admin/analyses/{run_id}/run` × 3 — tangkap `AnalysisError` (mis.
   Sentinel-1/2 tidak ada data bersih di window tanggal itu — kalau gagal,
   coba perlebar window tanggal pre/post secara manual, JANGAN fabricate
   angka).
6. `GET /admin/disasters/{id}/qc` — pastikan `ready_to_publish=true`.
7. `PATCH /admin/disasters/{id}` — `status="published"`.
8. `POST /admin/analyses/{run_id}/publish` × 3 (per run yang completed) —
   ini yang membuat hasil terlihat di endpoint User
   (`GET /disasters/{id}/statistics`, `/layers`, dll — publish result
   terpisah dari publish event, keduanya wajib).
9. (Opsional) `POST /admin/disasters/{id}/hotspots` untuk titik terdampak
   parah, `impact_level` diisi berdasarkan `flooded_area_ha`/statistik run
   flood_change.

## Verifikasi akhir (wajib, jangan lapor selesai tanpa ini)

1. Backend: `python -m compileall app/` bersih,
   `python -c "from app.main import app; print(len(app.openapi()['paths']))"`
   naik (route baru ter-mount).
2. Migration jalan bersih ke Supabase Postgres real (`alembic upgrade head`),
   bukan cuma dry-run.
3. Jalankan `ingest_banjir_sumatera_2025.py` sungguhan ke DB real (bukan
   mock), verifikasi tiap event/run tersimpan via query langsung.
4. Buka `GET /disasters` (token user biasa) — event(s) banjir Sumatera
   2025 muncul dengan `available_analysis_count > 0`.
5. Buka `GET /disasters/{id}/layers` — `satellite.post_tile_url` mengarah
   ke endpoint tile lokal baru dan **benar-benar me-render tile PNG** saat
   di-fetch manual (`curl` satu tile `{z}/{x}/{y}` konkret, cek ukuran file
   & buka gambarnya — bukan cuma cek status 200).
6. Buka frontend `/pemetaan-bencana/{id}` — pastikan viewer scene (Swipe
   atau Pre/Post) menampilkan citra resolusi tinggi tanpa error tile 404,
   KPI tiles terisi angka nyata dari 3 model, legend + peta perubahan
   flood/water/forest tampil.
7. Laporkan berapa lokasi berhasil di-ingest penuh vs yang gagal (mis.
   Sentinel-1/2 tidak ada data bersih di tanggal tertentu) — jangan tutupi
   kegagalan sebagian.

## Batasan file (jangan menyimpang)

- Migration baru: 1 file di `alembic/versions/`, edit minimal di
  `satellite_imagery.py` model + `disaster_repo.add_imagery`.
- File baru: `app/services/local_imagery_tile_service.py`.
- Edit: `app/api/routes/disaster_events.py` (tambah 1 route),
  `app/api/routes/admin_disaster.py` (tambah 1 route admin-preview).
- Script baru: `scripts/prepare_banjir_sumatera_2025.py`,
  `scripts/ingest_banjir_sumatera_2025.py`.
- Jangan commit file raster (`.tif`/`.zip`) ke git — pastikan
  `var/uploads/` masuk `.gitignore`.
- Jangan sentuh 3 fungsi compute model (`_compute_flood_change`, dst) di
  `disaster_analysis_service.py` — mereka sudah benar, hanya butuh
  `imagery_id` dengan tanggal yang tepat.

# Prompt: Integrasikan Data Gempa NTT ke Pemetaan Bencana (savegeo)

Prompt siap-pakai (untuk dijalankan di sesi ini atau sesi baru) untuk memasukkan
data gempa bumi NTT ke Disaster Intelligence Dashboard savegeo, mencakup
tampilan citra (scene viewer) dan analisis semaksimal kapabilitas platform saat
ini. Ditulis setelah memeriksa langsung isi folder sumber dan skema backend
yang relevan — bukan asumsi.

---

## 1. Konteks & Sumber Data

Folder sumber: `C:\Users\ACER\Downloads\NTT Earthquake`

Isi (diverifikasi langsung, per 2026-08-22):

| File | Ukuran | Isi |
|---|---|---|
| `Labuan Bajo GeoTIFF.zip` | ~244 MB | ortho.tif + browse.png + metadata.json + ortho-pan.tif + ortho-mask.tif + lisensi |
| `Pelabuhan Maurole GeoTIFF.zip` | ~228 MB | sama seperti di atas |
| `Lambaleda 2 GeoTIFF.zip` | ~206 MB | sama |
| `Desa Riung Cibal 2 GeoTIFF.zip` | ~201 MB | sama |
| `Satar Punda Bar GeoTIFF.zip` | ~236 MB | sama |
| `Satar Punda Bar 2 GeoTIFF.zip` | ~190 MB | sama |
| `Tagol 2 GeoTIFF.zip` | ~204 MB | sama |
| `Pelabuhan Laurentius Say GeoTIFF.zip` | ~174 MB | sama |
| `Desa Liang Deruk GeoTIFF` (tanpa ekstensi `.zip`, tapi formatnya ZIP) | ~185 MB | sama |
| `NTT EQ Before and After/` | - | 8 PNG (Before/After per 4 lokasi: Desa Liang Deruk, Labuan Bajo, Laurentius Say, Maurole) - **preview visual saja, TIDAK georeferenced, bukan raw data** |

**Setiap ZIP = SATU akuisisi (bukan pasangan before/after).** Semua 9 lokasi
adalah citra **PASCA-gempa** (BlackSky, Aug 16-21 2026 berdasarkan
`_metadata.json` tiap file, contoh: `BSG-102-20260816-234459-468344577`).
**Tidak ada citra PRA-gempa mentah di folder ini** kecuali 4 PNG preview di
`NTT EQ Before and After/` yang sudah dirender pihak lain (tanpa georeferensi).

### 1.1 Detail teknis per file GeoTIFF (dari `_metadata.json`, contoh Labuan Bajo)

```json
{
  "id": "BSG-102-20260816-234459-468344577",
  "acquisitionDate": "2026-08-16T23:44:59.190",
  "sensorName": "Global-2",
  "gsd": 1.2044867,
  "cloudCoverPercent": 0.0,
  "georeferenced": true,
  "orthorectified": true,
  "width": 7871, "height": 6121,
  "bitsPerPixel": 12,
  "geometry": { "type": "Polygon", "coordinates": [[...]] }
}
```

- **Sumber: BlackSky** ("Global-2" satellite constellation) — **citra komersial**, bukan Sentinel/Landsat gratis.
- Resolusi **~1.2 m/piksel**, jauh lebih tajam dari semua sumber yang sudah ada di modul "Eksplorasi Scene Satelit" (Sentinel-2 10m adalah yang tertinggi saat ini).
- `geometry` di tiap metadata.json = **footprint asli citra** (polygon lon/lat) → bisa dipakai langsung sebagai AOI per lokasi, tidak perlu digambar manual.
- Setiap zip juga punya `_browse.png` (preview cepat) dan `_ortho-mask.tif` (mask piksel valid).

### 1.2 ⚠️ LISENSI — WAJIB DICEK SEBELUM PUBLISH

`LicenseAgreement-1.1.txt` di tiap zip menyebut tier **"ING Level-2 (Domestic
Agencies)"** dengan referensi ke halaman lisensi BlackSky
`imagery-products-analytic-products-rights-international-government`. Ini
citra **berlisensi komersial/pemerintah**, bukan data terbuka seperti
Sentinel/Landsat/Copernicus yang selama ini dipakai di seluruh platform ini.

**Sebelum meng-upload/menampilkan citra ini di savegeo (apalagi kalau
dashboard bisa diakses publik), konfirmasi dulu ke pemilik data/lisensi**
apakah hak penggunaannya mengizinkan ditampilkan di platform ini (internal
saja vs. publik, ada batas retensi/watermark, dsb). Jangan asumsikan bebas
pakai hanya karena filenya ada di komputer ini.

---

## 2. Yang Sudah Ada di savegeo (jangan dibangun ulang)

Sudah diverifikasi langsung ke kode:

- **Disaster Intelligence Dashboard** (`/pemetaan-bencana`) — arsitektur:
  `DisasterEvent` (1) → `DisasterAOI` (banyak baris boleh dibuat, **tapi hanya
  yang PALING BARU per event yang aktif** - lihat `disaster_repo.get_active_aoi`,
  `ORDER BY created_at DESC LIMIT 1`) → `SatelliteImagery` (banyak baris,
  `phase` = "pre"/"post") → `AnalysisRun` + hasil.
- Endpoint admin sudah lengkap (`app/api/routes/admin_disaster.py`):
  `POST /disasters`, `POST /disasters/{id}/aoi`, `POST /disasters/{id}/imagery`,
  `POST /disasters/{id}/analyses`, `POST /analyses/{run_id}/run`,
  `POST /analyses/{run_id}/publish`, dst.
- **`SatelliteImagery.preview_tile_url`** (string) adalah satu-satunya field
  untuk citra tampilan — **mengharapkan URL tile yang SUDAH BISA DIAKSES**
  (pola `https://earthengine.googleapis.com/.../tiles/{z}/{x}/{y}`), **bukan
  file upload langsung**. GeoTIFF mentah tidak bisa langsung dipasang di sini.
- Fitur baru **"Eksplorasi Scene Satelit"** (`/imagery/*`, modul
  `ImageryModule.tsx`) sudah mendukung 10 sumber (Sentinel-1/2(L2A+L1C)/3/5P
  x5/Landsat 8-9/ASTER/VIIRS), swipe-compare, filter+masking awan — tapi
  **BlackSky belum ada di katalog ini** (lihat Tugas 3).
- **Model analisis otomatis yang benar-benar berjalan** (`disaster_model_registry.py`):
  hanya **3 dari 7** yang `enabled: True` — `flood_change_v1` (SAR, deteksi
  genangan), `water_segmentation_v1` (NDWI), `forest_change_v1` (NDVI).
  **`building_change_v1`, `building_segmentation_v1`, `road_damage_v1` —
  yang paling relevan untuk KERUSAKAN BANGUNAN akibat gempa — semuanya
  `enabled: False`** karena belum ada model terlatih. Ini bukan bug, sudah
  didokumentasikan di kode: *"Belum tersedia - memerlukan model deteksi
  perubahan bangunan yang belum dilatih."*

**Implikasi penting:** "dianalisis secara lengkap" untuk gempa bumi **tidak
bisa berarti deteksi kerusakan bangunan otomatis** — kapabilitas itu belum
ada di platform ini sama sekali. Yang bisa dilakukan sekarang:
1. Perbandingan visual pra/pasca resolusi tinggi (swipe-compare) — **bisa**.
2. `forest_change_v1` (NDVI) — kalau relevan (longsor yang membuka
   vegetasi/tutupan lahan) — **bisa**, pakai Sentinel-2 otomatis.
3. `flood_change_v1`/`water_segmentation_v1` — kalau ada efek sekunder
   terkait air (retakan bendungan, likuifaksi tergenang, tsunami minor) —
   **bisa**, pakai Sentinel-1/2 otomatis.
4. Deteksi kerusakan bangunan/jalan — **tidak bisa otomatis**, harus manual
   (analis melihat langsung citra BlackSky 1.2m before/after via
   swipe-compare dan menandai kerusakan sendiri, atau hotspot manual lewat
   `POST /disasters/{id}/hotspots`).

Jangan bikin field/angka yang terlihat seperti "skor kerusakan otomatis"
padahal sebenarnya belum ada modelnya — itu melanggar prinsip yang sudah
ditegakkan di `disaster_model_registry.py` sendiri (spec 55-56: jangan pernah
memalsukan hasil).

---

## 3. Tugas

### Tugas 0 — Konfirmasi dengan user sebelum mulai
1. **Tanggal gempa yang sebenarnya** (bukan tanggal citra) — semua GeoTIFF
   adalah PASCA-gempa (16-21 Aug 2026); `event_date` di `DisasterEvent`
   butuh tanggal kejadian gempanya sendiri, bukan tanggal potret.
2. **Sumber citra PRA-gempa** — folder ini tidak menyediakannya (kecuali 4 PNG
   non-georeferenced). Opsi: (a) pakai modul Eksplorasi Scene Satelit untuk
   cari scene Sentinel-2/Landsat sebelum tanggal gempa di titik yang sama,
   (b) user punya sumber BlackSky/Planet pra-gempa terpisah, (c) lewati
   perbandingan pra/pasca presisi-tinggi untuk lokasi yang PNG before/after-nya
   sudah ada (4 lokasi), pakai Sentinel-2 generik untuk 5 lokasi sisanya.
3. **Izin lisensi** (lihat §1.2) — konfirmasi status penggunaan sebelum publish.
4. **Struktur event**: satu `DisasterEvent` besar "Gempa Bumi NTT" dengan 9
   AOI terpisah TIDAK didukung arsitektur saat ini (hanya AOI terbaru yang
   aktif per event). Rekomendasi: buat **9 `DisasterEvent` terpisah** (satu
   per lokasi/desa), dikelompokkan lewat penamaan konsisten
   (`"Gempa NTT 2026 - <Nama Lokasi>"`) dan `province`/`district` yang sama,
   ATAU konfirmasi ke user apakah mereka mau mengubah skema untuk
   mendukung multi-AOI per event (perubahan lebih besar, di luar prompt ini).

### Tugas 1 — Siapkan citra agar bisa jadi `preview_tile_url`
GeoTIFF 150-250MB per lokasi tidak bisa langsung jadi tile URL. Jalur yang
konsisten dengan arsitektur platform ini (100% berbasis Earth Engine
`getMapId()`, lihat `get_tile_url()` di `gee_common.py`):
1. Upload tiap `_ortho.tif` sebagai **GEE Image asset privat** (project
   `optimal-cogency-354208`, lihat `GEE_PROJECT_ID` di `.env`) via
   `earthengine upload image` CLI atau `ee.data.startIngestion` (Python).
   Perlu Cloud Storage bucket sementara sebagai staging (GEE upload butuh
   sumber di GCS, tidak bisa langsung dari file lokal).
2. Setelah asset ready, panggil `get_tile_url()` dengan vis params RGB
   sederhana (band-nya butuh dicek — `_ortho.tif` kemungkinan RGB 3-band atau
   4-band termasuk NIR/pan; cek `gdalinfo`/`ee.Image(...).bandNames()` dulu
   sebelum asumsi urutan band) → hasil `tile_url` inilah yang disimpan ke
   `SatelliteImagery.preview_tile_url`.
3. Alternatif lebih ringan (kalau upload GEE lambat/kuota terbatas): host
   sebagai Cloud-Optimized GeoTIFF (COG) di storage yang sudah ada
   (Supabase bucket, lihat `SUPABASE_GEE_CREDENTIALS_BUCKET` di `.env`) lalu
   serve lewat tile server ringan (titiler) — perubahan arsitektur baru,
   pertimbangkan hanya jika opsi 1 tidak praktis.

### Tugas 2 — Registrasi ke Disaster Intelligence Dashboard (per lokasi)
Untuk tiap lokasi (setelah Tugas 0 & 1 selesai):
1. `POST /api/admin/disasters` — buat event: `name`, `disaster_type="earthquake"`,
   `location_name`, `province=["Nusa Tenggara Timur"]`, `event_date` (dari
   Tugas 0.1), `severity`, `description`.
2. `POST /api/admin/disasters/{id}/aoi` — `geojson` = polygon `geometry` dari
   `_metadata.json` lokasi tsb (sudah dalam format GeoJSON-compatible),
   `source="upload_geojson"`.
3. `POST /api/admin/disasters/{id}/imagery` (phase="post") — `satellite="BlackSky"`,
   `acquisition_date` dari metadata, `sensor="Global-2"`, `resolution_m=1.2`,
   `cloud_coverage_pct` dari metadata, `data_source="BlackSky (komersial)"`,
   `preview_tile_url` dari Tugas 1, `is_primary=true`.
4. `POST /api/admin/disasters/{id}/imagery` (phase="pre") — sesuai keputusan
   Tugas 0.2 (Sentinel-2 via GEE tile, atau PNG yang di-georeference manual
   kalau lokasinya salah satu dari 4 yang punya before/after PNG).

### Tugas 3 (opsional, konsisten dengan kerja sebelumnya) — Tambah BlackSky ke Eksplorasi Scene Satelit
Kalau user mau BlackSky juga bisa dicari/lihat lewat modul "Eksplorasi Scene
Satelit" (bukan cuma disaster dashboard): BlackSky **tidak** ada di GEE public
data catalog (ini citra dari akun komersial user sendiri, bukan koleksi publik
yang bisa di-`ee.ImageCollection(...)` langsung) — jadi tidak bisa ditambah
ke `imagery_provider_registry.py` dengan pola yang sama seperti
Sentinel/Landsat/ASTER (yang semuanya koleksi publik GEE). Perlu jalur
berbeda: baca langsung dari asset privat yang diupload di Tugas 1 (daftar
asset via `ee.data.listAssets` di bawah folder project, bukan
`ImageCollection.filterDate/filterBounds` ke katalog publik).

### Tugas 4 — Jalankan analisis yang benar-benar tersedia
1. Untuk lokasi yang relevan (misal ada indikasi longsor/tutupan lahan
   berubah): `POST /disasters/{id}/analyses` dengan `model_id="forest_change_v1"`,
   lalu `POST /analyses/{run_id}/run`.
2. Kalau ada indikasi genangan/perubahan air: `model_id="flood_change_v1"`
   atau `"water_segmentation_v1"`.
3. Untuk kerusakan bangunan/jalan: **tidak ada model otomatis** (lihat §2).
   Gunakan swipe-compare visual (SatelliteViewer.tsx yang sudah ada di
   disaster module, atau mode "Bandingkan 2 Waktu" di Eksplorasi Scene
   Satelit kalau BlackSky sudah masuk situ juga) untuk inspeksi manual, lalu
   catat temuan lewat `POST /disasters/{id}/hotspots` (hotspot manual, bukan
   hasil model).
4. `POST /analyses/{run_id}/publish` setelah semua hasil per lokasi di-QC
   (`GET /disasters/{id}/qc`).

---

## 4. Yang HARUS diverifikasi live sebelum eksekusi (jangan asumsi)
- Band assignment `_ortho.tif` (RGB urutan mana, apakah butuh stretch
  8-bit/12-bit sebelum divisualisasikan) — cek dengan `gdalinfo` sebelum
  upload ke GEE.
- Apakah GEE asset upload dari file lokal >150MB butuh path lewat GCS
  (kemungkinan besar ya) — cek kuota Cloud Storage yang tersedia untuk
  akun `geosave@optimal-cogency-354208.iam.gserviceaccount.com`.
- Konfirmasi ulang isi `LicenseAgreement-1.1.txt` LENGKAP (baru terbaca
  sebagian saat prompt ini ditulis) sebelum publish ke luar tim internal.

---

*Ditulis 2026-08-22 setelah memeriksa langsung isi folder sumber dan kode
backend terkait (bukan asumsi generik).*

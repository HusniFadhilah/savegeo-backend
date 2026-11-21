# Ruang kesiapan dan integritas proyek SaveGeo

Implementasi ini menambahkan tujuh alur kerja pada dMRV. Akses tetap berdasarkan
keanggotaan proyek. Akun admin tidak otomatis mendapat akses ke proyek anggota lain.

## Membuka fitur

1. Buka `/dmrv/projects`, pilih proyek, lalu **Kesiapan dan integritas proyek**.
2. Alamat langsung: `/dmrv/projects/ID_PROYEK/integrity`.
3. Untuk ringkasan lintas proyek, pilih **Portofolio proyek** atau buka `/dmrv/portfolio`.
4. dMRV tidak ditambahkan ke sidebar.

## Tujuh alur yang tersedia

| Alur | Penggunaan | Sumber hasil |
|---|---|---|
| Kesiapan | Sepuluh butir kesiapan dan enam safeguards, bukti, PIC, tenggat, dan review independen | Dokumen yang diajukan dan diterima reviewer |
| Portofolio | Pencarian, paginasi, luas, skor kesiapan, risiko, tenggat monitoring, temuan review, kesenjangan data, CSV halaman | Proyek yang memberikan keanggotaan eksplisit |
| Risiko | Pemeriksaan satelit manual/terjadwal, laporan lapangan, tindak lanjut dan penutupan dengan alasan | Dynamic World, FIRMS, dan laporan pengguna |
| Mitigasi | Lima pool, baseline dan proyek, emisi, leakage, pengurangan ketidakpastian, buffer, dan riwayat perhitungan | Profil dan input yang didokumentasikan pengguna |
| Registry | Pencatatan sumber, vintage, rentang seri, status menurut sumber; konflik seri dan tumpang tindih batas | Catatan internal dan geometri tersimpan |
| Validasi | Impor CSV, metrik peta awal, kalibrasi linear dengan holdout spasial dan buffer, bootstrap blok | Pasangan observasi dan prediksi yang diunggah |
| Safeguards | FPIC, manfaat, pengaduan, biodiversitas, dampak sosial, situs budaya, register pengaduan dan penyelesaian | Bukti sosial/lingkungan dan keputusan reviewer |

## Alur kerja dan kewenangan

**Pemilik proyek** mengisi identitas dan versi metodologi di Pengaturan, lalu
menambahkan akun analis atau reviewer yang sudah terdaftar. Pengguna dengan peran
`analyst` dapat mengisi penilaian dan monitoring. `reviewer` dan `verifier` dapat
menelaah penilaian yang diajukan, tetapi tidak mengubah input penulis.
`field_operator` tetap menggunakan tab data lapangan yang sudah ada; peran ini
tidak membuka dokumen finansial, sosial, atau seluruh ruang integritas.

Dokumen berawal sebagai **draft**, lalu **diajukan**, dan dapat **diterima** atau
**diminta perbaikan** oleh akun reviewer berbeda. Penulis awal maupun pengubah
terakhir tidak dapat menelaah dokumennya sendiri. Perubahan input mengembalikan
status ke draft dan menghapus keputusan pada versi aktif; versi terdahulu tetap
tersimpan dengan SHA-256 dan audit.

Skor = jumlah butir diterima / jumlah butir yang berlaku × 100. Pengajuan belum
menambah skor. Butir wajib tidak dapat dikecualikan. Pengecualian situs budaya
memerlukan alasan reviewer. Skor ini mengukur kesiapan bukti internal. Ia tidak
menetapkan kelayakan metodologi, additionality, sertifikasi, atau penerbitan kredit.

Di Pengaturan, pemilik dapat memberikan atau mencabut akses proyek. Pemilik tidak
dapat mencabut aksesnya sendiri. Untuk berbagi portofolio dengan tim IDCTA,
tambahkan akun tim tersebut sebagai reviewer pada masing-masing proyek yang
  disetujui untuk dibagikan. Untuk proyek yang terhubung ke workspace organisasi,
  akun harus menjadi anggota organisasi yang aktif sekaligus anggota proyek.
  Pencabutan keanggotaan organisasi juga menutup akses portofolio dan bukti proyek.
  Nama organisasi pada proyek lama hanya label dan tidak memberikan akses otomatis.

## Dokumen dan paket telaah

Unggah bukti di Dokumen, lalu salin `document:ID` ke kolom rujukan bukti, satu
rujukan per baris. Tautan HTTPS tanpa kredensial juga dapat dicatat. Dokumen dari
proyek lain ditolak. Format: PDF, DOCX, XLSX, UTF-8 TXT/CSV, PNG, JPEG. Batas tiap
file 5 MB; kuota proyek 50 MB. Simpan data pribadi pelapor secara terpisah.

**Ekspor paket telaah** menghasilkan ZIP berisi `workspace.json`, riwayat revisi,
salinan dokumen, dan `manifest.json` dengan SHA-256 tiap berkas. Unduhan memerlukan
keanggotaan dan izin yang sama dengan tampilan. Dokumen atau riwayat dengan hash
tidak cocok ditolak. Paket ini merupakan bahan telaah internal, bukan sertifikat.
Paket ini terpisah dari alur submission/evidence dMRV terdahulu; tidak otomatis
mengubah submission menjadi approved atau menerbitkan kredit.

Lokasi dokumen di server diatur dengan `DMRV_INTEGRITY_DIR`; default
`backend/var/dmrv/integrity`. Sertakan folder ini bersama database dalam backup.
Mengganti lokasi penyimpanan memerlukan pemindahan berkas yang sudah ada.

## Monitoring risiko

Simpan batas terlebih dahulu. Di Risiko, atur interval (1–90 hari), panjang setiap
jendela pengamatan (7–90 hari), ambang area, dan jumlah observasi minimum. Jadwal
bersifat opt-in. Tombol **Periksa sekarang** memasukkan pekerjaan ke antrean.
Worker yang sudah ada menjalankan jenis pekerjaan baru `integrity_risk_scan` dan
memeriksa jadwal aktif paling sering sekali per menit.

```powershell
python -m app.workers.dmrv_worker
```

Scan mengunci versi batas, checksum, tanggal, dan ambang saat masuk antrean.
Kegagalan provider menjadi kegagalan/retry pekerjaan; tidak diubah menjadi angka
risiko nol. Worker harus memiliki kredensial GEE aktif. Jadwal yang terhambat oleh
ketiadaan batas atau pencabutan peran penulis dicatat di audit.

Dynamic World membandingkan median probabilitas pohon dua jendela sama panjang.
Sinyal muncul bila probabilitas turun dari ≥0,6 menjadi ≤0,4 dengan dukungan jumlah
observasi per piksel. Luas dihitung pada grid screening 30 m, meskipun produk
aslinya 10 m. Bila dukungan observasi mencakup kurang dari 50% batas, hasil juga
menandai kekurangan data. FIRMS memakai raster confidence ≥80 pada skala 1 km;
yang dihitung adalah sel raster termal yang beririsan, bukan jumlah kejadian api
atau luas terbakar. Keduanya memerlukan telaah manusia. Sinyal tidak otomatis
menghasilkan kehilangan stok karbon, debit buffer, atau klaim pelanggaran.

Notifikasi tersedia di dalam aplikasi melalui daftar risiko dan portofolio.
Email, WhatsApp, dan pengiriman ke pihak lain tidak diaktifkan.

## Mitigasi: input dan rumus

Pilih periode monitoring; luas berasal dari versi batas yang dirujuk periode itu.
Metodologi dan versi perhitungan harus sama dengan identitas proyek. Isi sumber
profil dan dasar penerapan aturan/pengurangan. Untuk setiap pool, masukkan stok
awal/akhir proyek dan baseline dalam **Mg C/ha**. Biomassa kering harus dikonversi
ke karbon dengan faktor yang didukung bukti sebelum diisikan. Pool yang
dikecualikan memerlukan penjelasan.

Untuk tiap pool:

```
ΔC proyek = (stok proyek akhir − awal) × luas ha
ΔC baseline = (stok baseline akhir − awal) × luas ha
tambahan tCO₂ = (ΔC proyek − ΔC baseline) × 44/12
neto tCO₂e = Σ tambahan − emisi proyek − leakage
setelah ketidakpastian = neto positif × (1 − pengurangan%/100)
cadangan buffer = setelah ketidakpastian positif × buffer%/100
skenario unit = max(0, setelah ketidakpastian − cadangan buffer)
```

Input emisi, leakage, atau pengurangan yang kosong tetap **belum diketahui**;
tidak diasumsikan nol. Nilai neto negatif disimpan dan ditampilkan sebagai
pembalikan. Perhitungan menggunakan Decimal presisi 34. Urutan pengurangan adalah
aturan screening `stock_change_screening_v1`. Persentase merupakan input yang
harus dijustifikasi, bukan hasil estimasi statistik otomatis.

Mesin ini belum mengimplementasikan seluruh persyaratan VM0047, REDD+, atau
metodologi kredit lain. Mengisi ID metodologi tidak membuktikan kepatuhan.
`eligibility=not_determined` dan `issuance_status=not_issued` tetap tercatat,
termasuk setelah telaah internal diterima.

## Validasi dan kalibrasi

CSV UTF-8 maksimal 2 MB, 3–2000 pasangan, dengan kolom tepat:

```csv
point_id,lon,lat,observed,predicted
plot_001,110.10,-7.10,42,38
plot_002,110.30,-7.30,56,61
plot_003,110.50,-7.50,28,31
```

Angka di atas hanya contoh format. ID dan koordinat harus unik; agregasikan
pengukuran berulang pada plot yang sama sebelum diimpor. Nyatakan sumber label
(lapangan/peta), satuan, tahun referensi/prediktor, produk, dan provenance termasuk
pemisahan data pelatihan produk hulu. Perbedaan tahun ditandai eksploratif.

Metrik awal: R², RMSE, MAE, bias, dan N. R² negatif dipertahankan; ia berarti
prediksi lebih buruk daripada rata-rata observasi pada sampel tersebut. Bila
observasi konstan, R² ditampilkan tidak terdefinisi.

Kalibrasi: 3-fold GroupKFold menurut blok koordinat (default 0,1°), dengan data
latih yang berada kurang dari buffer (default 10 km) dari titik uji dikeluarkan.
Setiap fold melatih regresi linear observasi terhadap prediksi peta; keluaran
negatif dipotong menjadi nol. Minimal 15 titik, 3 blok, dan 5 titik latih yang
tersisa per fold serta variasi prediktor dibutuhkan. Jika syarat tidak terpenuhi,
metrik awal tetap tersedia tetapi skor OOF kalibrasi tidak diterbitkan sebagian.

Bootstrap memakai 300 resampling blok dan seed tetap. Interval 95% bersifat
kondisional pada prediksi tetap; tidak mencakup seluruh variasi pelatihan ulang.
Sedikit blok atau titik di luar batas proyek ditandai. Validasi terhadap peta
referensi mengukur kesesuaian peta. Holdout kalibrasi tidak membuktikan independensi
model peta asal dan tidak otomatis mengaktifkan model tersebut di produksi.

Data pasangan/OOF lengkap tersimpan dalam rekaman dan ekspor. Ringkasan halaman
mengecualikan array besar melalui proyeksi SQL agar transfer tetap ringan.

## Registry dan safeguards

Catat nama registry yang konsisten, ID eksternal, tautan sumber, vintage, awalan
seri, nomor urut awal/akhir, status menurut sumber, tanggal pemeriksaan, dan bukti.
Awalan menyimpan bagian identitas panjang; nomor urut harus integer ≤2^53−1 agar
tepat di browser. Konflik interval diperiksa dengan pasangan registry/awalan
yang dinormalisasi, lintas catatan dan proyek. Identitas serta detail proyek lain
tidak dikirim dalam hasil konflik. Status rejected dikecualikan dari pencocokan.
Luas tumpang tindih menggunakan gabungan area irisan agar tidak dijumlah ganda.
Tumpang tindih geometri belum membuktikan klaim ganda; periode, hak, dan sumber
registry harus diperiksa oleh reviewer. Tidak ada koneksi langsung atau sinkronisasi
registry eksternal pada versi ini; `external_registry_status=not_connected`.

Safeguards menggunakan alur bukti/review yang sama. Register pengaduan menyimpan
kode kasus, topik, tingkat dampak, penanggung jawab, tenggat, rujukan bukti, dan
alasan tindak lanjut/penutupan. Gunakan kode anonim untuk pelapor. Pembayaran
manfaat dan tanda tangan elektronik tidak dijalankan oleh aplikasi.

## Instalasi dan pemeriksaan

Gunakan dependensi proyek yang sudah ada. Jalankan migrasi sebelum API/worker
baru dijalankan, lalu restart API dan worker:

```powershell
python -m alembic upgrade head
python -m uvicorn app.main:app --host 127.0.0.1 --port 8086
# terminal lain
python -m app.workers.dmrv_worker
# frontend
npm run dev
```

Migrasi `0051_dmrv_integrity_workspace` dan `0052_dmrv_advanced_review_tools` menambah enam tabel dan tidak mengubah
data proyek lama. Tabel baru memakai RLS dan tidak memberi akses langsung kepada
role publik, anon, atau authenticated. Backend menggunakan koneksi database
privileg yang sudah dipakai SaveGeo; keputusan akses tetap di FastAPI melalui
keanggotaan proyek. Jangan memberikan grant publik untuk mengatasi masalah akses.

Pengujian backend utama: `tests/test_dmrv_integrity.py`. Pengujian frontend baru:
`src/features/dmrv/integrity/integrity.test.tsx`. Tambahkan regresi seluruh dMRV,
TypeScript, lint, build, dan pemeriksaan browser sebelum rilis.

## Studi kasus ilustratif

Pengembang menyiapkan restorasi 100 ha. Ia memasukkan batas dan periode, lalu
mengunggah legalitas, bukti hak pengelolaan, baseline, dan persetujuan masyarakat.
Dokumen yang baru diajukan tidak menambah skor. Tim IDCTA diberi peran reviewer
dan menelaah bukti dengan akun terpisah. Portofolio memperlihatkan butir yang
masih kurang, tenggat monitoring, dan risiko terbuka.

Sebagai contoh hitung satu pool, proyek berubah dari 10 menjadi 20 Mg C/ha dan
baseline dari 10 menjadi 12. Tambahan stok = 800 Mg C; konversi = 2933,33 tCO₂.
Jika emisi proyek 5 dan leakage 3 tCO₂e, neto = 2925,33. Dengan pengurangan
ketidakpastian 10% dan buffer 20%, hasil skenario = 2106,24 tCO₂e. Angka ini hanya
ilustrasi rumus. Proyek nyata harus mendukung pemilihan pool dan pengurangannya
dengan metodologi/bukti dan melalui verifikasi yang berlaku.

## Sumber teknis

- Dynamic World: https://developers.google.com/earth-engine/datasets/catalog/GOOGLE_DYNAMICWORLD_V1
- FIRMS: https://developers.google.com/earth-engine/datasets/catalog/FIRMS
- Earth Engine best practices: https://developers.google.com/earth-engine/guides/best_practices
- PostgreSQL/Supabase RLS: https://supabase.com/docs/guides/database/postgres/row-level-security
- Verra VM0047 v1.1: https://verra.org/methodologies/vm0047-afforestation-reforestation-and-revegetation-v1-1/
- Verra VM0047 listed data provider requirements: https://verra.org/methodologies-main/application-for-listed-data-service-providers-under-vm0047/
- Verra ARR resources and reviewer checklist notice: https://verra.org/verra-releases-new-resources-for-arr-projects/
- UNFCCC Article 6.4 registry: https://unfccc.int/process-and-meetings/the-paris-agreement/article-6/article-64-pacm/registry

## Fitur lanjutan: metodologi, citra, kredit, dan lapangan

Fitur-fitur ini tersedia dalam ruang kerja proyek. Checklist metodologi dan
provenance citra berada di tab **Metodologi & data**; pencatatan siklus unit ada
di tab **Siklus kredit**; pengumpulan catatan lapangan berada di tab data lapangan
proyek. dMRV tetap tidak ditambahkan ke sidebar.

### Checklist metodologi berbasis versi

Untuk proyek yang identitasnya diisi tepat sebagai **VM0047 v1.1**, SaveGeo
menampilkan checklist review yang membedakan pendekatan area-based dan
census-based. Butir area-based dan census-based hanya berlaku pada pendekatan
masing-masing. Checklist memuat penerapan pendekatan/eligibilitas, dynamic
performance benchmark atau census, kebutuhan Stocking Index, kualitas dan
cakupan data, seasonality, hubungan SI-AGB, leakage, monitoring, dan kuantifikasi.
Setiap butir dapat diberi status, catatan, dan tautan/dokumen bukti; checklist
yang diajukan tetap melalui review independen.

Paket ini merupakan bantuan kerja ringkas, bukan salinan lengkap metodologi.
VM0047 v1.1 aktif sejak 14 Mei 2025. Ambang resolusi SI ≤30 m berasal dari
persyaratan provider yang diterbitkan Verra; daftar provider atau kecocokan
produknya tidak menjamin proyek atau hasil diterima. Checklist reviewer Verra
yang diterbitkan pada Oktober 2026 juga menyebut dirinya tidak exhaustive.
SaveGeo tidak menyatakan proyek memenuhi syarat atau tersertifikasi.

### Provenance dan QA citra satelit

Di tab **Metodologi & data**, hubungkan catatan ke ID snapshot analisis proyek,
kemudian masukkan sumber/sensor dan versi produk, tanggal observasi, resolusi,
frekuensi, jendela musim, tutupan awan, jumlah observasi, celah, status aerosol,
referensi QA/koreksi, versi pemrosesan, dan bukti sumber. Tidak perlu mengisi
nilai yang tidak tersedia. Status aerosol yang belum diketahui atau tidak
dikoreksi, metadata awan yang kosong, dan resolusi yang tidak diketahui muncul
sebagai catatan untuk reviewer.

Metadata tersebut adalah deklarasi anggota proyek dan disimpan dengan revisi,
hash, serta audit; SaveGeo tidak mengambil ulang atau memverifikasi nilai itu
secara otomatis terhadap provider. Bukti dikaitkan ke snapshot yang benar-benar
ada pada proyek tersebut. Catatan ini membantu interpretasi, tetapi tidak
mengoreksi ulang citra dan tidak memberikan estimasi kesalahan aerosol otomatis.

### Siklus registry dan kredit

Tab **Siklus kredit** mencatat penerbitan, transfer, penggunaan, pensiun, dan
pembatalan yang **dilaporkan** untuk batch/serial tertentu. Untuk setiap event,
masukkan registry, ID transaksi eksternal, tanggal, jumlah unit bulat, pemilik,
dan bukti. API menolak event berulang, batch tanpa penerbitan sebelumnya,
transfer/penggunaan melebihi saldo internal tercatat, dan event bertanggal lebih
awal dari event batch sebelumnya. Riwayat event tidak dapat diedit atau dihapus
melalui aplikasi.

Saldo yang tampil hanyalah saldo yang dihitung dari event yang dimasukkan
anggota SaveGeo. Tidak ada koneksi/sinkronisasi registry eksternal dan tidak
ada penerbitan kredit oleh SaveGeo. Karena itu, saldo internal ini tidak boleh
dipakai sebagai saldo resmi atau klaim kepemilikan tanpa pemeriksaan registry.

### Prioritas pemeriksaan risiko

Bagian atas tab **Risiko** mengurutkan pekerjaan reviewer berdasarkan risiko
terbuka berkeparahan tinggi/sedang, kelengkapan provenance satelit, konfirmasi
aerosol dan cakupan pengamatan, ketersediaan validasi spasial beserta interval
bootstrap metriknya, QA catatan lapangan, serta kelengkapan input ketidakpastian.
Alasan setiap prioritas ditampilkan; metrik/interval akurasi yang diunggah
menjelaskan pasangan validasi tersebut, bukan ketidakpastian stok karbon. Bila
persentase pengurangan ketidakpastian diisi, angka itu hanya ditampilkan sebagai
input yang dinyatakan pengguna. Sistem tidak menggabungkan faktor-faktor tersebut
menjadi probabilitas kehilangan stok atau kredit. Prioritas bukan temuan reversal,
perubahan buffer, atau keputusan kredit.

### Catatan lapangan luring melalui ponsel

Di tab **Lapangan**, pengguna berperan `project_owner`, `analyst`, atau
`field_operator` dapat menulis kode plot, waktu, koordinat/GPS, akurasi, catatan,
dan foto PNG/JPEG. Catatan terlebih dulu masuk antrean IndexedDB pada peramban
dan disinkronkan otomatis saat jaringan tersedia; tombol sinkronisasi juga
tersedia. ID catatan tetap dipakai saat retry sehingga unggahan ulang tidak
membuat duplikat. Catatan tersinkron ditautkan ke plot terdaftar bila kode plot
cocok. Catatan tampil dengan QA flag bila kode plot tidak ditemukan, koordinat
GPS berbeda lebih dari 50 m (atau dua kali akurasi GPS bila lebih besar) dari
titik plot terdaftar, akurasi GPS lebih buruk dari 30 m, posisi di luar batas
proyek, batas belum ada, akurasi tidak direkam, atau waktunya di masa depan.
Ambang jarak hanya pemicu review dan tidak memindahkan koordinat. Foto dibatasi
5 MB dan ikut kuota dokumen proyek.

Agar dapat digunakan di area tanpa jaringan, buka proyek dan tab **Lapangan**
selagi sesi masih online, lalu biarkan halaman itu tetap terbuka saat merekam.
Antrean luring hanya ada pada perangkat/peramban tempat catatan dibuat. Jangan
hapus data situs atau ganti perangkat sebelum jumlah catatan tertunda menjadi
nol. Jika lokasi tidak tersedia, koordinat bisa diisi manual; pemeriksaannya
tetap perlu dilakukan oleh petugas. Hindari mencatat identitas pribadi atau data
sensitif yang tidak diperlukan. QA flag bukan penolakan data otomatis.

Endpoint API fitur lanjutan menggunakan autentikasi sesi SaveGeo dan pemeriksaan
keanggotaan proyek yang sama seperti API dMRV lainnya:

```text
GET  /api/dmrv/projects/{project_id}/integrity/methodology-checklist
GET  /api/dmrv/projects/{project_id}/integrity/risk-priority
GET  /api/dmrv/projects/{project_id}/integrity/credit-events
POST /api/dmrv/projects/{project_id}/integrity/credit-events
GET  /api/dmrv/projects/{project_id}/field-data/captures
POST /api/dmrv/projects/{project_id}/field-data/captures  (multipart: data + optional file)
```

Provenance satelit disimpan lewat catatan integrity kategori `satellite_provenance`
dan mengikuti revisi `If-Match` yang sama seperti catatan proyek lainnya.

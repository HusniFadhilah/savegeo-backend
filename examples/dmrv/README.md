# Contoh proyek dMRV PT Dahana Subang

Contoh ini memakai inventaris pohon dalam **Laporan Survei Lapangan Estimasi Stok Karbon di Area PT Dahana – Subang** (September 2026). File CSV menyimpan 90 pohon karet pada 10 transek berukuran 20 × 20 m. DBH, keliling, AGB dan BGB per pohon disalin dari tabel laporan; nilai AGB dipakai oleh impor lapangan SaveGeo. Kolom BGB dipertahankan dalam file sumber contoh, karena model impor pohon saat ini hanya menyimpan biomassa AGB.

Ringkasan laporan menyebut luas tapak sekitar 9 ha, luas sampel 0,4 ha, serta rata-rata karbon AGB 21,69 Mg C/ha dan BGB 8,44 Mg C/ha (30,13 Mg C/ha gabungan). Tanggal tiap pengukuran tidak dinyatakan, jadi kunjungan memakai presisi tahun 2026. Batas proyek yang dibuat untuk contoh adalah kotak ilustratif 300 × 300 m di sekitar titik transek, bukan batas legal atau poligon final PT Dahana. Pusat transek dihitung dari koordinat UTM 48S yang ada pada laporan.

Kolom `operator` sengaja kosong karena nama petugas per kunjungan tidak dimasukkan ke CSV. Sumber setiap baris dicatat sebagai `source_document`. Laporan menyajikan angka akurasi posisi yang berbeda antara bagian metode dan kesimpulan; angka tersebut tidak dipindahkan ke contoh.

## Angka simulasi

Nilai prediksi untuk validasi AGB/BGB dibuat deterministik dari faktor demo di seed script; laporan lapangan tetap menjadi sisi nilai observasi. Angka awal proyek dan baseline pada ledger, serta stok SOC, litter, dan deadwood, dibuat untuk menampilkan alur aplikasi. Nilai tersebut **bukan pengukuran, keluaran model, validasi independen, estimasi yang disetujui, atau kredit karbon**. Ledger menyisakan emisi, leakage, pengurangan ketidakpastian, dan buffer sebagai input kosong; statusnya tetap draft dan tidak dapat menerbitkan unit.

Perbedaan kecil dapat muncul antara penjumlahan angka pohon yang dibulatkan dan total pada tabel ringkasan laporan. Seed memakai nilai per pohon apa adanya dan memakai tabel ringkasan laporan untuk angka observasi per transek.

## Memasukkan contoh ke database

Jalankan dari direktori `backend` pada database yang memang ingin diisi:

```powershell
.\.venv\Scripts\python.exe .\scripts\seed_dahana_dmrv_demo.py --owner admin --apply
```

Perintah ini idempoten untuk akun pemilik dan membuat satu proyek, batas ilustratif, impor lapangan, periode monitoring 2026, snapshot ringkasan, paket bukti berstatus belum lengkap, draft submission, serta catatan validasi/readiness/risk. Proyek akan terlihat di `/dmrv/projects` saat masuk dengan akun pemilik (default: `admin`). Ia tidak berjalan otomatis saat backend start.

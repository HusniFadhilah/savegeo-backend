# SaveGeo VAPT remediation - 13 Oktober 2026

Status: perubahan source lokal; belum diterapkan atau diuji ulang pada produksi.
Sumber: laporan confidential 011/PTFT/UV-2/X/2026, tanggal 6 Oktober 2026.

## Empat temuan laporan

| Temuan | Implementasi dan bukti |
| --- | --- |
| Session Not Expired After Logout | Logout user/admin menaikkan `session_version` secara atomik dalam database sebelum menghapus cookie. Semua token sebelumnya ditolak untuk cookie, bearer, query tile, dan workflow. |
| User Can Access All ChatBot Session | Kontrol kepemilikan yang sudah ada diverifikasi: daftar difilter dengan `owner_key` dari akun terautentikasi; bukan dari request pengguna. |
| User Can Read Communication ChatBot Session | Detail, rename dan delete menggunakan kepemilikan di backend. Akun lain, termasuk admin dengan ID numerik sama, menerima 404. |
| User Can Send Chat Communication Another ChatBot Session | Agent memeriksa pemilik sebelum membaca history atau memanggil AI. ID asing/tidak ditemukan menerima 404; ID malformed ditolak 400. |

Riwayat lama dengan `owner_key=NULL` tetap disimpan namun tidak dapat dibaca. Jangan menebak kepemilikan dari IP. Recovery memerlukan pemetaan pemilik yang diverifikasi operator.

## Perbaikan tambahan

- JWT wajib memiliki tipe akun, expiry, issued-at, subject valid, session version, password fingerprint dan ID token unik. Token format lama membutuhkan login ulang.
- Fingerprint berupa HMAC atas hash password, sehingga perubahan password otomatis membatalkan access/reset token lama. Reset memakai update bersyarat untuk mencegah replay bersamaan.
- Workflow optional authentication memakai validasi yang sama, tanpa jalur bypass pencabutan sesi.
- Cookie mutation wajib Origin atau Referer tepercaya; login lintas origin ditolak. Secure cookie dan CSRF tidak dapat dinonaktifkan melalui konfigurasi produksi.
- Respons API menggunakan `Cache-Control: no-store`; respons CSRF/rate-limit juga mendapat header keamanan.
- Frontend menunggu revocation backend. Network/server error ditampilkan sebagai kegagalan logout. Respons 401 diperlakukan sebagai sesi yang sudah tidak berlaku.
- Widget chat diganti ketika identitas berubah; request chat dibatalkan saat unmount. Query cache dibersihkan setelah logout/expiry.
- File model harus berada di `MODEL_DIR`, termasuk path terdaftar dan fallback. Upload/import model memerlukan `model.write` dan `secret.write`, karena deserialisasi model merupakan akses eksekusi kode server.
- Paket backend rentan diperbarui: PyJWT, pypdf, httpx2/httpcore2, multidict, oauthlib, urllib3, setuptools. Minimum versi patch disimpan di `pyproject.toml`.

## Verifikasi

Tes lokal menggunakan database SQLite sementara untuk replay token, kepemilikan lintas akun, reset token sekali pakai, migrasi yang mempertahankan akun, CSRF, privilege model, dan path traversal. Tes lain mencakup akses fitur, SSRF URL, keselamatan endpoint analisis, password hashing dan kompatibilitas model karbon.

Hasil akhir: 101 tes backend terpilih lolos; suite tambahan agent/GeoAI lolos 73 tes (sebagian overlap). Seluruh 167 tes frontend pada 52 file lolos, kemudian 6 tes logout dijalankan ulang setelah perubahan API client terakhir dan tetap lolos. Build frontend terakhir dan Ruff seluruh backend berhasil. ESLint seluruh frontend: 0 error, 26 warning existing. Seluruh suite backend, browser produksi, dan image container produksi belum dijalankan.

Audit npm: 0 advisory. Audit Python sebelum update: 40 entri pada 8 paket. Sesudah update: hanya PYSEC-2024-110 pada scikit-learn 1.3.2, muncul dua kali dalam hasil audit. `pip check` berhasil. Bandit severity tinggi: tidak ada temuan. Pemindaian otomatis bukan bukti bahwa seluruh celah telah hilang.

### Pengecualian yang belum dihapus

scikit-learn 1.3.2 dipertahankan karena file pickle lama dilatih menggunakan versi tersebut. PYSEC-2024-110 terkait penyimpanan token training pada `TfidfVectorizer.stop_words_`; penelusuran kode aplikasi tidak menemukan penggunaan TF-IDF. Pengecualian existing pada CI tetap ada. Penghapusan total advisory memerlukan migrasi/retraining model dan pengujian hasil numerik pada scikit-learn versi patch. Jangan memperbarui pin tanpa memvalidasi model.

## Penerapan

1. Backup database dan model. Siapkan source backend/frontend beserta dependensi baru.
2. Jalankan `python -m alembic upgrade head` menggunakan image/runtime baru sebelum melayani request. Compose produksi sudah menjalankan perintah ini pada startup. Migrasi baru: `0032_session_revocation`.
3. Jalankan semua worker dengan source baru; hindari campuran worker lama dan baru. Deploy hasil `npm run build` frontend.
4. Semua akun harus login ulang. Logout sekarang mencabut semua sesi akun tersebut di semua perangkat. Perubahan password juga mencabut sesi lama.
5. Pastikan reverse proxy meneruskan Origin/Referer, tidak men-cache API, mengaktifkan HTTPS dan memberi batas body/rate limit. Rate limiter aplikasi masih per worker; pembatasan terdistribusi harus dilakukan di proxy atau penyimpanan bersama.
6. Jika tabel backend tersedia lewat Supabase Data API, verifikasi grants/RLS secara langsung. Autentikasi custom SaveGeo tidak identik dengan Supabase Auth; jangan membuat policy `auth.uid()` tanpa pemetaan identitas. Akses browser langsung ke tabel akun, percakapan, credential dan config harus ditutup. Konfigurasi/grants database produksi belum diperiksa dalam pekerjaan lokal ini.
7. Retest dengan dua user dan admin: simpan token sebelum logout, replay setelah logout pada seluruh jalur; pastikan ditolak 401. Uji daftar/detail/rename/delete/send sesi akun lain; pastikan tidak ada perubahan atau pemanggilan model. Uji login dan analisis sah tetap berjalan.

Rollback source auth lama akan menghilangkan perlindungan revocation. Jangan menurunkan migrasi atau mengembalikan auth lama sebagai rollback otomatis; gunakan rollout terkoordinasi dan login ulang bila perlu.

## Referensi

- https://supabase.com/docs/guides/api/securing-your-api
- https://pyjwt.readthedocs.io/en/2.15.0/changelog.html

TLS, reverse proxy, runtime container, akses database/Supabase Storage, jaringan internal, dan perilaku aplikasi produksi memerlukan verifikasi deployment serta VAPT ulang. Status empat temuan adalah mitigasi source yang diuji lokal, bukan penutupan resmi oleh pentester.

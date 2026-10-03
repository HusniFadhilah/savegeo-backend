# SAVEGEO release runbook

Versi sumber backend berada di `pyproject.toml`; frontend memiliki sumber versi sendiri di `package.json`. Keduanya saat ini diset ke product version `1.0.0`, tetapi status production tetap `not_verified` sampai deployment workflow yang tepat sukses.

## Build, endpoint, dan workflow

Workflow membuat metadata dengan `scripts/write_build_metadata.py`. Field yang dipakai hanya versi, build number, run attempt, full commit SHA, ref, workflow/run URL, environment, serta waktu UTC. Secret, `.env`, credential, dan token tidak pernah masuk metadata.

Endpoint publik `GET /api/version` mengembalikan metadata nonrahasia. Endpoint membaca `var/deployment-metadata.json` bila tersedia sehingga waktu deployment sukses dapat dicatat setelah health check. Respons tidak mengembalikan settings, database URL, provider key, atau isi environment.

Production dan beta mengunggah deployment record sebagai artifact setelah health check. Karena frontend dan backend adalah repo/workflow terpisah, ambil kedua record lalu gabungkan ke `release/v1.0.0.json`; jangan memperlakukan nomor workflow frontend sebagai nomor workflow backend.

## Pairing, tag, dan rollback

Pasangan harus memiliki `product_version` yang sama dan mencatat component version, build number, run attempt, full SHA, workflow URL, environment, validation status, dan waktu deploy masing-masing. Buat tag `v1.0.0` secara terpisah pada dua commit yang benar-benar dibangun setelah pasangan disetujui; prompt ini tidak membuat atau mendorong tag.

Rollback dilakukan dengan menjalankan ulang workflow terhadap pasangan commit yang diketahui baik, memeriksa `/api/health` dan `/api/version`, lalu membuat deployment record baru yang menunjuk ke pasangan lama. Jangan mengedit `.env` atau secrets untuk memperbaiki manifest.

Validasi lokal:

```powershell
python scripts/write_build_metadata.py --output build-metadata.json
python scripts/validate_build_metadata.py build-metadata.json
python scripts/validate_release_manifest.py release/v1.0.0.json
```

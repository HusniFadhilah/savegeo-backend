# Changelog SAVEGEO Backend

## v1.0.0 — 2026-10-03 — kandidat rilis, belum diverifikasi production

Pasangan sumber kandidat ini berhenti pada commit lokal `48997cf2f324559576f345067d37f5eb77dc78cc`. Perubahan lokal yang belum dikomit tidak termasuk catatan rilis. Belum ada klaim bahwa commit ini sudah berhasil dideploy ke production; verifikasi harus dilakukan melalui workflow production dan manifest pasangan.

### Fitur

- Alur imagery Himawari/USGS, analisis paparan bencana, review, publikasi, dan provenance hasil.
- Endpoint dan layanan geospasial untuk carbon, vegetation, land-cover, crop monitoring, dan disaster analysis.
- Integrasi chatbot/provider AI serta pemeriksaan Ollama pada deployment.
- Metadata runtime `/api/version` untuk menelusuri versi, build, workflow, commit, environment, dan deployment.

### Perbaikan dan keamanan

- Validasi lint, mypy, Bandit, pip-audit, pytest, Gitleaks GHCR, dan Trivy tetap menjadi gate workflow.
- Provenance analisis tidak lagi menggunakan `application_version` hard-coded.
- Image backend membawa OCI version/revision labels tanpa memasukkan konfigurasi atau credential.

### Aturan rilis berikutnya

- PATCH untuk perbaikan kompatibel ke belakang.
- MINOR untuk endpoint/kemampuan baru yang kompatibel.
- MAJOR untuk perubahan kontrak API, schema, atau migrasi yang tidak kompatibel.
- Nomor build workflow tidak menaikkan SemVer; build dan run attempt dicatat sebagai metadata.

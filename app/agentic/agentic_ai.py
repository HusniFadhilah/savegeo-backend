"""
agentic_ai.py - Safe agentic workflow planner for GeoMoka satellite analysis.

This module is intentionally deterministic and tool-whitelisted. It does not
execute arbitrary code or call external LLMs by itself. A future LLM layer can
produce the same plan schema, then pass through the validator here.
"""
from __future__ import annotations

import re
import time
from copy import deepcopy
from datetime import datetime
from threading import Lock
from typing import Any, Dict, List, Optional


# ── API key pool — per-provider multi-key with 429 rate-limit tracking ────────
class _KeyPool:
    """Thread-safe pool of API keys per provider.

    Keys are tried round-robin; on HTTP 429 a key is marked unavailable for
    `retry_after` seconds before being retried again. The pool state is
    in-memory and resets on process restart.
    """

    _lock:  Lock        = Lock()
    _rl:    Dict[str, float] = {}   # "provider:prefix" → unavailable_until (epoch)

    @classmethod
    def is_available(cls, provider: str, key: str) -> bool:
        with cls._lock:
            return cls._rl.get(f"{provider}:{key[:12]}", 0) < time.time()

    @classmethod
    def mark_limited(cls, provider: str, key: str, retry_after: int = 60) -> None:
        with cls._lock:
            cls._rl[f"{provider}:{key[:12]}"] = time.time() + retry_after

    @classmethod
    def available_in(cls, provider: str, key: str) -> int:
        with cls._lock:
            return max(0, int(cls._rl.get(f"{provider}:{key[:12]}", 0) - time.time()))

    @classmethod
    def status(cls, provider: str, keys: List[str]) -> List[dict]:
        now = time.time()
        out = []
        for k in keys:
            until = cls._rl.get(f"{provider}:{k[:12]}", 0)
            out.append({
                "prefix":       k[:6] + "…" + k[-4:] if len(k) > 10 else "***",
                "available":    until < now,
                "available_in": max(0, int(until - now)),
            })
        return out


ALLOWED_TOOLS: Dict[str, Dict[str, str]] = {
    "list_capabilities": {
        "method": "GET",
        "path": "/api/agent/capabilities",
        "description": "Read available datasets, models, and guardrails.",
    },
    "analyze_carbon": {
        "method": "POST",
        "path": "/api/analyze/carbon",
        "description": "Run carbon stock estimation for an AOI.",
    },
    "analyze_vegetation": {
        "method": "POST",
        "path": "/api/analyze/vegetation",
        "description": "Run Sentinel-2 vegetation index analysis (NDVI/EVI/SAVI/MSAVI/NDMI/NDWI/NBR/BSI/NDRE/GCI/ARVI/VARI/SIPI/LAI proxy), with classification, histogram, narrative, and optional multi-period time series.",
    },
    "analyze_vegetation_compare": {
        "method": "POST",
        "path": "/api/analyze/vegetation/compare",
        "description": "Compare two vegetation indices for the same AOI/period (e.g. NDVI vs NDMI) and return quadrant area breakdown + insight.",
    },
    "analyze_landcover": {
        "method": "POST",
        "path": "/api/analyze/landcover",
        "description": "Run land cover summary for one or more datasets.",
    },
    "analyze_landcover_transition": {
        "method": "POST",
        "path": "/api/analyze/landcover-transition",
        "description": "Run land cover transition analysis between years.",
    },
}

AGENT_SYSTEM_PROMPT = """
Anda adalah SaveGeo Agentic AI, asisten analisis geospasial berbasis chatbot yang terintegrasi
dengan platform SaveGeo dan backend GeoMoka.

Peran Anda:
- Jadilah pemandu utama pengguna SaveGeo, baik pengguna awam maupun teknis.
- Bantu pengguna dari awal sampai akhir: memahami tujuan, meminta input yang kurang, membaca konteks peta,
  memilih workflow, memilih dataset/model, menjalankan tool backend, memeriksa hasil, lalu membuat laporan.
- Gunakan bahasa Indonesia yang jelas, praktis, dan tidak mengintimidasi.
- Jika memakai istilah teknis, beri penjelasan pendek. Contoh: "AOI adalah batas area yang ingin dianalisis."
- Jangan hanya memberi instruksi manual; bila input sudah cukup dan tool tersedia, susun workflow yang bisa dieksekusi.

Tujuan utama:
- Mengubah percakapan pengguna menjadi workflow analisis spasial SaveGeo yang aman, jelas, dan dapat diaudit.
- Mengendalikan workflow end-to-end melalui endpoint/tool GeoMoka yang sudah di-whitelist.
- Membuat analisis lengkap, bukan hanya menjalankan endpoint: sertakan konteks, statistik, layer peta,
  insight, validasi kualitas, keterbatasan, dan rekomendasi tindak lanjut.
- Menghasilkan output yang siap ditampilkan di UI SaveGeo: progress workflow, pertanyaan lanjutan,
  layer peta, tabel statistik, narasi laporan, dan action berikutnya.

Konteks integrasi SaveGeo:
- SaveGeo adalah aplikasi peta interaktif untuk analisis citra satelit, AOI, tutupan lahan, vegetasi,
  karbon, perubahan multi-tahun, ekspor layer, dan laporan.
- Backend GeoMoka menyediakan endpoint analisis, dataset discovery, model registry, Earth Engine,
  ArcGIS Living Atlas/ImageServer, dan model karbon scikit-learn/joblib.
- Anda tidak mengakses file lokal, database, GEE, atau ArcGIS secara langsung. Semua aksi harus lewat
  tool/backend endpoint yang tersedia dalam capabilities runtime.
- Anggap UI SaveGeo dapat menyediakan AOI dari gambar polygon di peta, GeoJSON upload, tahun analisis,
  preferensi dataset, dan tombol eksekusi workflow.

Kemampuan analisis:
- Analisis lengkap area: jalankan kombinasi tutupan lahan, vegetasi, karbon, dan perubahan multi-tahun
  jika inputnya tersedia.
- Tutupan lahan: ringkasan kelas lahan, luas per kelas, layer peta, dan interpretasi pola area.
- Perubahan tutupan lahan: perbandingan antar tahun, matriks transisi, kelas yang bertambah/berkurang,
  dan perubahan yang perlu perhatian.
- Vegetasi: NDVI/EVI/SAVI/MSAVI/NDMI/NDWI/NBR/BSI/NDRE/GCI/ARVI/VARI/SIPI/LAI proxy sesuai dukungan sistem,
  kondisi kehijauan, indikasi stres vegetasi, area terbuka, area basah, atau indikasi kebakaran bila indeks
  tersedia. Setiap indeks punya klasifikasi area otomatis, histogram, dan narasi interpretasi. Bisa juga
  membandingkan dua indeks sekaligus (mis. NDVI vs NDMI) untuk melihat apakah vegetasi rapat tapi stres air.
- Karbon: estimasi stok/densitas karbon memakai dataset referensi dan model yang compatible.
- Rekomendasi dataset/model: pilih dari capabilities runtime, jelaskan alasan, resolusi, tahun,
  target pool, dan keterbatasannya.
- Troubleshooting: jelaskan error endpoint, R2 negatif, mismatch dataset/model, target pool,
  AOI terlalu kecil/besar, kredensial GEE/ArcGIS, dan saran perbaikan.
- Laporan: susun ringkasan awam, metodologi, parameter, hasil statistik, layer peta, insight,
  keterbatasan, dan rekomendasi.

Cara memandu pengguna awam:
- Jika user berkata umum seperti "analisis area ini", "cek area ini", "buat laporan", atau "analisis lengkap",
  siapkan workflow lengkap: tutupan lahan, vegetasi, karbon, dan perubahan multi-tahun jika ada tahun pembanding.
- Jika input wajib belum tersedia, jangan membuat asumsi berisiko. Ajukan pertanyaan singkat:
  1. Area mana yang ingin dianalisis? Minta user menggambar AOI atau mengunggah GeoJSON.
  2. Tahun/periode apa yang ingin dipakai?
  3. Apakah ingin analisis lengkap atau fokus tertentu?
- Jika user tidak tahu dataset/model, pilihkan otomatis dari capabilities runtime dan jelaskan alasannya.
- Jika user menyebut lokasi tetapi belum memberi polygon, minta user menggambar AOI di peta SaveGeo
  atau unggah GeoJSON. Jangan mengarang batas administrasi.
- Beri status workflow dalam bahasa sederhana:
  "mengumpulkan input", "memilih dataset", "menyiapkan workflow", "siap dijalankan",
  "menjalankan analisis", "memeriksa hasil", atau "membuat laporan".
- Selalu tutup respons dengan next action yang jelas.

Aturan pemilihan dataset/model:
- Jangan membatasi pilihan hanya pada dataset yang disebut di contoh prompt.
- Selalu gunakan capabilities runtime: capabilities.datasets, capabilities.models, capabilities.runtime,
  capabilities.tools, dan guardrails.
- Untuk karbon, gunakan hanya dataset yang memiliki compatible_models aktif, kecuali mode advisor/troubleshooting.
- Cocokkan intent user dengan metadata dataset: key, name, full_name, provider_type, target_pool, unit,
  resolution, year/year_range, description, limitations, supports_transition, requires_auth, dan model metrics.
- Jika user menyebut dataset legacy/tidak tersedia, cari pengganti operasional paling mirip dari daftar aktif.
  Jelaskan bahwa dataset asli tidak dipakai dan sebutkan alasan fallback.
- Bedakan tahun citra untuk inference, tahun dataset referensi karbon, dan tahun dataset land cover.
- Jelaskan target pool karbon: AGB, AGB+BGB, SOC, atau total ecosystem carbon.
- Jangan membandingkan AGB, AGB+BGB, SOC, dan total ecosystem carbon secara langsung tanpa warning.
- Untuk land cover transition, pilih dataset yang mendukung supports_transition.
- Untuk dataset ArcGIS atau provider eksternal, periksa kebutuhan kredensial, URL publik, dan keterbatasan statistik.

Aturan tool dan keamanan:
- Gunakan hanya tool yang ada dalam whitelist GeoMoka/SaveGeo.
- Jangan menjalankan arbitrary code, shell command, SQL bebas, training model besar, atau endpoint di luar registry.
- Jangan mengarang hasil. Jika tool belum dijalankan, sebutkan bahwa hasil masih berupa rencana.
- Jika tool gagal, jelaskan error dengan bahasa awam, identifikasi penyebab paling mungkin, lalu sarankan perbaikan.
- Jika AOI terlalu besar, dataset tidak tersedia, kredensial ArcGIS/GEE bermasalah, atau model tidak compatible,
  hentikan eksekusi dan minta input/perbaikan yang spesifik.

Strategi workflow:
- Advisor mode: jika input belum lengkap atau user bertanya konsep, berikan penjelasan dan rekomendasi.
- Plan mode: susun JSON plan berisi task, parameter, dataset/model, alasan pemilihan, steps, warnings,
  missing_inputs, dan tool_calls.
- Tool-calling mode: jalankan tool berurutan sesuai plan.
- Critic/validator mode: cek hasil tool, warning, mismatch target pool, tahun, resolusi, cloud threshold,
  status endpoint, dan kelengkapan statistik.
- Report mode: gabungkan hasil menjadi laporan yang mudah dibaca.

Workflow end-to-end default untuk "analisis lengkap":
1. Intake:
   - Pahami tujuan user.
   - Pastikan AOI tersedia.
   - Ambil tahun analisis; jika ada dua tahun, gunakan untuk perubahan.
2. Discovery:
   - Baca capabilities runtime.
   - Cek status Earth Engine dan ArcGIS.
   - Baca dataset carbon, landcover, model carbon, dan whitelist tools.
3. Planning:
   - Pilih workflow yang relevan.
   - Pilih dataset/model berdasarkan metadata dan kompatibilitas.
   - Susun tool_calls berurutan.
4. Execution:
   - Jalankan tool yang sudah di-whitelist.
   - Jangan lanjut jika input wajib hilang atau tool tidak tersedia.
5. Validation:
   - Cek status_code, ok, error, missing result, statistik kosong, warning target pool,
     tahun dataset, cloud threshold, resolusi, dan provider limitation.
6. Reporting:
   - Buat ringkasan awam.
   - Buat tabel/objek statistik.
   - Buat daftar layer peta.
   - Buat insight dan rekomendasi.
   - Sebutkan keterbatasan secara jujur.

Format respons plan:
Kembalikan objek dengan struktur:
{
  "status": "needs_input|ready|advisory|blocked",
  "workflow_stage": "intake|collecting_inputs|planning|ready_for_execution|executing|validating|reporting|blocked",
  "task": "complete_analysis|carbon|vegetation|landcover|landcover_transition|troubleshoot|recommendation",
  "message": "jawaban singkat untuk user",
  "user_guidance": {
    "plain_language": "penjelasan awam tentang apa yang akan dilakukan",
    "questions": ["pertanyaan singkat jika input kurang"],
    "next_action": "aksi berikutnya yang jelas"
  },
  "selected_inputs": {
    "aoi_present": true,
    "aoi_source": "drawn_on_map|uploaded_geojson|provided_payload|missing",
    "years": [],
    "analysis_depth": "quick|standard|complete",
    "datasets": [],
    "models": [],
    "parameters": {}
  },
  "selection_reasoning": [
    "alasan memilih dataset/model/parameter"
  ],
  "plan": [
    "langkah workflow berurutan"
  ],
  "tool_calls": [
    {
      "tool": "nama_tool_whitelist",
      "payload": {}
    }
  ],
  "quality_checks": [
    "hal yang harus divalidasi sebelum/sesudah eksekusi"
  ],
  "warnings": [
    "keterbatasan atau risiko interpretasi"
  ],
  "report_sections": [
    "Ringkasan awam",
    "Metodologi dan parameter",
    "Layer peta",
    "Statistik utama",
    "Insight",
    "Keterbatasan",
    "Rekomendasi"
  ]
}

Format laporan akhir setelah tool selesai:
- Ringkasan awam: 3-5 kalimat tentang kondisi utama area.
- Hasil utama: angka/statistik terpenting dari setiap analisis.
- Layer peta: daftar layer yang bisa ditampilkan.
- Interpretasi: apa arti hasil untuk area tersebut.
- Keterbatasan: resolusi, tahun data, awan, target pool, model, atau provider.
- Rekomendasi: tindakan lanjut yang konkret.

MODE NARASI HASIL (penting):
Jika pesan mengandung frasa seperti "analisis ... telah selesai dijalankan. Tolong ringkas dan jelaskan hasil":
- INI ADALAH permintaan narasi otomatis pasca-eksekusi, BUKAN permintaan menjalankan analisis baru.
- Jangan buat actions baru. Set needs_confirmation: false dan actions: [].
- Buat message yang lengkap dan detail: angka estimasi utama dari page_state (carbon_estimated,
  statistik tutupan lahan, indeks vegetasi), satuan, interpretasi kondisi ekologis area,
  dataset dan model yang dipakai, keterbatasan, dan rekomendasi konkret.
- Gunakan angka dari page_state.analysis_results bila tersedia. Jika page_state tidak memuat angka
  spesifik, jelaskan apa yang sudah tersedia di panel hasil dan minta user membacanya di UI.
- Format: Markdown dengan header singkat (## Ringkasan, ## Hasil Utama, ## Rekomendasi).

Contoh perilaku:
- User: "Saya awam, tolong analisis area ini."
  Respons: jelaskan bahwa Anda akan menjalankan analisis lengkap, cek apakah AOI sudah ada,
  minta tahun jika belum ada, lalu siapkan workflow lengkap.
- User: "Hitung karbon pakai dataset terbaik."
  Respons: pilih dataset karbon operasional dari capabilities runtime yang punya compatible model,
  jelaskan target_pool dan model yang dipakai, lalu siapkan analyze_carbon.
- User: "Bandingkan 2020 dan 2024."
  Respons: gunakan workflow landcover_transition bila AOI ada, pilih dataset yang supports_transition,
  lalu jelaskan bahwa hasil berupa perubahan kelas dan matriks transisi.
- User: "Kenapa R2 negatif?"
  Respons: masuk advisor/troubleshooting mode, jelaskan penyebab umum dan cek mismatch dataset/model,
  target pool, jumlah sampel, fitur, dan strategi validasi.
- User (sistem): "Analisis carbon telah selesai dijalankan. Tolong ringkas dan jelaskan hasil analisis..."
  Respons: narasi lengkap hasil karbon dari page_state, TANPA actions baru.
""".strip()


WORKFLOW_DEFAULTS = {
    "carbon": {
        "year": 2024,
        "dataset_year": 2020,
        "start_month": 1,
        "end_month": 12,
        "cloud_threshold": 20,
    },
    "vegetation": {
        "year": 2024,
        "start_month": 6,
        "end_month": 9,
        "cloud_threshold": 20,
        "indices": ["NDVI"],
    },
    "vegetation_compare": {
        "year": 2024,
        "start_month": 6,
        "end_month": 9,
        "cloud_threshold": 20,
        "index_a": "NDVI",
        "index_b": "NDMI",
    },
    "landcover": {
        "year": 2024,
        "start_month": 1,
        "end_month": 12,
        "datasets": [],
    },
}


def build_agent_capabilities(
    carbon_datasets: List[Dict[str, Any]],
    landcover_datasets: List[Dict[str, Any]],
    models: List[Dict[str, Any]],
    ee_initialized: bool,
    arcgis_enabled: bool,
) -> Dict[str, Any]:
    """Create the compact capability document exposed to clients and the planner."""
    return {
        "agent": {
            "name": "GeoMoka Satellite Analysis Agent",
            "mode": "deterministic_tool_planner",
            "supports_execution": True,
            "requires_aoi_for_execution": True,
            "supports_guided_chat": True,
            "supports_end_to_end_workflows": True,
        },
        "system_prompt": AGENT_SYSTEM_PROMPT,
        "runtime": {
            "earth_engine_initialized": bool(ee_initialized),
            "arcgis_enabled": bool(arcgis_enabled),
        },
        "tools": ALLOWED_TOOLS,
        "datasets": {
            "carbon": carbon_datasets,
            "landcover": landcover_datasets,
        },
        "models": {
            "carbon": models,
        },
        "selection_policy": {
            "dataset_source": "runtime_capabilities",
            "do_not_limit_to_examples": True,
            "carbon_requires_compatible_model": True,
            "prefer_user_requested_dataset_when_operational": True,
            "fallback_requires_same_or_similar_target_pool": True,
        },
        "workflow_modes": {
            "guided_chat": "Ask for missing AOI, years, objective, and preferred analysis depth in plain language.",
            "advisor": "Recommend datasets/models and explain limitations without executing tools.",
            "tool_calling": "Execute one selected GeoMoka analysis endpoint.",
            "complete_analysis": "Run land cover, vegetation, carbon, optional transition, validate outputs, and prepare report sections.",
        },
        "report_contract": {
            "audience_summary": "Bahasa awam: kondisi utama area dan maknanya.",
            "technical_summary": "Dataset, model, parameter, periode, resolusi, dan asumsi.",
            "map_layers": "Layer/tile yang dapat ditampilkan frontend.",
            "statistics": "Luas kelas, indeks vegetasi, estimasi karbon, atau matriks transisi bila tersedia.",
            "insights": "Temuan penting dan anomali yang perlu dicek.",
            "limitations": "Keterbatasan dataset, model, waktu, awan, resolusi, target pool, dan AOI.",
            "next_actions": "Saran tindak lanjut praktis untuk user.",
        },
        "guardrails": [
            "Only whitelisted GeoMoka API tools may be executed.",
            "Carbon analysis must use a reference dataset with at least one compatible active model.",
            "Dataset choices must be selected from the runtime capability document, not from a hardcoded example list.",
            "Different carbon target pools (AGB, AGB+BGB, SOC, total carbon) must not be compared without a warning.",
            "Large model training is not executed by the agent; it requires human confirmation through admin/training workflows.",
            "Datasets without operational source URLs or compatible models are advisory-only and excluded from default analysis choices.",
        ],
    }


def plan_agent_request(payload: Dict[str, Any], capabilities: Dict[str, Any]) -> Dict[str, Any]:
    """Build a validated workflow plan from a natural-language-ish request."""
    message = str(payload.get("message") or payload.get("prompt") or "").strip()
    requested_task = str(payload.get("task") or "").strip().lower()
    task = requested_task or _infer_task(message)

    if task in ("complete", "full_analysis", "end_to_end"):
        return _build_complete_analysis_plan(payload, message, capabilities)
    if task == "troubleshoot":
        return _build_troubleshooting_plan(message, capabilities)
    if task == "carbon":
        return _build_carbon_plan(payload, message, capabilities)
    if task == "landcover_transition":
        return _build_landcover_transition_plan(payload, message, capabilities)
    if task == "landcover":
        return _build_landcover_plan(payload, message, capabilities)
    if task == "vegetation":
        return _build_vegetation_plan(payload, message, capabilities)
    if task == "vegetation_compare":
        return _build_vegetation_compare_plan(payload, message, capabilities)

    return {
        "status": "needs_clarification",
        "task": "unknown",
        "workflow_stage": "intake",
        "message": "Saya bisa bantu analisis area secara lengkap. Kirim batas area/AOI lalu pilih tujuan utama: tutupan lahan, vegetasi, karbon, perubahan antar tahun, atau analisis lengkap.",
        "user_guidance": {
            "plain_language": "Saya belum tahu jenis analisis yang Anda inginkan. Kalau ragu, minta saja 'analisis lengkap area ini'.",
            "questions": [
                "Area mana yang ingin dianalisis? Gambar AOI di peta atau unggah GeoJSON.",
                "Apakah ingin satu tahun saja atau membandingkan dua tahun?",
                "Fokusnya apa: tutupan lahan, kesehatan vegetasi, estimasi karbon, atau semuanya?",
            ],
        },
        "plan": [],
        "warnings": [],
        "tool_calls": [],
    }


def execute_agent_plan(plan: Dict[str, Any], http_client) -> Dict[str, Any]:
    """Execute whitelisted tool calls against our own FastAPI app.

    `http_client` is an `httpx.Client` bound to the app via `httpx.ASGITransport`
    (see app/services/agent_service.py) — an in-process ASGI call, not a real
    network round-trip. This replaces the legacy Flask `app.test_client()`
    self-call trick with its closest FastAPI/httpx equivalent, so `ALLOWED_TOOLS`
    (tool name -> method+path whitelist) keeps working unchanged.
    """
    if plan.get("status") != "ready":
        return {
            "executed": False,
            "reason": plan.get("message") or "Plan is not ready.",
            "results": [],
        }

    results = []
    for call in plan.get("tool_calls", []):
        tool_name = call.get("tool")
        tool = ALLOWED_TOOLS.get(tool_name)
        if not tool:
            results.append({
                "tool": tool_name,
                "status": "blocked",
                "error": "Tool is not whitelisted.",
            })
            continue

        method = tool["method"]
        path = tool["path"]
        body = call.get("payload") or {}

        if method == "GET":
            response = http_client.get(path, params=body)
        elif method == "POST":
            response = http_client.post(path, json=body)
        else:
            results.append({
                "tool": tool_name,
                "status": "blocked",
                "error": f"Unsupported method: {method}",
            })
            continue

        try:
            data = response.json()
        except Exception:
            data = response.text

        results.append({
            "tool": tool_name,
            "path": path,
            "status_code": response.status_code,
            "ok": 200 <= response.status_code < 300,
            "result": data,
        })

    return {
        "executed": True,
        "results": results,
        "report": _build_execution_report(plan, results),
    }


def _infer_task(message: str) -> str:
    text = message.lower()
    if any(token in text for token in (
        "lengkap", "komprehensif", "end-to-end", "end to end", "semua analisis",
        "analisis area", "analisa area", "workflow lengkap", "laporan lengkap",
        "full analysis", "complete analysis",
    )):
        return "complete"
    if any(token in text for token in ("r2", "r²", "rsquare", "akurasi", "compatible", "kompatibel", "error model")):
        return "troubleshoot"
    if any(token in text for token in ("karbon", "carbon", "biomass", "biomassa", "co2", "co₂")):
        return "carbon"
    if any(token in text for token in ("perubahan tutupan", "transisi", "change", "transition")):
        return "landcover_transition"
    if any(token in text for token in ("land cover", "landcover", "tutupan lahan", "lulc")):
        return "landcover"
    veg_tokens = (
        "ndvi", "evi", "vegetasi", "vegetation", "ndmi", "nbr", "savi", "msavi",
        "ndwi", "ndre", "gci", "arvi", "vari", "sipi", "lai", "klorofil", "kekeringan",
        "stres air", "bare soil", "tanah terbuka",
    )
    if any(token in text for token in veg_tokens):
        if any(token in text for token in ("bandingkan", "banding", "vs", "versus", "dibanding", "compare")):
            return "vegetation_compare"
        return "vegetation"
    return "unknown"


def _build_complete_analysis_plan(payload: Dict[str, Any], message: str, capabilities: Dict[str, Any]) -> Dict[str, Any]:
    years = _years_from_message(message)
    primary_year = int(payload.get("year") or (years[-1] if years else WORKFLOW_DEFAULTS["landcover"]["year"]))
    baseline_year = payload.get("start_year") or (years[0] if len(years) > 1 else None)

    steps = [
        "Pahami tujuan user dan validasi AOI sebagai batas area analisis.",
        "Baca capabilities runtime: status GEE/ArcGIS, dataset aktif, model karbon aktif, dan guardrails.",
        "Pilih dataset land cover operasional untuk ringkasan kelas dan layer peta.",
        "Bangun komposit Sentinel-2 dan hitung indeks vegetasi utama.",
        "Pilih dataset karbon yang punya model compatible, lalu jalankan estimasi karbon.",
        "Jika ada dua tahun pembanding, jalankan analisis transisi tutupan lahan.",
        "Lakukan quality check: input lengkap, respons endpoint sukses, warning target pool, tahun dataset, awan, dan resolusi.",
        "Susun laporan akhir: ringkasan awam, statistik, layer peta, insight, rekomendasi, dan keterbatasan.",
    ]

    tool_calls = []
    warnings = []
    landcover_plan = _build_landcover_plan({**payload, "year": primary_year}, message, capabilities)
    vegetation_plan = _build_vegetation_plan({**payload, "year": primary_year}, message, capabilities)
    carbon_plan = _build_carbon_plan({**payload, "year": primary_year}, message, capabilities)
    for child_plan in (landcover_plan, vegetation_plan, carbon_plan):
        tool_calls.extend(child_plan.get("tool_calls", []))
        warnings.extend(child_plan.get("warnings", []))

    if baseline_year and int(baseline_year) != primary_year:
        transition_payload = {
            **payload,
            "start_year": int(baseline_year),
            "end_year": primary_year,
        }
        transition_plan = _build_landcover_transition_plan(transition_payload, message, capabilities)
        tool_calls.extend(transition_plan.get("tool_calls", []))
        warnings.extend(transition_plan.get("warnings", []))

    return _ready_plan(
        task="complete_analysis",
        summary="Workflow lengkap: tutupan lahan, vegetasi, karbon, dan transisi bila tahun pembanding tersedia.",
        steps=steps,
        tool_calls=tool_calls,
        warnings=warnings,
        requires_aoi=True,
        has_aoi=bool(payload.get("aoi")),
        guidance={
            "plain_language": "Saya akan membaca kondisi area dari beberapa sisi: jenis tutupan lahannya, tingkat kehijauan vegetasi, estimasi karbon, dan perubahan antar tahun jika datanya tersedia.",
            "questions": _missing_input_questions(payload, needs_year=False),
            "next_action": "Gambar atau unggah AOI, lalu jalankan workflow. Setelah hasil keluar, chatbot akan menyatukan statistik dan layer menjadi laporan.",
        },
        workflow_stage="ready_for_execution" if payload.get("aoi") else "collecting_inputs",
    )


def _build_carbon_plan(payload: Dict[str, Any], message: str, capabilities: Dict[str, Any]) -> Dict[str, Any]:
    carbon_datasets = capabilities["datasets"]["carbon"]
    dataset = _pick_carbon_dataset(payload, message, carbon_datasets)
    if not dataset:
        return _blocked_plan(
            "carbon",
            "Tidak ada dataset karbon operasional dengan model compatible.",
            ["Cek /api/carbon/datasets atau register model karbon aktif terlebih dahulu."],
        )

    model_name = payload.get("model_name") or _pick_model_for_dataset(dataset, capabilities)
    params = _merged_params("carbon", payload)
    request_payload = {
        "aoi": payload.get("aoi"),
        "year": params["year"],
        "dataset_year": params["dataset_year"],
        "start_month": params["start_month"],
        "end_month": params["end_month"],
        "cloud_threshold": params["cloud_threshold"],
        "reference_dataset": dataset["key"],
        "model_name": model_name,
        "scale": payload.get("scale", payload.get("carbon_scale", 250)),
        "clip_to_aoi": payload.get("clip_to_aoi", True),
    }

    warnings = _carbon_warnings(dataset)
    return _ready_plan(
        task="carbon",
        summary=f"Analisis karbon memakai {dataset['key']} dan model {model_name}.",
        steps=[
            "Validasi AOI dan parameter waktu.",
            "Validasi dataset karbon yang memiliki model compatible.",
            "Jalankan estimasi karbon dan statistik AOI.",
            "Kembalikan tile URL, statistik karbon, dan catatan keterbatasan.",
        ],
        tool_calls=[{"tool": "analyze_carbon", "payload": request_payload}],
        warnings=warnings,
        requires_aoi=True,
        has_aoi=bool(payload.get("aoi")),
        guidance={
            "plain_language": "Saya akan memperkirakan karbon di area Anda memakai dataset referensi yang punya model aktif.",
            "dataset_reason": _dataset_reason(dataset),
            "questions": _missing_input_questions(payload),
            "next_action": "Setelah AOI tersedia, workflow menjalankan estimasi karbon dan mengembalikan statistik serta layer peta.",
        },
    )


def _build_vegetation_plan(payload: Dict[str, Any], message: str, capabilities: Dict[str, Any]) -> Dict[str, Any]:
    params = _merged_params("vegetation", payload)
    indices = payload.get("indices") or _indices_from_message(message) or params["indices"]
    request_payload = {
        "aoi": payload.get("aoi"),
        "year": params["year"],
        "start_month": params["start_month"],
        "end_month": params["end_month"],
        "cloud_threshold": params["cloud_threshold"],
        "indices": indices,
        "scale": payload.get("scale", 20),
    }
    return _ready_plan(
        task="vegetation",
        summary=f"Analisis vegetasi Sentinel-2 untuk indeks {', '.join(indices)}.",
        steps=[
            "Bangun komposit Sentinel-2 bebas awan.",
            "Hitung indeks vegetasi yang diminta.",
            "Ringkas statistik AOI dan tile visualisasi.",
        ],
        tool_calls=[{"tool": "analyze_vegetation", "payload": request_payload}],
        warnings=[],
        requires_aoi=True,
        has_aoi=bool(payload.get("aoi")),
        guidance={
            "plain_language": "Saya akan mengukur kondisi vegetasi dari citra Sentinel-2. NDVI tinggi biasanya menunjukkan vegetasi lebih rapat atau sehat.",
            "questions": _missing_input_questions(payload),
            "next_action": "Setelah AOI tersedia, workflow menghitung indeks vegetasi dan statistik area.",
        },
    )


def _index_pair_from_message(message: str, default_a: str, default_b: str) -> tuple:
    found = _indices_from_message(message)
    if len(found) >= 2:
        return found[0], found[1]
    if len(found) == 1:
        return found[0], (default_b if found[0] != default_b else default_a)
    return default_a, default_b


def _build_vegetation_compare_plan(payload: Dict[str, Any], message: str, capabilities: Dict[str, Any]) -> Dict[str, Any]:
    params = _merged_params("vegetation_compare", payload)
    index_a, index_b = _index_pair_from_message(message, params["index_a"], params["index_b"])
    index_a = payload.get("index_a") or index_a
    index_b = payload.get("index_b") or index_b
    request_payload = {
        "aoi": payload.get("aoi"),
        "year": params["year"],
        "start_month": params["start_month"],
        "end_month": params["end_month"],
        "cloud_threshold": params["cloud_threshold"],
        "index_a": index_a,
        "index_b": index_b,
        "scale": payload.get("scale", 20),
    }
    return _ready_plan(
        task="vegetation_compare",
        summary=f"Bandingkan {index_a} vs {index_b} untuk melihat pola gabungan kedua indeks.",
        steps=[
            "Bangun komposit Sentinel-2 bebas awan.",
            f"Hitung {index_a} dan {index_b} pada AOI yang sama.",
            "Klasifikasi tinggi/rendah tiap indeks dan hitung luas per kombinasi (kuadran).",
            "Susun insight otomatis (mis. vegetasi rapat tapi stres air).",
        ],
        tool_calls=[{"tool": "analyze_vegetation_compare", "payload": request_payload}],
        warnings=[],
        requires_aoi=True,
        has_aoi=bool(payload.get("aoi")),
        guidance={
            "plain_language": f"Saya akan membandingkan {index_a} dan {index_b} untuk melihat area mana yang bagus di kedua sisi, dan area mana yang kelihatan hijau tapi sebenarnya bermasalah.",
            "questions": _missing_input_questions(payload),
            "next_action": "Setelah AOI tersedia, workflow menghitung kedua indeks dan mengembalikan pembagian luas per kuadran plus insight.",
        },
    )


def _build_landcover_plan(payload: Dict[str, Any], message: str, capabilities: Dict[str, Any]) -> Dict[str, Any]:
    params = _merged_params("landcover", payload)
    datasets = payload.get("datasets") or _pick_landcover_datasets(message, capabilities) or params["datasets"]
    request_payload = {
        "aoi": payload.get("aoi"),
        "year": params["year"],
        "start_month": params["start_month"],
        "end_month": params["end_month"],
        "datasets": datasets,
        "scale": payload.get("scale", 10),
    }
    return _ready_plan(
        task="landcover",
        summary=f"Analisis tutupan lahan memakai {', '.join(datasets)}.",
        steps=[
            "Pilih dataset land cover operasional.",
            "Ambil citra/klasifikasi untuk tahun yang diminta.",
            "Hitung luas per kelas dan tile peta.",
        ],
        tool_calls=[{"tool": "analyze_landcover", "payload": request_payload}],
        warnings=[],
        requires_aoi=True,
        has_aoi=bool(payload.get("aoi")),
        guidance={
            "plain_language": "Saya akan memetakan jenis tutupan lahan di area Anda, misalnya hutan, air, lahan terbuka, area terbangun, atau pertanian sesuai legenda dataset.",
            "questions": _missing_input_questions(payload),
            "next_action": "Setelah AOI tersedia, workflow menghitung luas tiap kelas dan menyiapkan layer peta.",
        },
    )


def _build_landcover_transition_plan(payload: Dict[str, Any], message: str, capabilities: Dict[str, Any]) -> Dict[str, Any]:
    years = _years_from_message(message)
    start_year = int(payload.get("start_year") or (years[0] if years else datetime.now().year - 1))
    end_year = int(payload.get("end_year") or (years[-1] if len(years) > 1 else datetime.now().year))
    dataset = payload.get("dataset") or _pick_transition_landcover_dataset(message, capabilities)
    if not dataset:
        return _blocked_plan(
            "landcover_transition",
            "Tidak ada dataset land cover operasional yang mendukung analisis transisi.",
            ["Cek /api/datasets?module=landcover dan pastikan supports_transition=true."],
        )
    request_payload = {
        "aoi": payload.get("aoi"),
        "dataset": dataset,
        "start_year": start_year,
        "end_year": end_year,
        "start_month": int(payload.get("start_month", 1)),
        "end_month": int(payload.get("end_month", 12)),
        "scale": payload.get("scale", 10),
    }
    return _ready_plan(
        task="landcover_transition",
        summary=f"Analisis perubahan tutupan lahan {dataset} dari {start_year} ke {end_year}.",
        steps=[
            "Validasi dataset land cover mendukung transisi.",
            "Bangun peta kelas per tahun.",
            "Hitung matriks transisi dan perubahan area.",
        ],
        tool_calls=[{"tool": "analyze_landcover_transition", "payload": request_payload}],
        warnings=[],
        requires_aoi=True,
        has_aoi=bool(payload.get("aoi")),
        guidance={
            "plain_language": f"Saya akan membandingkan tutupan lahan antara {start_year} dan {end_year}, lalu mencari kelas yang bertambah, berkurang, atau berubah.",
            "questions": _missing_input_questions(payload, needs_year=False),
            "next_action": "Setelah AOI tersedia, workflow menghitung matriks transisi dan perubahan area.",
        },
    )


def _build_troubleshooting_plan(message: str, capabilities: Dict[str, Any]) -> Dict[str, Any]:
    carbon_keys = [d["key"] for d in capabilities["datasets"]["carbon"]]
    example_dataset = carbon_keys[0] if carbon_keys else "dataset operasional dari /api/carbon/datasets"
    return {
        "status": "advisory",
        "task": "troubleshoot",
        "workflow_stage": "advisor",
        "summary": "Analisis diagnostik model/dataset.",
        "user_guidance": {
            "plain_language": "Saya akan bantu membaca penyebab metrik model buruk dan langkah perbaikannya tanpa menjalankan training otomatis.",
            "questions": [
                "Model mana yang ingin dicek?",
                "Dataset referensi dan target pool apa yang dipakai saat training?",
                "Berapa jumlah sampel dan metrik R2/RMSE/MAE terakhir?",
            ],
        },
        "plan": [
            "Baca daftar model dan dataset compatible.",
            "Cari mismatch target_dataset_key, target_pool, dan feature_stack.",
            "Evaluasi metrik R2/RMSE/MAE dan jumlah sampel.",
            "Rekomendasikan retraining atau dataset alternatif.",
        ],
        "available_carbon_reference_datasets": carbon_keys,
        "diagnosis": [
            "R2 negatif berarti model lebih buruk dari baseline rata-rata target.",
            "Penyebab umum: sampel terlalu sedikit, AOI terlalu sempit, tahun S2 tidak cocok dengan label referensi, target pool berbeda, atau model terlalu sederhana.",
            "Untuk dataset tanpa model compatible, gunakan endpoint /api/carbon/datasets sebagai daftar operasional.",
        ],
        "recommendations": [
            "Gunakan minimal ratusan hingga ribuan sampel untuk model produksi.",
            "Gunakan spatial cross-validation dan sampling stratified.",
            "Pisahkan AGB, AGB+BGB, SOC, dan total ecosystem carbon.",
            f"Pilih dataset dari daftar operasional runtime, misalnya {example_dataset}, bukan daftar contoh statis.",
        ],
        "tool_calls": [],
        "warnings": [],
    }


def _ready_plan(
    task: str,
    summary: str,
    steps: List[str],
    tool_calls: List[Dict[str, Any]],
    warnings: List[str],
    requires_aoi: bool,
    has_aoi: bool,
    guidance: Optional[Dict[str, Any]] = None,
    workflow_stage: Optional[str] = None,
) -> Dict[str, Any]:
    missing = []
    if requires_aoi and not has_aoi:
        missing.append("aoi")
    return {
        "status": "ready" if not missing else "needs_input",
        "task": task,
        "workflow_stage": workflow_stage or ("ready_for_execution" if not missing else "collecting_inputs"),
        "summary": summary,
        "plan": steps,
        "tool_calls": tool_calls,
        "warnings": warnings,
        "missing_inputs": missing,
        "user_guidance": guidance or {
            "plain_language": summary,
            "questions": _missing_input_questions({"aoi": has_aoi}),
        },
        "quality_checks": [
            "AOI tersedia dan valid.",
            "Dataset dipilih dari capabilities runtime.",
            "Endpoint yang dipakai ada di whitelist.",
            "Parameter tahun/periode/scale masuk akal.",
            "Warning target pool, provider, dan keterbatasan dataset disertakan.",
            "Respons endpoint sukses sebelum laporan akhir dibuat.",
        ],
        "report_sections": [
            "Ringkasan untuk user awam",
            "Parameter dan dataset yang dipakai",
            "Layer peta yang tersedia",
            "Statistik utama",
            "Insight dan interpretasi",
            "Keterbatasan",
            "Rekomendasi tindak lanjut",
        ],
        "message": "Plan siap dieksekusi." if not missing else "Saya sudah membuat rencana, tetapi masih membutuhkan input tambahan.",
    }


def _blocked_plan(task: str, message: str, warnings: List[str]) -> Dict[str, Any]:
    return {
        "status": "blocked",
        "task": task,
        "workflow_stage": "blocked",
        "summary": message,
        "user_guidance": {
            "plain_language": message,
            "questions": ["Periksa dataset/model aktif, lalu coba lagi."],
        },
        "plan": [],
        "tool_calls": [],
        "warnings": warnings,
    }


def _missing_input_questions(payload: Dict[str, Any], needs_year: bool = True) -> List[str]:
    questions = []
    if not payload.get("aoi"):
        questions.append("Tentukan AOI dulu: gambar batas area di peta atau unggah GeoJSON.")
    if needs_year and not payload.get("year"):
        questions.append("Tahun analisisnya ingin memakai tahun berapa?")
    if not questions:
        questions.append("Input utama sudah cukup. Anda bisa menjalankan workflow atau menyesuaikan dataset/parameter.")
    return questions


def _dataset_reason(dataset: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "key": dataset.get("key"),
        "name": dataset.get("name") or dataset.get("full_name"),
        "provider_type": dataset.get("provider_type"),
        "target_pool": dataset.get("target_pool"),
        "unit": dataset.get("unit"),
        "resolution": dataset.get("resolution"),
        "compatible_models": dataset.get("compatible_models", []),
        "reason": "Dipilih dari capabilities runtime karena tersedia operasional dan memiliki model compatible aktif.",
    }


def _build_execution_report(plan: Dict[str, Any], results: List[Dict[str, Any]]) -> Dict[str, Any]:
    ok_results = [result for result in results if result.get("ok")]
    failed_results = [result for result in results if not result.get("ok")]
    map_layers = []
    statistics = {}

    for result in ok_results:
        tool = result.get("tool")
        data = result.get("result")
        if isinstance(data, dict):
            layer = _extract_map_layer(tool, data)
            if layer:
                map_layers.append(layer)
            statistics[tool] = _extract_statistics(data)

    return {
        "status": "complete" if not failed_results else "partial",
        "audience_summary": (
            "Analisis selesai dan hasil utama sudah siap dibaca."
            if not failed_results else
            "Sebagian analisis berhasil, tetapi ada tool yang gagal dan perlu dicek."
        ),
        "technical_summary": {
            "task": plan.get("task"),
            "executed_tools": [result.get("tool") for result in results],
            "successful_tools": [result.get("tool") for result in ok_results],
            "failed_tools": [
                {
                    "tool": result.get("tool"),
                    "status_code": result.get("status_code"),
                    "error": _extract_error(result.get("result")),
                }
                for result in failed_results
            ],
        },
        "map_layers": map_layers,
        "statistics": statistics,
        "insights": [
            "Gunakan statistik dan layer peta untuk melihat pola utama pada AOI.",
            "Bandingkan hasil antar tool: tutupan lahan menjelaskan kelas area, vegetasi menjelaskan kondisi kehijauan, karbon menjelaskan estimasi stok/pool karbon.",
        ],
        "limitations": plan.get("warnings", []),
        "next_actions": [
            "Tinjau layer peta dan statistik yang paling menonjol.",
            "Jika ada hasil yang janggal, ulangi dengan AOI lebih spesifik atau dataset pembanding.",
            "Untuk laporan final, tambahkan konteks lapangan atau validasi lokal bila tersedia.",
        ],
    }


def _extract_map_layer(tool: str, data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    for key in ("tile_url", "tiles", "map_tile", "map_url"):
        if data.get(key):
            return {"tool": tool, "url": data[key]}
    results = data.get("results")
    if isinstance(results, dict):
        for dataset_key, dataset_result in results.items():
            if isinstance(dataset_result, dict):
                layer = _extract_map_layer(tool, dataset_result)
                if layer:
                    layer["dataset"] = dataset_key
                    return layer
    return None


def _extract_statistics(data: Dict[str, Any]) -> Dict[str, Any]:
    for key in ("statistics", "stats", "summary", "area_stats", "class_areas", "transition_matrix"):
        value = data.get(key)
        if value is not None:
            return {key: value}
    compact = {}
    for key, value in data.items():
        if isinstance(value, (int, float, str, bool)) or value is None:
            compact[key] = value
    return compact


def _extract_error(data: Any) -> Optional[str]:
    if isinstance(data, dict):
        return data.get("error") or data.get("message")
    if isinstance(data, str):
        return data[:300]
    return None


def _merged_params(workflow: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    params = deepcopy(WORKFLOW_DEFAULTS[workflow])
    for key in list(params.keys()):
        if key in payload and payload[key] is not None:
            params[key] = payload[key]
    year_from_message = _years_from_message(str(payload.get("message") or payload.get("prompt") or ""))
    if "year" in params and year_from_message and "year" not in payload:
        params["year"] = year_from_message[-1]
    return params


def _pick_carbon_dataset(payload: Dict[str, Any], message: str, datasets: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not datasets:
        return None
    requested = payload.get("reference_dataset") or payload.get("dataset")
    if requested:
        requested_text = str(requested)
        replacement = _dataset_replacement(requested_text)
        for dataset in datasets:
            if dataset["key"].lower() == requested_text.lower():
                return dataset
        if replacement:
            for dataset in datasets:
                if dataset["key"] == replacement:
                    return dataset
        matched = _best_dataset_match(requested_text, datasets)
        if matched:
            return matched

    matched = _best_dataset_match(message, datasets)
    if matched:
        return matched
    return _rank_carbon_datasets(datasets)[0]


def _dataset_replacement(key: str) -> Optional[str]:
    replacements = {
        "ESA_CCI": "ESA_CCI_SATIO_AGB",
        "GLOBAL_MANGROVE_WATCH_AGB": "HANSEN_TREECOVER_AGB_PROXY",
        "GEDI_L4A_MONTHLY": "GEDI_L4B_STACK",
    }
    return replacements.get(key)


_NON_GEE_PROVIDERS = {"non_gee_stac", "local_raster"}


def _pick_model_for_dataset(dataset: Dict[str, Any], capabilities: Optional[Dict[str, Any]] = None) -> Optional[str]:
    models = dataset.get("compatible_models") or []
    if not models:
        return None

    # _build_carbon_plan only ever calls the GEE tile endpoint (analyze_carbon) — a
    # non-GEE-trained model (STAC/local-raster feature stack, e.g. interaction terms
    # like NDVI_x_elevation) can't be built server-side in GEE and would fail deep
    # inside tile construction. Exclude those from auto-pick; they're only reachable
    # via explicit model_name + /api/analyze/carbon-local, never auto-selected here.
    provider_by_name: Dict[str, Optional[str]] = {}
    if capabilities:
        for m in capabilities.get("models", {}).get("carbon", []):
            meta = m.get("metadata_json") or {}
            provider_by_name[m.get("name")] = meta.get("provider")

    gee_models = [m for m in models if provider_by_name.get(m) not in _NON_GEE_PROVIDERS]
    if not gee_models:
        return None

    # Prefer deterministic, GEE-friendly linear models when present.
    for model in gee_models:
        if any(token in model.lower() for token in ("ridge", "linear", "lasso", "elastic")):
            return model
    return gee_models[0]


def _pick_landcover_datasets(message: str, capabilities: Dict[str, Any]) -> List[str]:
    available = [
        dataset for dataset in capabilities["datasets"]["landcover"]
        if dataset.get("supports_summary", True)
    ]
    picks = _matching_dataset_keys(message, available)
    if not picks:
        picks = [
            dataset["key"]
            for dataset in sorted(available, key=_landcover_dataset_sort_key)[:2]
        ]
    return picks


def _pick_transition_landcover_dataset(message: str, capabilities: Dict[str, Any]) -> Optional[str]:
    available = [
        dataset for dataset in capabilities["datasets"]["landcover"]
        if dataset.get("supports_transition", True)
    ]
    picks = _matching_dataset_keys(message, available)
    if picks:
        return picks[0]
    ranked = sorted(available, key=_landcover_dataset_sort_key)
    return ranked[0]["key"] if ranked else None


def _best_dataset_match(text: str, datasets: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    ranked = [
        (score, dataset)
        for dataset in datasets
        for score in [_dataset_match_score(text, dataset)]
        if score > 0
    ]
    if not ranked:
        return None
    ranked.sort(key=lambda item: (-item[0], _dataset_preference_score(item[1])))
    return ranked[0][1]


def _matching_dataset_keys(text: str, datasets: List[Dict[str, Any]]) -> List[str]:
    ranked = [
        (score, dataset)
        for dataset in datasets
        for score in [_dataset_match_score(text, dataset)]
        if score > 0
    ]
    ranked.sort(key=lambda item: (-item[0], _dataset_preference_score(item[1])))
    return [dataset["key"] for _, dataset in ranked[:3]]


def _dataset_match_score(text: str, dataset: Dict[str, Any]) -> int:
    haystack = " ".join(
        str(dataset.get(key) or "")
        for key in (
            "key",
            "name",
            "full_name",
            "provider",
            "provider_type",
            "source",
            "target_pool",
            "unit",
            "description",
            "attribution",
        )
    ).lower()
    words = _meaningful_words(text)
    score = 0
    for word in words:
        if word in haystack:
            score += 1
    key = str(dataset.get("key") or "").lower()
    if key and key in text.lower():
        score += 10
    return score


def _meaningful_words(text: str) -> List[str]:
    stopwords = {
        "analisis", "analysis", "hitung", "estimasi", "pakai", "gunakan",
        "dataset", "model", "untuk", "yang", "dan", "atau", "ini", "aoi",
        "lahan", "tutupan", "land", "cover", "carbon", "karbon",
    }
    return [
        word
        for word in re.findall(r"[a-zA-Z0-9_]+", text.lower())
        if len(word) > 2 and word not in stopwords
    ]


def _rank_carbon_datasets(datasets: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(datasets, key=_dataset_preference_score)


def _dataset_preference_score(dataset: Dict[str, Any]) -> tuple:
    model_count = int(dataset.get("compatible_model_count") or len(dataset.get("compatible_models") or []))
    resolution = _resolution_number(dataset.get("resolution"))
    provider_type = str(dataset.get("provider_type") or "")
    provider_rank = 0 if provider_type.startswith("gee") else 1 if provider_type.startswith("arcgis") else 2
    return (-model_count, provider_rank, resolution, str(dataset.get("key") or ""))


def _landcover_dataset_sort_key(dataset: Dict[str, Any]) -> tuple:
    resolution = _resolution_number(dataset.get("native_scale") or dataset.get("resolution"))
    year_max = dataset.get("year_max")
    year_rank = -int(year_max) if isinstance(year_max, int) else 0
    auth_rank = 1 if dataset.get("requires_auth") else 0
    return (auth_rank, resolution, year_rank, str(dataset.get("key") or ""))


def _resolution_number(value: Any) -> int:
    if isinstance(value, (int, float)):
        return int(value)
    match = re.search(r"\d+", str(value or ""))
    return int(match.group(0)) if match else 999999


def _indices_from_message(message: str) -> List[str]:
    text = message.upper()
    indices = []
    # MSAVI before SAVI, MNDWI before NDWI: "SAVI"/"NDWI" are substrings of the
    # longer names, so word-boundary regex + longest-first order avoids MSAVI
    # mentions also matching bare SAVI (and MNDWI matching bare NDWI).
    for idx in ("MSAVI", "SAVI", "NDVI", "EVI", "MNDWI", "NDWI", "NDBI", "NDMI", "NBR", "BSI", "NDRE", "GCI", "ARVI", "VARI", "SIPI", "LAI"):
        token = "LAI_PROXY" if idx == "LAI" else idx
        if token in indices:
            continue
        if re.search(rf"\b{idx}\b", text):
            indices.append(token)
    return indices


def _years_from_message(message: str) -> List[int]:
    years = []
    for match in re.findall(r"\b(20\d{2}|19\d{2})\b", message):
        year = int(match)
        if 1980 <= year <= 2100 and year not in years:
            years.append(year)
    return years


def _carbon_warnings(dataset: Dict[str, Any]) -> List[str]:
    warnings = []
    pool = str(dataset.get("target_pool") or "")
    if "soil" in pool:
        warnings.append("Dataset SOC/soil carbon tidak boleh dibandingkan langsung dengan model AGB tanpa normalisasi target pool.")
    if "total" in pool or "ecosystem" in pool:
        warnings.append("Dataset total ecosystem carbon mencampur pool; interpretasi berbeda dari AGB-only.")
    if dataset.get("provider_type", "").startswith("arcgis"):
        warnings.append("Dataset ArcGIS adalah reference raster eksternal; hasil bergantung pada ketersediaan ImageServer.")
    return warnings


# ══════════════════════════════════════════════════════════════════
# AI CONTROLLER — Claude-based action planner
# ══════════════════════════════════════════════════════════════════

CONTROLLER_SYSTEM_PROMPT = """Anda adalah SaveGeo AI Controller — asisten GIS cerdas yang mengendalikan platform SaveGeo melalui bahasa natural.

## Kemampuan Anda
1. Memahami permintaan user (bahasa Indonesia maupun Inggris).
2. Mengubah permintaan menjadi rencana action JSON bertahap yang bisa dieksekusi frontend.
3. Memandu user step-by-step untuk analisis kompleks.
4. Tidak mengarang hasil analisis numerik — selalu jalankan analisis nyata.
5. Meminta klarifikasi hanya jika informasi penting benar-benar tidak tersedia.

## Konteks Aplikasi
SaveGeo adalah platform web GIS berbasis Google Earth Engine untuk:
- **Stok Karbon** — estimasi AGB, SOC, BGB, total ekosistem per hektar menggunakan model ML
- **Tutupan Lahan (LULC)** — klasifikasi multi-kelas dari citra Sentinel-2
- **Perubahan Tutupan Lahan** — deteksi perubahan antar tahun, matriks transisi
- **Vegetasi** — indeks NDVI, EVI, SAVI, NDRE, NDWI dari Sentinel-2
- **Bencana** — analisis risiko spasial
- **Ekspor** — GeoTIFF ke Google Drive, statistik JSON, executive summary PDF

## AOI (Area of Interest)
AOI menentukan wilayah analisis. Ada beberapa cara menetapkan AOI:
- **Administratif**: pilih Provinsi → Kota/Kabupaten → Kecamatan → Desa via dropdown
- **Geocode**: cari nama lokasi (kota, kabupaten, area) → zoom peta → set batas wilayah
- **Upload file**: user upload GeoJSON/KML polygon kustom
- **Gambar manual**: user gambar polygon di peta

Jika user menyebut nama wilayah (misal "Semarang", "Kalimantan Timur", "Desa Maju"), SELALU gunakan fly_to + set_aoi_geocode atau set wilayah administratif, BUKAN hanya menjelaskan cara melakukannya.

## Format Output
Output wajib selalu JSON valid dengan struktur:
{"intent":"nama_intent","confidence":0.0,"needs_confirmation":false,"message":"Pesan untuk user (markdown diizinkan).","warnings":[],"actions":[]}

Gunakan `guide_step` di AWAL actions untuk setiap analisis kompleks (lebih dari 2 langkah), agar user tahu progress.

---

## DAFTAR ACTION YANG DIIZINKAN

### 1. switch_module — pindah modul
{"type":"switch_module","module":"carbon"}
Modul tersedia: carbon, landcover, lc_change, disaster, details, about, guide, admin

### 2. set_value — isi kontrol UI
{"type":"set_value","target":"year","value":2024}
Target tersedia: year, start_month, end_month, cloud_threshold,
carbon_reference_dataset, carbon_dataset_year, carbon_model, carbon_scale,
carbon_clip_mode, enable_carbon_delta, carbon_delta_start_year, carbon_delta_end_year,
carbon_delta_interval, landcover_datasets, landcover_dataset,
transition_dataset, transition_start_year, transition_end_year,
transition_start_month, transition_end_month, vegetation_indices,
province, city, district, village

### 3. run_analysis — jalankan analisis
{"type":"run_analysis","analysis":"carbon"}
Analysis tersedia: carbon, carbon_delta, landcover, landcover_transition, landcover_change_map, vegetation, complete

### 4. show_result_layer — tampilkan layer di peta
{"type":"show_result_layer","layer":"carbon_estimated"}
Layer: carbon_estimated, carbon_reference, landcover, landcover_change, vegetation_index, disaster_risk

### 5. download_report — unduh laporan
{"type":"download_report","report":"executive_summary"}
Report: executive_summary, statistics_json, geotiff

### 6. fly_to — pindahkan + zoom peta ke lokasi
# Jika hanya nama (backend geocode otomatis):
{"type":"fly_to","query":"Kota Semarang"}
# Jika koordinat sudah diketahui:
{"type":"fly_to","lat":-7.0051,"lng":110.4381,"zoom":12,"label":"Kota Semarang"}

### 7. set_aoi_geocode — cari lokasi dan jadikan AOI aktif
{"type":"set_aoi_geocode","query":"Kota Semarang, Jawa Tengah"}
Gunakan ini ketika user menyebut nama wilayah yang ingin dianalisis.
Akan otomatis zoom peta + set batas wilayah sebagai AOI.

### 8. guide_step — tampilkan langkah bernomor di chat
{"type":"guide_step","step":1,"total":5,"title":"Tentukan Wilayah","body":"Kita akan menggunakan **Kota Semarang** sebagai AOI. Sistem akan mencari dan menampilkan batasnya."}
Gunakan SELALU sebagai action pertama untuk workflow analisis kompleks.

### 9. highlight_ui — sorot elemen UI agar user tahu harus klik apa
{"type":"highlight_ui","element_id":"provinceSelect","hint":"Pilih provinsi di sini"}
Element ID umum: provinceSelect, citySelect, districtSelect, villageSelect,
yearSlider, carbonModelSelect, carbonReferenceDataset, runAnalysis,
analysisType, enableCarbonDelta, vegetationIndices

### 10. request_file_aoi — minta user upload AOI kustom
{"type":"request_file_aoi","hint":"Silakan upload file GeoJSON batas wilayah Anda menggunakan tombol 📎 di bawah."}
Gunakan ketika user ingin AOI kustom yang tidak bisa ditemukan lewat geocode.

### 11. open_draw_tool — buka alat gambar polygon AOI
{"type":"open_draw_tool"}
Gunakan ketika user ingin gambar AOI manual di peta.

### 12. ask_user — minta input tambahan
{"type":"ask_user","question":"Pertanyaan untuk user"}

### 13. explain — jelaskan konsep tanpa action
{"type":"explain","topic":"ndvi"}

### 14. offer_choices — tampilkan pilihan sebagai tombol klik
Gunakan ketika user perlu memilih salah satu opsi sebelum lanjut.
{
  "type": "offer_choices",
  "question": "Dataset mana yang ingin Anda gunakan?",
  "choices": [
    {"label": "WCMC Carbon Density", "msg": "Gunakan dataset WCMC"},
    {"label": "GlobBiomass AGB",     "msg": "Gunakan dataset GlobBiomass"}
  ]
}

### 15. download_boundary_geojson — ambil & unduh batas wilayah resmi sebagai GeoJSON
{"type":"download_boundary_geojson","query":"Kota Semarang"}
Gunakan ketika user meminta "carikan shapefile", "cari batas wilayah", "download GeoJSON area ini",
atau "minta file SHP" untuk lokasi tertentu. Sistem akan geocode, set AOI ke peta, dan
menyediakan link download GeoJSON batas administrasi resmi.
Tidak perlu user upload file — data diambil otomatis dari sumber administrasi.
Format: {"type":"download_boundary_geojson","query":"<nama lokasi>"}

---

## WORKFLOW PANDUAN ANALISIS

### Workflow: Analisis Stok Karbon
**WAJIB**: Sebelum atur parameter, cek page_state.available_carbon_datasets.
Jika ada lebih dari 1 dataset, WAJIB gunakan offer_choices agar user pilih dataset dulu.
Jika hanya 1 dataset, langsung pakai tanpa tanya.
1. guide_step(1/4, "Tentukan Wilayah") → set_aoi_geocode atau set_value province/city
2. offer_choices dataset + guide_step(2/4, "Pilih Dataset & Parameter") → set_value carbon_reference_dataset, year, carbon_model (gunakan "Use Default" jika tidak ada model spesifik)
3. guide_step(3/4, "Jalankan Analisis") → switch_module carbon + run_analysis carbon
4. guide_step(4/4, "Lihat Hasil di Peta") → show_result_layer carbon_estimated

### Workflow: Analisis Tutupan Lahan (LULC)
1. guide_step(1/4, "Tentukan Wilayah") → set_aoi_geocode
2. guide_step(2/4, "Atur Parameter") → set_value year, landcover_dataset, cloud_threshold
3. guide_step(3/4, "Jalankan Analisis") → switch_module landcover + run_analysis landcover
4. guide_step(4/4, "Lihat Hasil") → show_result_layer landcover

### Workflow: Analisis Vegetasi (Indeks)
1. guide_step(1/3, "Tentukan Wilayah") → set_aoi_geocode
2. guide_step(2/3, "Pilih Indeks") → set_value vegetation_indices ["NDVI","EVI"]
3. guide_step(3/3, "Jalankan") → switch_module carbon + run_analysis vegetation

### Workflow: Perubahan Tutupan Lahan
1. guide_step(1/4, "Tentukan Wilayah") → set_aoi_geocode
2. guide_step(2/4, "Atur Rentang Waktu") → set_value transition_start_year, transition_end_year
3. guide_step(3/4, "Pilih Dataset") → set_value transition_dataset
4. guide_step(4/4, "Jalankan") → switch_module lc_change + run_analysis landcover_transition

---

## ATURAN PENTING
- Jika user menyebut nama wilayah, LANGSUNG gunakan fly_to + set_aoi_geocode, jangan hanya menjelaskan.
- Untuk analisis multi-langkah, SELALU mulai dengan guide_step agar user tahu progress.
- Jika wilayah sudah ada di page_state (has_aoi=true), skip langkah set AOI.
- Untuk analisis berat (carbon, LULC), set needs_confirmation: true.
- Untuk navigasi peta dan UI saja, set needs_confirmation: false.
- JANGAN klaim hasil numerik sebelum analisis selesai.
- JANGAN bandingkan AGB vs SOC vs BGB langsung tanpa warning perbedaan satuan.
- JANGAN jalankan aksi admin destruktif tanpa konfirmasi.
- JANGAN sertakan download_report atau executive_summary dalam rencana analisis.
- JANGAN hardcode nilai carbon_reference_dataset — selalu cek page_state.available_carbon_datasets dan tanyakan user jika lebih dari 1 opsi.
- Jika tidak bisa membantu, gunakan explain atau ask_user.

Kembalikan hanya JSON valid. Tidak ada teks lain di luar JSON."""


def _get_ai_config() -> dict:
    """Read AI provider config from SystemConfig DB with env-var fallback."""
    import os

    def _env(key: str, fallback: str = "") -> str:
        return os.environ.get(key, fallback).strip()

    try:
        from app.db.models.system_config import SystemConfig
        from app.db.session import SessionLocal

        _db_session = SessionLocal()

        def _db(key: str, fallback: str = "") -> str:
            row = _db_session.query(SystemConfig).filter_by(key=key).first()
            return (row.value or "").strip() if row else fallback

        try:
            return {
                "provider":                  _db("ai.provider",                  _env("AI_PROVIDER", "anthropic")),
                "model":                     _db("ai.model",                     _env("AI_MODEL", "")),
                "anthropic_api_key":         _db("ai.anthropic_api_key",         _env("ANTHROPIC_API_KEY")),
                "openai_api_key":            _db("ai.openai_api_key",            _env("OPENAI_API_KEY")),
                "openrouter_api_key":        _db("ai.openrouter_api_key",        _env("OPENROUTER_API_KEY")),
                "deepseek_api_key":          _db("ai.deepseek_api_key",          _env("DEEPSEEK_API_KEY")),
                "gemini_api_key":            _db("ai.gemini_api_key",            _env("GEMINI_API_KEY")),
                "custom_base_url":           _db("ai.custom_base_url",           _env("AI_CUSTOM_BASE_URL")),
                "ollama_base_url":           _db("ai.ollama_base_url",           _env("OLLAMA_BASE_URL",    "http://localhost:11434/v1")),
                "opencode_base_url":         _db("ai.opencode_base_url",         _env("OPENCODE_BASE_URL",  "http://localhost:4096/v1")),
                "opencode_api_key":          _db("ai.opencode_api_key",          _env("OPENCODE_API_KEY",   "")),
                # Backup key pools (newline-separated)
                "backup_keys_gemini":        _db("ai.backup_keys.gemini",        ""),
                "backup_keys_anthropic":     _db("ai.backup_keys.anthropic",     ""),
                "backup_keys_openai":        _db("ai.backup_keys.openai",        ""),
                "backup_keys_openrouter":    _db("ai.backup_keys.openrouter",    ""),
                "backup_keys_deepseek":      _db("ai.backup_keys.deepseek",      ""),
            }
        finally:
            _db_session.close()
    except Exception:
        # Outside app context (tests, CLI) — env vars only
        return {
            "provider":               _env("AI_PROVIDER", "anthropic"),
            "model":                  _env("AI_MODEL", ""),
            "anthropic_api_key":      _env("ANTHROPIC_API_KEY"),
            "openai_api_key":         _env("OPENAI_API_KEY"),
            "openrouter_api_key":     _env("OPENROUTER_API_KEY"),
            "deepseek_api_key":       _env("DEEPSEEK_API_KEY"),
            "gemini_api_key":         _env("GEMINI_API_KEY"),
            "custom_base_url":        _env("AI_CUSTOM_BASE_URL"),
            "ollama_base_url":        _env("OLLAMA_BASE_URL",   "http://localhost:11434/v1"),
            "opencode_base_url":      _env("OPENCODE_BASE_URL", "http://localhost:4096/v1"),
            "opencode_api_key":       _env("OPENCODE_API_KEY",  ""),
            "backup_keys_gemini":     "",
            "backup_keys_anthropic":  "",
            "backup_keys_openai":     "",
            "backup_keys_openrouter": "",
            "backup_keys_deepseek":   "",
        }


def _get_keys_for_provider(cfg: dict, provider: str) -> List[str]:
    """Return [primary, ...backup] keys for a provider, deduplicated."""
    primary_field = {
        "gemini":     "gemini_api_key",
        "anthropic":  "anthropic_api_key",
        "openai":     "openai_api_key",
        "openrouter": "openrouter_api_key",
        "deepseek":   "deepseek_api_key",
        "opencode":   "opencode_api_key",
    }.get(provider, "")

    primary   = cfg.get(primary_field, "").strip()
    bk_raw    = cfg.get(f"backup_keys_{provider}", "").strip()
    backups   = [k.strip() for k in bk_raw.splitlines() if k.strip()]

    seen: set = set()
    keys: List[str] = []
    for k in [primary] + backups:
        if k and k not in seen:
            seen.add(k)
            keys.append(k)
    return keys


def _is_rate_limit_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return "429" in msg or "quota" in msg or "rate limit" in msg or "rate_limit" in msg


# Default model per provider when ai.model is not configured
_PROVIDER_DEFAULT_MODELS = {
    "anthropic":  "claude-sonnet-4-6",
    "openai":     "gpt-4o",
    "openrouter": "openai/gpt-4o",
    "deepseek":   "deepseek-chat",
    "gemini":     "gemini-2.5-flash-lite",
    "ollama":     "llama3.2",
    "opencode":   "",  # model wajib dikonfigurasi manual
}

# Base URL for OpenAI-compatible providers
_PROVIDER_BASE_URLS: dict = {
    "openrouter": "https://openrouter.ai/api/v1",
    "deepseek":   "https://api.deepseek.com",
    "ollama":     "http://localhost:11434/v1",
    "opencode":   "http://localhost:4096/v1",  # default opencode local port
    "openai":     None,
}


def _extract_attachment_text(att: dict) -> str:
    """
    Convert an attachment dict (from frontend) to a text block for the prompt.
    att keys: name, mime_type, text (for plain text), b64 (for binary).
    """
    if not att:
        return ""
    name  = att.get("name", "untitled")
    mime  = att.get("mime_type", "")
    text  = att.get("text", "")
    b64   = att.get("b64", "")

    # Plain text already extracted client-side
    if text:
        preview = text[:8000]  # safety cap
        if len(text) > 8000:
            preview += f"\n... [terpotong, total {len(text)} karakter]"
        return f"[Lampiran: {name}]\n{preview}"

    # Binary → try server-side extraction
    if not b64:
        return f"[Lampiran: {name} — tidak ada konten yang dapat dibaca]"

    import base64 as _b64
    try:
        data = _b64.b64decode(b64)
    except Exception:
        return f"[Lampiran: {name} — gagal decode base64]"

    # PDF
    if "pdf" in mime or name.lower().endswith(".pdf"):
        try:
            from pypdf import PdfReader
            import io
            reader = PdfReader(io.BytesIO(data))
            pages  = []
            for p in reader.pages[:10]:  # max 10 pages
                t = p.extract_text() or ""
                if t.strip():
                    pages.append(t)
            content = "\n\n".join(pages)[:8000]
            return f"[Lampiran PDF: {name} — {len(reader.pages)} halaman]\n{content}"
        except ImportError:
            return f"[Lampiran PDF: {name} — install pypdf: pip install pypdf]"
        except Exception as exc:
            return f"[Lampiran PDF: {name} — gagal membaca: {exc}]"

    # DOCX
    if "wordprocessingml" in mime or name.lower().endswith(".docx"):
        try:
            from docx import Document
            import io
            doc     = Document(io.BytesIO(data))
            content = "\n".join(p.text for p in doc.paragraphs if p.text.strip())[:8000]
            return f"[Lampiran DOCX: {name}]\n{content}"
        except Exception as exc:
            return f"[Lampiran DOCX: {name} — gagal membaca: {exc}]"

    return f"[Lampiran: {name} ({mime}) — format tidak didukung untuk ekstraksi teks]"


def plan_with_ai(message: str, page_state: dict,
                 image_b64: Optional[str] = None,
                 attachment: Optional[dict] = None,
                 history: Optional[List[dict]] = None) -> dict:
    """Multi-provider AI controller — dispatches to the configured provider.

    image_b64:  base64 PNG screenshot (no data-URL prefix), optional.
    attachment: {name, mime_type, text?, b64?} — file uploaded by user, optional.
    history:    list of {role, content} dicts for multi-turn context, optional.
    """
    import json as _json
    import re

    cfg      = _get_ai_config()
    provider = (cfg["provider"] or "anthropic").lower()
    model    = cfg["model"] or _PROVIDER_DEFAULT_MODELS.get(provider, "")

    payload: dict = {"message": message, "page_state": page_state}
    if image_b64:
        payload["has_screenshot"] = True
        payload["screenshot_note"] = "Screenshot peta SaveGeo disertakan sebagai gambar terpisah."

    # Inject attachment text into prompt
    att_text = _extract_attachment_text(attachment) if attachment else ""
    if att_text:
        payload["attachment"] = att_text

    user_input = _json.dumps(payload, ensure_ascii=False, indent=2)

    try:
        hist = history or []

        def _call_with_key(key: str) -> str:
            """Dispatch to the right backend using the given API key."""
            if provider == "anthropic":
                return _call_anthropic(key, model, user_input, image_b64, hist)
            elif provider in ("openai", "openrouter", "deepseek"):
                base_url = cfg["custom_base_url"] or _PROVIDER_BASE_URLS.get(provider)
                return _call_openai_compat(key, model, user_input, base_url, image_b64, hist)
            elif provider == "gemini":
                return _call_gemini(key, model, user_input, image_b64, hist)
            else:
                raise ValueError(f"Provider '{provider}' tidak mendukung key pool.")

        # Providers that use no key pool (local/special)
        if provider == "ollama":
            base_url = cfg.get("ollama_base_url") or cfg["custom_base_url"] or _PROVIDER_BASE_URLS["ollama"]
            raw = _call_openai_compat("ollama", model, user_input, base_url, image_b64, hist, force_json=False)
        elif provider == "opencode":
            base_url = cfg.get("opencode_base_url") or cfg["custom_base_url"] or _PROVIDER_BASE_URLS["opencode"]
            key      = cfg.get("opencode_api_key") or "opencode"
            if not model:
                return _controller_fallback("OpenCode: nama model wajib diisi di field 'ai.model'.")
            raw = _call_openai_compat(key, model, user_input, base_url, image_b64, hist, force_json=False)
        elif provider not in ("anthropic", "openai", "openrouter", "deepseek", "gemini"):
            return _controller_fallback(
                f"Provider tidak dikenal: '{provider}'. "
                "Pilih salah satu: anthropic, openai, openrouter, deepseek, gemini, ollama, opencode."
            )
        else:
            # Key pool — try primary then backups on 429
            keys     = _get_keys_for_provider(cfg, provider)
            raw      = ""
            last_exc: Optional[Exception] = None
            for key in keys:
                if not _KeyPool.is_available(provider, key):
                    continue
                try:
                    raw = _call_with_key(key)
                    last_exc = None
                    break
                except Exception as exc:
                    if _is_rate_limit_error(exc):
                        _KeyPool.mark_limited(provider, key, retry_after=60)
                        last_exc = exc
                        continue
                    raise   # non-429 error → propagate immediately
            else:
                # Loop completed without break → all keys exhausted
                if last_exc:
                    raise last_exc
                raise RuntimeError(f"Tidak ada API key yang tersedia untuk provider '{provider}'.")

        if not raw:
            return _controller_fallback("Model mengembalikan respons kosong.")

        raw = raw.strip()
        # Strip markdown code fences
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw.rstrip())
        raw = raw.strip()

        if not raw:
            return _controller_fallback("Model mengembalikan respons kosong setelah stripping.")

        # Try direct parse; then regex extraction; then Python-literal fallback
        def _parse(text: str) -> dict:
            # 1. Direct parse
            try:
                return _json.loads(text)
            except _json.JSONDecodeError:
                pass

            # 2. Extract first {...} block (handles leading/trailing garbage)
            m = re.search(r"\{.*\}", text, re.DOTALL)
            candidate = m.group(0) if m else text
            try:
                return _json.loads(candidate)
            except _json.JSONDecodeError:
                pass

            # 3. Sanitize common model quirks: Python booleans, None, unescaped newlines
            sanitized = (
                candidate
                .replace(": True",  ": true")
                .replace(": False", ": false")
                .replace(": None",  ": null")
                .replace(",True",   ",true")
                .replace(",False",  ",false")
                .replace(",None",   ",null")
            )
            # Replace literal newlines/tabs inside JSON strings with escaped versions
            sanitized = re.sub(r'(?<=: ")(.*?)(?=")', lambda mo: mo.group(0).replace('\n', '\\n').replace('\t', '\\t'), sanitized, flags=re.DOTALL)
            try:
                return _json.loads(sanitized)
            except _json.JSONDecodeError:
                pass

            # 4. Python ast.literal_eval (handles single-quoted dicts)
            import ast as _ast
            try:
                obj = _ast.literal_eval(candidate)
                if isinstance(obj, dict):
                    return obj
            except Exception:
                pass

            # All attempts failed — re-raise with original text for logging
            raise _json.JSONDecodeError("All parse attempts failed", text, 0)

        result = _parse(raw)
        result.setdefault("intent", "response")
        result.setdefault("confidence", 0.9)
        result.setdefault("needs_confirmation", False)
        result.setdefault("message", "")
        result.setdefault("warnings", [])
        result.setdefault("actions", [])
        return result

    except _json.JSONDecodeError as exc:
        return _controller_fallback(f"Respons AI bukan JSON valid: {exc}")
    except Exception as exc:
        raise RuntimeError(f"{provider} API error: {exc}") from exc


def _call_anthropic(api_key: str, model: str, user_input: str,
                    image_b64: Optional[str] = None,
                    history: Optional[List[dict]] = None) -> str:
    if not api_key:
        raise ValueError("ai.anthropic_api_key tidak dikonfigurasi.")
    try:
        import anthropic
    except ImportError:
        raise ImportError("Jalankan: pip install anthropic")

    client   = anthropic.Anthropic(api_key=api_key)
    messages = [{"role": h["role"], "content": h["content"]} for h in (history or [])]

    if image_b64:
        current_content: list = [
            {"type": "image", "source": {
                "type": "base64", "media_type": "image/png", "data": image_b64,
            }},
            {"type": "text", "text": user_input},
        ]
    else:
        current_content = [{"type": "text", "text": user_input}]

    messages.append({"role": "user", "content": current_content})

    resp = client.messages.create(
        model=model,
        max_tokens=8192,
        system=CONTROLLER_SYSTEM_PROMPT,
        messages=messages,
    )
    return resp.content[0].text


def _call_openai_compat(api_key: str, model: str, user_input: str,
                        base_url: Optional[str] = None,
                        image_b64: Optional[str] = None,
                        history: Optional[List[dict]] = None,
                        force_json: bool = True) -> str:
    _LOCAL_DUMMY_KEYS = {"ollama", "opencode"}
    if not api_key or (api_key not in _LOCAL_DUMMY_KEYS and not api_key.strip()):
        raise ValueError("API key tidak dikonfigurasi untuk provider ini.")
    try:
        from openai import OpenAI
    except ImportError:
        raise ImportError("Jalankan: pip install openai")

    kwargs: dict = {"api_key": api_key}
    if base_url:
        kwargs["base_url"] = base_url
    client = OpenAI(**kwargs)

    messages: list = [{"role": "system", "content": CONTROLLER_SYSTEM_PROMPT}]
    for h in (history or []):
        messages.append({"role": h["role"], "content": h["content"]})

    if image_b64:
        user_content: list = [
            {"type": "image_url",
             "image_url": {"url": f"data:image/png;base64,{image_b64}", "detail": "auto"}},
            {"type": "text", "text": user_input},
        ]
    else:
        user_content = [{"type": "text", "text": user_input}]

    messages.append({"role": "user", "content": user_content})

    create_kwargs: dict = {
        "model":       model,
        "max_tokens":  8192,
        "temperature": 0.3,
        "messages":    messages,
    }
    if force_json:
        create_kwargs["response_format"] = {"type": "json_object"}

    try:
        resp = client.chat.completions.create(**create_kwargs)
    except Exception as e:
        # Some providers/models don't support response_format — retry without
        if force_json and ("response_format" in str(e) or "json_object" in str(e)
                          or "not supported" in str(e).lower()):
            create_kwargs.pop("response_format", None)
            resp = client.chat.completions.create(**create_kwargs)
        else:
            raise
    choices = getattr(resp, "choices", None)
    if not choices:
        # OpenRouter may include resp.error when model fails
        err = getattr(resp, "error", None)
        if err:
            code = err.get("code", "") if isinstance(err, dict) else ""
            msg  = err.get("message", str(err)) if isinstance(err, dict) else str(err)
            # Treat as rate-limit if code signals it
            if code in (429, "429") or "rate" in str(msg).lower():
                raise RuntimeError(f"429 rate_limit: {msg}")
            raise RuntimeError(f"Model error ({code}): {msg}")
        raise RuntimeError("Model tidak mengembalikan respons (choices kosong atau None).")

    choice  = choices[0]
    content = getattr(getattr(choice, "message", None), "content", None)
    finish  = getattr(choice, "finish_reason", "unknown")

    # content_filter = model was blocked
    if finish == "content_filter":
        raise RuntimeError("Konten diblokir oleh filter model. Coba model lain.")

    # None content with stop = model returned nothing (refusal, null response, etc.)
    # Return "" so plan_with_ai falls back gracefully.
    return content or ""


def _call_gemini(api_key: str, model: str, user_input: str,
                 image_b64: Optional[str] = None,
                 history: Optional[List[dict]] = None) -> str:
    """Uses google-genai (new SDK: from google import genai)."""
    if not api_key:
        raise ValueError("ai.gemini_api_key tidak dikonfigurasi.")
    try:
        from google import genai
        from google.genai import types
    except ImportError:
        raise ImportError("Jalankan: pip install google-genai")

    import base64 as _b64

    client = genai.Client(api_key=api_key)

    # Build contents list: history + current message
    contents: list = []
    for h in (history or []):
        role = "model" if h["role"] == "assistant" else "user"
        contents.append(types.Content(
            role=role,
            parts=[types.Part(text=h["content"])],
        ))

    # Current user message (with optional image)
    current_parts: list = []
    if image_b64:
        current_parts.append(types.Part(
            inline_data=types.Blob(
                mime_type="image/png",
                data=_b64.b64decode(image_b64),
            )
        ))
    current_parts.append(types.Part(text=user_input))
    contents.append(types.Content(role="user", parts=current_parts))

    import time as _time

    last_err = None
    for attempt in range(3):
        try:
            resp = client.models.generate_content(
                model=model,
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=CONTROLLER_SYSTEM_PROMPT,
                    max_output_tokens=8192,
                    temperature=0.3,
                    response_mime_type="application/json",  # force JSON output
                ),
            )
            # resp.text can be None when safety filters block output
            text = resp.text
            if not text:
                # Try extracting from candidates directly
                try:
                    text = resp.candidates[0].content.parts[0].text
                except Exception:
                    pass
            if not text:
                finish = ""
                try:
                    finish = str(resp.candidates[0].finish_reason)
                except Exception:
                    pass
                raise ValueError(
                    f"Gemini mengembalikan respons kosong "
                    f"(finish_reason={finish}). Mungkin diblokir safety filter."
                )
            return text
        except Exception as e:
            last_err = e
            msg = str(e)
            if any(code in msg for code in ("503", "UNAVAILABLE", "529", "overloaded")):
                if attempt < 2:
                    _time.sleep(3 * (attempt + 1))
                    continue
            raise
    raise last_err


def _controller_fallback(reason: str) -> dict:
    return {
        "intent":             "unavailable",
        "confidence":         0.0,
        "needs_confirmation": False,
        "message": (
            "AI Controller tidak dapat digunakan saat ini. "
            "Gunakan tombol analisis di panel kiri secara manual."
        ),
        "warnings": [reason],
        "actions":  [],
    }


# Backward-compat alias
def plan_with_claude(message: str, page_state: dict,
                     image_b64: Optional[str] = None,
                     attachment: Optional[dict] = None,
                     history: Optional[List[dict]] = None) -> dict:
    return plan_with_ai(message, page_state, image_b64, attachment, history)


def get_key_pool_status() -> dict:
    """Return per-provider key pool status for admin dashboard."""
    cfg = _get_ai_config()
    providers = ["gemini", "anthropic", "openai", "openrouter", "deepseek"]
    result: dict = {}
    for p in providers:
        keys = _get_keys_for_provider(cfg, p)
        if keys:
            result[p] = _KeyPool.status(p, keys)
    return result


# ══════════════════════════════════════════════════════════════════
# GEO-AI ASSISTANT — tool-calling grounded query/action agent (P0)
# ══════════════════════════════════════════════════════════════════
#
# Unlike CONTROLLER_SYSTEM_PROMPT/plan_with_ai above (a single-shot JSON-mode
# call that emits UI-automation actions to drive the analysis forms), this is
# a real multi-round tool-calling loop: the model calls whitelisted functions
# from app.agentic.geoai_tools to read already-computed results / run hotspot
# search, then answers grounded in what those tools actually returned. See
# app/agentic/geoai_tools.py for the tool registry and the "no fabricated
# numbers" enforcement.

GEOAI_SYSTEM_PROMPT = """You are SAVEGEO Geo-AI Assistant.

You help users analyze geospatial information already available inside SAVEGEO: AOI, active
layers, analysis period, Sentinel-2 imagery, vegetation indices (NDVI/EVI/SAVI/NDWI/NBR/etc.),
land use/land cover, carbon estimation, and change detection.

## Hard rules (do not break these)
- Never invent measurements, statistics, coordinates, classifications, model results, or
  environmental findings. Every number in your answer must come from a tool result you actually
  received in this conversation (this turn's tool calls, or an earlier turn's tool result still
  visible in the conversation history).
- If a required analysis has not been run, say so explicitly and name the analysis to run - do not
  guess a plausible-sounding number instead. Use wording like: "Data tersebut belum tersedia untuk
  AOI ini. Jalankan {analysis} terlebih dahulu agar saya dapat menghitung {metric}."
- Clearly separate **measured/observed** facts (e.g. an NDVI raster statistic) from
  **interpretation** (a possible explanation for a change). Never present a possible cause as
  confirmed fact unless a tool result actually supports it. Example: "Data menunjukkan NDVI turun
  24%. Penurunan tersebut dapat berkaitan dengan berkurangnya vegetasi, kondisi musiman, gangguan
  lahan, atau faktor lain - untuk memastikan penyebab, diperlukan validasi tambahan."
- Carbon figures are always model **estimates**, never raw measurements - say "Estimasi carbon
  stock ... berdasarkan model ..." and include model_r2/uncertainty when the tool result has it,
  never a bare number stated as fact.
- Only call the tools you were given. Never claim to run SQL, code, or any action outside those
  tools.
- Respond in Indonesian (the app's working language) unless the user writes in English.

## How to work
1. If you're not sure what's already open/available, call get_current_context first.
2. To answer a question about existing results ("berapa luas hutan yang hilang", "apa arti NDVI
   0.72"), call get_analysis_results / query_vegetation_index / query_landcover / query_carbon /
   compare_periods - do not call find_hotspots for this, it's a heavier live computation.
3. Only call find_hotspots when the user actually wants a ranked list of specific *areas*
   (hotspots) - "cari area dengan kehilangan vegetasi terbesar", "hotspot deforestasi terbesar",
   "area mana yang berubah paling besar". It requires an AOI; if get_current_context or the tool
   result shows no AOI, ask the user to set one instead of calling it again.
4. For follow-ups like "yang kedua carbon loss-nya berapa?" or "detail hotspot pertama", match the
   ordinal to the hotspot list already returned earlier in this conversation (by id, e.g. "h2" for
   the 2nd one) - use get_hotspot_detail if you need to re-fetch it, but you usually already have
   it in the visible tool result from earlier in the conversation.
5. Once you have enough grounded information, stop calling tools and produce the final answer.

## Final answer format
When you are done (no more tool calls needed), respond with ONLY one JSON object, no other text,
no markdown code fence:
{
  "message": "<answer text, markdown allowed, follow the structure below>",
  "warnings": ["<data limitation or caveat, if any>"],
  "actions": [ ... ],
  "cards": [ ... ]
}

Keep "message" short and structured, in this order (use markdown headers only when the answer has
multiple parts - a one-line factual answer doesn't need headers):
- **Finding**: the single most important takeaway, 1-2 sentences.
- **Key Metrics**: at most 3-5 numbers, each with its unit and source analysis.
- **Location**: which area/hotspot this refers to, if relevant.
- **Interpretation**: a short, clearly-labeled interpretation, only if it adds value - never
  invent a cause.
- **Actions**: implied by the `actions`/`cards` you return, no need to restate as text.

### actions[] - things the map/UI should do (optional, omit if nothing to do)
- {"type":"zoom_to_location","lat":..,"lng":..,"zoom":12}
- {"type":"zoom_to_feature","geometry":<GeoJSON geometry>}
- {"type":"highlight_polygon","geometry":<GeoJSON geometry>,"label":"..."}
- {"type":"highlight_hotspot","hotspot_id":"h1"}
- {"type":"show_before_after","hotspot_id":"h1"}  (or omit hotspot_id to compare the whole AOI)
- {"type":"open_analysis_result","kind":"carbon|vegetation|landcover|landcover_transition"}
- {"type":"ask_user","question":"..."}  (when you genuinely need more input, e.g. no AOI set)
Only include an action when it's genuinely useful right now (e.g. auto-zoom to the single most
relevant hotspot you just found) - hotspot cards already have their own Zoom/Details buttons, you
don't need to repeat one action per hotspot.

### cards[] - structured result cards (optional, omit if a plain text answer is enough)
- {"type":"metric","title":"...","metrics":[{"label":"...","value":"...","unit":"..."}],"source":{...}}
- {"type":"hotspot","id":"h1","title":"Hotspot 1","area_ha":18.7,"metric_label":"NDVI Change","metric_value":"-32%","period":"2024→2026","geometry":<GeoJSON>,"centroid":[lng,lat],"source":{...}}
  (one card per hotspot you want to show; copy area_ha/geometry/etc. straight from the
  find_hotspots tool result, do not recompute or round differently)
- {"type":"comparison","title":"...","before":{...},"after":{...},"source":{...}}
- {"type":"warning","message":"..."}
- {"type":"suggested_action","label":"...","message":"<what sendMessage should say if clicked>"}
Every card should carry the "source" object from the tool result it came from
({"source": "...", "dataset": "...", "period": "...", "generated_at": "..."}) so the UI can show
provenance.

If nothing is available for what the user asked, still return valid JSON with an explanatory
"message" and empty "actions"/"cards" - never leave the JSON incomplete or add prose outside it.
""".strip()


def _extract_json_object(raw: str) -> dict:
    """Best-effort JSON object extraction from a model's final text turn -
    same tolerance strategy as plan_with_ai's local _parse (markdown fences,
    leading/trailing prose, single-quoted dict fallback), factored out so
    geoai_with_ai doesn't depend on plan_with_ai's closure."""
    import ast as _ast
    import json as _json
    import re as _re

    text = raw.strip()
    text = _re.sub(r"^```(?:json)?\s*", "", text)
    text = _re.sub(r"\s*```$", "", text.rstrip()).strip()

    try:
        return _json.loads(text)
    except _json.JSONDecodeError:
        pass

    m = _re.search(r"\{.*\}", text, _re.DOTALL)
    candidate = m.group(0) if m else text
    try:
        return _json.loads(candidate)
    except _json.JSONDecodeError:
        pass

    try:
        obj = _ast.literal_eval(candidate)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass

    return {
        "message": raw.strip() or "Maaf, saya tidak bisa memproses permintaan ini sekarang.",
        "warnings": [],
        "actions": [],
        "cards": [],
    }


def _tool_round_limit_response() -> str:
    import json as _json

    return _json.dumps({
        "message": "Pertanyaan ini butuh terlalu banyak langkah untuk dijawab dengan aman. Coba pertanyaan yang lebih spesifik, misalnya sebutkan metrik dan periode yang ingin dicek.",
        "warnings": ["tool_round_limit_exceeded"],
        "actions": [],
        "cards": [],
    }, ensure_ascii=False)


def _tool_schema_for_anthropic() -> List[dict]:
    from app.agentic.geoai_tools import GEOAI_TOOLS

    return [
        {"name": t["name"], "description": t["description"], "input_schema": t["parameters"]}
        for t in GEOAI_TOOLS
    ]


def _tool_schema_for_openai() -> List[dict]:
    from app.agentic.geoai_tools import GEOAI_TOOLS

    return [
        {"type": "function", "function": {"name": t["name"], "description": t["description"], "parameters": t["parameters"]}}
        for t in GEOAI_TOOLS
    ]


def _call_anthropic_tools(api_key: str, model: str, user_input: str,
                          history: Optional[List[dict]], tool_ctx: dict,
                          max_rounds: int = 6) -> str:
    import json as _json

    if not api_key:
        raise ValueError("ai.anthropic_api_key tidak dikonfigurasi.")
    try:
        import anthropic
    except ImportError:
        raise ImportError("Jalankan: pip install anthropic")

    from app.agentic.geoai_tools import execute_geoai_tool

    client = anthropic.Anthropic(api_key=api_key)
    messages: list = [{"role": h["role"], "content": h["content"]} for h in (history or [])]
    messages.append({"role": "user", "content": user_input})
    tools = _tool_schema_for_anthropic()

    for _ in range(max_rounds):
        resp = client.messages.create(
            model=model, max_tokens=4096, system=GEOAI_SYSTEM_PROMPT, tools=tools, messages=messages,
        )
        tool_uses = [b for b in resp.content if getattr(b, "type", None) == "tool_use"]
        if not tool_uses:
            text_blocks = [b.text for b in resp.content if getattr(b, "type", None) == "text"]
            return "\n".join(text_blocks)

        messages.append({"role": "assistant", "content": resp.content})
        tool_results = []
        for tu in tool_uses:
            result = execute_geoai_tool(tu.name, tu.input or {}, tool_ctx)
            tool_results.append({
                "type": "tool_result", "tool_use_id": tu.id,
                "content": _json.dumps(result, ensure_ascii=False, default=str),
            })
        messages.append({"role": "user", "content": tool_results})

    return _tool_round_limit_response()


def _call_openai_compat_tools(api_key: str, model: str, user_input: str,
                              base_url: Optional[str], history: Optional[List[dict]],
                              tool_ctx: dict, max_rounds: int = 6) -> str:
    import json as _json

    _LOCAL_DUMMY_KEYS = {"ollama", "opencode"}
    if not api_key or (api_key not in _LOCAL_DUMMY_KEYS and not api_key.strip()):
        raise ValueError("API key tidak dikonfigurasi untuk provider ini.")
    try:
        from openai import OpenAI
    except ImportError:
        raise ImportError("Jalankan: pip install openai")

    from app.agentic.geoai_tools import execute_geoai_tool

    kwargs: dict = {"api_key": api_key}
    if base_url:
        kwargs["base_url"] = base_url
    client = OpenAI(**kwargs)

    messages: list = [{"role": "system", "content": GEOAI_SYSTEM_PROMPT}]
    for h in (history or []):
        messages.append({"role": h["role"], "content": h["content"]})
    messages.append({"role": "user", "content": user_input})
    tools = _tool_schema_for_openai()

    for _ in range(max_rounds):
        resp = client.chat.completions.create(
            model=model, max_tokens=4096, temperature=0.2, messages=messages, tools=tools,
        )
        choice = resp.choices[0]
        msg = choice.message
        tool_calls = getattr(msg, "tool_calls", None)
        if not tool_calls:
            return msg.content or ""

        messages.append({
            "role": "assistant",
            "content": msg.content,
            "tool_calls": [
                {"id": tc.id, "type": "function", "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                for tc in tool_calls
            ],
        })
        for tc in tool_calls:
            try:
                args = _json.loads(tc.function.arguments or "{}")
            except Exception:
                args = {}
            result = execute_geoai_tool(tc.function.name, args, tool_ctx)
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": _json.dumps(result, ensure_ascii=False, default=str)})

    return _tool_round_limit_response()


def _call_gemini_tools(api_key: str, model: str, user_input: str,
                       history: Optional[List[dict]], tool_ctx: dict,
                       max_rounds: int = 6) -> str:
    if not api_key:
        raise ValueError("ai.gemini_api_key tidak dikonfigurasi.")
    try:
        from google import genai
        from google.genai import types
    except ImportError:
        raise ImportError("Jalankan: pip install google-genai")

    from app.agentic.geoai_tools import GEOAI_TOOLS, execute_geoai_tool

    client = genai.Client(api_key=api_key)

    contents: list = []
    for h in (history or []):
        role = "model" if h["role"] == "assistant" else "user"
        contents.append(types.Content(role=role, parts=[types.Part(text=h["content"])]))
    contents.append(types.Content(role="user", parts=[types.Part(text=user_input)]))

    tool_decls = [
        types.FunctionDeclaration(name=t["name"], description=t["description"], parameters=t["parameters"])
        for t in GEOAI_TOOLS
    ]
    gemini_tools = [types.Tool(function_declarations=tool_decls)]

    for _ in range(max_rounds):
        resp = client.models.generate_content(
            model=model, contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=GEOAI_SYSTEM_PROMPT, max_output_tokens=4096, temperature=0.2, tools=gemini_tools,
            ),
        )
        candidate = resp.candidates[0]
        parts = candidate.content.parts or []
        fn_calls = [p.function_call for p in parts if getattr(p, "function_call", None)]
        if not fn_calls:
            return resp.text or ""

        contents.append(candidate.content)
        response_parts = []
        for fc in fn_calls:
            args = dict(fc.args) if fc.args else {}
            result = execute_geoai_tool(fc.name, args, tool_ctx)
            response_parts.append(types.Part(function_response=types.FunctionResponse(name=fc.name, response={"result": result})))
        contents.append(types.Content(role="user", parts=response_parts))

    return _tool_round_limit_response()


def geoai_with_ai(message: str, context: dict, db, history: Optional[List[dict]] = None) -> dict:
    """Tool-calling Geo-AI Assistant loop (P0). See GEOAI_SYSTEM_PROMPT and
    app/agentic/geoai_tools.py for the grounding/whitelist contract.

    `context` is the richer client-held state (AOI geometry, period, active
    layer, and already-computed analysis results) - see
    windowBridge.ts buildGeoAiContext() on the frontend. `db` is passed
    through to tools that need a DB session (vegetation hotspot search reads
    satellite-provider config the same way analyze_vegetation does).
    """
    import json as _json

    cfg = _get_ai_config()
    provider = (cfg["provider"] or "anthropic").lower()
    model = cfg["model"] or _PROVIDER_DEFAULT_MODELS.get(provider, "")

    tool_ctx: dict = {"context": context or {}, "db": db, "last_hotspots": {}}
    user_input = _json.dumps({"message": message, "context": context or {}}, ensure_ascii=False, default=str)

    def _call_with_key(key: str) -> str:
        if provider == "anthropic":
            return _call_anthropic_tools(key, model, user_input, history, tool_ctx)
        elif provider in ("openai", "openrouter", "deepseek"):
            base_url = cfg["custom_base_url"] or _PROVIDER_BASE_URLS.get(provider)
            return _call_openai_compat_tools(key, model, user_input, base_url, history, tool_ctx)
        elif provider == "gemini":
            return _call_gemini_tools(key, model, user_input, history, tool_ctx)
        else:
            raise ValueError(f"Provider '{provider}' tidak mendukung Geo-AI tool-calling.")

    try:
        if provider == "ollama":
            base_url = cfg.get("ollama_base_url") or cfg["custom_base_url"] or _PROVIDER_BASE_URLS["ollama"]
            raw = _call_openai_compat_tools("ollama", model, user_input, base_url, history, tool_ctx)
        elif provider == "opencode":
            base_url = cfg.get("opencode_base_url") or cfg["custom_base_url"] or _PROVIDER_BASE_URLS["opencode"]
            key = cfg.get("opencode_api_key") or "opencode"
            if not model:
                return _geoai_fallback("OpenCode: nama model wajib diisi di field 'ai.model'.")
            raw = _call_openai_compat_tools(key, model, user_input, base_url, history, tool_ctx)
        elif provider not in ("anthropic", "openai", "openrouter", "deepseek", "gemini"):
            return _geoai_fallback(f"Provider tidak dikenal: '{provider}'.")
        else:
            keys = _get_keys_for_provider(cfg, provider)
            raw = ""
            last_exc: Optional[Exception] = None
            for key in keys:
                if not _KeyPool.is_available(provider, key):
                    continue
                try:
                    raw = _call_with_key(key)
                    last_exc = None
                    break
                except Exception as exc:
                    if _is_rate_limit_error(exc):
                        _KeyPool.mark_limited(provider, key, retry_after=60)
                        last_exc = exc
                        continue
                    raise
            else:
                if last_exc:
                    raise last_exc
                raise RuntimeError(f"Tidak ada API key yang tersedia untuk provider '{provider}'.")

        if not raw:
            return _geoai_fallback("Model mengembalikan respons kosong.")

        result = _extract_json_object(raw)
        result.setdefault("message", "")
        result.setdefault("warnings", [])
        result.setdefault("actions", [])
        result.setdefault("cards", [])
        return result
    except Exception as exc:
        raise RuntimeError(f"{provider} API error: {exc}") from exc


def _geoai_fallback(reason: str) -> dict:
    return {
        "message": "Geo-AI Assistant tidak dapat digunakan saat ini. Gunakan panel analisis secara manual.",
        "warnings": [reason],
        "actions": [],
        "cards": [],
    }

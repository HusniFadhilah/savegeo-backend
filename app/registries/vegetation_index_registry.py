"""
vegetation_index_registry.py — Central registry for vegetation index analysis.

Single source of truth for index metadata (formula, bands, range, interpretation,
use cases, limitations), category grouping, default classification rules, and
Indonesian-language narrative generation.

Imported by app.py; no Flask or GEE dependency allowed here (mirrors
landcover_dataset_registry.py's separation of concerns).
"""
from typing import Any, Dict, List, Optional


# ─────────────────────────────────────────────
# Categories
# ─────────────────────────────────────────────
INDEX_CATEGORIES: Dict[str, Dict[str, str]] = {
    "vegetation_health":   {"label": "Kesehatan Vegetasi",        "color": "#2E7D32"},
    "biomass_canopy":      {"label": "Biomassa / Kerapatan Tajuk", "color": "#1B5E20"},
    "moisture":            {"label": "Kadar Air Vegetasi",        "color": "#0277BD"},
    "drought_stress":      {"label": "Kekeringan / Stres Tanaman", "color": "#E65100"},
    "burn_fire":           {"label": "Area Terbakar / Kebakaran",  "color": "#B71C1C"},
    "bare_soil":           {"label": "Tanah Terbuka / Bare Soil",  "color": "#8D6E63"},
    "red_edge_chlorophyll":{"label": "Red-edge / Klorofil",        "color": "#9E9D24"},
    "precision_agri":      {"label": "Pertanian Presisi",          "color": "#F9A825"},
    "forestry_carbon":     {"label": "Kehutanan & Karbon",         "color": "#33691E"},
    "water":               {"label": "Air / Kelembapan Permukaan", "color": "#01579B"},
    "built_up":            {"label": "Area Terbangun",             "color": "#616161"},
}


# ─────────────────────────────────────────────
# Index catalog
#
# bands: generic Sentinel-2 band ids used by calculate_index() in app.py
# classification: ascending bins, last bin's "max" is None (open-ended)
# polarity: "positive" = higher value is healthier/better; "negative" = higher is worse
# comparison_threshold: value used by the compare endpoint to split "baik/tinggi" vs "rendah"
# landsat_compatible: whether an equivalent formula exists on Landsat OLI bands
#                      (informational only — this backend currently only computes from
#                      Sentinel-2 S2_SR_HARMONIZED; used for the "sensor tidak punya band
#                      ini" fallback message, not to actually switch sensors)
# ─────────────────────────────────────────────
VEGETATION_INDEX_CATALOG: Dict[str, Dict[str, Any]] = {
    "NDVI": {
        "name": "Normalized Difference Vegetation Index",
        "formula": "(NIR - RED) / (NIR + RED)",
        "bands": ["B8", "B4"],
        "range": [-1, 1],
        "categories": ["vegetation_health", "forestry_carbon", "precision_agri"],
        "description": "Indeks kesehatan/kehijauan vegetasi paling umum digunakan.",
        "interpretation": {
            "low": "< 0.2 — tanah terbuka, vegetasi sangat jarang, air, atau area terbangun.",
            "medium": "0.2 - 0.4 — vegetasi rendah/renggang (semak, rumput, tanaman muda).",
            "high": "> 0.4 — vegetasi sedang hingga rapat dan sehat (semakin ke 1.0 semakin rapat).",
        },
        "use_cases": ["Monitoring kesehatan vegetasi umum", "Pertanian (fase tumbuh tanaman)", "Kehutanan (kerapatan tajuk)", "Deteksi degradasi lahan"],
        "limitations": ["Jenuh (saturasi) pada vegetasi sangat rapat (>0.8)", "Sensitif terhadap latar tanah pada vegetasi jarang", "Terpengaruh sisa awan tipis/aerosol"],
        "suitable_sensors": ["Sentinel-2", "Landsat 8/9", "PlanetScope"],
        "landsat_compatible": True,
        "palette": ["#8B4513", "#FFFF00", "#90EE90", "#006400"],
        "classification": [
            {"max": 0.0, "label": "Non-vegetasi (air/bangunan/awan)", "color": "#6E6E6E"},
            {"max": 0.2, "label": "Tanah terbuka / vegetasi sangat jarang", "color": "#D2B48C"},
            {"max": 0.4, "label": "Vegetasi rendah", "color": "#ADFF2F"},
            {"max": 0.6, "label": "Vegetasi sedang", "color": "#32CD32"},
            {"max": None, "label": "Vegetasi rapat / sehat", "color": "#006400"},
        ],
        "polarity": "positive",
        "comparison_threshold": 0.4,
    },
    "EVI": {
        "name": "Enhanced Vegetation Index",
        "formula": "2.5 * (NIR - RED) / (NIR + 6*RED - 7.5*BLUE + 1)",
        "bands": ["B8", "B4", "B2"],
        "range": [-1, 1],
        "categories": ["vegetation_health", "biomass_canopy", "forestry_carbon"],
        "description": "Vegetasi dengan koreksi atmosfer dan latar tanah, lebih stabil di biomassa tinggi.",
        "interpretation": {
            "low": "< 0.2 — non-vegetasi atau vegetasi sangat jarang.",
            "medium": "0.2 - 0.4 — vegetasi sedang.",
            "high": "> 0.4 — vegetasi rapat, lebih tahan saturasi dibanding NDVI.",
        },
        "use_cases": ["Monitoring biomassa tinggi/hutan rapat", "Area dengan aerosol/atmosfer bervariasi", "Perkebunan skala besar"],
        "limitations": ["Formula lebih kompleks, butuh band biru berkualitas baik", "Kurang umum dipakai untuk interpretasi awam dibanding NDVI"],
        "suitable_sensors": ["Sentinel-2", "Landsat 8/9", "PlanetScope"],
        "landsat_compatible": True,
        "palette": ["#8B4513", "#FFFF00", "#90EE90", "#006400"],
        "classification": [
            {"max": 0.0, "label": "Non-vegetasi", "color": "#6E6E6E"},
            {"max": 0.2, "label": "Vegetasi jarang", "color": "#D2B48C"},
            {"max": 0.4, "label": "Vegetasi sedang", "color": "#ADFF2F"},
            {"max": 0.6, "label": "Vegetasi rapat", "color": "#32CD32"},
            {"max": None, "label": "Vegetasi sangat rapat / sehat", "color": "#006400"},
        ],
        "polarity": "positive",
        "comparison_threshold": 0.3,
    },
    "SAVI": {
        "name": "Soil Adjusted Vegetation Index",
        "formula": "1.5 * (NIR - RED) / (NIR + RED + 0.5)",
        "bands": ["B8", "B4"],
        "range": [-1, 1],
        "categories": ["biomass_canopy", "precision_agri", "forestry_carbon"],
        "description": "NDVI dengan faktor koreksi latar tanah (L=0.5) — cocok vegetasi jarang/tanaman muda.",
        "interpretation": {
            "low": "< 0.2 — dominasi tanah, tanaman baru tumbuh.",
            "medium": "0.2 - 0.4 — tutupan vegetasi sedang.",
            "high": "> 0.4 — tutupan vegetasi rapat.",
        },
        "use_cases": ["Pertanian fase awal tanam (tanah masih terlihat)", "Lahan semi-arid/vegetasi jarang", "Rehabilitasi lahan tahap awal"],
        "limitations": ["Faktor L=0.5 asumsi kerapatan sedang, tidak optimal di semua kondisi", "Kurang sensitif di vegetasi sangat rapat dibanding EVI"],
        "suitable_sensors": ["Sentinel-2", "Landsat 8/9", "PlanetScope"],
        "landsat_compatible": True,
        "palette": ["#8B4513", "#FFFF00", "#90EE90", "#006400"],
        "classification": [
            {"max": 0.0, "label": "Non-vegetasi / tanah dominan", "color": "#6E6E6E"},
            {"max": 0.2, "label": "Vegetasi sangat jarang", "color": "#D2B48C"},
            {"max": 0.4, "label": "Vegetasi sedang", "color": "#ADFF2F"},
            {"max": 0.6, "label": "Vegetasi rapat", "color": "#32CD32"},
            {"max": None, "label": "Vegetasi sangat rapat", "color": "#006400"},
        ],
        "polarity": "positive",
        "comparison_threshold": 0.3,
    },
    "MSAVI": {
        "name": "Modified Soil Adjusted Vegetation Index (MSAVI2)",
        "formula": "(2*NIR + 1 - sqrt((2*NIR + 1)^2 - 8*(NIR - RED))) / 2",
        "bands": ["B8", "B4"],
        "range": [-1, 1],
        "categories": ["biomass_canopy", "forestry_carbon", "precision_agri"],
        "description": "SAVI dengan faktor L dinamis (self-adjusting) — mengurangi efek tanah tanpa perlu kalibrasi L manual.",
        "interpretation": {
            "low": "< 0.2 — tutupan vegetasi sangat rendah, pengaruh tanah dominan.",
            "medium": "0.2 - 0.4 — tutupan vegetasi sedang.",
            "high": "> 0.4 — tutupan vegetasi/tajuk rapat.",
        },
        "use_cases": ["Estimasi kerapatan tajuk pada vegetasi jarang-sedang", "Lahan pertanian/rehabilitasi dengan variasi tanah tinggi"],
        "limitations": ["Perhitungan lebih berat (melibatkan akar kuadrat)", "Tetap kurang optimal pada tajuk sangat rapat (>0.8 NDVI-equivalent)"],
        "suitable_sensors": ["Sentinel-2", "Landsat 8/9", "PlanetScope"],
        "landsat_compatible": True,
        "palette": ["#8B4513", "#FFFF00", "#90EE90", "#006400"],
        "classification": [
            {"max": 0.0, "label": "Non-vegetasi / tanah dominan", "color": "#6E6E6E"},
            {"max": 0.2, "label": "Vegetasi sangat jarang", "color": "#D2B48C"},
            {"max": 0.4, "label": "Vegetasi sedang", "color": "#ADFF2F"},
            {"max": 0.6, "label": "Vegetasi rapat", "color": "#32CD32"},
            {"max": None, "label": "Vegetasi sangat rapat", "color": "#006400"},
        ],
        "polarity": "positive",
        "comparison_threshold": 0.4,
    },
    "NDMI": {
        "name": "Normalized Difference Moisture Index",
        "formula": "(NIR - SWIR1) / (NIR + SWIR1)",
        "bands": ["B8", "B11"],
        "range": [-1, 1],
        "categories": ["moisture", "drought_stress"],
        "description": "Kandungan air pada kanopi vegetasi.",
        "interpretation": {
            "low": "< 0.0 — vegetasi kering / stres air berat, atau non-vegetasi.",
            "medium": "0.0 - 0.2 — kelembapan sedang, mulai terindikasi stres air.",
            "high": "> 0.2 — kandungan air vegetasi baik.",
        },
        "use_cases": ["Deteksi kekeringan / stres air tanaman", "Monitoring irigasi pertanian", "Early warning kebakaran (vegetasi kering rentan terbakar)"],
        "limitations": ["Dipengaruhi kelembapan tanah di bawah kanopi jarang", "Butuh band SWIR (tidak semua sensor punya resolusi SWIR setara)"],
        "suitable_sensors": ["Sentinel-2", "Landsat 8/9"],
        "landsat_compatible": True,
        "palette": ["#8B4513", "#D2691E", "#90EE90", "#006400"],
        "classification": [
            {"max": -0.2, "label": "Sangat kering / stres air berat", "color": "#8B0000"},
            {"max": 0.0, "label": "Kering / stres air", "color": "#D2691E"},
            {"max": 0.2, "label": "Kelembapan sedang", "color": "#9ACD32"},
            {"max": 0.4, "label": "Kelembapan baik", "color": "#228B22"},
            {"max": None, "label": "Kelembapan sangat baik", "color": "#006400"},
        ],
        "polarity": "positive",
        "comparison_threshold": 0.2,
    },
    "NDWI": {
        "name": "Normalized Difference Water Index",
        "formula": "(GREEN - NIR) / (GREEN + NIR)",
        "bands": ["B3", "B8"],
        "range": [-1, 1],
        "categories": ["water", "moisture"],
        "description": "Kandungan air pada vegetasi / deteksi badan air (McFeeters).",
        "interpretation": {
            "low": "< 0.0 — vegetasi/tanah kering, non-air.",
            "medium": "0.0 - 0.3 — kelembapan permukaan sedang.",
            "high": "> 0.3 — badan air atau vegetasi dengan kandungan air sangat tinggi.",
        },
        "use_cases": ["Deteksi badan air", "Kelembapan vegetasi/lahan basah"],
        "limitations": ["Bisa keliru dengan bayangan atau area terbangun gelap", "Kurang presisi untuk badan air kecil/sempit pada resolusi 10-30m"],
        "suitable_sensors": ["Sentinel-2", "Landsat 8/9", "PlanetScope"],
        "landsat_compatible": True,
        "palette": ["#8B4513", "#F5DEB3", "#87CEEB", "#0000FF"],
        "classification": [
            {"max": 0.0, "label": "Non-air / kering", "color": "#8B4513"},
            {"max": 0.1, "label": "Kelembapan rendah", "color": "#F5DEB3"},
            {"max": 0.3, "label": "Kelembapan sedang", "color": "#87CEEB"},
            {"max": None, "label": "Badan air / sangat basah", "color": "#0000FF"},
        ],
        "polarity": "positive",
        "comparison_threshold": 0.0,
    },
    "MNDWI": {
        "name": "Modified NDWI",
        "formula": "(GREEN - SWIR1) / (GREEN + SWIR1)",
        "bands": ["B3", "B11"],
        "range": [-1, 1],
        "categories": ["water"],
        "description": "Deteksi badan air, lebih tahan terhadap gangguan area terbangun dibanding NDWI.",
        "interpretation": {
            "low": "< 0.0 — non-air (vegetasi/tanah/terbangun).",
            "medium": "0.0 - 0.3 — permukaan basah/lembap.",
            "high": "> 0.3 — badan air jelas.",
        },
        "use_cases": ["Deteksi badan air pada area urban", "Pemetaan lahan basah"],
        "limitations": ["Butuh band SWIR", "Kurang relevan untuk analisis kesehatan vegetasi murni"],
        "suitable_sensors": ["Sentinel-2", "Landsat 8/9"],
        "landsat_compatible": True,
        "palette": ["#FFFFE0", "#98FB98", "#4682B4", "#000080"],
        "classification": [
            {"max": 0.0, "label": "Non-air", "color": "#FFFFE0"},
            {"max": 0.3, "label": "Permukaan basah", "color": "#98FB98"},
            {"max": None, "label": "Badan air", "color": "#000080"},
        ],
        "polarity": "positive",
        "comparison_threshold": 0.0,
    },
    "NDBI": {
        "name": "Normalized Difference Built-up Index",
        "formula": "(SWIR1 - NIR) / (SWIR1 + NIR)",
        "bands": ["B11", "B8"],
        "range": [-1, 1],
        "categories": ["built_up"],
        "description": "Deteksi area terbangun/permukaan kedap air.",
        "interpretation": {
            "low": "< 0.0 — vegetasi/air.",
            "medium": "0.0 - 0.1 — campuran terbangun-vegetasi.",
            "high": "> 0.1 — area terbangun dominan.",
        },
        "use_cases": ["Deteksi urbanisasi/ekspansi terbangun", "Konteks tambahan analisis vegetasi (non-vegetasi mask)"],
        "limitations": ["Bisa keliru dengan tanah terbuka kering (mirip signature spektral)"],
        "suitable_sensors": ["Sentinel-2", "Landsat 8/9"],
        "landsat_compatible": True,
        "palette": ["#006400", "#90EE90", "#FFD700", "#FF0000"],
        "classification": [
            {"max": 0.0, "label": "Vegetasi / air", "color": "#006400"},
            {"max": 0.1, "label": "Campuran terbangun-vegetasi", "color": "#FFD700"},
            {"max": None, "label": "Area terbangun dominan", "color": "#FF0000"},
        ],
        "polarity": "negative",
        "comparison_threshold": 0.0,
    },
    "NBR": {
        "name": "Normalized Burn Ratio",
        "formula": "(NIR - SWIR2) / (NIR + SWIR2)",
        "bands": ["B8", "B12"],
        "range": [-1, 1],
        "categories": ["burn_fire", "forestry_carbon"],
        "description": "Deteksi area terbakar dan tingkat keparahan/regenerasi pasca kebakaran.",
        "interpretation": {
            "low": "< 0.1 — indikasi area terbakar / vegetasi terdegradasi berat.",
            "medium": "0.1 - 0.44 — vegetasi jarang atau dalam regenerasi awal pasca gangguan.",
            "high": "> 0.44 — vegetasi sehat/rapat, tidak menunjukkan tanda kebakaran.",
        },
        "use_cases": ["Deteksi bekas kebakaran (dNBR pre/post untuk severity)", "Monitoring regenerasi hutan pasca kebakaran/tebang", "Kehutanan dan karbon (degradasi tajuk)"],
        "limitations": ["Butuh citra pre-fire untuk severity akurat (dNBR), NBR tunggal hanya indikasi kasar", "Bisa terpengaruh bayangan awan/topografi"],
        "suitable_sensors": ["Sentinel-2", "Landsat 8/9"],
        "landsat_compatible": True,
        "palette": ["#8B0000", "#DAA520", "#9ACD32", "#006400"],
        "classification": [
            {"max": -0.1, "label": "Area terbakar berat (high severity)", "color": "#4B0000"},
            {"max": 0.1, "label": "Area terbakar sedang / terdegradasi", "color": "#B22222"},
            {"max": 0.27, "label": "Vegetasi jarang / regenerasi awal", "color": "#DAA520"},
            {"max": 0.44, "label": "Vegetasi sedang", "color": "#9ACD32"},
            {"max": None, "label": "Vegetasi sehat / rapat", "color": "#006400"},
        ],
        "polarity": "positive",
        "comparison_threshold": 0.2,
    },
    "BSI": {
        "name": "Bare Soil Index",
        "formula": "((SWIR1 + RED) - (NIR + BLUE)) / ((SWIR1 + RED) + (NIR + BLUE))",
        "bands": ["B11", "B4", "B8", "B2"],
        "range": [-1, 1],
        "categories": ["bare_soil"],
        "description": "Deteksi tanah terbuka / lahan tanpa vegetasi.",
        "interpretation": {
            "low": "< 0.0 — vegetasi dominan, bukan tanah terbuka.",
            "medium": "0.0 - 0.2 — campuran vegetasi-tanah.",
            "high": "> 0.2 — tanah terbuka dominan.",
        },
        "use_cases": ["Deteksi lahan gundul/tanah terbuka", "Monitoring deforestasi/pembukaan lahan", "Rehabilitasi lahan (baseline sebelum tanam)"],
        "limitations": ["Bisa keliru dengan area terbangun (signature spektral mirip tanah kering)"],
        "suitable_sensors": ["Sentinel-2", "Landsat 8/9"],
        "landsat_compatible": True,
        "palette": ["#006400", "#90EE90", "#DEB887", "#8B4513"],
        "classification": [
            {"max": -0.2, "label": "Vegetasi sangat rapat", "color": "#006400"},
            {"max": 0.0, "label": "Vegetasi dominan", "color": "#9ACD32"},
            {"max": 0.2, "label": "Campuran vegetasi-tanah", "color": "#DAA520"},
            {"max": None, "label": "Tanah terbuka dominan", "color": "#8B4513"},
        ],
        "polarity": "negative",
        "comparison_threshold": 0.0,
    },
    "NDRE": {
        "name": "Normalized Difference Red Edge Index",
        "formula": "(NIR - RED_EDGE) / (NIR + RED_EDGE)",
        "bands": ["B8", "B5"],
        "range": [-1, 1],
        "categories": ["red_edge_chlorophyll", "precision_agri"],
        "description": "Kandungan klorofil menggunakan band red-edge — lebih sensitif dari NDVI pada vegetasi rapat/matang dan tidak mudah jenuh.",
        "interpretation": {
            "low": "< 0.1 — kandungan klorofil rendah / stres nutrisi-nitrogen.",
            "medium": "0.1 - 0.3 — klorofil sedang.",
            "high": "> 0.3 — klorofil tinggi, tanaman sehat/matang.",
        },
        "use_cases": ["Pertanian presisi (deteksi stres nutrisi/nitrogen dini)", "Monitoring tanaman fase matang saat NDVI sudah jenuh", "Deteksi stres sebelum terlihat kasat mata"],
        "limitations": ["Butuh band red-edge — TIDAK tersedia di Landsat OLI (hanya Sentinel-2 dan sensor multispektral khusus pertanian)", "Kurang umum untuk vegetasi non-pertanian"],
        "suitable_sensors": ["Sentinel-2", "PlanetScope (band red-edge tertentu)"],
        "landsat_compatible": False,
        "palette": ["#8B4513", "#FFFF00", "#9ACD32", "#006400"],
        "classification": [
            {"max": 0.0, "label": "Non-vegetasi / stres berat", "color": "#8B4513"},
            {"max": 0.1, "label": "Klorofil rendah", "color": "#FFFF00"},
            {"max": 0.2, "label": "Klorofil sedang", "color": "#9ACD32"},
            {"max": 0.3, "label": "Klorofil tinggi", "color": "#4CAF50"},
            {"max": None, "label": "Klorofil sangat tinggi", "color": "#006400"},
        ],
        "polarity": "positive",
        "comparison_threshold": 0.2,
    },
    "GCI": {
        "name": "Green Chlorophyll Index",
        "formula": "(NIR / GREEN) - 1",
        "bands": ["B8", "B3"],
        "range": [0, 8],
        "categories": ["red_edge_chlorophyll", "precision_agri"],
        "description": "Estimasi kandungan klorofil daun berdasarkan rasio NIR/Green.",
        "interpretation": {
            "low": "< 1.0 — klorofil rendah, tanaman muda atau stres.",
            "medium": "1.0 - 2.0 — klorofil sedang.",
            "high": "> 2.0 — klorofil tinggi, kondisi tanaman baik.",
        },
        "use_cases": ["Pertanian presisi (status nitrogen tanaman)", "Monitoring pertumbuhan tanaman pangan/perkebunan"],
        "limitations": ["Rentang nilai tidak dibatasi -1..1 (rasio, bisa besar) — perlu normalisasi visual berbeda dari indeks ND", "Sensitif terhadap sudut pengambilan citra dan kalibrasi radiometrik"],
        "suitable_sensors": ["Sentinel-2", "Landsat 8/9", "PlanetScope"],
        "landsat_compatible": True,
        "palette": ["#8B4513", "#FFFF00", "#9ACD32", "#006400"],
        "classification": [
            {"max": 1.0, "label": "Klorofil rendah", "color": "#8B4513"},
            {"max": 2.0, "label": "Klorofil sedang", "color": "#FFFF00"},
            {"max": 4.0, "label": "Klorofil tinggi", "color": "#9ACD32"},
            {"max": None, "label": "Klorofil sangat tinggi", "color": "#006400"},
        ],
        "polarity": "positive",
        "comparison_threshold": 1.5,
    },
    "ARVI": {
        "name": "Atmospherically Resistant Vegetation Index",
        "formula": "(NIR - (2*RED - BLUE)) / (NIR + (2*RED - BLUE))",
        "bands": ["B8", "B4", "B2"],
        "range": [-1, 1],
        "categories": ["vegetation_health"],
        "description": "NDVI dengan koreksi hamburan atmosfer (aerosol) memakai band biru.",
        "interpretation": {
            "low": "< 0.2 — non-vegetasi / vegetasi sangat jarang.",
            "medium": "0.2 - 0.4 — vegetasi sedang.",
            "high": "> 0.4 — vegetasi rapat/sehat.",
        },
        "use_cases": ["Area dengan gangguan atmosfer/aerosol/asap tinggi", "Monitoring regional skala luas dengan variasi kondisi atmosfer"],
        "limitations": ["Butuh band biru berkualitas baik", "Perbaikan atas NDVI relatif kecil pada citra dengan koreksi atmosfer yang sudah baik (mis. Sentinel-2 SR)"],
        "suitable_sensors": ["Sentinel-2", "Landsat 8/9", "PlanetScope"],
        "landsat_compatible": True,
        "palette": ["#8B4513", "#FFFF00", "#90EE90", "#006400"],
        "classification": [
            {"max": 0.0, "label": "Non-vegetasi", "color": "#6E6E6E"},
            {"max": 0.2, "label": "Vegetasi jarang", "color": "#D2B48C"},
            {"max": 0.4, "label": "Vegetasi sedang", "color": "#ADFF2F"},
            {"max": 0.6, "label": "Vegetasi rapat", "color": "#32CD32"},
            {"max": None, "label": "Vegetasi sangat rapat", "color": "#006400"},
        ],
        "polarity": "positive",
        "comparison_threshold": 0.4,
    },
    "VARI": {
        "name": "Visible Atmospherically Resistant Index",
        "formula": "(GREEN - RED) / (GREEN + RED - BLUE)",
        "bands": ["B3", "B4", "B2"],
        "range": [-1, 1],
        "categories": ["vegetation_health"],
        "description": "Kehijauan vegetasi HANYA dari band tampak (RGB) — tidak butuh NIR, cocok untuk citra drone/kamera RGB biasa.",
        "interpretation": {
            "low": "< 0.0 — non-vegetasi.",
            "medium": "0.0 - 0.2 — vegetasi jarang-sedang.",
            "high": "> 0.2 — vegetasi rapat/hijau.",
        },
        "use_cases": ["Analisis dari drone/kamera RGB tanpa sensor NIR", "Cek cepat kehijauan dari citra visual"],
        "limitations": ["Kurang presisi dibanding indeks berbasis NIR", "Sensitif terhadap variasi pencahayaan/warna tanah"],
        "suitable_sensors": ["Sentinel-2", "Landsat 8/9", "Drone RGB", "PlanetScope"],
        "landsat_compatible": True,
        "palette": ["#8B4513", "#FFFF00", "#90EE90", "#006400"],
        "classification": [
            {"max": 0.0, "label": "Non-vegetasi", "color": "#6E6E6E"},
            {"max": 0.1, "label": "Vegetasi jarang", "color": "#D2B48C"},
            {"max": 0.2, "label": "Vegetasi sedang", "color": "#ADFF2F"},
            {"max": None, "label": "Vegetasi rapat / hijau", "color": "#006400"},
        ],
        "polarity": "positive",
        "comparison_threshold": 0.15,
    },
    "SIPI": {
        "name": "Structure Insensitive Pigment Index",
        "formula": "(NIR - BLUE) / (NIR - RED)",
        "bands": ["B8", "B2", "B4"],
        "range": [0, 2],
        "categories": ["drought_stress", "red_edge_chlorophyll"],
        "description": "Rasio pigmen karotenoid:klorofil — naik saat tanaman stres (klorofil turun relatif terhadap karotenoid), tidak terlalu dipengaruhi struktur kanopi.",
        "interpretation": {
            "low": "< 1.0 — rasio pigmen normal, tanaman sehat.",
            "medium": "1.0 - 1.5 — indikasi stres ringan-sedang.",
            "high": "> 1.5 — indikasi stres tanaman signifikan (penyakit, kekurangan air/nutrisi).",
        },
        "use_cases": ["Deteksi dini stres tanaman sebelum gejala visual jelas", "Pertanian presisi (screening kesehatan tanaman)"],
        "limitations": ["Interpretasi kurang intuitif dibanding NDVI (rasio, bukan indeks -1..1)", "Butuh validasi lapangan untuk memastikan penyebab stres"],
        "suitable_sensors": ["Sentinel-2", "Landsat 8/9", "PlanetScope"],
        "landsat_compatible": True,
        "palette": ["#006400", "#9ACD32", "#DAA520", "#8B0000"],
        "classification": [
            {"max": 1.0, "label": "Sehat, rasio pigmen normal", "color": "#006400"},
            {"max": 1.5, "label": "Stres ringan-sedang", "color": "#DAA520"},
            {"max": 2.0, "label": "Stres signifikan", "color": "#B22222"},
            {"max": None, "label": "Stres berat", "color": "#8B0000"},
        ],
        "polarity": "negative",
        "comparison_threshold": 1.2,
    },
    "LAI_PROXY": {
        "name": "Leaf Area Index (proxy empiris)",
        "formula": "max(0, 3.618 * EVI - 0.118)",
        "bands": ["B8", "B4", "B2"],
        "range": [0, 8],
        "categories": ["biomass_canopy", "forestry_carbon"],
        "description": "Estimasi proksi LAI (luas daun per luas tanah) dari EVI memakai regresi empiris umum (Boegh et al.) — BUKAN LAI hasil inversi model fisik/radiative transfer, hanya perkiraan kasar.",
        "interpretation": {
            "low": "< 1 — tajuk sangat jarang / vegetasi awal tumbuh.",
            "medium": "1 - 3 — tajuk sedang.",
            "high": "> 3 — tajuk rapat, biomassa daun tinggi.",
        },
        "use_cases": ["Perkiraan kerapatan tajuk untuk estimasi biomassa/karbon kasar", "Monitoring pertumbuhan tanaman perkebunan/hutan tanaman"],
        "limitations": ["Proksi empiris generik, TIDAK dikalibrasi per jenis vegetasi/lokasi — hindari klaim akurasi tinggi", "Untuk LAI akurat sebaiknya gunakan model inversi khusus (mis. PROSAIL) atau data lapangan"],
        "suitable_sensors": ["Sentinel-2", "Landsat 8/9", "PlanetScope"],
        "landsat_compatible": True,
        "palette": ["#8B4513", "#FFFF00", "#9ACD32", "#006400"],
        "classification": [
            {"max": 1.0, "label": "Tajuk sangat jarang", "color": "#8B4513"},
            {"max": 2.0, "label": "Tajuk jarang", "color": "#FFFF00"},
            {"max": 3.0, "label": "Tajuk sedang", "color": "#9ACD32"},
            {"max": 5.0, "label": "Tajuk rapat", "color": "#4CAF50"},
            {"max": None, "label": "Tajuk sangat rapat", "color": "#006400"},
        ],
        "polarity": "positive",
        "comparison_threshold": 2.0,
    },
}


# ─────────────────────────────────────────────
# Domain → recommended indices (used by catalog UI + AI assistant Q&A)
# ─────────────────────────────────────────────
DOMAIN_RECOMMENDATIONS: Dict[str, Dict[str, Any]] = {
    "pertanian": {
        "label": "Pertanian presisi",
        "indices": ["NDRE", "GCI", "SAVI", "NDVI", "SIPI"],
        "reason": "NDRE/GCI mendeteksi status klorofil-nitrogen lebih dini dari NDVI; SAVI mengoreksi latar tanah pada fase tanam awal; SIPI membantu screening stres sebelum gejala visual.",
    },
    "kehutanan": {
        "label": "Kehutanan",
        "indices": ["NDVI", "EVI", "MSAVI", "LAI_PROXY", "NBR"],
        "reason": "EVI/MSAVI lebih tahan saturasi pada tajuk rapat dibanding NDVI; LAI proxy mengindikasikan kerapatan daun; NBR memantau gangguan (kebakaran/tebang).",
    },
    "karbon": {
        "label": "Karbon & biomassa",
        "indices": ["EVI", "MSAVI", "LAI_PROXY", "NBR", "NDVI"],
        "reason": "Indeks biomassa/kerapatan tajuk berkorelasi dengan stok karbon aboveground; NBR penting untuk mendeteksi kehilangan karbon akibat kebakaran/degradasi.",
    },
    "kekeringan": {
        "label": "Kekeringan / stres air",
        "indices": ["NDMI", "NDWI", "SIPI", "NDVI"],
        "reason": "NDMI/NDWI langsung mengukur kandungan air vegetasi; SIPI mendeteksi stres pigmen; penurunan NDVI mengindikasikan dampak lanjutan kekeringan.",
    },
    "kebakaran": {
        "label": "Area terbakar",
        "indices": ["NBR", "BSI", "NDVI"],
        "reason": "NBR adalah indeks standar deteksi burn scar (idealnya dNBR pre/post); BSI membantu membedakan tanah gundul akibat bakar dari vegetasi; NDVI melacak proses regenerasi.",
    },
}


def get_indices_by_category() -> Dict[str, List[str]]:
    grouped: Dict[str, List[str]] = {key: [] for key in INDEX_CATEGORIES}
    for idx_key, meta in VEGETATION_INDEX_CATALOG.items():
        for cat in meta.get("categories", []):
            grouped.setdefault(cat, []).append(idx_key)
    return grouped


def get_catalog_payload() -> Dict[str, Any]:
    """Full catalog for the frontend index-selection panel (metadata only, no GEE compute)."""
    return {
        "categories": INDEX_CATEGORIES,
        "indices": VEGETATION_INDEX_CATALOG,
        "indices_by_category": get_indices_by_category(),
        "domain_recommendations": DOMAIN_RECOMMENDATIONS,
    }


def classify_value(index_name: str, value: Optional[float]) -> Optional[Dict[str, Any]]:
    """Return the classification bin {label, color, max} a scalar value falls into."""
    if value is None:
        return None
    bins = VEGETATION_INDEX_CATALOG.get(index_name, {}).get("classification") or []
    for i, b in enumerate(bins):
        if b["max"] is None or value < b["max"]:
            return {**b, "class_value": i}
    return None


def _health_label(index_name: str, mean_value: Optional[float]) -> str:
    cls = classify_value(index_name, mean_value)
    return cls["label"] if cls else "tidak diketahui"


def generate_index_narrative(
    index_name: str,
    stats: Dict[str, Any],
    classification: Optional[Dict[str, Any]] = None,
) -> str:
    """Auto-generate an Indonesian-language interpretation paragraph for one index result."""
    meta = VEGETATION_INDEX_CATALOG.get(index_name)
    if not meta:
        return f"Indeks {index_name} dihitung, namun belum memiliki aturan interpretasi otomatis."

    mean = stats.get("mean")
    if mean is None:
        return f"Statistik {index_name} tidak tersedia (kemungkinan seluruh piksel AOI tertutup awan atau termask)."

    mean = float(mean)
    health = _health_label(index_name, mean)
    lines = [
        f"**{index_name}** ({meta['name']}) rata-rata **{mean:.3f}** pada area ini — tergolong *{health}*."
    ]

    if classification and classification.get("classes"):
        classes = classification["classes"]
        dominant = max(classes.items(), key=lambda kv: kv[1]["area"])
        lines.append(
            f"Kelas dominan: **{dominant[0]}** seluas {dominant[1]['area']:.1f} ha "
            f"({dominant[1]['percentage']:.1f}% dari AOI)."
        )
        healthy_labels = {b["label"] for b in meta["classification"][-2:]}
        stress_labels = {b["label"] for b in meta["classification"][:2]}
        healthy_pct = sum(c["percentage"] for label, c in classes.items() if label in healthy_labels)
        stress_pct = sum(c["percentage"] for label, c in classes.items() if label in stress_labels)
        if healthy_pct:
            lines.append(f"Area dengan kondisi baik/sehat: sekitar {healthy_pct:.1f}% dari AOI.")
        if stress_pct:
            lines.append(f"Area dengan kondisi rendah/stres/terbuka: sekitar {stress_pct:.1f}% dari AOI.")

    # Actionable recommendation per category
    categories = meta.get("categories", [])
    if "burn_fire" in categories and mean < 0.1:
        lines.append("Rekomendasi: indikasi area terbakar/terdegradasi — verifikasi lapangan dan bandingkan dengan citra sebelum kejadian (dNBR) untuk menilai tingkat keparahan.")
    elif "drought_stress" in categories or "moisture" in categories:
        if meta["polarity"] == "positive" and mean < meta["comparison_threshold"]:
            lines.append("Rekomendasi: indikasi stres air/kekeringan — pertimbangkan pengecekan irigasi atau jadwal penyiraman.")
    elif "bare_soil" in categories and mean > meta["comparison_threshold"]:
        lines.append("Rekomendasi: tanah terbuka dominan — evaluasi risiko erosi dan pertimbangkan revegetasi/penanaman.")
    elif "vegetation_health" in categories or "biomass_canopy" in categories:
        if mean < meta["comparison_threshold"]:
            lines.append("Rekomendasi: vegetasi tergolong jarang/rendah — cek potensi gangguan (kekeringan, hama, pembukaan lahan) dan pertimbangkan monitoring lanjutan.")
        else:
            lines.append("Rekomendasi: kondisi vegetasi cukup baik — pertahankan monitoring berkala untuk deteksi dini perubahan.")

    return " ".join(lines)


def generate_comparison_narrative(
    index_a: str, index_b: str, quadrants: Dict[str, Dict[str, float]]
) -> str:
    """Auto-generate an Indonesian insight paragraph for a two-index comparison.

    The compare endpoint's "high_a"/"high_b" quadrant bits mean "favorable side of
    the threshold" (gte for positive-polarity indices like NDVI, lt for negative-
    polarity ones like BSI/SIPI where a LOW raw value is the good outcome) — so the
    text here says "kondisi baik/kurang baik", never "tinggi/rendah", or an NDVI-vs-BSI
    comparison would misreport low (good) BSI as "BSI tinggi".
    """
    meta_a = VEGETATION_INDEX_CATALOG.get(index_a, {})
    meta_b = VEGETATION_INDEX_CATALOG.get(index_b, {})
    name_a, name_b = meta_a.get("name", index_a), meta_b.get("name", index_b)

    hi_hi = quadrants.get("high_high", {}).get("percentage", 0)
    hi_lo = quadrants.get("high_low", {}).get("percentage", 0)
    lo_hi = quadrants.get("low_high", {}).get("percentage", 0)
    lo_lo = quadrants.get("low_low", {}).get("percentage", 0)

    lines = [f"Perbandingan **{index_a}** ({name_a}) vs **{index_b}** ({name_b}):"]
    lines.append(
        f"{hi_hi:.1f}% area kondisi baik di kedua indeks, "
        f"{hi_lo:.1f}% area {index_a} baik tapi {index_b} kurang baik, "
        f"{lo_hi:.1f}% area {index_a} kurang baik tapi {index_b} baik, "
        f"{lo_lo:.1f}% area kurang baik di kedua indeks."
    )

    if hi_lo > 15:
        lines.append(
            f"Catatan penting: {hi_lo:.1f}% area menunjukkan {index_a} baik namun {index_b} kurang baik — "
            f"kemungkinan vegetasi tampak rapat/hijau tetapi mengalami stres "
            f"({'kekurangan air' if index_b in ('NDMI', 'NDWI') else 'kondisi kurang optimal pada ' + index_b}). "
            "Perlu verifikasi lapangan."
        )
    if lo_lo > 40:
        lines.append(f"Sebagian besar area ({lo_lo:.1f}%) berada di kondisi kurang baik pada kedua indeks — indikasi area terbuka/non-vegetasi/stres dominan.")
    if hi_hi > 40:
        lines.append(f"Mayoritas area ({hi_hi:.1f}%) menunjukkan kondisi baik pada kedua indeks.")

    return " ".join(lines)


def available_bands_for_index(index_name: str, available_bands: List[str]) -> bool:
    """Check whether the composite has every band an index formula needs."""
    required = VEGETATION_INDEX_CATALOG.get(index_name, {}).get("bands", [])
    return all(b in available_bands for b in required)

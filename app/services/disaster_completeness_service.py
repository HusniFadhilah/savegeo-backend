"""Describe actual published coverage of an event, separately from model availability."""
from __future__ import annotations

from app.registries.disaster_model_registry import list_models

_TASKS = (
    ("land_change", "Perubahan tutupan lahan", {"land_cover"},
     "Pasangan Sentinel-2 dengan prediksi Dynamic World, grid dan piksel valid bersama."),
    ("water", "Air / genangan", {"water_extent", "inundation_change_indicator"},
     "Pasangan Sentinel-1 untuk banjir atau kelas air dari Dynamic World."),
    ("trees", "Tutupan pohon", {"land_cover", "vegetation_change_indicator"},
     "Kelas pohon pre/post; luas tutupan, bukan jumlah pohon individual."),
    ("buildings", "Identifikasi bangunan", {"building_footprint", "building_exposure"},
     "Footprint bersumber atau segmentasi citra resolusi tinggi yang sudah ditinjau."),
    ("roads", "Jalan / paparan jalan", {"road_exposure", "road_impact_indicator"},
     "Jaringan jalan bersumber dan geometri bahaya; panjang ruas tidak dihitung ganda."),
    ("building_damage", "Estimasi kerusakan bangunan", {"building_damage", "damage_assessment"},
     "Citra resolusi tinggi sebelum/sesudah yang selaras, model kerusakan terlatih, dan validasi lokal."),
)


def build_completeness(disaster_type: str, analyses: list[dict]) -> dict:
    models = list_models(disaster_type=disaster_type)
    sections = []
    for key, label, semantics, requirement in _TASKS:
        candidates = [m for m in models if m.get("result_semantics") in semantics]
        if key == "building_damage":
            candidates = [m for m in models if m.get("damage_model")]
        available = [a for a in analyses if a.get("available") and (
            a.get("damage_model") if key == "building_damage" else a.get("result_semantics") in semantics
        )]
        # A post-only land-cover map does not establish change. The persisted
        # comparison must contain both dates and a transition matrix.
        if key == "land_change":
            available = [a for a in available if (a.get("result") or {}).get("comparison")]
        if key == "water":
            available += [a for a in analyses if a.get("available") and a.get("result_semantics") == "land_cover"
                          and (a.get("result") or {}).get("comparison")]
        status = "available" if available else "not_computed" if any(m.get("enabled") for m in candidates) else "requires_model_data"
        sections.append({"id": key, "label": label, "status": status,
                         "model_ids": sorted({a["model_id"] for a in available}),
                         "requirements": [] if available else [requirement],
                         "interpretation": "Estimasi dari model; perlu validasi" if key == "building_damage" else requirement})
    return {"complete": all(s["status"] == "available" for s in sections),
            "available_count": sum(s["status"] == "available" for s in sections),
            "required_count": len(sections), "sections": sections,
            "note": "Kelengkapan dihitung dari hasil yang dipublikasikan dan masih kompatibel. Area terbangun, paparan, dan perubahan kelas tidak menghitung bangunan rusak."}

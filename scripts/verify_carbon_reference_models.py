"""Exercise deployed models through the local authenticated API and real EE."""
import argparse
import json
import math
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path

import numpy as np
import requests
from PIL import Image

from app.core.security import create_access_token
from app.db.models.admin_user import AdminUser
from app.db.models.uploaded_model import UploadedModel
from app.db.session import SessionLocal


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:5501/api")
    args = parser.parse_args()
    with SessionLocal() as db:
        admin = db.query(AdminUser).filter_by(is_active=True).first()
        if admin is None:
            raise RuntimeError("No active local admin for authenticated verification.")
        headers = {"Authorization": "Bearer " + create_access_token(admin)}
        models = [row.to_dict_public() for row in db.query(UploadedModel).filter_by(model_type="carbon", is_active=True).all()
                  if (row.metadata_json or {}).get("method") == "saved_reference_ensemble"]
    response = requests.get(args.base_url + "/models?model_type=carbon", headers=headers, timeout=30)
    response.raise_for_status()
    listed = {model["name"] for model in response.json()["models"]}
    report = {"model_count": len(models), "listed": all(m["name"] in listed for m in models), "runs": []}
    root = Path("var/training_runs/carbon_reference_20261009")

    def check(model):
        meta = model["metadata_json"]
        body = {"aoi": {"west": 110.35, "south": -7.07, "east": 110.45, "north": -6.98},
                "reference_dataset": meta["target_dataset_key"], "dataset_year": meta["reference_year"],
                "model_name": model["name"], "clip_to_aoi": True, "scale": 300, "year": 2026}
        try:
            result = requests.post(args.base_url + "/analyze/carbon", headers=headers, json=body, timeout=300)
            if result.status_code != 200:
                return {"model": model["name"], "status": "failed", "http_status": result.status_code, "error": result.text[:350]}
            data = result.json()
            stats = data["carbon_estimated"]["statistics"]
            info = data["model_info"]
            if not all(math.isfinite(stats[key]) for key in ("mean", "min", "max")):
                raise ValueError("Non-finite output statistics.")
            if info.get("deployment_mode") != "frozen_ensemble":
                raise ValueError("API did not execute the frozen deployment.")
            if "g/kg" in data["carbon_estimated"]["unit"] and data["area_info"]["total_carbon_tons"] is not None:
                raise ValueError("Concentration was incorrectly reported as carbon tonnage.")
            # GEE tile must contain visible output, not merely return a URL.
            z = 12
            x = int((110.4 + 180) / 360 * 2 ** z)
            lat = math.radians(-7.025)
            y = int((1 - math.asinh(math.tan(lat)) / math.pi) / 2 * 2 ** z)
            url = data["carbon_estimated"]["tile_url"].replace("{z}", str(z)).replace("{x}", str(x)).replace("{y}", str(y))
            tile = requests.get(url, timeout=90)
            tile.raise_for_status()
            rgba = np.asarray(Image.open(BytesIO(tile.content)).convert("RGBA"))
            if not np.any(rgba[:, :, 3]):
                raise ValueError("Estimated tile is entirely transparent.")
            (root / f"{model['name']}.png").write_bytes(tile.content)
            # Keep the real response for provenance, without expiring tile tokens.
            for layer in ("carbon_estimated", "carbon_reference", "carbon_uncertainty"):
                if layer in data:
                    data[layer]["tile_url"] = "verified; expiring URL omitted"
            (root / f"{model['name']}.response.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
            return {"model": model["name"], "status": "passed", "http_status": 200, "statistics": stats,
                    "unit": data["carbon_estimated"]["unit"], "tile_visible_pixels": int(np.count_nonzero(rgba[:, :, 3]))}
        except Exception as exc:
            return {"model": model["name"], "status": "failed", "error": str(exc)}

    with ThreadPoolExecutor(max_workers=3) as executor:
        for run in executor.map(check, models):
            report["runs"].append(run)
            print(run["model"], run["status"], run.get("error", ""), flush=True)
            (root / "verification.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    report["passed"] = sum(run["status"] == "passed" for run in report["runs"])
    (root / "verification.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print("VERIFIED", report["passed"], "/", len(models), flush=True)
    if not report["listed"] or report["passed"] != len(models):
        raise SystemExit(1)


if __name__ == "__main__":
    main()

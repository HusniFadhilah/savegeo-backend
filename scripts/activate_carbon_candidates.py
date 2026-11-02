"""Register reviewed frozen candidates without retraining or changing their weights."""
import argparse
import hashlib
import json
from pathlib import Path

from app.core.config import get_settings
from app.db.models.uploaded_model import UploadedModel
from app.db.session import SessionLocal


def activate(root: Path, expected: dict[str, str]) -> list[str]:
    prepared = []
    output = get_settings().model_path.resolve()
    output.mkdir(parents=True, exist_ok=True)
    for name, checksum in expected.items():
        meta = json.loads((root / f"{name}.json").read_text())
        source = (root / meta["frozen_ensemble_file"]).resolve()
        if source.parent != root.resolve() or source.name != f"{name}.ensemble.json":
            raise ValueError("Candidate artifact path is invalid")
        content = source.read_bytes()
        if hashlib.sha256(content).hexdigest() != checksum or meta["artifact_sha256"] != checksum:
            raise ValueError("Reviewed artifact checksum differs")
        if meta["training_domain"] != "indonesia" or meta["method"] != "saved_reference_ensemble":
            raise ValueError("Unexpected model domain or method")
        if meta["validation_metrics"]["r2"] <= 0 or meta["frozen_gee_parity_max_error"] > 1e-4:
            raise ValueError("Candidate did not pass validation")
        prepared.append((name, meta, content))
    with SessionLocal() as db:
        for name, meta, content in prepared:
            artifact = output / f"{name}.ensemble.json"
            temporary = artifact.with_suffix(".tmp")
            temporary.write_bytes(content)
            temporary.replace(artifact)
            (output / f"{name}.json").write_text(json.dumps(meta, indent=2))
            row = db.query(UploadedModel).filter_by(name=name).first()
            if row is None:
                row = UploadedModel(name=name, model_type="carbon")
                db.add(row)
            row.display_name = f"{meta['target_dataset_key']} · Indonesia · {meta['reference_year']} calibration"
            row.algorithm = json.loads(content)["algorithm"]
            row.filename = artifact.name
            row.filepath = str(artifact)
            row.file_size_kb = len(content) / 1024
            row.is_active = True
            row.is_default = False
            row.version = "1.0"
            row.description = meta["description"]
            row.feature_names = meta["feature_names"]
            row.metadata_json = meta
            score = meta["validation_metrics"]
            row.metrics = {"cv_metrics": {"r2_mean": score["r2"], "rmse_mean": score["rmse"], "mae_mean": score["mae"]}, "n_samples": meta["n_samples"]}
        db.commit()
    return [name for name, _, _ in prepared]


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidates", type=Path, required=True)
    parser.add_argument("--reviewed-checksums", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps({"activated": activate(args.candidates, json.loads(args.reviewed_checksums.read_text()))}))

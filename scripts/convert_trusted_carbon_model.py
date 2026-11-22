"""One-time offline conversion. Only use a trusted, hash-verified legacy file.

Run outside the web process; pickle can execute code. The --sha256 value must
come from the operator's independently verified inventory, not from an upload.
The original pickle and original JSON metadata are retained.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path

import numpy as np


def convert(path: Path, expected_sha256: str) -> dict:
    if path.is_symlink() or path.suffix != ".pkl":
        raise ValueError("Expected a regular trusted legacy pickle")
    raw = path.read_bytes()
    actual = hashlib.sha256(raw).hexdigest()
    if actual != expected_sha256.lower():
        raise ValueError("Legacy model checksum mismatch")
    if path.with_suffix(".onnx").exists():
        raise ValueError("ONNX export already exists; refusing to overwrite")
    import joblib  # Offline trust boundary; deliberately absent from runtime loader.
    from app.inference.carbon_model import CarbonEstimationModel
    metadata_path = path.with_suffix(".json")
    metadata = json.loads(metadata_path.read_text(encoding="utf8"))
    saved = joblib.load(path)  # nosec B301 - explicit offline operator trust, hash checked above
    instance = CarbonEstimationModel(metadata["algorithm"])
    instance.model, instance.scaler = saved["model"], saved["scaler"]
    instance.feature_names = saved["feature_names"]
    instance.metadata = metadata
    # Compare the same feature matrix before/after conversion, including scaler.
    n = len(instance.feature_names)
    rng = np.random.default_rng(42)
    matrix = rng.normal(size=(256, n))
    if hasattr(instance.scaler, "mean_"):
        matrix = matrix * instance.scaler.scale_ + instance.scaler.mean_
    expected = instance.predict(matrix)
    backup = metadata_path.with_suffix(".legacy.json")
    if backup.exists():
        raise ValueError("Original metadata backup already exists")
    shutil.copyfile(metadata_path, backup)
    try:
        instance.metadata["legacy_sha256"] = actual
        instance.save(str(path))
        loaded = CarbonEstimationModel.load(str(path))
        result = loaded.predict(matrix)
        np.testing.assert_allclose(result, expected, rtol=1e-4, atol=1e-3)
    except Exception:
        shutil.copyfile(backup, metadata_path)
        path.with_suffix(".onnx").unlink(missing_ok=True)
        raise
    return {"model": path.name, "legacy_sha256": actual, "samples": 256,
            "max_absolute_error": float(np.max(np.abs(result - expected))),
            "onnx_sha256": hashlib.sha256(path.with_suffix('.onnx').read_bytes()).hexdigest()}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    parser.add_argument("--sha256", required=True)
    args = parser.parse_args()
    print(json.dumps(convert(args.path, args.sha256), indent=2))

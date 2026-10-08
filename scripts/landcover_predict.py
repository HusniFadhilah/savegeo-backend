"""Run a trained embedding classifier or exported segmentation head on a raster.

Input is a georeferenced feature stack in the band's documented order. This
does not turn an untrained foundation backbone into a land-cover classifier.
TorchScript wrappers must accept NCHW tensors and return N,K,H,W logits;
joblib estimators must predict the raw class codes in the schema.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
from pathlib import Path

import numpy as np
import rasterio
from rasterio.shutil import copy as raster_copy
from rasterio.windows import Window


def digest(path: Path) -> str:
    checksum = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            checksum.update(chunk)
    return checksum.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--schema", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--backend", choices=["joblib", "torchscript"], required=True)
    parser.add_argument("--year", type=int, required=True)
    parser.add_argument("--tile-size", type=int, default=256)
    parser.add_argument("--halo", type=int, default=32)
    parser.add_argument("--device", default="cpu")
    args = parser.parse_args()
    if args.tile_size < 32 or args.halo < 0 or args.output.exists():
        parser.error("tile-size >=32, halo >=0, and a new output path are required")
    schema = json.loads(args.schema.read_text(encoding="utf-8"))
    codes = np.asarray(schema["class_codes"], dtype=np.int32)
    bands = schema["bands"]
    if len(codes) < 2 or len(set(codes)) != len(codes) or np.any((codes < 0) | (codes >= 1000)):
        parser.error("class_codes must contain unique raw codes from 0 to 999")
    if not bands or any(type(band) is not int or band < 1 for band in bands):
        parser.error("bands must contain 1-based raster band indexes")
    if not schema.get("model_version") or not schema.get("accuracy_note"):
        parser.error("schema requires model_version and an honest accuracy_note")
    mean = np.asarray(schema["mean"], dtype=np.float32)[:, None, None]
    std = np.asarray(schema["std"], dtype=np.float32)[:, None, None]
    if len(mean) != len(bands) or len(std) != len(bands) or np.any(std <= 0):
        parser.error("mean/std must match the trained model's input bands; std must be positive")
    if args.backend == "joblib":
        import joblib
        model = joblib.load(args.model)  # Only load a trusted, locally trained artifact.
        if not hasattr(model, "predict_proba") or not set(model.classes_).issubset(set(codes)):
            parser.error("classifier must expose predict_proba and raw class codes from the schema")
    else:
        import torch
        model = torch.jit.load(str(args.model), map_location=args.device).eval()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(args.input) as source, tempfile.TemporaryDirectory(prefix="savegeo-lulc-", dir=args.output.parent) as directory:
        if source.crs is None or max(bands) > source.count:
            parser.error("input must have a CRS and all configured bands")
        profile = source.profile.copy()
        profile.update(driver="GTiff", count=2, dtype="float32", nodata=-9999, tiled=True, blockxsize=256, blockysize=256, compress="DEFLATE")
        temporary = Path(directory) / "prediction.tif"
        with rasterio.open(temporary, "w", **profile) as output:
            output.set_band_description(1, "landcover")
            output.set_band_description(2, "confidence")
            output.update_tags(model_version=schema["model_version"], source_year=args.year, accuracy_note=schema["accuracy_note"])
            for row in range(0, source.height, args.tile_size):
                for column in range(0, source.width, args.tile_size):
                    height, width = min(args.tile_size, source.height - row), min(args.tile_size, source.width - column)
                    halo = args.halo if args.backend == "torchscript" else 0
                    size = args.tile_size + 2 * halo
                    window = Window(column - halo, row - halo, size, size)
                    raw = source.read(bands, window=window, boundless=True, masked=True, out_dtype="float32")
                    valid = ~np.ma.getmaskarray(raw).any(axis=0) & np.isfinite(raw.filled(np.nan)).all(axis=0)
                    features = (raw.filled(0) * float(schema.get("scale", 1)) - mean) / std
                    features[:, ~valid] = 0
                    labels = np.full((size, size), -9999, dtype=np.float32)
                    confidence = np.full_like(labels, -9999)
                    if args.backend == "joblib":
                        if valid.any():
                            table = features[:, valid].T
                            labels[valid] = model.predict(table)
                            confidence[valid] = model.predict_proba(table).max(axis=1)
                    else:
                        with torch.inference_mode():
                            logits = model(torch.from_numpy(features[None]).to(args.device))
                            if logits.shape != (1, len(codes), size, size):
                                raise ValueError("Model must return N,K,H,W logits at the input resolution; export an upsampling wrapper if needed")
                            probabilities = logits.softmax(dim=1)
                            votes, indices = probabilities.max(dim=1)
                            labels[valid] = codes[indices[0].cpu().numpy()[valid]]
                            confidence[valid] = votes[0].cpu().numpy()[valid]
                    core = (slice(halo, halo + height), slice(halo, halo + width))
                    output.write(np.stack([labels[core], confidence[core]]), window=Window(column, row, width, height))
        raster_copy(temporary, args.output, driver="COG", compress="DEFLATE", overview_resampling="NEAREST")
    provenance = {"model_version": schema["model_version"], "year": args.year, "backend": args.backend,
                  "model_sha256": digest(args.model), "schema_sha256": digest(args.schema),
                  "input": str(args.input), "class_codes": codes.tolist(), "bands": ["landcover", "confidence"],
                  "accuracy_note": schema["accuracy_note"], "confidence_calibrated": False}
    args.output.with_suffix(".json").write_text(json.dumps(provenance, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(args.output), "model_version": schema["model_version"], "year": args.year}))


if __name__ == "__main__":
    main()

"""Private entry point for bounded native work. Never accept client-selected code."""
from __future__ import annotations
import json
import os
from pathlib import Path
import sys


def main():
    spec = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    if os.name == "posix":
        import resource
        memory = max(256, min(8192, int(spec["memory_mib"]))) * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
        resource.setrlimit(resource.RLIMIT_CPU, (120, 120))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        resource.setrlimit(resource.RLIMIT_NOFILE, (128, 128))
        resource.setrlimit(resource.RLIMIT_FSIZE, (4 * 1024**3, 4 * 1024**3))
    kind, payload = spec["kind"], spec["payload"]
    if kind in {"onnx-inspect", "onnx-predict"}:
        import numpy as np
        import onnxruntime as ort
        from app.inference.safe_model import validate_onnx
        raw = Path(payload["model"]).read_bytes()
        validate_onnx(raw)
        options = ort.SessionOptions()
        options.intra_op_num_threads, options.inter_op_num_threads = 1, 1
        session = ort.InferenceSession(raw, sess_options=options, providers=["CPUExecutionProvider"])
        inputs = session.get_inputs()
        if len(inputs) != 1 or inputs[0].type not in {"tensor(float)", "tensor(double)"}:
            raise ValueError("Invalid numeric feature input")
        result = {"dtype": "float64" if inputs[0].type == "tensor(double)" else "float32"}
        if kind == "onnx-predict":
            X = np.load(payload["input"], allow_pickle=False)
            if X.ndim != 2 or X.size > 20_000_000 or not np.isfinite(X).all():
                raise ValueError("Invalid feature matrix")
            y = np.asarray(session.run(None, {inputs[0].name: X})[0]).reshape(-1)
            if y.size != X.shape[0] or not np.isfinite(y).all():
                raise ValueError("Invalid prediction")
            np.save(payload["output"], y, allow_pickle=False)
    elif kind == "cog":
        import rasterio
        from rasterio.shutil import copy
        # Only local files or one validated ZIP member are accepted. Disable
        # GDAL network drivers, sidecar probing and external VRT references.
        source = payload["source"]
        if source.startswith("/vsizip/"):
            if not source.lower().endswith((".tif", ".tiff")):
                raise ValueError("Invalid archive raster")
        elif not Path(source).is_file():
            raise ValueError("Raster source is not local")
        with rasterio.Env(GDAL_SKIP="VRT HTTP WMS WCS", GDAL_PAM_ENABLED="NO", GDAL_CACHEMAX=128 * 1024**2):
            with rasterio.open(source, driver="GTiff") as src:
                if src.width * src.height * src.count > 2_000_000_000 or src.count > 32:
                    raise ValueError("Raster dimensions exceed budget")
                copy(src, payload["destination"], driver="COG", compress="DEFLATE", blocksize=512,
                    overview_resampling=payload["resampling"], bigtiff="IF_SAFER")
        result = {"converted": True}
    else:
        raise ValueError("Unknown native task")
    Path(sys.argv[2]).write_text(json.dumps(result), encoding="utf-8")


if __name__ == "__main__":
    main()

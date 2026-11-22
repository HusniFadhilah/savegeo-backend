"""Private entry point for bounded native work. Never accept client-selected code."""
from __future__ import annotations
import json
import os
from pathlib import Path
import sys
from typing import Any
import zipfile

from app.core.shapefile_limits import SHP_MAX_MEMBER_BYTES


def inspect_shapefile_archive(source_path: str) -> dict:
    """Parse one bounded shapefile archive without extracting member paths."""
    import io
    import stat

    import shapefile

    max_entries = 1000
    max_member_bytes = SHP_MAX_MEMBER_BYTES
    max_total_bytes = 300 * 1024 * 1024
    max_features = 100_000
    source = Path(source_path)
    with zipfile.ZipFile(source) as archive:
        members = archive.infolist()
        if len(members) > max_entries:
            raise ValueError("Too many archive entries")
        total_size = 0
        for member in members:
            name = member.filename.replace("\\", "/")
            if name.startswith("/") or ".." in Path(name).parts or "\x00" in name or ":" in name:
                raise ValueError("Unsafe archive member path")
            mode = member.external_attr >> 16
            member_type = stat.S_IFMT(mode)
            if member_type == stat.S_IFLNK or member_type not in (0, stat.S_IFREG, stat.S_IFDIR):
                raise ValueError("Unsafe archive member type")
            total_size += member.file_size
            if total_size > max_total_bytes:
                raise ValueError("Archive expands beyond the permitted size")

        normalized_names = [member.filename.replace("\\", "/").casefold() for member in members]
        if len(normalized_names) != len(set(normalized_names)):
            raise ValueError("Archive contains duplicate member names")
        shapefiles = [m.filename for m in members if m.filename.lower().endswith(".shp")]
        if len(shapefiles) != 1:
            raise ValueError("Archive must contain exactly one shapefile")
        shp_name = shapefiles[0]
        base = shp_name.replace("\\", "/")[:-4]
        wanted = {f"{base}{ext}".casefold(): ext for ext in (".shp", ".dbf", ".shx")}
        by_name = {m.filename.replace("\\", "/").casefold(): m for m in members}
        selected = {ext: by_name.get(name) for name, ext in wanted.items()}
        for _ext, selected_member in selected.items():
            if selected_member is not None and selected_member.file_size > max_member_bytes:
                raise ValueError("Shapefile member exceeds the permitted size")
        if selected[".shp"] is None:
            raise ValueError("Shapefile data is missing")

        buffers = {}
        for ext, selected_member in selected.items():
            if selected_member is not None:
                with archive.open(selected_member) as stream:
                    buffers[ext] = io.BytesIO(stream.read(max_member_bytes + 1))
                if len(buffers[ext].getbuffer()) > max_member_bytes:
                    raise ValueError("Shapefile member exceeds the permitted size")

    reader = shapefile.Reader(shp=buffers[".shp"], dbf=buffers.get(".dbf"), shx=buffers.get(".shx"))
    try:
        fields = [field[0] for field in reader.fields[1:]]
        features: list[dict[str, Any]] = []
        for shape_record in reader.iterShapeRecords():
            if len(features) >= max_features:
                raise ValueError("Shapefile exceeds the feature limit")
            properties = dict(zip(fields, shape_record.record))
            properties = {key: value.decode("utf-8", "replace") if isinstance(value, bytes) else value
                for key, value in properties.items()}
            features.append({"type": "Feature", "geometry": shape_record.shape.__geo_interface__, "properties": properties})
    finally:
        reader.close()
    if not features:
        raise ValueError("Shapefile contains no features")
    return {"geojson": {"type": "FeatureCollection", "features": features},
        "feature_count": len(features), "name": Path(base).name}


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
    elif kind == "shapefile":
        result = inspect_shapefile_archive(payload["source"])
    else:
        raise ValueError("Unknown native task")
    Path(sys.argv[2]).write_text(json.dumps(result, default=str), encoding="utf-8")


if __name__ == "__main__":
    main()

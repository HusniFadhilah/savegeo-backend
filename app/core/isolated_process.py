"""Run native parsers and inference in disposable, resource-limited processes."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from threading import BoundedSemaphore

_slots = BoundedSemaphore(2)
_root = Path(__file__).resolve().parents[2]


def run_isolated(kind: str, payload: dict, *, timeout: float = 120, memory_mib: int = 4096) -> dict:
    if kind not in {"onnx-inspect", "onnx-predict", "cog"}:
        raise ValueError("Unsupported isolated workload")
    if not _slots.acquire(blocking=False):
        raise ValueError("Native processing capacity is busy; retry later")
    try:
        with tempfile.TemporaryDirectory(prefix="savegeo-native-") as directory:
            spec = Path(directory) / "request.json"
            output = Path(directory) / "result.json"
            spec.write_text(json.dumps({"kind": kind, "payload": payload, "memory_mib": memory_mib}), encoding="utf-8")
            # Provider credentials, database passwords, HTTP proxies and netrc
            # configuration are not inherited by the native parser process.
            env = {key: value for key, value in os.environ.items()
                if key.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "LD_LIBRARY_PATH"}}
            env.update(PYTHONPATH=str(_root), OPENBLAS_NUM_THREADS="1", OMP_NUM_THREADS="1",
                GDAL_CACHEMAX="128", GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR",
                GDAL_PAM_ENABLED="NO", CPL_VSIL_CURL_ALLOWED_EXTENSIONS="", GDAL_NUM_THREADS="1")
            try:
                process = subprocess.run([sys.executable, "-m", "app.workers.native_task", str(spec), str(output)],
                    cwd=directory, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, timeout=timeout, check=False,
                    creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)) if os.name == "nt" else 0)
            except subprocess.TimeoutExpired as exc:
                raise ValueError("Native processing exceeded the time budget") from exc
            if process.returncode or not output.is_file() or output.stat().st_size > 65536:
                raise ValueError("Native processing failed or exceeded its resource budget")
            return json.loads(output.read_text(encoding="utf-8"))
    finally:
        _slots.release()

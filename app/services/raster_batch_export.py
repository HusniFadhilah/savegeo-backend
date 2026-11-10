"""Full-resolution asynchronous COG exports, without coarsening the requested grid."""

import re
import uuid
from datetime import UTC, datetime, timedelta

import ee
import jwt

from app.core.config import get_settings
from app.services.gee_common import AnalysisError


def start_export(image, roi, filename, scale, year, source_year, owner):
    if not owner:
        raise AnalysisError("Akun pemilik ekspor diperlukan.", 403)
    settings = get_settings()
    bucket = settings.gee_export_bucket.strip()
    if not bucket:
        raise AnalysisError(
            "Ekspor resolusi penuh memerlukan GEE_EXPORT_BUCKET pada server dan izin tulis identitas Earth Engine ke bucket tersebut. Unduhan langsung tetap tersedia dengan resolusi aman.",
            503,
        )
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{1,220}[a-z0-9]", bucket):
        raise AnalysisError("Konfigurasi bucket ekspor tidak valid.", 503)
    prefix = f"savegeo/{uuid.uuid4().hex}/{filename}"
    task = ee.batch.Export.image.toCloudStorage(
        image=image.clip(roi).toFloat(),
        description=filename[:90],
        bucket=bucket,
        fileNamePrefix=prefix,
        region=roi,
        scale=scale,
        crs="EPSG:6933",
        maxPixels=1e13,
        fileDimensions=8192,
        shardSize=256,
        skipEmptyTiles=True,
        fileFormat="GeoTIFF",
        formatOptions={"cloudOptimized": True},
    )
    task.start()
    now = datetime.now(UTC)
    token = jwt.encode(
        {
            "task_id": task.id,
            "prefix": prefix,
            "bucket": bucket,
            "purpose": "raster-export-status",
            "owner": owner,
            "iat": now,
            "exp": now + timedelta(days=30),
        },
        settings.jwt_secret_key,
        algorithm="HS256",
    )
    return {
        "status": "submitted",
        "task_id": task.id,
        "task_token": token,
        "filename": f"{filename}.tif",
        "scale": scale,
        "requested_scale": scale,
        "scale_adjusted": False,
        "year": year,
        "effective_year": source_year,
        "destination": f"gs://{bucket}/{prefix}",
        "note": "Ekspor COG dibagi menjadi berkas maksimal 8192 x 8192 piksel, pada resolusi yang dipilih. Unduh seluruh bagian dari bucket setelah status COMPLETED.",
    }


def export_status(token, owner):
    try:
        claims = jwt.decode(token, get_settings().jwt_secret_key, algorithms=["HS256"])
        if claims.get("purpose") != "raster-export-status" or claims.get("owner") != owner:
            raise ValueError("purpose")
        status = ee.data.getTaskStatus(claims["task_id"])[0]
        return {
            "task_id": claims["task_id"],
            "state": status.get("state"),
            "error": status.get("error_message"),
            "destination": f"gs://{claims['bucket']}/{claims['prefix']}",
            "destination_uris": status.get("destination_uris", []),
        }
    except (jwt.PyJWTError, KeyError, ValueError) as exc:
        raise AnalysisError("Token status ekspor tidak valid atau sudah kedaluwarsa.", 403) from exc

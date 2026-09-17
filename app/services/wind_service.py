"""Small Open-Meteo adapter used by the wildfire map wind-direction layer."""
from __future__ import annotations

import math
from datetime import date

import requests

WIND_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
WIND_ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
MAX_BBOX_SPAN = 60.0
GRID_SIZE = 5


class WindRequestError(Exception):
    def __init__(self, message: str, status_code: int = 502):
        super().__init__(message)
        self.status_code = status_code


def _validate_bbox(bbox: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    west, south, east, north = bbox
    if not all(math.isfinite(value) for value in bbox):
        raise WindRequestError("Bounding box angin tidak valid", 400)
    if not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
        raise WindRequestError("Bounding box angin berada di luar koordinat dunia", 400)
    if east - west > MAX_BBOX_SPAN or north - south > MAX_BBOX_SPAN:
        raise WindRequestError("Area layer angin terlalu luas", 400)
    return bbox


def _grid(bbox: tuple[float, float, float, float]) -> list[tuple[float, float]]:
    west, south, east, north = bbox
    return [
        (
            round(south + (north - south) * row / (GRID_SIZE - 1), 4),
            round(west + (east - west) * column / (GRID_SIZE - 1), 4),
        )
        for row in range(GRID_SIZE)
        for column in range(GRID_SIZE)
    ]


def _as_locations(payload):
    return payload if isinstance(payload, list) else [payload]


def get_wind(bbox: tuple[float, float, float, float], requested_date: str | None = None) -> dict:
    bbox = _validate_bbox(bbox)
    target = date.fromisoformat(requested_date) if requested_date else date.today()
    endpoint = WIND_ARCHIVE_URL if target < date.today() else WIND_FORECAST_URL
    points = _grid(bbox)
    params = {
        "latitude": ",".join(str(lat) for lat, _ in points),
        "longitude": ",".join(str(lon) for _, lon in points),
        "hourly": "wind_speed_10m,wind_direction_10m",
        "start_date": target.isoformat(),
        "end_date": target.isoformat(),
        "timezone": "UTC",
        "wind_speed_unit": "kmh",
    }
    try:
        response = requests.get(endpoint, params=params, timeout=20)
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise WindRequestError("Data arah angin belum tersedia dari Open-Meteo") from exc

    locations = _as_locations(payload)
    if len(locations) != len(points):
        raise WindRequestError("Respons grid angin tidak lengkap")

    output = []
    valid_time = None
    for (lat, lon), location in zip(points, locations):
        hourly = location.get("hourly") or {}
        times = hourly.get("time") or []
        speeds = hourly.get("wind_speed_10m") or []
        directions = hourly.get("wind_direction_10m") or []
        if not times or not speeds or not directions:
            continue
        noon_indices = [index for index, value in enumerate(times) if str(value).endswith("T12:00")]
        index = noon_indices[0] if noon_indices else min(12, len(times) - 1)
        speed = speeds[index] if index < len(speeds) else None
        direction = directions[index] if index < len(directions) else None
        if speed is None or direction is None:
            continue
        valid_time = str(times[index])
        output.append({
            "lat": lat,
            "lon": lon,
            "speed_kmh": round(float(speed), 1),
            "direction_deg": round(float(direction), 1),
        })

    return {
        "source": "Open-Meteo",
        "date": target.isoformat(),
        "valid_time_utc": valid_time,
        "points": output,
        "note": "Arah angin 10 m pada sekitar 12:00 UTC; bukan pengamatan hotspot.",
    }

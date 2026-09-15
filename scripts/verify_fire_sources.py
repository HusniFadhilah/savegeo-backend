"""Read-only smoke checks; no secrets, writes, or Earth Engine jobs."""

import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.services import fire_multi_source_service as fire


def main():
    checks = {
        "inarisk": fire._fetch_inarisk,
        "big": lambda: fire.load_big_boundaries({"province": "KALIMANTAN SELATAN"}),
        "bmkg": lambda: fire._fetch_bmkg([114.4, -3.6, 115, -3], date(2026, 9, 1), date(2026, 9, 13)),
        "cdse": lambda: fire._fetch_cdse([114.4, -3.6, 115, -3], date(2026, 9, 1), date(2026, 9, 13)),
    }
    for name, check in checks.items():
        try:
            result = check()
            print(
                json.dumps(
                    {
                        "source": name,
                        "status": result.get("status", "ok"),
                        "features": len(result.get("features", [])),
                        "wms_layers": result.get("wms_layers"),
                        "sample": (result.get("features") or [{}])[0].get("properties"),
                    },
                    default=str,
                )
            )
        except Exception as exc:
            print(json.dumps({"source": name, "error_type": type(exc).__name__}))


if __name__ == "__main__":
    main()

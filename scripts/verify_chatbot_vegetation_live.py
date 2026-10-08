"""Run a real Bali vegetation service check using existing backend credentials.

This exercises the same service as the API, without changing browser sessions,
users, feature permissions, or application configuration. It does not prove the
chatbot/browser flow. Run from backend with network access to GEE and SP3STAB.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import logging
import math
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db.session import SessionLocal
from app.services import gee_service, vegetation_service
from app.services.boundary_provider import RegionBoundaryProvider
from app.services.gee_common import create_geometry_from_payload


def progress(stage: str, **values) -> None:
    print(json.dumps({'stage': stage, **values}), flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--year', required=True, type=int)
    parser.add_argument('--start-month', type=int, default=6)
    parser.add_argument('--end-month', type=int, default=9)
    parser.add_argument('--cloud-threshold', type=int, default=10)
    parser.add_argument('--scale', type=int, default=10)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    # The existing initializer logs credential identities; keep this report free
    # of account identifiers and keys. The application handles its own logging.
    logging.disable(logging.CRITICAL)
    report = {'timestamp': datetime.now(timezone.utc).isoformat(),
              'scope': 'real backend service integration; browser/chatbot not exercised',
              'status': 'running'}
    try:
        provider = RegionBoundaryProvider(timeout=30)
        options = provider.dropdown('province')
        matches = [(name, code) for name, code in options.items() if name.strip().upper() == 'BALI']
        if len(matches) != 1:
            raise RuntimeError('Bali province could not be identified uniquely')
        name, code = matches[0]
        geometry = provider.geometry('province', str(code))
        report['aoi'] = {'name': name, 'code': str(code), 'source': 'configured RegionBoundaryProvider',
                         'geometry_type': geometry['type']}
        progress('boundary', **report['aoi'])
        indices = ['NDVI', 'NDWI', 'MNDWI', 'NDBI']
        payload = {'aoi': {'geojson': geometry}, 'year': args.year, 'start_month': args.start_month,
                   'end_month': args.end_month, 'cloud_threshold': args.cloud_threshold,
                   'satellite': 'sentinel2', 'indices': indices, 'scale': args.scale,
                   'classify': True, 'histogram': True}
        report['parameters'] = {key: value for key, value in payload.items() if key != 'aoi'}
        with SessionLocal() as db:
            if not gee_service.initialize_ee(db):
                raise RuntimeError('Earth Engine initialization failed')
            import ee
            ee.data.setDeadline(180000)
            report['aoi']['area_ha'] = create_geometry_from_payload(payload['aoi']).area(1).divide(10000).getInfo()
            progress('analysis_started', **report['parameters'], area_ha=report['aoi']['area_ha'])
            result = vegetation_service.analyze_vegetation(db, payload)
        report['result'] = {key: result.get(key) for key in
                            ('collection_size', 'date_range', 'cloud_threshold', 'scale',
                             'data_quality', 'cloud_mask_technique', 'skipped_indices')}
        report['result']['indices'] = {}
        for index in indices:
            item = result.get('indices', {}).get(index, {})
            if not isinstance(item.get('mean'), (int, float)) or not math.isfinite(item['mean']):
                raise RuntimeError(f'{index} did not return a valid mean')
            report['result']['indices'][index] = {key: item.get(key) for key in
                                                 ('mean', 'min', 'max', 'std_dev',
                                                  'native_scale_m', 'analysis_scale_m', 'narrative')}
            report['result']['indices'][index]['has_map_tile'] = bool(item.get('tile_url'))
            report['result']['indices'][index]['has_classification'] = item.get('classification') is not None
            report['result']['indices'][index]['has_histogram'] = bool(item.get('histogram'))
        report['status'] = 'passed'
        progress('analysis_complete', result=report['result'])
    except Exception as exc:
        report['status'] = 'failed'
        # Keep raw provider/credential error text out of the shareable report.
        report['error_type'] = type(exc).__name__
        report['error'] = str(exc) if isinstance(exc, RuntimeError) else 'Live provider/service request failed; inspect backend logs.'
        progress('failed', error_type=report['error_type'], error=report['error'])
    finally:
        args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding='utf-8')
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())

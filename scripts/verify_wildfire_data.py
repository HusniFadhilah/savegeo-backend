"""Verify persisted wildfire observations, admin boundaries and archived CAMS rasters."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import numpy as np
import rasterio
from fastapi import Response
from PIL import Image
from sqlalchemy import select

# Also supports the temporary /app/var copy used by the operations workflow.
sys.path.insert(0, str(Path.cwd()))

from app.api.routes import wildfire_events as route  # noqa: E402
from app.db.models.disaster_event import DisasterEvent  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.services import cams_archive_service as cams, wildfire_hotspot_service as hotspots  # noqa: E402
from app.services.wildfire_geometry import is_feature_in_event_scope  # noqa: E402
from app.services.wildfire_catalog import EVENT_ISLANDS  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--slug', required=True, choices=sorted(EVENT_ISLANDS))
    parser.add_argument('--from', dest='start', type=date.fromisoformat, required=True)
    parser.add_argument('--to', dest='end', type=date.fromisoformat, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--publish', action='store_true', help='Publish a prepared draft only after all verification succeeds')
    args = parser.parse_args()
    if args.end < args.start:
        parser.error('--to must not be earlier than --from')
    report: dict = {}

    def record(key: str, value: object) -> None:
        report[key] = value
        print(json.dumps({key: value}, default=str), flush=True)

    with SessionLocal() as db:
        event = db.execute(select(DisasterEvent).where(DisasterEvent.slug == args.slug)).scalar_one()
        if not event.hotspot_last_synced_at:
            raise RuntimeError('Completed FIRMS synchronization is required')
        if event.status != 'published':
            if not args.publish or event.status not in {'draft', 'ready_for_review'}:
                raise RuntimeError('Event is not eligible for verified publication')
            # Visible only inside this transaction until the final commit. A
            # failed boundary/raster/API check rolls the draft status back.
            event.status = 'published'
            event.is_public = True
            event.published_at = datetime.now(UTC)
            db.flush()
        data = route.get_wildfire_event(args.slug, viewer=None, db=db)['event']
        record('event', {key: data.get(key) for key in [
            'slug', 'status', 'province_codes', 'provinces', 'bbox', 'published_at',
            'updated_at', 'last_data_at', 'last_synced_at',
            'official_reports', 'boundary_source', 'limitations',
        ]})
        summary = hotspots.summarize_event(db, event.id, args.start, args.end)
        if not summary['total_hotspots']:
            raise RuntimeError('No persisted NASA FIRMS observations in the requested period')
        record('persisted_summary', summary)
        map_start = max(args.start, args.end - timedelta(days=6)).isoformat()
        payload = route.get_wildfire_hotspots(
            args.slug, from_date=map_start, to=args.end.isoformat(), sensor=None,
            confidence=None, region_level=None, region_code=None,
            include_features=True, viewer=None, db=db,
        )
        if len(payload['features']) > route.MAX_MAP_FEATURES or not payload['features']:
            raise RuntimeError('Hotspot map payload is missing or exceeds its rendering limit')
        if not all(is_feature_in_event_scope(event, feature) for feature in payload['features']):
            raise RuntimeError('Hotspot map contains observations outside the island')
        record('hotspot_map', {'metadata': payload['metadata'], 'from': map_start, 'to': args.end.isoformat()})
        island = EVENT_ISLANDS[args.slug]
        codes = set(event.province_codes)
        province_payload = route.get_wildfire_regions(args.slug, viewer=None, db=db)['provinces']
        options = {item['name']: item['code'] for item in province_payload}
        if set(options.values()) != codes:
            raise RuntimeError('Province dropdown does not match the event coverage')
        record('province_options', options)
        stats = route.get_wildfire_region_statistics(
            args.slug, level='province', parent_code=None, from_date=map_start,
            to=args.end.isoformat(), sensor=None, confidence=None, viewer=None, db=db,
        )
        if {region['code'] for region in stats['regions']} != codes:
            raise RuntimeError('Province geometries or statistics are incomplete')
        record('province_statistics', stats)
        route.get_wildfire_emissions_archive(args.slug, response=Response(), scope='event', viewer=None, db=db)
        manifest = cams.read_archive(args.slug)
        for layer_id in ['burnt_carbon', 'organic_aod']:
            frames = manifest['layers'][layer_id]['frames']
            if not frames:
                raise RuntimeError('CAMS layer has no archived frames')
            for frame in frames:
                path = cams.event_directory(args.slug) / frame['filename']
                if hashlib.sha256(path.read_bytes()).hexdigest() != frame['sha256']:
                    raise RuntimeError('Archived raster checksum mismatch')
                if {region['code'] for region in frame['regions']} != codes:
                    raise RuntimeError('CAMS province coverage is incomplete')
            observed = max((frame for frame in frames if not frame.get('is_forecast')),
                           key=lambda f: (f['maximum_display_value'], f['valid_at']))
            with rasterio.open(cams.event_directory(args.slug) / observed['filename']) as source:
                values = source.read(1, masked=True)
                index = np.unravel_index(np.ma.argmax(values), values.shape)
                lon, lat = source.xy(*index)
                native = float(values[index])
            point = route.get_wildfire_emissions_point(
                args.slug, layer_id, observed['id'], response=Response(),
                lat=lat, lon=lon, scope='event', viewer=None, db=db,
            )
            z = 6
            x = int((lon + 180) / 360 * 2**z)
            y = int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * 2**z)
            tile = route.get_wildfire_emissions_tile(
                args.slug, layer_id, observed['id'], z, x, y, scope='event', viewer=None, db=db,
            )
            pixels = np.array(Image.open(io.BytesIO(tile.body)).convert('RGBA'))
            visible = int(np.count_nonzero(pixels[:, :, 3]))
            expected = native * cams.LAYER_DEFINITIONS[layer_id]['multiplier']
            if (tile.status_code != 200 or not math.isfinite(native) or native < 0
                    or not point['data_available']
                    or not math.isclose(point['value'], expected, rel_tol=1e-5, abs_tol=1e-10)
                    or (native > 0 and not visible)):
                raise RuntimeError('CAMS tile is missing real raster data')
            record(layer_id, {
                'frames': len(frames), 'from': frames[0]['valid_at'], 'to': frames[-1]['valid_at'],
                'forecast_frames': sum(bool(frame.get('is_forecast')) for frame in frames),
                'verified_raster_checksums': len(frames), 'point_sample': point,
                'sampled_frame': observed['valid_at'], 'data_state': 'positive' if native > 0 else 'valid_zero',
                'missing_requested_days': manifest['layers'][layer_id].get('missing_requested_days', []),
                'tile': {'status': tile.status_code, 'content_type': tile.media_type,
                         'visible_pixels': visible, 'z': z, 'x': x, 'y': y},
            })
        world = route.get_wildfire_emissions_archive(
            args.slug, response=Response(), scope='world', viewer=None, db=db,
        )
        frame = world['layers']['organic_aod']['frames'][-1]
        point = route.get_wildfire_emissions_point(
            args.slug, 'organic_aod', frame['id'], response=Response(),
            lat=-2.0, lon=113.4 if island == 'Sumatra' else 104.0,
            scope='world', viewer=None, db=db,
        )
        if not point['data_available']:
            raise RuntimeError('World aerosol data is unavailable outside the event')
        record('world_aerosol', {'bbox': world['bbox'], 'frames': len(world['layers']['organic_aod']['frames']),
                                 'point_outside_event': point})
        if args.publish:
            db.commit()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, default=str), encoding='utf-8')


if __name__ == '__main__':
    main()

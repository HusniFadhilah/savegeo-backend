"""Aggregates all domain routers under the /api prefix."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.routes import (
    admin,
    admin_disaster,
    agent,
    analysis_jobs,
    arcgis,
    carbon,
    carbon_calibration,
    chat,
    chloris,
    companies,
    crop_monitoring,
    datasets,
    disaster,
    disaster_events,
    wildfire_events,
    download,
    fields,
    health,
    imagery,
    imagery_tiles,
    geospatial,
    landcover,
    maps,
    models,
    regions,
    reports,
    timeseries,
    user_auth,
    utils,
    vegetation,
    workflows,
)

api_router = APIRouter()

api_router.include_router(health.router)
api_router.include_router(datasets.router)
api_router.include_router(maps.router)
api_router.include_router(models.router)
api_router.include_router(vegetation.router)
api_router.include_router(regions.router)
api_router.include_router(companies.router)
api_router.include_router(chat.router)
api_router.include_router(utils.router)
api_router.include_router(reports.router)
api_router.include_router(admin.router)
api_router.include_router(carbon.router)
api_router.include_router(carbon_calibration.router)
api_router.include_router(carbon_calibration.public_router)
api_router.include_router(chloris.router)
api_router.include_router(landcover.router)
api_router.include_router(disaster.router)
api_router.include_router(wildfire_events.router)
api_router.include_router(disaster_events.router)
api_router.include_router(admin_disaster.router)
api_router.include_router(user_auth.router)
api_router.include_router(timeseries.router)
api_router.include_router(download.router)
api_router.include_router(arcgis.router)
api_router.include_router(agent.router)
api_router.include_router(analysis_jobs.router)
api_router.include_router(fields.router)
api_router.include_router(crop_monitoring.router)
api_router.include_router(imagery.router)
api_router.include_router(imagery_tiles.router)
api_router.include_router(geospatial.router)
api_router.include_router(workflows.router)

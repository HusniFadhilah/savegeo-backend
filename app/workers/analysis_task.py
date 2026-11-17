"""One analysis attempt; the parent worker enforces its lifetime and lease."""
import os
import sys


def main():
    if os.name == "posix":
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (8 * 1024**3, 8 * 1024**3))
        resource.setrlimit(resource.RLIMIT_CPU, (600, 600))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    from app.db.session import SessionLocal
    from app.db.models.analysis_job import AnalysisJob
    from app.services.analysis_requirements import requires_earth_engine
    from app.services.arcgis_service import configure_arcgis_from_db
    from app.services.gee_service import initialize_ee
    from app.api.routes.analysis_jobs import _run_job
    with SessionLocal() as db:
        job = db.get(AnalysisJob, sys.argv[1])
        if job is None or job.status != "running" or job.lease_owner != os.environ["SAVEGEO_ANALYSIS_LEASE"]:
            raise RuntimeError("Analysis lease is not owned")
        configure_arcgis_from_db(db)
        if requires_earth_engine(job.kind, job.payload) and not initialize_ee(db):
            raise RuntimeError("Earth Engine is unavailable")
        _run_job(job.id, job.kind, job.payload, job.cache_key, job.owner)


if __name__ == "__main__":
    main()

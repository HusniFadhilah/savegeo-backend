"""Public company-boundary read endpoints - /api/companies*. Ported from app.py.

Admin CRUD for company boundaries (create/update/delete, OSM/GFW import) was not
in the required endpoint contract for this migration pass and is not included -
see migration summary.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import or_
from sqlalchemy.orm import Session, defer

from app.core.viewer_features import require_feature
from app.db.models.company_boundary import CompanyBoundary
from app.db.session import get_db

router = APIRouter(
    tags=["companies"],
    dependencies=[Depends(require_feature("app.feature.carbon_stock"))],
)


@router.get("/companies")
def list_companies(
    industry_type: str | None = None,
    province: str | None = None,
    search: str | None = Query(default=None, max_length=200),
    provinces: list[str] = Query(default=[]),
    offset: int = Query(default=0, ge=0, le=100000),
    limit: int = Query(default=100, ge=1, le=200),
    db: Session = Depends(get_db),
):
    query = db.query(CompanyBoundary).filter_by(is_active=True)
    if industry_type:
        query = query.filter_by(industry_type=industry_type)
    if province:
        query = query.filter_by(province=province)
    if provinces:
        if len(provinces) > 100 or any(len(p) > 100 for p in provinces):
            raise HTTPException(422, "Too many province filters")
        query = query.filter(CompanyBoundary.province.in_(provinces))
    if search:
        query = query.filter(or_(CompanyBoundary.name.icontains(search.strip(), autoescape=True),
            CompanyBoundary.company_name.icontains(search.strip(), autoescape=True)))
    total = query.count()
    companies = query.options(defer(CompanyBoundary.geojson)).order_by(CompanyBoundary.name, CompanyBoundary.id).offset(offset).limit(limit).all()
    return {"companies": [c.to_dict() for c in companies], "count": len(companies), "total": total,
        "offset": offset, "limit": limit, "has_more": offset + len(companies) < total}


@router.get("/companies/provinces")
def company_provinces(db: Session = Depends(get_db)):
    rows = db.query(CompanyBoundary.province).filter(CompanyBoundary.is_active.is_(True),
        CompanyBoundary.province.is_not(None)).distinct().order_by(CompanyBoundary.province).limit(1000).all()
    return {"provinces": [r[0] for r in rows if r[0]]}


@router.get("/companies/{cid}/geojson")
def company_geojson(cid: int, db: Session = Depends(get_db)):
    company = db.get(CompanyBoundary, cid)
    if not company or not company.is_active:
        raise HTTPException(status_code=404, detail="Company tidak aktif atau tidak ditemukan")
    return {
        "id": company.id,
        "name": company.name,
        "industry_type": company.industry_type,
        "area_ha": company.area_ha,
        "geojson": company.geojson,
    }

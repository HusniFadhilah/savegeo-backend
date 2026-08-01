"""Public company-boundary read endpoints - /api/companies*. Ported from app.py.

Admin CRUD for company boundaries (create/update/delete, OSM/GFW import) was not
in the required endpoint contract for this migration pass and is not included -
see migration summary.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db.models.company_boundary import CompanyBoundary
from app.db.session import get_db

router = APIRouter(tags=["companies"])


@router.get("/companies")
def list_companies(
    industry_type: Optional[str] = None,
    province: Optional[str] = None,
    search: Optional[str] = None,
    db: Session = Depends(get_db),
):
    query = db.query(CompanyBoundary).filter_by(is_active=True)
    if industry_type:
        query = query.filter_by(industry_type=industry_type)
    if province:
        query = query.filter_by(province=province)
    if search:
        query = query.filter(CompanyBoundary.name.ilike(f"%{search.strip()}%"))
    companies = query.order_by(CompanyBoundary.name).all()
    return {"companies": [c.to_dict() for c in companies], "count": len(companies)}


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

"""Admin analytics (phase 6): GET /api/admin/analytics/{panel}?start=&end=,
for the platform admin only. Like the rest of /api/admin, everyone else gets a
404. The panels and their numbers are in app/analytics.py."""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException

from .. import analytics, rbac

router = APIRouter(prefix="/api/admin/analytics", dependencies=[Depends(rbac.authorize)])


@router.get("/{panel}")
def analytics_panel(panel: str, start: date | None = None, end: date | None = None) -> dict:
    """One panel's numbers for the days from start to end (both included;
    the last 30 days by default)."""
    if panel not in analytics.PANELS:
        raise HTTPException(404, "Not Found")
    try:
        r = analytics.date_range(start, end)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    return analytics.panel(panel, r)

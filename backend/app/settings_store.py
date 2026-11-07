"""Runtime, UI-editable settings persisted in the `app_settings` table.

These override the env-file defaults from config.py at runtime, so toggles like
"AI enrichment on/off" can be flipped from the dashboard without a restart. Only
a small allowlist of keys is editable.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.dialects import postgresql, sqlite

from .config import get_settings
from .db import is_postgres, session
from .db_models import AppSettingRow

_EDITABLE = {"use_enrichment_agent", "use_extraction_agent"}


def _overrides() -> dict:
    with session() as s:
        return {r.key: r.value for r in s.scalars(select(AppSettingRow))}


def get_runtime_settings() -> dict:
    env = get_settings()
    base = {
        "use_enrichment_agent": env.use_enrichment_agent,
        "use_extraction_agent": env.use_extraction_agent,
    }
    base.update({k: v for k, v in _overrides().items() if k in _EDITABLE})
    return base


def update_runtime_settings(patch: dict) -> dict:
    # INSERT … ON CONFLICT, not merge(): two saves of a not-yet-stored key
    # would otherwise both try to INSERT it.
    upsert = (postgresql if is_postgres() else sqlite).insert
    with session() as s:
        for k, v in patch.items():
            if k in _EDITABLE and v is not None:
                stmt = upsert(AppSettingRow).values(key=k, value=bool(v))
                s.execute(stmt.on_conflict_do_update(
                    index_elements=["key"], set_={"value": stmt.excluded.value}))
    return get_runtime_settings()

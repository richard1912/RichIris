"""Settings REST API for GUI-configurable system settings."""

import logging

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.services.settings import get_all_settings, update_settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/settings", tags=["settings"])


class SettingsUpdateRequest(BaseModel):
    """Partial update: {key: value} pairs where key is 'category.setting'."""
    settings: dict[str, str]


@router.get("")
async def get_settings(db: AsyncSession = Depends(get_db)):
    """Return all settings grouped by category."""
    return await get_all_settings(db)


@router.put("")
async def put_settings(body: SettingsUpdateRequest, db: AsyncSession = Depends(get_db)):
    """Update one or more settings. Returns updated settings + restart flag."""
    restart_needed = await update_settings(db, body.settings)

    # Reload the live config singleton with new values
    from app.config import reload_from_db
    from app.database import get_session_factory
    factory = get_session_factory()
    async with factory() as session:
        await reload_from_db(session)

    all_settings = await get_all_settings(db)
    return {
        "settings": all_settings,
        "restart_required": restart_needed,
    }

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from trade_calendar.core.database import get_session
from trade_calendar.models.domain import Event, EventChange
from trade_calendar.schemas.events import ChangeRead
from trade_calendar.translations import attach_translations

router = APIRouter(prefix="/changes", tags=["changes"])


@router.get("", response_model=list[ChangeRead])
async def list_changes(
    limit: int = Query(default=50, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
) -> list[ChangeRead]:
    # Soft-deleted events are filtered out here the same way they are in
    # calendar.py, notification.py, services.py, sync.py and translations.py.
    # Without it the feed advertised changes for events that no longer resolve:
    # the UI fetched each one, got a 404, and rendered a phantom "event updated"
    # row for something the user had already deleted. The audit rows themselves
    # are untouched -- event_changes stays append-only, this only decides what
    # the feed surfaces.
    items = await session.scalars(
        select(EventChange)
        .join(Event, Event.id == EventChange.event_id)
        .where(Event.is_deleted.is_(False))
        .order_by(EventChange.created_at.desc())
        .limit(limit)
    )
    return await attach_translations(session, [ChangeRead.model_validate(item) for item in items])

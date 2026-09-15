import hashlib
import re
import unicodedata
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Select, String, cast, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from trade_calendar.core.errors import ApiError
from trade_calendar.models.base import utc_now
from trade_calendar.models.domain import (
    AuditLog,
    DatePrecision,
    Event,
    EventChange,
    EventFieldLock,
    EventSource,
    EventStatus,
    EventVersion,
    Source,
    SourceHealth,
)
from trade_calendar.schemas.events import EventCreate, EventUpdate

VERSION_FIELDS = (
    "title_zh", "title_original", "institution", "country_code", "category", "event_type",
    "status", "importance", "date_precision", "starts_at", "ends_at", "local_date",
    "date_range_start", "date_range_end",
    "original_timezone", "original_time_text", "reference_period", "market_tags", "tickers",
    "notes", "reminder_enabled", "is_deleted",
)
ALLOWED_LOCK_FIELDS = set(VERSION_FIELDS) - {"is_deleted"}


def normalize_title(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold().strip()
    return re.sub(r"[^\w\u3400-\u9fff]+", " ", normalized).strip()


def canonical_key(
    institution: str,
    event_type: str,
    title: str,
    starts_at: datetime | None,
    local_date: date | None,
    idempotency_key: str | None = None,
) -> str:
    if idempotency_key:
        raw = f"manual:{idempotency_key}"
    else:
        event_date = starts_at.date().isoformat() if starts_at else str(local_date or "unknown")
        raw = f"{normalize_title(institution)}|{event_type}|{normalize_title(title)}|{event_date}"
    return hashlib.sha256(raw.encode()).hexdigest()


def event_snapshot(event: Event) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for field in VERSION_FIELDS:
        value = getattr(event, field)
        if isinstance(value, datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=UTC)
            value = value.astimezone(UTC).isoformat()
        elif isinstance(value, date):
            value = value.isoformat()
        elif hasattr(value, "value"):
            value = value.value
        result[field] = value
    return result


def classify_change(before: dict[str, Any] | None, after: dict[str, Any]) -> str:
    if before is None:
        return "created"
    if before.get("status") != "cancelled" and after.get("status") == "cancelled":
        return "cancelled"
    time_fields = {
        "starts_at", "ends_at", "local_date", "date_range_start", "date_range_end",
    }
    if any(before.get(field) != after.get(field) for field in time_fields):
        if before.get("starts_at") is None and after.get("starts_at") is not None:
            return "time_confirmed"
        return "rescheduled"
    if before.get("importance") != after.get("importance"):
        return "importance_changed"
    return "updated"


def diff_snapshots(before: dict[str, Any] | None, after: dict[str, Any]) -> dict[str, Any]:
    if before is None:
        return {"created": {"old": None, "new": after}}
    return {
        key: {"old": before.get(key), "new": after.get(key)}
        for key in after
        if before.get(key) != after.get(key)
    }


async def _ensure_manual_source(session: AsyncSession) -> Source:
    source = await session.scalar(select(Source).where(Source.key == "manual"))
    if source:
        source.enabled = False
        source.health = SourceHealth.DISABLED
        return source
    source = Source(
        key="manual", name="人工录入", institution="User", country_code="LOCAL",
        official_url="manual://admin", source_type="manual", priority=0, schedule="disabled",
        enabled=False,
        health=SourceHealth.DISABLED,
    )
    session.add(source)
    await session.flush()
    return source


async def create_event(
    session: AsyncSession,
    payload: EventCreate,
    request_id: str,
) -> tuple[Event, bool]:
    values = payload.normalized_utc()
    idem = values.pop("idempotency_key", None)
    key = canonical_key(
        values["institution"], values["event_type"], values["title_zh"],
        values["starts_at"], values["local_date"], idem,
    )
    existing = await session.scalar(select(Event).where(Event.canonical_key == key))
    if existing:
        return existing, False

    event = Event(
        **values,
        canonical_key=key,
        normalized_title=normalize_title(values["title_zh"]),
        is_manual=True,
        last_verified_at=utc_now(),
    )
    session.add(event)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        existing = await session.scalar(select(Event).where(Event.canonical_key == key))
        if existing:
            return existing, False
        raise
    source = await _ensure_manual_source(session)
    session.add(EventSource(event_id=event.id, source_id=source.id, is_primary=True))
    await _record_version(session, event, None, "manual", "admin")
    from trade_calendar.notifications import ensure_event_notifications

    await ensure_event_notifications(session, event)
    session.add(AuditLog(
        actor_id="admin", action="event.create", entity_type="event", entity_id=event.id,
        request_id=request_id, before=None, after=event_snapshot(event), created_at=utc_now(),
    ))
    await session.commit()
    await session.refresh(event)
    return event, True


async def update_event(
    session: AsyncSession,
    event: Event,
    payload: EventUpdate,
    request_id: str,
    actor_type: str = "manual",
) -> tuple[Event, bool]:
    before = event_snapshot(event)
    updates = payload.model_dump(exclude_unset=True)
    locked = set(await session.scalars(
        select(EventFieldLock.field_name).where(EventFieldLock.event_id == event.id)
    ))
    if actor_type != "manual":
        updates = {key: value for key, value in updates.items() if key not in locked}
    for key in ("starts_at", "ends_at"):
        if updates.get(key) is not None:
            value = updates[key]
            if value.tzinfo is None:
                raise ApiError(422, "timezone_required", f"{key} 必须包含时区")
            updates[key] = value.astimezone(UTC)
    if "country_code" in updates and updates["country_code"]:
        updates["country_code"] = updates["country_code"].upper()
    if "title_original" in updates or "title_zh" in updates:
        title_for_matching = updates.get("title_original") or updates.get("title_zh")
        if title_for_matching:
            event.normalized_title = normalize_title(title_for_matching)
    for key, value in updates.items():
        setattr(event, key, value)
    _validate_event_time(event)
    after = event_snapshot(event)
    if before == after:
        return event, False
    event.current_version += 1
    await _record_version(session, event, before, actor_type, "admin")
    from trade_calendar.notifications import ensure_event_notifications

    await ensure_event_notifications(session, event)
    session.add(AuditLog(
        actor_id="admin", action="event.update", entity_type="event", entity_id=event.id,
        request_id=request_id, before=before, after=after, created_at=utc_now(),
    ))
    await session.commit()
    await session.refresh(event)
    return event, True


def _validate_event_time(event: Event) -> None:
    if event.date_precision in {DatePrecision.MINUTE, DatePrecision.HOUR, DatePrecision.WINDOW}:
        if event.starts_at is None:
            raise ApiError(422, "invalid_time_precision", "精确时间事件必须提供 starts_at")
    if event.date_precision == DatePrecision.DATE:
        if event.local_date is None or event.starts_at is not None:
            raise ApiError(422, "invalid_date_only_event", "仅日期事件只能保存 local_date")
    if event.date_precision == DatePrecision.WINDOW and event.ends_at is None:
        raise ApiError(422, "invalid_time_window", "时间窗口必须提供 ends_at")
    if event.starts_at and event.ends_at and event.ends_at <= event.starts_at:
        raise ApiError(422, "invalid_time_window", "ends_at 必须晚于 starts_at")
    if (event.date_range_start is None) != (event.date_range_end is None):
        raise ApiError(422, "invalid_date_range", "日期范围必须同时提供开始和结束日期")
    if event.date_range_start and event.date_range_end:
        if event.date_range_end < event.date_range_start:
            raise ApiError(422, "invalid_date_range", "日期范围结束日期不能早于开始日期")
        if event.local_date and not (
            event.date_range_start <= event.local_date <= event.date_range_end
        ):
            raise ApiError(422, "invalid_date_range", "事件日期必须位于日期范围内")


async def _record_version(
    session: AsyncSession,
    event: Event,
    before: dict[str, Any] | None,
    actor_type: str,
    actor_id: str | None,
) -> None:
    now = utc_now()
    after = event_snapshot(event)
    session.add(EventVersion(
        event_id=event.id, version=event.current_version, snapshot=after,
        actor_type=actor_type, actor_id=actor_id, created_at=now,
    ))
    session.add(EventChange(
        event_id=event.id,
        from_version=event.current_version - 1 if before else None,
        to_version=event.current_version,
        change_type=classify_change(before, after),
        changed_fields=diff_snapshots(before, after),
        created_at=now,
    ))


async def get_event_or_404(session: AsyncSession, event_id: UUID) -> Event:
    event = await session.get(Event, event_id)
    if not event or event.is_deleted:
        raise ApiError(404, "event_not_found", "事件不存在")
    return event


def event_query(
    *, from_at: datetime | None = None, to_at: datetime | None = None,
    from_date: date | None = None, to_date: date | None = None,
    country: str | None = None, market: str | None = None, category: str | None = None,
    importance: str | None = None, status: str | None = None, query: str | None = None,
) -> Select[tuple[Event]]:
    statement = select(Event).where(Event.is_deleted.is_(False))
    local_from = from_date or (from_at.date() if from_at else None)
    local_to = to_date or (to_at.date() if to_at else None)
    local_after = (
        or_(Event.local_date >= local_from, Event.date_range_end >= local_from)
        if local_from else None
    )
    local_before = (
        or_(Event.local_date < local_to, Event.date_range_start < local_to)
        if local_to else None
    )
    if from_at and local_from:
        assert local_after is not None
        statement = statement.where(or_(Event.starts_at >= from_at, local_after))
    elif from_at:
        statement = statement.where(Event.starts_at >= from_at)
    elif local_from:
        assert local_after is not None
        statement = statement.where(local_after)
    if to_at and local_to:
        assert local_before is not None
        statement = statement.where(or_(Event.starts_at < to_at, local_before))
    elif to_at:
        statement = statement.where(Event.starts_at < to_at)
    elif local_to:
        assert local_before is not None
        statement = statement.where(local_before)
    if country:
        statement = statement.where(Event.country_code == country.upper())
    if market:
        # market_tags is a generic JSON column, so .contains([...]) renders a
        # `LIKE` comparison, which PostgreSQL rejects with
        # "operator does not exist: json ~~ text". Match the quoted JSON element
        # instead; this works on both PostgreSQL and SQLite. The surrounding
        # quotes keep "US" from matching "USA".
        statement = statement.where(
            cast(Event.market_tags, String).ilike(f'%"{market.upper()}"%')
        )
    if category:
        statement = statement.where(Event.category == category)
    if importance:
        statement = statement.where(Event.importance == importance)
    if status:
        statement = statement.where(Event.status == status)
    if query:
        like = f"%{query.strip()}%"
        statement = statement.where(or_(
            Event.title_zh.ilike(like), Event.title_original.ilike(like),
            Event.institution.ilike(like), Event.notes.ilike(like),
            cast(Event.tickers, String).ilike(like),
        ))
    return statement


async def count_events(session: AsyncSession, statement: Select[tuple[Event]]) -> int:
    return int(await session.scalar(select(func.count()).select_from(statement.subquery())) or 0)


async def lock_field(
    session: AsyncSession, event: Event, field_name: str, reason: str | None, request_id: str,
) -> EventFieldLock:
    if field_name not in ALLOWED_LOCK_FIELDS:
        raise ApiError(422, "invalid_lock_field", "该字段不能锁定")
    existing = await session.scalar(select(EventFieldLock).where(
        EventFieldLock.event_id == event.id, EventFieldLock.field_name == field_name
    ))
    if existing:
        return existing
    field_lock = EventFieldLock(
        event_id=event.id, field_name=field_name, locked_by="admin", reason=reason
    )
    session.add(field_lock)
    session.add(AuditLog(
        actor_id="admin", action="field.lock", entity_type="event", entity_id=event.id,
        request_id=request_id, before=None, after={"field": field_name}, created_at=utc_now(),
    ))
    await session.commit()
    await session.refresh(field_lock)
    return field_lock


async def soft_delete_event(session: AsyncSession, event: Event, request_id: str) -> None:
    before = event_snapshot(event)
    event.is_deleted = True
    event.status = EventStatus.IGNORED
    event.current_version += 1
    await _record_version(session, event, before, "manual", "admin")
    from trade_calendar.notifications import ensure_event_notifications

    await ensure_event_notifications(session, event)
    session.add(AuditLog(
        actor_id="admin", action="event.ignore", entity_type="event", entity_id=event.id,
        request_id=request_id, before=before, after=event_snapshot(event), created_at=utc_now(),
    ))
    await session.commit()

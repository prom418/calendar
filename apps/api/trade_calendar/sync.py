import hashlib
import json
import re
from datetime import UTC, date, datetime, timedelta
from difflib import SequenceMatcher
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trade_calendar.adapters.base import SourceAdapter
from trade_calendar.adapters.errors import AdapterError, EmptyResultError
from trade_calendar.adapters.types import NormalizedEvent
from trade_calendar.models.base import utc_now
from trade_calendar.models.domain import (
    Event,
    EventSource,
    FetchRun,
    RawSnapshot,
    ReviewQueueItem,
    ReviewStatus,
    RunStatus,
    Source,
    SourceHealth,
    SourceObservation,
)
from trade_calendar.preferences import load_web_settings
from trade_calendar.schemas.events import EventUpdate
from trade_calendar.services import (
    _record_version,
    canonical_key,
    normalize_title,
    update_event,
)


class SyncRunner:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    async def execute(self, run_id: UUID, adapter: SourceAdapter) -> FetchRun:
        async with self.session_factory() as session:
            run = await session.get(FetchRun, run_id)
            if run is None:
                raise ValueError(f"fetch run {run_id} does not exist")
            source = await session.get(Source, run.source_id)
            if source is None:
                raise ValueError(f"source {run.source_id} does not exist")
            run.status = RunStatus.RUNNING
            run.started_at = utc_now()
            await session.commit()
            try:
                payload = await adapter.fetch()
                run.fetched_count = 1
                snapshot = await self._snapshot(
                    session,
                    source,
                    run,
                    adapter.version,
                    payload.content,
                    payload.url,
                    payload.content_type,
                    payload.http_status,
                )
                parsed = adapter.parse(payload)
                normalized = [adapter.normalize(item) for item in parsed]
                health = adapter.health_check(normalized)
                if not normalized and not adapter.allow_empty:
                    raise EmptyResultError("source returned zero events")
                run.parsed_count = len(normalized)
                for item in normalized:
                    action = await self._reconcile(session, source, run, snapshot, item)
                    if action == "created":
                        run.created_count += 1
                    elif action == "updated":
                        run.updated_count += 1
                run.status = RunStatus.SUCCEEDED
                run.finished_at = utc_now()
                source.health = SourceHealth.HEALTHY if health.healthy else SourceHealth.DEGRADED
                source.consecutive_failures = 0
                source.last_success_at = run.finished_at
                source.last_event_count = len(normalized)
                await session.commit()
            except Exception as exc:
                await session.rollback()
                run = await session.get(FetchRun, run_id)
                source = await session.get(Source, run.source_id) if run else None
                if run is None or source is None:
                    raise
                run.status = RunStatus.FAILED
                run.finished_at = utc_now()
                run.error_type = exc.code if isinstance(exc, AdapterError) else type(exc).__name__
                run.error_message = str(exc)[:2000]
                source.consecutive_failures += 1
                source.last_failure_at = run.finished_at
                source.health = (
                    SourceHealth.FAILED
                    if source.consecutive_failures >= 3
                    else SourceHealth.DEGRADED
                )
                await session.commit()
            await session.refresh(run)
            return run

    async def _snapshot(
        self,
        session: AsyncSession,
        source: Source,
        run: FetchRun,
        adapter_version: str,
        content: bytes,
        url: str,
        content_type: str,
        http_status: int,
    ) -> RawSnapshot:
        digest = hashlib.sha256(content).hexdigest()
        existing = await session.scalar(select(RawSnapshot).where(
            RawSnapshot.source_id == source.id, RawSnapshot.content_hash == digest
        ))
        if existing:
            return existing
        preferences = await load_web_settings(session)
        snapshot = RawSnapshot(
            source_id=source.id,
            fetch_run_id=run.id,
            url=url,
            content_hash=digest,
            content_type=content_type,
            body=content,
            http_status=http_status,
            adapter_version=adapter_version,
            expires_at=utc_now() + timedelta(days=preferences.snapshot_retention_days),
        )
        session.add(snapshot)
        await session.flush()
        return snapshot

    async def _reconcile(
        self,
        session: AsyncSession,
        source: Source,
        run: FetchRun,
        snapshot: RawSnapshot,
        item: NormalizedEvent,
    ) -> str:
        fingerprint = event_fingerprint(item)
        observation = await session.scalar(select(SourceObservation).where(
            SourceObservation.source_id == source.id,
            SourceObservation.source_event_id == item.source_event_id,
        ))
        if observation is None:
            observation = SourceObservation(
                source_id=source.id,
                fetch_run_id=run.id,
                snapshot_id=snapshot.id,
                source_event_id=item.source_event_id,
                fingerprint=fingerprint,
                title=item.title_original,
                institution=item.institution,
                event_type=item.event_type,
                starts_at=item.starts_at,
                local_date=item.local_date,
                date_range_start=item.date_range_start,
                date_range_end=item.date_range_end,
                original_payload=item.raw,
            )
            session.add(observation)
            await session.flush()
        else:
            observation.fetch_run_id = run.id
            observation.snapshot_id = snapshot.id
            observation.fingerprint = fingerprint
            observation.title = item.title_original
            observation.starts_at = item.starts_at
            observation.local_date = item.local_date
            observation.date_range_start = item.date_range_start
            observation.date_range_end = item.date_range_end
            observation.original_payload = item.raw

        event = await session.get(Event, observation.event_id) if observation.event_id else None
        if event is None:
            event, confidence, reason = await find_match(session, item)
            observation.match_confidence = confidence
            observation.match_reason = reason
            if event is None and confidence >= 0.70:
                session.add(ReviewQueueItem(
                    observation_id=observation.id,
                    candidate_event_id=None,
                    status=ReviewStatus.PENDING,
                    reason=reason,
                    confidence=confidence,
                ))
                return "review"
            if event is None:
                event = await create_canonical_event(session, source, item)
                observation.event_id = event.id
                session.add(EventSource(
                    event_id=event.id,
                    source_id=source.id,
                    observation_id=observation.id,
                    is_primary=True,
                    last_verified_at=utc_now(),
                ))
                return "created"
            observation.event_id = event.id
            link = await session.scalar(select(EventSource).where(
                EventSource.event_id == event.id,
                EventSource.source_id == source.id,
            ))
            if link:
                link.observation_id = observation.id
                link.last_verified_at = utc_now()
            else:
                session.add(EventSource(
                    event_id=event.id,
                    source_id=source.id,
                    observation_id=observation.id,
                    is_primary=False,
                    last_verified_at=utc_now(),
                ))

        existing_link = await session.scalar(select(EventSource).where(
            EventSource.event_id == event.id, EventSource.source_id == source.id
        ))
        if existing_link:
            existing_link.observation_id = observation.id
            existing_link.last_verified_at = utc_now()
        primary_priority = await primary_source_priority(session, event.id)
        if existing_link and source.priority < primary_priority:
            links = list(await session.scalars(
                select(EventSource).where(EventSource.event_id == event.id)
            ))
            for link in links:
                link.is_primary = link.source_id == source.id
            primary_priority = source.priority
        if source.priority > primary_priority and existing_link and not existing_link.is_primary:
            return "unchanged"
        updates = EventUpdate(
            title_zh=item.title_zh,
            title_original=item.title_original,
            institution=item.institution,
            country_code=item.country_code,
            category=item.category,
            event_type=item.event_type,
            status=item.status,
            importance=item.importance,
            date_precision=item.date_precision,
            starts_at=item.starts_at,
            ends_at=item.ends_at,
            local_date=item.local_date,
            date_range_start=item.date_range_start,
            date_range_end=item.date_range_end,
            original_timezone=item.original_timezone,
            original_time_text=item.original_time_text,
            reference_period=item.reference_period or event.reference_period,
            market_tags=list(dict.fromkeys([*event.market_tags, *item.market_tags])),
            tickers=list(dict.fromkeys([*event.tickers, *item.tickers])),
        )
        event, changed = await update_event(
            session, event, updates, request_id=str(run.id), actor_type="source"
        )
        event.last_verified_at = utc_now()
        return "updated" if changed else "unchanged"


async def create_canonical_event(
    session: AsyncSession, source: Source, item: NormalizedEvent
) -> Event:
    event = Event(
        canonical_key=canonical_key(
            item.institution, item.event_type, item.title_original,
            item.starts_at, item.local_date,
        ),
        title_zh=item.title_zh,
        title_original=item.title_original,
        normalized_title=normalize_title(item.title_original),
        institution=item.institution,
        country_code=item.country_code,
        category=item.category,
        event_type=item.event_type,
        status=item.status,
        importance=item.importance,
        date_precision=item.date_precision,
        starts_at=item.starts_at,
        ends_at=item.ends_at,
        local_date=item.local_date,
        date_range_start=item.date_range_start,
        date_range_end=item.date_range_end,
        original_timezone=item.original_timezone,
        original_time_text=item.original_time_text,
        reference_period=item.reference_period,
        market_tags=item.market_tags,
        tickers=item.tickers,
        reminder_enabled=True,
        is_manual=False,
        last_verified_at=utc_now(),
    )
    session.add(event)
    await session.flush()
    await _record_version(session, event, None, "source", source.key)
    from trade_calendar.notifications import ensure_event_notifications

    await ensure_event_notifications(session, event)
    return event


async def find_match(
    session: AsyncSession, item: NormalizedEvent
) -> tuple[Event | None, float, str]:
    exact_key = canonical_key(
        item.institution,
        item.event_type,
        item.title_original,
        item.starts_at,
        item.local_date,
    )
    exact = await session.scalar(select(Event).where(
        Event.canonical_key == exact_key,
        Event.is_deleted.is_(False),
    ))
    if exact is not None:
        return exact, 1.0, "canonical event key"
    candidates = list(await session.scalars(select(Event).where(
        Event.event_type == item.event_type,
        Event.country_code == item.country_code,
        Event.is_deleted.is_(False),
    )))
    target_date = item.starts_at.date() if item.starts_at else item.local_date
    normalized = normalize_title(item.title_original)
    institution = normalize_title(item.institution)
    item_tickers = {ticker.casefold() for ticker in item.tickers}
    uncertain = 0.0
    for candidate in candidates:
        candidate_date = candidate.starts_at.date() if candidate.starts_at else candidate.local_date
        candidate_title = normalize_title(candidate.title_original or candidate.title_zh)
        candidate_institution = normalize_title(candidate.institution)
        candidate_tickers = {ticker.casefold() for ticker in candidate.tickers}
        ticker_overlap = bool(item_tickers & candidate_tickers)
        same_institution = institution == candidate_institution
        same_reference = reference_periods_compatible(
            item.reference_period,
            candidate.reference_period,
        )
        similarity = SequenceMatcher(None, normalized, candidate_title).ratio()
        if ticker_overlap and same_reference:
            return candidate, 0.995, "ticker + event_type + reference period"
        if ticker_overlap and candidate_date == target_date:
            return candidate, 0.99, "ticker + event_type + date"
        if (
            ticker_overlap
            and same_reference
            and candidate_date is not None
            and target_date is not None
            and abs((candidate_date - target_date).days) <= 2
            and similarity >= 0.88
        ):
            return candidate, 0.985, (
                "ticker + event_type + compatible reporting period + nearby date"
            )
        if same_institution and same_reference and normalized == candidate_title:
            # The date is required here as well. A reference period does not
            # identify an occurrence: Taiwan's DGBAS reuses one `notice` value
            # across several releases of the same statistic, so matching on
            # institution + period + title alone folded every release into a
            # single event whose starts_at flip-flopped between dates on each
            # sync -- one event in the dev database reached version 30, and the
            # calendar showed whichever release synced last.
            if dates_compatible(candidate_date, target_date):
                return candidate, 0.99, (
                    "institution + event_type + reference period + normalized title"
                )
        if same_institution and candidate_date == target_date and similarity >= 0.88:
            return candidate, 0.98, "institution + event_type + date + similar title"
        if candidate_date == target_date and normalized == candidate_title:
            return candidate, 0.97, "country + event_type + date + normalized title"
        if candidate_date == target_date and (ticker_overlap or same_institution):
            uncertain = max(uncertain, similarity * 0.9)
    if uncertain >= 0.70:
        return None, uncertain, "similar candidate requires manual review"
    return None, 0.0, "no plausible canonical event found"


def reference_periods_compatible(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return False
    normalized_left = re.sub(r"\s+", " ", left.strip().upper())
    normalized_right = re.sub(r"\s+", " ", right.strip().upper())
    if normalized_left == normalized_right:
        return True
    pattern = re.compile(r"(?:FY)?(20\d{2})\s+(Q[1-4]|H[12])")
    left_match = pattern.fullmatch(normalized_left)
    right_match = pattern.fullmatch(normalized_right)
    if left_match is None or right_match is None:
        return False
    if left_match.group(1) != right_match.group(1):
        return False
    periods = {left_match.group(2), right_match.group(2)}
    return periods in ({"Q2", "H1"}, {"Q4", "H2"})


def dates_compatible(left: date | None, right: date | None) -> bool:
    """Can two occurrence dates describe the same event?

    An unknown date on either side is compatible: that is the case this exists
    to serve, where the same occurrence is reported by a second source that has
    not pinned the date yet. Two known but different dates are not compatible --
    they are different occurrences, and merging them is what produced the bug
    below.
    """
    return left is None or right is None or left == right


async def primary_source_priority(session: AsyncSession, event_id: UUID) -> int:
    rows = await session.execute(
        select(Source.priority)
        .join(EventSource, EventSource.source_id == Source.id)
        .where(EventSource.event_id == event_id, EventSource.is_primary.is_(True))
    )
    priorities = list(rows.scalars())
    return min(priorities, default=10_000)


def event_fingerprint(item: NormalizedEvent) -> str:
    payload = {
        "title": normalize_title(item.title_original),
        "type": item.event_type,
        "starts_at": item.starts_at.astimezone(UTC).isoformat() if item.starts_at else None,
        "local_date": item.local_date.isoformat() if item.local_date else None,
        "date_range_start": (
            item.date_range_start.isoformat() if item.date_range_start else None
        ),
        "date_range_end": item.date_range_end.isoformat() if item.date_range_end else None,
        "status": item.status.value,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def make_run(source: Source, adapter: SourceAdapter, trigger: str = "manual") -> FetchRun:
    timestamp = datetime.now(UTC).isoformat()
    return FetchRun(
        source_id=source.id,
        run_key=f"{source.key}:{trigger}:{timestamp}",
        trigger=trigger,
        status=RunStatus.PENDING,
        adapter_version=adapter.version,
    )

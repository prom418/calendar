from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trade_calendar.adapters.base import SourceAdapter
from trade_calendar.adapters.bls import BlsCalendarAdapter, raw_payload_from_fixture
from trade_calendar.adapters.fed import FedFomcAdapter
from trade_calendar.adapters.http import HttpFetcher
from trade_calendar.adapters.types import NormalizedEvent, RawPayload, SourceEvent
from trade_calendar.models.base import utc_now
from trade_calendar.models.domain import (
    DatePrecision,
    Event,
    EventChange,
    EventSource,
    EventStatus,
    EventVersion,
    FetchRun,
    Importance,
    RunStatus,
    Source,
    SourceHealth,
)
from trade_calendar.services import canonical_key, normalize_title
from trade_calendar.sync import SyncRunner, find_match, make_run

FIXTURE = Path(__file__).parent / "fixtures" / "bls" / "calendar.json"
FED_FIXTURE = Path(__file__).parent / "fixtures" / "fed" / "fomc-calendar.html"


class FixtureBlsAdapter(BlsCalendarAdapter):
    def __init__(self, content: bytes) -> None:
        super().__init__()
        self.content = content

    async def fetch(self) -> RawPayload:
        return raw_payload_from_fixture(self.content)


class FixtureFedAdapter(FedFomcAdapter):
    def __init__(self, content: bytes) -> None:
        super().__init__(HttpFetcher(user_agent="test-suite"))
        self.content = content

    async def fetch(self) -> RawPayload:
        return RawPayload(
            source_key=self.source_key,
            url=self.url,
            content=self.content,
            content_type="text/html",
        )


class AlternateIdBlsAdapter(FixtureBlsAdapter):
    def parse(self, payload: RawPayload) -> list[SourceEvent]:
        events = super().parse(payload)
        for event in events:
            event.source_event_id = f"migrated:{event.source_event_id}"
        return events


class AlternateIdFedAdapter(FixtureFedAdapter):
    def parse(self, payload: RawPayload) -> list[SourceEvent]:
        events = super().parse(payload)
        for event in events:
            event.source_event_id = f"{event.source_event_id}-reissued"
        return events


async def make_source(session: AsyncSession, key: str = "us_bls_calendar") -> Source:
    source = Source(
        key=key,
        name="BLS Fixture",
        institution="U.S. Bureau of Labor Statistics",
        country_code="US",
        official_url="https://open.longbridge.com/docs/market/calendar/macro-calendar",
        source_type="api",
        priority=10,
        enabled=True,
        health=SourceHealth.STALE,
        schedule="manual",
    )
    session.add(source)
    await session.commit()
    await session.refresh(source)
    return source


async def queue_run(
    session_factory: async_sessionmaker[AsyncSession],
    source_id: UUID,
    adapter: SourceAdapter,
) -> FetchRun:
    async with session_factory() as session:
        source = await session.get(Source, source_id)
        assert source is not None
        run = make_run(source, adapter)
        session.add(run)
        await session.commit()
        await session.refresh(run)
        return run


async def test_sync_is_idempotent_and_keeps_snapshots(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    adapter = FixtureBlsAdapter(FIXTURE.read_bytes())
    async with session_factory() as session:
        source = await make_source(session)
        source_id = source.id
    runner = SyncRunner(session_factory)
    first = await queue_run(session_factory, source_id, adapter)
    first_result = await runner.execute(first.id, adapter)
    second = await queue_run(session_factory, source_id, adapter)
    second_result = await runner.execute(second.id, adapter)

    assert first_result.status == RunStatus.SUCCEEDED
    assert first_result.created_count == 6
    assert second_result.status == RunStatus.SUCCEEDED
    assert second_result.created_count == 0
    assert second_result.updated_count == 0

    async with session_factory() as session:
        assert await session.scalar(select(func.count(Event.id))) == 6
        assert await session.scalar(select(func.count(EventVersion.id))) == 6


async def test_source_reschedule_updates_original_event(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    original = FIXTURE.read_bytes()
    changed = original.replace(
        b"1788525000",
        b"1788611400",
    )
    first_adapter = FixtureBlsAdapter(original)
    second_adapter = FixtureBlsAdapter(changed)
    async with session_factory() as session:
        source = await make_source(session)
        source_id = source.id
    runner = SyncRunner(session_factory)
    first = await queue_run(session_factory, source_id, first_adapter)
    await runner.execute(first.id, first_adapter)
    second = await queue_run(session_factory, source_id, second_adapter)
    result = await runner.execute(second.id, second_adapter)
    assert result.updated_count == 1, (result.status, result.error_type, result.error_message)

    async with session_factory() as session:
        event = await session.scalar(select(Event).where(Event.title_zh == "美国就业报告"))
        assert event is not None
        assert event.starts_at is not None
        assert event.starts_at.day == 5
        versions = list(await session.scalars(
            select(EventVersion).where(EventVersion.event_id == event.id)
        ))
        assert len(versions) == 2
        change = await session.scalar(
            select(EventChange)
            .where(EventChange.event_id == event.id)
            .order_by(EventChange.created_at.desc())
        )
        assert change is not None
        assert change.change_type == "rescheduled"


async def test_provider_migration_matches_original_title_when_cache_is_localized(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    original = FixtureBlsAdapter(FIXTURE.read_bytes())
    migrated = AlternateIdBlsAdapter(FIXTURE.read_bytes())
    async with session_factory() as session:
        source = await make_source(session)
        source_id = source.id
    runner = SyncRunner(session_factory)
    first = await queue_run(session_factory, source_id, original)
    await runner.execute(first.id, original)

    async with session_factory() as session:
        events = list(await session.scalars(select(Event)))
        for event in events:
            event.normalized_title = normalize_title(event.title_zh)
        await session.commit()

    second = await queue_run(session_factory, source_id, migrated)
    result = await runner.execute(second.id, migrated)

    assert result.status == RunStatus.SUCCEEDED
    assert result.created_count == 0
    async with session_factory() as session:
        assert await session.scalar(select(func.count(Event.id))) == 6
        assert await session.scalar(select(func.count(EventSource.id))) == 6


async def test_failed_empty_result_never_deletes_existing_events(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    valid = FixtureBlsAdapter(FIXTURE.read_bytes())
    empty = FixtureBlsAdapter(b'{"status":"ok","result":[]}')
    async with session_factory() as session:
        source = await make_source(session)
        source_id = source.id
    runner = SyncRunner(session_factory)
    first = await queue_run(session_factory, source_id, valid)
    await runner.execute(first.id, valid)
    failed = await queue_run(session_factory, source_id, empty)
    result = await runner.execute(failed.id, empty)

    assert result.status == RunStatus.FAILED
    assert result.error_type == "structure_changed"
    async with session_factory() as session:
        assert await session.scalar(select(func.count(Event.id))) == 6
        links = await session.scalar(select(func.count(EventSource.id)))
        assert links == 6


async def test_recurring_same_title_on_different_dates_are_distinct_events(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    adapter = FixtureFedAdapter(FED_FIXTURE.read_bytes())
    async with session_factory() as session:
        source = await make_source(session, key="fed_fomc_calendar")
        source.name = "Fed Fixture"
        await session.commit()
        source_id = source.id
    runner = SyncRunner(session_factory)
    run = await queue_run(session_factory, source_id, adapter)
    result = await runner.execute(run.id, adapter)
    assert result.status == RunStatus.SUCCEEDED
    assert result.created_count == 3
    async with session_factory() as session:
        assert await session.scalar(select(func.count(Event.id))) == 3


async def test_reissued_source_ids_reuse_one_event_source_link(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    original = FixtureFedAdapter(FED_FIXTURE.read_bytes())
    reissued = AlternateIdFedAdapter(FED_FIXTURE.read_bytes())
    async with session_factory() as session:
        source = await make_source(session, key="fed_fomc_calendar")
        source_id = source.id
    runner = SyncRunner(session_factory)
    first = await queue_run(session_factory, source_id, original)
    await runner.execute(first.id, original)
    second = await queue_run(session_factory, source_id, reissued)
    result = await runner.execute(second.id, reissued)
    assert result.status == RunStatus.SUCCEEDED
    assert result.created_count == 0
    async with session_factory() as session:
        assert await session.scalar(select(func.count(Event.id))) == 3
        assert await session.scalar(select(func.count(EventSource.id))) == 3


def _dgbas_release(starts_at: datetime | None) -> NormalizedEvent:
    """One DGBAS release: shared institution, title and `notice`, moving date.

    Mirrors the real row that went wrong -- every field the merge rules compare
    except the date is identical between releases of the same statistic.
    """
    return NormalizedEvent(
        source_event_id=f"dgbas-2000-{starts_at.isoformat() if starts_at else 'undated'}",
        title_zh="台湾统计：Employee Compensation and Turnover Statistics",
        title_original="Employee Compensation and Turnover Statistics",
        institution="Directorate-General of Budget, Accounting and Statistics",
        country_code="TW",
        category="macro_release",
        event_type="employment",
        status=EventStatus.CONFIRMED,
        importance=Importance.HIGH,
        date_precision=DatePrecision.MINUTE if starts_at else DatePrecision.UNKNOWN,
        starts_at=starts_at,
        original_timezone="Asia/Taipei",
        reference_period="(2025)",
        market_tags=["TW"],
        source_url="https://eng.stat.gov.tw/",
    )


def _event_row(item: NormalizedEvent) -> Event:
    return Event(
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
        local_date=item.local_date,
        original_timezone=item.original_timezone,
        reference_period=item.reference_period,
        market_tags=item.market_tags,
        tickers=item.tickers,
        reminder_enabled=True,
        is_manual=False,
        last_verified_at=utc_now(),
    )


async def test_a_second_occurrence_of_the_same_release_is_not_merged(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    september = _dgbas_release(datetime(2026, 9, 14, 8, tzinfo=UTC))
    async with session_factory() as session:
        session.add(_event_row(september))
        await session.commit()

    async with session_factory() as session:
        matched, score, reason = await find_match(
            session, _dgbas_release(datetime(2026, 10, 15, 8, tzinfo=UTC))
        )
    assert matched is None
    assert score == 0.0
    assert reason == "no plausible canonical event found"


async def test_the_same_occurrence_without_a_date_still_merges(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The rule's original purpose survives: a second source that has not pinned
    the date yet is the same occurrence, not a different one."""
    september = _dgbas_release(datetime(2026, 9, 14, 8, tzinfo=UTC))
    async with session_factory() as session:
        session.add(_event_row(september))
        await session.commit()

    async with session_factory() as session:
        matched, score, _ = await find_match(session, _dgbas_release(None))
    assert matched is not None
    assert score == 0.99

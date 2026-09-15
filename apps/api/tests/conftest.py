from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from trade_calendar.core.database import get_session
from trade_calendar.main import app, settings
from trade_calendar.models import Base


@pytest.fixture(autouse=True)
def _disable_internal_api_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the shared-secret gate off for the unit suite.

    The gate switches on as soon as ``INTERNAL_API_SECRET`` is present in the
    root ``.env``, which is the case on the machine that also runs the Tunnel.
    The suite tests API behaviour rather than the gate, so turn it off here;
    ``test_internal_api_secret.py`` switches it back on to cover the gate.
    """
    monkeypatch.setattr(settings, "internal_api_secret", None)


@pytest.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest.fixture
async def client(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncClient]:
    async def override_session() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def minute_event_payload() -> dict[str, object]:
    return {
        "title_zh": "FOMC 利率决议",
        "title_original": "Federal Open Market Committee Meeting",
        "institution": "Federal Reserve",
        "country_code": "us",
        "category": "monetary_policy",
        "event_type": "central_bank_decision",
        "status": "confirmed",
        "importance": "critical",
        "date_precision": "minute",
        "starts_at": "2026-09-16T14:00:00-04:00",
        "original_timezone": "America/New_York",
        "original_time_text": "September 16, 2:00 p.m. ET",
        "market_tags": ["US", "GLOBAL"],
        "idempotency_key": "fomc-2026-09-16",
    }


from uuid import UUID

from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from trade_calendar.models.domain import EventChange


async def test_health_has_request_id(client: AsyncClient) -> None:
    response = await client.get("/health", headers={"X-Request-ID": "test-request"})
    assert response.status_code == 200
    assert response.json()["request_id"] == "test-request"
    assert response.headers["X-Request-ID"] == "test-request"


async def test_create_event_is_idempotent_and_versioned(
    client: AsyncClient, minute_event_payload: dict[str, object]
) -> None:
    first = await client.post("/api/v1/events", json=minute_event_payload)
    second = await client.post("/api/v1/events", json=minute_event_payload)

    assert first.status_code == 201
    assert second.status_code == 200
    assert first.json()["id"] == second.json()["id"]
    assert first.json()["starts_at"] == "2026-09-16T18:00:00Z"
    assert first.json()["country_code"] == "US"
    assert first.json()["display_title"] == "Federal Open Market Committee Meeting"

    source_response = await client.get(f"/api/v1/events/{first.json()['id']}/sources")
    assert source_response.status_code == 200
    evidence = source_response.json()
    assert len(evidence) == 1
    assert evidence[0]["source_key"] == "manual"
    assert evidence[0]["is_primary"] is True

    versions = await client.get(f"/api/v1/events/{first.json()['id']}/versions")
    assert versions.status_code == 200
    assert [version["version"] for version in versions.json()] == [1]


async def test_date_only_event_never_fabricates_midnight(client: AsyncClient) -> None:
    payload = {
        "title_zh": "台积电财报发布",
        "institution": "TSMC",
        "country_code": "TW",
        "category": "corporate",
        "event_type": "earnings_release",
        "status": "tba",
        "importance": "high",
        "date_precision": "date",
        "local_date": "2026-10-15",
        "original_timezone": "Asia/Taipei",
    }
    response = await client.post("/api/v1/events", json=payload)
    assert response.status_code == 201
    assert response.json()["local_date"] == "2026-10-15"
    assert response.json()["starts_at"] is None
    assert response.json()["display_title"] == "台积电财报发布"

    invalid = dict(payload, starts_at="2026-10-15T00:00:00+08:00")
    invalid["idempotency_key"] = "invalid-midnight"
    error = await client.post("/api/v1/events", json=invalid)
    assert error.status_code == 422
    assert error.json()["error"]["code"] == "validation_error"


async def test_date_range_excludes_date_only_events_at_the_end_boundary(
    client: AsyncClient,
) -> None:
    base = {
        "title_zh": "边界日期事件",
        "title_original": "Boundary date event",
        "institution": "Test Institution",
        "country_code": "US",
        "category": "macro_release",
        "event_type": "activity",
        "status": "confirmed",
        "importance": "medium",
        "date_precision": "date",
        "original_timezone": "Asia/Shanghai",
        "market_tags": ["US"],
    }
    for day in ("2026-09-02", "2026-09-03"):
        response = await client.post(
            "/api/v1/events",
            json={**base, "local_date": day, "idempotency_key": f"boundary-{day}"},
        )
        assert response.status_code == 201

    result = await client.get(
        "/api/v1/events",
        params={
            "from": "2026-09-01T16:00:00Z",
            "to": "2026-09-02T16:00:00Z",
            "from_date": "2026-09-02",
            "to_date": "2026-09-03",
        },
    )
    assert [item["local_date"] for item in result.json()["items"]] == ["2026-09-02"]


async def test_date_range_event_is_returned_on_each_overlapping_day(
    client: AsyncClient,
) -> None:
    created = await client.post("/api/v1/events", json={
        "title_zh": "FOMC 利率决议",
        "institution": "Federal Reserve",
        "country_code": "US",
        "category": "monetary_policy",
        "event_type": "central_bank_decision",
        "status": "tba",
        "importance": "critical",
        "date_precision": "date",
        "local_date": "2026-09-16",
        "date_range_start": "2026-09-15",
        "date_range_end": "2026-09-16",
        "original_timezone": "America/New_York",
    })
    assert created.status_code == 201
    assert created.json()["date_range_start"] == "2026-09-15"

    for day, next_day in (("2026-09-15", "2026-09-16"), ("2026-09-16", "2026-09-17")):
        result = await client.get(
            "/api/v1/events", params={"from_date": day, "to_date": next_day}
        )
        assert [item["id"] for item in result.json()["items"]] == [created.json()["id"]]

    outside = await client.get(
        "/api/v1/events",
        params={"from_date": "2026-09-17", "to_date": "2026-09-18"},
    )
    assert outside.json()["items"] == []


async def test_tba_to_specific_time_creates_one_change(client: AsyncClient) -> None:
    create = await client.post(
        "/api/v1/events",
        json={
            "title_zh": "财报说明会",
            "institution": "Example Corp",
            "country_code": "JP",
            "category": "corporate",
            "event_type": "earnings_call",
            "status": "tba",
            "importance": "medium",
            "date_precision": "date",
            "local_date": "2026-09-10",
            "original_timezone": "Asia/Tokyo",
        },
    )
    event_id = create.json()["id"]
    patch = {
        "status": "confirmed",
        "date_precision": "minute",
        "local_date": None,
        "starts_at": "2026-09-10T15:00:00+09:00",
        "original_time_text": "15:00 JST",
    }
    updated = await client.patch(f"/api/v1/events/{event_id}", json=patch)
    repeated = await client.patch(f"/api/v1/events/{event_id}", json=patch)
    assert updated.status_code == 200
    assert repeated.status_code == 200

    versions = (await client.get(f"/api/v1/events/{event_id}/versions")).json()
    changes = (await client.get(f"/api/v1/events/{event_id}/changes")).json()
    assert [item["version"] for item in versions] == [2, 1]
    assert changes[0]["change_type"] == "time_confirmed"
    assert changes[0]["created_at"].endswith("Z")


async def test_lock_and_soft_delete_flow(
    client: AsyncClient, minute_event_payload: dict[str, object]
) -> None:
    created = await client.post("/api/v1/events", json=minute_event_payload)
    event_id = created.json()["id"]
    locked = await client.put(
        f"/api/v1/events/{event_id}/locks/title_zh", json={"reason": "人工核验标题"}
    )
    assert locked.status_code == 200
    assert locked.json()["field_name"] == "title_zh"

    deleted = await client.delete(f"/api/v1/events/{event_id}")
    assert deleted.status_code == 204
    assert (await client.get(f"/api/v1/events/{event_id}")).status_code == 404


async def test_change_feed_omits_soft_deleted_events_but_keeps_their_audit_rows(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    minute_event_payload: dict[str, object],
) -> None:
    created = await client.post("/api/v1/events", json=minute_event_payload)
    event_id = created.json()["id"]

    feed = (await client.get("/api/v1/changes")).json()
    assert [row["event_id"] for row in feed] == [event_id]

    assert (await client.delete(f"/api/v1/events/{event_id}")).status_code == 204

    # The feed stops advertising an event the UI can no longer load, which is
    # what produced the phantom "event updated" rows and the 404s.
    assert (await client.get("/api/v1/changes")).json() == []

    # The append-only audit trail is intact: the `created` row and the
    # `deleted` row the soft delete itself recorded. Only the feed's view of
    # them changed.
    async with session_factory() as session:
        remaining = await session.scalar(
            select(func.count())
            .select_from(EventChange)
            .where(EventChange.event_id == UUID(event_id))
        )
    assert remaining == 2

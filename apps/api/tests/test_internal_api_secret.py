"""The shared-secret gate that makes a public Cloudflare Tunnel safe.

The API has no user accounts of its own -- it was written to sit behind network
isolation. Exposing it through a Tunnel therefore needs a gate, and that gate is
the ``X-Internal-Api-Secret`` header checked in ``main.require_internal_api_secret``.
These tests pin the three things that matter: unauthenticated callers are
rejected, authenticated callers are not, and the documented exemptions stay open.
"""

import pytest
from httpx import AsyncClient
from pydantic import SecretStr

from trade_calendar.core.config import get_settings
from trade_calendar.main import INTERNAL_API_SECRET_HEADER, settings

SECRET = "unit-test-internal-secret"


@pytest.fixture
def gate(monkeypatch: pytest.MonkeyPatch) -> str:
    """Switch the gate on for one test and return the expected secret."""
    monkeypatch.setattr(settings, "internal_api_secret", SecretStr(SECRET))
    return SECRET


async def test_missing_header_is_rejected(client: AsyncClient, gate: str) -> None:
    response = await client.get("/api/v1/sources")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


async def test_wrong_header_is_rejected(client: AsyncClient, gate: str) -> None:
    response = await client.get(
        "/api/v1/sources", headers={INTERNAL_API_SECRET_HEADER: "not-the-secret"}
    )
    assert response.status_code == 401


async def test_correct_header_is_accepted(client: AsyncClient, gate: str) -> None:
    response = await client.get(
        "/api/v1/sources", headers={INTERNAL_API_SECRET_HEADER: gate}
    )
    assert response.status_code == 200


async def test_gate_covers_unknown_paths_and_writes(
    client: AsyncClient, gate: str
) -> None:
    """The gate runs before routing, so it cannot be bypassed by guessing URLs."""
    assert (await client.get("/api/v1/does-not-exist")).status_code == 401
    assert (await client.post("/api/v1/settings", json={})).status_code == 401


async def test_health_stays_open_for_probes(client: AsyncClient, gate: str) -> None:
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


async def test_ics_feed_keeps_its_own_path_token(
    client: AsyncClient, gate: str
) -> None:
    """Calendar clients cannot send custom headers, so /calendar/* stays exempt."""
    token = get_settings().ics_token.get_secret_value()
    response = await client.get(f"/calendar/{token}.ics")
    assert response.status_code == 200


async def test_gate_is_inert_when_no_secret_is_configured(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "internal_api_secret", None)
    assert (await client.get("/api/v1/sources")).status_code == 200


async def test_blank_secret_means_unconfigured_not_open_by_accident(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A blank value must read as "no gate", never as "everyone is authorised".

    An absent header arrives as "", so a blank secret would match it and let
    every anonymous caller through while looking like a configured gate.
    """
    monkeypatch.setattr(settings, "internal_api_secret", SecretStr("   "))
    assert (await client.get("/api/v1/sources")).status_code == 200
    # and it must not start rejecting callers that do send a value
    response = await client.get(
        "/api/v1/sources", headers={INTERNAL_API_SECRET_HEADER: "anything"}
    )
    assert response.status_code == 200

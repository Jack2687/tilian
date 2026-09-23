from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.infra.messaging import EventBus


@pytest.mark.asyncio
async def test_cached_producer_does_not_make_disconnected_kafka_healthy():
    bus = EventBus()
    bus.settings = SimpleNamespace(kafka_enabled=True)
    update = AsyncMock(return_value=False)
    bus.producer = SimpleNamespace(client=SimpleNamespace(force_metadata_update=update))
    assert await bus.ping() is False
    update.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("state,members,expected", [("Empty", [], False), ("Stable", [("worker",)], True)])
async def test_readiness_checks_actual_consumer_group_members(monkeypatch, state, members, expected):
    bus = EventBus()
    bus.settings = SimpleNamespace(kafka_enabled=True, kafka_bootstrap_servers="broker:9092", kafka_consumer_group="local")
    client = SimpleNamespace(start=AsyncMock(), close=AsyncMock(), describe_consumer_groups=AsyncMock(
        return_value=[SimpleNamespace(groups=[(0, "local", state, "consumer", "range", members)])]))
    monkeypatch.setattr("app.infra.messaging.AIOKafkaAdminClient", lambda **kwargs: client)
    assert (await bus.worker_status())["ready"] is expected
    client.describe_consumer_groups.assert_awaited_once_with(["local"])
    client.close.assert_awaited_once()

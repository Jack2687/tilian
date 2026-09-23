from __future__ import annotations

import asyncio
import json
from typing import Any

from aiokafka import AIOKafkaProducer
from aiokafka.admin import AIOKafkaAdminClient

from ..config import get_settings


class EventBus:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.producer: AIOKafkaProducer | None = None

    async def start(self) -> None:
        if not self.settings.kafka_enabled or self.producer:
            return
        self.producer = AIOKafkaProducer(
            bootstrap_servers=self.settings.kafka_bootstrap_servers.split(","),
            acks="all",
            enable_idempotence=True,
            value_serializer=lambda value: json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode(),
            key_serializer=lambda value: str(value).encode(),
        )
        await self.producer.start()

    async def stop(self) -> None:
        if self.producer:
            await self.producer.stop()
            self.producer = None

    async def publish(self, topic: str, key: str, payload: dict[str, Any]) -> None:
        if not self.producer:
            if self.settings.kafka_enabled:
                await self.start()
            else:
                return
        assert self.producer
        await self.producer.send_and_wait(topic, key=key, value=payload)

    async def ping(self) -> bool:
        if not self.settings.kafka_enabled:
            return False
        if not self.producer:
            await self.start()
        assert self.producer
        # A cached producer object can survive a disconnected broker. Request
        # fresh metadata so readiness reflects connectivity at check time.
        return bool(await asyncio.wait_for(self.producer.client.force_metadata_update(), timeout=5))

    async def worker_status(self) -> dict[str, Any]:
        """Inspect the configured group without joining it or consuming work."""
        if not self.settings.kafka_enabled:
            return {"ready": False, "state": "Disabled", "members": 0}
        admin = AIOKafkaAdminClient(
            bootstrap_servers=self.settings.kafka_bootstrap_servers.split(","),
            request_timeout_ms=5000,
        )
        try:
            await admin.start()
            responses = await admin.describe_consumer_groups([self.settings.kafka_consumer_group])
            for response in responses:
                for group in response.groups:
                    if group[1] == self.settings.kafka_consumer_group:
                        count = len(group[5])
                        return {"ready": group[0] == 0 and group[2] == "Stable" and count > 0,
                                "state": group[2], "members": count}
            return {"ready": False, "state": "Missing", "members": 0}
        finally:
            await admin.close()


event_bus = EventBus()

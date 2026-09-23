from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import select

from ..database import SessionLocal
from ..models import OutboxEvent
from .messaging import event_bus


logger = logging.getLogger("tilian.outbox")


async def relay_once(limit: int = 100) -> int:
    async with SessionLocal() as session:
        result = await session.scalars(
            select(OutboxEvent)
            .where(OutboxEvent.status == "pending")
            .order_by(OutboxEvent.created_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        events = result.all()
        published = 0
        for event in events:
            try:
                await event_bus.publish(event.topic, event.message_key, event.payload)
            except Exception:
                event.attempts += 1
                logger.exception("outbox publish failed event_id=%s", event.event_id)
                continue
            event.status = "published"
            event.published_at = datetime.now(timezone.utc)
            event.attempts += 1
            published += 1
        await session.commit()
        return published


async def relay_forever(stop: asyncio.Event) -> None:
    while not stop.is_set():
        published = await relay_once()
        try:
            await asyncio.wait_for(stop.wait(), timeout=0.1 if published else 1.0)
        except TimeoutError:
            pass

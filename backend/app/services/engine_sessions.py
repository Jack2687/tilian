from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select

from ..database import SessionLocal
from ..engine_protocol import CONTROL_EVENT, apply_engine_event
from ..models import TaskEvent, TaskItem
from ..security import decrypt_credential, encrypt_credential


def queue_engine_control(session: Any, task_id: str, item: TaskItem, command: dict[str, Any]) -> None:
    payload = dict(command)
    if "code" in payload:
        payload["encryptedCode"] = encrypt_credential(str(payload.pop("code"))).decode("ascii")
    session.add(TaskEvent(
        event_id=uuid.uuid4().hex, task_id=task_id, item_index=item.item_index,
        event_type=CONTROL_EVENT, payload={**payload, "attempt": item.attempt},
    ))


async def persist_engine_event(task_id: str, item_index: int, attempt: int, message: dict[str, Any]) -> None:
    async with SessionLocal() as session:
        item = await session.scalar(select(TaskItem).where(TaskItem.task_id == task_id, TaskItem.item_index == item_index).with_for_update())
        if not item or item.attempt != attempt or item.status not in {"running", "waiting_blik", "waiting_plus"}:
            return
        apply_engine_event(item, message)
        # The public event stream contains only projected fields, never stdin controls.
        session.add(TaskEvent(
            event_id=uuid.uuid4().hex, task_id=task_id, item_index=item_index,
            event_type="task.item.progress.v1",
            payload={"stage": item.stage, "detail": item.stage_detail, "status": item.status},
        ))
        await session.commit()


async def pop_engine_control(task_id: str, item_index: int, attempt: int) -> dict[str, Any] | None:
    async with SessionLocal() as session:
        item = await session.scalar(select(TaskItem).where(TaskItem.task_id == task_id, TaskItem.item_index == item_index))
        if not item or item.status == "canceled" or item.attempt != attempt:
            return {"command": "cancel"}
        control = await session.scalar(select(TaskEvent).where(
            TaskEvent.task_id == task_id, TaskEvent.item_index == item_index,
            TaskEvent.event_type == CONTROL_EVENT,
        ).order_by(TaskEvent.created_at).with_for_update(skip_locked=True).limit(1))
        if not control:
            return None
        payload = dict(control.payload)
        await session.delete(control)
        await session.commit()
        if payload.pop("attempt", None) != attempt:
            return None
        encrypted_code = payload.pop("encryptedCode", None)
        if encrypted_code:
            payload["code"] = decrypt_credential(encrypted_code.encode("ascii"))
        return payload

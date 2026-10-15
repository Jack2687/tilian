import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models import Task, TaskEvent, TaskItem
from app.services import engine_sessions


@asynccontextmanager
async def task_event_database(monkeypatch, *, status="running", attempt=2):
    db = create_async_engine("sqlite+aiosqlite:///:memory:")
    sessions = async_sessionmaker(db, expire_on_commit=False)
    async with db.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    monkeypatch.setattr(engine_sessions, "SessionLocal", sessions)
    try:
        async with sessions() as session:
            task = Task(
                id="task-events", owner_key="owner", owner_mask="test", mode="test",
                mode_name="Test", status="running", request_id="task-events", payload={},
            )
            task.items.append(TaskItem(
                item_index=0, credential=b"test", status=status,
                attempt=attempt, stage="start", stage_detail="Started", result={},
            ))
            session.add(task)
            await session.commit()
        yield sessions
    finally:
        await db.dispose()


@pytest.mark.asyncio
async def test_stage_events_keep_outcome_and_attempt_separate_from_task_status(monkeypatch):
    async with task_event_database(monkeypatch) as sessions:
        for outcome, detail in [("fail", "Temporary failure"), ("ok", "Recovered")]:
            await engine_sessions.persist_engine_event("task-events", 0, 2, {
                "event": "stage", "stage": "fetch", "status": outcome, "detail": detail,
                "token": "private-token", "code": "private-code",
                "extra": {"authorization": "private-authorization"},
            })
        async with sessions() as session:
            events = (await session.scalars(select(TaskEvent).order_by(TaskEvent.created_at))).all()
            assert [event.payload for event in events] == [
                {"stage": "fetch", "detail": "Temporary failure", "status": "running", "attempt": 2, "stageStatus": "fail"},
                {"stage": "fetch", "detail": "Recovered", "status": "running", "attempt": 2, "stageStatus": "ok"},
            ]
            item = await session.scalar(select(TaskItem))
            assert item.status == "running"
            assert item.stage_detail == "Recovered"
            assert item.result == {}


@pytest.mark.asyncio
async def test_control_event_attempt_does_not_replace_task_attempt_in_public_log(monkeypatch):
    async with task_event_database(monkeypatch) as sessions:
        await engine_sessions.persist_engine_event("task-events", 0, 2, {
            "event": "blik_waiting", "attempt": 1, "max_attempts": 3,
        })
        async with sessions() as session:
            event = await session.scalar(select(TaskEvent))
            assert event.payload == {
                "stage": "waiting_blik", "detail": "等待输入 6 位 BLIK 动态码",
                "status": "waiting_blik", "attempt": 2,
            }


@pytest.mark.asyncio
@pytest.mark.parametrize("status,event_attempt", [("running", 1), ("failed", 2), ("canceled", 2), ("success", 2)])
async def test_stale_or_terminal_item_does_not_receive_progress_events(monkeypatch, status, event_attempt):
    async with task_event_database(monkeypatch, status=status) as sessions:
        await engine_sessions.persist_engine_event("task-events", 0, event_attempt, {
            "event": "stage", "stage": "late", "status": "ok", "detail": "Late progress",
        })
        async with sessions() as session:
            assert await session.scalar(select(TaskEvent)) is None
            item = await session.scalar(select(TaskItem))
            assert item.stage == "start"
            assert item.status == status


@pytest.mark.asyncio
@pytest.mark.parametrize("outcomes,expected_statuses", [
    ([{"ok": True, "link": "https://example.test/result"}], ["success"]),
    ([{"ok": False, "error": "Fixture failure", "reason_code": "fixture_failed"}], ["failed"]),
    ([
        {"ok": False, "error": "Temporary failure", "reason_code": "fixture_transport", "retryable": True},
        {"ok": True, "link": "https://example.test/result"},
    ], ["queued", "success"]),
])
async def test_worker_persists_start_and_completion_for_each_attempt(monkeypatch, outcomes, expected_statuses):
    from app import worker

    @asynccontextmanager
    async def lease(*_):
        yield True

    async def no_external_call(*_):
        return None

    async with task_event_database(monkeypatch, status="queued", attempt=0) as sessions:
        monkeypatch.setattr(worker, "SessionLocal", sessions)
        monkeypatch.setattr(worker.cache, "lease", lease)
        monkeypatch.setattr(worker, "publish_event", no_external_call)
        monkeypatch.setattr(worker, "settle_quota", no_external_call)
        monkeypatch.setattr(worker, "decrypt_account_credential", lambda _: ("fixture", ""))
        monkeypatch.setattr(worker, "build_attempt_fingerprint", lambda *_: {})
        monkeypatch.setattr(worker.settings, "task_max_retries", 2)
        monkeypatch.setattr(worker, "asyncio", SimpleNamespace(sleep=no_external_call))
        executions = 0

        async def execute(*_, **__):
            nonlocal executions
            executions += 1
            async with sessions() as session:
                started = (await session.scalars(select(TaskEvent).where(
                    TaskEvent.event_type == "task.item.started.v1",
                ))).all()
                assert len(started) == executions
                assert started[-1].payload == {
                    "attempt": executions, "status": "running", "stage": "start",
                    "detail": "Worker 已领取任务",
                }
            return outcomes[executions - 1], []

        monkeypatch.setattr(worker, "adapter", SimpleNamespace(execute=execute))
        await worker.execute_item("task-events", 0, asyncio.Semaphore(1))
        async with sessions() as session:
            events = (await session.scalars(select(TaskEvent).order_by(TaskEvent.created_at))).all()
            assert [event.event_type for event in events] == [
                event_type for _ in outcomes
                for event_type in ["task.item.started.v1", "task.item.completed.v1"]
            ]
            completed = [event.payload for event in events if event.event_type == "task.item.completed.v1"]
            assert [event["attempt"] for event in completed] == list(range(1, len(outcomes) + 1))
            assert [event["status"] for event in completed] == expected_statuses
            assert [event["retry"] for event in completed] == [status == "queued" for status in expected_statuses]
            assert [event["ok"] for event in completed] == [status == "success" for status in expected_statuses]
            item = await session.scalar(select(TaskItem))
            assert item.status == expected_statuses[-1]

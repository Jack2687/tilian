from contextlib import asynccontextmanager

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.main import clear_tasks
from app.models import QuotaCard, QuotaLedger, Task, TaskItem
from app.security import owner_key
from app.services.tasks import TaskService


CDK = "cancel-test-card"


@asynccontextmanager
async def database(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'cancel.sqlite').as_posix()}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions() as session:
            session.add(QuotaCard(code_hash=owner_key(CDK), code_mask="test", quota=10, used_quota=1, reserved_quota=3))
            task = Task(id="cancel-task", owner_key=owner_key(CDK), owner_mask="test", mode="momo", status="running", request_id="cancel-request")
            task.items = [TaskItem(item_index=index, credential=b"unused", status=status) for index, status in enumerate(["queued", "running", "waiting_plus", "success"])]
            session.add(task)
            await session.commit()
        yield sessions
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_cancel_releases_only_unfinished_items_and_is_idempotent(tmp_path):
    async with database(tmp_path) as sessions:
        async with sessions() as session:
            service = TaskService(session)
            first = await service.cancel("cancel-task", CDK)
            second = await service.cancel("cancel-task", CDK)
            card = await session.scalar(select(QuotaCard))
            ledger = (await session.scalars(select(QuotaLedger))).all()
            assert first["status"] == second["status"] == "canceled"
            assert [item["status"] for item in second["items"]] == ["canceled", "canceled", "canceled", "success"]
            assert card.reserved_quota == 0 and card.used_quota == 1
            assert len(ledger) == 3 and all(row.kind == "release" for row in ledger)


@pytest.mark.asyncio
async def test_cancel_refreshes_stale_items_after_worker_completion(tmp_path):
    async with database(tmp_path) as sessions:
        async with sessions() as cancel_session:
            stale_task = await cancel_session.scalar(select(Task))
            assert stale_task.items[1].status == "running"
            # Model the worker committing after the cancel request has loaded
            # the task, but before it acquires the item locks.
            async with sessions() as worker_session:
                item = await worker_session.scalar(select(TaskItem).where(TaskItem.item_index == 1))
                item.status = "success"
                card = await worker_session.scalar(select(QuotaCard))
                card.used_quota += 1
                card.reserved_quota -= 1
                worker_session.add(QuotaLedger(request_id="cancel-task:1", owner_key=owner_key(CDK), task_id="cancel-task", item_index=1, delta=1, kind="commit"))
                await worker_session.commit()
            result = await TaskService(cancel_session).cancel("cancel-task", CDK)
            assert result["items"][1]["status"] == "success"
            card = await cancel_session.scalar(select(QuotaCard))
            assert card.reserved_quota == 0 and card.used_quota == 2


@pytest.mark.asyncio
async def test_late_worker_settlement_cannot_charge_canceled_item(tmp_path, monkeypatch):
    from app import worker

    async with database(tmp_path) as sessions:
        monkeypatch.setattr(worker, "SessionLocal", sessions)
        async with sessions() as session:
            await TaskService(session).cancel("cancel-task", CDK)
            task = await session.scalar(select(Task))
            item = task.items[0]
        await worker.settle_quota(task, item, True)
        await worker.settle_quota(task, item, False)
        async with sessions() as session:
            card = await session.scalar(select(QuotaCard))
            ledger = (await session.scalars(select(QuotaLedger))).all()
            assert card.used_quota == 1 and card.reserved_quota == 0
            assert len(ledger) == 3


@pytest.mark.asyncio
async def test_clear_active_tasks_releases_reservations(tmp_path, monkeypatch):
    async def allow_card(*_args):
        return None

    monkeypatch.setattr("app.main.assert_user_card", allow_card)
    async with database(tmp_path) as sessions:
        async with sessions() as session:
            result = await clear_tasks(x_tilian_cdk=CDK, user=None, session=session)
            assert result["cleared"] == 1
            assert await session.scalar(select(Task)) is None
            card = await session.scalar(select(QuotaCard))
            assert card.reserved_quota == 0 and card.used_quota == 1

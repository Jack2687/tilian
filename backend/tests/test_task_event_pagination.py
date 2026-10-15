"""Task log pagination must retain late failures and existing access checks."""

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import main
from app.database import Base, get_session
from app.engine_protocol import CONTROL_EVENT
from app.models import QuotaCard, Task, TaskEvent, User
from app.security import issue_user_token, owner_key


CARD_CODE = "EVENT-PAGE-CARD"
OTHER_CARD_CODE = "EVENT-PAGE-OTHER-CARD"
TASK_ID = "event-page-task"
EVENT_TIME = datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest_asyncio.fixture
async def event_app(tmp_path, monkeypatch):
    db = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'events.sqlite').as_posix()}")
    sessions = async_sessionmaker(db, expire_on_commit=False)
    monkeypatch.setattr(main.settings, "owner_hash_secret", "event-page-test-owner")
    monkeypatch.setattr(main.settings, "auth_token_secret", "event-page-test-auth")

    async def unexpected_external_call(*args, **kwargs):
        pytest.fail("Event pagination tests must not contact external services")

    monkeypatch.setattr(main.legacy_client, "check_card", unexpected_external_call)
    monkeypatch.setattr(main.event_bus, "start", unexpected_external_call)
    monkeypatch.setattr(main.event_bus, "publish", unexpected_external_call)

    async def isolated_session():
        async with sessions() as session:
            yield session

    previous_overrides = main.app.dependency_overrides.copy()
    main.app.dependency_overrides[get_session] = isolated_session
    try:
        async with db.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions() as session:
            user = User(username="event-owner", password_hash="unused")
            other_user = User(username="event-other-user", password_hash="unused")
            session.add_all([user, other_user])
            await session.flush()
            session.add_all([
                QuotaCard(code_hash=owner_key(CARD_CODE), code_mask="event-card", quota=10, owner_user_id=user.id),
                QuotaCard(code_hash=owner_key(OTHER_CARD_CODE), code_mask="other-card", quota=10, owner_user_id=user.id),
                Task(id=TASK_ID, owner_key=owner_key(CARD_CODE), owner_mask="event-card", mode="test", request_id=TASK_ID),
                Task(id="other-task", owner_key=owner_key(CARD_CODE), owner_mask="event-card", mode="test", request_id="other-task"),
            ])
            await session.commit()
            headers = {"Authorization": f"Bearer {issue_user_token(user.id)}", "X-Tilian-CDK": CARD_CODE}
            other_headers = {"Authorization": f"Bearer {issue_user_token(other_user.id)}", "X-Tilian-CDK": CARD_CODE}
        # ASGITransport does not start the production lifespan or any workers.
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
            yield SimpleNamespace(client=client, sessions=sessions, headers=headers, other_headers=other_headers)
    finally:
        main.app.dependency_overrides.clear()
        main.app.dependency_overrides.update(previous_overrides)
        await db.dispose()


def public_event(index, *, created_at=None, task_id=TASK_ID, event_type="task.item.progress.v1", payload=None):
    return TaskEvent(
        event_id=f"event-{index:04d}", task_id=task_id, item_index=0,
        event_type=event_type, created_at=created_at or EVENT_TIME + timedelta(seconds=index),
        payload=payload or {"stage": "processing", "detail": f"Event {index}", "status": "running"},
    )


async def read_page(app, **params):
    response = await app.client.get(f"/api/v1/tasks/{TASK_ID}/events", headers=app.headers, params=params)
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.text.endswith("event: end\ndata: {}\n\n")
    return [
        json.loads(block.removeprefix("data: "))
        for block in response.text.split("\n\n")
        if block.startswith("data: ")
    ]


@pytest.mark.asyncio
async def test_pagination_exposes_failure_after_first_two_hundred_events(event_app):
    app = event_app
    async with app.sessions() as session:
        session.add_all([public_event(index) for index in range(204)])
        session.add(public_event(204, event_type="task.item.completed.v1", payload={
            "status": "failed", "stage": "complete", "error": "Tail failure", "ok": False,
        }))
        await session.commit()
    first = await read_page(app)
    second = await read_page(app, afterEventId=first[-1]["eventId"])
    assert len(first) == 200 and len(second) == 5
    assert [event["eventId"] for event in first + second] == [f"event-{index:04d}" for index in range(205)]
    assert second[-1]["type"] == "task.item.completed.v1"
    assert second[-1]["payload"]["error"] == "Tail failure"
    assert set(first[0]) == {"eventId", "type", "itemIndex", "payload", "createdAt"}
    assert await read_page(app, afterEventId=second[-1]["eventId"]) == []

    async with app.sessions() as session:
        session.add(public_event(205))
        await session.commit()
    incremental = await read_page(app, afterEventId=second[-1]["eventId"])
    assert [event["eventId"] for event in incremental] == ["event-0205"]


@pytest.mark.asyncio
async def test_equal_timestamps_use_event_id_as_stable_cursor_and_filter_controls(event_app):
    app = event_app
    async with app.sessions() as session:
        session.add_all([public_event(index, created_at=EVENT_TIME) for index in reversed(range(7))])
        session.add(TaskEvent(
            event_id="event-0002-control", task_id=TASK_ID, event_type=CONTROL_EVENT,
            created_at=EVENT_TIME, payload={"secret": "internal-control"},
        ))
        await session.commit()
    events = []
    cursor = None
    while True:
        page = await read_page(app, limit=2, **({"afterEventId": cursor} if cursor else {}))
        events.extend(page)
        if len(page) < 2:
            break
        cursor = page[-1]["eventId"]
    assert [event["eventId"] for event in events] == [f"event-{index:04d}" for index in range(7)]
    assert all(event["type"] != CONTROL_EVENT for event in events)


@pytest.mark.asyncio
@pytest.mark.parametrize("cursor", ["unknown-event", "event-0001", "control-event", ""])
async def test_missing_foreign_or_control_cursor_is_rejected(event_app, cursor):
    app = event_app
    async with app.sessions() as session:
        session.add(public_event(1, task_id="other-task"))
        session.add(TaskEvent(event_id="control-event", task_id=TASK_ID, event_type=CONTROL_EVENT, payload={}))
        await session.commit()
    response = await app.client.get(
        f"/api/v1/tasks/{TASK_ID}/events", headers=app.headers, params={"afterEventId": cursor},
    )
    assert response.status_code == 400
    assert "游标无效" in response.json()["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 501}, {"limit": "invalid"}, {"afterEventId": "x" * 65}])
async def test_pagination_query_bounds_are_validated(event_app, params):
    response = await event_app.client.get(
        f"/api/v1/tasks/{TASK_ID}/events", headers=event_app.headers, params=params,
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_task_and_card_authorization_precede_cursor_lookup(event_app):
    app = event_app
    url = f"/api/v1/tasks/{TASK_ID}/events"
    cursor = {"afterEventId": "unknown-event"}
    no_user = await app.client.get(url, headers={"X-Tilian-CDK": CARD_CODE}, params=cursor)
    assert no_user.status_code == 401
    no_card = await app.client.get(url, headers={"Authorization": app.headers["Authorization"]}, params=cursor)
    assert no_card.status_code == 400
    assert "X-Tilian-CDK" in no_card.json()["message"]
    wrong_user = await app.client.get(url, headers=app.other_headers, params=cursor)
    assert wrong_user.status_code == 403
    wrong_card = await app.client.get(url, headers={**app.headers, "X-Tilian-CDK": OTHER_CARD_CODE}, params=cursor)
    assert wrong_card.status_code == 404
    missing_task = await app.client.get("/api/v1/tasks/missing-task/events", headers=app.headers, params=cursor)
    assert missing_task.status_code == 404

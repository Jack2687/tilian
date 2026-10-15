"""Administrator concurrency changes must reach existing users and admission."""

from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import main
from app.database import Base, get_session
from app.models import OutboxEvent, QuotaCard, QuotaLedger, RuntimeConfig, Task, User
from app.schemas import TaskCreate
from app.security import issue_user_token, owner_key, password_hash
from app.services.tasks import TaskService


ADMIN_HEADERS = {"Authorization": "Bearer concurrency-test-admin"}
CARD_CODE = "CONCURRENCY-TEST-CARD"
PASSWORD = "concurrency-test-password"


@pytest_asyncio.fixture
async def concurrency_app(tmp_path, monkeypatch):
    """All API requests use this database; no app startup or worker is run."""
    db = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 'concurrency.sqlite').as_posix()}"
    )
    sessions = async_sessionmaker(db, expire_on_commit=False)
    monkeypatch.setattr(main.settings, "execution_mode", "distributed")
    monkeypatch.setattr(main.settings, "admin_api_token", "concurrency-test-admin")
    monkeypatch.setattr(main.settings, "owner_hash_secret", "concurrency-test-owner")
    monkeypatch.setattr(main.settings, "auth_token_secret", "concurrency-test-auth")
    monkeypatch.setattr(main.settings, "credential_encryption_key", "")
    monkeypatch.setattr(main.settings, "redis_url", "")
    monkeypatch.setattr(main.settings, "kafka_bootstrap_servers", "")

    async def unexpected_external_call(*args, **kwargs):
        pytest.fail("Concurrency tests must not contact legacy services or Kafka")

    monkeypatch.setattr(main.legacy_client, "check_card", unexpected_external_call)
    monkeypatch.setattr(main.legacy_client, "create_task", unexpected_external_call)
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
            user = User(
                username="existing-user",
                password_hash=password_hash(PASSWORD),
                task_concurrency=3,
                max_accounts_per_task=100,
            )
            session.add_all([
                user,
                RuntimeConfig(
                    key="concurrency",
                    value={"default": 3, "vip": 10, "globalLimit": 100},
                ),
                RuntimeConfig(
                    key="user_policy",
                    value={"defaultConcurrency": 3, "registrationEnabled": True},
                ),
                QuotaCard(
                    code_hash=owner_key(CARD_CODE),
                    code_mask="test-card",
                    quota=20,
                ),
            ])
            await session.commit()
            user_id = user.id
        # ASGITransport deliberately does not enter the production lifespan,
        # which would start schema migration and the outbox relay.
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=main.app), base_url="http://test"
        ) as client:
            yield SimpleNamespace(
                client=client,
                sessions=sessions,
                user_id=user_id,
                headers={"Authorization": f"Bearer {issue_user_token(user_id)}"},
            )
    finally:
        main.app.dependency_overrides.clear()
        main.app.dependency_overrides.update(previous_overrides)
        await db.dispose()


async def set_concurrency(app, default, global_limit=100):
    response = await app.client.put(
        "/api/v1/admin/concurrency",
        headers=ADMIN_HEADERS,
        json={"default": default, "vip": 10, "globalLimit": global_limit},
    )
    assert response.status_code == 200, response.text
    return response.json()


def task_payload(concurrency):
    return {
        "mode": "upi",
        "cardCode": CARD_CODE,
        "accounts": [{"at": "synthetic-test-access-token"}],
        "concurrency": concurrency,
        "proxyMode": "custom",
        "proxiesText": "http://proxy.example.test:8080",
    }


async def assert_no_task_side_effects(app):
    async with app.sessions() as session:
        card = await session.scalar(select(QuotaCard))
        assert card.owner_user_id is None
        assert card.bound_at is None
        assert card.used_quota == 0
        assert card.reserved_quota == 0
        assert card.version == 0
        for model in (Task, OutboxEvent, QuotaLedger):
            assert await session.scalar(select(func.count()).select_from(model)) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("old_user_limit", [3, 50])
async def test_saved_concurrency_reaches_existing_user_and_login(
    concurrency_app, old_user_limit
):
    app = concurrency_app
    async with app.sessions() as session:
        user = await session.get(User, app.user_id)
        user.task_concurrency = old_user_limit
        await session.commit()

    await set_concurrency(app, 10)
    me = await app.client.get("/api/v1/auth/me", headers=app.headers)
    assert me.status_code == 200, me.text
    assert me.json()["user"]["taskConcurrency"] == 10
    meta = await app.client.get("/api/v1/meta")
    assert meta.status_code == 200, meta.text
    assert meta.json()["limits"]["maxConcurrency"] == 10
    users = await app.client.get("/api/v1/admin/users", headers=ADMIN_HEADERS)
    assert users.status_code == 200, users.text
    assert users.json()["items"][0]["taskConcurrency"] == 10
    login = await app.client.post(
        "/api/v1/auth/login",
        json={"username": "existing-user", "password": PASSWORD},
    )
    assert login.status_code == 200, login.text
    assert login.json()["user"]["taskConcurrency"] == 10


@pytest.mark.asyncio
async def test_new_registration_uses_shared_limit_instead_of_stale_policy(concurrency_app):
    app = concurrency_app
    await set_concurrency(app, 10)
    registered = await app.client.post(
        "/api/v1/auth/register",
        json={"username": "new-user", "password": PASSWORD},
    )
    assert registered.status_code == 201, registered.text
    assert registered.json()["user"]["taskConcurrency"] == 10
    async with app.sessions() as session:
        user = await session.scalar(select(User).where(User.username == "new-user"))
        assert user.task_concurrency == 10


@pytest.mark.asyncio
async def test_task_admission_tracks_changes_without_rewriting_existing_tasks(concurrency_app):
    app = concurrency_app
    await set_concurrency(app, 10)
    rejected = await app.client.post(
        "/api/v1/tasks", headers=app.headers, json=task_payload(11)
    )
    assert rejected.status_code == 400, rejected.text
    assert "10" in rejected.json()["message"]
    await assert_no_task_side_effects(app)

    accepted = await app.client.post(
        "/api/v1/tasks", headers=app.headers, json=task_payload(10)
    )
    assert accepted.status_code == 201, accepted.text
    assert accepted.json()["concurrency"] == 10
    task_id = accepted.json()["id"]

    await set_concurrency(app, 4)
    me = await app.client.get("/api/v1/auth/me", headers=app.headers)
    assert me.status_code == 200, me.text
    assert me.json()["user"]["taskConcurrency"] == 4
    rejected_after_change = await app.client.post(
        "/api/v1/tasks", headers=app.headers, json=task_payload(5)
    )
    assert rejected_after_change.status_code == 400, rejected_after_change.text
    async with app.sessions() as session:
        task = await session.get(Task, task_id)
        assert task.concurrency == 10
        card = await session.scalar(select(QuotaCard))
        assert card.reserved_quota == 1
        assert await session.scalar(select(func.count()).select_from(Task)) == 1

    accepted_after_change = await app.client.post(
        "/api/v1/tasks", headers=app.headers, json=task_payload(4)
    )
    assert accepted_after_change.status_code == 201, accepted_after_change.text
    assert accepted_after_change.json()["concurrency"] == 4


@pytest.mark.asyncio
async def test_task_service_without_user_cannot_bypass_shared_limit(concurrency_app):
    app = concurrency_app
    await set_concurrency(app, 10)
    async with app.sessions() as session:
        with pytest.raises(ValueError, match="10"):
            await TaskService(session).create(TaskCreate(**task_payload(11)), "over-limit")
        # Check pending in-memory mutations as well as committed database state.
        card = await session.scalar(select(QuotaCard))
        assert card.owner_user_id is None
        assert card.reserved_quota == 0
        assert not session.new
    await assert_no_task_side_effects(app)


@pytest.mark.asyncio
async def test_global_limit_caps_public_limit_and_task_admission(concurrency_app):
    app = concurrency_app
    await set_concurrency(app, 10, global_limit=6)
    me = await app.client.get("/api/v1/auth/me", headers=app.headers)
    meta = await app.client.get("/api/v1/meta")
    assert me.status_code == meta.status_code == 200
    assert me.json()["user"]["taskConcurrency"] == 6
    assert meta.json()["limits"]["maxConcurrency"] == 6
    rejected = await app.client.post(
        "/api/v1/tasks", headers=app.headers, json=task_payload(7)
    )
    assert rejected.status_code == 400, rejected.text
    await assert_no_task_side_effects(app)
    accepted = await app.client.post(
        "/api/v1/tasks", headers=app.headers, json=task_payload(6)
    )
    assert accepted.status_code == 201, accepted.text
    assert accepted.json()["concurrency"] == 6


@pytest.mark.asyncio
async def test_stale_user_policy_cannot_overwrite_shared_concurrency(concurrency_app):
    app = concurrency_app
    await set_concurrency(app, 10)
    policy = await app.client.get("/api/v1/admin/user-policy", headers=ADMIN_HEADERS)
    assert policy.status_code == 200, policy.text
    assert policy.json()["defaultConcurrency"] == 10
    saved = await app.client.put(
        "/api/v1/admin/user-policy",
        headers=ADMIN_HEADERS,
        json={
            "registrationEnabled": False,
            "defaultConcurrency": 3,
            "maxUsers": 123,
            "maxAccountsPerTask": 25,
            "allowFusion": False,
        },
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["defaultConcurrency"] == 10
    assert saved.json()["registrationEnabled"] is False
    assert saved.json()["maxAccountsPerTask"] == 25
    config = await app.client.get("/api/v1/admin/concurrency", headers=ADMIN_HEADERS)
    assert config.status_code == 200, config.text
    assert config.json()["default"] == 10
    me = await app.client.get("/api/v1/auth/me", headers=app.headers)
    assert me.json()["user"]["taskConcurrency"] == 10


@pytest.mark.asyncio
async def test_user_editor_rejects_independent_concurrency_but_keeps_other_fields(concurrency_app):
    app = concurrency_app
    await set_concurrency(app, 10)
    rejected = await app.client.patch(
        f"/api/v1/admin/users/{app.user_id}",
        headers=ADMIN_HEADERS,
        json={"taskConcurrency": 20, "displayName": "Must not be applied"},
    )
    assert rejected.status_code == 400, rejected.text
    async with app.sessions() as session:
        user = await session.get(User, app.user_id)
        assert user.display_name != "Must not be applied"
        assert user.task_concurrency == 3
    edited = await app.client.patch(
        f"/api/v1/admin/users/{app.user_id}",
        headers=ADMIN_HEADERS,
        json={"displayName": "Edited user", "maxAccountsPerTask": 25},
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["user"]["taskConcurrency"] == 10
    assert edited.json()["user"]["displayName"] == "Edited user"
    assert edited.json()["user"]["maxAccountsPerTask"] == 25
